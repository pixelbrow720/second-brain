"""M9 exact-packet and rollback helpers for an additive Codex rollout.

The module can inspect a supplied Codex home without exposing configuration
contents. It prepares an exact digest-bound packet and refuses to mutate a
target until the caller supplies that packet's current digest and an explicit
approval reference.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import tomllib
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

from .canonical import sha256_bytes, sha256_hex
from .errors import AuthorityDeniedError, IntegrityError, PathUnsafeError, SemanticValidationError
from .jsonio import load_strict_json
from .workspace import repository_root, resolve_workspace_path


M9_PACKET_SCHEMA_VERSION = 1
M9_ROLLBACK_PACKET_SCHEMA_VERSION = 1
M9_BACKUP_ID = "m9-v1"
M9_WRITER_VERSION = "m9-global-rollout/1"
M9_ROLLBACK_WRITER_VERSION = "m9-rollback-approval/1"
M9_SKILL_NAME = "pixel-second-brain-workflow"
M9_AGENTS_START = b"<!-- pixel-second-brain-v1:start -->"
M9_AGENTS_END = b"<!-- pixel-second-brain-v1:end -->"
M9_STAGED_ROOT = Path("dist/global/m9")
M9_SKILL_RELATIVE = Path("skills") / M9_SKILL_NAME / "SKILL.md"
M9_BACKUP_RELATIVE = Path("second-brain-backups") / M9_BACKUP_ID
M9_TOOLING_RELATIVE_PATHS = (
    Path("scripts/prepare_m9_approval_packet.py"),
    Path("scripts/run_m9_rollout.py"),
    Path("src/second_brain/global_rollout.py"),
)
M9_ROLLBACK_TOOLING_RELATIVE_PATHS = (
    Path("scripts/prepare_m9_rollback_approval_packet.py"),
    Path("scripts/run_m9_rollout.py"),
    Path("src/second_brain/global_rollout.py"),
)
M9_REQUIRED_PROFILE_PAIRS = frozenset(
    {
        ("cx/gpt-5.6-terra", "max"),
        ("cx/gpt-5.6-terra", "xhigh"),
        ("cx/gpt-5.6-terra", "high"),
        ("cx/gpt-5.6-sol", "max"),
        ("cx/gpt-5.6-sol", "xhigh"),
        ("cx/gpt-5.6-luna", "xhigh"),
        ("cx/gpt-5.5", "xhigh"),
    }
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _contained(root: Path, candidate: Path) -> Path:
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise PathUnsafeError("M9 target escapes its declared root") from error
    return resolved


def _read_regular_file(path: Path, *, maximum_bytes: int = 262_144) -> tuple[bytes, int]:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise IntegrityError("M9 required target is missing") from error
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise PathUnsafeError("M9 target must be a regular non-symlink file")
    if metadata.st_size > maximum_bytes:
        raise IntegrityError("M9 target exceeds the bounded inspection size")
    return path.read_bytes(), stat.S_IMODE(metadata.st_mode)


def _is_absent(path: Path) -> bool:
    return not path.exists() and not path.is_symlink()


def _require_absent(path: Path) -> None:
    if not _is_absent(path):
        raise IntegrityError("M9 managed destination already exists")


def _atomic_write(path: Path, payload: bytes, mode: int) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".m9-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


@contextmanager
def _agents_lock(path: Path) -> Iterator[None]:
    with path.open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _staged_root(path: Path | None = None) -> Path:
    root = repository_root().resolve()
    staged = (root / (M9_STAGED_ROOT if path is None else path)).resolve(strict=False)
    _contained(root, staged)
    return staged


def _load_staged_bundle(path: Path | None = None) -> dict[str, bytes]:
    staged = _staged_root(path)
    required = (
        Path("AGENTS.append.md"),
        M9_SKILL_RELATIVE,
        Path("rollback.py"),
    )
    bundle: dict[str, bytes] = {}
    for relative in required:
        target = _contained(staged, staged / relative)
        contents, _ = _read_regular_file(target)
        bundle[relative.as_posix()] = contents
    appendix = bundle["AGENTS.append.md"]
    if not appendix.startswith(M9_AGENTS_START) or not appendix.rstrip().endswith(M9_AGENTS_END):
        raise IntegrityError("M9 staged guidance markers are invalid")
    return bundle


def _tooling_sha256() -> dict[str, str]:
    root = repository_root().resolve()
    digests: dict[str, str] = {}
    for relative in M9_TOOLING_RELATIVE_PATHS:
        target = _contained(root, root / relative)
        contents, _ = _read_regular_file(target, maximum_bytes=1_048_576)
        digests[relative.as_posix()] = sha256_bytes(contents)
    return digests


def _safe_config_summary(path: Path) -> tuple[dict[str, Any], str, int]:
    contents, mode = _read_regular_file(path)
    try:
        parsed = tomllib.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SemanticValidationError("M9 config is not valid TOML") from error
    if not isinstance(parsed, dict):
        raise SemanticValidationError("M9 config must be a TOML table")
    mcp_servers = parsed.get("mcp_servers")
    providers = parsed.get("model_providers")
    provider_summaries = []
    if isinstance(providers, dict):
        for name, provider in sorted(providers.items()):
            provider_summaries.append(_safe_provider_summary(name, provider))
    return (
        {
            "top_level_keys": sorted(parsed),
            "has_agents_table": isinstance(parsed.get("agents"), dict),
            "has_hooks_table": isinstance(parsed.get("hooks"), dict),
            "mcp_server_names": sorted(mcp_servers) if isinstance(mcp_servers, dict) else [],
            "selected_model_provider": parsed.get("model_provider") if isinstance(parsed.get("model_provider"), str) else None,
            "openai_base_url": _safe_network_destination(parsed.get("openai_base_url")),
            "model_providers": provider_summaries,
        },
        sha256_bytes(contents),
        mode,
    )


def _safe_network_destination(value: object) -> dict[str, Any] | None:
    """Return only a network destination's routing identity, never its path or credentials."""

    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return {"configured": True, "valid_http_endpoint": False}
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return {"configured": True, "valid_http_endpoint": False}
    return {
        "configured": True,
        "host": parsed.hostname,
        "port": port,
        "scheme": parsed.scheme,
    }


