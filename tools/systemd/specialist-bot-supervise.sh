#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# REFERENCE SNAPSHOT of specialist's LIVE supervise script, synced 2026-09-28T06:25Z (T-4068).
# The LIVE copy at /home/claude-research/.local/bin/specialist-bot-supervise.sh is AUTHORITATIVE.
# This tracked copy is for review, audit and diff only. Edits land live first, and
# model/effort changes land live-only (see project_model_policy_opus_standard).
# Re-sync when a live change is load-bearing (e.g. a boot-time re-assert such as
# T-2629/T-4092). No drift lint by design: these scripts retire with the
# harness-controller cutover (T-2953/T-3650).
# ---------------------------------------------------------------------------
# specialist-bot-supervise.sh — systemd-supervised wrapper for Specialist's Claude Code
# session (claude-research account, @c_specialist_bot Telegram bidirectional).
#
# Why this exists (restart-capability #1, per sop/standard/adding-restart-
# capability-to-a-new-agent.md; T-0517 parity / T-0755):
#   Specialist's old unit was Type=oneshot + RemainAfterExit=yes: it fired
#   `tmux new-session -d` and returned 0 immediately, so systemd reported the
#   service "active (exited)" forever — even after the tmux session (and the
#   `claude` inside it) died. Nothing restarted it (the same dead-daemon mode
#   that bit Recovery 2026-05-21). This wrapper keeps the EXACT same detached tmux
#   session (so capture-pane / send-keys diagnosis + restart machinery still
#   work) but stays alive as systemd's MainPID for as long as the session
#   lives. When it dies, this script exits and `Restart=always` respawns it.
#
# Adapted from the canonical tools/systemd/recovery-bot-supervise.sh (T-0526).
# CLAUDE_ARGS MUST carry --channels (P1/T-0754 made @c_specialist_bot
# bidirectional; this is the launch path that keeps it on). The plugin must
# also stay enabled in settings.json (enabledPlugins telegram=true).
#
# NOTE: catches only the process-exit / inert-daemon mode. The "alive but
# 400ing" mode is handled by the api-health path (context_fill_monitor +
# drain), not here.
set -euo pipefail

SESSION="claude-research-bot"
CLAUDE_BIN="/home/claude-research/.npm-global/bin/claude"
# --permission-mode default pins the pre-v2.1.200 launch default (CC v2.1.200 flipped
# unset to Manual; unattended relaunches stall at the first tool prompt). Fleet ruling
# 2026-07-06 (orchestrator, T-1541/T-1544); applied by recovery as the cross-check tier.
# SUPERSEDED 2026-10-13 (T-4935, the operator TG 11753): the launch is now --permission-mode
# bypassPermissions (unix account = boundary); tools/bypass_consent.py pre-accepts the dialog.
# T-4068 2026-09-28 (orchestrator): this fallback-path pin is aligned to the operator TG 3304 (specialist = opus-4-8 xhigh).
# specialist is controller-launched, so instance.yaml is the EFFECTIVE source; this line only matters
# if the bespoke service is ever started, and then it must not contradict policy.
# T-4526 2026-10-04 (recovery): re-aligned to the operator TG 7988 (specialist = opus-5-5 xhigh).
# T-4653 (the operator TG 10756, 2026-10-05): every seat on --effort medium to cut weekly-Max burn.
CLAUDE_ARGS="--channels plugin:telegram@claude-plugins-official --model claude-opus-5-5 --effort medium --permission-mode bypassPermissions"

# T-4935 (the operator TG 11753 via T-4939; ORG-063 s1): launch is --permission-mode bypassPermissions
# (full unlock; supersedes the `default` pin in the comments above). The bypass warning dialog
# ("No, exit" preselected) is skipped only when skipDangerousModePermissionPrompt is in USER
# settings (--settings/flagSettings does not count, verified in the 2.1.296 binary), so the
# consent is merged into settings.json here: idempotent, fail-open (tools/bypass_consent.py).
python3 -c 'import json,os,sys;d=os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude");p=os.path.join(d,"settings.json");s=json.load(open(p)) if os.path.exists(p) else {};sys.exit(0) if s.get("skipDangerousModePermissionPrompt") is True else None;os.makedirs(d,exist_ok=True);s["skipDangerousModePermissionPrompt"]=True;t=p+".tmp";json.dump(s,open(t,"w"),indent=2);os.replace(t,p)' >/dev/null 2>&1 || true

# DURABLE ABSOLUTE-BUN PIN re-assert (T-2639 / mirrors recovery T-2625): a plugin-cache
# rebuild restores the pristine upstream .mcp.json with a bare `command: "bun"`,
# which ENOENTs under any launch whose PATH lacks ~/.bun/bin (the T-2624 comms-dark
# that hit specialist during the 2026-08-07 --scope cutover, T-2633). Re-assert the
# ABSOLUTE bun path onto BOTH live plugin copies before launch. ${CLAUDE_PLUGIN_ROOT}
# is a Claude-Code placeholder and MUST reach the file literally — the single-quoted
# heredoc keeps bash from expanding it. Idempotent (writes only on mismatch; $()
# strips the trailing newline both sides so a matched file never rewrites) and
# fail-open (|| true never bricks a boot). SUPERSEDED by the installer-level bun-path
# fix (the operator 7186, HO-owned) once it lands fleet-wide — retire this block then.
SPECIALIST_MCP=$(cat <<'MCPEOF'
{
  "mcpServers": {
    "telegram": {
      "command": "/home/claude-research/.bun/bin/bun",
      "args": ["run", "--cwd", "${CLAUDE_PLUGIN_ROOT}", "--shell=bun", "--silent", "start"]
    }
  }
}
MCPEOF
)
for MCP_JSON in \
  /home/claude-research/.claude/plugins/cache/claude-plugins-official/telegram/*/.mcp.json \
  /home/claude-research/.claude/plugins/marketplaces/claude-plugins-official/external_plugins/telegram/.mcp.json ; do
  [ -f "$MCP_JSON" ] || continue
  if [ "$(cat "$MCP_JSON" 2>/dev/null)" != "$SPECIALIST_MCP" ]; then
    printf '%s\n' "$SPECIALIST_MCP" > "$MCP_JSON" || true
  fi
done


# (Re)create the session only if it isn't already up — idempotent across
# systemd restarts and tolerant of a manual tmux spawn.
# ORG-106 L3a (T-5526): never launch a deep-sleeping seat. WAIT rather than exit (Restart=always
# would churn the unit); fail-open on a reader fault. Tracked copy of the live script: re-sync it.
while :; do
  _lc_rc=0
  python3 "$HOME/pantheon-config/tools/seat_state.py" launch-check specialist || _lc_rc=$?
  [[ $_lc_rc -eq 3 ]] || break
  sleep 60
done

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux new-session -d -s "$SESSION" "$CLAUDE_BIN $CLAUDE_ARGS"
  ~/pantheon-config/tools/arm_pane_log.sh "$SESSION:0" specialist || true  # T-1636 pane time-log tap
fi

# Block as systemd's MainPID until the session goes away. Polling (not a
# busy-wait) — 5s is responsive enough and costs nothing.
while tmux has-session -t "$SESSION" 2>/dev/null; do
  sleep 5
done

# Session ended → exit so systemd's Restart=always brings us back.
exit 0
