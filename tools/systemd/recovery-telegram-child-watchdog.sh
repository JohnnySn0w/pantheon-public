#!/usr/bin/env bash
# recovery-telegram-child-watchdog.sh — detect + auto-recover Recovery's Telegram plugin
# bun child dying MID-SESSION (the "comms-dark drop" failure T-1456).
#
# WHY THIS EXISTS (RCA in tickets/active/T-1456.md):
#   The telegram plugin MCP server (`bun server.ts`, whose pid == bot.pid) is a
#   COMBINED process: it is both (a) the stdio MCP server Claude Code spawned as
#   a child AND (b) the grammy getUpdates poller. When it exits (an exit path in
#   server.ts, a fatal poll error, a token 409 overlap), BOTH directions die and
#   Claude does NOT re-spawn the stdio child within the session — comms go
#   PERMANENTLY dark until a full session self_restart. recovery-bot-supervise.sh
#   only watches the tmux SESSION (which stays alive here), so it cannot catch
#   this inner-child death.
#
#   Recovery is the only agent that exhibits it in practice: recovery runs 12h+ sessions
#   while orchestrator/specialist context-fill-restart every ~1.5-3h, so their bun child
#   rarely lives long enough to hit the transient that kills it. The actionable
#   root is the RESILIENCE GAP (no supervisor for the inner child), not a leak.
#
# WHAT IT DOES (fail-open, bounded auto-remediation):
#   Fired by a systemd-user timer. Each fire:
#     1. If the tmux session is DOWN -> not our failure mode (that's
#        recovery-bot-supervise's job); reset state, exit 0.
#     2. If the claude proc is younger than BOOT_GRACE -> still booting the
#        plugin; skip.
#     3. If the bun child is ALIVE (bot.pid valid w/ server.ts cmdline, OR the
#        plugin wrapper proc present) -> healthy; clear counters, exit 0.
#     4. If the child is ABSENT: require DEBOUNCE consecutive absent fires (rides
#        out a transient boot/restart window), then fire self_restart.sh — but
#        gated by COOLDOWN (one restart per effectiveness window) and a per-WINDOW
#        CAP. Past the cap it STOPS restarting and alerts the operator directly (a
#        pathological loop is worth human eyes, not thrashing the session).
#   Every detection + action is appended to LOG_FILE so the next death is
#   diagnosable (the observability half of T-1456: WHEN + surrounding state).
#
#   Recovery is a full session self_restart (not an out-of-band child re-launch):
#   an externally-spawned server.ts would NOT reconnect Claude's in-session MCP
#   stdio transport, so it cannot restore the reply tool. self_restart is the
#   only reliable path, and it is the sanctioned self-heal mechanism.
#
# DELIBERATELY does NOT edit the cached plugin (clobbered on plugin update) —
# this is a recovery-side supervisor only.
#
# Flags: --dry-run (log the decision, take no action) ; --simulate-absent
# (force the absent branch for testing WITHOUT killing the live child — pair
# with --dry-run). Fail-open by construction: any unexpected error exits 0.
set -uo pipefail

# --- Config -----------------------------------------------------------------
SESSION="claude-telegram-bot-fallback"
PLUGIN_CWD_PAT="/home/claude-fallback/.claude/plugins/cache/claude-plugins-official/telegram"
BOT_PID_FILE="${HOME}/.claude/channels/telegram/bot.pid"
ENV_FILE="${HOME}/.claude/channels/telegram/.env"
STATE_FILE="${HOME}/.claude/recovery-telegram-watchdog.state"
LOG_FILE="${HOME}/.claude/logs/recovery-telegram-watchdog.log"
# Absolute repo path — this script is INSTALLED to ~/.local/bin (a different dir
# than the repo), so a BASH_SOURCE-relative lookup would miss the restart wrapper.
# T-4159: fire recovery_self_restart.sh, which RESOLVES the restart target from
# instance.yaml (agent_role_map.py restart-target recovery), instead of hardcoding
# claude-telegram-fallback.service. After T-3026's move that bespoke unit is
# retired, and restarting it would resurrect a second supervisor next to the
# runtime controller; the wrapper cycles the control-plane session instead.
SELF_RESTART="/home/claude-fallback/pantheon-config/tools/recovery_self_restart.sh"
CHAT_ID="424242424"                              # the operator

