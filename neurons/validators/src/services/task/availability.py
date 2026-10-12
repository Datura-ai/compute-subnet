"""DAH-2748: validation errors that mean "someone could not reach something".

An availability error says the platform could not talk to a machine or a service, not that the
machine failed a check. One is enough to take the node off the market: a node nobody can reach
cannot serve a customer, and offering it produces a failed rental.

The class is shared on purpose. A new reachability check joins it by naming who could not reach
what and emitting the event — the validator reaching the node today, the container reaching
Docker Hub or Hugging Face tomorrow. Nothing downstream changes: the backend hides any node
whose last cycle carried an availability error, and the provider portal shows the reason.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field
from services.task.models import AVAILABILITY_CATEGORY, JobResult, ValidationEvent, build_msg


class ReachSource(StrEnum):
    """Who tried to reach something."""

    VALIDATOR = "validator"
    CONTAINER = "container"
    MINER = "miner"


class ReachTarget(StrEnum):
    """What could not be reached. Add a member for every new reachability check."""

    EXECUTOR_SSH = "executor_ssh"
    # The executor's HTTP API, the miner's side of the node.
    EXECUTOR_API = "executor_api"
    # Not a reachability failure: the node's GPUs are not all the model it is listed as (OD-D850).
    # It rides this class because an availability entry is the one hide that closes the listing and
    # new rentals while leaving `active` (a live renter's billing) alone, and the next cycle that
    # does not raise it lists the node again.
    GPU_INVENTORY = "gpu_inventory"
    # Not a reachability failure either: a GPU on a node the validator reached failed a health
    # check (wedged, no CUDA device, a card off the bus, a rented card that dropped or faults).
    GPU_HEALTH = "gpu_health"


# The longest peer-supplied text a reading may keep: the node writes it, the portal shows it.
MAX_PEER_TEXT_LENGTH = 500
# Above this share of one cycle failing at the connect, the validator itself is the suspect and
# the cycle reports nothing about reachability.
FLEET_SHARE_THAT_MEANS_OUR_OWN_OUTAGE = 0.5
# Below this many checked nodes a share says nothing: one bad node out of two is already "most of
# them". A validator this small also cannot empty the market, so it reports what it found.
SMALLEST_FLEET_THAT_CAN_SHOW_AN_OUTAGE = 5


class AvailabilityErrorCode(StrEnum):
    """The code the backend stores and the portal shows. One per check."""

    EXECUTOR_SSH_UNREACHABLE = "EXECUTOR_SSH_UNREACHABLE"
    # DAH-3558: the backend lists the executor as rented, the miner answered the wave without it.
    RENTED_EXECUTOR_NOT_LISTED = "RENTED_EXECUTOR_NOT_LISTED"
    # A GPU failed a health check this cycle; the node must not take a new rental on it.
    GPU_UNHEALTHY = "GPU_UNHEALTHY"


# The failures that mean a card on the node is broken, not that a check could not run. A timeout,
# a skipped check or an UNKNOWN verdict is not here: it says nothing about the card.
GPU_HEALTH_FAILURE_CODES = frozenset(
    {
        "GPU_COUNT_ZERO",
        "GPU_DETAILS_MISMATCH",  # the scrape lists fewer cards than the node reports
        "GPU_MISSING",  # an anchored card is gone
        "GPU_WEDGED",  # 100% utilisation with no process, and the cure did not clear it
        "GPU_VERIFY_FAILED",
        "GPU_VERIFY_CUDA_NOT_READY",
        "GPU_VERIFY_NO_CUDA_DEVICE",
        "GPU_VERIFY_VRAM_UNAVAILABLE",
        "GPU_FAULT_PROBE_FAILED",  # ECC, remapped rows, Xid (GPU_FAULT_PROBE_CHECK_ENABLED)
        "RENTED_GPU_DROP",  # a rented card left the bus (RENTED_GPU_DROP_CHECK_ENABLED)
        "RENTED_GPU_HEALTH_SHADOW_FAULT",  # the NVML/Xid read on a rented node (lium-io#1552)
    }
)


def build_availability_event(
    *,
    code: AvailabilityErrorCode,
    reach_source: ReachSource,
    reach_target: ReachTarget,
    event_text: str,
    impact: str,
    remediation: str,
    what_we_saw: dict[str, str | int | None],
) -> ValidationEvent:
    """One availability error, naming who could not reach what."""
    return build_msg(
        event=event_text,
        reason=str(code),
        severity="error",
        category=AVAILABILITY_CATEGORY,
        impact=impact,
        remediation=remediation,
        what={
            "reach_source": str(reach_source),
            "reach_target": str(reach_target),
            **what_we_saw,
        },
    )


class AvailabilityError(BaseModel):
    """One reachability check that failed, in the shape the provider portal renders.

    A cycle can fail more than one — the image registry and the model hub, say — and each needs
    its own line: what could not be reached, and what we saw when we tried.
    """

    reason_code: str
    reach_source: str
    reach_target: str
    message: str
    remediation: str | None = None
    what_we_saw: dict[str, Any] = Field(default_factory=dict)


def availability_errors(events: list[ValidationEvent] | None) -> list[AvailabilityError]:
    """Every availability error this cycle raised, in the order the checks ran.

    The whole event list is read, not only the last one: a check that fails early still hides
    the node. One entry per reason code — a check that fires twice is still one problem.
    """
    errors: dict[str, AvailabilityError] = {}
    for event in events or []:
        if not event.is_availability_error or event.reason_code in errors:
            continue
        seen = dict(event.what_we_saw)
        errors[event.reason_code] = AvailabilityError(
            reason_code=event.reason_code,
            reach_source=str(seen.pop("reach_source", "")),
            reach_target=str(seen.pop("reach_target", "")),
            message=event.event,
            remediation=event.remediation,
            what_we_saw=seen,
        )
    return list(errors.values())


def build_ssh_unreachable_event(
    *, executor_uuid: str, host: str, port: int | None, error: str
) -> ValidationEvent:
    return build_availability_event(
        code=AvailabilityErrorCode.EXECUTOR_SSH_UNREACHABLE,
        reach_source=ReachSource.VALIDATOR,
        reach_target=ReachTarget.EXECUTOR_SSH,
        event_text="Validator cannot open SSH to this node",
        impact="The node is hidden from the market and cannot take new rentals until a check succeeds.",
        remediation=(
            "Check that sshd on the node accepts the validator on its management port, "
            "and that no firewall or rate limit rejects the connection."
        ),
        what_we_saw={
            "executor_uuid": executor_uuid,
            "ssh_host": host,
            "ssh_port": port,
            # The node's own sshd writes this text, and the provider portal renders it, so the
            # node is not allowed to store a reading of any length it likes.
            "error": error[:MAX_PEER_TEXT_LENGTH],
        },
    )


def build_rented_executor_not_listed_event(
    *, executor_uuid: str, host: str, port: int | None, miner_hotkey: str
) -> ValidationEvent:
    """DAH-3558: a rented node the miner left out of its answer to the wave.

    No pipeline runs for a node the miner does not list, so without this event the wave writes
    nothing about it. This event is the wave's record: the backend lists the executor as rented,
    the miner did not return it. Why the miner left it out is the miner's business (it may drop
    a node before it ever tries to reach it), so the text says only that.
    """
    return build_availability_event(
        code=AvailabilityErrorCode.RENTED_EXECUTOR_NOT_LISTED,
        reach_source=ReachSource.MINER,
        reach_target=ReachTarget.EXECUTOR_API,
        event_text="Rented node missing from the miner's answer",
        impact=(
            "Score 0 for this cycle; the node is hidden from the market until a cycle lists it "
            "again. The backend's staleness sweep reads this row as the node not answering."
        ),
        remediation=(
            "The miner did not return this node in its answer to the validator. Check that the "
            "node is up and that the miner lists it."
        ),
        what_we_saw={
            "executor_uuid": executor_uuid,
            "executor_ip_address": host,
            "executor_port": port,
            "miner_hotkey": miner_hotkey,
        },
    )


def is_our_own_outage(unreachable_count: int, checked_count: int) -> bool:
    """True when so much of one cycle failed at the connect that the validator is the suspect.

    One node that refuses SSH is the node's problem. Most of a cycle refusing at once is ours —
    our egress, our DNS, our keys — and hiding the whole market over it is worse than listing a
    node nobody can reach for one more cycle. The backend's hourly sweep guards `active` the
    same way (DAH-2658).

    A cycle with only a few nodes is exempt: there the share carries no signal, and a validator
    that small cannot empty the market anyway.
    """
    if checked_count < SMALLEST_FLEET_THAT_CAN_SHOW_AN_OUTAGE:
        return False
    return unreachable_count / checked_count > FLEET_SHARE_THAT_MEANS_OUR_OWN_OUTAGE


def _is_the_validators_own_reading(job_result: JobResult) -> bool:
    """False for a result the validator wrote without contacting the node.

    A RENTED_EXECUTOR_NOT_LISTED row (DAH-3558) is the miner's answer, not a connect the validator
    made, so it says nothing about our egress, DNS or keys: it is left out of the outage ratio on
    both sides, and the silencer leaves it alone. A wave-wide drop by one miner would otherwise
    read as our outage and silence real SSH failures on the rest of the cycle.
    """
    errors = job_result.availability_errors
    if errors is None:
        return False
    return not errors or any(error.get("reach_source") != ReachSource.MINER for error in errors)


# Entries that are a reading of a node the validator did reach, not a failed connect: they say
# nothing about our egress, DNS or keys, so they do not count toward the outage share.
NOT_A_CONNECT_TARGETS = frozenset({ReachTarget.GPU_INVENTORY, ReachTarget.GPU_HEALTH})


def _failed_to_connect(job_result: JobResult) -> bool:
    return any(error.get("reach_target") not in NOT_A_CONNECT_TARGETS for error in job_result.availability_errors or [])


def silence_availability_errors_on_our_own_outage(job_results: list[JobResult]) -> int:
    """Report nothing about reachability when the cycle looks like our own outage.

    Returns how many results were silenced, so the caller can log it; 0 means the cycle is
    trusted and every result keeps what it found. Only the validator's own readings count and
    are silenced (``_is_the_validators_own_reading``).
    """
    checked_results = [result for result in job_results if _is_the_validators_own_reading(result)]
    unreachable_results = [result for result in checked_results if _failed_to_connect(result)]
    if not is_our_own_outage(len(unreachable_results), len(checked_results)):
        return 0
    for result in checked_results:
        # None means "this cycle did not check": the backend leaves the stored errors alone.
        result.availability_errors = None
    return len(checked_results)


def build_gpu_unhealthy_event(failure: ValidationEvent) -> ValidationEvent:
    """The availability entry for a node whose GPU failed a health check this cycle.

    A failed GPU check alone only zeroes the score, and the market does not filter on score, so a
    wedged or faulting node stayed listed and was rented again. The entry hides it and refuses
    new rentals; a rental already on it keeps running. The first cycle that finds every card
    healthy reports no entry, and the node is listed again.
    """
    return build_availability_event(
        code=AvailabilityErrorCode.GPU_UNHEALTHY,
        reach_source=ReachSource.VALIDATOR,
        reach_target=ReachTarget.GPU_HEALTH,
        event_text="A GPU on this node failed a health check",
        impact=(
            "The node is hidden from the market and cannot take new rentals until a check finds every "
            "GPU healthy. A rental already on it keeps running."
        ),
        remediation=failure.remediation
        or "Check nvidia-smi and the kernel log (dmesg) for Xid errors, then reset or replace the card.",
        what_we_saw={
            "failed_check": failure.reason_code,
            "failure": failure.event[:MAX_PEER_TEXT_LENGTH],
        },
    )


def gpu_health_events(events: list[ValidationEvent] | None) -> list[ValidationEvent]:
    """The GPU_UNHEALTHY entry for this cycle's first GPU health failure, or none."""
    for event in events or []:
        if event.reason_code in GPU_HEALTH_FAILURE_CODES:
            return [build_gpu_unhealthy_event(event)]
    return []


def _is_gpu_health_entry(error: dict) -> bool:
    return error.get("reach_target") == ReachTarget.GPU_HEALTH


def silence_gpu_health_errors_on_our_own_fault(job_results: list[JobResult]) -> int:
    """Drop the GPU_UNHEALTHY entries of a cycle where most nodes failed a GPU check.

    Most of the fleet breaking its GPUs in one cycle is a validator fault (a bad probe, a broken
    image), and hiding the whole market over it is worse than listing a bad node for one more
    cycle. A result left with no entry reports None, so the backend keeps what it stored.
    """
    checked_results = [result for result in job_results if result.availability_errors is not None]
    unhealthy_results = [
        result for result in checked_results if any(_is_gpu_health_entry(error) for error in result.availability_errors)
    ]
    if not is_our_own_outage(len(unhealthy_results), len(checked_results)):
        return 0
    for result in unhealthy_results:
        kept = [error for error in result.availability_errors if not _is_gpu_health_entry(error)]
        result.availability_errors = kept or None
    return len(unhealthy_results)
