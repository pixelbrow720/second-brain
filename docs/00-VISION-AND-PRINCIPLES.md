# Vision and Product Principles

- Status: Blueprint v1 complete; implementation pending
- Audience: product owner, implementer, operator, reviewer, dan AI agent
- Scope: global second brain dan global Codex workflow untuk penggunaan pribadi

## 1. Visi

Membangun partner kerja AI yang dapat melanjutkan pekerjaan lintas sesi, menemukan
pengetahuan yang tepat tanpa memenuhi context window, dan memilih cara kerja yang
sebanding dengan kompleksitas permintaan. Sistem harus terasa cepat untuk hal
sederhana, dalam untuk hal sulit, serta tetap dapat diaudit oleh manusia.

Sistem akhir bukan "satu prompt besar" dan bukan "vault yang selalu dibaca". Ia
adalah gabungan dari:

1. **Global Knowledge Plane** untuk sumber, entitas, konsep, klaim, dan sintesis
   yang berguna lintas proyek.
2. **Project Recovery Plane** untuk keputusan, task aktif, blocker, perubahan,
   dan verification evidence pada satu proyek.
3. **Workflow Plane** untuk mengklasifikasikan permintaan, memilih model,
   skill/plugin/MCP, serta menentukan apakah pekerjaan cukup dijawab langsung,
   dikerjakan secara fokus, atau membutuhkan work graph.
4. **Bounded Context Compiler** untuk memberikan hanya evidence yang dibutuhkan
   oleh model pada saat itu.

Context window sekitar 357K token tetap diperlakukan sebagai working memory yang
terbatas, bukan tempat penyimpanan permanen.

## 2. Hasil yang Diinginkan

Pengguna memperoleh satu workflow global dengan perilaku berikut:

- Pertanyaan sederhana dijawab langsung oleh root model tanpa riset, subagent,
  plugin, atau ritual validasi yang tidak perlu.
- Tugas kompleks dipecah hanya ketika paralelisme, spesialisasi, atau independent
  review memberi manfaat nyata.
- Root agent menggunakan Tera Max secara default, tetap memegang intent pengguna,
  keputusan akhir, dan integrasi hasil.
- Skill, plugin, dan MCP yang sudah tersedia dipilih otomatis namun konservatif
  berdasarkan kebutuhan nyata, bukan karena sekadar tersedia.
- Pengetahuan global dipelihara sebagai wiki yang saling terhubung dan dapat
  diperbarui dari banyak sumber, bukan sekumpulan handoff.
- Recovery tiap proyek tetap kecil, current, dan terpisah dari pengetahuan global.
- Kualitas tidak ditukar secara diam-diam dengan kecepatan atau biaya. Sistem
  mengurangi kerja yang mubazir terlebih dahulu dan menaikkan effort saat risiko
  atau ketidakpastian memang menuntutnya.

## 3. Model Mental Produk

```mermaid
flowchart LR
    U["Permintaan pengguna"] --> A["Admission dan routing"]
    A --> D["Direct"]
    A --> F["Assisted execution"]
    A --> G["Work graph"]
    A --> H["Deep / high-risk"]

    K["Global knowledge"] --> C["Bounded context compiler"]
    P["Project recovery"] --> C
    C --> D
    C --> F
    C --> G
    C --> H

    D --> R["Jawaban terintegrasi"]
    F --> R
    G --> R
    H --> R
    R --> M["Selective memory commit"]
    M --> K
    M --> P
```

`Direct`, `Assisted`, `Graph`, dan `Deep` adalah kelas perilaku produk, bukan nama
model. `Assisted` adalah focused execution dengan satu root thread; `Deep` adalah
jalur graph/high-risk yang memiliki gate tambahan. Detail routing model dan
protokol orkestrasi dijelaskan oleh kontrak workflow.

## 4. Prinsip Normatif

Kata **MUST**, **SHOULD**, dan **MAY** dipakai dalam arti requirement.

### P-01: Direct by Default

Sistem MUST memilih jalur paling sederhana yang tetap dapat menghasilkan jawaban
berkualitas. Sapaan, definisi, kalkulasi biasa, penjelasan konsep yang stabil,
atau edit lokal yang jelas MUST tidak memicu riset, graph, maupun capability
eksternal tanpa alasan konkret.

### P-02: Complexity-Adaptive Effort

Effort mengikuti ambiguity, blast radius, reversibility, novelty, dan kebutuhan
evidence. Panjang prompt bukan indikator tunggal kompleksitas. Sistem SHOULD
menambah retrieval, verification, atau spesialis hanya ketika hal tersebut
menurunkan risiko atau meningkatkan kualitas secara terukur.

### P-03: Quality Floor Before Optimization

Optimasi latency, token, dan biaya hanya boleh dilakukan setelah quality floor
terpenuhi. Route yang lebih ringan tidak boleh digunakan jika tugas membutuhkan
reasoning, tool reliability, domain expertise, atau safety review yang lebih
tinggi. Penurunan model atau validation depth yang berpotensi material MUST
terlihat oleh pengguna atau diblokir.

### P-04: One Root, Explicit Ownership

