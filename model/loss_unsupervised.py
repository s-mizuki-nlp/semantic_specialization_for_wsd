#!/usr/bin/env python
# -*- coding:utf-8 -*-

from typing import Optional
import torch
from torch.nn.modules import loss as L, PairwiseDistance

from .similarity import CosineSimilarity
from .loss import _create_mask_tensor

### unsupervised loss classes ###

class MaxPoolingMarginLoss(L._Loss):

    def __init__(self, similarity_module: Optional[torch.nn.Module] = None,
                 label_threshold: float = 0.0, top_k: int = 1,
                 size_average=None, reduce=None, reduction: str = "mean"):
        super().__init__(size_average, reduce, reduction)

        self._similarity = CosineSimilarity(temperature=1.0) if similarity_module is None else similarity_module
        self._label_threshold = label_threshold
        self._top_k = top_k
        self._size_average = size_average
        self._reduce = reduce
        self._reduction = reduction

    def forward(self, queries: torch.Tensor, targets: torch.Tensor, num_target_samples: torch.LongTensor):
        # queries: (n, n_dim)
        # targetss: (n, n_tgt = max({n^tgt_i};0<=i<n), n_dim)
        # num_target_samples: (n,)
        # num_target_samples[i] = [1, n_tgt]; number of effective target samples for i-th query.

        # is_hard_examples: affects when similarity_module is ArcMarginProduct.
        # mat_sim_neg: (n, n_tgt)
        mat_sim = self._similarity(queries.unsqueeze(dim=1), targets, is_hard_examples=False)
        # fill -inf with masked positions
        mask_tensor = _create_mask_tensor(num_target_samples)
        mat_sim = mat_sim.masked_fill_(mask_tensor, value=-float("inf"))
        # vec_sim_topk: (n,); top-k average similarity for each query.
        if self._top_k == 1:
            vec_sim_topk, _ = torch.max(mat_sim, dim=-1)
        else:
            mat_sim_topk, _ = torch.topk(mat_sim, k=self._top_k)
            # replace invalid elements with zeroes
            mat_sim_topk = mat_sim_topk.masked_fill_(mask_tensor[:, :self._top_k], value=0.0)
            # take top-k average while number of target samples into account.
            t_denom = self._top_k - mask_tensor[:, :self._top_k].sum(dim=-1)
            vec_sim_topk = mat_sim_topk.sum(dim=-1) / t_denom

        # compare the threshold with similarity diff. between top-1 and top-2.
        # dummy elements are masked by -inf, then it naturally exceeds threshold = regarded as valid example.
        if mat_sim.shape[-1] > 1:
            obj = torch.topk(mat_sim, k=2, largest=True)
            is_valid_sample = (obj.values[:,0] - obj.values[:,1]) > self._label_threshold
        else:
            is_valid_sample = torch.ones_like(vec_sim_topk).type(torch.bool)

        # loss = 1.0 - negative top-k similarity as long as
        losses = (1.0 - vec_sim_topk) * is_valid_sample + 1.0 * (is_valid_sample == False)
        n_samples = max(1, is_valid_sample.sum().item())

        if self.reduction == "mean":
            return torch.sum(losses) / n_samples
        elif self.reduction == "sum":
            return torch.sum(losses)
        elif self.reduction == "none":
            return losses