from __future__ import annotations

import json
import stat
from pathlib import Path
import tempfile
import unittest

from second_brain.activation_v2_runtime import (
    ActivationV2RuntimeError,
    PolicyInputs,
    RuntimeManifest,
    assert_disposable_runtime_not_tracked,
    create_runtime_backup,
    disposable_json_exists,
    initialize_disposable_runtime,
    load_disposable_runtime,
    read_disposable_json,
    restore_runtime_backup,
    runtime_health,
    write_disposable_json,
)
from second_brain.contracts import validate_named_document
from second_brain.errors import ContentPolicyError, IntegrityError, PathUnsafeError
from second_brain.jsonio import load_strict_json
from second_brain.workspace import repository_root


class ActivationV2A1RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        test_runs = repository_root() / "artifacts" / "test-runs"
        test_runs.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="activation-v2-a1-", dir=test_runs)
        self.sandbox = Path(self.temporary.name)
        self.runtime_root = self.sandbox / "runtime"
        self.policy = PolicyInputs.fixture_recommended_defaults()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _runtime(self):
        return initialize_disposable_runtime(
            self.runtime_root,
            self.policy,
            created_at="2026-07-24T01:00:00Z",
        )

    def test_policy_and_manifest_canonical_fixtures_validate_and_bind_digests(self) -> None:
        root = repository_root()
        policy_document = load_strict_json(root / "fixtures/canonical/activation-v2-policy-inputs-v1.json")
        manifest_document = load_strict_json(root / "fixtures/canonical/activation-v2-runtime-manifest-v1.json")
        validate_named_document("activation-v2-policy-inputs-v1", policy_document)
        validate_named_document("activation-v2-runtime-manifest-v1", manifest_document)
        self.assertEqual(PolicyInputs.from_value(policy_document).to_dict(), policy_document)
        self.assertEqual(RuntimeManifest.from_value(manifest_document).to_dict(), manifest_document)

    def test_only_a_private_ignored_synthetic_runtime_can_initialize(self) -> None:
        runtime = self._runtime()
        self.assertEqual(runtime.manifest.mode, "disposable_synthetic")
        self.assertTrue(runtime.manifest.policy.fixture_only)
        self.assertTrue(all((runtime.root / item).is_dir() for item in runtime.manifest.directories))
        self.assertFalse((repository_root() / "runtime").exists())
        assert_disposable_runtime_not_tracked(runtime)
        for path in (runtime.root, *(runtime.root / item for item in runtime.manifest.directories)):
            self.assertEqual(stat.S_IMODE(path.lstat().st_mode) & 0o077, 0)

    def test_unresolved_or_non_fixture_policy_and_external_root_fail_closed(self) -> None:
        with self.assertRaises(ActivationV2RuntimeError):
            initialize_disposable_runtime(self.runtime_root, PolicyInputs.unresolved())
        self.assertFalse(self.runtime_root.exists())

        with self.assertRaises(PathUnsafeError):
            initialize_disposable_runtime(Path(tempfile.gettempdir()) / "activation-v2-runtime", self.policy)
        self.assertFalse(self.runtime_root.exists())

    def test_symlink_and_nonempty_path_are_rejected_before_runtime_creation(self) -> None:
        target = self.sandbox / "target"
        target.mkdir()
        symlink = self.sandbox / "runtime-link"
        symlink.symlink_to(target, target_is_directory=True)
        with self.assertRaises(PathUnsafeError):
            initialize_disposable_runtime(symlink / "nested", self.policy)

        self.runtime_root.mkdir()
        (self.runtime_root / "keep.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(PathUnsafeError):
            initialize_disposable_runtime(self.runtime_root, self.policy)

    def test_safe_json_write_and_read_are_bounded_and_deny_raw_fields(self) -> None:
        runtime = self._runtime()
        document = {"artifact_type": "synthetic_metadata", "status": "fixture_only"}
        write_disposable_json(runtime, "registry/a1-metadata.json", document)
        self.assertEqual(read_disposable_json(runtime, "registry/a1-metadata.json"), document)
        self.assertTrue(disposable_json_exists(runtime, "registry/a1-metadata.json"))

        with self.assertRaises(ContentPolicyError) as raised:
            write_disposable_json(
                runtime,
                "registry/rejected.json",
                {"raw_transcript": "V2_RAW_TRANSCRIPT_SENTINEL"},
            )
        self.assertIn("FORBIDDEN_RAW_FIELD", raised.exception.reason_codes)
        self.assertFalse(disposable_json_exists(runtime, "registry/rejected.json"))

    def test_backup_restore_reverts_only_synthetic_json_and_keeps_metadata_boundary(self) -> None:
        runtime = self._runtime()
        original = {"artifact_type": "synthetic_metadata", "status": "before_restore"}
        changed = {"artifact_type": "synthetic_metadata", "status": "after_mutation"}
        write_disposable_json(runtime, "registry/a1-metadata.json", original)
        backup = create_runtime_backup(runtime, created_at="2026-07-24T01:00:01Z")
        write_disposable_json(runtime, "registry/a1-metadata.json", changed)
        restore_runtime_backup(runtime, backup)
        self.assertEqual(read_disposable_json(runtime, "registry/a1-metadata.json"), original)
        self.assertTrue((runtime.root / backup.relative_path).is_file())
        self.assertEqual(runtime_health(runtime)["global_mutation"], False)
        self.assertEqual(runtime_health(runtime)["authority_store_opened"], False)

    def test_tampered_backup_is_rejected_before_existing_synthetic_state_is_cleared(self) -> None:
        runtime = self._runtime()
        original = {"artifact_type": "synthetic_metadata", "status": "before_backup"}
        current = {"artifact_type": "synthetic_metadata", "status": "current_state"}
        write_disposable_json(runtime, "registry/a1-metadata.json", original)
        backup = create_runtime_backup(runtime, created_at="2026-07-24T01:00:01Z")
        write_disposable_json(runtime, "registry/a1-metadata.json", current)
        backup_path = runtime.root / backup.relative_path
        payload = load_strict_json(backup_path)
        payload["backup_digest"] = "0" * 64
        backup_path.write_text(json.dumps(payload), encoding="utf-8")
        backup_path.chmod(0o600)
        with self.assertRaises(IntegrityError):
            restore_runtime_backup(runtime, backup)
        self.assertEqual(read_disposable_json(runtime, "registry/a1-metadata.json"), current)

    def test_backup_rejects_a_symlink_or_untrusted_file_without_following_it(self) -> None:
        runtime = self._runtime()
        external = self.sandbox / "outside.json"
        external.write_text("{}", encoding="utf-8")
        (runtime.root / "registry" / "link.json").symlink_to(external)
        with self.assertRaises(PathUnsafeError):
            create_runtime_backup(runtime)

    def test_manifest_tampering_causes_load_to_fail_closed(self) -> None:
        runtime = self._runtime()
        manifest_path = runtime.root / "manifest.json"
        manifest = load_strict_json(manifest_path)
        manifest["mode"] = "persistent"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_path.chmod(0o600)
        with self.assertRaises(IntegrityError):
            load_disposable_runtime(runtime.root)


if __name__ == "__main__":
    unittest.main()
