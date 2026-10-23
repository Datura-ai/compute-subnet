from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from core.config import settings
from core.utils import _m, get_extra_info

from ..messages import RentedThroughputMessages as Msg
from ..messages import render_message
from ..pipeline import CheckResult, Context
from .container_download_speed import (
    _FETCH_SCRIPT,
    _TIMEOUT_EXITS,
    ContainerDownloadSpeedCheck,
    download_speed_command,
    parse_download_speed,
)
from .rented_gpu_drop import POD_STATUS_RUNNING

logger = logging.getLogger(__name__)

DISK_PROBE_BYTES = 256 * 1024 * 1024
DISK_TIMEOUT_SECONDS = 60
# the 2 s busy window, the disk probe (direct, then buffered) and both fetches
COMMAND_TIMEOUT_SECONDS = 260
RETRY_SECONDS = 30 * 60
READINGS_BEFORE_VERDICT = 2
BUSY_WINDOW_SECONDS = 2
DISK_MARKER = "lium_disk"
SNAP_MARKER = "lium_tp"
_REDIS_PREFIX = "rented_throughput"
_REDIS_TTL_SECONDS = 7 * 24 * 3600

# On the host (the executor runs on the host network; /proc/diskstats is not namespaced): received bytes on
# the physical NICs and sectors moved per whole disk, twice BUSY_WINDOW_SECONDS apart, before the probe.
_SNAPSHOT = (
    f'snap() {{ echo "{SNAP_MARKER} t=$(cut -d" " -f1 /proc/uptime) '
    "rx=$(sed 's/:/ /' /proc/net/dev | awk 'NR>2 && $1!=\"lo\" && "
    '$1!~/^(docker|veth|br-|virbr|cni|flannel|cali|tun|wg|lxc|vxlan|kube)/{s+=$2} END{printf "%d", s}\') '
    "io=$(awk '$3~/^(sd[a-z]+|nvme[0-9]+n[0-9]+|vd[a-z]+|xvd[a-z]+|md[0-9]+)$/{printf \"%s:%d,\", $3, $6+$10}' "
    '/proc/diskstats)"; }; '
    f"snap; sleep {BUSY_WINDOW_SECONDS}; snap; "
)
# In the probe container, whose writable layer sits on the disk a renter's container writes to. O_DIRECT where the
# filesystem takes it, else buffered with the fsync timed in; a timed-out write is the slow reading itself.
_DISK_SCRIPT = (
    "up() { cut -d' ' -f1 /proc/uptime; }; "
    "dprobe() { rm -f /root/lium_disk; s=$(up); "
    f"timeout {DISK_TIMEOUT_SECONDS} dd if=/dev/zero of=/root/lium_disk bs=1M "
    f"count={DISK_PROBE_BYTES // (1024 * 1024)} $1 conv=fsync 2>/dev/null; rc=$?; e=$(up); "
    "n=$(wc -c 2>/dev/null </root/lium_disk || echo 0); rm -f /root/lium_disk; "
    f'echo "{DISK_MARKER} bytes=$n start=$s end=$e rc=$rc"; }}; '
    "dprobe oflag=direct; case $rc in 0|124|137|143) ;; *) dprobe '';; esac; "
)


def throughput_command(sysbox: bool) -> str:
    return _SNAPSHOT + download_speed_command(sysbox, _DISK_SCRIPT + _FETCH_SCRIPT)


@dataclass(frozen=True)
class DiskReading:
    bytes: int = 0
    seconds: float | None = None
    mb_per_s: float | None = None
    # None = measured; else why not: no_output, write_failed
    error: str | None = None


def parse_disk(stdout: str) -> DiskReading:
    """The last dd line: the buffered retry when O_DIRECT was refused."""
    lines = [ln for ln in (stdout or "").splitlines() if ln.startswith(f"{DISK_MARKER} ")]
    if not lines:
        return DiskReading(error="no_output")
    tokens = dict(t.partition("=")[::2] for t in lines[-1][len(DISK_MARKER) :].split())
    try:
        got = int(tokens.get("bytes") or 0)
        seconds = float(tokens["end"]) - float(tokens["start"])
        rc = int(tokens.get("rc") or -1)
    except (KeyError, ValueError):
        return DiskReading(error="no_output")
    if rc == 0 or rc in _TIMEOUT_EXITS:
        return DiskReading(got, seconds, got / max(seconds, 0.01) / 1e6)
    return DiskReading(got, seconds, error="write_failed")


