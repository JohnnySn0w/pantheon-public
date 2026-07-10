#!/usr/bin/env bash
# verify_startup.sh — canonical "did this agent session boot healthy?" check + boot-dialog de-wedge.
#
# T-0728. Design reviewer-vetted (Plan agent, 2026-06-02). Replaces drift-prone
# prose in pinned-rules/recovery.md with a self-verifying script the doc points at.
#
# Composes existing production-tested logic into one fast, FAIL-SAFE, idempotent
# self-check a fresh session can run (and, in a later ticket, the SessionStart
# hook). Two jobs: (1) verify the session booted healthy; (2) de-wedge — if the
# pane is stuck on the dev-channels startup confirm, dismiss it (fail-closed:
# only that exact dialog, never a destructive/arbitrary one).
#
# BLAST-RADIUS POSTURE (this may eventually run on every fresh boot):
#   - The ONLY hard-fail (exit 7) conditions are "pane captured cleanly but is
#     not at an idle prompt" and "a non-dismissable modal persists". A check
#     that cannot run (capture failed, network blip, token file race) WARNS and
#     still exits 0 — it must never brick a booting session.
#   - The de-wedge reuses handoff_and_restart.sh's EXACT T-0721 sentinel +
#     denylist (kept in sync; test_verify_startup.sh pins them byte-equal).
#
# Exit codes:  0 = healthy (boot dialog, if present, was dismissed and the pane
#                  then went idle; SOFT warnings may have printed). --at-boot
#                  ALWAYS exits 0 (pane check skipped; SOFT guards only).
#              7 = pane wedged — captured but not idle, OR a non-allowlisted /
#                  denylisted modal persists (fail-closed; did NOT press keys)
#             64 = usage error (unknown --agent / bad flag)
#
# Usage: verify_startup.sh [--agent recovery|orchestrator|specialist] [--self] [--no-dewedge]
#                          [--at-boot] [--pane <target>] [--timeout <sec>] [--json] [-h]
#   --at-boot: SessionStart-hook mode (T-1099) — self-call that skips the pane
#              check (pane unsettled mid-boot) + de-wedge (no-self-inject), runs
#              only the SOFT C4/C6/C7/C8 guards, and always exits 0.

set -uo pipefail

# --- shared pane sentinels: MUST stay byte-equal with handoff_and_restart.sh
# (VERIFY_PATTERN/IDLE_SENTINEL/DIALOG_EXCLUDE :57-59, BOOT_DIALOG_SENTINEL :66,
# BOOT_DISMISS_DENYLIST :72). test_verify_startup.sh asserts equality to catch
# drift. (Follow-up: extract to a shared lib both source — T-0728 design note.)
VERIFY_PATTERN='(╭─+╮|─{20,})'
IDLE_SENTINEL='❯'
DIALOG_EXCLUDE='Enter to confirm|Esc to cancel'
BOOT_DIALOG_SENTINEL='I am using this for local development'
BOOT_DISMISS_DENYLIST='Do you want to (proceed|make this edit|overwrite|create|delete)|Bypass Permissions|Managed settings|don.?t ask again|Tab to amend|Exit plan mode|Ready to code|Use skill'
# Liveness tokens (T-0716 _bottom_region_shows_live_input): if the live bottom
# region shows these and no dialog footer, the loop is working/idle and a
# sentinel elsewhere is scrollback text, not a foreground wedge.
LIVENESS_SENTINELS='esc to interrupt|\? for shortcuts|ctrl\+t to hide tasks'
BOTTOM_REGION_LINES=6