def _safe_provider_summary(name: object, provider: object) -> dict[str, Any]:
    table = provider if isinstance(provider, dict) else {}
    return {
        "base_url": _safe_network_destination(table.get("base_url")),
        "has_bearer_token_reference": isinstance(table.get("bearer_token"), str) and bool(table.get("bearer_token")),
        "has_env_key_reference": isinstance(table.get("env_key"), str) and bool(table.get("env_key")),
        "name": name if isinstance(name, str) else "invalid-provider-name",
        "wire_api": table.get("wire_api") if isinstance(table.get("wire_api"), str) else None,
    }


def _safe_agent_summary(path: Path) -> dict[str, Any]:
    contents, mode = _read_regular_file(path)
    try:
        parsed = tomllib.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SemanticValidationError("M9 custom-agent TOML is invalid") from error
    if not isinstance(parsed, dict):
        raise SemanticValidationError("M9 custom-agent TOML must be a table")
    for field in ("name", "description", "developer_instructions", "model", "model_reasoning_effort"):
        if not isinstance(parsed.get(field), str) or not parsed[field]:
            raise SemanticValidationError("M9 custom-agent file omits a required string field")
    return {
        "path": (Path("agents") / path.name).as_posix(),
        "sha256": sha256_bytes(contents),
        "mode": mode,
        "model": parsed["model"],
        "model_reasoning_effort": parsed["model_reasoning_effort"],
        "developer_instructions_sha256": sha256_bytes(parsed["developer_instructions"].encode("utf-8")),
    }


def _append_guidance(existing: bytes, appendix: bytes) -> bytes:
    if M9_AGENTS_START in existing or M9_AGENTS_END in existing:
        raise IntegrityError("M9 guidance markers already exist in global AGENTS")
    separator = b"\n" if existing.endswith(b"\n") else b"\n\n"
    return existing + separator + appendix.rstrip(b"\n") + b"\n"


