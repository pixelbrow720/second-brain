# M7 Bounded Work-Graph Runtime

M7 is the project-local implementation of the bounded work-graph contract in
[the roadmap](09-IMPLEMENTATION-ROADMAP.md). It validates and executes a
synthetic dependency graph without granting a worker authority to mutate a
store, call a provider, run a capability, or change global Codex configuration.

Status: complete and locally verified on 2026-07-23. The exact local receipt is
[`artifacts/m7-verification.json`](../artifacts/m7-verification.json).

## Contract and Admission Boundary

`schemas/work-graph-v1.json` and
`fixtures/canonical/work-graph-v1.json` define the closed V1 manifest. The
public `second_brain.graph_runtime` surface validates the JSON schema and then
the complete semantic contract before it can create a worker thread:

- only `GRAPH` and `DEEP` lanes are accepted; `DIRECT` and `ASSISTED` have no
  graph-runtime path;
- each graph has at most 12 nodes and four concurrent workers, explicit
  dependencies, one `tera-max` final integrator, fixed profile aliases, bounded
  timeout/attempt values, and non-overlapping writer scopes;
- `DEEP` requires a terminal read-only reviewer using a different approved
  profile; `gpt55-xhigh` is reserved for that review role;
- cycles, missing/self dependencies, profile/registry drift, invalid scopes,
  improper reviewer ownership, unknown fields, and invalid M5 admission are
  rejected before the injected runner is called.

Graph entry binds an issued M5 `AdmissionDecision` to its exact `AuditTrail`,
task ID, lane, and digest. A caller-created decision, an audit subclass, or a
shadowed audit method cannot grant entry. `GRAPH` additionally retains the
declared independent-branch condition rather than accepting a free-form claim.

## Synthetic Runtime Boundary

`GraphRuntime` owns private lifecycle state and only invokes an explicit,
injected synthetic callback. A node receives an immutable `NodeContext` with
its declared contract and bounded references from successful direct
dependencies. It receives no runtime, graph builder, filesystem handle,
capability executor, permission broker, or worker-spawn API.

The scheduler atomically claims ready nodes, enforces the four-worker cap,
blocks all descendants after a failed/quarantined/cancelled dependency, and
requires the final integrator to prove its declared artifacts and acceptance
checks through the full dependency path. A worker result is a strict typed
`NodeResult`; its artifacts, acceptance checks, proposal paths, identity, token
budget, reference budget, and route binding are revalidated before state can
advance. Proposed changes remain proposals only.

Cancellation wins over a late success. A redirect stops pending claims, signals
active work through an opaque cancellation token, and quarantines a late result.
Retries are limited to one additional attempt and only for the closed temporary
availability reasons. Reroute preserves node identity, scope, permission,
acceptance, and the pre-approved escalation edge. Serial fallback changes only
scheduling order for the already validated graph; it cannot admit a direct or
invalid graph or bypass a failed policy, route, or dependency gate.

## Scope, Route, and Observability Boundaries

Scopes are repository-relative and checked by path components. Absolute paths,
traversal, glob/variable/file-URI ambiguity, root scopes, symlink escape, and
file/descendant confusion fail closed. Writer scopes cannot overlap; a
read-only reviewer cannot return a proposal; a returned proposal must be inside
the node's declared scope.

Each dispatch constructs an immutable M6 route intent and accepts only a
reconciled local synthetic `MATCH` for scheduling diagnostics. M6's
`side_effect_allowed()` remains an unconditional denial boundary, including for
a synthetic `MATCH`; neither a manifest permission nor a route receipt can
authorize a material action in M7.

Graph events, node receipts, compacted artifacts, and final run receipts are
redacted allowlists of opaque IDs/digests, closed states/reasons, bounded
metrics, and safe artifact metadata. They do not store worker task bodies,
prompts, responses, exception text, headers, URLs, credentials, cookies,
environment data, or raw transcripts. The runtime records token/cost/latency
accounting so M8 can measure performance without treating an M7 fixture run as
a production benchmark.

## Verification, Review, and Limits

The focused M7 suite exercises invalid-manifest preflight rejection, admission
issuance, dependency blocking, concurrency, integration proof, cancellation,
timeout/retry/reroute limits, serial fallback, scope ownership, route
quarantine, bounded/redacted envelopes, nested-run rejection, and proposal-only
M5/M6 integration. It runs alongside schema/contract checks, the complete
deterministic suite, and a repository-contained clean-room check. Exact commands
and results are recorded in
[`artifacts/m7-verification.json`](../artifacts/m7-verification.json).

The independent `GPT-5.5 xhigh` re-review found zero open material findings;
see [`artifacts/m7-final-security-review.md`](../artifacts/m7-final-security-review.md).
It verified remediations for file-scope containment, audit spoofing, manifest
containment, nested child-thread graphs, result/reference aliasing, transitive
artifact/acceptance evidence, and retry-reason spoofing.

The cancellation token protects the normal cooperative callback contract, but
in-process Python callbacks are not a hostile-code sandbox. Untrusted or live
workers require a future process-isolation and host-enforced termination
boundary. M7 also does not claim M8's numerical speed, context-reduction,
quality, route-shadow, or live-canary gates.

Requirement coverage is FR-03, FR-06, FR-17, FR-18, FR-19; NFR-01, NFR-03,
NFR-06, NFR-09, NFR-11; AC-03, AC-08, AC-09; ADR-002, ADR-003, and ADR-004.
Rollback reverts only M7-local runtime/schema/fixture/test/artifact and
documentation paths. It must preserve prior authority state, user files, and
global configuration; M7 made no global, provider, old-vault, or `ai-memory`
mutation.
