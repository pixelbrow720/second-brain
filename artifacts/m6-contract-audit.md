# M6 Contract Audit: Model Registry and Route Attestation

Status: implementation-ready, subject to the M0 generated-asset ownership issue
described below. This is a project-local contract audit; it authorizes neither a
live route call nor a global configuration change.

## Contract Sources

- `docs/09-IMPLEMENTATION-ROADMAP.md` section 9 defines the M6 outputs and
  seven acceptance criteria.
- `docs/05-MODEL-ROUTING.md` sections 1, 2, 4, 5, 6, and 7 define the stable
  aliases, generated-agent boundary, three-stage attestation, fail-visible
  statuses, and canary/rollback behavior.
- `docs/08-EVALUATION-AND-TEST-PLAN.md` sections 5.5, 6, and 7 make the
  non-xhigh-to-xhigh regression, full canary, five-repeat mode, and rollback
  measurable requirements.
- `docs/07-SECURITY-AND-PRIVACY.md` sections 5.4, 9, and 11 require correlation,
  quarantine on mismatch, redacted telemetry, and no secret/prompt persistence.
- ADR-003 requires one explicit alias mapping and treats unavailable downstream
  telemetry as unverified. ADR-006 keeps all M6 artifacts project-local until a
  separate global approval packet.

## Discovered Version and Inventory Evidence

The following was supplied from reproducible, read-only local interfaces. No
global configuration directory was read.

| Evidence source | Observed value | What it proves | What it does not prove |
| --- | --- | --- | --- |
| `codex --version` | `codex-cli 0.144.6` | The client version to bind into a local adapter evidence record. | The HTTP effort field or its semantics. |
| `9router --version` | `0.5.40` | The router version to bind into a local adapter evidence record. | That any profile reaches a particular provider/effort. |
| `GET http://127.0.0.1:20128/v1/models` | `cx/gpt-5.6-terra`, `cx/gpt-5.6-sol`, `cx/gpt-5.6-luna`, `cx/gpt-5.5` (plus unrelated models) | Candidate raw model IDs available from the local router inventory. | Per-alias model selection, serialized effort field/value, normalization, correlation propagation, or observed telemetry. |

The inventory is sufficient to record candidate model IDs as explicit local
adapter data only when each mapping is version-bound and fixture-tested. It is
not evidence for an inferred effort representation. Any effort-field behavior
not observed through a reproducible adapter interface remains unverified; M6
must model it through synthetic fixtures and must not claim a live attestation
or promotion from that fixture result.

## Bounded Public Contract

M6 owns a pure, project-local policy/attestation layer. It does not execute a
provider call, install a plugin, write an agent into `~/.codex`, or implement the
M7 graph runtime.

