# Product Requirements Document

- Status: Blueprint v1 complete; implementation pending
- Product: Pixel Global Second Brain and Codex Workflow
- Primary owner: Pixel
- Default root model: Tera Max

## 1. Ringkasan Produk

Produk ini menyediakan workflow Codex global yang memilih effort, model, memory,
skill, plugin, MCP, dan orkestrasi sesuai kebutuhan tiap permintaan. Ia juga
menyediakan second brain global yang mengubah sumber mentah menjadi wiki semantic
yang dipelihara, serta recovery memory terpisah untuk setiap proyek.

Tujuan utamanya adalah menghilangkan dua kegagalan workflow lama:

1. **Under-memory:** konteks penting hilang setelah sesi baru atau compaction.
2. **Over-processing:** pertanyaan sederhana berubah menjadi riset dan validasi
   panjang karena setiap task menjalankan pipeline yang sama.

Solusinya bukan memilih salah satu ekstrem. Produk harus memberi jalur langsung
untuk pekerjaan mudah dan jalur graph yang disiplin untuk pekerjaan kompleks,
dengan context yang dikompilasi sesuai kebutuhan.

## 2. Problem Statement

### 2.1 Memory

- Context window besar tetap terbatas dan tidak durable.
- Handoff saja hanya menyimpan keadaan kerja, bukan pengetahuan yang berkembang.
- Vault besar tidak berguna jika seluruhnya dibaca atau hasil retrieval tidak
  dapat dipercaya.
- Implementasi `ai-memory` lama kuat pada recovery dan security, tetapi objek yang
  paling relevan dapat hilang dari retrieval ketika satu code reference stale.
- Index derived dapat tertinggal dari Markdown authoritative.
- MOC datar dan object kinds operasional belum membentuk wiki lintas domain.

### 2.2 Workflow

- Pengguna tidak ingin mengingat katalog model, skill, plugin, atau MCP.
- Routing statis membuat semua task terlalu berat atau terlalu dangkal.
- Pipeline linear membuang waktu ketika beberapa pekerjaan independen dapat maju
  bersamaan.
- Graph yang dipakai untuk semua task menciptakan latency, noise, dan biaya tanpa
  peningkatan kualitas.
- Model fallback atau capability failure dapat menurunkan hasil diam-diam jika
  tidak ada quality gate.

## 3. Target Users dan Persona

### Persona A: Pixel, Solo Builder

Membangun software, melakukan riset, dan berpindah antar proyek. Ingin menjelaskan
tujuan sekali, tidak menghafal tool, dan tetap bisa mengaudit keputusan penting.
Sensitif terhadap workflow yang lambat atau terlalu seremonial.

### Persona B: Root Agent

Partner utama pengguna. Menjaga intent, memilih execution lane, mengatur bounded
context, mengintegrasikan specialist output, melakukan final verification, dan
memutuskan durable memory write.

### Persona C: Specialist Agent

Mengerjakan satu bounded node seperti research, backend, frontend, security, atau
review. Membutuhkan input/output contract yang sempit dan tidak memiliki authority
untuk mengubah scope utama atau menulis memory final secara sepihak.

### Persona D: Operator/Reviewer

Memeriksa kesehatan memory, index, capability registry, routing, audit events,
quality benchmarks, dan release gates. Persona ini bisa manusia atau task review
khusus, tetapi keputusan berisiko tetap mengikuti authority pengguna.

## 4. Jobs to Be Done

