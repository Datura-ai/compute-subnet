"""GpuKernelCountCheck: the reported GPU list against the kernel's list.

The reported count and UUIDs come from the scrape. /proc/driver/nvidia/gpus is the kernel's own
inventory. A kernel list that is present and shorter than the reported GPU count is a mismatch:
logged every cycle, and failing the validation only under GPU_KERNEL_COUNT_ENFORCEMENT_ENABLED.
A missing kernel list is never a failure.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from core.config import settings
from services import nvidia_devices
from services.nvidia_devices import (
    HOST_KERNEL_GPU_VIEW_CMD,
    HOST_PID1_ROOT,
    KERNEL_GPU_FS_SEPARATOR,
    KERNEL_GPU_VIEW_CMD,
    KERNEL_GPU_VIEW_SEPARATOR,
)

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
from protocol.vc_protocol.compute_requests import (
    RentedExecutor,
    RentedExecutorsResponse,
    RentedPod,
)

from tests.helpers import build_state

# Version nibble and variant nibble are not 4 / 8-b: not shaped like a random UUID.
REAL = [f"GPU-0000000{i}-5281-aabc-1a90-c91764f90d1{i}" for i in range(8)]
# Further reported cards; most tests leave them off the kernel list. Their version nibble is 4
# and their variant nibble 8, 9, a or b, so the UUID-shape tests use them as V4_SHAPED.
OTHER_CARDS = [f"GPU-1111111{i}-d04f-4862-{v}c7d-95b6f13e8c4{i}" for i, v in enumerate("89ab89a")]
V4_SHAPED = OTHER_CARDS


class _Sink:
    async def emit(self, event) -> None:
        pass


def _host_stdout(uuids, *, mounts=(), fstype="proc", kind="regular empty file"):
    """What HOST_KERNEL_GPU_VIEW_CMD prints: PID 1's mounts, the stat rows, then the UUID rows."""
    files = [
        f"{HOST_PID1_ROOT}/proc/driver/nvidia/gpus/0000:00:{i + 7:02x}.0/information"
        for i in range(len(uuids))
    ]
    return "\n".join(
        [
            *mounts,
            KERNEL_GPU_FS_SEPARATOR,
            *(f"{path} {fstype}" for path in files),
            *(f"{path}|{kind}" for path in files),
            KERNEL_GPU_VIEW_SEPARATOR,
            *(f"{gpu_uuid}, {i}" for i, gpu_uuid in enumerate(uuids)),
        ]
    )


NO_HOST_VIEW = _host_stdout([])


def _ctx(
    context_factory,
    *,
    reported,
    kernel,
    read_attempted=True,
    cpu=64,
    ram_gb=512.0,
    count=None,
    host=NO_HOST_VIEW,
):
    """`host` is the stdout of the host-view read, or an exception that read raises."""
    specs = {
        "gpu": {"count": len(reported) if count is None else count},
        "cpu": {"count": cpu},
        "ram": {"total": ram_gb * KB_PER_GB},
    }
    if cpu is None:
        specs.pop("cpu")
    if ram_gb is None:
        specs.pop("ram")

    async def run(command, *args, **kwargs):
        if command != HOST_KERNEL_GPU_VIEW_CMD:
            raise AssertionError("the kernel list is read once per cycle")
        if isinstance(host, BaseException):
            raise host
        return SimpleNamespace(exit_status=0, stdout=host, stderr="")

    ssh = MagicMock()
    ssh.run = AsyncMock(side_effect=run)
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
    ctx = _ctx(context_factory, reported=[REAL[0], *OTHER_CARDS], kernel=[REAL[0]])

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
    assert what["reported_uuids_not_in_kernel"] == [u.lower() for u in OTHER_CARDS]


