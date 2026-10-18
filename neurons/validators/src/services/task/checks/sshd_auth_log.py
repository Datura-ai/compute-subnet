from __future__ import annotations

import logging

from core.utils import _m, get_extra_info

from ..messages import SshdAuthLogMessages as Msg
from ..messages import render_message
from ..pipeline import CheckResult, Context

logger = logging.getLogger(__name__)

# Written by the executor's sshd (run.sh: `sshd -E`, LogLevel VERBOSE from sshd_setup.sh).
SSHD_AUTH_LOG = "/var/log/sshd-auth.log"
# A cycle is ~15 min; more logins than this in one is a scanner or a loop, and the newest say enough.
MAX_LOGINS_PER_CYCLE = 200
# Read and empty in one command, so a line written between the two lands in the next cycle at worst.
READ_AND_EMPTY = (
    f'f={SSHD_AUTH_LOG}; [ -f "$f" ] || exit 3; '
    f'grep -a "Accepted " "$f" | tail -n {MAX_LOGINS_PER_CYCLE}; : > "$f"'
)


class SshdAuthLogCheck:
    """Ship the executor container's sshd logins to the validator log (DAH-4629, I-78).

    An unregistered key is appended to renters' pods from root sessions into executor containers,
    and nothing recorded which key opened those sessions. Each `Accepted publickey ... SHA256:<fp>`
    line since the last cycle is logged here, so Loki names the key of every session, ours included
    (the validator makes a fresh key per job). Logged directly, not through the event: the sink
    drops an info event that repeats the last one to debug. Observation only: never fails or halts,
    runs on rented nodes too.
    """

    check_id = "executor.observe.sshd_auth_log"
    fatal = False

    async def run(self, ctx: Context) -> CheckResult:
        try:
            result = await ctx.ssh.run(READ_AND_EMPTY, check=False)
        except Exception as exc:  # noqa: BLE001 - observation only, never fatal
            return self._unread(ctx, reason=f"ssh error: {exc}")
        if result.exit_status == 3:
            # an executor image from before DAH-4629
            return self._unread(ctx, reason="no sshd auth log on this executor")
        logins = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        if logins:
            logger.info(
                _m(
                    "Executor sshd logins",
                    extra=get_extra_info(
                        {**ctx.default_extra, "executor_uuid": ctx.executor.uuid, "logins": logins}
                    ),
                )
            )
        event = render_message(
            Msg.LOGINS,
            ctx=ctx,
            check_id=self.check_id,
            what={"executor_uuid": ctx.executor.uuid, "login_count": len(logins)},
        )
        return CheckResult(passed=True, event=event)

    def _unread(self, ctx: Context, *, reason: str) -> CheckResult:
        event = render_message(
            Msg.UNREAD,
            ctx=ctx,
            check_id=self.check_id,
            what={"executor_uuid": ctx.executor.uuid, "reason": reason},
        )
        return CheckResult(passed=True, event=event)
