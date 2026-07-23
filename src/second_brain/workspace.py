"""Repository containment helpers for local-only M0 execution."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator

from .errors import WorkspacePathError


def repository_root() -> Path:
    """Return the resolved root without consulting user or global configuration."""

    return Path(__file__).resolve().parents[2]


def resolve_workspace_path(relative_path: str | Path) -> Path:
    """Resolve a relative path only when it remains inside this repository."""

    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise WorkspacePathError("absolute paths are not allowed")

    root = repository_root()
    resolved = (root / candidate).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise WorkspacePathError(f"path escapes repository: {relative_path}") from error
    return resolved


@contextmanager
def temporary_store() -> Iterator[Path]:
    """Provide a disposable store rooted under ignored project artifacts."""

    test_root = resolve_workspace_path("artifacts/test-runs")
    test_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="m0-", dir=test_root) as path:
        yield Path(path)
