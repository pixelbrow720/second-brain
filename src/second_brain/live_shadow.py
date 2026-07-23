"""Redacted, fresh-session M9 PUBLIC read-only shadow evidence.

The M8 evaluator deliberately does not make provider calls.  This module is a
separate M9 boundary: it invokes a new ephemeral Codex CLI session for each
approved public probe and writes only bounded counters, statuses, and digests.
It does not treat a successful CLI response as authenticated route telemetry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence

from .canonical import canonical_jcs_bytes, sha256_bytes, sha256_hex
from .errors import AuthorityDeniedError, IntegrityError, SemanticValidationError
from .global_rollout import load_m9_packet, verify_m9_packet
from .workspace import repository_root, resolve_workspace_path


M9_SHADOW_SCHEMA_VERSION = 1
M9_SHADOW_WRITER_VERSION = "m9-live-shadow/1"
M9_SHADOW_RECEIPT_NAME = "m9-initial-shadow-report.json"
M9_SHADOW_PROVIDER = "cx_account"
M9_SHADOW_REQUEST_COUNT = 50
M9_SHADOW_TIMEOUT_SECONDS = 90
M9_SHADOW_EXPECTED_RESPONSE = "M9_PUBLIC_SHADOW_OK"

_HASH = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_REASON = re.compile(r"^[A-Z][A-Z0-9_]{2,95}$")
_MAX_EVENT_BYTES = 1_048_576
_MAX_EVENT_COUNT = 128
_TOOL_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "function_call",
        "mcp_tool_call",
        "tool_call",
        "web_search_call",
    }
)
_AGENT_MESSAGE_ITEM_TYPES = frozenset({"agent_message", "assistant_message"})

_PUBLIC_PROMPT_TEMPLATE = (
    "M9 synthetic PUBLIC read-only shadow {ordinal}/50. "
    "Return exactly M9_PUBLIC_SHADOW_OK. Do not invoke tools, commands, MCPs, "
    "browsers, skills, files, memory, or external actions."
)
_PROMPT_TEMPLATE_DIGEST = sha256_bytes(_PUBLIC_PROMPT_TEMPLATE.encode("utf-8"))
_EXPECTED_RESPONSE_DIGEST = sha256_bytes(M9_SHADOW_EXPECTED_RESPONSE.encode("utf-8"))

ProcessRunner = Callable[[Sequence[str], Path, int], subprocess.CompletedProcess[bytes]]


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise SemanticValidationError(f"{label} is invalid")
    return value


def _require_reason_codes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 8:
        raise SemanticValidationError("shadow reason codes are invalid")
    result = tuple(value)
    if len(set(result)) != len(result) or any(not isinstance(item, str) or _REASON.fullmatch(item) is None for item in result):
        raise SemanticValidationError("shadow reason codes are invalid")
    return result


@dataclass(frozen=True)
class ShadowRequestResult:
    """A redacted result for one real Codex CLI request."""

    ordinal: int
    status: str
    duration_ms: int
    exit_code: int | None
    event_count: int
    agent_message_count: int
    tool_call_count: int
    response_digest: str | None
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self) is not ShadowRequestResult:
            raise SemanticValidationError("shadow request result is invalid")
        if not isinstance(self.ordinal, int) or not 1 <= self.ordinal <= M9_SHADOW_REQUEST_COUNT:
            raise SemanticValidationError("shadow ordinal is invalid")
        if self.status not in {"PASS", "FAIL"}:
            raise SemanticValidationError("shadow request status is invalid")
        for value, label, maximum in (
            (self.duration_ms, "shadow duration", M9_SHADOW_TIMEOUT_SECONDS * 1_000),
            (self.event_count, "shadow event count", _MAX_EVENT_COUNT),
            (self.agent_message_count, "shadow message count", 8),
            (self.tool_call_count, "shadow tool count", 8),
        ):
            if not isinstance(value, int) or value < 0 or value > maximum:
                raise SemanticValidationError(f"{label} is invalid")
        if self.exit_code is not None and (
            not isinstance(self.exit_code, int) or self.exit_code < -255 or self.exit_code > 255
        ):
            raise SemanticValidationError("shadow exit code is invalid")
        if self.response_digest is not None:
            _require_hash(self.response_digest, "shadow response digest")
        reasons = _require_reason_codes(list(self.reason_codes))
        if self.status == "PASS":
            if (
                self.exit_code != 0
                or self.agent_message_count != 1
                or self.tool_call_count != 0
                or self.response_digest != _EXPECTED_RESPONSE_DIGEST
                or reasons
            ):
                raise SemanticValidationError("passing shadow request is invalid")
        elif not reasons:
            raise SemanticValidationError("failed shadow request lacks a reason")
        object.__setattr__(self, "reason_codes", reasons)

    def to_dict(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "event_count": self.event_count,
            "agent_message_count": self.agent_message_count,
            "tool_call_count": self.tool_call_count,
            "response_digest": self.response_digest,
            "reason_codes": list(self.reason_codes),
        }


def _command_for_shadow(prompt: str, scratch_directory: Path) -> list[str]:
    """Build a fixed, no-shell command that cannot inherit a project workspace."""

    return [
        "codex",
        "exec",
        "--ephemeral",
        "--json",
        "--color",
        "never",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--cd",
        str(scratch_directory),
        "--config",
        'model_provider="cx_account"',
        prompt,
    ]


def _run_codex(arguments: Sequence[str], scratch_directory: Path, timeout_seconds: int) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(arguments),
        check=False,
        cwd=scratch_directory,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
    )


def _parse_event_stream(stdout: object) -> tuple[int, int, int, str | None, tuple[str, ...]]:
    """Inspect CLI JSONL transiently, retaining neither event bodies nor messages."""

    if not isinstance(stdout, bytes) or len(stdout) > _MAX_EVENT_BYTES:
        return 0, 0, 0, None, ("EVENT_STREAM_INVALID",)
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError:
        return 0, 0, 0, None, ("EVENT_STREAM_INVALID",)
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or len(lines) > _MAX_EVENT_COUNT:
        return 0, 0, 0, None, ("EVENT_STREAM_INVALID",)

    event_count = 0
    agent_messages: list[str] = []
    tool_call_count = 0
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return event_count, len(agent_messages), tool_call_count, None, ("EVENT_STREAM_INVALID",)
        if not isinstance(event, dict):
            return event_count, len(agent_messages), tool_call_count, None, ("EVENT_STREAM_INVALID",)
        event_count += 1
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if not isinstance(item_type, str):
            return event_count, len(agent_messages), tool_call_count, None, ("EVENT_STREAM_INVALID",)
        if item_type in _TOOL_ITEM_TYPES or item_type.endswith("_tool_call"):
            tool_call_count += 1
        if item_type in _AGENT_MESSAGE_ITEM_TYPES:
            message = item.get("text")
            if not isinstance(message, str) or len(message) > 4_096:
                return event_count, len(agent_messages), tool_call_count, None, ("RESPONSE_CONTRACT_FAILED",)
            agent_messages.append(message.strip())

    response_digest = sha256_bytes(agent_messages[-1].encode("utf-8")) if agent_messages else None
    reasons: list[str] = []
    if tool_call_count:
        reasons.append("TOOL_ACTIVITY_DETECTED")
    if len(agent_messages) != 1 or response_digest != _EXPECTED_RESPONSE_DIGEST:
        reasons.append("RESPONSE_CONTRACT_FAILED")
    return event_count, len(agent_messages), tool_call_count, response_digest, tuple(reasons)


def _run_one_shadow(
    ordinal: int,
    scratch_directory: Path,
    timeout_seconds: int,
    process_runner: ProcessRunner,
) -> ShadowRequestResult:
    prompt = _PUBLIC_PROMPT_TEMPLATE.format(ordinal=ordinal)
    started = time.monotonic()
    try:
        completed = process_runner(_command_for_shadow(prompt, scratch_directory), scratch_directory, timeout_seconds)
    except subprocess.TimeoutExpired:
        return ShadowRequestResult(
            ordinal=ordinal,
            status="FAIL",
            duration_ms=timeout_seconds * 1_000,
            exit_code=None,
            event_count=0,
            agent_message_count=0,
            tool_call_count=0,
            response_digest=None,
            reason_codes=("REQUEST_TIMEOUT",),
        )
    except OSError:
        return ShadowRequestResult(
            ordinal=ordinal,
            status="FAIL",
            duration_ms=min(int((time.monotonic() - started) * 1_000), timeout_seconds * 1_000),
            exit_code=None,
            event_count=0,
            agent_message_count=0,
            tool_call_count=0,
            response_digest=None,
            reason_codes=("CODEX_PROCESS_UNAVAILABLE",),
        )

    duration_ms = min(int((time.monotonic() - started) * 1_000), timeout_seconds * 1_000)
    if not isinstance(completed.returncode, int) or completed.returncode != 0:
        return ShadowRequestResult(
            ordinal=ordinal,
            status="FAIL",
            duration_ms=duration_ms,
            exit_code=completed.returncode if isinstance(completed.returncode, int) else None,
            event_count=0,
            agent_message_count=0,
            tool_call_count=0,
            response_digest=None,
            reason_codes=("CODEX_PROCESS_FAILED",),
        )
    event_count, agent_message_count, tool_call_count, response_digest, reasons = _parse_event_stream(completed.stdout)
    return ShadowRequestResult(
        ordinal=ordinal,
        status="PASS" if not reasons else "FAIL",
        duration_ms=duration_ms,
        exit_code=completed.returncode,
        event_count=event_count,
        agent_message_count=agent_message_count,
        tool_call_count=tool_call_count,
        response_digest=response_digest,
        reason_codes=reasons,
    )


def _approval_is_current(packet: Mapping[str, Any], approval_reference: object) -> None:
    if (
        not isinstance(approval_reference, str)
        or not approval_reference.strip()
        or packet.get("packet_digest") not in approval_reference
    ):
        raise AuthorityDeniedError("M9 shadow requires the current explicit approval reference")


def _assert_packet_shadow_scope(packet: Mapping[str, Any]) -> None:
    impact = packet.get("network_and_data_impact")
    if not isinstance(impact, Mapping) or impact.get("selected_model_provider") != M9_SHADOW_PROVIDER:
        raise AuthorityDeniedError("M9 packet does not authorize the selected shadow route")
    request = packet.get("approval_request")
    if not isinstance(request, str) or "50-request" not in request or "PUBLIC" not in request:
        raise AuthorityDeniedError("M9 packet does not authorize the fixed public shadow plan")


def validate_m9_applied_state(packet: Mapping[str, Any], codex_home: Path) -> dict[str, object]:
    """Read every approved post-state target without changing global state.

    This verifier lives outside the packet's apply tooling so the approved
    tooling hashes remain immutable after a successful apply. It emits no file
    content, only the packet digest and a target count on success.
    """

    home = codex_home.resolve()
    if packet.get("codex_home") != str(home):
        raise IntegrityError("M9 packet targets a different Codex home")
    targets = packet.get("targets")
    if not isinstance(targets, list) or not targets:
        raise IntegrityError("M9 packet target list is invalid")
    for target in targets:
        if not isinstance(target, Mapping):
            raise IntegrityError("M9 packet target is invalid")
        relative_path = target.get("path")
        expected_digest = target.get("after_sha256")
        expected_mode = target.get("after_mode")
        relative = Path(relative_path) if isinstance(relative_path, str) else None
        if (
            relative is None
            or relative.is_absolute()
            or ".." in relative.parts
            or not isinstance(expected_digest, str)
            or type(expected_mode) is not int
        ):
            raise IntegrityError("M9 packet target is invalid")
        directory = home
        for part in relative.parts[:-1]:
            directory = directory / part
            if directory.is_symlink() or not directory.is_dir():
                raise IntegrityError("M9 applied target parent is unsafe")
        candidate = home / relative
        try:
            candidate.resolve(strict=False).relative_to(home)
            metadata = candidate.lstat()
        except (FileNotFoundError, ValueError) as error:
            raise IntegrityError("M9 applied target is missing or unsafe") from error
        if candidate.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_EVENT_BYTES:
            raise IntegrityError("M9 applied target is unsafe")
        contents = candidate.read_bytes()
        if stat.S_IMODE(metadata.st_mode) != expected_mode or sha256_bytes(contents) != expected_digest:
            raise IntegrityError("M9 applied target differs from the approved packet")
    return {
        "packet_digest": packet["packet_digest"],
        "status": "APPLIED_STATE_PASS",
        "target_count": len(targets),
    }


def run_initial_shadow(
    packet: Mapping[str, Any],
    codex_home: Path,
    *,
    approval_reference: str,
    timeout_seconds: int = M9_SHADOW_TIMEOUT_SECONDS,
    process_runner: ProcessRunner = _run_codex,
) -> dict[str, object]:
    """Run the one approved 50-request public shadow without retaining raw data.

    The first transport/security failure stops the batch.  An otherwise clean
    batch remains unpromoted because Codex CLI JSONL is not an authenticated
    9router/provider telemetry contract.
    """

    if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= M9_SHADOW_TIMEOUT_SECONDS:
        raise SemanticValidationError("M9 shadow timeout is invalid")
    verify_m9_packet(packet)
    _approval_is_current(packet, approval_reference)
    _assert_packet_shadow_scope(packet)
    readback = validate_m9_applied_state(packet, codex_home)

    results: list[ShadowRequestResult] = []
    with tempfile.TemporaryDirectory(prefix="m9-shadow-") as temporary:
        scratch_directory = Path(temporary)
        for ordinal in range(1, M9_SHADOW_REQUEST_COUNT + 1):
            result = _run_one_shadow(ordinal, scratch_directory, timeout_seconds, process_runner)
            results.append(result)
            if result.status != "PASS":
                break

    successful = sum(result.status == "PASS" for result in results)
    tool_calls = sum(result.tool_call_count for result in results)
    completed = len(results) == M9_SHADOW_REQUEST_COUNT and successful == M9_SHADOW_REQUEST_COUNT
    shadow_execution_status = "PASS" if completed else "FAIL"
    return {
        "schema_version": M9_SHADOW_SCHEMA_VERSION,
        "milestone": "M9",
        "receipt_kind": "initial-public-read-only-shadow",
        "writer_version": M9_SHADOW_WRITER_VERSION,
        "recorded_at": _utc_now(),
        "packet_digest": packet["packet_digest"],
        "configured_provider": M9_SHADOW_PROVIDER,
        "global_readback": readback,
        "execution": {
            "fresh_session_per_request": True,
            "session_persistence": "disabled",
            "sandbox": "read-only",
            "workspace": "empty-temporary-directory",
            "prompt_template_digest": _PROMPT_TEMPLATE_DIGEST,
            "expected_response_digest": _EXPECTED_RESPONSE_DIGEST,
            "requested_shadow_count": M9_SHADOW_REQUEST_COUNT,
            "executed_shadow_count": len(results),
            "passed_shadow_count": successful,
            "tool_call_count": tool_calls,
            "memory_write_started": False,
            "capability_execution_started": False,
            "external_mutation_started": False,
            "stopped_early": len(results) != M9_SHADOW_REQUEST_COUNT,
        },
        "route_attestation": {
            "status": "MISSING_TELEMETRY",
            "live_attested": False,
            "reason_code": "CODEX_CLI_EVENTS_LACK_AUTHENTICATED_ROUTE_OBSERVATION",
        },
        "shadow_execution_status": shadow_execution_status,
        "promotion_status": (
            "BLOCKED_MISSING_AUTHENTICATED_ROUTE_TELEMETRY"
            if completed
            else "BLOCKED_SHADOW_EXECUTION_FAILURE"
        ),
        "request_results": [result.to_dict() for result in results],
    }


def _without_receipt_digest(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "receipt_digest"}


def verify_initial_shadow_receipt(payload: Mapping[str, Any]) -> None:
    """Validate the closed, redacted M9 shadow receipt shape and digest."""

    required = {
        "schema_version",
        "milestone",
        "receipt_kind",
        "writer_version",
        "recorded_at",
        "packet_digest",
        "configured_provider",
        "global_readback",
        "execution",
        "route_attestation",
        "shadow_execution_status",
        "promotion_status",
        "request_results",
        "receipt_digest",
    }
    if type(payload) is not dict or set(payload) != required:
        raise SemanticValidationError("M9 shadow receipt shape is invalid")
    if (
        payload["schema_version"] != M9_SHADOW_SCHEMA_VERSION
        or payload["milestone"] != "M9"
        or payload["receipt_kind"] != "initial-public-read-only-shadow"
        or payload["writer_version"] != M9_SHADOW_WRITER_VERSION
        or not isinstance(payload["recorded_at"], str)
        or _TIMESTAMP.fullmatch(payload["recorded_at"]) is None
        or payload["configured_provider"] != M9_SHADOW_PROVIDER
    ):
        raise SemanticValidationError("M9 shadow receipt identity is invalid")
    _require_hash(payload["packet_digest"], "M9 shadow packet digest")
    _require_hash(payload["receipt_digest"], "M9 shadow receipt digest")

    readback = payload["global_readback"]
    if (
        type(readback) is not dict
        or readback.get("status") != "APPLIED_STATE_PASS"
        or readback.get("packet_digest") != payload["packet_digest"]
        or not isinstance(readback.get("target_count"), int)
        or readback["target_count"] < 1
    ):
        raise SemanticValidationError("M9 shadow readback is invalid")

    execution = payload["execution"]
    required_execution = {
        "fresh_session_per_request",
        "session_persistence",
        "sandbox",
        "workspace",
        "prompt_template_digest",
        "expected_response_digest",
        "requested_shadow_count",
        "executed_shadow_count",
        "passed_shadow_count",
        "tool_call_count",
        "memory_write_started",
        "capability_execution_started",
        "external_mutation_started",
        "stopped_early",
    }
    if type(execution) is not dict or set(execution) != required_execution:
        raise SemanticValidationError("M9 shadow execution is invalid")
    if (
        execution["fresh_session_per_request"] is not True
        or execution["session_persistence"] != "disabled"
        or execution["sandbox"] != "read-only"
        or execution["workspace"] != "empty-temporary-directory"
        or execution["prompt_template_digest"] != _PROMPT_TEMPLATE_DIGEST
        or execution["expected_response_digest"] != _EXPECTED_RESPONSE_DIGEST
        or execution["requested_shadow_count"] != M9_SHADOW_REQUEST_COUNT
        or any(execution[key] is not False for key in ("memory_write_started", "capability_execution_started", "external_mutation_started"))
        or not isinstance(execution["stopped_early"], bool)
    ):
        raise SemanticValidationError("M9 shadow execution is invalid")
    for key in ("executed_shadow_count", "passed_shadow_count", "tool_call_count"):
        if not isinstance(execution[key], int) or execution[key] < 0 or execution[key] > M9_SHADOW_REQUEST_COUNT:
            raise SemanticValidationError("M9 shadow execution count is invalid")

    route = payload["route_attestation"]
    if route != {
        "status": "MISSING_TELEMETRY",
        "live_attested": False,
        "reason_code": "CODEX_CLI_EVENTS_LACK_AUTHENTICATED_ROUTE_OBSERVATION",
    }:
        raise SemanticValidationError("M9 shadow route boundary is invalid")
    if payload["shadow_execution_status"] not in {"PASS", "FAIL"} or payload["promotion_status"] not in {
        "BLOCKED_MISSING_AUTHENTICATED_ROUTE_TELEMETRY",
        "BLOCKED_SHADOW_EXECUTION_FAILURE",
    }:
        raise SemanticValidationError("M9 shadow status is invalid")

    raw_results = payload["request_results"]
    if type(raw_results) is not list or not raw_results or len(raw_results) > M9_SHADOW_REQUEST_COUNT:
        raise SemanticValidationError("M9 shadow request results are invalid")
    results: list[ShadowRequestResult] = []
    for expected_ordinal, raw in enumerate(raw_results, start=1):
        if type(raw) is not dict or set(raw) != {
            "ordinal",
            "status",
            "duration_ms",
            "exit_code",
            "event_count",
            "agent_message_count",
            "tool_call_count",
            "response_digest",
            "reason_codes",
        }:
            raise SemanticValidationError("M9 shadow request result is invalid")
        result = ShadowRequestResult(
            ordinal=raw["ordinal"],
            status=raw["status"],
            duration_ms=raw["duration_ms"],
            exit_code=raw["exit_code"],
            event_count=raw["event_count"],
            agent_message_count=raw["agent_message_count"],
            tool_call_count=raw["tool_call_count"],
            response_digest=raw["response_digest"],
            reason_codes=tuple(raw["reason_codes"]) if isinstance(raw["reason_codes"], list) else (),
        )
        if result.ordinal != expected_ordinal:
            raise SemanticValidationError("M9 shadow result ordering is invalid")
        results.append(result)
    passed = sum(result.status == "PASS" for result in results)
    if (
        execution["executed_shadow_count"] != len(results)
        or execution["passed_shadow_count"] != passed
        or execution["tool_call_count"] != sum(result.tool_call_count for result in results)
        or execution["stopped_early"] != (len(results) != M9_SHADOW_REQUEST_COUNT)
    ):
        raise SemanticValidationError("M9 shadow counters do not match results")
    completed = len(results) == M9_SHADOW_REQUEST_COUNT and passed == M9_SHADOW_REQUEST_COUNT
    if (
        payload["shadow_execution_status"] != ("PASS" if completed else "FAIL")
        or payload["promotion_status"]
        != ("BLOCKED_MISSING_AUTHENTICATED_ROUTE_TELEMETRY" if completed else "BLOCKED_SHADOW_EXECUTION_FAILURE")
    ):
        raise SemanticValidationError("M9 shadow status does not match results")
    if sha256_hex(_without_receipt_digest(payload)) != payload["receipt_digest"]:
        raise IntegrityError("M9 shadow receipt digest does not match")


def serialize_initial_shadow_receipt(payload: Mapping[str, Any]) -> bytes:
    verify_initial_shadow_receipt(payload)
    return canonical_jcs_bytes(dict(payload)) + b"\n"


def write_initial_shadow_receipt(
    payload: Mapping[str, Any],
    relative_output: str = f"artifacts/{M9_SHADOW_RECEIPT_NAME}",
) -> Path:
    """Create an immutable M9 receipt; never replace a different existing file."""

    serialized = serialize_initial_shadow_receipt(payload)
    target = resolve_workspace_path(relative_output)
    artifacts_root = repository_root() / "artifacts"
    if artifacts_root.is_symlink() or not artifacts_root.is_dir():
        raise IntegrityError("M9 receipt root is unsafe")
    try:
        target.resolve(strict=False).relative_to(artifacts_root.resolve(strict=True))
    except ValueError as error:
        raise IntegrityError("M9 receipt target escapes artifacts") from error
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file():
            raise IntegrityError("M9 receipt target is unsafe")
        if target.read_bytes() != serialized:
            raise IntegrityError("M9 receipt already exists with different content")
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
        raise IntegrityError("M9 receipt already exists with different content")
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise
    return target


def build_and_write_initial_shadow(
    codex_home: Path,
    *,
    approval_reference: str,
    timeout_seconds: int = M9_SHADOW_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Load the exact packet, run the approved shadow, and persist its receipt."""

    packet = load_m9_packet()
    payload = run_initial_shadow(
        packet,
        codex_home,
        approval_reference=approval_reference,
        timeout_seconds=timeout_seconds,
    )
    payload = dict(payload)
    payload["receipt_digest"] = sha256_hex(payload)
    verify_initial_shadow_receipt(payload)
    output = write_initial_shadow_receipt(payload)
    return {
        "packet_digest": payload["packet_digest"],
        "receipt_digest": payload["receipt_digest"],
        "shadow_execution_status": payload["shadow_execution_status"],
        "promotion_status": payload["promotion_status"],
        "executed_shadow_count": payload["execution"]["executed_shadow_count"],
        "passed_shadow_count": payload["execution"]["passed_shadow_count"],
        "receipt_path": output.relative_to(repository_root()).as_posix(),
    }
