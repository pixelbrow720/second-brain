"""M4 end-to-end acceptance coverage over only synthetic local stores."""

from __future__ import annotations

from pathlib import Path
import unittest

from second_brain.retrieval import ContextCompiler, FederatedRetriever, StoreRegistry
from second_brain.storage import Mutation, Store, TransactionRequest

from tests.m2_recovery_helpers import BUBBLEWRAP_ID, CANONICAL_OBJECT_IDS, CANONICAL_SPECS, initialized_project_store, query
from tests.test_m3_integration import _ACTOR, _base_document, _object_id


def _payload(statement: str) -> dict[str, object]:
    return {
        "claim_type": "definitional",
        "statement": statement,
        "subject_ids": [],
        "predicate": "is",
        "scope": {"kind": "global"},
        "valid_time": {"start": None, "end": None},
        "source_time": {"start": None, "end": None},
        "support_ids": [],
        "contradiction_group": None,
    }


class M4IntegrationTests(unittest.TestCase):
    def setup_corpus(self, root: Path, clock: object, project: Store) -> tuple[Store, FederatedRetriever]:
        global_store = Store.initialize(
            root / "global-store",
            "knowledge:global",
            clock=clock,
            authorizer=lambda *args, **kwargs: True,
        )
        documents: list[Mutation] = []
        for number in range(1, 4):
            object_id = _object_id("claim", 500 + number)
            document = _base_document(
                object_id,
                "claim",
                f"M4 global canonical target {number}",
                _payload(f"m4globalcanonical{number} is an exact synthetic retrieval target."),
            )
            documents.append(Mutation("create", object_id, None, None, document))
        global_store.commit(
            TransactionRequest(
                transaction_id="txn:90000000-0000-4000-8000-000000000100",
                idempotency_key="m4-integration-global",
                store_id="knowledge:global",
                actor=_ACTOR,
                confirmation=None,
                mutations=tuple(documents),
                reason="synthetic M4 integration corpus",
            )
        )
        registry = StoreRegistry()
        registry.register_project(project)
        registry.register_global(global_store)
        return global_store, FederatedRetriever(registry, clock=clock)

    @staticmethod
    def projection(envelope: object) -> tuple[tuple[str, int, str, str], ...]:
        return tuple(
            (item["id"], item["revision"], item["citation"], item["freshness"]["aggregate"])
            for item in envelope.included  # type: ignore[attr-defined]
        )

    def test_canonical_project_and_global_recall_and_index_rebuild_equivalence(self) -> None:
        with initialized_project_store() as (root, clock, project, _):
            global_store, retriever = self.setup_corpus(root, clock, project)
            project_recalled: set[str] = set()
            for sequence, (_, _, _, token) in enumerate(CANONICAL_SPECS, start=1):
                envelope = retriever.retrieve(query(token, sequence))
                expected = CANONICAL_OBJECT_IDS[sequence - 1]
                self.assertIn(expected, {item["id"] for item in envelope.included})
                project_recalled.add(expected)
            self.assertEqual(project_recalled, set(CANONICAL_OBJECT_IDS))

            global_recalled: set[str] = set()
            for number in range(1, 4):
                request = query(f"m4globalcanonical{number}", 30 + number)
                request["scope"] = {"project_ids": [], "include_global": True, "branch": None, "repository_snapshot": None}
                envelope = retriever.retrieve(request)
                expected = _object_id("claim", 500 + number)
                self.assertIn(expected, [item["id"] for item in envelope.included[:3]])
                global_recalled.add(expected)
            self.assertEqual(len(global_recalled) / 3, 1.0)

            request = query("bubblewrap", 40)
            before = retriever.retrieve(request)
            retriever.build_indexes(("project:m2-fixture", "knowledge:global"))
            indexed = retriever.retrieve(query("bubblewrap", 41))
            self.assertEqual(self.projection(before), self.projection(indexed))
            database = project.root / "derived" / "m4-retrieval" / "lexical.sqlite"
            manifest = project.root / "derived" / "m4-retrieval" / "manifest.json"
            database.unlink()
            manifest.unlink()
            rebuilt_fallback = retriever.retrieve(query("bubblewrap", 42))
            self.assertEqual(self.projection(before), self.projection(rebuilt_fallback))
            self.assertEqual(global_store.snapshot().store_id, "knowledge:global")

    def test_partial_recovery_context_has_complete_provenance_and_no_irrelevant_objects(self) -> None:
        with initialized_project_store() as (root, clock, project, _):
            _, retriever = self.setup_corpus(root, clock, project)
            (root / "reference-corpus" / "ref-19.txt").write_text("changed integration reference\n", encoding="utf-8")
            envelope = retriever.retrieve(query("bubblewrap", 50))
            self.assertIn(BUBBLEWRAP_ID, {item["id"] for item in envelope.included})
            entry = next(item for item in envelope.included if item["id"] == BUBBLEWRAP_ID)
            self.assertEqual(entry["freshness"]["aggregate"], "partial")
            self.assertTrue(entry["provenance_ids"])

            packet = ContextCompiler(retriever, clock=clock).compile(
                envelope,
                "Resume Bubblewrap verification.",
                ["m2-fixture"],
                "Tera Max",
                purpose="recovery",
            )
            citations = set(packet.citation_map)
            compiled_entries = [
                entry
                for section in packet.sections
                if section["name"] in {"active_state", "knowledge", "evidence"}
                for entry in section["entries"]
            ]
            self.assertEqual(len(compiled_entries), 1)
            self.assertEqual(compiled_entries[0]["citation"], BUBBLEWRAP_ID + "@1")
            self.assertEqual(citations, {BUBBLEWRAP_ID + "@1"})
            self.assertIn("FRESHNESS_PARTIAL", " ".join(packet.warnings))
