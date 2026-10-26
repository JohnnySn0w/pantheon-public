#!/bin/bash
# send_keys_submit.sh — shared helper: inject text into an agent TUI pane and
# VERIFY submission (submit-verify form).
#
# T-0533: extracted from tools/handoff_and_restart.sh (T-0519) to become the
# single source of truth for "tmux send-keys + confirm submitted." All callers
# that inject text into a Claude Code pane should use this.
#
# T-1048: the submit-VERIFY predicate is now generalized across harness types
# (claude-code, codex, future) and lives in ONE place — tools/submit_verify.py.
# This script keeps the bash-facing contract (the send_keys_submit() function,
# the INJECT_ENTER_DELAY / PENDING_PASTE_SENTINEL / SUBMIT_RETRIES constants, and
# the standalone CLI) but DELEGATES the type-text -> Enter -> verify -> retry loop
# to `submit_verify.py --harness claude-code`. This removes the hard-coded
# Claude-Code paste-sentinel assumption from the load-bearing logic: the
# claude-code paste-sentinel is now just one harness profile among N. Direction:
# the operator, Telegram 3031 ("generalized ... configs for different harness types").
#
# Usage (standalone):
#   tools/send_keys_submit.sh [--harness <h>] [--socket <s>] [--render-wait <s>] <pane> <text>
#   exit 0 = submitted; exit 1 = send-keys failure or an untrusted surface;
#   exit 2 = text delivered but UNPROVEN after retries: still PENDING (busy pane: Claude
#            Code queues input mid-turn), or every capture after Enter was empty
#            (T-5412). Callers may treat it as queued (T-4117) only if they have their
#            own proof (e.g. an ack); it is never a confirmation.
#   exit 3 = REFUSED, render-gated callers only (T-4646): the pane is not at a composer
#            (a known dialog before the paste, or after the render wait a dialog or no
#            composer footer), so no Enter was pressed. Do not re-fire into it.
#
# Usage (sourced by another script — e.g. handoff_and_restart.sh):
#   source "$(dirname "${BASH_SOURCE[0]}")/send_keys_submit.sh"
#   # Defines: send_keys_submit(), INJECT_ENTER_DELAY, PENDING_PASTE_SENTINEL,
#   #          SUBMIT_RETRIES in the caller's namespace.
#   send_keys_submit "$pane" "$text"
#
# Why submit-verify over a blind delay:
#   Two back-to-back tmux send-keys calls (text, then Enter) get coalesced by the
#   TUI paste-mode debounce into a single paste, so the trailing Enter is treated
#   as a literal newline instead of a submit. The fix (T-0519): send text first,
#   wait INJECT_ENTER_DELAY for paste-mode to settle, THEN send Enter as a
#   separate keystroke; after Enter, verify the pane no longer shows the text as
#   pending (per the harness profile) and retry the Enter up to SUBMIT_RETRIES.
#   This is the submit-VERIFY form — stronger than a blind delay.
#
# Root cause context: T-0284 (9-day mystery: text in agent pane without Enter,
# never in any jsonl). T-0533 root cause = HO cross-agent dispatch used raw tmux
# send-keys with no submit-verify discipline. T-1046: the codex consumer of this
# discipline false-passed because the Claude-Code paste sentinel never appears on
# codex's TUI — T-1048 generalizes the predicate so each harness is a config.
#
# Justifies-via:
#   P1 (architecture over policy): single helper + single predicate module
#   enforce the discipline structurally; "remember to add the delay" is not enough.
#   P3 (decisions justify upward): constants, retry logic, and the per-harness
#   profiles are documented in submit_verify.py, not scattered across call sites.

set -u

# T-4849: the pre-Enter dialog-guard bypass (T-4832) is honoured ONLY call-scoped:
# `SEND_KEYS_ALLOW_DIALOG_ENTER=1 send_keys_submit ...` in a sourcing script, or
# --allow-dialog-enter on the CLI. An inherited (exported) value would switch the
# guard off for a whole process tree, so it is cleared here, at source/exec time.
unset SEND_KEYS_ALLOW_DIALOG_ENTER

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUBMIT_VERIFY_PY="${SUBMIT_VERIFY_PY:-$SCRIPT_DIR/submit_verify.py}"

