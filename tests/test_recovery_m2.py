"""Acceptance regressions for the local-only M2 Project Recovery Kernel."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import second_brain.recovery as recovery_module
from second_brain.recovery import ProjectRecoveryKernel, QueryRequest
from second_brain.storage import StorageError, canonical_jcs_bytes
from second_brain.workspace import repository_root

from tests.m2_recovery_helpers import (
    ARCHIVED_ID,
    BUBBLEWRAP_ID,
    CANONICAL_OBJECT_IDS,
    CANONICAL_SPECS,
    DANGLING_ID,
    LEGACY_FIXTURE,
    NEW_STATE_ID,
    SESSION_ID,
    authority_digest,
    create_mutation,
    initialized_project_store,
    logical_byte_only_edit,
    make_project_object,
    make_reference,
    make_relation,
    object_id,
    object_file,
    query,
    rehash,
    replace_mutation,
    transaction,
    tree_digest,
    value_dict,
)


class ProjectRecoveryM2Tests(unittest.TestCase):
    """Lock the M2 acceptance matrix to synthetic, repository-contained data."""

    def kernel(self, store: object, clock: object) -> ProjectRecoveryKernel:
        return ProjectRecoveryKernel(store, repository_root(), clock)  # type: ignore[arg-type]

    def assert_error_code(self, expected: str, action: object) -> StorageError:
        with self.assertRaises(StorageError) as caught:
            action()  # type: ignore[operator]
        self.assertEqual(getattr(caught.exception, "code", None), expected)
        return caught.exception

    def retrieve(
        self, kernel: ProjectRecoveryKernel, request: dict[str, object]
    ) -> dict[str, object]:
        return value_dict(kernel.retrieve(QueryRequest.from_value(request)))

    def recovery_pack(
        self, kernel: ProjectRecoveryKernel, request: dict[str, object]
    ) -> object:
        return kernel.build_recovery_pack(QueryRequest.from_value(request))

    def view_text(self, value: object, name: str, store_root: Path) -> str:
        """Read one generated public view whether it is returned as text or a path."""

        views = value_dict(value)
        candidate = views.get(f"{name}_markdown", views.get(name))
        if isinstance(candidate, str):
            if "\n" in candidate:
                return candidate
            path = Path(candidate)
            if not path.is_absolute():
                path = store_root / path
            return path.read_text(encoding="utf-8") if path.exists() else candidate
        if isinstance(candidate, dict):
            for key in ("markdown", "text", "content"):
                text = candidate.get(key)
                if isinstance(text, str):
                    return text
            path_value = candidate.get("path")
            if isinstance(path_value, str):
                path = Path(path_value)
                if not path.is_absolute():
                    path = store_root / path
                return path.read_text(encoding="utf-8")
        path_value = views.get(f"{name}_path")
        if isinstance(path_value, str):
            path = Path(path_value)
            if not path.is_absolute():
                path = store_root / path
            return path.read_text(encoding="utf-8")
        raise AssertionError(f"generated {name} view has no public text or path")

    def test_canonical_queries_recall_all_ten_expected_objects(self) -> None:
        """The fixed canonical recovery suite must retain 10/10 lexical recall."""

        with initialized_project_store() as (_, clock, store, _):
            kernel = self.kernel(store, clock)
            recalled: set[str] = set()
            for sequence, (_, _, _, token) in enumerate(CANONICAL_SPECS, start=1):
                envelope = self.retrieve(kernel, query(token, sequence))
                included = {str(item["id"]) for item in envelope["included"]}  # type: ignore[index]
                expected = CANONICAL_OBJECT_IDS[sequence - 1]
                self.assertIn(expected, included, token)
                recalled.add(expected)
            self.assertEqual(recalled, set(CANONICAL_OBJECT_IDS))

    def test_one_of_nineteen_changed_references_is_partial_warned_and_included(self) -> None:
        """The historical Bubblewrap miss cannot silently drop relevant evidence."""

        with initialized_project_store() as (temporary_root, clock, store, _):
            kernel = self.kernel(store, clock)
            changed = temporary_root / "reference-corpus" / "ref-19.txt"
            changed.write_text("M2 changed synthetic reference 19.\n", encoding="utf-8")

            observations = kernel.observe_freshness(kernel.snapshot())
            observation = next(
                value_dict(item)
                for item in observations
                if value_dict(item).get("object_id") == BUBBLEWRAP_ID
            )
            self.assertEqual(observation["aggregate"], "partial")
            states = [item["state"] for item in observation["references"]]  # type: ignore[index]
            self.assertEqual(states.count("changed"), 1)
            self.assertEqual(states.count("fresh"), 18)
            self.assertIn("partially_stale", " ".join(observation["warnings"]))  # type: ignore[index]

            envelope = self.retrieve(kernel, query("bubblewrap", 20))
            included = {str(item["id"]) for item in envelope["included"]}  # type: ignore[index]
            self.assertIn(BUBBLEWRAP_ID, included)
            self.assertIn("partially_stale", " ".join(envelope["warnings"]))  # type: ignore[index]

            strict = self.retrieve(kernel, query("bubblewrap", 21, freshness_policy="strict_fresh_only"))
            strict_ids = {str(item["id"]) for item in strict["included"]}  # type: ignore[index]
            self.assertNotIn(BUBBLEWRAP_ID, strict_ids)
            omitted = [item for item in strict["relevant_but_omitted"] if item["id"] == BUBBLEWRAP_ID]  # type: ignore[index]
            self.assertEqual(len(omitted), 1)
            self.assertEqual(omitted[0]["reason"], "strict_freshness_filter")
            self.assertEqual(omitted[0]["freshness"], "partial")

    def test_retrieval_skips_irrelevant_huge_references(self) -> None:
        """Candidate filtering must happen before any reference file can be opened."""

        with initialized_project_store() as (temporary_root, clock, store, _):
            # The copied source is too large for a tight query, so it must not
            # be opened when its containing object misses the lexical filter.
            large = temporary_root / "reference-corpus" / "irrelevant-large.bin"
            large.write_bytes(b"x" * 4096)
            irrelevant_id = "mem:m2-fixture:evidence:10000000-0000-4000-8000-000000000014"
            irrelevant = make_project_object(
                irrelevant_id,
                kind="evidence",
                title="Synthetic irrelevant source guard",
                body="This source is unrelated to the requested recovery evidence.",
                references=[
                    make_reference(
                        90,
                        large.relative_to(repository_root()).as_posix(),
                        "0" * 64,
                    )
                ],
            )
            store.commit(transaction(48, [create_mutation(irrelevant)]))
            kernel = self.kernel(store, clock)
            original_read = recovery_module._read_regular_file

            def reject_irrelevant_read(path: Path, root: Path) -> bytes:
                if path.resolve() == large.resolve():
                    raise AssertionError("retrieval read an irrelevant source")
                return original_read(path, root)

            with patch.object(recovery_module, "_read_regular_file", side_effect=reject_irrelevant_read):
                envelope = self.retrieve(kernel, query("bubblewrap", 22))

            included = {str(item["id"]) for item in envelope["included"]}  # type: ignore[index]
            self.assertIn(BUBBLEWRAP_ID, included)
            self.assertNotIn(irrelevant_id, included)

    def test_retrieval_marks_oversized_relevant_reference_unverifiable(self) -> None:
        """A selected source cannot consume more bytes than its query allows."""

        with initialized_project_store() as (temporary_root, clock, store, _):
            large = temporary_root / "reference-corpus" / "bounded-large.bin"
            large.write_bytes(b"x" * 4096)
            bounded_id = "mem:m2-fixture:evidence:10000000-0000-4000-8000-000000000015"
            bounded = make_project_object(
                bounded_id,
                kind="evidence",
                title="Bounded freshness source",
                body="boundedfreshness fixture for source-read accounting.",
                references=[
                    make_reference(
                        92,
                        large.relative_to(repository_root()).as_posix(),
                        "0" * 64,
                    )
                ],
            )
            store.commit(transaction(49, [create_mutation(bounded)]))
            request = query("boundedfreshness", 23)
            request["budget"] = {
                "candidate_limit": 1,
                "object_limit": 1,
                "token_limit": 12000,
                "byte_limit": 2048,
                "timeout_ms": 2000,
            }

            envelope = self.retrieve(self.kernel(store, clock), request)

            entry = next(item for item in envelope["included"] if item["id"] == bounded_id)  # type: ignore[index]
            self.assertEqual(entry["freshness"]["aggregate"], "unverifiable")  # type: ignore[index]
            self.assertIn("freshness_unverifiable", " ".join(entry["warnings"]))  # type: ignore[index]

    def test_byte_only_markdown_edit_invalidates_index_then_direct_scans(self) -> None:
        """A physical index mismatch must never narrow results before direct scan."""

        with initialized_project_store() as (_, clock, store, _):
            kernel = self.kernel(store, clock)
            index = value_dict(kernel.build_index(kernel.snapshot()))
            before_file_hash = next(
                item["file_sha256"] for item in index["inventory"] if item["id"] == BUBBLEWRAP_ID  # type: ignore[index]
            )
            path = object_file(store, BUBBLEWRAP_ID)
            logical_byte_only_edit(path)

            snapshot = value_dict(kernel.snapshot())
            changed_snapshot = next(
                item for item in snapshot["objects"] if item["id"] == BUBBLEWRAP_ID
            )
            self.assertNotEqual(before_file_hash, changed_snapshot["file_sha256"])
            self.assertEqual(changed_snapshot["document"]["content_hash"], store.read(BUBBLEWRAP_ID)["content_hash"])

            envelope = self.retrieve(kernel, query("bubblewrap", 30))
            included = {str(item["id"]) for item in envelope["included"]}  # type: ignore[index]
            self.assertIn(BUBBLEWRAP_ID, included)
            self.assertIn("INDEX_INVALID", " ".join(envelope["warnings"]))  # type: ignore[index]
            self.assertEqual(envelope["snapshots"][0]["index_state"], "invalid_fallback_direct_scan")  # type: ignore[index]

    def test_semantic_direct_markdown_edit_fails_closed_as_degraded(self) -> None:
        """An out-of-band semantic edit is never eligible for direct-scan recovery."""

        with initialized_project_store() as (_, clock, store, _):
            kernel = self.kernel(store, clock)
            kernel.build_index(kernel.snapshot())
            path = object_file(store, BUBBLEWRAP_ID)
            path.write_bytes(path.read_bytes() + b"\nsemantic M2 authority divergence\n")

            self.assert_error_code("STORE_DEGRADED", kernel.snapshot)
            self.assert_error_code(
                "STORE_DEGRADED",
                lambda: self.retrieve(kernel, query("bubblewrap", 31)),
            )

    def test_moc_covers_active_objects_and_dangling_relation_is_rejected(self) -> None:
        """MOC views are derived from active authority, while dangling edges fail closed."""

        with initialized_project_store() as (_, clock, store, _):
            kernel = self.kernel(store, clock)
            archived = deepcopy(store.read(ARCHIVED_ID))
            archived["revision"] = 2
            archived["updated_at"] = "2026-07-22T00:00:01Z"
            archived["lifecycle"] = {
                "status": "archived",
                "changed_at": "2026-07-22T00:00:01Z",
                "reason": "synthetic M2 MOC coverage fixture",
            }
            archived = rehash(archived)
            store.commit(transaction(2, [replace_mutation(store.read(ARCHIVED_ID), archived, operation="transition")]))

            views = kernel.generate_views(kernel.snapshot())
            moc = self.view_text(views, "moc", store.root)
            log = self.view_text(views, "log", store.root)
            self.assertIn("do_not_edit", moc)
            self.assertIn("corpus_digest", moc)
            for object_id_value in CANONICAL_OBJECT_IDS:
                self.assertEqual(moc.count(object_id_value), 1, object_id_value)
            self.assertNotIn(ARCHIVED_ID, moc)
            kind_order = {
                "project": 0,
                "decision": 1,
                "component": 2,
                "task": 3,
                "bug": 4,
                "experiment": 5,
                "evidence": 6,
                "question": 7,
            }
            expected_order = [
                object_id(kind, identifier)
                for kind, identifier, title, _ in sorted(
                    CANONICAL_SPECS,
                    key=lambda item: (kind_order[item[0]], item[2].casefold(), object_id(item[0], item[1])),
                )
            ]
            positions = [moc.index(object_id_value) for object_id_value in expected_order]
            self.assertEqual(positions, sorted(positions))
            self.assertIn("transaction", log.lower())

            dangling = make_project_object(
                DANGLING_ID,
                kind="question",
                title="Synthetic dangling relation",
                body="The M2 fixture must reject an unresolved local relation.",
                relations=[make_relation("mem:m2-fixture:evidence:10000000-0000-4000-8000-000000000199", suffix=99)],
            )
            self.assert_error_code(
                "RELATION_INVALID",
                lambda: store.commit(transaction(3, [create_mutation(dangling)])),
            )

    def test_receipt_rejections_fail_closed_without_authority_mutation(self) -> None:
        """All receipt failures reject before injection and preserve M1 authority bytes."""

        with initialized_project_store() as (_, clock, store, _):
            kernel = self.kernel(store, clock)
            pack, receipt = kernel.pre_compact(SESSION_ID, QueryRequest.from_value(query("bubblewrap", 40)))
            restored = kernel.post_compact(receipt, pack, SESSION_ID)
            self.assertEqual(value_dict(restored)["packet_digest"], value_dict(pack)["packet_digest"])
            self.assertEqual(value_dict(receipt)["sealed_at"], clock.now_rfc3339())

            before = authority_digest(store.root)
            tampered = value_dict(receipt)
            tampered["hmac_sha256"] = "0" * 64 if tampered["hmac_sha256"] != "0" * 64 else "f" * 64
            self.assert_error_code(
                "RECOVERY_RECEIPT_INVALID",
                lambda: kernel.post_compact(tampered, pack, SESSION_ID),
            )
            self.assertEqual(authority_digest(store.root), before)

            forged_pack = replace(
                pack,
                sections=(
                    {
                        "name": "active_state",
                        "entries": [{"text": "forged typed recovery payload"}],
                    },
                ),
            )
            self.assert_error_code(
                "RECOVERY_RECEIPT_PACK_MISMATCH",
                lambda: kernel.post_compact(receipt, forged_pack, SESSION_ID),
            )
            self.assertEqual(authority_digest(store.root), before)

            self.assert_error_code(
                "RECOVERY_RECEIPT_SESSION_MISMATCH",
                lambda: kernel.post_compact(receipt, pack, "session:other"),
            )
            self.assertEqual(authority_digest(store.root), before)

            other_pack = self.recovery_pack(kernel, query("m2canonicalproject", 41))
            self.assert_error_code(
                "RECOVERY_RECEIPT_PACK_MISMATCH",
                lambda: kernel.post_compact(receipt, other_pack, SESSION_ID),
            )
            self.assertEqual(authority_digest(store.root), before)

        with initialized_project_store() as (_, clock, store, _):
            kernel = self.kernel(store, clock)
            pack, receipt = kernel.pre_compact(SESSION_ID, QueryRequest.from_value(query("bubblewrap", 44)))
            checkpoint = next((store.root / "runtime" / "m2-recovery" / "checkpoints").glob("*.json"))
            tampered_checkpoint = json.loads(checkpoint.read_text(encoding="utf-8"))
            tampered_checkpoint["packet_id"] = "ctx:tampered-checkpoint"
            checkpoint.write_text(json.dumps(tampered_checkpoint, sort_keys=True), encoding="utf-8")
            self.assert_error_code(
                "RECOVERY_RECEIPT_INVALID",
                lambda: kernel.post_compact(receipt, pack, SESSION_ID),
            )

        with initialized_project_store() as (_, clock, store, _):
            kernel = self.kernel(store, clock)
            pack, receipt = kernel.pre_compact(SESSION_ID, QueryRequest.from_value(query("bubblewrap", 42)))
            before = authority_digest(store.root)
            clock.advance(seconds=366 * 24 * 60 * 60)
            self.assert_error_code(
                "RECOVERY_RECEIPT_EXPIRED",
                lambda: kernel.post_compact(receipt, pack, SESSION_ID),
            )
            self.assertEqual(authority_digest(store.root), before)

        with initialized_project_store() as (_, clock, store, _):
            kernel = self.kernel(store, clock)
            pack, receipt = kernel.pre_compact(SESSION_ID, QueryRequest.from_value(query("bubblewrap", 43)))
            changed_state = make_project_object(
                NEW_STATE_ID,
                kind="task",
                title="Synthetic post-receipt authority mutation",
                body="This valid mutation proves a receipt binds an exact M1 snapshot.",
            )
            store.commit(transaction(4, [create_mutation(changed_state)]))
            before = authority_digest(store.root)
            self.assert_error_code(
                "RECOVERY_RECEIPT_STATE_MISMATCH",
                lambda: kernel.post_compact(receipt, pack, SESSION_ID),
            )
            self.assertEqual(authority_digest(store.root), before)

    def test_legacy_inventory_and_mapping_are_read_only_and_quarantine_invalid_input(self) -> None:
        """Migration inventory operates on a copied fixture and never writes source bytes."""

        with initialized_project_store() as (temporary_root, clock, store, _):
            kernel = self.kernel(store, clock)
            copied = temporary_root / "legacy-copy"
            shutil.copytree(LEGACY_FIXTURE, copied)
            fixture_before = tree_digest(copied)
            repository_fixture_before = tree_digest(LEGACY_FIXTURE)
            authority_before = authority_digest(store.root)

            inventory = kernel.inventory_fixture(copied)
            report = kernel.dry_run_map(inventory)

            self.assertEqual(tree_digest(copied), fixture_before)
            self.assertEqual(tree_digest(LEGACY_FIXTURE), repository_fixture_before)
            self.assertEqual(authority_digest(store.root), authority_before)
            rendered = json.dumps({"inventory": value_dict(inventory), "report": value_dict(report)}, sort_keys=True)
            self.assertIn("legacy-bubblewrap", rendered)
            self.assertIn("dangling-relation", rendered)
            self.assertIn("unsafe-reference", rendered)
            self.assertIn("runtime/receipt.json", rendered)
            self.assertIn("derived/memory.sqlite", rendered)
            self.assertIn("handoff.md", rendered)

    def test_legacy_inventory_never_reads_or_binds_excluded_private_bytes(self) -> None:
        """Runtime, derived, and handoff files are metadata-only exclusions."""

        with initialized_project_store() as (temporary_root, clock, store, _):
            kernel = self.kernel(store, clock)
            copied = temporary_root / "legacy-excluded-copy"
            shutil.copytree(LEGACY_FIXTURE, copied)
            excluded_paths = (
                copied / "runtime" / "receipt.json",
                copied / "derived" / "memory.sqlite",
                copied / "handoff.md",
            )
            marker = "M2-EXCLUDED-PRIVATE-MARKER"
            for path in excluded_paths:
                path.write_text(f"{marker}:{path.name}\n", encoding="utf-8")
            original_read = recovery_module._read_regular_file

            def reject_excluded_read(path: Path, root: Path) -> bytes:
                if path.resolve() in {item.resolve() for item in excluded_paths}:
                    raise AssertionError("inventory read an excluded private file")
                return original_read(path, root)

            with patch.object(recovery_module, "_read_regular_file", side_effect=reject_excluded_read):
                first = value_dict(kernel.inventory_fixture(copied))

            for path in excluded_paths:
                path.write_text(f"{marker}:changed:{path.name}\n", encoding="utf-8")
            second = value_dict(kernel.inventory_fixture(copied))

            self.assertEqual(first["fixture_digest"], second["fixture_digest"])
            self.assertNotIn(marker, json.dumps(first, sort_keys=True))

    def test_legacy_inventory_does_not_retain_nested_source_payloads(self) -> None:
        """Untrusted v1 nested fields are reduced to safe mapping metadata."""

        with initialized_project_store() as (temporary_root, clock, store, _):
            kernel = self.kernel(store, clock)
            copied = temporary_root / "legacy-nested-copy"
            shutil.copytree(LEGACY_FIXTURE, copied)
            injected = copied / "memory" / "objects" / "evidence" / "nested-source.json"
            injected.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "id": "mem:m2-fixture:evidence:10000000-0000-4000-8000-000000000105",
                        "kind": "evidence",
                        "title": "Legacy nested metadata fixture",
                        "revision": 1,
                        "verification_state": {"raw": "M2-NESTED-SECRET-MARKER"},
                        "relations": [
                            {
                                "target": "mem:m2-fixture:task:10000000-0000-4000-8000-000000000102",
                                "note": "M2-NESTED-SECRET-MARKER",
                            }
                        ],
                        "references": [
                            {
                                "kind": "file",
                                "locator": "fixtures/m2/reference-corpus/ref-01.txt",
                                "freshness_policy": ["invalid-unhashable-policy"],
                                "nested": {"prompt": "M2-NESTED-SECRET-MARKER"},
                            }
                        ],
                        "body": "M2-NESTED-SECRET-MARKER",
                    }
                ),
                encoding="utf-8",
            )
            inventory = value_dict(kernel.inventory_fixture(copied))
            rendered = json.dumps(inventory, sort_keys=True)
            self.assertNotIn("M2-NESTED-SECRET-MARKER", rendered)
            nested = next(item for item in inventory["objects"] if item["legacy_id"].endswith("000000000105"))
            self.assertEqual(nested["references"][0]["freshness_policy"], "unknown")

    def test_receipt_pack_uses_one_snapshot_and_enforces_final_budget(self) -> None:
        """A concurrent M1 commit causes a retry, never a mixed snapshot pack."""

        with initialized_project_store() as (_, clock, store, _):
            kernel = self.kernel(store, clock)
            original = kernel._build_recovery_pack_for_snapshot
            calls = 0

            def mutate_once(request: QueryRequest, snapshot: object) -> object:
                nonlocal calls
                result = original(request, snapshot)  # type: ignore[arg-type]
                if calls == 0:
                    calls += 1
                    store.commit(
                        transaction(
                            9,
                            [
                                create_mutation(
                                    make_project_object(
                                        NEW_STATE_ID,
                                        kind="task",
                                        title="Concurrent sealing mutation",
                                        body="A synthetic concurrent M1 mutation.",
                                    )
                                )
                            ],
                        )
                    )
                return result

            with patch.object(kernel, "_build_recovery_pack_for_snapshot", side_effect=mutate_once):
                pack, receipt = kernel.pre_compact(SESSION_ID, query("bubblewrap", 45))
            pack_value = value_dict(pack)
            receipt_value = value_dict(receipt)
            self.assertEqual(pack_value["snapshots"][0]["mutation_epoch"], receipt_value["source"]["mutation_epoch"])
            self.assertEqual(pack_value["snapshots"][0]["corpus_digest"], receipt_value["source"]["state_digest"])
            self.assertEqual(len(canonical_jcs_bytes(pack_value)), pack_value["budget"]["used_bytes"])
            self.assertEqual((pack_value["budget"]["used_bytes"] + 3) // 4, pack_value["budget"]["estimated_tokens"])
            self.assertLessEqual(pack_value["budget"]["used_bytes"], pack_value["budget"]["byte_limit"])
            self.assertLessEqual(pack_value["budget"]["estimated_tokens"], pack_value["budget"]["token_limit"])
            self.assertEqual(value_dict(kernel.post_compact(receipt, pack, SESSION_ID))["packet_digest"], pack_value["packet_digest"])

    def test_supplied_snapshots_and_key_paths_are_revalidated(self) -> None:
        """Derived rendering never trusts mutable snapshots or external HMAC keys."""

        with initialized_project_store() as (temporary_root, clock, store, _):
            kernel = self.kernel(store, clock)
            snapshot = kernel.snapshot()
            snapshot.objects[0].document["payload"]["fixture"] = "forged-m2-derived-state"  # type: ignore[index]
            self.assert_error_code("RECOVERY_RECEIPT_STATE_MISMATCH", lambda: kernel.generate_views(snapshot))

            external_key = temporary_root / "external-receipt.key"
            external_key.write_bytes(b"x" * 32)
            os.chmod(external_key, 0o600)
            external_kernel = ProjectRecoveryKernel(
                store, repository_root(), clock, receipt_key_provider=external_key
            )
            self.assert_error_code(
                "PATH_UNSAFE",
                lambda: external_kernel.pre_compact(SESSION_ID, query("bubblewrap", 46)),
            )

            private_key = store.root / "runtime" / "m2-recovery" / "weak.key"
            private_key.parent.mkdir(parents=True, exist_ok=True)
            private_key.write_bytes(b"short")
            os.chmod(private_key, 0o600)
            weak_kernel = ProjectRecoveryKernel(store, repository_root(), clock, receipt_key_provider=private_key)
            self.assert_error_code(
                "RECOVERY_RECEIPT_INVALID",
                lambda: weak_kernel.pre_compact(SESSION_ID, query("bubblewrap", 47)),
            )

    def test_query_and_freshness_capture_repository_snapshot_provenance(self) -> None:
        """Repository scope survives request validation and observations bind ref state."""

        request = query("bubblewrap", 48)
        request["scope"] = {
            "project_ids": ["m2-fixture"],
            "include_global": False,
            "branch": "main",
            "repository_snapshot": "git:synthetic-m2-snapshot",
        }
        parsed = QueryRequest.from_value(request)
        self.assertEqual(parsed.to_dict()["scope"]["branch"], "main")
        self.assertEqual(parsed.to_dict()["scope"]["repository_snapshot"], "git:synthetic-m2-snapshot")

        with initialized_project_store() as (_, clock, store, _):
            kernel = self.kernel(store, clock)
            observation = next(
                value_dict(item)
                for item in kernel.observe_freshness(kernel.snapshot())
                if value_dict(item)["object_id"] == BUBBLEWRAP_ID
            )
            source_snapshot = observation["snapshot"]
            self.assertEqual(len(source_snapshot["reference_observation_digest"]), 64)
            self.assertIn("project_root", source_snapshot)

    def test_freshness_and_inventory_reject_symlink_escapes(self) -> None:
        """Reference and legacy inventory path traversal cannot cross a symlink."""

        with initialized_project_store() as (temporary_root, clock, store, _):
            kernel = self.kernel(store, clock)
            reference = temporary_root / "reference-corpus" / "ref-01.txt"
            outside = temporary_root / "outside-reference.txt"
            outside.write_text("synthetic outside target\n", encoding="utf-8")
            reference.unlink()
            try:
                os.symlink(outside, reference)
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"symlink fixtures are unavailable: {error}")
            self.assert_error_code("PATH_UNSAFE", lambda: kernel.observe_freshness(kernel.snapshot()))

            copied = temporary_root / "legacy-symlink-copy"
            shutil.copytree(LEGACY_FIXTURE, copied)
            escaped = copied / "memory" / "objects" / "evidence" / "escaped.json"
            try:
                os.symlink(outside, escaped)
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"symlink fixtures are unavailable: {error}")
            self.assert_error_code("PATH_UNSAFE", lambda: kernel.inventory_fixture(copied))


if __name__ == "__main__":
    unittest.main()
