"""DAH-3006: the process-wide Redis command lock.

In a ~500-executor wave every pipeline queues on `RedisService.lock` for its one-command calls,
and the FIFO queue drains at ~0.4 s per command: the duplicate check's single SISMEMBER measured
p50 73 s (Loki, 6 Sep 12:03 wave). The lock stays: commands run one at a time.
"""

import asyncio
import time

import pytest

from services import redis_service as redis_service_module
from services.redis_service import RedisService

CALLS = 20
COMMAND_SECONDS = 0.05


class _SlowFakeRedis:
    """A Redis whose every command takes COMMAND_SECONDS, so serialisation is measurable."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def _cmd(self, *args):
        self.calls.append(args)
        await asyncio.sleep(COMMAND_SECONDS)
        return True

    sismember = sadd = hset = hget = _cmd


def _service() -> RedisService:
    # The constructor only builds pools from settings; nothing connects until a command runs.
    service = RedisService()
    service.redis = _SlowFakeRedis()
    return service


async def _timed(coro_factory, n: int = CALLS) -> tuple[float, list]:
    started = time.perf_counter()
    results = await asyncio.gather(*(coro_factory(i) for i in range(n)))
    return time.perf_counter() - started, results


@pytest.mark.asyncio
async def test_the_lock_serialises_commands():
    service = _service()

    assert isinstance(service.lock, asyncio.Lock)
    elapsed, results = await _timed(lambda i: service.is_elem_exists_in_set("set", f"e{i}"))

    # Today's behaviour: one command at a time, N x command time.
    assert elapsed >= CALLS * COMMAND_SECONDS * 0.9
    assert results == [True] * CALLS


@pytest.mark.asyncio
async def test_writes_still_reach_the_client():
    service = _service()

    await service.sadd("set", "elem")
    await service.hset("hash", "field", "value")
    await service.set_verified_job_info("miner", "executor", prev_info={}, success=True)

    commands = [call[0] for call in service.redis.calls]
    assert commands == ["set", "hash", redis_service_module.VERIFIED_JOB_COUNT_KEY]
