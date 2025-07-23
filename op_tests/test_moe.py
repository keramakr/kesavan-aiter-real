# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2025, Advanced Micro Devices, Inc. All rights reserved.

import torch
from aiter.test_common import checkAllclose, perftest
from aiter import dtypes
from aiter.fused_moe import torch_moe, fused_topk
from aiter.fused_moe_bf16_asm import asm_moe
from aiter.ops.shuffle import shuffle_weight
from aiter import pertoken_quant
from aiter.int4_utils import *
from aiter import ActivationType
import argparse

BLOCK_SIZE_M = 32


def permute_weight_a(x: torch.Tensor) -> torch.Tensor:
    # Hardcode BLOCK_K and BLOCK_N
    BK = 128
    BN = 128
    x_ = x
    x_ = x_.view(
        x.shape[0], x.shape[1] // BN, BN // 16, 16, x.shape[2] // BK, BK // 32, 4, 8
    )
    x_ = x_.permute(0, 1, 5, 2, 6, 4, 3, 7)
    x_ = x_.contiguous()
    x_ = x_.view(x.shape[0], x.shape[1], x.shape[2])
    return x_


@perftest(num_warmup=1, num_iters=2)
def torch_moe_test(
    hidden_states,
    w1,
    w2,
    topk_weight,
    topk_ids,
    # following for int8 quant
    fc1_scale=None,  # [expert, inter_dim, 1]
    fc2_scale=None,  # [expert, model_dim, 1]
    fc1_smooth_scale=None,  # [expert, 1, model_dim]
    fc2_smooth_scale=None,  # [expert, 1, inter_dim]
    activation=ActivationType.Silu,
):
    return torch_moe(
        hidden_states,
        w1,
        w2,
        topk_weight,
        topk_ids,
        fc1_scale,
        fc2_scale,
        fc1_smooth_scale,
        fc2_smooth_scale,
        None,
        activation,
    )


@perftest()
def asm_moe_test(
    hidden_states,
    w1,
    w2,
    topk_weight,
    topk_ids,
    # following for int8 quant
    fc1_scale=None,  # [expert, inter_dim, 1]
    fc2_scale=None,  # [expert, model_dim, 1]
    fc1_smooth_scale=None,  # [expert, 1, model_dim]
    fc2_smooth_scale=None,  # [expert, 1, inter_dim]
    a16=False,
    activation=ActivationType.Silu,
):
    return asm_moe(
        hidden_states,
        w1,
        w2,
        topk_weight,
        topk_ids,
        fc1_scale,
        fc2_scale,
        fc1_smooth_scale,
        fc2_smooth_scale,
        a16,
        None,
        None,
        None,
        activation,
    )


@perftest()
def vllm_moe(hidden_states, w1, w2, topk_weight, topk_ids):
    return fused_experts(hidden_states, w1, w2, topk_weight, topk_ids, inplace=False)


quant_algo = [
    "No",  # g1u0/ck(g1ux) support
    "int8quant",  # g1u1 support
    "fp8quant",  # g1u1 support
    "int8smoothquant",  # g1u1/g1u0 support
    "fp8smoothquant",  # g1u1 support
    "wint4afp8smoothquant",  # g1u1 support
]


