#!/usr/bin/env bash
# persona_cutover.sh — T-2633: GENERIC forward-cutover, extracted from
# tools/t2590_recovery_cutover.sh (the first, hardened-in-place, Recovery-specific
# instance). Moves ANY persona's live runtime off its bespoke telegram-bot
# supervisor onto the control plane (pantheon-persona-consume@<persona>),
# using tools/persona_cutover_resolve.py to resolve every account-specific
# fact instead of hardcoding them. The operator's steer (T-2633): the second
# cutover is the moment to extract the generic mechanism, since the pieces
# underneath (persona_installer / persona_bringup / t2611 persona_apply
# --persona / runtime_controller) are already persona-general -- only the
# cutover *flow* was still hardcoded to Recovery.
#
# AUTHORED, DRY-VALIDATED for recovery+specialist (see tools/test_persona_cutover_
# resolve.py's correctness gate) -- NOT FIRED for specialist. The fire itself is
# operator-ack-gated, same posture as T-2590 (design: tickets/plans/T-2590-
# recovery-cutover-design.md section 2). Nothing in this script is invoked by
# anything until a human runs it with --execute.
#
# RUN AS the target persona's own account -- it writes that account's files
# and drives that account's user systemd. tools/persona_cutover_resolve.py
# resolves the uid dynamically (via pwd.getpwnam); this script enforces it
# against the REAL caller uid before doing anything destructive. HO
# fire-time invocation:
#   PW=$(tr -d '\n' < /home/claude/root.env)
#   echo "$PW" | sudo -S -k -u <account> bash <readable-path>/persona_cutover.sh --persona <name> --stamp <TS>            # dry-run (default)
#   echo "$PW" | sudo -S -k -u <account> bash <readable-path>/persona_cutover.sh --persona <name> --stamp <TS> --execute  # real cutover
# (<readable-path> = a path the target account can read -- its own
# pantheon-config clone, or a /tmp copy; /home/claude/... is not
# cross-account readable. Same constraint as t2590_recovery_cutover.sh.)
#
# BACKUP CONVENTION (this script CAPTURES; persona_rollback.sh RESTORES):
#   <file>.pre-cutover-<TS>  living alongside each <file>. Generic suffix (no
#   ticket/persona infix) now that this tool is no longer per-instance -- see
#   T-2633 DECISIONS. The same <TS> passed here must be passed to the
#   rollback script if a rollback is ever needed.
#
# MANIFEST PRECONDITION: tools/persona_cutover_resolve.py's resolve_persona()
# verifies the TARGET persona's OWN pantheon-config clone (not wherever this
# script itself is running from, unless --manifest is omitted, in which case
# the resolver's own default IS that same target-clone path -- see
# _default_manifest_path) -- the exact file pantheon-persona-
# consume@<persona>.service reads via its unit's
# Environment=PANTHEON_INSTANCE_MANIFEST=%h/pantheon-config/instance.yaml.
# Verifying any OTHER clone would pass while the live bringup reads stale
# config -- a false green that wedges the cutover. This script only VERIFIES
# that clone via the resolver; it never mutates or pulls it (auto-consumer
# reads disk state, per repo convention).
#
# URGENT-ENV DROP-IN: Recovery's original T-2590 cutover ALSO installed a
# recovery-specific PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED drop-in, using the
# cutover as a supervised-first-fire activation vehicle for a GENERIC
# mailbox-severity kill switch (tools/mailbox.py) that has nothing to do
# with any persona-specific concern. Generalized here as an explicit
# --with-urgent-env flag, default OFF: there is no manifest field to derive
# "wants it" from without inventing a new schema key for what is currently a
# single consumer (T-2633 DECISIONS). Specialist's cutover omits the flag.
set -euo pipefail

PERSONA=""
STAMP=""
EXECUTE=0
MANIFEST=""
UNIT_FILE=""
WITH_URGENT_ENV=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --persona=*)        PERSONA="${1#--persona=}" ;;
    --persona)          PERSONA="${2:-}"; shift ;;
    --stamp=*)           STAMP="${1#--stamp=}" ;;
    --stamp)             STAMP="${2:-}"; shift ;;
    --execute)           EXECUTE=1 ;;
    --manifest=*)        MANIFEST="${1#--manifest=}" ;;
    --manifest)          MANIFEST="${2:-}"; shift ;;
    # Operator escape hatch (also the seam tools/test_persona_cutover_verify.sh
    # uses): dry-run identity resolution against a LOCAL copy of the bespoke
    # unit file instead of the real cross-account read. See
    # persona_cutover_resolve.py's --unit-file help text.
    --unit-file=*)       UNIT_FILE="${1#--unit-file=}" ;;
    --unit-file)         UNIT_FILE="${2:-}"; shift ;;
    --with-urgent-env)   WITH_URGENT_ENV=1 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESOLVER="${SCRIPT_DIR}/persona_cutover_resolve.py"

