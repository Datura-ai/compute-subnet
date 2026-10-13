"""DAH-4219: a mixed-GPU node earns for each card at that card's own rate.

Before, incentive keyed on the first card: 1x H200 + 7 cheap cards earned as 8x H200.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from constants import TOTAL_BURN_EMISSION
from core.validator import settle_cycle_results
from incentive import base as incentive_base
from incentive.base import BaseIncentive
from incentive.default import DefaultIncentive
from protocol.vc_protocol.compute_requests import RentedPod
from services.task.checks.rented_machine import (
    _gpu_uuids_of_device_requests,
    _read_rented_gpu_uuids_of_mixed_node,
)

from core.config import settings
from tests.test_incentive_flow import _run_sync_with_jobs
from tests.test_rental_price_incentive_flow import (  # noqa: F401 - fixtures
    _make_rented_data,
    mock_price_provider,
    price_provider_holder,
    rental_price_config,
    validator_with_rental_price,
)

pytest_plugins = ["fixtures.incentive_fixtures"]

H200 = "NVIDIA H200"
RTX4090 = "NVIDIA GeForce RTX 4090"
RTX5060TI = "NVIDIA GeForce RTX 5060 Ti"
PORTION = {H200: 0.25, RTX4090: 0.15, RTX5060TI: 0.0}
MINING_ALLOCATION = 1 - TOTAL_BURN_EMISSION


def _node(create_job_result, cards: list[str], **kwargs):
    result = create_job_result(gpu_model=cards[0], gpu_count=len(cards), **kwargs)
    result.spec = {"gpu": {"count": len(cards), "details": [{"name": name} for name in cards]}}
    return result


@pytest.fixture
def per_model_portions(incentive_redis_service):
    async def get_portion(gpu_model):
        return PORTION[gpu_model]

    incentive_redis_service.get_portion_per_gpu_type = AsyncMock(side_effect=get_portion)
    return incentive_redis_service


def test_gpu_model_counts_names_every_card(create_job_result):
    node = _node(create_job_result, [H200, RTX4090, RTX4090])
    assert node.gpu_model_counts == {H200: 1, RTX4090: 2}


def test_gpu_model_counts_is_empty_when_the_spec_does_not_name_every_card(create_job_result):
    node = _node(create_job_result, [H200, RTX4090])
    node.gpu_count = 3
    assert node.gpu_model_counts == {}
    assert create_job_result(gpu_model=H200, gpu_count=2).gpu_model_counts == {}


def test_the_cycle_total_counts_each_card_under_its_own_model(create_job_result):
    results = {
        "mixed": [_node(create_job_result, [H200] + [RTX4090] * 7)],
        "plain": [_node(create_job_result, [RTX4090])],
    }
    assert settle_cycle_results(results, {}) == {H200: 1, RTX4090: 8}


@pytest.mark.asyncio
async def test_a_mixed_node_is_paid_each_card_at_its_own_rate(
    validator_with_mocks, per_model_portions, mock_settings, create_job_result, create_neuron_info
):
    """1x H200 + 7x RTX 4090 against an 8x H200 node: on main both earn the same."""
    validator = validator_with_mocks
    validator.miner_scores = {}
    miners = [
        create_neuron_info(uid=100, hotkey="burner1"),
        create_neuron_info(uid=101, hotkey="burner2"),
        create_neuron_info(uid=2, hotkey="mixed"),
        create_neuron_info(uid=3, hotkey="real_h200"),
    ]
    mixed_node = _node(create_job_result, [H200] + [RTX4090] * 7)
    jobs = {"mixed": [mixed_node], "real_h200": [_node(create_job_result, [H200] * 8)]}

    await _run_sync_with_jobs(validator, miners, jobs)

    # 9 H200 cards and 7 RTX 4090 cards in the cycle
    mixed_score = PORTION[H200] * 1 / 9 + PORTION[RTX4090] * 7 / 7
    real_score = PORTION[H200] * 8 / 9
    total = mixed_score + real_score
    assert validator.miner_scores["mixed"] == pytest.approx(MINING_ALLOCATION * mixed_score / total)
    assert validator.miner_scores["real_h200"] == pytest.approx(MINING_ALLOCATION * real_score / total)


@pytest.mark.asyncio
async def test_a_card_with_a_zero_rate_adds_nothing(per_model_portions, mock_settings, create_job_result):
    """1x RTX 4090 + 1x RTX 5060 Ti (rate 0.0) earns as one RTX 4090, not two."""
    mixed_node = _node(create_job_result, [RTX4090, RTX5060TI])
    plain_node = _node(create_job_result, [RTX4090])
    jobs = {"mixed": [mixed_node], "plain": [plain_node]}
    incentive = DefaultIncentive(mock_settings.incentive, per_model_portions, jobs, {RTX4090: 2, RTX5060TI: 1})

    await incentive.calculate_mining_scores()

    assert mixed_node.incentive == pytest.approx(plain_node.incentive)
    assert incentive.miner_incentives["mixed"] == pytest.approx(incentive.miner_incentives["plain"])


@pytest.mark.asyncio
async def test_a_mixed_node_leaves_the_cycle_as_one_result(per_model_portions, mock_settings, create_job_result):
    mixed_node = _node(create_job_result, [H200, RTX4090, RTX4090])
    jobs = {"mixed": [mixed_node]}
    incentive = DefaultIncentive(mock_settings.incentive, per_model_portions, jobs, {H200: 1, RTX4090: 2})

    await incentive.calculate_mining_scores()

    assert jobs["mixed"] == [mixed_node]
    assert jobs["mixed"][0] is mixed_node
    assert (mixed_node.gpu_model, mixed_node.gpu_count) == (H200, 3)
    assert mixed_node.incentive == pytest.approx(incentive.miner_incentives["mixed"])
    assert mixed_node.incentive_rented + mixed_node.incentive_idle == pytest.approx(mixed_node.incentive)
    assert mixed_node.incentive_formula_version == "per_gpu_model_v1"
    portions = mixed_node.incentive_formula_inputs["portions"]
    assert [(p["gpu_model"], p["gpu_count"]) for p in portions] == [(H200, 1), (RTX4090, 2)]
    assert sum(p["incentive"] for p in portions) == pytest.approx(mixed_node.incentive)


@pytest.mark.asyncio
async def test_a_single_model_node_is_scored_as_before(per_model_portions, mock_settings, create_job_result):
    node = _node(create_job_result, [H200, H200])
    jobs = {"plain": [node]}
    incentive = DefaultIncentive(mock_settings.incentive, per_model_portions, jobs, {H200: 2})

    await incentive.calculate_mining_scores()

    assert node.incentive_formula_version == "mining_v1"
    assert node.incentive == pytest.approx(MINING_ALLOCATION)


@pytest.mark.asyncio
async def test_rental_price_pays_an_idle_mixed_node_like_its_cards_on_separate_nodes(
    validator_with_rental_price, mock_settings, create_job_result, create_neuron_info, monkeypatch  # noqa: F811
):
    """1x H200 (idle pool) + 1x A100 (no idle pay, mining pool): on main it earned as 2x H200."""
    monkeypatch.setattr(incentive_base, "GPU_MODEL_RATES", {"H100": 1.0, "H200": 1.0, "A100": 1.0})
    validator = validator_with_rental_price
    validator.backend_client.get_all_rented_executors = AsyncMock(return_value=_make_rented_data())
    miners = [
        create_neuron_info(uid=100, hotkey="burner1"),
        create_neuron_info(uid=101, hotkey="burner2"),
        create_neuron_info(uid=2, hotkey="miner_a"),
        create_neuron_info(uid=3, hotkey="miner_b"),
    ]
    other = {"miner_b": [_node(create_job_result, ["H100"], executor_id="exec-b")]}

    validator.miner_scores = {}
    mixed_node = _node(create_job_result, ["H200", "A100"], executor_id="exec-a")
    await _run_sync_with_jobs(validator, miners, {"miner_a": [mixed_node], **other})
    paid_mixed = dict(validator.miner_scores)

    validator.miner_scores = {}
    validator.last_job_run_blocks = 0
    separate = [
        _node(create_job_result, ["H200"], executor_id="exec-a1"),
        _node(create_job_result, ["A100"], executor_id="exec-a2"),
    ]
    await _run_sync_with_jobs(validator, miners, {"miner_a": separate, **other})

    assert paid_mixed["miner_a"] > 0
    assert paid_mixed["miner_a"] == pytest.approx(validator.miner_scores["miner_a"])
    assert paid_mixed["miner_b"] == pytest.approx(validator.miner_scores["miner_b"])
    assert mixed_node.incentive_idle > 0 and mixed_node.incentive_rented == 0
    assert (mixed_node.gpu_model, mixed_node.gpu_count) == ("H200", 2)


def _split_node(create_job_result, cards: list[str], *, rented_count: int, rented_uuids: list[str] | None):
    result = create_job_result(gpu_model=cards[0], gpu_count=len(cards))
    result.spec = {
        "gpu": {"count": len(cards), "details": [{"name": name, "uuid": f"GPU-{i}"} for i, name in enumerate(cards)]}
    }
    result.is_rented = True
    result.supports_gpu_splitting = True
    result.gpu_splitting_min_count = 1
    result.rented_gpu_count = rented_count
    result.rented_gpu_uuids = rented_uuids
    return result


def _cards(portions):
    return [(p.gpu_model, p.gpu_count, p.is_rented) for p in portions]


@pytest.fixture
def split_scoring_on(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_SPLIT_PARTIAL_RENTAL_SCORING", True)


def test_a_partly_rented_mixed_node_pays_the_rented_pool_for_the_pods_cards_only(create_job_result, split_scoring_on):
    """Owner decision on #1580: the rented pool pays the cards the pods hold; the free ones are idle."""
    node = _split_node(create_job_result, [H200, H200, RTX4090, RTX4090], rented_count=2, rented_uuids=["GPU-1", "GPU-2"])

    assert _cards(BaseIncentive._portion_cards(node, node.gpu_model_counts)) == [
        (H200, 1, True),
        (H200, 1, False),
        (RTX4090, 1, True),
        (RTX4090, 1, False),
    ]


