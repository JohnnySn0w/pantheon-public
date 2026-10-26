#!/usr/bin/env bash
# tools/pantheon_oom_shield.sh — pin agent SESSION processes' oom_score_adj (T-1927).
#
# WHY: agent sessions are the largest processes in their slices AND (verified
# 2026-07-22) run at oom_score_adj=+200 — actively preferred by the kernel OOM
# killer. The 2026-07-21 spike killed recovery's live session for exactly this
# reason. systemd-oomd is INACTIVE on this box, and user-mode systemd cannot
# set a negative adjust, so a ROOT-side actor is required — this script.
#
# WHAT: for each (unix user, process pattern) below, set oom_score_adj to
# $SHIELD_ADJ on matching pids. -850 per the T-1927 HO ruling: strong-but-not-
# immune (-1000 would make sessions unkillable and risk hanging the box under
# extreme pressure; better to lose one session than the fleet). Children
# inherit adj at fork, so a shielded session's future subprocesses are covered;
# a freshly restarted session is re-shielded on the next timer fire (≤2 min
# window at default cadence).
#
# Deliberately NOT shielded: oneshot monitors (tiny, never picked — no-op
# churn), bun telegram bridges + kino + CI runners (Restart=always, recoverable
# — the ACCEPTED second-order victims), journald (has its own protection).
#
# INSTALL (root; Recovery cannot — kino-scoped sudo only):
#   cp tools/pantheon_oom_shield.sh /usr/local/sbin/pantheon_oom_shield.sh && chmod 755 /usr/local/sbin/pantheon_oom_shield.sh
#   cp tools/systemd/system/pantheon-oom-shield.{service,timer} /etc/systemd/system/
#   systemctl daemon-reload && systemctl enable --now pantheon-oom-shield.timer
#
# Usage: pantheon_oom_shield.sh [--dry-run]
set -euo pipefail

SHIELD_ADJ="${PANTHEON_OOM_SHIELD_ADJ:--850}"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

# account:pgrep-pattern — the session-DRIVING processes (inner agent binary +
# its tmux server, whose death also kills the session). Patterns are -f
# (full cmdline) and MUST NOT match the oneshot monitors (python3 tools/*).
TARGETS=(
  "claude:npm-global/bin/claude"
  "claude-research:npm-global/bin/claude"
  "claude-fallback:npm-global/bin/claude"
  "exocortex:npm-global/bin/claude"
  "suborch1:npm-global/bin/claude"
  "suborch2:npm-global/bin/claude"
  # Control-plane seats run a pinned binary at /opt/pantheon/claude/<acct>/current/
  # claude.exe (T-5222: recovery sat at +200 and was the kernel's FIRST kill on
  # 2026-10-19 because only the npm-global path was listed). The tmux server's
  # cmdline carries the same path, so one entry covers both.
  "claude:/opt/pantheon/claude/claude/current/claude.exe"
  "claude-fallback:/opt/pantheon/claude/claude-fallback/current/claude.exe"
  "codex:vendor/x86_64-unknown-linux-musl/bin/codex"
  # tmux SERVERS are session-critical too, but need no separate entries: a tmux
  # server's cmdline retains its launch command, so the claude-path pattern
  # already matches the claude-family servers. codex's server launches via
  # /usr/bin/codex (not the vendor path) — match it explicitly:
  "codex:tmux new-session -d -s codex-session"
)

changed=0 checked=0
for entry in "${TARGETS[@]}"; do
  user="${entry%%:*}"; pattern="${entry#*:}"
  for pid in $(pgrep -u "$user" -f "$pattern" 2>/dev/null || true); do
    adj_file="/proc/$pid/oom_score_adj"
    [ -r "$adj_file" ] || continue
    cur=$(cat "$adj_file" 2>/dev/null) || continue
    checked=$((checked + 1))
    if [ "$cur" != "$SHIELD_ADJ" ]; then
      if [ "$DRY_RUN" = 1 ]; then
        echo "[oom_shield] DRY: would set $user pid=$pid ($pattern) $cur -> $SHIELD_ADJ"
      else
        if echo "$SHIELD_ADJ" > "$adj_file" 2>/dev/null; then
          echo "[oom_shield] set $user pid=$pid ($pattern) $cur -> $SHIELD_ADJ"
        else
          echo "[oom_shield] FAILED writing $adj_file (need root)" >&2
        fi
      fi
      changed=$((changed + 1))
    fi
  done
done
echo "[oom_shield] checked=$checked adjusted=$changed adj=$SHIELD_ADJ dry_run=$DRY_RUN"