DEBOUNCE=2          # consecutive absent fires before acting (rides out boot window)
COOLDOWN=600        # sec between restart fires (== effectiveness window)
CAP=3               # max restarts per WINDOW before escalate-and-stop
WINDOW=3600         # rolling window for the cap (sec)
BOOT_GRACE=150      # sec; skip while the claude proc is this young (plugin booting)

DRY_RUN=0
SIMULATE_ABSENT=0
for a in "$@"; do
  case "$a" in
    --dry-run)         DRY_RUN=1 ;;
    --simulate-absent) SIMULATE_ABSENT=1 ;;
    -h|--help) sed -n '2,60p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a" >&2; exit 64 ;;
  esac
done

NOW="$(date +%s)"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

log() { echo "$TS $*" >>"$LOG_FILE" 2>/dev/null || true; }

# --- State I/O (simple key=val file; missing keys default empty) ------------
declare -A ST
load_state() {
  ST[consec]=0; ST[last_restart]=0; ST[restarts]=""; ST[escalated_at]=0
  [[ -r "$STATE_FILE" ]] || return 0
  local k v
  while IFS='=' read -r k v; do [[ -n "$k" ]] && ST[$k]="$v"; done <"$STATE_FILE"
}
save_state() {
  { for k in consec last_restart restarts escalated_at; do echo "$k=${ST[$k]}"; done; } \
    >"$STATE_FILE.tmp" 2>/dev/null && mv "$STATE_FILE.tmp" "$STATE_FILE" 2>/dev/null || true
}
load_state

# --- The operator alert (independent of Claude OAuth — uses the bot token direct) -
alert_operator() {
  local msg="$1" token
  token="$(grep -m1 -E '^(TELEGRAM_)?BOT_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)"
  [[ -z "$token" ]] && { log "ESCALATE: no bot token to alert the operator — $msg"; return; }
  timeout 8 curl -s "https://api.telegram.org/bot${token}/sendMessage" \
    --data-urlencode "chat_id=${CHAT_ID}" \
    --data-urlencode "text=${msg}" >/dev/null 2>&1 || log "ESCALATE: alert send failed"
}

# --- 1. Session-alive gate --------------------------------------------------
if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  # Session itself is down -> recovery-bot-supervise's domain, not ours. Reset.
  ST[consec]=0; save_state
  exit 0
fi

# --- 1b. Harness gate (T-4184): not ours when the session runs CODEX ----------
# After T-3026's move the runtime controller can launch CODEX in this session
# (claude capped -> codex fallback). A codex runtime has no claude plugin child
# to watch, so an ABSENT verdict there is meaningless, and self_restart would kill
# the live codex runtime (up to CAP/h, then page the operator). Positive detection of
# a codex process in the pane, NOT "no claude process found": the latter would
# silently disable this watchdog for claude too if claude's argv ever drifts.
pane_harness_is_codex() {
  local pp pid
  pp="$(tmux list-panes -t "$SESSION" -F '#{pane_pid}' 2>/dev/null | head -1)"
  [[ "$pp" =~ ^[0-9]+$ ]] || return 1
  for pid in "$pp" $(pgrep -P "$pp" 2>/dev/null); do
    tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -qE '(^|/)codex(\.js)?( |$)' && return 0
  done
  return 1
}
if pane_harness_is_codex; then
  [[ "${ST[consec]}" -ne 0 ]] && log "HARNESS=codex: controller codex runtime, not a claude session; clearing absent streak (consec was ${ST[consec]})"
  ST[consec]=0; save_state
  exit 0
fi

# --- 2. Boot-grace gate: don't fight a session still spawning the plugin -----
# pgrep -f matches the `tmux new-session ... claude --channels` launcher too;
# take the first match whose comm is NOT tmux (the real claude proc).
CLAUDE_PID=""
for _p in $(pgrep -u "$(id -u)" -f 'npm-global/bin/claude --channels' 2>/dev/null); do
  [[ "$(ps -o comm= -p "$_p" 2>/dev/null)" == tmux* ]] && continue
  CLAUDE_PID="$_p"; break
done
if [[ -n "$CLAUDE_PID" && -r "/proc/$CLAUDE_PID" ]]; then
  c_start="$(ps -o etimes= -p "$CLAUDE_PID" 2>/dev/null | tr -d ' ')"
  if [[ -n "$c_start" && "$c_start" -lt "$BOOT_GRACE" ]]; then
    ST[consec]=0; save_state
    exit 0
  fi
