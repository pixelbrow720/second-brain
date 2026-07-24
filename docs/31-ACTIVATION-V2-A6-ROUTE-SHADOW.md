# Activation V2 A6 Synthetic Route Shadow

Status: complete as synthetic-local evidence on 2026-07-24. A6 evaluates a
public, structured intent corpus in a disposable runtime. It does not accept a
prompt body, create a Codex session, serialize an outbound provider request,
call a provider, change a route default, or install a launcher.

## Contract

`src/second_brain/activation_v2_router_shadow.py` implements only a rule-first
classifier with an exact `CLI_SHADOW` fixture policy. Each input contains fixed
enums and booleans for task shape, admission lane, risk/scope, optional approved
explicit profile, and the seven Luna hard-gate facts. Free text, task content,
URLs, headers, credential fields, and prompt/transcript fields are not in the
input contract.

The rule order is deliberately conservative:

- an approved explicit choice is preserved only when the structured safety gate
  permits it;
- high-risk, ambiguous, cross-project, or multi-step work selects `tera-max`;
- known read, scoped-build, difficult-debug, and fully-gated finite-utility
  shapes select their documented profile; and
- a failed Luna gate or unknown shape visibly falls back to `tera-max`.

Each route-shadow receipt carries IDs/digests, selected/expected approved alias,
effort, bounded reason codes, match or mismatch status, and explicit flags for
zero model-classifier calls, provider-network calls, session creation, default
changes, prompt persistence, authority writes, and global writes. A passing
corpus report binds the opaque receipt IDs/digests, fallback count, and measured
local classifier latency. It is not 9router or provider attestation.

## Fail-Closed Evidence

- The canonical eight-case `PUBLIC_SYNTHETIC` corpus covers read explanation,
  scoped implementation, difficult debugging, fully gated Luna utility,
  uncertainty fallback, high-risk/cross-project override, safe explicit choice,
  and unsafe explicit Luna override.
- Corpus evaluation yields eight expected matches, three visible fallbacks,
  zero model/provider/session/default actions, and a bounded local latency
  receipt under a patched deterministic clock.
- A raw prompt field, non-`CLI_SHADOW` policy, and latency overrun fail before a
  route-shadow receipt path is created.
- An intentionally wrong expected profile writes a visible
  `MISMATCH_PROFILE` receipt but fails closed before it can produce a passing
  corpus report.
- Rehashed receipt/report boundary flags fail on readback rather than claiming
  a provider call or session creation did not occur.

The checked-in contracts and fixtures are:

- `schemas/activation-v2-route-shadow-corpus-v1.json`
- `schemas/activation-v2-route-shadow-receipt-v1.json`
- `schemas/activation-v2-route-shadow-report-v1.json`
- `fixtures/canonical/activation-v2-route-shadow-corpus-v1.json`
- `fixtures/canonical/activation-v2-route-shadow-receipt-v1.json`
- `fixtures/canonical/activation-v2-route-shadow-report-v1.json`
- `tests/test_activation_v2_a6.py`

## Boundary And Next Gate

A6 is an offline evaluator, not a front-door integration. `CLI_SHADOW` remains
a fixture policy input and there is no executable CLI launcher, session API,
provider request, global configuration target, or default-route mutation.

A7 may prepare local exact approval-packet machinery, backups, canary plans,
readback checks, and rollback support against supplied synthetic target
snapshots. It must stop before reading or mutating any real global target until
the user provides current target choices and explicitly approves a current
packet.
