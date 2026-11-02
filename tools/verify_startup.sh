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
# Usage: verify_startup.sh [--agent recovery|orchestrator|specialist|archivist|mason|artisan|codex|watchman] [--self] [--no-dewedge]
#                          [--at-boot] [--pane <target>] [--timeout <sec>] [--json] [-h]
#   --at-boot: SessionStart-hook mode (T-1099) — self-call that skips the pane
#              check (pane unsettled mid-boot) + de-wedge (no-self-inject), runs
#              only the SOFT C4/C6/C7/C8/C10 guards, and always exits 0.

set -uo pipefail

# --- shared pane sentinels: MUST stay byte-equal with handoff_and_restart.sh
# (VERIFY_PATTERN/IDLE_SENTINEL/DIALOG_EXCLUDE :57-59, BOOT_DIALOG_SENTINEL :66,
# BOOT_DISMISS_DENYLIST :72). test_verify_startup.sh asserts equality to catch
# drift. (Follow-up: extract to a shared lib both source — T-0728 design note.)
# Grouped quantifiers are load-bearing, not style — see the long note on the same
# constant in handoff_and_restart.sh. A bare `─+`/`─{20,}` binds the quantifier to
# the last BYTE of a 3-byte character, so under a non-UTF-8 locale this pattern can
# never match a real frame. That matters here specifically because this script runs
# from the SessionStart hook, where the environment is whatever systemd hands us.
# Byte-identical to handoff_and_restart.sh's constant by design; change both together.
VERIFY_PATTERN='(╭(─)+╮|(─){20,})'
IDLE_SENTINEL='❯'
DIALOG_EXCLUDE='Enter to confirm|Esc to cancel|Yes, set auto mode as my default permission mode'
BOOT_DIALOG_SENTINEL='I am using this for local development'
# T-2210: a SECOND boot modal exists (Claude Code v2.1.220) — 'Try the new
# fullscreen renderer?' with options '1. Yes, try it' / '2. Not now'. It wedged
# exocortex/archivist after the operator's droplet restart on 2026-07-28.
#
# ⚠️ IT IS DISMISSED WITH 2, NOT 1, AND THAT IS THE WHOLE REASON IT IS A SEPARATE
# CONSTANT. For the dev-channels dialog option 1 is the benign acknowledgement.
# For this one option 1 is "Yes, try it" — accepting a RENDERER CHANGE on a peer's
# live session, which nobody asked for and which this fleet's recovery tooling
# reads the pane of (a renderer swap has already caused one false 'recovery
# tooling is broken' alarm here). Declining is the only safe automatic answer: a
# de-wedger exists to restore comms, not to opt a peer into a UI change. That is
# what I did by hand on 2026-07-28 and it is now encoded.
BOOT_RENDERER_SENTINEL='Try the new fullscreen renderer'
BOOT_DISMISS_DENYLIST='Do you want to (proceed|make this edit|overwrite|create|delete)|Bypass Permissions|Managed settings|don.?t ask again|Tab to amend|Exit plan mode|Ready to code|Use skill'
# Liveness tokens (T-0716 _bottom_region_shows_live_input): if the live bottom
# region shows these and no dialog footer, the loop is working/idle and a
# sentinel elsewhere is scrollback text, not a foreground wedge.
# T-4020: the shift-tab MODE footer ("⏸ manual mode on", "plan mode on",
# "accept edits on", "bypass permissions on") is a DURABLE idle indicator — it
# renders only at the live, idle input box — whereas the "? for shortcuts" hint
# ROTATES with other tips ("· ← for agents", "· ctrl+t to hide tasks"), so a
# manual-mode idle pane whose hint had cycled away read as a false WEDGE (specialist
# 2026-09-25). Key on the mode footer too. Safe-direction: a pane mid-frozen-work
# shows the spinner "esc to interrupt", not an idle mode footer, so this never
# masks the TUI-alive-but-jsonl-frozen wedge this check exists to catch.
LIVENESS_SENTINELS='esc to interrupt|\? for shortcuts|ctrl\+t to hide tasks|(manual mode|plan mode|accept edits|bypass permissions) on'
BOTTOM_REGION_LINES=6
# T-5465: Codex's idle composer has no Claude frame or ❯ sentinel. Require the
# prompt AND its durable model/worktree/git footer in the live bottom region.
# These are deliberately separate from the Claude constants above, whose byte
# equality with handoff_and_restart.sh is guarded by the smoke suite.
CODEX_IDLE_PROMPT='^[[:space:]]*›([[:space:]]|$)'
# Codex always renders model + effort + cwd. Some builds also append the git
# context (`· Main [default]`); Watchman's observed composer omits that suffix.
# The prompt conjunction and modal veto below remain mandatory in both shapes.
CODEX_IDLE_FOOTER='^[[:space:]]*[[:alnum:]._-]+[[:space:]]+[[:alnum:]._-]+[[:space:]]+·[[:space:]]+[^[:space:]]+([[:space:]]+·[[:space:]]+.+[[:space:]]+\[[^]]+\])?[[:space:]]*$'
CODEX_DIALOG_EXCLUDE='Press enter to confirm|esc to cancel|Would you like to run'