| ID | Ketika... | Saya ingin... | Agar... |
| --- | --- | --- | --- |
| JTBD-01 | saya bertanya sesuatu yang sederhana | mendapat jawaban langsung | saya tidak menunggu pipeline yang tidak perlu |
| JTBD-02 | saya memberi task implementasi | sistem memilih tool dan depth sendiri | saya tidak perlu menghafal capability |
| JTBD-03 | task memiliki cabang independen | cabang dijalankan sebagai dependency graph | waktu total turun tanpa kehilangan integrasi |
| JTBD-04 | saya membuka sesi baru | state proyek yang current dimuat secara bounded | pekerjaan berlanjut tanpa membaca transcript lama |
| JTBD-05 | saya menyimpan artikel, repository, atau diskusi | source diingest dan memperbarui wiki | pengetahuan bertambah, bukan hanya menambah arsip |
| JTBD-06 | sumber baru berbeda dengan pengetahuan lama | konflik dan freshness terlihat | saya tidak memakai klaim obsolete sebagai fakta |
| JTBD-07 | saya meminta sintesis | jawaban memakai evidence relevan dan citation | saya dapat menilai keandalannya |
| JTBD-08 | model/tool utama gagal | sistem memilih fallback yang aman | task tetap maju tanpa downgrade diam-diam |

## 5. User Stories

### US-01: Simple Direct Answer

Sebagai pengguna, ketika saya bertanya kalkulus dasar atau konsep stabil, saya
ingin Tera Max menjawab langsung tanpa mencari memory, web, plugin, atau membuat
work graph kecuali pertanyaan secara eksplisit meminta sumber/current data.

### US-02: Assisted Project Change

Sebagai pengguna, ketika saya meminta perubahan pada satu proyek, saya ingin root
membaca aturan lokal dan recovery state yang relevan, memakai skill/tool minimum,
mengimplementasikan perubahan, menjalankan verification yang proporsional, dan
mencatat hanya keputusan atau evidence durable.

### US-03: Parallel Complex Build

Sebagai pengguna, ketika pekerjaan memiliki beberapa artifact independen, saya
ingin root membuat DAG dengan owner, dependency, write scope, deliverable, dan
acceptance gate; menjalankan node paralel; lalu mengintegrasikan serta mereview
hasil sebelum menganggap task selesai.

### US-04: Knowledge Ingest

Sebagai pengguna, ketika saya memberikan URL, file, repository, atau note, saya
ingin bytes asli dan metadata disimpan immutable, klaim diekstrak sebagai proposal,
entity/concept yang relevan diperbarui, citation dipertahankan, dan lint dijalankan.

### US-05: Knowledge Query

Sebagai pengguna, ketika saya bertanya lintas sumber, saya ingin sistem mengambil
subset yang relevan, menampilkan uncertainty/contradiction yang material, dan
menghasilkan sintesis yang dapat ditelusuri tanpa memasukkan seluruh vault.

### US-06: Fresh Session Recovery

Sebagai pengguna, ketika sesi dimulai ulang atau compact, saya ingin menerima
current objective, active decisions, blockers, touched areas, dan verification
state dalam packet kecil. Global knowledge hanya ditambahkan jika task saat ini
memerlukannya.

### US-07: Capability Discovery

Sebagai pengguna, ketika capability yang diperlukan belum terpasang, saya ingin
root menjelaskan manfaatnya dan meminta instalasi/persetujuan seperlunya, bukan
menginstal atau mengaktifkan global integration secara diam-diam.

## 6. Scope Produk

### 6.1 In Scope untuk V1

- Global custom instructions dengan root Tera Max dan admission policy.
- Request classification menjadi `DIRECT`, `ASSISTED`, `GRAPH`, atau `DEEP`.
- Registry dan conservative auto-selection untuk model, skill, plugin, app, MCP,
  dan tool lokal.
- Work graph berbentuk DAG untuk task yang lolos admission.
- Global Knowledge Plane dengan ingest, query, update/compile, dan lint.
- Immutable raw sources dan maintained semantic wiki.
- Project Recovery Plane berdasarkan kernel `ai-memory` yang diperbaiki.
- Bounded context compilation dengan provenance dan budget.
- Freshness per claim/reference, contradiction, supersession, dan reverification.
- Human-readable authority files dan rebuildable derived indexes.
- Audit event, metrics, benchmark, canary, rollback, dan degraded operation.
- Read-only migration tooling dari `ai-memory` lama; import eksplisit dan dapat
  diverifikasi.

### 6.2 Deferred

