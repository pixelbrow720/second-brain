#!/usr/bin/env python3
"""Pin a public OpenSSH allowed-signers file as a redacted M9 trust policy.

The supplied anchor is read explicitly and never copied into the project. The
output contains only its SHA-256 fingerprint and can be safely reviewed before
any future external-evidence verification.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat

from second_brain.canonical import canonical_jcs_bytes, sha256_bytes
from second_brain.errors import ContractError, IntegrityError, SemanticValidationError
from second_brain.m9_external_evidence import ExternalTrustPolicy
from second_brain.workspace import repository_root, resolve_workspace_path


_MAX_ANCHOR_BYTES = 65_536


def _read_public_anchor(raw_path: str) -> bytes:
    path = Path(raw_path)
    if not path.is_absolute():
        raise SemanticValidationError("trust anchor path must be absolute")
    try:
        before = path.lstat()
    except OSError as error:
        raise SemanticValidationError("trust anchor is unavailable") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > _MAX_ANCHOR_BYTES:
        raise SemanticValidationError("trust anchor is unsafe")
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or after.st_size != before.st_size
        ):
            raise IntegrityError("trust anchor changed while being read")
        chunks: list[bytes] = []
        remaining = _MAX_ANCHOR_BYTES + 1
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
    if not payload or len(payload) != before.st_size or len(payload) > _MAX_ANCHOR_BYTES:
        raise SemanticValidationError("trust anchor is unsafe")
    return payload


def _write_immutable_policy(policy: ExternalTrustPolicy, relative_output: str) -> Path:
    target = resolve_workspace_path(relative_output)
    artifacts_root = repository_root() / "artifacts"
    try:
        target.resolve(strict=False).relative_to(artifacts_root.resolve(strict=True))
    except ValueError as error:
        raise SemanticValidationError("trust policy output must stay under artifacts") from error
    if target.parent.is_symlink() or artifacts_root.is_symlink() or not artifacts_root.is_dir():
        raise IntegrityError("trust policy output directory is unsafe")
    serialized = canonical_jcs_bytes(policy.to_dict()) + b"\n"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file() or target.read_bytes() != serialized:
            raise IntegrityError("trust policy output already exists with different contents")
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
        raise IntegrityError("trust policy output already exists with different contents")
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
    parser.add_argument("--purpose", choices=("route-attestation", "blinded-semantic-review"), required=True)
    parser.add_argument("--authority-id", required=True)
    parser.add_argument("--allowed-signers", required=True, help="Absolute public allowed-signers file")
    parser.add_argument("--output", required=True, help="Project-relative redacted output under artifacts/")
    arguments = parser.parse_args()
    try:
        anchor = _read_public_anchor(arguments.allowed_signers)
        policy = ExternalTrustPolicy(
            policy_version=1,
            purpose=arguments.purpose,
            authority_id=arguments.authority_id,
            verifier_protocol="openssh-detached-proof-v1",
            trust_anchor_fingerprint=sha256_bytes(anchor),
        )
        output = _write_immutable_policy(policy, arguments.output)
    except (ContractError, OSError, ValueError):
        print(json.dumps({"reason": "M9_TRUST_POLICY_REJECTED", "status": "FAIL"}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "output": str(output.relative_to(repository_root())),
                "policy_digest": policy.policy_digest,
                "purpose": policy.purpose,
                "status": "REDACTED_TRUST_POLICY_CREATED",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
