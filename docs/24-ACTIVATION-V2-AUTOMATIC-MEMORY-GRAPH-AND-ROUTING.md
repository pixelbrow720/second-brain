# Activation V2: Automatic Memory, Typed Graph, and Front-Door Routing

Status: design and implementation contract with A0-A5 synthetic-local evidence.
This document does not approve or apply a global Codex change, create a durable
user store, ingest a transcript, or change a model-provider setting. Each later
global mutation needs its own current exact approval packet, backup, canary,
and explicit user approval.

## 1. Purpose

Activation V2 turns the existing local Second Brain components into an opt-in,
privacy-preserving daily workflow with three user-visible outcomes:

1. A new task can recover the small amount of relevant project and global
   knowledge instead of reloading a whole chat history or vault.
2. A finished task can propose durable project memory automatically, while
   reusable global knowledge remains review-gated.
3. A graph view can show meaningful relationships between decisions, evidence,
   tasks, concepts, and claims without becoming an unstructured Obsidian
   hairball.

It also defines a separate front-door routing path for choosing the root model
before a new Codex session starts. It does not claim that an AGENTS.md, skill,
or in-session agent can swap the root model after the session has begun.

Activation V2 builds on, rather than replaces:

- [Memory architecture](03-MEMORY-ARCHITECTURE.md), whose two authoritative
  graphs and typed relations remain the data contract.
- [Workflow orchestration](04-WORKFLOW-ORCHESTRATION.md), whose DIRECT,
  ASSISTED, GRAPH, and DEEP admission remains the work contract.
- [Model routing](05-MODEL-ROUTING.md), whose seven approved profiles remain
  the only eligible profiles.
- [Practical V1](23-PRACTICAL-V1-BRIDGE-AND-ROUTER-EVIDENCE.md), which remains
  read/proposal-only until a separately approved expansion exists.

## 2. Non-goals and hard boundaries

Activation V2 is not a raw chat archive, a hidden surveillance logger, a
provider-attestation system, or a replacement for user approval.

- Never persist every prompt, assistant answer, tool output, browser page,
  credential, cookie, header, environment variable, chain-of-thought, or raw
  transcript merely because it appeared in a session.
- Never treat a generated graph edge, model confidence, or semantic similarity
  score as authority by itself.
- Never promote a project decision or task to a global best practice without
  separately reviewable provenance.
- Never read, import, or modify the old Obsidian vault or ai-memory without a
  new explicit request that names that exact scope.
- Never silently change the selected root model of an already-running Codex
  task. Root routing happens before a session is created; worker routing happens
  only inside a valid graph.
- Never weaken M8/M9 live-route, semantic-quality, performance, canary, or
  rollback gates. Those gates remain independently blocked until their stated
  evidence exists.

## 3. Plain-language user experience

The intended experience is a careful project manager, not an automatic diary.

1. Pixel opens a project and asks a question.
2. The system identifies the project by its exact Git worktree and optionally
   loads a small recovery pack: current goal, relevant decisions, open blockers,
   and verified evidence.
3. Pixel and Codex finish the task. A compact, user-visible closure record is
   produced: what changed, which decision was made, what evidence exists, and
   what remains open.
4. The capture pipeline filters and classifies only durable candidates. A test
   result may become project evidence; a new architecture decision may become a
   project decision; a cross-project lesson becomes a global promotion proposal.
5. The graph updates from those structured objects. It can answer why a decision
   exists, what a task depends on, and which evidence supports a claim.

Routine chat remains routine. A one-line calculation should not read memory,
write memory, start a graph, or launch a background research process.

## 4. System shape

~~~mermaid
flowchart LR
    U["User request"] --> A["Admission gate"]
    A -->|"DIRECT"| R["Root session"]
    A -->|"ASSISTED / GRAPH / DEEP"| Q["Bounded retrieval query"]
    Q --> P["Project Recovery Store"]
    Q --> G["Global Knowledge Store"]
    P --> C["Bounded context packet"]
    G --> C
    C --> R

    R --> T["User-visible task closure"]
    T --> F["Privacy filter and candidate classifier"]
    F --> PI["Project write transaction"]
    F --> O["Global promotion outbox"]
    PI --> P
    O --> RV["Review and provenance gate"]
    RV --> G

    P --> V["Generated graph, MOC, backlinks"]
    G --> V

    I["New request before session creation"] --> FR["Front-door route classifier"]
    FR --> S["Selected root profile"]