# --- per-agent facts (single explicit source; mirrors _AGENT_HEALTH_CONFIG +
# DRAIN_AGENT_CONFIG). NO silent auto-detect beyond the $USER->agent table. ---
declare -A AGENT_USER=( [recovery]=claude-fallback [orchestrator]=claude [specialist]=claude-research )
declare -A AGENT_PANE=( [recovery]=claude-telegram-bot-fallback:0 [orchestrator]=claude-telegram-bot:0 [specialist]=claude-research-bot:0 )
declare -A AGENT_SERVICE=( [recovery]=claude-telegram-fallback.service [orchestrator]=claude-telegram.service [specialist]=claude-telegram-research.service )
declare -A AGENT_HOME=( [recovery]=/home/claude-fallback [orchestrator]=/home/claude [specialist]=/home/claude-research )

AGENT=""
SELF_MODE="auto"     # auto: decide from whether --agent's user == current user
DEWEDGE=1
PANE_OVERRIDE=""
CAPTURE_TIMEOUT=15
JSON=0
AT_BOOT=0            # --at-boot: SessionStart-hook mode (T-1099)

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while (( $# )); do
  case "$1" in
    --agent)      AGENT="${2:-}"; shift 2 ;;
    --self)       SELF_MODE="1"; shift ;;
    --no-dewedge) DEWEDGE=0; shift ;;
    # --at-boot (T-1099): the reflexive SessionStart self-call. The pane is NOT
    # settled mid-boot, so C1-C3 pane_check is SKIPPED (would false-positive
    # rc=7). Implies --self + --no-dewedge (NEVER send-keys into our own pane —
    # T-0559 no-self-inject). Only the SOFT guards run (C4/C6/C7/C8); RC stays 0.
    --at-boot)    AT_BOOT=1; SELF_MODE="1"; DEWEDGE=0; shift ;;
    --pane)       PANE_OVERRIDE="${2:-}"; shift 2 ;;
    --timeout)    CAPTURE_TIMEOUT="${2:-15}"; shift 2 ;;
    --json)       JSON=1; shift ;;
    -h|--help)    usage 0 ;;
    *) echo "verify_startup: unknown arg '$1'" >&2; exit 64 ;;
  esac
done

# Auto-detect agent from current user when --agent omitted.
CURRENT_USER="$(id -un)"
if [[ -z "$AGENT" ]]; then
  for a in "${!AGENT_USER[@]}"; do
    [[ "${AGENT_USER[$a]}" == "$CURRENT_USER" ]] && AGENT="$a" && break
  done
fi
if [[ -z "$AGENT" || -z "${AGENT_USER[$AGENT]:-}" ]]; then
  echo "verify_startup: unknown/undetermined agent (got '${AGENT:-}'; user '$CURRENT_USER'). Pass --agent recovery|orchestrator|specialist." >&2
  exit 64
fi

AGENT_UNIX_USER="${AGENT_USER[$AGENT]}"
PANE_TARGET="${PANE_OVERRIDE:-${AGENT_PANE[$AGENT]}}"
# Self-mode: the target user is the current user (no sudo, skip service check).
if [[ "$SELF_MODE" == "auto" ]]; then
  [[ "$AGENT_UNIX_USER" == "$CURRENT_USER" ]] && SELF_MODE=1 || SELF_MODE=0
fi

WARNINGS=()
NOTES=()
warn() { WARNINGS+=("$1"); }
note() { NOTES+=("$1"); }

# Capture the target pane. Self → direct; cross-account → sudo -n -u + XDG.
# Returns 0 + pane on stdout, or non-zero on capture failure (caller treats as SOFT).
capture_pane() {
  local cmd="tmux capture-pane -p -e -t '$PANE_TARGET'"
  if [[ "$SELF_MODE" == "1" ]]; then
    timeout "$CAPTURE_TIMEOUT" bash -c "$cmd" 2>/dev/null
  else
    timeout "$CAPTURE_TIMEOUT" sudo -n -u "$AGENT_UNIX_USER" bash -c "XDG_RUNTIME_DIR=/run/user/\$(id -u) $cmd" 2>/dev/null
  fi
}

