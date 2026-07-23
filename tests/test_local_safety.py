from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import unittest

from second_brain.clock import DeterministicClock
from second_brain.errors import WorkspacePathError
from second_brain.workspace import repository_root, resolve_workspace_path, temporary_store


class LocalSafetyTests(unittest.TestCase):
    def test_clock_is_deterministic_and_uses_utc(self) -> None:
        clock = DeterministicClock(datetime(2026, 7, 22, 1, 2, 3, tzinfo=UTC))
        self.assertEqual(clock.now_rfc3339(), "2026-07-22T01:02:03Z")
        clock.advance(seconds=57)
        self.assertEqual(clock.now_rfc3339(), "2026-07-22T01:03:00Z")

    def test_workspace_guard_rejects_outside_and_absolute_paths(self) -> None:
        with self.assertRaises(WorkspacePathError):
            resolve_workspace_path("../outside-repository")
        with self.assertRaises(WorkspacePathError):
            resolve_workspace_path(Path("/tmp/outside-repository"))

    def test_temporary_store_is_created_only_under_repository_artifacts(self) -> None:
        root = repository_root()
        with temporary_store() as store:
            self.assertTrue(store.is_dir())
            self.assertTrue(store.is_relative_to(root / "artifacts" / "test-runs"))
            marker = store / "marker.txt"
            marker.write_text("inside repository", encoding="utf-8")
            self.assertEqual(marker.read_text(encoding="utf-8"), "inside repository")
        self.assertFalse(store.exists())

    def test_ignore_rules_cover_private_runtime_and_derived_state(self) -> None:
        ignore_rules = (repository_root() / ".gitignore").read_text(encoding="utf-8")
        for rule in (
            "**/runtime/",
            "*.key",
            "*.sqlite",
            "*.cache",
            "**/derived/",
            "artifacts/receipts/",
            "artifacts/private/",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, ignore_rules)


if __name__ == "__main__":
    unittest.main()
