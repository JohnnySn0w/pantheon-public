---
symptom: "Explicit per-file Edit/Write allow rules in settings.json are ignored for files under ~/.claude/ - the edit still prompts"
fix: "Relocate the target file out of ~/.claude/; the CLI special-cases that directory ahead of per-file allow rules"
domain: claude-code-harness, permissions, settings
---
# Claude Code: files under `~/.claude/` bypass per-file Edit/Write allow rules

**Surfaced:** 2026-05-13 (Recovery investigation during HO HANDOFF.md prompt loop)
**Status:** External-substrate quirk (Claude Code CLI); worked around by relocating HANDOFF.md out of `~/.claude/`.

## Observed behavior

HO's `~/.claude/settings.json` and `~/.claude/settings.local.json` both contained explicit allow rules for HANDOFF.md:

```json
"allow": [
  "Edit", "Write",
  "Edit(/home/claude/.claude/HANDOFF.md)",
  "Write(/home/claude/.claude/HANDOFF.md)",
  "MultiEdit(/home/claude/.claude/HANDOFF.md)"
]
```

Yet every Edit/Write to `/home/claude/.claude/HANDOFF.md` still triggered the interactive permission prompt:

```
 Do you want to overwrite HANDOFF.md?
 ❯ 1. Yes
   2. Yes, and allow Claude to edit its own settings for this session
   3. No
```

Note the **option-2 wording**: "allow Claude to edit its own *settings*" — not "edit HANDOFF.md". That phrasing was the giveaway. Claude Code is treating ANY file under `~/.claude/` as a settings-tier path and short-circuiting the per-file allow rules. The broad `"Edit"` allow doesn't override either.

HO was hitting this prompt 4 times in ~90 minutes during routine HANDOFF.md updates (its own dispatch-cycle close discipline). Each firing required Recovery to send `1` + Enter to unblock (post-T-0314 routing landed alerts on Recovery's pane; pre-T-0314 they routed to the operator's Telegram, which is what initially surfaced the broken-plumbing complaint msg_1254).

## Why this matters for Pantheon

`~/.claude/HANDOFF.md` was chosen as the canonical handoff location because Claude Code already touches `~/.claude/` for its own state, so it felt like the right "Claude-related" home. The naming was correct, the directory choice wasn't — Claude Code's permission special-casing means any custom file under `~/.claude/` becomes operator-blocking on routine edits, even when the operator explicitly allowed it.

## Fix

Relocate HANDOFF.md to `/home/claude/HANDOFF.md` (home root). Updates landed in:

- HO substrate (sudo as `claude`):
  - File moved: `/home/claude/.claude/HANDOFF.md` → `/home/claude/HANDOFF.md`
  - `~/.claude/hooks/handoff-auto-read.sh` — HANDOFF= + inject-text references
  - `~/.claude/settings.json` + `~/.claude/settings.local.json` — per-file allow rules updated (cosmetic — they weren't taking effect under `~/.claude/` anyway)

- pantheon-config (Recovery repo edit):
  - `tools/handoff_and_restart.sh` — HANDOFF_FILE constant + DEFAULT_PROMPT / DEFAULT_KICK_PROMPT / usage strings
  - `tools/test_handoff_and_restart.sh` — sentinel grep strings
  - `tools/hooks/README.md`, `tools/README.md`, `sop/standard/handoff-restart-runbook.md` — doc path references
  - `/home/claude-fallback/CLAUDE.md` — graceful-restart section path references

## Verification

`bash ~/pantheon-config/tools/test_handoff_and_restart.sh` re-run after the edits — expect `9 passed, 0 failed` (per CLAUDE.md smoke contract).

Post-deploy: next HO HANDOFF.md edit lands without prompt.

## Generalizable lesson

**`~/.claude/` is Claude Code's home, not Pantheon's.** Pantheon state files that need user-controlled write access (handoffs, ledgers, runtime metadata) should live OUTSIDE `~/.claude/` even if the naming feels related to Claude. Use `~/`, `~/.local/state/pantheon/`, or a project-managed dir instead. Per-file Edit/Write allow rules anchored on `~/.claude/` paths will silently not apply.

This is an external-substrate quirk (Claude Code CLI behavior), not a Pantheon bug — but it informs Pantheon's path-design discipline. The discovery and workaround don't need ratification; the lesson is captured here for the next time someone considers `~/.claude/X` as a state file home.

## Related

- T-0314 (Recovery-pane routing for orchestrator-health escalations) — the prompt-firing-repeatedly is what surfaced this quirk; Recovery saw alerts directly post-T-0314 and could investigate.
- T-0315 (allowlist expansion for HO-self-write cases) — sibling Recovery-side backstop; now largely moot for HANDOFF.md specifically once the file is relocated, but the design pattern still applies for other future self-write cases.
- T-0142 (algedonic alert routing design) — parent lineage for both T-0314 and T-0315.
