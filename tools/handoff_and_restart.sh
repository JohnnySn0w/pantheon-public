#!/bin/bash
# handoff_and_restart.sh — orchestrate one graceful HO handoff cycle.
#
# Part of T-0008 deliverable 2: chain prompt -> wait -> restart -> verify
# behind a single command, eliminating the multi-step manual broker dance.
#
# Steps (each is also the script's exit code on failure):
#   1. Acquire flock on /tmp/handoff_and_restart.lock (non-blocking).
#   2. Read pre-prompt mtime of the HANDOFF.md file.
#   3. Inject the prompt into the target tmux pane.
#   4. Poll mtime until it changes (HO acknowledges by writing handoff).
#   5. systemctl restart the service.
#   6. systemctl is-active fast-fail (poll until active or timeout).
#   7. Capture the target pane and grep for steady-state sentinel pattern.
#   0 = success.
#
# T-2658 — control-plane-supervised seats (--supervisor control-plane, e.g.
# specialist post-T-2633-cutover): steps 5/6 branch.
#   5. tmux kill-session on the target pane's session FIRST (abort if it
#      survives), THEN systemctl restart — $SERVICE is the caller-resolved
#      pantheon-persona-consume@<persona>.service, not a bespoke unit; a live
#      matching pane would otherwise make runtime_controller.ensure_runtime()
#      just REUSE the old process instead of restarting it.
#   6. poll runtime_scope_owned_and_session_live (tools/t2590_cutover_verify_
#      lib.sh) instead of `systemctl is-active` — the consume unit is
#      Type=oneshot with no RemainAfterExit, so it is never is-active after
#      it exits; the real witness is the pane it started.
# Step 7 (composer-ready pane sentinel) is unchanged for both branches — it
# is already supervisor-agnostic. See tickets/active/T-2658.md (or
# tickets/done/T-2658.md once resolved) for the full design.
#
# Defaults assume HO's substrate (claude.service, --user mode, default
# HANDOFF.md path) but every behavioral knob is parameterized — see flags.
#
# Justifies-via:
#   - P1 (architecture over policy): one command serializes via flock; mode
#     and pane are explicit parameters not auto-detected (auto-detect drift
#     bites at the worst time). Sentinel pattern is the structural readiness
#     signal; ack is mtime-not-flag-file because mtime is the structural
#     witness already produced by HO writing the handoff.
#   - D1 (preserve bandwidth): one trigger replaces ~5 minutes of broker time.
#   - P3 (decisions justify upward): each non-obvious choice (sentinel regex,
#     two-phase verification, exit code = step number) is documented inline.

set -u

# ---------------------------------------------------------------------------
# Shared send_keys_submit helper (T-0533: extracted to standalone file).
# Source it early so INJECT_ENTER_DELAY, PENDING_PASTE_SENTINEL, and
# SUBMIT_RETRIES are all defined before the Defaults block that references them,
# and so send_keys_submit() is available throughout the script.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=send_keys_submit.sh
source "$SCRIPT_DIR/send_keys_submit.sh"
# T-2658: runtime_scope_owned_and_session_live — the control-plane-branch
# readiness predicate for step 6 (see the header comment above). Sourced
# unconditionally (cheap, function-only, no top-level side effects — same
# posture as send_keys_submit.sh) so it's available regardless of which
# --supervisor value this invocation ends up with.
# shellcheck source=t2590_cutover_verify_lib.sh
source "$SCRIPT_DIR/t2590_cutover_verify_lib.sh"

# ---------------------------------------------------------------------------
# Defaults.
# ---------------------------------------------------------------------------

SERVICE=""
MODE=""                                  # user | system
TARGET_PANE=""
HANDOFF_FILE="/home/claude/HANDOFF.md"
PROMPT_TEXT=""                           # default below if unset
ACK_TIMEOUT=600                          # 10 min — HO writing a handoff
STEADY_TIMEOUT=60                        # 1 min — fresh session warming up
POLL_INTERVAL=2                          # seconds between mtime/pane polls
SUPERVISOR="bespoke"                     # T-2658: bespoke | control-plane — branches steps 5/6
# The singleton control-plane tmux scope (tools/runtime_controller.py's
# TMUX_RUNTIME_SCOPE_UNIT). Not a CLI flag — production has exactly one value
# and every control-plane-supervised seat shares it; PROC_ROOT below is the
# test seam instead.
RUNTIME_SCOPE_UNIT="pantheon-runtime-tmux.scope"
PROC_ROOT="${HANDOFF_RESTART_PROC_ROOT:-/proc}"  # T-2658 test seam only
# INJECT_ENTER_DELAY, PENDING_PASTE_SENTINEL, SUBMIT_RETRIES are now defined
# in send_keys_submit.sh (sourced above). Values: 0.7 / '[Pasted text' / 2.
# T-2330/T-2351-adjacent (CI red, 2026-07-29): the quantifiers MUST wrap `─` in a
# group. `─` is 3 bytes (E2 94 80) and in a non-UTF-8 (byte) locale a bare `─+` /
# `─{20,}` binds the quantifier to the LAST BYTE only — "E2 94 then 0x80 repeated" —
# which no real box-drawing run can produce, so the frame check silently never
# matches. `❯` carries no quantifier and keeps matching, so the failure presents as
# a half-passing conjunction rather than as a locale problem. Grouping makes the
# quantifier apply to the whole character and is identical under UTF-8.
# Found because the self-hosted runner runs a byte locale while all four agent seats
# are C.UTF-8: `LC_ALL=C` reproduces the CI failure here exactly (15/18, same list).
VERIFY_PATTERN='(╭(─)+╮|(─){20,})'      # Claude Code prompt-frame top edge (corner-framed or bare horizontal run)
IDLE_SENTINEL='❯'                        # positive idle-prompt cursor; T-0528: empirical 90-frame Recovery-restart capture showed `❯` rendered in every frame from t+1s, while `? for shortcuts` rendered in only 1/90 — so `❯` is the reliable boot-window match. Stale OR-alternates (? for shortcuts, ctrl+t to hide tasks) removed.
DIALOG_EXCLUDE='Enter to confirm|Esc to cancel'  # negative: any of these in the pane DISQUALIFIES the steady-state match. T-0528: `❯` alone false-passes the dev-channels confirm dialog (option cursor is also `❯`), defeating T-0172's dialog guard. The dialog's footer markers are highly specific to Claude Code's confirm/cancel UI and are not present in normal idle/prose output.
# T-0721: the dev-channels development-channels startup confirm a freshly-restarted
# session boots into. Dismissing it (option 1) is SAFE and leaves the session idle.
# Dismissed INLINE at step-7 so the restart completes now instead of leaving the
# session wedged on the dialog until the separate ~5-10min inbox-consumer
# auto-resolve cron clears it (the wedge window the operator flagged 2026-06-02).
# EXACT fixed-string match only.
BOOT_DIALOG_SENTINEL='I am using this for local development'
# T-2210: a SECOND boot modal exists (Claude Code v2.1.220) — 'Try the new
# fullscreen renderer?' with options '1. Yes, try it' / '2. Not now'. It wedged
# exocortex/mnemosyne after the operator's droplet restart on 2026-07-28.
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
# Deny-list (regex): dangerous / non-boot confirms that must NEVER be auto-dismissed
# by the restart. If ANY is co-present we do NOT press 1 — fall through to exit 7
# (fail-closed; the human/cron handles it). Defends the "boot sentinel present in
# scrollback while a DIFFERENT dangerous modal is focused" edge (T-0721 adversarial
# review, HOLE 1).
BOOT_DISMISS_DENYLIST='Do you want to (proceed|make this edit|overwrite|create|delete)|Bypass Permissions|Managed settings|don.?t ask again|Tab to amend|Exit plan mode|Ready to code|Use skill'
LOCKFILE="/tmp/handoff_and_restart.$(id -u).lock"  # per-UID: HO (claude) and Recovery (claude-fallback) drive different services and must not contend on — or EPERM each other out of — one shared /tmp lock
DRY_RUN=0
SKIP_ACK=0                               # T-0510: force-restart mode — skip steps 2-4 (handoff prompt + ack-wait)
DISMISS_MODAL=0                          # T-2829: send Escape before the handoff prompt to clear a blocking permission modal (fail-closed — Escape CANCELS/denies, never approves). Opt-in; autonomous wedge-recovery passes it, the operator's attended manual restart does not.
HARD_RESTART_ON_ACK_TIMEOUT=0            # T-2829: on step-4 ack timeout, fall through to the restart (steps 5-8) instead of exit 4, so a session that cannot hand off (e.g. modal-wedged) is still recovered rather than left wedged. Opt-in.
NO_RESTART=0                             # T-1049: pause mode — run steps 1-4 + chronicle, then STOP (no restart). For the fleet-reboot quiesce: each agent finishes + writes HANDOFF + is chronicle'd + pauses, then the machine reboots.
KICK_ENABLED=1                           # T-0172: post-step-7 HANDOFF kick (default on; --no-kick to disable)
KICK_PROMPT=""                           # default below if unset

