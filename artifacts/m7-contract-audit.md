# M7 Contract Audit: Bounded Work-Graph Runtime

Status: implementation-ready, project-local only. This audit defines the
bounded M7 contract; it does not authorize a live provider call, a capability
execution, a durable commit, an external side effect, or a global Codex change.

## Contract Sources

- `docs/09-IMPLEMENTATION-ROADMAP.md` section 10 defines M7's outputs,
  acceptance criteria, and requirement ownership.
- `docs/01-PRD.md` FR-03, FR-06, FR-17 through FR-19, NFR-01, NFR-03,
  NFR-06, NFR-09, NFR-11, and AC-03, AC-08, and AC-09 define root ownership,
  bounded graph work, cancellation, audit, context, availability, and
  security.
- `docs/04-WORKFLOW-ORCHESTRATION.md` sections 3 through 7 define admission,
  graph limits, manifest fields, result-envelope bounds, retry behavior,
  context budgets, and redacted observability.
- `docs/05-MODEL-ROUTING.md` and ADR-003 require explicit logical profile
  aliases and visible route integrity; M7 cannot inherit a parent profile.
- `docs/07-SECURITY-AND-PRIVACY.md` sections 2, 5, 6, 8, and 11 require
  least privilege, no worker nesting, single write ownership, fail-visible
  route handling, and redacted logs.
- `docs/08-EVALUATION-AND-TEST-PLAN.md` sections 5.4 through 5.7 define the
  deterministic graph, route, context, and security gates. The benchmark
  performance targets themselves remain M8 evidence, not an M7 claim.
- ADR-002, ADR-003, ADR-004, and ADR-006 are binding: direct-by-default,
  explicit profiles, progressive capability disclosure, and project-local
  staging only.

## M7 Boundary

M7 is a deterministic, synthetic-local DAG validator and scheduler. It models
the execution contract and runs injected test workers only. It must not create
actual Codex child agents, invoke a tool/capability, run a shell command, open a
network connection, mutate an authority store, or write a global configuration.
An injected worker receives a narrow immutable context and returns a typed
proposal. It never receives a runtime object, a filesystem handle, a capability
executor, a permission broker, or a worker-spawn function.

`workspace-write` in an M7 manifest means a declared proposal ownership scope;
it is not permission to commit. Every proposed material change remains behind
M5's live-host permission boundary and M6's future authenticated live-route
receipt boundary. Neither exists in this milestone.

## Exact Public Surface

Implement one project-local module, `second_brain.graph_runtime`, and re-export
its intentional public values from `second_brain.__init__`. Use frozen
dataclasses, strict `from_value()` / `to_dict()` conversions, existing canonical
SHA-256 helpers, existing strict JSON loading, injected clocks, and stable,
allowlisted error/reason codes. Unknown fields, enum values, booleans in integer
positions, duplicate identifiers, and mutable caller mappings fail before
scheduling.

| Public value or operation | Required contract |
| --- | --- |
| `WorkGraphManifest` / `WorkGraphNode` | Parse and preserve the V1 manifest shape below. It is declarative data only; it cannot carry an executor, nested graph, approval, raw route mapping, or arbitrary extension object. |
| `validate_work_graph_manifest()` | Run schema plus all semantic checks before a worker, route attempt, or scheduler object starts. Return a frozen validated graph/snapshot or raise a stable validation/policy error. |
| `GraphRuntime` | Accept an injected clock and an immutable M6 registry snapshot. `run()` requires a validated M5 admission binding and a synthetic worker callback. Its internally owned state cannot be supplied, resumed, or forged by a caller. |
| `NodeContext` | A minimal immutable packet: manifest/task and node identity, lane, logical profile, declared read/write scopes, acceptance IDs, bounded declared task text, direct-dependency typed result references, and cancellation state. It carries no full parent transcript, registry/config content, filesystem object, credential, raw tool output, or child-runtime handle. |
| `NodeResult` / typed references | A bounded result proposal containing a controlled terminal outcome, artifact/evidence references with opaque IDs and SHA-256 hashes, declared proposed changes, test/check references, controlled unresolved codes, bounded next-node artifact references, usage counters, and an optional redacted M6 receipt reference. It cannot add nodes, profiles, permissions, capabilities, scopes, raw payloads, exception strings, or arbitrary metadata. |
| `CancellationToken` | An immutable-to-worker view with an internal runtime-owned cancellation event. A worker can poll only `cancelled`; it cannot clear cancellation, create work, or alter graph state. |
| `GraphEvent` / `GraphRunReceipt` | M7-owned redacted observability values. They expose only opaque IDs, digests, closed state/reason codes, profile alias, lane, timestamps/durations, bounded token/cost counters, and artifact IDs/digests. They never serialize goal/task text, scope paths, raw context, result body, exception text, prompt, telemetry blob, header, URL, credential, cookie, or secret. |
| `compact_artifact()` / `CompactedArtifact` | Convert an oversized out-of-context worker log/result into an artifact ID, byte/token counts, SHA-256 content hash, and a bounded digest-backed summary/reference. The root packet receives only this compact form. Raw bytes are caller-owned local evidence and are never included in a graph receipt or observability event. |
| `run(..., serial_fallback=False)` and `cancel()` | Schedule only a prevalidated graph with at most four concurrent injected workers. Support explicit safe cancellation, one transient retry, one evidence-bound profile reroute, and an explicit serial fallback. Return a redacted digest-bound run receipt plus typed final/integration result references. |

