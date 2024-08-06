import logging
import copy
import math

import torch
import triton
import triton.language as tl

from ..utils import libentry, TOTAL_CORE_NUM, MAX_NRAM_SIZE

def config_prune(configs, named_args, **kwargs):
    M = named_args["M"]
    N = named_args["N"]
    configs_map = {}
    pruned_configs = []
    n_per_core = math.ceil(N / TOTAL_CORE_NUM)
    for config in configs:
        kw = config.kwargs
        BLOCK_M, BLOCK_N, num_warps, num_stages = \
            kw['BLOCK_M'], kw['BLOCK_N'], config.num_warps, config.num_stages
        doopt = BLOCK_N * M * 4 * 3 < MAX_NRAM_SIZE
        if doopt:
            config = copy.deepcopy(config)
            BLOCK_M = config.kwargs["BLOCK_M"] = M
            num_stages = config.num_stages = 1
        key = (BLOCK_M, BLOCK_N, num_warps, num_stages)
        # Only keep one config for the same key
        configs_map.setdefault(key, config)
    pruned_configs = []
    for k, v in configs_map.items():
        pruned_configs.append(v)
    return pruned_configs

@libentry()
@triton.autotune(
    configs=[
        triton.Config({"BLOCK_M": m, "BLOCK_N": n}, num_stages=s, num_warps=1)
        for m in [64, 128, 256, 512, 1024]
        for n in [16, 32, 64, 128]
        for s in [1, 3]
    ],
    key=["M", "N"],
    prune_configs_by={'early_config_prune': config_prune},
)
@triton.heuristics(
    values={
        "ONE_TILE_PER_CTA": lambda args: args["M"] <= args["BLOCK_M"],
    }, )
@triton.jit
def mv_kernel(
    A,
    B,
    C,
    N,
    M,
    stride_an,
    stride_am,
    stride_bm,
    stride_cn,
    BLOCK_N: tl.constexpr,
    BLOCK_M: tl.constexpr,
    ONE_TILE_PER_CTA: tl.constexpr,
):
    pid = tl.program_id(0)
    offset_n = pid * BLOCK_N + tl.arange(0, BLOCK_N)[:, None]
    offset_m = tl.arange(0, BLOCK_M)[None, :]
    n_mask = offset_n < N
    A_ptrs = A + offset_n * stride_an + offset_m * stride_am
    B_ptrs = B + offset_m * stride_bm
    if ONE_TILE_PER_CTA:
        a = tl.load(A_ptrs, mask=n_mask, other=0.0).to(tl.float32)
        b = tl.load(B_ptrs).to(tl.float32)
        acc = tl.sum(a * b, axis=1)
        C_ptrs = C + offset_n * stride_cn
        tl.store(C_ptrs, acc[:, None], mask=n_mask)
    else:
        acc = tl.zeros((BLOCK_N, BLOCK_M), dtype=tl.float32)
        for m in range(0, M, BLOCK_M):
            m_mask = m + offset_m < M
            a = tl.load(A_ptrs, mask=n_mask & m_mask, other=0.0).to(tl.float32)
            b = tl.load(B_ptrs, mask=m_mask, other=0.0).to(tl.float32)
            acc += a * b
            A_ptrs += BLOCK_M * stride_am
            B_ptrs += BLOCK_M * stride_bm
        acc = tl.sum(acc, axis=1)
        C_ptrs = C + offset_n * stride_cn
        tl.store(C_ptrs, acc[:, None], mask=n_mask)


def mv(inp, vec):
    logging.debug("GEMS MV")
    assert inp.shape[1] == vec.shape[0], "incompatible dimensions"
    N, M = inp.shape
    out = torch.empty((N,), device=inp.device, dtype=inp.dtype)
    grid = lambda META: (triton.cdiv(N, META["BLOCK_N"]),)
    with torch.mlu.device(inp.device):
        mv_kernel[grid](
            inp,
            vec,
            out,
            N,
            M,
            inp.stride(0),
            inp.stride(1),
            vec.stride(0),
            out.stride(0),
        )
    return out
