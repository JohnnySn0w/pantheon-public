"""tools/kick_verify.py — post-kick verification + bounded re-fire (T-0899).

The silent kick-drop (root cause of the 1h46m Recovery silence, 2026-06-06): the step-8
`send_keys_submit` in handoff_and_restart.sh can land in the boot window before the fresh
TUI input listener is live, so the keystrokes/submit never register and the delivered turn
is never DISPATCHED — the session sits idle. The blind 3s KICK_READINESS_SETTLE was meant
to prevent this and proved insufficient on slower boots (claude + plugin + bun).

This replaces "settle then fire once, hope it took" with "fire, VERIFY a turn actually
started, re-fire if it didn't (bounded)". The verification signal is the same battle-tested
one keep_going/inbox_consumer use (T-0716): a dispatched turn makes the pane show the
actively-generating indicator; a dropped kick leaves it idle.

Side effects (inject / capture / sleep) are injected so the orchestration is fully
unit-testable. The LIVE wiring into handoff_and_restart.sh step 8 is the SUPERVISED step —
this is the wake mechanism itself, and every restart-stack change gets a supervised
first-fire. This module is the tested logic that wiring will call.

Caveat: a kick whose turn starts AND finishes faster than the first poll would read as
"dropped" → a spurious re-fire (a duplicate prompt). The post-restart kick prompt ("read
HANDOFF, post status") is a multi-second turn, so this is unlikely; noted, not solved here.
"""
from __future__ import annotations

import time

# Actively-generating indicator — mirrors keep_going._BUSY / inbox_consumer (T-0716).
_BUSY = "esc to interrupt"
_BOTTOM_REGION_LINES = 6


def kicked_turn_started(pane: str) -> bool:
    """True if the pane's live bottom region shows an actively-generating turn — i.e. the
    kick dispatched. A dropped kick leaves the session idle (no generating indicator)."""
    non_blank = [ln for ln in pane.splitlines() if ln.strip()]
    if not non_blank:
        return False
    bottom = "\n".join(non_blank[-_BOTTOM_REGION_LINES:])
    return _BUSY in bottom


class KickResult:
    def __init__(self, ok: bool, attempts: int, detail: str = ""):
        self.ok = ok
        self.attempts = attempts
        self.detail = detail

    def __repr__(self) -> str:
        return f"KickResult(ok={self.ok}, attempts={self.attempts}, detail={self.detail!r})"


def deliver_kick_verified(inject, capture, pane_target: str, prompt: str, *,
                          sleep=time.sleep, settle_secs: float = 3, poll_secs: float = 12,
                          poll_interval: float = 2, max_attempts: int = 3) -> KickResult:
    """Inject the kick, verify a turn started, re-fire (bounded) if it didn't.

    inject(pane_target, prompt) -> bool   (e.g. send_keys_submit; False = send failed)
    capture(pane_target)        -> str    (e.g. tmux capture-pane -p)

    Each attempt: settle → inject → poll up to poll_secs for an actively-generating turn.
    If none starts (drop) or the inject itself fails, re-fire on the next attempt. Returns
    a KickResult; .ok True once a turn is confirmed started, False after max_attempts.
    """
    for attempt in range(1, max_attempts + 1):
        if settle_secs:
            sleep(settle_secs)
        if not inject(pane_target, prompt):
            continue  # send-keys itself failed → re-fire
        waited = 0.0
        while waited < poll_secs:
            sleep(poll_interval)
            waited += poll_interval
            if kicked_turn_started(capture(pane_target)):
                return KickResult(True, attempt, "turn started")
        # polled out with no turn → the kick dropped; loop re-fires
    return KickResult(False, max_attempts, f"no turn started after {max_attempts} attempts")


