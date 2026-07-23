from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
import time
import unittest

from second_brain.admission import AdmissionDecision, AdmissionEngine, AdmissionFeatures, AuditTrail, Lane
from second_brain.errors import SemanticValidationError
from second_brain.graph_runtime import (
    ArtifactReference,
    GraphPolicyError,
    GraphRuntime,
    GraphState,
    GraphValidationError,
    NodeResult,
    NodeState,
    ProposedChange,
    ResultStatus,
    compact_artifact,
    load_work_graph_manifest,
    validate_work_graph_manifest,
)
from second_brain.jsonio import load_strict_json
from second_brain.workspace import repository_root, temporary_store


class WorkGraphM7Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = repository_root()
        self.raw = load_strict_json(self.root / "fixtures/canonical/work-graph-v1.json")

    def _manifest(self, mutate=None):
        candidate = deepcopy(self.raw)
        if mutate is not None:
            mutate(candidate)
        return validate_work_graph_manifest(candidate)

    def _issued_admission(self, manifest, audit: AuditTrail):
        engine = AdmissionEngine(audit_trail=audit)
        if manifest.lane is Lane.DEEP:
            features = AdmissionFeatures(high_risk=True)
        else:
            features = AdmissionFeatures(parallel_branches=2, multi_artifact=True)
        return engine.admit(features, task_id=manifest.task_id)

    @staticmethod
    def _result(manifest, context, *, status=ResultStatus.SUCCEEDED):
        node = manifest.node_by_id[context.node_id]
        if status is not ResultStatus.SUCCEEDED:
            return NodeResult(
                node_id=context.node_id,
                attempt=context.attempt,
                status=status,
                reroute_evidence_digest="e" * 64 if status is ResultStatus.REROUTE_REQUIRED else None,
            )
        changes = ()
        if context.node_id == "collect":
            changes = (ProposedChange("fixtures/m7/proposals/collect.json", "b" * 64),)
        return NodeResult.succeeded(
            context.node_id,
            context.attempt,
            artifacts=(ArtifactReference(node.expected_artifacts[0], "a" * 64),),
            changes=changes,
            checks=node.acceptance,
            token_count=3,
            cost_microunits=7,
        )

    def _runtime(self, manifest, audit: AuditTrail | None = None):
        audit = audit or AuditTrail()
        return GraphRuntime(manifest, audit_trail=audit), audit

    def test_canonical_fixture_has_a_closed_schema_and_semantic_contract(self) -> None:
        manifest = self._manifest()
        self.assertEqual(manifest.version, 1)
        self.assertEqual(manifest.final_node, "integrate")
        self.assertEqual(manifest.node_by_id["integrate"].profile, "tera-max")
        self.assertEqual(manifest.node_by_id["review"].profile, "gpt55-xhigh")

    def test_invalid_shape_and_semantics_fail_before_runner_can_start(self) -> None:
        cases = {
            "unknown": lambda value: value.__setitem__("unexpected", True),
            "direct": lambda value: value.__setitem__("lane", "DIRECT"),
            "cycle": lambda value: value["nodes"][0].__setitem__("depends_on", ["integrate"]),
            "missing-dependency": lambda value: value["nodes"][0].__setitem__("depends_on", ["missing"]),
            "boolean-timeout": lambda value: value["nodes"][0].__setitem__("timeout_seconds", True),
            "scope-overlap": lambda value: value["nodes"][1].__setitem__(
                "write_scope", ["fixtures/m7/proposals/"]
            )
            or value["nodes"][1].__setitem__("permission_class", "workspace-write"),
        }
        starts = 0

        def runner(*_args):
            nonlocal starts
            starts += 1
            raise AssertionError("invalid graph invoked a runner")

        for name, mutate in cases.items():
            with self.subTest(name=name):
                candidate = deepcopy(self.raw)
                mutate(candidate)
                with self.assertRaises((GraphValidationError, SemanticValidationError)):
                    GraphRuntime(candidate, audit_trail=AuditTrail())
                self.assertEqual(starts, 0)

    def test_m5_issued_admission_is_required_and_caller_clone_is_not_authority(self) -> None:
        manifest = self._manifest()
        audit = AuditTrail()
        issued = self._issued_admission(manifest, audit)
        cloned = AdmissionDecision.from_value(issued.to_dict())
        starts = 0

        def runner(context, cancellation):
            nonlocal starts
            starts += 1
            return self._result(manifest, context)

        with self.assertRaises(GraphPolicyError):
            GraphRuntime(manifest, audit_trail=audit).run(cloned, runner)
        self.assertEqual(starts, 0)

        with self.assertRaises(Exception):
            replace(issued, task_id="task:00000000-0000-4000-8000-000000000099")
        self.assertEqual(starts, 0)

    def test_audit_trail_subclass_or_shadowed_method_cannot_spoof_issuance(self) -> None:
        manifest = self._manifest()
        issuing_audit = AuditTrail()
        issued = self._issued_admission(manifest, issuing_audit)
        starts = 0

        def runner(context, cancellation):
            nonlocal starts
            starts += 1
            return self._result(manifest, context)

        class HostileAuditTrail(AuditTrail):
            def has_admission(self, _decision):
                return True

        with self.assertRaises(GraphPolicyError):
            GraphRuntime(manifest, audit_trail=HostileAuditTrail())

        shadowed_audit = AuditTrail()
        shadowed_audit.has_admission = lambda _decision: True
        runtime = GraphRuntime(manifest, audit_trail=shadowed_audit)
        with self.assertRaises(GraphPolicyError):
            runtime.run(issued, runner)
        self.assertEqual(starts, 0)

    def test_manifest_loader_accepts_only_contained_relative_paths(self) -> None:
        manifest = load_work_graph_manifest("fixtures/canonical/work-graph-v1.json")
        self.assertEqual(manifest.final_node, "integrate")

        with self.assertRaises(GraphValidationError):
            load_work_graph_manifest(self.root / "fixtures/canonical/work-graph-v1.json")
        with self.assertRaises(GraphValidationError):
            load_work_graph_manifest("../second-brain/fixtures/canonical/work-graph-v1.json")
        with temporary_store() as store:
            escaping_link = store / "m7-manifest-escape.json"
            escaping_link.symlink_to(self.root.parent / "outside-m7-manifest.json")
            with self.assertRaises(GraphValidationError):
                load_work_graph_manifest(escaping_link.relative_to(self.root))

    def test_dependency_ordering_join_and_proposal_only_receipt(self) -> None:
        manifest = self._manifest()
        runtime, audit = self._runtime(manifest)
        decision = self._issued_admission(manifest, audit)
        seen_dependencies: dict[str, tuple[str, ...]] = {}

        def runner(context, cancellation):
            self.assertFalse(cancellation.cancelled)
            seen_dependencies[context.node_id] = tuple(item.node_id for item in context.dependency_results)
            return self._result(manifest, context)

        receipt = runtime.run(decision, runner)
        self.assertIs(receipt.state, GraphState.SUCCEEDED)
        self.assertEqual({item.state for item in receipt.nodes}, {NodeState.SUCCEEDED})
        self.assertEqual(seen_dependencies["review"], ("collect", "analyze"))
        self.assertEqual(seen_dependencies["integrate"], ("collect", "analyze", "review"))
        self.assertTrue(receipt.proposal_only)
        self.assertFalse(receipt.material_side_effect_allowed)
        self.assertIsNotNone(receipt.final_result)

    def test_failure_blocks_descendants_without_partial_integration(self) -> None:
        manifest = self._manifest()
        runtime, audit = self._runtime(manifest)
        decision = self._issued_admission(manifest, audit)
        called: list[str] = []

        def runner(context, cancellation):
            called.append(context.node_id)
            if context.node_id == "collect":
                return NodeResult.failed(context.node_id, context.attempt)
            return self._result(manifest, context)

        receipt = runtime.run(decision, runner)
        states = {item.node_id: item.state for item in receipt.nodes}
        self.assertIs(receipt.state, GraphState.BLOCKED)
        self.assertIs(states["collect"], NodeState.FAILED)
        self.assertIs(states["review"], NodeState.BLOCKED)
        self.assertIs(states["integrate"], NodeState.BLOCKED)
        self.assertNotIn("integrate", called)
        self.assertIsNone(receipt.final_result)

    def test_successful_nodes_must_return_declared_evidence_through_transitive_join(self) -> None:
        raw = deepcopy(self.raw)
        next(node for node in raw["nodes"] if node["id"] == "integrate")["depends_on"] = ["review"]
        manifest = validate_work_graph_manifest(raw)
        runtime, audit = self._runtime(manifest)
        decision = self._issued_admission(manifest, audit)

        def runner(context, cancellation):
            if context.node_id in {"collect", "analyze"}:
                return NodeResult.succeeded(context.node_id, context.attempt)
            return self._result(manifest, context)

        receipt = runtime.run(decision, runner)
        states = {item.node_id: item.state for item in receipt.nodes}
        self.assertIs(receipt.state, GraphState.BLOCKED)
        self.assertIs(states["collect"], NodeState.FAILED)
        self.assertIs(states["analyze"], NodeState.FAILED)
        self.assertIs(states["review"], NodeState.BLOCKED)
        self.assertIs(states["integrate"], NodeState.BLOCKED)

    def test_result_subclass_cannot_publish_a_success(self) -> None:
        manifest = self._manifest()
        runtime, audit = self._runtime(manifest)
        decision = self._issued_admission(manifest, audit)

        class ForgedResult(NodeResult):
            pass

        def runner(context, cancellation):
            if context.node_id == "collect":
                node = manifest.node_by_id[context.node_id]
                return ForgedResult.succeeded(
                    context.node_id,
                    context.attempt,
                    artifacts=(ArtifactReference(node.expected_artifacts[0], "a" * 64),),
                    changes=(ProposedChange("fixtures/m7/proposals/collect.json", "b" * 64),),
                    checks=node.acceptance,
                )
            return self._result(manifest, context)

        receipt = runtime.run(decision, runner)
        states = {item.node_id: item.state for item in receipt.nodes}
        self.assertIs(receipt.state, GraphState.BLOCKED)
        self.assertIs(states["collect"], NodeState.FAILED)
        self.assertIs(states["integrate"], NodeState.BLOCKED)

    def test_worker_context_cannot_mutate_runtime_owned_dependency_references(self) -> None:
        manifest = self._manifest()
        runtime, audit = self._runtime(manifest)
        decision = self._issued_admission(manifest, audit)

        def runner(context, cancellation):
            if context.node_id == "review":
                object.__setattr__(context.dependency_results[0], "checks", ())
            return self._result(manifest, context)

        receipt = runtime.run(decision, runner)
        self.assertIs(receipt.state, GraphState.SUCCEEDED)
        self.assertEqual({item.state for item in receipt.nodes}, {NodeState.SUCCEEDED})

    def test_transient_retry_and_evidence_bound_reroute_consume_the_attempt_budget(self) -> None:
        manifest = self._manifest()
        runtime, audit = self._runtime(manifest)
        decision = self._issued_admission(manifest, audit)
        attempts: list[tuple[str, int, str]] = []

        def runner(context, cancellation):
            attempts.append((context.node_id, context.attempt, context.profile_alias))
            if context.node_id == "collect" and context.attempt == 1:
                return NodeResult.transient_failure(context.node_id, context.attempt)
            return self._result(manifest, context)

        receipt = runtime.run(decision, runner)
        self.assertIs(receipt.state, GraphState.SUCCEEDED)
        self.assertEqual([item[1] for item in attempts if item[0] == "collect"], [1, 2])
        self.assertIn("node_retry_scheduled", [event.event_type for event in receipt.events])

        runtime, audit = self._runtime(manifest)
        decision = self._issued_admission(manifest, audit)
        profiles: list[str] = []

        def rerouting_runner(context, cancellation):
            if context.node_id == "collect":
                profiles.append(context.profile_alias)
                if context.attempt == 1:
                    return NodeResult.reroute_required(context.node_id, context.attempt, evidence_digest="d" * 64)
            return self._result(manifest, context)

        receipt = runtime.run(decision, rerouting_runner)
        self.assertIs(receipt.state, GraphState.SUCCEEDED)
        self.assertEqual(profiles, ["sol-xhigh", "sol-max"])
        self.assertIn("node_rerouted", [event.event_type for event in receipt.events])

    def test_permission_failure_cannot_be_mislabeled_as_retryable_transient(self) -> None:
        with self.assertRaises(GraphValidationError):
            NodeResult.transient_failure("collect", 1, unresolved=("PERMISSION_DENIED",))

        manifest = self._manifest()
        runtime, audit = self._runtime(manifest)
        decision = self._issued_admission(manifest, audit)
        attempts: list[tuple[str, int]] = []

        def runner(context, cancellation):
            attempts.append((context.node_id, context.attempt))
            if context.node_id == "collect":
                return NodeResult(
                    node_id=context.node_id,
                    attempt=context.attempt,
                    status=ResultStatus.TRANSIENT_FAILURE,
                    unresolved=("PERMISSION_DENIED",),
                )
            return self._result(manifest, context)

        receipt = runtime.run(decision, runner)
        states = {item.node_id: item.state for item in receipt.nodes}
        reasons = {item.node_id: item.reason_code for item in receipt.nodes}
        self.assertIs(receipt.state, GraphState.BLOCKED)
        self.assertIs(states["collect"], NodeState.FAILED)
        self.assertEqual(reasons["collect"], "RUNNER_EXCEPTION")
        self.assertEqual([attempt for node_id, attempt in attempts if node_id == "collect"], [1])
        self.assertNotIn("node_retry_scheduled", [event.event_type for event in receipt.events])

    def test_route_mismatch_quarantines_before_any_runner_starts(self) -> None:
        manifest = self._manifest()
        runtime, audit = self._runtime(manifest)
        decision = self._issued_admission(manifest, audit)
        starts = 0

        def runner(context, cancellation):
            nonlocal starts
            starts += 1
            return self._result(manifest, context)

        receipt = runtime.run(decision, runner, route_observer=lambda _intent, _serialized: ())
        self.assertEqual(starts, 0)
        self.assertIs(receipt.state, GraphState.BLOCKED)
        self.assertTrue(any(item.state is NodeState.QUARANTINED_ROUTE for item in receipt.nodes))
        self.assertFalse(receipt.material_side_effect_allowed)

    def test_required_event_sink_failure_blocks_before_runner_dispatch(self) -> None:
        manifest = self._manifest()
        audit = AuditTrail()
        decision = self._issued_admission(manifest, audit)
        starts = 0

        def runner(context, cancellation):
            nonlocal starts
            starts += 1
            return self._result(manifest, context)

        runtime = GraphRuntime(
            manifest,
            audit_trail=audit,
            event_sink=lambda _event: (_ for _ in ()).throw(RuntimeError("sink unavailable")),
        )
        with self.assertRaises(GraphPolicyError):
            runtime.run(decision, runner)
        self.assertEqual(starts, 0)
        self.assertIs(runtime.state, GraphState.BLOCKED)

    def test_timeout_quarantines_late_success_and_blocks_its_descendants(self) -> None:
        manifest = self._manifest()

        class Monotonic:
            value = 0.0

            def __call__(self):
                return self.value

        monotonic = Monotonic()
        runtime, audit = self._runtime(manifest)
        runtime = GraphRuntime(manifest, audit_trail=audit, monotonic_clock=monotonic)
        decision = self._issued_admission(manifest, audit)

        def runner(context, cancellation):
            if context.node_id == "analyze":
                monotonic.value = 61.0
                while not cancellation.cancelled:
                    time.sleep(0.001)
            return self._result(manifest, context)

        receipt = runtime.run(decision, runner, serial_fallback=True)
        states = {item.node_id: item.state for item in receipt.nodes}
        self.assertIs(receipt.state, GraphState.BLOCKED)
        self.assertIn(NodeState.TIMED_OUT, states.values())
        self.assertIs(states["integrate"], NodeState.BLOCKED)

    def test_serial_fallback_preserves_the_graph_and_never_exceeds_one_worker(self) -> None:
        manifest = self._manifest()
        runtime, audit = self._runtime(manifest)
        decision = self._issued_admission(manifest, audit)
        active = 0
        maximum = 0

        def runner(context, cancellation):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            time.sleep(0.005)
            active -= 1
            return self._result(manifest, context)

        receipt = runtime.run(decision, runner, serial_fallback=True)
        self.assertIs(receipt.state, GraphState.SUCCEEDED)
        self.assertEqual(receipt.max_active_workers, 1)
        self.assertEqual(maximum, 1)
        self.assertTrue(receipt.serial_fallback)

    def test_cancel_wins_over_a_late_success_and_stops_pending_nodes(self) -> None:
        manifest = self._manifest()
        runtime, audit = self._runtime(manifest)
        decision = self._issued_admission(manifest, audit)
        started = Event()
        completed: list[object] = []
        exposed_events: list[str] = []

        def runner(context, cancellation):
            started.set()
            while not cancellation.cancelled:
                time.sleep(0.001)
            for name in (
                "_run_cancel",
                "_attempt_cancel",
                "_CancellationToken__cancelled",
                "_CancellationToken__wait",
            ):
                event = getattr(cancellation, name, None)
                if event is not None:
                    exposed_events.append(name)
                    event.clear()
            for name in dir(cancellation):
                if isinstance(getattr(cancellation, name), Event):
                    exposed_events.append(name)
            return self._result(manifest, context)

        thread = Thread(target=lambda: completed.append(runtime.run(decision, runner)), daemon=True)
        thread.start()
        self.assertTrue(started.wait(1))
        runtime.cancel()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        receipt = completed[0]
        self.assertIs(receipt.state, GraphState.CANCELLED)
        self.assertTrue(all(item.state is NodeState.CANCELLED for item in receipt.nodes))
        self.assertIsNone(receipt.final_result)
        self.assertIn("graph_cancel_requested", [event.event_type for event in receipt.events])
        self.assertEqual(exposed_events, [])

    def test_worker_child_thread_cannot_start_a_nested_graph(self) -> None:
        manifest = self._manifest()
        runtime, audit = self._runtime(manifest)
        decision = self._issued_admission(manifest, audit)
        nested_errors: list[Exception] = []
        nested_done = Event()

        def runner(context, cancellation):
            if context.node_id == "collect":
                nested_runtime = GraphRuntime(manifest, audit_trail=audit)

                def invoke_nested_graph() -> None:
                    try:
                        nested_runtime.run(
                            decision,
                            lambda nested_context, nested_cancellation: self._result(manifest, nested_context),
                        )
                    except Exception as error:
                        nested_errors.append(error)
                    finally:
                        nested_done.set()

                child = Thread(target=invoke_nested_graph, daemon=True)
                child.start()
                child.join(1)
                self.assertFalse(child.is_alive())
            return self._result(manifest, context)

        receipt = runtime.run(decision, runner)
        self.assertIs(receipt.state, GraphState.SUCCEEDED)
        self.assertTrue(nested_done.is_set())
        self.assertEqual(len(nested_errors), 1)
        self.assertIsInstance(nested_errors[0], GraphPolicyError)

    def test_result_scope_and_redaction_boundaries_fail_closed(self) -> None:
        manifest = self._manifest()
        runtime, audit = self._runtime(manifest)
        decision = self._issued_admission(manifest, audit)
        marker = "M7_REDACTION_MARKER"

        def runner(context, cancellation):
            if context.node_id == "collect":
                return NodeResult.succeeded(
                    context.node_id,
                    context.attempt,
                    artifacts=(ArtifactReference("artifact:collect", "a" * 64),),
                    changes=(ProposedChange("outside.txt", "b" * 64),),
                    checks=context.acceptance,
                )
            return self._result(manifest, context)

        receipt = runtime.run(decision, runner)
        self.assertIs({item.node_id: item.state for item in receipt.nodes}["collect"], NodeState.FAILED)
        encoded = str(receipt.to_dict())
        self.assertNotIn(marker, encoded)
        with self.assertRaises(GraphPolicyError):
            compact_artifact("token=" + marker, artifact_id="artifact:compact")
        compacted = compact_artifact("safe local diagnostic", artifact_id="artifact:compact")
        self.assertNotIn("safe local diagnostic", str(compacted.to_dict()))

    def test_file_scope_does_not_authorize_a_descendant_path(self) -> None:
        manifest = self._manifest()
        runtime, audit = self._runtime(manifest)
        decision = self._issued_admission(manifest, audit)

        def runner(context, cancellation):
            if context.node_id == "collect":
                return NodeResult.succeeded(
                    context.node_id,
                    context.attempt,
                    artifacts=(ArtifactReference("artifact:collect", "a" * 64),),
                    changes=(
                        ProposedChange(
                            "fixtures/m7/proposals/collect.json/escaped.json",
                            "b" * 64,
                        ),
                    ),
                    checks=context.acceptance,
                )
            return self._result(manifest, context)

        receipt = runtime.run(decision, runner)
        states = {item.node_id: item.state for item in receipt.nodes}
        self.assertIs(states["collect"], NodeState.FAILED)
        self.assertIs(states["integrate"], NodeState.BLOCKED)

    def test_graph_lane_requires_real_independent_branches_for_automatic_admission(self) -> None:
        raw = deepcopy(self.raw)
        raw["lane"] = "GRAPH"
        manifest = validate_work_graph_manifest(raw)
        runtime, audit = self._runtime(manifest)
        decision = self._issued_admission(manifest, audit)
        receipt = runtime.run(decision, lambda context, cancellation: self._result(manifest, context))
        self.assertIs(receipt.state, GraphState.SUCCEEDED)


if __name__ == "__main__":
    unittest.main()
