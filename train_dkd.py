import torch, timm
import torchvision
from torch import nn
from torch.utils.data import DataLoader
import torch.nn.parallel
import torch.utils.data.distributed
import torch.multiprocessing as mp
import torch.backends.cudnn as cudnn
import torch.distributed as dist
from torch.optim.lr_scheduler import ReduceLROnPlateau

import Models
from models_distill.embtrans import EmbTrans
# from Dataset import ImageNet
from utils_dkd import colorstr, Save_Checkpoint, DirectNormLoss, DKDLoss

import numpy as np
from pathlib import Path
import os
import time
import json
import random
import argparse
import warnings
from torch.utils.tensorboard import SummaryWriter

########------------lightweight-----------------#######
from lightweight.batch_engine import valid_trainer, batch_trainer
from lightweight.config import argument_parser
from lightweight.dataset.Dataset import Data166,get_transform
from lightweight.loss.CE_loss import CEL_Sigmoid
from lightweight.loss.AsymmetricLoss import AsymmetricLossOptimized
from lightweight.models.base_block import FeatClassifier_LW, BaseClassifier_LW
from lightweight.models.resnet import resnet50, resnet101, resnext50_32x4d,resnet152,resnet18,resnet34
from lightweight.models.senet import se_resnet50, se_resnet101,se_resnext50_32x4d,se_resnet152
from lightweight.models.efficientnet import efficientnet_b0, efficientnet_b6,efficientnet_b2,efficientnet_b4
from lightweight.tools.function import get_model_log_path, get_pedestrian_metrics
from lightweight.tools.utils import time_str, save_ckpt, ReDirectSTD, set_seed

######---------------teacher--------------#################
from dataset.face_attr.face import FaceAttr, FaceAttrHackathon, FaceAttrHackathonText
from models.base_block import FeatClassifier
from models.model_factory import build_loss, build_classifier, build_backbone
from configs import cfg, update_config
from tools.function import get_model_log_path, get_reload_weight, seperate_weight_decay

def main(args, teacher, T_EMB):
    args.distributed = args.world_size > 1 or args.multiprocessing_distributed

    if torch.cuda.is_available():
        ngpus_per_node = torch.cuda.device_count()

    if args.multiprocessing_distributed:
        # Since we have ngpus_per_node processes per node, the total world_size
        # needs to be adjusted accordingly
        args.world_size = ngpus_per_node * args.world_size
        
        print(colorstr('green', "Multiprocess distributed training, gpus:{}, total batch size:{}, epoch:{}, lr:{}".format(ngpus_per_node, args.batch_size, args.epochs, args.lr)))
    
        mp.spawn(main_worker, nprocs=ngpus_per_node, args=(ngpus_per_node, args, teacher, T_EMB))
    else:
        # Simply call main_worker function
        main_worker(args.gpu, ngpus_per_node, args, teacher)


