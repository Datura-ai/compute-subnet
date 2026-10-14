import asyncio
import json
import logging
import math
import os
import random
import re
import shlex
import time
import uuid as uuid4
from ctypes import CDLL, POINTER, c_char_p, c_longlong, c_void_p
from dataclasses import dataclass, field

import asyncssh

from core.config import settings
from core.utils import _m, get_extra_info
from services.gpu_spec_table import matmul_probe_vram_mb

logger = logging.getLogger(__name__)

# Hard cap on the remote matmul run. Real runs finish in 4-27s; without a cap a wedged
# GPU (e.g. VRAM held by an orphaned container, DAH-2364) hangs the whole pipeline until
# the outer JOB_TIME_OUT cancellation, which dies silently without a reason_code (DAH-2365).
MATRIX_VERIFY_TIMEOUT_SECONDS = 120

# DAH-2671 item 3 — wide aggregate wall-clock (seconds) for the all-claimed-cards work-proof.
# N honest cards run concurrently in ~single-card time (4-27s); a count lie that serialises onto one
# real card blows out to ~N*single-card. This threshold is deliberately WIDE so honest slowdowns
# (disabled P2P/IOMMU/ACS, 10-20% thermal throttle, a card already busy with a filler) do not trip
# it; the emitted RAW timing lets ops recalibrate before flipping enforcement. The per-card SSH runs
# each keep the MATRIX_VERIFY_TIMEOUT_SECONDS cap, so this only fails clear serialisation.
MATMUL_ALLCARDS_WALL_CLOCK_SECONDS = 90

# Cards probed at once. Each card takes an SSH channel on one connection and MAX_GPU_COUNT is 14,
# while OpenSSH defaults to MaxSessions 10 — 8 keeps the concurrency signal (a count lie still
# serialises) and stays clear of that ceiling on the widest honest hosts.
MATMUL_ALLCARDS_MAX_CONCURRENT_CARDS = 8

def allcards_wall_clock_limit_seconds(cards_run: int) -> int:
    """The aggregate wall-clock limit for `cards_run` concurrent per-card runs.

    The cards run MATMUL_ALLCARDS_MAX_CONCURRENT_CARDS at a time, so a node with more cards than
    that runs in rounds and an honest one takes about one card's time per round. The limit is the
    one-round threshold times the number of rounds, so a wide honest node is not failed on time
    alone while a count lie still serialises past it.
    """
    rounds = max(1, math.ceil(cards_run / MATMUL_ALLCARDS_MAX_CONCURRENT_CARDS))
    return MATMUL_ALLCARDS_WALL_CLOCK_SECONDS * rounds


# The shape a reported GPU UUID must have before it is placed on the per-card command line.
GPU_UUID_PATTERN = re.compile(r"^GPU-[0-9a-fA-F-]{1,64}$")


def should_run_allcards_probe(first_pass: bool) -> bool:
    """Whether the all-cards work-proof runs on this validation.

    While its verdict is only logged (enforcement off) it cannot fail the node, so a node's first
    pass skips it and the first verification stays as fast as the single-card run; the next cycle
    runs it. Once enforcement is on it runs on the first pass as well. `first_pass` is the
    pipeline's `ctx.config.first_pass`, which is set only while FIRST_PASS_FAST_PATH_ENABLED is on
    (off by default): with the fast path off a first pass runs like a scored cycle, proof included.
    """
    if not settings.MATMUL_ALLCARDS_CHECK_ENABLED:
        return False
    return not first_pass or settings.MATMUL_ALLCARDS_ENFORCEMENT_ENABLED


def allcards_probe_applies(machine_spec: dict) -> bool:
    """Whether the all-cards work-proof has anything to prove on this machine.

    It needs more than one claimed card, a reported card detail, and at least one claimed card of
    a model with a registry VRAM size (the others fail on their own as `unknown_model`). A
    single-GPU node, or one with no card that can be sized, is left to the single-card run.
    """
    gpu = (machine_spec or {}).get("gpu", {}) or {}
    gpu_count = gpu.get("count", 0) or 0
    details = gpu.get("details", []) or []
    if gpu_count <= 1 or not details:
        return False
    return any(matmul_probe_vram_mb(card.get("name", "") or "") for card in details[:gpu_count])

# VRAM (MB) the work-proof leaves untouched: a quarter of the card, clamped between the two.
# The reserve must clear what an idle card may legally hold (GPU_HELD_VRAM_MB_LIMIT, 2048 MB —
# a B200 in confidential-computing mode sits just under it at 1766 MB) plus the probe's own CUDA
# context, ~600 MB on Blackwell; under the old flat 2 GB, d_A fit and d_B OOMed (DAH-2850).
# Constants, not host-reported memory_used_mb — an executor could use that to shrink its own
# challenge. The floor keeps 3-6 GB consumer cards on the 2 GB they have always run with.
MATMUL_VRAM_RESERVE_MAX_MB = 4096
MATMUL_VRAM_RESERVE_MIN_MB = 2048


