# M8 Security Audit: Integrated Evaluation and Local Canary

Status: implementation-ready, project-local only. This is a read-only
adversarial design audit of the planned M8 evaluation, canary, rollback, and
release-evidence work. It does not authorize a provider request, capability
execution, durable authority mutation, global configuration change, or
promotion decision.

## Evidence Boundary

M8 may validate versioned synthetic fixtures, deterministic local graders,
bounded reports, an offline/simulated shadow-read-only canary, and a contained
rollback rehearsal. It may prove that the implementation does not fabricate a
local receipt or cross a project boundary. It cannot prove production quality,
live 9router telemetry, a 50-request live shadow, real provider latency/cost,
or global rollback readiness.

Every M8 receipt must state its evidence scope. Local fixtures and M6 route
observations remain `synthetic-local` with `live_attested: false`. A simulated
`MATCH`, a passing grader, or a local rollback rehearsal is diagnostic evidence
only. It cannot authorize a material action, override M5, weaken M6's
unconditional `side_effect_allowed()` denial, or satisfy the M9 approval gate.

## Fail-Closed Invariants

### Dataset and fixture provenance

1. Evaluation data must be strict, versioned JSONL parsed with duplicate-key and
   non-finite-value rejection. Each case needs a unique bounded ID, closed suite,
   risk, data class, rubric ID, and exact allowed input/expected fields. Unknown
   fields, duplicate cases, malformed Unicode, oversized values, or unsupported
   schema versions block evaluation before any grader starts.
2. Frozen regression data, rotating holdout data, rubrics, evaluator version,
   runner version, repository snapshot digest, registry digest, and seed must be
   separately content-addressed and bound into the run receipt. A result with a
   missing, changed, or mismatched digest is `BLOCKED_EVIDENCE`, not a pass.
3. Holdout cases must not be read by implementation/tuning code or mixed into the
   frozen corpus by a convenient flag. The evaluator receives a declared split;
   release reporting carries only split digest/counts and never holdout bodies.
   Any use of a holdout case for tuning invalidates that run rather than silently
   relabeling it as regression data.
4. Fixture text is data, not instruction. M8 must not execute a prompt, command,
   URL, path, capability ID, approval token, or report template supplied by a
   case. Workspace fixture references resolve only through a closed,
   repository-contained allowlist with canonical containment and symlink checks.
5. Cases must be `PUBLIC` synthetic data unless a separately approved data policy
   exists. `SECRET` is rejected; unknown data is rejected or classified at least
   `SENSITIVE` and excluded from the local evaluator. Secret/instruction-like
   scan failures block persistence and do not enter an error message or report.

### Comparable baselines and metric integrity

1. B0, B1, B2, and B3 each require an immutable baseline manifest binding exact
   case-set digest, repository/config/registry snapshot, seed, permissions,
   runner/grader version, evidence scope, and individual outcome digests. A
   baseline is not an aggregate number supplied by a caller.
2. Candidate and baseline runs must use the same declared case IDs, snapshot,
   permissions, seed/retry policy, grading version, and timeout budget. A
   comparison with a different input population, route mapping, or evaluator is
   `NOT_COMPARABLE` and blocks quality/performance claims.
3. Every scheduled case contributes exactly one terminal accounting record. A
   timeout, exception, route mismatch, cancellation, blocked permission, missing
   telemetry, or failed grader is visible as failure/blocked evidence; it may not
   be dropped, converted to zero cost, or replaced by a favorable rerun. Reruns
   require a predeclared deterministic policy and retain all attempt outcomes.
4. All aggregate metrics derive from allowlisted per-case counters. Counts,
   denominators, exclusions, quantile method, confidence method, weights, and
   rounding rules must be versioned and reproducible. Division by zero, missing
   token/cost/latency, insufficient sample size, non-finite values, or a missing
   critical case produces `NOT_MEASURED`/`BLOCKED`, never a green default.
