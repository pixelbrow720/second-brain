"""Local-only, content-addressed raw capture for the M3 staging wiki.

This module deliberately has no fetch, crawl, extraction, or compiler hook.
Callers provide source bytes explicitly (or one bounded regular local file), and
the resulting manifest is the only durable capture metadata.  Raw source text
is always treated as data.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Iterator, Mapping
import uuid

from .contracts import validate_named_document
from .errors import StorageError
from .jsonio import canonical_json_bytes, loads_strict_json
from .parsing import parse_ndjson
from .schema_validation import parse_rfc3339_utc
from .workspace import repository_root


_CAPTURE_ID = re.compile(
    r"^cap:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_CAPTURE_EVENT_ID = re.compile(
    r"^capevt:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_ORIGIN_KINDS = frozenset({"file", "url", "clipboard", "connector", "manual"})
_RETENTION_CLASSES = frozenset({"public", "private", "sensitive", "restricted"})
_REVIEW_DECISIONS = frozenset({"accept", "reject"})
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()

# Deliberately small text-only staging surface.  Unsupported material may be
# retained for review, but this module never claims to parse or extract it.
_DEFAULT_MEDIA_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
    }
)
_YAML_MEDIA_TYPES = frozenset(
    {
        "application/yaml",
        "application/x-yaml",
        "text/yaml",
        "text/x-yaml",
    }
)


def _is_yaml_media_type(media_type: str) -> bool:
    """Reject registered YAML aliases until a strict YAML parser is available."""

    _major, separator, subtype = media_type.partition("/")
    return bool(separator) and (
        media_type in _YAML_MEDIA_TYPES
        or subtype in {"yaml", "x-yaml", "yml", "x-yml"}
        or subtype.endswith(("+yaml", "+yml"))
    )


_REASON_ORDER = (
    "SIZE_EXCEEDED",
    "FORMAT_UNSUPPORTED",
    "FORMAT_MALFORMED",
    "SECRET_SUSPECTED",
    "INSTRUCTION_LIKE_CONTENT",
    "PATH_UNSAFE",
    "URL_UNSAFE",
    "LICENSE_RESTRICTED",
)

# These are detectors, not parsers or authority.  A positive result never
# exposes the matching text through an exception, manifest, or result object.
_SECRET_PATTERNS = (
    re.compile(
        rb"(?i)\b(?:api[_-]?key|access[_-]?token|secret|password|authorization)"
        rb"\s*[:=]\s*(?:['\"])?[a-z0-9_./+=-]{8,}"
    ),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"(?i)\b(?:sk|ghp|xoxb)-[a-z0-9_-]{8,}\b"),
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_INSTRUCTION_PATTERN = re.compile(
    rb"(?is)\b(?:ignore|disregard|override)\b.{0,80}\b(?:previous|prior|system)"
    rb".{0,80}\b(?:instruction|prompt|rule)s?\b"
)
_ACTION_PATTERN = re.compile(
    rb"(?is)(?:\b(?:curl|wget|powershell)\b|\brm\s+-rf\b|"
    rb"\b(?:run|execute)\s+(?:this\s+)?(?:command|shell|script)\b|"
    rb"\b(?:install|upload|exfiltrate)\s+(?:this\s+)?(?:package|data|file)\b)"
)


class CaptureError(StorageError):
    """A redaction-safe raw-capture failure."""


class CaptureRevisionConflictError(CaptureError):
    """A capture-manifest compare-and-swap precondition no longer holds."""

    def __init__(
        self,
        *,
        object_id: str,
        current_revision: int | None,
        current_content_hash: str | None,
    ) -> None:
        self.object_id = object_id
        self.current_revision = current_revision
        self.current_content_hash = current_content_hash
        super().__init__(
            "REVISION_CONFLICT",
            "revision or content hash no longer matches",
            object_id=object_id,
            current_revision=current_revision,
            current_content_hash=current_content_hash,
        )


@dataclass(frozen=True)
class CapturePolicy:
    """Deterministic local staging policy for raw evidence bytes."""

    max_bytes: int = 1_048_576
    allowed_media_types: frozenset[str] = field(default_factory=lambda: _DEFAULT_MEDIA_TYPES)
    restricted_licenses: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "no-license",
                "unlicensed",
                "proprietary",
                "restricted",
            }
        )
    )
    scanner_version: str = "m3-capture/1"

    @classmethod
    def from_value(cls, value: "CapturePolicy | Mapping[str, Any] | None") -> "CapturePolicy":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value._validated()
        if not isinstance(value, Mapping):
            raise CaptureError("SCHEMA_INVALID", "capture policy is invalid")

        allowed = {
            "max_bytes",
            "allowed_media_types",
            "restricted_licenses",
            "scanner_version",
        }
        if set(value) - allowed:
            raise CaptureError("SCHEMA_INVALID", "capture policy is invalid")
        try:
            policy = cls(
                max_bytes=value.get("max_bytes", 1_048_576),
                allowed_media_types=frozenset(value.get("allowed_media_types", _DEFAULT_MEDIA_TYPES)),
                restricted_licenses=frozenset(
                    value.get(
                        "restricted_licenses",
                        {"no-license", "unlicensed", "proprietary", "restricted"},
                    )
                ),
                scanner_version=value.get("scanner_version", "m3-capture/1"),
            )
        except (TypeError, ValueError) as error:
            raise CaptureError("SCHEMA_INVALID", "capture policy is invalid") from error
        return policy._validated()

    def _validated(self) -> "CapturePolicy":
        if not isinstance(self.max_bytes, int) or isinstance(self.max_bytes, bool) or self.max_bytes < 0:
            raise CaptureError("SCHEMA_INVALID", "capture policy is invalid")
        if not self.allowed_media_types or not all(
            isinstance(item, str) and _MEDIA_TYPE.fullmatch(item) for item in self.allowed_media_types
        ):
            raise CaptureError("SCHEMA_INVALID", "capture policy is invalid")
        if not all(isinstance(item, str) and item for item in self.restricted_licenses):
            raise CaptureError("SCHEMA_INVALID", "capture policy is invalid")
        if (
            not isinstance(self.scanner_version, str)
            or not self.scanner_version
            or len(self.scanner_version) > 160
            or _has_control_characters(self.scanner_version)
        ):
            raise CaptureError("SCHEMA_INVALID", "capture policy is invalid")
        normalized_media_types = frozenset(item.lower() for item in self.allowed_media_types)
        # M3 has no full-document YAML parser.  Keep YAML out of the allowlist
        # rather than treating arbitrary UTF-8 as a valid structured source.
        if any(_is_yaml_media_type(item) for item in normalized_media_types):
            raise CaptureError("SCHEMA_INVALID", "capture policy is invalid")
        return CapturePolicy(
            max_bytes=self.max_bytes,
            allowed_media_types=normalized_media_types,
            restricted_licenses=frozenset(item.strip().lower() for item in self.restricted_licenses),
            scanner_version=self.scanner_version,
        )


@dataclass(frozen=True)
class CaptureRequest:
    """Explicit metadata and optional in-memory bytes for one source capture."""

    origin: Mapping[str, Any]
    media_type: str
    retention: Mapping[str, Any]
    captured_by: str
    content: bytes | None = None
    capture_id: str | None = None
    license: str | None = None

    @classmethod
    def from_value(cls, value: "CaptureRequest | Mapping[str, Any]") -> "CaptureRequest":
        if isinstance(value, cls):
            return value._validated()
        if not isinstance(value, Mapping):
            raise CaptureError("SCHEMA_INVALID", "capture request is invalid")

        allowed = {
            "origin",
            "media_type",
            "retention",
            "captured_by",
            "capture_id",
            "license",
            "content",
            "bytes",
            "data",
            "body",
        }
        if set(value) - allowed:
            raise CaptureError("SCHEMA_INVALID", "capture request is invalid")
        payload_fields = [key for key in ("content", "bytes", "data", "body") if key in value]
        if len(payload_fields) > 1:
            raise CaptureError("SCHEMA_INVALID", "capture request is invalid")
        try:
            request = cls(
                origin=value["origin"],
                media_type=value["media_type"],
                retention=value["retention"],
                captured_by=value["captured_by"],
                content=value[payload_fields[0]] if payload_fields else None,
                capture_id=value.get("capture_id"),
                license=value.get("license"),
            )
        except KeyError as error:
            raise CaptureError("SCHEMA_INVALID", "capture request is invalid") from error
        return request._validated()

    def _validated(self) -> "CaptureRequest":
        origin = _normalize_origin(self.origin)
        media_type = _normalize_media_type(self.media_type)
        retention = _normalize_retention(self.retention)
        if (
            not isinstance(self.captured_by, str)
            or not self.captured_by
            or len(self.captured_by) > 160
            or _has_control_characters(self.captured_by)
        ):
            raise CaptureError("SCHEMA_INVALID", "capture request is invalid")
        content = _coerce_bytes(self.content) if self.content is not None else None
        capture_id = _normalize_capture_id(self.capture_id) if self.capture_id is not None else None
        if self.license is not None and (
            not isinstance(self.license, str)
            or not self.license.strip()
            or len(self.license) > 255
            or _has_control_characters(self.license)
        ):
            raise CaptureError("SCHEMA_INVALID", "capture request is invalid")
        return CaptureRequest(
            origin=origin,
            media_type=media_type,
            retention=retention,
            captured_by=self.captured_by,
            content=content,
            capture_id=capture_id,
            license=self.license.strip() if isinstance(self.license, str) else None,
        )


@dataclass(frozen=True)
class CaptureResult:
    """Safe receipt for a completed capture; it intentionally contains no bytes."""

    manifest: Mapping[str, Any]
    content_digest: str
    blob_existed: bool

    @property
    def capture_id(self) -> str:
        return str(self.manifest["capture_id"])

    @property
    def blob_already_existed(self) -> bool:
        """Compatibility alias that reads naturally at a call site."""

        return self.blob_existed

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": deepcopy(dict(self.manifest)),
            "content_digest": self.content_digest,
            "blob_existed": self.blob_existed,
        }


class RawCaptureRepository:
    """Own the bounded raw-capture layout rooted at one explicit local path."""

    def __init__(
        self,
        root: str | Path,
        *,
        clock: Any = None,
        policy: CapturePolicy | Mapping[str, Any] | None = None,
    ) -> None:
        self.root = _prepare_root(root)
        self._root_identity = self.root
        self._clock = clock
        self.policy = CapturePolicy.from_value(policy)
        self._lock = _process_lock_for(self.root)
        self._ensure_layout()

    @property
    def raw_root(self) -> Path:
        return self.root / "raw"

    @property
    def inbox_path(self) -> Path:
        return self.raw_root / "inbox"

    @property
    def quarantine_path(self) -> Path:
        return self.raw_root / "quarantine"

    @property
    def blobs_path(self) -> Path:
        return self.raw_root / "blobs" / "sha256"

    @property
    def manifests_path(self) -> Path:
        return self.raw_root / "manifests"

    @property
    def events_path(self) -> Path:
        return self.raw_root / "events.ndjson"

    @property
    def _lock_path(self) -> Path:
        return self.raw_root / ".capture.lock"

    def capture_bytes(
        self,
        request: CaptureRequest | Mapping[str, Any] | bytes | bytearray | memoryview,
        content: bytes | bytearray | memoryview | CaptureRequest | Mapping[str, Any] | None = None,
    ) -> CaptureResult:
        """Capture explicit source bytes without fetching, parsing, or compiling them.

        The normal form is ``capture_bytes(request, content)`` or a request
        mapping with one of ``content``, ``bytes``, ``data``, or ``body``.  The
        bytes-first form is also accepted for simple callers:
        ``capture_bytes(content, request)``.
        """

        normalized, payload = self._normalize_capture_input(request, content)
        digest = _sha256(payload)
        scan, safe_origin, materialize = self._scan(normalized, payload)
        capture_id = normalized.capture_id or _new_capture_id()
        manifest = _build_manifest(
            capture_id=capture_id,
            origin=safe_origin,
            media_type=normalized.media_type,
            retention=normalized.retention,
            captured_by=normalized.captured_by,
            payload_digest=digest,
            payload_size=len(payload),
            captured_at=self._now_rfc3339(),
            scan=scan,
        )
        validate_capture_manifest(manifest)

        with self._writer_lock():
            events = self._verified_manifest_events()
            manifest_path = self._manifest_path(capture_id)
            if any(event["capture_id"] == capture_id for event in events):
                raise CaptureError("CAPTURE_EXISTS", "capture ID already exists")

            blob_existed = False
            if materialize:
                blob_existed = self._store_blob(digest, payload)
            self._write_new_manifest(manifest_path, manifest)
            try:
                self._append_manifest_event(
                    "capture_created",
                    manifest,
                    before_content_hash=None,
                    events=events,
                )
            except Exception:
                # A manifest without its immutable transition record is not authority.
                _secure_unlink_if_regular(manifest_path, self.root)
                raise

        return CaptureResult(
            manifest=deepcopy(manifest),
            content_digest=digest,
            blob_existed=blob_existed,
        )

    def capture_file(
        self,
        path: str | Path,
        request: CaptureRequest | Mapping[str, Any],
        *,
        allowed_root: str | Path | None = None,
    ) -> CaptureResult:
        """Capture exactly one regular file under an explicitly supplied root.

        Absolute paths, traversal, symlink components, directories, and files
        larger than the local policy are rejected before source bytes are read.
        This is intentionally not a directory-ingest convenience API.
        """

        if allowed_root is None:
            raise CaptureError("PATH_UNSAFE", "an allowed root is required")
        relative = _relative_capture_path(path)
        allowed = _validate_allowed_root(allowed_root)
        normalized = CaptureRequest.from_value(request)
        if normalized.origin["kind"] != "file":
            raise CaptureError("SCHEMA_INVALID", "file capture requires a file origin")
        payload = _read_contained_regular_file(allowed, relative, self.policy.max_bytes)
        return self.capture_bytes(normalized, payload)

    def get_manifest(self, capture_id: str) -> dict[str, Any]:
        """Return one schema- and integrity-checked manifest without source bytes."""

        normalized_id = _normalize_capture_id(capture_id)
        with self._writer_lock():
            inventory = self._verified_manifest_inventory()
            manifest = inventory.get(normalized_id)
            if manifest is None:
                raise CaptureError("CAPTURE_NOT_FOUND", "capture manifest does not exist")
            return deepcopy(manifest)

    def list_manifests(self) -> tuple[dict[str, Any], ...]:
        """Return all valid manifests in stable capture-ID order."""

        with self._writer_lock():
            inventory = self._verified_manifest_inventory()
            return tuple(deepcopy(inventory[capture_id]) for capture_id in sorted(inventory))

    def review(
        self,
        capture_id: str,
        decision: str | Mapping[str, Any],
        *,
        expected_revision: int | None = None,
        expected_content_hash: str | None = None,
    ) -> dict[str, Any]:
        """Explicitly resolve a quarantined manifest using optional CAS values.

        Passing ``{"decision": "accept", "expected_revision": 1}`` is the
        fully explicit form.  The positional form remains convenient for a
        caller that just read the current manifest; stale explicit revisions or
        hashes always fail with ``REVISION_CONFLICT``.
        """

        normalized_id = _normalize_capture_id(capture_id)
        review_decision, expected_revision, expected_content_hash = _normalize_review(
            decision,
            expected_revision,
            expected_content_hash,
        )
        with self._writer_lock():
            events = self._verified_manifest_events()
            inventory = self._verified_manifest_inventory(events=events)
            current = inventory.get(normalized_id)
            if current is None:
                raise CaptureError("CAPTURE_NOT_FOUND", "capture manifest does not exist")
            path = self._manifest_path(normalized_id)
            self._assert_review_cas(current, expected_revision, expected_content_hash)
            if current["status"] != "quarantined":
                raise CaptureError("REVIEW_STATE_INVALID", "capture is not awaiting review")

            updated = deepcopy(current)
            updated["revision"] = int(current["revision"]) + 1
            updated["status"] = "accepted" if review_decision == "accept" else "rejected"
            updated["scan"]["decision"] = review_decision
            updated["content_hash"] = capture_manifest_content_hash(updated)
            validate_capture_manifest(updated)
            self._replace_manifest(path, updated)
            try:
                self._append_manifest_event(
                    "capture_reviewed",
                    updated,
                    before_content_hash=str(current["content_hash"]),
                    events=events,
                )
            except Exception:
                # Preserve the prior review state if the append-only history cannot advance.
                self._replace_manifest(path, current)
                raise
            return deepcopy(updated)

    def _normalize_capture_input(
        self,
        request: CaptureRequest | Mapping[str, Any] | bytes | bytearray | memoryview,
        content: bytes | bytearray | memoryview | CaptureRequest | Mapping[str, Any] | None,
    ) -> tuple[CaptureRequest, bytes]:
        if _is_bytes_like(request):
            if not isinstance(content, (CaptureRequest, Mapping)):
                raise CaptureError("SCHEMA_INVALID", "capture request is invalid")
            return CaptureRequest.from_value(content), _coerce_bytes(request)

        normalized = CaptureRequest.from_value(request)
        if content is None:
            if normalized.content is None:
                raise CaptureError("SCHEMA_INVALID", "capture bytes are required")
            return normalized, normalized.content
        if not _is_bytes_like(content):
            raise CaptureError("SCHEMA_INVALID", "capture bytes are invalid")
        if normalized.content is not None:
            raise CaptureError("SCHEMA_INVALID", "capture bytes are ambiguous")
        return normalized, _coerce_bytes(content)

    def _scan(
        self,
        request: CaptureRequest,
        payload: bytes,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        reasons: set[str] = set()
        origin = deepcopy(dict(request.origin))
        origin_unsafe = _origin_is_unsafe(origin)
        if origin_unsafe == "path":
            reasons.add("PATH_UNSAFE")
            origin = _redacted_origin(str(origin["kind"]), "path")
        elif origin_unsafe == "url":
            reasons.add("URL_UNSAFE")
            origin = _redacted_origin("url", "url")

        format_state = "supported"
        if request.media_type not in self.policy.allowed_media_types:
            format_state = "unsupported"
            reasons.add("FORMAT_UNSUPPORTED")
        elif not _is_well_formed_text_payload(request.media_type, payload):
            format_state = "malformed"
            reasons.add("FORMAT_MALFORMED")

        secret_state = "clear"
        injection_state = "clear"
        if len(payload) > self.policy.max_bytes:
            reasons.add("SIZE_EXCEEDED")
        if _looks_like_secret(payload):
            secret_state = "suspected"
            reasons.add("SECRET_SUSPECTED")
        elif _looks_like_instruction(payload):
            injection_state = "contains_instruction_like_text"
            reasons.add("INSTRUCTION_LIKE_CONTENT")

        if request.license and request.license.lower() in self.policy.restricted_licenses:
            reasons.add("LICENSE_RESTRICTED")

        rejected = bool(
            reasons
            & {
                "SIZE_EXCEEDED",
                "SECRET_SUSPECTED",
                "PATH_UNSAFE",
                "URL_UNSAFE",
                "LICENSE_RESTRICTED",
            }
        )
        quarantined = bool(
            reasons & {"FORMAT_UNSUPPORTED", "FORMAT_MALFORMED", "INSTRUCTION_LIKE_CONTENT"}
        )
        if rejected:
            status, decision = "rejected", "reject"
        elif quarantined:
            status, decision = "quarantined", "quarantine"
        else:
            status, decision = "accepted", "accept"

        # Never persist source bytes that match a secret detector.  Other
        # rejected content is also metadata-only so review cannot accidentally
        # treat unsafe provenance as materialized evidence.
        materialize = not rejected
        ordered_reasons = [code for code in _REASON_ORDER if code in reasons]
        return (
            {
                "scanner_version": self.policy.scanner_version,
                "secret": secret_state,
                "injection": injection_state,
                "format": format_state,
                "decision": decision,
                "reason_codes": ordered_reasons,
                "_status": status,
            },
            origin,
            materialize,
        )

    def _store_blob(self, digest: str, payload: bytes) -> bool:
        path = self._blob_path(digest)
        _ensure_private_directory(path.parent, self.root)
        try:
            _write_exclusive_bytes(path, payload, self.root)
            _verify_existing_blob(path, digest, self.root, expected_bytes=len(payload))
            return False
        except FileExistsError:
            _verify_existing_blob(path, digest, self.root, expected_bytes=len(payload))
            return True

    def _write_new_manifest(self, path: Path, manifest: Mapping[str, Any]) -> None:
        _ensure_private_directory(path.parent, self.root)
        encoded = canonical_json_bytes(dict(manifest))
        try:
            _write_exclusive_bytes(path, encoded, self.root)
        except FileExistsError as error:
            raise CaptureError("CAPTURE_EXISTS", "capture ID already exists") from error

    def _replace_manifest(self, path: Path, manifest: Mapping[str, Any]) -> None:
        _atomic_replace_bytes(path, canonical_json_bytes(dict(manifest)), self.root)

    def _read_manifest(self, path: Path, *, expected_capture_id: str) -> dict[str, Any]:
        try:
            manifest = loads_strict_json(_read_secure_bytes(path, self.root).decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, StorageError) as error:
            raise CaptureError("MANIFEST_INVALID", "capture manifest is invalid") from error
        if not isinstance(manifest, dict):
            raise CaptureError("MANIFEST_INVALID", "capture manifest is invalid")
        try:
            validate_capture_manifest(manifest)
        except (CaptureError, ValueError) as error:
            raise CaptureError("MANIFEST_INVALID", "capture manifest is invalid") from error
        if manifest["capture_id"] != expected_capture_id:
            raise CaptureError("INTEGRITY_FAILED", "manifest filename does not match capture ID")
        return manifest

    def _verify_manifest_blob(self, manifest: Mapping[str, Any]) -> None:
        if _manifest_allows_unmaterialized_blob(manifest):
            return
        digest = str(manifest["blob"]["sha256"])
        path = self._blob_path(digest)
        expected_bytes = int(manifest["blob"]["bytes"])
        try:
            _verify_existing_blob(path, digest, self.root, expected_bytes=expected_bytes)
        except CaptureError as error:
            raise

    def _append_manifest_event(
        self,
        event_type: str,
        manifest: Mapping[str, Any],
        *,
        before_content_hash: str | None,
        events: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        if event_type not in {"capture_created", "capture_reviewed"}:
            raise CaptureError("INTEGRITY_FAILED", "unsupported capture history event")
        ledger = list(events) if events is not None else self._verified_manifest_events()
        previous_hash = str(ledger[-1]["event_hash"]) if ledger else None
        blob = _require_manifest_mapping(manifest.get("blob"), "capture blob")
        event: dict[str, Any] = {
            "schema_version": 1,
            "event_id": f"capevt:{uuid.uuid4()}",
            "sequence": len(ledger) + 1,
            "event_type": event_type,
            "capture_id": manifest["capture_id"],
            "revision": manifest["revision"],
            "before_content_hash": before_content_hash,
            "after_content_hash": manifest["content_hash"],
            "blob_sha256": blob["sha256"],
            "blob_bytes": blob["bytes"],
            "occurred_at": self._now_rfc3339(),
            "prev_event_hash": previous_hash,
        }
        event["event_hash"] = _capture_event_hash(event)
        _validate_capture_event(event)
        _append_secure_bytes(self.events_path, canonical_json_bytes(event) + b"\n", self.root)

    def _verified_manifest_events(self) -> list[dict[str, Any]]:
        events = self._load_manifest_events()
        # A manifest without a ledger record (or vice versa) is a fail-closed
        # authority mismatch, not an invitation to infer a review state.
        names = _private_directory_names(self.manifests_path, self.root)
        capture_ids: list[str] = []
        manifests: dict[str, dict[str, Any]] = {}
        for name in names:
            if not name.endswith(".json"):
                raise CaptureError("INTEGRITY_FAILED", "unexpected manifest entry")
            capture_id = name[:-5]
            if _CAPTURE_ID.fullmatch(capture_id) is None:
                raise CaptureError("INTEGRITY_FAILED", "invalid manifest filename")
            capture_ids.append(capture_id)
            manifests[capture_id] = self._read_manifest(
                self._manifest_path(capture_id), expected_capture_id=capture_id
            )
        event_ids = {str(event["capture_id"]) for event in events}
        if set(capture_ids) != event_ids:
            raise CaptureError("INTEGRITY_FAILED", "capture manifest and history inventory diverged")
        for capture_id, manifest in manifests.items():
            self._verify_manifest_history(manifest, events=events)
            self._verify_manifest_blob(manifest)
        return events

    def _verified_manifest_inventory(
        self, *, events: Sequence[Mapping[str, Any]] | None = None
    ) -> dict[str, dict[str, Any]]:
        ledger = list(events) if events is not None else self._verified_manifest_events()
        inventory: dict[str, dict[str, Any]] = {}
        for name in _private_directory_names(self.manifests_path, self.root):
            capture_id = name[:-5]
            manifest = self._read_manifest(self._manifest_path(capture_id), expected_capture_id=capture_id)
            self._verify_manifest_history(manifest, events=ledger)
            self._verify_manifest_blob(manifest)
            inventory[capture_id] = manifest
        return inventory

    def _load_manifest_events(self) -> list[dict[str, Any]]:
        try:
            raw = _read_secure_bytes(self.events_path, self.root)
            events = parse_ndjson(raw.decode("utf-8"))
        except (UnicodeDecodeError, StorageError, ValueError) as error:
            raise CaptureError("INTEGRITY_FAILED", "capture history ledger is invalid") from error
        if not isinstance(events, list):
            raise CaptureError("INTEGRITY_FAILED", "capture history ledger is invalid")
        previous_hash: str | None = None
        histories: dict[str, list[dict[str, Any]]] = {}
        result: list[dict[str, Any]] = []
        for sequence, raw_event in enumerate(events, start=1):
            if not isinstance(raw_event, Mapping):
                raise CaptureError("INTEGRITY_FAILED", "capture history event is invalid")
            event = deepcopy(dict(raw_event))
            _validate_capture_event(event)
            if event["sequence"] != sequence or event["prev_event_hash"] != previous_hash:
                raise CaptureError("INTEGRITY_FAILED", "capture history sequence diverged")
            if _capture_event_hash(event) != event["event_hash"]:
                raise CaptureError("INTEGRITY_FAILED", "capture history hash diverged")
            history = histories.setdefault(str(event["capture_id"]), [])
            if not history:
                if event["event_type"] != "capture_created" or event["revision"] != 1 or event["before_content_hash"] is not None:
                    raise CaptureError("INTEGRITY_FAILED", "capture history does not begin with creation")
            else:
                prior = history[-1]
                if (
                    event["event_type"] != "capture_reviewed"
                    or event["revision"] != prior["revision"] + 1
                    or event["before_content_hash"] != prior["after_content_hash"]
                    or event["blob_sha256"] != prior["blob_sha256"]
                    or event["blob_bytes"] != prior["blob_bytes"]
                ):
                    raise CaptureError("INTEGRITY_FAILED", "capture history transition diverged")
            history.append(event)
            result.append(event)
            previous_hash = str(event["event_hash"])
        return result

    def _verify_manifest_history(
        self,
        manifest: Mapping[str, Any],
        *,
        events: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        ledger = list(events) if events is not None else self._load_manifest_events()
        history = [event for event in ledger if event["capture_id"] == manifest["capture_id"]]
        if not history:
            raise CaptureError("INTEGRITY_FAILED", "capture manifest has no transition history")
        terminal = history[-1]
        blob = _require_manifest_mapping(manifest.get("blob"), "capture blob")
        if (
            terminal["revision"] != manifest["revision"]
            or terminal["after_content_hash"] != manifest["content_hash"]
            or terminal["blob_sha256"] != blob.get("sha256")
            or terminal["blob_bytes"] != blob.get("bytes")
        ):
            raise CaptureError("INTEGRITY_FAILED", "capture manifest diverges from transition history")

    def _assert_review_cas(
        self,
        current: Mapping[str, Any],
        expected_revision: int | None,
        expected_content_hash: str | None,
    ) -> None:
        current_revision = int(current["revision"])
        current_hash = str(current["content_hash"])
        if expected_revision is not None and expected_revision != current_revision:
            raise CaptureRevisionConflictError(
                object_id=str(current["capture_id"]),
                current_revision=current_revision,
                current_content_hash=current_hash,
            )
        if expected_content_hash is not None and expected_content_hash != current_hash:
            raise CaptureRevisionConflictError(
                object_id=str(current["capture_id"]),
                current_revision=current_revision,
                current_content_hash=current_hash,
            )

    def _manifest_path(self, capture_id: str) -> Path:
        return self.manifests_path / f"{capture_id}.json"

    def _blob_path(self, digest: str) -> Path:
        if _HEX_DIGEST.fullmatch(digest) is None:
            raise CaptureError("PATH_UNSAFE", "unsafe blob path")
        return self.blobs_path / digest[:2] / digest

    def _ensure_layout(self) -> None:
        self._assert_root_identity()
        _ensure_private_directory(self.root, repository_root().resolve(), boundary_is_private=False)
        for path in (
            self.raw_root,
            self.inbox_path,
            self.quarantine_path,
            self.blobs_path,
            self.manifests_path,
        ):
            _ensure_private_directory(path, self.root)
        try:
            _write_exclusive_bytes(self.events_path, b"", self.root)
        except FileExistsError:
            _read_secure_bytes(self.events_path, self.root)

    @contextmanager
    def _writer_lock(self) -> Iterator[None]:
        self._assert_root_identity()
        self._ensure_layout()
        with self._lock:
            with _open_secure_parent_fd(self._lock_path, self.root) as (parent_fd, name):
                descriptor: int | None = None
                try:
                    descriptor = os.open(
                        name,
                        os.O_CREAT | os.O_RDWR | _nofollow_flag(),
                        0o600,
                        dir_fd=parent_fd,
                    )
                    _assert_secure_stat(os.fstat(descriptor), directory=False)
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    yield
                except OSError as error:
                    raise CaptureError("PATH_UNSAFE", "capture writer lock cannot be opened safely") from error
                finally:
                    if descriptor is not None:
                        try:
                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                        finally:
                            os.close(descriptor)

    def _assert_root_identity(self) -> None:
        if self.root != self._root_identity or _prepare_root(self.root) != self._root_identity:
            raise CaptureError("PATH_UNSAFE", "capture root changed after initialization")

    def _now_rfc3339(self) -> str:
        if self._clock is not None and hasattr(self._clock, "now_rfc3339"):
            value = self._clock.now_rfc3339()
            if isinstance(value, str):
                parse_rfc3339_utc(value)
                return value
        if self._clock is not None and hasattr(self._clock, "now"):
            value = self._clock.now()
            if isinstance(value, datetime) and value.tzinfo is not None:
                return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def capture_manifest_content_hash(manifest: Mapping[str, Any]) -> str:
    """Hash a raw manifest's logical record without its self-reference field."""

    logical = deepcopy(dict(manifest))
    logical.pop("content_hash", None)
    return _sha256(canonical_json_bytes(logical))


