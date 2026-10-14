import json
import logging
from unittest.mock import AsyncMock

import pytest
from neurons.validators.src.services import redis_service
from neurons.validators.src.services.redis_service import RedisService

OWNED = {"count": 7, "failed": 0, "spec": "s", "uuids": "GPU-a", "hotkey": "owner-hotkey"}


@pytest.fixture
def service():
    service = RedisService.__new__(RedisService)
    service.hset = AsyncMock()
    service.publish = AsyncMock()
    return service


def _written(service) -> dict:
    return json.loads(service.hset.await_args.args[2])


def _foreign_writer_logs(caplog) -> int:
    return sum("VERIFIED_RECORD_FOREIGN_WRITER" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_the_first_writer_becomes_the_record_owner(service):
    await service.set_verified_job_info("owner-hotkey", "exec-1", prev_info={}, spec="s")

    assert _written(service)["hotkey"] == "owner-hotkey"


@pytest.mark.asyncio
async def test_a_reset_keeps_the_record_owner(service):
    await service.clear_verified_job_info("owner-hotkey", "exec-1", prev_info=OWNED)

    assert _written(service)["hotkey"] == "owner-hotkey"


@pytest.mark.asyncio
async def test_a_foreign_reset_is_logged_and_still_written_by_default(service, caplog):
    with caplog.at_level(logging.WARNING, logger=redis_service.logger.name):
        await service.clear_verified_job_info("other-hotkey", "exec-1", prev_info=OWNED)

    assert _foreign_writer_logs(caplog) == 1
    assert _written(service) == {**OWNED, "count": 0}
    service.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_foreign_reset_is_refused_under_enforcement(service, monkeypatch):
    monkeypatch.setattr(redis_service.settings, "VERIFIED_RECORD_OWNER_ENFORCEMENT_ENABLED", True)

    await service.clear_verified_job_info("other-hotkey", "exec-1", prev_info=OWNED)
    await service.set_verified_job_info("other-hotkey", "exec-1", prev_info=OWNED)

    service.hset.assert_not_awaited()
    service.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_owner_still_writes_under_enforcement(service, monkeypatch, caplog):
    monkeypatch.setattr(redis_service.settings, "VERIFIED_RECORD_OWNER_ENFORCEMENT_ENABLED", True)

    with caplog.at_level(logging.WARNING, logger=redis_service.logger.name):
        await service.set_verified_job_info("owner-hotkey", "exec-1", prev_info=OWNED)

    assert _written(service)["count"] == 8
    assert _foreign_writer_logs(caplog) == 0


def test_owner_enforcement_is_off_by_default():
    fields = type(redis_service.settings).model_fields
    assert fields["VERIFIED_RECORD_OWNER_ENFORCEMENT_ENABLED"].default is False
