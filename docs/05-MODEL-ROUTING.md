# Kontrak Routing Model

Status: Blueprint v1 complete; implementation pending

Kontrak ini menetapkan tujuh profile model yang disetujui user, cara memilihnya,
dan cara membuktikan bahwa route yang dimaksud benar-benar sama dengan yang
dilihat 9router/provider. Nama profile adalah alias workflow yang stabil, bukan
asumsi tentang model id publik provider.

Hanya profile berikut yang diizinkan:

- Tera Max
- Tera xhigh
- Tera high
- Sol Max
- Sol xhigh
- Luna xhigh
- GPT-5.5 xhigh

Model atau effort lain tidak boleh masuk production routing tanpa decision record
dan evaluasi baru.

## 1. Invariant routing

1. Root chat default adalah **Tera Max**.
2. `DIRECT` tetap dikerjakan root. Jangan spawn agent hanya untuk memakai model
   yang lebih murah.
3. Setiap graph node menyebut satu profile alias secara eksplisit. Model dan
   effort child tidak pernah diwarisi diam-diam dari parent.
4. Luna xhigh hanya untuk pekerjaan trivial dan bounded. Ia tidak boleh memperluas
   scope, melakukan broad research, menangani secret, mengambil keputusan security,
   atau menjalankan external/destructive side effect.
5. Builder dan independent reviewer memakai role berbeda. GPT-5.5 xhigh adalah
   reviewer/second perspective, bukan upgrade universal di atas semua model.
6. Raw model slug dan serialized effort adalah konfigurasi adapter. Nilainya tidak
   ditanam di prompt, skill, workflow rule, atau `AGENTS.md`.
7. Route intent, request yang diserialisasi, dan telemetry router/provider harus
   cocok. Mismatch selalu terlihat.

## 2. Registry alias stabil

Satu registry versioned, misalnya `config/model-profiles.yaml`, menjadi source of
truth. Path final boleh berubah pada implementasi, tetapi registry authoritative
tidak boleh lebih dari satu.

| Nama | Alias stabil | Effort intent | Role utama | Cocok untuk | Jangan dipakai untuk |
| --- | --- | --- | --- | --- | --- |
| Tera Max | `tera-max` | `max` | Root orchestrator, architect, integrator | Goal ambigu, DAG, resolusi konflik, final synthesis | Worker batch repetitif |
| Tera xhigh | `tera-xhigh` | `xhigh` | Analyst dan research synthesizer | Dependency analysis, source synthesis, diagnosis, spec review | Formatting mekanis |
| Tera high | `tera-high` | `high` | Fast generalist/explorer | Read-heavy scan, penjelasan, log triage, reasoning rutin | Satu-satunya owner keputusan security/architecture high-risk |
| Sol Max | `sol-max` | `max` | Heavy builder/debugger | Implementasi multi-file sulit, tool use kompleks, deep debugging | Pertanyaan kasual atau inventory murah |
| Sol xhigh | `sol-xhigh` | `xhigh` | Scoped builder | Frontend/backend, refactor, test, polish dengan kontrak jelas | Planning tanpa batas atau security approval |
| Luna xhigh | `luna-xhigh` | `xhigh` | Utility worker | Extraction, classification deterministik, inventory, formatting, fixture | Planning, broad research, security, secret, destructive/external write |
| GPT-5.5 xhigh | `gpt55-xhigh` | `xhigh` | Independent reviewer | Security review, adversarial check, compatibility, test gap, second opinion | Root default atau routine implementation |

Registry memisahkan intent dari mapping endpoint:

```yaml
registry_version: 1
default_root_profile: tera-max
profiles:
  tera-max:
    display_name: Tera Max
    family: tera
    effort_intent: max
    role: orchestrator
    budget_class: premium
    adapters:
      nine_router:
        raw_model_slug: "REPLACE_WITH_VALIDATED_9ROUTER_SLUG"
        serialized_effort: "REPLACE_WITH_VALIDATED_VALUE"
        adapter_version: "pinned-version"
```

`raw_model_slug` dan `serialized_effort` adalah nilai deployment yang ditemukan
dan diverifikasi terhadap versi Codex serta 9router yang terpasang. Jangan
menebaknya dari display name. Upgrade router hanya mengubah adapter mapping dan
test-nya, bukan semua artifact workflow.

## 3. Policy pemilihan

### 3.1 Perilaku root

- Mulai chat biasa dengan Tera Max sesuai keputusan user.
- Pilihan profile eksplisit dari user dipertahankan jika termasuk tujuh profile
  approved dan tidak melanggar safety gate. User tidak dapat memaksa Luna untuk
  pekerjaan yang gagal melewati hard gate Luna.
- Kerjakan `DIRECT` di root walaupun Tera high atau Luna tampak lebih murah.
  Spawn dan duplikasi context akan menghapus penghematan tersebut.
- Root menormalisasi intent, memilih capability, memvalidasi graph,
  mengintegrasikan hasil, dan berkomunikasi dengan user.
- Tera Max tidak harus mengerjakan semua worker node. Pilih profile terkecil yang
  lulus kontrak kualitas dan risiko node.

