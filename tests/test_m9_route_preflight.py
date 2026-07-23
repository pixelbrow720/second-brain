from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from second_brain.canonical import sha256_bytes, sha256_hex
from second_brain.errors import AuthorityDeniedError, IntegrityError
from second_brain.global_rollout import M9_REQUIRED_PROFILE_PAIRS, apply_m9_packet, build_m9_packet
from second_brain.live_shadow import run_initial_shadow
from second_brain.m9_external_evidence import (
    ExternalTrustPolicy,
    ROUTE_ATTESTATION_PURPOSE,
    RouteAttestationApprovalIntent,
    RouteAttestationPlan,
    build_route_attestation_approval_packet,
    create_route_expectation,
)
from second_brain.m9_route_preflight import preflight_route_attestation_batch
from second_brain.profiles import load_profile_registry, registry_digest, resolve_profile
from second_brain.workspace import repository_root


class M9RoutePreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=repository_root() / "artifacts" / "test-runs")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "codex-home"
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
        self.initial_packet = build_m9_packet(self.home, created_at="2026-07-23T01:23:45Z")
        apply_m9_packet(self.initial_packet, self.home, approval_reference=self.initial_packet["packet_digest"])
        self.initial_shadow = run_initial_shadow(
            self.initial_packet,
            self.home,
            approval_reference=self.initial_packet["packet_digest"],
            process_runner=self._success_runner,
        )
        self.initial_shadow["receipt_digest"] = sha256_hex(self.initial_shadow)
        self.allowed_signers = self.root / "allowed-signers"
        self.allowed_signers.write_bytes(
            b"test:route-authority ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixedPublicAnchor\n"
        )
        self.intent = RouteAttestationApprovalIntent(
            intent_version=1,
            initial_m9_packet_digest=self.initial_packet["packet_digest"],
            initial_shadow_receipt_digest=self.initial_shadow["receipt_digest"],
            provider_id="cx_account",
        )
        registry = load_profile_registry()
        profile = resolve_profile("tera-max", registry)
        self.plan = RouteAttestationPlan(
            plan_version=1,
            approval_intent_digest=self.intent.intent_digest,
            profile_registry_digest=registry_digest(registry),
            provider_id="cx_account",
            expectations=tuple(
                create_route_expectation(
                    correlation_id=f"corr:preflight-{ordinal:04d}-opaque",
                    profile_alias="tera-max",
                    model_identifier=profile.raw_model_slug,
                    effort=profile.serialized_effort_value,
                )
                for ordinal in range(50)
            ),
        )
        self.policy = ExternalTrustPolicy(
            policy_version=1,
            purpose=ROUTE_ATTESTATION_PURPOSE,
            authority_id="test:route-authority",
            verifier_protocol="openssh-detached-proof-v1",
            trust_anchor_fingerprint=sha256_bytes(self.allowed_signers.read_bytes()),
        )
        self.approval_packet = build_route_attestation_approval_packet(
            intent=self.intent,
            plan=self.plan,
            policy=self.policy,
        )

    @staticmethod
    def _success_runner(arguments: list[str], _scratch: Path, _timeout_seconds: int) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=(
                b'{"type":"thread.started"}\n'
                b'{"type":"item.completed","item":{"type":"agent_message","text":"M9_PUBLIC_SHADOW_OK"}}\n'
                b'{"type":"turn.completed"}\n'
            ),
            stderr=b"",
        )

    def _preflight(self, approval_reference: str | None = None) -> dict[str, object]:
        return preflight_route_attestation_batch(
            initial_packet=self.initial_packet,
            initial_shadow=self.initial_shadow,
            intent=self.intent,
            plan=self.plan,
            policy=self.policy,
            approval_packet=self.approval_packet,
            codex_home=self.home,
            allowed_signers_path=self.allowed_signers,
            approval_reference=(
                f"approved {self.approval_packet.packet_digest}"
                if approval_reference is None
                else approval_reference
            ),
        )

    def test_preflight_reads_back_exact_bound_state_without_provider_execution(self) -> None:
        result = self._preflight()

        self.assertEqual(result["status"], "M9_ROUTE_BATCH_PREFLIGHT_PASS")
        self.assertEqual(result["approval_packet_digest"], self.approval_packet.packet_digest)
        self.assertEqual(result["plan_digest"], self.plan.plan_digest)
        self.assertEqual(result["expected_request_count"], 50)
        self.assertEqual(result["global_readback"], {
            "packet_digest": self.initial_packet["packet_digest"],
            "status": "APPLIED_STATE_PASS",
            "target_count": len(self.initial_packet["targets"]),
        })
        self.assertFalse(result["promotion_authorized"])

    def test_preflight_requires_the_new_final_packet_approval(self) -> None:
        with self.assertRaises(AuthorityDeniedError):
            self._preflight("approved initial-packet-only")

    def test_preflight_stops_on_anchor_or_global_post_state_drift(self) -> None:
        self.allowed_signers.write_bytes(b"different public anchor\n")
        with self.assertRaises(IntegrityError):
            self._preflight()

        self.allowed_signers.write_bytes(
            b"test:route-authority ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixedPublicAnchor\n"
        )
        (self.home / "AGENTS.md").write_text("changed after approval\n", encoding="utf-8")
        with self.assertRaises(IntegrityError):
            self._preflight()


if __name__ == "__main__":
    unittest.main()
