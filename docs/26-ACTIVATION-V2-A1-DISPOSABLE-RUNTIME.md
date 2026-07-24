# Activation V2 A1 Disposable Runtime

Status: complete as synthetic-local evidence on 2026-07-24. This checkpoint
creates disposable test directories only under `artifacts/test-runs/`; it does
not initialize `runtime/`, a user store, a hook, a launcher, or a global Codex
surface.

## Scope And Policy Boundary

`src/second_brain/activation_v2_runtime.py` accepts an absolute non-symlink
root only below the repository's ignored `artifacts/test-runs/` boundary. It
creates private empty directories for registry, project/global synthetic data,
outbox, receipts, and backups. Every runtime manifest binds
`fixture_only: true` and a digest of explicit policy inputs.

The recommended 30-day / `ASSISTED` / encrypted-disk / generated-snapshot /
CLI-shadow / per-item-review values exist only in canonical fixtures and
disposable tests. They are not a user policy decision, global default, or
personal-data retention rule. `PolicyInputs.unresolved()` fails before every
phase that needs a real policy decision.

## Safety Controls

- Path containment rejects external roots, missing parents, symlink components,
  non-private runtime directories, and nonempty replacement targets.
- Runtime writes accept bounded JSON objects only in allowlisted runtime areas.
  The shared V2 content barrier rejects raw-input field names, secret markers,
  transcript markers, prompt injection, and absolute paths.
- Backups copy only validated safe JSON, exclude backup recursion, bound file
  count and total bytes, and preserve their immutable manifest binding.
- Restore validates the full backup before clearing any synthetic data, retains
  the backup receipt, and never follows a symlink.
- `git check-ignore` is required for each disposable root. No checked-in
  runtime, authority object, or user memory is created.

## Evidence

| Acceptance | Local evidence |
| --- | --- |
| Path and symlink gate | External, symlink, nonempty, and manifest-tamper tests fail closed. |
| Backup and restore | A bounded synthetic metadata document restores exactly; a tampered backup leaves current synthetic state intact. |
| No Git leak | Every accepted root must be ignored and is removed with its test fixture. |
| Privacy boundary | Raw field and sentinel input is rejected before writing or backup. |
| Policy interface | Canonical policy/manifest schemas validate and their digests bind the fixture-only choices. |

Run the focused evidence with:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest \
  tests.test_activation_v2_a0 tests.test_activation_v2_a1 -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m second_brain.contract_checks
```

## Remaining Gate

A3 may use this runtime only for synthetic assisted recovery and closure
proposals. Before any real per-project runtime, capture default, retention,
storage/key-management, graph UI, router entry point, and promotion-review
policy still require explicit user decisions. Any global installation remains a
separate exact-packet and approval action.
