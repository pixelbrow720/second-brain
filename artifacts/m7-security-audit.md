# M7 Security Audit

Status: pre-implementation, read-only adversarial design audit for the M7
`security-audit` graph node. This is a local contract for a bounded scheduler;
it is not evidence of a live worker, user approval, router attestation, or
global deployment.

## Scope and Evidence Boundary

Reviewed `artifacts/m7-work-graph.json`, the M7 roadmap and workflow contract,
the M5 admission/permission API in `src/second_brain/admission.py`, the M6
route boundary in `src/second_brain/routing.py`, local containment helpers in
`src/second_brain/workspace.py`, and the existing M1 storage safety model.
No implementation path, global configuration, old vault, `ai-memory`, network
endpoint, provider, or external capability was accessed or changed.

M7 may add a deterministic project-local manifest validator, in-process
scheduler, bounded envelopes, synthetic executor fixtures, redacted audit
records, and project-local compaction artifacts. It must not become a worker
host with ambient filesystem, shell, network, install, connector, credential,
or global-configuration authority. A graph manifest, callback, serializable
permission value, or synthetic M6 route receipt is untrusted input until the
runtime validates and binds it.

The important M5/M6 seam is intentionally conservative:

- A non-`DIRECT` `AdmissionDecision` is authoritative only when it is valid,
  task/lane-bound, and was issued by the supplied `AuditTrail`; reconstructing
  an equal-looking dataclass from JSON is not equivalent to
  `AuditTrail.has_admission(decision)`.
- `PermissionBroker` is a policy decision function, not an execution host.
  Do not accept a manifest boolean, caller-minted `PermissionDecision`, or
  `UserApproval.for_request()` draft as permission to perform an action. At a
  real action boundary, construct and evaluate an exact M5 request in-process
  and retain its matching audit event. M7 itself remains synthetic/local.
- Every current M6 `RouteReceipt` is `synthetic-local` and
  `live_attested == false`. `side_effect_allowed()` deliberately always returns
  `False`. M7 must not wrap, subclass, reinterpret, or replace it with a
  "trusted" flag. A synthetic route `MATCH` is diagnostic metadata only and
  cannot authorize a workspace commit, durable write, install, publish, or
  external mutation.

## Required Fail-Closed Invariants

### 1. Manifest and node validation happen before any worker exists

1. Parse untrusted JSON with the existing strict parser and validate against a
   versioned `work-graph-v1` schema. Reject unknown fields at every level,
   duplicate node IDs, duplicate scope entries, non-string/object coercions,
   non-finite numeric values, booleans in integer fields, oversized text/list
   values, and unsupported schema versions. Semantic validation must run after
   schema validation and before creating an executor, thread, task, or route.
2. Accept only `GRAPH` or `DEEP` manifests. `DIRECT` and `ASSISTED` must have
   no graph-runtime entry point, including a serial-fallback shortcut. The
   supplied M5 decision must verify, have the same task ID, lane, and decision
   digest as the manifest, and be present in the exact injected `AuditTrail`.
   Missing audit evidence, an escalation downgrade, a task mismatch, or an
   audit failure blocks startup.
3. A graph is bounded statically: at most 12 nodes, at most four concurrent
   nodes, depth exactly one, and an explicit final integrator/join. Reject
   children/subgraphs, dynamic-node fields, a worker-provided node list, and
   any manifest that has no genuine graph structure. For `GRAPH`, retain the
   admitted independent-branch/benefit evidence rather than treating an
   arbitrary user string as proof. A `DEEP` graph still needs a bounded graph
   justification; risk alone does not permit unbounded fan-out.
4. Every node must have a bounded ID/objective, a closed node kind, one approved
   M6 profile alias, explicit `permission_class`, declared read/write scopes,
   acceptance checks, timeout in 30--900 seconds, and `max_attempts` of one or
   two. There is no inherited profile, timeout, capability, permission, scope,
   retry budget, or acceptance rule. Resolve profile aliases through the frozen
   approved registry; reject aliases, roles, or registry snapshots that drift.
