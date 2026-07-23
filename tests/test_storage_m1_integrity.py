from __future__ import annotations

import json
import os
from pathlib import Path
import unittest

from second_brain.storage import Mutation, StorageError, StoreHealth

from tests.m1_storage_helpers import (
    OBJECT_A,
    OBJECT_B,
    OBJECT_PROJECT,
    create_mutation,
    error_code,
    fixture_path,
    initialized_store,
    make_object,
    make_relation,
    open_store,
    transaction,
)


class StorageIntegrityTests(unittest.TestCase):
    def assert_error_code(self, expected: str, action: object) -> StorageError:
        with self.assertRaises(StorageError) as caught:
            action()  # type: ignore[operator]
        self.assertEqual(error_code(caught.exception), expected)
        return caught.exception

    def _commit_one_object(self, store: object) -> None:
        store.commit(  # type: ignore[union-attr]
            transaction(
                "txn:20000000-0000-4000-8000-000000000001",
                "m1-integrity-create",
                [create_mutation(make_object(OBJECT_A))],
            )
        )

    def _assert_tamper_degrades(self, root: Path, clock: object) -> None:
        degraded = open_store(root, clock)  # type: ignore[arg-type]
        self.assertEqual(degraded.health, StoreHealth.DEGRADED_READ_ONLY)
        report = degraded.lint()
        self.assertIn("INTEGRITY_FAILED", {finding.code for finding in report.findings})
        self.assert_error_code(
            "STORE_DEGRADED",
            lambda: degraded.commit(
                transaction(
                    "txn:20000000-0000-4000-8000-000000000002",
                    "m1-degraded-write",
                    [create_mutation(make_object(OBJECT_B))],
                )
            ),
        )

    def test_object_event_and_manifest_tampering_each_force_degraded_read_only(self) -> None:
        for target in ("object", "event", "manifest"):
            with self.subTest(target=target), initialized_store() as (root, clock, store):
                self._commit_one_object(store)
                if target == "object":
                    object_paths = list((root / "objects").rglob("*.md"))
                    self.assertEqual(len(object_paths), 1)
                    object_paths[0].write_text(
                        object_paths[0].read_text(encoding="utf-8") + "tampered body\n",
                        encoding="utf-8",
                    )
                elif target == "event":
                    event_path = root / "events" / "events.ndjson"
                    event_path.write_bytes(event_path.read_bytes() + b"not-json\n")
                else:
                    manifest_path = root / "manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest["mutation_epoch"] = manifest["mutation_epoch"] + 99
                    manifest_path.write_text(
                        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                        encoding="utf-8",
                    )

                self._assert_tamper_degrades(root, clock)

    def test_path_traversal_and_symlink_escape_are_rejected_before_any_write(self) -> None:
        with initialized_store() as (root, _, store):
            valid = make_object(OBJECT_A)
            traversal = Mutation(
                operation="create",
                object_id="../../outside-store",
                expected_revision=None,
                expected_content_hash=None,
                desired_object=valid,
            )
            self.assert_error_code(
                "PATH_UNSAFE",
                lambda: store.commit(
                    transaction(
                        "txn:20000000-0000-4000-8000-000000000003",
                        "m1-path-traversal",
                        [traversal],
                    )
                ),
            )
            self.assertFalse((root.parent / "outside-store").exists())

            outside = root.parent / "outside-via-symlink"
            outside.mkdir()
            object_directory = root / "objects" / "concepts"
            object_directory.mkdir(parents=True, exist_ok=True)
            object_directory.rmdir()
            try:
                os.symlink(outside, object_directory, target_is_directory=True)
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"symlink fixtures are unavailable: {error}")

            self.assert_error_code(
                "PATH_UNSAFE",
                lambda: store.commit(
                    transaction(
                        "txn:20000000-0000-4000-8000-000000000004",
                        "m1-symlink-escape",
                        [create_mutation(make_object(OBJECT_A))],
                    )
                ),
            )
            self.assertEqual(list(outside.iterdir()), [])

    def test_derived_deletion_never_removes_authoritative_objects_or_events(self) -> None:
        with initialized_store() as (root, _, store):
            self._commit_one_object(store)
            object_path = next((root / "objects").rglob("*.md"))
            event_path = root / "events" / "events.ndjson"
            original_object = object_path.read_bytes()
            original_events = event_path.read_bytes()
            derived = root / "derived"
            derived.mkdir(exist_ok=True)
            (derived / "throwaway.index").write_text("derived only", encoding="utf-8")

            store.invalidate_derived("deterministic deletion test")

            self.assertTrue(store.read(OBJECT_A))
            self.assertEqual(object_path.read_bytes(), original_object)
            self.assertEqual(event_path.read_bytes(), original_events)
            self.assertFalse(derived.exists() and any(derived.iterdir()))

    def test_dangling_unknown_cross_store_and_cycle_relations_are_rejected(self) -> None:
        with initialized_store() as (_, _, store):
            dangling = make_object(OBJECT_A, relations=[make_relation(OBJECT_B)])
            self.assert_error_code(
                "RELATION_INVALID",
                lambda: store.commit(
                    transaction(
                        "txn:20000000-0000-4000-8000-000000000005",
                        "m1-dangling-relation",
                        [create_mutation(dangling)],
                    )
                ),
            )

            cross_store = make_object(OBJECT_A, relations=[make_relation(OBJECT_PROJECT)])
            self.assert_error_code(
                "RELATION_INVALID",
                lambda: store.commit(
                    transaction(
                        "txn:20000000-0000-4000-8000-000000000006",
                        "m1-cross-store-relation",
                        [create_mutation(cross_store)],
                    )
                ),
            )

            store.commit(
                transaction(
                    "txn:20000000-0000-4000-8000-000000000007",
                    "m1-relation-target",
                    [create_mutation(make_object(OBJECT_B))],
                )
            )
            unknown_provenance = make_object(
                OBJECT_A,
                relations=[
                    make_relation(
                        OBJECT_B,
                        provenance_id="prov:00000000-0000-4000-8000-000000000099",
                    )
                ],
            )
            self.assert_error_code(
                "RELATION_INVALID",
                lambda: store.commit(
                    transaction(
                        "txn:20000000-0000-4000-8000-000000000008",
                        "m1-unknown-provenance",
                        [create_mutation(unknown_provenance)],
                    )
                ),
            )

        with initialized_store() as (_, _, store):
            cyclic_a = make_object(
                OBJECT_A,
                relations=[make_relation(OBJECT_B, relation_type="depends_on", suffix="2")],
            )
            cyclic_b = make_object(
                OBJECT_B,
                relations=[make_relation(OBJECT_A, relation_type="depends_on", suffix="3")],
            )
            self.assert_error_code(
                "RELATION_INVALID",
                lambda: store.commit(
                    transaction(
                        "txn:20000000-0000-4000-8000-000000000009",
                        "m1-cycle-relation",
                        [create_mutation(cyclic_a), create_mutation(cyclic_b)],
                    )
                ),
            )
            with self.assertRaises(StorageError):
                store.read(OBJECT_A)
            with self.assertRaises(StorageError):
                store.read(OBJECT_B)

    def test_authorizer_and_redacted_secret_or_instruction_policy_fail_closed(self) -> None:
        with initialized_store(authorizer=lambda *args, **kwargs: False) as (_, _, store):
            self.assert_error_code(
                "AUTHORITY_DENIED",
                lambda: store.commit(
                    transaction(
                        "txn:20000000-0000-4000-8000-000000000010",
                        "m1-authority-denied",
                        [create_mutation(make_object(OBJECT_A))],
                    )
                ),
            )

        secret_body = fixture_path("unsafe-secret-body.txt").read_text(encoding="utf-8")
        instruction_body = fixture_path("unsafe-instruction-body.txt").read_text(encoding="utf-8")
        with initialized_store() as (_, _, store):
            secret_error = self.assert_error_code(
                "CONTENT_POLICY_REJECTED",
                lambda: store.commit(
                    transaction(
                        "txn:20000000-0000-4000-8000-000000000011",
                        "m1-secret-rejected",
                        [create_mutation(make_object(OBJECT_A, body=secret_body))],
                    )
                ),
            )
            self.assertNotIn(secret_body.strip(), str(secret_error))

            instruction_error = self.assert_error_code(
                "CONTENT_POLICY_REJECTED",
                lambda: store.commit(
                    transaction(
                        "txn:20000000-0000-4000-8000-000000000012",
                        "m1-instruction-rejected",
                        [create_mutation(make_object(OBJECT_B, body=instruction_body))],
                    )
                ),
            )
            self.assertNotIn(instruction_body.strip(), str(instruction_error))


if __name__ == "__main__":
    unittest.main()
