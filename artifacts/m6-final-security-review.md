# M6 Final Security and Regression Review

Status: remediation required before M6 acceptance.

## Scope and Evidence

This independent, read-only review covered the M6 contract/security audits,
the routing and profile implementation, V1/V2 registry ownership, generated
staging, rollback, focused tests, ADR-003, ADR-006, and the M6 roadmap.

Focused verification passed:

- `python3 -c '... discover("tests", pattern="test_*m6.py") ...'`: 7 tests.
- `python3 -c '... discover("tests", pattern="test_m6_integration.py") ...'`: 5 tests.

Static path review found no home-directory/global-config, old-vault,
`ai-memory`, provider-client, or network access in the M6 implementation.
Generated artifacts are confined to `dist/global/m6/`; V1 remains a historical
schema/fixture while active routing loads only the V2 registry. These are static
implementation findings, not a host-history assertion.

## Findings

### P1: Synthetic-only `MATCH` receipts authorize GRAPH/DEEP side effects

`RouteReceipt` deliberately requires `evidence_scope == "synthetic-local"` and
`live_attested is False` in `src/second_brain/routing.py:803`, and
`create_route_receipt` always emits those values in
`src/second_brain/routing.py:911`. However, `side_effect_allowed` permits a
`MATCH` receipt for both `GRAPH` and `DEEP` without checking either field
(`src/second_brain/routing.py:916`, especially `:953`). The current test locks
in that behavior by setting `permitted = expected is ReconciliationStatus.MATCH`
in `tests/test_routing_m6.py:202`.

Reproduction using the public synthetic canary returned:

```text
{'canary_passed': True, 'evidence_scope': 'synthetic-local',
 'live_attested': False, 'graph_side_effect_allowed': True,
 'deep_side_effect_allowed': True}
```

This conflicts with the approved M6 security audit: synthetic mapping evidence
is unverified and cannot authorize a material action. A future executor that
uses this predicate at its commit boundary could commit, publish, install, or
otherwise mutate based solely on self-generated local fixture telemetry. M6 has
no executor today, so there is no observed host side effect, but the exported
gate is explicitly intended for that boundary and is not safe to accept as-is.

Remediation: reject every synthetic/non-live receipt in
`side_effect_allowed`. Since M6 intentionally cannot produce a live receipt,
the local predicate should deny all material actions in this milestone. Preserve
route reconciliation as a diagnostic result, and introduce a separate,
trusted live-attestation receipt only after its telemetry/authentication contract
is proven. Add regression coverage that a synthetic `MATCH` is denied for both
`GRAPH` and `DEEP`; do not relabel synthetic evidence as live merely to retain
the existing positive test.

## Controls Confirmed

- Non-`MATCH` reconciliation statuses are denied by the current gate; missing
  and ambiguous telemetry reconcile to non-success states.
- Exact effort comparison preserves `high`, `max`, and `xhigh`; the permanent
  non-xhigh-to-xhigh fixture returns `MISMATCH_EFFORT` across five repeats.
- Route receipts are built from fixed allowlists and the secret/prompt/URL/cookie
  marker test passes without retaining the markers.
- Staging and rollback use the one fixed `dist/global/m6` target and reject
  foreign destinations, manual drift, symlinks, stale files, and tampered
  candidate digests in the focused integration suite.
- The active V2 registry contains exactly the seven approved aliases with Tera
  Max as root; the M0 generator preserves historical V1 assets and no longer
  overwrites the active V2 registry.

## Conclusion

Material remediation is required. The current implementation satisfies the
non-`MATCH` denial property but fails the stronger project-local evidence
boundary because a synthetic `MATCH` can authorize a material action. Re-run
the focused M6 suite and this independent review after the gate and regression
expectations are corrected.