class DMCompVerifyWrapper:
    def __init__(self, lib_name: str):
        """
        Constructor, differentiate miner vs validator libs.
        """
        self._initialized = False
        lib_path = os.path.join(os.path.dirname(__file__), lib_name)
        self._lib = CDLL(lib_path)
        self._setup_lib_functions()

    def _setup_lib_functions(self):
        """
        Set up function signatures for the library.
        """
        # Set up function signatures for the library.
        self._lib.DMCompVerify_new.argtypes = [c_longlong, c_longlong]  # Parameters (long m_dim_n, long m_dim_k)
        self._lib.DMCompVerify_new.restype = POINTER(c_void_p)  # Return type is a pointer to a structure.

        self._lib.setDimension.argtypes = [POINTER(c_void_p), c_longlong, c_longlong]  # Parameters (long m_dim_n, long m_dim_k)
        self._lib.setDimension.restype = POINTER(c_void_p)  # Return type is a pointer to a structure.

        self._lib.generateChallenge.argtypes = [POINTER(c_void_p), c_longlong, c_char_p, c_char_p]
        self._lib.generateChallenge.restype = None
        
        self._lib.getCipherText.argtypes = [c_void_p]
        self._lib.getCipherText.restype = c_char_p

        # unsealResult is optional: present only on sealing-aware .so builds. It
        # authenticates+decrypts the executor's sealed {uuid, metrics} blob using the
        # generate-side object's key. An older .so without the symbol still loads.
        self._has_sealed = hasattr(self._lib, "unsealResult")
        if self._has_sealed:
            self._lib.unsealResult.argtypes = [POINTER(c_void_p), c_char_p]
            self._lib.unsealResult.restype = c_char_p

        self._lib.free.argtypes = [c_void_p]
        self._lib.free.restype = None

        self._initialized = True

    def DMCompVerify_new(self, m_dim_n: int, m_dim_k: int):
        """
        Wrap the C++ function DMCompVerify_new.
        Creates a new DMCompVerify object in C++.
        """
        return self._lib.DMCompVerify_new(m_dim_n, m_dim_k)

    def generateChallenge(self, verifier_ptr: POINTER(c_void_p), seed: int, machine_info: str, uuid: str):
        """
        Wrap the C++ function generateChallenge.
        Generates a challenge using the provided DMCompVerify pointer.
        """
        machine_info_bytes = machine_info.encode('utf-8')
        uuid_bytes = uuid.encode('utf-8')
        self._lib.generateChallenge(verifier_ptr, seed, machine_info_bytes, uuid_bytes)

    def getCipherText(self, verifier_ptr: POINTER(c_void_p)) -> str:
        """
        Wrap the C++ function getCipherText.
        Retrieves the cipher text as a string.
        """
        # Fetch the cipher text from the C++ function (assumes it's returned as a char*).
        cipher_text_ptr = self._lib.getCipherText(verifier_ptr)

        if cipher_text_ptr:
            cipher_text = c_char_p(cipher_text_ptr).value  # Decode the C string
            # self.free(cipher_text_ptr)
            return cipher_text.decode('utf-8')
        else:
            return None

    def unsealResult(self, verifier_ptr: POINTER(c_void_p), sealed_hex: str) -> str:
        """
        Wrap the C++ function unsealResult. Authenticates + decrypts the executor's
        sealed blob using THIS object's generate-side key. Returns the inner JSON
        ('{"uuid": "...", "metrics": {...}}') or "" on malformed input / auth failure.
        """
        if not getattr(self, "_has_sealed", False) or not sealed_hex:
            return ""
        result_ptr = self._lib.unsealResult(verifier_ptr, sealed_hex.encode('utf-8'))
        if result_ptr:
            value = c_char_p(result_ptr).value
            return value.decode('utf-8') if value else ""
        return ""

    def setDimension(self, ptr: c_void_p, m_dim_n: int, m_dim_k: int):
        self._lib.setDimension(ptr, m_dim_n, m_dim_k)

    def free(self, ptr: c_void_p):
        """
        Frees memory allocated for the given pointer.
        """
        self._lib.free(ptr)

@dataclass
class VerifierParams:
    def __init__(self, dim_n: int = 1000, dim_k: int = 10000, seed: int = 0, uuid: str = ""):
        self.dim_n = dim_n
        self.dim_k = dim_k
        self.seed = seed
        self.uuid = uuid
        self.cipher_text = ""

    def generate(self):
        # You can modify the range for more randomness or based on specific needs
        self.dim_n = random.randint(1900, 2000)  # Random dim_n between 1900 and 2000
        self.dim_k = random.randint(2000000, 2586932)  # Random dim_k between 2000000 and 2586932
        self.seed = int(time.time())
        self.uuid = str(uuid4.uuid4())

    def __str__(self) -> str:
        return f"--dim_n {self.dim_n} --dim_k {self.dim_k} --seed {self.seed} --cipher_text {self.cipher_text}"


# The error a probe that ran and answered no (or a wrong) uuid gets. CapabilityCheck classifies
# only this failure by the probe's output (stderr and stdout): the other empty-uuid results (a sealed blob that failed
# authentication, a stdout that could not be parsed) are not the probe's answer.
UUID_MISMATCH_ERROR_PREFIX = "UUID mismatch"

# Whose clock `ValidationResult.elapsed_seconds` is: the validator's around the SSH run, or the
# executor's own word for the step when the answer came through the local transport.
ELAPSED_SOURCE_VALIDATOR = "validator_wall_clock"
ELAPSED_SOURCE_EXECUTOR = "executor_reported"


@dataclass
class ValidationResult:
    """Result of GPU validation with detailed debugging information."""
    success: bool
    expected_uuid: str = ""
    returned_uuid: str = ""
    stdout: str = ""
    stderr: str = ""
    error_message: str = ""
    metrics: dict | None = None
    timed_out: bool = False
    # What the run measured, for the speed log only; no verdict reads these. `sealed` is True when
    # uuid and metrics came out of an authenticated sealed blob, False when they are the executor's
    # plaintext, None when no answer was judged. `elapsed_seconds` is the wall-clock of the run and
    # `elapsed_source` says whose clock it is.
    sealed: bool | None = None
    elapsed_seconds: float | None = None
    elapsed_source: str | None = None
    dim_n: int | None = None
    dim_k: int | None = None

    def __bool__(self) -> bool:
        """Allow using ValidationResult in boolean context for backward compatibility."""
        return self.success