The public API may use a single `GraphRun` handle rather than separate
`run()`/`cancel()` free functions, but it must retain these authority and
observability properties. A runner has this narrow conceptual protocol:

```python
def runner(context: NodeContext, cancellation: CancellationToken) -> NodeResult:
    ...  # Returns a proposal; it receives no executor or write authority.
```

No callback return type may contain a callable, `Path`, raw exception, child
manifest, mutable mapping that is later trusted, or a request to spawn a worker.

## V1 Manifest and Schema Contract

Add `schemas/work-graph-v1.json` and
`fixtures/canonical/work-graph-v1.json`, then register them manually in
`schemas/schema-registry.json`. Keep the registry version at `1`, as M6 did;
adding a closed, independently versioned schema entry is not a change to the
meaning of the registry itself.

The schema must use Draft 2020-12, `additionalProperties: false` at every
object level, bounded arrays/strings, ASCII-safe logical identifiers, and
explicit integer types. It validates shape; `validate_work_graph_manifest()`
performs DAG, scope, role, and admission semantics.

Top-level V1 fields are exactly:

```json
{
  "version": 1,
  "task_id": "task:<lowercase-canonical-uuid>",
  "lane": "GRAPH|DEEP",
  "goal": "bounded root-authored objective",
  "max_concurrency": 1,
  "nodes": [],
  "final_node": "node-id"
}
```

`goal` is root-authored in-memory task data, maximum 512 characters, and is
never copied raw to a graph event, receipt, observability digest input, or
compaction summary. A separately computed goal digest may bind it to the
in-memory graph/run receipt without exposing the text.
`max_concurrency` is an integer in `[1, 4]`; a graph contains `[2, 12]` nodes.
The schema must reject `DIRECT` and `ASSISTED`, rather than allowing callers to
reinterpret them later.

Each node has exactly these fields:

```json
{
  "id": "lowercase-node-id",
  "role": "worker|reviewer|integrator",
  "task": "bounded root-authored node objective",
  "profile": "one of the seven logical aliases",
  "depends_on": [],
  "read_scope": [],
  "write_scope": [],
  "capabilities": [],
  "expected_artifacts": [],
  "acceptance": [],
  "timeout_seconds": 30,
  "max_attempts": 1,
  "permission_class": "local-read|workspace-write",
  "reroute_to": null
}
```

`id` is a unique lowercase slug (`[a-z][a-z0-9-]{0,63}`). `task` is bounded to
512 characters and, like `goal`, is never observable raw. `depends_on` is a
unique list of known other node IDs. `capabilities` is a bounded list of
declarative `cap:<slug>` identifiers only; it does not load or activate the
descriptor. `expected_artifacts` is a nonempty bounded list of opaque artifact
identifiers. `acceptance` is a nonempty bounded list of root-authored check IDs
or bounded check labels. `timeout_seconds` is an integer in `[30, 900]` and
`max_attempts` is exactly `1` or `2`.

A scope is a normalized repository-relative logical file or directory name:

- no absolute path, `~`, backslash, NUL, URI scheme, glob, empty segment, `.`
  or `..` component, encoded separator, or duplicate slash;
- directory scopes use one trailing `/`; file scopes do not;
- a scope is compared by its normalized component boundary, never by a raw
  substring;
- the manifest declares only repository-local intent. A future real executor
  must still re-check actual canonical paths and symlinks through M1/M5 before
  a write.

