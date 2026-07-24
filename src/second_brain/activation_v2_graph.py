"""Derived-only synthetic focus graph views for Activation V2 A5.

The graph view compiler consumes an already-validated synthetic graph snapshot
and emits a bounded JSON view into a disposable runtime. It never opens an
authority store, starts a UI server, or treats a rendered graph as evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
import time
from typing import Any, Mapping
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


FOCUS_GRAPH_VIEW_VERSION = 1
MAX_FOCUS_NODES = 16
MAX_FOCUS_EDGES = 32
MAX_VIEW_LATENCY_MS = 1_000
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_PROJECT_OBJECT = re.compile(
    r"^mem:([a-z0-9][a-z0-9._-]{0,63}):(project|decision|component|task|bug|"
    r"experiment|evidence|question):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
_GLOBAL_OBJECT = re.compile(
    r"^kb:global:(source|entity|concept|claim|synthesis):[0-9a-f]{8}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_RELATIONS = frozenset(
    (
        "depends_on",
        "implements",
        "verifies",
        "supports",
        "contradicts",
        "derived_from",
        "about",
        "part_of",
        "supersedes",
        "refines",
        "related_to",
    )
)
_KINDS = frozenset(
    (
        "source",
        "entity",
        "concept",
        "claim",
        "synthesis",
        "project",
        "decision",
        "component",
        "task",
        "bug",
        "experiment",
        "evidence",
        "question",
    )
)
_FRESHNESS = frozenset(("fresh", "partial", "stale", "unverifiable", "not_applicable"))
_LIFECYCLES = frozenset(("active", "superseded", "archived", "deleted"))
_ACYCLIC_RELATIONS = frozenset(("part_of", "supersedes"))
_CROSS_STORE_RELATIONS = frozenset(("about", "derived_from", "implements", "supports", "related_to"))
_PROVENANCE_ID = re.compile(r"^prov:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_VIEW_NAMESPACE = uuid.UUID("f3f69d96-b2e7-5bc2-a20e-1a7dcbe0e5c5")


class DerivedGraphError(SemanticValidationError):
    """A derived-only graph/view request violates the A5 boundary."""


@dataclass(frozen=True)
class FocusGraphRequest:
    """A bounded focus selector with no free-text query or UI action."""

    request_id: str
    project_id: str
    focus_node_id: str
    max_hops: int
    max_nodes: int
    max_edges: int

    def __post_init__(self) -> None:
        if type(self) is not FocusGraphRequest:
            raise DerivedGraphError("focus graph request is invalid")
        _require_prefixed_uuid(self.request_id, "focus-graph-request", "focus graph request")
        _require_project_id(self.project_id, "focus graph request project")
        if not isinstance(self.focus_node_id, str):
            raise DerivedGraphError("focus graph focus node is invalid")
        if type(self.max_hops) is not int or self.max_hops not in {1, 2}:
            raise DerivedGraphError("focus graph hop budget is invalid")
        if type(self.max_nodes) is not int or not 1 <= self.max_nodes <= MAX_FOCUS_NODES:
            raise DerivedGraphError("focus graph node budget is invalid")
        if type(self.max_edges) is not int or not 0 <= self.max_edges <= MAX_FOCUS_EDGES:
            raise DerivedGraphError("focus graph edge budget is invalid")

    @classmethod
    def from_value(cls, value: object) -> "FocusGraphRequest":
        if isinstance(value, cls):
            return value
        expected = {"request_id", "project_id", "focus_node_id", "max_hops", "max_nodes", "max_edges"}
        if type(value) is not dict or set(value) != expected:
            raise DerivedGraphError("focus graph request has unsupported fields")
        try:
            return cls(**value)
        except (TypeError, DerivedGraphError) as error:
            raise DerivedGraphError("focus graph request is invalid") from error

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "project_id": self.project_id,
            "focus_node_id": self.focus_node_id,
            "max_hops": self.max_hops,
            "max_nodes": self.max_nodes,
            "max_edges": self.max_edges,
        }


@dataclass(frozen=True)
class FocusGraphNode:
    """A node copied from a public synthetic snapshot into a derived view."""

    node_id: str
    revision: int
    store_id: str
    kind: str
    lifecycle: str
    freshness: str
    label: str

    def __post_init__(self) -> None:
        if type(self) is not FocusGraphNode:
            raise DerivedGraphError("focus graph node is invalid")
        if not isinstance(self.node_id, str) or not isinstance(self.store_id, str):
            raise DerivedGraphError("focus graph node identity is invalid")
        if type(self.revision) is not int or self.revision < 1:
            raise DerivedGraphError("focus graph node revision is invalid")
        if self.kind not in _KINDS or self.lifecycle not in _LIFECYCLES:
            raise DerivedGraphError("focus graph node state is invalid")
        if self.freshness not in _FRESHNESS:
            raise DerivedGraphError("focus graph node freshness is invalid")
        if not isinstance(self.label, str) or not 1 <= len(self.label) <= 140 or "\n" in self.label or "\r" in self.label:
            raise DerivedGraphError("focus graph node label is invalid")
        validate_activation_v2_safe_content(self.label)

    def to_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "revision": self.revision,
            "store_id": self.store_id,
            "kind": self.kind,
            "lifecycle": self.lifecycle,
            "freshness": self.freshness,
            "label": self.label,
        }

    @classmethod
    def from_value(cls, value: object) -> "FocusGraphNode":
        expected = {"node_id", "revision", "store_id", "kind", "lifecycle", "freshness", "label"}
        if type(value) is not dict or set(value) != expected:
            raise DerivedGraphError("focus graph node has unsupported fields")
        try:
            return cls(**value)
        except (TypeError, DerivedGraphError) as error:
            raise DerivedGraphError("focus graph node is invalid") from error


@dataclass(frozen=True)
class FocusGraphEdge:
    """An edge displayed only when both snapshot endpoints are in the view."""

    edge_id: str
    relation: str
    source_id: str
    target_id: str
    source_revision: int
    target_revision: int
    cross_store: bool
    provenance_ids: tuple[str, ...]
    cross_store_provenance: "FocusGraphCrossStoreProvenance | None"

    def __post_init__(self) -> None:
        if type(self) is not FocusGraphEdge:
            raise DerivedGraphError("focus graph edge is invalid")
        _require_prefixed_uuid(self.edge_id, "edge", "focus graph edge")
        if self.relation not in _RELATIONS or not isinstance(self.source_id, str) or not isinstance(self.target_id, str):
            raise DerivedGraphError("focus graph edge is invalid")
        if self.source_id == self.target_id or type(self.cross_store) is not bool:
            raise DerivedGraphError("focus graph edge is invalid")
        _require_revision(self.source_revision, "focus graph edge source revision")
        _require_revision(self.target_revision, "focus graph edge target revision")
        if (
            not isinstance(self.provenance_ids, tuple)
            or not 1 <= len(self.provenance_ids) <= 16
            or len(set(self.provenance_ids)) != len(self.provenance_ids)
            or tuple(sorted(self.provenance_ids)) != self.provenance_ids
            or any(not isinstance(item, str) or _PROVENANCE_ID.fullmatch(item) is None for item in self.provenance_ids)
        ):
            raise DerivedGraphError("focus graph edge provenance is invalid")
        if self.cross_store:
            if type(self.cross_store_provenance) is not FocusGraphCrossStoreProvenance:
                raise DerivedGraphError("focus graph cross-store provenance is invalid")
        elif self.cross_store_provenance is not None:
            raise DerivedGraphError("same-store focus graph edge has cross-store provenance")

    def to_dict(self) -> dict[str, object]:
        return {
            "edge_id": self.edge_id,
            "relation": self.relation,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "source_revision": self.source_revision,
            "target_revision": self.target_revision,
            "cross_store": self.cross_store,
            "provenance_ids": list(self.provenance_ids),
            "cross_store_provenance": (
                self.cross_store_provenance.to_dict() if self.cross_store_provenance is not None else None
            ),
        }

    @classmethod
    def from_value(cls, value: object) -> "FocusGraphEdge":
        expected = {
            "edge_id",
            "relation",
            "source_id",
            "target_id",
            "source_revision",
            "target_revision",
            "cross_store",
            "provenance_ids",
            "cross_store_provenance",
        }
        if type(value) is not dict or set(value) != expected or not isinstance(value["provenance_ids"], list):
            raise DerivedGraphError("focus graph edge has unsupported fields")
        try:
            return cls(
                edge_id=value["edge_id"],
                relation=value["relation"],
                source_id=value["source_id"],
                target_id=value["target_id"],
                source_revision=value["source_revision"],
                target_revision=value["target_revision"],
                cross_store=value["cross_store"],
                provenance_ids=tuple(value["provenance_ids"]),
                cross_store_provenance=(
                    FocusGraphCrossStoreProvenance.from_value(value["cross_store_provenance"])
                    if value["cross_store_provenance"] is not None
                    else None
                ),
            )
        except (TypeError, DerivedGraphError) as error:
            raise DerivedGraphError("focus graph edge is invalid") from error


@dataclass(frozen=True)
class FocusGraphCrossStoreProvenance:
    """Opaque project-to-global provenance retained for the derived edge."""

    direction: str
    project_object_id: str
    project_object_revision: int
    repository_snapshot_digest: str

    def __post_init__(self) -> None:
        if type(self) is not FocusGraphCrossStoreProvenance:
            raise DerivedGraphError("focus graph cross-store provenance is invalid")
        if self.direction != "project_to_global":
            raise DerivedGraphError("focus graph cross-store direction is invalid")
        if _PROJECT_OBJECT.fullmatch(self.project_object_id) is None:
            raise DerivedGraphError("focus graph cross-store object is invalid")
        _require_revision(self.project_object_revision, "focus graph cross-store revision")
        _require_hash(self.repository_snapshot_digest, "focus graph cross-store digest")

    def to_dict(self) -> dict[str, object]:
        return {
            "direction": self.direction,
            "project_object_id": self.project_object_id,
            "project_object_revision": self.project_object_revision,
            "repository_snapshot_digest": self.repository_snapshot_digest,
        }

    @classmethod
    def from_value(cls, value: object) -> "FocusGraphCrossStoreProvenance":
        expected = {
            "direction",
            "project_object_id",
            "project_object_revision",
            "repository_snapshot_digest",
        }
        if type(value) is not dict or set(value) != expected:
            raise DerivedGraphError("focus graph cross-store provenance is invalid")
        try:
            return cls(**value)
        except (TypeError, DerivedGraphError) as error:
            raise DerivedGraphError("focus graph cross-store provenance is invalid") from error


@dataclass(frozen=True)
class FocusGraphView:
    """Digest-bound derived graph model; it has no authority or UI action."""

    schema_version: int
    view_id: str
    request_id: str
    source_snapshot_id: str
    source_snapshot_digest: str
    project_id: str
    focus_node_id: str
    max_hops: int
    max_nodes: int
    max_edges: int
    nodes: tuple[FocusGraphNode, ...]
    edges: tuple[FocusGraphEdge, ...]
    derived_only: bool
    fixture_only: bool
    ui_mode: str
    ui_server_started: bool
    authority_write: bool
    global_write: bool
    created_at: str
    view_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not FocusGraphView:
            raise DerivedGraphError("focus graph view is invalid")
        if self.schema_version != FOCUS_GRAPH_VIEW_VERSION or isinstance(self.schema_version, bool):
            raise DerivedGraphError("focus graph view version is unsupported")
        _require_prefixed_uuid(self.view_id, "focus-graph-view", "focus graph view")
        _require_prefixed_uuid(self.request_id, "focus-graph-request", "focus graph request")
        _require_prefixed_uuid(self.source_snapshot_id, "graph-snapshot", "focus graph source snapshot")
        _require_hash(self.source_snapshot_digest, "focus graph source digest")
        _require_project_id(self.project_id, "focus graph project")
        if type(self.max_hops) is not int or self.max_hops not in {1, 2}:
            raise DerivedGraphError("focus graph hop budget is invalid")
        if type(self.max_nodes) is not int or not 1 <= self.max_nodes <= MAX_FOCUS_NODES:
            raise DerivedGraphError("focus graph node budget is invalid")
        if type(self.max_edges) is not int or not 0 <= self.max_edges <= MAX_FOCUS_EDGES:
            raise DerivedGraphError("focus graph edge budget is invalid")
        if not isinstance(self.nodes, tuple) or not 1 <= len(self.nodes) <= MAX_FOCUS_NODES:
            raise DerivedGraphError("focus graph nodes are invalid")
        if len(self.nodes) > self.max_nodes:
            raise DerivedGraphError("focus graph exceeds its node budget")
        if any(type(node) is not FocusGraphNode for node in self.nodes):
            raise DerivedGraphError("focus graph nodes are invalid")
        if len({node.node_id for node in self.nodes}) != len(self.nodes):
            raise DerivedGraphError("focus graph repeats a node")
        node_by_id = {node.node_id: node for node in self.nodes}
        focus = node_by_id.get(self.focus_node_id)
        if focus is None:
            raise DerivedGraphError("focus graph view lacks its focus node")
        _validate_view_node_boundary(focus, self.project_id, require_project=True)
        for node in self.nodes:
            _validate_view_node_boundary(node, self.project_id, require_project=False)
        if not isinstance(self.edges, tuple) or len(self.edges) > MAX_FOCUS_EDGES:
            raise DerivedGraphError("focus graph edges are invalid")
        if len(self.edges) > self.max_edges:
            raise DerivedGraphError("focus graph exceeds its edge budget")
        if any(type(edge) is not FocusGraphEdge for edge in self.edges):
            raise DerivedGraphError("focus graph edges are invalid")
        if len({edge.edge_id for edge in self.edges}) != len(self.edges):
            raise DerivedGraphError("focus graph repeats an edge")
        for edge in self.edges:
            source = node_by_id.get(edge.source_id)
            target = node_by_id.get(edge.target_id)
            if source is None or target is None:
                raise DerivedGraphError("focus graph edge escapes displayed nodes")
            if edge.cross_store != (source.store_id != target.store_id):
                raise DerivedGraphError("focus graph edge boundary is inaccurate")
            if edge.source_revision != source.revision or edge.target_revision != target.revision:
                raise DerivedGraphError("focus graph edge revision is inaccurate")
            _validate_view_edge_boundary(edge, source, target, self.project_id)
        _validate_view_acyclic_relations(self.edges)
        if (
            self.derived_only is not True
            or self.fixture_only is not True
            or self.ui_mode != "generated_snapshot"
            or self.ui_server_started is not False
            or self.authority_write is not False
            or self.global_write is not False
        ):
            raise DerivedGraphError("focus graph view exceeds derived-only authority")
        _require_timestamp(self.created_at, "focus graph timestamp")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "view_digest")
        if self.view_digest and self.view_digest != expected:
            raise DerivedGraphError("focus graph view digest does not match")
        object.__setattr__(self, "view_digest", expected)

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "view_id": self.view_id,
            "request_id": self.request_id,
            "source_snapshot_id": self.source_snapshot_id,
            "source_snapshot_digest": self.source_snapshot_digest,
            "project_id": self.project_id,
            "focus_node_id": self.focus_node_id,
            "max_hops": self.max_hops,
            "max_nodes": self.max_nodes,
            "max_edges": self.max_edges,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "derived_only": self.derived_only,
            "fixture_only": self.fixture_only,
            "ui_mode": self.ui_mode,
            "ui_server_started": self.ui_server_started,
            "authority_write": self.authority_write,
            "global_write": self.global_write,
            "created_at": self.created_at,
        }
        if include_digest:
            value["view_digest"] = self.view_digest
        return value

    @classmethod
    def from_value(cls, value: object) -> "FocusGraphView":
        if isinstance(value, cls):
            return value
        expected = {
            "schema_version",
            "view_id",
            "request_id",
            "source_snapshot_id",
            "source_snapshot_digest",
            "project_id",
            "focus_node_id",
            "max_hops",
            "max_nodes",
            "max_edges",
            "nodes",
            "edges",
            "derived_only",
            "fixture_only",
            "ui_mode",
            "ui_server_started",
            "authority_write",
            "global_write",
            "created_at",
            "view_digest",
        }
        if type(value) is not dict or set(value) != expected or not isinstance(value["nodes"], list) or not isinstance(value["edges"], list):
            raise DerivedGraphError("focus graph view has unsupported fields")
        try:
            return cls(
                schema_version=value["schema_version"],
                view_id=value["view_id"],
                request_id=value["request_id"],
                source_snapshot_id=value["source_snapshot_id"],
                source_snapshot_digest=value["source_snapshot_digest"],
                project_id=value["project_id"],
                focus_node_id=value["focus_node_id"],
                max_hops=value["max_hops"],
                max_nodes=value["max_nodes"],
                max_edges=value["max_edges"],
                nodes=tuple(FocusGraphNode.from_value(item) for item in value["nodes"]),
                edges=tuple(FocusGraphEdge.from_value(item) for item in value["edges"]),
                derived_only=value["derived_only"],
                fixture_only=value["fixture_only"],
                ui_mode=value["ui_mode"],
                ui_server_started=value["ui_server_started"],
                authority_write=value["authority_write"],
                global_write=value["global_write"],
                created_at=value["created_at"],
                view_digest=value["view_digest"],
            )
        except (TypeError, DerivedGraphError) as error:
            raise DerivedGraphError("focus graph view is invalid") from error


def compile_focus_graph_view(
    runtime: DisposableRuntime,
    snapshot: Mapping[str, Any],
    request: FocusGraphRequest | Mapping[str, Any],
) -> FocusGraphView:
    """Compile one deterministic, bounded derived view from a public snapshot."""

    handle = load_disposable_runtime(runtime.root)
    handle.manifest.policy.require_resolved("A5")
    if not handle.manifest.policy.fixture_only or handle.manifest.policy.graph_ui != "GENERATED_SNAPSHOT":
        raise DerivedGraphError("A5 requires a fixture-only generated-snapshot policy")
    if type(snapshot) is not dict:
        raise DerivedGraphError("focus graph snapshot is invalid")
    validate_named_document("activation-v2-graph-snapshot-v1", snapshot)
    selected = FocusGraphRequest.from_value(request)
    if selected.project_id != snapshot["project_id"]:
        raise DerivedGraphError("focus graph request crosses its project boundary")
    nodes = {node["id"]: node for node in snapshot["nodes"]}
    focus = nodes.get(selected.focus_node_id)
    if focus is None or focus["store_id"] != f"project:{selected.project_id}":
        raise DerivedGraphError("focus graph must begin with an exact project node")
    started = time.monotonic()
    selected_ids = _select_node_ids(
        nodes,
        snapshot["edges"],
        selected.focus_node_id,
        selected.max_hops,
        selected.max_nodes,
    )
    selected_edges = tuple(
        FocusGraphEdge(
            edge_id=edge["edge_id"],
            relation=edge["relation"],
            source_id=edge["source_id"],
            target_id=edge["target_id"],
            source_revision=edge["source_revision"],
            target_revision=edge["target_revision"],
            cross_store=edge["cross_store"],
            provenance_ids=tuple(sorted(edge["provenance_ids"])),
            cross_store_provenance=(
                FocusGraphCrossStoreProvenance.from_value(edge["cross_store_provenance"])
                if edge["cross_store_provenance"] is not None
                else None
            ),
        )
        for edge in sorted(snapshot["edges"], key=lambda item: item["edge_id"])
        if (
            edge["source_id"] in selected_ids
            and edge["target_id"] in selected_ids
            and edge["relation"] != "related_to"
        )
    )[: selected.max_edges]
    selected_nodes = tuple(
        FocusGraphNode(
            node_id=node_id,
            revision=nodes[node_id]["revision"],
            store_id=nodes[node_id]["store_id"],
            kind=nodes[node_id]["kind"],
            lifecycle=nodes[node_id]["lifecycle"],
            freshness=nodes[node_id]["freshness"],
            label=nodes[node_id]["label"],
        )
        for node_id in selected_ids
    )
    elapsed_ms = math.ceil((time.monotonic() - started) * 1_000)
    if elapsed_ms < 0 or elapsed_ms > MAX_VIEW_LATENCY_MS:
        raise DerivedGraphError("focus graph rendering exceeds its local latency budget")
    result = FocusGraphView(
        schema_version=FOCUS_GRAPH_VIEW_VERSION,
        view_id=_derived_view_id(snapshot, selected),
        request_id=selected.request_id,
        source_snapshot_id=snapshot["snapshot_id"],
        source_snapshot_digest=snapshot["snapshot_digest"],
        project_id=selected.project_id,
        focus_node_id=selected.focus_node_id,
        max_hops=selected.max_hops,
        max_nodes=selected.max_nodes,
        max_edges=selected.max_edges,
        nodes=selected_nodes,
        edges=selected_edges,
        derived_only=True,
        fixture_only=True,
        ui_mode="generated_snapshot",
        ui_server_started=False,
        authority_write=False,
        global_write=False,
        created_at=snapshot["generated_at"],
    )
    validate_named_document("activation-v2-focus-graph-view-v1", result.to_dict())
    relative = _view_path(result.project_id, result.view_id)
    if disposable_json_exists(handle, relative):
        try:
            stored = FocusGraphView.from_value(read_disposable_json(handle, relative))
        except (IntegrityError, DerivedGraphError) as error:
            raise IntegrityError("focus graph view is unavailable or invalid") from error
        if stored.to_dict() != result.to_dict():
            raise IntegrityError("focus graph view identity is bound to another input")
        return stored
    write_disposable_json(handle, relative, result.to_dict())
    return result


def load_focus_graph_view(runtime: DisposableRuntime, project_id: str, view_id: str) -> FocusGraphView:
    """Load a derived graph artifact while retaining all boundary validation."""

    handle = load_disposable_runtime(runtime.root)
    if not isinstance(project_id, str) or _PROJECT_ID.fullmatch(project_id) is None:
        raise DerivedGraphError("focus graph project is invalid")
    _require_prefixed_uuid(view_id, "focus-graph-view", "focus graph view")
    try:
        document = read_disposable_json(handle, _view_path(project_id, view_id))
        validate_named_document("activation-v2-focus-graph-view-v1", document)
        view = FocusGraphView.from_value(document)
    except (IntegrityError, DerivedGraphError) as error:
        raise IntegrityError("focus graph view is unavailable or invalid") from error
    if view.project_id != project_id:
        raise IntegrityError("focus graph view crosses a project boundary")
    return view


def _select_node_ids(
    nodes: Mapping[str, Mapping[str, Any]],
    edges: list[Mapping[str, Any]],
    focus_node_id: str,
    max_hops: int,
    maximum: int,
) -> tuple[str, ...]:
    adjacency: dict[str, list[tuple[str, str]]] = {node_id: [] for node_id in nodes}
    for edge in edges:
        adjacency[edge["source_id"]].append((edge["edge_id"], edge["target_id"]))
        adjacency[edge["target_id"]].append((edge["edge_id"], edge["source_id"]))
    selected = {focus_node_id}
    ordered = [focus_node_id]
    pending = [(focus_node_id, 0)]
    while pending and len(ordered) < maximum:
        current, depth = pending.pop(0)
        if depth == max_hops:
            continue
        for _, neighbor in sorted(adjacency[current]):
            if neighbor in selected:
                continue
            selected.add(neighbor)
            ordered.append(neighbor)
            pending.append((neighbor, depth + 1))
            if len(ordered) == maximum:
                break
    return tuple(ordered)


def _validate_view_node_boundary(node: FocusGraphNode, project_id: str, *, require_project: bool) -> None:
    project_match = _PROJECT_OBJECT.fullmatch(node.node_id)
    global_match = _GLOBAL_OBJECT.fullmatch(node.node_id)
    if project_match:
        node_project, node_kind = project_match.groups()
        if node_project != project_id or node.store_id != f"project:{project_id}" or node.kind != node_kind:
            raise DerivedGraphError("focus graph project node crosses its boundary")
    elif global_match:
        if require_project or node.store_id != "knowledge:global" or node.kind != global_match.group(1):
            raise DerivedGraphError("focus graph global node is invalid")
    else:
        raise DerivedGraphError("focus graph node identity is invalid")


def _validate_view_edge_boundary(
    edge: FocusGraphEdge,
    source: FocusGraphNode,
    target: FocusGraphNode,
    project_id: str,
) -> None:
    if not edge.cross_store:
        return
    if edge.relation not in _CROSS_STORE_RELATIONS:
        raise DerivedGraphError("focus graph cross-store relation is invalid")
    if source.store_id != f"project:{project_id}" or target.store_id != "knowledge:global":
        raise DerivedGraphError("focus graph cross-store edge is invalid")
    provenance = edge.cross_store_provenance
    if (
        provenance is None
        or provenance.project_object_id != source.node_id
        or provenance.project_object_revision != source.revision
    ):
        raise DerivedGraphError("focus graph cross-store provenance does not bind the source")


def _validate_view_acyclic_relations(edges: tuple[FocusGraphEdge, ...]) -> None:
    for relation in _ACYCLIC_RELATIONS:
        adjacency: dict[str, set[str]] = {}
        for edge in edges:
            if edge.relation == relation:
                adjacency.setdefault(edge.source_id, set()).add(edge.target_id)
                adjacency.setdefault(edge.target_id, set())
        if _has_directed_cycle(adjacency):
            raise DerivedGraphError(f"focus graph {relation} relation contains a cycle")


def _has_directed_cycle(adjacency: Mapping[str, set[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        try:
            return any(visit(target) for target in adjacency.get(node, set()))
        finally:
            visiting.remove(node)
            visited.add(node)

    return any(visit(node) for node in adjacency)


def _derived_view_id(snapshot: Mapping[str, Any], request: FocusGraphRequest) -> str:
    digest = sha256_hex(
        {
            "source_snapshot_id": snapshot["snapshot_id"],
            "source_snapshot_digest": snapshot["snapshot_digest"],
            "request": request.to_dict(),
        }
    )
    return f"focus-graph-view:{uuid.uuid5(_VIEW_NAMESPACE, digest)}"


def _require_prefixed_uuid(value: object, prefix: str, label: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix + ":"):
        raise DerivedGraphError(f"{label} is invalid")
    if _UUID.fullmatch(value.removeprefix(prefix + ":")) is None:
        raise DerivedGraphError(f"{label} is invalid")
    return value


def _require_project_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _PROJECT_ID.fullmatch(value) is None:
        raise DerivedGraphError(f"{label} is invalid")
    return value


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise DerivedGraphError(f"{label} is invalid")
    return value


def _require_revision(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise DerivedGraphError(f"{label} is invalid")
    return value


def _require_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise DerivedGraphError(f"{label} is invalid")
    try:
        parse_rfc3339_utc(value)
    except Exception as error:
        raise DerivedGraphError(f"{label} is invalid") from error
    return value


def _view_path(project_id: str, view_id: str) -> str:
    return f"projects/{project_id}/derived/a5-focus/{view_id.removeprefix('focus-graph-view:')}.json"
