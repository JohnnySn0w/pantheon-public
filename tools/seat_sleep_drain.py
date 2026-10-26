#!/usr/bin/env python3
"""tools/seat_sleep_drain.py - ORG-106 L3b (T-5526): the DEEP-SLEEP drain.

`harness <seat> sleep` writes the record (state=deep-sleep, phase=draining) and returns;
the seat's own runtime_controller owns the session, so it performs the drain:

  1. no live session  -> straight to step 6 (drain_outcome "no-session");
  2. ensure the sleep marker (so the session's end is never recorded as process_death);
  3. prompt the live session for a fresh HANDOFF (stamp ``handoff_prompted_ts`` once);
  4. wait up to DRAIN_BUDGET_S from that stamp: the HANDOFF mtime moves past it, or the pane
     is idle on two consecutive polls >= IDLE_AFTER_PROMPT_S after it; on timeout end anyway
     (evidence ``sleep_drain_timeout``). The decision is made durable (``drain_outcome``)
     BEFORE the kill, so a retry never repeats the wait;
  5. SIGTERM the session (never SIGKILL); not confirmed dead -> stay ``draining`` and retry;
  6. stop any prior codex receiver (failure -> stay ``draining``, cooldown-gated alert, retry
     with no cap), clear active-runtime.json, THEN set phase=asleep (``expected_down`` flips
     true at ``asleep``, so a lingering file would false-page recovery_watcher's sleep_breach).

Durable by construction: every input is the record (``drain_started_ts``,
``handoff_prompted_ts``, ``drain_outcome``), the marker file or the live session, so a
controller that re-execs mid-drain resumes with the REMAINING budget and never re-prompts.
Every record write is atomic, under the per-seat lock, on a FRESH load, merging only this
module's keys and only if ``drain_started_ts`` still names the drain it started (a wake, a
pause or a replaced record aborts the drain with nothing killed).

All side effects go through `Seams` so tests drive real files and real locks with a fake
clock and fake tmux. Stdlib + repo modules only.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import harness_state as hs  # noqa: E402
from tools import seat_state  # noqa: E402

DRAIN_BUDGET_S = 600.0          # note section 4: wait up to 10 min for the handoff
POLL_S = 5.0
PROMPT_RETRY_S = 30.0
UNDELIVERED_MIN_ATTEMPTS = 3
IDLE_AFTER_PROMPT_S = 45.0
TERMINATE_CONFIRM_S = 30.0
LOCK_TIMEOUT_S = 10.0
MARKER_MAX_AGE_S = 900.0

ASLEEP = "asleep"
ABORTED = "aborted"
PENDING = "pending"
NOOP = "noop"

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

PROMPT_WITH_PATH = (
    "This seat is being put into deep sleep (ORG-106) and this session will end shortly. Please "
    "write a fresh {path} capturing current state, in-flight work, and immediate next actions "
    "for the session that wakes you. When done, say \"handoff written\" and stop. Rewrite the "
    "file even if nothing changed since your last handoff: the sleep waits for the file's "
    "modification time, not for your reply."
)
PROMPT_NO_PATH = (
    "This seat is being put into deep sleep (ORG-106) and this session will end shortly. Please "
    "write your handoff notes now, say \"handoff written\" and stop."
)


@dataclass
class Seams:
    persona: str
    root: Path                              # seat-state root (parent of personas/)
    runtime_ref: str
    handoff_path: Path | None
    alive: Callable[[], bool]
    submit: Callable[[str], Any]
    pane_idle: Callable[[], bool]
    terminate: Callable[[], Any]
    stop_receiver: Callable[[], Any]        # raises on failure
    clear_runtime_state: Callable[[], Any]  # active-runtime.json, mid-session-cap state
    evidence: Callable[[str, dict], Any]
    alert: Callable[[str, str], Any]        # (message, vocab); cooldown-gated by the caller
    now: Callable[[], datetime]
    sleep: Callable[[float], Any]
    poll_s: float = POLL_S
    budget_s: float = DRAIN_BUDGET_S
    prompt_retry_s: float = PROMPT_RETRY_S
    idle_after_s: float = IDLE_AFTER_PROMPT_S
    confirm_s: float = TERMINATE_CONFIRM_S
    lock_timeout_s: float = LOCK_TIMEOUT_S


def _fmt(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_TS_FMT)


def _ev(s: Seams, event: str, **payload: Any) -> None:
    try:
        s.evidence(event, payload)
    except Exception:  # noqa: BLE001 - evidence is best-effort
        pass


def load_sleeping(persona: str, root: Path, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The raw record iff it is a valid, unexpired deep-sleep record. Never raises."""
    try:
        doc = hs.load_seat_state(persona, root=root)
        if not isinstance(doc, dict) or doc.get("state") != seat_state.STATE_DEEP_SLEEP:
            return None
        if not seat_state.parse_record(doc, now=now).active:
            return None
        return doc
    except Exception:  # noqa: BLE001
        return None