# ---------------------------------------------------------------------------
# CLI (T-0899 wiring): handoff_and_restart.sh step-8 calls this instead of a
# blind `send_keys_submit + hope`. inject reuses the shared send_keys_submit.sh
# (single source of truth for tmux send-keys + submit-verify); capture uses
# `tmux capture-pane -p`. The tested deliver_kick_verified() above drives the
# loop. Exit 0 = a turn dispatched (verified); exit 8 = no turn after bounded
# re-fires (mirrors the step-8 kick-failure exit code).
# ---------------------------------------------------------------------------
def _build_inject(send_keys_submit_path: str):
    import subprocess

    def inject(pane: str, prompt: str) -> bool:
        try:
            return subprocess.run(
                [send_keys_submit_path, pane, prompt], timeout=60
            ).returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    return inject


def _pane_in_mode(pane: str) -> bool:
    """True if the pane is sitting in a tmux mode (copy-mode et al). (T-2277)

    Fail-SAFE: an unreadable answer counts as in-mode, because the only use of
    this is deciding whether to TRUST a capture, and trusting one we cannot
    vouch for is the failure being prevented.
    """
    import subprocess
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{pane_in_mode}"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return True
        return r.stdout.strip() != "0"
    except (subprocess.TimeoutExpired, OSError):
        return True


def _capture(pane: str) -> str:
    """Pane text, or "" when the surface cannot be trusted. (T-2268 shape, T-2277)

    ⚠️ THE WRITE AND THE READ SHARE A FAILURE DOMAIN, which is what this ticket is
    about. We inject into a pane and then confirm by capturing THE SAME PANE — and
    a pane in copy-mode returns a FROZEN view: returncode 0, non-empty stdout, and
    stale content. So during the one fault where the keystroke was most likely
    eaten, capture-pane hands back a snapshot of a previously-working pane and the
    verifier votes SUCCESS. A dropped kick then reports as delivered, which is
    strictly worse than no verifier — a missing check is visibly missing, this one
    emits positive confirmation while nothing happened.

    A failed capture was already safe: "" matches nothing, so the caller retries.
    It is the SUCCESSFUL-BUT-STALE read that had no defence.

    send_keys_submit.sh clears copy-mode before delivering (T-2259), which removes
    the common trigger but not the window — the pane can re-enter a mode between
    injection and verification, and nothing here noticed.

    Declining to answer is correct: the caller treats "" as not-yet-started and
    polls again, so an untrustworthy surface costs a retry rather than a wrong
    verdict.
    """
    import subprocess
    if _pane_in_mode(pane):
        return ""
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane],
            capture_output=True, text=True, timeout=15,
        )
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def main(argv=None) -> int:
    import argparse
    import os

    p = argparse.ArgumentParser(
        description="Deliver a restart kick and verify it dispatched, re-firing if dropped (T-0899)."
    )
    p.add_argument("--pane", required=True, help="tmux target pane (session:window)")
    p.add_argument("--prompt", required=True, help="kick prompt text to inject")
    p.add_argument("--settle", type=float, default=3.0, help="pre-inject readiness settle (s)")
    p.add_argument("--poll-secs", type=float, default=12.0, help="per-attempt poll window (s)")
    p.add_argument("--poll-interval", type=float, default=2.0, help="poll interval (s)")
    p.add_argument("--max-attempts", type=int, default=3, help="bounded re-fire attempts")
    p.add_argument(
        "--send-keys-submit",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "send_keys_submit.sh"),
        help="path to send_keys_submit.sh (the injector)",
    )
    args = p.parse_args(argv)

    # Operational visibility in the restart log: the effective kick params
    # (settle is now owned here, not by handoff_and_restart.sh's bash step-8).
    print(
        f"[kick_verify] deliver pane={args.pane} settle={args.settle}s "
        f"poll={args.poll_secs}s/{args.poll_interval}s attempts={args.max_attempts}",
        flush=True,
    )

    result = deliver_kick_verified(
        _build_inject(args.send_keys_submit), _capture, args.pane, args.prompt,
        settle_secs=args.settle, poll_secs=args.poll_secs,
        poll_interval=args.poll_interval, max_attempts=args.max_attempts,
    )
    print(f"[kick_verify] {result!r}")
    return 0 if result.ok else 8


if __name__ == "__main__":
    raise SystemExit(main())
