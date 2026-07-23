"""M8's local M4 evidence must be real, redacted, and corpus-bound."""

from __future__ import annotations

from copy import deepcopy
import json
import unittest

from second_brain.errors import SemanticValidationError
from second_brain.evaluation import load_local_evaluation_corpus
from second_brain.m8_m4_evidence import M4LocalEvaluation, run_m8_m4_local_evaluation


class M8M4EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        corpus = load_local_evaluation_corpus()
        cls.memory_case_ids = tuple(case.case_id for case in corpus.cases if case.suite == "memory")
        cls.result = run_m8_m4_local_evaluation(cls.memory_case_ids)

    def test_all_memory_cases_have_actual_retrieval_provenance_and_context_evidence(self) -> None:
        self.assertTrue(self.result.passed)
        self.assertEqual(tuple(item.case_id for item in self.result.cases), self.memory_case_ids)
        self.assertEqual(len(self.result.cases), 10)
        self.assertTrue(all(item.target_check_passed for item in self.result.cases))
        self.assertTrue(all(item.provenance_check_passed for item in self.result.cases))
        self.assertTrue(all(item.context_check_passed for item in self.result.cases))
        by_scenario = {item.scenario: item for item in self.result.cases}
        self.assertIn("FRESHNESS_PARTIAL", by_scenario["partial_visible"].warning_codes)
        self.assertIn("HISTORICAL_EVIDENCE", by_scenario["historical_visible"].warning_codes)
        self.assertIn("INDEX_INVALID", by_scenario["stale_index_fallback"].warning_codes)
        self.assertGreaterEqual(by_scenario["contradiction_visible"].context_citation_count, 2)
        self.assertGreaterEqual(by_scenario["bounded_omission"].retrieval_omission_count, 1)
        self.assertGreaterEqual(by_scenario["strict_freshness"].context_omission_count, 1)

    def test_receipt_is_deterministic_and_omits_query_source_and_temporary_path_content(self) -> None:
        repeated = run_m8_m4_local_evaluation(self.memory_case_ids)
        self.assertEqual(repeated.result_digest, self.result.result_digest)
        payload = json.dumps(self.result.to_receipt(), sort_keys=True)
        for marker in ("m8m4current", "m8m4partial", "Synthetic M8 M4", "artifacts/test-runs", "http://", "https://"):
            self.assertNotIn(marker, payload)

    def test_receipt_rejects_tampered_case_and_wrong_corpus_order(self) -> None:
        tampered = deepcopy(self.result.to_receipt())
        tampered["cases"][0]["target_check_passed"] = False
        with self.assertRaises(SemanticValidationError):
            M4LocalEvaluation.from_receipt(tampered)
        with self.assertRaises(SemanticValidationError):
            run_m8_m4_local_evaluation(tuple(reversed(self.memory_case_ids)))


if __name__ == "__main__":
    unittest.main()
