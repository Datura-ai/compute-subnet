"""DAH-2475: the periodic backstop that gives the DPHN filler cache back under disk pressure.

The create-time sweep only removes STALE cache versions and never reclaims — a node's free disk is
low precisely BECAUSE the cache is downloaded, so reclaiming there would make the backend grant it
again next cycle and the node would re-download ~37 GB forever. This backstop is the one place that
reclaims, and it decides on real free space: below the rental listing floor the node earns nothing.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.container_cleanup import DPHN_CACHE_RECLAIM_FREE_FLOOR_GB, ContainerCleanup

GB = 1024**3


class FakeSshClient:
    """`docker volume rm` ends in `|| true`, so exit status says nothing about what was removed —
    the fake must model the host's actual volume list, not just echo success."""

    def __init__(
        self,
        containers: list[str],
        volumes: list[str],
        free_gb: float,
        refuses: set[str] | None = None,
        docker_root: str = "/var/lib/docker",
    ):
        self._containers = "\n".join(containers)
        self._volumes = list(volumes)
        self._refuses = refuses or set()
        self._free_bytes = str(int(free_gb * GB))
        self.docker_root = docker_root
        self.commands: list[str] = []

    async def run(self, command: str, *args, **kwargs):
        self.commands.append(command)
        if "docker info" in command:
            stdout = self.docker_root
        elif "df -P -B1" in command:
            # Only answer for the real data root: a node with docker on a dedicated disk would
            # otherwise be judged by the root filesystem, which says nothing about docker's space.
            avail = self._free_bytes if self.docker_root.strip() in command else "999999999999"
            stdout = f"Filesystem 1B-blocks Used Available Capacity\n/dev/sda1 1 1 {avail} 1% /hostfs"
        elif "volume rm" in command:
            # dockerd refuses a volume referenced by any container, in any state.
            for name in _removal_targets(command):
                if name not in self._refuses and name in self._volumes:
                    self._volumes.remove(name)
            stdout = ""
        elif "volume ls" in command:
            stdout = "\n".join(self._volumes)
        elif "docker ps" in command:
            # Only `docker ps` without -a lists RUNNING containers; the reclaim guard uses that one.
            stdout = "" if "-a" in command else self._containers
        else:
            stdout = ""
        return SimpleNamespace(exit_status=0, stdout=stdout, stderr="")


def _removal_targets(command: str) -> list[str]:
    return command.split("volume rm ", 1)[1].split(" 2>/dev/null")[0].split()


def _removed(ssh_client: FakeSshClient) -> list[str]:
    removals = [c for c in ssh_client.commands if "volume rm" in c]
    return sorted(_removal_targets(removals[0])) if removals else []


@pytest.fixture
def cleanup() -> ContainerCleanup:
    return ContainerCleanup()


@pytest.mark.asyncio
async def test_reclaims_the_cache_when_free_disk_is_below_the_listing_floor(cleanup):
    ssh_client = FakeSshClient(
        containers=[], volumes=["dphn_cache_hf_x", "dphn_cache_dp_x", "volume_pod"], free_gb=40
    )

    removed = await cleanup.reclaim_dphn_cache_when_disk_is_tight(ssh_client, "exec-1")

    assert removed == 2
    assert _removed(ssh_client) == ["dphn_cache_dp_x", "dphn_cache_hf_x"]


@pytest.mark.asyncio
async def test_keeps_the_cache_when_the_node_is_comfortably_above_the_floor(cleanup):
    ssh_client = FakeSshClient(
        containers=[], volumes=["dphn_cache_hf_x"], free_gb=DPHN_CACHE_RECLAIM_FREE_FLOOR_GB + 500
    )

    assert await cleanup.reclaim_dphn_cache_when_disk_is_tight(ssh_client, "exec-1") == 0
    assert _removed(ssh_client) == []