# DEFAULT_PROMPT and DEFAULT_KICK_PROMPT defined AFTER arg parsing so they
# interpolate the final $HANDOFF_FILE value (which can be overridden via
# --handoff-file). Pre-T-0179-validation these were hardcoded to
# /home/claude/HANDOFF.md, breaking multi-agent restart for specialist/recovery.

# ---------------------------------------------------------------------------
# Arg parsing.
# ---------------------------------------------------------------------------

usage() {
  cat <<'EOF' >&2
Usage: handoff_and_restart.sh [options]

Required:
  --service <name>            systemd unit name (e.g. claude.service)
  --mode <user|system>        systemctl scope
  --target-pane <pane>        tmux target pane (e.g. main:0.0)

Optional:
  --handoff-file <path>       default: /home/claude/HANDOFF.md
  --prompt <text>             prompt to send to HO; default is canned
  --ack-timeout <sec>         default: 600
  --steady-timeout <sec>      default: 60
  --poll-interval <sec>       default: 2
  --verify-pattern <regex>    grep -E pattern; default: '(╭─+╮|─{20,})' (corner-framed or bare horizontal run >=20)
  --idle-sentinel <regex>     grep -E pattern (T-0172, T-0528). Positive prong of the step-7
                              conjunction: verify-pattern AND idle-sentinel must BOTH match in pane
                              AND dialog-exclude must NOT match. default: '❯' (the prompt cursor;
                              empirical from T-0528 capture — present in every frame of the boot
                              window, while older footer hints like '? for shortcuts' are not).
  --dialog-exclude <regex>    grep -E pattern (T-0528). Negative prong of the conjunction: if this
                              matches the pane, step-7 FAILS even if verify-pattern + idle-sentinel
                              match. default: 'Enter to confirm|Esc to cancel' — Claude Code's
                              dev-channels confirm dialog uses `❯` as its option cursor, which
                              false-passes the bare-`❯` idle-sentinel; the footer markers are
                              highly specific and either alone disqualifies. Pass an unmatchable
                              regex (e.g. 'NEVER_MATCH_THIS') to effectively disable the negative
                              prong (not recommended outside tests).
  --lockfile <path>           default: /tmp/handoff_and_restart.lock
  --kick-prompt <text>        post-step-7 directive injected into the fresh pane to trigger first-turn
                              action (Read HANDOFF.md + post status). default: canned text referencing chat_id 456474197.
  --no-kick                   skip the post-step-7 kick (T-0172). useful for tests / one-off invocations.
  --skip-ack                  force-restart mode (T-0510): skip steps 2-4 (handoff prompt + ack-wait) and
                              go straight to restart. For agents that cannot write a handoff (e.g. a
                              session 400ing on every API request). Steps 5-8 (restart, is-active,
                              steady-state, kick) still run, so the one wrapper still owns systemctl.
  --dismiss-modal             T-2829: before the handoff prompt (step 3), send a single Escape to the
                              target pane to clear any blocking permission modal. Escape CANCELS the
                              modal (denies the pending action) — the fail-closed direction; it never
                              presses an approval key. Lets a session wedged behind a modal (e.g. a
                              settings.json-edit confirm) receive + act on the handoff prompt instead
                              of ignoring it. Safe blind-send in the restart context: idle pane → no-op;
                              mid-turn → interrupt (we are restarting anyway); modal → dismiss. Ignored
                              under --skip-ack (no handoff prompt to protect). Off by default.
  --hard-restart-on-ack-timeout
                              T-2829: on step-4 ack timeout, instead of exit 4, log + fall through to
                              the restart (steps 5-8) — a hard restart. Guarantees recovery of a session
                              that cannot hand off (modal-wedged, or otherwise not progressing) rather
                              than abandoning it wedged (the pre-T-2829 hole that left specialist stuck on a
                              settings.json modal ~3.3d). The fresh handoff is lost, but the prior
                              cycle's handoff + chronicle remain. Paired with --dismiss-modal by the
                              autonomous wedge-recovery path (restart_drain graceful invocation): Escape
                              first (preserve the handoff), hard-restart if it still can't. Off by default.
  --no-restart                pause mode (T-1049): run steps 1-4 + chronicle archive (prompt the agent to
                              finish + write its HANDOFF, wait for the mtime ack, snapshot to the
                              chronicle), then STOP — skip the restart and all post-restart steps (5-8).
                              The fleet-reboot quiesce: each agent is left finished + recorded + paused;
                              the machine reboot (driven by the fleet orchestrator) is what cycles it.
                              Mutually exclusive with --skip-ack (which has no fresh handoff to chronicle).
  --dry-run                   print plan; perform no side effects
  --supervisor <bespoke|control-plane>
                              default: bespoke. T-2658: control-plane seats (a persona cut
                              over to the harness-manager control plane, e.g. specialist post-
                              T-2633) are NOT restarted via a plain `systemctl restart` of
                              $SERVICE — step 5 first tmux-kills the target pane's session
                              (aborting if it survives), then restarts $SERVICE (the caller-
                              resolved pantheon-persona-consume@<persona> unit); step 6 polls
                              runtime-scope + session liveness instead of `systemctl is-
                              active` (a Type=oneshot consume unit is never is-active after
                              it exits). bespoke (the default) is BYTE-IDENTICAL to pre-T-2658
                              behavior — this flag is fully additive.

Exit codes:
  0 success; 1 lock; 2 pre-mtime; 3 prompt-send; 4 ack-timeout;
  5 restart (bespoke: systemctl restart failed; control-plane: also covers a
    kill-session that didn't take); 6 is-active / runtime-not-live; 7 steady-state;
  8 kick-send; 64 usage error.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service)         SERVICE="$2"; shift 2 ;;
    --mode)            MODE="$2"; shift 2 ;;
    --target-pane)     TARGET_PANE="$2"; shift 2 ;;
    --handoff-file)    HANDOFF_FILE="$2"; shift 2 ;;
    --prompt)          PROMPT_TEXT="$2"; shift 2 ;;
    --ack-timeout)     ACK_TIMEOUT="$2"; shift 2 ;;
    --steady-timeout)  STEADY_TIMEOUT="$2"; shift 2 ;;
    --poll-interval)   POLL_INTERVAL="$2"; shift 2 ;;
    --verify-pattern)  VERIFY_PATTERN="$2"; shift 2 ;;
    --idle-sentinel)   IDLE_SENTINEL="$2"; shift 2 ;;
    --dialog-exclude)  DIALOG_EXCLUDE="$2"; shift 2 ;;
    --lockfile)        LOCKFILE="$2"; shift 2 ;;
    --kick-prompt)     KICK_PROMPT="$2"; shift 2 ;;
    --no-kick)         KICK_ENABLED=0; shift ;;
    --skip-ack)        SKIP_ACK=1; shift ;;
    --dismiss-modal)   DISMISS_MODAL=1; shift ;;
    --hard-restart-on-ack-timeout) HARD_RESTART_ON_ACK_TIMEOUT=1; shift ;;
    --no-restart)      NO_RESTART=1; shift ;;
    --dry-run)         DRY_RUN=1; shift ;;
    --supervisor)      SUPERVISOR="$2"; shift 2 ;;
    -h|--help)         usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 64 ;;
  esac
