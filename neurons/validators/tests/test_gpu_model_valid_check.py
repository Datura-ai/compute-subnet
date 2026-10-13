from unittest.mock import patch

import pytest
from neurons.validators.src.services.const import GPU_MODEL_RATES
from neurons.validators.src.services.task.availability import availability_errors
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
            {"NVIDIA RTX 3090": 0.05},
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


MIXED_RATES = {"NVIDIA GeForce RTX 4090": 1.0, "NVIDIA GeForce RTX 5060 Ti": 0.2}
# OD-D850: listed as "2X RTX 4090"; the renter's nvidia-smi showed a 4090 (24564MiB) and a 5060 Ti (16311MiB)
MIXED_DETAILS = [
    {"name": "NVIDIA GeForce RTX 4090", "uuid": "GPU-0", "capacity": 24564},
    {"name": "NVIDIA GeForce RTX 5060 Ti", "uuid": "GPU-1", "capacity": 16311},
]


def _mixed_ctx(context_factory, details=MIXED_DETAILS):
    state = build_state(specs={"gpu": {"count": len(details), "details": details}}, gpu_count=len(details), gpu_details=details)
    return context_factory(services=build_services(), config=build_context_config(gpu_model_rates=MIXED_RATES), state=state)


@pytest.mark.asyncio
@pytest.mark.parametrize("enforce", [False, True])
async def test_a_node_with_two_gpu_models_is_allowed_and_refused_only_under_the_interim_flag(enforce, context_factory):
    with patch("neurons.validators.src.services.task.checks.gpu_model_valid.settings") as s:
        s.MIXED_GPU_MODEL_ENFORCEMENT_ENABLED = enforce
        result = await GpuModelValidCheck().run(_mixed_ctx(context_factory))

    seen = {
        "listed_as": "NVIDIA GeForce RTX 4090",
        "gpu_count": 2,
        "models": {"NVIDIA GeForce RTX 4090": 1, "NVIDIA GeForce RTX 5060 Ti": 1},
    }
    assert result.passed is (not enforce)
    assert result.event.reason_code == Msg.MODEL_MIXED.reason
    # its verification is left alone either way: a reset would stop a live renter's billing
    assert result.updates.get("clear_verified_job_info", False) is False
    hidden = availability_errors([result.event])
    if not enforce:
        assert result.event.what_we_saw == seen
        assert result.event.severity == "info"
        assert hidden == []  # allowed: still listed
        return
    # enforced: one availability entry, which the backend reads as "hide from the market, refuse new rentals"
    assert [error.model_dump() for error in hidden] == [
        {
            "reason_code": Msg.MODEL_MIXED.reason,
            "reach_source": "validator",
            "reach_target": "gpu_inventory",
            "message": Msg.MODEL_MIXED_REFUSED.event,
            "remediation": Msg.MODEL_MIXED_REFUSED.remediation,
            "what_we_saw": seen,
        }
    ]


@pytest.mark.asyncio
async def test_a_mixed_node_is_refused_whichever_card_comes_first(context_factory):
    with patch("neurons.validators.src.services.task.checks.gpu_model_valid.settings") as s:
        s.MIXED_GPU_MODEL_ENFORCEMENT_ENABLED = True
        result = await GpuModelValidCheck().run(_mixed_ctx(context_factory, list(reversed(MIXED_DETAILS))))

    assert result.passed is False
    assert result.event.reason_code == Msg.MODEL_MIXED.reason
    assert result.event.what_we_saw["listed_as"] == "NVIDIA GeForce RTX 5060 Ti"


@pytest.mark.asyncio
async def test_same_model_cards_pass_under_enforcement(context_factory):
    details = [dict(MIXED_DETAILS[0], uuid=f"GPU-{i}") for i in range(4)]
    with patch("neurons.validators.src.services.task.checks.gpu_model_valid.settings") as s:
        s.MIXED_GPU_MODEL_ENFORCEMENT_ENABLED = True
        result = await GpuModelValidCheck().run(_mixed_ctx(context_factory, details))

    assert result.passed is True
    assert result.event.reason_code == Msg.MODEL_OK.reason


# The prod node of OD-D850 (a88a338a), its GPU details as the scrape stored them on 13 Oct.
PROD_MIXED_NODE_DETAILS = [
    {
        "cuda": "8.9", "name": "NVIDIA GeForce RTX 4090", "pcie": 8, "uuid": "GPU-7bcb1eea-6bac-643b-93a5-2d12f8519f4f",
        "capacity": 24564, "pcie_speed": 5000, "power_limit": 450, "memory_speed": 810, "graphics_speed": 2505,
        "memory_used_mb": 484.0, "gpu_utilization": 0.0, "power_max_limit": 600, "power_min_limit": 150,
        "memory_utilization": 0.0, "power_default_limit": 450,
    },
    {
        "cuda": "12.0", "name": "NVIDIA GeForce RTX 5060 Ti", "pcie": 8, "uuid": "GPU-297178cb-0afa-854b-f4f2-f4470f871b36",
        "capacity": 16311, "pcie_speed": 2500, "power_limit": 180, "memory_speed": 405, "graphics_speed": 180,
        "memory_used_mb": 464.0625, "gpu_utilization": 0.0, "power_max_limit": 180, "power_min_limit": 150,
        "memory_utilization": 0.0, "power_default_limit": 180,
    },
]


@pytest.mark.asyncio
async def test_the_prod_mixed_node_is_reported_by_its_real_cards_and_stays_listed(context_factory):
    specs = {"gpu": {"count": 2, "driver": "580.126.20", "cuda_driver": 13000, "details": PROD_MIXED_NODE_DETAILS}}
    state = build_state(specs=specs, gpu_count=2, gpu_details=PROD_MIXED_NODE_DETAILS)
    ctx = context_factory(
        services=build_services(), config=build_context_config(gpu_model_rates=GPU_MODEL_RATES), state=state
    )

    with patch("neurons.validators.src.services.task.checks.gpu_model_valid.settings") as s:
        s.MIXED_GPU_MODEL_ENFORCEMENT_ENABLED = False
        result = await GpuModelValidCheck().run(ctx)

    assert result.passed is True
    assert result.event.reason_code == Msg.MODEL_MIXED.reason
    assert result.event.what_we_saw["models"] == {"NVIDIA GeForce RTX 4090": 1, "NVIDIA GeForce RTX 5060 Ti": 1}
    assert availability_errors([result.event]) == []


@pytest.mark.asyncio
async def test_a_mixed_node_with_an_unsupported_second_card_is_refused(context_factory):
    details = [PROD_MIXED_NODE_DETAILS[0], dict(PROD_MIXED_NODE_DETAILS[1], name="NVIDIA MADE-UP 9000")]
    state = build_state(specs={"gpu": {"count": 2, "details": details}}, gpu_count=2, gpu_details=details)
    ctx = context_factory(
        services=build_services(), config=build_context_config(gpu_model_rates=GPU_MODEL_RATES), state=state
    )

    result = await GpuModelValidCheck().run(ctx)

    assert result.passed is False
    assert result.event.reason_code == Msg.MODEL_UNSUPPORTED.reason
    assert result.event.what_we_saw["gpu_model"] == "NVIDIA MADE-UP 9000"
