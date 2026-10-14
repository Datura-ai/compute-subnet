"""DAH-4299: the spot fallback average over every GPU count of a base model."""

from types import SimpleNamespace

import pytest

from core.config import settings
from protocol.vc_protocol.compute_requests import FillerRevenueByGpuConfig
from services.task.result_handler import model_filler_revenue_per_gpu_hour


def _rented(*entries) -> SimpleNamespace:
    return SimpleNamespace(
        filler_revenue_by_gpu_config=[
            FillerRevenueByGpuConfig(
                base_model=model, gpu_count=count, usd_per_gpu_hour=usd, gpu_hours=hours
            )
            for model, count, usd, hours in entries
        ]
    )


def test_the_model_average_is_weighted_by_gpu_hours_over_every_count(monkeypatch):
    monkeypatch.setattr(settings, "FILLER_REVENUE_MIN_GPU_HOURS", 24.0)
    rented = _rented(("B200", 8, 2.0, 30.0), ("B200", 1, 4.0, 10.0), ("H200", 8, 9.0, 99.0))

    assert model_filler_revenue_per_gpu_hour(rented, "B200") == pytest.approx(2.5)


def test_thin_samples_pool_up_to_the_floor(monkeypatch):
    monkeypatch.setattr(settings, "FILLER_REVENUE_MIN_GPU_HOURS", 24.0)
    rented = _rented(("B200", 8, 2.0, 12.0), ("B200", 4, 2.0, 12.0))

    assert model_filler_revenue_per_gpu_hour(rented, "B200") == pytest.approx(2.0)


def test_a_model_under_the_sample_floor_has_no_average(monkeypatch):
    monkeypatch.setattr(settings, "FILLER_REVENUE_MIN_GPU_HOURS", 24.0)
    rented = _rented(("B200", 8, 2.0, 5.0))

    assert model_filler_revenue_per_gpu_hour(rented, "B200") is None


def test_an_unknown_model_has_no_average():
    assert model_filler_revenue_per_gpu_hour(_rented(("B200", 8, 2.0, 99.0)), None) is None
