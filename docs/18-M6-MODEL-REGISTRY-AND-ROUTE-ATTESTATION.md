# M6 Model Registry and Route Attestation

M6 is the project-local implementation of the versioned model registry and
9router route-attestation contract from [the roadmap](09-IMPLEMENTATION-ROADMAP.md).
It proves the local serialization, correlation, reconciliation, staging, and
rollback invariants with synthetic fixtures. It does not make a provider call,
attest live 9router behavior, execute a side effect, or change global Codex
configuration.

Status: complete and locally verified on 2026-07-23. The exact local receipt is
[`artifacts/m6-verification.json`](../artifacts/m6-verification.json).

## Registry and Staging Boundary

`config/model-profiles.json` is the one active V2 registry. It contains exactly
the seven approved aliases, keeps `tera-max` as the only root default, and binds
each `nine_router` mapping to explicit local identity evidence, a model slug,
`model_reasoning_effort` field/value, adapter version, client/router version,
and normalization-rule version. `high`, `max`, and `xhigh` remain distinct
values through resolution, serialization, and comparison.

The observed Codex/9router versions and model IDs are recorded only as
reproducible local identity evidence. The mapping evidence is explicitly
`synthetic-local`; it is not evidence that a live proxy propagates effort or
telemetry correctly. The historical M0 V1 schema and fixture remain available
for contract history, but the M6 resolver never treats them as routing
authority.

Generated review-only TOML lives only under `dist/global/m6/`. Each agent TOML
explicitly carries `model` and `model_reasoning_effort`; `root.toml` explicitly
declares `tera-max`. The generator parses its own TOML, digests a closed file
manifest, and rejects drift, foreign files, symlinks, hard links, traversal, or
any destination outside the managed project-local directory.

## Route Attestation Boundary

`second_brain.routing` exposes immutable `RouteIntent`, allowlisted
`SerializedRoute`, strict `TelemetryObservation`, exact `RouteReconciliation`,
and redacted `RouteReceipt` values. Intent is snapshot-bound before
serialization; telemetry requires the exact route and correlation identifiers,
rather than timestamp proximity. The closed reconciliation states are `MATCH`,
`MISMATCH_MODEL`, `MISMATCH_EFFORT`, `MISMATCH_BOTH`, `MISSING_TELEMETRY`,
`AMBIGUOUS_TELEMETRY`, and `UNSUPPORTED_MAPPING`.

The permanent historical fixture sends a non-xhigh profile with an observed
`xhigh` effort. It remains `MISMATCH_EFFORT` in every one of the five repeats.
Missing, incomplete, duplicate, foreign, or unrecognized telemetry never becomes
`MATCH`. Receipts and their hash inputs use a fixed allowlist and contain only
opaque identifiers or digests, closed statuses/reasons, versions, and timestamps;
prompt text, headers, credentials, endpoint URLs, cookies, and raw telemetry are
not retained.

## Canary, Gate, and Rollback

The local canary creates seven non-sensitive, read-only synthetic checks, and
the five-repeat mode requires all 35 independently correlated checks to match.
Its output is labelled `synthetic-local` with `live_attested: false`; it cannot
be promoted as live router evidence.

M6 deliberately has no trusted live-attestation receipt type. Consequently,
`side_effect_allowed()` is an unconditional deny boundary for every M6 receipt,
including a synthetic `MATCH`, for `GRAPH` and `DEEP`. This was hardened after
independent review found first a synthetic-MATCH authorization path and then a
subclass/dynamic-dispatch spoof path. The final adversarial review verifies that
ordinary, forged, and hostile subclass inputs all deny without inspecting
caller-controlled fields. A later milestone needs a separately authenticated
live receipt contract before any material action can be authorized.

`LastKnownGoodReceipt` and `LocalStagingSnapshot` pin the registry, adapter,
normalization version, manifest, and exact managed staging bytes. Local rollback
validates candidate digests and containment, restores only the managed staging
tree, then reruns the synthetic canary. Tampered receipts, unsafe destinations,
foreign files, and failed canaries leave the candidate untouched.

## Verification and Rollback

The focused 33-test M6/profile/schema suite covers exact aliases and effort
classes, V1/V2 isolation, route intent snapshots, all reconciliation outcomes,
redaction, the seven and 35-check canaries, staging containment/drift, rollback,
and the synthetic/subclass side-effect denial boundary. `make check`, the M0
asset check, graph validation, and `scripts/check_clean_room.py` pass. The final
independent `GPT-5.5 xhigh` re-review has zero open material findings; its report
is in [`artifacts/m6-final-remediation-security-review.md`](../artifacts/m6-final-remediation-security-review.md).

Requirement coverage is recorded for FR-04, FR-17, FR-18, FR-19, NFR-01,
NFR-09, NFR-10, NFR-11, AC-08, AC-11, ADR-003, and ADR-006. Rollback reverts
only M6-local code, registry/schema/fixtures, generated staging, tests,
artifacts, and documentation. It must not delete earlier authority state or
change any global configuration. The old Obsidian vault and `ai-memory` remain
unread and unmodified.
