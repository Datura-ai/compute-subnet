import logging

import pytest
from neurons.validators.src.services.task.checks import verifyx
from neurons.validators.src.services.task.checks.verifyx import MAX_PACKAGE_FALLBACK_PROBES

from tests.test_verifyx_capacity_probe import (
    HOST_CAPACITY_MBPS,
    HOST_UPLOAD_MBPS,
    _cloudflare_unreachable_payload,
    _judge,
    _probe_payload,
    _run_check,
)


async def _download_emas(context_factory, payloads: list[dict]) -> list[float]:
    emas = [HOST_CAPACITY_MBPS]
    for payload in payloads:
        result = await _run_check(
            context_factory, _judge(payload), prev_ema=emas[-1], prev_upload=HOST_UPLOAD_MBPS
        )
        emas.append(result.updates["state"].specs["network"]["ema_verifyx_download_speed"])
    return emas[1:]


def _streak_logs(caplog) -> int:
    return sum(
        "VERIFYX_PACKAGE_FALLBACK_STREAK" in record.getMessage() for record in caplog.records
    )


@pytest.mark.asyncio
async def test_a_long_package_fallback_streak_is_logged_and_changes_no_ema_by_default(
    context_factory, caplog
):
    blocked = [_cloudflare_unreachable_payload()] * (MAX_PACKAGE_FALLBACK_PROBES + 2)

    with caplog.at_level(logging.WARNING, logger=verifyx.logger.name):
        emas = await _download_emas(context_factory, blocked)

    assert _streak_logs(caplog) == 2
    # every probe fed the package reading: the EMA settles on it, above the floor, and is never fed a 0
    assert emas[-1] > emas[-2] / 2
    assert min(emas) > verifyx.MIN_VERIFYX_EMA_DOWNLOAD_SPEED_MBPS


@pytest.mark.asyncio
async def test_under_the_cap_the_missing_capacity_reading_is_fed_as_zero(
    context_factory, monkeypatch
):
    monkeypatch.setattr(verifyx.settings, "VERIFYX_PACKAGE_FALLBACK_CAP_ENABLED", True)
    blocked = [_cloudflare_unreachable_payload()] * (MAX_PACKAGE_FALLBACK_PROBES + 1)

    emas = await _download_emas(context_factory, blocked)

    assert emas[-1] == pytest.approx(emas[-2] / 2)


@pytest.mark.asyncio
async def test_a_measured_probe_starts_the_count_again(context_factory, monkeypatch, caplog):
    monkeypatch.setattr(verifyx.settings, "VERIFYX_PACKAGE_FALLBACK_CAP_ENABLED", True)
    blocked = [_cloudflare_unreachable_payload()] * MAX_PACKAGE_FALLBACK_PROBES

    with caplog.at_level(logging.WARNING, logger=verifyx.logger.name):
        await _download_emas(context_factory, blocked + [_probe_payload()] + blocked)

    assert _streak_logs(caplog) == 0


def test_the_package_fallback_cap_is_off_by_default():
    fields = type(verifyx.settings).model_fields
    assert fields["VERIFYX_PACKAGE_FALLBACK_CAP_ENABLED"].default is False