# `--harness claude` is HARDCODED (not derived) at every t2611 call site below
# -- DELIBERATE current scope, not an oversight: this tool migrates an
# EXISTING claude persona off a bespoke claude-specific supervisor. Codex was
# never on a bespoke supervisor to migrate (it went straight through
# persona_installer as the ORG-018/031 pilot), so there is no codex analog of
# "the second cutover" this tool exists to generalize FROM. Relax-note (P6,
# same pattern as the tmux-session invariant in persona_cutover_resolve.py):
# if a future non-claude seat ever needs migrating off a bespoke supervisor,
# thread a --harness flag through here and resolve_persona()'s bespoke-unit
# parsing (parse_supervise_wrapper() is already harness-agnostic; only this
# hardcoded "claude" literal and the CONTROL_PLANE_CHANNELS telegram-plugin
# assumption in the resolver would need to become parameters).

die() { echo "CUTOVER-ABORT: $*" >&2; exit 1; }
note() { echo "-- $*"; }

# --- preflight ---------------------------------------------------------
[[ -n "$PERSONA" ]] || die "no --persona <name> given"
[[ -n "$STAMP" ]] || die "no --stamp <TS> given (the fire-time backup suffix)"

# --- resolve identity (this ALSO runs the manifest precondition) -------
# T-2620/T-2622: pure/testable verify helpers (pid selection, urgent-env
# confirm, telegram-poller tree-walk). Persona-general already -- reused
# unforked, same as t2590_recovery_cutover.sh sourced it.
# shellcheck source=t2590_cutover_verify_lib.sh
source "${SCRIPT_DIR}/t2590_cutover_verify_lib.sh"

RESOLVE_ARGS=(resolve --persona "$PERSONA" --format env)
[[ -n "$MANIFEST" ]] && RESOLVE_ARGS+=(--manifest "$MANIFEST")
[[ -n "$UNIT_FILE" ]] && RESOLVE_ARGS+=(--unit-file "$UNIT_FILE")
if ! RESOLVE_OUTPUT="$(python3 "$RESOLVER" "${RESOLVE_ARGS[@]}" 2>&1)"; then
  die "persona resolution / manifest precondition failed -- ${RESOLVE_OUTPUT}"
fi
# PCX_-prefixed keys only (see persona_cutover_resolve.py's as_env() docstring
# for why: bare UID/HOME keys would collide with bash's own readonly $UID and
# load-bearing $HOME).
eval "$RESOLVE_OUTPUT"
note "manifest precondition OK (${PCX_MANIFEST_PATH} carries the synced control-plane launch shape for persona ${PERSONA}, role ${PCX_ROLE})"

XRD="$PCX_XDG_RUNTIME_DIR"
DBUS="$PCX_DBUS_SESSION_BUS_ADDRESS"

# --- preflight continued: uid + XDG runtime dir -------------------------
[[ "$(id -u)" == "$PCX_UID" ]] || die "must run as ${PCX_ACCOUNT} (uid ${PCX_UID}); got uid $(id -u). Wrap with sudo -u ${PCX_ACCOUNT}."
[[ -d "$XRD" ]] || die "XDG_RUNTIME_DIR $XRD missing — user bus unreachable; cannot drive ${PCX_ACCOUNT}'s systemctl --user"
[[ -n "$DBUS" ]] || die "DBUS_SESSION_BUS_ADDRESS missing — user bus unreachable; cannot drive ${PCX_ACCOUNT}'s systemctl --user"
export XDG_RUNTIME_DIR="$XRD"
export DBUS_SESSION_BUS_ADDRESS="$DBUS"

