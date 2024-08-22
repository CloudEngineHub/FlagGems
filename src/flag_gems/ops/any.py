import builtins
import logging
import math

import torch
import triton
import triton.language as tl

from ..utils import dim_compress, libentry


# torch.any: Tests if any elements in input evaluate to True. If the dtype of input
#            is not BOOL, then test if any elements in input evaluate to non-zero value
# In triton function, test if any elements in input evaluate to non-zero value is ok.
def cfggen():
    block_m = [1, 2, 4, 8]
    configs = [
        triton.Config({"BLOCK_M": m, "BLOCK_N": 1024}, num_warps=4) for m in block_m
    ]
    return configs


@triton.jit
def reduce_any(a, b):
    return a or b


@libentry()
@triton.autotune(configs=cfggen(), key=["M", "N"])
@triton.jit
def any_kernel_dim(
    inp,
    out,
    M,
    N,
    BLOCK_M: tl.constexpr,
    BLOCK_N: tl.constexpr,
):
    # Map the program id to the row of inp it should compute.
    pid = tl.program_id(0)
    rows = pid * BLOCK_M + tl.arange(0, BLOCK_M)[:, None]
    inp = inp + rows * N
    out = out + rows
    row_mask = rows < M

    _any = tl.zeros([BLOCK_M, BLOCK_N], dtype=tl.int1)
    for off in range(0, N, BLOCK_N):
        cols = off + tl.arange(0, BLOCK_N)[None, :]
        col_mask = cols < N
        mask = row_mask and col_mask

        a = tl.load(inp + cols, mask, other=0.0)
        _any = _any or (a != 0)
    any = tl.reduce(_any, axis=1, combine_fn=reduce_any)
    tl.store(out, any[:, None], row_mask)


@libentry()
@triton.jit
def any_kernel_1(
    inp,
    mid,
    n_elements,
    mid_size,
    BLOCK_SIZE: tl.constexpr,
):
    pid = tl.program_id(0)
    offset = pid * BLOCK_SIZE
    mask = offset < n_elements
    any_val = False
    for roffset in range(0, BLOCK_SIZE, 1):
        inp_ptrs = inp + offset + roffset
        inp_val = tl.load(inp_ptrs, mask=mask, other=1.0)
        float_mask = inp_val != 0.0
        any_val = any_val or float_mask
    mid_ptr = mid + pid
    tl.store(mid_ptr, any_val)


@libentry()
@triton.jit
def any_kernel_2(mid, out, MID_SIZE, BLOCK_MID: tl.constexpr):
    any_val = False
    for roffset in range(0, MID_SIZE, 1):
        mask = roffset < MID_SIZE
        mid_ptrs = mid + roffset
        mid_val = tl.load(mid_ptrs, mask=mask, other=1.0).to(tl.int1)
        any_val = any_val or mid_val
    tl.store(out, any_val)


def any(inp):
    logging.debug("GEMS ANY")
    n_elements = inp.numel()
    # block_size = triton.next_power_of_2(math.ceil(math.sqrt(n_elements)))
    # vvv
    mid_size = 12  # CLUSTER_NUM = 8
    block_size = triton.next_power_of_2(math.ceil(triton.cdiv(n_elements, mid_size)))
    block_mid = triton.next_power_of_2(mid_size)
    final_mid_size = builtins.min(
        math.ceil(inp.numel() / block_size), builtins.min(mid_size, inp.numel())
    )

    mid = torch.empty((mid_size,), dtype=torch.bool, device=inp.device)
    out = torch.empty([], dtype=torch.bool, device=inp.device)

    with torch.cuda.device(inp.device):
        any_kernel_1[(mid_size, 1)](inp, mid, n_elements, mid_size, block_size)
        any_kernel_2[(1, 1)](mid, out, final_mid_size, block_mid)

    return out


def any_dim(inp, dim=None, keepdim=False):
    logging.debug("GEMS ANY DIM")
    shape = list(inp.shape)
    if dim is None:
        out = any(inp)
        if keepdim:
            out = torch.reshape(out, [1] * inp.ndim)
    else:
        assert dim >= -inp.ndim and dim < inp.ndim, "Invalid dim"
        dim = dim % inp.ndim
        inp = dim_compress(inp, dim)
        N = shape[dim]
        shape[dim] = 1
        M = inp.numel() // N

        out = torch.empty(shape, dtype=torch.bool, device=inp.device)

        grid = lambda meta: (triton.cdiv(M, meta["BLOCK_M"]),)
        with torch.cuda.device(inp.device):
            any_kernel_dim[grid](inp, out, M, N)
        if not keepdim:
            out = out.squeeze(dim=dim)
    return out


def any_dims(inp, dim=None, keepdim=False):
    logging.debug("GEMS ANY DIMS")
    if dim is None or isinstance(dim, int):
        return any_dim(inp, dim=dim, keepdim=keepdim)
    assert ((i >= -inp.ndim and i < inp.ndim) for i in dim), "Invalid dim"

    shape = list(inp.shape)
    dim = [d % inp.ndim for d in dim]
    inp = dim_compress(inp, dim)
    N = 1
    for i in dim:
        N *= shape[i]
        shape[i] = 1
    M = inp.numel() // N

    out = torch.empty(shape, dtype=torch.bool, device=inp.device)

    grid = lambda meta: (triton.cdiv(M, meta["BLOCK_M"]),)
    with torch.cuda.device(inp.device):
        any_kernel_dim[grid](inp, out, M, N)
    if not keepdim:
        out = out.squeeze(dim=dim)
    return out
