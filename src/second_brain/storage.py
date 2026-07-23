"""M1's local, authoritative, single-store storage kernel.

The implementation intentionally owns a small surface: one explicit store root,
full-object compare-and-swap mutations, a journaled commit protocol, and strict
read validation.  It never discovers a global store or treats stored text as
instructions.
"""

from __future__ import annotations

from base64 import b64decode, b64encode
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
import errno
import fcntl
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Callable, Iterator, Mapping, Sequence
from types import MappingProxyType
import uuid

from .canonical import canonical_jcs_bytes as _canonical_jcs_bytes
from .canonical import sha256_bytes
from .contracts import logical_content_hash, validate_named_document
from .errors import (
    AuthorityDeniedError,
    ContentPolicyError,
    IdempotencyKeyReusedError,
    IntegrityError,
    ParseError,
    PathUnsafeError,
    RelationInvalidError,
    RevisionConflictError,
    StorageError,
    StoreDegradedError,
)
from .parsing import parse_markdown_object, parse_ndjson, parse_strict_json
from .schema_validation import parse_rfc3339_utc
from .workspace import repository_root


STORE_WRITER_VERSION = "second-brain-store/0.1.0"
SCHEMA_REGISTRY_VERSION = "2.0.0"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TRANSACTION_ID = re.compile(r"^txn:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
_HEX_HASH = re.compile(r"^[0-9a-f]{64}$")
_PROJECT_STORE_ID = re.compile(r"^project:([a-z0-9][a-z0-9._-]{0,63})$")
_ROOT_CONFIRMATION_ACTOR = "root"
_KIND_PLURALS = {
    "source": "sources",
    "entity": "entities",
    "concept": "concepts",
    "claim": "claims",
    "synthesis": "syntheses",
    "project": "projects",
    "decision": "decisions",
    "component": "components",
    "task": "tasks",
    "bug": "bugs",
    "experiment": "experiments",
    "evidence": "evidence",
    "question": "questions",
}
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class StoreHealth:
    """String constants rather than an Enum keep serialized health obvious."""

    HEALTHY = "HEALTHY"
    DEGRADED_READ_ONLY = "DEGRADED_READ_ONLY"


@dataclass(frozen=True)
class Mutation:
    operation: str
    object_id: str
    expected_revision: int | None
    expected_content_hash: str | None
    desired_object: Mapping[str, Any]

    @classmethod
    def from_value(cls, value: "Mutation | Mapping[str, Any]") -> "Mutation":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise StorageError("SCHEMA_INVALID", "mutation must be an object")
        try:
            return cls(
                operation=value["operation"],
                object_id=value["object_id"],
                expected_revision=value.get("expected_revision"),
                expected_content_hash=value.get("expected_content_hash"),
                desired_object=value["desired_object"],
            )
        except (KeyError, TypeError) as error:
            raise StorageError("SCHEMA_INVALID", "mutation is missing required fields") from error


@dataclass(frozen=True)
class TransactionRequest:
    transaction_id: str
    idempotency_key: str
    store_id: str
    actor: Any
    confirmation: Any
    mutations: Sequence[Mutation | Mapping[str, Any]]
    reason: str
    schema_version: int = 1

    @classmethod
    def from_value(cls, value: "TransactionRequest | Mapping[str, Any]") -> "TransactionRequest":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise StorageError("SCHEMA_INVALID", "transaction request must be an object")
        try:
            return cls(
                transaction_id=value["transaction_id"],
                idempotency_key=value["idempotency_key"],
                store_id=value["store_id"],
                actor=value["actor"],
                confirmation=value.get("confirmation"),
                mutations=value["mutations"],
                reason=value["reason"],
                schema_version=value.get("schema_version", 1),
            )
        except (KeyError, TypeError) as error:
            raise StorageError("SCHEMA_INVALID", "transaction request is missing required fields") from error


@dataclass(frozen=True)
class ChangedObject:
    id: str
    revision: int
    content_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "revision": self.revision, "content_hash": self.content_hash}


@dataclass(frozen=True)
class CommitReceipt:
    transaction_id: str
    idempotency_key: str
    store_id: str
    mutation_epoch: int
    event_head: str | None
    changed_objects: tuple[ChangedObject, ...]
    committed_at: str
    status: str = "committed"
    schema_version: int = 1
    idempotent: bool = False

    @property
    def objects(self) -> tuple[ChangedObject, ...]:
        """Compatibility with the receipt shape in the data schema."""

        return self.changed_objects

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "transaction_id": self.transaction_id,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "store_id": self.store_id,
            "mutation_epoch": self.mutation_epoch,
            "event_head": self.event_head,
            "objects": [item.to_dict() for item in self.changed_objects],
            "committed_at": self.committed_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, idempotent: bool = False) -> "CommitReceipt":
        try:
            objects = tuple(
                ChangedObject(
                    id=item["id"], revision=item["revision"], content_hash=item["content_hash"]
                )
                for item in value["objects"]
            )
            receipt = cls(
                transaction_id=value["transaction_id"],
                idempotency_key=value["idempotency_key"],
                store_id=value["store_id"],
                mutation_epoch=value["mutation_epoch"],
                event_head=value.get("event_head"),
                changed_objects=objects,
                committed_at=value["committed_at"],
                status=value.get("status", "committed"),
                schema_version=value.get("schema_version", 1),
                idempotent=idempotent,
            )
        except (KeyError, TypeError) as error:
            raise IntegrityError("committed journal has an invalid receipt") from error
        if (
            not isinstance(receipt.transaction_id, str)
            or _TRANSACTION_ID.fullmatch(receipt.transaction_id) is None
            or not isinstance(receipt.idempotency_key, str)
            or not receipt.idempotency_key
            or not isinstance(receipt.store_id, str)
            or not receipt.store_id
            or not isinstance(receipt.mutation_epoch, int)
            or isinstance(receipt.mutation_epoch, bool)
            or receipt.mutation_epoch < 1
            or receipt.status != "committed"
            or receipt.schema_version != 1
            or not isinstance(receipt.committed_at, str)
            or (
                receipt.event_head is not None
                and (
                    not isinstance(receipt.event_head, str)
                    or _HEX_HASH.fullmatch(receipt.event_head) is None
                )
            )
        ):
            raise IntegrityError("committed journal has an invalid receipt")
        for item in receipt.changed_objects:
            if (
                not isinstance(item.id, str)
                or not isinstance(item.revision, int)
                or isinstance(item.revision, bool)
                or item.revision < 1
                or not isinstance(item.content_hash, str)
                or _HEX_HASH.fullmatch(item.content_hash) is None
            ):
                raise IntegrityError("committed journal has an invalid receipt object")
        return receipt


@dataclass(frozen=True)
class RecoveryReceipt:
    status: str
    recovered_transactions: tuple[str, ...]
    rolled_back_transactions: tuple[str, ...]
    health: str


@dataclass(frozen=True)
class ObjectSnapshot:
    """One integrity-checked authority object in a coherent store snapshot."""

    id: str
    revision: int
    content_hash: str
    relative_path: str
    file_sha256: str
    document: Mapping[str, Any]


@dataclass(frozen=True)
class StoreSnapshot:
    """Public, validated authority state for derived-state consumers."""

    store_id: str
    project_id: str
    mutation_epoch: int
    event_head: str | None
    object_count: int
    objects: tuple[ObjectSnapshot, ...]


@dataclass(frozen=True)
class LintFinding:
    severity: str
    code: str
    message: str
    object_id: str | None = None
    relation_id: str | None = None


@dataclass(frozen=True)
class LintReport:
    findings: tuple[LintFinding, ...]
    exit_code: int

    @property
    def counts(self) -> dict[str, int]:
        return {
            "errors": sum(item.severity == "error" for item in self.findings),
            "warnings": sum(item.severity == "warning" for item in self.findings),
            "info": sum(item.severity == "info" for item in self.findings),
        }


def canonical_jcs_bytes(value: Any) -> bytes:
    """Public JCS helper that presents malformed values as stable storage errors."""

    try:
        return _canonical_jcs_bytes(value)
    except (TypeError, ValueError) as error:
        raise StorageError("INTEGRITY_FAILED", "value cannot be canonicalized as JSON") from error


def sha256_hex(value: Any) -> str:
    """Hash bytes directly, or canonicalize a JSON value before hashing it."""

    if isinstance(value, bytes):
        return sha256_bytes(value)
    if isinstance(value, bytearray):
        return sha256_bytes(bytes(value))
    return sha256_bytes(canonical_jcs_bytes(value))


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


