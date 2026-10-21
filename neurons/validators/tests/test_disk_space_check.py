"""DAH-4961: the listing's disk floor as a validation failure of an idle node."""

from __future__ import annotations

import pytest
from protocol.vc_protocol.compute_requests import RentedExecutor, RentedExecutorsResponse, RentedPod
from services.const import MAX_DISK_UTILIZATION_PERCENT, MIN_FREE_DISK_GB
from services.task.checks.disk_space import KB_PER_GB, DiskSpaceCheck
from services.task.messages import DiskSpaceMessages as Msg
from services.task.pipeline_factory import PipelineFactory

from tests.helpers import build_state


def _disk(utilization: float, free_gb: float) -> dict:
    return {"hard_disk": {"utilization": utilization, "free": free_gb * KB_PER_GB}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "specs, reason",
    [
        (_disk(50, 500), Msg.OK.reason),
        (_disk(MAX_DISK_UTILIZATION_PERCENT + 1, 500), Msg.TOO_FULL.reason),
        (_disk(50, MIN_FREE_DISK_GB - 1), Msg.FREE_TOO_LOW.reason),
        ({}, Msg.NOT_REPORTED.reason),
        ({"hard_disk": {"utilization": 10}}, Msg.NOT_REPORTED.reason),
        (_disk(10, 50 - 1 / KB_PER_GB), Msg.FREE_TOO_LOW.reason),
        (_disk(10, 50), Msg.OK.reason),
        (_disk(10, 50 + 1 / KB_PER_GB), Msg.OK.reason),
        (_disk(89.999, 100), Msg.OK.reason),
        (_disk(90, 100), Msg.OK.reason),
        (_disk(90.001, 100), Msg.TOO_FULL.reason),
    ],
)
async def test_idle_node_fails_the_listing_disk_floor(context_factory, specs, reason):
    ctx = context_factory(state=build_state(specs=specs))

    result = await DiskSpaceCheck().run(ctx)

    assert result.event.reason_code == reason
    assert result.passed is (reason == Msg.OK.reason)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner, address, port, exempt",
    [
        ("miner-hotkey", "127.0.0.1", "8080", True),  # this miner's rental
        ("other-hotkey", "127.0.0.1", "22", True),  # another miner, same endpoint: the pod is here
        ("other-hotkey", "10.0.0.1", "22", False),  # another miner elsewhere
        (None, None, None, False),  # no rental
    ],
)
async def test_live_rental_is_not_failed_for_its_disk(
    context_factory, owner, address, port, exempt
):
    """Tenant enforcement and disk enforcement agree whether the rental belongs on this node."""
    executors = {}
    if owner is not None:
        executors["executor-123"] = RentedExecutor(
            miner_hotkey=owner,
            executor_ip_address=address,
            executor_ip_port=port,
            pods=[RentedPod(pod_id="pod-1", container_name="container_test", rented_ports=[])],
        )
    rented_data = RentedExecutorsResponse(executors=executors)
    ctx = context_factory(state=build_state(specs=_disk(95, 500), rented_data=rented_data))

    result = await DiskSpaceCheck().run(ctx)

    assert result.passed is exempt
    assert result.event.reason_code == Msg.TOO_FULL.reason


def test_disk_space_runs_right_after_verifyx_in_both_pipelines():
    for checks in (PipelineFactory.build_checks(), PipelineFactory.build_fast_path_checks()):
        names = [type(c).__name__ for c in checks]
        assert names[names.index("VerifyXCheck") + 1] == "DiskSpaceCheck"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"default_job_owner_by_executor": {"executor-123": "lium"}},
        {"all_filler_containers_by_executor": {"executor-123": ["filler_run-1"]}},
        {"default_job_owner_by_executor": {"executor-123": "miner"}},
    ],
)
async def test_default_job_or_filler_does_not_exempt_a_full_disk(context_factory, fields):
    """Only a live rental is exempt; an idle node with a Default Job or filler still fails."""
    rented_data = RentedExecutorsResponse(executors={}, **fields)
    ctx = context_factory(state=build_state(specs=_disk(95, 500), rented_data=rented_data))

    result = await DiskSpaceCheck().run(ctx)

    assert result.passed is False
    assert result.event.reason_code == Msg.TOO_FULL.reason
