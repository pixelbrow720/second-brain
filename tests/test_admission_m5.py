from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
import unittest

from second_brain.admission import (
    AdmissionDecision,
    AdmissionEngine,
    AdmissionFeatures,
    AuditEvent,
    Lane,
    AuditTrail,
    escalate_admission,
)
from second_brain.clock import DeterministicClock
from second_brain.errors import StorageError


TASK_ID = "task:00000000-0000-4000-8000-000000000005"


class AdmissionM5Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = DeterministicClock(datetime(2026, 7, 22, 5, tzinfo=UTC))
        self._event_number = 6

        def next_event_id() -> str:
            value = f"00000000-0000-4000-8000-{self._event_number:012d}"
            self._event_number += 1
            return value

        self.audit = AuditTrail(clock=self.clock, id_factory=next_event_id)
        self.engine = AdmissionEngine(audit_trail=self.audit)

    def test_direct_fixtures_are_r0_and_have_no_audit_ceremony(self) -> None:
        for fixture in ("basic calculus", "stable definition", "casual greeting", "small local edit"):
            with self.subTest(fixture=fixture):
                decision = self.engine.admit(AdmissionFeatures())
                self.assertEqual(decision.lane, Lane.DIRECT)
                self.assertEqual(decision.retrieval_tier, "R0")
                self.assertFalse(decision.audit_required)
                self.assertEqual(decision.reason_codes, ("SELF_CONTAINED_LOW_RISK",))
                self.assertEqual(self.audit.events, ())

    def test_admission_precedence_is_deterministic(self) -> None:
        cases = (
            (AdmissionFeatures(high_risk=True), Lane.DEEP, ("HIGH_RISK",)),
            (AdmissionFeatures(explicit_lane=Lane.DEEP), Lane.DEEP, ("USER_EXPLICIT_DEEP",)),
            (
                AdmissionFeatures(parallel_branches=2, multi_artifact=True),
                Lane.GRAPH,
                ("INDEPENDENT_BRANCHES",),
            ),
            (AdmissionFeatures(explicit_lane=Lane.GRAPH), Lane.GRAPH, ("USER_EXPLICIT_GRAPH",)),
            (AdmissionFeatures(workspace_bound=True), Lane.ASSISTED, ("BOUNDED_TOOL_OR_EVIDENCE_NEED",)),
            (AdmissionFeatures(freshness_required="targeted"), Lane.ASSISTED, ("BOUNDED_TOOL_OR_EVIDENCE_NEED",)),
            (AdmissionFeatures(ambiguity=True), Lane.ASSISTED, ("BOUNDED_TOOL_OR_EVIDENCE_NEED",)),
        )
        for features, expected_lane, expected_reasons in cases:
            with self.subTest(features=features):
                left = self.engine.admit(features, task_id=TASK_ID)
                right = self.engine.admit(features, task_id=TASK_ID)
                self.assertEqual(left.to_dict(), right.to_dict())
                self.assertEqual(left.lane, expected_lane)
                self.assertEqual(left.reason_codes, expected_reasons)

    def test_lower_user_preference_cannot_downgrade_high_risk(self) -> None:
        decision = self.engine.admit(
            AdmissionFeatures(explicit_lane=Lane.DIRECT, requested_permissions=("install-executable",)),
            task_id=TASK_ID,
        )
        self.assertEqual(decision.lane, Lane.DEEP)
        self.assertEqual(decision.reason_codes, ("HIGH_RISK", "USER_LOWER_LANE_NOT_APPLIED"))
        self.assertTrue(decision.audit_required)
        self.assertEqual(len(self.audit.events), 1)

    def test_graph_needs_independent_branches_or_explicit_request(self) -> None:
        no_benefit = self.engine.admit(
            AdmissionFeatures(parallel_branches=1, multi_artifact=True, parallel_savings_ms=500, orchestration_overhead_ms=1),
            task_id=TASK_ID,
        )
        self.assertEqual(no_benefit.lane, Lane.DIRECT)
        benefit = self.engine.admit(
            AdmissionFeatures(parallel_branches=2, parallel_savings_ms=501, orchestration_overhead_ms=500),
            task_id=TASK_ID,
        )
        self.assertEqual(benefit.lane, Lane.GRAPH)

    def test_late_risk_only_escalates(self) -> None:
        initial = self.engine.admit(AdmissionFeatures())
        escalated = escalate_admission(initial, AdmissionFeatures(high_risk=True), task_id=TASK_ID, audit_trail=self.audit)
        self.assertEqual(escalated.lane, Lane.DEEP)
        self.assertIn("ESCALATED_NEW_RISK", escalated.reason_codes)
        self.assertEqual([event.event_type for event in self.audit.events], ["admission_escalated"])
        self.assertEqual(escalate_admission(escalated, AdmissionFeatures()).lane, Lane.DEEP)

    def test_non_direct_admission_fails_closed_when_audit_is_unavailable(self) -> None:
        with self.assertRaises(StorageError) as blocked:
            AdmissionEngine().admit(AdmissionFeatures(workspace_bound=True), task_id=TASK_ID)
        self.assertEqual(blocked.exception.code, "AUDIT_FAILED")

        with self.assertRaises(StorageError) as no_task:
            self.engine.admit(AdmissionFeatures(workspace_bound=True))
        self.assertEqual(no_task.exception.code, "SCHEMA_INVALID")

    def test_mapping_rejects_unknown_or_permissive_values(self) -> None:
        with self.assertRaises(StorageError) as unknown:
            AdmissionFeatures.from_value({"unknown": True})
        self.assertEqual(unknown.exception.code, "SCHEMA_INVALID")
        with self.assertRaises(StorageError):
            AdmissionFeatures.from_value({"parallel_branches": True})
        with self.assertRaises(StorageError):
            AdmissionFeatures.from_value({"freshness_required": "everywhere"})
        with self.assertRaises(StorageError):
            AdmissionFeatures.from_value({"risk_flags": "install"})
        with self.assertRaises(StorageError):
            AdmissionFeatures.from_value({"risk_flags": ["unclassified_risk"]})

    def test_decision_mapping_is_digest_bound(self) -> None:
        decision = self.engine.admit(AdmissionFeatures(workspace_bound=True), task_id=TASK_ID)
        restored = AdmissionDecision.from_value(decision.to_dict())
        self.assertEqual(restored.to_dict(), decision.to_dict())
        tampered = decision.to_dict()
        tampered["lane"] = Lane.DEEP.value
        with self.assertRaises(StorageError):
            AdmissionDecision.from_value(tampered)

    def test_audit_is_allowlisted_and_redacts_untrusted_values(self) -> None:
        marker = "SB_AUDIT_SECRET_MARKER"
        decision = self.engine.admit(AdmissionFeatures(workspace_bound=True), task_id=TASK_ID)
        event = self.audit.events[-1]
        encoded = json.dumps(event.to_dict(), sort_keys=True)
        self.assertEqual(decision.lane, Lane.ASSISTED)
        self.assertNotIn(marker, encoded)
        self.assertNotIn("prompt", event.to_dict())
        self.assertTrue(event.verify())
        self.assertEqual(event.admission_digest, decision.decision_digest)
        with self.assertRaises(StorageError):
            replace(event, target_digest="0" * 64)

    def test_audit_event_type_and_event_ids_are_strict(self) -> None:
        self.engine.admit(AdmissionFeatures(workspace_bound=True), task_id=TASK_ID)
        event = self.audit.events[-1]
        with self.assertRaises(StorageError):
            AuditEvent.from_value({**event.to_dict(), "status": "blocked"})
        with self.assertRaises(StorageError):
            AuditEvent.from_value({**event.to_dict(), "event_digest": "0" * 64})
        duplicate = AuditTrail(
            clock=self.clock,
            id_factory=lambda: "00000000-0000-4000-8000-000000000099",
        )
        duplicate_engine = AdmissionEngine(audit_trail=duplicate)
        duplicate_engine.admit(AdmissionFeatures(workspace_bound=True), task_id=TASK_ID)
        with self.assertRaises(StorageError) as collision:
            duplicate_engine.admit(AdmissionFeatures(high_risk=True), task_id=TASK_ID)
        self.assertEqual(collision.exception.code, "AUDIT_FAILED")


if __name__ == "__main__":
    unittest.main()
