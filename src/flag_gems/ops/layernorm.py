import logging
import math

import torch
import triton
import copy
import triton.language as tl

from ..utils import libentry, TOTAL_CORE_NUM
from .. import runtime
from ..runtime import torch_device_fn
from ..utils import triton_lang_extension as tle
from ..utils.type_utils import get_accumulator_dtype

MAX_C_MLU_LAYERNORM_FORWARD = 8192
MAX_C_MLU_LAYERNORM_BACKWARD = 5120


@libentry()
@triton.autotune(
    configs=runtime.get_triton_config("layer_norm_persistent"),
    key=["M", "N"],
)
@triton.jit(do_not_specialize=["eps"])
def layer_norm_kernel_middle_n(
    X,
    Y,
    W,
    B,
    Mean,  # pointer to the mean
    Rstd,  # pointer to the 1/std
    M,
    eps,
    N: tl.constexpr,
    BLOCK_ROW_SIZE: tl.constexpr,
):
    pid = tl.program_id(0)
    row_start = pid * BLOCK_ROW_SIZE
    num_jobs = tl.num_programs(axis=0)
    step = num_jobs * BLOCK_ROW_SIZE

    cols_n = tl.arange(0, N)
    X += cols_n[None, :]
    Y += cols_n[None, :]
    cols_off = tl.arange(0, N)[None, :]
    w = tl.load(W + cols_off)
    b = tl.load(B + cols_off)
    for row in range(row_start, M, step):
        row_off = row + tl.arange(0, BLOCK_ROW_SIZE)
        col_mask = cols_off < N
        mask = row_off[:, None] < M
        off = row_off[:, None] * N
        x = tl.load(X + off, mask, other=0.0).to(tl.float32)
       
        # TODO: Use the following code as a fallback once the optimization for trans is complete.
        # mean = tl.sum(x_v, axis=1) / N
        # var = tl.sum(x_v * x_v, axis=1) / N - (mean * mean)
        # mean_bc = mean[:, None]

        x_v = tl.view(x, (BLOCK_ROW_SIZE, N))
        x_trans = tl.trans(x_v)
        mean = tl.sum(x_trans, axis=0) / N
        mean_bc = mean[:, None]
        tl.store(Mean + row_off[:, None], mean_bc, mask)
        var = tl.sum(x_trans * x_trans, axis=0) / N - (mean * mean)
        var = var[:, None]
        rstd = 1 / tl.sqrt(var + eps)
        tl.store(Rstd + row_off[:, None], rstd, mask)
        x = x - mean_bc
        x_hat = x * rstd
        y = x_hat * w + b
        tl.store(Y + off, y, mask=mask)

def config_prune(configs, named_args, **kwargs):
    M = named_args["M"]
    pruned_configs = []
    for config in configs:
        BLOCK_M = config.kwargs["BLOCK_ROW_SIZE"]
        if (M >= 1024 and BLOCK_M >= 22) or (M < 1024 and BLOCK_M < 22):
            pruned_configs.append(config)
    return pruned_configs

def cfggen():
    configs = [
        triton.Config({"BLOCK_ROW_SIZE": 2}, num_warps=1, num_stages=1),
        triton.Config({"BLOCK_ROW_SIZE": 8}, num_warps=1, num_stages=1),
        triton.Config({"BLOCK_ROW_SIZE": 14}, num_warps=1, num_stages=1),
        triton.Config({"BLOCK_ROW_SIZE": 22}, num_warps=1, num_stages=1),
        triton.Config({"BLOCK_ROW_SIZE": 32}, num_warps=1, num_stages=1),
    ]
    return configs

