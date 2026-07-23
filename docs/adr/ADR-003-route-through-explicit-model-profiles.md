# ADR-003: Route Through Explicit Model Profiles

- Status: Accepted for blueprint v1
- Date: 2026-07-22

## Context

The user wants a fixed model set and uses a custom 9router endpoint. Router
updates have previously caused requested reasoning effort to be observed as
`xhigh` even when another effort was selected. Raw model strings distributed
through prompts and agent files are difficult to audit and migrate.

## Decision

Use stable workflow profile aliases mapped centrally to exactly these options:

- Tera Max
- Tera xhigh
- Tera high
- Sol Max
- Sol xhigh
- Luna xhigh
- GPT-5.5 xhigh

The root chat defaults to Tera Max. Every delegated node names one profile
explicitly. Luna is restricted to bounded mechanical work and cannot own
planning, security decisions, destructive actions, or final acceptance.

Routing integrity is an end-to-end contract:

```text
profile intent -> Codex agent config -> serialized API request
               -> 9router observation -> provider/runtime telemetry
```

Canaries must compare all observable stages. A mismatch is visible and blocks
claims that the intended profile was used. The system never silently rewrites
an unsupported effort to `xhigh`.

## Consequences

- Router/model migrations change one mapping layer instead of every workflow.
- Model use becomes testable rather than inferred from agent names.
- If downstream telemetry is unavailable, the result is `unverified`, not a
  fabricated success.
- Provider aliases and credentials remain user-level configuration and are not
  committed to this repository.