def needs_drain(persona: str, root: Path, *, session_alive: bool, now: datetime | None = None) -> bool:
    """True iff the controller must run the drain: a draining record, or an ``asleep`` record
    next to a live session it owns (a launch that was already past the chokepoint when the
    record landed; L4's sleep_breach still pages it)."""
    rec = load_sleeping(persona, root, now=now)
    if rec is None:
        return False
    return rec.get("phase") == seat_state.PHASE_DRAINING or session_alive


class _Busy(Exception):
    """The seat-state lock was not free in time; the caller returns PENDING."""


def _patch(s: Seams, key: Any, updates: dict[str, Any]) -> bool:
    """Merge `updates` into the fresh record under the lock. False iff the record is no
    longer the deep-sleep drain identified by `key` (nothing written)."""
    try:
        with hs.seat_state_lock(s.persona, root=s.root, blocking=True, timeout_s=s.lock_timeout_s):
            rec = hs.load_seat_state(s.persona, root=s.root)
            if (not isinstance(rec, dict) or rec.get("state") != seat_state.STATE_DEEP_SLEEP
                    or rec.get("drain_started_ts") != key):
                return False
            hs.write_seat_state(s.persona, {**rec, **updates}, root=s.root)
            return True
    except hs.SeatStateBusy as exc:
        raise _Busy(str(exc)) from exc


def _ensure_marker(s: Seams, key: Any) -> None:
    """Idempotent: (re)write the sleep marker whenever it is missing (a death
    classification consumes it, so a re-exec'd drain may find it gone)."""
    try:
        if hs.load_sleep_marker(s.persona, root=s.root) is not None:
            return
        hs.write_sleep_marker(
            s.persona, root=s.root, runtime=s.runtime_ref, drain_started_ts=str(key or ""),
            ts=_fmt(s.now()), correlation_id=f"sleep-{key}",
        )
    except Exception as exc:  # noqa: BLE001 - never block the drain on the marker
        _ev(s, "sleep_marker_write_failed", error=f"{type(exc).__name__}: {exc}"[:300])


def _still_mine(s: Seams, key: Any) -> bool:
    rec = load_sleeping(s.persona, s.root, now=s.now())
    return rec is not None and rec.get("drain_started_ts") == key


def _abort(s: Seams, why: str) -> str:
    hs.remove_sleep_marker(s.persona, root=s.root)
    _ev(s, "sleep_drain_aborted", why=why)
    return ABORTED


def _reopen(s: Seams, rec: dict[str, Any]) -> dict[str, Any] | None:
    """asleep + a live session: re-drain. Replaces the drain identity (new
    drain_started_ts), clears the prompt stamp and the outcome."""
    ts = _fmt(s.now())
    try:
        with hs.seat_state_lock(s.persona, root=s.root, blocking=True, timeout_s=s.lock_timeout_s):
            fresh = hs.load_seat_state(s.persona, root=s.root)
            if (not isinstance(fresh, dict) or fresh.get("state") != seat_state.STATE_DEEP_SLEEP
                    or fresh.get("phase") != seat_state.PHASE_ASLEEP):
                return None
            new = {**fresh, "phase": seat_state.PHASE_DRAINING, "phase_ts": ts, "drain_started_ts": ts,
                   "handoff_prompted_ts": None, "drain_outcome": None}
            hs.write_seat_state(s.persona, new, root=s.root)
    except hs.SeatStateBusy as exc:
        raise _Busy(str(exc)) from exc
    _ev(s, "sleep_redrain", why="a live session was observed on an asleep seat")
    return new


