from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from second_brain.activation_v2 import activation_v2_logical_digest
from second_brain.activation_v2_memory import (
    ActivationV2MemoryError,
    AssistedRecoveryRequest,
    ClosureProposal,
    ProposalReviewReceipt,
    RecoveryProposal,
    SyntheticRecoveryCorpus,
    SyntheticRecoveryRecord,
    assisted_recovery,
    load_recovery_proposal,
    propose_task_closure,
    review_closure_proposal,
    seed_synthetic_recovery_corpus,
)
from second_brain.activation_v2_runtime import PolicyInputs, initialize_disposable_runtime, read_disposable_json
from second_brain.contracts import validate_named_document
from second_brain.errors import IntegrityError, SchemaValidationError
from second_brain.jsonio import load_strict_json
from second_brain.workspace import repository_root


class ActivationV2A3ProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        test_runs = repository_root() / "artifacts" / "test-runs"
        test_runs.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="activation-v2-a3-", dir=test_runs)
        self.sandbox = Path(self.temporary.name)
        self.runtime = initialize_disposable_runtime(
            self.sandbox / "runtime",
            PolicyInputs.fixture_recommended_defaults(),
            created_at="2026-07-24T03:00:00Z",
        )
        self.fixture = load_strict_json(repository_root() / "fixtures/activation-v2/a3-recovery-evaluation-v1.json")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _seed(self) -> SyntheticRecoveryCorpus:
        return seed_synthetic_recovery_corpus(self.runtime, self.fixture["corpus"])

    def test_canonical_recovery_and_closure_proposals_validate_and_remain_non_authoritative(self) -> None:
        root = repository_root()
        recovery = load_strict_json(root / "fixtures/canonical/activation-v2-recovery-proposal-v1.json")
        closure = load_strict_json(root / "fixtures/canonical/activation-v2-closure-proposal-v1.json")
        review = load_strict_json(root / "fixtures/canonical/activation-v2-proposal-review-receipt-v1.json")
        validate_named_document("activation-v2-recovery-proposal-v1", recovery)
        validate_named_document("activation-v2-closure-proposal-v1", closure)
        validate_named_document("activation-v2-proposal-review-receipt-v1", review)
        self.assertEqual(RecoveryProposal.from_value(recovery).to_dict(), recovery)
        self.assertEqual(ClosureProposal.from_value(closure).to_dict(), closure)
        self.assertEqual(ProposalReviewReceipt.from_value(review).to_dict(), review)
        self.assertFalse(recovery["authority_write"])
        self.assertFalse(recovery["global_write"])
        self.assertFalse(closure["authority_write"])
        self.assertFalse(closure["global_write"])

    def test_assisted_recovery_returns_relevant_records_omissions_and_freshness_without_authority_write(self) -> None:
        corpus = self._seed()
        case = self.fixture["cases"][0]
        proposal = assisted_recovery(
            self.runtime,
            case["request"],
            proposal_id="recovery-proposal:10000000-0000-4000-8000-000000000306",
            created_at="2026-07-24T03:00:00Z",
        )
        self.assertEqual(proposal.corpus_digest, corpus.corpus_digest)
        self.assertEqual([item.record_id for item in proposal.included], case["expected_included"])
        self.assertEqual([item.record_id for item in proposal.relevant_but_omitted], case["expected_omitted"])
        self.assertEqual(proposal.relevant_but_omitted[0].freshness, "partial")
        self.assertLessEqual(proposal.latency_ms, 1_000)
        self.assertFalse(proposal.authority_write)
        self.assertFalse(proposal.global_write)
        stored = read_disposable_json(
            self.runtime,
            "receipts/recovery/10000000-0000-4000-8000-000000000306.json",
        )
        self.assertEqual(stored, proposal.to_dict())
        self.assertNotIn("records", stored)

    def test_global_records_are_excluded_without_explicit_assisted_global_selection(self) -> None:
        self._seed()
        request = {
            "request_id": "recovery-request:10000000-0000-4000-8000-000000000311",
            "project_id": "v2-fixture",
            "query_labels": ["activation"],
            "max_results": 12,
            "include_global": False,
        }
        proposal = assisted_recovery(
            self.runtime,
            request,
            proposal_id="recovery-proposal:10000000-0000-4000-8000-000000000312",
            created_at="2026-07-24T03:01:00Z",
        )
        self.assertTrue(all(item.scope == "project" for item in proposal.included))
        self.assertTrue(all(item.scope == "project" for item in proposal.relevant_but_omitted))

    def test_cross_project_corpus_and_raw_request_field_fail_before_any_proposal(self) -> None:
        foreign_record = SyntheticRecoveryRecord(
            "mem:foreign-project:decision:10000000-0000-4000-8000-000000000313",
            "project",
            "foreign-project",
            "decision",
            "Foreign fixture record.",
            ("activation",),
            "active",
            "fresh",
            "verified",
            1,
        )
        local_record = SyntheticRecoveryRecord.from_value(self.fixture["corpus"]["records"][0])
        with self.assertRaises(ActivationV2MemoryError):
            SyntheticRecoveryCorpus(1, "PUBLIC_SYNTHETIC", "v2-fixture", (local_record, foreign_record))

        marker = "V2_RAW_TRANSCRIPT_SENTINEL"
        request = {
            "request_id": "recovery-request:10000000-0000-4000-8000-000000000314",
            "project_id": "v2-fixture",
            "query_labels": ["activation"],
            "max_results": 1,
            "include_global": False,
            "prompt": marker,
        }
        with self.assertRaises(ActivationV2MemoryError) as raised:
            AssistedRecoveryRequest.from_value(request)
        self.assertNotIn(marker, str(raised.exception))

    def test_task_closure_becomes_pending_project_and_global_proposals_only_then_gets_a_noncommitting_review(self) -> None:
        closure = load_strict_json(repository_root() / "fixtures/canonical/activation-v2-task-closure-v1.json")
        proposal = propose_task_closure(
            self.runtime,
            closure,
            proposal_id="closure-proposal:10000000-0000-4000-8000-000000000307",
            created_at="2026-07-24T03:00:01Z",
        )
        self.assertEqual(proposal.project_write_state, "pending_review")
        self.assertEqual(proposal.global_outbox_state, "pending_review")
        self.assertFalse(proposal.authority_write)
        self.assertFalse(proposal.global_write)
        review = review_closure_proposal(
            self.runtime,
            proposal,
            decision="approved_for_later_transaction",
            review_id="proposal-review:10000000-0000-4000-8000-000000000308",
            reviewed_at="2026-07-24T03:00:02Z",
        )
        self.assertEqual(review.decision, "approved_for_later_transaction")
        self.assertFalse(review.authority_write)
        self.assertFalse(review.global_write)
        self.assertTrue((self.runtime.root / "outbox" / "closure-proposals").is_dir())
        self.assertFalse((self.runtime.root / "projects" / "v2-fixture" / "authority.json").exists())

    def test_unsafe_closure_and_non_assisted_capture_policy_fail_closed_without_proposal(self) -> None:
        closure = load_strict_json(repository_root() / "fixtures/canonical/activation-v2-task-closure-v1.json")
        closure["prompt"] = "V2_PROMPT_INJECTION_SENTINEL"
        with self.assertRaises(SchemaValidationError) as raised:
            propose_task_closure(
                self.runtime,
                closure,
                proposal_id="closure-proposal:10000000-0000-4000-8000-000000000315",
            )
        self.assertNotIn("V2_PROMPT_INJECTION_SENTINEL", str(raised.exception))
        self.assertFalse((self.runtime.root / "outbox" / "closure-proposals" / "10000000-0000-4000-8000-000000000315.json").exists())

        off_runtime = initialize_disposable_runtime(
            self.sandbox / "off-runtime",
            PolicyInputs.fixture_recommended_defaults(capture_default="OFF"),
            created_at="2026-07-24T03:02:00Z",
        )
        clean_closure = load_strict_json(repository_root() / "fixtures/canonical/activation-v2-task-closure-v1.json")
        with self.assertRaises(ActivationV2MemoryError):
            propose_task_closure(off_runtime, clean_closure)

    def test_tampered_recovery_proposal_fails_readback_without_exposing_or_reusing_it(self) -> None:
        self._seed()
        proposal = assisted_recovery(
            self.runtime,
            self.fixture["cases"][0]["request"],
            proposal_id="recovery-proposal:10000000-0000-4000-8000-000000000316",
            created_at="2026-07-24T03:03:00Z",
        )
        path = self.runtime.root / "receipts" / "recovery" / "10000000-0000-4000-8000-000000000316.json"
        payload = load_strict_json(path)
        payload["global_write"] = True
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o600)
        with self.assertRaises(IntegrityError):
            load_recovery_proposal(self.runtime, proposal.proposal_id)

    def test_latency_budget_and_rehashed_cross_project_selector_fail_closed(self) -> None:
        self._seed()
        request = self.fixture["cases"][0]["request"]
        delayed_id = "recovery-proposal:10000000-0000-4000-8000-000000000317"
        with patch("second_brain.activation_v2_memory.time.monotonic", side_effect=(10.0, 11.001)):
            with self.assertRaises(ActivationV2MemoryError):
                assisted_recovery(
                    self.runtime,
                    request,
                    proposal_id=delayed_id,
                    created_at="2026-07-24T03:04:00Z",
                )
        self.assertFalse(
            (self.runtime.root / "receipts" / "recovery" / "10000000-0000-4000-8000-000000000317.json").exists()
        )

        proposal = assisted_recovery(
            self.runtime,
            request,
            proposal_id="recovery-proposal:10000000-0000-4000-8000-000000000318",
            created_at="2026-07-24T03:04:01Z",
        )
        rehashed = proposal.to_dict()
        rehashed["included"][0]["record_id"] = (
            "mem:foreign-project:decision:10000000-0000-4000-8000-000000000319"
        )
        rehashed["proposal_digest"] = activation_v2_logical_digest(rehashed, "proposal_digest")
        with self.assertRaises(ActivationV2MemoryError):
            RecoveryProposal.from_value(rehashed)


if __name__ == "__main__":
    unittest.main()
