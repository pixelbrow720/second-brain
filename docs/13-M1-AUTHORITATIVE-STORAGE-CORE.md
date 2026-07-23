# M1 Authoritative Storage Core

Status: M1 complete and locally verified on 2026-07-22. This checkpoint is
project-local and does not authorize global Codex configuration or source-data
changes.

## Delivered Kernel

M1 adds the shared authority layer used by the future Project Recovery and
Global Knowledge stores:

- strict JSON, NDJSON, and safe Markdown/YAML-frontmatter parsing with LF
  normalization, duplicate-key rejection, and unsupported-feature rejection;
- RFC 8785-style JCS canonicalization and SHA-256 helpers;
- schema, ID/store/kind, content-hash, relation, reference-path, content-policy,
  lifecycle, and user-decision confirmation validation;
- explicit local `Store` roots with CAS revisions, a single-writer lock,
  descriptor-relative no-follow filesystem operations, and restrictive ownership
  and mode checks;
- prepared transaction journals, atomic object/manifest replacement, fsynced
  event append, hash-chained events, manifest epochs, durable idempotency, and
  deterministic crash recovery;
- event, object, manifest, and journal integrity checks that force a fail-closed
  degraded state. Normal reads fail until recovery re-establishes one verified
  authoritative snapshot;
- a fast linter, redacted secret/instruction policy failures, and derived-state
  invalidation that never removes authoritative files.

The terminal transaction event preserves the required sorted changed-object
`after_hash` and uses its otherwise objectless `before_hash` as a SHA-256
commitment to the transaction ID, store ID, idempotency key, and request intent
digest. A tampered journal can therefore not fabricate idempotency without
breaking the ledger chain.

## Acceptance Evidence

| M1 acceptance criterion | Deterministic evidence |
| --- | --- |
| Create, replace, CAS conflict, idempotency, crash recovery, and concurrent writer fixtures pass | `tests/test_storage_m1_transactions.py` |
| No last-write-wins path exists | CAS and concurrent-writer regressions in `tests/test_storage_m1_transactions.py` |
| Event/object/manifest or journal divergence enters degraded mode | `tests/test_storage_m1_integrity.py` and `tests/test_storage_m1_security.py` |
| Traversal and symlink escapes fail before a write | `tests/test_storage_m1_integrity.py` and `tests/test_storage_m1_security.py` |
| Failure injection leaves only recoverable whole states | `tests/test_storage_m1_transactions.py` and the degraded-read regression |
| Deleting derived state loses no authoritative data | `tests/test_storage_m1_integrity.py` |

The final independent `GPT-5.5 xhigh` read-only security review reported no
material findings after the journal-binding, lifecycle-create, and
degraded-read regressions were added. The full local suite contains 47 tests;
the command receipts are recorded in `artifacts/m1-verification.json`.

## Requirement Coverage

This milestone implements and verifies the shared-kernel portions of FR-07,
FR-08, FR-09, FR-16, FR-18, FR-19, NFR-08, NFR-09, and NFR-12, following
ADR-001, ADR-005, and ADR-006. M2 and M3 retain their domain-specific recovery,
ingest, compiler, and retrieval work; M1 does not claim to have implemented
those later workflows.

## Boundary And Rollback

- No old Obsidian vault, `/home/pixel/Data/PROJECT/ai-memory`, global Codex
  configuration, plugin, MCP, hook, or model-provider setting was read or
  changed.
- M1 creates a store only at an explicit caller-supplied project-local root; it
  neither discovers nor initializes a global store.
- Code rollback returns the repository to the M0 scaffold. It must not delete a
  caller-created authority store: retain it for `Store.recover()` or explicit
  operator review, because journals and events are durable evidence.
- M2 subsequently completed against this local kernel without accessing
  `ai-memory`. The next ready milestone is M3 Global Knowledge Wiki and Ingest,
  which remains project-local until a later deployment gate.
