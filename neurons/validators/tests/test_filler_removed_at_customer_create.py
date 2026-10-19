"""A customer's create removes every `filler_*` on the host and confirms the removal.

DAH-3706 (20 Sep 2026): 6 of 459 rented nodes carried a live `filler_*` container beside a paying
pod. The backend's filler stop is best-effort, and a stop that did not confirm left the filler on the
create's `active_container_names` — the names `clean_existing_containers` preserves. The backend no
longer lists a filler on a customer create (lium-platform, same ticket); this is the validator's half:
a CUSTOMER_RENTAL create treats every `filler_*` as stale whatever the list says (an older backend
still lists one), re-reads `docker ps -a` after the removal, and writes the typed event
`FILLER_STILL_RUNNING` for a name that survived — the sweep goes on, the event makes it countable.
DAH-3980: the running check after the customer's `docker run` lists the host once more in the same exec;
a `filler_*` there (a sweep survivor, or one created since) is removed by ID and confirmed gone, or the
create fails before it is reported RUNNING.
A FILLER create keeps protecting its listed sibling bundles (DAH-2465).
DAH-3980: the customer's rm, that re-read and the unprotected volumes' rm are one bounded SSH command.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, Mock

import asyncssh
import pytest

from payload_models.payloads import ContainerCreated, FailedContainerRequest, WorkloadKind
from test_deploy_optimizations import _patch_happy, _payload, _run, _ssh_client

import services.docker_service as ds_module
from services.docker_service import (
    FILLER_STILL_RUNNING_EVENT,
    ContainerCleanupReport,
    DockerService,
    _remove_and_list_containers_command,
    own_sweep_removals,
)


@pytest.fixture
def svc():
    # local fixtures rather than imports, which pyflakes reads as names every parameter below
    # redefines (F811); same services test_deploy_optimizations / test_docker_service build
    return DockerService(ssh_service=Mock(), redis_service=Mock(get=AsyncMock(return_value=None)), attestation_service=Mock())


@pytest.fixture
def docker_service(svc):
    return svc


@pytest.fixture
def retry_ssh_mock(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(ds_module, "retry_ssh_command", mock)
    return mock


def _listing(stdout: str, exit_status: int = 0, stderr: str = ""):
    result = Mock()
    result.exit_status = exit_status
    result.stdout = stdout
    result.stderr = stderr
    return result


def _events(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if getattr(getattr(record, "msg", None), "extra", {}).get("event")
        == FILLER_STILL_RUNNING_EVENT
    ]


def _removal(*names_after: str, rm_exit: int = 0, ps_exit: int = 0, stderr: str = ""):
    # what the one removal command prints: the rm's status, the names left, the listing's status
    listed = "".join(f"NAME\t{name}\n" for name in names_after)
    return _listing(f"RM\t{rm_exit}\n{listed}PS\t{ps_exit}\n", stderr=stderr)


async def _clean_for_customer(docker_service, ssh_client, report=None, **over):
    kwargs = dict(
        ssh_client=ssh_client,
        default_extra={"executor_uuid": "exec-1"},
        pod_name="pod_target",
        active_container_names=[],
        remove_every_filler=True,
        report=report,
    )
    return await docker_service.clean_existing_containers(**{**kwargs, **over})


@pytest.mark.asyncio
async def test_customer_create_removes_a_filler_the_backend_still_lists(
    docker_service, retry_ssh_mock
):
    ssh_client = AsyncMock()
    # before: the host listing; then the removal, whose listing shows the filler gone
    ssh_client.run = AsyncMock(
        side_effect=[
            _listing("pod_target\nfiller_unconfirmed\npod_sibling\n"),
            _removal("pod_target_new", "pod_sibling"),
        ]
    )

    removed = await _clean_for_customer(
        docker_service, ssh_client, active_container_names=["filler_unconfirmed", "pod_sibling"]
    )

    rm_command = ssh_client.run.await_args_list[1].args[0]
    assert "filler_unconfirmed" in rm_command
    assert "pod_sibling" not in rm_command
    assert sorted(removed) == ["filler_unconfirmed", "pod_target"]
    retry_ssh_mock.assert_not_called()


@pytest.mark.asyncio
async def test_a_filler_that_survives_the_sweep_is_logged_and_left_to_the_running_check(
    docker_service, retry_ssh_mock, caplog
):
    ssh_client = AsyncMock()
    # the listing after the rm still names the filler: dockerd said removed, the host says otherwise
    ssh_client.run = AsyncMock(
        side_effect=[
            _listing("pod_target\nfiller_stuck\nfiller_gone\n"),
            _removal("filler_stuck"),
        ]
    )
    report = ContainerCleanupReport()

    with caplog.at_level(logging.WARNING):
        removed = await _clean_for_customer(
            docker_service,
            ssh_client,
            report,
            active_volume_names=["volume_target", "volume_stuck", "volume_gone"],
        )

    # the sweep does not raise: the running check after `docker run` removes the survivor again or fails
    # the create (test_a_filler_that_cannot_be_removed_fails_the_customer_create); the survivor is
    # reported with typed fields
    assert "filler_stuck" in removed
    [event] = _events(caplog)
    # the create path's default_extra keys the executor as `executor_uuid` (create_container); the backend's
    # rent-path event carries `executor_uuid` too (its `executor_id` is the DB row id) — one Loki query joins on it
    assert event.msg.extra["executor_uuid"] == "exec-1"
    assert event.msg.extra["pod_name"] == "pod_target"
    assert event.msg.extra["container_names"] == ["filler_stuck"]
    assert ssh_client.run.await_count == 2
    assert report.removed_cleanly_without_volume_rm is False


async def _run_like_asyncssh(answer, open_s: float | None, command_s: float, timeout: float | None):
    # asyncssh's run(): create_process (the channel open, no timeout of its own), then wait(check, timeout)
    if open_s is None:
        await asyncio.Event().wait()  # an sshd that never confirms the channel open
    await asyncio.sleep(open_s)
    try:
        await asyncio.wait_for(asyncio.sleep(command_s), timeout)
    except TimeoutError:
        raise asyncssh.TimeoutError(None, None, None, None, None, None, "", "") from None
    return answer


_FILLER_ID, _CUSTOMER_CONTAINER_ID = "f" * 64, "c" * 64


def _customer_create_host(svc, monkeypatch, *, listing_at_running_check: str, listing_after_rm: str):
    # a customer create whose real running check sees `listing_at_running_check` beside its running container
    ssh_client = _ssh_client(inspect_exit=0)
    _patch_happy(svc, monkeypatch, ssh_client)
    monkeypatch.delattr(svc, "check_container_running")
    svc._run_rental_docker_create_with_port_retry.return_value = _CUSTOMER_CONTAINER_ID
    default_run = ssh_client.run.side_effect

    def run(cmd, *args, **kwargs):
        if cmd.startswith("/usr/bin/docker ps -q --filter"):
            return _listing(f"{_CUSTOMER_CONTAINER_ID[:12]}\n{listing_at_running_check}")
        if cmd.startswith("/usr/bin/docker rm -fv") and "printf 'RM" in cmd:
            return _listing(listing_after_rm)
        return default_run(cmd, *args, **kwargs)

    ssh_client.run.side_effect = run
    return ssh_client


def _commands(ssh_client) -> list[str]:
    return [call.args[0] for call in ssh_client.run.await_args_list]


@pytest.mark.asyncio
async def test_a_filler_created_after_the_sweep_is_removed_before_the_customer_create_succeeds(svc, monkeypatch):
    ssh_client = _customer_create_host(
        svc,
        monkeypatch,
        listing_at_running_check=f"NAME\tpod_x {_CUSTOMER_CONTAINER_ID}\nNAME\tfiller_late {_FILLER_ID}\nPS\t0\n",
        listing_after_rm=f"RM\t0\nNAME\tpod_x {_CUSTOMER_CONTAINER_ID}\nPS\t0\n",
    )

    result = await _run(svc, _payload(workload_kind=WorkloadKind.CUSTOMER_RENTAL))

    assert isinstance(result, ContainerCreated)
    assert _remove_and_list_containers_command([_FILLER_ID], []) in _commands(ssh_client)
    assert own_sweep_removals.sent_rm_for(_FILLER_ID)


@pytest.mark.asyncio
async def test_a_filler_that_cannot_be_removed_fails_the_customer_create(svc, monkeypatch):
    # a survivor of the sweep before `docker run` is still listed here, and its rm leaves it again
    ssh_client = _customer_create_host(
        svc,
        monkeypatch,
        listing_at_running_check=f"NAME\tfiller_stuck {_FILLER_ID}\nPS\t0\n",
        listing_after_rm=f"RM\t1\nNAME\tfiller_stuck {_FILLER_ID}\nPS\t0\n",
    )

    result = await _run(svc, _payload(workload_kind=WorkloadKind.CUSTOMER_RENTAL))

    assert isinstance(result, FailedContainerRequest)
    assert result.failure_step == "container_cleanup"
    assert "filler_stuck" in result.detail
    # the customer's own container is removed, as on any failed run
    assert f"/usr/bin/docker rm -fv {_CUSTOMER_CONTAINER_ID} 2>/dev/null || true" in _commands(ssh_client)


@pytest.mark.asyncio
async def test_a_clean_node_lists_fillers_in_the_running_checks_own_exec(svc, monkeypatch):
    ssh_client = _customer_create_host(
        svc,
        monkeypatch,
        listing_at_running_check=f"NAME\tpod_x {_CUSTOMER_CONTAINER_ID}\nPS\t0\n",
        listing_after_rm="",
    )

    result = await _run(svc, _payload(workload_kind=WorkloadKind.CUSTOMER_RENTAL))

    assert isinstance(result, ContainerCreated)
    commands = _commands(ssh_client)
    [running_check] = [cmd for cmd in commands if cmd.startswith("/usr/bin/docker ps -q --filter")]
    assert "/usr/bin/docker ps -a" in running_check
    # nothing after it lists or removes: the clean node's check is the one exec it was before
    after_running_check = commands[commands.index(running_check) + 1:]
    assert not [cmd for cmd in after_running_check if "docker ps" in cmd or "docker rm" in cmd]


@pytest.mark.asyncio
async def test_a_late_fillers_power_cap_is_lifted_once_it_is_confirmed_gone(svc, monkeypatch):
    # a PEARL filler capped its GPUs after the customer's last restore before `docker run`
    filler_pod_id = "0b6c2e4a-7f1d-4c3b-9a5e-2d8f6b1c4e7a"
    ssh_client = _customer_create_host(
        svc,
        monkeypatch,
        listing_at_running_check=f"NAME\tfiller_{filler_pod_id} {_FILLER_ID}\nPS\t0\n",
        listing_after_rm="RM\t0\nPS\t0\n",
    )
    steps: list[str] = []
    default_run = ssh_client.run.side_effect

    def run(cmd, *args, **kwargs):
        if cmd.startswith("/usr/bin/docker rm -fv") and "printf 'RM" in cmd:
            steps.append("filler rm")
        return default_run(cmd, *args, **kwargs)

    ssh_client.run.side_effect = run
    restore = AsyncMock(side_effect=lambda *_args, **_kwargs: steps.append("power restore"))
    monkeypatch.setattr(ds_module, "restore_filler_pod_gpu_power_limits", restore)

    result = await _run(svc, _payload(workload_kind=WorkloadKind.CUSTOMER_RENTAL))

    assert isinstance(result, ContainerCreated)
    assert restore.await_args.args[2] == filler_pod_id
    assert steps == ["filler rm", "power restore"]


@pytest.mark.asyncio
@pytest.mark.parametrize("late_step", ["running check", "second listing", "filler rm", "power restore"])
async def test_a_step_after_docker_run_waits_for_a_channel_the_host_opens_late(
    svc, monkeypatch, late_step
):
    # a cancelled channel open that the host confirms later stays open, and a MaxSessions=1 sshd then
    # refuses the failed-run cleanup's channels: the steps after `docker run` bound commands, not opens
    filler_listing = f"NAME\tfiller_late {_FILLER_ID}\nPS\t0\n"
    ssh_client = _customer_create_host(
        svc,
        monkeypatch,
        # an unread listing (`docker ps -a` exited 1) makes the check read the host once more
        listing_at_running_check="PS\t1\n" if late_step == "second listing" else filler_listing,
        listing_after_rm="RM\t0\nPS\t0\n",
    )
    default_run = ssh_client.run.side_effect

    async def run(cmd, *args, timeout=None, **kwargs):
        # the second listing, by either listing command
        if cmd == ds_module._RUNNING_CHECK_LISTING_CMD:
            return await _run_like_asyncssh(_listing(filler_listing), 0.15, 0, timeout)
        if cmd == ds_module.DOCKER_PS_ALL_NAMES_IDS_CMD:
            return await _run_like_asyncssh(_listing(f"filler_late {_FILLER_ID}\n"), 0.15, 0, timeout)
        answer = default_run(cmd, *args, **kwargs)
        is_late = (
            late_step == "running check" and cmd.startswith("/usr/bin/docker ps -q --filter")
        ) or (
            late_step == "filler rm"
            and cmd.startswith("/usr/bin/docker rm -fv")
            and "printf 'RM" in cmd
        )
        return await _run_like_asyncssh(answer, 0.15, 0, timeout) if is_late else answer

    async def restore(*_args, **_kwargs):
        if late_step == "power restore":
            await asyncio.sleep(0.15)

    ssh_client.run.side_effect = run
    monkeypatch.setattr(ds_module, "restore_filler_pod_gpu_power_limits", restore)
    monkeypatch.setattr(ds_module, "_PRERUN_HOST_PROBE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(ds_module, "_CUSTOMER_CONTAINER_REMOVAL_TIMEOUT_SECONDS", 0.05)

    result = await asyncio.wait_for(
        _run(svc, _payload(workload_kind=WorkloadKind.CUSTOMER_RENTAL)), timeout=5
    )

    assert isinstance(result, ContainerCreated)


@pytest.mark.asyncio
async def test_a_running_check_that_never_answers_fails_the_customer_create(svc, monkeypatch):
    ssh_client = _customer_create_host(svc, monkeypatch, listing_at_running_check="PS\t0\n", listing_after_rm="")
    default_run = ssh_client.run.side_effect

    async def run(cmd, *args, timeout=None, **kwargs):
        if cmd.startswith("/usr/bin/docker ps -q --filter"):
            # the channel opens, the command hangs
            return await _run_like_asyncssh(_listing(""), 0, 1, timeout)
        return default_run(cmd, *args, **kwargs)

    ssh_client.run.side_effect = run
    monkeypatch.setattr(ds_module, "_PRERUN_HOST_PROBE_TIMEOUT_SECONDS", 0.05)

    result = await asyncio.wait_for(_run(svc, _payload(workload_kind=WorkloadKind.CUSTOMER_RENTAL)), timeout=5)

    assert isinstance(result, FailedContainerRequest)
    assert result.failure_step == "container_cleanup"
    assert f"/usr/bin/docker rm -fv {_CUSTOMER_CONTAINER_ID} 2>/dev/null || true" in _commands(ssh_client)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform_state_failure", [False, True])
async def test_failed_late_filler_power_restore_fails_and_cleans_customer(svc, monkeypatch, platform_state_failure):
    # Arrange: removal succeeded, but a GPU remains capped.
    ssh_client = _customer_create_host(svc, monkeypatch,
        listing_at_running_check=f"NAME\tfiller_late {_FILLER_ID}\nPS\t0\n",
        listing_after_rm="RM\t0\nPS\t0\n")
    error_type = ds_module.GpuPowerRestoreStateError if platform_state_failure else RuntimeError
    restore = AsyncMock(side_effect=error_type("GPU power restoration incomplete"))
    monkeypatch.setattr(ds_module, "restore_filler_pod_gpu_power_limits", restore)
    # Act: the real create path must not publish RUNNING.
    result = await _run(svc, _payload(workload_kind=WorkloadKind.CUSTOMER_RENTAL))
    # Assert: use the cleanup failure and remove the customer's own container.
    assert isinstance(result, FailedContainerRequest)
    assert result.failure_step == ("gpu_power_restore_state" if platform_state_failure else "container_cleanup")
    assert restore.await_args.kwargs["require_complete"] is True
    assert f"/usr/bin/docker rm -fv {_CUSTOMER_CONTAINER_ID} 2>/dev/null || true" in _commands(ssh_client)
