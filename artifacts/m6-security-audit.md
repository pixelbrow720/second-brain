# M6 Security Audit

Status: pre-implementation, read-only design audit for the `security-audit`
graph node. This is a project-local security contract, not evidence that a live
9router route has been attested or promoted.

## Scope and Evidence Boundary

M6 may implement an authoritative seven-profile registry, deterministic
project-local TOML staging, route intent/serialization/observation comparison,
redacted receipts, synthetic canary, local side-effect gate, and local rollback
rehearsal. It must not execute a real model call, mutate a global configuration,
or treat host-local configuration as an implementation input.

Read-only discovery reported Codex `0.144.6`, 9router `0.5.40`, and public
local `/v1/models` identifiers `cx/gpt-5.6-terra`, `cx/gpt-5.6-sol`,
`cx/gpt-5.6-luna`, and `cx/gpt-5.5`. This is reproducible identity evidence
only. It does not prove a `model_reasoning_effort` field name or value, proxy
propagation, provider normalization, or telemetry authenticity. In particular,
the shared Tera raw slug cannot establish whether `high`, `max`, and `xhigh`
are distinct. Until a no-side-effect fixture proves a mapping, it remains
unverified and cannot be promoted, called a successful live canary, or authorize
a material action.

All deployment-shaped files must remain below `dist/global/m6/` in this
repository. M6 must not read or write `~/.codex`, `~/.9router`, the old Obsidian
vault, `/home/pixel/Data/PROJECT/ai-memory`, provider configuration, or any
network endpoint. A synthetic observation is test input, never proof of live
router behavior.

## Fail-Closed Invariants

1. **One immutable registry snapshot.** The M6 resolver accepts exactly the
   seven approved aliases, with `tera-max` as the only default root. It loads
   one versioned v2 registry and snapshots it before creating intent. The M0 v1
   placeholder registry may remain a historical fixture, but must never be a
   fallback authority for an M6 route. Unknown aliases, duplicate aliases,
   mutable mappings, absent adapter/version fields, or unvalidated discovery
   evidence fail before serialization.
2. **Intent, wire fields, and comparison stay distinct.** A route intent binds
   route/task/node opaque IDs, alias, intended effort, registry digest/version,
   adapter version, and normalization-rules version before a request exists.
   Serialization records only the allowlisted model and effort field actually
   emitted. Comparison uses the frozen snapshot, not a later registry reload or
   a model-name substring. The raw model may legitimately be shared by several
   profiles, but `high`, `max`, and `xhigh` must remain distinct exact values
   through serializer and normalizer.
3. **Generated TOML is data, not a configuration escape.** Generated filenames
   come only from approved aliases; generated values have strict bounded syntax
   and are emitted by a TOML encoder/quoting routine, then parsed back and
   compared to the frozen registry. Newlines, quotes/table syntax, NUL,
   traversal, unexpected keys, hand edits, stale extra files, and symlink or
   hard-link escapes fail validation. A manifest of deterministic bytes and
   digests detects drift. No generated file can activate an agent, endpoint,
   hook, plugin, or global configuration.
4. **Correlation is one-to-one and never time-based.** Generate a fresh opaque
   correlation/route ID per request. An observation is eligible only when its
   exact ID, adapter/version, required fields, and normalization-rules version
   match the immutable route snapshot. Timestamp proximity, model equality,
   router request ID, or a caller-supplied boolean cannot substitute for that
   correlation. A replay, foreign ID, stale version, or multiple eligible
   observations is not a match.
5. **Reconciliation is conservative.** `MATCH` is possible only for exactly
   one complete eligible observation with exact normalized model and effort.
   Zero observations are `MISSING_TELEMETRY`; duplicate eligible observations
   or a missing mandatory field are `AMBIGUOUS_TELEMETRY`; unsupported,
   unpinned, or unknown mapping/normalizer versions are
   `UNSUPPORTED_MAPPING`. Exact differences produce `MISMATCH_MODEL`,
   `MISMATCH_EFFORT`, or `MISMATCH_BOTH`. Malformed telemetry, timeout, parser
   exception, and unavailable evidence must default to a non-`MATCH` status.
