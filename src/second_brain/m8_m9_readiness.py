"""Redacted, source-bound M8/M9 readiness checkpoint.

This joins the typed local M8 receipts with the immutable M9 initial-shadow
receipt.  It never reads global Codex files, sends a provider request, or turns
the M9 transport result into route attestation.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any, Mapping

from .canonical import canonical_jcs_bytes, sha256_hex
from .errors import IntegrityError, SemanticValidationError
from .evaluation import GateState, load_serialized_receipt, verify_m8_local_evidence
from .global_rollout import load_m9_packet
from .jsonio import load_strict_json
from .live_shadow import M9_SHADOW_REQUEST_COUNT, verify_initial_shadow_receipt
from .workspace import repository_root, resolve_workspace_path


M8_M9_READINESS_SCHEMA_VERSION = 1
M8_M9_READINESS_WRITER_VERSION = "m8-m9-readiness/1"
# Keep the prior source-bound checkpoint immutable when M8 receipt inputs change.
M8_M9_READINESS_RECEIPT_NAME = "m8-m9-readiness-v2.json"
M8_M9_READINESS_SCOPE = "cross-milestone-redacted-evidence"

_HASH = re.compile(r"^[0-9a-f]{64}$")
_GATE_ID = re.compile(r"^gate:[a-z][a-z0-9._-]{2,95}$")
_REASON = re.compile(r"^[A-Z][A-Z0-9_]{2,95}$")
_M8_ARTIFACT_COUNT = 8
_MAX_RECEIPT_BYTES = 1_048_576

_MANUAL_GATES = (
    ("gate:authenticated-route-telemetry", "MISSING_AUTHENTICATED_ROUTE_TELEMETRY"),
    ("gate:semantic-non-inferiority", "AUTHORIZED_BLINDED_SEMANTIC_COMPARISON_REQUIRED"),
    ("gate:provider-task-performance", "PROVIDER_TASK_PERFORMANCE_CONTEXT_REQUIRED"),
    ("gate:production-m4-context", "PRODUCTION_SCALE_M4_CONTEXT_REQUIRED"),
    ("gate:personal-canary-soak", "NEW_EXACT_OPT_IN_APPROVAL_AND_SOAK_REQUIRED"),
    ("gate:global-rollback", "APPROVED_GLOBAL_ROLLBACK_REQUIRED"),
)


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise SemanticValidationError(f"{label} is invalid")
    return value


def _require_exact_mapping(value: object, keys: frozenset[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise SemanticValidationError(f"{label} is invalid")
    return dict(value)


def _read_artifact(relative_path: str) -> dict[str, Any]:
    """Load a bounded regular artifact without following a symlink."""

    target = resolve_workspace_path(relative_path)
    artifacts_root = repository_root() / "artifacts"
    try:
        target.relative_to(artifacts_root)
    except ValueError as error:
        raise SemanticValidationError("readiness artifact path escapes artifacts") from error
    try:
        metadata = target.lstat()
    except FileNotFoundError as error:
        raise SemanticValidationError("readiness artifact is unavailable") from error
    if target.is_symlink() or not target.is_file() or metadata.st_size > _MAX_RECEIPT_BYTES:
        raise SemanticValidationError("readiness artifact is unsafe")
    try:
        payload = load_strict_json(target)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise SemanticValidationError("readiness artifact is invalid") from error
    if type(payload) is not dict:
        raise SemanticValidationError("readiness artifact is invalid")
    return payload


def _source_binding(
    *,
    m8_verification_output_digest: str,
    m8_release_output_digest: str,
    m9_packet_digest: str,
    m9_shadow_receipt_digest: str,
) -> str:
    for label, value in (
        ("M8 verification digest", m8_verification_output_digest),
        ("M8 release digest", m8_release_output_digest),
        ("M9 packet digest", m9_packet_digest),
        ("M9 shadow digest", m9_shadow_receipt_digest),
    ):
        _require_hash(value, label)
    return sha256_hex(
        {
            "m8_verification_output_digest": m8_verification_output_digest,
            "m8_release_output_digest": m8_release_output_digest,
            "m9_packet_digest": m9_packet_digest,
            "m9_shadow_receipt_digest": m9_shadow_receipt_digest,
        }
    )


def build_m8_m9_readiness_checkpoint() -> dict[str, object]:
    """Build the current checkpoint from verified project-local evidence only."""

    m8_verification = verify_m8_local_evidence()
    if (
        m8_verification.state is not GateState.PASS
        or m8_verification.artifact_count != _M8_ARTIFACT_COUNT
        or m8_verification.release_status != "BLOCKED"
    ):
        raise IntegrityError("M8 local evidence is not the expected blocked checkpoint")
    m8_verification_payload = load_serialized_receipt("m8-verification.json")
    m8_release_payload = load_serialized_receipt("m8-release-report.json")
    if (
        m8_verification_payload.get("status") != "PASS"
        or m8_verification_payload.get("release_status") != "BLOCKED"
        or m8_release_payload.get("release_status") != "BLOCKED"
        or m8_release_payload.get("promotion_status") != "M9_EVIDENCE_REQUIRED"
    ):
        raise IntegrityError("M8 typed receipts are not the expected blocked checkpoint")

    packet = load_m9_packet()
    shadow = _read_artifact("artifacts/m9-initial-shadow-report.json")
    verify_initial_shadow_receipt(shadow)
    execution = shadow["execution"]
    route = shadow["route_attestation"]
    if (
        shadow["packet_digest"] != packet["packet_digest"]
        or shadow["shadow_execution_status"] != "PASS"
        or shadow["promotion_status"] != "BLOCKED_MISSING_AUTHENTICATED_ROUTE_TELEMETRY"
        or route != {
            "status": "MISSING_TELEMETRY",
            "live_attested": False,
            "reason_code": "CODEX_CLI_EVENTS_LACK_AUTHENTICATED_ROUTE_OBSERVATION",
        }
        or execution["requested_shadow_count"] != M9_SHADOW_REQUEST_COUNT
        or execution["executed_shadow_count"] != M9_SHADOW_REQUEST_COUNT
        or execution["passed_shadow_count"] != M9_SHADOW_REQUEST_COUNT
        or execution["tool_call_count"] != 0
    ):
        raise IntegrityError("M9 initial shadow is not the expected unattested checkpoint")

    m8_verification_digest = sha256_hex(m8_verification_payload)
    m8_release_digest = sha256_hex(m8_release_payload)
    source_binding_digest = _source_binding(
        m8_verification_output_digest=m8_verification_digest,
        m8_release_output_digest=m8_release_digest,
        m9_packet_digest=packet["packet_digest"],
        m9_shadow_receipt_digest=shadow["receipt_digest"],
    )
    payload: dict[str, object] = {
        "schema_version": M8_M9_READINESS_SCHEMA_VERSION,
        "checkpoint_kind": "m8-m9-readiness",
        "writer_version": M8_M9_READINESS_WRITER_VERSION,
        "evidence_scope": M8_M9_READINESS_SCOPE,
        "checkpoint_status": "BLOCKED",
        "promotion_status": "BLOCKED_MISSING_AUTHENTICATED_ROUTE_TELEMETRY",
        "source_binding": {
            "m8_verification_output_digest": m8_verification_digest,
            "m8_release_output_digest": m8_release_digest,
            "m9_packet_digest": packet["packet_digest"],
            "m9_shadow_receipt_digest": shadow["receipt_digest"],
            "source_binding_digest": source_binding_digest,
        },
        "m8_local_evidence": {
            "artifact_count": _M8_ARTIFACT_COUNT,
            "verification_state": "PASS",
            "release_status": "BLOCKED",
        },
        "m9_initial_shadow": {
            "requested_shadow_count": M9_SHADOW_REQUEST_COUNT,
            "executed_shadow_count": M9_SHADOW_REQUEST_COUNT,
            "passed_shadow_count": M9_SHADOW_REQUEST_COUNT,
            "tool_call_count": 0,
            "shadow_execution_status": "PASS",
            "route_attestation_status": "MISSING_TELEMETRY",
            "live_attested": False,
        },
        "gates": [
            {
                "gate_id": "gate:m8-local-evidence",
                "state": "PASS",
                "reason_code": "EIGHT_TYPED_LOCAL_ARTIFACTS_VERIFIED",
            },
            {
                "gate_id": "gate:m9-initial-shadow-transport",
                "state": "PASS",
                "reason_code": "FIFTY_PUBLIC_READ_ONLY_SHADOWS_VERIFIED",
            },
            *[
                {"gate_id": gate_id, "state": "BLOCKED", "reason_code": reason_code}
                for gate_id, reason_code in _MANUAL_GATES
            ],
        ],
        "boundary_assertions": {
            "global_mutation_performed_by_checkpoint": False,
            "provider_network_request_started_by_checkpoint": False,
            "raw_prompt_or_response_retained": False,
        },
    }
    payload["checkpoint_digest"] = sha256_hex(payload)
    validate_m8_m9_readiness_checkpoint(payload)
    return payload


def validate_m8_m9_readiness_checkpoint(payload: object) -> None:
    """Validate the closed checkpoint shape without making any source reads."""

    raw = _require_exact_mapping(
        payload,
        frozenset(
            (
                "schema_version",
                "checkpoint_kind",
                "writer_version",
                "evidence_scope",
                "checkpoint_status",
                "promotion_status",
                "source_binding",
                "m8_local_evidence",
                "m9_initial_shadow",
                "gates",
                "boundary_assertions",
                "checkpoint_digest",
            )
        ),
        "M8/M9 readiness checkpoint",
    )
    if (
        raw["schema_version"] != M8_M9_READINESS_SCHEMA_VERSION
        or raw["checkpoint_kind"] != "m8-m9-readiness"
        or raw["writer_version"] != M8_M9_READINESS_WRITER_VERSION
        or raw["evidence_scope"] != M8_M9_READINESS_SCOPE
        or raw["checkpoint_status"] != "BLOCKED"
        or raw["promotion_status"] != "BLOCKED_MISSING_AUTHENTICATED_ROUTE_TELEMETRY"
    ):
        raise SemanticValidationError("M8/M9 readiness checkpoint identity is invalid")

    source = _require_exact_mapping(
        raw["source_binding"],
        frozenset(
            (
                "m8_verification_output_digest",
                "m8_release_output_digest",
                "m9_packet_digest",
                "m9_shadow_receipt_digest",
                "source_binding_digest",
            )
        ),
        "M8/M9 readiness source binding",
    )
    expected_source_binding = _source_binding(
        m8_verification_output_digest=source["m8_verification_output_digest"],
        m8_release_output_digest=source["m8_release_output_digest"],
        m9_packet_digest=source["m9_packet_digest"],
        m9_shadow_receipt_digest=source["m9_shadow_receipt_digest"],
    )
    if source["source_binding_digest"] != expected_source_binding:
        raise IntegrityError("M8/M9 readiness source binding does not match")

    m8 = _require_exact_mapping(
        raw["m8_local_evidence"],
        frozenset(("artifact_count", "verification_state", "release_status")),
        "M8/M9 readiness M8 evidence",
    )
    if (
        type(m8["artifact_count"]) is not int
        or m8["artifact_count"] != _M8_ARTIFACT_COUNT
        or m8["verification_state"] != "PASS"
        or m8["release_status"] != "BLOCKED"
    ):
        raise SemanticValidationError("M8/M9 readiness M8 evidence is invalid")

    shadow = _require_exact_mapping(
        raw["m9_initial_shadow"],
        frozenset(
            (
                "requested_shadow_count",
                "executed_shadow_count",
                "passed_shadow_count",
                "tool_call_count",
                "shadow_execution_status",
                "route_attestation_status",
                "live_attested",
            )
        ),
        "M8/M9 readiness M9 shadow",
    )
    if (
        any(
            type(shadow[key]) is not int or shadow[key] != M9_SHADOW_REQUEST_COUNT
            for key in ("requested_shadow_count", "executed_shadow_count", "passed_shadow_count")
        )
        or type(shadow["tool_call_count"]) is not int
        or shadow["tool_call_count"] != 0
        or shadow["shadow_execution_status"] != "PASS"
        or shadow["route_attestation_status"] != "MISSING_TELEMETRY"
        or shadow["live_attested"] is not False
    ):
        raise SemanticValidationError("M8/M9 readiness M9 shadow is invalid")

    gates = raw["gates"]
    expected_gates = (
        ("gate:m8-local-evidence", "PASS", "EIGHT_TYPED_LOCAL_ARTIFACTS_VERIFIED"),
        ("gate:m9-initial-shadow-transport", "PASS", "FIFTY_PUBLIC_READ_ONLY_SHADOWS_VERIFIED"),
        *((gate_id, "BLOCKED", reason_code) for gate_id, reason_code in _MANUAL_GATES),
    )
    if type(gates) is not list or len(gates) != len(expected_gates):
        raise SemanticValidationError("M8/M9 readiness gates are invalid")
    for item, expected in zip(gates, expected_gates):
        gate = _require_exact_mapping(item, frozenset(("gate_id", "state", "reason_code")), "M8/M9 readiness gate")
        if (
            not isinstance(gate["gate_id"], str)
            or _GATE_ID.fullmatch(gate["gate_id"]) is None
            or gate["gate_id"] != expected[0]
            or gate["state"] != expected[1]
            or not isinstance(gate["reason_code"], str)
            or _REASON.fullmatch(gate["reason_code"]) is None
            or gate["reason_code"] != expected[2]
        ):
            raise SemanticValidationError("M8/M9 readiness gate is invalid")

    boundaries = _require_exact_mapping(
        raw["boundary_assertions"],
        frozenset(
            (
                "global_mutation_performed_by_checkpoint",
                "provider_network_request_started_by_checkpoint",
                "raw_prompt_or_response_retained",
            )
        ),
        "M8/M9 readiness boundaries",
    )
    if any(value is not False for value in boundaries.values()):
        raise SemanticValidationError("M8/M9 readiness boundaries are invalid")

    _require_hash(raw["checkpoint_digest"], "M8/M9 readiness digest")
    unsigned = {key: value for key, value in raw.items() if key != "checkpoint_digest"}
    if sha256_hex(unsigned) != raw["checkpoint_digest"]:
        raise IntegrityError("M8/M9 readiness digest does not match")


def verify_current_m8_m9_readiness_checkpoint(
    relative_path: str = f"artifacts/{M8_M9_READINESS_RECEIPT_NAME}",
) -> dict[str, object]:
    """Recompute current source evidence and reject a stale readiness checkpoint."""

    payload = _read_artifact(relative_path)
    validate_m8_m9_readiness_checkpoint(payload)
    expected = build_m8_m9_readiness_checkpoint()
    if payload != expected:
        raise IntegrityError("M8/M9 readiness checkpoint does not match current evidence")
    return dict(payload)


def serialize_m8_m9_readiness_checkpoint(payload: Mapping[str, object]) -> bytes:
    validate_m8_m9_readiness_checkpoint(dict(payload))
    return canonical_jcs_bytes(dict(payload)) + b"\n"


def write_m8_m9_readiness_checkpoint(
    payload: Mapping[str, object],
    relative_path: str = f"artifacts/{M8_M9_READINESS_RECEIPT_NAME}",
) -> Path:
    """Create an immutable readiness checkpoint without replacing foreign evidence."""

    serialized = serialize_m8_m9_readiness_checkpoint(payload)
    target = resolve_workspace_path(relative_path)
    artifacts_root = repository_root() / "artifacts"
    if artifacts_root.is_symlink() or not artifacts_root.is_dir():
        raise IntegrityError("M8/M9 readiness artifact root is unsafe")
    try:
        target.resolve(strict=False).relative_to(artifacts_root.resolve(strict=True))
    except ValueError as error:
        raise IntegrityError("M8/M9 readiness artifact path escapes artifacts") from error
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file():
            raise IntegrityError("M8/M9 readiness artifact target is unsafe")
        if target.read_bytes() != serialized:
            raise IntegrityError("M8/M9 readiness artifact already exists with different content")
        return target
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor: int | None = None
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if target.is_file() and not target.is_symlink() and target.read_bytes() == serialized:
            return target
        raise IntegrityError("M8/M9 readiness artifact already exists with different content")
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise
    return target