def main_worker(gpu, ngpus_per_node, args, teacher, T_EMB):
    args.gpu = gpu
    if args.gpu is not None:
        print("Use GPU: {} for training".format(args.gpu))
    
    if args.distributed and args.multiprocessing_distributed:
        # For multiprocessing distributed training, rank needs to be the
        # global rank among all the processes
        args.rank = args.rank * ngpus_per_node + gpu
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                world_size=args.world_size, rank=args.rank)
    
    if args.rank % ngpus_per_node == 0:
        # weights
        save_dir = Path(args.save_dir)
        weights = save_dir / 'weights'
        weights.mkdir(parents=True, exist_ok=True)
        last = weights / 'last'
        best = weights / 'best'

        # acc,loss
        acc_loss = save_dir / 'acc_loss'
        acc_loss.mkdir(parents=True, exist_ok=True)
        train_acc_top1_savepath = acc_loss / 'train_acc_top1.npy'
        train_acc_top5_savepath = acc_loss / 'train_acc_top5.npy'
        train_loss_savepath = acc_loss / 'train_loss.npy'
        val_acc_top1_savepath = acc_loss / 'val_acc_top1.npy'
        val_acc_top5_savepath = acc_loss / 'val_acc_top5.npy'
        val_loss_savepath = acc_loss / 'val_loss.npy'

        # tensorboard
        logdir = save_dir / 'logs'
        logdir.mkdir(parents=True, exist_ok=True)
        summary_writer = SummaryWriter(logdir, flush_secs=120)
    
    # dataset
    # train_dataset, val_dataset, num_class = ImageNet()
    train_tsfm, valid_tsfm = get_transform(args)
    train_dataset = Data166(split='train',root=args.root_path,data_path=args.train_path,transform=train_tsfm)
    val_dataset = Data166(split='val',root=args.root_path,data_path=args.valid_path,transform=valid_tsfm)
    num_class = train_dataset.attr_num
    
    ################# model student
    # model = Models.__dict__[args.model_name]()
    
    if args.model_name == ' efficientnetb0':
        backbone = efficientnet_b0()
    # backbone = resnet18()
    # backbone = efficientnet_b2()
    classifier = BaseClassifier_LW(nattr=train_dataset.attr_num)
    model = FeatClassifier_LW(backbone, classifier)


    if args.model_name in ['mobilenetv1']:
        model = EmbTrans(student=model, model_name=args.model_name)
        
    elif args.teacher in ['resnet50', 'resnet101', 'resnet152']:
        model = EmbTrans(student=model, model_name=args.teacher)

    if args.distributed:
        # For multiprocessing distributed, DistributedDataParallel constructor
        # should always set the single device scope, otherwise,
        # DistributedDataParallel will use all available devices.
        if torch.cuda.is_available():
            if args.gpu is not None:
                torch.cuda.set_device(args.gpu)
                model.cuda(args.gpu)
                # When using a single GPU per process and per
                # DistributedDataParallel, we need to divide the batch size
                # ourselves based on the total number of GPUs of the current node.
                args.batch_size = int(args.batch_size / ngpus_per_node)
                args.workers = int((args.workers + ngpus_per_node - 1) / ngpus_per_node)
                model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu]) # , find_unused_parameters=True
                # teacher
                teacher.cuda(args.gpu)

    else:
        model = torch.nn.DataParallel(model).cuda()
        teacher = torch.nn.DataParallel(teacher).cuda()
        if args.rank % ngpus_per_node == 0:
            print(colorstr('green', 'use DataParallel mode training'))
    
    if torch.cuda.is_available():
        if args.gpu:
            device = torch.device('cuda:{}'.format(args.gpu))
        else:
            device = torch.device("cuda")
    # loss
    criterion = nn.CrossEntropyLoss().to(device)
    dkd_loss = DKDLoss(alpha=args.dkd_alpha, beta=args.dkd_beta, T=args.t).to(device)
    nd_loss = DirectNormLoss(num_class=num_class, nd_loss_factor=args.nd_loss_factor).to(device)
    optimizer = torch.optim.SGD(params=model.parameters(), 
                                lr=args.lr, 
                                momentum=args.momentum, 
                                nesterov=True, 
                                weight_decay=args.weight_decay)
    
    if args.resume:
        if args.gpu is None:
            checkpoint = torch.load(args.resume)
        elif torch.cuda.is_available():
            # Map model to be loaded to specified single gpu.
            loc = 'cuda:{}'.format(args.gpu)
            checkpoint = torch.load(args.resume, map_location=loc)
           
        start_epoch = checkpoint['epoch']
        model.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        best_acc = torch.tensor(checkpoint['best_acc'])
        if args.gpu is not None:
            # best_acc may be from a checkpoint from a different GPU
            best_acc = best_acc.to(args.gpu)

        train_acc_top1 = checkpoint['train_acc_top1']
        train_acc_top5 = checkpoint['train_acc_top5']
        train_loss = checkpoint['train_loss']
        test_acc_top1 = checkpoint['test_acc_top1']
        test_acc_top5 = checkpoint['test_acc_top5']
        test_loss = checkpoint['test_loss']
        if args.rank % ngpus_per_node == 0:
            print(colorstr('green', 'Resuming training from {} epoch'.format(start_epoch)))
    else:
        start_epoch = 0
        best_acc = 0
        train_acc_top1 = []
        train_acc_top5 = []
        train_loss = []
        test_acc_top1 = []
        test_acc_top5 = []
        test_loss = []

    if args.distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
        val_sampler = torch.utils.data.distributed.DistributedSampler(val_dataset, shuffle=False)
    else:
        train_sampler = None
        val_sampler = None
    
    train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=(train_sampler is None),
                              num_workers=args.workers, pin_memory=True, sampler=train_sampler)
    
    val_loader = DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False,
                            num_workers=args.workers, pin_memory=True, sampler=val_sampler)
    
    if args.evaluate:
        val_epoch_loss, val_acc1, val_acc5 = validate(model=model,
                                                      val_loader=val_loader,
                                                      criterion=criterion,
                                                      args=args)
        if args.rank % ngpus_per_node == 0:
            print("Test Loss: {}, Test Acc Top1: {}, Test Acc Top5: {}".format(val_epoch_loss, val_acc1, val_acc5))
        return
    
    for epoch in range(start_epoch, args.epochs):
        # lr scheduler
        if epoch in [30, 60, 90]:
            for param_group in optimizer.param_groups:
                param_group['lr'] *= 0.1
        if args.rank % ngpus_per_node == 0:
            print("Epoch {}/{}".format(epoch + 1, args.epochs))
        if args.distributed:
            train_sampler.set_epoch(epoch)
        
        norm_dir_loss, div_loss, cls_loss, train_epoch_loss, train_acc1, train_acc5 = train(model=model,
                                                                                                     teacher=teacher,
                                                                                                     T_EMB=T_EMB,
                                                                                                     train_loader=train_loader, 
                                                                                                     optimizer=optimizer, 
                                                                                                     criterion=criterion, 
                                                                                                     kd_loss=dkd_loss,
                                                                                                     nd_loss=nd_loss,
                                                                                                     ngpus_per_node=ngpus_per_node, 
                                                                                                     args=args,
                                                                                                     epoch=epoch)
        
        val_epoch_loss, val_acc1, val_acc5 = validate(model=model,
                                                      val_loader=val_loader,
                                                      criterion=criterion,
                                                      args=args)
        
        s = "Train Loss: {:.3f}, Train Acc Top1: {:.3f}, Train Acc Top5: {:.3f}, Test Loss: {:.3f}, Test Acc Top1: {:.3f}, Test Acc Top5: {:.3f}, lr: {:.5f}".format(
            train_epoch_loss, train_acc1, train_acc5, val_epoch_loss, val_acc1, val_acc5, optimizer.param_groups[0]['lr'])
        if args.rank % ngpus_per_node == 0:
            print(colorstr('green', s))

            # acc,loss
            train_loss.append(train_epoch_loss)
            train_acc_top1.append(train_acc1)
            train_acc_top5.append(train_acc5)
            test_loss.append(val_epoch_loss)
            test_acc_top1.append(val_acc1)
            test_acc_top5.append(val_acc5)

            # save model
            is_best = val_acc1 > best_acc
            best_acc = max(best_acc, val_acc1)
            state = {
                'epoch': epoch + 1,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'best_acc': best_acc,
                'train_acc_top1': train_acc_top1,
                'train_acc_top5': train_acc_top5,
                'train_loss': train_loss,
                'test_acc_top1': test_acc_top1,
                'test_acc_top5': test_acc_top5,
                'test_loss': test_loss,
            }

            last_path = last / 'epoch_{}_loss_{:.3f}_acc_{:.3f}'.format(
                epoch + 1, val_epoch_loss, val_acc1)
            best_path = best / 'epoch_{}_acc_{:.3f}'.format(
                epoch + 1, best_acc)
            Save_Checkpoint(state, last, last_path, best, best_path, is_best)

            if epoch == 1:
                images, labels = next(iter(train_loader))
                img_grid = torchvision.utils.make_grid(images)
                summary_writer.add_image('ImageNet Image', img_grid)

            summary_writer.add_scalar('lr', optimizer.param_groups[0]['lr'], epoch)

            summary_writer.add_scalar('train_loss', train_epoch_loss, epoch)
            summary_writer.add_scalar('train_acc_top1', train_acc1, epoch)
            summary_writer.add_scalar('train_acc_top5', train_acc5, epoch)
            summary_writer.add_scalar('val_loss', val_epoch_loss, epoch)
            summary_writer.add_scalar('val_acc_top1', val_acc1, epoch)
            summary_writer.add_scalar('val_acc_top5', val_acc5, epoch)

            summary_writer.add_scalar('nd_loss', norm_dir_loss, epoch)
            summary_writer.add_scalar('div_loss', div_loss, epoch)
            summary_writer.add_scalar('cls_loss', cls_loss, epoch)
    
    if args.rank % ngpus_per_node == 0:
        summary_writer.close()
        if not os.path.exists(train_acc_top1_savepath) or not os.path.exists(train_loss_savepath):
            np.save(train_acc_top1_savepath, train_acc_top1)
            np.save(train_acc_top5_savepath, train_acc_top5)
            np.save(train_loss_savepath, train_loss)
            np.save(val_acc_top1_savepath, test_acc_top1)
            np.save(val_acc_top5_savepath, test_acc_top5)
            np.save(val_loss_savepath, test_loss)