`permission_class=local-read` requires an empty `write_scope`. A nonempty
`write_scope` requires `workspace-write`. V1 deliberately excludes
`network-read`, `external-write`, `install-executable`, and `destructive` from
the schema: M7 has no live execution host for them. An unsupported class is an
invalid graph, not a permission to defer silently.

`reroute_to` is either null or one approved logical alias. It is a predeclared
candidate only; it never changes the node's task, scopes, capabilities,
permission, acceptance, or expected artifacts.

## Required Semantic Validation

All checks below run over the entire manifest before worker start. A failure
rejects the graph atomically and leaves the runner call count at zero.

1. Verify task ID format, lane, closed node fields, unique IDs, profile aliases
   using the M6 registry snapshot, integer bounds, and every list bound.
2. Reject missing dependencies, self-dependencies, cycles, hidden child/nested
   graph fields, unknown capability/profile/permission values, and any profile
   inherited from root or parent.
3. Treat dependency edges as DAG ordering, not agent nesting. The runtime has
   fixed delegation depth one: every node is a direct root-owned worker and the
   API contains no child-spawn operation. A longer dependency chain is allowed
   only as ordering within the one root-owned graph.
4. Require exactly one `integrator`. It is `final_node`, uses `tera-max`, has
   no dependents, and has a transitive dependency path from every other node.
   This makes integration artifact-based rather than a partial-summary merge.
5. Reject any two write scopes that are equal or ancestor/descendant overlaps,
   even if their nodes would run sequentially. There is one declared writer for
   each logical scope for the entire graph. A reader of another node's write
   scope must transitively depend on that writer; otherwise reject the possible
   read/write race.
6. `reviewer` nodes are `local-read` with an empty write scope. A
   `gpt55-xhigh` node must be a reviewer, never a writer/integrator. A
   `luna-xhigh` node must be a worker, `local-read`, have an empty write scope,
   no reroute, and no capability activation request. These hard gates are in
   addition to M5's Luna permission guard.
7. A `DEEP` graph has at least one read-only reviewer whose profile differs from
   every node it directly reviews and whose results are on the final integrator
   path. The canonical M7 fixture should use `gpt55-xhigh`. A graph admitted by
   M5 with `INDEPENDENT_BRANCHES` has at least two non-integrator nodes with no
   dependency path between them. A user-explicit graph may be serial but must
   retain a visible `USER_EXPLICIT_GRAPH` admission reason and earns no
   automatic speedup claim.
8. Resolve the M6 registry once at validation/start time and retain that frozen
   snapshot/digest for every node route attempt. The manifest names only logical
   aliases; raw model slugs, effort values, and runtime `pixel-*` profile names
   are invalid here.
9. A reroute is permitted only once per node, only after a controlled
   evidence/reference digest states that the old profile is inadequate, and only
   along the fixed policy edges: `luna-xhigh -> tera-high`,
   `tera-high -> tera-xhigh`, `tera-xhigh -> tera-max`, and
   `sol-xhigh -> sol-max`. Builders may request an independent
   `gpt55-xhigh` reviewer through a declared reviewer node, not by converting a
   builder into one. Any other reroute is a policy failure.

The canonical fixture should contain two independent bounded workers, one
read-only reviewer, and one `tera-max` integrator. Its writer scopes must be
disjoint; its reviewer and integrator dependencies must demonstrate the
read-after-write and join rules without modifying real repository files.

## Admission, Permission, and Route Bindings

### M5 admission binding

`GraphRuntime.run()` must require all of the following before scheduling:

- an `AdmissionDecision` with lane exactly equal to `manifest.lane`;
- the same canonical `task_id` as the manifest;
- `AdmissionDecision.verify()` true; and
- `AuditTrail.has_admission(decision)` true on the injected trail that issued
  it.

Do not trust a digest-valid caller-created `AdmissionDecision`; M5 explicitly
keeps issuance evidence private to the trail. `DIRECT` and `ASSISTED` are
rejected before route creation or runner invocation. M7 must not alter M5's
closed `AuditEvent` enum merely to add graph events; use the separate M7
redacted graph receipt/event types instead.

M7 validates only declarative V1 local permissions. It does not call a
capability, treat a `PermissionDecision(granted)` as a live host grant, or
execute an M5 `PermissionRequest`. A future execution host must bind every
actual action and exact target to M5 separately. For M7, a worker's proposed
`changes` are checked against its declared `write_scope` and retained as
uncommitted proposals only.