5. Dependencies must name existing distinct nodes, exclude self-dependencies,
   have no cycle, and be the only way a node can receive another node's result.
   The final integrator must transitively depend on every non-final terminal
   producer; a multi-writer graph cannot have a partial or optional implicit
   merge. A reviewer is not an integrator merely because its task text says
   "review".
6. A `DEEP` graph needs a terminal, read-only, independently profiled reviewer
   after the material integration path. It has empty write scope, cannot be a
   builder or final writer, and must be distinct from the profile it reviews.
   Prefer the approved `gpt55-xhigh` reviewer role; do not let a builder mark
   itself reviewed by changing a free-form string. If the required independent
   review cannot be formed, reject the graph rather than silently downgrading
   its risk treatment.

### 2. State is runtime-owned, monotonic, and non-forgeable

1. Keep graph/node lifecycle state private to one scheduler owner. Use a closed
   transition table, immutable/deep-copied snapshots, and a lock around every
   claim/transition. Do not deserialize an externally supplied "running" or
   "succeeded" snapshot into live state, expose a mutable node map, or trust a
   callback to set its own status.
2. A safe minimum graph machine is
   `NEW -> VALIDATED -> RUNNING -> VERIFYING -> CLOSED`, with terminal
   `FAILED`, `BLOCKED_POLICY`, and `CANCELLED_USER_REDIRECT` branches. A safe
   node machine is `PENDING -> RUNNING -> SUCCEEDED|FAILED|BLOCKED|CANCELLED`.
   `CANCELLING`/`ABANDONED` may be used for an active worker, but no terminal
   state can return to running or succeed later. Use a per-attempt nonce so a
   late result cannot be applied to a retried or cancelled node.
3. Only a `PENDING` node whose every declared dependency is `SUCCEEDED` may be
   atomically claimed. A failed, blocked, timed-out, route-quarantined, or
   cancelled dependency blocks every descendant before dispatch; it never
   yields an empty context or a partial merge. Final success requires successful
   validation, all required dependencies, final integration, and required
   review--not just that the worker queue became empty.
4. State/audit receipts must bind the manifest digest, M5 admission digest,
   graph ID/task ID, node ID, attempt number, route receipt digest when present,
   and terminal reason code. Recompute a digest from an allowlisted payload;
   reject mutable input aliasing and event-ID collision. A state report is
   evidence for inspection, never an API that can re-open or authorize a run.

### 3. Scheduler, ownership, and serial fallback cannot bypass the graph

1. The scheduler must atomically reserve capacity before dispatch and retain
   that reservation until the actual worker settles. It must never have more
   than four active workers, dispatch a node twice, or return while background
   workers can publish a result. A worker receives an immutable `NodeContext`,
   not the runtime/scheduler, a graph builder, a thread factory, or a spawn
   callback; nested graph execution is rejected while a graph is active.
2. Treat executor return values and exceptions as untrusted data. Validate a
   typed result envelope before changing state. An exception message, traceback,
   callback-owned status, or custom object must not become a success result or
   unbounded audit field.
3. Serial fallback is only a scheduling mode for the already validated graph.
   It preserves node IDs, dependencies, profiles, route/permission gates,
   scopes, attempt limits, context limits, and acceptance checks. It may not add
   a node, merge failed output, change a profile/capability/permission, convert
   an invalid graph into direct execution, or turn a route/security/policy
   failure into a serial retry. Record an allowlisted fallback reason.
4. The runtime must not infer authority from a node's declared permission. If a
   synthetic executor reports a change, retain it as a proposed result only.
   A future durable write still goes through the M1 writer's CAS, lock, atomic
   journal/event/manifest path and a separate action-bound M5 decision. Graph
   scope ownership is not a replacement for those controls.

### 4. Cancellation and timeout win every race

1. A user redirect atomically prevents further claims, marks all pending nodes
   cancelled, signals active workers through a read-only cancellation token, and
   records a redacted cancellation event. Check that token before dispatch,
   before a retry/reroute, before result publication, and at every synthetic
   safe boundary.
