#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# REFERENCE SNAPSHOT of recovery's LIVE supervise script, synced 2026-09-28T06:25Z (T-4068).
# The LIVE copy at /home/claude-fallback/.local/bin/recovery-bot-supervise.sh is AUTHORITATIVE.
# This tracked copy is for review, audit and diff only. Edits land live first, and
# model/effort changes land live-only (see project_model_policy_opus_standard).
# Re-sync when a live change is load-bearing (e.g. a boot-time re-assert such as
# T-2629/T-4092). No drift lint by design: these scripts retire with the
# harness-controller cutover (T-2953/T-3650).
# ---------------------------------------------------------------------------
# recovery-bot-supervise.sh — systemd-supervised wrapper for Recovery's Telegram bot.
#
# Why this exists (RCA 2026-05-21, T-0495-adjacent outage):
#   The old unit was Type=oneshot + RemainAfterExit=yes: it fired
#   `tmux new-session -d` and returned 0 immediately. systemd then believed
#   the service was "active (exited)" forever — even after the tmux session
#   (and the `claude` process inside it) died. Nothing restarted it. Result:
#   a ~14h dead-daemon outage.
#
# This wrapper keeps the EXACT same detached tmux session (so capture-pane /
# send-keys diagnosis and any restart machinery still work) but stays alive
# as systemd's MainPID for as long as the session lives. When the session
# dies, this script exits and `Restart=always` respawns it — auto-recovery.
#
# NOTE: this only catches the *process-exit / inert-daemon* failure mode.
# The "alive but every request 400s" mode (the actual 2026-05-21 trigger) is
# NOT caught here — that needs a health probe. See the recovery SOP.
#
# NO sg-wrap here (T-0783): this account operates on its OWN pantheon-config clone,
# which it owns, so no shared-group inheritance is needed. If you're standing up
# the ORCHESTRATOR (whose clone is the canonical one written by multiple
# pantheon-shared accounts), use the sg-wrapped claude-bot-supervise.sh instead —
# copying THIS script for the orchestrator would lose group inheritance and fail
# git writes to the canonical .git.
set -euo pipefail

SESSION="claude-telegram-bot-fallback"
CLAUDE_BIN="/home/claude-fallback/.npm-global/bin/claude"
# ⚠️ THESE TWO FLAGS WERE LIVE-ONLY UNTIL 2026-08-03 and the repo copy had NEITHER, so a
# redeploy from this file would have silently dropped both — the model pin falling back to
# settings.json, which still says `claude-fable-5[1m]`, a model that is gone fleet-wide.
# Backported here so repo == live. See the drift class in T-2444.
#
# --permission-mode default pins the pre-v2.1.200 launch default (CC v2.1.200 flipped
# unset to Manual; unattended relaunches stall at the first tool prompt). Fleet ruling
# 2026-07-06 (orchestrator, T-1541/T-1544).
# SUPERSEDED 2026-10-13 (T-4935, the operator TG 11753): the launch is now --permission-mode
# bypassPermissions (unix account = boundary); tools/bypass_consent.py pre-accepts the dialog.
#
# --model: THE LAUNCH FLAG IS THE AUTHORITATIVE PIN and outranks settings.json — see
# bugs/supervisor-launch-flag-outranks-settings-json-on-model-pin.md (settings.json already
# said opus[1m] and lost). 2026-08-03, the operator: claude-opus-5 -> 4-7 (TG 4958) -> 4-8 (TG 4960,
# "4.8 we might try for a bit instead"). Each id was verified to resolve with a real one-shot
# round-trip on a plugin-free seat before being pinned here. orchestrator stays on 4-7 for now — it
# was mid-work and the operator reported it "seems great on 4.7".
#
# NOTE this does NOT re-arm cap-autoswitch: model_stepper's expect_from reads
# context_fill_monitor's model_pin, still `claude-fable-5`, so the T-2134 mismatch — the thing
# that makes a cap event fail loud — is preserved. Do not "tidy" that pin.
#
# ⚠️ ONE NEW WRINKLE worth knowing before anyone reopens T-2136: claude-opus-4-8 IS
# model_stepper.OPUS_MODEL, i.e. the model a cap event would rewrite everything TO. So recovery now
# sits AT the step-down target. Still inert (dark + mismatched), but if the pin were ever
# "fixed", a cap event would be a no-op for recovery while stepping the other seats down — the
# ladder is no longer uniform across the fleet.
# 2026-09-28 the operator TG 10424: every seat except specialist to opus-5.5 (T-4067, orchestrator).
# T-4653 (the operator TG 10756, 2026-10-05): every seat on --effort medium to cut weekly-Max burn.
CLAUDE_ARGS="--channels plugin:telegram@claude-plugins-official --model claude-opus-5-5 --effort medium --permission-mode bypassPermissions"

