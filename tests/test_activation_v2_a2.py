from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from second_brain.activation_v2_lifecycle import (
    LifecycleObserveOnlyError,
    LifecycleReceipt,
    ObserveOnlyEvent,
    load_lifecycle_receipt,
    observe_lifecycle_event,
)
from second_brain.activation_v2_runtime import PolicyInputs, initialize_disposable_runtime, read_disposable_json
from second_brain.contracts import validate_named_document
from second_brain.errors import ContentPolicyError, IntegrityError
from second_brain.jsonio import load_strict_json
from second_brain.workspace import repository_root


class ActivationV2A2ObserveOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        test_runs = repository_root() / "artifacts" / "test-runs"
        test_runs.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="activation-v2-a2-", dir=test_runs)
        self.sandbox = Path(self.temporary.name)
        self.runtime = initialize_disposable_runtime(
            self.sandbox / "runtime",
            PolicyInputs.fixture_recommended_defaults(),
            created_at="2026-07-24T02:00:00Z",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_canonical_observe_only_receipt_validates_and_contains_no_event_body(self) -> None:
        document = load_strict_json(
            repository_root() / "fixtures/canonical/activation-v2-lifecycle-receipt-v1.json"
        )
        validate_named_document("activation-v2-lifecycle-receipt-v1", document)
        receipt = LifecycleReceipt.from_value(document)
        self.assertEqual(receipt.to_dict(), document)
        self.assertFalse(receipt.content_persisted)
        self.assertFalse(receipt.authority_write)
        self.assertFalse(receipt.hook_installed)

    def test_every_allowlisted_fixture_event_creates_metadata_only_synthetic_receipt(self) -> None:
        fixture = load_strict_json(repository_root() / "fixtures/activation-v2/a2-lifecycle-events-v1.json")
        self.assertEqual(fixture["data_class"], "PUBLIC_SYNTHETIC")
        self.assertEqual(len(fixture["events"]), 5)
        receipts = []
        for index, event in enumerate(fixture["events"], start=1):
            receipt = observe_lifecycle_event(
                self.runtime,
                event,
                receipt_id=f"lifecycle-receipt:10000000-0000-4000-8000-0000000002{20 + index}",
            )
            receipts.append(receipt)
            persisted = read_disposable_json(
                self.runtime,
                f"receipts/lifecycle/{receipt.receipt_id.removeprefix('lifecycle-receipt:')}.json",
            )
            self.assertEqual(persisted, receipt.to_dict())
            self.assertEqual(persisted["recording_mode"], "observe_only")
            self.assertFalse(persisted["content_persisted"])
            self.assertFalse(persisted["authority_write"])
            self.assertFalse(persisted["hook_installed"])
            self.assertNotIn("metadata", persisted)
        self.assertEqual({item.event_type for item in receipts}, {"SessionStart", "UserPromptSubmit", "PreCompact", "PostToolUse", "Stop"})

    def test_prompt_transcript_and_tool_body_fields_fail_before_any_receipt_is_written(self) -> None:
        marker = "V2_RAW_TRANSCRIPT_SENTINEL"
        event = {
            "event_id": "lifecycle-event:10000000-0000-4000-8000-000000000231",
            "event_type": "UserPromptSubmit",
            "project_id": "v2-fixture",
            "occurred_at": "2026-07-24T02:02:00Z",
            "metadata": {"memory_mode": "ASSISTED", "prompt": marker},
        }
        receipt_id = "lifecycle-receipt:10000000-0000-4000-8000-000000000232"
        with self.assertRaises(LifecycleObserveOnlyError) as raised:
            observe_lifecycle_event(self.runtime, event, receipt_id=receipt_id)
        self.assertNotIn(marker, str(raised.exception))
        self.assertFalse((self.runtime.root / "receipts" / "lifecycle" / "10000000-0000-4000-8000-000000000232.json").exists())

        event["metadata"] = {"memory_mode": "V2_PROMPT_INJECTION_SENTINEL"}
        with self.assertRaises(ContentPolicyError) as raised:
            ObserveOnlyEvent.from_value(event)
        self.assertNotIn("V2_PROMPT_INJECTION_SENTINEL", str(raised.exception))

    def test_post_tool_use_accepts_only_opaque_allowlisted_identifiers(self) -> None:
        event = {
            "event_id": "lifecycle-event:10000000-0000-4000-8000-000000000233",
            "event_type": "PostToolUse",
            "project_id": "v2-fixture",
            "occurred_at": "2026-07-24T02:03:00Z",
            "metadata": {"artifact_ids": ["artifact:v2-safe", "file:///v2-private-marker"]},
        }
        with self.assertRaises(ContentPolicyError) as raised:
            ObserveOnlyEvent.from_value(event)
        self.assertNotIn("file:///v2-private-marker", str(raised.exception))

    def test_duplicate_receipt_and_tampered_observe_only_flags_fail_closed(self) -> None:
        event = {
            "event_id": "lifecycle-event:10000000-0000-4000-8000-000000000234",
            "event_type": "Stop",
            "project_id": "v2-fixture",
            "occurred_at": "2026-07-24T02:04:00Z",
            "metadata": {"closure_state": "candidate_requested"},
        }
        receipt_id = "lifecycle-receipt:10000000-0000-4000-8000-000000000235"
        receipt = observe_lifecycle_event(self.runtime, event, receipt_id=receipt_id)
        with self.assertRaises(LifecycleObserveOnlyError):
            observe_lifecycle_event(self.runtime, event, receipt_id=receipt_id)

        path = self.runtime.root / "receipts" / "lifecycle" / f"{receipt.receipt_id.removeprefix('lifecycle-receipt:')}.json"
        payload = load_strict_json(path)
        payload["hook_installed"] = True
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o600)
        with self.assertRaises(IntegrityError):
            load_lifecycle_receipt(self.runtime, receipt_id)


if __name__ == "__main__":
    unittest.main()