done

if [[ -z "$SERVICE" || -z "$MODE" || -z "$TARGET_PANE" ]]; then
  echo "missing required arg" >&2
  usage
  exit 64
fi
case "$MODE" in
  user|system) ;;
  *) echo "--mode must be 'user' or 'system'" >&2; exit 64 ;;
esac
case "$SUPERVISOR" in
  bespoke|control-plane) ;;
  *) echo "--supervisor must be 'bespoke' or 'control-plane'" >&2; exit 64 ;;
esac
if [[ "$SUPERVISOR" == "control-plane" ]]; then
  if [[ ! "$SERVICE" =~ ^pantheon-persona-consume@[a-z][a-z0-9_-]*\.service$ ]]; then
    echo "control-plane supervisor requires pantheon-persona-consume@<safe-persona>.service; got '$SERVICE'" >&2
    exit 64
  fi
fi
if [[ "$NO_RESTART" == "1" && "$SKIP_ACK" == "1" ]]; then
  echo "--no-restart and --skip-ack are mutually exclusive (skip-ack has no fresh handoff to chronicle, and no-restart skips the restart skip-ack exists to perform)" >&2
  exit 64
fi
# In pause mode the agent is asked to finish + write its handoff + STOP (it will
# not be restarted by this wrapper); the machine reboot cycles it. Use a
# pause-specific default prompt unless the caller overrode --prompt.
DEFAULT_PROMPT="Recovery here — please write a fresh ${HANDOFF_FILE} capturing current state, in-flight work, and immediate next actions for the next session. When done, say \"handoff written\" and stop. I will then restart the service."
DEFAULT_PAUSE_PROMPT="Recovery here — a coordinated machine reboot is imminent. Please finish your current step, then write a fresh ${HANDOFF_FILE} capturing current state, in-flight work, and immediate next actions. When done, say \"handoff written\" and stop — do NOT start new work; the reboot will cycle the session and the next session resumes from your handoff."
# T-1500 (A): the kick must CONTINUE the handoff's queued work, not stop at the
# snapshot — a fresh session that completes the literal ask and parks caused the
# 2026-07-02 6.5h idle gap. Keep the phrase "post a one-paragraph status
# snapshot" intact (the smoke suite's kick_busy_token keys on it) and keep in
# sync with inbox_consumer.RECOVERY_AUTO_RESOLVE_KICKOFF_PROMPT.
DEFAULT_KICK_PROMPT="Read ${HANDOFF_FILE}, then post a one-paragraph status snapshot to the operator via your Telegram tool (chat_id 456474197). Then continue working immediately: pick up the handoff's queued next work (or your next ticket per standing priority) and execute — do not park after the snapshot. Begin."

if [[ -z "$PROMPT_TEXT" ]]; then
  if [[ "$NO_RESTART" == "1" ]]; then
    PROMPT_TEXT="$DEFAULT_PAUSE_PROMPT"
  else
    PROMPT_TEXT="$DEFAULT_PROMPT"
  fi
fi
if [[ -z "$KICK_PROMPT" ]]; then
  KICK_PROMPT="$DEFAULT_KICK_PROMPT"
fi

SYSTEMCTL_FLAG="--$MODE"

log() { echo "[handoff-and-restart] $*"; }
err() { echo "[handoff-and-restart] ERROR: $*" >&2; }

# send_keys_submit() is defined in send_keys_submit.sh (sourced at top).
# See that file for the full submit-verify logic + constant documentation.
# T-0533: DRY refactor — removed inline copy, single source of truth is now
# tools/send_keys_submit.sh.

# ---------------------------------------------------------------------------
# Dry run: print plan, no side effects.
# ---------------------------------------------------------------------------

if [[ "$DRY_RUN" == "1" ]]; then
  cat <<EOF
DRY RUN — no side effects.
  service:           $SERVICE ($MODE)
  supervisor:        $SUPERVISOR
  target pane:       $TARGET_PANE
  handoff file:      $HANDOFF_FILE
  ack timeout:       ${ACK_TIMEOUT}s
  steady timeout:    ${STEADY_TIMEOUT}s
  verify pattern:    $VERIFY_PATTERN
  idle sentinel:     $IDLE_SENTINEL
  dialog exclude:    $DIALOG_EXCLUDE
  skip ack:          $SKIP_ACK
  no restart (pause):$NO_RESTART
  kick enabled:      $KICK_ENABLED
  lockfile:          $LOCKFILE
  prompt (truncated):
    $(printf '%s' "$PROMPT_TEXT" | head -c 200)