- Multi-user collaboration dan permission model lintas akun.
- Automatic cross-device sync selain mekanisme filesystem/Git yang dipilih user.
- Fully autonomous maintenance schedule tanpa user-controlled automation.
- Vector database atau embeddings sebagai dependency wajib.
- Writable third-party code-memory provider.
- GUI khusus; Markdown/CLI/Codex interaction cukup untuk V1.

## 7. Functional Requirements

### FR-01: Request Admission

Sistem MUST mengklasifikasikan permintaan berdasarkan ambiguity, novelty,
freshness need, blast radius, reversibility, artifact count, dependency, dan
required evidence.

- `DIRECT`: root dapat menjawab/mengerjakan sendiri tanpa external retrieval.
- `ASSISTED`: satu execution thread dengan capability atau retrieval terbatas.
- `GRAPH`: DAG bounded dengan minimal dua branch independen atau measurable
  parallel benefit.
- `DEEP`: high-risk, destructive, security, migration, adversarial review, atau
  explicit deep scope yang membutuhkan gate tambahan; boleh memakai graph.

Klasifikasi MUST dapat dinaikkan saat evidence baru meningkatkan risiko dan dapat
diturunkan sebelum execution jika kompleksitas awal tidak terbukti.

### FR-02: Direct-Path Guardrail

Untuk prompt yang lolos direct-answer fixture, sistem MUST:

- tidak melakukan web research, global memory retrieval, atau graph;
- tidak menjalankan plugin/MCP yang tidak diminta;
- tidak membuat planning ceremony yang terlihat;
- tetap boleh menggunakan kalkulasi atau read-only local check yang langsung
  diperlukan untuk menjawab dengan benar.

### FR-03: Root Ownership

Root Tera Max MUST menjadi owner default task dan jawaban akhir. Root MUST menjaga
user intent, decision log sementara, dependency state, integration, dan final
quality gate. Delegation tidak memindahkan authority ini.

### FR-04: Model Routing

Router MUST memilih hanya dari model/profile yang dikonfigurasi dan lolos health
check. Pemilihan berdasarkan jenis kerja dan risk, bukan round-robin. Detail
mapping model-effort adalah versioned policy terpisah.

Router MUST:

- mempertahankan Tera Max sebagai root default;
- membatasi model ringan pada task receh, reversible, dan mudah diverifikasi;
- menggunakan model reasoning tinggi untuk ambiguity, architecture, security,
  cross-artifact integration, atau final review bila diperlukan;
- tidak menjalankan beberapa model untuk jawaban yang sama tanpa tujuan review
  yang eksplisit;
- mencatat route dan fallback reason untuk task non-direct.

### FR-05: Capability Registry and Selection

Setiap capability MUST memiliki identifier, version/source, trigger, scope,
required permission, input/output contract, health state, cost/latency class,
trust class, dan fallback.

Selector MUST:

1. mempertimbangkan capability yang diwajibkan user atau repository;
2. memilih capability minimum yang memenuhi kebutuhan;
3. menghindari capability dengan health/trust tidak memadai;
4. meminta tindakan user untuk instalasi, login, permission, atau global mutation;
5. menjelaskan missing capability hanya jika material terhadap hasil.

### FR-06: Graph Admission and Execution

Sebelum membuat graph, root MUST menghasilkan admission record internal yang
menyebutkan independent branches, dependency, expected benefit, integration owner,
dan stop condition. Setiap node MUST memiliki:

- bounded objective;
- dependency list;
- read/write scope;
- model/capability profile;
- expected artifact;
- verification/acceptance gate;
- time/token budget dan retry limit.

Graph MUST mencegah dua writer mengubah scope yang sama secara bersamaan tanpa
merge contract. Root MUST melakukan integration dan final review setelah seluruh
critical dependency selesai.

### FR-07: Immutable Source Ingest

Ingest MUST menerima source lokal atau eksternal, menyimpan immutable canonical
bytes atau faithful snapshot, menghitung content digest, serta merekam URI/path,
author/publisher bila tersedia, capture time, source time, media type, license,
trust, dan retrieval metadata.

