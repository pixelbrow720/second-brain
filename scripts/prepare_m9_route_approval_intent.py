#!/usr/bin/env python3
"""Create a redacted, non-authorizing scope intent for a future route batch.

The intent binds the already-applied initial M9 packet and its immutable
shadow receipt before any transient correlation/model data exists.  It is not
an approval and never starts a provider request or changes global state.
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
from second_brain.m9_external_evidence import RouteAttestationApprovalIntent
from second_brain.workspace import repository_root, resolve_workspace_path


_MAX_RECEIPT_BYTES = 1_048_576


def _read_initial_shadow() -> tuple[dict[str, object], dict[str, object]]:
    packet = load_m9_packet()
    target = resolve_workspace_path("artifacts/m9-initial-shadow-report.json")
    artifacts_root = repository_root() / "artifacts"
    try:
        target.relative_to(artifacts_root)
    except ValueError as error:
        raise SemanticValidationError("initial M9 shadow receipt escapes artifacts") from error
    try:
        metadata = target.lstat()
    except OSError as error:
        raise SemanticValidationError("initial M9 shadow receipt is unavailable") from error
    if target.is_symlink() or not target.is_file() or metadata.st_size <= 0 or metadata.st_size > _MAX_RECEIPT_BYTES:
        raise SemanticValidationError("initial M9 shadow receipt is unsafe")
    shadow = load_strict_json(target)
    if type(shadow) is not dict:
        raise SemanticValidationError("initial M9 shadow receipt is invalid")
    verify_initial_shadow_receipt(shadow)
    if (
        shadow["packet_digest"] != packet["packet_digest"]
        or shadow["shadow_execution_status"] != "PASS"
        or shadow["promotion_status"] != "BLOCKED_MISSING_AUTHENTICATED_ROUTE_TELEMETRY"
        or shadow["route_attestation"] != {
            "status": "MISSING_TELEMETRY",
            "live_attested": False,
            "reason_code": "CODEX_CLI_EVENTS_LACK_AUTHENTICATED_ROUTE_OBSERVATION",
        }
    ):
        raise IntegrityError("initial M9 shadow does not establish the required starting boundary")
    return packet, shadow


def _write_immutable_intent(intent: RouteAttestationApprovalIntent, relative_output: str) -> Path:
    target = resolve_workspace_path(relative_output)
    artifacts_root = repository_root() / "artifacts"
    try:
        target.resolve(strict=False).relative_to(artifacts_root.resolve(strict=True))
    except ValueError as error:
        raise SemanticValidationError("route approval intent output must stay under artifacts") from error
    if target.parent.is_symlink() or artifacts_root.is_symlink() or not artifacts_root.is_dir():
        raise IntegrityError("route approval intent output directory is unsafe")
    serialized = canonical_jcs_bytes(intent.to_dict()) + b"\n"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file() or target.read_bytes() != serialized:
            raise IntegrityError("route approval intent output already exists with different contents")
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
        raise IntegrityError("route approval intent output already exists with different contents")
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
    parser.add_argument("--output", required=True, help="Project-relative redacted output under artifacts/")
    arguments = parser.parse_args()
    try:
        packet, shadow = _read_initial_shadow()
        provider = shadow["configured_provider"]
        intent = RouteAttestationApprovalIntent(
            intent_version=1,
            initial_m9_packet_digest=packet["packet_digest"],
            initial_shadow_receipt_digest=shadow["receipt_digest"],
            provider_id=provider,
        )
        output = _write_immutable_intent(intent, arguments.output)
    except (ContractError, OSError, ValueError):
        print(json.dumps({"reason": "M9_ROUTE_APPROVAL_INTENT_REJECTED", "status": "FAIL"}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "intent_digest": intent.intent_digest,
                "output": str(output.relative_to(repository_root())),
                "provider_id": intent.provider_id,
                "status": "REDACTED_ROUTE_APPROVAL_INTENT_CREATED",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
