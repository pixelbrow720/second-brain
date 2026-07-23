#!/usr/bin/env python3
"""Run the documented M0 checks from an isolated copy inside this repository."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from second_brain.workspace import resolve_workspace_path


TEMP_PARENT = resolve_workspace_path("artifacts/test-runs")


def ignore_copy(directory: str, names: list[str]) -> set[str]:
    current = Path(directory)
    ignored = {".git", "__pycache__", ".pytest_cache", ".venv"}
    if current == ROOT / "artifacts":
        ignored.add("test-runs")
    return ignored.intersection(names)


def main() -> int:
    TEMP_PARENT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="clean-room-", dir=TEMP_PARENT) as temporary:
        copy_root = Path(temporary) / "repository"
        # Preserve symlinks so an untrusted link is never followed during the copy.
        shutil.copytree(ROOT, copy_root, ignore=ignore_copy, symlinks=True)
        result = subprocess.run(["make", "check"], cwd=copy_root, check=False)
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