EOF
  exit 0
fi

# ---------------------------------------------------------------------------
# Wall-clock watchdog (T-0584 Layer 1).
#
# Backstop the whole script with a process-group SIGTERM/SIGKILL after
# HANDOFF_RESTART_WALL_CLOCK_SECS so any single hung child (typically a
# tmux send-keys -l that wedges on a paste-mode pane) cannot hold the
# flock indefinitely. Caught 2026-05-25: a 2026-05-22 invocation against
# Orchestrator wedged at step 3 with its `send-keys -l` child still alive
# 3d 10h later, holding the flock; subsequent monitor fires saw
# "lock held by another instance" every 5min for days while context fill
# silently climbed past 60%. systemd could not cgroup-kill the zombie
# across UIDs; a same-UID watchdog inside the script is the structural fix.
#
# Self-contained within the script's UID — no cross-UID privilege needed.
# Default 1800s (30 min) is well past any healthy run (~minutes); the
# step-4 ack-poll already caps at ACK_TIMEOUT (default 600s) so 1800s
# leaves comfortable headroom. Override via env for test runs.
# ---------------------------------------------------------------------------

WALL_CLOCK_TIMEOUT_SECS="${HANDOFF_RESTART_WALL_CLOCK_SECS:-1800}"
SCRIPT_PID=$$

(
  # Subshell — its own process; sleeps the budget then kills the script
  # and ITS DESCENDANTS. We don't depend on process-group leadership (a
  # pgroup-kill assumes $$ == pgrp, which holds under sudo + setsid but
  # not under arbitrary test harnesses), so we walk the descendant tree
  # via pkill -P instead. Covers the actual production wedge — a hung
  # `tmux send-keys -l` child of the script.
  #
  # T-2617: the sleep is BACKGROUNDED (not run in the foreground with
  # `sleep ... || exit 0`) so this subshell can install its own TERM trap
  # and OWN the shutdown ordering of its child. The prior shape signalled
  # the sleep (via the outer EXIT trap's `pkill -P`) and this subshell
  # (via `kill`) as two INDEPENDENT targets, then only `wait`ed for this
  # subshell — which has no trap of its own and can be reaped on SIGTERM
  # regardless of whether the sleep has actually finished dying. Under
  # real CPU contention the sleep can take measurably longer than this
  # subshell to act on its own SIGTERM, so this subshell exits first,
  # orphaning the sleep under init for up to its full budget (confirmed
  # 20/20 reproducible with a deterministically slow-to-die stand-in;
  # unreproducible via 660 probabilistic-load trials — see T-2617). Now
  # this subshell explicitly kills + reaps its own sleep child (below)
  # before it exits, so the outer EXIT trap's `wait "$WATCHDOG_PID"`
  # cannot return until the sleep is actually gone.
  sleep "$WALL_CLOCK_TIMEOUT_SECS" &
  SLEEP_PID=$!
  # An INTERRUPTED sleep is the EXIT trap's cancel signal, not a timeout —
  # without this guard the subshell races the trap's follow-up kill and can
  # proceed into the FATAL branch on a clean exit, printing a spurious FATAL
  # and TERMing the exiting script (observed 2026-07-06 on orchestrator's first
  # idle-ceiling restart: 'cycle complete' then FATAL at 153s of an 1800s
  # budget — T-1536). On cancel: kill + reap the sleep, then exit quietly
  # (T-2617: reap happens HERE, inside this subshell, so it is guaranteed
  # complete before this subshell itself exits).
  trap 'kill "$SLEEP_PID" 2>/dev/null; wait "$SLEEP_PID" 2>/dev/null; exit 0' TERM
  wait "$SLEEP_PID"
  # Reaching here (no TERM trap fired) means the sleep ran its full budget
  # naturally — a genuine wedge, not a cancel.
  # Belt-and-braces: if the script is already gone, there is nothing to reap.
  kill -0 "$SCRIPT_PID" 2>/dev/null || exit 0
  echo "[handoff-and-restart] FATAL: wall-clock timeout ${WALL_CLOCK_TIMEOUT_SECS}s exceeded; killing script PID $SCRIPT_PID and descendants" >&2
  pkill -TERM -P "$SCRIPT_PID" 2>/dev/null || true
  kill -TERM "$SCRIPT_PID" 2>/dev/null || true
  sleep 5
  pkill -KILL -P "$SCRIPT_PID" 2>/dev/null || true
  kill -KILL "$SCRIPT_PID" 2>/dev/null || true
) &
WATCHDOG_PID=$!
# Clean shutdown: cancel the watchdog when the script exits normally so the
# sleep doesn't outlive us. T-2617: no longer independently `pkill -P`s the
# sleep here — the subshell's own TERM trap (above) now owns killing +
# reaping its sleep child before it exits, so a plain `kill` + `wait` on the
# subshell alone is sufficient and can no longer race the child's death.
trap 'kill "$WATCHDOG_PID" 2>/dev/null; wait "$WATCHDOG_PID" 2>/dev/null' EXIT

# ---------------------------------------------------------------------------
# Stale-flock takeover (T-0584 Layer 2).
#
# Before attempting the flock, check whether the lockfile is held by a
# process whose ELAPSED exceeds STALE_FLOCK_THRESHOLD_SECS. If so, kill
# the holder + its children and rm the lockfile, then proceed. Healthy
# runs are sub-minute; 30 min is well past any normal cycle — and is
# also the wall-clock watchdog budget (Layer 1), so any pre-Layer-1
# zombie holder will exceed it long-term, and any Layer-1-bounded run
# released the lock cleanly on exit.
#
# Defensive belt for Layer 1: Layer 1 ensures the script kills itself
# within the budget; Layer 2 covers the edge case where the script died
# but the lockfile didn't get released (or a non-Pantheon process is
# holding it — `lsof -t` will surface whoever has the FD open).
# ---------------------------------------------------------------------------

