"""Deterministic time fixture used by contract and storage tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class DeterministicClock:
    """A test clock that advances only when explicitly requested."""

    def __init__(self, initial: datetime | None = None) -> None:
        self._now = initial or datetime(2026, 7, 22, tzinfo=UTC)
        if self._now.tzinfo is None:
            raise ValueError("deterministic clock requires an aware timestamp")

    def now(self) -> datetime:
        return self._now

    def now_rfc3339(self) -> str:
        return self._now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def advance(self, *, seconds: int = 0) -> datetime:
        self._now += timedelta(seconds=seconds)
        return self._now