### 3.2 Algoritma pemilihan worker

Pilih berdasarkan role dan risiko, bukan ranking universal:

```text
if root planning, ambiguity resolution, atau multi-branch integration:
    tera-max
elif independent adversarial/security/compatibility review:
    gpt55-xhigh
elif difficult multi-file build atau deep tool-heavy debugging:
    sol-max
elif implementation/refactor/test dengan contract jelas:
    sol-xhigh
elif source synthesis, dependency analysis, atau medium diagnosis:
    tera-xhigh
elif read-heavy scan, explanation, atau routine log triage:
    tera-high
elif deterministic, trivial, bounded, dan non-sensitive:
    luna-xhigh
else:
    tera-max dengan reason "unresolved_role_or_risk"
```

### 3.3 Hard gate Luna

`luna-xhigh` hanya boleh dipilih jika semua kondisi benar:

- Output memiliki schema finite atau file scope yang jelas.
- Tidak ada judgment architecture/security.
- Tidak ada credential, private connector, production data, atau executable
  untrusted content.
- Tidak ada external write, install, publish, delete, atau destructive command.
- Node tidak boleh menambah node, tool, path, atau research source.
- Hasil dapat diperiksa deterministik atau direview parent.

Jika satu syarat gagal, gunakan Tera high/xhigh atau Sol xhigh sesuai role.

### 3.4 Jalur eskalasi

Eskalasi hanya satu kali dan harus membawa evidence baru:

| Profile awal | Eskalasi | Kondisi |
| --- | --- | --- |
| `luna-xhigh` | `tera-high` | Output ternyata memerlukan judgment |
| `tera-high` | `tera-xhigh` | Dependency/ambiguity/evidence melebihi scan rutin |
| `tera-xhigh` | `tera-max` | Konflik lintas branch atau architecture belum selesai |
| `sol-xhigh` | `sol-max` | Implementasi berubah menjadi deep multi-file debugging |
| Builder apa pun | reviewer `gpt55-xhigh` | Perlu adversarial/security/compatibility validation independen |

Jangan eskalasi hanya karena model lambat, verbose, atau output memerlukan koreksi
deterministik. Prompt identik tidak boleh diulang dua kali.

## 4. Surface konfigurasi Codex

Gunakan surface durable terkecil yang sesuai:

- Global `~/.codex/config.toml`: root Tera Max, batas multi-agent, MCP global,
  sandbox, dan approval default.
- Global `~/.codex/AGENTS.md`: aturan ringkas seperti admission dan anti-spiral.
- Project `.codex/config.toml`: override untuk repository trusted; tidak boleh
  mendefinisikan ulang arti alias global secara diam-diam.
- Repository/nested `AGENTS.md`: command, convention, constraint, dan verification
  project; file terdekat berlaku pada subtree-nya.
- Custom agent di `~/.codex/agents/` atau `.codex/agents/`: satu file generated
  per profile worker dengan `model` dan `model_reasoning_effort` eksplisit.
- Skill: workflow reusable dengan progressive disclosure; boleh menyarankan
  profile tetapi tidak memiliki raw slug.
- Plugin: distribution bundle untuk skill, connector/MCP, hook, dan asset.
- MCP: live external system serta data/action private.
- Hook: lifecycle telemetry dan mechanical gate, bukan semantic router.

Custom-agent TOML dibuat dari registry alias dan divalidasi CI. Edit manual pada
field generated `model` atau `model_reasoning_effort` dianggap configuration drift.

## 5. Attestation route tiga tahap

Kontrak pusatnya:

```text
route intent -> serialized request -> router/provider observed telemetry
```

### 5.1 Route intent

Orchestrator membuat intent immutable sebelum call:

```json
{
  "route_id": "route-uuid",
  "task_id": "task-uuid",
  "node_id": "backend",
  "profile_alias": "sol-max",
  "effort_intent": "max",
  "registry_version": 12,
  "adapter": "nine_router",
  "adapter_version": "9router-release-or-config-hash"
}
```

### 5.2 Serialized request

Adapter me-resolve alias lalu mencatat field yang benar-benar dikirim Codex.
Content dan credential harus di-redact:

```json
{
  "route_id": "route-uuid",
  "raw_model_slug": "endpoint-specific-slug",
  "serialized_effort_field": "endpoint-specific-field-name",
  "serialized_effort_value": "endpoint-specific-value",
  "request_schema_version": "responses-or-router-schema-version",
  "payload_hash": "sha256-of-canonical-redacted-routing-fields",
  "sent_at": "RFC3339"
}
```

Serializer memakai allowlist field routing dan menolak effort yang tidak dikenal.
Ia tidak boleh mengandalkan teks UI atau substring nama model.

### 5.3 Observed telemetry

9router/provider menyediakan record berkorelasi melalui response header,
structured log, atau authenticated telemetry endpoint:

```json
{
  "route_id": "route-uuid",
  "router_request_id": "...",
  "observed_model_slug": "...",
  "observed_effort": "...",
  "provider": "...",
  "router_version": "...",
  "normalization_rules_version": "...",
  "observed_at": "RFC3339"
}
```