def _handoff_confirmed(s: Seams, prompted: datetime) -> bool:
    if s.handoff_path is None:
        return False
    try:
        return s.handoff_path.stat().st_mtime > prompted.timestamp()
    except OSError:
        return False


def _prompt_phase(s: Seams, rec: dict[str, Any], key: Any) -> str | None:
    """Step 3. Returns None when the prompt stamp is durable (or the session ended / the
    prompt gave up -> the caller reads drain_outcome), or ABORTED / PENDING."""
    attempts = 0
    started = seat_state._parse_ts(rec.get("drain_started_ts")) or s.now()
    while True:
        if not s.alive():
            return None
        if not _still_mine(s, key):
            return _abort(s, "record changed before the handoff prompt")
        prompt = (PROMPT_WITH_PATH.format(path=s.handoff_path) if s.handoff_path is not None
                  else PROMPT_NO_PATH)
        try:
            s.submit(prompt)
        except Exception as exc:  # noqa: BLE001
            attempts += 1
            _ev(s, "sleep_prompt_undelivered", attempt=attempts, error=f"{type(exc).__name__}: {exc}"[:300])
            if attempts >= UNDELIVERED_MIN_ATTEMPTS and s.now() >= started + timedelta(seconds=s.budget_s):
                return None
            s.sleep(s.prompt_retry_s)
            continue
        stamped = _fmt(s.now())
        if not _patch(s, key, {"handoff_prompted_ts": stamped}):
            return _abort(s, "record changed while stamping the handoff prompt")
        _ev(s, "sleep_handoff_prompted", handoff_path=str(s.handoff_path or ""))
        return None


def _wait_phase(s: Seams, key: Any) -> str:
    """Step 4. Returns the drain outcome, or ABORTED."""
    rec = load_sleeping(s.persona, s.root, now=s.now())
    if rec is None or rec.get("drain_started_ts") != key:
        return ABORTED
    prompted = seat_state._parse_ts(rec.get("handoff_prompted_ts"))
    if prompted is None:
        return "undelivered"
    if s.handoff_path is None:
        _ev(s, "sleep_handoff_path_unknown", note="relying on idle-after-prompt confirmation")
    deadline = prompted + timedelta(seconds=s.budget_s)
    idle_hits = 0
    while True:
        if not _still_mine(s, key):
            return ABORTED
        if _handoff_confirmed(s, prompted):
            return "handoff"
        if not s.alive():
            return "idle"   # the session ended on its own after the prompt
        if s.now() >= prompted + timedelta(seconds=s.idle_after_s):
            idle_hits = idle_hits + 1 if s.pane_idle() else 0
            if idle_hits >= 2:
                return "idle"
        if s.now() >= deadline:
            return "timeout"
        s.sleep(s.poll_s)


def _end_session(s: Seams) -> bool:
    """Step 5. True iff the session is confirmed gone (SIGTERM only)."""
    s.terminate()
    end = s.now() + timedelta(seconds=s.confirm_s)
    while s.alive() and s.now() < end:
        s.sleep(min(s.poll_s, 1.0))
    return not s.alive()


def _finalize(s: Seams, key: Any, outcome: str) -> str:
    """Step 6 tail: runtime state first, THEN the phase flip (see module docstring)."""
    if not _still_mine(s, key):
        # A wake / replacement landed during the receiver stop: the cache files may already
        # belong to a fresh session, which a stale drain must not wipe.
        return _abort(s, "record changed before the runtime state was cleared")
    try:
        s.clear_runtime_state()
    except Exception as exc:  # noqa: BLE001 - a file we cannot clear must not look asleep
        _ev(s, "sleep_clear_runtime_state_failed", error=f"{type(exc).__name__}: {exc}"[:300])
        return PENDING
    ts = _fmt(s.now())
    if not _patch(s, key, {"phase": seat_state.PHASE_ASLEEP, "phase_ts": ts, "drain_outcome": outcome}):
        return _abort(s, "record changed before the asleep transition")
    hs.remove_sleep_marker(s.persona, root=s.root)
    _ev(s, "sleep_asleep", drain_outcome=outcome)
    return ASLEEP


