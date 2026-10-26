#!/bin/bash
# pantheon-seat-relaunch.sh — graceful ctxfill relaunch for a NOT-cut-over,
# runtime-controller-VALIDATED seat (mason/artisan). T-3838.
#
# WHY THIS EXISTS (and is not handoff_and_restart.sh): mason (suborch1) and
# artisan (suborch2) are NOT cut over to the control plane (T-3574 ratified
# disposition — characterized + not-cut-over, only specialist is cut over). They
# have NO restart_service: their interactive tmux session is created MANUALLY
# and merely VALIDATED (not owned/cycled) by pantheon-persona-consume@<persona>
# via runtime.json. Neither restart path fits:
#   * handoff_and_restart.sh (bespoke branch) does `systemctl restart $SERVICE`
#     — there is no service to restart.
#   * handoff_and_restart.sh (control-plane branch) resolves $SERVICE to
#     pantheon-persona-consume@<persona> = the Type=oneshot mailbox-drain that
#     silently kills auto-reset (the T-3098/T-3082 revert trap).
# So the ctxfill restart for these seats is a BESPOKE tmux-session-cycle: the
# SAME graceful primitive T-3829 used by hand to re-parent mason's session —
#   capture launch -> [fence consume timer] -> HANDOFF ack -> kill-server ->
#   systemd-run --user --scope respawn under the seat's OWN manager -> verify.
#
# ⚠️ THE CONSUME-TIMER FENCE (steps F1/F2 below) is the runtime-controller-
# ADJACENCY seam flagged in T-3838 and is pending orchestrator's ruling (D15 co-owned).
# FINDING (T-3838 DESIGN STEP 2, code-verified): the not-cut-over consume skip
# (RuntimeViabilityBlocked -> exit 5) is GATED ON PANE-LIVENESS —
# runtime_controller._block_if_live_target raises ONLY IF the pane is live; on a
# DEAD pane it returns, ensure_viable_runtime falls through to the viability-
# attempt loop -> ensure_runtime -> _start_runtime CREATE PATH. So a consume
# fire DURING the relaunch gap (pane killed, not yet respawned) would race this
# script's respawn = the T-3819 double-owner class. The fence stops/masks
# pantheon-persona-consume@<persona>.timer for the gap and restores it after a
# verified relaunch (leave-masked-on-failure = fail-safe). Only active when
# --consume-timer is passed; orchestrator may swap this for a controller-native guard.
#
# INERT until armed: context_fill_monitor keeps mason/artisan observation_only
# (no restart_bin derived, fire path refuses), so nothing invokes this script
# until the T-3838 ARM (orchestrator-reviewed) removes observation_only + provisions
# the pantheon-context-fill-monitor@{mason,artisan} timers.
#
# Runtime invariants (see .claude/rules/restart-pipeline-invariants.md):
#   * NO process-group-leadership assumption — kill via descendant-walk
#     (pkill -P), never a pgroup-kill.
#   * Ack-of-handoff is the HANDOFF.md mtime (the structural witness the seat
#     already produces by writing its handoff), NOT a flag file.
#   * Identity (session/window/pane/scope/uid) is an EXPLICIT parameter, never
#     auto-detected — auto-detect drift bites at the worst possible time. Only
#     the launch ARGV + cwd are captured live from the running session, because
#     for a not-cut-over seat they live in no canonical config (T-3829 did the
#     same); the capture FAILS LOUD rather than respawn a guessed command.
#   * ENV FIDELITY (limitation): the respawn restores argv + cwd + PANTHEON_TMUX_*
#     only; /proc/<pid>/environ is ptrace-blocked (artisan), so the rest of the env
#     is INHERITED from the (sudo -u, env_reset) invocation — the seat's standard
#     PATH (/usr/*bin:/snap/bin), which T-3829 proved sufficient for mason/artisan
#     (they are MAILBOX-FIRST, python3-MCP — no bun/telegram MCP that needs a
#     richer PATH). Step 7 verifies the idle prompt, NOT MCP/comms health, so the
#     mandatory per-seat live-verify at ARM time (T-3838) is the real comms gate.
#   * BLAST RADIUS: `tmux kill-server` tears down EVERY session on the seat's
#     default socket (required so the fresh scope owns a new server — a targeted
#     kill-session would leave the server in the wrong slice). mason/artisan run a
#     single session each (suborch{1,2}-main; verified 2026-09-20); a companion
#     session on that socket would be collateral. Re-verify single-session at ARM.
#
# Justifies-via: P1 (architecture over policy — one command serializes the
# graceful cycle via flock; the risky-drift identity fields are explicit
# params; the fence is the structural answer to a code-proven race). D1 (one
# trigger, no operator broker time). D15 (runtime-controller domain co-owned;
# the fence seam is surfaced for orchestrator, not silently decided).
#
# Exit codes (each is also the step it guards):
#   0 success; 1 lock; 2 capture (no launch argv); 3 prompt-send (session alive,
#   unfenced); 4 ack-timeout (session alive, unfenced); 5 kill-server survivor
#   (session refused to die — alive, unfenced); 6 respawn / pane-not-live (session
#   DEAD — left FENCED, fail-safe); 7 steady-state sentinel (session up but not
#   idle — left FENCED, fail-safe); 8 fence-restore failed; 9 fence-abort (in-flight
#   consume would not drain within the bound — SAFE no-op, session untouched,
#   nothing masked); 10 resume-kick not dispatched (T-4088: session live + unfenced
#   but IDLE — inject the handoff-read prompt by hand); 11 the OLD harness survived
#   kill-server + SIGTERM (T-4171: session dead, respawn refused, left FENCED);
#   12 --recover refused: the seat's session is LIVE (use the live-capture path;
#   nothing touched); 13 --record-argv could not persist the snapshot; 64 usage error;
#   75 the seat is in deep-sleep (ORG-106 launch-check; nothing touched, EX_TEMPFAIL).
#
# RECOVERY (T-4201): a relaunch that dies after kill-server (exit 6/11) leaves NO
# live pane, so a re-run cannot capture (exit 2). Every successful capture, and every
# verified step 7, persists the launch as the seat's last-good snapshot: ONE
# NUL-delimited file under $PANTHEON_STATE_DIR (record 0 = cwd, then argv), published
# by a single rename. `--recover` replays it, or `--launch-argv-file` replays a given
# argv-only file (with `--launch-cwd`), with the T-4085 role config still applied. It
# skips the handoff ack (there is no harness to ask) and keeps the fence, the T-4171
# harness-gone check, and the remain-on-exit diagnostics. Before the respawn it
# re-scans for ANY process still carrying the snapshot argv and refuses (exit 11) if
# one exists. It is still never a guessed command: the snapshot is validated (regular
# file, NUL-terminated, the seat's own for the persisted one).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=send_keys_submit.sh
source "$SCRIPT_DIR/send_keys_submit.sh"   # send_keys_submit(), INJECT_ENTER_DELAY, PENDING_PASTE_SENTINEL, SUBMIT_RETRIES