### M6 routing binding

For each attempt, construct a M6 `RouteIntent` from the frozen registry,
serialize it through the existing M6 API, and attach a redacted receipt/reference
to the node result. The default project-local test mode may use only M6's
synthetic observation helper. A non-`MATCH` M6 reconciliation for a `GRAPH` or
`DEEP` node quarantines that node result and blocks downstream integration.
Missing/ambiguous route evidence is never a success.

M6 currently makes `side_effect_allowed()` an unconditional denial for every
receipt, including synthetic `MATCH` and hostile subclasses. M7 must preserve
that result. It may schedule a synthetic proposal-producing writer for ownership
tests, but it must never call a store commit or label a change committed. A
material commit gate is false for every current M6 receipt and must be visible
as `PROPOSAL_ONLY`/blocked material action in the run receipt.

## Runtime Lifecycle

Use an internal, runtime-owned state machine. Public values may report states
but callers cannot provide a state snapshot to skip validation or dependency
gates.

```text
VALIDATED -> RUNNING -> SUCCEEDED | FAILED | CANCELLED | BLOCKED

PENDING -> READY -> RUNNING -> SUCCEEDED
                           -> FAILED | BLOCKED | TIMED_OUT | QUARANTINED_ROUTE
                           -> CANCELLED
                           -> RETRY_WAIT -> READY
```

- Ready nodes are selected deterministically by node ID once every dependency
  succeeded. The scheduler uses `min(manifest.max_concurrency, 4)` slots and
  never creates another worker from a worker.
- A failed, blocked, timed-out, cancelled, or route-quarantined dependency
  makes every downstream node `BLOCKED_DEPENDENCY`; no partial merge occurs.
- A retry occurs at most once and only for a closed transient outcome such as
  local synthetic timeout or temporary-unavailable. Validation, permission,
  cancellation, scope, security, and route-integrity failures are not ordinary
  retries. `max_attempts` remains the hard ceiling.
- A profile reroute is at most once and follows the predeclared edge above with
  a new evidence digest. It preserves the node identity and every declared
  scope/boundary. M6 mapping mismatch retries remain governed by M6's existing
  pre-side-effect `retry_allowed()` conditions; they never become a commit
  authorization.
- `cancel()` immediately cancels pending/ready nodes. Running workers receive
  the token and must stop before their next proposal/commit boundary. If a
  runner returns success after cancellation was requested, the runtime discards
  that result and records `CANCELLED`; downstream nodes never begin. The
  synthetic callback model is deliberate: M7 has no authority to kill an
  untrusted real process and provides no real write capability for it to race.
- Serial fallback is available only for an already validated graph and is
  explicit/audited with a controlled `SERIAL_FALLBACK` reason. It runs the same
  DAG with one slot and preserves all admission, profile, route, scope,
  cancellation, timeout, and dependency checks. It is forbidden for invalid
  graphs, `DIRECT`/`ASSISTED`, cancellation, permission/security failures, or
  route quarantine; it cannot be an implicit bypass.

The integrator receives typed successful dependency references and their
verification state, not concatenated worker summaries. It must fail verification
when a required artifact/check reference is absent, stale/quarantined, or outside
the declared input path. The M7 runtime only reports the resulting proposal;
root remains the authority for the user-facing answer and any later durable
write.

## Context, Result, Compaction, and Observability Bounds

M7 should use deterministic UTF-8 byte counting with a documented conservative
token estimate (for example, `ceil(bytes / 4)`) rather than trusting a
caller-supplied token count. The implementation must enforce all of these
limits before passing a packet onward:

- worker context: at most 16,000 estimated tokens and 32 typed input references;
- worker result envelope: at most 2,000 estimated tokens and 32 typed references;
- reviewer/integrator envelope: at most 4,000 estimated tokens and 48 typed
  references;
- no result body or out-of-context raw artifact is implicitly included merely
  because the caller has spare window capacity.

Large data becomes a `CompactedArtifact` with an opaque artifact ID, byte count,
content hash, and bounded summary/reference. Its raw body stays outside root
context and outside all `to_dict()`/digest inputs for telemetry. Context may
reference only the node's declared read scope and successful direct dependency
artifacts. A result change must fit a node's declared write scope; a result may
send only declared artifact references to existing downstream nodes.