Jika 9router belum mempertahankan client correlation id, tambahkan header khusus
dan teruskan melewati setiap proxy hop. Korelasi berdasarkan timestamp saja tidak
cukup.

### 5.4 Status rekonsiliasi

- `MATCH`: model dan effort cocok dengan alias mapping versioned.
- `MISMATCH_MODEL`: raw model berbeda.
- `MISMATCH_EFFORT`: effort berbeda, termasuk bug non-xhigh terbaca xhigh.
- `MISMATCH_BOTH`: model dan effort berbeda.
- `MISSING_TELEMETRY`: observation tidak datang sebelum timeout.
- `AMBIGUOUS_TELEMETRY`: lebih dari satu observation cocok atau field wajib hilang.
- `UNSUPPORTED_MAPPING`: versi router belum memiliki mapping tervalidasi.

Perbandingan exact dilakukan setelah normalization function yang versioned.
`max`, `xhigh`, dan `high` tidak boleh dilebur menjadi satu kategori umum.

## 6. Perilaku fail-visible

1. Pada node `GRAPH`/`DEEP`, status selain `MATCH` mengarantina hasil. Node yang
   menulis atau memiliki side effect tidak boleh commit, publish, install, atau
   menjalankan external mutation.
2. Retry satu kali dengan last-known-good adapter hanya diizinkan untuk
   `MISMATCH_*`/`UNSUPPORTED_MAPPING` dan hanya bila belum ada side effect.
3. Jika retry masih gagal, hentikan node dan laporkan profile intent, serialized
   value, observed value, router version, dan correlation id. Jangan mengklaim
   effort yang diminta telah digunakan.
4. Output `DIRECT` read-only low-risk boleh ditampilkan saat `MISSING_TELEMETRY`
   hanya dengan warning `UNVERIFIED_ROUTE`; output tersebut tidak boleh masuk
   benchmark route atau verified durable memory.
5. `AMBIGUOUS_TELEMETRY` adalah defect telemetry, bukan sukses.
6. Mismatch yang menaikkan effort tetap defect biaya/latency. `high -> xhigh` atau
   `max -> xhigh` tidak boleh diabaikan hanya karena kualitas tampak cukup.

## 7. Canary dan update 9router

Jalankan route canary:

- sebelum dan setelah setiap update 9router;
- setelah update Codex yang mengubah serialization;
- setelah perubahan registry, provider, normalization, atau proxy;
- secara periodik sesuai rencana evaluasi.

Jangan menambah canary call pada setiap chat. Session memakai health receipt
terakhir yang belum kedaluwarsa (default 24 jam) dan tetap merekonsiliasi telemetry
pada request nyata. Jika receipt hilang/expired, canary on-demand hanya dijalankan
sebelum node non-`DIRECT` atau side-effecting; pertanyaan sederhana tidak ditahan.

Full canary mengirim request singkat, deterministic, read-only, dan non-sensitive
untuk ketujuh profile. Ia memvalidasi intent, payload, observed telemetry,
correlation, dan latency.

```text
snapshot last-known-good mapping
  -> full canary candidate router
  -> shadow/read-only traffic
  -> bandingkan telemetry dan kualitas
  -> promote adapter version
  -> monitor mismatch dan latency
```

Satu mismatch memblokir promotion. Rollback mengembalikan adapter mapping serta
router/config version terakhir yang sehat tanpa mengubah alias profile.

## 8. Budget latency dan biaya

Harga endpoint dan latency disimpan sebagai telemetry/config versioned, bukan
angka hardcoded pada instruction. Pemilihan mengutamakan role dan quality floor,
lalu memilih biaya terendah di antara profile yang lulus.

| Kelas profile | Objective p95 node | Saat terlampaui |
| --- | ---: | --- |
| Luna utility | 90 detik | Berhenti; scope tidak boleh diperluas |
| Tera high scan | 3 menit | Kembalikan bounded partial result |
| Tera xhigh / Sol xhigh | 7 menit | Checkpoint lalu berhenti/eskalasi sekali |
| Tera Max / Sol Max / GPT-5.5 xhigh | 15 menit | Checkpoint dan minta budget lanjutan |

Scheduler mengestimasi biaya graph sebelum eksekusi dan mencatat token/cost aktual
bila telemetry menyediakannya. Node tidak boleh melewati budget diam-diam.

## 9. Kriteria penerimaan

Routing siap bila:

1. Semua alias di-resolve melalui satu registry dan generated custom-agent config.
2. Tera Max terbukti sebagai default root route.
3. Setiap node mencatat intent, serialized route, dan observed telemetry berkorelasi.
4. Regression test "request non-xhigh, observed xhigh" gagal sebelum fix dan lulus
   setelah fix.
5. Update router/Codex tidak dapat dipromosikan bila satu canary mismatch atau
   tidak memiliki telemetry.
6. Pelanggaran Luna ditolak sebelum execution.
7. Last-known-good mapping dapat dipulihkan tanpa mengedit prompt, skill, atau
   graph manifest.