# ---------------------------------------------------------------------------
# Constants (inherit from environment if set — allows test overrides).
# Kept for the sourced-namespace contract (handoff_and_restart.sh references
# them). The submit-verify predicate itself now lives in submit_verify.py;
# INJECT_ENTER_DELAY and SUBMIT_RETRIES are forwarded to it as env overrides.
# PENDING_PASTE_SENTINEL is retained for backward-compat with sourcing callers
# (it is the claude-code profile's sentinel; the profile owns it canonically now).
# ---------------------------------------------------------------------------

# T-5011 PR-B: was INJECT_ENTER_DELAY set by the caller (even to empty)? Read
# BEFORE the default below; only the hermes default depends on it.
_SKS_ENTER_DELAY_EXPLICIT="${INJECT_ENTER_DELAY+set}"
INJECT_ENTER_DELAY="${INJECT_ENTER_DELAY:-0.7}"   # seconds: paste-mode settle time
# T-5011 PR-B: Hermes swallows an Enter that lands within ~1s of the paste
# (T-4924 H0), so a hermes pane waits longer unless the caller set
# INJECT_ENTER_DELAY. Equals submit_verify.HERMES_SETTLE_DELAY_S (test-pinned).
# claude-code/codex: unchanged (INJECT_ENTER_DELAY).
HERMES_ENTER_DELAY="${HERMES_ENTER_DELAY:-1.2}"
PENDING_PASTE_SENTINEL="${PENDING_PASTE_SENTINEL:-[Pasted text}"  # claude-code pending-paste marker
SUBMIT_RETRIES="${SUBMIT_RETRIES:-2}"              # extra Enter attempts if submit unconfirmed
EMPTY_RECAPTURE_S="${EMPTY_RECAPTURE_S:-0.4}"       # T-5412: wait before re-reading an empty capture

# T-1106: the submit-verify predicate is per-harness (submit_verify.py PROFILES).
# Default to claude-code so every existing caller (handoff_and_restart, self_restart,
# keep_going, …) is unchanged; codex panes pass --harness codex (mailbox_wake). A
# hardcoded claude-code here was the false-PASS bug: codex's TUI never shows the
# "[Pasted text" sentinel, so the absence-check always passed (same class as T-1054).
HARNESS="${HARNESS:-claude-code}"                  # submit_verify.py harness profile

# T-2589: optional named-socket targeting (off-prod disposable-scope testing).
# Defaults to the bare default socket for every existing caller -- unchanged.
# Forwarded to `tmux` as `-L <name>` ahead of every tmux subcommand below.
TMUX_SOCKET_NAME="${TMUX_SOCKET_NAME:-}"

