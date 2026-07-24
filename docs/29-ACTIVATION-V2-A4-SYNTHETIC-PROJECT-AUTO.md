# Activation V2 A4 Synthetic PROJECT_AUTO Transaction

Status: complete as synthetic-local evidence on 2026-07-24. A4 implements a
fixture-only state machine below ignored `artifacts/test-runs`; it does not
enable `PROJECT_AUTO` for a real project, open an authority store, capture a
real closure, or make a global change.

## Contract

`src/second_brain/activation_v2_project_auto.py` accepts a schema-validated
TaskClosure only when the disposable runtime has a fixture-only
`capture_default: PROJECT_AUTO` policy. It writes no closure summary, prompt,
transcript, or output body. Its synthetic state and receipts contain only:

- project, closure, and transaction IDs;
- closure/state digests and monotonically increasing revisions;
- task outcome and bounded candidate counts; and
- an explicit `pending_review` global-outbox boundary with both authority flags
  set to `false`.

`commit_synthetic_project_auto` requires an exact compare-and-swap revision.
The same active closure/digest returns its original receipt without a duplicate
write; a changed closure identity, stale revision, reused transaction, or
non-durable closure is rejected. `rollback_synthetic_project_auto` can roll
back only the latest matching synthetic commit under CAS and keeps the state
revision monotonic.

## Fail-Closed Evidence

- Canonical state and receipt schemas validate and bind their digests.
- A successful transaction returns a content-free user-visible status: outcome
  and candidate counts, not decision/question/evidence summaries.
- Stale CAS, duplicate/reused closure identity, and non-`PROJECT_AUTO` policy
  fail before a synthetic state path is created or changed.
- Unsafe closure fields and `no_durable_change` closures fail before the state
  write.
- Rollback rejects a non-latest transaction, cannot run twice, and preserves
  monotonic revision history.
- A rehashed state that names another project fails readback instead of leaking
  cross-project state.

The canonical contracts are:

- `schemas/activation-v2-project-auto-state-v1.json`
- `schemas/activation-v2-project-auto-receipt-v1.json`
- `fixtures/canonical/activation-v2-project-auto-state-v1.json`
- `fixtures/canonical/activation-v2-project-auto-receipt-v1.json`
- `tests/test_activation_v2_a4.py`

## Boundary And Next Gate

The state machine is a disposable simulation, not a project authority writer.
It creates no global outbox item, does not accept global promotion, and has no
hook, router, provider, or global configuration integration.

A5 may consume public synthetic graph inputs to build derived graph/view
artifacts only. The user still needs to choose retention, capture default,
storage/key management, graph UI, router entry point, and promotion-review
policy before any real opt-in can be proposed.