# --- per-agent facts (single explicit source; mirrors _AGENT_HEALTH_CONFIG +
# DRAIN_AGENT_CONFIG). NO silent auto-detect beyond the $USER->agent table. ---
# T-2210: was recovery|orchestrator|specialist only, so the canonical de-wedger could not target
# four of the seven seats — including the one that was actually wedged after the
# 2026-07-28 droplet restart, which had to be cleared by hand. Values taken from
# mailbox_wake.AGENT_PANE (the fullest existing source) rather than re-derived,
# and the services MEASURED per account rather than assumed.
declare -A AGENT_USER=( [recovery]=claude-fallback [orchestrator]=claude [specialist]=claude-research \
                        [archivist]=exocortex [mason]=suborch1 [artisan]=suborch2 [codex]=codex [watchman]=codex-fallback )
declare -A AGENT_PANE=( [recovery]=claude-telegram-bot-fallback:0 [orchestrator]=claude-telegram-bot:0 [specialist]=claude-research-bot:0 \
                        [archivist]=claude-telegram-bot-exocortex:0 [mason]=suborch1-main:0 [artisan]=suborch2-main:0 [codex]=codex-session \
                        [watchman]=codex-fallback-main:0 )
# ⚠️ mason and artisan have NO session-owning unit — measured: their tmux sessions
# are created directly, and `systemctl --user list-units` shows only per-task
# units (inbox-consumer@, keep-going@), nothing that owns the session. The entry
# is deliberately EMPTY and service_check SKIPS with a stated reason. Inventing a
# plausible name here would produce a permanent "not active (got 'unknown')"
# warning about a unit that was never supposed to exist.
declare -A AGENT_SERVICE=( [recovery]=claude-telegram-fallback.service [orchestrator]=claude-telegram.service [specialist]=claude-telegram-research.service \
                           [archivist]=claude-telegram-exocortex.service [mason]= [artisan]= [codex]=codex-daemon.service \
                           [watchman]=pantheon-runtime-controller@watchman.service )
# T-5299: which seats run the Telegram plugin (and so have a poller child to find).
# This is NOT derivable from AGENT_SERVICE: a control-plane seat gets a session unit from
# restart-target whether or not it has a bot, and artisan (mailbox-first, no bot) has one
# since its cutover. Keying the poller probe on the unit made artisan read "comms-dark" and
# poller_escalate kill its healthy session. Unknown seat -> 0 -> NOT_APPLICABLE -> never
# escalates (an unlisted seat must be added here deliberately to be poller-checked).
declare -A AGENT_TELEGRAM=( [recovery]=1 [orchestrator]=1 [specialist]=1 [archivist]=1 [mason]=0 [artisan]=0 [codex]=0 [watchman]=0 )
declare -A AGENT_HOME=( [recovery]=/home/claude-fallback [orchestrator]=/home/claude [specialist]=/home/claude-research \
                        [archivist]=/home/exocortex [mason]=/home/suborch1 [artisan]=/home/suborch2 [codex]=/home/codex \
                        [watchman]=/home/codex-fallback )
