#!/usr/bin/env python3
"""Create one immutable, redacted plan for a future M9 route-attestation batch.

The explicit input is transient and may contain correlation/model identifiers.
Only their digests are written to the project-relative output. This command does
not contact a provider, read global Codex state, or authorize/run a shadow.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat

from second_brain.canonical import canonical_jcs_bytes
from second_brain.errors import ContractError, IntegrityError, SemanticValidationError
from second_brain.jsonio import load_strict_json, loads_strict_json
from second_brain.m9_external_evidence import (
    RouteAttestationApprovalIntent,
    RouteAttestationPlan,
    create_route_expectation,
)
from second_brain.profiles import load_profile_registry, registry_digest, resolve_profile
from second_brain.workspace import repository_root, resolve_workspace_path


_MAX_INPUT_BYTES = 1_048_576


def _read_explicit_manifest(raw_path: str) -> dict[str, object]:
    path = Path(raw_path)
    if not path.is_absolute():
        raise SemanticValidationError("route attestation input path must be absolute")
    try:
        before = path.lstat()
    except OSError as error:
        raise SemanticValidationError("route attestation input is unavailable") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > _MAX_INPUT_BYTES:
        raise SemanticValidationError("route attestation input is unsafe")
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or after.st_size != before.st_size
        ):
            raise IntegrityError("route attestation input changed while being read")
        chunks: list[bytes] = []
        remaining = _MAX_INPUT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw_bytes = b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not raw_bytes or len(raw_bytes) != before.st_size or len(raw_bytes) > _MAX_INPUT_BYTES:
        raise SemanticValidationError("route attestation input is unsafe")
    try:
        value = loads_strict_json(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise SemanticValidationError("route attestation input is invalid") from error
    if type(value) is not dict or set(value) != {
        "approval_intent_digest",
        "profile_registry_digest",
        "provider_id",
        "requests",
    }:
        raise SemanticValidationError("route attestation input is invalid")
    return value


def _read_approval_intent(relative_path: str) -> RouteAttestationApprovalIntent:
    target = resolve_workspace_path(relative_path)
    root = repository_root()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise SemanticValidationError("route approval intent escapes the project") from error
    try:
        metadata = target.lstat()
    except OSError as error:
        raise SemanticValidationError("route approval intent is unavailable") from error
    if target.is_symlink() or not target.is_file() or metadata.st_size <= 0 or metadata.st_size > _MAX_INPUT_BYTES:
        raise SemanticValidationError("route approval intent is unsafe")
    return RouteAttestationApprovalIntent.from_value(load_strict_json(target))


def _build_plan(value: dict[str, object], intent: RouteAttestationApprovalIntent) -> RouteAttestationPlan:
    registry = load_profile_registry()
    if value["profile_registry_digest"] != registry_digest(registry):
        raise IntegrityError("route attestation manifest does not bind the active profile registry")
    if value["approval_intent_digest"] != intent.intent_digest or value["provider_id"] != intent.provider_id:
        raise IntegrityError("route attestation manifest does not bind the approval intent")
    requests = value["requests"]
    if type(requests) is not list:
        raise SemanticValidationError("route attestation requests are invalid")
    expectations = []
    for item in requests:
        if type(item) is not dict or set(item) != {"correlation_id", "profile_alias", "model_identifier", "effort"}:
            raise SemanticValidationError("route attestation request is invalid")
        resolved = resolve_profile(item["profile_alias"], registry)
        if (
            item["model_identifier"] != resolved.raw_model_slug
            or item["effort"] != resolved.serialized_effort_value
        ):
            raise IntegrityError("route attestation request does not match the active profile registry")
        expectations.append(
            create_route_expectation(
                correlation_id=item["correlation_id"],
                profile_alias=item["profile_alias"],
                model_identifier=item["model_identifier"],
                effort=item["effort"],
            )
        )
    return RouteAttestationPlan(
        plan_version=1,
        approval_intent_digest=value["approval_intent_digest"],
        profile_registry_digest=value["profile_registry_digest"],
        provider_id=value["provider_id"],
        expectations=tuple(expectations),
    )


def _write_immutable_plan(plan: RouteAttestationPlan, relative_output: str) -> Path:
    target = resolve_workspace_path(relative_output)
    artifacts_root = repository_root() / "artifacts"
    try:
        target.resolve(strict=False).relative_to(artifacts_root.resolve(strict=True))
    except ValueError as error:
        raise SemanticValidationError("route attestation output must stay under artifacts") from error
    if target.parent.is_symlink() or artifacts_root.is_symlink() or not artifacts_root.is_dir():
        raise IntegrityError("route attestation output directory is unsafe")
    serialized = canonical_jcs_bytes(plan.to_dict()) + b"\n"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file() or target.read_bytes() != serialized:
            raise IntegrityError("route attestation output already exists with different contents")
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
        raise IntegrityError("route attestation output already exists with different contents")
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
    parser.add_argument("--approval-intent", required=True, help="Project-relative redacted route approval intent JSON")
    parser.add_argument("--input", required=True, help="Absolute transient JSON manifest containing raw correlations")
    parser.add_argument("--output", required=True, help="Project-relative redacted output under artifacts/")
    arguments = parser.parse_args()
    try:
        plan = _build_plan(_read_explicit_manifest(arguments.input), _read_approval_intent(arguments.approval_intent))
        output = _write_immutable_plan(plan, arguments.output)
    except (ContractError, OSError, ValueError):
        print(json.dumps({"reason": "M9_ROUTE_PLAN_REJECTED", "status": "FAIL"}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "output": str(output.relative_to(repository_root())),
                "plan_digest": plan.plan_digest,
                "request_count": len(plan.expectations),
                "status": "REDACTED_PLAN_CREATED",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