# T-4362: optional render gate for the FIRST Enter. 0 (the default) = off: every
# existing caller is unchanged, with no extra command of any kind. When >0, after the
# paste the helper polls the pane (every RENDER_POLL_S) until the verifier sees the
# text PENDING in the composer, and only then starts the settle -> Enter -> verify
# loop. The bound is WALL-CLOCK (T-4371 r1): the wait ends at the first poll after
# RENDER_WAIT_S seconds, so it overruns by at most one poll interval plus one check.
# (A poll COUNT of ceil(wait/interval) was the first bound; it ignored each poll's
# capture + python cost and ran 8x long at load 15. It stays only as a backstop
# against a clock that jumps.) Why: a freshly booted Claude Code TUI took ~2.1s to process a 130-char paste
# (recovery's T-3026 hand-move, reproduced 2026-09-30). The fixed INJECT_ENTER_DELAY Enter
# landed INSIDE the paste and went in as a literal CR ("Removed 1 invisible character
# · review and press Enter to send" on the next Enter), and the verify capture, taken
# before the paste had rendered, read the empty composer as "submitted" (a false rc 0,
# so the retry never ran). With the gate: 2/2 submitted, where 2/2 stuck without it.
# On timeout the helper presses Enter anyway (today's behaviour) and says so.
#
# T-4369: OFF BY DEFAULT on purpose. Only the FIRST paste after a (re)start opts in.
# Measured on a scratch Claude Code TUI, load 7.5-15.6:
#   - The first paste after input-ready rendered in 1.7-3.2s (6/6 over the 0.7s settle).
#   - The next paste, ~3s later, took 0.09-0.17s. Steady-state pastes took 0.09-0.64s
#     (0/42 over).
#   - The gate costs one capture + predicate per poll: median 0.18s, max 0.52s.
# So the opt-ins are the first-paste callers: runtime_controller's fresh-runtime
# submits, kick_verify's resume kick (handoff_and_restart and seat-relaunch),
# recovery_boot_kick, and claude_quota_probe. Steady-state callers keep the fixed settle.
# These must NEVER opt in:
#   - a digit sent to a boot modal (handoff_and_restart / seat-relaunch). It never
#     shows as pending in a composer, so the gate would wait its full bound and then
#     press Enter into whatever screen came next.
#   - activity_monitor. Its 10s timeout kills only sudo and would orphan the helper.
# Revisit if a steady-state strand is ever seen: repress_if_stuck (T-4141) firing,
# or a pane showing "Removed 1 invisible character".
RENDER_WAIT_S="${RENDER_WAIT_S:-0}"
RENDER_POLL_S="${RENDER_POLL_S:-0.1}"

# _sks_usec <decimal seconds> <outvar>: set outvar to integer microseconds. Returns 1,
# leaving outvar alone, unless the value is a plain decimal ("2", "0.5", "8.") with at
# most 6 integer digits: 13+ digits wrap the 64-bit arithmetic (T-4390, rev-t4371 r2
# NIT-1), and nothing waits 11+ days. Builtins only: an ungated call must not fork
# anything (T-4371 r1 R2).
_sks_usec() {
  [[ "$1" =~ ^([0-9]{1,6})([.]([0-9]*))?$ ]] || return 1
  local frac="${BASH_REMATCH[3]}000000"
  printf -v "$2" '%d' "$(( 10#${BASH_REMATCH[1]} * 1000000 + 10#${frac:0:6} ))"
}

# _sks_now_us <outvar>: wall-clock microseconds. EPOCHREALTIME (bash >= 5) carries the
# locale's decimal separator, hence [.,]. Older bash falls back to whole SECONDS.
_sks_now_us() {
  if [[ -n "${EPOCHREALTIME:-}" ]]; then
    printf -v "$1" '%s' "${EPOCHREALTIME/[.,]/}"
  else
    printf -v "$1" '%d' "$(( SECONDS * 1000000 ))"
  fi
}

# ---------------------------------------------------------------------------
# send_keys_submit <pane> <text>
#
# Inject <text> into tmux <pane> and confirm the TUI submitted it, using the
# claude-code harness profile.
#
# Orchestration stays in BASH (the tmux copy-mode / load-buffer / paste-buffer /
# send-keys calls are DIRECT children of this script's process) — load-bearing for
# the T-0584 wall-clock watchdog in handoff_and_restart.sh, which reaps a hung
# send-keys via `pkill -P "$SCRIPT_PID"` (parent-based: only direct children are
# killed). The display-message / capture-pane reads and the predicate run inside
# `$( ... || ... )` command substitutions, so they are GRANDCHILDREN that pkill -P
# does not reach (T-4369 audit; true of the verify loop since T-2268, and of the
# render gate). Callers that must bound them run this helper in its own process
# group and kill the group (runtime_controller, T-4390).
# Routing tmux through a python subprocess would make a hung send-keys a
# GRANDCHILD the watchdog can't reap (it would survive holding the flock — the
# exact 2026-05-25 failure mode). So python is called ONLY for the per-harness
# PREDICATE ("is the text still pending?"), via submit_verify.py --pending-check
# (no tmux I/O, returns fast). This is the T-1048 shape: "bash keeps the
# orchestration, python owns the predicate" — one predicate home, watchdog-safe.
#
# Returns 0 on confirmed submit, 2 if the verifier still saw the text PENDING after
# SUBMIT_RETRIES (T-4117: a busy pane queues it) or only empty captures (T-5412:
# unproven), 1 on any send-keys failure or an
# untrusted (mode-frozen) final surface. Before T-4117 all unconfirmed cases were 1.
# Fail-safe: a missing/erroring predicate yields a non-'submitted'
# verdict -> we keep retrying and ultimately report unconfirmed, NEVER false-pass.
# ---------------------------------------------------------------------------

