#!/usr/bin/env python3
"""Generate the frozen historical M0 schemas and synthetic fixtures.

The three data schemas are extracted from their normative JSON fences in
docs/06-DATA-SCHEMAS.md.  M0 continues to own its V1 profile schema and fixture
as historical contract material.  The active V2 registry and schema registry
are owned by M6, so this generator deliberately never overwrites them.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_IDS = {
    "memory-object-v2": "https://pixel.local/schemas/memory-object-v2.json",
    "raw-capture-manifest-v1": "https://pixel.local/schemas/raw-capture-manifest-v1.json",
    "event-v2": "https://pixel.local/schemas/event-v2.json",
}
UUIDS = {
    "object": "5f0a25ca-0e18-4b10-a245-1aa222788470",
    "provenance": "aa56a9ef-0cca-49d9-8785-cb26ac4e4821",
    "capture": "8b05e0e8-aa54-4c99-a20a-e64ed15791b2",
    "event": "97b72ca7-23d5-4a07-ae7d-25f52f6d26ac",
    "transaction": "c6126727-2158-4388-990a-1f551357b2fd",
}
PROFILES = {
    "tera-max": ("Tera Max", "tera", "max", "orchestrator"),
    "tera-xhigh": ("Tera xhigh", "tera", "xhigh", "analyst"),
    "tera-high": ("Tera high", "tera", "high", "generalist"),
    "sol-max": ("Sol Max", "sol", "max", "builder"),
    "sol-xhigh": ("Sol xhigh", "sol", "xhigh", "builder"),
    "luna-xhigh": ("Luna xhigh", "luna", "xhigh", "utility"),
    "gpt55-xhigh": ("GPT-5.5 xhigh", "gpt55", "xhigh", "reviewer"),
}


def strict_json(text: str) -> Any:
    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key in blueprint schema: {key}")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=reject_duplicate)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def content_hash(document: dict[str, Any]) -> str:
    logical = deepcopy(document)
    logical.pop("content_hash", None)
    return hashlib.sha256(canonical_json(logical)).hexdigest()


def extract_schemas() -> dict[str, dict[str, Any]]:
    document = (ROOT / "docs" / "06-DATA-SCHEMAS.md").read_text(encoding="utf-8")
    fences = re.findall(r"```json\n(.*?)\n```", document, flags=re.DOTALL)
    by_id: dict[str, dict[str, Any]] = {}
    for fence in fences:
        candidate = strict_json(fence)
        if isinstance(candidate, dict) and candidate.get("$id") in SCHEMA_IDS.values():
            by_id[candidate["$id"]] = candidate

    missing = set(SCHEMA_IDS.values()).difference(by_id)
    if missing:
        raise RuntimeError(f"unable to extract normative schemas: {sorted(missing)}")
    return {name: by_id[schema_id] for name, schema_id in SCHEMA_IDS.items()}


def profile_schema() -> dict[str, Any]:
    profile_properties = {alias: {"$ref": "#/$defs/profile"} for alias in PROFILES}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://pixel.local/schemas/model-profile-registry-v1.json",
        "title": "ModelProfileRegistryV1",
        "type": "object",
        "additionalProperties": False,
        "required": ["registry_version", "default_root_profile", "profiles"],
        "properties": {
            "registry_version": {"const": 1},
            "default_root_profile": {"const": "tera-max"},
            "profiles": {
                "type": "object",
                "additionalProperties": False,
                "required": list(PROFILES),
                "properties": profile_properties,
            },
        },
        "$defs": {
            "profile": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "display_name",
                    "family",
                    "effort_intent",
                    "role",
                    "adapters",
                ],
                "properties": {
                    "display_name": {"type": "string", "minLength": 1},
                    "family": {"enum": ["tera", "sol", "luna", "gpt55"]},
                    "effort_intent": {"enum": ["high", "max", "xhigh"]},
                    "role": {
                        "enum": ["orchestrator", "analyst", "generalist", "builder", "utility", "reviewer"]
                    },
                    "adapters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["nine_router"],
                        "properties": {
                            "nine_router": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "raw_model_slug",
                                    "serialized_effort",
                                    "adapter_version",
                                ],
                                "properties": {
                                    "raw_model_slug": {"type": "string", "minLength": 1},
                                    "serialized_effort": {"type": "string", "minLength": 1},
                                    "adapter_version": {"type": "string", "minLength": 1},
                                },
                            }
                        },
                    },
                },
            }
        },
    }


def profile_registry() -> dict[str, Any]:
    profiles: dict[str, Any] = {}
    for alias, (display_name, family, effort_intent, role) in PROFILES.items():
        profiles[alias] = {
            "display_name": display_name,
            "family": family,
            "effort_intent": effort_intent,
            "role": role,
            "adapters": {
                "nine_router": {
                    "raw_model_slug": "REPLACE_WITH_VALIDATED_9ROUTER_SLUG",
                    "serialized_effort": "REPLACE_WITH_VALIDATED_9ROUTER_VALUE",
                    "adapter_version": "REPLACE_WITH_VALIDATED_9ROUTER_ADAPTER_VERSION",
                }
            },
        }
    return {"registry_version": 1, "default_root_profile": "tera-max", "profiles": profiles}


def memory_object() -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": 2,
        "id": f"kb:global:concept:{UUIDS['object']}",
        "store_id": "knowledge:global",
        "kind": "concept",
        "revision": 1,
        "title": "Deterministic contract fixture",
        "aliases": [],
        "lifecycle": {"status": "active", "changed_at": None, "reason": None},
        "authority": "ai-synthesis",
        "trust": "agent_inference",
        "epistemic_status": "asserted",
        "confidence": 1,
        "actors": [{"actor_id": "agent:fixture", "actor_type": "agent", "role": "fixture"}],
        "provenance": [
            {
                "provenance_id": f"prov:{UUIDS['provenance']}",
                "kind": "agent_generation",
                "observed_at": "2026-07-22T00:00:00Z",
                "actor_id": "agent:fixture",
                "ref": None,
                "content_hash": None,
                "note": "Synthetic M0 fixture",
            }
        ],
        "created_at": "2026-07-22T00:00:00Z",
        "updated_at": "2026-07-22T00:00:00Z",
        "relations": [],
        "references": [],
        "verification": {
            "state": "unverified",
            "method": None,
            "checked_at": None,
            "verifier": None,
            "evidence_ids": [],
        },
        "tags": ["contract/schema"],
        "payload": {
            "domain": ["second-brain"],
            "definition": "A synthetic fixture for the M0 schema contract.",
            "boundaries": [],
            "non_examples": [],
        },
        "body": "This fixture is synthetic and contains no user data.",
    }
    document["content_hash"] = content_hash(document)
    return document


def raw_capture_manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "capture_id": f"cap:{UUIDS['capture']}",
        "revision": 1,
        "status": "accepted",
        "origin": {"kind": "manual", "locator": "fixture:synthetic", "final_locator": None},
        "blob": {
            "sha256": "b" * 64,
            "bytes": 42,
            "media_type": "text/plain",
            "relative_path": "raw/blobs/sha256/bb/" + "b" * 64,
        },
        "captured_at": "2026-07-22T00:00:00Z",
        "captured_by": "agent:fixture",
        "scan": {
            "scanner_version": "fixture/1",
            "secret": "clear",
            "injection": "clear",
            "format": "supported",
            "decision": "accept",
            "reason_codes": [],
        },
        "retention": {"classification": "public", "expires_at": None},
        "source_object_id": None,
        "content_hash": "c" * 64,
    }


def event() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "event_id": f"evt:{UUIDS['event']}",
        "sequence": 1,
        "store_id": "knowledge:global",
        "transaction_id": f"txn:{UUIDS['transaction']}",
        "event_type": "object_created",
        "occurred_at": "2026-07-22T00:00:00Z",
        "actor": "agent:fixture",
        "object_id": f"kb:global:concept:{UUIDS['object']}",
        "from_revision": None,
        "to_revision": 1,
        "before_hash": None,
        "after_hash": "d" * 64,
        "reason": "Create synthetic M0 fixture",
        "prev_event_hash": None,
        "event_hash": "e" * 64,
    }


def invalid_fixtures(valid_memory: dict[str, Any]) -> dict[str, str]:
    duplicate = json.dumps(valid_memory, ensure_ascii=False, separators=(",", ":"))
    marker = '"title":"Deterministic contract fixture",'
    duplicate = duplicate.replace(marker, marker + '"title":"Duplicate title",', 1)

    unknown = deepcopy(valid_memory)
    unknown["unexpected_field"] = "must be rejected"

    wrong_store = deepcopy(valid_memory)
    wrong_store["store_id"] = "project:fixture"
    wrong_store["content_hash"] = content_hash(wrong_store)

    wrong_id = deepcopy(valid_memory)
    wrong_id["id"] = "mem:fixture:task:5f0a25ca-0e18-4b10-a245-1aa222788470"
    wrong_id["content_hash"] = content_hash(wrong_id)

    wrong_kind = deepcopy(valid_memory)
    wrong_kind["kind"] = "claim"
    wrong_kind["content_hash"] = content_hash(wrong_kind)

    malformed_timestamp = deepcopy(valid_memory)
    malformed_timestamp["created_at"] = "2026-07-22T00:00:00+00:00"
    malformed_timestamp["content_hash"] = content_hash(malformed_timestamp)

    timestamp_order = deepcopy(valid_memory)
    timestamp_order["updated_at"] = "2026-07-21T23:59:59Z"
    timestamp_order["content_hash"] = content_hash(timestamp_order)

    malformed_hash = deepcopy(valid_memory)
    malformed_hash["content_hash"] = "not-a-sha256"

    mismatched_hash = deepcopy(valid_memory)
    mismatched_hash["content_hash"] = "a" * 64

    return {
        "memory-object-duplicate-key.json": duplicate + "\n",
        "memory-object-unknown-field.json": serialize(unknown),
        "memory-object-store-mismatch.json": serialize(wrong_store),
        "memory-object-id-mismatch.json": serialize(wrong_id),
        "memory-object-kind-mismatch.json": serialize(wrong_kind),
        "memory-object-malformed-timestamp.json": serialize(malformed_timestamp),
        "memory-object-timestamp-order.json": serialize(timestamp_order),
        "memory-object-malformed-hash.json": serialize(malformed_hash),
        "memory-object-content-hash-mismatch.json": serialize(mismatched_hash),
    }


def serialize(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_file(path: Path, content: str, check: bool) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    if check:
        raise RuntimeError(f"generated asset is stale: {path.relative_to(ROOT)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return True


def build(check: bool) -> int:
    schemas = extract_schemas()
    schemas["model-profile-registry-v1"] = profile_schema()
    outputs: dict[Path, str] = {
        ROOT / "fixtures" / "canonical" / "memory-object-v2.json": serialize(memory_object()),
        ROOT / "fixtures" / "canonical" / "raw-capture-manifest-v1.json": serialize(raw_capture_manifest()),
        ROOT / "fixtures" / "canonical" / "event-v2.json": serialize(event()),
        ROOT / "fixtures" / "canonical" / "model-profile-registry-v1.json": serialize(profile_registry()),
    }
    for name, schema in schemas.items():
        outputs[ROOT / "schemas" / f"{name}.json"] = serialize(schema)
    outputs.update(
        {
            ROOT / "fixtures" / "invalid" / name: content
            for name, content in invalid_fixtures(memory_object()).items()
        }
    )

    changed = sum(write_file(path, content, check) for path, content in outputs.items())
    print(f"M0 contract assets {'verified' if check else 'generated'}: {len(outputs)} files, {changed} changed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate or verify M0 contract assets")
    parser.add_argument("--check", action="store_true", help="fail if generated files are stale")
    args = parser.parse_args()
    try:
        return build(args.check)
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
