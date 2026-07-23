from __future__ import annotations

from copy import deepcopy
import unittest

from second_brain.admission import AdmissionEngine, AdmissionFeatures, AuditTrail
from second_brain.contract_checks import run_contract_checks
from second_brain.graph_runtime import ArtifactReference, GraphRuntime, GraphState, NodeResult, ProposedChange, validate_work_graph_manifest
from second_brain.jsonio import load_strict_json
from second_brain.routing import side_effect_allowed
from second_brain.workspace import repository_root


class M7IntegrationTests(unittest.TestCase):
    def test_contract_registry_runtime_and_m6_denial_boundary_join(self) -> None:
        root = repository_root()
        manifest = validate_work_graph_manifest(
            load_strict_json(root / "fixtures/canonical/work-graph-v1.json")
        )
        audit = AuditTrail()
        admission = AdmissionEngine(audit_trail=audit).admit(
            AdmissionFeatures(high_risk=True), task_id=manifest.task_id
        )

        def runner(context, cancellation):
            node = manifest.node_by_id[context.node_id]
            changes = ()
            if context.node_id == "collect":
                changes = (ProposedChange("fixtures/m7/proposals/collect.json", "b" * 64),)
            return NodeResult.succeeded(
                context.node_id,
                context.attempt,
                artifacts=(ArtifactReference(node.expected_artifacts[0], "a" * 64),),
                checks=node.acceptance,
                changes=changes,
            )

        receipt = GraphRuntime(manifest, audit_trail=audit).run(admission, runner)
        self.assertIs(receipt.state, GraphState.SUCCEEDED)
        self.assertTrue(receipt.proposal_only)
        self.assertFalse(receipt.material_side_effect_allowed)
        self.assertFalse(side_effect_allowed(None, manifest.lane))
        self.assertIn("schema:work-graph-v1", run_contract_checks())

    def test_direct_cannot_be_reinterpreted_as_serial_graph(self) -> None:
        raw = load_strict_json(repository_root() / "fixtures/canonical/work-graph-v1.json")
        raw = deepcopy(raw)
        raw["lane"] = "DIRECT"
        with self.assertRaises(Exception):
            validate_work_graph_manifest(raw)


if __name__ == "__main__":
    unittest.main()
