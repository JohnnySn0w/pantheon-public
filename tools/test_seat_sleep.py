"""ORG-106 L3a (T-5526): DEEP-SLEEP launch-side enforcement.

Everything here runs on temp roots with fakes. No real seat state, no systemctl, no tmux,
no /var/lib/pantheon. Records are hand-planted: L3a ships no way to write a deep-sleep
record (the `sleep` verb is L3b).
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from tools import harness_state as hs
from tools import persona_bringup
from tools import runtime_controller as rc
from tools import seat_sleep
from tools import seat_state
from tools import seat_state_janitor
from tools.test_runtime_controller import _Adapter, _binding
from tools.test_t5282_consume_seat_maintenance import _arm, _run, _token
from tools.test_persona_bringup_consume_e2e import _Rig

FMT = "%Y-%m-%dT%H:%M:%SZ"


def _ts(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).strftime(FMT)


def _plant(seat_root: Path, persona: str = "recovery", *, state: str = "deep-sleep",
           phase: str = "asleep", expires: str | None = None, indefinite: bool = False) -> Path:
    doc = {
        "schema": hs.SCHEMA_SEAT_STATE, "persona": persona, "state": state, "phase": phase,
        "phase_ts": _ts(timedelta(hours=-1)), "set_ts": _ts(timedelta(hours=-1)),
        "set_by": "operator:t", "owner": "operator", "reason": "L3a test", "ticket": None,
        "expires": None if indefinite else (expires or _ts(timedelta(hours=5))),
        "indefinite": indefinite, "telegram_dark_ack": False, "pre_expiry_notified": False,
        "ledger_entry": "e",
    }
    path = hs.seat_state_path(persona, root=seat_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    return path


@pytest.fixture
def seat(tmp_path, monkeypatch):
    """(seat_root, personas_dir): a runtime plane's personas dir and the seat-state root above it."""
    root = tmp_path / "state"
    personas = root / "personas"
    personas.mkdir(parents=True)
    monkeypatch.setattr(seat_state, "DEFAULT_OOM_HOLDS", tmp_path / "no-oom-holds.json")
    # ORG-106 L3b: the controller's sleep gate asks whether a session is live; never ask real tmux.
    monkeypatch.setattr(rc, "_seat_sleep_session_alive", lambda *a, **k: False)
    return root, personas


def _events(personas: Path, persona: str = "recovery") -> list[dict]:
    path = rc._evidence_log_path(persona, personas)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- the shared predicate ---------------------------------------------------------------

@pytest.mark.parametrize("phase", ["draining", "asleep"])
def test_both_deep_sleep_phases_block_launch(seat, phase):
    root, _ = seat
    _plant(root, phase=phase)
    block = seat_sleep.launch_block("recovery", root=root)
    assert block.sleeping and block.cause == seat_sleep.CAUSE_SLEEP


def test_expired_paused_and_absent_do_not_block(seat):
    root, _ = seat
    assert not seat_sleep.launch_block("recovery", root=root).sleeping
    _plant(root, state="paused-intake", phase="set")
    assert not seat_sleep.launch_block("recovery", root=root).sleeping
    _plant(root, expires=_ts(timedelta(minutes=-1)))
    assert not seat_sleep.launch_block("recovery", root=root).sleeping


def test_oom_hold_is_observed_not_a_sleep(seat, tmp_path):
    root, _ = seat
    holds = tmp_path / "holds.json"
    holds.write_text(json.dumps({"recovery": {"reason": "oom kill"}}))
    block = seat_sleep.launch_block("recovery", root=root, oom_holds_path=holds)
    assert block.oom_held and not block.sleeping and block.cause is None


def test_reader_fault_fails_open(seat):
    root, _ = seat
    block = seat_sleep.launch_block("recovery", root=root, reader=mock.Mock(side_effect=RuntimeError("boom")))
    assert block.cause is None and "boom" in (block.fault or "")