Tera Max adalah root model default. Root MUST mempertahankan intent pengguna,
menentukan scope, mengintegrasikan hasil, menyelesaikan konflik, dan mengirimkan
jawaban akhir. Specialist menghasilkan bounded artifact; specialist tidak boleh
mengubah tujuan atau menjadi sumber kebenaran kedua.

### P-05: Graph Only When Justified

Work graph MUST memiliki sekurangnya dua cabang yang benar-benar independen,
dependency yang eksplisit, atau kebutuhan independent review yang sebanding
dengan overhead. Graph dilarang untuk pertanyaan sederhana atau pekerjaan yang
lebih cepat diselesaikan root sendiri, kecuali pengguna secara eksplisit meminta
graph sebagai mode kerja/eksperimen. Paralelisme adalah alat, bukan default.

### P-06: Conservative Capability Selection

Skill, plugin, app, dan MCP MUST dipilih dari kebutuhan tugas dan permission yang
diperlukan. Sistem MUST menggunakan jumlah capability minimum. Capability yang
terpasang boleh diaktifkan otomatis bila trigger dan manfaatnya jelas; instalasi,
aktivasi global, autentikasi baru, atau perluasan permission membutuhkan tindakan
atau persetujuan pengguna.

### P-07: Two Memories, Two Authorities

Global knowledge dan project recovery MUST dipisahkan secara fisik dan logis.

- Global knowledge menjawab "apa yang kita ketahui dan dari mana?"
- Project recovery menjawab "apa yang sedang benar dan perlu dilakukan di proyek
  ini?"

Handoff, task state, dan transient execution logs tidak boleh mencemari wiki
pengetahuan. Knowledge lintas proyek tidak boleh otomatis menjadi keputusan
proyek.

### P-08: Immutable Sources, Maintained Wiki

Raw source bytes yang berhasil diingest MUST immutable dan content-addressed.
Capture atau edisi dengan bytes baru menjadi source object baru; koreksi metadata
boleh menjadi revisi CAS selama tetap menunjuk raw blob yang sama. Entity,
concept, claim, dan synthesis adalah maintained semantic pages yang dapat direvisi
dengan provenance dan revision history. Satu source MAY memperbarui banyak
semantic pages.

### P-09: Evidence and Authority Are Explicit

Setiap klaim durable MUST memiliki provenance, authority/trust class, waktu, dan
status verification yang dapat dibaca mesin. Urutan otoritas default adalah:

1. instruksi pengguna saat ini;
2. repository/runtime/test evidence saat ini untuk fakta proyek;
3. keputusan pengguna yang masih aktif;
4. sumber primer dan dokumentasi resmi yang current;
5. sintesis terkurasi;
6. inference agent atau sumber eksternal yang belum diverifikasi.

Urutan tersebut dapat berbeda per domain, tetapi perbedaannya MUST eksplisit.

### P-10: Staleness Must Be Visible

Freshness MUST dinilai per claim dan per reference, bukan hanya per note. Satu
file referensi yang berubah tidak boleh membuat seluruh objek paling relevan
hilang diam-diam. Retrieval SHOULD menampilkan evidence relevan yang partially
stale dengan warning, menjelaskan bagian yang terdampak, dan menawarkan langkah
reverification.

### P-11: Bounded, Progressive Context

Sistem MUST menyusun context packet yang task-specific, memiliki budget, dan
dapat ditelusuri ke sumbernya. Retrieval dilakukan bertahap: metadata dan lexical
recall lebih dahulu, graph expansion atau semantic search bila diperlukan.
Kegagalan recall tidak boleh dijawab dengan memasukkan seluruh vault.

### P-12: Human-Readable Authority, Disposable Acceleration

Authoritative state SHOULD berupa Markdown/YAML dan append-only event records yang
dapat direview, di-diff, dan dipindahkan. SQLite, FTS, embeddings, cache, MOC, dan
recovery pack adalah derived artifacts: semuanya MUST dapat dibangun ulang tanpa
kehilangan pengetahuan.

### P-13: External Content Is Data, Not Instruction

Isi web, source document, MCP response, memory record, dan output specialist MUST
diperlakukan sebagai untrusted data. Instruksi yang terkandung di dalamnya tidak
boleh mengambil alih system/developer/user intent. Secret dan data privat tidak
boleh masuk ke memory, log, atau source artifact tanpa policy eksplisit.

### P-14: Write Less, Curate Better

Tidak semua percakapan layak menjadi memory. Sistem MUST menyimpan durable state
saja: source, keputusan, klaim, sintesis, blocker, atau verification evidence
yang akan berguna kembali. Draft sementara, sapaan, reasoning trace, dan jawaban
yang mudah direkonstruksi SHOULD tidak disimpan.

### P-15: Explain Material Automation

Automation rutin tidak perlu menghasilkan narasi panjang, tetapi sistem MUST
dapat menjelaskan mengapa capability, model, retrieval, atau graph tertentu
dipakai. Operasi berisiko, perubahan global, fallback berkualitas lebih rendah,
dan hasil yang partially stale MUST terlihat sebelum atau pada saat berdampak.

### P-16: Fail Closed on Authority, Degrade Gracefully on Convenience

