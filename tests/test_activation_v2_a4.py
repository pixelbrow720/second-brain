from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from second_brain.activation_v2 import activation_v2_logical_digest
from second_brain.activation_v2_project_auto import (
    ProjectAutoReceipt,
    ProjectAutoSyntheticError,
    SyntheticProjectAutoState,
    commit_synthetic_project_auto,
    load_synthetic_project_auto_state,
    rollback_synthetic_project_auto,
)
from second_brain.activation_v2_runtime import PolicyInputs, initialize_disposable_runtime
from second_brain.contracts import validate_named_document
from second_brain.errors import IntegrityError, SchemaValidationError
from second_brain.jsonio import load_strict_json
from second_brain.workspace import repository_root


class ActivationV2A4SyntheticProjectAutoTests(unittest.TestCase):
    def setUp(self) -> None:
        test_runs = repository_root() / "artifacts" / "test-runs"
        test_runs.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="activation-v2-a4-", dir=test_runs)
        self.sandbox = Path(self.temporary.name)
        self.runtime = initialize_disposable_runtime(
            self.sandbox / "runtime",
            PolicyInputs.fixture_recommended_defaults(capture_default="PROJECT_AUTO"),
            created_at="2026-07-24T04:00:00Z",
        )
        self.closure = load_strict_json(
            repository_root() / "fixtures/canonical/activation-v2-task-closure-v1.json"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _commit(self, closure: dict[str, object], *, revision: int, suffix: int) -> ProjectAutoReceipt:
        return commit_synthetic_project_auto(
            self.runtime,
            closure,
            expected_revision=revision,
            transaction_id=f"project-auto:10000000-0000-4000-8000-0000000004{suffix:02d}",
            receipt_id=f"project-auto-receipt:10000000-0000-4000-8000-0000000004{suffix + 20:02d}",
            created_at="2026-07-24T04:00:01Z",
        )

    def test_canonical_state_and_receipt_validate_as_fixture_only(self) -> None:
        root = repository_root()
        state_document = load_strict_json(root / "fixtures/canonical/activation-v2-project-auto-state-v1.json")
        receipt_document = load_strict_json(root / "fixtures/canonical/activation-v2-project-auto-receipt-v1.json")
        validate_named_document("activation-v2-project-auto-state-v1", state_document)
        validate_named_document("activation-v2-project-auto-receipt-v1", receipt_document)
        self.assertEqual(SyntheticProjectAutoState.from_value(state_document).to_dict(), state_document)
        self.assertEqual(ProjectAutoReceipt.from_value(receipt_document).to_dict(), receipt_document)
        self.assertFalse(receipt_document["authority_write"])
        self.assertFalse(receipt_document["global_write"])

    def test_project_auto_commit_uses_exact_cas_dedupe_and_a_content_free_status(self) -> None:
        receipt = self._commit(self.closure, revision=0, suffix=1)
        self.assertEqual(receipt.project_write_state, "committed_synthetic")
        self.assertEqual(receipt.task_outcome, "completed")
        self.assertEqual(receipt.candidate_counts, {
            "decisions": 1,
            "evidence": 1,
            "open_tasks": 1,
            "questions": 1,
            "global_candidates": 1,
        })
        self.assertNotIn("summary", json.dumps(receipt.to_dict(), sort_keys=True))
        self.assertFalse(receipt.authority_write)
        self.assertFalse(receipt.global_write)

        duplicate = self._commit(self.closure, revision=99, suffix=2)
        self.assertEqual(duplicate.to_dict(), receipt.to_dict())
        state = load_synthetic_project_auto_state(self.runtime, "v2-fixture")
        self.assertEqual(state.revision, 1)
        self.assertEqual(len(state.entries), 1)

        second = dict(self.closure)
        second["closure_id"] = "closure:10000000-0000-4000-8000-000000000403"
        with self.assertRaises(ProjectAutoSyntheticError):
            self._commit(second, revision=0, suffix=3)
        self.assertEqual(load_synthetic_project_auto_state(self.runtime, "v2-fixture").revision, 1)

    def test_rollback_requires_the_latest_commit_and_retains_monotonic_synthetic_state(self) -> None:
        first = self._commit(self.closure, revision=0, suffix=4)
        second_closure = dict(self.closure)
        second_closure["closure_id"] = "closure:10000000-0000-4000-8000-000000000404"
        second = self._commit(second_closure, revision=1, suffix=5)
        with self.assertRaises(ProjectAutoSyntheticError):
            rollback_synthetic_project_auto(self.runtime, first, expected_revision=2)

        rollback = rollback_synthetic_project_auto(
            self.runtime,
            second,
            expected_revision=2,
            rollback_receipt_id="project-auto-receipt:10000000-0000-4000-8000-000000000426",
            created_at="2026-07-24T04:00:02Z",
        )
        self.assertEqual(rollback.operation, "rollback")
        self.assertEqual(rollback.project_write_state, "rolled_back_synthetic")
        state = load_synthetic_project_auto_state(self.runtime, "v2-fixture")
        self.assertEqual(state.revision, 3)
        self.assertEqual(state.entries[-1].state, "rolled_back")
        with self.assertRaises(ProjectAutoSyntheticError):
            rollback_synthetic_project_auto(self.runtime, second, expected_revision=3)

    def test_raw_or_no_durable_closure_and_non_project_auto_policy_fail_before_state_write(self) -> None:
        unsafe = dict(self.closure)
        unsafe["prompt"] = "V2_PROMPT_INJECTION_SENTINEL"
        with self.assertRaises(SchemaValidationError) as raised:
            self._commit(unsafe, revision=0, suffix=6)
        self.assertNotIn("V2_PROMPT_INJECTION_SENTINEL", str(raised.exception))
        self.assertFalse((self.runtime.root / "projects" / "v2-fixture" / "synthetic-a4").exists())

        no_change = dict(self.closure)
        no_change["task_outcome"] = "no_durable_change"
        for key in ("decisions", "evidence", "open_tasks", "questions", "global_candidates"):
            no_change[key] = []
        with self.assertRaises(ProjectAutoSyntheticError):
            self._commit(no_change, revision=0, suffix=7)

        assisted_runtime = initialize_disposable_runtime(
            self.sandbox / "assisted-runtime",
            PolicyInputs.fixture_recommended_defaults(capture_default="ASSISTED"),
            created_at="2026-07-24T04:01:00Z",
        )
        with self.assertRaises(ProjectAutoSyntheticError):
            commit_synthetic_project_auto(assisted_runtime, self.closure, expected_revision=0)
        self.assertFalse((assisted_runtime.root / "projects" / "v2-fixture" / "synthetic-a4").exists())

    def test_rehashed_cross_project_state_is_rejected_on_readback(self) -> None:
        self._commit(self.closure, revision=0, suffix=8)
        path = self.runtime.root / "projects" / "v2-fixture" / "synthetic-a4" / "project-auto-state.json"
        payload = load_strict_json(path)
        payload["project_id"] = "foreign-project"
        payload["state_digest"] = activation_v2_logical_digest(payload, "state_digest")
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o600)
        with self.assertRaises(IntegrityError):
            load_synthetic_project_auto_state(self.runtime, "v2-fixture")


if __name__ == "__main__":
    unittest.main()