@dataclass
class AllCardsProbe:
    """Outcome of the all-claimed-cards work-proof (DAH-2671 item 3).

    A card counts only when its own run answered its own challenge: the nonce comes back (sealed
    when the executor seals) under a key derived from that card's reported UUID and model. The
    verdict passes when every reported card is verified, so `verified_count == gpu_count`.

    Honesty (validator-side only, never sent to the host): this proves N cards' worth of capacity
    existed in the window, NOT that the cards share one chassis — a relay to a genuine 8-GPU host
    still passes. Chassis-binding (NCCL) is a follow-on ticket.
    """

    passed: bool
    failure_reason: str
    elapsed_seconds: float
    over_wall_clock: bool
    per_card: list[dict]
    verified_count: int = 0
    # The judged result of each verified card's own run, keyed by card index. Never logged.
    card_results: dict[int, "ValidationResult"] = field(default_factory=dict)


@dataclass
class CardOutcome:
    """One card's run in the all-cards work-proof: its logged outcome and, when verified, its judged
    result (the same shape the single-card run returns). `as_log()` is the per-card log shape."""

    card_index: int
    ok: bool
    reason: str
    elapsed_seconds: float | None = None
    result: "ValidationResult | None" = None

    def as_log(self) -> dict:
        logged = {"card_index": self.card_index, "ok": self.ok, "reason": self.reason}
        if self.elapsed_seconds is not None:
            logged["elapsed_seconds"] = self.elapsed_seconds
        return logged


@dataclass
class CardChallenges:
    """The per-card challenges of one all-cards work-proof, built before any card runs.

    `per_card` holds every claimed card's outcome so far: the cards decided without a run (no
    detail, a model with no registry size, an invalid or duplicated UUID) already carry their
    failure reason. `challenges` holds
    one challenge per card that runs, keyed by card index; `close()` releases their keys.
    """

    per_card: list[dict]
    challenges: dict[int, "MatmulChallenge"]
    reported_uuids: list[str]

    def close(self) -> None:
        for challenge in self.challenges.values():
            challenge.close()


