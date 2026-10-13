"""lium-platform F-29: a build-log batch goes to the backend as one websocket frame, which it caps at 1 MiB."""

import json
from unittest.mock import Mock

import pytest

from services.docker_service import LOG_LINE_MAX_CHARS, DockerService, log_batches

MIB = 1024 * 1024


def _log(text: str) -> dict:
    return {"log_text": text, "log_status": "success", "log_tag": "build"}


def _service(redis) -> DockerService:
    return DockerService(
        ssh_service=Mock(),
        redis_service=redis,
        attestation_service=Mock(),
        rental_docker_client_factory=Mock(),
    )


def test_a_batch_serializes_within_the_byte_budget_and_keeps_every_line_in_order():
    logs = [_log(f"{i:04d}" + "x" * 996) for i in range(1000)]

    batches = log_batches(logs, max_bytes=10_000)

    assert all(len(json.dumps(batch)) <= 10_000 for batch in batches)
    assert [log for batch in batches for log in batch] == logs


def test_many_one_character_lines_are_budgeted_with_their_fields_not_only_their_text():
    # 131,072 one-character lines are 128 Ki of text but 8 MB of JSON
    logs = [_log("x") for _ in range(131_072)]

    batches = log_batches(logs)

    assert len(batches) > 8
    assert all(len(json.dumps({"logs": batch}).encode()) < MIB for batch in batches)


def test_a_line_longer_than_the_line_cap_is_cut_to_the_cap_and_says_so():
    (batch,) = log_batches([_log("y" * 500)], line_max_chars=100)

    assert len(batch[0]["log_text"]) == 100
    assert batch[0]["log_text"].endswith(" [cut from 500 chars]")
    assert log_batches(batch, line_max_chars=100) == [batch]


def test_a_batch_of_the_worst_escaping_text_stays_under_the_backends_1_mib_frame():
    # a control character is 6 bytes in JSON (\u0001)
    logs = [_log("\x01" * LOG_LINE_MAX_CHARS) for _ in range(64)]

    for batch in log_batches(logs):
        assert len(json.dumps({"logs": batch}).encode()) < MIB


@pytest.mark.asyncio
async def test_a_noisy_build_is_published_in_frames_the_backend_accepts():
    published: list[dict] = []

    class _Redis:
        async def publish(self, channel, payload):
            published.append(payload)

    svc = _service(_Redis())
    for _ in range(1000):  # ~4 MB accumulated within one 500 ms interval
        await svc.stream_log("z" * 4000, "success", "build")
    await svc.finish_stream_logs()

    await svc.handle_stream_logs(miner_hotkey="m", executor_id="e", pod_id="p")

    assert len(published) > 1
    assert sum(len(p["logs"]) for p in published) == 1000
    assert all(len(json.dumps(p).encode()) < MIB for p in published)


@pytest.mark.asyncio
async def test_the_batches_after_a_failed_publish_go_back_to_the_front_of_the_queue():
    published: list[dict] = []

    class _RedisFailingOnTheSecondPublish:
        async def publish(self, channel, payload):
            if len(published) == 1:
                raise ConnectionError("redis is down")
            published.append(payload)

    svc = _service(_RedisFailingOnTheSecondPublish())
    lines = [f"{i:04d}" + "z" * 3996 for i in range(1000)]
    for line in lines:
        await svc.stream_log(line, "success", "build")
    await svc.finish_stream_logs()

    await svc.handle_stream_logs(miner_hotkey="m", executor_id="e", pod_id="p")

    sent = [log["log_text"] for log in published[0]["logs"]]
    assert len(published) == 1
    assert sent + [log["log_text"] for log in svc.logs_queue] == lines
