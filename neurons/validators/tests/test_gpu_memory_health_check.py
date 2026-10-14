"""DAH-3110: a card NVML reports broken (uncorrected ECC, pending/failed row remap, recovery
action) is flagged on rented and idle nodes alike; flag only, the score is not changed."""

import json
import subprocess
import sys

import pytest
from neurons.validators.src.services.task import pipeline_factory
from neurons.validators.src.services.task.checks.gpu_fault_probe import (
    PROBE_JSON_MARKER,
    PROBE_SOURCE,
)
from neurons.validators.src.services.task.checks.gpu_memory_health import GpuMemoryHealthCheck
from neurons.validators.src.services.task.messages import GpuMemoryHealthMessages as Msg

from tests.test_gpu_fault_probe_check import FakeRunner, _probe_namespace, make_ctx

# a card seen on a rented 8x RTX PRO 6000 node, 14 Oct: 1 uncorrectable DRAM ECC error, a pending
# row remap and "Drain and Reset" as the recovery action
BROKEN = {
    "index": 2,
    "uuid": "GPU-2",
    "ecc_uncorrected": 1,
    "remapped_rows": [0, 1, 1, 0],
    "recovery_action": 4,
}
HEALTHY = {
    "index": 0,
    "uuid": "GPU-0",
    "ecc_uncorrected": 0,
    "remapped_rows": [0, 0, 0, 0],
    "recovery_action": 0,
}


def _report(status: str, **extra) -> str:
    return PROBE_JSON_MARKER + " " + json.dumps({"status": status, **extra}) + "\n"


def test_the_standing_faults_of_a_broken_card_are_each_flagged():
    nvml_health = _probe_namespace("nvml_health", "RECOVERY_ACTIONS")["nvml_health"]

    flags = nvml_health({"available": True, "gpus": [HEALTHY, BROKEN]})

    assert flags == [
        "gpu 2 (GPU-2): 1 uncorrected ECC error(s) since the driver loaded",
        "gpu 2 (GPU-2): row remap pending (needs a GPU reset)",
        "gpu 2 (GPU-2): NVML recovery action DRAIN_AND_RESET required",
    ]


def test_a_card_whose_reads_are_not_supported_is_not_flagged():
    nvml_health = _probe_namespace("nvml_health", "RECOVERY_ACTIONS")["nvml_health"]
    consumer = {"index": 0, "ecc_uncorrected": None, "remapped_rows": None, "recovery_action": None}

    assert nvml_health({"available": True, "gpus": [consumer]}) == []


def test_nvml_only_runs_no_cuda_and_always_prints_a_report():
    # no NVML binding here: the mode still prints its marker line and exits 0
    run = subprocess.run(
        [sys.executable, "-I", "-", "--nvml-only"],
        input=PROBE_SOURCE,
        capture_output=True,
        text=True,
        timeout=60,
    )

    line = next(line for line in run.stdout.splitlines() if line.startswith(PROBE_JSON_MARKER))
    report = json.loads(line[len(PROBE_JSON_MARKER) :])
    assert run.returncode == 0
    assert report["status"] in ("ok", "flagged", "error")
    assert "devices" not in report


@pytest.mark.asyncio
async def test_a_flagged_card_is_reported_and_the_check_still_passes(context_factory):
    flags = ["gpu 2 (GPU-2): row remap pending (needs a GPU reset)"]
    runner = FakeRunner(_report("flagged", flags=flags))

    result = await GpuMemoryHealthCheck().run(make_ctx(context_factory, runner))

    assert result.passed is True
    assert result.event.reason_code == Msg.FLAGGED.reason
    assert result.event.what_we_saw["flags"] == flags
    assert runner.calls[0]["cmd"].endswith("-I - --nvml-only")
    assert runner.calls[0]["stdin_text"] == PROBE_SOURCE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stdout, reason",
    [
        (_report("ok", flags=[]), Msg.OK.reason),
        (_report("error", flags=[], nvml={"available": False}), Msg.UNKNOWN.reason),
        ("no marker", Msg.UNKNOWN.reason),
    ],
)
async def test_a_healthy_or_unread_node_passes(context_factory, stdout, reason):
    result = await GpuMemoryHealthCheck().run(make_ctx(context_factory, FakeRunner(stdout)))

    assert (result.passed, result.event.reason_code) == (True, reason)


def test_the_read_runs_before_the_rented_short_circuit_in_every_pipeline():
    for build in (
        pipeline_factory.PipelineFactory.build_checks,
        pipeline_factory.PipelineFactory.build_fast_path_checks,
        pipeline_factory.PipelineFactory.build_dry_run_checks,
    ):
        ids = [check.check_id for check in build()]
        assert ids.index("gpu.validate.memory_health") < ids.index("executor.validate.rented_state")
