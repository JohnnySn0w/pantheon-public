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
#   tools/send_keys_submit.sh <pane> <text>
#   exit 0 = submitted; exit 1 = send-keys failure or unconfirmed after retries
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

INJECT_ENTER_DELAY="${INJECT_ENTER_DELAY:-0.7}"   # seconds: paste-mode settle time
PENDING_PASTE_SENTINEL="${PENDING_PASTE_SENTINEL:-[Pasted text}"  # claude-code pending-paste marker
SUBMIT_RETRIES="${SUBMIT_RETRIES:-2}"              # extra Enter attempts if submit unconfirmed

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

# ---------------------------------------------------------------------------
# send_keys_submit <pane> <text>
#
# Inject <text> into tmux <pane> and confirm the TUI submitted it, using the
# claude-code harness profile.
#
# Orchestration stays in BASH (the tmux send-keys / capture-pane calls are
# DIRECT children of this script's process) — load-bearing for the T-0584
# wall-clock watchdog in handoff_and_restart.sh, which reaps a hung send-keys
# via `pkill -P "$SCRIPT_PID"` (parent-based: only direct children are killed).
# Routing tmux through a python subprocess would make a hung send-keys a
# GRANDCHILD the watchdog can't reap (it would survive holding the flock — the
# exact 2026-05-25 failure mode). So python is called ONLY for the per-harness
# PREDICATE ("is the text still pending?"), via submit_verify.py --pending-check
# (no tmux I/O, returns fast). This is the T-1048 shape: "bash keeps the
# orchestration, python owns the predicate" — one predicate home, watchdog-safe.
#
# Returns 0 on confirmed submit, 1 on any send-keys failure or unconfirmed after
# SUBMIT_RETRIES. Fail-safe: a missing/erroring predicate yields a non-'submitted'
# verdict -> we keep retrying and ultimately report unconfirmed, NEVER false-pass.
# ---------------------------------------------------------------------------

send_keys_submit() {
  local pane="$1" text="$2" harness="${3:-$HARNESS}" socket="${4:-$TMUX_SOCKET_NAME}"
  local attempt pane_text verdict
  # T-2589: every tmux invocation below is prefixed with this (possibly
  # empty) array. Unset/empty socket -> unchanged default-socket behavior.
  local -a tmux_socket_args=()
  [[ -n "$socket" ]] && tmux_socket_args=(-L "$socket")
  if [[ ! -f "$SUBMIT_VERIFY_PY" ]]; then
    echo "[send_keys_submit] ERROR: submit verifier not found at $SUBMIT_VERIFY_PY" >&2
    return 1
  fi
  # T-2259 (second fault, found live on mnemosyne's seat 2026-07-28): a pane
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
  # T-2259: deliver the text via load-buffer + paste-buffer, NOT send-keys -l.
  # tmux re-parses client argv and splits on words ending in an unescaped ';'
  # — so any text carrying semicolons (every mailbox_wake kick does: the drain
  # command is `python3 -c "import sys; ..."`) shatters into bogus tmux
  # commands and the injection fails ("no current client", observed live on
  # mnemosyne's pane while the same text pasted fine). Buffers carry the text
  # as opaque bytes; nothing is parsed. -d frees the buffer after pasting.
  if ! printf '%s' "$text" | tmux "${tmux_socket_args[@]}" load-buffer -b send_keys_submit -; then
    echo "[send_keys_submit] ERROR: tmux load-buffer failed" >&2
    return 1
  fi
  if ! tmux "${tmux_socket_args[@]}" paste-buffer -d -b send_keys_submit -t "$pane"; then
    echo "[send_keys_submit] ERROR: tmux paste-buffer failed for pane $pane" >&2
    return 1
  fi
  for (( attempt=0; attempt<=SUBMIT_RETRIES; attempt++ )); do
    sleep "$INJECT_ENTER_DELAY"
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
      continue
    fi
    pane_text="$(tmux "${tmux_socket_args[@]}" capture-pane -t "$pane" -p 2>/dev/null || true)"
    verdict="$(printf '%s' "$pane_text" \
      | python3 "$SUBMIT_VERIFY_PY" --harness "$harness" --pending-check --text "$text")"
    if [[ "$verdict" == "submitted" ]]; then
      return 0   # predicate confirms the input box no longer holds the pending text
    fi
    echo "[send_keys_submit] submit unconfirmed (still pending); retrying Enter ($((attempt+1))/$SUBMIT_RETRIES)" >&2
  done
  echo "[send_keys_submit] ERROR: prompt submit unconfirmed after $SUBMIT_RETRIES Enter retries (pane $pane)" >&2
  return 1
}

# ---------------------------------------------------------------------------
# Standalone mode: when executed directly (not sourced), run as CLI tool.
# When sourced, only the function + constants above are imported — nothing
# below runs.
# ---------------------------------------------------------------------------

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  # Optional leading --harness <h> (default claude-code) and/or --socket <s>
  # (T-2589, default the bare default socket), in either order; then
  # <pane> <text>.
  cli_harness="$HARNESS"
  cli_socket="$TMUX_SOCKET_NAME"
  usage() {
    echo "Usage: $(basename "$0") [--harness <h>] [--socket <s>] <pane> <text>" >&2
  }
  while [[ "${1:-}" == "--harness" || "${1:-}" == "--socket" ]]; do
    if [[ $# -lt 2 ]]; then
      usage
      exit 1
    fi
    case "$1" in
      --harness) cli_harness="$2" ;;
      --socket) cli_socket="$2" ;;
    esac
    shift 2
  done
  if [[ $# -ne 2 ]]; then
    usage
    exit 1
  fi
  send_keys_submit "$1" "$2" "$cli_harness" "$cli_socket"
  exit $?
fi