# _sks_effective_enter_delay <harness> [outvar]: the settle before each Enter.
# Builtins only (no fork); prints when no outvar is given (tests, introspection).
_sks_effective_enter_delay() {
  local _sks_delay="$INJECT_ENTER_DELAY"
  if [[ "$1" == "hermes" && -z "$_SKS_ENTER_DELAY_EXPLICIT" ]]; then
    _sks_delay="$HERMES_ENTER_DELAY"
  fi
  if [[ -n "${2:-}" ]]; then
    printf -v "$2" '%s' "$_sks_delay"
  else
    printf '%s\n' "$_sks_delay"
  fi
}

send_keys_submit() {
  local pane="$1" text="$2" harness="${3:-$HARNESS}" socket="${4:-$TMUX_SOCKET_NAME}"
  local attempt pane_text verdict last_check="none"
  # T-2589: every tmux invocation below is prefixed with this (possibly
  # empty) array. Unset/empty socket -> unchanged default-socket behavior.
  local -a tmux_socket_args=()
  [[ -n "$socket" ]] && tmux_socket_args=(-L "$socket")
  if [[ ! -f "$SUBMIT_VERIFY_PY" ]]; then
    echo "[send_keys_submit] ERROR: submit verifier not found at $SUBMIT_VERIFY_PY" >&2
    return 1
  fi
  # T-2259 (second fault, found live on archivist's seat 2026-07-28): a pane
  # sitting in tmux copy-mode SILENTLY EATS THE SUBMIT. paste-buffer still
  # lands the text in the composer, but the follow-up `send-keys Enter` is
  # consumed by copy-mode's key table instead of reaching the TUI — so the
  # prompt is typed and never submitted. That is the exact T-0284 signature
  # (text visible in the pane, nothing in any jsonl) which cost 9 days once.
  # capture-pane also returns the FROZEN copy-mode view, so the submit-verify
  # predicate reads a stale pane and can vote either way for the wrong reason.
  # `copy-mode -q` cancels copy mode (and any other mode) if the pane is in
  # one, and is a verified no-op when it is not — checked both directions
  # against a live tmux before this was written, because getting it backwards
  # would put every HEALTHY pane into copy-mode. Non-fatal by construction:
  # clearing the mode is a precondition we try to establish, never a reason to
  # abort an injection that might otherwise have worked.
  tmux "${tmux_socket_args[@]}" copy-mode -q -t "$pane" 2>/dev/null || true
  # T-4362 render gate (see RENDER_WAIT_S), validated before the paste because T-4646's
  # pre-paste check below runs only when the gate is on.
  # Off (0, or not a plain decimal) runs builtins only (T-4371 r1 R2).
  # T-4390 (rev-t4371 r2 NIT-2): the CLI refuses a bad --render-wait, so a bad env
  # value must not turn the gate off in silence either. echo is a builtin: still no fork.
  local render_wait_us=0 render_poll_us=0
  if ! _sks_usec "$RENDER_WAIT_S" render_wait_us; then
    echo "[send_keys_submit] WARNING: RENDER_WAIT_S='$RENDER_WAIT_S' is not plain decimal seconds (at most 6 integer digits); render gate off" >&2
    render_wait_us=0
  fi
  if (( render_wait_us > 0 )) && { ! _sks_usec "$RENDER_POLL_S" render_poll_us || (( render_poll_us == 0 )); }; then
    echo "[send_keys_submit] WARNING: RENDER_POLL_S='$RENDER_POLL_S' is not a positive plain decimal; render gate off" >&2
    render_poll_us=0
  fi
  # T-4646: a render-gated caller targets a freshly booted TUI, which can open on a
  # startup prompt instead of the composer. On 2026-10-05, 2.1.289's auto-mode nudge
  # took a kick's paste and then its Enter, which picked the highlighted "Yes".
  # Refuse on positive dialog evidence only (the footer may not be drawn yet).
  if (( render_wait_us > 0 && render_poll_us > 0 )); then
    local pre_verdict
    pre_verdict="$(tmux "${tmux_socket_args[@]}" capture-pane -t "$pane" -p 2>/dev/null \
      | python3 "$SUBMIT_VERIFY_PY" --harness "$harness" --paste-check 2>/dev/null || echo ok)"
    if [[ "$pre_verdict" == refuse:* ]]; then
      echo "[send_keys_submit] ERROR: $pane shows a dialog, not a composer (${pre_verdict#refuse:}); refusing to paste or press Enter [T-4646]" >&2
      return 3
    fi
  fi
  # T-2259: deliver the text via load-buffer + paste-buffer, NOT send-keys -l.
  # tmux re-parses client argv and splits on words ending in an unescaped ';'
  # — so any text carrying semicolons (every mailbox_wake kick does: the drain
  # command is `python3 -c "import sys; ..."`) shatters into bogus tmux
  # commands and the injection fails ("no current client", observed live on
  # archivist's pane while the same text pasted fine). Buffers carry the text
  # as opaque bytes; nothing is parsed. -d frees the buffer after pasting.
  if ! printf '%s' "$text" | tmux "${tmux_socket_args[@]}" load-buffer -b send_keys_submit -; then
    echo "[send_keys_submit] ERROR: tmux load-buffer failed" >&2
    return 1
  fi
  if ! tmux "${tmux_socket_args[@]}" paste-buffer -d -b send_keys_submit -t "$pane"; then
    echo "[send_keys_submit] ERROR: tmux paste-buffer failed for pane $pane" >&2
    return 1
  fi
  # T-4362 render gate. Only an explicit 'pending' verdict ends the wait early; an
  # in-mode surface is never read (T-2268), so it waits it out.
  if (( render_wait_us > 0 && render_poll_us > 0 )); then
    local render_verdict="none" render_polls=0 render_max_polls render_start_us render_now_us
    local render_elapsed_us render_elapsed
    render_max_polls=$(( (render_wait_us + render_poll_us - 1) / render_poll_us ))
    _sks_now_us render_start_us
    while :; do
      render_polls=$(( render_polls + 1 ))
      if [[ "$(tmux "${tmux_socket_args[@]}" display-message -p -t "$pane" '#{pane_in_mode}' 2>/dev/null || echo 1)" == "0" ]]; then
        pane_text="$(tmux "${tmux_socket_args[@]}" capture-pane -t "$pane" -p 2>/dev/null || true)"
        render_verdict="$(printf '%s' "$pane_text" \
          | python3 "$SUBMIT_VERIFY_PY" --harness "$harness" --pending-check --text "$text")"
        if [[ "$render_verdict" == "pending" ]]; then break; fi
      fi
      _sks_now_us render_now_us
      if (( render_now_us - render_start_us >= render_wait_us || render_polls >= render_max_polls )); then
        break
      fi
      sleep "$RENDER_POLL_S"
    done
    if [[ "$render_verdict" != "pending" ]]; then
      _sks_now_us render_now_us
      render_elapsed_us=$(( render_now_us - render_start_us ))
      printf -v render_elapsed '%d.%d' "$(( render_elapsed_us / 1000000 ))" "$(( render_elapsed_us / 100000 % 10 ))"
      # T-4646: the fallback Enter is only safe on a composer. A fresh capture, because
      # an in-mode poll leaves pane_text stale. An unreadable pane keeps the old Enter.
      local anyway_verdict
      anyway_verdict="$(tmux "${tmux_socket_args[@]}" capture-pane -t "$pane" -p 2>/dev/null \
        | python3 "$SUBMIT_VERIFY_PY" --harness "$harness" --enter-anyway-check 2>/dev/null || echo ok)"
      if [[ "$anyway_verdict" == refuse:* ]]; then
        echo "[send_keys_submit] ERROR: text not visible in a composer after ${render_elapsed}s" \
             "and $pane is not at one (${anyway_verdict#refuse:}); NOT pressing Enter [T-4646]" >&2
        return 3
      fi
      echo "[send_keys_submit] WARNING: text not visible in the composer after ${render_elapsed}s" \
           "(render wait ${RENDER_WAIT_S}s, ${render_polls} polls); pressing Enter anyway" \
           "(the submit verdict below may be unreliable)" >&2
    fi
  fi
  local enter_delay
  _sks_effective_enter_delay "$harness" enter_delay
  for (( attempt=0; attempt<=SUBMIT_RETRIES; attempt++ )); do
    sleep "$enter_delay"
    # T-5380 r2: before every RETRY Enter, look again. The previous Enter may have
    # landed late, inside the settle window, and a retry would then be an extra Enter
    # into a live seat. The first Enter stays unconditional. A pane in a tmux mode
    # shows a FROZEN view (T-2268), so it casts no vote here: the Enter path below
    # handles it as before. A verifier error is not "submitted" either.
    if (( attempt > 0 )) \
       && [[ "$(tmux "${tmux_socket_args[@]}" display-message -p -t "$pane" '#{pane_in_mode}' 2>/dev/null || echo 1)" == "0" ]]; then
      pane_text="$(tmux "${tmux_socket_args[@]}" capture-pane -t "$pane" -p 2>/dev/null || true)"
      verdict="$(printf '%s' "$pane_text" \
        | python3 "$SUBMIT_VERIFY_PY" --harness "$harness" --pending-check --text "$text" 2>/dev/null)"
      # A real pane is never empty: an empty or whitespace-only capture (a failed
      # capture-pane reads "") casts no vote, so the retry Enter fires (round 3).
      if [[ "$verdict" == "submitted" && "$pane_text" == *[![:space:]]* ]]; then
        return 0   # the earlier Enter landed late: no further Enter
      fi
    fi
    # T-4832: re-check right before EVERY Enter. A dialog that opened after the
    # pre-paste checks (the T-4828 step-3 guard's TOCTOU window) has its option
    # cursor on "Yes", so this Enter would approve it. Bottom region only.
    # - SEND_KEYS_ALLOW_DIALOG_ENTER=1 (call-scoped only, T-4849) skips it for ONE call: the boot-dialog dismiss,
    #   whose job is to Enter into a dialog. Callers set it only after the boot
    #   sentinel matched and BOOT_DISMISS_DENYLIST did not (handoff_and_restart.sh,
    #   pantheon-seat-relaunch.sh). It is never inferred from the text.
    # - A copy-mode pane shows a frozen view (T-2268), so it is not read and the
    #   Enter goes ahead unchecked: an accepted gap, as before T-4832.
    # - A verifier error fails open (the Enter goes ahead) with a WARNING.
    if [[ "${SEND_KEYS_ALLOW_DIALOG_ENTER:-0}" != "1" ]] \
       && [[ "$(tmux "${tmux_socket_args[@]}" display-message -p -t "$pane" '#{pane_in_mode}' 2>/dev/null || echo 1)" == "0" ]]; then
      local enter_verdict enter_rc=0
      enter_verdict="$(tmux "${tmux_socket_args[@]}" capture-pane -J -t "$pane" -p 2>/dev/null \
        | python3 "$SUBMIT_VERIFY_PY" --harness "$harness" --enter-check 2>/dev/null)" || enter_rc=$?
      if (( enter_rc != 0 )); then
        echo "[send_keys_submit] WARNING: dialog re-check verifier failed (rc=$enter_rc); pressing Enter unchecked [T-4832]" >&2
      elif [[ "$enter_verdict" == refuse:* ]]; then
        echo "[send_keys_submit] ERROR: refusing to inject: $pane shows a dialog after the paste" \
             "(${enter_verdict#refuse:}); NOT pressing Enter [T-4832]" >&2
        return 3
      fi
    fi
    if ! tmux "${tmux_socket_args[@]}" send-keys -t "$pane" Enter; then
      echo "[send_keys_submit] ERROR: tmux send-keys (Enter) failed for pane $pane" >&2
      return 1
    fi
    # T-2268: REFUSE TO VOTE ON AN UNTRUSTWORTHY SURFACE.
    # capture-pane returns the FROZEN mode view when the pane is in copy-mode,
    # so during the exact fault this predicate exists to catch, it reads a
    # stale snapshot of a previously-working pane and reports SUCCESS —
    # indefinitely. That is strictly worse than having no verifier: a missing
    # check is visibly missing, this one emits positive confirmation while
    # nothing is submitted. It is why T-2259 read as unsolvable for a night.
    #
    # T-2259's `copy-mode -q` before delivery removes the known TRIGGER; it
    # does not make the verifier independent of the fault. This does the part
    # that generalises: ask whether the surface is trustworthy BEFORE reading
    # it, and when it is not, decline to return a verdict rather than return a
    # false one. An unreadable mode query counts as untrusted (`|| echo 1`) —
    # fail-safe, matching this helper's documented "NEVER false-pass" contract.
    # Self-heals: cancel the mode and retry, so a pane that re-entered
    # copy-mode mid-sequence still converges instead of hard-failing.
    in_mode="$(tmux "${tmux_socket_args[@]}" display-message -p -t "$pane" '#{pane_in_mode}' 2>/dev/null || echo 1)"
    if [[ "$in_mode" != "0" ]]; then
      echo "[send_keys_submit] WARNING: pane $pane is in a tmux mode (pane_in_mode=$in_mode);" \
           "capture-pane would return the FROZEN view — declining to verify, cancelling and retrying" >&2
      tmux "${tmux_socket_args[@]}" copy-mode -q -t "$pane" 2>/dev/null || true
      last_check="untrusted"
      continue
    fi
    pane_text="$(tmux "${tmux_socket_args[@]}" capture-pane -t "$pane" -p 2>/dev/null || true)"
    # T-5412: one blank read can be a redraw in flight. Re-read once after
    # EMPTY_RECAPTURE_S; only two empty reads in a row count as empty.
    if [[ "$pane_text" != *[![:space:]]* ]]; then
      sleep "$EMPTY_RECAPTURE_S"
      pane_text="$(tmux "${tmux_socket_args[@]}" capture-pane -t "$pane" -p 2>/dev/null || true)"
    fi
    # T-5412: a real pane is never empty. An empty or whitespace-only capture (a
    # failed capture-pane reads "") has no composer region, so the predicate would
    # say "submitted" with no proof. It is UNKNOWN: fall through to the next
    # iteration's pre-retry re-check and bounded retry Enter; ends in rc 2, never 0.
    if [[ "$pane_text" != *[![:space:]]* ]]; then
      last_check="empty"
      echo "[send_keys_submit] submit unverified (empty capture after Enter); retrying ($((attempt+1))/$SUBMIT_RETRIES)" >&2
      continue
    fi
    verdict="$(printf '%s' "$pane_text" \
      | python3 "$SUBMIT_VERIFY_PY" --harness "$harness" --pending-check --text "$text")"
    if [[ "$verdict" == "submitted" ]]; then
      return 0   # predicate confirms the input box no longer holds the pending text
    fi
    # Only an explicit 'pending' verdict counts as queued; a missing/erroring
    # predicate must never be read as a busy pane (it stays rc 1).
    if [[ "$verdict" == "pending" ]]; then last_check="pending"; else last_check="unverified"; fi
    echo "[send_keys_submit] submit unconfirmed (still pending); retrying Enter ($((attempt+1))/$SUBMIT_RETRIES)" >&2
  done
  echo "[send_keys_submit] ERROR: prompt submit unconfirmed after $SUBMIT_RETRIES Enter retries (pane $pane)" >&2
  # T-4117: 2 only when the verifier itself saw the text still pending (a busy
  # pane queues it); an untrusted final surface stays 1 (the Enter may be eaten).
  # T-5412: "empty" (every read since the last good one was blank) is unproven, not
  # a failure: rc 2 like pending, which handoff_and_restart.sh / seat-relaunch gate
  # on the ack. rc 1 would hard-fail them (exit 3).
  [[ "$last_check" == "pending" || "$last_check" == "empty" ]] && return 2
  return 1
}

