"""Practical V1 local bridge and operator-trusted router-log evidence.

The bridge is deliberately narrow. It performs explicit read-only recovery or
knowledge queries and can persist only a pending project-to-global promotion
proposal. It never commits an authority-store mutation, discovers roots, reads
transcripts, or calls a provider.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping, Sequence
import uuid

from .canonical import sha256_bytes, sha256_hex
from .errors import PathUnsafeError, StorageError
from .knowledge import ProjectPromotionOutbox, PromotionRequest
from .parsing import parse_ndjson, parse_strict_json
from .profiles import APPROVED_PROFILE_ALIASES, SUPPORTED_EFFORTS, resolve_profile
from .recovery import ProjectRecoveryKernel, QueryRequest
from .retrieval import FederatedQueryRequest, FederatedRetriever, StoreRegistry
from .storage import Store
from .workspace import repository_root


PRACTICAL_V1_VERSION = "practical-v1/1"
PRACTICAL_ROUTE_EVIDENCE_VERSION = "practical-v1-router-evidence/1"
PRACTICAL_PROPOSAL_KIND = "project-to-global-promotion"
DIRECT_NO_MEMORY = "DIRECT_NO_MEMORY"

_MEMORY_LANES = frozenset(("ASSISTED", "GRAPH", "DEEP"))
_SAFE_RELATIVE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_CORRELATION_ID = re.compile(r"^corr:[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_ROUTER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ROUTE_PLAN_FIELDS = frozenset(
    ("schema_version", "evidence_kind", "router_id", "trust_basis", "expectations")
)
_ROUTE_EXPECTATION_FIELDS = frozenset(
    ("correlation_id", "expected_profile", "requested_effort")
)
_ROUTE_LOG_FIELDS = frozenset(
    (
        "schema_version",
        "event_type",
        "router_id",
        "correlation_id",
        "reported_profile",
        "requested_effort",
        "normalized_effort",
        "outbound_effort",
    )
)
_SOURCE_BINDING_FIXED = (
    Path("config/model-profiles.json"),
    Path("scripts/practical_v1_bridge.py"),
)


def _exact_fields(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    if set(value) != expected:
        raise StorageError("SCHEMA_INVALID", f"{label} fields are invalid")


def _require_text(value: Any, name: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise StorageError("SCHEMA_INVALID", f"{name} must be bounded non-empty text")
    return value


def _direct_result(operation: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "bridge_version": PRACTICAL_V1_VERSION,
        "operation": operation,
        "status": DIRECT_NO_MEMORY,
        "memory_accessed": False,
        "durable_write_performed": False,
    }


def _memory_lane(lane: str) -> str | None:
    normalized = _require_text(lane, "lane", maximum=16).upper()
    if normalized == "DIRECT":
        return None
    if normalized not in _MEMORY_LANES:
        raise StorageError("SCHEMA_INVALID", "lane is not supported by the Practical V1 bridge")
    return normalized


def _inspect_directory_chain(root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise PathUnsafeError("path escapes its declared boundary") from error
    current = root
    for component in relative.parts:
        current = current / component
        try:
            metadata = os.lstat(current)
        except OSError as error:
            raise PathUnsafeError("path cannot be inspected safely") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PathUnsafeError("path crosses a symlink or non-directory")


def _reject_lexical_symlinks(candidate: Path, label: str, *, allow_missing: bool = False) -> None:
    """Reject a caller's literal path if any existing component is a symlink.

    Resolving first loses the component that redirected the path.  The bridge
    accepts only canonical, non-symlink roots so an explicit root cannot be
    swapped for an alias after the caller has selected it.
    """

    if not candidate.is_absolute():
        raise PathUnsafeError(f"{label} must be an explicit absolute path")
    parts = candidate.parts
    current = Path(candidate.anchor)
    components = parts[1:]
    for index, component in enumerate(components):
        if component in {"", ".", ".."}:
            raise PathUnsafeError(f"{label} has unsafe path components")
        current = current / component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError as error:
            if allow_missing:
                return
            raise PathUnsafeError(f"{label} is unavailable") from error
        except OSError as error:
            raise PathUnsafeError(f"{label} cannot be inspected safely") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise PathUnsafeError(f"{label} crosses a symlink")
        if index < len(components) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise PathUnsafeError(f"{label} has a non-directory parent")


def _require_regular_git_head(git_directory: Path) -> None:
    """Confirm the minimum on-disk shape of a Git directory without running Git."""

    try:
        metadata = os.lstat(git_directory)
        head_metadata = os.lstat(git_directory / "HEAD")
    except OSError as error:
        raise PathUnsafeError("project root is not a Git worktree root") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(head_metadata.st_mode)
        or not stat.S_ISREG(head_metadata.st_mode)
        or head_metadata.st_size > 65_536
    ):
        raise PathUnsafeError("project root is not a Git worktree root")


def _require_git_worktree_root(root: Path) -> None:
    """Require the exact root of a normal or linked Git worktree."""

    marker = root / ".git"
    try:
        metadata = os.lstat(marker)
    except OSError as error:
        raise PathUnsafeError("project root must be an exact Git worktree root") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise PathUnsafeError("project root Git metadata cannot be a symlink")
    if stat.S_ISDIR(metadata.st_mode):
        _require_regular_git_head(marker)
        return
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
        raise PathUnsafeError("project root must be an exact Git worktree root")
    try:
        lines = marker.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise PathUnsafeError("project root Git metadata is invalid") from error
    if len(lines) != 1 or not lines[0].startswith("gitdir: "):
        raise PathUnsafeError("project root Git worktree file is invalid")
    location = lines[0].removeprefix("gitdir: ")
    if not location or "\x00" in location:
        raise PathUnsafeError("project root Git worktree file is invalid")
    candidate = Path(location)
    if not candidate.is_absolute():
        candidate = root / candidate
    lexical_git_directory = Path(os.path.abspath(candidate))
    _reject_lexical_symlinks(lexical_git_directory, "project root Git directory")
    try:
        git_directory = lexical_git_directory.resolve(strict=True)
    except OSError as error:
        raise PathUnsafeError("project root Git directory is unavailable") from error
    _require_regular_git_head(git_directory)


def validate_runtime_root(value: str | Path) -> Path:
    """Validate one explicit, private runtime root inside this source repository."""

    candidate = Path(value)
    if not candidate.is_absolute():
        raise PathUnsafeError("runtime root must be an explicit absolute path")
    _reject_lexical_symlinks(candidate, "runtime root")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise PathUnsafeError("runtime root is unavailable") from error
    source_root = repository_root().resolve()
    if resolved == source_root:
        raise PathUnsafeError("repository root cannot be used as the Practical V1 runtime root")
    try:
        resolved.relative_to(source_root)
    except ValueError as error:
        raise PathUnsafeError("runtime root must remain inside the Second Brain repository") from error
    _inspect_directory_chain(source_root, resolved)
    metadata = os.lstat(resolved)
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise PathUnsafeError("runtime root must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PathUnsafeError("runtime root must be private to the current user")
    return resolved


def validate_project_root(value: str | Path) -> Path:
    """Validate an operator-supplied project root without scanning for projects."""

    candidate = Path(value)
    if not candidate.is_absolute():
        raise PathUnsafeError("project root must be an explicit absolute path")
    _reject_lexical_symlinks(candidate, "project root")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise PathUnsafeError("project root is unavailable") from error
    if not resolved.is_dir():
        raise PathUnsafeError("project root must be a directory")
    filesystem_root = Path(resolved.anchor)
    home = Path.home().resolve()
    source_root = repository_root().resolve()
    if resolved in {filesystem_root, home}:
        raise PathUnsafeError("project root is too broad")
    if resolved != source_root:
        try:
            source_root.relative_to(resolved)
        except ValueError:
            pass
        else:
            raise PathUnsafeError("project root cannot be an ancestor of the Second Brain source")
    metadata = os.lstat(resolved)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PathUnsafeError("project root is unsafe")
    _require_git_worktree_root(resolved)
    return resolved


def _runtime_child(runtime_root: Path, value: str | Path, label: str, *, must_exist: bool = True) -> Path:
    raw = str(value)
    relative = Path(raw)
    if (
        not raw
        or len(raw) > 256
        or "\\" in raw
        or relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or _SAFE_RELATIVE.fullmatch(raw) is None
    ):
        raise PathUnsafeError(f"{label} must be a safe runtime-relative path")
    lexical = runtime_root / relative
    _reject_lexical_symlinks(lexical, label, allow_missing=not must_exist)
    candidate = lexical.resolve(strict=False)
    try:
        candidate.relative_to(runtime_root)
    except ValueError as error:
        raise PathUnsafeError(f"{label} escapes the runtime root") from error
    if must_exist:
        try:
            candidate = candidate.resolve(strict=True)
        except OSError as error:
            raise PathUnsafeError(f"{label} is unavailable") from error
        _inspect_directory_chain(runtime_root, candidate)
    return candidate


def _snapshot_status(store: Store, role: str, relative_root: str) -> dict[str, Any]:
    snapshot = store.snapshot()
    if role == "global":
        valid_identity = snapshot.store_id == "knowledge:global" and snapshot.project_id is None
    else:
        valid_identity = (
            snapshot.project_id is not None
            and snapshot.store_id == f"project:{snapshot.project_id}"
        )
    if not valid_identity:
        raise StorageError("AUTHORITY_DENIED", "status store role does not match its authority identity")
    return {
        "role": role,
        "relative_root": relative_root,
        "store_id": snapshot.store_id,
        "project_id": snapshot.project_id,
        "health": store.health,
        "mutation_epoch": snapshot.mutation_epoch,
        "event_head": snapshot.event_head,
        "object_count": snapshot.object_count,
        "snapshot_digest": sha256_hex(
            {
                "store_id": snapshot.store_id,
                "project_id": snapshot.project_id,
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
        ),
    }


def bridge_status(
    *,
    runtime_root: str | Path,
    project_store: str | None = None,
    global_store: str | None = None,
) -> dict[str, Any]:
    root = validate_runtime_root(runtime_root)
    stores: list[dict[str, Any]] = []
    if project_store is not None:
        path = _runtime_child(root, project_store, "project store")
        stores.append(_snapshot_status(Store.open(path), "project", project_store))
    if global_store is not None:
        path = _runtime_child(root, global_store, "global store")
        stores.append(_snapshot_status(Store.open(path), "global", global_store))
    if not stores:
        raise StorageError("SCHEMA_INVALID", "status requires an explicit project or global store")
    return {
        "schema_version": 1,
        "bridge_version": PRACTICAL_V1_VERSION,
        "operation": "status",
        "status": "PASS",
        "runtime_root_digest": sha256_hex({"runtime_root": str(root)}),
        "direct_default": DIRECT_NO_MEMORY,
        "stores": stores,
    }


def _query_id(namespace: str, *values: str) -> str:
    material = "/".join((namespace, *(sha256_hex({"value": value}) for value in values)))
    return f"qry:{uuid.uuid5(uuid.NAMESPACE_URL, 'second-brain/practical-v1/' + material)}"


def project_recovery_read(
    *,
    lane: str = "DIRECT",
    runtime_root: str | Path,
    project_store: str,
    project_id: str,
    project_root: str | Path,
    query_text: str,
) -> dict[str, Any]:
    admitted_lane = _memory_lane(lane)
    if admitted_lane is None:
        return _direct_result("project-recovery-read")
    project = _require_text(project_id, "project_id", maximum=64)
    root = validate_runtime_root(runtime_root)
    source_project = validate_project_root(project_root)
    store = Store.open(_runtime_child(root, project_store, "project store"))
    snapshot = store.snapshot()
    if snapshot.store_id != f"project:{project}" or snapshot.project_id != project:
        raise StorageError("AUTHORITY_DENIED", "project store identity does not match the explicit project")
    request = QueryRequest.from_value(
        {
            "schema_version": 1,
            "query_id": _query_id("project", project, query_text),
            "text": query_text,
            "scope": {
                "project_ids": [project],
                "include_global": False,
                "branch": None,
                "repository_snapshot": None,
            },
            "filters": {"kinds": [], "lifecycle": ["active"], "authorities": [], "tags_any": []},
            "freshness_policy": "include_with_warning",
            "relation": {"max_depth": 1, "max_fanout": 8, "types": []},
            "budget": {
                "candidate_limit": 32,
                "object_limit": 12,
                "token_limit": 8000,
                "byte_limit": 32768,
                "timeout_ms": 2000,
            },
            "purpose": "recovery",
        }
    )
    kernel = ProjectRecoveryKernel(
        store,
        source_project,
        project_boundary=source_project,
    )
    envelope = kernel.retrieve(request)
    return {
        "schema_version": 1,
        "bridge_version": PRACTICAL_V1_VERSION,
        "operation": "project-recovery-read",
        "status": "PASS",
        "lane": admitted_lane,
        "memory_accessed": True,
        "durable_write_performed": False,
        "project_id": project,
        "project_root_digest": sha256_hex({"project_root": str(source_project)}),
        "result": envelope.to_dict(),
    }


def global_knowledge_read(
    *,
    lane: str = "DIRECT",
    runtime_root: str | Path,
    global_store: str,
    query_text: str,
) -> dict[str, Any]:
    admitted_lane = _memory_lane(lane)
    if admitted_lane is None:
        return _direct_result("global-knowledge-read")
    root = validate_runtime_root(runtime_root)
    store = Store.open(_runtime_child(root, global_store, "global store"))
    snapshot = store.snapshot()
    if snapshot.store_id != "knowledge:global" or snapshot.project_id is not None:
        raise StorageError("AUTHORITY_DENIED", "global store identity is invalid")
    registry = StoreRegistry()
    registry.register_global(store)
    request = FederatedQueryRequest.for_lane(
        admitted_lane,
        {
            "schema_version": 1,
            "query_id": _query_id("global", query_text),
            "text": query_text,
            "tier": "R2",
            "scope": {
                "project_ids": [],
                "include_global": True,
                "branch": None,
                "repository_snapshot": None,
            },
            "filters": {
                "kinds": [],
                "lifecycle": ["active", "superseded", "archived"],
                "authorities": [],
                "tags_any": [],
            },
            "freshness_policy": "include_with_warning",
            "relation": {"max_depth": 1, "max_fanout": 8, "types": []},
            "budget": {
                "candidate_limit": 32,
                "object_limit": 12,
                "token_limit": 8000,
                "byte_limit": 32768,
                "timeout_ms": 2000,
            },
            "purpose": "scoped_task",
        },
    )
    if request is None:
        raise StorageError("DIRECT_NO_RETRIEVAL", "DIRECT requests cannot reach the global store")
    envelope = FederatedRetriever(registry).retrieve(request)
    return {
        "schema_version": 1,
        "bridge_version": PRACTICAL_V1_VERSION,
        "operation": "global-knowledge-read",
        "status": "PASS",
        "lane": admitted_lane,
        "memory_accessed": True,
        "durable_write_performed": False,
        "result": envelope.to_dict(),
    }


def propose_durable_write(
    *,
    lane: str = "DIRECT",
    runtime_root: str | Path,
    project_store: str,
    project_id: str,
    project_object_id: str,
    target_kind: str,
    idempotency_key: str,
    reason_code: str,
    proposal_only: bool,
) -> dict[str, Any]:
    admitted_lane = _memory_lane(lane)
    if admitted_lane is None:
        return _direct_result("propose-write")
    if proposal_only is not True:
        raise StorageError("AUTHORITY_DENIED", "proposal-only confirmation is required")
    if _SAFE_TOKEN.fullmatch(idempotency_key) is None:
        raise StorageError("SCHEMA_INVALID", "idempotency_key must be an opaque safe token")
    if _REASON_CODE.fullmatch(reason_code) is None:
        raise StorageError("SCHEMA_INVALID", "reason_code must be an allowlisted-style code")
    project = _require_text(project_id, "project_id", maximum=64)
    root = validate_runtime_root(runtime_root)
    store = Store.open(_runtime_child(root, project_store, "project store"))
    snapshot = store.snapshot()
    if snapshot.store_id != f"project:{project}" or snapshot.project_id != project:
        raise StorageError("AUTHORITY_DENIED", "project store identity does not match the explicit project")
    source = next((item for item in snapshot.objects if item.id == project_object_id), None)
    if source is None:
        raise StorageError("OBJECT_NOT_FOUND", "proposal source object is unavailable")
    request = PromotionRequest(
        project_store_id=snapshot.store_id,
        project_object_id=source.id,
        revision=source.revision,
        content_hash=source.content_hash,
        target_kind=target_kind,
        provenance={
            "bridge_version": PRACTICAL_V1_VERSION,
            "proposal_kind": PRACTICAL_PROPOSAL_KIND,
            "reason_code": reason_code,
            "source_store_id": snapshot.store_id,
            "source_object_id": source.id,
            "source_revision": source.revision,
            "source_content_hash": source.content_hash,
        },
        idempotency_key=idempotency_key,
        proposed_by="agent:practical-v1-bridge",
        rationale=f"Practical V1 pending review: {reason_code}",
    )
    outbox = ProjectPromotionOutbox(root / "bridge-proposals")
    item = outbox.enqueue(request)
    return {
        "schema_version": 1,
        "bridge_version": PRACTICAL_V1_VERSION,
        "operation": "propose-write",
        "proposal_kind": PRACTICAL_PROPOSAL_KIND,
        "status": "PENDING_REVIEW",
        "lane": admitted_lane,
        "memory_accessed": True,
        "durable_write_performed": True,
        "authority_store_mutated": False,
        "proposal": item.to_dict(),
    }


def _route_expectation(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise StorageError("SCHEMA_INVALID", "route expectation must be an object")
    _exact_fields(value, _ROUTE_EXPECTATION_FIELDS, "route expectation")
    correlation = _require_text(value.get("correlation_id"), "correlation_id", maximum=165)
    profile = _require_text(value.get("expected_profile"), "expected_profile", maximum=64)
    effort = _require_text(value.get("requested_effort"), "requested_effort", maximum=16)
    if _CORRELATION_ID.fullmatch(correlation) is None:
        raise StorageError("SCHEMA_INVALID", "correlation_id is invalid")
    if profile not in APPROVED_PROFILE_ALIASES or effort not in SUPPORTED_EFFORTS:
        raise StorageError("SCHEMA_INVALID", "route expectation profile or effort is invalid")
    resolved = resolve_profile(profile)
    if effort != resolved.effort_intent:
        raise StorageError("SCHEMA_INVALID", "requested effort does not match the expected profile")
    return {
        "correlation_id": correlation,
        "expected_profile": profile,
        "requested_effort": effort,
    }


def validate_router_log_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StorageError("SCHEMA_INVALID", "router log plan must be an object")
    _exact_fields(value, _ROUTE_PLAN_FIELDS, "router log plan")
    if value.get("schema_version") != 1 or value.get("evidence_kind") != "practical-v1-router-log-plan":
        raise StorageError("SCHEMA_INVALID", "router log plan version or kind is invalid")
    router_id = _require_text(value.get("router_id"), "router_id", maximum=64)
    if _ROUTER_ID.fullmatch(router_id) is None:
        raise StorageError("SCHEMA_INVALID", "router_id is invalid")
    if value.get("trust_basis") != "operator-trusted-structured-outbound-log":
        raise StorageError("SCHEMA_INVALID", "router log plan trust basis is invalid")
    raw_expectations = value.get("expectations")
    if not isinstance(raw_expectations, list) or not 1 <= len(raw_expectations) <= 100:
        raise StorageError("SCHEMA_INVALID", "router log plan must contain one to 100 expectations")
    expectations = [_route_expectation(item) for item in raw_expectations]
    correlations = [item["correlation_id"] for item in expectations]
    if len(correlations) != len(set(correlations)):
        raise StorageError("SCHEMA_INVALID", "router log plan correlations must be unique")
    return {
        "schema_version": 1,
        "evidence_kind": "practical-v1-router-log-plan",
        "router_id": router_id,
        "trust_basis": "operator-trusted-structured-outbound-log",
        "expectations": expectations,
    }


def _route_log_record(value: Any) -> dict[str, str | int]:
    if not isinstance(value, Mapping):
        raise StorageError("SCHEMA_INVALID", "router log record must be an object")
    _exact_fields(value, _ROUTE_LOG_FIELDS, "router log record")
    if value.get("schema_version") != 1 or value.get("event_type") != "outbound_request":
        raise StorageError("SCHEMA_INVALID", "router log event type is invalid")
    router_id = _require_text(value.get("router_id"), "router_id", maximum=64)
    correlation = _require_text(value.get("correlation_id"), "correlation_id", maximum=165)
    profile = _require_text(value.get("reported_profile"), "reported_profile", maximum=64)
    efforts = {
        name: _require_text(value.get(name), name, maximum=16)
        for name in ("requested_effort", "normalized_effort", "outbound_effort")
    }
    if _ROUTER_ID.fullmatch(router_id) is None or _CORRELATION_ID.fullmatch(correlation) is None:
        raise StorageError("SCHEMA_INVALID", "router log identity is invalid")
    if profile not in APPROVED_PROFILE_ALIASES or any(item not in SUPPORTED_EFFORTS for item in efforts.values()):
        raise StorageError("SCHEMA_INVALID", "router log profile or effort is invalid")
    return {
        "schema_version": 1,
        "event_type": "outbound_request",
        "router_id": router_id,
        "correlation_id": correlation,
        "reported_profile": profile,
        **efforts,
    }


def verify_operator_router_log(
    plan: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    *,
    log_sha256: str | None = None,
) -> dict[str, Any]:
    normalized_plan = validate_router_log_plan(plan)
    normalized_observations = [_route_log_record(item) for item in observations]
    expected = {item["correlation_id"]: item for item in normalized_plan["expectations"]}
    observed: dict[str, dict[str, Any]] = {}
    reason_codes: set[str] = set()
    for item in normalized_observations:
        correlation = str(item["correlation_id"])
        if correlation in observed:
            reason_codes.add("DUPLICATE_CORRELATION")
            continue
        observed[correlation] = item
        if correlation not in expected:
            reason_codes.add("UNEXPECTED_CORRELATION")
    matched: list[str] = []
    for correlation, expectation in expected.items():
        observation = observed.get(correlation)
        if observation is None:
            reason_codes.add("MISSING_CORRELATION")
            continue
        item_reasons: set[str] = set()
        if observation["router_id"] != normalized_plan["router_id"]:
            item_reasons.add("ROUTER_ID_MISMATCH")
        if observation["reported_profile"] != expectation["expected_profile"]:
            item_reasons.add("PROFILE_MISMATCH")
        if observation["requested_effort"] != expectation["requested_effort"]:
            item_reasons.add("REQUESTED_EFFORT_MISMATCH")
        if observation["normalized_effort"] != expectation["requested_effort"]:
            item_reasons.add("NORMALIZED_EFFORT_MISMATCH")
        if observation["outbound_effort"] != expectation["requested_effort"]:
            item_reasons.add("OUTBOUND_EFFORT_MISMATCH")
        reason_codes.update(item_reasons)
        if not item_reasons:
            matched.append(correlation)
    canonical_observations = [deepcopy(dict(item)) for item in normalized_observations]
    report = {
        "schema_version": 1,
        "writer_version": PRACTICAL_ROUTE_EVIDENCE_VERSION,
        "evidence_kind": "operator-trusted-router-outbound-report",
        "status": "PASS" if not reason_codes else "FAIL",
        "reason_codes": sorted(reason_codes),
        "router_id": normalized_plan["router_id"],
        "trust_basis": normalized_plan["trust_basis"],
        "plan_digest": sha256_hex(normalized_plan),
        "router_log_sha256": log_sha256 or sha256_hex(canonical_observations),
        "expected_count": len(expected),
        "observed_count": len(normalized_observations),
        "matched_count": len(matched),
        "correlation_digests": sorted(sha256_hex({"correlation_id": item}) for item in expected),
        "proves": "what the operator-trusted router reports and sends on its outbound boundary",
        "provider_attestation": False,
        "strict_upstream_attestation_satisfied": False,
        "limitations": [
            "does-not-prove-remote-provider-internal-model-or-effort",
            "depends-on-operator-trust-in-router-and-log-integrity",
            "upstream-retention-remains-unverified-from-local-configuration",
            "strict-signed-upstream-attestation-remains-a-future-hardening-gate",
        ],
    }
    report["report_digest"] = sha256_hex(report)
    return report


def _bounded_evidence_file(root: Path, relative_value: str | Path) -> tuple[Path, bytes]:
    relative = Path(relative_value)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in str(relative)
    ):
        raise PathUnsafeError("evidence file must be a safe relative path")
    lexical = root / relative
    current = root
    for index, component in enumerate(relative.parts):
        current = current / component
        try:
            metadata = os.lstat(current)
        except OSError as error:
            raise PathUnsafeError("evidence file is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise PathUnsafeError("evidence file path crosses a symlink")
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise PathUnsafeError("evidence file parent is not a directory")
    candidate = lexical.resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise PathUnsafeError("evidence file escapes its declared root") from error
    metadata = candidate.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1_048_576:
        raise PathUnsafeError("evidence file is not a bounded regular file")
    return candidate, candidate.read_bytes()


def verify_operator_router_log_files(
    *,
    evidence_root: str | Path,
    plan_path: str | Path,
    log_path: str | Path,
) -> dict[str, Any]:
    root_candidate = Path(evidence_root)
    if not root_candidate.is_absolute():
        raise PathUnsafeError("evidence root must be an explicit absolute path")
    _reject_lexical_symlinks(root_candidate, "evidence root")
    try:
        root = root_candidate.resolve(strict=True)
    except OSError as error:
        raise PathUnsafeError("evidence root is unavailable") from error
    if not root.is_dir() or root.is_symlink():
        raise PathUnsafeError("evidence root must be a non-symlink directory")
    _, raw_plan = _bounded_evidence_file(root, plan_path)
    _, raw_log = _bounded_evidence_file(root, log_path)
    try:
        plan = parse_strict_json(raw_plan.decode("utf-8"))
        records = parse_ndjson(raw_log.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise StorageError("PARSE_INVALID", "router evidence must be UTF-8") from error
    return verify_operator_router_log(plan, records, log_sha256=sha256_bytes(raw_log))


def practical_v1_source_tree_digest(root: str | Path | None = None) -> tuple[str, int]:
    """Bind the staged wrapper to the complete local Python/schema runtime tree."""

    source_root = repository_root().resolve() if root is None else Path(root).resolve(strict=True)
    relative_paths = list(_SOURCE_BINDING_FIXED)
    relative_paths.extend(path.relative_to(source_root) for path in sorted((source_root / "src/second_brain").glob("*.py")))
    relative_paths.extend(path.relative_to(source_root) for path in sorted((source_root / "schemas").glob("*.json")))
    digest = hashlib.sha256()
    seen: set[str] = set()
    count = 0
    for relative in sorted(relative_paths, key=lambda item: item.as_posix()):
        name = relative.as_posix()
        if name in seen:
            continue
        seen.add(name)
        path = source_root / relative
        try:
            metadata = path.lstat()
        except OSError as error:
            raise StorageError("INTEGRITY_FAILED", "source binding file is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PathUnsafeError("source binding includes an unsafe file")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\0")
        count += 1
    return digest.hexdigest(), count
