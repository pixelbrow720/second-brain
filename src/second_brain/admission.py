"""Deterministic local admission, capability, permission, and audit contracts.

M5 models policy only. It never discovers installed capabilities, reads global
configuration, executes a helper, installs a package, opens a connector, or
mutates an authority store. Inputs are normalized data; untrusted descriptions
are intentionally excluded from all routing and audit decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
import re
from threading import RLock
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence
import uuid

from .canonical import sha256_hex
from .errors import StorageError


ADMISSION_VERSION = "second-brain-admission/5.0.0"

_UUID_TEXT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_CAPABILITY_ID = re.compile(r"^cap:[a-z0-9][a-z0-9._-]{0,63}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_CONTRACT_ID = re.compile(r"^[a-z][a-z0-9._/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FRESHNESS = {"none", "targeted", "broad"}
_SAFE_TRUST = {"builtin", "repository_trusted", "approved_pinned"}
_COMPATIBLE_LICENSES = {
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "CC0-1.0",
    "ISC",
    "MIT",
    "MPL-2.0",
}
_IMMUTABLE_REFERENCE = re.compile(
    r"^(?:v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?|[0-9a-f]{40,64}|sha256:[0-9a-f]{64})$"
)
_REASON_CODES = {
    "ACTION_PERMISSION_MISMATCH",
    "SELF_CONTAINED_LOW_RISK",
    "HIGH_RISK",
    "USER_LOWER_LANE_NOT_APPLIED",
    "USER_EXPLICIT_DEEP",
    "INDEPENDENT_BRANCHES",
    "USER_EXPLICIT_GRAPH",
    "BOUNDED_TOOL_OR_EVIDENCE_NEED",
    "ESCALATED_NEW_RISK",
    "PARENT_PERMISSION_DENIED",
    "NODE_PERMISSION_EXPANSION",
    "CAPABILITY_PERMISSION_DENIED",
    "CAPABILITY_DESCRIPTOR_REQUIRED",
    "CAPABILITY_DESCRIPTOR_MISMATCH",
    "ADMISSION_DECISION_REQUIRED",
    "ADMISSION_BINDING_MISMATCH",
    "TASK_NOT_ACTIVE",
    "CAPABILITY_SELECTED",
    "LUNA_PERMISSION_DENIED",
    "DEEP_REQUIRED",
    "DIRECT_CAPABILITY_FORBIDDEN",
    "USER_APPROVAL_REQUIRED",
    "APPROVAL_BINDING_MISMATCH",
    "CAPABILITY_REQUIRES_APPROVAL",
    "SUPPLY_CHAIN_NOT_APPROVED",
    "AUDIT_FAILED",
    "NOT_INSTALLED",
    "HEALTH_UNAVAILABLE",
    "HEALTH_DEGRADED",
    "HEALTH_QUARANTINED",
    "HEALTH_DISABLED",
    "TRUST_NOT_PERMITTED",
    "PERMISSION_NOT_PERMITTED",
    "REQUIRED_CAPABILITY_MISSING",
    "OPTIONAL_CAPABILITY_UNAVAILABLE",
    "LOWER_RANK",
    "TRIGGER_COLLISION",
    "USER_FORBID_RESEARCH",
    "USER_FORBID_CAPABILITIES",
    "GATEWAY_NOT_REQUESTED",
    "GATEWAY_CONTRACT_MISMATCH",
    "LICENSE_MISSING",
    "LICENSE_INCOMPATIBLE",
    "IMMUTABLE_PIN_REQUIRED",
    "IDENTITY_REVIEW_REQUIRED",
    "MAINTENANCE_REVIEW_REQUIRED",
    "EXECUTABLE_REVIEW_REQUIRED",
    "NETWORK_REVIEW_REQUIRED",
    "PERMISSION_REVIEW_REQUIRED",
    "SECURITY_REVIEW_REQUIRED",
    "UTILITY_EVIDENCE_REQUIRED",
    "ROLLBACK_REVIEW_REQUIRED",
    "REVIEW_REQUIRED_FOR_UPDATE",
    "SCOPE_NOT_PERMITTED",
    "SUPPLY_CHAIN_APPROVED",
    "TRIGGER_COLLISION_LOWER_RANK",
}
_AUDIT_EVENT_TYPES = {
    "admission_decided",
    "admission_escalated",
    "capability_selected",
    "capability_blocked",
    "permission_requested",
    "permission_granted",
    "permission_denied",
    "supply_chain_reviewed",
    "capability_missing",
}
_AUDIT_STATUSES = {
    "decided",
    "selected",
    "blocked",
    "requested",
    "granted",
    "denied",
    "approval_required",
    "reviewed",
    "failed",
    "missing",
}
_AUDIT_EVENT_STATUSES = {
    "admission_decided": {"decided"},
    "admission_escalated": {"decided"},
    "capability_selected": {"selected"},
    "capability_blocked": {"blocked"},
    "permission_requested": {"approval_required"},
    "permission_granted": {"granted"},
    "permission_denied": {"denied"},
    "supply_chain_reviewed": {"reviewed", "failed"},
    "capability_missing": {"missing"},
}
_ACTIONS = {
    "local_read",
    "workspace_write",
    "network_read",
    "external_write",
    "install",
    "destructive",
    "login",
    "global_activation",
    "permission_expansion",
}
_RISK_FLAGS = {
    "install",
    "destructive",
    "external_mutation",
    "global_activation",
    "login",
    "permission_expansion",
}
_LOCAL_TARGET_SCHEMES = {"workspace", "project", "repository", "store", "artifact"}
_EXTERNAL_TARGET_SCHEMES = {
    "app",
    "connector",
    "customconnector",
    "ftp",
    "github",
    "http",
    "https",
    "mcp",
    "s3",
    "synthetic",
    "ws",
    "wss",
}
_NETWORK_URL_SCHEMES = {"ftp", "http", "https", "ws", "wss"}
_HIERARCHICAL_EXTERNAL_SCHEMES = _NETWORK_URL_SCHEMES | {"s3", "synthetic"}
_LOCATOR_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]{0,31}):(.*)$")


class Lane(str, Enum):
    DIRECT = "DIRECT"
    ASSISTED = "ASSISTED"
    GRAPH = "GRAPH"
    DEEP = "DEEP"


class PermissionClass(str, Enum):
    LOCAL_READ = "local-read"
    WORKSPACE_WRITE = "workspace-write"
    NETWORK_READ = "network-read"
    EXTERNAL_WRITE = "external-write"
    INSTALL_EXECUTABLE = "install-executable"
    DESTRUCTIVE = "destructive"


class CapabilityKind(str, Enum):
    SKILL = "skill"
    PLUGIN = "plugin"
    APP = "app"
    MCP = "mcp"
    HOOK = "hook"
    LOCAL_TOOL = "local_tool"


class CapabilityHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    QUARANTINED = "quarantined"
    DISABLED = "disabled"


class CapabilityTrust(str, Enum):
    BUILTIN = "builtin"
    REPOSITORY_TRUSTED = "repository_trusted"
    APPROVED_PINNED = "approved_pinned"
    UNREVIEWED = "unreviewed"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"


class CostClass(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Gateway(str, Enum):
    ORCHESTRATION = "orchestration"
    RESEARCH = "research"
    BACKEND = "backend"
    FRONTEND = "frontend"
    SECURITY = "security"
    MEMORY = "memory"


class SupplyChainState(str, Enum):
    QUARANTINED = "QUARANTINED"
    REJECTED = "REJECTED"
    APPROVED_PINNED = "APPROVED_PINNED"
    APPROVED_UPDATE_AVAILABLE = "APPROVED_UPDATE_AVAILABLE"


# Login and global activation are material external actions even if a
# capability misleadingly labels them as read-only.
_ACTION_PERMISSIONS = {
    "local_read": PermissionClass.LOCAL_READ,
    "workspace_write": PermissionClass.WORKSPACE_WRITE,
    "network_read": PermissionClass.NETWORK_READ,
    "external_write": PermissionClass.EXTERNAL_WRITE,
    "login": PermissionClass.EXTERNAL_WRITE,
    "global_activation": PermissionClass.EXTERNAL_WRITE,
    "permission_expansion": PermissionClass.EXTERNAL_WRITE,
    "install": PermissionClass.INSTALL_EXECUTABLE,
    "destructive": PermissionClass.DESTRUCTIVE,
}
_PERMISSION_ORDER = {
    PermissionClass.LOCAL_READ: 0,
    PermissionClass.WORKSPACE_WRITE: 1,
    PermissionClass.NETWORK_READ: 2,
    PermissionClass.EXTERNAL_WRITE: 3,
    PermissionClass.INSTALL_EXECUTABLE: 4,
    PermissionClass.DESTRUCTIVE: 5,
}
_COST_ORDER = {CostClass.LOW: 0, CostClass.MEDIUM: 1, CostClass.HIGH: 2}
_HIGH_RISK_PERMISSIONS = {
    PermissionClass.EXTERNAL_WRITE,
    PermissionClass.INSTALL_EXECUTABLE,
    PermissionClass.DESTRUCTIVE,
}
_APPROVAL_REQUIRED_PERMISSIONS = {
    PermissionClass.EXTERNAL_WRITE,
    PermissionClass.INSTALL_EXECUTABLE,
    PermissionClass.DESTRUCTIVE,
}


def _require_text(value: Any, name: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise StorageError("SCHEMA_INVALID", f"{name} must be bounded non-empty text")
    return value


def _optional_text(value: Any, name: str, *, maximum: int = 256) -> str | None:
    if value is None:
        return None
    return _require_text(value, name, maximum=maximum)


def _locator_parts(text: str) -> tuple[str | None, str | None, str]:
    """Return a locator scheme once so all policy checks see the same spelling."""

    match = _LOCATOR_SCHEME.match(text)
    if match is None:
        return None, None, text
    return match.group(1), match.group(1).casefold(), match.group(2)


def _safe_locator(value: Any, name: str, *, maximum: int = 1024) -> str:
    """Validate a bounded, non-executable locator without path reinterpretation."""

    text = _require_text(value, name, maximum=maximum)
    if "\n" in text or "\r" in text or "\\" in text or "%" in text or text.startswith(("/", "~")):
        raise StorageError("SCHEMA_INVALID", f"{name} is not a safe locator")
    scheme, canonical_scheme, remainder = _locator_parts(text)
    if scheme is not None and scheme != canonical_scheme:
        # Case-sensitive policy namespaces must not be normalized later by a
        # different consumer into a more privileged local target.
        raise StorageError("SCHEMA_INVALID", f"{name} has a non-canonical scheme")
    if canonical_scheme == "file":
        raise StorageError("SCHEMA_INVALID", f"{name} may not use a file locator")
    if canonical_scheme in _LOCAL_TARGET_SCHEMES:
        if (
            not remainder
            or remainder.startswith("/")
            or "?" in remainder
            or "#" in remainder
            or "%" in remainder
            or "//" in remainder
            or any(part in {"", ".", ".."} for part in remainder.split("/"))
        ):
            raise StorageError("SCHEMA_INVALID", f"{name} is not a safe local locator")
    if any(part in {".", ".."} for part in text.split("/")):
        raise StorageError("SCHEMA_INVALID", f"{name} is not a safe locator")
    return text


def _explicit_project_root(value: Any) -> Path:
    """Resolve the declared root before accepting a logical local target."""

    text = _require_text(value, "project_root", maximum=4096)
    if "\n" in text or "\r" in text or "\x00" in text or text.startswith("~"):
        raise StorageError("SCHEMA_INVALID", "project_root is not a safe root")
    root = Path(text)
    if not root.is_absolute():
        raise StorageError("SCHEMA_INVALID", "project_root must be absolute")
    try:
        resolved = root.resolve(strict=True)
    except OSError as error:
        raise StorageError("SCHEMA_INVALID", "project_root cannot be resolved") from error
    if not resolved.is_dir() or resolved == Path(resolved.anchor):
        raise StorageError("SCHEMA_INVALID", "project_root must be a project directory")
    return resolved


def _safe_permission_target(value: Any, project_root: Any) -> tuple[str, str | None]:
    """Bind every local logical target to an explicit, contained project root."""

    target = _safe_locator(value, "target", maximum=1024)
    _, scheme, remainder = _locator_parts(target)
    if scheme in _LOCAL_TARGET_SCHEMES:
        root = _explicit_project_root(project_root)
        try:
            resolved_target = (root / remainder).resolve(strict=False)
            resolved_target.relative_to(root)
        except (OSError, ValueError) as error:
            raise StorageError("SCHEMA_INVALID", "target escapes project_root") from error
        return target, str(root)
    if project_root is not None:
        raise StorageError("SCHEMA_INVALID", "external target may not declare project_root")
    if scheme not in _EXTERNAL_TARGET_SCHEMES or not remainder:
        raise StorageError("SCHEMA_INVALID", "external target requires an allowlisted explicit scheme")
    if scheme in _HIERARCHICAL_EXTERNAL_SCHEMES:
        if not remainder.startswith("//"):
            raise StorageError("SCHEMA_INVALID", "external URL target is invalid")
        authority_text = remainder[2:].split("/", 1)[0]
        authority = authority_text.split("?", 1)[0].split("#", 1)[0]
        if (
            not authority
            or authority in {".", ".."}
            or authority.startswith(":")
            or "@" in authority_text
        ):
            raise StorageError("SCHEMA_INVALID", "external URL target is invalid")
    if scheme not in _HIERARCHICAL_EXTERNAL_SCHEMES and (remainder.startswith("/") or "//" in remainder):
        raise StorageError("SCHEMA_INVALID", "external target is invalid")
    return target, None


def _action_summary(value: Any, name: str = "action_summary") -> str:
    """Keep approval context bounded while retaining it only as a digest."""

    text = _require_text(value, name, maximum=512)
    if not text.strip() or "\n" in text or "\r" in text:
        raise StorageError("SCHEMA_INVALID", f"{name} must be one bounded line")
    return text


def _target_scope(target: str) -> str:
    """Map a locator to the capability scope that is allowed to handle it."""

    _, scheme, _ = _locator_parts(target)
    if scheme in _LOCAL_TARGET_SCHEMES:
        return scheme
    return "external"


def _bounded_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum or value > maximum:
        raise StorageError("SCHEMA_INVALID", f"{name} is outside its allowed range")
    return value


def _bounded_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise StorageError("SCHEMA_INVALID", f"{name} must be boolean")
    return value


def _enum(value: Any, enum_type: type[Enum], name: str) -> Enum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as error:
        raise StorageError("SCHEMA_INVALID", f"{name} is not a supported value") from error


def _token(value: Any, name: str, *, maximum: int = 64) -> str:
    text = _require_text(value, name, maximum=maximum).casefold()
    if _TOKEN.fullmatch(text) is None:
        raise StorageError("SCHEMA_INVALID", f"{name} must be a normalized token")
    return text


def _tokens(value: Any, name: str, *, maximum: int = 32) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise StorageError("SCHEMA_INVALID", f"{name} must be a bounded list")
    result = tuple(_token(item, name) for item in value)
    if len(result) != len(set(result)):
        raise StorageError("SCHEMA_INVALID", f"{name} contains duplicates")
    return result


def _identifiers(value: Any, name: str, *, maximum: int = 32) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise StorageError("SCHEMA_INVALID", f"{name} must be a bounded list")
    result = tuple(_identifier(item, name) for item in value)
    if len(result) != len(set(result)):
        raise StorageError("SCHEMA_INVALID", f"{name} contains duplicates")
    return result


def _gateways(value: Any, name: str, *, maximum: int = 6) -> tuple[Gateway, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise StorageError("SCHEMA_INVALID", f"{name} must be a bounded list")
    result = tuple(_enum(item, Gateway, name) for item in value)
    if len(result) != len(set(result)):
        raise StorageError("SCHEMA_INVALID", f"{name} contains duplicates")
    return result


def _identifier(value: Any, name: str = "capability identifier") -> str:
    text = _require_text(value, name, maximum=68)
    if _CAPABILITY_ID.fullmatch(text) is None:
        raise StorageError("SCHEMA_INVALID", f"{name} is invalid")
    return text


def _opaque_id(value: Any, prefix: str, name: str) -> str:
    text = _require_text(value, name, maximum=len(prefix) + 36)
    if not text.startswith(prefix):
        raise StorageError("SCHEMA_INVALID", f"{name} has an invalid prefix")
    suffix = text[len(prefix):]
    if _UUID_TEXT.fullmatch(suffix) is None:
        raise StorageError("SCHEMA_INVALID", f"{name} must use a canonical UUID")
    try:
        parsed = uuid.UUID(suffix)
    except ValueError as error:
        raise StorageError("SCHEMA_INVALID", f"{name} must use a canonical UUID") from error
    if str(parsed) != suffix:
        raise StorageError("SCHEMA_INVALID", f"{name} must use a canonical UUID")
    return text


def _sha256(value: Any, name: str) -> str:
    text = _require_text(value, name, maximum=64)
    if _SHA256.fullmatch(text) is None:
        raise StorageError("SCHEMA_INVALID", f"{name} must be a SHA-256 digest")
    return text


def _permission_tuple(value: Any, name: str, *, maximum: int = 6) -> tuple[PermissionClass, ...]:
    if not isinstance(value, (tuple, list)) or len(value) > maximum:
        raise StorageError("SCHEMA_INVALID", f"{name} must be a bounded list")
    result = tuple(_enum(item, PermissionClass, name) for item in value)
    if len(result) != len(set(result)):
        raise StorageError("SCHEMA_INVALID", f"{name} contains duplicates")
    return tuple(sorted(result, key=lambda item: _PERMISSION_ORDER[item]))


def _rfc3339(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise StorageError("SCHEMA_INVALID", "clock must return an aware datetime")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clock_now(clock: Any = None) -> datetime:
    value = clock.now() if hasattr(clock, "now") else clock() if callable(clock) else datetime.now(UTC)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise StorageError("SCHEMA_INVALID", "clock must return an aware datetime")
    return value.astimezone(UTC)


def _parse_rfc3339(value: str, name: str) -> datetime:
    text = _require_text(value, name, maximum=32)
    if not text.endswith("Z"):
        raise StorageError("SCHEMA_INVALID", f"{name} must be UTC RFC3339")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise StorageError("SCHEMA_INVALID", f"{name} is invalid") from error
    if parsed.tzinfo is None:
        raise StorageError("SCHEMA_INVALID", f"{name} is invalid")
    return parsed.astimezone(UTC)


def _digest(value: Any) -> str:
    return sha256_hex(value)


def _reason_codes(values: Iterable[str]) -> tuple[str, ...]:
    result = tuple(values)
    if not result or any(value not in _REASON_CODES for value in result):
        raise StorageError("SCHEMA_INVALID", "reason code is not allowlisted")
    if len(result) != len(set(result)):
        raise StorageError("SCHEMA_INVALID", "reason codes contain duplicates")
    return result


@dataclass(frozen=True)
class AdmissionFeatures:
    """A normalized, pure feature extraction result for one task."""

    explicit_lane: Lane | str | None = None
    freshness_required: str | bool = "none"
    high_risk: bool = False
    parallel_branches: int = 0
    multi_artifact: bool = False
    workspace_bound: bool = False
    ambiguity: bool = False
    parallel_savings_ms: int = 0
    orchestration_overhead_ms: int = 0
    requested_permissions: tuple[PermissionClass | str, ...] = ()
    risk_flags: tuple[str, ...] = ()
    user_forbid_research: bool = False
    user_forbid_capabilities: bool = False

    def __post_init__(self) -> None:
        explicit = None if self.explicit_lane is None else _enum(self.explicit_lane, Lane, "explicit_lane")
        freshness = self.freshness_required
        if isinstance(freshness, bool):
            freshness = "targeted" if freshness else "none"
        if not isinstance(freshness, str) or freshness not in _FRESHNESS:
            raise StorageError("SCHEMA_INVALID", "freshness_required is not supported")
        object.__setattr__(self, "explicit_lane", explicit)
        object.__setattr__(self, "freshness_required", freshness)
        object.__setattr__(self, "high_risk", _bounded_bool(self.high_risk, "high_risk"))
        object.__setattr__(
            self,
            "parallel_branches",
            _bounded_int(self.parallel_branches, "parallel_branches", minimum=0, maximum=12),
        )
        object.__setattr__(self, "multi_artifact", _bounded_bool(self.multi_artifact, "multi_artifact"))
        object.__setattr__(self, "workspace_bound", _bounded_bool(self.workspace_bound, "workspace_bound"))
        object.__setattr__(self, "ambiguity", _bounded_bool(self.ambiguity, "ambiguity"))
        object.__setattr__(
            self,
            "parallel_savings_ms",
            _bounded_int(self.parallel_savings_ms, "parallel_savings_ms", minimum=0, maximum=86_400_000),
        )
        object.__setattr__(
            self,
            "orchestration_overhead_ms",
            _bounded_int(
                self.orchestration_overhead_ms,
                "orchestration_overhead_ms",
                minimum=0,
                maximum=86_400_000,
            ),
        )
        object.__setattr__(
            self,
            "requested_permissions",
            _permission_tuple(self.requested_permissions, "requested_permissions"),
        )
        risk_flags = _tokens(self.risk_flags, "risk_flags", maximum=16)
        if set(risk_flags) - _RISK_FLAGS:
            raise StorageError("SCHEMA_INVALID", "risk_flags contains an unknown risk signal")
        object.__setattr__(self, "risk_flags", risk_flags)
        object.__setattr__(
            self,
            "user_forbid_research",
            _bounded_bool(self.user_forbid_research, "user_forbid_research"),
        )
        object.__setattr__(
            self,
            "user_forbid_capabilities",
            _bounded_bool(self.user_forbid_capabilities, "user_forbid_capabilities"),
        )

    @classmethod
    def from_value(cls, value: AdmissionFeatures | Mapping[str, Any]) -> AdmissionFeatures:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise StorageError("SCHEMA_INVALID", "admission features must be an object")
        allowed = {
            "explicit_lane",
            "freshness_required",
            "high_risk",
            "parallel_branches",
            "multi_artifact",
            "workspace_bound",
            "ambiguity",
            "parallel_savings_ms",
            "orchestration_overhead_ms",
            "requested_permissions",
            "risk_flags",
            "user_forbid_research",
            "user_forbid_capabilities",
        }
        unknown = set(value) - allowed
        if unknown:
            raise StorageError("SCHEMA_INVALID", "admission features contain unknown fields")
        return cls(
            explicit_lane=value.get("explicit_lane"),
            freshness_required=value.get("freshness_required", "none"),
            high_risk=value.get("high_risk", False),
            parallel_branches=value.get("parallel_branches", 0),
            multi_artifact=value.get("multi_artifact", False),
            workspace_bound=value.get("workspace_bound", False),
            ambiguity=value.get("ambiguity", False),
            parallel_savings_ms=value.get("parallel_savings_ms", 0),
            orchestration_overhead_ms=value.get("orchestration_overhead_ms", 0),
            requested_permissions=value.get("requested_permissions", ()),
            risk_flags=value.get("risk_flags", ()),
            user_forbid_research=value.get("user_forbid_research", False),
            user_forbid_capabilities=value.get("user_forbid_capabilities", False),
        )

    @property
    def is_high_risk(self) -> bool:
        if self.high_risk or any(item in _HIGH_RISK_PERMISSIONS for item in self.requested_permissions):
            return True
        return bool(set(self.risk_flags) & _RISK_FLAGS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "explicit_lane": self.explicit_lane.value if self.explicit_lane is not None else None,
            "freshness_required": self.freshness_required,
            "high_risk": self.high_risk,
            "parallel_branches": self.parallel_branches,
            "multi_artifact": self.multi_artifact,
            "workspace_bound": self.workspace_bound,
            "ambiguity": self.ambiguity,
            "parallel_savings_ms": self.parallel_savings_ms,
            "orchestration_overhead_ms": self.orchestration_overhead_ms,
            "requested_permissions": [item.value for item in self.requested_permissions],
            "risk_flags": list(self.risk_flags),
            "user_forbid_research": self.user_forbid_research,
            "user_forbid_capabilities": self.user_forbid_capabilities,
        }


TaskFeatures = AdmissionFeatures


@dataclass(frozen=True)
class AdmissionDecision:
    lane: Lane | str
    reason_codes: tuple[str, ...]
    retrieval_tier: str | None
    audit_required: bool
    features: AdmissionFeatures
    task_id: str | None = None
    decision_digest: str | None = None

    def __post_init__(self) -> None:
        lane = _enum(self.lane, Lane, "lane")
        reasons = _reason_codes(self.reason_codes)
        if self.retrieval_tier not in {None, "R0"}:
            raise StorageError("SCHEMA_INVALID", "M5 may only declare R0")
        if lane is Lane.DIRECT and self.retrieval_tier != "R0":
            raise StorageError("SCHEMA_INVALID", "DIRECT must bind R0")
        if lane is not Lane.DIRECT and self.retrieval_tier is not None:
            raise StorageError("SCHEMA_INVALID", "non-direct admission cannot construct a retrieval tier")
        if not isinstance(self.audit_required, bool) or self.audit_required != (lane is not Lane.DIRECT):
            raise StorageError("SCHEMA_INVALID", "audit_required does not match lane")
        features = AdmissionFeatures.from_value(self.features)
        task_id = None if self.task_id is None else _opaque_id(self.task_id, "task:", "task_id")
        if lane is not Lane.DIRECT and task_id is None:
            raise StorageError("SCHEMA_INVALID", "non-direct admission decision requires a task_id")
        object.__setattr__(self, "lane", lane)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "features", features)
        object.__setattr__(self, "task_id", task_id)
        payload = self._payload()
        computed_digest = _digest(payload)
        digest = computed_digest if self.decision_digest is None else _sha256(self.decision_digest, "decision_digest")
        if digest != computed_digest:
            raise StorageError("SCHEMA_INVALID", "decision_digest does not match decision payload")
        object.__setattr__(self, "decision_digest", digest)

    @classmethod
    def from_value(cls, value: AdmissionDecision | Mapping[str, Any]) -> AdmissionDecision:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise StorageError("SCHEMA_INVALID", "admission decision must be an object")
        allowed = {
            "lane",
            "reason_codes",
            "retrieval_tier",
            "audit_required",
            "features",
            "task_id",
            "decision_digest",
        }
        if set(value) != allowed:
            raise StorageError("SCHEMA_INVALID", "admission decision fields are invalid")
        return cls(
            lane=value["lane"],
            reason_codes=value["reason_codes"],
            retrieval_tier=value["retrieval_tier"],
            audit_required=value["audit_required"],
            features=AdmissionFeatures.from_value(value["features"]),
            task_id=value.get("task_id"),
            decision_digest=value.get("decision_digest"),
        )

    def _payload(self) -> dict[str, Any]:
        return {
            "lane": self.lane.value,
            "reason_codes": list(self.reason_codes),
            "retrieval_tier": self.retrieval_tier,
            "audit_required": self.audit_required,
            "features": self.features.to_dict(),
            "task_id": self.task_id,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["decision_digest"] = self.decision_digest
        return payload

    def verify(self) -> bool:
        return _digest(self._payload()) == self.decision_digest


@dataclass(frozen=True)
class AuditEvent:
    """An allowlisted audit value which never retains raw task/capability text."""

    event_id: str
    task_id: str
    event_type: str
    occurred_at: str
    lane: Lane | str
    status: str
    reason_codes: tuple[str, ...]
    permission: PermissionClass | str | None = None
    capability_ref: str | None = None
    target_digest: str | None = None
    action_digest: str | None = None
    admission_digest: str | None = None
    event_digest: str | None = None

    def __post_init__(self) -> None:
        event_id = _opaque_id(self.event_id, "evt:", "event_id")
        task_id = _opaque_id(self.task_id, "task:", "task_id")
        if self.event_type not in _AUDIT_EVENT_TYPES:
            raise StorageError("SCHEMA_INVALID", "audit event type is not allowlisted")
        if self.status not in _AUDIT_STATUSES:
            raise StorageError("SCHEMA_INVALID", "audit status is not allowlisted")
        if self.status not in _AUDIT_EVENT_STATUSES[self.event_type]:
            raise StorageError("SCHEMA_INVALID", "audit status does not match event type")
        _parse_rfc3339(self.occurred_at, "occurred_at")
        lane = _enum(self.lane, Lane, "lane")
        reasons = _reason_codes(self.reason_codes)
        permission = None if self.permission is None else _enum(self.permission, PermissionClass, "permission")
        capability_ref = None if self.capability_ref is None else _sha256(self.capability_ref, "capability_ref")
        target_digest = None if self.target_digest is None else _sha256(self.target_digest, "target_digest")
        action_digest = None if self.action_digest is None else _sha256(self.action_digest, "action_digest")
        admission_digest = (
            None if self.admission_digest is None else _sha256(self.admission_digest, "admission_digest")
        )
        is_admission_event = self.event_type in {"admission_decided", "admission_escalated"}
        if is_admission_event != (admission_digest is not None):
            raise StorageError("SCHEMA_INVALID", "admission event binding is invalid")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "task_id", task_id)
        object.__setattr__(self, "lane", lane)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "permission", permission)
        object.__setattr__(self, "capability_ref", capability_ref)
        object.__setattr__(self, "target_digest", target_digest)
        object.__setattr__(self, "action_digest", action_digest)
        object.__setattr__(self, "admission_digest", admission_digest)
        computed_digest = _digest(self._payload())
        digest = computed_digest if self.event_digest is None else _sha256(self.event_digest, "event_digest")
        if digest != computed_digest:
            raise StorageError("SCHEMA_INVALID", "event_digest does not match event payload")
        object.__setattr__(self, "event_digest", digest)

    @classmethod
    def from_value(cls, value: AuditEvent | Mapping[str, Any]) -> AuditEvent:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise StorageError("SCHEMA_INVALID", "audit event must be an object")
        allowed = {
            "event_id",
            "task_id",
            "event_type",
            "occurred_at",
            "lane",
            "status",
            "reason_codes",
            "permission",
            "capability_ref",
            "target_digest",
            "action_digest",
            "admission_digest",
            "event_digest",
        }
        required = allowed - {"capability_ref", "target_digest", "action_digest", "admission_digest"}
        if set(value) - allowed or required - set(value):
            raise StorageError("SCHEMA_INVALID", "audit event fields are invalid")
        event = cls(**dict(value))
        if "event_digest" in value and not event.verify():
            raise StorageError("SCHEMA_INVALID", "audit event digest does not match event payload")
        return event

    def _payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "lane": self.lane.value,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "permission": None if self.permission is None else self.permission.value,
            "capability_ref": self.capability_ref,
            "target_digest": self.target_digest,
            "action_digest": self.action_digest,
            "admission_digest": self.admission_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["event_digest"] = self.event_digest
        return payload

    def verify(self) -> bool:
        return _digest(self._payload()) == self.event_digest


@dataclass(frozen=True)
class _AdmissionReceipt:
    """Trail-owned proof that the deterministic admission transition ran."""

    decision: AdmissionDecision
    decision_digest: str
    task_id: str
    lane: Lane
    reason_codes: tuple[str, ...]
    event_id: str


@dataclass(frozen=True)
class _SupplyChainReceipt:
    event_id: str
    task_id: str
    lane: Lane
    candidate_id: str
    pin_digest: str
    review_handle: object


@dataclass(frozen=True)
class _MaterialAuthorityReceipt:
    authority: "MaterialCapabilityAuthority"
    review_event_id: str
    review_handle: object


class AuditTrail:
    """In-memory project-local audit collector with generated opaque event IDs."""

    def __init__(
        self,
        *,
        clock: Any = None,
        id_factory: Callable[[], str | uuid.UUID] | None = None,
        fail_writes: bool = False,
    ) -> None:
        self._clock = clock
        self._id_factory = id_factory or uuid.uuid4
        self._fail_writes = _bounded_bool(fail_writes, "fail_writes")
        self._events: list[AuditEvent] = []
        self._admission_receipts: dict[str, _AdmissionReceipt] = {}
        self._supply_chain_receipts: dict[str, _SupplyChainReceipt] = {}
        self._supply_chain_candidates: dict[str, object] = {}
        self._material_authority_receipts: dict[str, _MaterialAuthorityReceipt] = {}

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        return tuple(self._events)

    def _next_event_id(self) -> str:
        raw = self._id_factory()
        suffix = str(raw)
        if suffix.startswith("evt:"):
            return _opaque_id(suffix, "evt:", "event_id")
        if _UUID_TEXT.fullmatch(suffix) is None:
            raise StorageError("SCHEMA_INVALID", "id_factory must produce a canonical UUID")
        return _opaque_id(f"evt:{suffix}", "evt:", "event_id")

    def _record(
        self,
        *,
        event_type: str,
        task_id: str,
        lane: Lane | str,
        status: str,
        reason_codes: Sequence[str],
        capability_id: str | None = None,
        capability_version: str | None = None,
        permission: PermissionClass | str | None = None,
        target: str | None = None,
        action: str | None = None,
        action_summary: str | None = None,
        admission_digest: str | None = None,
    ) -> AuditEvent:
        if self._fail_writes:
            raise StorageError("AUDIT_FAILED", "audit trail is unavailable")
        capability_ref = (
            None
            if capability_id is None
            else _digest(
                {
                    "capability_id": _identifier(capability_id),
                    "capability_version": _require_text(
                        capability_version or "unknown",
                        "capability_version",
                        maximum=128,
                    ),
                }
            )
        )
        target_digest = None if target is None else _digest({"target": _require_text(target, "target", maximum=1024)})
        permission_value = None if permission is None else _enum(permission, PermissionClass, "permission")
        if action is None and action_summary is not None:
            raise StorageError("SCHEMA_INVALID", "action_summary requires an action")
        is_admission_event = event_type in {"admission_decided", "admission_escalated"}
        if is_admission_event != (admission_digest is not None):
            raise StorageError("SCHEMA_INVALID", "admission event binding is invalid")
        action_digest = (
            None
            if action is None
            else _digest(
                {
                    "action": _token(action, "action"),
                    "action_summary": None if action_summary is None else _action_summary(action_summary),
                }
            )
        )
        event = AuditEvent(
            event_id=self._next_event_id(),
            task_id=task_id,
            event_type=event_type,
            occurred_at=_rfc3339(_clock_now(self._clock)),
            lane=lane,
            status=status,
            reason_codes=tuple(reason_codes),
            permission=permission_value,
            capability_ref=capability_ref,
            target_digest=target_digest,
            action_digest=action_digest,
            admission_digest=admission_digest,
        )
        if any(existing.event_id == event.event_id for existing in self._events):
            raise StorageError("AUDIT_FAILED", "audit event identifier collision")
        self._events.append(event)
        return event

    def record(
        self,
        *,
        event_type: str,
        task_id: str,
        lane: Lane | str,
        status: str,
        reason_codes: Sequence[str],
        capability_id: str | None = None,
        capability_version: str | None = None,
        permission: PermissionClass | str | None = None,
        target: str | None = None,
        action: str | None = None,
        action_summary: str | None = None,
        admission_digest: str | None = None,
    ) -> AuditEvent:
        """Record a public audit event; admission transitions are trail-owned."""

        if event_type in {"admission_decided", "admission_escalated"}:
            raise StorageError("POLICY_DENIED", "admission events are issued only by the admission engine")
        return self._record(
            event_type=event_type,
            task_id=task_id,
            lane=lane,
            status=status,
            reason_codes=reason_codes,
            capability_id=capability_id,
            capability_version=capability_version,
            permission=permission,
            target=target,
            action=action,
            action_summary=action_summary,
            admission_digest=admission_digest,
        )

    def _issue_admission(
        self,
        features: AdmissionFeatures | Mapping[str, Any],
        *,
        task_id: str,
        event_type: str,
        previous: AdmissionDecision | None = None,
    ) -> AdmissionDecision:
        """Classify and issue a non-direct decision in one trail-owned transition."""

        if event_type not in {"admission_decided", "admission_escalated"}:
            raise StorageError("SCHEMA_INVALID", "admission event type is invalid")
        lane, reasons, current = _classify_admission(features)
        if lane is Lane.DIRECT:
            raise StorageError("POLICY_DENIED", "DIRECT admission does not create an audit receipt")
        if event_type == "admission_decided":
            if previous is not None:
                raise StorageError("SCHEMA_INVALID", "new admission cannot carry a predecessor")
            decision_reasons = reasons
        else:
            if not isinstance(previous, AdmissionDecision) or not previous.verify():
                raise StorageError("POLICY_DENIED", "admission escalation lacks a valid predecessor")
            if _LANE_RANK[lane] <= _LANE_RANK[previous.lane]:
                raise StorageError("POLICY_DENIED", "admission escalation must increase the lane")
            decision_reasons = (*reasons, "ESCALATED_NEW_RISK")
        decision = _decision(lane, decision_reasons, current, task_id=task_id)
        existing = self._admission_receipts.get(decision.decision_digest)
        if existing is not None:
            if (
                existing.task_id != decision.task_id
                or existing.lane is not decision.lane
                or existing.reason_codes != decision.reason_codes
            ):
                raise StorageError("AUDIT_FAILED", "admission receipt conflicts with the decision")
            event = next((item for item in self._events if item.event_id == existing.event_id), None)
            if event is None or event.event_type != event_type:
                raise StorageError("AUDIT_FAILED", "admission receipt is incomplete")
            return existing.decision
        event = self._record(
            event_type=event_type,
            task_id=decision.task_id,
            lane=decision.lane,
            status="decided",
            reason_codes=decision.reason_codes,
            admission_digest=decision.decision_digest,
        )
        self._admission_receipts[decision.decision_digest] = _AdmissionReceipt(
            decision=decision,
            decision_digest=decision.decision_digest,
            task_id=decision.task_id,
            lane=decision.lane,
            reason_codes=decision.reason_codes,
            event_id=event.event_id,
        )
        return decision

    def has_admission(self, decision: AdmissionDecision) -> bool:
        """Return true only for an admission issued by the classifier transition."""

        receipt = (
            self._admission_receipts.get(decision.decision_digest)
            if isinstance(decision, AdmissionDecision)
            else None
        )
        return (
            isinstance(decision, AdmissionDecision)
            and decision.lane is not Lane.DIRECT
            and decision.task_id is not None
            and decision.verify()
            and receipt is not None
            and receipt.decision is decision
            and receipt.task_id == decision.task_id
            and receipt.lane is decision.lane
            and receipt.reason_codes == decision.reason_codes
            and any(
                event.event_type in {"admission_decided", "admission_escalated"}
                and event.task_id == decision.task_id
                and event.lane is decision.lane
                and event.admission_digest == decision.decision_digest
                and event.event_id == receipt.event_id
                for event in self._events
            )
        )

    def _record_supply_chain_review(
        self,
        review: Any,
        *,
        task_id: str,
        lane: Lane,
        _issuer: object | None = None,
    ) -> AuditEvent:
        if _issuer is not _SUPPLY_CHAIN_ISSUER:
            raise StorageError("POLICY_DENIED", "supply-chain receipts are issued only by the lifecycle")
        if not isinstance(review, SupplyChainReview) or not review._is_lifecycle_issued:
            raise StorageError("POLICY_DENIED", "supply-chain review lacks a lifecycle transition")
        prior_handle = self._supply_chain_candidates.get(review.candidate_id)
        if prior_handle is not None and prior_handle is not review._lineage:
            raise StorageError("POLICY_DENIED", "candidate must receive a new identity for another review")
        event = self.record(
            event_type="supply_chain_reviewed",
            task_id=task_id,
            lane=lane,
            status="reviewed" if review.installable else "failed",
            reason_codes=review.reason_codes or ("SUPPLY_CHAIN_APPROVED",),
            capability_id=review.capability_id,
            capability_version=review.capability_version,
        )
        self._supply_chain_receipts[event.event_id] = _SupplyChainReceipt(
            event_id=event.event_id,
            task_id=_opaque_id(task_id, "task:", "task_id"),
            lane=lane,
            candidate_id=review.candidate_id,
            pin_digest=review.pin_digest,
            review_handle=review._lineage,
        )
        self._supply_chain_candidates[review.candidate_id] = review._lineage
        return event

    def _issue_material_authority(
        self,
        authority: "MaterialCapabilityAuthority",
        review: Any,
        *,
        _issuer: object | None = None,
    ) -> "MaterialCapabilityAuthority":
        if _issuer is not _MATERIAL_AUTHORITY_ISSUER:
            raise StorageError("POLICY_DENIED", "material authority issuance is internal")
        if not isinstance(review, SupplyChainReview) or not review.installable:
            raise StorageError("POLICY_DENIED", "material authority requires an approved review")
        receipt = next(
            (
                item
                for item in reversed(tuple(self._supply_chain_receipts.values()))
                if item.event_id == authority.review_receipt_id
                and item.candidate_id == review.candidate_id
                and item.pin_digest == review.pin_digest
                and item.review_handle is review._lineage
            ),
            None,
        )
        if receipt is None:
            raise StorageError("POLICY_DENIED", "material authority lacks a matching review receipt")
        if authority.authority_digest in self._material_authority_receipts:
            raise StorageError("AUDIT_FAILED", "material authority was already issued")
        self._material_authority_receipts[authority.authority_digest] = _MaterialAuthorityReceipt(
            authority=authority,
            review_event_id=receipt.event_id,
            review_handle=review._lineage,
        )
        return authority

    def has_material_authority(self, authority: Any, review: Any) -> bool:
        if not isinstance(authority, MaterialCapabilityAuthority) or not isinstance(review, SupplyChainReview):
            return False
        receipt = self._material_authority_receipts.get(authority.authority_digest)
        return (
            receipt is not None
            and receipt.authority is authority
            and receipt.review_event_id == authority.review_receipt_id
            and receipt.review_handle is review._lineage
            and review.installable
            and review._is_lifecycle_issued
        )


def _decision(
    lane: Lane,
    reasons: Sequence[str],
    features: AdmissionFeatures,
    *,
    task_id: str | None,
) -> AdmissionDecision:
    return AdmissionDecision(
        lane=lane,
        reason_codes=tuple(reasons),
        retrieval_tier="R0" if lane is Lane.DIRECT else None,
        audit_required=lane is not Lane.DIRECT,
        features=features,
        task_id=task_id,
    )


def _classify_admission(
    features: AdmissionFeatures | Mapping[str, Any],
) -> tuple[Lane, tuple[str, ...], AdmissionFeatures]:
    """Pure admission classifier used before an auditable state transition."""

    current = AdmissionFeatures.from_value(features)
    if current.is_high_risk:
        reasons: list[str] = ["HIGH_RISK"]
        if current.explicit_lane in {Lane.DIRECT, Lane.ASSISTED, Lane.GRAPH}:
            reasons.append("USER_LOWER_LANE_NOT_APPLIED")
        return Lane.DEEP, tuple(reasons), current
    elif current.explicit_lane is Lane.DEEP:
        return Lane.DEEP, ("USER_EXPLICIT_DEEP",), current
    elif current.parallel_branches >= 2 and (
        current.multi_artifact or current.parallel_savings_ms > current.orchestration_overhead_ms
    ):
        return Lane.GRAPH, ("INDEPENDENT_BRANCHES",), current
    elif current.explicit_lane is Lane.GRAPH:
        return Lane.GRAPH, ("USER_EXPLICIT_GRAPH",), current
    elif current.workspace_bound or current.freshness_required != "none" or current.ambiguity:
        return Lane.ASSISTED, ("BOUNDED_TOOL_OR_EVIDENCE_NEED",), current
    return Lane.DIRECT, ("SELF_CONTAINED_LOW_RISK",), current


def admit(
    features: AdmissionFeatures | Mapping[str, Any],
    *,
    task_id: str | None = None,
    audit_trail: AuditTrail | None = None,
) -> AdmissionDecision:
    """Classify normalized task features without performing any capability work."""

    if audit_trail is not None and not isinstance(audit_trail, AuditTrail):
        raise StorageError("SCHEMA_INVALID", "audit_trail is invalid")
    lane, reasons, current = _classify_admission(features)
    if lane is not Lane.DIRECT:
        if audit_trail is None:
            raise StorageError("AUDIT_FAILED", "non-direct admission requires an audit trail")
        if task_id is None:
            raise StorageError("SCHEMA_INVALID", "non-direct admission needs an opaque task_id for audit")
    result = _decision(lane, reasons, current, task_id=task_id)
    if result.audit_required:
        result = audit_trail._issue_admission(
            current,
            task_id=task_id,
            event_type="admission_decided",
            previous=None,
        )
    return result


_LANE_RANK = {Lane.DIRECT: 0, Lane.ASSISTED: 1, Lane.GRAPH: 2, Lane.DEEP: 3}


def escalate_admission(
    previous: AdmissionDecision,
    features: AdmissionFeatures | Mapping[str, Any],
    *,
    task_id: str | None = None,
    audit_trail: AuditTrail | None = None,
) -> AdmissionDecision:
    """Apply newly observed features without allowing a safety downgrade."""

    if not isinstance(previous, AdmissionDecision):
        raise StorageError("SCHEMA_INVALID", "previous admission decision is required")
    if audit_trail is not None and not isinstance(audit_trail, AuditTrail):
        raise StorageError("SCHEMA_INVALID", "audit_trail is invalid")
    candidate_lane, candidate_reasons, candidate_features = _classify_admission(features)
    if _LANE_RANK[candidate_lane] <= _LANE_RANK[previous.lane]:
        return previous
    effective_task_id = previous.task_id if task_id is None else _opaque_id(task_id, "task:", "task_id")
    if effective_task_id is None:
        raise StorageError("SCHEMA_INVALID", "escalation needs an opaque task_id for audit")
    if previous.task_id is not None and previous.task_id != effective_task_id:
        raise StorageError("POLICY_DENIED", "admission escalation cannot change task identity")
    if audit_trail is None:
        raise StorageError("AUDIT_FAILED", "admission escalation requires an audit trail")
    result = audit_trail._issue_admission(
        candidate_features,
        task_id=effective_task_id,
        event_type="admission_escalated",
        previous=previous,
    )
    return result


class AdmissionEngine:
    """Small facade that keeps injected audit state separate from classification."""

    def __init__(self, *, audit_trail: AuditTrail | None = None) -> None:
        if audit_trail is not None and not isinstance(audit_trail, AuditTrail):
            raise StorageError("SCHEMA_INVALID", "audit_trail is invalid")
        self.audit_trail = audit_trail

    def admit(
        self,
        features: AdmissionFeatures | Mapping[str, Any],
        *,
        task_id: str | None = None,
    ) -> AdmissionDecision:
        return admit(features, task_id=task_id, audit_trail=self.audit_trail)

    def escalate(
        self,
        previous: AdmissionDecision,
        features: AdmissionFeatures | Mapping[str, Any],
        *,
        task_id: str | None = None,
    ) -> AdmissionDecision:
        return escalate_admission(previous, features, task_id=task_id, audit_trail=self.audit_trail)


@dataclass(frozen=True)
class PermissionRequest:
    task_id: str
    admission_digest: str
    capability_id: str
    capability_version: str
    capability_authority_digest: str
    permission: PermissionClass | str
    target: str
    action: str
    action_summary: str
    lane: Lane | str
    candidate_pin: str | None = None
    rollback_statement: str | None = None
    project_root: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _opaque_id(self.task_id, "task:", "task_id"))
        object.__setattr__(self, "admission_digest", _sha256(self.admission_digest, "admission_digest"))
        object.__setattr__(self, "capability_id", _identifier(self.capability_id))
        object.__setattr__(self, "capability_version", _require_text(self.capability_version, "capability_version", maximum=128))
        object.__setattr__(
            self,
            "capability_authority_digest",
            _sha256(self.capability_authority_digest, "capability_authority_digest"),
        )
        object.__setattr__(self, "permission", _enum(self.permission, PermissionClass, "permission"))
        target, project_root = _safe_permission_target(self.target, self.project_root)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "project_root", project_root)
        action = _token(self.action, "action")
        if action not in _ACTIONS:
            raise StorageError("SCHEMA_INVALID", "action is not allowlisted")
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "action_summary", _action_summary(self.action_summary))
        rollback_statement = (
            None
            if self.rollback_statement is None
            else _action_summary(self.rollback_statement, "rollback_statement")
        )
        if action == "destructive" and rollback_statement is None:
            raise StorageError("SCHEMA_INVALID", "destructive action requires a rollback_statement")
        object.__setattr__(self, "rollback_statement", rollback_statement)
        object.__setattr__(self, "lane", _enum(self.lane, Lane, "lane"))
        object.__setattr__(
            self,
            "candidate_pin",
            None if self.candidate_pin is None else _sha256(self.candidate_pin, "candidate_pin"),
        )

    @classmethod
    def from_value(cls, value: PermissionRequest | Mapping[str, Any]) -> PermissionRequest:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise StorageError("SCHEMA_INVALID", "permission request must be an object")
        allowed = {
            "task_id",
            "admission_digest",
            "capability_id",
            "capability_version",
            "capability_authority_digest",
            "permission",
            "target",
            "action",
            "action_summary",
            "lane",
            "candidate_pin",
            "rollback_statement",
            "project_root",
        }
        required = allowed - {"candidate_pin", "rollback_statement", "project_root"}
        if set(value) - allowed or required - set(value):
            raise StorageError("SCHEMA_INVALID", "permission request fields are invalid")
        return cls(**dict(value))

    @property
    def target_digest(self) -> str:
        return _digest({"target": self.target})

    @property
    def project_root_digest(self) -> str | None:
        return None if self.project_root is None else _digest({"project_root": self.project_root})

    @property
    def action_digest(self) -> str:
        return _digest({"action": self.action, "action_summary": self.action_summary})

    @property
    def rollback_digest(self) -> str | None:
        return None if self.rollback_statement is None else _digest({"rollback_statement": self.rollback_statement})

    @property
    def action_payload_digest(self) -> str:
        """Bind approval to the complete action payload without serializing it."""

        return _digest(
            {
                "task_id": self.task_id,
                "admission_digest": self.admission_digest,
                "capability_id": self.capability_id,
                "capability_version": self.capability_version,
                "capability_authority_digest": self.capability_authority_digest,
                "permission": self.permission.value,
                "target_digest": self.target_digest,
                "target_scope": self.target_scope,
                "project_root_digest": self.project_root_digest,
                "action_digest": self.action_digest,
                "rollback_digest": self.rollback_digest,
                "lane": self.lane.value,
                "candidate_pin": self.candidate_pin,
            }
        )

    @property
    def target_scope(self) -> str:
        return _target_scope(self.target)

    @property
    def target_requires_network(self) -> bool:
        # Local operations require an explicit project-local namespace. This
        # keeps opaque names and every unknown scheme from being reinterpreted
        # later as a connector target.
        return self.target_scope == "external"

    @property
    def requires_explicit_approval(self) -> bool:
        return self.permission in _APPROVAL_REQUIRED_PERMISSIONS or self.action in {
            "install",
            "destructive",
            "login",
            "global_activation",
            "permission_expansion",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "admission_digest": self.admission_digest,
            "capability_ref": _digest(
                {"capability_id": self.capability_id, "capability_version": self.capability_version}
            ),
            "capability_authority_digest": self.capability_authority_digest,
            "permission": self.permission.value,
            "target_digest": self.target_digest,
            "project_root_digest": self.project_root_digest,
            "action_digest": self.action_digest,
            "action_payload_digest": self.action_payload_digest,
            "rollback_digest": self.rollback_digest,
            "lane": self.lane.value,
            "candidate_pin": self.candidate_pin,
        }


@dataclass(frozen=True)
class UserApproval:
    """A serializable approval request; only an authority-issued receipt is live."""

    task_id: str
    admission_digest: str
    capability_id: str
    capability_version: str
    capability_authority_digest: str
    permission: PermissionClass | str
    target_digest: str
    project_root_digest: str | None
    action_digest: str
    action_payload_digest: str
    rollback_digest: str | None
    expires_at: str
    candidate_pin: str | None = None
    decision: str = "approved"
    receipt_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _opaque_id(self.task_id, "task:", "task_id"))
        object.__setattr__(self, "admission_digest", _sha256(self.admission_digest, "admission_digest"))
        object.__setattr__(self, "capability_id", _identifier(self.capability_id))
        object.__setattr__(self, "capability_version", _require_text(self.capability_version, "capability_version", maximum=128))
        object.__setattr__(
            self,
            "capability_authority_digest",
            _sha256(self.capability_authority_digest, "capability_authority_digest"),
        )
        object.__setattr__(self, "permission", _enum(self.permission, PermissionClass, "permission"))
        object.__setattr__(self, "target_digest", _sha256(self.target_digest, "target_digest"))
        object.__setattr__(
            self,
            "project_root_digest",
            None if self.project_root_digest is None else _sha256(self.project_root_digest, "project_root_digest"),
        )
        object.__setattr__(self, "action_digest", _sha256(self.action_digest, "action_digest"))
        object.__setattr__(
            self,
            "action_payload_digest",
            _sha256(self.action_payload_digest, "action_payload_digest"),
        )
        object.__setattr__(
            self,
            "rollback_digest",
            None if self.rollback_digest is None else _sha256(self.rollback_digest, "rollback_digest"),
        )
        _parse_rfc3339(self.expires_at, "expires_at")
        object.__setattr__(
            self,
            "candidate_pin",
            None if self.candidate_pin is None else _sha256(self.candidate_pin, "candidate_pin"),
        )
        if self.decision != "approved":
            raise StorageError("SCHEMA_INVALID", "approval decision must be explicit approval")
        object.__setattr__(
            self,
            "receipt_id",
            None if self.receipt_id is None else _opaque_id(self.receipt_id, "approval:", "receipt_id"),
        )

    @classmethod
    def from_value(cls, value: UserApproval | Mapping[str, Any]) -> UserApproval:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise StorageError("SCHEMA_INVALID", "user approval must be an object")
        allowed = {
            "task_id",
            "admission_digest",
            "capability_id",
            "capability_version",
            "capability_authority_digest",
            "permission",
            "target_digest",
            "project_root_digest",
            "action_digest",
            "action_payload_digest",
            "rollback_digest",
            "expires_at",
            "candidate_pin",
            "decision",
            "receipt_id",
        }
        required = allowed - {"candidate_pin", "receipt_id"}
        if set(value) - allowed or required - set(value):
            raise StorageError("SCHEMA_INVALID", "user approval fields are invalid")
        return cls(**dict(value))

    @classmethod
    def for_request(
        cls,
        request: PermissionRequest,
        *,
        clock: Any = None,
        expires_in_seconds: int = 300,
    ) -> UserApproval:
        if not isinstance(request, PermissionRequest):
            raise StorageError("SCHEMA_INVALID", "permission request is required")
        duration = _bounded_int(expires_in_seconds, "expires_in_seconds", minimum=1, maximum=86_400)
        expiry = _clock_now(clock) + timedelta(seconds=duration)
        # This is a non-live request draft. M5 never treats serializable data
        # as proof of a live user confirmation.
        return cls(
            task_id=request.task_id,
            admission_digest=request.admission_digest,
            capability_id=request.capability_id,
            capability_version=request.capability_version,
            capability_authority_digest=request.capability_authority_digest,
            permission=request.permission,
            target_digest=request.target_digest,
            project_root_digest=request.project_root_digest,
            action_digest=request.action_digest,
            action_payload_digest=request.action_payload_digest,
            rollback_digest=request.rollback_digest,
            expires_at=_rfc3339(expiry),
            candidate_pin=request.candidate_pin,
            decision="approved",
            receipt_id=None,
        )

    def matches(self, request: PermissionRequest, *, now: datetime) -> bool:
        return (
            self.task_id == request.task_id
            and self.admission_digest == request.admission_digest
            and self.capability_id == request.capability_id
            and self.capability_version == request.capability_version
            and self.capability_authority_digest == request.capability_authority_digest
            and self.permission is request.permission
            and self.target_digest == request.target_digest
            and self.project_root_digest == request.project_root_digest
            and self.action_digest == request.action_digest
            and self.action_payload_digest == request.action_payload_digest
            and self.rollback_digest == request.rollback_digest
            and self.candidate_pin == request.candidate_pin
            and _parse_rfc3339(self.expires_at, "expires_at") > now
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "admission_digest": self.admission_digest,
            "capability_ref": _digest(
                {"capability_id": self.capability_id, "capability_version": self.capability_version}
            ),
            "capability_authority_digest": self.capability_authority_digest,
            "permission": self.permission.value,
            "target_digest": self.target_digest,
            "project_root_digest": self.project_root_digest,
            "action_digest": self.action_digest,
            "action_payload_digest": self.action_payload_digest,
            "rollback_digest": self.rollback_digest,
            "expires_at": self.expires_at,
            "candidate_pin": self.candidate_pin,
            "decision": self.decision,
            "receipt_id": self.receipt_id,
        }


@dataclass(frozen=True)
class PermissionDecision:
    status: str
    reason_codes: tuple[str, ...]
    task_id: str
    permission: PermissionClass | str
    target_digest: str
    action_digest: str
    action_payload_digest: str
    effective_permissions: tuple[PermissionClass | str, ...]
    audit_event_id: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"granted", "approval_required", "denied"}:
            raise StorageError("SCHEMA_INVALID", "permission status is not supported")
        object.__setattr__(self, "reason_codes", _reason_codes(self.reason_codes))
        object.__setattr__(self, "task_id", _opaque_id(self.task_id, "task:", "task_id"))
        object.__setattr__(self, "permission", _enum(self.permission, PermissionClass, "permission"))
        object.__setattr__(self, "target_digest", _sha256(self.target_digest, "target_digest"))
        object.__setattr__(self, "action_digest", _sha256(self.action_digest, "action_digest"))
        object.__setattr__(
            self,
            "action_payload_digest",
            _sha256(self.action_payload_digest, "action_payload_digest"),
        )
        object.__setattr__(
            self,
            "effective_permissions",
            _permission_tuple(self.effective_permissions, "effective_permissions"),
        )
        object.__setattr__(
            self,
            "audit_event_id",
            None if self.audit_event_id is None else _opaque_id(self.audit_event_id, "evt:", "audit_event_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "task_id": self.task_id,
            "permission": self.permission.value,
            "target_digest": self.target_digest,
            "action_digest": self.action_digest,
            "action_payload_digest": self.action_payload_digest,
            "effective_permissions": [item.value for item in self.effective_permissions],
            "audit_event_id": self.audit_event_id,
        }


class PermissionBroker:
    """Compute policy decisions only; M5 has no live approval/execution host."""

    def __init__(
        self,
        *,
        clock: Any = None,
        audit_trail: AuditTrail | None = None,
        project_root: str | None = None,
    ) -> None:
        if audit_trail is not None and not isinstance(audit_trail, AuditTrail):
            raise StorageError("SCHEMA_INVALID", "audit_trail is invalid")
        # The request carries an untrusted claim about its local root. A host
        # must bind this broker to the project it is actually permitted to use.
        configured_root = None if project_root is None else str(_explicit_project_root(project_root))
        self._clock = clock
        self._audit_trail = audit_trail
        self._project_root = configured_root

    def _audit(
        self,
        request: PermissionRequest,
        *,
        status: str,
        reasons: Sequence[str],
    ) -> str:
        if self._audit_trail is None:
            raise StorageError("AUDIT_FAILED", "non-direct permission decision requires an audit trail")
        event_type = {
            "granted": "permission_granted",
            "approval_required": "permission_requested",
            "denied": "permission_denied",
        }[status]
        event = self._audit_trail.record(
            event_type=event_type,
            task_id=request.task_id,
            lane=request.lane,
            status=status,
            reason_codes=reasons,
            capability_id=request.capability_id,
            capability_version=request.capability_version,
            permission=request.permission,
            target=request.target,
            action=request.action,
            action_summary=request.action_summary,
        )
        return event.event_id

    @staticmethod
    def _decision(
        request: PermissionRequest,
        *,
        status: str,
        reasons: Sequence[str],
        effective: Sequence[PermissionClass],
        audit_event_id: str | None = None,
    ) -> PermissionDecision:
        return PermissionDecision(
            status=status,
            reason_codes=tuple(reasons),
            task_id=request.task_id,
            permission=request.permission,
            target_digest=request.target_digest,
            action_digest=request.action_digest,
            action_payload_digest=request.action_payload_digest,
            effective_permissions=tuple(effective),
            audit_event_id=audit_event_id,
        )

    def _capability_authority_reason(
        self,
        request: PermissionRequest,
        descriptor: CapabilityDescriptor | None,
        material_authority: MaterialCapabilityAuthority | None,
        supply_chain_review: "SupplyChainReview" | None,
    ) -> str | None:
        if descriptor is not None and material_authority is not None:
            return "CAPABILITY_DESCRIPTOR_MISMATCH"
        if material_authority is not None:
            if request.permission not in _HIGH_RISK_PERMISSIONS:
                return "CAPABILITY_DESCRIPTOR_MISMATCH"
            if (
                material_authority.identifier != request.capability_id
                or material_authority.version != request.capability_version
                or material_authority.permission is not request.permission
                or material_authority.authority_digest != request.capability_authority_digest
            ):
                return "CAPABILITY_DESCRIPTOR_MISMATCH"
            if request.target_scope not in material_authority.scopes:
                return "SCOPE_NOT_PERMITTED"
            if (
                self._audit_trail is None
                or supply_chain_review is None
                or not self._audit_trail.has_material_authority(material_authority, supply_chain_review)
            ):
                return "SUPPLY_CHAIN_NOT_APPROVED"
            return None
        if descriptor is None:
            return "CAPABILITY_DESCRIPTOR_REQUIRED"
        if (
            descriptor.identifier != request.capability_id
            or descriptor.version != request.capability_version
            or descriptor.reference_digest != request.capability_authority_digest
        ):
            return "CAPABILITY_DESCRIPTOR_MISMATCH"
        if descriptor.required_permission is not request.permission:
            return "CAPABILITY_DESCRIPTOR_MISMATCH"
        if request.target_scope not in descriptor.scopes:
            return "SCOPE_NOT_PERMITTED"
        if not descriptor.installed:
            return "NOT_INSTALLED"
        if descriptor.health is not CapabilityHealth.HEALTHY:
            return {
                CapabilityHealth.DEGRADED: "HEALTH_DEGRADED",
                CapabilityHealth.UNAVAILABLE: "HEALTH_UNAVAILABLE",
                CapabilityHealth.QUARANTINED: "HEALTH_QUARANTINED",
                CapabilityHealth.DISABLED: "HEALTH_DISABLED",
            }[descriptor.health]
        if descriptor.trust.value not in _SAFE_TRUST:
            return "TRUST_NOT_PERMITTED"
        if request.action == "login" and not descriptor.requires_login:
            return "CAPABILITY_DESCRIPTOR_MISMATCH"
        if request.action == "global_activation" and not descriptor.global_activation:
            return "CAPABILITY_DESCRIPTOR_MISMATCH"
        if request.action == "install" and not descriptor.executable:
            return "CAPABILITY_DESCRIPTOR_MISMATCH"
        if descriptor.kind in {CapabilityKind.HOOK, CapabilityKind.PLUGIN} or descriptor.executable:
            return "SUPPLY_CHAIN_NOT_APPROVED"
        return None

    def _admission_reason(
        self,
        request: PermissionRequest,
        decision: AdmissionDecision | None,
    ) -> str | None:
        if decision is None:
            return "ADMISSION_DECISION_REQUIRED"
        if (
            not decision.verify()
            or decision.task_id != request.task_id
            or decision.lane is not request.lane
            or decision.decision_digest != request.admission_digest
        ):
            return "ADMISSION_BINDING_MISMATCH"
        if self._audit_trail is None or not self._audit_trail.has_admission(decision):
            return "AUDIT_FAILED"
        return None

    def _project_root_reason(self, request: PermissionRequest) -> str | None:
        """Reject local work unless the request matches the host-bound root."""

        if request.target_scope not in _LOCAL_TARGET_SCHEMES:
            return None
        if self._project_root is None or request.project_root != self._project_root:
            return "SCOPE_NOT_PERMITTED"
        return None

    def _finish(
        self,
        request: PermissionRequest,
        *,
        status: str,
        reasons: Sequence[str],
        effective: Sequence[PermissionClass],
    ) -> PermissionDecision:
        if request.lane is Lane.DIRECT:
            # DIRECT never activates a capability, so it also never creates an
            # audit event merely to report a prohibited activation attempt.
            return self._decision(
                request,
                status="denied",
                reasons=("DIRECT_CAPABILITY_FORBIDDEN",),
                effective=effective,
            )
        try:
            event_id = self._audit(request, status=status, reasons=reasons)
        except StorageError:
            return self._decision(
                request,
                status="denied",
                reasons=("AUDIT_FAILED",),
                effective=effective,
            )
        return self._decision(
            request,
            status=status,
            reasons=reasons,
            effective=effective,
            audit_event_id=event_id,
        )

    def decide(
        self,
        request: PermissionRequest,
        *,
        parent_permissions: Sequence[PermissionClass | str],
        node_permissions: Sequence[PermissionClass | str],
        admission_decision: AdmissionDecision | None = None,
        capability_descriptor: CapabilityDescriptor | None = None,
        material_authority: MaterialCapabilityAuthority | None = None,
        approval: UserApproval | None = None,
        supply_chain_review: SupplyChainReview | None = None,
        actor_profile: str | None = None,
        task_active: bool = True,
    ) -> PermissionDecision:
        if not isinstance(request, PermissionRequest):
            raise StorageError("SCHEMA_INVALID", "permission request is required")
        if admission_decision is not None and not isinstance(admission_decision, AdmissionDecision):
            raise StorageError("SCHEMA_INVALID", "admission_decision is invalid")
        if capability_descriptor is not None and not isinstance(capability_descriptor, CapabilityDescriptor):
            raise StorageError("SCHEMA_INVALID", "capability_descriptor is invalid")
        if material_authority is not None and not isinstance(material_authority, MaterialCapabilityAuthority):
            raise StorageError("SCHEMA_INVALID", "material_authority is invalid")
        if approval is not None and not isinstance(approval, UserApproval):
            raise StorageError("SCHEMA_INVALID", "approval is invalid")
        if supply_chain_review is not None and not isinstance(supply_chain_review, SupplyChainReview):
            raise StorageError("SCHEMA_INVALID", "supply_chain_review is invalid")
        parent = _permission_tuple(parent_permissions, "parent_permissions")
        node = _permission_tuple(node_permissions, "node_permissions")
        task_active = _bounded_bool(task_active, "task_active")
        capability_permission = (
            capability_descriptor.required_permission
            if capability_descriptor is not None
            else material_authority.permission if material_authority is not None else None
        )
        capability = () if capability_permission is None else (capability_permission,)
        effective = tuple(item for item in parent if item in node and item in capability)
        if request.lane is Lane.DIRECT:
            return self._finish(
                request,
                status="denied",
                reasons=("DIRECT_CAPABILITY_FORBIDDEN",),
                effective=effective,
            )
        admission_reason = self._admission_reason(request, admission_decision)
        if admission_reason is not None:
            return self._finish(
                request,
                status="denied",
                reasons=(admission_reason,),
                effective=effective,
            )
        if not task_active:
            return self._finish(
                request,
                status="denied",
                reasons=("TASK_NOT_ACTIVE",),
                effective=effective,
            )
        if request.permission is not _ACTION_PERMISSIONS[request.action]:
            return self._finish(
                request,
                status="denied",
                reasons=("ACTION_PERMISSION_MISMATCH",),
                effective=effective,
            )
        if request.target_requires_network and request.action in {"local_read", "workspace_write"}:
            return self._finish(
                request,
                status="denied",
                reasons=("ACTION_PERMISSION_MISMATCH",),
                effective=effective,
            )
        if not request.target_requires_network and request.action in {
            "network_read",
            "external_write",
            "install",
            "login",
            "global_activation",
            "permission_expansion",
        }:
            return self._finish(
                request,
                status="denied",
                reasons=("ACTION_PERMISSION_MISMATCH",),
                effective=effective,
            )
        project_root_reason = self._project_root_reason(request)
        if project_root_reason is not None:
            return self._finish(
                request,
                status="denied",
                reasons=(project_root_reason,),
                effective=effective,
            )
        authority_reason = self._capability_authority_reason(
            request,
            capability_descriptor,
            material_authority,
            supply_chain_review,
        )
        if authority_reason is not None:
            return self._finish(
                request,
                status="denied",
                reasons=(authority_reason,),
                effective=effective,
            )
        if actor_profile is not None and _require_text(actor_profile, "actor_profile", maximum=64) in {
            "luna-xhigh",
            "pixel-luna-xhigh",
        }:
            if request.permission is not PermissionClass.LOCAL_READ:
                return self._finish(
                    request,
                    status="denied",
                    reasons=("LUNA_PERMISSION_DENIED",),
                    effective=effective,
                )
        if request.permission not in parent:
            return self._finish(
                request,
                status="denied",
                reasons=("PARENT_PERMISSION_DENIED",),
                effective=effective,
            )
        if request.permission not in node:
            return self._finish(
                request,
                status="approval_required",
                reasons=("NODE_PERMISSION_EXPANSION",),
                effective=effective,
            )
        if request.permission in _HIGH_RISK_PERMISSIONS or request.action in {
            "login",
            "global_activation",
            "permission_expansion",
        }:
            if request.lane is not Lane.DEEP:
                return self._finish(
                    request,
                    status="denied",
                    reasons=("DEEP_REQUIRED",),
                    effective=effective,
                )
        if request.permission is PermissionClass.INSTALL_EXECUTABLE:
            if (
                supply_chain_review is None
                or not supply_chain_review.installable
                or not supply_chain_review.matches(request)
            ):
                return self._finish(
                    request,
                    status="denied",
                    reasons=("SUPPLY_CHAIN_NOT_APPROVED",),
                    effective=effective,
                )
        if request.requires_explicit_approval:
            # A project-local policy library cannot attest a live user gesture.
            # Material actions stay at the boundary until a future host-owned
            # executor adds a separate, reviewable confirmation contract.
            return self._finish(
                request,
                status="approval_required",
                reasons=("USER_APPROVAL_REQUIRED",),
                effective=effective,
            )
        return self._finish(
            request,
            status="granted",
            reasons=("SELF_CONTAINED_LOW_RISK",),
            effective=effective,
        )


@dataclass(frozen=True)
class GatewayContract:
    gateway: Gateway | str
    allowed_permissions: tuple[PermissionClass | str, ...]
    allowed_kinds: tuple[CapabilityKind | str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "gateway", _enum(self.gateway, Gateway, "gateway"))
        object.__setattr__(
            self,
            "allowed_permissions",
            _permission_tuple(self.allowed_permissions, "allowed_permissions"),
        )
        kinds = tuple(_enum(item, CapabilityKind, "allowed_kinds") for item in self.allowed_kinds)
        if not kinds or len(kinds) != len(set(kinds)):
            raise StorageError("SCHEMA_INVALID", "allowed_kinds is invalid")
        object.__setattr__(self, "allowed_kinds", kinds)

    @classmethod
    def from_value(cls, value: GatewayContract | Mapping[str, Any]) -> GatewayContract:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise StorageError("SCHEMA_INVALID", "gateway contract must be an object")
        allowed = {"gateway", "allowed_permissions", "allowed_kinds"}
        if set(value) != allowed:
            raise StorageError("SCHEMA_INVALID", "gateway contract fields are invalid")
        return cls(**dict(value))

    def to_dict(self) -> dict[str, Any]:
        return {
            "gateway": self.gateway.value,
            "allowed_permissions": [item.value for item in self.allowed_permissions],
            "allowed_kinds": [item.value for item in self.allowed_kinds],
        }


GATEWAY_CONTRACTS: Mapping[Gateway, GatewayContract] = MappingProxyType({
    Gateway.ORCHESTRATION: GatewayContract(
        gateway=Gateway.ORCHESTRATION,
        allowed_permissions=(PermissionClass.LOCAL_READ, PermissionClass.WORKSPACE_WRITE),
        allowed_kinds=(CapabilityKind.SKILL, CapabilityKind.HOOK, CapabilityKind.LOCAL_TOOL),
    ),
    Gateway.RESEARCH: GatewayContract(
        gateway=Gateway.RESEARCH,
        allowed_permissions=(PermissionClass.LOCAL_READ, PermissionClass.NETWORK_READ),
        allowed_kinds=(CapabilityKind.SKILL, CapabilityKind.APP, CapabilityKind.MCP, CapabilityKind.LOCAL_TOOL),
    ),
    Gateway.BACKEND: GatewayContract(
        gateway=Gateway.BACKEND,
        allowed_permissions=(PermissionClass.LOCAL_READ, PermissionClass.WORKSPACE_WRITE),
        allowed_kinds=(CapabilityKind.SKILL, CapabilityKind.PLUGIN, CapabilityKind.LOCAL_TOOL),
    ),
    Gateway.FRONTEND: GatewayContract(
        gateway=Gateway.FRONTEND,
        allowed_permissions=(PermissionClass.LOCAL_READ, PermissionClass.WORKSPACE_WRITE),
        allowed_kinds=(CapabilityKind.SKILL, CapabilityKind.PLUGIN, CapabilityKind.LOCAL_TOOL),
    ),
    Gateway.SECURITY: GatewayContract(
        gateway=Gateway.SECURITY,
        allowed_permissions=(PermissionClass.LOCAL_READ, PermissionClass.WORKSPACE_WRITE),
        allowed_kinds=(CapabilityKind.SKILL, CapabilityKind.LOCAL_TOOL),
    ),
    Gateway.MEMORY: GatewayContract(
        gateway=Gateway.MEMORY,
        allowed_permissions=(PermissionClass.LOCAL_READ,),
        allowed_kinds=(CapabilityKind.SKILL, CapabilityKind.LOCAL_TOOL),
    ),
})


@dataclass(frozen=True)
class CapabilityDescriptor:
    """Explicit metadata. Description is untrusted and never participates in matching."""

    identifier: str
    version: str
    source_ref: str
    kind: CapabilityKind | str
    gateway: Gateway | str
    triggers: tuple[str, ...]
    scopes: tuple[str, ...]
    required_permission: PermissionClass | str
    input_contract: str
    output_contract: str
    health: CapabilityHealth | str
    trust: CapabilityTrust | str
    cost: CostClass | str
    latency: CostClass | str
    installed: bool
    optional: bool = False
    fallback_id: str | None = None
    description: str = ""
    requires_login: bool = False
    global_activation: bool = False
    executable: bool = False

    def __post_init__(self) -> None:
        identifier = _identifier(self.identifier)
        version = _require_text(self.version, "version", maximum=128)
        source_ref = _safe_locator(self.source_ref, "source_ref", maximum=256)
        kind = _enum(self.kind, CapabilityKind, "kind")
        gateway = _enum(self.gateway, Gateway, "gateway")
        triggers = _tokens(self.triggers, "triggers")
        scopes = _tokens(self.scopes, "scopes")
        if not triggers or not scopes:
            raise StorageError("SCHEMA_INVALID", "capability triggers and scopes must be non-empty")
        permission = _enum(self.required_permission, PermissionClass, "required_permission")
        input_contract = _require_text(self.input_contract, "input_contract", maximum=128)
        output_contract = _require_text(self.output_contract, "output_contract", maximum=128)
        if _CONTRACT_ID.fullmatch(input_contract) is None or _CONTRACT_ID.fullmatch(output_contract) is None:
            raise StorageError("SCHEMA_INVALID", "capability contract identifier is invalid")
        health = _enum(self.health, CapabilityHealth, "health")
        trust = _enum(self.trust, CapabilityTrust, "trust")
        cost = _enum(self.cost, CostClass, "cost")
        latency = _enum(self.latency, CostClass, "latency")
        installed = _bounded_bool(self.installed, "installed")
        optional = _bounded_bool(self.optional, "optional")
        fallback = None if self.fallback_id is None else _identifier(self.fallback_id, "fallback_id")
        if fallback == identifier:
            raise StorageError("SCHEMA_INVALID", "capability cannot fall back to itself")
        if (
            not isinstance(self.description, str)
            or len(self.description) > 4096
            or "\x00" in self.description
        ):
            raise StorageError("SCHEMA_INVALID", "description must be bounded text")
        requires_login = _bounded_bool(self.requires_login, "requires_login")
        global_activation = _bounded_bool(self.global_activation, "global_activation")
        executable = _bounded_bool(self.executable, "executable")
        contract = GATEWAY_CONTRACTS[gateway]
        if permission not in contract.allowed_permissions or kind not in contract.allowed_kinds:
            raise StorageError("SCHEMA_INVALID", "capability does not fit its gateway contract")
        if (requires_login or global_activation or executable) and kind is CapabilityKind.LOCAL_TOOL:
            raise StorageError("SCHEMA_INVALID", "local tool cannot declare external activation flags")
        object.__setattr__(self, "identifier", identifier)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "source_ref", source_ref)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "gateway", gateway)
        object.__setattr__(self, "triggers", triggers)
        object.__setattr__(self, "scopes", scopes)
        object.__setattr__(self, "required_permission", permission)
        object.__setattr__(self, "input_contract", input_contract)
        object.__setattr__(self, "output_contract", output_contract)
        object.__setattr__(self, "health", health)
        object.__setattr__(self, "trust", trust)
        object.__setattr__(self, "cost", cost)
        object.__setattr__(self, "latency", latency)
        object.__setattr__(self, "installed", installed)
        object.__setattr__(self, "optional", optional)
        object.__setattr__(self, "fallback_id", fallback)
        object.__setattr__(self, "requires_login", requires_login)
        object.__setattr__(self, "global_activation", global_activation)
        object.__setattr__(self, "executable", executable)

    @classmethod
    def from_value(cls, value: CapabilityDescriptor | Mapping[str, Any]) -> CapabilityDescriptor:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise StorageError("SCHEMA_INVALID", "capability descriptor must be an object")
        allowed = {
            "identifier",
            "version",
            "source_ref",
            "kind",
            "gateway",
            "triggers",
            "scopes",
            "required_permission",
            "input_contract",
            "output_contract",
            "health",
            "trust",
            "cost",
            "latency",
            "installed",
            "optional",
            "fallback_id",
            "description",
            "requires_login",
            "global_activation",
            "executable",
        }
        if set(value) - allowed:
            raise StorageError("SCHEMA_INVALID", "capability descriptor contains unknown fields")
        required = {
            "identifier",
            "version",
            "source_ref",
            "kind",
            "gateway",
            "triggers",
            "scopes",
            "required_permission",
            "input_contract",
            "output_contract",
            "health",
            "trust",
            "cost",
            "latency",
            "installed",
        }
        if required - set(value):
            raise StorageError("SCHEMA_INVALID", "capability descriptor is incomplete")
        return cls(
            identifier=value["identifier"],
            version=value["version"],
            source_ref=value["source_ref"],
            kind=value["kind"],
            gateway=value["gateway"],
            triggers=value["triggers"],
            scopes=value["scopes"],
            required_permission=value["required_permission"],
            input_contract=value["input_contract"],
            output_contract=value["output_contract"],
            health=value["health"],
            trust=value["trust"],
            cost=value["cost"],
            latency=value["latency"],
            installed=value["installed"],
            optional=value.get("optional", False),
            fallback_id=value.get("fallback_id"),
            description=value.get("description", ""),
            requires_login=value.get("requires_login", False),
            global_activation=value.get("global_activation", False),
            executable=value.get("executable", False),
        )

    @property
    def reference_digest(self) -> str:
        return _digest(
            {
                "identifier": self.identifier,
                "version": self.version,
                "source_ref_digest": _digest({"source_ref": self.source_ref}),
                "kind": self.kind.value,
                "gateway": self.gateway.value,
                "scopes": list(self.scopes),
                "required_permission": self.required_permission.value,
                "installed": self.installed,
                "health": self.health.value,
                "trust": self.trust.value,
                "requires_login": self.requires_login,
                "global_activation": self.global_activation,
                "executable": self.executable,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "version": self.version,
            "source_ref_digest": _digest({"source_ref": self.source_ref}),
            "kind": self.kind.value,
            "gateway": self.gateway.value,
            "triggers": list(self.triggers),
            "scopes": list(self.scopes),
            "required_permission": self.required_permission.value,
            "input_contract": self.input_contract,
            "output_contract": self.output_contract,
            "health": self.health.value,
            "trust": self.trust.value,
            "cost": self.cost.value,
            "latency": self.latency.value,
            "installed": self.installed,
            "optional": self.optional,
            "fallback_id": self.fallback_id,
            "requires_login": self.requires_login,
            "global_activation": self.global_activation,
            "executable": self.executable,
        }


@dataclass(frozen=True)
class MaterialCapabilityAuthority:
    """A review-bound proposal; only an audit-issued instance is authoritative."""

    identifier: str
    version: str
    permission: PermissionClass | str
    scopes: tuple[str, ...]
    evidence_digest: str
    review_receipt_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "identifier", _identifier(self.identifier))
        object.__setattr__(self, "version", _require_text(self.version, "version", maximum=128))
        permission = _enum(self.permission, PermissionClass, "permission")
        if permission not in _HIGH_RISK_PERMISSIONS:
            raise StorageError("SCHEMA_INVALID", "material authority must bind a material permission")
        object.__setattr__(self, "permission", permission)
        scopes = _tokens(self.scopes, "scopes")
        if not scopes:
            raise StorageError("SCHEMA_INVALID", "material authority scopes must be non-empty")
        object.__setattr__(self, "scopes", scopes)
        object.__setattr__(self, "evidence_digest", _sha256(self.evidence_digest, "evidence_digest"))
        object.__setattr__(
            self,
            "review_receipt_id",
            None
            if self.review_receipt_id is None
            else _opaque_id(self.review_receipt_id, "evt:", "review_receipt_id"),
        )

    @classmethod
    def from_value(
        cls,
        value: MaterialCapabilityAuthority | Mapping[str, Any],
    ) -> MaterialCapabilityAuthority:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping) or set(value) != {
            "identifier",
            "version",
            "permission",
            "scopes",
            "evidence_digest",
            "review_receipt_id",
        }:
            raise StorageError("SCHEMA_INVALID", "material authority fields are invalid")
        return cls(**dict(value))

    @classmethod
    def from_review(
        cls,
        review: "SupplyChainReview",
        *,
        permission: PermissionClass | str,
        scopes: tuple[str, ...],
        audit_trail: AuditTrail,
    ) -> "MaterialCapabilityAuthority":
        """Issue material authority only from an audited lifecycle review receipt."""

        if not isinstance(review, SupplyChainReview) or not review.installable:
            raise StorageError("POLICY_DENIED", "material authority requires an approved supply-chain review")
        if not isinstance(audit_trail, AuditTrail):
            raise StorageError("SCHEMA_INVALID", "audit_trail is invalid")
        receipt = next(
            (
                item
                for item in reversed(tuple(audit_trail._supply_chain_receipts.values()))
                if item.candidate_id == review.candidate_id
                and item.pin_digest == review.pin_digest
                and item.review_handle is review._lineage
            ),
            None,
        )
        if receipt is None:
            raise StorageError("POLICY_DENIED", "material authority requires an audited review receipt")
        authority = cls(
            identifier=review.capability_id,
            version=review.capability_version,
            permission=permission,
            scopes=scopes,
            evidence_digest=review.pin_digest,
            review_receipt_id=receipt.event_id,
        )
        return audit_trail._issue_material_authority(
            authority,
            review,
            _issuer=_MATERIAL_AUTHORITY_ISSUER,
        )

    @property
    def authority_digest(self) -> str:
        return _digest(
            {
                "identifier": self.identifier,
                "version": self.version,
                "permission": self.permission.value,
                "scopes": list(self.scopes),
                "evidence_digest": self.evidence_digest,
                "review_receipt_id": self.review_receipt_id,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "version": self.version,
            "permission": self.permission.value,
            "scopes": list(self.scopes),
            "evidence_digest": self.evidence_digest,
            "review_receipt_id": self.review_receipt_id,
            "authority_digest": self.authority_digest,
        }


class CapabilityRegistry:
    """Explicit caller-provided descriptors; intentionally no discovery API exists."""

    def __init__(self, descriptors: Iterable[CapabilityDescriptor | Mapping[str, Any]] = ()) -> None:
        self._entries: dict[str, CapabilityDescriptor] = {}
        for descriptor in descriptors:
            self.register(descriptor)

    def register(self, descriptor: CapabilityDescriptor | Mapping[str, Any]) -> CapabilityDescriptor:
        value = CapabilityDescriptor.from_value(descriptor)
        if value.identifier in self._entries:
            raise StorageError("SCHEMA_INVALID", "capability identifier is already registered")
        self._entries[value.identifier] = value
        return value

    def get(self, identifier: str) -> CapabilityDescriptor | None:
        return self._entries.get(_identifier(identifier))

    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(self._entries[key] for key in sorted(self._entries))


@dataclass(frozen=True)
class CapabilityRequest:
    task_id: str
    gateways: tuple[Gateway | str, ...] = ()
    intent_tags: tuple[str, ...] = ()
    required_scopes: tuple[str, ...] = ()
    required_ids: tuple[str, ...] = ()
    optional_ids: tuple[str, ...] = ()
    permitted_permissions: tuple[PermissionClass | str, ...] = ()
    forbid_research: bool = False
    forbid_capabilities: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _opaque_id(self.task_id, "task:", "task_id"))
        object.__setattr__(self, "gateways", _gateways(self.gateways, "gateways"))
        object.__setattr__(self, "intent_tags", _tokens(self.intent_tags, "intent_tags"))
        object.__setattr__(self, "required_scopes", _tokens(self.required_scopes, "required_scopes"))
        required = _identifiers(self.required_ids, "required_ids")
        optional = _identifiers(self.optional_ids, "optional_ids")
        if set(required) & set(optional):
            raise StorageError("SCHEMA_INVALID", "required and optional identifiers overlap")
        object.__setattr__(self, "required_ids", required)
        object.__setattr__(self, "optional_ids", optional)
        object.__setattr__(
            self,
            "permitted_permissions",
            _permission_tuple(self.permitted_permissions, "permitted_permissions"),
        )
        object.__setattr__(self, "forbid_research", _bounded_bool(self.forbid_research, "forbid_research"))
        object.__setattr__(
            self,
            "forbid_capabilities",
            _bounded_bool(self.forbid_capabilities, "forbid_capabilities"),
        )

    @classmethod
    def from_value(cls, value: CapabilityRequest | Mapping[str, Any]) -> CapabilityRequest:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise StorageError("SCHEMA_INVALID", "capability request must be an object")
        allowed = {
            "task_id",
            "gateways",
            "intent_tags",
            "required_scopes",
            "required_ids",
            "optional_ids",
            "permitted_permissions",
            "forbid_research",
            "forbid_capabilities",
        }
        if set(value) - allowed or "task_id" not in value:
            raise StorageError("SCHEMA_INVALID", "capability request fields are invalid")
        return cls(
            task_id=value["task_id"],
            gateways=value.get("gateways", ()),
            intent_tags=value.get("intent_tags", ()),
            required_scopes=value.get("required_scopes", ()),
            required_ids=value.get("required_ids", ()),
            optional_ids=value.get("optional_ids", ()),
            permitted_permissions=value.get("permitted_permissions", ()),
            forbid_research=value.get("forbid_research", False),
            forbid_capabilities=value.get("forbid_capabilities", False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "gateways": [item.value for item in self.gateways],
            "intent_tags": list(self.intent_tags),
            "required_scopes": list(self.required_scopes),
            "required_ids": list(self.required_ids),
            "optional_ids": list(self.optional_ids),
            "permitted_permissions": [item.value for item in self.permitted_permissions],
            "forbid_research": self.forbid_research,
            "forbid_capabilities": self.forbid_capabilities,
        }


@dataclass(frozen=True)
class CapabilityResolutionEntry:
    identifier: str
    status: str
    reason_code: str
    fallback_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "identifier", _identifier(self.identifier))
        if self.status not in {"selected", "not_selected", "missing", "blocked"}:
            raise StorageError("SCHEMA_INVALID", "capability resolution status is not supported")
        _reason_codes((self.reason_code,))
        object.__setattr__(
            self,
            "fallback_id",
            None if self.fallback_id is None else _identifier(self.fallback_id, "fallback_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "status": self.status,
            "reason_code": self.reason_code,
            "fallback_id": self.fallback_id,
        }


@dataclass(frozen=True)
class CapabilityResolution:
    selected: tuple[CapabilityResolutionEntry, ...] = ()
    not_selected: tuple[CapabilityResolutionEntry, ...] = ()
    missing: tuple[CapabilityResolutionEntry, ...] = ()
    blocked: tuple[CapabilityResolutionEntry, ...] = ()
    direct_short_circuit: bool = False
    degraded: bool = False

    def __post_init__(self) -> None:
        groups = {
            "selected": self.selected,
            "not_selected": self.not_selected,
            "missing": self.missing,
            "blocked": self.blocked,
        }
        ids: set[str] = set()
        for status, values in groups.items():
            entries = tuple(values)
            if any(not isinstance(item, CapabilityResolutionEntry) for item in entries):
                raise StorageError("SCHEMA_INVALID", "capability resolution entry is invalid")
            if any(item.status != status for item in entries):
                raise StorageError("SCHEMA_INVALID", "capability resolution group does not match status")
            for item in entries:
                if item.identifier in ids:
                    raise StorageError("SCHEMA_INVALID", "capability resolution repeats an identifier")
                ids.add(item.identifier)
            object.__setattr__(self, status, entries)
        object.__setattr__(self, "direct_short_circuit", _bounded_bool(self.direct_short_circuit, "direct_short_circuit"))
        object.__setattr__(self, "degraded", _bounded_bool(self.degraded, "degraded"))
        if self.direct_short_circuit and (ids or self.degraded):
            raise StorageError("SCHEMA_INVALID", "direct resolution cannot contain capability state")

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": [item.to_dict() for item in self.selected],
            "not_selected": [item.to_dict() for item in self.not_selected],
            "missing": [item.to_dict() for item in self.missing],
            "blocked": [item.to_dict() for item in self.blocked],
            "direct_short_circuit": self.direct_short_circuit,
            "degraded": self.degraded,
        }


class CapabilityResolver:
    """Select the minimum explicit local capability set without loading descriptions."""

    def __init__(self, registry: Any, *, audit_trail: AuditTrail | None = None) -> None:
        if audit_trail is not None and not isinstance(audit_trail, AuditTrail):
            raise StorageError("SCHEMA_INVALID", "audit_trail is invalid")
        self._registry = registry
        self._audit_trail = audit_trail

    @staticmethod
    def _eligibility(
        descriptor: CapabilityDescriptor,
        request: CapabilityRequest,
    ) -> str | None:
        if request.forbid_capabilities:
            return "USER_FORBID_CAPABILITIES"
        if request.gateways and descriptor.gateway not in request.gateways:
            return "GATEWAY_NOT_REQUESTED"
        if descriptor.gateway is Gateway.RESEARCH and request.forbid_research:
            return "USER_FORBID_RESEARCH"
        if not descriptor.installed:
            return "NOT_INSTALLED"
        if descriptor.health is not CapabilityHealth.HEALTHY:
            return {
                CapabilityHealth.DEGRADED: "HEALTH_DEGRADED",
                CapabilityHealth.UNAVAILABLE: "HEALTH_UNAVAILABLE",
                CapabilityHealth.QUARANTINED: "HEALTH_QUARANTINED",
                CapabilityHealth.DISABLED: "HEALTH_DISABLED",
            }[descriptor.health]
        if descriptor.trust.value not in _SAFE_TRUST:
            return "TRUST_NOT_PERMITTED"
        if not set(request.required_scopes).issubset(descriptor.scopes):
            return "SCOPE_NOT_PERMITTED"
        if descriptor.required_permission not in request.permitted_permissions:
            return "PERMISSION_NOT_PERMITTED"
        if descriptor.required_permission in _APPROVAL_REQUIRED_PERMISSIONS:
            # Registry resolution cannot turn a metadata match into a material
            # activation. The broker rechecks an exact descriptor at approval.
            return "CAPABILITY_REQUIRES_APPROVAL"
        if descriptor.requires_login or descriptor.global_activation:
            return "CAPABILITY_REQUIRES_APPROVAL"
        if descriptor.kind in {CapabilityKind.HOOK, CapabilityKind.PLUGIN} or descriptor.executable:
            return "SUPPLY_CHAIN_NOT_APPROVED"
        return None

    @staticmethod
    def _rank(descriptor: CapabilityDescriptor, request: CapabilityRequest) -> tuple[int, int, int, int]:
        coverage = len(set(descriptor.triggers) & set(request.intent_tags))
        return (
            -coverage,
            _PERMISSION_ORDER[descriptor.required_permission],
            _COST_ORDER[descriptor.cost],
            _COST_ORDER[descriptor.latency],
        )

    def _audit(
        self,
        *,
        decision: AdmissionDecision,
        request: CapabilityRequest,
        entry: CapabilityResolutionEntry,
        version: str | None = None,
    ) -> None:
        if self._audit_trail is None:
            raise StorageError("AUDIT_FAILED", "non-direct capability resolution requires an audit trail")
        if entry.status == "selected":
            event_type, status = "capability_selected", "selected"
        elif entry.status == "missing":
            event_type, status = "capability_missing", "missing"
        else:
            event_type, status = "capability_blocked", "blocked"
        self._audit_trail.record(
            event_type=event_type,
            task_id=request.task_id,
            lane=decision.lane,
            status=status,
            reason_codes=(entry.reason_code,),
            capability_id=entry.identifier,
            capability_version=version or "unknown",
        )

    def resolve(
        self,
        decision: AdmissionDecision,
        request: CapabilityRequest,
    ) -> CapabilityResolution:
        if not isinstance(decision, AdmissionDecision):
            raise StorageError("SCHEMA_INVALID", "admission decision is required")
        if not isinstance(request, CapabilityRequest):
            raise StorageError("SCHEMA_INVALID", "capability request is required")
        # This early return intentionally precedes any registry metadata access.
        if decision.lane is Lane.DIRECT:
            return CapabilityResolution(direct_short_circuit=True)
        if decision.task_id != request.task_id:
            raise StorageError("POLICY_DENIED", "capability request task does not match admission")
        if not decision.verify():
            raise StorageError("POLICY_DENIED", "capability request admission is invalid")
        if self._audit_trail is None:
            raise StorageError("AUDIT_FAILED", "non-direct capability resolution requires an audit trail")
        if not self._audit_trail.has_admission(decision):
            raise StorageError("AUDIT_FAILED", "non-direct capability resolution lacks admission evidence")
        # Intake-level prohibitions remain authoritative even if a caller omits
        # them while constructing the later resolver request.
        request = replace(
            request,
            forbid_research=request.forbid_research or decision.features.user_forbid_research,
            forbid_capabilities=request.forbid_capabilities or decision.features.user_forbid_capabilities,
        )

        selected: list[CapabilityResolutionEntry] = []
        not_selected: list[CapabilityResolutionEntry] = []
        missing: list[CapabilityResolutionEntry] = []
        blocked: list[CapabilityResolutionEntry] = []

        # A user prohibition is a policy boundary, so avoid even loading a
        # registry that could otherwise contain third-party metadata.
        if request.forbid_capabilities:
            for identifier in request.required_ids:
                entry = CapabilityResolutionEntry(identifier, "blocked", "USER_FORBID_CAPABILITIES")
                blocked.append(entry)
                self._audit(decision=decision, request=request, entry=entry)
            for identifier in request.optional_ids:
                entry = CapabilityResolutionEntry(identifier, "missing", "OPTIONAL_CAPABILITY_UNAVAILABLE")
                missing.append(entry)
                self._audit(decision=decision, request=request, entry=entry)
            for gateway in request.gateways:
                entry = CapabilityResolutionEntry(
                    f"cap:{gateway.value}-blocked", "blocked", "USER_FORBID_CAPABILITIES"
                )
                blocked.append(entry)
                self._audit(decision=decision, request=request, entry=entry)
            if not blocked and not missing:
                entry = CapabilityResolutionEntry("cap:capabilities-blocked", "blocked", "USER_FORBID_CAPABILITIES")
                blocked.append(entry)
                self._audit(decision=decision, request=request, entry=entry)
            return CapabilityResolution(
                missing=tuple(missing),
                blocked=tuple(blocked),
                degraded=bool(missing),
            )

        if not (request.gateways or request.required_ids or request.optional_ids):
            return CapabilityResolution()

        registry = self._registry
        if not hasattr(registry, "descriptors") or not callable(registry.descriptors):
            raise StorageError("SCHEMA_INVALID", "capability registry must expose descriptors")
        descriptors = tuple(registry.descriptors())
        if any(not isinstance(item, CapabilityDescriptor) for item in descriptors):
            raise StorageError("SCHEMA_INVALID", "capability registry returned an invalid descriptor")
        by_id = {item.identifier: item for item in descriptors}
        if len(by_id) != len(descriptors):
            raise StorageError("SCHEMA_INVALID", "capability registry returned duplicate identifiers")
        used_gateways: set[Gateway] = set()
        blocked_required_gateways: set[Gateway] = set()

        def already_reported(identifier: str) -> bool:
            return any(
                entry.identifier == identifier
                for entries in (selected, not_selected, missing, blocked)
                for entry in entries
            )

        def add_candidate(descriptor: CapabilityDescriptor) -> bool:
            reason = self._eligibility(descriptor, request)
            if reason is not None:
                entry = CapabilityResolutionEntry(descriptor.identifier, "blocked", reason, descriptor.fallback_id)
                blocked.append(entry)
                self._audit(decision=decision, request=request, entry=entry, version=descriptor.version)
                return False
            entry = CapabilityResolutionEntry(descriptor.identifier, "selected", "CAPABILITY_SELECTED", descriptor.fallback_id)
            selected.append(entry)
            used_gateways.add(descriptor.gateway)
            self._audit(decision=decision, request=request, entry=entry, version=descriptor.version)
            return True

        def add_optional_fallback(descriptor: CapabilityDescriptor) -> None:
            if descriptor.fallback_id is None:
                return
            fallback = by_id.get(descriptor.fallback_id)
            if fallback is None or already_reported(fallback.identifier):
                return
            add_candidate(fallback)

        for identifier in request.required_ids:
            descriptor = by_id.get(identifier)
            if descriptor is None:
                entry = CapabilityResolutionEntry(identifier, "missing", "REQUIRED_CAPABILITY_MISSING")
                missing.append(entry)
                self._audit(decision=decision, request=request, entry=entry)
                continue
            if not add_candidate(descriptor):
                blocked_required_gateways.add(descriptor.gateway)

        for identifier in request.optional_ids:
            descriptor = by_id.get(identifier)
            if descriptor is None:
                entry = CapabilityResolutionEntry(identifier, "missing", "OPTIONAL_CAPABILITY_UNAVAILABLE")
                missing.append(entry)
                self._audit(decision=decision, request=request, entry=entry)
                continue
            if self._eligibility(descriptor, request) is not None:
                entry = CapabilityResolutionEntry(
                    identifier,
                    "missing",
                    "OPTIONAL_CAPABILITY_UNAVAILABLE",
                    descriptor.fallback_id,
                )
                missing.append(entry)
                self._audit(decision=decision, request=request, entry=entry, version=descriptor.version)
                add_optional_fallback(descriptor)

        for gateway in request.gateways:
            if gateway in used_gateways or gateway in blocked_required_gateways:
                continue
            if gateway is Gateway.RESEARCH and request.forbid_research:
                entry = CapabilityResolutionEntry("cap:research-blocked", "blocked", "USER_FORBID_RESEARCH")
                blocked.append(entry)
                self._audit(decision=decision, request=request, entry=entry)
                continue
            gateway_candidates = [
                item
                for item in descriptors
                if item.gateway is gateway
                and (not request.intent_tags or bool(set(item.triggers) & set(request.intent_tags)))
            ]
            candidates = [item for item in gateway_candidates if self._eligibility(item, request) is None]
            if not candidates and gateway_candidates:
                for item in gateway_candidates:
                    if already_reported(item.identifier):
                        continue
                    reason = self._eligibility(item, request)
                    if reason is None:
                        continue
                    entry = CapabilityResolutionEntry(item.identifier, "blocked", reason, item.fallback_id)
                    blocked.append(entry)
                    self._audit(decision=decision, request=request, entry=entry, version=item.version)
                continue
            if not candidates:
                entry = CapabilityResolutionEntry(
                    f"cap:{gateway.value}-missing",
                    "missing",
                    "REQUIRED_CAPABILITY_MISSING",
                )
                missing.append(entry)
                self._audit(decision=decision, request=request, entry=entry)
                continue
            candidates.sort(key=lambda item: (*self._rank(item, request), item.identifier))
            top = candidates[0]
            top_rank = self._rank(top, request)
            tied = [item for item in candidates if self._rank(item, request) == top_rank]
            if len(tied) > 1:
                entry = CapabilityResolutionEntry(top.identifier, "blocked", "TRIGGER_COLLISION")
                blocked.append(entry)
                self._audit(decision=decision, request=request, entry=entry, version=top.version)
                for item in tied[1:]:
                    not_selected.append(
                        CapabilityResolutionEntry(item.identifier, "not_selected", "TRIGGER_COLLISION_LOWER_RANK")
                    )
                continue
            add_candidate(top)
            for item in candidates[1:]:
                not_selected.append(CapabilityResolutionEntry(item.identifier, "not_selected", "LOWER_RANK"))

        degraded = bool(
            any(item.reason_code == "OPTIONAL_CAPABILITY_UNAVAILABLE" for item in missing)
            or any(item.reason_code.startswith("HEALTH_") for item in blocked)
        )
        return CapabilityResolution(
            selected=tuple(selected),
            not_selected=tuple(not_selected),
            missing=tuple(missing),
            blocked=tuple(blocked),
            degraded=degraded,
        )


_SUPPLY_CHAIN_ISSUER = object()
_MATERIAL_AUTHORITY_ISSUER = object()


class _SupplyChainLineage:
    """Mutable transition witness kept out of serializable review data."""

    def __init__(self, owner: "SupplyChainLifecycle") -> None:
        self.owner = owner
        self.current: tuple[Any, ...] | None = None
        self.pending = False

    def begin(self) -> None:
        if self.pending:
            raise StorageError("AUDIT_FAILED", "supply-chain transition is already in progress")
        self.pending = True

    def abort(self) -> None:
        self.pending = False

    def commit(self, payload: tuple[Any, ...]) -> None:
        if not self.pending:
            raise StorageError("POLICY_DENIED", "supply-chain transition was not authorized")
        self.current = payload
        self.pending = False

    def validate(self, payload: tuple[Any, ...]) -> None:
        if self.pending:
            return
        if self.current != payload:
            raise StorageError("POLICY_DENIED", "supply-chain state may change only through its lifecycle")


class _SupplyChainRegistry:
    """Process-local single-writer ledger for candidate and pinned-package identities."""

    def __init__(self) -> None:
        self.lock = RLock()
        self.lineages: dict[str, _SupplyChainLineage] = {}
        self.identity_lineages: dict[str, _SupplyChainLineage] = {}


_SUPPLY_CHAIN_REGISTRY = _SupplyChainRegistry()


@dataclass(frozen=True)
class SupplyChainReview:
    """Candidate review data; lifecycle-issued states cannot be reset or replayed."""

    candidate_id: str
    capability_id: str
    capability_version: str
    exact_ref: str
    checksum: str | None
    license_id: str | None
    identity_reviewed: bool = False
    maintenance_reviewed: bool = False
    executable_reviewed: bool = False
    network_reviewed: bool = False
    permission_reviewed: bool = False
    security_reviewed: bool = False
    utility_evidence: bool = False
    rollback_reviewed: bool = False
    state: SupplyChainState | str = SupplyChainState.QUARANTINED
    reason_codes: tuple[str, ...] = ()
    reviewed_pin: str | None = None
    _lineage: _SupplyChainLineage | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _opaque_id(self.candidate_id, "candidate:", "candidate_id"))
        object.__setattr__(self, "capability_id", _identifier(self.capability_id))
        object.__setattr__(
            self,
            "capability_version",
            _require_text(self.capability_version, "capability_version", maximum=128),
        )
        object.__setattr__(self, "exact_ref", _safe_locator(self.exact_ref, "exact_ref", maximum=256))
        object.__setattr__(
            self,
            "checksum",
            None if self.checksum is None else _sha256(self.checksum, "checksum"),
        )
        object.__setattr__(self, "license_id", _optional_text(self.license_id, "license_id", maximum=128))
        for name in (
            "identity_reviewed",
            "maintenance_reviewed",
            "executable_reviewed",
            "network_reviewed",
            "permission_reviewed",
            "security_reviewed",
            "utility_evidence",
            "rollback_reviewed",
        ):
            object.__setattr__(self, name, _bounded_bool(getattr(self, name), name))
        state = _enum(self.state, SupplyChainState, "state")
        reasons = () if not self.reason_codes else _reason_codes(self.reason_codes)
        reviewed_pin = None if self.reviewed_pin is None else _sha256(self.reviewed_pin, "reviewed_pin")
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "reviewed_pin", reviewed_pin)
        if state is SupplyChainState.APPROVED_PINNED:
            if reasons or self._missing_review_reasons() or reviewed_pin != self.pin_digest:
                raise StorageError("SCHEMA_INVALID", "approved candidate is not bound to complete reviewed evidence")
        elif state is SupplyChainState.APPROVED_UPDATE_AVAILABLE:
            if reasons != ("REVIEW_REQUIRED_FOR_UPDATE",) or reviewed_pin is not None:
                raise StorageError("SCHEMA_INVALID", "update candidate must require a new review")
        elif state is SupplyChainState.REJECTED:
            if not reasons or reviewed_pin is not None:
                raise StorageError("SCHEMA_INVALID", "rejected candidate requires a terminal reason")
        elif reviewed_pin is not None:
            raise StorageError("SCHEMA_INVALID", "only an approved pin may retain reviewed evidence")
        if self._lineage is None:
            if state is not SupplyChainState.QUARANTINED or reasons or reviewed_pin is not None:
                raise StorageError("POLICY_DENIED", "only the lifecycle may issue a reviewed candidate state")
        elif not isinstance(self._lineage, _SupplyChainLineage):
            raise StorageError("SCHEMA_INVALID", "supply-chain lifecycle handle is invalid")
        else:
            self._lineage.validate(self._lifecycle_payload())

    @classmethod
    def from_value(cls, value: "SupplyChainReview" | Mapping[str, Any]) -> "SupplyChainReview":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise StorageError("SCHEMA_INVALID", "supply chain review must be an object")
        allowed = {
            "candidate_id",
            "capability_id",
            "capability_version",
            "exact_ref",
            "checksum",
            "license_id",
            "identity_reviewed",
            "maintenance_reviewed",
            "executable_reviewed",
            "network_reviewed",
            "permission_reviewed",
            "security_reviewed",
            "utility_evidence",
            "rollback_reviewed",
            "state",
            "reason_codes",
            "reviewed_pin",
        }
        required = {
            "candidate_id",
            "capability_id",
            "capability_version",
            "exact_ref",
            "checksum",
            "license_id",
        }
        if set(value) - allowed or required - set(value):
            raise StorageError("SCHEMA_INVALID", "supply chain review fields are invalid")
        return cls(**dict(value))

    def _lifecycle_payload(self) -> tuple[Any, ...]:
        return (
            self.candidate_id,
            self.capability_id,
            self.capability_version,
            self.exact_ref,
            self.checksum,
            self.license_id,
            self.identity_reviewed,
            self.maintenance_reviewed,
            self.executable_reviewed,
            self.network_reviewed,
            self.permission_reviewed,
            self.security_reviewed,
            self.utility_evidence,
            self.rollback_reviewed,
            self.state,
            self.reason_codes,
            self.reviewed_pin,
        )

    def _missing_review_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.license_id:
            reasons.append("LICENSE_MISSING")
        elif self.license_id not in _COMPATIBLE_LICENSES:
            reasons.append("LICENSE_INCOMPATIBLE")
        if _IMMUTABLE_REFERENCE.fullmatch(self.exact_ref) is None or self.checksum is None:
            reasons.append("IMMUTABLE_PIN_REQUIRED")
        for field_name, reason in (
            ("identity_reviewed", "IDENTITY_REVIEW_REQUIRED"),
            ("maintenance_reviewed", "MAINTENANCE_REVIEW_REQUIRED"),
            ("executable_reviewed", "EXECUTABLE_REVIEW_REQUIRED"),
            ("network_reviewed", "NETWORK_REVIEW_REQUIRED"),
            ("permission_reviewed", "PERMISSION_REVIEW_REQUIRED"),
            ("security_reviewed", "SECURITY_REVIEW_REQUIRED"),
            ("utility_evidence", "UTILITY_EVIDENCE_REQUIRED"),
            ("rollback_reviewed", "ROLLBACK_REVIEW_REQUIRED"),
        ):
            if not getattr(self, field_name):
                reasons.append(reason)
        return tuple(reasons)

    @property
    def pin_digest(self) -> str:
        return _digest(
            {
                "candidate_id": self.candidate_id,
                "capability_id": self.capability_id,
                "capability_version": self.capability_version,
                "exact_ref": self.exact_ref,
                "checksum": self.checksum,
            }
        )

    @property
    def package_identity_digest(self) -> str:
        """Identify immutable candidate bytes independently of a review UUID."""

        return _digest(
            {
                "capability_id": self.capability_id,
                "capability_version": self.capability_version,
                "exact_ref": self.exact_ref,
                "checksum": self.checksum,
            }
        )

    @property
    def _is_lifecycle_issued(self) -> bool:
        return self._lineage is not None and self._lineage.owner._is_current(self)

    @property
    def installable(self) -> bool:
        return (
            self._is_lifecycle_issued
            and self.state is SupplyChainState.APPROVED_PINNED
            and not self.reason_codes
            and self.reviewed_pin == self.pin_digest
        )

    def evaluate(self) -> "SupplyChainReview":
        if self._lineage is None:
            raise StorageError("POLICY_DENIED", "supply-chain evaluation requires a lifecycle")
        return self._lineage.owner.evaluate(self)

    def mark_update_available(self) -> "SupplyChainReview":
        if self._lineage is None:
            raise StorageError("POLICY_DENIED", "supply-chain transition requires a lifecycle")
        return self._lineage.owner.mark_update_available(self)

    def review(
        self,
        *,
        task_id: str,
        lane: Lane | str,
        audit_trail: AuditTrail | None = None,
    ) -> "SupplyChainReview":
        """Emit an audited receipt for a lifecycle-issued candidate state."""

        lane_value = _enum(lane, Lane, "lane")
        if lane_value is Lane.DIRECT:
            raise StorageError("POLICY_DENIED", "supply-chain review is not available on DIRECT")
        if audit_trail is None:
            raise StorageError("AUDIT_FAILED", "supply-chain review requires an audit trail")
        if not isinstance(audit_trail, AuditTrail):
            raise StorageError("SCHEMA_INVALID", "audit_trail is invalid")
        result = self.evaluate()
        audit_trail._record_supply_chain_review(
            result,
            task_id=task_id,
            lane=lane_value,
            _issuer=_SUPPLY_CHAIN_ISSUER,
        )
        return result

    def matches(self, request: PermissionRequest) -> bool:
        return (
            self.installable
            and self.reviewed_pin == self.pin_digest
            and request.capability_id == self.capability_id
            and request.capability_version == self.capability_version
            and request.candidate_pin == self.pin_digest
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "package_identity_digest": self.package_identity_digest,
            "capability_ref": _digest(
                {"capability_id": self.capability_id, "capability_version": self.capability_version}
            ),
            "pin_digest": self.pin_digest,
            "state": self.state.value,
            "reason_codes": list(self.reason_codes),
        }


class SupplyChainLifecycle:
    """Single-writer transition authority over the process-local candidate ledger."""

    def __init__(self) -> None:
        self._lineages = _SUPPLY_CHAIN_REGISTRY.lineages
        self._identity_lineages = _SUPPLY_CHAIN_REGISTRY.identity_lineages

    def _is_current(self, review: SupplyChainReview) -> bool:
        with _SUPPLY_CHAIN_REGISTRY.lock:
            lineage = review._lineage
            return (
                isinstance(lineage, _SupplyChainLineage)
                and lineage.owner is self
                and self._lineages.get(review.candidate_id) is lineage
                and self._identity_lineages.get(review.package_identity_digest) is lineage
                and lineage.current == review._lifecycle_payload()
                and not lineage.pending
            )

    def _transition(
        self,
        review: SupplyChainReview,
        *,
        state: SupplyChainState,
        reason_codes: tuple[str, ...],
        reviewed_pin: str | None,
        lineage: _SupplyChainLineage,
    ) -> SupplyChainReview:
        with _SUPPLY_CHAIN_REGISTRY.lock:
            existing = self._lineages.get(review.candidate_id)
            if existing is not None and existing is not lineage:
                raise StorageError("POLICY_DENIED", "candidate must receive a new identity for another review")
            existing_identity = self._identity_lineages.get(review.package_identity_digest)
            if existing_identity is not None and existing_identity is not lineage:
                raise StorageError("POLICY_DENIED", "pinned candidate package requires a new review identity")
            lineage.begin()
            try:
                result = replace(
                    review,
                    state=state,
                    reason_codes=reason_codes,
                    reviewed_pin=reviewed_pin,
                    _lineage=lineage,
                )
            except Exception:
                lineage.abort()
                raise
            lineage.commit(result._lifecycle_payload())
            self._lineages[result.candidate_id] = lineage
            self._identity_lineages[result.package_identity_digest] = lineage
            return result

    def evaluate(self, review: SupplyChainReview) -> SupplyChainReview:
        if not isinstance(review, SupplyChainReview):
            raise StorageError("SCHEMA_INVALID", "supply-chain review is invalid")
        with _SUPPLY_CHAIN_REGISTRY.lock:
            if review._lineage is None:
                if review.state is not SupplyChainState.QUARANTINED or review.reason_codes or review.reviewed_pin is not None:
                    raise StorageError("POLICY_DENIED", "only a new quarantined candidate may enter a lifecycle")
                if (
                    review.candidate_id in self._lineages
                    or review.package_identity_digest in self._identity_lineages
                ):
                    raise StorageError("POLICY_DENIED", "candidate must receive a new identity for another review")
                lineage = _SupplyChainLineage(self)
                reasons = review._missing_review_reasons()
                return self._transition(
                    review,
                    state=SupplyChainState.APPROVED_PINNED if not reasons else SupplyChainState.QUARANTINED,
                    reason_codes=reasons,
                    reviewed_pin=review.pin_digest if not reasons else None,
                    lineage=lineage,
                )
            if not self._is_current(review):
                raise StorageError("POLICY_DENIED", "supply-chain review is not current in this lifecycle")
            return review

    def reject(self, review: SupplyChainReview, *, reason_codes: Sequence[str]) -> SupplyChainReview:
        with _SUPPLY_CHAIN_REGISTRY.lock:
            if not self._is_current(review) or review.state is not SupplyChainState.QUARANTINED:
                raise StorageError("POLICY_DENIED", "only the current quarantined candidate may be rejected")
            reasons = _reason_codes(tuple(reason_codes))
            if not reasons:
                raise StorageError("SCHEMA_INVALID", "rejection requires a reason")
            return self._transition(
                review,
                state=SupplyChainState.REJECTED,
                reason_codes=reasons,
                reviewed_pin=None,
                lineage=review._lineage,
            )

    def mark_update_available(self, review: SupplyChainReview) -> SupplyChainReview:
        with _SUPPLY_CHAIN_REGISTRY.lock:
            if not self._is_current(review) or review.state is not SupplyChainState.APPROVED_PINNED:
                raise StorageError("POLICY_DENIED", "only the current approved pin may report an update")
            return self._transition(
                review,
                state=SupplyChainState.APPROVED_UPDATE_AVAILABLE,
                reason_codes=("REVIEW_REQUIRED_FOR_UPDATE",),
                reviewed_pin=None,
                lineage=review._lineage,
            )


__all__ = (
    "ADMISSION_VERSION",
    "AdmissionDecision",
    "AdmissionEngine",
    "AdmissionFeatures",
    "AuditEvent",
    "AuditTrail",
    "CapabilityDescriptor",
    "CapabilityHealth",
    "CapabilityKind",
    "CapabilityRegistry",
    "CapabilityRequest",
    "CapabilityResolution",
    "CapabilityResolutionEntry",
    "CapabilityResolver",
    "CapabilityTrust",
    "CostClass",
    "GATEWAY_CONTRACTS",
    "Gateway",
    "GatewayContract",
    "Lane",
    "MaterialCapabilityAuthority",
    "PermissionBroker",
    "PermissionClass",
    "PermissionDecision",
    "PermissionRequest",
    "SupplyChainLifecycle",
    "SupplyChainReview",
    "SupplyChainState",
    "TaskFeatures",
    "UserApproval",
    "admit",
    "escalate_admission",
)