| Public value or operation | Required behavior |
| --- | --- |
| `ModelProfileRegistryV2` | The sole active registry is `config/model-profiles.json`. It contains exactly `tera-max`, `tera-xhigh`, `tera-high`, `sol-max`, `sol-xhigh`, `luna-xhigh`, and `gpt55-xhigh`; `tera-max` is the only default root. Logical display name, family, role, and effort intent remain the seven accepted profiles. |
| Versioned adapter mapping | A `nine_router` mapping binds one raw model ID, an explicit serialized effort field name and value, adapter version, client/router evidence versions, and normalization-rules version. It rejects placeholders, unknown fields, unpinned/empty versions, unsupported effort intent, and mappings whose evidence is unavailable. Raw slugs and wire values remain adapter data, never prompt/agent-policy text. |
| `RouteIntent` | Create before serialization. Bind opaque route/task/node IDs, profile alias, logical effort intent, registry version, adapter identity/version, and a correlation ID. It is immutable and strict; an unknown alias or a profile/intent mismatch is invalid. |
| `SerializedRoute` | Resolve only from the active registry and capture the allowlisted routing fields actually proposed for sending: route/correlation IDs, raw model slug, serialized effort field/value, request schema version, timestamp, and a SHA-256 payload hash of canonical redacted routing fields. It must not retain prompt/body text, authorization headers, endpoint credentials, cookies, arbitrary payload keys, or UI-derived model text. |
| `TelemetryObservation` | Require the same correlation and route identity, exactly one matching observation, observed model and effort, router request ID, provider, router version, normalization-rules version, and timestamp. Missing required fields or duplicate candidate observations are not successful observations. |
| `reconcile_route` | Return exactly one of `MATCH`, `MISMATCH_MODEL`, `MISMATCH_EFFORT`, `MISMATCH_BOTH`, `MISSING_TELEMETRY`, `AMBIGUOUS_TELEMETRY`, or `UNSUPPORTED_MAPPING`. Compare exact normalized values under the recorded rules version. Never collapse `high`, `max`, and `xhigh`; a requested non-xhigh and observed xhigh is `MISMATCH_EFFORT`. |
| `RouteReceipt` | Serialize a small allowlist only: opaque identifiers, profile/effort intent, registry/adapter/router/normalization versions, routing-field hash, status, controlled reason codes, timestamps, and redacted/digest references. Receipt conversion must be deterministic and must not stringify an unbounded request, observation, exception, prompt, secret, credential, endpoint URL, or raw telemetry blob. |
| `side_effect_allowed` | For `GRAPH` and `DEEP`, only `MATCH` permits a commit/side effect. Every other reconciliation status quarantines output and blocks the operation. A `DIRECT` read-only result with missing telemetry may be returned only as `UNVERIFIED_ROUTE`; it cannot become verified durable memory, a benchmark result, or a side effect. M6 exposes this gate but does not execute an external action. |
| Canary and rollback | A local, synthetic seven-profile canary resolves, serializes, observes, and reconciles every alias. Five-repeat mode performs 35 deterministic checks. A last-known-good receipt binds a canonical registry/config digest and adapter version; a local rollback restores only a contained staging target after digest validation and is rehearsal-tested. |
| Generated staging | TOML files under `dist/global/m6/` are generated from the registry, parseable with `tomllib`, and have explicit `model` and `model_reasoning_effort` fields. `tera-max` is the staging root default. These are review artifacts, never deployment instructions. |

## Exact M6 Acceptance Checks

1. The active V2 registry schema and semantic validator accept exactly seven
   aliases and reject a missing, extra, renamed, duplicate, placeholder, or
   logical-profile-drift entry.
2. All seven aliases resolve via that one registry; no secondary alias-to-model
   table is consulted. Generated TOML is derived from those resolutions and
   explicitly names `model` plus `model_reasoning_effort` for every profile.
3. The generated root staging artifact declares `tera-max`; no caller default,
   parent profile, or TOML omission can change it.
4. Serialization and reconciliation preserve three distinct logical effort
   classes. Tests must prove that the declared representation for `high`, `max`,
   and `xhigh` is compared exactly rather than via a broad class or substring.
5. The permanent historical fixture creates a non-xhigh request and an xhigh
   observation under the same correlation. It must be `MISMATCH_EFFORT` in every
   run, including all five repeats.
6. Missing observation is `MISSING_TELEMETRY`; duplicate/wrongly correlated or
   incomplete observation is `AMBIGUOUS_TELEMETRY`; unknown adapter/mapping
   evidence is `UNSUPPORTED_MAPPING`. None is reported as `MATCH` or
   `UNVERIFIED_ROUTE` for a graph/deep node.
7. A graph/deep side-effect decision is false for every non-`MATCH` status,
   including an effort increase that might appear higher quality. The direct
   low-risk exception remains read-only and visibly unverified.
8. Receipts and errors are allowlist-redacted. A fixture containing a prompt-like
   string, bearer token, cookie, endpoint URL, or arbitrary nested telemetry
   must leave none of those bytes in the receipt or its canonical hash input.
9. The seven-profile local canary has seven `MATCH` results in one pass and 35
   `MATCH` results in five-repeat mode. It uses only synthetic non-sensitive
   data and does not make a network/provider mutation.
10. A last-known-good snapshot records exact registry/config and adapter digests.
    Local rollback succeeds only when its current target matches the expected
    candidate digest, restores the known-good staged files, and rejects tampered
    receipts, path escape, and an attempt to target `~/.codex`.
11. `make check`, clean-room validation, schema validation, and the focused M6
    suite pass after the M0 generated-asset ownership issue is resolved.

## Schema and Compatibility Recommendations

