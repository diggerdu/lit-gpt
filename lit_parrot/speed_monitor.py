# Copyright 2022 MosaicML Composer authors
# SPDX-License-Identifier: Apache-2.0
# Adapted for standalone use
from collections import deque
from contextlib import nullcontext
from typing import Deque, Optional

import torch
from lightning import Fabric
from torch.utils.flop_counter import FlopCounterMode

from lit_parrot import Parrot

GPU_AVAILABLE_FLOPS = {
    # source: https://resources.nvidia.com/en-us-tensor-core/nvidia-tensor-core-gpu-datasheet
    # nvidia publishes spec sheet with a 2x sparsity factor
    "h100-sxm": {
        "64-true": 67e12,
        "32-true": 67e12,
        "16-true": 1.979e15 / 2,
        "16-mixed": 1.979e15 / 2,
        "bf16-true": 1.979e15 / 2,
        "bf16-mixed": 1.979e15 / 2,
        "8-true": 3.958e15 / 2,
        "8-mixed": 3.958e15 / 2,
    },
    "h100-pcie": {
        "64-true": 51e12,
        "32-true": 51e12,
        "16-true": 1.513e15 / 2,
        "16-mixed": 1.513e15 / 2,
        "bf16-true": 1.513e15 / 2,
        "bf16-mixed": 1.513e15 / 2,
        "8-true": 3.026e15 / 2,
        "8-mixed": 3.026e15 / 2,
    },
    # source: https://www.nvidia.com/content/dam/en-zz/Solutions/Data-Center/a100/pdf/nvidia-a100-datasheet-us-nvidia-1758950-r4-web.pdf
    # sxm and pcie have same flop counts
    "a100": {
        "64-true": 19.5e12,
        "32-true": 19.5e12,
        "16-true": 312e12,
        "16-mixed": 312e12,
        "bf16-true": 312e12,
        "bf16-mixed": 312e12,
    },
    # source: https://www.nvidia.com/content/dam/en-zz/Solutions/Data-Center/a10/pdf/a10-datasheet.pdf
    "a10g": {"32-true": 31.2e12, "16-true": 125e12, "16-mixed": 125e12, "bf16-true": 125e12, "bf16-mixed": 125e12},
    # source: https://images.nvidia.com/content/technologies/volta/pdf/volta-v100-datasheet-update-us-1165301-r5.pdf
    "v100-sxm": {"64-true": 7.8e12, "32-true": 15.7e12, "16-true": 125e12, "16-mixed": 125e12},
    "v100-pcie": {"64-true": 7e12, "32-true": 14e12, "16-true": 112e12, "16-mixed": 112e12},
    "v100s-pcie": {"64-true": 8.2e12, "32-true": 16.4e12, "16-true": 130e12, "16-mixed": 130e12},
    # source: https://www.nvidia.com/content/dam/en-zz/Solutions/Data-Center/tesla-t4/t4-tensor-core-datasheet-951643.pdf
    # sxm and pcie have same flop counts
    "t4": {"32-true": 8.1e12, "16-true": 65e12, "16-mixed": 65e12, "8-true": 130e12, "int4": 260e12},
    # https://www.nvidia.com/content/dam/en-zz/Solutions/design-visualization/quadro-product-literature/quadro-rtx-5000-data-sheet-us-nvidia-704120-r4-web.pdf
    "quadro rtx 5000": {"32-true": 11.2e12, "16-true": 89.2e12, "16-mixed": 89.2e12},
}


def get_flops_available(device: torch.device, precision: str) -> Optional[float]:
    if device.type == "cuda":
        device_name = torch.cuda.get_device_name(device).lower()
        if "h100" in device_name and "hbm3" in device_name:
            device_name = "h100-sxm"
        elif "h100" in device_name and ("pcie" in device_name or "hbm2e" in device_name):
            device_name = "h100-pcie"
        elif "a100" in device_name:
            device_name = "a100"
        elif "a10g" in device_name:
            device_name = "a10g"
        elif "v100-sxm" in device_name:
            device_name = "v100-sxm"
        elif "v100-pcie" in device_name:
            device_name = "v100-pcie"
        elif "t4" in device_name:
            device_name = "t4"
        elif "quadro rtx 5000" in device_name:
            device_name = "quadro rtx 5000"
        else:
            device_name = None

        if device_name is not None:
            try:
                return int(GPU_AVAILABLE_FLOPS[device_name][precision])
            except KeyError:
                raise KeyError(
                    f"flop count not found for {device_name} with precision: {precision}; "
                    "MFU cannot be calculated and reported."
                )
    elif device.type == "xla":
        # TODO
        ...

    return None


