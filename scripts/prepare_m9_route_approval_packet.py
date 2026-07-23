#!/usr/bin/env python3
"""Build the exact user-approval packet for one future M9 route batch.

The packet is project-local, immutable, redacted, and non-authorizing until a
user explicitly approves its exact digest.  It cannot start a provider request,
mutate global Codex state, or promote the workflow.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from second_brain.canonical import canonical_jcs_bytes
from second_brain.errors import ContractError, IntegrityError, SemanticValidationError
from second_brain.global_rollout import load_m9_packet
from second_brain.jsonio import load_strict_json
from second_brain.live_shadow import verify_initial_shadow_receipt
from second_brain.m9_external_evidence import (
    ExternalTrustPolicy,
    ROUTE_ATTESTATION_PURPOSE,
    RouteAttestationApprovalIntent,
    RouteAttestationApprovalPacket,
    RouteAttestationPlan,
    build_route_attestation_approval_packet,
    validate_route_attestation_approval_packet,
)
from second_brain.profiles import load_profile_registry, registry_digest
from second_brain.workspace import repository_root, resolve_workspace_path


_MAX_ARTIFACT_BYTES = 1_048_576


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
    if target.is_symlink() or not target.is_file() or metadata.st_size <= 0 or metadata.st_size > _MAX_ARTIFACT_BYTES:
        raise SemanticValidationError(f"{label} is unsafe")
    return load_strict_json(target)


def _validate_current_initial_boundary(intent: RouteAttestationApprovalIntent) -> None:
    packet = load_m9_packet()
    shadow = _read_project_json("artifacts/m9-initial-shadow-report.json", "initial M9 shadow receipt")
    if type(shadow) is not dict:
        raise SemanticValidationError("initial M9 shadow receipt is invalid")
    verify_initial_shadow_receipt(shadow)
    if (
        intent.initial_m9_packet_digest != packet["packet_digest"]
        or intent.initial_shadow_receipt_digest != shadow["receipt_digest"]
        or shadow["packet_digest"] != packet["packet_digest"]
        or shadow["shadow_execution_status"] != "PASS"
        or shadow["promotion_status"] != "BLOCKED_MISSING_AUTHENTICATED_ROUTE_TELEMETRY"
        or shadow["configured_provider"] != intent.provider_id
    ):
        raise IntegrityError("route approval intent is stale against the current initial M9 boundary")


def _write_immutable_packet(packet: RouteAttestationApprovalPacket, relative_output: str) -> Path:
    target = resolve_workspace_path(relative_output)
    artifacts_root = repository_root() / "artifacts"
    try:
        target.resolve(strict=False).relative_to(artifacts_root.resolve(strict=True))
    except ValueError as error:
        raise SemanticValidationError("route approval packet output must stay under artifacts") from error
    if target.parent.is_symlink() or artifacts_root.is_symlink() or not artifacts_root.is_dir():
        raise IntegrityError("route approval packet output directory is unsafe")
    serialized = canonical_jcs_bytes(packet.to_dict()) + b"\n"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file() or target.read_bytes() != serialized:
            raise IntegrityError("route approval packet output already exists with different contents")
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
        raise IntegrityError("route approval packet output already exists with different contents")
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval-intent", required=True, help="Project-relative route approval intent JSON")
    parser.add_argument("--plan", required=True, help="Project-relative redacted route-attestation plan JSON")
    parser.add_argument("--policy", required=True, help="Project-relative route-attestation trust-policy JSON")
    parser.add_argument("--output", required=True, help="Project-relative redacted output under artifacts/")
    arguments = parser.parse_args()
    try:
        intent = RouteAttestationApprovalIntent.from_value(
            _read_project_json(arguments.approval_intent, "route approval intent")
        )
        plan = RouteAttestationPlan.from_value(_read_project_json(arguments.plan, "route attestation plan"))
        policy = ExternalTrustPolicy.from_value(_read_project_json(arguments.policy, "route trust policy"))
        _validate_current_initial_boundary(intent)
        if plan.profile_registry_digest != registry_digest(load_profile_registry()):
            raise IntegrityError("route attestation plan is stale against the active profile registry")
        if policy.purpose != ROUTE_ATTESTATION_PURPOSE:
            raise SemanticValidationError("route trust policy has the wrong purpose")
        packet = build_route_attestation_approval_packet(intent=intent, plan=plan, policy=policy)
        validate_route_attestation_approval_packet(packet=packet, intent=intent, plan=plan, policy=policy)
        output = _write_immutable_packet(packet, arguments.output)
    except (ContractError, OSError, ValueError):
        print(json.dumps({"reason": "M9_ROUTE_APPROVAL_PACKET_REJECTED", "status": "FAIL"}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "approval_request": packet.approval_request,
                "expected_request_count": packet.expected_request_count,
                "output": str(output.relative_to(repository_root())),
                "packet_digest": packet.packet_digest,
                "promotion_authorized": False,
                "status": "REDACTED_ROUTE_APPROVAL_PACKET_CREATED",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
