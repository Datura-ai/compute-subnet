from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import secrets
import shlex
from functools import cache
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from core.config import settings

from ..messages import GpuAttestationIdentityMessages as Msg, render_message
from ..pipeline import CheckResult, Context
from .banned_provider import has_live_rental, reported_gpu_uuids

logger = logging.getLogger(__name__)

# "NVIDIA Device Identity CA", the self-signed P-384 root every Hopper/Blackwell device certificate chains to.
# Published by NVIDIA as verifier_device_root.pem:
# https://github.com/NVIDIA/nvtrust/blob/main/guest_tools/gpu_verifiers/local_gpu_verifier/src/verifier/certs/verifier_device_root.pem
NVIDIA_DEVICE_ROOT_PEM = Path(__file__).resolve().parents[2] / "assets" / "nvidia_device_identity_ca.pem"

NONCE_BYTES = 32
# The report is the SPDM 1.1 GET_MEASUREMENTS request (version, code, param1, param2, nonce[32], slot) followed
# by the response; the response ends in a raw r||s ECDSA P-384 signature over everything before it.
SPDM_REQUEST_NONCE = slice(4, 4 + NONCE_BYTES)
SPDM_REQUEST_LENGTH = 37
SIGNATURE_BYTES = 96
# Root, "NVIDIA <chip> Identity", "NVIDIA <chip> Provisioner ICA": the cert NVIDIA's provisioner signs at
# manufacture is the 4th from the root ("GH100 A01 GSP BROM"), keyed by the GPU's fused identity key. The
# leaf below it is derived from the firmware measurements, so it changes with firmware and is not an identity.
DEVICE_CERT_FROM_ROOT = 4

ATTEST_MARKER = "--gpu-attest--"
# Runs in the executor container (python3 + libnvidia-ml.so.1 ship there: NVIDIA_DRIVER_CAPABILITIES=all).
# Struct layouts are nvml.h's nvmlConfComputeGpuCertificate_t / nvmlConfComputeGpuAttestationReport_t (R535+).
# With Confidential Computing off NVIDIA's own tools refuse to attest, so expect a non-zero rc there.
_ATTEST_CODE = f"""
import base64, ctypes, json, sys
n = bytes.fromhex(sys.argv[1]); out = []; b64 = lambda a, k: base64.b64encode(bytes(a[:k])).decode()
class C(ctypes.Structure):
    _fields_ = [("cs", ctypes.c_uint), ("acs", ctypes.c_uint), ("c", ctypes.c_ubyte * 0x1000), ("ac", ctypes.c_ubyte * 0x1400)]
class R(ctypes.Structure):
    _fields_ = [("cec", ctypes.c_uint), ("rs", ctypes.c_uint), ("cecs", ctypes.c_uint), ("n", ctypes.c_ubyte * 32),
                ("r", ctypes.c_ubyte * 0x2000), ("cecr", ctypes.c_ubyte * 0x1000)]
try:
    lib = ctypes.CDLL("libnvidia-ml.so.1"); assert lib.nvmlInit_v2() == 0
    cnt = ctypes.c_uint(); lib.nvmlDeviceGetCount_v2(ctypes.byref(cnt))
    for i in range(cnt.value):
        h = ctypes.c_void_p(); u = ctypes.create_string_buffer(96); g = {{}}; out.append(g)
        try:
            lib.nvmlDeviceGetHandleByIndex_v2(i, ctypes.byref(h)); lib.nvmlDeviceGetUUID(h, u, 96); g["uuid"] = u.value.decode()
            c = C(); g["cert_rc"] = lib.nvmlDeviceGetConfComputeGpuCertificate(h, ctypes.byref(c))
            if g["cert_rc"] == 0:
                r = R(); ctypes.memmove(r.n, n, 32); g["report_rc"] = lib.nvmlDeviceGetConfComputeGpuAttestationReport(h, ctypes.byref(r))
                g["chain"] = b64(c.ac, c.acs)
                if g["report_rc"] == 0: g["report"] = b64(r.r, r.rs)
        except Exception as e:
            g["error"] = repr(e)[:200]
except Exception as e:
    out.append({{"error": repr(e)[:200]}})
print("{ATTEST_MARKER}" + json.dumps(out))
"""
ATTEST_TIMEOUT_SECONDS = 60
KEY_UUID = "gpu_identity:uuid"  # GPU UUID -> device identity
KEY_IDENTITY = "gpu_identity:id"  # device identity -> GPU UUID, across executors and hotkeys
KEY_TTL_SECONDS = 180 * 24 * 3600


