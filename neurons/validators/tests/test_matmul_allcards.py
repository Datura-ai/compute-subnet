"""DAH-2671 item 3 — full-matmul-all-cards work-proof.

Every claimed card gets its own challenge, pinned to that card's reported UUID with
CUDA_VISIBLE_DEVICES, judged like the single-card run and timed in aggregate on the validator:
  - a card is verified only when its answer carries its own nonce (exit 0 + output is not enough);
  - the verdict passes only when the verified cards equal the reported GPU count;
  - two cards reporting one UUID, or a count with no card detail behind it, fail the verdict;
  - shadow (the default) logs the verdict and leaves the validation result alone, enforcement
    fails the validation;
  - an aggregate wall-clock over threshold fails the verdict;
  - a single-card (legacy) executor is never judged by the all-cards path (probe returns None).
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.matrix_validation_service as mvs
from core.config import Settings
from services.gpu_spec_table import GPU_VRAM_SIZES_MB, VRAM_FLOOR_RATIO, matmul_probe_vram_mb

MODEL = "NVIDIA H100 80GB HBM3"


class FakeLib:
    """Stands in for libdmcompverify on both sides. Each challenge gets its own generator object
    and cipher; the cipher remembers the nonce and the machine info it was built for. A seal
    opens only with the generator that built the challenge it answers."""

    def __init__(self):
        self.wrapper = MagicMock(name="DMCompVerifyWrapper")
        self.wrapper._has_sealed = True
        self.challenges: dict[str, dict] = {}  # cipher -> {"nonce", "machine_info"}
        self._cipher_of_ptr: dict[str, str] = {}
        self._count = 0
        self.wrapper.DMCompVerify_new.side_effect = self._new
        self.wrapper.generateChallenge.side_effect = self._generate
        self.wrapper.getCipherText.side_effect = lambda ptr: self._cipher_of_ptr.get(ptr, "")
        self.wrapper.unsealResult.side_effect = self._unseal

    def _new(self, *_dims):
        self._count += 1
        return f"ptr{self._count}"

    def _generate(self, ptr, seed, machine_info, nonce):
        cipher = f"cipher{ptr}"
        self._cipher_of_ptr[ptr] = cipher
        self.challenges[cipher] = {"nonce": nonce, "machine_info": json.loads(machine_info)}

    def _unseal(self, ptr, blob):
        cipher, _, answer = blob.partition(":")
        if self._cipher_of_ptr.get(ptr) != cipher:
            return ""
        return json.dumps({"uuid": answer, "metrics": None})


@pytest.fixture
def lib(monkeypatch):
    fake = FakeLib()
    monkeypatch.setattr(mvs, "DMCompVerifyWrapper", lambda *a, **k: fake.wrapper)
    return fake


@pytest.fixture
def svc(lib):
    """Real ValidationService over the fake native library (no .so on the test host)."""
    return mvs.ValidationService()


def _executor():
    return SimpleNamespace(root_dir="/root/app", python_path="/usr/bin/python")


def _uuids(count):
    return [f"GPU-{i:08x}-aaaa-bbbb-cccc-0123456789ab" for i in range(count)]


def _spec(count, model=MODEL, uuids=None):
    uuids = _uuids(count) if uuids is None else uuids
    return {
        "gpu": {
            "count": count,
            "details": [{"name": model, "uuid": uuid, "capacity": 81920} for uuid in uuids],
        }
    }


def _pinned(cmd: str) -> str | None:
    if not cmd.startswith("CUDA_VISIBLE_DEVICES="):
        return None
    return cmd.split("CUDA_VISIBLE_DEVICES=")[1].split(" ")[0]


def _cipher(cmd: str) -> str:
    return cmd.split("--cipher_text ")[1].strip()


def _answer(cipher: str, nonce: str) -> SimpleNamespace:
    line = "RESULT_JSON: " + json.dumps({"uuid": nonce, "metrics": None, "sealed": f"{cipher}:{nonce}"})
    return SimpleNamespace(exit_status=0, stdout=f"UUID:  {nonce}\n{line}", stderr="")


class FakeHost:
    """An executor host. `reported` is what an unpinned run enumerates (the single-card path);
    `real` are the cards a pinned run can actually open. A run recovers the nonce only when the
    devices it sees are the ones the challenge was built for, as the library's key derivation does.
    `overrides` maps a pinned UUID to a canned reply or exception."""

    def __init__(self, lib, reported, real=None, model=MODEL, overrides=None):
        self.lib = lib
        self.reported = list(reported)
        self.real = list(reported if real is None else real)
        self.model = model
        self.overrides = overrides or {}
        self.commands: list[str] = []

    async def run(self, cmd, *args, **kwargs):
        self.commands.append(cmd)
        if cmd.startswith("pkill"):
            return SimpleNamespace(exit_status=0, stdout="", stderr="")
        pinned = _pinned(cmd)
        if pinned in self.overrides:
            reply = self.overrides[pinned]
            if isinstance(reply, BaseException):
                raise reply
            return reply
        if pinned is None:
            visible = self.reported
        else:
            visible = [pinned] if pinned in self.real else []
        if not visible:
            return SimpleNamespace(exit_status=1, stdout="", stderr="no CUDA-capable device")
        cipher = _cipher(cmd)
        challenge = self.lib.challenges[cipher]
        sees = {"uuids": ",".join(visible), "gpu_count": len(visible), "gpu_model": self.model}
        nonce = challenge["nonce"] if challenge["machine_info"] == sees else "not-the-nonce"
        return _answer(cipher, nonce)

    @property
    def pinned_runs(self) -> list[str]:
        return [_pinned(cmd) for cmd in self.commands if _pinned(cmd)]


def _flags(monkeypatch, *, enforce):
    monkeypatch.setattr(mvs.settings, "MATMUL_ALLCARDS_CHECK_ENABLED", True, raising=False)
    monkeypatch.setattr(
        mvs.settings, "MATMUL_ALLCARDS_ENFORCEMENT_ENABLED", enforce, raising=False
    )


def _reasons(probe) -> list[str]:
    return [card["reason"] for card in probe.per_card]


def test_check_runs_by_default_and_enforcement_stays_off():
    fields = Settings.model_fields
    assert fields["MATMUL_ALLCARDS_CHECK_ENABLED"].default is True
    assert fields["MATMUL_ALLCARDS_ENFORCEMENT_ENABLED"].default is False


@pytest.mark.asyncio
async def test_all_cards_verified_passes(svc, lib):
    uuids = _uuids(8)
    host = FakeHost(lib, uuids)
    probe = await svc._probe_all_claimed_cards(host, _executor(), {}, _spec(8))
    assert probe is not None
    assert probe.passed is True
    assert probe.verified_count == 8
    assert all(card["ok"] for card in probe.per_card)
    # one run per card, each pinned to its own reported UUID
    assert sorted(host.pinned_runs) == sorted(uuids)
    # each card answers a challenge of its own: own nonce, built for that card alone
    challenges = list(lib.challenges.values())
    assert len({challenge["nonce"] for challenge in challenges}) == 8
    assert sorted(challenge["machine_info"]["uuids"] for challenge in challenges) == sorted(uuids)
    assert all(challenge["machine_info"]["gpu_count"] == 1 for challenge in challenges)
    assert all(challenge["machine_info"]["gpu_model"] == MODEL for challenge in challenges)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stdout",
    [
        "RESULT_JSON: {}",
        "some output",
        "UUID:  11111111-2222-4333-8444-555555555555",
        'RESULT_JSON: {"uuid": "11111111-2222-4333-8444-555555555555", "metrics": null}',
    ],
)
async def test_card_with_output_but_no_valid_nonce_fails_verdict(svc, lib, stdout):
    uuids = _uuids(4)
    host = FakeHost(
        lib,
        uuids,
        overrides={uuids[2]: SimpleNamespace(exit_status=0, stdout=stdout, stderr="")},
    )
    probe = await svc._probe_all_claimed_cards(host, _executor(), {}, _spec(4))
    assert probe.passed is False
    assert probe.verified_count == 3
    assert _reasons(probe) == ["", "", "unverified", ""]
    assert probe.failure_reason == "1/4 card(s) failed device work-proof"


@pytest.mark.asyncio
async def test_one_card_answer_replayed_for_another_card_fails(svc, lib):
    # card 1 hands back card 0's (genuine) answer: the seal does not open with card 1's key
    uuids = _uuids(2)
    honest = FakeHost(lib, uuids)

    async def run(cmd, *args, **kwargs):
        if _pinned(cmd) == uuids[1]:
            first = next(c for c in honest.commands if _pinned(c) == uuids[0])
            return _answer(_cipher(first), lib.challenges[_cipher(first)]["nonce"])
        return await honest.run(cmd, *args, **kwargs)

    probe = await svc._probe_all_claimed_cards(
        SimpleNamespace(run=run), _executor(), {}, _spec(2)
    )
    assert probe.passed is False
    assert _reasons(probe) == ["", "unverified"]


@pytest.mark.asyncio
async def test_two_cards_with_same_uuid_fail_verdict(svc, lib):
    uuids = _uuids(3)
    reported = [uuids[0], uuids[1], uuids[1]]
    host = FakeHost(lib, reported)
    probe = await svc._probe_all_claimed_cards(host, _executor(), {}, _spec(3, uuids=reported))
    assert probe.passed is False
    assert probe.verified_count == 1
    assert _reasons(probe) == ["", "uuid_duplicate", "uuid_duplicate"]
    # a UUID reported twice proves no card: it gets no run
    assert host.pinned_runs == [uuids[0]]


@pytest.mark.asyncio
async def test_fewer_verified_cards_than_reported_count_fails_verdict(svc, lib):
    # eight reported, one card can actually be opened
    uuids = _uuids(8)
    host = FakeHost(lib, uuids, real=uuids[:1])
    probe = await svc._probe_all_claimed_cards(host, _executor(), {}, _spec(8))
    assert probe.passed is False
    assert probe.verified_count == 1
    assert probe.failure_reason == "7/8 card(s) failed device work-proof"


@pytest.mark.asyncio
async def test_count_above_reported_card_details_fails_verdict(svc, lib):
    # count says 4, two cards are described: the other two cannot be verified
    uuids = _uuids(2)
    spec = _spec(2)
    spec["gpu"]["count"] = 4
    probe = await svc._probe_all_claimed_cards(FakeHost(lib, uuids), _executor(), {}, spec)
    assert probe.passed is False
    assert probe.verified_count == 2
    assert _reasons(probe) == ["", "", "not_reported", "not_reported"]


@pytest.mark.asyncio
async def test_uuid_that_is_not_a_gpu_uuid_gets_no_run(svc, lib):
    reported = [_uuids(1)[0], "GPU-1; touch /tmp/x"]
    host = FakeHost(lib, reported)
    probe = await svc._probe_all_claimed_cards(host, _executor(), {}, _spec(2, uuids=reported))
    assert probe.passed is False
    assert _reasons(probe) == ["", "uuid_invalid"]
    assert host.pinned_runs == [reported[0]]


@pytest.mark.asyncio
async def test_enforcement_off_logs_verdict_and_leaves_result_unchanged(
    svc, lib, monkeypatch, caplog
):
    _flags(monkeypatch, enforce=False)
    uuids = _uuids(8)
    # the single-card run sees eight devices and passes; only one card can be opened when pinned
    host = FakeHost(lib, uuids, real=uuids[:1])
    with caplog.at_level("WARNING", logger=mvs.logger.name):
        result = await svc.validate_gpu_model_and_process_job(
            ssh_client=host, executor_info=_executor(), default_extra={}, machine_spec=_spec(8)
        )
    # shadow: the validation result is the single-card run's, untouched
    assert result.success is True
    assert result.error_message == ""
    verdicts = [rec for rec in caplog.records if "All-cards GPU work-proof" in rec.getMessage()]
    assert len(verdicts) == 1
    assert verdicts[0].levelname == "WARNING"
    logged = verdicts[0].msg.extra
    assert logged["passed"] is False
    assert logged["gpu_count_claimed"] == 8
    assert logged["gpu_count_verified"] == 1
    assert logged["enforced"] is False
    assert [card["ok"] for card in logged["per_card"]] == [True] + [False] * 7


@pytest.mark.asyncio
async def test_enforcement_on_fails_validation(svc, lib, monkeypatch):
    _flags(monkeypatch, enforce=True)
    uuids = _uuids(8)
    host = FakeHost(lib, uuids, real=uuids[:1])
    result = await svc.validate_gpu_model_and_process_job(
        ssh_client=host, executor_info=_executor(), default_extra={}, machine_spec=_spec(8)
    )
    assert result.success is False
    assert result.error_message == (
        "All-cards GPU work-proof failed: 7/8 card(s) failed device work-proof"
    )
    # the single-card run is not reached
    assert all(_pinned(cmd) for cmd in host.commands)


@pytest.mark.asyncio
async def test_enforcement_on_passes_when_every_card_is_verified(svc, lib, monkeypatch):
    _flags(monkeypatch, enforce=True)
    host = FakeHost(lib, _uuids(8))
    result = await svc.validate_gpu_model_and_process_job(
        ssh_client=host, executor_info=_executor(), default_extra={}, machine_spec=_spec(8)
    )
    assert result.success is True


@pytest.mark.asyncio
async def test_check_off_runs_no_per_card_work(svc, lib, monkeypatch):
    monkeypatch.setattr(mvs.settings, "MATMUL_ALLCARDS_CHECK_ENABLED", False, raising=False)
    host = FakeHost(lib, _uuids(8))
    result = await svc.validate_gpu_model_and_process_job(
        ssh_client=host, executor_info=_executor(), default_extra={}, machine_spec=_spec(8)
    )
    assert result.success is True
    assert host.pinned_runs == []


@pytest.mark.asyncio
async def test_wall_clock_over_threshold_fails_under_enforcement(svc, lib, monkeypatch):
    _flags(monkeypatch, enforce=True)
    # -1s threshold: any real elapsed trips it even though every card ran fine (models serialisation).
    monkeypatch.setattr(mvs, "MATMUL_ALLCARDS_WALL_CLOCK_SECONDS", -1, raising=False)
    result = await svc.validate_gpu_model_and_process_job(
        ssh_client=FakeHost(lib, _uuids(8)),
        executor_info=_executor(),
        default_extra={},
        machine_spec=_spec(8),
    )
    assert result.success is False
    assert "wall-clock" in result.error_message


@pytest.mark.asyncio
async def test_wall_clock_over_threshold_only_emits_under_shadow(svc, lib, monkeypatch, caplog):
    _flags(monkeypatch, enforce=False)
    monkeypatch.setattr(mvs, "MATMUL_ALLCARDS_WALL_CLOCK_SECONDS", -1, raising=False)
    with caplog.at_level("WARNING"):
        result = await svc.validate_gpu_model_and_process_job(
            ssh_client=FakeHost(lib, _uuids(8)),
            executor_info=_executor(),
            default_extra={},
            machine_spec=_spec(8),
        )
    # shadow: the probe never turns into a returned all-cards failure.
    assert result.success is True
    # but the observation is emitted.
    assert any("All-cards GPU work-proof" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_legacy_single_card_is_not_judged_by_all_cards(svc, lib):
    # gpu_count <= 1 → probe returns None, so the executor is judged only by the unchanged
    # single-card path and a not-yet-upgraded / single-GPU fleet is never zeroed by this check.
    probe = await svc._probe_all_claimed_cards(FakeHost(lib, _uuids(1)), _executor(), {}, _spec(1))
    assert probe is None


@pytest.mark.asyncio
async def test_unknown_model_skips_probe(svc, lib):
    # a model with no registry VRAM size cannot be sized safely → probe returns None (fail-open).
    host = FakeHost(lib, _uuids(8), model="NVIDIA MADE-UP 9000")
    probe = await svc._probe_all_claimed_cards(
        host, _executor(), {}, _spec(8, model="NVIDIA MADE-UP 9000")
    )
    assert probe is None
    assert host.commands == []


@pytest.mark.asyncio
async def test_native_cipher_failure_skips_probe(svc, lib):
    # Our own .so failing must not read as the host failing: an empty cipher would go out as a bare
    # `--cipher_text ` and every card would come back ok=False. Skip the probe instead.
    lib.wrapper.getCipherText.side_effect = lambda ptr: ""
    host = FakeHost(lib, _uuids(8))
    probe = await svc._probe_all_claimed_cards(host, _executor(), {}, _spec(8))
    assert probe is None
    assert host.commands == []
    # every generator built on the way is released
    assert lib.wrapper.free.call_count == lib.wrapper.DMCompVerify_new.call_count - 1


@pytest.mark.asyncio
async def test_every_card_generator_is_released(svc, lib):
    await svc._probe_all_claimed_cards(FakeHost(lib, _uuids(4)), _executor(), {}, _spec(4))
    # the service holds one long-lived generator of its own (built in __init__)
    assert lib.wrapper.free.call_count == 4


def test_matmul_probe_vram_mb_floors_below_observed():
    # Finding 1: size each card's challenge from the 0.90 floor, NOT the raw nominal, so an honest
    # B200/L40S is never asked to allocate past its usable (NVML-reported) VRAM and OOM.
    b200 = matmul_probe_vram_mb("NVIDIA B200")
    assert b200 == round(196608 * VRAM_FLOOR_RATIO)
    assert b200 < 183359                                   # below observed usable total (registry note)
    assert b200 < GPU_VRAM_SIZES_MB["NVIDIA B200"][0]      # strictly below the raw nominal
    # multi-variant: smallest nominal, then floored
    assert matmul_probe_vram_mb("NVIDIA GeForce RTX 3060") == round(8192 * VRAM_FLOOR_RATIO)
    assert matmul_probe_vram_mb("NVIDIA MADE-UP 9000") is None


@pytest.mark.asyncio
async def test_card_fan_out_stays_under_sshd_max_sessions(svc, lib):
    # Finding: one SSH channel per card on a single connection; OpenSSH defaults to MaxSessions 10,
    # so a 14-card host must not open 14 at once or the tail channels read as failed work-proofs.
    in_flight = 0
    peak = 0
    host = FakeHost(lib, _uuids(14))

    async def run(cmd, *a, **k):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return await host.run(cmd, *a, **k)

    probe = await svc._probe_all_claimed_cards(SimpleNamespace(run=run), _executor(), {}, _spec(14))
    assert probe is not None and probe.passed is True
    assert peak <= mvs.MATMUL_ALLCARDS_MAX_CONCURRENT_CARDS < 10


@pytest.mark.asyncio
async def test_timeout_sweeps_orphan_matmul(svc, lib, monkeypatch):
    # Finding 2: a timed-out per-card run must trigger _kill_remote_matmul, or the orphaned remote
    # process keeps holding VRAM into the fatal single-card matmul that runs next in the same call.
    killed = AsyncMock()
    monkeypatch.setattr(svc, "_kill_remote_matmul", killed)
    uuids = _uuids(2)
    host = FakeHost(lib, uuids, overrides={uuids[0]: TimeoutError()})

    probe = await svc._probe_all_claimed_cards(host, _executor(), {}, _spec(2))
    assert probe is not None and probe.passed is False
    assert _reasons(probe) == ["timeout", ""]
    killed.assert_awaited_once()


@pytest.mark.asyncio
async def test_ssh_error_sweeps_orphan_matmul(svc, lib, monkeypatch):
    # A channel that dies mid-run leaves the remote matmul untouched, exactly like a timeout — and
    # the fatal single-card matmul that follows lands on device 0 and OOMs against it.
    killed = AsyncMock()
    monkeypatch.setattr(svc, "_kill_remote_matmul", killed)
    uuids = _uuids(2)
    host = FakeHost(lib, uuids, overrides={uuids[0]: OSError("channel closed")})

    probe = await svc._probe_all_claimed_cards(host, _executor(), {}, _spec(2))
    assert probe is not None and probe.passed is False
    killed.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_plain_card_failure_does_not_sweep(svc, lib, monkeypatch):
    # A card that answered (non-zero exit) has no process left to kill; sweeping there would pkill
    # the fatal single-card matmul of a concurrent validator run on the same host for nothing.
    killed = AsyncMock()
    monkeypatch.setattr(svc, "_kill_remote_matmul", killed)
    uuids = _uuids(2)
    host = FakeHost(lib, uuids, real=uuids[1:])

    probe = await svc._probe_all_claimed_cards(host, _executor(), {}, _spec(2))
    assert probe is not None and probe.passed is False
    assert _reasons(probe) == ["exit_status=1", ""]
    killed.assert_not_awaited()
