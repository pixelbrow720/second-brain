# ADR-002: Use Conservative Work Admission

- Status: Accepted for blueprint v1
- Date: 2026-07-22

## Context

Automatic research, validation, and multi-agent fan-out can improve difficult
work, but it makes simple questions slow and expensive. Earlier workflow designs
over-orchestrated routine requests and exposed too many capabilities on every
turn.

## Decision

Every request enters one explicit lane:

- `DIRECT`: answer or perform a self-contained task in the root thread.
- `ASSISTED`: use a small number of tools or one bounded lookup.
- `GRAPH`: run at least two genuinely independent branches.
- `DEEP`: apply high-risk gates and independent review; use a bounded graph only
  when parallel branches or dependency structure also justify it.

Default admission is `DIRECT`. Research, capability discovery, and subagents
require positive admission signals. The system must never spawn an agent only
to switch to a cheaper model.

Graph execution is limited to four concurrent threads, depth one, twelve nodes,
one owner per writable scope, and one integrator. Retries are bounded to one
transient retry and one evidence-based escalation.

## Consequences

- Simple questions remain fast and predictable.
- Parallelism is used where wall-clock savings exceed coordination overhead.
- A small risk of under-parallelizing borderline work is accepted in exchange
  for lower latency and less context pollution.
- Admission behavior must be covered by a routing benchmark, including explicit
  tests for trivial math, casual questions, and small edits.
