"""Executable M0 contract checks shared by the documented local commands."""

from __future__ import annotations

import argparse
from typing import Any

from .activation_v2 import A0_SCHEMA_NAMES, validate_activation_v2_fixture_bundle
from .contracts import (
    canonical_fixture_path,
    load_schema,
    load_schema_registry,
    validate_named_document,
)
from .documentation_checks import run_documentation_checks
from .graph_runtime import validate_work_graph_manifest
from .jsonio import load_strict_json
from .profiles import load_profile_registry


def run_contract_checks() -> list[str]:
    """Return deterministic check labels after validating all canonical fixtures."""

    checks: list[str] = []
    activation_v2_documents: dict[str, dict[str, Any]] = {}
    registry = load_schema_registry()
    for entry in registry["schemas"]:
        name = entry["name"]
        schema = load_schema(name)
        if "$schema" not in schema:
            raise ValueError(f"{name} does not declare a JSON Schema dialect")
        fixture = load_strict_json(canonical_fixture_path(entry["canonical_fixture"]))
        validate_named_document(name, fixture)
        if name in A0_SCHEMA_NAMES:
            if type(fixture) is not dict:
                raise ValueError(f"{name} canonical fixture is not an object")
            activation_v2_documents[name] = fixture
        if name == "work-graph-v1":
            validate_work_graph_manifest(fixture)
        checks.append(f"schema:{name}")

    validate_activation_v2_fixture_bundle(activation_v2_documents)
    checks.append("activation-v2-a0:fixture-bundle")

    load_profile_registry()
    checks.append("profiles:seven-approved-routing-mappings")
    checks.extend(run_documentation_checks())
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Run M0 machine-readable contract checks")
    parser.add_argument("--lint", action="store_true", help="emit lint-oriented check labels")
    args = parser.parse_args()
    for check in run_contract_checks():
        prefix = "lint" if args.lint else "ok"
        print(f"{prefix}: {check}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
