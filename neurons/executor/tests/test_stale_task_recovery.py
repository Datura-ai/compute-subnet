"""DAH-5101: the monitor restarts an executor held down by its own stale containerd task."""

import signal
from types import SimpleNamespace

from services.stale_task_recovery import recover_stale_executor_task

OWN = "a" * 64
OTHER = "b" * 64


def make_proc(tmp_path, processes):
    """processes: pid -> (ppid, argv)."""
    for pid, (ppid, argv) in processes.items():
        directory = tmp_path / str(pid)
        directory.mkdir()
        (directory / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
        (directory / "stat").write_text(f"{pid} (some name) S {ppid} 1 1")
    return str(tmp_path)


class FakeContainer:
    def __init__(self, container_id, status, error):
        self.id = container_id
        self.name = "executor-executor-1"
        self.attrs = {"State": {"Status": status, "Error": error}}
        self.started = False

    def start(self):
        self.started = True


def client_with(container):
    return SimpleNamespace(containers=SimpleNamespace(list=lambda **_: [container]))


PROCESSES = {
    100: (
        1,
        [
            "/usr/bin/containerd-shim-runc-v2",
            "-namespace",
            "moby",
            "-id",
            OWN,
            "-address",
            "/run/containerd.sock",
        ],
    ),
    101: (100, ["/sbin/docker-init", "--", "/root/app/run.sh"]),
    102: (101, ["pdm", "run", "python", "src/executor.py"]),
    200: (1, ["/usr/bin/containerd-shim-runc-v2", "-namespace", "moby", "-id", OTHER]),
    201: (200, ["python", "train.py"]),
    300: (1, ["bash", "-c", f"echo -id {OWN}"]),
}


def test_kills_only_its_own_shim_and_children_then_starts_the_executor(tmp_path):
    container = FakeContainer(
        OWN,
        "exited",
        f"failed to create task for container: AlreadyExists: task {OWN}: already exists",
    )
    killed = []

    done = recover_stale_executor_task(
        client_with(container),
        make_proc(tmp_path, PROCESSES),
        kill=lambda pid, sig: killed.append((pid, sig)),
    )

    assert sorted(killed) == [(100, signal.SIGKILL), (101, signal.SIGKILL), (102, signal.SIGKILL)]
    assert container.started
    assert done and "started executor-executor-1" in done


def test_another_containers_task_id_or_another_error_is_left_alone(tmp_path):
    proc_root = make_proc(tmp_path, PROCESSES)
    for container in (
        FakeContainer(OWN, "exited", f"AlreadyExists: task {OTHER}: already exists"),
        FakeContainer(OWN, "exited", "OCI runtime create failed"),
        FakeContainer(OWN, "running", f"AlreadyExists: task {OWN}: already exists"),
    ):
        killed = []

        done = recover_stale_executor_task(
            client_with(container), proc_root, kill=lambda pid, sig: killed.append(pid)
        )

        assert done is None and killed == [] and not container.started