# ⚠️ HARNESS. Every pane sentinel in this file — the idle '❯', the boot dialogs,
# the liveness tokens — describes CLAUDE CODE's TUI. codex runs a different
# harness with a different screen, so matching those patterns against its pane is
# meaningless and DISMISSING against it would be pressing unknown keys into a live
# session. It is listed so it can be named and identified; the de-wedge refuses.
declare -A AGENT_HARNESS=( [codex]=codex [watchman]=codex )

AGENT=""
SELF_MODE="auto"     # auto: decide from whether --agent's user == current user
DEWEDGE=1
PANE_OVERRIDE=""
CAPTURE_TIMEOUT=15
JSON=0
AT_BOOT=0            # --at-boot: SessionStart-hook mode (T-1099)
# Test seam only: production reads the real procfs. The smoke suite points this
# at a synthetic process tree so PRESENT / ABSENT / UNKNOWN do not inherit the
# process state of whichever account happens to run the suite (T-2322).
PROC_ROOT="${PANTHEON_VERIFY_PROC_ROOT:-/proc}"

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while (( $# )); do
  case "$1" in
    --agent)      AGENT="${2:-}"; shift 2 ;;
    --self)       SELF_MODE="1"; shift ;;
    --no-dewedge) DEWEDGE=0; shift ;;
    # --at-boot (T-1099): the reflexive SessionStart self-call. The pane is NOT
    # settled mid-boot, so C1-C3 pane_check is SKIPPED (would false-positive
    # rc=7). Implies --self + --no-dewedge (NEVER send-keys into our own pane —
    # T-0559 no-self-inject). Only the SOFT guards run (C4/C6/C7/C8/C10); RC stays 0.
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
  echo "verify_startup: unknown/undetermined agent (got '${AGENT:-}'; user '$CURRENT_USER'). Pass a known --agent (recovery, orchestrator, specialist, archivist, mason, artisan, codex, or watchman)." >&2
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

# T-4159: a restart-bound seat's session-owning unit and supervisor come from
# instance.yaml (agent_role_map.py restart-target), overriding the static map
# above. On a control-plane seat (specialist today; recovery/orchestrator after their T-3650
# hand-move) the unit that tracks the pane is pantheon-runtime-controller@<p>
# and the bespoke claude-telegram*.service is retired: checking it is a false
# WARN, and restarting it would resurrect a second supervisor. Fail-open: a
# persona with no restart binding (mason/artisan/codex) keeps its static entry.
AGENT_SUPERVISOR="bespoke"
ROLE_MAP="${PANTHEON_AGENT_ROLE_MAP:-$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/agent_role_map.py}"
if _rt="$(timeout 10 python3 "$ROLE_MAP" restart-target "$AGENT" 2>/dev/null)"; then
  _su="$(sed -n 's/^SESSION_UNIT=//p' <<<"$_rt")"
  [[ -n "$_su" ]] && AGENT_SERVICE[$AGENT]="$_su"
  [[ "$(sed -n 's/^SUPERVISOR=//p' <<<"$_rt")" == "control-plane" ]] && AGENT_SUPERVISOR="control-plane"
fi

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

