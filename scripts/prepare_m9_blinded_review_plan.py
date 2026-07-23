#!/usr/bin/env python3
"""Create an immutable redacted intake plan from a NOT_RUN blinded review packet.

The packet already contains only opaque case/evidence digests. This script does
not expose the hidden candidate-label mapping, contact a provider, or promote a
semantic gate.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from second_brain.canonical import canonical_jcs_bytes
from second_brain.errors import ContractError, IntegrityError, SemanticValidationError
from second_brain.jsonio import load_strict_json
from second_brain.m9_external_evidence import blinded_review_packet_from_value, build_blinded_review_intake_plan
from second_brain.workspace import repository_root, resolve_workspace_path


_MAX_PACKET_BYTES = 1_048_576


def _read_project_packet(relative_path: str) -> object:
    target = resolve_workspace_path(relative_path)
    root = repository_root()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise SemanticValidationError("blinded review packet escapes the project") from error
    try:
        metadata = target.lstat()
    except OSError as error:
        raise SemanticValidationError("blinded review packet is unavailable") from error
    if target.is_symlink() or not target.is_file() or metadata.st_size <= 0 or metadata.st_size > _MAX_PACKET_BYTES:
        raise SemanticValidationError("blinded review packet is unsafe")
    return load_strict_json(target)


def _write_immutable_plan(plan: object, relative_output: str) -> Path:
    to_dict = getattr(plan, "to_dict", None)
    if not callable(to_dict):
        raise SemanticValidationError("blinded review plan is invalid")
    target = resolve_workspace_path(relative_output)
    artifacts_root = repository_root() / "artifacts"
    try:
        target.resolve(strict=False).relative_to(artifacts_root.resolve(strict=True))
    except ValueError as error:
        raise SemanticValidationError("blinded review output must stay under artifacts") from error
    if target.parent.is_symlink() or artifacts_root.is_symlink() or not artifacts_root.is_dir():
        raise IntegrityError("blinded review output directory is unsafe")
    serialized = canonical_jcs_bytes(to_dict()) + b"\n"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file() or target.read_bytes() != serialized:
            raise IntegrityError("blinded review output already exists with different contents")
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
        raise IntegrityError("blinded review output already exists with different contents")
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
    parser.add_argument("--packet", required=True, help="Project-relative redacted blinded review packet JSON")
    parser.add_argument("--output", required=True, help="Project-relative redacted output under artifacts/")
    arguments = parser.parse_args()
    try:
        packet = blinded_review_packet_from_value(_read_project_packet(arguments.packet))
        plan = build_blinded_review_intake_plan(packet)
        output = _write_immutable_plan(plan, arguments.output)
    except (ContractError, OSError, ValueError):
        print(json.dumps({"reason": "M9_BLINDED_REVIEW_PLAN_REJECTED", "status": "FAIL"}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "output": str(output.relative_to(repository_root())),
                "plan_digest": plan.plan_digest,
                "review_case_count": len(plan.case_reference_digests),
                "status": "REDACTED_BLINDED_REVIEW_PLAN_CREATED",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