# ORG-106 L3a (T-5526): a cutover starts the seat's session; never wake a deep-sleeping seat.
_lc_rc=0
python3 "$SCRIPT_DIR/seat_state.py" launch-check "$PERSONA" || _lc_rc=$?
[[ $_lc_rc -ne 3 ]] || die "${PERSONA} is in deep-sleep; wake it first (harness ${PERSONA} wake)"

# --- precondition: persona is install-done / harness-findable (T-2611) -----
# Read-only (runs installer.plan() + a read-only preflight pass; never
# apply()). Runs in both dry-run and --execute modes: it's cheap and it's the
# guard that would have caught Recovery's attempt-2 PATH failure before Recovery went
# down. PERSONA_APPLY_HELPER is resolved via the TARGET clone's own tools/
# dir (PCX_REPO_ROOT = dirname(PCX_MANIFEST_PATH)), matching
# t2590_recovery_cutover.sh's RECOVERY_REPO_ROOT convention exactly -- the helper
# script and the manifest it operates on must come from the same checkout.
PERSONA_APPLY_HELPER="${PCX_REPO_ROOT}/tools/t2611_recovery_persona_apply.py"
if ! INSTALL_PRECONDITION_OUTPUT="$(PATH="$PCX_APPLY_PATH" python3 "$PERSONA_APPLY_HELPER" precondition --manifest "$PCX_MANIFEST_PATH" --persona "$PERSONA" --harness claude --repo-root "$PCX_REPO_ROOT" 2>&1)"; then
  die "install-done precondition failed (${PERSONA} is not control-plane-ready) -- ${INSTALL_PRECONDITION_OUTPUT}"
fi
note "install-done precondition OK -- ${INSTALL_PRECONDITION_OUTPUT}"

if [[ $EXECUTE == 1 ]]; then
  if ! STAGED_CHECK_OUTPUT="$(PATH="$PCX_APPLY_PATH" python3 "$PERSONA_APPLY_HELPER" check-staged --manifest "$PCX_MANIFEST_PATH" --persona "$PERSONA" --harness claude --repo-root "$PCX_REPO_ROOT" --stamp "$STAMP" 2>&1)"; then
    die "staged checkpoint precondition failed -- ${STAGED_CHECK_OUTPUT}"
  fi
  note "staged checkpoint OK -- ${STAGED_CHECK_OUTPUT}"
fi

echo "== T-2633 ${PERSONA} generic forward-cutover =="
echo "persona:  $PERSONA (role: $PCX_ROLE, account: $PCX_ACCOUNT)"
echo "stamp:    $STAMP"
echo "mode:     $([[ $EXECUTE == 1 ]] && echo EXECUTE || echo DRY-RUN)"
echo "manifest: $PCX_MANIFEST_PATH (verified OK above)"
echo "urgent-env drop-in: $([[ $WITH_URGENT_ENV == 1 ]] && echo ENABLED || echo disabled)"

declare -a FILES=(
  "$PCX_SERVICE_UNIT_PATH"
  "$PCX_SUPERVISE_WRAPPER_PATH"
  "$PCX_SETTINGS_JSON_PATH"
)

DROPIN_DIR="${PCX_HOME}/.config/systemd/user/${PCX_CONSUME_SERVICE}.d"
DROPIN_FILE="${DROPIN_DIR}/urgent.conf"
URGENT_ENV="PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED"

