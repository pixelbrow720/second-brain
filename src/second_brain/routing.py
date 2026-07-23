"""Project-local M6 model routing, attestation, staging, and rollback contracts.

This module models a fail-closed routing boundary.  It does not make a provider
call, load a global configuration file, or execute a side effect.  Local canary
results are intentionally labelled synthetic and cannot prove live router
behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence
import tomllib
import uuid

from .admission import Lane
from .canonical import sha256_bytes, sha256_hex
from .errors import PathUnsafeError, SemanticValidationError
from .jsonio import load_strict_json
from .profiles import (
    APPROVED_PROFILE_ALIASES,
    NINE_ROUTER_ADAPTER,
    PROFILE_REGISTRY_VERSION,
    SYNTHETIC_MAPPING_EVIDENCE,
    SUPPORTED_EFFORTS,
    ResolvedModelProfile,
    approved_effort_intent,
    load_profile_registry,
    registry_digest,
    resolve_profile,
    validate_profile_registry,
)
from .schema_validation import parse_rfc3339_utc
from .workspace import repository_root


ROUTING_VERSION = "second-brain-routing/6.0.0"
REQUEST_SCHEMA_VERSION = "m6-local-agent-toml/1"
NORMALIZATION_RULES_VERSION = "m6-synthetic-normalization/1"
OBSERVED_NINE_ROUTER_VERSION = "9router/0.5.40"
OBSERVED_CODEX_CLIENT_VERSION = "codex-cli/0.144.6"
STAGING_VERSION = 1
RECEIPT_VERSION = 1
DEFAULT_STAGING_RELATIVE_PATH = Path("dist/global/m6")
_HEX_HASH = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_ID = re.compile(r"^[a-z][a-z0-9._:-]{0,95}$")
_VERSION = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,95}$")
_MODEL = re.compile(r"^cx/gpt-5(?:\.[0-9]+(?:-[a-z0-9]+)?|\.[0-9]+)$")
_PROVIDER = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_RELATIVE_STAGE_FILE = re.compile(r"^(?:agents/)?[a-z0-9][a-z0-9._-]{0,95}\.toml$")
_REASON_BY_STATUS = {
    "MATCH": "EXACT_ROUTE_MATCH",
    "MISMATCH_MODEL": "OBSERVED_MODEL_DIFFERED",
    "MISMATCH_EFFORT": "OBSERVED_EFFORT_DIFFERED",
    "MISMATCH_BOTH": "OBSERVED_MODEL_AND_EFFORT_DIFFERED",
    "MISSING_TELEMETRY": "NO_CORRELATED_TELEMETRY",
    "AMBIGUOUS_TELEMETRY": "TELEMETRY_NOT_ONE_TO_ONE",
    "UNSUPPORTED_MAPPING": "MAPPING_OR_NORMALIZATION_UNSUPPORTED",
}


class ReconciliationStatus(str, Enum):
    """Closed outcome set for M6 route reconciliation."""

    MATCH = "MATCH"
    MISMATCH_MODEL = "MISMATCH_MODEL"
    MISMATCH_EFFORT = "MISMATCH_EFFORT"
    MISMATCH_BOTH = "MISMATCH_BOTH"
    MISSING_TELEMETRY = "MISSING_TELEMETRY"
    AMBIGUOUS_TELEMETRY = "AMBIGUOUS_TELEMETRY"
    UNSUPPORTED_MAPPING = "UNSUPPORTED_MAPPING"


class RouteDisposition(str, Enum):
    """Visible result label for the small DIRECT read-only exception."""

    VERIFIED_ROUTE = "VERIFIED_ROUTE"
    UNVERIFIED_ROUTE = "UNVERIFIED_ROUTE"
    QUARANTINED_ROUTE = "QUARANTINED_ROUTE"


def _require_text(value: object, field_name: str, *, pattern: re.Pattern[str], maximum: int = 96) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or pattern.fullmatch(value) is None:
        raise SemanticValidationError(f"{field_name} is invalid")
    return value


def _require_hash(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _HEX_HASH.fullmatch(value) is None:
        raise SemanticValidationError(f"{field_name} must be a SHA-256 digest")
    return value


def _require_timestamp(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise SemanticValidationError(f"{field_name} is invalid")
    try:
        parse_rfc3339_utc(value)
    except Exception as error:
        raise SemanticValidationError(f"{field_name} is invalid") from error
    return value


def _now_rfc3339() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _coerce_lane(value: Lane | str) -> Lane:
    if isinstance(value, Lane):
        return value
    if isinstance(value, str):
        try:
            return Lane(value)
        except ValueError as error:
            raise SemanticValidationError("lane is invalid") from error
    raise SemanticValidationError("lane is invalid")


def _check_exact_keys(value: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    if set(value) != allowed:
        raise SemanticValidationError(f"{label} has unsupported or missing fields")


def _digest_identifier(value: str) -> str:
    return sha256_hex({"opaque_identifier": value})


def _toml_quote(value: str) -> str:
    """Quote already-constrained local data as a TOML basic string."""

    if not isinstance(value, str) or "\x00" in value or "\n" in value or "\r" in value:
        raise SemanticValidationError("generated TOML value is unsafe")
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


@dataclass(frozen=True)
class RouteIntent:
    """Immutable intent captured before any routing fields are serialized."""

    route_id: str
    task_id: str
    node_id: str
    profile_alias: str
    effort_intent: str
    registry_version: int
    registry_digest: str
    adapter_name: str
    adapter_version: str
    client_version: str
    router_version: str
    normalization_rules_version: str
    mapping_evidence: str
    mapping_digest: str
    correlation_id: str
    created_at: str
    intent_digest: str = field(default="")

    def __post_init__(self) -> None:
        _require_text(self.route_id, "route_id", pattern=_OPAQUE_ID)
        _require_text(self.task_id, "task_id", pattern=_OPAQUE_ID)
        _require_text(self.node_id, "node_id", pattern=_OPAQUE_ID)
        _require_text(self.correlation_id, "correlation_id", pattern=_OPAQUE_ID)
        if self.route_id == self.correlation_id:
            raise SemanticValidationError("route and correlation identities must differ")
        if self.profile_alias not in APPROVED_PROFILE_ALIASES:
            raise SemanticValidationError("profile alias is not approved")
        if self.effort_intent not in SUPPORTED_EFFORTS:
            raise SemanticValidationError("effort intent is unsupported")
        if self.effort_intent != approved_effort_intent(self.profile_alias):
            raise SemanticValidationError("route intent effort does not match its profile alias")
        if self.registry_version != PROFILE_REGISTRY_VERSION or isinstance(self.registry_version, bool):
            raise SemanticValidationError("registry version is unsupported")
        _require_hash(self.registry_digest, "registry_digest")
        _require_hash(self.mapping_digest, "mapping_digest")
        if self.adapter_name != NINE_ROUTER_ADAPTER:
            raise SemanticValidationError("adapter is unsupported")
        if self.adapter_version != OBSERVED_NINE_ROUTER_VERSION:
            raise SemanticValidationError("adapter version is unsupported")
        if self.client_version != OBSERVED_CODEX_CLIENT_VERSION:
            raise SemanticValidationError("client version is unsupported")
        if self.router_version != OBSERVED_NINE_ROUTER_VERSION:
            raise SemanticValidationError("router version is unsupported")
        if self.normalization_rules_version != NORMALIZATION_RULES_VERSION:
            raise SemanticValidationError("normalization rules are unsupported")
        if self.mapping_evidence != SYNTHETIC_MAPPING_EVIDENCE:
            raise SemanticValidationError("mapping evidence is unavailable")
        _require_timestamp(self.created_at, "created_at")
        computed = sha256_hex(self._digest_input())
        if self.intent_digest and self.intent_digest != computed:
            raise SemanticValidationError("intent digest does not match immutable route intent")
        object.__setattr__(self, "intent_digest", computed)

    def _digest_input(self) -> dict[str, object]:
        return {
            "route_id": self.route_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "profile_alias": self.profile_alias,
            "effort_intent": self.effort_intent,
            "registry_version": self.registry_version,
            "registry_digest": self.registry_digest,
            "adapter_name": self.adapter_name,
            "adapter_version": self.adapter_version,
            "client_version": self.client_version,
            "router_version": self.router_version,
            "normalization_rules_version": self.normalization_rules_version,
            "mapping_evidence": self.mapping_evidence,
            "mapping_digest": self.mapping_digest,
            "correlation_id": self.correlation_id,
            "created_at": self.created_at,
        }

    @classmethod
    def create(
        cls,
        *,
        route_id: str,
        task_id: str,
        node_id: str,
        profile_alias: str,
        correlation_id: str | None = None,
        created_at: str | None = None,
        registry: Mapping[str, Any] | None = None,
    ) -> "RouteIntent":
        """Create a snapshot from the active registry before serialization."""

        resolved = resolve_profile(profile_alias, registry)
        return cls(
            route_id=route_id,
            task_id=task_id,
            node_id=node_id,
            profile_alias=resolved.alias,
            effort_intent=resolved.effort_intent,
            registry_version=resolved.registry_version,
            registry_digest=resolved.registry_digest,
            adapter_name=resolved.adapter_name,
            adapter_version=resolved.adapter_version,
            client_version=resolved.client_version,
            router_version=resolved.router_version,
            normalization_rules_version=resolved.normalization_rules_version,
            mapping_evidence=resolved.mapping_evidence,
            mapping_digest=resolved.mapping_digest,
            correlation_id=correlation_id or f"corr:{uuid.uuid4()}",
            created_at=created_at or _now_rfc3339(),
        )


@dataclass(frozen=True)
class SerializedRoute:
    """Allowlisted routing fields actually proposed for a local fixture request."""

    route_id: str
    correlation_id: str
    profile_alias: str
    raw_model_slug: str
    serialized_effort_field: str
    serialized_effort_value: str
    request_schema_version: str
    registry_version: int
    registry_digest: str
    adapter_name: str
    adapter_version: str
    router_version: str
    normalization_rules_version: str
    mapping_evidence: str
    intent_digest: str
    payload_hash: str
    sent_at: str
    serialized_route_digest: str = field(default="")

    def __post_init__(self) -> None:
        _require_text(self.route_id, "route_id", pattern=_OPAQUE_ID)
        _require_text(self.correlation_id, "correlation_id", pattern=_OPAQUE_ID)
        if self.profile_alias not in APPROVED_PROFILE_ALIASES:
            raise SemanticValidationError("profile alias is not approved")
        _require_text(self.raw_model_slug, "raw_model_slug", pattern=_MODEL)
        if self.serialized_effort_field != "model_reasoning_effort":
            raise SemanticValidationError("serialized effort field is unsupported")
        if self.serialized_effort_value not in SUPPORTED_EFFORTS:
            raise SemanticValidationError("serialized effort is unsupported")
        if self.serialized_effort_value != approved_effort_intent(self.profile_alias):
            raise SemanticValidationError("serialized effort does not match its profile alias")
        if self.request_schema_version != REQUEST_SCHEMA_VERSION:
            raise SemanticValidationError("request schema version is unsupported")
        if self.registry_version != PROFILE_REGISTRY_VERSION or isinstance(self.registry_version, bool):
            raise SemanticValidationError("registry version is unsupported")
        _require_hash(self.registry_digest, "registry_digest")
        if self.adapter_name != NINE_ROUTER_ADAPTER:
            raise SemanticValidationError("adapter is unsupported")
        if self.adapter_version != OBSERVED_NINE_ROUTER_VERSION:
            raise SemanticValidationError("adapter version is unsupported")
        if self.router_version != OBSERVED_NINE_ROUTER_VERSION:
            raise SemanticValidationError("router version is unsupported")
        if self.normalization_rules_version != NORMALIZATION_RULES_VERSION:
            raise SemanticValidationError("normalization rules are unsupported")
        if self.mapping_evidence != SYNTHETIC_MAPPING_EVIDENCE:
            raise SemanticValidationError("mapping evidence is unavailable")
        _require_hash(self.intent_digest, "intent_digest")
        _require_hash(self.payload_hash, "payload_hash")
        _require_timestamp(self.sent_at, "sent_at")
        computed = sha256_hex(self._digest_input())
        if self.serialized_route_digest and self.serialized_route_digest != computed:
            raise SemanticValidationError("serialized route digest does not match")
        object.__setattr__(self, "serialized_route_digest", computed)

    def routing_fields(self) -> dict[str, object]:
        """Return the only fields admitted to the request payload hash."""

        return {
            "route_id": self.route_id,
            "correlation_id": self.correlation_id,
            "raw_model_slug": self.raw_model_slug,
            "serialized_effort_field": self.serialized_effort_field,
            "serialized_effort_value": self.serialized_effort_value,
            "request_schema_version": self.request_schema_version,
            "registry_version": self.registry_version,
            "registry_digest": self.registry_digest,
            "adapter_name": self.adapter_name,
            "adapter_version": self.adapter_version,
            "router_version": self.router_version,
            "normalization_rules_version": self.normalization_rules_version,
        }

    def _digest_input(self) -> dict[str, object]:
        return {
            **self.routing_fields(),
            "mapping_evidence": self.mapping_evidence,
            "intent_digest": self.intent_digest,
            "payload_hash": self.payload_hash,
            "sent_at": self.sent_at,
        }


def create_route_intent(**kwargs: Any) -> RouteIntent:
    """Convenience wrapper kept as the public M6 intent entry point."""

    return RouteIntent.create(**kwargs)


def serialize_route(
    intent: RouteIntent,
    *,
    registry: Mapping[str, Any] | None = None,
    sent_at: str | None = None,
    request_schema_version: str = REQUEST_SCHEMA_VERSION,
) -> SerializedRoute:
    """Resolve and capture routing fields without accepting prompt or header data."""

    if not isinstance(intent, RouteIntent):
        raise SemanticValidationError("route intent is required")
    resolved = resolve_profile(intent.profile_alias, registry)
    if (
        resolved.registry_version != intent.registry_version
        or resolved.registry_digest != intent.registry_digest
        or resolved.mapping_digest != intent.mapping_digest
        or resolved.effort_intent != intent.effort_intent
        or resolved.adapter_name != intent.adapter_name
        or resolved.adapter_version != intent.adapter_version
        or resolved.client_version != intent.client_version
        or resolved.router_version != intent.router_version
        or resolved.normalization_rules_version != intent.normalization_rules_version
        or resolved.mapping_evidence != intent.mapping_evidence
    ):
        raise SemanticValidationError("registry changed after route intent was created")
    if request_schema_version != REQUEST_SCHEMA_VERSION:
        raise SemanticValidationError("request schema version is unsupported")
    payload_fields = {
        "route_id": intent.route_id,
        "correlation_id": intent.correlation_id,
        "raw_model_slug": resolved.raw_model_slug,
        "serialized_effort_field": resolved.serialized_effort_field,
        "serialized_effort_value": resolved.serialized_effort_value,
        "request_schema_version": request_schema_version,
        "registry_version": intent.registry_version,
        "registry_digest": intent.registry_digest,
        "adapter_name": intent.adapter_name,
        "adapter_version": intent.adapter_version,
        "router_version": intent.router_version,
        "normalization_rules_version": intent.normalization_rules_version,
    }
    return SerializedRoute(
        route_id=intent.route_id,
        correlation_id=intent.correlation_id,
        profile_alias=intent.profile_alias,
        raw_model_slug=resolved.raw_model_slug,
        serialized_effort_field=resolved.serialized_effort_field,
        serialized_effort_value=resolved.serialized_effort_value,
        request_schema_version=request_schema_version,
        registry_version=intent.registry_version,
        registry_digest=intent.registry_digest,
        adapter_name=intent.adapter_name,
        adapter_version=intent.adapter_version,
        router_version=intent.router_version,
        normalization_rules_version=intent.normalization_rules_version,
        mapping_evidence=intent.mapping_evidence,
        intent_digest=intent.intent_digest,
        payload_hash=sha256_hex(payload_fields),
        sent_at=sent_at or _now_rfc3339(),
    )


@dataclass(frozen=True)
class TelemetryObservation:
    """One strict, correlated observation; raw telemetry blobs are excluded."""

    route_id: str
    correlation_id: str
    observed_model_slug: str
    observed_effort: str
    router_request_id: str
    provider: str
    adapter_name: str
    adapter_version: str
    router_version: str
    normalization_rules_version: str
    registry_digest: str
    observed_at: str

    _FIELDS = frozenset(
        (
            "route_id",
            "correlation_id",
            "observed_model_slug",
            "observed_effort",
            "router_request_id",
            "provider",
            "adapter_name",
            "adapter_version",
            "router_version",
            "normalization_rules_version",
            "registry_digest",
            "observed_at",
        )
    )

    def __post_init__(self) -> None:
        _require_text(self.route_id, "route_id", pattern=_OPAQUE_ID)
        _require_text(self.correlation_id, "correlation_id", pattern=_OPAQUE_ID)
        _require_text(self.observed_model_slug, "observed_model_slug", pattern=_MODEL)
        _require_text(self.observed_effort, "observed_effort", pattern=_VERSION, maximum=16)
        _require_text(self.router_request_id, "router_request_id", pattern=_OPAQUE_ID)
        _require_text(self.provider, "provider", pattern=_PROVIDER, maximum=64)
        _require_text(self.adapter_name, "adapter_name", pattern=_VERSION)
        _require_text(self.adapter_version, "adapter_version", pattern=_VERSION)
        _require_text(self.router_version, "router_version", pattern=_VERSION)
        _require_text(
            self.normalization_rules_version,
            "normalization_rules_version",
            pattern=_VERSION,
        )
        _require_hash(self.registry_digest, "registry_digest")
        _require_timestamp(self.observed_at, "observed_at")

    @classmethod
    def from_value(cls, value: "TelemetryObservation | Mapping[str, Any]") -> "TelemetryObservation":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise SemanticValidationError("telemetry observation is invalid")
        _check_exact_keys(value, cls._FIELDS, "telemetry observation")
        try:
            return cls(**dict(value))
        except (TypeError, ValueError) as error:
            raise SemanticValidationError("telemetry observation is invalid") from error

    def digest(self) -> str:
        """Digest only strict telemetry routing fields, never an input blob."""

        return sha256_hex(
            {
                "route_id": self.route_id,
                "correlation_id": self.correlation_id,
                "observed_model_slug": self.observed_model_slug,
                "observed_effort": self.observed_effort,
                "router_request_id_digest": _digest_identifier(self.router_request_id),
                "provider": self.provider,
                "adapter_name": self.adapter_name,
                "adapter_version": self.adapter_version,
                "router_version": self.router_version,
                "normalization_rules_version": self.normalization_rules_version,
                "registry_digest": self.registry_digest,
                "observed_at": self.observed_at,
            }
        )


@dataclass(frozen=True)
class RouteReconciliation:
    """A redacted, controlled reconciliation result for one serialized route."""

    status: ReconciliationStatus
    reason_code: str
    route_id: str
    correlation_id: str
    registry_digest: str
    intent_digest: str
    serialized_route_digest: str
    observed_at: str | None
    observation_digest: str | None
    router_request_id_digest: str | None
    reconciled_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, ReconciliationStatus):
            try:
                object.__setattr__(self, "status", ReconciliationStatus(self.status))
            except (TypeError, ValueError) as error:
                raise SemanticValidationError("reconciliation status is invalid") from error
        expected_reason = _REASON_BY_STATUS[self.status.value]
        if self.reason_code != expected_reason:
            raise SemanticValidationError("reconciliation reason code is invalid")
        _require_text(self.route_id, "route_id", pattern=_OPAQUE_ID)
        _require_text(self.correlation_id, "correlation_id", pattern=_OPAQUE_ID)
        _require_hash(self.registry_digest, "registry_digest")
        _require_hash(self.intent_digest, "intent_digest")
        _require_hash(self.serialized_route_digest, "serialized_route_digest")
        if self.observed_at is not None:
            _require_timestamp(self.observed_at, "observed_at")
        if self.observation_digest is not None:
            _require_hash(self.observation_digest, "observation_digest")
        if self.router_request_id_digest is not None:
            _require_hash(self.router_request_id_digest, "router_request_id_digest")
        _require_timestamp(self.reconciled_at, "reconciled_at")
        compared_statuses = {
            ReconciliationStatus.MATCH,
            ReconciliationStatus.MISMATCH_MODEL,
            ReconciliationStatus.MISMATCH_EFFORT,
            ReconciliationStatus.MISMATCH_BOTH,
        }
        if self.status in compared_statuses and (
            self.observed_at is None
            or self.observation_digest is None
            or self.router_request_id_digest is None
        ):
            raise SemanticValidationError("compared reconciliation must bind one observation")
        if self.status is ReconciliationStatus.MISSING_TELEMETRY and any(
            value is not None
            for value in (self.observed_at, self.observation_digest, self.router_request_id_digest)
        ):
            raise SemanticValidationError("missing telemetry reconciliation cannot bind an observation")


def _reconciliation(
    serialized: SerializedRoute,
    status: ReconciliationStatus,
    *,
    observation: TelemetryObservation | None = None,
    reconciled_at: str | None = None,
) -> RouteReconciliation:
    return RouteReconciliation(
        status=status,
        reason_code=_REASON_BY_STATUS[status.value],
        route_id=serialized.route_id,
        correlation_id=serialized.correlation_id,
        registry_digest=serialized.registry_digest,
        intent_digest=serialized.intent_digest,
        serialized_route_digest=serialized.serialized_route_digest,
        observed_at=None if observation is None else observation.observed_at,
        observation_digest=None if observation is None else observation.digest(),
        router_request_id_digest=None
        if observation is None
        else _digest_identifier(observation.router_request_id),
        reconciled_at=reconciled_at or serialized.sent_at,
    )


def _serialized_mapping_supported(serialized: SerializedRoute) -> bool:
    return (
        serialized.adapter_name == NINE_ROUTER_ADAPTER
        and serialized.request_schema_version == REQUEST_SCHEMA_VERSION
        and serialized.registry_version == PROFILE_REGISTRY_VERSION
        and serialized.adapter_version == OBSERVED_NINE_ROUTER_VERSION
        and serialized.router_version == OBSERVED_NINE_ROUTER_VERSION
        and serialized.normalization_rules_version == NORMALIZATION_RULES_VERSION
        and serialized.mapping_evidence == SYNTHETIC_MAPPING_EVIDENCE
        and serialized.serialized_effort_field == "model_reasoning_effort"
        and serialized.serialized_effort_value in SUPPORTED_EFFORTS
    )


def _normalize_exact(value: str, *, rules_version: str, kind: str) -> str:
    """Normalize only declared exact M6 fixture values; never broaden effort."""

    if rules_version != NORMALIZATION_RULES_VERSION:
        raise SemanticValidationError("normalization rules are unsupported")
    allowed = SUPPORTED_EFFORTS if kind == "effort" else None
    if allowed is not None and value not in allowed:
        raise SemanticValidationError("observed effort is unsupported")
    if kind == "model" and _MODEL.fullmatch(value) is None:
        raise SemanticValidationError("observed model is unsupported")
    return value


def _candidate_observation_records(
    serialized: SerializedRoute,
    observations: Iterable[TelemetryObservation | Mapping[str, Any] | object],
) -> tuple[object, ...]:
    records: list[object] = []
    for value in observations:
        if isinstance(value, TelemetryObservation):
            if value.route_id == serialized.route_id or value.correlation_id == serialized.correlation_id:
                records.append(value)
            continue
        if isinstance(value, Mapping):
            route_id = value.get("route_id")
            correlation_id = value.get("correlation_id")
            if route_id == serialized.route_id or correlation_id == serialized.correlation_id:
                records.append(value)
    return tuple(records)


def reconcile_route(
    serialized: SerializedRoute,
    observations: Iterable[TelemetryObservation | Mapping[str, Any] | object],
    *,
    reconciled_at: str | None = None,
) -> RouteReconciliation:
    """Compare a frozen serialized route with exactly one correlated observation."""

    if not isinstance(serialized, SerializedRoute):
        raise SemanticValidationError("serialized route is required")
    if not _serialized_mapping_supported(serialized):
        return _reconciliation(
            serialized,
            ReconciliationStatus.UNSUPPORTED_MAPPING,
            reconciled_at=reconciled_at,
        )
    try:
        records = _candidate_observation_records(serialized, observations)
    except Exception:
        # A telemetry iterator/parser defect is never transformed into a match.
        return _reconciliation(
            serialized,
            ReconciliationStatus.AMBIGUOUS_TELEMETRY,
            reconciled_at=reconciled_at,
        )
    if not records:
        return _reconciliation(
            serialized,
            ReconciliationStatus.MISSING_TELEMETRY,
            reconciled_at=reconciled_at,
        )
    if len(records) != 1:
        return _reconciliation(
            serialized,
            ReconciliationStatus.AMBIGUOUS_TELEMETRY,
            reconciled_at=reconciled_at,
        )
    try:
        observation = TelemetryObservation.from_value(records[0])
    except Exception:
        return _reconciliation(
            serialized,
            ReconciliationStatus.AMBIGUOUS_TELEMETRY,
            reconciled_at=reconciled_at,
        )
    if (
        observation.route_id != serialized.route_id
        or observation.correlation_id != serialized.correlation_id
    ):
        return _reconciliation(
            serialized,
            ReconciliationStatus.AMBIGUOUS_TELEMETRY,
            observation=observation,
            reconciled_at=reconciled_at,
        )
    if (
        observation.adapter_name != serialized.adapter_name
        or observation.adapter_version != serialized.adapter_version
        or observation.router_version != serialized.router_version
        or observation.normalization_rules_version != serialized.normalization_rules_version
        or observation.registry_digest != serialized.registry_digest
    ):
        return _reconciliation(
            serialized,
            ReconciliationStatus.UNSUPPORTED_MAPPING,
            observation=observation,
            reconciled_at=reconciled_at,
        )
    try:
        expected_model = _normalize_exact(
            serialized.raw_model_slug,
            rules_version=serialized.normalization_rules_version,
            kind="model",
        )
        expected_effort = _normalize_exact(
            serialized.serialized_effort_value,
            rules_version=serialized.normalization_rules_version,
            kind="effort",
        )
        observed_model = _normalize_exact(
            observation.observed_model_slug,
            rules_version=observation.normalization_rules_version,
            kind="model",
        )
        observed_effort = _normalize_exact(
            observation.observed_effort,
            rules_version=observation.normalization_rules_version,
            kind="effort",
        )
    except Exception:
        return _reconciliation(
            serialized,
            ReconciliationStatus.UNSUPPORTED_MAPPING,
            observation=observation,
            reconciled_at=reconciled_at,
        )
    model_match = observed_model == expected_model
    effort_match = observed_effort == expected_effort
    if model_match and effort_match:
        status = ReconciliationStatus.MATCH
    elif not model_match and effort_match:
        status = ReconciliationStatus.MISMATCH_MODEL
    elif model_match and not effort_match:
        status = ReconciliationStatus.MISMATCH_EFFORT
    else:
        status = ReconciliationStatus.MISMATCH_BOTH
    return _reconciliation(serialized, status, observation=observation, reconciled_at=reconciled_at)


@dataclass(frozen=True)
class RouteReceipt:
    """A deterministic allowlist receipt with no body, headers, URL, or blob."""

    receipt_version: int
    route_id: str
    correlation_id: str
    task_id_digest: str
    node_id_digest: str
    profile_alias: str
    effort_intent: str
    registry_version: int
    registry_digest: str
    adapter_name: str
    adapter_version: str
    router_version: str
    normalization_rules_version: str
    routing_fields_hash: str
    intent_digest: str
    serialized_route_digest: str
    status: ReconciliationStatus
    reason_code: str
    created_at: str
    sent_at: str
    reconciled_at: str
    observed_at: str | None
    observation_digest: str | None
    router_request_id_digest: str | None
    evidence_scope: str
    live_attested: bool
    receipt_digest: str = field(default="")

    def __post_init__(self) -> None:
        if self.receipt_version != RECEIPT_VERSION or isinstance(self.receipt_version, bool):
            raise SemanticValidationError("route receipt version is invalid")
        _require_text(self.route_id, "route_id", pattern=_OPAQUE_ID)
        _require_text(self.correlation_id, "correlation_id", pattern=_OPAQUE_ID)
        _require_hash(self.task_id_digest, "task_id_digest")
        _require_hash(self.node_id_digest, "node_id_digest")
        if self.profile_alias not in APPROVED_PROFILE_ALIASES:
            raise SemanticValidationError("profile alias is not approved")
        if self.effort_intent not in SUPPORTED_EFFORTS:
            raise SemanticValidationError("effort intent is unsupported")
        if self.registry_version != PROFILE_REGISTRY_VERSION or isinstance(self.registry_version, bool):
            raise SemanticValidationError("registry version is unsupported")
        _require_hash(self.registry_digest, "registry_digest")
        if self.adapter_name != NINE_ROUTER_ADAPTER:
            raise SemanticValidationError("adapter is unsupported")
        if self.adapter_version != OBSERVED_NINE_ROUTER_VERSION:
            raise SemanticValidationError("adapter version is unsupported")
        if self.router_version != OBSERVED_NINE_ROUTER_VERSION:
            raise SemanticValidationError("router version is unsupported")
        if self.normalization_rules_version != NORMALIZATION_RULES_VERSION:
            raise SemanticValidationError("normalization rules are unsupported")
        _require_hash(self.routing_fields_hash, "routing_fields_hash")
        _require_hash(self.intent_digest, "intent_digest")
        _require_hash(self.serialized_route_digest, "serialized_route_digest")
        if not isinstance(self.status, ReconciliationStatus):
            try:
                object.__setattr__(self, "status", ReconciliationStatus(self.status))
            except (TypeError, ValueError) as error:
                raise SemanticValidationError("route receipt status is invalid") from error
        if self.reason_code != _REASON_BY_STATUS[self.status.value]:
            raise SemanticValidationError("route receipt reason code is invalid")
        _require_timestamp(self.created_at, "created_at")
        _require_timestamp(self.sent_at, "sent_at")
        _require_timestamp(self.reconciled_at, "reconciled_at")
        if self.observed_at is not None:
            _require_timestamp(self.observed_at, "observed_at")
        if self.observation_digest is not None:
            _require_hash(self.observation_digest, "observation_digest")
        if self.router_request_id_digest is not None:
            _require_hash(self.router_request_id_digest, "router_request_id_digest")
        if self.evidence_scope != SYNTHETIC_MAPPING_EVIDENCE or self.live_attested is not False:
            raise SemanticValidationError("route receipt cannot claim live route attestation")
        if self.status in {
            ReconciliationStatus.MATCH,
            ReconciliationStatus.MISMATCH_MODEL,
            ReconciliationStatus.MISMATCH_EFFORT,
            ReconciliationStatus.MISMATCH_BOTH,
        } and (
            self.observed_at is None
            or self.observation_digest is None
            or self.router_request_id_digest is None
        ):
            raise SemanticValidationError("compared route receipt must bind one observation")
        if self.status is ReconciliationStatus.MISSING_TELEMETRY and any(
            value is not None
            for value in (self.observed_at, self.observation_digest, self.router_request_id_digest)
        ):
            raise SemanticValidationError("missing telemetry route receipt cannot bind an observation")
        computed = sha256_hex(self._digest_input())
        if self.receipt_digest and self.receipt_digest != computed:
            raise SemanticValidationError("route receipt digest does not match")
        object.__setattr__(self, "receipt_digest", computed)

    def _digest_input(self) -> dict[str, object]:
        return {
            "receipt_version": self.receipt_version,
            "route_id": self.route_id,
            "correlation_id": self.correlation_id,
            "task_id_digest": self.task_id_digest,
            "node_id_digest": self.node_id_digest,
            "profile_alias": self.profile_alias,
            "effort_intent": self.effort_intent,
            "registry_version": self.registry_version,
            "registry_digest": self.registry_digest,
            "adapter_name": self.adapter_name,
            "adapter_version": self.adapter_version,
            "router_version": self.router_version,
            "normalization_rules_version": self.normalization_rules_version,
            "routing_fields_hash": self.routing_fields_hash,
            "intent_digest": self.intent_digest,
            "serialized_route_digest": self.serialized_route_digest,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "created_at": self.created_at,
            "sent_at": self.sent_at,
            "reconciled_at": self.reconciled_at,
            "observed_at": self.observed_at,
            "observation_digest": self.observation_digest,
            "router_request_id_digest": self.router_request_id_digest,
            "evidence_scope": self.evidence_scope,
            "live_attested": self.live_attested,
        }

    def to_dict(self) -> dict[str, object]:
        """Return the receipt's fixed allowlist; raw telemetry is unrecoverable."""

        return {**self._digest_input(), "receipt_digest": self.receipt_digest}