def parse_busy(stdout: str) -> tuple[float | None, float | None]:
    """Other traffic in the window before the probe: (received Mbps on the NICs, MB/s on the busiest disk)."""
    snaps = []
    for line in (stdout or "").splitlines():
        if line.startswith(f"{SNAP_MARKER} "):
            tokens = dict(t.partition("=")[::2] for t in line[len(SNAP_MARKER) :].split())
            try:
                io = {
                    d: int(n)
                    for d, _, n in (e.partition(":") for e in tokens.get("io", "").split(",") if e)
                }
                snaps.append((float(tokens["t"]), int(tokens["rx"]), io))
            except (KeyError, ValueError):
                return None, None
    if len(snaps) < 2:
        return None, None
    (t0, rx0, io0), (t1, rx1, io1) = snaps[0], snaps[1]
    dt = max(t1 - t0, 0.01)
    disk = max((io1[d] - io0[d] for d in io1 if d in io0), default=0) * 512 / dt / 1e6
    return (rx1 - rx0) * 8 / dt / 1e6, disk


def phase_offset_seconds(uuid: str) -> float:
    fraction = int(hashlib.sha256(uuid.encode()).hexdigest()[:12], 16) / 16**12
    return fraction * settings.RENTED_THROUGHPUT_INTERVAL_HOURS * 3600


@dataclass
class _State:
    at: float
    failures_in_a_row: int = 0
    reading: dict[str, Any] = field(default_factory=dict)
    measured: bool = False

    def next_due_at(self) -> float:
        if self.failures_in_a_row > 0:
            return self.at + RETRY_SECONDS
        return self.at + settings.RENTED_THROUGHPUT_INTERVAL_HOURS * 3600

    @classmethod
    def from_raw(cls, raw: Any) -> _State | None:
        try:
            data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            return cls(
                at=float(data["at"]),
                failures_in_a_row=int(data.get("failures_in_a_row", 0)),
                reading=dict(data.get("reading") or {}),
                measured=bool(data.get("measured")),
            )
        except (TypeError, ValueError, KeyError, AttributeError):
            return None


