"""M3's project-local global-knowledge compiler and derived navigation.

The module deliberately has a narrow authority boundary: semantic objects are
written only through the public :class:`Store` transaction API.  Raw captures
are treated as opaque evidence through a concrete
:class:`RawCaptureRepository`; source bytes are never read, interpreted, or
copied into a knowledge object here.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Iterator, Mapping, Sequence
import uuid

from .contracts import validate_named_document
from .errors import StorageError
from .ingest import CaptureError, RawCaptureRepository, validate_capture_manifest
from .parsing import parse_strict_json
from .storage import (
    CommitReceipt as StoreCommitReceipt,
    LintFinding,
    LintReport,
    Mutation,
    Store,
    StoreSnapshot,
    TransactionRequest,
    canonical_jcs_bytes,
    sha256_hex,
)
from .workspace import repository_root


KNOWLEDGE_COMPILER_VERSION = "second-brain-knowledge/3.0.0"
KNOWLEDGE_VIEWS_VERSION = "second-brain-knowledge-views/3.0.0"
_GLOBAL_STORE_ID = "knowledge:global"
_KNOWLEDGE_KINDS = frozenset({"source", "entity", "concept", "claim", "synthesis"})
_HEX = re.compile(r"^[0-9a-f]{64}$")
_CAPTURE_ID = re.compile(r"^cap:[0-9a-f-]{36}$")
_UUID_ID = re.compile(r"^(?:proposal|promotion):[0-9a-f-]{36}$")
_PROJECT_STORE = re.compile(r"^project:([a-z0-9][a-z0-9._-]{0,63})$")
_PROJECT_OBJECT = re.compile(
    r"^mem:([a-z0-9][a-z0-9._-]{0,63}):(project|decision|component|task|bug|"
    r"experiment|evidence|question):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
_GLOBAL_OBJECT = re.compile(
    r"^kb:global:(source|entity|concept|claim|synthesis):[0-9a-f]{8}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_SOURCE_TYPES = frozenset(
    {"article", "paper", "book", "video", "podcast", "dataset", "documentation", "conversation", "other"}
)
_ENTITY_TYPES = frozenset({"person", "organization", "product", "project", "place", "standard", "other"})
_CLAIM_TYPES = frozenset({"empirical", "definitional", "causal", "comparative", "recommendation", "prediction"})
_SYNTHESIS_TYPES = frozenset({"overview", "comparison", "architecture", "timeline", "guide", "thesis"})
_COVERAGE_STATES = frozenset({"draft", "partial", "reviewed"})
_RELATION_TYPES = frozenset(
    {
        "cites",
        "derived_from",
        "about",
        "mentions",
        "supports",
        "contradicts",
        "refines",
        "supersedes",
        "part_of",
        "depends_on",
        "implements",
        "verifies",
        "invalidates",
        "related_to",
    }
)
_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _plain(value: Any) -> Any:
    """Detach public values into ordinary JSON-compatible containers."""

    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _require_text(value: Any, name: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise StorageError("SCHEMA_INVALID", f"{name} must be bounded non-empty text")
    return value


def _optional_text(value: Any, name: str, *, maximum: int = 4096) -> str | None:
    if value is None:
        return None
    return _require_text(value, name, maximum=maximum)


def _require_hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or _HEX.fullmatch(value) is None:
        raise StorageError("SCHEMA_INVALID", f"{name} must be a SHA-256 digest")
    return value


def _require_int(value: Any, name: str, *, minimum: int = 0, maximum: int = 2**31 - 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise StorageError("SCHEMA_INVALID", f"{name} is outside its allowed range")
    return value


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StorageError("SCHEMA_INVALID", f"{name} must be an object")
    return value


def _require_text_list(value: Any, name: str, *, minimum: int = 0, maximum: int = 128) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not minimum <= len(value) <= maximum:
        raise StorageError("SCHEMA_INVALID", f"{name} must be a bounded list")
    result = tuple(_require_text(item, name, maximum=2048) for item in value)
    if len(set(result)) != len(result):
        raise StorageError("SCHEMA_INVALID", f"{name} contains duplicate values")
    return result


def _utc_now(clock: Any) -> datetime:
    value = clock.now() if hasattr(clock, "now") else clock() if callable(clock) else datetime.now(UTC)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise StorageError("SCHEMA_INVALID", "clock must return an aware datetime")
    return value.astimezone(UTC)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise StorageError("SCHEMA_INVALID", f"{name} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise StorageError("SCHEMA_INVALID", f"{name} must be an RFC3339 timestamp") from error
    if parsed.tzinfo is None:
        raise StorageError("SCHEMA_INVALID", f"{name} must be an aware timestamp")
    return parsed.astimezone(UTC)


def _stable_transaction_id(proposal_id: str) -> str:
    return f"txn:{uuid.uuid5(uuid.NAMESPACE_URL, 'second-brain/m3/' + proposal_id)}"


def _normalized_title(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _object_corpus_digest(snapshot: StoreSnapshot) -> str:
    """Bind derived state to exact authoritative object bytes and identity."""

    return sha256_hex(
        {
            "store_id": snapshot.store_id,
            "mutation_epoch": snapshot.mutation_epoch,
            "event_head": snapshot.event_head,
            "objects": [
                {
                    "id": item.id,
                    "revision": item.revision,
                    "content_hash": item.content_hash,
                    "file_sha256": item.file_sha256,
                }
                for item in snapshot.objects
            ],
        }
    )


@dataclass(frozen=True)
class CompilationProposal:
    """A reviewable, full-object M1 mutation graph with no raw source bytes."""

    proposal_id: str
    compiler_version: str
    idempotency_key: str
    rationale: str
    capture_ids: tuple[str, ...]
    citation_map: Mapping[str, Any]
    mutations: tuple[Mutation, ...]
    transaction_id: str | None = None
    actor: str = "agent:knowledge-compiler"
    schema_version: int = 1

    @classmethod
    def from_value(cls, value: "CompilationProposal | Mapping[str, Any]") -> "CompilationProposal":
        if isinstance(value, cls):
            value = value.to_dict()
        if not isinstance(value, Mapping):
            raise StorageError("SCHEMA_INVALID", "compilation proposal must be an object")
        raw_mutations = value.get("mutations")
        if not isinstance(raw_mutations, (list, tuple)):
            raise StorageError("SCHEMA_INVALID", "proposal mutations must be a list")
        try:
            mutations = tuple(Mutation.from_value(item) for item in raw_mutations)
        except StorageError:
            raise
        except Exception as error:
            raise StorageError("SCHEMA_INVALID", "proposal mutations are invalid") from error
        proposal = cls(
            proposal_id=_require_text(value.get("proposal_id"), "proposal_id", maximum=128),
            compiler_version=_require_text(
                value.get("compiler_version", KNOWLEDGE_COMPILER_VERSION), "compiler_version", maximum=160
            ),
            idempotency_key=_require_text(value.get("idempotency_key"), "idempotency_key", maximum=1024),
            rationale=_require_text(value.get("rationale"), "rationale", maximum=2000),
            capture_ids=_require_text_list(value.get("capture_ids", ()), "capture_ids", maximum=128),
            citation_map=deepcopy(dict(_require_mapping(value.get("citation_map", {}), "citation_map"))),
            mutations=mutations,
            transaction_id=_optional_text(value.get("transaction_id"), "transaction_id", maximum=128),
            actor=_require_text(value.get("actor", "agent:knowledge-compiler"), "actor", maximum=160),
            schema_version=_require_int(value.get("schema_version", 1), "schema_version", minimum=1, maximum=1),
        )
        if _UUID_ID.fullmatch(proposal.proposal_id) is None:
            raise StorageError("SCHEMA_INVALID", "proposal_id must be a proposal UUID")
        if proposal.transaction_id is not None and not re.fullmatch(
            r"txn:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", proposal.transaction_id
        ):
            raise StorageError("SCHEMA_INVALID", "transaction_id must be a transaction UUID")
        if len(proposal.mutations) == 0 or len(proposal.mutations) > 256:
            raise StorageError("SCHEMA_INVALID", "proposal must contain one to 256 mutations")
        if len({item.object_id for item in proposal.mutations}) != len(proposal.mutations):
            raise StorageError("SCHEMA_INVALID", "proposal mutates one object more than once")
        if any(_CAPTURE_ID.fullmatch(capture_id) is None for capture_id in proposal.capture_ids):
            raise StorageError("SCHEMA_INVALID", "proposal capture ID is invalid")
        return proposal

    @property
    def resolved_transaction_id(self) -> str:
        return self.transaction_id or _stable_transaction_id(self.proposal_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "compiler_version": self.compiler_version,
            "idempotency_key": self.idempotency_key,
            "rationale": self.rationale,
            "capture_ids": list(self.capture_ids),
            "citation_map": _plain(self.citation_map),
            "mutations": [
                {
                    "operation": item.operation,
                    "object_id": item.object_id,
                    "expected_revision": item.expected_revision,
                    "expected_content_hash": item.expected_content_hash,
                    "desired_object": _plain(item.desired_object),
                }
                for item in self.mutations
            ],
            "transaction_id": self.transaction_id,
            "actor": self.actor,
        }


@dataclass(frozen=True)
class ProposalValidation:
    """The no-write validation result for one whole compilation proposal."""

    accepted: bool
    reason_codes: tuple[str, ...]
    object_diff: tuple[Mapping[str, Any], ...]
    proposal_id: str | None = None

    @property
    def rejected(self) -> bool:
        return not self.accepted

    @property
    def reasons(self) -> tuple[str, ...]:
        """Compatibility alias for callers that only need stable codes."""

        return self.reason_codes

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "accepted": self.accepted,
            "reason_codes": list(self.reason_codes),
            "object_diff": [_plain(item) for item in self.object_diff],
        }


@dataclass(frozen=True)
class DerivedViews:
    """A deterministic, disposable view tree bound to one M1 snapshot."""

    root: str
    mutation_epoch: int
    event_head: str | None
    corpus_digest: str
    builder_version: str
    view_digest: str
    tree_digest: str
    files: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "mutation_epoch": self.mutation_epoch,
            "event_head": self.event_head,
            "corpus_digest": self.corpus_digest,
            "builder_version": self.builder_version,
            "view_digest": self.view_digest,
            "tree_digest": self.tree_digest,
            "files": list(self.files),
        }


@dataclass(frozen=True)
class CompilationReceipt:
    """Bind a committed semantic proposal to the derived tree rebuilt from it."""

    proposal_id: str
    commit_receipt: StoreCommitReceipt
    changed_object_ids: tuple[str, ...]
    derived_view_digest: str
    derived_view_epoch: int

    @property
    def store_receipt(self) -> StoreCommitReceipt:
        return self.commit_receipt

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "commit_receipt": self.commit_receipt.to_dict(),
            "changed_object_ids": list(self.changed_object_ids),
            "derived_view_digest": self.derived_view_digest,
            "derived_view_epoch": self.derived_view_epoch,
        }


class KnowledgeCompiler:
    """Compile reviewed global knowledge proposals through exactly one M1 commit."""

    def __init__(self, store: Store, raw_captures: RawCaptureRepository, clock: Any = None) -> None:
        if not isinstance(store, Store):
            raise StorageError("SCHEMA_INVALID", "KnowledgeCompiler requires a public Store")
        if type(raw_captures) is not RawCaptureRepository:
            raise StorageError("SCHEMA_INVALID", "KnowledgeCompiler requires a local raw capture repository")
        self.store = store
        # Refuse an externally-mutated Store handle before even asking it for a
        # snapshot: M3 has no authority outside this repository-local staging root.
        self._store_root = _repository_contained(store.root)
        self.raw_captures = raw_captures
        self._capture_root = _repository_contained(raw_captures.root)
        self.clock = clock
        self._assert_global_store()

    def _assert_global_store(self) -> StoreSnapshot:
        self._assert_store_root_identity()
        snapshot = self.store.snapshot()
        if snapshot.store_id != _GLOBAL_STORE_ID or snapshot.project_id is not None:
            raise StorageError("AUTHORITY_DENIED", "M3 compiler requires knowledge:global")
        return snapshot

    def _assert_store_root_identity(self) -> None:
        try:
            current = _repository_contained(Path(self.store.root))
        except (TypeError, StorageError) as error:
            raise StorageError("PATH_UNSAFE", "knowledge Store root is invalid") from error
        if current != self._store_root or Path(self.store.root) != self._store_root:
            raise StorageError("PATH_UNSAFE", "knowledge Store root changed after compiler initialization")

    def _assert_capture_repository_identity(self) -> None:
        if type(self.raw_captures) is not RawCaptureRepository:
            raise StorageError("CAPTURE_INVALID", "raw capture repository identity changed")
        try:
            current = _repository_contained(Path(self.raw_captures.root))
        except (TypeError, StorageError) as error:
            raise StorageError("PATH_UNSAFE", "raw capture root is invalid") from error
        if current != self._capture_root or Path(self.raw_captures.root) != self._capture_root:
            raise StorageError("PATH_UNSAFE", "raw capture root changed after compiler initialization")

    def validate(self, proposal: CompilationProposal | Mapping[str, Any]) -> ProposalValidation:
        """Validate the entire future graph without creating semantic side effects."""

        proposal_id: str | None = proposal.proposal_id if isinstance(proposal, CompilationProposal) else None
        diff: tuple[Mapping[str, Any], ...] = ()
        try:
            normalized = CompilationProposal.from_value(proposal)
            proposal_id = normalized.proposal_id
            snapshot = self._assert_global_store()
            diff = self._proposal_diff(normalized, snapshot)
            self._validate_complete_graph(normalized, snapshot)
        except StorageError as error:
            return ProposalValidation(False, (error.code,), diff, proposal_id)
        except Exception:
            return ProposalValidation(False, ("SCHEMA_INVALID",), diff, proposal_id)
        return ProposalValidation(True, (), diff, normalized.proposal_id)

    def commit(self, proposal: CompilationProposal | Mapping[str, Any]) -> CompilationReceipt:
        """Atomically commit a validated proposal once, then rebuild derived state."""

        normalized = CompilationProposal.from_value(proposal)
        validation = self.validate(normalized)
        if not validation.accepted and validation.reason_codes != ("CAS_CONFLICT",):
            code = validation.reason_codes[0] if validation.reason_codes else "PROPOSAL_REJECTED"
            raise StorageError("PROPOSAL_REJECTED", f"proposal rejected: {code}")

        # Recheck the mutable public Store handle immediately before its sole semantic write.
        self._assert_global_store()
        receipt = self.store.commit(
            TransactionRequest(
                transaction_id=normalized.resolved_transaction_id,
                idempotency_key=normalized.idempotency_key,
                store_id=_GLOBAL_STORE_ID,
                actor=normalized.actor,
                confirmation=None,
                mutations=normalized.mutations,
                reason=normalized.rationale,
            )
        )
        views = self.rebuild_views()
        return CompilationReceipt(
            proposal_id=normalized.proposal_id,
            commit_receipt=receipt,
            changed_object_ids=tuple(item.id for item in receipt.changed_objects),
            derived_view_digest=views.tree_digest,
            derived_view_epoch=views.mutation_epoch,
        )

    def rebuild_views(self) -> DerivedViews:
        """Regenerate all disposable navigation from one authoritative snapshot."""

        snapshot = self._assert_global_store()
        files, view_digest = self._expected_view_files(snapshot)
        derived_root = _safe_child(self._store_root, Path("derived"))
        target = _safe_child(derived_root, Path("knowledge"))
        _write_tree_atomically(target, files, boundary=self._store_root)
        tree_digest = _tree_digest(files)
        return DerivedViews(
            root=str(target.relative_to(self._store_root)),
            mutation_epoch=snapshot.mutation_epoch,
            event_head=snapshot.event_head,
            corpus_digest=_object_corpus_digest(snapshot),
            builder_version=KNOWLEDGE_VIEWS_VERSION,
            view_digest=view_digest,
            tree_digest=tree_digest,
            files=tuple(sorted(files)),
        )

    def lint(self) -> LintReport:
        """Combine M1 integrity lint with M3 semantic and derived-state checks."""

        self._assert_store_root_identity()
        base = self.store.lint()
        findings = list(base.findings)
        try:
            snapshot = self._assert_global_store()
            documents = {item.id: deepcopy(dict(item.document)) for item in snapshot.objects}
            findings.extend(self._semantic_lint(documents))
            findings.extend(self._derived_lint(snapshot))
        except StorageError as error:
            findings.append(LintFinding("error", error.code, "knowledge lint could not read authority"))
        except Exception:
            findings.append(LintFinding("error", "INTEGRITY_FAILED", "knowledge lint could not finish"))

        # A deterministic report is easier to compare across clean-room runs.
        unique: dict[tuple[Any, ...], LintFinding] = {}
        for finding in findings:
            key = (finding.severity, finding.code, finding.object_id, finding.relation_id, finding.message)
            unique[key] = finding
        ordered = tuple(
            sorted(
                unique.values(),
                key=lambda item: (item.severity, item.code, item.object_id or "", item.relation_id or "", item.message),
            )
        )
        return LintReport(
            findings=ordered,
            exit_code=1 if any(item.severity == "error" for item in ordered) else 0,
        )

    def _validate_complete_graph(self, proposal: CompilationProposal, snapshot: StoreSnapshot) -> None:
        current = {item.id: deepcopy(dict(item.document)) for item in snapshot.objects}
        proposed = deepcopy(current)
        changed: set[str] = set()
        for mutation in proposal.mutations:
            if mutation.operation not in {"create", "replace", "transition", "tombstone"}:
                raise StorageError("SCHEMA_INVALID", "proposal has an unsupported mutation operation")
            if not isinstance(mutation.desired_object, Mapping):
                raise StorageError("SCHEMA_INVALID", "proposal desired object must be an object")
            desired = deepcopy(dict(mutation.desired_object))
            if desired.get("id") != mutation.object_id:
                raise StorageError("SCHEMA_INVALID", "proposal mutation ID does not match object")
            self._validate_m1_document(desired)
            existing = current.get(mutation.object_id)
            if (
                existing is not None
                and existing.get("kind") == "claim"
                and mutation.operation in {"transition", "tombstone"}
            ):
                raise StorageError("MINORITY_EVIDENCE_PROTECTED", "compiler cannot delete or retire claims")
            self._validate_expected_cas(mutation, desired, existing)
            if existing is not None and existing.get("kind") == "claim":
                self._protect_claim_replacement(existing, desired, mutation.operation)
            proposed[mutation.object_id] = desired
            changed.add(mutation.object_id)

        if any(document.get("store_id") != _GLOBAL_STORE_ID for document in proposed.values()):
            raise StorageError("AUTHORITY_INVALID", "M3 semantic objects must remain in knowledge:global")
        if any(document.get("kind") not in _KNOWLEDGE_KINDS for document in proposed.values()):
            raise StorageError("SCHEMA_INVALID", "M3 supports only global knowledge kinds")

        required_capture_ids = set(proposal.capture_ids)
        for document in proposed.values():
            if document.get("kind") != "source":
                continue
            payload = _require_mapping(document.get("payload"), "source payload")
            raw = _require_mapping(payload.get("raw"), "source raw binding")
            required_capture_ids.add(_require_text(raw.get("capture_id"), "source capture_id", maximum=64))
        capture_cache = self._eligible_captures(tuple(sorted(required_capture_ids)))
        for object_id in sorted(proposed):
            document = proposed[object_id]
            self._validate_kind(document, proposed, proposal, capture_cache, object_id in changed)
            self._validate_relations(document, proposed)
        self._validate_contradiction_synthesis_visibility(proposed, changed)
        self._validate_synthesis_citations(proposed, proposal, changed=changed)

    def _validate_m1_document(self, document: dict[str, Any]) -> None:
        try:
            validate_named_document("memory-object-v2", document)
        except Exception as error:
            code = "CONTENT_HASH_MISMATCH" if "content_hash" in str(error) else "SCHEMA_INVALID"
            raise StorageError(code, "proposal object does not satisfy the M1 memory contract") from error

    def _validate_expected_cas(
        self, mutation: Mutation, desired: Mapping[str, Any], existing: Mapping[str, Any] | None
    ) -> None:
        if mutation.operation == "create":
            if existing is not None or mutation.expected_revision is not None or mutation.expected_content_hash is not None:
                raise StorageError("CAS_CONFLICT", "create proposal no longer matches authority")
            if desired.get("revision") != 1:
                raise StorageError("SCHEMA_INVALID", "created semantic object must begin at revision one")
            return
        if existing is None:
            raise StorageError("CAS_CONFLICT", "replacement proposal no longer matches authority")
        if (
            mutation.expected_revision != existing.get("revision")
            or mutation.expected_content_hash != existing.get("content_hash")
        ):
            raise StorageError("CAS_CONFLICT", "proposal CAS expectation is stale")
        if desired.get("revision") != existing.get("revision", 0) + 1:
            raise StorageError("SCHEMA_INVALID", "replacement must advance revision exactly once")
        for name in ("id", "store_id", "kind", "created_at"):
            if desired.get(name) != existing.get(name):
                raise StorageError("SCHEMA_INVALID", "proposal changed an immutable object identity field")

    def _protect_claim_replacement(
        self,
        existing: Mapping[str, Any],
        desired: Mapping[str, Any],
        operation: str,
    ) -> None:
        """Keep an atomic claim and its existing evidence append-only under M3."""

        if operation != "replace":
            raise StorageError("MINORITY_EVIDENCE_PROTECTED", "compiler cannot retire a claim")
        existing_payload = _require_mapping(existing.get("payload"), "existing claim payload")
        desired_payload = _require_mapping(desired.get("payload"), "replacement claim payload")
        for field in ("statement", "subject_ids", "temporal_scope", "applicability", "facet_id"):
            if desired_payload.get(field) != existing_payload.get(field):
                raise StorageError("MINORITY_EVIDENCE_PROTECTED", "claim identity must be revised as a new claim")
        if desired.get("lifecycle") != existing.get("lifecycle") or desired.get("epistemic_status") != existing.get("epistemic_status"):
            raise StorageError("MINORITY_EVIDENCE_PROTECTED", "claim lifecycle or epistemic status cannot erase evidence")
        if not _record_set(existing.get("provenance", ())).issubset(_record_set(desired.get("provenance", ()))):
            raise StorageError("MINORITY_EVIDENCE_PROTECTED", "claim provenance cannot be removed")
        existing_support = _claim_relation_records(existing, {"cites", "derived_from", "supports", "refines"})
        desired_support = _claim_relation_records(desired, {"cites", "derived_from", "supports", "refines"})
        existing_contradictions = _claim_relation_records(existing, {"contradicts"})
        desired_contradictions = _claim_relation_records(desired, {"contradicts"})
        if not _records_preserved(existing_support, desired_support) or not _records_preserved(
            existing_contradictions, desired_contradictions
        ):
            raise StorageError("MINORITY_EVIDENCE_PROTECTED", "claim support or contradiction evidence cannot be removed")
        existing_evidence = _verification_evidence_ids(existing)
        if not existing_evidence.issubset(_verification_evidence_ids(desired)):
            raise StorageError("MINORITY_EVIDENCE_PROTECTED", "claim verification evidence cannot be removed")

    def _eligible_captures(self, capture_ids: Sequence[str]) -> dict[str, Mapping[str, Any]]:
        self._assert_capture_repository_identity()
        captures: dict[str, Mapping[str, Any]] = {}
        for capture_id in capture_ids:
            try:
                # Calling the concrete implementation directly prevents an injected
                # duck-typed reader from forging an accepted capture receipt.
                manifest = RawCaptureRepository.get_manifest(self.raw_captures, capture_id)
                validate_capture_manifest(manifest)
            except CaptureError as error:
                code = "CAPTURE_NOT_FOUND" if error.code == "CAPTURE_NOT_FOUND" else "CAPTURE_INVALID"
                raise StorageError(code, "proposal references an unavailable capture") from error
            except (ValueError, StorageError) as error:
                raise StorageError("CAPTURE_INVALID", "proposal references an unavailable capture") from error
            if not isinstance(manifest, Mapping):
                raise StorageError("CAPTURE_INVALID", "capture manifest is invalid")
            if manifest.get("capture_id") != capture_id:
                raise StorageError("CAPTURE_INVALID", "capture manifest does not bind its requested capture ID")
            scan = manifest.get("scan")
            if (
                manifest.get("status") != "accepted"
                or not isinstance(scan, Mapping)
                or scan.get("decision") != "accept"
            ):
                raise StorageError("CAPTURE_NOT_ACCEPTED", "capture requires explicit acceptance before compilation")
            captures[capture_id] = deepcopy(dict(manifest))
        return captures

    def _validate_kind(
        self,
        document: Mapping[str, Any],
        documents: Mapping[str, Mapping[str, Any]],
        proposal: CompilationProposal,
        captures: Mapping[str, Mapping[str, Any]],
        changed: bool,
    ) -> None:
        kind = document.get("kind")
        payload = _require_mapping(document.get("payload"), "semantic payload")
        if kind == "source":
            self._validate_source(document, payload, proposal, captures, changed)
        elif kind == "entity":
            self._validate_entity(payload)
        elif kind == "concept":
            self._validate_concept(payload)
        elif kind == "claim":
            self._validate_claim(document, payload, documents)
        elif kind == "synthesis":
            self._validate_synthesis(payload, documents)
        else:
            raise StorageError("SCHEMA_INVALID", "unsupported semantic kind")

    def _validate_source(
        self,
        document: Mapping[str, Any],
        payload: Mapping[str, Any],
        proposal: CompilationProposal,
        captures: Mapping[str, Mapping[str, Any]],
        changed: bool,
    ) -> None:
        source_type = _require_text(payload.get("source_type"), "source_type", maximum=64)
        if source_type not in _SOURCE_TYPES:
            raise StorageError("SOURCE_PAYLOAD_INVALID", "source_type is unsupported")
        _optional_text(payload.get("canonical_uri"), "canonical_uri", maximum=4096)
        _require_text_list(payload.get("creators", ()), "source creators", maximum=64)
        _optional_text(payload.get("publisher"), "source publisher", maximum=512)
        if payload.get("published_at") is not None:
            _parse_time(payload.get("published_at"), "published_at")
        _parse_time(payload.get("captured_at"), "captured_at")
        _require_text(payload.get("language"), "source language", maximum=32)
        license_value = _require_mapping(payload.get("license"), "source license")
        if license_value.get("status") not in {"known", "unknown", "restricted"}:
            raise StorageError("SOURCE_PAYLOAD_INVALID", "source license status is invalid")
        _optional_text(license_value.get("identifier"), "source license identifier", maximum=256)
        _optional_text(license_value.get("note"), "source license note", maximum=1000)
        raw = _require_mapping(payload.get("raw"), "source raw binding")
        capture_id = _require_text(raw.get("capture_id"), "source capture_id", maximum=64)
        if _CAPTURE_ID.fullmatch(capture_id) is None:
            raise StorageError("SOURCE_PAYLOAD_INVALID", "source capture ID is invalid")
        _require_hash(raw.get("sha256"), "source raw SHA-256")
        _require_text(raw.get("media_type"), "source raw media type", maximum=255)
        _require_int(raw.get("bytes"), "source raw bytes", minimum=0)
        extraction = _require_mapping(payload.get("extraction"), "source extraction")
        _require_int(extraction.get("revision"), "extraction revision", minimum=1)
        _require_text(extraction.get("extractor"), "source extractor", maximum=160)
        _require_hash(extraction.get("extracted_text_sha256"), "extracted text SHA-256")
        if changed and capture_id not in proposal.capture_ids:
            raise StorageError("CAPTURE_PROVENANCE_MISSING", "changed source must name its capture in the proposal")
        manifest = captures.get(capture_id)
        if manifest is None:
            raise StorageError("CAPTURE_NOT_FOUND", "source capture is not available to this proposal")
        blob = _require_mapping(manifest.get("blob"), "capture blob")
        if (
            raw.get("sha256") != blob.get("sha256")
            or raw.get("media_type") != blob.get("media_type")
            or raw.get("bytes") != blob.get("bytes")
        ):
            raise StorageError("CAPTURE_MISMATCH", "source raw binding does not match its accepted capture")
        source_provenance = [
            item
            for item in document.get("provenance", [])
            if isinstance(item, Mapping)
            and item.get("kind") == "source_capture"
            and item.get("ref") == capture_id
            and item.get("content_hash") == blob.get("sha256")
        ]
        if len(source_provenance) != 1:
            raise StorageError("CAPTURE_PROVENANCE_MISSING", "source lacks matching capture provenance")
        # Raw instruction-looking text must never be copied into semantic state.
        for forbidden in ("raw_text", "raw_bytes", "content", "source_body"):
            if forbidden in payload or forbidden in raw:
                raise StorageError("RAW_CONTENT_FORBIDDEN", "source payload may contain metadata only")
        if document.get("authority") != "source-report":
            raise StorageError("AUTHORITY_INVALID", "source objects remain source reports")

    def _validate_entity(self, payload: Mapping[str, Any]) -> None:
        entity_type = _require_text(payload.get("entity_type"), "entity_type", maximum=64)
        if entity_type not in _ENTITY_TYPES:
            raise StorageError("ENTITY_PAYLOAD_INVALID", "entity type is unsupported")
        _require_text(payload.get("canonical_name"), "entity canonical_name", maximum=240)
        identifiers = payload.get("identifiers", ())
        if not isinstance(identifiers, (list, tuple)) or len(identifiers) > 64:
            raise StorageError("ENTITY_PAYLOAD_INVALID", "entity identifiers are invalid")
        for item in identifiers:
            mapping = _require_mapping(item, "entity identifier")
            _require_text(mapping.get("scheme"), "entity identifier scheme", maximum=128)
            _require_text(mapping.get("value"), "entity identifier value", maximum=1024)
        _require_text(payload.get("disambiguation"), "entity disambiguation", maximum=1000)

    def _validate_concept(self, payload: Mapping[str, Any]) -> None:
        _require_text_list(payload.get("domain"), "concept domain", minimum=1, maximum=32)
        _require_text(payload.get("definition"), "concept definition", maximum=12000)
        _require_text_list(payload.get("boundaries"), "concept boundaries", maximum=64)
        _require_text_list(payload.get("non_examples"), "concept non_examples", maximum=64)

    def _validate_claim(
        self, document: Mapping[str, Any], payload: Mapping[str, Any], documents: Mapping[str, Mapping[str, Any]]
    ) -> None:
        claim_type = _require_text(payload.get("claim_type"), "claim_type", maximum=64)
        if claim_type not in _CLAIM_TYPES:
            raise StorageError("CLAIM_PAYLOAD_INVALID", "claim type is unsupported")
        _require_text(payload.get("statement"), "claim statement", maximum=24000)
        subject_ids = _require_text_list(payload.get("subject_ids"), "claim subject_ids", minimum=1, maximum=64)
        for subject_id in subject_ids:
            subject = documents.get(subject_id)
            if subject is None or subject.get("kind") not in {"entity", "concept"}:
                raise StorageError("CLAIM_SUBJECT_INVALID", "claim subject must resolve to entity or concept")
        temporal = _require_mapping(payload.get("temporal_scope"), "claim temporal_scope")
        start = temporal.get("valid_from")
        end = temporal.get("valid_until")
        start_time = _parse_time(start, "claim temporal valid_from") if start is not None else None
        end_time = _parse_time(end, "claim temporal valid_until") if end is not None else None
        if start_time is not None and end_time is not None and end_time < start_time:
            raise StorageError("CLAIM_PAYLOAD_INVALID", "claim temporal scope is reversed")
        _require_text(payload.get("applicability"), "claim applicability", maximum=2000)
        _require_text(payload.get("facet_id"), "claim facet_id", maximum=256)
        if not self._claim_has_support(document, payload, documents):
            raise StorageError("CLAIM_SUPPORT_MISSING", "claim needs source or claim support, or explicit inference")

    def _claim_has_support(
        self, document: Mapping[str, Any], payload: Mapping[str, Any], documents: Mapping[str, Mapping[str, Any]]
    ) -> bool:
        if payload.get("inference") is True or payload.get("support") == "inference":
            return True
        for relation in document.get("relations", []):
            if not isinstance(relation, Mapping):
                continue
            target = documents.get(relation.get("target"))
            if target and target.get("kind") in {"source", "claim"} and relation.get("type") in {
                "cites",
                "derived_from",
                "supports",
                "refines",
            }:
                return True
        for evidence_id in document.get("verification", {}).get("evidence_ids", []):
            target = documents.get(evidence_id)
            if target and target.get("kind") in {"source", "claim"}:
                return True
        return False

    def _validate_synthesis(self, payload: Mapping[str, Any], documents: Mapping[str, Mapping[str, Any]]) -> None:
        synthesis_type = _require_text(payload.get("synthesis_type"), "synthesis_type", maximum=64)
        if synthesis_type not in _SYNTHESIS_TYPES:
            raise StorageError("SYNTHESIS_PAYLOAD_INVALID", "synthesis type is unsupported")
        _require_text(payload.get("topic"), "synthesis topic", maximum=1000)
        claim_ids = _require_text_list(payload.get("claim_ids"), "synthesis claim_ids", maximum=256)
        source_ids = _require_text_list(payload.get("source_ids"), "synthesis source_ids", maximum=256)
        if not claim_ids and not source_ids:
            raise StorageError("SYNTHESIS_PAYLOAD_INVALID", "synthesis must cover a claim or source")
        for claim_id in claim_ids:
            if documents.get(claim_id, {}).get("kind") != "claim":
                raise StorageError("SYNTHESIS_REFERENCE_INVALID", "synthesis claim ID is not a claim")
        for source_id in source_ids:
            if documents.get(source_id, {}).get("kind") != "source":
                raise StorageError("SYNTHESIS_REFERENCE_INVALID", "synthesis source ID is not a source")
        coverage = _require_mapping(payload.get("coverage"), "synthesis coverage")
        if coverage.get("status") not in _COVERAGE_STATES:
            raise StorageError("SYNTHESIS_PAYLOAD_INVALID", "synthesis coverage status is invalid")
        if coverage.get("reviewed_at") is not None:
            _parse_time(coverage.get("reviewed_at"), "synthesis reviewed_at")
        _require_text_list(payload.get("knowledge_gaps", ()), "synthesis knowledge_gaps", maximum=128)
        try:
            _require_text_list(payload.get("material_statements"), "material_statements", minimum=1, maximum=512)
        except StorageError as error:
            raise StorageError("CITATION_INCOMPLETE", "synthesis needs durable material statements") from error
        if not isinstance(payload.get("citation_map"), Mapping):
            raise StorageError("CITATION_INCOMPLETE", "synthesis needs a durable citation map")

    def _validate_relations(self, document: Mapping[str, Any], documents: Mapping[str, Mapping[str, Any]]) -> None:
        source_id = str(document.get("id", ""))
        source_kind = document.get("kind")
        provenance_ids = _document_provenance_ids(document)
        seen_relation_ids: set[str] = set()
        for relation in document.get("relations", []):
            if not isinstance(relation, Mapping):
                raise StorageError("RELATION_INVALID", "semantic relation must be an object")
            relation_id = relation.get("relation_id")
            if not isinstance(relation_id, str) or relation_id in seen_relation_ids:
                raise StorageError("RELATION_INVALID", "semantic relation ID is invalid or duplicated")
            seen_relation_ids.add(relation_id)
            relation_provenance = relation.get("provenance_ids")
            if (
                not isinstance(relation_provenance, (list, tuple))
                or not relation_provenance
                or not all(isinstance(item, str) and item in provenance_ids for item in relation_provenance)
            ):
                raise StorageError("RELATION_PROVENANCE_INVALID", "relation provenance must belong to its source object")
            relation_type = relation.get("type")
            target_id = relation.get("target")
            if relation_type not in _RELATION_TYPES or not isinstance(target_id, str):
                raise StorageError("RELATION_INVALID", "semantic relation type or target is invalid")
            target = documents.get(target_id)
            if target is None:
                raise StorageError("RELATION_TARGET_MISSING", "semantic relation target is absent")
            if source_id == target_id:
                raise StorageError("RELATION_INVALID", "self relations are not allowed")
            target_kind = target.get("kind")
            if relation_type == "cites" and target_kind != "source":
                raise StorageError("RELATION_KIND_INVALID", "cites must target a source")
            if relation_type == "derived_from" and target_kind not in {"source", "claim"}:
                raise StorageError("RELATION_KIND_INVALID", "derived_from must target source or claim")
            if relation_type in {"about", "mentions"} and target_kind not in {"entity", "concept"}:
                raise StorageError("RELATION_KIND_INVALID", "about and mentions must target entity or concept")
            if relation_type == "supports" and target_kind != "claim":
                raise StorageError("RELATION_KIND_INVALID", "supports must target a claim")
            if relation_type == "contradicts":
                if source_kind != "claim" or target_kind != "claim":
                    raise StorageError("RELATION_KIND_INVALID", "contradicts is only valid between claims")
                if source_id >= target_id:
                    raise StorageError("CONTRADICTION_NOT_CANONICAL", "write only lexical-low-to-high contradiction edge")
                if not isinstance(relation.get("scope"), str) or not relation["scope"].strip():
                    raise StorageError("CONTRADICTION_SCOPE_MISSING", "contradiction requires a nonempty scope")
            if relation_type == "refines" and source_kind != target_kind:
                raise StorageError("RELATION_KIND_INVALID", "refines requires matching semantic kinds")

    def _validate_synthesis_citations(
        self,
        documents: Mapping[str, Mapping[str, Any]],
        proposal: CompilationProposal,
        *,
        changed: set[str] | None = None,
        require_proposal_map: bool = True,
    ) -> None:
        for object_id, document in sorted(documents.items()):
            if document.get("kind") != "synthesis":
                continue
            payload = _require_mapping(document.get("payload"), "synthesis payload")
            citations = self._citation_map_for(
                object_id,
                payload,
                proposal,
                require_proposal_map=require_proposal_map and (changed is None or object_id in changed),
            )
            declared = _require_text_list(payload.get("material_statements"), "material_statements", minimum=1, maximum=512)
            if set(declared) != set(citations):
                raise StorageError("CITATION_INCOMPLETE", "citation map must cover each material statement exactly")
            allowed = set(payload.get("claim_ids", [])) | set(payload.get("source_ids", []))
            for statement, entry in citations.items():
                _require_text(statement, "citation statement", maximum=4096)
                support_ids, inference = _citation_support(entry)
                if not support_ids and not inference:
                    raise StorageError("CITATION_INCOMPLETE", "citation requires support IDs or explicit inference")
                for support_id in support_ids:
                    target = documents.get(support_id)
                    if target is None or target.get("kind") not in {"source", "claim"} or support_id not in allowed:
                        raise StorageError("CITATION_INVALID", "citation support must be an included source or claim")

    def _citation_map_for(
        self,
        synthesis_id: str,
        payload: Mapping[str, Any],
        proposal: CompilationProposal,
        *,
        require_proposal_map: bool,
    ) -> Mapping[str, Any]:
        durable = payload.get("citation_map")
        top_level = proposal.citation_map.get(synthesis_id)
        if durable is None:
            raise StorageError("CITATION_INCOMPLETE", "synthesis needs a durable citation map")
        durable_map = _require_mapping(durable, "synthesis citation_map")
        if require_proposal_map:
            if top_level is None or _plain(durable_map) != _plain(_require_mapping(top_level, "proposal citation map")):
                raise StorageError("CITATION_INCOMPLETE", "proposal and durable synthesis citation maps differ")
        elif top_level is not None and _plain(durable_map) != _plain(_require_mapping(top_level, "proposal citation map")):
            raise StorageError("CITATION_INCOMPLETE", "proposal and durable synthesis citation maps differ")
        return durable_map

    def _validate_contradiction_synthesis_visibility(
        self, documents: Mapping[str, Mapping[str, Any]], changed: set[str]
    ) -> None:
        pairs = {
            tuple(sorted((object_id, str(relation.get("target")))))
            for object_id, document in documents.items()
            if document.get("kind") == "claim"
            for relation in document.get("relations", [])
            if isinstance(relation, Mapping) and relation.get("type") == "contradicts" and isinstance(relation.get("target"), str)
        }
        for synthesis_id in changed:
            document = documents.get(synthesis_id)
            if not document or document.get("kind") != "synthesis":
                continue
            payload = _require_mapping(document.get("payload"), "synthesis payload")
            claim_ids = set(_require_text_list(payload.get("claim_ids"), "synthesis claim_ids", maximum=256))
            for left, right in pairs:
                if (left in claim_ids) != (right in claim_ids):
                    raise StorageError(
                        "CONTRADICTION_UNSURFACED",
                        "a synthesis that covers one contradiction side must cover both sides",
                    )

    def _proposal_diff(self, proposal: CompilationProposal, snapshot: StoreSnapshot) -> tuple[Mapping[str, Any], ...]:
        current = {item.id: item for item in snapshot.objects}
        diff: list[Mapping[str, Any]] = []
        for mutation in sorted(proposal.mutations, key=lambda item: item.object_id):
            desired = mutation.desired_object if isinstance(mutation.desired_object, Mapping) else {}
            previous = current.get(mutation.object_id)
            diff.append(
                {
                    "object_id": mutation.object_id,
                    "operation": mutation.operation,
                    "kind": desired.get("kind"),
                    "title": desired.get("title"),
                    "before_revision": previous.revision if previous else None,
                    "after_revision": desired.get("revision"),
                    "expected_revision": mutation.expected_revision,
                    "expected_content_hash": mutation.expected_content_hash,
                    "relation_targets": sorted(
                        relation.get("target")
                        for relation in desired.get("relations", [])
                        if isinstance(relation, Mapping) and isinstance(relation.get("target"), str)
                    ),
                }
            )
        return tuple(diff)

    def _semantic_lint(self, documents: Mapping[str, Mapping[str, Any]]) -> list[LintFinding]:
        findings: list[LintFinding] = []
        accepted_captures = self._accepted_capture_inventory()
        referenced_captures: set[str] = set()
        inbound: dict[str, int] = {object_id: 0 for object_id in documents}
        contradiction_pairs: list[tuple[str, str]] = []
        for object_id, document in sorted(documents.items()):
            try:
                payload = _require_mapping(document.get("payload"), "semantic payload")
                kind = document.get("kind")
                if kind == "source":
                    raw = _require_mapping(payload.get("raw"), "source raw binding")
                    capture_id = raw.get("capture_id")
                    if isinstance(capture_id, str):
                        referenced_captures.add(capture_id)
                    manifest = accepted_captures.get(capture_id)
                    if manifest is None:
                        findings.append(
                            LintFinding("warning", "SOURCE_UNCOMPILED", "source capture is not currently accepted", object_id)
                        )
                    else:
                        try:
                            self._validate_source(document, payload, _lint_proposal(), {capture_id: manifest}, False)
                        except StorageError as error:
                            findings.append(LintFinding("error", error.code, "source semantic binding is invalid", object_id))
                elif kind == "entity":
                    self._validate_entity(payload)
                elif kind == "concept":
                    self._validate_concept(payload)
                elif kind == "claim":
                    try:
                        self._validate_claim(document, payload, documents)
                    except StorageError as error:
                        severity = "warning" if error.code == "CLAIM_SUPPORT_MISSING" else "error"
                        code = "CLAIM_UNSUPPORTED" if error.code == "CLAIM_SUPPORT_MISSING" else error.code
                        findings.append(LintFinding(severity, code, "claim semantic support is incomplete", object_id))
                elif kind == "synthesis":
                    self._validate_synthesis(payload, documents)
                    citations = payload.get("citation_map")
                    if not isinstance(citations, Mapping):
                        findings.append(LintFinding("warning", "CITATION_INCOMPLETE", "synthesis lacks durable citation map", object_id))
                    else:
                        try:
                            self._validate_synthesis_citations(
                                documents,
                                _lint_proposal(),
                                require_proposal_map=False,
                            )
                        except StorageError as error:
                            findings.append(LintFinding("warning", error.code, "synthesis citations are incomplete", object_id))
                else:
                    findings.append(LintFinding("error", "SCHEMA_INVALID", "object kind is outside M3 scope", object_id))
            except StorageError as error:
                findings.append(LintFinding("error", error.code, "semantic payload is invalid", object_id))
            for relation in document.get("relations", []):
                if not isinstance(relation, Mapping):
                    continue
                target = relation.get("target")
                if isinstance(target, str) and target in inbound:
                    inbound[target] += 1
                if relation.get("type") == "related_to":
                    findings.append(
                        LintFinding("warning", "GENERIC_RELATION", "generic relation should be typed", object_id, relation.get("relation_id"))
                    )
                if relation.get("type") == "contradicts" and isinstance(target, str):
                    contradiction_pairs.append((object_id, target))

        for capture_id in sorted(accepted_captures):
            if capture_id not in referenced_captures:
                findings.append(LintFinding("warning", "SOURCE_UNCOMPILED", "accepted capture has no source object"))
        for object_id, document in sorted(documents.items()):
            kind = document.get("kind")
            outgoing = len(document.get("relations", []))
            if kind in {"entity", "concept", "synthesis"} and inbound.get(object_id, 0) == 0 and outgoing == 0:
                findings.append(LintFinding("warning", "ORPHAN_OBJECT", "semantic object has no graph connection", object_id))
        for left, right in contradiction_pairs:
            if not any(
                document.get("kind") == "synthesis"
                and isinstance(document.get("payload"), Mapping)
                and {left, right}.issubset(set(document["payload"].get("claim_ids", [])))
                for document in documents.values()
            ):
                findings.append(
                    LintFinding("warning", "CONTRADICTION_UNSURFACED", "contradiction is not covered by a synthesis", left)
                )
        findings.extend(_duplicate_findings(documents))
        return findings

    def _accepted_capture_inventory(self) -> dict[str, Mapping[str, Any]]:
        try:
            self._assert_capture_repository_identity()
            manifests = RawCaptureRepository.list_manifests(self.raw_captures)
        except (CaptureError, StorageError, ValueError) as error:
            raise StorageError("CAPTURE_INVALID", "raw capture inventory cannot be verified") from error
        result: dict[str, Mapping[str, Any]] = {}
        for manifest in manifests:
            if not isinstance(manifest, Mapping):
                raise StorageError("CAPTURE_INVALID", "raw capture inventory has an invalid manifest")
            try:
                validate_capture_manifest(manifest)
            except (CaptureError, ValueError) as error:
                raise StorageError("CAPTURE_INVALID", "raw capture inventory has an invalid manifest") from error
            capture_id = manifest.get("capture_id")
            scan = manifest.get("scan")
            if (
                isinstance(capture_id, str)
                and manifest.get("status") == "accepted"
                and isinstance(scan, Mapping)
                and scan.get("decision") == "accept"
            ):
                result[capture_id] = manifest
        return result

    def _derived_lint(self, snapshot: StoreSnapshot) -> list[LintFinding]:
        files, _view_digest = self._expected_view_files(snapshot)
        target = _safe_child(_safe_child(self._store_root, Path("derived")), Path("knowledge"))
        if not _directory_exists(target, self._store_root):
            return [LintFinding("warning", "MOC_DRIFT", "derived knowledge views are absent")]
        findings: list[LintFinding] = []
        for relative, expected in files.items():
            path = _safe_child(target, Path(relative))
            try:
                actual = _read_regular_file(path, self._store_root)
            except StorageError:
                findings.append(LintFinding("warning", "MOC_DRIFT", "derived knowledge view is missing or unsafe"))
                continue
            if actual != expected:
                code = "INDEX_SOURCE_MISMATCH" if relative == "manifest.json" else "MOC_DRIFT"
                findings.append(LintFinding("warning", code, "derived knowledge view does not match authority"))
        existing = _list_tree_files(target, self._store_root)
        if set(existing) != set(files):
            findings.append(LintFinding("warning", "MOC_DRIFT", "derived knowledge view inventory differs"))
        return findings

    def _expected_view_files(self, snapshot: StoreSnapshot) -> tuple[dict[str, bytes], str]:
        corpus_digest = _object_corpus_digest(snapshot)
        generated_at = _stable_generated_at(snapshot)
        metadata = {
            "generated": True,
            "generator": KNOWLEDGE_VIEWS_VERSION,
            "store_id": _GLOBAL_STORE_ID,
            "generated_at": generated_at,
            "generated_from_epoch": snapshot.mutation_epoch,
            "event_head": snapshot.event_head,
            "corpus_digest": corpus_digest,
            "do_not_edit": True,
        }
        objects = sorted(
            snapshot.objects,
            key=lambda item: (_normalized_title(item.document.get("title")), item.id),
        )
        active = [item for item in objects if item.document.get("lifecycle", {}).get("status") == "active"]
        relation_index, orphan_ids = _relation_projection(objects)
        files: dict[str, bytes] = {}
        files["index.md"] = _render_index(metadata, active, relation_index)
        kind_files = {
            "source": "moc-sources.md",
            "entity": "moc-entities.md",
            "concept": "moc-concepts.md",
            "claim": "moc-claims.md",
            "synthesis": "moc-syntheses.md",
        }
        for kind, name in kind_files.items():
            files[name] = _render_moc(metadata, kind, [item for item in active if item.document.get("kind") == kind])
        backlinks = {
            "schema_version": 1,
            "builder_version": KNOWLEDGE_VIEWS_VERSION,
            "source": {
                "mutation_epoch": snapshot.mutation_epoch,
                "event_head": snapshot.event_head,
                "corpus_digest": corpus_digest,
            },
            "by_object": relation_index,
        }
        files["backlinks.json"] = canonical_jcs_bytes(backlinks)
        files["orphans.json"] = canonical_jcs_bytes(
            {
                "schema_version": 1,
                "builder_version": KNOWLEDGE_VIEWS_VERSION,
                "source": {"mutation_epoch": snapshot.mutation_epoch, "corpus_digest": corpus_digest},
                "orphans": orphan_ids,
            }
        )
        files["log.md"] = _render_log(metadata, snapshot, active)
        view_digest = _tree_digest(files)
        manifest = {
            "schema_version": 1,
            "builder_version": KNOWLEDGE_VIEWS_VERSION,
            "store_id": _GLOBAL_STORE_ID,
            "source": {
                "mutation_epoch": snapshot.mutation_epoch,
                "event_head": snapshot.event_head,
                "corpus_digest": corpus_digest,
                "object_count": snapshot.object_count,
            },
            "view_digest": view_digest,
            "files": [
                {"path": name, "sha256": hashlib.sha256(content).hexdigest()}
                for name, content in sorted(files.items())
            ],
        }
        files["manifest.json"] = canonical_jcs_bytes(manifest)
        return files, view_digest


def _citation_support(value: Any) -> tuple[tuple[str, ...], bool]:
    if isinstance(value, str):
        if value == "inference":
            return (), True
        return (value,), False
    if isinstance(value, (list, tuple)):
        return _require_text_list(value, "citation support", minimum=1, maximum=128), False
    mapping = _require_mapping(value, "citation entry")
    raw_support = mapping.get("support_ids", mapping.get("supports", mapping.get("citations", ())))
    supports = _require_text_list(raw_support, "citation support", maximum=128)
    inference = mapping.get("inference", False)
    if not isinstance(inference, bool):
        raise StorageError("CITATION_INVALID", "citation inference label must be boolean")
    return supports, inference


def _record_set(value: Any) -> set[bytes]:
    if not isinstance(value, (list, tuple)):
        raise StorageError("SCHEMA_INVALID", "claim evidence records must be a list")
    try:
        return {canonical_jcs_bytes(_plain(item)) for item in value}
    except Exception as error:
        raise StorageError("SCHEMA_INVALID", "claim evidence records are invalid") from error


def _claim_relation_records(document: Mapping[str, Any], relation_types: set[str]) -> Counter[bytes]:
    """Return full evidence-bearing claim relations, retaining multiplicity."""

    relations = document.get("relations", ())
    if not isinstance(relations, (list, tuple)):
        raise StorageError("SCHEMA_INVALID", "claim relations must be a list")
    result: Counter[bytes] = Counter()
    for relation in relations:
        if not isinstance(relation, Mapping):
            raise StorageError("SCHEMA_INVALID", "claim relation is invalid")
        relation_type = relation.get("type")
        if relation_type not in relation_types:
            continue
        try:
            result[canonical_jcs_bytes(_plain(relation))] += 1
        except Exception as error:
            raise StorageError("SCHEMA_INVALID", "claim relation evidence is invalid") from error
    return result


def _records_preserved(existing: Counter[bytes], desired: Counter[bytes]) -> bool:
    return all(desired[record] >= count for record, count in existing.items())


def _document_provenance_ids(document: Mapping[str, Any]) -> set[str]:
    provenance = document.get("provenance")
    if not isinstance(provenance, (list, tuple)):
        raise StorageError("SCHEMA_INVALID", "semantic provenance must be a list")
    ids: set[str] = set()
    for record in provenance:
        if not isinstance(record, Mapping):
            raise StorageError("SCHEMA_INVALID", "semantic provenance record is invalid")
        provenance_id = record.get("provenance_id")
        if not isinstance(provenance_id, str) or provenance_id in ids:
            raise StorageError("SCHEMA_INVALID", "semantic provenance ID is invalid or duplicated")
        ids.add(provenance_id)
    return ids


def _verification_evidence_ids(document: Mapping[str, Any]) -> set[str]:
    verification = document.get("verification")
    if not isinstance(verification, Mapping):
        raise StorageError("SCHEMA_INVALID", "claim verification is invalid")
    evidence_ids = verification.get("evidence_ids", ())
    if not isinstance(evidence_ids, (list, tuple)) or not all(isinstance(item, str) for item in evidence_ids):
        raise StorageError("SCHEMA_INVALID", "claim verification evidence is invalid")
    return set(evidence_ids)


def _lint_proposal() -> CompilationProposal:
    """A no-mutation proposal only used to reuse durable-citation validation."""

    return CompilationProposal(
        proposal_id="proposal:00000000-0000-4000-8000-000000000000",
        compiler_version=KNOWLEDGE_COMPILER_VERSION,
        idempotency_key="lint-only",
        rationale="lint",
        capture_ids=(),
        citation_map={},
        mutations=(),
    )


def _duplicate_findings(documents: Mapping[str, Mapping[str, Any]]) -> list[LintFinding]:
    seen: dict[tuple[str, str], str] = {}
    findings: list[LintFinding] = []
    for object_id, document in sorted(documents.items()):
        payload = document.get("payload", {})
        if not isinstance(payload, Mapping) or document.get("kind") not in {"entity", "concept"}:
            continue
        value = payload.get("canonical_name") if document.get("kind") == "entity" else payload.get("definition")
        normalized = _normalized_title(value)
        if not normalized:
            continue
        key = (str(document.get("kind")), normalized)
        previous = seen.get(key)
        if previous is not None:
            findings.append(LintFinding("warning", "POSSIBLE_DUPLICATE", "semantic object resembles an earlier object", object_id))
        else:
            seen[key] = object_id
    return findings


def _stable_generated_at(snapshot: StoreSnapshot) -> str:
    values: list[datetime] = []
    for item in snapshot.objects:
        value = item.document.get("updated_at")
        try:
            values.append(_parse_time(value, "updated_at"))
        except StorageError:
            continue
    return _rfc3339(max(values)) if values else "1970-01-01T00:00:00Z"


def _frontmatter(metadata: Mapping[str, Any]) -> str:
    lines = ["---"]
    for key in (
        "generated",
        "generator",
        "store_id",
        "generated_at",
        "generated_from_epoch",
        "event_head",
        "corpus_digest",
        "do_not_edit",
    ):
        value = metadata.get(key)
        if value is True:
            rendered = "true"
        elif value is False:
            rendered = "false"
        elif value is None:
            rendered = "null"
        elif isinstance(value, int):
            rendered = str(value)
        else:
            rendered = json.dumps(str(value), ensure_ascii=False, separators=(",", ":"))
        lines.append(f"{key}: {rendered}")
    lines.append("---")
    return "\n".join(lines)


def _abstract(document: Mapping[str, Any]) -> str:
    body = " ".join(str(document.get("body", "")).split())
    if body:
        return body[:220]
    payload = document.get("payload", {})
    if isinstance(payload, Mapping):
        for field in ("statement", "definition", "topic", "canonical_name"):
            value = payload.get(field)
            if isinstance(value, str) and value.strip():
                return " ".join(value.split())[:220]
    return ""


def _render_index(metadata: Mapping[str, Any], objects: Sequence[Any], relations: Mapping[str, Any]) -> bytes:
    lines = [_frontmatter(metadata), "", "# Global Knowledge Index", ""]
    for item in objects:
        document = item.document
        relation_data = relations.get(item.id, {})
        inbound = len(relation_data.get("backlinks", [])) if isinstance(relation_data, Mapping) else 0
        outbound = len(document.get("relations", []))
        abstract = _abstract(document)
        lines.append(
            f"- [{document.get('kind')}] {document.get('title')} ({item.id}@{item.revision}) "
            f"status={document.get('lifecycle', {}).get('status')}; inbound={inbound}; outbound={outbound}"
        )
        if abstract:
            lines.append(f"  - {abstract}")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def _render_moc(metadata: Mapping[str, Any], kind: str, objects: Sequence[Any]) -> bytes:
    lines = [_frontmatter(metadata), "", f"# {kind.title()} MOC", ""]
    for item in objects:
        lines.append(f"- {item.document.get('title')} ({item.id}@{item.revision})")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def _render_log(metadata: Mapping[str, Any], snapshot: StoreSnapshot, active: Sequence[Any]) -> bytes:
    lines = [_frontmatter(metadata), "", "# Knowledge Event Log", ""]
    lines.append(
        f"- authoritative snapshot: epoch={snapshot.mutation_epoch}; event_head={snapshot.event_head or 'none'}; "
        f"active_objects={len(active)}"
    )
    lines.append("- This is a deterministic projection of the public authority snapshot, not an editable ledger.")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def _relation_projection(objects: Sequence[Any]) -> tuple[dict[str, Any], list[Mapping[str, Any]]]:
    by_object: dict[str, dict[str, Any]] = {
        item.id: {"backlinks": [], "contradictions": [], "outbound": 0} for item in objects
    }
    for item in objects:
        for relation in item.document.get("relations", []):
            if not isinstance(relation, Mapping):
                continue
            target = relation.get("target")
            if not isinstance(target, str) or target not in by_object:
                continue
            by_object[item.id]["outbound"] += 1
            entry = {
                "relation_id": relation.get("relation_id"),
                "type": relation.get("type"),
                "peer_id": item.id,
                "peer_revision": item.revision,
                "scope": relation.get("scope"),
            }
            by_object[target]["backlinks"].append(entry)
            if relation.get("type") == "contradicts":
                left = {
                    "relation_id": relation.get("relation_id"),
                    "peer_id": target,
                    "scope": relation.get("scope"),
                    "direction": "canonical_outgoing",
                }
                right = {
                    "relation_id": relation.get("relation_id"),
                    "peer_id": item.id,
                    "scope": relation.get("scope"),
                    "direction": "derived_inverse",
                }
                by_object[item.id]["contradictions"].append(left)
                by_object[target]["contradictions"].append(right)
    for item in by_object.values():
        item["backlinks"].sort(key=lambda entry: (str(entry.get("type")), str(entry.get("peer_id")), str(entry.get("relation_id"))))
        item["contradictions"].sort(key=lambda entry: (str(entry.get("peer_id")), str(entry.get("relation_id"))))
    orphans = [
        {"id": object_id, "reason": "no_relations"}
        for object_id, item in sorted(by_object.items())
        if not item["backlinks"] and not item["contradictions"] and item["outbound"] == 0
    ]
    return by_object, orphans


def _tree_digest(files: Mapping[str, bytes]) -> str:
    return sha256_hex(
        [
            {"path": path, "sha256": hashlib.sha256(content).hexdigest()}
            for path, content in sorted(files.items())
        ]
    )


def _repository_contained(path: Path) -> Path:
    root = repository_root().resolve()
    lexical = path if path.is_absolute() else Path.cwd() / path
    try:
        lexical.relative_to(root)
    except ValueError as error:
        raise StorageError("PATH_UNSAFE", "M3 state must remain inside this repository") from error
    resolved = lexical.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise StorageError("PATH_UNSAFE", "M3 state escapes this repository") from error
    return resolved


def _safe_child(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise StorageError("PATH_UNSAFE", "unsafe local state path")
    base = _repository_contained(root)
    return base / relative


def _nofollow_flag() -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise StorageError("PATH_UNSAFE", "platform cannot safely access derived paths")
    return os.O_NOFOLLOW


def _relative_under_boundary(path: Path, boundary: Path) -> Path:
    if not path.is_absolute() or not boundary.is_absolute():
        raise StorageError("PATH_UNSAFE", "local state path is unsafe")
    try:
        relative = path.relative_to(boundary)
    except ValueError as error:
        raise StorageError("PATH_UNSAFE", "local state path escapes boundary") from error
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise StorageError("PATH_UNSAFE", "local state path is unsafe")
    return relative


def _assert_secure_stat(metadata: os.stat_result, *, directory: bool, require_private: bool = True) -> None:
    mode = metadata.st_mode
    expected = stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
    if not expected or stat.S_ISLNK(mode):
        raise StorageError("PATH_UNSAFE", "local state path has an unsafe file type")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise StorageError("PATH_UNSAFE", "local state path is not owned by the current user")
    if require_private and stat.S_IMODE(mode) & 0o077:
        raise StorageError("PATH_UNSAFE", "local state path must be private to the current user")


@contextmanager
def _open_secure_parent_fd(path: Path, boundary: Path) -> Iterator[tuple[int, str]]:
    """Resolve a local file parent entirely through no-follow descriptors."""

    relative = _relative_under_boundary(path, boundary)
    if not relative.parts:
        raise StorageError("PATH_UNSAFE", "local root cannot be used as a file")
    try:
        descriptor = os.open(
            boundary,
            os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as error:
        raise StorageError("PATH_UNSAFE", "local state root cannot be opened safely") from error
    try:
        _assert_secure_stat(os.fstat(descriptor), directory=True)
        flags = os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0)
        for component in relative.parts[:-1]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as error:
                raise StorageError("PATH_UNSAFE", "local state parent cannot be opened safely") from error
            try:
                _assert_secure_stat(os.fstat(child), directory=True)
            except Exception:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        yield descriptor, relative.parts[-1]
    finally:
        os.close(descriptor)


def _ensure_directory(path: Path, boundary: Path, *, boundary_is_private: bool = True) -> None:
    """Create a private local directory without following any child symlink."""

    relative = _relative_under_boundary(path, boundary)
    try:
        descriptor = os.open(
            boundary,
            os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as error:
        raise StorageError("PATH_UNSAFE", "local state root cannot be opened safely") from error
    try:
        _assert_secure_stat(os.fstat(descriptor), directory=True, require_private=boundary_is_private)
        flags = os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0)
        for index, component in enumerate(relative.parts):
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    child = os.open(component, flags, dir_fd=descriptor)
                except OSError as error:
                    raise StorageError("PATH_UNSAFE", "local state directory cannot be created safely") from error
            except OSError as error:
                raise StorageError("PATH_UNSAFE", "local state directory cannot be opened safely") from error
            try:
                _assert_secure_stat(
                    os.fstat(child),
                    directory=True,
                    require_private=boundary_is_private or index == len(relative.parts) - 1,
                )
            except Exception:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)


@contextmanager
def _local_lock(path: Path, boundary: Path) -> Iterator[None]:
    _ensure_directory(path.parent, boundary)
    key = str(path)
    with _LOCAL_LOCKS_GUARD:
        lock = _LOCAL_LOCKS.setdefault(key, threading.RLock())
    with lock:
        with _open_secure_parent_fd(path, boundary) as (parent_fd, name):
            descriptor: int | None = None
            try:
                descriptor = os.open(
                    name,
                    os.O_CREAT | os.O_RDWR | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=parent_fd,
                )
                _assert_secure_stat(os.fstat(descriptor), directory=False)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            except OSError as error:
                raise StorageError("PATH_UNSAFE", "local lock cannot be opened safely") from error
            finally:
                if descriptor is not None:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    finally:
                        os.close(descriptor)


def _atomic_write(path: Path, content: bytes, boundary: Path) -> None:
    _ensure_directory(path.parent, boundary)
    if not isinstance(content, bytes):
        raise StorageError("PATH_UNSAFE", "local state content is invalid")
    with _open_secure_parent_fd(path, boundary) as (parent_fd, name):
        temporary = f".{name}.tmp-{uuid.uuid4().hex}"
        descriptor: int | None = None
        try:
            try:
                _assert_secure_stat(os.stat(name, dir_fd=parent_fd, follow_symlinks=False), directory=False)
            except FileNotFoundError:
                pass
            descriptor = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=parent_fd,
            )
            _assert_secure_stat(os.fstat(descriptor), directory=False)
            _write_all(descriptor, content)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError as error:
            raise StorageError("PATH_UNSAFE", "local state cannot be written safely") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _remove_tree(path: Path, boundary: Path) -> None:
    with _open_secure_parent_fd(path, boundary) as (parent_fd, name):
        _remove_tree_at(parent_fd, name)
        os.fsync(parent_fd)


def _write_tree_atomically(target: Path, files: Mapping[str, bytes], *, boundary: Path) -> None:
    parent = target.parent
    _ensure_directory(parent, boundary)
    lock_path = _safe_child(parent, Path(".knowledge.lock"))
    with _local_lock(lock_path, boundary):
        with _open_secure_parent_fd(target, boundary) as (parent_fd, target_name):
            stage_name = f".knowledge-stage-{uuid.uuid4().hex}"
            backup_name = f".knowledge-backup-{uuid.uuid4().hex}"
            moved_existing = False
            stage_created = False
            try:
                os.mkdir(stage_name, 0o700, dir_fd=parent_fd)
                stage_created = True
                stage_fd = os.open(
                    stage_name,
                    os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent_fd,
                )
                try:
                    _assert_secure_stat(os.fstat(stage_fd), directory=True)
                    for relative, content in sorted(files.items()):
                        _write_relative_file(stage_fd, relative, content)
                finally:
                    os.close(stage_fd)
                try:
                    existing = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    existing = None
                if existing is not None:
                    _assert_secure_stat(existing, directory=True)
                    os.replace(target_name, backup_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    moved_existing = True
                os.replace(stage_name, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                stage_created = False
                if moved_existing:
                    _remove_tree_at(parent_fd, backup_name)
                os.fsync(parent_fd)
            except OSError as error:
                if moved_existing and _entry_missing(parent_fd, target_name):
                    try:
                        os.replace(backup_name, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                        moved_existing = False
                    except OSError:
                        pass
                raise StorageError("PATH_UNSAFE", "derived views cannot be written safely") from error
            finally:
                if stage_created:
                    _remove_tree_at(parent_fd, stage_name)
                if moved_existing and _entry_missing(parent_fd, target_name):
                    try:
                        os.replace(backup_name, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    except OSError:
                        pass


def _read_regular_file(path: Path, boundary: Path) -> bytes:
    with _open_secure_parent_fd(path, boundary) as (parent_fd, name):
        try:
            descriptor = os.open(name, os.O_RDONLY | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0), dir_fd=parent_fd)
        except OSError as error:
            raise StorageError("PATH_UNSAFE", "local state file is unavailable") from error
        try:
            _assert_secure_stat(os.fstat(descriptor), directory=False)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 65_536)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        except OSError as error:
            raise StorageError("PATH_UNSAFE", "local state file cannot be read") from error
        finally:
            os.close(descriptor)


def _list_tree_files(root: Path, boundary: Path) -> tuple[str, ...]:
    with _open_secure_parent_fd(root, boundary) as (parent_fd, name):
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            return ()
        except OSError as error:
            raise StorageError("PATH_UNSAFE", "local state directory is unavailable") from error
        try:
            _assert_secure_stat(os.fstat(descriptor), directory=True)
            names: list[str] = []
            _list_tree_files_at(descriptor, "", names)
            return tuple(names)
        finally:
            os.close(descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short local state write")
        view = view[written:]


def _entry_missing(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError as error:
        raise StorageError("PATH_UNSAFE", "local state entry cannot be inspected") from error
    return False


def _remove_tree_at(parent_fd: int, name: str) -> None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        raise StorageError("PATH_UNSAFE", "local state tree cannot be opened safely") from error
    try:
        _assert_secure_stat(os.fstat(descriptor), directory=True)
        _remove_tree_contents(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except OSError as error:
        raise StorageError("PATH_UNSAFE", "local state tree cannot be removed safely") from error


def _remove_tree_contents(directory_fd: int) -> None:
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError as error:
        raise StorageError("PATH_UNSAFE", "local state tree cannot be listed safely") from error
    for name in names:
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise StorageError("PATH_UNSAFE", "local state tree entry cannot be inspected") from error
        if stat.S_ISDIR(metadata.st_mode):
            _assert_secure_stat(metadata, directory=True)
            _remove_tree_at(directory_fd, name)
            continue
        _assert_secure_stat(metadata, directory=False)
        try:
            os.unlink(name, dir_fd=directory_fd)
        except OSError as error:
            raise StorageError("PATH_UNSAFE", "local state file cannot be removed safely") from error
    try:
        os.fsync(directory_fd)
    except OSError as error:
        raise StorageError("PATH_UNSAFE", "local state tree cannot be synchronized") from error


def _write_relative_file(directory_fd: int, relative: str, content: bytes) -> None:
    if not isinstance(relative, str) or not isinstance(content, bytes):
        raise StorageError("PATH_UNSAFE", "derived file specification is unsafe")
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise StorageError("PATH_UNSAFE", "derived file specification is unsafe")
    descriptor = os.dup(directory_fd)
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0)
        for component in relative_path.parts[:-1]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    child = os.open(component, flags, dir_fd=descriptor)
                except OSError as error:
                    raise StorageError("PATH_UNSAFE", "derived directory cannot be created safely") from error
            except OSError as error:
                raise StorageError("PATH_UNSAFE", "derived directory cannot be opened safely") from error
            try:
                _assert_secure_stat(os.fstat(child), directory=True)
            except Exception:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        _write_file_at(descriptor, relative_path.parts[-1], content)
    finally:
        os.close(descriptor)


def _write_file_at(parent_fd: int, name: str, content: bytes) -> None:
    temporary = f".{name}.tmp-{uuid.uuid4().hex}"
    descriptor: int | None = None
    try:
        try:
            _assert_secure_stat(os.stat(name, dir_fd=parent_fd, follow_symlinks=False), directory=False)
        except FileNotFoundError:
            pass
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
        _assert_secure_stat(os.fstat(descriptor), directory=False)
        _write_all(descriptor, content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as error:
        raise StorageError("PATH_UNSAFE", "derived file cannot be written safely") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _list_tree_files_at(directory_fd: int, prefix: str, names: list[str]) -> None:
    try:
        entries = sorted(os.listdir(directory_fd))
    except OSError as error:
        raise StorageError("PATH_UNSAFE", "local state tree cannot be listed safely") from error
    for name in entries:
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise StorageError("PATH_UNSAFE", "local state tree entry cannot be inspected") from error
        relative = f"{prefix}{name}"
        if stat.S_ISDIR(metadata.st_mode):
            _assert_secure_stat(metadata, directory=True)
            try:
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory_fd,
                )
            except OSError as error:
                raise StorageError("PATH_UNSAFE", "local state tree directory cannot be opened safely") from error
            try:
                _assert_secure_stat(os.fstat(child), directory=True)
                _list_tree_files_at(child, f"{relative}/", names)
            finally:
                os.close(child)
        else:
            _assert_secure_stat(metadata, directory=False)
            names.append(relative)


def _directory_file_names(path: Path, boundary: Path) -> tuple[str, ...]:
    with _open_secure_parent_fd(path, boundary) as (parent_fd, name):
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_fd,
            )
        except OSError as error:
            raise StorageError("PATH_UNSAFE", "local state directory cannot be opened safely") from error
        try:
            _assert_secure_stat(os.fstat(descriptor), directory=True)
            result: list[str] = []
            for entry in sorted(os.listdir(descriptor)):
                try:
                    metadata = os.stat(entry, dir_fd=descriptor, follow_symlinks=False)
                except OSError as error:
                    raise StorageError("PATH_UNSAFE", "local state directory entry cannot be inspected") from error
                _assert_secure_stat(metadata, directory=False)
                result.append(entry)
            return tuple(result)
        finally:
            os.close(descriptor)


def _directory_exists(path: Path, boundary: Path) -> bool:
    with _open_secure_parent_fd(path, boundary) as (parent_fd, name):
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            return False
        except OSError as error:
            raise StorageError("PATH_UNSAFE", "local state directory is unavailable") from error
        try:
            _assert_secure_stat(os.fstat(descriptor), directory=True)
            return True
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class PromotionRequest:
    """A review request that deliberately has no global-accept operation."""

    project_store_id: str
    project_object_id: str
    revision: int
    content_hash: str
    target_kind: str
    provenance: Mapping[str, Any]
    idempotency_key: str
    proposed_by: str = "agent:promotion-requester"
    rationale: str = "Project evidence promotion requires review"
    proposal_id: str | None = None
    schema_version: int = 1

    @classmethod
    def from_value(cls, value: "PromotionRequest | Mapping[str, Any]") -> "PromotionRequest":
        if isinstance(value, cls):
            value = value.to_dict()
        if not isinstance(value, Mapping):
            raise StorageError("SCHEMA_INVALID", "promotion request must be an object")
        project_store_id = value.get("project_store_id", value.get("source_store"))
        project_object_id = value.get("project_object_id", value.get("source_object_id"))
        revision = value.get("revision", value.get("source_revision"))
        content_hash = value.get("content_hash", value.get("source_content_hash"))
        request = cls(
            project_store_id=_require_text(project_store_id, "project_store_id", maximum=128),
            project_object_id=_require_text(project_object_id, "project_object_id", maximum=256),
            revision=_require_int(revision, "project revision", minimum=1),
            content_hash=_require_hash(content_hash, "project content_hash"),
            target_kind=_require_text(value.get("target_kind"), "target_kind", maximum=64),
            provenance=deepcopy(dict(_require_mapping(value.get("provenance"), "promotion provenance"))),
            idempotency_key=_require_text(value.get("idempotency_key"), "promotion idempotency_key", maximum=1024),
            proposed_by=_require_text(value.get("proposed_by", "agent:promotion-requester"), "proposed_by", maximum=160),
            rationale=_require_text(
                value.get("rationale", "Project evidence promotion requires review"), "promotion rationale", maximum=2000
            ),
            proposal_id=_optional_text(value.get("proposal_id"), "promotion proposal_id", maximum=128),
            schema_version=_require_int(value.get("schema_version", 1), "promotion schema_version", minimum=1, maximum=1),
        )
        store_match = _PROJECT_STORE.fullmatch(request.project_store_id)
        object_match = _PROJECT_OBJECT.fullmatch(request.project_object_id)
        if store_match is None or object_match is None or store_match.group(1) != object_match.group(1):
            raise StorageError("SCHEMA_INVALID", "promotion source must be one coherent project object")
        if request.target_kind not in _KNOWLEDGE_KINDS:
            raise StorageError("SCHEMA_INVALID", "promotion target kind is not global knowledge")
        if request.proposal_id is not None and _UUID_ID.fullmatch(request.proposal_id) is None:
            raise StorageError("SCHEMA_INVALID", "promotion proposal_id is invalid")
        return request

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_store_id": self.project_store_id,
            "project_object_id": self.project_object_id,
            "revision": self.revision,
            "content_hash": self.content_hash,
            "target_kind": self.target_kind,
            "provenance": _plain(self.provenance),
            "idempotency_key": self.idempotency_key,
            "proposed_by": self.proposed_by,
            "rationale": self.rationale,
            "proposal_id": self.proposal_id,
        }


@dataclass(frozen=True)
class PromotionOutboxItem:
    """A persisted local request that remains pending until a later milestone."""

    outbox_id: str
    request: PromotionRequest
    status: str
    proposed_at: str

    def to_dict(self) -> dict[str, Any]:
        value = self.request.to_dict()
        value.update(
            {
                "outbox_id": self.outbox_id,
                "status": self.status,
                "proposed_at": self.proposed_at,
                "target_store": _GLOBAL_STORE_ID,
            }
        )
        return value

    @classmethod
    def from_value(cls, value: Mapping[str, Any]) -> "PromotionOutboxItem":
        if not isinstance(value, Mapping):
            raise StorageError("SCHEMA_INVALID", "promotion outbox item must be an object")
        item = cls(
            outbox_id=_require_text(value.get("outbox_id"), "outbox_id", maximum=128),
            request=PromotionRequest.from_value(value),
            status=_require_text(value.get("status"), "promotion status", maximum=64),
            proposed_at=_require_text(value.get("proposed_at"), "promotion proposed_at", maximum=64),
        )
        if _UUID_ID.fullmatch(item.outbox_id) is None or item.status != "pending_review":
            raise StorageError("SCHEMA_INVALID", "promotion outbox item is not pending review")
        _parse_time(item.proposed_at, "promotion proposed_at")
        if value.get("target_store") != _GLOBAL_STORE_ID:
            raise StorageError("SCHEMA_INVALID", "promotion outbox target is invalid")
        return item


class ProjectPromotionOutbox:
    """Persist project-to-global requests without any acceptance or writer path."""

    def __init__(self, root: str | Path, clock: Any = None) -> None:
        self.root = _repository_contained(Path(root))
        self.clock = clock
        _ensure_directory(self.root, repository_root().resolve(), boundary_is_private=False)
        self._outbox_root = _safe_child(self.root, Path("outbox/promotions"))
        _ensure_directory(self._outbox_root, self.root)

    def enqueue(self, request: PromotionRequest | Mapping[str, Any]) -> PromotionOutboxItem:
        normalized = PromotionRequest.from_value(request)
        lock_path = _safe_child(self._outbox_root, Path(".outbox.lock"))
        with _local_lock(lock_path, self.root):
            existing = self._find_by_key(normalized.idempotency_key)
            if existing is not None:
                if existing.request.to_dict() != normalized.to_dict():
                    raise StorageError("IDEMPOTENCY_KEY_REUSED", "promotion idempotency key is already bound")
                return existing
            outbox_id = normalized.proposal_id or f"promotion:{uuid.uuid5(uuid.NAMESPACE_URL, 'second-brain/m3/outbox/' + normalized.idempotency_key)}"
            item = PromotionOutboxItem(
                outbox_id=outbox_id,
                request=normalized,
                status="pending_review",
                proposed_at=_rfc3339(_utc_now(self.clock)),
            )
            filename = hashlib.sha256(normalized.idempotency_key.encode("utf-8")).hexdigest() + ".json"
            _atomic_write(_safe_child(self._outbox_root, Path(filename)), canonical_jcs_bytes(item.to_dict()), self.root)
            return item

    def list_pending(self) -> tuple[PromotionOutboxItem, ...]:
        items: list[PromotionOutboxItem] = []
        for name in _directory_file_names(self._outbox_root, self.root):
            if name == ".outbox.lock":
                continue
            if not name.endswith(".json"):
                raise StorageError("INTEGRITY_FAILED", "promotion outbox contains an unexpected item")
            path = _safe_child(self._outbox_root, Path(name))
            try:
                value = parse_strict_json(_read_regular_file(path, self.root).decode("utf-8"))
                item = PromotionOutboxItem.from_value(value)
            except (UnicodeDecodeError, StorageError) as error:
                raise StorageError("INTEGRITY_FAILED", "promotion outbox contains an invalid item") from error
            if item.status == "pending_review":
                items.append(item)
        return tuple(sorted(items, key=lambda item: (item.proposed_at, item.outbox_id)))

    def _find_by_key(self, idempotency_key: str) -> PromotionOutboxItem | None:
        for item in self.list_pending():
            if item.request.idempotency_key == idempotency_key:
                return item
        return None
