from unittest.mock import MagicMock

import pytest
from helpers import build_services, build_state
from neurons.validators.src.services import matrix_validation_service as mvs
from neurons.validators.src.services.matrix_validation_service import ValidationResult
from neurons.validators.src.services.task.checks import capability
from neurons.validators.src.services.task.checks.capability import CapabilityCheck
from neurons.validators.src.services.task.messages import CapabilityMessages as Msg
from protocol.vc_protocol.compute_requests import RentedExecutorsResponse


class SamplingValidationService:
    def __init__(self, *, success: bool = True, raises: bool = False):
        self.success = success
        self.raises = raises
        self.budgets: list[int | None] = []

    async def validate_gpu_model_and_process_job(self, *, vram_budget_mb=None, **_):
        self.budgets.append(vram_budget_mb)
        if self.raises:
            raise RuntimeError("ssh went away")
        return ValidationResult(
            success=self.success, error_message="" if self.success else "no card"
        )


@pytest.fixture(autouse=True)
def no_sample_count_carried_between_tests():
    capability._filler_cycles_since_sample.clear()
    yield
    capability._filler_cycles_since_sample.clear()


async def _filler_cycles(context_factory, service, cycles: int) -> list:
    results = []
    for _ in range(cycles):
        state = build_state(
            specs={"gpu": {"count": 1}},
            rented_data=RentedExecutorsResponse(
                executors={},
                banned_guids=[],
                filler_containers_by_executor={"executor-123": "filler_active"},
            ),
        )
        ctx = context_factory(services=build_services(validation=service), state=state)
        results.append(await CapabilityCheck().run(ctx))
    return results


@pytest.mark.asyncio
async def test_no_sample_runs_by_default(context_factory):
    service = SamplingValidationService()

    results = await _filler_cycles(context_factory, service, 5)

    assert service.budgets == []
    assert all(r.event.what_we_saw == {"filler_container": "filler_active"} for r in results)


@pytest.mark.asyncio
async def test_every_nth_filler_cycle_runs_a_small_sample(context_factory, monkeypatch):
    monkeypatch.setattr(capability.settings, "FILLER_MATMUL_SAMPLE_EVERY_N_CYCLES", 3)
    service = SamplingValidationService()

    results = await _filler_cycles(context_factory, service, 7)

    assert service.budgets == [capability.settings.FILLER_MATMUL_SAMPLE_VRAM_MB] * 2
    assert ["matmul_sample" in r.event.what_we_saw for r in results] == [
        False,
        False,
        True,
        False,
        False,
        True,
        False,
    ]
    assert results[2].event.what_we_saw["matmul_sample"]["passed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service", [SamplingValidationService(success=False), SamplingValidationService(raises=True)]
)
async def test_a_failed_sample_is_recorded_and_the_filler_skip_still_passes(
    context_factory, monkeypatch, service
):
    monkeypatch.setattr(capability.settings, "FILLER_MATMUL_SAMPLE_EVERY_N_CYCLES", 1)

    (result,) = await _filler_cycles(context_factory, service, 1)

    assert result.passed is True
    assert result.event.reason_code == Msg.FILLER_SKIPPED.reason
    assert result.event.what_we_saw["matmul_sample"]["passed"] is False
    assert result.event.what_we_saw["matmul_sample"]["error"]


def test_the_default_sample_budget_leaves_room_for_the_matrix(monkeypatch):
    monkeypatch.setattr(mvs, "DMCompVerifyWrapper", lambda *_a, **_kw: MagicMock())
    service = mvs.ValidationService()

    dim_k = service.get_max_matrix_dimensions(capability.settings.FILLER_MATMUL_SAMPLE_VRAM_MB, 2000)

    assert dim_k > 0
