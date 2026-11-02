"""ORG-106 L3b (T-5526): the `harness <seat> sleep` verb.

Real state files, real flock, and the repo's real instance.yaml (the supervisor and
Telegram-receiver facts are read from it, not mocked). The only seams are the tmux liveness
probe, the production drain seams, and the hold-down flag directory (a tmp dir). Nothing
touches a live seat, tmux, systemd or /var/lib/pantheon.
"""

from __future__ import annotations

import contextlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools import agent_role_map
from tools import harness
from tools import harness_state as hs
from tools import seat_sleep
from tools import seat_sleep_drain as drain
from tools import seat_state
from tools import seat_state_janitor
from tools.test_seat_sleep_drain import Env

NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)
FMT = "%Y-%m-%dT%H:%M:%SZ"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    root = tmp_path / "state"
    ledger = tmp_path / "ledger"
    hold = tmp_path / "oom-hold"
    for d in (root, ledger, hold):
        d.mkdir()
    monkeypatch.setattr(harness, "_OOM_HOLDS_PATH", tmp_path / "no-oom-holds.json")
    monkeypatch.setattr(harness, "_OOM_HOLD_DIR", hold)
    monkeypatch.setattr(harness, "_SESSION_PROBE", lambda persona: None)  # unknown: leave it to the controller
    ticks = iter(range(10**6))  # one distinct second per call: ledger ids are second-granular
    monkeypatch.setattr(
        harness, "_utc_now_ts",
        lambda: (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=next(ticks))).strftime(FMT))
    return type("Rig", (), {"root": root, "ledger": ledger, "hold": hold, "tmp": tmp_path})()


def run(rig, persona, *extra, operator="operator"):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = harness.main(["--operator", operator, persona, "sleep", *extra],
                            root=rig.root, ledger_dir=rig.ledger)
    return code, out.getvalue(), err.getvalue()


def record(rig, persona):
    return hs.load_seat_state(persona, root=rig.root)


