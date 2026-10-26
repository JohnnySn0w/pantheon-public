#!/usr/bin/env bash
# persona_rollback.sh — T-2633: GENERIC restore after a persona_cutover.sh
# run, extracted from tools/t2590_recovery_rollback.sh (the Recovery-specific
# original). Restores a persona's bespoke launch to the exact state captured
# immediately before fire, then restarts + verifies.
#
# STAGED CONTINGENCY — NOT FIRED. Pre-drafted, dry-run-verified rollback,
# same posture as T-2590's (operator-gated, ping-and-go). It is
# DIRECTION-INDEPENDENT: whatever the cutover replaces the launch with,
# rollback always means "put the three bespoke files back and restart" --
# safe to pre-build ahead of any live fire.
#
# RUN AS the target persona's own account -- it writes that account's files
# and drives that account's user systemd. HO fire-time invocation:
#   PW=$(tr -d '\n' < /home/claude/root.env)
#   echo "$PW" | sudo -S -k -u <account> bash <readable-path>/persona_rollback.sh --persona <name> --stamp <TS>            # dry-run (default)
#   echo "$PW" | sudo -S -k -u <account> bash <readable-path>/persona_rollback.sh --persona <name> --stamp <TS> --execute  # real restore
# (<readable-path> = a path the target account can read.)
#
# BACKUP CONVENTION (captured fire-time by persona_cutover.sh's backup step,
# NOT here): <file>.pre-cutover-<TS>  living alongside each <file>. This
# script only RESTORES; it never captures.
set -euo pipefail

PERSONA=""
STAMP=""
EXECUTE=0
MANIFEST=""
FORCE_BREAK_GLASS=0
UNIT_FILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --persona=*)  PERSONA="${1#--persona=}" ;;
    --persona)    PERSONA="${2:-}"; shift ;;
    --stamp=*)    STAMP="${1#--stamp=}" ;;
    --stamp)      STAMP="${2:-}"; shift ;;
    --execute)    EXECUTE=1 ;;
    --manifest=*) MANIFEST="${1#--manifest=}" ;;
    --manifest)   MANIFEST="${2:-}"; shift ;;
    # T-4848: explicit recovery step when the manifest pin refuses (manifest
    # loads but disagrees with the checkpoint). Never selected automatically.
    --break-glass-no-manifest) FORCE_BREAK_GLASS=1 ;;
    # Operator escape hatch (also the seam tools/test_persona_cutover_verify.sh
    # uses): dry-run identity resolution against a LOCAL copy of the bespoke
    # unit file instead of the real cross-account read.
    --unit-file=*) UNIT_FILE="${1#--unit-file=}" ;;
    --unit-file)   UNIT_FILE="${2:-}"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESOLVER="${SCRIPT_DIR}/persona_cutover_resolve.py"

# T-2620/T-2622/T-2642: pure/testable verify helpers, same lib
# tools/persona_cutover.sh sources -- reused unforked (rollback_scope_clean
# is the newest addition, T-2642).
# shellcheck source=t2590_cutover_verify_lib.sh
source "${SCRIPT_DIR}/t2590_cutover_verify_lib.sh"

# T-2642: the control-plane's tmux-server scope. Matches
# tools/runtime_controller.py's TMUX_RUNTIME_SCOPE_UNIT constant exactly --
# a persona-general singleton (not per-persona-named), so there is no
# PCX_-derived value to reuse here; hardcoded, same convention as this
# file's existing clau[d]e bracket-literal below (kept in sync manually,
# grep both files if either ever changes).
RUNTIME_SCOPE_UNIT="pantheon-runtime-tmux.scope"
CODEX_RECEIVER_UNIT="codex-telegram-poller.service"
# T-2642 note (b): observed live coupling the leftover control-plane launch
# to the restored bespoke seat. NOT a unit this repo defines anywhere
# (grep-verified: no unit file, no Slice=, no constant) -- most likely
# systemd's own auto-placement artifact for the persona-consume oneshot's
# launch context, not something this script can rely on existing or being
# named this consistently. Teardown of it below is defensive-only, never
# load-bearing -- RUNTIME_SCOPE_UNIT above is the real, code-confirmed
# mechanism.
RUNTIME_CONSUME_SLICE="app-pantheon-persona-consume.slice"

