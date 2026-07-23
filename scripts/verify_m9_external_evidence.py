#!/usr/bin/env python3
"""Verify a future proof-bound M9 route or blinded-review evidence payload.

This utility is intentionally read-only. It prints only a redacted receipt and
never starts a provider request, changes global Codex state, or writes an
evidence artifact. It rejects the consumed initial M9 packet for a new route
batch, so historical 50-shadow output cannot be retrofitted as attestation.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat

from second_brain.errors import AuthorityDeniedError, ContractError, IntegrityError, SemanticValidationError
from second_brain.global_rollout import load_m9_packet
from second_brain.jsonio import load_strict_json
from second_brain.live_shadow import verify_initial_shadow_receipt
from second_brain.m9_external_evidence import (
    BlindedReviewIntakePlan,
    ExternalTrustPolicy,
    OpenSshDetachedProofVerifier,
    RouteAttestationApprovalIntent,
    RouteAttestationApprovalPacket,
    RouteAttestationPlan,
    attest_external_routes,
    capture_blinded_review_results,
    validate_route_attestation_approval_packet,
)
from second_brain.profiles import load_profile_registry, registry_digest
from second_brain.workspace import repository_root, resolve_workspace_path


_MAX_PAYLOAD_BYTES = 1_048_576
_SYSTEM_SSH_KEYGEN = Path("/usr/bin/ssh-keygen")


def _read_project_json(relative_path: str, label: str) -> object:
    target = resolve_workspace_path(relative_path)
    root = repository_root()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise SemanticValidationError(f"{label} escapes the project") from error
    try:
        metadata = target.lstat()
    except OSError as error:
        raise SemanticValidationError(f"{label} is unavailable") from error
    if target.is_symlink() or not target.is_file() or metadata.st_size <= 0 or metadata.st_size > _MAX_PAYLOAD_BYTES:
        raise SemanticValidationError(f"{label} is unsafe")
    return load_strict_json(target)


def _read_explicit_regular_file(raw_path: str, label: str) -> bytes:
    path = Path(raw_path)
    if not path.is_absolute():
        raise SemanticValidationError(f"{label} path must be absolute")
    try:
        before = path.lstat()
    except OSError as error:
        raise SemanticValidationError(f"{label} is unavailable") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > _MAX_PAYLOAD_BYTES:
        raise SemanticValidationError(f"{label} is unsafe")
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or after.st_size != before.st_size
        ):
            raise IntegrityError(f"{label} changed while being read")
        chunks: list[bytes] = []
        remaining = _MAX_PAYLOAD_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not payload or len(payload) != before.st_size or len(payload) > _MAX_PAYLOAD_BYTES:
        raise SemanticValidationError(f"{label} is unsafe")
    return payload


def _require_authorization(reference: str, binding_digest: str, label: str) -> None:
    if not isinstance(reference, str) or binding_digest not in reference:
        raise AuthorityDeniedError(f"{label} requires an explicit bound authorization reference")


def _trusted_system_ssh_keygen() -> Path:
    """Use the system verifier only, never a caller-supplied executable.

    Raw external evidence is passed to this process on stdin.  Requiring the
    root-owned, non-writable system binary prevents a CLI option from turning
    verification into arbitrary-process execution or exfiltration.
    """

    try:
        metadata = _SYSTEM_SSH_KEYGEN.lstat()
    except OSError as error:
        raise AuthorityDeniedError("system OpenSSH verifier is unavailable") from error
    if (
        _SYSTEM_SSH_KEYGEN.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not metadata.st_mode & stat.S_IXUSR
    ):
        raise AuthorityDeniedError("system OpenSSH verifier is unsafe")
    return _SYSTEM_SSH_KEYGEN


def _validate_current_initial_boundary(intent: RouteAttestationApprovalIntent) -> None:
    current_packet = load_m9_packet()
    shadow = _read_project_json("artifacts/m9-initial-shadow-report.json", "initial M9 shadow receipt")
    if type(shadow) is not dict:
        raise SemanticValidationError("initial M9 shadow receipt is invalid")
    verify_initial_shadow_receipt(shadow)
    if (
        intent.initial_m9_packet_digest != current_packet["packet_digest"]
        or intent.initial_shadow_receipt_digest != shadow["receipt_digest"]
        or shadow["packet_digest"] != current_packet["packet_digest"]
        or shadow["shadow_execution_status"] != "PASS"
        or shadow["promotion_status"] != "BLOCKED_MISSING_AUTHENTICATED_ROUTE_TELEMETRY"
        or shadow["configured_provider"] != intent.provider_id
    ):
        raise IntegrityError("route approval intent is stale against the current initial M9 boundary")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("route", "review"))
    parser.add_argument("--plan", required=True, help="Project-relative redacted intake plan JSON")
    parser.add_argument("--policy", required=True, help="Project-relative public trust-policy JSON")
    parser.add_argument("--approval-intent", help="Project-relative route approval intent JSON (required for route)")
    parser.add_argument("--approval-packet", help="Project-relative exact route approval packet JSON (required for route)")
    parser.add_argument("--payload", required=True, help="Absolute path to transient external evidence JSON")
    parser.add_argument("--proof", required=True, help="Absolute path to detached proof")
    parser.add_argument("--allowed-signers", required=True, help="Absolute path to pinned OpenSSH allowed-signers file")
    parser.add_argument("--authorization-reference", required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        policy = ExternalTrustPolicy.from_value(_read_project_json(arguments.policy, "external trust policy"))
        if arguments.kind == "route":
            if not arguments.approval_intent or not arguments.approval_packet:
                raise AuthorityDeniedError("route verification requires an exact approval intent and packet")
            intent = RouteAttestationApprovalIntent.from_value(
                _read_project_json(arguments.approval_intent, "route approval intent")
            )
            plan = RouteAttestationPlan.from_value(_read_project_json(arguments.plan, "route attestation plan"))
            approval_packet = RouteAttestationApprovalPacket.from_value(
                _read_project_json(arguments.approval_packet, "route approval packet")
            )
            validate_route_attestation_approval_packet(
                packet=approval_packet,
                intent=intent,
                plan=plan,
                policy=policy,
            )
            _validate_current_initial_boundary(intent)
            if plan.profile_registry_digest != registry_digest(load_profile_registry()):
                raise IntegrityError("route attestation plan is stale against the active profile registry")
            _require_authorization(arguments.authorization_reference, approval_packet.packet_digest, "route verification")
            payload = _read_explicit_regular_file(arguments.payload, "external evidence payload")
            proof = _read_explicit_regular_file(arguments.proof, "external detached proof")
            verifier = OpenSshDetachedProofVerifier(
                allowed_signers_path=Path(arguments.allowed_signers),
                ssh_keygen_path=_trusted_system_ssh_keygen(),
            )
            receipt = attest_external_routes(
                plan=plan,
                payload=payload,
                detached_proof=proof,
                policy=policy,
                verifier=verifier,
            )
            result = {
                "approval_packet_digest": approval_packet.packet_digest,
                "attestation_state": receipt.attestation_state,
                "live_attested": receipt.live_attested,
                "promotion_authorized": False,
                "receipt": receipt.to_dict(),
                "status": "VERIFIED_REDACTED_ROUTE_EVIDENCE",
            }
            exit_code = 0 if receipt.live_attested else 2
        else:
            plan = BlindedReviewIntakePlan.from_value(_read_project_json(arguments.plan, "blinded review plan"))
            _require_authorization(arguments.authorization_reference, plan.packet_digest, "review verification")
            payload = _read_explicit_regular_file(arguments.payload, "external evidence payload")
            proof = _read_explicit_regular_file(arguments.proof, "external detached proof")
            verifier = OpenSshDetachedProofVerifier(
                allowed_signers_path=Path(arguments.allowed_signers),
                ssh_keygen_path=_trusted_system_ssh_keygen(),
            )
            receipt = capture_blinded_review_results(
                plan=plan,
                payload=payload,
                detached_proof=proof,
                policy=policy,
                verifier=verifier,
            )
            result = {
                "capture_status": receipt.capture_status,
                "promotion_authorized": False,
                "receipt": receipt.to_dict(),
                "semantic_non_inferiority_status": receipt.semantic_non_inferiority_status,
                "status": "VERIFIED_REDACTED_REVIEW_CAPTURE",
            }
            exit_code = 2
    except (AuthorityDeniedError, ContractError, OSError, ValueError):
        print(json.dumps({"reason": "M9_EXTERNAL_EVIDENCE_REJECTED", "status": "FAIL"}, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
