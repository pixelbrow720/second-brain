from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from second_brain.activation_v2 import activation_v2_logical_digest
from second_brain.activation_v2_graph import (
    DerivedGraphError,
    FocusGraphView,
    compile_focus_graph_view,
    load_focus_graph_view,
)
from second_brain.activation_v2_runtime import (
    PolicyInputs,
    initialize_disposable_runtime,
    runtime_health,
)
from second_brain.contracts import validate_named_document
from second_brain.errors import ContentPolicyError, IntegrityError
from second_brain.jsonio import canonical_json_bytes, load_strict_json
from second_brain.workspace import repository_root


class ActivationV2A5DerivedFocusGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        test_runs = repository_root() / "artifacts" / "test-runs"
        test_runs.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="activation-v2-a5-", dir=test_runs)
        self.sandbox = Path(self.temporary.name)
        self.runtime = self._runtime("runtime")
        self.snapshot = load_strict_json(
            repository_root() / "fixtures/canonical/activation-v2-graph-snapshot-v1.json"
        )
        self.fixture = load_strict_json(
            repository_root() / "fixtures/activation-v2/a5-focus-graph-evaluation-v1.json"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _runtime(self, name: str, *, graph_ui: str = "GENERATED_SNAPSHOT"):
        return initialize_disposable_runtime(
            self.sandbox / name,
            PolicyInputs.fixture_recommended_defaults(graph_ui=graph_ui),
            created_at="2026-07-24T05:00:00Z",
        )

    def test_canonical_view_contract_preserves_provenance_and_no_authority_flags(self) -> None:
        document = load_strict_json(
            repository_root() / "fixtures/canonical/activation-v2-focus-graph-view-v1.json"
        )
        validate_named_document("activation-v2-focus-graph-view-v1", document)
        view = FocusGraphView.from_value(document)
        self.assertEqual(view.to_dict(), document)
        self.assertTrue(view.derived_only)
        self.assertTrue(view.fixture_only)
        self.assertFalse(view.ui_server_started)
        self.assertFalse(view.authority_write)
        self.assertFalse(view.global_write)
        self.assertEqual(view.edges[1].cross_store_provenance.project_object_id, view.focus_node_id)

    def test_compiler_is_deterministic_bounded_and_writes_only_a_derived_snapshot(self) -> None:
        expected = load_strict_json(
            repository_root() / self.fixture["canonical_view_fixture"]
        )
        view = compile_focus_graph_view(self.runtime, self.snapshot, self.fixture["request"])
        self.assertEqual(view.to_dict(), expected)
        self.assertEqual([node.node_id for node in view.nodes], self.fixture["expected_node_ids"])
        self.assertEqual([edge.edge_id for edge in view.edges], self.fixture["expected_edge_ids"])
        self.assertLessEqual(len(view.nodes), view.max_nodes)
        self.assertLessEqual(len(view.edges), view.max_edges)

        replay = compile_focus_graph_view(self.runtime, self.snapshot, self.fixture["request"])
        self.assertEqual(replay.to_dict(), view.to_dict())
        self.assertEqual(load_focus_graph_view(self.runtime, "v2-fixture", view.view_id).to_dict(), expected)

        second_runtime = self._runtime("second-runtime")
        rebuilt = compile_focus_graph_view(second_runtime, self.snapshot, self.fixture["request"])
        self.assertEqual(canonical_json_bytes(rebuilt.to_dict()), canonical_json_bytes(view.to_dict()))
        self.assertTrue(
            (self.runtime.root / "projects" / "v2-fixture" / "derived" / "a5-focus").is_dir()
        )
        self.assertFalse((self.runtime.root / "projects" / "v2-fixture" / "authority.json").exists())
        self.assertEqual(runtime_health(self.runtime)["authority_store_opened"], False)
        self.assertEqual(runtime_health(self.runtime)["global_mutation"], False)

    def test_non_generated_policy_cross_project_request_and_over_hop_budget_fail_before_write(self) -> None:
        local_web_runtime = self._runtime("local-web-runtime", graph_ui="LOCAL_WEB")
        with self.assertRaises(DerivedGraphError):
            compile_focus_graph_view(local_web_runtime, self.snapshot, self.fixture["request"])
        self.assertFalse((local_web_runtime.root / "projects" / "v2-fixture" / "derived" / "a5-focus").exists())

        cross_project = dict(self.fixture["request"])
        cross_project["project_id"] = "foreign-project"
        with self.assertRaises(DerivedGraphError):
            compile_focus_graph_view(self.runtime, self.snapshot, cross_project)

        too_many_hops = dict(self.fixture["request"])
        too_many_hops["max_hops"] = 3
        with self.assertRaises(DerivedGraphError):
            compile_focus_graph_view(self.runtime, self.snapshot, too_many_hops)
        self.assertFalse((self.runtime.root / "projects" / "v2-fixture" / "derived" / "a5-focus").exists())

    def test_unsafe_snapshot_label_and_rehashed_view_label_fail_closed_without_exposing_content(self) -> None:
        marker = "V2_PROMPT_INJECTION_SENTINEL"
        unsafe_snapshot = json.loads(json.dumps(self.snapshot))
        unsafe_snapshot["nodes"][0]["label"] = marker
        unsafe_snapshot["snapshot_digest"] = activation_v2_logical_digest(unsafe_snapshot, "snapshot_digest")
        with self.assertRaises(ContentPolicyError) as raised:
            compile_focus_graph_view(self.runtime, unsafe_snapshot, self.fixture["request"])
        self.assertNotIn(marker, str(raised.exception))
        self.assertFalse((self.runtime.root / "projects" / "v2-fixture" / "derived" / "a5-focus").exists())

        rehashed_view = load_strict_json(
            repository_root() / "fixtures/canonical/activation-v2-focus-graph-view-v1.json"
        )
        rehashed_view["nodes"][0]["label"] = marker
        rehashed_view["view_digest"] = activation_v2_logical_digest(rehashed_view, "view_digest")
        with self.assertRaises(ContentPolicyError) as raised:
            FocusGraphView.from_value(rehashed_view)
        self.assertNotIn(marker, str(raised.exception))

    def test_related_to_is_excluded_and_cycle_cross_store_and_rehashed_project_tampering_fail_closed(self) -> None:
        related_snapshot = json.loads(json.dumps(self.snapshot))
        related_snapshot["edges"][0]["relation"] = "related_to"
        related_snapshot["snapshot_digest"] = activation_v2_logical_digest(related_snapshot, "snapshot_digest")
        related_request = dict(self.fixture["request"])
        related_request["request_id"] = "focus-graph-request:10000000-0000-4000-8000-000000000502"
        related_view = compile_focus_graph_view(self.runtime, related_snapshot, related_request)
        self.assertEqual([edge.relation for edge in related_view.edges], ["about"])

        cyclic = load_strict_json(
            repository_root() / "fixtures/canonical/activation-v2-focus-graph-view-v1.json"
        )
        cyclic["edges"][0]["relation"] = "part_of"
        reverse = dict(cyclic["edges"][0])
        reverse["edge_id"] = "edge:10000000-0000-4000-8000-000000000520"
        reverse["source_id"], reverse["target_id"] = reverse["target_id"], reverse["source_id"]
        reverse["source_revision"], reverse["target_revision"] = (
            reverse["target_revision"],
            reverse["source_revision"],
        )
        cyclic["edges"].append(reverse)
        cyclic["view_digest"] = activation_v2_logical_digest(cyclic, "view_digest")
        with self.assertRaises(DerivedGraphError):
            FocusGraphView.from_value(cyclic)

        view = compile_focus_graph_view(self.runtime, self.snapshot, self.fixture["request"])
        path = self.runtime.root / "projects" / "v2-fixture" / "derived" / "a5-focus" / (
            view.view_id.removeprefix("focus-graph-view:") + ".json"
        )
        payload = load_strict_json(path)
        payload["project_id"] = "foreign-project"
        payload["view_digest"] = activation_v2_logical_digest(payload, "view_digest")
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o600)
        with self.assertRaises(IntegrityError):
            load_focus_graph_view(self.runtime, "v2-fixture", view.view_id)

    def test_latency_budget_failure_precedes_any_derived_view_write(self) -> None:
        with patch("second_brain.activation_v2_graph.time.monotonic", side_effect=(10.0, 11.001)):
            with self.assertRaises(DerivedGraphError):
                compile_focus_graph_view(self.runtime, self.snapshot, self.fixture["request"])
        self.assertFalse((self.runtime.root / "projects" / "v2-fixture" / "derived" / "a5-focus").exists())


if __name__ == "__main__":
    unittest.main()
