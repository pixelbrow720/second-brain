# ADR-004: Use Progressive Capability Disclosure

- Status: Accepted for blueprint v1
- Date: 2026-07-22

## Context

The user should not need to remember skill, plugin, or MCP names. At the same
time, exposing a large capability catalog on every turn increases first-turn
context and can trigger unnecessary workflows.

## Decision

Use the smallest surface that matches the need:

1. `AGENTS.md` for concise durable behavior and repository rules.
2. Skills for reusable task workflows with progressive disclosure.
3. Plugins only as installable distribution bundles or when hooks, MCP, apps,
   assets, or multiple skills must ship together.
4. MCP only for live external data or actions unavailable in local files.
5. Subagents only after graph admission.

Prefer a small number of domain gateway skills (`research`, `backend`,
`frontend`, `security`, `memory`, `orchestration`) whose references load only
after selection. Do not recreate dozens of narrow always-visible skills.

Capability resolution order is: reuse installed capability, use a local
built-in implementation, recommend a vetted capability, then research a new
candidate. Installation or global enablement always requires an explicit
approval packet.

## Consequences

- The user can request outcomes in natural language.
- Skill metadata remains small enough for routine chats.
- Capability discovery and supply-chain review become separate from task
  execution.
- A registry and trigger-evaluation suite are required to prevent collisions and
  false-positive skill activation.