# Codex control-plane runtime (and the dedicated Codex seat): recognize only a
# complete live composer. A prompt-like glyph in output or an old footer in
# scrollback is insufficient, and modal text in the same bottom region is a
# hard veto. Everything else continues through the fail-closed path.
codex_pane_is_idle() {
  local pane="$1" bottom
  [[ "$AGENT_SUPERVISOR" == "control-plane" || "${AGENT_HARNESS[$AGENT]:-}" == "codex" ]] || return 1
  # capture-pane -e preserves SGR colour/style sequences; remove those before
  # applying line-anchored structural patterns.
  bottom="$(printf '%s\n' "$pane" | sed $'s/\033\\[[0-9;]*m//g' | grep -v '^[[:space:]]*$' | tail -n "$BOTTOM_REGION_LINES")"
  printf '%s\n' "$bottom" | grep -Eq "$CODEX_IDLE_PROMPT" \
    && printf '%s\n' "$bottom" | grep -Eq "$CODEX_IDLE_FOOTER" \
    && ! printf '%s\n' "$bottom" | grep -Eq "$DIALOG_EXCLUDE|$CODEX_DIALOG_EXCLUDE"
}

# --- T-2345: an idle pane is NOT a reachable agent. ---
#
# MEASURED 2026-07-28 recovering archivist: a session blocked on a boot modal never
# initialises the telegram plugin, AND THE PLUGIN DOES NOT RETRY once the modal
# clears. After the dialog was dismissed the pane went idle and looked perfectly
# healthy — with no `bun ... telegram/<ver> start` child and no `bun server.ts`
# grandchild. Still comms-dark, behind a prompt every pane-based check reads as fine.
# Only a full session cycle respawned them.
#
# So the de-wedge's own success condition was the false green: it proved the pane
# recovered, which is the visible surface, and said nothing about the poller child,
# which is what makes the agent contactable. Presence-vs-activation again.
#
# ⚠️ THE PROCESS TREE IS THE EVIDENCE, NEVER bot.pid. During that incident bot.pid
# was stale from Jul 20 — eight days wrong, unnoticed — and the alert that surfaced
# the problem cited that stale pid as its proof, which is why the whole thing first
# read as a pre-reboot artifact. A pid file is a proxy; the running process is the fact.
#
# Tri-state on purpose (T-2307 rule): "could not ask" is not "absent". ABSENT is
# actionable and fails the check; UNKNOWN must not claim health and must not cry wolf.
POLLER_PRESENT="PRESENT"; POLLER_ABSENT="ABSENT"; POLLER_UNKNOWN="UNKNOWN"
POLLER_NA="NOT_APPLICABLE"   # seat has no telegram plugin by design (mason/artisan)

