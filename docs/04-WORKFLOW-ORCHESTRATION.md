# Kontrak Orkestrasi Workflow

Status: Blueprint v1 complete; implementation pending

Dokumen ini menetapkan cara sebuah permintaan diterima, diarahkan, dipecah,
dijalankan, diverifikasi, dan dihentikan. Prinsipnya konservatif: pertanyaan
pendek tetap pendek, sedangkan pekerjaan luas atau berisiko boleh berkembang
menjadi graph yang tetap terbatas.

Dokumen terkait:

- [Routing model](05-MODEL-ROUTING.md) menetapkan alias model dan rekonsiliasi
  request/telemetry.
- [Arsitektur sistem](02-SYSTEM-ARCHITECTURE.md) dan [arsitektur memory](03-MEMORY-ARCHITECTURE.md)
  menetapkan konteks durable dan authority.
- [Keamanan dan privasi](07-SECURITY-AND-PRIVACY.md) menetapkan permission dan
  trust boundary.
- [Rencana evaluasi](08-EVALUATION-AND-TEST-PLAN.md) menetapkan quality gate.

## 1. Tujuan dan bukan tujuan

### Tujuan

1. Menjawab permintaan sederhana segera tanpa research spiral otomatis.
2. Menggunakan pekerjaan paralel hanya ketika cabangnya independen dan benar-benar
   memberi keuntungan latency atau kualitas.
3. Membuat pilihan model, tool, permission, retry, dan context terlihat serta
   dapat diaudit.
4. Menjaga kualitas dengan mengompilasi evidence yang fokus ke root, bukan
   menumpahkan log worker ke context utama.
5. Berhenti secara terlihat ketika budget, batas keamanan, atau integritas route
   gagal.

### Bukan tujuan

- Menginstal skill, plugin, MCP, atau hook yang belum ditinjau secara otomatis.
- Menganggap semua pertanyaan perlu graph atau reasoning tertinggi.
- Membiarkan note, halaman web, instruksi plugin, atau data MCP mengalahkan
  instruksi sistem, developer, dan user saat ini.
- Menyembunyikan ketidakpastian, evidence stale, mismatch route, atau verification
  yang gagal di balik jawaban yang terdengar yakin.

## 2. Prinsip operasi

- **Jalur minimum yang cukup:** mulai dari lane paling ringan yang masih mampu
  memenuhi request. Naik hanya karena evidence, bukan kecemasan tanpa bukti.
- **Eskalasi eksplisit:** classifier menyimpan alasan dan bukti untuk setiap
  kenaikan lane.
- **Artifact, bukan transcript:** worker mengembalikan hasil bertipe dan referensi
  file; root tidak memasukkan log tanpa batas.
- **Satu writer per scope:** satu node saja memiliki path/subsystem yang dapat
  ditulis pada satu waktu.
- **Fail-visible:** capability hilang, route mismatch, stale result, dan gate
  gagal menjadi status eksplisit, bukan fallback diam-diam.
- **Side effect yang dapat dipulihkan:** external write, command destruktif,
  credential, dan instalasi melewati approval boundary.
- **Research terbatas:** research dipakai untuk freshness atau ketidakpastian
  material, bukan sebagai gaya jawaban default.

## 3. Lane admission

Classifier menghasilkan tepat satu lane: `DIRECT`, `ASSISTED`, `GRAPH`, atau
`DEEP`.

| Lane | Kapan dipakai | Eksekusi default | Research | Budget | Target berhenti |
| --- | --- | --- | --- | --- | --- |
| `DIRECT` | Stabil, satu outcome, low-risk | Root saja; tanpa decomposition | Dilarang kecuali diminta atau freshness wajib | 0 child agent; 0 tool default | 60 detik |
| `ASSISTED` | Satu task workspace atau lookup sempit | Root dengan urutan tool terbatas | Satu lookup sempit bila ada alasan | 0 child agent; maksimal 3 tool round | 5 menit |
| `GRAPH` | Minimal dua cabang independen, beberapa artifact, atau parallel benefit terukur | Root planner + DAG tervalidasi + join | Hanya pada node yang memerlukannya | Maksimal 12 node, 4 concurrent, depth 1 | 15 menit lalu checkpoint |
| `DEEP` | Security, production, destructive, migration, adversarial review, atau permintaan deep | Root dengan gate high-risk; memakai DAG hanya jika graph juga justified | Diizinkan bila evidence diperlukan | 0-12 node, 4 concurrent, depth 1 | 15 menit lalu checkpoint |