def ledger(rig):
    rows = []
    for path in rig.ledger.rglob("*.jsonl"):
        rows += [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    return rows


# --- record shape ---------------------------------------------------------------------------

def test_default_sleep_writes_a_draining_record_and_a_ledger_entry(rig):
    code, out, err = run(rig, "artisan", "--reason", "quiet the seat", "--ticket", "T-5526")
    assert code == 0, err
    rec = record(rig, "artisan")
    assert seat_state.validate_record(rec) == []
    assert (rec["state"], rec["phase"]) == ("deep-sleep", "draining")
    assert rec["drain_started_ts"] == rec["set_ts"] == rec["phase_ts"]
    assert rec["telegram_dark_ack"] is False and rec["indefinite"] is False
    set_ts = datetime.strptime(rec["set_ts"], FMT).replace(tzinfo=timezone.utc)
    assert datetime.strptime(rec["expires"], FMT).replace(tzinfo=timezone.utc) - set_ts == timedelta(hours=24)
    assert rec["reason"] == "quiet the seat" and rec["ticket"] == "T-5526"
    assert rec["ledger_entry"] in [e["id"] for e in ledger(rig)]
    entry = [e for e in ledger(rig) if e["id"] == rec["ledger_entry"]][0]
    assert entry["action"] == "harness_override" and "sleep on artisan" in entry["summary"]
    assert "deep-sleep" in out


def test_reason_is_required(rig):
    code, _out, err = run(rig, "artisan")
    assert code == 2 and "reason" in err
    assert record(rig, "artisan") is None


@pytest.mark.parametrize("extra,ok", [
    (["--for", "14d"], True), (["--for", "15d"], False), (["--for", "15d", "--indefinite"], False),
    (["--indefinite"], True), (["--for", "30m"], True)])
def test_ttl_limits(rig, extra, ok):
    code, _out, err = run(rig, "artisan", "--reason", "x", *extra)
    assert (code == 0) is ok, err
    assert (record(rig, "artisan") is not None) is ok


def test_indefinite_has_null_expiry(rig):
    assert run(rig, "artisan", "--reason", "x", "--indefinite")[0] == 0
    rec = record(rig, "artisan")
    assert rec["expires"] is None and rec["indefinite"] is True


def test_dry_run_writes_nothing(rig):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = harness.main(["--dry-run", "--operator", "operator", "recovery", "sleep", "--reason", "x",
                             "--telegram-dark-ok"], root=rig.root, ledger_dir=rig.ledger)
    assert code == 0, err.getvalue()
    assert record(rig, "recovery") is None and not (rig.hold / "recovery").exists() and ledger(rig) == []


# --- refusals, in order, before any side effect ----------------------------------------------

# T-5698: mason moved to the control plane (runtime controller + persona-consume, like artisan),
# so codex is the remaining not-control-plane seat; mason is now covered by the allowed case below.
@pytest.mark.parametrize("persona", ["codex"])
def test_a_seat_that_is_not_control_plane_is_refused(rig, persona):
    assert agent_role_map.instance_binding(persona).get("supervisor") != "control-plane"
    code, _out, err = run(rig, persona, "--reason", "x", "--telegram-dark-ok")
    assert code == 2 and "control plane" in err
    assert record(rig, persona) is None and list(rig.hold.iterdir()) == []


def test_mason_control_plane_seat_can_sleep(rig):
    # T-5698: mason is control-plane now; sleep is allowed (no Telegram receiver, no ack needed).
    assert agent_role_map.instance_binding("mason").get("supervisor") == "control-plane"
    code, _out, err = run(rig, "mason", "--reason", "x")
    assert code == 0, err
    assert record(rig, "mason") is not None


def test_non_control_plane_is_refused_before_the_telegram_check(rig):
    code, _out, err = run(rig, "codex", "--reason", "x")  # also a long sleep, no ack
    assert code == 2 and "control plane" in err and "Telegram" not in err


@pytest.mark.parametrize("persona", ["specialist", "orchestrator", "recovery", "archivist"])
def test_a_telegram_receiver_seat_needs_the_ack_for_a_long_sleep(rig, persona):
    code, _out, err = run(rig, persona, "--reason", "x")  # default 24h > 20h
    assert code == 2 and "--telegram-dark-ok" in err
    assert record(rig, persona) is None and list(rig.hold.iterdir()) == []
    code, _out, err = run(rig, persona, "--reason", "x", "--indefinite")
    assert code == 2 and record(rig, persona) is None
    code, _out, err = run(rig, persona, "--reason", "x", "--for", "21h")
    assert code == 2


def test_the_ack_is_recorded_and_20h_needs_none(rig):
    assert run(rig, "specialist", "--reason", "x", "--for", "20h")[0] == 0
    assert record(rig, "specialist")["telegram_dark_ack"] is False
    assert run(rig, "archivist", "--reason", "x", "--telegram-dark-ok")[0] == 0
    assert record(rig, "archivist")["telegram_dark_ack"] is True


def test_the_ack_flag_on_a_non_receiver_seat_records_false(rig):
    assert run(rig, "artisan", "--reason", "x", "--telegram-dark-ok")[0] == 0
    assert record(rig, "artisan")["telegram_dark_ack"] is False


# --- A2: the receiver set, pinned against the real instance.yaml -------------------------------

def test_the_telegram_receiver_set_is_pinned_from_the_real_manifest():
    personas = sorted(set(agent_role_map.account_persona_map().values()))
    receivers = {p for p in personas if seat_sleep.is_telegram_receiver(p)}
    assert receivers == {"specialist", "archivist", "orchestrator", "recovery"}
    # All Telegram receiver seats now have a Codex bridge. Archivist joined in
    # T-5744 so a Claude cap swap preserves her direct communications surface.
    from tools import persona_bringup
    doc = agent_role_map.load_instance_document(persona_bringup.DEFAULT_MANIFEST)
    bridge = set(doc["persona_installer"]["codex_telegram_bridge"]["personas"])
    assert bridge == receivers


def test_a_manifest_fault_counts_as_a_receiver(tmp_path):
    assert seat_sleep.is_telegram_receiver("artisan", manifest_path=tmp_path / "missing.yaml") is True


# --- A3 / hold-down flag --------------------------------------------------------------------------

def test_sysorch_set_is_one_source_for_write_and_clear(rig):
    personas = sorted(set(agent_role_map.account_persona_map().values()))
    written = {p for p in personas if seat_sleep.write_sysorch_flag(p, hold_dir=rig.hold)}
    cleared = {p for p in personas if seat_sleep.clear_sysorch_flag(p, hold_dir=rig.hold)}
    assert written == cleared == {"recovery", "watchman"}


def test_sleep_writes_the_flag_for_a_sysorch_seat_before_the_record(rig):
    assert run(rig, "watchman", "--reason", "x")[0] == 0
    assert (rig.hold / "watchman").exists() and record(rig, "watchman") is not None
    assert run(rig, "artisan", "--reason", "x")[0] == 0
    assert not (rig.hold / "artisan").exists()


def test_flag_write_failure_refuses_and_leaves_no_record(rig):
    (rig.hold / "watchman").mkdir()  # a directory in the flag's place: the open fails for any uid
    code, _out, err = run(rig, "watchman", "--reason", "x")
    assert code == 2 and "oom-hold flag" in err and "privilege" in err
    assert record(rig, "watchman") is None
    assert not hs.seat_state_tombstones_path("watchman", root=rig.root).exists()


def test_record_write_failure_rolls_the_new_flag_back(rig, monkeypatch):
    def boom(*a, **k):
        raise hs.HarnessStateError("disk full")

    monkeypatch.setattr(hs, "write_seat_state", boom)
    code, _out, err = run(rig, "watchman", "--reason", "x")
    assert code == 2 and "disk full" in err
    assert not (rig.hold / "watchman").exists()


def test_wake_removes_the_flag_and_the_marker(rig):
    assert run(rig, "watchman", "--reason", "x")[0] == 0
    hs.write_sleep_marker("watchman", root=rig.root, runtime="codex/interactive",
                          drain_started_ts="t", ts=NOW.strftime(FMT), correlation_id="c")
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = harness.main(["--operator", "operator", "watchman", "wake", "--reason", "x"],
                            root=rig.root, ledger_dir=rig.ledger)
    assert code == 0, err.getvalue()
    assert not (rig.hold / "watchman").exists() and record(rig, "watchman") is None
    assert hs.load_sleep_marker("watchman", root=rig.root) is None


# --- replace / refresh -----------------------------------------------------------------------------

def test_upgrade_from_paused_intake_replaces_the_record_with_a_tombstone(rig):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        assert harness.main(["--operator", "operator", "artisan", "pause", "--reason", "p"],
                            root=rig.root, ledger_dir=rig.ledger) == 0
    assert run(rig, "artisan", "--reason", "now sleep")[0] == 0
    assert record(rig, "artisan")["state"] == "deep-sleep"
    rows = [json.loads(l) for l in hs.seat_state_tombstones_path("artisan", root=rig.root).read_text().splitlines()]
    assert rows[0]["state"] == "paused-intake" and rows[0]["ended_by"] == "replaced:operator:operator"


def test_refresh_keeps_an_asleep_phase_and_the_drain_bookkeeping(rig):
    assert run(rig, "artisan", "--reason", "first")[0] == 0
    rec = record(rig, "artisan")
    hs.write_seat_state("artisan", {**rec, "phase": "asleep", "handoff_prompted_ts": rec["set_ts"],
                                  "drain_outcome": "handoff"}, root=rig.root)
    assert run(rig, "artisan", "--reason", "extend", "--for", "48h")[0] == 0
    fresh = record(rig, "artisan")
    assert fresh["phase"] == "asleep" and fresh["drain_outcome"] == "handoff"
    assert fresh["drain_started_ts"] == rec["drain_started_ts"]
    assert fresh["reason"] == "extend"
    assert len(hs.seat_state_tombstones_path("artisan", root=rig.root).read_text().splitlines()) == 1


def test_refresh_of_a_draining_record_keeps_the_prompt_stamp(rig):
    assert run(rig, "artisan", "--reason", "first")[0] == 0
    rec = record(rig, "artisan")
    hs.write_seat_state("artisan", {**rec, "handoff_prompted_ts": rec["set_ts"]}, root=rig.root)
    assert run(rig, "artisan", "--reason", "again")[0] == 0
    fresh = record(rig, "artisan")
    assert fresh["phase"] == "draining" and fresh["handoff_prompted_ts"] == rec["set_ts"]
    assert fresh["drain_started_ts"] == rec["drain_started_ts"], "the controller's drain identity is stable"


def test_an_expired_deep_sleep_record_is_replaced_by_a_fresh_drain(rig):
    assert run(rig, "artisan", "--reason", "first")[0] == 0
    rec = record(rig, "artisan")
    hs.write_seat_state("artisan", {**rec, "phase": "asleep", "expires": "2020-01-01T00:00:00Z",
                                  "set_ts": "2019-12-31T00:00:00Z", "phase_ts": "2019-12-31T00:00:00Z",
                                  "drain_started_ts": "2019-12-31T00:00:00Z"}, root=rig.root)
    assert run(rig, "artisan", "--reason", "new")[0] == 0
    assert record(rig, "artisan")["phase"] == "draining"


# --- A1: a seat with no session goes straight to asleep -----------------------------------------------

def test_no_live_session_goes_straight_to_asleep_with_no_session(rig, monkeypatch):
    env = Env(rig.tmp, persona="artisan", root=rig.root)
    env.alive = False
    monkeypatch.setattr(harness, "_SESSION_PROBE", lambda persona: False)
    monkeypatch.setattr(harness, "_FAST_PATH_SEAMS", lambda persona, root: env.seams())
    code, out, err = run(rig, "artisan", "--reason", "x")
    assert code == 0, err
    rec = record(rig, "artisan")
    assert rec["phase"] == "asleep" and rec["drain_outcome"] == "no-session"
    assert not env.active_path.exists() and env.terminated == 0 and env.submits == []
    assert "no live session" in out


def test_a_live_or_unknown_session_is_left_to_the_controller(rig, monkeypatch):
    env = Env(rig.tmp, persona="artisan", root=rig.root)

    def must_not_run(*a, **k):
        raise AssertionError("the verb must not drain a seat that may have a live session")

    monkeypatch.setattr(harness, "_FAST_PATH_SEAMS", must_not_run)
    for probe in (True, None):
        monkeypatch.setattr(harness, "_SESSION_PROBE", lambda persona, p=probe: p)
        assert run(rig, "artisan", "--reason", "x")[0] == 0
        assert record(rig, "artisan")["phase"] == "draining"
    del env


def test_wait_returns_once_the_record_is_asleep(rig, monkeypatch):
    def controller_finishes(_secs):
        rec = record(rig, "artisan")
        hs.write_seat_state("artisan", {**rec, "phase": "asleep", "drain_outcome": "handoff"}, root=rig.root)

    monkeypatch.setattr(harness, "_wait_sleep", controller_finishes)
    code, out, err = run(rig, "artisan", "--reason", "x", "--wait")
    assert code == 0, err
    assert "asleep" in out


def test_wait_times_out_with_a_clear_error_and_the_record_stays(rig, monkeypatch):
    monkeypatch.setattr(harness, "_wait_sleep", lambda s: None)
    monkeypatch.setattr(harness, "SLEEP_WAIT_TIMEOUT_S", 6.0)
    code, _out, err = run(rig, "artisan", "--reason", "x", "--wait")
    assert code == 2 and "still draining" in err
    assert record(rig, "artisan")["phase"] == "draining"


# --- repeated flag-removal failure alerts (wake path and janitor path) ----------------------------------

def _stick_flag(rig, persona="watchman"):
    """Plant a sleeping sysorch seat whose flag cannot be removed (a directory in its place)."""
    assert run(rig, persona, "--reason", "x")[0] == 0
    flag = rig.hold / persona
    flag.unlink()
    flag.mkdir()
    (flag / "keep").write_text("x")  # unlink() on a non-empty directory raises an OSError
    return flag


def _wake(rig, persona="watchman"):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        return harness.main(["--operator", "operator", persona, "wake", "--reason", "x"],
                            root=rig.root, ledger_dir=rig.ledger)


def test_wake_flag_failure_pages_only_after_a_repeat_and_then_by_cooldown(rig, monkeypatch):
    alerts: list[tuple] = []
    monkeypatch.setattr("tools.inbox_consumer.route_failure_alert", lambda *a: alerts.append(a))
    _stick_flag(rig)
    for _ in range(seat_sleep.FLAG_FAILURE_ALERT_AFTER - 1):
        assert _wake(rig) == 2
    assert alerts == []
    assert _wake(rig) == 2
    assert len(alerts) == 1 and alerts[0][0] == "watchman" and alerts[0][2] == "oom_hold_flag_stuck"
    assert record(rig, "watchman") is not None, "the record is kept while the flag is stuck"
    for _ in range(4):
        assert _wake(rig) == 2
    assert len(alerts) == 1, "cooldown-gated"


def test_cooldown_expiry_pages_again_and_success_resets_the_counter(rig, monkeypatch):
    alerts: list[tuple] = []
    monkeypatch.setattr("tools.inbox_consumer.route_failure_alert", lambda *a: alerts.append(a))
    flag = _stick_flag(rig)
    for _ in range(seat_sleep.FLAG_FAILURE_ALERT_AFTER):
        _wake(rig)
    assert len(alerts) == 1
    path = seat_sleep.flag_failures_path("watchman", root=rig.root)
    doc = json.loads(path.read_text())
    doc["last_alert_ts"] = (datetime.now(timezone.utc) - seat_sleep.FLAG_FAILURE_ALERT_COOLDOWN
                            - timedelta(minutes=1)).strftime(FMT)
    path.write_text(json.dumps(doc))
    assert _wake(rig) == 2 and len(alerts) == 2
    (flag / "keep").unlink()
    flag.rmdir()  # the operator fixed it
    assert _wake(rig) == 0
    assert not path.exists(), "a successful removal resets the counter"


def test_janitor_expiry_flag_failure_pages_after_a_repeat(rig, monkeypatch):
    alerts: list[tuple] = []
    monkeypatch.setattr("tools.inbox_consumer.route_failure_alert", lambda *a: alerts.append(a))
    flag = _stick_flag(rig)
    rec = record(rig, "watchman")
    hs.write_seat_state("watchman", {**rec, "expires": "2020-01-01T00:00:00Z", "set_ts": "2019-12-31T00:00:00Z",
                                     "phase_ts": "2019-12-31T00:00:00Z", "drain_started_ts": "2019-12-31T00:00:00Z"},
                        root=rig.root)
    for i in range(seat_sleep.FLAG_FAILURE_ALERT_AFTER):
        assert seat_state_janitor.tick("watchman", state_root=rig.root, hold_dir=rig.hold,
                                       send_mail=lambda *a: None) == ["expire_flag_failed"]
        assert len(alerts) == (1 if i == seat_sleep.FLAG_FAILURE_ALERT_AFTER - 1 else 0)
    assert record(rig, "watchman") is not None
    (flag / "keep").unlink()
    flag.rmdir()
    assert seat_state_janitor.tick("watchman", state_root=rig.root, hold_dir=rig.hold,
                                   send_mail=lambda *a: None) == ["expire"]
    assert record(rig, "watchman") is None
    assert not seat_sleep.flag_failures_path("watchman", root=rig.root).exists()


def test_the_alert_helper_never_raises(rig):
    def boom(*a):
        raise RuntimeError("mail down")

    seat_sleep.note_flag_failure("watchman", rig.root, OSError("x"))
    for _ in range(3):
        seat_sleep.note_flag_failure("watchman", rig.root, OSError("x"))
    assert seat_sleep.maybe_alert_flag_failure("watchman", rig.root, alert_fn=boom) is False


# --- the telegram surface --------------------------------------------------------------------------------

def test_telegram_sleep_verb_parses_and_prompts_for_a_reason():
    from tools import harness_telegram as ht

    allow = frozenset({1})
    res = ht.parse_harness_message("/harness artisan sleep --for 8h --reason 'night'", 1, allow_from=allow)
    assert isinstance(res, ht.ParsedInvocation) and res.args.command == "sleep"
    res = ht.parse_harness_message("/harness artisan sleep --for 8h", 1, allow_from=allow)
    assert isinstance(res, ht.ReasonPrompt) and res.command == "sleep"
    res = ht.parse_harness_message("/harness artisan sleep --for 15d --reason x", 1, allow_from=allow)
    assert isinstance(res, ht.ParseError)
    assert isinstance(ht.parse_harness_message("/harness artisan sleep --reason x", 2, allow_from=allow),
                      ht.AuthRefusal)


# --- the end-to-end path: sleep -> asleep -> expiry -> launch (risk 9) ------------------------------------------

def test_sleep_then_asleep_then_expiry_then_the_next_consume_launches(rig, monkeypatch):
    """verb -> drain -> asleep (nothing launches) -> the record expires -> the launch predicate opens.
    Cap cooldowns are untouched throughout."""
    from tools import runtime_controller as rc
    from tools.test_runtime_controller import _binding

    monkeypatch.setattr(seat_state, "DEFAULT_OOM_HOLDS", rig.tmp / "no-oom-holds.json")
    personas = rig.root / "personas"
    claude = _binding(personas, harness="claude", profile="primary")
    claude.persona = "recovery"
    rc.record_last_attempt("recovery", claude, outcome="failed", failure_class="subscription_cap",
                           trigger="t", state_root=personas, now=rc._utc_now(),
                           cooldown_until_override=rc._utc_now() + timedelta(days=2))
    cooldown_before = rc.load_last_attempt("recovery", rc._binding_runtime_slug(claude), state_root=personas)

    env = Env(rig.tmp, persona="recovery", root=rig.root)
    env.alive = False
    monkeypatch.setattr(harness, "_SESSION_PROBE", lambda persona: False)
    monkeypatch.setattr(harness, "_FAST_PATH_SEAMS", lambda persona, root: env.seams())
    assert run(rig, "recovery", "--reason", "e2e", "--for", "20h")[0] == 0
    rec = record(rig, "recovery")
    assert rec["phase"] == "asleep"
    assert (rig.hold / "recovery").exists(), "recovery is a system orchestrator: the hold-down flag is up"

    assert seat_sleep.launch_block("recovery", root=rig.root).sleeping
    with pytest.raises(rc.RuntimeViabilityBlocked):
        rc._refuse_sleeping_seat_launch(claude, state_root=personas)

    # expiry (no wake): the janitor ends the record and takes the flag down; the reader already
    # treated it as active the moment it expired
    expired = {**rec, "expires": "2020-01-01T00:00:00Z", "set_ts": "2019-12-31T00:00:00Z",
               "phase_ts": "2019-12-31T00:00:00Z", "drain_started_ts": "2019-12-31T00:00:00Z"}
    hs.write_seat_state("recovery", expired, root=rig.root)
    assert not seat_sleep.launch_block("recovery", root=rig.root).sleeping
    rc._refuse_sleeping_seat_launch(claude, state_root=personas)  # no longer raises: consume may launch
    assert seat_state_janitor.tick("recovery", state_root=rig.root, hold_dir=rig.hold,
                                   send_mail=lambda *a: None) == ["expire"]
    assert not (rig.hold / "recovery").exists() and record(rig, "recovery") is None
    assert rc.load_last_attempt("recovery", rc._binding_runtime_slug(claude), state_root=personas) == cooldown_before, \
        "sleep, wake and expiry never touched the cap cooldown"


def test_pause_over_a_draining_record_removes_the_sleep_marker_and_the_flag(rig):
    """Risk 8: the downgrade path must not leave a stale marker that could mask a later death."""
    assert run(rig, "watchman", "--reason", "x")[0] == 0  # draining; watchman is a sysorch seat
    hs.write_sleep_marker("watchman", root=rig.root, runtime="codex/interactive",
                          drain_started_ts=record(rig, "watchman")["drain_started_ts"],
                          ts=datetime.now(timezone.utc).strftime(FMT), correlation_id="c")
    assert (rig.hold / "watchman").exists()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = harness.main(["--operator", "operator", "watchman", "pause", "--reason", "back to intake-only"],
                            root=rig.root, ledger_dir=rig.ledger)
    assert code == 0, err.getvalue()
    assert record(rig, "watchman")["state"] == "paused-intake"
    assert hs.load_sleep_marker("watchman", root=rig.root) is None
    assert not (rig.hold / "watchman").exists()