@pytest.mark.asyncio
async def test_kernel_one_reported_eight_leaves_the_pipeline_result_unchanged_in_shadow(
    context_factory, shadow
):
    ctx = _ctx(context_factory, reported=[REAL[0], *OTHER_CARDS], kernel=[REAL[0]])

    ok, events, after = await Pipeline([GpuKernelCountCheck()], _Sink()).run(ctx)

    assert ok is True
    assert events[-1].reason_code == Msg.COUNT_MISMATCH.reason
    assert after.state == ctx.state
    assert after.score == ctx.score


@pytest.mark.asyncio
async def test_kernel_one_reported_eight_fails_the_validation_under_enforcement(
    context_factory, enforcing
):
    ctx = _ctx(context_factory, reported=[REAL[0], *OTHER_CARDS], kernel=[REAL[0]])

    result = await GpuKernelCountCheck().run(ctx)
    ok, events, _ = await Pipeline([GpuKernelCountCheck()], _Sink()).run(ctx)

    assert result.passed is False
    assert result.event.reason_code == Msg.COUNT_MISMATCH.reason
    assert result.event.severity == "error"
    assert result.event.what_we_saw["enforced"] is True
    assert ok is False
    assert events[-1].what_we_saw["steps_failed"] == GpuKernelCountCheck.check_id


@pytest.mark.asyncio
async def test_mismatch_on_a_rented_node_fails_but_does_not_stop_the_run_under_enforcement(
    context_factory, enforcing
):
    # same rule as the ban checks: a node with a renter's pod on it keeps being verified
    ctx = _ctx(context_factory, reported=[REAL[0], *OTHER_CARDS], kernel=[REAL[0]])
    rented = RentedExecutorsResponse(
        executors={
            ctx.executor.uuid: RentedExecutor(
                miner_hotkey="miner-hotkey",
                executor_ip_address=ctx.executor.address,
                executor_ip_port=str(ctx.executor.port),
                pods=[RentedPod(pod_id="pod-1", container_name="pod_p1")],
            )
        }
    )
    ctx = ctx.model_copy(update={"state": replace(ctx.state, rented_data=rented)})

    result = await GpuKernelCountCheck().run(ctx)
    ok, _, _ = await Pipeline([GpuKernelCountCheck()], _Sink()).run(ctx)

    assert result.passed is False
    assert result.fatal is False
    assert result.event.reason_code == Msg.COUNT_MISMATCH.reason
    assert ok is True


@pytest.mark.asyncio
async def test_mismatch_on_an_idle_node_is_fatal_under_enforcement(context_factory, enforcing):
    ctx = _ctx(context_factory, reported=[REAL[0], *OTHER_CARDS], kernel=[REAL[0]])

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is False
    assert result.fatal is True


@pytest.mark.asyncio
async def test_mismatch_on_a_rented_node_still_passes_in_shadow(context_factory, shadow):
    ctx = _ctx(context_factory, reported=[REAL[0], *OTHER_CARDS], kernel=[REAL[0]])
    rented = RentedExecutorsResponse(
        executors={
            ctx.executor.uuid: RentedExecutor(
                miner_hotkey="miner-hotkey",
                executor_ip_address=ctx.executor.address,
                executor_ip_port=str(ctx.executor.port),
                pods=[RentedPod(pod_id="pod-1", container_name="pod_p1")],
            )
        }
    )
    ctx = ctx.model_copy(update={"state": replace(ctx.state, rented_data=rented)})

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is True
    assert result.event.reason_code == Msg.COUNT_MISMATCH.reason
    assert result.event.severity == "warning"


@pytest.mark.asyncio
async def test_reported_count_above_the_listed_cards_is_judged_on_the_count(
    context_factory, shadow
):
    # the node claims 8 and lists one card: the claim is what is compared
    ctx = _ctx(context_factory, reported=[REAL[0]], kernel=[REAL[0]], count=8)

    result = await GpuKernelCountCheck().run(ctx)

    assert result.event.reason_code == Msg.COUNT_MISMATCH.reason
    assert result.event.what_we_saw["reported_gpu_count"] == 8


# ---------------------------- container given a subset of the host's cards ----------------------------