def test_fmoe(
    dtype,
    token,
    model_dim,
    inter_dim,
    E,
    topk,
    quant="No",
    use_g1u1=False,
    shared_E=0,
    activation=ActivationType.Silu,
):
    quantAlgoId = quant_algo.index(quant)
    if quantAlgoId not in [0, 3] and not use_g1u1:
        print("g1u0 only could test no quant and int8smoothquant")
        return

    quantstr = quant_algo[quantAlgoId]
    use_int4 = "wint4" in quantstr
    quant_dtype = dtypes.i8 if use_int4 or quantstr.startswith("int8") else dtypes.fp8
    use_smooth = "smooth" in quantstr
    input = torch.randn((token, model_dim), dtype=dtype, device="cuda")
    if use_g1u1:
        w1 = (
            torch.randn(
                (E + shared_E, inter_dim * 2, model_dim), dtype=dtype, device="cuda"
            )
            / 10.0
        )
    else:
        w1 = torch.randn(
            (E + shared_E, inter_dim, model_dim), dtype=dtype, device="cuda"
        )
    w2 = torch.randn((E + shared_E, model_dim, inter_dim), dtype=dtype, device="cuda")
    score = torch.randn((token, E), device="cuda", dtype=dtype)
    topk_weights, topk_ids = fused_topk(input, score, topk, True)

    if shared_E > 0:
        shared_E_score = 0.5
        s_topk_weights = torch.tensor(
            [
                [shared_E_score, shared_E_score],
            ]
            * token,
            dtype=dtypes.fp32,
            device=input.device,
        )
        topk_weights = torch.cat((topk_weights, s_topk_weights), dim=1)
        s_topk_ids = torch.tensor(
            [
                [E, E + 1],
            ]
            * token,
            dtype=dtypes.i32,
            device=input.device,
        )
        topk_ids = torch.cat((topk_ids, s_topk_ids), dim=1)

    # ref implement
    # w1a = permute_weight_a(w1)
    # w2a = permute_weight_a(w2)
    w1a = w1
    w2a = w2
    avg_a = 1
    # ref1, avg_a = vllm_moe(input,
    #                        w1a,
    #                        w2a,
    #                        topk_weights,
    #                        topk_ids)
    # print(f'{ref1=}')

    if quantAlgoId == 0:
        # ref2 implement
        ref2, avg_c = torch_moe_test(input, w1, w2, topk_weights, topk_ids)

        # b implement
        w1b = shuffle_weight(w1)
        w2b = shuffle_weight(w2)

        if use_g1u1:
            out_b = ref2
            avg_b = 9999
            print("asm g1u1 only support quant/smoothquant Now")
        else:
            out_b, avg_b = asm_moe_test(input, w1b, w2b, topk_weights, topk_ids)

        msg = f"[perf] {token=}, quant={quantstr}, {model_dim=}, {inter_dim=}, {E=}, {topk=}, dtype: {dtype}, torch_avg: {avg_c:<8.2f} us, asm_avg: {avg_b:>8.2f} us, uplift: {avg_c/avg_b-1:.1%}"
        checkAllclose(ref2, out_b, rtol=0.01, atol=100, msg=msg)
    else:
        dtypeMax = 7 if use_int4 else None
        w1, fc1_scale = pertoken_quant(w1, quant_dtype=quant_dtype, dtypeMax=dtypeMax)
        w2, fc2_scale = pertoken_quant(w2, quant_dtype=quant_dtype, dtypeMax=dtypeMax)

        sp1 = (E + shared_E, inter_dim)
        sp2 = (E + shared_E, model_dim)

        if not use_smooth:
            fc1_smooth_scale = None
            fc2_smooth_scale = None
        else:
            if use_int4:
                # fixme @felix: hack here, int4 kernel need this buffer but not used, so ones.
                # [expert, 1, model_dim]
                fc1_smooth_scale = torch.ones(sp2, dtype=dtypes.fp32, device="cuda")
                # [expert, 1, inter_dim]
                fc2_smooth_scale = torch.ones(sp1, dtype=dtypes.fp32, device="cuda")
            else:
                # [expert, 1, model_dim]
                fc1_smooth_scale = torch.randn(sp2, dtype=dtypes.fp32, device="cuda")
                # [expert, 1, inter_dim]
                fc2_smooth_scale = torch.randn(sp1, dtype=dtypes.fp32, device="cuda")

        # ref2 implement
        ref2, avg_c = torch_moe_test(
            input,
            w1,
            w2,
            topk_weights,
            topk_ids,
            fc1_scale,
            fc2_scale,
            fc1_smooth_scale,
            fc2_smooth_scale,
            activation,
        )

        # b implement
        if use_int4:
            w1 = rearrange_4bit_elements(convert_int8_to_uint32_int4(w1))
            w2 = rearrange_4bit_elements(convert_int8_to_uint32_int4(w2))
        w1b = shuffle_weight(w1)
        w2b = shuffle_weight(w2)
        out_b, avg_b = asm_moe_test(
            input,
            w1b,
            w2b,
            topk_weights,
            topk_ids,
            fc1_scale,
            fc2_scale,
            fc1_smooth_scale,
            fc2_smooth_scale,
            a16=False,
            activation=activation,
        )

        def calculateTensorsSize(*args):
            num_btype = 0
            for el in args:
                if isinstance(el, torch.Tensor):
                    num_btype += el.element_size() * el.numel()
            return num_btype

        num_tb = calculateTensorsSize(
            input,
            input,
            w1b,
            w2b,
            topk_weights,
            topk_ids,
            fc1_scale,
            fc2_scale,
            fc1_smooth_scale,
            fc2_smooth_scale,
        ) / (1024 * 1024 * 1024 * 1024.0)
        bw = num_tb * 1e6 / avg_b
        print(
            f"[BW  ] {token=}, quant={quantstr}, {model_dim=}, {inter_dim=}, {E=}, {shared_E=}, {topk=}, dtype: {dtype}, asm_bandwidth: {bw:>8.2f}TB/s"
        )

        if use_smooth and (
            (
                (inter_dim % 512 == 0 or inter_dim % 320 == 0)
                and (w1b.dtype == dtypes.fp8 and inter_dim * 2 == w1b.shape[1])
            )
            or (
                (inter_dim % 320 == 0 or inter_dim % 256 == 0)
                and (w1b.dtype == dtypes.i8 and inter_dim * 2 == w1b.shape[1])
            )
            or (
                (inter_dim % 512 == 0)
                and (w1b.dtype == dtypes.i8 and inter_dim == w1b.shape[1])
            )
        ):
            out_b2, avg_b2 = asm_moe_test(
                input,
                w1b,
                w2b,
                topk_weights,
                topk_ids,
                fc1_scale,
                fc2_scale,
                fc1_smooth_scale,
                fc2_smooth_scale,
                a16=True,
                activation=activation,
            )
            msg = f"[perf] a8w8 asm: {avg_b:>8.2f} vs a16w8 asm: {avg_b2:>8.2f} ......"
            checkAllclose(ref2, out_b2, atol=100, msg=msg)

        msg = f"[perf] {use_g1u1=} {token=}, quant={quantstr}, {model_dim=}, {inter_dim=}, {E=}, {shared_E=}, {topk=}, dtype: {dtype}, torch_avg: {avg_c:<8.2f} us, asm_avg: {avg_b:>8.2f} us ...... uplift: {avg_c/avg_b-1:.1%}"
        checkAllclose(ref2, out_b, rtol=0.01, atol=100, msg=msg)


