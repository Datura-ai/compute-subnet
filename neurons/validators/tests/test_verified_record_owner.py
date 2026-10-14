import json
import logging
from unittest.mock import AsyncMock

import pytest
from neurons.validators.src.services import redis_service
from neurons.validators.src.services.redis_service import RedisService
from tests.helpers import default_executor

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


ENDPOINT = "203.0.113.7:2200"
OWNED_AT = {**OWNED, "ssh_endpoint": ENDPOINT}


@pytest.mark.asyncio
async def test_the_owner_records_its_ssh_endpoint(service):
    await service.set_verified_job_info(
        "owner-hotkey", "exec-1", prev_info=OWNED, endpoint=ENDPOINT
    )

    assert _written(service)["ssh_endpoint"] == ENDPOINT


@pytest.mark.asyncio
async def test_a_reset_from_the_recorded_endpoint_takes_over(service, monkeypatch):
    # the node moved to a new hotkey: same machine, same endpoint, new hotkey
    monkeypatch.setattr(redis_service.settings, "VERIFIED_RECORD_OWNER_ENFORCEMENT_ENABLED", True)

    await service.clear_verified_job_info(
        "new-hotkey", "exec-1", prev_info=OWNED_AT, endpoint=ENDPOINT
    )

    written = _written(service)
    assert written["hotkey"] == "new-hotkey"
    assert written["ssh_endpoint"] == ENDPOINT
    assert written["count"] == 0
    service.publish.assert_awaited_once()
    assert service.publish.await_args.args[1]["miner_hotkey"] == "new-hotkey"


@pytest.mark.asyncio
async def test_a_reset_from_another_endpoint_is_refused_under_enforcement(service, monkeypatch):
    monkeypatch.setattr(redis_service.settings, "VERIFIED_RECORD_OWNER_ENFORCEMENT_ENABLED", True)

    await service.clear_verified_job_info(
        "other-hotkey", "exec-1", prev_info=OWNED_AT, endpoint="198.51.100.9:2200"
    )

    service.hset.assert_not_awaited()
    service.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_record_without_an_endpoint_cannot_be_taken_over(service, monkeypatch):
    monkeypatch.setattr(redis_service.settings, "VERIFIED_RECORD_OWNER_ENFORCEMENT_ENABLED", True)

    await service.clear_verified_job_info(
        "new-hotkey", "exec-1", prev_info=OWNED, endpoint=ENDPOINT
    )

    service.hset.assert_not_awaited()
    service.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_plain_write_from_the_recorded_endpoint_is_still_refused(service, monkeypatch):
    # only a reset takes over; the moved node's successful writes wait for that reset
    monkeypatch.setattr(redis_service.settings, "VERIFIED_RECORD_OWNER_ENFORCEMENT_ENABLED", True)

    await service.set_verified_job_info(
        "new-hotkey", "exec-1", prev_info=OWNED_AT, endpoint=ENDPOINT
    )

    service.hset.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_foreign_write_let_through_keeps_owner_and_endpoint(service):
    await service.clear_verified_job_info(
        "other-hotkey", "exec-1", prev_info=OWNED_AT, endpoint="198.51.100.9:2200"
    )

    written = _written(service)
    assert written["hotkey"] == "owner-hotkey"
    assert written["ssh_endpoint"] == ENDPOINT


@pytest.mark.asyncio
@pytest.mark.parametrize("ssh_port, expected", [(2200, "203.0.113.7:2200"), (0, "")])
async def test_the_result_handler_passes_the_endpoint_it_logged_in_on(
    context_factory, ssh_port, expected
):
    # a forced pass (ssh_port 0) logged in nowhere and proves no endpoint
    from neurons.validators.src.services.task.result_handler import ResultHandler

    executor = default_executor().model_copy(
        update={"address": " 203.0.113.7 ", "ssh_port": ssh_port}
    )
    ctx = context_factory(executor=executor).model_copy(update={"clear_verified_job_info": True})
    redis = AsyncMock()

    await ResultHandler(redis_service=redis, dry_run=False)._persist_verification_data(
        "owner-hotkey", "exec-1", OWNED_AT, ctx, success=False
    )

    assert redis.clear_verified_job_info.await_args.kwargs["endpoint"] == expected