class SpeedMonitor:
    """Logs the training throughput and utilization.

    +-------------------------------------+-----------------------------------------------------------+
    | Key                                 | Logged data                                               |
    +=====================================+===========================================================+
    |                                     | Rolling average (over `window_size` most recent           |
    | `throughput/batches_per_sec`        | batches) of the number of batches processed per second    |
    |                                     |                                                           |
    +-------------------------------------+-----------------------------------------------------------+
    |                                     | Rolling average (over `window_size` most recent           |
    | `throughput/samples_per_sec`        | batches) of the number of samples processed per second    |
    |                                     |                                                           |
    +-------------------------------------+-----------------------------------------------------------+
    |                                     | Rolling average (over `window_size` most recent           |
    | `throughput/tokens_per_sec`         | batches) of the number of tokens processed per second.    |
    |                                     | This may include padding depending on dataset             |
    +-------------------------------------+-----------------------------------------------------------+
    |                                     | Estimates flops by `flops_per_batch * batches_per_sec`    |
    | `throughput/flops_per_sec`          |                                                           |
    |                                     |                                                           |
    +-------------------------------------+-----------------------------------------------------------+
    | `throughput/device/batches_per_sec` | `throughput/batches_per_sec` divided by world size        |
    +-------------------------------------+-----------------------------------------------------------+
    | `throughput/device/samples_per_sec` | `throughput/samples_per_sec` divided by world size        |
    +-------------------------------------+-----------------------------------------------------------+
    |                                     | `throughput/tokens_per_sec` divided by world size. This   |
    | `throughput/device/tokens_per_sec`  | may include pad tokens depending on dataset               |
    |                                     |                                                           |
    +-------------------------------------+-----------------------------------------------------------+
    |                                     | `throughput/flops_per_sec` divided by world size. Only    |
    | `throughput/device/flops_per_sec`   | logged when model has attribute `flops_per_batch`         |
    |                                     |                                                           |
    +-------------------------------------+-----------------------------------------------------------+
    |                                     | `throughput/device/flops_per_sec` divided by world size.  |
    | `throughput/device/mfu`             |                                                           |
    |                                     |                                                           |
    +-------------------------------------+-----------------------------------------------------------+
    | `time/train`                        | Total elapsed training time                               |
    +-------------------------------------+-----------------------------------------------------------+
    | `time/val`                          | Total elapsed validation time                             |
    +-------------------------------------+-----------------------------------------------------------+
    | `time/total`                        | Total elapsed time (time/train + time/val)                |
    +-------------------------------------+-----------------------------------------------------------+

    Args:
        window_size (int, optional): Number of batches to use for a rolling average of throughput.
            Defaults to 100.
        time_unit (str, optional): Time unit to use for `time` logging. Can be one of
            'seconds', 'minutes', 'hours', or 'days'. Defaults to 'hours'.
    """

    def __init__(self, fabric: Fabric, window_size: int = 100, time_unit: str = "hours"):
        self.fabric = fabric
        # TODO: this will not work properly if a precision plugin is passed to Fabric
        self.flops_available = get_flops_available(fabric.device, fabric._connector._precision_input)

        # Track the batch num samples and wct to compute throughput over a window of batches
        self.history_samples: Deque[int] = deque(maxlen=window_size + 1)
        self.history_wct: Deque[float] = deque(maxlen=window_size + 1)
        self.history_lengths: Deque[int] = deque(maxlen=window_size + 1)
        self.history_flops: Deque[int] = deque(maxlen=window_size + 1)

        self.divider = 1
        if time_unit == "seconds":
            self.divider = 1
        elif time_unit == "minutes":
            self.divider = 60
        elif time_unit == "hours":
            self.divider = 60 * 60
        elif time_unit == "days":
            self.divider = 60 * 60 * 24
        else:
            raise ValueError(
                f'Invalid time_unit: {time_unit}. Must be one of "seconds", "minutes", "hours", or "days".'
            )

        # Keep track of time spent evaluating
        self.total_eval_wct = 0.0
        self.step = -1

    def on_train_batch_end(
        self,
        samples: int,  # total samples seen (per device)
        train_elapsed: float,  # total training time (seconds)
        world_size: int,
        flops_per_batch: Optional[int] = None,  # (per device)
        lengths: Optional[int] = None,  # total length of the samples seen (per device)
    ):
        self.step += 1
        step = self.step
        metrics = {}

        self.history_samples.append(samples)
        if lengths is not None:
            self.history_lengths.append(lengths)
            # if lengths are passed, there should be as many values as samples
            assert len(self.history_samples) == len(self.history_lengths)
        self.history_wct.append(train_elapsed)
        if len(self.history_wct) == self.history_wct.maxlen:
            elapsed_batches = len(self.history_samples) - 1
            elapsed_samples = self.history_samples[-1] - self.history_samples[0]
            elapsed_wct = self.history_wct[-1] - self.history_wct[0]
            samples_per_sec = elapsed_samples * world_size / elapsed_wct
            dev_samples_per_sec = elapsed_samples / elapsed_wct
            metrics.update(
                {
                    "throughput/batches_per_se": elapsed_batches * world_size / elapsed_wct,
                    "throughput/samples_per_sec": samples_per_sec,
                    "throughput/device/batches_per_sec": elapsed_batches / elapsed_wct,
                    "throughput/device/samples_per_sec": dev_samples_per_sec,
                }
            )
            # Assumes no padding.
            if lengths is not None:
                elapsed_lengths = int(self.history_lengths[-1]) - int(self.history_lengths[0])
                metrics.update(
                    {
                        "throughput/tokens_per_sec": samples_per_sec * elapsed_lengths,
                        "throughput/device/tokens_per_sec": dev_samples_per_sec * elapsed_lengths,
                    }
                )

        if flops_per_batch is not None:
            # sum of flops per batch across ranks
            self.history_flops.append(flops_per_batch * world_size)
        if len(self.history_flops) == self.history_flops.maxlen:
            elapsed_flops = sum(self.history_flops) - self.history_flops[0]
            elapsed_wct = self.history_wct[-1] - self.history_wct[0]
            flops_per_sec = elapsed_flops / elapsed_wct
            device_flops_per_sec = flops_per_sec / world_size
            metrics.update(
                {"throughput/flops_per_sec": flops_per_sec, "throughput/device/flops_per_sec": device_flops_per_sec}
            )
            if self.flops_available:
                metrics["throughput/device/mfu"] = device_flops_per_sec / self.flops_available

        metrics.update(
            {
                "time/train": train_elapsed / self.divider,
                "time/val": self.total_eval_wct / self.divider,
                "time/total": (train_elapsed + self.total_eval_wct) / self.divider,
                "samples": samples,
            }
        )

        self.fabric.log_dict(metrics, step)

    def eval_end(self, eval_elapsed: float):
        self.total_eval_wct += eval_elapsed  # seconds


