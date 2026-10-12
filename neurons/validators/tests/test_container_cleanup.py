"""Tests for services.container_cleanup.ContainerCleanup: the reaper takes a stale filler's own volume and no other
(DAH-3980)."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.container_cleanup import ContainerCleanup


def _ssh_mock_from_calls(call_handler):
    """Return an AsyncMock ssh_client whose .run(cmd) routes to call_handler(cmd)."""
    ssh = AsyncMock()
    ssh.run = AsyncMock(side_effect=call_handler)
    return ssh


def _make_ssh_mock(containers: list[str], ages_by_name: dict[str, int], current_ts: int = 1_000_000_000):
    """Build an ssh mock that simulates:
      - docker ps -a filter -> newline list of containers
      - docker inspect ... Created -> age seconds (current_ts - age_seconds*60)
      - date +%s -> current_ts
      - docker rm -fv / volume rm -> success
    """
    rm_calls: list[str] = []

    async def handler(cmd, *args, **kwargs):
        # docker ps -a ... --filter name=... returns the container list
        if "docker ps -a" in cmd:
            return MagicMock(exit_status=0, stdout="\n".join(containers), stderr="")
        # current time on host
        if cmd.strip() == "date +%s":
            return MagicMock(exit_status=0, stdout=str(current_ts), stderr="")
        # docker inspect --format '{{json .Created}}' | xargs -I {} date -d {} +%s
        if "docker inspect" in cmd and "Created" in cmd:
            # match which container
            for name in ages_by_name:
                if name in cmd:
                    age_min = ages_by_name[name]
                    created_ts = current_ts - int(age_min * 60)
                    return MagicMock(exit_status=0, stdout=str(created_ts), stderr="")
            return MagicMock(exit_status=1, stdout="", stderr="not found")
        # docker rm -f / docker rm -fv
        if "docker rm -f" in cmd:
            rm_calls.append(cmd)
            return MagicMock(exit_status=0, stdout="", stderr="")
        # docker volume rm
        if "docker volume rm" in cmd:
            rm_calls.append(cmd)
            return MagicMock(exit_status=0, stdout="", stderr="")
        return MagicMock(exit_status=0, stdout="", stderr="")

    return _ssh_mock_from_calls(handler), rm_calls


def _rented_data(executor_uuid: str, pods: list[str]):
    """Build a RentedExecutorsResponse-like mock."""
    pod_mocks = [MagicMock(container_name=name) for name in pods]
    executor_mock = MagicMock(pods=pod_mocks)
    resp = MagicMock()
    resp.executors = {executor_uuid: executor_mock}
    resp.filler_containers_by_executor = {}
    resp.all_filler_containers_by_executor = {}
    return resp


EXECUTOR_UUID = "00000000-0000-0000-0000-000000000000"

# A rented pod the backend lists but that is not on the host: removal is only allowed against a
# fetched, non-empty rented list, so tests that exercise a removal carry one.
LISTED_LIVE_POD = "pod_listed_live"


def _listed():
    return _rented_data(EXECUTOR_UUID, [LISTED_LIVE_POD])


def _removed_volume_names(rm_calls: list[str]) -> list[str]:
    return [c.split("docker volume rm ")[1].split()[0] for c in rm_calls if "docker volume rm" in c]


@pytest.mark.asyncio
async def test_reaper_removes_a_stale_filler_and_only_its_own_volume():
    """A filler create sweeps only filler-labelled volumes, so the reaper must take the `volume_<run id>` of a
    filler it removes, or the preallocated volume holds the disk."""
    filler = "filler_run-1"
    customer_pod = "pod_customer"
    ssh, rm_calls = _make_ssh_mock(containers=[filler, customer_pod], ages_by_name={filler: 30, customer_pod: 30})

    _, removed_names, _ = await ContainerCleanup(stale_threshold_minutes=15).cleanup(
        ssh_client=ssh, rented_data=_rented_data(EXECUTOR_UUID, [customer_pod]), executor_uuid=EXECUTOR_UUID
    )

    assert removed_names == [filler]
    assert _removed_volume_names(rm_calls) == ["volume_run-1"]


@pytest.mark.parametrize(
    ("stderr", "warned"),
    [
        ("Error response from daemon: volume is in use", True),
        ("", True),
        ("Error response from daemon: get volume_x: no such volume", False),
    ],
    ids=["named_error", "failure_without_output", "volume_already_gone"],
)
@pytest.mark.asyncio
async def test_a_failed_filler_volume_rm_is_logged_unless_the_volume_is_gone(caplog, stderr: str, warned: bool):
    filler = "filler_run-1"
    ssh, _ = _make_ssh_mock(containers=[filler], ages_by_name={filler: 30})
    plain_handler = ssh.run.side_effect

    async def handler(cmd, *args, **kwargs):
        if "docker volume rm" in cmd:
            # a command ending in `|| true` hides the failure from the caller, as the shell would
            return MagicMock(exit_status=0 if cmd.rstrip().endswith("|| true") else 1, stdout="", stderr=stderr)
        return await plain_handler(cmd, *args, **kwargs)

    ssh.run.side_effect = handler

    with caplog.at_level(logging.WARNING, logger="services.container_cleanup"):
        await ContainerCleanup(stale_threshold_minutes=15).cleanup(
            ssh_client=ssh, rented_data=_listed(), executor_uuid=EXECUTOR_UUID
        )

    logged = any(f"Removed container {filler} but not its volume" in r.getMessage() for r in caplog.records)
    assert logged is warned
