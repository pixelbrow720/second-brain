from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from second_brain.errors import IntegrityError
from second_brain.practical_v1 import practical_v1_source_tree_digest
from second_brain.practical_v1_rollout import (
    PRACTICAL_BACKUP_RELATIVE,
    PRACTICAL_BRIDGE_RELATIVE,
    PRACTICAL_SKILL_RELATIVE,
    build_practical_v1_packet,
    serialize_practical_v1_packet,
    validate_practical_v1_packet_current,
    verify_practical_v1_packet,
    write_practical_v1_packet,
)
from second_brain.workspace import repository_root, temporary_store


STAGED = repository_root() / "dist/global/practical-v1"


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fake_home(root: Path) -> tuple[Path, bytes]:
    home = root / "codex-home"
    skill = home / PRACTICAL_SKILL_RELATIVE
    skill.parent.mkdir(parents=True)
    before = b"---\nname: pixel-second-brain-workflow\ndescription: Existing policy-only fixture.\n---\n\n# Fixture\n"
    skill.write_bytes(before)
    os.chmod(skill, 0o600)
    return home, before


class PracticalV1RolloutTests(unittest.TestCase):
    def test_packet_is_exact_read_only_and_keeps_strict_gates_open(self) -> None:
        with temporary_store() as temporary:
            home, before = _fake_home(temporary)
            packet = build_practical_v1_packet(home, created_at="2026-07-23T08:00:00Z")

            self.assertEqual(packet["status"], "PENDING_EXACT_USER_APPROVAL")
            self.assertFalse(packet["global_mutation"])
            self.assertEqual(packet["local_gate_state"], "PRACTICAL_V1_READY_FOR_APPROVAL")
            self.assertFalse(packet["strict_release_state"]["provider_attestation_claimed"])
            self.assertEqual(packet["strict_release_state"]["M8"], "in_progress")
            self.assertEqual((home / PRACTICAL_SKILL_RELATIVE).read_bytes(), before)
            self.assertFalse((home / PRACTICAL_BRIDGE_RELATIVE).exists())
            self.assertFalse((home / PRACTICAL_BACKUP_RELATIVE).exists())
            paths = {item["path"] for item in packet["targets"]}
            self.assertIn(PRACTICAL_SKILL_RELATIVE.as_posix(), paths)
            self.assertIn(PRACTICAL_BRIDGE_RELATIVE.as_posix(), paths)
            self.assertIn(PRACTICAL_BACKUP_RELATIVE.as_posix(), paths)
            verify_practical_v1_packet(packet)

    def test_source_bound_wrapper_matches_current_runtime_tree(self) -> None:
        wrapper = (STAGED / PRACTICAL_BRIDGE_RELATIVE).read_text(encoding="utf-8")
        digest, count = practical_v1_source_tree_digest()

        self.assertIn(f'EXPECTED_SOURCE_TREE_SHA256 = "{digest}"', wrapper)
        self.assertGreater(count, 30)

    def test_packet_tamper_and_current_state_drift_fail_closed(self) -> None:
        with temporary_store() as temporary:
            home, _ = _fake_home(temporary)
            packet = build_practical_v1_packet(home, created_at="2026-07-23T08:00:00Z")
            tampered = deepcopy(packet)
            tampered["limitations"] = []
            with self.assertRaises(IntegrityError):
                verify_practical_v1_packet(tampered)

            (home / PRACTICAL_SKILL_RELATIVE).write_text("drift\n", encoding="utf-8")
            os.chmod(home / PRACTICAL_SKILL_RELATIVE, 0o600)
            with self.assertRaises(IntegrityError):
                validate_practical_v1_packet_current(packet, home)

    def test_packet_writer_uses_digest_cas(self) -> None:
        with temporary_store() as temporary:
            home, _ = _fake_home(temporary)
            first = build_practical_v1_packet(home, created_at="2026-07-23T08:00:00Z")
            relative = "artifacts/test-runs/practical-v1-packet-test.json"
            path = write_practical_v1_packet(first, relative)
            self.assertEqual(path.read_bytes(), serialize_practical_v1_packet(first))

            second = build_practical_v1_packet(home, created_at="2026-07-23T08:00:01Z")
            with self.assertRaises(IntegrityError):
                write_practical_v1_packet(second, relative)
            write_practical_v1_packet(
                second,
                relative,
                replace_existing_packet_digest=first["packet_digest"],
            )
            self.assertEqual(json.loads(path.read_text())["packet_digest"], second["packet_digest"])
            path.unlink()

    def test_staged_rollback_restores_skill_and_removes_bridge_in_fake_home(self) -> None:
        with temporary_store() as temporary:
            home, before = _fake_home(temporary)
            packet = build_practical_v1_packet(home, created_at="2026-07-23T08:00:00Z")
            staged_skill = (STAGED / PRACTICAL_SKILL_RELATIVE).read_bytes()
            staged_bridge = (STAGED / PRACTICAL_BRIDGE_RELATIVE).read_bytes()
            staged_rollback = (STAGED / "rollback.py").read_bytes()

            skill = home / PRACTICAL_SKILL_RELATIVE
            skill.write_bytes(staged_skill)
            os.chmod(skill, 0o600)
            bridge = home / PRACTICAL_BRIDGE_RELATIVE
            bridge.parent.mkdir(mode=0o700)
            bridge.write_bytes(staged_bridge)
            os.chmod(bridge, 0o700)
            backup = home / PRACTICAL_BACKUP_RELATIVE
            backup.mkdir(mode=0o700, parents=True)
            (backup / "skill-before.md").write_bytes(before)
            os.chmod(backup / "skill-before.md", 0o600)
            (backup / "rollback.py").write_bytes(staged_rollback)
            os.chmod(backup / "rollback.py", 0o700)
            manifest = {
                "backup_id": "practical-v1-v1",
                "bridge_after_sha256": _sha(staged_bridge),
                "bridge_before_sha256": "absent",
                "bridge_relative_path": PRACTICAL_BRIDGE_RELATIVE.as_posix(),
                "codex_home": str(home.resolve()),
                "rollback_after_sha256": _sha(staged_rollback),
                "schema_version": 1,
                "skill_after_sha256": _sha(staged_skill),
                "skill_before_mode": 0o600,
                "skill_before_sha256": _sha(before),
                "skill_relative_path": PRACTICAL_SKILL_RELATIVE.as_posix(),
            }
            (backup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            os.chmod(backup / "manifest.json", 0o600)
            rollback_digest = "a" * 64

            completed = subprocess.run(
                [
                    sys.executable,
                    str(STAGED / "rollback.py"),
                    "--backup-dir",
                    str(backup),
                    "--rollback-packet-digest",
                    rollback_digest,
                    "--approval-reference",
                    f"approved rollback packet {rollback_digest}",
                ],
                cwd=repository_root(),
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(skill.read_bytes(), before)
            self.assertFalse(bridge.exists())
            self.assertTrue(backup.exists())
            self.assertEqual(packet["rollback"]["refuses_if_post_state_drifted"], True)


if __name__ == "__main__":
    unittest.main()
