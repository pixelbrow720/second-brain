# M8 Integrated Evaluation and Local Canary

Status: in progress on 2026-07-23. The project-local component harness is
verified, but the release decision is intentionally `BLOCKED` pending the M9
evidence listed below. M8 is not complete and no global activation is implied.

## Local Scope

M8 evaluates only the checked-in 70-case synthetic corpus: 49 frozen cases and
21 rotating cases. `fixtures/m8/execution-plan-v1.json` supplies M5 features,
capability requests, profiles, and graph nodes separately from each case's
grader expectation. The executor never reads `case.expected`; that data is used
only by deterministic grading after execution.

For every candidate and baseline case, the local runner:

- issues an M5 `AdmissionDecision` through its exact in-memory `AuditTrail`;
- resolves `cap:local-helper` through M5 when the fixed plan requires it,
  without launching a capability or permission broker;
- creates the complete M6 intent, serialization, synthetic observation,
  reconciliation, and receipt chain for every non-direct route;
- runs a case-bound M7 manifest for GRAPH and DEEP workflow modes, reconstructs
  the observed M6 route receipt, and checks its digest against the M7 node
  receipt; and
- records only bounded digests, identifiers, counts, and allowlisted status
  fields. It never stores prompts, source bodies, telemetry blobs, secrets,
  headers, cookies, credentials, or chain-of-thought.

The runs are distinct and bound to the same corpus, execution-plan digest,
registry snapshot, code digest, seed, and permission policy:

- B0 uses root `tera-max` without M7 graph routing.
- B1 runs the actual M7 graph with `serial_fallback=True`. A separate controlled
  fixture pairs the canonical M7 graph's parallel and serial schedules three
  times with `time.monotonic`; it persists only aggregate outcomes and a
  synthetic context proxy (1,440 to 385 tokens, 73%), never raw timings or a
  provider-performance claim.
- B2 performs zero memory retrievals. A separate redacted M4 fixture executes
  retrieval and context compilation for all ten fixed memory cases; it does not
  claim production-scale recall or context quality.
- B3 is a fresh local last-known-good workflow run, not a copied candidate. Its
  typed rollback control snapshot binds the B3 run digest, common run binding,
  baseline outcome digest, and canonically ordered B3 baseline receipt digest.
  A candidate run cannot be relabeled as B3: every outcome must carry the B3
  execution mode before a snapshot can be minted.

## Local Evidence Integrity

`second_brain.evaluation` exposes typed, closed builders for all eight artifacts:

- `artifacts/m8-baseline-receipts.json`
- `artifacts/m8-local-evaluation.json`
- `artifacts/m8-m4-local-evaluation.json`
- `artifacts/m8-performance-local-evaluation.json`
- `artifacts/m8-local-canary.json`
- `artifacts/m8-rollback-rehearsal.json`
- `artifacts/m8-release-report.json`
- `artifacts/m8-verification.json`

Each artifact is content-hashed after exact schema validation. Existing targets
are replaced only with a compare-and-swap check under a project-local lock. The
only migration exceptions are a fixed legacy file digest and explicit prior
project-generated output digests; an unknown or user-modified target, including
a structurally valid self-hashed typed receipt, fails visibly without being
overwritten. The verification receipt recomputes all component and artifact
bindings, so a forged release payload cannot become valid evidence.

Every baseline binding additionally content-binds the evaluator, M5/M6/M7,
the M4 evidence runner plus its retrieval/storage/contract/clock seams, and
the controlled M8 performance runner. Receipt values have fixed shape and
bounded depth, collection size, integer magnitude, and string length before
they are hashed or written. The blinded packet contains only opaque A/B labels,
case-reference digests, rubric IDs, and evidence digests; its candidate-label
assignment remains a digest-only commitment and review status is `NOT_RUN`.

The local release report is deliberately `BLOCKED` with promotion status
`M9_EVIDENCE_REQUIRED`. The CLI returns a nonzero exit code for that state;
`PASS` is not printed unconditionally.

## Canary and Rollback

The canary performs 35 M6 synthetic route checks and 50 simulated
shadow-read-only checks. It has zero live requests, global targets, material
actions, and side effects. M6's `side_effect_allowed()` remains false for every
path.

The rollback rehearsal owns an in-memory state holder only. It derives the
candidate control state from the evaluated candidate run, applies it, restores
the typed B3 snapshot, checks exact B3 digest equality, and runs a fresh
synthetic canary bound to the restored control-state and B3 snapshot digests.
It does not read or change a global configuration target.

## Verified Local Gates

