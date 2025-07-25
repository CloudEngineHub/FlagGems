import logging
import math
import os

import torch
import triton
import triton.language as tl

from flag_gems.runtime import device, torch_device_fn
from flag_gems.utils import libentry
from flag_gems.utils import triton_lang_extension as tle

logger = logging.getLogger(__name__)
device = device.name


@libentry()
@triton.jit(do_not_specialize=["n_elements", "part_num"])
def scan_part_sum_kernel(
    inp,
    out,
    partial_sum,
    n_elements,
    part_num,
    BLOCK_SIZE: tl.constexpr,
):
    pid = tle.program_id(0)
    offset = pid * BLOCK_SIZE + tl.arange(0, BLOCK_SIZE)
    mask = offset < n_elements

    inp_ptrs = inp + offset
    inp_vals = tl.load(inp_ptrs, mask=mask)
    if (
        tl.constexpr(inp_vals.dtype.is_int64())
        or tl.constexpr(inp_vals.dtype.is_uint64())
    ) or tl.constexpr(inp_vals.dtype.is_fp64()):
        inp_vals = inp_vals
    elif tl.constexpr(inp_vals.dtype.is_int()):
        inp_vals = inp_vals.to(tl.int32)
    else:
        inp_vals = inp_vals.to(tl.float32)
    result = tl.cumsum(inp_vals, axis=0)

    part_sum_via_sum = tl.sum(inp_vals)

    out_ptrs = out + offset
    tl.store(out_ptrs, result, mask=mask)

    partial_sum_ptrs = partial_sum + pid
    tl.store(partial_sum_ptrs, part_sum_via_sum)


@libentry()
@triton.jit(do_not_specialize=["n_elements", "part_num"])
def add_base_sum_kernel(
    out,
    partial_sum,
    n_elements,
    part_num,
    BLOCK_SIZE: tl.constexpr,
):
    pid = tle.program_id(0)
    offset = pid * BLOCK_SIZE + tl.arange(0, BLOCK_SIZE)
    mask = offset < n_elements

    out_ptrs = out + offset
    out_vals = tl.load(out_ptrs, mask=mask)

    if pid > 0:
        partial_sum_ptrs = partial_sum + pid - 1
        last_part_sum_via_sum = tl.load(partial_sum_ptrs)

        final_vals = out_vals + last_part_sum_via_sum
        tl.store(out_ptrs, final_vals.to(out_vals.dtype), mask=mask)


@libentry()
@triton.jit(do_not_specialize=["part_num"])
def scan_part_sum_abc_kernel(
    inp,
    out,
    partial_sum,
    B,
    C,
    part_num,
    BLOCK_SIZE: tl.constexpr,
):
    pid_a = tle.program_id(0)
    pid_b = tle.program_id(1)
    pid_c = tle.program_id(2)

    a_idx = pid_a
    b_idx = pid_b * BLOCK_SIZE + tl.arange(0, BLOCK_SIZE)
    c_idx = pid_c

    offset = a_idx * B * C + b_idx * C + c_idx
    base_part_offset = a_idx * part_num * C + c_idx
    part_offset = base_part_offset + pid_b * C

    mask = b_idx < B
    inp_ptrs = inp + offset
    inp_vals = tl.load(inp_ptrs, mask=mask)
    if (
        tl.constexpr(inp_vals.dtype.is_int64())
        or tl.constexpr(inp_vals.dtype.is_uint64())
    ) or tl.constexpr(inp_vals.dtype.is_fp64()):
        inp_vals = inp_vals
    elif tl.constexpr(inp_vals.dtype.is_int()):
        inp_vals = inp_vals.to(tl.int32)
    else:
        inp_vals = inp_vals.to(tl.float32)
    result = tl.cumsum(inp_vals, axis=0)

    part_sum_via_sum = tl.sum(inp_vals)

    offset = tl.where(mask, offset, -1)
    out_ptrs = out + offset
    tl.store(out_ptrs, result, mask=mask)

    partial_sum_ptrs = partial_sum + part_offset
    tl.store(partial_sum_ptrs, part_sum_via_sum)


