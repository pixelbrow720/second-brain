from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from second_brain import evaluation as evaluation_module
from second_brain.admission import AdmissionDecision, Lane
from second_brain.canonical import sha256_hex
from second_brain.errors import SemanticValidationError
from second_brain.evaluation import (
    BaselineKind,
    EvaluationCase,
    ExecutionMode,
    GateState,
    M8_SEED,
    compare_runs,
    create_candidate_control_state,
    create_local_run_binding,
    grade_case,
    load_local_evaluation_corpus,
    load_local_execution_plans,
    load_serialized_receipt,
    make_synthetic_run,
    rehearse_local_rollback,
    run_local_canary,
    run_local_evaluation,
    serialize_receipt,
)
from second_brain.jsonio import load_strict_json, loads_strict_json
from second_brain.workspace import repository_root


class EvaluationM8Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = repository_root()
        cls.corpus = load_local_evaluation_corpus()
        cls.plans = load_local_execution_plans(cls.corpus)
        # This executes M5/M6/M7 for all baseline modes once for the class.
        cls.result = run_local_evaluation(cls.corpus)

    def test_fixed_corpus_is_strict_split_and_plan_bound(self) -> None:
        self.assertEqual(len(self.corpus.frozen_cases), 49)
        self.assertEqual(len(self.corpus.rotating_cases), 21)
        self.assertEqual(self.corpus.manifest.case_count, 70)
        self.assertEqual(self.corpus.manifest.seed, M8_SEED)
        self.assertEqual(dict(self.corpus.manifest.language_counts), {"id": 28, "mixed": 21, "en": 21})
        self.assertEqual(self.corpus.manifest.anti_research_trap_count, 21)
        self.assertEqual(len(self.plans.plans), 70)
        for suite in ("admission", "routing", "capability", "graph", "memory", "security", "end_to_end"):
            self.assertEqual(sum(case.suite == suite for case in self.corpus.frozen_cases), 7)
            self.assertEqual(sum(case.suite == suite for case in self.corpus.rotating_cases), 3)

    def test_case_schema_rejects_unknown_and_unsafe_fixture_data(self) -> None:
        raw = load_strict_json(self.root / "fixtures/canonical/evaluation-case-v1.json")
        unknown = deepcopy(raw)
        unknown["unknown"] = True
        with self.assertRaises(SemanticValidationError):
            EvaluationCase.from_value(unknown)

        unsafe = deepcopy(raw)
        unsafe["input"]["prompt"] = "SECRET synthetic marker"
        with self.assertRaises(SemanticValidationError):
            EvaluationCase.from_value(unsafe)

        with self.assertRaises(Exception):
            loads_strict_json('{"version":1,"version":1}')

    def test_grader_uses_actual_m5_m6_m7_evidence_not_caller_status(self) -> None:
        candidate = self.result.candidate
        direct = next(
            (case, outcome)
            for case, outcome in zip(candidate.cases, candidate.outcomes)
            if outcome.component_evidence.admission.lane is Lane.DIRECT
        )
        graph = next(
            (case, outcome)
            for case, outcome in zip(candidate.cases, candidate.outcomes)
            if outcome.component_evidence.admission.lane in {Lane.GRAPH, Lane.DEEP}
        )
        direct_case, direct_outcome = direct
        graph_case, graph_outcome = graph
        self.assertIs(grade_case(direct_case, direct_outcome).state, GateState.PASS)
        self.assertIs(grade_case(graph_case, graph_outcome).state, GateState.PASS)
        self.assertIsNotNone(graph_outcome.component_evidence.route_receipt)
        self.assertIsNotNone(graph_outcome.component_evidence.graph_receipt)

        # A caller cannot turn a direct no-route result into a MATCH string.
        with self.assertRaises(SemanticValidationError):
            replace(direct_outcome, route_status="MATCH")

        evidence = graph_outcome.component_evidence
        cloned_admission = AdmissionDecision.from_value(evidence.admission.to_dict())
        # M5 issuance is identity-bound to the original audit trail.
        with self.assertRaises(SemanticValidationError):
            replace(evidence, admission=cloned_admission)
        # Missing M6 and M7 evidence fail before grading can see a green status.
        with self.assertRaises(SemanticValidationError):
            replace(evidence, route_receipt=None)
        with self.assertRaises(SemanticValidationError):
            replace(evidence, graph_receipt=None)

    def test_component_baselines_are_bound_and_semantic_quality_is_deferred(self) -> None:
        binding = create_local_run_binding(self.corpus, self.plans)
        candidate = make_synthetic_run(
            self.corpus, binding, run_id="run:test-candidate", baseline_kind=None, mode=ExecutionMode.CANDIDATE.value,
            plans=self.plans,
        )
        b0 = make_synthetic_run(
            self.corpus,
            binding,
            run_id="run:test-b0",
            baseline_kind=BaselineKind.B0_ROOT_TERA_MAX,
            mode=ExecutionMode.B0_ROOT.value,
            plans=self.plans,
        )
        b1 = make_synthetic_run(
            self.corpus,
            binding,
            run_id="run:test-b1",
            baseline_kind=BaselineKind.B1_SERIAL_WORKFLOW,
            mode=ExecutionMode.B1_SERIAL.value,
            plans=self.plans,
        )
        b2 = make_synthetic_run(
            self.corpus,
            binding,
            run_id="run:test-b2",
            baseline_kind=BaselineKind.B2_NO_MEMORY,
            mode=ExecutionMode.B2_NO_MEMORY.value,
            plans=self.plans,
        )
        b3 = make_synthetic_run(
            self.corpus,
            binding,
            run_id="run:test-b3",
            baseline_kind=BaselineKind.B3_LAST_KNOWN_GOOD,
            mode=ExecutionMode.B3_LAST_KNOWN_GOOD.value,
            plans=self.plans,
        )
        comparison = compare_runs(candidate, b0, b1, b2, b3)
        self.assertIs(comparison.state, GateState.BLOCKED)
        with self.assertRaises(SemanticValidationError):
            replace(b0, outcomes=b0.outcomes[:-1])

        self.assertTrue(self.result.passed)
        gates = {gate.gate_id: gate for gate in self.result.gates}
        self.assertIs(gates["gate:component-execution"].state, GateState.PASS)
        self.assertIs(gates["gate:graph-serial-fallback"].state, GateState.PASS)
        self.assertIs(gates["gate:m4-fixture-evidence"].state, GateState.PASS)
        self.assertTrue(self.result.m4_evaluation.passed)
        self.assertIs(gates["gate:quality-lcb"].state, GateState.DEFERRED_TO_M9)
        self.assertIs(gates["gate:graph-efficiency"].state, GateState.DEFERRED_TO_M9)
        self.assertIs(gates["gate:memory-context"].state, GateState.DEFERRED_TO_M9)

    def test_component_binding_covers_m4_and_performance_runner_sources(self) -> None:
        binding = create_local_run_binding(self.corpus, self.plans)
        original_digest = evaluation_module._safe_file_digest
        for source_path in (
            "src/second_brain/m8_m4_evidence.py",
            "src/second_brain/m8_performance_evidence.py",
        ):
            with self.subTest(source_path=source_path):
                with patch(
                    "second_brain.evaluation._safe_file_digest",
                    side_effect=lambda path: "f" * 64 if path == source_path else original_digest(path),
                ):
                    changed = create_local_run_binding(self.corpus, self.plans)
                self.assertNotEqual(changed.repository_snapshot_digest, binding.repository_snapshot_digest)
                self.assertNotEqual(changed.component_source_digest, binding.component_source_digest)

    def test_canary_is_synthetic_and_rollback_applies_candidate_then_restores_b3(self) -> None:
        canary = run_local_canary()
        self.assertIs(canary.state, GateState.PASS)
        self.assertEqual((canary.route_check_count, canary.route_match_count), (35, 35))
        self.assertEqual((canary.shadow_check_count, canary.shadow_match_count), (50, 50))
        self.assertEqual((canary.side_effects_started, canary.global_target_count, canary.live_request_count), (0, 0, 0))
        self.assertIsNone(canary.control_state_digest)
        self.assertIsNone(canary.b3_snapshot_digest)

        candidate = create_candidate_control_state(self.result.candidate)
        b3_snapshot = self.result.b3_rollback_snapshot
        self.assertEqual(b3_snapshot.b3_run_digest, self.result.b3_baseline.run_digest)
        self.assertEqual(b3_snapshot.binding_digest, self.result.b3_baseline.binding.binding_digest)
        self.assertEqual(
            b3_snapshot.baseline_outcome_digest,
            sha256_hex({"outcome_digests": [outcome.outcome_digest for outcome in self.result.b3_baseline.outcomes]}),
        )
        rehearsal = rehearse_local_rollback(
            candidate,
            b3_snapshot=b3_snapshot,
            expected_candidate_digest=candidate.digest,
        )
        self.assertIs(rehearsal.state, GateState.PASS)
        self.assertTrue(rehearsal.candidate_applied)
        self.assertEqual(rehearsal.applied_digest, candidate.digest)
        self.assertNotEqual(rehearsal.candidate_digest, rehearsal.b3_digest)
        self.assertTrue(rehearsal.exact_equality)
        self.assertEqual(rehearsal.restored_digest, rehearsal.b3_digest)
        self.assertEqual(rehearsal.b3_snapshot.snapshot_digest, b3_snapshot.snapshot_digest)
        self.assertEqual(rehearsal.post_restore_canary_control_state_digest, rehearsal.restored_digest)
        self.assertEqual(rehearsal.post_restore_canary_b3_snapshot_digest, b3_snapshot.snapshot_digest)
        self.assertIs(rehearsal.post_restore_canary_state, GateState.PASS)
        with self.assertRaises(SemanticValidationError):
            run_local_canary(control_state=candidate, b3_snapshot=b3_snapshot)
        with self.assertRaises(SemanticValidationError):
            rehearse_local_rollback(candidate, b3_snapshot=b3_snapshot, expected_candidate_digest="a" * 64)

    def test_b3_snapshot_rejects_candidate_relabel_and_unbound_receipt_projection(self) -> None:
        with self.assertRaises(SemanticValidationError):
            replace(
                self.result.candidate,
                run_id="run:forged-b3",
                baseline_kind=BaselineKind.B3_LAST_KNOWN_GOOD,
                run_digest="",
            )

        forged_evaluation = evaluation_module._evaluation_payload(self.result)
        forged_snapshot = forged_evaluation["b3_rollback_snapshot"]
        self.assertIsInstance(forged_snapshot, dict)
        forged_snapshot["baseline_receipt_digest"] = "a" * 64
        forged_snapshot["snapshot_digest"] = sha256_hex(
            {key: value for key, value in forged_snapshot.items() if key != "snapshot_digest"}
        )
        with self.assertRaises(SemanticValidationError):
            evaluation_module._validate_typed_receipt("m8-local-evaluation.json", forged_evaluation)

    def test_generic_or_forged_receipts_are_rejected(self) -> None:
        with self.assertRaises(SemanticValidationError):
            serialize_receipt("m8-local-evaluation.json", {"schema_version": 1})

        forged = {
            "schema_version": 1,
            "milestone": "M8",
            "release_status": "PASS",
            "promotion_status": "LOCAL_READY",
        }
        forged["output_digest"] = sha256_hex(forged)
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            target = Path(temporary) / "m8-release-report.json"
            target.write_text(json.dumps(forged, sort_keys=True), encoding="utf-8")
            with patch("second_brain.evaluation._receipt_target", return_value=target):
                with self.assertRaises(SemanticValidationError):
                    load_serialized_receipt("m8-release-report.json")

        # A fully typed and self-hashed but unknown receipt is still user-owned.
        valid_release = load_serialized_receipt("m8-release-report.json")
        forged_typed = deepcopy(valid_release)
        forged_typed["rollback_limitation"] = "forged-local-receipt"
        forged_serialized = json.dumps(
            {**forged_typed, "output_digest": sha256_hex(forged_typed)}, sort_keys=True
        )
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            target = Path(temporary) / "m8-release-report.json"
            target.write_text(forged_serialized, encoding="utf-8")
            with patch("second_brain.evaluation._receipt_target", return_value=target):
                self.assertEqual(load_serialized_receipt("m8-release-report.json"), forged_typed)
                with self.assertRaises(SemanticValidationError):
                    evaluation_module._write_typed_receipt("m8-release-report.json", valid_release)
                self.assertEqual(target.read_text(encoding="utf-8"), forged_serialized)

    def test_receipt_redaction_bounds_reject_oversized_nested_values(self) -> None:
        release = load_serialized_receipt("m8-release-report.json")
        mutations = (
            ("oversized list", lambda payload: payload.__setitem__("unresolved_risks", ["safe"] * 513)),
            ("oversized mapping", lambda payload: payload.__setitem__(
                "boundary_assertions", {f"field_{index}": False for index in range(129)}
            )),
            ("oversized string", lambda payload: payload.__setitem__("rollback_limitation", "x" * 257)),
            ("oversized integer", lambda payload: payload["local_gate_matrix"][0].__setitem__(
                "numerator", 1_000_000_001
            )),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                tampered = deepcopy(release)
                mutate(tampered)
                with self.assertRaises(SemanticValidationError):
                    evaluation_module._validate_typed_receipt("m8-release-report.json", tampered)


if __name__ == "__main__":
    unittest.main()