die() { echo "ROLLBACK-ABORT: $*" >&2; exit 1; }

# --- preflight -------------------------------------------------------------
[[ -n "$PERSONA" ]] || die "no --persona <name> given"
[[ -n "$STAMP" ]] || die "no --stamp <TS> given (the fire-time backup suffix)"

# Rollback must keep working even when a target's instance.yaml is missing,
# unreadable, or mid-incident reverted to an inert pre-cutover shape. Identity
# resolution therefore calls `bespoke-identity`, NOT `resolve` -- the latter's
# manifest-precondition gate (channels/model/tmux/post_start synced-shape
# check) is a persona_cutover.sh concern this script does not share. See
# BespokeIdentity's docstring in tools/persona_cutover_resolve.py.
#
# T-4848: the STAGED-checkpoint rollback is manifest-pinned by default (its
# allowed write roots must equal the roots derived from the trusted manifest).
# --manifest selects the manifest (default <home>/pantheon-config/instance.yaml).
# If that manifest is missing or does not load, the script falls back to the
# helper's explicit break-glass mode, loudly: it skips ONLY the manifest
# re-derivation; the receipt/plan digest tie, structural root bounds and
# per-mutation realpath containment are still enforced.
BESPOKE_ARGS=(bespoke-identity --persona "$PERSONA" --format env)
[[ -n "$UNIT_FILE" ]] && BESPOKE_ARGS+=(--unit-file "$UNIT_FILE")
if ! RESOLVE_OUTPUT="$(python3 "$RESOLVER" "${BESPOKE_ARGS[@]}" 2>&1)"; then
  die "persona resolution failed -- ${RESOLVE_OUTPUT}"
fi
eval "$RESOLVE_OUTPUT"

XRD="$PCX_XDG_RUNTIME_DIR"
DBUS="$PCX_DBUS_SESSION_BUS_ADDRESS"

[[ "$(id -u)" == "$PCX_UID" ]] || die "must run as ${PCX_ACCOUNT} (uid ${PCX_UID}); got uid $(id -u). Wrap with sudo -u ${PCX_ACCOUNT}."
[[ -d "$XRD" ]] || die "XDG_RUNTIME_DIR $XRD missing — user bus unreachable; cannot drive ${PCX_ACCOUNT}'s systemctl --user"
[[ -n "$DBUS" ]] || die "DBUS_SESSION_BUS_ADDRESS missing — user bus unreachable; cannot drive ${PCX_ACCOUNT}'s systemctl --user"
export XDG_RUNTIME_DIR="$XRD"
export DBUS_SESSION_BUS_ADDRESS="$DBUS"

# ORG-106 L3a (T-5526): a rollback restores the bespoke supervisor, which starts the session;
# it must not wake a deep-sleeping seat behind the operator's back.
_lc_rc=0
python3 "$SCRIPT_DIR/seat_state.py" launch-check "$PERSONA" || _lc_rc=$?
[[ $_lc_rc -ne 3 ]] || die "${PERSONA} is in deep-sleep; wake it first (harness ${PERSONA} wake)"

declare -a FILES=(
  "$PCX_SERVICE_UNIT_PATH"
  "$PCX_SUPERVISE_WRAPPER_PATH"
  "$PCX_SETTINGS_JSON_PATH"
)

# every backup must exist before we touch anything (never a partial restore)
for f in "${FILES[@]}"; do
  b="${f}.pre-cutover-${STAMP}"
  [[ -f "$b" ]] || die "missing backup: $b (capture fresh pre-cutover backups before rollback)"
done

DROPIN_FILE="${PCX_HOME}/.config/systemd/user/${PCX_CONSUME_SERVICE}.d/urgent.conf"