Emit only allowlisted M7 graph events such as `graph_validated`, `node_ready`,
`node_started`, `node_finished`, `node_blocked`, `node_retry_scheduled`,
`node_rerouted`, `graph_cancel_requested`, `serial_fallback`, `artifact_compacted`,
and `graph_closed`. Each carries opaque task/node/run IDs, lane, logical profile,
closed status/reason, timestamp/duration, numeric queue/start/finish/usage/cost
values when supplied, and hashes/opaque artifact IDs only. Never stringify a
runner exception or arbitrary mapping to make an event.

## Compatibility Seams

| Existing contract | M7 requirement |
| --- | --- |
| M0 schema generator | `scripts/build_m0_contract_assets.py --check` intentionally owns only frozen M0 schemas/fixtures. Do not add the M7 schema or fixture to its generated output and do not alter historical V1 profile ownership. The new M7 assets are manually owned by M7 just as M6 owns its V2 assets. |
| Schema registry/tests | Add one `work-graph-v1` entry. `tests/test_contract_schemas.py` already iterates registry entries, while `tests/test_project_metadata.py` currently asserts five schemas and must become six. `contract_checks.py` should call M7's semantic validator for the canonical fixture after generic schema validation; `contracts.py` need not be widened for this runtime-specific semantic contract. |
| Historical graph records | `planning/work-graph.json`, `artifacts/m5-work-graph.json`, `artifacts/m6-work-graph.json`, and `artifacts/m7-work-graph.json` are historical orchestration records, not M7 manifest fixtures. They use runtime `pixel-*` profile labels and/or omit V1 fields. The M7 validator must never scan or reinterpret them automatically. |
| M5 admission | Reuse `Lane`, `PermissionClass`, `AdmissionDecision`, and `AuditTrail`; do not duplicate their enums or bypass the private issuance proof. Do not modify `admission.py` merely for M7 event logging. |
| M5 capabilities/permissions | A manifest capability is metadata, not an activation. M7 must not inspect untrusted descriptions, call the resolver, execute a capability, or convert a project-local M5 decision into a live permission. |
| M1-M4 authority/context | Do not open a store, recovery pack, raw capture, index, or retriever automatically. M7 accepts only supplied bounded artifact references; any later durable mutation remains M1 CAS/transaction work outside M7. |
| M6 profiles/routes | Resolve only the seven V2 logical aliases from one frozen registry snapshot. Do not copy raw model slugs/efforts into the manifest, change `routing.py`, claim live attestation, or weaken the denial-only `side_effect_allowed()` boundary. |
| Global containment | No `~/.codex`, old Obsidian vault, `/home/pixel/Data/PROJECT/ai-memory`, global plugin/MCP/hook state, provider config, or live endpoint is read or written. All fixtures and receipts remain project-local and synthetic. |

## Deterministic Acceptance Matrix

| Contract / requirement | M7 locally verifiable evidence |
| --- | --- |
| FR-03, AC-03 | Exactly one `tera-max` final integrator joins every node through typed references; root authority is not transferred to a worker. |
| FR-06, ADR-002 | M5-issued GRAPH/DEEP admission is required; DIRECT/ASSISTED cannot instantiate a graph; invalid DAG/scope/profile/permission data produces zero worker starts. |
| FR-17 | Receipt exposes redacted lane/profile/reason/fallback/cancellation state; user redirect cancels pending work and visibly stops proposed writer results. |
| FR-18, NFR-11, AC-08 | Dependency failure blocks descendants, transient retry/reroute are bounded and visible, and explicit serial fallback preserves all gates. Route mismatch/missing evidence is quarantined rather than silently downgraded. |
| FR-19 | Structured M7 event/receipt fields are closed, digest-bound, bounded, and redacted; queue/latency/token/cost counters are available without prompt/source content. |
| NFR-01 | M7 preserves verification and independent-review structure but does not claim a benchmark quality result; M8 must measure non-inferiority. |
| NFR-03 | Worker/root result and context caps, direct-dependency-only inputs, compaction hashes, and artifact-based integration are deterministically tested. |
| NFR-06 | Scheduler reports enough duration/context counters for M8 B1 comparison; M7 proves at most-four bounded scheduling but not the 25%/20% benchmark itself. |
| NFR-09, AC-09 | Scope containment, one writer, Luna/reviewer gates, no dynamic workers/scopes, cancellation, no raw log leakage, and M6 material denial are adversarially tested. |
| ADR-003 | Every node names one approved logical profile, captures a redacted local M6 route reference, and never relies on inherited/raw profile strings. |
| ADR-004 | The runtime neither discovers nor activates capabilities; its declared IDs remain minimal metadata only. |
| ADR-006 | Tests use only synthetic local runners/artifacts and verify no global configuration or source-vault access. |

