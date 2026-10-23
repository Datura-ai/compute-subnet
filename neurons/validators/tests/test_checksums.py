from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from neurons.validators.src.core.checksums import sha256_from_executor
from neurons.validators.src.services.interactive_shell_service import InteractiveShellService


VALID_SHA256 = "a" * 64


@pytest.mark.asyncio
async def test_sha256_from_executor_uses_remote_hash_command():
    class Shell:
        async def get_sha256_checksum_by_path(self, _path: str) -> str:
            return f"{VALID_SHA256}\n"

    checksum = await sha256_from_executor(Shell(), "/usr/lib/libinspector.so")

    assert checksum == VALID_SHA256


@pytest.mark.asyncio
async def test_interactive_shell_service_parses_sha256sum_stdout():
    service = InteractiveShellService(
        host="127.0.0.1",
        username="root",
        private_key="key",
        port=22,
    )
    service.ssh_client = SimpleNamespace(
        run=AsyncMock(
            return_value=SimpleNamespace(
                stdout=f"{VALID_SHA256}  /usr/lib/libinspector.so\n",
                exit_status=0,
            )
        )
    )

    checksum = await service.get_sha256_checksum_by_path("/usr/lib/libinspector.so")

    assert checksum == VALID_SHA256
    service.ssh_client.run.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(stdout="", exit_status=0),
        SimpleNamespace(stdout=f"{'a' * 63} /usr/lib/libinspector.so\n", exit_status=0),
        SimpleNamespace(stdout="", exit_status=1),
    ],
)
async def test_interactive_shell_service_rejects_bad_sha256sum_output(result):
    service = InteractiveShellService(
        host="127.0.0.1",
        username="root",
        private_key="key",
        port=22,
    )
    service.ssh_client = SimpleNamespace(run=AsyncMock(return_value=result))

    checksum = await service.get_sha256_checksum_by_path("/usr/lib/libinspector.so")

    assert checksum == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stdout", "arch"),
    [("aarch64\n", "aarch64"), ("arm64\n", "aarch64"), ("x86_64\n", "x86_64"), ("", "x86_64"), (None, "x86_64")],
)
async def test_executor_arch_reads_uname_once_and_defaults_to_x86_64(stdout, arch):
    from neurons.validators.src.core.checksums import executor_arch

    shell = SimpleNamespace(ssh_client=SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout=stdout))))
    assert await executor_arch(shell) == arch
    assert await executor_arch(shell) == arch
    shell.ssh_client.run.assert_awaited_once_with("uname -m", timeout=15)


@pytest.mark.asyncio
async def test_executor_arch_is_x86_64_when_the_read_fails():
    from neurons.validators.src.core.checksums import executor_arch

    shell = SimpleNamespace(ssh_client=SimpleNamespace(run=AsyncMock(side_effect=OSError("closed"))))
    assert await executor_arch(shell) == "x86_64"


def test_aarch64_lib_path_only_when_the_build_is_on_the_validator(tmp_path):
    from neurons.validators.src.core.checksums import aarch64_lib_path

    lib = tmp_path / "libverifyx.so"
    assert aarch64_lib_path(str(lib)) is None
    (tmp_path / "libverifyx-aarch64.so").write_bytes(b"arm")
    assert aarch64_lib_path(str(lib)) == str(tmp_path / "libverifyx-aarch64.so")