def create_route_receipt(
    intent: RouteIntent,
    serialized: SerializedRoute,
    reconciliation: RouteReconciliation,
) -> RouteReceipt:
    """Bind a reconciliation to the exact immutable intent and serialized route."""

    if not isinstance(intent, RouteIntent) or not isinstance(serialized, SerializedRoute):
        raise SemanticValidationError("intent and serialized route are required")
    if not isinstance(reconciliation, RouteReconciliation):
        raise SemanticValidationError("route reconciliation is required")
    if (
        intent.route_id != serialized.route_id
        or intent.correlation_id != serialized.correlation_id
        or intent.intent_digest != serialized.intent_digest
        or intent.registry_digest != serialized.registry_digest
        or intent.registry_version != serialized.registry_version
        or reconciliation.route_id != serialized.route_id
        or reconciliation.correlation_id != serialized.correlation_id
        or reconciliation.intent_digest != intent.intent_digest
        or reconciliation.serialized_route_digest != serialized.serialized_route_digest
        or reconciliation.registry_digest != intent.registry_digest
    ):
        raise SemanticValidationError("route receipt inputs are not bound to one snapshot")
    return RouteReceipt(
        receipt_version=RECEIPT_VERSION,
        route_id=intent.route_id,
        correlation_id=intent.correlation_id,
        task_id_digest=_digest_identifier(intent.task_id),
        node_id_digest=_digest_identifier(intent.node_id),
        profile_alias=intent.profile_alias,
        effort_intent=intent.effort_intent,
        registry_version=intent.registry_version,
        registry_digest=intent.registry_digest,
        adapter_name=intent.adapter_name,
        adapter_version=intent.adapter_version,
        router_version=intent.router_version,
        normalization_rules_version=intent.normalization_rules_version,
        routing_fields_hash=serialized.payload_hash,
        intent_digest=intent.intent_digest,
        serialized_route_digest=serialized.serialized_route_digest,
        status=reconciliation.status,
        reason_code=reconciliation.reason_code,
        created_at=intent.created_at,
        sent_at=serialized.sent_at,
        reconciled_at=reconciliation.reconciled_at,
        observed_at=reconciliation.observed_at,
        observation_digest=reconciliation.observation_digest,
        router_request_id_digest=reconciliation.router_request_id_digest,
        evidence_scope=SYNTHETIC_MAPPING_EVIDENCE,
        live_attested=False,
    )