def test_unknown_pod_cards_pay_the_cheapest_cards_rented_and_nothing_idle(create_job_result, split_scoring_on):
    node = _split_node(create_job_result, [H200, H200, RTX4090], rented_count=2, rented_uuids=None)

    assert _cards(BaseIncentive._portion_cards(node, node.gpu_model_counts)) == [(H200, 1, True), (RTX4090, 1, True)]


def test_a_wholly_rented_mixed_node_is_rented_card_by_card(create_job_result, split_scoring_on):
    node = _split_node(create_job_result, [H200, RTX4090], rented_count=2, rented_uuids=["GPU-0", "GPU-1"])

    assert _cards(BaseIncentive._portion_cards(node, node.gpu_model_counts)) == [(H200, 1, True), (RTX4090, 1, True)]


@pytest.mark.asyncio
async def test_a_partly_rented_mixed_node_is_paid_from_both_pools_and_merged(
    per_model_portions, mock_settings, create_job_result, split_scoring_on
):
    node = _split_node(create_job_result, [H200, H200, RTX4090], rented_count=1, rented_uuids=["GPU-2"])
    rental_created_at = node.rental_created_at
    jobs = {"mixed": [node]}
    incentive = DefaultIncentive(mock_settings.incentive, per_model_portions, jobs, {H200: 2, RTX4090: 1})

    await incentive.calculate_mining_scores()

    assert jobs["mixed"] == [node]
    assert (node.gpu_model, node.gpu_count, node.is_rented, node.rented_gpu_count) == (H200, 3, True, 1)
    assert node.is_split_remainder is False and node.rental_created_at == rental_created_at
    portions = node.incentive_formula_inputs["portions"]
    assert [(p["gpu_model"], p["gpu_count"], p["is_rented"]) for p in portions] == [(H200, 2, False), (RTX4090, 1, True)]
    assert node.incentive_rented == pytest.approx(portions[1]["incentive"])
    assert node.incentive_idle == pytest.approx(portions[0]["incentive"])