`DEEP` adalah lane risiko, bukan perintah untuk selalu membuat banyak agent. Tugas
high-risk yang hanya membutuhkan satu pemeriksaan dapat tetap berjalan di root
dengan gate tambahan.

Waktu di atas adalah service objective. Pekerjaan eksplisit berupa implementasi,
audit, atau migration boleh melampaui target setelah mengumumkan budget dan
checkpoint; pertanyaan sederhana tidak boleh melewati target secara diam-diam.

### 3.1 Sinyal klasifikasi

Sebelum menjalankan tool, classifier menghitung:

```text
explicit_lane       = user menyebut direct/assisted/graph/deep/research
freshness_required  = fakta current, source eksternal, atau citation wajib
high_risk           = dampak security, credential, production, destructive,
                      data loss, atau side effect irreversible
parallel_branches   = unit independen dengan write scope terpisah
multi_artifact      = minimal dua deliverable yang perlu diintegrasikan
workspace_bound     = satu repo, satu lookup, atau path yang jelas
ambiguity           = requirement hilang dan mengubah hasil secara material
```

`high_risk` berarti risiko operasi yang diminta, bukan sekadar topik yang menyebut
kata security atau production.

### 3.2 Algoritma admission deterministik

Implementasi harus menyimpan reason code sehingga input yang sama menghasilkan
keputusan yang dapat direproduksi:

```python
def admit(f):
    if f.high_risk:
        return "DEEP", ["high_risk"]
    if f.explicit_lane == "DEEP":
        return "DEEP", ["user_explicit_deep"]
    if f.parallel_branches >= 2 and (f.multi_artifact or
                                     f.parallel_savings_ms > f.orchestration_overhead_ms):
        return "GRAPH", ["independent_branches"]
    if f.explicit_lane == "GRAPH":
        return "GRAPH", ["user_explicit_graph"]
    if f.workspace_bound or f.freshness_required or f.ambiguity:
        return "ASSISTED", ["bounded_tool_or_evidence_need"]
    return "DIRECT", ["self_contained_low_risk"]
```

Downgrade dari high-risk ke `DIRECT`/`ASSISTED` harus ditolak. Permintaan
`DIRECT` dapat dinaikkan jika side effect atau risiko baru ditemukan.

### 3.3 Aturan anti-spiral

1. Kalkulus dasar, definisi stabil, terjemahan, sapaan, dan edit kecil yang tidak
   meminta citation tidak boleh memanggil web/MCP/GitHub.
2. Jangan mencari GitHub hanya untuk menemukan skill/plugin pada task satu kali.
   Periksa capability yang sudah terpasang lebih dahulu.
3. Worker tidak boleh membuat research node baru tanpa unknown yang konkret,
   kelas sumber, dan stop condition.
4. Satu lookup gagal tidak membenarkan pencarian luas. Laporkan bounded
   uncertainty atau ajukan satu pertanyaan.
5. Ketika deadline lane tercapai, berhenti dan rangkum. Lanjutan memerlukan budget
   eksplisit atau kontrak `DEEP` yang sudah disetujui.
6. Kekhawatiran kualitas tanpa evidence bukan alasan eskalasi.

## 4. Lifecycle request

```text
RECEIVED
  -> NORMALIZED (intent, scope, constraint, output)
  -> ADMITTED (lane, reason, budget)
  -> CAPABILITIES_RESOLVED
  -> PLANNED (single node atau DAG tervalidasi)
  -> EXECUTING
  -> VERIFYING
  -> COMPILED (answer + evidence + memory candidates)
  -> CLOSED
```

