# -*- coding: utf-8 -*-

# python backdoor_defense.py --attack badnet --defense neural_cleanse --verbose --pretrain --validate_interval 1 --epoch 50 --lr 1e-2 --mark_height 3 --mark_width 3 --mark_alpha 0.0

import trojanzoo.environ
import trojanzoo.datasets
import trojanzoo.models
import trojanzoo.trainer
import trojanzoo.attacks
import trojanzoo.defenses

from trojanzoo.utils import summary
import argparse

import warnings
warnings.filterwarnings("ignore")

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    trojanzoo.environ.add_argument(parser)
    trojanzoo.datasets.add_argument(parser)
    trojanzoo.models.add_argument(parser)
    trojanzoo.trainer.add_argument(parser)
    trojanzoo.attacks.add_argument(parser)
    trojanzoo.defenses.add_argument(parser)
    args = parser.parse_args()

    env = trojanzoo.environ.create(**args.__dict__)
    dataset = trojanzoo.datasets.create(**args.__dict__)
    model = trojanzoo.models.create(dataset=dataset, **args.__dict__)
    trainer = trojanzoo.trainer.create(dataset=dataset, model=model, **args.__dict__)
    attack = trojanzoo.attacks.create(dataset=dataset, model=model, **args.__dict__)
    defense = trojanzoo.defenses.create(dataset=dataset, model=model, attack=attack, **args.__dict__)

    if env['verbose']:
        summary(dataset=dataset, model=model, trainer=trainer, attack=attack, defense=defense)
    defense.detect(**trainer)
