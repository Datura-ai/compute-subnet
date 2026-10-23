"""DAH-4658: a scrape that finds no GPU while the renter resets them (or just after) is held, not a 0.

8x H200, 23 Oct: `nvidia-smi -r` on all 8 cards ran ~45 s, a cycle's scrape listed 0 GPUs and failed with
SCRAPE_FAILED_NO_GPU, and the node scored 0. The gpu_reset mark was never read: the scrape is fatal and ends the
pipeline before the checks that skip on it. The mark was also gone by the time a scrape that ended just after the
reset was judged.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from payload_models.payloads import ResetPodGpuRequest
from services.docker_service import DockerService
from services.executor_rollout import ExecutorRolloutTracker
from services.gpu_reset import RECENT_RENTER_GPU_RESET_SECONDS, GpuResetReport, renter_gpu_reset_recent
from services.redis_service import GPU_RESET_PENDING_KIND
from services.ssh_service import SSHService
from test_executor_rollout_grace import _failed, _scored, _validator_process
from test_rented_host_gpu_fault_reset import (
    ENCRYPT_KEY,
    EXECUTOR,
    NO_GPU_REPORT,
    POD_ID,
    RENTAL_OWNER,
    DummyBackendClient,
    DummyScoreCalculator,
    DummySSHClient,
    MockContainerCleanup,
    _driver_report,
    _redis_service,
    _resets,
    _Runner,
    _scrape_result,
    _Sink,
    build_rented_data,
)

from helpers import FakeRedis, build_context_config, build_services, build_state
from neurons.validators.src.services.task.checks import machine_spec_scrape
from neurons.validators.src.services.task.checks.gpu_fingerprint import GpuFingerprintCheck
from neurons.validators.src.services.task.checks.machine_spec_scrape import MachineSpecScrapeCheck
from neurons.validators.src.services.task.checks.rented_machine import TenantEnforcementCheck
from neurons.validators.src.services.task.messages import MachineSpecMessages as Msg
from neurons.validators.src.services.task.pipeline import Pipeline
from neurons.validators.src.services.task.result_handler import ResultHandler

ZERO_GPUS = _scrape_result(stdout=NO_GPU_REPORT, exit_code=1)
RESET_REQUIRED = _scrape_result(stdout=_driver_report("NVMLError(16)"), exit_code=1)


async def _cycle(context_factory, service, *, scrape):
    """One rented cycle through the real Pipeline and ResultHandler, with the node's Redis marks in `service`."""
    backend = DummyBackendClient(active=True)
    backend.report_rented_gpu_drop = AsyncMock()
    ctx = context_factory(
        miner_hotkey=RENTAL_OWNER,
        services=build_services(
            ssh=SSHService(),
            redis=service,
            score_calculator=DummyScoreCalculator(actual_score=1.0, job_score=1.0, warning=""),
            container_cleanup=MockContainerCleanup(),
            backend=backend,
        ),
        config=build_context_config(machine_scrape_filename="scrape.sh", obfuscation_keys={}),
        state=build_state(
            remote_dir="/remote",
            rented_data=build_rented_data(
                EXECUTOR, {"containers": [{"name": "tenant-123", "pod_id": POD_ID}], "owner_flag": False}
            ),
        ),
        runner=_Runner(scrape),
        ssh=DummySSHClient(pod_running=True),
        encrypt_key=ENCRYPT_KEY,
        verified=await service.get_verified_job_info(RENTAL_OWNER, EXECUTOR),
        collateral_deposited=True,
        is_rental_succeed=True,
        contract_version="v1.0.0",
    )
    with patch.object(machine_spec_scrape.settings, "RENTED_HOST_GPU_FAULT_RESET_ENABLED", True):
        ok, events, ctx = await Pipeline(
            [MachineSpecScrapeCheck(), GpuFingerprintCheck(), TenantEnforcementCheck()], _Sink()
        ).run(ctx)
    await ResultHandler(redis_service=service, dry_run=False)._persist_verification_data(
        miner_hotkey=RENTAL_OWNER, executor_id=EXECUTOR, verified_job_info=ctx.verified, context=ctx, success=ok
    )
    return ok, events[-1], ctx, backend


