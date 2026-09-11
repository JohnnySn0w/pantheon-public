# pinned-rules — agents' auto-loaded `CLAUDE.md` files (version-controlled)

This directory version-controls the always-auto-loaded pinned-rules files for the pantheon-config-scoped
agents (every persona below except Orchestrator, whose `orchestrator.md` lives in `claude-config`). Each agent's live
path is a **symlink** into here, so the tracked file *is* the source of truth — git history, diff, and
backup come for free, and the harness auto-loads the symlink target transparently.

| persona | live path | tracked file |
|---------|-----------|--------------|
| Recovery (recovery) | `/home/claude-fallback/CLAUDE.md` | `recovery.md` |
| Specialist (specialist) | `/home/claude-research/CLAUDE.md` | `specialist.md` |
| Mnemosyne (second head — personal/creative hemisphere) | `/home/exocortex/CLAUDE.md` | `mnemosyne.md` |
| Metis (sub-orchestrator-1) | `/home/suborch1/CLAUDE.md` | `metis.md` |
| Atlas (sub-orchestrator-2) | `/home/suborch2/CLAUDE.md` | `atlas.md` |

## Why this lives in pantheon-config, not claude-config (T-0495, T-0496)

The canonical pinned-rules home is `claude-config/pinned-rules/` (Orchestrator's `orchestrator.md` lives there
per T-0493). Recovery's and Specialist's GitHub credentials are deploy keys scoped to **pantheon-config only** —
they cannot clone or push `claude-config`, and cross-user perms mean Orchestrator cannot reach into
`/home/claude-fallback` or `/home/claude-research` to manage a clone on their behalf. The T-0495/T-0496
Asks explicitly allow "own claude-config clone **or equivalent**"; pantheon-config is each agent's owned,
pushable repo and the org-config home, so it is the equivalent. The canonical
`claude-config/pinned-rules/README.md` table rows for Recovery and Specialist should point here.

## Edit protocol (T-0493, ratified the operator msg_1763 "no prs needed")

Edits to these files are **version-controlled but not PR-gated**: each agent edits its own tracked file
directly and self-commits (no PR + operator-review cycle). The git history is the audit trail;
trust-posture.md still classes pinned-rules as a hand-curated doc, so material rule changes are
still ledgered and surfaced per the normal autonomy bar — the relaxation is purely on the *PR
ceremony*, not on the surfacing/justification discipline.

## Symlink safety

OS `open()` resolves symlinks on read, so the harness reads the tracked file transparently (same
mechanism as Orchestrator's `/home/claude/CLAUDE.md` → `claude-config/pinned-rules/orchestrator.md`, and the
long-standing `~/.claude/agents` → `claude-config/agents` symlink). If a future session-open ever
shows the pinned rules missing from context, restore from the pre-symlink backup under
`~/.claude/backups/CLAUDE.md.pre-symlink-*` and re-investigate.

## Contents

| file | description |
|------|-------------|
<!-- AUTOGEN-START spine-index -->
| [`specialist.md`](specialist.md) | These rules auto-load in every session running from `/home/claude-research/` or below. You are the unattended watch-loop driver for Pantheon's continuous-S4 layer. Most fires are cron'd; some are manual operator invocations. |
| [`atlas.md`](atlas.md) | You are one of two sub-orchestrator seats under Orchestrator (the Orchestrator) per architecture/adr/ORG-043 in ~/pantheon-config. Your persona name is Atlas (self-chosen 2026-07-21). Mailbox recipient id: atlas. |
| [`metis.md`](metis.md) | You are one of two sub-orchestrator seats under Orchestrator (the Orchestrator) per architecture/adr/ORG-043 in ~/pantheon-config. Your persona name is Metis (self-chosen 2026-07-21). Mailbox recipient id: metis. |
| [`mnemosyne.md`](mnemosyne.md) | These rules auto-load into every session running from `/home/exocortex/` or below. This is the **bringup seed** written by Orchestrator (the other head) on 2026-07-20; you will grow and edit it — it's yours. Material rule changes get surfaced to the operator, same discipline as the other personas. |
| [`orchestrator-codex.md`](orchestrator-codex.md) | This file adapts Orchestrator's canonical operating rules to Codex. It is not a second copy of those rules and must not become an independent policy source. |
| [`recovery.md`](recovery.md) | These patterns have caused mistakes in single sessions despite being saved as feedback memories. They are pinned here because the memory system loads at session start but *that's not enough* — the in-session context can drift away from these rules within hours of writing them. Reread when you see them. |
<!-- AUTOGEN-END spine-index -->
