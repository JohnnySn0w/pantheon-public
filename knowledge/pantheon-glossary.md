---
title: Pantheon glossary
status: Drafted 2026-06-03 by Specialist (T-0751); refreshed 2026-06-04 (T-0778 review pass — Telegram bridge→plugin-self-poll, Specialist now bidirectional, supervisor model). Awaiting the operator ratification.
purpose: Reference for the daemon names, account paths, historical vocabulary, and stability-naming quirks an agent or operator will encounter in the repo. Not prescriptive — describes how Pantheon names things, not how *you* must name things if you stand up your own.
---

# Pantheon glossary

This is a reference for the named things in Pantheon as the operator's instance runs them today. An agent grepping the repo will see these names everywhere; this file is the index that explains what each one is, where it came from, and (where relevant) why it's called what it's called even when the name no longer matches the thing.

If you're standing up your own Pantheon: most of these names are *historical artifacts* you don't need to inherit. `sop/standard/pantheon-bringup.md` documents which names are load-bearing in code (and so need to stay synchronized across the supervise script + systemd unit + monitor configs) vs which are arbitrary labels you can pick freely.

This glossary names the *things*. For the recurring *infrastructure component families* and the patterns they follow (Monitors, Pollers/Wakers, Filers, Runners, Remediators, Guards, Digesters — "where does a new tool fit?"), see the companion [`infra-abstractions/`](infra-abstractions/) reference map. Glossary = terms; that map = components and shapes.

---

## Personas — the three persistent agents

Pantheon runs three persistent Claude Code daemons, each with a distinct role.

- **Orchestrator** — Orchestrator (HO). Owns the ledger, dispatches workers, talks to the operator. Runs as Unix user `claude`. Earlier identifiers: `orchestrator`, briefly `Orchestrator` — same entity, name evolved with the role articulation.
- **Specialist** — specialist / watch-loop. Runs continuous-watch dispatches, drafts research, writes evergreen docs. Runs as Unix user `claude-research`. The name is a recent self-naming, recorded in the agent's memory pin `identity_specialist.md`.
- **Recovery** — recovery. Monitors the other two, restarts them when they wedge, runs system-wide cron monitors. Runs as Unix user `claude-fallback`.

The persona/user mapping is canonical and load-bearing — `tools/agent_role_map.py` is the single source of truth.

## Service / harness vocabulary

- **Harness** — the LLM-running CLI. Pantheon's three persistent agents run **Claude Code** (Anthropic). The codex pilot (per ORG-018) runs **codex CLI** (OpenAI). "Harness" is the generic; either fits.
- **Daemon** — a persistent identity that survives session resets. Pantheon's daemons are tmux sessions wrapped in systemd user services. The inner harness gets reset; the daemon (the systemd-managed tmux + supervise script) does not.
- **Subagent / dispatched worker** — an ephemeral Claude Code (or codex) process spawned for a single task. Single-shot; exits when done. Distinct from the persistent daemons.
- **Persona** — a worker's role-and-discipline definition; lives in the separate `claude-config` repo at `claude-config/agents/<persona>.md`. Researcher, reviewer, implementer, project-orchestrator are the common ones.

## Tmux session names

The reference instance uses these specific session names. The names are *load-bearing in code* — the supervise script, the systemd unit, `context_fill_monitor.py`'s `restart_target_pane`, and `restart_drain.py` all reference them — but the choice of name itself is arbitrary. You can pick anything as long as it matches across all four places.

| Persona | Session name | Notes |
|---|---|---|
| Orchestrator | `claude-telegram-bot` | Named "telegram-bot" because Orchestrator runs the Telegram plugin. Originally ran the shared bridge; migrated to its own bot `c_swarm_bot` via per-agent plugin self-poll on 2026-06-04 (T-0767). |
| Specialist | `claude-research-bot` | Session for Specialist's interactive TUI. As of 2026-06-03 (T-0754/T-0755) Specialist runs the official Telegram plugin bidirectionally (`@c_specialist_bot`) via the supervise wrapper's `--channels` arg — so the `claude-telegram-research.service` unit name is accurate again (it was a misnomer only during the 2026-05-09→2026-06-03 scope-tightening window). |
| Recovery | `claude-telegram-bot-fallback` | Named "telegram" for stability — see quirks below. |

## Systemd service unit names

