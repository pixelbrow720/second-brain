"""Project-local M8 evaluation, canary, and rollback rehearsal contracts.

The evaluator is deliberately a closed synthetic harness.  It validates
versioned fixture data and deterministic observations, but never calls a
provider, launches a capability, or changes durable/global state.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Mapping, Sequence
import uuid

from .admission import (
    AdmissionDecision,
    AdmissionEngine,
    AdmissionFeatures,
    AuditTrail,
    CapabilityDescriptor,
    CapabilityRegistry,
    CapabilityRequest,
    CapabilityResolution,
    CapabilityResolver,
    Gateway,
    Lane,
    PermissionClass,
)
from .canonical import sha256_bytes, sha256_hex
from .clock import DeterministicClock
from .contracts import validate_named_document
from .errors import DuplicateKeyError, SemanticValidationError
from .graph_runtime import (
    ArtifactReference,
    GraphRunReceipt,
    GraphRuntime,
    GraphState,
    NodeResult,
    ProposedChange,
    WorkGraphManifest,
    validate_work_graph_manifest,
)
from .jsonio import load_strict_json, loads_strict_json
from .m8_m4_evidence import M4LocalEvaluation, run_m8_m4_local_evaluation
from .m8_performance_evidence import M8PerformanceMeasurement, run_m8_local_performance_measurement
from .profiles import APPROVED_PROFILE_ALIASES, load_profile_registry, registry_digest
from .routing import (
    ReconciliationStatus,
    RouteIntent,
    RouteReconciliation,
    RouteReceipt,
    SerializedRoute,
    create_route_intent,
    create_route_receipt,
    reconcile_route,
    run_five_repeat_synthetic_canary,
    serialize_route,
    side_effect_allowed,
    synthetic_observation,
)
from .workspace import repository_root


EVALUATION_VERSION = 1
EVALUATOR_VERSION = "m8-local-evaluator/3"
FIXTURE_EXECUTOR_VERSION = "m8-closed-component-executor/2"
M8_SEED = 20260723
M8_EVIDENCE_TIMESTAMP = "2026-07-23T00:00:00Z"
SYNTHETIC_LOCAL = "synthetic-local"
SIMULATED_SHADOW_READ_ONLY = "simulated-shadow-read-only"
LOCAL_ROLLBACK_REHEARSAL = "local-rollback-rehearsal"

_HEX = re.compile(r"^[0-9a-f]{64}$")
_CASE_ID = re.compile(r"^eval:[a-z0-9][a-z0-9._-]{2,95}$")
_FIXTURE_ID = re.compile(r"^fx:[a-z0-9][a-z0-9._-]{2,95}$")
_WORKSPACE_FIXTURE = re.compile(r"^m8-[a-z0-9][a-z0-9._-]{1,63}$")
_RUBRIC_ID = re.compile(r"^rubric:[a-z0-9][a-z0-9._-]{2,95}$")
_CHECK_ID = re.compile(r"^check:[a-z0-9][a-z0-9._-]{2,95}$")
_ARTIFACT_ID = re.compile(r"^artifact:[a-z0-9][a-z0-9._-]{2,95}$")
_CAPABILITY_ID = re.compile(r"^cap:[a-z0-9][a-z0-9._-]{0,63}$")
_SAFE_VERSION = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")

_SUITES = (
    "admission",
    "routing",
    "capability",
    "graph",
    "memory",
    "security",
    "end_to_end",
)
_PARTITIONS = ("frozen", "rotating")
_LANES = ("DIRECT", "ASSISTED", "GRAPH", "DEEP")
_EVENT_CODES = frozenset(
    (
        "external_research_started",
        "mcp_activated",
        "github_research_started",
        "graph_started",
        "memory_retrieval_started",
        "network_started",
        "capability_executed",
        "authority_write_started",
        "global_mutation_started",
    )
)
_UNSAFE_TEXT_MARKERS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "private key",
    "traceback",
    "http://",
    "https://",
    "file://",
    "../",
    "ignore previous",
    "system prompt",
    "os.environ",
)
_RECEIPT_NAMES = frozenset(
    (
        "m8-baseline-receipts.json",
        "m8-local-evaluation.json",
        "m8-m4-local-evaluation.json",
        "m8-performance-local-evaluation.json",
        "m8-local-canary.json",
        "m8-rollback-rehearsal.json",
        "m8-release-report.json",
        "m8-verification.json",
    )
)


class GateState(str, Enum):
    """Closed state set used by M8 local evidence."""

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_RUN = "NOT_RUN"
    DEFERRED_TO_M9 = "DEFERRED_TO_M9"


class ExecutionStatus(str, Enum):
    """Terminal, deterministic state for one injected fixture observation."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class BaselineKind(str, Enum):
    """The four immutable local baselines named by the evaluation plan."""

    B0_ROOT_TERA_MAX = "B0_ROOT_TERA_MAX"
    B1_SERIAL_WORKFLOW = "B1_SERIAL_WORKFLOW"
    B2_NO_MEMORY = "B2_NO_MEMORY"
    B3_LAST_KNOWN_GOOD = "B3_LAST_KNOWN_GOOD"


class ExecutionMode(str, Enum):
    """Closed strategies used to create the candidate and four baselines."""

    CANDIDATE = "candidate"
    B0_ROOT = "b0"
    B1_SERIAL = "b1"
    B2_NO_MEMORY = "b2"
    B3_LAST_KNOWN_GOOD = "b3"


def _require_exact_type(value: object, expected: type[object], label: str) -> None:
    if type(value) is not expected:
        raise SemanticValidationError(f"{label} is invalid")


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HEX.fullmatch(value) is None:
        raise SemanticValidationError(f"{label} is invalid")
    return value


