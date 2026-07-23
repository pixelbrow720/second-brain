# M8/M9 External Evidence Runbook

## Practical V1 Relationship

ADR-007 adds a separate operator-trusted Practical V1 path documented in
`docs/23-PRACTICAL-V1-BRIDGE-AND-ROUTER-EVIDENCE.md`. Its structured 9router
outbound logs prove only what the trusted router reports/sends. They do not
satisfy this runbook's signed upstream/provider attestation gate, do not attest
provider internals, and do not alter the existing strict evidence types or
historical receipts below.

Status: the Practical V1 bridge was globally installed after exact approval and
passed its no-network disposable canary on 2026-07-23. No external evidence,
provider request, personal canary, soak, or global rollback has been started by
this runbook or its scripts.

This document separates the remaining work into tooling that is ready and work
that necessarily needs a user, upstream router/provider, or real workload. It
does not reinterpret the completed 50-request M9 shadow as authenticated route
evidence.

## Current Checkpoint

- `artifacts/m9-initial-shadow-report.json` proves 50/50 `PUBLIC` read-only
  response contracts with zero observed tools or mutations.
- It remains `MISSING_TELEMETRY` and `live_attested: false`: the current Codex
  CLI event stream did not expose authenticated per-request correlations.
- `artifacts/m8-m9-readiness-v2.json` remains `BLOCKED`; verify it before any
  proposal with `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3
  scripts/verify_m8_m9_readiness.py`.
- The first M9 packet is consumed. Do not run its `preflight` or `apply` again.

## Ready Local Tooling

| Tool | Safe behavior | Does not do |
| --- | --- | --- |
| `scripts/prepare_m9_external_trust_policy.py` | Pins only the SHA-256 of an explicitly supplied public OpenSSH allowed-signers file. | Copy the anchor, read a private key, call a provider, or write global state. |
| `scripts/prepare_m9_route_approval_intent.py` | Binds the current applied M9 packet and initial shadow receipt into a non-authorizing route-batch scope intent. | Start a provider request, create a user approval, or mutate global state. |
| `scripts/prepare_m9_route_attestation_plan.py` | Converts an explicit 50-500-request transient manifest into an immutable plan containing only correlation/model digests, bound to a route-approval intent. | Retain raw correlations, prompt text, or model identifiers. |
| `scripts/prepare_m9_route_approval_packet.py` | Binds an intent, plan, and pinned trust policy into one exact redacted packet for fresh user approval. | Approve the packet, start a provider request, or promote M8/M9. |
| `scripts/preflight_m9_route_approval.py` | Validates the new final approval reference, pinned public anchor, intent/plan/policy packet binding, active registry, and current M9 post-state. | Start a provider request, invoke a signature verifier, write an artifact, mutate global state, or promote M8/M9. |
| `scripts/prepare_m9_rollback_approval_packet.py` | Builds a fresh redacted rollback packet from the exact applied post-state and retained backup. | Execute rollback, approve it, call a provider, or alter global state. |
| `scripts/prepare_m9_blinded_review_plan.py` | Converts a redacted `NOT_RUN` A/B packet into case-reference-only review intake. | Reveal or retain the candidate-label mapping. |
| `scripts/verify_m9_external_evidence.py` | Uses the root-owned `/usr/bin/ssh-keygen -Y verify` with a pinned public anchor and emits a redacted receipt to stdout. | Start a provider request, execute a caller-supplied verifier, persist an evidence receipt, promote M8/M9, or authorize a material action. |

`second_brain.m9_external_evidence.OpenSshDetachedProofVerifier` copies public
anchor bytes and the detached proof into a private temporary directory only for
the short-lived verification process. The raw telemetry payload is passed on
stdin and none of these values are printed or stored in a project artifact.

Every receipt from this tooling has `promotion_authorized: false`. M6 remains
denial-only, and receipt verification never starts a canary or side effect.

`attest_external_routes()` and `capture_blinded_review_results()` are
low-level proof-parsing APIs, not an alternate approval path. The official
`scripts/verify_m9_external_evidence.py` boundary validates the final route
packet, current initial-M9 boundary, active registry, and bound approval
reference before it reads a route payload. Any other caller must enforce those
same checks before a future batch begins; a resulting receipt still cannot
authorize promotion.

## Required Manual Inputs