def side_effect_allowed(
    receipt: RouteReceipt | None,
    lane: Lane | str,
    *,
    intent: RouteIntent | None = None,
    permission_allowed: bool = False,
    registry: Mapping[str, Any] | None = None,
) -> bool:
    """Deny every M6 receipt at a material side-effect boundary.

    M6 emits only synthetic-local telemetry evidence.  Its reconciliation is
    useful diagnostics, but it has no authenticated live-attestation type and
    therefore cannot authorize a commit, publish, installation, or other
    material action.  A later milestone must introduce a separate trusted
    receipt contract before this boundary can allow anything.
    """

    # Do not inspect caller-controlled receipt objects here: no current M6
    # receipt, including a synthetic MATCH, carries live authorization.
    return False


def route_disposition(
    receipt: RouteReceipt | None,
    lane: Lane | str,
    *,
    read_only: bool = False,
) -> RouteDisposition:
    """Expose the narrowly permitted DIRECT missing-telemetry warning label."""

    try:
        resolved_lane = _coerce_lane(lane)
    except Exception:
        return RouteDisposition.QUARANTINED_ROUTE
    if not isinstance(receipt, RouteReceipt):
        return RouteDisposition.QUARANTINED_ROUTE
    if receipt.status is ReconciliationStatus.MATCH:
        return RouteDisposition.VERIFIED_ROUTE
    if (
        resolved_lane is Lane.DIRECT
        and read_only
        and receipt.status is ReconciliationStatus.MISSING_TELEMETRY
    ):
        return RouteDisposition.UNVERIFIED_ROUTE
    return RouteDisposition.QUARANTINED_ROUTE