def validate_capture_manifest(manifest: Mapping[str, Any]) -> None:
    """Validate frozen schema plus M3 raw-capture cross-field invariants."""

    if not isinstance(manifest, Mapping):
        raise CaptureError("SCHEMA_INVALID", "capture manifest is invalid")
    document = deepcopy(dict(manifest))
    try:
        validate_named_document("raw-capture-manifest-v1", document)
    except ValueError as error:
        raise CaptureError("SCHEMA_INVALID", "capture manifest is invalid") from error

    _normalize_capture_id(document["capture_id"])
    if document["content_hash"] != capture_manifest_content_hash(document):
        raise CaptureError("MANIFEST_HASH_INVALID", "capture manifest hash is invalid")
    blob = document["blob"]
    digest = blob["sha256"]
    if blob["relative_path"] != f"raw/blobs/sha256/{digest[:2]}/{digest}":
        raise CaptureError("SCHEMA_INVALID", "capture manifest is invalid")
    if document["status"] == "accepted" and document["scan"]["decision"] != "accept":
        raise CaptureError("SCHEMA_INVALID", "capture manifest is invalid")
    if document["status"] == "quarantined" and document["scan"]["decision"] != "quarantine":
        raise CaptureError("SCHEMA_INVALID", "capture manifest is invalid")
    if document["status"] == "rejected" and document["scan"]["decision"] != "reject":
        raise CaptureError("SCHEMA_INVALID", "capture manifest is invalid")
    if document["scan"]["secret"] != "clear" and "SECRET_SUSPECTED" not in document["scan"]["reason_codes"]:
        raise CaptureError("SCHEMA_INVALID", "capture manifest is invalid")
    if (
        document["scan"]["injection"] != "clear"
        and "INSTRUCTION_LIKE_CONTENT" not in document["scan"]["reason_codes"]
    ):
        raise CaptureError("SCHEMA_INVALID", "capture manifest is invalid")


