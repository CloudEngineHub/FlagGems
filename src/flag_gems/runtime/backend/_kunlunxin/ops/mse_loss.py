import logging
from enum import Enum

import torch
import triton
import triton.language as tl

from flag_gems.runtime import torch_device_fn
from flag_gems.utils import libentry
from flag_gems.utils import triton_lang_extension as tle

from ..utils.pointwise_dynamic import pointwise_dynamic


@libentry()
@triton.jit
def kernel_1(
    inp, target, mid, M: tl.constexpr, BLOCK_SIZE: tl.constexpr, reduction: tl.constexpr
):
    pid = tle.program_id(0)
    offset = pid * BLOCK_SIZE + tl.arange(0, BLOCK_SIZE)
    inp_ptrs = inp + offset
    target_ptrs = target + offset
    mask = offset < M

    inp_val = tl.load(inp_ptrs, mask=mask, other=0).to(tl.float32)
    target_val = tl.load(target_ptrs, mask=mask, other=0).to(tl.float32)
    sub = inp_val - target_val
    pow_val = sub * sub
    # Reduction.MEAN.value: 1 Reduction.SUM.value: 2
    if reduction == 1:
        sum_val = tl.sum(pow_val) / M
    else:
        sum_val = tl.sum(pow_val)
    mid_ptr = mid + pid
    tl.store(mid_ptr, sum_val)


@libentry()
@triton.jit
def kernel_2(mid, out, mid_size: tl.constexpr, BLOCK_MID: tl.constexpr):
    offset = tl.arange(0, BLOCK_MID)
    mid_ptrs = mid + offset
    mask = offset < mid_size
    mid_val = tl.load(mid_ptrs, mask=mask, other=0).to(tl.float32)
    sum_val = tl.sum(mid_val)
    tl.store(out, sum_val)


@pointwise_dynamic(is_tensor=[True, True], promotion_methods=[(0, "DEFAULT")])
@triton.jit
def func(x, y):
    return (x - y) * (x - y)


class Reduction(Enum):
    NONE = 0
    MEAN = 1
    SUM = 2


def mse_loss(inp, target, reduction=Reduction.MEAN.value):
    # import pudb; pudb.set_trace()
    logging.debug("GEMS MSE LOSS")
    if reduction == Reduction.NONE.value:
        return func(inp, target)

    inp = inp.contiguous()
    target = target.contiguous()
    M = inp.numel()  # 119400
    dtype = inp.dtype

    # block_size = triton.next_power_of_2(math.ceil(math.sqrt(M)))
    # mid_size = triton.cdiv(M, block_size)
    # block_mid = triton.next_power_of_2(mid_size)
    # print(f'M = {M}')
    # block_size = 8192
    # mid_size = triton.cdiv(M, block_size) # 15
    # block_mid = triton.next_power_of_2(mid_size) # 16
    mid_size = 12
    block_size = triton.next_power_of_2(triton.cdiv(M, mid_size))  # 16384
    block_mid = mid_size  # 16

    mid = torch.zeros((mid_size,), dtype=dtype, device=inp.device)  # 12
    out = torch.zeros([], dtype=dtype, device=inp.device)  # 1

    # import os

    # os.environ["TRITONXPU_OTHER_SIM"] = "1"

    with torch_device_fn.device(inp.device):
        # print(f'old mid = {mid.cpu()}')
        # print(f'block_size = {block_size}')
        kernel_1[(mid_size, 1, 1)](inp, target, mid, M, block_size, reduction)
        # print(f'new mid = {mid.cpu()}')
        # print(f'mid_size = {mid_size}')
        # print(f'sum mid = {mid.cpu().sum()}')
        # assert(0)
        # print(f'old out = {out.cpu()}')
        # print(f'mid_size = {mid_size}')
        # print(f'block_mid = {block_mid}')
        kernel_2[(1, 1, 1)](mid, out, mid_size, block_mid)
        # print(f'new out = {out.cpu()}')

    # if "TRITONXPU_OTHER_SIM" in os.environ:
    #     del os.environ["TRITONXPU_OTHER_SIM"]

    return out
