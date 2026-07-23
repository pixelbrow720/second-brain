# M9 Exact Global Approval And Rollout

Status: the exact packet digest
`2e841bde59753662ebd78d858f7c5fdc4933d92247f330c9df595cc3fdb693d4` was
explicitly approved and applied on 2026-07-23. Its initial 50-request
synthetic `PUBLIC` read-only shadow is complete, but it is not authenticated
route evidence and does not authorize an opt-in or promoted rollout.

## Practical V1 Follow-up

The applied M9 layer remains policy-only. A separate Practical V1 bridge and
revised skill were approved and applied from
`artifacts/practical-v1-approval-packet.json` with digest
`abe806a22ad13f1611331d3552bf6787f6b90967b045a6b8a54be9c72c36be63`.
Its [redacted apply report](../artifacts/practical-v1-apply-report.json)
records seven-target readback and a no-network disposable canary. This does not
alter M9's route-evidence boundary: operator-trusted router evidence is not
provider attestation and does not complete strict M8/M9.

## Current Packet

`artifacts/m9-approval-packet.json` is the machine-readable before-state packet
whose exact digest was approved for this first shadow stage. It
contains only paths, allowlisted metadata, modes, and SHA-256 digests; it does
not contain global guidance text, credentials, prompts, raw configuration,
headers, cookies, or telemetry. The packet builder transiently reads declared
files to calculate digests, append bytes, and the backup hash, but emits none of
their raw contents into the repository. Its pre-apply preflight is historical:
it must not be rerun now because the approved post-state correctly differs from
the packet's before-state.

The packet currently verifies these facts:

- the existing seven custom-agent model/effort pairs cover the approved profiles;
- `config.toml` and all existing agent files are observed and preserved;
- no plugin, MCP declaration, hook, provider setting, or endpoint is changed;
- the only proposed behavior changes are an additive managed block in global
  `AGENTS.md` and a new global `pixel-second-brain-workflow` skill; and
- a complete before-state copy plus standalone rollback helper are created
  before either behavior change is written.

## Applied Initial Shadow

The approved five-path additive deployment completed before the live batch.
`artifacts/m9-initial-shadow-report.json` is an immutable, redacted receipt
with digest `18e9383c2544aa0c9a597124dbc63a0d5c371aff5e1c5154ced0c3274e3aa4ec`.
It records these verified facts only:

- post-apply readback matched all 13 packet targets;
- 50 fresh, ephemeral Codex CLI sessions ran via the requested `cx_account`
  configuration override, each in an empty temporary workspace and a
  `read-only` sandbox;
- 50 of 50 response contracts passed, with zero observed tool calls,
  capability executions, memory writes, or external mutations; and
- Codex CLI JSONL did not expose an authenticated 9router/provider correlation,
  route, model, or effort observation. The receipt therefore records
  `MISSING_TELEMETRY` and `live_attested: false` rather than inferring success.

The receipt retains counters, closed statuses, and SHA-256 digests only. It
does not retain the public prompt instances, responses, raw CLI events,
configuration, headers, credentials, or endpoint details. Verify it without a
provider call:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 \
  scripts/verify_m9_initial_shadow.py
```

## Source-Bound Readiness Checkpoint

`artifacts/m8-m9-readiness-v2.json` binds the current M8 typed local-evidence
verification to this immutable M9 shadow receipt and packet digest. It records
two verified facts: eight source-bound local M8 receipts and 50/50 public
read-only M9 transport shadows with zero observed tool calls. It explicitly
leaves route telemetry, semantic non-inferiority, provider-task performance,
production-scale M4, personal canary/soak, and global rollback as `BLOCKED`.

The verifier reads only project artifacts. It does not read global Codex files,
send a provider request, or replace a different checkpoint:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 \
  scripts/verify_m8_m9_readiness.py
```

`artifacts/m8-m9-readiness.json` remains immutable historical evidence for the
earlier M8 receipt tree; it is not the current verifier target.

The generator deliberately exits `2` for this valid-but-blocked state, just as
the M8 local evaluator does:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 \
  scripts/generate_m8_m9_readiness.py