parser = argparse.ArgumentParser(
    formatter_class=argparse.RawTextHelpFormatter,
    description="select test",
)
l_test = [
    "test_fmoe_16_bit",
    "g1u1_no_quant",
    "g1u1_int8quant",
    "g1u1_fp8quant",
    "g1u0_int8smoothquant",
    "g1u1_int8smoothquant",
    "g1u1_fp8smoothquant",
    "g1u1_int4",
]
parser.add_argument(
    "-t",
    "--test",
    type=str,
    choices=l_test,
    default=None,
    help="""Select test to run.
    e.g.: -t test_fmoe_16_bit""",
)
parser.add_argument(
    "-d",
    "--dtype",
    type=str,
    nargs="?",
    default=None,
    help="""Data type.
    e.g.: -d bf16""",
)
parser.add_argument(
    "-m",
    "--token",
    type=int,
    nargs="*",
    default=None,
    help="""Token Num.
    e.g.: -m 128""",
)
parser.add_argument(
    "-id",
    "--inter_dim",
    type=int,
    nargs="*",
    default=None,
    help="""Intermediate dim.
    e.g.: -id 4096""",
)
parser.add_argument(
    "-hd",
    "--hidden_dim",
    type=int,
    nargs="*",
    default=None,
    help="""Hidden states dim.
    e.g.: -hd 1024""",
)

args = parser.parse_args()
if args.test is not None:
    l_test = [args.test]