fi

# --- 3. Child-alive check (bias toward ALIVE to avoid false restarts) --------
child_alive() {
  [[ "$SIMULATE_ABSENT" == "1" ]] && return 1
  # (a) authoritative: bot.pid points at a live `server.ts` owned by us
  if [[ -r "$BOT_PID_FILE" ]]; then
    local bp; bp="$(cat "$BOT_PID_FILE" 2>/dev/null)"
    if [[ "$bp" =~ ^[0-9]+$ ]] && [[ -r "/proc/$bp/cmdline" ]] \
       && tr '\0' ' ' <"/proc/$bp/cmdline" 2>/dev/null | grep -q 'server\.ts'; then
      return 0
    fi
  fi
  # (b) fallback: the plugin wrapper proc for OUR cwd is present
  pgrep -u "$(id -u)" -f "bun run --cwd ${PLUGIN_CWD_PAT}" >/dev/null 2>&1 && return 0
  return 1
}

if child_alive; then
  if [[ "${ST[consec]}" -ne 0 ]]; then
    log "RECOVERED: bun child present again (consec was ${ST[consec]})"
  fi
  ST[consec]=0; save_state
  exit 0
fi

# --- 4. ABSENT: bounded remediation ----------------------------------------
BOT_PID_VAL="$(cat "$BOT_PID_FILE" 2>/dev/null || echo '<none>')"
ST[consec]=$(( ST[consec] + 1 ))
log "ABSENT: bun child gone (session up, claude_pid=${CLAUDE_PID:-?}, bot.pid=${BOT_PID_VAL}) consec=${ST[consec]}${DRY_RUN:+ [dry-run]}"

if [[ "${ST[consec]}" -lt "$DEBOUNCE" ]]; then
  save_state
  exit 0   # await confirmation next fire (debounce)
fi

# Effectiveness window: a recent restart may still be bringing the child back.
if (( NOW - ST[last_restart] < COOLDOWN )); then
  log "HOLD: within cooldown (${COOLDOWN}s) of last restart @${ST[last_restart]}; awaiting recovery"
  save_state
  exit 0
fi

# Prune the restart timestamps to the rolling window and enforce the cap.
pruned=""; count=0
for t in ${ST[restarts]}; do
  if [[ "$t" =~ ^[0-9]+$ ]] && (( NOW - t < WINDOW )); then
    pruned="$pruned $t"; count=$(( count + 1 ))
  fi
done
ST[restarts]="${pruned# }"

if (( count >= CAP )); then
  # Pathological loop — stop auto-restarting, surface to the operator once per window.
  if (( NOW - ST[escalated_at] >= WINDOW )); then
    ST[escalated_at]="$NOW"
    alert_operator "⚠️ Recovery telegram-child watchdog: bun child died ${count}x in the last hour and self_restart is not holding it. Auto-restart is now PAUSED (cap ${CAP}/${WINDOW}s reached). Recovery comms may be dark — needs a look. (T-1456 watchdog)"
    log "ESCALATE: cap ${CAP}/${WINDOW}s reached (count=${count}); paused auto-restart, alerted the operator"
  else
    log "ESCALATE: cap reached (count=${count}); still paused (already alerted @${ST[escalated_at]})"
  fi
  save_state
  exit 0
fi

# Fire the sanctioned self-restart (detached; survives the session restart).
if [[ "$DRY_RUN" == "1" ]]; then
  log "WOULD FIRE self_restart (dry-run): $SELF_RESTART (target resolved from instance.yaml)"
  save_state
  exit 0
fi

if [[ ! -x "$SELF_RESTART" ]]; then
  log "ERROR: self_restart not executable at $SELF_RESTART — cannot remediate"
  save_state
  exit 0
fi

log "FIRING self_restart (count_in_window was ${count}, consec=${ST[consec]})"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if "$SELF_RESTART" >>"$LOG_FILE" 2>&1; then
  ST[last_restart]="$NOW"
  ST[restarts]="${ST[restarts]:+${ST[restarts]} }$NOW"
  ST[consec]=0
  log "FIRED self_restart OK; recovery in flight"
else
  log "ERROR: self_restart invocation returned non-zero"
fi
save_state
exit 0