# ---------------------------------------------------------------------------
# repress_if_stuck <pane> <text> [harness] [socket]  (T-4141)
#
# Press Enter ONCE if <text> (which the caller injected earlier) is still sitting in
# the composer of a pane that is not generating, not running a background agent,
# and not showing a modal. The decision is submit_verify.py --repress-check
# (should_repress); this function only does the tmux I/O. It NEVER re-pastes, so it
# can't double-deliver. Returns 0 iff Enter was sent. Callers bound how often they
# call it. Why: a queued or eaten submit can leave the text in the composer after the
# pane goes idle; later injections then append to it and nothing submits (artisan,
# 2026-09-28, stuck ~1h until one manual Enter). Fail-safe: a tmux or predicate
# error, or an untrusted (in-mode) surface, returns 1 with no keystroke.
# ---------------------------------------------------------------------------
repress_if_stuck() {
  local pane="$1" text="$2" harness="${3:-$HARNESS}" socket="${4:-$TMUX_SOCKET_NAME}"
  local -a tmux_socket_args=()
  [[ -n "$socket" ]] && tmux_socket_args=(-L "$socket")
  local in_mode pane_text verdict
  in_mode="$(tmux "${tmux_socket_args[@]}" display-message -p -t "$pane" '#{pane_in_mode}' 2>/dev/null || echo 1)"
  [[ "$in_mode" == "0" ]] || return 1
  pane_text="$(tmux "${tmux_socket_args[@]}" capture-pane -t "$pane" -p 2>/dev/null)" || return 1
  verdict="$(printf '%s' "$pane_text" \
    | python3 "$SUBMIT_VERIFY_PY" --harness "$harness" --repress-check --text "$text" 2>/dev/null)" || return 1
  [[ "$verdict" == "repress" ]] || return 1
  tmux "${tmux_socket_args[@]}" send-keys -t "$pane" Enter 2>/dev/null || return 1
  return 0
}