# T-4935 (the operator TG 11753 via T-4939; ORG-063 s1): launch is --permission-mode bypassPermissions
# (full unlock; supersedes the `default` pin in the comments above). The bypass warning dialog
# ("No, exit" preselected) is skipped only when skipDangerousModePermissionPrompt is in USER
# settings (--settings/flagSettings does not count, verified in the 2.1.296 binary), so the
# consent is merged into settings.json here: idempotent, fail-open (tools/bypass_consent.py).
python3 -c 'import json,os,sys;d=os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude");p=os.path.join(d,"settings.json");s=json.load(open(p)) if os.path.exists(p) else {};sys.exit(0) if s.get("skipDangerousModePermissionPrompt") is True else None;os.makedirs(d,exist_ok=True);s["skipDangerousModePermissionPrompt"]=True;t=p+".tmp";json.dump(s,open(t,"w"),indent=2);os.replace(t,p)' >/dev/null 2>&1 || true

# Prewarm the telegram plugin's node_modules OFF the MCP critical path, AND pin
# an ABSOLUTE bun path in `.mcp.json` (T-2624/T-2625).
#
# CONFIRMED ROOT CAUSE of the control-plane comms-dark (2026-08-06, from the
# actual MCP-server log of the failed cutover boot, session 483db1a1):
#   "Connection failed after 13ms (ENOENT): Executable not found in $PATH: \"bun\""
# The control-plane launch (`systemd-run --user --scope`) runs with a PATH that
# does NOT include ~/.bun/bin, so Claude's spawn of the `.mcp.json` bare
# `command: "bun"` ENOENTs instantly -> the MCP server NEVER spawns -> comms dark.
# This is a deterministic PATH bug, NOT the ~5s session-init probe race that the
# earlier direct-spawn framing chased (bespoke boots connect in ~1.5s against a
# 60s timeout with bun on PATH — no 5s probe was ever observed). FIX: pin the
# absolute bun path in `command` so it resolves regardless of PATH under BOTH the
# bespoke tmux launch and the control-plane --scope launch. The direct-spawn form
# (no install, no `bun run start` wrapper) is retained as defence-in-depth; we
# keep node_modules warm HERE so the direct spawn never needs a runtime install.
# Idempotent (no-op when warm, ~0.01s) and fail-open (|| true never bricks a boot).
TG_PLUGIN_DIR=$(ls -d /home/claude-fallback/.claude/plugins/cache/claude-plugins-official/telegram/*/ 2>/dev/null | sort -V | tail -1)
if [ -n "${TG_PLUGIN_DIR:-}" ] && [ -f "${TG_PLUGIN_DIR}server.ts" ]; then
  # DURABLE PIN (T-2625): re-assert the DIRECT-SPAWN `.mcp.json` onto the live
  # cache before launch. The marketplace source `.mcp.json` is also matched to
  # this form, but a plugin-cache rebuild / version bump restores the marketplace
  # wrapper form (`bun install && start`) into a FRESH version dir, silently
  # reverting the T-2624 fix. This guard self-heals ANY such drift on the live
  # cache at every boot, regardless of source. `${CLAUDE_PLUGIN_ROOT}` is a
  # Claude-Code placeholder and MUST reach the file literally — the single-quoted
  # heredoc keeps bash from expanding it. Idempotent (writes only on mismatch;
  # $() strips the trailing newline both sides so a matched file never rewrites)
  # and fail-open (|| true never bricks a boot).
  DIRECT_MCP=$(cat <<'EOF'
{
  "mcpServers": {
    "telegram": {
      "command": "/home/claude-fallback/.bun/bin/bun",
      "args": ["run", "--cwd", "${CLAUDE_PLUGIN_ROOT}", "--silent", "server.ts"]
    }
  }
}
EOF
)
  if [ "$(cat "${TG_PLUGIN_DIR}.mcp.json" 2>/dev/null)" != "$DIRECT_MCP" ]; then
    printf '%s\n' "$DIRECT_MCP" > "${TG_PLUGIN_DIR}.mcp.json" || true
  fi
  # DURABLE 409-RESILIENCE PATCH (T-2629): re-assert the getUpdates never-give-up
  # retry patch onto the live server.ts copies. Same self-heal rationale as the
  # .mcp.json pin above — a plugin-cache rebuild restores the pristine upstream
  # server.ts (which gives up after 8 consecutive 409s and goes alive-but-deaf).
  # Marker-guarded, exact-literal, fail-open (|| true never bricks a boot).
  # T-4092 (2026-09-28): restored — lost from this live copy in an edit that
  # started from an older base; the tracked copy kept it.
  ~/pantheon-config/tools/telegram_poller_409_patch.sh >/dev/null 2>&1 || true
  ( cd "$TG_PLUGIN_DIR" && bun install --no-summary >/dev/null 2>&1 ) || true
fi

# (Re)create the session only if it isn't already up — makes the script
# idempotent across systemd restarts and tolerant of a manual tmux spawn.
# ORG-106 L3a (T-5526): never launch a deep-sleeping seat. WAIT rather than exit (Restart=always
# would churn the unit); fail-open on a reader fault. Tracked copy of the live script: re-sync it.
while :; do
  _lc_rc=0
  python3 "$HOME/pantheon-config/tools/seat_state.py" launch-check recovery || _lc_rc=$?
  [[ $_lc_rc -eq 3 ]] || break
  sleep 60
done

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux new-session -d -s "$SESSION" "$CLAUDE_BIN $CLAUDE_ARGS"
  ~/pantheon-config/tools/arm_pane_log.sh "$SESSION:0" recovery || true  # T-1636 pane time-log tap
  # T-2453: a BARE respawn (crash / manual or model-pin `systemctl restart` /
  # graceful self-EXIT that did NOT use self_restart.sh) otherwise comes up IDLE
  # and comms-dark until a human types — a fleet SPOF for the recovery fallback.
  # Deliver the first-turn kick in the BACKGROUND so it can never block MainPID,
  # and `|| true` so it can never fail the boot (fail-open == pre-T-2453 on any
  # error). It self-skips when a handoff_and_restart kicker is already in flight
  # (the graceful self_restart / child-watchdog path), and only ever sends into a
  # confirmed-idle prompt. Repo-script dependency is intentional and matches the
  # arm_pane_log tap above (both `|| true`); a repo-absent respawn just no-ops.
  ( ~/pantheon-config/tools/recovery_boot_kick.sh "$SESSION:0" recovery || true ) &
fi

# Block as systemd's MainPID until the session goes away. Polling (not a
# busy-wait) — 5s is responsive enough for a chat bridge and costs nothing.
while tmux has-session -t "$SESSION" 2>/dev/null; do
  sleep 5
done

# Session ended → exit so systemd's Restart=always brings us back.
exit 0
