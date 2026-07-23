from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import unittest

from second_brain.evaluation import GateState, generate_m8_local_evidence, load_serialized_receipt, verify_m8_local_evidence
from second_brain.routing import side_effect_allowed
from second_brain.workspace import repository_root


class M8IntegrationTests(unittest.TestCase):
    def test_typed_local_evidence_is_redacted_bound_and_not_promoted(self) -> None:
        artifacts = generate_m8_local_evidence()
        self.assertEqual(
            set(artifacts),
            {"baseline", "evaluation", "m4", "performance", "canary", "rollback", "release", "verification"},
        )
        root = repository_root() / "artifacts"
        prohibited = ("prompt", "cookie", "credential", "traceback", "http://", "https://")
        for artifact in artifacts.values():
            payload = (root / artifact.name).read_text(encoding="utf-8")
            self.assertNotIn('"promoted"', payload.lower())
            self.assertFalse(any(marker in payload.lower() for marker in prohibited))
            parsed = json.loads(payload)
            self.assertEqual(parsed["output_digest"], artifact.output_digest)
            self.assertEqual(load_serialized_receipt(artifact.name)["schema_version"], 1)

        verification = verify_m8_local_evidence()
        self.assertIs(verification.state, GateState.PASS)
        self.assertEqual(verification.artifact_count, 8)
        release = load_serialized_receipt("m8-release-report.json")
        self.assertEqual(release["release_status"], "BLOCKED")
        self.assertEqual(release["promotion_status"], "M9_EVIDENCE_REQUIRED")
        self.assertEqual(release["local_component_status"], "PASS")
        self.assertFalse(release["live_attested"])
        self.assertEqual(release["boundary_assertions"]["global_activation"], "disabled")
        self.assertGreaterEqual(len(release["deferred_live_gates"]), 7)

    def test_m8_does_not_mutate_m6_staging_or_authorize_actions(self) -> None:
        root = repository_root()
        staging = root / "dist" / "global" / "m6"
        before = self._tree_digest(staging)
        generate_m8_local_evidence()
        after = self._tree_digest(staging)
        self.assertEqual(before, after)
        self.assertFalse(side_effect_allowed(None, "DEEP"))
        self.assertFalse(side_effect_allowed(None, "GRAPH"))

    def test_cli_reports_blocked_release_and_nonzero_exit(self) -> None:
        root = repository_root()
        script = root / "scripts" / "run_m8_evaluation.py"
        spec = importlib.util.spec_from_file_location("run_m8_evaluation_test", script)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = module.main()
        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertEqual(payload["verification"]["state"], "PASS")

    def test_evidence_generators_offer_read_only_help(self) -> None:
        root = repository_root()
        for module_name, filename in (
            ("run_m8_evaluation_help_test", "run_m8_evaluation.py"),
            ("generate_m8_m9_readiness_help_test", "generate_m8_m9_readiness.py"),
        ):
            spec = importlib.util.spec_from_file_location(module_name, root / "scripts" / filename)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            output = io.StringIO()
            with self.assertRaises(SystemExit) as raised, redirect_stdout(output):
                module.main(("--help",))
            self.assertEqual(raised.exception.code, 0)
            self.assertIn("usage:", output.getvalue())

    @staticmethod
    def _tree_digest(path: Path) -> str:
        if not path.exists():
            return "missing"
        records = []
        for item in sorted(path.rglob("*")):
            relative = item.relative_to(path).as_posix()
            if item.is_symlink():
                records.append((relative, "symlink", item.readlink().as_posix()))
            elif item.is_file():
                records.append((relative, "file", hashlib.sha256(item.read_bytes()).hexdigest()))
        return hashlib.sha256(repr(records).encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()
