# M4 Final Security and Regression Review

Reviewed with the approved `pixel-gpt55-xhigh` profile. Findings are ordered by severity.

## P1 - Candidate caps can split a known contradiction pair

- `src/second_brain/retrieval.py:1502` rejects a relation peer once the lexical
  `candidate_limit` is full, before `_selection_groups()` can make the pair
  atomic at `src/second_brain/retrieval.py:1763`. A query with
  `candidate_limit=object_limit=1` that lexically matches only one side returns
  that side in `included` and puts its contradicting peer in
  `relevant_but_omitted` as `budget_candidate_limit`.
- Remedy: reserve/admit contradiction peers as an atomic candidate group even
  when they exceed the lexical cap, or omit the already-selected side with the
  same explicit contradiction-group reason. Add a regression where only one
  side matches the query and the candidate cap is one; assert that both sides
  are included or both are omitted as one group.

## P1 - Default lifecycle filtering hides relevant historical evidence

- `src/second_brain/retrieval.py:482` defaults every request to
  `lifecycle=("active",)`, and `src/second_brain/retrieval.py:816` rejects a
  matching `superseded` or `archived` object before the historical warning at
  `src/second_brain/retrieval.py:1685` can run. This fails the M4 acceptance
  requirement and ADR-005 rule that relevant historical evidence is visible
  with a warning rather than excluded by default.
- Remedy: retain relevant historical candidates by default (or inject required
  predecessor/successor history as a separately marked group), with
  `HISTORICAL_EVIDENCE` and an auditable lifecycle field. Add default-query and
  bounded-context tests for a lexically matching superseded claim, plus a
  strict/filtering test that records any intentional exclusion as an omission.

## P2 - Caller-controlled query IDs can put secrets into redacted receipts

- `src/second_brain/retrieval.py:476` accepts any bounded non-empty string as
  `query_id`; `src/second_brain/retrieval.py:564` and
  `src/second_brain/retrieval.py:666` then serialize it into retrieval and
  context receipts. For example, `qry:RECEIPT-SECRET-MARKER` is returned in the
  receipt verbatim, bypassing the stated no-prompt/no-secret receipt boundary.
- Remedy: require the opaque `qry:<canonical-uuid>` form or always generate the
  ID internally, and reject caller values outside that grammar. Add a receipt
  redaction regression proving a secret-bearing supplied ID is rejected and no
  serialized receipt contains the marker.

## Remediation Re-review

Re-reviewed with the approved `pixel-gpt55-xhigh` profile after the requested
remediations. Focused M4 suites pass (`17` tests), but one material finding
remains open.

**Open material findings: 1 (P1).**

### P1 - Candidate-limit contradiction atomicity is incomplete for multiple peers

- `src/second_brain/retrieval.py:1511` calls
  `_omit_conflict_pair_for_candidate_limit()` for the first full-capacity
  contradiction edge. That helper removes only the source at
  `src/second_brain/retrieval.py:1560`; the same loop then processes a second
  contradiction edge with spare capacity and can include that second peer on
  its own. A source claim contradicting two nonmatching peers with
  `candidate_limit=object_limit=1` reproduced one included peer while the
  source and first peer were omitted.
- Remedy: resolve/remove the complete reachable contradiction component before
  processing another edge (or keep a blocked-ID set for the source and every
  contradiction peer), then emit one shared `contradiction_group_budget`
  omission. Add a three-claim, two-edge candidate-cap regression that asserts
  no member of the component is included.

### Resolved - Historical lifecycle visibility

- `src/second_brain/retrieval.py:486` now defaults to active, superseded, and
  archived records, and `src/second_brain/retrieval.py:1716` marks historical
  project/global candidates. `tests/test_retrieval_m4.py:233` verifies both
  inclusion and propagation to bounded context. No remaining material finding
  for this remediation.

### Resolved - Query ID receipt redaction

- `src/second_brain/retrieval.py:476` now enforces an opaque canonical
  `qry:<uuid>` ID; `tests/test_retrieval_m4.py:80` rejects the secret-bearing
  marker before a receipt can be created. No remaining material finding for
  this remediation.

## Final P1 Remediation Re-review

Re-reviewed with the approved `pixel-gpt55-xhigh` profile.

**Open material findings: 0.**

- `src/second_brain/retrieval.py:1511` now resolves the complete in-memory
  contradiction component before applying the candidate cap. If it cannot fit,
  `src/second_brain/retrieval.py:1572` removes and omits every component member
  under one `contradiction_group_budget` selection group, then breaks the
  source-edge loop.
- `tests/test_retrieval_m4.py:294` passes for the two-peer regression. An
  additional read-only four-claim chained-component probe with
  `candidate_limit=object_limit=1` also produced no included component member;
  all four were omitted with `contradiction_group_budget`.