The local component harness verifies the dataset/plan binding, admission
macro-F1, capability precision/recall, M5/M6/M7 execution, route integrity,
B1 serial fallback, B2 zero retrieval, redaction/authority boundaries, actual
redacted M4 retrieval/provenance/context evidence for the ten fixed memory
cases, a controlled three-sample M7 scheduling/context-proxy fixture, offline
canary, and candidate-to-B3 rollback transition.

The following are explicitly `DEFERRED_TO_M9`, not treated as a passing local
claim:

- blinded semantic lower-confidence-bound comparison;
- provider-task graph performance and root-context reduction beyond the local
  controlled fixture;
- production-scale M4 recall, provenance, and context quality beyond the ten
  fixed local cases;
- authenticated provider telemetry, real shadow traffic, task soak, and global
  rollback evidence.

M9 has since completed an initial 50-request real `PUBLIC` read-only shadow
with zero observed tool activity or mutation. Its Codex CLI event stream did
not provide an authenticated route observation, so that result remains
`MISSING_TELEMETRY` and does not clear this deferred gate. See
`artifacts/m9-initial-shadow-report.json` and
`docs/21-M9-EXACT-GLOBAL-APPROVAL-AND-ROLLOUT.md`.

`artifacts/m8-m9-readiness-v2.json` is a separate, source-bound post-M9
checkpoint. It verifies the eight M8 receipts and the completed 50/50 M9
transport shadow together, so the old local release report is not misread as
claiming that real shadows are absent. The readiness checkpoint still records
authenticated telemetry and every later live gate as `BLOCKED`; it does not
alter the local-only M8 receipt or promote either milestone.
`artifacts/m8-m9-readiness.json` remains immutable historical evidence for the
earlier source-bound M8 receipt tree.

`second_brain.m9_external_evidence` now provides a project-local, fail-closed
intake boundary for a later detached-proof route-attestation batch and a later
blinded-review capture. It has no default verifier, provider client, artifact
writer, or promotion authority. A result is accepted only through a separately
trusted external verifier bound to a pinned policy and proof; a self-hashed JSON
payload is not evidence. The review receipt intentionally retains neither the
candidate-label mapping nor a semantic pass decision, so it remains
`BLOCKED_AUTHORIZED_ADJUDICATION_REQUIRED`. This code does not upgrade the
current M9 shadow: its original sessions have no retained authenticated route
correlations. The project-local OpenSSH adapter and immutable redacted plan
preparation are documented in `docs/22-M8-M9-EXTERNAL-EVIDENCE-RUNBOOK.md`;
they create no evidence for the current checkpoint by themselves.

## Remaining-Gate Locality Audit

The current checkpoint has no further safe synthetic substitute for its blocked
gates. The eight source-bound M8 receipts already cover the deterministic local
component claims, while the M9 initial-shadow receipt covers only transport
behavior and explicitly records missing authenticated route telemetry.

- Semantic non-inferiority needs fresh provider outputs, independent blinded
  reviewers, and an authorized holder of the hidden label map; local code must
  not invent any of those inputs or an adjudication result.
- Provider-task performance/root context needs real provider timing and token
  observations. The controlled monotonic fixture is intentionally not a proxy
  for a provider claim.
- Production-scale M4 needs an explicitly authorized corpus and quality review;
  the repository must not select or access an unnamed production corpus.
- Authenticated routing needs an independently controlled trust anchor and a
  new correlation-bound external batch. The existing local plan/preflight and
  proof verifier cover the safe intake boundary, not the upstream observation.
- Personal canary/soak needs a new opt-in approval, real traffic, and elapsed
  time. A local replay cannot prove either condition.
- Global rollback now has a fresh-packet and approval-gated project path, but
  its execution/readback remains a user-level mutation and cannot be rehearsed
  against the applied global state without that separate approval.

Accordingly, no new local receipt may change a blocked gate to `PASS` until
these inputs exist. The next work is evidence collection under
`docs/22-M8-M9-EXTERNAL-EVIDENCE-RUNBOOK.md`, not another synthetic evaluator.

## Requirement Checkpoint

This checkpoint exercises project-local parts of FR-18 and FR-19 and supports
local verification for NFR-01, NFR-02, NFR-06, NFR-07, NFR-09, NFR-11, NFR-12,
and AC-01, AC-02, AC-03, AC-08, AC-09, and AC-11. It does not claim the deferred
quality, live-route, real-canary, or global-rollout requirements as verified.

## Boundaries and Next Work

No old Obsidian vault, `/home/pixel/Data/PROJECT/ai-memory`, provider endpoint,
global Codex configuration, or global staging path is read or modified by M8.
The next session remains on M8 until the deferred external/live evidence passes.
M9's first exact approval is consumed; no opt-in, expanded rollout, default
activation, soak, or global rollback may begin without the required new report,
exact packet, and approval.