STALE_FLOCK_THRESHOLD_SECS="${HANDOFF_RESTART_STALE_FLOCK_THRESHOLD_SECS:-1800}"
if [[ -f "$LOCKFILE" ]] && command -v lsof >/dev/null 2>&1; then
  # `lsof -t` prints PIDs only, one per line. head -1 if multiple.
  holder_pid=$(lsof -t "$LOCKFILE" 2>/dev/null | head -1)
  if [[ -n "$holder_pid" ]]; then
    # `ps -o etimes=` prints elapsed seconds since the process started.
    holder_elapsed=$(ps -o etimes= -p "$holder_pid" 2>/dev/null | tr -d ' ')
    if [[ -n "$holder_elapsed" && "$holder_elapsed" =~ ^[0-9]+$ \
          && "$holder_elapsed" -gt "$STALE_FLOCK_THRESHOLD_SECS" ]]; then
      log "WARN: stale flock detected — PID $holder_pid has held $LOCKFILE for ${holder_elapsed}s (threshold ${STALE_FLOCK_THRESHOLD_SECS}s); reaping"
      pkill -KILL -P "$holder_pid" 2>/dev/null || true
      kill -KILL "$holder_pid" 2>/dev/null || true
      # Brief settle so the kernel releases the FD before we rm the
      # lockfile. Lockfile rm + re-create is fine even without the
      # wait, but tightening the window avoids a race where a new
      # invocation arrives between the kill and the rm.
      sleep 1
      rm -f "$LOCKFILE"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Step 1: flock — non-blocking, fail fast on contention.
# ---------------------------------------------------------------------------

# Open the lockfile on a high fd, then ask flock to acquire on it. Using a
# numeric fd (rather than `flock -n <file> <cmd>`) keeps the lock held for
# the rest of this script's process lifetime — implicit release on exit.
# Guard the OPEN separately from the LOCK: a cross-user EPERM on the lockfile
# path must not be misreported as "held by another instance" (it cost real
# diagnosis time 2026-05-21 when Recovery hit HO's claude-owned /tmp lock).
if ! ( true >>"$LOCKFILE" ) 2>/dev/null; then
  err "cannot open lockfile $LOCKFILE for writing (permission/path issue, not lock contention)"
  exit 1
fi
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  err "lock $LOCKFILE held by another instance"
  exit 1
fi

# ---------------------------------------------------------------------------
# Steps 2-4: handoff prompt + ack-wait. Skipped entirely under --skip-ack
# (T-0510 force-restart): a session 400ing on every request cannot write a
# handoff, so we bypass the prompt + mtime-ack and go straight to restart.
# Steps 5-8 (restart, is-active, steady-state, kick) still run below — so the
# one wrapper still owns systemctl; we never fork a parallel restart path.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Layer 0 (T-0584): capture pre-restart inner-claude PID for the step-7
# defensive PID-change assertion. The 2026-05-25 14:46Z legacy out-of-cgroup
# session went unnoticed because step-5 systemctl restart silently no-op'd
# the inner claude (it lived under the supervisor's pre-cgroup tmux server),
# yet sentinel matched + script exit 0. By snapshotting the inner-claude PID
# before the restart and re-checking after step 7, we convert that silent
# success into a loud step-7 failure. Best-effort: when ps finds no baseline
# (no claude currently running for this user — rare in production, normal
# in the test harness), the check is SKIPPED with a log line; the rest of
# the pipeline proceeds. Runs OUTSIDE the SKIP_ACK guard so force-restart
# mode also gets the assertion.
# ---------------------------------------------------------------------------

CLAUDE_PROCESS_PATTERN="${HANDOFF_RESTART_CLAUDE_PROCESS_PATTERN:-.npm-global/bin/claude\$}"
PRE_INNER_CLAUDE_PID=$(ps -u "$(id -u -n)" -o pid=,args= 2>/dev/null \
  | grep -E "$CLAUDE_PROCESS_PATTERN" | grep -v grep \
  | awk 'NR==1 {print $1}')
if [[ -n "$PRE_INNER_CLAUDE_PID" ]]; then
  log "pre-restart inner-claude PID: $PRE_INNER_CLAUDE_PID"
fi

if [[ "$SKIP_ACK" == "1" ]]; then
  log "skip-ack: bypassing handoff prompt + ack-wait (force-restart mode); proceeding to restart"
else

# ---------------------------------------------------------------------------
# Step 2: capture pre-prompt mtime.
# ---------------------------------------------------------------------------

if [[ ! -f "$HANDOFF_FILE" ]]; then
  # Allow a missing file — HO's first write will create it. Treat mtime as 0.
  PRE_MTIME=0
else
  if ! PRE_MTIME=$(stat -c %Y "$HANDOFF_FILE" 2>/dev/null); then
    err "stat failed on $HANDOFF_FILE"
    exit 2
  fi
fi
log "pre-prompt mtime: $PRE_MTIME ($HANDOFF_FILE)"

# ---------------------------------------------------------------------------
# Step 3: deliver the prompt via tmux send-keys.
# ---------------------------------------------------------------------------

# T-2829: pre-dismiss a blocking permission modal so a modal-wedged session can
# actually RECEIVE the handoff prompt below. When a session sits on a modal (e.g.
# "Do you want to make this edit to settings.json?"), the modal captures input —
# send_keys_submit's text lands on the modal, not the composer, so the session
# never sees the prompt, never writes a handoff, and step 4 times out (the
# 2026-08-11 specialist wedge). Escape CANCELS/denies whatever modal is focused — the
# fail-closed direction — so it is safe to send blind in the restart context:
#   idle pane  → no-op (clears the empty input line)
#   mid-turn   → interrupts the current turn (acceptable; we are restarting)
#   modal      → dismisses it (denies the pending action)
# One Escape only (minimal). Anything it can't clear is caught by the ack-timeout
# hard-restart fallback (step 4). Opt-in via --dismiss-modal; the autonomous
# wedge-recovery path passes it, the operator's attended manual restart does not.
if [[ "$DISMISS_MODAL" == "1" ]]; then
  log "dismiss-modal: sending Escape to $TARGET_PANE to clear any blocking modal (fail-closed) [T-2829]"
  tmux send-keys -t "$TARGET_PANE" Escape 2>/dev/null \
    || log "dismiss-modal: Escape send failed (non-fatal); proceeding to handoff prompt"
  sleep "$POLL_INTERVAL"
fi

# Deliver via send_keys_submit: literal text, then a delayed Enter so the TUI
# paste-mode debounce settles and the prompt actually submits (T-0519).
if ! send_keys_submit "$TARGET_PANE" "$PROMPT_TEXT"; then
  err "handoff prompt delivery/submit failed"
  exit 3
fi
log "prompt delivered to $TARGET_PANE"

# ---------------------------------------------------------------------------
# Step 4: poll mtime for HO's ack (handoff write).
# ---------------------------------------------------------------------------

ack_deadline=$(( $(date +%s) + ACK_TIMEOUT ))
ack_ok=0
while (( $(date +%s) < ack_deadline )); do
  cur=$(stat -c %Y "$HANDOFF_FILE" 2>/dev/null || echo 0)
  if [[ "$cur" != "$PRE_MTIME" && "$cur" -gt 0 ]]; then
    ack_ok=1
    log "handoff ack: mtime $PRE_MTIME -> $cur"
    break
  fi
  sleep "$POLL_INTERVAL"
