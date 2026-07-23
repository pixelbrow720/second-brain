from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from second_brain.canonical import sha256_bytes, sha256_hex
from second_brain.errors import AuthorityDeniedError, IntegrityError, SemanticValidationError
from second_brain.evaluation import BlindedReviewPacket, M8_SEED
from second_brain.global_rollout import load_m9_packet
from second_brain.jsonio import load_strict_json
from second_brain.live_shadow import verify_initial_shadow_receipt
from second_brain.m9_external_evidence import (
    BLINDED_REVIEW_PURPOSE,
    M9_REVIEW_EVIDENCE_KIND,
    M9_ROUTE_EVIDENCE_KIND,
    ROUTE_ATTESTATION_PURPOSE,
    BlindedReviewIntakePlan,
    ExternalTrustPolicy,
    ExternalVerification,
    ExternalVerificationRequest,
    OpenSshDetachedProofVerifier,
    RouteAttestationApprovalIntent,
    RouteAttestationApprovalPacket,
    RouteAttestationPlan,
    attest_external_routes,
    blinded_review_packet_from_value,
    build_route_attestation_approval_packet,
    build_blinded_review_intake_plan,
    capture_blinded_review_results,
    create_route_expectation,
    promotion_allowed,
)
from second_brain.profiles import load_profile_registry, registry_digest
from second_brain.workspace import repository_root


class _TrustedTestVerifier:
    """Test-only stand-in for a separately operated external verifier."""

    def verify(self, request: ExternalVerificationRequest) -> ExternalVerification:
        return ExternalVerification(
            policy_digest=request.policy.policy_digest,
            purpose=request.policy.purpose,
            payload_digest=request.payload_digest,
            proof_digest=request.proof_digest,
            verifier_id="test:external-verifier",
            verified_at="2026-07-23T02:00:00Z",
            verification_reference_digest=sha256_hex({"payload": request.payload_digest}),
        )


class _InvalidVerifier:
    def verify(self, request: ExternalVerificationRequest) -> ExternalVerification:
        return ExternalVerification(
            policy_digest=request.policy.policy_digest,
            purpose=request.policy.purpose,
            payload_digest="0" * 64,
            proof_digest=request.proof_digest,
            verifier_id="test:external-verifier",
            verified_at="2026-07-23T02:00:00Z",
            verification_reference_digest=sha256_hex({"payload": request.payload_digest}),
        )


