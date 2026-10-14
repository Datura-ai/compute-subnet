"""The MATMUL_SPEED_SHADOW line: what one capability matmul measured, next to the reported GPU model.

It is a log only. These tests pin its fields for a sealed and an unsealed answer, that a missing or
malformed measurement gives null fields instead of an error, that the flag turns the line off, and
that the check's verdict is the same with the line on or off.
"""

import json
import logging
from types import SimpleNamespace

import pytest

from core.config import settings
from neurons.validators.src.services.task.checks import matmul_speed_log as speed_log
from neurons.validators.src.services.task.checks.capability import CapabilityCheck
from neurons.validators.src.services.task.messages import CapabilityMessages as Msg
from services import matrix_validation_service as mvs
from services.matrix_validation_service import ValidationResult
from tests.helpers import build_context_config, build_services, build_state
from neurons.validators.src.services.task.checks.local_verify import LocalVerifyCheck
from tests.test_local_verify import (  # noqa: F401 - keypair, local_verify_on and verifyx_service are fixtures
    SPECS,
    FakeExecutor,
    FakeSSH,
    client_factory,
    context,
    keypair,
    local_verify_on,
    matmul_service,
    run_local_then_consumers,
    verifyx_service,
)

NODE_METRICS = {
    "device": "NVIDIA H100 80GB HBM3",
    "cc": "9.0",
    "sm_count": 132,
    "clock_mhz": 1980,
    "vram_gb": 79.6,
    "n": 8192,
    "fp32_tflops": 51.2,
    "fp32_median_tflops": 50.9,
    "fp32_cov_pct": 0.8,
}
EIGHT_CARD_SPECS = {
    "gpu": {
        "count": 8,
        "details": [
            {"uuid": f"GPU-{i}", "name": "NVIDIA H100 80GB HBM3", "capacity": 81559}
            for i in range(8)
        ],
    }
}


def _speed_records(caplog) -> list[dict]:
    return [
        record.msg.extra
        for record in caplog.records
        if str(record.msg) == speed_log.MATMUL_SPEED_SHADOW_EVENT
    ]


def _result(**overrides) -> ValidationResult:
    values = dict(
        success=True,
        metrics=dict(NODE_METRICS),
        sealed=True,
        elapsed_seconds=12.3456,
        elapsed_source=mvs.ELAPSED_SOURCE_VALIDATOR,
        dim_n=2000,
        dim_k=2_500_000,
    )
    values.update(overrides)
    return ValidationResult(**values)


def test_sealed_run_logs_one_line_with_the_node_figures(caplog):
    with caplog.at_level(logging.INFO, logger=speed_log.logger.name):
        speed_log.log_matmul_speed(
            EIGHT_CARD_SPECS, _result(), transport="ssh", default_extra={"executor_uuid": "node-1"}
        )

    records = _speed_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record["executor_uuid"] == "node-1"
    assert record["reported_gpu_model"] == "NVIDIA H100 80GB HBM3"
    assert record["reported_gpu_count"] == 8
    assert record["device_index"] == 0
    assert record["transport"] == "ssh"
    assert record["success"] is True
    assert record["timed_out"] is False
    assert record["sealed"] is True
    assert record["elapsed_seconds"] == 12.346
    assert record["elapsed_source"] == "validator_wall_clock"
    assert record["dim_n"] == 2000
    assert record["dim_k"] == 2_500_000
    assert "matrix_bytes" not in record
    assert record["node_metrics_present"] is True
    assert record["node_fp32_tflops"] == 51.2
    assert record["node_fp32_median_tflops"] == 50.9
    assert record["node_fp32_cov_pct"] == 0.8
    assert record["node_benchmark_n"] == 8192
    assert record["node_device_name"] == "NVIDIA H100 80GB HBM3"
    assert record["node_compute_capability"] == "9.0"
    assert record["node_sm_count"] == 132
    assert record["node_clock_mhz"] == 1980
    assert record["node_vram_gb"] == 79.6
    # 2 * n^2 * k operations over the elapsed time, in units of 1e12.
    assert record["computed_matmul_tflops"] == round(2 * 2000**2 * 2_500_000 / 12.3456 / 1e12, 4)
    json.dumps(record)


