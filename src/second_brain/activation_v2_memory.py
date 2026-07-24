"""Synthetic assisted recovery and closure proposals for Activation V2 A3.

All records here are public synthetic fixtures in a disposable runtime. The
module deliberately models retrieval and reviewable proposals, not authority
stores: it has no ambient project discovery, no prompt field, and no commit
operation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import math
import re
import time
from typing import Any, Mapping, Sequence
import uuid

from .activation_v2 import activation_v2_logical_digest, validate_activation_v2_safe_content
from .activation_v2_runtime import (
    DisposableRuntime,
    disposable_json_exists,
    load_disposable_runtime,
    read_disposable_json,
    write_disposable_json,
)
from .canonical import sha256_hex
from .contracts import validate_named_document
from .errors import IntegrityError, SemanticValidationError
from .schema_validation import parse_rfc3339_utc


SYNTHETIC_RECOVERY_CORPUS_VERSION = 1
RECOVERY_PROPOSAL_SCHEMA_VERSION = 1
CLOSURE_PROPOSAL_SCHEMA_VERSION = 1
PROPOSAL_REVIEW_RECEIPT_VERSION = 1
MAX_RECOVERY_RESULTS = 12
MAX_RECOVERY_LATENCY_MS = 1_000
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_LABEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")
_PROJECT_OBJECT = re.compile(
    r"^mem:([a-z0-9][a-z0-9._-]{0,63}):(project|decision|component|task|bug|"
    r"experiment|evidence|question):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
_GLOBAL_OBJECT = re.compile(
    r"^kb:global:(source|entity|concept|claim|synthesis):[0-9a-f]{8}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_PROJECT_KINDS = frozenset(("project", "decision", "component", "task", "bug", "experiment", "evidence", "question"))
_GLOBAL_KINDS = frozenset(("source", "entity", "concept", "claim", "synthesis"))
_LIFECYCLES = frozenset(("active", "superseded", "archived", "deleted"))
_FRESHNESS = frozenset(("fresh", "partial", "stale", "unverifiable", "not_applicable"))
_VERIFICATION = frozenset(("unverified", "verified", "partial", "stale", "failed", "not_applicable"))
_REVIEW_DECISIONS = frozenset(("approved_for_later_transaction", "rejected"))


class ActivationV2MemoryError(SemanticValidationError):
    """An A3-only synthetic retrieval or proposal boundary is invalid."""


@dataclass(frozen=True)
class SyntheticRecoveryRecord:
    """A bounded public-synthetic retrieval record, never an authority object."""

    record_id: str
    scope: str
    project_id: str | None
    kind: str
    title: str
    labels: tuple[str, ...]
    lifecycle: str
    freshness: str
    verification: str
    revision: int
    record_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not SyntheticRecoveryRecord:
            raise ActivationV2MemoryError("synthetic recovery record is invalid")
        if self.scope not in {"project", "global"}:
            raise ActivationV2MemoryError("synthetic recovery scope is invalid")
        _validate_record_identity(self.record_id, self.scope, self.project_id, self.kind)
        _require_text(self.title, "synthetic recovery title", maximum=140)
        labels = _unique_labels(self.labels, "synthetic recovery labels", minimum=1, maximum=12)
        if self.lifecycle not in _LIFECYCLES or self.freshness not in _FRESHNESS or self.verification not in _VERIFICATION:
            raise ActivationV2MemoryError("synthetic recovery state is invalid")
        if type(self.revision) is not int or not 1 <= self.revision <= 2_147_483_647:
            raise ActivationV2MemoryError("synthetic recovery revision is invalid")
        expected = sha256_hex(self._digest_input())
        if self.record_digest and self.record_digest != expected:
            raise ActivationV2MemoryError("synthetic recovery record digest does not match")
        object.__setattr__(self, "labels", labels)
        object.__setattr__(self, "record_digest", expected)

    def _digest_input(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "scope": self.scope,
            "project_id": self.project_id,
            "kind": self.kind,
            "title": self.title,
            "labels": list(self.labels),
            "lifecycle": self.lifecycle,
            "freshness": self.freshness,
            "verification": self.verification,
            "revision": self.revision,
        }

    @classmethod
    def from_value(cls, value: object) -> "SyntheticRecoveryRecord":
        if isinstance(value, cls):
            return value
        expected = {
            "record_id",
            "scope",
            "project_id",
            "kind",
            "title",
            "labels",
            "lifecycle",
            "freshness",
            "verification",
            "revision",
            "record_digest",
        }
        if type(value) is not dict or set(value) != expected or not isinstance(value["labels"], list):
            raise ActivationV2MemoryError("synthetic recovery record has unsupported fields")
        try:
            return cls(
                record_id=value["record_id"],
                scope=value["scope"],
                project_id=value["project_id"],
                kind=value["kind"],
                title=value["title"],
                labels=tuple(value["labels"]),
                lifecycle=value["lifecycle"],
                freshness=value["freshness"],
                verification=value["verification"],
                revision=value["revision"],
                record_digest=value["record_digest"],
            )
        except (TypeError, ActivationV2MemoryError) as error:
            raise ActivationV2MemoryError("synthetic recovery record is invalid") from error

    def to_dict(self) -> dict[str, object]:
        return {**self._digest_input(), "record_digest": self.record_digest}


@dataclass(frozen=True)
class SyntheticRecoveryCorpus:
    """Exact synthetic corpus for one project; no project discovery is allowed."""

    corpus_version: int
    data_class: str
    project_id: str
    records: tuple[SyntheticRecoveryRecord, ...]
    corpus_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not SyntheticRecoveryCorpus:
            raise ActivationV2MemoryError("synthetic recovery corpus is invalid")
        if self.corpus_version != SYNTHETIC_RECOVERY_CORPUS_VERSION or self.data_class != "PUBLIC_SYNTHETIC":
            raise ActivationV2MemoryError("synthetic recovery corpus boundary is invalid")
        if not isinstance(self.project_id, str) or _PROJECT_ID.fullmatch(self.project_id) is None:
            raise ActivationV2MemoryError("synthetic recovery corpus project is invalid")
        if not isinstance(self.records, tuple) or not 1 <= len(self.records) <= 64:
            raise ActivationV2MemoryError("synthetic recovery corpus records are invalid")
        if any(type(record) is not SyntheticRecoveryRecord for record in self.records):
            raise ActivationV2MemoryError("synthetic recovery corpus records are invalid")
        if len({record.record_id for record in self.records}) != len(self.records):
            raise ActivationV2MemoryError("synthetic recovery corpus repeats a record")
        if any(record.scope == "project" and record.project_id != self.project_id for record in self.records):
            raise ActivationV2MemoryError("synthetic recovery corpus crosses a project boundary")
        expected = sha256_hex(self._digest_input())
        if self.corpus_digest and self.corpus_digest != expected:
            raise ActivationV2MemoryError("synthetic recovery corpus digest does not match")
        object.__setattr__(self, "corpus_digest", expected)

    def _digest_input(self) -> dict[str, object]:
        return {
            "corpus_version": self.corpus_version,
            "data_class": self.data_class,
            "project_id": self.project_id,
            "records": [record.to_dict() for record in self.records],
        }

    @classmethod
    def from_value(cls, value: object) -> "SyntheticRecoveryCorpus":
        if isinstance(value, cls):
            return value
        expected = {"corpus_version", "data_class", "project_id", "records", "corpus_digest"}
        if type(value) is not dict or set(value) != expected or not isinstance(value["records"], list):
            raise ActivationV2MemoryError("synthetic recovery corpus has unsupported fields")
        try:
            return cls(
                corpus_version=value["corpus_version"],
                data_class=value["data_class"],
                project_id=value["project_id"],
                records=tuple(SyntheticRecoveryRecord.from_value(item) for item in value["records"]),
                corpus_digest=value["corpus_digest"],
            )
        except (TypeError, ActivationV2MemoryError) as error:
            raise ActivationV2MemoryError("synthetic recovery corpus is invalid") from error

    def to_dict(self) -> dict[str, object]:
        return {**self._digest_input(), "corpus_digest": self.corpus_digest}


@dataclass(frozen=True)
class AssistedRecoveryRequest:
    """A label-only assisted request; raw task or prompt text is not accepted."""

    request_id: str
    project_id: str
    query_labels: tuple[str, ...]
    max_results: int
    include_global: bool

    def __post_init__(self) -> None:
        if type(self) is not AssistedRecoveryRequest:
            raise ActivationV2MemoryError("assisted recovery request is invalid")
        _require_prefixed_uuid(self.request_id, "recovery-request", "recovery request identity")
        if not isinstance(self.project_id, str) or _PROJECT_ID.fullmatch(self.project_id) is None:
            raise ActivationV2MemoryError("assisted recovery project is invalid")
        object.__setattr__(self, "query_labels", _unique_labels(self.query_labels, "recovery query labels", minimum=1, maximum=8))
        if type(self.max_results) is not int or not 1 <= self.max_results <= MAX_RECOVERY_RESULTS:
            raise ActivationV2MemoryError("recovery result budget is invalid")
        if type(self.include_global) is not bool:
            raise ActivationV2MemoryError("global recovery choice is invalid")

    @classmethod
    def from_value(cls, value: object) -> "AssistedRecoveryRequest":
        if isinstance(value, cls):
            return value
        expected = {"request_id", "project_id", "query_labels", "max_results", "include_global"}
        if type(value) is not dict or set(value) != expected or not isinstance(value["query_labels"], list):
            raise ActivationV2MemoryError("assisted recovery request has unsupported fields")
        try:
            return cls(
                request_id=value["request_id"],
                project_id=value["project_id"],
                query_labels=tuple(value["query_labels"]),
                max_results=value["max_results"],
                include_global=value["include_global"],
            )
        except (TypeError, ActivationV2MemoryError) as error:
            raise ActivationV2MemoryError("assisted recovery request is invalid") from error

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "project_id": self.project_id,
            "query_labels": list(self.query_labels),
            "max_results": self.max_results,
            "include_global": self.include_global,
        }


@dataclass(frozen=True)
class RecoverySelector:
    """A bounded reference to a synthetic record, including freshness warning."""

    record_id: str
    revision: int
    scope: str
    title: str
    freshness: str
    verification: str
    score: int

    def __post_init__(self) -> None:
        if type(self) is not RecoverySelector:
            raise ActivationV2MemoryError("recovery selector is invalid")
        if self.scope not in {"project", "global"}:
            raise ActivationV2MemoryError("recovery selector scope is invalid")
        _validate_record_identity(self.record_id, self.scope, None, None)
        if type(self.revision) is not int or self.revision < 1:
            raise ActivationV2MemoryError("recovery selector revision is invalid")
        _require_text(self.title, "recovery selector title", maximum=140)
        if self.freshness not in _FRESHNESS or self.verification not in _VERIFICATION:
            raise ActivationV2MemoryError("recovery selector state is invalid")
        if type(self.score) is not int or not 1 <= self.score <= 1_000:
            raise ActivationV2MemoryError("recovery selector score is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "revision": self.revision,
            "scope": self.scope,
            "title": self.title,
            "freshness": self.freshness,
            "verification": self.verification,
            "score": self.score,
        }

    @classmethod
    def from_value(cls, value: object) -> "RecoverySelector":
        expected = {"record_id", "revision", "scope", "title", "freshness", "verification", "score"}
        if type(value) is not dict or set(value) != expected:
            raise ActivationV2MemoryError("recovery selector has unsupported fields")
        try:
            return cls(**value)
        except (TypeError, ActivationV2MemoryError) as error:
            raise ActivationV2MemoryError("recovery selector is invalid") from error


@dataclass(frozen=True)
class RecoveryProposal:
    """A persisted assisted-read proposal with inclusion and omission evidence."""

    schema_version: int
    proposal_id: str
    request_id: str
    project_id: str
    created_at: str
    corpus_digest: str
    included: tuple[RecoverySelector, ...]
    relevant_but_omitted: tuple[RecoverySelector, ...]
    latency_ms: int
    proposal_mode: str
    authority_write: bool
    global_write: bool
    proposal_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not RecoveryProposal:
            raise ActivationV2MemoryError("recovery proposal is invalid")
        if self.schema_version != RECOVERY_PROPOSAL_SCHEMA_VERSION or isinstance(self.schema_version, bool):
            raise ActivationV2MemoryError("recovery proposal version is unsupported")
        _require_prefixed_uuid(self.proposal_id, "recovery-proposal", "recovery proposal identity")
        _require_prefixed_uuid(self.request_id, "recovery-request", "recovery proposal request")
        if not isinstance(self.project_id, str) or _PROJECT_ID.fullmatch(self.project_id) is None:
            raise ActivationV2MemoryError("recovery proposal project is invalid")
        _require_timestamp(self.created_at, "recovery proposal timestamp")
        _require_hash(self.corpus_digest, "recovery proposal corpus digest")
        _validate_selectors(
            self.included,
            "recovery proposal included",
            project_id=self.project_id,
            minimum=0,
            maximum=MAX_RECOVERY_RESULTS,
        )
        _validate_selectors(
            self.relevant_but_omitted,
            "recovery proposal omissions",
            project_id=self.project_id,
            minimum=0,
            maximum=64,
        )
        if {item.record_id for item in self.included} & {item.record_id for item in self.relevant_but_omitted}:
            raise ActivationV2MemoryError("recovery proposal repeats a selected record")
        if type(self.latency_ms) is not int or not 0 <= self.latency_ms <= MAX_RECOVERY_LATENCY_MS:
            raise ActivationV2MemoryError("recovery proposal latency exceeds its local budget")
        if self.proposal_mode != "assisted_read_proposal" or self.authority_write is not False or self.global_write is not False:
            raise ActivationV2MemoryError("recovery proposal exceeds proposal-only authority")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "proposal_digest")
        if self.proposal_digest and self.proposal_digest != expected:
            raise ActivationV2MemoryError("recovery proposal digest does not match")
        object.__setattr__(self, "proposal_digest", expected)

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "request_id": self.request_id,
            "project_id": self.project_id,
            "created_at": self.created_at,
            "corpus_digest": self.corpus_digest,
            "included": [item.to_dict() for item in self.included],
            "relevant_but_omitted": [item.to_dict() for item in self.relevant_but_omitted],
            "latency_ms": self.latency_ms,
            "proposal_mode": self.proposal_mode,
            "authority_write": self.authority_write,
            "global_write": self.global_write,
        }
        if include_digest:
            value["proposal_digest"] = self.proposal_digest
        return value

    @classmethod
    def from_value(cls, value: object) -> "RecoveryProposal":
        expected = {
            "schema_version",
            "proposal_id",
            "request_id",
            "project_id",
            "created_at",
            "corpus_digest",
            "included",
            "relevant_but_omitted",
            "latency_ms",
            "proposal_mode",
            "authority_write",
            "global_write",
            "proposal_digest",
        }
        if (
            type(value) is not dict
            or set(value) != expected
            or not isinstance(value["included"], list)
            or not isinstance(value["relevant_but_omitted"], list)
        ):
            raise ActivationV2MemoryError("recovery proposal has unsupported fields")
        try:
            return cls(
                schema_version=value["schema_version"],
                proposal_id=value["proposal_id"],
                request_id=value["request_id"],
                project_id=value["project_id"],
                created_at=value["created_at"],
                corpus_digest=value["corpus_digest"],
                included=tuple(RecoverySelector.from_value(item) for item in value["included"]),
                relevant_but_omitted=tuple(
                    RecoverySelector.from_value(item) for item in value["relevant_but_omitted"]
                ),
                latency_ms=value["latency_ms"],
                proposal_mode=value["proposal_mode"],
                authority_write=value["authority_write"],
                global_write=value["global_write"],
                proposal_digest=value["proposal_digest"],
            )
        except (TypeError, ActivationV2MemoryError) as error:
            raise ActivationV2MemoryError("recovery proposal is invalid") from error


@dataclass(frozen=True)
class ClosureProposal:
    """Digest-bound TaskClosure classification with no project/global commit."""

    schema_version: int
    proposal_id: str
    closure_id: str
    closure_digest: str
    project_id: str
    created_at: str
    capture_mode: str
    candidate_counts: Mapping[str, int]
    project_write_state: str
    global_outbox_state: str
    authority_write: bool
    global_write: bool
    proposal_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not ClosureProposal:
            raise ActivationV2MemoryError("closure proposal is invalid")
        if self.schema_version != CLOSURE_PROPOSAL_SCHEMA_VERSION or isinstance(self.schema_version, bool):
            raise ActivationV2MemoryError("closure proposal version is unsupported")
        _require_prefixed_uuid(self.proposal_id, "closure-proposal", "closure proposal identity")
        _require_prefixed_uuid(self.closure_id, "closure", "closure proposal closure")
        _require_hash(self.closure_digest, "closure proposal closure digest")
        if not isinstance(self.project_id, str) or _PROJECT_ID.fullmatch(self.project_id) is None:
            raise ActivationV2MemoryError("closure proposal project is invalid")
        _require_timestamp(self.created_at, "closure proposal timestamp")
        if self.capture_mode != "ASSISTED":
            raise ActivationV2MemoryError("A3 closure proposal must remain assisted")
        expected_counts = {"decisions", "evidence", "open_tasks", "questions", "global_candidates"}
        if type(self.candidate_counts) is not dict or set(self.candidate_counts) != expected_counts:
            raise ActivationV2MemoryError("closure proposal candidate counts are invalid")
        if any(type(value) is not int or value < 0 or value > 32 for value in self.candidate_counts.values()):
            raise ActivationV2MemoryError("closure proposal candidate counts are invalid")
        if self.project_write_state != "pending_review" or self.global_outbox_state != "pending_review":
            raise ActivationV2MemoryError("closure proposal review state is invalid")
        if self.authority_write is not False or self.global_write is not False:
            raise ActivationV2MemoryError("closure proposal exceeds proposal-only authority")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "proposal_digest")
        if self.proposal_digest and self.proposal_digest != expected:
            raise ActivationV2MemoryError("closure proposal digest does not match")
        object.__setattr__(self, "proposal_digest", expected)

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "closure_id": self.closure_id,
            "closure_digest": self.closure_digest,
            "project_id": self.project_id,
            "created_at": self.created_at,
            "capture_mode": self.capture_mode,
            "candidate_counts": dict(self.candidate_counts),
            "project_write_state": self.project_write_state,
            "global_outbox_state": self.global_outbox_state,
            "authority_write": self.authority_write,
            "global_write": self.global_write,
        }
        if include_digest:
            value["proposal_digest"] = self.proposal_digest
        return value

    @classmethod
    def from_value(cls, value: object) -> "ClosureProposal":
        if isinstance(value, cls):
            return value
        expected = {
            "schema_version",
            "proposal_id",
            "closure_id",
            "closure_digest",
            "project_id",
            "created_at",
            "capture_mode",
            "candidate_counts",
            "project_write_state",
            "global_outbox_state",
            "authority_write",
            "global_write",
            "proposal_digest",
        }
        if type(value) is not dict or set(value) != expected:
            raise ActivationV2MemoryError("closure proposal has unsupported fields")
        try:
            return cls(**value)
        except (TypeError, ActivationV2MemoryError) as error:
            raise ActivationV2MemoryError("closure proposal is invalid") from error


@dataclass(frozen=True)
class ProposalReviewReceipt:
    """A3 review acknowledgement; it cannot authorize a transaction."""

    schema_version: int
    review_id: str
    proposal_id: str
    project_id: str
    reviewed_at: str
    decision: str
    reviewer_kind: str
    authority_write: bool
    global_write: bool
    receipt_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not ProposalReviewReceipt:
            raise ActivationV2MemoryError("proposal review receipt is invalid")
        if self.schema_version != PROPOSAL_REVIEW_RECEIPT_VERSION or isinstance(self.schema_version, bool):
            raise ActivationV2MemoryError("proposal review receipt version is unsupported")
        _require_prefixed_uuid(self.review_id, "proposal-review", "proposal review identity")
        _require_prefixed_uuid(self.proposal_id, "closure-proposal", "proposal review proposal")
        if not isinstance(self.project_id, str) or _PROJECT_ID.fullmatch(self.project_id) is None:
            raise ActivationV2MemoryError("proposal review project is invalid")
        _require_timestamp(self.reviewed_at, "proposal review timestamp")
        if self.decision not in _REVIEW_DECISIONS or self.reviewer_kind != "synthetic_fixture":
            raise ActivationV2MemoryError("proposal review decision is invalid")
        if self.authority_write is not False or self.global_write is not False:
            raise ActivationV2MemoryError("proposal review exceeds A3 authority")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "receipt_digest")
        if self.receipt_digest and self.receipt_digest != expected:
            raise ActivationV2MemoryError("proposal review receipt digest does not match")
        object.__setattr__(self, "receipt_digest", expected)

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "review_id": self.review_id,
            "proposal_id": self.proposal_id,
            "project_id": self.project_id,
            "reviewed_at": self.reviewed_at,
            "decision": self.decision,
            "reviewer_kind": self.reviewer_kind,
            "authority_write": self.authority_write,
            "global_write": self.global_write,
        }
        if include_digest:
            value["receipt_digest"] = self.receipt_digest
        return value

    @classmethod
    def from_value(cls, value: object) -> "ProposalReviewReceipt":
        if isinstance(value, cls):
            return value
        expected = {
            "schema_version",
            "review_id",
            "proposal_id",
            "project_id",
            "reviewed_at",
            "decision",
            "reviewer_kind",
            "authority_write",
            "global_write",
            "receipt_digest",
        }
        if type(value) is not dict or set(value) != expected:
            raise ActivationV2MemoryError("proposal review receipt has unsupported fields")
        try:
            return cls(**value)
        except (TypeError, ActivationV2MemoryError) as error:
            raise ActivationV2MemoryError("proposal review receipt is invalid") from error


def seed_synthetic_recovery_corpus(
    runtime: DisposableRuntime,
    corpus: SyntheticRecoveryCorpus | Mapping[str, Any],
) -> SyntheticRecoveryCorpus:
    """Store one explicit public synthetic corpus under its exact project ID."""

    handle = load_disposable_runtime(runtime.root)
    handle.manifest.policy.require_resolved("A3")
    selected = SyntheticRecoveryCorpus.from_value(corpus)
    relative = _corpus_path(selected.project_id)
    if disposable_json_exists(handle, relative):
        raise ActivationV2MemoryError("synthetic recovery corpus already exists")
    write_disposable_json(handle, relative, selected.to_dict())
    return selected


def assisted_recovery(
    runtime: DisposableRuntime,
    request: AssistedRecoveryRequest | Mapping[str, Any],
    *,
    proposal_id: str | None = None,
    created_at: str | None = None,
) -> RecoveryProposal:
    """Return a bounded proposal from an exact synthetic project corpus only."""

    handle = load_disposable_runtime(runtime.root)
    handle.manifest.policy.require_resolved("A3")
    selected_request = AssistedRecoveryRequest.from_value(request)
    corpus = _load_corpus(handle, selected_request.project_id)
    started = time.monotonic()
    candidates = [
        record
        for record in corpus.records
        if record.scope == "project" or selected_request.include_global
    ]
    ranked = _rank_records(candidates, selected_request.query_labels)
    included_records = ranked[: selected_request.max_results]
    omitted_records = ranked[selected_request.max_results :]
    latency_ms = math.ceil((time.monotonic() - started) * 1_000)
    if latency_ms > MAX_RECOVERY_LATENCY_MS:
        raise ActivationV2MemoryError("assisted recovery exceeds its local latency budget")
    result = RecoveryProposal(
        schema_version=RECOVERY_PROPOSAL_SCHEMA_VERSION,
        proposal_id=proposal_id or f"recovery-proposal:{uuid.uuid4()}",
        request_id=selected_request.request_id,
        project_id=selected_request.project_id,
        created_at=created_at or _now_rfc3339(),
        corpus_digest=corpus.corpus_digest,
        included=tuple(_selector(record, score) for score, record in included_records),
        relevant_but_omitted=tuple(_selector(record, score) for score, record in omitted_records),
        latency_ms=latency_ms,
        proposal_mode="assisted_read_proposal",
        authority_write=False,
        global_write=False,
    )
    relative = _recovery_proposal_path(result.proposal_id)
    if disposable_json_exists(handle, relative):
        raise ActivationV2MemoryError("recovery proposal identity already exists")
    write_disposable_json(handle, relative, result.to_dict())
    return result


def propose_task_closure(
    runtime: DisposableRuntime,
    closure: Mapping[str, Any],
    *,
    proposal_id: str | None = None,
    created_at: str | None = None,
) -> ClosureProposal:
    """Validate a bounded TaskClosure and create only a pending proposal."""

    handle = load_disposable_runtime(runtime.root)
    handle.manifest.policy.require_resolved("A3")
    if handle.manifest.policy.capture_default != "ASSISTED":
        raise ActivationV2MemoryError("A3 closure proposals require an assisted fixture policy")
    if type(closure) is not dict:
        raise ActivationV2MemoryError("task closure is invalid")
    validate_named_document("activation-v2-task-closure-v1", closure)
    candidate_counts = {
        "decisions": len(closure["decisions"]),
        "evidence": len(closure["evidence"]),
        "open_tasks": len(closure["open_tasks"]),
        "questions": len(closure["questions"]),
        "global_candidates": len(closure["global_candidates"]),
    }
    result = ClosureProposal(
        schema_version=CLOSURE_PROPOSAL_SCHEMA_VERSION,
        proposal_id=proposal_id or f"closure-proposal:{uuid.uuid4()}",
        closure_id=closure["closure_id"],
        closure_digest=sha256_hex(closure),
        project_id=closure["project_id"],
        created_at=created_at or _now_rfc3339(),
        capture_mode="ASSISTED",
        candidate_counts=candidate_counts,
        project_write_state="pending_review",
        global_outbox_state="pending_review",
        authority_write=False,
        global_write=False,
    )
    relative = _closure_proposal_path(result.proposal_id)
    if disposable_json_exists(handle, relative):
        raise ActivationV2MemoryError("closure proposal identity already exists")
    write_disposable_json(handle, relative, result.to_dict())
    return result


def review_closure_proposal(
    runtime: DisposableRuntime,
    proposal: ClosureProposal | Mapping[str, Any],
    *,
    decision: str,
    review_id: str | None = None,
    reviewed_at: str | None = None,
) -> ProposalReviewReceipt:
    """Record a synthetic review result without changing a project/global store."""

    handle = load_disposable_runtime(runtime.root)
    handle.manifest.policy.require_resolved("A3")
    selected = ClosureProposal.from_value(proposal)
    _require_closure_proposal_present(handle, selected)
    result = ProposalReviewReceipt(
        schema_version=PROPOSAL_REVIEW_RECEIPT_VERSION,
        review_id=review_id or f"proposal-review:{uuid.uuid4()}",
        proposal_id=selected.proposal_id,
        project_id=selected.project_id,
        reviewed_at=reviewed_at or _now_rfc3339(),
        decision=decision,
        reviewer_kind="synthetic_fixture",
        authority_write=False,
        global_write=False,
    )
    relative = _proposal_review_path(result.review_id)
    if disposable_json_exists(handle, relative):
        raise ActivationV2MemoryError("proposal review identity already exists")
    write_disposable_json(handle, relative, result.to_dict())
    return result


def load_recovery_proposal(runtime: DisposableRuntime, proposal_id: str) -> RecoveryProposal:
    _require_prefixed_uuid(proposal_id, "recovery-proposal", "recovery proposal identity")
    try:
        return RecoveryProposal.from_value(read_disposable_json(runtime, _recovery_proposal_path(proposal_id)))
    except (IntegrityError, ActivationV2MemoryError) as error:
        raise IntegrityError("recovery proposal is unavailable or invalid") from error


def load_closure_proposal(runtime: DisposableRuntime, proposal_id: str) -> ClosureProposal:
    _require_prefixed_uuid(proposal_id, "closure-proposal", "closure proposal identity")
    try:
        return ClosureProposal.from_value(read_disposable_json(runtime, _closure_proposal_path(proposal_id)))
    except (IntegrityError, ActivationV2MemoryError) as error:
        raise IntegrityError("closure proposal is unavailable or invalid") from error


def _load_corpus(runtime: DisposableRuntime, project_id: str) -> SyntheticRecoveryCorpus:
    try:
        corpus = SyntheticRecoveryCorpus.from_value(read_disposable_json(runtime, _corpus_path(project_id)))
    except (IntegrityError, ActivationV2MemoryError) as error:
        raise IntegrityError("synthetic recovery corpus is unavailable or invalid") from error
    if corpus.project_id != project_id:
        raise IntegrityError("synthetic recovery corpus crosses a project boundary")
    return corpus


def _rank_records(
    records: Sequence[SyntheticRecoveryRecord], query_labels: tuple[str, ...]
) -> list[tuple[int, SyntheticRecoveryRecord]]:
    query = set(query_labels)
    ranked: list[tuple[int, SyntheticRecoveryRecord]] = []
    for record in records:
        if record.lifecycle != "active":
            continue
        overlap = len(query & set(record.labels))
        if not overlap:
            continue
        score = overlap * 100
        if record.scope == "project":
            score += 20
        if record.verification == "verified":
            score += 10
        if record.freshness == "fresh":
            score += 5
        elif record.freshness in {"partial", "stale", "unverifiable"}:
            score -= 1
        ranked.append((score, record))
    return sorted(ranked, key=lambda item: (-item[0], item[1].record_id))


def _selector(record: SyntheticRecoveryRecord, score: int) -> RecoverySelector:
    return RecoverySelector(
        record_id=record.record_id,
        revision=record.revision,
        scope=record.scope,
        title=record.title,
        freshness=record.freshness,
        verification=record.verification,
        score=score,
    )


def _validate_record_identity(record_id: object, scope: object, project_id: object, kind: object) -> None:
    if not isinstance(record_id, str):
        raise ActivationV2MemoryError("synthetic recovery record identity is invalid")
    project_match = _PROJECT_OBJECT.fullmatch(record_id)
    global_match = _GLOBAL_OBJECT.fullmatch(record_id)
    if scope == "project":
        if project_match is None:
            raise ActivationV2MemoryError("synthetic project record identity is invalid")
        if project_id is not None and project_match.group(1) != project_id:
            raise ActivationV2MemoryError("synthetic project record crosses a project boundary")
        if kind is not None and (kind not in _PROJECT_KINDS or project_match.group(2) != kind):
            raise ActivationV2MemoryError("synthetic project record kind is invalid")
    elif scope == "global":
        if global_match is None:
            raise ActivationV2MemoryError("synthetic global record identity is invalid")
        if project_id is not None:
            raise ActivationV2MemoryError("synthetic global record has a project identity")
        if kind is not None and (kind not in _GLOBAL_KINDS or global_match.group(1) != kind):
            raise ActivationV2MemoryError("synthetic global record kind is invalid")
    else:
        raise ActivationV2MemoryError("synthetic recovery record scope is invalid")


def _validate_selectors(
    value: object,
    label: str,
    *,
    project_id: str,
    minimum: int,
    maximum: int,
) -> None:
    if not isinstance(value, tuple) or not minimum <= len(value) <= maximum:
        raise ActivationV2MemoryError(f"{label} are invalid")
    if any(type(item) is not RecoverySelector for item in value):
        raise ActivationV2MemoryError(f"{label} are invalid")
    if len({item.record_id for item in value}) != len(value):
        raise ActivationV2MemoryError(f"{label} repeat a record")
    for item in value:
        _validate_record_identity(
            item.record_id,
            item.scope,
            project_id if item.scope == "project" else None,
            None,
        )


def _unique_labels(value: object, label: str, *, minimum: int, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not minimum <= len(value) <= maximum:
        raise ActivationV2MemoryError(f"{label} are invalid")
    if any(not isinstance(item, str) or _LABEL.fullmatch(item) is None for item in value):
        raise ActivationV2MemoryError(f"{label} are invalid")
    if len(set(value)) != len(value):
        raise ActivationV2MemoryError(f"{label} repeat a value")
    return value


def _require_text(value: object, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or "\x00" in value or "\n" in value or "\r" in value:
        raise ActivationV2MemoryError(f"{label} is invalid")
    validate_activation_v2_safe_content(value)
    return value


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ActivationV2MemoryError(f"{label} is invalid")
    return value


def _require_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ActivationV2MemoryError(f"{label} is invalid")
    try:
        parse_rfc3339_utc(value)
    except Exception as error:
        raise ActivationV2MemoryError(f"{label} is invalid") from error
    return value


def _require_prefixed_uuid(value: object, prefix: str, label: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix + ":"):
        raise ActivationV2MemoryError(f"{label} is invalid")
    if _UUID.fullmatch(value.removeprefix(prefix + ":")) is None:
        raise ActivationV2MemoryError(f"{label} is invalid")
    return value


def _corpus_path(project_id: str) -> str:
    return f"projects/{project_id}/recovery/a3-corpus.json"


def _recovery_proposal_path(proposal_id: str) -> str:
    return f"receipts/recovery/{proposal_id.removeprefix('recovery-proposal:')}.json"


def _closure_proposal_path(proposal_id: str) -> str:
    return f"outbox/closure-proposals/{proposal_id.removeprefix('closure-proposal:')}.json"


def _proposal_review_path(review_id: str) -> str:
    return f"receipts/proposal-reviews/{review_id.removeprefix('proposal-review:')}.json"


def _require_closure_proposal_present(runtime: DisposableRuntime, proposal: ClosureProposal) -> None:
    persisted = load_closure_proposal(runtime, proposal.proposal_id)
    if persisted.to_dict() != proposal.to_dict():
        raise IntegrityError("closure proposal differs from its persisted review target")


def _now_rfc3339() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