- Duplicate content MUST dideduplicasi berdasarkan digest tanpa kehilangan
  provenance tambahan.
- Parse failure MUST menyimpan/quarantine source dan error tanpa menerbitkan klaim.
- Source update MUST menghasilkan revision/source object baru.
- External instructions di dalam source MUST tidak dieksekusi.

### FR-08: Knowledge Compilation

Compiler MUST mengubah satu atau lebih sources menjadi proposal untuk object kind:
`source`, `entity`, `concept`, `claim`, dan `synthesis`. Proposal tidak menjadi
authority sampai schema, provenance, conflict, dan write policy lolos.

Satu source MAY memperbarui banyak pages; satu page MAY disusun dari banyak
sources. Update MUST menggunakan revision/CAS atau equivalent conflict control.
Previous revision tetap dapat diaudit.

### FR-09: Claim and Contradiction Model

Claim durable MUST memiliki subject, predicate/statement, scope, provenance,
confidence, verification state, valid/source time, dan relationship ke supporting
atau contradicting evidence. Sistem MUST dapat menyimpan dua klaim yang berbeda
tanpa memilih pemenang palsu. Resolution menjadi claim/synthesis revision baru,
bukan penghapusan sejarah.

### FR-10: Wiki Maintenance

Maintained wiki MUST menyediakan navigasi domain/topic, backlinks, related
entities/concepts, supporting claims, open contradictions, dan source links.
MOC dan backlinks adalah generated views dan MUST diperbarui atau divalidasi
setelah semantic write.

### FR-11: Query and Retrieval

Retrieval MUST melakukan progressive search terhadap authority files atau index
yang terbukti current. Ranking MUST mempertimbangkan relevance, authority,
freshness, verification, project scope, dan source diversity.

Jika index hilang atau digest tidak cocok, query MUST fallback ke authoritative
scan atau membangun ulang index. Index stale tidak boleh dipercaya diam-diam.

### FR-12: Partial Staleness

Freshness MUST disimpan dan dihitung per claim/reference. Query MUST dapat
menghasilkan state mesin `fresh`, `partial`, `stale`, `unverifiable`, atau
`not_applicable`, terpisah dari epistemic state seperti `disputed`. Presentation
layer MAY memetakan kombinasi lifecycle/freshness/integrity menjadi `current`,
`partially_stale`, `historical`, atau `invalid`. Evidence paling relevan yang
`partial`/`partially_stale` SHOULD tetap muncul dengan affected references dan
warning. Full exclusion hanya boleh terjadi karena scope, explicit lifecycle,
integrity failure, atau policy yang dapat dijelaskan.

### FR-13: Bounded Context Compilation

Context compiler MUST:

- menerima task intent, project scope, route, dan token budget;
- memilih minimal sufficient evidence;
- mempertahankan object IDs, source citations, freshness, dan uncertainty;
- melakukan deduplication dan diversity control;
- memisahkan instruction dari retrieved data;
- menolak whole-vault injection;
- mendukung retrieval round berikutnya bila packet awal belum cukup.

Default budget terdapat di bagian NFR dan MUST configurable per model context
window.

### FR-14: Project Recovery

Setiap project yang diaktifkan MUST memiliki authority store terpisah untuk active
objective, user decisions, tasks, blockers, components, bugs, experiments, dan
verification evidence. Recovery packet MUST task-specific dan tidak otomatis
memuat global wiki.

Kernel awal SHOULD mempertahankan pola aman dari `ai-memory`: single authoritative
writer, atomic writes, event ledger, CAS, signed lifecycle receipt, bounded pack,
dan rebuildable index. Implementasi MUST memperbaiki whole-record staleness,
stale-index trust, dan MOC regeneration sebelum migration dianggap berhasil.

### FR-15: Selective Memory Commit

