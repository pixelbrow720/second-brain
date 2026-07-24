"""Disposable, synthetic-only private-runtime support for Activation V2.

This is deliberately not the future ``runtime/`` initializer. Every accepted
root lives under the Git-ignored ``artifacts/test-runs`` directory, binds a
fixture-only policy, and stores only bounded JSON documents that pass the V2
content barrier. It cannot initialize a user memory store or a global target.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
from typing import Any, Iterator, Mapping
import uuid

from .activation_v2 import validate_activation_v2_safe_content
from .canonical import sha256_hex
from .errors import ContentPolicyError, IntegrityError, PathUnsafeError, SemanticValidationError
from .jsonio import load_strict_json, loads_strict_json
from .schema_validation import parse_rfc3339_utc
from .workspace import repository_root


ACTIVATION_V2_RUNTIME_VERSION = "activation-v2-runtime/1"
POLICY_INPUTS_VERSION = 1
RUNTIME_MANIFEST_VERSION = 1
RUNTIME_BACKUP_VERSION = 1
SYNTHETIC_RUNTIME_MODE = "disposable_synthetic"
_RUNTIME_DIRECTORIES = ("registry", "global", "projects", "outbox", "receipts", "backups")
_RESTORABLE_DIRECTORIES = ("registry", "global", "projects", "outbox", "receipts")
_CAPTURE_DEFAULTS = frozenset(("UNSET", "OFF", "ASSISTED", "PROJECT_AUTO"))
_STORAGE_PROTECTIONS = frozenset(("UNSET", "ENCRYPTED_DISK", "APP_LEVEL_ENCRYPTION"))
_GRAPH_UI_CHOICES = frozenset(("UNSET", "GENERATED_SNAPSHOT", "LOCAL_WEB"))
_ROUTER_ENTRY_POINTS = frozenset(("UNSET", "CLI_SHADOW", "DESKTOP_INTEGRATION"))
_PROMOTION_REVIEW_MODES = frozenset(("UNSET", "PER_ITEM", "DAILY_BATCH", "MANUAL_ONLY"))
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SAFE_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_MAX_RUNTIME_FILE_BYTES = 1_048_576
_MAX_BACKUP_FILES = 256
_MAX_BACKUP_TOTAL_BYTES = 4 * _MAX_RUNTIME_FILE_BYTES
_RUNTIME_LOCK_GUARD = threading.Lock()
_RUNTIME_LOCKS: dict[str, threading.RLock] = {}


class ActivationV2RuntimeError(SemanticValidationError):
    """A disposable runtime violates the narrow local-only V2 contract."""


@dataclass(frozen=True)
class PolicyInputs:
    """Explicit policy inputs; production defaults are deliberately absent."""

    policy_version: int
    fixture_only: bool
    retention_days: int | None
    capture_default: str
    storage_protection: str
    graph_ui: str
    router_entry_point: str
    promotion_review_mode: str
    policy_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not PolicyInputs:
            raise ActivationV2RuntimeError("policy inputs are invalid")
        if self.policy_version != POLICY_INPUTS_VERSION or isinstance(self.policy_version, bool):
            raise ActivationV2RuntimeError("policy inputs version is unsupported")
        if type(self.fixture_only) is not bool:
            raise ActivationV2RuntimeError("policy fixture boundary is invalid")
        if self.retention_days is not None and (
            type(self.retention_days) is not int or not 1 <= self.retention_days <= 3660
        ):
            raise ActivationV2RuntimeError("policy retention is invalid")
        _require_choice(self.capture_default, _CAPTURE_DEFAULTS, "capture default")
        _require_choice(self.storage_protection, _STORAGE_PROTECTIONS, "storage protection")
        _require_choice(self.graph_ui, _GRAPH_UI_CHOICES, "graph UI")
        _require_choice(self.router_entry_point, _ROUTER_ENTRY_POINTS, "router entry point")
        _require_choice(self.promotion_review_mode, _PROMOTION_REVIEW_MODES, "promotion review mode")
        expected = sha256_hex(self._digest_input())
        if self.policy_digest and self.policy_digest != expected:
            raise ActivationV2RuntimeError("policy digest does not match")
        object.__setattr__(self, "policy_digest", expected)

    @classmethod
    def unresolved(cls) -> "PolicyInputs":
        """Return the explicit no-default state for a real future rollout."""

        return cls(
            policy_version=POLICY_INPUTS_VERSION,
            fixture_only=False,
            retention_days=None,
            capture_default="UNSET",
            storage_protection="UNSET",
            graph_ui="UNSET",
            router_entry_point="UNSET",
            promotion_review_mode="UNSET",
        )

    @classmethod
    def fixture_recommended_defaults(
        cls,
        *,
        capture_default: str = "ASSISTED",
        graph_ui: str = "GENERATED_SNAPSHOT",
        router_entry_point: str = "CLI_SHADOW",
        promotion_review_mode: str = "PER_ITEM",
    ) -> "PolicyInputs":
        """Return recommendations only for disposable synthetic fixture runs."""

        return cls(
            policy_version=POLICY_INPUTS_VERSION,
            fixture_only=True,
            retention_days=30,
            capture_default=capture_default,
            storage_protection="ENCRYPTED_DISK",
            graph_ui=graph_ui,
            router_entry_point=router_entry_point,
            promotion_review_mode=promotion_review_mode,
        )

    @classmethod
    def from_value(cls, value: object) -> "PolicyInputs":
        if isinstance(value, cls):
            return value
        if type(value) is not dict:
            raise ActivationV2RuntimeError("policy inputs are invalid")
        expected = {
            "policy_version",
            "fixture_only",
            "retention_days",
            "capture_default",
            "storage_protection",
            "graph_ui",
            "router_entry_point",
            "promotion_review_mode",
            "policy_digest",
        }
        if set(value) != expected:
            raise ActivationV2RuntimeError("policy inputs have unsupported fields")
        try:
            return cls(**value)
        except (TypeError, ValueError, ActivationV2RuntimeError) as error:
            raise ActivationV2RuntimeError("policy inputs are invalid") from error

    def _digest_input(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "fixture_only": self.fixture_only,
            "retention_days": self.retention_days,
            "capture_default": self.capture_default,
            "storage_protection": self.storage_protection,
            "graph_ui": self.graph_ui,
            "router_entry_point": self.router_entry_point,
            "promotion_review_mode": self.promotion_review_mode,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._digest_input(), "policy_digest": self.policy_digest}

    def require_resolved(self, phase: str) -> None:
        """Reject a phase when its real policy decision has not been supplied."""

        if phase not in {"A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8"}:
            raise ActivationV2RuntimeError("Activation V2 phase is invalid")
        required = {"retention_days", "capture_default", "storage_protection"}
        if phase in {"A5", "A6", "A7", "A8"}:
            required.update({"graph_ui", "router_entry_point"})
        if phase in {"A7", "A8"}:
            required.add("promotion_review_mode")
        unresolved = [
            field_name
            for field_name in sorted(required)
            if getattr(self, field_name) is None or getattr(self, field_name) == "UNSET"
        ]
        if unresolved:
            raise ActivationV2RuntimeError(
                "policy decisions are required before " + phase + ": " + ", ".join(unresolved)
            )


@dataclass(frozen=True)
class RuntimeManifest:
    """Metadata-only manifest for one disposable synthetic runtime."""

    schema_version: int
    runtime_id: str
    mode: str
    created_at: str
    policy: PolicyInputs
    directories: tuple[str, ...]
    runtime_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not RuntimeManifest:
            raise ActivationV2RuntimeError("runtime manifest is invalid")
        if self.schema_version != RUNTIME_MANIFEST_VERSION or isinstance(self.schema_version, bool):
            raise ActivationV2RuntimeError("runtime manifest version is unsupported")
        if not isinstance(self.runtime_id, str) or not self.runtime_id.startswith("runtime:"):
            raise ActivationV2RuntimeError("runtime identity is invalid")
        if _UUID.fullmatch(self.runtime_id.removeprefix("runtime:")) is None:
            raise ActivationV2RuntimeError("runtime identity is invalid")
        if self.mode != SYNTHETIC_RUNTIME_MODE:
            raise ActivationV2RuntimeError("runtime mode is not disposable synthetic")
        _require_timestamp(self.created_at, "runtime manifest timestamp")
        if type(self.policy) is not PolicyInputs or not self.policy.fixture_only:
            raise ActivationV2RuntimeError("runtime manifest must bind a fixture-only policy")
        if self.directories != _RUNTIME_DIRECTORIES:
            raise ActivationV2RuntimeError("runtime manifest directories are invalid")
        expected = sha256_hex(self._digest_input())
        if self.runtime_digest and self.runtime_digest != expected:
            raise ActivationV2RuntimeError("runtime manifest digest does not match")
        object.__setattr__(self, "runtime_digest", expected)

    def _digest_input(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "runtime_id": self.runtime_id,
            "mode": self.mode,
            "created_at": self.created_at,
            "policy": self.policy.to_dict(),
            "directories": list(self.directories),
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._digest_input(), "runtime_digest": self.runtime_digest}

    @classmethod
    def from_value(cls, value: object) -> "RuntimeManifest":
        if isinstance(value, cls):
            return value
        if type(value) is not dict:
            raise ActivationV2RuntimeError("runtime manifest is invalid")
        expected = {
            "schema_version",
            "runtime_id",
            "mode",
            "created_at",
            "policy",
            "directories",
            "runtime_digest",
        }
        if set(value) != expected or not isinstance(value["directories"], list):
            raise ActivationV2RuntimeError("runtime manifest has unsupported fields")
        try:
            return cls(
                schema_version=value["schema_version"],
                runtime_id=value["runtime_id"],
                mode=value["mode"],
                created_at=value["created_at"],
                policy=PolicyInputs.from_value(value["policy"]),
                directories=tuple(value["directories"]),
                runtime_digest=value["runtime_digest"],
            )
        except (TypeError, ValueError, ActivationV2RuntimeError) as error:
            raise ActivationV2RuntimeError("runtime manifest is invalid") from error


@dataclass(frozen=True)
class DisposableRuntime:
    """Validated test-only runtime handle; its root is never a production path."""

    root: Path
    manifest: RuntimeManifest

    def path(self, relative_path: str | Path) -> Path:
        """Return a contained path for diagnostics; use typed helpers to write."""

        return _contained_runtime_path(self.root, relative_path)


@dataclass(frozen=True)
class RuntimeBackup:
    """Metadata-only reference to a bounded synthetic runtime snapshot."""

    backup_id: str
    relative_path: str
    created_at: str
    backup_digest: str

    def __post_init__(self) -> None:
        if type(self) is not RuntimeBackup:
            raise ActivationV2RuntimeError("runtime backup reference is invalid")
        if (
            not isinstance(self.backup_id, str)
            or not self.backup_id.startswith("backup:")
            or _UUID.fullmatch(self.backup_id.removeprefix("backup:")) is None
        ):
            raise ActivationV2RuntimeError("runtime backup identity is invalid")
        expected_path = f"backups/{self.backup_id.removeprefix('backup:')}.json"
        if self.relative_path != expected_path:
            raise ActivationV2RuntimeError("runtime backup path is invalid")
        _require_timestamp(self.created_at, "runtime backup timestamp")
        if not isinstance(self.backup_digest, str) or _HASH.fullmatch(self.backup_digest) is None:
            raise ActivationV2RuntimeError("runtime backup digest is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "backup_id": self.backup_id,
            "relative_path": self.relative_path,
            "created_at": self.created_at,
            "backup_digest": self.backup_digest,
        }


def initialize_disposable_runtime(
    root: str | Path,
    policy: PolicyInputs | Mapping[str, Any],
    *,
    created_at: str | None = None,
) -> DisposableRuntime:
    """Create empty A1 directories only beneath the ignored test-runs root."""

    selected_policy = PolicyInputs.from_value(policy)
    if not selected_policy.fixture_only:
        raise ActivationV2RuntimeError("A1 only permits disposable synthetic policy inputs")
    selected_policy.require_resolved("A1")
    runtime_root = _validate_new_disposable_root(root)
    timestamp = created_at or _now_rfc3339()
    _require_timestamp(timestamp, "runtime created_at")

    runtime_root.mkdir(mode=0o700)
    try:
        _require_private_directory(runtime_root, "disposable runtime root")
        for directory in _RUNTIME_DIRECTORIES:
            child = runtime_root / directory
            child.mkdir(mode=0o700)
            _require_private_directory(child, "runtime directory")
        manifest = RuntimeManifest(
            schema_version=RUNTIME_MANIFEST_VERSION,
            runtime_id=f"runtime:{uuid.uuid4()}",
            mode=SYNTHETIC_RUNTIME_MODE,
            created_at=timestamp,
            policy=selected_policy,
            directories=_RUNTIME_DIRECTORIES,
        )
        _write_json_at_root(runtime_root, Path("manifest.json"), manifest.to_dict())
        handle = DisposableRuntime(runtime_root, manifest)
        assert_disposable_runtime_not_tracked(handle)
        return handle
    except Exception:
        # The root is known to be a fresh, contained disposable directory.
        _remove_disposable_tree(runtime_root)
        raise


def load_disposable_runtime(root: str | Path) -> DisposableRuntime:
    """Load only a valid existing disposable runtime below test-runs."""

    runtime_root = _validate_existing_disposable_root(root)
    manifest_path = _contained_runtime_path(runtime_root, "manifest.json")
    try:
        manifest = RuntimeManifest.from_value(load_strict_json(manifest_path))
    except (OSError, ValueError, ActivationV2RuntimeError) as error:
        raise IntegrityError("disposable runtime manifest is unavailable or invalid") from error
    _require_private_file(manifest_path, "runtime manifest")
    for directory in manifest.directories:
        _require_private_directory(_contained_runtime_path(runtime_root, directory), "runtime directory")
    handle = DisposableRuntime(runtime_root, manifest)
    assert_disposable_runtime_not_tracked(handle)
    return handle


def write_disposable_json(
    runtime: DisposableRuntime,
    relative_path: str | Path,
    document: Mapping[str, Any],
) -> None:
    """Write one bounded safe JSON object into a typed synthetic runtime area."""

    handle = _require_runtime(runtime)
    relative = _validate_runtime_data_path(relative_path, allow_backup=False)
    if type(document) is not dict:
        raise ActivationV2RuntimeError("runtime document must be a JSON object")
    _validate_runtime_document(document)
    _write_json_at_root(handle.root, relative, document)


def read_disposable_json(runtime: DisposableRuntime, relative_path: str | Path) -> dict[str, Any]:
    """Read a safe bounded JSON object from an explicitly named runtime path."""

    handle = _require_runtime(runtime)
    relative = _validate_runtime_data_path(relative_path, allow_backup=False)
    path = _contained_runtime_path(handle.root, relative)
    try:
        _require_private_file(path, "runtime document")
        document = load_strict_json(path)
    except (OSError, ValueError, PathUnsafeError) as error:
        raise IntegrityError("runtime document is unavailable or invalid") from error
    if type(document) is not dict:
        raise IntegrityError("runtime document is invalid")
    try:
        _validate_runtime_document(document)
    except (ActivationV2RuntimeError, ContentPolicyError) as error:
        raise IntegrityError("runtime document violates the local content boundary") from error
    return document


def disposable_json_exists(runtime: DisposableRuntime, relative_path: str | Path) -> bool:
    """Return whether one valid typed runtime document exists without scanning."""

    handle = _require_runtime(runtime)
    relative = _validate_runtime_data_path(relative_path, allow_backup=False)
    path = _contained_runtime_path(handle.root, relative)
    if not path.exists():
        return False
    _require_private_file(path, "runtime document")
    return True


def remove_disposable_json(runtime: DisposableRuntime, relative_path: str | Path) -> None:
    """Remove one exact validated synthetic JSON document from a disposable root."""

    handle = _require_runtime(runtime)
    relative = _validate_runtime_data_path(relative_path, allow_backup=False)
    path = _contained_runtime_path(handle.root, relative)
    try:
        _require_private_file(path, "runtime document")
        path.unlink()
    except OSError as error:
        raise ActivationV2RuntimeError("runtime document removal failed") from error


@contextmanager
def exclusive_disposable_runtime_lock(runtime: DisposableRuntime) -> Iterator[DisposableRuntime]:
    """Serialize synthetic state transitions using the immutable manifest inode."""

    handle = _require_runtime(runtime)
    manifest_path = _contained_runtime_path(handle.root, "manifest.json")
    with _runtime_lock_for(handle.root):
        descriptor: int | None = None
        try:
            _require_private_file(manifest_path, "runtime manifest")
            descriptor = os.open(manifest_path, os.O_RDONLY)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield handle
        except OSError as error:
            raise ActivationV2RuntimeError("disposable runtime lock is unavailable") from error
        finally:
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)


def _runtime_lock_for(root: Path) -> threading.RLock:
    """Return the process-local half of the disposable runtime lock."""

    key = os.fspath(root)
    with _RUNTIME_LOCK_GUARD:
        return _RUNTIME_LOCKS.setdefault(key, threading.RLock())


def create_runtime_backup(runtime: DisposableRuntime, *, created_at: str | None = None) -> RuntimeBackup:
    """Snapshot safe synthetic JSON without following links or copying backups."""

    handle = _require_runtime(runtime)
    timestamp = created_at or _now_rfc3339()
    _require_timestamp(timestamp, "backup timestamp")
    files = _snapshot_runtime_files(handle.root)
    backup_id = f"backup:{uuid.uuid4()}"
    payload: dict[str, object] = {
        "schema_version": RUNTIME_BACKUP_VERSION,
        "backup_id": backup_id,
        "runtime_digest": handle.manifest.runtime_digest,
        "created_at": timestamp,
        "files": files,
    }
    digest = sha256_hex(payload)
    payload["backup_digest"] = digest
    relative_path = Path("backups") / f"{backup_id.removeprefix('backup:')}.json"
    _write_backup_payload(handle.root, relative_path, payload)
    return RuntimeBackup(
        backup_id=backup_id,
        relative_path=relative_path.as_posix(),
        created_at=timestamp,
        backup_digest=digest,
    )


def restore_runtime_backup(runtime: DisposableRuntime, backup: RuntimeBackup | Mapping[str, Any]) -> None:
    """Restore a validated synthetic snapshot while retaining its backup file."""

    handle = _require_runtime(runtime)
    selected_backup = _coerce_backup(backup)
    backup_path = _contained_runtime_path(handle.root, selected_backup.relative_path)
    try:
        _require_private_file(backup_path, "runtime backup")
        payload = load_strict_json(backup_path)
    except (OSError, ValueError, PathUnsafeError) as error:
        raise IntegrityError("runtime backup is unavailable") from error
    files = _validate_backup_payload(handle, selected_backup, payload)
    # All input validation happens before any durable synthetic state is cleared.
    _restore_runtime_files(handle.root, files)
    restored = load_disposable_runtime(handle.root)
    if restored.manifest.runtime_digest != handle.manifest.runtime_digest:
        raise IntegrityError("runtime restore did not recover its manifest")


def assert_disposable_runtime_not_tracked(runtime: DisposableRuntime) -> None:
    """Fail when the disposable root is not covered by repository ignores."""

    handle = _require_runtime_without_reload(runtime)
    root = repository_root()
    try:
        relative = handle.root.relative_to(root)
    except ValueError as error:
        raise PathUnsafeError("disposable runtime is outside the repository test boundary") from error
    completed = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", relative.as_posix()],
        cwd=root,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        raise PathUnsafeError("disposable runtime is not ignored by Git")


def runtime_health(runtime: DisposableRuntime) -> dict[str, object]:
    """Return metadata-only health; no authority object or task text is read."""

    handle = _require_runtime(runtime)
    return {
        "runtime_version": ACTIVATION_V2_RUNTIME_VERSION,
        "runtime_id": handle.manifest.runtime_id,
        "mode": handle.manifest.mode,
        "fixture_only": handle.manifest.policy.fixture_only,
        "runtime_digest": handle.manifest.runtime_digest,
        "directories": list(handle.manifest.directories),
        "authority_store_opened": False,
        "global_mutation": False,
    }


def _require_choice(value: object, choices: frozenset[str], label: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ActivationV2RuntimeError(f"{label} is invalid")
    return value


def _require_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ActivationV2RuntimeError(f"{label} is invalid")
    try:
        parse_rfc3339_utc(value)
    except Exception as error:
        raise ActivationV2RuntimeError(f"{label} is invalid") from error
    return value


def _now_rfc3339() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _test_runs_root() -> Path:
    return repository_root() / "artifacts" / "test-runs"


def _validate_new_disposable_root(value: str | Path) -> Path:
    path = _absolute_path(value, "disposable runtime root")
    _require_under_test_runs(path)
    _reject_symlink_components(path, allow_missing_leaf=True)
    if path.exists():
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PathUnsafeError("disposable runtime root is unsafe")
        if any(path.iterdir()):
            raise PathUnsafeError("disposable runtime root must be empty")
        path.rmdir()
    parent = path.parent
    _require_directory(parent, "disposable runtime parent")
    return path


def _validate_existing_disposable_root(value: str | Path) -> Path:
    path = _absolute_path(value, "disposable runtime root")
    _require_under_test_runs(path)
    _reject_symlink_components(path)
    _require_private_directory(path, "disposable runtime root")
    return path


def _absolute_path(value: str | Path, label: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise PathUnsafeError(f"{label} is invalid")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise PathUnsafeError(f"{label} must be absolute")
    try:
        return Path(os.path.abspath(os.fspath(candidate)))
    except (TypeError, ValueError) as error:
        raise PathUnsafeError(f"{label} is invalid") from error


def _require_under_test_runs(path: Path) -> None:
    test_root = _test_runs_root()
    try:
        path.relative_to(test_root)
    except ValueError as error:
        raise PathUnsafeError("disposable runtime must be below artifacts/test-runs") from error
    if path == test_root:
        raise PathUnsafeError("disposable runtime root cannot be the test-runs parent")


def _reject_symlink_components(path: Path, *, allow_missing_leaf: bool = False) -> None:
    root = Path(path.anchor)
    parts = path.parts[1:]
    current = root
    for index, component in enumerate(parts):
        if component in {"", ".", ".."}:
            raise PathUnsafeError("runtime path has unsafe components")
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError as error:
            if allow_missing_leaf:
                return
            raise PathUnsafeError("runtime path is unavailable") from error
        except OSError as error:
            raise PathUnsafeError("runtime path cannot be inspected") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise PathUnsafeError("runtime path crosses a symlink")
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise PathUnsafeError("runtime path has a non-directory parent")


def _require_private_directory(path: Path, label: str) -> None:
    _require_directory(path, label)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise PathUnsafeError(f"{label} is unavailable") from error
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PathUnsafeError(f"{label} is not private")


def _require_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise PathUnsafeError(f"{label} is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PathUnsafeError(f"{label} is unsafe")


def _require_private_file(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise PathUnsafeError(f"{label} is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PathUnsafeError(f"{label} is unsafe")
    if metadata.st_size > _MAX_RUNTIME_FILE_BYTES or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PathUnsafeError(f"{label} is not private or bounded")


def _contained_runtime_path(root: Path, relative_path: str | Path) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise PathUnsafeError("runtime relative path is unsafe")
    candidate = root / relative
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise PathUnsafeError("runtime path escapes its root") from error
    _reject_symlink_components(candidate, allow_missing_leaf=True)
    return candidate


def _validate_runtime_data_path(value: str | Path, *, allow_backup: bool) -> Path:
    relative = Path(value)
    if relative.is_absolute() or len(relative.parts) < 2 or len(relative.parts) > 8:
        raise PathUnsafeError("runtime data path is unsafe")
    if any(part in {"", ".", ".."} or _SAFE_SEGMENT.fullmatch(part) is None for part in relative.parts[:-1]):
        raise PathUnsafeError("runtime data path is unsafe")
    filename = relative.name
    if not filename.endswith(".json") or _SAFE_SEGMENT.fullmatch(filename[:-5]) is None:
        raise PathUnsafeError("runtime data path is unsafe")
    top = relative.parts[0]
    if top not in _RUNTIME_DIRECTORIES or (top == "backups" and not allow_backup):
        raise PathUnsafeError("runtime data path is not an allowed area")
    if top == "projects" and (len(relative.parts) < 3 or _PROJECT_ID.fullmatch(relative.parts[1]) is None):
        raise PathUnsafeError("project runtime path is unsafe")
    return relative


def _validate_runtime_document(document: Mapping[str, Any]) -> None:
    try:
        serialized = json.dumps(document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ActivationV2RuntimeError("runtime document is not JSON-safe") from error
    if len(serialized.encode("utf-8")) > _MAX_RUNTIME_FILE_BYTES:
        raise ActivationV2RuntimeError("runtime document exceeds its bound")
    validate_activation_v2_safe_content(document)


def _ensure_runtime_parent(root: Path, relative: Path) -> Path:
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists():
            _require_private_directory(current, "runtime write parent")
        else:
            current.mkdir(mode=0o700)
            _require_private_directory(current, "runtime write parent")
    return current


def _write_json_at_root(root: Path, relative: Path, document: Mapping[str, Any]) -> None:
    if relative != Path("manifest.json"):
        _validate_runtime_data_path(relative, allow_backup=False)
    _validate_runtime_document(document)
    destination = _contained_runtime_path(root, relative)
    _ensure_runtime_parent(root, relative)
    payload = json.dumps(document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _atomic_write_bytes(root, relative, payload)


def _write_backup_payload(root: Path, relative: Path, payload: Mapping[str, Any]) -> None:
    _validate_runtime_data_path(relative, allow_backup=True)
    destination = _contained_runtime_path(root, relative)
    _ensure_runtime_parent(root, relative)
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_RUNTIME_FILE_BYTES:
        raise ActivationV2RuntimeError("runtime backup exceeds its bound")
    _atomic_write_bytes(root, relative, encoded)


def _atomic_write_bytes(root: Path, relative_path: Path, payload: bytes) -> None:
    if len(payload) > _MAX_RUNTIME_FILE_BYTES:
        raise ActivationV2RuntimeError("runtime write exceeds its bound")
    destination = _contained_runtime_path(root, relative_path)
    _require_private_directory(destination.parent, "runtime write parent")
    temporary = destination.parent / f".{destination.name}.tmp-{uuid.uuid4()}"
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    except OSError as error:
        raise ActivationV2RuntimeError("runtime write failed") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _snapshot_runtime_files(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    total_bytes = 0
    for current_root, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_root)
        safe_directories: list[str] = []
        for directory in directories:
            candidate = current / directory
            _require_private_directory(candidate, "runtime backup directory")
            if current == root and directory == "backups":
                continue
            safe_directories.append(directory)
        directories[:] = safe_directories
        for filename in filenames:
            candidate = current / filename
            _require_private_file(candidate, "runtime backup file")
            relative = candidate.relative_to(root)
            if relative == Path("manifest.json"):
                document = _read_safe_json_file(candidate)
                RuntimeManifest.from_value(document)
            else:
                _validate_runtime_data_path(relative, allow_backup=False)
                _read_safe_json_file(candidate)
            raw = candidate.read_bytes()
            total_bytes += len(raw)
            if len(files) >= _MAX_BACKUP_FILES or total_bytes > _MAX_BACKUP_TOTAL_BYTES:
                raise ActivationV2RuntimeError("runtime backup exceeds its aggregate bound")
            files[relative.as_posix()] = base64.b64encode(raw).decode("ascii")
    if "manifest.json" not in files:
        raise IntegrityError("runtime backup lacks its manifest")
    return dict(sorted(files.items()))


def _read_safe_json_file(path: Path) -> dict[str, Any]:
    try:
        document = load_strict_json(path)
    except (OSError, ValueError) as error:
        raise IntegrityError("runtime JSON file is invalid") from error
    if type(document) is not dict:
        raise IntegrityError("runtime JSON file is invalid")
    try:
        _validate_runtime_document(document)
    except (ActivationV2RuntimeError, ContentPolicyError) as error:
        raise IntegrityError("runtime JSON file violates the content boundary") from error
    return document


def _validate_backup_payload(
    runtime: DisposableRuntime,
    backup: RuntimeBackup,
    payload: object,
) -> dict[str, bytes]:
    if type(payload) is not dict:
        raise IntegrityError("runtime backup is invalid")
    expected = {"schema_version", "backup_id", "runtime_digest", "created_at", "files", "backup_digest"}
    if set(payload) != expected or payload.get("schema_version") != RUNTIME_BACKUP_VERSION:
        raise IntegrityError("runtime backup is invalid")
    supplied_digest = payload["backup_digest"]
    digest_input = {key: value for key, value in payload.items() if key != "backup_digest"}
    if supplied_digest != backup.backup_digest or sha256_hex(digest_input) != backup.backup_digest:
        raise IntegrityError("runtime backup digest does not match")
    if payload["backup_id"] != backup.backup_id or payload["runtime_digest"] != runtime.manifest.runtime_digest:
        raise IntegrityError("runtime backup identity does not match")
    _require_timestamp(payload["created_at"], "runtime backup timestamp")
    files = payload["files"]
    if type(files) is not dict or not files:
        raise IntegrityError("runtime backup files are invalid")
    decoded: dict[str, bytes] = {}
    total_bytes = 0
    for raw_relative, encoded in sorted(files.items()):
        if not isinstance(raw_relative, str) or not isinstance(encoded, str):
            raise IntegrityError("runtime backup file record is invalid")
        relative = Path(raw_relative)
        if relative == Path("manifest.json"):
            pass
        else:
            _validate_runtime_data_path(relative, allow_backup=False)
        try:
            content = base64.b64decode(encoded.encode("ascii"), validate=True)
        except ValueError as error:
            raise IntegrityError("runtime backup payload is invalid") from error
        total_bytes += len(content)
        if len(decoded) >= _MAX_BACKUP_FILES or len(content) > _MAX_RUNTIME_FILE_BYTES or total_bytes > _MAX_BACKUP_TOTAL_BYTES:
            raise IntegrityError("runtime backup exceeds its bound")
        decoded[raw_relative] = content
    manifest_bytes = decoded.get("manifest.json")
    if manifest_bytes is None:
        raise IntegrityError("runtime backup lacks its manifest")
    try:
        manifest_value = loads_strict_json(manifest_bytes.decode("utf-8"))
        manifest = RuntimeManifest.from_value(manifest_value)
    except (UnicodeDecodeError, ValueError, ActivationV2RuntimeError) as error:
        raise IntegrityError("runtime backup manifest is invalid") from error
    if manifest.runtime_digest != runtime.manifest.runtime_digest:
        raise IntegrityError("runtime backup belongs to a different runtime")
    for raw_relative, content in decoded.items():
        if raw_relative == "manifest.json":
            continue
        _validate_backup_document(content)
    return decoded


def _validate_backup_document(content: bytes) -> None:
    if len(content) > _MAX_RUNTIME_FILE_BYTES:
        raise IntegrityError("runtime backup payload exceeds its bound")
    try:
        document = loads_strict_json(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise IntegrityError("runtime backup payload is not safe JSON") from error
    if type(document) is not dict:
        raise IntegrityError("runtime backup payload is not a JSON object")
    try:
        _validate_runtime_document(document)
    except (ActivationV2RuntimeError, ContentPolicyError) as error:
        raise IntegrityError("runtime backup payload violates the content boundary") from error


def _restore_runtime_files(root: Path, files: Mapping[str, bytes]) -> None:
    for directory in _RESTORABLE_DIRECTORIES:
        _clear_directory(_contained_runtime_path(root, directory))
    for raw_relative, content in sorted(files.items()):
        if raw_relative == "manifest.json":
            continue
        relative = _validate_runtime_data_path(raw_relative, allow_backup=False)
        _ensure_runtime_parent(root, relative)
        _atomic_write_bytes(root, relative, content)


def _clear_directory(root: Path) -> None:
    _require_private_directory(root, "runtime restore directory")
    for child in sorted(root.iterdir(), key=lambda item: item.name, reverse=True):
        metadata = child.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise PathUnsafeError("runtime restore encountered a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            _clear_directory(child)
            child.rmdir()
        elif stat.S_ISREG(metadata.st_mode):
            child.unlink()
        else:
            raise PathUnsafeError("runtime restore encountered an unsafe entry")


def _remove_disposable_tree(root: Path) -> None:
    if not root.exists():
        return
    _clear_directory(root)
    root.rmdir()


def _require_runtime_without_reload(value: object) -> DisposableRuntime:
    if type(value) is not DisposableRuntime:
        raise ActivationV2RuntimeError("disposable runtime handle is invalid")
    return value


def _require_runtime(value: object) -> DisposableRuntime:
    handle = _require_runtime_without_reload(value)
    return load_disposable_runtime(handle.root)


def _coerce_backup(value: RuntimeBackup | Mapping[str, Any]) -> RuntimeBackup:
    if isinstance(value, RuntimeBackup):
        return value
    if type(value) is not dict or set(value) != {"backup_id", "relative_path", "created_at", "backup_digest"}:
        raise ActivationV2RuntimeError("runtime backup reference is invalid")
    try:
        return RuntimeBackup(**value)
    except (TypeError, ValueError, ActivationV2RuntimeError) as error:
        raise ActivationV2RuntimeError("runtime backup reference is invalid") from error
