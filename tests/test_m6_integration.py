from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import unittest
import tomllib

from second_brain.canonical import sha256_bytes
from second_brain.errors import PathUnsafeError, SemanticValidationError
from second_brain.profiles import load_profile_registry
from second_brain.routing import (
    StagingFile,
    StagingManifest,
    capture_last_known_good,
    prepare_local_rollback,
    rollback_staging,
    stage_profile_toml,
    validate_staging,
)
from second_brain.workspace import repository_root


class M6IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = load_profile_registry()
        self.manifest = stage_profile_toml(registry=self.registry)
        self.target = repository_root() / "dist/global/m6"

    def tearDown(self) -> None:
        # Restore the known deterministic fixture tree after every drift exercise.
        stage_profile_toml(registry=self.registry)

    def test_generated_toml_is_parseable_explicit_and_rooted_at_tera_max(self) -> None:
        self.assertEqual(self.manifest.target, "dist/global/m6")
        self.assertEqual(len(self.manifest.files), 8)
        root = tomllib.loads((self.target / "root.toml").read_text(encoding="utf-8"))
        self.assertEqual(root["default_root_profile"], "tera-max")
        self.assertEqual(root["model"], "cx/gpt-5.6-terra")
        self.assertEqual(root["model_reasoning_effort"], "max")
        for alias, profile in self.registry["profiles"].items():
            with self.subTest(alias=alias):
                generated = tomllib.loads(
                    (self.target / "agents" / f"{alias}.toml").read_text(encoding="utf-8")
                )
                adapter = profile["adapters"]["nine_router"]
                self.assertEqual(generated["profile_alias"], alias)
                self.assertEqual(generated["model"], adapter["raw_model_slug"])
                self.assertEqual(
                    generated["model_reasoning_effort"],
                    adapter["serialized_effort_value"],
                )
        self.assertEqual(validate_staging(registry=self.registry), self.manifest)

    def test_staging_rejects_manual_drift_foreign_files_and_nonlocal_destinations(self) -> None:
        agent = self.target / "agents/tera-high.toml"
        original = agent.read_bytes()
        agent.write_bytes(original + b"# manual drift\n")
        with self.assertRaises(SemanticValidationError):
            validate_staging(registry=self.registry)
        agent.write_bytes(original)

        foreign = self.target / "unmanaged.txt"
        foreign.write_text("do not overwrite", encoding="utf-8")
        try:
            with self.assertRaises(PathUnsafeError):
                validate_staging(registry=self.registry)
            with self.assertRaises(PathUnsafeError):
                stage_profile_toml(registry=self.registry)
        finally:
            foreign.unlink()
        with self.assertRaises(PathUnsafeError):
            stage_profile_toml(registry=self.registry, target=Path("/tmp/not-m6-staging"))

    def test_staging_rejects_a_symlink_without_following_it(self) -> None:
        link = self.target / "agents/escape.toml"
        os.symlink("/tmp", link)
        try:
            with self.assertRaises(PathUnsafeError):
                validate_staging(registry=self.registry)
        finally:
            link.unlink()

    def test_last_known_good_rollback_restores_exact_local_staging_after_digest_check(self) -> None:
        snapshot = capture_last_known_good(registry=self.registry)
        baseline_files = dict(snapshot.files)
        candidate_files = dict(snapshot.files)
        candidate_agent = "agents/tera-high.toml"
        candidate_files[candidate_agent] += b"# candidate-only formatting\n"
        candidate_manifest = StagingManifest(
            staging_version=snapshot.receipt.receipt_version,
            target=snapshot.receipt.target,
            registry_version=snapshot.receipt.registry_version,
            registry_digest=snapshot.receipt.registry_digest,
            adapter_version=snapshot.receipt.adapter_version,
            normalization_rules_version=snapshot.receipt.normalization_rules_version,
            files=tuple(
                StagingFile(relative_path=path, sha256=sha256_bytes(content))
                for path, content in sorted(candidate_files.items())
                if path != "manifest.json"
            ),
        )
        candidate_files["manifest.json"] = (
            json.dumps(candidate_manifest.to_dict(), ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        (self.target / candidate_agent).write_bytes(candidate_files[candidate_agent])
        (self.target / "manifest.json").write_bytes(candidate_files["manifest.json"])

        plan = prepare_local_rollback(
            snapshot,
            expected_candidate_manifest_digest=candidate_manifest.manifest_digest,
        )
        result = rollback_staging(plan, registry=self.registry)
        self.assertTrue(result.restored)
        self.assertTrue(result.canary.passed)
        self.assertEqual(result.manifest_digest, snapshot.receipt.manifest_digest)
        actual = {
            path.relative_to(self.target).as_posix(): path.read_bytes()
            for path in self.target.rglob("*")
            if path.is_file()
        }
        self.assertEqual(actual, baseline_files)

    def test_rollback_rejects_tampered_candidate_digest_and_unsafe_receipt_target(self) -> None:
        snapshot = capture_last_known_good(registry=self.registry)
        plan = prepare_local_rollback(
            snapshot,
            expected_candidate_manifest_digest="0" * 64,
        )
        with self.assertRaises(SemanticValidationError):
            rollback_staging(plan, registry=self.registry)
        with self.assertRaises(PathUnsafeError):
            replace(snapshot.receipt, target="~/.codex")


if __name__ == "__main__":
    unittest.main()
