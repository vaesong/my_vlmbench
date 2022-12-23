import random
from typing import List, Tuple, Dict, Optional, Any, Union
import itertools
import pickle
import os
import time
import json
from pathlib import Path
import torch
from torch import nn
import torch.optim as optim
from torch.utils.tensorboard import SummaryWriter  # type: ignore
from torch.utils.data import DataLoader, Dataset
from torch.utils.data._utils.collate import default_collate
from tools.TimeAverageMeter import AverageMeter, sec_to_str
import numpy as np
from tqdm import tqdm, trange
from filelock import FileLock
import tap
from hiverformer.process_instructions import get_language_feat
from hiverformer.network import Hiveformer
from hiverformer.dataset import My_Dataset, RLBenchDataset
from hiverformer.utils import (
    LossAndMetrics,
    count_parameters,
)
from vlm.scripts.VLDataloader_renjie import VLM_dataset
import torch.multiprocessing as mp
import torch.distributed as dist


class Arguments(tap.Tap):
    accumulate_grad_batches: int = 1
    cameras: list = ['left_shoulder','right_shoulder','wrist']
    checkpoint: Optional[Path] = None
    checkpoint_period: int = 10000
    
    xp: Path = "/home/liuchang/projects/VLMbench/VLMbench/xp"
    valset: Optional[Tuple[Path, ...]] = None
    name: str = "hiveformer"
    arch: str = "mct"
    num_workers: int = 5
    max_tries: int = 10
    max_episodes_per_taskvar: int = 100
    instructions: Optional[Path] = None
    cache_size: int = 300
    seed: int = 2

    # tasks: Tuple[str, ...]
    tasks: Tuple[str, ...]
    train_tasks: List = []
    train_variations: Tuple[int, ...] = (0,1,2,)
    valid_variations: Tuple[int, ...] = (0,)

    # Train
    batch_size: int = 64 # 25
    lr: float = 0.0005
    val_freq: int = 10000     # 200
    val_batch_size: int = 64
    jitter: bool = False
    
    # 自己加的
    train_dir: Path = "/home/liuchang/projects/VLMbench/VLMbench/hiverformer/packaged"
    valid_dir: Path = "/home/liuchang/projects/VLMbench/VLMbench/hiverformer/packaged/valid"
    epochs: int = 400000
    relative: bool = False
    renew_obs: bool = False
    add_low_lang: bool = True
    maxAction: int = 4

    img_size: list = [128, 128]
    unused_camera_list: list = ['overhead','front']
    preprocess: bool = False
    use_fail_cases: bool = False
    sample_numbers: int = 0
    workers: int = 0
    persistent_workers: bool = False
    gpu: int = 0

    # distributed
    world_size: int = 1
    rank: int = 0
    dist_url: str = 'tcp://127.0.0.1:23462'
    dist_backend: str = 'nccl'
    gpu_start: int = 0
    gpu_list: list = [2,3,4,5]
    # gpu_list: list = [6,7]
    gpu_number: int = 0
    ngpus_per_node: int = 0
    distributed: bool = False

    # tests
    headless: bool = True
    output: Path = Path(__file__).parent / "records.txt"

    # model
    depth: int = 4
    dim_feedforward: int = 64
    hidden_dim: int = 64
    instr_size: int = 512
    mask_obs_prob: float = 0.0
    num_layers: int = 1
    num_words: int = 75
    num_tasks: int = 24
    mode: str = 'train_blue_red_green'


