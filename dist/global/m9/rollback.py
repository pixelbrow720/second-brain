#!/usr/bin/env python3
"""Restore the exact M9 global guidance state from its local backup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write(path: Path, payload: bytes, mode: int) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".m9-rollback-", dir=path.parent)
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
    arguments = parser.parse_args()
    backup_dir = arguments.backup_dir.resolve()
    manifest_path = backup_dir / "manifest.json"
    backup_agents = backup_dir / "AGENTS.md"
    if not manifest_path.is_file() or not backup_agents.is_file():
        raise SystemExit("M9 rollback backup is incomplete")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("backup_id") != "m9-v1":
        raise SystemExit("M9 rollback manifest is invalid")

    codex_home = Path(manifest["codex_home"]).resolve()
    if backup_dir != (codex_home / "second-brain-backups" / "m9-v1").resolve():
        raise SystemExit("M9 rollback backup location is invalid")
    agents_path = codex_home / "AGENTS.md"
    skill_path = codex_home / "skills" / "pixel-second-brain-workflow" / "SKILL.md"
    if digest(backup_agents) != manifest["agents_before_sha256"]:
        raise SystemExit("M9 rollback backup digest does not match")
    if not agents_path.is_file() or digest(agents_path) != manifest["agents_after_sha256"]:
        raise SystemExit("M9 global guidance changed after rollout; refusing rollback")
    if not skill_path.is_file() or digest(skill_path) != manifest["skill_after_sha256"]:
        raise SystemExit("M9 skill changed after rollout; refusing rollback")
    if any(path != skill_path for path in skill_path.parent.iterdir()):
        raise SystemExit("M9 skill directory contains foreign files; refusing rollback")

    atomic_write(agents_path, backup_agents.read_bytes(), manifest["agents_mode"])
    skill_path.unlink()
    skill_directory = skill_path.parent
    skill_directory.rmdir()
    print(json.dumps({"status": "ROLLED_BACK", "agents_sha256": digest(agents_path)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
