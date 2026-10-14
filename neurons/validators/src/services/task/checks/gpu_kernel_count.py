from __future__ import annotations

import re
from typing import Any

from core.config import settings
from services.nvidia_devices import read_kernel_gpu_view

from ..messages import GpuKernelCountMessages as Msg
from ..messages import MessageTemplate, render_message
from ..pipeline import CheckResult, Context
from .banned_provider import kernel_gpu_view, reported_gpu_uuids
from .cpu_truth import advertised_cpu_count

# A reported GPU UUID shaped like a random (RFC 4122 version 4) UUID: version nibble 4 and variant
# nibble 8, 9, a or b. A driver-issued UUID lands on that shape about once in 64, so one such card
# on a node says nothing; this many on one node is logged.
GPU_UUID_V4_SHAPE_RE = re.compile(
    r"^(?:gpu-)?[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
GPU_UUID_V4_SHAPE_FLAG_MIN = 2
# Host-shape hint: a multi-GPU host with less CPU or RAM per reported card than this is logged.
# Deliberately low floors; a host below them is unusual, not proven wrong.
HOST_SHAPE_MIN_GPU_COUNT = 2
HOST_SHAPE_MIN_VCPU_PER_GPU = 2.0
HOST_SHAPE_MIN_RAM_GB_PER_GPU = 8.0
# specs.ram.total is in kB (machine_scrape: psutil total / 1024)
KB_PER_GB = 1024 * 1024

SHADOW_ONLY_IMPACT = "Shadow observation only: the validation result was NOT changed"
KERNEL_VIEW_SOURCE_CONTAINER = "container"
KERNEL_VIEW_SOURCE_HOST = "host_pid1_root"


def _normalized(uuids: list[str] | None) -> list[str]:
    seen: dict[str, None] = {}
    for gpu_uuid in uuids or []:
        key = str(gpu_uuid).strip().lower()
        if key:
            seen.setdefault(key)
    return list(seen)


def _as_count(value: Any) -> int | None:
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def reported_gpu_count(ctx: Context, reported_uuids: list[str]) -> int:
    """The GPU count the node claims: the scraped count, or the number of cards it listed if higher."""
    count = _as_count(ctx.state.gpu_count)
    if count is None:
        count = _as_count(((ctx.state.specs or {}).get("gpu") or {}).get("count")) or 0
    return max(count, len(reported_uuids))


def count_v4_shaped_uuids(uuids: list[str]) -> int:
    return sum(1 for gpu_uuid in _normalized(uuids) if GPU_UUID_V4_SHAPE_RE.fullmatch(gpu_uuid))


def reported_ram_gb(specs: dict[str, Any] | None) -> float | None:
    ram = (specs or {}).get("ram")
    if not isinstance(ram, dict):
        return None
    try:
        total_kb = float(ram.get("total"))
    except (TypeError, ValueError):
        return None
    return total_kb / KB_PER_GB if total_kb > 0 else None


def host_shape_hint(
    *, gpu_count: int, cpu_count: int | None, ram_gb: float | None
) -> dict[str, Any]:
    """CPU and RAM per reported card, and which of the two floors (if any) the host is below."""
    reasons: list[str] = []
    vcpu_per_gpu = ram_gb_per_gpu = None
    if gpu_count >= HOST_SHAPE_MIN_GPU_COUNT:
        if cpu_count is not None:
            vcpu_per_gpu = round(cpu_count / gpu_count, 2)
            if vcpu_per_gpu < HOST_SHAPE_MIN_VCPU_PER_GPU:
                reasons.append("vcpu_per_gpu_below_floor")
        if ram_gb is not None:
            ram_gb_per_gpu = round(ram_gb / gpu_count, 2)
            if ram_gb_per_gpu < HOST_SHAPE_MIN_RAM_GB_PER_GPU:
                reasons.append("ram_gb_per_gpu_below_floor")
    return {
        "cpu_count": cpu_count,
        "ram_gb": round(ram_gb, 1) if ram_gb is not None else None,
        "vcpu_per_gpu": vcpu_per_gpu,
        "ram_gb_per_gpu": ram_gb_per_gpu,
        "min_vcpu_per_gpu": HOST_SHAPE_MIN_VCPU_PER_GPU,
        "min_ram_gb_per_gpu": HOST_SHAPE_MIN_RAM_GB_PER_GPU,
        "host_shape_reasons": reasons,
        "host_shape_flag": bool(reasons),
    }


def evaluate_kernel_view(
    *, reported_count: int, reported_uuids: list[str], kernel_uuids: list[str] | None
) -> dict[str, Any]:
    """Compare the reported cards with the kernel's list. `kernel_verdict` is one of
    not_evaluated, match, count_lower, uuid_mismatch."""
    reported = _normalized(reported_uuids)
    kernel = _normalized(kernel_uuids)
    result: dict[str, Any] = {
        "reported_gpu_count": reported_count,
        "reported_uuid_count": len(reported),
        "kernel_gpu_count": len(kernel) if kernel else None,
        "reported_uuids_not_in_kernel": [],
        "kernel_uuids_not_reported": [],
    }
    if not kernel:
        return {**result, "kernel_verdict": "not_evaluated"}
    kernel_set, reported_set = set(kernel), set(reported)
    result["reported_uuids_not_in_kernel"] = [u for u in reported if u not in kernel_set]
    result["kernel_uuids_not_reported"] = [u for u in kernel if u not in reported_set]
    if len(kernel) < reported_count:
        verdict = "count_lower"
    elif result["reported_uuids_not_in_kernel"]:
        verdict = "uuid_mismatch"
    else:
        verdict = "match"
    return {**result, "kernel_verdict": verdict}


async def host_view_verdict(
    ctx: Context, *, reported_count: int, reported_uuids: list[str]
) -> dict[str, Any]:
    """Second reading, taken only when the container's list is shorter than the reported count.

    A container that was handed a subset of the host's cards has only those under its own /proc,
    while the scrape can still list every card. The host's list, read through PID 1's root, is
    the full one. It is used only when it is the host's real procfs (read_kernel_gpu_view's
    rules) and lists at least the reported number of cards. A host that itself has fewer cards
    than reported is short there too, and the first verdict stands. Never raises.
    """
    seen: dict[str, Any] = {"host_kernel_gpu_count": None, "host_kernel_gpu_foreign_mounts": []}
    if ctx.ssh is None:
        return seen
    try:
        host_uuids, host_foreign_mounts = await read_kernel_gpu_view(ctx.ssh, host_view=True)
    except Exception as exc:
        return {**seen, "host_kernel_view_error": repr(exc)[:300]}
    seen["host_kernel_gpu_foreign_mounts"] = host_foreign_mounts
    if not host_uuids:
        return seen
    judged = evaluate_kernel_view(
        reported_count=reported_count, reported_uuids=reported_uuids, kernel_uuids=host_uuids
    )
    seen["host_kernel_gpu_count"] = judged["kernel_gpu_count"]
    if judged["kernel_verdict"] not in ("match", "uuid_mismatch"):
        return seen
    return {
        **seen,
        "kernel_verdict": "match_host_view"
        if judged["kernel_verdict"] == "match"
        else "uuid_mismatch",
        "kernel_view_source": KERNEL_VIEW_SOURCE_HOST,
        "host_kernel_gpu_uuids": host_uuids,
        "reported_uuids_not_in_host_kernel": judged["reported_uuids_not_in_kernel"],
    }


class GpuKernelCountCheck:
    """Cross-check the reported GPU list against the kernel's view of the cards.

    The reported count and UUIDs come from the scrape (NVML); the kernel list is
    /proc/driver/nvidia/gpus as BannedProviderCheck reads it once per cycle. A node whose kernel
    list is present and shorter than its reported GPU count gets GPU_KERNEL_COUNT_MISMATCH. That
    verdict fails the validation only under GPU_KERNEL_COUNT_ENFORCEMENT_ENABLED; without it the
    check always passes and the verdict is only logged.

    The container the check reads in may have been handed only some of the host's cards. So a
    short list is checked once more against the host's own list (host_view_verdict): if that one
    covers the reported cards the verdict is match_host_view and nothing fails.

    A kernel list that is missing (unreadable, empty, read failed, withheld behind a foreign
    mount) is never a failure: the verdict is GPU_KERNEL_COUNT_NOT_EVALUATED.

    The same event carries two hints that are never enforced: how many reported UUIDs are shaped
    like random version-4 UUIDs, and CPU and RAM per reported card.
    """

    check_id = "gpu.validate.kernel_count"
    fatal = True

    async def run(self, ctx: Context) -> CheckResult:
        if not settings.GPU_KERNEL_COUNT_CHECK_ENABLED:
            event = render_message(Msg.SKIPPED, ctx=ctx, check_id=self.check_id)
            return CheckResult(passed=True, event=event)
        try:
            return await self._evaluate(ctx)
        except Exception as exc:  # a check that only observes must not break the cycle
            event = render_message(
                Msg.NOT_EVALUATED,
                ctx=ctx,
                check_id=self.check_id,
                what={
                    "kernel_verdict": "not_evaluated",
                    "not_evaluated_reason": "evaluation_error",
                    "error": repr(exc)[:300],
                },
            )
            return CheckResult(passed=True, event=event)

    async def _evaluate(self, ctx: Context) -> CheckResult:
        reported_uuids = reported_gpu_uuids(ctx)
        reported_count = reported_gpu_count(ctx, reported_uuids)
        kernel_uuids, foreign_mounts = await kernel_gpu_view(ctx)

        what = evaluate_kernel_view(
            reported_count=reported_count,
            reported_uuids=reported_uuids,
            kernel_uuids=kernel_uuids,
        )
        what["kernel_view_source"] = KERNEL_VIEW_SOURCE_CONTAINER
        if what["kernel_verdict"] == "count_lower":
            what["container_kernel_gpu_count"] = what["kernel_gpu_count"]
            what.update(
                await host_view_verdict(
                    ctx, reported_count=reported_count, reported_uuids=reported_uuids
                )
            )
        verdict = what["kernel_verdict"]
        if verdict == "not_evaluated":
            what["not_evaluated_reason"] = (
                "kernel_list_behind_foreign_mount" if foreign_mounts else "kernel_list_unavailable"
            )
        v4_count = count_v4_shaped_uuids(reported_uuids)
        shape = host_shape_hint(
            gpu_count=reported_count,
            cpu_count=advertised_cpu_count(ctx),
            ram_gb=reported_ram_gb(ctx.state.specs),
        )
        enforce = bool(settings.GPU_KERNEL_COUNT_ENFORCEMENT_ENABLED)
        would_fail = verdict == "count_lower"
        what.update(
            {
                "reported_uuids": reported_uuids,
                "kernel_gpu_uuids": kernel_uuids,
                "kernel_gpu_foreign_mounts": foreign_mounts,
                "v4_shaped_uuid_count": v4_count,
                "v4_shape_flag": v4_count >= GPU_UUID_V4_SHAPE_FLAG_MIN,
                **shape,
                "would_fail": would_fail,
                "enforced": enforce,
            }
        )
        shadow_flags = [name for name in ("v4_shape_flag", "host_shape_flag") if what[name]]
        what["shadow_flags"] = shadow_flags

        if would_fail:
            event = render_message(
                Msg.COUNT_MISMATCH,
                ctx=ctx,
                check_id=self.check_id,
                severity=None if enforce else "warning",
                impact=None if enforce else SHADOW_ONLY_IMPACT,
                what=what,
            )
            return CheckResult(passed=not enforce, event=event)

        template: MessageTemplate
        if verdict == "uuid_mismatch":
            template = Msg.UUID_MISMATCH
        elif shadow_flags:
            template = Msg.SHADOW_SIGNAL
        elif verdict == "not_evaluated":
            template = Msg.NOT_EVALUATED
        else:
            template = Msg.COUNT_OK
        event = render_message(template, ctx=ctx, check_id=self.check_id, what=what)
        return CheckResult(passed=True, event=event)