async def _reset_in_progress(service) -> None:
    await service.add_pending_pod(RENTAL_OWNER, EXECUTOR, POD_ID, kind=GPU_RESET_PENDING_KIND)


async def _reset_just_ended(service) -> None:
    await service.mark_gpu_reset_recent(RENTAL_OWNER, EXECUTOR, POD_ID, RECENT_RENTER_GPU_RESET_SECONDS)


@pytest.mark.parametrize("scrape", [ZERO_GPUS, RESET_REQUIRED], ids=["zero-gpus", "reset-required"])
@pytest.mark.parametrize("mark", [_reset_in_progress, _reset_just_ended], ids=["in-progress", "just-ended"])
@pytest.mark.asyncio
async def test_a_scrape_without_gpus_during_a_renter_reset_is_held(context_factory, mark, scrape):
    service = _redis_service()
    await mark(service)

    ok, event, ctx, backend = await _cycle(context_factory, service, scrape=scrape)

    assert ok is False
    assert event.check_id == MachineSpecScrapeCheck.check_id
    assert event.reason_code == Msg.SCRAPE_GPU_RESET_IN_PROGRESS.reason
    assert ctx.clear_verified_job_info is False
    assert _resets(service) == []
    backend.report_rented_gpu_drop.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_scrape_without_gpus_and_no_renter_reset_still_fails_as_before(context_factory):
    service = _redis_service()

    ok, event, ctx, _ = await _cycle(context_factory, service, scrape=ZERO_GPUS)

    assert ok is False
    assert event.reason_code == Msg.SCRAPE_FAILED_NO_GPU.reason
    assert ctx.clear_verified_job_info is True
    assert len(_resets(service)) == 1


@pytest.mark.asyncio
async def test_the_recent_reset_mark_expires_after_the_window():
    service = _redis_service()
    await _reset_just_ended(service)

    ttl = await service.redis.ttl(f"gpu_reset_recent:{RENTAL_OWNER}:{EXECUTOR}")

    assert 0 < ttl <= RECENT_RENTER_GPU_RESET_SECONDS
    assert await renter_gpu_reset_recent(service, RENTAL_OWNER, EXECUTOR) is True
    assert await renter_gpu_reset_recent(service, RENTAL_OWNER, EXECUTOR, POD_ID) is True
    assert await renter_gpu_reset_recent(service, RENTAL_OWNER, EXECUTOR, "pod-other") is False
    assert await renter_gpu_reset_recent(service, RENTAL_OWNER, "other-executor") is False


@pytest.mark.asyncio
async def test_an_unreadable_recent_reset_mark_is_no_mark():
    assert await renter_gpu_reset_recent(FakeRedis(failing=True), "hk", "ex") is False
    assert await renter_gpu_reset_recent(AsyncMock(), "hk", "ex") is False  # not a real pod id


@pytest.mark.asyncio
async def test_the_held_result_gets_no_verdict_this_cycle():
    validator_process = _validator_process(ExecutorRolloutTracker(redis_service=MagicMock(), grace_cycles=0))
    validator_process.fetch_executor_digest_or_none = AsyncMock(return_value=None)
    validator_process.observe_executor_rollout = AsyncMock(return_value=MagicMock(covers=lambda _: False))
    held = _failed("node-1", Msg.SCRAPE_GPU_RESET_IN_PROGRESS.reason)
    failed = _failed("node-2", Msg.SCRAPE_FAILED_NO_GPU.reason)
    healthy = _scored("node-3")

    kept, withheld = await validator_process.withhold_verdicts_for_rollout(
        {"5Miner": [held, failed, healthy]}, 7500, "batch-1", MagicMock()
    )

    assert kept == {"5Miner": [failed, healthy]}
    assert [(w.miner_hotkey, w.result.executor_info.uuid) for w in withheld] == [("5Miner", "node-1")]