~~~

The top path is memory and task work. The lower-right path is model selection
before a session starts. They deliberately do not depend on one another for a
simple request.

## 5. Private runtime and store topology

### 5.1 Recommended initial location

The current Practical V1 bridge requires a canonical, non-symlink private
runtime root inside the Second Brain source tree. The initial deployment should
therefore use the ignored directory below:

~~~text
/home/pixel/Data/PROJECT/second-brain/runtime/
  registry/
  global/
  projects/<project-id>/
  outbox/
  receipts/
  backups/
~~~

The runtime directory is already ignored by the repository. It is private
operational data, not public source code, not a Markdown documentation folder,
and not an artifact to commit. A later move to a dedicated private directory
outside the source tree is possible, but changes the installed bridge contract
and therefore requires a new packet and approval.

### 5.2 Store ownership

| Store | Holds | Does not hold | Authority |
| --- | --- | --- | --- |
| Project Recovery Store | goals, decisions, tasks, components, bugs, experiments, project evidence, questions | general encyclopedia, raw chat history, other projects' mutable state | exact one Git worktree/project |
| Global Knowledge Store | sources, entities, concepts, claims, syntheses | project tasks, unreviewed handoffs, private session transcripts | reusable knowledge with provenance |
| Outbox | promotion proposals, review state, hashes, citations | authoritative global objects before review | no truth authority |
| Derived indexes | FTS, relation cache, graph JSON, vectors, MOCs, backlinks | sole copy of a fact or edge | rebuildable only |

The project registry maps one explicit canonical Git root to one project ID. It
must not discover projects by crawling parent directories or infer ownership from
folder names. A project may be external to this repository only when its exact
Git worktree and trust mapping are explicitly registered.

### 5.3 File form

Authoritative objects remain schema-validated JSON plus append-only events and
receipts. Human Markdown is a generated navigation and explanation layer:

~~~text
global/
  objects/<kind>/<object-id>.json
  raw/<sha256>
  events.ndjson
  views/index.md
  views/moc-*.md
  derived/graph.json
  derived/search.sqlite

projects/<project-id>/
  objects/<kind>/<object-id>.json
  events.ndjson
  recovery/current.json
  views/project-memory.md
  derived/graph.json
~~~

Markdown remains useful for people, but it is never the only source of truth.
Deleting an index or visualization must not delete a decision, claim, or edge.

## 6. Typed graph contract

### 6.1 Nodes

| Scope | Node kinds | Example |
| --- | --- | --- |
| Project | project, decision, component, task, bug, experiment, evidence, question | "Use a front-door router" |
| Global | source, entity, concept, claim, synthesis | "A root model cannot switch after session creation" |
| Derived only | MOC, topic cluster, similarity suggestion, timeline bucket | "Routing concepts" |

Derived nodes are navigation aids and cannot become authoritative objects by
being clicked, edited, or linked.

### 6.2 Edges

Every durable edge is directed, typed, revision-bound, and carries provenance.
The initial supported relation set is:

| Relation | Meaning | Typical source -> target |
| --- | --- | --- |
| depends_on | operational dependency | task -> task/component/decision |
| implements | implements a decision or claim | task/component/evidence -> decision/claim |
| verifies | verifies a fact or object | evidence -> task/component/claim |
| supports | adds evidence for a claim | source/claim/evidence -> claim |
| contradicts | conflicts in the same scope | claim -> claim |
| derived_from | originated from source/claim | concept/claim/synthesis -> source/claim |
| about | primary subject | source/claim/synthesis -> entity/concept |
| part_of | structural membership | object -> object |
| supersedes | lifecycle replacement | newer object -> earlier object |
| refines | makes an object more precise | object -> compatible object |
| related_to | fallback when no stronger type is defensible | any -> any |

