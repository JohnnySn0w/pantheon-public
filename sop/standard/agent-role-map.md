# Agent persona ↔ role-id mapping

**Status:** Established 2026-05-04 by T-0056. Single source of truth: `tools/agent_role_map.py`.

## Why two namespaces

Pantheon agents carry two parallel identifiers:

- **Persona name** (human-readable): `orchestrator`, `specialist`, `recovery`, … — used by systemd unit instances (`pantheon-inbox-consumer@orchestrator.service`), Telegram operator prose, the inbox-consumer inject prompt's `target=<agent>` substitution, and ticket-grooming digests.
- **Role ID** (audit-chain controlled vocabulary) — used by ledger `agent` fields (ORG-001 as amended 2026-10-08, the operator TG 11474: the ORG-100 role id, with the seat slot as `executor`), and accepted as aliased input for ticket `target` / `requester` fields (ORG-003).

The divergence emerged when the Orchestrator chose the personal name "Orchestrator" while the audit-chain rule (`/home/claude/CLAUDE.md` §Identity) kept actor IDs role-shaped for ledger continuity. Since the operator's ORG-100 roles vocabulary (T-3807, executed T-3814 on 2026-09-19), the namespaces diverge for every persona: each seat's role ID is its ratified role class (table below). Persona names are canonical for ticket `target`/`requester`; role IDs are accepted as aliased input and normalized to persona names on write (T-0492/msg_1661).

Two properties the normalize-to-persona model has to handle:

- **Legacy input aliases.** Retired audit IDs still resolve, for audit continuity of historical ledger entries: `orchestrator` → orchestrator (T-3814), `specialist` → specialist (T-3807), `consultant` → codex. See `PERSONA_ALIASES`.
- **One role class, two seats.** `mason` and `artisan` share `engineering_orchestrator`, so that ID does not identify a single persona. Address one of them by persona name, or the pool by the ORG-088 form `role:engineering_orchestrator`. A bare shared role ID is never reverse-resolved to one seat (T-4044 fix: it passes through unchanged); in the ledger, `executor` carries the seat slot that tells the two apart.

## Canonical mapping

Live source of truth is `AGENT_ROLE_MAP` in `tools/agent_role_map.py`; this table mirrors it.

| Persona        | Role ID                          |
|----------------|----------------------------------|
| orchestrator          | head_engineering_orchestrator (legacy alias: orchestrator) |
| specialist          | head_research_orchestrator (legacy alias: specialist) |
| recovery           | head_system_orchestrator         |
| codex          | head_auditor (persona alias: consultant) |
| archivist      | head_memory_orchestrator         |
| mason          | engineering_orchestrator         |
| artisan          | engineering_orchestrator         |

## How agents use the map

`tools/inbox_consumer.py` resolves `--agent <persona>` through `resolve_role()` and matches a ticket if `target ∈ {persona, role_id}`. This lets tickets address either surface without the consumer mis-skipping.

Add new pairs by appending to `AGENT_ROLE_MAP` in `tools/agent_role_map.py` and adding a row to the table above.

## Not this namespace: instance role-keys

This doc covers the **persona ↔ audit-role-id** mapping only. There is a SEPARATE namespace — **instance role-keys** (`orchestrator`, `specialist`, `head-system-orchestrator`, `engineering-orchestrator-1/2`, `head-memory-orchestrator`, `peer-daemon-codex`; renamed from `recovery`/`sub-orchestrator-1/2`/`second-head` by the operator's T-3807 roles vocabulary) — used by `repos.yaml` grants and resolved via `instance.yaml` + `resolve_instance_role()` (ORG-030 Q4). A reader hunting role-keys wants that surface, not this one; see `instance.yaml`, `repos.yaml`, and `tools/agent_role_map.py`'s `resolve_instance_role()` (T-1922 landed the ORG-042/043 seat keys there).

## See also

- `tools/agent_role_map.py` — module + `resolve_role()` (audit-role-id) and `resolve_instance_role()` (instance role-key) helpers.
- `architecture/adr/ORG-003-ticket-system.md` §Front-matter field semantics — controlled vocabulary for `target` / `requester`.
- `/home/claude/CLAUDE.md` §Identity (HO-pinned, auto-loaded — home dir, not a repo file) — audit-chain continuity rule for actor IDs.