# T-0716: bottom region (last N non-blank lines) shows live input (liveness
# token present AND no dialog footer) → working/idle, not a foreground wedge.
bottom_shows_live_input() {
  local pane="$1" bottom
  bottom="$(printf '%s\n' "$pane" | grep -v '^[[:space:]]*$' | tail -n "$BOTTOM_REGION_LINES")"
  printf '%s' "$bottom" | grep -Eq "$LIVENESS_SENTINELS" \
    && ! printf '%s' "$bottom" | grep -Eq "$DIALOG_EXCLUDE"
}

# Idle conjunction (handoff_and_restart.sh step-7).
pane_is_idle() {
  local pane="$1"
  printf '%s' "$pane" | grep -Eq "$VERIFY_PATTERN" \
    && printf '%s' "$pane" | grep -Eq "$IDLE_SENTINEL" \
    && ! printf '%s' "$pane" | grep -Eq "$DIALOG_EXCLUDE"
}

# --- C1/C2/C3 + de-wedge: the only HARD-fail surface. ---
PANE_VERDICT="ok"   # ok | wedged | uncheckable
pane_check() {
  local pane
  if ! pane="$(capture_pane)" || [[ -z "$pane" ]]; then
    warn "pane capture failed/empty for $PANE_TARGET — cannot verify TUI (SOFT; not failing boot)"
    PANE_VERDICT="uncheckable"
    return
  fi
  if pane_is_idle "$pane" || bottom_shows_live_input "$pane"; then
    PANE_VERDICT="ok"; return
  fi
  # Not idle: is this the dismissable boot dialog (and nothing dangerous co-present)?
  if (( DEWEDGE )) \
     && printf '%s' "$pane" | grep -qF "$BOOT_DIALOG_SENTINEL" \
     && ! printf '%s' "$pane" | grep -Eq "$BOOT_DISMISS_DENYLIST"; then
    note "dev-channels boot dialog detected — dismissing inline (option 1) [T-0728]"
    if [[ "$SELF_MODE" == "1" ]]; then
      timeout "$CAPTURE_TIMEOUT" bash -c "tmux send-keys -t '$PANE_TARGET' -l 1 && sleep 0.7 && tmux send-keys -t '$PANE_TARGET' Enter" 2>/dev/null || true
    else
      timeout "$CAPTURE_TIMEOUT" sudo -n -u "$AGENT_UNIX_USER" bash -c "XDG_RUNTIME_DIR=/run/user/\$(id -u) tmux send-keys -t '$PANE_TARGET' -l 1 && sleep 0.7 && tmux send-keys -t '$PANE_TARGET' Enter" 2>/dev/null || true
    fi
    sleep 1
    local after
    if after="$(capture_pane)" && { pane_is_idle "$after" || bottom_shows_live_input "$after"; }; then
      note "boot dialog dismissed; pane now idle"
      PANE_VERDICT="ok"; return
    fi
    warn "boot dialog dismiss did not clear the pane"
    PANE_VERDICT="wedged"; return
  fi
  # Any other dialog / denylisted / dewedge off → fail-closed, no keypress.
  PANE_VERDICT="wedged"
  warn "pane not at idle prompt and not an auto-dismissable boot dialog (fail-closed)"
}

# --- C4: Telegram reachable (SOFT). Per-agent probe. ---
# All three agents now self-poll their own bot via plugin:telegram (Orchestrator
# migrated off the shared pantheon-bridge in T-0767; Recovery/Specialist were already
# plugin clients). The bridge-client path + its /health probe was retired in
# T-0816 — every agent probes its own bot's getMe with the configured token.
telegram_check() {
  local envf="${AGENT_HOME[$AGENT]}/.claude/channels/telegram/.env" token=""
  if [[ "$SELF_MODE" == "1" && -r "$envf" ]]; then
    # The plugin .env uses TELEGRAM_BOT_TOKEN (all three agents); accept a bare
    # BOT_TOKEN too for robustness. The old '^BOT_TOKEN=' anchor never matched
    # TELEGRAM_BOT_TOKEN, so this probe was silently dead fleet-wide (always
    # warned + skipped getMe). [tidy-skim 2026-06-11]
    token="$(grep -m1 -E '^(TELEGRAM_)?BOT_TOKEN=' "$envf" 2>/dev/null | cut -d= -f2-)"
  fi
  if [[ -z "$token" ]]; then
    warn "telegram bot token not readable here (SOFT; skipping getMe)"; return
  fi
  timeout 5 curl -s "https://api.telegram.org/bot${token}/getMe" 2>/dev/null | grep -q '"ok": *true' \
    || warn "telegram getMe did not return ok:true (SOFT)"
}

