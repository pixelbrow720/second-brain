"""Read-only, fail-closed preflight for one future M9 route-evidence batch."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .errors import AuthorityDeniedError, IntegrityError, SemanticValidationError
from .global_rollout import verify_m9_packet
from .live_shadow import validate_m9_applied_state, verify_initial_shadow_receipt
from .m9_external_evidence import (
    ExternalTrustPolicy,
    RouteAttestationApprovalIntent,
    RouteAttestationApprovalPacket,
    RouteAttestationPlan,
    validate_openssh_trust_anchor,
    validate_route_attestation_approval_packet,
)
from .profiles import load_profile_registry, registry_digest


def _require_bound_authorization(reference: object, packet_digest: str) -> None:
    if not isinstance(reference, str) or packet_digest not in reference:
        raise AuthorityDeniedError("route batch preflight requires an explicit bound approval reference")


def _validate_initial_boundary(
    *,
    initial_packet: Mapping[str, Any],
    initial_shadow: Mapping[str, Any],
    intent: RouteAttestationApprovalIntent,
) -> None:
    verify_m9_packet(initial_packet)
    if type(initial_shadow) is not dict:
        raise SemanticValidationError("initial M9 shadow receipt is invalid")
    verify_initial_shadow_receipt(initial_shadow)
    if (
        intent.initial_m9_packet_digest != initial_packet["packet_digest"]
        or intent.initial_shadow_receipt_digest != initial_shadow["receipt_digest"]
        or initial_shadow["packet_digest"] != initial_packet["packet_digest"]
        or initial_shadow["shadow_execution_status"] != "PASS"
        or initial_shadow["promotion_status"] != "BLOCKED_MISSING_AUTHENTICATED_ROUTE_TELEMETRY"
        or initial_shadow["configured_provider"] != intent.provider_id
    ):
        raise IntegrityError("route approval intent is stale against the current initial M9 boundary")


def preflight_route_attestation_batch(
    *,
    initial_packet: Mapping[str, Any],
    initial_shadow: Mapping[str, Any],
    intent: RouteAttestationApprovalIntent,
    plan: RouteAttestationPlan,
    policy: ExternalTrustPolicy,
    approval_packet: RouteAttestationApprovalPacket,
    codex_home: Path,
    allowed_signers_path: Path,
    approval_reference: str,
) -> dict[str, object]:
    """Verify all local prerequisites without starting a provider request."""

    if (
        type(intent) is not RouteAttestationApprovalIntent
        or type(plan) is not RouteAttestationPlan
        or type(policy) is not ExternalTrustPolicy
        or type(approval_packet) is not RouteAttestationApprovalPacket
    ):
        raise SemanticValidationError("route batch preflight inputs are invalid")
    _validate_initial_boundary(initial_packet=initial_packet, initial_shadow=initial_shadow, intent=intent)
    validate_route_attestation_approval_packet(
        packet=approval_packet,
        intent=intent,
        plan=plan,
        policy=policy,
    )
    if plan.profile_registry_digest != registry_digest(load_profile_registry()):
        raise IntegrityError("route attestation plan is stale against the active profile registry")
    _require_bound_authorization(approval_reference, approval_packet.packet_digest)
    validate_openssh_trust_anchor(policy, allowed_signers_path)
    readback = validate_m9_applied_state(initial_packet, codex_home)
    return {
        "approval_packet_digest": approval_packet.packet_digest,
        "expected_request_count": approval_packet.expected_request_count,
        "global_readback": readback,
        "plan_digest": plan.plan_digest,
        "promotion_authorized": False,
        "status": "M9_ROUTE_BATCH_PREFLIGHT_PASS",
    }
