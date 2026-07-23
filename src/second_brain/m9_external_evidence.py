"""Fail-closed intake contracts for future M9 external evidence.

This module deliberately has no default verifier, no artifact writer, no
provider client, and no global configuration access. A caller must supply a
separately trusted verifier for a detached proof before transient external
payloads can become a redacted receipt. The public intake helpers are
low-level proof parsers, not route-batch authority: an operator must bind the
final approval packet, current boundary, registry, and approval reference
before starting a batch. The receipts are evidence only; they never authorize
a rollout, a material action, or an M6 side effect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Any, Callable, Protocol, Sequence

from .canonical import sha256_bytes, sha256_hex
from .errors import AuthorityDeniedError, IntegrityError, SemanticValidationError
from .evaluation import BlindedReviewPacket, GateState
from .jsonio import loads_strict_json


M9_EXTERNAL_EVIDENCE_VERSION = 1
M9_EXTERNAL_EVIDENCE_SCOPE = "external-verifier-bound-project-local"
ROUTE_ATTESTATION_PURPOSE = "route-attestation"
BLINDED_REVIEW_PURPOSE = "blinded-semantic-review"
M9_ROUTE_EVIDENCE_KIND = "m9-route-observations-v1"
M9_REVIEW_EVIDENCE_KIND = "m9-blinded-review-results-v1"

_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9._:-]{2,95}$")
_PROFILE_ALIAS = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_OPAQUE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,191}$")
_MODEL_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,159}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_MAX_EXTERNAL_PAYLOAD_BYTES = 1_048_576
_MAX_DETACHED_PROOF_BYTES = 1_048_576
_MAX_TRUST_ANCHOR_BYTES = 65_536
_MIN_ROUTE_OBSERVATIONS = 50
_MAX_ROUTE_OBSERVATIONS = 500
_MAX_REVIEW_ENTRIES = 1_024
_ALLOWED_EFFORTS = frozenset(("high", "max", "xhigh"))
_PREFERRED_LABELS = frozenset(("A", "B", "TIE"))
_OPENSSH_PROTOCOL = "openssh-detached-proof-v1"
_OPENSSH_NAMESPACE = "pixel-second-brain-m9"
_OPENSSH_TIMEOUT_SECONDS = 10
_ROUTE_APPROVAL_SCOPE = "external-public-read-only-route-attestation"
_ROUTE_APPROVAL_DATA_CLASS = "PUBLIC"
_ROUTE_APPROVAL_EXECUTION_MODE = "read-only-shadow"
_ROUTE_APPROVAL_RETENTION_STATUS = "UNVERIFIABLE_FROM_LOCAL_CONFIGURATION"
_ROUTE_APPROVAL_STOP_CONDITIONS = (
    "ROUTE_MISMATCH",
    "TOOL_ACTIVITY",
    "MUTATION",
    "SECURITY_EVENT",
    "MISSING_SIGNED_OBSERVATION",
)


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise SemanticValidationError(f"{label} is invalid")
    return value


def _require_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise SemanticValidationError(f"{label} is invalid")
    return value


def _require_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise SemanticValidationError(f"{label} is invalid")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as error:
        raise SemanticValidationError(f"{label} is invalid") from error
    return value


def _require_exact_mapping(value: object, keys: frozenset[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise SemanticValidationError(f"{label} is invalid")
    return dict(value)


def _require_external_bytes(value: object, maximum: int, label: str) -> bytes:
    if not isinstance(value, bytes) or not value or len(value) > maximum:
        raise SemanticValidationError(f"{label} is invalid")
    return value


@dataclass(frozen=True)
class ExternalTrustPolicy:
    """A pinned external-verification policy, never proof by itself."""

    policy_version: int
    purpose: str
    authority_id: str
    verifier_protocol: str
    trust_anchor_fingerprint: str
    policy_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not ExternalTrustPolicy:
            raise SemanticValidationError("external trust policy is invalid")
        if self.policy_version != M9_EXTERNAL_EVIDENCE_VERSION or self.purpose not in {
            ROUTE_ATTESTATION_PURPOSE,
            BLINDED_REVIEW_PURPOSE,
        }:
            raise SemanticValidationError("external trust policy identity is invalid")
        _require_identifier(self.authority_id, "external authority")
        if self.verifier_protocol not in {"detached-proof-v1", _OPENSSH_PROTOCOL}:
            raise SemanticValidationError("external verifier protocol is invalid")
        _require_hash(self.trust_anchor_fingerprint, "external trust anchor")
        digest = sha256_hex(self._digest_input())
        if self.policy_digest and self.policy_digest != digest:
            raise SemanticValidationError("external trust policy digest does not match")
        object.__setattr__(self, "policy_digest", digest)

    def _digest_input(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "purpose": self.purpose,
            "authority_id": self.authority_id,
            "verifier_protocol": self.verifier_protocol,
            "trust_anchor_fingerprint": self.trust_anchor_fingerprint,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._digest_input(), "policy_digest": self.policy_digest}

    @classmethod
    def from_value(cls, value: object) -> "ExternalTrustPolicy":
        raw = _require_exact_mapping(
            value,
            frozenset(
                (
                    "policy_version",
                    "purpose",
                    "authority_id",
                    "verifier_protocol",
                    "trust_anchor_fingerprint",
                    "policy_digest",
                )
            ),
            "external trust policy",
        )
        try:
            return cls(**raw)
        except (TypeError, ValueError, SemanticValidationError) as error:
            raise SemanticValidationError("external trust policy is invalid") from error


@dataclass(frozen=True)
class ExternalVerificationRequest:
    """Transient raw evidence handed only to the separately trusted adapter."""

    policy: ExternalTrustPolicy
    payload: bytes
    detached_proof: bytes

    def __post_init__(self) -> None:
        if type(self) is not ExternalVerificationRequest or type(self.policy) is not ExternalTrustPolicy:
            raise SemanticValidationError("external verification request is invalid")
        _require_external_bytes(self.payload, _MAX_EXTERNAL_PAYLOAD_BYTES, "external evidence payload")
        _require_external_bytes(self.detached_proof, _MAX_DETACHED_PROOF_BYTES, "external detached proof")

    @property
    def payload_digest(self) -> str:
        return sha256_bytes(self.payload)

    @property
    def proof_digest(self) -> str:
        return sha256_bytes(self.detached_proof)


@dataclass(frozen=True)
class ExternalVerification:
    """Redacted result emitted only by an external trust adapter."""

    policy_digest: str
    purpose: str
    payload_digest: str
    proof_digest: str
    verifier_id: str
    verified_at: str
    verification_reference_digest: str

    def __post_init__(self) -> None:
        if type(self) is not ExternalVerification:
            raise SemanticValidationError("external verification is invalid")
        for label, value in (
            ("external verification policy", self.policy_digest),
            ("external verification payload", self.payload_digest),
            ("external verification proof", self.proof_digest),
            ("external verification reference", self.verification_reference_digest),
        ):
            _require_hash(value, label)
        if self.purpose not in {ROUTE_ATTESTATION_PURPOSE, BLINDED_REVIEW_PURPOSE}:
            raise SemanticValidationError("external verification purpose is invalid")
        _require_identifier(self.verifier_id, "external verifier")
        _require_timestamp(self.verified_at, "external verification timestamp")

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_digest": self.policy_digest,
            "purpose": self.purpose,
            "payload_digest": self.payload_digest,
            "proof_digest": self.proof_digest,
            "verifier_id": self.verifier_id,
            "verified_at": self.verified_at,
            "verification_reference_digest": self.verification_reference_digest,
        }


class ExternalEvidenceVerifier(Protocol):
    """Adapter boundary for an actual signature/log/human-review verifier."""

    def verify(self, request: ExternalVerificationRequest) -> ExternalVerification:
        """Verify the detached proof against the policy's external trust anchor."""


