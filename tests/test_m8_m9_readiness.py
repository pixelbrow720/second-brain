"""M8/M9 readiness must bind current local and shadow evidence without promotion."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from second_brain.canonical import sha256_hex
from second_brain.errors import IntegrityError, SemanticValidationError
from second_brain.m8_m9_readiness import (
    build_m8_m9_readiness_checkpoint,
    validate_m8_m9_readiness_checkpoint,
    verify_current_m8_m9_readiness_checkpoint,
    write_m8_m9_readiness_checkpoint,
)
from second_brain.workspace import repository_root


class M8M9ReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.checkpoint = build_m8_m9_readiness_checkpoint()

    def test_checkpoint_records_verified_transport_but_not_route_promotion(self) -> None:
        validate_m8_m9_readiness_checkpoint(self.checkpoint)
        self.assertEqual(self.checkpoint["checkpoint_status"], "BLOCKED")
        self.assertEqual(self.checkpoint["promotion_status"], "BLOCKED_MISSING_AUTHENTICATED_ROUTE_TELEMETRY")
        self.assertEqual(self.checkpoint["m8_local_evidence"]["artifact_count"], 8)
        self.assertEqual(self.checkpoint["m9_initial_shadow"]["passed_shadow_count"], 50)
        self.assertFalse(self.checkpoint["m9_initial_shadow"]["live_attested"])
        gates = {item["gate_id"]: item for item in self.checkpoint["gates"]}
        self.assertEqual(gates["gate:m8-local-evidence"]["state"], "PASS")
        self.assertEqual(gates["gate:m9-initial-shadow-transport"]["state"], "PASS")
        self.assertEqual(gates["gate:authenticated-route-telemetry"]["state"], "BLOCKED")

    def test_closed_shape_rejects_self_hashed_gate_promotion(self) -> None:
        tampered = deepcopy(self.checkpoint)
        tampered["gates"][2]["state"] = "PASS"
        tampered["checkpoint_digest"] = sha256_hex(
            {key: value for key, value in tampered.items() if key != "checkpoint_digest"}
        )
        with self.assertRaises(SemanticValidationError):
            validate_m8_m9_readiness_checkpoint(tampered)

    def test_current_verifier_rejects_a_source_bound_tamper(self) -> None:
        with tempfile.TemporaryDirectory(dir=repository_root() / "artifacts" / "test-runs") as temporary:
            target = Path(temporary) / "readiness.json"
            relative_target = target.relative_to(repository_root()).as_posix()
            write_m8_m9_readiness_checkpoint(self.checkpoint, relative_target)
            self.assertEqual(
                verify_current_m8_m9_readiness_checkpoint(relative_target)["checkpoint_digest"],
                self.checkpoint["checkpoint_digest"],
            )

            tampered = deepcopy(self.checkpoint)
            source = tampered["source_binding"]
            source["m9_shadow_receipt_digest"] = "a" * 64
            source["source_binding_digest"] = sha256_hex(
                {
                    "m8_verification_output_digest": source["m8_verification_output_digest"],
                    "m8_release_output_digest": source["m8_release_output_digest"],
                    "m9_packet_digest": source["m9_packet_digest"],
                    "m9_shadow_receipt_digest": source["m9_shadow_receipt_digest"],
                }
            )
            tampered["checkpoint_digest"] = sha256_hex(
                {key: value for key, value in tampered.items() if key != "checkpoint_digest"}
            )
            target.write_text(json.dumps(tampered, sort_keys=True), encoding="utf-8")
            with self.assertRaises(IntegrityError):
                verify_current_m8_m9_readiness_checkpoint(relative_target)


if __name__ == "__main__":
    unittest.main()