CONTAINER_MOUNTS_OF_ONE_CARD = (
    "504 494 0:63 / /proc/driver/nvidia rw,nosuid,nodev,noexec,relatime - tmpfs tmpfs rw,mode=555",
    "546 504 0:23 /driver/nvidia/gpus/0000:00:07.0 /proc/driver/nvidia/gpus/0000:00:07.0 "
    "ro,nosuid,nodev,noexec,relatime master:12 - proc proc rw",
)


def test_container_read_is_the_same_command_and_the_host_read_goes_through_pid_1():
    assert "/proc/1/" not in KERNEL_GPU_VIEW_CMD
    assert "/proc/self/mountinfo" in KERNEL_GPU_VIEW_CMD
    assert HOST_KERNEL_GPU_VIEW_CMD == KERNEL_GPU_VIEW_CMD.replace(
        "/proc/self/mountinfo", "/proc/1/mountinfo"
    ).replace("/proc/driver/nvidia/gpus/*", "/proc/1/root/proc/driver/nvidia/gpus/*")


@pytest.mark.asyncio
@pytest.mark.parametrize("enforcement", [False, True])
async def test_container_one_host_four_reported_four_matches_on_the_host_view(
    context_factory, monkeypatch, enforcement
):
    monkeypatch.setattr(settings, "GPU_KERNEL_COUNT_CHECK_ENABLED", True)
    monkeypatch.setattr(settings, "GPU_KERNEL_COUNT_ENFORCEMENT_ENABLED", enforcement)
    ctx = _ctx(context_factory, reported=REAL[:4], kernel=[REAL[0]], host=_host_stdout(REAL[:4]))

    result = await GpuKernelCountCheck().run(ctx)
    ok, events, _ = await Pipeline([GpuKernelCountCheck()], _Sink()).run(ctx)

    assert result.passed is True
    assert ok is True
    assert result.event.reason_code == Msg.COUNT_OK.reason
    assert result.event.severity == "info"
    what = result.event.what_we_saw
    assert what["kernel_verdict"] == "match_host_view"
    assert what["kernel_view_source"] == "host_pid1_root"
    assert what["container_kernel_gpu_count"] == 1
    assert what["host_kernel_gpu_count"] == 4
    assert what["reported_uuids_not_in_host_kernel"] == []
    assert what["would_fail"] is False
    assert [call.args[0] for call in ctx.ssh.run.await_args_list] == [HOST_KERNEL_GPU_VIEW_CMD] * 2


@pytest.mark.asyncio
async def test_host_listing_more_cards_than_reported_matches_on_the_host_view(
    context_factory, enforcing
):
    ctx = _ctx(context_factory, reported=REAL[:4], kernel=[REAL[0]], host=_host_stdout(REAL))

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is True
    assert result.event.what_we_saw["kernel_verdict"] == "match_host_view"
    assert result.event.what_we_saw["host_kernel_gpu_count"] == 8


@pytest.mark.asyncio
async def test_container_one_host_one_reported_eight_still_fails_under_enforcement(
    context_factory, enforcing
):
    ctx = _ctx(
        context_factory,
        reported=[REAL[0], *OTHER_CARDS],
        kernel=[REAL[0]],
        host=_host_stdout([REAL[0]]),
    )

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is False
    assert result.event.reason_code == Msg.COUNT_MISMATCH.reason
    what = result.event.what_we_saw
    assert what["kernel_verdict"] == "count_lower"
    assert what["kernel_view_source"] == "container"
    assert what["container_kernel_gpu_count"] == 1
    assert what["host_kernel_gpu_count"] == 1
    assert ctx.ssh.run.await_count == 1