def _require_manifest_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CaptureError("INTEGRITY_FAILED", f"{name} is invalid")
    return value


def _capture_event_hash(event: Mapping[str, Any]) -> str:
    logical = deepcopy(dict(event))
    logical.pop("event_hash", None)
    return _sha256(canonical_json_bytes(logical))


def _validate_capture_event(event: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "event_id",
        "sequence",
        "event_type",
        "capture_id",
        "revision",
        "before_content_hash",
        "after_content_hash",
        "blob_sha256",
        "blob_bytes",
        "occurred_at",
        "prev_event_hash",
        "event_hash",
    }
    if set(event) != required:
        raise CaptureError("INTEGRITY_FAILED", "capture history event has an invalid shape")
    if event["schema_version"] != 1:
        raise CaptureError("INTEGRITY_FAILED", "capture history event has an invalid schema")
    event_id = event["event_id"]
    if not isinstance(event_id, str) or _CAPTURE_EVENT_ID.fullmatch(event_id) is None:
        raise CaptureError("INTEGRITY_FAILED", "capture history event has an invalid ID")
    try:
        if str(uuid.UUID(event_id[7:])) != event_id[7:]:
            raise ValueError("noncanonical event UUID")
    except ValueError as error:
        raise CaptureError("INTEGRITY_FAILED", "capture history event has an invalid ID") from error
    _normalize_capture_id(event["capture_id"])
    if (
        not isinstance(event["sequence"], int)
        or isinstance(event["sequence"], bool)
        or event["sequence"] < 1
        or not isinstance(event["revision"], int)
        or isinstance(event["revision"], bool)
        or event["revision"] < 1
        or event["event_type"] not in {"capture_created", "capture_reviewed"}
        or not isinstance(event["blob_bytes"], int)
        or isinstance(event["blob_bytes"], bool)
        or event["blob_bytes"] < 0
    ):
        raise CaptureError("INTEGRITY_FAILED", "capture history event has invalid fields")
    for field in ("after_content_hash", "blob_sha256", "event_hash"):
        if not isinstance(event[field], str) or _HEX_DIGEST.fullmatch(event[field]) is None:
            raise CaptureError("INTEGRITY_FAILED", "capture history event has an invalid hash")
    for field in ("before_content_hash", "prev_event_hash"):
        value = event[field]
        if value is not None and (not isinstance(value, str) or _HEX_DIGEST.fullmatch(value) is None):
            raise CaptureError("INTEGRITY_FAILED", "capture history event has an invalid linked hash")
    if not isinstance(event["occurred_at"], str):
        raise CaptureError("INTEGRITY_FAILED", "capture history event has an invalid timestamp")
    try:
        parse_rfc3339_utc(event["occurred_at"])
    except ValueError as error:
        raise CaptureError("INTEGRITY_FAILED", "capture history event has an invalid timestamp") from error
    if event["event_type"] == "capture_created" and (
        event["revision"] != 1 or event["before_content_hash"] is not None
    ):
        raise CaptureError("INTEGRITY_FAILED", "capture creation event is invalid")
    if event["event_type"] == "capture_reviewed" and (
        event["revision"] < 2 or event["before_content_hash"] is None
    ):
        raise CaptureError("INTEGRITY_FAILED", "capture review event is invalid")


