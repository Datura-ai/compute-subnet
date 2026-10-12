from __future__ import annotations

from collections import Counter

from core.config import settings
from protocol.vc_protocol.validator_requests import AVAILABILITY_CATEGORY

from ..availability import ReachSource, ReachTarget

from ..messages import GpuModelMessages as Msg, render_message
from ..pipeline import CheckResult, Context


class GpuModelValidCheck:
    """Gate validation on supported GPU SKUs and healthy scrape output.

    This mirrors the legacy guard that rejected unknown models, zero counts, or mismatched
    detail lists. It prevents us from handing out scores when the scrape clearly failed or
    when a miner advertises off-policy hardware.

    A detail list whose length differs from the count (GPU_DETAILS_MISMATCH) also clears the
    verified job (DAH-3519): in production this is a scrape that enumerated fewer cards than the
    node reports, which is what a card that fell off the bus looks like. The check is fatal and
    runs before SpecChangeCheck, so the reset has to happen here; the node then leaves the market
    through the same path banned_gpu, gpu_fingerprint, duplicate_executor and spec_change use,
    rented or idle, and is verified again on the first cycle whose scrape lists every card.

    A detail list naming more than one model (GPU_MODEL_MIXED) is always reported, and refused only
    under MIXED_GPU_MODEL_ENFORCEMENT_ENABLED: the node is listed and priced as its first card. The
    refusal is an availability entry, so the node also leaves the market until every card matches.
    """

    check_id = "gpu.validate.model"
    fatal = True

    async def run(self, ctx: Context) -> CheckResult:
        gpu_model_rates = ctx.config.gpu_model_rates
        if not gpu_model_rates:
            event = render_message(
                Msg.POLICY_MISSING,
                ctx=ctx,
                check_id=self.check_id,
            )
            return CheckResult(passed=False, event=event)

        gpu_model_rates_map = gpu_model_rates
        specs = ctx.state.specs
        gpu_count = ctx.state.gpu_count
        if gpu_count is None:
            gpu_count = specs.get("gpu", {}).get("count", 0)
        gpu_details = ctx.state.gpu_details
        if not gpu_details:
            gpu_details = specs.get("gpu", {}).get("details", [])

        gpu_model = None
        if gpu_count > 0 and len(gpu_details) > 0:
            gpu_model = gpu_details[0].get("name", None)

        if gpu_model not in gpu_model_rates_map:
            supported_models = list(gpu_model_rates_map.keys())
            event = render_message(
                Msg.MODEL_UNSUPPORTED,
                ctx=ctx,
                check_id=self.check_id,
                what={
                    "gpu_model": gpu_model,
                    "gpu_count": gpu_count,
                    "supported_models": supported_models,
                },
                remediation=(
                    "Use a supported GPU model. Supported models: "
                    f"{', '.join(supported_models[:5])}{'...' if len(supported_models) > 5 else ''}"
                ),
            )
            return CheckResult(passed=False, event=event)

        if gpu_count == 0:
            event = render_message(
                Msg.COUNT_ZERO,
                ctx=ctx,
                check_id=self.check_id,
                what={"gpu_count": gpu_count},
            )
            return CheckResult(passed=False, event=event)

        if len(gpu_details) != gpu_count:
            event = render_message(
                Msg.DETAILS_MISMATCH,
                ctx=ctx,
                check_id=self.check_id,
                what={"gpu_count": gpu_count, "details_len": len(gpu_details)},
            )
            return CheckResult(passed=False, event=event, updates={"clear_verified_job_info": True})

        # The node is listed and priced as gpu_details[0] x gpu_count, so every card has to be that
        # model: a renter of a "2x RTX 4090" got a 4090 and an RTX 5060 Ti (OD-D850).
        models = Counter(detail.get("name") for detail in gpu_details)
        if len(models) > 1:
            enforce = settings.MIXED_GPU_MODEL_ENFORCEMENT_ENABLED
            what = {"listed_as": gpu_model, "gpu_count": gpu_count, "models": dict(models)}
            if enforce:
                # An availability entry hides the node from the market and refuses new rentals; a
                # renter already on it keeps running and is still billed (no verification reset).
                what = {"reach_source": str(ReachSource.VALIDATOR), "reach_target": str(ReachTarget.GPU_INVENTORY), **what}
            event = render_message(
                Msg.MODEL_MIXED,
                ctx=ctx,
                check_id=self.check_id,
                severity=None if enforce else "warning",
                category=AVAILABILITY_CATEGORY if enforce else None,
                impact=None if enforce else "Shadow observation only: score and listing were NOT changed",
                what=what,
            )
            return CheckResult(passed=not enforce, event=event)

        event = render_message(
            Msg.MODEL_OK,
            ctx=ctx,
            check_id=self.check_id,
            what={"gpu_model": gpu_model, "gpu_count": gpu_count},
        )
        return CheckResult(passed=True, event=event)
