from __future__ import annotations

import asyncio
import hashlib
import os


def sha256_from_path(file_path: str) -> str:
    with open(file_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


async def sha256_from_executor(shell, file_path: str, *, max_retries: int = 2) -> str:
    for attempt in range(1, max_retries + 1):
        try:
            checksum = await shell.get_sha256_checksum_by_path(file_path)
            return checksum.strip()
        except Exception:
            if attempt < max_retries:
                await asyncio.sleep(1.0)
    return ""


ARM_VERIFIER_NOT_AVAILABLE = "ARM_VERIFIER_NOT_AVAILABLE"
ARM_VERIFIER_NOT_AVAILABLE_ERROR = (
    f"{ARM_VERIFIER_NOT_AVAILABLE}: executor CPU is aarch64; the ARM verifier library is not available yet"
)
_ARM = ("aarch64", "arm64")


async def executor_arch(shell) -> str:
    """Return "aarch64" for an ARM executor (`uname -m`, read once per shell), else "x86_64", the default."""
    arch = getattr(shell, "executor_arch", None)
    if not isinstance(arch, str):
        try:
            result = await shell.ssh_client.run("uname -m", timeout=15)
            arch = str(getattr(result, "stdout", "") or "").strip()
        except Exception:
            arch = ""
        arch = "aarch64" if arch in _ARM else "x86_64"
        shell.executor_arch = arch
    return arch


def aarch64_lib_path(lib_path: str) -> str | None:
    """The validator's aarch64 build of `lib_path` (`<lib>-aarch64.so` next to it), for ARM executors
    (GB300 Grace hosts). None while that build is not on this validator."""
    path = lib_path.removesuffix(".so") + "-aarch64.so"
    return path if os.path.exists(path) else None
