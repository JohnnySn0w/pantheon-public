#!/usr/bin/env python3
"""cap_modal_reaper.py (T-4024) — auto-dismiss a seat's spend-limit /rate-limit-options
modal ONCE its usage window has rolled, so a capped seat self-recovers instead of
sitting wedged on the blocking modal until Recovery hand-clears it.

WHY: the weekly usage-cap window pops a BLOCKING /rate-limit-options modal on a seat's
pane. Unlike the idle-429 variant (which resumes on its own when the window rolls), the
MODAL blocks the pane and does NOT auto-dismiss even after the reset — it needs an Esc.
On 2026-09-27 that wedged the whole fleet (specialist/orchestrator/mason/artisan) for ~34h until Recovery
Esc-cleared each pane by hand (see reference_cyber_safeguard_block_on_security_research,
3rd instance). This reaper does that Esc automatically, but ONLY after the window has
actually rolled (T-2290: the cap self-clears at the printed reset), gated hard against
Esc-ing a healthy pane.

CAP-SIGNAL SOURCE: the T-4037 mid-session-cap detector's published state file
`<state_root>/<persona>/mid-session-cap.json` (schema pantheon-persona-mid-session-cap/v1,
authoritative reader `runtime_controller.read_mid_session_cap_state`). `modal_present` is
the ONLY action trigger — it is already 2-sample-debounced by the detector; the
non-authoritative `candidate_modal_kind` MUST NOT be acted on and this module never reads
it. Read cross-account via sudo (same posture as cap_mute_driver / context_fill_monitor).

SAFETY (defence in depth — the reaper Esc is an ACTIVE keystroke on a live pane, so its
false-positive is HARMFUL, unlike a suppression which fails safe):
  * decision gates (reap_disposition, pure): modal_present True, reset_at parsed, now >=
    reset_at (window rolled), observed_at fresh (detector still writing). Any miss => noop.
  * LIVE re-verify before Esc (increment 2): re-capture the pane and confirm the modal
    chrome is still the LAST non-empty line (pane_shows_cap_modal_at_bottom) — a stale/false
    state file, or a pane merely QUOTING the modal, can then never produce a keystroke.
    Fail-CLOSED: any ambiguity => no Esc (orchestrator T-4037 review ask, 2026-09-27).
  * fail-OPEN on an unreadable state file/pane (never reap what we cannot read).
  * NEVER targets Recovery's own pane (T-0559 no-self-inject); a wedged Recovery can't run this
    anyway.

INCREMENT 1 (this module): the hermetically-testable DECISION CORE (reap_disposition),
the state read, the live-pane classifier (pane_shows_cap_modal_at_bottom), and a SAFE
DRY-RUN observe (reads state, prints per-seat dispositions, sends NOTHING). The live-action
plumbing (pane capture, the Esc keystroke, the Recovery timer, and the default-OFF `--arm`
opt-in with a SUPERVISED first-fire) lands in the reviewed follow-on increment — mirroring
cap_mute_driver's own decision-core-first split (T-3844). Orchestrator has no detector file until
the T-2953 cutover, so a pane-read fallback for orchestrator/non-controller seats is also deferred
to the follow-on.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# Dual-identity-safe imports (run-as-package in tests, run-as-script in prod).
try:  # pragma: no cover - import shim
    from tools.agent_role_map import instance_binding
except ImportError:  # pragma: no cover - bare run with cwd=tools/
    from agent_role_map import instance_binding  # type: ignore

# Seats whose panes could carry the modal. Recovery is EXCLUDED as a reap target (self-inject
# is forbidden, T-0559; and a modal-wedged Recovery can't run this loop anyway) but is still
# read in the dry-run for visibility.
FLEET_PERSONAS: tuple[str, ...] = ("orchestrator", "specialist", "recovery", "archivist", "mason", "artisan")
SELF_EXCLUDED: frozenset[str] = frozenset({"recovery"})

# Per-seat persona state root (uniform across the fleet). mid-session-cap.json is a sibling
# of active-runtime.json under _controller_persona_root — the T-2966 stateless tree —
# NOT under the older <persona>/runtime-controller/ KEEP layer (transition.json).
_STATE_ROOT_TMPL = "/home/{account}/.local/state/pantheon/personas"
CAP_STATE_FILENAME = "mid-session-cap.json"

# observed_at freshness bound. The detector polls ~60s (MID_SESSION_CAP_POLL_INTERVAL_
# SECONDS); a record older than this means its poller is not currently writing (paused /
# stopped / racing a teardown), so the state is not authoritative -> do not act on it.
# Generous multiple of the poll interval; env-tunable.
DEFAULT_STALE_AFTER_SECONDS = float(os.environ.get("CAP_REAPER_STALE_AFTER_SECONDS", "600"))

# Live-pane classifier (pane_shows_cap_modal_at_bottom) patterns. The /rate-limit-options
# modal's footer is its LAST non-empty line; a live modal draws nothing below itself, while
# prose merely QUOTING the modal always has the input box + status line under it (orchestrator
# T-4037 round-2 finding). Conservative + fail-closed: this is a defence-in-depth gate, not
# a re-implementation of the detector's authoritative classification.
_MODAL_FOOTER_RE = re.compile(r"enter to confirm.*esc to cancel", re.IGNORECASE)
_CAP_SENTINEL_RE = re.compile(
    r"monthly spend limit|wait for limit to reset|rate-limit-options|adjust monthly spend",
    re.IGNORECASE,
)
# How many trailing non-empty lines count as the modal's render span for the sentinel check.
_MODAL_SPAN_LINES = 16


def _cap_state_path(persona: str, account: str) -> str:
    return f"{_STATE_ROOT_TMPL.format(account=account)}/{persona}/{CAP_STATE_FILENAME}"


def _read_cap_state_raw(path: str, account: str) -> tuple[str, str]:
    """Fetch a seat's mid-session-cap.json text. Returns (status, text) with status
    'missing' (never written / cleared -> not capped), 'fault' (genuine read error ->
    unknown), or 'ok'. SELF-ACCOUNT reads DIRECTLY — Recovery's sudoers grant covers the OTHER
    accounts, not self-sudo (`sudo -n -u <self>` is REJECTED, misreading a missing file as
    a fault). Mirrors cap_mute_driver._read_transition_raw exactly."""
    if account == getpass.getuser():
        try:
            return "ok", Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return "missing", ""
        except OSError:
            return "fault", ""
    try:
        proc = subprocess.run(
            ["sudo", "-n", "-u", account, "cat", path],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return "fault", ""
    if proc.returncode != 0:
        if not proc.stdout.strip() and "No such file" in (proc.stderr or ""):
            return "missing", ""
        if not proc.stdout.strip():
            return "fault", ""
    return "ok", proc.stdout


def read_cap_modal_state(
    persona: str, account: str,
    *, read_raw: Callable[[str, str], tuple[str, str]] = _read_cap_state_raw,
) -> dict:
    """Parsed mid-session-cap.json for `persona`, or {} when missing/cleared/faulted/
    malformed. {} means 'nothing actionable' (fail-open — never reap what we can't read)."""
    status, out = read_raw(_cap_state_path(persona, account), account)
    if status != "ok":
        return {}
    out = out.strip()
    if not out:
        return {}
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_iso(ts) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp (the detector writes `...Z`). None on anything odd."""
    if not isinstance(ts, str) or not ts:
        return None
    s = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def reap_disposition(
    state: dict, now: datetime,
    *, stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
) -> str:
    """PURE decision on the detector state alone: 'reap' iff every gate passes, else
    'noop:<reason>'. `modal_present` is the ONLY trigger (never candidate_modal_kind).
    A live re-verify (increment 2) still runs before any actual Esc — this is necessary,
    not sufficient. Conservative throughout: an unknown/absent field is a noop, never a reap.
    """
    if not state:
        return "noop:no-state"
    if state.get("modal_present") is not True:
        return "noop:no-modal"
    reset_at = parse_iso(state.get("reset_at"))
    if reset_at is None:
        # No parseable reset instant -> we cannot know the window rolled -> wait, do not Esc.
        return "noop:no-reset-at"
    observed_at = parse_iso(state.get("observed_at"))
    if observed_at is None:
        return "noop:no-observed-at"
    if (now - observed_at).total_seconds() > stale_after_seconds:
        # Detector poller not currently writing -> state not authoritative.
        return "noop:stale"
    if now < reset_at:
        # Window has not rolled yet -> Esc would just re-pop on the next turn.
        return "noop:pre-reset"
    return "reap"


def pane_shows_cap_modal_at_bottom(pane_text: str | None) -> bool:
    """Defence-in-depth live gate (used by the increment-2 Esc path): True iff the pane's
    LAST non-empty line is the /rate-limit-options modal footer AND a cap sentinel appears
    within the trailing render span. A live modal draws nothing below its footer; prose
    QUOTING the modal always has the input box + status line under it, so its footer is not
    the last line. Fail-CLOSED (False) on empty/unreadable — never Esc on an unread pane."""
    if not pane_text:
        return False
    lines = [ln for ln in pane_text.splitlines() if ln.strip()]
    if not lines:
        return False
    if not _MODAL_FOOTER_RE.search(lines[-1]):
        return False
    span = "\n".join(lines[-_MODAL_SPAN_LINES:])
    return bool(_CAP_SENTINEL_RE.search(span))


def observe(personas: tuple[str, ...] = FLEET_PERSONAS, *,
            read_state: Callable[[str, str], dict] = read_cap_modal_state,
            now: datetime | None = None, log=print) -> dict[str, str]:
    """DRY-RUN (increment 1): read each seat's cap state and report the disposition the
    reaper WOULD take. Sends nothing, captures no pane, touches no live pane."""
    now = now or datetime.now(timezone.utc)
    results: dict[str, str] = {}
    for persona in personas:
        try:
            account = instance_binding(persona).get("account")
        except Exception:
            account = None
        if not account:
            results[persona] = "skip:no-account"
            continue
        disp = reap_disposition(read_state(persona, account), now)
        if disp == "reap" and persona in SELF_EXCLUDED:
            disp = "would-reap-but-self-excluded"  # T-0559: never Esc Recovery's own pane
        elif disp == "reap":
            disp = "would-reap(dry-run)"
        results[persona] = disp
    would = {p: d for p, d in results.items() if d.startswith("would-reap")}
    log(f"[cap_modal_reaper] DRY-RUN {len(personas)} seats; would-reap={would or 'none'}; all={results}")
    return results


# ============================ INCREMENT 2 (T-4024) ============================
# Live Esc plumbing + the pre-Esc live re-verify + the default-OFF --arm gate + the
# Recovery timer entry. The Esc is an ACTIVE keystroke on a live pane, so every gate here
# is fail-CLOSED: an unreadable pane, a modal no longer at the bottom, or a state file
# that no longer says reap => NO keystroke. Nothing is sent unless --arm is passed.

# Seconds to wait after Esc before re-capturing to confirm the modal cleared. Module
# level so tests patch it to 0 (no real sleep). Env-tunable.
ESC_SETTLE_SECONDS = float(os.environ.get("CAP_REAPER_ESC_SETTLE_SECONDS", "1.5"))


def _capture_pane(persona: str, account: str, target: str, *, timeout: float = 8.0) -> str | None:
    """Cross-account `tmux capture-pane -p -t <target>`. Returns the pane text, or None on
    any failure (fail-CLOSED for the reaper — never Esc a pane we couldn't read). SELF reads
    directly (no self-sudo, per bugs/sudo-u-to-runners-own-account...); every other account
    via `sudo -n -u`. `$(id -u)` resolves to the TARGET user's uid post-sudo, giving that
    user's tmux server. Mirrors mail_staleness_monitor.capture_pane_idle / inbox_consumer."""
    if not target:
        return None
    try:
        if account == getpass.getuser():
            cmd = ["tmux", "capture-pane", "-p", "-t", target]
        else:
            cmd = ["sudo", "-n", "-u", account, "bash", "-c",
                   f"XDG_RUNTIME_DIR=/run/user/$(id -u) tmux capture-pane -p -t {shlex.quote(target)}"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0:
            return None
        return res.stdout
    except (subprocess.TimeoutExpired, OSError):
        return None


def _send_escape(account: str, target: str, *, timeout: float = 5.0) -> bool:
    """Cross-account `tmux send-keys -t <target> Escape` (cancel the modal — NEVER Enter/→,
    which is the money 'Adjust' option). Returns True iff the keystroke exited 0. REFUSES the
    self-account (T-0559 no-self-inject; Recovery is a reap-excluded target anyway) — defence in
    depth. Same proven cross-account send-keys shape as inbox_consumer._apply_resolve_action."""
    if not target or account == getpass.getuser():
        return False
    try:
        shell_cmd = f"XDG_RUNTIME_DIR=/run/user/$(id -u) tmux send-keys -t {shlex.quote(target)} Escape"
        res = subprocess.run(["sudo", "-n", "-u", account, "bash", "-c", shell_cmd],
                             capture_output=True, text=True, timeout=timeout)
        return res.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def reap_seat(
    persona: str, account: str, target: str | None, *,
    now: datetime | None = None,
    arm: bool = False,
    read_state: Callable[[str, str], dict] = read_cap_modal_state,
    capture_pane: Callable[[str, str, str], "str | None"] = _capture_pane,
    send_escape: Callable[[str, str], bool] = _send_escape,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
) -> str:
    """Reconcile ONE seat: reap iff the state disposition says reap AND the LIVE pane still
    shows the modal at the bottom AND a fresh state re-read still says reap. Returns a short
    disposition. Never raises. The Esc fires only when `arm` is True; unarmed, a fully-verified
    candidate returns 'would-reap(verified)' and NOTHING is sent.

    Fail-CLOSED order (orchestrator T-4037 round-2 review): state-gate -> self-exclude -> live pane
    re-verify (unreadable/modal-gone => skip) -> fresh state re-read (changed => skip) -> arm
    gate -> Esc -> confirm-cleared. Every skip reason is a NO-keystroke path."""
    now = now or datetime.now(timezone.utc)
    disp = reap_disposition(read_state(persona, account), now, stale_after_seconds=stale_after_seconds)
    if disp != "reap":
        return disp
    if persona in SELF_EXCLUDED:
        return "skip:self-excluded"  # T-0559: never Esc Recovery's own pane
    if not target:
        return "skip:no-pane-target"

    # ---- LIVE re-verify before ANY keystroke (state file alone is never sufficient) ----
    pane = capture_pane(persona, account, target)
    if not pane:
        return "skip:pane-unreadable"           # fail-closed: never Esc what we can't read
    if not pane_shows_cap_modal_at_bottom(pane):
        return "skip:modal-not-at-bottom"        # pane changed / stale-or-false state / quoted modal
    # fresh state re-read (the detector may have cleared modal_present since the first read)
    if reap_disposition(read_state(persona, account), now, stale_after_seconds=stale_after_seconds) != "reap":
        return "skip:reverify-state-changed"

    if not arm:
        return "would-reap(verified)"            # OBSERVE: all gates passed, no keystroke sent

    if not send_escape(account, target):
        return "esc-failed"
    # confirm the modal actually cleared
    if ESC_SETTLE_SECONDS > 0:
        time.sleep(ESC_SETTLE_SECONDS)
    after = capture_pane(persona, account, target)
    if after is not None and pane_shows_cap_modal_at_bottom(after):
        return "reaped:not-cleared"              # Esc sent but modal persists — surfaced for review
    return "reaped"


def run(personas: tuple[str, ...] = FLEET_PERSONAS, *, arm: bool = False,
        now: datetime | None = None,
        read_state: Callable[[str, str], dict] = read_cap_modal_state,
        capture_pane: Callable[[str, str, str], "str | None"] = _capture_pane,
        send_escape: Callable[[str, str], bool] = _send_escape,
        log=print) -> dict[str, str]:
    """Drive every fleet seat once through reap_seat. Returns {persona: disposition}.
    OBSERVE-only unless `arm` (the T-4024 supervised-first-fire gate)."""
    now = now or datetime.now(timezone.utc)
    results: dict[str, str] = {}
    for persona in personas:
        try:
            binding = instance_binding(persona)
            account = binding.get("account")
            target = binding.get("restart_pane")
        except Exception:
            results[persona] = "skip:no-binding"
            continue
        if not account:
            results[persona] = "skip:no-account"
            continue
        results[persona] = reap_seat(persona, account, target, now=now, arm=arm,
                                     read_state=read_state, capture_pane=capture_pane,
                                     send_escape=send_escape)
    acted = {p: d for p, d in results.items()
             if d.startswith("reaped") or d.startswith("would-reap")}
    mode = "ARMED" if arm else "OBSERVE"
    log(f"[cap_modal_reaper] {mode} {len(personas)} seats; reap/would={acted or 'none'}; all={results}")
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Auto-dismiss a rolled spend-limit modal once its window has rolled (T-4024).")
    p.add_argument("--persona", help="one persona (default: whole fleet)")
    p.add_argument("--arm", action="store_true",
                   default=os.environ.get("CAP_REAPER_ARM", "").lower() in ("1", "true", "yes"),
                   help="actually send Esc (default OFF: observe-only with live re-verify, for the supervised first-fire)")
    p.add_argument("--dry-run-state", action="store_true",
                   help="increment-1 STATE-ONLY observe (no pane capture, no keystroke)")
    args = p.parse_args(argv)
    personas = (args.persona,) if args.persona else FLEET_PERSONAS
    if args.dry_run_state:
        observe(personas)
    else:
        run(personas, arm=args.arm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