@pytest.mark.asyncio
async def test_host_with_fewer_cards_than_reported_still_fails_under_enforcement(
    context_factory, enforcing
):
    ctx = _ctx(context_factory, reported=REAL, kernel=[REAL[0]], host=_host_stdout(REAL[:4]))

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is False
    assert result.event.what_we_saw["kernel_verdict"] == "count_lower"
    assert result.event.what_we_saw["host_kernel_gpu_count"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host",
    [
        # PID 1 is the container's own init: its mount table is the container's again
        _host_stdout(REAL[:4], mounts=CONTAINER_MOUNTS_OF_ONE_CARD),
        # a tmpfs laid over the gpus path on the host
        _host_stdout(
            REAL[:4], mounts=("90 25 0:70 / /proc/driver/nvidia/gpus rw - tmpfs tmpfs rw",)
        ),
        # an overlay one level up, with nothing but the stat rows to show it
        _host_stdout(REAL[:4], mounts=("90 25 0:70 / /proc/driver/nvidia rw - overlay o rw",)),
        _host_stdout(REAL[:4], fstype="tmpfs"),
        _host_stdout(REAL[:4], kind="symbolic link"),
    ],
)
async def test_host_view_behind_a_foreign_mount_is_ignored(context_factory, enforcing, host):
    ctx = _ctx(context_factory, reported=REAL[:4], kernel=[REAL[0]], host=host)

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is False
    assert result.event.reason_code == Msg.COUNT_MISMATCH.reason
    what = result.event.what_we_saw
    assert what["kernel_verdict"] == "count_lower"
    assert what["kernel_view_source"] == "container"
    assert what["host_kernel_gpu_count"] is None
    assert what["host_kernel_gpu_foreign_mounts"]


@pytest.mark.asyncio
async def test_host_uuid_rows_without_procfs_evidence_are_ignored(context_factory, enforcing):
    uuid_rows_only = "\n".join(
        [
            KERNEL_GPU_FS_SEPARATOR,
            KERNEL_GPU_VIEW_SEPARATOR,
            *(f"{u}, {i}" for i, u in enumerate(REAL[:4])),
        ]
    )
    ctx = _ctx(context_factory, reported=REAL[:4], kernel=[REAL[0]], host=uuid_rows_only)

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is False
    assert result.event.what_we_saw["kernel_verdict"] == "count_lower"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host", [OSError("connection lost"), NO_HOST_VIEW, "", "not the expected output"]
)
async def test_host_read_error_or_empty_host_view_is_a_mismatch_and_never_raises(
    context_factory, enforcing, host
):
    ctx = _ctx(context_factory, reported=REAL[:4], kernel=[REAL[0]], host=host)

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is False
    assert result.event.reason_code == Msg.COUNT_MISMATCH.reason
    assert result.event.what_we_saw["kernel_verdict"] == "count_lower"
    assert result.event.what_we_saw["host_kernel_gpu_count"] is None


@pytest.mark.asyncio
async def test_host_read_non_zero_exit_is_a_mismatch(context_factory, enforcing):
    ctx = _ctx(context_factory, reported=REAL[:4], kernel=[REAL[0]])
    ctx.ssh.run = AsyncMock(
        return_value=SimpleNamespace(exit_status=1, stdout=_host_stdout(REAL[:4]), stderr="")
    )

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is False
    assert result.event.what_we_saw["kernel_verdict"] == "count_lower"


@pytest.mark.asyncio
async def test_host_read_timeout_is_a_mismatch_and_never_raises(
    context_factory, enforcing, monkeypatch
):
    monkeypatch.setattr(nvidia_devices, "KERNEL_GPU_UUID_READ_TIMEOUT_SECONDS", 0.01)
    ctx = _ctx(context_factory, reported=REAL[:4], kernel=[REAL[0]])

    async def hang(command, *args, **kwargs):
        await asyncio.sleep(30)

    ctx.ssh.run = AsyncMock(side_effect=hang)

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is False
    assert result.event.reason_code == Msg.COUNT_MISMATCH.reason
    assert result.event.what_we_saw["kernel_verdict"] == "count_lower"


