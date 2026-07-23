from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from second_brain.canonical import sha256_hex
from second_brain.errors import AuthorityDeniedError, IntegrityError
from second_brain.global_rollout import M9_REQUIRED_PROFILE_PAIRS, apply_m9_packet, build_m9_packet
from second_brain.live_shadow import (
    M9_SHADOW_REQUEST_COUNT,
    run_initial_shadow,
    serialize_initial_shadow_receipt,
    verify_initial_shadow_receipt,
    write_initial_shadow_receipt,
)
from second_brain.workspace import repository_root


class M9InitialShadowTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=repository_root() / "artifacts" / "test-runs")
        self.addCleanup(temporary.cleanup)
        self.temporary_root = Path(temporary.name)
        self.home = self.temporary_root / "codex-home"
        self.home.mkdir()
        (self.home / "AGENTS.md").write_text("# Personal guidance\n", encoding="utf-8")
        (self.home / "config.toml").write_text(
            "\n".join(
                (
                    'model = "cx/gpt-5.6-terra"',
                    'model_provider = "cx_account"',
                    "[agents]",
                    "enabled = true",
                    "[model_providers.cx_account]",
                    'base_url = "http://localhost:20128/private/path"',
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
        self.packet = build_m9_packet(self.home, created_at="2026-07-23T01:23:45Z")
        apply_m9_packet(self.packet, self.home, approval_reference=self.packet["packet_digest"])
        self.approval_reference = f"user-approved-{self.packet['packet_digest']}"

    @staticmethod
    def _success_events() -> bytes:
        return b"\n".join(
            (
                b'{"type":"thread.started"}',
                b'{"type":"turn.started"}',
                b'{"type":"item.completed","item":{"type":"agent_message","text":"M9_PUBLIC_SHADOW_OK"}}',
                b'{"type":"turn.completed"}',
            )
        ) + b"\n"

    def test_runs_all_approved_shadows_without_retaining_raw_output(self) -> None:
        calls: list[list[str]] = []

        def runner(arguments: list[str], scratch: Path, timeout_seconds: int) -> subprocess.CompletedProcess[bytes]:
            self.assertTrue(scratch.is_dir())
            self.assertEqual(timeout_seconds, 90)
            self.assertIn('model_provider="cx_account"', arguments)
            self.assertIn("--ephemeral", arguments)
            self.assertIn("read-only", arguments)
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, stdout=self._success_events(), stderr=b"DO_NOT_LEAK")

        payload = run_initial_shadow(
            self.packet,
            self.home,
            approval_reference=self.approval_reference,
            process_runner=runner,
        )
        payload["receipt_digest"] = sha256_hex(payload)
        verify_initial_shadow_receipt(payload)
        serialized = serialize_initial_shadow_receipt(payload)

        self.assertEqual(len(calls), M9_SHADOW_REQUEST_COUNT)
        self.assertEqual(payload["shadow_execution_status"], "PASS")
        self.assertEqual(payload["promotion_status"], "BLOCKED_MISSING_AUTHENTICATED_ROUTE_TELEMETRY")
        self.assertEqual(payload["execution"]["passed_shadow_count"], M9_SHADOW_REQUEST_COUNT)
        self.assertNotIn(b"M9_PUBLIC_SHADOW_OK", serialized)
        self.assertNotIn(b"DO_NOT_LEAK", serialized)
        self.assertNotIn(b"private/path", serialized)

    def test_stops_immediately_when_a_tool_event_is_observed(self) -> None:
        calls: list[list[str]] = []
        tool_events = b"\n".join(
            (
                b'{"type":"thread.started"}',
                b'{"type":"item.completed","item":{"type":"function_call","name":"do_not_retain"}}',
                b'{"type":"turn.completed"}',
            )
        ) + b"\n"

        def runner(arguments: list[str], _scratch: Path, _timeout_seconds: int) -> subprocess.CompletedProcess[bytes]:
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, stdout=tool_events, stderr=b"")

        payload = run_initial_shadow(
            self.packet,
            self.home,
            approval_reference=self.approval_reference,
            process_runner=runner,
        )
        payload["receipt_digest"] = sha256_hex(payload)
        verify_initial_shadow_receipt(payload)

        self.assertEqual(len(calls), 1)
        self.assertEqual(payload["shadow_execution_status"], "FAIL")
        self.assertEqual(payload["execution"]["tool_call_count"], 1)
        self.assertEqual(payload["request_results"][0]["reason_codes"], ["TOOL_ACTIVITY_DETECTED", "RESPONSE_CONTRACT_FAILED"])

    def test_requires_the_current_approval_reference(self) -> None:
        with self.assertRaises(AuthorityDeniedError):
            run_initial_shadow(
                self.packet,
                self.home,
                approval_reference="wrong-packet",
                process_runner=lambda *_arguments: subprocess.CompletedProcess([], 0, stdout=b"", stderr=b""),
            )

    def test_receipt_never_replaces_different_existing_evidence(self) -> None:
        def runner(arguments: list[str], _scratch: Path, _timeout_seconds: int) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(arguments, 0, stdout=self._success_events(), stderr=b"")

        payload = run_initial_shadow(
            self.packet,
            self.home,
            approval_reference=self.approval_reference,
            process_runner=runner,
        )
        payload["receipt_digest"] = sha256_hex(payload)
        relative_output = (self.temporary_root / "receipt.json").relative_to(repository_root()).as_posix()
        first = write_initial_shadow_receipt(payload, relative_output)
        self.assertEqual(write_initial_shadow_receipt(payload, relative_output), first)

        changed = dict(payload)
        changed["recorded_at"] = "2026-07-23T01:23:46Z"
        changed["receipt_digest"] = sha256_hex({key: value for key, value in changed.items() if key != "receipt_digest"})
        with self.assertRaises(IntegrityError):
            write_initial_shadow_receipt(changed, relative_output)


if __name__ == "__main__":
    unittest.main()
