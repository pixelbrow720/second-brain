"""Synthetic observe-only lifecycle receipts for Activation V2 A2.

The adapter accepts a very small event metadata shape and stores only a digest
and field names in a disposable runtime receipt. It has no hook installer,
does not receive prompt or transcript bodies, and cannot write an authority
store or start a session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping
import uuid

from .activation_v2 import activation_v2_logical_digest, validate_activation_v2_safe_content
from .activation_v2_runtime import (
    ActivationV2RuntimeError,
    DisposableRuntime,
    disposable_json_exists,
    load_disposable_runtime,
    read_disposable_json,
    write_disposable_json,
)
from .canonical import sha256_hex
from .errors import IntegrityError, SemanticValidationError
from .schema_validation import parse_rfc3339_utc


LIFECYCLE_ADAPTER_VERSION = "activation-v2-observe-only/1"
LIFECYCLE_RECEIPT_SCHEMA_VERSION = 1
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_REFERENCE = re.compile(r"^(?:artifact|ref):[a-z0-9][a-z0-9._-]{0,95}$")
_EVENT_FIELDS = {
    "SessionStart": frozenset(("project_binding_digest",)),
    "UserPromptSubmit": frozenset(("memory_mode",)),
    "PreCompact": frozenset(("closure_request",)),
    "PostToolUse": frozenset(("artifact_ids",)),
    "Stop": frozenset(("closure_state",)),
}
_MEMORY_MODES = frozenset(("OFF", "DIRECT", "ASSISTED", "PROJECT_AUTO", "DEEP_REVIEW"))
_CLOSURE_STATES = frozenset(("not_requested", "candidate_requested", "candidate_unavailable"))


class LifecycleObserveOnlyError(SemanticValidationError):
    """An A2 fixture event or receipt violates the observe-only boundary."""


@dataclass(frozen=True)
class ObserveOnlyEvent:
    """Allowlisted lifecycle metadata; it has no prompt or tool-output field."""

    event_id: str
    event_type: str
    project_id: str
    occurred_at: str
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        if type(self) is not ObserveOnlyEvent:
            raise LifecycleObserveOnlyError("lifecycle event is invalid")
        _require_prefixed_uuid(self.event_id, "lifecycle-event", "lifecycle event identity")
        if self.event_type not in _EVENT_FIELDS:
            raise LifecycleObserveOnlyError("lifecycle event type is unsupported")
        if not isinstance(self.project_id, str) or _PROJECT_ID.fullmatch(self.project_id) is None:
            raise LifecycleObserveOnlyError("lifecycle event project is invalid")
        _require_timestamp(self.occurred_at, "lifecycle event timestamp")
        if type(self.metadata) is not dict or set(self.metadata) != _EVENT_FIELDS[self.event_type]:
            raise LifecycleObserveOnlyError("lifecycle event metadata is not allowlisted")
        validate_activation_v2_safe_content(self.metadata)
        _validate_event_metadata(self.event_type, self.metadata)

    @classmethod
    def from_value(cls, value: object) -> "ObserveOnlyEvent":
        if isinstance(value, cls):
            return value
        if type(value) is not dict or set(value) != {
            "event_id",
            "event_type",
            "project_id",
            "occurred_at",
            "metadata",
        }:
            raise LifecycleObserveOnlyError("lifecycle event has unsupported fields")
        try:
            return cls(**value)
        except (TypeError, LifecycleObserveOnlyError) as error:
            raise LifecycleObserveOnlyError("lifecycle event is invalid") from error

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "project_id": self.project_id,
            "occurred_at": self.occurred_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class LifecycleReceipt:
    """Persisted A2 receipt containing no event payload values or task text."""

    schema_version: int
    receipt_id: str
    event_id: str
    project_id: str
    event_type: str
    occurred_at: str
    adapter_version: str
    recording_mode: str
    payload_fields: tuple[str, ...]
    payload_digest: str
    content_persisted: bool
    authority_write: bool
    hook_installed: bool
    receipt_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not LifecycleReceipt:
            raise LifecycleObserveOnlyError("lifecycle receipt is invalid")
        if self.schema_version != LIFECYCLE_RECEIPT_SCHEMA_VERSION or isinstance(self.schema_version, bool):
            raise LifecycleObserveOnlyError("lifecycle receipt version is unsupported")
        _require_prefixed_uuid(self.receipt_id, "lifecycle-receipt", "lifecycle receipt identity")
        _require_prefixed_uuid(self.event_id, "lifecycle-event", "lifecycle receipt event")
        if not isinstance(self.project_id, str) or _PROJECT_ID.fullmatch(self.project_id) is None:
            raise LifecycleObserveOnlyError("lifecycle receipt project is invalid")
        if self.event_type not in _EVENT_FIELDS:
            raise LifecycleObserveOnlyError("lifecycle receipt event type is unsupported")
        _require_timestamp(self.occurred_at, "lifecycle receipt timestamp")
        if self.adapter_version != LIFECYCLE_ADAPTER_VERSION or self.recording_mode != "observe_only":
            raise LifecycleObserveOnlyError("lifecycle receipt adapter boundary is invalid")
        if self.payload_fields != tuple(sorted(_EVENT_FIELDS[self.event_type])):
            raise LifecycleObserveOnlyError("lifecycle receipt fields are invalid")
        if not isinstance(self.payload_digest, str) or _HASH.fullmatch(self.payload_digest) is None:
            raise LifecycleObserveOnlyError("lifecycle receipt payload digest is invalid")
        if self.content_persisted is not False or self.authority_write is not False or self.hook_installed is not False:
            raise LifecycleObserveOnlyError("lifecycle receipt exceeds observe-only authority")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "receipt_digest")
        if self.receipt_digest and self.receipt_digest != expected:
            raise LifecycleObserveOnlyError("lifecycle receipt digest does not match")
        object.__setattr__(self, "receipt_digest", expected)

    @classmethod
    def from_value(cls, value: object) -> "LifecycleReceipt":
        if isinstance(value, cls):
            return value
        expected = {
            "schema_version",
            "receipt_id",
            "event_id",
            "project_id",
            "event_type",
            "occurred_at",
            "adapter_version",
            "recording_mode",
            "payload_fields",
            "payload_digest",
            "content_persisted",
            "authority_write",
            "hook_installed",
            "receipt_digest",
        }
        if type(value) is not dict or set(value) != expected or not isinstance(value["payload_fields"], list):
            raise LifecycleObserveOnlyError("lifecycle receipt has unsupported fields")
        try:
            return cls(
                schema_version=value["schema_version"],
                receipt_id=value["receipt_id"],
                event_id=value["event_id"],
                project_id=value["project_id"],
                event_type=value["event_type"],
                occurred_at=value["occurred_at"],
                adapter_version=value["adapter_version"],
                recording_mode=value["recording_mode"],
                payload_fields=tuple(value["payload_fields"]),
                payload_digest=value["payload_digest"],
                content_persisted=value["content_persisted"],
                authority_write=value["authority_write"],
                hook_installed=value["hook_installed"],
                receipt_digest=value["receipt_digest"],
            )
        except (TypeError, LifecycleObserveOnlyError) as error:
            raise LifecycleObserveOnlyError("lifecycle receipt is invalid") from error

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "event_id": self.event_id,
            "project_id": self.project_id,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "adapter_version": self.adapter_version,
            "recording_mode": self.recording_mode,
            "payload_fields": list(self.payload_fields),
            "payload_digest": self.payload_digest,
            "content_persisted": self.content_persisted,
            "authority_write": self.authority_write,
            "hook_installed": self.hook_installed,
        }
        if include_digest:
            value["receipt_digest"] = self.receipt_digest
        return value


def observe_lifecycle_event(
    runtime: DisposableRuntime,
    event: ObserveOnlyEvent | Mapping[str, Any],
    *,
    receipt_id: str | None = None,
) -> LifecycleReceipt:
    """Record a metadata-only synthetic receipt; no hook installation occurs."""

    handle = load_disposable_runtime(runtime.root)
    handle.manifest.policy.require_resolved("A2")
    selected_event = ObserveOnlyEvent.from_value(event)
    identifier = receipt_id or f"lifecycle-receipt:{uuid.uuid4()}"
    receipt = LifecycleReceipt(
        schema_version=LIFECYCLE_RECEIPT_SCHEMA_VERSION,
        receipt_id=identifier,
        event_id=selected_event.event_id,
        project_id=selected_event.project_id,
        event_type=selected_event.event_type,
        occurred_at=selected_event.occurred_at,
        adapter_version=LIFECYCLE_ADAPTER_VERSION,
        recording_mode="observe_only",
        payload_fields=tuple(sorted(selected_event.metadata)),
        payload_digest=sha256_hex(selected_event.to_dict()),
        content_persisted=False,
        authority_write=False,
        hook_installed=False,
    )
    relative = _receipt_path(receipt.receipt_id)
    if disposable_json_exists(handle, relative):
        raise LifecycleObserveOnlyError("lifecycle receipt identity already exists")
    write_disposable_json(handle, relative, receipt.to_dict())
    return receipt


def load_lifecycle_receipt(runtime: DisposableRuntime, receipt_id: str) -> LifecycleReceipt:
    """Load one receipt from a synthetic runtime and re-check every boundary."""

    _require_prefixed_uuid(receipt_id, "lifecycle-receipt", "lifecycle receipt identity")
    try:
        return LifecycleReceipt.from_value(read_disposable_json(runtime, _receipt_path(receipt_id)))
    except (IntegrityError, LifecycleObserveOnlyError) as error:
        raise IntegrityError("lifecycle receipt is unavailable or invalid") from error


def _receipt_path(receipt_id: str) -> str:
    return f"receipts/lifecycle/{receipt_id.removeprefix('lifecycle-receipt:')}.json"


def _validate_event_metadata(event_type: str, metadata: Mapping[str, Any]) -> None:
    if event_type == "SessionStart":
        digest = metadata["project_binding_digest"]
        if not isinstance(digest, str) or _HASH.fullmatch(digest) is None:
            raise LifecycleObserveOnlyError("session start binding is invalid")
    elif event_type == "UserPromptSubmit":
        if metadata["memory_mode"] not in _MEMORY_MODES:
            raise LifecycleObserveOnlyError("memory mode is invalid")
    elif event_type == "PreCompact":
        if metadata["closure_request"] is not True:
            raise LifecycleObserveOnlyError("pre-compact receipt must request a closure candidate")
    elif event_type == "PostToolUse":
        references = metadata["artifact_ids"]
        if type(references) is not list or not references or len(references) > 16:
            raise LifecycleObserveOnlyError("tool-use references are invalid")
        if len(set(references)) != len(references) or any(
            not isinstance(reference, str) or _OPAQUE_REFERENCE.fullmatch(reference) is None
            for reference in references
        ):
            raise LifecycleObserveOnlyError("tool-use references are invalid")
    else:
        if metadata["closure_state"] not in _CLOSURE_STATES:
            raise LifecycleObserveOnlyError("stop closure state is invalid")


def _require_prefixed_uuid(value: object, prefix: str, label: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix + ":"):
        raise LifecycleObserveOnlyError(f"{label} is invalid")
    if _UUID.fullmatch(value.removeprefix(prefix + ":")) is None:
        raise LifecycleObserveOnlyError(f"{label} is invalid")
    return value


def _require_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise LifecycleObserveOnlyError(f"{label} is invalid")
    try:
        parse_rfc3339_utc(value)
    except Exception as error:
        raise LifecycleObserveOnlyError(f"{label} is invalid") from error
    return value