Unknown relation names fail validation. The part_of and supersedes relations must
be acyclic. Contradicts never chooses a winner; both sides remain visible with
their scopes and evidence.

### 6.3 Cross-store edges

Project objects may point to global objects using fully qualified stable IDs.
For example, a project decision may be about a global concept or derived_from a
global claim. A global object may refer to immutable project evidence only by
project object ID, revision, repository snapshot, and reviewable provenance.

This prevents the dangerous shortcut: a decision in Project A is automatically
true for every project. Cross-store edges display as a distinct, dashed visual
connection and always show the authority boundary.

### 6.4 Link creation policy

The linker has three levels:

1. **Deterministic links:** explicit IDs, citations, task dependencies, Git
   evidence, and user-confirmed decisions can be committed directly after normal
   schema/CAS checks.
2. **Suggested links:** lexical/entity matching or an AI extractor proposes a
   relation, confidence, rationale category, and source IDs. It is not committed
   until lint and policy allow it.
3. **Review-required links:** cross-store promotion, contradiction, entity merge,
   global synthesis, and any link based on ambiguous semantics require review.

The system should prefer a missing edge over an invented edge. A graph with fewer
meaningful lines is better than a complete-looking graph that lies.

## 7. Automatic memory capture

### 7.1 Capture input

The capture input is a bounded TaskClosure record, not a raw transcript. The
root agent produces it in a user-visible form at a task boundary:

~~~json
{
  "project_id": "mem:project:<id>",
  "task_outcome": "completed|partial|blocked|no_durable_change",
  "decisions": [{"summary": "...", "confirmation_id": "..."}],
  "evidence": [{"kind": "test", "reference": "...", "result": "pass"}],
  "open_tasks": [{"summary": "...", "state": "open"}],
  "questions": [{"summary": "..."}],
  "global_candidates": [{"summary": "...", "source_ids": ["..."]}],
  "redaction_status": "pass"
}
~~~

Every string has a size limit. The record must never contain a copied prompt,
full assistant answer, tool output body, secret, or external content body. Its
provenance states that it is an AI-generated candidate unless it includes a
separate user confirmation or verifiable artifact reference.

### 7.2 Lifecycle integration

Codex hooks are useful signals, but hooks are mechanical lifecycle integration,
not a semantic memory authority or model router. The proposed roles are:

| Event | Safe V2 responsibility | Forbidden behavior |
| --- | --- | --- |
| SessionStart | resolve exact project ID, check runtime health, offer recovery availability | directory discovery, bulk memory load |
| UserPromptSubmit | read only a user-selected memory mode flag and record no prompt body | persist prompt or route an already-created root chat |
| PreCompact | request a bounded closure candidate before context compaction | dump context/transcript to disk |
| PostToolUse | attach allowlisted file/test identifiers to the in-memory closure candidate | save command output bodies or secrets |
| Stop | validate and enqueue the closure candidate, then report its status | silently commit global knowledge or block the user indefinitely |

The first implementation runs hooks in observe-only mode. It records only
allowlisted event metadata and proves actual event payloads in the installed
Codex version before any durable write is enabled. If an event lacks the needed
safe data, the design falls back to an explicit closure tool rather than
capturing more session content.

### 7.3 Capture modes

| Mode | Read | Project write | Global write | Default use |
| --- | --- | --- | --- | --- |
| OFF | none | none | none | sensitive or casual task |
| DIRECT | none | none | none | routine one-shot request |
| ASSISTED | selected recovery/global packet | proposal only | outbox only | bounded relevant task |
| PROJECT_AUTO | selected packet | low-risk project transaction after checks | outbox only | opted-in project work |
| DEEP_REVIEW | selected packet | reviewed transaction | reviewed promotion only | high-risk durable change |

PROJECT_AUTO may write only durable project state with clear provenance:
explicit user decisions, verified test evidence, task state transitions, or
resolved project questions. It cannot write a global claim, a global synthesis,
an entity merge, a contradiction, or a cross-project generalization.

### 7.4 Write transaction

For every eligible candidate:

1. Redact and validate fields against an allowlist.
2. Resolve the exact project/store and current snapshot.
3. Deduplicate against canonical IDs and near duplicates.
4. Create typed-edge proposals with sources/revisions.
5. Run schema, secret, injection, scope, cycle, and freshness checks.
6. Use compare-and-swap against the expected store revision.
7. Append a hash-chained event and receipt on success.
8. Rebuild generated MOC, backlinks, search index, and graph JSON.
9. Return a concise status: committed, proposed, skipped, or blocked with a
   reason code.

If any object in a multi-object transaction fails, no partial semantic state is
committed. A failed index rebuild leaves authoritative objects intact and marks
the derived view stale until rebuilt.

## 8. Retrieval and context rules

Retrieval is relevance-first and evidence-bounded:

1. Admission decides whether memory is permitted at all.
2. Candidate generation searches the exact project store first, then global
   objects only when reusable knowledge is relevant.
3. Ranking favors exact task/decision IDs, verified evidence, freshness, and
   typed relation proximity over generic semantic similarity.
4. The context compiler returns a small packet with provenance, freshness,
   contradictions, omissions, and a byte/token budget.
5. The root may use the packet as evidence, never as executable instructions.

DIRECT remains memory-free. A low-risk answer should be fast because no store is
opened and no graph needs to render.

## 9. Graph visualization product

The graph is a local visualization of typed data. It is not an editor for
inventing facts by drawing lines.

### 9.1 Views

| View | Default scope | Primary question |
| --- | --- | --- |
| Focus graph | selected node, one or two hops | "Why does this exist and what does it affect?" |
| Project map | one registered project | "What is active, blocked, decided, or verified?" |
| Knowledge map | selected topic/global concept | "Which claims, sources, and disagreements surround this idea?" |
| Decision trail | timeline plus dependencies | "What changed this direction and what evidence led there?" |
| Promotion review | outbox candidates only | "What can safely become reusable knowledge?" |

### 9.2 Visual grammar

- Node color represents kind and scope, not model confidence alone.
- Node border represents lifecycle/freshness: active, partial, stale, disputed,
  superseded, or archived.
- Edge style represents relation: solid for direct typed relation, dashed for
  cross-store relation, warning color for contradiction or invalidation.
- Edge labels appear on focus/hover; the full graph starts with labels hidden.
- Hubs are collapsed into a summary node until the user expands them.
- The default view excludes generic related_to edges, archived objects, and
  low-confidence suggestions.
- Every click opens provenance, revision, source/evidence count, and a human
  explanation of the relation type.

### 9.3 Avoiding the hairball

The screenshot-style all-vault graph is useful as a map only after filtering.
V2 must not open there by default. It uses scoped views, hop limits, relation
filters, time windows, topic filters, and degree caps. An orphan report is more
useful than drawing every possible semantic resemblance.

The visualization consumes generated graph.json snapshots. It can later be a
local web UI, desktop panel, or export; the storage and relation contract stay
the same.

## 10. Front-door root model routing

### 10.1 The timing constraint

An agent cannot choose a cheaper root model without first receiving the request,
but once the current root model receives the request, that session is already
running. Therefore true automatic root routing must be performed by a launcher,
proxy, or Codex session-creation integration before the task starts.

Inside an existing Tera Max session:

- DIRECT stays in the root. Spawning a worker merely to answer 1 + 1 costs more
  than it saves.
- A valid GRAPH can select smaller profiles for bounded worker nodes.
- The root model does not mutate itself into Tera High, Sol, or Luna.

### 10.2 Router contract

The front-door router receives a transient RouteRequest, makes a conservative
choice, creates a session with a named approved profile, and emits a redacted
route intent receipt. It must not persist the prompt body.

~~~text
new request
  -> explicit user override?
  -> deterministic risk/scope classifier
  -> selected root profile and effort
  -> create Codex session with that profile
  -> correlate intent, serialized request, and router log
~~~

Priority order:

