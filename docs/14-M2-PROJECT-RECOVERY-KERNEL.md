# M2 Project Recovery Kernel v2

Status: M2 complete and locally verified on 2026-07-22. This checkpoint remains
project-local: it does not activate global knowledge, change Codex configuration,
or read or modify `/home/pixel/Data/PROJECT/ai-memory`.

## Delivered Kernel

M2 builds a recovery facade only over the public, integrity-checked M1 snapshot:

- project-scoped snapshot projection, lexical candidate ranking, per-reference
  and per-facet freshness observations, default `include_with_warning`, and
  strict-mode omission reporting;
- index manifest validation against exact physical authority inventory, direct
  authoritative scan fallback, and deterministic MOC/event-ledger views;
- bounded Context Packet v1 generation, checkpoint binding, project-private HMAC
  receipts, and verified PreCompact, PostCompact, and SessionStart flows;
- a read-only v1 fixture adapter and dry-run mapping report which retain no raw
  bodies and never write fixture/source bytes;
- descriptor-pinned, no-follow reference reads capped by the query freshness
  budget. Oversized or uncheckable selected references become `unverifiable`,
  missing references retain their explicit missing state, and irrelevant
  candidates are not observed;
- inventory digesting limited to records admitted for migration. Runtime,
  derived, handoff, and out-of-scope source bytes are never read; quarantined
  candidate records are never re-read or bound into the migration digest.

## Acceptance Evidence

| M2 acceptance criterion | Deterministic evidence |
| --- | --- |
| Canonical recovery recall is 10/10 | `tests/test_recovery_m2.py` canonical-query regression |
| One of 19 changed references is `partial`, warned, and retained | Bubblewrap freshness regression |
| Physical Markdown edits invalidate an index; semantic edits fail closed | direct-scan and degraded-store regressions |
| MOC covers active objects; dangling legacy records quarantine | MOC and read-only inventory regressions |
| Receipt HMAC, expiry, session, state, pack, and checkpoint mismatches reject | receipt lifecycle regressions |
| Excluded private legacy bytes are neither read nor digest-bound | excluded-runtime/derived/handoff regression |
| Irrelevant or oversized references cannot bypass retrieval budgets | candidate-first and bounded-read regressions |

The focused M2 suite passes 15 tests. `make check` and
`python3 scripts/check_clean_room.py` each pass the complete 62-test local suite.
The final independent read-only security re-review reported no material findings
for the bounded-reader, legacy-inventory, receipt, and snapshot paths. Exact
command receipts are in `artifacts/m2-verification.json`.

## Requirement Coverage

M2 implements the local project-recovery portions of FR-12, FR-14, FR-16,
FR-20, NFR-04, NFR-05, NFR-08, NFR-09, AC-05, AC-06, AC-07, and AC-10 under
ADR-001 and ADR-005. It deliberately does not claim M3 global ingest, M4
federated retrieval, or any global deployment behavior.

## Boundaries And Rollback

- Only repository-contained synthetic fixtures were used. The old Obsidian vault,
  `/home/pixel/Data/PROJECT/ai-memory`, global Codex configuration, plugins,
  hooks, MCP registrations, and model-provider configuration were untouched.
- M2 derives only index/view/packet/receipt state under an explicit caller-owned
  M1 store. It never mutates M1 authority objects, manifests, or events.
- Roll back code by reverting only M2-local paths. Do not delete a caller-created
  authority store: preserve its events and journals for recovery or explicit
  operator review.
- The next ready milestone is M3 Global Knowledge Wiki and Ingest, staged solely
  against project-local synthetic inputs until the later global deployment gate.