| Remaining gate | Evidence that is still required | Why this cannot be automated locally |
| --- | --- | --- |
| Authenticated route telemetry | New correlated 50+ `PUBLIC` read-only batch plus an upstream detached proof signed by a separately controlled authority. | Current CLI emits no authenticated correlation/route observation. |
| Blinded semantic non-inferiority | Fresh provider-output A/B packet, independent blinded reviewers, and authorized adjudication of the hidden label map and 95% LCB/pairwise thresholds. | Quality judgment and candidate identity must remain outside the local synthetic evaluator. |
| Provider performance/root context | Real B0/B1/provider task measurements with repeated timing, token/context metrics, and nondeterminism retained. | The local monotonic fixture is explicitly not provider performance. |
| Production-scale M4 | Authorized production-scale corpus/retrieval/context evaluation with provenance, freshness, omissions, and quality review. | The project has only the redacted ten-case local fixture and must not access an unnamed production corpus. |
| Personal canary and soak | New opt-in approval, at least the required task/time windows, route/quality/security monitoring, and visible failure handling. | It requires real user traffic and elapsed time. |
| Global rollback | Fresh rollback-specific packet, fresh approval naming its digest, and an executed, read-back rollback of the current global post-state. | It changes user-level Codex state; the previous approval explicitly excludes rollback rehearsal and the historical helper is not a new approval. |

## Global Rollback Sequence

Do not run this sequence now: no rollback is currently approved. If a future
hard gate requires rollback, keep the current layer active until all of these
steps are complete.

1. Generate a current redacted packet. This reads the exact applied post-state
   and writes only the project artifact; it neither starts a provider request
   nor changes global Codex state:

   ```bash
   PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 \
     scripts/prepare_m9_rollback_approval_packet.py \
     --codex-home /home/pixel/.codex
   ```

2. Review `artifacts/m9-rollback-approval-packet.json` and obtain a new explicit
   user approval that includes its `packet_digest`. The initial M9 digest is not
   valid for this purpose.
3. Execute the official rollback path only with that approved packet and
   reference. It restores the exact backed-up `AGENTS.md`, removes only the
   exact managed skill, and preserves/readbacks the three backup files:

   ```bash
   PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 \
     scripts/run_m9_rollout.py --codex-home /home/pixel/.codex rollback \
     --rollback-approval-packet artifacts/m9-rollback-approval-packet.json \
     --approval-reference '<fresh explicit approval containing rollback packet digest>'
   ```

The historical deployed `second-brain-backups/m9-v1/rollback.py` is not part of
this approval path. Do not execute or alter it without a separate exact global
approval packet.

## Route-Attestation Sequence

Do these only after a future exact packet describes the new shadow mechanism and
the user explicitly approves it. The old packet does not authorize any of these
steps.

1. Obtain a public `allowed-signers` file from the independently trusted
   router/provider audit authority. Keep its private signing key and all raw
   telemetry outside this repository.
2. Create a redacted policy under `artifacts/` by explicitly naming the public
   anchor outside the project:

   ```bash
   PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 \
     scripts/prepare_m9_external_trust_policy.py \
     --purpose route-attestation \
     --authority-id '<upstream-principal>' \
     --allowed-signers /absolute/path/to/allowed-signers \
     --output artifacts/m9-future-route-trust-policy.json
   ```

3. Create a new non-authorizing approval intent. It records only the existing
   applied M9 packet/shadow digests, fixed `PUBLIC` read-only scope, provider,
   stop conditions, and the known upstream-retention limitation. It breaks the
   plan/packet digest cycle; it is not a user approval:

   ```bash
   PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 \
     scripts/prepare_m9_route_approval_intent.py \
     --output artifacts/m9-future-route-approval-intent.json
   ```

4. Have the approved runner/upstream create a transient manifest outside this
   repository with exactly these fields: `approval_intent_digest`,
   `profile_registry_digest`, `provider_id`, and `requests`. The intent digest
   must come from step 3. The registry digest must equal the canonical digest of
   the active `config/model-profiles.json`; every request's alias, model
   identifier, and effort must match that registry. Each request has only
   `correlation_id`, `profile_alias`, `model_identifier`, and `effort`. The
   correlations must be transmitted to, and echoed by, the upstream
   authenticated audit path. Do not commit this transient file.
