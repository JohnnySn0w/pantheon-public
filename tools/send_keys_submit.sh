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
  local pane="$1" text="$2" harness="${3:-$HARNESS}" attempt pane_text verdict
  if [[ ! -f "$SUBMIT_VERIFY_PY" ]]; then
    echo "[send_keys_submit] ERROR: submit verifier not found at $SUBMIT_VERIFY_PY" >&2
    return 1
  fi
  if ! tmux send-keys -t "$pane" -l "$text"; then
    echo "[send_keys_submit] ERROR: tmux send-keys (text) failed for pane $pane" >&2
    return 1
  fi
  for (( attempt=0; attempt<=SUBMIT_RETRIES; attempt++ )); do
    sleep "$INJECT_ENTER_DELAY"
    if ! tmux send-keys -t "$pane" Enter; then
      echo "[send_keys_submit] ERROR: tmux send-keys (Enter) failed for pane $pane" >&2
      return 1
    fi
    pane_text="$(tmux capture-pane -t "$pane" -p 2>/dev/null || true)"
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
  # Optional leading --harness <h> (default claude-code); then <pane> <text>.
  cli_harness="$HARNESS"
  if [[ "${1:-}" == "--harness" ]]; then
    if [[ $# -lt 3 ]]; then
      echo "Usage: $(basename "$0") [--harness <h>] <pane> <text>" >&2
      exit 1
    fi
    cli_harness="$2"; shift 2
  fi
  if [[ $# -ne 2 ]]; then
    echo "Usage: $(basename "$0") [--harness <h>] <pane> <text>" >&2
    exit 1
  fi
  send_keys_submit "$1" "$2" "$cli_harness"
  exit $?
fi