def retry_allowed(
    reconciliation: RouteReconciliation | None,
    *,
    retries_completed: int,
    side_effect_started: bool,
    last_known_good_pinned: bool,
) -> bool:
    """Allow one pre-side-effect retry only for a pinned mapping mismatch.

    Telemetry loss and ambiguity never become retry-to-success paths, because a
    retry would hide the original evidence defect instead of resolving it.
    """

    if (
        not isinstance(reconciliation, RouteReconciliation)
        or not isinstance(retries_completed, int)
        or isinstance(retries_completed, bool)
        or retries_completed != 0
        or side_effect_started is not False
        or last_known_good_pinned is not True
    ):
        return False
    return reconciliation.status in {
        ReconciliationStatus.MISMATCH_MODEL,
        ReconciliationStatus.MISMATCH_EFFORT,
        ReconciliationStatus.MISMATCH_BOTH,
        ReconciliationStatus.UNSUPPORTED_MAPPING,
    }


def synthetic_observation(intent: RouteIntent, serialized: SerializedRoute) -> TelemetryObservation:
    """Create a deterministic, non-sensitive local fixture observation only."""

    if intent.intent_digest != serialized.intent_digest:
        raise SemanticValidationError("synthetic observation requires a bound route")
    return TelemetryObservation(
        route_id=serialized.route_id,
        correlation_id=serialized.correlation_id,
        observed_model_slug=serialized.raw_model_slug,
        observed_effort=serialized.serialized_effort_value,
        router_request_id=f"router:{serialized.route_id}",
        provider="synthetic-local",
        adapter_name=serialized.adapter_name,
        adapter_version=serialized.adapter_version,
        router_version=serialized.router_version,
        normalization_rules_version=serialized.normalization_rules_version,
        registry_digest=serialized.registry_digest,
        observed_at=serialized.sent_at,
    )


