"""M4 federation, FTS fallback, freshness, and authority-boundary regressions."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from second_brain.errors import StorageError
from second_brain.retrieval import (
    ContextCompiler,
    FederatedQueryRequest,
    FederatedRetriever,
    QualifiedLink,
    StoreRegistry,
)
from second_brain.storage import Mutation, Store, TransactionRequest

from tests.m2_recovery_helpers import BUBBLEWRAP_ID, initialized_project_store, query
from tests.test_m3_integration import _ACTOR, _base_document, _object_id, _rehash, _relation


def _claim_payload(statement: str) -> dict[str, object]:
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


def _global_transaction(sequence: int, mutations: tuple[Mutation, ...]) -> TransactionRequest:
    return TransactionRequest(
        transaction_id=f"txn:90000000-0000-4000-8000-{sequence:012d}",
        idempotency_key=f"m4-global-{sequence}",
        store_id="knowledge:global",
        actor=_ACTOR,
        confirmation=None,
        mutations=mutations,
        reason="synthetic M4 retrieval fixture",
    )


class M4RetrievalTests(unittest.TestCase):
    """Use only project-contained synthetic M1/M2/M3 stores."""

    def global_store(self, root: Path, clock: object) -> Store:
        return Store.initialize(
            root / "global-store",
            "knowledge:global",
            clock=clock,
            authorizer=lambda *args, **kwargs: True,
        )

    def registry(self, project: Store, global_store: Store | None = None) -> StoreRegistry:
        registry = StoreRegistry()
        registry.register_project(project)
        if global_store is not None:
            registry.register_global(global_store)
        return registry

    def test_direct_or_r0_creates_no_retrieval_or_store_read(self) -> None:
        with initialized_project_store() as (_, clock, project, _):
            registry = self.registry(project)
            retriever = FederatedRetriever(registry, clock=clock)
            request = query("bubblewrap", 1)
            request["tier"] = "R0"
            self.assertIsNone(FederatedQueryRequest.for_lane("DIRECT", request))
            with patch.object(project, "snapshot", side_effect=AssertionError("R0 must not snapshot")):
                with self.assertRaises(StorageError) as caught:
                    retriever.retrieve(request)
            self.assertEqual(caught.exception.code, "DIRECT_NO_RETRIEVAL")

            secret_id = query("bubblewrap", 101)
            secret_id["query_id"] = "qry:RECEIPT-SECRET-MARKER"
            with self.assertRaises(StorageError) as secret_error:
                retriever.retrieve(secret_id)
            self.assertEqual(secret_error.exception.code, "SCHEMA_INVALID")

            unsafe_gate = query("bubblewrap", 102, freshness_policy="diagnostic_no_check")
            unsafe_gate["purpose"] = "verification_gate"
            with self.assertRaises(StorageError) as gate_error:
                retriever.retrieve(unsafe_gate)
            self.assertEqual(gate_error.exception.code, "AUTHORITY_DENIED")

    def test_registry_pins_concrete_root_identity_and_explicit_scope(self) -> None:
        with initialized_project_store() as (root, clock, project, _):
            registry = self.registry(project)
            duplicate = Store.initialize(
                root / "duplicate-project-store",
                "project:m2-fixture",
                clock=clock,
                authorizer=lambda *args, **kwargs: True,
            )
            with self.assertRaises(StorageError) as duplicate_error:
                registry.register_project(duplicate)
            self.assertEqual(duplicate_error.exception.code, "SCHEMA_INVALID")

            alternate = Store.initialize(
                root / "alternate-project-store",
                "project:m2-fixture-alt",
                clock=clock,
                authorizer=lambda *args, **kwargs: True,
            )
            project.root = alternate.root
            with self.assertRaises(StorageError) as root_error:
                registry.capture("project:m2-fixture")
            self.assertEqual(root_error.exception.code, "AUTHORITY_DENIED")

    def test_scope_does_not_scan_registered_global_without_request_or_link(self) -> None:
        with initialized_project_store() as (root, clock, project, _):
            global_store = self.global_store(root, clock)
            global_id = _object_id("claim", 401)
            global_document = _base_document(
                global_id,
                "claim",
                "Bubblewrap global knowledge",
                _claim_payload("Bubblewrap is global-only synthetic knowledge."),
            )
            global_store.commit(_global_transaction(1, (Mutation("create", global_id, None, None, global_document),)))
            retriever = FederatedRetriever(self.registry(project, global_store), clock=clock)

            project_only = retriever.retrieve(query("bubblewrap", 2))
            self.assertNotIn(global_id, {item["id"] for item in project_only.included})

            global_query = query("bubblewrap", 3)
            global_query["scope"] = {"project_ids": [], "include_global": True, "branch": None, "repository_snapshot": None}
            global_only = retriever.retrieve(global_query)
            self.assertIn(global_id, {item["id"] for item in global_only.included})

    def test_index_corruption_and_deletion_fall_back_to_authoritative_scan(self) -> None:
        with initialized_project_store() as (_, clock, project, _):
            retriever = FederatedRetriever(self.registry(project), clock=clock)
            baseline = retriever.retrieve(query("bubblewrap", 4))
            baseline_projection = [(item["id"], item["citation"]) for item in baseline.included]
            build = retriever.build_index("project:m2-fixture")
            self.assertEqual(build["index_state"], "valid_fts5_verified_direct_scan")
            indexed = retriever.retrieve(query("bubblewrap", 5))
            self.assertEqual(baseline_projection, [(item["id"], item["citation"]) for item in indexed.included])
            self.assertEqual(indexed.metrics["index_path"], "fts5_verified_direct_scan")

            database = project.root / "derived" / "m4-retrieval" / "lexical.sqlite"
            database.write_bytes(database.read_bytes() + b"M4 index corruption fixture")
            corrupt = retriever.retrieve(query("bubblewrap", 6))
            self.assertEqual(baseline_projection, [(item["id"], item["citation"]) for item in corrupt.included])
            self.assertIn("INDEX_INVALID", " ".join(corrupt.warnings))

            manifest = project.root / "derived" / "m4-retrieval" / "manifest.json"
            database.unlink()
            manifest.unlink()
            deleted = retriever.retrieve(query("bubblewrap", 7))
            self.assertEqual(baseline_projection, [(item["id"], item["citation"]) for item in deleted.included])

            retriever.build_index("project:m2-fixture")
            database.unlink()
            rogue = project.root / "rogue-index.sqlite"
            rogue.write_bytes(b"not an index")
            database.symlink_to(rogue)
            symlinked = retriever.retrieve(query("bubblewrap", 70))
            self.assertEqual(baseline_projection, [(item["id"], item["citation"]) for item in symlinked.included])
            self.assertIn("INDEX_INVALID", " ".join(symlinked.warnings))

    def test_partial_evidence_is_included_and_strict_policy_names_exclusion(self) -> None:
        with initialized_project_store() as (root, clock, project, _):
            (root / "reference-corpus" / "ref-19.txt").write_text("changed M4 freshness fixture\n", encoding="utf-8")
            retriever = FederatedRetriever(self.registry(project), clock=clock)
            warned = retriever.retrieve(query("bubblewrap", 8))
            entry = next(item for item in warned.included if item["id"] == BUBBLEWRAP_ID)
            self.assertEqual(entry["freshness"]["aggregate"], "partial")
            self.assertIn("FRESHNESS_PARTIAL", " ".join(entry["warnings"]))

            strict_request = query("bubblewrap", 9, freshness_policy="strict_fresh_only")
            strict = retriever.retrieve(strict_request)
            self.assertNotIn(BUBBLEWRAP_ID, {item["id"] for item in strict.included})
            omission = next(item for item in strict.relevant_but_omitted if item["id"] == BUBBLEWRAP_ID)
            self.assertEqual(omission["reason"], "strict_freshness_filter")
            self.assertEqual(omission["freshness"], "partial")

    def test_qualified_link_is_query_local_and_dangling_target_is_visible(self) -> None:
        with initialized_project_store() as (root, clock, project, _):
            global_store = self.global_store(root, clock)
            global_id = _object_id("claim", 402)
            document = _base_document(
                global_id,
                "claim",
                "Federated global target",
                _claim_payload("A project-qualified link may expose this global claim."),
            )
            global_store.commit(_global_transaction(2, (Mutation("create", global_id, None, None, document),)))
            link = QualifiedLink(BUBBLEWRAP_ID, global_id, "supports", expected_target_revision=1)
            retriever = FederatedRetriever(self.registry(project, global_store), qualified_links=[link], clock=clock)
            linked = retriever.retrieve(query("bubblewrap", 10))
            self.assertIn(global_id, {item["id"] for item in linked.included})

            missing = _object_id("claim", 499)
            dangling = FederatedRetriever(
                self.registry(project, global_store),
                qualified_links=[QualifiedLink(BUBBLEWRAP_ID, missing, "supports")],
                clock=clock,
            ).retrieve(query("bubblewrap", 11))
            self.assertIn("DANGLING_EXTERNAL", " ".join(dangling.warnings))
            self.assertIn(missing, {item["id"] for item in dangling.rejected})

    def test_contradiction_pair_is_never_split_by_object_budget(self) -> None:
        with initialized_project_store() as (root, clock, project, _):
            global_store = self.global_store(root, clock)
            low_id = _object_id("claim", 403)
            high_id = _object_id("claim", 404)
            low = _base_document(
                low_id,
                "claim",
                "Synthetic lower position",
                _claim_payload("The lower position is disputed."),
                relations=[_relation(low_id, high_id, 450, "contradicts")],
            )
            high = _base_document(
                high_id,
                "claim",
                "Find synthetic higher position",
                _claim_payload("The higher position is disputed."),
            )
            global_store.commit(
                _global_transaction(
                    3,
                    (Mutation("create", low_id, None, None, low), Mutation("create", high_id, None, None, high)),
                )
            )
            retriever = FederatedRetriever(self.registry(project, global_store), clock=clock)
            request = query("find synthetic higher", 12)
            request["scope"] = {"project_ids": [], "include_global": True, "branch": None, "repository_snapshot": None}
            request["relation"] = {"max_depth": 1, "max_fanout": 8, "types": ["contradicts"]}
            request["budget"]["candidate_limit"] = 1
            request["budget"]["object_limit"] = 1
            result = retriever.retrieve(request)
            self.assertNotIn(low_id, {item["id"] for item in result.included})
            self.assertNotIn(high_id, {item["id"] for item in result.included})
            omitted = {item["id"]: item for item in result.relevant_but_omitted}
            self.assertEqual(omitted[low_id]["reason"], "contradiction_group_budget")
            self.assertEqual(omitted[high_id]["reason"], "contradiction_group_budget")
            self.assertIn("CONTRADICTION_OMITTED", " ".join(result.warnings))

    def test_default_lifecycle_surfaces_historical_evidence_with_warning(self) -> None:
        with initialized_project_store() as (root, clock, project, _):
            global_store = self.global_store(root, clock)
            historical_id = _object_id("claim", 405)
            document = _base_document(
                historical_id,
                "claim",
                "Historical M4 lifecycle target",
                _claim_payload("m4historicaltarget remains relevant as historical evidence."),
            )
            global_store.commit(_global_transaction(4, (Mutation("create", historical_id, None, None, document),)))
            historical = deepcopy(global_store.read(historical_id))
            historical["revision"] = 2
            historical["lifecycle"] = {
                "status": "superseded",
                "changed_at": "2026-07-22T00:00:00Z",
                "reason": "Synthetic replacement for M4 visibility coverage.",
            }
            historical = _rehash(historical)
            global_store.commit(
                _global_transaction(
                    5,
                    (
                        Mutation(
                            "transition",
                            historical_id,
                            1,
                            document["content_hash"],
                            historical,
                        ),
                    ),
                )
            )
            retriever = FederatedRetriever(self.registry(project, global_store), clock=clock)
            request = query("m4historicaltarget", 16)
            request["scope"] = {"project_ids": [], "include_global": True, "branch": None, "repository_snapshot": None}
            request["filters"].pop("lifecycle")
            result = retriever.retrieve(request)
            entry = next(item for item in result.included if item["id"] == historical_id)
            self.assertEqual(entry["lifecycle"], "superseded")
            self.assertIn("HISTORICAL_EVIDENCE", entry["warnings"])
            packet = ContextCompiler(retriever, clock=clock).compile(
                result, "Review historical evidence", [], "Tera Max"
            )
            conflicts = next(section["entries"] for section in packet.sections if section["name"] == "conflicts_and_freshness")
            self.assertTrue(any(item["citation"] == historical_id + "@2" for item in conflicts))

    def test_candidate_limit_omits_entire_multi_peer_contradiction_component(self) -> None:
        with initialized_project_store() as (root, clock, project, _):
            global_store = self.global_store(root, clock)
            source_id = _object_id("claim", 406)
            peer_one = _object_id("claim", 407)
            peer_two = _object_id("claim", 408)
            source = _base_document(
                source_id,
                "claim",
                "Find multi-peer contradiction source",
                _claim_payload("m4multipeerroot is disputed by two peers."),
                relations=[
                    _relation(source_id, peer_one, 460, "contradicts"),
                    _relation(source_id, peer_two, 461, "contradicts"),
                ],
            )
            first = _base_document(peer_one, "claim", "First nonmatching peer", _claim_payload("first peer"))
            second = _base_document(peer_two, "claim", "Second nonmatching peer", _claim_payload("second peer"))
            global_store.commit(
                _global_transaction(
                    6,
                    (
                        Mutation("create", source_id, None, None, source),
                        Mutation("create", peer_one, None, None, first),
                        Mutation("create", peer_two, None, None, second),
                    ),
                )
            )
            retriever = FederatedRetriever(self.registry(project, global_store), clock=clock)
            request = query("m4multipeerroot", 17)
            request["scope"] = {"project_ids": [], "include_global": True, "branch": None, "repository_snapshot": None}
            request["relation"] = {"max_depth": 1, "max_fanout": 8, "types": ["contradicts"]}
            request["budget"]["candidate_limit"] = 1
            request["budget"]["object_limit"] = 1
            result = retriever.retrieve(request)
            self.assertTrue({source_id, peer_one, peer_two}.isdisjoint({item["id"] for item in result.included}))
            omissions = {item["id"]: item for item in result.relevant_but_omitted}
            self.assertEqual({source_id, peer_one, peer_two}, set(omissions).intersection({source_id, peer_one, peer_two}))
            self.assertEqual({item["selection_group"] for item in omissions.values() if item["id"] in {source_id, peer_one, peer_two}}, {next(item["selection_group"] for item in omissions.values() if item["id"] == source_id)})
            self.assertTrue(all(omissions[item_id]["reason"] == "contradiction_group_budget" for item_id in (source_id, peer_one, peer_two)))

    def test_unavailable_global_degrades_without_hiding_project_result(self) -> None:
        with initialized_project_store() as (_, clock, project, _):
            retriever = FederatedRetriever(self.registry(project), clock=clock)
            request = query("bubblewrap", 13)
            request["scope"]["include_global"] = True
            result = retriever.retrieve(request)
            self.assertEqual(result.status, "partial")
            self.assertIn(BUBBLEWRAP_ID, {item["id"] for item in result.included})
            self.assertIn("GLOBAL_UNAVAILABLE", " ".join(result.warnings))

    def test_disabled_adapter_cannot_run_and_enabled_failure_falls_back(self) -> None:
        class Adapter:
            def __init__(self) -> None:
                self.calls = 0

            def search(self, **_: object) -> None:
                self.calls += 1
                raise RuntimeError("synthetic optional adapter outage")

        with initialized_project_store() as (_, clock, project, _):
            adapter = Adapter()
            disabled = FederatedRetriever(self.registry(project), vector_adapter=adapter, clock=clock)
            disabled_result = disabled.retrieve(query("bubblewrap", 14))
            self.assertEqual(adapter.calls, 0)
            self.assertIn("VECTOR_ADAPTER_DISABLED", " ".join(disabled_result.warnings))

            enabled = FederatedRetriever(
                self.registry(project), vector_adapter=adapter, enable_vector_adapter=True, clock=clock
            )
            enabled_result = enabled.retrieve(query("bubblewrap", 15))
            self.assertEqual(adapter.calls, 1)
            self.assertIn(BUBBLEWRAP_ID, {item["id"] for item in enabled_result.included})
            self.assertIn("VECTOR_ADAPTER_UNAVAILABLE", " ".join(enabled_result.warnings))
