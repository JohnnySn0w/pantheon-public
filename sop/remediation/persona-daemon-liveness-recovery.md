# Persona daemon liveness — diagnose + recover

**Status:** Draft 2026-06-06 (T-0873; generalizes `recovery-service-recovery.md` to all three personas per the 2026-05-21 incident's stated recurrence vector). Author: Recovery. Pending the operator ratification per the sop/remediation per-SOP ratification rule.

How to tell whether a persona's Telegram-bot daemon is actually alive, and bring it back when it isn't. Audience: whoever is recovering a persona — Recovery (the monitoring/recovery host), HO/Orchestrator via sudo, or the operator directly. Applies to **Orchestrator, Specialist, and Recovery** — all three run their personal daemon the same way (tmux session under a `systemctl --user` unit), so any of them can die the same silent way, and the two-daemons-per-host topology makes "is the persona up?" non-obvious from a process list.

## Substrate (verified 2026-06-06)

Every persona = one `claude --channels plugin:telegram@claude-plugins-official` process inside a named tmux session, owned by a per-account `systemctl --user` unit with a `Restart=always` supervisor script (the supervisor keeps the named tmux session alive and exits when it dies so systemd respawns it; crash-loop guard `StartLimitIntervalSec`/`StartLimitBurst`).

| Persona | Account (uid) | `systemctl --user` unit | tmux session | jsonl dir |
|---|---|---|---|---|
| Orchestrator (HO) | `claude` (1000) | `claude-telegram.service` | `claude-telegram-bot` | `/home/claude/.claude/projects/-home-claude` |
| Specialist | `claude-research` (1002) | `claude-telegram-research.service` | `claude-research-bot` | `/home/claude-research/.claude/projects/-home-claude-research` |
| Recovery | `claude-fallback` (1001) | `claude-telegram-fallback.service` | `claude-telegram-bot-fallback` | `/home/claude-fallback/.claude/projects/-home-claude-fallback` |

Reference supervisor impl: Recovery's `~/.local/bin/recovery-bot-supervise.sh` + unit; T-0509 mirrored the auto-restart shape to HO + Specialist. The auto-restart/health layer on top (T-0510 watchdog, `context_fill_monitor` api-health + wedge/staleness, `restart_drain`, `handoff_and_restart.sh`) reduces incidence but does not replace this runbook for the residual cases.

## Failure shape

Three distinct modes — **`systemctl is-active` does NOT distinguish them; `tmux capture-pane` is the source of truth.**

1. **Inert daemon** — tmux session + `claude` process exited; nothing running. Auto-restarts post-T-0509; persists only if the crash-loop guard tripped (unit shows `failed`).
2. **Alive-but-erroring** — process running, `is-active` = `active`, but every API request 400s (the 2026-05-21 Recovery trigger: `400 role 'system' is not supported on this model`, 162 failures over ~14h on a giant long-lived session). Bare auto-restart does NOT catch this (the process never exits); the `context_fill_monitor` api-health check now force-restarts it (Recovery since T-0510; Orchestrator/Specialist armed per T-0867's supervised cutover).
3. **Wedged TUI** — blocked at a modal/dialog (e.g. malformed `~/.claude/settings.json` → "Settings Error"; a "Resume from summary" usage modal; the dev-channels boot dialog). capture-pane shows the dialog; the loop is frozen behind it.

A fourth, comms-only mode worth ruling out: **bridge-dead-but-session-healthy** — the pane is at a clean idle/working prompt and the session is fine, but outbound Telegram never reaches the operator. Causes: the persona composed a reply in its transcript without calling the reply tool; or a fork-on-resume severed the live `--channels` binding (see recovery-service RCA cross-ref). Confirm with a round-trip, not just a healthy-looking pane.

## Diagnosis (always capture the pane first)

Substitute the persona's row from the table. `SESSION`, `UNIT`, `ACCT` below.

```bash
# Same-account (recovering your own / from the owning account):
tmux capture-pane -t <SESSION> -p | tail -40        # source of truth
systemctl --user status <UNIT>
journalctl --user -u <UNIT> -n 100 --no-pager

# Cross-account (Recovery recovering Orchestrator/Specialist, or the operator recovering any):
sudo -u <ACCT> tmux capture-pane -t <SESSION> -p | tail -40     # no XDG needed for tmux
sudo -u <ACCT> XDG_RUNTIME_DIR=/run/user/$(id -u <ACCT>) systemctl --user status <UNIT>
```

- Mode 2 (alive-but-400ing): pane shows repeated `API Error: 400 …`; confirm in `<jsonl dir>/*.jsonl` (grep `not supported on this model` / sustained 400s).
- Mode 4 (bridge dead): pane looks healthy → send a Telegram message and confirm a reply actually lands; check the persona's bridge process (`bot.pid` under `~/.claude/channels/telegram/`) is the live session's child.

## Recommended remediation (ranked)

1. **Restart the service (preferred — clean lifecycle; the cure for mode 2).**
   ```bash
   # same-account:
   systemctl --user restart <UNIT>
   # cross-account:
   sudo -u <ACCT> XDG_RUNTIME_DIR=/run/user/$(id -u <ACCT>) systemctl --user restart <UNIT>
   ```
   ⚠️ **`systemctl restart` can be a NO-OP for the inner `claude`.** The supervisor wrapper is idempotent — it only (re)creates the tmux session if missing, so a restart that finds the session alive cycles only the supervisor and leaves the SAME inner-claude PID. **Verify by inner-claude PID change** (`pgrep -af '.npm-global/bin/claude'`), NEVER by `is-active`/exit-zero alone. (Caught on Orchestrator 2026-05-25; see `reference_handoff_restart_systemctl_no_op`.)

2. **True restart when the inner claude must actually cycle (mode 2/3, or a no-op restart):**
   ```bash
   sudo -u <ACCT> tmux kill-session -t <SESSION>     # supervisor (Restart=always) respawns a FRESH session
   # then confirm a NEW inner-claude PID appears
   ```
   A fresh session resets accumulated session/request state (clears the mode-2 poison) and gives a fresh `--channels` bridge (clears mode-4 fork-severance) and dismisses a modal (mode 3).

3. **If the unit is `failed` (crash-loop guard tripped):**
   ```bash
   systemctl --user reset-failed <UNIT> && systemctl --user restart <UNIT>
   ```

4. **Mode 3 (wedged on malformed settings):** restore `~/.claude/settings.json` from a known-good backup (snapshot hooks / `~/.claude/backups/`), then restart. Only hand-edit config if the failure points there — every edit is a chance to make recovery harder.

### Cross-account gotcha

`sudo -u` strips `XDG_RUNTIME_DIR`, which breaks `systemctl --user` (but not `tmux`). Always restore it dynamically: `XDG_RUNTIME_DIR=/run/user/$(id -u <ACCT>)`. Recovery has NOPASSWD sudo to `claude` + `claude-research` (not the reverse); the operator/root can reach any.

## Verification after recovery

```bash
systemctl --user is-active <UNIT>                      # active
pgrep -af '.npm-global/bin/claude'                     # NEW inner-claude PID vs pre-restart
tmux capture-pane -t <SESSION> -p | tail -5            # Claude Code prompt frame, no API errors / modal
```
Then **round-trip the bridge**: send a Telegram message and confirm a reply lands (a healthy-looking idle pane can still have a dead bridge — mode 4). Canonical self-check: `bash ~/pantheon-config/tools/verify_startup.sh --agent <persona>` (exit 0 = healthy; 7 = pane wedged).

## Autonomous-vs-bubble-up (D3) placement

- **Autonomous** (Recovery / any recoverer applies directly — in-scope, reversible): restart / reset-failed / tmux-kill-session / restore-settings-from-backup, and the round-trip verification. Confirm health (is-active + capture-pane + PID change + round-trip) and state "confirmed healthy" rather than leaving it implied.
- **Bubble up to the operator:** crash-loop guard repeatedly exhausting after restarts (genuinely broken, not transient); any need to *edit* (not restore) config or change the model; a recurrence of the mode-2 400 outage (signals an unresolved upstream harness/model cause); a fresh session that boots silent and a re-kick doesn't recover it.

## Precedent cases

- **2026-05-21 Recovery** — mode 2 (alive-but-400ing), 162 failures over ~14h; cause = harness/model edge on a giant long-lived session, duration = missing self-recovery. (`incidents/2026-05-21-recovery-personal-daemon-silent-outage`.)
- **2026-05-25 Orchestrator** — `systemctl restart` no-op left the same inner-claude PID; true restart needed `tmux kill-session` (`reference_handoff_restart_systemctl_no_op`).
- **2026-06-06 Orchestrator** — mode 4 (bridge healthy, outbound replies composed in transcript without the reply tool → the operator received nothing); recovered by a send-keys nudge to re-send via the tool.

## Cross-references

- `recovery-service-recovery.md` — the original Recovery-scoped runbook this generalizes (kept for the 2026-05-21 RCA detail).
- `reference_handoff_restart_systemctl_no_op`, `feedback_confirm_health_after_touching_live_sessions` (Recovery memory) — the verify-by-PID + confirm-health discipline.
- Auto-restart/health layer: T-0509 (HO+Specialist auto-restart), T-0510 (api-health watchdog), T-0867 (api-health generalized to all personas), `tools/handoff_and_restart.sh`, `tools/context_fill_monitor.py`, `tools/restart_drain.py`.
- `tools/verify_startup.sh` (T-0728) — the self-verifying boot/health gate.
