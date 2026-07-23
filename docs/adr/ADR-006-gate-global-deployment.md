# ADR-006: Gate Global Deployment

- Status: Accepted for blueprint v1
- Date: 2026-07-22

## Context

The final system will affect all Codex projects through user-level guidance,
agents, skills, plugins, MCP configuration, or hooks. A project-local prototype
cannot prove that a global rollout is safe, fast, or compatible with every
repository.

## Decision

All development remains project-local until an exact global approval packet is
accepted. The packet must contain:

- every target path and before/after digest;
- the exact config, agent, skill, plugin, MCP, and hook changes;
- permissions and network impact;
- routing, context, recovery, and security canary evidence;
- rollback steps that restore the complete before-state;
- known limitations and unresolved risks.

Rollout stages are `local test -> shadow -> opt-in profile -> limited global ->
default global`. Failure at any gate rolls back or leaves the prior layer
active. The implementation must not modify `~/.codex`, the old Obsidian vault,
or global MCP/plugin state while building the local prototype.

## Consequences

- Blueprint and implementation can progress without risking the current host.
- Global activation is slower but auditable and reversible.
- The new-session implementation prompt starts locally and treats global
  installation as a later milestone requiring fresh user approval.