def test_unsealed_run_is_marked_unsealed(caplog):
    with caplog.at_level(logging.INFO, logger=speed_log.logger.name):
        speed_log.log_matmul_speed(SPECS, _result(sealed=False), transport="ssh")

    (record,) = _speed_records(caplog)
    assert record["sealed"] is False
    assert record["node_fp32_tflops"] == 51.2
    assert record["reported_gpu_model"] == "H100"
    assert record["reported_gpu_count"] == 1


def test_missing_measurements_are_null(caplog):
    bare = ValidationResult(success=True)
    with caplog.at_level(logging.INFO, logger=speed_log.logger.name):
        speed_log.log_matmul_speed({"gpu": {"count": 2}}, bare, transport="ssh")

    (record,) = _speed_records(caplog)
    assert record["reported_gpu_model"] is None
    assert record["reported_gpu_count"] == 2
    assert record["sealed"] is None
    assert record["node_metrics_present"] is False
    for field in (
        "elapsed_seconds",
        "elapsed_source",
        "dim_n",
        "dim_k",
        "node_fp32_tflops",
        "node_fp32_median_tflops",
        "node_fp32_cov_pct",
        "node_benchmark_n",
        "node_device_name",
        "node_compute_capability",
        "node_sm_count",
        "node_clock_mhz",
        "node_vram_gb",
        "computed_matmul_tflops",
    ):
        assert record[field] is None, field


def test_passing_run_without_an_elapsed_time_still_logs(caplog):
    # A passing result that carries no elapsed time (e.g. one returned by a path that did not time
    # the run) still writes the line; only the time and the figure computed from it are null.
    untimed = _result(elapsed_seconds=None)
    with caplog.at_level(logging.INFO, logger=speed_log.logger.name):
        speed_log.log_matmul_speed(EIGHT_CARD_SPECS, untimed, transport="ssh")

    (record,) = _speed_records(caplog)
    assert record["success"] is True
    assert record["sealed"] is True
    assert record["dim_n"] == 2000
    assert record["node_fp32_tflops"] == 51.2
    assert record["elapsed_seconds"] is None
    assert record["elapsed_source"] is None
    assert record["computed_matmul_tflops"] is None


def test_record_and_reported_gpu_are_dataclasses():
    record = speed_log.build_matmul_speed_record(EIGHT_CARD_SPECS, _result(), transport="ssh")
    assert isinstance(record, speed_log.MatmulSpeedRecord)
    assert record.reported_gpu_model == "NVIDIA H100 80GB HBM3"
    assert speed_log._reported_gpu(EIGHT_CARD_SPECS) == speed_log.ReportedGpu(
        model="NVIDIA H100 80GB HBM3", count=8
    )
    assert speed_log._reported_gpu(None) == speed_log.ReportedGpu()


@pytest.mark.parametrize(
    "metrics",
    [
        "not-a-dict",
        ["fp32_tflops", 9999],
        {"fp32_tflops": "fast", "device": 7, "sm_count": True, "cc": None},
        {"fp32_tflops": float("nan"), "fp32_median_tflops": float("inf"), "n": -1},
    ],
)
def test_malformed_node_metrics_are_null_and_do_not_raise(caplog, metrics):
    with caplog.at_level(logging.INFO, logger=speed_log.logger.name):
        speed_log.log_matmul_speed(SPECS, _result(metrics=metrics, sealed=False), transport="ssh")

    (record,) = _speed_records(caplog)
    assert record["node_fp32_tflops"] is None
    assert record["node_fp32_median_tflops"] is None
    assert record["node_benchmark_n"] is None
    assert record["node_device_name"] is None
    assert record["node_sm_count"] is None
    assert record["node_compute_capability"] is None