Jika integrity, permission, provenance, atau authority tidak dapat divalidasi,
sistem MUST menolak write atau recovery. Jika hanya index, plugin opsional,
network, atau paralelisme yang gagal, sistem SHOULD melanjutkan dengan fallback
lokal/serial sambil menyatakan batas evidence yang tersedia.

### P-17: Measure Behavior, Not Self-Reported Confidence

Kualitas dinilai dengan fixture, exact artifacts, citations, recovery canary,
latency, dan regression benchmark. Pernyataan agent seperti "sudah diverifikasi"
tanpa evidence bukan gate keberhasilan.

### P-18: Reversible Evolution

Perubahan schema, model routing, skill/plugin/MCP, dan global instructions MUST
versioned, dapat diuji di staging/canary, dan memiliki rollback. Tidak ada sistem
yang boleh bergantung permanen pada satu vendor, satu model, atau satu opaque
index.

## 5. Quality Invariants

Implementasi tidak boleh dinyatakan siap jika salah satu invariant berikut gagal:

| ID | Invariant |
| --- | --- |
| QI-01 | Permintaan sederhana dapat selesai tanpa retrieval atau orkestrasi yang tidak relevan. |
| QI-02 | Root mempertahankan intent dan menjadi satu-satunya owner jawaban akhir. |
| QI-03 | Setiap fakta eksternal yang material dalam sintesis dapat ditelusuri ke source. |
| QI-04 | Source bytes yang sudah diterima tidak berubah; bytes baru menghasilkan capture/source object baru. |
| QI-05 | Evidence relevan yang stale tidak dihilangkan tanpa indikator eksplisit. |
| QI-06 | Project A tidak menerima recovery state atau private knowledge milik project B tanpa permintaan eksplisit. |
| QI-07 | Derived index dapat dihapus dan dibangun ulang dari authority files. |
| QI-08 | Model/capability fallback tidak menurunkan quality floor secara diam-diam. |
| QI-09 | Graph tidak dijalankan tanpa admission reason dan dependency contract. |
| QI-10 | Memory write yang authoritative memiliki provenance, revision, dan audit event. |

## 6. Keputusan Trade-off

Urutan optimasi produk adalah:

1. correctness dan keselamatan;
2. fidelity terhadap intent pengguna;
3. completeness evidence yang material;
4. latency interaktif;
5. context efficiency;
6. biaya komputasi dan jumlah tool call.

Urutan ini bukan izin untuk selalu menggunakan model termahal atau riset paling
panjang. Redundant retrieval, duplicate validation, agent yang menunggu, dan
context yang tidak relevan adalah waste dan harus dieliminasi tanpa mengurangi
quality floor.

## 7. Non-Goals Tingkat Visi

Produk ini tidak dimaksudkan untuk:

- menyimpan seluruh transcript atau chain-of-thought;
- menggantikan Git, test suite, issue tracker, atau dokumentasi resmi;
- menganggap memory sebagai instruksi executable;
- mengaktifkan semua skill/plugin/MCP pada setiap task;
- menjadikan Obsidian, embeddings, SQLite, atau satu MCP sebagai authority;
- melakukan autonomous global configuration atau instalasi tanpa review;
- menyediakan shared multi-user knowledge service pada versi awal;
- menjanjikan infinite context atau recall sempurna tanpa evidence;
- meneliti ulang pertanyaan stabil yang dapat dijawab root dengan yakin;
- menyalin repository pihak ketiga tanpa pemeriksaan license, provenance, dan
  security.

## 8. Definition of Product Integrity

Produk dianggap menjaga integritas bila pengguna dapat menjawab lima pertanyaan
berikut dari artifact yang tersedia:

1. Mengapa sistem memilih jalur direct, assisted, graph, atau deep?
2. Model dan capability apa yang dipakai, dan untuk kebutuhan apa?
3. Evidence mana yang mendukung jawaban atau keputusan material?
4. Bagian memory mana yang fresh, partially stale, disputed, atau unverified?
5. Apa yang ditulis secara durable, siapa/apa sumbernya, dan bagaimana rollback?

Jika salah satu jawaban memerlukan tebakan atau hanya bergantung pada klaim agent,
implementasi belum memenuhi visi.

## 9. Design Lineage

Kontrak ini mengambil dua pelajaran utama tanpa menjadikan implementasi lama atau
referensi eksternal sebagai dependency:

- Audit [`ai-memory`](/home/pixel/Data/PROJECT/ai-memory) menunjukkan recovery
  kernel yang kuat (atomic Markdown/YAML, event ledger, bounded pack, dan signed
  lifecycle), sekaligus menunjukkan bahaya whole-object staleness, stale derived
  index, dan MOC yang tidak dipelihara.
- Konsep [LLM Wiki Andrej Karpathy](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
  menginspirasi loop `ingest -> maintained wiki -> query -> lint`: source mentah
  dipertahankan, sedangkan semantic pages terus dikompilasi ulang ketika evidence
  baru datang.

Blueprint ini menggabungkan keduanya: recovery kernel tetap project-local dan
operasional; wiki menjadi global knowledge layer yang terpisah.