def train(model, teacher, T_EMB, train_loader, optimizer, criterion, kd_loss, nd_loss, ngpus_per_node, args, epoch):
    train_loss = AverageMeter()
    train_acc1 = AverageMeter()
    train_acc5 = AverageMeter()

    Cls_loss = AverageMeter()
    Div_loss = AverageMeter()
    Norm_loss = AverageMeter()

    # Model on train mode
    model.train()
    teacher.eval()
    step_per_epoch = len(train_loader)
    for step, (images, labels) in enumerate(train_loader):
        start = time.time()
        if args.gpu is not None and torch.cuda.is_available():
            images = images.cuda(args.gpu, non_blocking=True)
            labels = labels.cuda(args.gpu, non_blocking=True)
        
        # compute output
        s_emb, s_logits = model(images, embed=True)

        with torch.no_grad():
            t_emb, t_logits = teacher(images, embed=True)
        
        # cls loss
        cls_loss = criterion(s_logits, labels) * args.cls_loss_factor
        # KL loss
        div_loss = kd_loss(s_logits, t_logits, labels) * min(1.0, epoch/args.warm_up)
        # nd loss
        norm_loss = nd_loss(s_emb=s_emb, t_emb=t_emb, T_EMB=T_EMB, labels=labels) * min(1.0, epoch/args.warm_up)

        loss = cls_loss + div_loss + norm_loss

        # measure accuracy and record loss
        acc1, acc5 = accuracy(s_logits, labels, topk=(1, 5))

        train_loss.update(loss.item(), images.size(0))
        train_acc1.update(acc1[0].item(), images.size(0))
        train_acc5.update(acc5[0].item(), images.size(0))

        Cls_loss.update(cls_loss.item(), images.size(0))
        Div_loss.update(div_loss.item(), images.size(0))
        Norm_loss.update(norm_loss.item(), images.size(0))

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        t = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))
        s1 = '\r{} [{}/{}]'.format(t, step+1, step_per_epoch)
        s2 = ' - {:.2f}ms/step - direct_loss: {:.3f} - norm_loss: {:.3f} - div_loss: {:.3f} - cls_loss: {:.3f} - train_loss: {:.3f} - train_acc_top1: {:.3f} - train_acc_top5: {:.3f}'.format(
             1000 * (time.time()-start), norm_loss.item(), div_loss.item(), cls_loss.item(), train_loss.val, train_acc1.val, train_acc5.val)
        if args.rank % ngpus_per_node == 0:
            print(s1+s2, end='', flush=True)
    
    if args.rank % ngpus_per_node == 0:
        print()
    return Norm_loss.avg, Div_loss.avg, Cls_loss.avg, train_loss.avg, train_acc1.avg, train_acc5.avg


