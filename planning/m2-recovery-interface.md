# M2 Recovery Interface Freeze

Status: approved for local M2 implementation on 2026-07-22.

This contract preserves M1 authority semantics. It does not authorize a read of
the real `ai-memory` project, an old Obsidian vault, global configuration, or
global knowledge writes.

## Boundary

- M2 accepts exactly one `project:<project-id>` M1 store. A knowledge/global
  store, cross-project ID, or cross-store relation is rejected.
- Authoritative objects, manifest, and events remain M1-owned. M2 creates only
  derived index/MOC/log/pack data and private local receipt state.
- M2 receives a public integrity-checked store snapshot. It must never call
  private M1 helpers or expose a partial authority corpus.
- A v1 reader is a read-only fixture adapter. It never writes v1 bytes and it
  reports rejected or quarantined legacy input without preserving raw bodies.

## Required M1 Extension

`Store.snapshot()` returns a coherent, validated snapshot or raises the existing
M1 integrity/degraded error before any result can be used:

```python
@dataclass(frozen=True)
class ObjectSnapshot:
    id: str
    revision: int
    content_hash: str
    relative_path: str
    file_sha256: str
    document: Mapping[str, Any]

@dataclass(frozen=True)
class StoreSnapshot:
    store_id: str
    project_id: str
    mutation_epoch: int
    event_head: str | None
    object_count: int
    objects: tuple[ObjectSnapshot, ...]  # sorted by ID
```

The normative corpus digest is SHA-256 over JCS of `store_id`,
`mutation_epoch`, `event_head`, and the ID-sorted object inventory containing
`id`, `revision`, `content_hash`, and physical `file_sha256`.

## Project Facade

`ProjectRecoveryKernel(store, project_root, clock, receipt_key_provider)` owns
the M2 flow:

- `snapshot()` and `observe_freshness(snapshot, object_ids=None)`
- `retrieve(request)` using `include_with_warning` by default
- `build_index(snapshot)` and direct authoritative scan fallback
- `generate_views(snapshot)` for Project Memory MOC and event-ledger log
- `build_recovery_pack(request)`, `checkpoint(pack, snapshot, observations)`
- `pre_compact(session_id, request)`, `post_compact(receipt, pack, session_id)`,
  and `session_start(...)`
- `inventory_fixture(fixture_root)` and `dry_run_map(inventory)`

Freshness observations bind an exact object revision and source/repository
snapshot. They do not mutate semantic objects. A changed optional or retained
fresh facet yields `partial`; `disputed` stays epistemic. The default policy
ranks active candidates before freshness penalty and includes relevant
`partial`/`unverifiable` candidates with warnings. Strict mode may omit them
only when it records ID, title, freshness, and `strict_freshness_filter` in the
envelope.

## Derived State and Receipts

- An index is usable only when its exact physical inventory digest matches a
  healthy M1 snapshot. Mismatch emits `INDEX_INVALID` and uses direct scan.
- MOC contains every and only active object once, sorted by category,
  normalized title, and ID. Generated MOC/log metadata records epoch, digest,
  generator, and `do_not_edit`; neither becomes authority.
- Recovery Pack is a bounded, generated Context Packet v1. Checkpoint is a
  generated non-authoritative record binding state, packet, and observation
  digests.
- Receipt HMAC covers canonical receipt bytes without `hmac_sha256`, uses a
  project-local non-symlink mode-0600 key, and compares in constant time.
  Tampered, expired, cross-project/session, state-mismatched, or pack-mismatched
  receipts fail closed before injection. Cold reuse is explicit-only.

Stable M2 rejection codes are `RECOVERY_RECEIPT_INVALID`,
`RECOVERY_RECEIPT_EXPIRED`, `RECOVERY_RECEIPT_SESSION_MISMATCH`,
`RECOVERY_RECEIPT_STATE_MISMATCH`, and `RECOVERY_RECEIPT_PACK_MISMATCH`.

## Direct Markdown Rule

1. A byte-only Markdown edit that preserves the parsed logical object and
   `content_hash` changes `file_sha256`: M1 stays healthy, M2 invalidates the
   index and direct-scans the healthy authority corpus.
2. A semantic out-of-band Markdown edit diverges M1 ledger/object state: M2
   detects the index mismatch but must surface `STORE_DEGRADED` rather than
   recover from untrusted changed bytes.

## Acceptance Matrix

1. Ten fixed canonical recovery queries recall all ten expected objects.
2. One changed reference among 19 produces `partial` plus a
   `partially_stale` warning and remains included.
3. A byte-only Markdown edit invalidates the index before narrowing; a semantic
   edit fails closed as degraded.
4. MOC covers all active objects; dangling relations fail or quarantine during
   read-only migration inventory.
5. Valid same-session receipt works; invalid HMAC, expiry, session/state/pack
   mismatch reject without changing authority.
6. Only synthetic copied fixtures are inventoried; the source project remains
   untouched.

All M2 time behavior uses `DeterministicClock`; fixture reference paths are
repository-contained and reject traversal, absolutes, backslashes, and symlink
escapes.
