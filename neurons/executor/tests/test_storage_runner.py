from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from storage.models import StorageOperationSpec
from storage.restic import ENCRYPTED_BACKUP_SCRIPT, ResticOperationError, ResticStorageRunner
from storage.workspace import LocalWorkspace

OPERATION_ID = UUID("11111111-1111-4111-8111-111111111111")
POD_ID = UUID("22222222-2222-4222-8222-222222222222")
SNAPSHOT_ID = "a" * 64


def _operation_payload(
    *,
    action: str = "backup",
    mode: str = "plain_volume",
    requested_path: str = "/root/checkpoints",
    bootstrap: bool | None = None,
) -> dict[str, object]:
    workspace: dict[str, object] = {
        "mode": mode,
        "volume_name": "customer-volume",
        "volume_path": "/root",
        "requested_path": requested_path,
    }
    if mode == "encrypted_running":
        workspace["container_name"] = "rental-pod"
    if bootstrap is not None:
        workspace["bootstrap"] = bootstrap
    return {
        "operation_id": str(OPERATION_ID),
        "pod_id": str(POD_ID),
        "action": action,
        "snapshot_id": SNAPSHOT_ID if action == "restore" else None,
        "progress_interval_seconds": 0,
        "repository": {
            "bucket": "backup-bucket",
            "access_key_id": "access-key",
            "secret_access_key": "secret-key",
            "session_token": "session-token",
            "password": "repository-password",
        },
        "workspace": workspace,
    }


class _FakePopen:
    def __init__(self, command: list[str], *args: object, **kwargs: object) -> None:
        self.command = command
        self.cwd = kwargs.get("cwd")
        self.stdout = iter(
            [
                '{"message_type":"status","percent_done":0.5}\n',
                f'{{"message_type":"summary","snapshot_id":"{SNAPSHOT_ID}"}}\n',
            ]
        )

    def wait(self) -> int:
        return 3

    def kill(self) -> None:
        return None


def test_encrypted_backup_script_stops_when_the_backup_path_is_missing(tmp_path: Path) -> None:
    marker = tmp_path / "ran"

    completed = subprocess.run(
        ["/bin/sh", "-c", ENCRYPTED_BACKUP_SCRIPT, "sh", str(tmp_path / "missing"), "touch", str(marker)],
        capture_output=True,
    )

    assert completed.returncode == 66
    assert not marker.exists()
    # the reporter keeps the first diagnostic, so sh's own cd error must not reach it ahead of ours
    assert completed.stderr == b""


class _MissingSourcePopen(_FakePopen):
    def __init__(self, command: list[str], *args: object, **kwargs: object) -> None:
        super().__init__(command, *args, **kwargs)
        self.stdout = iter(["sh: cd: can't cd to /workspace/missing: No such file or directory\n"])

    def wait(self) -> int:
        return 66


def test_backup_of_a_missing_path_fails_with_a_clear_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    operation = StorageOperationSpec.from_mapping(_operation_payload())
    runner = ResticStorageRunner(operation, LocalWorkspace(tmp_path))
    monkeypatch.setattr(
        "storage.restic.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="[]", stderr=""),
    )
    monkeypatch.setattr("storage.restic.subprocess.Popen", _MissingSourcePopen)

    with pytest.raises(ResticOperationError, match="backup path does not exist"):
        runner._backup()


def test_local_backup_of_a_missing_directory_fails_before_restic_runs(tmp_path: Path) -> None:
    operation = StorageOperationSpec.from_mapping(_operation_payload())
    runner = ResticStorageRunner(operation, LocalWorkspace(tmp_path / "missing"))

    with pytest.raises(ResticOperationError, match="backup path does not exist"):
        runner._backup()