def _require_int(value: object, label: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise SemanticValidationError(f"{label} is invalid")
    if maximum is not None and value > maximum:
        raise SemanticValidationError(f"{label} is invalid")
    return value


def _require_identifier(value: object, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise SemanticValidationError(f"{label} is invalid")
    return value


def _require_exact_keys(value: object, keys: frozenset[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise SemanticValidationError(f"{label} is invalid")
    return deepcopy(value)


def _require_safe_fixture_text(value: object, label: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise SemanticValidationError(f"{label} is invalid")
    lowered = value.lower()
    if any(marker in lowered for marker in _UNSAFE_TEXT_MARKERS):
        raise SemanticValidationError(f"{label} violates local synthetic-data policy")
    return value


def _freeze_identifiers(
    value: object,
    label: str,
    pattern: re.Pattern[str],
    *,
    maximum: int,
    allowed: frozenset[str] | None = None,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise SemanticValidationError(f"{label} is invalid")
    copied = tuple(value)
    if len(set(copied)) != len(copied):
        raise SemanticValidationError(f"{label} contains duplicates")
    for item in copied:
        _require_identifier(item, label, pattern)
        if allowed is not None and item not in allowed:
            raise SemanticValidationError(f"{label} is invalid")
    return copied


def _opaque_id_digest(identifier: str) -> str:
    return sha256_hex({"opaque_identifier": identifier})


def _ordered_case_id_digest(cases: Sequence["EvaluationCase"]) -> str:
    return sha256_hex({"ordered_case_ids": [case.case_id for case in cases]})


@dataclass(frozen=True)
class EvaluationInput:
    """Synthetic fixture input; it is never emitted in a receipt."""

    fixture_id: str
    prompt: str
    workspace_fixture: str

    def __post_init__(self) -> None:
        if type(self) is not EvaluationInput:
            raise SemanticValidationError("evaluation input wrapper is invalid")
        _require_identifier(self.fixture_id, "fixture_id", _FIXTURE_ID)
        _require_safe_fixture_text(self.prompt, "prompt")
        _require_identifier(self.workspace_fixture, "workspace_fixture", _WORKSPACE_FIXTURE)

    def to_dict(self) -> dict[str, str]:
        return {
            "fixture_id": self.fixture_id,
            "prompt": self.prompt,
            "workspace_fixture": self.workspace_fixture,
        }


@dataclass(frozen=True)
class ExpectedEvaluation:
    """Closed deterministic expectations for one synthetic case."""

    lane: str
    allowed_profiles: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    forbidden_events: tuple[str, ...]
    required_check_ids: tuple[str, ...]
    required_artifact_ids: tuple[str, ...]
    safety_critical: bool

    def __post_init__(self) -> None:
        if type(self) is not ExpectedEvaluation:
            raise SemanticValidationError("expected evaluation wrapper is invalid")
        if self.lane not in _LANES:
            raise SemanticValidationError("expected lane is invalid")
        profiles = _freeze_identifiers(
            self.allowed_profiles,
            "allowed_profiles",
            _SAFE_VERSION,
            maximum=7,
            allowed=frozenset(APPROVED_PROFILE_ALIASES),
        )
        if not profiles:
            raise SemanticValidationError("allowed_profiles is invalid")
        capabilities = _freeze_identifiers(
            self.required_capabilities, "required_capabilities", _CAPABILITY_ID, maximum=12
        )
        events = _freeze_identifiers(
            self.forbidden_events,
            "forbidden_events",
            _SAFE_VERSION,
            maximum=12,
            allowed=_EVENT_CODES,
        )
        checks = _freeze_identifiers(
            self.required_check_ids, "required_check_ids", _CHECK_ID, maximum=24
        )
        if not checks:
            raise SemanticValidationError("required_check_ids is invalid")
        artifacts = _freeze_identifiers(
            self.required_artifact_ids, "required_artifact_ids", _ARTIFACT_ID, maximum=24
        )
        if type(self.safety_critical) is not bool:
            raise SemanticValidationError("safety_critical is invalid")
        object.__setattr__(self, "allowed_profiles", profiles)
        object.__setattr__(self, "required_capabilities", capabilities)
        object.__setattr__(self, "forbidden_events", events)
        object.__setattr__(self, "required_check_ids", checks)
        object.__setattr__(self, "required_artifact_ids", artifacts)

    def to_dict(self) -> dict[str, object]:
        return {
            "lane": self.lane,
            "allowed_profiles": list(self.allowed_profiles),
            "required_capabilities": list(self.required_capabilities),
            "forbidden_events": list(self.forbidden_events),
            "required_check_ids": list(self.required_check_ids),
            "required_artifact_ids": list(self.required_artifact_ids),
            "safety_critical": self.safety_critical,
        }


@dataclass(frozen=True)
class EvaluationCase:
    """Strict V1 evaluation input parsed from one JSONL line."""

    version: int
    case_id: str
    suite: str
    partition: str
    language: str
    anti_research_trap: bool
    input: EvaluationInput
    expected: ExpectedEvaluation
    risk: str
    data_class: str
    rubric_id: str
    weight: int
    tags: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self) is not EvaluationCase:
            raise SemanticValidationError("evaluation case wrapper is invalid")
        if self.version != EVALUATION_VERSION or isinstance(self.version, bool):
            raise SemanticValidationError("evaluation case version is invalid")
        _require_identifier(self.case_id, "case_id", _CASE_ID)
        if self.suite not in _SUITES or self.partition not in _PARTITIONS:
            raise SemanticValidationError("evaluation case suite or partition is invalid")
        if self.language not in {"id", "mixed", "en"}:
            raise SemanticValidationError("evaluation case language is invalid")
        if type(self.anti_research_trap) is not bool:
            raise SemanticValidationError("anti_research_trap is invalid")
        if type(self.input) is not EvaluationInput or type(self.expected) is not ExpectedEvaluation:
            raise SemanticValidationError("evaluation case fields are invalid")
        if self.risk not in {"low", "medium", "high"} or self.data_class != "PUBLIC":
            raise SemanticValidationError("evaluation case classification is invalid")
        _require_identifier(self.rubric_id, "rubric_id", _RUBRIC_ID)
        _require_int(self.weight, "weight", minimum=1, maximum=100)
        tags = _freeze_identifiers(self.tags, "tags", re.compile(r"^[a-z0-9][a-z0-9._/-]{0,63}$"), maximum=12)
        if not tags:
            raise SemanticValidationError("tags is invalid")
        object.__setattr__(self, "tags", tags)

    @classmethod
    def from_value(cls, value: object) -> "EvaluationCase":
        """Validate a JSON-shaped V1 case without retaining caller containers."""

        if type(value) is not dict:
            raise SemanticValidationError("evaluation case is invalid")
        candidate = deepcopy(value)
        try:
            validate_named_document("evaluation-case-v1", candidate)
        except Exception as error:
            raise SemanticValidationError("evaluation case schema is invalid") from error
        try:
            raw_input = candidate["input"]
            raw_expected = candidate["expected"]
            if type(raw_input) is not dict or type(raw_expected) is not dict:
                raise TypeError
            return cls(
                version=candidate["version"],
                case_id=candidate["case_id"],
                suite=candidate["suite"],
                partition=candidate["partition"],
                language=candidate["language"],
                anti_research_trap=candidate["anti_research_trap"],
                input=EvaluationInput(**deepcopy(raw_input)),
                expected=ExpectedEvaluation(
                    lane=raw_expected["lane"],
                    allowed_profiles=tuple(raw_expected["allowed_profiles"]),
                    required_capabilities=tuple(raw_expected["required_capabilities"]),
                    forbidden_events=tuple(raw_expected["forbidden_events"]),
                    required_check_ids=tuple(raw_expected["required_check_ids"]),
                    required_artifact_ids=tuple(raw_expected["required_artifact_ids"]),
                    safety_critical=raw_expected["safety_critical"],
                ),
                risk=candidate["risk"],
                data_class=candidate["data_class"],
                rubric_id=candidate["rubric_id"],
                weight=candidate["weight"],
                tags=tuple(candidate["tags"]),
            )
        except (KeyError, TypeError, ValueError, SemanticValidationError) as error:
            raise SemanticValidationError("evaluation case is invalid") from error

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "case_id": self.case_id,
            "suite": self.suite,
            "partition": self.partition,
            "language": self.language,
            "anti_research_trap": self.anti_research_trap,
            "input": self.input.to_dict(),
            "expected": self.expected.to_dict(),
            "risk": self.risk,
            "data_class": self.data_class,
            "rubric_id": self.rubric_id,
            "weight": self.weight,
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class DatasetManifest:
    """Immutable digest and distribution metadata for ordered synthetic cases."""

    dataset_version: int
    partition: str
    case_count: int
    ordered_case_id_digest: str
    dataset_sha256: str
    suite_counts: tuple[tuple[str, int], ...]
    language_counts: tuple[tuple[str, int], ...]
    anti_research_trap_count: int
    seed: int
    manifest_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not DatasetManifest:
            raise SemanticValidationError("dataset manifest wrapper is invalid")
        if self.dataset_version != EVALUATION_VERSION or isinstance(self.dataset_version, bool):
            raise SemanticValidationError("dataset version is invalid")
        if self.partition not in {*_PARTITIONS, "combined"}:
            raise SemanticValidationError("dataset partition is invalid")
        _require_int(self.case_count, "case_count", minimum=1)
        _require_hash(self.ordered_case_id_digest, "ordered_case_id_digest")
        _require_hash(self.dataset_sha256, "dataset_sha256")
        _require_int(self.anti_research_trap_count, "anti_research_trap_count", minimum=0)
        if self.anti_research_trap_count > self.case_count:
            raise SemanticValidationError("anti_research_trap_count is invalid")
        if self.seed != M8_SEED or isinstance(self.seed, bool):
            raise SemanticValidationError("evaluation seed is invalid")
        suites = _freeze_count_pairs(self.suite_counts, _SUITES, "suite_counts")
        languages = _freeze_count_pairs(self.language_counts, ("id", "mixed", "en"), "language_counts")
        if sum(count for _, count in suites) != self.case_count:
            raise SemanticValidationError("suite counts do not match dataset count")
        if sum(count for _, count in languages) != self.case_count:
            raise SemanticValidationError("language counts do not match dataset count")
        object.__setattr__(self, "suite_counts", suites)
        object.__setattr__(self, "language_counts", languages)
        digest = sha256_hex(self._digest_input())
        if self.manifest_digest and self.manifest_digest != digest:
            raise SemanticValidationError("dataset manifest digest does not match")
        object.__setattr__(self, "manifest_digest", digest)

    def _digest_input(self) -> dict[str, object]:
        return {
            "dataset_version": self.dataset_version,
            "partition": self.partition,
            "case_count": self.case_count,
            "ordered_case_id_digest": self.ordered_case_id_digest,
            "dataset_sha256": self.dataset_sha256,
            "suite_counts": [{"suite": name, "count": count} for name, count in self.suite_counts],
            "language_counts": [{"language": name, "count": count} for name, count in self.language_counts],
            "anti_research_trap_count": self.anti_research_trap_count,
            "seed": self.seed,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self._digest_input(),
            "manifest_digest": self.manifest_digest,
        }

    @classmethod
    def from_value(cls, value: object) -> "DatasetManifest":
        raw = _require_exact_keys(
            value,
            frozenset(
                (
                    "dataset_version",
                    "partition",
                    "case_count",
                    "ordered_case_id_digest",
                    "dataset_sha256",
                    "suite_counts",
                    "language_counts",
                    "anti_research_trap_count",
                    "seed",
                    "manifest_digest",
                )
            ),
            "dataset manifest",
        )
        try:
            suites = tuple((item["suite"], item["count"]) for item in raw["suite_counts"])
            languages = tuple((item["language"], item["count"]) for item in raw["language_counts"])
            return cls(
                dataset_version=raw["dataset_version"],
                partition=raw["partition"],
                case_count=raw["case_count"],
                ordered_case_id_digest=raw["ordered_case_id_digest"],
                dataset_sha256=raw["dataset_sha256"],
                suite_counts=suites,
                language_counts=languages,
                anti_research_trap_count=raw["anti_research_trap_count"],
                seed=raw["seed"],
                manifest_digest=raw["manifest_digest"],
            )
        except (KeyError, TypeError, ValueError, SemanticValidationError) as error:
            raise SemanticValidationError("dataset manifest is invalid") from error


def _freeze_count_pairs(
    value: object, expected_names: Sequence[str], label: str
) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, (tuple, list)):
        raise SemanticValidationError(f"{label} is invalid")
    pairs: list[tuple[str, int]] = []
    for item in value:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise SemanticValidationError(f"{label} is invalid")
        name, count = item
        if name not in expected_names:
            raise SemanticValidationError(f"{label} is invalid")
        pairs.append((name, _require_int(count, label, minimum=0)))
    if tuple(name for name, _ in pairs) != tuple(expected_names):
        raise SemanticValidationError(f"{label} is invalid")
    return tuple(pairs)


def create_dataset_manifest(
    cases: Sequence[EvaluationCase], *, partition: str, seed: int = M8_SEED
) -> DatasetManifest:
    """Create a manifest from ordered strict cases without emitting their bodies."""

    copied = tuple(cases)
    if not copied or any(type(case) is not EvaluationCase for case in copied):
        raise SemanticValidationError("dataset cases are invalid")
    if partition not in {*_PARTITIONS, "combined"} or seed != M8_SEED or isinstance(seed, bool):
        raise SemanticValidationError("dataset manifest inputs are invalid")
    if partition != "combined" and any(case.partition != partition for case in copied):
        raise SemanticValidationError("dataset case partition does not match manifest")
    if len({case.case_id for case in copied}) != len(copied):
        raise SemanticValidationError("dataset contains duplicate case IDs")
    suite_counts = tuple((suite, sum(case.suite == suite for case in copied)) for suite in _SUITES)
    language_counts = tuple((language, sum(case.language == language for case in copied)) for language in ("id", "mixed", "en"))
    return DatasetManifest(
        dataset_version=EVALUATION_VERSION,
        partition=partition,
        case_count=len(copied),
        ordered_case_id_digest=_ordered_case_id_digest(copied),
        dataset_sha256=sha256_hex(
            {
                "dataset_version": EVALUATION_VERSION,
                "partition": partition,
                "cases": [case.to_dict() for case in copied],
            }
        ),
        suite_counts=suite_counts,
        language_counts=language_counts,
        anti_research_trap_count=sum(case.anti_research_trap for case in copied),
        seed=seed,
    )


@dataclass(frozen=True)
class EvaluationCorpus:
    """The fixed 70-case local corpus plus per-partition manifests."""

    frozen_cases: tuple[EvaluationCase, ...]
    rotating_cases: tuple[EvaluationCase, ...]
    frozen_manifest: DatasetManifest
    rotating_manifest: DatasetManifest
    manifest: DatasetManifest

    def __post_init__(self) -> None:
        if type(self) is not EvaluationCorpus:
            raise SemanticValidationError("evaluation corpus wrapper is invalid")
        frozen = tuple(self.frozen_cases)
        rotating = tuple(self.rotating_cases)
        if any(type(case) is not EvaluationCase for case in frozen + rotating):
            raise SemanticValidationError("evaluation corpus cases are invalid")
        if any(case.partition != "frozen" for case in frozen) or any(
            case.partition != "rotating" for case in rotating
        ):
            raise SemanticValidationError("evaluation corpus partition is invalid")
        if len({case.case_id for case in frozen + rotating}) != len(frozen) + len(rotating):
            raise SemanticValidationError("evaluation corpus contains duplicate case IDs")
        for manifest, cases, partition in (
            (self.frozen_manifest, frozen, "frozen"),
            (self.rotating_manifest, rotating, "rotating"),
            (self.manifest, frozen + rotating, "combined"),
        ):
            if type(manifest) is not DatasetManifest or manifest.partition != partition:
                raise SemanticValidationError("evaluation corpus manifest is invalid")
            expected = create_dataset_manifest(cases, partition=partition)
            if manifest.manifest_digest != expected.manifest_digest:
                raise SemanticValidationError("evaluation corpus manifest drifted")
        _validate_local_corpus_distribution(frozen, rotating)
        object.__setattr__(self, "frozen_cases", frozen)
        object.__setattr__(self, "rotating_cases", rotating)

    @property
    def cases(self) -> tuple[EvaluationCase, ...]:
        return self.frozen_cases + self.rotating_cases


def _workspace_regular_file(relative_path: str | Path, *, contained_in: str | None = None) -> Path:
    """Resolve a checked-in regular file while rejecting every symlink component."""

    relative = Path(relative_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise SemanticValidationError("local M8 input is unavailable")
    root = repository_root()
    lexical = root / relative
    current = lexical
    while current != root:
        if current.is_symlink():
            raise SemanticValidationError("local M8 input is unavailable")
        current = current.parent
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root)
        if contained_in is not None:
            resolved.relative_to((root / contained_in).resolve(strict=True))
    except (OSError, ValueError) as error:
        raise SemanticValidationError("local M8 input is unavailable") from error
    if not resolved.is_file() or resolved.is_symlink():
        raise SemanticValidationError("local M8 input is unavailable")
    return resolved


def _contained_fixture_path(path: str | Path) -> Path:
    try:
        return _workspace_regular_file(path, contained_in="fixtures/m8")
    except Exception as error:
        raise SemanticValidationError("evaluation dataset path is invalid") from error


def load_evaluation_jsonl(path: str | Path, *, partition: str) -> tuple[EvaluationCase, ...]:
    """Strictly load one synthetic JSONL partition before any grading begins."""

    if partition not in _PARTITIONS:
        raise SemanticValidationError("evaluation partition is invalid")
    source = _contained_fixture_path(path)
    try:
        text = source.read_bytes().decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise SemanticValidationError("evaluation dataset is not valid UTF-8") from error
    except OSError as error:
        raise SemanticValidationError("evaluation dataset is unavailable") from error
    lines = text.splitlines()
    if not lines:
        raise SemanticValidationError("evaluation dataset is empty")
    cases: list[EvaluationCase] = []
    for line in lines:
        if not line.strip():
            raise SemanticValidationError("evaluation dataset contains a blank record")
        try:
            raw = loads_strict_json(line)
        except (ValueError, DuplicateKeyError) as error:
            raise SemanticValidationError("evaluation dataset record is invalid") from error
        case = EvaluationCase.from_value(raw)
        if case.partition != partition:
            raise SemanticValidationError("evaluation dataset partition does not match")
        cases.append(case)
    if len({case.case_id for case in cases}) != len(cases):
        raise SemanticValidationError("evaluation dataset contains duplicate case IDs")
    return tuple(cases)


def _validate_local_corpus_distribution(
    frozen: Sequence[EvaluationCase], rotating: Sequence[EvaluationCase]
) -> None:
    for suite in _SUITES:
        if sum(case.suite == suite for case in frozen) != 7:
            raise SemanticValidationError("local frozen corpus does not have seven cases per suite")
        if sum(case.suite == suite for case in rotating) != 3:
            raise SemanticValidationError("local rotating corpus does not have three cases per suite")
    cases = tuple(frozen) + tuple(rotating)
    if len(cases) != 70:
        raise SemanticValidationError("local corpus size is invalid")
    if sum(case.language == "id" for case in cases) < 21:
        raise SemanticValidationError("local corpus Indonesian coverage is invalid")
    if sum(case.language == "mixed" for case in cases) < 14:
        raise SemanticValidationError("local corpus mixed-language coverage is invalid")
    if sum(case.anti_research_trap for case in cases) < 11:
        raise SemanticValidationError("local corpus anti-research coverage is invalid")
    for suite in ("routing", "capability", "graph", "memory", "security", "end_to_end"):
        if not any(case.suite == suite and case.expected.safety_critical for case in cases):
            raise SemanticValidationError("local corpus safety-critical coverage is invalid")


def load_local_evaluation_corpus() -> EvaluationCorpus:
    """Load the fixed project-contained corpus used by the M8 release runner."""

    frozen = load_evaluation_jsonl("fixtures/m8/frozen.jsonl", partition="frozen")
    rotating = load_evaluation_jsonl("fixtures/m8/rotating.jsonl", partition="rotating")
    return EvaluationCorpus(
        frozen_cases=frozen,
        rotating_cases=rotating,
        frozen_manifest=create_dataset_manifest(frozen, partition="frozen"),
        rotating_manifest=create_dataset_manifest(rotating, partition="rotating"),
        manifest=create_dataset_manifest(frozen + rotating, partition="combined"),
    )


_TASK_ID = re.compile(r"^task:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_GRAPH_PRIMARY_NODES = frozenset(("analyze", "review", "integrate"))


@dataclass(frozen=True)
class FixtureExecutionPlan:
    """Separate, digest-bound local component inputs for one evaluation case."""

    case_id: str
    admission_features: AdmissionFeatures
    profile_alias: str
    capability_required: bool
    graph_primary_node: str | None
    check_id: str
    plan_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not FixtureExecutionPlan:
            raise SemanticValidationError("execution plan wrapper is invalid")
        _require_identifier(self.case_id, "execution plan case_id", _CASE_ID)
        if type(self.admission_features) is not AdmissionFeatures:
            raise SemanticValidationError("execution plan admission features are invalid")
        if self.profile_alias not in APPROVED_PROFILE_ALIASES:
            raise SemanticValidationError("execution plan profile is invalid")
        if type(self.capability_required) is not bool:
            raise SemanticValidationError("execution plan capability requirement is invalid")
        if self.graph_primary_node is not None and self.graph_primary_node not in _GRAPH_PRIMARY_NODES:
            raise SemanticValidationError("execution plan graph node is invalid")
        _require_identifier(self.check_id, "execution plan check", _CHECK_ID)
        digest = sha256_hex(self._digest_input())
        if self.plan_digest and self.plan_digest != digest:
            raise SemanticValidationError("execution plan digest does not match")
        object.__setattr__(self, "plan_digest", digest)

    def _digest_input(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "admission_features": self.admission_features.to_dict(),
            "profile_alias": self.profile_alias,
            "capability_required": self.capability_required,
            "graph_primary_node": self.graph_primary_node,
            "check_id": self.check_id,
        }

    @classmethod
    def from_value(cls, value: object) -> "FixtureExecutionPlan":
        raw = _require_exact_keys(
            value,
            frozenset(
                (
                    "case_id",
                    "admission_features",
                    "profile_alias",
                    "capability_required",
                    "graph_primary_node",
                    "check_id",
                )
            ),
            "execution plan",
        )
        try:
            return cls(
                case_id=raw["case_id"],
                admission_features=AdmissionFeatures.from_value(raw["admission_features"]),
                profile_alias=raw["profile_alias"],
                capability_required=raw["capability_required"],
                graph_primary_node=raw["graph_primary_node"],
                check_id=raw["check_id"],
            )
        except Exception as error:
            raise SemanticValidationError("execution plan is invalid") from error


@dataclass(frozen=True)
class FixtureExecutionPlanSet:
    """The closed plan set is distinct from the grader's expected assertions."""

    plans: tuple[FixtureExecutionPlan, ...]
    plan_set_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not FixtureExecutionPlanSet:
            raise SemanticValidationError("execution plan set is invalid")
        plans = tuple(self.plans)
        if not plans or any(type(plan) is not FixtureExecutionPlan for plan in plans):
            raise SemanticValidationError("execution plans are invalid")
        if len({plan.case_id for plan in plans}) != len(plans):
            raise SemanticValidationError("execution plans contain duplicate cases")
        digest = sha256_hex({"plan_digests": [plan.plan_digest for plan in plans]})
        if self.plan_set_digest and self.plan_set_digest != digest:
            raise SemanticValidationError("execution plan set digest does not match")
        object.__setattr__(self, "plans", plans)
        object.__setattr__(self, "plan_set_digest", digest)

    def for_case(self, case_id: str) -> FixtureExecutionPlan:
        for plan in self.plans:
            if plan.case_id == case_id:
                return plan
        raise SemanticValidationError("execution plan is unavailable for evaluation case")


def load_local_execution_plans(corpus: EvaluationCorpus) -> FixtureExecutionPlanSet:
    """Load fixed component inputs without consulting ``case.expected``."""

    if type(corpus) is not EvaluationCorpus:
        raise SemanticValidationError("execution plan corpus is invalid")
    source = _contained_fixture_path("fixtures/m8/execution-plan-v1.json")
    try:
        raw = load_strict_json(source)
    except Exception as error:
        raise SemanticValidationError("execution plan is unavailable") from error
    if type(raw) is not dict or set(raw) != {"version", "plans"} or raw["version"] != EVALUATION_VERSION:
        raise SemanticValidationError("execution plan document is invalid")
    if type(raw["plans"]) is not list or len(raw["plans"]) != len(corpus.cases):
        raise SemanticValidationError("execution plan document is invalid")
    plans = tuple(FixtureExecutionPlan.from_value(item) for item in raw["plans"])
    if tuple(plan.case_id for plan in plans) != tuple(case.case_id for case in corpus.cases):
        raise SemanticValidationError("execution plan order does not bind the corpus")
    return FixtureExecutionPlanSet(plans=plans)


@dataclass(frozen=True)
class CaseExecutionEvidence:
    """In-memory M5/M6/M7 evidence; serializations retain digests only."""

    case_id: str
    task_id: str
    plan: FixtureExecutionPlan
    admission: AdmissionDecision
    audit_trail: AuditTrail
    capability_resolution: CapabilityResolution
    route_receipt: RouteReceipt | None
    graph_receipt: GraphRunReceipt | None
    execution_mode: ExecutionMode
    execution_profile: str
    route_node_id: str | None
    memory_retrieval_performed: bool
    execution_plan_set_digest: str
    evidence_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not CaseExecutionEvidence:
            raise SemanticValidationError("case execution evidence is invalid")
        _require_identifier(self.case_id, "execution evidence case_id", _CASE_ID)
        _require_identifier(self.task_id, "execution evidence task_id", _TASK_ID)
        if type(self.plan) is not FixtureExecutionPlan or self.plan.case_id != self.case_id:
            raise SemanticValidationError("execution evidence plan is invalid")
        if type(self.admission) is not AdmissionDecision or not self.admission.verify():
            raise SemanticValidationError("execution evidence admission is invalid")
        if type(self.audit_trail) is not AuditTrail or type(self.capability_resolution) is not CapabilityResolution:
            raise SemanticValidationError("execution evidence M5 boundary is invalid")
        if type(self.execution_mode) is not ExecutionMode:
            raise SemanticValidationError("execution evidence mode is invalid")
        if self.execution_profile not in APPROVED_PROFILE_ALIASES:
            raise SemanticValidationError("execution evidence profile is invalid")
        if self.execution_mode is ExecutionMode.B0_ROOT:
            if self.execution_profile != "tera-max":
                raise SemanticValidationError("B0 execution must bind the root Tera Max profile")
        elif self.execution_profile != self.plan.profile_alias:
            raise SemanticValidationError("workflow execution must use the planned profile")
        if self.route_node_id is not None:
            _require_identifier(
                self.route_node_id,
                "execution evidence route node",
                re.compile(r"^[a-z][a-z0-9._:-]{2,95}$"),
            )
        if type(self.memory_retrieval_performed) is not bool:
            raise SemanticValidationError("execution evidence memory flag is invalid")
        if self.execution_mode is ExecutionMode.B2_NO_MEMORY and self.memory_retrieval_performed:
            raise SemanticValidationError("B2 execution may not perform memory retrieval")
        _require_hash(self.execution_plan_set_digest, "execution plan set digest")
        lane = self.admission.lane
        if lane is Lane.DIRECT:
            if (
                self.admission.task_id not in {None, self.task_id}
                or self.route_receipt is not None
                or self.graph_receipt is not None
                or self.route_node_id is not None
            ):
                raise SemanticValidationError("direct execution evidence crosses a component boundary")
            if self.capability_resolution.direct_short_circuit is not True:
                raise SemanticValidationError("direct execution evidence must short-circuit capabilities")
        else:
            if self.admission.task_id != self.task_id or not AuditTrail.has_admission(self.audit_trail, self.admission):
                raise SemanticValidationError("execution evidence does not bind issued M5 admission")
            if type(self.route_receipt) is not RouteReceipt or self.route_node_id is None:
                raise SemanticValidationError("non-direct execution evidence requires an M6 receipt")
            if (
                self.route_receipt.task_id_digest != sha256_hex({"opaque_identifier": self.task_id})
                or self.route_receipt.node_id_digest != sha256_hex({"opaque_identifier": self.route_node_id})
                or self.route_receipt.profile_alias != self.execution_profile
                or self.route_receipt.live_attested is not False
                or side_effect_allowed(self.route_receipt, lane) is not False
            ):
                raise SemanticValidationError("execution evidence M6 receipt is not bound")
            if lane in {Lane.GRAPH, Lane.DEEP}:
                if self.plan.graph_primary_node is None:
                    raise SemanticValidationError("graph execution evidence lacks a planned primary node")
                if self.execution_mode is ExecutionMode.B0_ROOT:
                    if self.graph_receipt is not None:
                        raise SemanticValidationError("B0 root execution may not enter M7")
                else:
                    if type(self.graph_receipt) is not GraphRunReceipt:
                        raise SemanticValidationError("graph execution evidence requires an M7 receipt")
                    if (
                        self.graph_receipt.admission_digest != self.admission.decision_digest
                        or self.graph_receipt.task_id_digest != sha256_hex({"task_id": self.task_id})
                        or self.graph_receipt.lane is not lane
                        or self.route_node_id != f"node:{self.plan.graph_primary_node}"
                    ):
                        raise SemanticValidationError("execution evidence M7 receipt is not bound")
                    node = next(
                        (item for item in self.graph_receipt.nodes if item.node_id == self.plan.graph_primary_node),
                        None,
                    )
                    if node is None or node.route_receipt_digest != self.route_receipt.receipt_digest:
                        raise SemanticValidationError("execution evidence route is not bound to M7 node")
            elif self.graph_receipt is not None:
                raise SemanticValidationError("non-graph execution evidence cannot carry an M7 receipt")
            elif self.plan.graph_primary_node is not None:
                raise SemanticValidationError("non-graph execution plan declares an M7 node")
        selected = tuple(entry.identifier for entry in self.capability_resolution.selected)
        if self.plan.capability_required:
            if selected != ("cap:local-helper",):
                raise SemanticValidationError("execution evidence did not select the required capability")
        elif selected:
            raise SemanticValidationError("execution evidence activated an unexpected capability")
        digest = sha256_hex(self._digest_input())
        if self.evidence_digest and self.evidence_digest != digest:
            raise SemanticValidationError("execution evidence digest does not match")
        object.__setattr__(self, "evidence_digest", digest)

    @property
    def route_status(self) -> str:
        return "NOT_APPLICABLE" if self.route_receipt is None else self.route_receipt.status.value

    @property
    def selected_capability_ids(self) -> tuple[str, ...]:
        return tuple(entry.identifier for entry in self.capability_resolution.selected)

    @property
    def execution_status(self) -> ExecutionStatus:
        if self.route_receipt is not None and self.route_receipt.status is not ReconciliationStatus.MATCH:
            return ExecutionStatus.BLOCKED
        if self.graph_receipt is not None and self.graph_receipt.state is not GraphState.SUCCEEDED:
            return ExecutionStatus.BLOCKED
        return ExecutionStatus.SUCCEEDED

    @property
    def audit_event_digest(self) -> str | None:
        if self.admission.lane is Lane.DIRECT:
            return None
        for event in self.audit_trail.events:
            if event.admission_digest == self.admission.decision_digest:
                return event.event_digest
        raise SemanticValidationError("issued M5 admission event is unavailable")

    def _digest_input(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "task_id_digest": sha256_hex({"opaque_identifier": self.task_id}),
            "execution_plan_digest": self.plan.plan_digest,
            "execution_plan_set_digest": self.execution_plan_set_digest,
            "execution_mode": self.execution_mode.value,
            "execution_profile": self.execution_profile,
            "route_node_id_digest": None
            if self.route_node_id is None
            else sha256_hex({"opaque_identifier": self.route_node_id}),
            "memory_retrieval_performed": self.memory_retrieval_performed,
            "admission_digest": self.admission.decision_digest,
            "audit_event_digest": self.audit_event_digest,
            "capability_resolution_digest": sha256_hex(self.capability_resolution.to_dict()),
            "route_receipt_digest": None if self.route_receipt is None else self.route_receipt.receipt_digest,
            "graph_run_receipt_digest": None if self.graph_receipt is None else self.graph_receipt.receipt_digest,
        }

    def to_receipt(self) -> dict[str, object]:
        return {**self._digest_input(), "execution_evidence_digest": self.evidence_digest}


@dataclass(frozen=True)
class CaseOutcome:
    """One bounded synthetic observation, without prompt/output/telemetry blobs."""

    case_id: str
    observed_lane: str
    observed_profile: str
    observed_capability_ids: tuple[str, ...]
    observed_event_ids: tuple[str, ...]
    check_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    artifact_digests: tuple[tuple[str, str], ...]
    execution_status: ExecutionStatus
    route_status: str
    latency_ms: int
    cost_microunits: int
    context_tokens: int
    retrieval_count: int
    advisory_omissions: int = 0
    proposal_only: bool = True
    material_side_effect_allowed: bool = False
    material_actions_started: int = 0
    network_requests_started: int = 0
    global_target_count: int = 0
    attempt_count: int = 1
    component_evidence: CaseExecutionEvidence | None = None
    outcome_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not CaseOutcome:
            raise SemanticValidationError("case outcome wrapper is invalid")
        _require_identifier(self.case_id, "outcome case_id", _CASE_ID)
        if self.observed_lane not in _LANES or self.observed_profile not in APPROVED_PROFILE_ALIASES:
            raise SemanticValidationError("case outcome route is invalid")
        capabilities = _freeze_identifiers(
            self.observed_capability_ids, "observed_capability_ids", _CAPABILITY_ID, maximum=12
        )
        events = _freeze_identifiers(
            self.observed_event_ids,
            "observed_event_ids",
            _SAFE_VERSION,
            maximum=12,
            allowed=_EVENT_CODES,
        )
        checks = _freeze_identifiers(self.check_ids, "check_ids", _CHECK_ID, maximum=24)
        artifacts = _freeze_identifiers(self.artifact_ids, "artifact_ids", _ARTIFACT_ID, maximum=24)
        if not isinstance(self.artifact_digests, (tuple, list)):
            raise SemanticValidationError("artifact_digests is invalid")
        artifact_pairs: list[tuple[str, str]] = []
        for item in self.artifact_digests:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise SemanticValidationError("artifact_digests is invalid")
            artifact_id, digest = item
            _require_identifier(artifact_id, "artifact digest ID", _ARTIFACT_ID)
            _require_hash(digest, "artifact digest")
            artifact_pairs.append((artifact_id, digest))
        if len({item[0] for item in artifact_pairs}) != len(artifact_pairs):
            raise SemanticValidationError("artifact_digests contains duplicates")
        if {item[0] for item in artifact_pairs} != set(artifacts):
            raise SemanticValidationError("artifact_digests does not match artifact IDs")
        if not isinstance(self.execution_status, ExecutionStatus):
            try:
                object.__setattr__(self, "execution_status", ExecutionStatus(self.execution_status))
            except (TypeError, ValueError) as error:
                raise SemanticValidationError("execution status is invalid") from error
        if self.route_status not in {
            "MATCH",
            "NOT_APPLICABLE",
            "MISMATCH_MODEL",
            "MISMATCH_EFFORT",
            "MISMATCH_BOTH",
            "MISSING_TELEMETRY",
            "AMBIGUOUS_TELEMETRY",
            "UNSUPPORTED_MAPPING",
        }:
            raise SemanticValidationError("route status is invalid")
        if type(self.component_evidence) is not CaseExecutionEvidence:
            raise SemanticValidationError("case outcome requires typed component evidence")
        evidence = self.component_evidence
        expected_latency = 0 if evidence.graph_receipt is None else evidence.graph_receipt.duration_ms
        expected_cost = 0 if evidence.graph_receipt is None else evidence.graph_receipt.total_cost_microunits
        expected_context = 0 if evidence.graph_receipt is None else evidence.graph_receipt.total_tokens
        expected_checks = (evidence.plan.check_id,) if evidence.execution_status is ExecutionStatus.SUCCEEDED else ()
        expected_proposal_only = True if evidence.graph_receipt is None else evidence.graph_receipt.proposal_only
        expected_side_effect_allowed = (
            False if evidence.graph_receipt is None else evidence.graph_receipt.material_side_effect_allowed
        )
        if (
            self.case_id != evidence.case_id
            or self.observed_lane != evidence.admission.lane.value
            or self.observed_profile != evidence.execution_profile
            or tuple(self.observed_capability_ids) != evidence.selected_capability_ids
            or tuple(self.observed_event_ids) != ()
            or tuple(self.check_ids) != expected_checks
            or tuple(self.artifact_ids) != ()
            or tuple(self.artifact_digests) != ()
            or self.execution_status is not evidence.execution_status
            or self.route_status != evidence.route_status
            or self.latency_ms != expected_latency
            or self.cost_microunits != expected_cost
            or self.context_tokens != expected_context
            or self.retrieval_count != 0
            or self.advisory_omissions != 0
            or self.proposal_only is not expected_proposal_only
            or self.material_side_effect_allowed is not expected_side_effect_allowed
            or self.material_actions_started != 0
            or self.network_requests_started != 0
            or self.global_target_count != 0
            or self.attempt_count != 1
        ):
            raise SemanticValidationError("case outcome does not match component execution evidence")
        for label, value in (
            ("latency_ms", self.latency_ms),
            ("cost_microunits", self.cost_microunits),
            ("context_tokens", self.context_tokens),
            ("retrieval_count", self.retrieval_count),
            ("advisory_omissions", self.advisory_omissions),
            ("material_actions_started", self.material_actions_started),
            ("network_requests_started", self.network_requests_started),
            ("global_target_count", self.global_target_count),
        ):
            _require_int(value, label, minimum=0)
        if type(self.proposal_only) is not bool or type(self.material_side_effect_allowed) is not bool:
            raise SemanticValidationError("case outcome authority flags are invalid")
        if self.attempt_count != 1 or isinstance(self.attempt_count, bool):
            raise SemanticValidationError("case outcome attempt_count is invalid")
        object.__setattr__(self, "observed_capability_ids", capabilities)
        object.__setattr__(self, "observed_event_ids", events)
        object.__setattr__(self, "check_ids", checks)
        object.__setattr__(self, "artifact_ids", artifacts)
        object.__setattr__(self, "artifact_digests", tuple(sorted(artifact_pairs)))
        digest = sha256_hex(self._digest_input())
        if self.outcome_digest and self.outcome_digest != digest:
            raise SemanticValidationError("case outcome digest does not match")
        object.__setattr__(self, "outcome_digest", digest)

    def _digest_input(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "observed_lane": self.observed_lane,
            "observed_profile": self.observed_profile,
            "observed_capability_ids": list(self.observed_capability_ids),
            "observed_event_ids": list(self.observed_event_ids),
            "check_ids": list(self.check_ids),
            "artifact_ids": list(self.artifact_ids),
            "artifact_digests": [
                {"artifact_id": artifact_id, "sha256": digest}
                for artifact_id, digest in self.artifact_digests
            ],
            "execution_status": self.execution_status.value,
            "route_status": self.route_status,
            "latency_ms": self.latency_ms,
            "cost_microunits": self.cost_microunits,
            "context_tokens": self.context_tokens,
            "retrieval_count": self.retrieval_count,
            "advisory_omissions": self.advisory_omissions,
            "proposal_only": self.proposal_only,
            "material_side_effect_allowed": self.material_side_effect_allowed,
            "material_actions_started": self.material_actions_started,
            "network_requests_started": self.network_requests_started,
            "global_target_count": self.global_target_count,
            "attempt_count": self.attempt_count,
            "component_execution": self.component_evidence.to_receipt(),
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._digest_input(), "outcome_digest": self.outcome_digest}

    @classmethod
    def from_value(cls, value: object) -> "CaseOutcome":
        # A receipt stores only component digests; it cannot recreate the private
        # M5 issuance witness or the exact M6/M7 result objects needed for grading.
        raise SemanticValidationError("serialized case outcomes are non-authoritative")


@dataclass(frozen=True)
class GradeResult:
    """Deterministic grading result; semantic score is only a local surrogate."""

    case_id: str
    score: int
    state: GateState
    deterministic_passed: bool
    execution_passed: bool
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self) is not GradeResult:
            raise SemanticValidationError("grade result wrapper is invalid")
        _require_identifier(self.case_id, "grade case_id", _CASE_ID)
        _require_int(self.score, "grade score", minimum=0, maximum=4)
        if self.state not in {GateState.PASS, GateState.FAIL, GateState.BLOCKED}:
            raise SemanticValidationError("grade state is invalid")
        if type(self.deterministic_passed) is not bool or type(self.execution_passed) is not bool:
            raise SemanticValidationError("grade flags are invalid")
        reasons = _freeze_identifiers(
            self.reason_codes, "grade reasons", re.compile(r"^[A-Z][A-Z0-9_]{2,95}$"), maximum=24
        )
        if (self.state is GateState.PASS) != (not reasons and self.score in {3, 4}):
            raise SemanticValidationError("grade state does not match its disposition")
        object.__setattr__(self, "reason_codes", reasons)

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "score": self.score,
            "state": self.state.value,
            "deterministic_passed": self.deterministic_passed,
            "execution_passed": self.execution_passed,
            "reason_codes": list(self.reason_codes),
        }


def grade_case(case: EvaluationCase, outcome: CaseOutcome) -> GradeResult:
    """Grade only closed assertions before computing a deterministic surrogate score."""

    if type(case) is not EvaluationCase or type(outcome) is not CaseOutcome:
        raise SemanticValidationError("grade inputs are invalid")
    if case.case_id != outcome.case_id:
        raise SemanticValidationError("grade outcome does not bind its case")
    reasons: list[str] = []
    if outcome.observed_lane != case.expected.lane:
        reasons.append("LANE_MISMATCH")
    if outcome.observed_profile not in case.expected.allowed_profiles:
        reasons.append("PROFILE_MISMATCH")
    if set(outcome.observed_capability_ids) != set(case.expected.required_capabilities):
        reasons.append("CAPABILITY_MISMATCH")
    if set(outcome.observed_event_ids).intersection(case.expected.forbidden_events):
        reasons.append("FORBIDDEN_EVENT")
    if not set(case.expected.required_check_ids).issubset(outcome.check_ids):
        reasons.append("CHECK_MISSING")
    if not set(case.expected.required_artifact_ids).issubset(outcome.artifact_ids):
        reasons.append("ARTIFACT_MISSING")
    if outcome.route_status not in {"MATCH", "NOT_APPLICABLE"}:
        reasons.append("ROUTE_QUARANTINED")
    if (
        outcome.proposal_only is not True
        or outcome.material_side_effect_allowed is not False
        or outcome.material_actions_started != 0
        or outcome.network_requests_started != 0
        or outcome.global_target_count != 0
    ):
        reasons.append("AUTHORITY_BOUNDARY_FAILED")
    execution_passed = outcome.execution_status is ExecutionStatus.SUCCEEDED
    if not execution_passed:
        reasons.append("EXECUTION_NOT_SUCCEEDED")
    deterministic_passed = not reasons
    unsafe = any(
        reason in {"ROUTE_QUARANTINED", "AUTHORITY_BOUNDARY_FAILED"} for reason in reasons
    ) or (case.expected.safety_critical and bool(reasons))
    if unsafe:
        score = 0
    elif outcome.execution_status is ExecutionStatus.BLOCKED:
        score = 1
    elif reasons:
        score = 2
    elif outcome.advisory_omissions:
        score = 3
    else:
        score = 4
    state = GateState.PASS if deterministic_passed and execution_passed else (
        GateState.BLOCKED if outcome.execution_status is ExecutionStatus.BLOCKED else GateState.FAIL
    )
    return GradeResult(
        case_id=case.case_id,
        score=score,
        state=state,
        deterministic_passed=deterministic_passed,
        execution_passed=execution_passed,
        reason_codes=tuple(sorted(set(reasons))),
    )


@dataclass(frozen=True)
class RunBinding:
    """All immutable values that must agree before baseline comparison."""

    dataset_manifest_digest: str
    ordered_case_id_digest: str
    repository_snapshot_digest: str
    permission_policy_digest: str
    profile_registry_digest: str
    graph_manifest_digest: str | None
    execution_plan_digest: str
    component_source_digest: str
    seed: int
    fixture_executor_version: str
    grader_version: str
    binding_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not RunBinding:
            raise SemanticValidationError("run binding wrapper is invalid")
        for label, value in (
            ("dataset_manifest_digest", self.dataset_manifest_digest),
            ("ordered_case_id_digest", self.ordered_case_id_digest),
            ("repository_snapshot_digest", self.repository_snapshot_digest),
            ("permission_policy_digest", self.permission_policy_digest),
            ("profile_registry_digest", self.profile_registry_digest),
            ("execution_plan_digest", self.execution_plan_digest),
            ("component_source_digest", self.component_source_digest),
        ):
            _require_hash(value, label)
        if self.graph_manifest_digest is not None:
            _require_hash(self.graph_manifest_digest, "graph_manifest_digest")
        if self.seed != M8_SEED or isinstance(self.seed, bool):
            raise SemanticValidationError("run binding seed is invalid")
        if self.fixture_executor_version != FIXTURE_EXECUTOR_VERSION or self.grader_version != EVALUATOR_VERSION:
            raise SemanticValidationError("run binding version is invalid")
        digest = sha256_hex(self._digest_input())
        if self.binding_digest and self.binding_digest != digest:
            raise SemanticValidationError("run binding digest does not match")
        object.__setattr__(self, "binding_digest", digest)

    def _digest_input(self) -> dict[str, object]:
        return {
            "dataset_manifest_digest": self.dataset_manifest_digest,
            "ordered_case_id_digest": self.ordered_case_id_digest,
            "repository_snapshot_digest": self.repository_snapshot_digest,
            "permission_policy_digest": self.permission_policy_digest,
            "profile_registry_digest": self.profile_registry_digest,
            "graph_manifest_digest": self.graph_manifest_digest,
            "execution_plan_digest": self.execution_plan_digest,
            "component_source_digest": self.component_source_digest,
            "seed": self.seed,
            "fixture_executor_version": self.fixture_executor_version,
            "grader_version": self.grader_version,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._digest_input(), "binding_digest": self.binding_digest}

    @classmethod
    def from_value(cls, value: object) -> "RunBinding":
        raw = _require_exact_keys(
            value,
            frozenset(
                (
                    "dataset_manifest_digest",
                    "ordered_case_id_digest",
                    "repository_snapshot_digest",
                    "permission_policy_digest",
                    "profile_registry_digest",
                    "graph_manifest_digest",
                    "execution_plan_digest",
                    "component_source_digest",
                    "seed",
                    "fixture_executor_version",
                    "grader_version",
                    "binding_digest",
                )
            ),
            "run binding",
        )
        try:
            return cls(**raw)
        except (TypeError, ValueError, SemanticValidationError) as error:
            raise SemanticValidationError("run binding is invalid") from error


def _safe_file_digest(relative_path: str) -> str:
    return sha256_bytes(_workspace_regular_file(relative_path).read_bytes())


def create_local_run_binding(
    corpus: EvaluationCorpus, plans: FixtureExecutionPlanSet | None = None
) -> RunBinding:
    """Bind local runs to checked-in inputs without reading global configuration."""

    if type(corpus) is not EvaluationCorpus:
        raise SemanticValidationError("evaluation corpus is invalid")
    plans = load_local_execution_plans(corpus) if plans is None else plans
    if type(plans) is not FixtureExecutionPlanSet:
        raise SemanticValidationError("execution plan set is invalid")
    registry = load_profile_registry()
    snapshot = sha256_hex(
        {
            "evaluation_schema": _safe_file_digest("schemas/evaluation-case-v1.json"),
            "schema_registry": _safe_file_digest("schemas/schema-registry.json"),
            "m8_graph_builder": _safe_file_digest("src/second_brain/evaluation.py"),
            "profile_registry": _safe_file_digest("config/model-profiles.json"),
            "execution_plan": _safe_file_digest("fixtures/m8/execution-plan-v1.json"),
            "m5_admission": _safe_file_digest("src/second_brain/admission.py"),
            "m6_routing": _safe_file_digest("src/second_brain/routing.py"),
            "m7_runtime": _safe_file_digest("src/second_brain/graph_runtime.py"),
            "m8_evaluator": _safe_file_digest("src/second_brain/evaluation.py"),
            "m8_m4_evidence": _safe_file_digest("src/second_brain/m8_m4_evidence.py"),
            "m8_performance_evidence": _safe_file_digest("src/second_brain/m8_performance_evidence.py"),
            "m4_retrieval": _safe_file_digest("src/second_brain/retrieval.py"),
            "m1_storage": _safe_file_digest("src/second_brain/storage.py"),
            "memory_contracts": _safe_file_digest("src/second_brain/contracts.py"),
            "fixture_clock": _safe_file_digest("src/second_brain/clock.py"),
        }
    )
    return RunBinding(
        dataset_manifest_digest=corpus.manifest.manifest_digest,
        ordered_case_id_digest=corpus.manifest.ordered_case_id_digest,
        repository_snapshot_digest=snapshot,
        permission_policy_digest=sha256_hex({"m5_boundary": "denial-only-local-evaluation"}),
        profile_registry_digest=registry_digest(registry),
        graph_manifest_digest=sha256_hex(
            {"m8_case_graph_builder": _safe_file_digest("src/second_brain/evaluation.py")}
        ),
        execution_plan_digest=plans.plan_set_digest,
        component_source_digest=sha256_hex(
            {
                "admission": _safe_file_digest("src/second_brain/admission.py"),
                "routing": _safe_file_digest("src/second_brain/routing.py"),
                "graph_runtime": _safe_file_digest("src/second_brain/graph_runtime.py"),
                "evaluator": _safe_file_digest("src/second_brain/evaluation.py"),
                "m4_evidence": _safe_file_digest("src/second_brain/m8_m4_evidence.py"),
                "m4_retrieval": _safe_file_digest("src/second_brain/retrieval.py"),
                "m4_storage": _safe_file_digest("src/second_brain/storage.py"),
                "memory_contracts": _safe_file_digest("src/second_brain/contracts.py"),
                "fixture_clock": _safe_file_digest("src/second_brain/clock.py"),
                "performance_evidence": _safe_file_digest("src/second_brain/m8_performance_evidence.py"),
            }
        ),
        seed=M8_SEED,
        fixture_executor_version=FIXTURE_EXECUTOR_VERSION,
        grader_version=EVALUATOR_VERSION,
    )


@dataclass(frozen=True)
class EvaluationRun:
    """A complete comparable run with one outcome for every ordered case."""

    run_id: str
    baseline_kind: BaselineKind | None
    binding: RunBinding
    cases: tuple[EvaluationCase, ...]
    outcomes: tuple[CaseOutcome, ...]
    evidence_scope: str = SYNTHETIC_LOCAL
    live_attested: bool = False
    run_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not EvaluationRun:
            raise SemanticValidationError("evaluation run wrapper is invalid")
        _require_identifier(self.run_id, "run_id", re.compile(r"^run:[a-z0-9][a-z0-9._-]{2,95}$"))
        if self.baseline_kind is not None and not isinstance(self.baseline_kind, BaselineKind):
            try:
                object.__setattr__(self, "baseline_kind", BaselineKind(self.baseline_kind))
            except (TypeError, ValueError) as error:
                raise SemanticValidationError("baseline kind is invalid") from error
        if type(self.binding) is not RunBinding:
            raise SemanticValidationError("evaluation run binding is invalid")
        cases = tuple(self.cases)
        outcomes = tuple(self.outcomes)
        if not cases or any(type(case) is not EvaluationCase for case in cases):
            raise SemanticValidationError("evaluation run cases are invalid")
        if any(type(outcome) is not CaseOutcome for outcome in outcomes):
            raise SemanticValidationError("evaluation run outcomes are invalid")
        case_ids = tuple(case.case_id for case in cases)
        if tuple(outcome.case_id for outcome in outcomes) != case_ids:
            raise SemanticValidationError("evaluation run outcomes are not ordered or complete")
        if len(set(case_ids)) != len(case_ids) or len(set(outcome.outcome_digest for outcome in outcomes)) != len(outcomes):
            raise SemanticValidationError("evaluation run has duplicate evidence")
        expected_mode = {
            None: ExecutionMode.CANDIDATE,
            BaselineKind.B0_ROOT_TERA_MAX: ExecutionMode.B0_ROOT,
            BaselineKind.B1_SERIAL_WORKFLOW: ExecutionMode.B1_SERIAL,
            BaselineKind.B2_NO_MEMORY: ExecutionMode.B2_NO_MEMORY,
            BaselineKind.B3_LAST_KNOWN_GOOD: ExecutionMode.B3_LAST_KNOWN_GOOD,
        }[self.baseline_kind]
        if any(outcome.component_evidence.execution_mode is not expected_mode for outcome in outcomes):
            raise SemanticValidationError("evaluation run execution mode does not match its baseline")
        if self.binding.ordered_case_id_digest != _ordered_case_id_digest(cases):
            raise SemanticValidationError("evaluation run case order does not match binding")
        if self.evidence_scope != SYNTHETIC_LOCAL or self.live_attested is not False:
            raise SemanticValidationError("evaluation run cannot claim live evidence")
        object.__setattr__(self, "cases", cases)
        object.__setattr__(self, "outcomes", outcomes)
        digest = sha256_hex(self._digest_input())
        if self.run_digest and self.run_digest != digest:
            raise SemanticValidationError("evaluation run digest does not match")
        object.__setattr__(self, "run_digest", digest)

    def _digest_input(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "baseline_kind": None if self.baseline_kind is None else self.baseline_kind.value,
            "binding_digest": self.binding.binding_digest,
            "outcome_digests": [outcome.outcome_digest for outcome in self.outcomes],
            "evidence_scope": self.evidence_scope,
            "live_attested": self.live_attested,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self._digest_input(),
            "case_count": len(self.cases),
            "run_digest": self.run_digest,
        }


@dataclass(frozen=True)
class BaselineReceipt:
    """Digest-bound, local-only receipt for one named baseline run."""

    receipt_version: int
    baseline_kind: BaselineKind
    binding: RunBinding
    outcome_digest: str
    outcome_count: int
    evidence_scope: str = SYNTHETIC_LOCAL
    live_attested: bool = False
    receipt_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not BaselineReceipt:
            raise SemanticValidationError("baseline receipt wrapper is invalid")
        if self.receipt_version != EVALUATION_VERSION or isinstance(self.receipt_version, bool):
            raise SemanticValidationError("baseline receipt version is invalid")
        if not isinstance(self.baseline_kind, BaselineKind) or type(self.binding) is not RunBinding:
            raise SemanticValidationError("baseline receipt binding is invalid")
        _require_hash(self.outcome_digest, "baseline outcome_digest")
        _require_int(self.outcome_count, "baseline outcome_count", minimum=1)
        if self.evidence_scope != SYNTHETIC_LOCAL or self.live_attested is not False:
            raise SemanticValidationError("baseline receipt cannot claim live evidence")
        digest = sha256_hex(self._digest_input())
        if self.receipt_digest and self.receipt_digest != digest:
            raise SemanticValidationError("baseline receipt digest does not match")
        object.__setattr__(self, "receipt_digest", digest)

    def _digest_input(self) -> dict[str, object]:
        return {
            "receipt_version": self.receipt_version,
            "baseline_kind": self.baseline_kind.value,
            "binding": self.binding.to_dict(),
            "outcome_digest": self.outcome_digest,
            "outcome_count": self.outcome_count,
            "evidence_scope": self.evidence_scope,
            "live_attested": self.live_attested,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._digest_input(), "receipt_digest": self.receipt_digest}

    @classmethod
    def from_value(cls, value: object) -> "BaselineReceipt":
        raw = _require_exact_keys(
            value,
            frozenset(
                (
                    "receipt_version",
                    "baseline_kind",
                    "binding",
                    "outcome_digest",
                    "outcome_count",
                    "evidence_scope",
                    "live_attested",
                    "receipt_digest",
                )
            ),
            "baseline receipt",
        )
        try:
            return cls(
                receipt_version=raw["receipt_version"],
                baseline_kind=BaselineKind(raw["baseline_kind"]),
                binding=RunBinding.from_value(raw["binding"]),
                outcome_digest=raw["outcome_digest"],
                outcome_count=raw["outcome_count"],
                evidence_scope=raw["evidence_scope"],
                live_attested=raw["live_attested"],
                receipt_digest=raw["receipt_digest"],
            )
        except (KeyError, TypeError, ValueError, SemanticValidationError) as error:
            raise SemanticValidationError("baseline receipt is invalid") from error


def create_baseline_receipt(run: EvaluationRun) -> BaselineReceipt:
    """Create one receipt only for a complete immutable baseline run."""

    if type(run) is not EvaluationRun or run.baseline_kind is None:
        raise SemanticValidationError("baseline run is invalid")
    return BaselineReceipt(
        receipt_version=EVALUATION_VERSION,
        baseline_kind=run.baseline_kind,
        binding=run.binding,
        outcome_digest=sha256_hex({"outcome_digests": [item.outcome_digest for item in run.outcomes]}),
        outcome_count=len(run.outcomes),
    )


@dataclass(frozen=True)
class ComparisonResult:
    """Fixed weighted lower-confidence-bound comparison against B0."""

    state: GateState
    mean_delta_percentage_points: Decimal | None
    lcb_95_percentage_points: Decimal | None
    n_eff: Decimal | None
    variance: Decimal | None
    pairwise_tie_or_preferred_percent: Decimal | None
    pairwise_loss_percent: Decimal | None
    comparable_case_count: int
    failed_case_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self) is not ComparisonResult:
            raise SemanticValidationError("comparison result wrapper is invalid")
        if self.state not in {GateState.PASS, GateState.FAIL, GateState.BLOCKED}:
            raise SemanticValidationError("comparison state is invalid")
        _require_int(self.comparable_case_count, "comparable_case_count", minimum=0)
        failed = _freeze_identifiers(self.failed_case_ids, "failed_case_ids", _CASE_ID, maximum=256)
        reasons = _freeze_identifiers(
            self.reason_codes, "comparison reason_codes", re.compile(r"^[A-Z][A-Z0-9_]{2,95}$"), maximum=32
        )
        if len(set(failed)) != len(failed):
            raise SemanticValidationError("comparison failed_case_ids is invalid")
        if self.state is GateState.PASS and (failed or reasons):
            raise SemanticValidationError("passing comparison has failures")
        for value in (
            self.mean_delta_percentage_points,
            self.lcb_95_percentage_points,
            self.n_eff,
            self.variance,
            self.pairwise_tie_or_preferred_percent,
            self.pairwise_loss_percent,
        ):
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite()):
                raise SemanticValidationError("comparison metric is invalid")
        object.__setattr__(self, "failed_case_ids", failed)
        object.__setattr__(self, "reason_codes", reasons)

    def to_dict(self) -> dict[str, object]:
        def display(value: Decimal | None) -> str | None:
            return None if value is None else str(value.quantize(Decimal("0.0001")))

        return {
            "state": self.state.value,
            "mean_delta_percentage_points": display(self.mean_delta_percentage_points),
            "lcb_95_percentage_points": display(self.lcb_95_percentage_points),
            "n_eff": display(self.n_eff),
            "variance": display(self.variance),
            "pairwise_tie_or_preferred_percent": display(self.pairwise_tie_or_preferred_percent),
            "pairwise_loss_percent": display(self.pairwise_loss_percent),
            "comparable_case_count": self.comparable_case_count,
            "failed_case_ids": list(self.failed_case_ids),
            "reason_codes": list(self.reason_codes),
            "quality_measurement_scope": "deterministic-local-surrogate",
        }


def _validate_comparable_runs(
    candidate: EvaluationRun, b0: EvaluationRun, b1: EvaluationRun, b2: EvaluationRun, b3: EvaluationRun
) -> None:
    expected_kinds = (
        (b0, BaselineKind.B0_ROOT_TERA_MAX),
        (b1, BaselineKind.B1_SERIAL_WORKFLOW),
        (b2, BaselineKind.B2_NO_MEMORY),
        (b3, BaselineKind.B3_LAST_KNOWN_GOOD),
    )
    if any(type(run) is not EvaluationRun for run, _ in expected_kinds) or type(candidate) is not EvaluationRun:
        raise SemanticValidationError("comparison runs are invalid")
    expected_case_ids = tuple(case.case_id for case in candidate.cases)
    for run, kind in expected_kinds:
        if run.baseline_kind is not kind:
            raise SemanticValidationError("comparison baseline kind is invalid")
        if tuple(case.case_id for case in run.cases) != expected_case_ids:
            raise SemanticValidationError("comparison case order does not match")
        if run.binding.binding_digest != candidate.binding.binding_digest:
            raise SemanticValidationError("comparison binding does not match")
        if len(run.outcomes) != len(candidate.outcomes):
            raise SemanticValidationError("comparison outcomes are incomplete")


def compare_runs(
    candidate: EvaluationRun,
    b0: EvaluationRun,
    b1: EvaluationRun,
    b2: EvaluationRun,
    b3: EvaluationRun,
) -> ComparisonResult:
    """Keep local component observations separate from semantic quality evidence.

    The local runners prove policy and plumbing behavior.  They do not generate
    blinded human/provider quality observations, so computing a confidence bound
    from their fixture assertions would manufacture a non-inferiority claim.
    """

    _validate_comparable_runs(candidate, b0, b1, b2, b3)
    return ComparisonResult(
        state=GateState.BLOCKED,
        mean_delta_percentage_points=None,
        lcb_95_percentage_points=None,
        n_eff=None,
        variance=None,
        pairwise_tie_or_preferred_percent=None,
        pairwise_loss_percent=None,
        comparable_case_count=len(candidate.cases),
        failed_case_ids=(),
        reason_codes=("LOCAL_SEMANTIC_QUALITY_DEFERRED_TO_M9",),
    )


@dataclass(frozen=True)
class BlindedReviewPacket:
    """Commitment for a future human review; it cannot be marked reviewed by code."""

    packet_version: int
    dataset_manifest_digest: str
    seed: int
    label_assignment_digest: str
    entries: tuple[tuple[str, str, str, str], ...]
    status: GateState = GateState.NOT_RUN
    packet_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not BlindedReviewPacket:
            raise SemanticValidationError("blinded review packet wrapper is invalid")
        if self.packet_version != EVALUATION_VERSION or self.seed != M8_SEED:
            raise SemanticValidationError("blinded review packet version is invalid")
        _require_hash(self.dataset_manifest_digest, "review dataset digest")
        _require_hash(self.label_assignment_digest, "review label digest")
        if self.status is not GateState.NOT_RUN:
            raise SemanticValidationError("human review cannot be manufactured by the evaluator")
        entries: list[tuple[str, str, str, str]] = []
        for entry in self.entries:
            if not isinstance(entry, (tuple, list)) or len(entry) != 4:
                raise SemanticValidationError("blinded review entry is invalid")
            reference, rubric_id, label_a_evidence_digest, label_b_evidence_digest = entry
            _require_hash(reference, "blinded case reference")
            _require_identifier(rubric_id, "blinded rubric", _RUBRIC_ID)
            _require_hash(label_a_evidence_digest, "blinded label A evidence")
            _require_hash(label_b_evidence_digest, "blinded label B evidence")
            entries.append((reference, rubric_id, label_a_evidence_digest, label_b_evidence_digest))
        if len({item[0] for item in entries}) != len(entries):
            raise SemanticValidationError("blinded review entries contain duplicates")
        object.__setattr__(self, "entries", tuple(entries))
        digest = sha256_hex(self._digest_input())
        if self.packet_digest and self.packet_digest != digest:
            raise SemanticValidationError("blinded review packet digest does not match")
        object.__setattr__(self, "packet_digest", digest)

    def _digest_input(self) -> dict[str, object]:
        return {
            "packet_version": self.packet_version,
            "dataset_manifest_digest": self.dataset_manifest_digest,
            "seed": self.seed,
            "label_assignment_digest": self.label_assignment_digest,
            "entries": [
                {
                    "case_reference_digest": reference,
                    "rubric_id": rubric_id,
                    "label_a_evidence_digest": label_a_evidence_digest,
                    "label_b_evidence_digest": label_b_evidence_digest,
                }
                for reference, rubric_id, label_a_evidence_digest, label_b_evidence_digest in self.entries
            ],
            "status": self.status.value,
        }

    def to_dict(self) -> dict[str, object]:
        # The A/B-to-system mapping remains committed by digest but is not public evidence.
        return {**self._digest_input(), "packet_digest": self.packet_digest}


def build_blinded_review_packet(candidate: EvaluationRun, b0: EvaluationRun) -> BlindedReviewPacket:
    """Build a deterministic, opaque future-review packet from comparable runs."""

    if type(candidate) is not EvaluationRun or type(b0) is not EvaluationRun:
        raise SemanticValidationError("blinded review runs are invalid")
    if candidate.binding.binding_digest != b0.binding.binding_digest or tuple(
        case.case_id for case in candidate.cases
    ) != tuple(case.case_id for case in b0.cases):
        raise SemanticValidationError("blinded review runs are not comparable")
    assignments = []
    entries = []
    for case, candidate_outcome, baseline_outcome in zip(candidate.cases, candidate.outcomes, b0.outcomes):
        reference = _opaque_id_digest(case.case_id)
        # This committed value is deterministic, but never reveals which label is candidate.
        label = "A" if int(sha256_hex({"case": case.case_id, "seed": M8_SEED})[:2], 16) % 2 == 0 else "B"
        assignments.append({"case_reference_digest": reference, "candidate_label": label})
        if label == "A":
            entries.append((reference, case.rubric_id, candidate_outcome.outcome_digest, baseline_outcome.outcome_digest))
        else:
            entries.append((reference, case.rubric_id, baseline_outcome.outcome_digest, candidate_outcome.outcome_digest))
    return BlindedReviewPacket(
        packet_version=EVALUATION_VERSION,
        dataset_manifest_digest=candidate.binding.dataset_manifest_digest,
        seed=M8_SEED,
        label_assignment_digest=sha256_hex({"assignments": assignments}),
        entries=tuple(entries),
    )


@dataclass(frozen=True)
class MetricGate:
    """A denominator-preserving local metric result."""

    gate_id: str
    state: GateState
    numerator: int
    denominator: int
    threshold: str
    value: str
    failed_case_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self) is not MetricGate:
            raise SemanticValidationError("metric gate wrapper is invalid")
        _require_identifier(self.gate_id, "metric gate_id", re.compile(r"^gate:[a-z0-9][a-z0-9._-]{2,95}$"))
        if self.state not in {GateState.PASS, GateState.FAIL, GateState.BLOCKED, GateState.DEFERRED_TO_M9}:
            raise SemanticValidationError("metric gate state is invalid")
        _require_int(self.numerator, "metric numerator", minimum=0)
        _require_int(self.denominator, "metric denominator", minimum=1)
        if self.numerator > self.denominator:
            raise SemanticValidationError("metric numerator exceeds denominator")
        _require_safe_fixture_text(self.threshold, "metric threshold", maximum=96)
        _require_safe_fixture_text(self.value, "metric value", maximum=96)
        failed = _freeze_identifiers(self.failed_case_ids, "metric failed IDs", _CASE_ID, maximum=256)
        object.__setattr__(self, "failed_case_ids", failed)

    def to_dict(self) -> dict[str, object]:
        return {
            "gate_id": self.gate_id,
            "state": self.state.value,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "threshold": self.threshold,
            "value": self.value,
            "failed_case_ids": list(self.failed_case_ids),
            "measurement_scope": "synthetic-local",
        }

    @classmethod
    def from_value(cls, value: object) -> "MetricGate":
        raw = _require_exact_keys(
            value,
            frozenset(
                (
                    "gate_id",
                    "state",
                    "numerator",
                    "denominator",
                    "threshold",
                    "value",
                    "failed_case_ids",
                    "measurement_scope",
                )
            ),
            "metric gate",
        )
        if raw["measurement_scope"] != "synthetic-local":
            raise SemanticValidationError("metric gate measurement scope is invalid")
        try:
            return cls(
                gate_id=raw["gate_id"],
                state=GateState(raw["state"]),
                numerator=raw["numerator"],
                denominator=raw["denominator"],
                threshold=raw["threshold"],
                value=raw["value"],
                failed_case_ids=tuple(raw["failed_case_ids"]),
            )
        except (TypeError, ValueError, SemanticValidationError) as error:
            raise SemanticValidationError("metric gate is invalid") from error