class AttestationInvalid(ValueError):
    """The evidence is present but does not verify: a forged or replayed report, or a chain not from NVIDIA."""


def build_attest_command(nonce: bytes) -> str:
    return f"python3 -c {shlex.quote(_ATTEST_CODE)} {nonce.hex()}"


@cache
def nvidia_root() -> x509.Certificate:
    return x509.load_pem_x509_certificate(NVIDIA_DEVICE_ROOT_PEM.read_bytes())


def verify_gpu_evidence(chain_pem: bytes, report: bytes, nonce: bytes, root: x509.Certificate) -> dict:
    """Verify one GPU's evidence off the host; return its stable identity.

    The chain must end in `root` (compared by key, not by the host's copy), every cert must be signed by the
    next, the report must carry our nonce and be signed by the chain's leaf key. Raises AttestationInvalid.
    """
    try:
        chain = x509.load_pem_x509_certificates(chain_pem)
    except ValueError as exc:
        raise AttestationInvalid(f"chain_unparsable: {exc}") from exc
    if len(chain) < DEVICE_CERT_FROM_ROOT:
        raise AttestationInvalid(f"chain_too_short: {len(chain)}")
    spki = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    if chain[-1].public_key().public_bytes(*spki) != root.public_key().public_bytes(*spki):
        raise AttestationInvalid("chain_not_nvidia_root")
    try:
        for cert, issuer in zip(chain, chain[1:]):
            cert.verify_directly_issued_by(issuer)
        chain[-1].verify_directly_issued_by(root)
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise AttestationInvalid(f"chain_signature: {exc!r}"[:200]) from exc

    if len(report) <= SPDM_REQUEST_LENGTH + SIGNATURE_BYTES:
        raise AttestationInvalid(f"report_too_short: {len(report)}")
    if report[SPDM_REQUEST_NONCE] != nonce:
        raise AttestationInvalid("report_nonce_mismatch")
    sig = report[-SIGNATURE_BYTES:]
    half = SIGNATURE_BYTES // 2
    der = encode_dss_signature(int.from_bytes(sig[:half], "big"), int.from_bytes(sig[half:], "big"))
    leaf_key = chain[0].public_key()
    if not isinstance(leaf_key, ec.EllipticCurvePublicKey):
        raise AttestationInvalid("leaf_key_not_ec")
    try:
        leaf_key.verify(der, report[:-SIGNATURE_BYTES], ec.ECDSA(hashes.SHA384()))
    except InvalidSignature as exc:
        raise AttestationInvalid("report_signature") from exc

    device = chain[-DEVICE_CERT_FROM_ROOT]
    serial = device.subject.get_attributes_for_oid(x509.oid.NameOID.SERIAL_NUMBER)
    common_name = device.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
    return {
        "identity": hashlib.sha256(device.public_key().public_bytes(*spki)).hexdigest()[:32],
        "device_serial": serial[0].value if serial else None,
        "device_cn": common_name[0].value if common_name else None,
    }


def parse_attest_output(stdout: str) -> list[dict]:
    _, _, payload = (stdout or "").rpartition(ATTEST_MARKER)
    try:
        rows = json.loads(payload.strip().splitlines()[0]) if payload.strip() else []
    except (ValueError, IndexError):
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _text(raw) -> str | None:
    return raw.decode() if isinstance(raw, bytes) else raw


