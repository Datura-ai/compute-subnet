"""lium-platform F-29: a build-log batch goes to the backend as one websocket frame, which it caps at 1 MiB."""

import asyncio
import json
from unittest.mock import Mock

import pytest
from services.docker_service import (
    LOG_LINE_MAX_CHARS,
    LOG_STREAM_FINAL_BACKOFF_S,
    DockerService,
    log_batches,
)

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
async def test_the_batches_after_a_failed_publish_go_back_to_the_front_of_the_queue(monkeypatch):
    monkeypatch.setattr("services.docker_service.LOG_STREAM_INTERVAL", 0)
    monkeypatch.setattr("services.docker_service.LOG_STREAM_FINAL_BACKOFF_S", (0,) * 6)
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


@pytest.mark.asyncio
async def test_a_publish_failing_during_the_final_pass_is_retried_before_the_stream_ends(
    monkeypatch,
):
    # a per-request DockerService: nothing publishes its queue once handle_stream_logs returns
    monkeypatch.setattr("services.docker_service.LOG_STREAM_INTERVAL", 0)
    monkeypatch.setattr("services.docker_service.LOG_STREAM_FINAL_BACKOFF_S", (0,) * 6)
    published: list[dict] = []
    calls = 0

    class _RedisFailingOnce:
        async def publish(self, channel, payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ConnectionError("redis blipped")
            published.append(payload)

    svc = _service(_RedisFailingOnce())
    lines = [f"{i:04d}" + "z" * 3996 for i in range(1000)]
    for line in lines:
        await svc.stream_log(line, "success", "build")
    await svc.finish_stream_logs()

    await svc.handle_stream_logs(miner_hotkey="m", executor_id="e", pod_id="p")

    assert [log["log_text"] for p in published for log in p["logs"]] == lines
    assert svc.logs_queue == []


@pytest.mark.asyncio
async def test_a_redis_that_stays_down_ends_the_stream_after_the_final_attempts(monkeypatch):
    monkeypatch.setattr("services.docker_service.LOG_STREAM_INTERVAL", 0)
    monkeypatch.setattr("services.docker_service.LOG_STREAM_FINAL_BACKOFF_S", (0,) * 6)
    attempts = 0

    class _RedisDown:
        async def publish(self, channel, payload):
            nonlocal attempts
            attempts += 1
            raise ConnectionError("redis is down")

    svc = _service(_RedisDown())
    await svc.stream_log("line", "success", "build")
    await svc.finish_stream_logs()

    await asyncio.wait_for(svc.handle_stream_logs(miner_hotkey="m", executor_id="e", pod_id="p"), 5)

    assert attempts == 1 + len(LOG_STREAM_FINAL_BACKOFF_S)


@pytest.mark.asyncio
async def test_a_redis_outage_shorter_than_the_final_backoff_loses_no_line(monkeypatch):
    # F-29 review: a ~1.5 s outage used to exhaust the final retries; a slept second stands in for each wait
    clock = {"now": 0.0}

    async def fake_sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr("services.docker_service.LOG_STREAM_INTERVAL", 0)
    monkeypatch.setattr("services.docker_service.asyncio.sleep", fake_sleep)
    published: list[dict] = []

    class _RedisDownForTwentySeconds:
        async def publish(self, channel, payload):
            if clock["now"] < 20:
                raise ConnectionError("redis is restarting")
            published.append(payload)

    svc = _service(_RedisDownForTwentySeconds())
    await svc.stream_log("renter build output", "success", "build")
    await svc.finish_stream_logs()

    await svc.handle_stream_logs(miner_hotkey="m", executor_id="e", pod_id="p")

    assert [log["log_text"] for p in published for log in p["logs"]] == ["renter build output"]
    assert svc.logs_queue == []
