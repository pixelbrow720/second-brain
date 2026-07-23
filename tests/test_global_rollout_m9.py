from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from second_brain.errors import AuthorityDeniedError, IntegrityError
from second_brain.canonical import sha256_bytes
from second_brain.global_rollout import (
    M9_REQUIRED_PROFILE_PAIRS,
    M9_SKILL_RELATIVE,
    apply_m9_packet,
    build_m9_packet,
    build_m9_rollback_approval_packet,
    load_m9_rollback_approval_packet,
    rollback_m9_packet,
    serialize_m9_packet,
    serialize_m9_rollback_approval_packet,
    validate_m9_packet_current,
    validate_m9_rollback_approval_packet_current,
    write_m9_packet,
    write_m9_rollback_approval_packet,
)
from second_brain.workspace import repository_root


class M9GlobalRolloutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=repository_root() / "artifacts" / "test-runs")
        self.home = Path(self.temporary.name) / "codex-home"
        self.home.mkdir()
        self.agents_path = self.home / "AGENTS.md"
        self.original_agents = b"# Personal guidance\nDO_NOT_LEAK_GLOBAL_GUIDANCE\n"
        self.agents_path.write_bytes(self.original_agents)
        (self.home / "config.toml").write_text(
            "\n".join(
                (
                    'model = "cx/gpt-5.6-terra"',
                    'model_provider = "test-router"',
                    "[agents]",
                    "enabled = true",
                    "[mcp_servers.local]",
                    "[model_providers.test-router]",
                    'base_url = "https://user:password@router.example.test:443/private/path?token=do-not-leak"',
                    'env_key = "DO_NOT_LEAK_ENV_NAME"',
                    'wire_api = "responses"',
                    "",
                )
            ),
            encoding="utf-8",
        )
        agents_directory = self.home / "agents"
        agents_directory.mkdir()
        for index, (model, effort) in enumerate(sorted(M9_REQUIRED_PROFILE_PAIRS)):
            (agents_directory / f"agent-{index}.toml").write_text(
                "\n".join(
                    (
                        f'name = "agent-{index}"',
                        'description = "bounded test agent"',
                        'developer_instructions = "Use bounded test behavior."',
                        f'model = "{model}"',
                        f'model_reasoning_effort = "{effort}"',
                        "",
                    )
                ),
                encoding="utf-8",
            )
        (self.home / "skills").mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_packet_is_redacted_exact_and_current_without_a_global_mutation(self) -> None:
        packet = build_m9_packet(self.home, created_at="2026-07-23T01:23:45Z")
        serialized = serialize_m9_packet(packet).decode("utf-8")
        self.assertEqual(packet["status"], "PENDING_EXACT_USER_APPROVAL")
        self.assertFalse(packet["global_mutation"])
        self.assertNotIn("DO_NOT_LEAK_GLOBAL_GUIDANCE", serialized)
        self.assertNotIn("DO_NOT_LEAK_ENV_NAME", serialized)
        self.assertNotIn("private/path", serialized)
        self.assertEqual(packet["profile_precondition"]["missing_model_effort_pairs"], [])
        route = packet["network_and_data_impact"]["configured_model_routes"][0]
        self.assertEqual(route["base_url"]["host"], "router.example.test")
        self.assertTrue(route["has_env_key_reference"])
        paths = {target["path"] for target in packet["targets"]}
        self.assertIn("AGENTS.md", paths)
        self.assertIn(M9_SKILL_RELATIVE.as_posix(), paths)
        self.assertIn("config.toml", paths)
        self.assertTrue(all(target["after_sha256"] for target in packet["targets"]))
        self.assertIn("src/second_brain/global_rollout.py", packet["tooling_sha256"])
        self.assertIn("AGENTS.md", packet["approval_request"])
        self.assertIn(M9_SKILL_RELATIVE.as_posix(), packet["approval_request"])
        self.assertIn("50-request", packet["approval_request"])
        validate_m9_packet_current(packet, self.home)
        self.assertEqual(self.agents_path.read_bytes(), self.original_agents)

    def test_preflight_rejects_any_observed_target_drift(self) -> None:
        packet = build_m9_packet(self.home, created_at="2026-07-23T01:23:45Z")
        (self.home / "agents" / "agent-0.toml").write_text(
            'name = "agent-0"\ndescription = "changed"\ndeveloper_instructions = "changed"\nmodel = "cx/gpt-5.5"\nmodel_reasoning_effort = "xhigh"\n',
            encoding="utf-8",
        )
        with self.assertRaises(IntegrityError):
            validate_m9_packet_current(packet, self.home)

    def test_packet_output_requires_an_explicit_generated_packet_cas_digest_to_replace(self) -> None:
        first = build_m9_packet(self.home, created_at="2026-07-23T01:23:45Z")
        output = Path(self.temporary.name) / "packet.json"
        relative_output = output.relative_to(repository_root()).as_posix()
        write_m9_packet(first, relative_output)
        replacement = build_m9_packet(self.home, created_at="2026-07-23T01:24:45Z")
        with self.assertRaises(IntegrityError):
            write_m9_packet(replacement, relative_output)
        write_m9_packet(
            replacement,
            relative_output,
            replace_existing_packet_digest=first["packet_digest"],
        )
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["packet_digest"], replacement["packet_digest"])

    def test_apply_requires_approval_then_rolls_back_only_managed_state(self) -> None:
        packet = build_m9_packet(self.home, created_at="2026-07-23T01:23:45Z")
        with self.assertRaises(AuthorityDeniedError):
            apply_m9_packet(packet, self.home, approval_reference="")
        with self.assertRaises(AuthorityDeniedError):
            apply_m9_packet(packet, self.home, approval_reference="user-approved-wrong-packet")

        applied = apply_m9_packet(packet, self.home, approval_reference=f"user-approved-{packet['packet_digest']}")
        self.assertEqual(applied["status"], "APPLIED")
        self.assertIn(b"pixel-second-brain-v1:start", self.agents_path.read_bytes())
        skill_path = self.home / M9_SKILL_RELATIVE
        self.assertTrue(skill_path.is_file())
        backup = self.home / "second-brain-backups" / "m9-v1"
        self.assertEqual((backup / "AGENTS.md").read_bytes(), self.original_agents)
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["agents_before_sha256"], packet["targets"][0]["before_sha256"])

        rollback_packet = build_m9_rollback_approval_packet(
            packet,
            self.home,
            created_at="2026-07-23T01:24:45Z",
        )
        with self.assertRaises(AuthorityDeniedError):
            rollback_m9_packet(
                packet,
                self.home,
                rollback_approval_packet=rollback_packet,
                approval_reference=packet["packet_digest"],
            )
        rolled_back = rollback_m9_packet(
            packet,
            self.home,
            rollback_approval_packet=rollback_packet,
            approval_reference=f"user-approved-{rollback_packet['packet_digest']}",
        )
        self.assertEqual(rolled_back["status"], "ROLLED_BACK")
        self.assertEqual(rolled_back["rollback_packet_digest"], rollback_packet["packet_digest"])
        self.assertEqual(self.agents_path.read_bytes(), self.original_agents)
        self.assertFalse(skill_path.exists())
        self.assertTrue((backup / "rollback.py").is_file())
        targets = {target["path"]: target for target in packet["targets"]}
        for relative in ("AGENTS.md", "manifest.json", "rollback.py"):
            backup_path = backup / relative
            target = targets[(Path("second-brain-backups") / "m9-v1" / relative).as_posix()]
            self.assertEqual(sha256_bytes(backup_path.read_bytes()), target["after_sha256"])
            self.assertEqual(backup_path.stat().st_mode & 0o777, target["after_mode"])

    def test_rollback_refuses_to_remove_a_user_modified_skill(self) -> None:
        packet = build_m9_packet(self.home, created_at="2026-07-23T01:23:45Z")
        apply_m9_packet(packet, self.home, approval_reference=packet["packet_digest"])
        rollback_packet = build_m9_rollback_approval_packet(packet, self.home, created_at="2026-07-23T01:24:45Z")
        skill_path = self.home / M9_SKILL_RELATIVE
        skill_path.write_text("user-owned change\n", encoding="utf-8")
        with self.assertRaises(IntegrityError):
            rollback_m9_packet(
                packet,
                self.home,
                rollback_approval_packet=rollback_packet,
                approval_reference=rollback_packet["packet_digest"],
            )
        self.assertIn(b"pixel-second-brain-v1:start", self.agents_path.read_bytes())

    def test_standalone_backup_rollback_restores_the_exact_before_state(self) -> None:
        packet = build_m9_packet(self.home, created_at="2026-07-23T01:23:45Z")
        apply_m9_packet(packet, self.home, approval_reference=packet["packet_digest"])
        backup = self.home / "second-brain-backups" / "m9-v1"
        result = subprocess.run(
            ["python3", str(backup / "rollback.py"), "--backup-dir", str(backup)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "ROLLED_BACK")
        self.assertEqual(self.agents_path.read_bytes(), self.original_agents)
        self.assertFalse((self.home / M9_SKILL_RELATIVE).exists())

    def test_rollback_refuses_before_changing_guidance_when_skill_directory_has_foreign_file(self) -> None:
        packet = build_m9_packet(self.home, created_at="2026-07-23T01:23:45Z")
        apply_m9_packet(packet, self.home, approval_reference=packet["packet_digest"])
        rollback_packet = build_m9_rollback_approval_packet(packet, self.home, created_at="2026-07-23T01:24:45Z")
        skill_path = self.home / M9_SKILL_RELATIVE
        (skill_path.parent / "user-note.txt").write_text("keep me", encoding="utf-8")
        with self.assertRaises(IntegrityError):
            rollback_m9_packet(
                packet,
                self.home,
                rollback_approval_packet=rollback_packet,
                approval_reference=rollback_packet["packet_digest"],
            )
        self.assertIn(b"pixel-second-brain-v1:start", self.agents_path.read_bytes())
        self.assertTrue(skill_path.is_file())

    def test_rollback_packet_is_redacted_current_and_requires_explicit_cas_replacement(self) -> None:
        packet = build_m9_packet(self.home, created_at="2026-07-23T01:23:45Z")
        apply_m9_packet(packet, self.home, approval_reference=packet["packet_digest"])
        rollback_packet = build_m9_rollback_approval_packet(packet, self.home, created_at="2026-07-23T01:24:45Z")
        serialized = serialize_m9_rollback_approval_packet(rollback_packet).decode("utf-8")
        self.assertEqual(rollback_packet["status"], "PENDING_EXACT_USER_APPROVAL")
        self.assertTrue(rollback_packet["global_mutation"])
        self.assertIn(packet["packet_digest"], rollback_packet["approval_request"])
        self.assertNotIn("DO_NOT_LEAK_GLOBAL_GUIDANCE", serialized)
        self.assertNotIn("DO_NOT_LEAK_ENV_NAME", serialized)
        self.assertNotIn("private/path", serialized)
        validate_m9_rollback_approval_packet_current(rollback_packet, packet, self.home)

        output = Path(self.temporary.name) / "rollback-packet.json"
        relative_output = output.relative_to(repository_root()).as_posix()
        write_m9_rollback_approval_packet(rollback_packet, relative_output)
        self.assertEqual(
            load_m9_rollback_approval_packet(relative_output)["packet_digest"], rollback_packet["packet_digest"]
        )
        replacement = build_m9_rollback_approval_packet(packet, self.home, created_at="2026-07-23T01:25:45Z")
        with self.assertRaises(IntegrityError):
            write_m9_rollback_approval_packet(replacement, relative_output)
        write_m9_rollback_approval_packet(
            replacement,
            relative_output,
            replace_existing_packet_digest=rollback_packet["packet_digest"],
        )

    def test_rollback_cli_requires_a_fresh_packet_and_bound_approval(self) -> None:
        packet = build_m9_packet(self.home, created_at="2026-07-23T01:23:45Z")
        apply_m9_packet(packet, self.home, approval_reference=packet["packet_digest"])
        initial_path = Path(self.temporary.name) / "initial-packet.json"
        rollback_path = Path(self.temporary.name) / "rollback-packet.json"
        initial_relative = initial_path.relative_to(repository_root()).as_posix()
        rollback_relative = rollback_path.relative_to(repository_root()).as_posix()
        write_m9_packet(packet, initial_relative)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(repository_root() / "src")
        prepare = subprocess.run(
            [
                sys.executable,
                str(repository_root() / "scripts/prepare_m9_rollback_approval_packet.py"),
                "--codex-home",
                str(self.home),
                "--initial-packet",
                initial_relative,
                "--output",
                rollback_relative,
                "--created-at",
                "2026-07-23T01:24:45Z",
            ],
            check=False,
            cwd=repository_root(),
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(prepare.returncode, 0, prepare.stderr)
        rollback_packet = load_m9_rollback_approval_packet(rollback_relative)
        self.assertNotIn("DO_NOT_LEAK_GLOBAL_GUIDANCE", rollback_path.read_text(encoding="utf-8"))

        rejected = subprocess.run(
            [
                sys.executable,
                str(repository_root() / "scripts/run_m9_rollout.py"),
                "--codex-home",
                str(self.home),
                "--packet",
                initial_relative,
                "rollback",
                "--rollback-approval-packet",
                rollback_relative,
                "--approval-reference",
                packet["packet_digest"],
            ],
            check=False,
            cwd=repository_root(),
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn(b"pixel-second-brain-v1:start", self.agents_path.read_bytes())

        accepted = subprocess.run(
            [
                sys.executable,
                str(repository_root() / "scripts/run_m9_rollout.py"),
                "--codex-home",
                str(self.home),
                "--packet",
                initial_relative,
                "rollback",
                "--rollback-approval-packet",
                rollback_relative,
                "--approval-reference",
                f"approved {rollback_packet['packet_digest']}",
            ],
            check=False,
            cwd=repository_root(),
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(json.loads(accepted.stdout)["status"], "ROLLED_BACK")
        self.assertEqual(self.agents_path.read_bytes(), self.original_agents)


if __name__ == "__main__":
    unittest.main()
