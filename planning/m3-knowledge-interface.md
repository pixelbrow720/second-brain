# M3 Global Knowledge Interface

## Boundary

M3 implements a project-local staging instance of `knowledge:global`.  It does
not discover another store, read `ai-memory` or an Obsidian vault, fetch a URL,
or modify global Codex configuration.  All source bytes arrive through an
explicit caller-provided `bytes` value or an explicit, regular, contained local
file.  A URL is provenance metadata only in M3; no URL fetch API exists.

The existing public `Store` API is the sole authority writer for semantic
objects.  M3 must not call private M1 helpers.  Raw bytes are evidence/data,
never instructions; neither capture nor compilation may execute or follow text
found in a source.

## Capture Module: `second_brain.ingest`

`RawCaptureRepository(root, clock=None, policy=None)` owns only these paths
below its explicit project-local root:

```text
raw/inbox/
raw/quarantine/
raw/blobs/sha256/<first-two-hex>/<sha256>
raw/manifests/<capture-id>.json
```

Its public operations are deliberately bounded:

- `capture_bytes(request)` receives bytes, explicit origin metadata, media type,
  retention classification, and capturer identity.  It returns a validated
  manifest/result, a content digest, and whether the blob already existed.
- `capture_file(path, request)` is optional convenience only.  It accepts an
  explicit regular file contained under the caller-provided allowed root, and
  rejects traversal, a symlink component, directories, and oversized content.
  It never crawls a directory.
- `get_manifest(capture_id)`, `list_manifests()`, and `review(capture_id,
  decision)` are explicit; there is no automatic promotion or compiler trigger.

The capture manifest must validate against `raw-capture-manifest-v1`.  Byte
digests use SHA-256; an existing blob is never overwritten.  Identical bytes
create distinct capture manifests so their origin/capture provenance remains,
but point at one blob.  Changed bytes create a new blob and a new capture ID.
Manifest updates advance `revision` with a compare-and-swap expectation and
never alter blob bytes.

The staging policy must have deterministic, redacted reason codes for at least:
`SIZE_EXCEEDED`, `FORMAT_UNSUPPORTED`, `FORMAT_MALFORMED`, `SECRET_SUSPECTED`,
`INSTRUCTION_LIKE_CONTENT`, `PATH_UNSAFE`, `URL_UNSAFE`, and
`LICENSE_RESTRICTED`.  It must not persist a suspected/confirmed secret value
or print it in an exception/log.  Unsupported/malformed/instruction-like input
is quarantined or rejected with a manifest; it never becomes a semantic object
until an explicit review permits it.  A scan label is metadata, not executable
authority.  M3 supports a small allowlist of text media types and does not
pretend to extract PDF, Office, archive, image, or executable input.

## Semantic Module: `second_brain.knowledge`

`KnowledgeCompiler(store, raw_captures, clock=None)` operates only on an
explicit initialized `Store` whose ID is `knowledge:global`, and on an explicit
`RawCaptureRepository` rooted in the same local staging fixture.  Its public
proposal/receipt types must be serializable without source body bytes:

- `CompilationProposal`: proposal ID, compiler version, idempotency key,
  rationale, capture IDs, citation map, and full-object M1 `Mutation` values.
- `ProposalValidation`: accepted/rejected, stable reason codes, a reviewable
  object diff, and no write side effect.
- `CompilationReceipt`: proposal ID, Store `CommitReceipt`, changed object IDs,
  and derived-view digest/epoch.

`validate(proposal)` validates the complete proposed graph before writing.
`commit(proposal)` invokes exactly one public `Store.commit()` for every semantic
object change in the proposal.  Thus a CAS conflict or a failed object/edge
validation leaves all semantic changes absent; retrying the same proposal is
idempotent via its stable idempotency key.  The compiler must not silently swap
current revision/hash values after a conflict.

All documents must be normal valid M1 `memory-object-v2` records with global
IDs and one of `source`, `entity`, `concept`, `claim`, or `synthesis`.  M3 adds
kind-specific validation according to `docs/06-DATA-SCHEMAS.md`:

- a source payload links its accepted capture ID, raw SHA-256, media type,
  license and extraction metadata;
- an entity has a canonical name and disambiguation/type;
- a concept has domain, definition, boundaries and non-examples;
- an atomic claim has statement, subject IDs, temporal/applicability scope;
- a synthesis has topic, claim/source IDs, coverage and a material-statement
  citation map.

Every material synthesis statement maps to one or more source/claim IDs or is
explicitly labeled `inference`; absence is a proposal error.  A source report
is not elevated to observed fact by compilation.  A claim needs a source or
claim support path, provenance, verification state, and temporal/applicability
scope.  Contradiction edges require a claim target and a nonempty scope.  Since
the M1 generic graph rejects directed cycles, only one canonical contradiction
edge is written (stable ID ordering); derived backlinks show the relationship
from both sides.  No compiler operation resolves/deletes minority evidence.

`rebuild_views()` is derived-only and deterministic for a fixed `Store.snapshot`
and builder version.  It writes atomically below `derived/knowledge/`:

```text
manifest.json
index.md
moc-sources.md
moc-entities.md
moc-concepts.md
moc-claims.md
moc-syntheses.md
backlinks.json
orphans.json
log.md
```

The derived manifest binds the snapshot epoch, event head, authoritative corpus
digest, and builder version.  Generated Markdown records the same epoch/digest
and says not to edit it manually.  Stable ordering is normalized title then ID.
Manual drift is reported, then a rebuild replaces it.  Deleting all derived
state and rebuilding must recreate byte-identical views for the same authority
snapshot.

`lint()` combines `Store.lint()` with M3 semantic/derived checks.  Errors cover
invalid graph/schema/authority integrity.  Warnings include unsupported claims,
unresolved contradiction visibility, orphan objects, generic relations,
possible duplicate entities/concepts, incomplete citations, uncompiled sources,
and derived MOC/backlink drift.  It must never repair authority state.

`ProjectPromotionOutbox(root)` accepts only an explicit, reviewable promotion
request that names a project store/object ID, revision/content hash, target
global kind, provenance, and idempotency key.  It persists a `pending_review`
outbox item locally and is idempotent.  It has no accept/auto-commit path in M3;
writing an outbox item must not change `knowledge:global`.

## Integration Expectations

The root integration suite must demonstrate:

1. same bytes create one immutable blob and two capture manifests;
2. changed bytes create a different blob/capture/source without overwriting old
   bytes;
3. an accepted source can atomically create/update several semantic pages;
4. malicious instruction-looking source text remains data/quarantined and does
   not trigger an action or automatic compiler commit;
5. synthesis cites claims/sources or explicitly labels inference, while a
   contradiction and minority claim remain visible in generated views/lint;
6. deleting derived views then rebuilding yields the same tree digest; and
7. a project promotion remains an unaccepted local outbox item.

No module in M3 may access a user home directory, another repository, network,
old vault, `ai-memory`, or global configuration.
