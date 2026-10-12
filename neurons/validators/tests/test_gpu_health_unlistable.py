"""A node whose GPU failed a health check must not be listed or rented (GPU_HEALTH_UNLISTABLE_ENABLED).

A failed GPU check alone only zeroed the score, and the market does not filter on score: a node
with wedged cards stayed listed and one renter rented it five times (F-1592/F-1593).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from datura.requests.miner_requests import ExecutorSSHInfo
from payload_models.payloads import MinerJobRequestPayload
from services.attestation_service import HostPolicyResult
from services.task import service as task_service_module
from services.task.availability import (
    GPU_HEALTH_FAILURE_CODES,
    AvailabilityErrorCode,
    availability_errors,
    gpu_health_events,
    silence_availability_errors_on_our_own_outage,
    silence_gpu_health_errors_on_our_own_fault,
)
from services.task.models import JobResult, build_msg
from services.task.service import TaskService


def _event(reason: str, event: str = "GPU check", remediation: str | None = "Reset the card.") -> object:
    return build_msg(event=event, reason=reason, severity="error", impact="Score is zero.", remediation=remediation)


@pytest.mark.parametrize("reason", sorted(GPU_HEALTH_FAILURE_CODES))
def test_every_gpu_health_failure_becomes_one_gpu_unhealthy_entry(reason: str) -> None:
    # Arrange
    events = [_event("GPU_MODEL_OK"), _event(reason, event="GPU 0 is wedged"), _event("FINALIZE")]

    # Act
    problems = availability_errors(gpu_health_events(events))

    # Assert
    assert [p.model_dump() for p in problems] == [
        {
            "reason_code": AvailabilityErrorCode.GPU_UNHEALTHY,
            "reach_source": "validator",
            "reach_target": "gpu_health",
            "message": "A GPU on this node failed a health check",
            "remediation": "Reset the card.",
            "what_we_saw": {"failed_check": reason, "failure": "GPU 0 is wedged"},
        }
    ]


@pytest.mark.parametrize(
    "reason",
    # a check that could not run, or did not finish, says nothing about the card
    ["GPU_VERIFY_TIMEOUT", "GPU_VERIFY_SKIPPED_RENTED", "GPU_FAULT_PROBE_UNKNOWN", "GPU_VERIFY_OK", "GPU_MODEL_MIXED"],
)
def test_a_check_that_did_not_find_a_broken_card_hides_nothing(reason: str) -> None:
    assert gpu_health_events([_event(reason)]) == []


def test_the_entry_falls_back_to_a_generic_remediation() -> None:
    [event] = gpu_health_events([_event("GPU_WEDGED", remediation=None)])

    assert "dmesg" in event.remediation


def _result_with(errors: list[dict] | None) -> JobResult:
    return JobResult(
        executor_info=ExecutorSSHInfo(
            uuid="node", address="10.0.0.5", port=8080, ssh_username="root", ssh_port=2200,
            python_path="/usr/bin/python3", root_dir="/root/app",
        ),
        score=0, job_score=0, job_batch_id="b", log_status="error", log_text="x",
        availability_errors=errors,
    )


UNHEALTHY = {"reason_code": "GPU_UNHEALTHY", "reach_source": "validator", "reach_target": "gpu_health"}
UNREACHABLE = {"reason_code": "EXECUTOR_SSH_UNREACHABLE", "reach_source": "validator", "reach_target": "executor_ssh"}


def test_a_few_broken_nodes_stay_hidden() -> None:
    # Arrange
    results = [_result_with([UNHEALTHY]) for _ in range(2)] + [_result_with([]) for _ in range(8)]

    # Act
    silenced = silence_gpu_health_errors_on_our_own_fault(results)

    # Assert
    assert silenced == 0
    assert results[0].availability_errors == [UNHEALTHY]


def test_most_of_a_cycle_failing_a_gpu_check_is_our_fault_and_hides_nothing_new() -> None:
    # Arrange: one of the unhealthy nodes is also unreachable; that entry is not ours to drop here
    results = (
        [_result_with([UNHEALTHY]) for _ in range(7)]
        + [_result_with([UNHEALTHY, UNREACHABLE])]
        + [_result_with([]) for _ in range(2)]
    )

    # Act
    silenced = silence_gpu_health_errors_on_our_own_fault(results)

    # Assert - None, so the backend keeps what it stored
    assert silenced == 8
    assert all(result.availability_errors is None for result in results[:7])
    assert results[7].availability_errors == [UNREACHABLE]
    assert results[8].availability_errors == []


def test_broken_gpus_do_not_count_as_failed_connects() -> None:
    results = [_result_with([UNHEALTHY]) for _ in range(8)] + [_result_with([UNREACHABLE]), _result_with([])]

    assert silence_availability_errors_on_our_own_outage(results) == 0
    assert results[0].availability_errors == [UNHEALTHY]


def _service_whose_pipeline_ends_with(events: list) -> TaskService:
    service = TaskService.__new__(TaskService)
    service.ssh_service = MagicMock()
    service.ssh_service.decrypt_payload = MagicMock(return_value="decrypted-key")
    service.attestation_service = MagicMock()
    service.attestation_service.prepare_host_policy = AsyncMock(return_value=HostPolicyResult())
    service.redis_service = MagicMock()
    service.pipeline_factory = MagicMock()
    service.pipeline_factory.build_context = AsyncMock(return_value=MagicMock())
    pipeline = MagicMock()
    pipeline.run = AsyncMock(return_value=(False, events, MagicMock(success=False)))
    service.pipeline_factory.build_pipeline = MagicMock(return_value=pipeline)
    return service


class _Shell:
    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self) -> "_Shell":
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_a_wedged_node_is_hidden_only_when_the_flag_is_on(enabled: bool, monkeypatch) -> None:
    # Arrange
    monkeypatch.setattr(task_service_module, "InteractiveShellService", _Shell)
    monkeypatch.setattr(task_service_module.settings, "GPU_HEALTH_UNLISTABLE_ENABLED", enabled)
    monkeypatch.setattr(task_service_module.PipelineFactory, "takes_fast_path", staticmethod(lambda *_: False))
    service = _service_whose_pipeline_ends_with([_event("GPU_USAGE_OK"), _event("GPU_WEDGED")])
    handler = MagicMock()
    handler.handle_result = AsyncMock(return_value=_result_with(None))
    executor = ExecutorSSHInfo(
        uuid="node-9", address="10.0.0.5", port=8080, ssh_username="root", ssh_port=2200,
        python_path="/usr/bin/python3", root_dir="/root/app",
    )
    miner = MinerJobRequestPayload(
        job_batch_id="batch-1", miner_hotkey="5Miner", miner_coldkey="5Cold",
        miner_address="10.0.0.5", miner_port=8080, executors=[],
    )

    # Act
    with patch.object(task_service_module, "ResultHandler", return_value=handler):
        result = await service.create_task(
            miner_info=miner, executor_info=executor, keypair=MagicMock(ss58_address="5Val"),
            private_key="key", public_key="pub", encrypted_files=MagicMock(),
            rented_data=MagicMock(), default_docker_image_digests={},
        )

    # Assert
    assert result.failure_reason_code == "GPU_WEDGED"
    assert [e["reason_code"] for e in result.availability_errors] == (["GPU_UNHEALTHY"] if enabled else [])
