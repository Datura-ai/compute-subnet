"""ticket-0739: a renter on a node listed at ~3 Gbps downloaded at 0.7-2 MB/s and the disk crawled, while the idle-node
download check never looks at a rented node."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from protocol.vc_protocol.compute_requests import RentedExecutor, RentedExecutorsResponse, RentedPod

from services.task.checks import container_download_speed as download_module
from services.task.checks import rented_throughput as module
from services.task.checks.rented_throughput import (
    RentedThroughputCheck,
    parse_busy,
    parse_disk,
    throughput_command,
)
from services.task.messages import RentedThroughputMessages as Msg
from tests.helpers import build_services, build_state, default_executor, make_context
from tests.test_container_download_speed_check import FAST, SLOW, Clock, FakeRedis, FakeRunner

EXECUTOR = default_executor()
pytestmark = pytest.mark.asyncio


def snaps(rx_bytes: int = 0, sectors: int = 0) -> str:
    """Two host snapshots 2 s apart: rx_bytes received on the NICs and sectors moved on nvme0n1 in between."""
    return (
        "lium_tp t=100.0 rx=1000 io=nvme0n1:500,sda:7,\n"
        f"lium_tp t=102.0 rx={1000 + rx_bytes} io=nvme0n1:{500 + sectors},sda:7,\n"
    )


def disk(mb: int = 256, secs: float = 0.5, rc: int = 0) -> str:
    return f"lium_disk bytes={mb * 1024 * 1024} start=10.0 end={10 + secs} rc={rc}\n"


GOOD = snaps() + disk() + FAST
SLOW_DISK = snaps() + disk(secs=30.0) + FAST  # 9 MB/s
SLOW_NET = snaps() + disk() + SLOW
# the renter is pulling 400 Mbps: a slow probe download is the shared link, not the host
BUSY_NET = snaps(rx_bytes=100_000_000) + disk() + SLOW


def rented(hotkey: str = "miner-hotkey", status: str | None = None) -> RentedExecutorsResponse:
    return RentedExecutorsResponse(
        executors={
            EXECUTOR.uuid: RentedExecutor(
                miner_hotkey=hotkey,
                executor_ip_address=EXECUTOR.address,
                executor_ip_port=str(EXECUTOR.port),
                pods=[RentedPod(pod_id="p1", container_name="pod_p1", status=status)],
            )
        }
    )


@contextmanager
def flags():
    fake = SimpleNamespace(
        RENTED_THROUGHPUT_CHECK_ENABLED=True,
        RENTED_THROUGHPUT_INTERVAL_HOURS=3.0,
        RENTED_THROUGHPUT_DISK_MIN_MB_PER_S=50.0,
        RENTED_THROUGHPUT_BUSY_MBPS=200.0,
        RENTED_THROUGHPUT_DISK_BUSY_MB_PER_S=200.0,
        CONTAINER_DOWNLOAD_SPEED_MIN_MBPS=10.0,
    )
    clock = Clock()

    async def reaches(url):
        return True

    with (
        patch.object(module, "settings", fake),
        patch.object(download_module, "settings", fake),
        patch.object(module, "time", clock),
        patch.object(download_module, "validator_reaches", reaches),
        patch.object(module, "phase_offset_seconds", lambda uuid: 3 * 3600),
    ):
        yield clock


def make_ctx(*answers: str, rented_data=None):
    runner = FakeRunner(*answers)
    ctx = make_context(
        state=build_state(
            rented_data=rented() if rented_data is None else rented_data, sysbox_runtime=True
        ),
        runner=runner,
        services=build_services(redis=FakeRedis()),
    )
    return ctx, runner


def test_defaults_enforce_with_a_kill_switch():
    fields = type(module.settings).model_fields
    assert fields["RENTED_THROUGHPUT_CHECK_ENABLED"].default is True
    assert fields["RENTED_THROUGHPUT_DISK_MIN_MB_PER_S"].default == 50.0
    assert RentedThroughputCheck.fatal is False


def test_command_samples_the_host_then_probes_disk_and_network_in_the_renter_runtime():
    cmd = throughput_command(True)
    assert cmd.index("/proc/net/dev") < cmd.index("/usr/bin/docker run")
    assert "--runtime=sysbox-runc" in cmd and '--network "$net"' in cmd
    assert "oflag=direct" in cmd and "conv=fsync" in cmd and "files.pythonhosted.org" in cmd


def test_parse_disk_and_busy():
    assert parse_disk(disk(secs=1.0)).mb_per_s == pytest.approx(268.4, rel=1e-3)
    assert parse_disk(disk(mb=20, secs=60.0, rc=143)).mb_per_s == pytest.approx(
        0.35, rel=1e-2
    )  # timed out: slow
    assert parse_disk(disk(rc=1)).error == "write_failed"
    # O_DIRECT refused, the buffered retry is the reading
    assert parse_disk(disk(mb=0, rc=1) + disk(secs=2.0)).error is None
    assert parse_disk("").error == "no_output"
    rx, io = parse_busy(snaps(rx_bytes=50_000_000, sectors=2_000_000))
    assert rx == pytest.approx(200.0) and io == pytest.approx(512.0)
    assert parse_busy("") == (None, None)


async def test_good_reading_passes():
    with flags():
        ctx, runner = make_ctx(GOOD)
        res = await RentedThroughputCheck().run(ctx)
    assert res.passed and res.event.reason_code == Msg.OK.reason and len(runner.commands) == 1


@pytest.mark.parametrize("answer", [SLOW_DISK, SLOW_NET])
async def test_two_slow_readings_zero_the_score_until_one_passes(answer):
    with flags() as clock:
        ctx, runner = make_ctx(answer, answer, GOOD)
        check = RentedThroughputCheck()
        first = await check.run(ctx)
        assert first.passed and first.event.reason_code == Msg.SLOW_ONCE.reason
        clock.now += 31 * 60
        second = await check.run(ctx)
        assert not second.passed and second.event.reason_code == Msg.SLOW.reason
        assert second.updates == {"rented_throughput_passed": False}
        clock.now += 60  # the finding stands between readings
        assert (await check.run(ctx)).passed is False and len(runner.commands) == 2
        clock.now += 31 * 60
        assert (await check.run(ctx)).passed is True


async def test_a_busy_renter_is_no_verdict_and_keeps_the_streak():
    with flags() as clock:
        ctx, runner = make_ctx(SLOW_NET, BUSY_NET, SLOW_NET)
        check = RentedThroughputCheck()
        await check.run(ctx)
        clock.now += 31 * 60
        busy = await check.run(ctx)
        assert busy.passed and busy.event.reason_code == Msg.SLOW_ONCE.reason
        clock.now += 31 * 60
        assert (await check.run(ctx)).passed is False


async def test_idle_foreign_or_stopped_rentals_are_left_alone():
    for rented_data in (
        RentedExecutorsResponse(executors={}),
        rented(hotkey="other"),
        rented(status="STOPPED"),
    ):
        with flags():
            ctx, runner = make_ctx(rented_data=rented_data)
            res = await RentedThroughputCheck().run(ctx)
        assert res.passed and not runner.commands and res.event.reason_code == Msg.SKIPPED.reason
