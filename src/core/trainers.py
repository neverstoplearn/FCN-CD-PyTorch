import shutil
import os
from types import MappingProxyType
from copy import deepcopy

import torch
from skimage import io
from tqdm import tqdm

import constants
from data.common import to_array
from utils.misc import R
from utils.metrics import AverageMeter
from utils.utils import mod_crop
from .factories import (model_factory, optim_factory, critn_factory, data_factory, metric_factory)


class Trainer:
    def __init__(self, model, dataset, criterion, optimizer, settings):
        super().__init__()
        context = deepcopy(settings)
        self.ctx = MappingProxyType(vars(context))
        self.mode = ('train', 'val').index(context.cmd)

        self.logger = R['LOGGER']
        self.gpc = R['GPC']     # Global Path Controller
        self.path = self.gpc.get_path

        self.batch_size = context.batch_size
        self.checkpoint = context.resume
        self.load_checkpoint = (len(self.checkpoint)>0)
        self.num_epochs = context.num_epochs
        self.lr = float(context.lr)
        self.save = context.save_on or context.out_dir
        self.out_dir = context.out_dir
        self.track_intvl = int(context.track_intvl)
        self.device = torch.device(context.device)
        self.suffix_off = context.suffix_off

        for k, v in sorted(self.ctx.items()):
            self.logger.show("{}: {}".format(k,v))

        self.model = model_factory(model, context)
        self.model.to(self.device)
        self.criterion = critn_factory(criterion, context)
        self.criterion.to(self.device)
        self.metrics = metric_factory(context.metrics, context)

        if self.is_training:
            self.train_loader = data_factory(dataset, 'train', context)
            self.val_loader = data_factory(dataset, 'val', context)
            self.optimizer = optim_factory(optimizer, self.model, context)
        else:
            self.val_loader = data_factory(dataset, 'val', context)
        
        self.start_epoch = 0
        self._init_max_acc_and_epoch = (0.0, 0)

    @property
    def is_training(self):
        return self.mode == 0

    def train_epoch(self, epoch):
        raise NotImplementedError

    def validate_epoch(self, epoch=0, store=False):
        raise NotImplementedError

    def _write_prompt(self):
        self.logger.dump(input("\nWrite some notes: "))

    def run(self):
        if self.is_training:
            self._write_prompt()
            self.train()
        else:
            self.evaluate()

    def train(self):
        if self.load_checkpoint:
            self._resume_from_checkpoint()

        max_acc, best_epoch = self._init_max_acc_and_epoch

        for epoch in range(self.start_epoch, self.num_epochs):
            lr = self._adjust_learning_rate(epoch)

            self.logger.show_nl("Epoch: [{0}]\tlr {1:.06f}".format(epoch, lr))

            # Train for one epoch
            self.train_epoch(epoch)
            
            # Clear the history of metric objects
            for m in self.metrics:
                m.reset()
                
            # Evaluate the model on validation set
            self.logger.show_nl("Validate")
            acc = self.validate_epoch(epoch=epoch, store=self.save)
            
            is_best = acc > max_acc
            if is_best:
                max_acc = acc
                best_epoch = epoch
            self.logger.show_nl("Current: {:.6f} ({:03d})\tBest: {:.6f} ({:03d})\t".format(
                                acc, epoch, max_acc, best_epoch))

            # The checkpoint saves next epoch
            self._save_checkpoint(
                self.model.state_dict(), 
                self.optimizer.state_dict() if self.ctx['save_optim'] else {}, 
                (max_acc, best_epoch), epoch+1, is_best
            )
        
    def evaluate(self):
        if self.checkpoint: 
            if self._resume_from_checkpoint():
                self.validate_epoch(self.ckp_epoch, self.save)
        else:
            self.logger.warning("Warning: no checkpoint assigned!")

    def _adjust_learning_rate(self, epoch):
        if self.ctx['lr_mode'] == 'step':
            lr = self.lr * (0.5 ** (epoch // self.ctx['step']))
        elif self.ctx['lr_mode'] == 'poly':
            lr = self.lr * (1 - epoch / self.num_epochs) ** 1.1
        elif self.ctx['lr_mode'] == 'const':
            lr = self.lr
        else:
            raise ValueError('unknown lr mode {}'.format(self.ctx['lr_mode']))

        for param_group in self.optimizer.param_groups:
            param_group['lr'] = lr
        return lr

    def _resume_from_checkpoint(self):
        ## XXX: This could be slow!
        if not os.path.isfile(self.checkpoint):
            self.logger.error("=> No checkpoint was found at '{}'.".format(self.checkpoint))
            return False

        self.logger.show("=> Loading checkpoint '{}'".format(
                        self.checkpoint))
        checkpoint = torch.load(self.checkpoint, map_location=self.device)

        state_dict = self.model.state_dict()
        ckp_dict = checkpoint.get('state_dict', checkpoint)
        update_dict = {k:v for k,v in ckp_dict.items() 
            if k in state_dict and state_dict[k].shape == v.shape}
        
        num_to_update = len(update_dict)
        if (num_to_update < len(state_dict)) or (len(state_dict) < len(ckp_dict)):
            if not self.is_training and (num_to_update < len(state_dict)):
                self.logger.error("=> Mismatched checkpoint for evaluation")
                return False
            self.logger.warning("Warning: trying to load an mismatched checkpoint.")
            if num_to_update == 0:
                self.logger.error("=> No parameter is to be loaded.")
                return False
            else:
                self.logger.warning("=> {} params are to be loaded.".format(num_to_update))
        elif (not self.ctx['anew']) or not self.is_training:
            self.start_epoch = checkpoint.get('epoch', 0)
            max_acc_and_epoch = checkpoint.get('max_acc', (0.0, self.ckp_epoch))
            # For backward compatibility
            if isinstance(max_acc_and_epoch, (float, int)):
                self._init_max_acc_and_epoch = (max_acc_and_epoch, self.ckp_epoch)
            else:
                self._init_max_acc_and_epoch = max_acc_and_epoch
            if self.ctx['load_optim'] and self.is_training:
                # Note that weight decay might be modified here
                self.optimizer.load_state_dict(checkpoint['optimizer'])

        state_dict.update(update_dict)
        self.model.load_state_dict(state_dict)

        self.logger.show("=> Loaded checkpoint '{}' (epoch {}, max_acc {:.4f} at epoch {})".format(
            self.checkpoint, self.ckp_epoch, *self._init_max_acc_and_epoch
            ))
        return True
        
    def _save_checkpoint(self, state_dict, optim_state, max_acc, epoch, is_best):
        state = {
            'epoch': epoch,
            'state_dict': state_dict,
            'optimizer': optim_state, 
            'max_acc': max_acc
        } 
        # Save history
        history_path = self.path('weight', constants.CKP_COUNTED.format(e=epoch), underline=True)
        if epoch % self.track_intvl == 0:
            torch.save(state, history_path)
        # Save latest
        latest_path = self.path(
            'weight', constants.CKP_LATEST, 
            underline=True
        )
        torch.save(state, latest_path)
        if is_best:
            shutil.copyfile(
                latest_path, self.path(
                    'weight', constants.CKP_BEST, 
                    underline=True
                )
            )
    
    @property
    def ckp_epoch(self):
        # Get current epoch of the checkpoint
        # For dismatched ckp or no ckp, set to 0
        return max(self.start_epoch-1, 0)

    def save_image(self, file_name, image, epoch):
        file_path = os.path.join(
            'epoch_{}/'.format(epoch),
            self.out_dir,
            file_name
        )
        out_path = self.path(
            'out', file_path,
            suffix=not self.suffix_off,
            auto_make=True,
            underline=True
        )
        return io.imsave(out_path, image)


class CDTrainer(Trainer):
    def __init__(self, arch, dataset, optimizer, settings):
        super().__init__(arch, dataset, 'NLL', optimizer, settings)

    def train_epoch(self, epoch):
        losses = AverageMeter()
        len_train = len(self.train_loader)
        pb = tqdm(self.train_loader)
        
        self.model.train()

        for i, (t1, t2, label) in enumerate(pb):
            t1, t2, label = t1.to(self.device), t2.to(self.device), label.to(self.device)
            
            prob = self.model(t1, t2)
            
            loss = self.criterion(prob, label)
            
            losses.update(loss.item(), n=self.batch_size)

            # Compute gradients and do SGD step
            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()

            desc = self.logger.make_desc(
                i+1, len_train,
                ('loss', losses, '.4f')
            )

            pb.set_description(desc)
            self.logger.dump(desc)

    def validate_epoch(self, epoch=0, store=False):
        self.logger.show_nl("Epoch: [{0}]".format(epoch))
        losses = AverageMeter()
        len_val = len(self.val_loader)
        pb = tqdm(self.val_loader)

        self.model.eval()

        with torch.no_grad():
            for i, (name, t1, t2, label) in enumerate(pb):
                if self.is_training and i >= 16: 
                    # Do not validate all images on training phase
                    pb.close()
                    self.logger.warning("validation ends early")
                    break
                t1, t2, label = t1.to(self.device), t2.to(self.device), label.to(self.device)

                prob = self.model(t1, t2)

                loss = self.criterion(prob, label)
                losses.update(loss.item(), n=self.batch_size)

                # Convert to numpy arrays
                CM = to_array(torch.argmax(prob[0], 0)).astype('uint8')
                label = to_array(label[0]).astype('uint8')
                for m in self.metrics:
                    m.update(CM, label)

                desc = self.logger.make_desc(
                    i+1, len_val,
                    ('loss', losses, '.4f'),
                    *(
                        (m.__name__, m, '.4f')
                        for m in self.metrics
                    )
                )
                pb.set_description(desc)
                self.logger.dump(desc)
                    
                if store:
                    self.save_image(name[0], CM*255, epoch)

        return self.metrics[0].avg if len(self.metrics) > 0 else max(1.0 - losses.avg, self._init_max_acc)