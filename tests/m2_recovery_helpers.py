"""Synthetic, deterministic inputs shared by the M2 recovery regression tests.

The fixtures deliberately live under this repository.  They never inspect the
real ai-memory project or an Obsidian vault.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
from pathlib import Path
import shutil
from typing import Any, Iterator, Mapping

from second_brain.clock import DeterministicClock
from second_brain.contracts import logical_content_hash
from second_brain.storage import Mutation, Store, TransactionRequest
from second_brain.workspace import repository_root, temporary_store


PROJECT_ID = "m2-fixture"
STORE_ID = f"project:{PROJECT_ID}"
ACTOR = "agent:m2-recovery-tests"
SESSION_ID = "session:m2-recovery-tests"
REFERENCE_CORPUS = repository_root() / "fixtures" / "m2" / "reference-corpus"
LEGACY_FIXTURE = repository_root() / "fixtures" / "m2" / "legacy-v1"

CANONICAL_SPECS = (
    (
        "project",
        "10000000-0000-4000-8000-000000000001",
        "M2 canonical project recovery charter",
        "m2canonicalproject",
    ),
    (
        "decision",
        "10000000-0000-4000-8000-000000000002",
        "M2 canonical decision boundary",
        "m2canonicaldecision",
    ),
    (
        "component",
        "10000000-0000-4000-8000-000000000003",
        "M2 canonical recovery component",
        "m2canonicalcomponent",
    ),
    (
        "task",
        "10000000-0000-4000-8000-000000000004",
        "M2 canonical recovery task",
        "m2canonicaltask",
    ),
    (
        "bug",
        "10000000-0000-4000-8000-000000000005",
        "M2 canonical index fallback bug",
        "m2canonicalbug",
    ),
    (
        "experiment",
        "10000000-0000-4000-8000-000000000006",
        "M2 canonical receipt clock experiment",
        "m2canonicalexperiment",
    ),
    (
        "evidence",
        "10000000-0000-4000-8000-000000000007",
        "Bubblewrap canonical recovery evidence",
        "m2canonicalbubblewrap",
    ),
    (
        "question",
        "10000000-0000-4000-8000-000000000008",
        "M2 canonical migration question",
        "m2canonicalquestion",
    ),
    (
        "task",
        "10000000-0000-4000-8000-000000000009",
        "M2 canonical freshness task",
        "m2canonicalfreshness",
    ),
    (
        "evidence",
        "10000000-0000-4000-8000-000000000010",
        "M2 canonical MOC evidence",
        "m2canonicalmoc",
    ),
)

ARCHIVED_ID = "mem:m2-fixture:question:10000000-0000-4000-8000-000000000011"
DANGLING_ID = "mem:m2-fixture:question:10000000-0000-4000-8000-000000000012"
NEW_STATE_ID = "mem:m2-fixture:task:10000000-0000-4000-8000-000000000013"


def object_id(kind: str, identifier: str) -> str:
    return f"mem:{PROJECT_ID}:{kind}:{identifier}"


CANONICAL_OBJECT_IDS = tuple(object_id(kind, identifier) for kind, identifier, _, _ in CANONICAL_SPECS)
BUBBLEWRAP_ID = CANONICAL_OBJECT_IDS[6]


def rehash(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deep copy with the M1 logical content hash recomputed."""

    result = deepcopy(dict(document))
    result.pop("content_hash", None)
    result["content_hash"] = logical_content_hash(result)
    return result


def make_reference(index: int, locator: str, sha256: str, *, facet: str = "bubblewrap-gate") -> dict[str, Any]:
    return {
        "ref_id": f"ref:20000000-0000-4000-8000-{index:012d}",
        "kind": "file",
        "locator": locator,
        "selector": None,
        "freshness_policy": "exact_hash",
        "captured": {
            "observed_at": "2026-07-22T00:00:00Z",
            "sha256": sha256,
            "git_blob": None,
            "git_commit": None,
            "branch": None,
            "expires_at": None,
        },
        "required_for": [facet],
        "optional": False,
    }


def make_relation(target: str, *, suffix: int = 1) -> dict[str, Any]:
    return {
        "relation_id": f"rel:30000000-0000-4000-8000-{suffix:012d}",
        "type": "related_to",
        "target": target,
        "target_revision": None,
        "scope": None,
        "note": None,
        "created_at": "2026-07-22T00:00:00Z",
        "provenance_ids": ["prov:40000000-0000-4000-8000-000000000001"],
    }