for test in l_test:
    print(f"\nRunning test: {test}")
    if test == "test_fmoe_16_bit":
        print("test test_fmoe 16 bit")
        print("\ng1u0 no quant")
        for dtype in (
            [dtypes.fp16, dtypes.bf16]
            if args.dtype is None
            else [dtypes.d_dtypes[args.dtype]]
        ):
            for m in [128, 256] if args.token is None else args.token:
                for dim in [4096, 8192] if args.inter_dim is None else args.inter_dim:
                    for hdim in [1024] if args.hidden_dim is None else args.hidden_dim:
                        # test_fmoe(dtype, m, dim, hdim, 32, 5)
                        test_fmoe(dtype, m, dim, hdim, 32, 5, quant="No")
    elif test == "g1u1_no_quant":
        for dtype in (
            [dtypes.fp16, dtypes.bf16]
            if args.dtype is None
            else [dtypes.d_dtypes[args.dtype]]
        ):
            for m in [128, 256] if args.token is None else args.token:
                for dim in [4096, 8192] if args.inter_dim is None else args.inter_dim:
                    for hdim in [1024] if args.hidden_dim is None else args.hidden_dim:
                        # test_fmoe(dtype, m, dim, hdim, 32, 5)
                        test_fmoe(dtype, m, dim, hdim, 32, 5, quant="No", use_g1u1=True)
    elif test == "g1u1_int8quant":
        for dtype in (
            [dtypes.bf16] if args.dtype is None else [dtypes.d_dtypes[args.dtype]]
        ):
            for m in [128, 256] if args.token is None else args.token:
                for dim in [4096, 8192] if args.inter_dim is None else args.inter_dim:
                    for hdim in [1024] if args.hidden_dim is None else args.hidden_dim:
                        test_fmoe(
                            dtype,
                            m,
                            dim,
                            hdim,
                            32,
                            5,
                            #   quant='int8quant', use_g1u1=True, shared_E=0, activation=ActivationType.Gelu)
                            quant="int8quant",
                            use_g1u1=True,
                        )

    elif test == "g1u1_fp8quant":
        for dtype in (
            [dtypes.bf16] if args.dtype is None else [dtypes.d_dtypes[args.dtype]]
        ):
            for m in [128, 256] if args.token is None else args.token:
                for dim in [4096, 8192] if args.inter_dim is None else args.inter_dim:
                    for hdim in [1024] if args.hidden_dim is None else args.hidden_dim:
                        test_fmoe(
                            dtype,
                            m,
                            dim,
                            hdim,
                            32,
                            5,
                            quant="fp8quant",
                            use_g1u1=True,
                            shared_E=0,
                            activation=ActivationType.Gelu,
                        )
                        #   quant='fp8quant', use_g1u1=True)

    elif test == "g1u0_int8smoothquant":
        for dtype in (
            [dtypes.bf16] if args.dtype is None else [dtypes.d_dtypes[args.dtype]]
        ):
            for m in [128] if args.token is None else args.token:
                for dim in (
                    [4096, 6144, 8192] if args.inter_dim is None else args.inter_dim
                ):
                    for hdim in (
                        [512, 1024] if args.hidden_dim is None else args.hidden_dim
                    ):
                        test_fmoe(
                            dtype,
                            m,
                            dim,
                            hdim,
                            32,
                            5,
                            quant="int8smoothquant",
                            use_g1u1=False,
                        )

    elif test == "g1u1_int8smoothquant":
        for dtype in (
            [dtypes.bf16] if args.dtype is None else [dtypes.d_dtypes[args.dtype]]
        ):
            for m in [128] if args.token is None else args.token:
                for dim in (
                    [4096, 6144, 8192] if args.inter_dim is None else args.inter_dim
                ):
                    for hdim in (
                        [512, 1024, 1280, 1536]
                        if args.hidden_dim is None
                        else args.hidden_dim
                    ):
                        test_fmoe(
                            dtype,
                            m,
                            dim,
                            hdim,
                            32,
                            5,
                            quant="int8smoothquant",
                            use_g1u1=True,
                        )

    elif test == "g1u1_fp8smoothquant":
        for dtype in (
            [dtypes.bf16] if args.dtype is None else [dtypes.d_dtypes[args.dtype]]
        ):
            for m in [128] if args.token is None else args.token:
                for dim in (
                    [4096, 6144, 8192] if args.inter_dim is None else args.inter_dim
                ):
                    for hdim in (
                        [512, 1024, 1280]
                        if args.hidden_dim is None
                        else args.hidden_dim
                    ):
                        test_fmoe(
                            dtype,
                            m,
                            dim,
                            hdim,
                            32,
                            5,
                            quant="fp8smoothquant",
                            use_g1u1=True,
                        )
    elif test == "g1u1_int4":
        for dtype in (
            [dtypes.bf16] if args.dtype is None else [dtypes.d_dtypes[args.dtype]]
        ):
            for m in [32, 128] if args.token is None else args.token:
                for dim in [4096, 6144] if args.inter_dim is None else args.inter_dim:
                    for hdim in (
                        [1024, 4096] if args.hidden_dim is None else args.hidden_dim
                    ):
                        test_fmoe(
                            dtype,
                            m,
                            dim,
                            hdim,
                            8,
                            3,
                            quant="wint4afp8smoothquant",
                            use_g1u1=True,
                        )
    else:
        raise ValueError(f"Unknown test: {test}")
