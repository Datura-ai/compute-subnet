"""DAH-5088: an unknown NVIDIA model passes validation and is scored as its reference model.

The reference is the cheapest known model whose nominal VRAM is within 0.90-1.10x of the card's
reported VRAM, else the cheapest with the largest size below it, else none (still unsupported).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from incentive.config import RENTAL_PRICES_PER_HOUR
from neurons.validators.src.services.task.checks.gpu_model_valid import GpuModelValidCheck
from neurons.validators.src.services.task.messages import GpuModelMessages as Msg
from neurons.validators.src.services.task.result_handler import ResultHandler
from neurons.validators.src.services.task.score_calculator import calculate_scores
from services.const import GPU_MODEL_RATES
from services.gpu_precheck import UnsupportedGpuModelError, precheck_gpu_spec
from services.gpu_spec_table import (
    GPU_VRAM_SIZES_MB,
    VRAM_FLOOR_RATIO,
    matmul_probe_vram_mb,
    reference_gpu_model,
)

from core.config import shared_client
from tests.helpers import build_context_config, build_services, build_state, default_executor
from tests.test_idle_slot_caps_by_tier import _run_with_production_config
from tests.test_rental_price_incentive_flow import _make_pcc_job

UNKNOWN = "NVIDIA RTX Future 9000"
CANDIDATES = [m for m in GPU_VRAM_SIZES_MB if m in GPU_MODEL_RATES and m in RENTAL_PRICES_PER_HOUR]


def _in_band(model: str, vram_mb: int) -> bool:
    return any(0.90 * vram_mb <= size <= 1.10 * vram_mb for size in GPU_VRAM_SIZES_MB[model])


@pytest.mark.parametrize("vram_mb", [8000, 15352, 23028, 46068, 81559, 140000, 183359, 275040])
def test_an_unknown_card_gets_the_cheapest_known_model_of_its_memory(vram_mb):
    reference = reference_gpu_model(UNKNOWN, vram_mb)
    in_band = [m for m in CANDIDATES if _in_band(m, vram_mb)]
    assert reference in in_band
    cheapest = min(RENTAL_PRICES_PER_HOUR[m] for m in in_band)
    assert RENTAL_PRICES_PER_HOUR[reference] == cheapest
    assert reference == min(m for m in in_band if RENTAL_PRICES_PER_HOUR[m] == cheapest)


def test_with_none_that_close_it_falls_back_to_the_largest_size_below():
    vram_mb = 400_000
    assert not [m for m in CANDIDATES if _in_band(m, vram_mb)]
    largest = max(size for m in CANDIDATES for size in GPU_VRAM_SIZES_MB[m] if size < vram_mb)
    reference = reference_gpu_model(UNKNOWN, vram_mb)
    assert largest in GPU_VRAM_SIZES_MB[reference]
    at_largest = [m for m in CANDIDATES if largest in GPU_VRAM_SIZES_MB[m]]
    assert RENTAL_PRICES_PER_HOUR[reference] == min(RENTAL_PRICES_PER_HOUR[m] for m in at_largest)


@pytest.mark.parametrize(
    "name,vram_mb",
    [
        ("AMD Instinct MI300X", 196_608),
        (UNKNOWN, 3000),
        (UNKNOWN, None),
        (UNKNOWN, 0),
        (None, 24_576),
    ],
)
def test_no_reference_for_a_non_nvidia_name_or_too_little_memory(name, vram_mb):
    assert reference_gpu_model(name, vram_mb) is None


def test_a_known_name_is_its_own_reference_whatever_its_vram():
    assert reference_gpu_model("NVIDIA H100 80GB HBM3", 8000) == "NVIDIA H100 80GB HBM3"
    assert reference_gpu_model("Tesla T4", None) == "NVIDIA T4 Tensor Core GPU"


def test_precheck_and_matmul_sizing_take_the_reported_vram_of_an_unknown_card():
    precheck_gpu_spec(UNKNOWN, 23028)
    with pytest.raises(UnsupportedGpuModelError):
        precheck_gpu_spec("AMD Instinct MI300X", 196_608)
    assert matmul_probe_vram_mb(UNKNOWN, 23028) == round(23028 * VRAM_FLOOR_RATIO)
    assert matmul_probe_vram_mb(UNKNOWN) is None
    assert matmul_probe_vram_mb("AMD Instinct MI300X", 196_608) is None


def _model_ctx(context_factory, details: list[dict]):
    state = build_state(
        specs={"gpu": {"count": len(details), "details": details}},
        gpu_count=len(details),
        gpu_details=details,
    )
    return context_factory(
        services=build_services(),
        config=build_context_config(gpu_model_rates=GPU_MODEL_RATES),
        state=state,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "details,reason",
    [
        ([{"name": UNKNOWN, "capacity": 23028}] * 2, Msg.MODEL_OK.reason),
        ([{"name": "AMD Instinct MI300X", "capacity": 196_608}], Msg.MODEL_UNSUPPORTED.reason),
        ([{"name": UNKNOWN, "capacity": 3000}], Msg.MODEL_UNSUPPORTED.reason),
        (
            [
                {"name": UNKNOWN, "capacity": 23028},
                {"name": "NVIDIA GeForce RTX 4090", "capacity": 23028},
            ],
            Msg.MODEL_MIXED.reason,
        ),
    ],
)
async def test_gpu_validate_model_passes_an_unknown_nvidia_card(context_factory, details, reason):
    result = await GpuModelValidCheck().run(_model_ctx(context_factory, details))
    assert result.event.reason_code == reason
    assert result.passed is (reason == Msg.MODEL_OK.reason)
    if result.passed:
        assert result.event.what_we_saw["reference_gpu_model"] == reference_gpu_model(
            UNKNOWN, 23028
        )


@pytest.mark.asyncio
async def test_the_result_is_scored_as_the_reference_and_keeps_the_card_name_in_its_specs(
    context_factory,
):
    details = [{"name": UNKNOWN, "capacity": 23028, "uuid": "GPU-1"}]
    state = build_state(
        specs={"gpu": {"count": 1, "details": details}},
        gpu_details=details,
        gpu_model_count=f"{UNKNOWN}:1",
    )
    ctx = context_factory(state=state)
    result = await ResultHandler(redis_service=MagicMock(), dry_run=True).handle_result(
        context=ctx,
        miner_info=SimpleNamespace(miner_hotkey="hk", job_batch_id="batch"),
        executor_info=ctx.executor,
        verified_job_info={},
        log_text="",
        success=True,
    )
    reference = reference_gpu_model(UNKNOWN, 23028)
    assert result.gpu_model == reference
    assert result.spec["gpu"]["details"][0]["name"] == UNKNOWN

    # idle pay and its cap: the unknown card's result is paid exactly as an honest reference card
    jobs = {
        "unknown": [_make_pcc_job("unknown", result.gpu_model, 1)],
        "honest": [_make_pcc_job("honest", reference, 1)],
    }
    await _run_with_production_config(jobs)
    unknown, honest = jobs["unknown"][0], jobs["honest"][0]
    assert unknown.eligible_for_rental_share is honest.eligible_for_rental_share
    assert unknown.effective_rate == honest.effective_rate


def test_an_unknown_card_has_no_hard_price_limit(context_factory):
    # the reference (the cheapest same-memory model) is a payout floor, not a fair price (the backend skips it too)
    details = [{"name": UNKNOWN, "capacity": 23028}]
    reference_limit = (
        shared_client.config.machine_prices[reference_gpu_model(UNKNOWN, 23028)]
        * shared_client.config.machine_max_price_rate
    )
    executor = default_executor().model_copy(update={"price_per_gpu": reference_limit * 10})
    state = build_state(
        specs={"network": {"ema_verifyx_download_speed": 500.0}},
        gpu_model=UNKNOWN,
        gpu_details=details,
    )

    assert calculate_scores(context_factory(executor=executor, state=state), rented=True)[0] == 1.0
