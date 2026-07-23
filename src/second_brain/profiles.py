"""The project-local, versioned source of truth for model profile aliases.

M0's V1 registry remains a historical fixture only.  M6 routes only through the
active V2 registry in ``config/model-profiles.json`` and never consults a second
alias-to-model table.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .canonical import sha256_hex
from .errors import SemanticValidationError
from .jsonio import load_strict_json
from .schema_validation import validate_json_schema
from .workspace import resolve_workspace_path


PROFILE_REGISTRY_VERSION = 2
NINE_ROUTER_ADAPTER = "nine_router"
SYNTHETIC_MAPPING_EVIDENCE = "synthetic-local"
SUPPORTED_EFFORTS = frozenset(("high", "max", "xhigh"))
APPROVED_PROFILE_ALIASES = (
    "tera-max",
    "tera-xhigh",
    "tera-high",
    "sol-max",
    "sol-xhigh",
    "luna-xhigh",
    "gpt55-xhigh",
)

_EXPECTED_PROFILE_INTENT = {
    "tera-max": ("Tera Max", "tera", "max", "orchestrator"),
    "tera-xhigh": ("Tera xhigh", "tera", "xhigh", "analyst"),
    "tera-high": ("Tera high", "tera", "high", "generalist"),
    "sol-max": ("Sol Max", "sol", "max", "builder"),
    "sol-xhigh": ("Sol xhigh", "sol", "xhigh", "builder"),
    "luna-xhigh": ("Luna xhigh", "luna", "xhigh", "utility"),
    "gpt55-xhigh": ("GPT-5.5 xhigh", "gpt55", "xhigh", "reviewer"),
}
_OBSERVED_MODEL_SLUGS = frozenset(
    (
        "cx/gpt-5.6-terra",
        "cx/gpt-5.6-sol",
        "cx/gpt-5.6-luna",
        "cx/gpt-5.5",
    )
)
_ROUTER_VERSION = "9router/0.5.40"
_CLIENT_VERSION = "codex-cli/0.144.6"
_NORMALIZATION_RULES_VERSION = "m6-synthetic-normalization/1"


@dataclass(frozen=True)
class ResolvedModelProfile:
    """An immutable profile mapping resolved from one validated V2 snapshot."""

    alias: str
    display_name: str
    family: str
    role: str
    effort_intent: str
    raw_model_slug: str
    serialized_effort_field: str
    serialized_effort_value: str
    adapter_name: str
    adapter_version: str
    client_version: str
    router_version: str
    normalization_rules_version: str
    mapping_evidence: str
    registry_version: int
    registry_digest: str
    mapping_digest: str


def profile_registry_path() -> Path:
    """Return the sole active, repository-contained registry path."""

    return resolve_workspace_path("config/model-profiles.json")


def profile_registry_schema_path() -> Path:
    """Return the V2 schema path without treating the historical V1 as active."""

    return resolve_workspace_path("schemas/model-profile-registry-v2.json")


def load_profile_registry() -> dict[str, Any]:
    """Load a fresh, validated copy of the active V2 profile registry."""

    registry = load_strict_json(profile_registry_path())
    validate_profile_registry(registry)
    return deepcopy(registry)


def validate_profile_registry(registry: Mapping[str, Any]) -> None:
    """Validate V2 shape and its fixed logical-profile and adapter invariants."""

    if not isinstance(registry, Mapping):
        raise SemanticValidationError("profile registry must be an object")
    # The schema validator expects ordinary JSON dictionaries, not arbitrary mappings.
    candidate = deepcopy(dict(registry))
    schema = load_strict_json(profile_registry_schema_path())
    validate_json_schema(candidate, schema)
    validate_profile_registry_semantics(candidate)


def validate_profile_registry_semantics(registry: Mapping[str, Any]) -> None:
    """Reject V1 fallback, profile drift, placeholders, and unpinned mappings."""

    if registry.get("registry_version") != PROFILE_REGISTRY_VERSION:
        raise SemanticValidationError("M6 routes only through model profile registry V2")
    profiles = registry.get("profiles")
    if not isinstance(profiles, Mapping) or set(profiles) != set(APPROVED_PROFILE_ALIASES):
        raise SemanticValidationError("profile registry must contain exactly the seven approved aliases")
    if registry.get("default_root_profile") != "tera-max":
        raise SemanticValidationError("Tera Max must remain the default root profile")

    for alias in APPROVED_PROFILE_ALIASES:
        profile = profiles[alias]
        if not isinstance(profile, Mapping):
            raise SemanticValidationError(f"{alias} must be an object")
        expected = _EXPECTED_PROFILE_INTENT[alias]
        actual = (
            profile.get("display_name"),
            profile.get("family"),
            profile.get("effort_intent"),
            profile.get("role"),
        )
        if actual != expected:
            raise SemanticValidationError(f"{alias} has an unexpected logical profile intent")

        adapters = profile.get("adapters")
        if not isinstance(adapters, Mapping):
            raise SemanticValidationError(f"{alias} adapter mapping is missing")
        adapter = adapters.get(NINE_ROUTER_ADAPTER)
        if not isinstance(adapter, Mapping):
            raise SemanticValidationError(f"{alias} nine_router adapter is missing")
        _validate_adapter(alias, profile, adapter)


def _validate_adapter(alias: str, profile: Mapping[str, Any], adapter: Mapping[str, Any]) -> None:
    raw_model_slug = adapter.get("raw_model_slug")
    effort_field = adapter.get("serialized_effort_field")
    effort_value = adapter.get("serialized_effort_value")
    if raw_model_slug not in _OBSERVED_MODEL_SLUGS:
        raise SemanticValidationError(f"{alias} has an unsupported local model identity")
    if not isinstance(raw_model_slug, str) or "REPLACE_WITH" in raw_model_slug:
        raise SemanticValidationError(f"{alias} has an unresolved raw model slug")
    if effort_field != "model_reasoning_effort":
        raise SemanticValidationError(f"{alias} must use the explicit reasoning effort field")
    if effort_value not in SUPPORTED_EFFORTS or effort_value != profile.get("effort_intent"):
        raise SemanticValidationError(f"{alias} has an unsupported or drifted reasoning effort")
    if adapter.get("adapter_version") != _ROUTER_VERSION:
        raise SemanticValidationError(f"{alias} does not bind the observed 9router version")
    if adapter.get("router_version") != _ROUTER_VERSION:
        raise SemanticValidationError(f"{alias} does not bind the observed router evidence version")
    if adapter.get("client_version") != _CLIENT_VERSION:
        raise SemanticValidationError(f"{alias} does not bind the observed client evidence version")
    if adapter.get("normalization_rules_version") != _NORMALIZATION_RULES_VERSION:
        raise SemanticValidationError(f"{alias} has an unsupported normalization rules version")
    if adapter.get("mapping_evidence") != SYNTHETIC_MAPPING_EVIDENCE:
        raise SemanticValidationError(f"{alias} mapping evidence is unavailable")

    # Model identity remains registry data.  This only checks family coherence;
    # route resolution below reads the raw slug exclusively from the registry.
    family = profile.get("family")
    if family == "gpt55":
        family_matches = raw_model_slug == "cx/gpt-5.5"
    else:
        # The observed 9router slug uses ``terra`` while the stable logical
        # family is intentionally spelled ``tera`` in the approved aliases.
        slug_family = "terra" if family == "tera" else family
        family_matches = raw_model_slug.endswith(f"-{slug_family}")
    if not family_matches:
        raise SemanticValidationError(f"{alias} raw model identity does not match its family")


def registry_digest(registry: Mapping[str, Any]) -> str:
    """Hash a fully validated snapshot before it can become a routing authority."""

    validate_profile_registry(registry)
    return sha256_hex(deepcopy(dict(registry)))


def approved_effort_intent(alias: str) -> str:
    """Return the immutable logical effort contract for one approved alias."""

    try:
        return _EXPECTED_PROFILE_INTENT[alias][2]
    except KeyError as error:
        raise SemanticValidationError("unknown approved model profile alias") from error


def resolve_profile(
    alias: str,
    registry: Mapping[str, Any] | None = None,
) -> ResolvedModelProfile:
    """Resolve one alias only from the supplied V2 snapshot or active registry."""

    if not isinstance(alias, str) or alias not in APPROVED_PROFILE_ALIASES:
        raise SemanticValidationError("unknown approved model profile alias")
    source = load_profile_registry() if registry is None else deepcopy(dict(registry))
    validate_profile_registry(source)
    profile = source["profiles"][alias]
    adapter = profile["adapters"][NINE_ROUTER_ADAPTER]
    snapshot_digest = sha256_hex(source)
    mapping = {
        "alias": alias,
        "effort_intent": profile["effort_intent"],
        "raw_model_slug": adapter["raw_model_slug"],
        "serialized_effort_field": adapter["serialized_effort_field"],
        "serialized_effort_value": adapter["serialized_effort_value"],
        "adapter_name": NINE_ROUTER_ADAPTER,
        "adapter_version": adapter["adapter_version"],
        "client_version": adapter["client_version"],
        "router_version": adapter["router_version"],
        "normalization_rules_version": adapter["normalization_rules_version"],
        "mapping_evidence": adapter["mapping_evidence"],
    }
    return ResolvedModelProfile(
        alias=alias,
        display_name=profile["display_name"],
        family=profile["family"],
        role=profile["role"],
        effort_intent=profile["effort_intent"],
        raw_model_slug=adapter["raw_model_slug"],
        serialized_effort_field=adapter["serialized_effort_field"],
        serialized_effort_value=adapter["serialized_effort_value"],
        adapter_name=NINE_ROUTER_ADAPTER,
        adapter_version=adapter["adapter_version"],
        client_version=adapter["client_version"],
        router_version=adapter["router_version"],
        normalization_rules_version=adapter["normalization_rules_version"],
        mapping_evidence=adapter["mapping_evidence"],
        registry_version=source["registry_version"],
        registry_digest=snapshot_digest,
        mapping_digest=sha256_hex(mapping),
    )