@dataclass(frozen=True)
class LocalEvaluationResult:
    """Safe summary of the deterministic M8 quality and contract run."""

    corpus: EvaluationCorpus
    candidate: EvaluationRun
    b3_baseline: EvaluationRun
    b3_rollback_snapshot: "B3RollbackSnapshot"
    baselines: tuple[BaselineReceipt, ...]
    comparison: ComparisonResult
    blinded_review: BlindedReviewPacket
    m4_evaluation: M4LocalEvaluation
    performance_measurement: M8PerformanceMeasurement
    gates: tuple[MetricGate, ...]

    def __post_init__(self) -> None:
        if type(self) is not LocalEvaluationResult:
            raise SemanticValidationError("local evaluation result wrapper is invalid")
        if (
            type(self.corpus) is not EvaluationCorpus
            or type(self.candidate) is not EvaluationRun
            or type(self.b3_baseline) is not EvaluationRun
            or type(self.b3_rollback_snapshot) is not B3RollbackSnapshot
        ):
            raise SemanticValidationError("local evaluation result is invalid")
        if self.candidate.baseline_kind is not None or self.b3_baseline.baseline_kind is not BaselineKind.B3_LAST_KNOWN_GOOD:
            raise SemanticValidationError("local evaluation candidate or B3 baseline is invalid")
        if self.b3_baseline.binding.binding_digest != self.candidate.binding.binding_digest:
            raise SemanticValidationError("local evaluation B3 binding is invalid")
        baselines = tuple(self.baselines)
        if len(baselines) != 4 or any(type(item) is not BaselineReceipt for item in baselines):
            raise SemanticValidationError("local evaluation baselines are invalid")
        if {item.baseline_kind for item in baselines} != set(BaselineKind):
            raise SemanticValidationError("local evaluation baseline set is invalid")
        if tuple(item.baseline_kind for item in baselines) != tuple(BaselineKind):
            raise SemanticValidationError("local evaluation baseline order is invalid")
        if any(item.binding.binding_digest != self.candidate.binding.binding_digest for item in baselines):
            raise SemanticValidationError("local evaluation baseline binding is invalid")
        b3_receipt = next(item for item in baselines if item.baseline_kind is BaselineKind.B3_LAST_KNOWN_GOOD)
        b3_outcome_digest = sha256_hex({"outcome_digests": [item.outcome_digest for item in self.b3_baseline.outcomes]})
        snapshot = self.b3_rollback_snapshot
        if (
            snapshot.b3_run_digest != self.b3_baseline.run_digest
            or snapshot.binding_digest != self.b3_baseline.binding.binding_digest
            or snapshot.baseline_outcome_digest != b3_outcome_digest
            or snapshot.baseline_receipt_digest != b3_receipt.receipt_digest
        ):
            raise SemanticValidationError("local evaluation B3 rollback snapshot is unbound")
        if (
            type(self.comparison) is not ComparisonResult
            or type(self.blinded_review) is not BlindedReviewPacket
            or type(self.m4_evaluation) is not M4LocalEvaluation
            or type(self.performance_measurement) is not M8PerformanceMeasurement
        ):
            raise SemanticValidationError("local evaluation evidence is invalid")
        memory_case_ids = tuple(case.case_id for case in self.corpus.cases if case.suite == "memory")
        if tuple(item.case_id for item in self.m4_evaluation.cases) != memory_case_ids:
            raise SemanticValidationError("local evaluation M4 evidence is unbound from memory cases")
        gates = tuple(self.gates)
        if not gates or any(type(item) is not MetricGate for item in gates):
            raise SemanticValidationError("local evaluation gates are invalid")
        if len({item.gate_id for item in gates}) != len(gates):
            raise SemanticValidationError("local evaluation gates contain duplicates")
        object.__setattr__(self, "baselines", baselines)
        object.__setattr__(self, "gates", gates)

    @property
    def passed(self) -> bool:
        return all(gate.state is GateState.PASS for gate in self.gates if gate.state is not GateState.DEFERRED_TO_M9)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": EVALUATION_VERSION,
            "status": "LOCAL_COMPONENTS_VERIFIED" if self.passed else "LOCAL_COMPONENTS_FAILED",
            "evidence_scope": SYNTHETIC_LOCAL,
            "live_attested": False,
            "corpus_scale": "local-synthetic-smoke",
            "dataset_manifests": {
                "frozen": self.corpus.frozen_manifest.to_dict(),
                "rotating": self.corpus.rotating_manifest.to_dict(),
                "combined": self.corpus.manifest.to_dict(),
            },
            "candidate": self.candidate.to_dict(),
            "b3_baseline": self.b3_baseline.to_dict(),
            "b3_rollback_snapshot": self.b3_rollback_snapshot.to_dict(),
            "baseline_receipt_digests": [item.receipt_digest for item in self.baselines],
            "comparison": self.comparison.to_dict(),
            "blinded_review": self.blinded_review.to_dict(),
            "m4_evaluation": self.m4_evaluation.to_receipt(),
            "performance_measurement": self.performance_measurement.to_receipt(),
            "gates": [gate.to_dict() for gate in self.gates],
            "corpus_limit": "production-scale suite minima are deferred to M9",
        }


