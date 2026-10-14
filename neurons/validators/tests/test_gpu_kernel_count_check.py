"""GpuKernelCountCheck: the reported GPU list against the kernel's list.

The reported count and UUIDs come from the scrape. /proc/driver/nvidia/gpus is the kernel's own
inventory. A kernel list that is present and shorter than the reported GPU count is a mismatch:
logged every cycle, and failing the validation only under GPU_KERNEL_COUNT_ENFORCEMENT_ENABLED.
A missing kernel list is never a failure.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from core.config import settings
from neurons.validators.src.services.task.checks.gpu_kernel_count import (
    GPU_UUID_V4_SHAPE_FLAG_MIN,
    HOST_SHAPE_MIN_RAM_GB_PER_GPU,
    HOST_SHAPE_MIN_VCPU_PER_GPU,
    KB_PER_GB,
    GpuKernelCountCheck,
    count_v4_shaped_uuids,
    evaluate_kernel_view,
    host_shape_hint,
)
from neurons.validators.src.services.task.messages import GpuKernelCountMessages as Msg
from neurons.validators.src.services.task.pipeline import Pipeline

from tests.helpers import build_state

# Version nibble and variant nibble are not 4 / 8-b: not shaped like a random UUID.
REAL = [f"GPU-0000000{i}-5281-aabc-1a90-c91764f90d1{i}" for i in range(8)]
# Version nibble 4, variant nibble 8, 9, a or b.
V4_SHAPED = [f"GPU-1111111{i}-d04f-4862-{v}c7d-95b6f13e8c4{i}" for i, v in enumerate("89ab89a")]


class _Sink:
    async def emit(self, event) -> None:
        pass


def _ctx(
    context_factory, *, reported, kernel, read_attempted=True, cpu=64, ram_gb=512.0, count=None
):
    specs = {
        "gpu": {"count": len(reported) if count is None else count},
        "cpu": {"count": cpu},
        "ram": {"total": ram_gb * KB_PER_GB},
    }
    if cpu is None:
        specs.pop("cpu")
    if ram_gb is None:
        specs.pop("ram")
    ssh = MagicMock()
    ssh.run = AsyncMock(side_effect=AssertionError("the kernel list is read once per cycle"))
    return context_factory(
        state=build_state(
            specs=specs,
            gpu_count=len(reported) if count is None else count,
            gpu_uuids=",".join(reported),
            kernel_gpu_uuids=kernel,
            kernel_gpu_uuids_read_attempted=read_attempted,
        ),
        ssh=ssh,
    )


@pytest.fixture
def shadow(monkeypatch):
    monkeypatch.setattr(settings, "GPU_KERNEL_COUNT_CHECK_ENABLED", True)
    monkeypatch.setattr(settings, "GPU_KERNEL_COUNT_ENFORCEMENT_ENABLED", False)


@pytest.fixture
def enforcing(monkeypatch):
    monkeypatch.setattr(settings, "GPU_KERNEL_COUNT_CHECK_ENABLED", True)
    monkeypatch.setattr(settings, "GPU_KERNEL_COUNT_ENFORCEMENT_ENABLED", True)


def test_defaults_are_check_on_enforcement_off():
    fields = type(settings).model_fields
    assert fields["GPU_KERNEL_COUNT_CHECK_ENABLED"].default is True
    assert fields["GPU_KERNEL_COUNT_ENFORCEMENT_ENABLED"].default is False


# ---------------------------- kernel 1 vs reported 8 ----------------------------


@pytest.mark.asyncio
async def test_kernel_one_reported_eight_is_logged_and_passes_in_shadow(context_factory, shadow):
    ctx = _ctx(context_factory, reported=[REAL[0], *V4_SHAPED], kernel=[REAL[0]])

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is True
    assert result.updates == {}
    assert result.event.reason_code == Msg.COUNT_MISMATCH.reason
    assert result.event.severity == "warning"
    what = result.event.what_we_saw
    assert what["kernel_verdict"] == "count_lower"
    assert what["reported_gpu_count"] == 8
    assert what["kernel_gpu_count"] == 1
    assert what["would_fail"] is True
    assert what["enforced"] is False
    assert what["reported_uuids_not_in_kernel"] == [u.lower() for u in V4_SHAPED]


@pytest.mark.asyncio
async def test_kernel_one_reported_eight_leaves_the_pipeline_result_unchanged_in_shadow(
    context_factory, shadow
):
    ctx = _ctx(context_factory, reported=[REAL[0], *V4_SHAPED], kernel=[REAL[0]])

    ok, events, after = await Pipeline([GpuKernelCountCheck()], _Sink()).run(ctx)

    assert ok is True
    assert events[-1].reason_code == Msg.COUNT_MISMATCH.reason
    assert after.state == ctx.state
    assert after.score == ctx.score


@pytest.mark.asyncio
async def test_kernel_one_reported_eight_fails_the_validation_under_enforcement(
    context_factory, enforcing
):
    ctx = _ctx(context_factory, reported=[REAL[0], *V4_SHAPED], kernel=[REAL[0]])

    result = await GpuKernelCountCheck().run(ctx)
    ok, events, _ = await Pipeline([GpuKernelCountCheck()], _Sink()).run(ctx)

    assert result.passed is False
    assert result.event.reason_code == Msg.COUNT_MISMATCH.reason
    assert result.event.severity == "error"
    assert result.event.what_we_saw["enforced"] is True
    assert ok is False
    assert events[-1].what_we_saw["steps_failed"] == GpuKernelCountCheck.check_id


@pytest.mark.asyncio
async def test_reported_count_above_the_listed_cards_is_judged_on_the_count(
    context_factory, shadow
):
    # the node claims 8 and lists one card: the claim is what is compared
    ctx = _ctx(context_factory, reported=[REAL[0]], kernel=[REAL[0]], count=8)

    result = await GpuKernelCountCheck().run(ctx)

    assert result.event.reason_code == Msg.COUNT_MISMATCH.reason
    assert result.event.what_we_saw["reported_gpu_count"] == 8


# ---------------------------- kernel 8 vs reported 8 ----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("enforcement", [False, True])
async def test_kernel_eight_reported_eight_passes(context_factory, monkeypatch, enforcement):
    monkeypatch.setattr(settings, "GPU_KERNEL_COUNT_CHECK_ENABLED", True)
    monkeypatch.setattr(settings, "GPU_KERNEL_COUNT_ENFORCEMENT_ENABLED", enforcement)
    # the kernel prints the same UUIDs; letter case is not a difference
    ctx = _ctx(context_factory, reported=REAL, kernel=[u.upper() for u in reversed(REAL)])

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is True
    assert result.event.reason_code == Msg.COUNT_OK.reason
    what = result.event.what_we_saw
    assert what["kernel_verdict"] == "match"
    assert what["would_fail"] is False
    assert what["reported_uuids_not_in_kernel"] == []
    assert what["shadow_flags"] == []


@pytest.mark.asyncio
async def test_kernel_listing_more_cards_than_reported_passes(context_factory, enforcing):
    ctx = _ctx(context_factory, reported=REAL[:2], kernel=REAL[:4])

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is True
    assert result.event.reason_code == Msg.COUNT_OK.reason
    assert result.event.what_we_saw["kernel_uuids_not_reported"] == [u.lower() for u in REAL[2:4]]


# ---------------------------- kernel list missing ----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("kernel", [None, []])
async def test_missing_kernel_list_is_not_evaluated_and_never_fails(
    context_factory, enforcing, kernel
):
    ctx = _ctx(context_factory, reported=REAL, kernel=kernel)

    result = await GpuKernelCountCheck().run(ctx)
    ok, _, _ = await Pipeline([GpuKernelCountCheck()], _Sink()).run(ctx)

    assert result.passed is True
    assert ok is True
    assert result.event.reason_code == Msg.NOT_EVALUATED.reason
    assert result.event.severity == "info"
    what = result.event.what_we_saw
    assert what["kernel_verdict"] == "not_evaluated"
    assert what["not_evaluated_reason"] == "kernel_list_unavailable"
    assert what["kernel_gpu_count"] is None
    assert what["would_fail"] is False


@pytest.mark.asyncio
async def test_kernel_list_withheld_behind_a_foreign_mount_is_not_evaluated(
    context_factory, enforcing
):
    ctx = _ctx(context_factory, reported=REAL, kernel=None)
    mounts = ["/proc/driver/nvidia/gpus tmpfs"]
    ctx = ctx.model_copy(update={"state": replace(ctx.state, kernel_gpu_foreign_mounts=mounts)})

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is True
    assert result.event.reason_code == Msg.NOT_EVALUATED.reason
    assert result.event.what_we_saw["not_evaluated_reason"] == "kernel_list_behind_foreign_mount"
    assert result.event.what_we_saw["kernel_gpu_foreign_mounts"] == mounts


@pytest.mark.asyncio
async def test_failed_kernel_read_is_not_evaluated(context_factory, enforcing):
    # BannedProviderCheck did not run: the check reads the list itself, and the read fails
    ctx = _ctx(context_factory, reported=REAL, kernel=None, read_attempted=False)
    ctx.ssh.run = AsyncMock(side_effect=OSError("connection lost"))

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is True
    assert result.event.reason_code == Msg.NOT_EVALUATED.reason


@pytest.mark.asyncio
async def test_malformed_specs_never_fail_the_check(context_factory, enforcing):
    ctx = _ctx(context_factory, reported=REAL, kernel=REAL)
    specs = {"gpu": "x", "cpu": {"count": "many"}, "ram": {"total": "?"}}
    state = replace(ctx.state, specs=specs)

    result = await GpuKernelCountCheck().run(ctx.model_copy(update={"state": state}))

    assert result.passed is True
    assert result.event.reason_code == Msg.COUNT_OK.reason
    assert result.event.what_we_saw["host_shape_flag"] is False


@pytest.mark.asyncio
async def test_disabled_check_reads_nothing(context_factory, monkeypatch):
    monkeypatch.setattr(settings, "GPU_KERNEL_COUNT_CHECK_ENABLED", False)
    monkeypatch.setattr(settings, "GPU_KERNEL_COUNT_ENFORCEMENT_ENABLED", True)
    ctx = _ctx(context_factory, reported=[REAL[0], *V4_SHAPED], kernel=[REAL[0]])

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is True
    assert result.event.reason_code == Msg.SKIPPED.reason


# ---------------------------- reported UUID absent from the kernel list ----------------------------


@pytest.mark.asyncio
async def test_reported_uuid_absent_from_kernel_list_is_flagged_not_failed(
    context_factory, enforcing
):
    reported = [*REAL[:7], "GPU-99999999-5281-aabc-1a90-c91764f90d19"]
    ctx = _ctx(context_factory, reported=reported, kernel=REAL)

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is True
    assert result.event.reason_code == Msg.UUID_MISMATCH.reason
    assert result.event.severity == "warning"
    what = result.event.what_we_saw
    assert what["kernel_verdict"] == "uuid_mismatch"
    assert what["reported_uuids_not_in_kernel"] == ["gpu-99999999-5281-aabc-1a90-c91764f90d19"]
    assert what["kernel_uuids_not_reported"] == [REAL[7].lower()]
    assert what["would_fail"] is False


def test_evaluate_kernel_view_verdicts():
    assert (
        evaluate_kernel_view(reported_count=8, reported_uuids=REAL, kernel_uuids=REAL[:1])[
            "kernel_verdict"
        ]
        == "count_lower"
    )
    assert (
        evaluate_kernel_view(reported_count=8, reported_uuids=REAL, kernel_uuids=REAL)[
            "kernel_verdict"
        ]
        == "match"
    )
    assert (
        evaluate_kernel_view(reported_count=8, reported_uuids=REAL, kernel_uuids=None)[
            "kernel_verdict"
        ]
        == "not_evaluated"
    )
    assert (
        evaluate_kernel_view(reported_count=0, reported_uuids=[], kernel_uuids=REAL[:1])[
            "kernel_verdict"
        ]
        == "match"
    )


# ---------------------------- shadow signal: UUID shape ----------------------------


def test_v4_shape_counts_only_version_4_with_rfc_variant():
    assert count_v4_shaped_uuids(REAL) == 0
    assert count_v4_shaped_uuids(V4_SHAPED) == 7
    assert count_v4_shaped_uuids([u.upper() for u in V4_SHAPED[:3]]) == 3
    # version 4 with a variant nibble outside 8-b is not the shape
    assert count_v4_shaped_uuids(["GPU-3ab9e571-d04f-4862-cc7d-95b6f13e8c40"]) == 0
    # repeated UUIDs count once; text that is not a UUID does not count
    assert count_v4_shaped_uuids([V4_SHAPED[0], V4_SHAPED[0], "not-a-uuid", ""]) == 1


@pytest.mark.asyncio
async def test_v4_shaped_uuids_are_flagged_and_never_enforced(context_factory, enforcing):
    reported = [*REAL[:6], *V4_SHAPED[:GPU_UUID_V4_SHAPE_FLAG_MIN]]
    ctx = _ctx(context_factory, reported=reported, kernel=reported)

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is True
    assert result.event.reason_code == Msg.SHADOW_SIGNAL.reason
    assert result.event.severity == "warning"
    what = result.event.what_we_saw
    assert what["v4_shaped_uuid_count"] == GPU_UUID_V4_SHAPE_FLAG_MIN
    assert what["v4_shape_flag"] is True
    assert what["shadow_flags"] == ["v4_shape_flag"]


@pytest.mark.asyncio
async def test_one_v4_shaped_uuid_is_not_flagged(context_factory, shadow):
    reported = [*REAL[:7], V4_SHAPED[0]]
    ctx = _ctx(context_factory, reported=reported, kernel=reported)

    result = await GpuKernelCountCheck().run(ctx)

    assert result.event.reason_code == Msg.COUNT_OK.reason
    assert result.event.what_we_saw["v4_shaped_uuid_count"] == 1
    assert result.event.what_we_saw["v4_shape_flag"] is False


@pytest.mark.asyncio
async def test_shadow_signals_are_logged_when_the_kernel_list_is_missing(
    context_factory, enforcing
):
    ctx = _ctx(context_factory, reported=[REAL[0], *V4_SHAPED], kernel=None, cpu=12, ram_gb=62.0)

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is True
    assert result.event.reason_code == Msg.SHADOW_SIGNAL.reason
    what = result.event.what_we_saw
    assert what["kernel_verdict"] == "not_evaluated"
    assert what["shadow_flags"] == ["v4_shape_flag", "host_shape_flag"]


# ---------------------------- shadow signal: host shape ----------------------------


def test_host_shape_floors():
    small = host_shape_hint(gpu_count=8, cpu_count=12, ram_gb=62.0)
    assert small["host_shape_flag"] is True
    assert small["host_shape_reasons"] == ["vcpu_per_gpu_below_floor", "ram_gb_per_gpu_below_floor"]
    assert small["vcpu_per_gpu"] == 1.5
    assert small["ram_gb_per_gpu"] == 7.75

    at_floor = host_shape_hint(
        gpu_count=8,
        cpu_count=int(8 * HOST_SHAPE_MIN_VCPU_PER_GPU),
        ram_gb=8 * HOST_SHAPE_MIN_RAM_GB_PER_GPU,
    )
    assert at_floor["host_shape_flag"] is False

    # one card is never a host-shape finding, and a missing reading is not a finding either
    assert host_shape_hint(gpu_count=1, cpu_count=1, ram_gb=2.0)["host_shape_flag"] is False
    assert host_shape_hint(gpu_count=8, cpu_count=None, ram_gb=None)["host_shape_flag"] is False


@pytest.mark.asyncio
async def test_small_host_for_the_reported_gpus_is_flagged_and_never_enforced(
    context_factory, enforcing
):
    ctx = _ctx(context_factory, reported=REAL, kernel=REAL, cpu=12, ram_gb=74.0)

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is True
    assert result.event.reason_code == Msg.SHADOW_SIGNAL.reason
    what = result.event.what_we_saw
    assert what["host_shape_flag"] is True
    assert what["host_shape_reasons"] == ["vcpu_per_gpu_below_floor"]
    assert what["cpu_count"] == 12
    assert what["ram_gb"] == 74.0
    assert what["shadow_flags"] == ["host_shape_flag"]


# ---------------------------- wiring ----------------------------


def test_check_is_in_every_pipeline_after_the_kernel_read():
    from neurons.validators.src.services.task.pipeline_factory import PipelineFactory

    def flat(checks):
        out = []
        for chk in checks:
            lanes = getattr(chk, "lanes", None)
            out.extend(c.check_id for lane in lanes for c in lane) if lanes else out.append(
                chk.check_id
            )
        return out

    for ids in (
        flat(PipelineFactory.build_checks()),
        flat(PipelineFactory.build_fast_path_checks()),
        flat(PipelineFactory.build_dry_run_checks()),
    ):
        assert ids.count(GpuKernelCountCheck.check_id) == 1
        assert ids.index("gpu.validate.banned_provider") < ids.index(GpuKernelCountCheck.check_id)
