from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import second_brain.activation_v2_rollout as rollout
from second_brain.activation_v2 import activation_v2_logical_digest
from second_brain.activation_v2_rollout import (
    SyntheticA7ApprovalPacket,
    SyntheticA7PendingOperation,
    SyntheticRolloutError,
    SyntheticRolloutReceipt,
    SyntheticRolloutState,
    SyntheticTargetBundle,
    create_synthetic_a7_backup,
    initialize_synthetic_a7_rollout,
    load_synthetic_a7_receipt,
    load_synthetic_a7_rollout_state,
    prepare_synthetic_a7_packet,
    readback_synthetic_a7_rollout,
    rollback_synthetic_a7_rollout,
    run_synthetic_a7_canary,
    synthetic_a7_implementation_digest,
)
from second_brain.activation_v2_runtime import (
    ActivationV2RuntimeError,
    PolicyInputs,
    initialize_disposable_runtime,
    runtime_health,
)
from second_brain.contracts import validate_named_document
from second_brain.errors import ContractError, IntegrityError
from second_brain.jsonio import load_strict_json
from second_brain.workspace import repository_root


class ActivationV2A7SyntheticRolloutTests(unittest.TestCase):
    as_of = "2026-07-24T09:00:00Z"

    def setUp(self) -> None:
        self.root = repository_root()
        test_runs = self.root / "artifacts" / "test-runs"
        test_runs.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="activation-v2-a7-", dir=test_runs)
        self.sandbox = Path(self.temporary.name)
        self.evaluation = self._load("fixtures/activation-v2/a7-rollout-evaluation-v1.json")
        self.bundle = self._load(self.evaluation["target_bundle_fixture"])
        self.policy = self._load(self.evaluation["policy_fixture"])
        self.runtime = self._runtime("runtime")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _load(self, relative: str) -> dict[str, object]:
        return load_strict_json(self.root / relative)

    def _runtime(self, name: str, policy: PolicyInputs | None = None):
        return initialize_disposable_runtime(
            self.sandbox / name,
            policy or PolicyInputs.from_value(self.policy),
            created_at=self.as_of,
        )

    def _packet(self, bundle: dict[str, object] | None = None):
        return prepare_synthetic_a7_packet(
            self.policy,
            bundle or self.bundle,
            packet_id=self.evaluation["packet_id"],
            as_of=self.as_of,
        )

    def _full_rehearsal(self):
        packet = self._packet()
        initialize_synthetic_a7_rollout(self.runtime, packet, self.bundle, as_of=self.as_of)
        backup = create_synthetic_a7_backup(
            self.runtime,
            packet,
            self.bundle,
            as_of=self.as_of,
            expected_state_revision=0,
            backup_id=self.evaluation["backup_id"],
            receipt_id=self.evaluation["receipt_ids"]["backup"],
        )
        canary = run_synthetic_a7_canary(
            self.runtime,
            packet,
            self.bundle,
            backup,
            as_of=self.as_of,
            expected_state_revision=0,
            receipt_id=self.evaluation["receipt_ids"]["canary"],
        )
        readback = readback_synthetic_a7_rollout(
            self.runtime,
            packet,
            self.bundle,
            backup,
            canary,
            as_of=self.as_of,
            expected_state_revision=1,
            receipt_id=self.evaluation["receipt_ids"]["readback"],
        )
        rollback = rollback_synthetic_a7_rollout(
            self.runtime,
            packet,
            self.bundle,
            backup,
            readback,
            as_of=self.as_of,
            expected_state_revision=1,
            receipt_id=self.evaluation["receipt_ids"]["rollback"],
        )
        return packet, backup, canary, readback, rollback

    def _rehersed_backup(self):
        packet = self._packet()
        initialize_synthetic_a7_rollout(self.runtime, packet, self.bundle, as_of=self.as_of)
        backup = create_synthetic_a7_backup(
            self.runtime,
            packet,
            self.bundle,
            as_of=self.as_of,
            expected_state_revision=0,
            backup_id=self.evaluation["backup_id"],
            receipt_id=self.evaluation["receipt_ids"]["backup"],
        )
        return packet, backup

    def test_canonical_contracts_are_synthetic_only_digest_bound_and_source_bound(self) -> None:
        documents = (
            ("activation-v2-a7-target-bundle-v1", self.evaluation["target_bundle_fixture"], SyntheticTargetBundle),
            ("activation-v2-a7-approval-packet-v1", self.evaluation["canonical_packet_fixture"], SyntheticA7ApprovalPacket),
            ("activation-v2-a7-rollout-state-v1", self.evaluation["canonical_final_state_fixture"], SyntheticRolloutState),
            ("activation-v2-a7-rollout-receipt-v1", self.evaluation["canonical_rollback_receipt_fixture"], SyntheticRolloutReceipt),
            ("activation-v2-a7-pending-operation-v1", self.evaluation["canonical_pending_operation_fixture"], SyntheticA7PendingOperation),
        )
        for name, fixture, model in documents:
            with self.subTest(name=name):
                document = self._load(fixture)
                validate_named_document(name, document)
                self.assertEqual(model.from_value(document).to_dict(), document)

        packet = self._load(self.evaluation["canonical_packet_fixture"])
        self.assertEqual(packet["approval_state"], "SYNTHETIC_DRAFT_NOT_APPROVED")
        self.assertEqual(packet["canary_sequence"], ["backup", "canary", "readback", "rollback"])
        self.assertEqual(packet["implementation_digest"], synthetic_a7_implementation_digest())
        self.assertEqual(packet["synthetic_project_id"], self.evaluation["synthetic_project_id"])
        self.assertFalse(packet["network_permitted"])
        self.assertFalse(packet["global_target_access"])
        self.assertFalse(packet["global_mutation_authorized"])

    def test_packet_rehearsal_matches_fixture_receipts_and_restores_synthetic_state(self) -> None:
        packet, backup, canary, readback, rollback = self._full_rehearsal()
        expected_packet = self._load(self.evaluation["canonical_packet_fixture"])
        expected_state = self._load(self.evaluation["canonical_final_state_fixture"])
        expected_rollback = self._load(self.evaluation["canonical_rollback_receipt_fixture"])
        self.assertEqual(packet.to_dict(), expected_packet)
        self.assertEqual(rollback.to_dict(), expected_rollback)
        self.assertEqual(
            load_synthetic_a7_rollout_state(self.runtime, packet, self.bundle, as_of=self.as_of).to_dict(),
            expected_state,
        )

        receipts = {"backup": backup, "canary": canary, "readback": readback, "rollback": rollback}
        for operation, receipt in receipts.items():
            with self.subTest(operation=operation):
                self.assertEqual(receipt.receipt_digest, self.evaluation["expected_receipt_digests"][operation])
                self.assertEqual(receipt.state_revision, self.evaluation["expected_state_revisions"][operation])
                self.assertEqual(
                    load_synthetic_a7_receipt(
                        self.runtime, packet, self.bundle, receipt.receipt_id, as_of=self.as_of
                    ),
                    receipt,
                )
                self.assertTrue(receipt.fixture_only)
                self.assertEqual(receipt.network_calls, 0)
                self.assertFalse(receipt.global_target_access)
                self.assertFalse(receipt.global_mutation)
                self.assertFalse(receipt.authority_write)

        health = runtime_health(self.runtime)
        self.assertFalse(health["authority_store_opened"])
        self.assertFalse(health["global_mutation"])
        self.assertFalse(any((self.runtime.root / "projects").iterdir()))
        self.assertFalse(any((self.runtime.root / "global").iterdir()))
        self.assertFalse(any((self.runtime.root / "outbox").iterdir()))
        serialized = json.dumps(packet.to_dict(), sort_keys=True)
        for forbidden in ("prompt", "transcript", "assistant_output", "credential", "cookie", "private_key"):
            self.assertNotIn(forbidden, serialized)

    def test_exact_roles_project_and_safe_content_fail_closed_before_schema_reflection(self) -> None:
        duplicate_role = deepcopy(self.bundle)
        duplicate_role["targets"][1]["target_role"] = "project_activation_manifest"
        duplicate_role["targets"][1]["snapshot_digest"] = activation_v2_logical_digest(
            duplicate_role["targets"][1], "snapshot_digest"
        )
        duplicate_role["bundle_digest"] = activation_v2_logical_digest(duplicate_role, "bundle_digest")
        with self.assertRaises(ContractError):
            prepare_synthetic_a7_packet(self.policy, duplicate_role, packet_id=self.evaluation["packet_id"], as_of=self.as_of)

        mixed_project = deepcopy(self.bundle)
        mixed_project["targets"][1]["synthetic_project_id"] = "other-project"
        mixed_project["targets"][1]["snapshot_digest"] = activation_v2_logical_digest(
            mixed_project["targets"][1], "snapshot_digest"
        )
        mixed_project["bundle_digest"] = activation_v2_logical_digest(mixed_project, "bundle_digest")
        with self.assertRaises(ContractError):
            prepare_synthetic_a7_packet(self.policy, mixed_project, packet_id=self.evaluation["packet_id"], as_of=self.as_of)

        unsafe = deepcopy(self.bundle)
        marker = "V2_RAW_TRANSCRIPT_SENTINEL"
        unsafe["captured_at"] = marker
        with self.assertRaises(ContractError) as raised:
            validate_named_document("activation-v2-a7-target-bundle-v1", unsafe)
        self.assertNotIn(marker, str(raised.exception))
        self.assertFalse((self.runtime.root / "registry" / "a7-rollout").exists())

    def test_every_transition_requires_the_exact_bundle_policy_and_unexpired_source(self) -> None:
        packet, backup = self._rehersed_backup()
        altered_packet = packet.to_dict()
        altered_packet["targets"][0]["candidate_after_digest"] = "a" * 64
        altered_packet["targets"][0]["snapshot_digest"] = activation_v2_logical_digest(
            altered_packet["targets"][0], "snapshot_digest"
        )
        altered_packet["packet_digest"] = activation_v2_logical_digest(altered_packet, "packet_digest")
        with self.assertRaises(SyntheticRolloutError):
            run_synthetic_a7_canary(
                self.runtime,
                altered_packet,
                self.bundle,
                backup,
                as_of=self.as_of,
                expected_state_revision=0,
            )

        altered_bundle = deepcopy(self.bundle)
        altered_bundle["targets"][0]["candidate_after_digest"] = "b" * 64
        altered_bundle["targets"][0]["snapshot_digest"] = activation_v2_logical_digest(
            altered_bundle["targets"][0], "snapshot_digest"
        )
        altered_bundle["bundle_digest"] = activation_v2_logical_digest(altered_bundle, "bundle_digest")
        with self.assertRaises(SyntheticRolloutError):
            run_synthetic_a7_canary(
                self.runtime,
                packet,
                altered_bundle,
                backup,
                as_of=self.as_of,
                expected_state_revision=0,
            )

        mismatched_policy = PolicyInputs(
            policy_version=1,
            fixture_only=True,
            retention_days=31,
            capture_default="ASSISTED",
            storage_protection="ENCRYPTED_DISK",
            graph_ui="GENERATED_SNAPSHOT",
            router_entry_point="CLI_SHADOW",
            promotion_review_mode="PER_ITEM",
        )
        other_runtime = self._runtime("policy-mismatch", mismatched_policy)
        with self.assertRaises(SyntheticRolloutError):
            initialize_synthetic_a7_rollout(other_runtime, packet, self.bundle, as_of=self.as_of)

        with self.assertRaises(SyntheticRolloutError):
            initialize_synthetic_a7_rollout(self._runtime("expired"), packet, self.bundle, as_of="2026-07-24T10:00:01Z")

        source_drift_bundle = deepcopy(self.bundle)
        source_drift_bundle["implementation_digest"] = "a" * 64
        source_drift_bundle["bundle_digest"] = activation_v2_logical_digest(source_drift_bundle, "bundle_digest")
        with self.assertRaisesRegex(SyntheticRolloutError, "SOURCE_DRIFT"):
            prepare_synthetic_a7_packet(
                self.policy, source_drift_bundle, packet_id=self.evaluation["packet_id"], as_of=self.as_of
            )

    def test_canary_readback_and_rollback_require_persisted_exact_predecessors_and_fresh_cas(self) -> None:
        packet, backup = self._rehersed_backup()
        other_runtime = self._runtime("other-runtime")
        initialize_synthetic_a7_rollout(other_runtime, packet, self.bundle, as_of=self.as_of)
        with self.assertRaises(SyntheticRolloutError):
            run_synthetic_a7_canary(other_runtime, packet, self.bundle, backup, as_of=self.as_of, expected_state_revision=0)
        self.assertEqual(
            load_synthetic_a7_rollout_state(other_runtime, packet, self.bundle, as_of=self.as_of).state_revision, 0
        )

        with self.assertRaises(SyntheticRolloutError):
            run_synthetic_a7_canary(self.runtime, packet, self.bundle, backup, as_of=self.as_of, expected_state_revision=1)
        canary = run_synthetic_a7_canary(
            self.runtime,
            packet,
            self.bundle,
            backup,
            as_of=self.as_of,
            expected_state_revision=0,
            receipt_id=self.evaluation["receipt_ids"]["canary"],
        )
        receipt_directory = self.runtime.root / "receipts" / "a7-rollout"
        receipt_count = len(list(receipt_directory.glob("*.json")))
        with self.assertRaises(SyntheticRolloutError):
            readback_synthetic_a7_rollout(
                self.runtime, packet, self.bundle, backup, backup, as_of=self.as_of, expected_state_revision=1
            )
        self.assertEqual(len(list(receipt_directory.glob("*.json"))), receipt_count)
        readback = readback_synthetic_a7_rollout(
            self.runtime,
            packet,
            self.bundle,
            backup,
            canary,
            as_of=self.as_of,
            expected_state_revision=1,
            receipt_id=self.evaluation["receipt_ids"]["readback"],
        )
        with self.assertRaises(SyntheticRolloutError):
            rollback_synthetic_a7_rollout(
                self.runtime, packet, self.bundle, backup, canary, as_of=self.as_of, expected_state_revision=1
            )
        rollback_synthetic_a7_rollout(
            self.runtime,
            packet,
            self.bundle,
            backup,
            readback,
            as_of=self.as_of,
            expected_state_revision=1,
            receipt_id=self.evaluation["receipt_ids"]["rollback"],
        )
        self.assertEqual(
            load_synthetic_a7_rollout_state(self.runtime, packet, self.bundle, as_of=self.as_of).state_revision, 2
        )

    def test_concurrent_canaries_serialize_cas_and_cannot_overwrite_receipts(self) -> None:
        packet, backup = self._rehersed_backup()
        barrier = threading.Barrier(2)

        def attempt(receipt_id: str):
            barrier.wait()
            try:
                return run_synthetic_a7_canary(
                    self.runtime,
                    packet,
                    self.bundle,
                    backup,
                    as_of=self.as_of,
                    expected_state_revision=0,
                    receipt_id=receipt_id,
                )
            except SyntheticRolloutError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    attempt,
                    (
                        "a7-rollout-receipt:10000000-0000-4000-8000-000000000726",
                        "a7-rollout-receipt:10000000-0000-4000-8000-000000000727",
                    ),
                )
            )
        winner = [receipt for receipt in results if receipt is not None]
        self.assertEqual(len(winner), 1)
        self.assertEqual(
            load_synthetic_a7_rollout_state(self.runtime, packet, self.bundle, as_of=self.as_of).state_revision, 1
        )
        self.assertEqual(len(list((self.runtime.root / "receipts" / "a7-rollout").glob("*.json"))), 2)

    def test_pending_operation_recovers_after_keyboard_interrupt_and_completes_after_receipt_write(self) -> None:
        packet, backup = self._rehersed_backup()
        with patch.object(rollout, "_write_receipt", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_synthetic_a7_canary(
                    self.runtime,
                    packet,
                    self.bundle,
                    backup,
                    as_of=self.as_of,
                    expected_state_revision=0,
                    receipt_id=self.evaluation["receipt_ids"]["canary"],
                )
        journal = self.runtime.root / "registry" / "a7-rollout" / packet.packet_id.removeprefix("a7-packet:") / "pending-operation.json"
        self.assertTrue(journal.exists())
        self.assertEqual(
            load_synthetic_a7_rollout_state(self.runtime, packet, self.bundle, as_of=self.as_of).state_revision, 0
        )
        self.assertFalse(journal.exists())

        with patch.object(rollout, "remove_disposable_json", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_synthetic_a7_canary(
                    self.runtime,
                    packet,
                    self.bundle,
                    backup,
                    as_of=self.as_of,
                    expected_state_revision=0,
                    receipt_id=self.evaluation["receipt_ids"]["canary"],
                )
        self.assertTrue(journal.exists())
        self.assertEqual(
            load_synthetic_a7_rollout_state(self.runtime, packet, self.bundle, as_of=self.as_of).state_revision, 1
        )
        self.assertFalse(journal.exists())
        self.assertEqual(
            load_synthetic_a7_receipt(
                self.runtime, packet, self.bundle, self.evaluation["receipt_ids"]["canary"], as_of=self.as_of
            ).operation,
            "canary",
        )

    def test_rehashed_global_or_network_boundary_flags_fail_on_receipt_readback(self) -> None:
        packet, backup, _, _, _ = self._full_rehearsal()
        receipt_path = self.runtime.root / "receipts" / "a7-rollout" / (
            backup.receipt_id.removeprefix("a7-rollout-receipt:") + ".json"
        )
        payload = load_strict_json(receipt_path)
        payload["network_calls"] = 1
        payload["receipt_digest"] = activation_v2_logical_digest(payload, "receipt_digest")
        receipt_path.write_text(json.dumps(payload), encoding="utf-8")
        receipt_path.chmod(0o600)
        with self.assertRaises(IntegrityError):
            load_synthetic_a7_receipt(self.runtime, packet, self.bundle, backup.receipt_id, as_of=self.as_of)

    def test_unresolved_or_nonfixture_policy_cannot_prepare_a7_packet(self) -> None:
        with self.assertRaises(ActivationV2RuntimeError):
            prepare_synthetic_a7_packet(
                PolicyInputs.unresolved(), self.bundle, packet_id=self.evaluation["packet_id"], as_of=self.as_of
            )
        nonfixture = PolicyInputs(
            policy_version=1,
            fixture_only=False,
            retention_days=30,
            capture_default="ASSISTED",
            storage_protection="ENCRYPTED_DISK",
            graph_ui="GENERATED_SNAPSHOT",
            router_entry_point="CLI_SHADOW",
            promotion_review_mode="PER_ITEM",
        )
        with self.assertRaises(SyntheticRolloutError):
            prepare_synthetic_a7_packet(nonfixture, self.bundle, packet_id=self.evaluation["packet_id"], as_of=self.as_of)


if __name__ == "__main__":
    unittest.main()