def _reset_service(service) -> DockerService:
    docker_service = DockerService.__new__(DockerService)
    docker_service.redis_service = service
    docker_service.ssh_service = MagicMock(decrypt_payload=MagicMock(return_value="key"))
    docker_service._prepare_known_hosts_policy = AsyncMock(return_value=None)
    return docker_service


async def _run_reset(service, **connect):
    payload = ResetPodGpuRequest(
        miner_hotkey=RENTAL_OWNER, executor_id=EXECUTOR, pod_id=POD_ID, container_name="c", gpu_uuids=["GPU-1"], request_id="r1"
    )
    executor_info = MagicMock(address="203.0.113.5", ssh_port=22, tdx_quote=None)
    with patch("services.docker_service.asyncssh.import_private_key"), \
            patch("services.docker_service.require_rental_docker_ssh_host_key"), \
            patch("services.docker_service.public_host_sock", AsyncMock(return_value={})), \
            patch("services.docker_service.asyncssh.connect", **connect):
        return await _reset_service(service).reset_pod_gpus(payload, executor_info, MagicMock(), "encrypted")


@pytest.mark.asyncio
async def test_the_reset_leaves_a_recent_mark_when_its_pending_mark_goes():
    service = _redis_service()
    connection = MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False))
    stopped = GpuResetReport(ok=True, pod_stopped=True, container_restarted=True)

    with patch("services.docker_service.reset_pod_gpus", AsyncMock(return_value=stopped)):
        result = await _run_reset(service, return_value=connection)

    assert result.ok is True
    assert await service.gpu_reset_marks(RENTAL_OWNER, EXECUTOR) == []
    assert await renter_gpu_reset_recent(service, RENTAL_OWNER, EXECUTOR, POD_ID) is True


@pytest.mark.asyncio
async def test_a_reset_that_never_stopped_the_pod_leaves_no_recent_mark():
    service = _redis_service()

    result = await _run_reset(service, side_effect=OSError("down"))

    assert result.ok is False
    assert await renter_gpu_reset_recent(service, RENTAL_OWNER, EXECUTOR, POD_ID) is False


@pytest.mark.asyncio
async def test_no_verified_job_reset_is_sent_just_after_a_renter_reset():
    import asyncio
    import json

    from clients.compute_client import ComputeClient
    from services.redis_service import RESET_VERIFIED_JOB_CHANNEL

    def message(executor_uuid: str, pod_id: str | None = None) -> dict:
        data = {"miner_hotkey": RENTAL_OWNER, "executor_uuid": executor_uuid, "reason": 0,
                "check_id": "gpu.validate.fingerprint", "reason_code": "GPU_MISSING", "evidence": {"pod_id": pod_id}}
        return {"channel": RESET_VERIFIED_JOB_CHANNEL.encode(), "data": json.dumps(data)}

    class PubSub:
        async def listen(self):
            yield message(EXECUTOR, POD_ID)
            yield message(EXECUTOR, "pod-other")  # another pod on the node: still sent
            yield message("idle")
            raise asyncio.CancelledError  # ends the subscriber's reconnect loop

        async def aclose(self):
            pass

    service = _redis_service()
    await _reset_just_ended(service)
    service.subscribe = AsyncMock(return_value=PubSub())
    client = ComputeClient.__new__(ComputeClient)
    client.lock = asyncio.Lock()
    client.message_queue = []
    client.logging_extra = {}
    client.miner_service = MagicMock(redis_service=service)
    client.my_hotkey = MagicMock(return_value="validator-hotkey")

    with pytest.raises(asyncio.CancelledError):
        await client.subscribe_mesages_from_redis()

    assert [request.executor_uuid for request in client.message_queue] == [EXECUTOR, "idle"]