## Focused Test Plan

Add `tests/test_work_graph_m7.py` and `tests/test_m7_integration.py`, with
inline invalid mappings plus synthetic fixtures under `fixtures/m7/`. Use a
deterministic clock, bounded in-memory runners, barriers/counters for scheduler
tests, and no network or real filesystem mutation.

1. Validate the canonical V1 schema/fixture through the registry; reject
   unknown fields, duplicate IDs, boolean integers, direct/assisted lane,
   malformed task ID, unknown logical alias, timeout outside 30-900, attempts
   outside 1-2, node count outside 2-12, and concurrency outside 1-4.
2. Prove every invalid fixture has zero runner calls: missing/self dependency,
   cycle, final-node mismatch, missing/multiple integrator, incomplete join,
   `pixel-*` profile, invalid permission, nested-worker field, malformed scope,
   writer overlap including prefix overlap, and undeclared read-after-write.
3. Exercise M5 binding with an actual `AdmissionEngine` and `AuditTrail`:
   direct/assisted decisions reject; task/lane mismatch rejects; a digest-valid
   forged decision without trail issuance rejects; valid GRAPH/DEEP decisions
   are the only scheduling entry point.
4. Test role gates: an automatic independent-branches admission needs two
   independent work nodes; DEEP requires an independent reviewer; gpt reviewer
   cannot write; Luna cannot write/reroute/request capabilities; final
   integrator is `tera-max` and joins all output.
5. Run a branch/join graph with controlled barriers and prove no more than
   `max_concurrency` or four nodes run at once, ready ordering is deterministic,
   dependencies start only after success, and a failed/timed-out/quarantined
   dependency blocks every descendant.
6. Cover exactly one transient retry and exactly one evidence-bound allowed
   reroute. Prove security, scope, permission, cancellation, and route failures
   do not retry; prove an unknown/escalation-skipping reroute cannot change
   profile, scope, or capability.
7. Cancel before start and during a writer: pending nodes never run, active
   callback sees cancellation, a late success is discarded, no descendant runs,
   and every proposed change remains uncommitted/abandoned.
8. Verify explicit serial fallback uses one slot after successful validation and
   records its reason. It must refuse invalid/direct/cancelled/route-quarantined
   graphs rather than converting them into a root workaround.
9. Feed oversized context/result/log fixtures and prompt-injection/secret/URL/
   cookie markers. Prove cap/compaction behavior, declared-scope enforcement,
   direct-dependency-only context, typed integration, and absence of marker
   bytes from all events, receipts, serializations, and digest inputs.
10. Integrate M6 using synthetic receipts: profile snapshot is stable, a
    synthetic `MATCH` permits only proposal computation, every non-`MATCH`
    quarantines graph/deep output, and `side_effect_allowed()` remains false
    for ordinary and hostile-subclass receipts. No test may fabricate live
    attestation.
11. Run schema checks, focused M7 tests, existing M5/M6 regression tests,
    `python3 scripts/build_m0_contract_assets.py --check`, `make check`, and
    `python3 scripts/check_clean_room.py` after implementation. The M0 asset
    check must remain green without generating M7-owned files.

## Explicit Non-Goals

- Running actual Codex subagents, treating dependency depth as nested agent
  depth, or allowing a worker to create work.
- Executing a skill/plugin/MCP/tool, research, connector call, shell command,
  install, delete, publish, login, or external mutation.
- Committing M1/M2/M3/M4 authority state, bypassing CAS, or turning a proposed
  graph change into a durable write.
- Treating any M5 policy result or synthetic M6 `MATCH` as a trusted live-host
  authorization.
- Live 9router/provider attestation, route canary promotion, external data
  privacy approval, global configuration staging/activation, or M9 approval.
- Claiming the M8 semantic-quality, wall-clock speedup, context-reduction, or
  production security targets from synthetic M7 tests alone.

This contract covers FR-03, FR-06, FR-17, FR-18, FR-19; NFR-01, NFR-03,
NFR-06, NFR-09, NFR-11; AC-03, AC-08, AC-09; and ADR-002, ADR-003, ADR-004,
and ADR-006 within the local M7 scope. Rollback is limited to M7 schema,
fixture, runtime, exports, tests, documentation, and receipt artifacts. It must
not remove prior authority state or alter any global configuration.
