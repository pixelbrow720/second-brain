"""The controlled M8 performance fixture must stay local and self-verifying."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import unittest

from second_brain.errors import SemanticValidationError
from second_brain.m8_performance_evidence import (
    M8_PERFORMANCE_SAMPLE_COUNT,
    M8PerformanceMeasurement,
    run_m8_local_performance_measurement,
)


class M8PerformanceEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.result = run_m8_local_performance_measurement()

    def test_paired_local_schedule_and_context_proxy_pass(self) -> None:
        self.assertTrue(self.result.local_contract_passed)
        self.assertEqual(self.result.sample_count, M8_PERFORMANCE_SAMPLE_COUNT)
        self.assertEqual(self.result.parallel_faster_sample_count, self.result.sample_count)
        self.assertGreaterEqual(self.result.parallel_max_active_workers, 2)
        self.assertEqual(self.result.serial_max_active_workers, 1)
        self.assertEqual(self.result.raw_intermediate_token_count, 1440)
        self.assertEqual(self.result.integrator_context_tokens, 385)
        self.assertEqual(self.result.context_reduction_percent, 73)
        self.assertEqual(
            (self.result.global_target_count, self.result.material_actions_started, self.result.network_requests_started),
            (0, 0, 0),
        )
        self.assertFalse(self.result.live_attested)

    def test_receipt_round_trip_and_tampering_are_rejected(self) -> None:
        receipt = self.result.to_receipt()
        parsed = M8PerformanceMeasurement.from_receipt(receipt)
        self.assertEqual(parsed, self.result)

        tampered = deepcopy(receipt)
        tampered["parallel_max_active_workers"] = 1
        with self.assertRaises(SemanticValidationError):
            M8PerformanceMeasurement.from_receipt(tampered)

        with self.assertRaises(SemanticValidationError):
            replace(self.result, live_attested=True, result_digest="")


if __name__ == "__main__":
    unittest.main()
