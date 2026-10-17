from __future__ import annotations

from core.config import settings
from services.redis_service import SSHD_BOOTSTRAP_FAILED_PREFIX

from ..messages import SshdBootstrapMessages as Msg
from ..messages import render_message
from ..pipeline import CheckResult, Context


class SshdBootstrapCheck:
    """Fail an unrented node on which a filler create could not bring sshd up.

    The connector stamps the executor when the sshd bootstrap in Lium's own filler image ends with sshd
    installed but not running or listening, and clears it when a later filler create verifies sshd; the
    stamp lapses after SSHD_BOOTSTRAP_FAILED_TTL_SECONDS. A rented node is left alone: its stamp predates
    the rental and failing it would not help the renter.
    """

    check_id = "executor.validate.sshd_bootstrap"
    fatal = True

    async def run(self, ctx: Context) -> CheckResult:
        rented_data = ctx.state.rented_data
        rented_executor = rented_data.executors.get(ctx.executor.uuid) if rented_data else None
        is_rented = rented_executor is not None and len(rented_executor.pods) > 0
        failed = (
            None
            if is_rented
            else await ctx.services.redis.get(f"{SSHD_BOOTSTRAP_FAILED_PREFIX}:{ctx.executor.uuid}")
        )
        what = {"sshd_bootstrap_failed": bool(failed), "is_rented": is_rented}
        if not failed:
            return CheckResult(
                passed=True,
                event=render_message(Msg.OK, ctx=ctx, check_id=self.check_id, what=what),
            )
        if not settings.SSHD_BOOTSTRAP_ENFORCEMENT_ENABLED:
            return CheckResult(
                passed=True,
                event=render_message(
                    Msg.FAILED_OBSERVED, ctx=ctx, check_id=self.check_id, what=what
                ),
            )
        return CheckResult(
            passed=False,
            event=render_message(Msg.FAILED, ctx=ctx, check_id=self.check_id, what=what),
        )