@pytest.mark.asyncio
async def test_never_reclaims_while_a_filler_container_is_live(cleanup):
    # A cache a sibling still mounts is in use, and a node still running fillers is not the stranded
    # node this backstop exists to rescue.
    ssh_client = FakeSshClient(containers=["filler_abc"], volumes=["dphn_cache_hf_x"], free_gb=10)

    assert await cleanup.reclaim_dphn_cache_when_disk_is_tight(ssh_client, "exec-1") == 0
    assert _removed(ssh_client) == []


@pytest.mark.asyncio
async def test_no_cache_on_the_host_is_a_no_op(cleanup):
    ssh_client = FakeSshClient(containers=[], volumes=["volume_pod"], free_gb=10)

    assert await cleanup.reclaim_dphn_cache_when_disk_is_tight(ssh_client, "exec-1") == 0
    assert _removed(ssh_client) == []


@pytest.mark.asyncio
async def test_dry_run_reports_without_removing(cleanup):
    dry = ContainerCleanup(dry_run=True)
    ssh_client = FakeSshClient(containers=[], volumes=["dphn_cache_hf_x"], free_gb=10)

    assert await dry.reclaim_dphn_cache_when_disk_is_tight(ssh_client, "exec-1") == 0
    assert _removed(ssh_client) == []


@pytest.mark.asyncio
async def test_the_disk_is_measured_on_the_real_docker_data_root(cleanup):
    # Nodes can move docker's data-root to a dedicated disk. Measuring /var/lib/docker there reads the
    # root filesystem instead: the reclaim then never fires while docker's disk is full, or fires and
    # deletes a healthy cache the node had room for. The rest of the codebase already discovers the
    # root dynamically.
    ssh_client = FakeSshClient(
        containers=[], volumes=["dphn_cache_hf_x"], free_gb=10, docker_root="/mnt/docker-data"
    )

    assert await cleanup.reclaim_dphn_cache_when_disk_is_tight(ssh_client, "exec-1") == 1
    assert any("/mnt/docker-data" in command for command in ssh_client.commands), (
        "the reclaim measured a filesystem that is not docker's data root"
    )


@pytest.mark.asyncio
async def test_a_refused_removal_is_never_reported_as_reclaimed(cleanup):
    # `|| true` swallows dockerd's refusal, so counting the volumes we ASKED to remove reports a
    # rescue that never happened — and the node stays delisted while telemetry says it was saved.
    ssh_client = FakeSshClient(
        containers=[], volumes=["dphn_cache_hf_x"], free_gb=10, refuses={"dphn_cache_hf_x"}
    )

    assert await cleanup.reclaim_dphn_cache_when_disk_is_tight(ssh_client, "exec-1") == 0


@pytest.mark.asyncio
async def test_only_the_volumes_that_actually_went_away_are_counted(cleanup):
    ssh_client = FakeSshClient(
        containers=[],
        volumes=["dphn_cache_hf_x", "dphn_cache_dp_x"],
        free_gb=10,
        refuses={"dphn_cache_dp_x"},
    )

    assert await cleanup.reclaim_dphn_cache_when_disk_is_tight(ssh_client, "exec-1") == 1


@pytest.mark.asyncio
async def test_a_stopped_filler_does_not_block_the_reclaim(cleanup):
    # `docker ps -a` would list a long-exited filler_* forever and wedge the guard; the check must
    # look at RUNNING containers only.
    ssh_client = FakeSshClient(containers=[], volumes=["dphn_cache_hf_x"], free_gb=10)

    assert await cleanup.reclaim_dphn_cache_when_disk_is_tight(ssh_client, "exec-1") == 1
    assert _removed(ssh_client) == ["dphn_cache_hf_x"]
    assert not any("ps -a" in c for c in ssh_client.commands), "guard must not use `docker ps -a`"


# A closed rental's leftover volume, removed once the node is hidden for lack of disk.
POD = "11111111-2222-3333-4444-555555555555"
LIVE = "66666666-7777-8888-9999-000000000000"