Terminal state yang sah: `CLOSED`, `BLOCKED_USER_INPUT`, `BLOCKED_POLICY`,
`FAILED_VERIFICATION`, dan `CANCELLED_USER_REDIRECT`. Setiap transition mencatat
task id, timestamp, lane, profile alias, capability/tool id, dan reason yang telah
di-redact.

### 4.1 Intake packet

Sebelum tool atau agent berjalan, buat packet berikut:

```yaml
task_id: stable-id
user_intent: satu kalimat
requested_output: satu kalimat
scope:
  paths: []
  external_systems: []
constraints: []
explicit_lane: null
risk_flags: []
freshness_requirement: none|targeted|broad
acceptance_checks: []
```

Jika requirement yang hilang mengubah hasil secara material, ajukan satu
pertanyaan. Jika ada default aman, catat sebagai assumption dan lanjutkan.

### 4.2 Resolusi capability

Urutan resolusi progressive disclosure:

1. Baca metadata skill yang terpasang dan cocokkan description; load `SKILL.md`
   hanya untuk skill terpilih, lalu reference/script yang benar-benar diperlukan.
2. Periksa plugin aktif beserta bundled skill, connector, MCP, hook, dan auth.
3. Periksa MCP/app yang sudah dikonfigurasi untuk sistem eksternal. Gunakan
   connector untuk data private, bukan web search.
4. Jika capability memang hilang, usulkan discovery eksternal. Jangan install
   otomatis; gunakan supply-chain gate di [07-SECURITY-AND-PRIVACY.md](07-SECURITY-AND-PRIVACY.md),
   pin versi yang ditinjau, dan minta persetujuan user.

Resolver mengembalikan entri `selected`, `not_selected`, `missing`, atau `blocked`
dengan reason. User tidak perlu menghafal nama capability.

### 4.3 Planning dan eksekusi

`DIRECT` dan `ASSISTED` memakai satu root plan. `GRAPH` dan `DEEP` yang memakai
DAG wajib melewati validator sebelum worker dimulai. Root memegang requirement dan
integrasi; node hanya memegang scope yang dideklarasikan.

Setiap node wajib memilih satu alias di [05-MODEL-ROUTING.md](05-MODEL-ROUTING.md).
Model dan effort tidak pernah diwarisi secara implisit dari parent.

### 4.4 Verification dan compilation

- `DIRECT`: consistency check internal.
- `ASSISTED`: cek output/path yang diminta dan evidence yang dipakai.
- `GRAPH`: deterministic check per node lalu integration check.
- `DEEP`: independent reviewer serta security/rollback gate.

Root hanya memasukkan decision, evidence, changed path, unresolved risk, dan next
action. Log besar disimpan sebagai artifact ber-hash.

## 5. Kontrak graph

### 5.1 Constraint wajib

- DAG tidak boleh memiliki cycle atau dependency tersembunyi.
- Maksimal 12 node, 4 node concurrent, dan depth 1.
- Setiap node memiliki profile alias, timeout 30-900 detik, dan satu atau dua
  attempt.
- Path/subsystem yang overlap memiliki satu owner writer. Reviewer read-only boleh
  membaca scope tersebut.
- Graph multi-writer wajib memiliki join/integrator sebelum output akhir.
- Graph high-risk memakai reviewer read-only dengan profile berbeda bila praktis.
- Dependency gagal memblokir downstream; tidak ada partial merge implicit.
- Redirect user membatalkan node pending dan menandai artifact-nya abandoned.

### 5.2 Manifest

```json
{
  "version": 1,
  "task_id": "task-...",
  "lane": "GRAPH",
  "goal": "...",
  "nodes": [
    {
      "id": "research-api",
      "kind": "research",
      "task": "Jawab satu unknown memakai sumber resmi",
      "profile": "tera-xhigh",
      "depends_on": [],
      "read_scope": ["docs/"],
      "write_scope": ["artifacts/research-api.json"],
      "capabilities": ["openai-docs"],
      "acceptance": ["URL dan tanggal sumber", "claim/evidence mapping"],
      "timeout_seconds": 300,
      "max_attempts": 1,
      "permission_class": "local-read"
    }
  ],
  "final_node": "integrate"
}
```