def _backup_manifest_bytes(
    *,
    codex_home: Path,
    agents_before_sha256: str,
    agents_after_sha256: str,
    agents_mode: int,
    skill_after_sha256: str,
    rollback_after_sha256: str,
) -> bytes:
    manifest = {
        "agents_after_sha256": agents_after_sha256,
        "agents_before_sha256": agents_before_sha256,
        "agents_mode": agents_mode,
        "backup_id": M9_BACKUP_ID,
        "codex_home": str(codex_home),
        "rollback_after_sha256": rollback_after_sha256,
        "schema_version": 1,
        "skill_after_sha256": skill_after_sha256,
    }
    return (json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _target(
    path: str,
    *,
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
        "operation": operation,
        "path": path,
        "role": role,
    }


def _without_packet_digest(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in packet.items() if key != "packet_digest"}


def verify_m9_packet(packet: Mapping[str, Any]) -> None:
    if not isinstance(packet, Mapping):
        raise SemanticValidationError("M9 packet must be an object")
    if packet.get("schema_version") != M9_PACKET_SCHEMA_VERSION or packet.get("milestone") != "M9":
        raise SemanticValidationError("M9 packet schema or milestone is invalid")
    digest = packet.get("packet_digest")
    if not isinstance(digest, str) or len(digest) != 64:
        raise SemanticValidationError("M9 packet digest is invalid")
    if sha256_hex(_without_packet_digest(packet)) != digest:
        raise IntegrityError("M9 packet digest does not match its contents")


def build_m9_packet(
    codex_home: Path,
    *,
    created_at: str | None = None,
    staged_root: Path | None = None,
) -> dict[str, Any]:
    """Create a redacted, exact M9 packet without mutating ``codex_home``."""

    home = codex_home.resolve()
    if not home.is_dir() or home.is_symlink():
        raise PathUnsafeError("M9 Codex home must be an existing non-symlink directory")
    timestamp = _now_utc() if created_at is None else created_at
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise SemanticValidationError("M9 packet timestamp must be an RFC3339 UTC string")

    bundle = _load_staged_bundle(staged_root)
    agents_path = _contained(home, home / "AGENTS.md")
    config_path = _contained(home, home / "config.toml")
    global_agents, agents_mode = _read_regular_file(agents_path)
    global_agents_after = _append_guidance(global_agents, bundle["AGENTS.append.md"])
    config_summary, config_sha256, config_mode = _safe_config_summary(config_path)

    agents_directory = _contained(home, home / "agents")
    if not agents_directory.is_dir() or agents_directory.is_symlink():
        raise PathUnsafeError("M9 agents directory is unsafe")
    agent_summaries = [_safe_agent_summary(path) for path in sorted(agents_directory.glob("*.toml"))]
    actual_pairs = {(item["model"], item["model_reasoning_effort"]) for item in agent_summaries}
    missing_pairs = sorted(M9_REQUIRED_PROFILE_PAIRS - actual_pairs)

    skill_destination = _contained(home, home / M9_SKILL_RELATIVE)
    skill_directory = skill_destination.parent
    backup_root = _contained(home, home / M9_BACKUP_RELATIVE)
    _require_absent(skill_directory)
    _require_absent(backup_root.parent)

    skill_contents = bundle[M9_SKILL_RELATIVE.as_posix()]
    rollback_contents = bundle["rollback.py"]
    skill_sha256 = sha256_bytes(skill_contents)
    rollback_sha256 = sha256_bytes(rollback_contents)
    agents_before_sha256 = sha256_bytes(global_agents)
    agents_after_sha256 = sha256_bytes(global_agents_after)
    backup_manifest = _backup_manifest_bytes(
        codex_home=home,
        agents_before_sha256=agents_before_sha256,
        agents_after_sha256=agents_after_sha256,
        agents_mode=agents_mode,
        skill_after_sha256=skill_sha256,
        rollback_after_sha256=rollback_sha256,
    )
    backup_manifest_sha256 = sha256_bytes(backup_manifest)
    backup_tree_sha256 = sha256_hex(
        {
            "AGENTS.md": agents_before_sha256,
            "manifest.json": backup_manifest_sha256,
            "rollback.py": rollback_sha256,
        }
    )

    targets = [
        _target(
            "AGENTS.md",
            role="mutate",
            operation="append-managed-guidance",
            before_sha256=agents_before_sha256,
            after_sha256=agents_after_sha256,
            before_mode=agents_mode,
            after_mode=agents_mode,
        ),
        _target(
            M9_SKILL_RELATIVE.as_posix(),
            role="mutate",
            operation="create-managed-skill",
            before_sha256="absent",
            after_sha256=skill_sha256,
            before_mode=None,
            after_mode=0o600,
        ),
        _target(
            (M9_BACKUP_RELATIVE / "AGENTS.md").as_posix(),
            role="backup",
            operation="create-before-state-copy",
            before_sha256="absent",
            after_sha256=agents_before_sha256,
            before_mode=None,
            after_mode=agents_mode,
        ),
        _target(
            (M9_BACKUP_RELATIVE / "manifest.json").as_posix(),
            role="backup",
            operation="create-rollback-manifest",
            before_sha256="absent",
            after_sha256=backup_manifest_sha256,
            before_mode=None,
            after_mode=0o600,
        ),
        _target(
            (M9_BACKUP_RELATIVE / "rollback.py").as_posix(),
            role="backup",
            operation="create-standalone-rollback",
            before_sha256="absent",
            after_sha256=rollback_sha256,
            before_mode=None,
            after_mode=0o700,
        ),
        _target(
            "config.toml",
            role="observe",
            operation="preserve",
            before_sha256=config_sha256,
            after_sha256=config_sha256,
            before_mode=config_mode,
            after_mode=config_mode,
        ),
    ]
    for agent in agent_summaries:
        targets.append(
            _target(
                agent["path"],
                role="observe",
                operation="preserve",
                before_sha256=agent["sha256"],
                after_sha256=agent["sha256"],
                before_mode=agent["mode"],
                after_mode=agent["mode"],
            )
        )
    targets.sort(key=lambda entry: entry["path"])

    status = "PENDING_EXACT_USER_APPROVAL" if not missing_pairs else "BLOCKED_PROFILE_PRECONDITION"
    packet: dict[str, Any] = {
        "approval_request": (
            "Authorize only these M9 mutations: append the marker-bounded guidance block to AGENTS.md; "
            "create skills/pixel-second-brain-workflow/SKILL.md; create "
            "second-brain-backups/m9-v1/AGENTS.md, manifest.json, and rollback.py. Preserve config.toml, "
            "all existing agents, plugins, MCP declarations, hooks, and provider settings. After apply, authorize "
            "only an initial 50-request synthetic PUBLIC read-only shadow through the listed model route, with no "
            "MCP call, capability execution, memory write, or external mutation; accept that upstream retention is "
            "unverifiable from local config. Require a new report and approval before any opt-in or expanded rollout."
        ),
        "backup": {
            "backup_directory": M9_BACKUP_RELATIVE.as_posix(),
            "backup_tree_sha256": backup_tree_sha256,
            "must_be_absent_before_apply": True,
            "preserved_after_rollback": True,
        },
        "codex_home": str(home),
        "created_at": timestamp,
        "evidence_scope": "global-preapproval-metadata",
        "global_mutation": False,
        "m8_evidence_gates": [
            "authorized-blinded-semantic-comparison",
            "monotonic-graph-performance-and-context-measurement",
            "per-case-m4-retrieval-context-evidence",
            "authenticated-live-route-and-read-only-shadow-evidence",
            "approved-global-before-state-and-verified-rollback",
        ],
        "milestone": "M9",
        "network_and_data_impact": {
            "configured_model_routes": config_summary["model_providers"],
            "configured_openai_route": config_summary["openai_base_url"],
            "configured_mcp_servers_not_invoked_by_canary": config_summary["mcp_server_names"],
            "packet_preparation": "no provider, connector, or network request; metadata and digests only",
            "later_live_canary": "synthetic PUBLIC prompts only; provider routing telemetry and timing may be observed after approval",
            "retention": "local evidence retains redacted digests, counters, and allowlisted route fields only; upstream provider retention is unverifiable from local config and must be accepted explicitly",
            "selected_model_provider": config_summary["selected_model_provider"],
        },
        "permission_impact": {
            "packet_preparation": "read-only access to declared Codex config and agent metadata",
            "apply_after_approval": "write only the listed guidance, skill, and backup paths",
            "no_changes": ["config.toml", "existing custom agents", "plugins", "MCP declarations", "hooks", "provider settings"],
        },
        "profile_precondition": {
            "actual_pair_count": len(actual_pairs),
            "missing_model_effort_pairs": [list(pair) for pair in missing_pairs],
            "required_pair_count": len(M9_REQUIRED_PROFILE_PAIRS),
        },
        "rollback": {
            "command_after_apply": "python3 <backup-directory>/rollback.py --backup-dir <backup-directory>",
            "refuses_if_post_state_drifted": True,
            "restores": ["AGENTS.md", M9_SKILL_RELATIVE.as_posix()],
        },
        "rollout_sequence": [
            "revalidate-current-packet-and-backup",
            "apply-additive-guidance-and-skill",
            "restart-or-open-a-fresh-Codex-session",
            "initial-50-request-read-only-shadow-with-synthetic-public-inputs",
            "report-and-request-new-approval-before-opt-in-canary",
            "opt-in-limited-expanded-default-and-soak-are-separate-promotions",
        ],
        "schema_version": M9_PACKET_SCHEMA_VERSION,
        "staged_bundle": {
            "path": M9_STAGED_ROOT.as_posix(),
            "sha256": {path: sha256_bytes(contents) for path, contents in sorted(bundle.items())},
        },
        "status": status,
        "targets": targets,
        "tooling_sha256": _tooling_sha256(),
        "unresolved_risks": [
            "upstream-provider-retention-is-not-inferable-from-local-config",
            "live-route-telemetry-contract-must-be-observed-before-promotion",
            "blinded-semantic-and-human-review-evidence-remain-required",
        ],
        "writer_version": M9_WRITER_VERSION,
    }
    packet["packet_digest"] = sha256_hex(packet)
    verify_m9_packet(packet)
    return packet


def validate_m9_packet_current(packet: Mapping[str, Any], codex_home: Path) -> None:
    """Reject a stale packet before any live test or global mutation starts."""

    verify_m9_packet(packet)
    if packet.get("status") != "PENDING_EXACT_USER_APPROVAL":
        raise IntegrityError("M9 packet is not ready for exact user approval")
    home = codex_home.resolve()
    if packet.get("codex_home") != str(home):
        raise IntegrityError("M9 packet targets a different Codex home")
    rebuilt = build_m9_packet(home, created_at=packet["created_at"])
    if rebuilt["packet_digest"] != packet["packet_digest"]:
        raise IntegrityError("M9 packet is stale or its staged bundle drifted")


def serialize_m9_packet(packet: Mapping[str, Any]) -> bytes:
    verify_m9_packet(packet)
    return (json.dumps(dict(packet), ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_m9_packet(
    packet: Mapping[str, Any],
    relative_output: str = "artifacts/m9-approval-packet.json",
    *,
    replace_existing_packet_digest: str | None = None,
) -> Path:
    """Write a packet only when new, identical, or explicitly CAS-replaced."""

    output = resolve_workspace_path(relative_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    serialized = serialize_m9_packet(packet)
    if output.exists() or output.is_symlink():
        current, _ = _read_regular_file(output, maximum_bytes=1_048_576)
        if current != serialized:
            if replace_existing_packet_digest is None:
                raise IntegrityError("M9 packet output already exists with different contents")
            existing = load_strict_json(output)
            if not isinstance(existing, dict):
                raise IntegrityError("M9 existing packet output is not an object")
            verify_m9_packet(existing)
            if (
                existing.get("writer_version") != M9_WRITER_VERSION
                or existing.get("packet_digest") != replace_existing_packet_digest
            ):
                raise IntegrityError("M9 existing packet does not match the explicit CAS digest")
            _atomic_write(output, serialized, 0o600)
        return output
    _atomic_write(output, serialized, 0o600)
    return output


def load_m9_packet(relative_path: str = "artifacts/m9-approval-packet.json") -> dict[str, Any]:
    packet = load_strict_json(resolve_workspace_path(relative_path))
    if not isinstance(packet, dict):
        raise SemanticValidationError("M9 packet file must contain an object")
    verify_m9_packet(packet)
    return packet


_M9_TARGET_KEYS = frozenset(
    ("after_mode", "after_sha256", "before_mode", "before_sha256", "operation", "path", "role")
)


def _require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise SemanticValidationError(f"{label} is invalid")
    return value


def _require_mode(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= 0o777:
        raise SemanticValidationError(f"{label} is invalid")
    return value


def _safe_m9_home(codex_home: Path) -> Path:
    if not isinstance(codex_home, Path):
        raise SemanticValidationError("M9 Codex home is invalid")
    try:
        metadata = codex_home.lstat()
    except OSError as error:
        raise IntegrityError("M9 Codex home is unavailable") from error
    if codex_home.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise PathUnsafeError("M9 Codex home must be a non-symlink directory")
    return codex_home.resolve(strict=True)


def _safe_relative_target(value: object) -> Path:
    if not isinstance(value, str):
        raise SemanticValidationError("M9 target path is invalid")
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise PathUnsafeError("M9 target path is unsafe")
    return relative


def _required_rollback_targets(packet: Mapping[str, Any], home: Path) -> dict[str, Mapping[str, Any]]:
    """Validate the five original M9 targets needed for a safe rollback."""

    verify_m9_packet(packet)
    if (
        packet.get("status") != "PENDING_EXACT_USER_APPROVAL"
        or packet.get("global_mutation") is not False
        or packet.get("codex_home") != str(home)
    ):
        raise IntegrityError("M9 initial packet is not a valid rollback source")
    backup = packet.get("backup")
    if (
        type(backup) is not dict
        or backup.get("backup_directory") != M9_BACKUP_RELATIVE.as_posix()
        or backup.get("must_be_absent_before_apply") is not True
        or backup.get("preserved_after_rollback") is not True
    ):
        raise IntegrityError("M9 initial rollback metadata is invalid")
    _require_sha256(backup.get("backup_tree_sha256"), "M9 initial backup tree")

    targets = packet.get("targets")
    if type(targets) is not list or not targets:
        raise IntegrityError("M9 initial target list is invalid")
    by_path: dict[str, Mapping[str, Any]] = {}
    for target in targets:
        if type(target) is not dict or set(target) != _M9_TARGET_KEYS:
            raise IntegrityError("M9 initial target is invalid")
        relative = _safe_relative_target(target["path"])
        path = relative.as_posix()
        if path in by_path:
            raise IntegrityError("M9 initial target paths are duplicated")
        by_path[path] = target

    specifications = {
        "AGENTS.md": ("mutate", "append-managed-guidance"),
        M9_SKILL_RELATIVE.as_posix(): ("mutate", "create-managed-skill"),
        (M9_BACKUP_RELATIVE / "AGENTS.md").as_posix(): ("backup", "create-before-state-copy"),
        (M9_BACKUP_RELATIVE / "manifest.json").as_posix(): ("backup", "create-rollback-manifest"),
        (M9_BACKUP_RELATIVE / "rollback.py").as_posix(): ("backup", "create-standalone-rollback"),
    }
    required: dict[str, Mapping[str, Any]] = {}
    for path, (role, operation) in specifications.items():
        target = by_path.get(path)
        if target is None or target["role"] != role or target["operation"] != operation:
            raise IntegrityError("M9 initial rollback target is invalid")
        required[path] = target

    agents = required["AGENTS.md"]
    skill = required[M9_SKILL_RELATIVE.as_posix()]
    backup_agents = required[(M9_BACKUP_RELATIVE / "AGENTS.md").as_posix()]
    manifest = required[(M9_BACKUP_RELATIVE / "manifest.json").as_posix()]
    rollback = required[(M9_BACKUP_RELATIVE / "rollback.py").as_posix()]
    agents_before = _require_sha256(agents["before_sha256"], "M9 rollback AGENTS before digest")
    _require_sha256(agents["after_sha256"], "M9 rollback AGENTS after digest")
    agents_before_mode = _require_mode(agents["before_mode"], "M9 rollback AGENTS before mode")
    if _require_mode(agents["after_mode"], "M9 rollback AGENTS after mode") != agents_before_mode:
        raise IntegrityError("M9 rollback AGENTS mode is inconsistent")
    _require_sha256(skill["after_sha256"], "M9 rollback skill digest")
    if (
        skill["before_sha256"] != "absent"
        or skill["before_mode"] is not None
        or _require_mode(skill["after_mode"], "M9 rollback skill mode") != 0o600
    ):
        raise IntegrityError("M9 rollback skill target is invalid")
    if (
        backup_agents["before_sha256"] != "absent"
        or backup_agents["before_mode"] is not None
        or _require_sha256(backup_agents["after_sha256"], "M9 rollback backup AGENTS digest") != agents_before
        or _require_mode(backup_agents["after_mode"], "M9 rollback backup AGENTS mode") != agents_before_mode
    ):
        raise IntegrityError("M9 rollback backup AGENTS target is invalid")
    for target, label, mode in ((manifest, "manifest", 0o600), (rollback, "helper", 0o700)):
        if (
            target["before_sha256"] != "absent"
            or target["before_mode"] is not None
            or _require_mode(target["after_mode"], f"M9 rollback {label} mode") != mode
        ):
            raise IntegrityError("M9 rollback backup target is invalid")
        _require_sha256(target["after_sha256"], f"M9 rollback {label} digest")
    return required


def _approved_post_state_path(home: Path, target: Mapping[str, Any]) -> tuple[Path, bytes, int]:
    relative = _safe_relative_target(target["path"])
    expected_digest = _require_sha256(target["after_sha256"], "M9 rollback target digest")
    expected_mode = _require_mode(target["after_mode"], "M9 rollback target mode")
    directory = home
    for component in relative.parts[:-1]:
        directory = directory / component
        try:
            metadata = directory.lstat()
        except OSError as error:
            raise IntegrityError("M9 rollback target parent is unavailable") from error
        if directory.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise PathUnsafeError("M9 rollback target parent is unsafe")
    candidate = directory / relative.parts[-1]
    _contained(home, candidate)
    contents, mode = _read_regular_file(candidate, maximum_bytes=1_048_576)
    if mode != expected_mode or sha256_bytes(contents) != expected_digest:
        raise IntegrityError("M9 rollback target differs from the approved post-state")
    return candidate, contents, mode


def _validated_rollback_state(packet: Mapping[str, Any], home: Path) -> dict[str, object]:
    """Read only the current rollback scope; unrelated config drift stays untouched."""

    required = _required_rollback_targets(packet, home)
    agents_path, agents_contents, agents_mode = _approved_post_state_path(home, required["AGENTS.md"])
    skill_path, skill_contents, skill_mode = _approved_post_state_path(home, required[M9_SKILL_RELATIVE.as_posix()])
    backup_agents_path, backup_agents_contents, backup_agents_mode = _approved_post_state_path(
        home, required[(M9_BACKUP_RELATIVE / "AGENTS.md").as_posix()]
    )
    manifest_path, _, _ = _approved_post_state_path(home, required[(M9_BACKUP_RELATIVE / "manifest.json").as_posix()])
    rollback_path, _, _ = _approved_post_state_path(home, required[(M9_BACKUP_RELATIVE / "rollback.py").as_posix()])
    if any(path != skill_path for path in skill_path.parent.iterdir()):
        raise IntegrityError("M9 skill directory contains foreign files")
    return {
        "agents_contents": agents_contents,
        "agents_mode": agents_mode,
        "agents_path": agents_path,
        "backup_agents_contents": backup_agents_contents,
        "backup_agents_mode": backup_agents_mode,
        "backup_agents_path": backup_agents_path,
        "manifest_path": manifest_path,
        "rollback_path": rollback_path,
        "skill_contents": skill_contents,
        "skill_mode": skill_mode,
        "skill_path": skill_path,
        "targets": required,
    }


def _rollback_tooling_sha256() -> dict[str, str]:
    root = repository_root().resolve()
    digests: dict[str, str] = {}
    for relative in M9_ROLLBACK_TOOLING_RELATIVE_PATHS:
        target = _contained(root, root / relative)
        contents, _ = _read_regular_file(target, maximum_bytes=1_048_576)
        digests[relative.as_posix()] = sha256_bytes(contents)
    return digests


def _rollback_approval_request(initial_packet_digest: str) -> str:
    return (
        "Authorize only this exact M9 global rollback: restore AGENTS.md from the "
        f"verified m9-v1 before-state and remove only skills/{M9_SKILL_NAME}/SKILL.md, "
        f"bound to initial M9 packet {initial_packet_digest}. Preserve the m9-v1 "
        "backup, do not change config, agents, plugins, MCPs, hooks, providers, "
        "or endpoints, and make no provider request, canary, expansion, or promotion."
    )


def _without_rollback_packet_digest(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in packet.items() if key != "packet_digest"}


def build_m9_rollback_approval_packet(
    initial_packet: Mapping[str, Any],
    codex_home: Path,
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a fresh, redacted approval packet for one exact global rollback."""

    home = _safe_m9_home(codex_home)
    timestamp = _now_utc() if created_at is None else created_at
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise SemanticValidationError("M9 rollback packet timestamp must be an RFC3339 UTC string")
    state = _validated_rollback_state(initial_packet, home)
    targets = state["targets"]
    if type(targets) is not dict:
        raise IntegrityError("M9 rollback targets are invalid")
    agents = targets["AGENTS.md"]
    skill = targets[M9_SKILL_RELATIVE.as_posix()]
    backup_agents = targets[(M9_BACKUP_RELATIVE / "AGENTS.md").as_posix()]
    manifest = targets[(M9_BACKUP_RELATIVE / "manifest.json").as_posix()]
    helper = targets[(M9_BACKUP_RELATIVE / "rollback.py").as_posix()]
    initial_digest = _require_sha256(initial_packet.get("packet_digest"), "M9 initial packet digest")
    packet: dict[str, Any] = {
        "approval_request": _rollback_approval_request(initial_digest),
        "backup": {
            "agents_before_sha256": backup_agents["after_sha256"],
            "backup_directory": M9_BACKUP_RELATIVE.as_posix(),
            "manifest_sha256": manifest["after_sha256"],
            "rollback_helper_sha256": helper["after_sha256"],
        },
        "codex_home": str(home),
        "created_at": timestamp,
        "global_mutation": True,
        "initial_m9_packet_digest": initial_digest,
        "milestone": "M9",
        "operations": [
            {
                "after_mode": agents["before_mode"],
                "after_sha256": agents["before_sha256"],
                "before_mode": agents["after_mode"],
                "before_sha256": agents["after_sha256"],
                "operation": "restore-exact-before-state",
                "path": "AGENTS.md",
            },
            {
                "after_mode": None,
                "after_sha256": "absent",
                "before_mode": skill["after_mode"],
                "before_sha256": skill["after_sha256"],
                "operation": "remove-exact-managed-skill",
                "path": M9_SKILL_RELATIVE.as_posix(),
            },
        ],
        "permission_impact": {
            "no_changes": ["config.toml", "existing custom agents", "plugins", "MCP declarations", "hooks", "provider settings"],
            "rollback_after_approval": "write only AGENTS.md and remove only the exact managed M9 skill",
        },
        "schema_version": M9_ROLLBACK_PACKET_SCHEMA_VERSION,
        "status": "PENDING_EXACT_USER_APPROVAL",
        "tooling_sha256": _rollback_tooling_sha256(),
        "unresolved_risks": [
            "rollback is a global mutation and requires a fresh explicit approval",
            "upstream-provider-retention-is-not-inferable-from-local-config",
            "the historical standalone backup helper is not authority for this rollback packet",
        ],
        "writer_version": M9_ROLLBACK_WRITER_VERSION,
    }
    packet["packet_digest"] = sha256_hex(packet)
    verify_m9_rollback_approval_packet(packet)
    return packet


def verify_m9_rollback_approval_packet(packet: Mapping[str, Any]) -> None:
    """Validate the closed, redacted rollback approval packet shape and digest."""

    required_keys = frozenset(
        (
            "approval_request",
            "backup",
            "codex_home",
            "created_at",
            "global_mutation",
            "initial_m9_packet_digest",
            "milestone",
            "operations",
            "packet_digest",
            "permission_impact",
            "schema_version",
            "status",
            "tooling_sha256",
            "unresolved_risks",
            "writer_version",
        )
    )
    if type(packet) is not dict or set(packet) != required_keys:
        raise SemanticValidationError("M9 rollback approval packet is invalid")
    if (
        packet["schema_version"] != M9_ROLLBACK_PACKET_SCHEMA_VERSION
        or packet["milestone"] != "M9"
        or packet["writer_version"] != M9_ROLLBACK_WRITER_VERSION
        or packet["status"] != "PENDING_EXACT_USER_APPROVAL"
        or packet["global_mutation"] is not True
        or not isinstance(packet["codex_home"], str)
        or not packet["codex_home"].startswith("/")
        or not isinstance(packet["created_at"], str)
        or not packet["created_at"].endswith("Z")
        or not isinstance(packet["approval_request"], str)
        or not packet["approval_request"]
    ):
        raise SemanticValidationError("M9 rollback approval packet identity is invalid")
    _require_sha256(packet["initial_m9_packet_digest"], "M9 rollback initial packet digest")
    _require_sha256(packet["packet_digest"], "M9 rollback packet digest")
    backup = packet["backup"]
    if type(backup) is not dict or set(backup) != {
        "agents_before_sha256", "backup_directory", "manifest_sha256", "rollback_helper_sha256"
    }:
        raise SemanticValidationError("M9 rollback backup metadata is invalid")
    if backup["backup_directory"] != M9_BACKUP_RELATIVE.as_posix():
        raise SemanticValidationError("M9 rollback backup directory is invalid")
    for field in ("agents_before_sha256", "manifest_sha256", "rollback_helper_sha256"):
        _require_sha256(backup[field], f"M9 rollback {field}")
    operations = packet["operations"]
    if type(operations) is not list or len(operations) != 2:
        raise SemanticValidationError("M9 rollback operations are invalid")
    expected_operations = (
        ("AGENTS.md", "restore-exact-before-state"),
        (M9_SKILL_RELATIVE.as_posix(), "remove-exact-managed-skill"),
    )
    for operation, (path, name) in zip(operations, expected_operations, strict=True):
        if type(operation) is not dict or set(operation) != {
            "after_mode", "after_sha256", "before_mode", "before_sha256", "operation", "path"
        }:
            raise SemanticValidationError("M9 rollback operation is invalid")
        if operation["path"] != path or operation["operation"] != name:
            raise SemanticValidationError("M9 rollback operation identity is invalid")
        _require_sha256(operation["before_sha256"], "M9 rollback operation before digest")
        _require_mode(operation["before_mode"], "M9 rollback operation before mode")
        if path == "AGENTS.md":
            _require_sha256(operation["after_sha256"], "M9 rollback AGENTS after digest")
            _require_mode(operation["after_mode"], "M9 rollback AGENTS after mode")
        elif operation["after_sha256"] != "absent" or operation["after_mode"] is not None:
            raise SemanticValidationError("M9 rollback skill removal operation is invalid")
    impact = packet["permission_impact"]
    if type(impact) is not dict or set(impact) != {"no_changes", "rollback_after_approval"}:
        raise SemanticValidationError("M9 rollback permission impact is invalid")
    if not isinstance(impact["no_changes"], list) or not isinstance(impact["rollback_after_approval"], str):
        raise SemanticValidationError("M9 rollback permission impact is invalid")
    tooling = packet["tooling_sha256"]
    if type(tooling) is not dict or set(tooling) != {path.as_posix() for path in M9_ROLLBACK_TOOLING_RELATIVE_PATHS}:
        raise SemanticValidationError("M9 rollback tooling binding is invalid")
    for digest in tooling.values():
        _require_sha256(digest, "M9 rollback tooling digest")
    if (
        type(packet["unresolved_risks"]) is not list
        or not packet["unresolved_risks"]
        or any(not isinstance(item, str) or not item for item in packet["unresolved_risks"])
    ):
        raise SemanticValidationError("M9 rollback unresolved risks are invalid")
    if sha256_hex(_without_rollback_packet_digest(packet)) != packet["packet_digest"]:
        raise IntegrityError("M9 rollback approval packet digest does not match")


def validate_m9_rollback_approval_packet_current(
    rollback_packet: Mapping[str, Any], initial_packet: Mapping[str, Any], codex_home: Path
) -> None:
    """Reject a rollback packet when its source packet, tooling, or state drifts."""

    verify_m9_rollback_approval_packet(rollback_packet)
    expected = build_m9_rollback_approval_packet(
        initial_packet,
        codex_home,
        created_at=rollback_packet["created_at"],
    )
    if expected["packet_digest"] != rollback_packet["packet_digest"]:
        raise IntegrityError("M9 rollback approval packet is stale or differs from current state")


def serialize_m9_rollback_approval_packet(packet: Mapping[str, Any]) -> bytes:
    verify_m9_rollback_approval_packet(packet)
    return (json.dumps(dict(packet), ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_m9_rollback_approval_packet(
    packet: Mapping[str, Any],
    relative_output: str = "artifacts/m9-rollback-approval-packet.json",
    *,
    replace_existing_packet_digest: str | None = None,
) -> Path:
    """Write a rollback packet only through an explicit local CAS replacement."""

    serialized = serialize_m9_rollback_approval_packet(packet)
    target = resolve_workspace_path(relative_output)
    artifacts_root = repository_root() / "artifacts"
    try:
        target.resolve(strict=False).relative_to(artifacts_root.resolve(strict=True))
    except ValueError as error:
        raise PathUnsafeError("M9 rollback packet output must stay under artifacts") from error
    if artifacts_root.is_symlink() or target.parent.is_symlink():
        raise PathUnsafeError("M9 rollback packet output directory is unsafe")
    if target.exists() or target.is_symlink():
        current, _ = _read_regular_file(target, maximum_bytes=1_048_576)
        if current == serialized:
            return target
        if replace_existing_packet_digest is None:
            raise IntegrityError("M9 rollback packet output already exists with different content")
        existing = load_strict_json(target)
        if type(existing) is not dict:
            raise IntegrityError("M9 existing rollback packet is invalid")
        verify_m9_rollback_approval_packet(existing)
        if existing["packet_digest"] != replace_existing_packet_digest:
            raise IntegrityError("M9 existing rollback packet does not match the explicit CAS digest")
        _atomic_write(target, serialized, 0o600)
        return target
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor: int | None = None
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if target.is_file() and not target.is_symlink() and target.read_bytes() == serialized:
            return target
        raise IntegrityError("M9 rollback packet output already exists with different content")
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise
    return target


def load_m9_rollback_approval_packet(
    relative_path: str = "artifacts/m9-rollback-approval-packet.json",
) -> dict[str, Any]:
    packet = load_strict_json(resolve_workspace_path(relative_path))
    if type(packet) is not dict:
        raise SemanticValidationError("M9 rollback approval packet must be an object")
    verify_m9_rollback_approval_packet(packet)
    return packet


def _target_by_path(packet: Mapping[str, Any], relative_path: str) -> Mapping[str, Any]:
    for target in packet["targets"]:
        if target["path"] == relative_path:
            return target
    raise IntegrityError("M9 packet target is missing")


def _assert_digest(path: Path, expected: str, *, expected_mode: int | None = None) -> None:
    contents, mode = _read_regular_file(path, maximum_bytes=1_048_576)
    if sha256_bytes(contents) != expected or (expected_mode is not None and mode != expected_mode):
        raise IntegrityError("M9 target digest or mode changed")


def apply_m9_packet(
    packet: Mapping[str, Any],
    codex_home: Path,
    *,
    approval_reference: str,
) -> dict[str, Any]:
    """Apply the additive M9 files after a caller binds a current approval.

    The caller is responsible for obtaining the real user approval. This API
    makes accidental execution harder by requiring a non-empty reference and
    revalidating every digest under an exclusive AGENTS lock.
    """

    if (
        not isinstance(approval_reference, str)
        or not approval_reference.strip()
        or packet.get("packet_digest") not in approval_reference
    ):
        raise AuthorityDeniedError("M9 apply requires an explicit approval reference")
    home = codex_home.resolve()
    agents_path = _contained(home, home / "AGENTS.md")
    with _agents_lock(agents_path):
        validate_m9_packet_current(packet, home)
        bundle = _load_staged_bundle()
        original_agents, agents_mode = _read_regular_file(agents_path)
        agents_after = _append_guidance(original_agents, bundle["AGENTS.append.md"])
        skill_path = _contained(home, home / M9_SKILL_RELATIVE)
        backup_root = _contained(home, home / M9_BACKUP_RELATIVE)
        _require_absent(skill_path.parent)
        _require_absent(backup_root.parent)

        agents_target = _target_by_path(packet, "AGENTS.md")
        skill_target = _target_by_path(packet, M9_SKILL_RELATIVE.as_posix())
        manifest_target = _target_by_path(packet, (M9_BACKUP_RELATIVE / "manifest.json").as_posix())
        rollback_target = _target_by_path(packet, (M9_BACKUP_RELATIVE / "rollback.py").as_posix())
        backup_agents_target = _target_by_path(packet, (M9_BACKUP_RELATIVE / "AGENTS.md").as_posix())
        if sha256_bytes(original_agents) != agents_target["before_sha256"]:
            raise IntegrityError("M9 AGENTS digest changed before apply")
        if sha256_bytes(agents_after) != agents_target["after_sha256"]:
            raise IntegrityError("M9 AGENTS after digest differs from packet")

        skill_contents = bundle[M9_SKILL_RELATIVE.as_posix()]
        rollback_contents = bundle["rollback.py"]
        backup_manifest = _backup_manifest_bytes(
            codex_home=home,
            agents_before_sha256=agents_target["before_sha256"],
            agents_after_sha256=agents_target["after_sha256"],
            agents_mode=agents_mode,
            skill_after_sha256=skill_target["after_sha256"],
            rollback_after_sha256=rollback_target["after_sha256"],
        )
        expected = (
            (skill_contents, skill_target["after_sha256"]),
            (rollback_contents, rollback_target["after_sha256"]),
            (backup_manifest, manifest_target["after_sha256"]),
            (original_agents, backup_agents_target["after_sha256"]),
        )
        if any(sha256_bytes(contents) != digest for contents, digest in expected):
            raise IntegrityError("M9 staged or backup content differs from packet")

        backup_root.parent.mkdir(mode=0o700)
        backup_root.mkdir(mode=0o700)
        applied_agents = False
        applied_skill = False
        try:
            _atomic_write(backup_root / "AGENTS.md", original_agents, agents_mode)
            _atomic_write(backup_root / "manifest.json", backup_manifest, 0o600)
            _atomic_write(backup_root / "rollback.py", rollback_contents, 0o700)
            skill_path.parent.mkdir(mode=0o700)
            _atomic_write(skill_path, skill_contents, 0o600)
            applied_skill = True
            _atomic_write(agents_path, agents_after, agents_mode)
            applied_agents = True
            _assert_digest(backup_root / "AGENTS.md", backup_agents_target["after_sha256"])
            _assert_digest(backup_root / "manifest.json", manifest_target["after_sha256"])
            _assert_digest(backup_root / "rollback.py", rollback_target["after_sha256"])
            _assert_digest(agents_path, agents_target["after_sha256"])
            _assert_digest(skill_path, skill_target["after_sha256"])
        except BaseException:
            if applied_agents:
                _atomic_write(agents_path, original_agents, agents_mode)
            if applied_skill and skill_path.exists() and sha256_bytes(skill_path.read_bytes()) == skill_target["after_sha256"]:
                skill_path.unlink()
            raise

    return {
        "approval_reference_present": True,
        "backup_tree_sha256": packet["backup"]["backup_tree_sha256"],
        "global_mutation": True,
        "packet_digest": packet["packet_digest"],
        "status": "APPLIED",
    }


def rollback_m9_packet(
    packet: Mapping[str, Any],
    codex_home: Path,
    *,
    rollback_approval_packet: Mapping[str, Any],
    approval_reference: str,
) -> dict[str, Any]:
    """Restore M9 only after a fresh, exact rollback approval is bound."""

    verify_m9_packet(packet)
    verify_m9_rollback_approval_packet(rollback_approval_packet)
    if rollback_approval_packet["initial_m9_packet_digest"] != packet.get("packet_digest"):
        raise IntegrityError("M9 rollback approval packet binds a different initial packet")
    if (
        not isinstance(approval_reference, str)
        or not approval_reference.strip()
        or rollback_approval_packet["packet_digest"] not in approval_reference
    ):
        raise AuthorityDeniedError("M9 rollback requires a fresh explicit rollback approval reference")
    home = _safe_m9_home(codex_home)
    required = _required_rollback_targets(packet, home)
    agents_path, _, _ = _approved_post_state_path(home, required["AGENTS.md"])
    with _agents_lock(agents_path):
        validate_m9_rollback_approval_packet_current(rollback_approval_packet, packet, home)
        state = _validated_rollback_state(packet, home)
        agents_target = required["AGENTS.md"]
        agents_contents = state["agents_contents"]
        agents_mode = state["agents_mode"]
        agents_path = state["agents_path"]
        backup_agents = state["backup_agents_contents"]
        skill_contents = state["skill_contents"]
        skill_mode = state["skill_mode"]
        skill_path = state["skill_path"]
        if not all(isinstance(value, (bytes, Path, int)) for value in (
            agents_contents,
            agents_mode,
            agents_path,
            backup_agents,
            skill_contents,
            skill_mode,
            skill_path,
        )):
            raise IntegrityError("M9 rollback state is invalid")

        agents_restored = False
        skill_removed = False
        try:
            _atomic_write(agents_path, backup_agents, int(agents_target["before_mode"]))
            agents_restored = True
            skill_path.unlink()
            skill_removed = True
            skill_path.parent.rmdir()
        except BaseException:
            if skill_removed and not skill_path.exists() and skill_path.parent.is_dir():
                _atomic_write(skill_path, skill_contents, skill_mode)
            if agents_restored:
                _atomic_write(agents_path, agents_contents, agents_mode)
            raise
        # Do not report a rollback unless both restored state and retained recovery
        # evidence read back exactly. A failure remains visible for manual recovery.
        _assert_digest(
            agents_path,
            agents_target["before_sha256"],
            expected_mode=_require_mode(agents_target["before_mode"], "M9 rollback AGENTS before mode"),
        )
        if not _is_absent(skill_path) or not _is_absent(skill_path.parent):
            raise IntegrityError("M9 managed skill remains after rollback")
        backup_agents_target = required[(M9_BACKUP_RELATIVE / "AGENTS.md").as_posix()]
        manifest_target = required[(M9_BACKUP_RELATIVE / "manifest.json").as_posix()]
        rollback_target = required[(M9_BACKUP_RELATIVE / "rollback.py").as_posix()]
        backup_agents_path = state["backup_agents_path"]
        manifest_path = state["manifest_path"]
        rollback_path = state["rollback_path"]
        if not all(isinstance(value, Path) for value in (backup_agents_path, manifest_path, rollback_path)):
            raise IntegrityError("M9 rollback backup state is invalid")
        _assert_digest(
            backup_agents_path,
            backup_agents_target["after_sha256"],
            expected_mode=_require_mode(backup_agents_target["after_mode"], "M9 rollback backup AGENTS mode"),
        )
        _assert_digest(
            manifest_path,
            manifest_target["after_sha256"],
            expected_mode=_require_mode(manifest_target["after_mode"], "M9 rollback manifest mode"),
        )
        _assert_digest(
            rollback_path,
            rollback_target["after_sha256"],
            expected_mode=_require_mode(rollback_target["after_mode"], "M9 rollback helper mode"),
        )

    return {
        "backup_preserved": True,
        "global_mutation": True,
        "initial_packet_digest": packet["packet_digest"],
        "rollback_packet_digest": rollback_approval_packet["packet_digest"],
        "status": "ROLLED_BACK",
    }
