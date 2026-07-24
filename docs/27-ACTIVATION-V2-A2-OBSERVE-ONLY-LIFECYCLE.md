# Activation V2 A2 Observe-Only Lifecycle Receipts

Status: complete as synthetic-local evidence on 2026-07-24. This phase models
an adapter; it does not install a Codex hook, register a lifecycle callback, or
capture a running task.

## Contract

`src/second_brain/activation_v2_lifecycle.py` accepts only five named event
shapes: `SessionStart`, `UserPromptSubmit`, `PreCompact`, `PostToolUse`, and
`Stop`. Each event admits exactly one small metadata field:

| Event | Allowed metadata | Persisted receipt content |
| --- | --- | --- |
| `SessionStart` | Redacted project-binding digest | Field name and payload digest only. |
| `UserPromptSubmit` | Memory-mode enum | Field name and payload digest only; no prompt. |
| `PreCompact` | Closure-request boolean | Field name and payload digest only. |
| `PostToolUse` | Opaque allowlisted artifact/reference IDs | Field name and payload digest only; no command or output body. |
| `Stop` | Closure-request state enum | Field name and payload digest only. |

The persisted `LifecycleReceipt` is immutable/digest-bound and requires
`recording_mode: observe_only`, `content_persisted: false`,
`authority_write: false`, and `hook_installed: false`. Its only destination is
a disposable synthetic runtime receipt path.

## Adversarial Evidence

- Extra prompt/transcript/tool-body fields fail before a receipt path is
  created, and errors do not echo the rejected marker.
- Prompt-injection and absolute-path markers are rejected by the shared content
  barrier before metadata can be hashed.
- Non-opaque `PostToolUse` identifiers fail closed.
- Duplicate receipt IDs do not overwrite a prior receipt.
- A receipt tampered to claim hook installation fails at readback.

The exact public synthetic event fixture is
`fixtures/activation-v2/a2-lifecycle-events-v1.json`; the canonical receipt
schema/fixture is `activation-v2-lifecycle-receipt-v1`. No payload body is
checked into an artifact or runtime receipt.

## Next Gate

A3 may consume only explicit synthetic TaskClosure fixtures and selected
synthetic records. It must keep recovery and promotion as proposals, with no
authority transaction or global write. Real lifecycle installation, persistent
capture, and policy defaults remain unapproved.