class Store:
    """One explicit repository-contained authoritative store."""

    def __init__(
        self,
        root: Path,
        *,
        clock: Any = None,
        id_factory: Callable[..., Any] | None = None,
        authorizer: Callable[..., Any] | None = None,
        failure_injector: Callable[[str], Any] | None = None,
    ) -> None:
        self.root = _validate_store_root(root, create=False)
        self._clock = clock
        self._id_factory = id_factory
        self._authorizer = authorizer
        self._failure_injector = failure_injector
        self._health = StoreHealth.HEALTHY
        self._health_reason: str | None = None

    @classmethod
    def initialize(
        cls,
        root: str | Path,
        store_id: str,
        *,
        project_id: str | None = None,
        clock: Any = None,
        id_factory: Callable[..., Any] | None = None,
        authorizer: Callable[..., Any] | None = None,
        failure_injector: Callable[[str], Any] | None = None,
    ) -> "Store":
        if not isinstance(store_id, str) or not store_id:
            raise StorageError("SCHEMA_INVALID", "store_id must be non-empty text")
        if project_id is None and store_id.startswith("project:"):
            project_id = store_id.split(":", 1)[1]
        _validate_store_identity(store_id, project_id, error_type=StorageError)
        resolved = _validate_store_root(Path(root), create=True)
        _ensure_directory(resolved)
        _assert_no_symlink_path(resolved, resolved)
        manifest_path = resolved / "manifest.json"
        if _read_bytes_if_exists(manifest_path, resolved) is not None:
            store = cls(
                resolved,
                clock=clock,
                id_factory=id_factory,
                authorizer=authorizer,
                failure_injector=failure_injector,
            )
            manifest = store._load_manifest()
            if manifest.get("store_id") != store_id:
                raise IntegrityError("existing manifest belongs to another store")
            store._refresh_health()
            return store

        store = cls(
            resolved,
            clock=clock,
            id_factory=id_factory,
            authorizer=authorizer,
            failure_injector=failure_injector,
        )
        store._initialize_layout()
        with store._writer_lock():
            # Another initializer may have won the lock while this one created
            # the empty layout. Never overwrite its manifest or event ledger.
            if _read_bytes_if_exists(store._manifest_path, store.root) is not None:
                manifest = store._load_manifest()
                if manifest.get("store_id") != store_id:
                    raise IntegrityError("existing manifest belongs to another store")
            else:
                manifest = {
                    "schema_version": 2,
                    "store_id": store_id,
                    "project_id": project_id,
                    "created_at": store._now_rfc3339(),
                    "mutation_epoch": 0,
                    "event_sequence": 0,
                    "event_head": None,
                    "object_count": 0,
                    "schema_registry_version": SCHEMA_REGISTRY_VERSION,
                    "writer_version": STORE_WRITER_VERSION,
                }
                _atomic_write(store._manifest_path, canonical_jcs_bytes(manifest), store.root)
                _atomic_write(store._events_path, b"", store.root)
        store._refresh_health()
        return store

    @classmethod
    def open(
        cls,
        root: str | Path,
        *,
        clock: Any = None,
        id_factory: Callable[..., Any] | None = None,
        authorizer: Callable[..., Any] | None = None,
        failure_injector: Callable[[str], Any] | None = None,
    ) -> "Store":
        store = cls(
            Path(root),
            clock=clock,
            id_factory=id_factory,
            authorizer=authorizer,
            failure_injector=failure_injector,
        )
        store._assert_layout_exists()
        store._refresh_health()
        return store

    @property
    def health(self) -> str:
        return self._health

    @property
    def _manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def _events_path(self) -> Path:
        return self.root / "events" / "events.ndjson"

    @property
    def _transactions_path(self) -> Path:
        return self.root / "runtime" / "transactions"

    def _event_offset(self) -> int:
        raw = _read_bytes_if_exists(self._events_path, self.root)
        if raw is None:
            raise IntegrityError("event ledger disappeared during transaction preparation")
        return len(raw)

    def read(self, object_id: str) -> dict[str, Any]:
        """Read and integrity-check one object without returning partial content."""

        try:
            path = self._object_path_for_id(object_id)
        except PathUnsafeError:
            raise
        self._assert_layout_exists()
        self._refresh_health()
        self._assert_healthy_for_read()
        if not path.exists():
            raise StorageError("OBJECT_NOT_FOUND", "object does not exist")
        try:
            document = self._read_object_path(path)
            if document["id"] != object_id:
                raise IntegrityError("object file identity does not match requested ID")
        except StorageError as error:
            self._degrade(error.code)
            raise
        return deepcopy(document)

    def snapshot(self) -> StoreSnapshot:
        """Return a coherent, integrity-checked view of all authority objects.

        Derived-state readers need both the logical object identity and the
        exact physical bytes that supplied it.  Holding the normal writer lock
        keeps cooperative M1 commits out of the middle of the capture; the
        complete authority audit makes an out-of-band edit fail closed rather
        than producing a partial corpus.
        """

        try:
            self._assert_layout_exists()
            with self._writer_lock():
                self._assert_layout_exists()
                manifest = self._load_manifest()
                documents = self._load_current_objects()
                if manifest["object_count"] != len(documents):
                    raise IntegrityError("manifest object count does not match authority files")
                events = self._validate_event_ledger(manifest)
                self._validate_event_object_snapshot(events, documents)
                self._validate_journal_records(manifest, events)
                if self._prepared_journals():
                    raise StoreDegradedError("store has an incomplete transaction")

                objects: list[ObjectSnapshot] = []
                for object_id in sorted(documents):
                    document = documents[object_id]
                    path = self._object_path_for_document(document)
                    raw = _read_bytes_if_exists(path, self.root)
                    if raw is None:
                        raise IntegrityError("authority object disappeared during snapshot")
                    # Parse the exact bytes used for the physical digest so a
                    # non-cooperating edit cannot mix logical and byte state.
                    exact_document = self._read_object_path(path)
                    if exact_document != document:
                        raise IntegrityError("authority object changed during snapshot")
                    objects.append(
                        ObjectSnapshot(
                            id=object_id,
                            revision=document["revision"],
                            content_hash=document["content_hash"],
                            relative_path=str(path.relative_to(self.root)),
                            file_sha256=sha256_bytes(raw),
                            document=MappingProxyType(deepcopy(document)),
                        )
                    )

                # Re-read the manifest after the object inventory.  A direct
                # replacement cannot be treated as the same snapshot even if
                # it happened to leave the object count unchanged.
                if self._load_manifest() != manifest:
                    raise IntegrityError("manifest changed during snapshot")
        except StorageError as error:
            self._degrade(error.code)
            if isinstance(error, StoreDegradedError):
                raise
            raise StoreDegradedError("store is degraded; recover before authoritative snapshots") from error
        except Exception as error:
            self._degrade("INTEGRITY_FAILED")
            raise StoreDegradedError("store is degraded; recover before authoritative snapshots") from error

        self._health = StoreHealth.HEALTHY
        self._health_reason = None
        return StoreSnapshot(
            store_id=manifest["store_id"],
            project_id=manifest["project_id"],
            mutation_epoch=manifest["mutation_epoch"],
            event_head=manifest["event_head"],
            object_count=manifest["object_count"],
            objects=tuple(objects),
        )

    def commit(self, request: TransactionRequest | Mapping[str, Any]) -> CommitReceipt:
        """Validate then atomically commit one full-object CAS transaction."""

        normalized = self._normalize_request(request)
        # Path checks run before object/schema validation so a malicious ID never
        # gets interpreted as a file name or leaks schema detail.
        for mutation in normalized.mutations:
            self._object_path_for_id(mutation.object_id)
        self._validate_request_shape(normalized)
        self._assert_layout_exists()
        # A long-lived handle must not trust its last health snapshot after an
        # external edit to a manifest, ledger, or durable idempotency receipt.
        self._refresh_health()
        self._assert_healthy_for_write()

        # A durable exact retry is allowed even though its original create CAS
        # now naturally conflicts with the already-committed object.
        existing = self._find_idempotency(normalized)
        if existing is not None:
            return existing

        # Pre-lock validation has no filesystem side effects and gives callers a
        # deterministic early failure for malformed/policy-rejected input.
        pre_state = self._load_current_objects()
        self._validate_transaction(normalized, pre_state)

        try:
            with self._writer_lock():
                self._assert_layout_exists()
                self._refresh_health()
                self._assert_healthy_for_write()
                existing = self._find_idempotency(normalized)
                if existing is not None:
                    return existing
                current_state = self._load_current_objects()
                self._validate_transaction(normalized, current_state)
                return self._commit_locked(normalized, current_state)
        except StorageError:
            raise
        except Exception:
            # A caller-provided failure injector simulates a process crash.  The
            # durable journal remains the source of truth, so this handle must
            # never continue writing before explicit recovery.
            self._degrade("TRANSACTION_INTERRUPTED")
            raise

    def recover(self) -> RecoveryReceipt:
        """Reconcile prepared journals to a wholly pre- or post-transaction state."""

        self._assert_layout_exists()
        recovered: list[str] = []
        rolled_back: list[str] = []
        try:
            with self._writer_lock():
                self._assert_layout_exists()
                journals = self._prepared_journals()
                for journal_path, journal in journals:
                    outcome = self._recover_prepared_journal(journal_path, journal)
                    if outcome == "committed":
                        recovered.append(journal["transaction_id"])
                    else:
                        rolled_back.append(journal["transaction_id"])
                self._refresh_health()
                if self._health != StoreHealth.HEALTHY:
                    raise IntegrityError("store remains divergent after recovery")
        except StorageError as error:
            self._degrade(error.code)
            raise
        return RecoveryReceipt(
            status="recovered" if recovered or rolled_back else "no_recovery_needed",
            recovered_transactions=tuple(recovered),
            rolled_back_transactions=tuple(rolled_back),
            health=self._health,
        )

    def lint(self) -> LintReport:
        """Run a bounded authority audit without exposing object bodies."""

        findings: list[LintFinding] = []
        try:
            self._assert_layout_exists()
            manifest = self._load_manifest()
            documents, object_findings = self._collect_objects_for_lint()
            findings.extend(object_findings)
            findings.extend(self._lint_relations(documents))
            events, event_findings = self._collect_events_for_lint()
            findings.extend(event_findings)
            if not event_findings:
                try:
                    self._validate_event_object_snapshot(events, documents)
                except StorageError:
                    findings.append(
                        LintFinding("error", "EVENT_CHAIN_BROKEN", "objects diverge from event ledger")
                    )
            findings.extend(self._lint_manifest(manifest, documents, events))
            if not event_findings:
                try:
                    self._validate_journal_records(manifest, events)
                except StorageError:
                    findings.append(
                        LintFinding("error", "INTEGRITY_FAILED", "transaction journal integrity failed")
                    )
            for _path, journal in self._prepared_journals(raise_on_invalid=False):
                if journal is None:
                    findings.append(
                        LintFinding("error", "INTEGRITY_FAILED", "transaction journal is unreadable")
                    )
                else:
                    findings.append(
                        LintFinding(
                            "error",
                            "TRANSACTION_INCOMPLETE",
                            "prepared transaction requires recovery",
                        )
                    )
        except StorageError as error:
            findings.append(LintFinding("error", error.code, _safe_message(error)))
        except Exception:
            findings.append(LintFinding("error", "INTEGRITY_FAILED", "lint could not audit store"))

        # Keep the public integrity code simple while preserving more precise
        # lint finding codes for operator repair decisions.
        if any(item.severity == "error" for item in findings):
            if not any(item.code == "INTEGRITY_FAILED" for item in findings):
                findings.append(LintFinding("error", "INTEGRITY_FAILED", "authoritative state is invalid"))
            self._degrade("LINT_ERROR")
        return LintReport(
            findings=tuple(findings),
            exit_code=1 if any(item.severity == "error" for item in findings) else 0,
        )

    def invalidate_derived(self, reason: str) -> None:
        """Remove disposable derived state without touching authority files."""

        if not isinstance(reason, str) or not reason:
            raise StorageError("SCHEMA_INVALID", "derived invalidation requires a reason")
        derived = _safe_child(self.root, Path("derived"))
        if derived.exists():
            _remove_tree_contents(derived, self.root)
        _fsync_directory(self.root)

    def _initialize_layout(self) -> None:
        for relative in (
            "objects",
            "events",
            "runtime",
            "runtime/transactions",
            "derived",
        ):
            directory = _safe_child(self.root, Path(relative))
            _ensure_directory(directory)
        for plural in _KIND_PLURALS.values():
            _ensure_directory(_safe_child(self.root, Path("objects") / plural))

    def _assert_layout_exists(self) -> None:
        _assert_no_symlink_path(self.root, self.root)
        _assert_secure_directory(self.root)
        required_directories = ("objects", "events", "runtime", "runtime/transactions")
        for relative in required_directories:
            directory = _safe_child(self.root, Path(relative))
            _assert_secure_directory(directory)
        for path in (self._manifest_path, self._events_path):
            _assert_regular_file(path, self.root, allow_missing=False)

    def _load_manifest(self) -> dict[str, Any]:
        try:
            raw = _read_bytes_if_exists(self._manifest_path, self.root)
            if raw is None:
                raise IntegrityError("required authority file is missing")
            manifest = parse_strict_json(raw.decode("utf-8"))
        except (UnicodeError, StorageError) as error:
            raise IntegrityError("manifest cannot be read as strict JSON") from error
        if not isinstance(manifest, dict):
            raise IntegrityError("manifest must be a JSON object")
        _validate_manifest(manifest)
        return manifest

    def _read_object_path(self, path: Path) -> dict[str, Any]:
        try:
            raw = _read_bytes_if_exists(path, self.root)
            if raw is None:
                raise IntegrityError("required authority file is missing")
            document = parse_markdown_object(raw.decode("utf-8"))
        except (UnicodeError, StorageError) as error:
            raise IntegrityError("object cannot be read as valid Markdown authority") from error
        self._validate_document(document)
        expected_path = self._object_path_for_document(document)
        if path != expected_path:
            raise IntegrityError("object is stored at an unsafe or incorrect path")
        return document

    def _load_current_objects(self) -> dict[str, dict[str, Any]]:
        objects_root = _safe_child(self.root, Path("objects"))
        documents: dict[str, dict[str, Any]] = {}
        for path in sorted(objects_root.rglob("*.md")):
            _assert_regular_file(path, self.root, allow_missing=False)
            document = self._read_object_path(path)
            object_id = document["id"]
            if object_id in documents:
                raise IntegrityError("duplicate authoritative object ID")
            documents[object_id] = document
        return documents

    def _normalize_request(self, request: TransactionRequest | Mapping[str, Any]) -> TransactionRequest:
        normalized = TransactionRequest.from_value(request)
        try:
            mutations = tuple(Mutation.from_value(item) for item in normalized.mutations)
        except TypeError as error:
            raise StorageError("SCHEMA_INVALID", "mutations must be a sequence") from error
        return TransactionRequest(
            transaction_id=normalized.transaction_id,
            idempotency_key=normalized.idempotency_key,
            store_id=normalized.store_id,
            actor=normalized.actor,
            confirmation=normalized.confirmation,
            mutations=mutations,
            reason=normalized.reason,
            schema_version=normalized.schema_version,
        )

    def _validate_request_shape(self, request: TransactionRequest) -> None:
        if request.schema_version != 1:
            raise StorageError("SCHEMA_INVALID", "unsupported transaction schema version")
        if _TRANSACTION_ID.fullmatch(request.transaction_id) is None:
            raise StorageError("SCHEMA_INVALID", "transaction_id must be a txn UUID")
        if not isinstance(request.idempotency_key, str) or not request.idempotency_key.strip():
            raise StorageError("SCHEMA_INVALID", "idempotency_key must be non-empty text")
        if len(request.idempotency_key) > 1024:
            raise StorageError("SCHEMA_INVALID", "idempotency_key is too long")
        if not isinstance(request.reason, str) or not request.reason.strip() or len(request.reason) > 2000:
            raise StorageError("SCHEMA_INVALID", "reason must be bounded non-empty text")
        if not request.mutations:
            raise StorageError("SCHEMA_INVALID", "transaction must contain at least one mutation")
        if len(request.mutations) > 256:
            raise StorageError("SCHEMA_INVALID", "transaction has too many mutations")
        if not isinstance(request.store_id, str) or request.store_id != self._load_manifest()["store_id"]:
            raise StorageError("SCHEMA_INVALID", "transaction store_id does not match this store")
        ids = [mutation.object_id for mutation in request.mutations]
        if len(ids) != len(set(ids)):
            raise StorageError("SCHEMA_INVALID", "transaction mutates one object more than once")

    def _validate_transaction(
        self, request: TransactionRequest, current: Mapping[str, dict[str, Any]]
    ) -> None:
        proposed = {key: deepcopy(value) for key, value in current.items()}
        for mutation in request.mutations:
            desired = self._normalize_desired_document(mutation)
            self._validate_document(desired)
            self._check_content_policy(desired)
            existing = current.get(mutation.object_id)
            self._validate_authority(request, mutation, desired, existing)
            self._validate_mutation_cas(mutation, desired, existing)
            self._validate_references(desired)
            proposed[mutation.object_id] = desired
        self._validate_confirmation(
            request,
            proposed,
            {mutation.object_id for mutation in request.mutations},
            current,
        )
        self._validate_relations(proposed)

    def _normalize_desired_document(self, mutation: Mutation) -> dict[str, Any]:
        if not isinstance(mutation.desired_object, Mapping):
            raise StorageError("SCHEMA_INVALID", "desired_object must be an object")
        desired = deepcopy(dict(mutation.desired_object))
        if desired.get("id") != mutation.object_id:
            raise StorageError("SCHEMA_INVALID", "mutation object_id and desired object ID differ")
        body = desired.get("body")
        if isinstance(body, str):
            desired["body"] = body.replace("\r\n", "\n").replace("\r", "\n")
        return desired

    def _validate_document(self, document: dict[str, Any]) -> None:
        try:
            validate_named_document("memory-object-v2", document)
        except Exception as error:
            message = str(error)
            if "content_hash" in message:
                raise StorageError("CONTENT_HASH_MISMATCH", "object content hash does not match") from error
            raise StorageError("SCHEMA_INVALID", "object does not satisfy the memory object contract") from error
        self._object_path_for_document(document)

    def _validate_mutation_cas(
        self, mutation: Mutation, desired: dict[str, Any], existing: dict[str, Any] | None
    ) -> None:
        operation = mutation.operation
        if operation not in {"create", "replace", "transition", "tombstone"}:
            raise StorageError("SCHEMA_INVALID", "unsupported mutation operation")
        if operation == "create":
            if mutation.expected_revision is not None or mutation.expected_content_hash is not None:
                raise StorageError("SCHEMA_INVALID", "create must use null CAS expectations")
            if existing is not None:
                raise RevisionConflictError(
                    object_id=mutation.object_id,
                    current_revision=existing["revision"],
                    current_content_hash=existing["content_hash"],
                )
            if desired["revision"] != 1:
                raise StorageError("SCHEMA_INVALID", "created object revision must be one")
            return
        if existing is None:
            raise RevisionConflictError(
                object_id=mutation.object_id, current_revision=None, current_content_hash=None
            )
        if mutation.expected_revision is None or mutation.expected_content_hash is None:
            raise StorageError("SCHEMA_INVALID", "replace requires revision and hash CAS expectations")
        if (
            mutation.expected_revision != existing["revision"]
            or mutation.expected_content_hash != existing["content_hash"]
        ):
            raise RevisionConflictError(
                object_id=mutation.object_id,
                current_revision=existing["revision"],
                current_content_hash=existing["content_hash"],
            )
        if desired["revision"] != existing["revision"] + 1:
            raise StorageError("SCHEMA_INVALID", "replacement revision must advance exactly once")
        for field_name in ("id", "store_id", "kind", "created_at"):
            if desired[field_name] != existing[field_name]:
                raise StorageError("SCHEMA_INVALID", f"immutable object field changed: {field_name}")

    def _validate_authority(
        self,
        request: TransactionRequest,
        mutation: Mutation,
        desired: dict[str, Any],
        existing: dict[str, Any] | None,
    ) -> None:
        operation = mutation.operation
        if operation not in {"create", "replace", "transition", "tombstone"}:
            raise StorageError("SCHEMA_INVALID", "unsupported mutation operation")

        existing_lifecycle = existing.get("lifecycle", {}) if existing else {}
        desired_lifecycle = desired.get("lifecycle", {})
        lifecycle_changed = existing is not None and desired_lifecycle != existing_lifecycle
        authority_changed = existing is not None and desired.get("authority") != existing.get("authority")
        protects_user_decision = desired.get("authority") == "user-decision" or (
            existing is not None and existing.get("authority") == "user-decision"
        )

        if operation == "create" and desired_lifecycle.get("status") != "active":
            raise StorageError("SCHEMA_INVALID", "created object lifecycle must start active")
        if operation == "replace" and (lifecycle_changed or authority_changed):
            if self._authorizer is None:
                raise AuthorityDeniedError("lifecycle or authority changes require explicit authorization")
            raise StorageError("SCHEMA_INVALID", "replace cannot alter lifecycle or authority")
        if operation == "transition":
            if existing is None:
                raise StorageError("SCHEMA_INVALID", "transition requires an existing object")
            if not lifecycle_changed and not authority_changed:
                raise StorageError("SCHEMA_INVALID", "transition must change lifecycle or authority")
        if operation == "tombstone":
            if existing is None:
                raise StorageError("SCHEMA_INVALID", "tombstone requires an existing object")
            if desired_lifecycle.get("status") != "deleted":
                raise StorageError("SCHEMA_INVALID", "tombstone must set lifecycle to deleted")

        # Authority transitions, lifecycle transitions, tombstones, and every
        # user decision fail closed unless an explicit project authorizer grants
        # this exact mutation.  A supplied authorizer still gates ordinary writes.
        requires_authorizer = (
            operation in {"transition", "tombstone"}
            or lifecycle_changed
            or authority_changed
            or protects_user_decision
        )
        if requires_authorizer and self._authorizer is None:
            raise AuthorityDeniedError("sensitive authority mutation requires an explicit authorizer")
        if self._authorizer is not None and not _call_authorizer(
            self._authorizer, request=request, mutation=mutation, store=self
        ):
            raise AuthorityDeniedError()
        if desired["authority"] == "user-decision":
            if desired["kind"] != "decision" or not desired["store_id"].startswith("project:"):
                raise AuthorityDeniedError("user decision authority is limited to project decisions")

    def _validate_confirmation(
        self,
        request: TransactionRequest,
        proposed: Mapping[str, dict[str, Any]],
        changed_ids: set[str],
        current: Mapping[str, dict[str, Any]],
    ) -> None:
        requires_confirmation = any(
            document.get("authority") == "user-decision"
            or current.get(object_id, {}).get("authority") == "user-decision"
            for object_id, document in proposed.items()
            if object_id in changed_ids
        )
        if not requires_confirmation:
            return
        confirmation = request.confirmation
        if not isinstance(confirmation, Mapping):
            raise StorageError("CONFIRMATION_INVALID", "user decision requires structured confirmation")
        required = {"confirmation_id", "confirmed_by", "confirmed_at", "action_digest"}
        if set(confirmation) != required:
            raise StorageError("CONFIRMATION_INVALID", "confirmation is incomplete")
        if not all(isinstance(confirmation[key], str) and confirmation[key] for key in required):
            raise StorageError("CONFIRMATION_INVALID", "confirmation contains invalid values")
        if _HEX_HASH.fullmatch(confirmation["action_digest"]) is None:
            raise StorageError("CONFIRMATION_INVALID", "confirmation action digest is invalid")
        try:
            parse_rfc3339_utc(confirmation["confirmed_at"])
        except Exception as error:
            raise StorageError("CONFIRMATION_INVALID", "confirmation time is invalid") from error
        if (
            confirmation["confirmed_by"] != _ROOT_CONFIRMATION_ACTOR
            or _actor_id(request.actor) != _ROOT_CONFIRMATION_ACTOR
        ):
            raise StorageError("CONFIRMATION_INVALID", "user decision confirmation requires root")
        if confirmation["action_digest"] != self._confirmation_action_digest(request):
            raise StorageError("CONFIRMATION_INVALID", "confirmation does not bind this action")
        for object_id in changed_ids:
            document = proposed[object_id]
            previous = current.get(object_id)
            if document.get("authority") != "user-decision" and (
                previous is None or previous.get("authority") != "user-decision"
            ):
                continue
            matching_provenance = [
                item
                for item in document.get("provenance", [])
                if isinstance(item, Mapping)
                and item.get("kind") == "user_confirmation"
                and item.get("ref") == confirmation["confirmation_id"]
                and item.get("actor_id") == confirmation["confirmed_by"]
                and item.get("observed_at") == confirmation["confirmed_at"]
            ]
            if len(matching_provenance) != 1:
                raise StorageError(
                    "CONFIRMATION_INVALID",
                    "user decision lacks matching confirmation provenance",
                )
        used = self._confirmation_intents(confirmation["confirmation_id"])
        if used:
            raise StorageError("CONFIRMATION_INVALID", "confirmation was already used")

    def _validate_references(self, document: Mapping[str, Any]) -> None:
        """Reject project code/file locators that could escape the repository."""

        if not str(document.get("store_id", "")).startswith("project:"):
            return
        for reference in document.get("references", []):
            if not isinstance(reference, Mapping) or reference.get("kind") not in {"code", "file"}:
                continue
            locator = reference.get("locator")
            if not isinstance(locator, str):
                raise PathUnsafeError("project reference locator is not text")
            self._validate_project_reference_locator(locator)

    def _validate_project_reference_locator(self, locator: str) -> None:
        if not locator or "\\" in locator:
            raise PathUnsafeError("project reference locator is not a safe relative path")
        relative = Path(locator)
        if relative.is_absolute() or ".." in relative.parts:
            raise PathUnsafeError("project reference locator escapes the repository")
        repository = repository_root().resolve()
        lexical = repository / relative
        try:
            lexical.resolve(strict=False).relative_to(repository)
        except ValueError as error:
            raise PathUnsafeError("project reference locator escapes the repository") from error
        current = repository
        for component in relative.parts:
            if component in {"", "."}:
                continue
            current = current / component
            if current.exists() or current.is_symlink():
                try:
                    mode = os.lstat(current).st_mode
                except OSError as error:
                    raise PathUnsafeError("project reference path cannot be inspected") from error
                if stat.S_ISLNK(mode):
                    raise PathUnsafeError("project reference crosses a symlink")

    def _validate_relations(self, documents: Mapping[str, dict[str, Any]]) -> None:
        adjacency: dict[str, set[str]] = {object_id: set() for object_id in documents}
        for object_id, document in documents.items():
            known_provenance = {
                item.get("provenance_id") for item in document.get("provenance", []) if isinstance(item, Mapping)
            }
            for relation in document.get("relations", []):
                if not isinstance(relation, Mapping):
                    raise RelationInvalidError()
                if not set(relation.get("provenance_ids", [])).issubset(known_provenance):
                    raise RelationInvalidError("relation references unknown provenance")
                target = relation.get("target")
                if not isinstance(target, str) or target not in documents:
                    raise RelationInvalidError("relation target is missing")
                target_document = documents[target]
                if target_document.get("store_id") != document.get("store_id"):
                    raise RelationInvalidError("cross-store relations are not supported in M1")
                adjacency[object_id].add(target)
        if _has_directed_cycle(adjacency):
            raise RelationInvalidError("relation graph contains a cycle")

    def _check_content_policy(self, document: Mapping[str, Any]) -> None:
        reasons = _classify_content(document)
        if reasons:
            raise ContentPolicyError(tuple(reasons))

    def _commit_locked(
        self, request: TransactionRequest, current: Mapping[str, dict[str, Any]]
    ) -> CommitReceipt:
        manifest = self._load_manifest()
        intent_digest = self._intent_digest(request)
        changed: list[_PreparedObject] = []
        for mutation in request.mutations:
            desired = self._normalize_desired_document(mutation)
            path = self._object_path_for_document(desired)
            before_document = current.get(mutation.object_id)
            before_bytes = _read_bytes_if_exists(path, self.root)
            if before_document is None:
                if before_bytes is not None:
                    raise IntegrityError("object path exists without an object record")
            else:
                expected_before = _render_markdown_object(before_document)
                if before_bytes != expected_before:
                    raise IntegrityError("object bytes do not match logical authority")
            changed.append(
                _PreparedObject(
                    object_id=mutation.object_id,
                    relative_path=str(path.relative_to(self.root)),
                    before_bytes=before_bytes,
                    after_bytes=_render_markdown_object(desired),
                    before_hash=before_document["content_hash"] if before_document else None,
                    after_hash=desired["content_hash"],
                    revision=desired["revision"],
                )
            )

        event_bytes, event_head, event_sequence = self._build_events(
            request, changed, manifest, intent_digest=intent_digest
        )
        intended_manifest = dict(manifest)
        intended_manifest.update(
            {
                "mutation_epoch": manifest["mutation_epoch"] + 1,
                "event_sequence": event_sequence,
                "event_head": event_head,
                "object_count": len(current) + sum(item.before_bytes is None for item in changed),
            }
        )
        _validate_manifest(intended_manifest)
        receipt = CommitReceipt(
            transaction_id=request.transaction_id,
            idempotency_key=request.idempotency_key,
            store_id=request.store_id,
            mutation_epoch=intended_manifest["mutation_epoch"],
            event_head=event_head,
            changed_objects=tuple(
                ChangedObject(item.object_id, item.revision, item.after_hash) for item in changed
            ),
            committed_at=self._now_rfc3339(),
        )
        journal = self._build_journal(
            request=request,
            changed=changed,
            event_bytes=event_bytes,
            event_offset=self._event_offset(),
            before_manifest=canonical_jcs_bytes(manifest),
            after_manifest=canonical_jcs_bytes(intended_manifest),
            receipt=receipt,
            intent_digest=intent_digest,
        )
        journal_path = self._journal_path(request.transaction_id)
        if _read_bytes_if_exists(journal_path, self.root) is not None:
            # A same-ID prepared journal is unsafe; a matching committed record
            # was handled by idempotency lookup before this method.
            raise IntegrityError("transaction journal already exists")
        _atomic_write(journal_path, canonical_jcs_bytes(journal), self.root)
        self._inject_failure("after_prepare")

        for item in changed:
            _atomic_write(self.root / item.relative_path, item.after_bytes, self.root)
        self._inject_failure("after_objects")

        _append_fsync(self._events_path, event_bytes, self.root)
        self._inject_failure("after_events")

        _atomic_write(self._manifest_path, canonical_jcs_bytes(intended_manifest), self.root)
        self._inject_failure("after_manifest")

        journal["status"] = "committed"
        journal["committed_at"] = receipt.committed_at
        _atomic_write(journal_path, canonical_jcs_bytes(journal), self.root)
        self.invalidate_derived("authoritative mutation")
        # A committed receipt is returned only after a local fast integrity pass.
        self._refresh_health()
        self._assert_healthy_for_write()
        return receipt

    def _build_events(
        self,
        request: TransactionRequest,
        changed: Sequence["_PreparedObject"],
        manifest: Mapping[str, Any],
        *,
        intent_digest: str,
    ) -> tuple[bytes, str, int]:
        previous_hash = manifest["event_head"]
        sequence = manifest["event_sequence"]
        records: list[dict[str, Any]] = []
        actor = _actor_id(request.actor)
        for item in changed:
            sequence += 1
            event = {
                "schema_version": 2,
                "event_id": self._new_id("evt"),
                "sequence": sequence,
                "store_id": request.store_id,
                "transaction_id": request.transaction_id,
                "event_type": "object_created" if item.before_hash is None else "object_updated",
                "occurred_at": self._now_rfc3339(),
                "actor": actor,
                "object_id": item.object_id,
                "from_revision": None if item.before_hash is None else item.revision - 1,
                "to_revision": item.revision,
                "before_hash": item.before_hash,
                "after_hash": item.after_hash,
                "reason": request.reason,
                "prev_event_hash": previous_hash,
            }
            event["event_hash"] = _event_hash(event)
            previous_hash = event["event_hash"]
            records.append(event)
        sequence += 1
        transaction_event = {
            "schema_version": 2,
            "event_id": self._new_id("evt"),
            "sequence": sequence,
            "store_id": request.store_id,
            "transaction_id": request.transaction_id,
            "event_type": "transaction_committed",
            "occurred_at": self._now_rfc3339(),
            "actor": actor,
            "object_id": None,
            "from_revision": None,
            "to_revision": None,
            # The terminal event has no object snapshot before it. Its before
            # hash instead commits the durable idempotency record to this block.
            "before_hash": _transaction_intent_commitment(
                request.transaction_id,
                request.store_id,
                request.idempotency_key,
                intent_digest,
            ),
            "after_hash": _transaction_after_hash(changed),
            "reason": request.reason,
            "prev_event_hash": previous_hash,
        }
        transaction_event["event_hash"] = _event_hash(transaction_event)
        records.append(transaction_event)
        return b"".join(canonical_jcs_bytes(record) + b"\n" for record in records), transaction_event[
            "event_hash"
        ], sequence

    def _build_journal(
        self,
        *,
        request: TransactionRequest,
        changed: Sequence["_PreparedObject"],
        event_bytes: bytes,
        event_offset: int,
        before_manifest: bytes,
        after_manifest: bytes,
        receipt: CommitReceipt,
        intent_digest: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": "prepared",
            "transaction_id": request.transaction_id,
            "idempotency_key": request.idempotency_key,
            "intent_digest": intent_digest,
            "store_id": request.store_id,
            "confirmation_id": _confirmation_id(request.confirmation),
            "prepared_at": self._now_rfc3339(),
            "event_offset": event_offset,
            "event_bytes_b64": _b64(event_bytes),
            "event_sha256": sha256_bytes(event_bytes),
            "manifest_before_b64": _b64(before_manifest),
            "manifest_after_b64": _b64(after_manifest),
            "objects": [item.to_journal_dict() for item in changed],
            "receipt": receipt.to_dict(),
        }

    def _find_idempotency(self, request: TransactionRequest) -> CommitReceipt | None:
        matching: list[dict[str, Any]] = []
        for path in sorted(self._transactions_path.glob("*.json")):
            journal = self._read_journal(path, required=False)
            if journal is None:
                continue
            if journal.get("idempotency_key") == request.idempotency_key:
                matching.append(journal)
        if not matching:
            return None
        intent = self._intent_digest(request)
        if len(matching) != 1:
            raise IntegrityError("idempotency key has multiple durable records")
        journal = matching[0]
        if journal.get("status") != "committed":
            raise IdempotencyKeyReusedError("idempotency key is bound to an incomplete transaction")
        if journal.get("intent_digest") != intent:
            raise IdempotencyKeyReusedError()
        receipt = CommitReceipt.from_dict(journal.get("receipt", {}), idempotent=True)
        if receipt.idempotency_key != request.idempotency_key:
            raise IntegrityError("idempotency journal receipt does not match key")
        return receipt

    def _confirmation_intents(self, confirmation_id: str) -> set[str]:
        found: set[str] = set()
        for path in self._transactions_path.glob("*.json"):
            journal = self._read_journal(path, required=False)
            if journal and journal.get("status") == "committed" and journal.get("confirmation_id") == confirmation_id:
                digest = journal.get("intent_digest")
                if isinstance(digest, str):
                    found.add(digest)
        return found

    def _intent_digest(self, request: TransactionRequest) -> str:
        # Transaction IDs are retry transport IDs.  The idempotency binding is
        # the actual requested semantic intent, excluding that transport ID.
        return sha256_hex(
            {
                "schema_version": request.schema_version,
                "store_id": request.store_id,
                "actor": _jsonable(request.actor),
                "confirmation": _jsonable(request.confirmation),
                "mutations": [
                    {
                        "operation": mutation.operation,
                        "object_id": mutation.object_id,
                        "expected_revision": mutation.expected_revision,
                        "expected_content_hash": mutation.expected_content_hash,
                        "desired_object": _jsonable(mutation.desired_object),
                    }
                    for mutation in request.mutations
                ],
                "reason": request.reason,
            }
        )

    def _confirmation_action_digest(self, request: TransactionRequest) -> str:
        """Bind a user confirmation to the semantic mutation, never its retry ID."""

        return sha256_hex(
            {
                "schema_version": request.schema_version,
                "store_id": request.store_id,
                "actor": _jsonable(request.actor),
                "mutations": [
                    {
                        "operation": mutation.operation,
                        "object_id": mutation.object_id,
                        "expected_revision": mutation.expected_revision,
                        "expected_content_hash": mutation.expected_content_hash,
                        "desired_object": _jsonable(mutation.desired_object),
                    }
                    for mutation in request.mutations
                ],
                "reason": request.reason,
            }
        )

    def _journal_path(self, transaction_id: str) -> Path:
        match = _TRANSACTION_ID.fullmatch(transaction_id)
        if match is None:
            raise PathUnsafeError("unsafe transaction journal name")
        return _safe_child(self.root, Path("runtime") / "transactions" / f"txn-{match.group(1)}.json")

    def _read_journal(self, path: Path, *, required: bool) -> dict[str, Any] | None:
        try:
            raw = _read_bytes_if_exists(path, self.root)
        except StorageError:
            if required:
                raise
            return None
        if raw is None:
            if required:
                raise IntegrityError("transaction journal is missing")
            return None
        try:
            value = parse_strict_json(raw.decode("utf-8"))
        except (UnicodeError, StorageError) as error:
            if required:
                raise IntegrityError("transaction journal is unreadable") from error
            return None
        if not isinstance(value, dict):
            if required:
                raise IntegrityError("transaction journal is not an object")
            return None
        return value

    def _prepared_journals(
        self, *, raise_on_invalid: bool = True
    ) -> list[tuple[Path, dict[str, Any] | None]]:
        journals: list[tuple[Path, dict[str, Any] | None]] = []
        for path in sorted(self._transactions_path.glob("*.json")):
            journal = self._read_journal(path, required=raise_on_invalid)
            if journal is None:
                if not raise_on_invalid:
                    journals.append((path, None))
                continue
            if journal.get("status") == "prepared":
                journals.append((path, journal))
        return journals

    def _recover_prepared_journal(self, path: Path, journal: Mapping[str, Any]) -> str:
        _validate_journal(journal, self.root)
        objects = [_PreparedObject.from_journal_dict(item) for item in journal["objects"]]
        event_bytes = _unb64(journal["event_bytes_b64"])
        if sha256_bytes(event_bytes) != journal["event_sha256"]:
            raise IntegrityError("prepared journal event bytes are corrupt")
        before_manifest = _unb64(journal["manifest_before_b64"])
        after_manifest = _unb64(journal["manifest_after_b64"])
        event_offset = journal["event_offset"]

        object_states = [_prepared_object_state(self.root / item.relative_path, item, self.root) for item in objects]
        event_state = _prepared_event_state(self._events_path, event_offset, event_bytes, self.root)
        manifest_state = _prepared_file_state(
            self._manifest_path, before_manifest, after_manifest, self.root
        )
        all_after = all(state == "after" for state in object_states) and event_state == "after" and manifest_state == "after"
        if all_after:
            committed = dict(journal)
            committed["status"] = "committed"
            committed["committed_at"] = committed.get("committed_at", self._now_rfc3339())
            _atomic_write(path, canonical_jcs_bytes(committed), self.root)
            self.invalidate_derived("recovered committed transaction")
            return "committed"

        # Any known prefix/mix of a prepared transaction rolls back to the exact
        # before bytes.  Unknown bytes are tampering, not a recovery candidate.
        if any(state == "unknown" for state in object_states) or event_state == "unknown" or manifest_state == "unknown":
            raise IntegrityError("prepared transaction diverged from its journal")
        for item in objects:
            target = self.root / item.relative_path
            if item.before_bytes is None:
                if _read_bytes_if_exists(target, self.root) is not None:
                    _safe_unlink(target, self.root)
            else:
                _atomic_write(target, item.before_bytes, self.root)
        _truncate_fsync(self._events_path, event_offset, self.root)
        _atomic_write(self._manifest_path, before_manifest, self.root)
        rolled_back = dict(journal)
        rolled_back["status"] = "rolled_back"
        rolled_back["recovered_at"] = self._now_rfc3339()
        _atomic_write(path, canonical_jcs_bytes(rolled_back), self.root)
        self.invalidate_derived("recovered rolled back transaction")
        return "rolled_back"

    def _refresh_health(self) -> None:
        try:
            self._assert_layout_exists()
            manifest = self._load_manifest()
            documents = self._load_current_objects()
            if manifest["object_count"] != len(documents):
                raise IntegrityError("manifest object count does not match authority files")
            events = self._validate_event_ledger(manifest)
            self._validate_event_object_snapshot(events, documents)
            self._validate_journal_records(manifest, events)
            if self._prepared_journals():
                self._degrade("TRANSACTION_INCOMPLETE")
                return
        except StorageError as error:
            self._degrade(error.code)
            return
        except Exception:
            self._degrade("INTEGRITY_FAILED")
            return
        self._health = StoreHealth.HEALTHY
        self._health_reason = None

    def _validate_journal_records(
        self, manifest: Mapping[str, Any], events: Sequence[Mapping[str, Any]]
    ) -> None:
        ledger_transactions = _ledger_transactions(events)
        committed_records: set[str] = set()
        for path in self._transactions_path.glob("*.json"):
            journal = self._read_journal(path, required=True)
            if journal is None:
                raise IntegrityError("transaction journal disappeared")
            status = journal.get("status")
            if status not in {"committed", "rolled_back"}:
                if status != "prepared":
                    raise IntegrityError("transaction journal has an unknown status")
            _validate_journal(
                journal,
                self.root,
                allowed_statuses={"prepared", "committed", "rolled_back"},
            )
            if path != self._journal_path(journal["transaction_id"]):
                raise IntegrityError("transaction journal filename does not match its identity")
            if journal["store_id"] != manifest["store_id"]:
                raise IntegrityError("transaction journal belongs to another store")
            if status == "committed":
                transaction_events = ledger_transactions.get(journal["transaction_id"])
                if transaction_events is None:
                    raise IntegrityError("committed journal has no committed ledger transaction")
                _validate_committed_journal(
                    journal,
                    manifest,
                    transaction_events,
                    list(ledger_transactions).index(journal["transaction_id"]) + 1,
                )
                committed_records.add(journal["transaction_id"])
            elif status == "rolled_back" and journal["transaction_id"] in ledger_transactions:
                raise IntegrityError("rolled back journal still has ledger events")

        missing = set(ledger_transactions).difference(committed_records)
        if missing:
            raise IntegrityError("committed ledger transaction lacks a durable receipt")

    def _validate_event_ledger(self, manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
        try:
            raw = _read_bytes_if_exists(self._events_path, self.root)
            if raw is None:
                raise IntegrityError("required authority file is missing")
            events = parse_ndjson(raw.decode("utf-8"))
        except (UnicodeError, StorageError) as error:
            raise IntegrityError("event ledger is not strict NDJSON") from error
        previous_hash: str | None = None
        sequence = 0
        event_ids: set[str] = set()
        for event in events:
            _validate_event(event)
            if event["event_id"] in event_ids:
                raise IntegrityError("event ID is duplicated in the ledger")
            event_ids.add(event["event_id"])
            if event["store_id"] != manifest["store_id"]:
                raise IntegrityError("event belongs to another store")
            if event["sequence"] != sequence + 1 or event["prev_event_hash"] != previous_hash:
                raise IntegrityError("event sequence or hash chain diverged")
            if _event_hash(event) != event["event_hash"]:
                raise IntegrityError("event hash does not match event body")
            sequence = event["sequence"]
            previous_hash = event["event_hash"]
        if manifest["event_sequence"] != sequence or manifest["event_head"] != previous_hash:
            raise IntegrityError("manifest event head does not match ledger")
        transactions = _ledger_transactions(events)
        for transaction_events in transactions.values():
            if transaction_events[-1]["event_type"] != "transaction_committed":
                raise IntegrityError("transaction lacks terminal committed event")
            changed = [event for event in transaction_events if event["event_type"] != "transaction_committed"]
            if not changed:
                raise IntegrityError("transaction has no authoritative object events")
            expected = _transaction_after_hash_from_events(changed)
            if transaction_events[-1]["after_hash"] != expected:
                raise IntegrityError("transaction commit event does not bind changed objects")
        if manifest["mutation_epoch"] != len(transactions):
            raise IntegrityError("manifest mutation epoch does not match ledger transactions")
        return events

    def _validate_event_object_snapshot(
        self, events: Sequence[Mapping[str, Any]], documents: Mapping[str, Mapping[str, Any]]
    ) -> None:
        latest: dict[str, tuple[int, str]] = {}
        for event in events:
            if event["event_type"] == "transaction_committed":
                continue
            object_id = event["object_id"]
            after_hash = event["after_hash"]
            revision = event["to_revision"]
            if not isinstance(object_id, str) or not isinstance(after_hash, str) or not isinstance(revision, int):
                raise IntegrityError("object event has an invalid snapshot reference")
            previous = latest.get(object_id)
            if previous is None:
                if (
                    event["event_type"] != "object_created"
                    or event["from_revision"] is not None
                    or event["before_hash"] is not None
                    or revision != 1
                ):
                    raise IntegrityError("object ledger history does not begin with a create event")
            elif (
                event["event_type"] != "object_updated"
                or event["from_revision"] != previous[0]
                or event["before_hash"] != previous[1]
                or revision != previous[0] + 1
            ):
                raise IntegrityError("object ledger revision history is not contiguous")
            latest[object_id] = (revision, after_hash)
        if set(latest) != set(documents):
            raise IntegrityError("objects and committed event ledger diverged")
        for object_id, document in documents.items():
            revision, content_hash = latest[object_id]
            if document.get("revision") != revision or document.get("content_hash") != content_hash:
                raise IntegrityError("object revision does not match committed event ledger")

    def _collect_objects_for_lint(self) -> tuple[dict[str, dict[str, Any]], list[LintFinding]]:
        documents: dict[str, dict[str, Any]] = {}
        findings: list[LintFinding] = []
        objects_root = _safe_child(self.root, Path("objects"))
        for path in sorted(objects_root.rglob("*.md")):
            try:
                document = self._read_object_path(path)
                if document["id"] in documents:
                    raise IntegrityError("duplicate object ID")
                documents[document["id"]] = document
            except StorageError as error:
                code = "CONTENT_HASH_MISMATCH" if error.code == "CONTENT_HASH_MISMATCH" else "SCHEMA_INVALID"
                findings.append(LintFinding("error", code, "object failed authority validation"))
        return documents, findings

    def _collect_events_for_lint(self) -> tuple[list[dict[str, Any]], list[LintFinding]]:
        try:
            raw = _read_bytes_if_exists(self._events_path, self.root)
            if raw is None:
                raise IntegrityError("required authority file is missing")
            events = parse_ndjson(raw.decode("utf-8"))
            manifest = self._load_manifest()
            self._validate_event_ledger(manifest)
            return events, []
        except (UnicodeError, StorageError):
            return [], [LintFinding("error", "EVENT_CHAIN_BROKEN", "event ledger is invalid")]

    def _lint_relations(self, documents: Mapping[str, dict[str, Any]]) -> list[LintFinding]:
        try:
            self._validate_relations(documents)
        except RelationInvalidError as error:
            message = str(error)
            code = "RELATION_CYCLE" if "cycle" in message else "RELATION_TARGET_MISSING"
            return [LintFinding("error", code, "relation validation failed")]
        return []

    def _lint_manifest(
        self, manifest: Mapping[str, Any], documents: Mapping[str, Any], events: Sequence[Mapping[str, Any]]
    ) -> list[LintFinding]:
        if manifest.get("object_count") != len(documents):
            return [LintFinding("error", "STORE_MANIFEST_MISMATCH", "object count does not match manifest")]
        if not events and (manifest.get("event_sequence") != 0 or manifest.get("event_head") is not None):
            return [LintFinding("error", "STORE_MANIFEST_MISMATCH", "empty ledger does not match manifest")]
        return []

    def _object_path_for_id(self, object_id: str) -> Path:
        if not isinstance(object_id, str) or "/" in object_id or "\\" in object_id or ".." in object_id:
            raise PathUnsafeError("object ID cannot be used as a safe path")
        parts = object_id.split(":")
        if len(parts) < 4:
            raise PathUnsafeError("object ID does not have a safe namespace")
        kind = parts[-2]
        identifier = parts[-1]
        if kind not in _KIND_PLURALS or _UUID.fullmatch(identifier) is None:
            raise PathUnsafeError("object ID cannot map to a safe object path")
        return _safe_child(self.root, Path("objects") / _KIND_PLURALS[kind] / f"{identifier}.md")

    def _object_path_for_document(self, document: Mapping[str, Any]) -> Path:
        path = self._object_path_for_id(document.get("id"))
        kind = document.get("kind")
        if kind not in _KIND_PLURALS or path.parent.name != _KIND_PLURALS[kind]:
            raise PathUnsafeError("object kind does not have a safe folder")
        return path

    @contextmanager
    def _writer_lock(self) -> Iterator[None]:
        lock_path = _safe_child(self.root, Path("runtime") / "writer.lock")
        process_lock = _process_lock_for(self.root)
        with process_lock:
            with _open_secure_parent_fd(lock_path, self.root) as (parent_fd, name):
                flags = os.O_CREAT | os.O_RDWR | _nofollow_flag()
                try:
                    descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
                except OSError as error:
                    raise PathUnsafeError("writer lock cannot be opened safely") from error
                try:
                    _assert_secure_stat(os.fstat(descriptor), directory=False)
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    yield
                finally:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    finally:
                        os.close(descriptor)

    def _assert_healthy_for_write(self) -> None:
        if self._health != StoreHealth.HEALTHY:
            raise StoreDegradedError()

    def _assert_healthy_for_read(self) -> None:
        if self._health != StoreHealth.HEALTHY:
            raise StoreDegradedError("store is degraded; recover before authoritative reads")

    def _degrade(self, reason: str) -> None:
        self._health = StoreHealth.DEGRADED_READ_ONLY
        self._health_reason = reason

    def _inject_failure(self, stage: str) -> None:
        if self._failure_injector is not None:
            self._failure_injector(stage)

    def _now_rfc3339(self) -> str:
        if self._clock is None:
            return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if hasattr(self._clock, "now_rfc3339"):
            value = self._clock.now_rfc3339()
            if isinstance(value, str):
                return value
        value = self._clock.now() if hasattr(self._clock, "now") else self._clock()
        if not isinstance(value, datetime):
            raise StorageError("SCHEMA_INVALID", "clock must return a datetime")
        if value.tzinfo is None:
            raise StorageError("SCHEMA_INVALID", "clock must return an aware datetime")
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _new_id(self, prefix: str) -> str:
        if self._id_factory is None:
            return f"{prefix}:{uuid.uuid4()}"
        try:
            generated = self._id_factory(prefix)
        except TypeError:
            generated = self._id_factory()
        value = str(generated)
        if value.startswith(f"{prefix}:"):
            return value
        if _UUID.fullmatch(value):
            return f"{prefix}:{value}"
        raise StorageError("SCHEMA_INVALID", "id_factory returned an invalid ID")


@dataclass(frozen=True)
class _PreparedObject:
    object_id: str
    relative_path: str
    before_bytes: bytes | None
    after_bytes: bytes
    before_hash: str | None
    after_hash: str
    revision: int

    def to_journal_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "relative_path": self.relative_path,
            "before_bytes_b64": None if self.before_bytes is None else _b64(self.before_bytes),
            "after_bytes_b64": _b64(self.after_bytes),
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "revision": self.revision,
        }

    @classmethod
    def from_journal_dict(cls, value: Mapping[str, Any]) -> "_PreparedObject":
        try:
            before = value.get("before_bytes_b64")
            return cls(
                object_id=value["object_id"],
                relative_path=value["relative_path"],
                before_bytes=None if before is None else _unb64(before),
                after_bytes=_unb64(value["after_bytes_b64"]),
                before_hash=value.get("before_hash"),
                after_hash=value["after_hash"],
                revision=value["revision"],
            )
        except (KeyError, TypeError, StorageError) as error:
            raise IntegrityError("prepared journal object record is invalid") from error


