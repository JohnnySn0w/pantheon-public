# Agent persona ↔ role-id mapping

**Status:** Established 2026-05-04 by T-0056. Single source of truth: `tools/agent_role_map.py`.

## Why two namespaces

Pantheon agents carry two parallel identifiers:

- **Persona name** (human-readable): `orchestrator`, `specialist`, `recovery`, … — used by systemd unit instances (`pantheon-inbox-consumer@orchestrator.service`), Telegram operator prose, the inbox-consumer inject prompt's `target=<agent>` substitution, and ticket-grooming digests.
- **Role ID** (audit-chain controlled vocabulary) — used by ticket `target` / `requester` fields and ledger `agent` fields per ORG-003 §Front-matter field semantics.

The divergence emerged when the Orchestrator chose the personal name "Orchestrator" while the audit-chain rule (`/home/claude/CLAUDE.md` §Identity) kept actor IDs as `orchestrator` for ledger continuity. Only the three original personas diverge: `orchestrator`→`orchestrator` and `specialist`→`specialist`; `recovery` is identical. Every persona added since (the peer daemons and the ORG-042/043 seats) registered with a role ID equal to its own persona name — so for those, the two namespaces coincide. Per `/home/claude/CLAUDE.md` §Identity, persona names are canonical for ticket `target`/`requester`; role IDs are accepted as aliased input and normalized to persona names on write (T-0492/msg_1661).

## Canonical mapping

Live source of truth is `AGENT_ROLE_MAP` in `tools/agent_role_map.py`; this table mirrors it.

| Persona        | Role ID           |
|----------------|-------------------|
| orchestrator          | orchestrator |
| specialist          | specialist      |
| recovery           | recovery              |
| codex          | codex             |
| the-ancestor   | the-ancestor      |
| mnemosyne      | mnemosyne         |
| metis          | metis             |
| atlas          | atlas             |

## How agents use the map

`tools/inbox_consumer.py` resolves `--agent <persona>` through `resolve_role()` and matches a ticket if `target ∈ {persona, role_id}`. This lets tickets address either surface without the consumer mis-skipping.

Add new pairs by appending to `AGENT_ROLE_MAP` in `tools/agent_role_map.py` and adding a row to the table above.

## Not this namespace: instance role-keys

This doc covers the **persona ↔ audit-role-id** mapping only. There is a SEPARATE namespace — **instance role-keys** (`orchestrator`, `specialist`, `recovery`, `sub-orchestrator-1/2`, `second-head`, `peer-daemon-codex`) — used by `repos.yaml` grants and resolved via `instance.yaml` + `resolve_instance_role()` (ORG-030 Q4). A reader hunting role-keys wants that surface, not this one; see `instance.yaml`, `repos.yaml`, and `tools/agent_role_map.py`'s `resolve_instance_role()` (T-1922 landed the ORG-042/043 seat keys there).

## See also

- `tools/agent_role_map.py` — module + `resolve_role()` (audit-role-id) and `resolve_instance_role()` (instance role-key) helpers.
- `architecture/adr/ORG-003-ticket-system.md` §Front-matter field semantics — controlled vocabulary for `target` / `requester`.
- `/home/claude/CLAUDE.md` §Identity (HO-pinned, auto-loaded — home dir, not a repo file) — audit-chain continuity rule for actor IDs.
