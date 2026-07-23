from __future__ import annotations

import unittest

from second_brain.profiles import (
    APPROVED_PROFILE_ALIASES,
    PROFILE_REGISTRY_VERSION,
    load_profile_registry,
    resolve_profile,
    validate_profile_registry_semantics,
)
from second_brain.contracts import validate_named_document
from second_brain.errors import SchemaValidationError, SemanticValidationError
from second_brain.workspace import repository_root
from second_brain.jsonio import load_strict_json


class ProfileRegistryTests(unittest.TestCase):
    def test_registry_has_exactly_the_seven_approved_aliases(self) -> None:
        registry = load_profile_registry()
        self.assertEqual(set(registry["profiles"]), set(APPROVED_PROFILE_ALIASES))
        self.assertEqual(registry["default_root_profile"], "tera-max")

    def test_every_adapter_is_explicitly_pinned_and_preserves_logical_effort(self) -> None:
        registry = load_profile_registry()
        self.assertEqual(registry["registry_version"], PROFILE_REGISTRY_VERSION)
        observed_models = {
            "cx/gpt-5.6-terra",
            "cx/gpt-5.6-sol",
            "cx/gpt-5.6-luna",
            "cx/gpt-5.5",
        }
        for alias in APPROVED_PROFILE_ALIASES:
            with self.subTest(alias=alias):
                adapter = registry["profiles"][alias]["adapters"]["nine_router"]
                self.assertIn(adapter["raw_model_slug"], observed_models)
                self.assertEqual(adapter["serialized_effort_field"], "model_reasoning_effort")
                self.assertEqual(adapter["serialized_effort_value"], registry["profiles"][alias]["effort_intent"])
                self.assertEqual(adapter["adapter_version"], "9router/0.5.40")
                self.assertEqual(adapter["client_version"], "codex-cli/0.144.6")
                self.assertEqual(adapter["mapping_evidence"], "synthetic-local")

    def test_canonical_fixture_matches_the_authoritative_registry(self) -> None:
        root = repository_root()
        fixture = load_strict_json(root / "fixtures/canonical/model-profile-registry-v2.json")
        registry = load_strict_json(root / "config/model-profiles.json")
        self.assertEqual(fixture, registry)

    def test_v1_is_preserved_as_history_but_cannot_be_active_routing_authority(self) -> None:
        root = repository_root()
        historical = load_strict_json(root / "fixtures/canonical/model-profile-registry-v1.json")
        validate_named_document("model-profile-registry-v1", historical)
        with self.assertRaises(SemanticValidationError):
            validate_profile_registry_semantics(historical)

    def test_boolean_does_not_satisfy_registry_version_const(self) -> None:
        registry = load_profile_registry()
        registry["registry_version"] = True
        with self.assertRaises(SchemaValidationError):
            validate_named_document("model-profile-registry-v2", registry)

    def test_adapter_rejects_an_unpinned_or_drifted_mapping(self) -> None:
        registry = load_profile_registry()
        registry["profiles"]["tera-max"]["adapters"]["nine_router"]["serialized_effort_value"] = "xhigh"
        with self.assertRaises(SemanticValidationError):
            validate_profile_registry_semantics(registry)

    def test_resolver_uses_the_active_single_registry_snapshot(self) -> None:
        registry = load_profile_registry()
        resolved = resolve_profile("sol-max", registry)
        self.assertEqual(resolved.raw_model_slug, registry["profiles"]["sol-max"]["adapters"]["nine_router"]["raw_model_slug"])
        self.assertEqual(resolved.serialized_effort_value, "max")
        with self.assertRaises(SemanticValidationError):
            resolve_profile("unapproved-profile", registry)


if __name__ == "__main__":
    unittest.main()