def training(
    model: nn.Module,
    optimizer,
    train_loader,
    train_sampler,
    val_loaders,
    checkpointer,
    loss_and_metrics,
    args: Arguments,
    writer: SummaryWriter,
):
    # model.eval()
    iter_loader = iter(train_loader)
    device = next(model.parameters()).device

    print('---------------------------------------------start------------------------------------------------------')
    with trange(args.epochs, ncols=100) as tbar:
        for step_id in tbar:
            try:
                sample = next(iter_loader)
            except StopIteration:
                iter_loader = iter(train_loader)
                sample = next(iter_loader)

            rgbs = sample["rgbs"].to(device) # B 4key_frame 3camera 4channel(rgb+attn) 128 128 except for the end index img
            pcds = sample["pcds"].to(device) # B 4key_frame 3camera 3channel 128 128 except for the end index pcd
            gripper = sample["gripper"].to(device) # B 4key_frame 8(action_ls[:-1]) except for the end index action
            outputs = sample["action"].to(device) # B 4key_frame 8(action_ls[1:]) except for the start index action 
            padding_mask = sample["padding_mask"].to(device)

            lang_feat = sample["language"].to(device) # B 53 512
            # lang_feat = get_language_feat(instr, "clip", args.num_words, device).float().to(device)  # B 75 512

            if step_id % args.accumulate_grad_batches == 0:
                optimizer.zero_grad()

            pred = model(
                rgbs,   # 
                pcds,
                padding_mask,
                lang_feat,
                gripper,
            )

            train_losses = loss_and_metrics.compute_loss(pred, sample)
            train_losses["total"] = sum(list(train_losses.values()))  # type: ignore

            for n, l in train_losses.items():
                writer.add_scalar(f"train-loss/{n}", l, step_id)

            writer.add_scalar(f"lr/", args.lr, step_id)

            metrics = loss_and_metrics.compute_metrics(pred, sample)
            for n, l in metrics.items():
                writer.add_scalar(f"train-metrics/{n}", l, step_id)

            train_losses["total"].backward()  # type: ignore

            if step_id % args.accumulate_grad_batches == args.accumulate_grad_batches - 1:
                optimizer.step()

            if (step_id + 1) % args.val_freq == 0:
                if val_loaders is not None:
                    val_metrics = validation_step(
                        step_id,
                        val_loaders,
                        model,
                        writer,
                        loss_and_metrics,
                        args,
                    )
                    model.train()
                else:
                    val_metrics = {}
                checkpointer(val_metrics)
            # 显示 loss 的
            tbar.set_postfix(l=float(train_losses["total"]))
            
def get_log_dir(args: Arguments) -> Path:
    log_dir = args.xp / args.name
    task = list(args.tasks)[0].split("_")[0]
    version = int(os.environ.get("SLURM_JOBID", 0))
    while (log_dir / f"{task}_{args.mode}_version{version}").is_dir():
        version += 1
    return log_dir / f"{task}_{args.mode}_version{version}"


class CheckpointCallback:
    def __init__(
        self,
        name: str,
        log_dir: Path,
        state_dict: Any,
        minimizing: bool = True,
        checkpoint_period: int = 200,
    ):
        self._name = name
        self._minimizing = minimizing
        self._best = float("inf") if minimizing else -float("inf")
        self._log_dir = log_dir
        self._checkpoint_period = checkpoint_period
        self._step = 0
        self._state_dict = state_dict

    def __call__(self, metrics: Dict[str, torch.Tensor]):
        self._step += 1
        step = self._step * self._checkpoint_period

        value = int(metrics.get(self._name, 0))
        dest = self._log_dir / f"model.epoch={step}-value={value}.pth"
        torch.save(self._state_dict, dest)

        if (self._minimizing and self._best > value) or (
            not self._minimizing and self._best < value
        ):
            best = self._log_dir / "best.pth"
            # best.unlink(missing_ok=True)
            best.symlink_to(dest.resolve())
            self._best = value


@torch.no_grad()
def validation_step(
    step_id: int,
    val_loaders: DataLoader,
    model,
    writer,
    loss_and_metrics,
    args: Arguments,
    val_iters: int = 5,
):
    values = {}
    device = next(model.parameters()).device 
    model.eval()

    for val_id, sample in enumerate(val_loaders):
        if val_id == val_iters:
            break

        rgbs = sample["rgbs"].to(device)
        pcds = sample["pcds"].to(device)
        gripper = sample["gripper"].to(device)
        outputs = sample["action"].to(device)
        padding_mask = sample["padding_mask"].to(device)

        lang_feat = sample["language"].to(device) # B 75 512
        # lang_feat = get_language_feat(instr, "clip", args.num_words, device).float().to(device)  # B 75 512

        pred = model(
            rgbs,
            pcds,
            padding_mask,
            lang_feat,
            gripper,
        )

        losses: Dict[str, torch.Tensor] = loss_and_metrics.compute_loss(pred, sample)
        losses["total"] = torch.stack(list(losses.values())).sum()

        for n, l in losses.items():
            key = f"val-loss/{n}"
            writer.add_scalar(key, l, step_id + val_id)
            if key not in values:
                values[key] = torch.Tensor([]).to(device)
            values[key] = torch.cat([values[key], l.unsqueeze(0)])

        writer.add_scalar(f"lr/", args.lr, step_id + val_id)

        metrics = loss_and_metrics.compute_metrics(pred, sample)
        for n, l in metrics.items():
            key = f"val-metrics/{n}"
            writer.add_scalar(key, l, step_id + val_id)
            if key not in metrics:
                values[key] = torch.Tensor([]).to(device)
            values[key] = torch.cat([values[key], l.unsqueeze(0)])

    key = f"val-loss/total"
    print(f"Validation Loss {val_id}: {values[key].mean():.05f}")
    key = f"val-metrics/position"
    print(f"Validation Position {val_id}: {values[key].mean():.05f}")

    return values

