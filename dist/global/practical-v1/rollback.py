#!/usr/bin/env python3
"""Restore the pre-Practical-V1 skill after separate rollback approval."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile


BACKUP_ID = "practical-v1-v1"


def read_regular(path: Path, maximum: int = 1_048_576) -> tuple[bytes, int]:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
        raise RuntimeError("unsafe rollback file")
    return path.read_bytes(), stat.S_IMODE(metadata.st_mode)


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def atomic_write(path: Path, payload: bytes, mode: int) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".practical-v1-rollback-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--rollback-packet-digest", required=True)
    parser.add_argument("--approval-reference", required=True)
    arguments = parser.parse_args()
    if re.fullmatch(r"[0-9a-f]{64}", arguments.rollback_packet_digest) is None:
        raise RuntimeError("rollback packet digest is invalid")
    if arguments.rollback_packet_digest not in arguments.approval_reference:
        raise RuntimeError("fresh rollback approval does not bind the packet digest")

    backup = arguments.backup_dir.resolve(strict=True)
    manifest_bytes, _ = read_regular(backup / "manifest.json")
    manifest = json.loads(manifest_bytes)
    home = Path(manifest["codex_home"]).resolve(strict=True)
    expected_backup = (home / "second-brain-backups" / BACKUP_ID).resolve(strict=True)
    if backup != expected_backup or manifest.get("backup_id") != BACKUP_ID:
        raise RuntimeError("rollback backup identity is invalid")

    skill = (home / manifest["skill_relative_path"]).resolve(strict=True)
    bridge = home / manifest["bridge_relative_path"]
    current_skill, _ = read_regular(skill)
    current_bridge, _ = read_regular(bridge)
    before_skill, _ = read_regular(backup / "skill-before.md")
    if digest(current_skill) != manifest["skill_after_sha256"]:
        raise RuntimeError("installed skill drifted after Practical V1 apply")
    if digest(current_bridge) != manifest["bridge_after_sha256"]:
        raise RuntimeError("installed bridge drifted after Practical V1 apply")
    if digest(before_skill) != manifest["skill_before_sha256"]:
        raise RuntimeError("rollback backup skill is invalid")

    atomic_write(skill, before_skill, int(manifest["skill_before_mode"]))
    bridge.unlink()
    try:
        bridge.parent.rmdir()
    except OSError:
        pass
    restored, restored_mode = read_regular(skill)
    if digest(restored) != manifest["skill_before_sha256"] or restored_mode != manifest["skill_before_mode"]:
        raise RuntimeError("rollback readback failed")
    print(json.dumps({"status": "ROLLED_BACK", "backup_preserved": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
