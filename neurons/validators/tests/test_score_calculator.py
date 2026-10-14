"""Tests for calculate_scores in score_calculator.py, focused on the EMA verifyx download
speed threshold. Other score_calculator paths (rental, price) are exercised
via integration in test_score_check.py and test_pipeline_default_scenarios.py.
"""
import pytest

from neurons.validators.src.services.task import score_calculator
from neurons.validators.src.services.task.score_calculator import calculate_scores
from neurons.validators.src.services.task.checks.verifyx import MIN_VERIFYX_EMA_DOWNLOAD_SPEED_MBPS
from helpers import build_context_config, build_services, build_state, default_executor, make_context


def _ctx_without_specs(specs, price_per_gpu=None):
    executor = default_executor()
    executor = executor.model_copy(update={"price_per_gpu": price_per_gpu})
    state = build_state(specs=specs)
    return make_context(
        executor=executor,
        state=state,
        services=build_services(),
        config=build_context_config(),
        collateral_deposited=True,
        is_rental_succeed=True,
    )


def _ctx(ema_verifyx_download_speed, price_per_gpu=None):
    return _ctx_without_specs(
        {"network": {"ema_verifyx_download_speed": ema_verifyx_download_speed}},
        price_per_gpu=price_per_gpu,
    )


@pytest.mark.parametrize(
    "ema_speed, scores_zeroed, warning_fragment",
    [
        # No EMA available for a non-rented executor — zero (verifyx disabled or first-run edge case)
        (None, True, "unavailable"),
        # At or above threshold — no penalty (threshold enforcement is in VerifyXCheck, not here)
        (MIN_VERIFYX_EMA_DOWNLOAD_SPEED_MBPS, False, None),
        (120.0, False, None),
        (500.0, False, None),
    ],
)
def test_ema_verifyx_download_speed_scoring_unrented(ema_speed, scores_zeroed, warning_fragment):
    ctx = _ctx(ema_speed)
    actual_score, job_score, warning = calculate_scores(ctx, rented=False)

    if scores_zeroed:
        assert actual_score == 0.0
        assert job_score == 0.0
        assert warning_fragment in warning
    else:
        assert actual_score == 1.0
        assert job_score == 1.0
        assert warning == ""


def test_ema_download_missing_rented_does_not_zero():
    """Rented executor with missing EMA must not be zeroed — avoids killing emission on active rentals."""
    ctx = _ctx(None)
    actual_score, job_score, warning = calculate_scores(ctx, rented=True)
    assert actual_score == 1.0
    assert job_score == 1.0
    assert "unavailable" not in warning


def test_no_network_specs_unrented_zeros():
    """ctx.state.specs has no 'network' key at all — non-rented should zero."""
    ctx = _ctx_without_specs({})
    actual_score, job_score, warning = calculate_scores(ctx, rented=False)
    assert actual_score == 0.0
    assert job_score == 0.0
    assert "unavailable" in warning


def test_no_network_specs_rented_does_not_penalise():
    """ctx.state.specs has no 'network' key — rented should not zero."""
    ctx = _ctx_without_specs({})
    actual_score, job_score, warning = calculate_scores(ctx, rented=True)
    assert actual_score == 1.0
    assert job_score == 1.0
    assert warning == ""


def test_none_specs_unrented_zeros():
    """ctx.state.specs is None — non-rented should zero."""
    ctx = _ctx_without_specs(None)
    actual_score, job_score, warning = calculate_scores(ctx, rented=False)
    assert actual_score == 0.0
    assert job_score == 0.0
    assert "unavailable" in warning


def test_none_specs_rented_does_not_penalise():
    """ctx.state.specs is None — rented should not zero."""
    ctx = _ctx_without_specs(None)
    actual_score, job_score, warning = calculate_scores(ctx, rented=True)
    assert actual_score == 1.0
    assert job_score == 1.0
    assert warning == ""


@pytest.mark.parametrize("gpu_model", ["NVIDIA RTX 4090", "NVIDIA B200"])
def test_executor_without_deposit_scores_in_full(gpu_model):
    """Provider collateral is optional: an executor with nothing deposited keeps a full score, no warning."""
    ctx = make_context(
        executor=default_executor().model_copy(update={"price_per_gpu": None}),
        state=build_state(gpu_model=gpu_model, specs={"network": {"ema_verifyx_download_speed": 500.0}}),
        services=build_services(),
        config=build_context_config(),
        collateral_deposited=False,
        is_rental_succeed=True,
    )
    actual_score, job_score, warning = calculate_scores(ctx, rented=False)
    assert actual_score == 1.0
    assert job_score == 1.0
    assert warning == ""


def _attestation_ctx(*, tdx_passed: bool, gpu_passed: bool | None, gpu_model: str = "NVIDIA H200"):
    return make_context(
        executor=default_executor().model_copy(update={"price_per_gpu": None}),
        state=build_state(gpu_model=gpu_model, specs={"network": {"ema_verifyx_download_speed": 500.0}}),
        services=build_services(),
        config=build_context_config(),
        collateral_deposited=True,
        is_rental_succeed=True,
        tdx_attestation_passed=tdx_passed,
        gpu_attestation_passed=gpu_passed,
    )


def test_an_unattested_node_scores_in_full_by_default():
    actual_score, job_score, warning = calculate_scores(_attestation_ctx(tdx_passed=False, gpu_passed=None))

    assert (actual_score, job_score, warning) == (1.0, 1.0, "")


@pytest.mark.parametrize(
    "tdx_passed, gpu_passed, expected_score",
    [(True, True, 1.0), (True, None, 0.0), (True, False, 0.0), (False, None, 0.0), (False, True, 0.0)],
)
def test_the_requirement_pays_only_a_verified_quote_with_gpu_attestation(
    monkeypatch, tdx_passed, gpu_passed, expected_score
):
    monkeypatch.setattr(score_calculator.settings, "ATTESTED_HARDWARE_REQUIRED_ENABLED", True)

    actual_score, job_score, warning = calculate_scores(
        _attestation_ctx(tdx_passed=tdx_passed, gpu_passed=gpu_passed)
    )

    assert (actual_score, job_score) == (expected_score, expected_score)
    assert ("Attested hardware required" in warning) is (expected_score == 0.0)


def test_the_requirement_can_be_limited_to_listed_gpu_models(monkeypatch):
    monkeypatch.setattr(score_calculator.settings, "ATTESTED_HARDWARE_REQUIRED_ENABLED", True)
    monkeypatch.setattr(score_calculator.settings, "ATTESTED_HARDWARE_REQUIRED_GPU_MODELS", "NVIDIA H200, NVIDIA B200")

    listed = calculate_scores(_attestation_ctx(tdx_passed=False, gpu_passed=None, gpu_model="NVIDIA H200"))
    unlisted = calculate_scores(_attestation_ctx(tdx_passed=False, gpu_passed=None, gpu_model="NVIDIA RTX 4090"))

    assert listed[0] == 0.0
    assert unlisted[0] == 1.0


def test_the_attested_hardware_requirement_is_off_by_default():
    assert type(score_calculator.settings).model_fields["ATTESTED_HARDWARE_REQUIRED_ENABLED"].default is False
