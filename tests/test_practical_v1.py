from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from second_brain.canonical import sha256_hex
from second_brain.clock import DeterministicClock
from second_brain.errors import StorageError
from second_brain.practical_v1 import (
    DIRECT_NO_MEMORY,
    bridge_status,
    global_knowledge_read,
    project_recovery_read,
    propose_durable_write,
    validate_project_root,
    validate_runtime_root,
    validate_router_log_plan,
    verify_operator_router_log,
    verify_operator_router_log_files,
)
from second_brain.recovery import ProjectRecoveryKernel
from second_brain.storage import Store
from second_brain.workspace import repository_root, temporary_store

from tests.m1_storage_helpers import (
    OBJECT_A,
    create_mutation as global_create,
    make_object,
    transaction as global_transaction,
)
from tests.m2_recovery_helpers import (
    PROJECT_ID,
    authority_digest,
    create_mutation as project_create,
    make_project_object,
    make_reference,
    transaction as project_transaction,
)


FIXTURE_ROOT = repository_root() / "fixtures" / "practical-v1"


def initialize_git_worktree(root: Path) -> Path:
    root.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(root)],
        check=True,
        capture_output=True,
        text=True,
    )
    return root.resolve()


class PracticalRouterEvidenceTests(unittest.TestCase):
    def test_operator_trusted_fixture_passes_without_provider_attestation_claim(self) -> None:
        report = verify_operator_router_log_files(
            evidence_root=FIXTURE_ROOT,
            plan_path="router-plan.json",
            log_path="router-outbound.jsonl",
        )

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["matched_count"], 3)
        self.assertFalse(report["provider_attestation"])
        self.assertFalse(report["strict_upstream_attestation_satisfied"])
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn("corr:practical-v1-0001", serialized)
        self.assertIn("does-not-prove-remote-provider-internal-model-or-effort", serialized)

    def test_normalized_or_outbound_effort_drift_is_visible(self) -> None:
        plan = json.loads((FIXTURE_ROOT / "router-plan.json").read_text(encoding="utf-8"))
        records = [json.loads(line) for line in (FIXTURE_ROOT / "router-outbound.jsonl").read_text().splitlines()]
        records[0]["normalized_effort"] = "xhigh"
        records[1]["outbound_effort"] = "xhigh"

        report = verify_operator_router_log(plan, records)

        self.assertEqual(report["status"], "FAIL")
        self.assertIn("NORMALIZED_EFFORT_MISMATCH", report["reason_codes"])
        self.assertIn("OUTBOUND_EFFORT_MISMATCH", report["reason_codes"])
        self.assertEqual(report["matched_count"], 1)

    def test_plan_rejects_extra_fields_and_profile_effort_drift(self) -> None:
        plan = json.loads((FIXTURE_ROOT / "router-plan.json").read_text(encoding="utf-8"))
        plan["raw_prompt"] = "must never enter the evidence contract"
        with self.assertRaises(StorageError):
            validate_router_log_plan(plan)

        drifted = json.loads((FIXTURE_ROOT / "router-plan.json").read_text(encoding="utf-8"))
        drifted["expectations"][0]["requested_effort"] = "xhigh"
        with self.assertRaises(StorageError):
            validate_router_log_plan(drifted)


