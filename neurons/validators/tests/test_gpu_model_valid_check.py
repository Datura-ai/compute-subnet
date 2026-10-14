import pytest

from neurons.validators.src.services.task.checks.gpu_model_valid import GpuModelValidCheck
from neurons.validators.src.services.task.messages import GpuModelMessages as Msg
from protocol.vc_protocol.compute_requests import RentedExecutor, RentedExecutorsResponse, RentedPod

from tests.helpers import build_context_config, build_services, build_state, default_executor


@pytest.mark.parametrize(
    "gpu_model_rates,state_kwargs,expected_pass,expected_reason",
    [
        (None, {"specs": {}, "gpu_count": 1, "gpu_details": [{"name": "NVIDIA RTX 3090"}]}, False, Msg.POLICY_MISSING.reason),
        (
            {"NVIDIA RTX 3090": 0.05},
            {"specs": {"gpu": {"count": 1, "details": [{"name": "Unsupported"}]}}, "gpu_count": 1, "gpu_details": [{"name": "Unsupported"}]},
            False,
            Msg.MODEL_UNSUPPORTED.reason,
        ),
        (
            {None: 1.0, "NVIDIA RTX 3090": 0.05},
            {"specs": {"gpu": {"count": 0, "details": []}}, "gpu_count": 0, "gpu_details": []},
            False,
            Msg.COUNT_ZERO.reason,
        ),
        (
            {"NVIDIA RTX 3090": 0.05},
            {"specs": {"gpu": {"count": 2, "details": [{"name": "NVIDIA RTX 3090"}]}}, "gpu_count": 2, "gpu_details": [{"name": "NVIDIA RTX 3090"}]},
            False,
            Msg.DETAILS_MISMATCH.reason,
        ),
        (
            {"NVIDIA RTX 3090": 0.05},
            {"specs": {"gpu": {"count": 1, "details": [{"name": "NVIDIA RTX 3090"}]}}, "gpu_count": 1, "gpu_details": [{"name": "NVIDIA RTX 3090"}]},
            True,
            Msg.MODEL_OK.reason,
        ),
        (
            {"NVIDIA GeForce RTX 3080": 0.0},
            {
                "specs": {"gpu": {"count": 1, "details": [{"name": "NVIDIA GeForce RTX 3080"}]}},
                "gpu_count": 1,
                "gpu_details": [{"name": "NVIDIA GeForce RTX 3080"}],
            },
            True,
            Msg.MODEL_OK.reason,
        ),
    ],
)
@pytest.mark.asyncio
async def test_gpu_model_valid_check(gpu_model_rates, state_kwargs, expected_pass, expected_reason, context_factory):
    services = build_services()
    config = build_context_config(gpu_model_rates=gpu_model_rates)
    state = build_state(**state_kwargs)

    ctx = context_factory(services=services, config=config, state=state)

    result = await GpuModelValidCheck().run(ctx)

    assert result.passed is expected_pass
    assert result.event.reason_code == expected_reason
    # only the short detail list clears the verified job (DAH-3519); the other refusals leave it alone
    assert result.updates.get("clear_verified_job_info", False) is (expected_reason == Msg.DETAILS_MISMATCH.reason)


@pytest.mark.asyncio
async def test_a_rented_node_that_lists_fewer_gpus_than_it_reports_loses_its_verification(context_factory):
    """DAH-3519 / F-1361: a rented 8-GPU node enumerated 1 card from 07:58Z on; every cycle was GPU_DETAILS_MISMATCH
    at score 0 and nothing else happened, because this fatal check halts the pipeline before SpecChangeCheck. On
    origin/main the result carries no ``clear_verified_job_info`` and the node stays verified and listed.

    The rental, the recorded spec and ``gpu_model_count`` are F-1361's scenery, not inputs: the check reads only the
    count and the detail list, so the same scrape on an idle node clears the verified job too."""
    executor = default_executor()
    model = "NVIDIA GeForce RTX 5090"
    rented = RentedExecutorsResponse(
        executors={
            executor.uuid: RentedExecutor(
                miner_hotkey="miner-hotkey",
                executor_ip_address=executor.address,
                executor_ip_port=str(executor.port),
                pods=[RentedPod(pod_id="p1", container_name="pod_p1")],
            )
        }
    )
    state = build_state(
        gpu_count=8,
        gpu_details=[{"name": model, "uuid": "GPU-0", "capacity": 32768}],
        gpu_model_count=f"{model}:8",
        rented_data=rented,
    )
    ctx = context_factory(
        services=build_services(),
        config=build_context_config(gpu_model_rates={model: 1.0}),
        state=state,
        executor=executor,
        verified={"spec": f"{model}:8", "uuids": ",".join(f"GPU-{i}" for i in range(8))},
    )

    result = await GpuModelValidCheck().run(ctx)

    assert result.passed is False
    assert result.event.reason_code == Msg.DETAILS_MISMATCH.reason
    assert result.event.what_we_saw == {"gpu_count": 8, "details_len": 1}
    assert result.updates == {"clear_verified_job_info": True}


async def _run_gpu_model_check_on_cards(context_factory, names, rates):
    details = [{"name": name, "uuid": f"GPU-{i}"} for i, name in enumerate(names)]
    state = build_state(gpu_count=len(details), gpu_details=details)
    ctx = context_factory(
        services=build_services(), config=build_context_config(gpu_model_rates=rates), state=state
    )
    return await GpuModelValidCheck().run(ctx)


@pytest.mark.asyncio
async def test_a_node_with_two_supported_gpu_models_fails_as_mixed(context_factory):
    """DAH-4304: the owner ruled one executor may not carry more than one GPU model."""
    rates = {"NVIDIA GeForce RTX 4090": 0.1, "NVIDIA GeForce RTX 5060 Ti": 0.1}
    names = ["NVIDIA GeForce RTX 4090", "NVIDIA GeForce RTX 5060 Ti"]

    result = await _run_gpu_model_check_on_cards(context_factory, names, rates)

    assert result.passed is False
    assert result.event.reason_code == Msg.MODEL_MIXED.reason
    assert result.event.what_we_saw["gpu_models"] == {names[0]: 1, names[1]: 1}
    assert "same model" in result.event.remediation
    assert all(name in result.event.remediation for name in names)
    assert result.updates.get("clear_verified_job_info", False) is False


@pytest.mark.asyncio
async def test_a_node_whose_gpus_are_all_one_model_passes(context_factory):
    model = "NVIDIA H100 80GB HBM3"

    result = await _run_gpu_model_check_on_cards(context_factory, [model] * 4, {model: 1.0})

    assert result.passed is True
    assert result.event.reason_code == Msg.MODEL_OK.reason


@pytest.mark.asyncio
async def test_a_second_card_of_an_unsupported_model_fails(context_factory):
    model = "NVIDIA H100 80GB HBM3"

    result = await _run_gpu_model_check_on_cards(
        context_factory, [model, "Unsupported"], {model: 1.0}
    )

    assert result.passed is False
    assert result.event.reason_code == Msg.MODEL_UNSUPPORTED.reason
    assert result.event.what_we_saw["gpu_model"] == "Unsupported"
