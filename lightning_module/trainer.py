#!/usr/bin/env python
# -*- coding:utf-8 -*-
from __future__ import absolute_import
from __future__ import unicode_literals
from __future__ import division
from __future__ import print_function

from typing import Optional, Dict, Tuple
from collections import defaultdict
import pickle
import numpy as np
import torch
from torch import nn
from torch.utils.data import Dataset, DataLoader
from torch.nn.modules.loss import _Loss
from torch.optim import Adam
import pytorch_lightning as pl

from model.autoencoder import AutoEncoder, MaskedAutoEncoder
from model.loss import ReconstructionLoss, HyponymyScoreLoss, NonHyponymyScoreLoss

class UnsupervisedTrainer(pl.LightningModule):

    def __init__(self,
                 model: AutoEncoder,
                 loss_reconst: _Loss,
                 loss_mutual_info: Optional[_Loss] = None,
                 dataloader_train: Optional[DataLoader] = None,
                 dataloader_val: Optional[DataLoader] = None,
                 dataloader_test: Optional[DataLoader] = None,
                 learning_rate: Optional[float] = 0.001
                 ):

        super(UnsupervisedTrainer, self).__init__()

        self._model = model
        self._encoder = model._encoder
        self._decoder = model._decoder
        self._loss_reconst = loss_reconst
        self._loss_mutual_info = loss_mutual_info
        self._learning_rate = learning_rate
        self._dataloaders = {
            "train": dataloader_train,
            "val": dataloader_val,
            "test": dataloader_test
        }
        # auxiliary function that is solely used for validation
        self._auxiliary = HyponymyScoreLoss()

    def _numpy_to_tensor(self, np_array: np.array):
        return torch.from_numpy(np_array).to(self._device)

    def configure_optimizers(self):
        opt = Adam(self.parameters(), lr=self._learning_rate)
        return opt

    @pl.data_loader
    def tng_dataloader(self):
        return self._dataloaders["train"]

    @pl.data_loader
    def val_dataloader(self):
        return self._dataloaders["val"]

    @pl.data_loader
    def test_dataloader(self):
        return self._dataloaders["test"]

    def forward(self, x):
        return self._model.forward(x)

    def training_step(self, data_batch, batch_nb):

        # forward computation
        t_x = data_batch["embedding"]
        t_latent_code, t_code_prob, t_x_dash = self._model.forward(t_x)

        loss_reconst = self._loss_reconst.forward(t_x_dash, t_x)

        if self._loss_mutual_info is not None:
            loss_mi = self._loss_mutual_info(t_code_prob)
        else:
            loss_mi = torch.tensor(0.0, dtype=torch.float32)

        loss = loss_reconst + loss_mi

        dict_losses = {
            "loss_reconst": loss_reconst,
            "loss_mutual_info": loss_mi,
            "loss": loss
        }
        return dict_losses

    def _evaluate_code_stats(self, t_code_prob):

        _EPS = 1E-6
        n_ary = self._model.n_ary
        soft_code_length = self._auxiliary.calc_soft_code_length(t_code_prob)
        code_probability_divergence = torch.mean(np.log(n_ary) + torch.sum(t_code_prob * torch.log(t_code_prob + _EPS), axis=-1), axis=-1)

        metrics = {
            "val_soft_code_length_mean":torch.mean(soft_code_length),
            "val_soft_code_length_std":torch.std(soft_code_length),
            "val_code_probability_divergence":torch.mean(code_probability_divergence)
        }
        return metrics


    def validation_step(self, data_batch, batch_nb):

        # forward computation without back-propagation
        t_x = data_batch["embedding"]
        t_intermediate, t_code_prob, t_x_dash = self._model._predict(t_x)

        loss_reconst = self._loss_reconst.forward(t_x_dash, t_x)
        if self._loss_mutual_info is not None:
            loss_mi = self._loss_mutual_info(t_code_prob)
        else:
            loss_mi = torch.tensor(0.0, dtype=torch.float32)

        loss = loss_reconst + loss_mi

        metrics = {
            "val_loss_reconst": loss_reconst,
            "val_mutual_info": loss_mi,
            "val_loss": loss
        }
        if self._loss_mutual_info is not None:
            metrics_repr = self._evaluate_code_stats(t_code_prob)
            metrics.update(metrics_repr)

        return {"val_loss":loss, "log":metrics}

    def validation_end(self, outputs):
        avg_loss = torch.stack([x['val_loss'] for x in outputs]).mean()

        avg_metrics = defaultdict(list)
        for output in outputs:
            for key, value in output["log"].items():
                avg_metrics[key].append(value)
        for key, values in avg_metrics.items():
            avg_metrics[key] = torch.stack(values).mean()
        return {'avg_val_loss': avg_loss, 'log': avg_metrics}

    def on_save_checkpoint(self, checkpoint):
        checkpoint["model_dump"] = pickle.dumps(self._model)

    @classmethod
    def load_model_from_checkpoint(self, weights_path: str, on_gpu, map_location=None):
        if on_gpu:
            if map_location is not None:
                checkpoint = torch.load(weights_path, map_location=map_location)
            else:
                checkpoint = torch.load(weights_path)
        else:
            checkpoint = torch.load(weights_path, map_location=lambda storage, loc: storage)

        model = pickle.loads(checkpoint["model_dump"])
        state_dict = {key.replace("_model.", ""):param for key, param in checkpoint["state_dict"].items()}
        model.load_state_dict(state_dict)

        return model