class PracticalBridgeTests(unittest.TestCase):
    def test_project_root_rejects_broad_filesystem_boundaries(self) -> None:
        with self.assertRaises(StorageError):
            validate_project_root(Path(Path.cwd().anchor))

    def test_project_root_requires_an_exact_git_worktree_root(self) -> None:
        with temporary_store() as temporary:
            non_git = temporary / "non-git"
            non_git.mkdir()
            parent = temporary / "projects"
            parent.mkdir()
            project = initialize_git_worktree(parent / "selected-project")

            with self.assertRaises(StorageError):
                validate_project_root(non_git)
            with self.assertRaises(StorageError):
                validate_project_root(parent)

            self.assertEqual(validate_project_root(project), project)

            (project / "README.md").write_text("linked worktree fixture\n", encoding="utf-8")
            for command in (
                ["git", "-C", str(project), "config", "user.email", "tests@example.invalid"],
                ["git", "-C", str(project), "config", "user.name", "Practical V1 Tests"],
                ["git", "-C", str(project), "add", "README.md"],
                ["git", "-C", str(project), "commit", "--quiet", "-m", "fixture"],
            ):
                subprocess.run(command, check=True, capture_output=True, text=True)
            linked = temporary / "linked-project"
            subprocess.run(
                ["git", "-C", str(project), "worktree", "add", "--quiet", "--detach", str(linked)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(validate_project_root(linked), linked.resolve())

    def test_supplied_lexical_symlinks_are_rejected(self) -> None:
        with temporary_store() as temporary:
            runtime = temporary / "practical-runtime"
            runtime.mkdir(mode=0o700)
            project = initialize_git_worktree(temporary / "selected-project")
            runtime_link = temporary / "runtime-link"
            project_link = temporary / "project-link"
            store_link = runtime / "store-link"
            evidence_link = temporary / "evidence-link"
            try:
                runtime_link.symlink_to(runtime, target_is_directory=True)
                project_link.symlink_to(project, target_is_directory=True)
                store_link.symlink_to(runtime, target_is_directory=True)
                evidence_link.symlink_to(FIXTURE_ROOT, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symlink fixtures are unavailable: {error}")

            with self.assertRaises(StorageError):
                validate_runtime_root(runtime_link)
            with self.assertRaises(StorageError):
                validate_project_root(project_link)
            with patch("second_brain.practical_v1.Store.open") as open_store:
                with self.assertRaises(StorageError):
                    bridge_status(runtime_root=runtime, global_store="store-link/global")
            open_store.assert_not_called()
            with self.assertRaises(StorageError):
                verify_operator_router_log_files(
                    evidence_root=evidence_link,
                    plan_path="router-plan.json",
                    log_path="router-outbound.jsonl",
                )

    def test_direct_is_zero_memory_before_any_root_or_store_access(self) -> None:
        with patch("second_brain.practical_v1.Store.open") as open_store:
            project = project_recovery_read(
                runtime_root="/definitely/not/a/runtime",
                project_store="stores/project",
                project_id="ignored",
                project_root="/definitely/not/a/project",
                query_text="ignored",
            )
            global_result = global_knowledge_read(
                runtime_root="/definitely/not/a/runtime",
                global_store="stores/global",
                query_text="ignored",
            )
            proposal = propose_durable_write(
                runtime_root="/definitely/not/a/runtime",
                project_store="stores/project",
                project_id="ignored",
                project_object_id="ignored",
                target_kind="claim",
                idempotency_key="ignored",
                reason_code="IGNORED",
                proposal_only=False,
            )

        open_store.assert_not_called()
        self.assertEqual(project["status"], DIRECT_NO_MEMORY)
        self.assertEqual(global_result["status"], DIRECT_NO_MEMORY)
        self.assertEqual(proposal["status"], DIRECT_NO_MEMORY)

    def test_health_global_read_and_project_read_use_only_explicit_stores(self) -> None:
        with temporary_store() as temporary:
            runtime = temporary / "practical-runtime"
            runtime.mkdir(mode=0o700)
            clock = DeterministicClock()

            global_store = Store.initialize(
                runtime / "stores/global",
                "knowledge:global",
                clock=clock,
                authorizer=lambda *args, **kwargs: True,
            )
            global_document = make_object(
                OBJECT_A,
                title="Practical V1 global bridge knowledge",
                body="A bounded global knowledge read fixture.",
            )
            global_store.commit(global_transaction("txn:00000000-0000-4000-8000-000000000091", "pv1-global", [global_create(global_document)]))

            project_root = initialize_git_worktree(temporary / "explicit-project-root")
            reference = project_root / "evidence.txt"
            reference.write_text("Practical V1 project evidence.\n", encoding="utf-8")
            project_store = Store.initialize(
                runtime / "stores/project",
                f"project:{PROJECT_ID}",
                clock=clock,
                authorizer=lambda *args, **kwargs: True,
            )
            project_document = make_project_object(
                f"mem:{PROJECT_ID}:evidence:90000000-0000-4000-8000-000000000001",
                kind="evidence",
                title="Practical V1 explicit project recovery",
                body="The bridge retrieves only the selected project authority.",
                references=[
                    make_reference(
                        91,
                        "evidence.txt",
                        hashlib.sha256(reference.read_bytes()).hexdigest(),
                        facet="practical-v1",
                    )
                ],
            )
            project_store.commit(project_transaction(91, [project_create(project_document)]))

            status = bridge_status(
                runtime_root=runtime,
                project_store="stores/project",
                global_store="stores/global",
            )
            with self.assertRaises(StorageError):
                bridge_status(runtime_root=runtime, project_store="stores/global")
            global_result = global_knowledge_read(
                lane="ASSISTED",
                runtime_root=runtime,
                global_store="stores/global",
                query_text="bounded global knowledge",
            )
            project_result = project_recovery_read(
                lane="ASSISTED",
                runtime_root=runtime,
                project_store="stores/project",
                project_id=PROJECT_ID,
                project_root=project_root,
                query_text="explicit project recovery",
            )

            self.assertEqual(status["status"], "PASS")
            self.assertEqual({item["store_id"] for item in status["stores"]}, {"knowledge:global", f"project:{PROJECT_ID}"})
            self.assertIn(OBJECT_A, {item["id"] for item in global_result["result"]["included"]})
            self.assertIn(project_document["id"], {item["id"] for item in project_result["result"]["included"]})
            project_entry = next(item for item in project_result["result"]["included"] if item["id"] == project_document["id"])
            self.assertEqual(project_entry["freshness"]["aggregate"], "fresh")

    def test_external_git_project_recovery_uses_a_redacted_root_marker(self) -> None:
        with temporary_store() as temporary, TemporaryDirectory(prefix="practical-v1-external-") as external_root:
            runtime = temporary / "practical-runtime"
            runtime.mkdir(mode=0o700)
            external_project = initialize_git_worktree(Path(external_root) / "selected-project")
            reference = external_project / "evidence.txt"
            reference.write_text("External project recovery evidence.\n", encoding="utf-8")
            clock = DeterministicClock()
            store = Store.initialize(
                runtime / "stores/project",
                f"project:{PROJECT_ID}",
                clock=clock,
                authorizer=lambda *args, **kwargs: True,
            )
            document = make_project_object(
                f"mem:{PROJECT_ID}:evidence:90000000-0000-4000-8000-000000000003",
                kind="evidence",
                title="External Practical V1 recovery",
                body="The bridge recovers selected evidence from one external Git project.",
                references=[
                    make_reference(
                        93,
                        "evidence.txt",
                        hashlib.sha256(reference.read_bytes()).hexdigest(),
                        facet="practical-v1",
                    )
                ],
            )
            store.commit(project_transaction(93, [project_create(document)]))

            result = project_recovery_read(
                lane="ASSISTED",
                runtime_root=runtime,
                project_store="stores/project",
                project_id=PROJECT_ID,
                project_root=external_project,
                query_text="external selected evidence",
            )
            self.assertIn(document["id"], {item["id"] for item in result["result"]["included"]})

            kernel = ProjectRecoveryKernel(store, external_project, project_boundary=external_project)
            observation = kernel.observe_freshness(kernel.snapshot(), object_ids=[document["id"]])[0].to_dict()
            expected_marker = "external:" + sha256_hex({"project_root": str(external_project)})
            self.assertEqual(observation["snapshot"]["project_root"], expected_marker)
            self.assertNotIn(str(external_project), json.dumps(observation, sort_keys=True))

    def test_proposal_write_persists_review_metadata_without_authority_mutation(self) -> None:
        with temporary_store() as temporary:
            runtime = temporary / "practical-runtime"
            runtime.mkdir(mode=0o700)
            store = Store.initialize(
                runtime / "stores/project",
                f"project:{PROJECT_ID}",
                authorizer=lambda *args, **kwargs: True,
            )
            document = make_project_object(
                f"mem:{PROJECT_ID}:evidence:90000000-0000-4000-8000-000000000002",
                kind="evidence",
                title="Promotion proposal source",
                body="Synthetic project evidence for a proposal-only bridge test.",
            )
            store.commit(project_transaction(92, [project_create(document)]))
            before = authority_digest(store.root)

            result = propose_durable_write(
                lane="ASSISTED",
                runtime_root=runtime,
                project_store="stores/project",
                project_id=PROJECT_ID,
                project_object_id=document["id"],
                target_kind="claim",
                idempotency_key="practical-v1-proposal-92",
                reason_code="REUSABLE_VERIFIED_EVIDENCE",
                proposal_only=True,
            )

            self.assertEqual(result["status"], "PENDING_REVIEW")
            self.assertFalse(result["authority_store_mutated"])
            self.assertEqual(authority_digest(store.root), before)
            self.assertTrue((runtime / "bridge-proposals/outbox/promotions").is_dir())
            serialized = json.dumps(result, sort_keys=True)
            self.assertNotIn(document["body"], serialized)

    def test_bridge_cli_verifies_only_local_fixture_files(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(repository_root() / "scripts/practical_v1_bridge.py"),
                "verify-router-log",
                "--evidence-root",
                str(FIXTURE_ROOT),
                "--plan",
                "router-plan.json",
                "--log",
                "router-outbound.jsonl",
            ],
            cwd=repository_root(),
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        output = json.loads(completed.stdout)
        self.assertEqual(output["status"], "PASS")
        self.assertFalse(output["provider_attestation"])


if __name__ == "__main__":
    unittest.main()
