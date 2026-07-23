# ADR-001: Separate Project Recovery and Global Knowledge

- Status: Accepted for blueprint v1
- Date: 2026-07-22

## Context

Project recovery and a personal knowledge wiki solve different problems. Project
recovery must preserve current decisions, work state, blockers, and verification
with strict freshness. A global second brain must accumulate sources, claims,
concepts, entities, and evolving synthesis across projects.

Combining both into one object graph makes operational evidence dominate the
wiki, promotes project-local facts globally by accident, and forces one
freshness policy onto data with different lifecycles.

## Decision

Use two cooperating durable domains:

1. **Project Recovery Kernel**: project-local authority for decisions, tasks,
   blockers, code-linked evidence, checkpoints, and bounded restart context.
2. **Global Knowledge Wiki**: personal cross-project knowledge containing
   immutable raw source records and maintained source, entity, concept, claim,
   and synthesis pages.

The two domains may reference each other through stable qualified IDs, but no
project fact is promoted to global knowledge automatically. Promotion requires
an explicit proposal and provenance review.

Raw sources are the immutable source-of-record for what was observed. They are
not assumed to be true. Wiki claims and syntheses remain derived and must cite
their supporting or contradicting sources.

## Consequences

- Recovery remains small, strict, and task-specific.
- The wiki can evolve without polluting startup context.
- Cross-project leakage is controlled by explicit visibility and promotion.
- Retrieval must federate two stores while preserving their authority labels.
- Migration from `ai-memory` reuses the recovery kernel but does not treat its
  operational evidence catalog as a completed global wiki.