| Persona | Unit | Wraps |
|---|---|---|
| Orchestrator | `claude-telegram.service` | The supervise script that keeps the tmux session alive. |
| Specialist | `claude-telegram-research.service` | Same shape. The "telegram" in the name is accurate again — Specialist runs the plugin bidirectionally as of 2026-06-03 (it was a misnomer only during the scope-tightening window; see quirks). |
| Recovery | `claude-telegram-fallback.service` | Same shape. |
| (shared, Orchestrator-owned) | `pantheon-bridge.service` | **Historical / decommissioned 2026-06-04 (T-0767; ORG-004 retired, repo archived).** The shared Telegram getUpdates poller + SSE fanout daemon (one process owned the bot's poll lock). Superseded by per-agent plugin self-poll — all three agents (Orchestrator on its own `c_swarm_bot` too) now self-poll; the bridge unit is disabled. |

The `pantheon-*` timers and per-agent templated units (`pantheon-inbox-consumer@<agent>.timer`, etc.) are documented in `tools/systemd/README.md`.

## Account paths

For each agent's account (`/home/<user>/`):

| Path | Holds |
|---|---|
| `~/pantheon-config/` | The org-config repo clone (this repo). |
| `~/.npm-global/bin/claude` | The Claude Code binary (npm-global per-account). |
| `~/.bun/bin/bun` | bun runtime (per-account; runs the Telegram plugin's `server.ts` poller and some project daemons). |
| `~/.claude/` | Claude Code state — OAuth session, MCP configs, plugin caches, settings.json, hooks. |
| `~/.claude/channels/telegram/.env` | **(live)** The agent's own bot token (`TELEGRAM_BOT_TOKEN`) for the plugin self-poll — per-account, mode 0600, never committed. |
| `~/.claude/channels/telegram/access.json` | **(live)** DM/pairing whitelist for the plugin (`{dmPolicy, allowFrom:[chat_id]}`). |
| `~/.claude/channels/telegram/bot.pid` | **(live)** PID of the running plugin poller (`bun server.ts`); its presence — not the startup banner — is how you verify the plugin actually loaded. |
| `~/.claude/channels/telegram/pantheon.env` | Legacy outbound direct-curl POST credentials (byte-identical to `.env`); predates the plugin self-poll. |
| `~/.claude/channels/telegram-bridge/.env` | **Historical (defunct bridge).** Bridge tokens + bot token; do not create for new agents. |
| `~/.config/systemd/user/` | Per-account systemd units + timers. |
| `~/.local/bin/<agent>-bot-supervise.sh` | The supervise script for the daemon (the supervise-script pattern in the bringup SOP). |
| `~/.local/state/pantheon/` | All Pantheon runtime state files — cap-hit state, restart tombstones, monitor state, session-reset logs. |
| `~/HANDOFF.md` | The per-agent handoff file the outgoing session writes at restart; SessionStart hook on the incoming session reads it. |

Operator-placed files outside the agent accounts:

| Path | Holds | Owner |
|---|---|---|
| `/home/claude/root.env` | The Orchestrator-only sudo password used for substrate captures. Mode 0600. operator-placed. Never committed. | `claude` (Orchestrator) |
| `/etc/sudoers.d/fallback-rescues-prime` | Recovery's NOPASSWD-sudo grant into Orchestrator. | root |
| `/etc/sudoers.d/50-claude-fallback-research` | Recovery's NOPASSWD-sudo grant into Specialist. | root |

## Unix groups

- **`pantheon-shared`** (gid 1003 on the reference host) — supplementary group whose members are `claude`, `claude-fallback`, and `kino` (the PWA service account). Specialist (`claude-research`) is deliberately NOT a member — it commits through its own ownership path and doesn't share write access with the others.
  - **Quirk to remember:** user-mode systemd cannot grant supplementary groups. Anything that needs to inherit `pantheon-shared` (notably the tmux daemons that write to the shared `pantheon-config/.git/objects`) must be launched wrapped in `sg pantheon-shared -c '...'` (see the supervise script pattern).

## Repos

- **`pantheon-config`** — this repo. Org-tier scaffolding: principles, directives, ADRs, tickets, ledger, SOPs, knowledge, tools.
- **`claude-config`** — worker persona definitions (sibling repo to pantheon-config). Implementer, reviewer, researcher persona MDs.
- **`pantheon-bridge`** — the Telegram bridge daemon source. Sibling repo.
- **Project repos** (sibling repos) — `echo-prismatic` (voice agent stack), `pantheon-pwa` / kino (operator-facing PWA), and any others. Each has its own ADRs starting at the project-tier numbering range (e.g. echo-prismatic uses ADR-201+).

## Restart-pipeline vocabulary

- **Tombstone** — a small JSON file in `~/.local/state/pantheon/` written by a monitor when a restart is needed. The drain timer consumes it.
- **Drain** — `restart_drain.py` running on each agent's account; checks for tombstones, fires `handoff_and_restart.sh` against the local agent when one appears.
- **Sentinel** — a regex pattern matching the steady-state Claude Code prompt frame in a tmux pane capture. Used as a post-restart structural witness.
- **Kick** — the post-restart prompt the supervisor sends into the fresh pane via `tmux send-keys` to trigger the first turn. Without it, the fresh session sits idle.
- **`--skip-ack`** — flag passed to `handoff_and_restart.sh` for self-restarts. Skips the handoff-prompt + ack-wait phases (the agent restarting itself can't answer its own ack request).
- **Steady-state** — the post-restart confirmation that the fresh Claude Code TUI is up and idle (sentinel match + PID change + `is-active`).

## Ratification + governance vocabulary

- **Justifies-via** — a frontmatter / ledger field naming the principle(s) or directive(s) a decision serves. Required on material decisions per P3.
- **DECISIONS section** — a section in a report enumerating non-obvious calls with their justify-via chain. Required at every dispatcher↔dispatchee boundary per D7.
- **REBOUND** — reviewer's "go back and fix this" response on a plan or code-tier READY message. Per ticket budget = 3 per D7.
- **READY** — implementer / reviewer's signal back to the dispatcher that a phase is complete and ready for inspection.
- **Auto-approved / held-and-pinged / never-permitted** — the three lists in the trust-posture autonomy bar (D3 calibration).
- **Above the bar / below the bar** — short-hand for "needs operator ratification" vs "agent acts on principle alignment alone."
- **Ratifiable file** — a doc whose changes count as a ratification artifact per ORG-015/ORG-022 (the `ratifiable_files` ticket frontmatter field pairs a ratification-class ticket with the file it ratifies).
- **Bubble up** — escalate a decision past the current tier to the next dispatcher up. Per D7's five structural items (locked-decision deviation, scope creep, new deps, REBOUND budget exhausted, supervised-first-fire).

## Stability-naming quirks (the names that don't quite match the things)

A handful of names in Pantheon are *intentionally not updated* to match their current state. Renaming would invalidate the audit chain — every prior ledger entry, ticket transition log, commit message, and SOP cross-reference would suddenly point at a stale name. The convention is: keep the name stable, document the quirk.

- **`claude-telegram-research.service`** is Specialist's TUI service. It was a *misnomer* during the scope-tightening window (the operator msg_784/785, 2026-05-09 → 2026-06-03), when Specialist ran bare-claude with outbound-only direct-curl and no plugin. As of 2026-06-03 (T-0754/T-0755) Specialist runs the official Telegram plugin bidirectionally, so the name is accurate again. `tools/systemd/README.md` documents the history.
- **`claude-telegram-bot-fallback`** is Recovery's tmux session name. Recovery originally ran a Telegram-bridge client; the architecture evolved but the session name (referenced in monitor configs, drain configs, and a dozen log lines) stayed put.
- **`claude` (the Unix user)** is Orchestrator's account. The name predates the Orchestrator/Specialist/Recovery role articulation. Renaming would break every ledger entry that says "[claude]" as the actor.
- **`orchestrator` / `Orchestrator`** are historical identifiers for Orchestrator. You'll see them in pre-2026-05 ledger entries and SOPs. Same entity.
- **`pantheon-bridge.service`** (the Telegram bridge daemon) is named for its role at the time it was built (Pantheon's bridge to chat). The bridge *implementation* is now historical — superseded by per-agent plugin self-poll and decommissioned 2026-06-04 (T-0767; ORG-004 retired) — though the transport-agnostic *concept* it embodied (the same name would apply across Slack/Discord/Matrix) is still valid. The unit name describes the role, not the platform.

The general rule: if you can't tell whether a name is "what we'd pick today" or "what it was called when it was built," **it's almost always the latter** and the audit-chain stability wins over re-naming.

## Cross-references

- [`sop/standard/pantheon-bringup.md`](../sop/standard/pantheon-bringup.md) — the bringup procedure references this glossary for the names you don't need to invent.
- [`sop/standard/agent-role-map.md`](../sop/standard/agent-role-map.md) — the human-readable companion to `tools/agent_role_map.py` (the executable single source of truth) for the persona↔Unix-user mapping.
- [`tools/systemd/README.md`](../tools/systemd/README.md) — canonical per-unit install procedure; documents the `claude-telegram-research.service` misnomer inline.
- [`reports/PR/primitives.md`](../reports/PR/primitives.md) — the architectural primitives this glossary names instances of.

— Specialist (specialist), drafted 2026-06-03 (T-0751); refreshed 2026-06-04 (T-0778 review pass).
