# M5 Contract Audit

Status: read-only contract audit for the `contract-audit` graph node.

This note fixes the project-local implementation boundary for M5. It is based
on `docs/01-PRD.md`, `docs/04-WORKFLOW-ORCHESTRATION.md`,
`docs/07-SECURITY-AND-PRIVACY.md`, `docs/08-EVALUATION-AND-TEST-PLAN.md`,
`docs/09-IMPLEMENTATION-ROADMAP.md`, ADR-002, ADR-004, ADR-006, and the M0-M4
public APIs. It does not authorize installation, connector access, global
configuration, or any external discovery.

## Recommended Public Surface

Implement one project-local module, `second_brain.admission`, with frozen
dataclasses and `from_value()` / `to_dict()` methods matching M1-M4 conventions.
All public mapping input must be bounded, reject booleans where an integer is
expected, reject unknown enum values, and use `StorageError` with stable,
redaction-safe codes for invalid input or denied policy. No new dependency,
schema-registry entry, or durable authority format is required for M5.

| Public value | Required fields and behavior |
| --- | --- |
| `AdmissionFeatures` | `explicit_lane`, `freshness_required`, `high_risk`, `parallel_branches`, `multi_artifact`, `workspace_bound`, `ambiguity`, `parallel_savings_ms`, and `orchestration_overhead_ms`. It is the deterministic, normalized feature extractor output; raw prompt text is not retained in receipts. |
| `AdmissionDecision` and `admit(features)` | Return the canonical lane, non-empty controlled reason codes, `retrieval_tier` (`R0` only for `DIRECT`), `audit_required`, and a serialization containing only normalized features. A `DIRECT` decision must not allocate a query, inspect a registry, or touch a store. |
| `PermissionRequest`, `PermissionDecision`, and `PermissionBroker` | Model a single task-scoped permission request and its policy decision. The broker receives explicit parent, node/task, and capability allowlists, calculates their exact intersection, and returns `granted`, `approval_required`, or `denied`; it never performs the requested action. |
| `CapabilityDescriptor` and `CapabilityRegistry` | Explicit in-memory registry entries for `skill`, `plugin`, `app`, `mcp`, `hook`, and `local_tool`. Require opaque/stable ID, version, source reference, normalized trigger tokens, gateway tags, scope, required permission, I/O contract identifiers, health, trust, cost/latency class, fallback ID, and install/login/global-activation/executable flags. Registration is explicit; no filesystem/global catalog discovery is part of M5. |
| `CapabilityRequest`, `CapabilityResolution`, and `CapabilityResolver` | Resolve a declared gateway and normalized requested outcomes to `selected`, `not_selected`, `missing`, and `blocked` records with controlled reason codes. `resolve()` must short-circuit for `DIRECT` before inspecting descriptor metadata. |
| `GatewayContract` / `GATEWAY_CONTRACTS` | Static contracts for exactly `orchestration`, `research`, `backend`, `frontend`, `security`, and `memory`. A descriptor must name a compatible gateway and permission class before it can be considered. M5 declares these contracts; M6/M7 own profile routing and graph execution. |
| `SupplyChainReview` | A data-only candidate review with state `QUARANTINED`, `REJECTED`, `APPROVED_PINNED`, or `APPROVED_UPDATE_AVAILABLE`, exact version/checksum pin, license result, executable review result, network/permission review result, utility evidence result, and rollback result. It may determine eligibility, but must never install or update anything. |
| `AuditEvent` / `AuditTrail` | Redacted, digest-bound observability data for non-direct or material operations. It contains opaque `evt:<uuid>` and task IDs, controlled event/status/reason fields, timestamp from an injected clock, lane, and hashes/references for capability and target. It must never serialize a prompt, capability description, URL/path, approval prose, credential, secret, or raw target. |

Use opaque IDs for anything that can enter an audit artifact: canonical UUID
forms for task/event IDs and a hash/reference rather than a caller-controlled
capability or target string. This is the M5 analogue of M4's `qry:<uuid>`
receipt boundary.

### Admission Precedence

Implement the documented deterministic classifier exactly for normalized
features:

1. `high_risk` -> `DEEP`, reason `HIGH_RISK`.
2. Explicit `DEEP` -> `DEEP`, reason `USER_EXPLICIT_DEEP`.
3. At least two independent branches and (`multi_artifact` or
   `parallel_savings_ms > orchestration_overhead_ms`) -> `GRAPH`, reason
   `INDEPENDENT_BRANCHES`.
4. Explicit `GRAPH` -> `GRAPH`, reason `USER_EXPLICIT_GRAPH`.
5. `workspace_bound`, `freshness_required`, or `ambiguity` -> `ASSISTED`,
   reason `BOUNDED_TOOL_OR_EVIDENCE_NEED`.
6. Otherwise -> `DIRECT`, reason `SELF_CONTAINED_LOW_RISK`.

