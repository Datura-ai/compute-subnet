"""lium-platform F-29: a build-log batch goes to the backend as one websocket frame, which it caps at 1 MiB."""

import json
from unittest.mock import Mock

import pytest
from services.docker_service import (
    LOG_BATCH_MAX_CHARS,
    LOG_LINE_MAX_CHARS,
    DockerService,
    log_batches,
)


def _log(text: str) -> dict:
    return {"log_text": text, "log_status": "success", "log_tag": "build"}


def test_a_batch_carries_at_most_the_character_budget_and_keeps_every_line_in_order():
    logs = [_log(f"{i:04d}" + "x" * 996) for i in range(1000)]

    batches = log_batches(logs, max_chars=10_000)

    assert all(sum(len(log["log_text"]) for log in batch) <= 10_000 for batch in batches)
    assert [log for batch in batches for log in batch] == logs


def test_a_line_longer_than_the_line_cap_is_cut_and_says_so():
    (batch,) = log_batches([_log("y" * 50)], line_max_chars=10)

    assert batch == [_log("y" * 10 + " [40 chars cut]")]


def test_a_batch_of_the_worst_escaping_text_stays_under_the_backends_1_mib_frame():
    # a control character is 6 bytes in JSON (\u0001); a dense build log of them must still fit one frame
    logs = [_log("\x01" * LOG_LINE_MAX_CHARS) for _ in range(64)]

    for batch in log_batches(logs):
        assert len(json.dumps({"logs": batch}).encode()) < 1024 * 1024
    assert LOG_BATCH_MAX_CHARS * 6 < 1024 * 1024


@pytest.mark.asyncio
async def test_a_noisy_build_is_published_in_frames_the_backend_accepts():
    published: list[dict] = []

    class _Redis:
        async def publish(self, channel, payload):
            published.append(payload)

    svc = DockerService(
        ssh_service=Mock(),
        redis_service=_Redis(),
        attestation_service=Mock(),
        rental_docker_client_factory=Mock(),
    )
    for _ in range(1000):  # ~4 MB accumulated within one 500 ms interval
        await svc.stream_log("z" * 4000, "success", "build")
    await svc.finish_stream_logs()

    await svc.handle_stream_logs(miner_hotkey="m", executor_id="e", pod_id="p")

    assert len(published) > 1
    assert sum(len(p["logs"]) for p in published) == 1000
    assert all(len(json.dumps(p).encode()) < 1024 * 1024 for p in published)