6. **Receipts are allowlisted and redacted.** Public receipt data may contain
   controlled status/enum values, timestamps, registry/adapter/normalizer
   versions, safe profile aliases, a canonical routing-fields digest, and opaque
   or hashed correlation/task/node/router identifiers. It must not retain a
   request body, prompt, response body, headers, endpoint URL/query, credential,
   exception text, raw telemetry blob, arbitrary task/node text, or recursive
   object dump. All input mappings reject unknown fields and receipt serialization
   must be deterministic and non-aliasing.
7. **The side-effect gate is an enforcement boundary.** For `GRAPH` and `DEEP`,
   only a receipt bound to the exact route snapshot with status `MATCH` may pass
   the local gate. Every other status, a missing receipt, stale receipt, unknown
   enum, parser error, or gate error blocks commit/publish/install/external
   mutation. The gate must sit at the commit boundary rather than merely label
   output, and it must not weaken M5 permission, target, approval, or supply-chain
   controls. M6 models this decision only; it must not gain an executor.
8. **Retry cannot hide telemetry loss.** At most one retry using a pinned
   last-known-good mapping is permitted only for `MISMATCH_*` or
   `UNSUPPORTED_MAPPING`, and only before any side effect. `MISSING_TELEMETRY`,
   `AMBIGUOUS_TELEMETRY`, and all gate errors are not retry-to-success paths.
9. **Canary evidence is all-or-nothing and labeled synthetic.** A local canary
   creates fresh deterministic, non-sensitive, read-only request fixtures for
   all seven profiles. Five-repeat mode requires all 35 independently correlated
   reconciliations to be `MATCH`; a single mismatch, missing/ambiguous record,
   duplicated ID, exception, or omitted profile fails the aggregate result.
   It cannot contact 9router, accept a majority/last-result success, or produce
   a health/promotion receipt claiming live coverage.
10. **Rollback restores an exact local snapshot only.** The last-known-good
    receipt pins registry digest/version, adapter and normalization versions,
    generated TOML manifest/digests, and its managed project-local target.
    Rollback validates all of those before atomically restoring only managed
    staging paths, preserves unrelated/user files, blocks traversal/symlink
    targets and digest mismatches, then reruns the synthetic seven-profile
    canary. A mutable path in a receipt, a missing baseline, or a failed
    rehearsal leaves the candidate quarantined rather than overwriting files.

## Threat Cases and Required Handling

| Threat | Required fail-closed behavior |
| --- | --- |
| A discovery response or registry value tries TOML injection, changes an alias, or adds a hook/endpoint directive. | Treat it as untrusted data; schema/character bounds and parse-round-trip reject it before a file is staged. Discovery never creates authority by itself. |
| A registry changes between intent creation, serialization, telemetry arrival, and rollback. | Bind every stage to one canonical snapshot digest; any version/digest disagreement is unsupported or blocked. |
| Tera profiles share a raw slug and the implementation compares only model identity. | Compare exact serialized and observed effort after the pinned normalizer; `high -> xhigh` and `max -> xhigh` remain mismatches. |
| A proxy drops/replaces correlation headers, a prior record is replayed, or two records share an ID. | Do not correlate by time. Missing/foreign/replayed evidence is non-match; two eligible records are ambiguous even if values agree. |
| Router telemetry is partial, malformed, delayed, or from an unknown router/normalizer version. | Preserve no raw blob in the receipt and return a non-`MATCH` controlled status; never infer success from a response payload or UI label. |
| Prompt, token, Authorization header, endpoint query, secret marker, or arbitrary exception enters a receipt. | Build receipt dictionaries from an explicit allowlist plus hashes. Secret-marker tests must prove all such values are absent from registry fixtures, receipts, manifests, and error output. |
| A caller supplies `MATCH`, skips reconciliation, lowercases a status, or catches a gate exception. | Use closed enums and a receipt-bound gate with deny-by-default behavior. The caller cannot provide an allow flag or use a status string as authority. |
| A failed canary reports the last successful profile/repeat as overall success. | Aggregate exact expected profile and repeat cardinality, unique IDs, and all statuses; fail on any omission or unexpected observation. |
| A rollback receipt is tampered to point outside staging, overwrites a manual file, or restores only part of a mapping. | Validate a canonical manifest and repository-contained managed path before atomic restore; preserve foreign files and reject partial/mismatched snapshots. |
| Staging code follows `Path.home()`, a global config path, environment-supplied destination, or network client. | Reject non-repository destinations and keep all M6 operations pure/local; tests must prove no global path, global config, or network operation is reached. |

