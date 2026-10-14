"""DAH-4272: only the hotkey whose SSH login the node accepted this cycle writes its record.

A write or reset from any other hotkey is logged (VERIFIED_RECORD_WRITE_WITHOUT_LOGIN) and, with
VERIFIED_RECORD_OWNER_ENFORCEMENT_ENABLED on, refused. The backend's portal owner is not consulted:
a node that runs under a key other than the one the portal lists must stay verified.
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from neurons.validators.src.services import redis_service
from neurons.validators.src.services.redis_service import RedisService
from neurons.validators.src.services.task import pipeline_factory as pipeline_factory_module
from neurons.validators.src.services.task.pipeline_factory import PipelineFactory
from neurons.validators.src.services.task.result_handler import ResultHandler
from tests.helpers import build_state

VERIFIED_RECORD = {"count": 7, "failed": 0, "spec": "s", "uuids": "GPU-a"}
# b8717923: the portal lists one hotkey, the node runs (and logs in) under another of the
# same provider
B8717923 = "b8717923-0000-4000-8000-000000000000"
PORTAL_OWNER = "5GivXCUF-portal-owner"
RUNNING_HOTKEY = "5C8CG5Jk-running-hotkey"


@pytest.fixture
def service():
    service = RedisService.__new__(RedisService)
    service.hset = AsyncMock()
    service.publish = AsyncMock()
    return service


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setattr(redis_service.settings, "VERIFIED_RECORD_OWNER_ENFORCEMENT_ENABLED", True)


def _written_verified_record(service) -> dict:
    return json.loads(service.hset.await_args.args[2])


def _logs(caplog, key: str) -> int:
    return sum(key in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_a_write_and_reset_backed_by_the_login_go_through_under_enforcement(
    service, enforced, caplog
):
    with caplog.at_level(logging.WARNING, logger=redis_service.logger.name):
        await service.set_verified_job_info(
            "hotkey", "exec-1", prev_info=VERIFIED_RECORD, login_hotkey="hotkey"
        )
        assert _written_verified_record(service)["count"] == 8
        await service.clear_verified_job_info(
            "hotkey", "exec-1", prev_info=VERIFIED_RECORD, login_hotkey="hotkey"
        )

    assert _written_verified_record(service)["count"] == 0
    service.publish.assert_awaited_once()
    assert _logs(caplog, "VERIFIED_RECORD_WRITE_WITHOUT_LOGIN") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("login_hotkey", [None, "another-hotkey"])
async def test_a_write_without_the_login_is_logged_and_still_written_by_default(
    service, caplog, login_hotkey
):
    with caplog.at_level(logging.WARNING, logger=redis_service.logger.name):
        await service.clear_verified_job_info(
            "hotkey", "exec-1", prev_info=VERIFIED_RECORD, login_hotkey=login_hotkey
        )

    assert _logs(caplog, "VERIFIED_RECORD_WRITE_WITHOUT_LOGIN") == 1
    assert _written_verified_record(service) == {**VERIFIED_RECORD, "count": 0}
    service.publish.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("login_hotkey", [None, "another-hotkey"])
async def test_a_write_or_reset_without_the_login_is_refused_under_enforcement(
    service, enforced, login_hotkey
):
    await service.clear_verified_job_info(
        "hotkey", "exec-1", prev_info=VERIFIED_RECORD, login_hotkey=login_hotkey
    )
    await service.set_verified_job_info(
        "hotkey", "exec-1", prev_info=VERIFIED_RECORD, login_hotkey=login_hotkey
    )

    service.hset.assert_not_awaited()
    service.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_b8717923_stays_verified_under_the_key_it_runs_on(service, enforced, caplog):
    # the node accepts RUNNING_HOTKEY's key while the portal lists PORTAL_OWNER: its own
    # verified writes go through, enforced or not, and nothing is logged
    with caplog.at_level(logging.WARNING, logger=redis_service.logger.name):
        await service.set_verified_job_info(
            RUNNING_HOTKEY, B8717923, prev_info=VERIFIED_RECORD, login_hotkey=RUNNING_HOTKEY
        )

    assert _written_verified_record(service)["count"] == 8
    assert _logs(caplog, "VERIFIED_RECORD_WRITE_WITHOUT_LOGIN") == 0
    assert PORTAL_OWNER not in caplog.text


def test_owner_enforcement_is_off_by_default():
    fields = type(redis_service.settings).model_fields
    assert fields["VERIFIED_RECORD_OWNER_ENFORCEMENT_ENABLED"].default is False


@pytest.mark.asyncio
@pytest.mark.parametrize("success, clear", [(True, False), (False, True), (False, False)])
async def test_the_result_handler_passes_the_login_hotkey(context_factory, success, clear):
    redis = AsyncMock()
    ctx = context_factory(
        state=build_state(gpu_model_count="1x H100", gpu_uuids="GPU-a"),
        miner_hotkey=RUNNING_HOTKEY,
    ).model_copy(update={"clear_verified_job_info": clear, "ssh_login_hotkey": RUNNING_HOTKEY})

    await ResultHandler(redis_service=redis, dry_run=False)._persist_verification_data(
        RUNNING_HOTKEY, B8717923, VERIFIED_RECORD, ctx, success=success
    )

    call = redis.clear_verified_job_info if clear else redis.set_verified_job_info
    assert call.await_args.kwargs["login_hotkey"] == RUNNING_HOTKEY


@pytest.mark.asyncio
async def test_build_context_records_the_hotkey_whose_login_opened_the_shell(monkeypatch):
    # Context validates a live asyncssh connection; capture the kwargs instead
    monkeypatch.setattr(pipeline_factory_module, "Context", lambda **kw: SimpleNamespace(**kw))
    factory = PipelineFactory.__new__(PipelineFactory)
    factory.redis_service = SimpleNamespace(
        get_verified_job_info=AsyncMock(return_value={}),
        is_elem_exists_in_set=AsyncMock(return_value=False),
    )
    for name in (
        "ssh_service",
        "validation_service",
        "verifyx_validation_service",
        "inspector_validation_service",
        "executor_connectivity_service",
        "backend_client",
        "pod_recovery",
        "container_cleanup",
    ):
        setattr(factory, name, MagicMock())
    executor = SimpleNamespace(
        uuid=B8717923, address="1.2.3.4", port=8000, ssh_username="root", ssh_port=22,
        root_dir="/root/app",
    )
    encrypted_files = SimpleNamespace(
        encrypt_key="k", machine_scrape_file_name="scrape", machine_scrape_source=None,
        all_keys={}, tmp_directory="/tmp/x",
    )
    miner_info = SimpleNamespace(
        job_batch_id="b", miner_hotkey=RUNNING_HOTKEY, miner_coldkey="c",
        miner_address="1.1.1.1", miner_port=1,
    )

    ctx = await factory.build_context(
        shell=SimpleNamespace(ssh_client=MagicMock()),
        miner_info=miner_info,
        executor_info=executor,
        keypair=None,
        private_key="p",
        public_key="q",
        encrypted_files=encrypted_files,
        rented_data=None,
        default_docker_image_digests={},
    )

    assert ctx.ssh_login_hotkey == RUNNING_HOTKEY
