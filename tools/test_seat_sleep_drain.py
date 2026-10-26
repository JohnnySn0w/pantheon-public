"""ORG-106 L3b (T-5526): the DEEP-SLEEP drain (tools/seat_sleep_drain.py) and its controller wiring.

Real files and real flock locks in tmp roots. The only fakes are the true external seams: the
tmux session (alive / submit / idle / terminate), the codex receiver, and the clock. Nothing
here touches a live seat, tmux, systemd or /var/lib/pantheon.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from tools import harness_state as hs
from tools import recovery_watcher
from tools import runtime_controller as rc
from tools import seat_sleep
from tools import seat_sleep_drain as drain
from tools import seat_state
from tools.test_runtime_controller import _binding

T0 = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)
FMT = "%Y-%m-%dT%H:%M:%SZ"
PERSONA = "specialist"


class Crash(BaseException):
    """A controller death mid-drain (not an Exception, so run_drain cannot swallow it)."""


class Env:
    """A fake clock + fake tmux session around REAL state files."""

    def __init__(self, tmp_path: Path, *, persona: str = PERSONA, root: Path | None = None):
        self.persona = persona
        self.root = root or (tmp_path / "state")
        (self.root / "personas" / persona).mkdir(parents=True, exist_ok=True)
        self.handoff = tmp_path / "HANDOFF.md"
        self.clock = T0
        self.alive = True
        self.idle = False
        self.submits: list[str] = []
        self.submit_error: Exception | None = None
        self.terminated = 0
        self.dies_on_terminate = True
        self.receiver_errors = 0
        self.receiver_calls = 0
        self.cleared_in_phase: list[str] = []
        self.events: list[tuple[str, dict]] = []
        self.alerts: list[tuple[str, str]] = []
        self.hook = lambda secs: None
        self.marker_at_terminate: list[bool] = []
        self.active_path = hs.active_runtime_path(persona, root=self.root)
        self.active_path.write_text(json.dumps({"runtime": "claude/primary"}))

    def now(self) -> datetime:
        return self.clock

    def sleep(self, secs: float) -> None:
        self.clock += timedelta(seconds=secs)
        self.hook(secs)

    def write_handoff(self, offset: float = 1.0) -> None:
        self.handoff.write_text("handoff")
        t = self.clock.timestamp() + offset
        os.utime(self.handoff, (t, t))

    def _submit(self, text: str) -> None:
        if self.submit_error is not None:
            raise self.submit_error
        self.submits.append(text)

    def _terminate(self) -> None:
        self.terminated += 1
        self.marker_at_terminate.append(hs.load_sleep_marker(self.persona, root=self.root) is not None)
        if self.dies_on_terminate:
            self.alive = False

    def _stop_receiver(self) -> None:
        self.receiver_calls += 1
        if self.receiver_errors > 0:
            self.receiver_errors -= 1
            raise RuntimeError("poller still up")

    def _clear(self) -> None:
        rec = hs.load_seat_state(self.persona, root=self.root)
        self.cleared_in_phase.append(rec["phase"])
        self.active_path.unlink(missing_ok=True)

    def seams(self, *, handoff_path="default", **over) -> drain.Seams:
        kw = dict(
            persona=self.persona, root=self.root, runtime_ref="claude/primary",
            handoff_path=self.handoff if handoff_path == "default" else handoff_path,
            alive=lambda: self.alive, submit=self._submit, pane_idle=lambda: self.idle,
            terminate=self._terminate, stop_receiver=self._stop_receiver,
            clear_runtime_state=self._clear,
            evidence=lambda event, payload: self.events.append((event, payload)),
            alert=lambda msg, vocab: self.alerts.append((msg, vocab)),
            now=self.now, sleep=self.sleep, lock_timeout_s=0.2,
        )
        kw.update(over)
        return drain.Seams(**kw)

    def names(self) -> list[str]:
        return [e for e, _ in self.events]

    def record(self) -> dict | None:
        return hs.load_seat_state(self.persona, root=self.root)


def plant(env: Env, *, phase: str = "draining", drain_started: str | None = "default",
          state: str = "deep-sleep", **over) -> dict:
    ts = T0.strftime(FMT)
    doc = {
        "schema": hs.SCHEMA_SEAT_STATE, "persona": env.persona, "state": state,
        "phase": phase if state == "deep-sleep" else "set",
        "phase_ts": ts, "set_ts": ts, "set_by": "operator:t", "owner": "operator",
        "reason": "drain test", "ticket": None,
        "expires": (T0 + timedelta(hours=24)).strftime(FMT), "indefinite": False,
        "telegram_dark_ack": False, "pre_expiry_notified": False, "ledger_entry": "e",
    }
    if state == "deep-sleep":
        doc["drain_started_ts"] = ts if drain_started == "default" else drain_started
    doc.update(over)
    hs.write_seat_state(env.persona, doc, root=env.root)
    return doc


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# --- the happy path and its exits ---------------------------------------------------------

def test_happy_path_handoff_then_asleep(env):
    plant(env)
    env.hook = lambda secs: env.write_handoff() if env.clock >= T0 + timedelta(seconds=20) else None
    assert drain.run_drain(env.seams()) == drain.ASLEEP
    rec = env.record()
    assert rec["phase"] == "asleep" and rec["drain_outcome"] == "handoff"
    assert rec["handoff_prompted_ts"] == T0.strftime(FMT)
    assert len(env.submits) == 1 and str(env.handoff) in env.submits[0]
    assert env.terminated == 1 and env.receiver_calls == 1
    assert not env.active_path.exists()
    assert hs.load_sleep_marker(env.persona, root=env.root) is None
    assert [n for n in env.names() if n.startswith("sleep_")] == [
        "sleep_drain_started", "sleep_handoff_prompted", "sleep_handoff_confirmed",
        "sleep_session_terminated", "sleep_asleep"]


def test_timeout_ends_anyway_with_sleep_drain_timeout(env):
    plant(env)
    assert drain.run_drain(env.seams()) == drain.ASLEEP
    rec = env.record()
    assert rec["drain_outcome"] == "timeout" and rec["phase"] == "asleep"
    assert "sleep_drain_timeout" in env.names()
    assert env.clock >= T0 + timedelta(seconds=drain.DRAIN_BUDGET_S)
    assert env.terminated == 1


def test_idle_pane_after_the_prompt_confirms_but_not_before_the_grace(env):
    plant(env)
    env.idle = True
    assert drain.run_drain(env.seams()) == drain.ASLEEP
    assert env.record()["drain_outcome"] == "idle"
    assert env.clock >= T0 + timedelta(seconds=drain.IDLE_AFTER_PROMPT_S)
    assert env.clock < T0 + timedelta(seconds=drain.IDLE_AFTER_PROMPT_S + 30)


def test_unknown_handoff_path_relies_on_idle_and_says_so(env):
    plant(env)
    env.idle = True
    assert drain.run_drain(env.seams(handoff_path=None)) == drain.ASLEEP
    assert env.record()["drain_outcome"] == "idle"
    assert "sleep_handoff_path_unknown" in env.names()
    assert "HANDOFF" not in env.submits[0]


def test_undelivered_prompt_retries_then_ends_anyway(env):
    plant(env, drain_started=(T0 - timedelta(minutes=11)).strftime(FMT))
    env.submit_error = RuntimeError("no pane")
    assert drain.run_drain(env.seams()) == drain.ASLEEP
    assert env.record()["drain_outcome"] == "undelivered"
    assert env.names().count("sleep_prompt_undelivered") >= drain.UNDELIVERED_MIN_ATTEMPTS
    assert "sleep_drain_timeout" in env.names()
    assert env.terminated == 1


def test_no_live_session_goes_straight_to_asleep(env):
    plant(env)
    env.alive = False
    assert drain.run_drain(env.seams()) == drain.ASLEEP
    rec = env.record()
    assert rec["phase"] == "asleep" and rec["drain_outcome"] == "no-session"
    assert env.submits == [] and env.terminated == 0
    assert env.receiver_calls == 1 and not env.active_path.exists()


# --- risk 3: the lingering active-runtime.json --------------------------------------------

def test_active_runtime_is_cleared_before_the_asleep_flip(env):
    plant(env)
    env.idle = True
    assert drain.run_drain(env.seams()) == drain.ASLEEP
    assert env.cleared_in_phase == ["draining"], "cleared while still draining, then flipped"


def test_lingering_active_runtime_would_false_page_sleep_breach_and_the_drain_prevents_it(env):
    plant(env)
    export = lambda: {"expected_down": True,  # noqa: E731 - the L4 health export for an asleep seat
                      "runtime": (hs.load_active_runtime(env.persona, root=env.root) or {}).get("runtime")}
    # the failure shape: asleep + a lingering cache file
    assert recovery_watcher.sleep_breach(export()) is True
    env.alive = False
    assert drain.run_drain(env.seams()) == drain.ASLEEP
    assert recovery_watcher.sleep_breach(export()) is False


def test_a_runtime_state_file_that_cannot_be_cleared_never_looks_asleep(env):
    plant(env)
    env.alive = False
    bad = env.seams(clear_runtime_state=mock.Mock(side_effect=PermissionError("ro")))
    assert drain.run_drain(bad) == drain.PENDING
    assert env.record()["phase"] == "draining"
    assert "sleep_clear_runtime_state_failed" in env.names()


# --- terminate / receiver retries, idempotency -------------------------------------------

def test_terminate_unconfirmed_stays_draining_then_retry_does_not_repeat_the_wait(env):
    plant(env)
    env.idle = True
    env.dies_on_terminate = False
    assert drain.run_drain(env.seams()) == drain.PENDING
    assert env.record()["phase"] == "draining" and env.record()["drain_outcome"] == "idle"
    assert "sleep_terminate_unconfirmed" in env.names()
    env.dies_on_terminate = True
    before = env.clock
    assert drain.run_drain(env.seams()) == drain.ASLEEP
    assert len(env.submits) == 1, "no second prompt"
    assert env.clock - before < timedelta(seconds=drain.POLL_S * 3), "no second 10-minute wait"
    assert env.names().count("sleep_drain_timeout") == 0


def test_receiver_stop_failure_stays_draining_alerts_and_retries_without_a_cap(env):
    plant(env)
    env.alive = False
    env.receiver_errors = 6
    for _ in range(6):
        assert drain.run_drain(env.seams()) == drain.PENDING
        assert env.record()["phase"] == "draining"
    assert len(env.alerts) == 6 and all(v == "sleep_receiver_stop_failed" for _, v in env.alerts)
    assert env.names().count("sleep_receiver_stop_failed") == 6
    assert drain.run_drain(env.seams()) == drain.ASLEEP


def test_marker_is_rewritten_when_a_death_classification_consumed_it(env):
    plant(env)
    env.idle = True
    env.dies_on_terminate = False
    assert drain.run_drain(env.seams()) == drain.PENDING
    assert env.marker_at_terminate == [True]
    hs.remove_sleep_marker(env.persona, root=env.root)  # a death classification consumed it
    env.dies_on_terminate = True
    assert drain.run_drain(env.seams()) == drain.ASLEEP
    assert env.marker_at_terminate == [True, True], "step 3 rewrote the missing marker before the kill"


# --- risk 4: controller re-exec mid-drain ---------------------------------------------------

def test_reexec_mid_wait_resumes_with_the_remaining_budget_and_never_reprompts(env):
    plant(env)

    def die_after_100s(_secs):
        if env.clock >= T0 + timedelta(seconds=100):
            raise Crash()

    env.hook = die_after_100s
    with pytest.raises(Crash):
        drain.run_drain(env.seams())
    assert env.record()["handoff_prompted_ts"] == T0.strftime(FMT)
    assert env.terminated == 0 and len(env.submits) == 1
    # the next controller process: 200 s after the prompt, a fresh fake session, same files
    env.hook = lambda secs: None
    env.clock = T0 + timedelta(seconds=200)
    assert drain.run_drain(env.seams()) == drain.ASLEEP
    assert len(env.submits) == 1, "no second handoff prompt"
    assert env.record()["drain_outcome"] == "timeout"
    assert T0 + timedelta(seconds=drain.DRAIN_BUDGET_S) <= env.clock < T0 + timedelta(seconds=drain.DRAIN_BUDGET_S + 10), \
        "it waited out the REMAINING budget, not a fresh 10 minutes"


def test_reexec_after_the_kill_but_before_asleep_finishes_without_a_prompt(env):
    plant(env, handoff_prompted_ts=T0.strftime(FMT), drain_outcome="handoff")
    env.alive = False  # the old controller's SIGTERM already took
    assert drain.run_drain(env.seams()) == drain.ASLEEP
    assert env.submits == [] and env.terminated == 0
    assert env.record()["drain_outcome"] == "handoff"


# --- aborts: the record is the authority ---------------------------------------------------

@pytest.mark.parametrize("how", ["wake", "pause", "replaced"])
def test_a_wake_pause_or_replacement_mid_drain_aborts_without_a_kill(env, how):
    plant(env)

    def interfere(_secs):
        if env.clock < T0 + timedelta(seconds=15):
            return
        if how == "wake":
            hs.clear_seat_state(env.persona, root=env.root)
        elif how == "pause":
            plant(env, state="paused-intake")
        else:
            plant(env, drain_started=(T0 + timedelta(seconds=15)).strftime(FMT))

    env.hook = interfere
    assert drain.run_drain(env.seams()) == drain.ABORTED
    assert env.terminated == 0 and env.receiver_calls == 0
    assert hs.load_sleep_marker(env.persona, root=env.root) is None
    assert "sleep_drain_aborted" in env.names()


def test_a_busy_lock_is_pending_and_writes_nothing(env):
    plant(env)
    env.alive = False
    before = hs.seat_state_path(env.persona, root=env.root).read_text()
    with hs.seat_state_lock(env.persona, root=env.root, blocking=False):
        assert drain.run_drain(env.seams()) == drain.PENDING
    assert hs.seat_state_path(env.persona, root=env.root).read_text() == before


def test_no_record_or_paused_or_expired_is_a_noop(env):
    assert drain.run_drain(env.seams()) == drain.NOOP
    plant(env, state="paused-intake")
    assert drain.run_drain(env.seams()) == drain.NOOP
    plant(env, expires=(T0 - timedelta(minutes=1)).strftime(FMT))
    assert drain.run_drain(env.seams()) == drain.NOOP
    assert env.terminated == 0


# --- A1: asleep + a live session is a re-drain ---------------------------------------------

def test_asleep_with_a_live_session_is_redrained_not_passed_silently(env):
    plant(env, phase="asleep", drain_outcome="no-session")
    env.idle = True
    assert drain.needs_drain(env.persona, env.root, session_alive=True, now=T0)
    assert drain.run_drain(env.seams()) == drain.ASLEEP
    assert "sleep_redrain" in env.names() and env.terminated == 1
    assert env.record()["drain_started_ts"] == T0.strftime(FMT)


def test_asleep_with_no_session_needs_nothing(env):
    plant(env, phase="asleep", drain_outcome="no-session")
    env.alive = False
    assert not drain.needs_drain(env.persona, env.root, session_alive=False, now=T0)
    assert drain.run_drain(env.seams()) == drain.NOOP


@pytest.mark.parametrize("phase,alive,expected", [
    ("draining", False, True), ("draining", True, True), ("asleep", True, True), ("asleep", False, False)])
def test_needs_drain_table(env, phase, alive, expected):
    plant(env, phase=phase)
    assert drain.needs_drain(env.persona, env.root, session_alive=alive, now=T0) is expected


# --- the sleep marker and the death classification -----------------------------------------

def _marker(env, *, ts=T0, runtime="claude/primary"):
    hs.write_sleep_marker(env.persona, root=env.root, runtime=runtime,
                          drain_started_ts=T0.strftime(FMT), ts=ts.strftime(FMT), correlation_id="c")


def test_marker_is_consumed_once_and_matches_only_its_runtime_and_window(env):
    started = T0 - timedelta(minutes=30)
    _marker(env)
    assert drain.sleep_end_expected(env.persona, env.root, "claude/primary",
                                    attempt_started_at=started, current=T0 + timedelta(minutes=5))
    assert hs.load_sleep_marker(env.persona, root=env.root) is None, "unlinked on examine"
    assert not drain.sleep_end_expected(env.persona, env.root, "claude/primary",
                                        attempt_started_at=started, current=T0 + timedelta(minutes=5))
    for kwargs in (
        dict(runtime="codex/interactive"),                       # another runtime
        dict(ts=T0 - timedelta(hours=1)),                        # written before this attempt began
    ):
        _marker(env, **kwargs)
        assert not drain.sleep_end_expected(env.persona, env.root, "claude/primary",
                                            attempt_started_at=started, current=T0 + timedelta(minutes=5))
        assert hs.load_sleep_marker(env.persona, root=env.root) is None, "stale or mismatched is still unlinked"
    _marker(env)  # older than MARKER_MAX_AGE_S
    assert not drain.sleep_end_expected(env.persona, env.root, "claude/primary", attempt_started_at=started,
                                        current=T0 + timedelta(seconds=drain.MARKER_MAX_AGE_S + 5))


def test_a_deep_sleep_record_alone_also_makes_the_end_expected(env):
    plant(env)
    assert drain.sleep_end_expected(env.persona, env.root, "claude/primary",
                                    attempt_started_at=T0, current=T0 + timedelta(minutes=1))


def test_a_genuine_death_with_no_marker_and_no_record_is_not_expected(env):
    assert not drain.sleep_end_expected(env.persona, env.root, "claude/primary",
                                        attempt_started_at=T0, current=T0 + timedelta(minutes=1))
    plant(env, state="paused-intake")
    assert not drain.sleep_end_expected(env.persona, env.root, "claude/primary",
                                        attempt_started_at=T0, current=T0 + timedelta(minutes=1))


def test_sleep_marker_path_matches_the_controller_copy(env):
    assert hs.sleep_marker_path(env.persona, root=env.root) == (
        env.root / "personas" / env.persona / "overrides" / "sleep-marker.json")


# --- the validator -------------------------------------------------------------------------

def test_validator_accepts_the_optional_drain_keys_and_rejects_bad_ones(env):
    doc = plant(env, handoff_prompted_ts=T0.strftime(FMT), drain_outcome="timeout")
    assert seat_state.validate_record(doc) == []
    assert seat_state.validate_record({**doc, "drain_started_ts": "yesterday"})
    assert seat_state.validate_record({**doc, "handoff_prompted_ts": 5})
    assert seat_state.validate_record({**doc, "drain_outcome": "because"})
    assert seat_state.validate_record({**doc, "drain_outcome": None}) == []


# --- controller wiring (production seams, tmux dead) ---------------------------------------

@pytest.fixture
def ctl(tmp_path, monkeypatch):
    root = tmp_path / "state"
    personas = root / "personas"
    (personas / "recovery").mkdir(parents=True)
    monkeypatch.setattr(seat_state, "DEFAULT_OOM_HOLDS", tmp_path / "no-oom-holds.json")
    monkeypatch.setattr(rc, "_seat_sleep_session_alive", lambda *a, **k: False)
    return root, personas


def _recovery_plant(root: Path, *, phase="draining", **over):
    ts = T0.strftime(FMT)
    doc = {
        "schema": hs.SCHEMA_SEAT_STATE, "persona": "recovery", "state": "deep-sleep", "phase": phase,
        "phase_ts": ts, "set_ts": ts, "set_by": "operator:t", "owner": "operator", "reason": "ctl",
        "ticket": None, "expires": (datetime.now(timezone.utc) + timedelta(hours=5)).strftime(FMT),
        "indefinite": False, "telegram_dark_ack": False, "pre_expiry_notified": False,
        "ledger_entry": "e", "drain_started_ts": ts, **over,
    }
    hs.write_seat_state("recovery", doc, root=root)


def test_the_sleep_gate_finishes_a_drain_after_a_reexec_and_launches_nothing(ctl):
    root, personas = ctl
    _recovery_plant(root)
    (personas / "recovery" / "active-runtime.json").write_text('{"runtime": "claude/primary"}')
    claude = _binding(personas, harness="claude", profile="primary")
    started: list = []
    sleeps: list[float] = []
    result = rc.controller_main(
        "recovery", candidates=[claude], state_root=personas, sleep=sleeps.append,
        start_runtime_fn=lambda b: started.append(b), block_until_death_fn=lambda b: None,
        persist_record_fn=lambda *a, **k: "persisted", max_reselect_cycles=2,
    )
    assert result == 1 and started == []
    rec = hs.load_seat_state("recovery", root=root)
    assert rec["phase"] == "asleep" and rec["drain_outcome"] == "no-session"
    assert not (personas / "recovery" / "active-runtime.json").exists()
    events = [json.loads(l)["event"] for l in rc._evidence_log_path("recovery", personas).read_text().splitlines()]
    assert "sleep_asleep" in events


def test_a_pending_drain_step_retries_fast_at_the_gate(ctl):
    root, personas = ctl
    _recovery_plant(root)
    claude = _binding(personas, harness="claude", profile="primary")
    sleeps: list[float] = []
    with mock.patch.object(drain, "run_drain", return_value=drain.PENDING):
        rc.controller_main(
            "recovery", candidates=[claude], state_root=personas, sleep=sleeps.append,
            start_runtime_fn=lambda b: None, block_until_death_fn=lambda b: None,
            persist_record_fn=lambda *a, **k: "persisted", max_reselect_cycles=2,
        )
    assert sleeps == [rc.SEAT_SLEEP_DRAIN_RETRY_SECONDS] * 2


def test_the_supervise_loop_notices_the_record_drains_and_returns_a_sleep_outcome(ctl, monkeypatch, tmp_path):
    root, personas = ctl
    claude = _binding(personas, harness="claude", profile="primary")
    env = Env(tmp_path, persona="recovery", root=root)
    state = {"polls": 0}
    live = {"v": True}

    def alive():
        state["polls"] += 1
        if state["polls"] == 3:
            _recovery_plant(root)  # `harness recovery sleep` lands while the controller is supervising
        return live["v"]

    monkeypatch.setattr(rc, "_seat_sleep_session_alive", lambda *a, **k: live["v"])
    fake = env.seams(persona="recovery", root=root, alive=lambda: live["v"])
    fake.terminate = lambda: live.__setitem__("v", False)
    monkeypatch.setattr(rc, "build_seat_sleep_seams", lambda *a, **k: fake)
    now, sleep = env.now, env.sleep
    env.hook = lambda secs: setattr(fake, "pane_idle", lambda: True) if env.clock >= T0 + timedelta(seconds=40) else None

    out = rc.block_until_tmux_death(
        claude, alive_check=alive, sleep=sleep, now=now, state_root=personas,
        seat_sleep_fn=lambda: rc._seat_sleep_check("recovery", [claude], state_root=personas, now=now, sleep=sleep),
        seat_sleep_check_interval_seconds=10.0,
    )
    assert isinstance(out, rc.SeatSleepOutcome) and out.drain_outcome == "idle"
    assert hs.load_seat_state("recovery", root=root)["phase"] == "asleep"


def _run_one_supervised_cycle(personas, claude, root, *, block_result, plant_during_start=False):
    started: list = []

    def start(binding):
        if plant_during_start:
            _recovery_plant(root)
        started.append(binding)
        return (rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False),
                rc.RuntimeHandle(binding.launch, binding.controller.tmux, None, rc._utc_now()))

    return rc.controller_main(
        "recovery", candidates=[claude], state_root=personas, sleep=lambda s: None,
        start_runtime_fn=start, block_until_death_fn=lambda b: block_result,
        persist_record_fn=lambda *a, **k: "persisted",
    )


def test_a_seat_sleep_outcome_is_a_success_never_a_failure_or_cooldown(ctl):
    root, personas = ctl
    claude = _binding(personas, harness="claude", profile="primary")
    assert _run_one_supervised_cycle(personas, claude, root, block_result=rc.SeatSleepOutcome("handoff")) == 0
    attempt = rc.load_last_attempt("recovery", rc._binding_runtime_slug(claude), state_root=personas)
    assert attempt["outcome"] == "success" and not attempt.get("failure_class")
    events = [json.loads(l)["event"] for l in rc._evidence_log_path("recovery", personas).read_text().splitlines()]
    assert "controller_observed_seat_sleep" in events


def test_a_death_inside_a_drain_is_not_process_death_but_a_plain_death_still_is(ctl):
    root, personas = ctl
    claude = _binding(personas, harness="claude", profile="primary")
    # the session dies at once (well under SUSTAINED_ALIVE) while a deep-sleep record is in force
    _run_one_supervised_cycle(personas, claude, root, block_result=None, plant_during_start=True)
    attempt = rc.load_last_attempt("recovery", rc._binding_runtime_slug(claude), state_root=personas)
    assert attempt["outcome"] == "success" and not attempt.get("failure_class")
    events = [json.loads(l)["event"] for l in rc._evidence_log_path("recovery", personas).read_text().splitlines()]
    assert "sleep_end_observed" in events
    # the control: the same quick death with NO sleep in force is a real process_death
    hs.clear_seat_state("recovery", root=root)
    _run_one_supervised_cycle(personas, claude, root, block_result=None)
    attempt = rc.load_last_attempt("recovery", rc._binding_runtime_slug(claude), state_root=personas)
    assert attempt["outcome"] == "failed" and attempt["failure_class"] == "process_death"


def test_production_seams_read_the_handoff_path_from_the_binding(ctl):
    root, personas = ctl
    claude = _binding(personas, harness="claude", profile="primary")
    seams = rc.build_seat_sleep_seams("recovery", [claude], state_root=personas)
    assert seams.handoff_path == Path(claude.account_home) / "HANDOFF.md"
    assert seams.root == root and seams.runtime_ref == "claude/primary"


# --- lock-time identity: interference AFTER the drain's last _still_mine check --------------
# These pin `_patch`'s fresh-load identity check (state == deep-sleep AND drain_started_ts ==
# the drain's own key) and the abort that follows a lost patch. The hook fires inside a seam, i.e.
# after the preceding `_still_mine` read and before the write it guards.

HOWS = ["wake", "pause", "replaced"]
NEW_DRAIN_TS = (T0 + timedelta(seconds=7)).strftime(FMT)


def _interfere(env: Env, how: str) -> None:
    if how == "wake":
        hs.clear_seat_state(env.persona, root=env.root)
    elif how == "pause":
        plant(env, state="paused-intake")
    else:  # a new sleep: a different drain identity
        plant(env, drain_started=NEW_DRAIN_TS)


def _record_bytes(env: Env) -> bytes | None:
    path = hs.seat_state_path(env.persona, root=env.root)
    return path.read_bytes() if path.exists() else None


def _abort_reasons(env: Env) -> list[str]:
    return [p["why"] for e, p in env.events if e == "sleep_drain_aborted"]


@pytest.mark.parametrize("how", HOWS)
def test_interference_between_prompt_delivery_and_the_stamp_aborts_without_stamping(env, how):
    plant(env)

    def submit(text):
        env.submits.append(text)
        _interfere(env, how)

    assert drain.run_drain(env.seams(submit=submit)) == drain.ABORTED
    rec = env.record()
    if how == "wake":
        assert rec is None
    else:
        assert not rec.get("handoff_prompted_ts"), "the stale drain stamped the replacement record"
        if how == "replaced":
            assert rec["drain_started_ts"] == NEW_DRAIN_TS
        else:
            assert rec["state"] == "paused-intake"
    assert env.terminated == 0
    assert hs.load_sleep_marker(env.persona, root=env.root) is None
    assert "sleep_handoff_prompted" not in env.names(), "the lost stamp must not read as a delivered prompt"
    assert _abort_reasons(env) == ["record changed while stamping the handoff prompt"]


@pytest.mark.parametrize("how", HOWS)
def test_interference_during_the_receiver_stop_aborts_and_never_reaches_asleep(env, how):
    plant(env)
    env.idle = True
    snap: dict[str, bytes | None] = {}

    def stop():
        env.receiver_calls += 1
        _interfere(env, how)
        snap["after"] = _record_bytes(env)

    assert drain.run_drain(env.seams(stop_receiver=stop)) == drain.ABORTED
    assert _record_bytes(env) == snap["after"], "the stale drain wrote onto the post-interference record"
    rec = env.record()
    assert rec is None or rec["phase"] != "asleep"
    if how == "replaced":
        assert rec["phase"] == "draining" and rec["drain_started_ts"] == NEW_DRAIN_TS
    assert hs.load_sleep_marker(env.persona, root=env.root) is None
    assert "sleep_asleep" not in env.names()


@pytest.mark.parametrize("how", HOWS)
def test_interference_before_the_drain_outcome_patch_aborts_without_a_kill(env, how):
    plant(env)
    calls = {"n": 0}

    def idle():
        calls["n"] += 1
        if calls["n"] == 2:  # the second consecutive idle poll is the one that ends the wait
            _interfere(env, how)
        return True

    assert drain.run_drain(env.seams(pane_idle=idle)) == drain.ABORTED
    assert calls["n"] == 2
    rec = env.record()
    if how == "wake":
        assert rec is None
    else:
        assert rec.get("drain_outcome") is None, "the stale drain stamped drain_outcome onto the replacement"
    assert env.terminated == 0
    assert hs.load_sleep_marker(env.persona, root=env.root) is None
    assert _abort_reasons(env) == ["record changed before the session end"]


@pytest.mark.parametrize("how", ["wake", "replaced"])
def test_a_stale_drain_does_not_wipe_a_fresh_sessions_active_runtime_json(env, how):
    plant(env)
    env.alive = False

    def stop():
        env.receiver_calls += 1
        _interfere(env, how)
        env.active_path.write_text(json.dumps({"runtime": "claude/fresh"}))  # the new session's cache

    assert drain.run_drain(env.seams(stop_receiver=stop)) == drain.ABORTED
    assert json.loads(env.active_path.read_text()) == {"runtime": "claude/fresh"}
    assert env.cleared_in_phase == []
    assert _abort_reasons(env) == ["record changed before the runtime state was cleared"]