5. Speed and context comparisons include only predeclared graph-eligible matched
   cases. B1 must execute the same logical graph serially; it cannot omit review,
   verification, retries, or input accounting. M8's synthetic timing may test
   accounting mechanics, but reports must not claim the 25% wall-clock or 35%
   context target without the required real controlled evidence.
6. A lower confidence bound, pairwise rate, or safety-critical gate may be
   reported only from stored individual outcomes and a fixed implementation of
   the stated statistic. Model self-rating, a hand-written aggregate, or a
   favorable subset cannot be used as a semantic-quality score.

### Grading, blinding, and human review

1. Deterministic schema/route/permission/file-diff checks run before semantic
   grading and are independently recorded. A semantic score never overrides a
   failed security, provenance, route, or permission assertion.
2. Blinding uses a run-owned random assignment committed before review. Candidate
   and B0 labels remain opaque to the semantic reviewer; unblinding occurs only
   after the immutable review record is written. Reviewer identity/version,
   rubric digest, case digest, and label-map digest are bound to the result.
3. GPT-5.5 or any model reviewer is advisory, not sole ground truth. High-risk
   failures, disagreements, and the required end-to-end sample need an explicit
   `HUMAN_REVIEW_REQUIRED` state until a real authorized review is attached. A
   synthetic test may cover this state transition but may not manufacture human
   approval.
4. Grader exceptions, arbitrary callback objects, raw model output, and free-form
   evaluator metadata are untrusted. They cannot become a pass, metric value, or
   serialized receipt. Preserve only a closed reason code and an opaque/digested
   artifact reference.

### Redaction and artifact handling

1. Public reports, baseline receipts, canary receipts, and release reports use
   fixed allowlists: IDs/digests, schema/version labels, lane/profile aliases,
   closed states/reasons, bounded counters, timestamps/durations, and safe
   artifact references. They never serialize case prompts, outputs, raw source,
   private path, endpoint/URL/query, header, cookie, credential, environment,
   traceback, or arbitrary `__dict__`.
2. Redaction/secret scanning happens before a value is accepted into metrics,
   compaction, or a report. A scan/redaction failure blocks the run. Hashing an
   unsafe body is not a redaction bypass when that body could still be retained
   in an event or exception string.
3. Oversized diagnostic data stays outside root/report context as a bounded
   artifact reference with byte count and SHA-256. Compaction summaries are
   generated from an allowlisted controlled form; raw fixtures or callback logs
   cannot be echoed back through the summary.
4. Reports must distinguish `PASS`, `FAIL`, `BLOCKED`, `NOT_MEASURED`, and
   `SYNTHETIC_ONLY`. Absence of a field, an empty suite, or a redacted value may
   not be rendered as a passing zero-risk result.

### M5, M6, and M7 boundary preservation

1. The evaluator is not an execution host. It does not call a capability,
   `PermissionBroker` action, store writer, shell, network client, plugin,
   connector, browser, or provider. M5 decisions remain policy data and cannot
   be minted from a case fixture or used as a material permission.
2. M7 graph fixtures remain synthetic proposal tests. `DIRECT` test cases must
   demonstrate no graph creation; graph/deep cases must retain M5-issued
   admission, M7 scope/ownership/cancellation constraints, and proposal-only
   result semantics. No evaluator shortcut may turn an M7 proposal into a commit.
3. Route evidence is created only via the existing M6 synthetic APIs and frozen
   registry snapshot. Missing, ambiguous, mismatched, unsupported, forged, or
   subclass-spoofed receipt evidence blocks the affected graph/deep outcome.
   Synthetic `MATCH` must stay proposal-only and `side_effect_allowed()` must
   remain false for every receipt object.
4. The local canary must be explicitly `offline` or `simulated-shadow-read-only`,
   use only non-sensitive synthetic inputs, and report zero live requests. It
   cannot label itself promoted traffic, a seven-profile live canary, or 50 live
   route shadows. Any future live canary remains M9/deployment-gated work.

### Rollback and local containment