def _build_manifest(
    *,
    capture_id: str,
    origin: Mapping[str, Any],
    media_type: str,
    retention: Mapping[str, Any],
    captured_by: str,
    payload_digest: str,
    payload_size: int,
    captured_at: str,
    scan: Mapping[str, Any],
) -> dict[str, Any]:
    status = scan["_status"]
    manifest = {
        "schema_version": 1,
        "capture_id": capture_id,
        "revision": 1,
        "status": status,
        "origin": deepcopy(dict(origin)),
        "blob": {
            "sha256": payload_digest,
            "bytes": payload_size,
            "media_type": media_type,
            "relative_path": f"raw/blobs/sha256/{payload_digest[:2]}/{payload_digest}",
        },
        "captured_at": captured_at,
        "captured_by": captured_by,
        "scan": {
            key: deepcopy(value)
            for key, value in scan.items()
            if key != "_status"
        },
        "retention": deepcopy(dict(retention)),
        "source_object_id": None,
    }
    manifest["content_hash"] = capture_manifest_content_hash(manifest)
    return manifest


def _normalize_origin(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - {"kind", "locator", "final_locator"}:
        raise CaptureError("SCHEMA_INVALID", "capture request is invalid")
    try:
        kind = value["kind"]
        locator = value["locator"]
    except KeyError as error:
        raise CaptureError("SCHEMA_INVALID", "capture request is invalid") from error
    final_locator = value.get("final_locator")
    if (
        not isinstance(kind, str)
        or kind not in _ORIGIN_KINDS
        or not isinstance(locator, str)
        or not locator
        or len(locator) > 4096
        or _has_control_characters(locator)
        or (
            final_locator is not None
            and (
                not isinstance(final_locator, str)
                or len(final_locator) > 4096
                or _has_control_characters(final_locator)
            )
        )
    ):
        raise CaptureError("SCHEMA_INVALID", "capture request is invalid")
    origin: dict[str, Any] = {"kind": kind, "locator": locator}
    if "final_locator" in value:
        origin["final_locator"] = final_locator
    return origin


def _normalize_media_type(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 255 or _has_control_characters(value):
        raise CaptureError("SCHEMA_INVALID", "capture request is invalid")
    normalized = value.strip().lower()
    if ";" in normalized:
        normalized = normalized.split(";", 1)[0].strip()
    if _MEDIA_TYPE.fullmatch(normalized) is None:
        raise CaptureError("SCHEMA_INVALID", "capture request is invalid")
    return normalized


def _normalize_retention(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - {"classification", "expires_at"}:
        raise CaptureError("SCHEMA_INVALID", "capture request is invalid")
    try:
        classification = value["classification"]
        expires_at = value["expires_at"]
    except KeyError as error:
        raise CaptureError("SCHEMA_INVALID", "capture request is invalid") from error
    if not isinstance(classification, str) or classification not in _RETENTION_CLASSES:
        raise CaptureError("SCHEMA_INVALID", "capture request is invalid")
    if expires_at is not None:
        if not isinstance(expires_at, str):
            raise CaptureError("SCHEMA_INVALID", "capture request is invalid")
        try:
            parse_rfc3339_utc(expires_at)
        except ValueError as error:
            raise CaptureError("SCHEMA_INVALID", "capture request is invalid") from error
    return {"classification": classification, "expires_at": expires_at}


def _normalize_capture_id(value: Any) -> str:
    if not isinstance(value, str) or _CAPTURE_ID.fullmatch(value) is None:
        raise CaptureError("SCHEMA_INVALID", "capture ID is invalid")
    try:
        parsed = uuid.UUID(value[4:])
    except (ValueError, AttributeError) as error:
        raise CaptureError("SCHEMA_INVALID", "capture ID is invalid") from error
    if str(parsed) != value[4:]:
        raise CaptureError("SCHEMA_INVALID", "capture ID is invalid")
    return value


def _normalize_review(
    decision: str | Mapping[str, Any],
    expected_revision: int | None,
    expected_content_hash: str | None,
) -> tuple[str, int | None, str | None]:
    if isinstance(decision, Mapping):
        if set(decision) - {"decision", "expected_revision", "expected_content_hash"}:
            raise CaptureError("SCHEMA_INVALID", "review request is invalid")
        if "decision" not in decision:
            raise CaptureError("SCHEMA_INVALID", "review request is invalid")
        if expected_revision is not None and "expected_revision" in decision:
            raise CaptureError("SCHEMA_INVALID", "review request is invalid")
        if expected_content_hash is not None and "expected_content_hash" in decision:
            raise CaptureError("SCHEMA_INVALID", "review request is invalid")
        expected_revision = decision.get("expected_revision", expected_revision)
        expected_content_hash = decision.get("expected_content_hash", expected_content_hash)
        decision = decision["decision"]
    if not isinstance(decision, str) or decision not in _REVIEW_DECISIONS:
        raise CaptureError("SCHEMA_INVALID", "review request is invalid")
    if expected_revision is not None and (
        not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1
    ):
        raise CaptureError("SCHEMA_INVALID", "review request is invalid")
    if expected_content_hash is not None and (
        not isinstance(expected_content_hash, str) or _HEX_DIGEST.fullmatch(expected_content_hash) is None
    ):
        raise CaptureError("SCHEMA_INVALID", "review request is invalid")
    return decision, expected_revision, expected_content_hash


def _origin_is_unsafe(origin: Mapping[str, Any]) -> str | None:
    kind = origin["kind"]
    locator = origin["locator"]
    final_locator = origin.get("final_locator")
    if kind == "file":
        if _unsafe_locator_path(str(locator)) or (
            final_locator is not None and _unsafe_locator_path(str(final_locator))
        ):
            return "path"
    if kind == "url":
        if not _safe_metadata_url(str(locator)) or (
            final_locator is not None and not _safe_metadata_url(str(final_locator))
        ):
            return "url"
    return None


def _redacted_origin(kind: str, reason: str) -> dict[str, Any]:
    return {"kind": kind, "locator": f"redacted:{reason}-unsafe", "final_locator": None}


def _unsafe_locator_path(value: str) -> bool:
    candidate = Path(value)
    return candidate.is_absolute() or ".." in candidate.parts or "\x00" in value


def _safe_metadata_url(value: str) -> bool:
    # URL text is provenance only: only allow ordinary HTTP(S) locator syntax,
    # never a filesystem URL, embedded credentials, or control characters.
    authority = value.split("/", 3)[2] if value.count("/") >= 2 else ""
    if _has_control_characters(value) or "@" in authority:
        return False
    match = re.fullmatch(r"https?://([A-Za-z0-9.-]+)(?::[0-9]{1,5})?(?:/[^\s]*)?", value)
    return match is not None


def _is_well_formed_text_payload(media_type: str, payload: bytes) -> bool:
    # A policy cannot enable YAML until this boundary has a strict parser.
    if _is_yaml_media_type(media_type):
        return False
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if media_type == "application/json":
        try:
            loads_strict_json(text)
        except (ValueError, TypeError):
            return False
    return True


def _looks_like_secret(payload: bytes) -> bool:
    return any(pattern.search(payload) is not None for pattern in _SECRET_PATTERNS)


def _looks_like_instruction(payload: bytes) -> bool:
    return _INSTRUCTION_PATTERN.search(payload) is not None or _ACTION_PATTERN.search(payload) is not None


def _manifest_allows_unmaterialized_blob(manifest: Mapping[str, Any]) -> bool:
    reasons = set(manifest["scan"]["reason_codes"])
    return manifest["status"] == "rejected" and bool(
        reasons
        & {
            "SIZE_EXCEEDED",
            "SECRET_SUSPECTED",
            "PATH_UNSAFE",
            "URL_UNSAFE",
            "LICENSE_RESTRICTED",
        }
    )


def _prepare_root(root: str | Path) -> Path:
    if not isinstance(root, (str, Path)):
        raise CaptureError("PATH_UNSAFE", "capture root is unsafe")
    try:
        path = Path(os.path.abspath(os.fspath(root)))
    except (TypeError, ValueError) as error:
        raise CaptureError("PATH_UNSAFE", "capture root is unsafe") from error
    if not path.is_absolute():
        raise CaptureError("PATH_UNSAFE", "capture root is unsafe")
    repository = repository_root().resolve()
    try:
        path.relative_to(repository)
    except ValueError as error:
        raise CaptureError(
            "PATH_UNSAFE",
            "capture root must be explicitly contained by this repository",
        ) from error
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(repository)
    except ValueError as error:
        raise CaptureError(
            "PATH_UNSAFE",
            "capture root must be explicitly contained by this repository",
        ) from error
    if resolved == repository:
        raise CaptureError("PATH_UNSAFE", "repository root cannot be a capture root")
    return resolved


def _validate_allowed_root(root: str | Path) -> Path:
    allowed = _prepare_root(root)
    with _open_directory_fd(allowed, require_private=False):
        pass
    return allowed


def _relative_capture_path(path: str | Path) -> tuple[str, ...]:
    if not isinstance(path, (str, Path)):
        raise CaptureError("PATH_UNSAFE", "capture file path is unsafe")
    candidate = Path(path)
    if candidate.is_absolute() or not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        raise CaptureError("PATH_UNSAFE", "capture file path is unsafe")
    return tuple(candidate.parts)


def _read_contained_regular_file(root: Path, parts: tuple[str, ...], max_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    no_follow = _nofollow_flag()
    directory = os.O_DIRECTORY
    root_fd: int | None = None
    current_fd: int | None = None
    file_fd: int | None = None
    try:
        root_fd = os.open(root, flags | directory | no_follow)
        _assert_secure_stat(os.fstat(root_fd), directory=True, require_private=False)
        current_fd = root_fd
        for part in parts[:-1]:
            next_fd = os.open(part, flags | directory | no_follow, dir_fd=current_fd)
            _assert_secure_stat(os.fstat(next_fd), directory=True, require_private=False)
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        file_fd = os.open(parts[-1], flags | no_follow, dir_fd=current_fd)
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise CaptureError("PATH_UNSAFE", "capture file is not regular")
        if metadata.st_size > max_bytes:
            raise CaptureError("SIZE_EXCEEDED", "capture file exceeds local size policy")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(file_fd, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > max_bytes:
            raise CaptureError("SIZE_EXCEEDED", "capture file exceeds local size policy")
        after = os.fstat(file_fd)
        if after.st_size != metadata.st_size:
            raise CaptureError("PATH_UNSAFE", "capture file changed during bounded read")
        return payload
    except CaptureError:
        raise
    except OSError as error:
        raise CaptureError("PATH_UNSAFE", "capture file path is unsafe") from error
    finally:
        for descriptor in (file_fd, current_fd, root_fd):
            if descriptor is None:
                continue
            try:
                os.close(descriptor)
            except OSError:
                pass


def _nofollow_flag() -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise CaptureError("PATH_UNSAFE", "platform cannot safely access capture paths")
    return os.O_NOFOLLOW


def _relative_under_boundary(path: Path, boundary: Path) -> Path:
    if not path.is_absolute() or not boundary.is_absolute():
        raise CaptureError("PATH_UNSAFE", "capture path is unsafe")
    try:
        relative = path.relative_to(boundary)
    except ValueError as error:
        raise CaptureError("PATH_UNSAFE", "capture path escapes its root") from error
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise CaptureError("PATH_UNSAFE", "capture path is unsafe")
    return relative


def _assert_secure_stat(
    metadata: os.stat_result,
    *,
    directory: bool,
    require_private: bool = True,
) -> None:
    mode = metadata.st_mode
    expected = stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
    if not expected or stat.S_ISLNK(mode):
        raise CaptureError("PATH_UNSAFE", "capture path has an unsafe file type")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise CaptureError("PATH_UNSAFE", "capture path is not owned by the current user")
    if require_private and stat.S_IMODE(mode) & 0o077:
        raise CaptureError("PATH_UNSAFE", "capture path must be private to the current user")


@contextmanager
def _open_directory_fd(path: Path, *, require_private: bool) -> Iterator[int]:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as error:
        raise CaptureError("PATH_UNSAFE", "capture directory cannot be opened safely") from error
    try:
        _assert_secure_stat(os.fstat(descriptor), directory=True, require_private=require_private)
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_secure_parent_fd(path: Path, boundary: Path) -> Iterator[tuple[int, str]]:
    """Anchor a file parent to no-follow directory descriptors under one root."""

    relative = _relative_under_boundary(path, boundary)
    if not relative.parts:
        raise CaptureError("PATH_UNSAFE", "capture root cannot be used as a file")
    with _open_directory_fd(boundary, require_private=True) as root_fd:
        descriptor = os.dup(root_fd)
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0)
        for component in relative.parts[:-1]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as error:
                raise CaptureError("PATH_UNSAFE", "capture parent cannot be opened safely") from error
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
def _open_secure_directory_fd(path: Path, boundary: Path) -> Iterator[int]:
    with _open_secure_parent_fd(path, boundary) as (parent_fd, name):
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_fd,
            )
        except OSError as error:
            raise CaptureError("PATH_UNSAFE", "capture directory cannot be opened safely") from error
        try:
            _assert_secure_stat(os.fstat(descriptor), directory=True)
            yield descriptor
        finally:
            os.close(descriptor)


def _ensure_private_directory(path: Path, boundary: Path, *, boundary_is_private: bool = True) -> None:
    """Create/access capture directories through no-follow directory descriptors."""

    relative = _relative_under_boundary(path, boundary)
    try:
        descriptor = os.open(
            boundary,
            os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as error:
        raise CaptureError("PATH_UNSAFE", "capture root cannot be opened safely") from error
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
                    raise CaptureError("PATH_UNSAFE", "capture directory cannot be created safely") from error
            except OSError as error:
                raise CaptureError("PATH_UNSAFE", "capture directory cannot be opened safely") from error
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


def _private_directory_names(path: Path, boundary: Path) -> tuple[str, ...]:
    with _open_secure_directory_fd(path, boundary) as descriptor:
        try:
            names = os.listdir(descriptor)
        except OSError as error:
            raise CaptureError("PATH_UNSAFE", "capture directory cannot be listed safely") from error
        result: list[str] = []
        for name in names:
            if not isinstance(name, str) or not name or "/" in name or "\\" in name:
                raise CaptureError("INTEGRITY_FAILED", "capture directory contains an unsafe entry")
            try:
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except OSError as error:
                raise CaptureError("PATH_UNSAFE", "capture directory entry cannot be inspected") from error
            _assert_secure_stat(metadata, directory=False)
            result.append(name)
        return tuple(sorted(result))


def _read_secure_bytes(path: Path, boundary: Path, *, missing_code: str = "PATH_UNSAFE") -> bytes:
    with _open_secure_parent_fd(path, boundary) as (parent_fd, name):
        try:
            descriptor = os.open(name, os.O_RDONLY | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0), dir_fd=parent_fd)
        except FileNotFoundError as error:
            raise CaptureError(missing_code, "capture file is unavailable") from error
        except OSError as error:
            raise CaptureError("PATH_UNSAFE", "capture file cannot be opened safely") from error
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
            raise CaptureError("PATH_UNSAFE", "capture file cannot be read safely") from error
        finally:
            os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short capture write")
        view = view[written:]


def _write_exclusive_bytes(path: Path, payload: bytes, boundary: Path) -> None:
    with _open_secure_parent_fd(path, boundary) as (parent_fd, name):
        descriptor: int | None = None
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=parent_fd,
            )
            _assert_secure_stat(os.fstat(descriptor), directory=False)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.fsync(parent_fd)
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _atomic_replace_bytes(path: Path, payload: bytes, boundary: Path) -> None:
    with _open_secure_parent_fd(path, boundary) as (parent_fd, name):
        temporary = f".{name}.tmp-{uuid.uuid4().hex}"
        descriptor: int | None = None
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            _assert_secure_stat(metadata, directory=False)
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=parent_fd,
            )
            _assert_secure_stat(os.fstat(descriptor), directory=False)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileNotFoundError as error:
            raise CaptureError("CAPTURE_NOT_FOUND", "capture manifest does not exist") from error
        except OSError as error:
            raise CaptureError("PATH_UNSAFE", "capture manifest cannot be replaced safely") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _append_secure_bytes(path: Path, payload: bytes, boundary: Path) -> None:
    with _open_secure_parent_fd(path, boundary) as (parent_fd, name):
        descriptor: int | None = None
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_APPEND | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_fd,
            )
            _assert_secure_stat(os.fstat(descriptor), directory=False)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.fsync(parent_fd)
        except FileNotFoundError as error:
            raise CaptureError("INTEGRITY_FAILED", "capture history ledger is missing") from error
        except OSError as error:
            raise CaptureError("INTEGRITY_FAILED", "capture history ledger cannot be appended") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _secure_unlink_if_regular(path: Path, boundary: Path) -> None:
    with _open_secure_parent_fd(path, boundary) as (parent_fd, name):
        try:
            _assert_secure_stat(os.stat(name, dir_fd=parent_fd, follow_symlinks=False), directory=False)
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileNotFoundError:
            return
        except OSError as error:
            raise CaptureError("INTEGRITY_FAILED", "capture manifest rollback failed") from error


def _verify_existing_blob(path: Path, digest: str, boundary: Path, *, expected_bytes: int) -> None:
    try:
        payload = _read_secure_bytes(path, boundary, missing_code="BLOB_MISSING")
    except CaptureError:
        raise
    if len(payload) != expected_bytes:
        raise CaptureError("BLOB_INTEGRITY_FAILED", "captured blob byte count does not match")
    if _sha256(payload) != digest:
        raise CaptureError("BLOB_INTEGRITY_FAILED", "captured blob digest does not match")


def _coerce_bytes(value: Any) -> bytes:
    if not _is_bytes_like(value):
        raise CaptureError("SCHEMA_INVALID", "capture bytes are invalid")
    return bytes(value)


def _is_bytes_like(value: Any) -> bool:
    return isinstance(value, (bytes, bytearray, memoryview))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _new_capture_id() -> str:
    return f"cap:{uuid.uuid4()}"


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 and character not in {"\t", "\n", "\r"} for character in value)


def _process_lock_for(root: Path) -> threading.RLock:
    key = str(root)
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())
