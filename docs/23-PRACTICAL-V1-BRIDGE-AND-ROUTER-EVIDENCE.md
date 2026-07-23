# Practical V1 Bridge and Router Evidence

Status: globally installed from exact user-approved packet; seven-target
readback and a local disposable canary pass. This is not M8 or strict M9
completion.

## What Practical V1 Means

The local verifier accepts only a small structured outbound record:

| Field | Purpose |
| --- | --- |
| `correlation_id` | binds one request across the plan and router log |
| `expected_profile` / `reported_profile` | compares the logical approved profile |
| `requested_effort` | records the caller's requested effort |
| `normalized_effort` | records the router's normalized value |
| `outbound_effort` | records the value sent on the outbound boundary |

The report is `operator-trusted-router-outbound-report`. It proves only that the
trusted router reported and sent those allowlisted values. It does not attest a
remote provider's internal model, hidden reasoning effort, processing, or
retention. The report always carries `provider_attestation: false` and
`strict_upstream_attestation_satisfied: false`. The existing signed upstream
evidence path in `src/second_brain/m9_external_evidence.py` remains the future
strict hardening route.

Only the synthetic fixtures under `fixtures/practical-v1/` are used locally.
No provider or network request is made.

## Bridge Contract

The bounded CLI is `scripts/practical_v1_bridge.py`. The staged global wrapper
is under `dist/global/practical-v1/` and refuses to run if the pinned source
tree digest changes.

| Operation | Behavior |
| --- | --- |
| `status` / `health` | Reads only explicitly named project/global store metadata |
| `project-recovery-read` | Reads one selected project through the M2 recovery API and an explicit project root |
| `global-knowledge-read` | Reads only the explicitly registered global store through the M4 retrieval API |
| `propose-write` | Writes a pending-review promotion proposal containing IDs/hashes only; never commits authority |
| `verify-router-log` | Verifies the bounded plan and outbound JSONL fixture; no network |

Memory operations default to `DIRECT`. In that lane they return
`DIRECT_NO_MEMORY` before validating roots or opening a store. `ASSISTED`,
`GRAPH`, or `DEEP` must be supplied explicitly for a read. Runtime roots,
runtime-relative store paths, project roots, and evidence roots reject lexical
symlinks before resolution. Runtime paths must be canonical absolute paths under
the private runtime root. A project root must be the exact canonical,
non-symlink root of a Git worktree (`.git` directory or validated worktree
file), never a broad parent discovered by scanning. Choosing which exact Git
root represents a `project_id` remains an explicit operator trust decision.

Recovery against a selected Git project outside this repository is supported.
Its freshness snapshot uses a deterministic `external:<sha256>` marker instead
of leaking the external absolute path; roots inside this repository retain the
existing repository-relative marker.

The bridge does not ingest transcripts, write durable memory automatically,
follow arbitrary paths, retain secrets, or call a provider. The only durable
operation is the existing review outbox boundary for a project-to-global
promotion proposal.

## Rollout Boundary

The exact packet
[artifacts/practical-v1-approval-packet.json](../artifacts/practical-v1-approval-packet.json)
with digest `abe806a22ad13f1611331d3552bf6787f6b90967b045a6b8a54be9c72c36be63`
was explicitly approved and applied to its seven listed targets only. The
[redacted apply report](../artifacts/practical-v1-apply-report.json) proves
post-apply target/source readback, preserved M9 surfaces, `DIRECT_NO_MEMORY`,
health, targeted reads, proposal-only authority preservation, and the local
operator-trusted router fixture. It makes no provider/network request and does
not claim provider attestation.

A later rollback is itself a new mutation and needs a fresh exact rollback
packet and approval. The retained `practical-v1-v1` backup is recovery material,
not authorization to run rollback or change the installed skill.

The current state is `PRACTICAL_V1_APPLIED_AND_CANARY_PASS`; it does not change
historical M8/M9 receipts or unblock strict live/provider gates.