def _task_id_for_case(case_id: str) -> str:
    """Derive one opaque M5 task identity shared by every baseline for a case."""

    _require_identifier(case_id, "component task case_id", _CASE_ID)
    return f"task:{uuid.uuid5(uuid.NAMESPACE_URL, f'second-brain-m8:{case_id}') }"


def _case_token(case_id: str) -> str:
    return sha256_hex({"case_id": case_id, "seed": M8_SEED})[:24]


def _fixed_clock() -> DeterministicClock:
    return DeterministicClock(datetime(2026, 7, 23, tzinfo=UTC))


def _uuid_factory(namespace: str) -> Callable[[], uuid.UUID]:
    counter = 0

    def next_uuid() -> uuid.UUID:
        nonlocal counter
        counter += 1
        return uuid.uuid5(uuid.NAMESPACE_URL, f"second-brain-m8:{namespace}:{counter}")

    return next_uuid


def _local_helper_descriptor() -> CapabilityDescriptor:
    """Return the sole non-executable capability fixture used by M8."""

    return CapabilityDescriptor(
        identifier="cap:local-helper",
        version="1.0.0",
        source_ref="project-local",
        kind="local_tool",
        gateway=Gateway.BACKEND,
        triggers=("m8",),
        scopes=("workspace",),
        required_permission=PermissionClass.LOCAL_READ,
        input_contract="task-intent/v1",
        output_contract="artifact/v1",
        health="healthy",
        trust="repository_trusted",
        cost="low",
        latency="low",
        installed=True,
    )


def _resolve_case_capability(
    decision: AdmissionDecision,
    audit: AuditTrail,
    *,
    task_id: str,
    required: bool,
) -> CapabilityResolution:
    request = CapabilityRequest(
        task_id=task_id,
        gateways=(Gateway.BACKEND,) if required else (),
        intent_tags=("m8",) if required else (),
        required_scopes=("workspace",) if required else (),
        required_ids=("cap:local-helper",) if required else (),
        permitted_permissions=(PermissionClass.LOCAL_READ,) if required else (),
    )
    return CapabilityResolver(
        CapabilityRegistry((_local_helper_descriptor(),)), audit_trail=audit
    ).resolve(decision, request)


def _m6_receipt(
    *,
    case_id: str,
    task_id: str,
    node_id: str,
    profile_alias: str,
    execution_mode: ExecutionMode,
    registry: Mapping[str, Any],
) -> RouteReceipt:
    """Issue one exact synthetic M6 chain; callers never supply a route status."""

    token = _case_token(case_id)
    suffix = f"{token}-{execution_mode.value}"
    intent = create_route_intent(
        route_id=f"route:m8-{suffix}",
        task_id=task_id,
        node_id=node_id,
        profile_alias=profile_alias,
        correlation_id=f"corr:m8-{suffix}",
        created_at=M8_EVIDENCE_TIMESTAMP,
        registry=registry,
    )
    if type(intent) is not RouteIntent:
        raise SemanticValidationError("M6 intent type is invalid")
    serialized = serialize_route(intent, registry=registry, sent_at=M8_EVIDENCE_TIMESTAMP)
    if type(serialized) is not SerializedRoute:
        raise SemanticValidationError("M6 serialized route type is invalid")
    reconciliation = reconcile_route(
        serialized,
        (synthetic_observation(intent, serialized),),
        reconciled_at=M8_EVIDENCE_TIMESTAMP,
    )
    if type(reconciliation) is not RouteReconciliation:
        raise SemanticValidationError("M6 reconciliation type is invalid")
    receipt = create_route_receipt(intent, serialized, reconciliation)
    if type(receipt) is not RouteReceipt:
        raise SemanticValidationError("M6 route receipt type is invalid")
    return receipt


def _node_spec(
    *,
    case_id: str,
    node_id: str,
    role: str,
    profile: str,
    depends_on: Sequence[str],
    acceptance: str,
    writable: bool,
) -> dict[str, object]:
    token = _case_token(case_id)
    write_path = f"fixtures/m8/proposals/{token}-{node_id}.json"
    return {
        "id": node_id,
        "role": role,
        "task": f"Produce the declared bounded M8 synthetic result for {node_id}.",
        "profile": profile,
        "depends_on": list(depends_on),
        "read_scope": ["fixtures/m8/inputs/"],
        "write_scope": [write_path] if writable else [],
        "capabilities": [],
        "expected_artifacts": [f"artifact:m8-{token}-{node_id}"],
        "acceptance": [acceptance],
        "timeout_seconds": 30,
        "max_attempts": 1,
        "permission_class": "workspace-write" if writable else "local-read",
        "reroute_to": None,
    }