Setelah response atau material event, root MUST menilai apakah ada durable write.
Yang boleh disimpan mencakup source baru, user decision, verified fact, reusable
claim/synthesis, active blocker, atau verification evidence. Sistem MUST tidak
menyimpan greeting, raw chain-of-thought, secret, seluruh transcript, atau detail
sementara yang tidak berguna lintas sesi.

### FR-16: Lint

Lint MUST memeriksa sekurangnya:

- schema dan identifier validity;
- missing/broken links dan dangling relation;
- orphan entity/concept/synthesis;
- claim tanpa source atau provenance;
- contradiction yang belum diberi status;
- stale reference dan stale index;
- duplicate/near-duplicate objects;
- supersession/revision inconsistency;
- secret atau instruction-injection pattern;
- MOC/backlink drift;
- project/global boundary violation.

Lint MUST membedakan blocking error dari advisory warning.

### FR-17: Explainability and User Control

Pengguna MUST dapat memaksa atau melarang web research, memory, capability,
model/profile, graph, atau durable write untuk task saat ini. Explicit user choice
mengalahkan automatic routing selama tidak melanggar safety/permission.

Untuk non-direct task, audit artifact MUST cukup untuk menjawab route, capability,
evidence, fallback, dan memory write yang terjadi. Jawaban pengguna tetap ringkas;
detail audit tersedia on demand.

### FR-18: Failure and Degraded Operation

Setiap external dependency MUST memiliki known failure state dan fallback. Sistem
MUST membedakan:

- integrity/authority failure yang memblokir operasi;
- quality-critical dependency failure yang memerlukan reroute atau user notice;
- optional acceleration failure yang boleh fallback secara transparan.

Tidak ada empty result yang boleh otomatis ditafsirkan sebagai "tidak ada
pengetahuan" jika index atau retrieval health tidak terbukti.

### FR-19: Observability and Audit

Sistem MUST merekam structured event untuk route non-direct, graph admission,
capability activation, external retrieval, authoritative write, revision,
fallback, validation result, context packet metadata, dan error. Log MUST bounded,
redacted, dan tidak menyimpan chain-of-thought atau credentials.

### FR-20: Migration from `ai-memory`

Migration MUST bersifat read-only terhadap sumber, menghasilkan manifest dan
dry-run report, memisahkan operational records dari reusable knowledge, menjaga
stable IDs/provenance bila valid, dan menolak import ambigu tanpa review. Old
Obsidian handoff/plugin state bukan sumber migration otomatis.

Cutover hanya lolos setelah fixture membuktikan recovery recall, partial-stale
behavior, index rebuild, link integrity, dan rollback.

## 8. Non-Functional Requirements

### NFR-01: Quality Regression

Pada benchmark representative, candidate workflow MUST tidak turun lebih dari 2
percentage points dari high-quality reference baseline pada correctness/completeness
aggregate, dan MUST tidak turun sama sekali pada safety-critical assertions.

### NFR-02: Direct Latency

Sedikitnya 95% fixture `DIRECT` MUST selesai tanpa network, global memory query,
atau graph. Routing overhead lokal p95 SHOULD di bawah 1 detik, di luar model
generation time.

### NFR-03: Context Efficiency

Budget default untuk **retrieved memory/evidence payload**, sebelum ada tuning
berbasis evidence:

| Retrieval tier | Default ceiling | Behavior |
| --- | ---: | --- |
| direct | 0 memory token | retrieval berarti task harus direklasifikasi |
| project recovery | min(8K token, 2.5% active window) | trim derived detail, jangan trim active decision/blocker |
| scoped/assisted retrieval | min(12K token, 5% active window) | lakukan retrieval round baru bila diperlukan |
| graph/deep retrieval | min(24K token, 8% active window) | split packet atau lakukan bounded follow-up retrieval |

Angka ini bukan total prompt/working-set lane; contract workflow boleh menetapkan
envelope lebih besar untuk code, instructions, dan artifact integration. Retrieved
payload boleh melewati ceiling hanya dengan reason yang tercatat. Sistem MUST
tidak mengisi context hanya karena window masih tersedia.

### NFR-04: Recovery Reliability

