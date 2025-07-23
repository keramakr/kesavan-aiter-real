# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2025, Advanced Micro Devices, Inc. All rights reserved.

# user interface

import torch
from torch import Tensor
from ..jit.core import (
    compile_ops,
)
from ..utility import dtypes
from ..jit.utils.chip_info import get_cu_num


@compile_ops("module_moe_asm", fc_name="biased_grouped_topk")
def biased_grouped_topk_hip(
    gating_output: Tensor,
    correction_bias: Tensor,
    topk_weights: Tensor,
    topk_ids: Tensor,
    num_expert_group: int,
    topk_grp: int,
    need_renorm: bool,
    routed_scaling_factor: float = 1.0,
) -> None: ...


@compile_ops("module_moe_asm")
def grouped_topk(
    gating_output: Tensor,
    topk_weights: Tensor,
    topk_ids: Tensor,
    num_expert_group: int,
    topk_group: int,
    need_renorm: bool,
    scoring_func: str = "softmax",
    scale_factor: float = 1.0,
): ...


@compile_ops("module_moe_asm")
def moe_fused_gate(
    input: Tensor,
    bias: Tensor,
    num_expert_group: int,
    topk_group: int,
    topk: int,
    n_share_experts_fusion: int,
    scale_factor: float = 1.0,
): ...


def biased_grouped_topk(
    gating_output: Tensor,
    correction_bias: Tensor,
    topk_weights: Tensor,
    topk_ids: Tensor,
    num_expert_group: int,
    topk_group: int,
    need_renorm: bool,
    routed_scaling_factor: float = 1.0,  # mul to topk_weights
):
    token_num = gating_output.shape[0]
    cu_num = get_cu_num()
    if token_num >= cu_num * 16:
        return biased_grouped_topk_hip(
            gating_output,
            correction_bias,
            topk_weights,
            topk_ids,
            num_expert_group,
            topk_group,
            need_renorm,
            routed_scaling_factor,
        )
    else:
        topk = topk_ids.shape[1]
        assert need_renorm, "Renormalization is required for moe_fused_gate."
        return moe_fused_gate(
            gating_output,
            correction_bias,
            num_expert_group,
            topk_group,
            topk,
            n_share_experts_fusion=0,
            scale_factor=routed_scaling_factor,
        )


# this one copied from sglang
def biased_grouped_topk_torch(
    gating_output: torch.Tensor,
    correction_bias: torch.Tensor,
    topk: int,
    renormalize: bool,
    num_expert_group: int = 0,
    topk_group: int = 0,
):
    scores = gating_output.to(dtypes.fp32).sigmoid()
    num_token = scores.shape[0]

    scores_for_choice = scores.view(num_token, -1) + correction_bias.unsqueeze(0)

    group_scores = (
        scores_for_choice.view(num_token, num_expert_group, -1)
        .topk(2, dim=-1)[0]
        .sum(dim=-1)
    )  # [n, n_group]

    group_idx = torch.topk(group_scores, k=topk_group, dim=-1, sorted=False)[
        1
    ]  # [n, top_k_group]
    group_mask = torch.zeros_like(group_scores)  # [n, n_group]
    group_mask.scatter_(1, group_idx, 1)  # [n, n_group]
    score_mask = (
        group_mask.unsqueeze(-1)
        .expand(num_token, num_expert_group, scores.shape[-1] // num_expert_group)
        .reshape(num_token, -1)
    )  # [n, e]
    tmp_scores = scores_for_choice.masked_fill(~score_mask.bool(), 0.0)  # [n, e]

    _, topk_ids = torch.topk(tmp_scores, k=topk, dim=-1, sorted=False)
    topk_weights = scores.gather(1, topk_ids)

    if renormalize:
        topk_weights = topk_weights / topk_weights.sum(dim=-1, keepdim=True)

    return topk_weights.to(dtypes.fp32), topk_ids.to(dtypes.i32)


# this one copied from sglang
def grouped_topk_torch(
    gating_output: torch.Tensor,
    topk: int,
    renormalize: bool,
    num_expert_group: int = 0,
    topk_group: int = 0,
    scoring_func: str = "softmax",
):
    gating_output = gating_output.to(dtypes.fp32)
    if scoring_func == "softmax":
        scores = torch.softmax(gating_output, dim=-1)
    elif scoring_func == "sigmoid":
        scores = gating_output.sigmoid()
    else:
        raise ValueError(f"Scoring function '{scoring_func}' is not supported.")

    num_token = scores.shape[0]
    group_scores = (
        scores.view(num_token, num_expert_group, -1).max(dim=-1).values
    )  # [n, n_group]
    group_idx = torch.topk(group_scores, k=topk_group, dim=-1, sorted=False)[
        1
    ]  # [n, top_k_group]
    group_mask = torch.zeros_like(group_scores)  # [n, n_group]
    group_mask.scatter_(1, group_idx, 1)  # [n, n_group]
    score_mask = (
        group_mask.unsqueeze(-1)
        .expand(num_token, num_expert_group, scores.shape[-1] // num_expert_group)
        .reshape(num_token, -1)
    )  # [n, e]
    tmp_scores = scores.masked_fill(~score_mask.bool(), 0.0)  # [n, e]
    topk_weights, topk_ids = torch.topk(tmp_scores, k=topk, dim=-1, sorted=False)

    if renormalize:
        topk_weights = topk_weights / topk_weights.sum(dim=-1, keepdim=True)

    return topk_weights.to(dtypes.fp32), topk_ids.to(dtypes.i32)