@pytest.mark.parametrize(
    "specs", [None, {}, {"gpu": None}, {"gpu": {"count": "8", "details": "x"}}, []]
)
def test_malformed_specs_are_null_and_do_not_raise(caplog, specs):
    with caplog.at_level(logging.INFO, logger=speed_log.logger.name):
        speed_log.log_matmul_speed(specs, _result(), transport="ssh")

    (record,) = _speed_records(caplog)
    assert record["reported_gpu_model"] is None
    assert record["reported_gpu_count"] is None


def test_long_node_text_is_cut(caplog):
    with caplog.at_level(logging.INFO, logger=speed_log.logger.name):
        speed_log.log_matmul_speed(SPECS, _result(metrics={"device": "x" * 5000}), transport="ssh")

    (record,) = _speed_records(caplog)
    assert len(record["node_device_name"]) == 128


def test_failed_run_is_logged_without_a_computed_figure(caplog):
    failed = _result(success=False, timed_out=True, metrics=None, sealed=None)
    with caplog.at_level(logging.INFO, logger=speed_log.logger.name):
        speed_log.log_matmul_speed(SPECS, failed, transport="ssh")

    (record,) = _speed_records(caplog)
    assert record["success"] is False
    assert record["timed_out"] is True
    assert record["computed_matmul_tflops"] is None


def test_no_result_writes_no_line(caplog):
    with caplog.at_level(logging.DEBUG, logger=speed_log.logger.name):
        speed_log.log_matmul_speed(EIGHT_CARD_SPECS, None, transport="ssh")

    assert _speed_records(caplog) == []


def test_a_failure_while_building_the_line_is_swallowed(monkeypatch, caplog):
    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(speed_log, "build_matmul_speed_record", boom)
    with caplog.at_level(logging.DEBUG, logger=speed_log.logger.name):
        speed_log.log_matmul_speed(SPECS, _result(), transport="ssh")

    assert _speed_records(caplog) == []


# --- the service fills the measurement fields ---------------------------------------------------


def test_sealed_answer_carries_seal_and_size(monkeypatch):
    validation = matmul_service(monkeypatch)
    challenge = validation.prepare_matmul_challenge(SPECS, {}, vram_budget_mb=8192)
    stdout = "RESULT_JSON: " + json.dumps({"uuid": "ignored", "metrics": None, "sealed": "cafe"})

    result = validation.evaluate_matmul_output(challenge, stdout=stdout)
    challenge.close()

    assert result.success is True
    assert result.sealed is True
    assert (result.dim_n, result.dim_k) == (challenge.params.dim_n, challenge.params.dim_k)
    assert result.elapsed_seconds is None


def test_unsealed_answer_is_marked_unsealed(monkeypatch):
    validation = matmul_service(monkeypatch)
    challenge = validation.prepare_matmul_challenge(SPECS, {}, vram_budget_mb=8192)
    uuid = challenge.params.uuid
    stdout = "RESULT_JSON: " + json.dumps({"uuid": uuid, "metrics": {"fp32_tflops": 9999.0}})

    result = validation.evaluate_matmul_output(challenge, stdout=stdout)
    challenge.close()

    assert result.success is True
    assert result.sealed is False
    assert result.metrics == {"fp32_tflops": 9999.0}


def test_sealed_blob_on_a_library_without_unseal_is_unsealed(monkeypatch):
    validation = matmul_service(monkeypatch)
    validation.wrapper._has_sealed = False
    challenge = validation.prepare_matmul_challenge(SPECS, {}, vram_budget_mb=8192)
    uuid = challenge.params.uuid
    stdout = "RESULT_JSON: " + json.dumps({"uuid": uuid, "metrics": None, "sealed": "cafe"})

    result = validation.evaluate_matmul_output(challenge, stdout=stdout)
    challenge.close()

    assert result.success is True
    assert result.sealed is False


def test_failed_seal_authentication_is_not_marked_sealed(monkeypatch):
    validation = matmul_service(monkeypatch, sealed=None)
    challenge = validation.prepare_matmul_challenge(SPECS, {}, vram_budget_mb=8192)
    stdout = "RESULT_JSON: " + json.dumps({"uuid": "x", "metrics": None, "sealed": "cafe"})

    result = validation.evaluate_matmul_output(challenge, stdout=stdout)
    challenge.close()

    assert result.success is False
    assert result.sealed is None


