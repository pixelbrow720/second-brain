# M1 Storage Interface

Status: approved local implementation contract from the M1 read-only audit.

## Scope And Decisions

- All store roots are explicit, repository-contained paths supplied by the
  caller. M1 never discovers or reads a global store, the old vault, or
  `ai-memory`.
- M1 supports a deliberately small YAML frontmatter subset implemented with the
  Python standard library. It accepts mappings, lists, quoted/plain scalars,
  and JSON flow values; it rejects duplicate keys, anchors, aliases, tags,
  multiple documents, BOM, and malformed frontmatter. Input line endings are
  normalized to LF before parsing and hashing.
- `transaction_committed.after_hash` is the SHA-256 of JCS bytes for the sorted
  list of `{id, revision, content_hash}` values changed by that transaction.
  Its terminal `before_hash` is a SHA-256 commitment to the transaction ID,
  store ID, idempotency key, and request intent digest, so a journal cannot be
  retargeted without breaking the hash-chained ledger.
- A committed journal/receipt retained under `runtime/transactions/` is the M1
  durable idempotency record. M2 may promote or compact it only with an explicit
  migration. A missing/corrupt matching record never fabricates idempotence.
- M1 accepts only local relation targets in the same explicit store. It rejects
  dangling targets, unknown provenance IDs, cycles, and unsupported cross-store
  resolution. M2/M3 extend federation deliberately.
- Content policy rejects detected secret material and instruction-like body text
  before an authoritative write. Diagnostics expose only stable reason codes,
  never the matching text.

## Public API

```python
Store.initialize(root, store_id, *, project_id=None, clock=None, id_factory=None,
                 authorizer=None, failure_injector=None) -> Store
Store.open(root, *, clock=None, id_factory=None, authorizer=None,
           failure_injector=None) -> Store

store.health  # StoreHealth.HEALTHY | StoreHealth.DEGRADED_READ_ONLY
store.read(object_id) -> dict
store.commit(TransactionRequest) -> CommitReceipt
store.recover() -> RecoveryReceipt
store.lint() -> LintReport
store.invalidate_derived(reason) -> None
```

`Mutation` has `operation`, `object_id`, `expected_revision`,
`expected_content_hash`, and `desired_object`. `TransactionRequest` has
`transaction_id`, `idempotency_key`, `store_id`, `actor`, `confirmation`,
`mutations`, and `reason`.

Stable error codes include `REVISION_CONFLICT`, `IDEMPOTENCY_KEY_REUSED`,
`STORE_DEGRADED`, `PATH_UNSAFE`, `INTEGRITY_FAILED`, `AUTHORITY_DENIED`, and
`RELATION_INVALID`.

## Commit And Recovery

1. Validate paths, parser/schema, content policy, identity, authority,
   relations, hashes, and all CAS expectations without side effects.
2. Take the single-store lock and repeat health/CAS/idempotency checks.
3. Persist and fsync a `prepared` journal containing before/after bytes, event
   offset, and intended manifest.
4. Atomic-replace object files, append/fsync chained events plus the final
   transaction event, then atomic-replace/fsync the manifest with one new epoch.
5. Mark the journal committed and invalidate only derived state.

Recovery produces a wholly pre-transaction or wholly committed post-transaction
state. A missing trustworthy journal or any object/event/manifest divergence
forces `DEGRADED_READ_ONLY`; normal `read()` fails closed until recovery has
returned the store to a verified authoritative snapshot.
