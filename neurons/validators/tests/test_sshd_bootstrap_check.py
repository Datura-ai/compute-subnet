from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from protocol.vc_protocol.compute_requests import RentedExecutor, RentedExecutorsResponse, RentedPod

from services.docker_service import DockerService
from services.redis_service import SSHD_BOOTSTRAP_FAILED_PREFIX, SSHD_BOOTSTRAP_FAILED_TTL_SECONDS
from services.task.checks.sshd_bootstrap import SshdBootstrapCheck
from services.task.messages import SshdBootstrapMessages as Msg
from tests.helpers import FakeRedis, build_services, build_state

EXECUTOR = "executor-123"
KEY = f"{SSHD_BOOTSTRAP_FAILED_PREFIX}:{EXECUTOR}"
FILLER = "filler_run-1"


def _ctx(context_factory, redis, rented=False, filler_owner=None, ssh=None, backend=None):
    filler_fields = (
        {
            "all_filler_containers_by_executor": {EXECUTOR: [FILLER]},
            "default_job_owner_by_executor": {EXECUTOR: filler_owner},
        }
        if filler_owner
        else {}
    )
    rented_data = RentedExecutorsResponse(
        **filler_fields,
        executors={
            EXECUTOR: RentedExecutor(
                miner_hotkey="m",
                executor_ip_address="127.0.0.1",
                executor_ip_port="22",
                pods=[RentedPod(pod_id="p", container_name="c")],
            )
        }
        if rented
        else {}
    )
    services = build_services(redis=redis, **({"backend": backend} if backend else {}))
    return context_factory(state=build_state(rented_data=rented_data), services=services, ssh=ssh)


@pytest.mark.asyncio
async def test_a_stamped_unrented_node_fails(context_factory):
    redis = FakeRedis()
    redis.store[KEY] = "1"
    result = await SshdBootstrapCheck().run(_ctx(context_factory, redis))
    assert result.passed is False
    assert result.event.reason_code == Msg.FAILED.reason


@pytest.mark.asyncio
async def test_a_stamped_node_running_our_filler_fails_and_the_filler_is_removed(context_factory):
    # the broken nodes always run a filler (its failed create is the stamp): no exemption, the filler goes
    redis = FakeRedis()
    redis.store[KEY] = "1"
    ssh = AsyncMock()
    ssh.run.return_value = SimpleNamespace(exit_status=0, stdout="", stderr="")
    backend = build_services().backend
    result = await SshdBootstrapCheck().run(
        _ctx(context_factory, redis, filler_owner="lium", ssh=ssh, backend=backend)
    )
    assert result.passed is False
    assert result.event.reason_code == Msg.FAILED.reason
    assert ssh.run.await_args.args[0] == f"/usr/bin/docker rm -fv {FILLER}"
    backend.get_filler_run_active.assert_awaited_once_with("run-1", container_missing=True)
    assert result.event.what_we_saw["removed_fillers"] == [FILLER]


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,remove_fillers", [("miner", True), ("lium", False)])
async def test_a_miners_own_job_or_a_dry_run_keeps_the_filler(context_factory, owner, remove_fillers):
    redis = FakeRedis()
    redis.store[KEY] = "1"
    ssh = AsyncMock()
    result = await SshdBootstrapCheck(remove_fillers=remove_fillers).run(
        _ctx(context_factory, redis, filler_owner=owner, ssh=ssh)
    )
    assert result.passed is False
    ssh.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_filler_removal_keeps_the_verdict(context_factory):
    redis = FakeRedis()
    redis.store[KEY] = "1"
    ssh = AsyncMock()
    ssh.run.side_effect = OSError("connection lost")
    backend = build_services().backend
    result = await SshdBootstrapCheck().run(
        _ctx(context_factory, redis, filler_owner="lium", ssh=ssh, backend=backend)
    )
    assert result.passed is False
    assert result.event.what_we_saw["removed_fillers"] == []
    backend.get_filler_run_active.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_redis_error_passes_the_node(context_factory):
    result = await SshdBootstrapCheck().run(_ctx(context_factory, FakeRedis(failing=True)))
    assert result.passed is True
    assert result.event.reason_code == Msg.OK.reason


@pytest.mark.asyncio
async def test_kill_switch_off_only_logs(context_factory):
    redis = FakeRedis()
    redis.store[KEY] = "1"
    with patch(
        "services.task.checks.sshd_bootstrap.settings.SSHD_BOOTSTRAP_ENFORCEMENT_ENABLED", False
    ):
        result = await SshdBootstrapCheck().run(_ctx(context_factory, redis))
    assert result.passed is True
    assert result.event.reason_code == Msg.FAILED_OBSERVED.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("stamped,rented", [(False, False), (True, True)])
async def test_no_stamp_or_a_rented_node_passes(context_factory, stamped, rented):
    redis = FakeRedis()
    if stamped:
        redis.store[KEY] = "1"
    result = await SshdBootstrapCheck().run(_ctx(context_factory, redis, rented=rented))
    assert result.passed is True
    assert result.event.reason_code == Msg.OK.reason


def _service(redis) -> DockerService:
    service = DockerService(ssh_service=Mock(), redis_service=redis, attestation_service=Mock())
    service.stream_log = AsyncMock()
    return service


async def _bootstrap(service, run_result, executor_uuid=EXECUTOR):
    ok = SimpleNamespace(exit_status=0, stdout="", stderr="")
    with patch(
        "services.docker_service.exec_logged_rental_docker_sdk_operation",
        AsyncMock(side_effect=[ok, run_result]),
    ):
        return await service.install_open_ssh_server_and_start_ssh_service_with_rental_docker(
            Mock(),
            container_name="filler_x",
            log_tag="t",
            log_extra={},
            sshd_verdict_executor_uuid=executor_uuid,
        )


@pytest.mark.asyncio
async def test_sshd_not_serving_stamps_and_a_later_success_clears():
    redis = FakeRedis()
    service = _service(redis)
    failed = SimpleNamespace(
        exit_status=1, stdout="sshd verification failed after 30s\n", stderr=""
    )

    assert await _bootstrap(service, failed) is False
    assert redis.store[KEY] == "1"
    assert redis.ttl[KEY] == SSHD_BOOTSTRAP_FAILED_TTL_SECONDS

    assert await _bootstrap(service, SimpleNamespace(exit_status=0, stdout="", stderr="")) is True
    assert KEY not in redis.store


@pytest.mark.asyncio
async def test_other_failures_and_renter_creates_stamp_nothing():
    redis = FakeRedis()
    service = _service(redis)
    # apt-get failing exits before verify_sshd: not evidence about the node
    assert (
        await _bootstrap(
            service, SimpleNamespace(exit_status=100, stdout="E: Unable to fetch", stderr="")
        )
        is False
    )
    assert (
        await _bootstrap(
            service,
            SimpleNamespace(exit_status=1, stdout="sshd verification failed", stderr=""),
            executor_uuid=None,
        )
        is False
    )
    assert KEY not in redis.store