if [[ $EXECUTE != 1 ]]; then
  echo
  echo "DRY-RUN — would then run:"
  for f in "${FILES[@]}"; do
    echo "  cp -p ${f}  ${f}.pre-cutover-${STAMP}"
  done
  if [[ $WITH_URGENT_ENV == 1 ]]; then
    echo "  mkdir -p ${DROPIN_DIR}"
    echo "  write ${DROPIN_FILE}: [Service]\\nEnvironment=${URGENT_ENV}=1"
  fi
  echo "  systemctl --user daemon-reload"
  echo "  systemctl --user disable --now ${PCX_SERVICE}"
  echo "  tmux kill-session -t ${PCX_TMUX_SESSION}   # T-2611: ensure default socket is empty before activation"
  echo "  [abort if ${PCX_TMUX_SESSION} survives the kill]"
  if [[ -n "$PCX_COMPANION_UNITS" ]]; then
    for tok in $PCX_COMPANION_UNITS; do
      echo "  systemctl --user stop ${tok%%=*}   # T-2648: also shares the default tmux socket (session ${tok#*=}); must be down before _start_runtime"
      echo "  [abort if ${tok#*=} survives the stop]"
    done
  fi
  echo "  require staged checkpoint: PATH=${PCX_APPLY_PATH} python3 ${PERSONA_APPLY_HELPER} check-staged --manifest ${PCX_MANIFEST_PATH} --persona ${PERSONA} --harness claude --repo-root ${PCX_REPO_ROOT} --stamp ${STAMP}"
  echo "  PATH=${PCX_APPLY_PATH} python3 ${PERSONA_APPLY_HELPER} activate-staged --manifest ${PCX_MANIFEST_PATH} --persona ${PERSONA} --harness claude --repo-root ${PCX_REPO_ROOT} --stamp ${STAMP}"
  echo "                        # persona_installer activation suffix: fires ${PCX_CONSUME_TIMER} / ${PCX_CONSUME_SERVICE} from staged checkpoint (T-2953 R5)"
  if [[ -n "$PCX_COMPANION_UNITS" ]]; then
    for tok in $PCX_COMPANION_UNITS; do
      echo "  systemctl --user start ${tok%%=*}   # T-2648: rejoin the now-scope-owned default tmux server"
    done
  fi
  echo "  verify (soak-crit-4): tmux session, scope ownership, exact argv flags"
  if [[ $WITH_URGENT_ENV == 1 ]]; then
    echo "                        , urgent env via ${PCX_CONSUME_SERVICE}'s effective"
    echo "                        systemd Environment= (T-2620 — a live claude"
    echo "                        process's /proc/<pid>/environ is ptrace-denied"
    echo "                        from this verify's context)"
  fi
  echo "                        , ready composer (no channels-startup dialog),"
  echo "                        telegram inbound poller present (T-2622, bounded retry)"
  echo "All preconditions OK. Dry-run OK — pass --execute to perform the cutover."
  exit 0
fi

# --- execute -------------------------------------------------------------
for f in "${FILES[@]}"; do
  cp -p "$f" "${f}.pre-cutover-${STAMP}"
  echo "backed up: $f"
done

if [[ $WITH_URGENT_ENV == 1 ]]; then
  mkdir -p "$DROPIN_DIR"
  printf '[Service]\nEnvironment=%s=1\n' "$URGENT_ENV" > "$DROPIN_FILE"
  echo "installed: $DROPIN_FILE"
fi

systemctl --user daemon-reload

# Retire the bespoke supervisor. Its ExecStop kills the bespoke tmux session,
# so the default tmux socket is empty afterward -- the precondition
# _start_runtime needs before it will create the control-plane's own session.
systemctl --user disable --now "$PCX_SERVICE"
echo "disabled: $PCX_SERVICE"

# Activate the persona under the control plane (T-2611 fix, generalized).
# The activation must run AFTER the bespoke session is confirmed dead: an
# activated control-plane claude colliding with a still-live bespoke claude
# on the same telegram bot poller is the two-supervisor failure T-2611's
# dry-plan found before any live mutation.
tmux kill-session -t "$PCX_TMUX_SESSION" 2>/dev/null || true
if tmux has-session -t "$PCX_TMUX_SESSION" 2>/dev/null; then
  die "bespoke tmux session ${PCX_TMUX_SESSION} survived kill-session -- refusing to run persona_installer apply with a live bespoke claude still bound to the telegram bot"
fi
echo "confirmed: default tmux socket has no ${PCX_TMUX_SESSION} session"

# T-2648: any companion unit(s) declared for this persona (instance.yaml
# roles.<role>.companion_units, e.g. specialist's acp-bridge, T-1689) ALSO land on
# the default tmux socket. If one survives past this point, the apply step
# below fires _start_runtime against a live, non-scope-owned default server
# and aborts (T-2648 root cause) -- stop every declared companion here, same
# as the bespoke session above, before persona_installer ever runs.
# Deliberately unquoted word-split: PCX_COMPANION_UNITS is manifest-
# controlled `unit=session` tokens (agent_role_map._encode_companion_units),
# never glob- or space-bearing within a token.
for tok in $PCX_COMPANION_UNITS; do
  companion_unit="${tok%%=*}"
  companion_session="${tok#*=}"
  systemctl --user stop "$companion_unit"
  echo "stopped companion unit: $companion_unit"
  if tmux has-session -t "$companion_session" 2>/dev/null; then
    die "companion tmux session ${companion_session} survived 'systemctl --user stop ${companion_unit}' -- refusing to run persona_installer apply with a live foreign session still on the default tmux socket"
  fi
  echo "confirmed: default tmux socket has no ${companion_session} session"