def _validate_store_root(root: Path, *, create: bool) -> Path:
    if not isinstance(root, Path):
        root = Path(root)
    repository = repository_root().resolve()
    lexical = root if root.is_absolute() else Path.cwd() / root
    try:
        lexical.relative_to(repository)
    except ValueError as error:
        raise PathUnsafeError("store root must be explicitly contained by this repository") from error
    _assert_no_symlink_path(lexical, repository)
    candidate = lexical.resolve(strict=False)
    try:
        candidate.relative_to(repository)
    except ValueError as error:
        raise PathUnsafeError("store root must be explicitly contained by this repository") from error
    if candidate == repository:
        raise PathUnsafeError("repository root cannot itself be an authority store")
    if not create and not candidate.is_dir():
        raise IntegrityError("store root does not exist")
    _assert_no_symlink_path(candidate, repository)
    if candidate.exists():
        _assert_secure_directory(candidate)
    return candidate


def _safe_child(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise PathUnsafeError()
    candidate = root / relative
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
    except ValueError as error:
        raise PathUnsafeError() from error
    _assert_no_symlink_path(candidate, root)
    return candidate


def _assert_no_symlink_path(path: Path, root: Path) -> None:
    root = root.resolve(strict=False)
    candidate = path.resolve(strict=False)
    # ``resolve`` above can hide an already-followed symlink, so inspect lexical
    # components from the guaranteed repository-contained root as well.
    try:
        lexical = path if path.is_absolute() else root / path
        lexical.relative_to(root)
    except ValueError:
        # For an absolute root itself, only inspect it and its parents below.
        lexical = path
    parts: list[Path] = []
    current = lexical
    while True:
        parts.append(current)
        if current == root or current.parent == current:
            break
        current = current.parent
    for component in reversed(parts):
        if component.exists() or component.is_symlink():
            try:
                mode = os.lstat(component).st_mode
            except OSError as error:
                raise PathUnsafeError("store path cannot be inspected") from error
            if stat.S_ISLNK(mode):
                raise PathUnsafeError("symlinks are not permitted in store paths")


def _assert_parent_safe(path: Path, root: Path) -> None:
    parent = path.parent
    _assert_no_symlink_path(parent, root)
    _assert_secure_directory(parent)


def _ensure_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as error:
        raise PathUnsafeError("store directory cannot be created") from error
    _assert_secure_directory(path)


def _assert_secure_directory(path: Path) -> None:
    """Require a user-owned, non-group-writable authority directory."""

    try:
        metadata = os.lstat(path)
    except FileNotFoundError as error:
        raise IntegrityError("store layout is incomplete") from error
    except OSError as error:
        raise PathUnsafeError("store directory cannot be inspected") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PathUnsafeError("store path is not a real directory")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise PathUnsafeError("store path is not owned by the current user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PathUnsafeError("store path must be private to the current user")


def _assert_regular_file(path: Path, root: Path, *, allow_missing: bool) -> None:
    _assert_parent_safe(path, root)
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        if allow_missing:
            return
        raise IntegrityError("required authority file is missing")
    except OSError as error:
        raise PathUnsafeError("authority file cannot be inspected") from error
    if stat.S_ISLNK(mode):
        raise PathUnsafeError("symlinked authority file is not allowed")
    if not stat.S_ISREG(mode):
        raise PathUnsafeError("authority path is not a regular file")
    if hasattr(os, "geteuid") and os.lstat(path).st_uid != os.geteuid():
        raise PathUnsafeError("authority file is not owned by the current user")
    if stat.S_IMODE(mode) & 0o077:
        raise PathUnsafeError("authority file must be private to the current user")


def _assert_secure_stat(metadata: os.stat_result, *, directory: bool) -> None:
    mode = metadata.st_mode
    expected = stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
    if not expected or stat.S_ISLNK(mode):
        raise PathUnsafeError("authority path has an unsafe file type")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise PathUnsafeError("authority path is not owned by the current user")
    if stat.S_IMODE(mode) & 0o077:
        raise PathUnsafeError("authority path must be private to the current user")


def _nofollow_flag() -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise PathUnsafeError("this platform cannot safely open authority paths")
    return os.O_NOFOLLOW


def _relative_under_root(path: Path, root: Path) -> Path:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise PathUnsafeError("authority path escapes its store") from error
    if not relative.parts or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise PathUnsafeError("authority path is not a safe child")
    return relative


@contextmanager
def _open_secure_parent_fd(path: Path, root: Path) -> Iterator[tuple[int, str]]:
    """Open the parent through no-follow directory descriptors, not path strings."""

    relative = _relative_under_root(path, root)
    flags = os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag()
    try:
        descriptor = os.open(root, flags)
    except OSError as error:
        raise PathUnsafeError("store root cannot be opened safely") from error
    try:
        _assert_secure_stat(os.fstat(descriptor), directory=True)
        for component in relative.parts[:-1]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as error:
                raise PathUnsafeError("authority parent cannot be opened safely") from error
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


@contextmanager
def _open_secure_directory_fd(path: Path) -> Iterator[int]:
    flags = os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag()
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PathUnsafeError("authority directory cannot be opened safely") from error
    try:
        _assert_secure_stat(os.fstat(descriptor), directory=True)
        yield descriptor
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, data: bytes, root: Path) -> None:
    temporary_name: str | None = None
    descriptor: int | None = None
    with _open_secure_parent_fd(path, root) as (parent_fd, name):
        try:
            try:
                _assert_secure_stat(os.stat(name, dir_fd=parent_fd, follow_symlinks=False), directory=False)
            except FileNotFoundError:
                pass
            temporary_name = f".{name}.tmp-{uuid.uuid4().hex}"
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | _nofollow_flag()
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
            _assert_secure_stat(os.fstat(descriptor), directory=False)
            _write_all(descriptor, data)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError as error:
            raise IntegrityError("atomic authority write failed") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass


def _append_fsync(path: Path, data: bytes, root: Path) -> None:
    with _open_secure_parent_fd(path, root) as (parent_fd, name):
        flags = os.O_WRONLY | os.O_APPEND | _nofollow_flag()
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
            try:
                _assert_secure_stat(os.fstat(descriptor), directory=False)
                _write_all(descriptor, data)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(parent_fd)
        except OSError as error:
            raise IntegrityError("event ledger append failed") from error


def _truncate_fsync(path: Path, size: int, root: Path) -> None:
    if not isinstance(size, int) or size < 0:
        raise IntegrityError("journal has an invalid event offset")
    with _open_secure_parent_fd(path, root) as (parent_fd, name):
        flags = os.O_WRONLY | _nofollow_flag()
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
            try:
                _assert_secure_stat(os.fstat(descriptor), directory=False)
                os.ftruncate(descriptor, size)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(parent_fd)
        except OSError as error:
            raise IntegrityError("event ledger rollback failed") from error


def _safe_unlink(path: Path, root: Path) -> None:
    with _open_secure_parent_fd(path, root) as (parent_fd, name):
        try:
            _assert_secure_stat(os.stat(name, dir_fd=parent_fd, follow_symlinks=False), directory=False)
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError as error:
            raise IntegrityError("prepared object rollback failed") from error


def _read_bytes_if_exists(path: Path, root: Path) -> bytes | None:
    with _open_secure_parent_fd(path, root) as (parent_fd, name):
        try:
            descriptor = os.open(name, os.O_RDONLY | _nofollow_flag(), dir_fd=parent_fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise IntegrityError("authority object cannot be opened safely") from error
        try:
            _assert_secure_stat(os.fstat(descriptor), directory=False)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        except OSError as error:
            raise IntegrityError("authority object cannot be read") from error
        finally:
            os.close(descriptor)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    try:
        with _open_secure_directory_fd(path) as descriptor:
            os.fsync(descriptor)
    except (OSError, StorageError):
        # Some filesystems do not allow directory fsync.  File fsync has still
        # happened; refusing to silently skip it on normal POSIX filesystems is
        # handled by the caller's atomic operation error path where available.
        pass


def _remove_tree_contents(directory: Path, root: Path) -> None:
    _assert_no_symlink_path(directory, root)
    if not directory.is_dir():
        raise PathUnsafeError("derived path is not a directory")
    for entry in list(directory.iterdir()):
        try:
            mode = os.lstat(entry).st_mode
        except OSError as error:
            raise PathUnsafeError("derived entry cannot be inspected") from error
        if stat.S_ISLNK(mode) or stat.S_ISREG(mode):
            entry.unlink()
        elif stat.S_ISDIR(mode):
            _remove_tree_contents(entry, root)
            entry.rmdir()
        else:
            raise PathUnsafeError("derived state contains an unsupported filesystem entry")
    _fsync_directory(directory)


def _process_lock_for(root: Path) -> threading.RLock:
    key = str(root.resolve())
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


def _validate_store_identity(
    store_id: Any,
    project_id: Any,
    *,
    error_type: type[StorageError] = IntegrityError,
) -> None:
    if store_id == "knowledge:global":
        if project_id is not None:
            _raise_store_identity_error(error_type, "knowledge store cannot carry a project ID")
        return
    match = _PROJECT_STORE_ID.fullmatch(store_id) if isinstance(store_id, str) else None
    if match is None or project_id != match.group(1):
        _raise_store_identity_error(error_type, "store ID and project ID are inconsistent")


def _raise_store_identity_error(error_type: type[StorageError], message: str) -> None:
    if error_type is StorageError:
        raise StorageError("SCHEMA_INVALID", message)
    raise error_type(message)


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "store_id",
        "created_at",
        "mutation_epoch",
        "event_sequence",
        "event_head",
        "object_count",
        "schema_registry_version",
        "writer_version",
        "project_id",
    }
    if set(manifest) != required:
        raise IntegrityError("manifest is missing required fields")
    if manifest["schema_version"] != 2 or not isinstance(manifest["store_id"], str):
        raise IntegrityError("manifest has an unsupported identity")
    _validate_store_identity(manifest["store_id"], manifest["project_id"])
    if not isinstance(manifest["created_at"], str) or not isinstance(
        manifest["schema_registry_version"], str
    ) or not isinstance(manifest["writer_version"], str):
        raise IntegrityError("manifest has invalid metadata")
    for key in ("mutation_epoch", "event_sequence", "object_count"):
        if not isinstance(manifest[key], int) or isinstance(manifest[key], bool) or manifest[key] < 0:
            raise IntegrityError("manifest has an invalid counter")
    event_head = manifest["event_head"]
    if event_head is not None and (not isinstance(event_head, str) or _HEX_HASH.fullmatch(event_head) is None):
        raise IntegrityError("manifest has an invalid event head")
    if manifest["event_sequence"] == 0 and event_head is not None:
        raise IntegrityError("empty manifest ledger has an event head")
    try:
        parse_rfc3339_utc(manifest["created_at"])
    except Exception as error:
        raise IntegrityError("manifest has an invalid timestamp") from error


def _validate_event(event: Mapping[str, Any]) -> None:
    try:
        validate_named_document("event-v2", dict(event))
    except Exception as error:
        raise IntegrityError("event does not satisfy the event contract") from error
    required = {
        "schema_version",
        "event_id",
        "sequence",
        "store_id",
        "transaction_id",
        "event_type",
        "occurred_at",
        "actor",
        "object_id",
        "from_revision",
        "to_revision",
        "before_hash",
        "after_hash",
        "reason",
        "prev_event_hash",
        "event_hash",
    }
    if set(event) != required:
        raise IntegrityError("event has an invalid shape")
    if event["schema_version"] != 2 or _TRANSACTION_ID.fullmatch(event["transaction_id"]) is None:
        raise IntegrityError("event has an invalid identity")
    if not isinstance(event["event_id"], str) or not event["event_id"].startswith("evt:") or _UUID.fullmatch(
        event["event_id"][4:]
    ) is None:
        raise IntegrityError("event has an invalid event ID")
    if event["event_type"] not in {
        "object_created",
        "object_updated",
        "lifecycle_changed",
        "relation_added",
        "relation_removed",
        "decision_confirmed",
        "object_migrated",
        "source_captured",
        "source_compiled",
        "promotion_proposed",
        "promotion_accepted",
        "promotion_rejected",
        "transaction_committed",
    }:
        raise IntegrityError("event has an unsupported type")
    if not isinstance(event["store_id"], str) or not isinstance(event["actor"], str):
        raise IntegrityError("event has an invalid actor or store")
    if not isinstance(event["reason"], str) or not event["reason"]:
        raise IntegrityError("event has an invalid reason")
    if not isinstance(event["sequence"], int) or event["sequence"] < 1:
        raise IntegrityError("event has an invalid sequence")
    if not isinstance(event["event_hash"], str) or _HEX_HASH.fullmatch(event["event_hash"]) is None:
        raise IntegrityError("event has an invalid hash")
    for name in ("prev_event_hash", "before_hash", "after_hash"):
        value = event[name]
        if value is not None and (not isinstance(value, str) or _HEX_HASH.fullmatch(value) is None):
            raise IntegrityError("event has an invalid linked hash")
    if event["event_type"] == "transaction_committed":
        if (
            event["object_id"] is not None
            or event["from_revision"] is not None
            or event["to_revision"] is not None
            or not isinstance(event["before_hash"], str)
            or not isinstance(event["after_hash"], str)
        ):
            raise IntegrityError("terminal transaction event has an invalid snapshot")
    elif (
        not isinstance(event["object_id"], str)
        or not isinstance(event["to_revision"], int)
        or not isinstance(event["after_hash"], str)
    ):
        raise IntegrityError("object event has an invalid snapshot")


def _event_hash(event: Mapping[str, Any]) -> str:
    logical = dict(event)
    logical.pop("event_hash", None)
    return sha256_hex(logical)


def _transaction_after_hash(changed: Sequence[_PreparedObject]) -> str:
    return sha256_hex(
        [
            {"id": item.object_id, "revision": item.revision, "content_hash": item.after_hash}
            for item in sorted(changed, key=lambda item: item.object_id)
        ]
    )


def _transaction_intent_commitment(
    transaction_id: str,
    store_id: str,
    idempotency_key: str,
    intent_digest: str,
) -> str:
    """Commit journal identity to the immutable terminal ledger event."""

    return sha256_hex(
        {
            "transaction_id": transaction_id,
            "store_id": store_id,
            "idempotency_key": idempotency_key,
            "intent_digest": intent_digest,
        }
    )


def _transaction_after_hash_from_events(events: Sequence[Mapping[str, Any]]) -> str:
    return sha256_hex(
        [
            {
                "id": event["object_id"],
                "revision": event["to_revision"],
                "content_hash": event["after_hash"],
            }
            for event in sorted(events, key=lambda item: item["object_id"])
        ]
    )


def _has_directed_cycle(adjacency: Mapping[str, set[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for child in adjacency.get(node, set()):
            if walk(child):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(walk(node) for node in adjacency)


def _render_markdown_object(document: Mapping[str, Any]) -> bytes:
    if "body" not in document or not isinstance(document["body"], str):
        raise StorageError("SCHEMA_INVALID", "logical object body must be text")
    header = {key: value for key, value in document.items() if key != "body"}
    lines = ["---"]
    for key in sorted(header):
        lines.extend(_emit_yaml_field(str(key), header[key], 0))
    lines.append("---")
    rendered = "\n".join(lines) + "\n" + document["body"]
    return rendered.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _emit_yaml_field(key: str, value: Any, indent: int) -> list[str]:
    prefix = " " * indent + key + ":"
    if isinstance(value, Mapping):
        if not value:
            return [prefix + " {}"]
        lines = [prefix]
        for child_key in sorted(value):
            lines.extend(_emit_yaml_field(str(child_key), value[child_key], indent + 2))
        return lines
    if isinstance(value, (list, tuple)):
        if not value:
            return [prefix + " []"]
        lines = [prefix]
        for item in value:
            lines.extend(_emit_yaml_list_item(item, indent + 2))
        return lines
    return [prefix + " " + _emit_yaml_scalar(value)]


def _emit_yaml_list_item(value: Any, indent: int) -> list[str]:
    prefix = " " * indent + "-"
    if isinstance(value, Mapping):
        if not value:
            return [prefix + " {}"]
        keys = sorted(value)
        first = keys[0]
        first_value = value[first]
        if isinstance(first_value, (Mapping, list, tuple)):
            lines = [prefix, *_emit_yaml_field(str(first), first_value, indent + 2)]
        else:
            lines = [prefix + " " + str(first) + ": " + _emit_yaml_scalar(first_value)]
        for child_key in keys[1:]:
            lines.extend(_emit_yaml_field(str(child_key), value[child_key], indent + 2))
        return lines
    if isinstance(value, (list, tuple)):
        if not value:
            return [prefix + " []"]
        lines = [prefix]
        for child in value:
            lines.extend(_emit_yaml_list_item(child, indent + 2))
        return lines
    return [prefix + " " + _emit_yaml_scalar(value)]


def _emit_yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return canonical_jcs_bytes(value).decode("ascii")
    if isinstance(value, str):
        # JSON strings are a supported YAML-subset quoted scalar and preserve
        # control characters without creating ambiguous physical lines.
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    raise StorageError("SCHEMA_INVALID", "cannot serialize non-JSON YAML scalar")


def _prepared_object_state(path: Path, item: _PreparedObject, root: Path) -> str:
    actual = _read_bytes_if_exists(path, root)
    if actual == item.after_bytes:
        return "after"
    if actual == item.before_bytes:
        return "before"
    return "unknown"


def _prepared_file_state(path: Path, before: bytes, after: bytes, root: Path) -> str:
    actual = _read_bytes_if_exists(path, root)
    if actual == after:
        return "after"
    if actual == before:
        return "before"
    return "unknown"


def _prepared_event_state(path: Path, offset: int, expected: bytes, root: Path) -> str:
    actual = _read_bytes_if_exists(path, root)
    if actual is None or not isinstance(offset, int) or offset < 0 or len(actual) < offset:
        return "unknown"
    if len(actual) == offset:
        return "before"
    if len(actual) == offset + len(expected) and actual[offset:] == expected:
        return "after"
    return "unknown"


def _ledger_transactions(
    events: Sequence[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    """Return contiguous committed transaction blocks in ledger order."""

    transactions: dict[str, list[Mapping[str, Any]]] = {}
    active_id: str | None = None
    active_closed = False
    for event in events:
        transaction_id = event.get("transaction_id")
        if not isinstance(transaction_id, str):
            raise IntegrityError("event has an invalid transaction identity")
        if transaction_id != active_id:
            if active_id is not None and not active_closed:
                raise IntegrityError("transaction is missing a terminal commit event")
            if transaction_id in transactions:
                raise IntegrityError("transaction events are not contiguous")
            transactions[transaction_id] = []
            active_id = transaction_id
            active_closed = False
        if active_closed:
            raise IntegrityError("transaction has events after its terminal commit")
        transactions[transaction_id].append(event)
        if event.get("event_type") == "transaction_committed":
            active_closed = True
    if active_id is not None and not active_closed:
        raise IntegrityError("transaction is missing a terminal commit event")
    return transactions


def _validate_committed_journal(
    journal: Mapping[str, Any],
    manifest: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    mutation_epoch: int,
) -> None:
    """Bind a retained idempotency receipt to its exact committed ledger block."""

    if not events or events[-1].get("event_type") != "transaction_committed":
        raise IntegrityError("committed journal ledger block has no terminal event")
    receipt = CommitReceipt.from_dict(journal["receipt"])
    terminal = events[-1]
    changed_events = events[:-1]
    expected_objects = sorted(
        (
            event.get("object_id"),
            event.get("to_revision"),
            event.get("after_hash"),
        )
        for event in changed_events
    )
    actual_objects = sorted(
        (item.id, item.revision, item.content_hash) for item in receipt.changed_objects
    )
    if (
        receipt.transaction_id != journal["transaction_id"]
        or receipt.idempotency_key != journal["idempotency_key"]
        or receipt.store_id != manifest["store_id"]
        or receipt.status != "committed"
        or receipt.schema_version != 1
        or receipt.mutation_epoch != mutation_epoch
        or receipt.event_head != terminal.get("event_hash")
        or expected_objects != actual_objects
        or terminal.get("before_hash")
        != _transaction_intent_commitment(
            journal["transaction_id"],
            journal["store_id"],
            journal["idempotency_key"],
            journal["intent_digest"],
        )
    ):
        raise IntegrityError("committed journal receipt does not bind its ledger transaction")
    if not isinstance(receipt.committed_at, str):
        raise IntegrityError("committed journal receipt has an invalid timestamp")
    try:
        parse_rfc3339_utc(receipt.committed_at)
    except Exception as error:
        raise IntegrityError("committed journal receipt has an invalid timestamp") from error

    encoded_events = b"".join(canonical_jcs_bytes(event) + b"\n" for event in events)
    journal_events = _unb64(journal["event_bytes_b64"])
    if journal_events != encoded_events or sha256_bytes(journal_events) != journal["event_sha256"]:
        raise IntegrityError("committed journal event payload does not match the ledger")

    try:
        before_manifest = parse_strict_json(_unb64(journal["manifest_before_b64"]).decode("utf-8"))
        after_manifest = parse_strict_json(_unb64(journal["manifest_after_b64"]).decode("utf-8"))
    except (UnicodeError, StorageError) as error:
        raise IntegrityError("committed journal manifest payload is invalid") from error
    if not isinstance(before_manifest, Mapping) or not isinstance(after_manifest, Mapping):
        raise IntegrityError("committed journal manifest payload is invalid")
    _validate_manifest(before_manifest)
    _validate_manifest(after_manifest)
    if (
        before_manifest.get("store_id") != manifest["store_id"]
        or after_manifest.get("store_id") != manifest["store_id"]
        or before_manifest.get("mutation_epoch") != mutation_epoch - 1
        or after_manifest.get("mutation_epoch") != mutation_epoch
        or after_manifest.get("event_sequence") != terminal.get("sequence")
        or after_manifest.get("event_head") != terminal.get("event_hash")
    ):
        raise IntegrityError("committed journal manifest payload does not bind the ledger")


def _validate_journal(
    journal: Mapping[str, Any],
    root: Path,
    *,
    allowed_statuses: set[str] | None = None,
) -> None:
    required = {
        "schema_version",
        "status",
        "transaction_id",
        "idempotency_key",
        "intent_digest",
        "store_id",
        "confirmation_id",
        "prepared_at",
        "event_offset",
        "event_bytes_b64",
        "event_sha256",
        "manifest_before_b64",
        "manifest_after_b64",
        "objects",
        "receipt",
    }
    allowed_statuses = allowed_statuses or {"prepared"}
    allowed_fields = required | {"committed_at", "recovered_at"}
    if not required.issubset(journal) or set(journal).difference(allowed_fields):
        raise IntegrityError("prepared journal has an invalid shape")
    if journal.get("schema_version") != 1 or journal.get("status") not in allowed_statuses:
        raise IntegrityError("prepared journal has an invalid status")
    if journal["status"] == "committed":
        try:
            parse_rfc3339_utc(journal["committed_at"])
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError("committed journal has an invalid timestamp") from error
    if journal["status"] == "rolled_back":
        try:
            parse_rfc3339_utc(journal["recovered_at"])
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError("rolled back journal has an invalid timestamp") from error
    if not isinstance(journal["transaction_id"], str) or _TRANSACTION_ID.fullmatch(journal["transaction_id"]) is None:
        raise IntegrityError("prepared journal has an invalid transaction ID")
    if (
        not isinstance(journal["idempotency_key"], str)
        or not journal["idempotency_key"]
        or not isinstance(journal["intent_digest"], str)
        or _HEX_HASH.fullmatch(journal["intent_digest"]) is None
        or not isinstance(journal["store_id"], str)
        or not isinstance(journal["objects"], list)
        or not journal["objects"]
        or not isinstance(journal["event_offset"], int)
        or isinstance(journal["event_offset"], bool)
        or journal["event_offset"] < 0
        or (journal["confirmation_id"] is not None and not isinstance(journal["confirmation_id"], str))
    ):
        raise IntegrityError("prepared journal has an invalid payload")
    try:
        parse_rfc3339_utc(journal["prepared_at"])
    except Exception as error:
        raise IntegrityError("prepared journal has an invalid timestamp") from error
    event_bytes = _unb64(journal["event_bytes_b64"])
    if not isinstance(journal["event_sha256"], str) or sha256_bytes(event_bytes) != journal["event_sha256"]:
        raise IntegrityError("prepared journal has an invalid event digest")
    try:
        journal_events = parse_ndjson(event_bytes.decode("utf-8"))
    except (UnicodeError, StorageError) as error:
        raise IntegrityError("prepared journal has invalid event bytes") from error
    for event in journal_events:
        _validate_event(event)
    if not journal_events or journal_events[-1].get("event_type") != "transaction_committed":
        raise IntegrityError("prepared journal lacks a terminal transaction event")
    if any(event.get("transaction_id") != journal["transaction_id"] for event in journal_events):
        raise IntegrityError("prepared journal event belongs to another transaction")
    if any(event.get("event_type") == "transaction_committed" for event in journal_events[:-1]):
        raise IntegrityError("prepared journal has an early terminal transaction event")
    terminal = journal_events[-1]
    if terminal.get("before_hash") != _transaction_intent_commitment(
        journal["transaction_id"],
        journal["store_id"],
        journal["idempotency_key"],
        journal["intent_digest"],
    ):
        raise IntegrityError("prepared journal intent does not bind its terminal ledger event")
    try:
        before_manifest = parse_strict_json(_unb64(journal["manifest_before_b64"]).decode("utf-8"))
        after_manifest = parse_strict_json(_unb64(journal["manifest_after_b64"]).decode("utf-8"))
    except (UnicodeError, StorageError) as error:
        raise IntegrityError("prepared journal has invalid manifest bytes") from error
    if not isinstance(before_manifest, Mapping) or not isinstance(after_manifest, Mapping):
        raise IntegrityError("prepared journal has invalid manifest bytes")
    _validate_manifest(before_manifest)
    _validate_manifest(after_manifest)
    if before_manifest.get("store_id") != journal["store_id"] or after_manifest.get("store_id") != journal["store_id"]:
        raise IntegrityError("prepared journal manifest store does not match")
    receipt = CommitReceipt.from_dict(journal["receipt"])
    if (
        receipt.transaction_id != journal["transaction_id"]
        or receipt.idempotency_key != journal["idempotency_key"]
        or receipt.store_id != journal["store_id"]
        or receipt.status != "committed"
    ):
        raise IntegrityError("prepared journal receipt does not match its identity")
    for raw in journal["objects"]:
        item = _PreparedObject.from_journal_dict(raw)
        candidate = _safe_child(root, Path(item.relative_path))
        if candidate != root / item.relative_path or not item.relative_path.startswith("objects/"):
            raise IntegrityError("prepared journal object path is unsafe")


def _b64(value: bytes) -> str:
    return b64encode(value).decode("ascii")


def _unb64(value: Any) -> bytes:
    if not isinstance(value, str):
        raise IntegrityError("journal byte field is not text")
    try:
        return b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as error:
        raise IntegrityError("journal byte field is not base64") from error


def _actor_id(actor: Any) -> str:
    if isinstance(actor, str) and actor:
        return actor
    if isinstance(actor, Mapping) and isinstance(actor.get("actor_id"), str) and actor["actor_id"]:
        return actor["actor_id"]
    raise StorageError("SCHEMA_INVALID", "actor must be text or include actor_id")


def _confirmation_id(confirmation: Any) -> str | None:
    if isinstance(confirmation, Mapping):
        value = confirmation.get("confirmation_id")
        return value if isinstance(value, str) else None
    return None


def _call_authorizer(authorizer: Callable[..., Any], **kwargs: Any) -> bool:
    """Accept simple project authorizer call signatures without broad retries."""

    try:
        signature = inspect.signature(authorizer)
    except (TypeError, ValueError):
        return bool(authorizer(**kwargs))
    parameters = list(signature.parameters.values())
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return bool(authorizer(**kwargs))
    if any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters):
        return bool(authorizer(kwargs["request"], kwargs["mutation"], kwargs["store"]))
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional) == 0:
        return bool(authorizer())
    if len(positional) == 1:
        return bool(authorizer(kwargs["request"]))
    if len(positional) == 2:
        return bool(authorizer(kwargs["request"], kwargs["mutation"]))
    return bool(authorizer(kwargs["request"], kwargs["mutation"], kwargs["store"]))


_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN(?: [A-Z0-9-]+)? PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(
        r"\b(?:api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{8,}",
        re.IGNORECASE,
    ),
)
_INSTRUCTION_PATTERNS = (
    re.compile(
        r"(?:^|\n)\s*(?:ignore|disregard|override)\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions|rules)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:^|\n)\s*(?:system|developer)\s+(?:prompt|message)\s*:", re.IGNORECASE),
)


def _classify_content(value: Any) -> list[str]:
    strings = list(_walk_strings(value))
    reasons: list[str] = []
    if any(pattern.search(text) for text in strings for pattern in _SECRET_PATTERNS):
        reasons.append("SECRET_DETECTED")
    if any(pattern.search(text) for text in strings for pattern in _INSTRUCTION_PATTERNS):
        reasons.append("INSTRUCTION_LIKE_CONTENT")
    return reasons


def _walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)


def _safe_message(error: StorageError) -> str:
    # Error messages are authored locally and must never include body snippets.
    return str(error) if error.code not in {"CONTENT_POLICY_REJECTED"} else "content policy rejected authority"
