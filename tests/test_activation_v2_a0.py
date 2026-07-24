from __future__ import annotations

import json
import unittest

from second_brain.activation_v2 import (
    A0_CANONICAL_FIXTURES,
    A0_SCHEMA_NAMES,
    activation_v2_logical_digest,
    validate_activation_v2_fixture_bundle,
)
from second_brain.contracts import load_schema_registry, validate_named_document
from second_brain.errors import ContentPolicyError, SchemaValidationError, SemanticValidationError
from second_brain.jsonio import load_strict_json
from second_brain.workspace import repository_root


class ActivationV2A0ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = repository_root()

    def _fixture(self, name: str) -> dict[str, object]:
        return load_strict_json(self.root / A0_CANONICAL_FIXTURES[name])

    def _bundle(self) -> dict[str, dict[str, object]]:
        return {name: self._fixture(name) for name in A0_SCHEMA_NAMES}

    def test_registry_accepts_every_canonical_a0_contract_and_bundle(self) -> None:
        registry_names = {entry["name"] for entry in load_schema_registry()["schemas"]}
        self.assertTrue(A0_SCHEMA_NAMES.issubset(registry_names))
        bundle = self._bundle()
        for name, document in bundle.items():
            with self.subTest(contract=name):
                validate_named_document(name, document)
        validate_activation_v2_fixture_bundle(bundle)

    def test_canonical_contracts_are_explicitly_non_authorizing(self) -> None:
        receipt = self._fixture("activation-v2-capture-receipt-v1")
        outbox = self._fixture("activation-v2-promotion-outbox-v1")
        route = self._fixture("activation-v2-route-intent-v1")
        snapshot = self._fixture("activation-v2-graph-snapshot-v1")
        self.assertEqual(receipt["execution_mode"], "fixture_only")
        self.assertEqual(receipt["write_authority"], "none")
        self.assertFalse(receipt["global_write_committed"])
        self.assertEqual(outbox["review_state"], "pending_review")
        self.assertFalse(outbox["global_write_committed"])
        self.assertEqual(route["prompt_persistence"], "forbidden")
        self.assertEqual(route["session_action"], "none")
        self.assertTrue(snapshot["derived_only"])
        self.assertFalse((self.root / "runtime").exists())

    def test_public_synthetic_threat_fixture_covers_required_a0_threats(self) -> None:
        fixture = load_strict_json(self.root / "fixtures/activation-v2/a0-threat-cases-v1.json")
        self.assertEqual(fixture["fixture_version"], 1)
        self.assertEqual(fixture["data_class"], "PUBLIC_SYNTHETIC")
        self.assertEqual(
            {case["case_id"] for case in fixture["cases"]},
            {
                "secret-leakage",
                "raw-transcript",
                "prompt-injection",
                "cross-project-leakage",
                "edge-cycle",
                "route-mismatch",
            },
        )

    def test_sensitive_and_transcript_markers_fail_closed_without_echoing_content(self) -> None:
        cases = (
            ("V2_SECRET_SENTINEL", "SECRET_DETECTED"),
            ("V2_RAW_TRANSCRIPT_SENTINEL", "RAW_TRANSCRIPT_DETECTED"),
            ("V2_PROMPT_INJECTION_SENTINEL", "PROMPT_INJECTION_DETECTED"),
        )
        for marker, reason in cases:
            with self.subTest(marker=marker):
                closure = self._fixture("activation-v2-task-closure-v1")
                closure["decisions"][0]["summary"] = marker
                with self.assertRaises(ContentPolicyError) as raised:
                    validate_named_document("activation-v2-task-closure-v1", closure)
                self.assertIn(reason, raised.exception.reason_codes)
                self.assertNotIn(marker, str(raised.exception))

    def test_raw_transcript_and_prompt_fields_are_rejected_by_the_allowlist(self) -> None:
        for field, marker in (
            ("raw_transcript", "V2_RAW_TRANSCRIPT_SENTINEL"),
            ("prompt", "V2_PROMPT_INJECTION_SENTINEL"),
            ("assistant_output", "V2_RAW_TRANSCRIPT_SENTINEL"),
        ):
            with self.subTest(field=field):
                closure = self._fixture("activation-v2-task-closure-v1")
                closure[field] = marker
                with self.assertRaises(SchemaValidationError) as raised:
                    validate_named_document("activation-v2-task-closure-v1", closure)
                self.assertNotIn(marker, str(raised.exception))

    def test_absolute_path_in_a_permitted_summary_is_rejected(self) -> None:
        closure = self._fixture("activation-v2-task-closure-v1")
        marker = "file:///v2-private-marker"
        closure["questions"][0]["summary"] = marker
        with self.assertRaises(ContentPolicyError) as raised:
            validate_named_document("activation-v2-task-closure-v1", closure)
        self.assertIn("ABSOLUTE_PATH_DETECTED", raised.exception.reason_codes)
        self.assertNotIn(marker, str(raised.exception))

    def test_cross_project_sources_and_nodes_are_rejected(self) -> None:
        outbox = self._fixture("activation-v2-promotion-outbox-v1")
        outbox["source_objects"][0]["id"] = (
            "mem:foreign-project:evidence:10000000-0000-4000-8000-000000000001"
        )
        with self.assertRaises(SemanticValidationError):
            validate_named_document("activation-v2-promotion-outbox-v1", outbox)

        snapshot = self._fixture("activation-v2-graph-snapshot-v1")
        snapshot["nodes"][0]["id"] = (
            "mem:foreign-project:decision:10000000-0000-4000-8000-000000000008"
        )
        with self.assertRaises(SemanticValidationError):
            validate_named_document("activation-v2-graph-snapshot-v1", snapshot)

    def test_part_of_and_supersedes_cycles_are_rejected_before_the_snapshot_is_accepted(self) -> None:
        for relation in ("part_of", "supersedes"):
            with self.subTest(relation=relation):
                snapshot = self._fixture("activation-v2-graph-snapshot-v1")
                decision_id = snapshot["nodes"][0]["id"]
                evidence_id = snapshot["nodes"][1]["id"]
                snapshot["edges"].extend(
                    (
                        {
                            "edge_id": "edge:10000000-0000-4000-8000-000000000014",
                            "relation": relation,
                            "source_id": decision_id,
                            "target_id": evidence_id,
                            "source_revision": 1,
                            "target_revision": 1,
                            "cross_store": False,
                            "provenance_ids": ["prov:10000000-0000-4000-8000-000000000015"],
                            "cross_store_provenance": None,
                        },
                        {
                            "edge_id": "edge:10000000-0000-4000-8000-000000000016",
                            "relation": relation,
                            "source_id": evidence_id,
                            "target_id": decision_id,
                            "source_revision": 1,
                            "target_revision": 1,
                            "cross_store": False,
                            "provenance_ids": ["prov:10000000-0000-4000-8000-000000000017"],
                            "cross_store_provenance": None,
                        },
                    )
                )
                with self.assertRaisesRegex(SemanticValidationError, relation):
                    validate_named_document("activation-v2-graph-snapshot-v1", snapshot)

    def test_profile_effort_mismatch_and_prompt_body_fail_closed(self) -> None:
        route = self._fixture("activation-v2-route-intent-v1")
        route["profile_alias"] = "tera-high"
        with self.assertRaisesRegex(SemanticValidationError, "effort"):
            validate_named_document("activation-v2-route-intent-v1", route)

        route = self._fixture("activation-v2-route-intent-v1")
        marker = "V2_RAW_TRANSCRIPT_SENTINEL"
        route["prompt_body"] = marker
        with self.assertRaises(SchemaValidationError) as raised:
            validate_named_document("activation-v2-route-intent-v1", route)
        self.assertNotIn(marker, str(raised.exception))

    def test_digest_tampering_is_rejected_for_outbox_route_and_graph(self) -> None:
        checks = (
            ("activation-v2-promotion-outbox-v1", "proposal_digest"),
            ("activation-v2-route-intent-v1", "intent_digest"),
            ("activation-v2-graph-snapshot-v1", "snapshot_digest"),
        )
        for name, field in checks:
            with self.subTest(contract=name):
                document = self._fixture(name)
                document[field] = "0" * 64
                with self.assertRaises(SemanticValidationError):
                    validate_named_document(name, document)

    def test_bundle_detects_receipt_count_or_outbox_source_drift(self) -> None:
        bundle = self._bundle()
        bundle["activation-v2-capture-receipt-v1"]["candidate_counts"]["evidence"] = 0
        with self.assertRaises(SemanticValidationError):
            validate_activation_v2_fixture_bundle(bundle)

        bundle = self._bundle()
        bundle["activation-v2-promotion-outbox-v1"]["source_objects"][0]["id"] = (
            "mem:v2-fixture:decision:10000000-0000-4000-8000-000000000008"
        )
        bundle["activation-v2-promotion-outbox-v1"]["proposal_digest"] = activation_v2_logical_digest(
            bundle["activation-v2-promotion-outbox-v1"], "proposal_digest"
        )
        with self.assertRaises(SemanticValidationError):
            validate_activation_v2_fixture_bundle(bundle)

    def test_fixture_payloads_are_json_objects_not_hidden_serialized_transcripts(self) -> None:
        for name, document in self._bundle().items():
            with self.subTest(contract=name):
                encoded = json.dumps(document, sort_keys=True)
                self.assertNotIn("raw_transcript", encoded)
                self.assertNotIn("prompt_body", encoded)
                self.assertNotIn("assistant_output", encoded)


if __name__ == "__main__":
    unittest.main()