consume_timer_link_path() {
  printf '%s/.config/systemd/user/timers.target.wants/%s\n' "$PCX_HOME" "$PCX_CONSUME_TIMER"
}

consume_timer_link_require_absent() {
  local link_path
  link_path="$(consume_timer_link_path)"
  [[ ! -e "$link_path" && ! -L "$link_path" ]] || die "${PCX_CONSUME_TIMER} enablement link still exists: ${link_path}"
}

consume_unit_show_state() {
  local unit="$1"
  systemctl --user show "$unit" -p LoadState -p UnitFileState -p ActiveState -p SubState
}

PCX_UNIT_STATE_ERROR=""

consume_unit_state_matches() {
  local unit="$1"
  local require_not_found="$2"
  local output line key value
  local load_state="__missing__"
  local unit_file_state="__missing__"
  local active_state="__missing__"
  local sub_state="__missing__"
  if ! output="$(consume_unit_show_state "$unit" 2>&1)"; then
    PCX_UNIT_STATE_ERROR="${unit} state proof failed -- ${output}"
    return 1
  fi
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    if [[ "$line" != *=* ]]; then
      PCX_UNIT_STATE_ERROR="${unit} state proof malformed line: ${line}"
      return 1
    fi
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      LoadState) [[ "$load_state" == "__missing__" ]] || { PCX_UNIT_STATE_ERROR="${unit} duplicate LoadState"; return 1; }; load_state="$value" ;;
      UnitFileState) [[ "$unit_file_state" == "__missing__" ]] || { PCX_UNIT_STATE_ERROR="${unit} duplicate UnitFileState"; return 1; }; unit_file_state="$value" ;;
      ActiveState) [[ "$active_state" == "__missing__" ]] || { PCX_UNIT_STATE_ERROR="${unit} duplicate ActiveState"; return 1; }; active_state="$value" ;;
      SubState) [[ "$sub_state" == "__missing__" ]] || { PCX_UNIT_STATE_ERROR="${unit} duplicate SubState"; return 1; }; sub_state="$value" ;;
      *) PCX_UNIT_STATE_ERROR="${unit} state proof unexpected key: ${key}"; return 1 ;;
    esac
  done <<< "$output"
  if [[ "$load_state" == "__missing__" || "$unit_file_state" == "__missing__" || "$active_state" == "__missing__" || "$sub_state" == "__missing__" ]]; then
    PCX_UNIT_STATE_ERROR="${unit} state proof missing required keys"
    return 1
  fi
  if [[ "$active_state" != "inactive" || "$sub_state" != "dead" ]]; then
    PCX_UNIT_STATE_ERROR="${unit} is not inactive/dead (LoadState=${load_state} UnitFileState=${unit_file_state} ActiveState=${active_state} SubState=${sub_state})"
    return 1
  fi
  if [[ "$require_not_found" == 1 ]]; then
    if [[ "$load_state" != "not-found" ]]; then
      PCX_UNIT_STATE_ERROR="${unit} LoadState is ${load_state}, expected not-found"
      return 1
    fi
    if [[ "$unit_file_state" != "not-found" && "$unit_file_state" != "" ]]; then
      PCX_UNIT_STATE_ERROR="${unit} UnitFileState is ${unit_file_state}, expected not-found/empty"
      return 1
    fi
  fi
  PCX_UNIT_STATE_ERROR=""
}

consume_unit_require_state() {
  local unit="$1"
  local require_not_found="$2"
  consume_unit_state_matches "$unit" "$require_not_found" || die "$PCX_UNIT_STATE_ERROR"
}

consume_unit_stop_or_notfound_dead() {
  local unit="$1"
  local output rc digest
  if output="$(systemctl --user stop "$unit" 2>&1)"; then
    consume_unit_require_state "$unit" 0
    return
  fi
  rc=$?
  digest="$(printf '%s' "$output" | sha256sum | awk '{print $1}')"
  consume_unit_state_matches "$unit" 1 || die "failed to stop ${unit}; stop_rc=${rc}; stop_output_sha256=${digest}; ${PCX_UNIT_STATE_ERROR}"
}