@libentry()
@triton.autotune(configs=cfggen(), key=["M", "N"], prune_configs_by={'early_config_prune': config_prune})
@triton.jit(do_not_specialize=["eps"])
def layer_norm_kernel_non_inner(
    X,
    Y,
    W,
    B,
    Mean,  # pointer to the mean
    Rstd,  # pointer to the 1/std
    M,
    N,
    eps,
    BLOCK_ROW_SIZE: tl.constexpr,
    BLOCK_COL_SIZE: tl.constexpr
):
    # Map the program id to the row of X and Y it should compute.
    pid = tl.program_id(0)
    row = pid * BLOCK_ROW_SIZE + tl.arange(0, BLOCK_ROW_SIZE)[:, None]
    row_mask = row < M
    X += row * N
    Y += row * N
    # BLOCK_COL_SIZE = N

    # Compute mean
    _mean = tl.zeros([BLOCK_ROW_SIZE, BLOCK_COL_SIZE], dtype=tl.float32)
    # Compute variance
    _var = tl.zeros([BLOCK_ROW_SIZE, BLOCK_COL_SIZE], dtype=tl.float32)
    # for off in range(0, N, BLOCK_COL_SIZE):
    cols = tl.arange(0, BLOCK_COL_SIZE)[None, :]
    col_mask = cols < N
    mask = row_mask and col_mask
    a = tl.load(X + cols, mask, other=0.0).to(tl.float32)
    _mean += a
    _var += a * a
    mean = tl.sum(_mean, axis=1) / N
    mean_bc = mean[:, None]

    a = tl.where(col_mask, a - mean_bc, 0.0)
    # Write mean / rstd
    tl.store(Mean + row, mean_bc, row_mask)
    var = tl.sum(_var, axis=1) / N - (mean * mean)
    var = var[:, None]
    rstd = 1 / tl.sqrt(var + eps)
    x_hat = a * rstd
    tl.store(Rstd + row, rstd, row_mask)

    # Normalize and apply linear transformation
    w = tl.load(W + cols, col_mask)
    b = tl.load(B + cols, col_mask)
    y = x_hat * w + b
    # Write output
    tl.store(Y + cols, y, mask=mask)


@libentry()
@triton.autotune(configs=runtime.get_triton_config("layer_norm_loop"), key=["M", "N"], prune_configs_by={'early_config_prune': config_prune})
@triton.jit(do_not_specialize=["eps"])
def layer_norm_kernel_inner(
    X,
    Y,
    W,
    B,
    Mean,  # pointer to the mean
    Rstd,  # pointer to the 1/std
    M,
    eps,
    N: tl.constexpr,
    BLOCK_ROW_SIZE: tl.constexpr,
    BLOCK_COL_SIZE: tl.constexpr,
):
    # Map the program id to the row of X and Y it should compute.
    pid = tl.program_id(0)
    row = pid * BLOCK_ROW_SIZE + tl.arange(0, BLOCK_ROW_SIZE)[:, None]
    row_mask = row < M
    X += row * N
    Y += row * N

    # Compute mean
    _mean = tl.zeros([BLOCK_ROW_SIZE, BLOCK_COL_SIZE], dtype=tl.float32)
    # Compute variance
    _var = tl.zeros([BLOCK_ROW_SIZE, BLOCK_COL_SIZE], dtype=tl.float32)
    block_col_size = tl.arange(0, BLOCK_COL_SIZE)[None, :]
    for off in range(0, N, BLOCK_COL_SIZE):
        cols = off + block_col_size
        col_mask = cols < N
        mask = row_mask and col_mask
        a = tl.load(X + cols, mask, other=0.0).to(tl.float32)
        _mean += a
        _var += a * a

    mean = tl.sum(_mean, axis=1) / N
    mean_bc = mean[:, None]

    var = tl.sum(_var, axis=1) / N - (mean * mean)
    var = var[:, None]
    rstd = 1 / tl.sqrt(var + eps)
    # Write mean / rstd
    tl.store(Mean + row, mean_bc, row_mask)
    tl.store(Rstd + row, rstd, row_mask)

    # Normalize and apply linear transformation
    for off in range(0, N, BLOCK_COL_SIZE):
        cols = off + block_col_size
        col_mask = cols < N
        mask = row_mask and col_mask
        w = tl.load(W + cols, col_mask)
        b = tl.load(B + cols, col_mask)
        x = tl.load(X + cols, mask, other=0.0).to(tl.float32)
        x = tl.where(col_mask, x - mean_bc, 0.0)
        x_hat = x * rstd
        y = x_hat * w + b
        # Write output
        tl.store(Y + cols, y, mask=mask)

def cfggen_in_wb_bw():
    configs = [
        triton.Config({"BLOCK_ROW_SIZE": 1, "BLOCK_COL_SIZE": 4096}, num_warps=1, num_stages=5),
        triton.Config({"BLOCK_ROW_SIZE": 4, "BLOCK_COL_SIZE": 1024}, num_warps=1, num_stages=5),
        triton.Config({"BLOCK_ROW_SIZE": 4, "BLOCK_COL_SIZE": 2048}, num_warps=1, num_stages=5),
        triton.Config({"BLOCK_ROW_SIZE": 8, "BLOCK_COL_SIZE": 1024}, num_warps=1, num_stages=5),
        triton.Config({"BLOCK_ROW_SIZE": 22, "BLOCK_COL_SIZE": 512}, num_warps=1, num_stages=5),
        triton.Config({"BLOCK_ROW_SIZE": 32, "BLOCK_COL_SIZE": 256}, num_warps=1, num_stages=5),
    ]
    return configs

