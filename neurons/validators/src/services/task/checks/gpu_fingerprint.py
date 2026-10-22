from __future__ import annotations

import logging

from services.redis_service import GPU_ANCHOR_BROKEN_KEY, GPU_ANCHOR_KEY

from ..messages import GpuFingerprintMessages as Msg, render_message
from ..pipeline import CheckResult, Context

logger = logging.getLogger(__name__)

# The two decisions the check takes on a set that differs from the anchor.
DECISION_GPU_MISSING = "GPU_MISSING"
DECISION_ANCHOR_BROKEN = "ANCHOR_BROKEN"


def split_uuids(csv: str) -> list[str]:
    """The distinct UUIDs of a comma-joined list, sorted.

    A UUID listed twice is one card read twice (`gpu_uuids` is joined from `gpu_details[*].uuid` by the
    scrape), never a second card, so one copy counts. Comparing distinct sets keeps a duplicated scrape
    from breaking the anchor for good under the hard rule.
    """
    return sorted({u for u in csv.split(",") if u})


class GpuFingerprintCheck:
    """Compare the anchored GPU UUID set with the latest scrape.

    The anchor is the `uuids` value of the executor's verified-job record: written on the first successful
    verification and kept by every later write, including the reset (`clear_verified_job_info` copies it),
    so a node is never re-anchored under the same executor id.

    This check runs before SpecChangeCheck (pipeline_factory), so every set change reaches it, and
    SpecChangeCheck only fires when the UUIDs match but the model:count string differs.

    DAH-3457:
    - strict subset of the anchor  -> GPU_MISSING, fatal, verification reset; the node passes again on the
      first cycle that shows the full set;
    - any UUID outside the anchor  -> GPU_UUID_CHANGED with `anchor_broken`, fatal, verification reset, and
      the record is marked broken: every later cycle fails the same way until the provider re-registers
      the node (a new executor id gets a new anchor).
    A node whose record is already marked broken fails before the sets are compared.

    Both sets are compared as distinct UUIDs. A scrape that lists one anchored card twice matches the anchor
    (`duplicate_uuid: true` on the event, one warning in the log). A scrape with zero cards passes this check
    and leaves the anchor unchanged. Neither is read as a set change.
    """

    check_id = "gpu.validate.fingerprint"
    fatal = True

    async def run(self, ctx: Context) -> CheckResult:
        verified = ctx.verified or {}
        prev_uuids = verified.get(GPU_ANCHOR_KEY) or ""
        current_uuids = ctx.state.gpu_uuids or ""

        if verified.get(GPU_ANCHOR_BROKEN_KEY):
            event = render_message(
                Msg.ANCHOR_BROKEN,
                ctx=ctx,
                check_id=self.check_id,
                what={"previous": prev_uuids, "current": current_uuids, "anchor_broken": True},
            )
            return CheckResult(passed=False, event=event)

        anchor = split_uuids(prev_uuids)
        current = split_uuids(current_uuids)
        # A duplicated scrape is not a set change (split_uuids keeps one copy). It is logged and flagged on the
        # event so the rows show how often the scrape does this. A scrape with zero cards never reaches the
        # comparison. Neither one touches the stored anchor.
        listed = [u for u in current_uuids.split(",") if u]
        duplicated = len(listed) != len(current)
        if duplicated:
            logger.warning(
                "GPU UUID listed twice in the scrape of executor %s; one copy counts: %s",
                ctx.executor.uuid,
                current_uuids,
            )
        seen = {"duplicate_uuid": True} if duplicated else {}

        if anchor and current and anchor != current:
            missing = sorted(set(anchor) - set(current))
            unexpected = sorted(set(current) - set(anchor))
            # Sets decide: only cards missing = GPU_MISSING; any card outside the anchor = broken.
            decision = DECISION_GPU_MISSING if not unexpected else DECISION_ANCHOR_BROKEN
            what = {
                "previous": prev_uuids,
                "current": current_uuids,
                "missing": missing,
                "unexpected": unexpected,
                **seen,
            }

            if decision == DECISION_GPU_MISSING:
                event = render_message(Msg.GPU_MISSING, ctx=ctx, check_id=self.check_id, what=what)
                return CheckResult(passed=False, event=event, updates={"clear_verified_job_info": True})

            event = render_message(
                Msg.ANCHOR_BROKEN,
                ctx=ctx,
                check_id=self.check_id,
                what={**what, "anchor_broken": True},
            )
            return CheckResult(
                passed=False,
                event=event,
                updates={"clear_verified_job_info": True, "gpu_anchor_broken": True},
            )

        event = render_message(
            Msg.UUID_OK,
            ctx=ctx,
            check_id=self.check_id,
            what={"gpu_uuids": current_uuids, **seen},
        )
        return CheckResult(passed=True, event=event)