@pytest.mark.asyncio
async def test_ssh_run_times_the_remote_command(monkeypatch):
    validation = matmul_service(monkeypatch)
    monkeypatch.setattr(settings, "MATMUL_ALLCARDS_CHECK_ENABLED", False)
    clock = iter([100.0, 107.5])
    monkeypatch.setattr(mvs.time, "perf_counter", lambda: next(clock))

    class _SSH:
        async def run(self, command, timeout=None):
            stdout = "RESULT_JSON: " + json.dumps(
                {"uuid": "ignored", "metrics": None, "sealed": "cafe"}
            )
            return SimpleNamespace(stdout=stdout, stderr="")

    result = await validation.validate_gpu_model_and_process_job(
        _SSH(), SimpleNamespace(root_dir="/root/app", python_path="python"), {}, SPECS
    )

    assert result.success is True
    assert result.elapsed_seconds == 7.5
    assert result.elapsed_source == "validator_wall_clock"
    assert result.sealed is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("local_verify_on")
async def test_local_transport_logs_the_executor_step_time(request, monkeypatch, caplog):
    signer = request.getfixturevalue("keypair")
    verifyx = request.getfixturevalue("verifyx_service")
    validation = matmul_service(monkeypatch)

    async with FakeExecutor(signer, step_sleep=0.02) as executor:
        ctx = context(
            signer,
            executor.executor_info,
            validation=validation,
            verifyx=verifyx,
            ssh=FakeSSH(),
        )
        with caplog.at_level(logging.INFO, logger=speed_log.logger.name):
            _local, _verifyx, capability = await run_local_then_consumers(
                ctx, LocalVerifyCheck(client_factory=client_factory(signer))
            )

    assert capability.passed
    (record,) = _speed_records(caplog)
    assert record["transport"] == "local_verify"
    assert record["elapsed_source"] == "executor_reported"
    assert record["elapsed_seconds"] == 0.02
    assert record["sealed"] is True


# --- the capability check writes the line and keeps its verdict -----------------------------------


class _Service:
    def __init__(self, result):
        self.result = result

    async def validate_gpu_model_and_process_job(self, **_kwargs):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def _run_check(context_factory, result, *, specs=EIGHT_CARD_SPECS):
    ctx = context_factory(
        services=build_services(validation=_Service(result)),
        config=build_context_config(),
        state=build_state(specs=specs),
        default_extra={"executor_uuid": "node-1"},
    )
    return await CapabilityCheck().run(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False])
async def test_check_verdict_is_unchanged_by_the_line(caplog, context_factory, success):
    result = _result(success=success, error_message="" if success else "UUID mismatch: x")

    with caplog.at_level(logging.INFO, logger=speed_log.logger.name):
        outcome = await _run_check(context_factory, result)

    assert outcome.passed is success
    assert outcome.event.reason_code == (Msg.VERIFY_OK if success else Msg.VERIFY_FAILED).reason
    records = _speed_records(caplog)
    assert len(records) == 1
    assert records[0]["executor_uuid"] == "node-1"
    assert records[0]["reported_gpu_count"] == 8
    assert records[0]["success"] is success


@pytest.mark.asyncio
async def test_check_writes_no_line_when_the_run_raised(caplog, context_factory):
    with caplog.at_level(logging.INFO, logger=speed_log.logger.name):
        outcome = await _run_check(context_factory, RuntimeError("ssh dropped"))

    assert outcome.passed is False
    assert _speed_records(caplog) == []


@pytest.mark.asyncio
async def test_check_writes_no_line_without_specs(caplog, context_factory):
    with caplog.at_level(logging.INFO, logger=speed_log.logger.name):
        outcome = await _run_check(context_factory, _result(), specs={})

    assert outcome.passed is False
    assert _speed_records(caplog) == []