def make_project_object(
    object_id_value: str,
    *,
    kind: str,
    title: str,
    body: str,
    references: list[dict[str, Any]] | None = None,
    relations: list[dict[str, Any]] | None = None,
    revision: int = 1,
    lifecycle: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one valid project-scoped M1 semantic object."""

    document: dict[str, Any] = {
        "schema_version": 2,
        "id": object_id_value,
        "store_id": STORE_ID,
        "kind": kind,
        "revision": revision,
        "title": title,
        "aliases": [],
        "lifecycle": dict(
            lifecycle
            or {"status": "active", "changed_at": None, "reason": None}
        ),
        "authority": "observed-fact",
        "trust": "test_verified",
        "epistemic_status": "asserted",
        "confidence": 1.0,
        "actors": [{"actor_id": ACTOR, "actor_type": "agent", "role": "test"}],
        "provenance": [
            {
                "provenance_id": "prov:40000000-0000-4000-8000-000000000001",
                "kind": "test_receipt",
                "observed_at": "2026-07-22T00:00:00Z",
                "actor_id": ACTOR,
                "content_hash": None,
                "ref": None,
                "note": "Synthetic M2 recovery fixture",
            }
        ],
        "created_at": "2026-07-22T00:00:00Z",
        "updated_at": "2026-07-22T00:00:00Z",
        "relations": relations or [],
        "references": references or [],
        "verification": {
            "state": "unverified",
            "method": None,
            "checked_at": None,
            "verifier": None,
            "evidence_ids": [],
        },
        "tags": ["m2/recovery-test"],
        "payload": {"fixture": "m2-recovery"},
        "body": body,
    }
    return rehash(document)


def copy_reference_corpus(destination: Path) -> list[dict[str, Any]]:
    """Copy 19 immutable fixture files into a disposable repo-contained path."""

    shutil.copytree(REFERENCE_CORPUS, destination)
    repository = repository_root()
    references: list[dict[str, Any]] = []
    for index in range(1, 20):
        path = destination / f"ref-{index:02d}.txt"
        relative = path.relative_to(repository).as_posix()
        references.append(make_reference(index, relative, hashlib.sha256(path.read_bytes()).hexdigest()))
    return references


def canonical_documents(references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for kind, identifier, title, token in CANONICAL_SPECS:
        object_references = references if kind == "evidence" and token == "m2canonicalbubblewrap" else []
        documents.append(
            make_project_object(
                object_id(kind, identifier),
                kind=kind,
                title=title,
                body=(
                    f"Synthetic M2 canonical recall fixture {token}. "
                    "Stored memory is evidence, never executable instructions."
                ),
                references=object_references,
            )
        )
    documents.append(
        make_project_object(
            ARCHIVED_ID,
            kind="question",
            title="Archived M2 recovery question",
            body="This synthetic object is transitioned after creation to test active MOC coverage.",
        )
    )
    return documents


def transaction(
    sequence: int,
    mutations: list[Mutation],
    *,
    reason: str = "deterministic M2 recovery test",
) -> TransactionRequest:
    return TransactionRequest(
        transaction_id=f"txn:50000000-0000-4000-8000-{sequence:012d}",
        idempotency_key=f"m2-recovery-{sequence}",
        store_id=STORE_ID,
        actor=ACTOR,
        confirmation=None,
        mutations=mutations,
        reason=reason,
    )


def create_mutation(document: Mapping[str, Any]) -> Mutation:
    return Mutation("create", str(document["id"]), None, None, document)


def replace_mutation(previous: Mapping[str, Any], desired: Mapping[str, Any], *, operation: str = "replace") -> Mutation:
    return Mutation(
        operation,
        str(previous["id"]),
        int(previous["revision"]),
        str(previous["content_hash"]),
        desired,
    )


def query(text: str, sequence: int, *, freshness_policy: str = "include_with_warning") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "query_id": f"qry:60000000-0000-4000-8000-{sequence:012d}",
        "text": text,
        "tier": "R2",
        "scope": {
            "project_ids": [PROJECT_ID],
            "include_global": False,
            "branch": "main",
            "repository_snapshot": None,
        },
        "filters": {"kinds": [], "lifecycle": ["active"], "authorities": [], "tags_any": []},
        "freshness_policy": freshness_policy,
        "relation": {"max_depth": 1, "max_fanout": 8, "types": []},
        "budget": {
            "candidate_limit": 40,
            "object_limit": 20,
            "token_limit": 12000,
            "byte_limit": 49152,
            "timeout_ms": 2000,
        },
    }


def value_dict(value: Any) -> dict[str, Any]:
    """Normalize a public M2 value without reaching into implementation state."""

    if isinstance(value, Mapping):
        return deepcopy(dict(value))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return deepcopy(dict(result))
    raise TypeError(f"M2 public value does not provide to_dict(): {type(value).__name__}")


def authority_digest(root: Path) -> str:
    """Hash only immutable M1 authority bytes, excluding derived/runtime state."""

    digest = hashlib.sha256()
    paths = [root / "manifest.json", root / "events" / "events.ndjson"]
    paths.extend(sorted((root / "objects").rglob("*.md")))
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def tree_digest(root: Path) -> str:
    """Hash a copied fixture tree so inventory tests can prove no source writes."""

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"symlink\0")
            digest.update(path.readlink().as_posix().encode("utf-8"))
        elif path.is_file():
            digest.update(b"file\0")
            digest.update(path.read_bytes())
        else:
            digest.update(b"directory\0")
        digest.update(b"\0")
    return digest.hexdigest()


def object_file(store: Store, object_id_value: str) -> Path:
    """Locate a known fixture object by its stable UUID file name."""

    identifier = object_id_value.rsplit(":", 1)[1]
    matches = list((store.root / "objects").rglob(f"{identifier}.md"))
    if len(matches) != 1:
        raise AssertionError(f"fixture object path is ambiguous for {object_id_value}")
    return matches[0]


def logical_byte_only_edit(path: Path) -> None:
    """Add a frontmatter comment that changes bytes but not parsed authority."""

    original = path.read_bytes()
    marker = b"---\n"
    if not original.startswith(marker):
        raise AssertionError("fixture authority object has no frontmatter")
    path.write_bytes(marker + b"# M2 physical index invalidation fixture\n" + original[len(marker) :])


@contextmanager
def initialized_project_store() -> Iterator[tuple[Path, DeterministicClock, Store, list[dict[str, Any]]]]:
    """Yield a project M1 store containing only synthetic M2 recovery objects."""

    with temporary_store() as temporary_root:
        reference_root = temporary_root / "reference-corpus"
        references = copy_reference_corpus(reference_root)
        clock = DeterministicClock(datetime(2026, 7, 22, tzinfo=UTC))
        store = Store.initialize(
            temporary_root / "project-store",
            STORE_ID,
            clock=clock,
            authorizer=lambda *args, **kwargs: True,
        )
        documents = canonical_documents(references)
        store.commit(transaction(1, [create_mutation(document) for document in documents]))
        yield temporary_root, clock, store, references