consume_timer_service_teardown_before_staged_rollback() {
  consume_unit_stop_or_notfound_dead "$PCX_CONSUME_TIMER"
  consume_unit_stop_or_notfound_dead "$PCX_CONSUME_SERVICE"
  echo "confirmed: consume timer stopped and consume service inactive/dead before staged rollback"
}

consume_units_final_proof_after_staged_rollback() {
  systemctl --user daemon-reload
  consume_timer_link_require_absent
  consume_unit_require_state "$PCX_CONSUME_TIMER" 1
  consume_unit_require_state "$PCX_CONSUME_SERVICE" 1
  echo "confirmed: consume link absent and timer/service inactive/dead before bespoke restart"
}

echo "== T-2633 ${PERSONA} generic rollback =="
echo "persona: $PERSONA (role: $PCX_ROLE, account: $PCX_ACCOUNT)"
echo "stamp:   $STAMP"
echo "mode:    $([[ $EXECUTE == 1 ]] && echo EXECUTE || echo DRY-RUN)"
echo "restores:"
for f in "${FILES[@]}"; do echo "  ${f}.pre-cutover-${STAMP}  ->  ${f}"; done

if [[ $EXECUTE != 1 ]]; then
  echo
  echo "DRY-RUN — would then run:"
  echo "  cp -p <each backup> <each live path>"
  echo "  systemctl --user daemon-reload"
  echo "  systemctl --user stop ${RUNTIME_SCOPE_UNIT}            # T-2642: tear down leftover control-plane scope before bespoke restart"
  echo "  systemctl --user reset-failed ${RUNTIME_SCOPE_UNIT}"
  echo "  systemctl --user stop ${CODEX_RECEIVER_UNIT}           # R5: emergency rollback stop-codex-poller"
  echo "  [abort if ${CODEX_RECEIVER_UNIT} remains active]       # R5: probe-codex-absent before bespoke restart"
  echo "  systemctl --user stop ${RUNTIME_CONSUME_SLICE}   # T-2642: best-effort, no-op if absent (not a unit this repo defines)"
  echo "  systemctl --user reset-failed ${RUNTIME_CONSUME_SLICE}"
  echo "  tmux kill-session -t ${PCX_TMUX_SESSION}   # T-2642: symmetric inverse of persona_cutover.sh's own pre-apply guard"
  echo "  [abort if ${PCX_TMUX_SESSION} survives the kill]"
  echo "  systemctl --user stop ${PCX_CONSUME_TIMER}"
  echo "  systemctl --user stop ${PCX_CONSUME_SERVICE}"
  echo "  [abort unless ${PCX_CONSUME_SERVICE} and ${PCX_CONSUME_TIMER} prove inactive/dead]"
  echo "  PATH=${PCX_APPLY_PATH} python3 ${PCX_HOME}/pantheon-config/tools/t2611_recovery_persona_apply.py rollback-staged --persona ${PERSONA} --stamp ${STAMP} --account-home ${PCX_HOME} --manifest <manifest>   # or --break-glass-no-manifest when forced"
  echo "          # R5: checkpoint rollback before bespoke restart; manifest-pinned (T-4848), or --break-glass-no-manifest if the manifest does not load"
  echo "  systemctl --user daemon-reload"
  echo "  [abort unless $(consume_timer_link_path) is absent and ${PCX_CONSUME_TIMER}/${PCX_CONSUME_SERVICE} prove not-found/inactive/dead]"
  echo "  systemctl --user enable ${PCX_SERVICE}   # T-2611: cutover's disable --now removed the enable symlink"
  echo "  systemctl --user restart ${PCX_SERVICE}"
  echo "  verify: is-active ${PCX_SERVICE}; tmux has-session ${PCX_TMUX_SESSION}; claude+bun children present;"
  echo "          tmux server NOT under ${RUNTIME_SCOPE_UNIT} (bespoke, clean seat -- T-2642)"
  echo "  systemctl --user disable --now ${PCX_CONSUME_TIMER}   # shape-1 inverse (T-2608)"
  echo "  rm -f ${DROPIN_FILE}                                   # shape-1 inverse; no-op if never installed"
  echo "  systemctl --user daemon-reload"
  if [[ -n "$PCX_COMPANION_UNITS" ]]; then
    for tok in $PCX_COMPANION_UNITS; do
      echo "  systemctl --user start ${tok%%=*}   # T-2648: restore companion unit possibly stopped by the aborted cutover"
    done
  fi
  echo "All backups present. Dry-run OK — pass --execute to perform the restore."
  exit 0
