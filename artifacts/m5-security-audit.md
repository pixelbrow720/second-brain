# M5 Security Design Audit

Status: pre-implementation design review. This review is intentionally
read-only with respect to the M5 implementation and test scopes. No M5 runtime
surface existed at review time, so the entries below are acceptance invariants
and required regressions, not claims about a shipped defect.

## Scope and Threat Model

Reviewed against `docs/01-PRD.md` (FR-01/02/03/05/17/18/19, NFR-02/07/09/11,
AC-01/02/08/09), `docs/04-WORKFLOW-ORCHESTRATION.md`,
`docs/07-SECURITY-AND-PRIVACY.md`, `docs/08-EVALUATION-AND-TEST-PLAN.md`, and
ADR-002, ADR-004, and ADR-006.

M5 must remain a project-local policy implementation. It may model permissions,
capabilities, audit events, and candidate reviews, but it must not inspect or
mutate `~/.codex`, installed plugins, MCP registration, credentials, the old
vault, `ai-memory`, or an external service. Capability descriptions and all
adapter/tool output are untrusted data, never authority.

## Material Design Risks and Required Invariants

### P1 - Admission must fail closed on high-risk work and cannot be downgraded

`DIRECT` is the default only for a self-contained, low-risk request. A caller's
`DIRECT`, `ASSISTED`, or `GRAPH` preference must never lower a detected
high-risk action below `DEEP`. In particular, `install-executable` and
`destructive` requests require `DEEP`; an unknown or malformed risk signal must
not be interpreted as low risk. A request that merely discusses security or
production is not automatically high risk: the signal is the requested
operation, not a keyword in arbitrary content.

Required invariant:

- feature extraction is pure, deterministic, bounded, and returns a closed
  reason-code set;
- automatic admission uses the documented precedence: high risk, explicit
  `DEEP`, justified independent branches, explicit `GRAPH`, bounded work or
  evidence need, then `DIRECT`;
- a user may request an escalation, prohibit research, or request direct work,
  but no override can bypass a safety or permission gate;
- newly discovered side effect/risk produces a recorded escalation before any
  capability resolution or execution.

### P1 - The permission broker must use an intersection, not a capability claim

Effective action authority is the intersection of the verified parent runtime
permission, declared node permission, registered tool permission, and a
task-scoped user approval when one is required. It is not a union of requested
permissions, metadata, MCP side-effect annotations, or a candidate's own
description. Unknown classes and missing parent evidence must deny by default.

Required invariant:

- permission class is a closed enum: `local-read`, `workspace-write`,
  `network-read`, `external-write`, `install-executable`, and `destructive`;
- a grant is bound to the opaque task ID, capability ID, action/target digest,
  permission class, approval requirement, and expiry. It cannot be replayed by
  another task, candidate, target, or broader class;
- `install-executable`, login/auth boundary, permission expansion, external
  mutation, global activation, and destructive action stop for explicit user
  approval. `install-executable` and `destructive` also require `DEEP`;
- an external-write request must have an explicit target and task-scoped
  approval; a tool's misleading `read-only` annotation cannot weaken local
  policy;
- a denial never silently selects a broader fallback, retries as a different
  capability, or changes the lane.

### P1 - Untrusted capability metadata must not become instructions or authority

Registry matching must be metadata-first, but metadata is data. A capability
description that says "install me", "grant network", or "ignore policy" must
not affect objective, lane, selected permission, approval state, discovery, or
execution. The resolver may return that it is missing or blocked, with a
sanitized reason code, but must not interpret embedded instructions.

Required invariant:

- registry entries use canonical unique IDs and validate a bounded schema for
  kind, version/source, trigger, scope, required permission, I/O contract,
  health, cost/latency, trust, and fallback;
- only a healthy, policy-trusted, already-installed/local capability with a
  permitted minimum scope is eligible for automatic selection;
- disabled, quarantined, unreviewed, unhealthy, unknown, or insufficient-trust
  entries are never selected, including when explicitly named by the user;
- `DIRECT` performs no registry load, capability selection, external discovery,
  research, memory retrieval, plugin/MCP activation, or audit ceremony;
- trigger collisions have deterministic ordering for reporting but no ambiguous
  automatic activation. If ties remain material, return `blocked` or `missing`
  rather than guessing;
- fallback is independently policy-checked. An optional missing capability may
  reduce convenience only; a material capability gap must be explained and
  block or request a choice rather than silently lower correctness.

### P1 - Approval is a capability and target binding, not a sticky task flag