def prune_in_wb_config(configs, named_args, **kwargs):
    M = named_args["M"]
    pruned_configs = []
    for config in configs:
        BLOCK_M = config.kwargs["BLOCK_ROW_SIZE"]
        if (M // BLOCK_M < 1):
            continue
        pruned_configs.append(config)
    return pruned_configs

@libentry()
@triton.autotune(
    configs=cfggen_in_wb_bw(),
    prune_configs_by={'early_config_prune': prune_in_wb_config},
    key=["M", "N"]
)
@triton.jit
def input_backward_kernel(
    dY,
    X,
    W,
    Mean,
    Rstd,
    dX,
    M,
    N,
    BLOCK_ROW_SIZE: tl.constexpr,
    BLOCK_COL_SIZE: tl.constexpr,
):
    pid = tl.program_id(0)

    row_start = pid * BLOCK_ROW_SIZE
    num_jobs = tl.num_programs(axis=0)
    step = num_jobs * BLOCK_ROW_SIZE

    for row in range(row_start, M, step):
        row_off = row + tl.arange(0, BLOCK_ROW_SIZE)
        mean = tl.load(Mean + row_off, mask = row_off < M, other = 0.0)[:, None].to(tl.float32)
        rstd = tl.load(Rstd + row_off, mask = row_off < M, other = 0.0)[:, None].to(tl.float32)

        row_mask = row_off[:, None] < M
        off = row_off[:, None] * N
        new_dY = dY + off
        new_X = X + off
        new_DX = dX + off

        dx_part2 = tl.zeros([BLOCK_ROW_SIZE, BLOCK_COL_SIZE], dtype=tl.float32)
        dx_part3 = tl.zeros([BLOCK_ROW_SIZE, BLOCK_COL_SIZE], dtype=tl.float32)

        for off in range(0, N, BLOCK_COL_SIZE):
            cols = off + tl.arange(0, BLOCK_COL_SIZE)
            col_mask = cols[None, :] < N
            mask = row_mask and col_mask
            dy = tl.load(new_dY + cols[None, :], mask, other = 0.0).to(tl.float32)
            x = tl.load(new_X + cols[None, :], mask, other = 0.0).to(tl.float32)
            x_hat = (x - mean) * rstd
            w = tl.load(W + cols, mask=cols < N).to(tl.float32)
            wdy = dy * w
            dx_part2 += wdy
            dx_part3 += wdy * x_hat

        dx_part2_trans = tl.trans(dx_part2)
        dx_2 = tl.sum(dx_part2_trans, axis=0)[:, None]
        dx_part3_trans = tl.trans(dx_part3)
        dx_3 = tl.sum(dx_part3_trans, axis=0)[:, None]

        for off in range(0, N, BLOCK_COL_SIZE):
            cols = off + tl.arange(0, BLOCK_COL_SIZE)
            col_mask = cols[None, :] < N
            mask = row_mask and col_mask
            dy = tl.load(new_dY + cols[None, :], mask, other = 0.0).to(tl.float32)
            x = tl.load(new_X + cols[None, :], mask, other = 0.0).to(tl.float32)
            w = tl.load(W + cols, mask=cols < N, other = 0.0).to(tl.float32)
            x_hat = (x - mean) * rstd
            wdy = dy * w
            dx = rstd * (wdy - (dx_2 + x_hat * dx_3) / N)
            tl.store(new_DX + cols, dx.to(x.dtype), mask=mask)


@libentry()
@triton.autotune(
    configs=cfggen_in_wb_bw(),
    prune_configs_by={'early_config_prune': prune_in_wb_config},
    key=["M", "N"]
)
@triton.jit
def weight_bias_backward_kernel(
    dY,
    X,
    Mean,
    Rstd,
    dW,
    dB,
    M,
    N,
    BLOCK_ROW_SIZE: tl.constexpr,
    BLOCK_COL_SIZE: tl.constexpr,
):

    pid = tl.program_id(0)

    col_start = pid * BLOCK_COL_SIZE
    num_jobs = tl.num_programs(axis=0)
    step = num_jobs * BLOCK_COL_SIZE

    for col in range(col_start, N, step):
        col_off = col + tl.arange(0, BLOCK_COL_SIZE)[None, :]
        col_mask = col_off < N

        new_dY = dY + col_off
        new_X = X + col_off
        new_dW = dW + col_off
        new_dB = dB + col_off

        accW = tl.zeros([BLOCK_ROW_SIZE, BLOCK_COL_SIZE], dtype=tl.float32)
        accB = tl.zeros([BLOCK_ROW_SIZE, BLOCK_COL_SIZE], dtype=tl.float32)

        for off in range(0, M, BLOCK_ROW_SIZE):
            rows = off + tl.arange(0, BLOCK_ROW_SIZE)
            row_mask = rows[:, None] < M
            mask = row_mask and col_mask
            dy = tl.load(new_dY + rows[:, None] * N, mask, other = 0.0).to(tl.float32)
            x = tl.load(new_X + rows[:, None] * N, mask, other = 0.0).to(tl.float32)
            mean = tl.load(Mean + rows, mask = rows < M, other = 0.0)[:, None].to(tl.float32)
            rstd = tl.load(Rstd + rows, mask = rows < M, other = 0.0)[:, None].to(tl.float32)
            x_hat = (x - mean) * rstd
            accW += dy * x_hat
            accB += dy
        dw = tl.sum(accW, axis=0)
        db = tl.sum(accB, axis=0)
        tl.store(new_dW, dw[None, :], mask=col_mask)
        tl.store(new_dB, db[None, :], mask=col_mask)

def cfggen_bw_middle_n():
    block_m = [1, 2, 4, 8, 12, 18, 22, 32]

    warps = [1]
    num_stages = [1, 3]
    configs = [
        triton.Config({
            "BLOCK_ROW_SIZE": m,
        },
                      num_warps=w,
                      num_stages=s) 
        for m in block_m
        for w in warps for s in num_stages
    ]
    return configs
@libentry()
@triton.autotune(
      configs=cfggen_bw_middle_n(),
      key=["M", "N"],
      reset_to_zero=["DW", "DB"]
)
@triton.jit
def layer_norm_backward_kernel_middle_n(
        DX,  # pointer to the input gradient
        DY,  # pointer to the output gradient
        DW,  # pointer to the partial sum of weights gradient
        DB,  # pointer to the partial sum of biases gradient
        X,  # pointer to the input
        W,  # pointer to the weights
        Mean,  # pointer to the mean
        Rstd,  # pointer to the 1/std
        M,  # number of rows in X
        N: tl.constexpr,  # number of columns in X
        BLOCK_ROW_SIZE: tl.constexpr,
):
    pid = tl.program_id(0)

    row_start = pid * BLOCK_ROW_SIZE
    cols = tl.arange(0, N)
    num_jobs = tl.num_programs(axis=0)
    step = num_jobs * BLOCK_ROW_SIZE

    X += cols[None, :]
    DY += cols[None, :]
    W += cols[None, :]
    DX += cols[None, :]
    w = tl.load(W).to(tl.float32)

    partial_dw = tl.zeros([BLOCK_ROW_SIZE, N], dtype=tl.float32)
    partial_db = tl.zeros([BLOCK_ROW_SIZE, N], dtype=tl.float32)
    for row in range(row_start, M, step):
        row_off = row + tl.arange(0, BLOCK_ROW_SIZE)
        mask = row_off[:, None] < M
        # Load data to SRAM
        off = row_off[:, None] * N
        x = tl.load(X + off, mask, other=0.0).to(tl.float32)
        dy = tl.load(DY + off, mask, other=0.0).to(tl.float32)
        mean = tl.load(Mean + row_off, mask = row_off < M)[:, None].to(tl.float32)
        rstd = tl.load(Rstd + row_off, mask = row_off < M)[:, None].to(tl.float32)
        # Compute dx
        x_hat = (x - mean) * rstd
        wdy = w * dy
        x_hat_dy = x_hat * wdy
        x_hat_dy = tl.view(x_hat_dy, (BLOCK_ROW_SIZE, N))
        x_hat_dy_trans = tl.trans(x_hat_dy)
        c1 = tl.sum(x_hat_dy_trans, axis=0)[:, None]

        wdy_v = tl.view(wdy, (BLOCK_ROW_SIZE, N))
        wdy_v_trans = tl.trans(wdy_v)
        c2 = tl.sum(wdy_v_trans, axis=0)[:, None]
        dx = (wdy - (x_hat * c1 + c2) / N) * rstd
        # Accumulate partial sums for dw/db
        partial_dw += (dy * x_hat).to(tl.float32)
        partial_db += (dy).to(tl.float32)
        # Write dx
        tl.store(DX + off, dx.to(x.dtype), mask=mask)

    dw = tl.sum(partial_dw, axis=0)
    db = tl.sum(partial_db, axis=0)
    tl.atomic_add(DW + cols, dw)
    tl.atomic_add(DB + cols, db)

class LayerNorm(torch.autograd.Function):

    @staticmethod
    def forward(ctx,
                x,
                normalized_shape,
                weight,
                bias,
                eps=1e-5,
                cudnn_enable=True):
        logging.debug("GEMS LAYERNORM FORWARD")
        # dim = x.ndim - len(normalized_shape)
        # M = math.prod(x.shape[:dim])
        N = math.prod(normalized_shape)
        M = x.numel() // N
        x = x.contiguous()
        weight = weight.contiguous()
        bias = bias.contiguous()
        y = torch.empty_like(x)
        acc_type = get_accumulator_dtype(x.dtype)
        mean = torch.empty(M, dtype=acc_type, device=x.device)
        rstd = torch.empty(M, dtype=acc_type, device=x.device)
        if N <= MAX_C_MLU_LAYERNORM_FORWARD:
            grid = lambda META: (min(triton.cdiv(M, META["BLOCK_ROW_SIZE"]), TOTAL_CORE_NUM),)
            with torch_device_fn.device(x.device):
                layer_norm_kernel_middle_n[grid](x, y, weight, bias, mean, rstd, M, eps, N)
        else:
            grid = lambda META: (triton.cdiv(M, META["BLOCK_ROW_SIZE"]),)
            with torch_device_fn.device(x.device):
                layer_norm_kernel_inner[grid](x, y, weight, bias, mean, rstd, M, eps, N)
        ctx.save_for_backward(x, weight, mean, rstd)
        ctx.M = M
        ctx.N = N
        return y, mean, rstd

    @staticmethod
    def backward(ctx, out_grad, mean_grad, rstd_grad):
        logging.debug("GEMS LAYERNORM BACKWARD")
        out_grad = out_grad.contiguous()
        x, weight, mean, rstd = ctx.saved_tensors
        M, N = ctx.M, ctx.N
        if N <= MAX_C_MLU_LAYERNORM_BACKWARD:
            in_grad = torch.empty_like(out_grad)
            weight_grad = torch.zeros((weight.shape[0],), dtype=torch.float, device=weight.device)
            bias_grad = torch.zeros((weight.shape[0],), dtype=torch.float, device=weight.device)
            # enqueue kernel using forward pass heuristics
            # also compute partial sums for DW and DB
            grid = lambda META: (min(triton.cdiv(M, META['BLOCK_ROW_SIZE']), TOTAL_CORE_NUM),)
            with torch_device_fn.device(x.device):
                layer_norm_backward_kernel_middle_n[grid](
                    in_grad,
                    out_grad,
                    weight_grad,
                    bias_grad,
                    x,
                    weight,
                    mean,
                    rstd,
                    M=M,
                    N=N
                )
            weight_grad = weight_grad.to(x.dtype)
            bias_grad = bias_grad.to(x.dtype)
        else:
            in_grad = torch.empty_like(x)
            grid = lambda META: (min(triton.cdiv(M, META['BLOCK_ROW_SIZE']), TOTAL_CORE_NUM),)
            input_backward_kernel[grid](
                out_grad,
                x,
                weight,
                mean,
                rstd,
                in_grad,
                M,
                N,
            )
            weight_grad = torch.empty_like(weight)
            bias_grad = torch.empty_like(weight)
            grid = lambda META: (min(triton.cdiv(N, META['BLOCK_COL_SIZE']), TOTAL_CORE_NUM),)
            with torch.cuda.device(x.device):
                weight_bias_backward_kernel[grid](
                    out_grad,
                    x,
                    mean,
                    rstd,
                    weight_grad,
                    bias_grad,
                    M,
                    N,
                )

        return in_grad, None, weight_grad, bias_grad, None, None


def layer_norm(x, normalized_shape, weight, bias, eps=1e-5, cudnn_enable=True):
    return LayerNorm.apply(x, normalized_shape, weight, bias, eps,
                           cudnn_enable)
