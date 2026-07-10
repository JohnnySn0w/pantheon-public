# Recovery telegram-bot service recovery

**Status:** Draft 2026-05-21 (filed from the 2026-05-21 ~14h Recovery outage; pending the operator ratification per the sop/remediation per-SOP ratification rule). Author: Recovery.

**Generalized by [`persona-daemon-liveness-recovery.md`](persona-daemon-liveness-recovery.md) (T-0873, 2026-06-06)** — the persona-agnostic version covering Orchestrator/Specialist/Recovery is now canonical for the recovery procedure. This Recovery-scoped doc is retained for its 2026-05-21 RCA detail.

How to reset and bring Recovery's own Telegram-bot service back up. Audience: whoever is recovering Recovery — the operator directly, HO/Orchestrator via sudo, or a fresh Recovery reading this after a restart.

## Substrate (verified 2026-05-21)

- **Service:** `claude-telegram-fallback.service` — a `systemctl --user` unit on the `claude-fallback` account (uid 1001).
- **Unit file:** `/home/claude-fallback/.config/systemd/user/claude-telegram-fallback.service`
- **tmux session:** `claude-telegram-bot-fallback` (running `claude --channels plugin:telegram@claude-plugins-official`).
- **Supervision (post-2026-05-21):** `Type=simple` + `Restart=always` + `RestartSec=5`, MainPID is `~/.local/bin/recovery-bot-supervise.sh` which keeps the named tmux session alive and exits when it dies so systemd respawns it. Crash-loop guard: `StartLimitIntervalSec=600` / `StartLimitBurst=20`.

## Failure shape

Three distinct modes — **`systemctl is-active` does NOT distinguish them; `tmux capture-pane` is the source of truth.**

1. **Inert daemon** — tmux session + `claude` process exited; nothing running. *Post-fix this auto-restarts;* only persists if the crash-loop guard tripped (unit shows `failed`). (Pre-fix, this was the default outcome — the old `Type=oneshot`+`RemainAfterExit` couldn't see the exit.)
2. **Alive-but-erroring** — process running, `systemctl is-active` = `active`, but **every API request 400s**. *This was the 2026-05-21 trigger* (`400 role 'system' is not supported on this model`, 162 failures over ~14h on an 8-day/2551-message session). **Auto-restart does NOT catch this** — the process never exits. Needs manual detection + restart, or a health watchdog (not yet built; see Cross-references).
3. **Wedged TUI dialog** — e.g. malformed `~/.claude/settings.json` blocks at a "Settings Error" prompt. capture-pane shows the dialog.

## Diagnosis (always capture the pane first)

```bash
# From Recovery's own account:
tmux capture-pane -t claude-telegram-bot-fallback -p | tail -40   # source of truth
systemctl --user status claude-telegram-fallback.service
journalctl --user -u claude-telegram-fallback.service -n 100 --no-pager
```
For mode 2 (alive-but-400ing), the pane shows repeated `API Error: 400 …`. Confirm in the live session transcript under `~/.claude/projects/-home-claude-fallback/*.jsonl` (grep `not supported on this model`).

## Recommended remediation

1. **Restart the service (preferred — clean lifecycle, and the cure for mode 2).**
   ```bash
   systemctl --user restart claude-telegram-fallback.service
   ```
   Restart spawns a **fresh session** (messageCount resets). The 2026-05-21 poison lived in accumulated session/request state, so a fresh session clears it — restart is the fix, not a config edit.

2. **If the unit is `failed` (crash-loop guard tripped):**
   ```bash
   systemctl --user reset-failed claude-telegram-fallback.service
   systemctl --user restart claude-telegram-fallback.service
   ```

3. **If systemd is wedged / refuses — force via tmux, then restart:**
   ```bash
   tmux kill-session -t claude-telegram-bot-fallback
   systemctl --user restart claude-telegram-fallback.service
   ```

4. **Mode 3 (wedged on malformed settings):** restore `~/.claude/settings.json` from a known-good backup (snapshot hooks / `~/.claude/backups/`), then restart. Only hand-edit config if the failure points there — every edit is a chance to make recovery harder.

### Cross-account (someone else recovering Recovery)

`sudo -u` strips `XDG_RUNTIME_DIR`, which breaks `systemctl --user`. Restore it (Recovery uid = 1001, but compute dynamically):

```bash
sudo -u claude-fallback XDG_RUNTIME_DIR=/run/user/$(id -u claude-fallback) \
  systemctl --user restart claude-telegram-fallback.service
# tmux peek (no XDG needed):
sudo -u claude-fallback tmux capture-pane -t claude-telegram-bot-fallback -p | tail -40
```

## Verification after recovery

```bash
systemctl --user is-active claude-telegram-fallback.service          # active
tmux capture-pane -t claude-telegram-bot-fallback -p | tail -5       # Claude Code prompt frame, no API errors
```
Then send a Telegram message and confirm a reply lands.

## Autonomous-vs-bubble-up (D3) placement

- **Autonomous:** restart / reset-failed / tmux-kick / restore-settings-from-backup — Recovery (or a recoverer) applies these directly; this is the in-scope reversible recovery path.
- **Bubble up to the operator:** crash-loop guard repeatedly exhausting after restarts (genuinely broken, not transient); any need to *edit* (not restore) config or change the model; a recurrence of the mode-2 400 outage (signals the upstream harness/model cause is unresolved).

## Cross-references

- RCA: 2026-05-21 outage (window 02:16:29Z–16:40:34Z; transcript `2f411d82`). Trigger = harness/model edge on a giant long-lived session; duration = missing self-recovery.
- `~/.claude/projects/-home-claude-fallback/memory/recovery_runbook.md` — the *inverse* runbook (Recovery recovering the **prime** bot). Same architecture, same `is-active`-lies gotcha.
- Reference impl for the auto-restart unit: `~/.local/bin/recovery-bot-supervise.sh` + the unit file above. T-0509 mirrors it to HO + Specialist.
- **GAP / follow-up:** mode 2 (alive-but-400ing) has no automated detection. A health watchdog (probe the bridge / tail the transcript for sustained API errors → force restart) is the missing piece that would have auto-closed the 2026-05-21 outage. Recommend a ticket.