# ---------------------------------------------------------------------------
# Defaults (mirror handoff_and_restart.sh where the semantics are identical).
# ---------------------------------------------------------------------------
PERSONA=""                               # mason|artisan — for scope/timer naming + logging
ACCOUNT=""                               # suborch1|suborch2 — the seat's OS account (this script already runs AS it)
UID_ARG=""                               # numeric uid for XDG_RUNTIME_DIR; default: id -u
SESSION=""                               # tmux session name (e.g. suborch1-main) — EXPLICIT
WINDOW="harness"                         # tmux window name — EXPLICIT (matches live launch)
TARGET_PANE=""                           # tmux target pane (e.g. suborch1-main:0.0) — EXPLICIT
HANDOFF_FILE=""                          # /home/<account>/HANDOFF.md
EPISODE_START=0                          # T-4278: first dispatch epoch for retry ACKs
SCOPE_UNIT=""                            # transient scope unit (e.g. pantheon-mason-seat.scope) — EXPLICIT
CONSUME_TIMER=""                         # pantheon-persona-consume@<persona>.timer to fence (opt-in; the orchestrator seam)
LOCKFILE=""                              # default: /tmp/pantheon-seat-relaunch.<uid>.lock
ACK_TIMEOUT=600
QUEUED_ACK_TIMEOUT=1200                  # T-4117: ack window when the prompt QUEUED on a busy pane (fits the 1800s watchdog)
REPRESS_INTERVAL="${PANTHEON_SEAT_RELAUNCH_REPRESS_INTERVAL_SECS:-20}"  # T-4141: seconds between stuck-composer checks
REPRESS_MAX=2                            # T-4141: at most this many extra Enters per run (never a re-paste)
STEADY_TIMEOUT=90                       # a fresh scope+session+REPL boot is slower than a systemctl restart
POLL_INTERVAL=2
# T-4171: bound on the OLD harness exiting after kill-server (then SIGTERM, same bound
# again) before the respawn. Two claudes overlapping on one account killed artisan's new
# session at startup on 2026-09-28 (the old one outlived its tmux server by 3s).
OLD_HARNESS_EXIT_SECS="${PANTHEON_SEAT_RELAUNCH_OLD_HARNESS_EXIT_SECS:-20}"
FENCE_DRAIN_TIMEOUT="${FENCE_DRAIN_TIMEOUT:-30}"  # T-3846: max wait (s) for an in-flight consume run to drain at fence (env-overridable for tests)
FENCE_DRAIN_POLL="${FENCE_DRAIN_POLL:-2}"         # poll cadence (s) while draining an in-flight consume
SKIP_ACK=0
DISMISS_MODAL=0
HARD_RESTART_ON_ACK_TIMEOUT=0
DRY_RUN=0
RECOVER=0                                # T-4201: replay the last-good snapshot (no live pane)
LAUNCH_ARGV_FILE=""                      # T-4201: replay THIS NUL-delimited argv file instead (implies --recover)
LAUNCH_CWD=""                            # T-4201: cwd for --launch-argv-file (default /home/<account>/pantheon-config)
HARNESS_PID=""                           # T-4171: the OLD harness pid (capture, or the recovery orphan scan)
RECORD_ONLY=0                            # T-4201: capture + persist the snapshot, then exit (seeds recovery)
KICK_ENABLED=1                          # T-4088: post-verify resume kick (default on; --no-kick to disable)
KICK_PROMPT=""                           # default built at kick time (needs $HANDOFF_FILE + ack freshness)
# Step-7 sentinel conjunction (byte-identical to handoff_and_restart.sh:95). The
# `─` (3 bytes E2 94 80) MUST be wrapped in a group: under a non-UTF-8 (byte)
# locale a bare `─+`/`─{20,}` binds the quantifier to the trailing 0x80 only and
# NEVER matches a real box-drawing run. This runs via `sudo -u <seat>` (env_reset
# drops LANG/LC_*), so grep can land in the C locale — the exact T-2330 trap. Use
# the GROUPED form (identical semantics under UTF-8).
VERIFY_PATTERN='(╭(─)+╮|(─){20,})'
IDLE_SENTINEL='❯'
DIALOG_EXCLUDE='Enter to confirm|Esc to cancel'
BOOT_DIALOG_SENTINEL='I am using this for local development'
BOOT_RENDERER_SENTINEL='Try the new fullscreen renderer'
BOOT_DISMISS_DENYLIST='Do you want to (proceed|make this edit|overwrite|create|delete)|Bypass Permissions|Managed settings|don.?t ask again|Tab to amend|Exit plan mode|Ready to code|Use skill'
# Capture-only override seam (tests): read the launch argv from here instead of
# /proc, so the capture logic is exercisable without a live seat.
PROC_ROOT="${PANTHEON_SEAT_RELAUNCH_PROC_ROOT:-/proc}"

# T-3937: echo shared-cargo-target durability. Echo-building seats redirect cargo
# to the shared CARGO_TARGET_DIR (umask 002 so artifacts stay group-writable for
# peer-seat cache sharing, per T-3777) instead of regrowing a per-seat target/.
# Gate to echo-BUILDING personas only (per T-3593: specialist standing + artisan/orchestrator
# dispatched); a global CARGO_TARGET_DIR would misdirect ANY cargo build. In THIS
# script the only reachable personas are mason + artisan — artisan builds echo, mason
# does not — so the injection trips for artisan only here. (specialist is covered in
# runtime_controller.py; orchestrator via its service drop-in.) Overridable for tests.
ECHO_TARGET_DIR="${PANTHEON_ECHO_TARGET_DIR:-/var/lib/pantheon/echo-target}"
ECHO_BUILDER_PERSONAS="${PANTHEON_ECHO_BUILDER_PERSONAS:- artisan orchestrator specialist }"
is_echo_builder_persona() { [[ "$ECHO_BUILDER_PERSONAS" == *" $1 "* ]]; }

log() { echo "[seat-relaunch] $*"; }
err() { echo "[seat-relaunch] ERROR: $*" >&2; }

usage() {
  cat <<'EOF' >&2
Usage: pantheon-seat-relaunch.sh --persona <p> --session <s> --target-pane <p> --scope-unit <u> [options]

Required:
  --persona <mason|artisan>     seat persona (scope/timer naming, logging)
  --session <name>            tmux session name (e.g. suborch1-main)
  --target-pane <pane>        tmux target pane (e.g. suborch1-main:0.0)
  --scope-unit <unit>         transient --scope unit (e.g. pantheon-mason-seat.scope)
  --handoff-file <path>       /home/<account>/HANDOFF.md
  --episode-start <epoch>     first dispatch of this restart episode (default: 0)

Optional:
  --account <name>            seat OS account (default: current user)
  --uid <n>                   uid for XDG_RUNTIME_DIR (default: id -u)
  --window <name>             tmux window name (default: harness)
  --consume-timer <unit>      pantheon-persona-consume@<persona>.timer to fence for the
                              relaunch gap (opt-in). ⚠️ the T-3838 runtime-controller-
                              adjacency seam pending orchestrator's ruling. Absent: no fence.
  --lockfile <path>           default: /tmp/pantheon-seat-relaunch.<uid>.lock
  --ack-timeout <sec>         default: 600
  --queued-ack-timeout <sec>  default: 1200. T-4117: when the prompt lands on a BUSY pane
                              (submit verified still pending: Claude Code queues it mid-turn),
                              wait this long for the ack instead of exiting 3. The consume
                              fence stays up for the longer wait.
  --steady-timeout <sec>      default: 90
  --poll-interval <sec>       default: 2
  --skip-ack                  force path: skip handoff prompt + mtime ack
  --dismiss-modal             send one Escape before the handoff prompt (fail-closed)
  --hard-restart-on-ack-timeout  on ack timeout, relaunch anyway instead of exit 4
  --kick-prompt <text>        resume prompt injected into the fresh session (default:
                              read the handoff, drain mailbox, continue queued work)
  --no-kick                   skip the post-verify resume kick (T-4088)
  --recover                   T-4201: the seat has NO live pane (a relaunch died after
                              kill-server). Replay its last-good snapshot
                              ($PANTHEON_STATE_DIR/seat_relaunch_<persona>.last_launch), with role
                              config applied; skip the handoff ack. Refused (exit 12) unless
                              every pane of the session is dead or the session is gone.
  --launch-argv-file <path>   like --recover, but replay this NUL-delimited, NUL-terminated
                              argv-only file
  --launch-cwd <dir>          cwd for --launch-argv-file (default /home/<account>/pantheon-config;
                              never borrowed from the persisted snapshot)
  --record-argv               capture the live launch, persist it as the snapshot, and exit
                              (seeds recovery; touches nothing else; with --dry-run, writes nothing)
  --dry-run                   print plan; no side effects
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --persona)        PERSONA="$2"; shift 2 ;;
    --account)        ACCOUNT="$2"; shift 2 ;;
    --uid)            UID_ARG="$2"; shift 2 ;;
    --session)        SESSION="$2"; shift 2 ;;
    --window)         WINDOW="$2"; shift 2 ;;
    --target-pane)    TARGET_PANE="$2"; shift 2 ;;
    --handoff-file)   HANDOFF_FILE="$2"; shift 2 ;;
    --episode-start)  EPISODE_START="$2"; shift 2 ;;
    --scope-unit)     SCOPE_UNIT="$2"; shift 2 ;;
    --consume-timer)  CONSUME_TIMER="$2"; shift 2 ;;
    --lockfile)       LOCKFILE="$2"; shift 2 ;;
    --ack-timeout)    ACK_TIMEOUT="$2"; shift 2 ;;
    --queued-ack-timeout) QUEUED_ACK_TIMEOUT="$2"; shift 2 ;;
    --steady-timeout) STEADY_TIMEOUT="$2"; shift 2 ;;
    --poll-interval)  POLL_INTERVAL="$2"; shift 2 ;;
    --skip-ack)       SKIP_ACK=1; shift ;;
    --dismiss-modal)  DISMISS_MODAL=1; shift ;;
    --hard-restart-on-ack-timeout) HARD_RESTART_ON_ACK_TIMEOUT=1; shift ;;
    --kick-prompt)    KICK_PROMPT="$2"; shift 2 ;;
    --no-kick)        KICK_ENABLED=0; shift ;;
    --dry-run)        DRY_RUN=1; shift ;;
    --recover)        RECOVER=1; shift ;;
    --launch-argv-file|--launch-cwd)
      [[ -n "${2:-}" && "$2" != --* ]] || { err "$1 needs a value"; usage; exit 64; }
      if [[ "$1" == --launch-cwd ]]; then LAUNCH_CWD="$2"; else LAUNCH_ARGV_FILE="$2"; RECOVER=1; fi
      shift 2 ;;
    --record-argv)    RECORD_ONLY=1; shift ;;
    -h|--help)       usage; exit 0 ;;
    *)                err "unknown arg: $1"; usage; exit 64 ;;
  esac