poller_child_state() {
  # Seats with no telegram plugin have no poller BY DESIGN — mason and artisan launch
  # bare (verified: runner=0 server=0 on both, against runner=1 server=1 on orchestrator,
  # specialist and archivist). Reporting them ABSENT would be a permanent false alarm on
  # a correct configuration, which is how a check gets muted. The telegram seats are
  # exactly the ones with a telegram session unit; codex never reaches here (the
  # harness guard refuses before the de-wedge).
  if [[ -z "${AGENT_SERVICE[$AGENT]:-}" || "${AGENT_TELEGRAM[$AGENT]:-0}" != 1 ]]; then echo "$POLLER_NA"; return; fi
  # Both halves must be present: the plugin runner and its bun server child. The
  # 2026-07-28 case had NEITHER, but a half-started pair is equally not-reachable.
  #
  # Post-close T-2345 follow-up: do NOT rediscover these children with
  # `pgrep -f`. Even uid-scoped,
  # that asks whether argv text exists anywhere on the account, not whether the
  # target pane owns the poller. Ground the probe at tmux's kernel-backed pane_pid,
  # walk only that pid's kernel PPID tree (`pgrep -P`, not argv matching), then
  # identify each role by the account's ABSOLUTE Bun executable plus its exact
  # argv/cwd shape. This also
  # preserves T-2345's defining rule: the live process tree is evidence; bot.pid
  # is not (it was eight days stale in the founding incident).
  local probe='pane=$1; home=$2; proc=$3
    root=$(tmux display-message -p -t "$pane" "#{pane_pid}" 2>/dev/null) || exit 2
    [[ "$root" =~ ^[0-9]+$ && -r "$proc/$root/status" ]] || exit 2
    bun="$home/.bun/bin/bun"
    plugin="$home/.claude/plugins/cache/claude-plugins-official/telegram/"
    queue=("$root"); seen=0; runner=0; server=0
    while (( ${#queue[@]} )); do
      parent=${queue[0]}; queue=("${queue[@]:1}")
      children=$(pgrep -P "$parent" 2>/dev/null || true)
      for pid in $children; do
        [[ "$pid" =~ ^[0-9]+$ ]] || continue
        queue+=("$pid"); seen=$((seen + 1)); (( seen <= 256 )) || exit 2
        exe=$(readlink "$proc/$pid/exe" 2>/dev/null) || continue
        [[ "$exe" == "$bun" ]] || continue
        cwd=$(readlink "$proc/$pid/cwd" 2>/dev/null) || continue
        argv=()
        while IFS= read -r -d "" arg; do argv+=("$arg"); done < "$proc/$pid/cmdline"
        if (( ${#argv[@]} >= 5 )) && [[ "${argv[1]}" == run && "${argv[2]}" == --cwd \
             && "${argv[3]}" == "$plugin"* && -n "${argv[3]#"$plugin"}" \
             && "${argv[3]#"$plugin"}" != */* \
             && "$cwd" == "${argv[3]}" ]]; then
          for arg in "${argv[@]:4}"; do [[ "$arg" == start ]] && runner=1; done
        fi
        if (( ${#argv[@]} == 2 )) && [[ "${argv[1]}" == server.ts \
             && "$cwd" == "$plugin"* && -n "${cwd#"$plugin"}" \
             && "${cwd#"$plugin"}" != */* ]]; then
          server=1
        fi
      done
    done
    echo "$runner $server"'
  local out
  if [[ "$SELF_MODE" == "1" ]]; then
    out="$(timeout 8 bash -c "$probe" _ "$PANE_TARGET" "${AGENT_HOME[$AGENT]}" "$PROC_ROOT" 2>/dev/null)" \
      || { echo "$POLLER_UNKNOWN"; return; }
  else
    out="$(timeout 8 sudo -n -u "$AGENT_UNIX_USER" bash -c "$probe" _ \
      "$PANE_TARGET" "${AGENT_HOME[$AGENT]}" "$PROC_ROOT" 2>/dev/null)" \
      || { echo "$POLLER_UNKNOWN"; return; }
  fi
  [[ "$out" =~ ^[0-9]+\ [0-9]+$ ]] || { echo "$POLLER_UNKNOWN"; return; }
  local runner="${out% *}" server="${out#* }"
  if (( runner > 0 && server > 0 )); then echo "$POLLER_PRESENT"
  else echo "$POLLER_ABSENT"; fi
}

# Escalation for a confirmed-absent poller: only a session cycle respawns it.
# Returns 0 if a cycle was performed, 1 if it was unavailable (and says why).
poller_escalate() {
  if [[ "$SELF_MODE" == "1" ]]; then
    warn "poller absent but escalation UNAVAILABLE in self-mode — cycling our own session mid-check is the T-0559 no-self-act rule; a peer or the operator must do it"
    return 1
  fi
  if [[ -z "${AGENT_SERVICE[$AGENT]:-}" ]]; then
    warn "poller absent and $AGENT has NO session-owning unit (T-2210) — escalation UNAVAILABLE, reporting rather than attempting"
    return 1
  fi
  if [[ "$AGENT_SUPERVISOR" == "control-plane" ]]; then
    # T-4159: restarting the runtime controller would just re-ATTACH to the same
    # live pane (T-4101), so the poller stays dead. Killing the session is the
    # control-plane cycle: the controller's blocking wait returns, Restart=always
    # relaunches. (That relaunch gets no post_start boot kick yet: T-3650 F1.)
    local sess="${AGENT_PANE[$AGENT]%%:*}"
    note "poller child absent after de-wedge — killing control-plane session $sess so ${AGENT_SERVICE[$AGENT]} relaunches it [T-2345/T-4159]"
    timeout 30 sudo -n -u "$AGENT_UNIX_USER" bash -c \
      "XDG_RUNTIME_DIR=/run/user/\$(id -u) tmux kill-session -t '$sess'" 2>/dev/null || {
        warn "control-plane session kill of $sess failed"; return 1; }
    return 0
  fi
  note "poller child absent after de-wedge — cycling ${AGENT_SERVICE[$AGENT]} (the only thing that respawns it) [T-2345]"
  timeout 30 sudo -n -u "$AGENT_UNIX_USER" bash -c \
    "XDG_RUNTIME_DIR=/run/user/\$(id -u) systemctl --user restart '${AGENT_SERVICE[$AGENT]}'" 2>/dev/null || {
      warn "session cycle of ${AGENT_SERVICE[$AGENT]} failed"; return 1; }
  return 0
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
  if pane_is_idle "$pane" || bottom_shows_live_input "$pane" || codex_pane_is_idle "$pane"; then
    PANE_VERDICT="ok"; return
  fi
  # Not idle: is this the dismissable boot dialog (and nothing dangerous co-present)?
  if [[ -n "${AGENT_HARNESS[$AGENT]:-}" ]]; then
    # T-2210: the sentinels below are Claude Code's. Refuse rather than press keys
    # into a screen this file cannot read.
    PANE_VERDICT="wedged"
    warn "pane not idle, and $AGENT runs the '${AGENT_HARNESS[$AGENT]}' harness — this checker's \
sentinels describe Claude Code only, so it will not guess at a dismissal (fail-closed)"
    return
  fi
  local opt; opt="$(boot_dismiss_option "$pane")"
  if (( DEWEDGE )) && [[ -n "$opt" ]] \
     && ! printf '%s' "$pane" | grep -Eq "$BOOT_DISMISS_DENYLIST"; then
    note "known boot dialog detected — dismissing inline (option $opt) [T-0728/T-2210]"
    if [[ "$SELF_MODE" == "1" ]]; then
      timeout "$CAPTURE_TIMEOUT" bash -c "tmux send-keys -t '$PANE_TARGET' -l '$opt' && sleep 0.7 && tmux send-keys -t '$PANE_TARGET' Enter" 2>/dev/null || true
    else
      timeout "$CAPTURE_TIMEOUT" sudo -n -u "$AGENT_UNIX_USER" bash -c "XDG_RUNTIME_DIR=/run/user/\$(id -u) tmux send-keys -t '$PANE_TARGET' -l '$opt' && sleep 0.7 && tmux send-keys -t '$PANE_TARGET' Enter" 2>/dev/null || true
    fi
    sleep 1
    local after
    if after="$(capture_pane)" && { pane_is_idle "$after" || bottom_shows_live_input "$after" || codex_pane_is_idle "$after"; }; then
      note "boot dialog dismissed; pane now idle"
      # T-2345: the pane recovering is NOT the agent being reachable. The plugin
      # does not retry after a modal clears, so this is exactly where a comms-dark
      # seat used to be reported healthy.
      case "$(poller_child_state)" in
        "$POLLER_PRESENT")
          note "telegram poller child present — agent is actually reachable"
          PANE_VERDICT="ok"; return ;;
        "$POLLER_NA")
          note "no telegram poller expected for $AGENT (no telegram plugin / no session unit) — pane recovery is the whole check here"
          PANE_VERDICT="ok"; return ;;
        "$POLLER_ABSENT")
          warn "pane is idle but the telegram poller child is ABSENT — agent is COMMS-DARK behind a healthy-looking prompt (T-2345)"
          if poller_escalate; then
            sleep 3
            # Confirmed by the SUCCESSOR, not by the act: a restart that came up
            # without the poller is the same false green one layer down.
            if [[ "$(poller_child_state)" == "$POLLER_PRESENT" ]]; then
              note "session cycled and the poller child is now present — comms restored"
              PANE_VERDICT="ok"; return
            fi
            warn "session cycled but the poller child is STILL absent — needs a human"
          fi
          PANE_VERDICT="wedged"; return ;;
        *)
          warn "pane is idle but the poller child could not be determined — NOT claiming healthy (unknown is not present)"
          PANE_VERDICT="uncheckable"; return ;;
      esac
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
  # T-2210: the "this agent has no session-owning unit" fact is true regardless of
  # who is asking, so it is reported BEFORE the self-mode early return. Otherwise a
  # seat checking itself is told nothing and a reader cannot distinguish "no unit"
  # from "not checked".
  local st
  if [[ -z "${AGENT_SERVICE[$AGENT]:-}" ]]; then
    note "no session-owning service for $AGENT — skipping (its tmux session is not unit-managed)"
    return
  fi
  [[ "$AGENT_SUPERVISOR" == "control-plane" ]] && \
    note "session supervisor for $AGENT: ${AGENT_SERVICE[$AGENT]} (control-plane per instance.yaml) [T-4159]"
  [[ "$SELF_MODE" == "1" ]] && return
  st="$(timeout 5 sudo -n -u "$AGENT_UNIX_USER" bash -c "XDG_RUNTIME_DIR=/run/user/\$(id -u) systemctl --user is-active '${AGENT_SERVICE[$AGENT]}'" 2>/dev/null || true)"
  [[ "$st" == "active" ]] || warn "service ${AGENT_SERVICE[$AGENT]} not active (got '${st:-unknown}') (SOFT)"
}