def run_drain(s: Seams) -> str:
    """Run (or resume) the drain. Returns ASLEEP, ABORTED, PENDING (retry me) or NOOP.
    Never raises for a lock, I/O or seam fault."""
    try:
        return _run(s)
    except _Busy:
        return PENDING
    except Exception as exc:  # noqa: BLE001
        _ev(s, "sleep_drain_fault", error=f"{type(exc).__name__}: {exc}"[:300])
        return PENDING


def _run(s: Seams) -> str:
    rec = load_sleeping(s.persona, s.root, now=s.now())
    if rec is None:
        return NOOP
    alive = s.alive()
    if rec.get("phase") == seat_state.PHASE_ASLEEP:
        if not alive:
            return NOOP
        rec = _reopen(s, rec)
        if rec is None:
            return NOOP
    key = rec.get("drain_started_ts")
    outcome = rec.get("drain_outcome")
    if not alive:
        outcome = outcome or "no-session"
    elif outcome is None:
        if rec.get("handoff_prompted_ts") is None:
            _ev(s, "sleep_drain_started", runtime=s.runtime_ref)
        _ensure_marker(s, key)
        if rec.get("handoff_prompted_ts") is None:
            early = _prompt_phase(s, rec, key)
            if early is not None:
                return early
        if not s.alive():
            outcome = "no-session"
        else:
            outcome = _wait_phase(s, key)
            if outcome == ABORTED:
                return _abort(s, "record changed during the handoff wait")
            if outcome in ("timeout", "undelivered"):
                _ev(s, "sleep_drain_timeout", reason=outcome, budget_s=s.budget_s)
            else:
                _ev(s, "sleep_handoff_confirmed", via=outcome)
            if not _patch(s, key, {"drain_outcome": outcome}):
                return _abort(s, "record changed before the session end")
    if s.alive():
        _ensure_marker(s, key)
        if not _end_session(s):
            _ev(s, "sleep_terminate_unconfirmed", runtime=s.runtime_ref)
            return PENDING
        _ev(s, "sleep_session_terminated", runtime=s.runtime_ref)
    try:
        s.stop_receiver()
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"[:300]
        _ev(s, "sleep_receiver_stop_failed", error=detail)
        try:
            s.alert(
                f"sleep_receiver_stop_failed: {s.persona} is draining into deep-sleep but its codex "
                f"receiver could not be stopped ({detail}). The seat stays 'draining' and the stop "
                "is retried; a poller left running with no session would consume Telegram messages.",
                "sleep_receiver_stop_failed",
            )
        except Exception:  # noqa: BLE001
            pass
        return PENDING
    return _finalize(s, key, outcome)


def sleep_end_expected(
    persona: str, root: Path, runtime_ref: str, *, attempt_started_at: datetime, current: datetime,
) -> bool:
    """Death classification (controller_main): is this session end the drain's doing? The
    marker is examined at most once (unlinked whenever one existed, like T-5483's) and counts
    if it names `runtime_ref`, was written at/after this attempt started and is at most
    MARKER_MAX_AGE_S old. An unexpired deep-sleep record counts too (the session died before
    the controller's next check). Never raises; a fault reads False."""
    try:
        doc = hs.load_sleep_marker(persona, root=root)
    except Exception:  # noqa: BLE001
        doc = None
    expected = False
    if doc is not None:
        try:
            hs.remove_sleep_marker(persona, root=root)
        except OSError:
            pass
        ts = seat_state._parse_ts(doc.get("ts")) if isinstance(doc, dict) else None
        try:
            if (ts is not None and doc.get("runtime") == runtime_ref and ts >= attempt_started_at
                    and 0 <= (current - ts).total_seconds() <= MARKER_MAX_AGE_S):
                expected = True
        except TypeError:
            pass
    return expected or load_sleeping(persona, root, now=current) is not None
