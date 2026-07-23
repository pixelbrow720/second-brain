"""Shared deterministic fixtures for the M1 public storage contract tests."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from second_brain.clock import DeterministicClock
from second_brain.storage import (
    Mutation,
    Store,
    TransactionRequest,
    sha256_hex,
)
from second_brain.workspace import repository_root, temporary_store


STORE_ID = "knowledge:global"
ACTOR = "agent:m1-test"
OBJECT_A = "kb:global:concept:00000000-0000-4000-8000-000000000001"
OBJECT_B = "kb:global:concept:00000000-0000-4000-8000-000000000002"
OBJECT_C = "kb:global:concept:00000000-0000-4000-8000-000000000003"
OBJECT_PROJECT = "mem:other-project:decision:00000000-0000-4000-8000-000000000004"


def fixture_path(name: str) -> Path:
    return repository_root() / "fixtures" / "m1" / name


def rehash(document: dict[str, Any]) -> dict[str, Any]:
    """Return a logical record with its JCS content hash recalculated."""

    result = deepcopy(document)
    result.pop("content_hash", None)
    result["content_hash"] = sha256_hex(result)
    return result


def make_relation(
    target: str,
    *,
    relation_type: str = "related_to",
    provenance_id: str = "prov:00000000-0000-4000-8000-000000000001",
    suffix: str = "1",
) -> dict[str, Any]:
    return {
        "relation_id": f"rel:00000000-0000-4000-8000-00000000000{suffix}",
        "type": relation_type,
        "target": target,
        "target_revision": None,
        "scope": None,
        "note": None,
        "created_at": "2026-07-22T00:00:00Z",
        "provenance_ids": [provenance_id],
    }


def make_object(
    object_id: str,
    *,
    title: str = "M1 synthetic object",
    body: str = "Deterministic synthetic storage fixture.",
    revision: int = 1,
    relations: list[dict[str, Any]] | None = None,
    provenance_id: str = "prov:00000000-0000-4000-8000-000000000001",
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": 2,
        "id": object_id,
        "store_id": STORE_ID,
        "kind": "concept",
        "revision": revision,
        "title": title,
        "aliases": [],
        "lifecycle": {"status": "active", "changed_at": None, "reason": None},
        "authority": "ai-synthesis",
        "trust": "test_verified",
        "epistemic_status": "asserted",
        "confidence": 1.0,
        "actors": [{"actor_id": ACTOR, "actor_type": "agent", "role": "test"}],
        "provenance": [
            {
                "provenance_id": provenance_id,
                "kind": "test_receipt",
                "observed_at": "2026-07-22T00:00:00Z",
                "actor_id": ACTOR,
                "content_hash": None,
                "ref": None,
                "note": "Synthetic M1 fixture",
            }
        ],
        "created_at": "2026-07-22T00:00:00Z",
        "updated_at": "2026-07-22T00:00:00Z",
        "relations": relations or [],
        "references": [],
        "verification": {
            "state": "unverified",
            "method": None,
            "checked_at": None,
            "verifier": None,
            "evidence_ids": [],
        },
        "tags": ["m1/test"],
        "payload": {"definition": "Synthetic storage object"},
        "body": body,
    }
    return rehash(document)


def create_mutation(document: dict[str, Any]) -> Mutation:
    return Mutation(
        operation="create",
        object_id=document["id"],
        expected_revision=None,
        expected_content_hash=None,
        desired_object=document,
    )


def replace_mutation(
    previous: dict[str, Any], desired: dict[str, Any]
) -> Mutation:
    return Mutation(
        operation="replace",
        object_id=previous["id"],
        expected_revision=previous["revision"],
        expected_content_hash=previous["content_hash"],
        desired_object=desired,
    )


def transaction(
    transaction_id: str,
    idempotency_key: str,
    mutations: list[Mutation],
    *,
    reason: str = "deterministic M1 contract test",
) -> TransactionRequest:
    return TransactionRequest(
        transaction_id=transaction_id,
        idempotency_key=idempotency_key,
        store_id=STORE_ID,
        actor=ACTOR,
        confirmation="test-confirmed",
        mutations=mutations,
        reason=reason,
    )


@contextmanager
def initialized_store(
    *, authorizer: Any = None, failure_injector: Any = None
) -> Iterator[tuple[Path, DeterministicClock, Store]]:
    """Create one explicit disposable store rooted under repository artifacts."""

    with temporary_store() as temporary_root:
        root = temporary_root / "m1-store"
        clock = DeterministicClock(datetime(2026, 7, 22, tzinfo=UTC))
        store = Store.initialize(
            root,
            STORE_ID,
            clock=clock,
            authorizer=authorizer,
            failure_injector=failure_injector,
        )
        yield root, clock, store


def open_store(
    root: Path,
    clock: DeterministicClock,
    *, authorizer: Any = None, failure_injector: Any = None
) -> Store:
    return Store.open(
        root,
        clock=clock,
        authorizer=authorizer,
        failure_injector=failure_injector,
    )


def error_code(error: BaseException) -> str | None:
    return getattr(error, "code", None)