def test_each_card_takes_its_own_models_filler_floor(per_model_portions, mock_settings, create_job_result):
    node = _node(create_job_result, [H200, RTX4090, RTX4090])
    node.filler_revenue_per_gpu_hour = 9.0  # looked up as 3x H200: never used on a mixed node
    node.filler_revenue_per_gpu_hour_by_model = {H200: 2.0, RTX4090: 0.3}
    jobs = {"mixed": [node]}
    incentive = DefaultIncentive(mock_settings.incentive, per_model_portions, jobs, {H200: 1, RTX4090: 2})

    incentive._expand_mixed_gpu_model_results()

    assert [(p.gpu_model, p.filler_revenue_per_gpu_hour) for p in jobs["mixed"]] == [(H200, 2.0), (RTX4090, 0.3)]


@pytest.mark.parametrize(
    "device_requests, expected",
    [
        ('[{"Count":0,"DeviceIDs":["GPU-b"],"Capabilities":[["gpu"]]}]', ["GPU-b"]),
        ('[{"Count":0,"DeviceIDs":["0","2"],"Capabilities":[["gpu"]]}]', ["GPU-a", "GPU-c"]),
        ('[{"Count":-1,"DeviceIDs":null,"Capabilities":[["gpu"]]}]', ["GPU-a", "GPU-b", "GPU-c"]),
        ('[{"Count":2,"DeviceIDs":null,"Capabilities":[["gpu"]]}]', None),
        ('[{"Count":0,"DeviceIDs":["GPU-x"],"Capabilities":[["gpu"]]}]', None),
        ("null", None),
        ("not json", None),
    ],
)
def test_a_pods_cards_are_read_from_its_gpu_device_request(device_requests, expected):
    assert _gpu_uuids_of_device_requests(device_requests, ["GPU-a", "GPU-b", "GPU-c"]) == expected


