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
import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import aiohttp
import pytest

from clients.compute_client import ComputeClient
from datura.requests.miner_requests import AcceptSSHKeyRequest, ExecutorSSHInfo
from payload_models.payloads import (
    ContainerCreateRequest,
    CustomOptions,
    PayloadPortMapping,
    WorkloadKind,
)
from services import docker_service as docker_service_module
from services.docker_service import customer_creates
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


@pytest.mark.asyncio
async def test_create_request_delegates_to_create_container(mocker, miner_service):
    """A ContainerCreateRequest is delegated to create_container, and
    miner_service itself no longer calls wait_for_port_check_containers
    (removal moved into create_container, after the pending-pod flag)."""
    executor_id = str(uuid4())
    payload = _make_create_payload(executor_id)
    _wire_common_mocks(mocker, miner_service, executor_id)

    wait_mock = mocker.patch(
        "services.miner_service.DockerService.wait_for_port_check_containers",
        AsyncMock(return_value=(True, "No port check containers found")),
    )
    order: list[str] = []

    async def create(*args, **kwargs):
        order.append("create")
        return Mock()

    create_mock = mocker.patch(
        "services.miner_service.DockerService.create_container",
        AsyncMock(side_effect=create),
    )
    exclusion = miner_service.redis_service.executor_create_exclusion
    exclusion.return_value.__aenter__.side_effect = lambda *a: order.append("lock")
    exclusion.return_value.__aexit__.side_effect = lambda *a: order.append("unlock")

    await miner_service._handle_container(payload)

    create_mock.assert_awaited_once()
    # First positional arg to create_container is the create payload.
    assert create_mock.call_args.args[0] is payload
    # DAH-3436 (review): the create ran inside the per-executor create lock the rental probe shares
    exclusion.assert_called_once_with(executor_id)
    assert order == ["lock", "create", "unlock"]
    # No pre-flag port-check removal in miner_service anymore.
    wait_mock.assert_not_awaited()


