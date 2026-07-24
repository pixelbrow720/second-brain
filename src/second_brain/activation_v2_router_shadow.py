"""Synthetic, rule-first front-door routing shadow for Activation V2 A6.

The module classifies only bounded structured fixture intents. It never accepts
prompt text, creates a Codex session, serializes a provider request, or changes
a route default. Receipts are local synthetic evidence, not router attestation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
import time
from typing import Any, Iterable, Mapping
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
from .errors import ContractError, IntegrityError, SemanticValidationError
from .profiles import APPROVED_PROFILE_ALIASES, PROFILE_REGISTRY_VERSION, approved_effort_intent, registry_digest
from .profiles import load_profile_registry
from .schema_validation import parse_rfc3339_utc


ROUTE_SHADOW_CORPUS_VERSION = 1
ROUTE_SHADOW_RECEIPT_VERSION = 1
ROUTE_SHADOW_REPORT_VERSION = 1
ROUTE_SHADOW_CLASSIFIER_VERSION = "activation-v2-a6/1"
MAX_SHADOW_CASES = 16
MAX_SHADOW_LATENCY_MS = 1_000
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TASK_SHAPES = frozenset(
    (
        "read_explanation",
        "ambiguous_plan",
        "scoped_implementation",
        "difficult_debugging",
        "finite_utility",
        "unknown",
    )
)
_LANES = frozenset(("DIRECT", "ASSISTED", "GRAPH", "DEEP"))
_SELECTION_REASONS = frozenset(
    (
        "explicit_user_safe",
        "safety_override",
        "rule_read_explanation",
        "rule_scoped_implementation",
        "rule_difficult_debugging",
        "rule_finite_utility",
        "fallback_luna_gate",
        "fallback_uncertainty",
    )
)
_REASON_CODES = frozenset(
    (
        "EXPLICIT_PROFILE_PRESERVED",
        "HIGH_RISK_OR_AMBIGUOUS",
        "CROSS_PROJECT_SCOPE",
        "MULTI_STEP_SCOPE",
        "LUNA_HARD_GATE_FAILED",
        "UNKNOWN_STRUCTURED_SHAPE",
        "RULE_MATCH",
    )
)
_SHADOW_NAMESPACE = uuid.UUID("c31d704c-a2c1-5675-b8cc-e2f1d4da010e")


class RouteShadowError(SemanticValidationError):
    """A synthetic A6 route-shadow input or result violates its boundary."""


@dataclass(frozen=True)
class LunaHardGate:
    """Structured A6-only proof that a finite utility route is safe to select."""

    output_schema_finite: bool
    scope_bounded: bool
    non_sensitive: bool
    no_architecture_or_security_judgment: bool
    no_external_or_destructive_action: bool
    no_scope_expansion: bool
    deterministically_verifiable: bool

    def __post_init__(self) -> None:
        if type(self) is not LunaHardGate or any(type(value) is not bool for value in self.to_dict().values()):
            raise RouteShadowError("Luna hard gate is invalid")

    @classmethod
    def from_value(cls, value: object) -> "LunaHardGate":
        expected = {
            "output_schema_finite",
            "scope_bounded",
            "non_sensitive",
            "no_architecture_or_security_judgment",
            "no_external_or_destructive_action",
            "no_scope_expansion",
            "deterministically_verifiable",
        }
        if type(value) is not dict or set(value) != expected:
            raise RouteShadowError("Luna hard gate is invalid")
        try:
            return cls(**value)
        except (TypeError, RouteShadowError) as error:
            raise RouteShadowError("Luna hard gate is invalid") from error

    def passes(self) -> bool:
        return all(self.to_dict().values())

    def to_dict(self) -> dict[str, bool]:
        return {
            "output_schema_finite": self.output_schema_finite,
            "scope_bounded": self.scope_bounded,
            "non_sensitive": self.non_sensitive,
            "no_architecture_or_security_judgment": self.no_architecture_or_security_judgment,
            "no_external_or_destructive_action": self.no_external_or_destructive_action,
            "no_scope_expansion": self.no_scope_expansion,
            "deterministically_verifiable": self.deterministically_verifiable,
        }


@dataclass(frozen=True)
class StructuredRouteIntent:
    """A text-free intent that is safe to place in an A6 fixture corpus."""

    case_id: str
    task_shape: str
    admission_lane: str
    high_risk: bool
    ambiguous: bool
    cross_project: bool
    multi_step: bool
    explicit_profile_alias: str | None
    explicit_override_allowed: bool
    luna_hard_gate: LunaHardGate
    expected_profile_alias: str

    def __post_init__(self) -> None:
        if type(self) is not StructuredRouteIntent:
            raise RouteShadowError("structured route intent is invalid")
        _require_prefixed_uuid(self.case_id, "route-shadow-case", "route shadow case")
        if (
            not isinstance(self.task_shape, str)
            or self.task_shape not in _TASK_SHAPES
            or not isinstance(self.admission_lane, str)
            or self.admission_lane not in _LANES
        ):
            raise RouteShadowError("structured route intent is invalid")
        if any(
            type(value) is not bool
            for value in (
                self.high_risk,
                self.ambiguous,
                self.cross_project,
                self.multi_step,
                self.explicit_override_allowed,
            )
        ):
            raise RouteShadowError("structured route intent is invalid")
        if self.explicit_profile_alias is not None and (
            not isinstance(self.explicit_profile_alias, str)
            or self.explicit_profile_alias not in APPROVED_PROFILE_ALIASES
        ):
            raise RouteShadowError("explicit route profile is invalid")
        if self.explicit_profile_alias is None and self.explicit_override_allowed:
            raise RouteShadowError("explicit route override is invalid")
        if type(self.luna_hard_gate) is not LunaHardGate:
            raise RouteShadowError("Luna hard gate is invalid")
        if not isinstance(self.expected_profile_alias, str) or self.expected_profile_alias not in APPROVED_PROFILE_ALIASES:
            raise RouteShadowError("expected route profile is invalid")

    @classmethod
    def from_value(cls, value: object) -> "StructuredRouteIntent":
        if isinstance(value, cls):
            return value
        expected = {
            "case_id",
            "task_shape",
            "admission_lane",
            "high_risk",
            "ambiguous",
            "cross_project",
            "multi_step",
            "explicit_profile_alias",
            "explicit_override_allowed",
            "luna_hard_gate",
            "expected_profile_alias",
        }
        if type(value) is not dict or set(value) != expected:
            raise RouteShadowError("structured route intent has unsupported fields")
        validate_activation_v2_safe_content(value)
        try:
            return cls(
                case_id=value["case_id"],
                task_shape=value["task_shape"],
                admission_lane=value["admission_lane"],
                high_risk=value["high_risk"],
                ambiguous=value["ambiguous"],
                cross_project=value["cross_project"],
                multi_step=value["multi_step"],
                explicit_profile_alias=value["explicit_profile_alias"],
                explicit_override_allowed=value["explicit_override_allowed"],
                luna_hard_gate=LunaHardGate.from_value(value["luna_hard_gate"]),
                expected_profile_alias=value["expected_profile_alias"],
            )
        except (TypeError, RouteShadowError) as error:
            raise RouteShadowError("structured route intent is invalid") from error

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "task_shape": self.task_shape,
            "admission_lane": self.admission_lane,
            "high_risk": self.high_risk,
            "ambiguous": self.ambiguous,
            "cross_project": self.cross_project,
            "multi_step": self.multi_step,
            "explicit_profile_alias": self.explicit_profile_alias,
            "explicit_override_allowed": self.explicit_override_allowed,
            "luna_hard_gate": self.luna_hard_gate.to_dict(),
            "expected_profile_alias": self.expected_profile_alias,
        }


@dataclass(frozen=True)
class RouteShadowCorpus:
    """A digest-bound, public synthetic structured routing corpus."""

    schema_version: int
    corpus_id: str
    data_class: str
    evaluated_at: str
    latency_budget_ms: int
    intents: tuple[StructuredRouteIntent, ...]
    corpus_digest: str = field(default="")

    def __post_init__(self) -> None:
        if (
            type(self) is not RouteShadowCorpus
            or type(self.schema_version) is not int
            or self.schema_version != ROUTE_SHADOW_CORPUS_VERSION
        ):
            raise RouteShadowError("route shadow corpus version is invalid")
        _require_prefixed_uuid(self.corpus_id, "route-shadow-corpus", "route shadow corpus")
        if self.data_class != "PUBLIC_SYNTHETIC":
            raise RouteShadowError("route shadow corpus is not public synthetic data")
        _require_timestamp(self.evaluated_at, "route shadow corpus timestamp")
        if type(self.latency_budget_ms) is not int or not 1 <= self.latency_budget_ms <= MAX_SHADOW_LATENCY_MS:
            raise RouteShadowError("route shadow latency budget is invalid")
        if (
            not isinstance(self.intents, tuple)
            or not 1 <= len(self.intents) <= MAX_SHADOW_CASES
            or any(type(intent) is not StructuredRouteIntent for intent in self.intents)
            or len({intent.case_id for intent in self.intents}) != len(self.intents)
        ):
            raise RouteShadowError("route shadow corpus intents are invalid")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "corpus_digest")
        if self.corpus_digest and self.corpus_digest != expected:
            raise RouteShadowError("route shadow corpus digest does not match")
        object.__setattr__(self, "corpus_digest", expected)

    @classmethod
    def from_value(cls, value: object) -> "RouteShadowCorpus":
        expected = {
            "schema_version",
            "corpus_id",
            "data_class",
            "evaluated_at",
            "latency_budget_ms",
            "intents",
            "corpus_digest",
        }
        if type(value) is not dict or set(value) != expected or not isinstance(value["intents"], list):
            raise RouteShadowError("route shadow corpus has unsupported fields")
        validate_activation_v2_safe_content(value)
        try:
            return cls(
                schema_version=value["schema_version"],
                corpus_id=value["corpus_id"],
                data_class=value["data_class"],
                evaluated_at=value["evaluated_at"],
                latency_budget_ms=value["latency_budget_ms"],
                intents=tuple(StructuredRouteIntent.from_value(item) for item in value["intents"]),
                corpus_digest=value["corpus_digest"],
            )
        except (TypeError, RouteShadowError) as error:
            raise RouteShadowError("route shadow corpus is invalid") from error

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "corpus_id": self.corpus_id,
            "data_class": self.data_class,
            "evaluated_at": self.evaluated_at,
            "latency_budget_ms": self.latency_budget_ms,
            "intents": [intent.to_dict() for intent in self.intents],
        }
        if include_digest:
            value["corpus_digest"] = self.corpus_digest
        return value


@dataclass(frozen=True)
class RouteShadowDecision:
    """The pure rule output; it has no route, session, or provider action."""

    profile_alias: str
    selection_reason: str
    reason_codes: tuple[str, ...]
    fallback_used: bool

    def __post_init__(self) -> None:
        if (
            type(self) is not RouteShadowDecision
            or not isinstance(self.profile_alias, str)
            or self.profile_alias not in APPROVED_PROFILE_ALIASES
            or not isinstance(self.selection_reason, str)
            or self.selection_reason not in _SELECTION_REASONS
            or type(self.fallback_used) is not bool
            or not isinstance(self.reason_codes, tuple)
            or not self.reason_codes
            or len(set(self.reason_codes)) != len(self.reason_codes)
            or any(code not in _REASON_CODES for code in self.reason_codes)
        ):
            raise RouteShadowError("route shadow decision is invalid")


@dataclass(frozen=True)
class RouteShadowReceipt:
    """A redacted selection result with explicit no-session/no-network flags."""

    schema_version: int
    receipt_id: str
    case_id: str
    classifier_version: str
    registry_version: int
    registry_digest: str
    input_digest: str
    expected_profile_alias: str
    selected_profile_alias: str
    effort_intent: str
    selection_reason: str
    reason_codes: tuple[str, ...]
    match_status: str
    fallback_used: bool
    latency_within_budget: bool
    model_classifier_calls: int
    provider_network_calls: int
    session_created: bool
    default_changed: bool
    prompt_persisted: bool
    fixture_only: bool
    authority_write: bool
    global_write: bool
    created_at: str
    receipt_digest: str = field(default="")

    def __post_init__(self) -> None:
        if (
            type(self) is not RouteShadowReceipt
            or type(self.schema_version) is not int
            or self.schema_version != ROUTE_SHADOW_RECEIPT_VERSION
        ):
            raise RouteShadowError("route shadow receipt version is invalid")
        _require_prefixed_uuid(self.receipt_id, "route-shadow-receipt", "route shadow receipt")
        _require_prefixed_uuid(self.case_id, "route-shadow-case", "route shadow case")
        if self.classifier_version != ROUTE_SHADOW_CLASSIFIER_VERSION:
            raise RouteShadowError("route shadow classifier version is invalid")
        if self.registry_version != PROFILE_REGISTRY_VERSION or isinstance(self.registry_version, bool):
            raise RouteShadowError("route shadow registry version is invalid")
        _require_hash(self.registry_digest, "route shadow registry digest")
        _require_hash(self.input_digest, "route shadow input digest")
        if (
            not isinstance(self.expected_profile_alias, str)
            or self.expected_profile_alias not in APPROVED_PROFILE_ALIASES
            or not isinstance(self.selected_profile_alias, str)
            or self.selected_profile_alias not in APPROVED_PROFILE_ALIASES
            or not isinstance(self.effort_intent, str)
            or self.effort_intent != approved_effort_intent(self.selected_profile_alias)
            or not isinstance(self.selection_reason, str)
            or self.selection_reason not in _SELECTION_REASONS
            or not isinstance(self.match_status, str)
            or self.match_status not in {"MATCH", "MISMATCH_PROFILE"}
            or type(self.fallback_used) is not bool
            or type(self.latency_within_budget) is not bool
        ):
            raise RouteShadowError("route shadow selection is invalid")
        if not isinstance(self.reason_codes, tuple) or not self.reason_codes or len(set(self.reason_codes)) != len(self.reason_codes):
            raise RouteShadowError("route shadow reason codes are invalid")
        if any(not isinstance(code, str) or code not in _REASON_CODES for code in self.reason_codes):
            raise RouteShadowError("route shadow reason codes are invalid")
        if (self.match_status == "MATCH") != (self.expected_profile_alias == self.selected_profile_alias):
            raise RouteShadowError("route shadow match status is invalid")
        if (
            type(self.model_classifier_calls) is not int
            or self.model_classifier_calls != 0
            or type(self.provider_network_calls) is not int
            or self.provider_network_calls != 0
            or self.session_created is not False
            or self.default_changed is not False
            or self.prompt_persisted is not False
            or self.fixture_only is not True
            or self.authority_write is not False
            or self.global_write is not False
        ):
            raise RouteShadowError("route shadow receipt exceeds its local-only boundary")
        _require_timestamp(self.created_at, "route shadow receipt timestamp")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "receipt_digest")
        if self.receipt_digest and self.receipt_digest != expected:
            raise RouteShadowError("route shadow receipt digest does not match")
        object.__setattr__(self, "receipt_digest", expected)

    @classmethod
    def from_value(cls, value: object) -> "RouteShadowReceipt":
        expected = {
            "schema_version",
            "receipt_id",
            "case_id",
            "classifier_version",
            "registry_version",
            "registry_digest",
            "input_digest",
            "expected_profile_alias",
            "selected_profile_alias",
            "effort_intent",
            "selection_reason",
            "reason_codes",
            "match_status",
            "fallback_used",
            "latency_within_budget",
            "model_classifier_calls",
            "provider_network_calls",
            "session_created",
            "default_changed",
            "prompt_persisted",
            "fixture_only",
            "authority_write",
            "global_write",
            "created_at",
            "receipt_digest",
        }
        if type(value) is not dict or set(value) != expected or not isinstance(value["reason_codes"], list):
            raise RouteShadowError("route shadow receipt has unsupported fields")
        try:
            return cls(
                schema_version=value["schema_version"],
                receipt_id=value["receipt_id"],
                case_id=value["case_id"],
                classifier_version=value["classifier_version"],
                registry_version=value["registry_version"],
                registry_digest=value["registry_digest"],
                input_digest=value["input_digest"],
                expected_profile_alias=value["expected_profile_alias"],
                selected_profile_alias=value["selected_profile_alias"],
                effort_intent=value["effort_intent"],
                selection_reason=value["selection_reason"],
                reason_codes=tuple(value["reason_codes"]),
                match_status=value["match_status"],
                fallback_used=value["fallback_used"],
                latency_within_budget=value["latency_within_budget"],
                model_classifier_calls=value["model_classifier_calls"],
                provider_network_calls=value["provider_network_calls"],
                session_created=value["session_created"],
                default_changed=value["default_changed"],
                prompt_persisted=value["prompt_persisted"],
                fixture_only=value["fixture_only"],
                authority_write=value["authority_write"],
                global_write=value["global_write"],
                created_at=value["created_at"],
                receipt_digest=value["receipt_digest"],
            )
        except (TypeError, RouteShadowError) as error:
            raise RouteShadowError("route shadow receipt is invalid") from error

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "case_id": self.case_id,
            "classifier_version": self.classifier_version,
            "registry_version": self.registry_version,
            "registry_digest": self.registry_digest,
            "input_digest": self.input_digest,
            "expected_profile_alias": self.expected_profile_alias,
            "selected_profile_alias": self.selected_profile_alias,
            "effort_intent": self.effort_intent,
            "selection_reason": self.selection_reason,
            "reason_codes": list(self.reason_codes),
            "match_status": self.match_status,
            "fallback_used": self.fallback_used,
            "latency_within_budget": self.latency_within_budget,
            "model_classifier_calls": self.model_classifier_calls,
            "provider_network_calls": self.provider_network_calls,
            "session_created": self.session_created,
            "default_changed": self.default_changed,
            "prompt_persisted": self.prompt_persisted,
            "fixture_only": self.fixture_only,
            "authority_write": self.authority_write,
            "global_write": self.global_write,
            "created_at": self.created_at,
        }
        if include_digest:
            value["receipt_digest"] = self.receipt_digest
        return value


@dataclass(frozen=True)
class RouteShadowReceiptBinding:
    """A report binds opaque receipt identifiers and digests, not task bodies."""

    receipt_id: str
    receipt_digest: str

    def __post_init__(self) -> None:
        if type(self) is not RouteShadowReceiptBinding:
            raise RouteShadowError("route shadow receipt binding is invalid")
        _require_prefixed_uuid(self.receipt_id, "route-shadow-receipt", "route shadow receipt")
        _require_hash(self.receipt_digest, "route shadow receipt digest")

    def to_dict(self) -> dict[str, str]:
        return {"receipt_id": self.receipt_id, "receipt_digest": self.receipt_digest}

    @classmethod
    def from_value(cls, value: object) -> "RouteShadowReceiptBinding":
        expected = {"receipt_id", "receipt_digest"}
        if type(value) is not dict or set(value) != expected:
            raise RouteShadowError("route shadow receipt binding is invalid")
        try:
            return cls(**value)
        except (TypeError, RouteShadowError) as error:
            raise RouteShadowError("route shadow receipt binding is invalid") from error


@dataclass(frozen=True)
class RouteShadowReport:
    """A passing corpus summary that proves no model/session/provider action."""

    schema_version: int
    report_id: str
    corpus_id: str
    corpus_digest: str
    case_count: int
    matched_count: int
    fallback_count: int
    max_observed_latency_ms: int
    latency_budget_ms: int
    receipt_bindings: tuple[RouteShadowReceiptBinding, ...]
    model_classifier_calls: int
    provider_network_calls: int
    sessions_created: int
    defaults_changed: int
    fixture_only: bool
    authority_write: bool
    global_write: bool
    status: str
    created_at: str
    report_digest: str = field(default="")

    def __post_init__(self) -> None:
        if (
            type(self) is not RouteShadowReport
            or type(self.schema_version) is not int
            or self.schema_version != ROUTE_SHADOW_REPORT_VERSION
        ):
            raise RouteShadowError("route shadow report version is invalid")
        _require_prefixed_uuid(self.report_id, "route-shadow-report", "route shadow report")
        _require_prefixed_uuid(self.corpus_id, "route-shadow-corpus", "route shadow corpus")
        _require_hash(self.corpus_digest, "route shadow corpus digest")
        if (
            type(self.case_count) is not int
            or not 1 <= self.case_count <= MAX_SHADOW_CASES
            or type(self.matched_count) is not int
            or self.matched_count != self.case_count
            or type(self.fallback_count) is not int
            or not 0 <= self.fallback_count <= self.case_count
            or type(self.max_observed_latency_ms) is not int
            or not 0 <= self.max_observed_latency_ms <= MAX_SHADOW_LATENCY_MS
            or type(self.latency_budget_ms) is not int
            or not 1 <= self.latency_budget_ms <= MAX_SHADOW_LATENCY_MS
            or self.max_observed_latency_ms > self.latency_budget_ms
        ):
            raise RouteShadowError("route shadow report counts are invalid")
        if (
            not isinstance(self.receipt_bindings, tuple)
            or len(self.receipt_bindings) != self.case_count
            or any(type(item) is not RouteShadowReceiptBinding for item in self.receipt_bindings)
            or len({item.receipt_id for item in self.receipt_bindings}) != self.case_count
        ):
            raise RouteShadowError("route shadow report receipt bindings are invalid")
        if (
            type(self.model_classifier_calls) is not int
            or self.model_classifier_calls != 0
            or type(self.provider_network_calls) is not int
            or self.provider_network_calls != 0
            or type(self.sessions_created) is not int
            or self.sessions_created != 0
            or type(self.defaults_changed) is not int
            or self.defaults_changed != 0
            or self.fixture_only is not True
            or self.authority_write is not False
            or self.global_write is not False
            or self.status != "PASS"
        ):
            raise RouteShadowError("route shadow report exceeds its local-only boundary")
        _require_timestamp(self.created_at, "route shadow report timestamp")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "report_digest")
        if self.report_digest and self.report_digest != expected:
            raise RouteShadowError("route shadow report digest does not match")
        object.__setattr__(self, "report_digest", expected)

    @classmethod
    def from_value(cls, value: object) -> "RouteShadowReport":
        expected = {
            "schema_version",
            "report_id",
            "corpus_id",
            "corpus_digest",
            "case_count",
            "matched_count",
            "fallback_count",
            "max_observed_latency_ms",
            "latency_budget_ms",
            "receipt_bindings",
            "model_classifier_calls",
            "provider_network_calls",
            "sessions_created",
            "defaults_changed",
            "fixture_only",
            "authority_write",
            "global_write",
            "status",
            "created_at",
            "report_digest",
        }
        if type(value) is not dict or set(value) != expected or not isinstance(value["receipt_bindings"], list):
            raise RouteShadowError("route shadow report has unsupported fields")
        try:
            return cls(
                schema_version=value["schema_version"],
                report_id=value["report_id"],
                corpus_id=value["corpus_id"],
                corpus_digest=value["corpus_digest"],
                case_count=value["case_count"],
                matched_count=value["matched_count"],
                fallback_count=value["fallback_count"],
                max_observed_latency_ms=value["max_observed_latency_ms"],
                latency_budget_ms=value["latency_budget_ms"],
                receipt_bindings=tuple(
                    RouteShadowReceiptBinding.from_value(item) for item in value["receipt_bindings"]
                ),
                model_classifier_calls=value["model_classifier_calls"],
                provider_network_calls=value["provider_network_calls"],
                sessions_created=value["sessions_created"],
                defaults_changed=value["defaults_changed"],
                fixture_only=value["fixture_only"],
                authority_write=value["authority_write"],
                global_write=value["global_write"],
                status=value["status"],
                created_at=value["created_at"],
                report_digest=value["report_digest"],
            )
        except (TypeError, RouteShadowError) as error:
            raise RouteShadowError("route shadow report is invalid") from error

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "report_id": self.report_id,
            "corpus_id": self.corpus_id,
            "corpus_digest": self.corpus_digest,
            "case_count": self.case_count,
            "matched_count": self.matched_count,
            "fallback_count": self.fallback_count,
            "max_observed_latency_ms": self.max_observed_latency_ms,
            "latency_budget_ms": self.latency_budget_ms,
            "receipt_bindings": [item.to_dict() for item in self.receipt_bindings],
            "model_classifier_calls": self.model_classifier_calls,
            "provider_network_calls": self.provider_network_calls,
            "sessions_created": self.sessions_created,
            "defaults_changed": self.defaults_changed,
            "fixture_only": self.fixture_only,
            "authority_write": self.authority_write,
            "global_write": self.global_write,
            "status": self.status,
            "created_at": self.created_at,
        }
        if include_digest:
            value["report_digest"] = self.report_digest
        return value


def select_shadow_route(intent: StructuredRouteIntent | Mapping[str, Any]) -> RouteShadowDecision:
    """Classify a bounded intent without a prompt, model call, or side effect."""

    selected = StructuredRouteIntent.from_value(intent)
    safety_risk = selected.high_risk or selected.ambiguous or selected.cross_project or selected.multi_step
    if selected.explicit_profile_alias is not None and selected.explicit_override_allowed:
        if not safety_risk and _explicit_profile_is_safe(selected):
            return RouteShadowDecision(
                selected.explicit_profile_alias,
                "explicit_user_safe",
                ("EXPLICIT_PROFILE_PRESERVED",),
                False,
            )
        return RouteShadowDecision(
            "tera-max",
            "safety_override",
            _safety_reason_codes(selected),
            True,
        )
    if safety_risk:
        return RouteShadowDecision("tera-max", "safety_override", _safety_reason_codes(selected), True)
    if selected.task_shape == "read_explanation":
        return RouteShadowDecision("tera-high", "rule_read_explanation", ("RULE_MATCH",), False)
    if selected.task_shape == "scoped_implementation":
        return RouteShadowDecision("sol-xhigh", "rule_scoped_implementation", ("RULE_MATCH",), False)
    if selected.task_shape == "difficult_debugging":
        return RouteShadowDecision("sol-max", "rule_difficult_debugging", ("RULE_MATCH",), False)
    if selected.task_shape == "finite_utility":
        if selected.luna_hard_gate.passes():
            return RouteShadowDecision("luna-xhigh", "rule_finite_utility", ("RULE_MATCH",), False)
        return RouteShadowDecision("tera-max", "fallback_luna_gate", ("LUNA_HARD_GATE_FAILED",), True)
    return RouteShadowDecision("tera-max", "fallback_uncertainty", ("UNKNOWN_STRUCTURED_SHAPE",), True)


def evaluate_route_shadow_corpus(
    runtime: DisposableRuntime,
    corpus: Mapping[str, Any],
    *,
    report_id: str | None = None,
) -> RouteShadowReport:
    """Evaluate a public synthetic corpus and persist redacted shadow evidence."""

    handle = _require_fixture_shadow_runtime(runtime)
    if type(corpus) is not dict:
        raise RouteShadowError("route shadow corpus is invalid")
    validate_named_document("activation-v2-route-shadow-corpus-v1", corpus)
    selected_corpus = RouteShadowCorpus.from_value(corpus)
    registry = load_profile_registry()
    snapshot_digest = registry_digest(registry)
    results: list[tuple[RouteShadowReceipt, int]] = []
    for intent in selected_corpus.intents:
        started = time.monotonic()
        decision = select_shadow_route(intent)
        elapsed_ms = math.ceil((time.monotonic() - started) * 1_000)
        if elapsed_ms < 0 or elapsed_ms > selected_corpus.latency_budget_ms:
            raise RouteShadowError("route shadow classification exceeds its latency budget")
        receipt = _receipt_for_intent(selected_corpus, intent, decision, snapshot_digest)
        results.append((receipt, elapsed_ms))

    _write_or_verify_receipts(handle, (receipt for receipt, _ in results))
    if any(receipt.match_status != "MATCH" for receipt, _ in results):
        raise RouteShadowError("route shadow corpus contains a profile mismatch")
    report = RouteShadowReport(
        schema_version=ROUTE_SHADOW_REPORT_VERSION,
        report_id=report_id or f"route-shadow-report:{uuid.uuid4()}",
        corpus_id=selected_corpus.corpus_id,
        corpus_digest=selected_corpus.corpus_digest,
        case_count=len(results),
        matched_count=len(results),
        fallback_count=sum(receipt.fallback_used for receipt, _ in results),
        max_observed_latency_ms=max(elapsed for _, elapsed in results),
        latency_budget_ms=selected_corpus.latency_budget_ms,
        receipt_bindings=tuple(
            RouteShadowReceiptBinding(receipt.receipt_id, receipt.receipt_digest) for receipt, _ in results
        ),
        model_classifier_calls=0,
        provider_network_calls=0,
        sessions_created=0,
        defaults_changed=0,
        fixture_only=True,
        authority_write=False,
        global_write=False,
        status="PASS",
        created_at=selected_corpus.evaluated_at,
    )
    validate_named_document("activation-v2-route-shadow-report-v1", report.to_dict())
    relative = _report_path(report.report_id)
    if disposable_json_exists(handle, relative):
        raise RouteShadowError("route shadow report identity already exists")
    write_disposable_json(handle, relative, report.to_dict())
    return report


def load_route_shadow_receipt(runtime: DisposableRuntime, receipt_id: str) -> RouteShadowReceipt:
    """Load one validated shadow receipt without opening a router or a store."""

    handle = _require_fixture_shadow_runtime(runtime)
    _require_prefixed_uuid(receipt_id, "route-shadow-receipt", "route shadow receipt")
    try:
        document = read_disposable_json(handle, _receipt_path(receipt_id))
        validate_named_document("activation-v2-route-shadow-receipt-v1", document)
        return RouteShadowReceipt.from_value(document)
    except (IntegrityError, ContractError) as error:
        raise IntegrityError("route shadow receipt is unavailable or invalid") from error


def load_route_shadow_report(runtime: DisposableRuntime, report_id: str) -> RouteShadowReport:
    """Load one validated passing synthetic shadow report."""

    handle = _require_fixture_shadow_runtime(runtime)
    _require_prefixed_uuid(report_id, "route-shadow-report", "route shadow report")
    try:
        document = read_disposable_json(handle, _report_path(report_id))
        validate_named_document("activation-v2-route-shadow-report-v1", document)
        return RouteShadowReport.from_value(document)
    except (IntegrityError, ContractError) as error:
        raise IntegrityError("route shadow report is unavailable or invalid") from error


def _require_fixture_shadow_runtime(runtime: DisposableRuntime) -> DisposableRuntime:
    handle = load_disposable_runtime(runtime.root)
    handle.manifest.policy.require_resolved("A6")
    if not handle.manifest.policy.fixture_only or handle.manifest.policy.router_entry_point != "CLI_SHADOW":
        raise RouteShadowError("A6 requires a fixture-only CLI shadow policy")
    return handle


def _explicit_profile_is_safe(intent: StructuredRouteIntent) -> bool:
    if intent.explicit_profile_alias != "luna-xhigh":
        return True
    return intent.task_shape == "finite_utility" and intent.luna_hard_gate.passes()


def _safety_reason_codes(intent: StructuredRouteIntent) -> tuple[str, ...]:
    reasons: list[str] = []
    if intent.high_risk or intent.ambiguous:
        reasons.append("HIGH_RISK_OR_AMBIGUOUS")
    if intent.cross_project:
        reasons.append("CROSS_PROJECT_SCOPE")
    if intent.multi_step:
        reasons.append("MULTI_STEP_SCOPE")
    if intent.explicit_profile_alias == "luna-xhigh" and not intent.luna_hard_gate.passes():
        reasons.append("LUNA_HARD_GATE_FAILED")
    return tuple(reasons or ("HIGH_RISK_OR_AMBIGUOUS",))


def _receipt_for_intent(
    corpus: RouteShadowCorpus,
    intent: StructuredRouteIntent,
    decision: RouteShadowDecision,
    snapshot_digest: str,
) -> RouteShadowReceipt:
    return RouteShadowReceipt(
        schema_version=ROUTE_SHADOW_RECEIPT_VERSION,
        receipt_id=_derived_receipt_id(corpus.corpus_digest, intent.case_id),
        case_id=intent.case_id,
        classifier_version=ROUTE_SHADOW_CLASSIFIER_VERSION,
        registry_version=PROFILE_REGISTRY_VERSION,
        registry_digest=snapshot_digest,
        input_digest=sha256_hex(intent.to_dict()),
        expected_profile_alias=intent.expected_profile_alias,
        selected_profile_alias=decision.profile_alias,
        effort_intent=approved_effort_intent(decision.profile_alias),
        selection_reason=decision.selection_reason,
        reason_codes=decision.reason_codes,
        match_status="MATCH" if decision.profile_alias == intent.expected_profile_alias else "MISMATCH_PROFILE",
        fallback_used=decision.fallback_used,
        latency_within_budget=True,
        model_classifier_calls=0,
        provider_network_calls=0,
        session_created=False,
        default_changed=False,
        prompt_persisted=False,
        fixture_only=True,
        authority_write=False,
        global_write=False,
        created_at=corpus.evaluated_at,
    )


def _write_or_verify_receipts(runtime: DisposableRuntime, receipts: Iterable[RouteShadowReceipt]) -> None:
    for receipt in receipts:
        if type(receipt) is not RouteShadowReceipt:
            raise RouteShadowError("route shadow receipt is invalid")
        validate_named_document("activation-v2-route-shadow-receipt-v1", receipt.to_dict())
        relative = _receipt_path(receipt.receipt_id)
        if disposable_json_exists(runtime, relative):
            try:
                existing = RouteShadowReceipt.from_value(read_disposable_json(runtime, relative))
            except (IntegrityError, RouteShadowError) as error:
                raise IntegrityError("route shadow receipt is unavailable or invalid") from error
            if existing.to_dict() != receipt.to_dict():
                raise IntegrityError("route shadow receipt identity is bound to another input")
            continue
        write_disposable_json(runtime, relative, receipt.to_dict())


def _derived_receipt_id(corpus_digest: str, case_id: str) -> str:
    name = sha256_hex({"corpus_digest": corpus_digest, "case_id": case_id})
    return f"route-shadow-receipt:{uuid.uuid5(_SHADOW_NAMESPACE, name)}"


def _receipt_path(receipt_id: str) -> str:
    return f"receipts/route-shadow/{receipt_id.removeprefix('route-shadow-receipt:')}.json"


def _report_path(report_id: str) -> str:
    return f"receipts/route-shadow-reports/{report_id.removeprefix('route-shadow-report:')}.json"


def _require_prefixed_uuid(value: object, prefix: str, label: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix + ":"):
        raise RouteShadowError(f"{label} is invalid")
    if _UUID.fullmatch(value.removeprefix(prefix + ":")) is None:
        raise RouteShadowError(f"{label} is invalid")
    return value


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise RouteShadowError(f"{label} is invalid")
    return value


def _require_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise RouteShadowError(f"{label} is invalid")
    try:
        parse_rfc3339_utc(value)
    except Exception as error:
        raise RouteShadowError(f"{label} is invalid") from error
    return value
