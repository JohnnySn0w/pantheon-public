#!/usr/bin/env bash
# T-4330: hold the codex Telegram poller down for a hand-move window, so a
# transient codex fallback cannot start a getUpdates consumer on the seat's bot
# token.
#
# WHY. During a hand-move the consume bootstrap can fall back to codex. Its
# claude-to-codex post handoff then starts codex-telegram-poller.service on the
# seat's token (recovery, 2026-09-29 20:01:15-20Z), and the move's rollback tears it
# down seconds later. A codex poller on a seat with no poller.offset runs the
# codex-config first-boot drain-and-skip: it CONFIRMS every queued update and
# injects none. A move's dark window (phase 2 on) is exactly when the operator's
# messages queue. Held, the start fails, the controller records
# receiver_handoff_failed and compensates, gate A rolls back, and the queue waits
# for the restored claude's plugin: a delay, never a loss.
#
# HOW. A drop-in under $XDG_RUNTIME_DIR/systemd/user/<unit>.d/ adding a failing
# ExecStartPre and Restart=no. NOT `systemctl --user mask --runtime`: the unit
# file lives in ~/.config/systemd/user, which outranks the runtime dir, so that
# mask is a silent no-op (measured 2026-09-29: link created, `start` rc 0, unit
# active). Drop-ins from every dir merge, so this one applies. It lives on
# tmpfs, so a reboot also clears it.
#
# Usage: codex_poller_hold.sh hold <stamp> | release | status
#   status: exit 0 = held (prints the hold's header line), 1 = not held.
# Env: CODEX_POLLER_UNIT (default codex-telegram-poller.service),
#      SYSTEMCTL (default systemctl; a test seam).
set -uo pipefail

UNIT="${CODEX_POLLER_UNIT:-codex-telegram-poller.service}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
: "${XDG_RUNTIME_DIR:?XDG_RUNTIME_DIR is unset (run as the seat account)}"
DIR="$XDG_RUNTIME_DIR/systemd/user/$UNIT.d"
HOLD="$DIR/t4330-handmove-hold.conf"

case "${1:-}" in
  hold)
    stamp="${2:?usage: codex_poller_hold.sh hold <stamp>}"
    mkdir -p "$DIR" || exit 1
    # No % or $ below: systemd would expand them in an Exec line.
    printf '%s\n' \
      "# T-4330 hand-move hold ($stamp). Remove with: tools/codex_poller_hold.sh release" \
      '[Service]' \
      'Restart=no' \
      "ExecStartPre=/bin/sh -c 'echo held by the T-4330 hand-move hold $stamp >&2; exit 1'" \
      > "$HOLD.tmp" && mv "$HOLD.tmp" "$HOLD" || exit 1
    # An unloaded hold holds nothing: fail, and don't leave the file for the next
    # reload to pick up behind the caller's back (T-4335).
    "$SYSTEMCTL" --user daemon-reload || { rm -f "$HOLD"; exit 1; }
    ;;
  release)
    rm -f "$HOLD" "$HOLD.tmp"
    rmdir "$DIR" 2>/dev/null || true
    "$SYSTEMCTL" --user daemon-reload || exit 1
    # A start refused while held leaves the unit `failed`; clear it.
    "$SYSTEMCTL" --user reset-failed "$UNIT" 2>/dev/null || true
    ;;
  status)
    [ -e "$HOLD" ] || exit 1
    head -n 1 "$HOLD"  # the header names the stamp that took it
    ;;
  *)
    echo "usage: codex_poller_hold.sh hold <stamp> | release | status" >&2
    exit 2
    ;;
esac
