# M4 Federated Retrieval and Bounded Context

M4 is the project-local implementation of Federated Retrieval and Bounded
Context from [the roadmap](09-IMPLEMENTATION-ROADMAP.md). It retrieves the
minimum sufficient semantic evidence from explicitly registered project and
global M1 stores without activating global Codex configuration, importing a
legacy vault, or reading `/home/pixel/Data/PROJECT/ai-memory`.

Status: complete and locally verified on 2026-07-22. The exact local receipt is
[`artifacts/m4-verification.json`](../artifacts/m4-verification.json).

## Authority Boundary

The public M4 surface is in `second_brain.retrieval`:

- `StoreRegistry` accepts only concrete M1 `Store` handles with canonical,
  repository-contained roots. Registration captures and subsequently checks the
  store ID, role, project ID, root, and fresh snapshot identity. It has no
  discovery, crawling, or path-based lookup behavior.
- `FederatedQueryRequest` is a separate non-`R0` contract. Its `for_lane()`
  factory returns no request for `DIRECT`, before a query ID, snapshot, index,
  receipt, or optional adapter can be created. `R0` is rejected if passed to
  the retriever directly.
- `QualifiedLink` is a derived, query-only cross-store link. M1 continues to
  reject persisted cross-store relations and distributed transactions. Link
  targets must use a qualified authority ID and resolve through the registry;
  missing or unavailable targets become visible `dangling_external` results.
- `FederatedRetriever` consumes only M1 `Store.snapshot()` objects. It never
  reads raw capture blobs, quarantine, runtime receipts, global configuration,
  or arbitrary repository files.

Project and global snapshots are independently captured and recorded. They do
not claim distributed atomicity. A missing global store leaves project retrieval
usable with `GLOBAL_UNAVAILABLE`; a missing requested project is reported as
unrecovered rather than replaced by a general answer.

## Retrieval, Index, and Freshness

Every registered store can have a disposable FTS5 index at
`derived/m4-retrieval/`. Its strict manifest binds the index schema/builder,
tokenizer configuration, store identity, epoch, event head, exact corpus digest,
full inventory, and SQLite file digest. Missing, malformed, stale, sidecar, or
symlinked index state always falls back to an authoritative direct scan. The
direct scan remains part of the correctness path, so a bad index cannot turn
knowledge into a false empty answer. Deleting all indexes therefore preserves
canonical result identity, citations, ordering, and omission classes.

Lexical ranking exposes relevance, scope, authority, verification, relation,
corroboration, freshness, and duplication components. Optional vector/hybrid
retrieval is represented by a boundary only: it is disabled by default, cannot
expand scope, and a failure leaves lexical/direct retrieval authoritative.

M4 reuses M2's public bounded freshness observation seam for project objects.
The caller supplies requested repository scope and freshness read ceilings;
M4 does not use an M2 private method. Relevant `partial`, `stale`, and
`unverifiable` evidence remains visible under `include_with_warning`. In
`strict_fresh_only`, the same object ID, title, freshness state, and
`strict_freshness_filter` reason are retained in
`relevant_but_omitted`. `diagnostic_no_check` is forbidden for a
`verification_gate` packet.

Typed expansion respects explicit relation types, depth (maximum 3), fan-out,
candidate, and time ceilings. Relation paths retain relation ID, expected target
revision/hash, scope, creation time, provenance IDs, and reverse-view metadata.
M3's canonical one-way contradiction edge is represented bidirectionally only
in the query view. Both conflict sides form one selection group: they are both
included or both omitted with an explicit contradiction budget/policy reason.

## Bounded Context and Receipts

`ContextCompiler` accepts only an envelope issued by its paired retriever. It
checks its digest, a private issuance record, current registry generation, and
fresh M1 snapshot digests before compiling. Caller-mutated snippets, hashes,
citations, or snapshot data are rejected rather than rendered.

The fixed packet sections are:

1. `task_scope`
2. `active_state`
3. `knowledge`
4. `evidence`
5. `conflicts_and_freshness`
6. `omissions`
7. `guardrails`

Packets support `recovery`, `scoped_task`, `graph_synthesis`, and
`verification_gate` purposes. Their final serialized packet obeys hard object,
UTF-8 byte, estimated-token, and time ceilings. Guardrails, snapshots,
citations, warnings, conflicts, and omissions are reserved before optional body
text; the compiler trims quoted evidence excerpts first. Each `ID@revision`
maps exactly once to store ID, relative semantic-object path, content hash,
selector, and provenance IDs. Retrieved content is marked `content_role=data`
and quoted; it cannot change task scope, route, budget, or the static guardrail.

Retrieval and context receipts contain only IDs, hashes, snapshots, counts,
budget metadata, warning codes, and omission reasons. They deliberately exclude
query text, source body text, raw bytes, secrets, credentials, and full packet
bodies.

## Verification and Rollback

The M4 unit, context, and integration suites cover `DIRECT`/`R0` no-op behavior,
explicit scope isolation, registry root pinning, FTS5/direct-scan equivalence,
corrupt/deleted-index fallback, partial freshness, strict omission, qualified
links, unavailable global degradation, adapter disable/fallback, contradiction
atomicity, all packet purposes, final packet ceilings, citation completeness,
envelope tamper rejection, receipt redaction, canonical project/global recall,
and rebuild equivalence.

The focused M4 and M2 compatibility command passes 33 deterministic tests.
`make check` and `python3 scripts/check_clean_room.py` also pass after the final
independent re-review, which has zero open material findings. Requirement
coverage is recorded in the receipt for FR-11, FR-12, FR-13, FR-18, FR-19,
NFR-03, NFR-05, NFR-08, NFR-11, AC-05, AC-06, AC-07, ADR-001, and ADR-005.

M4 remains local. Rollback consists of reverting the M4 module, M2's
backward-compatible public freshness-bound parameters, M4 tests, documentation,
and disposable `derived/m4-retrieval/` indexes. It does not delete M1 authority
objects, raw captures, M2 recovery state, or caller-created stores. No global
activation, old Obsidian-vault access, or `ai-memory` access is implied.