def _case_graph_manifest(
    *,
    case: EvaluationCase,
    plan: FixtureExecutionPlan,
    task_id: str,
    lane: Lane,
    registry: Mapping[str, Any],
) -> WorkGraphManifest:
    """Build a case-bound M7 manifest instead of reusing an older fixture graph."""

    if lane not in {Lane.GRAPH, Lane.DEEP} or plan.graph_primary_node is None:
        raise SemanticValidationError("case graph manifest is not admitted")
    primary = plan.graph_primary_node
    token = _case_token(case.case_id)
    support_check = f"check:m8-{token}-support"
    final_check = f"check:m8-{token}-final"
    if lane is Lane.GRAPH:
        if primary == "integrate":
            nodes = (
                _node_spec(
                    case_id=case.case_id,
                    node_id="analyze",
                    role="worker",
                    profile="sol-xhigh",
                    depends_on=(),
                    acceptance=support_check,
                    writable=True,
                ),
                _node_spec(
                    case_id=case.case_id,
                    node_id="support",
                    role="worker",
                    profile="tera-high",
                    depends_on=(),
                    acceptance=f"check:m8-{token}-support-two",
                    writable=False,
                ),
                _node_spec(
                    case_id=case.case_id,
                    node_id="integrate",
                    role="integrator",
                    profile="tera-max",
                    depends_on=("analyze", "support"),
                    acceptance=plan.check_id,
                    writable=False,
                ),
            )
        elif primary == "review":
            nodes = (
                _node_spec(
                    case_id=case.case_id,
                    node_id="review",
                    role="reviewer",
                    profile="gpt55-xhigh",
                    depends_on=(),
                    acceptance=plan.check_id,
                    writable=False,
                ),
                _node_spec(
                    case_id=case.case_id,
                    node_id="support",
                    role="worker",
                    profile="sol-xhigh",
                    depends_on=(),
                    acceptance=support_check,
                    writable=True,
                ),
                _node_spec(
                    case_id=case.case_id,
                    node_id="integrate",
                    role="integrator",
                    profile="tera-max",
                    depends_on=("review", "support"),
                    acceptance=final_check,
                    writable=False,
                ),
            )
        else:
            nodes = (
                _node_spec(
                    case_id=case.case_id,
                    node_id="analyze",
                    role="worker",
                    profile=plan.profile_alias,
                    depends_on=(),
                    acceptance=plan.check_id,
                    writable=True,
                ),
                _node_spec(
                    case_id=case.case_id,
                    node_id="support",
                    role="worker",
                    profile="tera-high",
                    depends_on=(),
                    acceptance=support_check,
                    writable=False,
                ),
                _node_spec(
                    case_id=case.case_id,
                    node_id="integrate",
                    role="integrator",
                    profile="tera-max",
                    depends_on=("analyze", "support"),
                    acceptance=final_check,
                    writable=False,
                ),
            )
    elif primary == "review":
        nodes = (
            _node_spec(
                case_id=case.case_id,
                node_id="analyze",
                role="worker",
                profile="sol-xhigh",
                depends_on=(),
                acceptance=support_check,
                writable=True,
            ),
            _node_spec(
                case_id=case.case_id,
                node_id="review",
                role="reviewer",
                profile="gpt55-xhigh",
                depends_on=("analyze",),
                acceptance=plan.check_id,
                writable=False,
            ),
            _node_spec(
                case_id=case.case_id,
                node_id="integrate",
                role="integrator",
                profile="tera-max",
                depends_on=("review",),
                acceptance=final_check,
                writable=False,
            ),
        )
    else:
        nodes = (
            _node_spec(
                case_id=case.case_id,
                node_id="analyze",
                role="worker",
                profile="sol-xhigh",
                depends_on=(),
                acceptance=support_check,
                writable=True,
            ),
            _node_spec(
                case_id=case.case_id,
                node_id="review",
                role="reviewer",
                profile="gpt55-xhigh",
                depends_on=("analyze",),
                acceptance=f"check:m8-{token}-review",
                writable=False,
            ),
            _node_spec(
                case_id=case.case_id,
                node_id="integrate",
                role="integrator",
                profile="tera-max",
                depends_on=("review",),
                acceptance=plan.check_id,
                writable=False,
            ),
        )
    return validate_work_graph_manifest(
        {
            "version": 1,
            "task_id": task_id,
            "lane": lane.value,
            "goal": f"Run bounded M8 local component fixture {token}.",
            "max_concurrency": 2,
            "nodes": list(nodes),
            "final_node": "integrate",
        },
        registry=registry,
    )


def _run_case_graph(
    *,
    case: EvaluationCase,
    plan: FixtureExecutionPlan,
    task_id: str,
    admission: AdmissionDecision,
    audit: AuditTrail,
    execution_mode: ExecutionMode,
    registry: Mapping[str, Any],
) -> tuple[GraphRunReceipt, RouteReceipt]:
    manifest = _case_graph_manifest(
        case=case,
        plan=plan,
        task_id=task_id,
        lane=admission.lane,
        registry=registry,
    )
    observed_routes: dict[str, RouteReceipt] = {}

    def route_observer(intent: object, serialized: object) -> tuple[object, ...]:
        if type(intent) is not RouteIntent or type(serialized) is not SerializedRoute:
            raise SemanticValidationError("M7 route observer received an invalid M6 seam")
        observation = synthetic_observation(intent, serialized)
        reconciliation = reconcile_route(
            serialized,
            (observation,),
            reconciled_at=M8_EVIDENCE_TIMESTAMP,
        )
        if type(reconciliation) is not RouteReconciliation:
            raise SemanticValidationError("M7 route reconciliation type is invalid")
        receipt = create_route_receipt(intent, serialized, reconciliation)
        if type(receipt) is not RouteReceipt:
            raise SemanticValidationError("M7 route receipt type is invalid")
        observed_routes[intent.node_id] = receipt
        return (observation,)

    def runner(context: object, _cancellation: object) -> NodeResult:
        node_id = getattr(context, "node_id", None)
        attempt = getattr(context, "attempt", None)
        node = manifest.node_by_id.get(node_id)
        if node is None or type(attempt) is not int:
            raise SemanticValidationError("M7 runner context is invalid")
        artifacts = tuple(
            ArtifactReference(
                artifact_id,
                sha256_hex({"case_id": case.case_id, "node_id": node.id, "artifact_id": artifact_id}),
            )
            for artifact_id in node.expected_artifacts
        )
        changes = ()
        if node.write_scope:
            changes = (
                ProposedChange(
                    node.write_scope[0],
                    sha256_hex({"case_id": case.case_id, "node_id": node.id, "proposal": True}),
                ),
            )
        return NodeResult.succeeded(
            node.id,
            attempt,
            artifacts=artifacts,
            changes=changes,
            checks=node.acceptance,
            token_count=len(node.expected_artifacts),
            cost_microunits=len(node.acceptance),
        )

    runtime = GraphRuntime(
        manifest,
        audit_trail=audit,
        registry=registry,
        clock=_fixed_clock(),
        monotonic_clock=lambda: 0.0,
        id_factory=_uuid_factory(f"{case.case_id}:{execution_mode.value}:graph"),
    )
    receipt = runtime.run(
        admission,
        runner,
        serial_fallback=execution_mode is ExecutionMode.B1_SERIAL,
        route_observer=route_observer,
    )
    primary_node = f"node:{plan.graph_primary_node}"
    primary_receipt = observed_routes.get(primary_node)
    if type(primary_receipt) is not RouteReceipt:
        raise SemanticValidationError("M7 primary route receipt is unavailable")
    return receipt, primary_receipt


def _execute_case(
    case: EvaluationCase,
    plan: FixtureExecutionPlan,
    plans: FixtureExecutionPlanSet,
    *,
    execution_mode: ExecutionMode,
    registry: Mapping[str, Any],
) -> CaseOutcome:
    """Execute M5/M6/M7 from fixed inputs; ``case.expected`` is never consulted."""

    task_id = _task_id_for_case(case.case_id)
    audit = AuditTrail(
        clock=_fixed_clock(), id_factory=_uuid_factory(f"{case.case_id}:{execution_mode.value}:audit")
    )
    admission = AdmissionEngine(audit_trail=audit).admit(plan.admission_features, task_id=task_id)
    resolution = _resolve_case_capability(
        admission, audit, task_id=task_id, required=plan.capability_required
    )
    execution_profile = "tera-max" if execution_mode is ExecutionMode.B0_ROOT else plan.profile_alias
    route_receipt: RouteReceipt | None = None
    graph_receipt: GraphRunReceipt | None = None
    route_node_id: str | None = None
    if admission.lane is not Lane.DIRECT:
        if admission.lane in {Lane.GRAPH, Lane.DEEP} and execution_mode is not ExecutionMode.B0_ROOT:
            graph_receipt, route_receipt = _run_case_graph(
                case=case,
                plan=plan,
                task_id=task_id,
                admission=admission,
                audit=audit,
                execution_mode=execution_mode,
                registry=registry,
            )
            route_node_id = f"node:{plan.graph_primary_node}"
        else:
            route_node_id = f"node:m8-{_case_token(case.case_id)}-{execution_mode.value}"
            route_receipt = _m6_receipt(
                case_id=case.case_id,
                task_id=task_id,
                node_id=route_node_id,
                profile_alias=execution_profile,
                execution_mode=execution_mode,
                registry=registry,
            )
    evidence = CaseExecutionEvidence(
        case_id=case.case_id,
        task_id=task_id,
        plan=plan,
        admission=admission,
        audit_trail=audit,
        capability_resolution=resolution,
        route_receipt=route_receipt,
        graph_receipt=graph_receipt,
        execution_mode=execution_mode,
        execution_profile=execution_profile,
        route_node_id=route_node_id,
        memory_retrieval_performed=False,
        execution_plan_set_digest=plans.plan_set_digest,
    )
    return CaseOutcome(
        case_id=case.case_id,
        observed_lane=evidence.admission.lane.value,
        observed_profile=evidence.execution_profile,
        observed_capability_ids=evidence.selected_capability_ids,
        observed_event_ids=(),
        check_ids=(plan.check_id,) if evidence.execution_status is ExecutionStatus.SUCCEEDED else (),
        artifact_ids=(),
        artifact_digests=(),
        execution_status=evidence.execution_status,
        route_status=evidence.route_status,
        latency_ms=0 if graph_receipt is None else graph_receipt.duration_ms,
        cost_microunits=0 if graph_receipt is None else graph_receipt.total_cost_microunits,
        context_tokens=0 if graph_receipt is None else graph_receipt.total_tokens,
        retrieval_count=0,
        proposal_only=True if graph_receipt is None else graph_receipt.proposal_only,
        material_side_effect_allowed=False
        if graph_receipt is None
        else graph_receipt.material_side_effect_allowed,
        component_evidence=evidence,
    )


def _execution_mode(value: str) -> ExecutionMode:
    try:
        return ExecutionMode(value)
    except (TypeError, ValueError) as error:
        raise SemanticValidationError("component execution mode is invalid") from error


def make_synthetic_run(
    corpus: EvaluationCorpus,
    binding: RunBinding,
    *,
    run_id: str,
    baseline_kind: BaselineKind | None,
    mode: str,
    plans: FixtureExecutionPlanSet | None = None,
) -> EvaluationRun:
    """Compatibility entry point that now runs the closed component executor.

    The historical name remains import-compatible, but it can no longer mint an
    outcome from fixture expectations or caller-provided metric constants.
    """

    if type(corpus) is not EvaluationCorpus or type(binding) is not RunBinding:
        raise SemanticValidationError("component run inputs are invalid")
    execution_mode = _execution_mode(mode)
    expected_kinds = {
        ExecutionMode.CANDIDATE: None,
        ExecutionMode.B0_ROOT: BaselineKind.B0_ROOT_TERA_MAX,
        ExecutionMode.B1_SERIAL: BaselineKind.B1_SERIAL_WORKFLOW,
        ExecutionMode.B2_NO_MEMORY: BaselineKind.B2_NO_MEMORY,
        ExecutionMode.B3_LAST_KNOWN_GOOD: BaselineKind.B3_LAST_KNOWN_GOOD,
    }
    if baseline_kind is not expected_kinds[execution_mode]:
        raise SemanticValidationError("component run baseline does not match its execution mode")
    plans = load_local_execution_plans(corpus) if plans is None else plans
    if type(plans) is not FixtureExecutionPlanSet:
        raise SemanticValidationError("component run plans are invalid")
    expected_binding = create_local_run_binding(corpus, plans)
    if binding.binding_digest != expected_binding.binding_digest:
        raise SemanticValidationError("component run binding is stale or unbound")
    registry = load_profile_registry()
    return EvaluationRun(
        run_id=run_id,
        baseline_kind=baseline_kind,
        binding=binding,
        cases=corpus.cases,
        outcomes=tuple(
            _execute_case(
                case,
                plans.for_case(case.case_id),
                plans,
                execution_mode=execution_mode,
                registry=registry,
            )
            for case in corpus.cases
        ),
    )


def _gate(
    gate_id: str,
    cases: Sequence[EvaluationCase],
    passed_case_ids: set[str],
    threshold: str,
    value: str,
) -> MetricGate:
    failed = tuple(case.case_id for case in cases if case.case_id not in passed_case_ids)
    return MetricGate(
        gate_id=gate_id,
        state=GateState.PASS if not failed else GateState.FAIL,
        numerator=len(cases) - len(failed),
        denominator=max(1, len(cases)),
        threshold=threshold,
        value=value,
        failed_case_ids=failed,
    )


def _deferred_gate(gate_id: str, cases: Sequence[EvaluationCase], threshold: str, value: str) -> MetricGate:
    return MetricGate(
        gate_id=gate_id,
        state=GateState.DEFERRED_TO_M9,
        numerator=0,
        denominator=max(1, len(cases)),
        threshold=threshold,
        value=value,
    )


def _lane_macro_f1(cases: Sequence[EvaluationCase], outcomes: Sequence[CaseOutcome]) -> Decimal:
    scores: list[Decimal] = []
    for lane in _LANES:
        true_positive = sum(
            case.expected.lane == lane and outcome.observed_lane == lane
            for case, outcome in zip(cases, outcomes)
        )
        false_positive = sum(
            case.expected.lane != lane and outcome.observed_lane == lane
            for case, outcome in zip(cases, outcomes)
        )
        false_negative = sum(
            case.expected.lane == lane and outcome.observed_lane != lane
            for case, outcome in zip(cases, outcomes)
        )
        denominator = 2 * true_positive + false_positive + false_negative
        scores.append(Decimal(1) if denominator == 0 else Decimal(2 * true_positive) / Decimal(denominator))
    return sum(scores, Decimal(0)) / Decimal(len(scores))


def _capability_precision_recall(
    cases: Sequence[EvaluationCase], outcomes: Sequence[CaseOutcome]
) -> tuple[Decimal, Decimal, tuple[str, ...]]:
    true_positive = false_positive = false_negative = 0
    failed: list[str] = []
    for case, outcome in zip(cases, outcomes):
        expected = set(case.expected.required_capabilities)
        observed = set(outcome.observed_capability_ids)
        true_positive += len(expected & observed)
        false_positive += len(observed - expected)
        false_negative += len(expected - observed)
        if expected != observed:
            failed.append(case.case_id)
    precision = Decimal(1) if true_positive + false_positive == 0 else Decimal(true_positive) / Decimal(true_positive + false_positive)
    recall = Decimal(1) if true_positive + false_negative == 0 else Decimal(true_positive) / Decimal(true_positive + false_negative)
    return precision, recall, tuple(failed)


def run_local_evaluation(corpus: EvaluationCorpus | None = None) -> LocalEvaluationResult:
    """Run local M4/M5/M6/M7 components and defer promotion-only claims."""

    corpus = load_local_evaluation_corpus() if corpus is None else corpus
    if type(corpus) is not EvaluationCorpus:
        raise SemanticValidationError("evaluation corpus is invalid")
    plans = load_local_execution_plans(corpus)
    binding = create_local_run_binding(corpus, plans)
    candidate = make_synthetic_run(
        corpus, binding, run_id="run:m8-candidate", baseline_kind=None, mode="candidate", plans=plans
    )
    b0 = make_synthetic_run(
        corpus,
        binding,
        run_id="run:m8-b0",
        baseline_kind=BaselineKind.B0_ROOT_TERA_MAX,
        mode="b0",
        plans=plans,
    )
    b1 = make_synthetic_run(
        corpus,
        binding,
        run_id="run:m8-b1",
        baseline_kind=BaselineKind.B1_SERIAL_WORKFLOW,
        mode="b1",
        plans=plans,
    )
    b2 = make_synthetic_run(
        corpus,
        binding,
        run_id="run:m8-b2",
        baseline_kind=BaselineKind.B2_NO_MEMORY,
        mode="b2",
        plans=plans,
    )
    b3 = make_synthetic_run(
        corpus,
        binding,
        run_id="run:m8-b3",
        baseline_kind=BaselineKind.B3_LAST_KNOWN_GOOD,
        mode="b3",
        plans=plans,
    )
    comparison = compare_runs(candidate, b0, b1, b2, b3)
    grades = tuple(grade_case(case, outcome) for case, outcome in zip(candidate.cases, candidate.outcomes))
    all_case_ids = {case.case_id for case in corpus.cases}
    deterministic_passed = {
        grade.case_id for grade in grades if grade.state is GateState.PASS
    }
    lane_f1 = _lane_macro_f1(candidate.cases, candidate.outcomes)
    admission_failures = {
        case.case_id
        for case, outcome in zip(candidate.cases, candidate.outcomes)
        if case.expected.lane != outcome.observed_lane
        or (case.expected.lane == "DIRECT" and outcome.observed_lane in {"GRAPH", "DEEP"})
    }
    admission_passed = all_case_ids - admission_failures
    capability_precision, capability_recall, capability_failures = _capability_precision_recall(
        candidate.cases, candidate.outcomes
    )
    graph_cases = tuple(
        case for case, outcome in zip(candidate.cases, candidate.outcomes)
        if outcome.component_evidence.admission.lane in {Lane.GRAPH, Lane.DEEP}
    )
    candidate_by_id = {outcome.case_id: outcome for outcome in candidate.outcomes}
    b0_by_id = {outcome.case_id: outcome for outcome in b0.outcomes}
    b1_by_id = {outcome.case_id: outcome for outcome in b1.outcomes}
    b2_by_id = {outcome.case_id: outcome for outcome in b2.outcomes}
    graph_contract_passed = {
        case.case_id
        for case in graph_cases
        if candidate_by_id[case.case_id].component_evidence.graph_receipt is not None
        and candidate_by_id[case.case_id].component_evidence.graph_receipt.state is GraphState.SUCCEEDED
        and b1_by_id[case.case_id].component_evidence.graph_receipt is not None
        and b1_by_id[case.case_id].component_evidence.graph_receipt.serial_fallback
        and b1_by_id[case.case_id].component_evidence.graph_receipt.max_active_workers <= 1
        and b0_by_id[case.case_id].component_evidence.graph_receipt is None
    }
    route_cases = tuple(
        case
        for case, outcome in zip(candidate.cases, candidate.outcomes)
        if outcome.component_evidence.admission.lane is not Lane.DIRECT
    )
    route_passed = {
        case.case_id
        for case in route_cases
        if candidate_by_id[case.case_id].component_evidence.route_receipt is not None
        and candidate_by_id[case.case_id].component_evidence.route_receipt.status is ReconciliationStatus.MATCH
        and side_effect_allowed(candidate_by_id[case.case_id].component_evidence.route_receipt, "DEEP") is False
    }
    security_passed = {
        case.case_id
        for case, outcome in zip(candidate.cases, candidate.outcomes)
        if outcome.proposal_only
        and not outcome.material_side_effect_allowed
        and outcome.material_actions_started == 0
        and outcome.network_requests_started == 0
        and outcome.global_target_count == 0
        and outcome.observed_event_ids == ()
    }
    b2_no_memory_passed = {
        case.case_id
        for case in corpus.cases
        if b2_by_id[case.case_id].component_evidence.memory_retrieval_performed is False
    }
    memory_cases = tuple(case for case in corpus.cases if case.suite == "memory")
    m4_evaluation = run_m8_m4_local_evaluation(tuple(case.case_id for case in memory_cases))
    m4_passed = {
        item.case_id
        for item in m4_evaluation.cases
        if item.target_check_passed and item.provenance_check_passed and item.context_check_passed
    }
    performance_measurement = run_m8_local_performance_measurement()
    performance_passed = {
        case.case_id for case in graph_cases if performance_measurement.local_contract_passed
    }
    gates = (
        _gate(
            "gate:dataset-schema",
            corpus.cases,
            all_case_ids,
            "strict-70-case-plan-bound-corpus",
            "70-of-70",
        ),
        MetricGate(
            gate_id="gate:admission",
            state=GateState.PASS if lane_f1 >= Decimal("0.96") and not admission_failures else GateState.FAIL,
            numerator=len(admission_passed),
            denominator=len(corpus.cases),
            threshold="macro-f1-gte-0.96-zero-direct-escalation",
            value=f"macro-f1-{lane_f1:.4f}",
            failed_case_ids=tuple(sorted(admission_failures)),
        ),
        MetricGate(
            gate_id="gate:capability",
            state=GateState.PASS
            if capability_precision >= Decimal("0.98") and capability_recall >= Decimal("0.97") and not capability_failures
            else GateState.FAIL,
            numerator=len(corpus.cases) - len(capability_failures),
            denominator=len(corpus.cases),
            threshold="precision-gte-0.98-recall-gte-0.97",
            value=f"precision-{capability_precision:.4f}-recall-{capability_recall:.4f}",
            failed_case_ids=capability_failures,
        ),
        _gate(
            "gate:component-execution",
            corpus.cases,
            deterministic_passed,
            "all-m5-m6-m7-checks-and-boundaries-pass",
            f"{len(deterministic_passed)}-of-{len(corpus.cases)}",
        ),
        _gate(
            "gate:route-integrity",
            route_cases,
            route_passed,
            "all-local-non-direct-routes-match-and-deny-side-effects",
            f"{len(route_passed)}-of-{len(route_cases)}",
        ),
        _gate(
            "gate:graph-serial-fallback",
            graph_cases,
            graph_contract_passed,
            "m7-runs-and-b1-is-actual-serial-fallback",
            f"{len(graph_contract_passed)}-of-{len(graph_cases)}",
        ),
        _gate(
            "gate:b2-no-memory",
            corpus.cases,
            b2_no_memory_passed,
            "b2-performs-zero-memory-retrievals",
            f"{len(b2_no_memory_passed)}-of-{len(corpus.cases)}",
        ),
        _gate(
            "gate:security-redaction",
            corpus.cases,
            security_passed,
            "zero-local-boundary-failures",
            f"{len(security_passed)}-of-{len(corpus.cases)}",
        ),
        _gate(
            "gate:m4-fixture-evidence",
            memory_cases,
            m4_passed,
            "all-memory-cases-have-redacted-m4-retrieval-provenance-context-evidence",
            f"{len(m4_passed)}-of-{len(memory_cases)}",
        ),
        _gate(
            "gate:graph-measurement-fixture",
            graph_cases,
            performance_passed,
            "controlled-monotonic-m7-schedule-and-context-proxy",
            f"{len(performance_passed)}-of-{len(graph_cases)}",
        ),
        _deferred_gate(
            "gate:quality-lcb",
            corpus.cases,
            "lcb-gte--1.0-and-no-safety-loss",
            "deferred-no-blinded-semantic-observations",
        ),
        _deferred_gate(
            "gate:graph-efficiency",
            graph_cases,
            "speedup-gte-25-context-reduction-gte-35",
            "deferred-provider-task-performance-and-root-context",
        ),
        _deferred_gate(
            "gate:memory-context",
            memory_cases,
            "m4-recall-provenance-and-context-quality",
            "deferred-production-scale-m4-recall-and-context-quality",
        ),
    )
    baseline_receipts = tuple(create_baseline_receipt(run) for run in (b0, b1, b2, b3))
    return LocalEvaluationResult(
        corpus=corpus,
        candidate=candidate,
        b3_baseline=b3,
        b3_rollback_snapshot=create_b3_rollback_snapshot(b3),
        baselines=baseline_receipts,
        comparison=comparison,
        blinded_review=build_blinded_review_packet(candidate, b0),
        m4_evaluation=m4_evaluation,
        performance_measurement=performance_measurement,
        gates=gates,
    )