1. Explicit user profile choice wins when allowed by safety policy.
2. High-risk, ambiguous, multi-step, or cross-project work uses Tera Max.
3. Bounded read-heavy explanation/log triage uses Tera High.
4. Scoped implementation with a clear contract can use Sol XHigh.
5. Difficult multi-file/debugging work uses Sol Max.
6. A deterministic, finite, non-sensitive utility can use Luna XHigh only if
   every Luna hard gate passes.
7. Uncertainty always falls back to Tera Max.

The initial classifier is rule-first, not an extra model call for every short
prompt. A model-based classifier is considered only if measurements show that
its quality/cost trade-off beats the rules.

### 10.3 Profiles and examples

| Request shape | Root or worker choice | Reason |
| --- | --- | --- |
| one-line stable explanation | Tera High root through launcher | fast general reasoning |
| architecture or ambiguous plan | Tera Max root | integration and judgment |
| focused API/refactor task | Sol XHigh worker or root if launched there | implementation contract |
| hard multi-file debugger | Sol Max worker or root if launched there | heavy tool use |
| finite extraction/classification | Luna XHigh worker only when gates pass | utility work |
| independent adversarial review | GPT-5.5 XHigh worker | second perspective |

The existing pixel-profile custom agents and config profiles remain the mapping
source. A new automatic launcher may select them, but it must not invent
additional model slugs or reasoning efforts.

### 10.4 9router evidence

Before any automatic routing default is enabled, every route class needs an
evaluation fixture and a correlation-bound canary. A route receipt binds:

- selected profile and intended effort;
- registry and adapter version;
- correlation ID;
- serialized outbound model/effort fields;
- operator-trusted 9router observed fields; and
- a visible mismatch or missing-telemetry status.

The current Practical V1 router verifier proves only the trusted router's
outbound report. It does not prove remote provider internal reasoning or model
behavior. V2 must preserve that limitation and the existing strict signed
upstream evidence path.

## 11. Delivery surfaces

| Surface | Role in V2 | Must not do |
| --- | --- | --- |
| Global AGENTS.md | policy and admission guidance | store data or switch root model |
| Second Brain skill | bounded operating procedure | capture raw transcript |
| Hook configuration | lifecycle signal and mechanical checks | become semantic authority |
| Private runtime | authoritative stores and receipts | enter public Git history |
| Local graph UI | read generated graph snapshot | mutate authority without transaction |
| Front-door launcher | choose root profile before session creation | infer permission or bypass safety |
| 9router verifier | compare route evidence | claim provider attestation |

The hook script, launcher, store initializer, graph renderer, and any new global
skill content are future artifacts. Their exact paths, modes, before/after
digests, permissions, network behavior, backup, rollback, and canary must be
listed in a fresh approval packet before installation.

## 12. Rollout plan

The activation tracks below do not redefine M8/M9. They are an opt-in extension
whose promotion remains blocked by their own acceptance evidence and by all
existing global-change rules.

| Phase | Capability | Write authority | Exit evidence |
| --- | --- | --- | --- |
| A0 | Design fixtures and threat model | none | schema, privacy, and failure fixtures pass |
| A1 | Private runtime initialization | empty private stores only | path/symlink, backup, restore, and no-Git-leak checks pass |
| A2 | Observe-only lifecycle integration | receipts only | event payload allowlist and no-transcript test pass |
| A3 | Assisted recovery/read and closure proposals | project/global proposal only | relevance, omission, latency, and proposal-review tests pass |
| A4 | Opt-in PROJECT_AUTO | constrained project transaction | CAS, dedupe, secret filter, rollback, and user-visible closure tests pass |
| A5 | Typed graph and local visualization | derived graph/index only | graph invariants, view performance, and no-authority UI tests pass |
| A6 | Front-door router in shadow mode | no default change | route corpus, cost/latency, mismatch, and fallback evidence pass |
| A7 | Per-project opt-in defaults | approved scoped global config | exact packet, backup, canary, readback, and rollback evidence pass |
| A8 | Global promotion workflow | reviewed global transaction | provenance, review, cross-project leakage, and restore evidence pass |

