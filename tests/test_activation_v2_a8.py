from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

import second_brain.activation_v2_promotion as promotion
import second_brain.activation_v2_runtime as runtime_module
from second_brain.activation_v2 import activation_v2_logical_digest
from second_brain.activation_v2_promotion import (
    SyntheticA8PendingOperation,
    SyntheticA8PromotionCorpus,
    SyntheticA8PromotionPacket,
    SyntheticA8PromotionReceipt,
    SyntheticA8PromotionState,
    SyntheticPromotionError,
    create_synthetic_a8_backup,
    initialize_synthetic_a8_promotion,
    load_synthetic_a8_promotion_receipt,
    load_synthetic_a8_promotion_state,
    prepare_synthetic_a8_packet,
    readback_synthetic_a8_promotion,
    restore_synthetic_a8_promotion,
    run_synthetic_a8_promotion_canary,
    synthetic_a8_implementation_digest,
)
from second_brain.activation_v2_runtime import ActivationV2RuntimeError, PolicyInputs, initialize_disposable_runtime, runtime_health
from second_brain.contracts import validate_named_document
from second_brain.errors import ContractError, IntegrityError
from second_brain.jsonio import load_strict_json
from second_brain.workspace import repository_root


class ActivationV2A8SyntheticPromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = repository_root()
        test_runs = self.root / "artifacts" / "test-runs"
        test_runs.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="activation-v2-a8-", dir=test_runs)
        self.sandbox = Path(self.temporary.name)
        self.evaluation = self._load("fixtures/activation-v2/a8-promotion-evaluation-v1.json")
        self.policy = self._load(self.evaluation["policy_fixture"])
        self.a7_packet = self._load(self.evaluation["a7_packet_fixture"])
        self.corpus = self._load(self.evaluation["corpus_fixture"])
        self.runtime = self._runtime("runtime", runtime_id=self.evaluation["runtime_id"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _load(self, relative: str) -> dict[str, object]:
        return load_strict_json(self.root / relative)

    def _runtime(self, name: str, *, runtime_id: str | None = None, policy: PolicyInputs | None = None):
        selected = policy or PolicyInputs.from_value(self.policy)
        if runtime_id is None:
            return initialize_disposable_runtime(
                self.sandbox / name,
                selected,
                created_at=self.evaluation["as_of"],
            )
        with patch.object(runtime_module.uuid, "uuid4", return_value=uuid.UUID(runtime_id.removeprefix("runtime:"))):
            return initialize_disposable_runtime(
                self.sandbox / name,
                selected,
                created_at=self.evaluation["as_of"],
            )

    def _packet(self, corpus: dict[str, object] | None = None, a7_packet: dict[str, object] | None = None):
        return prepare_synthetic_a8_packet(
            self.policy,
            a7_packet or self.a7_packet,
            corpus or self.corpus,
            packet_id=self.evaluation["packet_id"],
            as_of=self.evaluation["as_of"],
        )

    def _full_rehearsal(self):
        packet = self._packet()
        initialize_synthetic_a8_promotion(self.runtime, packet, self.corpus, self.a7_packet, as_of=self.evaluation["as_of"])
        backup = create_synthetic_a8_backup(
            self.runtime,
            packet,
            self.corpus,
            self.a7_packet,
            as_of=self.evaluation["as_of"],
            expected_state_revision=0,
            backup_id=self.evaluation["backup_id"],
            receipt_id=self.evaluation["receipt_ids"]["backup"],
        )
        canary = run_synthetic_a8_promotion_canary(
            self.runtime,
            packet,
            self.corpus,
            self.a7_packet,
            backup,
            as_of=self.evaluation["as_of"],
            expected_state_revision=0,
            receipt_id=self.evaluation["receipt_ids"]["promotion_canary"],
        )
        readback = readback_synthetic_a8_promotion(
            self.runtime,
            packet,
            self.corpus,
            self.a7_packet,
            backup,
            canary,
            as_of=self.evaluation["as_of"],
            expected_state_revision=1,
            receipt_id=self.evaluation["receipt_ids"]["readback"],
        )
        restore = restore_synthetic_a8_promotion(
            self.runtime,
            packet,
            self.corpus,
            self.a7_packet,
            backup,
            readback,
            as_of=self.evaluation["as_of"],
            expected_state_revision=1,
            receipt_id=self.evaluation["receipt_ids"]["restore"],
        )
        return packet, backup, canary, readback, restore

    def _prepared_backup(self):
        packet = self._packet()
        initialize_synthetic_a8_promotion(self.runtime, packet, self.corpus, self.a7_packet, as_of=self.evaluation["as_of"])
        backup = create_synthetic_a8_backup(
            self.runtime,
            packet,
            self.corpus,
            self.a7_packet,
            as_of=self.evaluation["as_of"],
            expected_state_revision=0,
            backup_id=self.evaluation["backup_id"],
            receipt_id=self.evaluation["receipt_ids"]["backup"],
        )
        return packet, backup

    def test_canonical_contracts_are_reviewed_synthetic_and_source_bound(self) -> None:
        documents = (
            ("activation-v2-a8-promotion-corpus-v1", self.evaluation["corpus_fixture"], SyntheticA8PromotionCorpus),
            ("activation-v2-a8-promotion-packet-v1", self.evaluation["canonical_packet_fixture"], SyntheticA8PromotionPacket),
            ("activation-v2-a8-promotion-state-v1", self.evaluation["canonical_final_state_fixture"], SyntheticA8PromotionState),
            ("activation-v2-a8-promotion-receipt-v1", self.evaluation["canonical_restore_receipt_fixture"], SyntheticA8PromotionReceipt),
            ("activation-v2-a8-pending-operation-v1", self.evaluation["canonical_pending_operation_fixture"], SyntheticA8PendingOperation),
        )
        for name, fixture, model in documents:
            with self.subTest(name=name):
                document = self._load(fixture)
                validate_named_document(name, document)
                self.assertEqual(model.from_value(document).to_dict(), document)

        packet = self._load(self.evaluation["canonical_packet_fixture"])
        self.assertEqual(packet["implementation_digest"], synthetic_a8_implementation_digest())
        self.assertEqual(packet["review_mode"], "PER_ITEM")
        self.assertEqual(packet["canary_sequence"], ["backup", "promotion_canary", "readback", "restore"])
        self.assertFalse(packet["network_permitted"])
        self.assertFalse(packet["global_target_access"])
        self.assertFalse(packet["global_mutation_authorized"])

    def test_reviewed_promotion_rehearsal_matches_canonical_restore_and_leaves_no_authority_state(self) -> None:
        packet, backup, canary, readback, restore = self._full_rehearsal()
        expected_packet = self._load(self.evaluation["canonical_packet_fixture"])
        expected_state = self._load(self.evaluation["canonical_final_state_fixture"])
        expected_restore = self._load(self.evaluation["canonical_restore_receipt_fixture"])
        self.assertEqual(packet.to_dict(), expected_packet)
        self.assertEqual(restore.to_dict(), expected_restore)
        self.assertEqual(
            load_synthetic_a8_promotion_state(
                self.runtime, packet, self.corpus, self.a7_packet, as_of=self.evaluation["as_of"]
            ).to_dict(),
            expected_state,
        )
        receipts = {
            "backup": backup,
            "promotion_canary": canary,
            "readback": readback,
            "restore": restore,
        }
        for operation, receipt in receipts.items():
            with self.subTest(operation=operation):
                self.assertEqual(receipt.receipt_digest, self.evaluation["expected_receipt_digests"][operation])
                self.assertEqual(receipt.state_revision, self.evaluation["expected_state_revisions"][operation])
                self.assertEqual(
                    load_synthetic_a8_promotion_receipt(
                        self.runtime,
                        packet,
                        self.corpus,
                        self.a7_packet,
                        receipt.receipt_id,
                        as_of=self.evaluation["as_of"],
                    ),
                    receipt,
                )
                self.assertTrue(receipt.fixture_only)
                self.assertEqual(receipt.network_calls, 0)
                self.assertFalse(receipt.global_target_access)
                self.assertFalse(receipt.global_mutation)
                self.assertFalse(receipt.authority_write)
                self.assertFalse(receipt.persistent_user_memory_written)

        health = runtime_health(self.runtime)
        self.assertFalse(health["authority_store_opened"])
        self.assertFalse(health["global_mutation"])
        self.assertFalse(any((self.runtime.root / "projects").iterdir()))
        self.assertFalse(any((self.runtime.root / "global").iterdir()))
        self.assertFalse(any((self.runtime.root / "outbox").iterdir()))
        serialized = json.dumps(packet.to_dict(), sort_keys=True)
        for forbidden in ("prompt", "transcript", "assistant_output", "credential", "cookie", "private_key"):
            self.assertNotIn(forbidden, serialized)

    def test_unreviewed_cross_project_or_raw_corpus_is_rejected_before_packet_or_state(self) -> None:
        rejected = deepcopy(self.corpus)
        rejected["candidates"][0]["review_receipt"]["decision"] = "rejected"
        rejected["candidates"][0]["review_receipt"]["receipt_digest"] = activation_v2_logical_digest(
            rejected["candidates"][0]["review_receipt"], "receipt_digest"
        )
        rejected["candidates"][0]["candidate_digest"] = activation_v2_logical_digest(
            rejected["candidates"][0], "candidate_digest"
        )
        rejected["corpus_digest"] = activation_v2_logical_digest(rejected, "corpus_digest")
        with self.assertRaises(SyntheticPromotionError):
            prepare_synthetic_a8_packet(self.policy, self.a7_packet, rejected, packet_id=self.evaluation["packet_id"], as_of=self.evaluation["as_of"])

        cross_project = deepcopy(self.corpus)
        cross_project["candidates"][0]["source_object_ids"] = [
            "mem:foreign-project:evidence:10000000-0000-4000-8000-000000000802"
        ]
        cross_project["candidates"][0]["candidate_digest"] = activation_v2_logical_digest(
            cross_project["candidates"][0], "candidate_digest"
        )
        cross_project["corpus_digest"] = activation_v2_logical_digest(cross_project, "corpus_digest")
        with self.assertRaises(SyntheticPromotionError):
            SyntheticA8PromotionCorpus.from_value(cross_project)

        raw = deepcopy(self.corpus)
        marker = "V2_RAW_TRANSCRIPT_SENTINEL"
        raw["prompt"] = marker
        with self.assertRaises(ContractError) as raised:
            validate_named_document("activation-v2-a8-promotion-corpus-v1", raw)
        self.assertNotIn(marker, str(raised.exception))
        self.assertFalse((self.runtime.root / "registry" / "a8-promotion").exists())

    def test_policy_a7_provenance_expiry_and_source_drift_fail_closed(self) -> None:
        stale_corpus = deepcopy(self.corpus)
        stale_corpus["expires_at"] = "2026-07-24T09:00:00Z"
        stale_corpus["corpus_digest"] = activation_v2_logical_digest(stale_corpus, "corpus_digest")
        with self.assertRaises(SyntheticPromotionError):
            self._packet(stale_corpus)

        future_corpus = deepcopy(self.corpus)
        future_corpus["captured_at"] = "2026-07-24T09:01:00Z"
        future_corpus["corpus_digest"] = activation_v2_logical_digest(future_corpus, "corpus_digest")
        with self.assertRaises(SyntheticPromotionError):
            self._packet(future_corpus)

        drifted_corpus = deepcopy(self.corpus)
        drifted_corpus["implementation_digest"] = "a" * 64
        drifted_corpus["corpus_digest"] = activation_v2_logical_digest(drifted_corpus, "corpus_digest")
        with self.assertRaisesRegex(SyntheticPromotionError, "SOURCE_DRIFT"):
            self._packet(drifted_corpus)

        altered_a7 = deepcopy(self.a7_packet)
        altered_a7["implementation_digest"] = "a" * 64
        altered_a7["packet_digest"] = activation_v2_logical_digest(altered_a7, "packet_digest")
        altered_corpus = deepcopy(self.corpus)
        altered_corpus["a7_packet_digest"] = altered_a7["packet_digest"]
        altered_corpus["corpus_digest"] = activation_v2_logical_digest(altered_corpus, "corpus_digest")
        with self.assertRaisesRegex(SyntheticPromotionError, "SOURCE_DRIFT"):
            self._packet(altered_corpus, altered_a7)

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
        with self.assertRaises(SyntheticPromotionError):
            prepare_synthetic_a8_packet(
                nonfixture, self.a7_packet, self.corpus, packet_id=self.evaluation["packet_id"], as_of=self.evaluation["as_of"]
            )
        with self.assertRaises(ActivationV2RuntimeError):
            prepare_synthetic_a8_packet(
                PolicyInputs.unresolved(), self.a7_packet, self.corpus, packet_id=self.evaluation["packet_id"], as_of=self.evaluation["as_of"]
            )

    def test_sequence_requires_exact_persisted_receipts_fresh_cas_and_same_runtime(self) -> None:
        packet, backup = self._prepared_backup()
        other_runtime = self._runtime("other-runtime")
        initialize_synthetic_a8_promotion(other_runtime, packet, self.corpus, self.a7_packet, as_of=self.evaluation["as_of"])
        with self.assertRaises(SyntheticPromotionError):
            run_synthetic_a8_promotion_canary(
                other_runtime,
                packet,
                self.corpus,
                self.a7_packet,
                backup,
                as_of=self.evaluation["as_of"],
                expected_state_revision=0,
            )

        with self.assertRaises(SyntheticPromotionError):
            run_synthetic_a8_promotion_canary(
                self.runtime,
                packet,
                self.corpus,
                self.a7_packet,
                backup,
                as_of=self.evaluation["as_of"],
                expected_state_revision=1,
            )
        canary = run_synthetic_a8_promotion_canary(
            self.runtime,
            packet,
            self.corpus,
            self.a7_packet,
            backup,
            as_of=self.evaluation["as_of"],
            expected_state_revision=0,
            receipt_id=self.evaluation["receipt_ids"]["promotion_canary"],
        )
        with self.assertRaises(SyntheticPromotionError):
            readback_synthetic_a8_promotion(
                self.runtime,
                packet,
                self.corpus,
                self.a7_packet,
                backup,
                backup,
                as_of=self.evaluation["as_of"],
                expected_state_revision=1,
            )
        readback = readback_synthetic_a8_promotion(
            self.runtime,
            packet,
            self.corpus,
            self.a7_packet,
            backup,
            canary,
            as_of=self.evaluation["as_of"],
            expected_state_revision=1,
            receipt_id=self.evaluation["receipt_ids"]["readback"],
        )
        with self.assertRaises(SyntheticPromotionError):
            restore_synthetic_a8_promotion(
                self.runtime,
                packet,
                self.corpus,
                self.a7_packet,
                backup,
                canary,
                as_of=self.evaluation["as_of"],
                expected_state_revision=1,
            )
        restore_synthetic_a8_promotion(
            self.runtime,
            packet,
            self.corpus,
            self.a7_packet,
            backup,
            readback,
            as_of=self.evaluation["as_of"],
            expected_state_revision=1,
            receipt_id=self.evaluation["receipt_ids"]["restore"],
        )
        self.assertEqual(
            load_synthetic_a8_promotion_state(
                self.runtime, packet, self.corpus, self.a7_packet, as_of=self.evaluation["as_of"]
            ).state_revision,
            2,
        )

    def test_concurrent_canaries_serialize_cas_and_cannot_overwrite_receipts(self) -> None:
        packet, backup = self._prepared_backup()
        barrier = threading.Barrier(2)

        def attempt(receipt_id: str):
            barrier.wait()
            try:
                return run_synthetic_a8_promotion_canary(
                    self.runtime,
                    packet,
                    self.corpus,
                    self.a7_packet,
                    backup,
                    as_of=self.evaluation["as_of"],
                    expected_state_revision=0,
                    receipt_id=receipt_id,
                )
            except SyntheticPromotionError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    attempt,
                    (
                        "a8-promotion-receipt:10000000-0000-4000-8000-000000000836",
                        "a8-promotion-receipt:10000000-0000-4000-8000-000000000837",
                    ),
                )
            )
        self.assertEqual(len([receipt for receipt in results if receipt is not None]), 1)
        self.assertEqual(
            load_synthetic_a8_promotion_state(
                self.runtime, packet, self.corpus, self.a7_packet, as_of=self.evaluation["as_of"]
            ).state_revision,
            1,
        )
        self.assertEqual(len(list((self.runtime.root / "receipts" / "a8-promotion").glob("*.json"))), 2)

    def test_pending_operation_recovers_state_before_receipt_and_completes_after_receipt(self) -> None:
        packet, backup = self._prepared_backup()
        with patch.object(promotion, "_write_receipt", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_synthetic_a8_promotion_canary(
                    self.runtime,
                    packet,
                    self.corpus,
                    self.a7_packet,
                    backup,
                    as_of=self.evaluation["as_of"],
                    expected_state_revision=0,
                    receipt_id=self.evaluation["receipt_ids"]["promotion_canary"],
                )
        journal = self.runtime.root / "registry" / "a8-promotion" / packet.packet_id.removeprefix("a8-packet:") / "pending-operation.json"
        self.assertTrue(journal.exists())
        self.assertEqual(
            load_synthetic_a8_promotion_state(
                self.runtime, packet, self.corpus, self.a7_packet, as_of=self.evaluation["as_of"]
            ).state_revision,
            0,
        )
        self.assertFalse(journal.exists())

        with patch.object(promotion, "remove_disposable_json", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_synthetic_a8_promotion_canary(
                    self.runtime,
                    packet,
                    self.corpus,
                    self.a7_packet,
                    backup,
                    as_of=self.evaluation["as_of"],
                    expected_state_revision=0,
                    receipt_id=self.evaluation["receipt_ids"]["promotion_canary"],
                )
        self.assertTrue(journal.exists())
        self.assertEqual(
            load_synthetic_a8_promotion_state(
                self.runtime, packet, self.corpus, self.a7_packet, as_of=self.evaluation["as_of"]
            ).state_revision,
            1,
        )
        self.assertFalse(journal.exists())

    def test_rehashed_boundary_flags_fail_on_receipt_readback(self) -> None:
        packet, backup, _, _, _ = self._full_rehearsal()
        receipt_path = self.runtime.root / "receipts" / "a8-promotion" / (backup.receipt_id.removeprefix("a8-promotion-receipt:") + ".json")
        payload = load_strict_json(receipt_path)
        payload["network_calls"] = 1
        payload["receipt_digest"] = activation_v2_logical_digest(payload, "receipt_digest")
        receipt_path.write_text(json.dumps(payload), encoding="utf-8")
        receipt_path.chmod(0o600)
        with self.assertRaises(IntegrityError):
            load_synthetic_a8_promotion_receipt(
                self.runtime,
                packet,
                self.corpus,
                self.a7_packet,
                backup.receipt_id,
                as_of=self.evaluation["as_of"],
            )


if __name__ == "__main__":
    unittest.main()