done
# T-2829: ack-outcome branch.
#   success                                   → archive the fresh handoff (below), then restart.
#   timeout + --hard-restart-on-ack-timeout   → fall through to the restart (steps 5-8) WITHOUT an
#                                                archive (no fresh handoff was written; the prior
#                                                cycle's snapshot remains) — recovers a wedged session
#                                                instead of abandoning it (the specialist modal-wedge hole).
#   timeout without the flag                  → exit 4 (pre-T-2829 default; operator/cron handles it).
if [[ "$ack_ok" != "1" ]]; then
  if [[ "$HARD_RESTART_ON_ACK_TIMEOUT" == "1" ]]; then
    err "ack timeout after ${ACK_TIMEOUT}s; HANDOFF.md mtime unchanged — HARD-RESTART fallback (steps 5-8) instead of abandoning a wedged session (--hard-restart-on-ack-timeout) [T-2829]"
  else
    err "ack timeout after ${ACK_TIMEOUT}s; HANDOFF.md mtime unchanged"
    exit 4
  fi
else

# T-0766 (Veneration Chronicle): snapshot the freshly-written handoff to on-disk local
# state (NOT the repo — portability + no .git bloat, msg 2758) so a successor can READ its
# predecessor's last words (the cheap recovery tier) rather than resurrecting the dead
# session. Runs only on the ack path (a fresh handoff exists); the forced/--skip-ack path
# has no fresh handoff → no archive → that is precisely the live claude --resume tier.
# Best-effort + non-fatal: a failure here must NEVER block the restart (same discipline as
# the session_reset_log emit below).
CHRONICLE_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# T-1636 review fix: detect the dying session's uuid ONCE and pass it to BOTH
# the handoff archive and the pane-log companion below — two independent
# detections could scan different newest-jsonls seconds apart and break the
# self-pairing <ts>-<uuid> stem contract.
CHRONICLE_UUID="$(python3 "$(dirname "$0")/chronicle.py" detect-uuid \
      --projects-dir "$HOME/.claude/projects" 2>/dev/null || echo unknown)"

# T-2638: conservation check BEFORE the archive call below — at this point
# chronicle.py's latest snapshot is still the PRIOR cycle's handoff, so this
# compares two genuinely consecutive HANDOFF rewrites (the archive call right
# after this turns the current text into the new latest). Advisory only: it
# only ever prints ALERT lines on suspected silent detail loss (context
# collapse / whole-section loss) and never blocks the restart — no exit-code
# branching here on purpose, since a `python | sed` pipe would mask the
# python exit code under `set -u` without `pipefail` anyway; the alert lines
# already carry through stdout/stderr regardless.
python3 "$(dirname "$0")/handoff_conservation.py" check \
      --handoff-file "$HANDOFF_FILE" \
      2>&1 | sed 's/^/  [handoff-conservation] /'

if ! python3 "$(dirname "$0")/chronicle.py" archive \
      --handoff-file "$HANDOFF_FILE" \
      --reason "graceful_restart" \
      --ts "$CHRONICLE_TS" \
      --uuid "$CHRONICLE_UUID" \
      2>&1 | sed 's/^/  [chronicle] /'; then
  log "chronicle archive failed (non-fatal); see [chronicle] lines above"
fi

fi  # T-2829: end ack-outcome branch (archive only on a successful ack)

fi  # end SKIP_ACK guard (steps 2-4)

# ---------------------------------------------------------------------------
# T-1049 pause mode: handoff written + chronicle'd → STOP here (no restart).
# The agent is now finished + recorded + idle; the fleet orchestrator's machine
# reboot is what cycles it. Skips steps 5-8 (restart, is-active, steady-state,
# kick) entirely — the wrapper deliberately does NOT own systemctl in this mode.
# ---------------------------------------------------------------------------
if [[ "$NO_RESTART" == "1" ]]; then
  log "pause mode (--no-restart): handoff written + chronicle archived; skipping restart. Agent paused pending machine reboot."
  exit 0
fi

# ---------------------------------------------------------------------------
# T-1636 (the operator TG 3724) stage 1/2: STAGE the dying session's pane time-logs.
# A bare rename (same filesystem, atomic) — the live tap's awk holds the file's
# INODE, so every line the dying session prints from here to its death lands in
# the .dying file (nothing is lost); the successor's launcher re-arms a fresh
# tap at the original path via tools/arm_pane_log.sh. Runs on BOTH the ack and
# --skip-ack paths (review fix: an unconsumed log would blend two sessions'
# scrollback under the next archive's stem) but NOT in pause mode (session
# stays alive there — its tap must keep its file). Stage 2/2 gzips the staged
# file into the Chronicle AFTER the restart kills the session (file complete).
# Best-effort throughout: staging failure must never block the restart.
for _panelog in "${PANE_LOG_DIR:-$HOME/.local/log/pantheon}"/pane-timed-*.log; do
  [[ -e "$_panelog" ]] || continue
  if mv -- "$_panelog" "$_panelog.dying" 2>/dev/null; then
    log "pane time-log staged: ${_panelog##*/} -> ${_panelog##*/}.dying"
  else
    log "pane time-log staging failed for ${_panelog##*/} (non-fatal)"
  fi
done

# ---------------------------------------------------------------------------
# Step 5: systemctl restart.
# ---------------------------------------------------------------------------

# TARGET_PANE is "session:window" or "session:window.pane" — the session
# component is everything before the first ':'. Used by both the
# control-plane kill-session below and step 6's readiness predicate.
TARGET_SESSION="${TARGET_PANE%%:*}"

if [[ "$SUPERVISOR" == "control-plane" ]]; then
  # T-2658: kill the target session FIRST. runtime_controller.ensure_runtime()
  # (invoked by the consume unit below) REUSES a live matching pane rather
  # than restarting it — a plain `systemctl restart` of the consume unit
  # against a still-live pane would be a silent no-op, not a restart.
  # Companion units (e.g. specialist's acp-bridge, T-2648) are DELIBERATELY left
  # untouched: they hold their own tmux session on the same default socket,
  # so killing only this session neither tears down the shared tmux server
  # (when a companion session survives) nor requires special-casing when it
  # doesn't — ensure_runtime()/_start_runtime() already handle both "server
  # survives" (respawn onto the existing scope) and "server died" (recreate
  # the scope from scratch) generically.
  tmux kill-session -t "$TARGET_PANE" 2>/dev/null || true
  if tmux has-session -t "$TARGET_SESSION" 2>/dev/null; then
    err "control-plane session $TARGET_SESSION survived kill-session — refusing to fire $SERVICE against a still-live pane"
    exit 5
  fi
  log "control-plane: killed session $TARGET_SESSION pre-restart"
fi

if ! systemctl "$SYSTEMCTL_FLAG" restart "$SERVICE"; then
  err "systemctl $SYSTEMCTL_FLAG restart $SERVICE failed"
  exit 5
