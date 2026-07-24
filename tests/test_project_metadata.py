from __future__ import annotations

import unittest

from second_brain.jsonio import load_strict_json
from second_brain.workspace import repository_root


class ProjectMetadataTests(unittest.TestCase):
    def test_project_declares_no_runtime_dependencies_for_m0(self) -> None:
        root = repository_root()
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("dependencies = []", pyproject)

    def test_schema_registry_is_machine_readable_json(self) -> None:
        root = repository_root()
        registry = load_strict_json(root / "schemas/schema-registry.json")
        names = [entry["name"] for entry in registry["schemas"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertGreaterEqual(len(names), 7)
        self.assertIn(
            "evaluation-case-v1",
            names,
        )
        self.assertTrue(
            {
                "activation-v2-a8-promotion-corpus-v1",
                "activation-v2-a8-promotion-packet-v1",
                "activation-v2-a8-promotion-state-v1",
                "activation-v2-a8-promotion-receipt-v1",
                "activation-v2-a8-pending-operation-v1",
            }.issubset(names)
        )


if __name__ == "__main__":
    unittest.main()
