# M6 Remediation Security Re-review

Status: **FAIL -- one material caller-controlled authorization bypass remains.**

## Scope

This independent, read-only re-review inspected the remediation following
`artifacts/m6-final-security-review.md`, with focus on
`src/second_brain/routing.py`, `tests/test_routing_m6.py`, and
`tests/test_m6_integration.py`. It did not modify code, configuration,
documentation, or global paths.

## Verification Run

- `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_routing_m6.py' -v`
  passed: 7 tests.
- `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_m6_integration.py' -v`
  passed: 5 tests.
- A direct public-API check confirmed that an ordinary synthetic `MATCH`
  receipt is denied for both `GRAPH` and `DEEP`; `dataclasses.replace` cannot
  change it to `live_attested=True`, and a directly tampered exact receipt is
  rejected by reconstruction.

## Material Finding

### P1: `RouteReceipt` subclass bypasses the synthetic/non-live gate

`side_effect_allowed` accepts subclasses via
`isinstance(receipt, RouteReceipt)` at
`src/second_brain/routing.py:940`, makes its preliminary trust decision from
caller-controlled attributes at `src/second_brain/routing.py:944`, then
validates through the dynamically dispatched `receipt.to_dict()` at
`src/second_brain/routing.py:948`.

A caller-controlled `RouteReceipt` subclass can be constructed from a valid
synthetic `MATCH` receipt, report `evidence_scope="trusted-live"` and
`live_attested=True` while the gate reads those attributes, and return the
original valid synthetic dictionary from an overridden `to_dict()`. The
reconstruction therefore validates the different synthetic representation,
while the final comparisons still see the forged live-looking attributes.
The focused reproduction returned:

```text
{'synthetic_receipt_originally_valid': True,
 'graph_side_effect_allowed': True,
 'deep_side_effect_allowed': True}
```

This defeats the stated property that synthetic-local or `live_attested=False`
receipts cannot authorize any `GRAPH` or `DEEP` material side effect, even when
the underlying normal receipt is `MATCH`. It is a caller-controlled bypass of
the exported commit-boundary predicate, so the P1 remediation is not complete.

## Required Remediation

1. Make the boundary reject subclasses (`type(receipt) is RouteReceipt` and
   `type(intent) is RouteIntent`), rather than relying on `isinstance` for
   these authority-bearing values.
2. Do not use dynamically dispatched `receipt.to_dict()` as the validation
   source at the authorization boundary. Reconstruct from a fixed internal
   allowlist of exact `RouteReceipt` fields, or use an exact-type-only helper.
3. Add a regression test containing the hostile subclass above and assert
   denial for both `GRAPH` and `DEEP`. Preserve the existing all-status tests.

## Controls Still Confirmed

- For ordinary exact `RouteReceipt` instances, M6 now rejects all
  synthetic/non-live evidence before status, registry, or permission inputs
  can authorize a side effect.
- Normal non-`MATCH` outcomes remain fail-closed, including missing and
  ambiguous telemetry; the focused routing suite covers every reconciliation
  status for `GRAPH` and `DEEP`.
- No M6 executor currently calls the predicate, so this re-review observed no
  host side effect. The exported predicate is nevertheless intended for a
  future material commit boundary and must be corrected before M6 acceptance.