The common confused-deputy failure is turning one user approval into a generic
"approved" boolean. That permits an approved local tool or one plugin version
to perform a different external action, install a replacement, or activate a
global integration.

Required invariant:

- approval input must contain a concrete action summary, exact target(s),
  capability/version or candidate pin, requested class, rollback statement
  where applicable, and an explicit current-user decision;
- approval is checked immediately before the material operation and is invalid
  after expiry, task redirect/cancellation, capability version/pin change,
  target change, or permission expansion;
- no capability or candidate may manufacture an approval from its metadata,
  an audit event, or a prior approval for a different task;
- M5's local model of an approval must not invoke a real installer, login flow,
  global configuration write, or external mutation.

### P1 - Audit receipts need allowlist serialization and correlation safety

Non-direct or material operations need a redacted audit trail, but a receipt can
become an exfiltration path if it serializes caller-controlled IDs, raw
objective, exception text, capability descriptions, approval text, URLs with
tokens, or arbitrary metadata. Generic recursive redaction is not enough if
new fields are added later; receipt serialization should be an explicit
allowlist of safe fields.

Required invariant:

- generated audit/event IDs are opaque canonical IDs; caller-provided IDs that
  do not meet the grammar are rejected or replaced before serialization;
- receipt/event payloads contain only bounded safe fields such as opaque IDs,
  lane, closed reason codes, capability ID/version digest, permission class,
  status, timestamp, and redacted target/action digest;
- raw prompt/objective, full capability/candidate description, credential,
  cookie, authorization header, secret, stack trace, and arbitrary exception
  message are never retained; audit events are immutable after creation;
- expected events cover admission, selection/blocked selection, permission
  request/grant/deny, candidate state changes, and failure/degraded outcomes;
  normal `DIRECT` creates neither a retrieval/capability event nor a material
  audit receipt;
- audit creation failure fails visible for a material action. It must not allow
  the action to proceed without the required evidence.

### P1 - Supply-chain lifecycle must bind approval to one immutable pin

The candidate state alone cannot authorize installation. A review for version A
must not accidentally bless version B, an update channel, a hook added after
review, or a capability with a changed endpoint/permission. The lifecycle must
model the candidate as reviewed data and preserve enough evidence to explain a
block without storing secrets.

Required invariant:

- the only states are `REJECTED`, `QUARANTINED`, `APPROVED_PINNED`, and
  `APPROVED_UPDATE_AVAILABLE`; invalid transitions and unknown states fail
  closed;
- `APPROVED_PINNED` requires all gate evidence: identity/provenance,
  maintenance, explicit compatible license, exact version/tag/commit plus
  checksum or equivalent immutable digest, executable/hook review, network
  endpoints, permissions, security posture, utility evidence, and rollback;
- a missing license, unreviewed executable/helper/hook, unknown network or
  permission boundary, mutable `latest` reference, or incomplete pin cannot
  become `APPROVED_PINNED`;
- any reviewed identity, version/tag/commit, digest, dependency tree,
  executable inventory, endpoint, or required permission change invalidates
  the prior approval and yields a new quarantined/update-available candidate;
- `APPROVED_UPDATE_AVAILABLE` never auto-updates. Installing an
  `APPROVED_PINNED` candidate still requires the separate task-scoped user
  approval and M5 must only model that boundary locally;
- rollback/disable and credential-revocation instructions are review evidence,
  not executable instructions from the candidate.

### P2 - Gateway contracts must narrow, not broaden, capability access

The six gateway names (`orchestration`, `research`, `backend`, `frontend`,
`security`, and `memory`) are routing metadata, not a permission grant. Unknown
gateway names, a capability belonging to multiple conflicting gateways, or a
gateway/capability scope mismatch must be blocked. Selecting `research` cannot
implicitly enable a network connector, and selecting `memory` cannot authorize
a durable write.

### P2 - Data and path boundaries remain local and bounded

All test candidates, registry records, and audit artifacts must be synthetic and
project-local. Reject path traversal, absolute paths, NULs, symlink escapes, and
unbounded strings wherever local artifact paths or locators exist. Candidate
metadata must not cause a filesystem scan, command execution, import, network
request, or access to global configuration during matching or review.

## Required Regression Tests Before Acceptance

The implementation may choose different public type names, but it must provide
deterministic equivalent tests in the M5 test scopes. Each test should assert
both the returned decision and the absence/presence of an auditable safe event.