@libentry()
@triton.jit(do_not_specialize=["part_num"])
def add_base_sum_abc_kernel(
    out,
    partial_sum,
    B,
    C,
    part_num,
    BLOCK_SIZE: tl.constexpr,
):
    pid_a = tle.program_id(0)
    pid_b = tle.program_id(1)
    pid_c = tle.program_id(2)

    a_idx = pid_a
    b_idx = pid_b * BLOCK_SIZE + tl.arange(0, BLOCK_SIZE)
    c_idx = pid_c

    base_offset = a_idx * B * C + c_idx
    offset = base_offset + b_idx * C
    base_part_offset = a_idx * part_num * C + c_idx
    last_part_offset = base_part_offset + (pid_b - 1) * C

    mask = b_idx < B
    out_ptrs = out + offset
    out_vals = tl.load(out_ptrs, mask=mask)

    if pid_b > 0:
        partial_sum_ptrs = partial_sum + last_part_offset
        last_part_sum_via_sum = tl.load(partial_sum_ptrs)

        final_vals = out_vals + last_part_sum_via_sum
        tl.store(out_ptrs, final_vals.to(out_vals.dtype), mask=mask)


def scan_then_fan_col(inp, out, n_ele, dtype):
    # TODO(all): tune on target board
    BLOCK_SIZE = 1024
    if n_ele <= 1024 * 4:
        BLOCK_SIZE = triton.next_power_of_2(n_ele)
    part_num = math.ceil(n_ele / BLOCK_SIZE)
    partial_sum = torch.empty(part_num, dtype=dtype, device=inp.device)

    grid = (part_num,)
    with torch_device_fn.device(inp.device):
        scan_part_sum_kernel[grid](inp, out, partial_sum, n_ele, part_num, BLOCK_SIZE)

    if part_num >= 2:
        scan_then_fan_col(partial_sum, partial_sum, part_num, dtype)
        with torch_device_fn.device(inp.device):
            add_base_sum_kernel[grid](out, partial_sum, n_ele, part_num, BLOCK_SIZE)


def scan_then_fan(inp, out, A, B, C, dtype):
    # TODO(all): tune on target board
    BLOCK_SIZE = 1024
    if B <= 1024 * 4:
        BLOCK_SIZE = triton.next_power_of_2(B)
    part_num = math.ceil(B / BLOCK_SIZE)
    partial_sum = torch.empty(A, part_num, C, dtype=dtype, device=inp.device)

    grid = (A, part_num, C)

    if inp.shape[1] > 8192:
        os.environ["TRITONXPU_OTHER_SIM"] = "1"
        os.environ["TRITONXPU_STORE_MASK_SIM"] = "1"
        scan_part_sum_abc_kernel[grid](
            inp, out, partial_sum, B, C, part_num, BLOCK_SIZE
        )

        if "TRITONXPU_OTHER_SIM" in os.environ:
            del os.environ["TRITONXPU_OTHER_SIM"]
        if "TRITONXPU_STORE_MASK_SIM" in os.environ:
            del os.environ["TRITONXPU_STORE_MASK_SIM"]

    else:
        with torch_device_fn.device(inp.device):
            scan_part_sum_abc_kernel[grid](
                inp, out, partial_sum, B, C, part_num, BLOCK_SIZE
            )

    if part_num >= 2:
        scan_then_fan(partial_sum, partial_sum, A, part_num, C, dtype)
        with torch_device_fn.device(inp.device):
            add_base_sum_abc_kernel[grid](out, partial_sum, B, C, part_num, BLOCK_SIZE)


def cumsum_wrapper(inp, dim=1, dtype=None, out=None):
    assert dim >= -inp.ndim and dim < inp.ndim, "Invalid dim"
    shape = inp.shape
    dim = dim % inp.ndim
    M = 1
    N = shape[dim]
    for i in range(dim):
        M *= shape[i]
    inp = inp.contiguous()
    K = inp.numel() // M // N

    if dtype is None:
        dtype = inp.dtype
        if dtype is torch.bool:
            dtype = torch.int64
    if out is None:
        out = torch.empty_like(inp, dtype=dtype)

    compute_dtype = out.dtype
    if inp.dtype == torch.float16 or inp.dtype == torch.bfloat16:
        compute_dtype = torch.float32

    if M == 1 and K == 1:
        scan_then_fan_col(inp, out, N, compute_dtype)
    else:
        scan_then_fan(inp, out, M, N, K, compute_dtype)
    return out


def cumsum(inp, dim=1, *, dtype=None):
    logger.debug("GEMS CUMSUM")
    return cumsum_wrapper(inp, dim, dtype)