2. An active writer must stop at a safe boundary. If it returns after
   cancellation or timeout, quarantine/abandon its result; do not consume it,
   mark it successful, start descendants, or commit a proposed change. A
   cancellation race always resolves to cancellation, never completion.
3. Measure timeout with runtime-controlled monotonic time. A callback-supplied
   timestamp cannot reset a budget. Timeout is a terminal failure or a single
   closed-class transient retry; it cannot leave an orphan reservation or make
   the scheduler exceed four workers.
4. Cancellation, permission denial, scope/path denial, result-envelope failure,
   secret/redaction failure, dependency failure, and route integrity failure are
   never retryable. They remain visible in the final graph receipt.

### 5. Retry and reroute are bounded evidence transitions

1. Count the initial run plus every retry/reroute against `max_attempts`; a
   node with a two-attempt budget cannot receive a transient retry and then a
   third "escalation" attempt. Retry only once for a closed transient set such
   as timeout/provider-5xx/temporary-MCP failure, with an auditable reason and
   no side effect started.
2. A reroute is one evidence-based, pre-side-effect transition, not a fallback
   string. It must preserve node ID, task, scope, permission, acceptance,
   capability contract, and bounded context; choose an explicit approved M6
   profile using the documented escalation policy and record both route intents.
   No silent effort downgrade or profile inheritance is allowed.
3. M6 mismatch/unsupported mapping behavior remains M6 behavior: only its
   pinned, pre-side-effect `retry_allowed()` case may be attempted once. Missing
   or ambiguous telemetry is never retry-to-success. Since all current M6
   receipts are synthetic, neither a retry nor a synthetic `MATCH` permits a
   material side effect in M7.

### 6. Scope and write ownership are canonical and checked again at use time

1. Normalize every scope against the repository root using the project-local
   containment model. Reject absolute paths, empty/root scopes, `.`/`..`, `~`,
   NUL/control characters, backslash ambiguity, URI/file schemes, globs,
   environment interpolation, unresolved variables, and paths outside the
   repository. Resolve existing components and reject symlinks/hard links that
   escape. Re-check containment immediately before any file/artifact access to
   close validation-to-use races.
2. Compare scopes by canonical path segments, not prefix strings. Thus `src/a`
   overlaps `src/a/x.py`, and `src/a` does not accidentally match `src/ab`.
   Reject any write/write ancestor, descendant, or equal overlap across nodes
   before a worker starts. Duplicate/overlapping scopes inside one node should
   also be rejected or canonicalized deterministically--never left ambiguous.
   Read/read overlap is harmless; read/write access does not grant write
   ownership to the reader.
3. A result's declared changes/artifacts must be a strict subset of the current
   node's validated scope and must carry a safe locator plus content digest.
   A read-only reviewer has no changes and no write scope. Retry/reroute retains
   the same owner; a callback may not claim a sibling's path, an unowned derived
   directory, or a new scope discovered from output text.
4. Runtime-owned compaction/audit files use one fixed repository-contained
   managed root, atomic safe writes, and a manifest/digest. They are not a
   loophole to write arbitrary `artifacts/`, `.codex/`, a home directory, or a
   user-owned file. Preserve unexpected/foreign files rather than overwriting
   or deleting them.

### 7. Context, result, compaction, and observability are allowlisted

1. `NodeContext` contains only the normalized node contract and bounded,
   declared-dependency summaries/artifact references. It does not include the
   whole root transcript, environment, credentials, arbitrary manifest fields,
   other workers' logs, raw exception strings, or implicit memory retrieval.
   A node can access only declared dependency inputs; result text cannot add a
   source, capability, tool, path, or node.
2. Define strict, typed, deep-copied envelopes with exact keys. Worker output is
   capped at 2,000 tokens (or a deterministic conservative byte equivalent);
   join/reviewer output is capped at 4,000. Enforce item counts and per-string
   bounds before it enters state. Oversize output is rejected or reduced to a
   separately validated, hashed, project-local compaction artifact--not silently
   truncated into a false success.