5. Produce the immutable redacted plan:

   ```bash
   PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 \
     scripts/prepare_m9_route_attestation_plan.py \
     --approval-intent artifacts/m9-future-route-approval-intent.json \
     --input /absolute/path/to/transient-route-manifest.json \
     --output artifacts/m9-future-route-plan.json
   ```

6. Build the exact final packet from the intent, plan, and public trust policy.
   Review its `approval_request`, then obtain a fresh user approval that names
   its `packet_digest`; the old M9 approval never satisfies this step:

   ```bash
   PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 \
     scripts/prepare_m9_route_approval_packet.py \
     --approval-intent artifacts/m9-future-route-approval-intent.json \
     --plan artifacts/m9-future-route-plan.json \
     --policy artifacts/m9-future-route-trust-policy.json \
     --output artifacts/m9-future-route-approval-packet.json
   ```

7. After the user approves the exact digest from step 6, run this read-only
   preflight immediately before the batch. A passing result confirms the live
   global post-state still matches the original applied M9 packet; it does not
   start any provider work:

   ```bash
   PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 \
     scripts/preflight_m9_route_approval.py \
     --codex-home /home/pixel/.codex \
     --approval-intent artifacts/m9-future-route-approval-intent.json \
     --approval-packet artifacts/m9-future-route-approval-packet.json \
     --plan artifacts/m9-future-route-plan.json \
     --policy artifacts/m9-future-route-trust-policy.json \
     --allowed-signers /absolute/path/to/allowed-signers \
     --approval-reference '<explicit approval containing fresh packet digest>'
   ```

8. Run the newly approved 50+ `PUBLIC` read-only shadows through the external
   correlated mechanism. Stop on a route mismatch, tool event, mutation,
   security event, or missing signed observation.
9. Keep the upstream payload and detached proof outside the repository, then
   verify them explicitly. The authorization reference must contain the final
   approval packet digest from step 6:

   ```bash
   PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 \
     scripts/verify_m9_external_evidence.py route \
     --approval-intent artifacts/m9-future-route-approval-intent.json \
     --approval-packet artifacts/m9-future-route-approval-packet.json \
     --plan artifacts/m9-future-route-plan.json \
     --policy artifacts/m9-future-route-trust-policy.json \
     --payload /absolute/path/to/upstream-route-observations.json \
     --proof /absolute/path/to/upstream-route-observations.sig \
     --allowed-signers /absolute/path/to/allowed-signers \
     --authorization-reference '<explicit approval containing fresh packet digest>'
   ```

The verifier rejects a policy/anchor mismatch, invalid signature, stale initial
boundary, missing/tampered intent or final packet, registry drift,
missing/duplicate/foreign correlation, route mismatch, or an attempt to pass
self-hashed JSON as proof. It validates authorization before reading the raw
payload or proof. A successful route receipt is evidence only; it still cannot
promote the workflow without the other gates and a new approval.

## Blinded-Review Sequence

The human-review plan must be based on fresh real candidate/B0 outputs, never
the local synthetic outcome scores. It is a redacted `NOT_RUN` review packet
with opaque case references and A/B evidence digests. The hidden candidate-label
assignment remains outside the reviewer and this repository.

1. Save only the redacted packet in the project, then derive an immutable intake
   plan:

   ```bash
   PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 \
     scripts/prepare_m9_blinded_review_plan.py \
     --packet artifacts/m9-future-blinded-review-packet.json \
     --output artifacts/m9-future-blinded-review-plan.json
   ```

2. Independently review every required case plus the prescribed spot reviews.
   The signed result may contain only the case-reference digest, A/B/tie choice,
   0-4 label scores, and high-risk flag; it must not contain prompt/response
   bodies or the candidate mapping.
3. Pin a separate review-authority trust policy and verify the detached proof
   with `scripts/verify_m9_external_evidence.py review`. A valid capture exits
   with code `2`, because it intentionally remains
   `BLOCKED_AUTHORIZED_ADJUDICATION_REQUIRED` until an authorized adjudicator
   evaluates the hidden mapping and the full non-inferiority thresholds.

## Promotion Boundary

Nothing in this runbook authorizes `opt-in`, limited rollout, expanded rollout,
default activation, soak, or global rollback. A rollback can proceed only under
the fresh packet and approval sequence above. After every manual evidence stage,
attach only redacted receipts/counters to a fresh report, prepare a new exact
packet if any target changes, and ask the user for approval before the next
stage. Preserve the current layer and stop/rollback on every hard-gate failure.