# ---------------------------------------------------------------------------
# Standalone mode: when executed directly (not sourced), run as CLI tool.
# When sourced, only the function + constants above are imported — nothing
# below runs.
# ---------------------------------------------------------------------------

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  # Optional leading --harness <h> (default claude-code), --socket <s>
  # (T-2589, default the bare default socket) and/or --render-wait <seconds>
  # (T-4362, default RENDER_WAIT_S, i.e. off), in any order; then <pane> <text>.
  cli_harness="$HARNESS"
  cli_socket="$TMUX_SOCKET_NAME"
  cli_allow_dialog_enter=0
  usage() {
    echo "Usage: $(basename "$0") [--harness <h>] [--socket <s>] [--render-wait <s>] [--allow-dialog-enter] <pane> <text>" >&2
  }
  while [[ "${1:-}" == "--harness" || "${1:-}" == "--socket" || "${1:-}" == "--render-wait" \
           || "${1:-}" == "--allow-dialog-enter" ]]; do
    # T-4849: the boot-dialog-dismiss bypass, explicit per invocation (never inherited).
    if [[ "$1" == "--allow-dialog-enter" ]]; then
      cli_allow_dialog_enter=1
      shift
      continue
    fi
    if [[ $# -lt 2 ]]; then
      usage
      exit 1
    fi
    case "$1" in
      --harness) cli_harness="$2" ;;
      --socket) cli_socket="$2" ;;
      --render-wait)
        if [[ ! "$2" =~ ^[0-9]{1,6}([.][0-9]+)?$ ]]; then
          usage
          exit 1
        fi
        RENDER_WAIT_S="$2" ;;
    esac
    shift 2
  done
  if [[ $# -ne 2 ]]; then
    usage
    exit 1
  fi
  SEND_KEYS_ALLOW_DIALOG_ENTER="$cli_allow_dialog_enter" send_keys_submit "$1" "$2" "$cli_harness" "$cli_socket"
  exit $?
fi
