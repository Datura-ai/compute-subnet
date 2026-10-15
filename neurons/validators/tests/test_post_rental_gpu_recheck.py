"""POST_RENTAL_GPU_RECHECK (DAH-4482): a node flagged during a rental proves every card before it re-lists."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from neurons.validators.src.services.task.checks import (
    GpuFaultProbeCheck,
    PinnedMemoryProbeCheck,
    PostRentalGpuRecheckCheck,
    RentedGpuHealthShadowCheck,
    ScoreCheck,
    TenantEnforcementCheck,
)
from neurons.validators.src.services.task.checks import post_rental_gpu_recheck as module
from neurons.validators.src.services.task.messages import PostRentalGpuRecheckMessages as Msg
from neurons.validators.src.services.task.pipeline import ParallelStage
from neurons.validators.src.services.task.pipeline_factory import PipelineFactory

from tests.helpers import build_context_config, build_services, build_state, default_executor
from tests.test_gpu_fault_probe_check import FakeRunner, probe_stdout
from tests.test_rented_gpu_health_shadow import _ctx as rented_ctx
from tests.test_rented_gpu_health_shadow import _healthy, _stdout

KEY = module.marker_key("miner-hotkey", default_executor().uuid)
MARKER = {"at": "2026-10-15T00:00:00+00:00", "source": "rented_gpu_health_shadow", "cards": []}


class FakeValidation:
    def __init__(self, all_cards=None):
        self.all_cards = all_cards
        self.calls = 0

    async def _probe_all_claimed_cards(
        self, ssh_client, executor_info, default_extra, machine_spec
    ):
        self.calls += 1
        return self.all_cards


def _idle_ctx(context_factory, runner, validation, *, marker=MARKER):
    services = build_services(validation=validation)
    if marker is not None:
        services.redis.store[KEY] = json.dumps(marker)
    return context_factory(
        services=services,
        config=build_context_config(),
        state=build_state(specs={"gpu": {"count": 2}}),
        runner=runner,
    )


@pytest.fixture
def recheck_on():
    # the global probe flags are off: the recheck enforces regardless
    with (
        patch.object(module.settings, "POST_RENTAL_GPU_RECHECK_ENABLED", True),
        patch.object(module.settings, "GPU_FAULT_PROBE_CHECK_ENABLED", False),
        patch.object(module.settings, "GPU_FAULT_PROBE_ENFORCEMENT_ENABLED", False),
    ):
        yield


def test_off_by_default():
    assert type(module.settings).model_fields["POST_RENTAL_GPU_RECHECK_ENABLED"].default is False


# --- the marker, set on a rented cycle ----------------------------------------------------------------------


@pytest.fixture
def shadow_on():
    with patch.object(module.settings, "RENTED_GPU_HEALTH_SHADOW_ENABLED", True):
        yield


@pytest.mark.asyncio
async def test_a_flagged_rented_cycle_records_the_marker(context_factory, shadow_on):
    gpus = [_healthy(i) for i in range(8)]
    gpus[0]["recovery_action"] = 1
    gpus[1] |= {"pcie_width": 8, "pcie_width_max": 16}
    ctx = rented_ctx(context_factory, FakeRunner(_stdout(gpus)))

    result = await RentedGpuHealthShadowCheck().run(ctx)

    assert result.passed is True
    redis = ctx.services.redis
    assert redis.ttl[KEY] == module.MARKER_TTL_SECONDS
    marker = json.loads(redis.store[KEY])
    assert marker["source"] == "rented_gpu_health_shadow" and marker["at"]
    assert [(card["index"], card["flags"]) for card in marker["cards"]] == [
        (0, ["recovery_action"]),
        (1, ["pcie_width_drop"]),
    ]


@pytest.mark.asyncio
async def test_a_clean_rented_cycle_records_nothing(context_factory, shadow_on):
    ctx = rented_ctx(context_factory, FakeRunner(_stdout([_healthy(i) for i in range(8)])))

    await RentedGpuHealthShadowCheck().run(ctx)

    assert KEY not in ctx.services.redis.store


# --- the recheck, on the next unrented cycle ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_marker_runs_nothing(context_factory, recheck_on):
    runner, validation = FakeRunner(), FakeValidation()

    result = await PostRentalGpuRecheckCheck().run(
        _idle_ctx(context_factory, runner, validation, marker=None)
    )

    assert result.passed and result.event.reason_code == Msg.NO_MARKER.reason
    assert runner.calls == [] and validation.calls == 0


@pytest.mark.asyncio
async def test_off_only_logs_what_it_would_require(context_factory, caplog):
    runner, validation = FakeRunner(), FakeValidation()
    ctx = _idle_ctx(context_factory, runner, validation)

    with caplog.at_level("WARNING", logger=module.__name__):
        result = await PostRentalGpuRecheckCheck().run(ctx)

    assert result.passed and result.event.reason_code == Msg.WOULD_REQUIRE.reason
    assert result.event.what_we_saw["marker"] == MARKER
    assert runner.calls == [] and validation.calls == 0
    assert KEY in ctx.services.redis.store
    assert any(r.getMessage() == "POST_RENTAL_GPU_RECHECK_WOULD_REQUIRE" for r in caplog.records)


@pytest.mark.asyncio
async def test_on_a_fault_fails_the_node_and_keeps_the_marker(context_factory, recheck_on):
    runner = FakeRunner(probe_stdout("fault", faults=["gpu 1: wrong result in gather"]))
    validation = FakeValidation(SimpleNamespace(passed=True, failure_reason=""))
    ctx = _idle_ctx(context_factory, runner, validation)

    result = await PostRentalGpuRecheckCheck().run(ctx)

    assert result.passed is False and PostRentalGpuRecheckCheck.fatal is True
    assert result.event.reason_code == Msg.FAILED.reason
    assert "gpu 1: wrong result" in result.event.what_we_saw["error"]
    assert validation.calls == 1 and len(runner.calls) == 1
    assert KEY in ctx.services.redis.store


@pytest.mark.asyncio
async def test_on_a_failed_all_cards_matmul_fails_the_node(context_factory, recheck_on):
    runner = FakeRunner(probe_stdout("ok"))
    validation = FakeValidation(
        SimpleNamespace(passed=False, failure_reason="1/2 card(s) failed device work-proof")
    )
    ctx = _idle_ctx(context_factory, runner, validation)

    result = await PostRentalGpuRecheckCheck().run(ctx)

    assert result.passed is False and result.event.reason_code == Msg.FAILED.reason
    assert "1/2 card(s)" in result.event.what_we_saw["error"]
    assert KEY in ctx.services.redis.store


@pytest.mark.asyncio
async def test_on_a_pass_clears_the_marker(context_factory, recheck_on, caplog):
    runner = FakeRunner(probe_stdout("ok"))
    validation = FakeValidation(SimpleNamespace(passed=True, failure_reason=""))
    ctx = _idle_ctx(context_factory, runner, validation)

    with caplog.at_level("INFO", logger=module.__name__):
        result = await PostRentalGpuRecheckCheck().run(ctx)

    assert result.passed and result.event.reason_code == Msg.PASSED.reason
    assert KEY not in ctx.services.redis.store
    assert any(r.getMessage() == "POST_RENTAL_GPU_RECHECK_PASSED" for r in caplog.records)


@pytest.mark.asyncio
async def test_on_a_probe_without_a_verdict_keeps_the_marker(context_factory, recheck_on):
    runner = FakeRunner("python: not found", exit_code=127)
    ctx = _idle_ctx(context_factory, runner, FakeValidation())

    result = await PostRentalGpuRecheckCheck().run(ctx)

    assert result.passed and result.event.reason_code == Msg.INCONCLUSIVE.reason
    assert KEY in ctx.services.redis.store


# --- placement ----------------------------------------------------------------------------------------------


def _flat(checks) -> list[type]:
    kinds: list[type] = []
    for check in checks:
        if isinstance(check, ParallelStage):
            for lane in check.lanes:
                kinds += _flat(lane)
        else:
            kinds.append(type(check))
    return kinds


@pytest.mark.parametrize(
    "build",
    [
        PipelineFactory.build_checks,
        PipelineFactory.build_dry_run_checks,
        PipelineFactory.build_fast_path_checks,
    ],
)
def test_the_recheck_runs_unrented_after_the_gpu_probes_before_the_score(build):
    kinds = _flat(build())

    at = kinds.index(PostRentalGpuRecheckCheck)
    assert kinds.index(TenantEnforcementCheck) < kinds.index(GpuFaultProbeCheck) < at
    assert kinds[at - 1] is PinnedMemoryProbeCheck
    assert at < kinds.index(ScoreCheck)