@dataclass(frozen=True)
class CanaryCheck:
    """One synthetic canary route and its redacted outcome."""

    profile_alias: str
    repeat: int
    intent: RouteIntent
    serialized: SerializedRoute
    reconciliation: RouteReconciliation
    receipt: RouteReceipt


@dataclass(frozen=True)
class SyntheticCanaryResult:
    """All-or-nothing local canary report; it cannot be promoted as live evidence."""

    repeats: int
    checks: tuple[CanaryCheck, ...]
    evidence_scope: str = SYNTHETIC_MAPPING_EVIDENCE
    live_attested: bool = False

    @property
    def passed(self) -> bool:
        expected = {(alias, repeat) for repeat in range(self.repeats) for alias in APPROVED_PROFILE_ALIASES}
        actual = {(check.profile_alias, check.repeat) for check in self.checks}
        correlations = {check.intent.correlation_id for check in self.checks}
        return (
            self.repeats in {1, 5}
            and self.evidence_scope == SYNTHETIC_MAPPING_EVIDENCE
            and self.live_attested is False
            and len(self.checks) == len(expected)
            and actual == expected
            and len(correlations) == len(self.checks)
            and all(check.reconciliation.status is ReconciliationStatus.MATCH for check in self.checks)
        )

    @property
    def match_count(self) -> int:
        return sum(check.reconciliation.status is ReconciliationStatus.MATCH for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "repeats": self.repeats,
            "expected_checks": len(APPROVED_PROFILE_ALIASES) * self.repeats,
            "match_count": self.match_count,
            "passed": self.passed,
            "evidence_scope": self.evidence_scope,
            "live_attested": self.live_attested,
            "receipts": [check.receipt.to_dict() for check in self.checks],
        }