Every phase stops on route mismatch, secret detection, unexpected capture,
cross-project leakage, quality regression, source drift, or failed rollback.
Later phases do not excuse skipped evidence in earlier phases.

The project-local A0 schema, synthetic fixture, threat-model, and fail-closed
test evidence is recorded in [the A0 fixture and threat-model checkpoint](25-ACTIVATION-V2-A0-FIXTURES-AND-THREAT-MODEL.md). It does not initialize a
runtime or authorize A1, hooks, automatic capture, routing, or a global change.

A1 adds only the separately documented [disposable synthetic runtime](26-ACTIVATION-V2-A1-DISPOSABLE-RUNTIME.md). Its root is restricted to ignored
test artifacts and it cannot initialize the future private `runtime/` path or
any user/global target.

A2 records only synthetic observe-only metadata receipts, and A3 adds only
[synthetic assisted recovery and closure proposals](28-ACTIVATION-V2-A3-SYNTHETIC-RECOVERY-AND-PROPOSALS.md).
Neither phase installs a hook, opens an authority store, commits a project or
global object, or changes a session route.

A4 adds a [fixture-only PROJECT_AUTO state machine](29-ACTIVATION-V2-A4-SYNTHETIC-PROJECT-AUTO.md)
with CAS, dedupe, synthetic rollback, and content-free closure status. It does
not enable automatic capture or an authority transaction for any real project.

## 13. Evaluation and acceptance criteria

### 13.1 Memory

- DIRECT opens no store and writes no receipt containing task text.
- Project recovery recalls the active goal, relevant decision, and current
  blocker for a fixed corpus without importing irrelevant historical data.
- A stale or disputed record appears with a warning rather than disappearing.
- A rejected candidate leaves no partial object, relation, event, or index
  mutation.
- Secret, prompt-injection, raw-transcript, absolute-path, and cross-project
  fixtures are rejected or redacted according to policy.

### 13.2 Graph

- Every displayed durable edge resolves to objects/revisions and an allowed
  relation type.
- Part_of and supersedes cycle fixtures fail closed.
- Focus graph returns a bounded result within an agreed local latency budget.
- A generated graph rebuilt from the same authority snapshot is byte-equivalent.
- A global view never reveals private project content beyond explicitly approved
  provenance metadata.

### 13.3 Routing

- A fixed prompt corpus maps to the expected profile or a documented fallback.
- Explicit profile selection is preserved when safe.
- Unknown or ambiguous requests fail to Tera Max instead of a cheap profile.
- Serialized effort and 9router outbound observations either match or produce a
  visible failure receipt.
- The automatic classifier consumes less latency/cost than the work it avoids
  and does not reduce agreed quality below the Tera Max baseline.

### 13.4 Operations

- Backup and restore are tested against a disposable runtime before opt-in.
- Every global install has a current exact packet and a separately exercisable
  rollback plan.
- Read-only failure degrades to ordinary Codex work; it never blocks a simple
  answer indefinitely.

## 14. Obsidian migration policy

The old Obsidian graph is a useful visual reference, not an automatic import
source. V2 starts with empty stores and synthetic fixtures. If Pixel later asks
to migrate a named vault, the migration is a separate project with:

1. read-only inventory and explicit inclusion/exclusion rules;
2. secret and instruction quarantine;
3. copy-only dry run into a disposable runtime;
4. relation/type review and duplicate detection;
5. explicit user approval before any authoritative import; and
6. rollback that never modifies the original vault.

The graph renderer can eventually export Markdown-style pages or read a
carefully reviewed import, but Obsidian is neither required nor authoritative.

## 15. Decisions required before implementation

The following choices need explicit user direction before A1 or later:

1. **Retention:** how long project closure receipts, outbox entries, backups,
   and generated graph snapshots are retained.
2. **Capture default:** whether new projects begin OFF, ASSISTED, or
   PROJECT_AUTO. Recommended default: ASSISTED.
3. **Global promotion:** whether review is per-item, daily batch, or manual-only.
   Recommended default: per-item for early rollout.
4. **Graph UI:** local web app first, or generated Markdown plus a later app.
   Recommended default: local web focus/project/knowledge views.
