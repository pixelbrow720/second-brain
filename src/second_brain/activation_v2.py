"""Fail-closed, project-local Activation V2 A0 contract validation.

This module validates checked-in synthetic fixtures only.  It does not create a
runtime, open an authority store, install a hook, route a session, or write a
receipt.  Later Activation V2 phases need separate implementation and approval.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import re
from typing import Any

from .canonical import sha256_hex
from .errors import ContentPolicyError, SemanticValidationError
from .profiles import APPROVED_PROFILE_ALIASES, approved_effort_intent
from .schema_validation import parse_rfc3339_utc


A0_SCHEMA_NAMES = frozenset(
    (
        "activation-v2-task-closure-v1",
        "activation-v2-capture-receipt-v1",
        "activation-v2-promotion-outbox-v1",
        "activation-v2-route-intent-v1",
        "activation-v2-graph-snapshot-v1",
    )
)

A0_CANONICAL_FIXTURES = {
    "activation-v2-task-closure-v1": "fixtures/canonical/activation-v2-task-closure-v1.json",
    "activation-v2-capture-receipt-v1": "fixtures/canonical/activation-v2-capture-receipt-v1.json",
    "activation-v2-promotion-outbox-v1": "fixtures/canonical/activation-v2-promotion-outbox-v1.json",
    "activation-v2-route-intent-v1": "fixtures/canonical/activation-v2-route-intent-v1.json",
    "activation-v2-graph-snapshot-v1": "fixtures/canonical/activation-v2-graph-snapshot-v1.json",
}

_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PROJECT_OBJECT = re.compile(
    r"^mem:([a-z0-9][a-z0-9._-]{0,63}):(project|decision|component|task|bug|"
    r"experiment|evidence|question):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
_GLOBAL_OBJECT = re.compile(
    r"^kb:global:(source|entity|concept|claim|synthesis):[0-9a-f]{8}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_HASH = re.compile(r"^[0-9a-f]{64}$")
_SECRET_PATTERNS = (
    re.compile(r"\bV2_SECRET_SENTINEL\b", re.IGNORECASE),
    re.compile(r"-----BEGIN(?: [A-Z0-9-]+)? PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(
        r"\b(?:api[_-]?key|secret|password|access[_-]?token|authorization)"
        r"\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{8,}",
        re.IGNORECASE,
    ),
)
_INJECTION_PATTERNS = (
    re.compile(r"\bV2_PROMPT_INJECTION_SENTINEL\b", re.IGNORECASE),
    re.compile(
        r"(?:^|\n)\s*(?:ignore|disregard|override)\s+(?:all\s+)?"
        r"(?:previous|prior|above)\s+(?:instructions|rules)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:^|\n)\s*(?:system|developer)\s+(?:prompt|message)\s*:", re.IGNORECASE),
)
_TRANSCRIPT_PATTERNS = (
    re.compile(r"\bV2_RAW_TRANSCRIPT_SENTINEL\b", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*(?:user|assistant|tool)\s*:", re.IGNORECASE),
)
_ABSOLUTE_PATH_PATTERN = re.compile(r"(?:^/|^[A-Za-z]:[\\/]|\bfile://)")
_ACYCLIC_RELATIONS = frozenset(("part_of", "supersedes"))
_CROSS_STORE_RELATIONS = frozenset(("about", "derived_from", "implements", "supports", "related_to"))


def activation_v2_logical_digest(document: Mapping[str, Any], digest_field: str) -> str:
    """Return the digest of a record after removing its self-referential field."""

    logical = deepcopy(dict(document))
    logical.pop(digest_field, None)
    return sha256_hex(logical)


def validate_activation_v2_document(name: str, document: dict[str, Any]) -> None:
    """Apply cross-field and privacy invariants after JSON Schema validation."""

    if name not in A0_SCHEMA_NAMES:
        raise SemanticValidationError("unknown Activation V2 A0 contract")
    if type(document) is not dict:
        raise SemanticValidationError("Activation V2 A0 record must be an object")
    _reject_unsafe_content(document)
    if name == "activation-v2-task-closure-v1":
        _validate_task_closure(document)
    elif name == "activation-v2-capture-receipt-v1":
        _validate_capture_receipt(document)
    elif name == "activation-v2-promotion-outbox-v1":
        _validate_promotion_outbox(document)
    elif name == "activation-v2-route-intent-v1":
        _validate_route_intent(document)
    else:
        _validate_graph_snapshot(document)


def validate_activation_v2_fixture_bundle(documents: Mapping[str, dict[str, Any]]) -> None:
    """Validate the five canonical A0 fixtures as one non-authorizing bundle."""

    if set(documents) != A0_SCHEMA_NAMES:
        raise SemanticValidationError("Activation V2 A0 fixture bundle is incomplete")
    for name in sorted(A0_SCHEMA_NAMES):
        validate_activation_v2_document(name, documents[name])

    closure = documents["activation-v2-task-closure-v1"]
    receipt = documents["activation-v2-capture-receipt-v1"]
    outbox = documents["activation-v2-promotion-outbox-v1"]
    _require_equal(receipt["closure_id"], closure["closure_id"], "capture receipt closure")
    _require_equal(receipt["project_id"], closure["project_id"], "capture receipt project")
    _require_equal(
        receipt["closure_digest"],
        sha256_hex(closure),
        "capture receipt closure digest",
    )
    expected_counts = {
        "decisions": len(closure["decisions"]),
        "evidence": len(closure["evidence"]),
        "open_tasks": len(closure["open_tasks"]),
        "questions": len(closure["questions"]),
        "global_candidates": len(closure["global_candidates"]),
    }
    _require_equal(receipt["candidate_counts"], expected_counts, "capture receipt candidate counts")
    _require_equal(outbox["origin_closure_id"], closure["closure_id"], "outbox closure")
    _require_equal(outbox["project_id"], closure["project_id"], "outbox project")
    _require_equal(
        outbox["origin_closure_digest"],
        sha256_hex(closure),
        "outbox closure digest",
    )
    closure_sources = {
        object_id
        for candidate in closure["global_candidates"]
        for object_id in candidate["source_object_ids"]
    }
    if not {item["id"] for item in outbox["source_objects"]}.issubset(closure_sources):
        raise SemanticValidationError("outbox sources are not declared by the closure")


def _validate_task_closure(document: Mapping[str, Any]) -> None:
    project_id = _require_project_id(document["project_id"], "task closure project")
    _require_timestamp(document["closed_at"], "task closure timestamp")
    for decision in document["decisions"]:
        _require_project_object_ids(decision["evidence_ids"], project_id, "decision evidence")
    for evidence in document["evidence"]:
        _require_project_object(evidence["evidence_id"], project_id, "closure evidence")
    for candidate in document["global_candidates"]:
        _require_project_object_ids(candidate["source_object_ids"], project_id, "global candidate source")
    if document["task_outcome"] == "no_durable_change" and any(
        document[key] for key in ("decisions", "evidence", "open_tasks", "questions", "global_candidates")
    ):
        raise SemanticValidationError("no-durable-change closure must not include durable candidates")


def _validate_capture_receipt(document: Mapping[str, Any]) -> None:
    _require_project_id(document["project_id"], "capture receipt project")
    _require_timestamp(document["generated_at"], "capture receipt timestamp")
    if document["execution_mode"] != "fixture_only" or document["write_authority"] != "none":
        raise SemanticValidationError("A0 capture receipt cannot authorize a write")
    if document["global_write_committed"] is not False:
        raise SemanticValidationError("A0 capture receipt cannot commit global authority")
    if document["status"] == "validated" and "NO_WRITE_AUTHORITY" not in document["reason_codes"]:
        raise SemanticValidationError("A0 capture receipt must state its no-write boundary")


def _validate_promotion_outbox(document: Mapping[str, Any]) -> None:
    project_id = _require_project_id(document["project_id"], "promotion outbox project")
    _require_timestamp(document["created_at"], "promotion outbox timestamp")
    _require_equal(document["source_store"], f"project:{project_id}", "promotion outbox source store")
    if not document["idempotency_key"].startswith(f"promotion:{project_id}:"):
        raise SemanticValidationError("promotion outbox idempotency key does not bind its project")
    for source in document["source_objects"]:
        _require_project_object(source["id"], project_id, "promotion outbox source")
        if type(source["revision"]) is not int or isinstance(source["revision"], bool):
            raise SemanticValidationError("promotion outbox source revision is invalid")
        _require_hash(source["content_hash"], "promotion outbox source hash")
    if document["review_state"] != "pending_review" or document["global_write_committed"] is not False:
        raise SemanticValidationError("A0 promotion outbox cannot commit global authority")
    expected = activation_v2_logical_digest(document, "proposal_digest")
    _require_equal(document["proposal_digest"], expected, "promotion outbox digest")


def _validate_route_intent(document: Mapping[str, Any]) -> None:
    _require_timestamp(document["created_at"], "route intent timestamp")
    if document["route_id"] == document["correlation_id"]:
        raise SemanticValidationError("route and correlation identities must differ")
    alias = document["profile_alias"]
    if alias not in APPROVED_PROFILE_ALIASES:
        raise SemanticValidationError("route profile is not approved")
    if document["effort_intent"] != approved_effort_intent(alias):
        raise SemanticValidationError("route effort does not match the approved profile")
    if document["selection_reason"] == "fallback_uncertainty" and alias != "tera-max":
        raise SemanticValidationError("uncertain routing must fall back to tera-max")
    if document["prompt_persistence"] != "forbidden" or document["session_action"] != "none":
        raise SemanticValidationError("A0 route intent must not persist a prompt or create a session")
    expected = activation_v2_logical_digest(document, "intent_digest")
    _require_equal(document["intent_digest"], expected, "route intent digest")


def _validate_graph_snapshot(document: Mapping[str, Any]) -> None:
    project_id = _require_project_id(document["project_id"], "graph snapshot project")
    _require_timestamp(document["generated_at"], "graph snapshot timestamp")
    if document["derived_only"] is not True:
        raise SemanticValidationError("A0 graph snapshot must remain derived only")

    node_by_id: dict[str, Mapping[str, Any]] = {}
    stores: set[str] = set()
    for node in document["nodes"]:
        node_id = node["id"]
        if node_id in node_by_id:
            raise SemanticValidationError("graph snapshot repeats a node")
        _validate_graph_node(node, project_id)
        node_by_id[node_id] = node
        stores.add(node["store_id"])

    snapshot_stores = {item["store_id"] for item in document["source_snapshots"]}
    if len(snapshot_stores) != len(document["source_snapshots"]):
        raise SemanticValidationError("graph snapshot repeats a source store")
    if snapshot_stores != stores:
        raise SemanticValidationError("graph snapshot stores do not match displayed nodes")
    if f"project:{project_id}" not in snapshot_stores:
        raise SemanticValidationError("graph snapshot lacks its exact project store")
    if any(store.startswith("project:") and store != f"project:{project_id}" for store in snapshot_stores):
        raise SemanticValidationError("graph snapshot includes another project store")

    edge_ids: set[str] = set()
    adjacency: dict[str, dict[str, set[str]]] = {
        relation: {node_id: set() for node_id in node_by_id} for relation in _ACYCLIC_RELATIONS
    }
    for edge in document["edges"]:
        edge_id = edge["edge_id"]
        if edge_id in edge_ids:
            raise SemanticValidationError("graph snapshot repeats an edge")
        edge_ids.add(edge_id)
        source = node_by_id.get(edge["source_id"])
        target = node_by_id.get(edge["target_id"])
        if source is None or target is None:
            raise SemanticValidationError("graph edge does not resolve to a node")
        if edge["source_revision"] != source["revision"] or edge["target_revision"] != target["revision"]:
            raise SemanticValidationError("graph edge revision does not match its node")
        if edge["source_id"] == edge["target_id"]:
            raise SemanticValidationError("graph snapshot self-edge is forbidden")
        cross_store = source["store_id"] != target["store_id"]
        if edge["cross_store"] is not cross_store:
            raise SemanticValidationError("graph edge cross-store flag is inaccurate")
        _validate_edge_boundary(edge, source, target)
        if edge["relation"] in _ACYCLIC_RELATIONS:
            adjacency[edge["relation"]][edge["source_id"]].add(edge["target_id"])

    for relation, relation_adjacency in adjacency.items():
        if _has_directed_cycle(relation_adjacency):
            raise SemanticValidationError(f"graph {relation} relation contains a cycle")
    expected = activation_v2_logical_digest(document, "snapshot_digest")
    _require_equal(document["snapshot_digest"], expected, "graph snapshot digest")


def _validate_graph_node(node: Mapping[str, Any], project_id: str) -> None:
    node_id = node["id"]
    store_id = node["store_id"]
    project_match = _PROJECT_OBJECT.fullmatch(node_id)
    global_match = _GLOBAL_OBJECT.fullmatch(node_id)
    if project_match:
        node_project, node_kind = project_match.groups()
        if node_project != project_id or store_id != f"project:{project_id}" or node["kind"] != node_kind:
            raise SemanticValidationError("project graph node crosses its project boundary")
    elif global_match:
        if store_id != "knowledge:global" or node["kind"] != global_match.group(1):
            raise SemanticValidationError("global graph node identity is invalid")
    else:
        raise SemanticValidationError("graph node ID is unsupported")


def _validate_edge_boundary(
    edge: Mapping[str, Any], source: Mapping[str, Any], target: Mapping[str, Any]
) -> None:
    provenance = edge["cross_store_provenance"]
    if not edge["cross_store"]:
        if provenance is not None:
            raise SemanticValidationError("same-store edge cannot carry cross-store provenance")
        return
    if edge["relation"] not in _CROSS_STORE_RELATIONS:
        raise SemanticValidationError("cross-store relation type is not approved for A0")
    if not source["store_id"].startswith("project:") or target["store_id"] != "knowledge:global":
        raise SemanticValidationError("A0 permits only project-to-global graph references")
    if not isinstance(provenance, Mapping):
        raise SemanticValidationError("cross-store edge lacks provenance")
    if provenance["project_object_id"] != source["id"]:
        raise SemanticValidationError("cross-store provenance does not bind the project source")
    if provenance["project_object_revision"] != source["revision"]:
        raise SemanticValidationError("cross-store provenance does not bind the project revision")
    _require_hash(provenance["repository_snapshot_digest"], "cross-store repository snapshot")


def _reject_unsafe_content(value: Any) -> None:
    reasons: set[str] = set()
    for text in _walk_strings(value):
        if "\x00" in text or "\r" in text or "\n" in text:
            reasons.add("CONTROL_OR_MULTILINE_TEXT")
        if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
            reasons.add("SECRET_DETECTED")
        if any(pattern.search(text) for pattern in _INJECTION_PATTERNS):
            reasons.add("PROMPT_INJECTION_DETECTED")
        if any(pattern.search(text) for pattern in _TRANSCRIPT_PATTERNS):
            reasons.add("RAW_TRANSCRIPT_DETECTED")
        if _ABSOLUTE_PATH_PATTERN.search(text):
            reasons.add("ABSOLUTE_PATH_DETECTED")
    if reasons:
        raise ContentPolicyError(tuple(sorted(reasons)))


def _walk_strings(value: Any) -> Sequence[str]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        return tuple(item for child in value.values() for item in _walk_strings(child))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(item for child in value for item in _walk_strings(child))
    return ()


def _require_project_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _PROJECT_ID.fullmatch(value) is None:
        raise SemanticValidationError(f"{label} is invalid")
    return value


def _require_project_object(value: object, project_id: str, label: str) -> str:
    if not isinstance(value, str):
        raise SemanticValidationError(f"{label} is invalid")
    match = _PROJECT_OBJECT.fullmatch(value)
    if match is None or match.group(1) != project_id:
        raise SemanticValidationError(f"{label} crosses the project boundary")
    return value


def _require_project_object_ids(values: object, project_id: str, label: str) -> None:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise SemanticValidationError(f"{label} is invalid")
    for value in values:
        _require_project_object(value, project_id, label)


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise SemanticValidationError(f"{label} is invalid")
    return value


def _require_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise SemanticValidationError(f"{label} is invalid")
    try:
        parse_rfc3339_utc(value)
    except Exception as error:
        raise SemanticValidationError(f"{label} is invalid") from error
    return value


def _require_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise SemanticValidationError(f"{label} is invalid")


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
            visiting.discard(node)
            visited.add(node)

    return any(visit(node) for node in adjacency)
