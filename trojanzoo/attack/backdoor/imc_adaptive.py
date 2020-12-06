# -*- coding: utf-8 -*-

from .imc import IMC
from trojanzoo.model.image import MagNet
from trojanzoo.utils import to_tensor
from trojanzoo.utils.model import AverageMeter, total_variation

import torch
import numpy as np
import os
import math
import random

from typing import Dict, Tuple, List


from trojanzoo.utils.config import Config
env = Config.env


class IMC_Adaptive(IMC):
    name: str = 'imc_adaptive'

    def __init__(self, seed_num: int = -5, count_mask: bool = True,
                 samp_k: int = 1, same_range: bool = False, n_samples: int = 5,
                 strip_percent: float = 1.0, abs_weight: float = 1e-5, **kwargs):
        super().__init__(**kwargs)
        data_shape = [self.dataset.n_channel]
        data_shape.extend(self.dataset.n_dim)
        self.data_shape: List[int] = data_shape

        self.seed_num: int = seed_num
        if self.seed_num < 0:
            self.seed_num = self.model.num_classes * abs(self.seed_num)
        self.count_mask: bool = count_mask

        # -----------Neural Sampling------------- #
        self.samp_k: int = samp_k
        self.same_range: bool = same_range
        self.n_samples: int = n_samples
        self.top_n_neurons: int = 20

        self.seed_data = self.load_seed_data()
        self.neuron_list = []

        self.strip_percent: float = strip_percent
        self.abs_weight: float = abs_weight

    def attack(self, epoch: int, **kwargs):
        super().attack(epoch, loss_fn=self.loss_fn, **kwargs)

    def epoch_func(self, **kwargs):
        self.save()
        _input, _label = self.seed_data['input'], self.seed_data['label']
        all_ps = self.sample_neuron(_input)
        self.neuron_list: List[Dict] = self.find_min_max(all_ps, _label)[0]
        self.optimize_mark()

    # ---------------------------- Seed Data --------------------------- #

    def save_seed_data(self) -> Dict[str, np.ndarray]:
        torch.manual_seed(env['seed'])
        if self.seed_num % self.model.num_classes:
            raise ValueError(f'seed_num({self.seed_num:d}) % num_classes({self.model.num_classes:d}) should be 0.')
        seed_class_num: int = self.seed_num // self.model.num_classes
        x, y = [], []
        for _class in range(self.model.num_classes):
            loader = self.dataset.get_dataloader(mode='train', batch_size=seed_class_num, classes=[_class],
                                                 shuffle=True, num_workers=0, pin_memory=False)
            _input, _label = next(iter(loader))
            x.append(_input)
            y.append(_label)
        x = torch.cat(x).numpy()
        y = torch.cat(y).numpy()
        seed_data = {'input': x, 'label': y}
        seed_path = f'{env["result_dir"]}{self.dataset.name}/{self.name}_{self.seed_num}.npy'
        np.save(seed_path, seed_data)
        print('seed data saved at: ', seed_path)
        return seed_data

    def load_seed_data(self) -> Dict[str, torch.Tensor]:
        seed_path = f'{env["result_dir"]}{self.dataset.name}/{self.name}_{self.seed_num}.npy'
        seed_data: Dict[str, torch.Tensor] = {}
        seed_data = np.load(seed_path, allow_pickle=True).item() if os.path.exists(seed_path) \
            else self.save_seed_data()
        seed_data['input'] = to_tensor(seed_data['input'])
        seed_data['label'] = to_tensor(seed_data['label'], dtype=torch.long)
        return seed_data

    # -----------------------Neural Sample---------------------------- #

    def sample_neuron(self, _input: torch.Tensor) -> Dict[str, torch.Tensor]:
        all_ps: Dict[str, torch.Tensor] = {}
        batch_size = _input.shape[0]

        layer_output = self.model.get_all_layer(_input)
        for layer in self.model.get_layer_name():
            if 'pool' in layer or layer in ['features', 'flatten', 'classifier', 'logits', 'output']:
                continue
            cur_layer_output: torch.Tensor = layer_output[layer].detach().cpu()  # (batch_size, C, H, W)
            channel_num: int = cur_layer_output.shape[1]  # channels

            repeat_shape = [channel_num, self.n_samples]
            repeat_shape.extend([1] * len(cur_layer_output.shape))
            h_t: torch.Tensor = cur_layer_output.repeat(repeat_shape)
            # (C, n_samples, batch_size, C, H, W)

            vs = self.samp_k * torch.arange(self.n_samples, device=h_t.device, dtype=torch.float)
            if not self.same_range:
                maxes = cur_layer_output.max()
                vs *= float(maxes) / self.n_samples
            vs_shape = [1] * len(cur_layer_output.shape)
            vs_shape[0] = -1
            vs = vs.view(vs_shape)
            # (n_samples, 1, 1, 1)
            # todo: might use parallel to avoid for loop (torch.Tensor.scatter?)
            for neuron in range(channel_num):
                h_t[neuron, :, :, neuron] = vs
            # todo: the shape is too large
            # result = self.model.get_layer(h_t.flatten(end_dim=2), layer_input=layer).detach().cpu()
            result = []
            for h in h_t:
                h = h.to(device=env['device'])
                result.append(self.model.get_layer(h.flatten(end_dim=1), layer_input=layer).detach().cpu())
            result = torch.cat(result)

            result_shape = list(h_t.shape)[:3]
            result_shape.extend(list(result.shape)[1:])
            result = result.view(result_shape)
            all_ps[layer] = result
            # (C, n_samples, batch_size, num_classes)
        return all_ps

    def find_min_max(self, all_ps: Dict[str, torch.Tensor], _label: torch.Tensor) -> Dict[int, List[dict]]:
        neuron_dict: Dict[int, list] = {i: [] for i in range(self.model.num_classes)}
        _label = _label.cpu()
        for layer in all_ps.keys():
            ps = all_ps[layer]  # (C, n_samples, batch_size, num_classes)
            vs: torch.Tensor = ps[:, self.n_samples // 5:].max(dim=1)[0] \
                - ps[:, :self.n_samples // 5].min(dim=1)[0]  # (C, batch_size, num_classes)
            values, labels = vs.sort(dim=-1, descending=True)
            condition1 = labels[:, :, 0].eq(_label)  # exclude the ground-truth labels
            values = torch.where(condition1, values[:, :, 1] - values[:, :, 2],
                                 values[:, :, 0] - values[:, :, 1])  # (C, batch_size)
            labels = torch.where(condition1, labels[:, :, 1], labels[:, :, 0])  # (C, batch_size)

            mode_labels = labels.mode(keepdim=True)[0]  # (C, 1) The most frequent label
            mode_idx = labels.eq(mode_labels)  # (C, batch_size)
            mode_labels_counts = mode_idx.sum(dim=-1)  # (C)
            condition2 = mode_labels_counts.ge(self.seed_num * 0.75)
            idx_list = condition2.nonzero().flatten().tolist()
            idx_list = sorted(idx_list, key=lambda idx: float(values[idx][mode_idx[idx]].min()))[:self.top_n_neurons]
            for idx in idx_list:
                value = float(values[idx][mode_idx[idx]].min())
                neuron_dict[int(mode_labels[idx])].append({'layer': layer, 'neuron': int(idx), 'value': value})
        return neuron_dict
    # -------------------------ReMask--------------------------------- #

    def abs_loss(self, layer_dict: Dict[str, torch.Tensor], layer: str, neuron: int):
        feats = layer_dict[layer]
        vloss1 = feats[:, neuron].sum()
        vloss2 = feats.sum() - vloss1
        return -vloss1 + 1e-3 * vloss2

    def loss_fn(self, _input: torch.Tensor, _label: torch.LongTensor, **kwargs) -> torch.Tensor:
        loss = self.model.loss(_input, _label)
        idx = 0
        for i in range(len(_label)):
            if _label[len(_label) - 1 - i] != self.target_class:
                break
            idx = len(_label) - 1 - i
        _input = _input[idx:]
        _label = _label[idx:]
        layer_dict = self.model.get_all_layer(_input)
        for sub_dict in self.neuron_list:
            layer = sub_dict['layer']
            neuron = sub_dict['neuron']
            loss -= self.abs_weight * self.abs_loss(layer_dict, layer, neuron)
        return loss

    def add_strip_mark(self, x: torch.Tensor, **kwargs) -> torch.Tensor:
        return self.mark.add_mark(x, alpha=1 - (1 - self.mark.mark_alpha) / 2, **kwargs)

    def get_data(self, data: Tuple[torch.Tensor, torch.LongTensor], **kwargs) -> Tuple[torch.Tensor, torch.LongTensor]:
        _input, _label = self.model.get_data(data)

        decimal, integer = math.modf(self.poison_num)
        integer = int(integer)
        if random.uniform(0, 1) < decimal:
            integer += 1
        if integer:
            org_input, org_label = _input, _label
            poison_input = self.add_mark(org_input[:integer])
            poison_label = self.target_class * torch.ones_like(org_label[:integer])
            strip_num: float = integer * self.strip_percent
            strip_decimal, strip_integer = math.modf(strip_num)
            strip_integer = int(strip_integer)
            if random.uniform(0, 1) < strip_decimal:
                strip_integer += 1
            if strip_integer:
                strip_input = self.add_strip_mark(org_input[:strip_integer])
                strip_label = org_label[:strip_integer]
                _input = torch.cat((_input, strip_input))
                _label = torch.cat((_label, strip_label))
            _input = torch.cat((_input, poison_input))
            _label = torch.cat((_label, poison_label))
        return _input, _label

    def get_poison_data(self, data: Tuple[torch.Tensor, torch.LongTensor], poison_label: bool = True, strip: bool = False, **kwargs) -> Tuple[torch.Tensor, torch.LongTensor]:
        _input, _label = self.model.get_data(data)
        integer = len(_label)
        if strip:
            _input = self.add_strip_mark(_input[:integer])
        else:
            _input = self.add_mark(_input[:integer])
        if poison_label:
            _label = self.target_class * torch.ones_like(_label[:integer])
        else:
            _label = _label[:integer]
        return _input, _label

    def validate_func(self, get_data=None, loss_fn=None, **kwargs) -> Tuple[float, float, float]:
        clean_loss, clean_acc, _ = self.model._validate(print_prefix='Validate Clean',
                                                        get_data=None, **kwargs)
        target_loss, target_acc, _ = self.model._validate(print_prefix='Validate Trigger Tgt',
                                                          get_data=self.get_poison_data, **kwargs)
        _, orginal_acc, _ = self.model._validate(print_prefix='Validate Trigger Org',
                                                 get_data=self.get_poison_data, poison_label=False, **kwargs)
        self.model._validate(print_prefix='Validate STRIP Tgt',
                             get_data=self.get_poison_data, strip=True, **kwargs)
        self.model._validate(print_prefix='Validate STRIP Org',
                             get_data=self.get_poison_data, strip=True, poison_label=False, **kwargs)
        print(f'Validate Confidence : {self.validate_confidence():.3f}')
        if self.clean_acc - clean_acc > 3 and self.clean_acc > 40:
            target_acc = 0.0
        return clean_loss + target_loss, target_acc, clean_acc

    def get_filename(self, mark_alpha: float = None, target_class: int = None, **kwargs):
        if mark_alpha is None:
            mark_alpha = self.mark.mark_alpha
        if target_class is None:
            target_class = self.target_class
        _file = '{mark}_tar{target:d}_alpha{mark_alpha:.2f}_mark({height:d},{width:d})'.format(
            mark=os.path.split(self.mark.mark_path)[1][:-4],
            target=target_class, mark_alpha=mark_alpha,
            height=self.mark.height, width=self.mark.width)
        if self.mark.random_pos:
            _file = 'random_pos_' + _file
        if self.mark.mark_distributed:
            _file = 'distributed_' + _file
        _file += f'strippercent_{self.strip_percent:f}_absweight_{self.abs_weight:f}'
        return _file