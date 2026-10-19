"""RENTED_GPU_DROP: a rented node that lost a GPU is reported the cycle it is seen, once per incident."""

import json

import pytest
from neurons.validators.src.services.task.checks import (
    MachineSpecScrapeCheck,
)
from neurons.validators.src.services.task.checks.rented_gpu_drop import (
    FAULT_ANCHORED_MISSING,
    FAULT_BELOW_RENTED,
    FAULT_DETAILS_SHORT,
    FAULT_NVML_ERROR,
)
from protocol.vc_protocol.compute_requests import (
    RentedExecutor,
    RentedExecutorsResponse,
    RentedGpuDropResponse,
    RentedPod,
)

from tests.helpers import (
    FakeRedis,
    build_context_config,
    build_services,
    build_state,
    default_executor,
)
from tests.test_machine_spec_scrape_check import DummySSHCommandRunner, make_command_result
UUIDS = [f"GPU-a0a{i}-0000-0000-0000-00000000000{i}" for i in range(8)]
POD_ID = "00000000-0000-4000-8000-000000000001"
NOTIFIED = RentedGpuDropResponse(recorded=True, delivery="notified")


def _rented(
    executor_uuid: str,
    *,
    status: str | None = "RUNNING",
    gpu_count: int | None = 8,
    pods: list[RentedPod] | None = None,
    owner: str = "miner-hotkey",
) -> RentedExecutorsResponse:
    return RentedExecutorsResponse(
        executors={
            executor_uuid: RentedExecutor(
                miner_hotkey=owner,
                executor_ip_address="127.0.0.1",
                executor_ip_port="8001",
                pods=pods
                or [
                    RentedPod(
                        pod_id=POD_ID,
                        container_name="container_pod1",
                        gpu_count=gpu_count,
                        status=status,
                    )
                ],
            )
        }
    )


def _services(answer=NOTIFIED, redis=None):
    services = build_services(redis=redis or FakeRedis())
    services.backend.report_rented_gpu_drop.return_value = answer
    return services


BR, DS, AM, NV = FAULT_BELOW_RENTED, FAULT_DETAILS_SHORT, FAULT_ANCHORED_MISSING, FAULT_NVML_ERROR


async def _scrape_listing_no_gpu(
    context_factory, services, *, count, scrape_error, anchor=UUIDS, **rented_kwargs
):
    data: dict = {"data_gpu": {"gpu_count": count, "gpu_details": []}}
    if scrape_error:
        data["gpu_scrape_error"] = scrape_error
    report = json.dumps({"error": "no_gpu_details", "data": data})
    executor = default_executor()
    ctx = context_factory(
        services=services,
        config=build_context_config(
            machine_scrape_filename="scrape.sh", machine_scrape_timeout=300, obfuscation_keys={}
        ),
        state=build_state(
            remote_dir="/remote/path", rented_data=_rented(executor.uuid, **rented_kwargs)
        ),
        runner=DummySSHCommandRunner(
            result=make_command_result(success=False, exit_code=1, stdout=report)
        ),
        executor=executor,
        verified={"uuids": ",".join(anchor)},
        encrypt_key="test-encrypt-key",
    )
    return await MachineSpecScrapeCheck().run(ctx)


@pytest.mark.asyncio
async def test_a_scrape_that_lists_no_gpu_on_an_idle_node_posts_nothing(context_factory):
    services = _services()

    result = await _scrape_listing_no_gpu(
        context_factory, services, count=8, scrape_error="NVMLError(999)", status="STOPPED"
    )

    assert result.passed is False
    services.backend.report_rented_gpu_drop.assert_not_awaited()
    # only the GPU loss hold (DAH-4457) is written; no rented-GPU-drop mark
    assert [key.split(":")[0] for key in services.redis.store] == ["gpu_loss_hold"]