class RentedThroughputCheck:
    """What a renter's pod gets from the network and the disk on a rented node (ticket-0739: 0.7-2 MB/s downloads
    and a crawling disk on a node listed at ~3 Gbps). ContainerDownloadSpeedCheck measures idle nodes only, so a
    host that shapes or starves a node once it is rented kept its score.

    Once per RENTED_THROUGHPUT_INTERVAL_HOURS at a per-node phase, a throwaway container on the rentals bridge (the
    renter's runtime) writes DISK_PROBE_BYTES with fsync and reads the two pinned files of ContainerDownloadSpeedCheck.
    A reading fails when a download fails that check's test (CONTAINER_DOWNLOAD_SPEED_MIN_MBPS, or unreachable while
    the validator reaches it) or the write runs under RENTED_THROUGHPUT_DISK_MIN_MB_PER_S. The renter's own traffic
    is no excuse below the floors: only when the 2 s before the probe already moved RENTED_THROUGHPUT_BUSY_MBPS on the
    NICs, or RENTED_THROUGHPUT_DISK_BUSY_MB_PER_S on a disk, is that side no verdict (a link or disk doing that much
    is not the defect). READINGS_BEFORE_VERDICT failing readings in a row (RETRY_SECONDS apart) zero the score
    (RENTED_THROUGHPUT_SLOW) until a reading passes. Non-fatal: the rental keeps running.
    """

    check_id = "host.validate.rented_throughput"
    fatal = False

    async def run(self, ctx: Context) -> CheckResult:
        if not settings.RENTED_THROUGHPUT_CHECK_ENABLED:
            return self._skipped(ctx, "RENTED_THROUGHPUT_CHECK_ENABLED is off")
        rented_data = ctx.state.rented_data
        rented_executor = rented_data.executors.get(ctx.executor.uuid) if rented_data else None
        if rented_executor is None or rented_executor.miner_hotkey != ctx.miner_hotkey:
            return self._skipped(ctx, "not rented")
        if not any(pod.status in (None, POD_STATUS_RUNNING) for pod in rented_executor.pods):
            return self._skipped(ctx, "no running pod")

        key = f"{_REDIS_PREFIX}:{ctx.executor.uuid}"
        try:
            raw = await ctx.services.redis.get(key)
        except Exception:
            logger.warning(
                _m("Rented throughput: Redis unreadable", extra=get_extra_info(ctx.default_extra))
            )
            return self._skipped(ctx, "last reading unreadable in Redis")
        state = _State.from_raw(raw) if raw is not None else None
        now = time.time()
        if state is None:
            state = _State(at=now - phase_offset_seconds(ctx.executor.uuid))
            await self._save(ctx, key, state)
        if now < state.next_due_at():
            return self._verdict(ctx, state, probed=False)

        reading, failing, no_verdict = await self._measure_and_judge(ctx)
        if failing:
            failures = state.failures_in_a_row + 1
        elif no_verdict:
            failures = state.failures_in_a_row
        else:
            failures = 0
        state = _State(at=now, failures_in_a_row=failures, reading=reading, measured=not no_verdict)
        logger.info(
            _m(
                "RENTED_THROUGHPUT",
                extra=get_extra_info(
                    {
                        **ctx.default_extra,
                        **reading,
                        "failing": failing,
                        "failures_in_a_row": failures,
                    }
                ),
            )
        )
        await self._save(ctx, key, state)
        return self._verdict(ctx, state, probed=True)

    async def _measure_and_judge(self, ctx: Context) -> tuple[dict[str, Any], bool, bool]:
        result = await ctx.runner.run(
            throughput_command(bool(ctx.state.sysbox_runtime)),
            timeout=COMMAND_TIMEOUT_SECONDS,
            retryable=False,
        )
        stdout = result.stdout or ""
        endpoints = parse_download_speed(stdout)
        disk = parse_disk(stdout)
        rx_mbps, disk_busy = parse_busy(stdout)
        net_busy = rx_mbps is not None and rx_mbps >= settings.RENTED_THROUGHPUT_BUSY_MBPS
        disk_is_busy = (
            disk_busy is not None and disk_busy >= settings.RENTED_THROUGHPUT_DISK_BUSY_MB_PER_S
        )

        net = await ContainerDownloadSpeedCheck()._judge(endpoints)
        net_failing = net.is_failing and not net_busy
        net_no_verdict = net_busy or net.has_no_verdict
        disk_failing = (
            disk.mb_per_s is not None
            and disk.mb_per_s < settings.RENTED_THROUGHPUT_DISK_MIN_MB_PER_S
            and not disk_is_busy
        )
        disk_no_verdict = disk_is_busy or disk.mb_per_s is None
        failing = net_failing or disk_failing
        reading = {
            "endpoints": [asdict(r) for r in endpoints],
            "disk": asdict(disk),
            "other_rx_mbps": rx_mbps,
            "other_disk_mb_per_s": disk_busy,
            "network_failing": net_failing,
            "disk_failing": disk_failing,
            "min_mbps": settings.CONTAINER_DOWNLOAD_SPEED_MIN_MBPS,
            "disk_min_mb_per_s": settings.RENTED_THROUGHPUT_DISK_MIN_MB_PER_S,
        }
        if result.error_type is not None:
            reading["error"] = f"{result.error_type}: {(result.error_message or '')[-200:]}"
        return reading, failing, not failing and (net_no_verdict or disk_no_verdict)

    def _verdict(self, ctx: Context, state: _State, *, probed: bool) -> CheckResult:
        what: dict[str, Any] = {
            **state.reading,
            "probed_this_cycle": probed,
            "failures_in_a_row": state.failures_in_a_row,
        }
        if state.failures_in_a_row >= READINGS_BEFORE_VERDICT:
            event = render_message(Msg.SLOW, ctx=ctx, check_id=self.check_id, what=what)
            return CheckResult(
                passed=False, event=event, updates={"rented_throughput_passed": False}
            )
        if not probed:
            return self._skipped(
                ctx, "not due", last={**what, "read_at": state.at, "next_at": state.next_due_at()}
            )
        if state.failures_in_a_row:
            template = Msg.SLOW_ONCE
        elif state.measured:
            template = Msg.OK
        else:
            template = Msg.UNMEASURED
        return CheckResult(
            passed=True, event=render_message(template, ctx=ctx, check_id=self.check_id, what=what)
        )

    async def _save(self, ctx: Context, key: str, state: _State) -> None:
        payload = {
            "at": state.at,
            "failures_in_a_row": state.failures_in_a_row,
            "reading": state.reading,
            "measured": state.measured,
        }
        try:
            await ctx.services.redis.set(key, json.dumps(payload), ex=_REDIS_TTL_SECONDS)
        except Exception:
            logger.warning(
                _m(
                    "Rented throughput: could not record the reading",
                    extra=get_extra_info(ctx.default_extra),
                )
            )

    def _skipped(
        self, ctx: Context, reason: str, *, last: dict[str, Any] | None = None
    ) -> CheckResult:
        what: dict[str, Any] = {"reason": reason}
        if last is not None:
            what["last"] = last
        return CheckResult(
            passed=True,
            event=render_message(Msg.SKIPPED, ctx=ctx, check_id=self.check_id, what=what),
        )