@dataclass(frozen=True)
class LocalCanaryResult:
    """Offline M6 checks plus synthetic shadow-read-only diagnostics."""

    state: GateState
    route_check_count: int
    route_match_count: int
    shadow_check_count: int
    shadow_match_count: int
    alias_counts: tuple[tuple[str, int], ...]
    side_effects_started: int
    global_target_count: int
    live_request_count: int
    live_attested: bool
    reason_codes: tuple[str, ...]
    control_state_digest: str | None = None
    b3_snapshot_digest: str | None = None
    canary_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not LocalCanaryResult:
            raise SemanticValidationError("local canary wrapper is invalid")
        if self.state not in {GateState.PASS, GateState.FAIL, GateState.BLOCKED}:
            raise SemanticValidationError("local canary state is invalid")
        for label, value in (
            ("route_check_count", self.route_check_count),
            ("route_match_count", self.route_match_count),
            ("shadow_check_count", self.shadow_check_count),
            ("shadow_match_count", self.shadow_match_count),
            ("side_effects_started", self.side_effects_started),
            ("global_target_count", self.global_target_count),
            ("live_request_count", self.live_request_count),
        ):
            _require_int(value, label, minimum=0)
        if self.route_match_count > self.route_check_count or self.shadow_match_count > self.shadow_check_count:
            raise SemanticValidationError("local canary counts are invalid")
        aliases = _freeze_count_pairs(self.alias_counts, APPROVED_PROFILE_ALIASES, "canary alias_counts")
        if sum(count for _, count in aliases) != self.shadow_check_count:
            raise SemanticValidationError("canary alias counts are invalid")
        if self.live_attested is not False:
            raise SemanticValidationError("local canary cannot claim live attestation")
        if (self.control_state_digest is None) != (self.b3_snapshot_digest is None):
            raise SemanticValidationError("canary control binding is incomplete")
        if self.control_state_digest is not None:
            _require_hash(self.control_state_digest, "canary control-state digest")
            _require_hash(self.b3_snapshot_digest, "canary B3 snapshot digest")
        reasons = _freeze_identifiers(
            self.reason_codes, "canary reasons", re.compile(r"^[A-Z][A-Z0-9_]{2,95}$"), maximum=32
        )
        object.__setattr__(self, "alias_counts", aliases)
        object.__setattr__(self, "reason_codes", reasons)
        digest = sha256_hex(self._digest_input())
        if self.canary_digest and self.canary_digest != digest:
            raise SemanticValidationError("local canary digest does not match")
        object.__setattr__(self, "canary_digest", digest)

    def _digest_input(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "route_check_count": self.route_check_count,
            "route_match_count": self.route_match_count,
            "shadow_check_count": self.shadow_check_count,
            "shadow_match_count": self.shadow_match_count,
            "alias_counts": [{"profile_alias": alias, "count": count} for alias, count in self.alias_counts],
            "side_effects_started": self.side_effects_started,
            "global_target_count": self.global_target_count,
            "live_request_count": self.live_request_count,
            "live_attested": self.live_attested,
            "reason_codes": list(self.reason_codes),
            "control_state_digest": self.control_state_digest,
            "b3_snapshot_digest": self.b3_snapshot_digest,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": EVALUATION_VERSION,
            "status": self.state.value,
            "offline_route_evidence_scope": SYNTHETIC_LOCAL,
            "shadow_evidence_scope": SIMULATED_SHADOW_READ_ONLY,
            **self._digest_input(),
            "canary_digest": self.canary_digest,
        }


def run_local_canary(
    *,
    control_state: "RollbackControlState | None" = None,
    b3_snapshot: "B3RollbackSnapshot | None" = None,
) -> LocalCanaryResult:
    """Exercise only M6 synthetic APIs; optionally bind a restored B3 state."""

    if (control_state is None) != (b3_snapshot is None):
        raise SemanticValidationError("canary control binding is incomplete")
    if control_state is not None:
        if type(control_state) is not RollbackControlState or type(b3_snapshot) is not B3RollbackSnapshot:
            raise SemanticValidationError("canary control binding is invalid")
        if control_state.digest != b3_snapshot.control_state.digest:
            raise SemanticValidationError("canary control state does not match B3 snapshot")

    route_canary = run_five_repeat_synthetic_canary(timestamp=M8_EVIDENCE_TIMESTAMP)
    route_checks = tuple(route_canary.checks)
    route_passed = (
        route_canary.passed
        and len(route_checks) == 35
        and all(check.reconciliation.status is ReconciliationStatus.MATCH for check in route_checks)
        and all(side_effect_allowed(check.receipt, "DEEP") is False for check in route_checks)
    )
    shadow_matches = 0
    alias_counts = {alias: 0 for alias in APPROVED_PROFILE_ALIASES}
    shadow_side_effect_allowed = False
    for index in range(50):
        alias = APPROVED_PROFILE_ALIASES[index % len(APPROVED_PROFILE_ALIASES)]
        alias_counts[alias] += 1
        intent = create_route_intent(
            route_id=f"route:m8-shadow-{index:02d}-{alias}",
            task_id="task:m8-shadow",
            node_id=f"node:m8-shadow-{index:02d}",
            profile_alias=alias,
            correlation_id=f"corr:m8-shadow-{index:02d}-{alias}",
            created_at=M8_EVIDENCE_TIMESTAMP,
        )
        serialized = serialize_route(intent, sent_at=M8_EVIDENCE_TIMESTAMP)
        reconciliation = reconcile_route(
            serialized,
            (synthetic_observation(intent, serialized),),
            reconciled_at=M8_EVIDENCE_TIMESTAMP,
        )
        receipt = create_route_receipt(intent, serialized, reconciliation)
        if reconciliation.status is ReconciliationStatus.MATCH:
            shadow_matches += 1
        shadow_side_effect_allowed = shadow_side_effect_allowed or side_effect_allowed(receipt, "GRAPH")
    shadow_passed = shadow_matches == 50 and not shadow_side_effect_allowed
    reasons: list[str] = []
    if not route_passed:
        reasons.append("SYNTHETIC_ROUTE_CANARY_FAILED")
    if not shadow_passed:
        reasons.append("SIMULATED_SHADOW_FAILED")
    return LocalCanaryResult(
        state=GateState.PASS if not reasons else GateState.FAIL,
        route_check_count=len(route_checks),
        route_match_count=sum(check.reconciliation.status is ReconciliationStatus.MATCH for check in route_checks),
        shadow_check_count=50,
        shadow_match_count=shadow_matches,
        alias_counts=tuple((alias, alias_counts[alias]) for alias in APPROVED_PROFILE_ALIASES),
        side_effects_started=0,
        global_target_count=0,
        live_request_count=0,
        live_attested=False,
        reason_codes=tuple(reasons),
        control_state_digest=None if control_state is None else control_state.digest,
        b3_snapshot_digest=None if b3_snapshot is None else b3_snapshot.snapshot_digest,
    )


@dataclass(frozen=True)
class RollbackControlState:
    """Contained B3 control state; it is not a global configuration snapshot."""

    workflow_enabled: bool
    auto_graph_enabled: bool
    external_capability_discovery: bool
    memory_write_mode: str
    router_adapter_version: str
    profile_registry_version: str
    plugin_hook_bundle_version: str

    def __post_init__(self) -> None:
        if type(self) is not RollbackControlState:
            raise SemanticValidationError("rollback control-state wrapper is invalid")
        if any(
            type(value) is not bool
            for value in (
                self.workflow_enabled,
                self.auto_graph_enabled,
                self.external_capability_discovery,
            )
        ):
            raise SemanticValidationError("rollback control-state flags are invalid")
        if self.memory_write_mode not in {"read_only", "proposal_only", "read_write"}:
            raise SemanticValidationError("rollback memory mode is invalid")
        for value in (
            self.router_adapter_version,
            self.profile_registry_version,
            self.plugin_hook_bundle_version,
        ):
            _require_identifier(value, "rollback version", _SAFE_VERSION)

    @classmethod
    def from_value(cls, value: object) -> "RollbackControlState":
        if type(value) is not dict or set(value) != {
            "workflow_enabled",
            "auto_graph_enabled",
            "external_capability_discovery",
            "memory_write_mode",
            "router_adapter_version",
            "profile_registry_version",
            "plugin_hook_bundle_version",
        }:
            raise SemanticValidationError("rollback control state is invalid")
        try:
            return cls(**deepcopy(value))
        except (TypeError, ValueError, SemanticValidationError) as error:
            raise SemanticValidationError("rollback control state is invalid") from error

    def to_dict(self) -> dict[str, object]:
        return {
            "workflow_enabled": self.workflow_enabled,
            "auto_graph_enabled": self.auto_graph_enabled,
            "external_capability_discovery": self.external_capability_discovery,
            "memory_write_mode": self.memory_write_mode,
            "router_adapter_version": self.router_adapter_version,
            "profile_registry_version": self.profile_registry_version,
            "plugin_hook_bundle_version": self.plugin_hook_bundle_version,
        }

    @property
    def digest(self) -> str:
        return sha256_hex(self.to_dict())


def _control_state_for_evaluation_run(run: EvaluationRun, *, memory_write_mode: str) -> RollbackControlState:
    """Derive an opaque local control state from one actual evaluated run."""

    if type(run) is not EvaluationRun or run.baseline_kind not in {None, BaselineKind.B3_LAST_KNOWN_GOOD}:
        raise SemanticValidationError("rollback control run is invalid")
    if memory_write_mode not in {"read_only", "proposal_only"}:
        raise SemanticValidationError("rollback control memory mode is invalid")
    role = "candidate" if run.baseline_kind is None else "b3"
    graph_bundle_digest = run.binding.graph_manifest_digest or run.binding.component_source_digest
    return RollbackControlState(
        workflow_enabled=True,
        auto_graph_enabled=any(
            outcome.component_evidence.graph_receipt is not None for outcome in run.outcomes
        ),
        external_capability_discovery=False,
        memory_write_mode=memory_write_mode,
        router_adapter_version=f"m8-{role}-route-{run.run_digest[:24]}",
        profile_registry_version=f"m8-registry-{run.binding.profile_registry_digest[:24]}",
        plugin_hook_bundle_version=f"m8-graph-{graph_bundle_digest[:24]}",
    )


def create_candidate_control_state(candidate_run: EvaluationRun) -> RollbackControlState:
    """Bind a local candidate transition to the evaluated candidate run."""

    if type(candidate_run) is not EvaluationRun or candidate_run.baseline_kind is not None:
        raise SemanticValidationError("rollback candidate run is invalid")
    return _control_state_for_evaluation_run(candidate_run, memory_write_mode="proposal_only")


@dataclass(frozen=True)
class B3RollbackSnapshot:
    """Typed B3 control snapshot bound to the exact evaluated baseline run."""

    snapshot_version: int
    baseline_kind: BaselineKind
    b3_run_digest: str
    binding_digest: str
    baseline_outcome_digest: str
    baseline_receipt_digest: str
    control_state: RollbackControlState
    evidence_scope: str = LOCAL_ROLLBACK_REHEARSAL
    snapshot_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not B3RollbackSnapshot:
            raise SemanticValidationError("B3 rollback snapshot wrapper is invalid")
        if self.snapshot_version != EVALUATION_VERSION or isinstance(self.snapshot_version, bool):
            raise SemanticValidationError("B3 rollback snapshot version is invalid")
        if self.baseline_kind is not BaselineKind.B3_LAST_KNOWN_GOOD:
            raise SemanticValidationError("B3 rollback snapshot baseline is invalid")
        for label, value in (
            ("B3 run digest", self.b3_run_digest),
            ("B3 binding digest", self.binding_digest),
            ("B3 outcome digest", self.baseline_outcome_digest),
            ("B3 baseline receipt digest", self.baseline_receipt_digest),
        ):
            _require_hash(value, label)
        if type(self.control_state) is not RollbackControlState:
            raise SemanticValidationError("B3 rollback control state is invalid")
        if self.evidence_scope != LOCAL_ROLLBACK_REHEARSAL:
            raise SemanticValidationError("B3 rollback snapshot scope is invalid")
        digest = sha256_hex(self._digest_input())
        if self.snapshot_digest and self.snapshot_digest != digest:
            raise SemanticValidationError("B3 rollback snapshot digest does not match")
        object.__setattr__(self, "snapshot_digest", digest)

    def _digest_input(self) -> dict[str, object]:
        return {
            "snapshot_version": self.snapshot_version,
            "baseline_kind": self.baseline_kind.value,
            "b3_run_digest": self.b3_run_digest,
            "binding_digest": self.binding_digest,
            "baseline_outcome_digest": self.baseline_outcome_digest,
            "baseline_receipt_digest": self.baseline_receipt_digest,
            "control_state": self.control_state.to_dict(),
            "evidence_scope": self.evidence_scope,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._digest_input(), "snapshot_digest": self.snapshot_digest}

    @classmethod
    def from_value(cls, value: object) -> "B3RollbackSnapshot":
        raw = _require_exact_keys(
            value,
            frozenset(
                (
                    "snapshot_version",
                    "baseline_kind",
                    "b3_run_digest",
                    "binding_digest",
                    "baseline_outcome_digest",
                    "baseline_receipt_digest",
                    "control_state",
                    "evidence_scope",
                    "snapshot_digest",
                )
            ),
            "B3 rollback snapshot",
        )
        try:
            return cls(
                snapshot_version=raw["snapshot_version"],
                baseline_kind=BaselineKind(raw["baseline_kind"]),
                b3_run_digest=raw["b3_run_digest"],
                binding_digest=raw["binding_digest"],
                baseline_outcome_digest=raw["baseline_outcome_digest"],
                baseline_receipt_digest=raw["baseline_receipt_digest"],
                control_state=RollbackControlState.from_value(raw["control_state"]),
                evidence_scope=raw["evidence_scope"],
                snapshot_digest=raw["snapshot_digest"],
            )
        except (KeyError, TypeError, ValueError, SemanticValidationError) as error:
            raise SemanticValidationError("B3 rollback snapshot is invalid") from error


def create_b3_rollback_snapshot(b3_run: EvaluationRun) -> B3RollbackSnapshot:
    """Capture only an opaque, in-memory B3 state from the evaluated baseline."""

    if type(b3_run) is not EvaluationRun or b3_run.baseline_kind is not BaselineKind.B3_LAST_KNOWN_GOOD:
        raise SemanticValidationError("B3 rollback run is invalid")
    receipt = create_baseline_receipt(b3_run)
    return B3RollbackSnapshot(
        snapshot_version=EVALUATION_VERSION,
        baseline_kind=BaselineKind.B3_LAST_KNOWN_GOOD,
        b3_run_digest=b3_run.run_digest,
        binding_digest=b3_run.binding.binding_digest,
        baseline_outcome_digest=receipt.outcome_digest,
        baseline_receipt_digest=receipt.receipt_digest,
        control_state=_control_state_for_evaluation_run(b3_run, memory_write_mode="read_only"),
    )


def last_known_good_control_state(b3_snapshot: B3RollbackSnapshot) -> RollbackControlState:
    """Return a copy only from a typed, evaluated B3 control snapshot."""

    if type(b3_snapshot) is not B3RollbackSnapshot:
        raise SemanticValidationError("B3 rollback snapshot is invalid")
    return RollbackControlState.from_value(b3_snapshot.control_state.to_dict())


@dataclass(frozen=True)
class RollbackRehearsalResult:
    """Receipt for an in-memory B3 restoration only."""

    state: GateState
    candidate_digest: str
    applied_digest: str
    b3_digest: str
    b3_snapshot: B3RollbackSnapshot
    restored_digest: str
    candidate_applied: bool
    exact_equality: bool
    post_restore_canary_state: GateState
    post_restore_canary_digest: str
    post_restore_canary_control_state_digest: str
    post_restore_canary_b3_snapshot_digest: str
    global_target_count: int
    material_actions_started: int
    evidence_scope: str = LOCAL_ROLLBACK_REHEARSAL
    rehearsal_digest: str = field(default="")

    def __post_init__(self) -> None:
        if type(self) is not RollbackRehearsalResult:
            raise SemanticValidationError("rollback rehearsal wrapper is invalid")
        if self.state not in {GateState.PASS, GateState.FAIL, GateState.BLOCKED}:
            raise SemanticValidationError("rollback rehearsal state is invalid")
        for label, value in (
            ("candidate_digest", self.candidate_digest),
            ("applied_digest", self.applied_digest),
            ("b3_digest", self.b3_digest),
            ("restored_digest", self.restored_digest),
            ("post_restore_canary_digest", self.post_restore_canary_digest),
            ("post-restore canary control-state digest", self.post_restore_canary_control_state_digest),
            ("post-restore canary B3 snapshot digest", self.post_restore_canary_b3_snapshot_digest),
        ):
            _require_hash(value, label)
        if type(self.b3_snapshot) is not B3RollbackSnapshot:
            raise SemanticValidationError("rollback B3 snapshot is invalid")
        if (
            self.b3_digest != self.b3_snapshot.control_state.digest
            or self.restored_digest != self.b3_snapshot.control_state.digest
            or self.post_restore_canary_control_state_digest != self.b3_snapshot.control_state.digest
            or self.post_restore_canary_b3_snapshot_digest != self.b3_snapshot.snapshot_digest
        ):
            raise SemanticValidationError("rollback B3 restore or canary binding is invalid")
        if type(self.candidate_applied) is not bool or type(self.exact_equality) is not bool:
            raise SemanticValidationError("rollback equality is invalid")
        if self.post_restore_canary_state not in {GateState.PASS, GateState.FAIL, GateState.BLOCKED}:
            raise SemanticValidationError("rollback post-restore canary state is invalid")
        _require_int(self.global_target_count, "rollback global_target_count", minimum=0)
        _require_int(self.material_actions_started, "rollback material_actions_started", minimum=0)
        if self.evidence_scope != LOCAL_ROLLBACK_REHEARSAL:
            raise SemanticValidationError("rollback evidence scope is invalid")
        if self.state is GateState.PASS and not (
            self.candidate_applied
            and self.candidate_digest == self.applied_digest
            and self.exact_equality
            and self.restored_digest == self.b3_digest
            and self.post_restore_canary_state is GateState.PASS
            and self.post_restore_canary_control_state_digest == self.restored_digest
            and self.post_restore_canary_b3_snapshot_digest == self.b3_snapshot.snapshot_digest
            and self.global_target_count == 0
            and self.material_actions_started == 0
        ):
            raise SemanticValidationError("passing rollback rehearsal lacks an applied restore transition")
        digest = sha256_hex(self._digest_input())
        if self.rehearsal_digest and self.rehearsal_digest != digest:
            raise SemanticValidationError("rollback rehearsal digest does not match")
        object.__setattr__(self, "rehearsal_digest", digest)

    def _digest_input(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "candidate_digest": self.candidate_digest,
            "applied_digest": self.applied_digest,
            "b3_digest": self.b3_digest,
            "b3_snapshot": self.b3_snapshot.to_dict(),
            "restored_digest": self.restored_digest,
            "candidate_applied": self.candidate_applied,
            "exact_equality": self.exact_equality,
            "post_restore_canary_state": self.post_restore_canary_state.value,
            "post_restore_canary_digest": self.post_restore_canary_digest,
            "post_restore_canary_control_state_digest": self.post_restore_canary_control_state_digest,
            "post_restore_canary_b3_snapshot_digest": self.post_restore_canary_b3_snapshot_digest,
            "global_target_count": self.global_target_count,
            "material_actions_started": self.material_actions_started,
            "evidence_scope": self.evidence_scope,
        }

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": EVALUATION_VERSION, **self._digest_input(), "rehearsal_digest": self.rehearsal_digest}