@pytest.mark.asyncio
async def test_no_ssh_connection_leaves_the_mismatch_as_it_was(context_factory, enforcing):
    with_ssh = _ctx(context_factory, reported=REAL[:4], kernel=[REAL[0]])
    ctx = context_factory(state=with_ssh.state, ssh=None)
    assert ctx.ssh is None

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is False
    assert result.event.what_we_saw["kernel_verdict"] == "count_lower"


@pytest.mark.asyncio
async def test_host_has_the_count_but_not_a_reported_uuid_is_not_a_match(
    context_factory, enforcing
):
    reported = [*REAL[:3], OTHER_CARDS[0]]
    ctx = _ctx(context_factory, reported=reported, kernel=[REAL[0]], host=_host_stdout(REAL[:4]))

    result = await GpuKernelCountCheck().run(ctx)

    # as for a container that has every card: the count holds, the UUID difference is logged
    assert result.passed is True
    assert result.event.reason_code == Msg.UUID_MISMATCH.reason
    what = result.event.what_we_saw
    assert what["kernel_verdict"] == "uuid_mismatch"
    assert what["kernel_view_source"] == "host_pid1_root"
    assert what["reported_uuids_not_in_host_kernel"] == [OTHER_CARDS[0].lower()]
    assert what["would_fail"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reported", "kernel"),
    [(REAL[:4], REAL[:4]), (REAL[:2], REAL[:4]), ([*REAL[:3], OTHER_CARDS[0]], REAL[:4])],
)
async def test_host_view_is_not_read_when_the_container_list_is_long_enough(
    context_factory, enforcing, reported, kernel
):
    ctx = _ctx(context_factory, reported=reported, kernel=kernel, host=_host_stdout(REAL))

    result = await GpuKernelCountCheck().run(ctx)

    assert result.passed is True
    assert result.event.what_we_saw["kernel_view_source"] == "container"
    assert "host_kernel_gpu_count" not in result.event.what_we_saw
    assert ctx.ssh.run.await_count == 0


@pytest.mark.asyncio
async def test_host_view_is_not_read_when_the_container_list_is_missing(context_factory, enforcing):
    ctx = _ctx(context_factory, reported=REAL[:4], kernel=None, host=_host_stdout(REAL))

    result = await GpuKernelCountCheck().run(ctx)

    assert result.event.reason_code == Msg.NOT_EVALUATED.reason
    assert ctx.ssh.run.await_count == 0


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
    ctx = _ctx(context_factory, reported=[REAL[0], *OTHER_CARDS], kernel=[REAL[0]])

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
        evaluate_kernel_view(
            reported_count=8, reported_uuids=REAL, kernel_uuids=REAL[:1]
        ).kernel_verdict
        == "count_lower"
    )
    assert (
        evaluate_kernel_view(
            reported_count=8, reported_uuids=REAL, kernel_uuids=REAL
        ).kernel_verdict
        == "match"
    )
    assert (
        evaluate_kernel_view(
            reported_count=8, reported_uuids=REAL, kernel_uuids=None
        ).kernel_verdict
        == "not_evaluated"
    )
    assert (
        evaluate_kernel_view(
            reported_count=0, reported_uuids=[], kernel_uuids=REAL[:1]
        ).kernel_verdict
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
    assert small.host_shape_flag is True
    assert small.host_shape_reasons == ["vcpu_per_gpu_below_floor", "ram_gb_per_gpu_below_floor"]
    assert small.vcpu_per_gpu == 1.5
    assert small.ram_gb_per_gpu == 7.75

    at_floor = host_shape_hint(
        gpu_count=8,
        cpu_count=int(8 * HOST_SHAPE_MIN_VCPU_PER_GPU),
        ram_gb=8 * HOST_SHAPE_MIN_RAM_GB_PER_GPU,
    )
    assert at_floor.host_shape_flag is False

    # one card is never a host-shape finding, and a missing reading is not a finding either
    assert host_shape_hint(gpu_count=1, cpu_count=1, ram_gb=2.0).host_shape_flag is False
    assert host_shape_hint(gpu_count=8, cpu_count=None, ram_gb=None).host_shape_flag is False


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
