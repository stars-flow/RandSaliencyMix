import os
import argparse

import tqdm
import yaml
import torch
from torch import nn
from torch.backends import cudnn

from data import saliency_mix
from forge import *
from utils import AverageMeter, calc_error


def prepare_training(cfg):
    train_loader = create_train_loader(cfg['train_dataset'])
    val_loader = create_val_loader(cfg['val_dataset'])
    model = create_model(cfg['model'])
    model = nn.DataParallel(model)
    model = model.cuda()
    optim = create_optimizer(cfg['optimizer'], model)
    scheduler = create_scheduler(cfg['scheduler'], optim)
    loss_fn = nn.CrossEntropyLoss().cuda()
    return train_loader, val_loader, model, optim, scheduler, loss_fn


def train(loader, model, optim, loss_fn, cfg):
    model.train()
    loss_meter = AverageMeter()
    top1_meter = AverageMeter()
    top5_meter = AverageMeter()

    for i, (inp, tar) in enumerate(tqdm.tqdm(loader)):
        # Perform augmentation
        if 'augment' in cfg:
            aug_name = cfg['augment']['name']
            aug_args = cfg['augment']['args']
            if aug_name == 'SaliencyMix':
                inp, tar_a, tar_b, lam = saliency_mix(inp, tar, **aug_args)
                inp = inp.cuda()
                tar = tar.cuda()
                tar_a = tar_a.cuda()
                tar_b = tar_b.cuda()
                output = model.forward(inp)
                loss = loss_fn(output, tar_a) * lam + loss_fn(output, tar_b) * (1 - lam)

            else:
                raise NotImplementedError(f'Augmentation "{aug_name}" is not supported.')

        else:
            inp = inp.cuda()
            tar = tar.cuda()
            output = model.forward(inp)
            loss = loss_fn(output, tar)

        err1, err5 = calc_error(output, tar, topk=(1, 5))
        num_items = inp.shape[0]
        loss_meter.update(loss.item(), num_items)
        top1_meter.update(err1, num_items)
        top5_meter.update(err5, num_items)

        optim.zero_grad()
        loss.backward()
        optim.step()

    return loss_meter.avg, top1_meter.avg, top5_meter.avg


def validate(val_loader, model, loss_fn):
    model.eval()
    loss_meter = AverageMeter()
    top1_meter = AverageMeter()
    top5_meter = AverageMeter()
    for i, (inp, tar) in enumerate(tqdm.tqdm(loader)):
        tar = tar.cuda()

        output = model.forward(inp)
        loss = loss_fn(output, tar)

        err1, err5 = calc_error(output, tar, topk=(1, 5))
        num_items = inp.shape[0]
        loss_meter.update(loss.item(), num_items)
        top1_meter.update(err1, num_items)
        top5_meter.update(err5, num_items)

    return loss_meter.avg, top1_meter.avg, top5_meter.avg


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config')
    parser.add_argument('--gpu')
    args = parser.parse_args()

    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
    cudnn.benchmark = True

    with open(args.config, 'r') as f:
        cfg = yaml.load(f, Loader=yaml.FullLoader)
        if cfg is not None:
            print('Loaded config.')
        else:
            raise IOError('Failed to load config.')
    train_loader, val_loader, model, optim, scheduler, loss_fn = prepare_training(cfg)

    for epoch in range(cfg['epochs']):
        train(train_loader, model, optim, loss_fn, cfg)
        validate(val_loader, model, loss_fn)
        scheduler.step()


if __name__ == '__main__':
    main()
