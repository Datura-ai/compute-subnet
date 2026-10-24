"""Restart the executor container when a stale containerd task keeps it down (DAH-5101).

If the executor dies while its containerd shim is still alive (a host IO stall can leave the shim blocked
for minutes), every restart fails with `failed to create task for container: AlreadyExists: task <id>:
already exists`. Docker's restart policy then stops retrying and autoheal only restarts running containers,
so the node stays offline until someone kills the shim by hand. The monitor container (pid: host, docker
socket) runs this check every minute: it kills only the shim that carries this container's own id, and its
children, then starts the container. A shim that cannot be killed yet is retried on the next pass.
"""

import os
import re
import signal

EXECUTOR_SERVICE_LABEL = "com.docker.compose.service=executor"
_ALREADY_EXISTS = re.compile(r"AlreadyExists: task ([0-9a-f]{64})")


def stale_task_id(container_id: str, status: str, error: str) -> str | None:
    """The container's own id when it is down because its old task still exists, else None."""
    match = _ALREADY_EXISTS.search(error or "")
    if status not in ("exited", "created") or not match or match.group(1) != container_id:
        return None
    return container_id


def _cmdline(proc_root: str, pid: str) -> list[str]:
    try:
        with open(os.path.join(proc_root, pid, "cmdline"), "rb") as file:
            return [arg.decode(errors="replace") for arg in file.read().split(b"\0") if arg]
    except OSError:
        return []


def _parent_pid(proc_root: str, pid: str) -> str | None:
    try:
        with open(os.path.join(proc_root, pid, "stat")) as file:
            # the comm field may hold spaces; ppid is the 2nd field after its closing parenthesis
            return file.read().rpartition(")")[2].split()[1]
    except (OSError, IndexError):
        return None


def shim_pids(task_id: str, proc_root: str = "/proc") -> list[int]:
    """The containerd shim holding `task_id` and all its descendants, shim first."""
    pids = [pid for pid in os.listdir(proc_root) if pid.isdigit()]
    shims = []
    for pid in pids:
        argv = _cmdline(proc_root, pid)
        if (
            argv
            and os.path.basename(argv[0]).startswith("containerd-shim")
            and task_id in _id_args(argv)
        ):
            shims.append(pid)
    found = list(shims)
    parent_of = {pid: _parent_pid(proc_root, pid) for pid in pids}
    for parent in found:  # grows while iterating: breadth-first over the shims' descendants
        found.extend(pid for pid, ppid in parent_of.items() if ppid == parent and pid not in found)
    return [int(pid) for pid in found]


def _id_args(argv: list[str]) -> set[str]:
    return {argv[index + 1] for index, arg in enumerate(argv[:-1]) if arg == "-id"}


def recover_stale_executor_task(client, proc_root: str = "/proc", kill=os.kill) -> str | None:
    """One pass: start an executor container held down by its own stale task. Returns what was done."""
    for container in client.containers.list(all=True, filters={"label": EXECUTOR_SERVICE_LABEL}):
        state = container.attrs.get("State", {})
        task_id = stale_task_id(container.id, state.get("Status", ""), state.get("Error", ""))
        if task_id is None:
            continue
        for pid in shim_pids(task_id, proc_root):
            try:
                kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        container.start()
        return f"killed stale containerd task {task_id[:12]} and started {container.name}"
    return None
