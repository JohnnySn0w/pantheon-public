# Agent persona ↔ role-id mapping

**Status:** Established 2026-05-04 by T-0056. Single source of truth: `tools/agent_role_map.py`.

## Why two namespaces

Pantheon agents carry two parallel identifiers:

- **Persona name** (human-readable): `orchestrator`, `specialist`, `recovery` — used by systemd unit instances (`pantheon-inbox-consumer@orchestrator.service`), Telegram operator prose, the inbox-consumer inject prompt's `target=<agent>` substitution, and ticket-grooming digests.
- **Role ID** (audit-chain controlled vocabulary): `orchestrator`, `specialist`, `recovery` — used by ticket `target` / `requester` fields and ledger `agent` fields per ORG-003 §Front-matter field semantics.

The divergence emerged when the Orchestrator chose the personal name "Orchestrator" while the audit-chain rule (CLAUDE.md §Identity) kept actor IDs as `orchestrator` for ledger continuity. Recovery's persona and role ID happen to be identical; Specialist's role ID is `specialist`.

## Canonical mapping

| Persona | Role ID            |
|---------|--------------------|
| orchestrator   | orchestrator  |
| specialist   | specialist       |
| recovery    | recovery               |

## How agents use the map

`tools/inbox_consumer.py` resolves `--agent <persona>` through `resolve_role()` and matches a ticket if `target ∈ {persona, role_id}`. This lets tickets address either surface without the consumer mis-skipping.

Add new pairs by appending to `AGENT_ROLE_MAP` in `tools/agent_role_map.py` and adding a row to the table above.

## See also

- `tools/agent_role_map.py` — module + `resolve_role()` helper.
- `adr/ORG-003-ticket-system.md` §Front-matter field semantics — controlled vocabulary for `target` / `requester`.
- `~/CLAUDE.md` §Identity (HO-pinned) — audit-chain continuity rule for actor IDs.