fi
log "service restart issued: $SERVICE ($MODE)"

# T-0256: append session-reset stats entry. Best-effort; non-zero exit from
# the helper is logged but not propagated (the restart already succeeded).
if ! python3 "$(dirname "$0")/session_reset_log.py" emit \
      --class handoff_restart \
      --trigger operator_initiated \
      --extras "service=$SERVICE" "target_pane=$TARGET_PANE" "mode=$MODE" \
      2>&1 | sed 's/^/  [sr_log] /'; then
  log "session_reset_log emit failed (non-fatal); see [sr_log] lines above"
fi

# T-1636 stage 2/2: the restart above killed the dying session (and its tap),
# so the staged .dying pane logs are COMPLETE — gzip each into the Chronicle as
# <ts>-<uuid>.pane.log.gz beside the handoff snapshot. Agent derived from the
# handoff PATH via argv (review fix: no inline-python splice — quote-safe, no
# injection surface); uuid/ts reuse the archive call's values on the ack path
# (self-pairing stem), "unknown-forced" on --skip-ack. rc captured directly
# (review fix: `if ! cmd | sed` tested sed's rc, making the error branch dead).
# Best-effort: a failed archive leaves the .dying file for the NEXT restart's
# stage-2 sweep (the *.log glob in stage 1 can't re-blend it).
for _staged in "${PANE_LOG_DIR:-$HOME/.local/log/pantheon}"/pane-timed-*.log.dying; do
  [[ -e "$_staged" ]] || continue
  _companion_out="$(python3 "$(dirname "$0")/chronicle.py" companion \
        --source "$_staged" \
        --handoff-file "$HANDOFF_FILE" \
        --ts "${CHRONICLE_TS:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}" \
        --uuid "${CHRONICLE_UUID:-unknown-forced}" \
        2>&1)"; _companion_rc=$?
  [[ -n "$_companion_out" ]] && printf '%s\n' "$_companion_out" | sed 's/^/  [chronicle] /'
  if [[ "$_companion_rc" -ne 0 ]]; then
    log "pane-log companion not archived for ${_staged##*/} (non-fatal; retried next restart)"
  fi
done

# ---------------------------------------------------------------------------
# Step 6: readiness fast-fail — systemctl is-active (bespoke) or runtime
# scope/session liveness (control-plane).
# ---------------------------------------------------------------------------

if [[ "$SUPERVISOR" == "control-plane" ]]; then
  # T-2658: $SERVICE is a Type=oneshot pantheon-persona-consume@<persona>
  # unit with no RemainAfterExit — it is NEVER is-active after ExecStart
  # exits, so `systemctl is-active` is the wrong witness here (ExecStart
  # already ran to completion by the time the BLOCKING `systemctl restart`
  # above returned — Type=oneshot's restart job waits for ExecStart to exit,
  # so persona_bringup.py consume -> wake_persistent_runtime ->
  # ensure_runtime's own internal pane/scope verification already ran; this
  # poll is a safety net, not the primary wait). The real witness is the
  # pane runtime_controller.ensure_runtime() started: a live target session
  # whose tmux server is cgroup-owned by the control-plane scope.
  scope_deadline=$(( $(date +%s) + STEADY_TIMEOUT ))
  scope_ok=0
  while (( $(date +%s) < scope_deadline )); do
    if runtime_scope_owned_and_session_live "$TARGET_SESSION" "$RUNTIME_SCOPE_UNIT" "$PROC_ROOT"; then
      scope_ok=1
      log "runtime scope/session live: $TARGET_SESSION under $RUNTIME_SCOPE_UNIT"
      break
    fi
    sleep "$POLL_INTERVAL"
  done
  if [[ "$scope_ok" != "1" ]]; then
    err "runtime scope/session-live timeout after ${STEADY_TIMEOUT}s; session=$TARGET_SESSION scope=$RUNTIME_SCOPE_UNIT"
    exit 6
  fi
else
  # is-active is the unit-level readiness signal: it goes "active" once the
  # ExecStart process is running. Doesn't tell us the app inside is ready —
  # step 7 covers that. But it fast-fails quickly if the unit's start failed
  # (e.g. ExecStart returned non-zero), saving us the full steady-state wait.
  isactive_deadline=$(( $(date +%s) + STEADY_TIMEOUT ))
  isactive_ok=0
  while (( $(date +%s) < isactive_deadline )); do
    state=$(systemctl "$SYSTEMCTL_FLAG" is-active "$SERVICE" 2>/dev/null || true)
    case "$state" in
      active)
        isactive_ok=1
        log "unit is-active: $state"
        break
        ;;
      failed)
        err "unit is-active: failed"
        exit 6
        ;;
      *)
        # activating, inactive, deactivating: keep polling
        ;;
    esac
    sleep "$POLL_INTERVAL"
  done
  if [[ "$isactive_ok" != "1" ]]; then
    err "is-active timeout after ${STEADY_TIMEOUT}s; last state: ${state:-unknown}"
    exit 6
  fi
fi

# ---------------------------------------------------------------------------
# Step 7: pane sentinel poll.
# ---------------------------------------------------------------------------

