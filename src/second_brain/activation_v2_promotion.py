"""Fixture-only A8 reviewed-promotion and restore evaluator.

This module accepts only public synthetic, digest-only provenance supplied by
the caller.  It simulates a reviewed global promotion inside an ignored A1
disposable runtime; it cannot discover a real target, accept approval, make a
network call, or write a project/global authority store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Any, Mapping
import uuid

from .activation_v2 import activation_v2_logical_digest, validate_activation_v2_safe_content
from .activation_v2_memory import ClosureProposal, ProposalReviewReceipt
from .activation_v2_rollout import SyntheticA7ApprovalPacket, synthetic_a7_implementation_digest
from .activation_v2_runtime import (
    DisposableRuntime,
    PolicyInputs,
    disposable_json_exists,
    exclusive_disposable_runtime_lock,
    load_disposable_runtime,
    read_disposable_json,
    remove_disposable_json,
    write_disposable_json,
)
from .canonical import sha256_hex
from .errors import IntegrityError, SemanticValidationError
from .schema_validation import parse_rfc3339_utc
from .workspace import repository_root


A8_PROMOTION_CORPUS_VERSION = 1
A8_PACKET_VERSION = 1
A8_STATE_VERSION = 1
A8_RECEIPT_VERSION = 1
A8_PENDING_OPERATION_VERSION = 1
MAX_A8_CANDIDATES = 8
A8_SCHEMA_NAMES = frozenset(
    (
        "activation-v2-a8-promotion-corpus-v1",
        "activation-v2-a8-promotion-packet-v1",
        "activation-v2-a8-promotion-state-v1",
        "activation-v2-a8-promotion-receipt-v1",
        "activation-v2-a8-pending-operation-v1",
    )
)

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PROJECT_OBJECT = re.compile(
    r"^mem:([a-z0-9][a-z0-9._-]{0,63}):(project|decision|component|task|bug|"
    r"experiment|evidence|question):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
_GLOBAL_CLAIM = re.compile(
    r"^kb:global:claim:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{12}$"
)
_OPERATIONS = frozenset(("backup", "promotion_canary", "readback", "restore"))
_TRANSITION_OPERATIONS = frozenset(("promotion_canary", "restore"))
_STATUS_BY_OPERATION = {
    "backup": "BACKUP_CREATED_SYNTHETIC",
    "promotion_canary": "PROMOTION_CANARY_APPLIED_SYNTHETIC",
    "readback": "PROMOTION_READBACK_MATCH_SYNTHETIC",
    "restore": "PROMOTION_RESTORED_SYNTHETIC",
}
_SEQUENCE = ("backup", "promotion_canary", "readback", "restore")
_ROLLBACK_TRIGGER_CODES = (
    "BOUNDARY_VIOLATION",
    "CAS_CONFLICT",
    "CROSS_PROJECT_LEAKAGE",
    "READBACK_MISMATCH",
    "REVIEW_MISMATCH",
    "SOURCE_DRIFT",
)
_A8_IMPLEMENTATION_FILES = (
    "src/second_brain/activation_v2.py",
    "src/second_brain/activation_v2_memory.py",
    "src/second_brain/activation_v2_runtime.py",
    "src/second_brain/activation_v2_rollout.py",
    "src/second_brain/activation_v2_promotion.py",
    "schemas/activation-v2-a8-promotion-corpus-v1.json",
    "schemas/activation-v2-a8-promotion-packet-v1.json",
    "schemas/activation-v2-a8-promotion-state-v1.json",
    "schemas/activation-v2-a8-promotion-receipt-v1.json",
    "schemas/activation-v2-a8-pending-operation-v1.json",
)


class SyntheticPromotionError(SemanticValidationError):
    """An A8 reviewed-promotion input or synthetic transition is unsafe."""


def synthetic_a8_implementation_digest() -> str:
    """Hash the local evaluator and its direct contract dependencies.

    The digest is fixture-drift detection only.  It is not a signature, target
    attestation, or substitute for a fresh real approval packet.
    """

    root = repository_root()
    try:
        files = {
            relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
            for relative in _A8_IMPLEMENTATION_FILES
        }
    except OSError as error:
        raise SyntheticPromotionError("SOURCE_DRIFT") from error
    return sha256_hex({"implementation_id": "activation-v2-a8-synthetic-promotion-v1", "files": files})


@dataclass(frozen=True)
class SyntheticA8PromotionCandidate:
    """One review-approved, opaque global-claim candidate from one project."""

    candidate_id: str
    source_project_id: str
    closure_proposal: ClosureProposal
    review_receipt: ProposalReviewReceipt
    source_object_ids: tuple[str, ...]
    global_object_id: str
    global_object_revision: int
    before_digest: str
    candidate_after_digest: str
    candidate_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not SyntheticA8PromotionCandidate:
            raise SyntheticPromotionError("synthetic promotion candidate is invalid")
        _require_prefixed_uuid(self.candidate_id, "a8-candidate", "synthetic promotion candidate")
        _require_project_id(self.source_project_id, "synthetic promotion project")
        if type(self.closure_proposal) is not ClosureProposal or type(self.review_receipt) is not ProposalReviewReceipt:
            raise SyntheticPromotionError("synthetic promotion review provenance is invalid")
        if (
            self.closure_proposal.project_id != self.source_project_id
            or self.review_receipt.project_id != self.source_project_id
            or self.review_receipt.proposal_id != self.closure_proposal.proposal_id
            or self.review_receipt.decision != "approved_for_later_transaction"
            or self.review_receipt.reviewer_kind != "synthetic_fixture"
            or self.closure_proposal.candidate_counts["global_candidates"] < 1
        ):
            raise SyntheticPromotionError("synthetic promotion review is not eligible")
        object_ids = _project_object_ids(self.source_object_ids, self.source_project_id)
        if not isinstance(self.global_object_id, str) or _GLOBAL_CLAIM.fullmatch(self.global_object_id) is None:
            raise SyntheticPromotionError("synthetic promotion global object is invalid")
        _require_revision(self.global_object_revision, "synthetic promotion object revision", minimum=0)
        _require_hash(self.before_digest, "synthetic promotion before digest")
        _require_hash(self.candidate_after_digest, "synthetic promotion candidate digest")
        if self.before_digest == self.candidate_after_digest:
            raise SyntheticPromotionError("synthetic promotion candidate does not change state")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "candidate_digest")
        if self.candidate_digest and self.candidate_digest != expected:
            raise SyntheticPromotionError("synthetic promotion candidate digest does not match")
        object.__setattr__(self, "source_object_ids", object_ids)
        object.__setattr__(self, "candidate_digest", expected)

    @classmethod
    def from_value(cls, value: object) -> "SyntheticA8PromotionCandidate":
        if isinstance(value, cls):
            return value
        expected = {
            "candidate_id",
            "source_project_id",
            "closure_proposal",
            "review_receipt",
            "source_object_ids",
            "global_object_id",
            "global_object_revision",
            "before_digest",
            "candidate_after_digest",
            "candidate_digest",
        }
        if (
            type(value) is not dict
            or set(value) != expected
            or not isinstance(value["closure_proposal"], dict)
            or not isinstance(value["review_receipt"], dict)
            or not isinstance(value["source_object_ids"], list)
        ):
            raise SyntheticPromotionError("synthetic promotion candidate has unsupported fields")
        validate_activation_v2_safe_content(value)
        try:
            return cls(
                candidate_id=value["candidate_id"],
                source_project_id=value["source_project_id"],
                closure_proposal=ClosureProposal.from_value(value["closure_proposal"]),
                review_receipt=ProposalReviewReceipt.from_value(value["review_receipt"]),
                source_object_ids=tuple(value["source_object_ids"]),
                global_object_id=value["global_object_id"],
                global_object_revision=value["global_object_revision"],
                before_digest=value["before_digest"],
                candidate_after_digest=value["candidate_after_digest"],
                candidate_digest=value["candidate_digest"],
            )
        except (KeyError, TypeError, SyntheticPromotionError, SemanticValidationError) as error:
            raise SyntheticPromotionError("synthetic promotion candidate is invalid") from error

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "candidate_id": self.candidate_id,
            "source_project_id": self.source_project_id,
            "closure_proposal": self.closure_proposal.to_dict(),
            "review_receipt": self.review_receipt.to_dict(),
            "source_object_ids": list(self.source_object_ids),
            "global_object_id": self.global_object_id,
            "global_object_revision": self.global_object_revision,
            "before_digest": self.before_digest,
            "candidate_after_digest": self.candidate_after_digest,
        }
        if include_digest:
            value["candidate_digest"] = self.candidate_digest
        return value


@dataclass(frozen=True)
class SyntheticA8PromotionCorpus:
    """A closed, reviewed public-synthetic corpus for exactly one project."""

    schema_version: int
    corpus_id: str
    data_class: str
    synthetic_project_id: str
    a7_packet_id: str
    a7_packet_digest: str
    implementation_digest: str
    captured_at: str
    expires_at: str
    candidates: tuple[SyntheticA8PromotionCandidate, ...]
    corpus_digest: str = field(default="")

    def __post_init__(self) -> None:
        if (
            type(self) is not SyntheticA8PromotionCorpus
            or type(self.schema_version) is not int
            or self.schema_version != A8_PROMOTION_CORPUS_VERSION
        ):
            raise SyntheticPromotionError("synthetic promotion corpus version is invalid")
        _require_prefixed_uuid(self.corpus_id, "a8-promotion-corpus", "synthetic promotion corpus")
        if self.data_class != "PUBLIC_SYNTHETIC":
            raise SyntheticPromotionError("synthetic promotion corpus data class is invalid")
        _require_project_id(self.synthetic_project_id, "synthetic promotion corpus project")
        _require_prefixed_uuid(self.a7_packet_id, "a7-packet", "synthetic promotion source packet")
        _require_hash(self.a7_packet_digest, "synthetic promotion source packet digest")
        _require_hash(self.implementation_digest, "synthetic promotion implementation digest")
        captured_at = _require_timestamp(self.captured_at, "synthetic promotion corpus timestamp")
        expires_at = _require_timestamp(self.expires_at, "synthetic promotion corpus expiry")
        if parse_rfc3339_utc(expires_at) <= parse_rfc3339_utc(captured_at):
            raise SyntheticPromotionError("synthetic promotion corpus expiry is invalid")
        if (
            not isinstance(self.candidates, tuple)
            or not 1 <= len(self.candidates) <= MAX_A8_CANDIDATES
            or any(type(candidate) is not SyntheticA8PromotionCandidate for candidate in self.candidates)
            or len({candidate.candidate_id for candidate in self.candidates}) != len(self.candidates)
            or tuple(candidate.candidate_id for candidate in self.candidates)
            != tuple(sorted(candidate.candidate_id for candidate in self.candidates))
            or len({candidate.global_object_id for candidate in self.candidates}) != len(self.candidates)
            or any(candidate.source_project_id != self.synthetic_project_id for candidate in self.candidates)
        ):
            raise SyntheticPromotionError("synthetic promotion corpus candidates are invalid")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "corpus_digest")
        if self.corpus_digest and self.corpus_digest != expected:
            raise SyntheticPromotionError("synthetic promotion corpus digest does not match")
        object.__setattr__(self, "corpus_digest", expected)

    @classmethod
    def from_value(cls, value: object) -> "SyntheticA8PromotionCorpus":
        if isinstance(value, cls):
            return value
        expected = {
            "schema_version",
            "corpus_id",
            "data_class",
            "synthetic_project_id",
            "a7_packet_id",
            "a7_packet_digest",
            "implementation_digest",
            "captured_at",
            "expires_at",
            "candidates",
            "corpus_digest",
        }
        if type(value) is not dict or set(value) != expected or not isinstance(value["candidates"], list):
            raise SyntheticPromotionError("synthetic promotion corpus has unsupported fields")
        validate_activation_v2_safe_content(value)
        try:
            return cls(
                schema_version=value["schema_version"],
                corpus_id=value["corpus_id"],
                data_class=value["data_class"],
                synthetic_project_id=value["synthetic_project_id"],
                a7_packet_id=value["a7_packet_id"],
                a7_packet_digest=value["a7_packet_digest"],
                implementation_digest=value["implementation_digest"],
                captured_at=value["captured_at"],
                expires_at=value["expires_at"],
                candidates=tuple(SyntheticA8PromotionCandidate.from_value(item) for item in value["candidates"]),
                corpus_digest=value["corpus_digest"],
            )
        except (KeyError, TypeError, SyntheticPromotionError) as error:
            raise SyntheticPromotionError("synthetic promotion corpus is invalid") from error

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "corpus_id": self.corpus_id,
            "data_class": self.data_class,
            "synthetic_project_id": self.synthetic_project_id,
            "a7_packet_id": self.a7_packet_id,
            "a7_packet_digest": self.a7_packet_digest,
            "implementation_digest": self.implementation_digest,
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }
        if include_digest:
            value["corpus_digest"] = self.corpus_digest
        return value


@dataclass(frozen=True)
class A8PacketCandidate:
    """Digest-only packet binding for one reviewed promotion candidate."""

    candidate_id: str
    source_project_id: str
    global_object_id: str
    current_revision: int
    before_digest: str
    candidate_after_digest: str
    candidate_digest: str

    def __post_init__(self) -> None:
        if type(self) is not A8PacketCandidate:
            raise SyntheticPromotionError("synthetic promotion packet candidate is invalid")
        _require_prefixed_uuid(self.candidate_id, "a8-candidate", "synthetic promotion packet candidate")
        _require_project_id(self.source_project_id, "synthetic promotion packet project")
        if not isinstance(self.global_object_id, str) or _GLOBAL_CLAIM.fullmatch(self.global_object_id) is None:
            raise SyntheticPromotionError("synthetic promotion packet global object is invalid")
        _require_revision(self.current_revision, "synthetic promotion packet revision", minimum=0)
        _require_hash(self.before_digest, "synthetic promotion packet before digest")
        _require_hash(self.candidate_after_digest, "synthetic promotion packet candidate digest")
        _require_hash(self.candidate_digest, "synthetic promotion packet provenance digest")
        if self.before_digest == self.candidate_after_digest:
            raise SyntheticPromotionError("synthetic promotion packet candidate does not change state")

    @classmethod
    def from_candidate(cls, candidate: SyntheticA8PromotionCandidate) -> "A8PacketCandidate":
        return cls(
            candidate_id=candidate.candidate_id,
            source_project_id=candidate.source_project_id,
            global_object_id=candidate.global_object_id,
            current_revision=candidate.global_object_revision,
            before_digest=candidate.before_digest,
            candidate_after_digest=candidate.candidate_after_digest,
            candidate_digest=candidate.candidate_digest,
        )

    @classmethod
    def from_value(cls, value: object) -> "A8PacketCandidate":
        expected = {
            "candidate_id",
            "source_project_id",
            "global_object_id",
            "current_revision",
            "before_digest",
            "candidate_after_digest",
            "candidate_digest",
        }
        if type(value) is not dict or set(value) != expected:
            raise SyntheticPromotionError("synthetic promotion packet candidate is invalid")
        try:
            return cls(**value)
        except (TypeError, SyntheticPromotionError) as error:
            raise SyntheticPromotionError("synthetic promotion packet candidate is invalid") from error

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "source_project_id": self.source_project_id,
            "global_object_id": self.global_object_id,
            "current_revision": self.current_revision,
            "before_digest": self.before_digest,
            "candidate_after_digest": self.candidate_after_digest,
            "candidate_digest": self.candidate_digest,
        }


@dataclass(frozen=True)
class SyntheticA8PromotionPacket:
    """A non-authorizing packet bound to reviewed synthetic candidates only."""

    schema_version: int
    packet_id: str
    a7_packet_id: str
    a7_packet_digest: str
    corpus_id: str
    corpus_digest: str
    synthetic_project_id: str
    policy_digest: str
    implementation_digest: str
    expires_at: str
    review_mode: str
    candidates: tuple[A8PacketCandidate, ...]
    backup_required: bool
    promotion_canary_required: bool
    readback_required: bool
    restore_required: bool
    canary_sequence: tuple[str, ...]
    permission_impact: str
    rollback_trigger_codes: tuple[str, ...]
    network_permitted: bool
    global_target_access: bool
    global_mutation_authorized: bool
    requires_current_user_approval: bool
    approval_state: str
    fixture_only: bool
    created_at: str
    packet_digest: str = field(default="")

    def __post_init__(self) -> None:
        if (
            type(self) is not SyntheticA8PromotionPacket
            or type(self.schema_version) is not int
            or self.schema_version != A8_PACKET_VERSION
        ):
            raise SyntheticPromotionError("synthetic promotion packet version is invalid")
        _require_prefixed_uuid(self.packet_id, "a8-packet", "synthetic promotion packet")
        _require_prefixed_uuid(self.a7_packet_id, "a7-packet", "synthetic promotion source packet")
        _require_hash(self.a7_packet_digest, "synthetic promotion source packet digest")
        _require_prefixed_uuid(self.corpus_id, "a8-promotion-corpus", "synthetic promotion corpus")
        _require_hash(self.corpus_digest, "synthetic promotion corpus digest")
        _require_project_id(self.synthetic_project_id, "synthetic promotion packet project")
        _require_hash(self.policy_digest, "synthetic promotion policy digest")
        _require_hash(self.implementation_digest, "synthetic promotion implementation digest")
        _require_timestamp(self.expires_at, "synthetic promotion packet expiry")
        if (
            self.review_mode != "PER_ITEM"
            or not isinstance(self.candidates, tuple)
            or not 1 <= len(self.candidates) <= MAX_A8_CANDIDATES
            or any(type(candidate) is not A8PacketCandidate for candidate in self.candidates)
            or len({candidate.candidate_id for candidate in self.candidates}) != len(self.candidates)
            or tuple(candidate.candidate_id for candidate in self.candidates)
            != tuple(sorted(candidate.candidate_id for candidate in self.candidates))
            or len({candidate.global_object_id for candidate in self.candidates}) != len(self.candidates)
            or any(candidate.source_project_id != self.synthetic_project_id for candidate in self.candidates)
            or self.backup_required is not True
            or self.promotion_canary_required is not True
            or self.readback_required is not True
            or self.restore_required is not True
            or self.canary_sequence != _SEQUENCE
            or self.permission_impact != "NONE_SYNTHETIC"
            or self.rollback_trigger_codes != _ROLLBACK_TRIGGER_CODES
            or self.network_permitted is not False
            or self.global_target_access is not False
            or self.global_mutation_authorized is not False
            or self.requires_current_user_approval is not True
            or self.approval_state != "SYNTHETIC_DRAFT_NOT_APPROVED"
            or self.fixture_only is not True
        ):
            raise SyntheticPromotionError("synthetic promotion packet exceeds its local boundary")
        created_at = _require_timestamp(self.created_at, "synthetic promotion packet timestamp")
        if parse_rfc3339_utc(created_at) > parse_rfc3339_utc(self.expires_at):
            raise SyntheticPromotionError("synthetic promotion packet expiry is invalid")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "packet_digest")
        if self.packet_digest and self.packet_digest != expected:
            raise SyntheticPromotionError("synthetic promotion packet digest does not match")
        object.__setattr__(self, "packet_digest", expected)

    @classmethod
    def from_value(cls, value: object) -> "SyntheticA8PromotionPacket":
        if isinstance(value, cls):
            return value
        expected = {
            "schema_version",
            "packet_id",
            "a7_packet_id",
            "a7_packet_digest",
            "corpus_id",
            "corpus_digest",
            "synthetic_project_id",
            "policy_digest",
            "implementation_digest",
            "expires_at",
            "review_mode",
            "candidates",
            "backup_required",
            "promotion_canary_required",
            "readback_required",
            "restore_required",
            "canary_sequence",
            "permission_impact",
            "rollback_trigger_codes",
            "network_permitted",
            "global_target_access",
            "global_mutation_authorized",
            "requires_current_user_approval",
            "approval_state",
            "fixture_only",
            "created_at",
            "packet_digest",
        }
        if (
            type(value) is not dict
            or set(value) != expected
            or not isinstance(value["candidates"], list)
            or not isinstance(value["canary_sequence"], list)
            or not isinstance(value["rollback_trigger_codes"], list)
        ):
            raise SyntheticPromotionError("synthetic promotion packet has unsupported fields")
        validate_activation_v2_safe_content(value)
        try:
            return cls(
                schema_version=value["schema_version"],
                packet_id=value["packet_id"],
                a7_packet_id=value["a7_packet_id"],
                a7_packet_digest=value["a7_packet_digest"],
                corpus_id=value["corpus_id"],
                corpus_digest=value["corpus_digest"],
                synthetic_project_id=value["synthetic_project_id"],
                policy_digest=value["policy_digest"],
                implementation_digest=value["implementation_digest"],
                expires_at=value["expires_at"],
                review_mode=value["review_mode"],
                candidates=tuple(A8PacketCandidate.from_value(item) for item in value["candidates"]),
                backup_required=value["backup_required"],
                promotion_canary_required=value["promotion_canary_required"],
                readback_required=value["readback_required"],
                restore_required=value["restore_required"],
                canary_sequence=tuple(value["canary_sequence"]),
                permission_impact=value["permission_impact"],
                rollback_trigger_codes=tuple(value["rollback_trigger_codes"]),
                network_permitted=value["network_permitted"],
                global_target_access=value["global_target_access"],
                global_mutation_authorized=value["global_mutation_authorized"],
                requires_current_user_approval=value["requires_current_user_approval"],
                approval_state=value["approval_state"],
                fixture_only=value["fixture_only"],
                created_at=value["created_at"],
                packet_digest=value["packet_digest"],
            )
        except (KeyError, TypeError, SyntheticPromotionError) as error:
            raise SyntheticPromotionError("synthetic promotion packet is invalid") from error

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "packet_id": self.packet_id,
            "a7_packet_id": self.a7_packet_id,
            "a7_packet_digest": self.a7_packet_digest,
            "corpus_id": self.corpus_id,
            "corpus_digest": self.corpus_digest,
            "synthetic_project_id": self.synthetic_project_id,
            "policy_digest": self.policy_digest,
            "implementation_digest": self.implementation_digest,
            "expires_at": self.expires_at,
            "review_mode": self.review_mode,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "backup_required": self.backup_required,
            "promotion_canary_required": self.promotion_canary_required,
            "readback_required": self.readback_required,
            "restore_required": self.restore_required,
            "canary_sequence": list(self.canary_sequence),
            "permission_impact": self.permission_impact,
            "rollback_trigger_codes": list(self.rollback_trigger_codes),
            "network_permitted": self.network_permitted,
            "global_target_access": self.global_target_access,
            "global_mutation_authorized": self.global_mutation_authorized,
            "requires_current_user_approval": self.requires_current_user_approval,
            "approval_state": self.approval_state,
            "fixture_only": self.fixture_only,
            "created_at": self.created_at,
        }
        if include_digest:
            value["packet_digest"] = self.packet_digest
        return value


@dataclass(frozen=True)
class SyntheticA8CandidateState:
    """Current opaque candidate state within the disposable evaluator."""

    candidate_id: str
    source_project_id: str
    global_object_id: str
    content_digest: str
    object_revision: int
    promotion_state: str

    def __post_init__(self) -> None:
        if type(self) is not SyntheticA8CandidateState:
            raise SyntheticPromotionError("synthetic promotion candidate state is invalid")
        _require_prefixed_uuid(self.candidate_id, "a8-candidate", "synthetic promotion state candidate")
        _require_project_id(self.source_project_id, "synthetic promotion state project")
        if not isinstance(self.global_object_id, str) or _GLOBAL_CLAIM.fullmatch(self.global_object_id) is None:
            raise SyntheticPromotionError("synthetic promotion state global object is invalid")
        _require_hash(self.content_digest, "synthetic promotion state digest")
        _require_revision(self.object_revision, "synthetic promotion state revision", minimum=0)
        if self.promotion_state not in {"before", "promoted", "restored"}:
            raise SyntheticPromotionError("synthetic promotion state phase is invalid")

    @classmethod
    def from_value(cls, value: object) -> "SyntheticA8CandidateState":
        expected = {
            "candidate_id",
            "source_project_id",
            "global_object_id",
            "content_digest",
            "object_revision",
            "promotion_state",
        }
        if type(value) is not dict or set(value) != expected:
            raise SyntheticPromotionError("synthetic promotion candidate state is invalid")
        try:
            return cls(**value)
        except (TypeError, SyntheticPromotionError) as error:
            raise SyntheticPromotionError("synthetic promotion candidate state is invalid") from error

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "source_project_id": self.source_project_id,
            "global_object_id": self.global_object_id,
            "content_digest": self.content_digest,
            "object_revision": self.object_revision,
            "promotion_state": self.promotion_state,
        }


@dataclass(frozen=True)
class SyntheticA8PromotionState:
    """CAS-protected evaluator state, never a global authority store."""

    schema_version: int
    packet_id: str
    packet_digest: str
    corpus_digest: str
    a7_packet_digest: str
    runtime_id: str
    synthetic_project_id: str
    state_revision: int
    phase: str
    candidates: tuple[SyntheticA8CandidateState, ...]
    state_digest: str = field(default="")

    def __post_init__(self) -> None:
        if (
            type(self) is not SyntheticA8PromotionState
            or type(self.schema_version) is not int
            or self.schema_version != A8_STATE_VERSION
        ):
            raise SyntheticPromotionError("synthetic promotion state version is invalid")
        _require_prefixed_uuid(self.packet_id, "a8-packet", "synthetic promotion state packet")
        _require_hash(self.packet_digest, "synthetic promotion state packet digest")
        _require_hash(self.corpus_digest, "synthetic promotion state corpus digest")
        _require_hash(self.a7_packet_digest, "synthetic promotion state A7 packet digest")
        _require_prefixed_uuid(self.runtime_id, "runtime", "synthetic promotion runtime")
        _require_project_id(self.synthetic_project_id, "synthetic promotion state project")
        _require_revision(self.state_revision, "synthetic promotion state revision", minimum=0)
        if (
            self.phase not in {"initialized", "promoted", "restored"}
            or not isinstance(self.candidates, tuple)
            or not 1 <= len(self.candidates) <= MAX_A8_CANDIDATES
            or any(type(candidate) is not SyntheticA8CandidateState for candidate in self.candidates)
            or len({candidate.candidate_id for candidate in self.candidates}) != len(self.candidates)
            or tuple(candidate.candidate_id for candidate in self.candidates)
            != tuple(sorted(candidate.candidate_id for candidate in self.candidates))
            or len({candidate.global_object_id for candidate in self.candidates}) != len(self.candidates)
            or any(candidate.source_project_id != self.synthetic_project_id for candidate in self.candidates)
        ):
            raise SyntheticPromotionError("synthetic promotion state candidates are invalid")
        expected_phase = {0: "initialized", 1: "promoted", 2: "restored"}.get(self.state_revision)
        expected_candidate_state = {0: "before", 1: "promoted", 2: "restored"}.get(self.state_revision)
        if expected_phase != self.phase or any(candidate.promotion_state != expected_candidate_state for candidate in self.candidates):
            raise SyntheticPromotionError("synthetic promotion state phase is inconsistent")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "state_digest")
        if self.state_digest and self.state_digest != expected:
            raise SyntheticPromotionError("synthetic promotion state digest does not match")
        object.__setattr__(self, "state_digest", expected)

    @classmethod
    def initial(cls, packet: SyntheticA8PromotionPacket, runtime_id: str) -> "SyntheticA8PromotionState":
        return cls(
            schema_version=A8_STATE_VERSION,
            packet_id=packet.packet_id,
            packet_digest=packet.packet_digest,
            corpus_digest=packet.corpus_digest,
            a7_packet_digest=packet.a7_packet_digest,
            runtime_id=runtime_id,
            synthetic_project_id=packet.synthetic_project_id,
            state_revision=0,
            phase="initialized",
            candidates=tuple(
                SyntheticA8CandidateState(
                    candidate_id=candidate.candidate_id,
                    source_project_id=candidate.source_project_id,
                    global_object_id=candidate.global_object_id,
                    content_digest=candidate.before_digest,
                    object_revision=candidate.current_revision,
                    promotion_state="before",
                )
                for candidate in packet.candidates
            ),
        )

    @classmethod
    def from_value(cls, value: object) -> "SyntheticA8PromotionState":
        expected = {
            "schema_version",
            "packet_id",
            "packet_digest",
            "corpus_digest",
            "a7_packet_digest",
            "runtime_id",
            "synthetic_project_id",
            "state_revision",
            "phase",
            "candidates",
            "state_digest",
        }
        if type(value) is not dict or set(value) != expected or not isinstance(value["candidates"], list):
            raise SyntheticPromotionError("synthetic promotion state has unsupported fields")
        try:
            return cls(
                schema_version=value["schema_version"],
                packet_id=value["packet_id"],
                packet_digest=value["packet_digest"],
                corpus_digest=value["corpus_digest"],
                a7_packet_digest=value["a7_packet_digest"],
                runtime_id=value["runtime_id"],
                synthetic_project_id=value["synthetic_project_id"],
                state_revision=value["state_revision"],
                phase=value["phase"],
                candidates=tuple(SyntheticA8CandidateState.from_value(item) for item in value["candidates"]),
                state_digest=value["state_digest"],
            )
        except (KeyError, TypeError, SyntheticPromotionError) as error:
            raise SyntheticPromotionError("synthetic promotion state is invalid") from error

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "packet_id": self.packet_id,
            "packet_digest": self.packet_digest,
            "corpus_digest": self.corpus_digest,
            "a7_packet_digest": self.a7_packet_digest,
            "runtime_id": self.runtime_id,
            "synthetic_project_id": self.synthetic_project_id,
            "state_revision": self.state_revision,
            "phase": self.phase,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }
        if include_digest:
            value["state_digest"] = self.state_digest
        return value


@dataclass(frozen=True)
class SyntheticA8PromotionReceipt:
    """Content-free evidence for one synthetic A8 evaluator operation."""

    schema_version: int
    receipt_id: str
    operation: str
    status: str
    packet_id: str
    packet_digest: str
    corpus_digest: str
    a7_packet_digest: str
    runtime_id: str
    synthetic_project_id: str
    backup_id: str
    backup_digest: str
    expected_state_revision: int
    previous_state_revision: int
    state_revision: int
    candidate_states: tuple[SyntheticA8CandidateState, ...]
    fixture_only: bool
    network_calls: int
    global_target_access: bool
    global_mutation: bool
    authority_write: bool
    persistent_user_memory_written: bool
    created_at: str
    receipt_digest: str = field(default="")

    def __post_init__(self) -> None:
        if (
            type(self) is not SyntheticA8PromotionReceipt
            or type(self.schema_version) is not int
            or self.schema_version != A8_RECEIPT_VERSION
        ):
            raise SyntheticPromotionError("synthetic promotion receipt version is invalid")
        _require_prefixed_uuid(self.receipt_id, "a8-promotion-receipt", "synthetic promotion receipt")
        if self.operation not in _OPERATIONS or self.status != _STATUS_BY_OPERATION.get(self.operation):
            raise SyntheticPromotionError("synthetic promotion receipt operation is invalid")
        _require_prefixed_uuid(self.packet_id, "a8-packet", "synthetic promotion receipt packet")
        _require_hash(self.packet_digest, "synthetic promotion receipt packet digest")
        _require_hash(self.corpus_digest, "synthetic promotion receipt corpus digest")
        _require_hash(self.a7_packet_digest, "synthetic promotion receipt A7 packet digest")
        _require_prefixed_uuid(self.runtime_id, "runtime", "synthetic promotion receipt runtime")
        _require_project_id(self.synthetic_project_id, "synthetic promotion receipt project")
        _require_prefixed_uuid(self.backup_id, "a8-backup", "synthetic promotion backup")
        _require_hash(self.backup_digest, "synthetic promotion backup digest")
        _require_revision(self.expected_state_revision, "synthetic promotion expected revision", minimum=0)
        _require_revision(self.previous_state_revision, "synthetic promotion previous revision", minimum=0)
        _require_revision(self.state_revision, "synthetic promotion state revision", minimum=0)
        if self.operation == "backup" and (self.previous_state_revision, self.state_revision) != (0, 0):
            raise SyntheticPromotionError("synthetic promotion backup receipt revision is invalid")
        if self.operation == "promotion_canary" and (self.previous_state_revision, self.state_revision) != (0, 1):
            raise SyntheticPromotionError("synthetic promotion canary receipt revision is invalid")
        if self.operation == "readback" and (self.previous_state_revision, self.state_revision) != (1, 1):
            raise SyntheticPromotionError("synthetic promotion readback receipt revision is invalid")
        if self.operation == "restore" and (self.previous_state_revision, self.state_revision) != (1, 2):
            raise SyntheticPromotionError("synthetic promotion restore receipt revision is invalid")
        if self.expected_state_revision != self.previous_state_revision:
            raise SyntheticPromotionError("synthetic promotion receipt compare-and-swap revision is invalid")
        if (
            not isinstance(self.candidate_states, tuple)
            or not 1 <= len(self.candidate_states) <= MAX_A8_CANDIDATES
            or any(type(candidate) is not SyntheticA8CandidateState for candidate in self.candidate_states)
            or len({candidate.candidate_id for candidate in self.candidate_states}) != len(self.candidate_states)
            or tuple(candidate.candidate_id for candidate in self.candidate_states)
            != tuple(sorted(candidate.candidate_id for candidate in self.candidate_states))
            or any(candidate.source_project_id != self.synthetic_project_id for candidate in self.candidate_states)
            or self.fixture_only is not True
            or type(self.network_calls) is not int
            or self.network_calls != 0
            or self.global_target_access is not False
            or self.global_mutation is not False
            or self.authority_write is not False
            or self.persistent_user_memory_written is not False
        ):
            raise SyntheticPromotionError("synthetic promotion receipt exceeds its local boundary")
        _require_timestamp(self.created_at, "synthetic promotion receipt timestamp")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "receipt_digest")
        if self.receipt_digest and self.receipt_digest != expected:
            raise SyntheticPromotionError("synthetic promotion receipt digest does not match")
        object.__setattr__(self, "receipt_digest", expected)

    @classmethod
    def from_value(cls, value: object) -> "SyntheticA8PromotionReceipt":
        if isinstance(value, cls):
            return value
        expected = {
            "schema_version",
            "receipt_id",
            "operation",
            "status",
            "packet_id",
            "packet_digest",
            "corpus_digest",
            "a7_packet_digest",
            "runtime_id",
            "synthetic_project_id",
            "backup_id",
            "backup_digest",
            "expected_state_revision",
            "previous_state_revision",
            "state_revision",
            "candidate_states",
            "fixture_only",
            "network_calls",
            "global_target_access",
            "global_mutation",
            "authority_write",
            "persistent_user_memory_written",
            "created_at",
            "receipt_digest",
        }
        if type(value) is not dict or set(value) != expected or not isinstance(value["candidate_states"], list):
            raise SyntheticPromotionError("synthetic promotion receipt has unsupported fields")
        validate_activation_v2_safe_content(value)
        try:
            return cls(
                schema_version=value["schema_version"],
                receipt_id=value["receipt_id"],
                operation=value["operation"],
                status=value["status"],
                packet_id=value["packet_id"],
                packet_digest=value["packet_digest"],
                corpus_digest=value["corpus_digest"],
                a7_packet_digest=value["a7_packet_digest"],
                runtime_id=value["runtime_id"],
                synthetic_project_id=value["synthetic_project_id"],
                backup_id=value["backup_id"],
                backup_digest=value["backup_digest"],
                expected_state_revision=value["expected_state_revision"],
                previous_state_revision=value["previous_state_revision"],
                state_revision=value["state_revision"],
                candidate_states=tuple(SyntheticA8CandidateState.from_value(item) for item in value["candidate_states"]),
                fixture_only=value["fixture_only"],
                network_calls=value["network_calls"],
                global_target_access=value["global_target_access"],
                global_mutation=value["global_mutation"],
                authority_write=value["authority_write"],
                persistent_user_memory_written=value["persistent_user_memory_written"],
                created_at=value["created_at"],
                receipt_digest=value["receipt_digest"],
            )
        except (KeyError, TypeError, SyntheticPromotionError) as error:
            raise SyntheticPromotionError("synthetic promotion receipt is invalid") from error

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "operation": self.operation,
            "status": self.status,
            "packet_id": self.packet_id,
            "packet_digest": self.packet_digest,
            "corpus_digest": self.corpus_digest,
            "a7_packet_digest": self.a7_packet_digest,
            "runtime_id": self.runtime_id,
            "synthetic_project_id": self.synthetic_project_id,
            "backup_id": self.backup_id,
            "backup_digest": self.backup_digest,
            "expected_state_revision": self.expected_state_revision,
            "previous_state_revision": self.previous_state_revision,
            "state_revision": self.state_revision,
            "candidate_states": [candidate.to_dict() for candidate in self.candidate_states],
            "fixture_only": self.fixture_only,
            "network_calls": self.network_calls,
            "global_target_access": self.global_target_access,
            "global_mutation": self.global_mutation,
            "authority_write": self.authority_write,
            "persistent_user_memory_written": self.persistent_user_memory_written,
            "created_at": self.created_at,
        }
        if include_digest:
            value["receipt_digest"] = self.receipt_digest
        return value


@dataclass(frozen=True)
class SyntheticA8PendingOperation:
    """Recoverable state-before-receipt journal for A8 synthetic transitions."""

    schema_version: int
    journal_id: str
    operation: str
    packet_id: str
    packet_digest: str
    corpus_digest: str
    a7_packet_digest: str
    runtime_id: str
    previous_state: SyntheticA8PromotionState
    next_state: SyntheticA8PromotionState
    receipt: SyntheticA8PromotionReceipt
    pending_digest: str = field(default="")

    def __post_init__(self) -> None:
        if (
            type(self) is not SyntheticA8PendingOperation
            or type(self.schema_version) is not int
            or self.schema_version != A8_PENDING_OPERATION_VERSION
        ):
            raise SyntheticPromotionError("synthetic promotion pending operation version is invalid")
        _require_prefixed_uuid(self.journal_id, "a8-journal", "synthetic promotion journal")
        if self.operation not in _TRANSITION_OPERATIONS:
            raise SyntheticPromotionError("synthetic promotion journal operation is invalid")
        _require_prefixed_uuid(self.packet_id, "a8-packet", "synthetic promotion journal packet")
        _require_hash(self.packet_digest, "synthetic promotion journal packet digest")
        _require_hash(self.corpus_digest, "synthetic promotion journal corpus digest")
        _require_hash(self.a7_packet_digest, "synthetic promotion journal A7 packet digest")
        _require_prefixed_uuid(self.runtime_id, "runtime", "synthetic promotion journal runtime")
        if (
            type(self.previous_state) is not SyntheticA8PromotionState
            or type(self.next_state) is not SyntheticA8PromotionState
            or type(self.receipt) is not SyntheticA8PromotionReceipt
            or self.previous_state.packet_id != self.packet_id
            or self.next_state.packet_id != self.packet_id
            or self.receipt.packet_id != self.packet_id
            or self.previous_state.packet_digest != self.packet_digest
            or self.next_state.packet_digest != self.packet_digest
            or self.receipt.packet_digest != self.packet_digest
            or self.previous_state.corpus_digest != self.corpus_digest
            or self.next_state.corpus_digest != self.corpus_digest
            or self.receipt.corpus_digest != self.corpus_digest
            or self.previous_state.a7_packet_digest != self.a7_packet_digest
            or self.next_state.a7_packet_digest != self.a7_packet_digest
            or self.receipt.a7_packet_digest != self.a7_packet_digest
            or self.previous_state.runtime_id != self.runtime_id
            or self.next_state.runtime_id != self.runtime_id
            or self.receipt.runtime_id != self.runtime_id
            or self.receipt.operation != self.operation
            or self.receipt.previous_state_revision != self.previous_state.state_revision
            or self.receipt.state_revision != self.next_state.state_revision
        ):
            raise SyntheticPromotionError("synthetic promotion journal binding is invalid")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "pending_digest")
        if self.pending_digest and self.pending_digest != expected:
            raise SyntheticPromotionError("synthetic promotion journal digest does not match")
        object.__setattr__(self, "pending_digest", expected)

    @classmethod
    def from_value(cls, value: object) -> "SyntheticA8PendingOperation":
        if isinstance(value, cls):
            return value
        expected = {
            "schema_version",
            "journal_id",
            "operation",
            "packet_id",
            "packet_digest",
            "corpus_digest",
            "a7_packet_digest",
            "runtime_id",
            "previous_state",
            "next_state",
            "receipt",
            "pending_digest",
        }
        if (
            type(value) is not dict
            or set(value) != expected
            or not isinstance(value["previous_state"], dict)
            or not isinstance(value["next_state"], dict)
            or not isinstance(value["receipt"], dict)
        ):
            raise SyntheticPromotionError("synthetic promotion pending operation has unsupported fields")
        validate_activation_v2_safe_content(value)
        try:
            return cls(
                schema_version=value["schema_version"],
                journal_id=value["journal_id"],
                operation=value["operation"],
                packet_id=value["packet_id"],
                packet_digest=value["packet_digest"],
                corpus_digest=value["corpus_digest"],
                a7_packet_digest=value["a7_packet_digest"],
                runtime_id=value["runtime_id"],
                previous_state=SyntheticA8PromotionState.from_value(value["previous_state"]),
                next_state=SyntheticA8PromotionState.from_value(value["next_state"]),
                receipt=SyntheticA8PromotionReceipt.from_value(value["receipt"]),
                pending_digest=value["pending_digest"],
            )
        except (KeyError, TypeError, SyntheticPromotionError) as error:
            raise SyntheticPromotionError("synthetic promotion pending operation is invalid") from error

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "journal_id": self.journal_id,
            "operation": self.operation,
            "packet_id": self.packet_id,
            "packet_digest": self.packet_digest,
            "corpus_digest": self.corpus_digest,
            "a7_packet_digest": self.a7_packet_digest,
            "runtime_id": self.runtime_id,
            "previous_state": self.previous_state.to_dict(),
            "next_state": self.next_state.to_dict(),
            "receipt": self.receipt.to_dict(),
        }
        if include_digest:
            value["pending_digest"] = self.pending_digest
        return value


def validate_activation_v2_a8_document(name: str, document: dict[str, Any]) -> None:
    """Validate an A8 canonical artifact without treating it as authority."""

    if name not in A8_SCHEMA_NAMES or type(document) is not dict:
        raise SyntheticPromotionError("unknown Activation V2 A8 contract")
    validate_activation_v2_safe_content(document)
    if name == "activation-v2-a8-promotion-corpus-v1":
        artifact = SyntheticA8PromotionCorpus.from_value(document)
    elif name == "activation-v2-a8-promotion-packet-v1":
        artifact = SyntheticA8PromotionPacket.from_value(document)
    elif name == "activation-v2-a8-promotion-state-v1":
        artifact = SyntheticA8PromotionState.from_value(document)
    elif name == "activation-v2-a8-promotion-receipt-v1":
        artifact = SyntheticA8PromotionReceipt.from_value(document)
    else:
        artifact = SyntheticA8PendingOperation.from_value(document)
    if hasattr(artifact, "implementation_digest") and artifact.implementation_digest != synthetic_a8_implementation_digest():
        raise SyntheticPromotionError("SOURCE_DRIFT")


def prepare_synthetic_a8_packet(
    policy: PolicyInputs | Mapping[str, Any],
    a7_packet: SyntheticA7ApprovalPacket | Mapping[str, Any],
    corpus: SyntheticA8PromotionCorpus | Mapping[str, Any],
    *,
    packet_id: str | None = None,
    as_of: str | None = None,
) -> SyntheticA8PromotionPacket:
    """Build one non-authorizing A8 packet from exact reviewed fixture inputs."""

    selected_policy = PolicyInputs.from_value(policy)
    selected_a7 = _coerce_a7_packet(a7_packet)
    selected_corpus = _coerce_corpus(corpus)
    timestamp = _require_timestamp(as_of, "synthetic promotion packet timestamp")
    _require_policy_and_sources(selected_policy, selected_a7, selected_corpus, timestamp)
    return SyntheticA8PromotionPacket(
        schema_version=A8_PACKET_VERSION,
        packet_id=packet_id or f"a8-packet:{uuid.uuid4()}",
        a7_packet_id=selected_a7.packet_id,
        a7_packet_digest=selected_a7.packet_digest,
        corpus_id=selected_corpus.corpus_id,
        corpus_digest=selected_corpus.corpus_digest,
        synthetic_project_id=selected_corpus.synthetic_project_id,
        policy_digest=selected_policy.policy_digest,
        implementation_digest=synthetic_a8_implementation_digest(),
        expires_at=selected_corpus.expires_at,
        review_mode="PER_ITEM",
        candidates=tuple(A8PacketCandidate.from_candidate(candidate) for candidate in selected_corpus.candidates),
        backup_required=True,
        promotion_canary_required=True,
        readback_required=True,
        restore_required=True,
        canary_sequence=_SEQUENCE,
        permission_impact="NONE_SYNTHETIC",
        rollback_trigger_codes=_ROLLBACK_TRIGGER_CODES,
        network_permitted=False,
        global_target_access=False,
        global_mutation_authorized=False,
        requires_current_user_approval=True,
        approval_state="SYNTHETIC_DRAFT_NOT_APPROVED",
        fixture_only=True,
        created_at=timestamp,
    )


def initialize_synthetic_a8_promotion(
    runtime: DisposableRuntime,
    packet: SyntheticA8PromotionPacket | Mapping[str, Any],
    corpus: SyntheticA8PromotionCorpus | Mapping[str, Any],
    a7_packet: SyntheticA7ApprovalPacket | Mapping[str, Any],
    *,
    as_of: str | None = None,
) -> SyntheticA8PromotionState:
    """Create an empty synthetic evaluator state in one supplied runtime."""

    handle, selected_packet, selected_corpus, _ = _require_context(runtime, packet, corpus, a7_packet, as_of)
    with exclusive_disposable_runtime_lock(handle):
        if disposable_json_exists(handle, _state_path(selected_packet.packet_id)):
            raise SyntheticPromotionError("synthetic promotion state already exists")
        state = SyntheticA8PromotionState.initial(selected_packet, handle.manifest.runtime_id)
        _write_state(handle, state)
        return state


def create_synthetic_a8_backup(
    runtime: DisposableRuntime,
    packet: SyntheticA8PromotionPacket | Mapping[str, Any],
    corpus: SyntheticA8PromotionCorpus | Mapping[str, Any],
    a7_packet: SyntheticA7ApprovalPacket | Mapping[str, Any],
    *,
    as_of: str | None = None,
    expected_state_revision: int,
    backup_id: str | None = None,
    receipt_id: str | None = None,
) -> SyntheticA8PromotionReceipt:
    """Record the exact synthetic pre-promotion state before any canary."""

    handle, selected_packet, selected_corpus, timestamp = _require_context(runtime, packet, corpus, a7_packet, as_of)
    with exclusive_disposable_runtime_lock(handle):
        _recover_pending_operation(handle, selected_packet)
        state = _load_state_unrecovered(handle, selected_packet)
        _require_exact_revision(state, expected_state_revision, "synthetic promotion backup")
        _require_state_matches_packet(state, selected_packet, expected="before")
        backup_identity = backup_id or f"a8-backup:{uuid.uuid4()}"
        receipt = _receipt(
            receipt_id=receipt_id or f"a8-promotion-receipt:{uuid.uuid4()}",
            operation="backup",
            packet=selected_packet,
            state=state,
            expected_state_revision=expected_state_revision,
            previous_state_revision=state.state_revision,
            backup_id=backup_identity,
            backup_digest=_backup_digest(selected_packet, state),
            created_at=timestamp,
        )
        _write_receipt(handle, receipt)
        return receipt


def run_synthetic_a8_promotion_canary(
    runtime: DisposableRuntime,
    packet: SyntheticA8PromotionPacket | Mapping[str, Any],
    corpus: SyntheticA8PromotionCorpus | Mapping[str, Any],
    a7_packet: SyntheticA7ApprovalPacket | Mapping[str, Any],
    backup: SyntheticA8PromotionReceipt | Mapping[str, Any],
    *,
    as_of: str | None = None,
    expected_state_revision: int,
    receipt_id: str | None = None,
) -> SyntheticA8PromotionReceipt:
    """Apply a digest-only promotion canary in the disposable evaluator."""

    handle, selected_packet, selected_corpus, timestamp = _require_context(runtime, packet, corpus, a7_packet, as_of)
    with exclusive_disposable_runtime_lock(handle):
        _recover_pending_operation(handle, selected_packet)
        state = _load_state_unrecovered(handle, selected_packet)
        _require_exact_revision(state, expected_state_revision, "synthetic promotion canary")
        selected_backup = _require_backup(handle, backup, selected_packet, state)
        _require_state_matches_packet(state, selected_packet, expected="before")
        next_state = _next_state(selected_packet, state, phase="promoted")
        receipt = _receipt(
            receipt_id=receipt_id or f"a8-promotion-receipt:{uuid.uuid4()}",
            operation="promotion_canary",
            packet=selected_packet,
            state=next_state,
            expected_state_revision=expected_state_revision,
            previous_state_revision=state.state_revision,
            backup_id=selected_backup.backup_id,
            backup_digest=selected_backup.backup_digest,
            created_at=timestamp,
        )
        _write_state_then_receipt(handle, state, next_state, receipt)
        return receipt


def readback_synthetic_a8_promotion(
    runtime: DisposableRuntime,
    packet: SyntheticA8PromotionPacket | Mapping[str, Any],
    corpus: SyntheticA8PromotionCorpus | Mapping[str, Any],
    a7_packet: SyntheticA7ApprovalPacket | Mapping[str, Any],
    backup: SyntheticA8PromotionReceipt | Mapping[str, Any],
    canary: SyntheticA8PromotionReceipt | Mapping[str, Any],
    *,
    as_of: str | None = None,
    expected_state_revision: int,
    receipt_id: str | None = None,
) -> SyntheticA8PromotionReceipt:
    """Verify the exact canary state before allowing synthetic restoration."""

    handle, selected_packet, selected_corpus, timestamp = _require_context(runtime, packet, corpus, a7_packet, as_of)
    with exclusive_disposable_runtime_lock(handle):
        _recover_pending_operation(handle, selected_packet)
        state = _load_state_unrecovered(handle, selected_packet)
        _require_exact_revision(state, expected_state_revision, "synthetic promotion readback")
        selected_backup = _require_backup(handle, backup, selected_packet, None)
        _require_prior_receipt(handle, canary, selected_packet, selected_backup, "promotion_canary", state)
        _require_state_matches_packet(state, selected_packet, expected="promoted")
        receipt = _receipt(
            receipt_id=receipt_id or f"a8-promotion-receipt:{uuid.uuid4()}",
            operation="readback",
            packet=selected_packet,
            state=state,
            expected_state_revision=expected_state_revision,
            previous_state_revision=state.state_revision,
            backup_id=selected_backup.backup_id,
            backup_digest=selected_backup.backup_digest,
            created_at=timestamp,
        )
        _write_receipt(handle, receipt)
        return receipt


def restore_synthetic_a8_promotion(
    runtime: DisposableRuntime,
    packet: SyntheticA8PromotionPacket | Mapping[str, Any],
    corpus: SyntheticA8PromotionCorpus | Mapping[str, Any],
    a7_packet: SyntheticA7ApprovalPacket | Mapping[str, Any],
    backup: SyntheticA8PromotionReceipt | Mapping[str, Any],
    readback: SyntheticA8PromotionReceipt | Mapping[str, Any],
    *,
    as_of: str | None = None,
    expected_state_revision: int,
    receipt_id: str | None = None,
) -> SyntheticA8PromotionReceipt:
    """Restore pre-promotion digests after a persisted synthetic readback."""

    handle, selected_packet, selected_corpus, timestamp = _require_context(runtime, packet, corpus, a7_packet, as_of)
    with exclusive_disposable_runtime_lock(handle):
        _recover_pending_operation(handle, selected_packet)
        state = _load_state_unrecovered(handle, selected_packet)
        _require_exact_revision(state, expected_state_revision, "synthetic promotion restore")
        selected_backup = _require_backup(handle, backup, selected_packet, None)
        _require_prior_receipt(handle, readback, selected_packet, selected_backup, "readback", state)
        _require_state_matches_packet(state, selected_packet, expected="promoted")
        next_state = _next_state(selected_packet, state, phase="restored")
        receipt = _receipt(
            receipt_id=receipt_id or f"a8-promotion-receipt:{uuid.uuid4()}",
            operation="restore",
            packet=selected_packet,
            state=next_state,
            expected_state_revision=expected_state_revision,
            previous_state_revision=state.state_revision,
            backup_id=selected_backup.backup_id,
            backup_digest=selected_backup.backup_digest,
            created_at=timestamp,
        )
        _write_state_then_receipt(handle, state, next_state, receipt)
        return receipt


def load_synthetic_a8_promotion_state(
    runtime: DisposableRuntime,
    packet: SyntheticA8PromotionPacket | Mapping[str, Any],
    corpus: SyntheticA8PromotionCorpus | Mapping[str, Any],
    a7_packet: SyntheticA7ApprovalPacket | Mapping[str, Any],
    *,
    as_of: str | None = None,
) -> SyntheticA8PromotionState:
    """Read exact evaluator state, recovering only a known journal window."""

    handle, selected_packet, _, _ = _require_context(runtime, packet, corpus, a7_packet, as_of)
    with exclusive_disposable_runtime_lock(handle):
        _recover_pending_operation(handle, selected_packet)
        return _load_state_unrecovered(handle, selected_packet)


def load_synthetic_a8_promotion_receipt(
    runtime: DisposableRuntime,
    packet: SyntheticA8PromotionPacket | Mapping[str, Any],
    corpus: SyntheticA8PromotionCorpus | Mapping[str, Any],
    a7_packet: SyntheticA7ApprovalPacket | Mapping[str, Any],
    receipt_id: str,
    *,
    as_of: str | None = None,
) -> SyntheticA8PromotionReceipt:
    """Read one exact content-free A8 receipt from its typed runtime path."""

    handle, selected_packet, _, _ = _require_context(runtime, packet, corpus, a7_packet, as_of)
    with exclusive_disposable_runtime_lock(handle):
        _recover_pending_operation(handle, selected_packet)
        return _load_receipt_unrecovered(handle, receipt_id, selected_packet)


def _require_context(
    runtime: DisposableRuntime,
    packet: SyntheticA8PromotionPacket | Mapping[str, Any],
    corpus: SyntheticA8PromotionCorpus | Mapping[str, Any],
    a7_packet: SyntheticA7ApprovalPacket | Mapping[str, Any],
    as_of: str | None,
) -> tuple[DisposableRuntime, SyntheticA8PromotionPacket, SyntheticA8PromotionCorpus, str]:
    handle = load_disposable_runtime(runtime.root)
    selected_packet = _coerce_packet(packet)
    selected_corpus = _coerce_corpus(corpus)
    selected_a7 = _coerce_a7_packet(a7_packet)
    timestamp = _require_timestamp(as_of, "synthetic promotion as_of")
    _require_policy_and_sources(handle.manifest.policy, selected_a7, selected_corpus, timestamp)
    if (
        selected_packet.implementation_digest != synthetic_a8_implementation_digest()
        or selected_packet.policy_digest != handle.manifest.policy.policy_digest
        or selected_packet.a7_packet_id != selected_a7.packet_id
        or selected_packet.a7_packet_digest != selected_a7.packet_digest
        or selected_packet.corpus_id != selected_corpus.corpus_id
        or selected_packet.corpus_digest != selected_corpus.corpus_digest
        or selected_packet.synthetic_project_id != selected_corpus.synthetic_project_id
        or selected_packet.expires_at != selected_corpus.expires_at
        or parse_rfc3339_utc(selected_packet.created_at) > parse_rfc3339_utc(timestamp)
        or parse_rfc3339_utc(selected_packet.expires_at) <= parse_rfc3339_utc(timestamp)
        or selected_packet.candidates
        != tuple(A8PacketCandidate.from_candidate(candidate) for candidate in selected_corpus.candidates)
    ):
        raise SyntheticPromotionError("SOURCE_DRIFT")
    return handle, selected_packet, selected_corpus, timestamp


def _require_policy_and_sources(
    policy: PolicyInputs,
    a7_packet: SyntheticA7ApprovalPacket,
    corpus: SyntheticA8PromotionCorpus,
    timestamp: str,
) -> None:
    policy.require_resolved("A8")
    if not policy.fixture_only or policy.promotion_review_mode != "PER_ITEM":
        raise SyntheticPromotionError("synthetic promotion requires per-item fixture policy")
    if (
        a7_packet.implementation_digest != synthetic_a7_implementation_digest()
        or not a7_packet.fixture_only
        or a7_packet.network_permitted
        or a7_packet.global_target_access
        or a7_packet.global_mutation_authorized
        or a7_packet.approval_state != "SYNTHETIC_DRAFT_NOT_APPROVED"
        or a7_packet.policy_digest != policy.policy_digest
        or parse_rfc3339_utc(a7_packet.created_at) > parse_rfc3339_utc(timestamp)
        or parse_rfc3339_utc(a7_packet.expires_at) <= parse_rfc3339_utc(timestamp)
        or corpus.implementation_digest != synthetic_a8_implementation_digest()
        or corpus.a7_packet_id != a7_packet.packet_id
        or corpus.a7_packet_digest != a7_packet.packet_digest
        or corpus.synthetic_project_id != a7_packet.synthetic_project_id
        or parse_rfc3339_utc(corpus.captured_at) > parse_rfc3339_utc(timestamp)
        or parse_rfc3339_utc(corpus.expires_at) <= parse_rfc3339_utc(timestamp)
        or parse_rfc3339_utc(corpus.expires_at) > parse_rfc3339_utc(a7_packet.expires_at)
    ):
        raise SyntheticPromotionError("SOURCE_DRIFT")


def _coerce_a7_packet(value: SyntheticA7ApprovalPacket | Mapping[str, Any]) -> SyntheticA7ApprovalPacket:
    if isinstance(value, SyntheticA7ApprovalPacket):
        return value
    if type(value) is not dict:
        raise SyntheticPromotionError("synthetic promotion source packet is invalid")
    validate_activation_v2_safe_content(value)
    try:
        return SyntheticA7ApprovalPacket.from_value(value)
    except SemanticValidationError as error:
        raise SyntheticPromotionError("synthetic promotion source packet is invalid") from error


def _coerce_corpus(value: SyntheticA8PromotionCorpus | Mapping[str, Any]) -> SyntheticA8PromotionCorpus:
    if isinstance(value, SyntheticA8PromotionCorpus):
        return value
    if type(value) is not dict:
        raise SyntheticPromotionError("synthetic promotion corpus is invalid")
    return SyntheticA8PromotionCorpus.from_value(value)


def _coerce_packet(value: SyntheticA8PromotionPacket | Mapping[str, Any]) -> SyntheticA8PromotionPacket:
    if isinstance(value, SyntheticA8PromotionPacket):
        return value
    if type(value) is not dict:
        raise SyntheticPromotionError("synthetic promotion packet is invalid")
    return SyntheticA8PromotionPacket.from_value(value)


def _load_state_unrecovered(runtime: DisposableRuntime, packet: SyntheticA8PromotionPacket) -> SyntheticA8PromotionState:
    try:
        state = SyntheticA8PromotionState.from_value(read_disposable_json(runtime, _state_path(packet.packet_id)))
    except (IntegrityError, SemanticValidationError) as error:
        raise IntegrityError("synthetic promotion state is unavailable or invalid") from error
    if (
        state.packet_id != packet.packet_id
        or state.packet_digest != packet.packet_digest
        or state.corpus_digest != packet.corpus_digest
        or state.a7_packet_digest != packet.a7_packet_digest
        or state.runtime_id != runtime.manifest.runtime_id
        or state.synthetic_project_id != packet.synthetic_project_id
    ):
        raise IntegrityError("synthetic promotion state is bound to another evaluator")
    return state


def _load_receipt_unrecovered(
    runtime: DisposableRuntime,
    receipt_id: str,
    packet: SyntheticA8PromotionPacket,
) -> SyntheticA8PromotionReceipt:
    try:
        receipt = SyntheticA8PromotionReceipt.from_value(read_disposable_json(runtime, _receipt_path(receipt_id)))
    except (IntegrityError, SemanticValidationError) as error:
        raise IntegrityError("synthetic promotion receipt is unavailable or invalid") from error
    if not _receipt_matches_packet(receipt, packet, runtime.manifest.runtime_id):
        raise IntegrityError("synthetic promotion receipt is bound to another evaluator")
    return receipt


def _recover_pending_operation(runtime: DisposableRuntime, packet: SyntheticA8PromotionPacket) -> None:
    relative = _journal_path(packet.packet_id)
    if not disposable_json_exists(runtime, relative):
        return
    try:
        pending = SyntheticA8PendingOperation.from_value(read_disposable_json(runtime, relative))
    except (IntegrityError, SemanticValidationError) as error:
        raise IntegrityError("synthetic promotion pending operation is invalid") from error
    if (
        pending.packet_id != packet.packet_id
        or pending.packet_digest != packet.packet_digest
        or pending.corpus_digest != packet.corpus_digest
        or pending.a7_packet_digest != packet.a7_packet_digest
        or pending.runtime_id != runtime.manifest.runtime_id
    ):
        raise IntegrityError("synthetic promotion pending operation is bound to another evaluator")
    state = _load_state_unrecovered(runtime, packet)
    receipt_path = _receipt_path(pending.receipt.receipt_id)
    persisted = _load_receipt_unrecovered(runtime, pending.receipt.receipt_id, packet) if disposable_json_exists(runtime, receipt_path) else None
    if state == pending.previous_state and persisted is None:
        remove_disposable_json(runtime, relative)
        return
    if state == pending.next_state and persisted == pending.receipt:
        remove_disposable_json(runtime, relative)
        return
    if state == pending.next_state and persisted is None:
        _write_state(runtime, pending.previous_state)
        remove_disposable_json(runtime, relative)
        return
    raise IntegrityError("synthetic promotion pending operation recovery is inconsistent")


def _require_backup(
    runtime: DisposableRuntime,
    backup: SyntheticA8PromotionReceipt | Mapping[str, Any],
    packet: SyntheticA8PromotionPacket,
    current_state: SyntheticA8PromotionState | None,
) -> SyntheticA8PromotionReceipt:
    selected = _coerce_receipt(backup)
    if (
        selected.operation != "backup"
        or not _receipt_matches_packet(selected, packet, runtime.manifest.runtime_id)
        or selected.backup_digest != _backup_digest_from_states(packet, selected.runtime_id, selected.candidate_states)
    ):
        raise SyntheticPromotionError("synthetic promotion backup is invalid for this packet")
    _require_candidate_states_match_packet(selected.candidate_states, packet, expected="before")
    _require_persisted_receipt(runtime, selected, packet)
    if current_state is not None and selected.candidate_states != current_state.candidates:
        raise SyntheticPromotionError("synthetic promotion state does not match its backup")
    return selected


def _require_prior_receipt(
    runtime: DisposableRuntime,
    receipt: SyntheticA8PromotionReceipt | Mapping[str, Any],
    packet: SyntheticA8PromotionPacket,
    backup: SyntheticA8PromotionReceipt,
    operation: str,
    state: SyntheticA8PromotionState,
) -> SyntheticA8PromotionReceipt:
    selected = _coerce_receipt(receipt)
    if (
        selected.operation != operation
        or not _receipt_matches_packet(selected, packet, runtime.manifest.runtime_id)
        or selected.backup_id != backup.backup_id
        or selected.backup_digest != backup.backup_digest
        or selected.state_revision != state.state_revision
        or selected.candidate_states != state.candidates
    ):
        raise SyntheticPromotionError("synthetic promotion sequence receipt is invalid")
    _require_persisted_receipt(runtime, selected, packet)
    return selected


def _require_persisted_receipt(
    runtime: DisposableRuntime,
    receipt: SyntheticA8PromotionReceipt,
    packet: SyntheticA8PromotionPacket,
) -> None:
    try:
        stored = _load_receipt_unrecovered(runtime, receipt.receipt_id, packet)
    except IntegrityError as error:
        raise SyntheticPromotionError("synthetic promotion prerequisite receipt is unavailable") from error
    if stored != receipt:
        raise SyntheticPromotionError("synthetic promotion prerequisite receipt is not persisted exactly")


def _coerce_receipt(value: SyntheticA8PromotionReceipt | Mapping[str, Any]) -> SyntheticA8PromotionReceipt:
    if isinstance(value, SyntheticA8PromotionReceipt):
        return value
    if type(value) is not dict:
        raise SyntheticPromotionError("synthetic promotion receipt is invalid")
    return SyntheticA8PromotionReceipt.from_value(value)


def _receipt_matches_packet(
    receipt: SyntheticA8PromotionReceipt,
    packet: SyntheticA8PromotionPacket,
    runtime_id: str,
) -> bool:
    return (
        receipt.packet_id == packet.packet_id
        and receipt.packet_digest == packet.packet_digest
        and receipt.corpus_digest == packet.corpus_digest
        and receipt.a7_packet_digest == packet.a7_packet_digest
        and receipt.runtime_id == runtime_id
        and receipt.synthetic_project_id == packet.synthetic_project_id
    )


def _next_state(
    packet: SyntheticA8PromotionPacket,
    state: SyntheticA8PromotionState,
    *,
    phase: str,
) -> SyntheticA8PromotionState:
    if phase == "promoted":
        revision = 1
        candidate_state = "promoted"
        digests = {candidate.candidate_id: candidate.candidate_after_digest for candidate in packet.candidates}
        object_revisions = {candidate.candidate_id: candidate.current_revision + 1 for candidate in packet.candidates}
    elif phase == "restored":
        revision = 2
        candidate_state = "restored"
        digests = {candidate.candidate_id: candidate.before_digest for candidate in packet.candidates}
        object_revisions = {candidate.candidate_id: candidate.current_revision + 2 for candidate in packet.candidates}
    else:
        raise SyntheticPromotionError("synthetic promotion next phase is invalid")
    if state.state_revision != revision - 1:
        raise SyntheticPromotionError("synthetic promotion transition is stale")
    return SyntheticA8PromotionState(
        schema_version=A8_STATE_VERSION,
        packet_id=state.packet_id,
        packet_digest=state.packet_digest,
        corpus_digest=state.corpus_digest,
        a7_packet_digest=state.a7_packet_digest,
        runtime_id=state.runtime_id,
        synthetic_project_id=state.synthetic_project_id,
        state_revision=revision,
        phase=phase,
        candidates=tuple(
            SyntheticA8CandidateState(
                candidate_id=candidate.candidate_id,
                source_project_id=candidate.source_project_id,
                global_object_id=candidate.global_object_id,
                content_digest=digests[candidate.candidate_id],
                object_revision=object_revisions[candidate.candidate_id],
                promotion_state=candidate_state,
            )
            for candidate in packet.candidates
        ),
    )


def _require_exact_revision(state: SyntheticA8PromotionState, expected: object, label: str) -> None:
    _require_revision(expected, label + " expected revision", minimum=0)
    if state.state_revision != expected:
        raise SyntheticPromotionError(label + " compare-and-swap revision is stale")


def _require_state_matches_packet(
    state: SyntheticA8PromotionState,
    packet: SyntheticA8PromotionPacket,
    *,
    expected: str,
) -> None:
    _require_candidate_states_match_packet(state.candidates, packet, expected=expected)


def _require_candidate_states_match_packet(
    states: tuple[SyntheticA8CandidateState, ...],
    packet: SyntheticA8PromotionPacket,
    *,
    expected: str,
) -> None:
    if expected == "before":
        expected_digests = {candidate.candidate_id: candidate.before_digest for candidate in packet.candidates}
        expected_revisions = {candidate.candidate_id: candidate.current_revision for candidate in packet.candidates}
        expected_phase = "before"
    elif expected == "promoted":
        expected_digests = {candidate.candidate_id: candidate.candidate_after_digest for candidate in packet.candidates}
        expected_revisions = {candidate.candidate_id: candidate.current_revision + 1 for candidate in packet.candidates}
        expected_phase = "promoted"
    elif expected == "restored":
        expected_digests = {candidate.candidate_id: candidate.before_digest for candidate in packet.candidates}
        expected_revisions = {candidate.candidate_id: candidate.current_revision + 2 for candidate in packet.candidates}
        expected_phase = "restored"
    else:
        raise SyntheticPromotionError("synthetic promotion expected state is invalid")
    if (
        {state.candidate_id: state.content_digest for state in states} != expected_digests
        or {state.candidate_id: state.object_revision for state in states} != expected_revisions
        or any(state.promotion_state != expected_phase for state in states)
        or any(state.source_project_id != packet.synthetic_project_id for state in states)
    ):
        raise SyntheticPromotionError("synthetic promotion candidate state does not match the packet")


def _backup_digest(packet: SyntheticA8PromotionPacket, state: SyntheticA8PromotionState) -> str:
    return _backup_digest_from_states(packet, state.runtime_id, state.candidates)


def _backup_digest_from_states(
    packet: SyntheticA8PromotionPacket,
    runtime_id: str,
    states: tuple[SyntheticA8CandidateState, ...],
) -> str:
    return sha256_hex(
        {
            "packet_id": packet.packet_id,
            "packet_digest": packet.packet_digest,
            "corpus_digest": packet.corpus_digest,
            "a7_packet_digest": packet.a7_packet_digest,
            "runtime_id": runtime_id,
            "candidate_states": [state.to_dict() for state in states],
        }
    )


def _receipt(
    *,
    receipt_id: str,
    operation: str,
    packet: SyntheticA8PromotionPacket,
    state: SyntheticA8PromotionState,
    expected_state_revision: int,
    previous_state_revision: int,
    backup_id: str,
    backup_digest: str,
    created_at: str,
) -> SyntheticA8PromotionReceipt:
    return SyntheticA8PromotionReceipt(
        schema_version=A8_RECEIPT_VERSION,
        receipt_id=receipt_id,
        operation=operation,
        status=_STATUS_BY_OPERATION[operation],
        packet_id=packet.packet_id,
        packet_digest=packet.packet_digest,
        corpus_digest=packet.corpus_digest,
        a7_packet_digest=packet.a7_packet_digest,
        runtime_id=state.runtime_id,
        synthetic_project_id=packet.synthetic_project_id,
        backup_id=backup_id,
        backup_digest=backup_digest,
        expected_state_revision=expected_state_revision,
        previous_state_revision=previous_state_revision,
        state_revision=state.state_revision,
        candidate_states=state.candidates,
        fixture_only=True,
        network_calls=0,
        global_target_access=False,
        global_mutation=False,
        authority_write=False,
        persistent_user_memory_written=False,
        created_at=created_at,
    )


def _write_state_then_receipt(
    runtime: DisposableRuntime,
    previous_state: SyntheticA8PromotionState,
    next_state: SyntheticA8PromotionState,
    receipt: SyntheticA8PromotionReceipt,
) -> None:
    pending = SyntheticA8PendingOperation(
        schema_version=A8_PENDING_OPERATION_VERSION,
        journal_id=f"a8-journal:{uuid.uuid4()}",
        operation=receipt.operation,
        packet_id=receipt.packet_id,
        packet_digest=receipt.packet_digest,
        corpus_digest=receipt.corpus_digest,
        a7_packet_digest=receipt.a7_packet_digest,
        runtime_id=receipt.runtime_id,
        previous_state=previous_state,
        next_state=next_state,
        receipt=receipt,
    )
    _write_pending_operation(runtime, pending)
    _write_state(runtime, next_state)
    _write_receipt(runtime, receipt)
    remove_disposable_json(runtime, _journal_path(receipt.packet_id))


def _write_state(runtime: DisposableRuntime, state: SyntheticA8PromotionState) -> None:
    write_disposable_json(runtime, _state_path(state.packet_id), state.to_dict())


def _write_receipt(runtime: DisposableRuntime, receipt: SyntheticA8PromotionReceipt) -> None:
    relative = _receipt_path(receipt.receipt_id)
    if disposable_json_exists(runtime, relative):
        raise SyntheticPromotionError("synthetic promotion receipt identity already exists")
    write_disposable_json(runtime, relative, receipt.to_dict())


def _write_pending_operation(runtime: DisposableRuntime, pending: SyntheticA8PendingOperation) -> None:
    relative = _journal_path(pending.packet_id)
    if disposable_json_exists(runtime, relative):
        raise SyntheticPromotionError("synthetic promotion pending operation already exists")
    write_disposable_json(runtime, relative, pending.to_dict())


def _state_path(packet_id: str) -> str:
    return f"registry/a8-promotion/{packet_id.removeprefix('a8-packet:')}/state.json"


def _receipt_path(receipt_id: str) -> str:
    return f"receipts/a8-promotion/{receipt_id.removeprefix('a8-promotion-receipt:')}.json"


def _journal_path(packet_id: str) -> str:
    return f"registry/a8-promotion/{packet_id.removeprefix('a8-packet:')}/pending-operation.json"


def _project_object_ids(value: object, project_id: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not 1 <= len(value) <= 12 or any(not isinstance(item, str) for item in value):
        raise SyntheticPromotionError("synthetic promotion source objects are invalid")
    if len(set(value)) != len(value) or tuple(sorted(value)) != value:
        raise SyntheticPromotionError("synthetic promotion source objects are invalid")
    for object_id in value:
        match = _PROJECT_OBJECT.fullmatch(object_id)
        if match is None or match.group(1) != project_id:
            raise SyntheticPromotionError("synthetic promotion crosses a project boundary")
    return value


def _require_prefixed_uuid(value: object, prefix: str, label: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix + ":"):
        raise SyntheticPromotionError(f"{label} is invalid")
    if _UUID.fullmatch(value.removeprefix(prefix + ":")) is None:
        raise SyntheticPromotionError(f"{label} is invalid")
    return value


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise SyntheticPromotionError(f"{label} is invalid")
    return value


def _require_project_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _PROJECT_ID.fullmatch(value) is None:
        raise SyntheticPromotionError(f"{label} is invalid")
    return value


def _require_revision(value: object, label: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum or value > 2_147_483_647:
        raise SyntheticPromotionError(f"{label} is invalid")
    return value


def _require_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise SyntheticPromotionError(f"{label} is invalid")
    try:
        parse_rfc3339_utc(value)
    except Exception as error:
        raise SyntheticPromotionError(f"{label} is invalid") from error
    return value