class _OpenSshRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], bytes, Path, int]] = []

    def __call__(
        self, arguments: tuple[str, ...], payload: bytes, scratch_directory: Path, timeout_seconds: int
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((arguments, payload, scratch_directory, timeout_seconds))
        return subprocess.CompletedProcess(arguments, 0, b"", b"")


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")


class M9ExternalEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.verifier = _TrustedTestVerifier()
        self.route_policy = ExternalTrustPolicy(
            policy_version=1,
            purpose=ROUTE_ATTESTATION_PURPOSE,
            authority_id="test:route-authority",
            verifier_protocol="openssh-detached-proof-v1",
            trust_anchor_fingerprint="a" * 64,
        )
        self.review_policy = ExternalTrustPolicy(
            policy_version=1,
            purpose=BLINDED_REVIEW_PURPOSE,
            authority_id="test:review-authority",
            verifier_protocol="detached-proof-v1",
            trust_anchor_fingerprint="b" * 64,
        )
        initial_shadow = load_strict_json(repository_root() / "artifacts" / "m9-initial-shadow-report.json")
        self.assertIsInstance(initial_shadow, dict)
        verify_initial_shadow_receipt(initial_shadow)
        self.route_intent = RouteAttestationApprovalIntent(
            intent_version=1,
            initial_m9_packet_digest=load_m9_packet()["packet_digest"],
            initial_shadow_receipt_digest=initial_shadow["receipt_digest"],
            provider_id=initial_shadow["configured_provider"],
        )

    def _route_plan(self) -> tuple[RouteAttestationPlan, list[dict[str, str]]]:
        observations: list[dict[str, str]] = []
        expectations = []
        for ordinal in range(50):
            correlation = f"corr:shadow-{ordinal:04d}-opaque"
            model = "cx/gpt-5.6-terra"
            expectations.append(
                create_route_expectation(
                    correlation_id=correlation,
                    profile_alias="tera-max",
                    model_identifier=model,
                    effort="max",
                )
            )
            observations.append(
                {"correlation_id": correlation, "model_identifier": model, "effort": "max"}
            )
        return (
            RouteAttestationPlan(
                plan_version=1,
                approval_intent_digest=self.route_intent.intent_digest,
                profile_registry_digest=registry_digest(load_profile_registry()),
                provider_id=self.route_intent.provider_id,
                expectations=tuple(expectations),
            ),
            observations,
        )

    def _route_approval_packet(
        self, plan: RouteAttestationPlan, policy: ExternalTrustPolicy
    ) -> RouteAttestationApprovalPacket:
        return build_route_attestation_approval_packet(
            intent=self.route_intent,
            plan=plan,
            policy=policy,
        )

    def test_route_attestation_requires_detached_external_verification_and_redacts_raw_values(self) -> None:
        plan, observations = self._route_plan()
        self.assertEqual(RouteAttestationPlan.from_value(plan.to_dict()), plan)
        self.assertEqual(ExternalTrustPolicy.from_value(self.route_policy.to_dict()), self.route_policy)
        payload = _json_bytes(
            {
                "evidence_kind": M9_ROUTE_EVIDENCE_KIND,
                "plan_digest": plan.plan_digest,
                "provider_id": plan.provider_id,
                "observations": observations,
            }
        )
        receipt = attest_external_routes(
            plan=plan,
            payload=payload,
            detached_proof=b"test-detached-proof",
            policy=self.route_policy,
            verifier=self.verifier,
        )
        self.assertEqual(receipt.attestation_state, "MATCH")
        self.assertTrue(receipt.live_attested)
        self.assertFalse(receipt.promotion_authorized)
        self.assertFalse(promotion_allowed(receipt))
        serialized = json.dumps(receipt.to_dict(), sort_keys=True)
        self.assertNotIn(observations[0]["correlation_id"], serialized)
        self.assertNotIn(observations[0]["model_identifier"], serialized)
        self.assertNotIn("test-detached-proof", serialized)

        # A caller-supplied self-hash is not a detached proof and is not schema-accepted.
        self_hashed = json.loads(payload.decode("utf-8"))
        self_hashed["payload_digest"] = sha256_hex(self_hashed)
        with self.assertRaises(SemanticValidationError):
            attest_external_routes(
                plan=plan,
                payload=_json_bytes(self_hashed),
                detached_proof=b"test-detached-proof",
                policy=self.route_policy,
                verifier=self.verifier,
            )
        with self.assertRaises((AuthorityDeniedError, SemanticValidationError)):
            attest_external_routes(
                plan=plan,
                payload=payload,
                detached_proof=b"",
                policy=self.route_policy,
                verifier=self.verifier,
            )
        with self.assertRaises(AuthorityDeniedError):
            attest_external_routes(
                plan=plan,
                payload=payload,
                detached_proof=b"test-detached-proof",
                policy=self.route_policy,
                verifier=object(),
            )
        with self.assertRaises(IntegrityError):
            attest_external_routes(
                plan=plan,
                payload=payload,
                detached_proof=b"test-detached-proof",
                policy=self.route_policy,
                verifier=_InvalidVerifier(),
            )

    def test_route_mismatch_is_visible_and_duplicate_or_foreign_correlation_is_rejected(self) -> None:
        plan, observations = self._route_plan()
        mismatched = [dict(item) for item in observations]
        mismatched[0]["effort"] = "xhigh"
        receipt = attest_external_routes(
            plan=plan,
            payload=_json_bytes(
                {
                    "evidence_kind": M9_ROUTE_EVIDENCE_KIND,
                    "plan_digest": plan.plan_digest,
                    "provider_id": plan.provider_id,
                    "observations": mismatched,
                }
            ),
            detached_proof=b"test-detached-proof",
            policy=self.route_policy,
            verifier=self.verifier,
        )
        self.assertEqual((receipt.attestation_state, receipt.match_count, receipt.mismatch_count), ("MISMATCH", 49, 1))
        self.assertFalse(receipt.live_attested)

        duplicate = [dict(item) for item in observations]
        duplicate[-1]["correlation_id"] = duplicate[0]["correlation_id"]
        with self.assertRaises(IntegrityError):
            attest_external_routes(
                plan=plan,
                payload=_json_bytes(
                    {
                        "evidence_kind": M9_ROUTE_EVIDENCE_KIND,
                        "plan_digest": plan.plan_digest,
                        "provider_id": plan.provider_id,
                        "observations": duplicate,
                    }
                ),
                detached_proof=b"test-detached-proof",
                policy=self.route_policy,
                verifier=self.verifier,
            )
        wrong_provider = _json_bytes(
            {
                "evidence_kind": M9_ROUTE_EVIDENCE_KIND,
                "plan_digest": plan.plan_digest,
                "provider_id": "other-provider",
                "observations": observations,
            }
        )
        with self.assertRaises(SemanticValidationError):
            attest_external_routes(
                plan=plan,
                payload=wrong_provider,
                detached_proof=b"test-detached-proof",
                policy=self.route_policy,
                verifier=self.verifier,
            )

    def test_openssh_verifier_pins_anchor_and_keeps_proof_ephemeral(self) -> None:
        plan, observations = self._route_plan()
        payload = _json_bytes(
            {
                "evidence_kind": M9_ROUTE_EVIDENCE_KIND,
                "plan_digest": plan.plan_digest,
                "provider_id": plan.provider_id,
                "observations": observations,
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            anchor_path = Path(temporary) / "allowed-signers"
            executable_path = Path(temporary) / "ssh-keygen"
            anchor_bytes = b"trusted-public-anchor-only\n"
            anchor_path.write_bytes(anchor_bytes)
            executable_path.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
            executable_path.chmod(0o700)
            policy = ExternalTrustPolicy(
                policy_version=1,
                purpose=ROUTE_ATTESTATION_PURPOSE,
                authority_id="test:route-authority",
                verifier_protocol="openssh-detached-proof-v1",
                trust_anchor_fingerprint=sha256_bytes(anchor_bytes),
            )
            runner = _OpenSshRunner()
            verifier = OpenSshDetachedProofVerifier(
                allowed_signers_path=anchor_path,
                ssh_keygen_path=executable_path,
                process_runner=runner,
                clock=lambda: "2026-07-23T02:00:00Z",
            )
            receipt = attest_external_routes(
                plan=plan,
                payload=payload,
                detached_proof=b"opaque-detached-proof",
                policy=policy,
                verifier=verifier,
            )
            self.assertTrue(receipt.live_attested)
            self.assertEqual(receipt.verification.verifier_id, "openssh-detached-proof-v1")
            self.assertEqual(len(runner.calls), 1)
            arguments, sent_payload, scratch, timeout = runner.calls[0]
            self.assertEqual(arguments[1:3], ("-Y", "verify"))
            self.assertEqual(arguments[6], policy.authority_id)
            self.assertEqual(arguments[8], "pixel-second-brain-m9")
            self.assertEqual(sent_payload, payload)
            self.assertEqual(timeout, 10)
            self.assertFalse(Path(arguments[4]).exists())
            self.assertFalse(Path(arguments[10]).exists())
            self.assertFalse(scratch.exists())

            anchor_path.write_bytes(b"changed-public-anchor\n")
            with self.assertRaises(IntegrityError):
                attest_external_routes(
                    plan=plan,
                    payload=payload,
                    detached_proof=b"opaque-detached-proof",
                    policy=policy,
                    verifier=verifier,
                )

    @unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is unavailable")
    def test_openssh_verifier_accepts_a_real_ephemeral_signature(self) -> None:
        executable = Path(shutil.which("ssh-keygen") or "/nonexistent").resolve()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            key_path = directory / "test-key"
            payload_path = directory / "payload.json"
            payload = b'{"bounded":"public-test"}\n'
            subprocess.run(
                [str(executable), "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            public_parts = key_path.with_suffix(".pub").read_text(encoding="ascii").split()
            anchor_path = directory / "allowed-signers"
            anchor_path.write_text(f"test:route-authority {public_parts[0]} {public_parts[1]}\n", encoding="ascii")
            payload_path.write_bytes(payload)
            subprocess.run(
                [str(executable), "-Y", "sign", "-f", str(key_path), "-n", "pixel-second-brain-m9", str(payload_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            policy = ExternalTrustPolicy(
                policy_version=1,
                purpose=ROUTE_ATTESTATION_PURPOSE,
                authority_id="test:route-authority",
                verifier_protocol="openssh-detached-proof-v1",
                trust_anchor_fingerprint=sha256_bytes(anchor_path.read_bytes()),
            )
            verification = OpenSshDetachedProofVerifier(
                allowed_signers_path=anchor_path,
                ssh_keygen_path=executable,
                clock=lambda: "2026-07-23T02:00:00Z",
            ).verify(
                ExternalVerificationRequest(
                    policy=policy,
                    payload=payload,
                    detached_proof=payload_path.with_suffix(".json.sig").read_bytes(),
                )
            )
            self.assertEqual(verification.payload_digest, sha256_bytes(payload))
            self.assertEqual(verification.verifier_id, "openssh-detached-proof-v1")

    @unittest.skipUnless(Path("/usr/bin/ssh-keygen").is_file(), "system ssh-keygen is unavailable")
    def test_read_only_cli_emits_only_redacted_route_receipt(self) -> None:
        plan, observations = self._route_plan()
        payload = _json_bytes(
            {
                "evidence_kind": M9_ROUTE_EVIDENCE_KIND,
                "plan_digest": plan.plan_digest,
                "provider_id": plan.provider_id,
                "observations": observations,
            }
        )
        root = repository_root()
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            directory = Path(temporary)
            executable = Path("/usr/bin/ssh-keygen")
            key_path = directory / "test-key"
            payload_path = directory / "payload.json"
            subprocess.run(
                [str(executable), "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            public_parts = key_path.with_suffix(".pub").read_text(encoding="ascii").split()
            anchor_bytes = f"test:route-authority {public_parts[0]} {public_parts[1]}\n".encode("ascii")
            policy = ExternalTrustPolicy(
                policy_version=1,
                purpose=ROUTE_ATTESTATION_PURPOSE,
                authority_id="test:route-authority",
                verifier_protocol="openssh-detached-proof-v1",
                trust_anchor_fingerprint=sha256_bytes(anchor_bytes),
            )
            approval_packet = self._route_approval_packet(plan, policy)
            (directory / "approval-intent.json").write_text(
                json.dumps(self.route_intent.to_dict()), encoding="utf-8"
            )
            (directory / "approval-packet.json").write_text(
                json.dumps(approval_packet.to_dict()), encoding="utf-8"
            )
            (directory / "plan.json").write_text(json.dumps(plan.to_dict()), encoding="utf-8")
            (directory / "policy.json").write_text(json.dumps(policy.to_dict()), encoding="utf-8")
            payload_path.write_bytes(payload)
            subprocess.run(
                [str(executable), "-Y", "sign", "-f", str(key_path), "-n", "pixel-second-brain-m9", str(payload_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            (directory / "allowed-signers").write_bytes(anchor_bytes)
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(root / "src")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts/verify_m9_external_evidence.py"),
                    "route",
                    "--plan",
                    str((directory / "plan.json").relative_to(root)),
                    "--policy",
                    str((directory / "policy.json").relative_to(root)),
                    "--approval-intent",
                    str((directory / "approval-intent.json").relative_to(root)),
                    "--approval-packet",
                    str((directory / "approval-packet.json").relative_to(root)),
                    "--payload",
                    str(payload_path),
                    "--proof",
                    str(payload_path.with_suffix(".json.sig")),
                    "--allowed-signers",
                    str(directory / "allowed-signers"),
                    "--authorization-reference",
                    f"approved {approval_packet.packet_digest}",
                ],
                check=False,
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads(completed.stdout)
            self.assertEqual(output["status"], "VERIFIED_REDACTED_ROUTE_EVIDENCE")
            self.assertEqual(output["approval_packet_digest"], approval_packet.packet_digest)
            self.assertTrue(output["live_attested"])
            self.assertFalse(output["promotion_authorized"])
            self.assertNotIn(observations[0]["correlation_id"], completed.stdout)
            self.assertNotIn(observations[0]["model_identifier"], completed.stdout)

    def test_plan_preparation_cli_redacts_raw_input_and_refuses_foreign_replacement(self) -> None:
        root = repository_root()
        requests = [
            {
                "correlation_id": f"corr:future-{ordinal:04d}-opaque",
                "profile_alias": "tera-max",
                "model_identifier": "cx/gpt-5.6-terra",
                "effort": "max",
            }
            for ordinal in range(50)
        ]
        with tempfile.TemporaryDirectory() as input_temporary, tempfile.TemporaryDirectory(dir=root / "artifacts") as output_temporary:
            input_path = Path(input_temporary) / "raw-plan.json"
            intent_path = Path(output_temporary) / "intent.json"
            intent_path.write_text(json.dumps(self.route_intent.to_dict()), encoding="utf-8")
            raw_manifest = {
                "approval_intent_digest": self.route_intent.intent_digest,
                "profile_registry_digest": registry_digest(load_profile_registry()),
                "provider_id": self.route_intent.provider_id,
                "requests": requests,
            }
            input_path.write_text(json.dumps(raw_manifest), encoding="utf-8")
            output_path = Path(output_temporary) / "plan.json"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(root / "src")
            command = [
                sys.executable,
                str(root / "scripts/prepare_m9_route_attestation_plan.py"),
                "--approval-intent",
                str(intent_path.relative_to(root)),
                "--input",
                str(input_path),
                "--output",
                str(output_path.relative_to(root)),
            ]
            first = subprocess.run(command, check=False, cwd=root, env=environment, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            output = json.loads(first.stdout)
            self.assertEqual(output["status"], "REDACTED_PLAN_CREATED")
            serialized = output_path.read_text(encoding="utf-8")
            self.assertNotIn(requests[0]["correlation_id"], serialized)
            self.assertNotIn(requests[0]["model_identifier"], serialized)
            plan = RouteAttestationPlan.from_value(json.loads(serialized))
            self.assertEqual((len(plan.expectations), plan.plan_digest), (50, output["plan_digest"]))

            repeated = subprocess.run(command, check=False, cwd=root, env=environment, capture_output=True, text=True)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            raw_manifest["requests"][0]["effort"] = "xhigh"
            input_path.write_text(json.dumps(raw_manifest), encoding="utf-8")
            rejected = subprocess.run(command, check=False, cwd=root, env=environment, capture_output=True, text=True)
            self.assertEqual(rejected.returncode, 1)
            self.assertEqual(output_path.read_text(encoding="utf-8"), serialized)

    def test_plan_preparation_rejects_foreign_intent_and_registry_mapping_drift(self) -> None:
        root = repository_root()
        requests = [
            {
                "correlation_id": f"corr:bound-{ordinal:04d}-opaque",
                "profile_alias": "tera-max",
                "model_identifier": "cx/gpt-5.6-terra",
                "effort": "max",
            }
            for ordinal in range(50)
        ]
        with tempfile.TemporaryDirectory() as input_temporary, tempfile.TemporaryDirectory(dir=root / "artifacts") as output_temporary:
            input_path = Path(input_temporary) / "raw-plan.json"
            output_root = Path(output_temporary)
            intent_path = output_root / "intent.json"
            intent_path.write_text(json.dumps(self.route_intent.to_dict()), encoding="utf-8")
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(root / "src")

            def run(output_name: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        sys.executable,
                        str(root / "scripts/prepare_m9_route_attestation_plan.py"),
                        "--approval-intent",
                        str(intent_path.relative_to(root)),
                        "--input",
                        str(input_path),
                        "--output",
                        str((output_root / output_name).relative_to(root)),
                    ],
                    check=False,
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=True,
                )

            manifest = {
                "approval_intent_digest": "0" * 64,
                "profile_registry_digest": registry_digest(load_profile_registry()),
                "provider_id": self.route_intent.provider_id,
                "requests": requests,
            }
            input_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(run("foreign-intent.json").returncode, 1)
            self.assertFalse((output_root / "foreign-intent.json").exists())

            manifest["approval_intent_digest"] = self.route_intent.intent_digest
            manifest["profile_registry_digest"] = "0" * 64
            input_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(run("wrong-registry.json").returncode, 1)
            self.assertFalse((output_root / "wrong-registry.json").exists())

            manifest["profile_registry_digest"] = registry_digest(load_profile_registry())
            manifest["requests"][0]["model_identifier"] = "cx/gpt-5.6-sol"
            input_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(run("wrong-mapping.json").returncode, 1)
            self.assertFalse((output_root / "wrong-mapping.json").exists())

    def test_route_approval_intent_cli_binds_only_the_current_initial_boundary(self) -> None:
        root = repository_root()
        with tempfile.TemporaryDirectory(dir=root / "artifacts") as output_temporary:
            output_path = Path(output_temporary) / "intent.json"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(root / "src")
            command = [
                sys.executable,
                str(root / "scripts/prepare_m9_route_approval_intent.py"),
                "--output",
                str(output_path.relative_to(root)),
            ]
            first = subprocess.run(command, check=False, cwd=root, env=environment, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            output = json.loads(first.stdout)
            intent = RouteAttestationApprovalIntent.from_value(json.loads(output_path.read_text(encoding="utf-8")))
            self.assertEqual(intent, self.route_intent)
            self.assertEqual(output["intent_digest"], self.route_intent.intent_digest)
            self.assertFalse(intent.global_mutation)
            self.assertFalse(intent.promotion_authorized)
            self.assertNotIn("M9_PUBLIC_SHADOW_OK", output_path.read_text(encoding="utf-8"))
            repeated = subprocess.run(command, check=False, cwd=root, env=environment, capture_output=True, text=True)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)

    def test_route_approval_packet_cli_binds_intent_plan_and_policy_without_raw_values(self) -> None:
        root = repository_root()
        plan, observations = self._route_plan()
        with tempfile.TemporaryDirectory(dir=root / "artifacts") as output_temporary:
            directory = Path(output_temporary)
            intent_path = directory / "intent.json"
            plan_path = directory / "plan.json"
            policy_path = directory / "policy.json"
            output_path = directory / "approval-packet.json"
            intent_path.write_text(json.dumps(self.route_intent.to_dict()), encoding="utf-8")
            plan_path.write_text(json.dumps(plan.to_dict()), encoding="utf-8")
            policy_path.write_text(json.dumps(self.route_policy.to_dict()), encoding="utf-8")
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(root / "src")
            command = [
                sys.executable,
                str(root / "scripts/prepare_m9_route_approval_packet.py"),
                "--approval-intent",
                str(intent_path.relative_to(root)),
                "--plan",
                str(plan_path.relative_to(root)),
                "--policy",
                str(policy_path.relative_to(root)),
                "--output",
                str(output_path.relative_to(root)),
            ]
            completed = subprocess.run(command, check=False, cwd=root, env=environment, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads(completed.stdout)
            packet = RouteAttestationApprovalPacket.from_value(json.loads(output_path.read_text(encoding="utf-8")))
            self.assertEqual(packet.packet_digest, output["packet_digest"])
            self.assertEqual(packet.expected_request_count, 50)
            self.assertFalse(packet.global_mutation)
            self.assertFalse(packet.promotion_authorized)
            serialized = output_path.read_text(encoding="utf-8")
            self.assertNotIn(observations[0]["correlation_id"], serialized)
            self.assertNotIn(observations[0]["model_identifier"], serialized)
            self.assertIn(packet.approval_request, completed.stdout)

            foreign_plan = RouteAttestationPlan(
                plan_version=1,
                approval_intent_digest="0" * 64,
                profile_registry_digest=plan.profile_registry_digest,
                provider_id=plan.provider_id,
                expectations=plan.expectations,
            )
            with self.assertRaises(IntegrityError):
                build_route_attestation_approval_packet(
                    intent=self.route_intent,
                    plan=foreign_plan,
                    policy=self.route_policy,
                )

    def test_trust_policy_cli_pins_public_anchor_without_copying_it(self) -> None:
        root = repository_root()
        with tempfile.TemporaryDirectory() as input_temporary, tempfile.TemporaryDirectory(dir=root / "artifacts") as output_temporary:
            anchor_path = Path(input_temporary) / "allowed-signers"
            anchor_bytes = b"test:route-authority ssh-ed25519 AAAA-public-anchor\n"
            anchor_path.write_bytes(anchor_bytes)
            output_path = Path(output_temporary) / "policy.json"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(root / "src")
            command = [
                sys.executable,
                str(root / "scripts/prepare_m9_external_trust_policy.py"),
                "--purpose",
                "route-attestation",
                "--authority-id",
                "test:route-authority",
                "--allowed-signers",
                str(anchor_path),
                "--output",
                str(output_path.relative_to(root)),
            ]
            first = subprocess.run(command, check=False, cwd=root, env=environment, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            serialized = output_path.read_text(encoding="utf-8")
            self.assertNotIn(anchor_bytes.decode("ascii"), serialized)
            policy = ExternalTrustPolicy.from_value(json.loads(serialized))
            self.assertEqual(policy.trust_anchor_fingerprint, sha256_bytes(anchor_bytes))
            repeated = subprocess.run(command, check=False, cwd=root, env=environment, capture_output=True, text=True)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            anchor_path.write_bytes(b"changed-public-anchor\n")
            rejected = subprocess.run(command, check=False, cwd=root, env=environment, capture_output=True, text=True)
            self.assertEqual(rejected.returncode, 1)
            self.assertEqual(output_path.read_text(encoding="utf-8"), serialized)

    def test_blinded_review_plan_cli_retains_only_case_references(self) -> None:
        root = repository_root()
        packet = BlindedReviewPacket(
            packet_version=1,
            dataset_manifest_digest="3" * 64,
            seed=M8_SEED,
            label_assignment_digest="4" * 64,
            entries=tuple(
                (
                    sha256_hex({"case": ordinal}),
                    f"rubric:case-{ordinal}",
                    sha256_hex({"label": "a", "case": ordinal}),
                    sha256_hex({"label": "b", "case": ordinal}),
                )
                for ordinal in range(3)
            ),
        )
        with tempfile.TemporaryDirectory(dir=root) as packet_temporary, tempfile.TemporaryDirectory(dir=root / "artifacts") as output_temporary:
            packet_path = Path(packet_temporary) / "packet.json"
            packet_path.write_text(json.dumps(packet.to_dict()), encoding="utf-8")
            output_path = Path(output_temporary) / "review-plan.json"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(root / "src")
            command = [
                sys.executable,
                str(root / "scripts/prepare_m9_blinded_review_plan.py"),
                "--packet",
                str(packet_path.relative_to(root)),
                "--output",
                str(output_path.relative_to(root)),
            ]
            completed = subprocess.run(command, check=False, cwd=root, env=environment, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            plan = BlindedReviewIntakePlan.from_value(json.loads(output_path.read_text(encoding="utf-8")))
            self.assertEqual(plan.packet_digest, packet.packet_digest)
            self.assertNotIn(packet.entries[0][2], output_path.read_text(encoding="utf-8"))
            self.assertNotIn(packet.entries[0][3], output_path.read_text(encoding="utf-8"))
    def test_blinded_review_capture_is_bound_but_cannot_adjudicate_or_promote(self) -> None:
        packet = BlindedReviewPacket(
            packet_version=1,
            dataset_manifest_digest="e" * 64,
            seed=M8_SEED,
            label_assignment_digest="f" * 64,
            entries=tuple(
                (
                    sha256_hex({"case": ordinal}),
                    f"rubric:case-{ordinal}",
                    sha256_hex({"label": "a", "case": ordinal}),
                    sha256_hex({"label": "b", "case": ordinal}),
                )
                for ordinal in range(3)
            ),
        )
        self.assertEqual(blinded_review_packet_from_value(packet.to_dict()), packet)
        plan = build_blinded_review_intake_plan(packet)
        self.assertIsInstance(plan, BlindedReviewIntakePlan)
        self.assertEqual(BlindedReviewIntakePlan.from_value(plan.to_dict()), plan)
        entries = [
            {
                "case_reference_digest": reference,
                "preferred_label": "TIE",
                "label_a_score": 3,
                "label_b_score": 3,
                "high_risk_failure": False,
            }
            for reference in plan.case_reference_digests
        ]
        payload = _json_bytes(
            {
                "evidence_kind": M9_REVIEW_EVIDENCE_KIND,
                "plan_digest": plan.plan_digest,
                "review_packet_digest": packet.packet_digest,
                "entries": entries,
            }
        )
        receipt = capture_blinded_review_results(
            plan=plan,
            payload=payload,
            detached_proof=b"test-detached-proof",
            policy=self.review_policy,
            verifier=self.verifier,
        )
        self.assertEqual(receipt.capture_status, "CAPTURED")
        self.assertEqual(receipt.semantic_non_inferiority_status, "BLOCKED_AUTHORIZED_ADJUDICATION_REQUIRED")
        self.assertFalse(receipt.candidate_label_mapping_retained)
        self.assertFalse(receipt.promotion_authorized)
        self.assertFalse(promotion_allowed(receipt))
        serialized = json.dumps(receipt.to_dict(), sort_keys=True)
        self.assertNotIn(plan.case_reference_digests[0], serialized)
        self.assertNotIn("test-detached-proof", serialized)

        incomplete = list(entries[:-1])
        with self.assertRaises(SemanticValidationError):
            capture_blinded_review_results(
                plan=plan,
                payload=_json_bytes(
                    {
                        "evidence_kind": M9_REVIEW_EVIDENCE_KIND,
                        "plan_digest": plan.plan_digest,
                        "review_packet_digest": packet.packet_digest,
                        "entries": incomplete,
                    }
                ),
                detached_proof=b"test-detached-proof",
                policy=self.review_policy,
                verifier=self.verifier,
            )


if __name__ == "__main__":
    unittest.main()