## Required Regression Contract

The implementation should add focused deterministic tests for each item below.
No test may require a real router, credential, host configuration, or live
telemetry.

1. **Registry and staging:** test the exact seven aliases/root; strict v2 schema
   and semantic validation; rejection of v1-placeholder fallback, unknown alias,
   unknown field, wrong adapter/normalizer version, bool-as-version, and mutable
   input aliasing. Assert model IDs may be shared only when distinct validated
   effort values survive to the generated TOML. Inject newline, quote, table,
   traversal, NUL, and secret-marker values into candidate fields; generation
   must fail without writes. Parse each generated TOML, compare its explicit
   `model` and `model_reasoning_effort` fields to the frozen mapping, detect
   manual drift and stale/extra generated files, and reject a symlink escape.
2. **Serialization and receipt redaction:** construct an intent, mutate the
   original registry/input mapping, and show serialized fields and digest remain
   unchanged. Reject an unknown effort or field name; show digest changes for an
   allowed routing-field change but never includes prompt/header/credential/error
   markers. Assert serialization accepts only strict IDs/timestamps/enums and
   receipts contain no raw task/node text, endpoint, body, telemetry blob, or
   secret marker.
3. **Reconciliation:** cover exact match; model-only, effort-only, and combined
   mismatch; zero observation; duplicate identical observation; duplicate
   conflicting observation; absent required field; wrong correlation; replayed
   correlation; stale adapter/version; unknown normalizer; malformed telemetry;
   and parser failure. The permanent regression must prove that requested
   `tera-high`/`high`, `tera-max`/`max`, and `sol-max`/`max` observed as `xhigh`
   are all detected, while legitimate xhigh profiles still match.
4. **Gate and retry:** parameterize every reconciliation status, a missing
   receipt, stale/digest-mismatched receipt, forged enum, and gate exception.
   `GRAPH`/`DEEP` side effects must be denied except for the exact current
   `MATCH`; M5 permission denial must still deny a matching route. Prove a single
   allowed mismatch/unsupported retry occurs before an action, and that missing,
   ambiguous, duplicate, or post-side-effect cases never retry or commit.
5. **Canary:** assert exactly seven unique profile aliases in one run and exactly
   35 unique correlations in five-repeat mode. Inject one non-xhigh-to-xhigh
   mismatch, one missing record, one duplicate, one foreign record, one exception,
   and one omitted profile; each fails the full canary. Verify deterministic
   request contents are non-sensitive/read-only, no network client is invoked,
   and the result is labeled simulated/unverified rather than live promotion.
6. **Rollback and local boundary:** snapshot a known-good staged manifest, stage
   a candidate, exercise rollback, require byte-identical managed restoration,
   then require a seven-profile synthetic canary. Reject a changed digest,
   truncated receipt, absent baseline, partial manifest, path traversal, symlink,
   foreign/user file collision, and failed post-rollback canary without any
   overwrite. Assert every resolved output stays below `dist/global/m6/` and no
   global config, home-directory, old-vault, ai-memory, or network path is read
   or written.

## Review Conclusion

M6 is safe to implement as a local deterministic attestation model only if the
above checks are enforcement contracts rather than report-only diagnostics. The
largest residual risk is confusing discovered model identity with verified
effort/telemetry behavior. Synthetic local evidence can prove registry and
fail-closed logic, but cannot authorize global deployment or claim that 9router
`0.5.40` currently preserves the requested effort. Any non-`MATCH`, unverified
discovery, or rollback rehearsal failure must remain visible and block material
use.
