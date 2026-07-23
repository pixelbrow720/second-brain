# M7 Final Security and Regression Review

Status: PASS -- zero open material findings in the project-local M7 scope.

## Scope

This was an independent read-only review of the bounded work-graph runtime,
its M5/M6 seams, and the M7 regression suite. It reviewed
`src/second_brain/graph_runtime.py`, `tests/test_work_graph_m7.py`, and
`tests/test_m7_integration.py`. It did not modify runtime code, tests, status,
documentation, global configuration, or external systems.

## Verified Findings and Remediations

- File scopes now authorize only the exact file; directory scopes use component
  boundaries. A descendant of a file scope is rejected.
- M5 issuance is bound to an exact `AuditTrail` and invokes the base
  `AuditTrail.has_admission` implementation, so subclass and instance-method
  spoofing do not grant graph entry.
- Manifest loading accepts only a contained relative workspace path and rejects
  absolute, traversal, and symlink-escape inputs.
- Process-local graph-run and worker leases reject a nested graph started from a
  worker child thread.
- A successful node must return every declared artifact and acceptance check;
  integration cannot pass through an omitted transitive dependency output.
- `NodeResult`, artifact references, proposed changes, and dependency references
  are canonicalized before use. Result subclasses cannot publish state, and a
  callback cannot mutate the runtime-owned dependency reference used by a later
  validation step.
- Retry is now an explicit closed availability set:
  `LOCAL_SYNTHETIC_TIMEOUT`, `PROVIDER_5XX`,
  `MCP_TEMPORARY_UNAVAILABLE`, and `TEMPORARY_UNAVAILABLE`. A
  `TRANSIENT_FAILURE` carrying `PERMISSION_DENIED` is rejected before it can
  schedule a second attempt; retry settlement repeats the allowlist check.
- Cancellation wins over a late success, active/pending work is cancelled at the
  safe boundary, route mismatch is quarantined before callback dispatch, and
  M6 remains proposal-only with `side_effect_allowed()` denial-only.

## Verification Evidence

- `PYTHONPATH=src python3 -m unittest -q tests.test_work_graph_m7 tests.test_m7_integration`
  passed: 23 tests.
- `PYTHONPATH=src python3 -m unittest -q tests.test_admission_m5 tests.test_routing_m6 tests.test_m6_integration`
  passed: 23 tests.
- `make check` passed: schema/contract checks, documentation checks, lint, and
  the complete deterministic suite.
- `python3 scripts/check_clean_room.py` passed from a repository-contained
  clean-room copy.
- A static scan of `src/second_brain/graph_runtime.py` found no direct shell,
  subprocess, network, delete, or general file-write API.

## Residual Boundary

The opaque cancellation token prevents ordinary callback code from directly
setting or clearing the runtime events, but injected same-process Python
callbacks are not a hostile-code sandbox. Arbitrary Python code can import
runtime internals and use reflective/object-level access to interfere with
process-local state. This is an explicit M7 limitation, not a hidden
authorization path: M7 runs cooperative synthetic/test callbacks only, exposes
no material writer, executor, capability host, or live side-effect authority,
and M6 receipts remain denial-only. Any future live or untrusted worker must
use process isolation/sandboxing and a host-enforced cancellation/termination
boundary before it can be considered safe.

M7 also does not claim M8's live quality, latency, or provider-attestation
targets; those remain separate deployment-gated evidence.
