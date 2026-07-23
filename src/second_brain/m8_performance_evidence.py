"""Controlled local graph/context measurement for M8.

This runner measures a disposable M7 fixture with ``time.monotonic``.  It is
deliberately not a provider benchmark: only aggregate, redacted scheduling and
context-proxy facts are retained, and promotion remains deferred.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import re
from threading import Barrier, BrokenBarrierError, Lock
import time
from typing import Any

from .admission import AdmissionEngine, AdmissionFeatures, AuditTrail
from .canonical import sha256_hex
from .errors import SemanticValidationError
from .graph_runtime import ArtifactReference, GraphRuntime, GraphState, NodeResult, ProposedChange, validate_work_graph_manifest
from .jsonio import load_strict_json
from .workspace import repository_root


M8_PERFORMANCE_EVIDENCE_VERSION = 1
M8_PERFORMANCE_EVIDENCE_SCOPE = "synthetic-local-controlled"
M8_PERFORMANCE_SAMPLE_COUNT = 3

_HEX = re.compile(r"^[0-9a-f]{64}$")
_CLOCK_NAME = "monotonic-clock"
_RAW_INTERMEDIATE_TOKENS = {"collect": 640, "analyze": 640, "review": 160}
_NODE_TOKENS = {**_RAW_INTERMEDIATE_TOKENS, "integrate": 80}


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HEX.fullmatch(value) is None:
        raise SemanticValidationError(f"{label} is invalid")
    return value


def _require_int(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise SemanticValidationError(f"{label} is invalid")
    return value


def _require_exact_keys(value: object, keys: frozenset[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise SemanticValidationError(f"{label} is invalid")
    return deepcopy(value)


@dataclass(frozen=True)
class M8PerformanceMeasurement:
    """A stable, aggregate result from controlled M7 scheduling samples."""

    manifest_digest: str
    sample_count: int
    parallel_faster_sample_count: int
    parallel_max_active_workers: int
    serial_max_active_workers: int
    raw_intermediate_token_count: int
    integrator_context_tokens: int
    context_reduction_percent: int
    graph_contract_passed: bool
    context_proxy_passed: bool
    global_target_count: int = 0
    material_actions_started: int = 0
    network_requests_started: int = 0
    live_attested: bool = False
    result_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not M8PerformanceMeasurement:
            raise SemanticValidationError("M8 performance measurement is invalid")
        _require_hash(self.manifest_digest, "M8 performance manifest digest")
        _require_int(self.sample_count, "M8 performance sample count", minimum=1, maximum=16)
        _require_int(
            self.parallel_faster_sample_count,
            "M8 parallel faster sample count",
            minimum=0,
            maximum=self.sample_count,
        )
        _require_int(self.parallel_max_active_workers, "M8 parallel worker count", minimum=1, maximum=4)
        _require_int(self.serial_max_active_workers, "M8 serial worker count", minimum=1, maximum=1)
        _require_int(self.raw_intermediate_token_count, "M8 raw intermediate tokens", minimum=1, maximum=100_000)
        _require_int(self.integrator_context_tokens, "M8 integrator context tokens", minimum=1, maximum=100_000)
        _require_int(self.context_reduction_percent, "M8 context reduction percent", minimum=0, maximum=100)
        if not isinstance(self.graph_contract_passed, bool) or not isinstance(self.context_proxy_passed, bool):
            raise SemanticValidationError("M8 performance checks are invalid")
        for label, value in (
            ("M8 performance global target count", self.global_target_count),
            ("M8 performance material action count", self.material_actions_started),
            ("M8 performance network request count", self.network_requests_started),
        ):
            if _require_int(value, label, minimum=0, maximum=0) != 0:
                raise SemanticValidationError(f"{label} is invalid")
        if self.live_attested is not False:
            raise SemanticValidationError("M8 performance measurement cannot claim live evidence")
        if self.context_reduction_percent != (
            (self.raw_intermediate_token_count - self.integrator_context_tokens) * 100
        ) // self.raw_intermediate_token_count:
            raise SemanticValidationError("M8 performance context reduction is invalid")
        digest = sha256_hex(self._digest_input())
        if self.result_digest and self.result_digest != digest:
            raise SemanticValidationError("M8 performance measurement digest does not match")
        object.__setattr__(self, "result_digest", digest)

    @property
    def local_contract_passed(self) -> bool:
        return (
            self.graph_contract_passed
            and self.context_proxy_passed
            and self.parallel_faster_sample_count == self.sample_count
            and self.parallel_max_active_workers >= 2
            and self.serial_max_active_workers == 1
        )

    def _digest_input(self) -> dict[str, object]:
        return {
            "evidence_version": M8_PERFORMANCE_EVIDENCE_VERSION,
            "evidence_scope": M8_PERFORMANCE_EVIDENCE_SCOPE,
            "measurement_clock": _CLOCK_NAME,
            "promotion_disposition": "DEFERRED_TO_M9",
            "manifest_digest": self.manifest_digest,
            "sample_count": self.sample_count,
            "parallel_faster_sample_count": self.parallel_faster_sample_count,
            "parallel_max_active_workers": self.parallel_max_active_workers,
            "serial_max_active_workers": self.serial_max_active_workers,
            "raw_intermediate_token_count": self.raw_intermediate_token_count,
            "integrator_context_tokens": self.integrator_context_tokens,
            "context_reduction_percent": self.context_reduction_percent,
            "graph_contract_passed": self.graph_contract_passed,
            "context_proxy_passed": self.context_proxy_passed,
            "global_target_count": self.global_target_count,
            "material_actions_started": self.material_actions_started,
            "network_requests_started": self.network_requests_started,
            "live_attested": self.live_attested,
        }

    def to_receipt(self) -> dict[str, object]:
        return {
            **self._digest_input(),
            "state": "LOCAL_CONTROLLED_COMPLETE" if self.local_contract_passed else "LOCAL_CONTROLLED_INCONCLUSIVE",
            "result_digest": self.result_digest,
        }

    @classmethod
    def from_receipt(cls, value: object) -> "M8PerformanceMeasurement":
        raw = _require_exact_keys(
            value,
            frozenset(
                (
                    "evidence_version",
                    "evidence_scope",
                    "measurement_clock",
                    "promotion_disposition",
                    "manifest_digest",
                    "sample_count",
                    "parallel_faster_sample_count",
                    "parallel_max_active_workers",
                    "serial_max_active_workers",
                    "raw_intermediate_token_count",
                    "integrator_context_tokens",
                    "context_reduction_percent",
                    "graph_contract_passed",
                    "context_proxy_passed",
                    "global_target_count",
                    "material_actions_started",
                    "network_requests_started",
                    "live_attested",
                    "state",
                    "result_digest",
                )
            ),
            "M8 performance measurement receipt",
        )
        if (
            raw["evidence_version"] != M8_PERFORMANCE_EVIDENCE_VERSION
            or raw["evidence_scope"] != M8_PERFORMANCE_EVIDENCE_SCOPE
            or raw["measurement_clock"] != _CLOCK_NAME
            or raw["promotion_disposition"] != "DEFERRED_TO_M9"
        ):
            raise SemanticValidationError("M8 performance measurement receipt is invalid")
        try:
            result = cls(
                manifest_digest=raw["manifest_digest"],
                sample_count=raw["sample_count"],
                parallel_faster_sample_count=raw["parallel_faster_sample_count"],
                parallel_max_active_workers=raw["parallel_max_active_workers"],
                serial_max_active_workers=raw["serial_max_active_workers"],
                raw_intermediate_token_count=raw["raw_intermediate_token_count"],
                integrator_context_tokens=raw["integrator_context_tokens"],
                context_reduction_percent=raw["context_reduction_percent"],
                graph_contract_passed=raw["graph_contract_passed"],
                context_proxy_passed=raw["context_proxy_passed"],
                global_target_count=raw["global_target_count"],
                material_actions_started=raw["material_actions_started"],
                network_requests_started=raw["network_requests_started"],
                live_attested=raw["live_attested"],
                result_digest=raw["result_digest"],
            )
        except (TypeError, ValueError, SemanticValidationError) as error:
            raise SemanticValidationError("M8 performance measurement receipt is invalid") from error
        expected_state = "LOCAL_CONTROLLED_COMPLETE" if result.local_contract_passed else "LOCAL_CONTROLLED_INCONCLUSIVE"
        if raw["state"] != expected_state:
            raise SemanticValidationError("M8 performance measurement state is invalid")
        return result


def _manifest() -> Any:
    path = repository_root() / "fixtures" / "canonical" / "work-graph-v1.json"
    if path.is_symlink() or not path.is_file():
        raise SemanticValidationError("M8 performance graph fixture is unavailable")
    try:
        return validate_work_graph_manifest(load_strict_json(path))
    except Exception as error:
        raise SemanticValidationError("M8 performance graph fixture is invalid") from error


def _run_sample(manifest: Any, *, serial_fallback: bool) -> tuple[Any, int]:
    audit = AuditTrail()
    admission = AdmissionEngine(audit_trail=audit).admit(AdmissionFeatures(high_risk=True), task_id=manifest.task_id)
    barrier = None if serial_fallback else Barrier(2)
    contexts: dict[str, int] = {}
    contexts_lock = Lock()

    def runner(context: object, _cancellation: object) -> NodeResult:
        node_id = getattr(context, "node_id", None)
        attempt = getattr(context, "attempt", None)
        if node_id not in _NODE_TOKENS or not isinstance(attempt, int):
            raise SemanticValidationError("M8 performance runner context is invalid")
        if node_id in {"collect", "analyze"}:
            if barrier is not None:
                try:
                    barrier.wait(timeout=1)
                except BrokenBarrierError as error:
                    raise SemanticValidationError("M8 performance parallel branches did not overlap") from error
            time.sleep(0.04)
        else:
            time.sleep(0.006)
        estimated_tokens = getattr(context, "estimated_tokens", None)
        if not isinstance(estimated_tokens, int):
            raise SemanticValidationError("M8 performance context is invalid")
        with contexts_lock:
            contexts[node_id] = estimated_tokens
        node = manifest.node_by_id[node_id]
        changes = ()
        if node.write_scope:
            changes = (ProposedChange(node.write_scope[0], sha256_hex({"node_id": node_id, "proposal": True})),)
        return NodeResult.succeeded(
            node_id,
            attempt,
            artifacts=(ArtifactReference(node.expected_artifacts[0], sha256_hex({"node_id": node_id, "artifact": True})),),
            changes=changes,
            checks=node.acceptance,
            token_count=_NODE_TOKENS[node_id],
            cost_microunits=1,
        )

    receipt = GraphRuntime(manifest, audit_trail=audit, monotonic_clock=time.monotonic).run(
        admission,
        runner,
        serial_fallback=serial_fallback,
    )
    if receipt.state is not GraphState.SUCCEEDED or "integrate" not in contexts:
        raise SemanticValidationError("M8 performance graph execution failed")
    return receipt, contexts["integrate"]


def run_m8_local_performance_measurement() -> M8PerformanceMeasurement:
    """Observe a paired synthetic M7 schedule without claiming a live benchmark."""

    manifest = _manifest()
    parallel_faster = 0
    parallel_workers = 0
    serial_workers = 0
    integrator_context_tokens: set[int] = set()
    graph_contract_passed = True
    for _ in range(M8_PERFORMANCE_SAMPLE_COUNT):
        parallel, parallel_context = _run_sample(manifest, serial_fallback=False)
        serial, serial_context = _run_sample(manifest, serial_fallback=True)
        parallel_faster += int(parallel.duration_ms < serial.duration_ms)
        parallel_workers = max(parallel_workers, parallel.max_active_workers)
        serial_workers = max(serial_workers, serial.max_active_workers)
        integrator_context_tokens.update((parallel_context, serial_context))
        graph_contract_passed = graph_contract_passed and (
            parallel.state is GraphState.SUCCEEDED
            and serial.state is GraphState.SUCCEEDED
            and parallel.serial_fallback is False
            and serial.serial_fallback is True
            and parallel.max_active_workers >= 2
            and serial.max_active_workers == 1
        )
    if len(integrator_context_tokens) != 1:
        raise SemanticValidationError("M8 performance context proxy is nondeterministic")
    raw_intermediate = sum(_RAW_INTERMEDIATE_TOKENS.values())
    integrator_context = next(iter(integrator_context_tokens))
    reduction = ((raw_intermediate - integrator_context) * 100) // raw_intermediate
    return M8PerformanceMeasurement(
        manifest_digest=manifest.manifest_digest,
        sample_count=M8_PERFORMANCE_SAMPLE_COUNT,
        parallel_faster_sample_count=parallel_faster,
        parallel_max_active_workers=parallel_workers,
        serial_max_active_workers=serial_workers,
        raw_intermediate_token_count=raw_intermediate,
        integrator_context_tokens=integrator_context,
        context_reduction_percent=reduction,
        graph_contract_passed=graph_contract_passed,
        context_proxy_passed=reduction >= 35,
    )
