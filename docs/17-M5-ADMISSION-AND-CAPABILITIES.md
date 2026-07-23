# M5 Admission, Permissions, and Capability Resolver

M5 is the project-local implementation of Admission, Permissions, and
Capability Resolution from [the roadmap](09-IMPLEMENTATION-ROADMAP.md). It
keeps a routine request on the smallest safe path and models the boundaries that
a future execution host must enforce. It neither executes a capability nor
changes global Codex configuration.

Status: complete and locally verified on 2026-07-23. The exact local receipt is
[`artifacts/m5-verification.json`](../artifacts/m5-verification.json).

## Admission and Audit Boundary

The public M5 policy surface is `second_brain.admission`:

- `AdmissionFeatures` is a bounded, strict, deterministic feature contract for
  one task. Its reason codes classify work as `DIRECT`, `ASSISTED`, `GRAPH`, or
  `DEEP` without loading a capability registry or performing research.
- High-risk permissions, destructive work, and explicit risk flags always
  select `DEEP`. A requested lower lane cannot downgrade them. An explicit user
  request can escalate the lane or forbid research/capabilities, but cannot
  bypass a safety or permission gate.
- `escalate_admission()` only moves upward after newly discovered risk. The
  classifier has no retry-driven lane escalation, which prevents an admission
  spiral.
- `DIRECT` is bound to `R0` and produces no audit, resolver, capability, or
  research activity. A non-direct decision needs a canonical task ID and an
  `AuditTrail` record issued by the classifier itself; a caller-created digest-
  valid decision is not authority.
- `AuditEvent` uses a fixed allowlist of opaque IDs, closed reason codes,
  digests, status, and timestamp. It excludes the raw objective, descriptions,
  action summary, target, credentials, and arbitrary exception text.

## Permission Boundary

`PermissionBroker` is a policy decision point, not an executor. The closed
permission set is `local-read`, `workspace-write`, `network-read`,
`external-write`, `install-executable`, and `destructive`. A request can only
receive the intersection of parent runtime, task/node, and capability-declared
permission. Unknown classes, missing evidence, inactive tasks, and mismatched
action classes deny by default.

Local targets are logical `workspace:` or `project:` locators bound to a
broker-configured, canonical project root. The caller's claimed root cannot
authorize another directory; traversal, symlink escape, absolute paths,
mixed-case schemes, encoded separators, and ambiguous locators fail schema
validation. Network or material actions require an explicit allowlisted
external scheme, and a network capability cannot disguise an external target as
a local action.

Install, login, permission expansion, external mutation, global activation,
and destructive work require `DEEP` where applicable and stop at an auditable
approval boundary. `UserApproval` remains a shape/binding model only: M5 has no
trusted live-confirmation issuer, so every material action returns
`approval_required`, even when a caller provides an approval-shaped object. No
operation is executed.

## Capability Resolution

`CapabilityRegistry` holds explicit, installed descriptors only; it never
discovers, imports, scans, or activates an external capability. It validates the
six supported kinds (`skill`, `plugin`, `app`, `mcp`, `hook`, and `local_tool`)
and the six static gateways: orchestration, research, backend, frontend,
security, and memory.

`CapabilityResolver` performs metadata-first matching only for non-direct work.
It filters by exact required ID, gateway, trigger, scope, installed state,
health, trust, and permitted permission before deterministic minimal-cost
ranking. Equal material trigger matches are blocked rather than guessed.
Missing optional capabilities remain visible as degraded convenience; a required
capability never silently substitutes a broader one. Descriptions are untrusted
data and never affect the task objective, lane, permission, approval, or audit
payload.

Plugins, hooks, and executable descriptors cannot self-assert
`approved_pinned`. They remain blocked until a separate supply-chain and
execution contract provides authority. `Luna xhigh` is limited to local read
work by the broker.

## Supply-Chain Lifecycle

`SupplyChainLifecycle` is a process-shared, lock-protected, single-writer
ledger over the candidate UUID and immutable package identity. Its only states
are `QUARANTINED`, `REJECTED`, `APPROVED_PINNED`, and
`APPROVED_UPDATE_AVAILABLE`; a terminal rejection or update state cannot be
reset with a replacement object or a new candidate UUID for the same immutable
package.

Approval requires a compatible explicit license, immutable ref and checksum,
identity and maintenance review, executable/hook review, network and permission
review, security and utility evidence, and rollback evidence. `UNLICENSED`, a
mutable ref such as `main`, missing review evidence, or a changed package pin
fails closed. An approved package still cannot install in M5: it must later
pass the task-bound `DEEP` and live-host approval boundary.

## Verification and Rollback

The focused 38-test M5 suite covers direct no-op admission, deterministic
precedence, anti-spiral escalation, high-risk downgrade resistance, private
admission issuance, audit redaction, capability health/trust/scope filtering,
trigger collision blocking, optional fallback, target/root containment,
permission intersection, material approval fail-closed behavior, local/network
action binding, supply-chain lifecycle tampering, license/ref checks, and the
redacted assisted local flow.

`make check`, `python3 scripts/check_clean_room.py`, and validation of
`artifacts/m5-work-graph.json` pass. The final independent `GPT-5.5 xhigh`
read-only review has zero open material findings; its report is in
[`artifacts/m5-final-security-review.md`](../artifacts/m5-final-security-review.md).
Requirement coverage is recorded for FR-01, FR-02, FR-03, FR-05, FR-17,
FR-18, FR-19, NFR-02, NFR-07, NFR-09, NFR-11, AC-01, AC-02, AC-08, AC-09,
ADR-002, ADR-004, and ADR-006.

Rollback reverts only the M5 module, exports, M5 tests, receipts, and
documentation. It must not delete caller-created authority stores, derived
state owned by earlier milestones, or any global configuration. M5 persists no
live approval, installation, login, external mutation, or global activation.