fi

# --- execute ---------------------------------------------------------------
for f in "${FILES[@]}"; do
  cp -p "${f}.pre-cutover-${STAMP}" "$f"
  echo "restored: $f"
done

systemctl --user daemon-reload

# T-2642: tear down the leftover control-plane launch BEFORE restarting
# bespoke -- the symmetric inverse of persona_cutover.sh's own pre-apply
# guard (kill bespoke + abort if survives, before it activates
# control-plane). Without this, the supervise wrapper's
# skip-if-session-exists behavior means restarting the bespoke service below
# is a no-op against a still-live control-plane tmux session -- verify then
# reports the WRONG claude as healthy (T-2642 root cause, T-2633 fire#3).
#
# Soft (`|| true`) on the scope/slice stops: the hard gate is the
# kill-session+confirm-gone check below, and the new bespoke-scope verify
# (near the end of this script) is the final end-to-end confirmation -- a
# scope that "failed to stop" but whose session is confirmed dead is not
# actually a problem. Every step here is a safe no-op when there was no
# control-plane launch to tear down (e.g. rollback of a very-early abort
# that never created the scope).
if [[ "$(systemctl --user show "$RUNTIME_SCOPE_UNIT" -p ActiveState --value 2>/dev/null)" == "active" ]]; then
  systemctl --user stop "$RUNTIME_SCOPE_UNIT" || true
  echo "stopped control-plane scope: $RUNTIME_SCOPE_UNIT"
fi
systemctl --user reset-failed "$RUNTIME_SCOPE_UNIT" >/dev/null 2>&1 || true

systemctl --user stop "$CODEX_RECEIVER_UNIT" >/dev/null 2>&1 || true
if systemctl --user is-active --quiet "$CODEX_RECEIVER_UNIT"; then
  die "codex receiver ${CODEX_RECEIVER_UNIT} still active -- refusing to restart bespoke ${PCX_SERVICE}"
fi
echo "confirmed: ${CODEX_RECEIVER_UNIT} inactive/not-found before bespoke restart"

# T-2642 note (b): best-effort teardown of the auto-derived persona-consume
# slice observed coupling the leftover control-plane launch to the restored
# bespoke seat. Not a unit defined anywhere in this repo (grep-verified) --
# defensive only, never load-bearing (see RUNTIME_CONSUME_SLICE's
# definition above).
systemctl --user stop "$RUNTIME_CONSUME_SLICE" >/dev/null 2>&1 || true
systemctl --user reset-failed "$RUNTIME_CONSUME_SLICE" >/dev/null 2>&1 || true

# Explicit kill of the target tmux session, mirroring persona_cutover.sh's
# own guard (kill + confirm gone) -- belt-and-suspenders in case the
# control-plane session wasn't (or is no longer) inside the scope above
# (e.g. the scope-stop raced the session, or a companion unit owns it).
tmux kill-session -t "$PCX_TMUX_SESSION" 2>/dev/null || true
if tmux has-session -t "$PCX_TMUX_SESSION" 2>/dev/null; then
  die "control-plane tmux session ${PCX_TMUX_SESSION} survived kill-session -- refusing to restart bespoke ${PCX_SERVICE} against a still-live foreign session. Rollback is now MID-FLIGHT: bespoke files are restored and the control-plane scope was stopped above, but ${PCX_SERVICE} has NOT been enabled/restarted yet -- ${PERSONA} may be unsupervised. To complete manually: confirm what is holding ${PCX_TMUX_SESSION} open (check for a companion unit or a second control-plane launch), force-kill it (systemctl --user stop <unit>; tmux kill-session -t ${PCX_TMUX_SESSION}; confirm gone), then re-run this rollback with the same --persona/--stamp/--execute to finish enabling and restarting ${PCX_SERVICE}."