class _FakeSsh:
    def __init__(self, stdout: str, exit_status: int = 0):
        self.stdout, self.exit_status, self.commands = stdout, exit_status, []

    async def run(self, command):
        self.commands.append(command)
        return SimpleNamespace(stdout=self.stdout, exit_status=self.exit_status)


def _rented_ctx(ssh, cards):
    details = [{"name": name, "uuid": f"GPU-{i}"} for i, name in enumerate(cards)]
    return SimpleNamespace(ssh=ssh, state=SimpleNamespace(gpu_details=details), default_extra={})


def _pods(*names):
    return [RentedPod(pod_id=name, container_name=name) for name in names]


@pytest.mark.asyncio
async def test_the_rented_check_reads_the_cards_every_pod_holds_on_a_mixed_node():
    ssh = _FakeSsh(
        '[{"Count":0,"DeviceIDs":["GPU-0"],"Capabilities":[["gpu"]]}]\n'
        '[{"Count":0,"DeviceIDs":["GPU-2"],"Capabilities":[["gpu"]]}]\n'
    )
    ctx = _rented_ctx(ssh, [H200, H200, RTX4090])

    assert await _read_rented_gpu_uuids_of_mixed_node(ctx, _pods("pod-a", "pod-b")) == ["GPU-0", "GPU-2"]
    assert "pod-a pod-b" in ssh.commands[0]


@pytest.mark.asyncio
async def test_the_rented_check_reads_nothing_on_a_single_model_node():
    ssh = _FakeSsh("")
    assert await _read_rented_gpu_uuids_of_mixed_node(_rented_ctx(ssh, [H200, H200]), _pods("pod-a")) is None
    assert ssh.commands == []


@pytest.mark.asyncio
async def test_a_failed_read_leaves_the_rented_cards_unknown():
    ssh = _FakeSsh("Error: No such object: pod-b\n", exit_status=1)
    assert await _read_rented_gpu_uuids_of_mixed_node(_rented_ctx(ssh, [H200, RTX4090]), _pods("pod-a", "pod-b")) is None