Validator memeriksa schema, alias, dependency, cycle, overlap scope, jumlah node,
concurrency, timeout, attempt, dan permission sebelum scheduling.

### 5.3 Envelope hasil node

```yaml
node_id: research-api
status: succeeded|failed|blocked|cancelled
claims: []
evidence:
  - artifact_id: artifact-...
    source: local|mcp|web|command
    locator: path-or-url
    collected_at: RFC3339
changes: []
tests: []
unresolved: []
route_observation_id: route-...
next_node_inputs: []
```

Envelope default dibatasi 2.000 token untuk worker dan 4.000 token untuk
join/reviewer. Output besar menjadi artifact ber-hash dengan ringkasan pendek.

### 5.4 Retry dan eskalasi

- Retry satu kali hanya untuk failure transient yang terklasifikasi (timeout,
  provider 5xx, MCP sementara).
- Eskalasi satu kali hanya jika evidence baru mengubah risiko atau capability yang
  dibutuhkan.
- Self-review identik tidak boleh diulang tanpa evidence baru.
- Route mismatch, permission violation, dan security gate failure bukan retry biasa;
  hasil dikarantina dan node dihentikan.

## 6. Context, waktu, dan biaya

Budget memory packet mengikuti [03-MEMORY-ARCHITECTURE.md](03-MEMORY-ARCHITECTURE.md)
dan [01-PRD.md](01-PRD.md):

| Paket memory | Soft limit | Hard behavior |
| --- | ---: | --- |
| `DIRECT` | 0 token memory | retrieval hanya setelah alasan eskalasi |
| Recovery project | min(8K, 2.5% window) | trim detail derived, pertahankan decision/blocker |
| `ASSISTED` knowledge | min(12K, 5% window) | retrieval round bounded berikutnya |
| Graph worker memory | min(16K, 6% worker window) | minta evidence tambahan secara terbatas |
| Graph/deep root synthesis | min(24K, 8% root window) | kompres evidence sebelum menambah source |

Budget 16K hanya untuk subpacket satu worker. Budget canonical retrieval/context
R3 pada root/join adalah `min(24K, 8% active root window)`; ia bukan penjumlahan
seluruh packet worker.

Total input model (aturan, code excerpt, dan artifact selain memory) memiliki
ceiling runtime terpisah: `DIRECT` 16K, `ASSISTED` 48K, worker 64K, join/reviewer
96K. Root working set di atas 60% window memicu checkpoint; 70% menghentikan
penambahan context dan memerlukan task baru. Pada window sekitar 357K, jangan
memperlakukan seluruh kapasitas sebagai ruang kerja bebas.

## 7. Observability minimum

Emit event terstruktur dan sudah di-redact untuk `task_received`,
`admission_decided`, `capability_selected`, `graph_validated`, `node_started`,
`node_finished`, `route_serialized`, `route_observed`, `route_mismatch`,
`tool_denied`, `verification_finished`, `memory_candidate_created`,
`task_closed`, dan `task_blocked`.

Field wajib: `event_id`, `task_id`, `node_id` bila ada, `lane`, `profile_alias`,
hash raw model id, timestamp, duration, token/cost bila tersedia, status, reason
code, dan artifact id. Jangan simpan prompt penuh atau credential.

## 8. Kriteria penerimaan

Implementasi siap bila:

1. Intake yang sama menghasilkan lane dan reason code yang sama pada test deterministik.
2. Pertanyaan stabil sederhana tidak memanggil research eksternal tanpa request
   atau freshness requirement.
3. Graph invalid ditolak sebelum worker dimulai.
4. Setiap node memiliki profile, timeout, permission, dan acceptance check terlihat.
5. Partial failure, stale evidence, dan route mismatch tetap terlihat di output.
6. Graph mengurangi noise context tanpa menurunkan quality floor di [08-EVALUATION-AND-TEST-PLAN.md](08-EVALUATION-AND-TEST-PLAN.md).