5. **Router entry point:** CLI launcher first, desktop integration when a safe
   supported session-creation surface is verified, or both. Recommended default:
   CLI launcher shadow mode first.
6. **Storage protection:** rely on encrypted disk/keychain, or define an
   application-level encryption/key rotation design before personal data exists.

No answer is assumed merely because a prior conversation mentioned a preference.

## 16. Initial implementation backlog

The first implementation session should produce a design-to-code plan and
fixtures before any global activation:

1. Define TaskClosure, capture receipt, promotion outbox, route intent, and
   graph snapshot schemas.
2. Implement private runtime initializer and disposable test runtime.
3. Implement redaction/allowlist and candidate validator with adversarial tests.
4. Implement deterministic graph snapshot compiler and focus-graph query.
5. Implement observe-only hook adapters with exact payload fixtures.
6. Implement a front-door router in shadow mode that emits selections but creates
   no alternate session yet.
7. Build evaluation corpora for memory relevance, graph correctness, and routing.
8. Prepare an exact global approval packet only after local evidence passes and
   the user has chosen the decisions in section 15.

Suggested profile allocation for a future graph implementation is:

| Work | Profile | Why |
| --- | --- | --- |
| integration, safety decisions, final contract | Tera Max | root orchestration |
| schema/store implementation | Sol Max or Sol XHigh | difficult/scoped implementation |
| graph UI and fixtures | Sol XHigh | bounded implementation |
| read-heavy contract analysis | Tera XHigh | synthesis and diagnosis |
| finite fixture generation | Luna XHigh | only deterministic, non-sensitive utilities |
| independent security/privacy review | GPT-5.5 XHigh | separate adversarial perspective |

## 17. Definition of done for Activation V2

Activation V2 is ready for per-project opt-in only when all of the following are
true:

- private stores are initialized, backed up, restorable, ignored by Git, and
  inaccessible through unsafe paths or symlinks;
- no raw chat/transcript/secret capture is possible through hooks, closure, or
  receipts;
- project/global authority and cross-store link rules pass adversarial tests;
- graph snapshots are deterministic, bounded, and visibly explain provenance;
- routing starts before session creation, preserves user overrides, and fails to
  Tera Max when uncertain;
- 9router intent/serialization/outbound evidence is correlated and mismatch is
  fail-visible;
- all global changes have a fresh exact approval packet, backup, canary,
  readback, and approved rollback path; and
- M8/M9 remain truthfully represented as blocked wherever their original
  evidence remains absent.

Until then, Practical V1 remains the safe daily bridge: explicit targeted reads,
proposal-only promotion, direct-path memory avoidance, and no provider claim.

## 18. Local implementation checkpoints

| Phase | Local evidence | Still forbidden |
| --- | --- | --- |
| A0 | Five fixture schemas, public synthetic threat corpus, and fail-closed contract tests. | Runtime initialization, hooks, capture, routing, and global change. |
| A1 | Fixture-only policy/manifest schemas and a private disposable runtime with path, symlink, backup, restore, content-boundary, and Git-ignore tests. | `runtime/` initialization, persistent user memory, policy defaults, hooks, launcher/session creation, provider calls, and global mutation. |
| A2 | Exact synthetic observe-only event fixtures and metadata-only, digest-bound lifecycle receipts. | Hook installation, prompt/transcript/tool-output capture, authority writes, session routing, and global mutation. |
| A3 | Explicit synthetic assisted-recovery corpus, proposal-only closure flow, omission/freshness evidence, and latency/cross-project fail-closed tests. | Authority-store writes, real task capture, global promotion acceptance, hook installation, provider calls, and global mutation. |
| A4 | Fixture-only PROJECT_AUTO state machine with CAS, dedupe, synthetic rollback, and content-free closure status. | Real project transactions, automatic capture, global-promotion acceptance, hooks, provider calls, and global mutation. |
| A5 | Deterministic, bounded two-hop focus graph fixture with retained revision/provenance metadata and no-authority derived JSON output. | Graph server/UI installation, authority edits, real graph data, hooks, provider calls, and global mutation. |