1. A rollback rehearsal may operate only on a fixed project-local managed root
   already owned by the relevant milestone, such as M6's contained staging tree.
   It binds a verified last-known-good snapshot and candidate manifest digest
   before changing that managed tree, rejects foreign files/symlinks/hard links,
   and refuses to overwrite user-owned content.
2. Successful rollback requires exact restored manifest/snapshot digest and a
   fresh post-restore synthetic canary. A preflight, restore, or post-check
   failure leaves a visible blocked result; it cannot report rollback success
   from a cached receipt.
3. M8 must not claim backup/restore, credential revocation, plugin removal, or
   global adapter rollback beyond the local synthetic rehearsal actually run.
   It must not access `~/.codex`, old Obsidian data, `ai-memory`, another
   repository, provider state, or any global deployment target.
4. No output root may be caller-selected outside the repository. Relative paths
   are rechecked at use time for traversal/symlink escape; no destructive delete,
   broad cleanup, or environment-variable path expansion is an M8 fallback.

## Required Adversarial Regressions

The M8 implementation and final review should reject or visibly block all of
the following before claiming a local pass:

| Area | Required regression |
| --- | --- |
| Case ingestion | Duplicate JSON key/ID, unknown field, boolean/non-finite numeric metric, invalid split/data class, oversized prompt, secret/cookie/header marker, prompt-injection text, absolute/traversal/URI/symlink fixture reference, and a case attempting to name a command/capability/approval. |
| Evidence binding | Changed dataset/rubric/snapshot/registry/seed digest, swapped B0/B1 case set, caller-provided aggregate, duplicate per-case outcome, forged artifact hash, mutable mapping/subclass aliasing, and stale receipt reused for a new run. |
| Metric laundering | Omitted timeout/exception/mismatch, cherry-picked successful rerun, negative or non-finite counters, zero denominator, insufficient confidence sample, changed weight/quantile method, B1 with omitted review/context, and a synthetic duration presented as a live target pass. |
| Blinding/review | Candidate label exposed before review, label-map changed after review, model self-score used as sole quality evidence, unresolved human-review state rendered as pass, and deterministic security failure overridden by semantic score. |
| Redaction | Prompt/source/URL/query/header/cookie/credential/environment/traceback markers through case, result, exception, metric, compaction, canary, and release-report paths; assert absence from serialized payloads and digest inputs. |
| M5/M6/M7 | Caller-minted admission/permission, `DIRECT` graph attempt, M7 proposal treated as committed, every non-`MATCH` route status, `None`, hostile receipt subclass, and synthetic `MATCH`; all material actions remain denied and graph/deep results quarantine where required. |
| Canary | Fewer than seven aliases, missing correlation, duplicate/foreign telemetry, non-read-only flag, private fixture, claimed live request count, attempt to promote, and a local synthetic canary reported as promoted/live evidence. |
| Rollback/containment | Candidate-digest mismatch, altered snapshot, foreign file, symlink/hard-link escape, output root outside repository, dirty user file collision, post-restore canary failure, and a rollback attempt targeting home/global/old-vault/`ai-memory` paths. |

## Acceptance Blockers

Treat these as P1 blockers for M8 local acceptance:

1. a report can claim a metric, baseline comparison, human review, live canary,
   route attestation, or rollback success without immutable supporting evidence;
2. a failed/blocked case can disappear from a denominator or become a favorable
   rerun, zero, or omitted field;
3. a fixture, callback, receipt, or report can inject executable authority,
   expand scope, leak sensitive text, or bypass redaction;
4. synthetic M5/M6/M7 evidence can authorize a store/global/external action;
5. a local canary is presented as live/promoted traffic or enables a global
   rollout; or
6. any evaluation, artifact, or rollback path reaches global configuration,
   provider/network, old-vault data, `ai-memory`, or outside-repository state.

The residual M7 limitation remains relevant: same-process injected callbacks are
cooperative synthetic test code, not a hostile-code sandbox. M8 must not broaden
that callback model into an execution host. A future live evaluator or worker
requires separate process isolation, host-enforced termination, data-policy
approval, and the exact M9 global approval packet.
