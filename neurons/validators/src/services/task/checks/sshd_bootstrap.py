from __future__ import annotations

import logging
import shlex

from core.config import settings
from core.docker_utils import DockerCommand
from core.utils import _m, get_extra_info
from services.redis_service import SSHD_BOOTSTRAP_FAILED_PREFIX

from ...const import DEFAULT_JOB_OWNER_LIUM, FILLER_CONTAINER_PREFIX
from ..messages import SshdBootstrapMessages as Msg
from ..messages import render_message
from ..pipeline import CheckResult, Context

logger = logging.getLogger(__name__)


class SshdBootstrapCheck:
    """Fail an unrented node on which a filler create could not bring sshd up.

    The connector stamps the executor when the sshd bootstrap in Lium's own filler image ends with sshd
    installed but not running or listening, and clears it when a later filler create verifies sshd; the
    stamp lapses after SSHD_BOOTSTRAP_FAILED_TTL_SECONDS. A rented node is left alone: its stamp predates
    the rental and failing it would not help the renter.

    A running Lium filler does not exempt the node: the failed filler create is the only signal, so the
    nodes this check exists for always have one. Instead, while the node is failed, the validator removes
    the Lium fillers the backend lists there and reports each as missing (the backend closes such a run,
    as for any filler gone from its host), so Lium never uses the GPUs of a node it scores 0.
    """

    check_id = "executor.validate.sshd_bootstrap"
    fatal = True

    def __init__(self, remove_fillers: bool = True) -> None:
        self.remove_fillers = remove_fillers

    async def run(self, ctx: Context) -> CheckResult:
        rented_data = ctx.state.rented_data
        rented_executor = rented_data.executors.get(ctx.executor.uuid) if rented_data else None
        is_rented = rented_executor is not None and len(rented_executor.pods) > 0
        try:
            failure_stamp = (
                None
                if is_rented
                else await ctx.services.redis.get(f"{SSHD_BOOTSTRAP_FAILED_PREFIX}:{ctx.executor.uuid}")
            )
        except Exception:
            # an unreadable stamp is no evidence against the node
            logger.warning(
                _m("sshd bootstrap stamp could not be read", extra=get_extra_info(ctx.default_extra)),
                exc_info=True,
            )
            failure_stamp = None
        what = {"sshd_bootstrap_failed": bool(failure_stamp), "is_rented": is_rented}
        if not failure_stamp:
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
        if (
            self.remove_fillers
            and rented_data
            and rented_data.get_default_job_owner(ctx.executor.uuid) == DEFAULT_JOB_OWNER_LIUM
        ):
            what["removed_fillers"] = await self._remove_lium_fillers(
                ctx, rented_data.get_filler_containers(ctx.executor.uuid)
            )
        return CheckResult(
            passed=False,
            event=render_message(Msg.FAILED, ctx=ctx, check_id=self.check_id, what=what),
        )

    async def _remove_lium_fillers(self, ctx: Context, fillers: list[str]) -> list[str]:
        """`docker rm -fv` each filler, then report it missing so the backend closes its run; never raises."""
        removed: list[str] = []
        for name in fillers:
            try:
                result = await ctx.ssh.run(DockerCommand.remove_with_volumes(shlex.quote(name)), timeout=60)
                if result.exit_status != 0:
                    continue
                removed.append(name)
                await ctx.services.backend.get_filler_run_active(
                    name.removeprefix(FILLER_CONTAINER_PREFIX), container_missing=True
                )
            except Exception:
                logger.warning(
                    _m(
                        "Filler on a node failing the sshd check could not be removed",
                        extra=get_extra_info({**ctx.default_extra, "container_name": name}),
                    ),
                    exc_info=True,
                )
        return removed