done

if [[ -z "$PERSONA" || -z "$SESSION" || -z "$TARGET_PANE" || -z "$SCOPE_UNIT" || -z "$HANDOFF_FILE" ]]; then
  err "missing required arg (--persona --session --target-pane --scope-unit --handoff-file)"
  usage
  exit 64
fi
# T-4117: the queued path compares these arithmetically; a non-integer (e.g. '4s')
# would silently skip raising the ack window, so refuse it up front.
if [[ ! "$ACK_TIMEOUT" =~ ^[0-9]+$ || ! "$QUEUED_ACK_TIMEOUT" =~ ^[0-9]+$ || ! "$REPRESS_INTERVAL" =~ ^[0-9]+$ || ! "$EPISODE_START" =~ ^[0-9]+$ ]]; then
  err "--ack-timeout, --queued-ack-timeout, --episode-start and PANTHEON_SEAT_RELAUNCH_REPRESS_INTERVAL_SECS must be whole seconds"; exit 64
fi
# Identity discipline: the scope unit is persona-named, the session is
# account-named; a swapped pair is a config bug we refuse loudly rather than
# relaunch into the wrong cgroup.
if [[ ! "$SCOPE_UNIT" =~ ^pantheon-[a-z][a-z0-9_-]*-seat\.scope$ ]]; then
  err "scope-unit must match pantheon-<persona>-seat.scope; got '$SCOPE_UNIT'"
  exit 64
fi
if [[ -n "$CONSUME_TIMER" && ! "$CONSUME_TIMER" =~ ^pantheon-persona-consume@[a-z][a-z0-9_-]*\.timer$ ]]; then
  err "consume-timer must match pantheon-persona-consume@<persona>.timer; got '$CONSUME_TIMER'"
  exit 64
fi
if [[ "$RECORD_ONLY" == "1" && "$RECOVER" == "1" ]]; then
  err "--record-argv captures a LIVE seat; --recover/--launch-argv-file replay for a DEAD one — pick one"
  exit 64
fi
if [[ -n "$LAUNCH_CWD" && -z "$LAUNCH_ARGV_FILE" ]]; then
  err "--launch-cwd only applies to --launch-argv-file (the persisted snapshot carries its own cwd)"
  exit 64
fi

: "${UID_ARG:=$(id -u)}"
: "${ACCOUNT:=$(id -un)}"
: "${LOCKFILE:=/tmp/pantheon-seat-relaunch.${UID_ARG}.lock}"
export XDG_RUNTIME_DIR="/run/user/${UID_ARG}"
uctl() { systemctl --user "$@"; }

# ORG-106 L3a (T-5526): a deep-sleeping seat is never relaunched (--record-only only reads a
# live seat). Fail-open on a fault: the controller's launch_breach tripwire is the backstop.
if [[ "$RECORD_ONLY" != "1" ]]; then
  _lc_rc=0
  /usr/bin/python3 "$SCRIPT_DIR/seat_state.py" launch-check "$PERSONA" || _lc_rc=$?
  if [[ $_lc_rc -eq 3 ]]; then
    err "$PERSONA is in deep-sleep; refusing to relaunch (wake it first: harness $PERSONA wake)"
    exit 75   # EX_TEMPFAIL: ORG-106 deep-sleep, nothing touched. 3 is prompt-send, taken.
  fi
fi

# T-4201: the seat's last-good launch snapshot (fleet state-dir convention; the script
# runs AS the seat, so this is the seat's own ~/.local/state/pantheon).
STATE_DIR="${PANTHEON_STATE_DIR:-${HOME:-/home/$ACCOUNT}/.local/state/pantheon}"
SNAPSHOT_FILE="$STATE_DIR/seat_relaunch_${PERSONA}.last_launch"   # NUL-delimited: cwd, then argv
ARGV_SOURCE="captured live"

