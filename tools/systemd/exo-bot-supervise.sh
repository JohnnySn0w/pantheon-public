#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# REFERENCE SNAPSHOT of archivist's LIVE supervise script, synced 2026-09-28T06:25Z (T-4068).
# The LIVE copy at /home/exocortex/.local/bin/exo-bot-supervise.sh is AUTHORITATIVE.
# This tracked copy is for review, audit and diff only. Edits land live first, and
# model/effort changes land live-only (see project_model_policy_opus_standard).
# Re-sync when a live change is load-bearing (e.g. a boot-time re-assert such as
# T-2629/T-4092). No drift lint by design: these scripts retire with the
# harness-controller cutover (T-2953/T-3650).
# ---------------------------------------------------------------------------
# exo-bot-supervise.sh — systemd-supervised wrapper for the second head's Telegram
# daemon. Same shape as recovery-bot-supervise.sh (see its header for the RCA that
# motivated the supervise-not-oneshot design): keeps a detached tmux session
# running `claude --channels`, blocks as MainPID while the session lives, exits
# when it dies so Restart=always respawns it.
set -euo pipefail

SESSION="claude-telegram-bot-exocortex"
CLAUDE_BIN="/home/exocortex/.npm-global/bin/claude"
# --permission-mode default per fleet ruling 2026-07-06 (T-1541/T-1544).
# SUPERSEDED 2026-10-13 (T-4935, the operator TG 11753): the launch is now --permission-mode
# bypassPermissions (unix account = boundary); tools/bypass_consent.py pre-accepts the dialog.
# 2026-09-28 the operator TG 10424: every seat except specialist to opus-5.5 (T-4067, orchestrator).
# T-4653 (the operator TG 10756, 2026-10-05): every seat on --effort medium to cut weekly-Max burn.
CLAUDE_ARGS="--channels plugin:telegram@claude-plugins-official --model claude-opus-5-5 --effort medium --permission-mode bypassPermissions"

# T-4935 (the operator TG 11753 via T-4939; ORG-063 s1): launch is --permission-mode bypassPermissions
# (full unlock; supersedes the `default` pin in the comments above). The bypass warning dialog
# ("No, exit" preselected) is skipped only when skipDangerousModePermissionPrompt is in USER
# settings (--settings/flagSettings does not count, verified in the 2.1.296 binary), so the
# consent is merged into settings.json here: idempotent, fail-open (tools/bypass_consent.py).
python3 -c 'import json,os,sys;d=os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude");p=os.path.join(d,"settings.json");s=json.load(open(p)) if os.path.exists(p) else {};sys.exit(0) if s.get("skipDangerousModePermissionPrompt") is True else None;os.makedirs(d,exist_ok=True);s["skipDangerousModePermissionPrompt"]=True;t=p+".tmp";json.dump(s,open(t,"w"),indent=2);os.replace(t,p)' >/dev/null 2>&1 || true

# T-2629/T-4092 (recovery): re-assert the telegram poller never-give-up-on-409 patch on every boot,
# since a plugin-cache rebuild restores upstream's give-up-after-8. Fail-open: never bricks a boot.
/home/exocortex/pantheon-config/tools/telegram_poller_409_patch.sh >/dev/null 2>&1 || true

# ORG-106 L3a (T-5526): never launch a deep-sleeping seat. WAIT rather than exit (Restart=always
# would churn the unit); fail-open on a reader fault. Tracked copy of the live script: re-sync it.
while :; do
  _lc_rc=0
  python3 "$HOME/pantheon-config/tools/seat_state.py" launch-check archivist || _lc_rc=$?
  [[ $_lc_rc -eq 3 ]] || break
  sleep 60
done

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  # T-5016: archive the prior session to the Chronicle before a fresh start
  # (covers crash/reboot ends; bounded, fail-open, exactly-once).
  timeout 90 python3 /home/exocortex/pantheon-config/tools/session_chronicle.py session-end --persona archivist --cause start-reconcile --stage --certain --handoff-file /home/exocortex/HANDOFF.md >/dev/null 2>&1 || true
  tmux new-session -d -s "$SESSION" -c /home/exocortex "$CLAUDE_BIN $CLAUDE_ARGS"
  /home/exocortex/pantheon-config/tools/arm_pane_log.sh "$SESSION:0" archivist || true  # T-5016 pane tap
fi

while tmux has-session -t "$SESSION" 2>/dev/null; do
  sleep 5
done
exit 0