def estimate_flops(model: Parrot) -> int:
    """Measures estimated FLOPs for MFU: https://arxiv.org/abs/2205.05198"""
    # using all parameters for this is a naive over estimation because not all model parameters actually contribute to
    # this FLOP computation (e.g. embedding, norm). For this reason, the result will be higher by a fixed percentage
    # (~10%) compared to the measured FLOPs, making those lower but more realistic.
    # For a proper estimate, this needs a more fine-grained calculation as in Appendix A of the paper.
    n_params = sum(p.numel() for p in model.parameters())
    # credit: https://github.com/mosaicml/examples/blob/release/v0.0.4/examples/llm/throughput/README.md#mfu-and-hfu
    flops_per_token = 2 * n_params
    flops_per_seq = flops_per_token * model.config.block_size
    attn_flops_per_seq = model.config.n_layer * 2 * 2 * (model.config.n_embd * (model.config.block_size**2))
    mult = 3 if model.training else 1
    total_flops = mult * (flops_per_seq + attn_flops_per_seq)
    return total_flops


def measure_flops(model: Parrot, x: torch.Tensor) -> int:
    """Measures real FLOPs for HFU"""
    flop_counter = FlopCounterMode(model, display=False)
    ctx = nullcontext() if model.training else torch.no_grad()
    with ctx, flop_counter:
        y = model(x)
        if model.training:
            y.sum().backward()
    return flop_counter.get_total_flops()
