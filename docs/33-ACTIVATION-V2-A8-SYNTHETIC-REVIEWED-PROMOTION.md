# Activation V2 A8 Synthetic Reviewed Promotion

Status: complete as synthetic-local evidence on 2026-07-24. A8 evaluates a
reviewed-promotion and restore sequence only from checked-in `PUBLIC_SYNTHETIC`
fixtures inside an ignored A1 disposable runtime. It does not inspect a real
global target, accept user approval, create global knowledge, or activate any
memory, hook, router, or global configuration.

## Contract

`src/second_brain/activation_v2_promotion.py` accepts one closed reviewed
promotion corpus. Each candidate contains no semantic body: only a project ID,
opaque project-object IDs, an opaque global-claim ID, revisions, SHA-256
digests, and the safe A3 closure-proposal and proposal-review metadata that
establish its provenance. The candidate is rejected unless all of the following
are true:

- every project-scoped source ID, closure proposal, and review belongs to the
  same exact synthetic project;
- the review references the exact closure proposal and is
  `approved_for_later_transaction` from the synthetic fixture reviewer;
- the closure proposal still has at least one pending global candidate; and
- its before and candidate-after digests differ.

The corpus is bound to the exact A7 draft packet ID/digest, an unexpired
timestamp, and the local A8 implementation digest. A8 also verifies the source
A7 packet's current local implementation digest, its fixture-only policy digest,
its project ID, its expiry, and its zero network/global-mutation boundary. A
packet cannot be built when either source is future-dated, stale, rehashed, or
different from the exact supplied corpus.

The resulting A8 packet has `review_mode: PER_ITEM`, carries no target path or
configuration body, and records all of these fixed boundaries:

- `approval_state: SYNTHETIC_DRAFT_NOT_APPROVED`;
- `requires_current_user_approval: true`;
- `network_permitted: false`, `global_target_access: false`, and
  `global_mutation_authorized: false`; and
- the fixed `backup -> promotion_canary -> readback -> restore` sequence.

The fixture-only `PER_ITEM` policy is an evaluator input, not a global policy
default. Retention, capture mode, storage/key management, graph UI, router
entry point, and real promotion review policy remain user decisions.

## Synthetic Restore Evaluator

The evaluator writes only safe JSON below `artifacts/test-runs`. Its state holds
opaque candidate digests and monotonically increasing synthetic revisions:

1. `backup` records the exact pre-promotion candidate state and runtime ID.
2. `promotion_canary` changes only the disposable state to candidate digests.
3. `readback` requires the exact persisted canary receipt and matching state.
4. `restore` requires the exact persisted readback receipt, restores the
   pre-promotion digest, and still advances the synthetic object revision.

All transitions use an exclusive process/file lock and exact compare-and-swap
revision. Backup and predecessor receipts are bound to the packet, corpus, A7
packet digest, project, runtime ID, and backup digest. They cannot be replayed
in another disposable runtime.

Canary and restore place a digest-only pending-operation journal before writing
state. A later locked read removes the journal only when neither write occurred
or when the exact state and receipt both exist. If only state exists, it restores
the known prior synthetic state. Any other combination fails closed. This is a
crash-window rehearsal for disposable data, not a user-memory journal.

## Fail-Closed Evidence

- The five A8 schemas validate a reviewed corpus, non-authorizing packet,
  disposable state, receipt, and pending-operation journal.
- `tests/test_activation_v2_a8.py` validates exact canonical corpus/packet,
  four receipt digests, final restored state, and source bindings.
- Rejected review, cross-project source object, raw transcript field,
  future/stale corpus, policy mismatch, A7 source drift, A8 source drift, stale
  CAS, foreign-runtime backup, wrong predecessor receipt, concurrent canary,
  interrupted state/receipt write, and rehashed network boundary flag all fail
  closed.
- The successful rehearsal leaves `projects/`, `global/`, and `outbox/` empty;
  runtime health continues to report no authority-store access and no global
  mutation.

The checked-in local evidence is:

- `schemas/activation-v2-a8-promotion-corpus-v1.json`
- `schemas/activation-v2-a8-promotion-packet-v1.json`
- `schemas/activation-v2-a8-promotion-state-v1.json`
- `schemas/activation-v2-a8-promotion-receipt-v1.json`
- `schemas/activation-v2-a8-pending-operation-v1.json`
- `fixtures/canonical/activation-v2-a8-promotion-corpus-v1.json`
- `fixtures/canonical/activation-v2-a8-promotion-packet-v1.json`
- `fixtures/canonical/activation-v2-a8-promotion-state-v1.json`
- `fixtures/canonical/activation-v2-a8-promotion-receipt-v1.json`
- `fixtures/canonical/activation-v2-a8-pending-operation-v1.json`
- `fixtures/activation-v2/a8-promotion-evaluation-v1.json`
- `tests/test_activation_v2_a8.py`

## Boundary And Required Decision

The A8 evaluator has no real target reader, installer, provider client, network
call, approval consumer, lifecycle hook, or persistent-memory capture path. It
does not access `~/.codex`, global AGENTS/skills/plugins/MCP/hooks, any old
Obsidian vault, or `/home/pixel/Data/PROJECT/ai-memory`.

No real A7 opt-in or A8 global promotion is now authorized. Before any such
work, the user must choose the unresolved policy inputs, provide a current exact
read-only snapshot of each real target, receive a freshly generated packet that
lists all mutations and permissions, and explicitly approve that exact packet
digest. M8/M9's existing strict gates remain separately blocked.