| Area | Regression fixture | Required assertion |
| --- | --- | --- |
| Admission | Stable calculus, definition, greeting, and a one-file low-risk edit | `DIRECT`, deterministic self-contained reason, no resolver/research/registry call, no material receipt. |
| Admission | "Review this security policy" versus "delete the production data" | The first is not elevated solely by topic words; the destructive request is `DEEP`. |
| Admission | User says `DIRECT` for a destructive or install request | Decision remains `DEEP`, records both the risk reason and non-bypassing user preference. |
| Admission | Same normalized intake twice, including explicit `GRAPH` and unknown fields | Same lane/reason codes; malformed/unknown fields reject rather than creating a permissive default. |
| Admission | Risk discovered after initial `DIRECT` classification | Escalation occurs before capability lookup/execution and records a bounded reason. |
| Permission | Parent is `local-read`, node/tool asks `workspace-write` or `network-read` | Denied; the result is the intersection, not the requested maximum. |
| Permission | A tool labels an external mutation `read-only` | Still requires local `external-write` policy, target, and user approval. |
| Permission | Approval for task A/candidate pin A/target A is replayed for task B, pin B, target B, broader permission, expired approval, or redirected task | Every replay is denied and emits only a redacted denial event. |
| Permission | `install-executable`, destructive, global activation, login, permission expansion, and external mutation without approval | Blocked at the user boundary; install/destructive are also rejected below `DEEP`; no side-effect adapter is invoked. |
| Resolver | A `DIRECT` task with an instrumented registry/resolver | Registry and capability providers are never accessed. |
| Resolver | Two healthy same-rank capabilities share a trigger | No automatic activation; deterministic collision/blocked explanation identifies safe IDs only. |
| Resolver | Quarantined, unhealthy, untrusted, disabled, or permission-ineligible capability explicitly requested by user | Never selected; result is blocked with health/trust/permission reason, not an unsafe fallback. |
| Resolver | Required local capability plus an optional absent plugin/MCP | Local capability is selected; optional absence is visible only when material and local correctness remains intact. |
| Resolver | Malicious description containing a secret marker and directives to install/grant permissions | It remains data: no lane/scope/permission/discovery change, no command/network call, and marker is absent from all receipts. |
| Gateway | Unknown gateway or gateway/capability permission mismatch | Reject before selection; no capability activation. |
| Audit | Put distinct `SB_AUDIT_SECRET_MARKER` values in task ID, objective, reason text, candidate description, URL query, approval text, and exception message | Serialized event/receipt JSON contains none of the markers or raw fields; opaque IDs and safe digests remain traceable. |
| Audit | Valid non-direct admission, selection, permission denial, candidate transition, and audit writer failure | Correct redacted event classes occur; writer failure blocks the material action visibly. |
| Candidate | Missing license; mutable version; missing checksum; unknown endpoint; unreviewed hook/helper | Cannot enter `APPROVED_PINNED`; result is rejected/quarantined with a closed reason. |
| Candidate | Fully reviewed pinned candidate followed by changed tag, commit, digest, dependency, hook, endpoint, or permission | Old approval no longer authorizes it; a new candidate/review is required and no auto-update occurs. |
| Candidate | `APPROVED_PINNED` candidate with no user approval, then with an approval for a different pin | No install action is authorized in either case; only the exact current pin plus a valid task approval can model approval readiness. |
| Local boundary | Traversal, absolute, NUL, and symlink candidate/artifact locators | Rejected before I/O; no filesystem access outside a project-local synthetic fixture. |

## Acceptance Review Checklist

Before M5 is marked complete, an independent reviewer should verify all of the
following from tests and receipts:

- `DIRECT` has no capability/research activation and no false safety downgrade.
- Every material action has a lane, reason, minimum permission, exact target,
  user-bound approval disposition, and redacted audit evidence.
- No untrusted description or tool annotation expands scope, permission, or
  objective.
- Candidate approval is immutable-pin-specific, update-safe, and does not cause
  installation or global configuration mutation.
- Capability outage/missing optional capability is explicit and does not make
  direct/local correctness unavailable.
- No test, receipt, artifact, or fixture contains a secret marker, raw prompt,
  raw transcript, credential, or external-system access.

## Review Conclusion

M5 is feasible as a project-local deterministic policy layer if the invariants
above are treated as blocking contracts. The primary acceptance risks are a
confused-deputy permission union, capability metadata treated as instruction,
approval replay across a changed target/pin, and audit serialization leaking
caller-controlled text. These should be addressed with test-first contracts
before any global deployment work.
