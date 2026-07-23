# M0 Local Scaffold

Status: M0 complete and locally verified on 2026-07-22. This document records
local ownership and reproducible commands; it does not authorize a global Codex
change.

## Directory Ownership

| Path | Owner | Purpose | Authority status |
| --- | --- | --- | --- |
| `src/second_brain/` | M0 package | Strict JSON loading, narrow schema checks, profile and workspace guards | Local support code; not a storage writer |
| `schemas/` | Contract freeze | Generated JSON Schemas and source registry | Machine-readable contracts |
| `config/model-profiles.json` | Model-routing contract | The only local logical-profile registry | Placeholders only; no raw 9router mapping |
| `fixtures/` | Contract tests | Synthetic valid and invalid inputs | No private source, secret, or transcript |
| `tests/` | Verification | Standard-library deterministic tests | Local only |
| `scripts/` | Reproducibility | Asset generation and local check entry points | Must stay repository-contained |
| `artifacts/` | Evidence | Public verification evidence only | Runtime/private receipts are ignored |
| `dist/global/` | Future staging | Candidate global artifacts after later milestones | Empty by design; never deployed by M0 |

`scripts/build_m0_contract_assets.py` extracts the three normative JSON Schema
fences from `docs/06-DATA-SCHEMAS.md` and generates synthetic fixtures. Do not
manually edit its generated outputs; rerun the generator and review the source
document change instead.

The M0 validator supports only the JSON Schema keywords used by these frozen
contracts. Safe Markdown/YAML parsing, full RFC 8785 canonicalization, CAS, and
all authoritative writes remain M1 responsibilities.

## Local Commands

Run each command from the repository root:

```bash
python3 scripts/build_m0_contract_assets.py --check
make contracts
make docs
make lint
make test
make check
python3 scripts/check_clean_room.py
```

`check_clean_room.py` copies the project under ignored
`artifacts/test-runs/`, runs `make check` there, and removes the copy. It never
writes to a system temporary directory, the old vault, `ai-memory`, or global
Codex configuration.

## M0 Contract Coverage

- duplicate JSON keys fail before schema validation;
- unknown fields fail for closed schemas;
- memory object ID, store, and kind must agree;
- timestamps must be RFC 3339 UTC values with `Z`;
- SHA-256 field shape and canonical logical-record hash are checked;
- test clocks and temporary stores are deterministic and repository-contained;
- the profile registry contains exactly the approved seven aliases and only
  unvalidated 9router placeholders.

M0 has no semantic storage write path. The rollback boundary is removing the
new project-local scaffold files; no user-level configuration or existing data
has been touched.