def cumsum_out(inp, dim=1, *, dtype=None, out):
    logger.debug("GEMS CUMSUM_OUT")
    return cumsum_wrapper(inp, dim, dtype, out)


@libentry()
@triton.jit(do_not_specialize=["K"])
def normed_cumsum_kernel(inp, out, K, BLOCK: tl.constexpr):
    row_start = tle.program_id(0) * K
    row_off = tl.arange(0, BLOCK)
    x = tl.load(inp + row_start + row_off, mask=row_off < K, other=0)
    if x.dtype.is_fp16():
        x = x.to(tl.float32)
    y_sum = tl.sum(x, 0)
    y = tl.cumsum(x, 0)
    y = y / y_sum
    tl.store(out + row_start + row_off, y, mask=row_off < K)


@libentry()
@triton.jit(
    do_not_specialize=[
        "r",
        "t",
        "R",
        "K",
        "r_stride",
        "out_r_stride",
    ]
)
def block_cumsum_kernel(
    inp,
    out,
    sums,
    r: tl.constexpr,
    t: tl.constexpr,
    R: tl.constexpr,
    K: tl.constexpr,
    r_stride: tl.constexpr,
    k_stride: tl.constexpr,
    out_r_stride: tl.constexpr,
    out_k_stride: tl.constexpr,
    OUTPUT_SUMS: tl.constexpr,
    NORMALIZE: tl.constexpr,
    HAS_OUT_LAYOUT: tl.constexpr,
    TILE: tl.constexpr,
):
    # One CTA processes a (r, t*tile) chunk
    # rows = [ grid.y, grid.y + r )
    # cols = [ grid.x * t * tile, (grid.x + 1) * t * tile )
    gridx = tle.program_id(0).to(tl.int64)
    gridy = tle.program_id(1).to(tl.int64)
    n_chunks = tle.num_programs(0)

    for row in range(gridy * r, min((gridy + 1) * r, R)):
        curr_cumsum = tl.zeros((1,), tl.float32)
        row_offset = row * r_stride
        cols = gridx * t * TILE + tl.arange(0, TILE)
        for ti in range(0, t):
            cols_offset = cols * k_stride
            x = tl.load(inp + row_offset + cols_offset, mask=cols < K, other=0)
            if x.dtype.is_fp16() | x.dtype.is_bf16():
                x = x.to(tl.float32)
            tile_sum = tl.sum(x, 0)[None]
            tile_cumsum = tl.cumsum(x, 0) + curr_cumsum
            curr_cumsum += tile_sum
            if HAS_OUT_LAYOUT:
                cols_offset = cols * out_k_stride
                row_offset = row * out_r_stride
            tl.store(out + row_offset + cols_offset, tile_cumsum, mask=cols < K)
            if OUTPUT_SUMS:
                tl.store(sums + row * n_chunks + gridx[None], curr_cumsum)
            cols += TILE
        if NORMALIZE:
            cols = gridx * t * TILE + tl.arange(0, TILE)
            for _ in range(0, t):
                cols_offset = cols * k_stride
                if HAS_OUT_LAYOUT:
                    cols_offset = cols * out_k_stride
                    row_offset = row * out_r_stride
                x = tl.load(out + row_offset + cols_offset, mask=cols < K, other=0)
                if x.dtype.is_fp16() | x.dtype.is_bf16():
                    x = x.to(tl.float32)
                x = x / curr_cumsum
                tl.store(out + row_offset + cols_offset, x, mask=cols < K)
                cols += TILE


@libentry()
@triton.jit(
    do_not_specialize=[
        "r",
        "t",
        "R",
        "K",
        "r_stride",
        "out_r_stride",
    ]
)
def block_update_kernel(
    inp,
    base,
    rscale_ptr,
    out,
    r,
    t,
    R,
    K,
    r_stride,
    k_stride,
    out_r_stride,
    out_k_stride,
    rscale_stride,
    HAS_OUT_LAYOUT: tl.constexpr,
    TILE: tl.constexpr,
):
    # One CTA processes a (r, t*tile) chunk
    # rows = [ grid.y, grid.y + r )
    # cols = [ grid.x * t * tile, (grid.x + 1) * t * tile )
    gridx = tle.program_id(0).to(tl.int64)
    gridy = tle.program_id(1).to(tl.int64)
    n_gridx = tle.num_programs(1)

    base += gridy * n_gridx + gridx
    rscale_ptr += gridy * rscale_stride

    for row in range(gridy, min(gridy + r, R)):
        d = tl.load(base)
        rscale = tl.load(rscale_ptr)
        base += gridx
        rscale_ptr += rscale_stride
        row_offset = row * r_stride
        cols = gridx * t * TILE + tl.arange(0, TILE)
        for _ in range(0, t):
            cols_offset = cols * k_stride
            x = tl.load(inp + row_offset + cols_offset, mask=cols < K, other=0)
            x += d
            x /= rscale
            if HAS_OUT_LAYOUT:
                cols_offset = cols * out_k_stride
                row_offset = row * out_r_stride
            tl.store(out + row_offset + cols_offset, x, mask=cols < K)
            cols += TILE


