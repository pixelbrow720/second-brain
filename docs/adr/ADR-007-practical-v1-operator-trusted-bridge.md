# ADR-007: Practical V1 Operator-Trusted Bridge

- Status: Accepted for project-local staging; global activation pending a new exact approval
- Date: 2026-07-23

## Context

The first M9 shadow proved transport and read-only behavior, but the local Codex
event stream did not contain authenticated 9router/provider telemetry. Calling
that shadow "provider attestation" would be false. At the same time, the
policy-only global skill cannot yet reach the project-local runtime, so a small
cross-project adapter is needed before a useful opt-in can be considered.

## Decision

Adopt a bounded Practical V1 path for local readiness:

1. Treat an operator-trusted structured 9router log as evidence only when each
   outbound record is bound to a correlation ID, expected logical profile,
   requested effort, normalized effort, and outbound effort. The verifier uses
   an allowlist and stores hashes/digests rather than prompts or responses.
2. Describe the resulting claim narrowly: it proves what the trusted router
   reports and sends at its outbound boundary. It does **not** prove the remote
   provider's internal model, reasoning effort, processing, or retention.
3. Keep the existing signed upstream evidence path as strict future hardening.
   Practical V1 never changes `live_attested` to true and never promotes a
   route on router-log evidence alone.
4. Expose a stable project-local bridge with explicit canonical non-symlink
   runtime-relative store paths and an explicit exact Git-worktree project root.
   `DIRECT` returns before root/store access; assisted reads use the existing
   M2/M4 contracts; durable output is limited to a pending project-to-global
   promotion proposal. Authority stores are never committed by the bridge.
   Project roots outside this source repository receive a deterministic redacted
   freshness marker rather than an absolute-path disclosure.
5. Stage, test, and packet the future global skill/wrapper separately. No global
   file is changed by this decision.

## Residual Risks

- The router and its structured log remain operator-trusted and could be wrong,
  incomplete, replayed, or tampered with before verification.
- Mapping a `project_id` to an explicitly supplied exact Git root is a trust
  decision. The adapter rejects traversal, broad/non-Git roots, lexical
  symlinks, and unbounded files but cannot identify a malicious
  operator-selected project.
- Upstream provider retention and internal routing remain unverifiable from
  local configuration.
- The bridge returns selected evidence to the caller; task-level retention and
  display remain outside this local adapter.
- Strict M8/M9 semantic, provider-task performance, production-scale context,
  live route, canary, soak, and global rollback gates remain open.

## Consequences

Practical V1 makes a future opt-in useful without relabeling synthetic or
operator evidence as live attestation. It adds a small local integration and a
new exact approval packet, while preserving rollback, proposal review, the
separate authority domains, and the stricter signed-evidence roadmap.