def validate(model, val_loader, criterion, args):
    val_loss = AverageMeter()
    val_acc1 = AverageMeter()
    val_acc5 = AverageMeter()
    
    # model to evaluate mode
    model.eval()
    with torch.no_grad():
        for step, (images, labels) in enumerate(val_loader):
            if args.gpu is not None and torch.cuda.is_available():
                 images = images.cuda(args.gpu, non_blocking=True)
                 labels = labels.cuda(args.gpu, non_blocking=True)

            # compute logits
            logits = model(images, embed=False)

            loss = criterion(logits, labels)

            # measure accuracy and record loss
            acc1, acc5 = accuracy(logits, labels, topk=(1, 5))

            # Average loss and accuracy across processes
            if args.distributed:
                loss = reduce_tensor(loss, args)
                acc1 = reduce_tensor(acc1, args)
                acc5 = reduce_tensor(acc5, args)

            val_loss.update(loss.item(), images.size(0))
            val_acc1.update(acc1[0].item(), images.size(0))
            val_acc5.update(acc5[0].item(), images.size(0))
    
    return val_loss.avg, val_acc1.avg, val_acc5.avg


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def reduce_tensor(tensor, args):
    rt = tensor.clone()
    dist.all_reduce(rt, op=dist.ReduceOp.SUM)
    rt /= args.world_size
    return rt


