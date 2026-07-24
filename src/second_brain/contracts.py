"""M0 schema registry loading and cross-field contract checks."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
from pathlib import Path
import re
from typing import Any

from .activation_v2 import A0_SCHEMA_NAMES, validate_activation_v2_document
from .errors import SemanticValidationError, WorkspacePathError
from .jsonio import canonical_json_bytes, load_strict_json
from .schema_validation import parse_rfc3339_utc, validate_json_schema
from .workspace import repository_root, resolve_workspace_path


_GLOBAL_OBJECT_ID = re.compile(
    r"^kb:global:(source|entity|concept|claim|synthesis):[0-9a-f]{8}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_PROJECT_OBJECT_ID = re.compile(
    r"^mem:([a-z0-9][a-z0-9._-]{0,63}):(project|decision|component|task|bug|experiment|"
    r"evidence|question):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def schema_registry_path() -> Path:
    return resolve_workspace_path("schemas/schema-registry.json")


def load_schema_registry() -> dict[str, Any]:
    registry = load_strict_json(schema_registry_path())
    if not isinstance(registry, dict) or not isinstance(registry.get("schemas"), list):
        raise SemanticValidationError("schema registry must contain a schemas list")
    return registry


def schema_entry(name: str) -> dict[str, Any]:
    for entry in load_schema_registry()["schemas"]:
        if entry.get("name") == name:
            return entry
    raise SemanticValidationError(f"unknown schema contract: {name}")


def load_schema(name: str) -> dict[str, Any]:
    entry = schema_entry(name)
    schema = load_strict_json(schema_asset_path(entry["file"]))
    if not isinstance(schema, dict):
        raise SemanticValidationError(f"schema {name} is not a JSON object")
    return schema


def schema_asset_path(relative_path: str) -> Path:
    """Resolve a schema registry entry only within the checked-in schema directory."""

    return _contained_asset_path("schemas", Path("schemas") / relative_path)


def canonical_fixture_path(relative_path: str) -> Path:
    """Resolve a registry fixture only within synthetic canonical fixtures."""

    return _contained_asset_path("fixtures/canonical", relative_path)


def _contained_asset_path(directory: str, relative_path: str) -> Path:
    if not isinstance(relative_path, (str, Path)):
        raise SemanticValidationError("registry asset path must be a relative path")
    try:
        base = resolve_workspace_path(directory)
        candidate = resolve_workspace_path(relative_path)
    except WorkspacePathError as error:
        raise SemanticValidationError(f"invalid registry asset path: {relative_path}") from error
    try:
        candidate.relative_to(base)
    except ValueError as error:
        raise SemanticValidationError(
            f"registry asset path escapes {directory}: {relative_path}"
        ) from error
    return candidate


def validate_named_document(name: str, document: Any) -> None:
    """Validate one frozen M0 document and its currently-known semantics."""

    validate_json_schema(document, load_schema(name))
    if name == "memory-object-v2":
        validate_memory_object_semantics(document)
    elif name in A0_SCHEMA_NAMES:
        validate_activation_v2_document(name, document)


def validate_memory_object_semantics(document: dict[str, Any]) -> None:
    """Enforce ID/store/kind, temporal, and content-hash invariants from section 3."""

    object_id = document["id"]
    kind = document["kind"]
    store_id = document["store_id"]
    global_match = _GLOBAL_OBJECT_ID.fullmatch(object_id)
    project_match = _PROJECT_OBJECT_ID.fullmatch(object_id)

    if global_match:
        if store_id != "knowledge:global":
            raise SemanticValidationError("global object ID requires store_id knowledge:global")
        if kind != global_match.group(1):
            raise SemanticValidationError("global object ID kind does not match kind field")
    elif project_match:
        project_id, id_kind = project_match.groups()
        if store_id != f"project:{project_id}":
            raise SemanticValidationError("project object ID project does not match store_id")
        if kind != id_kind:
            raise SemanticValidationError("project object ID kind does not match kind field")
    else:
        raise SemanticValidationError("object ID is not a supported global or project namespace")

    created_at = _parse_timestamp(document["created_at"], "created_at")
    updated_at = _parse_timestamp(document["updated_at"], "updated_at")
    if updated_at < created_at:
        raise SemanticValidationError("updated_at must not precede created_at")

    expected_hash = logical_content_hash(document)
    if document["content_hash"] != expected_hash:
        raise SemanticValidationError("content_hash does not match the logical record")


def logical_content_hash(document: dict[str, Any]) -> str:
    """Hash a logical record with its self-referential hash field removed."""

    logical_record = deepcopy(document)
    logical_record.pop("content_hash", None)
    return hashlib.sha256(canonical_json_bytes(logical_record)).hexdigest()


def _parse_timestamp(value: str, field_name: str) -> datetime:
    try:
        return parse_rfc3339_utc(value)
    except Exception as error:
        raise SemanticValidationError(f"{field_name} is invalid") from error
