from __future__ import annotations

from typing import Any

from services.const import MAX_DISK_UTILIZATION_PERCENT, MIN_FREE_DISK_GB

from ..messages import DiskSpaceMessages as Msg
from ..messages import render_message
from ..pipeline import CheckResult, Context
from .banned_provider import has_live_rental

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
    (TenantEnforcementCheck halts a rented one); a live rental is never failed for its disk.
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
            free_gb = round(_as_float(hard_disk.get("free")) / KB_PER_GB, 1)
            what.update(utilization_percent=utilization, free_gb=free_gb)
            if utilization > MAX_DISK_UTILIZATION_PERCENT:
                template = Msg.TOO_FULL
            elif free_gb < MIN_FREE_DISK_GB:
                template = Msg.FREE_TOO_LOW
            else:
                return CheckResult(
                    passed=True,
                    event=render_message(Msg.OK, ctx=ctx, check_id=self.check_id, what=what),
                )
        if has_live_rental(ctx):
            event = render_message(
                template,
                ctx=ctx,
                check_id=self.check_id,
                what=what,
                impact="Proceed; a live rental is not failed",
            )
            return CheckResult(passed=True, event=event)
        return CheckResult(
            passed=False, event=render_message(template, ctx=ctx, check_id=self.check_id, what=what)
        )