def accuracy(output, target, topk=(1,)):
    """Computes the accuracy over the k top predictions for the specified values of k"""
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)

        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        for k in topk:
            correct_k = correct[:k].reshape(-1).float().sum(0, keepdim=True)
            # res.append(correct_k.mul_(100.0 / batch_size))
            res.append(correct_k.mul_(1.0 / batch_size))
        return res


if __name__ == "__main__":
    # model_names = sorted(name for name in Models.__dict__ 
    #                      if name.islower() and not name.startswith("__") 
    #                      and callable(Models.__dict__[name]))

    parser = argparse.ArgumentParser(description='PyTorch ImageNet Training with Distributed Data Parallel.')
    parser.add_argument("--model_name", type=str, default="efficientnetb0", help="model architecture")
    parser.add_argument("--dataset", type=str, default='imagenet')
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--batch_size", type=int, default=64, help="total batch size")
    parser.add_argument('--evaluate', action='store_true', help='evaluate model on validation set')
    parser.add_argument('--workers', default=4, type=int, help='number of data loading workers')
    parser.add_argument("--lr", type=float, default=0.1)
    parser.add_argument('--momentum', default=0.9, type=float, help='momentum')
    parser.add_argument("--weight_decay", type=float, default=1e-4)

    parser.add_argument("--teacher", type=str, default="swin_b", help="teacher architecture")
    parser.add_argument("--teacher_weights", type=str, default="", help="teacher weights path")
    parser.add_argument("--cls_loss_factor", type=float, default=1.0, help="cls loss weight factor")
    parser.add_argument("--dkd_alpha", type=float, default=0.5, help="dkd loss weight factor, alpha")
    parser.add_argument("--dkd_beta", type=float, default=0.5, help="dkd loss weight factor, beta")
    parser.add_argument("--t", type=float, default=1.0, help="temperature")
    parser.add_argument("--nd_loss_factor", type=float, default=1.0, help="ND loss weight factor")
    parser.add_argument("--warm_up", type=float, default=5.0, help='loss weight warm up epochs')

    parser.add_argument('--world_size', default=1, type=int, help='number of nodes for distributed training')
    parser.add_argument('--rank', default=0, type=int, help='node rank for distributed training')
    parser.add_argument('--dist_url', default='tcp://localhost:10000', type=str, help='url used to set up distributed training')
    parser.add_argument('--dist_backend', default='nccl', type=str, help='distributed backend')
    parser.add_argument('--gpu', default=None, type=int, help='GPU id to use.')
    parser.add_argument('--seed', default=None, type=int, help='seed for initializing training.')
    parser.add_argument('--multiprocessing-distributed', action='store_true', 
                        help='Use multi-processing distributed training to launch '
                             'N processes per node, which has N GPUs. This is the '
                             'fastest way to use PyTorch for either single node or '
                             'multi node data parallel training')

    parser.add_argument("--resume", type=str, help="ckpt's path to resume most recent training")
    parser.add_argument("--save_dir", type=str, default="./run/DKD++/Swin_b-B0", help="save path, eg, acc_loss, weights, tensorboard, and so on")
    
    parser.add_argument('--root_path', default='/data/AI/backup/phuongdd/Computer-Vison/attributes/images', type=str, help='Root of folder images')
    parser.add_argument('--train_path', default='/data/AI/backup/phuongdd/Computer-Vison/attributes/train.csv', type=str, help='Root of train csv' )
    parser.add_argument('--valid_path', default='/data/AI/backup/phuongdd/Computer-Vison/attributes/valid.csv', type=str, help='Root of valid csv')
    parser.add_argument("--height", type=int, default=256)
    parser.add_argument("--width", type=int, default=128)
    parser.add_argument(
        "--cfg", help="decide which cfg to use", type=str,
        default="/data/AI/backup/phuongdd/Computer-Vison/attributes/ELPT-AI-Hackathon-Face-Analysis/configs/face.yaml",

    )
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True
        cudnn.benchmark = False
        warnings.warn('You have chosen to seed training. '
                      'This will turn on the CUDNN deterministic setting, '
                      'which can slow down your training considerably! '
                      'You may see unexpected behavior when restarting '
                      'from checkpoints.')
    
    if args.gpu is not None:
        warnings.warn('You have chosen a specific GPU. This will completely '
                      'disable data parallelism.')
    
    # train_data, test_data, num_class = ImageNet()
    train_tsfm, valid_tsfm = get_transform(args)
    train_dataset = Data166(split='train',root=args.root_path,data_path=args.train_path,transform=train_tsfm)
    val_dataset = Data166(split='val',root=args.root_path,data_path=args.valid_path,transform=valid_tsfm)
    num_class = train_dataset.attr_num
    
    # if args.teacher == 'vits':
    #     teacher = timm.create_model('vit_small_patch16_224', pretrained=True, num_classes=1000)
    # elif args.teacher == 'vitb':
    #     teacher = timm.create_model('vit_base_patch16_224', pretrained=True, num_classes=1000)
    
    update_config(cfg, args)
    backbone, c_output = build_backbone(cfg.BACKBONE.TYPE, cfg.BACKBONE.MULTISCALE)

    classifier = build_classifier(cfg.CLASSIFIER.NAME)(
        nattr=train_dataset.attr_num,
        c_in=1024 * 2,
        bn=cfg.CLASSIFIER.BN,
        pool=cfg.CLASSIFIER.POOLING,
        scale =cfg.CLASSIFIER.SCALE
    )

    teacher = FeatClassifier(backbone, classifier, bn_wd=cfg.TRAIN.BN_WD)
    teacher = torch.nn.DataParallel(teacher)
    

    with open("/data/AI/backup/phuongdd/Computer-Vison/attributes/ELPT-AI-Hackathon-Face-Analysis/center_emb_train_nopretrained.json", 'r') as f:
        T_EMB = json.load(f)
    f.close()

    print(colorstr('green', 'Use ' + args.teacher + ' Training ' + args.model_name + ' ...'))
    main(args=args, teacher=teacher, T_EMB=T_EMB)