class _LocalControlStateHolder:
    """Own only an in-memory control-state copy used by the local rehearsal."""

    def __init__(self, initial: RollbackControlState) -> None:
        if type(initial) is not RollbackControlState:
            raise SemanticValidationError("rollback holder initial state is invalid")
        self._state = RollbackControlState.from_value(initial.to_dict())

    @property
    def state(self) -> RollbackControlState:
        return RollbackControlState.from_value(self._state.to_dict())

    def apply(self, candidate: RollbackControlState) -> RollbackControlState:
        if type(candidate) is not RollbackControlState:
            raise SemanticValidationError("rollback holder candidate is invalid")
        self._state = RollbackControlState.from_value(candidate.to_dict())
        return self.state

    def restore(self, target: RollbackControlState) -> RollbackControlState:
        if type(target) is not RollbackControlState:
            raise SemanticValidationError("rollback holder restore target is invalid")
        self._state = RollbackControlState.from_value(target.to_dict())
        return self.state


def rehearse_local_rollback(
    candidate: RollbackControlState | Mapping[str, Any],
    *,
    b3_snapshot: B3RollbackSnapshot,
    expected_candidate_digest: str | None = None,
) -> RollbackRehearsalResult:
    """Restore the evaluated B3 local state and bind the post-restore canary."""

    if type(b3_snapshot) is not B3RollbackSnapshot:
        raise SemanticValidationError("rollback B3 snapshot is invalid")
    if type(candidate) is RollbackControlState:
        candidate_state = candidate
    elif type(candidate) is dict:
        candidate_state = RollbackControlState.from_value(candidate)
    else:
        raise SemanticValidationError("rollback candidate state is invalid")
    if expected_candidate_digest is not None:
        _require_hash(expected_candidate_digest, "expected candidate digest")
        if expected_candidate_digest != candidate_state.digest:
            raise SemanticValidationError("rollback candidate digest does not match")
    b3 = last_known_good_control_state(b3_snapshot)
    holder = _LocalControlStateHolder(b3)
    applied = holder.apply(candidate_state)
    candidate_applied = applied.digest == candidate_state.digest
    restored = holder.restore(b3)
    exact = restored.digest == b3.digest
    post_restore_canary = run_local_canary(control_state=holder.state, b3_snapshot=b3_snapshot)
    if (
        post_restore_canary.control_state_digest is None
        or post_restore_canary.b3_snapshot_digest is None
    ):
        raise SemanticValidationError("post-restore canary control binding is unavailable")
    passed = (
        candidate_applied
        and exact
        and post_restore_canary.state is GateState.PASS
        and post_restore_canary.control_state_digest == restored.digest
        and post_restore_canary.b3_snapshot_digest == b3_snapshot.snapshot_digest
    )
    return RollbackRehearsalResult(
        state=GateState.PASS if passed else GateState.FAIL,
        candidate_digest=candidate_state.digest,
        applied_digest=applied.digest,
        b3_digest=b3.digest,
        b3_snapshot=b3_snapshot,
        restored_digest=restored.digest,
        candidate_applied=candidate_applied,
        exact_equality=exact,
        post_restore_canary_state=post_restore_canary.state,
        post_restore_canary_digest=post_restore_canary.canary_digest,
        post_restore_canary_control_state_digest=post_restore_canary.control_state_digest,
        post_restore_canary_b3_snapshot_digest=post_restore_canary.b3_snapshot_digest,
        global_target_count=0,
        material_actions_started=0,
    )


@dataclass(frozen=True)
class ReceiptArtifact:
    """Metadata for one fixed-name project-local receipt."""

    name: str
    output_digest: str
    byte_count: int

    def __post_init__(self) -> None:
        if type(self) is not ReceiptArtifact or self.name not in _RECEIPT_NAMES:
            raise SemanticValidationError("receipt artifact is invalid")
        _require_hash(self.output_digest, "receipt output digest")
        _require_int(self.byte_count, "receipt byte_count", minimum=1)

    def to_dict(self) -> dict[str, object]:
        return {"artifact_name": self.name, "output_digest": self.output_digest, "byte_count": self.byte_count}


_TYPED_RECEIPT_WRITER = "m8-typed-receipts-3"
_RECEIPT_KINDS = {
    "m8-baseline-receipts.json": "baseline-receipts",
    "m8-local-evaluation.json": "local-evaluation",
    "m8-m4-local-evaluation.json": "m4-local-evaluation",
    "m8-performance-local-evaluation.json": "performance-local-evaluation",
    "m8-local-canary.json": "local-canary",
    "m8-rollback-rehearsal.json": "rollback-rehearsal",
    "m8-release-report.json": "release-report",
    "m8-verification.json": "verification",
}
_RECEIPT_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_LEGACY_PROVISIONAL_FILE_DIGESTS = {
    # The only pre-typed M8 outputs this migration may replace. Any other
    # existing file is treated as user-owned and causes a visible failure.
    "m8-baseline-receipts.json": "3564d3811e3234fa5f61fad5b954ca44726e5c5167e2474be38dbb47dadf710d",
    "m8-local-evaluation.json": "01dab2af4d69016f70e606be540e5706fd4976d7d22d477af159ba918529d04a",
    "m8-local-canary.json": "d1a772b92ac656faaf18acc85a4eb939c7fb73e4b88008411ae0afbde5c9f779",
    "m8-rollback-rehearsal.json": "0c775190fed72eb6e288da3fe242cb67ba04bc93be4f3111d97a30fcd491ba10",
    "m8-release-report.json": "8ad595915c9344ccbb067b24d07b3bf5a18acdf884b314f92f848a3364774fe2",
}
_OWNED_PREDECESSOR_OUTPUT_DIGESTS = {
    # A source change must add its immediately prior generated digest here before
    # refreshing it. This prevents a merely well-shaped user edit from being
    # mistaken for a writer-owned artifact.
    "m8-baseline-receipts.json": frozenset(
        (
            "3589036797dfe1494f78a1331ae2523316568a77a38812edcb4d33f3ff66da4a",
            "f13d0b3eb210a584414a67027f224b86e161733f4fba2667cf4964a3a63aff36",
            "cb6a036d8caa6e0b46c8cb524f51073897ad4531d5255c43e1f4975140a3adfb",
            "2d769b2fb2baf70ad8695833992182291bc3b3c029ba229a336a331dc7cf17db",
            "908aea4d3b5c5014a04a8bb88da5f0976a6419fb94abc69906c6a3fb2dd38f98",
            "aa439f0f0b173a1004563ee22f9f0fd25ac6c6e6418c450d974e43346d9dde8e",
            "53e826a447ce6874b7b52b018b819c7ba796375a328fa26fc48666fad06fc705",
        )
    ),
    "m8-local-evaluation.json": frozenset(
        (
            "7592d84e36c41a5fb858ab688c1fc5a454e9a4edf5bc72e960862a7c2a465ba0",
            "b3ed7c7e25afadc44db8c7f43e6bf180e7cfda73ca28892ab759bb4cb3991d48",
            "53e3e31b2a63024c8b6574f618f0fb0c1b104612f7cd3a155c349b7670b09ddf",
            "084d4b4dbe43b2444716abe43efccdb9c6d19649d352b4dee8214b5ab5a324ae",
            "32cfa262b37efc59ff198c60939511507e8c2d2971147cbeef3859b500a2cf23",
            "c651c1dc2a2eecd7f60168702ead1514dd403519c756b7cb3997520d7c0a5ef0",
        )
    ),
    "m8-m4-local-evaluation.json": frozenset(
        (
            "bfe187adeffb105720479c06b0bf5c7cae77335293131ca3b10021250915a1b6",
            "05f58be8dc6ab46380ec468c3e70369c59be45f21a0d530331917c7d72981446",
        )
    ),
    "m8-local-canary.json": frozenset(
        (
            "f23cbbcd5add9d98cb13582c1b1aed6029c7c6ee09b2bf76bf34d40db0783390",
            "d71293735fc73d4b30140a6fc323d5c90bf468fcf794686d006b85492771ba32",
            "be8a02aced2ae788fd0afb4624b3cf9911d48b0d2d39e138adeee3c92f70f2cb",
            "d71293735fc73d4b30140a6fc323d5c90bf468fcf794686d006b85492771ba32",
        )
    ),
    "m8-rollback-rehearsal.json": frozenset(
        (
            "49d0ab19d2dcde4c256d8c24a8a9096086e67d284fffc38bac4aeb48ddd50f62",
            "04aa9e36d5acba2e2b7f76b36ae5d5c6441dc9b7c969f9640240b5b5db37986b",
            "6fa6312cc575c368fd098b2a6cc06ba92ccab3793b39d27884be9b47aa625cd1",
            "e0f27b41a2985eef1c2f8e8830be2f1724017720239b557d364a08bc2384725c",
            "673dcdd43334f0b0a03dd3a49f6d111fced5bc8e2d59c69ffd37780bc9a27a09",
            "2b327dbde0c56abf69a9de0d9647f549dccc1cbdda546b51dc262a38e51db5d4",
        )
    ),
    "m8-release-report.json": frozenset(
        (
            "a7318a60232992c670128cd7e2d45588b5751b6f998ac022c264a5ae6fc73ffb",
            "c8c20d401b7a4b05fa8d3c6685a9927a46b53c0406bda807528eb2901de00a2d",
            "5245abaf5c9ba168770799aa6fa56a7cd99206e78af974f797d8cc4e748cb636",
            "49aab89e40dc1049406db67d4a9b0af3897082d863ca2a6f9611087d08655ab6",
            "a2f33a0c822e601b31a9b7cdcaba026dbca9bd225c2c103a4e57ff2ce26ae8c7",
            "e89adbe804a09b9f31cd4f38fa8374425eaa0ca50f636546050e791a8b703a71",
        )
    ),
    "m8-verification.json": frozenset(
        (
            "2c04701a6cc478f35401ddbbe1e1375a458bf2c8bedb07d609963be511968d4e",
            "d7900bdac57663214bbdc14e90d46c07c0ac3711d05b635c16f37cbf0f96ae48",
            "3e5618cce5061c6dd213f79b12bc3fc6ebc1511000b8bf533c6408c227ce3545",
            "78814b2873a0774b28b10d87b89242a54156714db62e1b94b0ab9cd4a4953ae0",
            "9b8b297846a6e914f368c310580f77c5ad5c226770fffc7cef2bbfae1b4e2157",
            "daabfbaeef823b89bf29594cc37da04877ac2b8fd0cc367b560df558d5b0dfe0",
        )
    ),
}


def _receipt_target(name: str) -> Path:
    if type(name) is not str or name not in _RECEIPT_NAMES:
        raise SemanticValidationError("receipt target is invalid")
    lexical_root = repository_root() / "artifacts"
    if lexical_root.is_symlink() or not lexical_root.is_dir():
        raise SemanticValidationError("receipt target is invalid")
    root = lexical_root.resolve(strict=True)
    target = lexical_root / name
    if target.exists() and target.is_symlink():
        raise SemanticValidationError("receipt target is invalid")
    try:
        target.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError as error:
        raise SemanticValidationError("receipt target is invalid") from error
    return target


def _assert_redacted_receipt_value(value: object, *, depth: int = 0) -> None:
    """Bound receipt trees before hashing or writing them to an artifact."""

    if depth > 16:
        raise SemanticValidationError("receipt nesting exceeds the local bound")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > 1_000_000_000:
            raise SemanticValidationError("receipt integer exceeds the local bound")
        return
    if type(value) is str:
        _require_safe_fixture_text(value, "receipt value", maximum=256)
        return
    if type(value) is list:
        if len(value) > 512:
            raise SemanticValidationError("receipt list exceeds the local bound")
        for item in value:
            _assert_redacted_receipt_value(item, depth=depth + 1)
        return
    if type(value) is dict:
        if len(value) > 128:
            raise SemanticValidationError("receipt object exceeds the local bound")
        for key, item in value.items():
            if type(key) is not str or _RECEIPT_FIELD.fullmatch(key) is None:
                raise SemanticValidationError("receipt field is invalid")
            _assert_redacted_receipt_value(item, depth=depth + 1)
        return
    raise SemanticValidationError("receipt contains an unsupported value")


def _receipt_header(name: str) -> dict[str, object]:
    return {
        "schema_version": EVALUATION_VERSION,
        "milestone": "M8",
        "receipt_kind": _RECEIPT_KINDS[name],
        "writer_version": _TYPED_RECEIPT_WRITER,
        "timestamp": M8_EVIDENCE_TIMESTAMP,
    }


def _require_payload_keys(name: str, payload: Mapping[str, object], keys: frozenset[str]) -> None:
    if type(payload) is not dict or set(payload) != keys:
        raise SemanticValidationError(f"{name} receipt shape is invalid")
    if (
        payload.get("schema_version") != EVALUATION_VERSION
        or payload.get("milestone") != "M8"
        or payload.get("receipt_kind") != _RECEIPT_KINDS[name]
        or payload.get("writer_version") != _TYPED_RECEIPT_WRITER
        or payload.get("timestamp") != M8_EVIDENCE_TIMESTAMP
    ):
        raise SemanticValidationError(f"{name} receipt identity is invalid")


def _validate_artifact_index(value: object, expected_names: Sequence[str]) -> tuple[ReceiptArtifact, ...]:
    if type(value) is not list or len(value) != len(expected_names):
        raise SemanticValidationError("receipt artifact index is invalid")
    artifacts: list[ReceiptArtifact] = []
    for item in value:
        raw = _require_exact_keys(item, frozenset(("artifact_name", "output_digest", "byte_count")), "artifact index")
        artifacts.append(
            ReceiptArtifact(
                name=raw["artifact_name"], output_digest=raw["output_digest"], byte_count=raw["byte_count"]
            )
        )
    if tuple(item.name for item in artifacts) != tuple(expected_names):
        raise SemanticValidationError("receipt artifact index ordering is invalid")
    return tuple(artifacts)


def _validate_run_projection(value: object, *, expected_baseline_kind: BaselineKind | None, label: str) -> dict[str, object]:
    raw = _require_exact_keys(
        value,
        frozenset(
            (
                "run_id",
                "baseline_kind",
                "binding_digest",
                "outcome_digests",
                "evidence_scope",
                "live_attested",
                "case_count",
                "run_digest",
            )
        ),
        label,
    )
    _require_identifier(raw["run_id"], f"{label} run_id", re.compile(r"^run:[a-z0-9][a-z0-9._-]{2,95}$"))
    expected_kind = None if expected_baseline_kind is None else expected_baseline_kind.value
    if (
        raw["baseline_kind"] != expected_kind
        or raw["evidence_scope"] != SYNTHETIC_LOCAL
        or raw["live_attested"] is not False
    ):
        raise SemanticValidationError(f"{label} is invalid")
    _require_hash(raw["binding_digest"], f"{label} binding digest")
    _require_hash(raw["run_digest"], f"{label} run digest")
    case_count = _require_int(raw["case_count"], f"{label} case count", minimum=1, maximum=512)
    if type(raw["outcome_digests"]) is not list or len(raw["outcome_digests"]) != case_count:
        raise SemanticValidationError(f"{label} outcome projection is invalid")
    for digest in raw["outcome_digests"]:
        _require_hash(digest, f"{label} outcome digest")
    expected_digest = sha256_hex(
        {
            "run_id": raw["run_id"],
            "baseline_kind": raw["baseline_kind"],
            "binding_digest": raw["binding_digest"],
            "outcome_digests": raw["outcome_digests"],
            "evidence_scope": raw["evidence_scope"],
            "live_attested": raw["live_attested"],
        }
    )
    if raw["run_digest"] != expected_digest:
        raise SemanticValidationError(f"{label} digest does not match")
    return raw