OpenSshProcessRunner = Callable[[Sequence[str], bytes, Path, int], subprocess.CompletedProcess[bytes]]


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_anchor_bytes(path: Path) -> bytes:
    """Read one small regular public anchor without following a symlink."""

    try:
        before = path.lstat()
    except OSError as error:
        raise AuthorityDeniedError("external trust anchor is unavailable") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > _MAX_TRUST_ANCHOR_BYTES:
        raise AuthorityDeniedError("external trust anchor is unsafe")
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or after.st_size != before.st_size
        ):
            raise AuthorityDeniedError("external trust anchor changed while being read")
        chunks: list[bytes] = []
        remaining = _MAX_TRUST_ANCHOR_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    except AuthorityDeniedError:
        raise
    except OSError as error:
        raise AuthorityDeniedError("external trust anchor is unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not payload or len(payload) != before.st_size or len(payload) > _MAX_TRUST_ANCHOR_BYTES:
        raise AuthorityDeniedError("external trust anchor is unsafe")
    return payload


def validate_openssh_trust_anchor(policy: ExternalTrustPolicy, allowed_signers_path: Path) -> None:
    """Confirm that a supplied public anchor still matches its pinned policy."""

    if type(policy) is not ExternalTrustPolicy or policy.verifier_protocol != _OPENSSH_PROTOCOL:
        raise AuthorityDeniedError("OpenSSH trust policy is not selected")
    if not isinstance(allowed_signers_path, Path) or not allowed_signers_path.is_absolute():
        raise SemanticValidationError("OpenSSH trust anchor path must be absolute")
    if sha256_bytes(_read_anchor_bytes(allowed_signers_path)) != policy.trust_anchor_fingerprint:
        raise IntegrityError("external trust anchor does not match the pinned policy")


def _run_openssh_verify(
    arguments: Sequence[str], payload: bytes, scratch_directory: Path, timeout_seconds: int
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(arguments),
        check=False,
        cwd=scratch_directory,
        input=payload,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout_seconds,
    )


@dataclass(frozen=True)
class OpenSshDetachedProofVerifier:
    """Verify OpenSSH signatures against an explicitly pinned allowed-signers file.

    The allowed-signers file is an intentionally supplied public trust anchor.
    Its exact bytes must hash to the policy fingerprint before it is copied into
    a private temporary directory for the short-lived ``ssh-keygen`` call.
    """

    allowed_signers_path: Path
    ssh_keygen_path: Path
    process_runner: OpenSshProcessRunner = _run_openssh_verify
    clock: Callable[[], str] = _utc_now

    def __post_init__(self) -> None:
        if type(self) is not OpenSshDetachedProofVerifier:
            raise SemanticValidationError("OpenSSH verifier is invalid")
        if not isinstance(self.allowed_signers_path, Path) or not isinstance(self.ssh_keygen_path, Path):
            raise SemanticValidationError("OpenSSH verifier paths are invalid")
        if not self.allowed_signers_path.is_absolute() or not self.ssh_keygen_path.is_absolute():
            raise SemanticValidationError("OpenSSH verifier paths must be absolute")
        if not callable(self.process_runner) or not callable(self.clock):
            raise SemanticValidationError("OpenSSH verifier callbacks are invalid")
        try:
            executable = self.ssh_keygen_path.resolve(strict=True)
        except OSError as error:
            raise SemanticValidationError("OpenSSH executable is unavailable") from error
        if executable.name != "ssh-keygen" or not executable.is_file() or not os.access(executable, os.X_OK):
            raise SemanticValidationError("OpenSSH executable is invalid")
        object.__setattr__(self, "ssh_keygen_path", executable)

    def verify(self, request: ExternalVerificationRequest) -> ExternalVerification:
        if type(request) is not ExternalVerificationRequest:
            raise AuthorityDeniedError("OpenSSH verification request is invalid")
        policy = request.policy
        if policy.verifier_protocol != _OPENSSH_PROTOCOL:
            raise AuthorityDeniedError("OpenSSH verifier protocol is not selected")
        anchor_bytes = _read_anchor_bytes(self.allowed_signers_path)
        if sha256_bytes(anchor_bytes) != policy.trust_anchor_fingerprint:
            raise IntegrityError("external trust anchor does not match the pinned policy")
        try:
            verified_at = self.clock()
            _require_timestamp(verified_at, "OpenSSH verification timestamp")
        except (TypeError, ValueError, SemanticValidationError) as error:
            raise AuthorityDeniedError("OpenSSH verifier clock is invalid") from error
        with tempfile.TemporaryDirectory(prefix="m9-proof-") as temporary:
            scratch = Path(temporary)
            anchor_path = scratch / "allowed-signers"
            proof_path = scratch / "proof.sig"
            _write_private_temp_file(anchor_path, anchor_bytes)
            _write_private_temp_file(proof_path, request.detached_proof)
            arguments = (
                str(self.ssh_keygen_path),
                "-Y",
                "verify",
                "-f",
                str(anchor_path),
                "-I",
                policy.authority_id,
                "-n",
                _OPENSSH_NAMESPACE,
                "-s",
                str(proof_path),
            )
            try:
                completed = self.process_runner(arguments, request.payload, scratch, _OPENSSH_TIMEOUT_SECONDS)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise AuthorityDeniedError("OpenSSH detached proof could not be verified") from error
        if type(completed) is not subprocess.CompletedProcess or completed.returncode != 0:
            raise AuthorityDeniedError("OpenSSH detached proof was rejected")
        return ExternalVerification(
            policy_digest=policy.policy_digest,
            purpose=policy.purpose,
            payload_digest=request.payload_digest,
            proof_digest=request.proof_digest,
            verifier_id="openssh-detached-proof-v1",
            verified_at=verified_at,
            verification_reference_digest=sha256_hex(
                {
                    "protocol": _OPENSSH_PROTOCOL,
                    "authority_id": policy.authority_id,
                    "anchor_fingerprint": policy.trust_anchor_fingerprint,
                    "payload_digest": request.payload_digest,
                    "proof_digest": request.proof_digest,
                }
            ),
        )


def _write_private_temp_file(path: Path, payload: bytes) -> None:
    """Write only short-lived public-anchor/proof bytes for OpenSSH verification."""

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _verify_external_evidence(
    *,
    policy: ExternalTrustPolicy,
    purpose: str,
    payload: object,
    detached_proof: object,
    verifier: object,
) -> ExternalVerification:
    """Reject self-hashed data unless an external verifier binds it to the policy."""

    if type(policy) is not ExternalTrustPolicy or policy.purpose != purpose:
        raise AuthorityDeniedError("external evidence policy does not authorize this purpose")
    request = ExternalVerificationRequest(
        policy=policy,
        payload=_require_external_bytes(payload, _MAX_EXTERNAL_PAYLOAD_BYTES, "external evidence payload"),
        detached_proof=_require_external_bytes(detached_proof, _MAX_DETACHED_PROOF_BYTES, "external detached proof"),
    )
    method = getattr(verifier, "verify", None)
    if not callable(method):
        raise AuthorityDeniedError("external evidence requires a trusted verifier")
    try:
        result = method(request)
    except (AuthorityDeniedError, IntegrityError, SemanticValidationError):
        raise
    except Exception as error:
        raise AuthorityDeniedError("external verifier rejected the evidence") from error
    if type(result) is not ExternalVerification:
        raise AuthorityDeniedError("external verifier returned an invalid result")
    if (
        result.policy_digest != policy.policy_digest
        or result.purpose != purpose
        or result.payload_digest != request.payload_digest
        or result.proof_digest != request.proof_digest
    ):
        raise IntegrityError("external verifier result does not bind the submitted evidence")
    return result


@dataclass(frozen=True)
class RouteAttestationApprovalIntent:
    """A pre-plan, redacted scope commitment for one future route batch.

    The intent is deliberately not an approval.  It breaks the otherwise
    circular dependency between an exact final packet and a plan that must bind
    to it: the plan binds this immutable scope digest, while the final packet
    later binds both the plan and trust policy for explicit user approval.
    """

    intent_version: int
    initial_m9_packet_digest: str
    initial_shadow_receipt_digest: str
    provider_id: str
    minimum_request_count: int = _MIN_ROUTE_OBSERVATIONS
    maximum_request_count: int = _MAX_ROUTE_OBSERVATIONS
    scope: str = _ROUTE_APPROVAL_SCOPE
    data_class: str = _ROUTE_APPROVAL_DATA_CLASS
    execution_mode: str = _ROUTE_APPROVAL_EXECUTION_MODE
    global_mutation: bool = False
    promotion_authorized: bool = False
    upstream_retention_status: str = _ROUTE_APPROVAL_RETENTION_STATUS
    intent_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not RouteAttestationApprovalIntent or self.intent_version != M9_EXTERNAL_EVIDENCE_VERSION:
            raise SemanticValidationError("route approval intent is invalid")
        _require_hash(self.initial_m9_packet_digest, "route approval initial packet")
        _require_hash(self.initial_shadow_receipt_digest, "route approval initial shadow")
        _require_identifier(self.provider_id, "route approval provider")
        if (
            type(self.minimum_request_count) is not int
            or type(self.maximum_request_count) is not int
            or self.minimum_request_count != _MIN_ROUTE_OBSERVATIONS
            or self.maximum_request_count != _MAX_ROUTE_OBSERVATIONS
            or self.scope != _ROUTE_APPROVAL_SCOPE
            or self.data_class != _ROUTE_APPROVAL_DATA_CLASS
            or self.execution_mode != _ROUTE_APPROVAL_EXECUTION_MODE
            or self.global_mutation is not False
            or self.promotion_authorized is not False
            or self.upstream_retention_status != _ROUTE_APPROVAL_RETENTION_STATUS
        ):
            raise SemanticValidationError("route approval intent scope is invalid")
        digest = sha256_hex(self._digest_input())
        if self.intent_digest and self.intent_digest != digest:
            raise SemanticValidationError("route approval intent digest does not match")
        object.__setattr__(self, "intent_digest", digest)

    def _digest_input(self) -> dict[str, object]:
        return {
            "intent_version": self.intent_version,
            "initial_m9_packet_digest": self.initial_m9_packet_digest,
            "initial_shadow_receipt_digest": self.initial_shadow_receipt_digest,
            "provider_id": self.provider_id,
            "minimum_request_count": self.minimum_request_count,
            "maximum_request_count": self.maximum_request_count,
            "scope": self.scope,
            "data_class": self.data_class,
            "execution_mode": self.execution_mode,
            "global_mutation": self.global_mutation,
            "promotion_authorized": self.promotion_authorized,
            "upstream_retention_status": self.upstream_retention_status,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._digest_input(), "intent_digest": self.intent_digest}

    @classmethod
    def from_value(cls, value: object) -> "RouteAttestationApprovalIntent":
        raw = _require_exact_mapping(
            value,
            frozenset(
                (
                    "intent_version",
                    "initial_m9_packet_digest",
                    "initial_shadow_receipt_digest",
                    "provider_id",
                    "minimum_request_count",
                    "maximum_request_count",
                    "scope",
                    "data_class",
                    "execution_mode",
                    "global_mutation",
                    "promotion_authorized",
                    "upstream_retention_status",
                    "intent_digest",
                )
            ),
            "route approval intent",
        )
        try:
            return cls(**raw)
        except (TypeError, ValueError, SemanticValidationError) as error:
            raise SemanticValidationError("route approval intent is invalid") from error


@dataclass(frozen=True)
class RouteExpectation:
    """One future correlated route expectation with no raw correlation retained."""

    correlation_digest: str
    profile_alias: str
    model_identifier_digest: str
    effort: str

    def __post_init__(self) -> None:
        if type(self) is not RouteExpectation:
            raise SemanticValidationError("route expectation is invalid")
        _require_hash(self.correlation_digest, "route correlation digest")
        if not isinstance(self.profile_alias, str) or _PROFILE_ALIAS.fullmatch(self.profile_alias) is None:
            raise SemanticValidationError("route profile alias is invalid")
        _require_hash(self.model_identifier_digest, "route model digest")
        if self.effort not in _ALLOWED_EFFORTS:
            raise SemanticValidationError("route effort is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "correlation_digest": self.correlation_digest,
            "profile_alias": self.profile_alias,
            "model_identifier_digest": self.model_identifier_digest,
            "effort": self.effort,
        }

    @classmethod
    def from_value(cls, value: object) -> "RouteExpectation":
        raw = _require_exact_mapping(
            value,
            frozenset(("correlation_digest", "profile_alias", "model_identifier_digest", "effort")),
            "route expectation",
        )
        try:
            return cls(**raw)
        except (TypeError, ValueError, SemanticValidationError) as error:
            raise SemanticValidationError("route expectation is invalid") from error


def create_route_expectation(
    *, correlation_id: str, profile_alias: str, model_identifier: str, effort: str
) -> RouteExpectation:
    """Commit ephemeral correlation and model identifiers without retaining them."""

    if not isinstance(correlation_id, str) or _OPAQUE_TOKEN.fullmatch(correlation_id) is None:
        raise SemanticValidationError("route correlation identifier is invalid")
    if not isinstance(model_identifier, str) or _MODEL_IDENTIFIER.fullmatch(model_identifier) is None:
        raise SemanticValidationError("route model identifier is invalid")
    return RouteExpectation(
        correlation_digest=sha256_hex({"correlation_id": correlation_id}),
        profile_alias=profile_alias,
        model_identifier_digest=sha256_bytes(model_identifier.encode("utf-8")),
        effort=effort,
    )


@dataclass(frozen=True)
class RouteAttestationPlan:
    """A new, correlation-bound live-shadow plan; not a current rollout approval."""

    plan_version: int
    approval_intent_digest: str
    profile_registry_digest: str
    provider_id: str
    expectations: tuple[RouteExpectation, ...]
    plan_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not RouteAttestationPlan or self.plan_version != M9_EXTERNAL_EVIDENCE_VERSION:
            raise SemanticValidationError("route attestation plan is invalid")
        _require_hash(self.approval_intent_digest, "route approval intent")
        _require_hash(self.profile_registry_digest, "route profile registry")
        _require_identifier(self.provider_id, "route provider")
        expectations = tuple(self.expectations)
        if not _MIN_ROUTE_OBSERVATIONS <= len(expectations) <= _MAX_ROUTE_OBSERVATIONS:
            raise SemanticValidationError("route attestation count is invalid")
        if any(type(item) is not RouteExpectation for item in expectations):
            raise SemanticValidationError("route attestation expectations are invalid")
        if len({item.correlation_digest for item in expectations}) != len(expectations):
            raise SemanticValidationError("route attestation correlations are duplicated")
        object.__setattr__(self, "expectations", expectations)
        digest = sha256_hex(self._digest_input())
        if self.plan_digest and self.plan_digest != digest:
            raise SemanticValidationError("route attestation plan digest does not match")
        object.__setattr__(self, "plan_digest", digest)

    def _digest_input(self) -> dict[str, object]:
        return {
            "plan_version": self.plan_version,
            "approval_intent_digest": self.approval_intent_digest,
            "profile_registry_digest": self.profile_registry_digest,
            "provider_id": self.provider_id,
            "expectations": [item.to_dict() for item in self.expectations],
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._digest_input(), "plan_digest": self.plan_digest}

    @classmethod
    def from_value(cls, value: object) -> "RouteAttestationPlan":
        raw = _require_exact_mapping(
            value,
            frozenset(
                (
                    "plan_version",
                    "approval_intent_digest",
                    "profile_registry_digest",
                    "provider_id",
                    "expectations",
                    "plan_digest",
                )
            ),
            "route attestation plan",
        )
        if type(raw["expectations"]) is not list:
            raise SemanticValidationError("route attestation plan expectations are invalid")
        try:
            return cls(
                plan_version=raw["plan_version"],
                approval_intent_digest=raw["approval_intent_digest"],
                profile_registry_digest=raw["profile_registry_digest"],
                provider_id=raw["provider_id"],
                expectations=tuple(RouteExpectation.from_value(item) for item in raw["expectations"]),
                plan_digest=raw["plan_digest"],
            )
        except (TypeError, ValueError, SemanticValidationError) as error:
            raise SemanticValidationError("route attestation plan is invalid") from error


def _route_approval_request(
    *,
    approval_intent_digest: str,
    route_plan_digest: str,
    trust_policy_digest: str,
    trust_authority_id: str,
    provider_id: str,
    expected_request_count: int,
) -> str:
    """Return the exact, redacted user-facing request for one external batch."""

    return (
        "Authorize only one M9 external PUBLIC read-only route-attestation batch "
        f"for provider {provider_id}, {expected_request_count} correlated requests, "
        f"intent {approval_intent_digest}, plan {route_plan_digest}, trust policy "
        f"{trust_policy_digest}, and authority {trust_authority_id}. Permit no global mutation, capability execution, memory "
        "write, promotion, opt-in, expansion, default activation, soak, or rollback. "
        "Stop on route mismatch, tool activity, mutation, security event, or missing "
        "signed observation; upstream retention remains unverifiable from local configuration."
    )


@dataclass(frozen=True)
class RouteAttestationApprovalPacket:
    """The exact redacted packet a user can approve after plan and policy exist."""

    packet_version: int
    approval_intent_digest: str
    initial_m9_packet_digest: str
    initial_shadow_receipt_digest: str
    route_plan_digest: str
    trust_policy_digest: str
    trust_authority_id: str
    verifier_protocol: str
    provider_id: str
    profile_registry_digest: str
    expected_request_count: int
    approval_request: str
    scope: str = _ROUTE_APPROVAL_SCOPE
    data_class: str = _ROUTE_APPROVAL_DATA_CLASS
    execution_mode: str = _ROUTE_APPROVAL_EXECUTION_MODE
    global_mutation: bool = False
    promotion_authorized: bool = False
    upstream_retention_status: str = _ROUTE_APPROVAL_RETENTION_STATUS
    stop_conditions: tuple[str, ...] = _ROUTE_APPROVAL_STOP_CONDITIONS
    packet_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not RouteAttestationApprovalPacket or self.packet_version != M9_EXTERNAL_EVIDENCE_VERSION:
            raise SemanticValidationError("route approval packet is invalid")
        for label, value in (
            ("route approval intent", self.approval_intent_digest),
            ("route approval initial packet", self.initial_m9_packet_digest),
            ("route approval initial shadow", self.initial_shadow_receipt_digest),
            ("route approval plan", self.route_plan_digest),
            ("route approval policy", self.trust_policy_digest),
            ("route approval profile registry", self.profile_registry_digest),
        ):
            _require_hash(value, label)
        _require_identifier(self.trust_authority_id, "route approval trust authority")
        _require_identifier(self.provider_id, "route approval provider")
        if (
            type(self.expected_request_count) is not int
            or not _MIN_ROUTE_OBSERVATIONS <= self.expected_request_count <= _MAX_ROUTE_OBSERVATIONS
            or self.scope != _ROUTE_APPROVAL_SCOPE
            or self.data_class != _ROUTE_APPROVAL_DATA_CLASS
            or self.execution_mode != _ROUTE_APPROVAL_EXECUTION_MODE
            or self.global_mutation is not False
            or self.promotion_authorized is not False
            or self.upstream_retention_status != _ROUTE_APPROVAL_RETENTION_STATUS
            or tuple(self.stop_conditions) != _ROUTE_APPROVAL_STOP_CONDITIONS
            or self.verifier_protocol != _OPENSSH_PROTOCOL
        ):
            raise SemanticValidationError("route approval packet scope is invalid")
        expected_request = _route_approval_request(
            approval_intent_digest=self.approval_intent_digest,
            route_plan_digest=self.route_plan_digest,
            trust_policy_digest=self.trust_policy_digest,
            trust_authority_id=self.trust_authority_id,
            provider_id=self.provider_id,
            expected_request_count=self.expected_request_count,
        )
        if self.approval_request != expected_request:
            raise SemanticValidationError("route approval request does not match its packet")
        digest = sha256_hex(self._digest_input())
        if self.packet_digest and self.packet_digest != digest:
            raise SemanticValidationError("route approval packet digest does not match")
        object.__setattr__(self, "packet_digest", digest)

    def _digest_input(self) -> dict[str, object]:
        return {
            "packet_version": self.packet_version,
            "approval_intent_digest": self.approval_intent_digest,
            "initial_m9_packet_digest": self.initial_m9_packet_digest,
            "initial_shadow_receipt_digest": self.initial_shadow_receipt_digest,
            "route_plan_digest": self.route_plan_digest,
            "trust_policy_digest": self.trust_policy_digest,
            "trust_authority_id": self.trust_authority_id,
            "verifier_protocol": self.verifier_protocol,
            "provider_id": self.provider_id,
            "profile_registry_digest": self.profile_registry_digest,
            "expected_request_count": self.expected_request_count,
            "approval_request": self.approval_request,
            "scope": self.scope,
            "data_class": self.data_class,
            "execution_mode": self.execution_mode,
            "global_mutation": self.global_mutation,
            "promotion_authorized": self.promotion_authorized,
            "upstream_retention_status": self.upstream_retention_status,
            "stop_conditions": list(self.stop_conditions),
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._digest_input(), "packet_digest": self.packet_digest}

    @classmethod
    def from_value(cls, value: object) -> "RouteAttestationApprovalPacket":
        raw = _require_exact_mapping(
            value,
            frozenset(
                (
                    "packet_version",
                    "approval_intent_digest",
                    "initial_m9_packet_digest",
                    "initial_shadow_receipt_digest",
                    "route_plan_digest",
                    "trust_policy_digest",
                    "trust_authority_id",
                    "verifier_protocol",
                    "provider_id",
                    "profile_registry_digest",
                    "expected_request_count",
                    "approval_request",
                    "scope",
                    "data_class",
                    "execution_mode",
                    "global_mutation",
                    "promotion_authorized",
                    "upstream_retention_status",
                    "stop_conditions",
                    "packet_digest",
                )
            ),
            "route approval packet",
        )
        if type(raw["stop_conditions"]) is not list:
            raise SemanticValidationError("route approval packet stop conditions are invalid")
        try:
            return cls(**{**raw, "stop_conditions": tuple(raw["stop_conditions"])})
        except (TypeError, ValueError, SemanticValidationError) as error:
            raise SemanticValidationError("route approval packet is invalid") from error


def build_route_attestation_approval_packet(
    *,
    intent: RouteAttestationApprovalIntent,
    plan: RouteAttestationPlan,
    policy: ExternalTrustPolicy,
) -> RouteAttestationApprovalPacket:
    """Bind an immutable intent, route plan, and trust policy into one packet."""

    if (
        type(intent) is not RouteAttestationApprovalIntent
        or type(plan) is not RouteAttestationPlan
        or type(policy) is not ExternalTrustPolicy
    ):
        raise SemanticValidationError("route approval inputs are invalid")
    if (
        intent.intent_digest != plan.approval_intent_digest
        or intent.provider_id != plan.provider_id
        or policy.purpose != ROUTE_ATTESTATION_PURPOSE
        or policy.verifier_protocol != _OPENSSH_PROTOCOL
        or not intent.minimum_request_count <= len(plan.expectations) <= intent.maximum_request_count
    ):
        raise IntegrityError("route approval inputs do not share one exact scope")
    return RouteAttestationApprovalPacket(
        packet_version=M9_EXTERNAL_EVIDENCE_VERSION,
        approval_intent_digest=intent.intent_digest,
        initial_m9_packet_digest=intent.initial_m9_packet_digest,
        initial_shadow_receipt_digest=intent.initial_shadow_receipt_digest,
        route_plan_digest=plan.plan_digest,
        trust_policy_digest=policy.policy_digest,
        trust_authority_id=policy.authority_id,
        verifier_protocol=policy.verifier_protocol,
        provider_id=plan.provider_id,
        profile_registry_digest=plan.profile_registry_digest,
        expected_request_count=len(plan.expectations),
        approval_request=_route_approval_request(
            approval_intent_digest=intent.intent_digest,
            route_plan_digest=plan.plan_digest,
            trust_policy_digest=policy.policy_digest,
            trust_authority_id=policy.authority_id,
            provider_id=plan.provider_id,
            expected_request_count=len(plan.expectations),
        ),
    )


def validate_route_attestation_approval_packet(
    *,
    packet: RouteAttestationApprovalPacket,
    intent: RouteAttestationApprovalIntent,
    plan: RouteAttestationPlan,
    policy: ExternalTrustPolicy,
) -> None:
    """Reject a packet unless every immutable input recreates it exactly."""

    if type(packet) is not RouteAttestationApprovalPacket:
        raise SemanticValidationError("route approval packet is invalid")
    expected = build_route_attestation_approval_packet(intent=intent, plan=plan, policy=policy)
    if packet.packet_digest != expected.packet_digest:
        raise IntegrityError("route approval packet does not bind its exact inputs")


@dataclass(frozen=True)
class RouteAttestationReceipt:
    """Redacted external route-evidence result that cannot authorize promotion."""

    plan_digest: str
    verification: ExternalVerification
    expected_count: int
    observed_count: int
    match_count: int
    mismatch_count: int
    attestation_state: str
    live_attested: bool
    promotion_authorized: bool
    evidence_scope: str = M9_EXTERNAL_EVIDENCE_SCOPE
    receipt_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not RouteAttestationReceipt or type(self.verification) is not ExternalVerification:
            raise SemanticValidationError("route attestation receipt is invalid")
        _require_hash(self.plan_digest, "route attestation plan")
        for label, value in (
            ("route expected count", self.expected_count),
            ("route observed count", self.observed_count),
            ("route match count", self.match_count),
            ("route mismatch count", self.mismatch_count),
        ):
            if type(value) is not int or value < 0 or value > _MAX_ROUTE_OBSERVATIONS:
                raise SemanticValidationError(f"{label} is invalid")
        if self.expected_count < _MIN_ROUTE_OBSERVATIONS or self.observed_count != self.expected_count:
            raise SemanticValidationError("route receipt count binding is invalid")
        if self.match_count + self.mismatch_count != self.expected_count:
            raise SemanticValidationError("route receipt outcome count is invalid")
        expected_state = "MATCH" if self.mismatch_count == 0 else "MISMATCH"
        if self.attestation_state != expected_state or self.live_attested is not (expected_state == "MATCH"):
            raise SemanticValidationError("route receipt attestation state is invalid")
        if self.promotion_authorized is not False or self.evidence_scope != M9_EXTERNAL_EVIDENCE_SCOPE:
            raise SemanticValidationError("route receipt authority boundary is invalid")
        digest = sha256_hex(self._digest_input())
        if self.receipt_digest and self.receipt_digest != digest:
            raise SemanticValidationError("route attestation receipt digest does not match")
        object.__setattr__(self, "receipt_digest", digest)

    def _digest_input(self) -> dict[str, object]:
        return {
            "plan_digest": self.plan_digest,
            "verification": self.verification.to_dict(),
            "expected_count": self.expected_count,
            "observed_count": self.observed_count,
            "match_count": self.match_count,
            "mismatch_count": self.mismatch_count,
            "attestation_state": self.attestation_state,
            "live_attested": self.live_attested,
            "promotion_authorized": self.promotion_authorized,
            "evidence_scope": self.evidence_scope,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._digest_input(), "receipt_digest": self.receipt_digest}


def _load_route_observations(payload: bytes, plan: RouteAttestationPlan) -> tuple[tuple[str, str, str], ...]:
    try:
        decoded = loads_strict_json(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise SemanticValidationError("route observation payload is invalid") from error
    raw = _require_exact_mapping(
        decoded,
        frozenset(("evidence_kind", "plan_digest", "provider_id", "observations")),
        "route observation payload",
    )
    if (
        raw["evidence_kind"] != M9_ROUTE_EVIDENCE_KIND
        or raw["plan_digest"] != plan.plan_digest
        or raw["provider_id"] != plan.provider_id
    ):
        raise SemanticValidationError("route observation binding is invalid")
    if type(raw["observations"]) is not list or len(raw["observations"]) != len(plan.expectations):
        raise SemanticValidationError("route observation count is invalid")
    observations: list[tuple[str, str, str]] = []
    for item in raw["observations"]:
        observation = _require_exact_mapping(
            item,
            frozenset(("correlation_id", "model_identifier", "effort")),
            "route observation",
        )
        correlation = observation["correlation_id"]
        model = observation["model_identifier"]
        effort = observation["effort"]
        if (
            not isinstance(correlation, str)
            or _OPAQUE_TOKEN.fullmatch(correlation) is None
            or not isinstance(model, str)
            or _MODEL_IDENTIFIER.fullmatch(model) is None
            or effort not in _ALLOWED_EFFORTS
        ):
            raise SemanticValidationError("route observation is invalid")
        observations.append((correlation, model, effort))
    return tuple(observations)


def attest_external_routes(
    *,
    plan: RouteAttestationPlan,
    payload: bytes,
    detached_proof: bytes,
    policy: ExternalTrustPolicy,
    verifier: ExternalEvidenceVerifier,
) -> RouteAttestationReceipt:
    """Parse proof-verified observations after an approved batch is complete.

    This low-level helper deliberately does not read an approval packet or
    global state. The official CLI performs those authority checks before it
    invokes this parser; its receipt remains non-promoting in every case.
    """

    if type(plan) is not RouteAttestationPlan:
        raise SemanticValidationError("route attestation plan is invalid")
    verification = _verify_external_evidence(
        policy=policy,
        purpose=ROUTE_ATTESTATION_PURPOSE,
        payload=payload,
        detached_proof=detached_proof,
        verifier=verifier,
    )
    observations = _load_route_observations(payload, plan)
    expected_by_correlation = {item.correlation_digest: item for item in plan.expectations}
    observed_correlations: set[str] = set()
    mismatch_count = 0
    for correlation, model, effort in observations:
        correlation_digest = sha256_hex({"correlation_id": correlation})
        if correlation_digest in observed_correlations or correlation_digest not in expected_by_correlation:
            raise IntegrityError("route observations are not a one-to-one plan match")
        observed_correlations.add(correlation_digest)
        expected = expected_by_correlation[correlation_digest]
        if sha256_bytes(model.encode("utf-8")) != expected.model_identifier_digest or effort != expected.effort:
            mismatch_count += 1
    if len(observed_correlations) != len(expected_by_correlation):
        raise IntegrityError("route observations do not cover the plan")
    expected_count = len(plan.expectations)
    return RouteAttestationReceipt(
        plan_digest=plan.plan_digest,
        verification=verification,
        expected_count=expected_count,
        observed_count=len(observations),
        match_count=expected_count - mismatch_count,
        mismatch_count=mismatch_count,
        attestation_state="MATCH" if mismatch_count == 0 else "MISMATCH",
        live_attested=mismatch_count == 0,
        promotion_authorized=False,
    )


@dataclass(frozen=True)
class BlindedReviewIntakePlan:
    """A digest-bound intake plan that never exposes the candidate-label mapping."""

    packet_digest: str
    dataset_manifest_digest: str
    label_assignment_digest: str
    case_reference_digests: tuple[str, ...]
    plan_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not BlindedReviewIntakePlan:
            raise SemanticValidationError("blinded review intake plan is invalid")
        for label, value in (
            ("blinded review packet", self.packet_digest),
            ("blinded review dataset", self.dataset_manifest_digest),
            ("blinded review assignment", self.label_assignment_digest),
        ):
            _require_hash(value, label)
        references = tuple(self.case_reference_digests)
        if not references or len(references) > _MAX_REVIEW_ENTRIES:
            raise SemanticValidationError("blinded review references are invalid")
        if len(set(references)) != len(references):
            raise SemanticValidationError("blinded review references are duplicated")
        for reference in references:
            _require_hash(reference, "blinded review case reference")
        object.__setattr__(self, "case_reference_digests", references)
        digest = sha256_hex(self._digest_input())
        if self.plan_digest and self.plan_digest != digest:
            raise SemanticValidationError("blinded review intake plan digest does not match")
        object.__setattr__(self, "plan_digest", digest)

    def _digest_input(self) -> dict[str, object]:
        return {
            "packet_digest": self.packet_digest,
            "dataset_manifest_digest": self.dataset_manifest_digest,
            "label_assignment_digest": self.label_assignment_digest,
            "case_reference_digests": list(self.case_reference_digests),
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._digest_input(), "plan_digest": self.plan_digest}

    @classmethod
    def from_value(cls, value: object) -> "BlindedReviewIntakePlan":
        raw = _require_exact_mapping(
            value,
            frozenset(
                (
                    "packet_digest",
                    "dataset_manifest_digest",
                    "label_assignment_digest",
                    "case_reference_digests",
                    "plan_digest",
                )
            ),
            "blinded review intake plan",
        )
        if type(raw["case_reference_digests"]) is not list:
            raise SemanticValidationError("blinded review intake plan references are invalid")
        try:
            return cls(
                packet_digest=raw["packet_digest"],
                dataset_manifest_digest=raw["dataset_manifest_digest"],
                label_assignment_digest=raw["label_assignment_digest"],
                case_reference_digests=tuple(raw["case_reference_digests"]),
                plan_digest=raw["plan_digest"],
            )
        except (TypeError, ValueError, SemanticValidationError) as error:
            raise SemanticValidationError("blinded review intake plan is invalid") from error


def build_blinded_review_intake_plan(packet: BlindedReviewPacket) -> BlindedReviewIntakePlan:
    """Bind intake to the exact NOT_RUN packet without exposing its label mapping."""

    if type(packet) is not BlindedReviewPacket or packet.status is not GateState.NOT_RUN:
        raise SemanticValidationError("blinded review packet is not eligible for external intake")
    return BlindedReviewIntakePlan(
        packet_digest=packet.packet_digest,
        dataset_manifest_digest=packet.dataset_manifest_digest,
        label_assignment_digest=packet.label_assignment_digest,
        case_reference_digests=tuple(entry[0] for entry in packet.entries),
    )


def blinded_review_packet_from_value(value: object) -> BlindedReviewPacket:
    """Rebuild one closed redacted review packet without changing M8's evaluator API."""

    raw = _require_exact_mapping(
        value,
        frozenset(
            (
                "packet_version",
                "dataset_manifest_digest",
                "seed",
                "label_assignment_digest",
                "entries",
                "status",
                "packet_digest",
            )
        ),
        "blinded review packet",
    )
    if type(raw["entries"]) is not list:
        raise SemanticValidationError("blinded review packet entries are invalid")
    entries: list[tuple[str, str, str, str]] = []
    for item in raw["entries"]:
        entry = _require_exact_mapping(
            item,
            frozenset(
                (
                    "case_reference_digest",
                    "rubric_id",
                    "label_a_evidence_digest",
                    "label_b_evidence_digest",
                )
            ),
            "blinded review packet entry",
        )
        entries.append(
            (
                entry["case_reference_digest"],
                entry["rubric_id"],
                entry["label_a_evidence_digest"],
                entry["label_b_evidence_digest"],
            )
        )
    try:
        return BlindedReviewPacket(
            packet_version=raw["packet_version"],
            dataset_manifest_digest=raw["dataset_manifest_digest"],
            seed=raw["seed"],
            label_assignment_digest=raw["label_assignment_digest"],
            entries=tuple(entries),
            status=GateState(raw["status"]),
            packet_digest=raw["packet_digest"],
        )
    except (KeyError, TypeError, ValueError, SemanticValidationError) as error:
        raise SemanticValidationError("blinded review packet is invalid") from error


@dataclass(frozen=True)
class BlindedReviewReceipt:
    """Redacted proof-verified review capture pending separate authorized adjudication."""

    plan_digest: str
    verification: ExternalVerification
    reviewed_case_count: int
    label_a_preferred_count: int
    label_b_preferred_count: int
    tie_count: int
    high_risk_failure_count: int
    label_a_score_total: int
    label_b_score_total: int
    capture_status: str
    semantic_non_inferiority_status: str
    promotion_authorized: bool
    candidate_label_mapping_retained: bool
    evidence_scope: str = M9_EXTERNAL_EVIDENCE_SCOPE
    receipt_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not BlindedReviewReceipt or type(self.verification) is not ExternalVerification:
            raise SemanticValidationError("blinded review receipt is invalid")
        _require_hash(self.plan_digest, "blinded review plan")
        counts = (
            self.reviewed_case_count,
            self.label_a_preferred_count,
            self.label_b_preferred_count,
            self.tie_count,
            self.high_risk_failure_count,
            self.label_a_score_total,
            self.label_b_score_total,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise SemanticValidationError("blinded review receipt count is invalid")
        if self.reviewed_case_count == 0 or self.reviewed_case_count > _MAX_REVIEW_ENTRIES:
            raise SemanticValidationError("blinded review receipt count is invalid")
        if self.label_a_preferred_count + self.label_b_preferred_count + self.tie_count != self.reviewed_case_count:
            raise SemanticValidationError("blinded review preference count is invalid")
        if self.high_risk_failure_count > self.reviewed_case_count:
            raise SemanticValidationError("blinded review high-risk count is invalid")
        if self.label_a_score_total > self.reviewed_case_count * 4 or self.label_b_score_total > self.reviewed_case_count * 4:
            raise SemanticValidationError("blinded review score total is invalid")
        if (
            self.capture_status != "CAPTURED"
            or self.semantic_non_inferiority_status != "BLOCKED_AUTHORIZED_ADJUDICATION_REQUIRED"
            or self.promotion_authorized is not False
            or self.candidate_label_mapping_retained is not False
            or self.evidence_scope != M9_EXTERNAL_EVIDENCE_SCOPE
        ):
            raise SemanticValidationError("blinded review receipt authority boundary is invalid")
        digest = sha256_hex(self._digest_input())
        if self.receipt_digest and self.receipt_digest != digest:
            raise SemanticValidationError("blinded review receipt digest does not match")
        object.__setattr__(self, "receipt_digest", digest)

    def _digest_input(self) -> dict[str, object]:
        return {
            "plan_digest": self.plan_digest,
            "verification": self.verification.to_dict(),
            "reviewed_case_count": self.reviewed_case_count,
            "label_a_preferred_count": self.label_a_preferred_count,
            "label_b_preferred_count": self.label_b_preferred_count,
            "tie_count": self.tie_count,
            "high_risk_failure_count": self.high_risk_failure_count,
            "label_a_score_total": self.label_a_score_total,
            "label_b_score_total": self.label_b_score_total,
            "capture_status": self.capture_status,
            "semantic_non_inferiority_status": self.semantic_non_inferiority_status,
            "promotion_authorized": self.promotion_authorized,
            "candidate_label_mapping_retained": self.candidate_label_mapping_retained,
            "evidence_scope": self.evidence_scope,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._digest_input(), "receipt_digest": self.receipt_digest}


def _load_blinded_review_results(
    payload: bytes, plan: BlindedReviewIntakePlan
) -> tuple[tuple[str, str, int, int, bool], ...]:
    try:
        decoded = loads_strict_json(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise SemanticValidationError("blinded review payload is invalid") from error
    raw = _require_exact_mapping(
        decoded,
        frozenset(("evidence_kind", "plan_digest", "review_packet_digest", "entries")),
        "blinded review payload",
    )
    if (
        raw["evidence_kind"] != M9_REVIEW_EVIDENCE_KIND
        or raw["plan_digest"] != plan.plan_digest
        or raw["review_packet_digest"] != plan.packet_digest
        or type(raw["entries"]) is not list
        or len(raw["entries"]) != len(plan.case_reference_digests)
    ):
        raise SemanticValidationError("blinded review payload binding is invalid")
    entries: list[tuple[str, str, int, int, bool]] = []
    for item in raw["entries"]:
        entry = _require_exact_mapping(
            item,
            frozenset(("case_reference_digest", "preferred_label", "label_a_score", "label_b_score", "high_risk_failure")),
            "blinded review result",
        )
        reference = _require_hash(entry["case_reference_digest"], "blinded review result reference")
        label = entry["preferred_label"]
        score_a = entry["label_a_score"]
        score_b = entry["label_b_score"]
        high_risk = entry["high_risk_failure"]
        if (
            label not in _PREFERRED_LABELS
            or type(score_a) is not int
            or type(score_b) is not int
            or not 0 <= score_a <= 4
            or not 0 <= score_b <= 4
            or type(high_risk) is not bool
        ):
            raise SemanticValidationError("blinded review result is invalid")
        entries.append((reference, label, score_a, score_b, high_risk))
    return tuple(entries)


def capture_blinded_review_results(
    *,
    plan: BlindedReviewIntakePlan,
    payload: bytes,
    detached_proof: bytes,
    policy: ExternalTrustPolicy,
    verifier: ExternalEvidenceVerifier,
) -> BlindedReviewReceipt:
    """Capture a verified review after the caller enforces its intake authority.

    This parser never adjudicates candidate identity or supplies rollout
    authority, even when a proof and review plan are valid.
    """

    if type(plan) is not BlindedReviewIntakePlan:
        raise SemanticValidationError("blinded review intake plan is invalid")
    verification = _verify_external_evidence(
        policy=policy,
        purpose=BLINDED_REVIEW_PURPOSE,
        payload=payload,
        detached_proof=detached_proof,
        verifier=verifier,
    )
    entries = _load_blinded_review_results(payload, plan)
    expected_references = set(plan.case_reference_digests)
    received_references = [entry[0] for entry in entries]
    if len(set(received_references)) != len(received_references) or set(received_references) != expected_references:
        raise IntegrityError("blinded review results do not cover the plan")
    label_a_preferred = sum(entry[1] == "A" for entry in entries)
    label_b_preferred = sum(entry[1] == "B" for entry in entries)
    ties = sum(entry[1] == "TIE" for entry in entries)
    high_risk = sum(entry[4] for entry in entries)
    return BlindedReviewReceipt(
        plan_digest=plan.plan_digest,
        verification=verification,
        reviewed_case_count=len(entries),
        label_a_preferred_count=label_a_preferred,
        label_b_preferred_count=label_b_preferred,
        tie_count=ties,
        high_risk_failure_count=high_risk,
        label_a_score_total=sum(entry[2] for entry in entries),
        label_b_score_total=sum(entry[3] for entry in entries),
        capture_status="CAPTURED",
        semantic_non_inferiority_status="BLOCKED_AUTHORIZED_ADJUDICATION_REQUIRED",
        promotion_authorized=False,
        candidate_label_mapping_retained=False,
    )


def promotion_allowed(receipt: object) -> bool:
    """Keep both future evidence receipts outside every rollout/material authority."""

    del receipt
    return False