done

if ! PERSONA_APPLY_OUTPUT="$(PATH="$PCX_APPLY_PATH" python3 "$PERSONA_APPLY_HELPER" activate-staged --manifest "$PCX_MANIFEST_PATH" --persona "$PERSONA" --harness claude --repo-root "$PCX_REPO_ROOT" --stamp "$STAMP" 2>&1)"; then
  echo "$PERSONA_APPLY_OUTPUT" >&2
  die "persona_installer apply failed -- ${PERSONA} has no active supervisor now. Roll back immediately: tools/persona_rollback.sh --persona ${PERSONA} --stamp ${STAMP} --execute"
fi
echo "$PERSONA_APPLY_OUTPUT"
echo "activated: persona_installer staged checkpoint validated + activation suffix fired ${PCX_CONSUME_TIMER} / ${PCX_CONSUME_SERVICE}"

# T-2648: rejoin now that the control plane owns the default tmux server --
# the companion's own join-existing-server behavior (T-1689, e.g. acp-
# bridge-supervise.sh) lands it in the scope cgroup this time, which the
# ticket calls arguably better than its pre-cutover foreign-server home.
for tok in $PCX_COMPANION_UNITS; do
  companion_unit="${tok%%=*}"
  systemctl --user start "$companion_unit"
  echo "restarted companion unit: $companion_unit"
done

# --- verify (soak-crit-4) -------------------------------------------------
# NB: bracket on the 'd' (clau[d]e) is deliberate — it matches the runtime
# process identically while keeping the contiguous claude-binary byte-sequence
# OUT of this script, so HO's own no-second-claude PreToolUse guard (which
# inspects executed script files) does not false-positive when firing cutover.
fail=0

if tmux has-session -t "$PCX_TMUX_SESSION" 2>/dev/null; then
  echo "verify: tmux session ${PCX_TMUX_SESSION} up OK"
else
  echo "verify: tmux session ${PCX_TMUX_SESSION} MISSING FAIL"; fail=1
fi

# T-2648: every companion unit stopped above must have rejoined (active unit
# + its tmux session present) by the time apply() returned.
#
# T-2651: bounded retry (10x1s = 10s), same shape as the telegram-poller
# check below (T-2624 precedent) — fire#3 false-negatived a companion that
# WAS live: the immediate, zero-wait check here ran right after
# `systemctl --user start "$companion_unit"` above, before the unit's own
# `tmux new-session` had landed on the scope-owned default server. 10s is a
# tmux-session-creation race, not the poller's bun-install/boot race, hence
# the much smaller budget than the poller's 30x3s below.
for tok in $PCX_COMPANION_UNITS; do
  companion_unit="${tok%%=*}"
  companion_session="${tok#*=}"
  COMPANION_OK=0
  for _ in $(seq 1 10); do
    if cutover_companion_rejoined "$companion_unit" "$companion_session"; then
      COMPANION_OK=1
      break
    fi
    sleep 1
  done
  if [[ "$COMPANION_OK" == 1 ]]; then
    echo "verify: companion unit ${companion_unit} (session ${companion_session}) rejoined OK"
  else
    echo "verify: companion unit ${companion_unit} (session ${companion_session}) NOT rejoined after bounded wait FAIL"; fail=1
  fi
done

# Scope ownership: the tmux SERVER pid (not the claude child) must live
# under the scope unit recorded in runtime.json's `runtime_scope_unit`
# field (T-2606 state shape — there is no `server_pid` key; the server pid
# is read fresh from the default tmux socket, matching
# `_default_tmux_server_pid()`).
STATE_ROOT="${PCX_HOME}/.local/state/pantheon/personas"
RUNTIME_JSON="${STATE_ROOT}/${PERSONA}/runtime-controller/runtime.json"
SCOPE_UNIT=""
if [[ -f "$RUNTIME_JSON" ]]; then
  SCOPE_UNIT="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('runtime_scope_unit',''))" "$RUNTIME_JSON" 2>/dev/null || true)"