3. Envelopes and graph events use an allowlist of IDs/digests, closed statuses
   and reason codes, timestamps, bounded numeric latency/cost/token fields,
   safe profile aliases, and safe artifact metadata. Do not serialize
   `__dict__`, `asdict()` of arbitrary objects, exception text, raw task/goal,
   prompt, response, transcript, environment, headers, URL/query, cookie,
   credential, private key, chain-of-thought, or recursive callback payload.
4. Scan/redact before result, compaction, and event persistence. Unknown data
   class is treated conservatively; a secret/instruction-like marker blocks or
   quarantines persistence instead of being copied into a log. Compaction logs
   are data, never executable instructions, and their hash is verified before a
   later context read. Emit enough metadata to explain lane/profile/state,
   latency/cost, route disposition, fallback, cancellation, and artifact digest
   without retaining bodies.
5. Non-direct observability is fail-visible. If graph validation/admission audit
   issuance or required graph event recording fails, do not start or continue
   workers. Audit failures must not be caught and converted to a successful
   local run. Direct work remains graph/audit-free as defined by M5.

### 8. M5 permission and M6 route binding remain denial boundaries

1. Bind graph start to the exact M5 `AdmissionDecision` and injected
   `AuditTrail`; do not accept a `decision_digest` copied only into the
   manifest. Preserve user prohibitions from the admission features (research,
   capabilities, graph, durable write) and never let a node override them.
2. If a node is ever allowed to invoke a capability/action, construct an exact
   `PermissionRequest` at dispatch from the frozen node and host-bound project
   root, call `PermissionBroker.decide()`, and require a matching live in-memory
   audit event plus `granted` status. The effective permission must be the M5
   intersection of parent, node, descriptor/material-authority, target, lane,
   and task-active state. Do not treat a manifest permission, approval draft,
   descriptor description, or cached/caller-created decision as authority.
3. M7's project-local synthetic implementation must deny external-write,
   install-executable, destructive, login, global-activation, and permission
   expansion classes. Luna remains local-read-only and cannot own planning,
   security review, scope changes, side effects, or final acceptance.
4. Each dispatched node has an immutable M6 `RouteIntent` bound to the frozen
   alias/task/node snapshot. A serialized route/receipt may be stored as
   redacted diagnostics. Before any future material action, call the existing
   M6 denial-only side-effect boundary; its false result blocks the action.
   Missing, ambiguous, mismatched, unsupported, forged, subclassed, or merely
   synthetic `MATCH` route evidence always quarantines a material result.

### 9. Local-only containment is a hard property

1. The implementation may use only repository-contained schemas, fixtures,
   source, and a fixed local artifact root. It must not consult `Path.home()`,
   global Codex/9router settings, old-vault/`ai-memory` paths, environment dumps,
   provider endpoints, or installed package metadata as graph input.
2. No M7 production path invokes a shell, subprocess, network client,
   connector/MCP mutation, package installer, browser, global staging writer,
   or filesystem delete. Synthetic test executors must be explicit injected
   callbacks and should use temporary repository-contained fixtures only.
3. The runtime never writes `~/.codex`, global `AGENTS.md`, global skills,
   plugins, hooks, MCP registrations, model-provider configuration, or any path
   outside the repository. There is no global activation flag or fallback
   destination. A containment failure is a policy block, not an alternate path.

## Required Regression Contract

The implementation should add deterministic tests for the following cases. No
case may require a real model, worker host, credential, router, global config,
network request, old vault, or `ai-memory` data.