class SupervisedHypernymyRelationTrainer(UnsupervisedTrainer):

    def __init__(self,
                 model: MaskedAutoEncoder,
                 loss_reconst: ReconstructionLoss,
                 loss_hyponymy: HyponymyScoreLoss,
                 use_intermediate_repr_for_hyponymy_score: bool = False,
                 loss_mutual_info: Optional[_Loss] = None,
                 loss_non_hyponymy: Optional[NonHyponymyScoreLoss] = None,
                 dataloader_train: Optional[DataLoader] = None,
                 dataloader_val: Optional[DataLoader] = None,
                 dataloader_test: Optional[DataLoader] = None,
                 learning_rate: Optional[float] = 0.001
                 ):

        super().__init__(model, loss_reconst, loss_mutual_info, dataloader_train, dataloader_val, dataloader_test, learning_rate)

        self._loss_hyponymy = loss_hyponymy
        self._loss_non_hyponymy = loss_non_hyponymy
        self._use_intermediate_repr_for_hyponymy_score = use_intermediate_repr_for_hyponymy_score

        self._scale_loss_reconst = loss_reconst.scale
        self._scale_loss_mi = loss_mutual_info.scale if loss_mutual_info is not None else 1.
        self._scale_loss_hyponymy = loss_hyponymy.scale
        self._scale_loss_non_hyponymy = loss_non_hyponymy.scale if loss_non_hyponymy is not None else 1.

    def training_step(self, data_batch, batch_nb):

        # forward computation
        t_x = data_batch["embedding"]
        t_latent_code, t_code_prob, t_x_dash = self._model.forward(t_x)

        # (required) reconstruction loss
        loss_reconst = self._loss_reconst(t_x_dash, t_x)

        # hyponymy relation related loss
        if self._use_intermediate_repr_for_hyponymy_score:
            code_repr = t_latent_code
        else:
            code_repr = t_code_prob

        # (required) hyponymy score loss
        lst_tup_hyponymy = data_batch["hyponymy_relation"]
        loss_hyponymy = self._loss_hyponymy(code_repr, lst_tup_hyponymy)

        # (optional) non-hyponymy score loss
        if self._loss_non_hyponymy is not None:
            lst_tup_non_hyponymy = data_batch["non_hyponymy_relation"]
            loss_non_hyponymy = self._loss_non_hyponymy(code_repr, lst_tup_non_hyponymy)
        else:
            loss_non_hyponymy = torch.tensor(0.0, dtype=torch.float32)

        # (optional) mutual information loss
        if self._loss_mutual_info is not None:
            loss_mi = self._loss_mutual_info(t_code_prob)
        else:
            loss_mi = torch.tensor(0.0, dtype=torch.float32)

        loss = loss_reconst + loss_hyponymy + loss_non_hyponymy + loss_mi

        dict_losses = {
            "train_loss_reconst": loss_reconst,
            "train_loss_mutual_info": loss_mi / self._scale_loss_mi,
            "train_loss_hyponymy": loss_hyponymy / self._scale_loss_hyponymy,
            "train_loss_non_hyponymy": loss_non_hyponymy / self._scale_loss_non_hyponymy,
            "train_loss": loss
        }
        return {"loss":loss, "log": dict_losses}

    def validation_step(self, data_batch, batch_nb):

        # forward computation without back-propagation
        t_x = data_batch["embedding"]
        t_latent_code, t_code_prob, t_x_dash = self._model._predict(t_x)

        # (required) reconstruction loss
        loss_reconst = self._loss_reconst(t_x_dash, t_x)

        # hyponymy relation related loss
        if self._use_intermediate_repr_for_hyponymy_score:
            code_repr = t_latent_code
        else:
            code_repr = t_code_prob

        # (required) hyponymy score loss
        lst_tup_hyponymy = data_batch["hyponymy_relation"]
        loss_hyponymy = self._loss_hyponymy(code_repr, lst_tup_hyponymy)

        # (optional) non-hyponymy score loss
        if self._loss_non_hyponymy is not None:
            lst_tup_non_hyponymy = data_batch["non_hyponymy_relation"]
            loss_non_hyponymy = self._loss_non_hyponymy(code_repr, lst_tup_non_hyponymy)
        else:
            loss_non_hyponymy = torch.tensor(0.0, dtype=torch.float32)

        # (optional) mutual information loss
        if self._loss_mutual_info is not None:
            loss_mi = self._loss_mutual_info(t_code_prob)
        else:
            loss_mi = torch.tensor(0.0, dtype=torch.float32)

        loss = loss_reconst + loss_hyponymy + loss_non_hyponymy + loss_mi

        metrics = {
            "val_loss_reconst": loss_reconst,
            "val_loss_mutual_info": loss_mi / self._scale_loss_mi,
            "val_loss_hyponymy": loss_hyponymy / self._scale_loss_hyponymy,
            "val_loss_non_hyponymy": loss_non_hyponymy / self._scale_loss_non_hyponymy,
            "val_loss": loss
        }
        metrics_repr = self._evaluate_code_stats(t_code_prob)
        metrics.update(metrics_repr)

        return {"val_loss":loss, "log":metrics}

    def on_epoch_end(self):
        if hasattr(self._model._encoder, "gate_open_ratio"):
            coef_accelerator = 1.2
            current_value = self._model._encoder.gate_open_ratio
            if current_value is not None:
                new_value = min(1.0, coef_accelerator * (self.current_epoch+1) / self.trainer.max_nb_epochs)
                self._model._encoder.gate_open_ratio = new_value

                # DEBUG
                print(f"update gate_open_ratio: {current_value:.2} -> {new_value:.2}")



class SupervisedCodeLengthTrainer(UnsupervisedTrainer):
    pass