# T-2210: which key dismisses the modal currently on screen, or "" if none of the
# known ones is. Returns 1 for the dev-channels confirm and 2 ("Not now") for the
# fullscreen-renderer offer — see BOOT_RENDERER_SENTINEL for why the difference is
# load-bearing rather than cosmetic.
boot_dismiss_option() {
  local pane="$1"
  if printf '%s' "$pane" | grep -qF "$BOOT_DIALOG_SENTINEL"; then echo 1; return 0; fi
  if printf '%s' "$pane" | grep -qF "$BOOT_RENDERER_SENTINEL"; then echo 2; return 0; fi
  echo ""; return 1
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
    # T-5223: an unset key and a refused sudo both exit 1, so probe sudo first;
    # without the grant the read is UNKNOWN, not a false "hooks are OFF".
    if ! timeout 5 sudo -n -u "$AGENT_UNIX_USER" true 2>/dev/null; then
      note "git hooks UNKNOWN: cannot read $AGENT_UNIX_USER's core.hooksPath cross-account (no sudo -n -u grant)"
      return
    fi
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

# --- C10: break-glass flag (SOFT; T-4392 phase 2a). While a LIVE flag stands for this
# account, the fail-closed hooks it names FAIL OPEN on their fail-closed paths until it
# expires; surface it at every boot. The base is world-readable (root:root 0755), so the
# listing needs no sudo even cross-account. Read-only, via the helper's own parser (the same
# checks the hooks apply). PANTHEON_BREAKGLASS_LIST_BASE is a test seam only (like
# PANTHEON_VERIFY_PROC_ROOT); the hooks never read it.
breakglass_check() {
  local helper line out
  helper="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/hooks/_failclosed.py"
  [[ -f "$helper" ]] || return
  out="$(timeout 5 python3 "$helper" --list --account "$AGENT_UNIX_USER" \
         --base "${PANTHEON_BREAKGLASS_LIST_BASE:-/var/lib/pantheon-breakglass}" 2>/dev/null || true)"
  while IFS= read -r line; do
    [[ -n "$line" ]] && warn "BREAK-GLASS flag for ${AGENT_UNIX_USER}: ${line} — a LIVE flag makes the named fail-closed hooks FAIL OPEN until expiry (SOFT) [T-4392]"
  done <<<"$out"
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
breakglass_check

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