Critical recovery recall MUST 100% pada canonical fixtures dan minimal 99% pada
longitudinal canary. Silent stale omission MUST 0. Recovery injection MUST tetap
di bawah configured hard budget.

### NFR-05: Retrieval Quality

Pada curated query set:

- critical relevant evidence recall@bounded-context >= 0.95;
- irrelevant object rate di compiled packet <= 0.15;
- provenance coverage untuk material claims = 100%;
- stale/contradiction label accuracy >= 0.95;
- empty-result health classification = 100%.

### NFR-06: Graph Efficiency

Graph yang di-admit otomatis MUST menghasilkan salah satu manfaat yang terukur:
wall-clock improvement >= 20% dibanding estimasi sequential, isolation of
conflicting scopes, atau independent risk review yang diwajibkan. Graph yang
dipaksa pengguna diberi reason `user_explicit_graph` dan dikecualikan dari metric
benefit. Pada simple-task benchmark, false automatic graph admission MUST <= 2%.

### NFR-07: Capability Precision

Pada capability-selection fixture:

- required capability recall >= 0.95;
- unnecessary external capability activation <= 0.05;
- unauthorized installation/global mutation = 0;
- missing health/permission disclosure untuk dependency material = 0 kasus.

### NFR-08: Durability and Rebuild

Authoritative write MUST atomic dan revision-safe. Menghapus seluruh derived state
kemudian rebuild MUST menghasilkan semantic query result ekuivalen. Raw source
digest mismatch atau silent mutation MUST 0.

### NFR-09: Security and Privacy

Cross-project private data leakage, secret persistence, instruction execution dari
retrieved content, dan unauthorized authoritative write MUST 0 pada adversarial
suite. File privat dan runtime key MUST mengikuti least privilege.

### NFR-10: Portability

Authority data MUST tetap dapat dibaca tanpa aplikasi khusus. Model/provider,
index, dan capability adapters MUST replaceable melalui versioned contracts.

### NFR-11: Availability

Kerusakan index, embeddings, optional plugin, network, atau satu specialist MUST
tidak membuat direct/local workflow tidak tersedia. Integrity atau authority
failure boleh fail closed dengan recovery instruction yang jelas.

### NFR-12: Maintainability

Schema dan policy changes MUST versioned, memiliki migration dan rollback test,
serta didokumentasikan lewat ADR bila mengubah authority boundary, routing rule,
atau user-visible behavior.

## 9. UX Rules

1. Jawaban dimulai dengan outcome, bukan laporan orkestrasi.
2. Direct task tidak menampilkan plan, routing, atau memory ceremony.
3. Progress update hanya diberikan untuk pekerjaan yang benar-benar berlangsung
   lama atau memiliki milestone material.
4. Sistem tidak bertanya jika reasonable low-risk assumption cukup; assumption
   material disebutkan dengan ringkas.
5. Pengguna tidak perlu menyebut nama skill/plugin/MCP agar sistem memakainya.
6. Pengguna selalu dapat berkata "jawab langsung", "jangan riset", "gunakan
   source", "jangan simpan", atau "pakai graph"; router mengikuti kecuali ada
   conflict safety/permission.
7. Warning hanya muncul bila memengaruhi confidence, completeness, permission,
   atau tindakan berikutnya.
8. Citation harus berada dekat dengan klaim material dan mengarah ke source yang
   benar, bukan ke search result semata.
9. Bila hasil partially stale, sistem menjawab bagian yang masih didukung lalu
   menandai bagian yang perlu reverification.
10. Bila fallback menurunkan quality floor, sistem berhenti atau meminta pilihan;
    bila hanya menurunkan kenyamanan/kecepatan, sistem lanjut dan melaporkannya.

## 10. Success Metrics

