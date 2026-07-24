# Activation V2 A7 Synthetic Rollout Preparation

Status: complete as synthetic-local evidence on 2026-07-24. A7 prepares and
rehearses a packet only from supplied `PUBLIC_SYNTHETIC` target snapshots in an
ignored disposable runtime. It does not inspect a real target, create a real
per-project opt-in default, or authorize a global Codex mutation.

## Contract

`src/second_brain/activation_v2_rollout.py` accepts only opaque target IDs,
roles, synthetic project IDs, revisions, and before/candidate SHA-256 digests.
No path, configuration body, prompt, transcript, secret, header, cookie,
credential, or user-memory field is accepted. A bundle has exactly the two
roles `project_opt_in_config` and `project_activation_manifest`, one of each;
every target must carry the same opaque `synthetic_project_id` as its bundle.

Every state-changing API receives the draft packet, the exact supplied target
bundle, and an explicit synthetic `as_of` timestamp. It checks the full packet
target list against that bundle, the bundle/packet project ID, expiry, the
fixture runtime policy digest, and a scoped local implementation digest before
it reads or writes state. The implementation digest covers the listed A7 source
and schema files only; it detects fixture/source drift in this local rehearsal.
It is SHA-256 integrity metadata, not a signature, identity proof, or user
approval mechanism.

The packet names every required synthetic rehearsal step in order:

1. `backup`
2. `canary`
3. `readback`
4. `rollback`

It also records no permission impact, no network permission, no global target
access, no global mutation authorization, and a
`SYNTHETIC_DRAFT_NOT_APPROVED` approval state. This is exact only for the
supplied synthetic snapshot bundle. It is deliberately not a current real
approval packet: producing one would require current target choices, a
read-only exact snapshot of those targets, and new explicit user approval.

The disposable state machine uses exact compare-and-swap revisions under a
process-local lock plus an exclusive manifest-file lock. A canary requires the
exact persisted backup receipt; readback requires the exact persisted canary
receipt; and rollback requires the exact persisted readback receipt. Each
receipt binds the packet, synthetic project, backup digest, state revision,
target digest states, and zero network/global/authority flags. Rollback
restores the backup digests while keeping every synthetic target and state
revision monotonic.

Canary and rollback write a versioned `pending-operation` journal before their
state write. On the next locked load, the journal is removed when neither write
occurred or both state and receipt match; if only the state write occurred, the
prior digest-only state is restored; every other combination fails closed. This
is a disposable crash-recovery rehearsal, not a durable user-memory journal.

## Fail-Closed Evidence

- The five A7 schemas validate canonical target-bundle, packet, state, receipt,
  and pending-operation fixtures with self-digest checks.
- The fixed two-target corpus produces the exact packet, four receipt digests,
  and final restored state in a disposable runtime.
- Duplicate/missing roles, mixed project IDs, an altered/rehashed packet target,
  mismatched runtime policy, mismatched source digest, stale expiry, malformed
  unsafe timestamp, unknown raw-input field, or non-fixture policy fails before
  a state transition.
- A stale CAS revision, a backup receipt copied from another runtime, an
  incorrect predecessor receipt, concurrent canaries, an interrupted
  state-before-receipt window, and an altered/rehashed network boundary flag
  fail closed. No later receipt is written after a failed prerequisite.
- The rehearsal leaves project, global, and outbox directories empty; runtime
  health continues to report no authority-store access and no global mutation.

The checked-in local contracts and evidence are:

- `schemas/activation-v2-a7-target-bundle-v1.json`
- `schemas/activation-v2-a7-approval-packet-v1.json`
- `schemas/activation-v2-a7-rollout-state-v1.json`
- `schemas/activation-v2-a7-rollout-receipt-v1.json`
- `schemas/activation-v2-a7-pending-operation-v1.json`
- `fixtures/canonical/activation-v2-a7-target-bundle-v1.json`
- `fixtures/canonical/activation-v2-a7-approval-packet-v1.json`
- `fixtures/canonical/activation-v2-a7-rollout-state-v1.json`
- `fixtures/canonical/activation-v2-a7-rollout-receipt-v1.json`
- `fixtures/canonical/activation-v2-a7-pending-operation-v1.json`
- `fixtures/activation-v2/a7-rollout-evaluation-v1.json`
- `tests/test_activation_v2_a7.py`

## Boundary And Next Gate

The A7 packet and rollback rehearsal are project-local test machinery, not an
installer, global configuration reader, deployment command, or approval token.
No `~/.codex` path, global AGENTS/skill/plugin/MCP/hook/provider endpoint,
Obsidian vault, or `ai-memory` path is discovered or accessed.

A8 may evaluate a synthetic reviewed-promotion and restore workflow only. A
real A7 opt-in or A8 promotion remains blocked until the user chooses retention,
capture mode, storage/key management, graph UI, router entry point, and
promotion-review policy; supplies a current exact target snapshot; and approves
the resulting real packet explicitly.