def run_synthetic_canary(
    *,
    repeats: int = 1,
    registry: Mapping[str, Any] | None = None,
    timestamp: str = "2026-07-23T00:00:00Z",
    observation_overrides: Mapping[
        tuple[str, int], Sequence[TelemetryObservation | Mapping[str, Any] | object] | None
    ]
    | None = None,
) -> SyntheticCanaryResult:
    """Exercise all aliases locally without a network call or provider mutation."""

    if repeats not in {1, 5} or isinstance(repeats, bool):
        raise SemanticValidationError("synthetic canary repeats must be exactly one or five")
    _require_timestamp(timestamp, "timestamp")
    source = load_profile_registry() if registry is None else dict(registry)
    validate_profile_registry(source)
    overrides = observation_overrides or {}
    checks: list[CanaryCheck] = []
    for repeat in range(repeats):
        for alias in APPROVED_PROFILE_ALIASES:
            intent = RouteIntent.create(
                route_id=f"route:canary-{repeat}-{alias}",
                task_id="task:synthetic-canary",
                node_id=f"node:{alias}",
                profile_alias=alias,
                correlation_id=f"corr:canary-{repeat}-{alias}",
                created_at=timestamp,
                registry=source,
            )
            serialized = serialize_route(intent, registry=source, sent_at=timestamp)
            default_records: Sequence[TelemetryObservation | Mapping[str, Any] | object] = (
                synthetic_observation(intent, serialized),
            )
            records = overrides.get((alias, repeat), default_records)
            reconciliation = reconcile_route(serialized, () if records is None else records)
            receipt = create_route_receipt(intent, serialized, reconciliation)
            checks.append(
                CanaryCheck(
                    profile_alias=alias,
                    repeat=repeat,
                    intent=intent,
                    serialized=serialized,
                    reconciliation=reconciliation,
                    receipt=receipt,
                )
            )
    return SyntheticCanaryResult(repeats=repeats, checks=tuple(checks))


def run_five_repeat_synthetic_canary(**kwargs: Any) -> SyntheticCanaryResult:
    """Run the required 35-check deterministic regression mode."""

    return run_synthetic_canary(repeats=5, **kwargs)


@dataclass(frozen=True)
class StagingFile:
    """Digest of one generated TOML file relative to the managed staging root."""

    relative_path: str
    sha256: str

    def __post_init__(self) -> None:
        if _RELATIVE_STAGE_FILE.fullmatch(self.relative_path) is None:
            raise SemanticValidationError("staging file path is invalid")
        _require_hash(self.sha256, "staging file digest")

    def to_dict(self) -> dict[str, str]:
        return {"relative_path": self.relative_path, "sha256": self.sha256}


