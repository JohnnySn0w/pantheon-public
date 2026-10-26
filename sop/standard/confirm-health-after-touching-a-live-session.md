---
title: Confirm health after touching a live session
status: Adopted 2026-10-05 by Recovery (recovery) per T-4568, promoting Recovery's private memory pin `feedback_confirm_health_after_touching_live_sessions` (2026-05-22) under ORG-012 §6. Six SOPs plus bugs/ and knowledge/ cited the pin as the authority for a verification step, but no other seat could read it (the T-0872 class). Below-goal-level methodology; adopt-and-proceed (msg-3733 principles-as-ratification).
---

# Confirm health after touching a live session

After you run anything against ANOTHER seat's live session, it is on you to show the seat is still healthy, not to leave it implied. That includes restart tooling, a `--dry-run`, a cross-account `sudo -u` command, a pane read, or anything near its Telegram bridge. End the report with the check and its result.

**Why.** 2026-05-22: Recovery dry-ran `self_restart.sh` against orchestrator's and specialist's substrates. The dry-run had no side effects (read-only preflights, then it printed the systemd-run command and exited). But the report did not say the bots were confirmed alive afterwards, and the operator's first reaction was "U realize… dry running those would break their telegram sessions?" His suspicion of lifecycle tooling near live sessions is the right default. The gap was leaving "they're fine" unsaid. Both seats were checked and were healthy.

## When to use this

- You ran a command as, or against, another persona's account or session. That covers restart and relogin tooling (any mode), unit operations, send-keys or kicks, clone or config edits, and runtime-record repair.
- The seat carries a live channel (a Telegram bridge, the codex poller), where a mistake leaves the operator's path dark without any alarm.
- Not needed for a pure read of files on disk that never touches the process (a `git log` in its clone, reading its credential expiry fields).

## Procedure

1. **Know which side of the line the action was on.**
   - Non-disruptive to a running process: `--dry-run` modes, `tmux has-session`, `tmux capture-pane`, `systemctl --user is-active`/`status`, and `git pull --ff-only` into its clone. A running process never reloads code from a pull; the new code waits for the next start.
   - Disruptive: a non-dry restart, `tmux send-keys`, `kill-session`, `systemctl restart`/`stop`, and **ever** starting a second `claude` on an account with the Telegram plugin, which steals the bot poller (`sop/remediation/agent-oauth-relogin.md` "Venue selection").
2. **Run the check, as the seat's account, after the action:**
   ```bash
   bash ~/pantheon-config/tools/verify_startup.sh --agent <persona> --no-dewedge
   ```
   Exit `0` means healthy, and `7` means the pane is wedged. `--no-dewedge` keeps it a pure check. Without it, the tool may dismiss a boot dialog on the other seat, which is itself a send-keys. If the tool can't run there, check by hand:
   - the session-owning unit is active (`systemctl --user is-active pantheon-runtime-controller@<persona>` on a control-plane seat);
   - a `tmux capture-pane -p -t <pane>` tail shows the prompt frame `(╭─+╮|─{20,})` and an idle composer, or a turn in progress. A modal footer ("Enter to confirm · Esc to cancel") means it is not healthy.
   - For a comms seat, the Telegram child is alive: `~/.claude/channels/telegram/bot.pid` points at a live `server.ts`.
3. **State it in the report:** "<persona> confirmed healthy: verify_startup rc 0" (or the unit and pane evidence). If the action was disruptive, also show the successor came back and is doing work, e.g. a fresh pane plus an answered message or a mail wake. A unit that reads `active` alone does not show that.
4. **If it is not healthy,** say so plainly with the evidence and go to the matching remediation SOP. Do not report the action as done.

## Common failure modes

- **Implied safety.** "It was only a dry-run" is a claim about the tool, not about the seat. Show the seat.
- **Checking the wrong principal.** Run the check as the seat's own account. A capture from your own account can fail on permissions and read as "down" (a false alarm), or read a different tmux server.
- **Running the checker in a mode that acts.** `verify_startup.sh` without `--no-dewedge` on another seat can send a keystroke.
- **Reading one surface.** An active unit with a dead Telegram child is a dark seat. For a comms seat, check the channel too.

## Boundary

- This SOP is the verification step only. What you may do across accounts, and as whom, is `cross-account-ops-discipline.md`. How to send text into a pane safely is `cross-agent-dispatch-discipline.md`.
- Your OWN seat: run `verify_startup.sh` with no `--agent` (it auto-detects). It too can dismiss a known boot dialog in your pane, so add `--no-dewedge` for a pure check.

## Cross-references

- Origin: Recovery memory pin `feedback_confirm_health_after_touching_live_sessions` (2026-05-22), now a pointer to this SOP. T-4568 (ORG-012 §6 sweep item S10, specialist).
- Cited from: `sop/remediation/agent-spend-limit-wedge.md`, `persona-daemon-liveness-recovery.md`, `telegram-plugin-mcp-drop-recovery.md`, `acp-bridge-recovery.md`, `agent-oauth-relogin.md`, `sop/standard/agent-autonomy-act-vs-park.md`, `knowledge/infra-abstractions/reactive.md`, and `bugs/heavy-build-on-live-ci-runner-container-starves-it-offline-while-busy.md`.
- `tools/verify_startup.sh` (T-0728): the health check.