```

## Future External Evidence Contract

`second_brain.m9_external_evidence` supplies a project-local intake contract
for the evidence that is still missing. It performs no provider request, global
read/write, or rollout action. There is deliberately no default trust anchor or
automatic verifier invocation. A future operator can use the concrete
`openssh-detached-proof-v1` adapter only after explicitly pinning an
independently trusted public allowed-signers file; it verifies the raw payload
and detached proof against that pinned fingerprint. The resulting receipt
retains only digests, bounded counters, statuses, and verifier metadata.
The `attest_external_routes()` and `capture_blinded_review_results()` library
helpers are intentionally lower-level proof parsers: they do not load a final
packet or an approval reference. The project CLI is the authority boundary
because it validates those inputs, the current initial-M9 boundary, and the
active profile registry before route evidence is accepted.

A future route batch must use a fresh exact approval packet and a new
correlation-bound plan containing at least 50 `PUBLIC` read-only requests. The
raw correlation and model identifiers are transient input only; the plan and
receipt retain their digests. A missing, duplicate, foreign, or mismatched
observation fails visibly. The current initial-shadow receipt cannot be
retrofitted into this path: it has no authenticated per-request correlation and
must remain `MISSING_TELEMETRY`.

Before a newly approved batch starts, the runner must execute
`scripts/preflight_m9_route_approval.py`. It readbacks the original applied M9
post-state and validates the new approval reference, intent/plan/policy/final
packet binding, active profile registry, and pinned public trust anchor. It is
read-only and cannot start a provider request, invoke the signature verifier,
write evidence, or promote a rollout.

The same boundary can capture a proof-verified A/B blinded-review result bound
to the exact review packet. It deliberately does not retain the hidden
candidate-label mapping and returns
`BLOCKED_AUTHORIZED_ADJUDICATION_REQUIRED`, not a semantic non-inferiority
pass. Both receipt types have `promotion_authorized: false`; they never alter
M6's denial-only side-effect boundary or authorize a canary. Tests use a
test-only verifier and are not live evidence. A real verifier, fresh report,
new exact rollout packet where a mutation is proposed, and user approval are
still required before the next stage.

The exact local commands, transient-input boundary, manual prerequisites, and
remaining non-automatable gates are in
`docs/22-M8-M9-EXTERNAL-EVIDENCE-RUNBOOK.md`.

## Exact Mutation Scope

Relative to the current Codex home, M9 may mutate only these paths after the
user approves the current packet:

| Path | Operation | Rollback behavior |
| --- | --- | --- |
| `AGENTS.md` | Append a marker-bounded second-brain guidance block. | Restore the exact backed-up bytes. |
| `skills/pixel-second-brain-workflow/SKILL.md` | Create the staged global skill. | Remove it only if its digest is still exact. |
| `second-brain-backups/m9-v1/AGENTS.md` | Create an immutable before-state copy. | Preserve as rollback evidence. |
| `second-brain-backups/m9-v1/manifest.json` | Create the digest-bound rollback manifest. | Preserve as rollback evidence. |
| `second-brain-backups/m9-v1/rollback.py` | Create a standalone rollback helper. | Preserve as rollback evidence. |

The package refuses to proceed if any observed config/agent file, the global
guidance file, the staged bundle, the packet tooling, or either destination
differs from the packet. It never overwrites an existing managed-skill
destination or backup directory.

The packet also reports only sanitized model-route identity: protocol, host,
port, provider name, wire API, and whether a credential reference exists. It
never records an endpoint path, query, username, password, token, credential
name, or value. Upstream retention cannot be inferred from local configuration;
that unresolved risk is explicit in the packet and must be accepted before a
live canary.

## Fresh Rollback Approval

Rollback is a new global mutation. The first M9 approval does not authorize it,
including a rehearsal. The official project path first builds a new, redacted
rollback approval packet from the exact current applied state. It binds the
original M9 packet, current managed `AGENTS.md` and skill digests, the retained
backup contents, and the current rollback tooling digests. It does not read
provider data, make a provider request, or change global Codex state.

There is no current rollback packet or approval because no rollback has been
requested. If a rollback is required later, prepare the packet without changing
global state:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 \
  scripts/prepare_m9_rollback_approval_packet.py \
  --codex-home /home/pixel/.codex
```

Review the resulting `artifacts/m9-rollback-approval-packet.json`, obtain a
fresh user approval that contains its exact `packet_digest`, then execute only
the official runner:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 \
  scripts/run_m9_rollout.py --codex-home /home/pixel/.codex rollback \
  --rollback-approval-packet artifacts/m9-rollback-approval-packet.json \
  --approval-reference '<fresh explicit approval containing rollback packet digest>'
```

The runner rejects the initial packet reference, stale current state, modified
managed skill, foreign files in the managed skill directory, or drifted backup.
After a successful rollback it readbacks the restored `AGENTS.md`, confirms the
managed skill and its directory are absent, and confirms all three backup files
remain byte- and mode-identical. It changes only `AGENTS.md` and the exact
managed skill; the backup remains preserved.

`second-brain-backups/m9-v1/rollback.py` was created by the already-approved
initial packet and cannot be changed without a separate exact packet and user
approval. Treat it as historical recovery evidence, not as an authority bypass
for a new rollback. This checkpoint does not execute, modify, or rely on it.

## Packet Commands

Create a fresh packet without a global mutation only for a future, separately
approved rollout after the current state has been safely rolled back or a new
packet design is required:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 \
  scripts/prepare_m9_approval_packet.py --codex-home /home/pixel/.codex
```

Verify that an unapplied packet is still current without a global mutation:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 \
  scripts/run_m9_rollout.py --codex-home /home/pixel/.codex preflight
```

The `apply` command was intentionally unavailable until the user explicitly
approved this exact packet digest. It revalidated all targets under an exclusive
`AGENTS.md` lock, wrote the backup first, performed only the listed additive
changes, and verified their after-digests. Do not run `preflight` or `apply`
against the already-applied packet; use the shadow receipt readback instead.

## Required Approval

The user approved this packet's five listed mutations and initial 50-request
synthetic `PUBLIC` read-only shadow plan, including the unresolved upstream
retention limitation. That approval is consumed. It does not approve an opt-in,
expanded, default, rollback rehearsal, or soak promotion; each needs the
preceding report, a new exact packet where applicable, and a new approval.
The official rollback sequence above also requires a new rollback-specific
packet and approval; the original packet digest is never sufficient.

The approved live test sequence uses only synthetic `PUBLIC` inputs, stores only
redacted counters/digests and allowlisted route fields, and must stop on a route,
security, quality, context, or rollback failure. It still cannot access the old
Obsidian vault or modify `/home/pixel/Data/PROJECT/ai-memory`.

## Remaining Evidence

M8 and M9 are not complete yet. The real 50-shadow count is now satisfied, but
the report deliberately leaves promotion blocked because authenticated route
observations are absent. Remaining evidence includes blinded semantic
non-inferiority, provider-task graph performance/root-context measurements,
production-scale M4 retrieval/context, authenticated route observations,
controlled canary and soak behavior, and verified global rollback. M8's local
controlled M4 and performance fixtures do not clear those live gates. Any
failure leaves the prior layer active or triggers rollback; it cannot be
relabelled as local success.