class GpuAttestationIdentityCheck:
    """Pin each GPU to the identity NVIDIA signed into the card (DAH-4551).

    The UUIDs the scrape reports come from NVML on the host, which a root provider can fake (a shim
    nvidia-smi, a preloaded libnvidia-ml). On Hopper and Blackwell the card holds an NVIDIA-provisioned
    identity key: the validator sends a fresh nonce, the card signs a report over it, and the validator checks
    the chain to NVIDIA's pinned root and the signature itself. The host can still choose which UUID it pairs
    with a card, so the pairing is pinned both ways in Redis: a UUID may not change identity, an identity may
    not change UUID (on any executor or hotkey), and two UUIDs of one node may not present the same card.

    With Confidential Computing off NVML refuses to attest, so on most bare-metal hosts this logs
    GPU_ATTESTATION_UNAVAILABLE and passes. Shadow by default: findings are warning events and the node
    passes. Under GPU_ATTESTATION_IDENTITY_ENFORCEMENT_ENABLED a mismatch or forged evidence fails the node
    (not fatal with a live rental, as HardwareIdentityCheck). Unavailable evidence never fails it, except a
    UUID that attested before and now does not (a downgrade), which counts as a mismatch.
    """

    check_id = "gpu.validate.attestation_identity"
    fatal = True

    async def run(self, ctx: Context) -> CheckResult:
        reported = sorted(set(reported_gpu_uuids(ctx)))
        nonce = secrets.token_bytes(NONCE_BYTES)
        rows: list[dict] = []
        if ctx.ssh is not None and reported:
            try:
                res = await asyncio.wait_for(ctx.ssh.run(build_attest_command(nonce)), timeout=ATTEST_TIMEOUT_SECONDS)
                rows = parse_attest_output(res.stdout)
            except Exception as exc:
                logger.warning("gpu attestation read failed: %s", repr(exc)[:500], extra=ctx.default_extra)

        verified: dict[str, dict] = {}
        unavailable: dict[str, str] = {}
        invalid: dict[str, str] = {}
        by_uuid = {row.get("uuid"): row for row in rows if row.get("uuid")}
        for gpu_uuid in reported:
            row = by_uuid.get(gpu_uuid)
            if row is None or not row.get("chain") or not row.get("report"):
                unavailable[gpu_uuid] = (
                    "not_listed" if row is None
                    else row.get("error") or f"cert_rc={row.get('cert_rc')},report_rc={row.get('report_rc')}"
                )
                continue
            try:
                verified[gpu_uuid] = verify_gpu_evidence(
                    base64.b64decode(row["chain"]), base64.b64decode(row["report"]), nonce, nvidia_root()
                )
            except (AttestationInvalid, binascii.Error) as exc:
                invalid[gpu_uuid] = str(exc)[:200]

        findings: list[str] = []
        if invalid:
            findings.append("attestation_invalid")
        identities = [v["identity"] for v in verified.values()]
        if len(set(identities)) != len(identities):
            findings.append("one_card_under_several_uuids")

        moved: dict[str, str] = {}
        try:
            redis = ctx.services.redis
            for gpu_uuid in reported:
                pinned = _text(await redis.get(f"{KEY_UUID}:{gpu_uuid}"))
                identity = verified.get(gpu_uuid, {}).get("identity")
                if pinned and identity and pinned != identity:
                    moved[gpu_uuid] = "uuid_changed_identity"
                elif pinned and gpu_uuid in unavailable:
                    moved[gpu_uuid] = "attested_before_unavailable_now"
                if identity:
                    owner = _text(await redis.get(f"{KEY_IDENTITY}:{identity}"))
                    if owner and owner != gpu_uuid:
                        moved[gpu_uuid] = f"identity_was_under:{owner}"
            if moved:
                findings.append("identity_moved")
            if not findings:  # a node under a finding cannot rewrite its pins
                for gpu_uuid, v in verified.items():
                    await redis.set(f"{KEY_UUID}:{gpu_uuid}", v["identity"], ex=KEY_TTL_SECONDS)
                    await redis.set(f"{KEY_IDENTITY}:{v['identity']}", gpu_uuid, ex=KEY_TTL_SECONDS)
        except Exception:
            logger.warning("gpu identity record unavailable", exc_info=True, extra=ctx.default_extra)

        enforce = settings.GPU_ATTESTATION_IDENTITY_ENFORCEMENT_ENABLED
        what = {
            "verified": verified,
            "unavailable": unavailable,
            "invalid": invalid,
            "moved": moved,
            "findings": findings,
            "enforced": enforce,
        }
        if findings:
            event = render_message(
                Msg.MISMATCH,
                ctx=ctx,
                check_id=self.check_id,
                impact=None if enforce else "Shadow observation only: score was NOT changed",
                what=what,
            )
            return CheckResult(passed=not enforce, event=event, fatal=not has_live_rental(ctx))
        template = Msg.OK if verified and not unavailable else Msg.UNAVAILABLE
        return CheckResult(passed=True, event=render_message(template, ctx=ctx, check_id=self.check_id, what=what))
