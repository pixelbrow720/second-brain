# Activation V2 A5 Derived Focus Graph

Status: complete as synthetic-local evidence on 2026-07-24. A5 compiles a
bounded graph snapshot only from public synthetic inputs below ignored
`artifacts/test-runs`; it does not start a graph server, open an authority
store, create user memory, or enable a graph UI for any real project.

## Contract

`src/second_brain/activation_v2_graph.py` accepts a validated A0
`GraphSnapshot` and an opaque, bounded focus selector. A selector has an exact
project ID, a project-owned focus node, one or two hops, and node/edge limits of
16/32. It has no text query, browser action, authority target, or session action.

The generated `FocusGraphView` is deterministic for the same source snapshot
and selector:

- its ID is derived from the source snapshot ID/digest and selector;
- its timestamp is the source snapshot generation timestamp rather than the
  wall clock;
- node traversal, edge order, and provenance IDs are canonicalized; and
- rebuilding an already-derived view verifies the exact existing artifact
  rather than creating a second one.

Nodes retain kind, lifecycle, freshness, revision, and a safe bounded label.
Edges retain both endpoint revisions, relation, opaque provenance IDs, and the
required project-to-global provenance tuple. `related_to` is excluded from the
default focus view. `part_of` and `supersedes` cycles remain invalid even if an
attacker rehashes the derived view.

The view contract binds all of the following to `false`/derived-only values:
`fixture_only`, `ui_server_started`, `authority_write`, and `global_write`.
It is a JSON snapshot, not a UI server or editor. The compiler measures its
local operation against a 1,000 ms limit before writing; timing is deliberately
not serialized so byte-equivalent rebuilds do not claim an invented latency.

## Fail-Closed Evidence

- Canonical schema and fixture validate, including revision-bound cross-store
  provenance and no-authority UI flags.
- The fixed public synthetic graph compiles byte-equivalently in two disposable
  runtimes and reuses the exact persisted derived artifact on replay.
- Non-generated graph policy, cross-project selectors, and hop-budget overflow
  fail before an A5 artifact path exists.
- Unsafe source or rehashed derived labels fail the shared secret/transcript/
  injection barrier without exposing the rejected text.
- Generic `related_to` is excluded by default; rehashed cycles, project-boundary
  tampering, and over-budget compilation fail closed.
- A forced latency breach leaves no derived view file.

The checked-in contracts and fixtures are:

- `schemas/activation-v2-focus-graph-view-v1.json`
- `fixtures/canonical/activation-v2-focus-graph-view-v1.json`
- `fixtures/activation-v2/a5-focus-graph-evaluation-v1.json`
- `tests/test_activation_v2_a5.py`

## Boundary And Next Gate

A5 contains no web panel, desktop integration, hook, launcher, provider call,
authority-store write, or global target. `GENERATED_SNAPSHOT` is a recommended
fixture policy only, not a user-selected graph UI default.

A6 may now implement a local, synthetic front-door routing shadow evaluator.
It must emit redacted selection evidence only: no alternate session creation,
provider/network call, default-route change, or global launcher installation.