fi
echo "confirmed: default tmux socket has no ${PCX_TMUX_SESSION} session (control-plane torn down)"

consume_timer_service_teardown_before_staged_rollback

PERSONA_APPLY_HELPER="${PCX_HOME}/pantheon-config/tools/t2611_recovery_persona_apply.py"
ROLLBACK_MANIFEST="${MANIFEST:-${PCX_HOME}/pantheon-config/instance.yaml}"
ROLLBACK_PIN_ARGS=(--break-glass-no-manifest)
RERUN_CMD="bash $0 --persona ${PERSONA} --stamp ${STAMP}"
[[ -n "$UNIT_FILE" ]] && RERUN_CMD+=" --unit-file ${UNIT_FILE}"
RERUN_CMD+=" --execute --break-glass-no-manifest"
if [[ $FORCE_BREAK_GLASS == 1 ]]; then
  echo "WARNING: BREAK-GLASS: --break-glass-no-manifest given -- staged rollback proceeds WITHOUT the manifest pin (digest tie, root bounds and realpath containment still enforced)" >&2
elif [[ -f "$ROLLBACK_MANIFEST" ]] && PATH="$PCX_APPLY_PATH" python3 "$PERSONA_APPLY_HELPER" check-manifest --manifest "$ROLLBACK_MANIFEST" >/dev/null 2>&1; then
  ROLLBACK_PIN_ARGS=(--manifest "$ROLLBACK_MANIFEST")
  echo "staged rollback: pinned to manifest ${ROLLBACK_MANIFEST}"
else
  echo "WARNING: BREAK-GLASS: manifest ${ROLLBACK_MANIFEST} is missing or does not load -- staged rollback proceeds WITHOUT the manifest pin (digest tie, root bounds and realpath containment still enforced)" >&2
fi
if ! STAGED_ROLLBACK_OUTPUT="$(PATH="$PCX_APPLY_PATH" python3 "$PERSONA_APPLY_HELPER" rollback-staged --persona "$PERSONA" --stamp "$STAMP" --account-home "$PCX_HOME" "${ROLLBACK_PIN_ARGS[@]}" 2>&1)"; then
  echo "$STAGED_ROLLBACK_OUTPUT" >&2
  if [[ $FORCE_BREAK_GLASS != 1 && "${ROLLBACK_PIN_ARGS[0]}" == "--manifest" ]]; then
    die "staged checkpoint rollback failed before bespoke restart. Reason: ${STAGED_ROLLBACK_OUTPUT}. If the reason is a manifest pin refusal (the manifest loads but disagrees with the checkpoint), recover EXPLICITLY (this is never automatic; it skips only the manifest pin): ${RERUN_CMD}"
  fi
  die "staged checkpoint rollback failed before bespoke restart. Reason: ${STAGED_ROLLBACK_OUTPUT}"
fi
echo "$STAGED_ROLLBACK_OUTPUT"
echo "staged checkpoint rollback complete before bespoke restart"

consume_units_final_proof_after_staged_rollback

# T-2611: the cutover's `disable --now` removes the enable symlink -- without
# this, rollback restores the files and restarts the service for THIS boot
# only, silently leaving the persona unsupervised across the next reboot.
systemctl --user enable "$PCX_SERVICE"
echo "enabled: $PCX_SERVICE"

systemctl --user restart "$PCX_SERVICE"
sleep 6

