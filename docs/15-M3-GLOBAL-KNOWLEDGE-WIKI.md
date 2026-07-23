# M3 Global Knowledge Wiki and Ingest

M3 is complete as the project-local staging form of the Global Knowledge Wiki
from [the roadmap](09-IMPLEMENTATION-ROADMAP.md). It adds immutable raw capture
and reviewable semantic compilation without activating a global Codex
integration, fetching URLs, or reading a legacy vault.

## Authority Boundary

`RawCaptureRepository` owns only an explicit, repository-contained staging
root. It creates this local layout:

```text
raw/inbox/
raw/quarantine/
raw/blobs/sha256/<prefix>/<sha256>
raw/manifests/<capture-id>.json
raw/events.ndjson
```

The inbox and quarantine directories intentionally do not duplicate source
bytes: immutable blobs plus schema-validated capture manifests are the durable
evidence. `raw/inbox/` is empty after an explicit in-memory/file capture, and
the manifest's `status`, scanner result, and reason codes record quarantine.

M3 has no crawler or URL fetcher. A URL can be recorded as origin metadata, but
the caller must provide bytes explicitly. `capture_file()` requires one regular
file beneath an explicit allowed root and rejects absolute paths, traversal,
directories, symlinks, and oversized content.

## Capture and Review

The public capture API lives in `second_brain.ingest`:

- `RawCaptureRepository.capture_bytes()` writes a SHA-256 addressed blob once
  and creates a separate manifest for every capture provenance record.
- `capture_file()` is a bounded explicit-file convenience API; it never crawls.
- `get_manifest()` and `list_manifests()` verify the manifest filename/ID,
  append-only transition chain, blob digest, and exact blob byte count.
- `review()` applies explicit accept/reject CAS to a quarantined manifest under
  a private cross-process writer lock while preserving the raw blob bytes.

Every capture creation/review appends a hash-chained `raw/events.ndjson` record.
Any manifest/history inventory divergence, rewritten review state, moved manifest,
or changed blob fails closed. Raw and derived local file operations are anchored
to private no-follow directory descriptors to reject symlink substitution.

The policy gates size, supported text format, malformed JSON, secret
markers, instruction-like content, unsafe path/URL metadata, and restricted
licenses. Secret-marked bytes are never materialized; error and manifest data
contain only stable reason codes, not matched values. Instruction-like content
is data, not authority: it remains quarantined until explicit review and never
causes an action. YAML is intentionally unsupported until a safe full-document
parser is added, so it is quarantined rather than treated as arbitrary UTF-8.
This also applies to registered aliases such as `application/yml` and structured
suffixes such as `application/vnd.example+yaml`; policy cannot enable any of
them without a strict parser.

## Semantic Compilation

`KnowledgeCompiler` accepts an explicit `Store` with ID `knowledge:global` and
a concrete `RawCaptureRepository`. It refuses roots outside this repository,
revalidates the Store and capture root identities before authority operations,
and uses only the public M1 Store transaction surface for semantic writes.

`CompilationProposal` contains full-object M1 mutations, capture IDs,
idempotency key, rationale, and a reviewable citation map. `validate()` has no
write side effect. `commit()` validates all source/entity/concept/claim/synthesis
records, accepted-capture provenance, typed edges, claims, contradictions, and
material synthesis citations before one atomic Store transaction. A failed
validation or CAS does not produce a partial semantic commit; an exact retry is
idempotent.

Source objects retain metadata and a raw capture hash binding but never copy raw
source bytes into their semantic body or payload. Claims retain source/claim
support or an explicit inference label; replacements cannot erase their
statement, provenance, support, contradiction, verification, lifecycle, or
epistemic evidence. Existing support and contradiction relations are preserved
as complete canonical records, including relation ID, target revision, creation
time, and provenance. Every relation provenance ID must resolve to a provenance
record on its own source object. A contradiction has one canonical
directed edge compatible with the M1 acyclic graph, while deterministic derived
backlinks display it on both claims. A synthesis must cite every material
statement through a durable, exact citation map or mark it as inference. A
synthesis that covers one contradiction side must cover both, so
minority/disputed evidence cannot vanish through a narrative rewrite.

## Derived Navigation and Lint

`rebuild_views()` writes disposable, deterministic views below
`derived/knowledge/`: an index, per-kind MOCs, backlinks, orphan report, event
log projection, and a manifest bound to the Store snapshot epoch, event head,
and corpus digest. Manual drift produces lint warnings; deleting the derived
directory and rebuilding recreates the same tree for an unchanged authority
snapshot.

`lint()` combines M1 integrity checks with M3 warnings for orphan/duplicate
objects, unsupported claims, generic edges, incomplete citations, uncompiled
accepted captures, contradiction visibility, and MOC/backlink drift. It never
repairs authoritative state.

Project evidence can enter `ProjectPromotionOutbox` only through an explicit,
idempotent request naming its project object revision and hash. Outbox entries
remain `pending_review`; M3 intentionally provides no accept or automatic global
write path.

## Verification and Limits

Focused M3 suites cover capture policy/immutability, ledger and blob tampering,
semantic CAS and citation rules, relation-provenance preservation, derived
rebuild, promotion isolation, and cross-module acceptance. The independent
read-only security review has no open material finding after remediation. The
full local verification receipt is stored in `artifacts/m3-verification.json`.

M3 does not implement federated retrieval or bounded context (M4), automatic
project-to-global promotion, external fetch/connectors, or any global activation.
Raw bytes and semantic authority remain project-local; derived views may be
deleted and rebuilt, while authority rollback remains the M1 transaction
recovery path. No change is made to `~/.codex`, the old Obsidian vault,
`/home/pixel/Data/PROJECT/ai-memory`, or other repositories.
