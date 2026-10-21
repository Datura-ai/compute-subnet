"""DAH-2272: MinerService._handle_container create-request path.

No existing test drove MinerService._handle_container before this; the harness
below mocks every boundary it crosses (bittensor wallet, REST submit, redis
renting_in_progress, ssh decrypt) so the create branch actually executes.

DAH-2272 removed the pre-flag port-check force-remove that used to live here —
probe removal now happens entirely inside create_container, AFTER the
pending-pod flag is set, so a probe the rental kills is covered by
PortConnectivityCheck's renting_in_progress tolerate. This test therefore pins
that a ContainerCreateRequest is delegated to create_container and that
miner_service no longer makes an early wait_for_port_check_containers call.
"""
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import aiohttp
import pytest

from datura.requests.miner_requests import AcceptSSHKeyRequest, ExecutorSSHInfo
from payload_models.payloads import (
    ContainerCreateRequest,
    CustomOptions,
    PayloadPortMapping,
)
from services.miner_service import MinerService


def _make_executor_info(executor_id: str) -> ExecutorSSHInfo:
    return ExecutorSSHInfo(
        uuid=executor_id,
        address="127.0.0.1",
        port=8001,
        ssh_username="root",
        ssh_port=2200,
        python_path="/usr/bin/python3",
        root_dir="/root/app",
        port_range="9100-9130",
    )


def _make_create_payload(executor_id: str) -> ContainerCreateRequest:
    return ContainerCreateRequest(
        miner_hotkey="5TestMiner",
        executor_id=executor_id,
        miner_address="10.0.0.1",
        miner_port=8000,
        pod_id=str(uuid4()),
        docker_image="daturaai/pytorch:test",
        user_public_keys=["ssh-ed25519 test-key"],
        gpu_uuids=["GPU-test"],
        cpu_count=2,
        memory_gb=8,
        custom_options=CustomOptions(volumes=[], environment={}, startup_commands=None, shm_size="1g"),
        volume_limit_gb=100,
        storage_limit_gb=50,
        available_ports=[PayloadPortMapping(internal_port=22, external_port=30022)],
        pod_mapping=[],
        active_container_names=[],
        active_volume_names=[],
    )


@pytest.fixture
def miner_service(mocker):
    ssh_service = mocker.Mock()
    ssh_service.generate_ssh_key.return_value = (b"---PRIV---", b"ssh-ed25519 pub")
    ssh_service.decrypt_payload.return_value = "---DECRYPTED-PRIV---"

    redis_service = mocker.AsyncMock()
    redis_service.renting_in_progress = AsyncMock(return_value=False)
    redis_service.remove_rented_machine = AsyncMock()
    # the per-executor create lock held around create_container (DAH-3436): an async context manager
    redis_service.executor_create_exclusion = MagicMock()

    svc = MinerService(
        ssh_service=ssh_service,
        task_service=mocker.Mock(),
        redis_service=redis_service,
        attestation_service=mocker.Mock(),
    )
    return svc


def _wire_common_mocks(mocker, miner_service, executor_id: str):
    """Patch every boundary between MinerService._handle_container's entry and
    the create_container call, so the create branch is reached deterministically."""
    my_key = Mock(ss58_address="validator-hotkey")
    my_key.sign.return_value = b"\x01\x02\x03"
    my_key.get_hotkey = Mock(return_value=my_key)

    # `settings` is a pydantic Settings *instance* — patch the method on the
    # class (get_bittensor_wallet is a plain method, not a pydantic field).
    mocker.patch(
        "core.config.Settings.get_bittensor_wallet",
        return_value=Mock(get_hotkey=Mock(return_value=my_key)),
    )

    accept_msg = AcceptSSHKeyRequest(executors=[_make_executor_info(executor_id)])
    mocker.patch.object(
        miner_service,
        "_make_rest_request",
        AsyncMock(return_value=(200, {"message_type": "AcceptSSHKeyRequest"})),
    )
    mocker.patch("services.miner_service._parse_miner_response", return_value=accept_msg)
    mocker.patch("services.miner_service.asyncssh.import_private_key", return_value=Mock())

    return my_key


# ---------------------------------------------------------------------------
# DAH-3338: an executor the miner KNOWS but could not reach is ExecutorUnreachable;
# InvalidExecutorId is kept for an id the miner does not list at all, worded "the miner no longer
# lists this executor" (DAH-4481); a miner without known_executor_ids keeps DAH-3508's wording.
# ---------------------------------------------------------------------------



def _refused():
    return aiohttp.ClientConnectorError(Mock(host="10.0.0.1", port=8000, ssl=None), OSError(111, "Connect call failed"))


@pytest.mark.asyncio
async def test_a_create_waits_out_a_miner_that_refuses_connections_while_it_restarts(mocker, miner_service):
    # a miner redeploy refuses connections for about a minute; a create used to fail on the first refusal
    executor_id = str(uuid4())
    _wire_common_mocks(mocker, miner_service, executor_id)
    accepted = (200, {"message_type": "AcceptSSHKeyRequest"})
    miner_service._make_rest_request.side_effect = [_refused(), _refused(), accepted]
    mocker.patch("services.miner_service.asyncio.sleep", AsyncMock())
    stamps = iter(range(100))
    mocker.patch.object(miner_service, "_generate_auth_headers", side_effect=lambda *_: {"X-Timestamp": next(stamps)})
    create_mock = mocker.patch("services.miner_service.DockerService.create_container", AsyncMock(return_value=Mock()))

    await miner_service._handle_container(_make_create_payload(executor_id))

    create_mock.assert_awaited_once()
    # the miner rejects a signed timestamp older than 10 s, so every attempt must be signed anew
    sent = [c.kwargs["headers"]["X-Timestamp"] for c in miner_service._make_rest_request.await_args_list[:3]]
    assert sent == [0, 1, 2]


@pytest.mark.asyncio
async def test_a_create_gives_up_once_the_miner_stays_unreachable_past_the_wait(mocker, miner_service):
    executor_id = str(uuid4())
    _wire_common_mocks(mocker, miner_service, executor_id)
    miner_service._make_rest_request.side_effect = _refused()
    mocker.patch("services.miner_service.MINER_RESTART_WAIT_S", 0)
    create_mock = mocker.patch("services.miner_service.DockerService.create_container", AsyncMock())

    result = await miner_service._handle_container(_make_create_payload(executor_id))

    create_mock.assert_not_awaited()
    assert result.msg == "Resulted in an exception"
