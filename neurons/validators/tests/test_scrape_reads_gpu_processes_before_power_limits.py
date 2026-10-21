"""The scrape lists every card's GPU processes before it reads any power limit (DAH-4941).

The validator caps a PEARL filler's GPUs before it creates the container, so a filler process in that
list proves the limits were read after the cap. The backend's power-cap guard judges only such runs:
with the processes read after the limits, a run that started in between was judged on pre-cap power.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def _call_lines(function: ast.FunctionDef, name: str) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == name
    ]


def test_every_gpu_process_is_listed_before_the_first_power_limit_read():
    tree = ast.parse((SRC / "miner_jobs" / "machine_scrape.py").read_text())
    specs = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "get_machine_specs"
    )

    process_reads = _call_lines(specs, "nvmlDeviceGetComputeRunningProcesses_v2")
    power_reads = _call_lines(specs, "nvmlDeviceGetPowerManagementLimit")

    assert process_reads and power_reads
    assert max(process_reads) < min(power_reads)