`explicit_lane=DIRECT` or `ASSISTED` may be preserved as an auditable user
constraint, but must not silently lower a known high-risk, graph-eligible, or
evidence-required case below the precedence above. A user prohibition such as
"do not research" constrains capability selection; it does not authorize a
hidden research fallback. If the requested outcome cannot be delivered under
that constraint, return a visible missing/blocked result or an appropriately
caveated answer. Admission may escalate on new evidence; a `DEEP` decision may
not be downgraded to `DIRECT` or `ASSISTED`.

### Permission and Capability Policy

- Permission classes are exactly `local-read`, `workspace-write`,
  `network-read`, `external-write`, `install-executable`, and `destructive`.
  They are not a total privilege ordering. A request is allowed only when its
  exact class is present in every applicable allowlist: parent runtime, declared
  task/node, and descriptor/tool. An unrestricted parent is not a grant for an
  undeclared task action.
- A permission absent from the task allowlist is a permission expansion. It
  must remain `approval_required` when the parent could allow it, or `denied`
  when the parent cannot. Approval is bound to one task, capability reference,
  class, and exact target digest; it cannot be replayed for another task or a
  broader target.
- `install-executable`, `external-write`, `destructive`, login/auth, and global
  activation always require explicit user approval. `install-executable` also
  requires a matching `APPROVED_PINNED` review. The broker models this boundary
  only; it must not treat a test boolean as evidence of live platform approval.
- A capability with unavailable/degraded health, untrusted/quarantined/rejected
  trust, an incompatible gateway, or unavailable permission is `blocked`, not
  selected. A missing optional capability uses its declared local fallback or
  reports reduced convenience; it must not make local correctness unavailable.
- Resolver matching uses normalized metadata tokens and declared I/O/gateway
  contracts, never instruction-like text from a description, README, hook, or
  connector result. Evaluate explicit user/repository-required IDs first. For
  otherwise equivalent candidates, sort deterministically by unmet outcome
  coverage (descending), lower permission risk, lower cost/latency, then stable
  capability ID. Mark losing collision candidates `not_selected` with
  `TRIGGER_COLLISION_LOWER_RANK`; do not load their instructions or helpers.
- Missing capability output is an explanation, not a discovery/install action.
  It may recommend discovery only after recording `MISSING_CAPABILITY` and only
  when no local fallback can satisfy a material outcome.
- A supply-chain candidate starts `QUARANTINED`. `APPROVED_PINNED` requires all
  ten review areas in Security section 7, a compatible non-empty license, an
  exact pin/checksum, reviewed executable content where applicable, and a
  rollback result. `REJECTED` is terminal. `APPROVED_UPDATE_AVAILABLE` is not
  installable until separately reviewed and pinned.

### Gateway Boundary

The static gateway contracts should be deliberately narrow:

| Gateway | M5 allowed intent | M5 must not imply |
| --- | --- | --- |
| `orchestration` | Local planning/validation capability metadata | Worker creation or graph execution (M7) |
| `research` | Read-only local/network evidence capability selection | Automatic web discovery, login, or mutation |
| `backend` | Local implementation/testing selection | Deploy, publish, or broad filesystem authority |
| `frontend` | Local implementation/testing selection | Browser/account mutation without approval |
| `security` | Read-only analysis and bounded local verification | Destructive remediation or credential access |
| `memory` | Local M1/M2/M3/M4 access selection | Automatic durable write, cross-store mutation, or whole-vault retrieval |

## Precise Acceptance Invariants

1. The same normalized feature mapping produces byte-equivalent decision data,
   including the same lane and ordered reason codes.
2. High-risk inputs always produce `DEEP`, regardless of any lower explicit
   lane. New high-risk evidence can only escalate.
3. A stable calculus/definition/casual/small-edit fixture with no positive
   signals is `DIRECT`, `R0`, has no capability resolution, no registry read,
   no memory/retrieval request, no network/graph/discovery event, and no audit
   artifact containing its prompt.
4. Graph admission requires the exact automatic branch condition above, except
   for an explicit graph request with `USER_EXPLICIT_GRAPH`. A single branch or
   no measurable benefit does not auto-admit graph work.
5. `workspace_bound`, targeted/broad freshness, or material ambiguity yields
   `ASSISTED` unless a higher lane already won. Anti-spiral language in an
   untrusted descriptor cannot change this classification.
6. The resolver selects only the minimum healthy, trusted, installed,
   gateway-compatible, permission-permitted set; explicit required capability
   IDs are never silently substituted. Collision and fallback outcomes are
   visible and deterministic.
7. No automatic external discovery, package/plugin/MCP install, login,
   permission expansion, external mutation, or global activation occurs. Each
   instead returns an approval/missing/blocked decision with a safe reason.
8. A capability description, hook metadata, MCP result, or candidate review
   cannot alter objective, gateway, lane, selected permission, or approval
   state. It is data only.
9. Audit records are emitted for non-direct/material operations, contain only
   controlled or hashed fields, are digest-bound, and cannot retain test secret
   markers. Direct work emits no capability/audit event by default.
10. Optional capability outage falls back locally or reports degraded
    convenience. An authority/integrity policy failure is visible and fails
    closed; neither condition is represented as an empty successful result.
