# Activation V2 A0 Fixtures and Threat Model

Status: local A0 design-contract checkpoint. These artifacts define and test
synthetic inputs only. They do not create a runtime, initialize a store,
capture a task, install a hook, create a session, select a live route, or
change any global Codex surface.

This checkpoint implements A0 from the [Activation V2 blueprint](24-ACTIVATION-V2-AUTOMATIC-MEMORY-GRAPH-AND-ROUTING.md): schema, privacy, and
failure fixtures must pass before any A1 work can begin. It is independent of
the strict M8/M9 gates, which remain blocked by their existing evidence
requirements.

## Contract Boundary

The schemas are checked-in public synthetic fixtures and are validated by the
existing dependency-free schema checker plus fail-closed semantic checks in
`src/second_brain/activation_v2.py`.

| Contract | Schema and canonical fixture | A0 authority boundary |
| --- | --- | --- |
| TaskClosure | `activation-v2-task-closure-v1` | Bounded, redaction-passed candidate only; no transcript or raw body field exists. |
| Capture receipt | `activation-v2-capture-receipt-v1` | `fixture_only`, `write_authority: none`, and no global commit. |
| Promotion outbox | `activation-v2-promotion-outbox-v1` | Hash-bound pending review metadata only; no global object or committed authority. |
| Route intent | `activation-v2-route-intent-v1` | No prompt body, `prompt_persistence: forbidden`, and `session_action: none`. |
| Graph snapshot | `activation-v2-graph-snapshot-v1` | Derived-only, revision-bound node/edge view; it cannot mutate authority. |

All five canonical records are joined only as a synthetic fixture bundle. The
bundle verifies closure digests, candidate counts, project identity, and that
outbox source objects were declared by the closure. It is not a store format or
a task-capture implementation.

## Threat Model

The threat fixtures use opaque `V2_*_SENTINEL` markers rather than real
credentials, user prompts, assistant text, or untrusted instructions. Rejected
errors expose stable reason codes only; they never echo the matched value.

| Threat | Entry point | A0 prevention | Fail-closed evidence |
| --- | --- | --- | --- |
| Secret leakage | Any permitted bounded text | Content policy rejects synthetic secret marker, key/token patterns, and private-key headers. | `SECRET_DETECTED`; no marker in error text. |
| Raw transcript or tool/assistant body | Unknown closure or route fields; permitted text | Exact allowlists reject `raw_transcript`, `assistant_output`, prompt fields, and transcript marker/role patterns. | Schema error or `RAW_TRANSCRIPT_DETECTED`. |
| Prompt injection | Closure summary or question | Content policy rejects synthetic injection marker and instruction-like patterns before any receipt/outbox can validate. | `PROMPT_INJECTION_DETECTED`. |
| Absolute path leakage | Permitted closure text | File URL and absolute-path forms are rejected rather than redacted into an identifier. | `ABSOLUTE_PATH_DETECTED`. |
| Cross-project leakage | Closure evidence, outbox sources, graph nodes/stores | Every project object and source store must match one exact project ID; project-focus graph rejects another project store. | Semantic validation failure before digest acceptance. |
| Graph edge cycle | `part_of` or `supersedes` edges | Each acyclic relation is independently traversed; self-edges and cycles are rejected. | `SemanticValidationError` naming the relation. |
| Route mismatch | Profile/effort intent | Only approved profiles are accepted; immutable effort must match the selected alias and uncertainty must fall back to `tera-max`. | Semantic validation failure before intent-digest acceptance. |

These controls are deliberately conservative. Pattern detection cannot prove
that arbitrary prose is safe; A0 therefore admits neither a runtime writer nor
a hook payload. A future A4 candidate validator needs further adversarial
corpus evidence before it can write project state.

## Test Matrix

| Area | Fixture or mutation | Check | Expected result |
| --- | --- | --- | --- |
| Schema registry | Five canonical A0 fixtures | `test_registry_accepts_every_canonical_a0_contract_and_bundle` | Every schema and cross-record bundle validates. |
| No authority | Canonical receipt, outbox, route, and snapshot | `test_canonical_contracts_are_explicitly_non_authorizing` | Fixture-only/no-write/no-session/derived-only invariants hold. |
| Threat coverage | `fixtures/activation-v2/a0-threat-cases-v1.json` | `test_public_synthetic_threat_fixture_covers_required_a0_threats` | Exactly the six requested threat classes are represented. |
| Secret, transcript, injection | In-memory mutation of TaskClosure summary | `test_sensitive_and_transcript_markers_fail_closed_without_echoing_content` | Stable policy code, no matched content in error. |
| Raw body allowlist | In-memory unknown closure fields | `test_raw_transcript_and_prompt_fields_are_rejected_by_the_allowlist` | Schema rejects fields before semantic processing. |
| Project boundary | Outbox source and graph node mutation | `test_cross_project_sources_and_nodes_are_rejected` | Different project ID fails closed. |
| Graph correctness | Two-edge `part_of`/`supersedes` cycle | `test_part_of_and_supersedes_cycles_are_rejected_before_the_snapshot_is_accepted` | Both cycles fail closed. |
| Route correctness | Profile/effort drift and prompt field | `test_profile_effort_mismatch_and_prompt_body_fail_closed` | Route is rejected; prompt body is not accepted. |
| Tamper detection | Digest replacement | `test_digest_tampering_is_rejected_for_outbox_route_and_graph` | Immutable metadata digest mismatch fails. |

Run the focused A0 suite with:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_activation_v2_a0 -v
```

The repository-wide contract and documentation checks include all five schemas
and the fixture bundle:

```bash
make check
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 scripts/check_clean_room.py
```

## A1 Preconditions

A0 does not authorize A1. Before a separate A1 request can initialize even an
empty private runtime, Pixel must decide the relevant items from Activation V2
section 15:

1. retention for closure receipts, outbox entries, backups, and graph snapshots;
2. default capture mode (the design recommendation is `ASSISTED`);
3. storage protection and key-rotation posture before personal data exists; and
4. whether to proceed with a project-local disposable runtime after reviewing
   the A1 path, symlink, backup, restore, and no-Git-leak plan.

Global hooks, global configuration, a front-door launcher, automatic capture,
provider calls, and opt-in defaults remain out of scope until later phases have
their own exact approval packets, backups, canaries, rollback evidence, and
explicit user approval.
