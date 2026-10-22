"""setup_dirty_cap.sh (DAH-4978): the executor caps the host's dirty page cache so a container
removal never waits for a host-sized flush. The script is run against a fixture /proc tree under
DIRTY_CAP_SYS_ROOT; the kernel's own behaviour (writers throttled at the cap) is not simulated.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "setup_dirty_cap.sh"
GIB = 1024**3
TARGET = GIB
TARGET_BG = GIB // 4


@pytest.fixture
def sys_root(tmp_path: Path) -> Path:
    vm = tmp_path / "proc" / "sys" / "vm"
    vm.mkdir(parents=True)
    (vm / "dirty_bytes").write_text("0\n")
    (vm / "dirty_background_bytes").write_text("0\n")
    set_dirty(tmp_path, 100 * 1024**2)
    return tmp_path


def set_dirty(sys_root: Path, dirty_bytes: int, writeback_bytes: int = 0) -> None:
    (sys_root / "proc" / "meminfo").write_text(
        f"MemTotal:       4294967296 kB\nDirty:          {dirty_bytes // 1024} kB\n"
        f"Writeback:      {writeback_bytes // 1024} kB\n"
    )


def cap(sys_root: Path) -> tuple[int, int]:
    vm = sys_root / "proc" / "sys" / "vm"
    return int((vm / "dirty_bytes").read_text()), int((vm / "dirty_background_bytes").read_text())


def run_setup(sys_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), "setup"],
        env={**os.environ, "DIRTY_CAP_SYS_ROOT": str(sys_root)},
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_idle_host_gets_the_target_cap_at_once(sys_root: Path) -> None:
    completed = run_setup(sys_root)

    assert completed.returncode == 0, completed.stderr
    assert cap(sys_root) == (TARGET, TARGET_BG)
    assert "[dirty-cap] dirty_bytes=1073741824 dirty_background_bytes=268435456" in completed.stdout


def test_host_with_a_big_backlog_is_capped_just_above_its_dirty_pages(sys_root: Path) -> None:
    # a cap below the current Dirty would freeze every writer on the host until the backlog drains
    set_dirty(sys_root, 500 * GIB)

    completed = run_setup(sys_root)

    assert completed.returncode == 0, completed.stderr
    assert cap(sys_root) == (550 * GIB, 550 * GIB // 4)
    assert "the janitor keeps lowering it" in completed.stdout


def test_repeated_steps_lower_the_cap_as_dirty_falls_and_never_raise_it(sys_root: Path) -> None:
    set_dirty(sys_root, 500 * GIB)
    run_setup(sys_root)

    set_dirty(sys_root, 600 * GIB)
    run_setup(sys_root)
    assert cap(sys_root)[0] == 550 * GIB

    set_dirty(sys_root, 2 * GIB)
    run_setup(sys_root)
    assert cap(sys_root)[0] == 2 * GIB + 2 * GIB // 10

    set_dirty(sys_root, 300 * 1024**2)
    run_setup(sys_root)
    assert cap(sys_root) == (TARGET, TARGET_BG)


def test_pages_already_in_writeback_count_toward_the_safe_cap(sys_root: Path) -> None:
    # writers are throttled on Dirty + Writeback, so a slow disk still draining 10 GiB must not
    # see a 1 GiB cap yet
    set_dirty(sys_root, 100 * 1024**2, writeback_bytes=10 * GIB)

    run_setup(sys_root)

    pending = 100 * 1024**2 + 10 * GIB
    assert cap(sys_root)[0] == pending + pending // 10


def test_a_backlog_past_two_tebibytes_is_still_read_as_an_integer(sys_root: Path) -> None:
    # the kB sum passes 2^31 here; mawk would print it as 3.2e+09 without printf
    set_dirty(sys_root, 3 * 1024 * GIB)

    completed = run_setup(sys_root)

    assert completed.returncode == 0, completed.stdout
    assert cap(sys_root)[0] == 3 * 1024 * GIB + 3 * 1024 * GIB // 10


def test_half_applied_setup_is_completed_on_the_next_pass(sys_root: Path) -> None:
    # the state a pass leaves when its second write fails: dirty_bytes set, background still 0
    # (seeded directly: a chmod-based failure would not hold for root, CAP_DAC_OVERRIDE)
    (sys_root / "proc" / "sys" / "vm" / "dirty_bytes").write_text(f"{TARGET}\n")

    completed = run_setup(sys_root)

    assert completed.returncode == 0
    assert cap(sys_root) == (TARGET, TARGET_BG)
    assert "dirty_background_bytes=268435456" in completed.stdout


def test_a_providers_lower_cap_is_left_alone(sys_root: Path) -> None:
    (sys_root / "proc" / "sys" / "vm" / "dirty_bytes").write_text(f"{GIB // 2}\n")
    (sys_root / "proc" / "sys" / "vm" / "dirty_background_bytes").write_text(f"{GIB // 16}\n")

    completed = run_setup(sys_root)

    assert completed.returncode == 0
    assert cap(sys_root) == (GIB // 2, GIB // 16)
    assert "dirty_bytes=" not in completed.stdout


def test_janitor_reasserts_the_cap_after_a_provider_reset(sys_root: Path) -> None:
    run_setup(sys_root)
    janitor = subprocess.Popen(
        ["bash", str(SCRIPT), "janitor"],
        env={**os.environ, "DIRTY_CAP_SYS_ROOT": str(sys_root), "DIRTY_CAP_HOLD_SECONDS": "0.2", "DIRTY_CAP_RAMP_SECONDS": "0.2"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # vm.dirty_ratio written by the provider's config management zeroes dirty_bytes
        (sys_root / "proc" / "sys" / "vm" / "dirty_bytes").write_text("0\n")
        deadline = time.monotonic() + 5
        while cap(sys_root)[0] != TARGET and time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        janitor.kill()
        janitor.wait()

    assert cap(sys_root) == (TARGET, TARGET_BG)


def test_missing_proc_tree_fails_open_with_a_warning(tmp_path: Path) -> None:
    completed = run_setup(tmp_path / "nowhere")

    assert completed.returncode == 1
    assert "[dirty-cap] cannot read Dirty" in completed.stdout


def test_run_sh_starts_setup_and_janitor_fail_open() -> None:
    run_sh = (Path(__file__).resolve().parents[1] / "run.sh").read_text()

    assert "bash setup_dirty_cap.sh || echo" in run_sh
    assert "bash setup_dirty_cap.sh janitor &" in run_sh