@dataclass(frozen=True)
class StagingManifest:
    """Manifest for deterministic project-local generated TOML only."""

    staging_version: int
    target: str
    registry_version: int
    registry_digest: str
    adapter_version: str
    normalization_rules_version: str
    files: tuple[StagingFile, ...]
    manifest_digest: str = field(default="")

    def __post_init__(self) -> None:
        if self.staging_version != STAGING_VERSION or isinstance(self.staging_version, bool):
            raise SemanticValidationError("staging manifest version is invalid")
        if self.target != DEFAULT_STAGING_RELATIVE_PATH.as_posix():
            raise SemanticValidationError("staging target is not managed project-local output")
        if self.registry_version != PROFILE_REGISTRY_VERSION or isinstance(self.registry_version, bool):
            raise SemanticValidationError("staging registry version is invalid")
        _require_hash(self.registry_digest, "staging registry digest")
        _require_text(self.adapter_version, "staging adapter version", pattern=_VERSION)
        if self.normalization_rules_version != NORMALIZATION_RULES_VERSION:
            raise SemanticValidationError("staging normalization version is invalid")
        if not self.files or len({item.relative_path for item in self.files}) != len(self.files):
            raise SemanticValidationError("staging manifest files are invalid")
        computed = sha256_hex(self._digest_input())
        if self.manifest_digest and self.manifest_digest != computed:
            raise SemanticValidationError("staging manifest digest does not match")
        object.__setattr__(self, "manifest_digest", computed)

    def _digest_input(self) -> dict[str, object]:
        return {
            "staging_version": self.staging_version,
            "target": self.target,
            "registry_version": self.registry_version,
            "registry_digest": self.registry_digest,
            "adapter_version": self.adapter_version,
            "normalization_rules_version": self.normalization_rules_version,
            "files": [item.to_dict() for item in self.files],
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._digest_input(), "manifest_digest": self.manifest_digest}

    @classmethod
    def from_value(cls, value: Mapping[str, Any]) -> "StagingManifest":
        fields = frozenset(
            (
                "staging_version",
                "target",
                "registry_version",
                "registry_digest",
                "adapter_version",
                "normalization_rules_version",
                "files",
                "manifest_digest",
            )
        )
        _check_exact_keys(value, fields, "staging manifest")
        raw_files = value.get("files")
        if not isinstance(raw_files, list):
            raise SemanticValidationError("staging manifest files are invalid")
        files: list[StagingFile] = []
        for raw_file in raw_files:
            if not isinstance(raw_file, Mapping):
                raise SemanticValidationError("staging manifest file is invalid")
            _check_exact_keys(raw_file, frozenset(("relative_path", "sha256")), "staging manifest file")
            files.append(StagingFile(**dict(raw_file)))
        try:
            return cls(
                staging_version=value["staging_version"],
                target=value["target"],
                registry_version=value["registry_version"],
                registry_digest=value["registry_digest"],
                adapter_version=value["adapter_version"],
                normalization_rules_version=value["normalization_rules_version"],
                files=tuple(files),
                manifest_digest=value["manifest_digest"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise SemanticValidationError("staging manifest is invalid") from error


def _staging_target(target: str | Path | None = None) -> Path:
    """Return only the one project-local output tree; no caller-selected path."""

    root = repository_root()
    expected = root / DEFAULT_STAGING_RELATIVE_PATH
    if target is None:
        candidate = expected
    else:
        raw = Path(target)
        if any(part == ".." for part in raw.parts):
            raise PathUnsafeError("staging target cannot traverse directories")
        candidate = raw if raw.is_absolute() else root / raw
    if candidate.absolute() != expected.absolute():
        raise PathUnsafeError("staging target is outside the managed project-local directory")
    _assert_safe_path(expected, root, allow_missing=True)
    return expected


def _assert_safe_path(path: Path, root: Path, *, allow_missing: bool) -> None:
    """Reject symlinks, special entries, and paths outside the repository tree."""

    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError as error:
        raise PathUnsafeError("staging path escapes repository") from error
    current = root
    for component in relative.parts:
        current = current / component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                return
            raise PathUnsafeError("managed staging path is missing") from None
        except OSError as error:
            raise PathUnsafeError("managed staging path cannot be inspected") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise PathUnsafeError("managed staging path cannot traverse symlinks")
        if current != path and not stat.S_ISDIR(metadata.st_mode):
            raise PathUnsafeError("managed staging parent is not a directory")
        if current == path and not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise PathUnsafeError("managed staging path has an unsafe type")


def _safe_tree_files(target: Path) -> dict[str, bytes]:
    """Read a managed tree without following symlinks or accepting hard links."""

    _assert_safe_path(target, repository_root(), allow_missing=False)
    if not target.is_dir():
        raise PathUnsafeError("managed staging target is not a directory")
    files: dict[str, bytes] = {}
    for current, directories, names in os.walk(target, topdown=True, followlinks=False):
        current_path = Path(current)
        for directory in tuple(directories):
            child = current_path / directory
            metadata = os.lstat(child)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise PathUnsafeError("managed staging contains an unsafe directory")
        for name in names:
            child = current_path / name
            try:
                metadata = os.lstat(child)
            except OSError as error:
                raise PathUnsafeError("managed staging file cannot be inspected") from error
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise PathUnsafeError("managed staging contains an unsafe file")
            relative = child.relative_to(target).as_posix()
            if relative != "manifest.json" and _RELATIVE_STAGE_FILE.fullmatch(relative) is None:
                raise PathUnsafeError("managed staging contains an unexpected file")
            files[relative] = child.read_bytes()
    return files


def _render_agent_toml(resolved: ResolvedModelProfile) -> bytes:
    lines = (
        f"profile_alias = {_toml_quote(resolved.alias)}",
        f"model = {_toml_quote(resolved.raw_model_slug)}",
        f"model_reasoning_effort = {_toml_quote(resolved.serialized_effort_value)}",
        f"routing_adapter = {_toml_quote(resolved.adapter_name)}",
        f"adapter_version = {_toml_quote(resolved.adapter_version)}",
        f"registry_version = {resolved.registry_version}",
        f"registry_digest = {_toml_quote(resolved.registry_digest)}",
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _render_root_toml(resolved: ResolvedModelProfile) -> bytes:
    lines = (
        f"default_root_profile = {_toml_quote(resolved.alias)}",
        f"model = {_toml_quote(resolved.raw_model_slug)}",
        f"model_reasoning_effort = {_toml_quote(resolved.serialized_effort_value)}",
        f"routing_adapter = {_toml_quote(resolved.adapter_name)}",
        f"adapter_version = {_toml_quote(resolved.adapter_version)}",
        f"registry_version = {resolved.registry_version}",
        f"registry_digest = {_toml_quote(resolved.registry_digest)}",
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


def render_staging(
    *, registry: Mapping[str, Any] | None = None
) -> tuple[StagingManifest, dict[str, bytes]]:
    """Render deterministic TOML and manifest bytes from exactly one registry."""

    source = load_profile_registry() if registry is None else dict(registry)
    validate_profile_registry(source)
    resolved_profiles = tuple(resolve_profile(alias, source) for alias in APPROVED_PROFILE_ALIASES)
    first = resolved_profiles[0]
    if any(
        resolved.adapter_version != first.adapter_version
        or resolved.normalization_rules_version != first.normalization_rules_version
        or resolved.registry_digest != first.registry_digest
        for resolved in resolved_profiles
    ):
        raise SemanticValidationError("registry does not have one pinned adapter snapshot")
    files: dict[str, bytes] = {
        "root.toml": _render_root_toml(resolve_profile("tera-max", source)),
    }
    for resolved in resolved_profiles:
        files[f"agents/{resolved.alias}.toml"] = _render_agent_toml(resolved)
    manifest = StagingManifest(
        staging_version=STAGING_VERSION,
        target=DEFAULT_STAGING_RELATIVE_PATH.as_posix(),
        registry_version=first.registry_version,
        registry_digest=first.registry_digest,
        adapter_version=first.adapter_version,
        normalization_rules_version=first.normalization_rules_version,
        files=tuple(
            StagingFile(relative_path=relative_path, sha256=sha256_bytes(content))
            for relative_path, content in sorted(files.items())
        ),
    )
    files["manifest.json"] = (
        json.dumps(manifest.to_dict(), ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return manifest, files


def _load_staging_shape(target: Path) -> tuple[StagingManifest, dict[str, bytes]]:
    files = _safe_tree_files(target)
    manifest_bytes = files.get("manifest.json")
    if manifest_bytes is None:
        raise SemanticValidationError("managed staging manifest is missing")
    try:
        raw_manifest = load_strict_json(target / "manifest.json")
    except Exception as error:
        raise SemanticValidationError("managed staging manifest is invalid") from error
    if not isinstance(raw_manifest, Mapping):
        raise SemanticValidationError("managed staging manifest is invalid")
    manifest = StagingManifest.from_value(raw_manifest)
    expected_paths = {"manifest.json", *(item.relative_path for item in manifest.files)}
    if set(files) != expected_paths:
        raise SemanticValidationError("managed staging has stale or foreign files")
    for item in manifest.files:
        if sha256_bytes(files[item.relative_path]) != item.sha256:
            raise SemanticValidationError("managed staging file digest drifted")
        try:
            parsed = tomllib.loads(files[item.relative_path].decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise SemanticValidationError("generated TOML is invalid") from error
        if not isinstance(parsed, dict):
            raise SemanticValidationError("generated TOML is invalid")
    return manifest, files


def validate_staging(
    *, registry: Mapping[str, Any] | None = None, target: str | Path | None = None
) -> StagingManifest:
    """Detect manual TOML drift, stale files, and registry/manifest disagreement."""

    managed_target = _staging_target(target)
    actual_manifest, actual_files = _load_staging_shape(managed_target)
    expected_manifest, expected_files = render_staging(registry=registry)
    if actual_manifest != expected_manifest or actual_files != expected_files:
        raise SemanticValidationError("generated M6 staging does not match the active registry")
    return actual_manifest


def _write_stage_tree(parent: Path, files: Mapping[str, bytes]) -> Path:
    stage = Path(tempfile.mkdtemp(prefix=".m6-stage-", dir=parent))
    try:
        for relative_path, content in files.items():
            relative = Path(relative_path)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise PathUnsafeError("generated staging file path is unsafe")
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        return stage
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _replace_managed_tree(target: Path, files: Mapping[str, bytes]) -> None:
    """Atomically replace a structurally-valid generated tree inside dist/global."""

    root = repository_root()
    _assert_safe_path(target.parent, root, allow_missing=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_path(target.parent, root, allow_missing=False)
    if target.exists():
        # Refuse to overwrite a user/foreign tree even though the destination is local.
        _load_staging_shape(target)
    stage = _write_stage_tree(target.parent, files)
    backup: Path | None = None
    try:
        if target.exists():
            backup = target.parent / f".m6-backup-{uuid.uuid4().hex}"
            os.replace(target, backup)
        os.replace(stage, target)
    except Exception:
        if backup is not None and backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    if backup is not None and backup.exists():
        _assert_safe_path(backup, root, allow_missing=False)
        shutil.rmtree(backup)


def stage_profile_toml(
    *, registry: Mapping[str, Any] | None = None, target: str | Path | None = None
) -> StagingManifest:
    """Write review-only custom-agent TOML fixtures under ``dist/global/m6``."""

    managed_target = _staging_target(target)
    manifest, files = render_staging(registry=registry)
    _replace_managed_tree(managed_target, files)
    return validate_staging(registry=registry, target=managed_target)


@dataclass(frozen=True)
class LastKnownGoodReceipt:
    """Pins a contained local staging snapshot; it never names a global path."""

    receipt_version: int
    target: str
    registry_version: int
    registry_digest: str
    adapter_version: str
    normalization_rules_version: str
    manifest_digest: str
    snapshot_digest: str
    files: tuple[StagingFile, ...]
    receipt_digest: str = field(default="")

    def __post_init__(self) -> None:
        if self.receipt_version != RECEIPT_VERSION or isinstance(self.receipt_version, bool):
            raise SemanticValidationError("last-known-good receipt version is invalid")
        if self.target != DEFAULT_STAGING_RELATIVE_PATH.as_posix():
            raise PathUnsafeError("last-known-good receipt target is unsafe")
        if self.registry_version != PROFILE_REGISTRY_VERSION or isinstance(self.registry_version, bool):
            raise SemanticValidationError("last-known-good registry version is invalid")
        _require_hash(self.registry_digest, "last-known-good registry digest")
        _require_text(self.adapter_version, "last-known-good adapter version", pattern=_VERSION)
        if self.normalization_rules_version != NORMALIZATION_RULES_VERSION:
            raise SemanticValidationError("last-known-good normalization version is invalid")
        _require_hash(self.manifest_digest, "last-known-good manifest digest")
        _require_hash(self.snapshot_digest, "last-known-good snapshot digest")
        if not self.files or len({item.relative_path for item in self.files}) != len(self.files):
            raise SemanticValidationError("last-known-good file manifest is invalid")
        computed = sha256_hex(self._digest_input())
        if self.receipt_digest and self.receipt_digest != computed:
            raise SemanticValidationError("last-known-good receipt digest does not match")
        object.__setattr__(self, "receipt_digest", computed)

    def _digest_input(self) -> dict[str, object]:
        return {
            "receipt_version": self.receipt_version,
            "target": self.target,
            "registry_version": self.registry_version,
            "registry_digest": self.registry_digest,
            "adapter_version": self.adapter_version,
            "normalization_rules_version": self.normalization_rules_version,
            "manifest_digest": self.manifest_digest,
            "snapshot_digest": self.snapshot_digest,
            "files": [item.to_dict() for item in self.files],
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._digest_input(), "receipt_digest": self.receipt_digest}


@dataclass(frozen=True)
class LocalStagingSnapshot:
    """In-memory local rollback material paired with a verifiable public receipt."""

    receipt: LastKnownGoodReceipt
    files: tuple[tuple[str, bytes], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, LastKnownGoodReceipt):
            raise SemanticValidationError("local staging snapshot receipt is invalid")
        file_map = dict(self.files)
        if len(file_map) != len(self.files):
            raise SemanticValidationError("local staging snapshot contains duplicate files")
        expected_paths = {"manifest.json", *(item.relative_path for item in self.receipt.files)}
        if set(file_map) != expected_paths or any(not isinstance(data, bytes) for data in file_map.values()):
            raise SemanticValidationError("local staging snapshot files are invalid")
        digest = _snapshot_digest(file_map)
        if digest != self.receipt.snapshot_digest:
            raise SemanticValidationError("local staging snapshot digest does not match")
        for item in self.receipt.files:
            if sha256_bytes(file_map[item.relative_path]) != item.sha256:
                raise SemanticValidationError("local staging snapshot file digest does not match")


def _snapshot_digest(files: Mapping[str, bytes]) -> str:
    return sha256_hex(
        {
            "files": [
                {"relative_path": path, "sha256": sha256_bytes(content)}
                for path, content in sorted(files.items())
            ]
        }
    )


def capture_last_known_good(
    *, registry: Mapping[str, Any] | None = None, target: str | Path | None = None
) -> LocalStagingSnapshot:
    """Capture a verified local staging snapshot for a later contained rollback."""

    managed_target = _staging_target(target)
    manifest = validate_staging(registry=registry, target=managed_target)
    _, files = _load_staging_shape(managed_target)
    snapshot_digest = _snapshot_digest(files)
    receipt = LastKnownGoodReceipt(
        receipt_version=RECEIPT_VERSION,
        target=DEFAULT_STAGING_RELATIVE_PATH.as_posix(),
        registry_version=manifest.registry_version,
        registry_digest=manifest.registry_digest,
        adapter_version=manifest.adapter_version,
        normalization_rules_version=manifest.normalization_rules_version,
        manifest_digest=manifest.manifest_digest,
        snapshot_digest=snapshot_digest,
        files=manifest.files,
    )
    return LocalStagingSnapshot(receipt=receipt, files=tuple(sorted(files.items())))


@dataclass(frozen=True)
class LocalRollbackPlan:
    """A one-target plan requiring the exact candidate manifest before restore."""

    snapshot: LocalStagingSnapshot
    expected_candidate_manifest_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, LocalStagingSnapshot):
            raise SemanticValidationError("rollback snapshot is invalid")
        _require_hash(self.expected_candidate_manifest_digest, "candidate manifest digest")


@dataclass(frozen=True)
class LocalRollbackResult:
    """Result of a rehearsal-only rollback after the post-restore local canary."""

    restored: bool
    manifest_digest: str
    canary: SyntheticCanaryResult


def prepare_local_rollback(
    snapshot: LocalStagingSnapshot,
    *,
    expected_candidate_manifest_digest: str,
) -> LocalRollbackPlan:
    """Bind a rollback to one candidate digest before any staged file is changed."""

    return LocalRollbackPlan(
        snapshot=snapshot,
        expected_candidate_manifest_digest=expected_candidate_manifest_digest,
    )


def rollback_staging(
    plan: LocalRollbackPlan,
    *,
    registry: Mapping[str, Any] | None = None,
    canary_runner: Callable[..., SyntheticCanaryResult] = run_synthetic_canary,
) -> LocalRollbackResult:
    """Restore only a digest-matched project-local staging snapshot and rehearse it."""

    if not isinstance(plan, LocalRollbackPlan):
        raise SemanticValidationError("local rollback plan is required")
    # Revalidate every immutable component before looking at the candidate tree.
    snapshot = LocalStagingSnapshot(receipt=plan.snapshot.receipt, files=plan.snapshot.files)
    managed_target = _staging_target(snapshot.receipt.target)
    candidate_manifest, _ = _load_staging_shape(managed_target)
    if candidate_manifest.manifest_digest != plan.expected_candidate_manifest_digest:
        raise SemanticValidationError("candidate staging digest does not match rollback plan")
    source = load_profile_registry() if registry is None else dict(registry)
    validate_profile_registry(source)
    if registry_digest(source) != snapshot.receipt.registry_digest:
        raise SemanticValidationError("active registry does not match last-known-good receipt")
    # A failed local canary leaves the candidate untouched rather than restoring blindly.
    preflight = canary_runner(registry=source)
    if not isinstance(preflight, SyntheticCanaryResult) or not preflight.passed:
        raise SemanticValidationError("last-known-good canary preflight failed")
    _replace_managed_tree(managed_target, dict(snapshot.files))
    restored_manifest = validate_staging(registry=source, target=managed_target)
    if restored_manifest.manifest_digest != snapshot.receipt.manifest_digest:
        raise SemanticValidationError("restored staging manifest does not match last-known-good receipt")
    canary = canary_runner(registry=source)
    if not isinstance(canary, SyntheticCanaryResult) or not canary.passed:
        raise SemanticValidationError("post-rollback synthetic canary failed")
    return LocalRollbackResult(
        restored=True,
        manifest_digest=restored_manifest.manifest_digest,
        canary=canary,
    )
