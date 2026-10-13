"""The verified-job record is keyed by (miner hotkey, executor uuid).

Through the real RedisService, Pipeline and ResultHandler on a fakeredis server.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fakeredis import FakeServer
from fakeredis.aioredis import FakeRedis

from neurons.validators.src.services.task.checks.gpu_fingerprint import GpuFingerprintCheck
from neurons.validators.src.services.task.checks.spec_change import SpecChangeCheck
from neurons.validators.src.services.task.pipeline import Pipeline
from neurons.validators.src.services.task.result_handler import ResultHandler
from services.redis_service import VERIFIED_JOB_COUNT_KEY, RedisService, verified_job_field

from tests.helpers import build_state

EXECUTOR = "22222222-2222-2222-2222-222222222222"
HOTKEY_A = "hotkey-a"
HOTKEY_B = "hotkey-b"
SETTINGS = "neurons.validators.src.services.task.checks.gpu_fingerprint.settings"


class _Sink:
    async def emit(self, event) -> None:
        return None


def _redis_service() -> RedisService:
    service = RedisService.__new__(RedisService)
    service.redis = FakeRedis(server=FakeServer())
    service.lock = asyncio.Lock()
    service.publish = AsyncMock()
    return service


async def _field(service: RedisService, field: str) -> dict | None:
    data = await service.redis.hget(VERIFIED_JOB_COUNT_KEY, field)
    return json.loads(data) if data else None


async def _cycle(service, context_factory, *, hotkey: str, uuids: str) -> bool:
    handler = ResultHandler(redis_service=service, dry_run=False)
    verified = await service.get_verified_job_info(EXECUTOR, hotkey)
    ctx = context_factory(
        state=build_state(gpu_uuids=uuids, gpu_model_count=f"A100:{len(uuids.split(','))}"), verified=verified
    )
    with patch(SETTINGS) as s:
        s.GPU_ANCHOR_HARD_ENABLED = False
        ok, _, ctx = await Pipeline([GpuFingerprintCheck(), SpecChangeCheck()], _Sink()).run(ctx)
    await handler._persist_verification_data(
        miner_hotkey=hotkey, executor_id=EXECUTOR, verified_job_info=verified, context=ctx, success=ok
    )
    return ok


@pytest.mark.asyncio
async def test_verification_state_is_keyed_by_hotkey_and_uuid(context_factory):
    service = _redis_service()
    for _ in range(3):
        assert await _cycle(service, context_factory, hotkey=HOTKEY_A, uuids="gpu-001") is True
    before = await _field(service, verified_job_field(HOTKEY_A, EXECUTOR))
    assert before["count"] == 3

    # another hotkey reporting the same executor id with different GPUs is not accepted and writes nothing
    assert await _cycle(service, context_factory, hotkey=HOTKEY_B, uuids="gpu-009") is False
    assert await _field(service, verified_job_field(HOTKEY_B, EXECUTOR)) is None
    await service.clear_verified_job_info(HOTKEY_B, EXECUTOR, prev_info=await service.get_verified_job_info(EXECUTOR, HOTKEY_B), anchor_broken=True)
    assert await _field(service, EXECUTOR) is None

    assert await _field(service, verified_job_field(HOTKEY_A, EXECUTOR)) == before
    assert (await service.get_verified_job_info(EXECUTOR, HOTKEY_A))["count"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("legacy_uuids", "cycle_uuids", "expected"),
    [
        ("gpu-001", "gpu-001", (41, 2, "gpu-001")),
        ("gpu-001,gpu-002", "gpu-002,gpu-001", (41, 2, "gpu-001,gpu-002")),
        ("", "gpu-001", (1, 0, "gpu-001")),
    ],
)
async def test_first_passing_cycle_migrates_the_uuid_only_record(context_factory, legacy_uuids, cycle_uuids, expected):
    service = _redis_service()
    spec = f"A100:{len(cycle_uuids.split(','))}"
    legacy = {"count": 40, "failed": 2, "spec": spec, "uuids": legacy_uuids}
    await service.redis.hset(VERIFIED_JOB_COUNT_KEY, EXECUTOR, json.dumps(legacy))

    ok = await _cycle(service, context_factory, hotkey=HOTKEY_A, uuids=cycle_uuids)

    assert ok is True
    record = await _field(service, verified_job_field(HOTKEY_A, EXECUTOR))
    assert (record["count"], record["failed"], record["uuids"]) == expected
    assert await service.redis.hget(VERIFIED_JOB_COUNT_KEY, EXECUTOR) is None
    assert "count" not in await service.get_verified_job_info(EXECUTOR, HOTKEY_B)


@pytest.mark.asyncio
@pytest.mark.parametrize("anchor", ["gpu-001", ""])
async def test_failing_cycle_leaves_legacy_and_other_records_unchanged(context_factory, anchor):
    service = _redis_service()
    legacy = json.dumps({"count": 9, "failed": 0, "spec": "A100:1", "uuids": anchor})
    other = json.dumps({"count": 4, "failed": 0, "spec": "A100:1", "uuids": "gpu-001"})
    await service.redis.hset(VERIFIED_JOB_COUNT_KEY, EXECUTOR, legacy)
    await service.redis.hset(VERIFIED_JOB_COUNT_KEY, verified_job_field(HOTKEY_A, EXECUTOR), other)

    verified = await service.get_verified_job_info(EXECUTOR, HOTKEY_B)
    await service.set_verified_job_info(HOTKEY_B, EXECUTOR, prev_info=verified, success=False)
    await service.clear_verified_job_info(HOTKEY_B, EXECUTOR, prev_info=verified, anchor_broken=True)

    # an anchored legacy record only gains the broken mark (as on main); nothing is imported into B's record
    expected = json.loads(legacy) | ({"anchor_broken": True} if anchor else {})
    assert await _field(service, EXECUTOR) == expected
    if anchor:
        assert await _field(service, verified_job_field(HOTKEY_B, EXECUTOR)) is None
    assert await _field(service, verified_job_field(HOTKEY_A, EXECUTOR)) == json.loads(other)


@pytest.mark.asyncio
@pytest.mark.parametrize("a_first", [False, True])
@pytest.mark.parametrize(
    "preseed",
    [
        None,
        {"count": 0, "failed": 1, "spec": "", "uuids": ""},
        {"count": 0, "failed": 0, "spec": "", "uuids": ""},
    ],
    ids=["absent", "failed", "reset"],
)
async def test_failed_cycle_does_not_import_another_hotkeys_legacy_identity(context_factory, a_first, preseed):
    service = _redis_service()
    legacy = {"count": 40, "failed": 0, "spec": "A100:1", "uuids": "gpu-001"}
    await service.redis.hset(VERIFIED_JOB_COUNT_KEY, EXECUTOR, json.dumps(legacy))
    if preseed:
        await service.redis.hset(VERIFIED_JOB_COUNT_KEY, verified_job_field(HOTKEY_B, EXECUTOR), json.dumps(preseed))
    if a_first:
        assert await _cycle(service, context_factory, hotkey=HOTKEY_A, uuids="gpu-001")

    assert await _cycle(service, context_factory, hotkey=HOTKEY_B, uuids="gpu-009") is False
    assert await _field(service, verified_job_field(HOTKEY_B, EXECUTOR)) == preseed

    if not a_first:
        assert await _cycle(service, context_factory, hotkey=HOTKEY_A, uuids="gpu-001")
    assert (await _field(service, verified_job_field(HOTKEY_A, EXECUTOR)))["count"] == 41
    # a different host never takes the id over, whichever hotkey migrated first, even from an empty own record
    assert await _cycle(service, context_factory, hotkey=HOTKEY_B, uuids="gpu-009") is False
    assert await _field(service, verified_job_field(HOTKEY_B, EXECUTOR)) == preseed


@pytest.mark.asyncio
async def test_executor_id_with_colon_cannot_address_another_hotkeys_record(context_factory):
    service = _redis_service()
    record = {"count": 5, "failed": 0, "spec": "A100:1", "uuids": "gpu-001"}
    await service.redis.hset(VERIFIED_JOB_COUNT_KEY, verified_job_field(HOTKEY_A, EXECUTOR), json.dumps(record))
    forged = f"{HOTKEY_A}:{EXECUTOR}"
    assert await service.get_verified_job_info(forged, HOTKEY_B) == {}
    await service.set_verified_job_info(HOTKEY_B, forged, prev_info={}, uuids="gpu-009")
    assert await _field(service, verified_job_field(HOTKEY_A, EXECUTOR)) == record
    service = _redis_service()
    await service.set_verified_job_info(HOTKEY_B, f"prefix:{EXECUTOR}", prev_info={}, uuids="gpu-009")
    assert await service.get_verified_job_info(EXECUTOR, "hotkey-c") == {}


@pytest.mark.asyncio
async def test_foreign_anchor_lookup_does_not_scan_the_verified_job_hash(context_factory):
    service = _redis_service()
    assert await _cycle(service, context_factory, hotkey=HOTKEY_A, uuids="gpu-001")
    service.redis.hscan_iter = MagicMock()

    for _ in range(3):
        assert await _cycle(service, context_factory, hotkey=HOTKEY_B, uuids="gpu-009") is False

    assert await service.get_verified_job_info(EXECUTOR, HOTKEY_B) == {
        "uuids": "gpu-001", "legacy_fallback": True, "foreign_fallback": True
    }
    service.redis.hscan_iter.assert_not_called()