| Area | Required adversarial regressions |
| --- | --- |
| Schema and graph admission | Reject unknown fields, duplicate IDs/scopes, invalid version/lane/task binding, `DIRECT`/`ASSISTED`, missing/unissued/tampered M5 admission, cycle, self/missing dependency, no final join, node >12, concurrency >4, depth >1, timeout 29/901, boolean/non-integer timeout/attempt, attempt 0/3, unknown profile, missing permission/acceptance, and no independent-review contract for `DEEP`. Assert no executor is called for every invalid case. |
| Lifecycle and scheduler | Prove only dependencies in `SUCCEEDED` become ready; failed/blocked/timed-out/route-quarantined parents block descendants; exactly one atomic claim per node; active count never exceeds four; final integration waits for all required paths; duplicate/late/forged result and mutable input mutation cannot alter state; direct cannot enter the scheduler; worker cannot spawn a nested graph. |
| Cancellation and timeout | Cancel before start, while a reader runs, while a synthetic writer is at a safe boundary, and during a retry. Assert pending nodes never start, active late success is abandoned, descendants never receive it, terminal state remains cancelled, reservations drain, and no new action begins. Assert callback timestamps cannot bypass timeout and timeout has no orphan/late publish path. |
| Retry, reroute, fallback | Permit exactly one classified transient retry within the two-attempt cap; reject generic failure, validation/security/permission/scope/cancel failure, post-side-effect retry, third attempt, silent profile change, and retry after M6 missing/ambiguous telemetry. Verify M6 mismatch retry is pre-side-effect/pinned only. Verify serial fallback reuses the same DAG/scope/profile/permission/context and cannot make a blocked graph pass. |
| Scope and ownership | Reject absolute, traversal, encoded/path-segment trick, prefix collision, glob, variable, file URI, symlink escape, root scope, duplicate and ancestor/descendant write scopes. Assert reviewers cannot report changes; a worker cannot report a path outside its scope; a retry retains owner; result artifact paths remain under the managed local root; and a re-check catches a symlink introduced after validation. |
| Context/result/redaction | Feed prompt/secret/cookie/header/URL/query/environment/traceback/instruction markers through manifest, callback result, exception, artifact, and compaction paths. Assert they are absent from context, result receipt, event, digest input, and persisted artifact. Reject unknown/nested result fields, oversized lists/text, wrong node/attempt ID, unbounded numeric cost/token fields, unsafe locator, bad digest, and callback object serialization. Verify dependency context contains only declared bounded summaries/references. |
| M5 and M6 seams | Reject caller-minted `AdmissionDecision`, mismatched task/lane/digest, missing trail receipt, manifest `permission_allowed`, caller-minted permission result, approval draft, wrong descriptor/root/parent-node intersection, inactive task, Luna write/security request, and all high-risk classes. Parameterize every M6 reconciliation status plus `None`, hostile object, and spoofed receipt subclass: all material actions remain denied, including synthetic `MATCH`. |
| Local boundary and observability | Monkeypatch/spies must show no home/global config/network/subprocess/global staging access. Reject outside-repository output roots. Verify an audit sink failure prevents start/continues as a visible block. Assert event/compaction exact allowlists, deterministic digest, collision rejection, bounded metric accounting, and preservation of unrelated user files. |

## Implementation Gate

Treat the following as P1 blockers for M7 acceptance:

1. any invalid manifest can invoke an executor;
2. a downstream/final node can run after a required dependency fails or cancels;
3. cancellation, timeout, retry, serial fallback, or a late result can publish
   an unowned/material result;
4. write overlap, scope escape, symlink race, or out-of-scope result is accepted;
5. unbounded/raw context, result, exception, telemetry, or compaction data is
   persisted or fed to another node;
6. a manifest/caller-controlled permission or M6 synthetic receipt authorizes a
   side effect; or
7. any graph path reaches global configuration, an external endpoint, old-vault
   data, `ai-memory`, or a path outside this repository.

## Conclusion

M7 is safe to implement only as a fail-closed, project-local coordination model.
The core security property is not merely that the scheduler is bounded: its
manifest, state, context, permission, route, and file boundaries must remain
authoritative at the moment work is claimed and again at the moment a result is
accepted. M5 remains the policy authority, M6 remains a diagnostic/denial-only
route boundary, and M1 remains the only durable mutation authority. Until a
separate live host and deployment approval contract exists, all M7 worker output
must remain synthetic/local, redacted, bounded, and non-authorizing.