# --- C5: service active (SOFT; skipped in self-mode). ---
service_check() {
  [[ "$SELF_MODE" == "1" ]] && return
  local st
  st="$(timeout 5 sudo -n -u "$AGENT_UNIX_USER" bash -c "XDG_RUNTIME_DIR=/run/user/\$(id -u) systemctl --user is-active '${AGENT_SERVICE[$AGENT]}'" 2>/dev/null || true)"
  [[ "$st" == "active" ]] || warn "service ${AGENT_SERVICE[$AGENT]} not active (got '${st:-unknown}') (SOFT)"
}

# --- C6: HANDOFF present & fresh (SOFT, informational). ---
handoff_check() {
  local hf="${AGENT_HOME[$AGENT]}/HANDOFF.md"
  if [[ "$SELF_MODE" == "1" ]]; then
    [[ -f "$hf" ]] || { note "no HANDOFF.md (normal on a cold boot)"; return; }
    local age=$(( $(date +%s) - $(stat -c %Y "$hf" 2>/dev/null || echo 0) ))
    (( age <= 3600 )) && note "HANDOFF.md fresh (${age}s old)" || note "HANDOFF.md stale (${age}s old)"
  fi
}

# --- C7: git hooks active (SOFT). Catches a silent core.hooksPath revert off
# .githooks. The version-controlled hooks (HERMES byte-guard pre-commit,
# commit-msg, pre-push freshness + auto-commit-cron contract gate) ONLY fire
# when core.hooksPath points at .githooks; a revert to the empty default
# .git/hooks silently disables all of them. This is the parity-guard for the
# T-1084 regression where Recovery had reverted to .git/hooks and lost every hook
# (T-1091). SOFT — a wrong hooksPath never bricks a boot, but it must surface.
githooks_check() {
  local repo="${AGENT_HOME[$AGENT]}/pantheon-config" hp=""
  if [[ "$SELF_MODE" == "1" ]]; then
    hp="$(git -C "$repo" config --get core.hooksPath 2>/dev/null || true)"
  else
    hp="$(timeout 5 sudo -n -u "$AGENT_UNIX_USER" bash -c "git -C '$repo' config --get core.hooksPath" 2>/dev/null || true)"
  fi
  if [[ "$hp" == ".githooks" || "$hp" == */.githooks ]]; then
    note "git hooks active (core.hooksPath=$hp)"
  else
    warn "git hooks NOT active: core.hooksPath='${hp:-<unset>}' (expected .githooks) — HERMES guard + pre-push contract gate are OFF; run tools/install_hooks.sh (SOFT) [T-1091]"
  fi
}