GRID_Y_LIMIT = 65535


def normed_cumsum(inp, dim=-1):
    logger.debug("GEMS NORMED_CUMSUM")
    assert inp.dtype in (torch.float16, torch.bfloat16, torch.float32, torch.float64)
    dim = dim % inp.ndim
    N = inp.numel()
    K = inp.size(dim)
    # inp = inp.contiguous()
    # First and last dims are easier to handle, but transpose the middle dim to the last
    ranked_dims = sorted(range(inp.ndim), key=lambda i: inp.stride(i), reverse=True)
    is_mid_dim = dim not in (ranked_dims[0], ranked_dims[-1])
    if is_mid_dim:
        inp = inp.transpose(dim, -1).contiguous()
        dim = -1
    out = torch.empty_like(inp)
    with torch_device_fn.device(inp.device.index):
        # Pass one, scan a (batch, n_tiles * TILE) sized block within each cta
        num_sms = torch_device_fn.get_device_properties(device).multi_processor_count
        TILE = 8192
        # Each row is split into n_chunks of chunks where each chunk is compised of
        # n_tiles of tiles. Different chunks are assigned to different ctas.
        n_rows = N // K
        n_chunks = min(triton.cdiv(num_sms, n_rows), triton.cdiv(K, TILE))
        n_tiles = triton.cdiv(triton.cdiv(K, TILE), n_chunks)
        k_stride = inp.stride(dim)
        r_stride = inp.size(dim) if k_stride == 1 else 1
        if n_rows > GRID_Y_LIMIT:
            batch = triton.cdiv(n_rows, GRID_Y_LIMIT)
            n_batch = triton.cdiv(n_rows, batch)
        else:
            batch = 1
            n_batch = n_rows

        grid = (n_chunks, n_batch)
        if n_chunks == 1:
            block_cumsum_kernel[grid](
                inp,
                out,
                0,
                batch,
                n_tiles,
                n_rows,
                K,
                r_stride,
                k_stride,
                r_stride,
                k_stride,
                OUTPUT_SUMS=False,
                NORMALIZE=True,
                HAS_OUT_LAYOUT=False,
                TILE=TILE,
                isCloseUnrollControl=True,
            )
            return out

        if inp.dtype != torch.float64:
            acc_dtype = torch.float32
        sums = torch.empty((n_rows, n_chunks), dtype=acc_dtype, device=device.name)
        cumsums = torch.empty_like(sums)
        block_cumsum_kernel[grid](
            inp,
            out,
            sums,
            batch,
            n_tiles,
            n_rows,
            K,
            r_stride,
            k_stride,
            r_stride,
            k_stride,
            OUTPUT_SUMS=True,
            NORMALIZE=False,
            HAS_OUT_LAYOUT=False,
            TILE=TILE,
            isCloseUnrollControl=True,
        )
        # Pass two, scan partial cumsums
        block_cumsum_kernel[(1, n_batch)](
            sums,
            cumsums,
            0,
            batch,
            1,
            n_rows,
            n_chunks,
            n_chunks,
            1,
            n_chunks,
            1,
            OUTPUT_SUMS=False,
            NORMALIZE=False,
            HAS_OUT_LAYOUT=True,
            TILE=TILE,
            isCloseUnrollControl=True,
        )
        # print(sums)
        rscale = cumsums[..., -1]
        block_update_kernel[grid](
            out,
            cumsums - sums,
            rscale,
            out,
            batch,
            n_tiles,
            n_rows,
            K,
            r_stride,
            k_stride,
            r_stride,
            k_stride,
            n_chunks,
            HAS_OUT_LAYOUT=False,
            TILE=TILE,
        )
        return out