@pytest.mark.parametrize("miner_lists_the_executor", [True, False], ids=["reached_create_container", "executor_left_out"])
@pytest.mark.asyncio
async def test_create_reply_does_not_wait_for_the_validator_key_removal(
    mocker, miner_service, caplog, miner_lists_the_executor
):
    """A miner holding or failing the key removal cannot hold or fail the created pod's reply; the
    customer create counts against fillers on its executor while it runs and until its reply, and refuses a filler that
    started before it only once it reached create_container; the connector's shutdown waits for the removal."""
    executor_id = str(uuid4())
    payload = _make_create_payload(executor_id)
    _wire_common_mocks(mocker, miner_service, executor_id)
    accept = AcceptSSHKeyRequest(executors=[_make_executor_info(executor_id)] if miner_lists_the_executor else [])
    mocker.patch("services.miner_service._parse_miner_response", return_value=accept)
    created = Mock()
    customer_create_counted_while_it_ran: list[bool] = []

    async def create_container(*args) -> Mock:
        customer_create_counted_while_it_ran.append(customer_creates.is_running(payload.miner_hotkey, executor_id))
        return created

    mocker.patch("services.miner_service.DockerService.create_container", create_container)
    removal_released = asyncio.Event()
    if not miner_lists_the_executor:
        removal_released.set()  # an early exit removes the key before its reply, as on main

    async def rest_removal_held_by_the_miner(**kwargs) -> bool:
        await removal_released.wait()
        return False

    mocker.patch.object(miner_service, "_remove_ssh_key_via_rest", rest_removal_held_by_the_miner)
    filler = payload.model_copy(update={"workload_kind": WorkloadKind.FILLER})

    with caplog.at_level("INFO"), customer_creates.track(filler):
        result = await asyncio.wait_for(miner_service.handle_container(payload), 5)
        counted_after_the_reply = customer_creates.is_running(payload.miner_hotkey, executor_id)
        filler_refused_after_the_reply = customer_creates.ran_since_filler_started(filler)
        connector = ComputeClient.__new__(ComputeClient)
        connector.miner_drivers = asyncio.Queue()
        connector.miner_driver_awaiter_task = asyncio.create_task(connector.miner_driver_awaiter())
        asyncio.get_running_loop().call_later(0.05, removal_released.set)
        await asyncio.wait_for(connector.__aexit__(None, None, None), 5)

    assert (result is created, filler_refused_after_the_reply) == (miner_lists_the_executor, miner_lists_the_executor)
    assert (customer_create_counted_while_it_ran, counted_after_the_reply) == ([True] * miner_lists_the_executor, False)
    if not miner_lists_the_executor:
        return
    assert any("Validator SSH key removal after reply finished" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_cancelled_key_removal_after_the_reply_is_logged(mocker, miner_service, caplog):
    """A delete's wait timing out, or a shutdown, can leave the validator's key at the miner: say so."""
    executor_id = str(uuid4())
    payload = _make_create_payload(executor_id)
    my_key = _wire_common_mocks(mocker, miner_service, executor_id)
    mocker.patch("services.miner_service.DockerService.create_container", AsyncMock(return_value=Mock()))

    async def removal_that_hangs(**kwargs) -> bool:
        await asyncio.Event().wait()
        return True

    mocker.patch.object(miner_service, "_remove_ssh_key_via_rest", removal_that_hangs)

    with caplog.at_level("INFO"):
        await miner_service._handle_container(payload)
        await asyncio.sleep(0)
        steps_finished = await docker_service_module.create_steps_after_reply.wait_until_done(payload.pod_id, 0.01)

    assert steps_finished is False
    [cancelled] = [r for r in caplog.records if "Validator SSH key removal after reply cancelled" in r.getMessage()]
    assert cancelled.levelname == "WARNING"
    assert str(my_key) not in cancelled.getMessage()


@pytest.mark.asyncio
async def test_a_step_stuck_after_its_cancel_does_not_hold_the_wait_past_its_bound():
    """A cancelled step stuck in its own cleanup (a `docker rm` on a hung dockerd) must not hold a delete or an edit."""
    registry = docker_service_module._CreateStepsAfterReplyRegistry()

    async def step_stuck_in_its_cleanup() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.Event().wait()

    registry.start("pod-1", step_stuck_in_its_cleanup())
    [stuck_step] = registry._tasks_by_pod_id["pod-1"]
    await asyncio.sleep(0)

    try:
        steps_finished = await asyncio.wait_for(registry.wait_until_done("pod-1", 0.05), 1)
    finally:
        stuck_step.cancel()

    assert steps_finished is False


# ---------------------------------------------------------------------------
# DAH-3338: an executor the miner KNOWS but could not reach is ExecutorUnreachable;
# InvalidExecutorId is kept for an id the miner does not list at all, worded "the miner no longer
# lists this executor" (DAH-4481); a miner without known_executor_ids keeps DAH-3508's wording.
# ---------------------------------------------------------------------------

from payload_models.payloads import FailedContainerErrorCodes, FailedContainerRequest
from services.miner_service import _missing_executor_failure


def _wire_missing_executor(mocker, miner_service, executor_id: str, known: list[str] | None):
    my_key = Mock(ss58_address="validator-hotkey")
    my_key.sign.return_value = b"\x01\x02\x03"
    mocker.patch(
        "core.config.Settings.get_bittensor_wallet",
        return_value=Mock(get_hotkey=Mock(return_value=my_key)),
    )
    # The miner accepted the key on none of its executors.
    accept_msg = AcceptSSHKeyRequest(executors=[], known_executor_ids=known)
    mocker.patch.object(
        miner_service,
        "_make_rest_request",
        AsyncMock(return_value=(200, {"message_type": "AcceptSSHKeyRequest"})),
    )
    mocker.patch("services.miner_service._parse_miner_response", return_value=accept_msg)
    remove_key = mocker.patch.object(miner_service, "_remove_ssh_key_via_rest", AsyncMock())
    return remove_key


@pytest.mark.parametrize(
    "known,expected_code,expected_headline",
    [
        pytest.param(
            "the requested id",
            FailedContainerErrorCodes.ExecutorUnreachable,
            "Error: Executor unreachable",
            id="known-but-not-accepted",
        ),
        pytest.param(
            [str(uuid4())],
            FailedContainerErrorCodes.InvalidExecutorId,
            "Error: the miner no longer lists this executor",
            id="not-known",
        ),
        pytest.param(
            None,
            FailedContainerErrorCodes.InvalidExecutorId,
            "Error: no executor accepted the SSH key",
            id="old-miner-without-the-field",
        ),
    ],
)
@pytest.mark.asyncio
async def test_rest_twin_splits_unreachable_from_invalid_executor_id(
    mocker, miner_service, known, expected_code, expected_headline
):
    executor_id = str(uuid4())
    if known == "the requested id":
        known = [executor_id]
    payload = _make_create_payload(executor_id)
    remove_key = _wire_missing_executor(mocker, miner_service, executor_id, known)

    result = await miner_service._handle_container(payload)

    assert isinstance(result, FailedContainerRequest)
    assert result.error_code == expected_code
    assert result.msg == expected_headline
    # The key the miner accepted nowhere is still withdrawn, as before.
    remove_key.assert_awaited_once()


def test_missing_executor_failure_reads_known_executor_ids():
    executor_id = str(uuid4())
    known = AcceptSSHKeyRequest(executors=[], known_executor_ids=[executor_id])
    unknown = AcceptSSHKeyRequest(executors=[], known_executor_ids=[])
    old_miner = AcceptSSHKeyRequest(executors=[])

    extra = {"miner_hotkey": "m", "executor_id": executor_id}
    unreachable = _missing_executor_failure(known, executor_id, extra)
    assert unreachable[1] is FailedContainerErrorCodes.ExecutorUnreachable
    assert str(unreachable[0]) == "Error: Executor unreachable"
    assert _missing_executor_failure(unknown, executor_id, extra)[1] is FailedContainerErrorCodes.InvalidExecutorId
    assert _missing_executor_failure(old_miner, executor_id, extra)[1] is FailedContainerErrorCodes.InvalidExecutorId
    # DAH-4481: a miner that lists its executors but not this one says so, instead of blaming the SSH key.
    not_listed = _missing_executor_failure(unknown, executor_id, extra)[0]
    assert str(not_listed) == "Error: the miner no longer lists this executor"
    assert not_listed.extra["reason_code"] == "EXECUTOR_NOT_LISTED_BY_MINER"
    # A miner without known_executor_ids keeps DAH-3508's wording, not the old "Invalid executor id".
    assert str(_missing_executor_failure(old_miner, executor_id, extra)[0]) == "Error: no executor accepted the SSH key"


def _storage_payload(kind, engine="restic", **extra):
    from payload_models.payloads import BackupContainerRequest, ExternalVolumeInfo, RestoreContainerRequest

    common = dict(
        miner_hotkey="miner", executor_id="executor", pod_id="pod", auth_token="token", backup_engine=engine,
        backup_volume_info=ExternalVolumeInfo(name="b", plugin="s3", iam_user_access_key="a", iam_user_secret_key="s"),
    )
    if kind == "backup":
        return BackupContainerRequest(
            **common, source_volume="v", backup_path="/root", source_volume_path="/root", backup_target_path="",
            backup_log_id=str(uuid4()), **extra,
        )
    return RestoreContainerRequest(
        **common, target_volume="v", backup_source_path="b", target_volume_path="/root", restore_path="/root",
        restore_log_id=str(uuid4()), **extra,
    )


@pytest.mark.parametrize(
    "kind,engine,extra,reported",
    [
        ("backup", "restic", {}, True),
        ("backup", "tar_aws_cli", {}, False),
        ("restore", "restic", {}, True),
        # a reconnect re-dispatch may find the executor down while the first runner still works; never fail it
        ("restore", "restic", {"recovery": True}, False),
    ],
)
@pytest.mark.asyncio
async def test_storage_op_that_fails_before_its_runner_starts_is_reported_at_once(
    mocker, miner_service, kind, engine, extra, reported
):
    """An unreachable executor used to leave the operation IN_PROGRESS until the 20-min heartbeat sweep."""
    payload = _storage_payload(kind, engine, **extra)
    failed = FailedContainerRequest(
        miner_hotkey="miner", executor_id="executor", pod_id="pod", msg="Error: Executor unreachable",
        error_code=FailedContainerErrorCodes.ExecutorUnreachable,
    )
    mocker.patch.object(miner_service, "_route_container", AsyncMock(return_value=failed))
    report = mocker.patch("services.miner_service.report_launch_failure", AsyncMock())

    assert await miner_service.handle_container(payload) is failed

    if not reported:
        report.assert_not_awaited()
        return
    operation_id, spec, error = report.await_args.args
    assert str(operation_id) == (payload.backup_log_id if kind == "backup" else payload.restore_log_id)
    assert spec["reporter"]["auth_token"] == "token" and spec["reporter"]["resource"] == kind
    assert str(error) == "Error: Executor unreachable"


@pytest.mark.asyncio
async def test_an_old_executor_image_reports_its_specific_reason(mocker, miner_service):
    payload = _storage_payload("backup", exclude_patterns=["*.cache"])
    ssh = MagicMock()
    ssh.__aenter__ = AsyncMock(return_value=Mock())
    ssh.__aexit__ = AsyncMock(return_value=False)
    mocker.patch("services.miner_service.asyncssh.connect", return_value=ssh)
    mocker.patch("services.miner_service.public_host_sock", AsyncMock(return_value={}))
    mocker.patch("services.miner_service.supports_workspace_field", AsyncMock(return_value=False))
    start = mocker.patch("services.miner_service.start_storage_operation", AsyncMock())
    report = mocker.patch("services.miner_service.report_launch_failure", AsyncMock())
    executor = Mock(address="h", ssh_port=22, ssh_username="root", python_path="python3")

    with pytest.raises(RuntimeError, match="exclude_patterns"):
        await miner_service.handle_backup_container_req(executor, payload, Mock())

    start.assert_not_awaited()
    assert "update the executor image" in str(report.await_args.args[2])

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
