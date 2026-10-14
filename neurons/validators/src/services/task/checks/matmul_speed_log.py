"""Shadow log of the speed the capability matmul measured, next to the GPU model the node reports.

The GPU model is a name the node reports about itself. This module writes one line per validation
that ran the matmul, with the reported model and what the run measured, so measured speed can be
read per reported model across the fleet. It is a log only: nothing reads it back, and no verdict,
score or published spec depends on it.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

from core.config import settings
from core.utils import _m, get_extra_info

if TYPE_CHECKING:
    from services.matrix_validation_service import ValidationResult

logger = logging.getLogger(__name__)

MATMUL_SPEED_SHADOW_EVENT = "MATMUL_SPEED_SHADOW"

# The capability run does not select a device, so CUDA's default device runs it: index 0.
MATMUL_MEASURED_DEVICE_INDEX = 0

# The challenge holds two dim_n x dim_k matrices of 8-byte values (`get_max_matrix_dimensions`).
_MATRIX_COUNT = 2
_ELEMENT_BYTES = 8
# Strings from the node are cut to this length before they are logged.
_TEXT_MAX_CHARS = 128


def _number(value: Any) -> float | int | None:
    """A finite, non-negative number, else None. The metrics are the node's when unsealed."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:_TEXT_MAX_CHARS]


def _reported_gpu(machine_spec: Any) -> tuple[str | None, int | None]:
    gpu = machine_spec.get("gpu") if isinstance(machine_spec, dict) else None
    if not isinstance(gpu, dict):
        return None, None
    count = gpu.get("count")
    count = count if isinstance(count, int) and not isinstance(count, bool) else None
    details = gpu.get("details")
    first = details[0] if isinstance(details, list) and details else None
    name = first.get("name") if isinstance(first, dict) else None
    return _text(name), count


def computed_matmul_tflops(dim_n: Any, dim_k: Any, elapsed_seconds: Any) -> float | None:
    """Throughput computed on the validator from the challenge size and the elapsed time.

    It counts one (dim_n x dim_k) by (dim_k x dim_n) product, 2 * dim_n^2 * dim_k operations, over
    the whole elapsed time. That time also holds the SSH round trip, process start, allocation and
    the node's own benchmark, so the figure is far below the card's real throughput. It is a rough
    lower bound to compare runs of the same size, not a benchmark.
    """
    n, k, elapsed = _number(dim_n), _number(dim_k), _number(elapsed_seconds)
    if not n or not k or not elapsed:
        return None
    return round(2.0 * n * n * k / elapsed / 1e12, 4)


def build_matmul_speed_record(
    machine_spec: Any, result: ValidationResult, *, transport: str
) -> dict[str, Any]:
    """The fields of the speed line. A value the run did not give is None."""
    model, count = _reported_gpu(machine_spec)
    metrics = result.metrics if isinstance(result.metrics, dict) else {}
    dim_n, dim_k = _number(result.dim_n), _number(result.dim_k)
    elapsed = _number(result.elapsed_seconds)
    matrix_bytes = int(_MATRIX_COUNT * _ELEMENT_BYTES * dim_n * dim_k) if dim_n and dim_k else None
    return {
        "reported_gpu_model": model,
        "reported_gpu_count": count,
        "device_index": MATMUL_MEASURED_DEVICE_INDEX,
        "transport": transport,
        "success": bool(result.success),
        "timed_out": bool(result.timed_out),
        # True: the metrics below came out of the authenticated sealed blob. False: they are the
        # executor's plaintext, which its own wrapper can edit. None: no answer was judged.
        "sealed": result.sealed,
        "elapsed_seconds": round(elapsed, 3) if elapsed is not None else None,
        "elapsed_source": result.elapsed_source,
        "dim_n": dim_n,
        "dim_k": dim_k,
        "matrix_bytes": matrix_bytes,
        # Reported by the node's verifier library (sealed or not, see `sealed`).
        "node_metrics_present": bool(metrics),
        "node_fp32_tflops": _number(metrics.get("fp32_tflops")),
        "node_fp32_median_tflops": _number(metrics.get("fp32_median_tflops")),
        "node_fp32_cov_pct": _number(metrics.get("fp32_cov_pct")),
        "node_benchmark_n": _number(metrics.get("n")),
        "node_device_name": _text(metrics.get("device")),
        "node_compute_capability": _text(metrics.get("cc")),
        "node_sm_count": _number(metrics.get("sm_count")),
        "node_clock_mhz": _number(metrics.get("clock_mhz")),
        "node_vram_gb": _number(metrics.get("vram_gb")),
        # Computed here, not reported by the node; only for a run that answered correctly.
        "computed_matmul_tflops": (
            computed_matmul_tflops(dim_n, dim_k, elapsed) if result.success else None
        ),
    }


def log_matmul_speed(
    machine_spec: Any,
    result: ValidationResult | None,
    *,
    transport: str,
    default_extra: dict | None = None,
) -> None:
    """Write the speed line for one matmul run. Never raises and never changes the result."""
    if result is None or not settings.MATMUL_SPEED_SHADOW_LOG_ENABLED:
        return
    try:
        record = build_matmul_speed_record(machine_spec, result, transport=transport)
        logger.info(
            _m(MATMUL_SPEED_SHADOW_EVENT, extra=get_extra_info({**(default_extra or {}), **record}))
        )
    except Exception as exc:  # noqa: BLE001 - a log line must not fail a validation
        logger.warning("%s not written: %s", MATMUL_SPEED_SHADOW_EVENT, exc)