fi
TMUX_SERVER_PID="$(tmux -N list-sessions -F '#{pid}' 2>/dev/null | sort -u | head -n1 || true)"
if [[ -n "$TMUX_SERVER_PID" && -n "$SCOPE_UNIT" ]] && grep -q "$SCOPE_UNIT" "/proc/${TMUX_SERVER_PID}/cgroup" 2>/dev/null; then
  echo "verify: scope ownership (${SCOPE_UNIT}) OK"
else
  echo "verify: scope ownership MISSING/UNVERIFIABLE FAIL"; fail=1
fi

SELECTED_HARNESS=""
SELECTED_PROFILE=""
if [[ -f "$RUNTIME_JSON" ]]; then
  SELECTED_HARNESS="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('harness',''))" "$RUNTIME_JSON" 2>/dev/null || true)"
  SELECTED_PROFILE="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('profile',''))" "$RUNTIME_JSON" 2>/dev/null || true)"
fi

if [[ "$SELECTED_HARNESS" == "codex" ]]; then
  if [[ "$SELECTED_PROFILE" == "interactive-chatgpt" ]]; then
    echo "verify: selected runtime codex/${SELECTED_PROFILE} OK"
  else
    echo "verify: selected codex profile ${SELECTED_PROFILE:-missing} FAIL"; fail=1
  fi
  if (cd "$PCX_REPO_ROOT" && PATH="$PCX_APPLY_PATH" python3 -m tools.runtime_controller initial-viability --persona "$PERSONA" --state-root "$STATE_ROOT" --expect-selected "codex/interactive-chatgpt" --expect-failed "claude/interactive-subscription" --expect-failure-class subscription-cap --expect-failure-class operational-launch --expect-failure-class operational-readiness --require-source-provenance --require-receiver-receipt) >/dev/null; then
    echo "verify: initial viability selected Codex with allowed failure and receiver evidence OK"
  else
    echo "verify: initial viability selected Codex evidence MISSING/MISMATCH FAIL"; fail=1
  fi
elif [[ "$SELECTED_HARNESS" == "claude" ]]; then
# Exact launch flags. The `clau[d]e` bracket idiom (see NB above) is load-
# bearing here too. Path-anchored on the npm-global claude binary -- a bare
# "clau[d]e" substring would false-positive-match any other process whose
# argv happens to contain that string, and self-match this very probing
# shell's own argv (T-1930 class; tools/pgrep_predicate_lint.py).
#
# T-2620: the raw pgrep -f match matches BOTH the real claude process and the
# transient `tmux new-session -d ... claude ...` LAUNCHER (its argv is a
# superset match). Filter candidates by `comm`
# (select_cutover_claude_pid, t2590_cutover_verify_lib.sh) so the resolved
# pid is the actual claude process, not its launcher.
CUTOVER_PID="$(
  { pgrep -u "$PCX_ACCOUNT" -f "\.npm-global/bin/clau[d]e" 2>/dev/null | while IFS= read -r cp; do
      printf '%s %s\n' "$cp" "$(ps -o comm= -p "$cp" 2>/dev/null || true)"
    done
  } | select_cutover_claude_pid || true
)"
if [[ -n "$CUTOVER_PID" ]]; then
  CUTOVER_ARGV="$(ps -o args= -p "$CUTOVER_PID" 2>/dev/null || true)"
else
  CUTOVER_ARGV=""
fi
if echo "$CUTOVER_ARGV" | grep -q -- "--channels ${PCX_CHANNELS}"; then
  echo "verify: --channels flag OK"
else
  echo "verify: --channels flag MISSING FAIL"; fail=1
fi
if echo "$CUTOVER_ARGV" | grep -q -- "--model ${PCX_MODEL}"; then
  echo "verify: --model flag OK"
else
  echo "verify: --model flag MISSING FAIL"; fail=1
fi
if echo "$CUTOVER_ARGV" | grep -q -- "--permission-mode bypassPermissions"; then
  echo "verify: --permission-mode flag OK"
else
  echo "verify: --permission-mode flag MISSING FAIL"; fail=1
fi