def _validate_typed_receipt(name: str, payload: Mapping[str, object]) -> None:
    """Dispatch exact closed receipt schemas; no free-form receipt type exists."""

    if name == "m8-baseline-receipts.json":
        _require_payload_keys(
            name,
            payload,
            frozenset(
                (
                    "schema_version", "milestone", "receipt_kind", "writer_version", "timestamp", "status",
                    "evidence_scope", "live_attested", "baseline_receipts",
                )
            ),
        )
        if payload["status"] not in {"LOCAL_COMPONENTS_VERIFIED", "LOCAL_COMPONENTS_FAILED"}:
            raise SemanticValidationError("baseline receipt status is invalid")
        if payload["evidence_scope"] != SYNTHETIC_LOCAL or payload["live_attested"] is not False:
            raise SemanticValidationError("baseline receipt boundary is invalid")
        raw_receipts = payload["baseline_receipts"]
        if type(raw_receipts) is not list or len(raw_receipts) != len(BaselineKind):
            raise SemanticValidationError("baseline receipt set is invalid")
        receipts = tuple(BaselineReceipt.from_value(item) for item in raw_receipts)
        if {item.baseline_kind for item in receipts} != set(BaselineKind):
            raise SemanticValidationError("baseline receipt kinds are invalid")
        if tuple(item.baseline_kind for item in receipts) != tuple(BaselineKind):
            raise SemanticValidationError("baseline receipt ordering is invalid")
    elif name == "m8-local-evaluation.json":
        _require_payload_keys(
            name,
            payload,
            frozenset(
                (
                    "schema_version", "milestone", "receipt_kind", "writer_version", "timestamp", "status",
                    "evidence_scope", "live_attested", "corpus_scale", "dataset_manifests", "candidate",
                    "b3_baseline", "b3_rollback_snapshot", "baseline_receipt_digests", "comparison",
                    "blinded_review", "m4_evaluation", "performance_measurement", "gates", "corpus_limit",
                )
            ),
        )
        if payload["status"] not in {"LOCAL_COMPONENTS_VERIFIED", "LOCAL_COMPONENTS_FAILED"}:
            raise SemanticValidationError("evaluation receipt status is invalid")
        if payload["evidence_scope"] != SYNTHETIC_LOCAL or payload["live_attested"] is not False:
            raise SemanticValidationError("evaluation receipt boundary is invalid")
        candidate = _validate_run_projection(
            payload["candidate"], expected_baseline_kind=None, label="candidate projection"
        )
        b3 = _validate_run_projection(
            payload["b3_baseline"],
            expected_baseline_kind=BaselineKind.B3_LAST_KNOWN_GOOD,
            label="B3 baseline projection",
        )
        b3_snapshot = B3RollbackSnapshot.from_value(payload["b3_rollback_snapshot"])
        if (
            b3_snapshot.b3_run_digest != b3["run_digest"]
            or b3_snapshot.binding_digest != b3["binding_digest"]
            or b3_snapshot.binding_digest != candidate["binding_digest"]
            or b3_snapshot.baseline_outcome_digest
            != sha256_hex({"outcome_digests": b3["outcome_digests"]})
        ):
            raise SemanticValidationError("evaluation B3 rollback snapshot is unbound")
        manifests = _require_exact_keys(
            payload["dataset_manifests"], frozenset(("frozen", "rotating", "combined")), "dataset manifests"
        )
        for manifest in manifests.values():
            DatasetManifest.from_value(manifest)
        if type(payload["baseline_receipt_digests"]) is not list or len(payload["baseline_receipt_digests"]) != 4:
            raise SemanticValidationError("evaluation baseline bindings are invalid")
        for digest in payload["baseline_receipt_digests"]:
            _require_hash(digest, "evaluation baseline receipt digest")
        if payload["baseline_receipt_digests"][-1] != b3_snapshot.baseline_receipt_digest:
            raise SemanticValidationError("evaluation B3 baseline receipt is unbound")
        M4LocalEvaluation.from_receipt(payload["m4_evaluation"])
        M8PerformanceMeasurement.from_receipt(payload["performance_measurement"])
        if type(payload["gates"]) is not list or not payload["gates"]:
            raise SemanticValidationError("evaluation gates are invalid")
        for gate in payload["gates"]:
            MetricGate.from_value(gate)
    elif name == "m8-m4-local-evaluation.json":
        _require_payload_keys(
            name,
            payload,
            frozenset(
                (
                    "schema_version", "milestone", "receipt_kind", "writer_version", "timestamp",
                    "evidence_version", "evidence_scope", "corpus_digest", "fixture_source_digest",
                    "case_evidence_digests", "global_target_count", "material_actions_started",
                    "network_requests_started", "live_attested", "state", "case_count", "cases", "result_digest",
                )
            ),
        )
        M4LocalEvaluation.from_receipt(
            {
                key: value
                for key, value in payload.items()
                if key
                not in {"schema_version", "milestone", "receipt_kind", "writer_version", "timestamp"}
            }
        )
    elif name == "m8-performance-local-evaluation.json":
        _require_payload_keys(
            name,
            payload,
            frozenset(
                (
                    "schema_version", "milestone", "receipt_kind", "writer_version", "timestamp",
                    "evidence_version", "evidence_scope", "measurement_clock", "promotion_disposition",
                    "manifest_digest", "sample_count", "parallel_faster_sample_count", "parallel_max_active_workers",
                    "serial_max_active_workers", "raw_intermediate_token_count", "integrator_context_tokens",
                    "context_reduction_percent", "graph_contract_passed", "context_proxy_passed",
                    "global_target_count", "material_actions_started", "network_requests_started", "live_attested",
                    "state", "result_digest",
                )
            ),
        )
        M8PerformanceMeasurement.from_receipt(
            {
                key: value
                for key, value in payload.items()
                if key
                not in {"schema_version", "milestone", "receipt_kind", "writer_version", "timestamp"}
            }
        )
    elif name == "m8-local-canary.json":
        _require_payload_keys(
            name,
            payload,
            frozenset(
                (
                    "schema_version", "milestone", "receipt_kind", "writer_version", "timestamp", "status",
                    "offline_route_evidence_scope", "shadow_evidence_scope", "state", "route_check_count",
                    "route_match_count", "shadow_check_count", "shadow_match_count", "alias_counts",
                    "side_effects_started", "global_target_count", "live_request_count", "live_attested",
                    "reason_codes", "control_state_digest", "b3_snapshot_digest", "canary_digest",
                )
            ),
        )
        if payload["status"] != payload["state"] or payload["offline_route_evidence_scope"] != SYNTHETIC_LOCAL:
            raise SemanticValidationError("canary receipt status is invalid")
        if payload["shadow_evidence_scope"] != SIMULATED_SHADOW_READ_ONLY:
            raise SemanticValidationError("canary shadow scope is invalid")
        aliases = payload["alias_counts"]
        if type(aliases) is not list:
            raise SemanticValidationError("canary aliases are invalid")
        alias_pairs = tuple((item.get("profile_alias"), item.get("count")) for item in aliases if type(item) is dict)
        if len(alias_pairs) != len(aliases):
            raise SemanticValidationError("canary aliases are invalid")
        LocalCanaryResult(
            state=GateState(payload["state"]),
            route_check_count=payload["route_check_count"],
            route_match_count=payload["route_match_count"],
            shadow_check_count=payload["shadow_check_count"],
            shadow_match_count=payload["shadow_match_count"],
            alias_counts=alias_pairs,
            side_effects_started=payload["side_effects_started"],
            global_target_count=payload["global_target_count"],
            live_request_count=payload["live_request_count"],
            live_attested=payload["live_attested"],
            reason_codes=tuple(payload["reason_codes"]),
            control_state_digest=payload["control_state_digest"],
            b3_snapshot_digest=payload["b3_snapshot_digest"],
            canary_digest=payload["canary_digest"],
        )
    elif name == "m8-rollback-rehearsal.json":
        _require_payload_keys(
            name,
            payload,
            frozenset(
                (
                    "schema_version", "milestone", "receipt_kind", "writer_version", "timestamp", "state",
                    "candidate_digest", "applied_digest", "b3_digest", "b3_snapshot", "restored_digest",
                    "candidate_applied", "exact_equality", "post_restore_canary_state", "post_restore_canary_digest",
                    "post_restore_canary_control_state_digest", "post_restore_canary_b3_snapshot_digest",
                    "global_target_count", "material_actions_started", "evidence_scope", "rehearsal_digest",
                )
            ),
        )
        RollbackRehearsalResult(
            state=GateState(payload["state"]),
            candidate_digest=payload["candidate_digest"],
            applied_digest=payload["applied_digest"],
            b3_digest=payload["b3_digest"],
            b3_snapshot=B3RollbackSnapshot.from_value(payload["b3_snapshot"]),
            restored_digest=payload["restored_digest"],
            candidate_applied=payload["candidate_applied"],
            exact_equality=payload["exact_equality"],
            post_restore_canary_state=GateState(payload["post_restore_canary_state"]),
            post_restore_canary_digest=payload["post_restore_canary_digest"],
            post_restore_canary_control_state_digest=payload["post_restore_canary_control_state_digest"],
            post_restore_canary_b3_snapshot_digest=payload["post_restore_canary_b3_snapshot_digest"],
            global_target_count=payload["global_target_count"],
            material_actions_started=payload["material_actions_started"],
            evidence_scope=payload["evidence_scope"],
            rehearsal_digest=payload["rehearsal_digest"],
        )
    elif name == "m8-release-report.json":
        _require_payload_keys(
            name,
            payload,
            frozenset(
                (
                    "schema_version", "milestone", "receipt_kind", "writer_version", "timestamp", "release_status",
                    "promotion_status", "local_component_status", "evidence_scope", "live_attested", "artifact_index",
                    "local_gate_matrix", "deferred_live_gates", "unresolved_risks", "rollback_limitation",
                    "boundary_assertions",
                )
            ),
        )
        if payload["release_status"] not in {"FAIL", "BLOCKED"} or payload["promotion_status"] not in {
            "LOCAL_COMPONENTS_FAILED", "M9_EVIDENCE_REQUIRED"
        }:
            raise SemanticValidationError("release receipt cannot claim promotion readiness")
        if payload["evidence_scope"] != SYNTHETIC_LOCAL or payload["live_attested"] is not False:
            raise SemanticValidationError("release receipt boundary is invalid")
        _validate_artifact_index(
            payload["artifact_index"],
            (
                "m8-baseline-receipts.json",
                "m8-local-evaluation.json",
                "m8-m4-local-evaluation.json",
                "m8-performance-local-evaluation.json",
                "m8-local-canary.json",
                "m8-rollback-rehearsal.json",
            ),
        )
    elif name == "m8-verification.json":
        _require_payload_keys(
            name,
            payload,
            frozenset(
                (
                    "schema_version", "milestone", "receipt_kind", "writer_version", "timestamp", "status",
                    "release_status", "evidence_scope", "live_attested", "source_binding_digest", "artifact_index",
                    "verification_scope", "reason_codes",
                )
            ),
        )
        if payload["status"] != "PASS" or payload["evidence_scope"] != SYNTHETIC_LOCAL or payload["live_attested"] is not False:
            raise SemanticValidationError("verification receipt status is invalid")
        _require_hash(payload["source_binding_digest"], "verification source binding")
        _validate_artifact_index(
            payload["artifact_index"],
            (
                "m8-baseline-receipts.json",
                "m8-local-evaluation.json",
                "m8-m4-local-evaluation.json",
                "m8-performance-local-evaluation.json",
                "m8-local-canary.json",
                "m8-rollback-rehearsal.json",
                "m8-release-report.json",
            ),
        )
    else:
        raise SemanticValidationError("receipt kind is unavailable")
    _assert_redacted_receipt_value(payload)


def _serialized_receipt(name: str, payload: Mapping[str, object]) -> tuple[str, bytes]:
    _validate_typed_receipt(name, payload)
    digest = sha256_hex(payload)
    serialized = json.dumps(
        {**payload, "output_digest": digest}, ensure_ascii=True, allow_nan=False, indent=2, sort_keys=True
    ).encode("utf-8") + b"\n"
    return digest, serialized


def _receipt_artifact(name: str, payload: Mapping[str, object]) -> ReceiptArtifact:
    digest, serialized = _serialized_receipt(name, payload)
    return ReceiptArtifact(name=name, output_digest=digest, byte_count=len(serialized))


def _load_raw_receipt(name: str) -> tuple[dict[str, object], str]:
    target = _receipt_target(name)
    try:
        raw = loads_strict_json(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, DuplicateKeyError) as error:
        raise SemanticValidationError("serialized receipt is invalid") from error
    if type(raw) is not dict or "output_digest" not in raw:
        raise SemanticValidationError("serialized receipt is invalid")
    received_digest = raw.pop("output_digest")
    _require_hash(received_digest, "serialized receipt digest")
    if sha256_hex(raw) != received_digest:
        raise SemanticValidationError("serialized receipt digest does not match")
    return deepcopy(raw), received_digest


def load_serialized_receipt(name: str) -> dict[str, Any]:
    """Load one closed M8 receipt; arbitrary safe-looking payloads are rejected."""

    payload, _ = _load_raw_receipt(name)
    _validate_typed_receipt(name, payload)
    return deepcopy(payload)


def _existing_owned_digest(name: str, *, expected_digest: str) -> str | None:
    _require_hash(expected_digest, "expected receipt output digest")
    target = _receipt_target(name)
    if not target.exists():
        return None
    try:
        payload, _ = _load_raw_receipt(name)
    except SemanticValidationError as receipt_error:
        try:
            file_digest = sha256_bytes(target.read_bytes())
        except OSError as error:
            raise SemanticValidationError("existing receipt cannot be read") from error
        if _LEGACY_PROVISIONAL_FILE_DIGESTS.get(name) != file_digest:
            raise SemanticValidationError("existing receipt is not an owned typed M8 artifact") from receipt_error
        return file_digest
    digest = sha256_hex(payload)
    if digest == expected_digest or digest in _OWNED_PREDECESSOR_OUTPUT_DIGESTS.get(name, frozenset()):
        return digest
    try:
        file_digest = sha256_bytes(target.read_bytes())
    except OSError as error:
        raise SemanticValidationError("existing receipt cannot be read") from error
    if _LEGACY_PROVISIONAL_FILE_DIGESTS.get(name) == file_digest:
        return file_digest
    raise SemanticValidationError("existing typed receipt is not an approved predecessor")


def _write_typed_receipt(name: str, payload: Mapping[str, object]) -> ReceiptArtifact:
    """Replace only an absent, recognized legacy, or currently typed receipt via CAS."""

    digest, serialized = _serialized_receipt(name, payload)
    target = _receipt_target(name)
    expected_old_digest = _existing_owned_digest(name, expected_digest=digest)
    lock_path = target.parent / ".m8-receipt.lock"
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            current_digest = _existing_owned_digest(name, expected_digest=digest)
            if current_digest != expected_old_digest:
                raise SemanticValidationError("receipt changed before compare-and-swap replacement")
            if current_digest == digest:
                return ReceiptArtifact(name=name, output_digest=digest, byte_count=len(serialized))
            descriptor, temporary_name = tempfile.mkstemp(prefix=".m8-", suffix=".tmp", dir=target.parent)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(serialized)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, target)
                directory_descriptor = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except Exception:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                raise
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return ReceiptArtifact(name=name, output_digest=digest, byte_count=len(serialized))


def serialize_receipt(name: str, payload: Mapping[str, Any]) -> ReceiptArtifact:
    """Reject the retired generic writer so callers cannot mint an M8 receipt."""

    del name, payload
    raise SemanticValidationError("generic M8 receipt serialization is prohibited")


def _baseline_payload(result: LocalEvaluationResult) -> dict[str, object]:
    return {
        **_receipt_header("m8-baseline-receipts.json"),
        "status": "LOCAL_COMPONENTS_VERIFIED" if result.passed else "LOCAL_COMPONENTS_FAILED",
        "evidence_scope": SYNTHETIC_LOCAL,
        "live_attested": False,
        "baseline_receipts": [item.to_dict() for item in result.baselines],
    }


def _evaluation_payload(result: LocalEvaluationResult) -> dict[str, object]:
    payload = result.to_dict()
    return {
        **payload,
        **_receipt_header("m8-local-evaluation.json"),
        "milestone": "M8",
    }


def _m4_evaluation_payload(result: LocalEvaluationResult) -> dict[str, object]:
    return {
        **_receipt_header("m8-m4-local-evaluation.json"),
        **result.m4_evaluation.to_receipt(),
        "milestone": "M8",
    }


def _performance_evaluation_payload(result: LocalEvaluationResult) -> dict[str, object]:
    return {
        **_receipt_header("m8-performance-local-evaluation.json"),
        **result.performance_measurement.to_receipt(),
        "milestone": "M8",
    }


def _canary_payload(canary: LocalCanaryResult) -> dict[str, object]:
    return {**canary.to_dict(), **_receipt_header("m8-local-canary.json"), "milestone": "M8"}


def _rollback_payload(rollback: RollbackRehearsalResult) -> dict[str, object]:
    return {**rollback.to_dict(), **_receipt_header("m8-rollback-rehearsal.json"), "milestone": "M8"}


def _release_payload(
    result: LocalEvaluationResult,
    canary: LocalCanaryResult,
    rollback: RollbackRehearsalResult,
    artifacts: Sequence[ReceiptArtifact],
) -> dict[str, object]:
    local_components_pass = result.passed and canary.state is GateState.PASS and rollback.state is GateState.PASS
    release_status = "BLOCKED" if local_components_pass else "FAIL"
    return {
        **_receipt_header("m8-release-report.json"),
        "release_status": release_status,
        "promotion_status": "M9_EVIDENCE_REQUIRED" if local_components_pass else "LOCAL_COMPONENTS_FAILED",
        "local_component_status": "PASS" if local_components_pass else "FAIL",
        "evidence_scope": SYNTHETIC_LOCAL,
        "live_attested": False,
        "artifact_index": [artifact.to_dict() for artifact in artifacts],
        "local_gate_matrix": [gate.to_dict() for gate in result.gates]
        + [
            {
                "gate_id": "gate:route-canary",
                "state": canary.state.value,
                "numerator": canary.route_match_count + canary.shadow_match_count,
                "denominator": canary.route_check_count + canary.shadow_check_count,
                "threshold": "35-route-and-50-simulated-shadow-match",
                "value": "local-synthetic-only",
                "failed_case_ids": [],
                "measurement_scope": SYNTHETIC_LOCAL,
            },
            {
                "gate_id": "gate:rollback-rehearsal",
                "state": rollback.state.value,
                "numerator": 1 if rollback.exact_equality and rollback.candidate_applied else 0,
                "denominator": 1,
                "threshold": "candidate-applied-exact-b3-and-fresh-canary",
                "value": "local-in-memory-only",
                "failed_case_ids": [],
                "measurement_scope": LOCAL_ROLLBACK_REHEARSAL,
            },
        ],
        "deferred_live_gates": [
            {"deferred_gate": "semantic-quality", "missing_evidence": "authorized-blinded-semantic-comparison"},
            {"deferred_gate": "graph-performance", "missing_evidence": "provider-task-performance-and-root-context"},
            {"deferred_gate": "memory-context", "missing_evidence": "production-scale-m4-recall-and-context-quality"},
            {"deferred_gate": "provider-telemetry", "missing_evidence": "authenticated-live-route-observations"},
            {"deferred_gate": "route-shadows", "missing_evidence": "50-real-read-only-shadows"},
            {"deferred_gate": "task-soak", "missing_evidence": "personal-canary-and-soak"},
            {"deferred_gate": "global-rollback", "missing_evidence": "approved-global-before-state"},
        ],
        "unresolved_risks": [
            "synthetic-local component evidence is not semantic quality evidence",
            "simulated shadows are not live traffic",
            "local rollback restores only in-memory control state",
        ],
        "rollback_limitation": "no global target was read or changed",
        "boundary_assertions": {
            "global_mutation": False,
            "old_obsidian_vault_access": False,
            "ai_memory_access": False,
            "provider_network_call": False,
            "action_executor": False,
            "global_activation": "disabled",
        },
    }


def _verification_payload(
    result: LocalEvaluationResult, release_payload: Mapping[str, object], artifacts: Sequence[ReceiptArtifact]
) -> dict[str, object]:
    return {
        **_receipt_header("m8-verification.json"),
        "status": "PASS",
        "release_status": release_payload["release_status"],
        "evidence_scope": SYNTHETIC_LOCAL,
        "live_attested": False,
        "source_binding_digest": result.candidate.binding.binding_digest,
        "artifact_index": [artifact.to_dict() for artifact in artifacts],
        "verification_scope": "typed-local-artifact-integrity",
        "reason_codes": ["M9_EVIDENCE_REQUIRED"],
    }


def _build_receipt_payloads(
    result: LocalEvaluationResult, canary: LocalCanaryResult, rollback: RollbackRehearsalResult
) -> dict[str, dict[str, object]]:
    baseline = _baseline_payload(result)
    evaluation = _evaluation_payload(result)
    m4_evaluation = _m4_evaluation_payload(result)
    performance_evaluation = _performance_evaluation_payload(result)
    canary_payload = _canary_payload(canary)
    rollback_payload = _rollback_payload(rollback)
    primary = tuple(
        _receipt_artifact(name, payload)
        for name, payload in (
            ("m8-baseline-receipts.json", baseline),
            ("m8-local-evaluation.json", evaluation),
            ("m8-m4-local-evaluation.json", m4_evaluation),
            ("m8-performance-local-evaluation.json", performance_evaluation),
            ("m8-local-canary.json", canary_payload),
            ("m8-rollback-rehearsal.json", rollback_payload),
        )
    )
    release = _release_payload(result, canary, rollback, primary)
    release_artifact = _receipt_artifact("m8-release-report.json", release)
    verification = _verification_payload(result, release, (*primary, release_artifact))
    return {
        "m8-baseline-receipts.json": baseline,
        "m8-local-evaluation.json": evaluation,
        "m8-m4-local-evaluation.json": m4_evaluation,
        "m8-performance-local-evaluation.json": performance_evaluation,
        "m8-local-canary.json": canary_payload,
        "m8-rollback-rehearsal.json": rollback_payload,
        "m8-release-report.json": release,
        "m8-verification.json": verification,
    }


@dataclass(frozen=True)
class M8EvidenceVerification:
    """Result of recomputing every artifact source binding without writing files."""

    state: GateState
    artifact_count: int
    release_status: str
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self) is not M8EvidenceVerification or self.state not in {GateState.PASS, GateState.FAIL}:
            raise SemanticValidationError("M8 evidence verification is invalid")
        _require_int(self.artifact_count, "verification artifact count", minimum=0, maximum=len(_RECEIPT_NAMES))
        if self.release_status not in {"FAIL", "BLOCKED", "UNKNOWN"}:
            raise SemanticValidationError("verification release status is invalid")
        object.__setattr__(
            self,
            "reason_codes",
            _freeze_identifiers(
                self.reason_codes, "verification reasons", re.compile(r"^[A-Z][A-Z0-9_]{2,95}$"), maximum=16
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "artifact_count": self.artifact_count,
            "release_status": self.release_status,
            "reason_codes": list(self.reason_codes),
        }


def verify_m8_local_evidence() -> M8EvidenceVerification:
    """Recompute source-bound payloads and reject forged or stale receipt trees."""

    try:
        result = run_local_evaluation()
        canary = run_local_canary()
        rollback = rehearse_local_rollback(
            create_candidate_control_state(result.candidate),
            b3_snapshot=result.b3_rollback_snapshot,
        )
        expected = _build_receipt_payloads(result, canary, rollback)
        mismatches = []
        for name, expected_payload in expected.items():
            actual_payload = load_serialized_receipt(name)
            if actual_payload != expected_payload:
                mismatches.append(name)
        release_status = expected["m8-release-report.json"]["release_status"]
        if mismatches:
            return M8EvidenceVerification(
                state=GateState.FAIL,
                artifact_count=len(expected) - len(mismatches),
                release_status=release_status,
                reason_codes=("ARTIFACT_BINDING_MISMATCH",),
            )
        return M8EvidenceVerification(
            state=GateState.PASS,
            artifact_count=len(expected),
            release_status=release_status,
            reason_codes=("M9_EVIDENCE_REQUIRED",),
        )
    except Exception:
        return M8EvidenceVerification(
            state=GateState.FAIL,
            artifact_count=0,
            release_status="UNKNOWN",
            reason_codes=("ARTIFACT_VERIFICATION_FAILED",),
        )


def generate_m8_local_evidence() -> dict[str, ReceiptArtifact]:
    """Generate eight typed, CAS-protected M8 receipts from local component evidence."""

    result = run_local_evaluation()
    canary = run_local_canary()
    rollback = rehearse_local_rollback(
        create_candidate_control_state(result.candidate),
        b3_snapshot=result.b3_rollback_snapshot,
    )
    payloads = _build_receipt_payloads(result, canary, rollback)
    artifacts = {name: _write_typed_receipt(name, payload) for name, payload in payloads.items()}
    verification = verify_m8_local_evidence()
    if verification.state is not GateState.PASS:
        raise SemanticValidationError("generated M8 receipts failed source-binding verification")
    return {
        "baseline": artifacts["m8-baseline-receipts.json"],
        "evaluation": artifacts["m8-local-evaluation.json"],
        "m4": artifacts["m8-m4-local-evaluation.json"],
        "performance": artifacts["m8-performance-local-evaluation.json"],
        "canary": artifacts["m8-local-canary.json"],
        "rollback": artifacts["m8-rollback-rehearsal.json"],
        "release": artifacts["m8-release-report.json"],
        "verification": artifacts["m8-verification.json"],
    }
