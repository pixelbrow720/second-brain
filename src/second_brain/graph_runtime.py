"""Project-local, fail-closed runtime for bounded synthetic work graphs.

M7 models coordination only.  A runner receives an immutable context and may
return an uncommitted proposal.  This module never opens a store, invokes a
tool, launches a shell, contacts a provider, or grants a material side effect.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
import math
import re
from pathlib import Path
from threading import Event, RLock, local
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
import uuid

from .admission import AdmissionDecision, AuditTrail, Lane, PermissionClass
from .canonical import canonical_jcs_bytes, sha256_bytes, sha256_hex
from .contracts import load_schema
from .errors import SemanticValidationError
from .jsonio import load_strict_json
from .profiles import (
    APPROVED_PROFILE_ALIASES,
    load_profile_registry,
    resolve_profile,
    validate_profile_registry,
)
from .routing import (
    ReconciliationStatus,
    RouteReceipt,
    create_route_intent,
    create_route_receipt,
    reconcile_route,
    serialize_route,
    side_effect_allowed,
    synthetic_observation,
)
from .schema_validation import validate_json_schema
from .workspace import resolve_workspace_path


WORK_GRAPH_VERSION = 1
MAX_GRAPH_NODES = 12
MAX_GRAPH_CONCURRENCY = 4
MAX_WORKER_CONTEXT_TOKENS = 16_000
MAX_WORKER_RESULT_TOKENS = 2_000
MAX_JOIN_RESULT_TOKENS = 4_000
MAX_WORKER_REFERENCES = 32
MAX_JOIN_REFERENCES = 48

_TASK_ID = re.compile(
    r"^task:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_NODE_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_OPAQUE_ID = re.compile(r"^[a-z][a-z0-9._:-]{0,95}$")
_ARTIFACT_ID = re.compile(r"^artifact:[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_LABEL = re.compile(r"^[a-z][a-z0-9._:-]{0,95}$")
_REASON = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SCOPE_ALLOWED = re.compile(r"^[A-Za-z0-9._/-]{1,256}$")
_SENSITIVE_MARKER = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer\s+|cookie|password|private\s+key|"
    r"secret|token[=:]|session[=_-]?|ignore\s+previous|system\s+prompt)"
)

_REROUTE_EDGES = {
    "luna-xhigh": "tera-high",
    "tera-high": "tera-xhigh",
    "tera-xhigh": "tera-max",
    "sol-xhigh": "sol-max",
}
# A retry is an availability recovery only. Policy, scope, route, and
# cancellation outcomes must remain terminal and visible to the root.
_TRANSIENT_FAILURE_REASONS = frozenset(
    (
        "LOCAL_SYNTHETIC_TIMEOUT",
        "PROVIDER_5XX",
        "MCP_TEMPORARY_UNAVAILABLE",
        "TEMPORARY_UNAVAILABLE",
    )
)
_GRAPH_EVENT_TYPES = frozenset(
    (
        "graph_validated",
        "node_ready",
        "node_started",
        "node_finished",
        "node_blocked",
        "node_retry_scheduled",
        "node_rerouted",
        "graph_cancel_requested",
        "serial_fallback",
        "artifact_compacted",
        "graph_closed",
    )
)
_EVENT_REASONS = frozenset(
    (
        "GRAPH_VALIDATED",
        "NODE_READY",
        "NODE_STARTED",
        "NODE_SUCCEEDED",
        "NODE_FAILED",
        "BLOCKED_DEPENDENCY",
        "RESULT_INVALID",
        "RESULT_SCOPE_DENIED",
        "ROUTE_QUARANTINED",
        "RUNNER_EXCEPTION",
        "TRANSIENT_RETRY",
        "REROUTED",
        "ATTEMPTS_EXHAUSTED",
        "CANCELLED_USER_REDIRECT",
        "TIMED_OUT",
        "SERIAL_FALLBACK",
        "ARTIFACT_COMPACTED",
        "GRAPH_SUCCEEDED",
        "GRAPH_FAILED",
        "GRAPH_BLOCKED",
        "GRAPH_CANCELLED",
        "INTEGRATION_INCOMPLETE",
        "AUDIT_SINK_FAILED",
        "POLICY_DENIED",
    )
)
_RUNTIME_LOCAL = local()
# One process-local lease prevents a callback, including one that starts a new
# thread, from creating a nested graph while a root graph is still active.
_GRAPH_RUN_GUARD = RLock()
_ACTIVE_GRAPH_RUNS = 0
_ACTIVE_WORKERS = 0
_CANCELLATION_GUARD = RLock()
_CANCELLATION_HANDLES: dict[object, tuple[Event, Event]] = {}


def _reserve_root_graph_run() -> None:
    global _ACTIVE_GRAPH_RUNS
    with _GRAPH_RUN_GUARD:
        if _ACTIVE_GRAPH_RUNS or _ACTIVE_WORKERS:
            raise GraphPolicyError("a graph runtime may not start while a graph worker is active")
        _ACTIVE_GRAPH_RUNS += 1


def _release_root_graph_run() -> None:
    global _ACTIVE_GRAPH_RUNS
    with _GRAPH_RUN_GUARD:
        if _ACTIVE_GRAPH_RUNS != 1:
            raise GraphPolicyError("graph runtime lease accounting failed closed")
        _ACTIVE_GRAPH_RUNS = 0


def _enter_graph_worker() -> None:
    global _ACTIVE_WORKERS
    with _GRAPH_RUN_GUARD:
        if _ACTIVE_GRAPH_RUNS != 1:
            raise GraphPolicyError("graph worker started without an active root graph")
        _ACTIVE_WORKERS += 1


def _leave_graph_worker() -> None:
    global _ACTIVE_WORKERS
    with _GRAPH_RUN_GUARD:
        if _ACTIVE_WORKERS < 1:
            raise GraphPolicyError("graph worker lease accounting failed closed")
        _ACTIVE_WORKERS -= 1


def _register_cancellation_handle(run_cancel: Event, attempt_cancel: Event) -> object:
    handle = object()
    with _CANCELLATION_GUARD:
        _CANCELLATION_HANDLES[handle] = (run_cancel, attempt_cancel)
    return handle


def _retire_cancellation_handle(handle: object) -> None:
    with _CANCELLATION_GUARD:
        _CANCELLATION_HANDLES.pop(handle, None)


def _cancellation_requested(handle: object) -> bool:
    with _CANCELLATION_GUARD:
        events = _CANCELLATION_HANDLES.get(handle)
        return events is None or events[0].is_set() or events[1].is_set()


def _wait_for_cancellation(handle: object, timeout: float | None) -> bool:
    if _cancellation_requested(handle):
        return True
    if timeout is not None and timeout <= 0:
        return _cancellation_requested(handle)
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        with _CANCELLATION_GUARD:
            events = _CANCELLATION_HANDLES.get(handle)
            if events is None or events[0].is_set() or events[1].is_set():
                return True
            run_cancel = events[0]
        remaining = 0.01 if deadline is None else min(0.01, deadline - time.monotonic())
        if remaining <= 0:
            return _cancellation_requested(handle)
        run_cancel.wait(remaining)


class GraphValidationError(SemanticValidationError):
    """A manifest, result, or context violates the bounded M7 contract."""


class GraphPolicyError(SemanticValidationError):
    """A required admission, audit, route, or local-only policy gate failed."""


class NodeRole(str, Enum):
    WORKER = "worker"
    REVIEWER = "reviewer"
    INTEGRATOR = "integrator"


class NodeState(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    QUARANTINED_ROUTE = "quarantined_route"


class GraphState(str, Enum):
    NEW = "new"
    VALIDATED = "validated"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class ResultStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TRANSIENT_FAILURE = "transient_failure"
    REROUTE_REQUIRED = "reroute_required"
    CANCELLED = "cancelled"


_TERMINAL_NODE_STATES = frozenset(
    (
        NodeState.SUCCEEDED,
        NodeState.FAILED,
        NodeState.BLOCKED,
        NodeState.CANCELLED,
        NodeState.TIMED_OUT,
        NodeState.QUARANTINED_ROUTE,
    )
)
_NODE_TRANSITIONS: dict[NodeState, frozenset[NodeState]] = {
    NodeState.PENDING: frozenset((NodeState.READY, NodeState.BLOCKED, NodeState.CANCELLED)),
    NodeState.READY: frozenset(
        (NodeState.RUNNING, NodeState.BLOCKED, NodeState.CANCELLED, NodeState.QUARANTINED_ROUTE)
    ),
    NodeState.RUNNING: frozenset(
        (
            NodeState.SUCCEEDED,
            NodeState.FAILED,
            NodeState.CANCELLED,
            NodeState.TIMED_OUT,
            NodeState.RETRY_WAIT,
        )
    ),
    NodeState.RETRY_WAIT: frozenset((NodeState.PENDING, NodeState.CANCELLED, NodeState.BLOCKED)),
    NodeState.SUCCEEDED: frozenset(),
    NodeState.FAILED: frozenset(),
    NodeState.BLOCKED: frozenset(),
    NodeState.CANCELLED: frozenset(),
    NodeState.TIMED_OUT: frozenset(),
    NodeState.QUARANTINED_ROUTE: frozenset(),
}


def _require_text(
    value: Any,
    field_name: str,
    *,
    pattern: re.Pattern[str] | None = None,
    minimum: int = 1,
    maximum: int = 512,
) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise GraphValidationError(f"{field_name} must be bounded text")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise GraphValidationError(f"{field_name} contains unsafe control characters")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise GraphValidationError(f"{field_name} has an invalid format")
    return value


def _require_integer(value: Any, field_name: str, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise GraphValidationError(f"{field_name} is outside its bounded integer range")
    return value


def _as_tuple(value: Any, field_name: str, *, maximum: int) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise GraphValidationError(f"{field_name} must be an array")
    result = tuple(value)
    if len(result) > maximum:
        raise GraphValidationError(f"{field_name} exceeds its item limit")
    return result


def _unique_strings(
    value: Any,
    field_name: str,
    *,
    maximum: int,
    pattern: re.Pattern[str],
    minimum: int = 0,
) -> tuple[str, ...]:
    items = _as_tuple(value, field_name, maximum=maximum)
    if len(items) < minimum:
        raise GraphValidationError(f"{field_name} needs at least {minimum} item(s)")
    normalized = tuple(
        _require_text(item, field_name, pattern=pattern, maximum=96) for item in items
    )
    if len(set(normalized)) != len(normalized):
        raise GraphValidationError(f"{field_name} contains duplicates")
    return normalized


def _safe_scope(value: Any, field_name: str, *, allow_directory: bool = True) -> str:
    scope = _require_text(value, field_name, maximum=256)
    if _SCOPE_ALLOWED.fullmatch(scope) is None:
        raise GraphValidationError(f"{field_name} is not a safe repository-relative scope")
    if (
        scope.startswith(("/", "~"))
        or "\\" in scope
        or "//" in scope
        or "%" in scope
        or "$" in scope
        or any(symbol in scope for symbol in ("*", "?", "[", "]", "{", "}", ":"))
    ):
        raise GraphValidationError(f"{field_name} is not a safe repository-relative scope")
    directory = scope.endswith("/")
    if directory and not allow_directory:
        raise GraphValidationError(f"{field_name} must name a file")
    stripped = scope[:-1] if directory else scope
    if not stripped:
        raise GraphValidationError(f"{field_name} may not name the repository root")
    parts = stripped.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise GraphValidationError(f"{field_name} contains an unsafe path segment")
    return scope


def _scope_parts(scope: str) -> tuple[str, ...]:
    return tuple(scope.rstrip("/").split("/"))


def _scope_overlaps(left: str, right: str) -> bool:
    left_parts = _scope_parts(left)
    right_parts = _scope_parts(right)
    common = min(len(left_parts), len(right_parts))
    return left_parts[:common] == right_parts[:common]


def _scope_contains(scope: str, target: str) -> bool:
    scope_parts = _scope_parts(scope)
    target_parts = _scope_parts(target)
    if not scope.endswith("/"):
        return scope_parts == target_parts
    return len(scope_parts) <= len(target_parts) and target_parts[: len(scope_parts)] == scope_parts


def _estimated_tokens(value: Any) -> int:
    return math.ceil(len(canonical_jcs_bytes(value)) / 4)


def _digest_task_id(task_id: str) -> str:
    return sha256_hex({"task_id": task_id})


def _timestamp(clock: Any) -> str:
    if clock is not None:
        candidate = getattr(clock, "now_rfc3339", None)
        if callable(candidate):
            value = candidate()
            if isinstance(value, str):
                return value
        candidate = getattr(clock, "now", None)
        if callable(candidate):
            value = candidate()
            if isinstance(value, datetime):
                return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _contains_sensitive_marker(value: str | bytes) -> bool:
    try:
        text = value.decode("utf-8", "strict") if isinstance(value, bytes) else value
    except UnicodeDecodeError:
        return True
    return _SENSITIVE_MARKER.search(text) is not None


@dataclass(frozen=True)
class WorkGraphNode:
    """One declarative root-owned worker contract; it carries no executor."""

    id: str
    role: NodeRole | str
    task: str
    profile: str
    depends_on: tuple[str, ...] | Sequence[str]
    read_scope: tuple[str, ...] | Sequence[str]
    write_scope: tuple[str, ...] | Sequence[str]
    capabilities: tuple[str, ...] | Sequence[str]
    expected_artifacts: tuple[str, ...] | Sequence[str]
    acceptance: tuple[str, ...] | Sequence[str]
    timeout_seconds: int
    max_attempts: int
    permission_class: PermissionClass | str
    reroute_to: str | None

    _FIELDS = frozenset(
        (
            "id",
            "role",
            "task",
            "profile",
            "depends_on",
            "read_scope",
            "write_scope",
            "capabilities",
            "expected_artifacts",
            "acceptance",
            "timeout_seconds",
            "max_attempts",
            "permission_class",
            "reroute_to",
        )
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "node id", pattern=_NODE_ID, maximum=64))
        try:
            role = self.role if isinstance(self.role, NodeRole) else NodeRole(self.role)
        except (TypeError, ValueError) as error:
            raise GraphValidationError("node role is invalid") from error
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "task", _require_text(self.task, "node task", maximum=512))
        object.__setattr__(
            self,
            "profile",
            _require_text(self.profile, "node profile", pattern=_SAFE_LABEL, maximum=96),
        )
        object.__setattr__(
            self,
            "depends_on",
            _unique_strings(self.depends_on, "node dependencies", maximum=11, pattern=_NODE_ID),
        )
        read_scope = tuple(_safe_scope(item, "read scope") for item in _as_tuple(self.read_scope, "read scope", maximum=16))
        write_scope = tuple(_safe_scope(item, "write scope") for item in _as_tuple(self.write_scope, "write scope", maximum=16))
        if len(set(read_scope)) != len(read_scope) or len(set(write_scope)) != len(write_scope):
            raise GraphValidationError("node scopes contain duplicates")
        object.__setattr__(self, "read_scope", read_scope)
        object.__setattr__(self, "write_scope", write_scope)
        object.__setattr__(
            self,
            "capabilities",
            _unique_strings(
                self.capabilities,
                "node capabilities",
                maximum=8,
                pattern=re.compile(r"^cap:[a-z0-9][a-z0-9._-]{0,63}$"),
            ),
        )
        object.__setattr__(
            self,
            "expected_artifacts",
            _unique_strings(
                self.expected_artifacts,
                "expected artifacts",
                maximum=16,
                minimum=1,
                pattern=_ARTIFACT_ID,
            ),
        )
        object.__setattr__(
            self,
            "acceptance",
            _unique_strings(
                self.acceptance,
                "acceptance checks",
                maximum=16,
                minimum=1,
                pattern=_SAFE_LABEL,
            ),
        )
        object.__setattr__(
            self,
            "timeout_seconds",
            _require_integer(self.timeout_seconds, "timeout_seconds", minimum=30, maximum=900),
        )
        object.__setattr__(
            self,
            "max_attempts",
            _require_integer(self.max_attempts, "max_attempts", minimum=1, maximum=2),
        )
        try:
            permission = (
                self.permission_class
                if isinstance(self.permission_class, PermissionClass)
                else PermissionClass(self.permission_class)
            )
        except (TypeError, ValueError) as error:
            raise GraphValidationError("node permission class is invalid") from error
        if permission not in {PermissionClass.LOCAL_READ, PermissionClass.WORKSPACE_WRITE}:
            raise GraphValidationError("M7 supports only local-read or workspace-write declarations")
        if permission is PermissionClass.LOCAL_READ and self.write_scope:
            raise GraphValidationError("local-read node cannot declare a write scope")
        if permission is PermissionClass.WORKSPACE_WRITE and not self.write_scope:
            raise GraphValidationError("workspace-write node requires a declared proposal scope")
        object.__setattr__(self, "permission_class", permission)
        if self.reroute_to is not None:
            object.__setattr__(
                self,
                "reroute_to",
                _require_text(self.reroute_to, "reroute target", pattern=_SAFE_LABEL, maximum=96),
            )

    @classmethod
    def from_value(cls, value: WorkGraphNode | Mapping[str, Any]) -> WorkGraphNode:
        if type(value) is cls:
            return cls(
                id=value.id,
                role=value.role,
                task=value.task,
                profile=value.profile,
                depends_on=value.depends_on,
                read_scope=value.read_scope,
                write_scope=value.write_scope,
                capabilities=value.capabilities,
                expected_artifacts=value.expected_artifacts,
                acceptance=value.acceptance,
                timeout_seconds=value.timeout_seconds,
                max_attempts=value.max_attempts,
                permission_class=value.permission_class,
                reroute_to=value.reroute_to,
            )
        if isinstance(value, cls):
            raise GraphValidationError("work graph node subclasses are not accepted")
        if not isinstance(value, Mapping) or set(value) != cls._FIELDS:
            raise GraphValidationError("work graph node fields are invalid")
        return cls(**deepcopy(dict(value)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role.value,
            "task": self.task,
            "profile": self.profile,
            "depends_on": list(self.depends_on),
            "read_scope": list(self.read_scope),
            "write_scope": list(self.write_scope),
            "capabilities": list(self.capabilities),
            "expected_artifacts": list(self.expected_artifacts),
            "acceptance": list(self.acceptance),
            "timeout_seconds": self.timeout_seconds,
            "max_attempts": self.max_attempts,
            "permission_class": self.permission_class.value,
            "reroute_to": self.reroute_to,
        }

    def redacted_contract(self) -> dict[str, Any]:
        """Return a stable binding that excludes task text and repository paths."""

        return {
            "id": self.id,
            "role": self.role.value,
            "task_length": len(self.task),
            "profile": self.profile,
            "depends_on": list(self.depends_on),
            "read_scope_digests": [sha256_hex({"scope": item}) for item in self.read_scope],
            "write_scope_digests": [sha256_hex({"scope": item}) for item in self.write_scope],
            "capabilities": list(self.capabilities),
            "expected_artifacts": list(self.expected_artifacts),
            "acceptance": list(self.acceptance),
            "timeout_seconds": self.timeout_seconds,
            "max_attempts": self.max_attempts,
            "permission_class": self.permission_class.value,
            "reroute_to": self.reroute_to,
        }


@dataclass(frozen=True)
class WorkGraphManifest:
    """Frozen V1 graph data.  Runtime lifecycle state is intentionally absent."""

    version: int
    task_id: str
    lane: Lane | str
    goal: str
    max_concurrency: int
    nodes: tuple[WorkGraphNode, ...] | Sequence[WorkGraphNode | Mapping[str, Any]]
    final_node: str
    manifest_digest: str = field(default="")

    _FIELDS = frozenset(
        ("version", "task_id", "lane", "goal", "max_concurrency", "nodes", "final_node")
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "version",
            _require_integer(self.version, "work graph version", minimum=WORK_GRAPH_VERSION, maximum=WORK_GRAPH_VERSION),
        )
        object.__setattr__(self, "task_id", _require_text(self.task_id, "task id", pattern=_TASK_ID, maximum=41))
        try:
            lane = self.lane if isinstance(self.lane, Lane) else Lane(self.lane)
        except (TypeError, ValueError) as error:
            raise GraphValidationError("work graph lane is invalid") from error
        if lane not in {Lane.GRAPH, Lane.DEEP}:
            raise GraphValidationError("DIRECT and ASSISTED may not create a work graph")
        object.__setattr__(self, "lane", lane)
        object.__setattr__(self, "goal", _require_text(self.goal, "graph goal", maximum=512))
        object.__setattr__(
            self,
            "max_concurrency",
            _require_integer(self.max_concurrency, "max_concurrency", minimum=1, maximum=MAX_GRAPH_CONCURRENCY),
        )
        raw_nodes = _as_tuple(self.nodes, "work graph nodes", maximum=MAX_GRAPH_NODES)
        if len(raw_nodes) < 2:
            raise GraphValidationError("work graph needs at least two nodes")
        nodes = tuple(WorkGraphNode.from_value(item) for item in raw_nodes)
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "final_node", _require_text(self.final_node, "final node", pattern=_NODE_ID, maximum=64))
        computed = sha256_hex(self.redacted_contract())
        if self.manifest_digest and self.manifest_digest != computed:
            raise GraphValidationError("manifest digest does not match the redacted graph contract")
        object.__setattr__(self, "manifest_digest", computed)

    @classmethod
    def from_value(cls, value: WorkGraphManifest | Mapping[str, Any]) -> WorkGraphManifest:
        if type(value) is cls:
            return cls(
                version=value.version,
                task_id=value.task_id,
                lane=value.lane,
                goal=value.goal,
                max_concurrency=value.max_concurrency,
                nodes=value.nodes,
                final_node=value.final_node,
                manifest_digest=value.manifest_digest,
            )
        if isinstance(value, cls):
            raise GraphValidationError("work graph manifest subclasses are not accepted")
        if not isinstance(value, Mapping) or set(value) != cls._FIELDS:
            raise GraphValidationError("work graph manifest fields are invalid")
        return cls(**deepcopy(dict(value)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "task_id": self.task_id,
            "lane": self.lane.value,
            "goal": self.goal,
            "max_concurrency": self.max_concurrency,
            "nodes": [node.to_dict() for node in self.nodes],
            "final_node": self.final_node,
        }

    def redacted_contract(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "task_id_digest": _digest_task_id(self.task_id),
            "lane": self.lane.value,
            "goal_length": len(self.goal),
            "max_concurrency": self.max_concurrency,
            "nodes": [node.redacted_contract() for node in self.nodes],
            "final_node": self.final_node,
        }

    @property
    def node_by_id(self) -> dict[str, WorkGraphNode]:
        return {node.id: node for node in self.nodes}


def load_work_graph_manifest(path: str | Path, *, registry: Mapping[str, Any] | None = None) -> WorkGraphManifest:
    """Load one strict local JSON manifest; historical planning graphs are never scanned."""

    try:
        candidate = Path(path)
        if (
            candidate.is_absolute()
            or not candidate.parts
            or any(part in {"", ".", ".."} for part in candidate.parts)
            or "\\" in str(candidate)
        ):
            raise GraphValidationError("work graph manifest path is not repository-relative")
        document = load_strict_json(resolve_workspace_path(candidate))
    except Exception as error:
        raise GraphValidationError("work graph manifest could not be loaded") from error
    return validate_work_graph_manifest(document, registry=registry)


def _dependency_closure(nodes: Mapping[str, WorkGraphNode], node_id: str) -> frozenset[str]:
    seen: set[str] = set()

    def visit(current: str) -> None:
        for dependency in nodes[current].depends_on:
            if dependency not in seen:
                seen.add(dependency)
                visit(dependency)

    visit(node_id)
    return frozenset(seen)


def _validate_cycle(nodes: Mapping[str, WorkGraphNode]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise GraphValidationError("work graph contains a dependency cycle")
        if node_id in visited:
            return
        visiting.add(node_id)
        for dependency in nodes[node_id].depends_on:
            visit(dependency)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in sorted(nodes):
        visit(node_id)


def _validate_manifest_semantics(manifest: WorkGraphManifest, registry: Mapping[str, Any]) -> None:
    if len(manifest.nodes) > MAX_GRAPH_NODES:
        raise GraphValidationError("work graph exceeds the node limit")
    nodes = manifest.node_by_id
    if len(nodes) != len(manifest.nodes):
        raise GraphValidationError("work graph node IDs must be unique")
    if manifest.final_node not in nodes:
        raise GraphValidationError("final node is not declared")
    for node in manifest.nodes:
        if node.id in node.depends_on:
            raise GraphValidationError("node may not depend on itself")
        missing = set(node.depends_on).difference(nodes)
        if missing:
            raise GraphValidationError("node depends on an undeclared node")
        if node.profile not in APPROVED_PROFILE_ALIASES:
            raise GraphValidationError("node profile is not an approved logical alias")
        try:
            resolve_profile(node.profile, registry)
        except Exception as error:
            raise GraphValidationError("node profile does not resolve through the frozen registry") from error
        if node.reroute_to is not None:
            if node.reroute_to not in APPROVED_PROFILE_ALIASES:
                raise GraphValidationError("reroute profile is not approved")
            if _REROUTE_EDGES.get(node.profile) != node.reroute_to:
                raise GraphValidationError("reroute target is not an approved bounded escalation")
    _validate_cycle(nodes)

    integrators = [node for node in manifest.nodes if node.role is NodeRole.INTEGRATOR]
    if len(integrators) != 1:
        raise GraphValidationError("work graph requires exactly one integrator")
    final = integrators[0]
    if final.id != manifest.final_node or final.profile != "tera-max":
        raise GraphValidationError("final node must be the tera-max integrator")
    if any(final.id in node.depends_on for node in manifest.nodes):
        raise GraphValidationError("final integrator may not have dependents")
    if _dependency_closure(nodes, final.id) != set(nodes).difference({final.id}):
        raise GraphValidationError("final integrator must join every non-final node")

    closures = {node_id: _dependency_closure(nodes, node_id) for node_id in nodes}
    write_owners: list[tuple[str, str]] = []
    for node in manifest.nodes:
        if node.role is NodeRole.REVIEWER:
            if node.permission_class is not PermissionClass.LOCAL_READ or node.write_scope:
                raise GraphValidationError("reviewer must be read-only")
        if node.profile == "gpt55-xhigh" and node.role is not NodeRole.REVIEWER:
            raise GraphValidationError("gpt55-xhigh is reserved for an independent reviewer")
        if node.profile == "luna-xhigh" and (
            node.role is not NodeRole.WORKER
            or node.permission_class is not PermissionClass.LOCAL_READ
            or node.write_scope
            or node.capabilities
            or node.reroute_to is not None
        ):
            raise GraphValidationError("luna-xhigh is restricted to bounded local-read utility work")
        for scope in node.write_scope:
            for owner_id, owner_scope in write_owners:
                if _scope_overlaps(scope, owner_scope):
                    raise GraphValidationError("work graph has overlapping write scopes")
            write_owners.append((node.id, scope))

    for reader in manifest.nodes:
        for read_scope in reader.read_scope:
            for writer_id, write_scope in write_owners:
                if writer_id != reader.id and _scope_overlaps(read_scope, write_scope):
                    if writer_id not in closures[reader.id]:
                        raise GraphValidationError("read/write scope overlap lacks a dependency ordering")

    if manifest.lane is Lane.DEEP:
        reviewers = [node for node in manifest.nodes if node.role is NodeRole.REVIEWER]
        if not reviewers:
            raise GraphValidationError("DEEP work graph requires an independent read-only reviewer")
        for reviewer in reviewers:
            if not reviewer.depends_on or any(
                nodes[dependency].profile == reviewer.profile for dependency in reviewer.depends_on
            ):
                raise GraphValidationError("reviewer must independently review a different profile")


def validate_work_graph_manifest(
    value: WorkGraphManifest | Mapping[str, Any],
    *,
    registry: Mapping[str, Any] | None = None,
) -> WorkGraphManifest:
    """Validate the whole V1 graph before a runtime or worker can be created."""

    if isinstance(value, WorkGraphManifest):
        manifest = WorkGraphManifest.from_value(value)
    else:
        if not isinstance(value, Mapping):
            raise GraphValidationError("work graph manifest must be an object")
        try:
            candidate = deepcopy(dict(value))
        except Exception as error:
            raise GraphValidationError("work graph manifest cannot contain mutable foreign values") from error
        try:
            validate_json_schema(candidate, load_schema("work-graph-v1"))
        except Exception as error:
            raise GraphValidationError("work graph schema validation failed") from error
        manifest = WorkGraphManifest.from_value(candidate)
    try:
        snapshot = load_profile_registry() if registry is None else deepcopy(dict(registry))
        validate_profile_registry(snapshot)
    except Exception as error:
        raise GraphValidationError("frozen model profile registry is invalid") from error
    _validate_manifest_semantics(manifest, snapshot)
    return manifest


@dataclass(frozen=True)
class ArtifactReference:
    """A body-free content-addressed reference permitted in M7 envelopes."""

    artifact_id: str
    sha256: str

    _FIELDS = frozenset(("artifact_id", "sha256"))

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _require_text(self.artifact_id, "artifact id", pattern=_ARTIFACT_ID, maximum=73))
        object.__setattr__(self, "sha256", _require_text(self.sha256, "artifact sha256", pattern=_SHA256, maximum=64))

    @classmethod
    def from_value(cls, value: ArtifactReference | Mapping[str, Any]) -> ArtifactReference:
        if type(value) is cls:
            return cls(artifact_id=value.artifact_id, sha256=value.sha256)
        if isinstance(value, cls):
            raise GraphValidationError("artifact reference subclasses are not accepted")
        if not isinstance(value, Mapping) or set(value) != cls._FIELDS:
            raise GraphValidationError("artifact reference fields are invalid")
        return cls(**deepcopy(dict(value)))

    def to_dict(self) -> dict[str, str]:
        return {"artifact_id": self.artifact_id, "sha256": self.sha256}


@dataclass(frozen=True)
class ProposedChange:
    """An uncommitted, content-addressed change proposal owned by one node."""

    path: str
    sha256: str

    _FIELDS = frozenset(("path", "sha256"))

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _safe_scope(self.path, "proposed change path", allow_directory=False))
        object.__setattr__(self, "sha256", _require_text(self.sha256, "proposed change sha256", pattern=_SHA256, maximum=64))

    @classmethod
    def from_value(cls, value: ProposedChange | Mapping[str, Any]) -> ProposedChange:
        if type(value) is cls:
            return cls(path=value.path, sha256=value.sha256)
        if isinstance(value, cls):
            raise GraphValidationError("proposed change subclasses are not accepted")
        if not isinstance(value, Mapping) or set(value) != cls._FIELDS:
            raise GraphValidationError("proposed change fields are invalid")
        return cls(**deepcopy(dict(value)))

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class NodeResult:
    """Typed, bounded proposal returned by an injected synthetic runner."""

    node_id: str
    attempt: int
    status: ResultStatus | str
    artifacts: tuple[ArtifactReference, ...] | Sequence[ArtifactReference | Mapping[str, Any]] = ()
    evidence: tuple[ArtifactReference, ...] | Sequence[ArtifactReference | Mapping[str, Any]] = ()
    changes: tuple[ProposedChange, ...] | Sequence[ProposedChange | Mapping[str, Any]] = ()
    checks: tuple[str, ...] | Sequence[str] = ()
    unresolved: tuple[str, ...] | Sequence[str] = ()
    next_inputs: tuple[ArtifactReference, ...] | Sequence[ArtifactReference | Mapping[str, Any]] = ()
    token_count: int = 0
    cost_microunits: int = 0
    reroute_evidence_digest: str | None = None
    result_digest: str = field(default="")

    _FIELDS = frozenset(
        (
            "node_id",
            "attempt",
            "status",
            "artifacts",
            "evidence",
            "changes",
            "checks",
            "unresolved",
            "next_inputs",
            "token_count",
            "cost_microunits",
            "reroute_evidence_digest",
            "result_digest",
        )
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _require_text(self.node_id, "result node id", pattern=_NODE_ID, maximum=64))
        object.__setattr__(self, "attempt", _require_integer(self.attempt, "result attempt", minimum=1, maximum=2))
        try:
            status = self.status if isinstance(self.status, ResultStatus) else ResultStatus(self.status)
        except (TypeError, ValueError) as error:
            raise GraphValidationError("result status is invalid") from error
        object.__setattr__(self, "status", status)
        artifacts = tuple(ArtifactReference.from_value(item) for item in _as_tuple(self.artifacts, "result artifacts", maximum=MAX_JOIN_REFERENCES))
        evidence = tuple(ArtifactReference.from_value(item) for item in _as_tuple(self.evidence, "result evidence", maximum=MAX_JOIN_REFERENCES))
        changes = tuple(ProposedChange.from_value(item) for item in _as_tuple(self.changes, "result changes", maximum=16))
        next_inputs = tuple(ArtifactReference.from_value(item) for item in _as_tuple(self.next_inputs, "result next inputs", maximum=MAX_JOIN_REFERENCES))
        if len({item.artifact_id for item in artifacts}) != len(artifacts):
            raise GraphValidationError("result artifacts contain duplicates")
        if len({item.artifact_id for item in evidence}) != len(evidence):
            raise GraphValidationError("result evidence contains duplicates")
        if len({item.path for item in changes}) != len(changes):
            raise GraphValidationError("result changes contain duplicates")
        if len({item.artifact_id for item in next_inputs}) != len(next_inputs):
            raise GraphValidationError("result next inputs contain duplicates")
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "changes", changes)
        object.__setattr__(self, "next_inputs", next_inputs)
        object.__setattr__(
            self,
            "checks",
            _unique_strings(self.checks, "result checks", maximum=MAX_JOIN_REFERENCES, pattern=_SAFE_LABEL),
        )
        object.__setattr__(
            self,
            "unresolved",
            _unique_strings(self.unresolved, "result unresolved codes", maximum=16, pattern=_REASON),
        )
        if self.status is ResultStatus.TRANSIENT_FAILURE and (
            not self.unresolved or not set(self.unresolved).issubset(_TRANSIENT_FAILURE_REASONS)
        ):
            raise GraphValidationError("transient result must use an allowlisted transient reason")
        object.__setattr__(
            self,
            "token_count",
            _require_integer(self.token_count, "result token count", minimum=0, maximum=1_000_000),
        )
        object.__setattr__(
            self,
            "cost_microunits",
            _require_integer(self.cost_microunits, "result cost", minimum=0, maximum=1_000_000_000),
        )
        if self.reroute_evidence_digest is not None:
            object.__setattr__(
                self,
                "reroute_evidence_digest",
                _require_text(
                    self.reroute_evidence_digest,
                    "reroute evidence digest",
                    pattern=_SHA256,
                    maximum=64,
                ),
            )
        computed = sha256_hex(self._digest_input())
        if self.result_digest and self.result_digest != computed:
            raise GraphValidationError("result digest does not match the typed result envelope")
        object.__setattr__(self, "result_digest", computed)

    @classmethod
    def from_value(cls, value: NodeResult | Mapping[str, Any]) -> NodeResult:
        if type(value) is cls:
            return cls(
                node_id=value.node_id,
                attempt=value.attempt,
                status=value.status,
                artifacts=value.artifacts,
                evidence=value.evidence,
                changes=value.changes,
                checks=value.checks,
                unresolved=value.unresolved,
                next_inputs=value.next_inputs,
                token_count=value.token_count,
                cost_microunits=value.cost_microunits,
                reroute_evidence_digest=value.reroute_evidence_digest,
                result_digest=value.result_digest,
            )
        if isinstance(value, cls):
            raise GraphValidationError("node result subclasses are not accepted")
        if not isinstance(value, Mapping) or set(value) != cls._FIELDS:
            raise GraphValidationError("node result fields are invalid")
        return cls(**deepcopy(dict(value)))

    @classmethod
    def succeeded(
        cls,
        node_id: str,
        attempt: int,
        *,
        artifacts: Sequence[ArtifactReference | Mapping[str, Any]] = (),
        evidence: Sequence[ArtifactReference | Mapping[str, Any]] = (),
        changes: Sequence[ProposedChange | Mapping[str, Any]] = (),
        checks: Sequence[str] = (),
        next_inputs: Sequence[ArtifactReference | Mapping[str, Any]] = (),
        token_count: int = 0,
        cost_microunits: int = 0,
    ) -> NodeResult:
        return cls(
            node_id=node_id,
            attempt=attempt,
            status=ResultStatus.SUCCEEDED,
            artifacts=artifacts,
            evidence=evidence,
            changes=changes,
            checks=checks,
            next_inputs=next_inputs,
            token_count=token_count,
            cost_microunits=cost_microunits,
        )

    success = succeeded

    @classmethod
    def failed(cls, node_id: str, attempt: int, *, unresolved: Sequence[str] = ("RUNNER_FAILED",)) -> NodeResult:
        return cls(node_id=node_id, attempt=attempt, status=ResultStatus.FAILED, unresolved=unresolved)

    @classmethod
    def transient_failure(
        cls, node_id: str, attempt: int, *, unresolved: Sequence[str] = ("TEMPORARY_UNAVAILABLE",)
    ) -> NodeResult:
        return cls(node_id=node_id, attempt=attempt, status=ResultStatus.TRANSIENT_FAILURE, unresolved=unresolved)

    @classmethod
    def reroute_required(cls, node_id: str, attempt: int, *, evidence_digest: str) -> NodeResult:
        return cls(
            node_id=node_id,
            attempt=attempt,
            status=ResultStatus.REROUTE_REQUIRED,
            reroute_evidence_digest=evidence_digest,
        )

    def _digest_input(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "attempt": self.attempt,
            "status": self.status.value,
            "artifacts": [item.to_dict() for item in self.artifacts],
            "evidence": [item.to_dict() for item in self.evidence],
            "changes": [item.to_dict() for item in self.changes],
            "checks": list(self.checks),
            "unresolved": list(self.unresolved),
            "next_inputs": [item.to_dict() for item in self.next_inputs],
            "token_count": self.token_count,
            "cost_microunits": self.cost_microunits,
            "reroute_evidence_digest": self.reroute_evidence_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._digest_input(), "result_digest": self.result_digest}

    @property
    def estimated_tokens(self) -> int:
        return _estimated_tokens(self._digest_input())


@dataclass(frozen=True)
class ResultReference:
    """The only result projection available to a dependent node or root join."""

    node_id: str
    attempt: int
    artifacts: tuple[ArtifactReference, ...]
    evidence: tuple[ArtifactReference, ...]
    checks: tuple[str, ...]
    route_receipt_digest: str
    result_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _require_text(self.node_id, "reference node id", pattern=_NODE_ID, maximum=64))
        object.__setattr__(self, "attempt", _require_integer(self.attempt, "reference attempt", minimum=1, maximum=2))
        object.__setattr__(self, "artifacts", tuple(ArtifactReference.from_value(item) for item in _as_tuple(self.artifacts, "reference artifacts", maximum=MAX_JOIN_REFERENCES)))
        object.__setattr__(self, "evidence", tuple(ArtifactReference.from_value(item) for item in _as_tuple(self.evidence, "reference evidence", maximum=MAX_JOIN_REFERENCES)))
        object.__setattr__(self, "checks", _unique_strings(self.checks, "reference checks", maximum=MAX_JOIN_REFERENCES, pattern=_SAFE_LABEL))
        object.__setattr__(self, "route_receipt_digest", _require_text(self.route_receipt_digest, "route receipt digest", pattern=_SHA256, maximum=64))
        object.__setattr__(self, "result_digest", _require_text(self.result_digest, "result digest", pattern=_SHA256, maximum=64))

    @classmethod
    def from_value(cls, value: ResultReference | Mapping[str, Any]) -> ResultReference:
        if type(value) is cls:
            return cls(
                node_id=value.node_id,
                attempt=value.attempt,
                artifacts=value.artifacts,
                evidence=value.evidence,
                checks=value.checks,
                route_receipt_digest=value.route_receipt_digest,
                result_digest=value.result_digest,
            )
        if isinstance(value, cls):
            raise GraphValidationError("result reference subclasses are not accepted")
        fields = {
            "node_id",
            "attempt",
            "artifacts",
            "evidence",
            "checks",
            "route_receipt_digest",
            "result_digest",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise GraphValidationError("result reference fields are invalid")
        return cls(**deepcopy(dict(value)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "attempt": self.attempt,
            "artifacts": [item.to_dict() for item in self.artifacts],
            "evidence": [item.to_dict() for item in self.evidence],
            "checks": list(self.checks),
            "route_receipt_digest": self.route_receipt_digest,
            "result_digest": self.result_digest,
        }


class CancellationToken:
    """Read-only cancellation view; runners cannot clear or set either event."""

    __slots__ = ("__handle",)

    def __init__(self, run_cancel: Event, attempt_cancel: Event) -> None:
        object.__setattr__(self, "_CancellationToken__handle", _register_cancellation_handle(run_cancel, attempt_cancel))

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise AttributeError("cancellation token is read-only")

    @property
    def cancelled(self) -> bool:
        return _cancellation_requested(self.__handle)

    def wait(self, timeout: float | None = None) -> bool:
        return _wait_for_cancellation(self.__handle, timeout)


@dataclass(frozen=True)
class NodeContext:
    """Minimal immutable packet passed to a synthetic runner only."""

    run_id: str
    task_id: str
    lane: Lane
    node_id: str
    attempt: int
    role: NodeRole
    task: str
    profile_alias: str
    permission_class: PermissionClass
    read_scope: tuple[str, ...]
    write_scope: tuple[str, ...]
    acceptance: tuple[str, ...]
    dependency_results: tuple[ResultReference, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _require_text(self.run_id, "run id", pattern=_OPAQUE_ID, maximum=96))
        object.__setattr__(self, "task_id", _require_text(self.task_id, "context task id", pattern=_TASK_ID, maximum=41))
        try:
            lane = self.lane if isinstance(self.lane, Lane) else Lane(self.lane)
            role = self.role if isinstance(self.role, NodeRole) else NodeRole(self.role)
            permission = (
                self.permission_class
                if isinstance(self.permission_class, PermissionClass)
                else PermissionClass(self.permission_class)
            )
        except (TypeError, ValueError) as error:
            raise GraphValidationError("context enum value is invalid") from error
        if lane not in {Lane.GRAPH, Lane.DEEP}:
            raise GraphValidationError("context lane is invalid")
        if permission not in {PermissionClass.LOCAL_READ, PermissionClass.WORKSPACE_WRITE}:
            raise GraphValidationError("context permission is invalid")
        object.__setattr__(self, "lane", lane)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "permission_class", permission)
        object.__setattr__(self, "node_id", _require_text(self.node_id, "context node id", pattern=_NODE_ID, maximum=64))
        object.__setattr__(self, "attempt", _require_integer(self.attempt, "context attempt", minimum=1, maximum=2))
        object.__setattr__(self, "task", _require_text(self.task, "context task", maximum=512))
        object.__setattr__(self, "profile_alias", _require_text(self.profile_alias, "context profile", pattern=_SAFE_LABEL, maximum=96))
        read_scope = tuple(_safe_scope(item, "context read scope") for item in _as_tuple(self.read_scope, "context read scope", maximum=16))
        write_scope = tuple(_safe_scope(item, "context write scope") for item in _as_tuple(self.write_scope, "context write scope", maximum=16))
        acceptance = _unique_strings(self.acceptance, "context acceptance", maximum=16, pattern=_SAFE_LABEL, minimum=1)
        dependencies = tuple(
            ResultReference.from_value(item)
            for item in _as_tuple(self.dependency_results, "context dependency references", maximum=MAX_WORKER_REFERENCES)
        )
        object.__setattr__(self, "read_scope", read_scope)
        object.__setattr__(self, "write_scope", write_scope)
        object.__setattr__(self, "acceptance", acceptance)
        object.__setattr__(self, "dependency_results", dependencies)
        if self.estimated_tokens > MAX_WORKER_CONTEXT_TOKENS:
            raise GraphValidationError("context exceeds the worker token budget")

    def to_worker_dict(self) -> dict[str, Any]:
        """Return the bounded packet; callers must not persist it as observability."""

        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "lane": self.lane.value,
            "node_id": self.node_id,
            "attempt": self.attempt,
            "role": self.role.value,
            "task": self.task,
            "profile_alias": self.profile_alias,
            "permission_class": self.permission_class.value,
            "read_scope": list(self.read_scope),
            "write_scope": list(self.write_scope),
            "acceptance": list(self.acceptance),
            "dependency_results": [item.to_dict() for item in self.dependency_results],
        }

    @property
    def estimated_tokens(self) -> int:
        return _estimated_tokens(self.to_worker_dict())


@dataclass(frozen=True)
class CompactedArtifact:
    """Body-free metadata for oversized local evidence; no raw data is retained."""

    artifact_id: str
    sha256: str
    byte_count: int
    estimated_tokens: int
    summary: str = "content-compacted"

    def __post_init__(self) -> None:
        _require_text(self.artifact_id, "compacted artifact id", pattern=_ARTIFACT_ID, maximum=73)
        _require_text(self.sha256, "compacted artifact sha256", pattern=_SHA256, maximum=64)
        _require_integer(self.byte_count, "compacted artifact byte count", minimum=0, maximum=100_000_000)
        _require_integer(self.estimated_tokens, "compacted artifact token count", minimum=0, maximum=100_000_000)
        if self.summary != "content-compacted":
            raise GraphValidationError("compaction summary is not allowlisted")

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
            "estimated_tokens": self.estimated_tokens,
            "summary": self.summary,
        }


def compact_artifact(raw: str | bytes, *, artifact_id: str) -> CompactedArtifact:
    """Hash safe local bytes without storing or returning the original content."""

    if not isinstance(raw, (str, bytes)):
        raise GraphValidationError("compaction input must be text or bytes")
    if _contains_sensitive_marker(raw):
        raise GraphPolicyError("compaction input is not safe to retain as graph evidence")
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    return CompactedArtifact(
        artifact_id=artifact_id,
        sha256=sha256_bytes(data),
        byte_count=len(data),
        estimated_tokens=math.ceil(len(data) / 4),
    )


@dataclass(frozen=True)
class GraphEvent:
    """Redacted runtime-owned observability event with a fixed field allowlist."""

    event_id: str
    run_id: str
    task_id_digest: str
    manifest_digest: str
    lane: Lane | str
    event_type: str
    status: str
    reason_code: str
    occurred_at: str
    node_id: str | None = None
    profile_alias: str | None = None
    duration_ms: int = 0
    token_count: int = 0
    cost_microunits: int = 0
    artifacts: tuple[ArtifactReference, ...] = ()
    event_digest: str = field(default="")

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _require_text(self.event_id, "event id", pattern=_OPAQUE_ID, maximum=96))
        object.__setattr__(self, "run_id", _require_text(self.run_id, "event run id", pattern=_OPAQUE_ID, maximum=96))
        object.__setattr__(self, "task_id_digest", _require_text(self.task_id_digest, "event task digest", pattern=_SHA256, maximum=64))
        object.__setattr__(self, "manifest_digest", _require_text(self.manifest_digest, "event manifest digest", pattern=_SHA256, maximum=64))
        try:
            lane = self.lane if isinstance(self.lane, Lane) else Lane(self.lane)
        except (TypeError, ValueError) as error:
            raise GraphValidationError("event lane is invalid") from error
        if lane not in {Lane.GRAPH, Lane.DEEP}:
            raise GraphValidationError("event lane is invalid")
        object.__setattr__(self, "lane", lane)
        if self.event_type not in _GRAPH_EVENT_TYPES:
            raise GraphValidationError("event type is not allowlisted")
        _require_text(self.status, "event status", pattern=re.compile(r"^[a-z_]{1,64}$"), maximum=64)
        if self.reason_code not in _EVENT_REASONS:
            raise GraphValidationError("event reason code is not allowlisted")
        _require_text(self.occurred_at, "event timestamp", maximum=32)
        if self.node_id is not None:
            object.__setattr__(self, "node_id", _require_text(self.node_id, "event node id", pattern=_NODE_ID, maximum=64))
        if self.profile_alias is not None:
            if self.profile_alias not in APPROVED_PROFILE_ALIASES:
                raise GraphValidationError("event profile is not approved")
        object.__setattr__(self, "duration_ms", _require_integer(self.duration_ms, "event duration", minimum=0, maximum=86_400_000))
        object.__setattr__(self, "token_count", _require_integer(self.token_count, "event tokens", minimum=0, maximum=1_000_000))
        object.__setattr__(self, "cost_microunits", _require_integer(self.cost_microunits, "event cost", minimum=0, maximum=1_000_000_000))
        artifacts = tuple(ArtifactReference.from_value(item) for item in self.artifacts)
        if len(artifacts) > MAX_JOIN_REFERENCES:
            raise GraphValidationError("event artifact references exceed the limit")
        object.__setattr__(self, "artifacts", artifacts)
        computed = sha256_hex(self._digest_input())
        if self.event_digest and self.event_digest != computed:
            raise GraphValidationError("event digest does not match the allowlisted event")
        object.__setattr__(self, "event_digest", computed)

    def _digest_input(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "task_id_digest": self.task_id_digest,
            "manifest_digest": self.manifest_digest,
            "lane": self.lane.value,
            "event_type": self.event_type,
            "status": self.status,
            "reason_code": self.reason_code,
            "occurred_at": self.occurred_at,
            "node_id": self.node_id,
            "profile_alias": self.profile_alias,
            "duration_ms": self.duration_ms,
            "token_count": self.token_count,
            "cost_microunits": self.cost_microunits,
            "artifacts": [item.to_dict() for item in self.artifacts],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._digest_input(), "event_digest": self.event_digest}


@dataclass(frozen=True)
class NodeRunReceipt:
    """Redacted terminal state for one node, not a mutable scheduler snapshot."""

    node_id: str
    state: NodeState | str
    attempts: int
    profile_alias: str
    reason_code: str | None
    route_receipt_digest: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _require_text(self.node_id, "receipt node id", pattern=_NODE_ID, maximum=64))
        try:
            state = self.state if isinstance(self.state, NodeState) else NodeState(self.state)
        except (TypeError, ValueError) as error:
            raise GraphValidationError("node receipt state is invalid") from error
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "attempts", _require_integer(self.attempts, "node receipt attempts", minimum=0, maximum=2))
        if self.profile_alias not in APPROVED_PROFILE_ALIASES:
            raise GraphValidationError("node receipt profile is invalid")
        if self.reason_code is not None and self.reason_code not in _EVENT_REASONS:
            raise GraphValidationError("node receipt reason is invalid")
        if self.route_receipt_digest is not None:
            object.__setattr__(self, "route_receipt_digest", _require_text(self.route_receipt_digest, "node route digest", pattern=_SHA256, maximum=64))

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "state": self.state.value,
            "attempts": self.attempts,
            "profile_alias": self.profile_alias,
            "reason_code": self.reason_code,
            "route_receipt_digest": self.route_receipt_digest,
        }


@dataclass(frozen=True)
class GraphRunReceipt:
    """Final redacted receipt.  It proves no M7 result was a material commit."""

    run_id: str
    task_id_digest: str
    manifest_digest: str
    admission_digest: str
    lane: Lane | str
    state: GraphState | str
    nodes: tuple[NodeRunReceipt, ...]
    final_result: ResultReference | None
    events: tuple[GraphEvent, ...]
    serial_fallback: bool
    max_active_workers: int
    duration_ms: int
    total_tokens: int
    total_cost_microunits: int
    proposal_only: bool = True
    material_side_effect_allowed: bool = False
    receipt_digest: str = field(default="")

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _require_text(self.run_id, "receipt run id", pattern=_OPAQUE_ID, maximum=96))
        for field_name in ("task_id_digest", "manifest_digest", "admission_digest"):
            object.__setattr__(self, field_name, _require_text(getattr(self, field_name), field_name, pattern=_SHA256, maximum=64))
        try:
            lane = self.lane if isinstance(self.lane, Lane) else Lane(self.lane)
            state = self.state if isinstance(self.state, GraphState) else GraphState(self.state)
        except (TypeError, ValueError) as error:
            raise GraphValidationError("graph receipt state or lane is invalid") from error
        object.__setattr__(self, "lane", lane)
        object.__setattr__(self, "state", state)
        receipts = tuple(self.nodes)
        if not receipts or len({item.node_id for item in receipts}) != len(receipts):
            raise GraphValidationError("graph receipt node states are invalid")
        object.__setattr__(self, "nodes", receipts)
        if self.final_result is not None and not isinstance(self.final_result, ResultReference):
            raise GraphValidationError("graph receipt final result is invalid")
        events = tuple(self.events)
        if len({event.event_id for event in events}) != len(events):
            raise GraphValidationError("graph receipt event IDs collide")
        object.__setattr__(self, "events", events)
        if not isinstance(self.serial_fallback, bool):
            raise GraphValidationError("serial fallback flag is invalid")
        object.__setattr__(self, "max_active_workers", _require_integer(self.max_active_workers, "max active workers", minimum=0, maximum=MAX_GRAPH_CONCURRENCY))
        object.__setattr__(self, "duration_ms", _require_integer(self.duration_ms, "receipt duration", minimum=0, maximum=86_400_000))
        object.__setattr__(self, "total_tokens", _require_integer(self.total_tokens, "receipt tokens", minimum=0, maximum=1_000_000))
        object.__setattr__(self, "total_cost_microunits", _require_integer(self.total_cost_microunits, "receipt cost", minimum=0, maximum=1_000_000_000))
        if self.proposal_only is not True or self.material_side_effect_allowed is not False:
            raise GraphPolicyError("M7 receipts are proposal-only and cannot authorize material side effects")
        computed = sha256_hex(self._digest_input())
        if self.receipt_digest and self.receipt_digest != computed:
            raise GraphValidationError("graph receipt digest does not match")
        object.__setattr__(self, "receipt_digest", computed)

    def _digest_input(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id_digest": self.task_id_digest,
            "manifest_digest": self.manifest_digest,
            "admission_digest": self.admission_digest,
            "lane": self.lane.value,
            "state": self.state.value,
            "nodes": [item.to_dict() for item in self.nodes],
            "final_result": None if self.final_result is None else self.final_result.to_dict(),
            "events": [event.event_digest for event in self.events],
            "serial_fallback": self.serial_fallback,
            "max_active_workers": self.max_active_workers,
            "duration_ms": self.duration_ms,
            "total_tokens": self.total_tokens,
            "total_cost_microunits": self.total_cost_microunits,
            "proposal_only": self.proposal_only,
            "material_side_effect_allowed": self.material_side_effect_allowed,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._digest_input(),
            "events": [event.to_dict() for event in self.events],
            "receipt_digest": self.receipt_digest,
        }


@dataclass
class _Attempt:
    node: WorkGraphNode
    attempt: int
    profile_alias: str
    local_cancel: Event
    context: NodeContext
    route_receipt: RouteReceipt
    started_at: float
    future: Future[Any] | None = None
    timed_out: bool = False
    cancelled: bool = False


class GraphRuntime:
    """Execute one prevalidated graph with injected synthetic workers only."""

    def __init__(
        self,
        manifest: WorkGraphManifest | Mapping[str, Any],
        *,
        audit_trail: AuditTrail,
        registry: Mapping[str, Any] | None = None,
        clock: Any = None,
        monotonic_clock: Callable[[], float] | None = None,
        id_factory: Callable[[], str | uuid.UUID] | None = None,
        event_sink: Callable[[GraphEvent], None] | None = None,
    ) -> None:
        if type(audit_trail) is not AuditTrail:
            raise GraphPolicyError("M7 graph runtime requires the issuing M5 audit trail")
        try:
            snapshot = load_profile_registry() if registry is None else deepcopy(dict(registry))
            validate_profile_registry(snapshot)
        except Exception as error:
            raise GraphPolicyError("M7 graph runtime needs a valid frozen M6 registry") from error
        self._registry = snapshot
        self.manifest = validate_work_graph_manifest(manifest, registry=snapshot)
        self._audit_trail = audit_trail
        self._clock = clock
        self._monotonic = monotonic_clock or time.monotonic
        self._id_factory = id_factory or uuid.uuid4
        self._event_sink = event_sink
        self._lock = RLock()
        self._cancel_event = Event()
        self._state = GraphState.NEW
        self._node_states = {node.id: NodeState.PENDING for node in self.manifest.nodes}
        self._node_reasons: dict[str, str | None] = {node.id: None for node in self.manifest.nodes}
        self._attempts = {node.id: 0 for node in self.manifest.nodes}
        self._profiles = {node.id: node.profile for node in self.manifest.nodes}
        self._route_digests: dict[str, str | None] = {node.id: None for node in self.manifest.nodes}
        self._result_refs: dict[str, ResultReference] = {}
        self._events: list[GraphEvent] = []
        self._run_id: str | None = None
        self._event_sequence = 0
        self._cancel_event_emitted = False
        self._max_active_workers = 0
        self._total_tokens = 0
        self._total_cost = 0

    @property
    def state(self) -> GraphState:
        with self._lock:
            return self._state

    @property
    def node_states(self) -> dict[str, NodeState]:
        with self._lock:
            return dict(self._node_states)

    @property
    def events(self) -> tuple[GraphEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def cancel(self) -> None:
        """Request a safe user-redirect cancellation; no caller can resume this run."""

        with self._lock:
            self._cancel_event.set()

    def _new_run_id(self) -> str:
        raw = str(self._id_factory())
        if raw.startswith("graph:"):
            return _require_text(raw, "run id", pattern=_OPAQUE_ID, maximum=96)
        return _require_text(f"graph:{raw}", "run id", pattern=_OPAQUE_ID, maximum=96)

    def _emit(
        self,
        event_type: str,
        *,
        status: str,
        reason_code: str,
        node: WorkGraphNode | None = None,
        profile_alias: str | None = None,
        duration_ms: int = 0,
        token_count: int = 0,
        cost_microunits: int = 0,
        artifacts: Sequence[ArtifactReference] = (),
    ) -> None:
        if self._run_id is None:
            raise GraphPolicyError("graph observability cannot start before the run identity exists")
        self._event_sequence += 1
        event = GraphEvent(
            event_id=f"event:{self._run_id.split(':', 1)[1]}-{self._event_sequence}",
            run_id=self._run_id,
            task_id_digest=_digest_task_id(self.manifest.task_id),
            manifest_digest=self.manifest.manifest_digest,
            lane=self.manifest.lane,
            event_type=event_type,
            status=status,
            reason_code=reason_code,
            occurred_at=_timestamp(self._clock),
            node_id=None if node is None else node.id,
            profile_alias=profile_alias,
            duration_ms=duration_ms,
            token_count=token_count,
            cost_microunits=cost_microunits,
            artifacts=tuple(artifacts),
        )
        try:
            if self._event_sink is not None:
                self._event_sink(event)
        except Exception as error:
            self._cancel_event.set()
            raise GraphPolicyError("required graph observability sink failed") from error
        self._events.append(event)

    def _set_node_state(self, node_id: str, state: NodeState, reason_code: str | None = None) -> None:
        current = self._node_states[node_id]
        if current is state:
            return
        if state not in _NODE_TRANSITIONS[current]:
            raise GraphPolicyError("illegal graph node lifecycle transition")
        self._node_states[node_id] = state
        self._node_reasons[node_id] = reason_code

    def _validate_admission(self, admission: AdmissionDecision) -> None:
        if not isinstance(admission, AdmissionDecision) or not admission.verify():
            raise GraphPolicyError("M5 admission decision is invalid")
        if not AuditTrail.has_admission(self._audit_trail, admission):
            raise GraphPolicyError("M5 admission was not issued by the injected audit trail")
        if admission.lane is not self.manifest.lane or admission.task_id != self.manifest.task_id:
            raise GraphPolicyError("M5 admission does not bind this graph task and lane")
        if admission.lane not in {Lane.GRAPH, Lane.DEEP}:
            raise GraphPolicyError("DIRECT and ASSISTED cannot enter the graph runtime")
        if admission.features.user_forbid_capabilities and any(node.capabilities for node in self.manifest.nodes):
            raise GraphPolicyError("user capability prohibition blocks the graph")
        if "INDEPENDENT_BRANCHES" in admission.reason_codes:
            nodes = self.manifest.node_by_id
            non_final = [node for node in self.manifest.nodes if node.id != self.manifest.final_node]
            closures = {node.id: _dependency_closure(nodes, node.id) for node in non_final}
            independent = any(
                right.id not in closures[left.id] and left.id not in closures[right.id]
                for index, left in enumerate(non_final)
                for right in non_final[index + 1 :]
            )
            if not independent:
                raise GraphPolicyError("automatic graph admission lacks independent declared branches")

    def _route_attempt(
        self,
        node: WorkGraphNode,
        attempt: int,
        profile_alias: str,
        route_observer: Callable[[Any, Any], Iterable[Any]] | None,
    ) -> RouteReceipt | None:
        assert self._run_id is not None
        route_suffix = sha256_hex(
            {"run_id": self._run_id, "node_id": node.id, "attempt": attempt}
        )[:32]
        try:
            intent = create_route_intent(
                route_id=f"route:{route_suffix}",
                task_id=self.manifest.task_id,
                node_id=f"node:{node.id}",
                profile_alias=profile_alias,
                correlation_id=f"corr:{route_suffix}",
                created_at=_timestamp(self._clock),
                registry=self._registry,
            )
            serialized = serialize_route(intent, registry=self._registry, sent_at=_timestamp(self._clock))
            observations = (
                (synthetic_observation(intent, serialized),)
                if route_observer is None
                else tuple(route_observer(intent, serialized))
            )
            reconciliation = reconcile_route(serialized, observations, reconciled_at=_timestamp(self._clock))
            receipt = create_route_receipt(intent, serialized, reconciliation)
        except Exception:
            return None
        if receipt.status is not ReconciliationStatus.MATCH:
            return receipt
        # This boundary must remain false even for the local synthetic MATCH receipt.
        if side_effect_allowed(receipt, self.manifest.lane, intent=intent, permission_allowed=False):
            raise GraphPolicyError("synthetic M6 receipt unexpectedly authorized a material side effect")
        return receipt

    def _context_for(self, node: WorkGraphNode, attempt: int, profile_alias: str) -> NodeContext:
        assert self._run_id is not None
        dependencies = tuple(self._result_refs[dependency] for dependency in node.depends_on)
        return NodeContext(
            run_id=self._run_id,
            task_id=self.manifest.task_id,
            lane=self.manifest.lane,
            node_id=node.id,
            attempt=attempt,
            role=node.role,
            task=node.task,
            profile_alias=profile_alias,
            permission_class=node.permission_class,
            read_scope=node.read_scope,
            write_scope=node.write_scope,
            acceptance=node.acceptance,
            dependency_results=dependencies,
        )

    def _validate_result(self, node: WorkGraphNode, attempt: _Attempt, result: NodeResult) -> None:
        if result.node_id != node.id or result.attempt != attempt.attempt:
            raise GraphValidationError("result identity does not bind the active node attempt")
        max_tokens = MAX_JOIN_RESULT_TOKENS if node.role in {NodeRole.REVIEWER, NodeRole.INTEGRATOR} else MAX_WORKER_RESULT_TOKENS
        max_references = MAX_JOIN_REFERENCES if node.role in {NodeRole.REVIEWER, NodeRole.INTEGRATOR} else MAX_WORKER_REFERENCES
        if result.estimated_tokens > max_tokens or result.token_count > max_tokens:
            raise GraphValidationError("result envelope exceeds its token budget")
        if len(result.artifacts) + len(result.evidence) + len(result.next_inputs) > max_references:
            raise GraphValidationError("result envelope exceeds its reference budget")
        allowed_artifacts = set(node.expected_artifacts)
        referenced_ids = {item.artifact_id for item in (*result.artifacts, *result.evidence, *result.next_inputs)}
        if not referenced_ids.issubset(allowed_artifacts):
            raise GraphValidationError("result introduces an undeclared artifact reference")
        if set(result.checks).difference(node.acceptance):
            raise GraphValidationError("result introduces an undeclared acceptance check")
        if result.status is ResultStatus.SUCCEEDED:
            if set(node.expected_artifacts).difference(item.artifact_id for item in result.artifacts):
                raise GraphValidationError("successful node omitted a required artifact reference")
            if set(node.acceptance).difference(result.checks):
                raise GraphValidationError("successful node omitted a required acceptance check")
        if result.changes and node.permission_class is not PermissionClass.WORKSPACE_WRITE:
            raise GraphValidationError("read-only node returned a proposed change")
        if node.role is NodeRole.REVIEWER and result.changes:
            raise GraphValidationError("reviewer returned a proposed change")
        for change in result.changes:
            if not any(_scope_contains(scope, change.path) for scope in node.write_scope):
                raise GraphValidationError("result proposed an out-of-scope change")
        if result.status is ResultStatus.REROUTE_REQUIRED:
            if (
                result.reroute_evidence_digest is None
                or result.artifacts
                or result.evidence
                or result.changes
                or result.checks
                or result.next_inputs
            ):
                raise GraphValidationError("reroute result must be an evidence-only pre-side-effect proposal")

    def _validate_integrator_result(self, node: WorkGraphNode, result: NodeResult) -> None:
        if set(node.expected_artifacts).difference(item.artifact_id for item in result.artifacts):
            raise GraphValidationError("integrator omitted a required final artifact reference")
        if set(node.acceptance).difference(result.checks):
            raise GraphValidationError("integrator omitted a required final acceptance check")
        by_id = self.manifest.node_by_id
        for dependency in node.depends_on:
            reference = self._result_refs.get(dependency)
            if reference is None:
                raise GraphValidationError("integrator lacks a successful direct dependency reference")
            required = set(by_id[dependency].expected_artifacts)
            observed = {item.artifact_id for item in reference.artifacts}
            if required.difference(observed):
                raise GraphValidationError("integrator dependency is missing a required artifact")
            if set(by_id[dependency].acceptance).difference(reference.checks):
                raise GraphValidationError("integrator dependency is missing a required acceptance check")

    def _reference_for(self, result: NodeResult, route_receipt: RouteReceipt) -> ResultReference:
        return ResultReference(
            node_id=result.node_id,
            attempt=result.attempt,
            artifacts=result.artifacts,
            evidence=result.evidence,
            checks=result.checks,
            route_receipt_digest=route_receipt.receipt_digest,
            result_digest=result.result_digest,
        )

    def _cancel_pending_and_active(self, running: Mapping[Future[Any], _Attempt]) -> None:
        for node in self.manifest.nodes:
            current = self._node_states[node.id]
            if current in {NodeState.PENDING, NodeState.READY, NodeState.RETRY_WAIT}:
                self._set_node_state(node.id, NodeState.CANCELLED, "CANCELLED_USER_REDIRECT")
                self._emit(
                    "node_blocked",
                    status=NodeState.CANCELLED.value,
                    reason_code="CANCELLED_USER_REDIRECT",
                    node=node,
                    profile_alias=self._profiles[node.id],
                )
        for attempt in running.values():
            attempt.cancelled = True
            attempt.local_cancel.set()
            current = self._node_states[attempt.node.id]
            if current is NodeState.RUNNING:
                self._set_node_state(attempt.node.id, NodeState.CANCELLED, "CANCELLED_USER_REDIRECT")
                self._emit(
                    "node_blocked",
                    status=NodeState.CANCELLED.value,
                    reason_code="CANCELLED_USER_REDIRECT",
                    node=attempt.node,
                    profile_alias=attempt.profile_alias,
                )

    def _emit_cancel_requested(self) -> None:
        if not self._cancel_event_emitted:
            self._cancel_event_emitted = True
            self._emit(
                "graph_cancel_requested",
                status=GraphState.CANCELLED.value,
                reason_code="CANCELLED_USER_REDIRECT",
            )

    def _block_downstream(self) -> bool:
        changed = False
        for node in self.manifest.nodes:
            current = self._node_states[node.id]
            if current not in {NodeState.PENDING, NodeState.READY, NodeState.RETRY_WAIT}:
                continue
            if any(self._node_states[dependency] in _TERMINAL_NODE_STATES - {NodeState.SUCCEEDED} for dependency in node.depends_on):
                self._set_node_state(node.id, NodeState.BLOCKED, "BLOCKED_DEPENDENCY")
                self._emit(
                    "node_blocked",
                    status=NodeState.BLOCKED.value,
                    reason_code="BLOCKED_DEPENDENCY",
                    node=node,
                    profile_alias=self._profiles[node.id],
                )
                changed = True
        return changed

    def _ready_nodes(self) -> list[WorkGraphNode]:
        ready: list[WorkGraphNode] = []
        for node in sorted(self.manifest.nodes, key=lambda item: item.id):
            if self._node_states[node.id] is NodeState.PENDING and all(
                self._node_states[dependency] is NodeState.SUCCEEDED for dependency in node.depends_on
            ):
                ready.append(node)
        return ready

    def _start_attempt(
        self,
        executor: ThreadPoolExecutor,
        node: WorkGraphNode,
        *,
        route_observer: Callable[[Any, Any], Iterable[Any]] | None,
    ) -> _Attempt | None:
        if self._cancel_event.is_set():
            return None
        self._set_node_state(node.id, NodeState.READY, "NODE_READY")
        self._emit(
            "node_ready",
            status=NodeState.READY.value,
            reason_code="NODE_READY",
            node=node,
            profile_alias=self._profiles[node.id],
        )
        attempt_number = self._attempts[node.id] + 1
        profile_alias = self._profiles[node.id]
        receipt = self._route_attempt(node, attempt_number, profile_alias, route_observer)
        if receipt is None or receipt.status is not ReconciliationStatus.MATCH:
            self._route_digests[node.id] = None if receipt is None else receipt.receipt_digest
            self._set_node_state(node.id, NodeState.QUARANTINED_ROUTE, "ROUTE_QUARANTINED")
            self._emit(
                "node_blocked",
                status=NodeState.QUARANTINED_ROUTE.value,
                reason_code="ROUTE_QUARANTINED",
                node=node,
                profile_alias=profile_alias,
            )
            return None
        context = self._context_for(node, attempt_number, profile_alias)
        local_cancel = Event()
        attempt = _Attempt(
            node=node,
            attempt=attempt_number,
            profile_alias=profile_alias,
            local_cancel=local_cancel,
            context=context,
            route_receipt=receipt,
            started_at=self._monotonic(),
        )
        self._attempts[node.id] = attempt_number
        self._route_digests[node.id] = receipt.receipt_digest
        self._set_node_state(node.id, NodeState.RUNNING, "NODE_STARTED")
        self._emit(
            "node_started",
            status=NodeState.RUNNING.value,
            reason_code="NODE_STARTED",
            node=node,
            profile_alias=profile_alias,
        )
        return attempt

    @staticmethod
    def _call_runner(
        runner: Callable[[NodeContext, CancellationToken], NodeResult],
        context: NodeContext,
        cancellation: CancellationToken,
    ) -> NodeResult:
        if getattr(_RUNTIME_LOCAL, "worker_active", False):
            raise GraphPolicyError("worker may not invoke a nested graph runtime")
        _RUNTIME_LOCAL.worker_active = True
        entered = False
        try:
            _enter_graph_worker()
            entered = True
            return runner(context, cancellation)
        finally:
            try:
                _retire_cancellation_handle(object.__getattribute__(cancellation, "_CancellationToken__handle"))
            finally:
                try:
                    if entered:
                        _leave_graph_worker()
                finally:
                    _RUNTIME_LOCAL.worker_active = False

    def _settle_attempt(self, attempt: _Attempt) -> None:
        assert attempt.future is not None
        node = attempt.node
        if attempt.cancelled or self._cancel_event.is_set():
            # Cancellation wins even when a runner races to return success.
            if self._node_states[node.id] is NodeState.RUNNING:
                self._set_node_state(node.id, NodeState.CANCELLED, "CANCELLED_USER_REDIRECT")
                self._emit(
                    "node_blocked",
                    status=NodeState.CANCELLED.value,
                    reason_code="CANCELLED_USER_REDIRECT",
                    node=node,
                    profile_alias=attempt.profile_alias,
                )
            return
        if attempt.timed_out:
            return
        try:
            raw_result = attempt.future.result()
        except Exception:
            self._set_node_state(node.id, NodeState.FAILED, "RUNNER_EXCEPTION")
            self._emit(
                "node_finished",
                status=NodeState.FAILED.value,
                reason_code="RUNNER_EXCEPTION",
                node=node,
                profile_alias=attempt.profile_alias,
            )
            return
        if type(raw_result) is not NodeResult:
            self._set_node_state(node.id, NodeState.FAILED, "RESULT_INVALID")
            self._emit(
                "node_finished",
                status=NodeState.FAILED.value,
                reason_code="RESULT_INVALID",
                node=node,
                profile_alias=attempt.profile_alias,
            )
            return
        try:
            raw_result = NodeResult.from_value(raw_result)
            self._validate_result(node, attempt, raw_result)
        except Exception:
            self._set_node_state(node.id, NodeState.FAILED, "RESULT_INVALID")
            self._emit(
                "node_finished",
                status=NodeState.FAILED.value,
                reason_code="RESULT_INVALID",
                node=node,
                profile_alias=attempt.profile_alias,
            )
            return
        if raw_result.status is ResultStatus.SUCCEEDED:
            try:
                if node.role is NodeRole.INTEGRATOR:
                    self._validate_integrator_result(node, raw_result)
            except Exception:
                self._set_node_state(node.id, NodeState.FAILED, "INTEGRATION_INCOMPLETE")
                self._emit(
                    "node_finished",
                    status=NodeState.FAILED.value,
                    reason_code="INTEGRATION_INCOMPLETE",
                    node=node,
                    profile_alias=attempt.profile_alias,
                )
                return
            reference = self._reference_for(raw_result, attempt.route_receipt)
            self._result_refs[node.id] = reference
            self._total_tokens += raw_result.token_count
            self._total_cost += raw_result.cost_microunits
            duration_ms = max(0, int((self._monotonic() - attempt.started_at) * 1000))
            self._set_node_state(node.id, NodeState.SUCCEEDED, "NODE_SUCCEEDED")
            self._emit(
                "node_finished",
                status=NodeState.SUCCEEDED.value,
                reason_code="NODE_SUCCEEDED",
                node=node,
                profile_alias=attempt.profile_alias,
                duration_ms=duration_ms,
                token_count=raw_result.token_count,
                cost_microunits=raw_result.cost_microunits,
                artifacts=raw_result.artifacts,
            )
            return
        if (
            raw_result.status is ResultStatus.TRANSIENT_FAILURE
            and raw_result.unresolved
            and set(raw_result.unresolved).issubset(_TRANSIENT_FAILURE_REASONS)
            and attempt.attempt < node.max_attempts
        ):
            self._set_node_state(node.id, NodeState.RETRY_WAIT, "TRANSIENT_RETRY")
            self._emit(
                "node_retry_scheduled",
                status=NodeState.RETRY_WAIT.value,
                reason_code="TRANSIENT_RETRY",
                node=node,
                profile_alias=attempt.profile_alias,
            )
            self._set_node_state(node.id, NodeState.PENDING, "TRANSIENT_RETRY")
            return
        if raw_result.status is ResultStatus.REROUTE_REQUIRED:
            if (
                attempt.attempt < node.max_attempts
                and node.reroute_to is not None
                and _REROUTE_EDGES.get(attempt.profile_alias) == node.reroute_to
            ):
                self._profiles[node.id] = node.reroute_to
                self._set_node_state(node.id, NodeState.RETRY_WAIT, "REROUTED")
                self._emit(
                    "node_rerouted",
                    status=NodeState.RETRY_WAIT.value,
                    reason_code="REROUTED",
                    node=node,
                    profile_alias=node.reroute_to,
                )
                self._set_node_state(node.id, NodeState.PENDING, "REROUTED")
                return
        if raw_result.status is ResultStatus.CANCELLED:
            self._set_node_state(node.id, NodeState.CANCELLED, "CANCELLED_USER_REDIRECT")
            self._emit(
                "node_finished",
                status=NodeState.CANCELLED.value,
                reason_code="CANCELLED_USER_REDIRECT",
                node=node,
                profile_alias=attempt.profile_alias,
            )
            return
        reason = "ATTEMPTS_EXHAUSTED" if raw_result.status is ResultStatus.TRANSIENT_FAILURE else "NODE_FAILED"
        self._set_node_state(node.id, NodeState.FAILED, reason)
        self._emit(
            "node_finished",
            status=NodeState.FAILED.value,
            reason_code=reason,
            node=node,
            profile_alias=attempt.profile_alias,
        )

    def _check_timeouts(self, running: Mapping[Future[Any], _Attempt]) -> None:
        now = self._monotonic()
        for attempt in running.values():
            if attempt.timed_out or attempt.cancelled or attempt.future is None or attempt.future.done():
                continue
            if now - attempt.started_at >= attempt.node.timeout_seconds:
                attempt.timed_out = True
                attempt.local_cancel.set()
                self._set_node_state(attempt.node.id, NodeState.TIMED_OUT, "TIMED_OUT")
                self._emit(
                    "node_finished",
                    status=NodeState.TIMED_OUT.value,
                    reason_code="TIMED_OUT",
                    node=attempt.node,
                    profile_alias=attempt.profile_alias,
                    duration_ms=max(0, int((now - attempt.started_at) * 1000)),
                )

    def _run_scheduler(
        self,
        runner: Callable[[NodeContext, CancellationToken], NodeResult],
        *,
        serial_fallback: bool,
        route_observer: Callable[[Any, Any], Iterable[Any]] | None,
    ) -> None:
        slots = 1 if serial_fallback else min(self.manifest.max_concurrency, MAX_GRAPH_CONCURRENCY)
        running: dict[Future[Any], _Attempt] = {}
        with ThreadPoolExecutor(max_workers=slots, thread_name_prefix="m7-local") as executor:
            while True:
                if self._cancel_event.is_set():
                    self._emit_cancel_requested()
                    self._cancel_pending_and_active(running)
                else:
                    self._block_downstream()
                    while len(running) < slots:
                        ready = self._ready_nodes()
                        if not ready or self._cancel_event.is_set():
                            break
                        node = ready[0]
                        attempt = self._start_attempt(executor, node, route_observer=route_observer)
                        if attempt is None:
                            continue
                        if self._cancel_event.is_set():
                            self._emit_cancel_requested()
                            self._cancel_pending_and_active(running)
                            attempt.cancelled = True
                            attempt.local_cancel.set()
                            self._set_node_state(node.id, NodeState.CANCELLED, "CANCELLED_USER_REDIRECT")
                            self._emit(
                                "node_blocked",
                                status=NodeState.CANCELLED.value,
                                reason_code="CANCELLED_USER_REDIRECT",
                                node=node,
                                profile_alias=attempt.profile_alias,
                            )
                            continue
                        token = CancellationToken(self._cancel_event, attempt.local_cancel)
                        attempt.future = executor.submit(self._call_runner, runner, attempt.context, token)
                        running[attempt.future] = attempt
                        self._max_active_workers = max(self._max_active_workers, len(running))

                completed = [future for future in running if future.done()]
                for future in completed:
                    attempt = running.pop(future)
                    self._settle_attempt(attempt)
                self._check_timeouts(running)

                nonterminal = [state for state in self._node_states.values() if state not in _TERMINAL_NODE_STATES]
                if not running and not nonterminal:
                    return
                if not running and nonterminal:
                    if not self._cancel_event.is_set() and self._ready_nodes():
                        continue
                    if self._cancel_event.is_set():
                        self._cancel_pending_and_active({})
                        return
                    # A validated DAG cannot deadlock; this makes a future regression fail closed.
                    for node in self.manifest.nodes:
                        if self._node_states[node.id] not in _TERMINAL_NODE_STATES:
                            self._set_node_state(node.id, NodeState.BLOCKED, "BLOCKED_DEPENDENCY")
                            self._emit(
                                "node_blocked",
                                status=NodeState.BLOCKED.value,
                                reason_code="BLOCKED_DEPENDENCY",
                                node=node,
                                profile_alias=self._profiles[node.id],
                            )
                    return
                if running and not completed:
                    wait(tuple(running), timeout=0.01)

    def _close_receipt(self, admission: AdmissionDecision, started_at: float, serial_fallback: bool) -> GraphRunReceipt:
        if self._cancel_event.is_set() or any(state is NodeState.CANCELLED for state in self._node_states.values()):
            self._state = GraphState.CANCELLED
            reason = "GRAPH_CANCELLED"
        elif any(state in {NodeState.BLOCKED, NodeState.QUARANTINED_ROUTE} for state in self._node_states.values()):
            self._state = GraphState.BLOCKED
            reason = "GRAPH_BLOCKED"
        elif any(state in {NodeState.FAILED, NodeState.TIMED_OUT} for state in self._node_states.values()):
            self._state = GraphState.FAILED
            reason = "GRAPH_FAILED"
        elif all(state is NodeState.SUCCEEDED for state in self._node_states.values()):
            self._state = GraphState.SUCCEEDED
            reason = "GRAPH_SUCCEEDED"
        else:
            self._state = GraphState.BLOCKED
            reason = "GRAPH_BLOCKED"
        duration_ms = max(0, int((self._monotonic() - started_at) * 1000))
        self._emit(
            "graph_closed",
            status=self._state.value,
            reason_code=reason,
            duration_ms=duration_ms,
            token_count=self._total_tokens,
            cost_microunits=self._total_cost,
        )
        return GraphRunReceipt(
            run_id=self._run_id or "graph:unavailable",
            task_id_digest=_digest_task_id(self.manifest.task_id),
            manifest_digest=self.manifest.manifest_digest,
            admission_digest=admission.decision_digest,
            lane=self.manifest.lane,
            state=self._state,
            nodes=tuple(
                NodeRunReceipt(
                    node_id=node.id,
                    state=self._node_states[node.id],
                    attempts=self._attempts[node.id],
                    profile_alias=self._profiles[node.id],
                    reason_code=self._node_reasons[node.id],
                    route_receipt_digest=self._route_digests[node.id],
                )
                for node in self.manifest.nodes
            ),
            final_result=self._result_refs.get(self.manifest.final_node),
            events=tuple(self._events),
            serial_fallback=serial_fallback,
            max_active_workers=self._max_active_workers,
            duration_ms=duration_ms,
            total_tokens=self._total_tokens,
            total_cost_microunits=self._total_cost,
        )

    def run(
        self,
        admission: AdmissionDecision,
        runner: Callable[[NodeContext, CancellationToken], NodeResult],
        *,
        serial_fallback: bool = False,
        route_observer: Callable[[Any, Any], Iterable[Any]] | None = None,
    ) -> GraphRunReceipt:
        """Run the one validated synthetic graph and return a redacted receipt."""

        if getattr(_RUNTIME_LOCAL, "worker_active", False):
            raise GraphPolicyError("workers cannot create nested graph runtimes")
        if not callable(runner):
            raise GraphPolicyError("M7 requires an injected synthetic runner callable")
        if route_observer is not None and not callable(route_observer):
            raise GraphPolicyError("route observer must be an injected callable")
        if not isinstance(serial_fallback, bool):
            raise GraphPolicyError("serial fallback flag is invalid")
        _reserve_root_graph_run()
        try:
            with self._lock:
                if self._state is not GraphState.NEW:
                    raise GraphPolicyError("graph runtime cannot resume or rerun a prior lifecycle")
                self._validate_admission(admission)
                self._run_id = self._new_run_id()
                self._state = GraphState.VALIDATED
            started_at = self._monotonic()
            try:
                self._emit(
                    "graph_validated",
                    status=GraphState.VALIDATED.value,
                    reason_code="GRAPH_VALIDATED",
                )
                if serial_fallback:
                    self._emit(
                        "serial_fallback",
                        status=GraphState.VALIDATED.value,
                        reason_code="SERIAL_FALLBACK",
                    )
                if self._cancel_event.is_set():
                    self._emit_cancel_requested()
                    self._cancel_pending_and_active({})
                    return self._close_receipt(admission, started_at, serial_fallback)
                self._state = GraphState.RUNNING
                self._run_scheduler(runner, serial_fallback=serial_fallback, route_observer=route_observer)
                return self._close_receipt(admission, started_at, serial_fallback)
            except GraphPolicyError:
                self._cancel_event.set()
                self._state = GraphState.BLOCKED
                raise
        finally:
            _release_root_graph_run()
