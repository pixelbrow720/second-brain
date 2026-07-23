# ADR-005: Never Silently Omit Relevant Memory

- Status: Accepted for blueprint v1
- Date: 2026-07-22

## Context

The previous `ai-memory` prototype treated an object as stale when any code
reference hash changed. Retrieval then excluded the whole object without
warning. One changed file could therefore remove an otherwise current and
highly relevant conclusion from the Recovery Pack.

## Decision

Freshness is tracked per claim and per reference. Lifecycle, epistemic state,
verification, freshness, and integrity remain separate machine fields. The
presentation layer may derive these user-facing result classes:

- `current`: claim and required references are verified.
- `partially_stale`: the claim remains relevant, but some supporting references
  need revalidation.
- `historical`: valid for a prior time or snapshot, not current authority.
- `invalid`: corrupt, unsafe, revoked, or contradicted by authoritative state.

Machine freshness uses `fresh`, `partial`, `stale`, `unverifiable`, and
`not_applicable`; `disputed` remains epistemic. Relevant `partially_stale` and
`historical` results may be returned with clear warnings. Only `invalid` results
are excluded by default. Recovery output must report relevant stale candidates
that were omitted by a strict policy.

Derived indexes must prove that their content digest matches authoritative
files before they can narrow candidate retrieval. On mismatch, rebuild or fall
back to direct file search.

## Consequences

- Retrieval favors transparent uncertainty over false absence.
- Context packets require a compact warning section and provenance summaries.
- Tests must include partial hash drift, direct Markdown edits, deleted targets,
  contradictory claims, and stale-index fallback.