class ValidationService:
    def __init__(self):
        """
        Constructor, differentiate miner vs validator libs.
        """
        self.wrapper = DMCompVerifyWrapper("/usr/lib/libdmcompverify.so")
        self.verifier_ptr = self.wrapper.DMCompVerify_new(10, 10)

    def encrypt_challenge(self, m_dim_n, m_dim_k, seed, machine_info, uuid):
        try:
            # Example of usage:
            # Create a new DMCompVerify object
            self.wrapper.setDimension(self.verifier_ptr, m_dim_n, m_dim_k)

            self.wrapper.generateChallenge(self.verifier_ptr, seed, machine_info, uuid)

            cipher_text = self.wrapper.getCipherText(self.verifier_ptr)
            return cipher_text
        except Exception as e:
            logger.error("Failed encrypt challenge request: %s", str(e))
            return ""
    
    def get_gpu_memory(self, machine_spec: dict) -> bool:
        """
        Check if machine has data center GPUs (A100, H100, H200 or similar with >40GB memory)
        A data center GPU, or Graphics Processing Unit, is a specialized electronic circuit that speeds up tasks in data centers. 
        GPUs are used to perform parallel processing, which is ideal for workloads that require simultaneous computations. 
        Args:
            machine_spec: Machine specification dictionary

        Returns:
            bool: is_data_center
        """
        if machine_spec.get("gpu", {}).get("count", 0) > 0:
            details = machine_spec["gpu"].get("details", [])
            if len(details) > 0:
                gpu_memory = details[0].get("capacity", 0)  # Memory in MB

                return gpu_memory

        return 0

    def get_max_matrix_dimensions(self, gpu_memory, dim_n):
        reserved_mb = min(
            MATMUL_VRAM_RESERVE_MAX_MB, max(MATMUL_VRAM_RESERVE_MIN_MB, gpu_memory // 4)
        )
        max_memory = (gpu_memory - reserved_mb) * (1024.0 ** 2)

        element_size = 8  # 8 bytes for double precision

        # Calculate maximum number of elements that can fit in the available memory
        max_elements = max_memory // element_size

        max_dim_k = max_elements // (2 * dim_n) - dim_n

        return max_dim_k

    async def _kill_remote_matmul(self, ssh_client, script_path: str, log_extra: dict) -> None:
        # best-effort kill of the leaked matmul process so it stops holding GPU memory
        try:
            # SIGKILL: a SIGTERM can be ignored/slow on a wedged process; -9 is strictly
            # more reliable outside pure D-state (where no signal lands anyway)
            await ssh_client.run(f"pkill -9 -f {shlex.quote(script_path)}", timeout=10)
        except Exception as e:
            logger.warning(
                _m(
                    f"Failed to kill remote matmul process: {e}",
                    extra=get_extra_info(log_extra),
                )
            )

    @staticmethod
    def _card_probe_vram_mb(card: dict) -> int | None:
        """VRAM (MB) one card's challenge is sized from: the card's own reported capacity.

        The challenge fills the memory the card reports (less the usual reserve), so a card that
        reports more memory than it has cannot complete it, exactly as the single-card run sizes
        card 0. The registry floor for the model (smallest known variant) is a lower bound only,
        so a card that under-reports still gets at least the challenge its model must hold.
        None for a model with no registry size: the probe is then skipped.
        """
        registry_floor_mb = matmul_probe_vram_mb(card.get("name", "") or "")
        if not registry_floor_mb:
            return None
        try:
            reported_mb = int(card.get("capacity", 0) or 0)
        except (TypeError, ValueError):
            reported_mb = 0
        return max(reported_mb, registry_floor_mb)

    def _prepare_card_challenges(
        self,
        cards: list[dict],
        gpu_count: int,
        sized_vram_mb: list[int | None],
        default_extra: dict,
    ) -> CardChallenges | None:
        """Decide the cards that cannot be verified and build one challenge per remaining card.

        A claimed card with no reported detail, a model with no registry size (`unknown_model`:
        it cannot be sized), a UUID that is not a GPU UUID, or a UUID reported for more than one
        card gets no run and fails on its own. Returns None (every built challenge released) when
        the native library fails to produce a cipher: our own fault, not the host's.
        """
        reported_uuids = [str(card.get("uuid", "") or "") for card in cards]
        prepared = CardChallenges(
            per_card=[
                {"card_index": index, "gpu_uuid": "", "ok": False, "reason": "not_reported"}
                for index in range(gpu_count)
            ],
            challenges={},
            reported_uuids=reported_uuids,
        )
        try:
            for index, card in enumerate(cards):
                gpu_uuid = reported_uuids[index]
                prepared.per_card[index]["gpu_uuid"] = gpu_uuid
                if not sized_vram_mb[index]:
                    prepared.per_card[index]["reason"] = "unknown_model"
                    continue
                if not GPU_UUID_PATTERN.match(gpu_uuid):
                    prepared.per_card[index]["reason"] = "uuid_invalid"
                    continue
                if reported_uuids.count(gpu_uuid) > 1:
                    prepared.per_card[index]["reason"] = "uuid_duplicate"
                    continue
                # The executor's library derives the challenge key from the devices its process can
                # see. Pinned to one card that is this card alone, so the challenge is built for a
                # one-card machine carrying this card's UUID and model: only that card can answer it.
                challenge = self.prepare_matmul_challenge(
                    {
                        "gpu": {
                            "count": 1,
                            "details": [
                                {
                                    "name": card.get("name", "") or "",
                                    "uuid": gpu_uuid,
                                    "capacity": sized_vram_mb[index],
                                }
                            ],
                        }
                    },
                    {**default_extra, "all_cards_probe": True, "card_index": index},
                )
                prepared.challenges[index] = challenge
                # A native error yields an empty cipher, which would go out as a bare
                # `--cipher_text ` and read as a failed work-proof on every card. Our own fault, not
                # the host's: skip the probe and let the caller's single-card path decide, as the
                # legacy path does.
                if not challenge.params.cipher_text:
                    logger.warning(
                        _m(
                            "All-cards matmul skipped: cipher text generation failed (native error)",
                            extra=get_extra_info(default_extra),
                        )
                    )
                    prepared.close()
                    return None
        except BaseException:
            prepared.close()
            raise
        return prepared

    async def _probe_all_claimed_cards(
        self,
        ssh_client,
        executor_info,
        default_extra: dict,
        machine_spec: dict,
        vram_budget_mb: int | None = None,
    ) -> AllCardsProbe | None:
        # concurrent per-card work-proof: one independent challenge per claimed card (own seed + own
        # nonce + own key), each run pinned to that card's reported UUID, sized from that card's own
        # reported capacity (`_card_probe_vram_mb`), aggregate timed on the validator against a
        # limit scaled by the number of rounds. A card is verified only when
        # its answer passes `evaluate_matmul_output`, the judgement the single-card run gets.
        # The cards run at the same time, one process per card, so an honest host answers in about
        # one card's time per round. `vram_budget_mb` caps each card's size as it caps the
        # single-card run.
        # Returns None when it does not apply — a single claimed card (legacy/whole-single-GPU path)
        # or no card of a model with a known registry VRAM size (none can be sized safely) — so the
        # caller's single-card path stays authoritative. When only some cards have an unknown model,
        # those cards fail on their own (`unknown_model`) and the others are still proved.
        if not allcards_probe_applies(machine_spec):
            return None
        gpu = machine_spec.get("gpu", {}) or {}
        gpu_count = gpu.get("count", 0) or 0
        cards = (gpu.get("details", []) or [])[:gpu_count]
        sized_vram_mb = [self._card_probe_vram_mb(card) for card in cards]
        if vram_budget_mb:
            sized_vram_mb = [sized and min(sized, vram_budget_mb) for sized in sized_vram_mb]

        script_path = f"{executor_info.root_dir}/src/decrypt_challenge.py"
        prepared = self._prepare_card_challenges(cards, gpu_count, sized_vram_mb, default_extra)
        if prepared is None:
            return None
        per_card = prepared.per_card
        challenges = prepared.challenges
        reported_uuids = prepared.reported_uuids
        try:
            # One SSH channel per card on a single connection, so the fan-out has to stay under the
            # executor's sshd MaxSessions (OpenSSH defaults to 10). A 12-14 card host would otherwise
            # push the tail channels into the ssh_error branch and read as a failed work-proof.
            gate = asyncio.Semaphore(MATMUL_ALLCARDS_MAX_CONCURRENT_CARDS)

            async def run_gated(index: int, challenge: MatmulChallenge) -> CardOutcome:
                async with gate:
                    return await self._run_one_card(
                        ssh_client,
                        executor_info,
                        script_path,
                        index,
                        reported_uuids[index],
                        challenge,
                    )

            start = time.perf_counter()
            outcomes = await asyncio.gather(
                *(run_gated(index, challenge) for index, challenge in challenges.items())
            )
            elapsed = time.perf_counter() - start
        finally:
            prepared.close()

        # Each run returns its own card's outcome and judged result; nothing is shared between runs.
        card_results: dict[int, ValidationResult] = {}
        for outcome in outcomes:
            per_card[outcome.card_index].update(outcome.as_log())
            if outcome.ok:
                card_results[outcome.card_index] = outcome.result

        verified_count = sum(1 for outcome in per_card if outcome["ok"])
        wall_clock_limit = allcards_wall_clock_limit_seconds(len(challenges))
        over_wall_clock = elapsed > wall_clock_limit
        passed = verified_count == gpu_count and not over_wall_clock
        if verified_count != gpu_count:
            failure_reason = (
                f"{gpu_count - verified_count}/{gpu_count} card(s) failed device work-proof"
            )
        elif over_wall_clock:
            failure_reason = (
                f"aggregate wall-clock {elapsed:.1f}s over {wall_clock_limit}s "
                f"(serialisation on fewer real cards than claimed)"
            )
        else:
            failure_reason = ""

        log_extra = {
            **default_extra,
            "gpu_count_claimed": gpu_count,
            "gpu_count_verified": verified_count,
            "gpu_models": sorted({card.get("name", "") or "" for card in cards}),
            "sized_vram_mb": sized_vram_mb,
            "elapsed_seconds": round(elapsed, 3),
            "wall_clock_threshold_seconds": wall_clock_limit,
            "over_wall_clock": over_wall_clock,
            "per_card": per_card,
            "passed": passed,
            "enforced": settings.MATMUL_ALLCARDS_ENFORCEMENT_ENABLED,
        }
        log = logger.info if passed else logger.warning
        log(_m("All-cards GPU work-proof", extra=get_extra_info(log_extra)))

        if any(self._leaves_matmul_running(outcome) for outcome in per_card):
            # asyncssh's timeout only abandons the local wait; a timed-out remote matmul keeps
            # running and holding VRAM. A failed verdict sends this same call on to the fatal
            # single-card matmul, so sweep the orphans first or it OOMs on device 0 — mirrors the
            # legacy path.
            await self._kill_remote_matmul(ssh_client, script_path, log_extra)

        return AllCardsProbe(
            passed=passed,
            failure_reason=failure_reason,
            elapsed_seconds=elapsed,
            over_wall_clock=over_wall_clock,
            per_card=per_card,
            verified_count=verified_count,
            card_results=card_results,
        )

    @staticmethod
    def _leaves_matmul_running(outcome: dict) -> bool:
        # the per-card failures where the remote matmul may still hold VRAM: a timeout abandons only
        # the local wait, and a channel that dies mid-run (ssh_error) leaves the process untouched
        reason = outcome.get("reason") or ""
        return reason == "timeout" or reason.startswith("ssh_error")

    async def _run_one_card(
        self,
        ssh_client,
        executor_info,
        script_path: str,
        index: int,
        gpu_uuid: str,
        challenge: "MatmulChallenge",
    ) -> CardOutcome:
        # run one card's challenge pinned to that card's reported UUID and judge the answer with
        # this challenge's own key. The command sent to the executor is the single-card command with
        # a CUDA_VISIBLE_DEVICES prefix, and never reveals which card/signal disagreed
        # (anti-treadmill). Returns the card's outcome and, when the card is verified, its judged
        # result (the same shape the single-card run returns, metrics included).
        command = (
            f"CUDA_VISIBLE_DEVICES={shlex.quote(gpu_uuid)} "
            f"{executor_info.python_path} {script_path} {challenge.params}"
        )
        run_started = time.perf_counter()
        try:
            result = await ssh_client.run(command, timeout=MATRIX_VERIFY_TIMEOUT_SECONDS)
        except (TimeoutError, asyncssh.TimeoutError):
            return CardOutcome(index, ok=False, reason="timeout")
        except Exception as e:
            return CardOutcome(index, ok=False, reason=f"ssh_error: {e}")
        # This card's own run time on the validator's clock, for the logs only; no verdict reads it.
        elapsed_seconds = round(time.perf_counter() - run_started, 3)

        exit_status = getattr(result, "exit_status", 0)
        stdout = getattr(result, "stdout", "") or ""
        stderr = getattr(result, "stderr", "") or ""
        if exit_status != 0:
            return CardOutcome(
                index,
                ok=False,
                reason=f"exit_status={exit_status}",
                elapsed_seconds=elapsed_seconds,
            )
        if not stdout.strip():
            return CardOutcome(
                index, ok=False, reason="empty_output", elapsed_seconds=elapsed_seconds
            )
        # Output alone proves nothing: the answer has to carry this card's nonce.
        verdict = self.evaluate_matmul_output(
            challenge, stdout=stdout.strip(), stderr=stderr.strip()
        )
        if not verdict.success:
            return CardOutcome(
                index, ok=False, reason="unverified", elapsed_seconds=elapsed_seconds
            )
        return CardOutcome(
            index, ok=True, reason="", elapsed_seconds=elapsed_seconds, result=verdict
        )

    def prepare_matmul_challenge(
        self,
        machine_spec: dict,
        default_extra: dict,
        vram_budget_mb: int | None = None,
    ) -> "MatmulChallenge":
        """Size and encrypt one capability challenge; the caller decides how it reaches the executor.

        The SSH path runs `decrypt_challenge.py {challenge.params}` over the shell; the local path
        (liumd phase 1, `POST /verify`) sends the same four arguments in the intent. Either way
        the output comes back to `evaluate_matmul_output`, which holds the only pass/fail logic, so
        both transports are judged by one function. The returned object owns the generate-side
        seal key (`gen_ptr`): call `close()` once the output has been evaluated.
        """
        # vram_budget_mb (DAH-3011): size the matmul from min(card VRAM, budget) instead of the whole
        # card. Same challenge, seal and UUID check — a wrong/absent GPU or a spoofed UUID still
        # fails; only the "fill the card" capacity proof is deferred. None = today's full-card size.
        gpu_model = ""
        if machine_spec.get("gpu", {}).get("count", 0) > 0:
            details = machine_spec["gpu"].get("details", [])
            if len(details) > 0:
                gpu_model = details[0].get("name", "")

        gpu_details = machine_spec.get("gpu", {}).get("details", [])
        gpu_count = machine_spec.get("gpu", {}).get("count", 0)
        gpu_uuids = ','.join([detail.get('uuid', '') for detail in gpu_details])

        # NOTE: machine_info MUST exactly match what the executor's libdmcompverify
        # reconstructs locally via getGPUInfo() — otherwise the hash-derived AES key
        # will not match and the executor's decrypt will fail. Adding any new field
        # to this JSON (e.g. gpu_capacity_mb) requires a corresponding change in the
        # .so's getGPUInfo(). The Python-side pre-check below uses gpu_capacity_mb
        # as a separate local variable; it is NEVER embedded into machine_info.
        gpu_info = {
            "uuids": gpu_uuids,
            "gpu_count": gpu_count,
            "gpu_model": gpu_model,
        }
        machine_info = json.dumps(gpu_info, sort_keys=True)

        # GPU model<->VRAM consistency is gated earlier in the pipeline by
        # GpuVramPrecheck (before the rented short-circuit), so it is NOT
        # repeated here. gpu_capacity_mb is still needed to size the matmul.
        gpu_capacity_mb = self.get_gpu_memory(machine_spec)
        if vram_budget_mb and gpu_capacity_mb:
            gpu_capacity_mb = min(gpu_capacity_mb, vram_budget_mb)

        verifier_params = VerifierParams()
        verifier_params.generate()
        verifier_params.dim_k = int(self.get_max_matrix_dimensions(gpu_capacity_mb, verifier_params.dim_n))

        # Generate the challenge on a dedicated per-call object (mirrors the legacy
        # encrypt_challenge flow, but its key is retained for the post-transport unseal).
        gen_ptr = None
        try:
            gen_ptr = self.wrapper.DMCompVerify_new(10, 10)
            self.wrapper.setDimension(gen_ptr, verifier_params.dim_n, verifier_params.dim_k)
            self.wrapper.generateChallenge(
                gen_ptr, verifier_params.seed, machine_info, verifier_params.uuid
            )
            verifier_params.cipher_text = self.wrapper.getCipherText(gen_ptr) or ""
        except Exception as e:
            logger.error("Failed encrypt challenge request: %s", str(e))
            verifier_params.cipher_text = ""

        log_extra = {
            **default_extra,
            "dim_n": verifier_params.dim_n,
            "dim_k": verifier_params.dim_k,
            "sized_vram_mb": gpu_capacity_mb,
            "seed": verifier_params.seed,
            "uuid": verifier_params.uuid,
            "cipher_text": verifier_params.cipher_text,
            "machine_info": machine_info,
        }
        return MatmulChallenge(
            params=verifier_params, gen_ptr=gen_ptr, machine_info=machine_info, log_extra=log_extra, service=self
        )

    def evaluate_matmul_output(
        self,
        challenge: "MatmulChallenge",
        *,
        stdout: str,
        stderr: str = "",
    ) -> ValidationResult:
        """Judge the executor's `decrypt_challenge.py` output — the one place that decides.

        `stdout`/`stderr` are the process's, however they travelled (SSH capture or the `/verify`
        result document). The sealed blob is authoritative and is unsealed with THIS challenge's
        generate-side key, so a transport cannot change the verdict.
        """
        verifier_params = challenge.params
        gen_ptr = challenge.gen_ptr
        log_extra = challenge.log_extra
        stdout = stdout.strip()
        stderr = stderr.strip() if stderr else ""

        # Extract the result from stdout. Prefer the combined RESULT_JSON marker
        # (carries uuid + TFLOPS metrics); fall back to the legacy "UUID:" line for
        # backward compatibility with older executors. A malformed RESULT_JSON also
        # falls back to the legacy line rather than failing the check. The library
        # prints many debug lines, so we take the LAST RESULT_JSON match.
        metrics = None
        sealed_blob = ""
        try:
            lines = stdout.splitlines()
            result_json_line = next(
                (line for line in reversed(lines) if line.startswith("RESULT_JSON:")),
                None,
            )
            uuid = ""
            if result_json_line is not None:
                try:
                    parsed = json.loads(result_json_line[len("RESULT_JSON:"):].strip())
                except (ValueError, TypeError):
                    parsed = None
                # Only trust a well-formed object carrying a *string* uuid. Stdout is
                # miner-controlled, so a crafted uuid of list/int/dict type must fall
                # back to the legacy UUID: line rather than erroring out the whole
                # check. (UUID comparison stays fail-closed regardless.)
                if isinstance(parsed, dict) and isinstance(parsed.get("uuid"), str):
                    uuid = parsed["uuid"].strip()
                    metrics = parsed.get("metrics")
                    # The sealed blob (when present) is authoritative; verified below.
                    if isinstance(parsed.get("sealed"), str):
                        sealed_blob = parsed["sealed"]
                else:
                    result_json_line = None  # malformed/unusable → fall through to legacy parse
            if result_json_line is None:
                uuid_line = next((line for line in lines if line.startswith("UUID:")), None)
                uuid = uuid_line.split("UUID:")[1].strip() if uuid_line else ""
        except Exception as e:
            error_msg = f"Failed to extract UUID from stdout: {str(e)}"
            logger.error(_m(error_msg, extra=get_extra_info(log_extra)))
            return ValidationResult(
                success=False,
                expected_uuid=verifier_params.uuid,
                returned_uuid="",
                stdout=stdout,
                stderr=stderr,
                error_message=error_msg
            )

        # Anti-spoof gate: when the executor returns a sealed (authenticated-encrypted)
        # blob, it is AUTHORITATIVE. We decrypt it with OUR generate-side key and take
        # uuid+metrics from inside, so a miner who only edits the (untrusted) Python
        # wrapper cannot forge or inflate the result. If a sealed blob is present but
        # fails authentication, we REJECT — we do not fall back to the miner-controlled
        # plaintext line. Executors that predate sealing send no blob and keep the
        # legacy behavior, so independent validator/executor upgrades stay compatible.
        if getattr(self.wrapper, "_has_sealed", False) and sealed_blob:
            inner = self.wrapper.unsealResult(gen_ptr, sealed_blob)
            inner_obj = None
            if inner:
                try:
                    inner_obj = json.loads(inner)
                except (ValueError, TypeError):
                    inner_obj = None
            if not isinstance(inner_obj, dict) or not isinstance(inner_obj.get("uuid"), str):
                error_msg = "Sealed result failed authentication (tampered/forged executor output)"
                logger.error(_m("Matrix Multiplication Verification Failed", extra=get_extra_info({**log_extra, "error": error_msg})))
                return ValidationResult(
                    success=False,
                    expected_uuid=verifier_params.uuid,
                    returned_uuid="",
                    stdout=stdout,
                    stderr=stderr,
                    error_message=error_msg,
                )
            # Authenticated: these override anything in the plaintext line.
            uuid = inner_obj["uuid"].strip()
            metrics = inner_obj.get("metrics")
        elif getattr(self.wrapper, "_has_sealed", False) and (
            uuid in verifier_params.uuid.split(",")
        ):
            # A seal-aware library got no sealed blob: the uuid and metrics are the executor's plaintext,
            # which its own wrapper can edit, and the metrics reach the published specs. Only a
            # matching plaintext uuid is a real answer that skipped the seal; a probe that never
            # answered (out of memory, `sealed: null`) falls through to the mismatch below, which
            # the capability check can still waive for a busy GPU.
            logger.warning(
                _m(
                    "MATMUL_RESULT_UNSEALED",
                    extra=get_extra_info({**log_extra, "enforced": settings.MATMUL_SEAL_REQUIRED_ENABLED}),
                )
            )
            if settings.MATMUL_SEAL_REQUIRED_ENABLED:
                return ValidationResult(
                    success=False,
                    expected_uuid=verifier_params.uuid,
                    returned_uuid="",
                    stdout=stdout,
                    stderr=stderr,
                    error_message="Unsealed result from a seal-aware verifier",
                )

        try:
            uuid_array = verifier_params.uuid.split(",")
            if uuid in uuid_array:
                logger.info(_m("Matrix Multiplication Verification Succeed", extra=get_extra_info(log_extra)))
                return ValidationResult(
                    success=True,
                    expected_uuid=verifier_params.uuid,
                    returned_uuid=uuid,
                    stdout=stdout,
                    stderr=stderr,
                    metrics=metrics,
                    sealed=self._answer_was_sealed(sealed_blob),
                    dim_n=verifier_params.dim_n,
                    dim_k=verifier_params.dim_k,
                )
            else:
                error_msg = (
                    f"{UUID_MISMATCH_ERROR_PREFIX}: "
                    f"expected '{verifier_params.uuid}', got '{uuid}'"
                )
                logger.error(_m("Matrix Multiplication Verification Failed", extra=get_extra_info({**log_extra, "returned_uuid": uuid, "error": error_msg})))
                return ValidationResult(
                    success=False,
                    expected_uuid=verifier_params.uuid,
                    returned_uuid=uuid,
                    stdout=stdout,
                    stderr=stderr,
                    error_message=error_msg,
                    metrics=metrics,
                    sealed=self._answer_was_sealed(sealed_blob),
                    dim_n=verifier_params.dim_n,
                    dim_k=verifier_params.dim_k,
                )
        except Exception as e:
            error_msg = f"Error during UUID verification: {str(e)}"
            logger.error(_m(error_msg, extra=get_extra_info(log_extra)))
            return ValidationResult(
                success=False,
                expected_uuid=verifier_params.uuid,
                returned_uuid=uuid if 'uuid' in locals() else "",
                stdout=stdout,
                stderr=stderr,
                error_message=error_msg
            )

    def _answer_was_sealed(self, sealed_blob: str) -> bool:
        """True when the judged uuid and metrics came out of the sealed blob. A blob that fails
        authentication never gets this far: `evaluate_matmul_output` rejects it first."""
        return bool(sealed_blob) and bool(getattr(self.wrapper, "_has_sealed", False))

    async def validate_gpu_model_and_process_job(
        self,
        ssh_client,
        executor_info,
        default_extra: dict,
        machine_spec: dict,
        vram_budget_mb: int | None = None,
        first_pass: bool = False,
    ) -> ValidationResult:
        # The SSH transport of the capability matmul: prepare_matmul_challenge → one remote
        # `decrypt_challenge.py` run → evaluate_matmul_output. The local transport (checks/
        # local_verify.py) calls the same two functions around `POST /verify`.
        # DAH-2671 item 3: all-claimed-cards work-proof. Shadow (MATMUL_ALLCARDS_CHECK_ENABLED
        # without enforcement, the default) logs timing + per-card verdict and NEVER changes the
        # returned result; MATMUL_ALLCARDS_ENFORCEMENT_ENABLED fails the validation when fewer cards
        # are verified than reported or the aggregate wall-clock is over. Single-card hosts and
        # hosts with no card of a known model skip it (probe returns None), which also keeps the
        # legacy single-card path below unchanged. `first_pass` is the caller's word that this is a
        # node's first, unscored verification: see `should_run_allcards_probe`.
        # A passing verdict replaces the single-card run: card 0 was just verified with a challenge
        # of its own, sized from its reported capacity as the single-card run sizes it, so its
        # result is returned and card 0 is not run a second time. A failing
        # verdict in shadow leaves the result to the single-card run below, as before.
        if should_run_allcards_probe(first_pass):
            all_cards = await self._probe_all_claimed_cards(
                ssh_client, executor_info, default_extra, machine_spec, vram_budget_mb
            )
            if all_cards is not None and all_cards.passed:
                return all_cards.card_results[0]
            if settings.MATMUL_ALLCARDS_ENFORCEMENT_ENABLED and all_cards is not None:
                return ValidationResult(
                    success=False,
                    error_message=f"All-cards GPU work-proof failed: {all_cards.failure_reason}",
                )
        elif first_pass and settings.MATMUL_ALLCARDS_CHECK_ENABLED:
            logger.info(
                _m(
                    "All-cards GPU work-proof skipped on the first pass (verdict not enforced)",
                    extra=get_extra_info(default_extra),
                )
            )

        # Per-call verifier object: its key MUST survive the SSH round-trip so we can
        # unseal the executor's authenticated response. A shared object would be clobbered
        # by concurrent validations across the `await` below. Freed in `finally`.
        challenge = None
        try:
            script_path = f"{executor_info.root_dir}/src/decrypt_challenge.py"
            challenge = self.prepare_matmul_challenge(machine_spec, default_extra, vram_budget_mb)
            verifier_params = challenge.params
            log_extra = challenge.log_extra

            # Short-circuit if encrypt_challenge returned empty. This happens when
            # the native call raised. No point SSHing an empty cipher to the
            # executor — it would surface as a misleading "UUID mismatch" downstream.
            if not verifier_params.cipher_text:
                error_msg = "Cipher text generation failed (native error)"
                logger.warning(_m(error_msg, extra=get_extra_info(log_extra)))
                return ValidationResult(
                    success=False,
                    expected_uuid=verifier_params.uuid,
                    error_message=error_msg,
                )

            command = f"{executor_info.python_path} {script_path} {verifier_params}"

            logger.info(_m("Matrix Multiplication Python Script Command", extra=get_extra_info(log_extra)))

            # Run the script
            run_started = time.perf_counter()
            try:
                result = await ssh_client.run(command, timeout=MATRIX_VERIFY_TIMEOUT_SECONDS)
            except (TimeoutError, asyncssh.TimeoutError):
                # asyncssh's timeout only abandons the local wait — the remote process keeps
                # running and holding GPU memory, so it must be killed explicitly.
                error_msg = (
                    f"Matrix multiplication timed out after {MATRIX_VERIFY_TIMEOUT_SECONDS}s"
                )
                logger.error(_m(error_msg, extra=get_extra_info(log_extra)))
                await self._kill_remote_matmul(ssh_client, script_path, log_extra)
                return ValidationResult(
                    success=False,
                    expected_uuid=verifier_params.uuid,
                    error_message=error_msg,
                    timed_out=True,
                )
            except Exception as e:
                error_msg = f"Failed to execute SSH command: {str(e)}"
                logger.error(_m(error_msg, extra=get_extra_info(log_extra)))
                return ValidationResult(
                    success=False,
                    expected_uuid=verifier_params.uuid,
                    error_message=error_msg
                )

            logger.info(f"{script_path}: {result}")

            if result is None:
                error_msg = "GPU model validation job failed: result is None"
                logger.error(_m(error_msg, extra=get_extra_info(log_extra)))
                return ValidationResult(
                    success=False,
                    expected_uuid=verifier_params.uuid,
                    error_message=error_msg
                )

            try:
                stdout = result.stdout.strip()
                stderr = result.stderr.strip() if hasattr(result, 'stderr') else ""
            except AttributeError:
                error_msg = "Result object missing stdout attribute"
                logger.error(_m(error_msg, extra=get_extra_info(log_extra)))
                return ValidationResult(
                    success=False,
                    expected_uuid=verifier_params.uuid,
                    error_message=error_msg
                )

            run_seconds = time.perf_counter() - run_started
            judged = self.evaluate_matmul_output(challenge, stdout=stdout, stderr=stderr)
            # The validator's own clock around the remote run: SSH round trip, process start,
            # allocation, the challenge and the node's benchmark together.
            judged.elapsed_seconds = run_seconds
            judged.elapsed_source = ELAPSED_SOURCE_VALIDATOR
            return judged

        except Exception as e:
            error_msg = f"Unexpected error in validate_gpu_model_and_process_job: {str(e)}"
            logger.error(_m(error_msg, extra=default_extra))
            return ValidationResult(
                success=False,
                error_message=error_msg
            )
        finally:
            # Always release the per-call verifier object (it held the seal key).
            if challenge is not None:
                challenge.close()


@dataclass
class MatmulChallenge:
    """One prepared capability challenge and the key that judges its answer (see
    `ValidationService.prepare_matmul_challenge`)."""

    params: VerifierParams
    gen_ptr: object
    machine_info: str
    log_extra: dict
    service: ValidationService

    def close(self) -> None:
        if self.gen_ptr is not None:
            try:
                self.service.wrapper.free(self.gen_ptr)
            except Exception as exc:
                # The native free failing leaks one generator; nothing to roll back, the verdict stands.
                logger.warning(
                    _m(
                        "Matmul challenge generator free failed",
                        extra=get_extra_info({**self.log_extra, "error": str(exc)}),
                    )
                )
            self.gen_ptr = None
