#!/usr/bin/env python3
"""Read back the exact local prerequisites before one approved M9 route batch.

This command is read-only: it never starts a provider request, invokes a
verifier, writes an artifact, changes global state, or authorizes promotion.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import stat

from second_brain.errors import ContractError, SemanticValidationError
from second_brain.global_rollout import load_m9_packet
from second_brain.jsonio import load_strict_json
from second_brain.m9_external_evidence import (
    ExternalTrustPolicy,
    RouteAttestationApprovalIntent,
    RouteAttestationApprovalPacket,
    RouteAttestationPlan,
)
from second_brain.m9_route_preflight import preflight_route_attestation_batch
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
    if (
        target.is_symlink()
        or not target.is_file()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > _MAX_ARTIFACT_BYTES
    ):
        raise SemanticValidationError(f"{label} is unsafe")
    return load_strict_json(target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", required=True, type=Path)
    parser.add_argument("--approval-intent", required=True, help="Project-relative route approval intent JSON")
    parser.add_argument("--approval-packet", required=True, help="Project-relative exact route approval packet JSON")
    parser.add_argument("--plan", required=True, help="Project-relative redacted route-attestation plan JSON")
    parser.add_argument("--policy", required=True, help="Project-relative route-attestation trust-policy JSON")
    parser.add_argument("--allowed-signers", required=True, type=Path, help="Absolute pinned public allowed-signers file")
    parser.add_argument("--approval-reference", required=True)
    arguments = parser.parse_args()
    try:
        result = preflight_route_attestation_batch(
            initial_packet=load_m9_packet(),
            initial_shadow=_read_project_json("artifacts/m9-initial-shadow-report.json", "initial M9 shadow receipt"),
            intent=RouteAttestationApprovalIntent.from_value(
                _read_project_json(arguments.approval_intent, "route approval intent")
            ),
            plan=RouteAttestationPlan.from_value(_read_project_json(arguments.plan, "route attestation plan")),
            policy=ExternalTrustPolicy.from_value(_read_project_json(arguments.policy, "route trust policy")),
            approval_packet=RouteAttestationApprovalPacket.from_value(
                _read_project_json(arguments.approval_packet, "route approval packet")
            ),
            codex_home=arguments.codex_home,
            allowed_signers_path=arguments.allowed_signers,
            approval_reference=arguments.approval_reference,
        )
    except (ContractError, OSError, ValueError):
        print(json.dumps({"reason": "M9_ROUTE_BATCH_PREFLIGHT_REJECTED", "status": "FAIL"}, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
