"""Synthetic-only A7 opt-in rollout packet and rollback rehearsal machinery.

This module intentionally has no filesystem discovery outside a disposable A1
runtime. It accepts only supplied public synthetic target snapshots containing
opaque IDs, revisions, and digests. It cannot inspect or mutate a global Codex
target, execute a canary process, or turn a packet into user approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Any, Mapping
import uuid

from .activation_v2 import activation_v2_logical_digest, validate_activation_v2_safe_content
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
from .contracts import validate_named_document
from .errors import ContractError, IntegrityError, SemanticValidationError
from .schema_validation import parse_rfc3339_utc
from .workspace import repository_root


A7_TARGET_BUNDLE_VERSION = 1
A7_PACKET_VERSION = 1
A7_STATE_VERSION = 1
A7_RECEIPT_VERSION = 1
A7_PENDING_OPERATION_VERSION = 1
MAX_A7_TARGETS = 2
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TARGET_ROLES = frozenset(("project_opt_in_config", "project_activation_manifest"))
_OPERATIONS = frozenset(("backup", "canary", "readback", "rollback"))
_STATUS_BY_OPERATION = {
    "backup": "BACKUP_CREATED_SYNTHETIC",
    "canary": "CANARY_APPLIED_SYNTHETIC",
    "readback": "READBACK_MATCH_SYNTHETIC",
    "rollback": "ROLLED_BACK_SYNTHETIC",
}
_CANARY_SEQUENCE = ("backup", "canary", "readback", "rollback")
_ROLLBACK_TRIGGER_CODES = (
    "BOUNDARY_VIOLATION",
    "CAS_CONFLICT",
    "READBACK_MISMATCH",
    "SOURCE_DRIFT",
)
_A7_IMPLEMENTATION_FILES = (
    "src/second_brain/activation_v2.py",
    "src/second_brain/activation_v2_runtime.py",
    "src/second_brain/activation_v2_rollout.py",
    "schemas/activation-v2-a7-target-bundle-v1.json",
    "schemas/activation-v2-a7-approval-packet-v1.json",
    "schemas/activation-v2-a7-rollout-state-v1.json",
    "schemas/activation-v2-a7-rollout-receipt-v1.json",
    "schemas/activation-v2-a7-pending-operation-v1.json",
)


class SyntheticRolloutError(SemanticValidationError):
    """An A7 local packet or synthetic state transition is unsafe."""


def synthetic_a7_implementation_digest() -> str:
    """Hash the checked-in synthetic implementation without retaining its source.

    This is an integrity binding for fixture drift, not a signature or an
    authorization mechanism. A real rollout needs independently trusted build
    provenance and a current user-approved packet.
    """

    root = repository_root()
    file_digests: dict[str, str] = {}
    try:
        for relative in _A7_IMPLEMENTATION_FILES:
            file_digests[relative] = hashlib.sha256((root / relative).read_bytes()).hexdigest()
    except OSError as error:
        raise SyntheticRolloutError("SOURCE_DRIFT") from error
    return sha256_hex(
        {
            "implementation_id": "activation-v2-a7-synthetic-rollout-v1",
            "files": file_digests,
        }
    )


@dataclass(frozen=True)
class SyntheticTargetSnapshot:
    """Opaque exact-before/after metadata for one supplied synthetic target."""

    target_id: str
    synthetic_project_id: str
    target_role: str
    current_revision: int
    before_digest: str
    candidate_after_digest: str
    snapshot_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not SyntheticTargetSnapshot:
            raise SyntheticRolloutError("synthetic target snapshot is invalid")
        _require_prefixed_uuid(self.target_id, "a7-target", "synthetic target")
        _require_project_id(self.synthetic_project_id, "synthetic target project")
        if self.target_role not in _TARGET_ROLES:
            raise SyntheticRolloutError("synthetic target role is invalid")
        _require_revision(self.current_revision, "synthetic target revision", minimum=0)
        _require_hash(self.before_digest, "synthetic target before digest")
        _require_hash(self.candidate_after_digest, "synthetic target candidate digest")
        if self.before_digest == self.candidate_after_digest:
            raise SyntheticRolloutError("synthetic target candidate does not change state")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "snapshot_digest")
        if self.snapshot_digest and self.snapshot_digest != expected:
            raise SyntheticRolloutError("synthetic target snapshot digest does not match")
        object.__setattr__(self, "snapshot_digest", expected)

    @classmethod
    def from_value(cls, value: object) -> "SyntheticTargetSnapshot":
        if isinstance(value, cls):
            return value
        expected = {
            "target_id",
            "synthetic_project_id",
            "target_role",
            "current_revision",
            "before_digest",
            "candidate_after_digest",
            "snapshot_digest",
        }
        if type(value) is not dict or set(value) != expected:
            raise SyntheticRolloutError("synthetic target snapshot is invalid")
        try:
            return cls(**value)
        except (TypeError, SyntheticRolloutError) as error:
            raise SyntheticRolloutError("synthetic target snapshot is invalid") from error

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "target_id": self.target_id,
            "synthetic_project_id": self.synthetic_project_id,
            "target_role": self.target_role,
            "current_revision": self.current_revision,
            "before_digest": self.before_digest,
            "candidate_after_digest": self.candidate_after_digest,
        }
        if include_digest:
            value["snapshot_digest"] = self.snapshot_digest
        return value


@dataclass(frozen=True)
class SyntheticTargetBundle:
    """A complete supplied snapshot set; no global target can be inferred."""

    schema_version: int
    bundle_id: str
    data_class: str
    captured_at: str
    expires_at: str
    target_scope: str
    synthetic_project_id: str
    implementation_digest: str
    targets: tuple[SyntheticTargetSnapshot, ...]
    bundle_digest: str = field(default="")

    def __post_init__(self) -> None:
        if (
            type(self) is not SyntheticTargetBundle
            or type(self.schema_version) is not int
            or self.schema_version != A7_TARGET_BUNDLE_VERSION
        ):
            raise SyntheticRolloutError("synthetic target bundle version is invalid")
        _require_prefixed_uuid(self.bundle_id, "a7-target-bundle", "synthetic target bundle")
        if self.data_class != "PUBLIC_SYNTHETIC" or self.target_scope != "synthetic_project_opt_in":
            raise SyntheticRolloutError("synthetic target bundle boundary is invalid")
        _require_timestamp(self.captured_at, "synthetic target bundle timestamp")
        _require_timestamp(self.expires_at, "synthetic target bundle expiry")
        if parse_rfc3339_utc(self.expires_at) <= parse_rfc3339_utc(self.captured_at):
            raise SyntheticRolloutError("synthetic target bundle expiry is invalid")
        _require_project_id(self.synthetic_project_id, "synthetic target bundle project")
        _require_hash(self.implementation_digest, "synthetic target bundle implementation digest")
        if (
            not isinstance(self.targets, tuple)
            or len(self.targets) != MAX_A7_TARGETS
            or any(type(target) is not SyntheticTargetSnapshot for target in self.targets)
            or len({target.target_id for target in self.targets}) != len(self.targets)
            or tuple(sorted(target.target_id for target in self.targets))
            != tuple(target.target_id for target in self.targets)
            or {target.target_role for target in self.targets} != _TARGET_ROLES
            or any(target.synthetic_project_id != self.synthetic_project_id for target in self.targets)
        ):
            raise SyntheticRolloutError("synthetic target bundle targets are invalid")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "bundle_digest")
        if self.bundle_digest and self.bundle_digest != expected:
            raise SyntheticRolloutError("synthetic target bundle digest does not match")
        object.__setattr__(self, "bundle_digest", expected)

    @classmethod
    def from_value(cls, value: object) -> "SyntheticTargetBundle":
        if isinstance(value, cls):
            return value
        expected = {
            "schema_version",
            "bundle_id",
            "data_class",
            "captured_at",
            "expires_at",
            "target_scope",
            "synthetic_project_id",
            "implementation_digest",
            "targets",
            "bundle_digest",
        }
        if type(value) is not dict or set(value) != expected or not isinstance(value["targets"], list):
            raise SyntheticRolloutError("synthetic target bundle has unsupported fields")
        validate_activation_v2_safe_content(value)
        try:
            return cls(
                schema_version=value["schema_version"],
                bundle_id=value["bundle_id"],
                data_class=value["data_class"],
                captured_at=value["captured_at"],
                expires_at=value["expires_at"],
                target_scope=value["target_scope"],
                synthetic_project_id=value["synthetic_project_id"],
                implementation_digest=value["implementation_digest"],
                targets=tuple(SyntheticTargetSnapshot.from_value(item) for item in value["targets"]),
                bundle_digest=value["bundle_digest"],
            )
        except (TypeError, SyntheticRolloutError) as error:
            raise SyntheticRolloutError("synthetic target bundle is invalid") from error

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "bundle_id": self.bundle_id,
            "data_class": self.data_class,
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
            "target_scope": self.target_scope,
            "synthetic_project_id": self.synthetic_project_id,
            "implementation_digest": self.implementation_digest,
            "targets": [target.to_dict() for target in self.targets],
        }
        if include_digest:
            value["bundle_digest"] = self.bundle_digest
        return value


@dataclass(frozen=True)
class A7PacketTarget:
    """A packet binding to one target snapshot, without a filesystem path."""

    target_id: str
    synthetic_project_id: str
    target_role: str
    current_revision: int
    before_digest: str
    candidate_after_digest: str
    snapshot_digest: str

    def __post_init__(self) -> None:
        if type(self) is not A7PacketTarget:
            raise SyntheticRolloutError("A7 packet target is invalid")
        _require_prefixed_uuid(self.target_id, "a7-target", "A7 packet target")
        _require_project_id(self.synthetic_project_id, "A7 packet target project")
        if self.target_role not in _TARGET_ROLES:
            raise SyntheticRolloutError("A7 packet target role is invalid")
        _require_revision(self.current_revision, "A7 packet target revision", minimum=0)
        _require_hash(self.before_digest, "A7 packet target before digest")
        _require_hash(self.candidate_after_digest, "A7 packet target candidate digest")
        _require_hash(self.snapshot_digest, "A7 packet target snapshot digest")
        if self.before_digest == self.candidate_after_digest:
            raise SyntheticRolloutError("A7 packet target candidate does not change state")
        expected = activation_v2_logical_digest(
            {
                "target_id": self.target_id,
                "synthetic_project_id": self.synthetic_project_id,
                "target_role": self.target_role,
                "current_revision": self.current_revision,
                "before_digest": self.before_digest,
                "candidate_after_digest": self.candidate_after_digest,
            },
            "snapshot_digest",
        )
        if self.snapshot_digest != expected:
            raise SyntheticRolloutError("A7 packet target snapshot digest does not match")

    @classmethod
    def from_snapshot(cls, snapshot: SyntheticTargetSnapshot) -> "A7PacketTarget":
        return cls(
            target_id=snapshot.target_id,
            synthetic_project_id=snapshot.synthetic_project_id,
            target_role=snapshot.target_role,
            current_revision=snapshot.current_revision,
            before_digest=snapshot.before_digest,
            candidate_after_digest=snapshot.candidate_after_digest,
            snapshot_digest=snapshot.snapshot_digest,
        )

    @classmethod
    def from_value(cls, value: object) -> "A7PacketTarget":
        expected = {
            "target_id",
            "synthetic_project_id",
            "target_role",
            "current_revision",
            "before_digest",
            "candidate_after_digest",
            "snapshot_digest",
        }
        if type(value) is not dict or set(value) != expected:
            raise SyntheticRolloutError("A7 packet target is invalid")
        try:
            return cls(**value)
        except (TypeError, SyntheticRolloutError) as error:
            raise SyntheticRolloutError("A7 packet target is invalid") from error

    def to_dict(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "synthetic_project_id": self.synthetic_project_id,
            "target_role": self.target_role,
            "current_revision": self.current_revision,
            "before_digest": self.before_digest,
            "candidate_after_digest": self.candidate_after_digest,
            "snapshot_digest": self.snapshot_digest,
        }


@dataclass(frozen=True)
class SyntheticA7ApprovalPacket:
    """A non-authorizing exact packet built solely from a supplied fixture bundle."""

    schema_version: int
    packet_id: str
    snapshot_bundle_id: str
    snapshot_bundle_digest: str
    synthetic_project_id: str
    implementation_digest: str
    expires_at: str
    policy_digest: str
    targets: tuple[A7PacketTarget, ...]
    backup_required: bool
    canary_required: bool
    readback_required: bool
    rollback_required: bool
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
            type(self) is not SyntheticA7ApprovalPacket
            or type(self.schema_version) is not int
            or self.schema_version != A7_PACKET_VERSION
        ):
            raise SyntheticRolloutError("A7 packet version is invalid")
        _require_prefixed_uuid(self.packet_id, "a7-packet", "A7 packet")
        _require_prefixed_uuid(self.snapshot_bundle_id, "a7-target-bundle", "A7 snapshot bundle")
        _require_hash(self.snapshot_bundle_digest, "A7 snapshot bundle digest")
        _require_project_id(self.synthetic_project_id, "A7 packet project")
        _require_hash(self.implementation_digest, "A7 packet implementation digest")
        _require_timestamp(self.expires_at, "A7 packet expiry")
        _require_hash(self.policy_digest, "A7 policy digest")
        if (
            not isinstance(self.targets, tuple)
            or len(self.targets) != MAX_A7_TARGETS
            or any(type(target) is not A7PacketTarget for target in self.targets)
            or len({target.target_id for target in self.targets}) != len(self.targets)
            or tuple(target.target_id for target in self.targets) != tuple(sorted(target.target_id for target in self.targets))
            or {target.target_role for target in self.targets} != _TARGET_ROLES
            or any(target.synthetic_project_id != self.synthetic_project_id for target in self.targets)
        ):
            raise SyntheticRolloutError("A7 packet targets are invalid")
        if (
            self.backup_required is not True
            or self.canary_required is not True
            or self.readback_required is not True
            or self.rollback_required is not True
            or self.canary_sequence != _CANARY_SEQUENCE
            or self.permission_impact != "NONE_SYNTHETIC"
            or self.rollback_trigger_codes != _ROLLBACK_TRIGGER_CODES
            or self.network_permitted is not False
            or self.global_target_access is not False
            or self.global_mutation_authorized is not False
            or self.requires_current_user_approval is not True
            or self.approval_state != "SYNTHETIC_DRAFT_NOT_APPROVED"
            or self.fixture_only is not True
        ):
            raise SyntheticRolloutError("A7 packet exceeds its synthetic preparation boundary")
        _require_timestamp(self.created_at, "A7 packet timestamp")
        if parse_rfc3339_utc(self.created_at) > parse_rfc3339_utc(self.expires_at):
            raise SyntheticRolloutError("A7 packet expiry is invalid")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "packet_digest")
        if self.packet_digest and self.packet_digest != expected:
            raise SyntheticRolloutError("A7 packet digest does not match")
        object.__setattr__(self, "packet_digest", expected)

    @classmethod
    def from_value(cls, value: object) -> "SyntheticA7ApprovalPacket":
        if isinstance(value, cls):
            return value
        expected = {
            "schema_version",
            "packet_id",
            "snapshot_bundle_id",
            "snapshot_bundle_digest",
            "synthetic_project_id",
            "implementation_digest",
            "expires_at",
            "policy_digest",
            "targets",
            "backup_required",
            "canary_required",
            "readback_required",
            "rollback_required",
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
            or not isinstance(value["targets"], list)
            or not isinstance(value["canary_sequence"], list)
            or not isinstance(value["rollback_trigger_codes"], list)
        ):
            raise SyntheticRolloutError("A7 packet has unsupported fields")
        try:
            return cls(
                schema_version=value["schema_version"],
                packet_id=value["packet_id"],
                snapshot_bundle_id=value["snapshot_bundle_id"],
                snapshot_bundle_digest=value["snapshot_bundle_digest"],
                synthetic_project_id=value["synthetic_project_id"],
                implementation_digest=value["implementation_digest"],
                expires_at=value["expires_at"],
                policy_digest=value["policy_digest"],
                targets=tuple(A7PacketTarget.from_value(item) for item in value["targets"]),
                backup_required=value["backup_required"],
                canary_required=value["canary_required"],
                readback_required=value["readback_required"],
                rollback_required=value["rollback_required"],
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
        except (TypeError, SyntheticRolloutError) as error:
            raise SyntheticRolloutError("A7 packet is invalid") from error

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "packet_id": self.packet_id,
            "snapshot_bundle_id": self.snapshot_bundle_id,
            "snapshot_bundle_digest": self.snapshot_bundle_digest,
            "synthetic_project_id": self.synthetic_project_id,
            "implementation_digest": self.implementation_digest,
            "expires_at": self.expires_at,
            "policy_digest": self.policy_digest,
            "targets": [target.to_dict() for target in self.targets],
            "backup_required": self.backup_required,
            "canary_required": self.canary_required,
            "readback_required": self.readback_required,
            "rollback_required": self.rollback_required,
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
class SyntheticRolloutTargetState:
    """Current synthetic digest/revision for one opaque packet target."""

    target_id: str
    synthetic_project_id: str
    content_digest: str
    target_revision: int

    def __post_init__(self) -> None:
        if type(self) is not SyntheticRolloutTargetState:
            raise SyntheticRolloutError("synthetic rollout target state is invalid")
        _require_prefixed_uuid(self.target_id, "a7-target", "synthetic rollout target")
        _require_project_id(self.synthetic_project_id, "synthetic rollout target project")
        _require_hash(self.content_digest, "synthetic rollout content digest")
        _require_revision(self.target_revision, "synthetic rollout target revision", minimum=0)

    @classmethod
    def from_value(cls, value: object) -> "SyntheticRolloutTargetState":
        expected = {"target_id", "synthetic_project_id", "content_digest", "target_revision"}
        if type(value) is not dict or set(value) != expected:
            raise SyntheticRolloutError("synthetic rollout target state is invalid")
        try:
            return cls(**value)
        except (TypeError, SyntheticRolloutError) as error:
            raise SyntheticRolloutError("synthetic rollout target state is invalid") from error

    def to_dict(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "synthetic_project_id": self.synthetic_project_id,
            "content_digest": self.content_digest,
            "target_revision": self.target_revision,
        }


@dataclass(frozen=True)
class SyntheticRolloutState:
    """CAS-protected disposable state; it is never a global configuration."""

    schema_version: int
    packet_id: str
    packet_digest: str
    synthetic_project_id: str
    state_revision: int
    targets: tuple[SyntheticRolloutTargetState, ...]
    state_digest: str = field(default="")

    def __post_init__(self) -> None:
        if (
            type(self) is not SyntheticRolloutState
            or type(self.schema_version) is not int
            or self.schema_version != A7_STATE_VERSION
        ):
            raise SyntheticRolloutError("synthetic rollout state version is invalid")
        _require_prefixed_uuid(self.packet_id, "a7-packet", "synthetic rollout packet")
        _require_hash(self.packet_digest, "synthetic rollout packet digest")
        _require_project_id(self.synthetic_project_id, "synthetic rollout project")
        _require_revision(self.state_revision, "synthetic rollout state revision", minimum=0)
        if (
            not isinstance(self.targets, tuple)
            or len(self.targets) != MAX_A7_TARGETS
            or any(type(target) is not SyntheticRolloutTargetState for target in self.targets)
            or len({target.target_id for target in self.targets}) != len(self.targets)
            or tuple(target.target_id for target in self.targets) != tuple(sorted(target.target_id for target in self.targets))
            or any(target.synthetic_project_id != self.synthetic_project_id for target in self.targets)
        ):
            raise SyntheticRolloutError("synthetic rollout state targets are invalid")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "state_digest")
        if self.state_digest and self.state_digest != expected:
            raise SyntheticRolloutError("synthetic rollout state digest does not match")
        object.__setattr__(self, "state_digest", expected)

    @classmethod
    def initial(cls, packet: SyntheticA7ApprovalPacket) -> "SyntheticRolloutState":
        return cls(
            schema_version=A7_STATE_VERSION,
            packet_id=packet.packet_id,
            packet_digest=packet.packet_digest,
            synthetic_project_id=packet.synthetic_project_id,
            state_revision=0,
            targets=tuple(
                SyntheticRolloutTargetState(
                    target.target_id,
                    target.synthetic_project_id,
                    target.before_digest,
                    target.current_revision,
                )
                for target in packet.targets
            ),
        )

    @classmethod
    def from_value(cls, value: object) -> "SyntheticRolloutState":
        expected = {
            "schema_version",
            "packet_id",
            "packet_digest",
            "synthetic_project_id",
            "state_revision",
            "targets",
            "state_digest",
        }
        if type(value) is not dict or set(value) != expected or not isinstance(value["targets"], list):
            raise SyntheticRolloutError("synthetic rollout state has unsupported fields")
        try:
            return cls(
                schema_version=value["schema_version"],
                packet_id=value["packet_id"],
                packet_digest=value["packet_digest"],
                synthetic_project_id=value["synthetic_project_id"],
                state_revision=value["state_revision"],
                targets=tuple(SyntheticRolloutTargetState.from_value(item) for item in value["targets"]),
                state_digest=value["state_digest"],
            )
        except (TypeError, SyntheticRolloutError) as error:
            raise SyntheticRolloutError("synthetic rollout state is invalid") from error

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "packet_id": self.packet_id,
            "packet_digest": self.packet_digest,
            "synthetic_project_id": self.synthetic_project_id,
            "state_revision": self.state_revision,
            "targets": [target.to_dict() for target in self.targets],
        }
        if include_digest:
            value["state_digest"] = self.state_digest
        return value


@dataclass(frozen=True)
class SyntheticRolloutReceipt:
    """A digest-only A7 backup/canary/readback/rollback receipt."""

    schema_version: int
    receipt_id: str
    operation: str
    status: str
    packet_id: str
    packet_digest: str
    synthetic_project_id: str
    backup_id: str
    backup_digest: str
    expected_state_revision: int
    previous_state_revision: int
    state_revision: int
    target_states: tuple[SyntheticRolloutTargetState, ...]
    fixture_only: bool
    network_calls: int
    global_target_access: bool
    global_mutation: bool
    authority_write: bool
    created_at: str
    receipt_digest: str = field(default="")

    def __post_init__(self) -> None:
        if (
            type(self) is not SyntheticRolloutReceipt
            or type(self.schema_version) is not int
            or self.schema_version != A7_RECEIPT_VERSION
        ):
            raise SyntheticRolloutError("synthetic rollout receipt version is invalid")
        _require_prefixed_uuid(self.receipt_id, "a7-rollout-receipt", "synthetic rollout receipt")
        if self.operation not in _OPERATIONS or self.status != _STATUS_BY_OPERATION.get(self.operation):
            raise SyntheticRolloutError("synthetic rollout receipt operation is invalid")
        _require_prefixed_uuid(self.packet_id, "a7-packet", "synthetic rollout packet")
        _require_hash(self.packet_digest, "synthetic rollout packet digest")
        _require_project_id(self.synthetic_project_id, "synthetic rollout project")
        _require_prefixed_uuid(self.backup_id, "a7-backup", "synthetic rollout backup")
        _require_hash(self.backup_digest, "synthetic rollout backup digest")
        _require_revision(self.expected_state_revision, "synthetic rollout expected revision", minimum=0)
        _require_revision(self.previous_state_revision, "synthetic rollout previous revision", minimum=0)
        _require_revision(self.state_revision, "synthetic rollout state revision", minimum=0)
        if (
            not isinstance(self.target_states, tuple)
            or len(self.target_states) != MAX_A7_TARGETS
            or any(type(target) is not SyntheticRolloutTargetState for target in self.target_states)
            or len({target.target_id for target in self.target_states}) != len(self.target_states)
            or tuple(target.target_id for target in self.target_states) != tuple(sorted(target.target_id for target in self.target_states))
            or any(target.synthetic_project_id != self.synthetic_project_id for target in self.target_states)
        ):
            raise SyntheticRolloutError("synthetic rollout receipt target states are invalid")
        if self.operation in {"backup", "readback"} and not (
            self.expected_state_revision == self.previous_state_revision == self.state_revision
        ):
            raise SyntheticRolloutError("synthetic rollout receipt revision chain is invalid")
        if self.operation in {"canary", "rollback"} and not (
            self.expected_state_revision == self.previous_state_revision
            and self.state_revision == self.previous_state_revision + 1
        ):
            raise SyntheticRolloutError("synthetic rollout receipt revision chain is invalid")
        if (
            self.fixture_only is not True
            or type(self.network_calls) is not int
            or self.network_calls != 0
            or self.global_target_access is not False
            or self.global_mutation is not False
            or self.authority_write is not False
        ):
            raise SyntheticRolloutError("synthetic rollout receipt exceeds its local-only boundary")
        _require_timestamp(self.created_at, "synthetic rollout receipt timestamp")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "receipt_digest")
        if self.receipt_digest and self.receipt_digest != expected:
            raise SyntheticRolloutError("synthetic rollout receipt digest does not match")
        object.__setattr__(self, "receipt_digest", expected)

    @classmethod
    def from_value(cls, value: object) -> "SyntheticRolloutReceipt":
        if isinstance(value, cls):
            return value
        expected = {
            "schema_version",
            "receipt_id",
            "operation",
            "status",
            "packet_id",
            "packet_digest",
            "synthetic_project_id",
            "backup_id",
            "backup_digest",
            "expected_state_revision",
            "previous_state_revision",
            "state_revision",
            "target_states",
            "fixture_only",
            "network_calls",
            "global_target_access",
            "global_mutation",
            "authority_write",
            "created_at",
            "receipt_digest",
        }
        if type(value) is not dict or set(value) != expected or not isinstance(value["target_states"], list):
            raise SyntheticRolloutError("synthetic rollout receipt has unsupported fields")
        try:
            return cls(
                schema_version=value["schema_version"],
                receipt_id=value["receipt_id"],
                operation=value["operation"],
                status=value["status"],
                packet_id=value["packet_id"],
                packet_digest=value["packet_digest"],
                synthetic_project_id=value["synthetic_project_id"],
                backup_id=value["backup_id"],
                backup_digest=value["backup_digest"],
                expected_state_revision=value["expected_state_revision"],
                previous_state_revision=value["previous_state_revision"],
                state_revision=value["state_revision"],
                target_states=tuple(SyntheticRolloutTargetState.from_value(item) for item in value["target_states"]),
                fixture_only=value["fixture_only"],
                network_calls=value["network_calls"],
                global_target_access=value["global_target_access"],
                global_mutation=value["global_mutation"],
                authority_write=value["authority_write"],
                created_at=value["created_at"],
                receipt_digest=value["receipt_digest"],
            )
        except (TypeError, SyntheticRolloutError) as error:
            raise SyntheticRolloutError("synthetic rollout receipt is invalid") from error

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "operation": self.operation,
            "status": self.status,
            "packet_id": self.packet_id,
            "packet_digest": self.packet_digest,
            "synthetic_project_id": self.synthetic_project_id,
            "backup_id": self.backup_id,
            "backup_digest": self.backup_digest,
            "expected_state_revision": self.expected_state_revision,
            "previous_state_revision": self.previous_state_revision,
            "state_revision": self.state_revision,
            "target_states": [target.to_dict() for target in self.target_states],
            "fixture_only": self.fixture_only,
            "network_calls": self.network_calls,
            "global_target_access": self.global_target_access,
            "global_mutation": self.global_mutation,
            "authority_write": self.authority_write,
            "created_at": self.created_at,
        }
        if include_digest:
            value["receipt_digest"] = self.receipt_digest
        return value


@dataclass(frozen=True)
class SyntheticA7PendingOperation:
    """A durable local journal for one state-before-receipt transition."""

    schema_version: int
    journal_id: str
    operation: str
    packet_id: str
    packet_digest: str
    synthetic_project_id: str
    previous_state: SyntheticRolloutState
    next_state: SyntheticRolloutState
    receipt: SyntheticRolloutReceipt
    journal_digest: str = field(default="")

    def __post_init__(self) -> None:
        if (
            type(self) is not SyntheticA7PendingOperation
            or type(self.schema_version) is not int
            or self.schema_version != A7_PENDING_OPERATION_VERSION
        ):
            raise SyntheticRolloutError("synthetic A7 pending operation version is invalid")
        _require_prefixed_uuid(self.journal_id, "a7-journal", "synthetic A7 pending operation")
        if self.operation not in {"canary", "rollback"}:
            raise SyntheticRolloutError("synthetic A7 pending operation is invalid")
        _require_prefixed_uuid(self.packet_id, "a7-packet", "synthetic A7 pending packet")
        _require_hash(self.packet_digest, "synthetic A7 pending packet digest")
        _require_project_id(self.synthetic_project_id, "synthetic A7 pending project")
        if (
            type(self.previous_state) is not SyntheticRolloutState
            or type(self.next_state) is not SyntheticRolloutState
            or type(self.receipt) is not SyntheticRolloutReceipt
            or self.previous_state.packet_id != self.packet_id
            or self.next_state.packet_id != self.packet_id
            or self.receipt.packet_id != self.packet_id
            or self.previous_state.packet_digest != self.packet_digest
            or self.next_state.packet_digest != self.packet_digest
            or self.receipt.packet_digest != self.packet_digest
            or self.previous_state.synthetic_project_id != self.synthetic_project_id
            or self.next_state.synthetic_project_id != self.synthetic_project_id
            or self.receipt.synthetic_project_id != self.synthetic_project_id
            or self.receipt.operation != self.operation
            or self.next_state.state_revision != self.previous_state.state_revision + 1
            or self.receipt.previous_state_revision != self.previous_state.state_revision
            or self.receipt.state_revision != self.next_state.state_revision
            or self.receipt.target_states != self.next_state.targets
        ):
            raise SyntheticRolloutError("synthetic A7 pending operation binding is invalid")
        expected = activation_v2_logical_digest(self.to_dict(include_digest=False), "journal_digest")
        if self.journal_digest and self.journal_digest != expected:
            raise SyntheticRolloutError("synthetic A7 pending operation digest does not match")
        object.__setattr__(self, "journal_digest", expected)

    @classmethod
    def from_value(cls, value: object) -> "SyntheticA7PendingOperation":
        if isinstance(value, cls):
            return value
        expected = {
            "schema_version",
            "journal_id",
            "operation",
            "packet_id",
            "packet_digest",
            "synthetic_project_id",
            "previous_state",
            "next_state",
            "receipt",
            "journal_digest",
        }
        if type(value) is not dict or set(value) != expected:
            raise SyntheticRolloutError("synthetic A7 pending operation is invalid")
        try:
            return cls(
                schema_version=value["schema_version"],
                journal_id=value["journal_id"],
                operation=value["operation"],
                packet_id=value["packet_id"],
                packet_digest=value["packet_digest"],
                synthetic_project_id=value["synthetic_project_id"],
                previous_state=SyntheticRolloutState.from_value(value["previous_state"]),
                next_state=SyntheticRolloutState.from_value(value["next_state"]),
                receipt=SyntheticRolloutReceipt.from_value(value["receipt"]),
                journal_digest=value["journal_digest"],
            )
        except (TypeError, SyntheticRolloutError) as error:
            raise SyntheticRolloutError("synthetic A7 pending operation is invalid") from error

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "journal_id": self.journal_id,
            "operation": self.operation,
            "packet_id": self.packet_id,
            "packet_digest": self.packet_digest,
            "synthetic_project_id": self.synthetic_project_id,
            "previous_state": self.previous_state.to_dict(),
            "next_state": self.next_state.to_dict(),
            "receipt": self.receipt.to_dict(),
        }
        if include_digest:
            value["journal_digest"] = self.journal_digest
        return value


def prepare_synthetic_a7_packet(
    policy: PolicyInputs | Mapping[str, Any],
    target_bundle: SyntheticTargetBundle | Mapping[str, Any],
    *,
    packet_id: str | None = None,
    as_of: str,
) -> SyntheticA7ApprovalPacket:
    """Build a draft packet only from exact supplied synthetic snapshots."""

    selected_policy = PolicyInputs.from_value(policy)
    selected_policy.require_resolved("A7")
    if not selected_policy.fixture_only:
        raise SyntheticRolloutError("A7 packet preparation requires a fixture-only policy")
    bundle = _coerce_target_bundle(target_bundle)
    _require_fresh_bundle(bundle, as_of)
    if bundle.implementation_digest != synthetic_a7_implementation_digest():
        raise SyntheticRolloutError("SOURCE_DRIFT")
    packet = SyntheticA7ApprovalPacket(
        schema_version=A7_PACKET_VERSION,
        packet_id=packet_id or f"a7-packet:{uuid.uuid4()}",
        snapshot_bundle_id=bundle.bundle_id,
        snapshot_bundle_digest=bundle.bundle_digest,
        synthetic_project_id=bundle.synthetic_project_id,
        implementation_digest=bundle.implementation_digest,
        expires_at=bundle.expires_at,
        policy_digest=selected_policy.policy_digest,
        targets=tuple(A7PacketTarget.from_snapshot(target) for target in bundle.targets),
        backup_required=True,
        canary_required=True,
        readback_required=True,
        rollback_required=True,
        canary_sequence=_CANARY_SEQUENCE,
        permission_impact="NONE_SYNTHETIC",
        rollback_trigger_codes=_ROLLBACK_TRIGGER_CODES,
        network_permitted=False,
        global_target_access=False,
        global_mutation_authorized=False,
        requires_current_user_approval=True,
        approval_state="SYNTHETIC_DRAFT_NOT_APPROVED",
        fixture_only=True,
        created_at=as_of,
    )
    validate_named_document("activation-v2-a7-approval-packet-v1", packet.to_dict())
    return packet


def initialize_synthetic_a7_rollout(
    runtime: DisposableRuntime,
    packet: SyntheticA7ApprovalPacket | Mapping[str, Any],
    target_bundle: SyntheticTargetBundle | Mapping[str, Any],
    *,
    as_of: str,
) -> SyntheticRolloutState:
    """Initialize only an ignored synthetic digest state for a prepared packet."""

    handle = _require_fixture_a7_runtime(runtime)
    selected = _coerce_packet(packet)
    bundle = _coerce_target_bundle(target_bundle)
    with exclusive_disposable_runtime_lock(handle) as locked:
        _require_packet_context(locked, selected, bundle, as_of)
        _recover_pending_operation(locked, selected)
        relative = _state_path(selected.packet_id)
        if disposable_json_exists(locked, relative):
            raise SyntheticRolloutError("synthetic A7 rollout state already exists")
        state = SyntheticRolloutState.initial(selected)
        _write_state(locked, state)
        return state


def create_synthetic_a7_backup(
    runtime: DisposableRuntime,
    packet: SyntheticA7ApprovalPacket | Mapping[str, Any],
    target_bundle: SyntheticTargetBundle | Mapping[str, Any],
    *,
    as_of: str,
    expected_state_revision: int,
    backup_id: str | None = None,
    receipt_id: str | None = None,
    created_at: str | None = None,
) -> SyntheticRolloutReceipt:
    """Record an exact digest-only backup before the synthetic canary transition."""

    handle = _require_fixture_a7_runtime(runtime)
    selected = _coerce_packet(packet)
    bundle = _coerce_target_bundle(target_bundle)
    with exclusive_disposable_runtime_lock(handle) as locked:
        _require_packet_context(locked, selected, bundle, as_of)
        _recover_pending_operation(locked, selected)
        state = _load_state_unrecovered(locked, selected)
        _require_exact_revision(state, expected_state_revision, "synthetic A7 backup")
        _require_state_matches_packet(state, selected, expected="before")
        chosen_backup_id = backup_id or f"a7-backup:{uuid.uuid4()}"
        _require_prefixed_uuid(chosen_backup_id, "a7-backup", "synthetic A7 backup")
        backup_digest = _backup_digest(selected, state.targets)
        receipt = _receipt(
            receipt_id=receipt_id or f"a7-rollout-receipt:{uuid.uuid4()}",
            operation="backup",
            packet=selected,
            backup_id=chosen_backup_id,
            backup_digest=backup_digest,
            expected_state_revision=expected_state_revision,
            previous_state_revision=state.state_revision,
            state_revision=state.state_revision,
            target_states=state.targets,
            created_at=created_at or as_of,
        )
        _write_receipt(locked, receipt)
        return receipt


def run_synthetic_a7_canary(
    runtime: DisposableRuntime,
    packet: SyntheticA7ApprovalPacket | Mapping[str, Any],
    target_bundle: SyntheticTargetBundle | Mapping[str, Any],
    backup: SyntheticRolloutReceipt | Mapping[str, Any],
    *,
    as_of: str,
    expected_state_revision: int,
    receipt_id: str | None = None,
    created_at: str | None = None,
) -> SyntheticRolloutReceipt:
    """Apply a candidate digest only to disposable state under exact CAS."""

    handle = _require_fixture_a7_runtime(runtime)
    selected = _coerce_packet(packet)
    bundle = _coerce_target_bundle(target_bundle)
    with exclusive_disposable_runtime_lock(handle) as locked:
        _require_packet_context(locked, selected, bundle, as_of)
        _recover_pending_operation(locked, selected)
        selected_backup = _require_backup_for_packet(locked, backup, selected)
        state = _load_state_unrecovered(locked, selected)
        _require_exact_revision(state, expected_state_revision, "synthetic A7 canary")
        _require_state_matches_receipt(state, selected_backup)
        next_state = SyntheticRolloutState(
            schema_version=A7_STATE_VERSION,
            packet_id=state.packet_id,
            packet_digest=state.packet_digest,
            synthetic_project_id=state.synthetic_project_id,
            state_revision=state.state_revision + 1,
            targets=tuple(
                SyntheticRolloutTargetState(
                    target.target_id,
                    target.synthetic_project_id,
                    target.candidate_after_digest,
                    current.target_revision + 1,
                )
                for target, current in zip(selected.targets, state.targets, strict=True)
            ),
        )
        receipt = _receipt(
            receipt_id=receipt_id or f"a7-rollout-receipt:{uuid.uuid4()}",
            operation="canary",
            packet=selected,
            backup_id=selected_backup.backup_id,
            backup_digest=selected_backup.backup_digest,
            expected_state_revision=expected_state_revision,
            previous_state_revision=state.state_revision,
            state_revision=next_state.state_revision,
            target_states=next_state.targets,
            created_at=created_at or as_of,
        )
        _write_state_then_receipt(locked, state, next_state, receipt)
        return receipt


def readback_synthetic_a7_rollout(
    runtime: DisposableRuntime,
    packet: SyntheticA7ApprovalPacket | Mapping[str, Any],
    target_bundle: SyntheticTargetBundle | Mapping[str, Any],
    backup: SyntheticRolloutReceipt | Mapping[str, Any],
    canary: SyntheticRolloutReceipt | Mapping[str, Any],
    *,
    as_of: str,
    expected_state_revision: int,
    receipt_id: str | None = None,
    created_at: str | None = None,
) -> SyntheticRolloutReceipt:
    """Verify the candidate digest state without executing a real readback."""

    handle = _require_fixture_a7_runtime(runtime)
    selected = _coerce_packet(packet)
    bundle = _coerce_target_bundle(target_bundle)
    with exclusive_disposable_runtime_lock(handle) as locked:
        _require_packet_context(locked, selected, bundle, as_of)
        _recover_pending_operation(locked, selected)
        selected_backup = _require_backup_for_packet(locked, backup, selected)
        state = _load_state_unrecovered(locked, selected)
        _require_exact_revision(state, expected_state_revision, "synthetic A7 readback")
        _require_prior_receipt(locked, canary, selected, selected_backup, "canary", state)
        _require_state_matches_packet(state, selected, expected="candidate")
        receipt = _receipt(
            receipt_id=receipt_id or f"a7-rollout-receipt:{uuid.uuid4()}",
            operation="readback",
            packet=selected,
            backup_id=selected_backup.backup_id,
            backup_digest=selected_backup.backup_digest,
            expected_state_revision=expected_state_revision,
            previous_state_revision=state.state_revision,
            state_revision=state.state_revision,
            target_states=state.targets,
            created_at=created_at or as_of,
        )
        _write_receipt(locked, receipt)
        return receipt


def rollback_synthetic_a7_rollout(
    runtime: DisposableRuntime,
    packet: SyntheticA7ApprovalPacket | Mapping[str, Any],
    target_bundle: SyntheticTargetBundle | Mapping[str, Any],
    backup: SyntheticRolloutReceipt | Mapping[str, Any],
    readback: SyntheticRolloutReceipt | Mapping[str, Any],
    *,
    as_of: str,
    expected_state_revision: int,
    receipt_id: str | None = None,
    created_at: str | None = None,
) -> SyntheticRolloutReceipt:
    """Restore only the backup digests in disposable state under exact CAS."""

    handle = _require_fixture_a7_runtime(runtime)
    selected = _coerce_packet(packet)
    bundle = _coerce_target_bundle(target_bundle)
    with exclusive_disposable_runtime_lock(handle) as locked:
        _require_packet_context(locked, selected, bundle, as_of)
        _recover_pending_operation(locked, selected)
        selected_backup = _require_backup_for_packet(locked, backup, selected)
        state = _load_state_unrecovered(locked, selected)
        _require_exact_revision(state, expected_state_revision, "synthetic A7 rollback")
        _require_prior_receipt(locked, readback, selected, selected_backup, "readback", state)
        _require_state_matches_packet(state, selected, expected="candidate")
        next_state = SyntheticRolloutState(
            schema_version=A7_STATE_VERSION,
            packet_id=state.packet_id,
            packet_digest=state.packet_digest,
            synthetic_project_id=state.synthetic_project_id,
            state_revision=state.state_revision + 1,
            targets=tuple(
                SyntheticRolloutTargetState(
                    before.target_id,
                    before.synthetic_project_id,
                    before.content_digest,
                    current.target_revision + 1,
                )
                for before, current in zip(selected_backup.target_states, state.targets, strict=True)
            ),
        )
        receipt = _receipt(
            receipt_id=receipt_id or f"a7-rollout-receipt:{uuid.uuid4()}",
            operation="rollback",
            packet=selected,
            backup_id=selected_backup.backup_id,
            backup_digest=selected_backup.backup_digest,
            expected_state_revision=expected_state_revision,
            previous_state_revision=state.state_revision,
            state_revision=next_state.state_revision,
            target_states=next_state.targets,
            created_at=created_at or as_of,
        )
        _write_state_then_receipt(locked, state, next_state, receipt)
        return receipt


def load_synthetic_a7_rollout_state(
    runtime: DisposableRuntime,
    packet: SyntheticA7ApprovalPacket | Mapping[str, Any],
    target_bundle: SyntheticTargetBundle | Mapping[str, Any],
    *,
    as_of: str,
) -> SyntheticRolloutState:
    """Load a validated state without opening or discovering a global target."""

    handle = _require_fixture_a7_runtime(runtime)
    selected = _coerce_packet(packet)
    bundle = _coerce_target_bundle(target_bundle)
    with exclusive_disposable_runtime_lock(handle) as locked:
        _require_packet_context(locked, selected, bundle, as_of)
        _recover_pending_operation(locked, selected)
        return _load_state_unrecovered(locked, selected)


def load_synthetic_a7_receipt(
    runtime: DisposableRuntime,
    packet: SyntheticA7ApprovalPacket | Mapping[str, Any],
    target_bundle: SyntheticTargetBundle | Mapping[str, Any],
    receipt_id: str,
    *,
    as_of: str,
) -> SyntheticRolloutReceipt:
    """Load one digest-only synthetic receipt under the A7 policy boundary."""

    handle = _require_fixture_a7_runtime(runtime)
    selected = _coerce_packet(packet)
    bundle = _coerce_target_bundle(target_bundle)
    _require_prefixed_uuid(receipt_id, "a7-rollout-receipt", "synthetic rollout receipt")
    with exclusive_disposable_runtime_lock(handle) as locked:
        _require_packet_context(locked, selected, bundle, as_of)
        _recover_pending_operation(locked, selected)
        return _load_receipt_unrecovered(locked, receipt_id, selected)


def _require_fixture_a7_runtime(runtime: DisposableRuntime) -> DisposableRuntime:
    handle = load_disposable_runtime(runtime.root)
    handle.manifest.policy.require_resolved("A7")
    if not handle.manifest.policy.fixture_only:
        raise SyntheticRolloutError("A7 requires a fixture-only disposable runtime")
    return handle


def _coerce_packet(value: SyntheticA7ApprovalPacket | Mapping[str, Any]) -> SyntheticA7ApprovalPacket:
    if isinstance(value, SyntheticA7ApprovalPacket):
        return value
    if type(value) is not dict:
        raise SyntheticRolloutError("A7 packet is invalid")
    validate_named_document("activation-v2-a7-approval-packet-v1", value)
    return SyntheticA7ApprovalPacket.from_value(value)


def _coerce_target_bundle(value: SyntheticTargetBundle | Mapping[str, Any]) -> SyntheticTargetBundle:
    if isinstance(value, SyntheticTargetBundle):
        return value
    if type(value) is not dict:
        raise SyntheticRolloutError("synthetic target bundle is invalid")
    validate_named_document("activation-v2-a7-target-bundle-v1", value)
    return SyntheticTargetBundle.from_value(value)


def _require_fresh_bundle(bundle: SyntheticTargetBundle, as_of: object) -> str:
    timestamp = _require_timestamp(as_of, "synthetic A7 as_of")
    selected = parse_rfc3339_utc(timestamp)
    if selected < parse_rfc3339_utc(bundle.captured_at) or selected > parse_rfc3339_utc(bundle.expires_at):
        raise SyntheticRolloutError("synthetic A7 packet is stale")
    return timestamp


def _require_packet_context(
    runtime: DisposableRuntime,
    packet: SyntheticA7ApprovalPacket,
    bundle: SyntheticTargetBundle,
    as_of: object,
) -> str:
    """Bind every transition to one exact bundle, policy, project, and source."""

    timestamp = _require_fresh_bundle(bundle, as_of)
    if parse_rfc3339_utc(timestamp) < parse_rfc3339_utc(packet.created_at):
        raise SyntheticRolloutError("synthetic A7 packet is not yet valid")
    if (
        runtime.manifest.policy.policy_digest != packet.policy_digest
        or packet.snapshot_bundle_id != bundle.bundle_id
        or packet.snapshot_bundle_digest != bundle.bundle_digest
        or packet.synthetic_project_id != bundle.synthetic_project_id
        or packet.implementation_digest != bundle.implementation_digest
        or packet.expires_at != bundle.expires_at
        or packet.targets != tuple(A7PacketTarget.from_snapshot(target) for target in bundle.targets)
    ):
        raise SyntheticRolloutError("synthetic A7 packet context does not match the supplied target bundle")
    if packet.implementation_digest != synthetic_a7_implementation_digest():
        raise SyntheticRolloutError("SOURCE_DRIFT")
    return timestamp


def _load_state_unrecovered(runtime: DisposableRuntime, packet: SyntheticA7ApprovalPacket) -> SyntheticRolloutState:
    relative = _state_path(packet.packet_id)
    try:
        state = SyntheticRolloutState.from_value(read_disposable_json(runtime, relative))
        validate_named_document("activation-v2-a7-rollout-state-v1", state.to_dict())
    except (IntegrityError, ContractError) as error:
        raise IntegrityError("synthetic A7 rollout state is unavailable or invalid") from error
    if (
        state.packet_id != packet.packet_id
        or state.packet_digest != packet.packet_digest
        or state.synthetic_project_id != packet.synthetic_project_id
    ):
        raise IntegrityError("synthetic A7 rollout state is bound to another packet")
    if (
        tuple(target.target_id for target in state.targets) != tuple(target.target_id for target in packet.targets)
        or any(target.synthetic_project_id != packet.synthetic_project_id for target in state.targets)
    ):
        raise IntegrityError("synthetic A7 rollout state target set is invalid")
    return state


def _load_receipt_unrecovered(
    runtime: DisposableRuntime,
    receipt_id: str,
    packet: SyntheticA7ApprovalPacket,
) -> SyntheticRolloutReceipt:
    try:
        document = read_disposable_json(runtime, _receipt_path(receipt_id))
        validate_named_document("activation-v2-a7-rollout-receipt-v1", document)
        receipt = SyntheticRolloutReceipt.from_value(document)
    except (IntegrityError, ContractError, SyntheticRolloutError) as error:
        raise IntegrityError("synthetic A7 rollout receipt is unavailable or invalid") from error
    if (
        receipt.packet_id != packet.packet_id
        or receipt.packet_digest != packet.packet_digest
        or receipt.synthetic_project_id != packet.synthetic_project_id
    ):
        raise IntegrityError("synthetic A7 rollout receipt is bound to another packet")
    return receipt


def _recover_pending_operation(runtime: DisposableRuntime, packet: SyntheticA7ApprovalPacket) -> None:
    """Recover a crash between the atomic state and receipt writes under lock."""

    relative = _journal_path(packet.packet_id)
    if not disposable_json_exists(runtime, relative):
        return
    try:
        document = read_disposable_json(runtime, relative)
        validate_named_document("activation-v2-a7-pending-operation-v1", document)
        pending = SyntheticA7PendingOperation.from_value(document)
    except (IntegrityError, ContractError, SyntheticRolloutError) as error:
        raise IntegrityError("synthetic A7 pending operation is unavailable or invalid") from error
    if (
        pending.packet_id != packet.packet_id
        or pending.packet_digest != packet.packet_digest
        or pending.synthetic_project_id != packet.synthetic_project_id
    ):
        raise IntegrityError("synthetic A7 pending operation is bound to another packet")
    state = _load_state_unrecovered(runtime, packet)
    receipt_path = _receipt_path(pending.receipt.receipt_id)
    persisted_receipt: SyntheticRolloutReceipt | None
    if disposable_json_exists(runtime, receipt_path):
        persisted_receipt = _load_receipt_unrecovered(runtime, pending.receipt.receipt_id, packet)
    else:
        persisted_receipt = None
    if state == pending.previous_state and persisted_receipt is None:
        remove_disposable_json(runtime, relative)
        return
    if state == pending.next_state and persisted_receipt == pending.receipt:
        remove_disposable_json(runtime, relative)
        return
    if state == pending.next_state and persisted_receipt is None:
        _write_state(runtime, pending.previous_state)
        remove_disposable_json(runtime, relative)
        return
    raise IntegrityError("synthetic A7 pending operation recovery is inconsistent")


def _require_backup_for_packet(
    runtime: DisposableRuntime,
    backup: SyntheticRolloutReceipt | Mapping[str, Any],
    packet: SyntheticA7ApprovalPacket,
) -> SyntheticRolloutReceipt:
    selected = _coerce_receipt(backup)
    if (
        selected.operation != "backup"
        or selected.packet_id != packet.packet_id
        or selected.packet_digest != packet.packet_digest
        or selected.synthetic_project_id != packet.synthetic_project_id
        or selected.backup_digest != _backup_digest(packet, selected.target_states)
    ):
        raise SyntheticRolloutError("synthetic A7 backup is invalid for this packet")
    _require_state_matches_packet_targets(selected.target_states, packet, expected="before")
    _require_persisted_receipt(runtime, selected)
    return selected


def _require_prior_receipt(
    runtime: DisposableRuntime,
    receipt: SyntheticRolloutReceipt | Mapping[str, Any],
    packet: SyntheticA7ApprovalPacket,
    backup: SyntheticRolloutReceipt,
    operation: str,
    state: SyntheticRolloutState,
) -> SyntheticRolloutReceipt:
    selected = _coerce_receipt(receipt)
    if (
        selected.operation != operation
        or selected.packet_id != packet.packet_id
        or selected.packet_digest != packet.packet_digest
        or selected.synthetic_project_id != packet.synthetic_project_id
        or selected.backup_id != backup.backup_id
        or selected.backup_digest != backup.backup_digest
        or selected.state_revision != state.state_revision
        or selected.target_states != state.targets
    ):
        raise SyntheticRolloutError("synthetic A7 rollout sequence receipt is invalid")
    _require_persisted_receipt(runtime, selected)
    return selected


def _require_persisted_receipt(runtime: DisposableRuntime, receipt: SyntheticRolloutReceipt) -> None:
    try:
        stored = SyntheticRolloutReceipt.from_value(read_disposable_json(runtime, _receipt_path(receipt.receipt_id)))
        validate_named_document("activation-v2-a7-rollout-receipt-v1", stored.to_dict())
    except (IntegrityError, ContractError) as error:
        raise SyntheticRolloutError("synthetic A7 prerequisite receipt is unavailable") from error
    if stored != receipt:
        raise SyntheticRolloutError("synthetic A7 prerequisite receipt is not the persisted receipt")


def _coerce_receipt(value: SyntheticRolloutReceipt | Mapping[str, Any]) -> SyntheticRolloutReceipt:
    if isinstance(value, SyntheticRolloutReceipt):
        return value
    if type(value) is not dict:
        raise SyntheticRolloutError("synthetic A7 receipt is invalid")
    validate_named_document("activation-v2-a7-rollout-receipt-v1", value)
    return SyntheticRolloutReceipt.from_value(value)


def _require_exact_revision(state: SyntheticRolloutState, expected: object, label: str) -> None:
    _require_revision(expected, label + " expected revision", minimum=0)
    if state.state_revision != expected:
        raise SyntheticRolloutError(label + " compare-and-swap revision is stale")


def _require_state_matches_receipt(state: SyntheticRolloutState, receipt: SyntheticRolloutReceipt) -> None:
    if state.targets != receipt.target_states:
        raise SyntheticRolloutError("synthetic A7 state does not match its backup")


def _require_state_matches_packet(
    state: SyntheticRolloutState,
    packet: SyntheticA7ApprovalPacket,
    *,
    expected: str,
) -> None:
    _require_state_matches_packet_targets(state.targets, packet, expected=expected)


def _require_state_matches_packet_targets(
    states: tuple[SyntheticRolloutTargetState, ...],
    packet: SyntheticA7ApprovalPacket,
    *,
    expected: str,
) -> None:
    if expected not in {"before", "candidate"}:
        raise SyntheticRolloutError("synthetic A7 expected state is invalid")
    expected_digests = {
        target.target_id: target.before_digest if expected == "before" else target.candidate_after_digest
        for target in packet.targets
    }
    if (
        {item.target_id: item.content_digest for item in states} != expected_digests
        or any(item.synthetic_project_id != packet.synthetic_project_id for item in states)
    ):
        raise SyntheticRolloutError("synthetic A7 target digests do not match the packet")


def _backup_digest(packet: SyntheticA7ApprovalPacket, targets: tuple[SyntheticRolloutTargetState, ...]) -> str:
    return sha256_hex(
        {
            "packet_id": packet.packet_id,
            "packet_digest": packet.packet_digest,
            "synthetic_project_id": packet.synthetic_project_id,
            "target_states": [target.to_dict() for target in targets],
        }
    )


def _receipt(
    *,
    receipt_id: str,
    operation: str,
    packet: SyntheticA7ApprovalPacket,
    backup_id: str,
    backup_digest: str,
    expected_state_revision: int,
    previous_state_revision: int,
    state_revision: int,
    target_states: tuple[SyntheticRolloutTargetState, ...],
    created_at: str,
) -> SyntheticRolloutReceipt:
    return SyntheticRolloutReceipt(
        schema_version=A7_RECEIPT_VERSION,
        receipt_id=receipt_id,
        operation=operation,
        status=_STATUS_BY_OPERATION[operation],
        packet_id=packet.packet_id,
        packet_digest=packet.packet_digest,
        synthetic_project_id=packet.synthetic_project_id,
        backup_id=backup_id,
        backup_digest=backup_digest,
        expected_state_revision=expected_state_revision,
        previous_state_revision=previous_state_revision,
        state_revision=state_revision,
        target_states=target_states,
        fixture_only=True,
        network_calls=0,
        global_target_access=False,
        global_mutation=False,
        authority_write=False,
        created_at=created_at,
    )


def _write_state_then_receipt(
    runtime: DisposableRuntime,
    previous_state: SyntheticRolloutState,
    next_state: SyntheticRolloutState,
    receipt: SyntheticRolloutReceipt,
) -> None:
    pending = SyntheticA7PendingOperation(
        schema_version=A7_PENDING_OPERATION_VERSION,
        journal_id=f"a7-journal:{uuid.uuid4()}",
        operation=receipt.operation,
        packet_id=receipt.packet_id,
        packet_digest=receipt.packet_digest,
        synthetic_project_id=receipt.synthetic_project_id,
        previous_state=previous_state,
        next_state=next_state,
        receipt=receipt,
    )
    _write_pending_operation(runtime, pending)
    _write_state(runtime, next_state)
    _write_receipt(runtime, receipt)
    remove_disposable_json(runtime, _journal_path(receipt.packet_id))


def _write_state(runtime: DisposableRuntime, state: SyntheticRolloutState) -> None:
    validate_named_document("activation-v2-a7-rollout-state-v1", state.to_dict())
    write_disposable_json(runtime, _state_path(state.packet_id), state.to_dict())


def _write_receipt(runtime: DisposableRuntime, receipt: SyntheticRolloutReceipt) -> None:
    validate_named_document("activation-v2-a7-rollout-receipt-v1", receipt.to_dict())
    relative = _receipt_path(receipt.receipt_id)
    if disposable_json_exists(runtime, relative):
        raise SyntheticRolloutError("synthetic A7 receipt identity already exists")
    write_disposable_json(runtime, relative, receipt.to_dict())


def _write_pending_operation(runtime: DisposableRuntime, pending: SyntheticA7PendingOperation) -> None:
    validate_named_document("activation-v2-a7-pending-operation-v1", pending.to_dict())
    relative = _journal_path(pending.packet_id)
    if disposable_json_exists(runtime, relative):
        raise SyntheticRolloutError("synthetic A7 pending operation already exists")
    write_disposable_json(runtime, relative, pending.to_dict())


def _state_path(packet_id: str) -> str:
    return f"registry/a7-rollout/{packet_id.removeprefix('a7-packet:')}/state.json"


def _receipt_path(receipt_id: str) -> str:
    return f"receipts/a7-rollout/{receipt_id.removeprefix('a7-rollout-receipt:')}.json"


def _journal_path(packet_id: str) -> str:
    return f"registry/a7-rollout/{packet_id.removeprefix('a7-packet:')}/pending-operation.json"


def _require_prefixed_uuid(value: object, prefix: str, label: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix + ":"):
        raise SyntheticRolloutError(f"{label} is invalid")
    if _UUID.fullmatch(value.removeprefix(prefix + ":")) is None:
        raise SyntheticRolloutError(f"{label} is invalid")
    return value


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise SyntheticRolloutError(f"{label} is invalid")
    return value


def _require_project_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _PROJECT_ID.fullmatch(value) is None:
        raise SyntheticRolloutError(f"{label} is invalid")
    return value


def _require_revision(value: object, label: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise SyntheticRolloutError(f"{label} is invalid")
    return value


def _require_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise SyntheticRolloutError(f"{label} is invalid")
    try:
        parse_rfc3339_utc(value)
    except Exception as error:
        raise SyntheticRolloutError(f"{label} is invalid") from error
    return value