def get_taskvar(tasks: Tuple[str, ...], root: Path, variations: Tuple[int, ...]):
    # 确定训练的任务
    if 'all' in list(tasks):
        train_tasks =  [
            'drop_pen_color', 'drop_pen_relative', 'drop_pen_size',
            'wipe_table_color', 'wipe_table_relative', 'wipe_table_shape', 'wipe_table_size', 'wipe_table_direction',
            'pour_demo_color', 'pour_demo_relative', 'pour_demo_size',
            'pick_cube_color', 'pick_cube_relative', 'pick_cube_shape', 'pick_cube_size',
            'stack_cubes_color', 'stack_cubes_size', 'stack_cubes_relative', 'stack_cubes_shape',
            'place_into_shape_sorter_color', 'place_into_shape_sorter_shape', 'place_into_shape_sorter_relative',
            'open_drawer',
            'open_door_complex'
            ]
    else:
        train_tasks = list(tasks)

    task_var = []

    for task in train_tasks:
        # path = root / task
        # variations = os.listdir(path)
        for var in variations:
            task_var.append((task, var))

    return task_var

def main(gpu, ngpus_per_node, args):

    # 首先处理分布式的问题
    # 这里的 gpu 如果是分布式，就是第几个进程（0，1，2，...），如果不是分布式，就是指定的 gpu
    args.gpu = args.gpu_list[gpu]  if args.distributed  else gpu
    if args.gpu is not None:
        print("Use GPU: {} for training".format(args.gpu))
    if args.distributed:
        if args.dist_url == "env://" and args.rank == -1:
                args.rank = int(os.environ["RANK"])
        
        args.rank = args.rank * ngpus_per_node + gpu
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                world_size=args.world_size, rank=args.rank)

    # 不同的进程的model参数初始化要相同，可以用同样的随机种子
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    random.seed(args.seed)

    args.train_dir = args.train_dir/args.mode
    # 确定训练的任务
    train_taskvar: List[Tuple[str, str]] = get_taskvar(args.tasks, args.train_dir, args.train_variations)
    # valid_taskvar: List[Tuple[str, str]] = get_taskvar(args.tasks, args.valid_dir, args.valid_variations)

    # 处理 checkpoint 存放的路径,只有主进程才生成 tensorboard 文件
    if args.rank == 0:
        log_dir = get_log_dir(args)
        log_dir.mkdir(exist_ok=True, parents=True)
        print("Logging:", log_dir)
        args.save(str(log_dir / "hparams.json"))
        args.save(str(log_dir / ".txt"))
        writer = SummaryWriter(log_dir=log_dir)
    else:
        writer = None

    # 获得最大的任务长度
    with open("episodes.json") as fid:
        episodes = json.load(fid)
    max_eps_dict = episodes["max_episode_length"]
    args.maxAction = max_eps_dict[args.tasks[0]]
    # 构建模型和优化器
    model = Hiveformer(
        depth=args.depth,
        dim_feedforward=args.dim_feedforward,
        hidden_dim=args.hidden_dim,
        instr_size=args.instr_size,
        mask_obs_prob=args.mask_obs_prob,
        max_episode_length=args.maxAction,
        num_words=args.num_words,
        num_layers=args.num_layers,
        num_tasks=args.num_tasks,
    )
    if args.distributed:
        if args.gpu is not None:
            torch.cuda.set_device(args.gpu)
            model.cuda(args.gpu)
            model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu], find_unused_parameters=True)
        else:
            model.cuda()
            model = torch.nn.parallel.DistributedDataParallel(model, find_unused_parameters=True)
    else:
        model.cuda(args.gpu)
    # 优化器
    # optimizer = torch.optim.Adam(model.parameters(),args.lr)
    optimizer_grouped_parameters = [
        {"params": [], "weight_decay": 0.0, "lr": args.lr},
        {"params": [], "weight_decay": 5e-4, "lr": args.lr},
    ]
    no_decay = ["bias", "LayerNorm.weight", "LayerNorm.bias"]
    for name, param in model.named_parameters():
        if any(nd in name for nd in no_decay):
            optimizer_grouped_parameters[0]["params"].append(param)  # type: ignore
        else:
            optimizer_grouped_parameters[1]["params"].append(param)  # type: ignore
    optimizer: optim.Optimizer = optim.AdamW(optimizer_grouped_parameters)
    
    model.train()
    # 加载模型
    if args.checkpoint is not None:
        print("Load from checkpoint ........")
        model_dict = torch.load(args.checkpoint, map_location="cpu")
        if args.distributed:
            model.module.load_state_dict(model_dict["weight"])
        else:
            model.load_state_dict(model_dict["weight"])
        optimizer.load_state_dict(model_dict["optimizer"])

    print(model)
    print("Number of parameters:")
    model_params = count_parameters(model)
    print("- model", model_params)
    print("Total", model_params)

    # 设置损失函数
    loss_and_metrics = LossAndMetrics(args)

    # 保存模型参数，以及构建需要的 checkpoint 实例
    model_dict = {
        "weight": model.module.state_dict() if args.distributed else model.state_dict(),
        "optimizer": optimizer.state_dict(),
    }

    if args.rank == 0:
        checkpointer = CheckpointCallback(
            "val-metrics/position",
            log_dir,
            model_dict,
            minimizing=False,
            checkpoint_period=args.checkpoint_period,
        )
    else:
        checkpointer = None

    # 构建训练集和 train_loader
    # train_dataset = My_Dataset(
    #     root=args.train_dir,
    #     taskvar=train_taskvar,
    #     max_episode_length=args.maxAction,
    #     cache_size=args.cache_size,
    #     training=True,
    # )

    train_dataset = RLBenchDataset(
        root=args.train_dir,
        taskvar=train_taskvar,
        max_episode_length=args.maxAction,
        max_episodes_per_taskvar=args.max_episodes_per_taskvar,
        cache_size=args.cache_size,
        cameras=args.cameras,  # type: ignore
    )

    if args.distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
    else:
        train_sampler = None

    train_loader = torch.utils.data.DataLoader(  
            train_dataset, 
            batch_size=args.batch_size, 
            shuffle=(train_sampler is None),
            num_workers=args.workers, 
            pin_memory=False, 
            sampler=train_sampler, 
            drop_last=True,
            persistent_workers=args.persistent_workers) #,persistent_workers=True
    
    # 构建验证集和 val_loader
    # val_dataset = RLBenchDataset(
    #     root=args.valid_dir,
    #     taskvar=valid_taskvar,
    #     max_episode_length=args.maxAction,
    #     max_episodes_per_taskvar=args.max_episodes_per_taskvar,
    #     cache_size=args.cache_size,
    #     cameras=args.cameras,  # type: ignore
    # )

    # if args.distributed:
    #     val_sampler = torch.utils.data.distributed.DistributedSampler(val_dataset)
    # else:
    #     val_sampler = None

    # val_loader = torch.utils.data.DataLoader(  
    #         val_dataset, 
    #         batch_size=args.batch_size, 
    #         shuffle=(val_sampler is None),
    #         num_workers=args.workers, 
    #         pin_memory=False, 
    #         sampler=val_sampler, 
    #         drop_last=True,
    #         persistent_workers=args.persistent_workers) #,persistent_workers=True

    val_loader = None
    # 开始训练
    training(
        model,
        optimizer,
        train_loader,
        train_sampler,
        val_loader,
        checkpointer,
        loss_and_metrics,
        args,
        writer,
    )

    if writer is not None:
        writer.close()

if __name__ == "__main__":
    args = Arguments().parse_args()
    print(args)

    #如果没有设置数量，就自动检测，得到总共该节点有几块 gpu 可用
    ngpus_per_node = torch.cuda.device_count() if len(args.gpu_list)==0 else len(args.gpu_list)
    args.ngpus_per_node = ngpus_per_node
    if args.distributed:
        args.world_size = ngpus_per_node * args.world_size
        mp.spawn(main, nprocs=ngpus_per_node, args=(ngpus_per_node, args))
    else:
        main(args.gpu, ngpus_per_node, args)