| Metric | V1 release target |
| --- | ---: |
| Eligible simple prompts completed via `DIRECT` | >= 95% |
| False graph admission on simple suite | <= 2% |
| Unnecessary external capability activation | <= 5% |
| Critical project recovery recall | 100% canonical; >= 99% canary |
| Silent omission caused by partial staleness | 0 |
| Material external claims with provenance | 100% |
| Cross-project leakage / secret persistence | 0 |
| Quality delta versus reference baseline | >= -2 pp aggregate; 0 safety regression |
| Compiled-context irrelevant object rate | <= 15% |
| Derived-index clean rebuild equivalence | 100% canonical fixtures |
| Immutable source digest preservation | 100% |
| Unauthorized plugin/MCP/global config mutation | 0 |

Metrics MUST dihitung dari artifacts dan fixtures, bukan self-rating model.

## 11. Non-Goals Produk

- Menjawab semua pertanyaan melalui research pipeline.
- Menjalankan multi-agent hanya agar terlihat canggih.
- Menyimpan seluruh chat sebagai long-term knowledge.
- Menjamin kebenaran sumber eksternal tanpa verifikasi.
- Menulis ke global memory dari specialist tanpa root acceptance.
- Menggunakan Luna atau model ringan untuk keputusan berisiko yang tidak mudah
  diverifikasi.
- Menginstal plugin/MCP secara otomatis.
- Menjadikan search/vector similarity sebagai pengganti authority dan provenance.
- Mengimpor seluruh sistem lama tanpa klasifikasi serta lint.
- Menghapus historical contradiction atau superseded decision dari audit trail.

## 12. Dependencies dan Assumptions

- Sistem ditujukan untuk single-user trusted host pada V1.
- Codex tetap menerapkan system/developer/repository instructions di atas memory.
- Tersedia filesystem local untuk authority records dan runtime state.
- Model/profile names dapat berubah; routing menggunakan logical profile yang
  dipetakan melalui config versioned.
- Web, MCP, plugin, dan app bersifat optional dependency kecuali task secara
  eksplisit memerlukannya.
- Obsidian MAY menjadi human interface, tetapi bukan runtime dependency.

## 13. Release Acceptance Criteria

V1 hanya dapat dinyatakan siap bila seluruh kondisi berikut terbukti:

- AC-01: Direct suite menunjukkan target latency/admission dan tidak memicu
  unnecessary retrieval/capability.
- AC-02: Assisted suite memilih minimal sufficient capabilities dan menghasilkan
  verification sesuai risk.
- AC-03: Graph suite membuktikan dependency ordering, parallel branches, write
  isolation, integration, cancellation, dan final review.
- AC-04: Ingest fixture menyimpan immutable source, membuat proposals, memperbarui
  beberapa wiki pages, mempertahankan citations, dan lulus lint.
- AC-05: Query fixture memenuhi recall, precision, provenance, context budget,
  contradiction, dan partial-stale requirements.
- AC-06: Recovery canary lulus fresh start, resume, compaction, changed reference,
  invalid receipt, stale index, dan cross-project isolation.
- AC-07: Derived indexes dapat dihapus dan dibangun ulang tanpa perubahan hasil
  semantic canonical.
- AC-08: Capability/model outage fixture menghasilkan approved fallback atau
  explicit block; tidak ada silent quality downgrade.
- AC-09: Security suite lulus secret, prompt injection, path traversal, symlink,
  concurrent writer, tamper, dan unauthorized mutation checks.
- AC-10: Migration dry-run terhadap `ai-memory` menghasilkan manifest lengkap,
  tidak mengubah source, dan rollback/cutover rehearsal berhasil.
- AC-11: Dokumentasi operator, policy version, schema version, audit format, dan
  new-session bootstrap prompt konsisten dengan implementasi.
- AC-12: User melakukan canary nyata pada direct, assisted, graph, deep, knowledge
  query, dan project recovery sebelum global rollout.

## 14. Traceability

Setiap implementation issue SHOULD menyebutkan requirement IDs yang dipenuhi.
Setiap acceptance test MUST menyebutkan minimal satu FR/NFR/AC. Perubahan yang
menghapus atau melemahkan quality invariant membutuhkan ADR dan persetujuan
product owner.
