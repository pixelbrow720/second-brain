"""Exact packet preparation for the staged Practical V1 global skill update.

This module is read-only against the supplied Codex home. It stages no global
mutation and grants no authority to apply the packet.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping

from .canonical import sha256_bytes, sha256_hex
from .errors import IntegrityError, PathUnsafeError, SemanticValidationError
from .jsonio import load_strict_json
from .practical_v1 import practical_v1_source_tree_digest
from .workspace import repository_root, resolve_workspace_path


PRACTICAL_PACKET_SCHEMA_VERSION = 1
PRACTICAL_PACKET_WRITER_VERSION = "practical-v1-approval/1"
PRACTICAL_BACKUP_ID = "practical-v1-v1"
PRACTICAL_STAGED_ROOT = Path("dist/global/practical-v1")
PRACTICAL_SKILL_RELATIVE = Path("skills/pixel-second-brain-workflow/SKILL.md")
PRACTICAL_BRIDGE_RELATIVE = Path("skills/pixel-second-brain-workflow/scripts/pixel-second-brain")
PRACTICAL_BRIDGE_DIRECTORY = PRACTICAL_BRIDGE_RELATIVE.parent
PRACTICAL_BACKUP_RELATIVE = Path("second-brain-backups") / PRACTICAL_BACKUP_ID
_EXPECTED_SOURCE = re.compile(r'^EXPECTED_SOURCE_TREE_SHA256 = "([0-9a-f]{64})"$', re.MULTILINE)


def _now_utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _contained(root: Path, candidate: Path) -> Path:
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise PathUnsafeError("Practical V1 target escapes its declared root") from error
    return resolved


def _read_regular(path: Path, *, maximum_bytes: int = 1_048_576) -> tuple[bytes, int]:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise IntegrityError("required Practical V1 file is missing") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PathUnsafeError("Practical V1 file must be a regular non-symlink file")
    if metadata.st_size > maximum_bytes:
        raise IntegrityError("Practical V1 file exceeds its inspection bound")
    return path.read_bytes(), stat.S_IMODE(metadata.st_mode)


def _absent(path: Path) -> bool:
    return not path.exists() and not path.is_symlink()


def _staged_bundle(staged_root: Path | None = None) -> dict[str, bytes]:
    source = repository_root().resolve()
    root = _contained(source, source / (PRACTICAL_STAGED_ROOT if staged_root is None else staged_root))
    required = (PRACTICAL_SKILL_RELATIVE, PRACTICAL_BRIDGE_RELATIVE, Path("rollback.py"))
    result: dict[str, bytes] = {}
    for relative in required:
        contents, _ = _read_regular(_contained(root, root / relative))
        result[relative.as_posix()] = contents
    skill = result[PRACTICAL_SKILL_RELATIVE.as_posix()]
    if not skill.startswith(b"---\n") or b"name: pixel-second-brain-workflow\n" not in skill[:512]:
        raise IntegrityError("staged Practical V1 skill metadata is invalid")
    bridge = result[PRACTICAL_BRIDGE_RELATIVE.as_posix()]
    match = _EXPECTED_SOURCE.search(bridge.decode("utf-8"))
    current_digest, _ = practical_v1_source_tree_digest()
    if match is None or match.group(1) != current_digest:
        raise IntegrityError("staged bridge source binding is stale")
    return result


def _backup_manifest_bytes(
    *,
    codex_home: Path,
    skill_before_sha256: str,
    skill_before_mode: int,
    skill_after_sha256: str,
    bridge_after_sha256: str,
    rollback_after_sha256: str,
) -> bytes:
    value = {
        "backup_id": PRACTICAL_BACKUP_ID,
        "bridge_after_sha256": bridge_after_sha256,
        "bridge_before_sha256": "absent",
        "bridge_relative_path": PRACTICAL_BRIDGE_RELATIVE.as_posix(),
        "codex_home": str(codex_home),
        "rollback_after_sha256": rollback_after_sha256,
        "schema_version": 1,
        "skill_after_sha256": skill_after_sha256,
        "skill_before_mode": skill_before_mode,
        "skill_before_sha256": skill_before_sha256,
        "skill_relative_path": PRACTICAL_SKILL_RELATIVE.as_posix(),
    }
    return (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _target(
    path: str,
    *,
    kind: str,
    role: str,
    operation: str,
    before_sha256: str,
    after_sha256: str,
    before_mode: int | None,
    after_mode: int | None,
) -> dict[str, Any]:
    return {
        "after_mode": after_mode,
        "after_sha256": after_sha256,
        "before_mode": before_mode,
        "before_sha256": before_sha256,
        "kind": kind,
        "operation": operation,
        "path": path,
        "role": role,
    }


def _without_digest(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in packet.items() if key != "packet_digest"}


def verify_practical_v1_packet(packet: Mapping[str, Any]) -> None:
    if not isinstance(packet, Mapping):
        raise SemanticValidationError("Practical V1 packet must be an object")
    if packet.get("schema_version") != PRACTICAL_PACKET_SCHEMA_VERSION:
        raise SemanticValidationError("Practical V1 packet schema is invalid")
    if packet.get("rollout") != "PRACTICAL_V1" or packet.get("status") != "PENDING_EXACT_USER_APPROVAL":
        raise SemanticValidationError("Practical V1 packet state is invalid")
    digest = packet.get("packet_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SemanticValidationError("Practical V1 packet digest is invalid")
    if sha256_hex(_without_digest(packet)) != digest:
        raise IntegrityError("Practical V1 packet digest does not match its contents")


def build_practical_v1_packet(
    codex_home: Path,
    *,
    created_at: str | None = None,
    staged_root: Path | None = None,
) -> dict[str, Any]:
    """Build a new exact packet without changing the supplied Codex home."""

    home = codex_home.resolve(strict=True)
    if not home.is_dir() or home.is_symlink():
        raise PathUnsafeError("Codex home must be an existing non-symlink directory")
    timestamp = _now_utc() if created_at is None else created_at
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise SemanticValidationError("packet timestamp must be RFC3339 UTC text")
    bundle = _staged_bundle(staged_root)

    skill_path = _contained(home, home / PRACTICAL_SKILL_RELATIVE)
    bridge_path = _contained(home, home / PRACTICAL_BRIDGE_RELATIVE)
    bridge_directory = _contained(home, home / PRACTICAL_BRIDGE_DIRECTORY)
    backup_root = _contained(home, home / PRACTICAL_BACKUP_RELATIVE)
    skill_before, skill_mode = _read_regular(skill_path)
    if not _absent(bridge_path) or not _absent(bridge_directory):
        raise IntegrityError("Practical V1 bridge destination already exists")
    if not _absent(backup_root):
        raise IntegrityError("Practical V1 backup destination already exists")

    staged_skill = bundle[PRACTICAL_SKILL_RELATIVE.as_posix()]
    staged_bridge = bundle[PRACTICAL_BRIDGE_RELATIVE.as_posix()]
    staged_rollback = bundle["rollback.py"]
    skill_before_sha = sha256_bytes(skill_before)
    skill_after_sha = sha256_bytes(staged_skill)
    bridge_sha = sha256_bytes(staged_bridge)
    rollback_sha = sha256_bytes(staged_rollback)
    source_digest, source_file_count = practical_v1_source_tree_digest()
    backup_manifest = _backup_manifest_bytes(
        codex_home=home,
        skill_before_sha256=skill_before_sha,
        skill_before_mode=skill_mode,
        skill_after_sha256=skill_after_sha,
        bridge_after_sha256=bridge_sha,
        rollback_after_sha256=rollback_sha,
    )
    backup_manifest_sha = sha256_bytes(backup_manifest)
    backup_tree_sha = sha256_hex(
        {
            "manifest.json": backup_manifest_sha,
            "rollback.py": rollback_sha,
            "skill-before.md": skill_before_sha,
        }
    )
    bridge_tree_sha = sha256_hex({"pixel-second-brain": bridge_sha})

    targets = [
        _target(
            PRACTICAL_SKILL_RELATIVE.as_posix(),
            kind="file",
            role="mutate",
            operation="replace-managed-skill",
            before_sha256=skill_before_sha,
            after_sha256=skill_after_sha,
            before_mode=skill_mode,
            after_mode=0o600,
        ),
        _target(
            PRACTICAL_BRIDGE_DIRECTORY.as_posix(),
            kind="directory-tree",
            role="mutate",
            operation="create-bridge-directory",
            before_sha256="absent",
            after_sha256=bridge_tree_sha,
            before_mode=None,
            after_mode=0o700,
        ),
        _target(
            PRACTICAL_BRIDGE_RELATIVE.as_posix(),
            kind="file",
            role="mutate",
            operation="create-source-bound-bridge-wrapper",
            before_sha256="absent",
            after_sha256=bridge_sha,
            before_mode=None,
            after_mode=0o700,
        ),
        _target(
            PRACTICAL_BACKUP_RELATIVE.as_posix(),
            kind="directory-tree",
            role="backup",
            operation="create-backup-tree",
            before_sha256="absent",
            after_sha256=backup_tree_sha,
            before_mode=None,
            after_mode=0o700,
        ),
        _target(
            (PRACTICAL_BACKUP_RELATIVE / "skill-before.md").as_posix(),
            kind="file",
            role="backup",
            operation="copy-current-skill-before-state",
            before_sha256="absent",
            after_sha256=skill_before_sha,
            before_mode=None,
            after_mode=skill_mode,
        ),
        _target(
            (PRACTICAL_BACKUP_RELATIVE / "manifest.json").as_posix(),
            kind="file",
            role="backup",
            operation="create-rollback-manifest",
            before_sha256="absent",
            after_sha256=backup_manifest_sha,
            before_mode=None,
            after_mode=0o600,
        ),
        _target(
            (PRACTICAL_BACKUP_RELATIVE / "rollback.py").as_posix(),
            kind="file",
            role="backup",
            operation="create-drift-refusing-rollback",
            before_sha256="absent",
            after_sha256=rollback_sha,
            before_mode=None,
            after_mode=0o700,
        ),
    ]
    packet: dict[str, Any] = {
        "schema_version": PRACTICAL_PACKET_SCHEMA_VERSION,
        "writer_version": PRACTICAL_PACKET_WRITER_VERSION,
        "rollout": "PRACTICAL_V1",
        "status": "PENDING_EXACT_USER_APPROVAL",
        "created_at": timestamp,
        "codex_home": str(home),
        "global_mutation": False,
        "local_gate_state": "PRACTICAL_V1_READY_FOR_APPROVAL",
        "approval_request": (
            "Authorize exactly the listed Practical V1 targets: replace the existing "
            "pixel-second-brain-workflow SKILL.md, create its source-bound local bridge wrapper, "
            "and create the practical-v1-v1 backup/rollback tree. Do not change AGENTS.md, "
            "config.toml, agents, providers, plugins, MCPs, hooks, 9router, Obsidian, or ai-memory."
        ),
        "targets": targets,
        "staged_bundle": {
            "path": PRACTICAL_STAGED_ROOT.as_posix(),
            "sha256": {
                PRACTICAL_SKILL_RELATIVE.as_posix(): skill_after_sha,
                PRACTICAL_BRIDGE_RELATIVE.as_posix(): bridge_sha,
                "rollback.py": rollback_sha,
            },
        },
        "source_binding": {
            "source_root": "/home/pixel/Data/PROJECT/second-brain",
            "source_tree_sha256": source_digest,
            "source_file_count": source_file_count,
            "drift_behavior": "bridge refuses execution until a newly staged and approved digest is installed",
        },
        "permission_and_network_impact": {
            "global_write_after_approval": [
                PRACTICAL_SKILL_RELATIVE.as_posix(),
                PRACTICAL_BRIDGE_RELATIVE.as_posix(),
                PRACTICAL_BACKUP_RELATIVE.as_posix(),
            ],
            "runtime_reads": "only explicit non-symlink runtime-relative stores and an explicit exact Git project root",
            "runtime_writes": "only pending-review proposal metadata under the explicit runtime root",
            "authority_store_commits": "none",
            "network": "none; no provider, connector, MCP, plugin, or 9router call",
            "preserved_global_surfaces": [
                "AGENTS.md",
                "config.toml",
                "agents",
                "providers",
                "plugins",
                "MCP declarations",
                "hooks",
            ],
        },
        "retention_impact": {
            "bridge_stdout": "selected bounded memory results are returned to the invoking task and are not auto-persisted",
            "proposal_outbox": "IDs, revisions, hashes, target kind, and fixed reason metadata only; no transcript or body",
            "router_evidence": "allowlisted structured fields and correlation digests only; no prompt, response, headers, or credentials",
            "upstream": "provider/router upstream retention remains unverifiable from local configuration",
        },
        "canary_sequence": [
            "verify staged skill and wrapper digests",
            "verify source binding and refuse drift",
            "confirm DIRECT returns DIRECT_NO_MEMORY before root/store access",
            "run health against disposable explicit project and global stores",
            "run one targeted project recovery read and one global knowledge read",
            "enqueue one disposable pending-review proposal and prove authority bytes unchanged",
            "verify the synthetic operator-trusted 9router fixture with no network",
            "require a separate report and approval before any broader rollout",
        ],
        "backup": {
            "path": PRACTICAL_BACKUP_RELATIVE.as_posix(),
            "tree_sha256": backup_tree_sha,
            "must_be_absent_before_apply": True,
            "preserved_after_rollback": True,
        },
        "rollback": {
            "command_after_apply": (
                "python3 <codex-home>/second-brain-backups/practical-v1-v1/rollback.py "
                "--backup-dir <codex-home>/second-brain-backups/practical-v1-v1 "
                "--rollback-packet-digest <fresh-digest> --approval-reference <fresh-approval>"
            ),
            "restores": [PRACTICAL_SKILL_RELATIVE.as_posix()],
            "removes": [PRACTICAL_BRIDGE_RELATIVE.as_posix()],
            "refuses_if_post_state_drifted": True,
            "requires_fresh_exact_rollback_packet_and_approval": True,
        },
        "strict_release_state": {
            "M8": "in_progress",
            "M9": "initial_shadow_complete_unattested",
            "strict_signed_upstream_attestation": "retained_future_hardening",
            "provider_attestation_claimed": False,
        },
        "limitations": [
            "operator-trusted-router-logs-do-not-prove-remote-provider-internal-behavior",
            "project-id-to-exact-git-root-mapping-remains-an-operator-trust-decision",
            "DIRECT-does-not-read-memory-by-default",
            "bridge-does-not-ingest-transcripts-or-auto-write-authority",
            "only-project-to-global-pending-review-proposals-are-supported",
            "source-repository-relocation-or-drift-requires-a-new-packet-and-approval",
            "staged-rollback-helper-does-not-create-or-validate-the-required-fresh-rollback-packet",
            "strict-M8-M9-quality-live-route-soak-and-rollback-gates-remain-open",
        ],
    }
    packet["packet_digest"] = sha256_hex(packet)
    verify_practical_v1_packet(packet)
    return packet


def validate_practical_v1_packet_current(packet: Mapping[str, Any], codex_home: Path) -> None:
    verify_practical_v1_packet(packet)
    rebuilt = build_practical_v1_packet(
        codex_home,
        created_at=str(packet["created_at"]),
    )
    if rebuilt["packet_digest"] != packet["packet_digest"]:
        raise IntegrityError("Practical V1 packet no longer matches current global or staged state")


def serialize_practical_v1_packet(packet: Mapping[str, Any]) -> bytes:
    verify_practical_v1_packet(packet)
    return (json.dumps(dict(packet), ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_practical_v1_packet(
    packet: Mapping[str, Any],
    relative_path: str = "artifacts/practical-v1-approval-packet.json",
    *,
    replace_existing_packet_digest: str | None = None,
) -> Path:
    payload = serialize_practical_v1_packet(packet)
    target = resolve_workspace_path(relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file():
            raise PathUnsafeError("Practical V1 packet target is unsafe")
        existing = load_strict_json(target)
        verify_practical_v1_packet(existing)
        if existing["packet_digest"] == packet["packet_digest"]:
            return target
        if replace_existing_packet_digest != existing["packet_digest"]:
            raise IntegrityError("existing Practical V1 packet requires exact CAS replacement")
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        temporary.write_bytes(payload)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target