# Why a separate poll: is-active says "the process is running"; we still need
# "the app inside has come up and is showing its prompt." For Claude Code,
# the prompt frame renders as either corner-framed (╭─...─╮) or a bare
# horizontal run of ─ chars (observed on Claude Code v2.1.x, TERM/width-
# dependent). The default pattern covers both variants. Override via
# --verify-pattern.
steady_deadline=$(( $(date +%s) + STEADY_TIMEOUT ))
steady_ok=0
boot_dialog_dismissed=0
while (( $(date +%s) < steady_deadline )); do
  if pane=$(tmux capture-pane -p -t "$TARGET_PANE" 2>/dev/null); then
    # T-0172 (A) + T-0528: step-7 is a THREE-WAY conjunction.
    #   positive 1: VERIFY_PATTERN matches (prompt-frame chars present)
    #   positive 2: IDLE_SENTINEL matches  (idle-prompt cursor `❯` present)
    #   negative : DIALOG_EXCLUDE does NOT match (no `Enter to confirm` /
    #              `Esc to cancel` footer — the dev-channels confirm dialog
    #              reuses both the frame chars AND the `❯` glyph as its
    #              option cursor, so positives alone false-pass on it).
    # Empirical grounding (T-0528 capture, 90s × 1s): `❯` rendered every
    # frame from t+1s; older footer hints (`? for shortcuts`) did not.
    if printf '%s' "$pane" | grep -Eq "$VERIFY_PATTERN" \
       && printf '%s' "$pane" | grep -Eq "$IDLE_SENTINEL" \
       && ! printf '%s' "$pane" | grep -Eq "$DIALOG_EXCLUDE"; then
      steady_ok=1
      log "pane sentinel matched: frame='$VERIFY_PATTERN' idle='$IDLE_SENTINEL' (no dialog markers)"
      break
    fi
    # T-0721: tighter restart. If the ONLY thing blocking step-7 is the
    # dev-channels startup confirm (EXACT sentinel) and no dangerous confirm is
    # co-present, dismiss it inline ONCE (option 1) so the restart completes now
    # instead of waiting for the separate ~5-10min auto-resolve cron. Dismiss
    # ONLY — step-8 owns the single kickoff (so it doesn't fight this poll).
    # Single-shot (boot_dialog_dismissed) so we never re-press. Any OTHER dialog
    # (or denylisted marker co-present) → no dismiss → fall through to exit 7
    # (fail-closed). Per T-0721 adversarial review (safe shape C).
    # T-2210: two known boot modals, and they take DIFFERENT options — 1 for the
    # dev-channels confirm, 2 ("Not now") for the fullscreen-renderer offer, where
    # option 1 would accept a renderer change on this session. Kept symmetric with
    # verify_startup.sh, whose constants this file is byte-pinned to.
    boot_opt=""
    if printf '%s' "$pane" | grep -qF "$BOOT_DIALOG_SENTINEL"; then boot_opt=1
    elif printf '%s' "$pane" | grep -qF "$BOOT_RENDERER_SENTINEL"; then boot_opt=2; fi
    if (( ! boot_dialog_dismissed )) && [[ -n "$boot_opt" ]] \
       && ! printf '%s' "$pane" | grep -Eq "$BOOT_DISMISS_DENYLIST"; then
      log "step-7: known startup dialog detected — dismissing inline (option $boot_opt) [T-0721/T-2210]"
      send_keys_submit "$TARGET_PANE" "$boot_opt" \
        || log "step-7: boot-dialog dismiss send failed; will fall through to exit 7 if it persists"
      boot_dialog_dismissed=1
      sleep "$POLL_INTERVAL"
      continue
    fi
  fi
  sleep "$POLL_INTERVAL"
done
if [[ "$steady_ok" != "1" ]]; then
  err "steady-state timeout after ${STEADY_TIMEOUT}s; required '$VERIFY_PATTERN' AND '$IDLE_SENTINEL' AND NOT '$DIALOG_EXCLUDE' in pane"
  exit 7
fi

# Layer 0 (T-0584): post-sentinel PID-change assertion. The sentinel match
# means the unit's pane renders the idle prompt, but doesn't prove THIS
# claude is fresh. The legacy out-of-cgroup case (2026-05-25 14:46Z): step
# 5 fired `systemctl restart` against a unit whose inner claude was parented
# under the pre-cgroup user tmux server, so systemd couldn't reach it; the
# pre-restart session survived, sentinel matched, exit 0 — silent no-op.
# Comparing the inner-claude PID before vs. after detects that case.
# Skipped if no PRE-PID baseline was captured (no claude running, or test
# harness).
if [[ -n "$PRE_INNER_CLAUDE_PID" ]]; then
  POST_INNER_CLAUDE_PID=$(ps -u "$(id -u -n)" -o pid=,args= 2>/dev/null \
    | grep -E "$CLAUDE_PROCESS_PATTERN" | grep -v grep \
    | awk 'NR==1 {print $1}')
  if [[ -z "$POST_INNER_CLAUDE_PID" ]]; then
    err "post-restart inner-claude PID not found via ps; expected a fresh process (pre was $PRE_INNER_CLAUDE_PID)"
    exit 7
  fi
  if [[ "$POST_INNER_CLAUDE_PID" == "$PRE_INNER_CLAUDE_PID" ]]; then
    err "inner-claude PID unchanged after restart ($PRE_INNER_CLAUDE_PID); restart was a no-op — likely out-of-cgroup session (see 2026-05-25 incident)"
    exit 7
  fi
  log "PID-change verified: $PRE_INNER_CLAUDE_PID -> $POST_INNER_CLAUDE_PID"
fi

# ---------------------------------------------------------------------------
# Step 8: post-step-7 HANDOFF kick (T-0172 (B)).
# ---------------------------------------------------------------------------

# The SessionStart hook (handoff-auto-read.sh) injects "Read HANDOFF" as
# additionalContext, but additionalContext doesn't trigger a first-turn
# action — fresh-Orchestrator sits idle until an inbound message arrives. The
# kick is an explicit prompt that triggers the read + a Telegram surface
# so the operator sees fresh-Orchestrator come online.
#
# Best-effort: kick failure exits 8 but the handoff itself already
# succeeded structurally (sentinel matched). Operator can inject the
# read prompt manually if the kick failed.
if [[ "$KICK_ENABLED" == "1" ]]; then
  # T-0899: VERIFY-and-re-fire the kick instead of "settle, fire once, hope it
  # took". The 2026-06-06 silent kick-drop (1h46m Recovery silence): the step-8
  # send_keys_submit landed in the boot window before the fresh TUI input
  # listener was live, so the delivered turn never DISPATCHED, and the blind 3s
  # KICK_READINESS_SETTLE (T-0590) didn't prevent it. kick_verify.py drives the
  # tested deliver_kick_verified loop: settle -> inject (the shared
  # send_keys_submit.sh, single source of truth for tmux send-keys + submit-verify)
  # -> poll the pane for an actually-started turn ('esc to interrupt') -> bounded
  # re-fire if it dropped. Settle + poll window/interval + attempts are env-
  # overridable so tests run fast. Exit 8 only after re-fires exhaust with no
  # dispatched turn — the handoff itself already succeeded structurally (sentinel
  # matched), so the operator can inject manually.
  KICK_READINESS_SETTLE_SECS="${HANDOFF_RESTART_KICK_READINESS_SETTLE_SECS:-3}"
  KICK_POLL_SECS="${HANDOFF_RESTART_KICK_POLL_SECS:-12}"
  KICK_POLL_INTERVAL="${HANDOFF_RESTART_KICK_POLL_INTERVAL:-2}"
  KICK_MAX_ATTEMPTS="${HANDOFF_RESTART_KICK_MAX_ATTEMPTS:-3}"
  if /usr/bin/python3 "$SCRIPT_DIR/kick_verify.py" \
        --pane "$TARGET_PANE" --prompt "$KICK_PROMPT" \
        --settle "$KICK_READINESS_SETTLE_SECS" \
        --poll-secs "$KICK_POLL_SECS" \
        --poll-interval "$KICK_POLL_INTERVAL" \
        --max-attempts "$KICK_MAX_ATTEMPTS"; then
    log "post-restart kick delivered + dispatch-verified to $TARGET_PANE"
  else
    err "post-restart kick failed verification (no turn dispatched after bounded re-fires)"
    exit 8
  fi
else
  log "post-restart kick skipped (--no-kick)"
fi

log "handoff-and-restart cycle complete"
exit 0
