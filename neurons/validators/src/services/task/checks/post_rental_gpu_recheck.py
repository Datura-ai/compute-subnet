"""POST_RENTAL_GPU_RECHECK (DAH-4482): a node whose GPUs looked broken during a rental proves every card
before it re-lists.

A rented node's cycle halts at TenantEnforcementCheck before any GPU work, and a GeForce card that breaks
under a rental keeps passing the single-card matmul once the renter leaves. RentedGpuHealthShadowCheck
records a per-executor marker whenever its read shows a fault or a flag; on the node's next unrented cycle
this check runs the all-cards matmul and the kernel-fault probe on every card, whatever the
MATMUL_ALLCARDS_* / GPU_FAULT_PROBE_* flags say. A failure fails the node and keeps the marker; a pass
clears it. Off by default (POST_RENTAL_GPU_RECHECK_ENABLED): the cycle then only logs
POST_RENTAL_GPU_RECHECK_WOULD_REQUIRE.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from services.redis_service import POST_RENTAL_GPU_RECHECK_PREFIX

from core.config import settings
from core.utils import _m, get_extra_info

from ..messages import GpuFaultProbeMessages, render_message
from ..messages import PostRentalGpuRecheckMessages as Msg
from ..pipeline import CheckResult, Context
from .capability import _get_filler_only_container
from .gpu_fault_probe import GpuFaultProbeCheck

logger = logging.getLogger(__name__)

# a few days: long enough to outlast a weekend rental's tail, short enough that a stale flag dies
MARKER_TTL_SECONDS = 3 * 24 * 3600
MAX_MARKED_CARDS = 16


def marker_key(miner_hotkey: str, executor_uuid: str) -> str:
    return f"{POST_RENTAL_GPU_RECHECK_PREFIX}:{miner_hotkey}:{executor_uuid}"


async def record_marker(ctx: Context, source: str, health: dict[str, Any]) -> None:
    """Remember what a rented cycle flagged. Never raises: the caller is a shadow check."""
    if settings.DRY_RUN:
        return
    cards = [
        {
            key: card.get(key)
            for key in ("index", "uuid", "verdict", "reasons", "flags")
            if card.get(key) is not None
        }
        for card in health.get("cards") or []
        if card.get("verdict") != "ok" or card.get("flags")
    ][:MAX_MARKED_CARDS]
    marker = {
        "at": datetime.now(UTC).isoformat(),
        "source": source,
        "verdict": health.get("verdict"),
        "cards": cards,
    }
    try:
        await ctx.services.redis.set(
            marker_key(ctx.miner_hotkey, ctx.executor.uuid),
            json.dumps(marker),
            ex=MARKER_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            _m(
                "POST_RENTAL_GPU_RECHECK_MARKER_WRITE_FAILED",
                extra=get_extra_info({**ctx.default_extra, "error": str(exc)}),
            )
        )


class PostRentalGpuRecheckCheck:
    check_id = "gpu.validate.post_rental_recheck"
    fatal = True

    async def run(self, ctx: Context) -> CheckResult:
        key = marker_key(ctx.miner_hotkey, ctx.executor.uuid)
        try:
            raw = await ctx.services.redis.get(key)
        except Exception as exc:  # noqa: BLE001 — no marker readable is no recheck, never a failure
            raw = None
            logger.warning(
                _m(
                    "POST_RENTAL_GPU_RECHECK_MARKER_READ_FAILED",
                    extra=get_extra_info({**ctx.default_extra, "error": str(exc)}),
                )
            )
        if raw is None:
            return CheckResult(
                passed=True, event=render_message(Msg.NO_MARKER, ctx=ctx, check_id=self.check_id)
            )
        try:
            marker = json.loads(raw)
        except (TypeError, ValueError):
            marker = {"raw": str(raw)[:200]}
        what: dict[str, Any] = {"executor_uuid": ctx.executor.uuid, "marker": marker}

        if not settings.POST_RENTAL_GPU_RECHECK_ENABLED:
            self._log(ctx, "POST_RENTAL_GPU_RECHECK_WOULD_REQUIRE", what, logging.WARNING)
            return CheckResult(
                passed=True,
                event=render_message(Msg.WOULD_REQUIRE, ctx=ctx, check_id=self.check_id, what=what),
            )

        if _get_filler_only_container(ctx):
            # both probes would compete with the filler for VRAM; the marker waits for an idle cycle
            return CheckResult(
                passed=True,
                event=render_message(
                    Msg.FILLER_SKIPPED, ctx=ctx, check_id=self.check_id, what=what
                ),
            )

        failures: list[str] = []
        all_cards = await ctx.services.validation._probe_all_claimed_cards(
            ctx.ssh, ctx.executor, ctx.default_extra, ctx.state.specs or {}
        )
        # None: one card or an unsized model, where the capability matmul already covers the card
        what["all_cards_matmul"] = (
            None
            if all_cards is None
            else {"passed": all_cards.passed, "failure_reason": all_cards.failure_reason}
        )
        if all_cards is not None and not all_cards.passed:
            failures.append(f"all-cards matmul: {all_cards.failure_reason}")
        probe = await GpuFaultProbeCheck(enforce=True).run(ctx)
        what["fault_probe"] = {
            "reason": probe.event.reason_code,
            "error": probe.event.what_we_saw.get("error"),
        }
        if not probe.passed:
            failures.append(f"fault probe: {probe.event.what_we_saw.get('error')}")

        if failures:
            what["error"] = "; ".join(failures)
            self._log(ctx, "POST_RENTAL_GPU_RECHECK_FAILED", what, logging.WARNING)
            return CheckResult(
                passed=False,
                event=render_message(Msg.FAILED, ctx=ctx, check_id=self.check_id, what=what),
            )
        if probe.event.reason_code != GpuFaultProbeMessages.PROBE_OK.reason:
            # the probe could not run (or found a filler): not a pass, so the marker stays
            self._log(ctx, "POST_RENTAL_GPU_RECHECK_INCONCLUSIVE", what, logging.INFO)
            return CheckResult(
                passed=True,
                event=render_message(Msg.INCONCLUSIVE, ctx=ctx, check_id=self.check_id, what=what),
            )
        if not settings.DRY_RUN:
            try:
                await ctx.services.redis.delete(key)
            except Exception as exc:  # noqa: BLE001 — the pass stands; the recheck repeats next cycle
                what["marker_clear_error"] = str(exc)
        self._log(ctx, "POST_RENTAL_GPU_RECHECK_PASSED", what, logging.INFO)
        return CheckResult(
            passed=True,
            event=render_message(Msg.PASSED, ctx=ctx, check_id=self.check_id, what=what),
        )

    @staticmethod
    def _log(ctx: Context, key: str, what: dict[str, Any], level: int) -> None:
        logger.log(level, _m(key, extra=get_extra_info({**ctx.default_extra, **what})))
