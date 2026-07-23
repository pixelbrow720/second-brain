# M8 Final Security and Regression Review

Status: historical findings preserved below; see the 2026-07-23 remediation
addendum. M8 remains `BLOCKED` for M9 evidence, not for the local P1 findings.

This was an independent read-only review of the M8 evaluator, JSONL schema and
fixtures, generated local receipts, focused tests, M5/M6/M7 seams, and the
local-only boundary. No implementation, test, documentation, configuration,
global path, old Obsidian vault, `ai-memory`, provider, or external system was
modified by this review.

## Findings

### P1: The evaluator certifies self-generated expectations rather than observed component behavior

`_synthetic_outcome()` copies every expected lane, profile, capability, check,
and artifact directly into the candidate and every B0/B1/B2/B3 outcome
([`src/second_brain/evaluation.py:1616`](../src/second_brain/evaluation.py#L1616)).
`run_local_evaluation()` then creates all five runs from that helper
([`src/second_brain/evaluation.py:1683`](../src/second_brain/evaluation.py#L1683)).
It consequently gets score 4 and a zero quality delta even if M5, M4, M6, or
M7 behavior regresses; no actual admission decision/audit, retrieval result,
graph receipt, or per-case route receipt is evaluated.

The remaining metric gates compound this problem: admission, capability,
memory/context, and security report `100` when every self-minted grade passes,
rather than calculating their documented numerators and denominators
([`src/second_brain/evaluation.py:1663`](../src/second_brain/evaluation.py#L1663),
[`src/second_brain/evaluation.py:1712`](../src/second_brain/evaluation.py#L1712)).
B1's claimed 30% speedup/40% context reduction is constructed from constant
values, not a serial M7 run of the same graph
([`src/second_brain/evaluation.py:1619`](../src/second_brain/evaluation.py#L1619),
[`src/second_brain/evaluation.py:1694`](../src/second_brain/evaluation.py#L1694)).
B2 also leaves `retrieval_count` enabled for every memory case regardless of
mode ([`src/second_brain/evaluation.py:1638`](../src/second_brain/evaluation.py#L1638)),
so it is not a no-memory baseline.

The generated release report nevertheless marks all these threshold gates PASS
and declares `LOCAL_READY` ([`artifacts/m8-release-report.json:81`](m8-release-report.json#L81),
[`artifacts/m8-release-report.json:154`](m8-release-report.json#L154)). This
violates the requirement that metrics derive from real per-case fixture evidence
and that unavailable local gates be `BLOCKED`, rather than a green synthetic
default.

Remediation: introduce fixed deterministic component runners for every claimed
local gate. Bind each outcome to actual M5 admission/audit evidence, M4
retrieval/context result, M6 receipt/reconciliation, and M7 proposal-only run
where relevant. Calculate admission F1, capability precision/recall, memory
recall/provenance, security failures, B1 serial comparison, and B2 no-memory
behavior from those observed results and their specific case subsets. If a
metric cannot be exercised locally, emit `BLOCKED`/`DEFERRED_TO_M9` and exclude
it from `LOCAL_READY` rather than manufacturing a pass.

### P1: Per-case route and authority assertions can be caller-minted

`CaseOutcome` accepts an unrestricted status string such as `MATCH`, and
`grade_case()` accepts that string without a M6 `RouteReceipt`, reconciliation
digest, or route intent binding ([`src/second_brain/evaluation.py:942`](../src/second_brain/evaluation.py#L942)).
The synthetic outcome helper supplies `MATCH` directly
([`src/second_brain/evaluation.py:1634`](../src/second_brain/evaluation.py#L1634)).
A read-only probe confirmed that a GRAPH case with this unbound status receives
`PASS` and score 4.

This breaks the M6/M7 evidence seam: a route mismatch could be hidden by a
caller-created `CaseOutcome` even though the separate canary remains
synthetic-local and denial-only. The current boolean proposal/action fields are
also self-reported, not linked to an M7 `GraphRunReceipt` or an exact M5 policy
record.

Remediation: require a strict, exact base-type M6 receipt/reference for every
non-DIRECT route assertion; recompute reconciliation and reject `None`, forged,
subclassed, missing, ambiguous, and mismatched evidence. Require M5-issued
admission/audit and M7 run-receipt references for graph/deep cases. Preserve
M6's unconditional `side_effect_allowed() == false`; no new receipt may be
treated as action authority.

### P1: Receipt hashes do not establish receipt integrity, and the CLI reports PASS unconditionally

`serialize_receipt()` accepts any mapping that uses an allowlisted vocabulary,
but it does not require a per-artifact shape, required fields, source binding,
metric semantics, or a result produced by the evaluator
([`src/second_brain/evaluation.py:2092`](../src/second_brain/evaluation.py#L2092),
[`src/second_brain/evaluation.py:2128`](../src/second_brain/evaluation.py#L2128)).
`load_serialized_receipt()` performs the same generic vocabulary check and only
verifies the self-authored hash ([`src/second_brain/evaluation.py:2161`](../src/second_brain/evaluation.py#L2161)).
Consequently, another local caller can write a safe-looking, self-hashed
`LOCAL_READY` release payload with fabricated metrics. The writer also uses
`os.replace()` without an expected-old-digest/ownership check
([`src/second_brain/evaluation.py:2151`](../src/second_brain/evaluation.py#L2151)),
so it can overwrite an existing user-edited M8 artifact.

The runner compounds the false-success path by printing `{"status":"PASS"}`
and returning zero regardless of the generated release result
([`scripts/run_m8_evaluation.py:12`](../scripts/run_m8_evaluation.py#L12)).

Remediation: replace the generic public serializer with typed, per-receipt
builders/parsers that validate exact closed shapes, canonical component/baseline
bindings, gate-state semantics, and artifact-index hashes. Make artifact writes
compare-and-swap against an expected owned digest (or fail on an existing
foreign/user-modified target). Have the CLI load and validate the release
receipt, print its actual state, and exit nonzero for any fail or block.

### P1: The rollback rehearsal is a tautology, not a verified candidate-to-B3 restoration

`rehearse_local_rollback()` parses the candidate but never applies a transition
from it. It simply clones B3 and compares that clone to B3
([`src/second_brain/evaluation.py:2006`](../src/second_brain/evaluation.py#L2006),
[`src/second_brain/evaluation.py:2013`](../src/second_brain/evaluation.py#L2013)).
The result therefore passes for any valid candidate state; it neither binds a
known candidate snapshot nor executes a post-restore synthetic canary. The
release report counts this tautology as a passing rollback gate
([`src/second_brain/evaluation.py:2220`](../src/second_brain/evaluation.py#L2220)).

The scope is correctly labelled in-memory/local and no global target is touched,
but the current PASS cannot support even the limited local rollback claim.

Remediation: model an owned local control-state holder with an explicit
candidate snapshot, expected candidate digest, restore transition, exact
post-restore B3 equality, and a fresh contained post-restore M6 synthetic
canary. Bind the rehearsal to existing M4 rebuild/M6 rollback evidence by digest
where it only reuses prior tests. Until that happens, report rollback as
`BLOCKED` or `PLAN_ONLY`, not PASS.

### P2: Evaluator and human-review bindings remain incomplete

The run binding snapshots schema/registry/graph/profile assets, but it does not
content-bind `src/second_brain/evaluation.py`, the fixture executor, M5/M6/M7
modules, or a rubric definition; it relies on mutable constant version strings
([`src/second_brain/evaluation.py:1065`](../src/second_brain/evaluation.py#L1065)).
A changed grader can therefore remain nominally comparable to an old baseline
without a version/digest change.

The "blinded" packet also serializes fields named `candidate_score` and
`baseline_score` ([`src/second_brain/evaluation.py:1471`](../src/second_brain/evaluation.py#L1471)).
It remains `NOT_RUN`, so it is not currently used to green a gate, but it is not
a suitable opaque A/B packet for a future human review.

Remediation: bind immutable code/rubric/runner content digests into every
baseline and use opaque A/B labels with a separately committed, non-public map.
Keep human review deferred until a real authorized review record exists.

### P2: Receipt containment is not quantitatively bounded

The receipt allowlist limits key names and strings, but recursive lists/dicts
and integers have no depth, item-count, or numeric-size ceiling
([`src/second_brain/evaluation.py:2092`](../src/second_brain/evaluation.py#L2092)).
This permits oversized safe-vocabulary payloads and recursive-resource pressure,
contrary to bounded telemetry requirements. Add depth, collection, byte, and
integer limits before hashing or serializing.

## Verified Boundaries

- The JSONL schema is strict, data classes reject unknown fields, duplicate JSON
  keys are rejected by the strict loader, and the checked-in corpus enforces the
  required 49/21 frozen/rotating split.
- The M6 offline canary is explicitly labelled `synthetic-local`; the 50 shadow
  checks are labelled `simulated-shadow-read-only`; receipts retain
  `live_attested: false` and zero live request/global-target counts.
- Static inspection found no provider/network client, shell/subprocess call,
  home/global-Codex path, old-vault access, or `ai-memory` access in the M8
  evaluator and runner. The generated receipts likewise contain no prompt,
  cookie, credential, traceback, or URL marker.
- M6's current `side_effect_allowed()` remains false in the tested M8 paths,
  and M8 does not mutate `dist/global/m6` during its integration scenario.

## Review Evidence

- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_evaluation_m8 -q`
  passed: 7 tests. The suite does not cover the material false-pass, forged
  receipt, actual-component binding, or rollback-transition cases above.
- Read-only validation loaded all five generated receipts and verified their
  current self-hashes. This confirms format/digest consistency only; it does not
  remedy the source-binding issue.
- A read-only public-API probe produced a PASS/score-4 GRAPH grade from an
  unbound `route_status="MATCH"`, and constructed a `MetricGate(PASS, 0/1)`
  successfully.

## Acceptance Decision

M8 has **four open P1 blockers**. Do not mark it complete locally, update the
status to M9, or treat `artifacts/m8-release-report.json` as `LOCAL_READY` until
they are remediated and independently re-reviewed. The live/M9 gates remain
properly deferred; the blockers concern local evidence integrity and do not
authorize any global action.

## Remediation Addendum -- 2026-07-23

The historical findings above described the pre-remediation evaluator and are
retained for provenance. An independent read-only re-review of the current
project-local implementation found no remaining P1 in this remediation scope.

- The closed fixture executor now obtains actual M5 admission/audit, M6 route,
  and M7 graph evidence without reading `case.expected` during execution. B1
  uses an actual serial fallback, B2 performs zero retrievals, and semantic
  claims remain deferred to M9.
- The current M8 checkpoint additionally exercises all ten fixed memory cases
  through redacted M4 retrieval/provenance/context evidence and records a
  controlled three-sample, monotonic M7 schedule/context proxy. Those local
  fixtures do not establish provider-task performance, production-scale M4
  quality, or live evidence.
- Closed per-artifact receipt schemas, source recomputation, bounded redaction,
  and CAS ownership reject an unknown fully typed/self-hashed receipt without
  overwriting it. The CLI reports the actual `BLOCKED` release state and exits
  nonzero.
- Rollback applies a candidate control state derived from the evaluated
  candidate run, restores a typed B3 snapshot bound to the B3 run, common
  binding, outcome digest, and canonical B3 baseline receipt, then binds the
  post-restore synthetic canary to that restored state and snapshot.
- A candidate cannot be relabeled as B3: every `EvaluationRun` outcome must
  carry the execution mode required by its baseline. The receipt parser also
  rejects a B3 snapshot whose receipt digest is not the canonical B3 digest.
- The historical P2 binding/containment findings are now closed locally:
  baseline bindings content-bind the evaluator, M5/M6/M7, M4 evidence and its
  retrieval/storage/contract/clock seams, and the controlled performance
  runner. Receipt traversal enforces depth, collection, integer, and string
  bounds before hashing; the blinded packet retains opaque A/B labels and a
  digest-only candidate assignment rather than candidate/baseline scores.

Verification completed after the remediation with focused M8 tests, `make
check`, `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3
scripts/check_clean_room.py`, and a second independent read-only review.
`artifacts/m8-verification.json` verifies all eight local receipts with status
`PASS`; `artifacts/m8-release-report.json` remains `BLOCKED` with
`M9_EVIDENCE_REQUIRED`.

### Current Acceptance Decision

The local P1 findings are resolved, but M8 is still `in_progress`. Do not mark
it complete, promote it, or advance to M9 until authorized evidence exists for
the blinded semantic comparison, provider-task performance/context measurement,
production-scale M4 evidence, authenticated live route/canary data, and
approved global rollback before-state. M8 itself accessed no global target, old
Obsidian vault, `ai-memory`, or provider endpoint; the separately approved M9
operation remains shadow-only and does not change this acceptance decision.
