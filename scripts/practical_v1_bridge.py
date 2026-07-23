#!/usr/bin/env python3
"""Run the bounded project-local Practical V1 bridge."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from second_brain.errors import ContractError, StorageError  # noqa: E402
from second_brain.practical_v1 import (  # noqa: E402
    bridge_status,
    global_knowledge_read,
    practical_v1_source_tree_digest,
    project_recovery_read,
    propose_durable_write,
    verify_operator_router_log_files,
)


def _add_runtime(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runtime-root", required=True, type=Path)


def _add_lane(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--lane", default="DIRECT", choices=("DIRECT", "ASSISTED", "GRAPH", "DEEP"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    for name in ("health", "status"):
        status = commands.add_parser(name, help="Read bounded store health and snapshot metadata")
        _add_runtime(status)
        status.add_argument("--project-store")
        status.add_argument("--global-store")

    project = commands.add_parser("project-recovery-read", help="Read one explicitly selected project authority")
    _add_lane(project)
    _add_runtime(project)
    project.add_argument("--project-store", required=True)
    project.add_argument("--project-id", required=True)
    project.add_argument("--project-root", required=True, type=Path)
    project.add_argument("--query", required=True)

    global_read = commands.add_parser("global-knowledge-read", help="Read the explicit global knowledge authority")
    _add_lane(global_read)
    _add_runtime(global_read)
    global_read.add_argument("--global-store", required=True)
    global_read.add_argument("--query", required=True)

    proposal = commands.add_parser("propose-write", help="Persist only a pending project-to-global proposal")
    _add_lane(proposal)
    _add_runtime(proposal)
    proposal.add_argument("--project-store", required=True)
    proposal.add_argument("--project-id", required=True)
    proposal.add_argument("--project-object-id", required=True)
    proposal.add_argument("--target-kind", required=True, choices=("source", "entity", "concept", "claim", "synthesis"))
    proposal.add_argument("--idempotency-key", required=True)
    proposal.add_argument("--reason-code", required=True)
    proposal.add_argument("--proposal-only", action="store_true")

    route = commands.add_parser("verify-router-log", help="Verify operator-trusted structured 9router outbound logs")
    route.add_argument("--evidence-root", required=True, type=Path)
    route.add_argument("--plan", required=True)
    route.add_argument("--log", required=True)

    commands.add_parser("source-binding", help="Report the deterministic local runtime source binding")
    return parser


def dispatch(arguments: argparse.Namespace) -> dict[str, object]:
    if arguments.command in {"health", "status"}:
        return bridge_status(
            runtime_root=arguments.runtime_root,
            project_store=arguments.project_store,
            global_store=arguments.global_store,
        )
    if arguments.command == "project-recovery-read":
        return project_recovery_read(
            lane=arguments.lane,
            runtime_root=arguments.runtime_root,
            project_store=arguments.project_store,
            project_id=arguments.project_id,
            project_root=arguments.project_root,
            query_text=arguments.query,
        )
    if arguments.command == "global-knowledge-read":
        return global_knowledge_read(
            lane=arguments.lane,
            runtime_root=arguments.runtime_root,
            global_store=arguments.global_store,
            query_text=arguments.query,
        )
    if arguments.command == "propose-write":
        return propose_durable_write(
            lane=arguments.lane,
            runtime_root=arguments.runtime_root,
            project_store=arguments.project_store,
            project_id=arguments.project_id,
            project_object_id=arguments.project_object_id,
            target_kind=arguments.target_kind,
            idempotency_key=arguments.idempotency_key,
            reason_code=arguments.reason_code,
            proposal_only=arguments.proposal_only,
        )
    if arguments.command == "verify-router-log":
        return verify_operator_router_log_files(
            evidence_root=arguments.evidence_root,
            plan_path=arguments.plan,
            log_path=arguments.log,
        )
    digest, file_count = practical_v1_source_tree_digest(ROOT)
    return {
        "schema_version": 1,
        "operation": "source-binding",
        "status": "PASS",
        "source_tree_digest": digest,
        "source_file_count": file_count,
    }


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        result = dispatch(arguments)
    except (ContractError, OSError, ValueError) as error:
        reason = error.code if isinstance(error, StorageError) else type(error).__name__
        print(json.dumps({"status": "FAIL", "reason_code": reason}, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 2 if result.get("status") == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
