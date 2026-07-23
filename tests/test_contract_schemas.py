from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest

from second_brain.contracts import (
    canonical_fixture_path,
    load_schema,
    load_schema_registry,
    schema_asset_path,
    validate_named_document,
)
from second_brain.errors import DuplicateKeyError, SchemaValidationError, SemanticValidationError
from second_brain.graph_runtime import validate_work_graph_manifest
from second_brain.jsonio import load_strict_json
from second_brain.workspace import repository_root


class ContractSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = repository_root()

    def test_generated_assets_are_current(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/build_m0_contract_assets.py", "--check"],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_every_extracted_schema_parses_and_accepts_its_canonical_fixture(self) -> None:
        registry = load_schema_registry()
        self.assertEqual(registry["schema_registry_version"], 1)
        self.assertIn("model-profile-registry-v1", [entry["name"] for entry in registry["schemas"]])
        self.assertIn("model-profile-registry-v2", [entry["name"] for entry in registry["schemas"]])
        self.assertIn("evaluation-case-v1", [entry["name"] for entry in registry["schemas"]])
        for entry in registry["schemas"]:
            with self.subTest(schema=entry["name"]):
                schema = load_schema(entry["name"])
                fixture = load_strict_json(self.root / entry["canonical_fixture"])
                self.assertIn("$schema", schema)
                validate_named_document(entry["name"], fixture)
                if entry["name"] == "work-graph-v1":
                    validate_work_graph_manifest(fixture)

    def test_duplicate_keys_are_rejected_before_schema_validation(self) -> None:
        fixture = self.root / "fixtures/invalid/memory-object-duplicate-key.json"
        with self.assertRaises(DuplicateKeyError):
            load_strict_json(fixture)

    def test_unknown_fields_are_rejected(self) -> None:
        document = load_strict_json(
            self.root / "fixtures/invalid/memory-object-unknown-field.json"
        )
        with self.assertRaises(SchemaValidationError):
            validate_named_document("memory-object-v2", document)

    def test_id_store_and_kind_mismatches_are_rejected(self) -> None:
        for name in (
            "memory-object-id-mismatch.json",
            "memory-object-store-mismatch.json",
            "memory-object-kind-mismatch.json",
        ):
            with self.subTest(fixture=name):
                document = load_strict_json(self.root / "fixtures/invalid" / name)
                with self.assertRaises(SemanticValidationError):
                    validate_named_document("memory-object-v2", document)

    def test_malformed_timestamp_is_rejected(self) -> None:
        document = load_strict_json(
            self.root / "fixtures/invalid/memory-object-malformed-timestamp.json"
        )
        with self.assertRaises(SchemaValidationError):
            validate_named_document("memory-object-v2", document)

    def test_timestamp_order_is_rejected(self) -> None:
        document = load_strict_json(
            self.root / "fixtures/invalid/memory-object-timestamp-order.json"
        )
        with self.assertRaises(SemanticValidationError):
            validate_named_document("memory-object-v2", document)

    def test_malformed_and_mismatched_hashes_are_rejected(self) -> None:
        malformed = load_strict_json(
            self.root / "fixtures/invalid/memory-object-malformed-hash.json"
        )
        with self.assertRaises(SchemaValidationError):
            validate_named_document("memory-object-v2", malformed)

        mismatched = load_strict_json(
            self.root / "fixtures/invalid/memory-object-content-hash-mismatch.json"
        )
        with self.assertRaises(SemanticValidationError):
            validate_named_document("memory-object-v2", mismatched)

    def test_json_boolean_does_not_satisfy_an_integer_const(self) -> None:
        raw_manifest = load_strict_json(
            self.root / "fixtures/canonical/raw-capture-manifest-v1.json"
        )
        raw_manifest["schema_version"] = True
        with self.assertRaises(SchemaValidationError):
            validate_named_document("raw-capture-manifest-v1", raw_manifest)

    def test_schema_registry_only_references_existing_contract_files(self) -> None:
        for entry in load_schema_registry()["schemas"]:
            with self.subTest(schema=entry["name"]):
                self.assertTrue(schema_asset_path(entry["file"]).is_file())
                self.assertTrue(canonical_fixture_path(entry["canonical_fixture"]).is_file())

    def test_registry_assets_cannot_escape_their_owned_directories(self) -> None:
        for resolver in (schema_asset_path, canonical_fixture_path):
            with self.subTest(resolver=resolver.__name__):
                with self.assertRaises(SemanticValidationError):
                    resolver("../README.md")
                with self.assertRaises(SemanticValidationError):
                    resolver("/tmp/outside-repository")


if __name__ == "__main__":
    unittest.main()
