import subprocess
import sys
from pathlib import Path

import pytest

from services import storage_operations


@pytest.mark.parametrize("field", ["bootstrap", "exclude_patterns"])
def test_workspace_field_probe_finds_the_field_in_this_repo_s_executor_models(field: str) -> None:
    # The probe imports the executor's own storage.models; run it against this checkout's copy
    # so a renamed field or a wrong sys.path root fails here, not on every encrypted create.
    executor_src = Path(__file__).resolve().parents[2] / "executor" / "src"
    probe = storage_operations._WORKSPACE_FIELD_PROBE.replace("/root/app/src", str(executor_src))

    assert subprocess.run([sys.executable, "-c", probe, field], check=False).returncode == 0
    assert subprocess.run([sys.executable, "-c", probe, "no_such_field"], check=False).returncode == 3
