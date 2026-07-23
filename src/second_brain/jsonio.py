"""Strict JSON loading for authority-bound contract inputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .canonical import canonical_jcs_bytes
from .errors import DuplicateKeyError


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant is forbidden: {value}")


def loads_strict_json(text: str) -> Any:
    """Load JSON while rejecting duplicate keys and NaN-like constants."""

    return json.loads(
        text,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_non_json_constant,
    )


def load_strict_json(path: Path) -> Any:
    """Read one UTF-8 JSON file using the authority-safe loader."""

    return loads_strict_json(path.read_text(encoding="utf-8"))


def canonical_json_bytes(value: Any) -> bytes:
    """Return RFC 8785 canonical JSON bytes (kept for the M0 API)."""

    return canonical_jcs_bytes(value)