# ---------------------------------------------------------------------------
# Step 0 (pre-flight): CAPTURE the live launch snapshot BEFORE any disruption.
# argv + cwd only — identity is already explicit. Runs even under --dry-run so
# the plan shows what would be respawned. FAILS LOUD (exit 2) on empty argv:
# never respawn a guessed/empty command.
# ---------------------------------------------------------------------------
capture_launch() {
  # pane foreground process == the interactive claude (new-session exec'd
  # env -> claude, so pane_pid is claude post-exec; confirmed live 2026-09-20).
  local pane_pid
  pane_pid="$(tmux display-message -p -t "$TARGET_PANE" '#{pane_pid}' 2>/dev/null)"
  if [[ -z "$pane_pid" ]]; then
    err "capture: cannot resolve pane_pid for $TARGET_PANE (session not live?)"
    return 2
  fi
  local cmdline_file="$PROC_ROOT/$pane_pid/cmdline"
  if [[ ! -r "$cmdline_file" ]]; then
    err "capture: $cmdline_file not readable"
    return 2
  fi
  # NUL-delimited argv -> bash array. cmdline is readable same-user even where
  # /proc/<pid>/environ is ptrace-blocked (artisan), so we source identity/env
  # from tmux, argv from here.
  mapfile -d '' -t CAPTURED_ARGV < "$cmdline_file"
  HARNESS_PID="$pane_pid"                     # T-4171: the OLD harness, waited on before respawn
  CAPTURED_ARGV_RAW=("${CAPTURED_ARGV[@]}")   # its argv as captured: PID-reuse guard (CAPTURED_ARGV is re-derived later)
  if [[ ${#CAPTURED_ARGV[@]} -eq 0 || -z "${CAPTURED_ARGV[0]:-}" ]]; then
    err "capture: empty argv from $cmdline_file — refusing to respawn a guessed command"
    return 2
  fi
  # cwd from tmux (pane_current_path) — reliable even where /proc/<pid>/cwd
  # readlink is blocked; fall back to the account home's pantheon-config.
  CAPTURED_CWD="$(tmux display-message -p -t "$TARGET_PANE" '#{pane_current_path}' 2>/dev/null)"
  [[ -n "$CAPTURED_CWD" ]] || CAPTURED_CWD="/home/${ACCOUNT}/pantheon-config"
  log "captured launch: cwd=$CAPTURED_CWD argv=[${CAPTURED_ARGV[*]}]"
  return 0
}

# T-4171: is the OLD harness still running? kill-server kills the tmux server, but the
# harness lives in its own tmux-spawn-<uuid>.scope (tmux 3.5), so neither has-session
# nor stopping $SCOPE_UNIT proves it has exited. Same argv as captured = same process.
harness_alive() {
  [[ -n "${HARNESS_PID:-}" && -r "$PROC_ROOT/$HARNESS_PID/cmdline" ]] || return 1
  cmdline_is_raw_argv "$PROC_ROOT/$HARNESS_PID/cmdline"
}

# cmdline_is_raw_argv <cmdline-file>: does it hold EXACTLY CAPTURED_ARGV_RAW, element by
# element? (T-4201: a space-joined compare would equate ["a b","c"] with ["a","b c"].)
cmdline_is_raw_argv() {
  local now=() i
  mapfile -d '' -t now < "$1" 2>/dev/null || return 1
  (( ${#now[@]} == ${#CAPTURED_ARGV_RAW[@]} && ${#now[@]} > 0 )) || return 1
  for (( i=0; i<${#now[@]}; i++ )); do
    [[ "${now[i]}" == "${CAPTURED_ARGV_RAW[i]}" ]] || return 1
  done
  return 0
}

# T-4171: the respawn sets remain-on-exit, so a harness that dies at startup leaves a
# DEAD pane holding its last output, instead of a session that silently vanishes.
pane_is_dead() { [[ "$(tmux display-message -p -t "$TARGET_PANE" '#{pane_dead}' 2>/dev/null)" == "1" ]]; }
die_on_dead_pane() {
  err "respawn: the new harness EXITED at startup; its last pane output:"
  tmux capture-pane -p -t "$TARGET_PANE" -S -40 2>/dev/null | grep -v '^[[:space:]]*$' | tail -25 | sed 's/^/[seat-relaunch]   | /' >&2
  tmux kill-server 2>/dev/null || true   # keep the failure semantics: session DEAD
  fail_masked 6
}

# ---------------------------------------------------------------------------
# T-4201: last-good snapshot + recovery. The snapshot is ONE NUL-delimited file
# (record 0 = cwd, records 1.. = argv), so the pair publishes in a single rename:
# mktemp in the state dir (no predictable tmp name), fsync, then mv. The relaunch path
# only WARNS on a failed write (never block a relaunch on a state file); --record-argv
# exits 13 on one.
# ---------------------------------------------------------------------------
persist_launch() {  # <what> <cwd> <argv...>
  local what="$1" cwd="$2" tmp=""; shift 2
  if { mkdir -p "$STATE_DIR" \
       && tmp="$(mktemp "$STATE_DIR/.seat_relaunch_${PERSONA}.XXXXXX")" \
       && printf '%s\0' "$cwd" "$@" > "$tmp" \
       && { sync -- "$tmp" || true; } \
       && mv -fT -- "$tmp" "$SNAPSHOT_FILE"; } 2>/dev/null; then
    log "snapshot: persisted the $what launch -> $SNAPSHOT_FILE"
    return 0
  fi
  [[ -n "$tmp" ]] && rm -f -- "$tmp" 2>/dev/null
  log "WARNING snapshot: could not persist the $what launch to $SNAPSHOT_FILE — a later --recover would lack it"
  return 1
}

# read_nul_records <file> <array-name>: refuse anything but a regular, non-symlink,
# NUL-TERMINATED file (a truncated or newline-delimited file is garbage, not argv).
read_nul_records() {
  local f="$1"
  if [[ ! -f "$f" || -L "$f" || ! -r "$f" ]]; then
    err "recover: $f is missing, not a regular file, or unreadable (every live capture persists a snapshot; seed one with --record-argv while the seat is live) — refusing to respawn a guessed command"
    return 2
  fi
  if [[ ! -s "$f" || -n "$(tail -c1 -- "$f" | tr -d '\0')" ]]; then
    err "recover: $f is empty or not NUL-terminated (truncated?) — refusing to respawn a guessed command"
    return 2
  fi
  mapfile -d '' -t "$2" < "$f"
}

load_launch_snapshot() {
  local recs=()
  if [[ -n "$LAUNCH_ARGV_FILE" ]]; then
    read_nul_records "$LAUNCH_ARGV_FILE" CAPTURED_ARGV || return 2
    CAPTURED_CWD="${LAUNCH_CWD:-/home/${ACCOUNT}/pantheon-config}"
    ARGV_SOURCE="argv file $LAUNCH_ARGV_FILE, cwd $CAPTURED_CWD (recovery)"
  else
    read_nul_records "$SNAPSHOT_FILE" recs || return 2
    if [[ ! -O "$SNAPSHOT_FILE" ]]; then
      err "recover: $SNAPSHOT_FILE is not owned by $(id -un) — refusing to replay it"
      return 2
    fi
    CAPTURED_CWD="${recs[0]}"
    CAPTURED_ARGV=("${recs[@]:1}")
    [[ -n "$CAPTURED_CWD" ]] || CAPTURED_CWD="/home/${ACCOUNT}/pantheon-config"
    ARGV_SOURCE="snapshot $SNAPSHOT_FILE (recovery)"
  fi
  if [[ ${#CAPTURED_ARGV[@]} -eq 0 || -z "${CAPTURED_ARGV[0]:-}" ]]; then
    err "recover: empty argv in the snapshot — refusing to respawn a guessed command"
    return 2
  fi
  CAPTURED_ARGV_RAW=("${CAPTURED_ARGV[@]}")
  log "recover: replaying $ARGV_SOURCE: argv=[${CAPTURED_ARGV[*]}]"
  return 0
}

# A harness from the failed relaunch can outlive its tmux server (reparented to init;
# T-4171, artisan 2026-09-28). With no pane there is no captured pid, so find it by the
# SAME exact-argv identity harness_alive() uses, among this account's own processes.
# The T-4171 wait / SIGTERM then targets the first match; the pre-respawn re-scan
# (orphans_remain) refuses if that one, or any duplicate, is still there.
find_orphan_harness() {
  local d
  HARNESS_PID=""
  for d in "$PROC_ROOT"/[0-9]*; do
    [[ -O "$d" && -r "$d/cmdline" && "${d##*/}" != "$$" ]] || continue
    if cmdline_is_raw_argv "$d/cmdline"; then
      HARNESS_PID="${d##*/}"
      log "recover: a harness with the snapshot argv is still running (pid $HARNESS_PID) — it must exit before the respawn (T-4171)"
      return 0
    fi
  done
  log "recover: no harness with the snapshot argv is running"
  return 0
}
orphans_remain() {  # recovery only: any process still carrying the snapshot argv?
  find_orphan_harness >/dev/null
  [[ -n "$HARNESS_PID" ]]
}

recover_preflight() {
  # NO live pane anywhere in the session: every pane must REPORT dead. A session
  # whose panes cannot be listed is "cannot tell", which is not "dead": refuse.
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    local panes
    panes="$(tmux list-panes -s -t "$SESSION" -F '#{pane_dead}' 2>/dev/null)"
    if [[ -z "$panes" ]] || grep -qv '^1$' <<<"$panes"; then
      err "recover: session $SESSION exists with a live (or unlistable) pane — refusing to replay a snapshot over a running seat; run without --recover/--launch-argv-file (the live-capture path)"
      return 12
    fi
    log "recover: session $SESSION holds only DEAD panes — kill-server will clear it"
  fi
  load_launch_snapshot || return 2
  find_orphan_harness
  SKIP_ACK=1
  log "recover: no live harness to ask for a handoff — skipping the prompt + ack (the successor is told its handoff may be stale)"
  return 0
}

if [[ "$RECOVER" == "1" ]]; then
  recover_preflight; rc=$?
  (( rc == 0 )) || exit "$rc"
else
  if ! capture_launch; then
    exit 2
  fi
  if [[ "$RECORD_ONLY" == "1" ]]; then
    if [[ "$DRY_RUN" == "1" ]]; then
      log "dry-run: would persist cwd=$CAPTURED_CWD argv=[${CAPTURED_ARGV_RAW[*]}] -> $SNAPSHOT_FILE"
      exit 0
    fi
    persist_launch "captured" "$CAPTURED_CWD" "${CAPTURED_ARGV_RAW[@]}" || exit 13
    exit 0
  fi
fi

# ---------------------------------------------------------------------------
# T-4085: re-derive --model / --effort from the seat's ROLE config. Replaying the
# captured argv verbatim made a relaunch a context refresh, never a config reload,
# so an instance.yaml model change (T-4067: opus-4-8 -> 5-5) could not reach a
# not-cut-over seat without a hand launch. seat_launch_config.py reads the same
# keys the harness adapter uses for controller-launched seats (role.claude.model,
# role.claude.native.effort). A key the role does not declare, or any resolve
# failure, keeps the captured value — never respawn a guessed command. Idempotent
# across cycles: the next capture reads the already-rewritten argv.
# ---------------------------------------------------------------------------
INSTANCE_YAML="${PANTHEON_SEAT_RELAUNCH_INSTANCE_YAML:-$SCRIPT_DIR/../instance.yaml}"

# argv_set_flag <flag> <value>: set <flag>'s value in CAPTURED_ARGV. Rewrites every
# `<flag> v` and `<flag>=v` occurrence before a `--` terminator; inserts
# `<flag> <value>` right after argv[0] when absent. Logs the substitution loudly.
argv_set_flag() {
  local flag="$1" value="$2" i n=${#CAPTURED_ARGV[@]} found=0 old=""
  for (( i=1; i<n; i++ )); do
    [[ "${CAPTURED_ARGV[i]}" == "--" ]] && break
    if [[ "${CAPTURED_ARGV[i]}" == "$flag" ]] && (( i + 1 < n )); then
      old="${CAPTURED_ARGV[i+1]}"; CAPTURED_ARGV[i+1]="$value"; found=1; (( i++ ))
    elif [[ "${CAPTURED_ARGV[i]}" == "$flag="* ]]; then
      old="${CAPTURED_ARGV[i]#*=}"; CAPTURED_ARGV[i]="$flag=$value"; found=1
    fi
  done
  if (( ! found )); then
    CAPTURED_ARGV=( "${CAPTURED_ARGV[0]}" "$flag" "$value" "${CAPTURED_ARGV[@]:1}" )
    log "launch config: argv had no $flag — inserted $flag $value from instance.yaml"
  elif [[ "$old" != "$value" ]]; then
    log "launch config: argv $flag $old -> $value from instance.yaml"
  else
    log "launch config: argv $flag $value already matches instance.yaml"
  fi
}

apply_role_launch_config() {
  local out rc line cfg_model="" cfg_effort=""
  out="$(/usr/bin/python3 "$SCRIPT_DIR/seat_launch_config.py" --persona "$PERSONA" --instance "$INSTANCE_YAML" 2>&1)"
  rc=$?
  if (( rc != 0 )); then
    log "WARNING launch config: cannot resolve $PERSONA's role in $INSTANCE_YAML (rc=$rc: ${out//$'\n'/ }) — keeping captured argv"
    return 0
  fi
  while IFS= read -r line; do
    case "$line" in
      model=*)  cfg_model="${line#model=}" ;;
      effort=*) cfg_effort="${line#effort=}" ;;
    esac
  done <<<"$out"
  if [[ -n "$cfg_model" ]]; then argv_set_flag --model "$cfg_model"
  else log "launch config: $PERSONA's role declares no claude.model — keeping captured --model"; fi
  [[ -n "$cfg_effort" ]] && argv_set_flag --effort "$cfg_effort"
  log "launch argv after role config: [${CAPTURED_ARGV[*]}]"
  return 0
}
apply_role_launch_config

# ---------------------------------------------------------------------------
# T-3937: build the shared-cargo-target launcher wrapper for echo-building seats.
# The wrapper runs BEFORE the captured claude and, in ONE place, (a) sets umask 002
# so build artifacts stay group-writable, and (b) exports CARGO_TARGET_DIR — but
# only if the shared cache is writable, falling through cleanly to the per-seat
# default otherwise (preserves T-3777's fleet-optional property). It then `exec`s
# claude IN PLACE, so the pane foreground process image stays the bare claude argv:
# capture_launch on the NEXT relaunch reads that image, NOT the wrapper, so the
# wrapper re-applies fresh each cycle and never accumulates (the fleet-critical
# capture-respawn safety property). The shared target dir is passed as data ($1),
# never string-interpolated into the sh script. Empty array (non-echo persona) =>
# the respawn is byte-identical to the pre-T-3937 command.
CARGO_WRAP=()
if is_echo_builder_persona "$PERSONA"; then
  CARGO_WRAP=( sh -c 'umask 002; if [ -d "$1" ] && [ -w "$1" ] && [ -x "$1" ]; then export CARGO_TARGET_DIR="$1"; fi; shift; exec "$@"' _ "$ECHO_TARGET_DIR" )
  log "echo-builder $PERSONA: wrapping respawn with umask 002 + CARGO_TARGET_DIR=$ECHO_TARGET_DIR (if writable)"
fi

if [[ "$DRY_RUN" == "1" ]]; then
  cat <<EOF
DRY RUN — no side effects.
  persona:        $PERSONA   account: $ACCOUNT (uid $UID_ARG)
  session:        $SESSION   window: $WINDOW   pane: $TARGET_PANE
  scope unit:     $SCOPE_UNIT
  consume timer:  ${CONSUME_TIMER:-<none — no fence>}
  handoff file:   $HANDOFF_FILE
  skip ack:       $SKIP_ACK   ack timeout: ${ACK_TIMEOUT}s   steady: ${STEADY_TIMEOUT}s
  resume kick:    $KICK_ENABLED
  captured cwd:   $CAPTURED_CWD
  argv source:    $ARGV_SOURCE
  launch argv:    ${CAPTURED_ARGV[*]}   (captured, then role config applied — T-4085)
  old harness:    pid ${HARNESS_PID:-?} — wait <=${OLD_HARNESS_EXIT_SECS}s after kill-server, SIGTERM, then refuse (exit 11) (T-4171)
  respawn:        systemd-run --user --scope --unit=$SCOPE_UNIT tmux start-server \; set-option -g remain-on-exit on \; new-session -d -s $SESSION -n $WINDOW -c $CAPTURED_CWD \\
                    /usr/bin/env -- PANTHEON_TMUX_SESSION=$SESSION PANTHEON_TMUX_WINDOW=$WINDOW PANTHEON_TMUX_PANE=0 ${CARGO_WRAP[*]:+${CARGO_WRAP[*]} }${CAPTURED_ARGV[*]}
EOF
  exit 0
fi

# T-4201: the live capture is what was running — the seat's last-good launch. Persist
# it before any disruption, so a relaunch that dies after kill-server is recoverable.
if [[ "$RECOVER" != "1" ]]; then
  persist_launch "captured" "$CAPTURED_CWD" "${CAPTURED_ARGV_RAW[@]}" || true
fi

# ---------------------------------------------------------------------------
# Wall-clock watchdog (T-0584 Layer 1, descendant-walk kill — no pgroup
# assumption per .claude/rules/restart-pipeline-invariants.md). Backstops a
# hung `tmux send-keys` child so it can never hold the flock indefinitely.
# ---------------------------------------------------------------------------
WALL_CLOCK_TIMEOUT_SECS="${PANTHEON_SEAT_RELAUNCH_WALL_CLOCK_SECS:-1800}"
SCRIPT_PID=$$
(
  sleep "$WALL_CLOCK_TIMEOUT_SECS" &
  SLEEP_PID=$!
  trap 'kill "$SLEEP_PID" 2>/dev/null; wait "$SLEEP_PID" 2>/dev/null; exit 0' TERM
  wait "$SLEEP_PID"
  kill -0 "$SCRIPT_PID" 2>/dev/null || exit 0
  echo "[seat-relaunch] FATAL: wall-clock ${WALL_CLOCK_TIMEOUT_SECS}s exceeded; killing $SCRIPT_PID + descendants" >&2
  pkill -TERM -P "$SCRIPT_PID" 2>/dev/null || true
  kill -TERM "$SCRIPT_PID" 2>/dev/null || true
  sleep 5
  pkill -KILL -P "$SCRIPT_PID" 2>/dev/null || true
  kill -KILL "$SCRIPT_PID" 2>/dev/null || true
) &
WATCHDOG_PID=$!
# T-4171: ROE_SET=1 while the respawn's remain-on-exit is on; never leave it on.
trap 'kill "$WATCHDOG_PID" 2>/dev/null; wait "$WATCHDOG_PID" 2>/dev/null; [[ "${ROE_SET:-0}" == 1 ]] && tmux set-option -gu remain-on-exit 2>/dev/null; true' EXIT

# ---------------------------------------------------------------------------
# Step 1: flock (per-uid; implicit release on exit). Guard the OPEN separately
# from the LOCK so an EPERM is not misreported as contention.
# ---------------------------------------------------------------------------
if ! ( true >>"$LOCKFILE" ) 2>/dev/null; then
  err "cannot open lockfile $LOCKFILE (permission/path, not contention)"
  exit 1
fi
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  err "lock $LOCKFILE held by another instance"
  exit 1
fi

# ---------------------------------------------------------------------------
# Step F1: FENCE the consume timer for the relaunch gap (opt-in; orchestrator seam).
# Stop + mask so neither the timer nor a manual `start` can fire a consume
# (create-path race) while the pane is dead. Then DRAIN any in-flight consume run
# (T-3846) BEFORE we kill the session — an in-flight consume mid-relaunch running
# its controller AFTER our respawn is the exact collision we are fencing against.
# ---------------------------------------------------------------------------
FENCED=0
fence_consume_timer() {
  [[ -z "$CONSUME_TIMER" ]] && { log "no --consume-timer: skipping fence"; return 0; }
  local svc="${CONSUME_TIMER%.timer}.service"
  # Order (orchestrator adjacency review, nit 1 — close the check-then-start race): MASK
  # FIRST, THEN check is-active. Masking the SERVICE (not just the timer — the
  # timer alone leaves the service startable by any non-timer activation) blocks
  # EVERY new start; stop+mask the timer too so a scheduled fire does not log a
  # failed trigger. THEN the is-active check catches a consume that started in the
  # narrow window just BEFORE the mask took hold — and if one did, ABORT and fully
  # UNFENCE (a fence-abort must leave nothing masked). Checking BEFORE masking (the
  # old order) left a ms-scale window where a timer fire between check and mask
  # produced a new start that then raced the kill-server. The fence covers the
  # WHOLE relaunch (ack -> kill-server -> respawn), not merely the dead-pane gap.
  log "fence: mask $svc + stop/mask $CONSUME_TIMER for the whole relaunch"
  if ! uctl mask "$svc" 2>/dev/null; then
    err "fence: failed to mask $svc — refusing to proceed (the create-path race would be unfenced)"
    return 1
  fi
  FENCED=1   # set ONLY after the load-bearing service mask succeeds
  uctl stop "$CONSUME_TIMER" 2>/dev/null || true
  uctl mask "$CONSUME_TIMER" 2>/dev/null || true
  # DRAIN an in-flight consume run (T-3846). persona-consume@ is Type=oneshot, so a
  # running instance sits in ActiveState=activating and NEVER reaches "active" —
  # the old `is-active --quiet` check (matches only "active") never caught a running
  # oneshot, so a consume the timer fired in the same instant the mask took hold
  # (artisan 2026-09-21: timer fire raced the fence) ran its controller AFTER our
  # respawn, hit _prepare_runtime_scope's active-scope refusal, and blanked
  # runtime.json (the T-3819 teardown). The service is now masked (no NEW run can
  # start), so we WAIT for the in-flight run to finish: it was fired against the
  # still-live OLD session, so letting it drain BEFORE kill-server is harmless (it
  # rehydrates the matching old state — no teardown). Then kill+respawn with no
  # consume in flight. Only if it will not drain within the bound do we abort +
  # fully unfence (a fence-abort must leave nothing masked).
  local waited=0 astate
  while :; do
    astate="$(uctl show "$svc" -p ActiveState --value 2>/dev/null)"
    [[ "$astate" != "active" && "$astate" != "activating" ]] && break
    if (( waited >= FENCE_DRAIN_TIMEOUT )); then
      err "fence: $svc still '$astate' after ${FENCE_DRAIN_TIMEOUT}s — in-flight consume won't drain; aborting + unfencing rather than race it at kill-server"
      unfence_consume_timer || true
      return 1
    fi
    log "fence: consume $svc in-flight ('$astate') — draining before respawn (${waited}s/${FENCE_DRAIN_TIMEOUT}s)"
    sleep "$FENCE_DRAIN_POLL"
    waited=$(( waited + FENCE_DRAIN_POLL ))
  done
  return 0
}
unfence_consume_timer() {
  [[ "$FENCED" == "1" ]] || return 0
  local svc="${CONSUME_TIMER%.timer}.service" rc=0
  log "unfence: unmask $svc + unmask/start $CONSUME_TIMER"
  uctl unmask "$svc"           2>/dev/null || { err "unfence: unmask $svc failed"; rc=1; }
  uctl unmask "$CONSUME_TIMER" 2>/dev/null || { err "unfence: unmask timer failed"; rc=1; }
  uctl start  "$CONSUME_TIMER" 2>/dev/null || { err "unfence: start timer failed"; rc=1; }
  [[ "$rc" == "0" ]] && FENCED=0
  return "$rc"
}
# Fail-safe exit for a DEAD-session failure: leave the fence UP (do NOT let the
# controller auto-create into a half-relaunched seat — the T-3819 mode) but
# always tell the operator/monitor exactly how to reconcile.
fail_masked() {
  local code="$1"
  if [[ "$FENCED" == "1" ]]; then
    local svc="${CONSUME_TIMER%.timer}.service"
    err "consume LEFT FENCED (fail-safe). Reconcile after fixing $PERSONA: systemctl --user unmask $svc $CONSUME_TIMER && systemctl --user start $CONSUME_TIMER"
  fi
  exit "$code"
}
# Exit 9 (NOT 5): fence-abort is a SAFE no-op — an in-flight consume would not
# drain within the bound, session untouched, nothing masked. Distinct from exit 5
# (kill-server survivor = a session that refused to die), benign vs dangerous.
if ! fence_consume_timer; then
  exit 9
fi

# ---------------------------------------------------------------------------
# Steps 2-4: handoff prompt + mtime ack (skipped under --skip-ack).
# ---------------------------------------------------------------------------
PROMPT_TEXT="[ctxfill graceful relaunch] Please write a fresh $HANDOFF_FILE capturing your live state (active ticket, in-flight arc, next actions) NOW — your session is about to be cycled to reclaim context. Finish the current thought, write the handoff, and stop; a successor will read it on boot. Rewrite the file even if nothing changed since your last handoff: the restart waits for the file's modification time, not for your reply."
HANDOFF_FRESH=0   # T-4088: 1 only on a witnessed ack — the resume kick warns the successor otherwise

if [[ "$SKIP_ACK" == "1" ]]; then
  log "skip-ack: bypassing handoff prompt + ack-wait (force path)"
else
  if [[ ! -f "$HANDOFF_FILE" ]]; then PRE_MTIME=0
  else PRE_MTIME=$(stat -c %Y "$HANDOFF_FILE" 2>/dev/null || echo 0); fi
  log "pre-prompt mtime: $PRE_MTIME ($HANDOFF_FILE)"

  if [[ "$DISMISS_MODAL" == "1" ]]; then
    log "dismiss-modal: Escape -> $TARGET_PANE (fail-closed)"
    tmux send-keys -t "$TARGET_PANE" Escape 2>/dev/null || log "dismiss-modal: send failed (non-fatal)"
    sleep "$POLL_INTERVAL"
  fi

  # T-4117: rc 2 = delivered but verified still PENDING. A busy pane queues the
  # input and runs it when the current turn ends, so wait for the ack on the
  # longer window instead of exiting 3. The prompt is never re-sent.
  submit_rc=0
  send_keys_submit "$TARGET_PANE" "$PROMPT_TEXT" || submit_rc=$?
  if [[ "$submit_rc" == "2" ]]; then
    if (( QUEUED_ACK_TIMEOUT > ACK_TIMEOUT )); then ACK_TIMEOUT="$QUEUED_ACK_TIMEOUT"; fi
    log "prompt queued on $TARGET_PANE (submit unproven or still pending: busy pane or blank capture, T-5412) — waiting up to ${ACK_TIMEOUT}s for the ack [T-4117]"
  elif [[ "$submit_rc" != "0" ]]; then
    err "handoff prompt delivery/submit failed"
    unfence_consume_timer || true
    exit 3
  else
    log "prompt delivered to $TARGET_PANE"
  fi

  ack_deadline=$(( $(date +%s) + ACK_TIMEOUT ))
  ack_ok=0
  # T-4141: prompt still in the composer of a no-longer-busy pane -> one more Enter
  # (bounded, never a re-paste; repress_if_stuck holds on busy/bg-agent/modal panes).
  repress_left=$REPRESS_MAX
  next_repress=$(( $(date +%s) + REPRESS_INTERVAL ))
  while (( $(date +%s) < ack_deadline )); do
    cur=$(stat -c %Y "$HANDOFF_FILE" 2>/dev/null || echo 0)
    if [[ "$cur" -gt 0 ]] && { [[ "$cur" != "$PRE_MTIME" ]] || (( EPISODE_START > 0 && cur > EPISODE_START )); }; then
      ack_ok=1; HANDOFF_FRESH=1; log "handoff ack: mtime $PRE_MTIME -> $cur (episode-start=$EPISODE_START)"; break
    fi
    if (( repress_left > 0 && $(date +%s) >= next_repress )); then
      next_repress=$(( $(date +%s) + REPRESS_INTERVAL ))
      if repress_if_stuck "$TARGET_PANE" "$PROMPT_TEXT"; then
        repress_left=$(( repress_left - 1 ))
        log "re-pressed Enter on $TARGET_PANE: pane not busy, handoff prompt still in the composer [T-4141]"
      fi
    fi
    sleep "$POLL_INTERVAL"
  done
  if [[ "$ack_ok" != "1" ]]; then
    if [[ "$HARD_RESTART_ON_ACK_TIMEOUT" == "1" ]]; then
      err "ack timeout ${ACK_TIMEOUT}s; HARD-RESTART fallback (relaunching a wedged session)"
    else
      err "ack timeout ${ACK_TIMEOUT}s; HANDOFF.md mtime unchanged"
      unfence_consume_timer || true
      exit 4
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Step 5: kill the seat's tmux server (removes the old session from its scope),
# then confirm the session is gone. A survivor means we must NOT respawn (a
# second new-session would collide) — abort with the fence still up (fail-safe).
# ---------------------------------------------------------------------------
log "kill-server: tearing down $SESSION's tmux server"
tmux kill-server 2>/dev/null || true
sleep "$POLL_INTERVAL"
if tmux has-session -t "$SESSION" 2>/dev/null; then
  err "kill-server: session $SESSION survived — refusing to respawn into a live session"
  # The OLD session is still ALIVE, so restore its validation timer (do NOT
  # leave it fenced — unlike a dead-session failure, there is a live seat to
  # cover). exit 5 = "session refused to die" (dangerous), distinct from exit 9.
  unfence_consume_timer || true
  exit 5
fi
# T-3838 review #3: kill-server kills the tmux SERVER, but claude (its child) is
# reparented to init and STAYS in the old <scope> cgroup until it actually exits.
# has-session going false only proves the socket is gone, not that the cgroup is
# empty — so re-creating the SAME-named scope can fail "Unit already exists".
# Explicitly stop the old scope (SIGTERMs the whole cgroup, incl. the reparented
# claude) and wait until it is no longer active before reusing the name.
uctl stop "$SCOPE_UNIT" 2>/dev/null || true
scope_gone_deadline=$(( $(date +%s) + STEADY_TIMEOUT ))
while (( $(date +%s) < scope_gone_deadline )); do
  [[ "$(uctl is-active "$SCOPE_UNIT" 2>/dev/null)" != "active" ]] && break
  sleep "$POLL_INTERVAL"
done
uctl reset-failed "$SCOPE_UNIT" 2>/dev/null || true   # clear lingering state so systemd-run can reuse the unit name

# T-4171: wait for the OLD harness itself to exit. It flushes its session and
# ~/.claude.json on the SIGHUP, and a new harness started into that overlap died at
# startup (artisan, 2026-09-28 19:15Z). Bounded; SIGTERM once if it lingers; if it
# survives even that, refuse to put a second harness on the account.
harness_deadline=$(( $(date +%s) + OLD_HARNESS_EXIT_SECS ))
while harness_alive && (( $(date +%s) < harness_deadline )); do sleep "$POLL_INTERVAL"; done
if harness_alive; then
  log "old harness pid $HARNESS_PID still alive ${OLD_HARNESS_EXIT_SECS}s after kill-server — SIGTERM"
  env kill -TERM "$HARNESS_PID" 2>/dev/null || true
  harness_deadline=$(( $(date +%s) + OLD_HARNESS_EXIT_SECS ))
  while harness_alive && (( $(date +%s) < harness_deadline )); do sleep "$POLL_INTERVAL"; done
fi
if harness_alive; then
  err "old harness pid $HARNESS_PID survived kill-server + SIGTERM — refusing to start a second harness on this account"
  fail_masked 11
fi
log "old harness pid ${HARNESS_PID:-?} has exited"
# T-4201: under recovery the seat is already dead, so refusing costs little. Last check
# right before the respawn: if ANY process still carries the snapshot argv (a duplicate
# orphan, or one that appeared since the preflight), do not start another beside it.
if [[ "$RECOVER" == "1" ]] && orphans_remain; then
  err "recover: pid $HARNESS_PID still carries the snapshot argv at respawn time — refusing to start a second harness on this account"
  fail_masked 11
fi

# ---------------------------------------------------------------------------
# Step 5b (T-5016): archive the session that just ended to the Chronicle, through
# the same session-end duty the runtime controller runs (one record per session
# end, claim-deduplicated). The old harness has exited and kill-server closed its
# pane-log tap, so the log is complete; --stage renames it so the respawned
# session's tap (re-armed in step 6) starts a fresh file. Bounded and non-fatal:
# a failed archive never blocks the respawn, and the duty alerts Recovery itself.
# ---------------------------------------------------------------------------
_sc_out="$(timeout 90 /usr/bin/python3 "$SCRIPT_DIR/session_chronicle.py" session-end \
      --persona "$PERSONA" --cause ctxfill-relaunch --handoff-file "$HANDOFF_FILE" \
      --handoff-fresh "$HANDOFF_FRESH" --stage --certain \
      --persona-state-dir "${PANTHEON_PERSONA_STATE_DIR:-${PANTHEON_STATE_DIR:-$HOME/.local/state/pantheon}/personas}" \
      --entrypoint pantheon-seat-relaunch 2>&1)" || true
log "session-chronicle: ${_sc_out:0:400}"

# ---------------------------------------------------------------------------
# Step 6: respawn under the seat's OWN user manager via a transient --scope
# unit (T-3829 primitive). The scope parents the new tmux server in
# user-<uid>.slice/<scope-unit>, not a foreign manager. Reconstruct the
# PANTHEON_TMUX_* env deterministically from the explicit identity (matches the
# canonical launch), and replay the captured claude argv verbatim.
# ---------------------------------------------------------------------------
log "respawn: systemd-run --user --scope --unit=$SCOPE_UNIT (uid $UID_ARG)"
# `9>&-` CLOSES the flock fd (opened `exec 9>` at step 1) for the respawn ONLY.
# Without it the long-lived respawned processes (the tmux SERVER + the claude REPL)
# INHERIT fd 9 and thus hold the flock for the life of the new session — so the
# NEXT relaunch on the same lockfile (a repeated ctxfill auto-restart, or a
# re-arm) deadlocks on "lock held by another instance", where the holder is the
# very session it wants to relaunch (T-3846: surfaced on artisan's re-arm; latent
# for every armed seat's 2nd relaunch). Closing it here is the surgical fix — the
# parent script keeps the lock for the whole relaunch; only the child session drops
# it. Short-lived children (systemctl, tmux send-keys, kill-server) exit and their
# inheritance is harmless, so the respawn is the only leak point that matters.
# T-3937: "${CARGO_WRAP[@]}" is the echo shared-cargo-target launcher wrapper
# (empty for non-echo seats => byte-identical to the pre-T-3937 command). It
# `exec`s the captured claude in place, so the respawned pane's foreground image
# is the bare claude argv — capture-safe on the next relaunch.
if ! systemd-run --user --scope --unit="$SCOPE_UNIT" \
      tmux start-server ';' set-option -g remain-on-exit on ';' \
        new-session -d -s "$SESSION" -n "$WINDOW" -c "$CAPTURED_CWD" \
        /usr/bin/env -- \
          "PANTHEON_TMUX_SESSION=$SESSION" \
          "PANTHEON_TMUX_WINDOW=$WINDOW" \
          "PANTHEON_TMUX_PANE=0" \
          "${CARGO_WRAP[@]}" \
          "${CAPTURED_ARGV[@]}" 9>&- ; then
  err "respawn: systemd-run --scope failed"
  fail_masked 6
fi
ROE_SET=1

# Poll for the pane to come live.
steady_deadline=$(( $(date +%s) + STEADY_TIMEOUT ))
pane_live=0
while (( $(date +%s) < steady_deadline )); do
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    pane_is_dead && die_on_dead_pane
    pane_live=1; break
  fi
  sleep "$POLL_INTERVAL"
done
if [[ "$pane_live" != "1" ]]; then
  err "respawn: session $SESSION did not come live within ${STEADY_TIMEOUT}s"
  fail_masked 6
fi
# T-5016: re-arm the pane-log tap on the respawned session. kill-server ended the
# old tap, and nothing else re-arms it on this path, so without this every session
# after the first relaunch went unlogged (nothing for the Chronicle to archive).
# Fail-open by contract (arm_pane_log.sh always exits 0).
timeout 20 "$SCRIPT_DIR/arm_pane_log.sh" "$TARGET_PANE" "$PERSONA" || true

# ---------------------------------------------------------------------------
# Step 7: steady-state sentinel conjunction (verify-pattern AND idle-sentinel
# present, dialog-exclude ABSENT). Dismiss the two known benign boot dialogs
# inline (fail-closed: never dismiss if a dangerous modal is co-present).
# ---------------------------------------------------------------------------
steady_ok=0
boot_dialog_dismissed=0
steady_deadline=$(( $(date +%s) + STEADY_TIMEOUT ))
while (( $(date +%s) < steady_deadline )); do
  pane_is_dead && die_on_dead_pane
  pane="$(tmux capture-pane -p -t "$TARGET_PANE" 2>/dev/null || true)"
  # Inline dismissal of the two benign boot dialogs (dev-channels = option 1;
  # renderer = option 2), fail-closed against the denylist. SINGLE-SHOT
  # (boot_dialog_dismissed) so a not-yet-cleared dialog is not re-pressed every
  # poll; and via send_keys_submit so the digit is SUBMITTED with the confirming
  # Enter the dialogs' 'Enter to confirm' footer requires (bare send-keys of the
  # digit selects but never confirms). Matches handoff_and_restart.sh:824-834.
  if (( ! boot_dialog_dismissed )) && ! grep -Eq "$BOOT_DISMISS_DENYLIST" <<<"$pane"; then
    boot_opt=""
    if grep -Fq "$BOOT_DIALOG_SENTINEL" <<<"$pane"; then boot_opt=1
    elif grep -Fq "$BOOT_RENDERER_SENTINEL" <<<"$pane"; then boot_opt=2; fi
    if [[ -n "$boot_opt" ]]; then
      log "step7: known startup dialog — dismissing inline (option $boot_opt; 1=dev-channels ack, 2=decline renderer)"
      # T-4832: this Enter MUST land in the (sentinel-matched, not-denylisted) boot
      # dialog, so the one-call bypass of send_keys_submit's pre-Enter dialog check.
      SEND_KEYS_ALLOW_DIALOG_ENTER=1 send_keys_submit "$TARGET_PANE" "$boot_opt" \
        || log "step7: boot-dialog dismiss send failed; will fall through to fail_masked if it persists"
      boot_dialog_dismissed=1
      sleep "$POLL_INTERVAL"; continue
    fi
  fi
  if grep -Eq "$VERIFY_PATTERN" <<<"$pane" \
     && grep -Eq "$IDLE_SENTINEL" <<<"$pane" \
     && ! grep -Eq "$DIALOG_EXCLUDE" <<<"$pane"; then
    steady_ok=1; break
  fi
  sleep "$POLL_INTERVAL"
done
if [[ "$steady_ok" != "1" ]]; then
  err "step7: steady-state sentinel not met within ${STEADY_TIMEOUT}s (pane not at idle prompt); required '$VERIFY_PATTERN' AND '$IDLE_SENTINEL' AND NOT '$DIALOG_EXCLUDE'"
  # Session came live (step 6) but never reached idle — an unknown/half-booted
  # state. Leave the fence UP (fail-safe) so the controller does not auto-create
  # over it, and print the reconcile command.
  fail_masked 7
fi
log "step7: fresh session up at idle prompt"
# T-4201: what we just launched reached an idle prompt, so it is now the last-good
# launch (it carries any T-4085 role-config rewrite).
persist_launch "verified" "$CAPTURED_CWD" "${CAPTURED_ARGV[@]}" || true
# T-4171: the harness is up; restore normal exit semantics (a later harness exit ends
# the session again, as every monitor expects).
tmux set-option -gu remain-on-exit 2>/dev/null || true
ROE_SET=0

# ---------------------------------------------------------------------------
# Step 7b (T-4088): resume KICK. A fresh claude boots to an idle prompt and does
# nothing until input arrives, so it never reads the handoff its predecessor just
# wrote (mason 2026-09-28: first working relaunch sat idle until Recovery hand-kicked
# it). Worse, an idle session writes NO jsonl, so context_fill_monitor keeps
# measuring the predecessor's full transcript. Deliver a resume prompt through
# kick_verify.py — the same verified-dispatch loop handoff_and_restart.sh step 8
# uses (settle -> send_keys_submit -> poll for a started turn -> bounded re-fire).
# Runs BEFORE the unfence, so no persona-consume run can race it. The OTHER
# injectors (mailbox-wake, keep-going) are not fenced; if one starts a turn first,
# the kick sits queued behind it, so --accept-running-turn counts a running turn as
# delivered instead of re-pasting duplicates (the goal is "successor not idle"). A
# failed kick does NOT skip the unfence (the session is live and must be covered);
# it surfaces as exit 10 after the unfence, and context_fill_monitor's idle-
# successor alert backstops it.
# ---------------------------------------------------------------------------
KICK_FAILED=0
if [[ "$KICK_ENABLED" == "1" ]]; then
  if [[ -z "$KICK_PROMPT" ]]; then
    KICK_PROMPT="[ctxfill relaunch — successor] Your session was just cycled to reclaim context. Read $HANDOFF_FILE (your predecessor's handoff), drain your mailbox, then continue the handoff's queued next work — do not sit idle."
    if [[ "$HANDOFF_FRESH" != "1" ]]; then
      KICK_PROMPT+=" NOTE: the handoff step was skipped or timed out, so that file may predate this relaunch — verify its claims against live state before acting."
    fi
  fi
  KICK_SETTLE_SECS="${PANTHEON_SEAT_RELAUNCH_KICK_SETTLE_SECS:-3}"
  KICK_POLL_SECS="${PANTHEON_SEAT_RELAUNCH_KICK_POLL_SECS:-12}"
  KICK_POLL_INTERVAL="${PANTHEON_SEAT_RELAUNCH_KICK_POLL_INTERVAL:-2}"
  KICK_MAX_ATTEMPTS="${PANTHEON_SEAT_RELAUNCH_KICK_MAX_ATTEMPTS:-3}"
  # T-4456: render-gate each inject into the fresh TUI (T-4362/T-4455); 8s is the
  # runtime_controller fresh-runtime bound.
  KICK_RENDER_WAIT="${PANTHEON_SEAT_RELAUNCH_KICK_RENDER_WAIT:-8}"
  if /usr/bin/python3 "$SCRIPT_DIR/kick_verify.py" \
        --pane "$TARGET_PANE" --prompt "$KICK_PROMPT" \
        --settle "$KICK_SETTLE_SECS" \
        --poll-secs "$KICK_POLL_SECS" \
        --poll-interval "$KICK_POLL_INTERVAL" \
        --max-attempts "$KICK_MAX_ATTEMPTS" \
        --render-wait "$KICK_RENDER_WAIT" \
        --accept-running-turn \
        --send-keys-submit "$SCRIPT_DIR/send_keys_submit.sh"; then
    log "step7b: resume kick delivered + dispatch-verified to $TARGET_PANE"
  else
    err "step7b: resume kick NOT dispatched after bounded re-fires — $PERSONA is live but IDLE; inject by hand: send_keys_submit.sh $TARGET_PANE '<read $HANDOFF_FILE and resume>'"
    KICK_FAILED=1
  fi
else
  log "step7b: resume kick skipped (--no-kick)"
fi

# ---------------------------------------------------------------------------
# Step 8: relaunch verified — restore the consume timer.
# ---------------------------------------------------------------------------
if ! unfence_consume_timer; then
  err "step8: consume-timer restore failed — timer left masked; reconcile with: systemctl --user unmask $CONSUME_TIMER && systemctl --user start $CONSUME_TIMER"
  exit 8
fi

if [[ "$KICK_FAILED" == "1" ]]; then
  err "relaunch landed but the successor is IDLE (resume kick not dispatched) — exit 10"
  exit 10
fi

log "relaunch complete: $PERSONA session $SESSION live in $SCOPE_UNIT"
exit 0
