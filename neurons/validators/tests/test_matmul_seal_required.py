"""G18: a seal-aware verifier that gets no sealed blob judges the executor's plaintext uuid and metrics.

The plaintext is printed by the executor's own wrapper, so the answer is logged and, under
MATMUL_SEAL_REQUIRED_ENABLED, failed. A sealed answer is judged as before.
"""

import json
import logging

import pytest

from core.config import settings
from services import matrix_validation_service as mvs
from tests.test_local_verify import SPECS, matmul_service


def _unsealed_stdout(uuid: str) -> str:
    return (
        "UUID:  "
        + uuid
        + "\nRESULT_JSON: "
        + json.dumps({"uuid": uuid, "metrics": {"tflops": 9999.0}})
    )


def _sealed_stdout(uuid: str) -> str:
    return "RESULT_JSON: " + json.dumps(
        {"uuid": uuid, "metrics": {"tflops": 9999.0}, "sealed": "cafe"}
    )


def _unsealed_logs(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if "MATMUL_RESULT_UNSEALED" in record.getMessage()]


def test_unsealed_answer_is_logged_and_still_judged_by_default(monkeypatch, caplog):
    validation = matmul_service(monkeypatch)
    challenge = validation.prepare_matmul_challenge(SPECS, {}, vram_budget_mb=8192)

    with caplog.at_level(logging.WARNING, logger=mvs.logger.name):
        result = validation.evaluate_matmul_output(
            challenge, stdout=_unsealed_stdout(challenge.params.uuid)
        )
    challenge.close()

    assert result.success is True
    assert len(_unsealed_logs(caplog)) == 1


def test_unsealed_answer_fails_when_the_seal_is_required(monkeypatch):
    monkeypatch.setattr(settings, "MATMUL_SEAL_REQUIRED_ENABLED", True)
    validation = matmul_service(monkeypatch)
    challenge = validation.prepare_matmul_challenge(SPECS, {}, vram_budget_mb=8192)

    result = validation.evaluate_matmul_output(
        challenge, stdout=_unsealed_stdout(challenge.params.uuid)
    )
    challenge.close()

    assert result.success is False
    assert result.metrics is None


def test_sealed_answer_passes_and_logs_nothing_when_the_seal_is_required(monkeypatch, caplog):
    monkeypatch.setattr(settings, "MATMUL_SEAL_REQUIRED_ENABLED", True)
    validation = matmul_service(monkeypatch)
    challenge = validation.prepare_matmul_challenge(SPECS, {}, vram_budget_mb=8192)

    with caplog.at_level(logging.WARNING, logger=mvs.logger.name):
        result = validation.evaluate_matmul_output(challenge, stdout=_sealed_stdout("ignored"))
    challenge.close()

    assert result.success is True
    assert result.metrics == {"tflops": 42.0}
    assert _unsealed_logs(caplog) == []


def test_seal_is_not_required_by_default():
    assert type(settings).model_fields["MATMUL_SEAL_REQUIRED_ENABLED"].default is False
