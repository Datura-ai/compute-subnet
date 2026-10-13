"""Abstract base class for incentive algorithms."""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import bittensor
from pydantic import BaseModel
from services.const import GPU_MODEL_RATES
from services.redis_service import RedisService
from services.task_service import JobResult

from core.config import settings
from core.utils import _m, get_logger
from incentive.burn_service import BurnService
from incentive.config import IncentiveConfig
from incentive.utils import log_for_monitoring

logger = get_logger(__name__)


@dataclass
class _MixedGpuModelPortions:
    """One mixed-GPU node while it is scored as one virtual result per model (DAH-4219)."""

    sibling_results: list[JobResult]
    first_model_portion: JobResult  # the node's own result, cut down to its first portion's cards
    other_model_portions: list[JobResult]
    restore: dict[str, Any]  # the node's own fields the portions changed, put back at the merge


@dataclass(frozen=True)
class _PortionCards:
    gpu_model: str
    gpu_count: int
    is_rented: bool


class BaseIncentive(ABC):
    """Abstract base class for incentive calculation algorithms.

    All incentive algorithms must implement the calculate_executor_score
    and calculate_final_weights methods.
    """

    def __init__(
        self,
        config: IncentiveConfig,
        redis_service: RedisService,
        jobs_results: dict[str, list[JobResult]],
        total_gpu_model_count_map: dict,
        burn_service: BurnService | None = None,
    ):
        """Initialize the incentive algorithm.

        Args:
            config: Incentive configuration
            redis_service: Redis service for accessing shared state
            burn_service: Optional injected burn service, primarily for tests
        """
        self.config = config
        self.redis_service = redis_service
        self.burn_service = burn_service or BurnService()
        self.job_results = jobs_results
        self.total_gpu_model_count_map = total_gpu_model_count_map

    async def calculate_mining_scores(self):
        """Normalize the score of a job result.

        Args:
            all_job_results: All job results

        Returns:
            None
        """
        t1 = time.time()
        mixed_nodes: list[_MixedGpuModelPortions] = self._expand_mixed_gpu_model_results()
        for hotkey, results in self.job_results.items():
            for result in results:
                await self._pre_process_job_result(hotkey, result)

        await self._on_finish_pre_process()

        for hotkey, results in self.job_results.items():
            for result in results:
                await self._post_process_job_result(hotkey, result)
                self._split_incentive_across_pools(result)

        log_for_monitoring(
            self.job_results,
            t1,
            getattr(self, "unrented_count_by_bucket", None),
            unbucketed_share=getattr(self, "unbucketed_share", 0.0),
            unbucketed_rental_cost=getattr(self, "_unbucketed_rental_cost", 0.0),
        )
        self._merge_mixed_gpu_model_results(mixed_nodes)

    def _expand_mixed_gpu_model_results(self) -> list[_MixedGpuModelPortions]:
        """DAH-4219: score a mixed-GPU node as one virtual result per model.

        Each card is then paid at its own model's rate by the same rules as a node of that
        model alone, instead of every card at the first card's rate. Cards of a model with no
        rate get no portion and earn nothing. A partly rented split node gets a rented and a
        free portion per model (see `_portion_cards`). The portions are merged back afterwards:
        the downstream consumers expect one result per executor.
        """
        mixed_nodes: list[_MixedGpuModelPortions] = []
        for sibling_results in self.job_results.values():
            for result in list(sibling_results):
                model_counts: dict[str, int] = result.gpu_model_counts
                if len(model_counts) < 2:
                    continue
                portion_cards: list[_PortionCards] = self._portion_cards(result, model_counts)
                if not portion_cards:
                    continue
                restore: dict[str, Any] = {
                    name: getattr(result, name)
                    for name in (
                        "gpu_model",
                        "gpu_count",
                        "is_rented",
                        "is_split_remainder",
                        "rental_created_at",
                        "rented_gpu_count",
                        "filler_revenue_per_gpu_hour",
                    )
                }
                partly_rented: bool = any(not cards.is_rented for cards in portion_cards) and result.is_rented
                node = _MixedGpuModelPortions(sibling_results, result, [], restore)
                for index, cards in enumerate(portion_cards):
                    portion: JobResult = result if index == 0 else result.model_copy(deep=True)
                    if index:
                        portion.incentive_logs = []
                        portion.zero_incentive_reasons = []
                        sibling_results.append(portion)
                        node.other_model_portions.append(portion)
                    portion.gpu_model = cards.gpu_model
                    portion.gpu_count = cards.gpu_count
                    portion.is_rented = cards.is_rented
                    portion.rented_gpu_count = None
                    # each card at its own model's floor; a split remainder gets none, as on a single-model node
                    by_model = result.filler_revenue_per_gpu_hour_by_model
                    portion.filler_revenue_per_gpu_hour = (
                        (by_model or {}).get(cards.gpu_model) if not (partly_rented and not cards.is_rented) else None
                    )
                    if partly_rented and not cards.is_rented:
                        portion.is_split_remainder = True
                        portion.rental_created_at = None
                mixed_nodes.append(node)
                logger.info(
                    _m(
                        "Mixed-GPU node scored per model",
                        extra={
                            "executor_id": str(result.executor_info.uuid),
                            "gpu_models": model_counts,
                            "portions": [
                                {"gpu_model": c.gpu_model, "gpu_count": c.gpu_count, "is_rented": c.is_rented}
                                for c in portion_cards
                            ],
                        },
                    )
                )
        return mixed_nodes

    @staticmethod
    def _portion_cards(result: JobResult, model_counts: dict[str, int]) -> list[_PortionCards]:
        """The cards each portion of a mixed-GPU node is paid for, first card's model first.

        A partly rented split node (owner decision on #1580): the rented pool pays only the cards
        the pods hold, the free cards are a split remainder in the idle pool. When the pods' cards
        are not known, the rented pool pays `rented_gpu_count` of the cheapest cards and the rest
        earn nothing, so not knowing never pays more than knowing.
        """
        rated: dict[str, int] = {m: n for m, n in model_counts.items() if m in GPU_MODEL_RATES}
        rented_gpu_count: int | None = result.rented_gpu_count
        partly_rented: bool = bool(
            settings.ENABLE_SPLIT_PARTIAL_RENTAL_SCORING
            and result.is_rented
            and result.supports_gpu_splitting
            and result.gpu_splitting_min_count
            and rented_gpu_count
            and rented_gpu_count < result.gpu_count
        )
        if not partly_rented:
            return [_PortionCards(m, n, result.is_rented) for m, n in rated.items()]

        rented_counts: dict[str, int] | None = result.rented_gpu_model_counts
        free_paid: bool = rented_counts is not None and sum(rented_counts.values()) == rented_gpu_count
        if not free_paid:
            rented_counts = {}
            left: int = rented_gpu_count
            for gpu_model in sorted(model_counts, key=lambda m: GPU_MODEL_RATES.get(m, 0.0)):
                take: int = min(left, model_counts[gpu_model])
                if take:
                    rented_counts[gpu_model] = take
                left -= take
        portions: list[_PortionCards] = []
        for gpu_model, gpu_count in rated.items():
            rented: int = min(rented_counts.get(gpu_model, 0), gpu_count)
            if rented:
                portions.append(_PortionCards(gpu_model, rented, True))
            if free_paid and gpu_count - rented:
                portions.append(_PortionCards(gpu_model, gpu_count - rented, False))
        return portions

    @staticmethod
    def _merge_mixed_gpu_model_results(mixed_nodes: list[_MixedGpuModelPortions]) -> None:
        # miner_incentives already accumulated every portion during post-processing, so summing
        # here only restores the per-executor view the downstream consumers read.
        for node in mixed_nodes:
            merged: JobResult = node.first_model_portion
            portions: list[JobResult] = [merged, *node.other_model_portions]
            # taken while each portion still carries the model and count its formula ran on
            merged.record_per_gpu_model_formula_inputs(
                [
                    {
                        "gpu_model": portion.gpu_model,
                        "gpu_count": portion.gpu_count,
                        "is_rented": portion.is_rented,
                        "incentive": portion.incentive,
                        "incentive_formula_version": portion.incentive_formula_version,
                        "incentive_formula_inputs": portion.incentive_formula_inputs,
                    }
                    for portion in portions
                ]
            )
            for portion in node.other_model_portions:
                if portion.mining_score is not None:
                    merged.mining_score = (merged.mining_score or 0.0) + portion.mining_score
                if portion.incentive is not None:
                    merged.set_incentive_split(
                        (merged.incentive_rented or 0.0) + (portion.incentive_rented or 0.0),
                        (merged.incentive_idle or 0.0) + (portion.incentive_idle or 0.0),
                    )
                # a reason about the node itself is recorded once per portion: keep one
                merged.incentive_logs.extend(
                    line for line in portion.incentive_logs if line not in merged.incentive_logs
                )
                merged.zero_incentive_reasons.extend(
                    reason for reason in portion.zero_incentive_reasons if reason not in merged.zero_incentive_reasons
                )
            for name, value in node.restore.items():
                setattr(merged, name, value)
            # Drop by identity: two portions of one executor can compare equal as pydantic models.
            node.sibling_results[:] = [
                result
                for result in node.sibling_results
                if not any(result is portion for portion in node.other_model_portions)
            ]

    @staticmethod
    def _split_incentive_across_pools(result: JobResult) -> None:
        """DAH-2467: the single place a scored result gets its rented/idle breakdown.

        A whole node earns from one pool only, so the pair is just its incentive on the
        side it was scored on. A partially rented split node is merged from two portions
        afterwards, which overwrites the pair with the real two-pool split.
        """
        if result.incentive is None:
            return
        rented: float = result.incentive if result.is_rented else 0.0
        result.set_incentive_split(rented, result.incentive - rented)

    async def _pre_process_job_result(self, hotkey: str, result: JobResult) -> JobResult:
        """Callback before post-processing a job result.

        Args:
            result: Job execution result to process
        """
        pass


    async def _post_process_job_result(self, hotkey: str, result: JobResult) -> JobResult:
        """Callback after post-processing a job result.

        Args:
            result: Job execution result to process
        """
        pass


    async def _on_finish_pre_process(self) -> None:
        """Callback after pre-processing all job results.

        Args:
            None

        Returns:
            None
        """
        pass

    def get_snapshot(self) -> BaseModel | None:
        """Return a serializable snapshot of the current epoch state.

        Subclasses override this to expose their state for the /incentive-snapshot endpoint.
        """
        return None

    @abstractmethod
    async def calculate_executor_score(
        self,
        job_result: JobResult,
    ) -> JobResult:
        """
        Calculate mining score for a single executor/job result.

        Args:
            job_result: Job execution result to score

        Returns:
            JobResult with calculated mining score
        """
        raise NotImplementedError

    @abstractmethod
    async def calculate_final_weights(
        self,
        miners: list[bittensor.NeuronInfo],
        last_mechanism_step_block: int | None,
        current_epoch: int | None = None,
    ) -> dict[str, float]:
        """Calculate final weights with burning logic applied for this cycle.

        This method receives mining scores from Phase 1 (calculate_executor_score)
        for the current cycle only. It applies burning logic and returns weights
        that will be accumulated across cycles.

        Args:
            miners: List of all miners (needed to identify burners by UID)
            last_mechanism_step_block: For burner selection randomization

        Returns:
            dict[str, float]: Weights with burning applied for each miner.
                - Burners receive high scores (proportional to the burn emission share)
                - Regular miners receive low scores (proportional to 1 - burn emission share)
                These weights are accumulated across cycles in validator.miner_scores
        """
        pass