# --- C8: no stalled timers (SOFT, self-mode). An active pantheon-*.timer that
# has NO scheduled next-fire AND has never fired will never run — the silent-
# delivery-failure class (T-1096: inbox-consumer stalled at NextElapse=infinity,
# ticket auto-delivery silently down for an unknown window). Detector (T-1097,
# false-positive-hardened against three traps found live):
#   stalled = active AND has_next=NO AND never_fired=YES
#   has_next = (NextElapseUSecRealtime non-empty)  -- OnCalendar timers
#           OR (NextElapseUSecMonotonic is a finite, non-0, non-infinity value)  -- monotonic timers
# This EXCLUDES: healthy OnCalendar timers (monotonic=0 but realtime set);
# conditional/oneshot timers like restart-drain (they have fired → never_fired=NO).
# Use the FULL `show` value, NOT `-p ... --value` (which returns 'infinity' for a
# healthy firing timer — a confirmed false positive).
timers_check() {
  [[ "$SELF_MODE" == "1" ]] || return   # self-mode only for v1 (each agent flags its own stall at boot)
  local stalled=() t rt mo lt
  while read -r t; do
    [[ -z "$t" ]] && continue
    rt="$(systemctl --user show "$t" -p NextElapseUSecRealtime 2>/dev/null | cut -d= -f2-)"
    mo="$(systemctl --user show "$t" -p NextElapseUSecMonotonic 2>/dev/null | cut -d= -f2-)"
    lt="$(systemctl --user show "$t" -p LastTriggerUSec 2>/dev/null | cut -d= -f2-)"
    if [[ -n "$rt" ]] || { [[ -n "$mo" && "$mo" != "0" && "$mo" != "infinity" ]]; }; then
      continue   # has a scheduled next-fire → healthy
    fi
    [[ -z "$lt" ]] && stalled+=("$t")   # no next AND never fired → stalled
  done < <(systemctl --user list-units --type=timer --state=active --no-legend 'pantheon-*.timer' 2>/dev/null | awk '{print $1}')
  if (( ${#stalled[@]} )); then
    warn "stalled timer(s) — active but no scheduled next-fire and never triggered (will never run; silent-delivery failure, see T-1096): ${stalled[*]} — kick the .service once + ensure OnActiveSec is set (SOFT)"
  else
    note "no stalled timers (active pantheon timers all have a scheduled next-fire)"
  fi
}

# --- C9: context-fill-monitor enablement legs armed (SOFT, Recovery self-mode). The
# per-agent api-health/staleness/model-outage detectors are armed via LOCAL
# systemd drop-ins in Recovery's ~/.config that are NOT unit-tracked; a re-provision
# would silently LOSE them (detection goes dark with no signal). The
# reproducibility tool (T-1214) verifies the full set; a disarmed leg WARNs here
# so re-provision drift surfaces instead of going dark. Recovery-only — the drop-ins
# live in Recovery's config (Recovery monitors the fleet). SOFT: never bricks a boot.
monitor_dropins_check() {
  [[ "$SELF_MODE" == "1" && "$AGENT" == "recovery" ]] || return
  local repo="${AGENT_HOME[$AGENT]}/pantheon-config" out=""
  if out="$(timeout 10 python3 "$repo/tools/provision_context_fill_monitor_dropins.py" --verify 2>&1)"; then
    note "context-fill-monitor enablement legs armed"
  else
    warn "context-fill-monitor enablement DISARMED — ${out} — re-arm: python3 tools/provision_context_fill_monitor_dropins.py --provision (SOFT) [T-1214]"
  fi
}

if (( AT_BOOT )); then
  note "at-boot mode: pane check skipped (pane not settled mid-boot); SOFT guards only, no de-wedge"
else
  pane_check
fi
telegram_check
service_check
handoff_check
githooks_check
timers_check
monitor_dropins_check

# --- report ---
RC=0
[[ "$PANE_VERDICT" == "wedged" ]] && RC=7

if (( JSON )); then
  printf '{"agent":"%s","self":%s,"pane":"%s","rc":%d,"warnings":%d}\n' \
    "$AGENT" "$SELF_MODE" "$PANE_VERDICT" "$RC" "${#WARNINGS[@]}"
else
  echo "[verify_startup] agent=$AGENT self=$SELF_MODE pane=$PANE_VERDICT rc=$RC"
  for n in "${NOTES[@]:-}";    do [[ -n "$n" ]] && echo "  note: $n"; done
  for w in "${WARNINGS[@]:-}"; do [[ -n "$w" ]] && echo "  WARN: $w"; done
  (( RC == 0 )) && echo "  => healthy" || echo "  => UNHEALTHY (pane wedged)"
fi
exit "$RC"
