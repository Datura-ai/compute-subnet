from __future__ import annotations

from ..messages import PidHostRequiredMessages as Msg
from ..messages import render_message
from ..pipeline import CheckResult, Context


class PidHostRequiredCheck:
    """Fail an unrented executor whose container does not run with `pid: host`.

    Backups read the rental's /proc/<pid> from the executor, which only works in the host PID
    namespace. Reads ``specs.executor_pid_mode`` (docker HostConfig.PidMode of the executor's own
    container). A missing value (old scrape, inspect failed) is unknown and passes. Rented nodes
    are left alone, like SysboxRequiredCheck, so live rentals are never disrupted.
    """

    check_id = "executor.validate.pid_host_required"
    fatal = True

    async def run(self, ctx: Context) -> CheckResult:
        pid_mode = (ctx.state.specs or {}).get("executor_pid_mode")
        rented = ctx.state.rented_data.executors.get(ctx.executor.uuid) if ctx.state.rented_data else None
        is_rented = rented is not None and len(rented.pods) > 0
        what = {"executor_pid_mode": pid_mode, "is_rented": is_rented}
        if isinstance(pid_mode, str) and pid_mode != "host" and not is_rented:
            return CheckResult(
                passed=False, event=render_message(Msg.MISSING, ctx=ctx, check_id=self.check_id, what=what)
            )
        return CheckResult(passed=True, event=render_message(Msg.OK, ctx=ctx, check_id=self.check_id, what=what))