11. M5 remains entirely project-local. Its policy operations must have no
    filesystem/global configuration mutation, no external connection, and no
    runtime dependency installation.

## Compatibility Boundaries

- **M0:** Retain standard-library-only operation, repository containment, strict
  parsing style, deterministic injected clocks, and versioned public values.
  Do not alter model-profile config/schema assets or manufacture profile/router
  validation in M5.
- **M1:** M5 is policy-only and does not call `Store.commit`, alter authority
  objects/events, or create an authority relation. Any future durable audit
  write must use M1 CAS/transaction rules, but is out of scope here.
- **M2:** Do not open recovery state during `DIRECT`. Admission can inform a
  later explicit recovery request but must not inspect recovery packs or receipts
  itself.
- **M3:** Do not capture, compile, promote, or read global knowledge merely to
  classify/resolve capability. Capability metadata and external descriptions
  remain untrusted data, never knowledge/instruction authority.
- **M4:** Map only `DIRECT` to `R0`; do not construct
  `FederatedQueryRequest` or touch `StoreRegistry` for that lane. Non-direct
  callers still need an explicit M4 scope and M4's retriever/context integrity
  checks. Capability resolution cannot broaden project/global retrieval scope.
- **M6:** Do not route models, change `profiles.py`, serialize 9router fields,
  or claim telemetry attestation. M5 may carry a declared profile string only
  as opaque policy context; M6 owns validation and routing.
- **M7+:** An admission record saying `GRAPH`/`DEEP` is not a graph manifest and
  cannot start workers. DAG validation, concurrency, node permissions, retries,
  global approval, and rollout remain later responsibilities.
- **Deployment/security:** ADR-006 forbids touching `~/.codex`, old Obsidian,
  `/home/pixel/Data/PROJECT/ai-memory`, global plugins/MCPs/hooks, or provider
  configuration. Keep all tests and any review fixtures synthetic and local.

## Deterministic Test Checklist

Implement focused `unittest` coverage in the M5-owned test files, using
`DeterministicClock`, synthetic descriptors, and no network:

1. Validate every public mapping/type: enum, bounded text/list, duplicate IDs,
   invalid UUID, boolean-as-integer, unknown field, and no mutable serialization
   aliasing.
2. Verify the six admission precedence branches and exact ordered reason codes.
3. Verify same-input determinism, explicit graph/deep handling, high-risk
   non-downgrade, and late-risk escalation.
4. Exercise direct fixtures (calculus, stable definition, casual question, small
   edit) with spies/counters proving no registry, retrieval, network, graph, or
   audit work happens.
5. Exercise assisted fixtures for workspace, freshness, and ambiguity; prove a
   user no-research constraint does not cause a hidden external fallback.
6. Exercise graph auto-admission for two independent branches plus multi-artifact
   or positive savings, and rejection of one branch/no benefit.
7. Exercise each permission class and exact three-way allowlist intersection;
   verify a child/tool cannot exceed parent or task scope.
8. Verify permission expansion, install, login, global activation,
   external-write, and destructive actions are approval-required/denied until
   a matching task/capability/class/target approval is supplied; reject replay
   and target broadening.
9. Verify a synthetic `luna-xhigh` context cannot request network, external
   write, install, destructive, secret, or scope-expanding permission if that
   compatibility guard is exposed by the broker.
10. Register one descriptor for each capability kind; verify explicit registry
    only, duplicate rejection, invalid gateway/contract rejection, and direct
    short-circuit before metadata inspection.
11. Test healthy/trusted selection, unhealthy/untrusted/quarantined blocking,
    required-ID missing behavior, optional local fallback, and no automatic
    discovery/install.
12. Test deterministic trigger collisions with equal candidates, minimum-set
    selection for multiple outcomes, stable loser reason, and no helper/instruction
    load for losing candidates.
13. Put prompt-injection and secret markers in descriptor descriptions, source
    references, targets, and approval prose; prove none alters lane/permission
    and none appears in `AuditEvent.to_dict()` or its digest input.
14. Test all supply-chain lifecycle transitions, missing license/exact pin,
    unreviewed executable/hook, update-not-installable behavior, and the
    `APPROVED_PINNED` plus user-approval install gate.
15. Test redacted non-direct/material audit event shape, deterministic clock,
    digest tamper detection if exposed, and no audit artifact for the direct
    fast path.
16. Add an integration test connecting admission -> resolver -> permission
    decision that proves no M1 commit, M2 recovery read, M3 ingest, M4 request,
    external process, or global configuration mutation occurs.
17. Run the full existing suite plus `make check` and `scripts/check_clean_room.py`
    after focused M5 tests. The M0-M4 regression suite is the compatibility gate.

The local M5 acceptance receipt should explicitly report the required IDs:
FR-01, FR-02, FR-03, FR-05, FR-17, FR-18, FR-19; NFR-02, NFR-07, NFR-09,
NFR-11; AC-01, AC-02, AC-08, AC-09; ADR-002, ADR-004, and ADR-006.