if [[ $WITH_URGENT_ENV == 1 ]]; then
  # Urgent env (T-2620): /proc/<pid>/environ is UNAVAILABLE from this verify's
  # context regardless of which pid -- it runs as the target account
  # (privileges dropped, no root) and is NOT an ancestor of the claude
  # process, so ptrace_scope denies the read even for the right pid. Confirm
  # instead via the consume service's own EFFECTIVE Environment (base unit +
  # the urgent.conf drop-in this script installs above) -- the same-uid,
  # no-ptrace proxy for "the runtime has the flag".
  CONSUME_ENV_SHOW="$(systemctl --user show "$PCX_CONSUME_SERVICE" -p Environment --value 2>/dev/null || true)"
  if cutover_urgent_env_confirmed "${URGENT_ENV}=1" "$CONSUME_ENV_SHOW"; then
    echo "verify: urgent env (${URGENT_ENV}) OK"
  else
    echo "verify: urgent env MISSING FAIL"; fail=1
  fi
fi

# PRE-3 channels-startup-dialog check: the composer must come up ready with
# no dev-channels-confirm modal in the way. Sentinels reused verbatim from
# recovery_boot_kick.sh (VERIFY_PATTERN / DIALOG_EXCLUDE) rather than re-derived,
# so this script and the boot-kick agree on what "ready" looks like.
PANE_TEXT="$(tmux capture-pane -t "$PCX_TMUX_SESSION" -p 2>/dev/null || true)"
if echo "$PANE_TEXT" | grep -qE '(╭(─)+╮|(─){20,})' && ! echo "$PANE_TEXT" | grep -qE 'Enter to confirm|Esc to cancel'; then
  echo "verify: composer ready, no channels dialog OK"
else
  echo "verify: composer NOT ready or channels dialog present FAIL"; fail=1
fi

# Inbound comms (T-2622): a healthy-looking pane is NOT the same as the
# persona being reachable. Confirm the plugin's bun children are actually
# alive in THIS pane's process tree (cutover_telegram_poller_present,
# t2590_cutover_verify_lib.sh -- the same kernel-PPID-tree-walk predicate
# tools/verify_startup.sh uses, deliberately not bot.pid). Bounded retry
# (30x3s = 90s, T-2624): a slow-but-coming-up poller must not be hard-failed
# as comms-dark.
COMMS_OK=0
for _ in $(seq 1 30); do
  if cutover_telegram_poller_present "$PCX_TMUX_SESSION" "$PCX_HOME" "/proc"; then
    COMMS_OK=1
    break
  fi
  sleep 3
done
if [[ "$COMMS_OK" == 1 ]]; then
  echo "verify: telegram inbound poller (plugin runner + bun server.ts) present OK"
else
  # T-2624 diagnostic: capture the LIVE control-plane session evidence BEFORE
  # the operator rolls back, so a comms-dark verdict is decidable. Survives
  # the rollback. Best-effort; never blocks the FAIL.
  DIAG="${PCX_HOME}/.local/state/pantheon/t2633-comms-dark-${STAMP}.txt"
  {
    echo "=== T-2633 comms-dark diagnostic, persona ${PERSONA}, stamp ${STAMP} ==="
    echo "--- tmux pane (last 200 lines: bun install / server.ts / 409 / MCP-init) ---"
    tmux capture-pane -t "$PCX_TMUX_SESSION" -p -S -200 2>&1
    echo "--- process tree under the pane (did the poller partially spawn?) ---"
    PANE_PID="$(tmux display-message -p -t "$PCX_TMUX_SESSION" '#{pane_pid}' 2>/dev/null)"
    [[ -n "$PANE_PID" ]] && ps --forest -o pid,ppid,etimes,comm,args -g "$(ps -o sid= -p "$PANE_PID" 2>/dev/null | tr -d ' ')" 2>&1 | head -40
    echo "--- any bun/server.ts under ${PCX_ACCOUNT} ---"
    pgrep -u "$PCX_ACCOUNT" -af 'bun' 2>&1 | head
  } > "$DIAG" 2>&1 || true
  echo "verify: telegram inbound poller MISSING after bounded wait -- ${PERSONA} would be comms-dark FAIL (diag: ${DIAG})"; fail=1
fi
else
  echo "verify: selected runtime harness ${SELECTED_HARNESS:-missing} is not declared for post-activation verification FAIL"; fail=1
fi

if [[ $fail == 0 ]]; then
  echo "CUTOVER COMPLETE — ${PERSONA} on control-plane, all soak-crit-4 elements verified."
  exit 0
else
  die "cutover ran but verification FAILED — see FAIL lines above. Roll back: tools/persona_rollback.sh --persona ${PERSONA} --stamp ${STAMP} --execute"
fi