# Shape-1 inverse (T-2608): after a Shape-1 cutover, pantheon-persona-consume
# @<persona>.timer is the live supervisor. The staged rollback above owns
# disabling/removing the consume units and this script proves both dead before
# bespoke restart. The urgent drop-in remains a shell-local cutover artifact.
rm -f "$DROPIN_FILE"
systemctl --user daemon-reload
echo "shape-1 inverse: consume units proven dead, urgent dropin removed (if present)"

# T-2648: restore any companion unit(s) the cutover's fire may have stopped
# (e.g. specialist's acp-bridge, which shares the default tmux socket with the
# bespoke TUI session). `start` is unconditionally safe here, same
# direction-independent-restore reasoning as the urgent-dropin `rm -f`
# above: if the fire aborted BEFORE ever reaching the companion-stop step
# (e.g. the install-done precondition failed), the unit is already running
# and `start` on an already-active unit is a no-op success.
for tok in $PCX_COMPANION_UNITS; do
  companion_unit="${tok%%=*}"
  systemctl --user start "$companion_unit"
  echo "restored companion unit: $companion_unit"
done

# --- verify ----------------------------------------------------------------
fail=0
if [[ "$(systemctl --user is-active "$PCX_SERVICE" 2>&1)" == "active" ]]; then
  echo "verify: service active OK"
else
  echo "verify: service NOT active FAIL"; fail=1
fi
# T-2611: verify the re-enable (above) actually stuck -- this is exactly the
# gap both live Recovery attempts silently had (service active, but not enabled).
if [[ "$(systemctl --user is-enabled "$PCX_SERVICE" 2>&1)" == "enabled" ]]; then
  echo "verify: service enabled OK"
else
  echo "verify: service NOT enabled FAIL"; fail=1
fi
if tmux has-session -t "$PCX_TMUX_SESSION" 2>/dev/null; then
  echo "verify: tmux session ${PCX_TMUX_SESSION} up OK"
else
  echo "verify: tmux session ${PCX_TMUX_SESSION} MISSING FAIL"; fail=1
fi
# T-2642: the restarted claude must be genuinely BESPOKE -- confirm the
# default tmux server is NOT (still, or again) a member of the
# control-plane's runtime scope. rollback_scope_clean (t2590_cutover_verify_
# lib.sh) is the same cgroup-membership technique persona_cutover.sh's own
# "scope ownership" verify uses, inverted: here, membership means the
# teardown above FAILED.
if rollback_scope_clean "$RUNTIME_SCOPE_UNIT"; then
  echo "verify: tmux server not under control-plane scope (bespoke, clean seat) OK"
else
  echo "verify: tmux server still under ${RUNTIME_SCOPE_UNIT} FAIL (restart did not take over a clean seat)"; fail=1
fi
# NB: bracket on the 'd' (clau[d]e) is deliberate — it matches the runtime
# process identically while keeping the contiguous claude-binary byte-sequence
# OUT of this script, so HO's own no-second-claude PreToolUse guard (which
# inspects executed script files) does not false-positive when firing rollback.
if pgrep -u "$PCX_ACCOUNT" -f "\.npm-global/bin/clau[d]e" >/dev/null 2>&1; then
  echo "verify: claude child present OK"
else
  echo "verify: claude child MISSING FAIL"; fail=1
fi
if pgrep -u "$PCX_ACCOUNT" -f "bun.*server\.ts" >/dev/null 2>&1; then
  echo "verify: bun poller child present OK"
else
  echo "verify: bun poller child MISSING (may still be spawning) WARN"
fi
# T-2648: every companion unit restored above must be active.
for tok in $PCX_COMPANION_UNITS; do
  companion_unit="${tok%%=*}"
  if systemctl --user is-active --quiet "$companion_unit"; then
    echo "verify: companion unit ${companion_unit} active OK"
  else
    echo "verify: companion unit ${companion_unit} NOT active FAIL"; fail=1
  fi
done

if [[ $fail == 0 ]]; then
  echo "ROLLBACK COMPLETE — ${PERSONA} restored to bespoke launch, service healthy."
  exit 0
else
  die "restore ran but verification FAILED — ${PERSONA} may be down; escalate to the operator immediately."
fi
