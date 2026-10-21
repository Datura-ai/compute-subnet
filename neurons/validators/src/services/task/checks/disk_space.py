from __future__ import annotations

from typing import Any

from services.const import DEFAULT_JOB_OWNER_LIUM, MAX_DISK_UTILIZATION_PERCENT, MIN_FREE_DISK_GB

from ..messages import DiskSpaceMessages as Msg
from ..messages import render_message
from ..pipeline import CheckResult, Context
from .rented_machine import _rental_of_validated_miner

KB_PER_GB = 1024 * 1024


def _as_float(value: Any) -> float:
    # the listing read a null or unparsable figure as 0
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


class DiskSpaceCheck:
    """DAH-4961: the disk floor the renter listing used to hide nodes for, as a validation failure.

    Pure data on ``specs.hard_disk`` (free in KB, utilization in percent) as VerifyX leaves it, so it
    sits right after VerifyXCheck: the published figure is the one judged. Only idle nodes get here
    (TenantEnforcementCheck halts a rented one); a live rental, or Lium's own Default Job or filler
    on the node, is never failed for the disk it uses.
    """

    check_id = "executor.validate.disk_space"
    fatal = True

    async def run(self, ctx: Context) -> CheckResult:
        hard_disk = (ctx.state.specs or {}).get("hard_disk")
        what: dict[str, Any] = {
            "max_utilization_percent": MAX_DISK_UTILIZATION_PERCENT,
            "required_free_gb": MIN_FREE_DISK_GB,
        }
        if (
            not isinstance(hard_disk, dict)
            or "utilization" not in hard_disk
            or "free" not in hard_disk
        ):
            template = Msg.NOT_REPORTED
        else:
            utilization = _as_float(hard_disk.get("utilization"))
            free_kb = _as_float(hard_disk.get("free"))
            # compare unrounded KB like the listing did; round only the displayed figure
            what.update(utilization_percent=utilization, free_gb=round(free_kb / KB_PER_GB, 1))
            if utilization > MAX_DISK_UTILIZATION_PERCENT:
                template = Msg.TOO_FULL
            elif free_kb < MIN_FREE_DISK_GB * KB_PER_GB:
                template = Msg.FREE_TOO_LOW
            else:
                return CheckResult(
                    passed=True,
                    event=render_message(Msg.OK, ctx=ctx, check_id=self.check_id, what=what),
                )
        rented_data = ctx.state.rented_data
        # same ownership rule as TenantEnforcementCheck: another miner's rental on a different
        # endpoint does not exempt this node
        rental = _rental_of_validated_miner(
            ctx, rented_data.executors.get(ctx.executor.uuid) if rented_data else None
        )
        if rental and rental.pods:
            impact = "Proceed; a live rental is not failed"
        elif rented_data and (
            rented_data.get_default_job_owner(ctx.executor.uuid) == DEFAULT_JOB_OWNER_LIUM
            or rented_data.get_filler_containers(ctx.executor.uuid)
        ):
            # the disk may be held by Lium's own Default Job / filler; the node keeps its idle score
            impact = "Proceed; Lium's Default Job may hold this disk"
        else:
            impact = None
        if impact:
            event = render_message(
                template, ctx=ctx, check_id=self.check_id, what=what, impact=impact
            )
            return CheckResult(passed=True, event=event)
        return CheckResult(
            passed=False, event=render_message(template, ctx=ctx, check_id=self.check_id, what=what)
        )