class DanglingAwareSsh(FakeSshClient):
    def __init__(self, dangling: list[str], used_gb: float = 1, **kwargs):
        super().__init__(**kwargs)
        self._dangling = dangling
        self._used = str(int(used_gb * GB))

    async def run(self, command: str, *args, **kwargs):
        if "dangling=true" in command:
            self.commands.append(command)
            return SimpleNamespace(exit_status=0, stdout="\n".join(self._dangling), stderr="")
        if "df -P -B1" in command:
            self.commands.append(command)
            line = f"/dev/sda1 1 {self._used} {self._free_bytes} 1% /hostfs"
            return SimpleNamespace(exit_status=0, stdout=f"Filesystem 1B-blocks Used Available Capacity\n{line}", stderr="")
        return await super().run(command, *args, **kwargs)


def _snapshot(pod_ids: list[str]):
    from protocol.vc_protocol.compute_requests import RentedExecutor, RentedExecutorsResponse, RentedPod

    pods = [RentedPod(pod_id=pod_id, container_name=f"pod_{pod_id}") for pod_id in pod_ids]
    executor = RentedExecutor(miner_hotkey="m", executor_ip_address="1.2.3.4", executor_ip_port="1", pods=pods)
    return RentedExecutorsResponse(executors={"exec-1": executor})


async def _ended(pod_id: str) -> bool:
    return pod_id == POD


@pytest.mark.asyncio
async def test_removes_only_an_unlisted_dangling_ended_rental_volume_when_disk_is_low(cleanup):
    volumes = [f"volume_{POD}", f"volume_{LIVE}", "volume_custom", "dphn_cache_x"]
    ssh_client = DanglingAwareSsh(dangling=volumes, containers=[], volumes=volumes, free_gb=40)
    assert await cleanup.remove_ended_rental_volumes_when_disk_is_tight(ssh_client, _snapshot([LIVE]), "exec-1", _ended) == 1
    assert _removed(ssh_client) == [f"volume_{POD}"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dangling, listed, free_gb, used_gb, is_ended",
    [
        ([], [], 40, 1, _ended),  # mounted by a container, even a stopped one
        ([f"volume_{POD}"], [POD], 40, 1, _ended),  # the snapshot still lists the pod
        ([f"volume_{POD}"], [], 500, 1, _ended),  # the node is over the listing floor
        ([f"volume_{POD}"], [], 40, 1, AsyncMock(return_value=False)),  # backend: not closed
    ],
)
async def test_keeps_every_volume_that_is_not_a_long_ended_unmounted_rental(cleanup, dangling, listed, free_gb, used_gb, is_ended):
    volumes = [f"volume_{POD}"]
    ssh_client = DanglingAwareSsh(dangling=dangling, used_gb=used_gb, containers=[], volumes=volumes, free_gb=free_gb)
    assert await cleanup.remove_ended_rental_volumes_when_disk_is_tight(ssh_client, _snapshot([LIVE, *listed]), "exec-1", is_ended) == 0
    assert _removed(ssh_client) == []


@pytest.mark.asyncio
async def test_an_empty_snapshot_removes_no_volume(cleanup):
    ssh_client = DanglingAwareSsh(dangling=[f"volume_{POD}"], containers=[], volumes=[f"volume_{POD}"], free_gb=40)
    assert await cleanup.remove_ended_rental_volumes_when_disk_is_tight(ssh_client, None, "exec-1", _ended) == 0
    assert _removed(ssh_client) == []


@pytest.mark.asyncio
async def test_a_disk_over_ninety_percent_full_counts_as_low_even_with_the_free_floor_met(cleanup):
    ssh_client = DanglingAwareSsh(
        dangling=[f"volume_{POD}"], used_gb=1900, containers=[], volumes=[f"volume_{POD}"], free_gb=150
    )
    assert await cleanup.remove_ended_rental_volumes_when_disk_is_tight(ssh_client, _snapshot([LIVE]), "exec-1", _ended) == 1