def test_corrupt_record_fails_open(seat):
    root, _ = seat
    path = hs.seat_state_path("recovery", root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    block = seat_sleep.launch_block("recovery", root=root)
    assert block.cause is None and block.fault


# --- launch-check CLI (shell launchers) -------------------------------------------------

def test_launch_check_exit_codes(seat):
    root, _ = seat
    args = ["launch-check", "recovery", "--state-root", str(root)]
    assert seat_state.main(args) == 0
    _plant(root)
    err = io.StringIO()
    with redirect_stderr(err):
        assert seat_state.main(args) == seat_state.LAUNCH_CHECK_BLOCKED == seat_sleep.EXIT_BLOCKED_SLEEP
    assert "deep-sleep" in err.getvalue() and "wake" in err.getvalue()


def test_launch_check_fails_open_with_a_stderr_line(seat):
    root, _ = seat
    path = hs.seat_state_path("recovery", root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    err = io.StringIO()
    with redirect_stderr(err):
        assert seat_state.main(["launch-check", "recovery", "--state-root", str(root)]) == 0
    assert "fail-open" in err.getvalue()


def test_launch_check_resolves_an_account(seat):
    root, _ = seat
    _plant(root, "recovery")
    with mock.patch.object(seat_state.agent_role_map, "account_persona", return_value="recovery"):
        with redirect_stderr(io.StringIO()):
            assert seat_state.main(["launch-check", "--account", "claude-fallback", "--state-root", str(root)]) == 3
    with mock.patch.object(seat_state.agent_role_map, "account_persona", return_value=None):
        with redirect_stderr(io.StringIO()):
            assert seat_state.main(["launch-check", "--account", "nobody", "--state-root", str(root)]) == 0


# --- runtime_controller launch seams ----------------------------------------------------

def test_chokepoint_refuses_with_cause_seat_sleep(seat):
    root, personas = seat
    _plant(root)
    binding = _binding(personas, harness="claude", profile="primary")
    with pytest.raises(rc.RuntimeViabilityBlocked) as ctx:
        rc._refuse_held_runtime_launch(binding, state_root=personas)
    assert ctx.value.reason == "seat-sleep"
    assert ctx.value.policy_skip_reason == "seat_sleep"
    assert [e["event"] for e in _events(personas)] == ["runtime_launch_refused_seat_sleep"]


def test_chokepoint_allows_active_and_paused(seat):
    root, personas = seat
    binding = _binding(personas, harness="claude", profile="primary")
    rc._refuse_held_runtime_launch(binding, state_root=personas)
    _plant(root, state="paused-intake", phase="set")
    rc._refuse_held_runtime_launch(binding, state_root=personas)


def test_chokepoint_observes_an_oom_hold_without_refusing(seat, tmp_path):
    root, personas = seat
    holds = tmp_path / "holds.json"
    holds.write_text(json.dumps({"recovery": {"reason": "oom kill"}}))
    seat_state.DEFAULT_OOM_HOLDS = holds  # monkeypatched by the fixture, restored there
    binding = _binding(personas, harness="claude", profile="primary")
    rc._refuse_held_runtime_launch(binding, state_root=personas)  # no raise
    assert [e["event"] for e in _events(personas)] == ["launch_attempt_oom_held"]


def test_lowest_launch_primitive_refuses_before_touching_the_adapter(seat):
    root, personas = seat
    _plant(root)
    binding = _binding(personas, harness="claude", profile="primary")
    adapter = mock.Mock()
    with pytest.raises(rc.RuntimeViabilityBlocked):
        rc._start_runtime(binding, adapter, replace=False, state_root=personas)
    adapter.build_interactive_launch.assert_not_called()


def test_controller_start_refuses_before_attach_or_respawn(seat):
    root, personas = seat
    _plant(root)
    binding = _binding(personas, harness="claude", profile="primary")
    with mock.patch.object(rc, "_runtime_alive") as alive, mock.patch.object(rc, "_start_runtime") as start:
        with pytest.raises(rc.RuntimeViabilityBlocked):
            rc._controller_start_runtime(binding, state_root=personas)
    alive.assert_not_called()
    start.assert_not_called()


def test_ensure_runtime_replace_path_is_behind_the_primitive(seat):
    # the planned-swap / continuity coordinator launch via _replace_runtime -> _start_runtime
    root, personas = seat
    _plant(root)
    binding = _binding(personas, harness="claude", profile="primary")
    with mock.patch.object(rc, "build_adapter", create=True), \
            mock.patch("tools.persona_bringup.build_adapter", return_value=mock.Mock()):
        with pytest.raises(rc.RuntimeViabilityBlocked):
            rc._replace_runtime(binding, state_root=personas)


def test_selection_skips_every_candidate_with_cause_seat_sleep(seat):
    root, personas = seat
    primary = _binding(personas, harness="claude", profile="primary")
    fallback = _binding(personas, harness="codex", profile="interactive")
    chosen, skipped = rc._select_viable_runtime("recovery", [primary, fallback], {}, rc._utc_now(),
                                                state_root=personas)
    assert chosen is primary and skipped == []
    _plant(root)
    chosen, skipped = rc._select_viable_runtime("recovery", [primary, fallback], {}, rc._utc_now(),
                                                state_root=personas)
    assert chosen is None
    assert [r["reason"] for r in skipped] == ["seat-sleep", "seat-sleep"]


def test_initial_viability_records_skipped_held_seat_sleep_and_launches_nothing(seat):
    root, personas = seat
    _plant(root)
    primary = _binding(personas, harness="claude", profile="primary")
    fallback = _binding(personas, harness="codex", profile="interactive")
    with mock.patch.object(rc, "_matching_recorded_runtime_selection", return_value=None), \
            mock.patch.object(rc, "ensure_runtime") as ensure:
        with pytest.raises(rc.RuntimeViabilityBlocked) as ctx:
            rc.ensure_viable_runtime((primary, fallback), adapter_factory=_Adapter, state_root=personas)
    ensure.assert_not_called()
    assert ctx.value.policy_skip_reason == "seat_sleep"
    assert [(a["status"], a["cause"]) for a in ctx.value.evidence["attempts"]] == [
        ("skipped-held", "seat-sleep"), ("skipped-held", "seat-sleep")]


# --- controller loop ---------------------------------------------------------------------

def _fake_start(started: list):
    def start(binding):
        started.append(rc._binding_runtime_ref(binding))
        return (rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False),
                rc.RuntimeHandle(binding.launch, binding.controller.tmux, None, rc._utc_now()))
    return start


def test_controller_waits_in_process_and_never_launches_while_asleep(seat):
    root, personas = seat
    _plant(root)
    claude = _binding(personas, harness="claude", profile="primary")
    started: list = []
    sleeps: list[float] = []
    result = rc.controller_main(
        "recovery", candidates=[claude], state_root=personas, sleep=sleeps.append,
        start_runtime_fn=_fake_start(started), block_until_death_fn=lambda b: None,
        persist_record_fn=lambda *a, **k: "persisted", max_reselect_cycles=3,
    )
    assert result == 1 and started == []
    assert sleeps == [rc.SEAT_SLEEP_RECHECK_SECONDS] * 3
    assert [e["event"] for e in _events(personas)].count("controller_seat_sleep_wait") == 1
    assert rc.load_last_attempt("recovery", rc._binding_runtime_slug(claude), state_root=personas) is None


def test_controller_resumes_launching_on_wake_and_cooldowns_still_bind(seat):
    root, personas = seat
    path = _plant(root)
    claude = _binding(personas, harness="claude", profile="primary")
    codex = _binding(personas, harness="codex", profile="interactive")
    # the claude cap cooldown is a fact the wake must not clear
    rc.record_last_attempt("recovery", claude, outcome="failed", failure_class="subscription_cap",
                           trigger="t", state_root=personas, now=rc._utc_now(),
                           cooldown_until_override=rc._utc_now() + timedelta(hours=3))
    started: list = []
    calls = {"n": 0}

    def sleeper(_s):
        calls["n"] += 1
        path.unlink()  # `harness wake` ran

    result = rc.controller_main(
        "recovery", candidates=[claude, codex], state_root=personas, sleep=sleeper,
        start_runtime_fn=_fake_start(started), block_until_death_fn=lambda b: None,
        persist_record_fn=lambda *a, **k: "persisted", max_reselect_cycles=3,
    )
    assert result == 0
    assert started == ["codex/interactive"], "wake neither launched the capped runtime nor cleared its cooldown"
    attempt = rc.load_last_attempt("recovery", rc._binding_runtime_slug(claude), state_root=personas)
    assert attempt["failure_class"] == "subscription_cap" and attempt["cooldown_until"]


def test_expiry_relaunches_without_any_wake(seat):
    root, personas = seat
    _plant(root, expires=_ts(timedelta(minutes=-1)))
    claude = _binding(personas, harness="claude", profile="primary")
    started: list = []
    result = rc.controller_main(
        "recovery", candidates=[claude], state_root=personas, sleep=lambda s: None,
        start_runtime_fn=_fake_start(started), block_until_death_fn=lambda b: None,
        persist_record_fn=lambda *a, **k: "persisted", max_reselect_cycles=2,
    )
    assert result == 0 and started == ["claude/primary"]


def test_sleep_set_between_selection_and_start_is_a_clean_exit_not_a_failure(seat):
    root, personas = seat
    claude = _binding(personas, harness="claude", profile="primary")

    def racing_start(binding):
        _plant(root)
        raise rc._seat_sleep_block_error("recovery", rc._binding_runtime_ref(binding), seat_sleep.LaunchBlock())

    result = rc.controller_main(
        "recovery", candidates=[claude], state_root=personas, sleep=lambda s: None,
        start_runtime_fn=racing_start, block_until_death_fn=lambda b: None,
        persist_record_fn=lambda *a, **k: "persisted",
    )
    assert result == 0
    assert rc.load_last_attempt("recovery", rc._binding_runtime_slug(claude), state_root=personas) is None


def test_launch_breach_pages_when_a_launch_lands_under_deep_sleep(seat):
    root, personas = seat
    claude = _binding(personas, harness="claude", profile="primary")
    started: list = []

    def bypassing_start(binding):  # an unrouted launcher: starts without asking the chokepoint
        _plant(root)
        return _fake_start(started)(binding)

    with mock.patch.object(rc, "_route_failure_alert") as route:
        rc.controller_main(
            "recovery", candidates=[claude], state_root=personas, sleep=lambda s: None,
            start_runtime_fn=bypassing_start, block_until_death_fn=lambda b: None,
            persist_record_fn=lambda *a, **k: "persisted",
        )
        # a second breach inside the cooldown does not page again
        rc._note_launch_breach("recovery", "claude/primary", state_root=personas)
    events = [e["event"] for e in _events(personas)]
    assert events.count("launch_breach") == 2
    route.assert_called_once()
    assert "launch_breach" in route.call_args.args[1]


def test_launch_breach_pages_for_a_launch_under_an_oom_hold(seat, tmp_path):
    root, personas = seat
    holds = tmp_path / "holds.json"
    holds.write_text(json.dumps({"recovery": {"reason": "oom kill"}}))
    seat_state.DEFAULT_OOM_HOLDS = holds
    with mock.patch.object(rc, "_route_failure_alert") as route:
        rc._note_launch_breach("recovery", "claude/primary", state_root=personas)
    route.assert_called_once()
    assert [e["event"] for e in _events(personas)] == ["launch_breach"]


def test_no_breach_when_nothing_blocks(seat):
    root, personas = seat
    with mock.patch.object(rc, "_route_failure_alert") as route:
        rc._note_launch_breach("recovery", "claude/primary", state_root=personas)
    route.assert_not_called()
    assert _events(personas) == []


# --- consume plane -----------------------------------------------------------------------

def _consume(tmp_path, monkeypatch):
    state = tmp_path / "seatstate"
    monkeypatch.setenv("PANTHEON_STATE_DIR", str(state))
    monkeypatch.delenv("PANTHEON_PERSONA_STATE_DIR", raising=False)
    monkeypatch.setenv("PANTHEON_MAINTENANCE_REGISTRY", str(tmp_path / "absent.json"))
    monkeypatch.setattr(seat_state, "DEFAULT_OOM_HOLDS", tmp_path / "no-oom-holds.json")
    return state


def test_consume_skips_the_whole_cycle_on_deep_sleep(tmp_path, monkeypatch):
    state = _consume(tmp_path, monkeypatch)
    _plant(state)
    rig = _Rig(tmp_path)
    code, out, mail, m = _run(rig)
    assert code == 0, out
    assert out[-1]["consume"]["skipped"] == "seat_sleep"
    for name in ("wake", "ensure_viable", "start", "rehydrate", "ensure_runtime"):
        m[name].assert_not_called()
    rig.fakes["submit"].assert_not_called()
    assert mail.exists(), "mail stays queued, unclaimed, for the wake"
    assert persona_bringup.load_policy_skip(rig.state, "recovery")["reason"] == "seat_sleep"


def test_maintenance_bypass_token_never_bypasses_deep_sleep(tmp_path, monkeypatch):
    state = _consume(tmp_path, monkeypatch)
    _arm(tmp_path, monkeypatch, expires_delta=timedelta(hours=1))
    _plant(state)
    rig = _Rig(tmp_path)
    tok = _token(rig)
    code, out, _mail, m = _run(rig)
    assert out[-1]["consume"]["skipped"] == "seat_sleep"
    m["wake"].assert_not_called()
    m["start"].assert_not_called()
    assert not tok.exists(), "the token is still single-use: read and cleared, but not honoured"


def test_paused_intake_consume_proceeds(tmp_path, monkeypatch):
    state = _consume(tmp_path, monkeypatch)
    _plant(state, state="paused-intake", phase="set")
    rig = _Rig(tmp_path)
    code, out, _mail, m = _run(rig)
    assert code == 0, out
    m["wake"].assert_called_once()


def test_expired_sleep_relaunches_on_the_next_consume_fire(tmp_path, monkeypatch):
    state = _consume(tmp_path, monkeypatch)
    _plant(state, expires=_ts(timedelta(minutes=-1)))
    rig = _Rig(tmp_path)
    code, out, _mail, m = _run(rig)
    assert code == 0, out
    assert out[-1]["consume"].get("skipped") != "seat_sleep"
    m["wake"].assert_called_once()


def test_consume_reader_fault_fails_open(tmp_path, monkeypatch):
    _consume(tmp_path, monkeypatch)
    rig = _Rig(tmp_path)
    with mock.patch("tools.seat_sleep.launch_block", side_effect=RuntimeError("boom")):
        code, out, _mail, m = _run(rig)
    assert code == 0, out
    m["wake"].assert_called_once()


# --- janitor expiry ----------------------------------------------------------------------

def test_janitor_expiry_tombstones_and_clears_the_sysorch_flag(seat, tmp_path):
    root, _ = seat
    _plant(root, expires=_ts(timedelta(minutes=-5)))
    hold = tmp_path / "hold"
    hold.mkdir()
    (hold / "recovery").write_text("x")
    sent: list = []
    actions = seat_state_janitor.tick(
        "recovery", state_root=root, send_mail=lambda *a: sent.append(a), hold_dir=hold)
    assert actions == ["expire"]
    assert not (hold / "recovery").exists()
    assert hs.load_seat_state("recovery", root=root) is None
    row = json.loads(hs.seat_state_tombstones_path("recovery", root=root).read_text().splitlines()[-1])
    assert row["ended_by"] == "expiry"


def test_janitor_flag_failure_keeps_the_record_for_the_next_tick(seat, tmp_path):
    root, _ = seat
    _plant(root, expires=_ts(timedelta(minutes=-5)))
    with mock.patch.object(seat_sleep, "clear_sysorch_flag", side_effect=PermissionError("no")):
        actions = seat_state_janitor.tick("recovery", state_root=root, send_mail=lambda *a: None)
    assert actions == ["expire_flag_failed"]
    assert hs.load_seat_state("recovery", root=root) is not None


def test_janitor_expiry_leaves_a_non_sysorch_flag_and_pause_alone(seat, tmp_path):
    root, _ = seat
    hold = tmp_path / "hold"
    hold.mkdir()
    (hold / "specialist").write_text("manual")
    _plant(root, "specialist", expires=_ts(timedelta(minutes=-5)))
    seat_state_janitor.tick("specialist", state_root=root, send_mail=lambda *a: None, hold_dir=hold)
    assert (hold / "specialist").exists()


def test_sysorch_set_is_exactly_recovery_and_watchman():
    for persona in ("recovery", "watchman"):
        assert seat_sleep.is_sysorch(persona)
    for persona in ("orchestrator", "artisan", "mason", "specialist", "archivist", "codex"):
        assert not seat_sleep.is_sysorch(persona)
