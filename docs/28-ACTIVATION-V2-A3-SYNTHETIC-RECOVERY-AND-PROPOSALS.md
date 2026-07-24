# Activation V2 A3 Synthetic Recovery and Closure Proposals

Status: complete as synthetic-local evidence on 2026-07-24. This checkpoint
uses public synthetic records in ignored disposable test runtimes only. It does
not open a project or global authority store, discover a project, capture a
task body, install a hook, call a provider, or alter global configuration.

## Scope

`src/second_brain/activation_v2_memory.py` models a bounded `ASSISTED`
recovery request using labels only. An explicit synthetic corpus is bound to
one project ID, and global selectors require the explicit `include_global`
flag. A recovery proposal contains only selected identifiers, revisions,
bounded synthetic titles, freshness/verification labels, score, omissions, and
latency evidence. It is not a context packet or an authority object.

The TaskClosure path validates the existing bounded closure contract, then
writes only a `pending_review` closure proposal into the disposable outbox. A
synthetic review receipt can acknowledge it for a later transaction, but both
the proposal and review receipt require `authority_write: false` and
`global_write: false`.

## Fail-Closed Evidence

- A fixed public corpus produces the expected relevant result ordering,
  preserves a relevant `partial` record in the omission list, and reports a
  bounded local latency value.
- Retrieval rejects latency over one second instead of clipping the measured
  value, and leaves no recovery proposal receipt behind.
- A project corpus with a foreign project record is rejected. Rehashed recovery
  selectors cannot cross the selected project boundary, so a recomputed digest
  cannot turn a cross-project selector into an accepted proposal.
- Global records are excluded unless the request explicitly selects global
  inclusion; `DIRECT` is not an input to this API.
- Raw request fields, unsafe closure fields, non-`ASSISTED` fixture policy, and
  tampered receipt state fail before a proposal can be accepted or reused.
- Closure review verifies that its exact synthetic proposal exists and matches
  before it records the noncommitting review receipt.

The public fixtures and schemas are:

- `fixtures/activation-v2/a3-recovery-evaluation-v1.json`
- `fixtures/canonical/activation-v2-recovery-proposal-v1.json`
- `fixtures/canonical/activation-v2-closure-proposal-v1.json`
- `fixtures/canonical/activation-v2-proposal-review-receipt-v1.json`
- `schemas/activation-v2-recovery-proposal-v1.json`
- `schemas/activation-v2-closure-proposal-v1.json`
- `schemas/activation-v2-proposal-review-receipt-v1.json`

Run the focused checkpoint with:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest \
  tests.test_activation_v2_a0 tests.test_activation_v2_a1 \
  tests.test_activation_v2_a2 tests.test_activation_v2_a3 -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m second_brain.contract_checks
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m second_brain.documentation_checks
```

## Boundary And Next Gate

All writes in A3 are disposable safe-JSON fixture receipts below ignored
`artifacts/test-runs`; their test directories are removed after each test. No
real task closure, project memory, global knowledge, promotion acceptance, or
persisted user data exists.

A4 can implement only fixture/disposable `PROJECT_AUTO` transaction machinery.
Any real opt-in still needs explicit user decisions for retention, capture
default, storage/key management, graph UI, router entry point, and promotion
review policy. Global activation remains a separate exact-packet approval path.