- Add `model-profile-registry-v2.json` rather than widening or silently
  repurposing V1. The active config and canonical V2 fixture must have
  `registry_version: 2`; V1 remains a historical M0 placeholder contract.
- Keep schema objects closed (`additionalProperties: false`) at the registry,
  profile, adapter, intent, serialized-route, observation, receipt, canary, and
  rollback levels. Bound IDs, versions, hash fields, timestamps, and reason codes
  with existing strict JSON/schema helpers. Do not add a free-form metadata or
  raw-payload escape hatch.
- Put `serialized_effort_field` and `serialized_effort_value` in the adapter
  contract separately. The V1 `serialized_effort` string cannot describe actual
  wire behavior or prove field capture. Bind the selected normalization rules
  version to both serialization and observation.
- Treat a V1 registry as non-routable in M6. A reader may identify it only to
  produce a clear migration/unsupported-mapping error; it must not invent raw
  model or effort values while upgrading. V2 is the one writer/active read
  version for model routing.
- Add the V2 schema and fixture to `schemas/schema-registry.json` and update
  profile/contract tests to validate V2. If the schema registry version changes,
  update its explicit test expectation and retain V1 assets for historical
  validation rather than overwriting them.
- Preserve ordinary M5 `Lane` values as the boundary for the side-effect gate.
  Do not invent an M7 execution state machine or perform a durable/external
  operation in M6 merely to test the predicate.

## Required M0 Generated-Asset Resolution

This is a real acceptance blocker, not a cosmetic cleanup. The current M0
generator and its test own the active registry as a V1 placeholder:

- `scripts/build_m0_contract_assets.py --check` regenerates
  `config/model-profiles.json`, `schemas/schema-registry.json`, and
  `fixtures/canonical/model-profile-registry-v1.json` as V1 output.
- `tests/test_contract_schemas.py` invokes that check, and `tests/test_profiles.py`
  requires every adapter to equal the M0 placeholder strings.

Therefore changing the active config to V2 under the current implementation
scope makes `make check` fail. Before M6 acceptance, expand ownership or make
an equivalent controlled change so that the M0 generator verifies only the V1
historical assets it still owns, while the M6 source owns the active V2 config,
schema registry entry, V2 canonical fixture, and profile tests. A valid
alternative is to update the generator itself to produce and check the V2
source of truth. Do not keep a second active V1 registry merely to satisfy the
old check; that violates the one-registry requirement.

## Focused Test Checklist

- Registry/schema: V2 canonical acceptance; duplicate/unknown/missing alias;
  V1 rejection for active routing; wrong root; placeholder/unpinned mapping;
  unsupported effort; distinct `high`/`max`/`xhigh` serialization.
- Generation: each of seven TOML files parses, has explicit model and effort,
  matches `resolve(alias)`, and no raw mapping lives outside the registry.
- Attestation: immutable intent; deterministic canonical payload hash;
  correlation propagation; `MATCH`; all three mismatch variants; missing,
  duplicate, conflicting, and incomplete telemetry; normalization-version drift.
- Security: receipt/error allowlist redaction; secret/prompt/URL/cookie fixture;
  no global path; only repository-contained staging/rollback targets; direct
  unverified result cannot become verified or side-effecting.
- Canary/rollback: seven aliases once and five times; injected non-xhigh/xhigh
  regression; candidate mismatch blocks promotion; known-good replay; tampered
  digest and unsafe target fail closed; local rollback restoration is rehearsed.
- Regression: run focused profile/routing/integration tests, then
  `python3 scripts/build_m0_contract_assets.py --check`, `make check`, and
  `python3 scripts/check_clean_room.py`. The M0 generator check is expected to
  be included only after its V1/V2 ownership conflict has been resolved.

## Requirement Traceability and Boundaries

This contract covers FR-04, FR-17, FR-18, FR-19, NFR-01, NFR-09, NFR-10,
NFR-11, AC-08, AC-11, ADR-003, and ADR-006. It intentionally does not claim a
live provider canary, privacy approval for `INTERNAL`/`SENSITIVE` data, M7 graph
execution, or global activation. The old Obsidian vault, `ai-memory`,
`~/.codex`, and global plugins/MCP/hooks remain outside scope.
