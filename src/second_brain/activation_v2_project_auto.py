"""Disposable synthetic PROJECT_AUTO transactions for Activation V2 A4.

This module intentionally models the transaction boundary without opening an
authority store. It writes only bounded identifiers, digests, counts, and state
transitions into an ignored disposable runtime. Real project/global memory,
automatic capture, and global activation remain outside this phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import re
from typing import Any, Mapping
import uuid

from .activation_v2 import activation_v2_logical_digest
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


PROJECT_AUTO_STATE_VERSION = 1
PROJECT_AUTO_RECEIPT_VERSION = 1
MAX_PROJECT_AUTO_ENTRIES = 64
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_OUTCOMES = frozenset(("completed", "partial", "blocked"))
_ENTRY_STATES = frozenset(("active", "rolled_back"))
_OPERATIONS = frozenset(("commit", "rollback"))
_RECEIPT_STATES = frozenset(("committed_synthetic", "rolled_back_synthetic"))
_COUNT_KEYS = frozenset(("decisions", "evidence", "open_tasks", "questions", "global_candidates"))


class ProjectAutoSyntheticError(SemanticValidationError):
    """A synthetic A4 transaction violates a fail-closed boundary."""


@dataclass(frozen=True)
class ProjectAutoEntry:
    """One closure identity in a synthetic project-auto state machine."""

    transaction_id: str
    closure_id: str
    closure_digest: str
    task_outcome: str
    candidate_counts: Mapping[str, int]
    committed_revision: int
    state: str
    rollback_revision: int | None

    def __post_init__(self) -> None:
        if type(self) is not ProjectAutoEntry:
            raise ProjectAutoSyntheticError("project-auto entry is invalid")
        _require_prefixed_uuid(self.transaction_id, "project-auto", "project-auto transaction")
        _require_prefixed_uuid(self.closure_id, "closure", "project-auto closure")
        _require_hash(self.closure_digest, "project-auto closure digest")
        if self.task_outcome not in _OUTCOMES:
            raise ProjectAutoSyntheticError("project-auto outcome is invalid")
        _validate_candidate_counts(self.candidate_counts)
        _require_revision(self.committed_revision, "project-auto committed revision", minimum=1)
        if self.state not in _ENTRY_STATES:
            raise ProjectAutoSyntheticError("project-auto entry state is invalid")
        if self.state == "active" and self.rollback_revision is not None:
            raise ProjectAutoSyntheticError("active project-auto entry has a rollback revision")
        if self.state == "rolled_back":
            _require_revision(self.rollback_revision, "project-auto rollback revision", minimum=self.committed_revision + 1)

    def to_dict(self) -> dict[str, object]:
        return {
            "transaction_id": self.transaction_id,
            "closure_id": self.closure_id,
            "closure_digest": self.closure_digest,
            "task_outcome": self.task_outcome,
            "candidate_counts": dict(self.candidate_counts),
            "committed_revision": self.committed_revision,
            "state": self.state,
            "rollback_revision": self.rollback_revision,
        }

    @classmethod
    def from_value(cls, value: object) -> "ProjectAutoEntry":
        expected = {
            "transaction_id",
            "closure_id",
            "closure_digest",
            "task_outcome",
            "candidate_counts",
            "committed_revision",
            "state",
            "rollback_revision",
        }
        if type(value) is not dict or set(value) != expected:
            raise ProjectAutoSyntheticError("project-auto entry has unsupported fields")
        try:
            return cls(**value)
        except (TypeError, ProjectAutoSyntheticError) as error:
            raise ProjectAutoSyntheticError("project-auto entry is invalid") from error


@dataclass(frozen=True)
class SyntheticProjectAutoState:
    """Digest-bound state for one synthetic fixture project only."""

    schema_version: int
    project_id: str
    revision: int
    entries: tuple[ProjectAutoEntry, ...]
    state_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not SyntheticProjectAutoState:
            raise ProjectAutoSyntheticError("project-auto state is invalid")
        if self.schema_version != PROJECT_AUTO_STATE_VERSION or isinstance(self.schema_version, bool):
            raise ProjectAutoSyntheticError("project-auto state version is unsupported")
        if not isinstance(self.project_id, str) or _PROJECT_ID.fullmatch(self.project_id) is None:
            raise ProjectAutoSyntheticError("project-auto state project is invalid")
        _require_revision(self.revision, "project-auto state revision", minimum=0)
        if not isinstance(self.entries, tuple) or len(self.entries) > MAX_PROJECT_AUTO_ENTRIES:
            raise ProjectAutoSyntheticError("project-auto state entries are invalid")
        if any(type(entry) is not ProjectAutoEntry for entry in self.entries):
            raise ProjectAutoSyntheticError("project-auto state entries are invalid")
        if len({entry.transaction_id for entry in self.entries}) != len(self.entries):
            raise ProjectAutoSyntheticError("project-auto state repeats a transaction")
        if len({entry.closure_id for entry in self.entries}) != len(self.entries):
            raise ProjectAutoSyntheticError("project-auto state repeats a closure")
        if any(entry.committed_revision > self.revision for entry in self.entries):
            raise ProjectAutoSyntheticError("project-auto state entry revision is invalid")
        if any(
            entry.rollback_revision is not None and entry.rollback_revision > self.revision
            for entry in self.entries
        ):
            raise ProjectAutoSyntheticError("project-auto state rollback revision is invalid")
        expected = sha256_hex(self._digest_input())
        if self.state_digest and self.state_digest != expected:
            raise ProjectAutoSyntheticError("project-auto state digest does not match")
        object.__setattr__(self, "state_digest", expected)

    @classmethod
    def initial(cls, project_id: str) -> "SyntheticProjectAutoState":
        return cls(PROJECT_AUTO_STATE_VERSION, project_id, 0, ())

    def _digest_input(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "revision": self.revision,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._digest_input(), "state_digest": self.state_digest}

    @classmethod
    def from_value(cls, value: object) -> "SyntheticProjectAutoState":
        expected = {"schema_version", "project_id", "revision", "entries", "state_digest"}
        if type(value) is not dict or set(value) != expected or not isinstance(value["entries"], list):
            raise ProjectAutoSyntheticError("project-auto state has unsupported fields")
        try:
            return cls(
                schema_version=value["schema_version"],
                project_id=value["project_id"],
                revision=value["revision"],
                entries=tuple(ProjectAutoEntry.from_value(item) for item in value["entries"]),
                state_digest=value["state_digest"],
            )
        except (TypeError, ProjectAutoSyntheticError) as error:
            raise ProjectAutoSyntheticError("project-auto state is invalid") from error


@dataclass(frozen=True)
class ProjectAutoReceipt:
    """A user-visible synthetic transaction status without closure content."""

    schema_version: int
    receipt_id: str
    operation: str
    transaction_id: str
    rollback_of_transaction_id: str | None
    project_id: str
    closure_id: str
    closure_digest: str
    task_outcome: str
    candidate_counts: Mapping[str, int]
    expected_revision: int
    previous_revision: int
    state_revision: int
    project_write_state: str
    global_outbox_state: str
    fixture_only: bool
    authority_write: bool
    global_write: bool
    state_digest: str
    created_at: str
    receipt_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not ProjectAutoReceipt:
            raise ProjectAutoSyntheticError("project-auto receipt is invalid")
        if self.schema_version != PROJECT_AUTO_RECEIPT_VERSION or isinstance(self.schema_version, bool):
            raise ProjectAutoSyntheticError("project-auto receipt version is unsupported")
        _require_prefixed_uuid(self.receipt_id, "project-auto-receipt", "project-auto receipt")
        if self.operation not in _OPERATIONS:
            raise ProjectAutoSyntheticError("project-auto receipt operation is invalid")
        _require_prefixed_uuid(self.transaction_id, "project-auto", "project-auto transaction")
        if self.operation == "commit":
            if self.rollback_of_transaction_id is not None:
                raise ProjectAutoSyntheticError("project-auto commit cannot target a rollback")
            if self.project_write_state != "committed_synthetic":
                raise ProjectAutoSyntheticError("project-auto commit state is invalid")
        else:
            _require_prefixed_uuid(
                self.rollback_of_transaction_id,
                "project-auto",
                "project-auto rollback target",
            )
            if self.rollback_of_transaction_id != self.transaction_id:
                raise ProjectAutoSyntheticError("project-auto rollback target is invalid")
            if self.project_write_state != "rolled_back_synthetic":
                raise ProjectAutoSyntheticError("project-auto rollback state is invalid")
        if not isinstance(self.project_id, str) or _PROJECT_ID.fullmatch(self.project_id) is None:
            raise ProjectAutoSyntheticError("project-auto receipt project is invalid")
        _require_prefixed_uuid(self.closure_id, "closure", "project-auto receipt closure")
        _require_hash(self.closure_digest, "project-auto receipt closure digest")
        if self.task_outcome not in _OUTCOMES:
            raise ProjectAutoSyntheticError("project-auto receipt outcome is invalid")
        _validate_candidate_counts(self.candidate_counts)
        _require_revision(self.expected_revision, "project-auto expected revision", minimum=0)
        _require_revision(self.previous_revision, "project-auto previous revision", minimum=0)
        _require_revision(self.state_revision, "project-auto state revision", minimum=1)
        if self.expected_revision != self.previous_revision or self.state_revision != self.previous_revision + 1:
            raise ProjectAutoSyntheticError("project-auto receipt compare-and-swap values are invalid")
        if self.global_outbox_state != "pending_review" or self.fixture_only is not True:
            raise ProjectAutoSyntheticError("project-auto receipt boundary is invalid")
        if self.authority_write is not False or self.global_write is not False:
            raise ProjectAutoSyntheticError("project-auto receipt exceeds synthetic authority")
        _require_hash(self.state_digest, "project-auto receipt state digest")
        _require_timestamp(self.created_at, "project-auto receipt timestamp")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "receipt_digest")
        if self.receipt_digest and self.receipt_digest != expected:
            raise ProjectAutoSyntheticError("project-auto receipt digest does not match")
        object.__setattr__(self, "receipt_digest", expected)

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "operation": self.operation,
            "transaction_id": self.transaction_id,
            "rollback_of_transaction_id": self.rollback_of_transaction_id,
            "project_id": self.project_id,
            "closure_id": self.closure_id,
            "closure_digest": self.closure_digest,
            "task_outcome": self.task_outcome,
            "candidate_counts": dict(self.candidate_counts),
            "expected_revision": self.expected_revision,
            "previous_revision": self.previous_revision,
            "state_revision": self.state_revision,
            "project_write_state": self.project_write_state,
            "global_outbox_state": self.global_outbox_state,
            "fixture_only": self.fixture_only,
            "authority_write": self.authority_write,
            "global_write": self.global_write,
            "state_digest": self.state_digest,
            "created_at": self.created_at,
        }
        if include_digest:
            value["receipt_digest"] = self.receipt_digest
        return value

    @classmethod
    def from_value(cls, value: object) -> "ProjectAutoReceipt":
        if isinstance(value, cls):
            return value
        expected = {
            "schema_version",
            "receipt_id",
            "operation",
            "transaction_id",
            "rollback_of_transaction_id",
            "project_id",
            "closure_id",
            "closure_digest",
            "task_outcome",
            "candidate_counts",
            "expected_revision",
            "previous_revision",
            "state_revision",
            "project_write_state",
            "global_outbox_state",
            "fixture_only",
            "authority_write",
            "global_write",
            "state_digest",
            "created_at",
            "receipt_digest",
        }
        if type(value) is not dict or set(value) != expected:
            raise ProjectAutoSyntheticError("project-auto receipt has unsupported fields")
        try:
            return cls(**value)
        except (TypeError, ProjectAutoSyntheticError) as error:
            raise ProjectAutoSyntheticError("project-auto receipt is invalid") from error


def commit_synthetic_project_auto(
    runtime: DisposableRuntime,
    closure: Mapping[str, Any],
    *,
    expected_revision: int,
    transaction_id: str | None = None,
    receipt_id: str | None = None,
    created_at: str | None = None,
) -> ProjectAutoReceipt:
    """Commit a fixture-only closure summary after exact CAS and dedupe checks."""

    handle = load_disposable_runtime(runtime.root)
    _require_fixture_project_auto_policy(handle)
    if type(closure) is not dict:
        raise ProjectAutoSyntheticError("project-auto closure is invalid")
    validate_named_document("activation-v2-task-closure-v1", closure)
    if closure["task_outcome"] == "no_durable_change":
        raise ProjectAutoSyntheticError("project-auto requires a durable closure candidate")
    state = _load_state(handle, closure["project_id"])
    closure_digest = sha256_hex(closure)
    existing = _find_entry_by_closure(state, closure["closure_id"])
    if existing is not None:
        if existing.closure_digest != closure_digest or existing.state != "active":
            raise ProjectAutoSyntheticError("project-auto closure identity cannot be reused")
        return _load_commit_receipt(handle, existing.transaction_id)
    _require_revision(expected_revision, "project-auto expected revision", minimum=0)
    if expected_revision != state.revision:
        raise ProjectAutoSyntheticError("project-auto compare-and-swap revision does not match")
    identifier = transaction_id or f"project-auto:{uuid.uuid4()}"
    _require_prefixed_uuid(identifier, "project-auto", "project-auto transaction")
    if any(entry.transaction_id == identifier for entry in state.entries):
        raise ProjectAutoSyntheticError("project-auto transaction identity already exists")
    next_revision = state.revision + 1
    counts = _closure_candidate_counts(closure)
    entry = ProjectAutoEntry(
        transaction_id=identifier,
        closure_id=closure["closure_id"],
        closure_digest=closure_digest,
        task_outcome=closure["task_outcome"],
        candidate_counts=counts,
        committed_revision=next_revision,
        state="active",
        rollback_revision=None,
    )
    next_state = SyntheticProjectAutoState(
        PROJECT_AUTO_STATE_VERSION,
        closure["project_id"],
        next_revision,
        state.entries + (entry,),
    )
    receipt = ProjectAutoReceipt(
        schema_version=PROJECT_AUTO_RECEIPT_VERSION,
        receipt_id=receipt_id or f"project-auto-receipt:{uuid.uuid4()}",
        operation="commit",
        transaction_id=identifier,
        rollback_of_transaction_id=None,
        project_id=next_state.project_id,
        closure_id=entry.closure_id,
        closure_digest=entry.closure_digest,
        task_outcome=entry.task_outcome,
        candidate_counts=entry.candidate_counts,
        expected_revision=expected_revision,
        previous_revision=state.revision,
        state_revision=next_state.revision,
        project_write_state="committed_synthetic",
        global_outbox_state="pending_review",
        fixture_only=True,
        authority_write=False,
        global_write=False,
        state_digest=next_state.state_digest,
        created_at=created_at or _now_rfc3339(),
    )
    _write_state(handle, next_state)
    try:
        _write_commit_receipt(handle, receipt)
    except Exception:
        # The prior state is still safe and was fully validated before this write.
        _write_state(handle, state)
        raise
    return receipt


def rollback_synthetic_project_auto(
    runtime: DisposableRuntime,
    receipt: ProjectAutoReceipt | Mapping[str, Any],
    *,
    expected_revision: int,
    rollback_receipt_id: str | None = None,
    created_at: str | None = None,
) -> ProjectAutoReceipt:
    """Roll back only the latest matching synthetic transaction under CAS."""

    handle = load_disposable_runtime(runtime.root)
    _require_fixture_project_auto_policy(handle)
    commit_receipt = ProjectAutoReceipt.from_value(receipt)
    if commit_receipt.operation != "commit":
        raise ProjectAutoSyntheticError("project-auto rollback requires a commit receipt")
    persisted = _load_commit_receipt(handle, commit_receipt.transaction_id)
    if persisted.to_dict() != commit_receipt.to_dict():
        raise IntegrityError("project-auto rollback receipt does not match persisted commit")
    state = _load_state(handle, commit_receipt.project_id)
    _require_revision(expected_revision, "project-auto rollback expected revision", minimum=1)
    if expected_revision != state.revision or state.revision != commit_receipt.state_revision:
        raise ProjectAutoSyntheticError("project-auto rollback compare-and-swap revision does not match")
    entry = _find_entry_by_transaction(state, commit_receipt.transaction_id)
    if entry is None or entry.state != "active" or entry.committed_revision != state.revision:
        raise ProjectAutoSyntheticError("project-auto rollback target is not the latest active transaction")
    next_revision = state.revision + 1
    replaced = replace(entry, state="rolled_back", rollback_revision=next_revision)
    next_state = SyntheticProjectAutoState(
        PROJECT_AUTO_STATE_VERSION,
        state.project_id,
        next_revision,
        tuple(replaced if item.transaction_id == entry.transaction_id else item for item in state.entries),
    )
    rollback_receipt = ProjectAutoReceipt(
        schema_version=PROJECT_AUTO_RECEIPT_VERSION,
        receipt_id=rollback_receipt_id or f"project-auto-receipt:{uuid.uuid4()}",
        operation="rollback",
        transaction_id=entry.transaction_id,
        rollback_of_transaction_id=entry.transaction_id,
        project_id=next_state.project_id,
        closure_id=entry.closure_id,
        closure_digest=entry.closure_digest,
        task_outcome=entry.task_outcome,
        candidate_counts=entry.candidate_counts,
        expected_revision=expected_revision,
        previous_revision=state.revision,
        state_revision=next_state.revision,
        project_write_state="rolled_back_synthetic",
        global_outbox_state="pending_review",
        fixture_only=True,
        authority_write=False,
        global_write=False,
        state_digest=next_state.state_digest,
        created_at=created_at or _now_rfc3339(),
    )
    _write_state(handle, next_state)
    try:
        _write_rollback_receipt(handle, rollback_receipt)
    except Exception:
        _write_state(handle, state)
        raise
    return rollback_receipt


def load_synthetic_project_auto_state(
    runtime: DisposableRuntime,
    project_id: str,
) -> SyntheticProjectAutoState:
    """Read a validated synthetic state without opening an authority store."""

    handle = load_disposable_runtime(runtime.root)
    _require_fixture_project_auto_policy(handle)
    if not isinstance(project_id, str) or _PROJECT_ID.fullmatch(project_id) is None:
        raise ProjectAutoSyntheticError("project-auto state project is invalid")
    return _load_state(handle, project_id)


def _require_fixture_project_auto_policy(runtime: DisposableRuntime) -> None:
    runtime.manifest.policy.require_resolved("A4")
    if not runtime.manifest.policy.fixture_only or runtime.manifest.policy.capture_default != "PROJECT_AUTO":
        raise ProjectAutoSyntheticError("A4 requires a fixture-only PROJECT_AUTO policy")


def _load_state(runtime: DisposableRuntime, project_id: str) -> SyntheticProjectAutoState:
    relative = _state_path(project_id)
    if not disposable_json_exists(runtime, relative):
        return SyntheticProjectAutoState.initial(project_id)
    try:
        state = SyntheticProjectAutoState.from_value(read_disposable_json(runtime, relative))
    except (IntegrityError, ProjectAutoSyntheticError) as error:
        raise IntegrityError("synthetic project-auto state is unavailable or invalid") from error
    if state.project_id != project_id:
        raise IntegrityError("synthetic project-auto state crosses a project boundary")
    return state


def _write_state(runtime: DisposableRuntime, state: SyntheticProjectAutoState) -> None:
    write_disposable_json(runtime, _state_path(state.project_id), state.to_dict())


def _load_commit_receipt(runtime: DisposableRuntime, transaction_id: str) -> ProjectAutoReceipt:
    try:
        receipt = ProjectAutoReceipt.from_value(read_disposable_json(runtime, _commit_receipt_path(transaction_id)))
    except (IntegrityError, ProjectAutoSyntheticError) as error:
        raise IntegrityError("synthetic project-auto commit receipt is unavailable or invalid") from error
    if receipt.operation != "commit" or receipt.transaction_id != transaction_id:
        raise IntegrityError("synthetic project-auto commit receipt is invalid")
    return receipt


def _write_commit_receipt(runtime: DisposableRuntime, receipt: ProjectAutoReceipt) -> None:
    if disposable_json_exists(runtime, _commit_receipt_path(receipt.transaction_id)):
        raise ProjectAutoSyntheticError("project-auto commit receipt identity already exists")
    write_disposable_json(runtime, _commit_receipt_path(receipt.transaction_id), receipt.to_dict())


def _write_rollback_receipt(runtime: DisposableRuntime, receipt: ProjectAutoReceipt) -> None:
    relative = _rollback_receipt_path(receipt.receipt_id)
    if disposable_json_exists(runtime, relative):
        raise ProjectAutoSyntheticError("project-auto rollback receipt identity already exists")
    write_disposable_json(runtime, relative, receipt.to_dict())


def _find_entry_by_closure(state: SyntheticProjectAutoState, closure_id: str) -> ProjectAutoEntry | None:
    return next((entry for entry in state.entries if entry.closure_id == closure_id), None)


def _find_entry_by_transaction(state: SyntheticProjectAutoState, transaction_id: str) -> ProjectAutoEntry | None:
    return next((entry for entry in state.entries if entry.transaction_id == transaction_id), None)


def _closure_candidate_counts(closure: Mapping[str, Any]) -> dict[str, int]:
    return {key: len(closure[key]) for key in sorted(_COUNT_KEYS)}


def _validate_candidate_counts(value: object) -> None:
    if type(value) is not dict or set(value) != _COUNT_KEYS:
        raise ProjectAutoSyntheticError("project-auto candidate counts are invalid")
    if any(type(item) is not int or not 0 <= item <= 32 for item in value.values()):
        raise ProjectAutoSyntheticError("project-auto candidate counts are invalid")


def _require_prefixed_uuid(value: object, prefix: str, label: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix + ":"):
        raise ProjectAutoSyntheticError(f"{label} is invalid")
    if _UUID.fullmatch(value.removeprefix(prefix + ":")) is None:
        raise ProjectAutoSyntheticError(f"{label} is invalid")
    return value


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ProjectAutoSyntheticError(f"{label} is invalid")
    return value


def _require_revision(value: object, label: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ProjectAutoSyntheticError(f"{label} is invalid")
    return value


def _require_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ProjectAutoSyntheticError(f"{label} is invalid")
    try:
        parse_rfc3339_utc(value)
    except Exception as error:
        raise ProjectAutoSyntheticError(f"{label} is invalid") from error
    return value


def _state_path(project_id: str) -> str:
    return f"projects/{project_id}/synthetic-a4/project-auto-state.json"


def _commit_receipt_path(transaction_id: str) -> str:
    return f"receipts/project-auto/{transaction_id.removeprefix('project-auto:')}.json"


def _rollback_receipt_path(receipt_id: str) -> str:
    return f"receipts/project-auto-rollbacks/{receipt_id.removeprefix('project-auto-receipt:')}.json"


def _now_rfc3339() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
