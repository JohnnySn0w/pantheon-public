"""Tests for tools/codex_thread_rotate.py (T-6115 D-3). Hermetic: tmp dirs and fakes only; no
systemctl, tmux, mailbox, ~/.codex or live seat is touched."""

from __future__ import annotations

import json
import grp
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import chronicle
from tools import codex_handoff_checkpoint as chk
from tools import codex_thread_rotate as rot
from tools import session_chronicle as sc

NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)
OLD_ID = "019aaaaa-0000-7000-8000-000000000001"
NEW_ID = "019bbbbb-0000-7000-8000-000000000002"
WRITER = "codex-seat-delivery.service"
DAEMON = "codex-daemon.service"
RT_STARTED = "2026-11-01T08:00:00.000000Z"


class Fixture:
    def __init__(self, tmp: Path, persona: str = "codex", *, age_days: float = 5, size: int = 100) -> None:
        self.home = tmp / "home"
        self.state = self.home / ".local/state/pantheon/codex-appserver"
        self.codex_home = self.home / ".codex"
        self.chron = tmp / "chronicle"
        self.pstate = tmp / "personas"
        self.logs = tmp / "logs"
        for d in (self.state, self.chron, self.pstate, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        self.persona = persona
        started = NOW - timedelta(days=age_days)
        self.rollout = self.codex_home / "sessions/2026/10/29" / f"rollout-2026-10-29T10-00-00-{OLD_ID}.jsonl"
        self.rollout.parent.mkdir(parents=True)
        first = {"type": "session_meta", "payload": {"id": OLD_ID, "timestamp": started.isoformat().replace("+00:00", "Z")}}
        self.rollout.write_text(json.dumps(first) + "\n" + "x" * size + "\n")
        self.record = {"thread_id": OLD_ID, "materialized": True, "binding": {"model": "m"}}
        (self.state / "thread.json").write_text(json.dumps(self.record))
        (self.home / "HANDOFF.md").write_text("# handoff\n")
        self.active = {WRITER: True, DAEMON: True}
        self.events: list[str] = []
        self.sent: list[tuple[str, str, str]] = []
        self.busy_pane = False
        self.checkpoint_status = "written"
        self.fail_start_daemon = False
        self.fail_session_end = False
        self.no_new_thread = False
        self.now = NOW.timestamp()
        self.audit_old = True

    # ---- seams
    def run_cmd(self, argv):
        assert argv[:2] == ["systemctl", "--user"], argv
        verb, unit = argv[2], argv[3]
        if verb == "is-active":
            return SimpleNamespace(returncode=0 if self.active[unit] else 3,
                                   stdout="active\n" if self.active[unit] else "inactive\n", stderr="")
        if verb == "stop":
            self.events.append(f"stop:{unit}")
            self.active[unit] = False
        elif verb == "start":
            self.events.append(f"start:{unit}")
            if unit == DAEMON:
                self.events.append("thread.json:" + ("present" if (self.state / "thread.json").exists() else "absent"))
                if self.fail_start_daemon and not getattr(self, "_failed_once", False):
                    self._failed_once = True
                    return SimpleNamespace(returncode=1, stdout="", stderr="boom")
                if not (self.state / "thread.json").exists() and not self.no_new_thread:
                    (self.state / "thread.json").write_text(json.dumps({"thread_id": NEW_ID, "materialized": True}))
            self.active[unit] = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def sleep(self, s):
        self.now += s

    def clock(self):
        return self.now

    def send(self, inbox, payload, subject):
        self.events.append("seed")
        self.sent.append((inbox, payload, subject))

    def pane_busy(self):
        return self.busy_pane

    def checkpoint_fn(self, **kw):
        self.events.append("checkpoint")
        return chk.Outcome(self.checkpoint_status, "t", 0 if self.checkpoint_status != "no-ack" else 3)

    def archive_fn(self, cfg, thread_id, rollout, ts):
        self.events.append("archive")
        return {"path": f"/chron/codex/{thread_id}.gz", "bytes": 5, "sha256": "ab" * 32, "source_bytes": 9}

    def session_end_fn(self, cfg, pointer, thread_id, when=None):
        self.events.append("session-end")
        if self.fail_session_end:
            return {"status": "failed", "error": "x"}
        return {"status": "ok", "results": [{"status": "archived", "record": "/chron/x.md.gz"}]}

    def cfg(self, **kw):
        base = dict(persona=self.persona, home=self.home, state_dir=self.state, codex_home=self.codex_home,
                    max_age_days=3.0, max_mb=60.0, chronicle_root=self.chron,
                    persona_state_root=self.pstate, log_dir=self.logs,
                    archive_group=ARCHIVE_GROUP)
        base.update(kw)
        return rot.Config(**base)

    def go(self, **kw):
        fakes = kw.pop("fakes", True)
        extra = dict(checkpoint_fn=self.checkpoint_fn, archive_fn=self.archive_fn,
                     session_end_fn=self.session_end_fn) if fakes else {}
        extra.setdefault("group_problem", kw.pop("group_problem", lambda g: None))
        return rot.rotate(self.cfg(**kw), run_cmd=self.run_cmd, sleep=self.sleep, clock=self.clock,
                          send=self.send, pane_busy=self.pane_busy, **extra)

    def thread_id(self):
        return json.loads((self.state / "thread.json").read_text())["thread_id"]

    def history(self):
        return sorted((self.state / "thread-history").glob("*.json"))


# Hermetic (#1622 CI): the process's own egid group passes the real archive-group preflight on
# any runner; the CI runner is not in pantheon-shared.
ARCHIVE_GROUP = os.environ.get("T6115_TEST_ARCHIVE_GROUP") or grp.getgrgid(os.getegid()).gr_name


@pytest.fixture
def fx(tmp_path):
    return Fixture(tmp_path)


def _snapshot(f: Fixture):
    return (f.thread_id(), dict(f.active), list(f.events), list(f.sent))


# ------------------------------------------------------------------ gate
def test_not_due_changes_nothing(tmp_path):
    f = Fixture(tmp_path, age_days=1, size=100)
    before = _snapshot(f)
    out = f.go()
    assert (out.status, out.exit_code) == ("not-due", 0)
    assert _snapshot(f) == before and not f.history()


def test_due_by_size(tmp_path):
    f = Fixture(tmp_path, age_days=1, size=2 * 1024 * 1024)
    assert f.go(max_mb=1.0).status == "rotated"


def test_due_by_age_uses_session_meta_not_mtime(tmp_path):
    f = Fixture(tmp_path, age_days=5)
    os.utime(f.rollout, (f.now, f.now))  # fresh mtime must not make an old thread young
    assert f.go().status == "rotated"


def test_age_falls_back_to_file_ctime_without_session_meta(tmp_path):
    f = Fixture(tmp_path, age_days=1)
    f.rollout.write_text("not json\n")
    f.now = f.rollout.stat().st_ctime + 5 * 86400
    assert f.go().status == "rotated"


def test_busy_pane_changes_nothing(fx):
    fx.busy_pane = True
    before = _snapshot(fx)
    out = fx.go()
    assert (out.status, out.exit_code) == ("busy", 0)
    assert _snapshot(fx) == before


@pytest.mark.parametrize("state", ["queued", "accepted"])
def test_in_flight_delivery_is_busy(fx, state):
    (fx.state / "deliveries.jsonl").write_text(json.dumps({"delivery_id": "d1", "state": state}) + "\n")
    assert fx.go().status == "busy"


def test_latest_state_per_delivery_id_wins(fx):
    lines = [{"delivery_id": "d1", "state": "queued"}, {"delivery_id": "d1", "state": "accepted"},
             {"delivery_id": "d1", "state": "delivered"}, {"event": "connected"}]
    (fx.state / "deliveries.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines) + "{torn")
    assert fx.go().status == "rotated"


def test_ambiguous_and_failed_are_terminal_not_busy(fx):
    lines = [{"delivery_id": "a", "state": "ambiguous"}, {"delivery_id": "b", "state": "failed"}]
    (fx.state / "deliveries.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    assert fx.go().status == "rotated"


@pytest.mark.parametrize("sub", ["spool", "pending"])
def test_spool_or_pending_entries_are_busy(fx, sub):
    (fx.state / sub).mkdir()
    (fx.state / sub / "m1.json").write_text("{}")
    assert fx.go().status == "busy"


def test_unreadable_delivery_log_is_busy(fx):
    (fx.state / "deliveries.jsonl").mkdir()  # reading a directory raises: fail closed
    assert fx.go().status == "busy"


def test_unreadable_spool_is_busy(fx):
    (fx.state / "spool").write_text("a file where a dir belongs")
    assert fx.go().status == "busy"


def test_no_thread_record_or_no_rollout_is_a_clean_noop(fx):
    fx.rollout.unlink()
    out = fx.go()
    assert (out.status, out.exit_code) == ("no-rollout", 0) and not fx.events
    (fx.state / "thread.json").unlink()
    assert fx.go().status == "no-thread"


def test_dry_run_reports_and_changes_nothing(fx):
    before = _snapshot(fx)
    out = fx.go(dry_run=True)
    assert (out.status, out.exit_code) == ("dry-run", 0) and "due" in out.detail
    assert _snapshot(fx) == before and not fx.history()



@pytest.mark.parametrize("dry_run", [True, False])
def test_unwritable_archive_group_stops_before_the_checkpoint(fx, dry_run):
    before = _snapshot(fx)
    out = fx.go(dry_run=dry_run, group_problem=lambda g: f"not in {g}")
    assert (out.status, out.exit_code) == ("archive-unwritable", 8) and f"not in {ARCHIVE_GROUP}" in out.detail
    assert _snapshot(fx) == before and not fx.history()


def test_archive_group_problem_real_checks(monkeypatch):
    import grp
    monkeypatch.setattr(rot.os, "geteuid", lambda: 1234)
    assert rot.archive_group_problem(None) is None
    assert "does not exist" in rot.archive_group_problem("no-such-group-t6115")
    mine = grp.getgrgid(os.getegid()).gr_name
    assert rot.archive_group_problem(mine) is None
    monkeypatch.setattr(rot.os, "getegid", lambda: -7)
    monkeypatch.setattr(rot.os, "getgroups", lambda: [])
    assert "EPERM" in rot.archive_group_problem(mine)


# ------------------------------------------------------------------ checkpoint
def test_checkpoint_no_ack_aborts_with_no_change(fx):
    fx.checkpoint_status = "no-ack"
    out = fx.go()
    assert (out.status, out.exit_code) == ("checkpoint-failed", 3)
    assert fx.events == ["checkpoint"] and fx.thread_id() == OLD_ID and not fx.history()


def test_checkpoint_fresh_proceeds(fx):
    fx.checkpoint_status = "fresh"
    assert fx.go().status == "rotated"


# ------------------------------------------------------------------ happy path
def test_happy_path_order_of_side_effects(fx):
    out = fx.go()
    assert (out.status, out.exit_code) == ("rotated", 0), out
    assert fx.events == [
        "checkpoint", "archive",
        f"stop:{WRITER}", f"stop:{DAEMON}",
        "session-end",
        f"start:{DAEMON}", "thread.json:absent",   # rebind happened: no record at daemon start
        f"start:{WRITER}",
        "seed",
    ]
    assert fx.thread_id() == NEW_ID
    assert fx.active == {WRITER: True, DAEMON: True}
    (hist,) = fx.history()
    doc = json.loads(hist.read_text())
    assert doc["record"] == fx.record and doc["old_thread_id"] == OLD_ID
    assert doc["rollout"] == str(fx.rollout) and doc["archive"]["sha256"] == "ab" * 32
    assert hist.name.endswith(f"-{OLD_ID}.json")
    assert fx.rollout.exists()  # the rollout is never deleted


def test_seed_mail_names_old_thread_and_archive(fx):
    fx.go()
    ((inbox, payload, _subject),) = fx.sent
    assert inbox == "/home/post-office/mailbox/codex"
    assert "Fresh thread after rotation (T-6115)" in payload and "~/HANDOFF.md" in payload
    assert OLD_ID in payload and f"/chron/codex/{OLD_ID}.gz" in payload


def test_writer_is_stopped_before_the_daemon_and_started_after_the_new_thread(fx):
    fx.go()
    e = fx.events
    assert e.index(f"stop:{WRITER}") < e.index(f"stop:{DAEMON}") < e.index(f"start:{DAEMON}") < e.index(f"start:{WRITER}")


def test_writer_that_will_not_start_after_rebind_restores_old_thread(fx):
    orig = fx.run_cmd

    def run(argv):
        if argv[2:] == ["start", WRITER] and not getattr(fx, "_w", False):
            fx._w = True
            fx.events.append(f"start:{WRITER}")
            return SimpleNamespace(returncode=1, stdout="", stderr="no")
        return orig(argv)

    out = rot.rotate(fx.cfg(), run_cmd=run, sleep=fx.sleep, clock=fx.clock, send=fx.send,
                     pane_busy=fx.pane_busy, checkpoint_fn=fx.checkpoint_fn, archive_fn=fx.archive_fn,
                     session_end_fn=fx.session_end_fn)
    assert (out.status, out.exit_code) == ("restored", 5)
    assert fx.thread_id() == OLD_ID and fx.active == {WRITER: True, DAEMON: True}


# ------------------------------------------------------------------ failure after stop
def test_daemon_start_failure_restores_binding_and_restarts_both_units(fx):
    fx.fail_start_daemon = True
    out = fx.go()
    assert (out.status, out.exit_code) == ("restored", 5)
    assert fx.thread_id() == OLD_ID and fx.record == json.loads((fx.state / "thread.json").read_text())
    assert fx.active == {WRITER: True, DAEMON: True}
    assert not fx.history()  # a restored rotation leaves no live history entry
    assert fx.rollout.exists() and not fx.sent
    tail = fx.events[fx.events.index(f"start:{DAEMON}"):]
    assert tail.count(f"start:{DAEMON}") == 2 and tail[-1] == f"start:{WRITER}"


def test_no_new_thread_id_in_time_restores(fx):
    fx.no_new_thread = True
    out = fx.go()
    assert (out.status, out.exit_code) == ("restored", 5)
    assert fx.thread_id() == OLD_ID and fx.active == {WRITER: True, DAEMON: True}
    assert fx.now - NOW.timestamp() >= rot.NEW_THREAD_TIMEOUT_S
    assert f"stop:{DAEMON}" in fx.events[fx.events.index(f"start:{DAEMON}"):]  # half-started daemon stopped first


def test_session_end_failure_after_stop_restarts_without_rebind(fx):
    fx.fail_session_end = True
    out = fx.go()
    assert (out.status, out.exit_code) == ("restored", 5)
    assert fx.thread_id() == OLD_ID and fx.active == {WRITER: True, DAEMON: True} and not fx.history()


def test_stop_that_does_not_take_effect_aborts_and_restores(fx):
    orig = fx.run_cmd

    def run(argv):
        if argv[2:] == ["stop", DAEMON]:
            fx.events.append(f"stop:{DAEMON}")
            return SimpleNamespace(returncode=0, stdout="", stderr="")  # but stays active
        return orig(argv)

    out = rot.rotate(fx.cfg(), run_cmd=run, sleep=fx.sleep, clock=fx.clock, send=fx.send, pane_busy=fx.pane_busy,
                     checkpoint_fn=fx.checkpoint_fn, archive_fn=fx.archive_fn, session_end_fn=fx.session_end_fn)
    assert out.exit_code == 5 and "session-end" not in fx.events and fx.thread_id() == OLD_ID


def test_restore_that_itself_fails_is_exit_6(fx):
    fx.fail_start_daemon = True
    orig = fx.run_cmd
    calls = {"n": 0}

    def run(argv):
        if argv[2:] == ["start", DAEMON]:
            calls["n"] += 1
            if calls["n"] >= 2:
                return SimpleNamespace(returncode=1, stdout="", stderr="still down")
        return orig(argv)

    out = rot.rotate(fx.cfg(), run_cmd=run, sleep=fx.sleep, clock=fx.clock, send=fx.send, pane_busy=fx.pane_busy,
                     checkpoint_fn=fx.checkpoint_fn, archive_fn=fx.archive_fn, session_end_fn=fx.session_end_fn)
    assert out.exit_code == 6 and fx.thread_id() == OLD_ID  # binding is back even though the unit is not


def test_seed_failure_after_success_is_loud_but_not_a_restore(fx):
    def boom(*a):
        raise OSError("mailbox down")

    fx.send = boom
    out = fx.go()
    assert (out.status, out.exit_code) == ("rotated-unseeded", 7)
    assert fx.thread_id() == NEW_ID


# ------------------------------------------------------------------ rollback
def test_rollback_restores_newest_history_and_restarts(fx):
    fx.go()
    assert fx.thread_id() == NEW_ID
    fx.events.clear()
    out = fx.go(rollback=True)
    assert (out.status, out.exit_code) == ("rolled-back", 0), out
    assert fx.thread_id() == OLD_ID and json.loads((fx.state / "thread.json").read_text()) == fx.record
    assert fx.active == {WRITER: True, DAEMON: True}
    assert fx.events == [f"stop:{WRITER}", f"stop:{DAEMON}", f"start:{DAEMON}", "thread.json:present",
                         f"start:{WRITER}"]
    assert not fx.history() and fx.rollout.exists()
    assert list((fx.state / "thread-history").glob("*.superseded"))  # the rotated-to thread is not lost


def test_rollback_without_history_fails_cleanly(fx):
    out = fx.go(rollback=True)
    assert out.exit_code != 0 and not fx.events and fx.thread_id() == OLD_ID


# ------------------------------------------------------------------ watchman
def test_watchman_live_rotation_is_refused_without_side_effects(tmp_path):
    f = Fixture(tmp_path, persona="watchman")
    out = f.go()
    assert (out.status, out.exit_code) == ("refused", 4)
    assert "watchman live rotation pending T-6113" in out.detail
    assert not f.events and f.thread_id() == OLD_ID
    assert f.go(rollback=True).exit_code == 4


def test_watchman_dry_run_gates(tmp_path):
    f = Fixture(tmp_path, persona="watchman")
    out = f.go(dry_run=True)
    assert (out.status, out.exit_code) == ("dry-run", 0) and not f.events
    f2 = Fixture(tmp_path / "b", persona="watchman", age_days=1)
    assert f2.go(dry_run=True).status == "not-due"


# ------------------------------------------------------------------ B: real chronicle
def _live_log(f: Fixture, stamp="2026-11-01 08:00:05"):
    p = f.logs / "pane-timed-codex.log"
    p.write_text(f"[{stamp}] old session\n")
    return p


def _runtime(f: Fixture, started):
    p = f.pstate / "codex/runtime-controller/runtime.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"persona": "codex", "harness": "codex", "started_at": started}))


def _reconcile(f: Fixture, now):
    req = sc.SessionEndRequest(persona="codex", cause="start-reconcile", account_home=f.home,
                               handoff_file=f.home / "HANDOFF.md", stage=True, certain=True,
                               state_root=f.pstate, chronicle_root=f.chron, log_dir=f.logs)
    return sc.session_end(req, now=now, route_fn=lambda *a: None)


def _real(f: Fixture, **kw):
    return rot.rotate(
        f.cfg(archive_group=None, **kw), run_cmd=f.run_cmd, sleep=f.sleep, clock=f.clock, send=f.send, pane_busy=f.pane_busy,
        checkpoint_fn=f.checkpoint_fn)


def test_real_rotation_files_one_record_with_the_archived_transcript(fx):
    _runtime(fx, RT_STARTED)
    _live_log(fx)
    out = _real(fx)
    assert out.status == "rotated", out
    (entry,) = chronicle.entries("codex", root=fx.chron)
    meta = json.loads(next((fx.chron / "codex").glob("*/*.session.json")).read_text())
    assert meta["cause"] == "thread-rotation"
    assert meta["native_transcript"]["source_bytes"] == fx.rollout.stat().st_size
    assert Path(meta["native_transcript"]["path"]).exists()
    assert entry["uuid"] == OLD_ID
    assert fx.rollout.exists()


def test_start_reconcile_after_rotation_does_not_double_record(fx):
    _runtime(fx, RT_STARTED)
    _live_log(fx)
    assert _real(fx).status == "rotated"
    # the supervisor's first start_pane runs this when the daemon comes up (no live log yet)
    res = _reconcile(fx, NOW + timedelta(minutes=2))
    assert [r["status"] for r in res["results"]] == ["already-archived"], res
    assert len(chronicle.entries("codex", root=fx.chron)) == 1


def _pin_capture(fx):
    seen = {}

    def fake_end(req, now=None, **kw):
        seen["req"] = req
        return {"status": "ok", "results": []}
    return seen, fake_end


def test_rt_pin_is_a_fallback_used_when_runtime_json_lacks_started_at(fx):
    """A seat whose runtime.json is absent (or {}) gets a unique per-rotation identity."""
    seen, fake_end = _pin_capture(fx)
    pointer = {"path": "/x"}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sc, "session_end", fake_end)
        rot.default_session_end(fx.cfg(), pointer, OLD_ID, NOW)      # runtime.json absent
        first = seen["req"]
        (fx.pstate / "codex/runtime-controller").mkdir(parents=True)
        (fx.pstate / "codex/runtime-controller/runtime.json").write_text("{}")
        rot.default_session_end(fx.cfg(), pointer, OLD_ID, NOW + timedelta(minutes=1))
        second = seen["req"]
    for req in (first, second):
        assert req.rt_pinned and req.rt_started_at.startswith(f"thread-rotation:{OLD_ID}:")
    assert first.rt_started_at != second.rt_started_at


def test_rt_pin_is_not_used_when_runtime_json_has_started_at(fx):
    """A bare supervisor's own runtime.json (T-6118 PR-3) keys the rotation, so start-reconcile dedupes."""
    seen, fake_end = _pin_capture(fx)
    _runtime(fx, RT_STARTED)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sc, "session_end", fake_end)
        rot.default_session_end(fx.cfg(), {"path": "/x"}, OLD_ID, NOW)
    assert not seen["req"].rt_pinned and seen["req"].rt_started_at is None


def test_restore_then_a_later_real_session_end_is_still_archived(fx):
    _runtime(fx, RT_STARTED)
    _live_log(fx)
    fx.fail_start_daemon = True
    out = _real(fx)
    assert out.status == "restored", out
    assert len(chronicle.entries("codex", root=fx.chron)) == 1  # the rotation's own record
    # the restored daemon is a new runtime: the controller rewrites started_at on that launch
    _runtime(fx, "2026-11-03T12:05:00.000000Z")
    _live_log(fx, stamp="2026-11-03 12:05:09")
    later = sc.session_end(sc.SessionEndRequest(
        persona="codex", cause="death", account_home=fx.home, handoff_file=fx.home / "HANDOFF.md",
        stage=True, state_root=fx.pstate, chronicle_root=fx.chron, log_dir=fx.logs),
        now=NOW + timedelta(hours=1), route_fn=lambda *a: None)
    assert [r["status"] for r in later["results"]] == ["archived"], later
    assert len(chronicle.entries("codex", root=fx.chron)) == 2


def test_archive_and_record_share_one_stem(fx):
    _runtime(fx, RT_STARTED)
    _live_log(fx)
    assert _real(fx).status == "rotated"
    (entry,) = chronicle.entries("codex", root=fx.chron)
    meta = json.loads(next((fx.chron / "codex").glob("*/*.session.json")).read_text())
    assert Path(meta["native_transcript"]["path"]).name.startswith(
        chronicle._fs_safe_ts(entry["ts"]) + "-" + entry["uuid"])




# ------------------------------------------------------------------ review rebound
def _bus_failure(argv):
    return SimpleNamespace(returncode=1, stdout="", stderr="Failed to connect to bus: No medium found")


@pytest.mark.parametrize("out,ok", [("inactive\n", True), ("failed\n", True), ("", False),
                                    ("active\n", False), ("activating\n", False)])
def test_stop_requires_inactive_or_failed_on_stdout(out, ok):
    def run(argv):
        if argv[2] == "is-active":
            return SimpleNamespace(returncode=3 if ok else 1, stdout=out, stderr="Failed to connect to bus")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    units = rot.Units(run)
    if ok:
        units.stop(DAEMON)
    else:
        with pytest.raises(rot.RotateFail):
            units.stop(DAEMON)


def test_bus_failure_never_counts_as_stopped_and_flow_does_not_proceed(fx):
    out = rot.rotate(fx.cfg(), run_cmd=_bus_failure, sleep=fx.sleep, clock=fx.clock, send=fx.send,
                     pane_busy=fx.pane_busy, checkpoint_fn=fx.checkpoint_fn, archive_fn=fx.archive_fn,
                     session_end_fn=fx.session_end_fn)
    assert out.exit_code in (5, 6)
    assert "session-end" not in fx.events and fx.thread_id() == OLD_ID and not fx.history()


def test_start_requires_active_on_stdout():
    def run(argv):
        return SimpleNamespace(returncode=0, stdout="" if argv[2] == "is-active" else "", stderr="bus")

    with pytest.raises(rot.RotateFail):
        rot.Units(run).start(DAEMON)


def test_default_run_cmd_sets_the_user_bus_environment(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return SimpleNamespace(returncode=0, stdout="active\n", stderr="")

    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(rot.subprocess, "run", fake_run)
    rot.default_run_cmd(["systemctl", "--user", "is-active", DAEMON])
    uid = os.geteuid()
    assert seen["env"]["XDG_RUNTIME_DIR"] == f"/run/user/{uid}"
    assert seen["env"]["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path=/run/user/{uid}/bus"
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/custom")
    rot.default_run_cmd(["x"])
    assert seen["env"]["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/custom"


# 2: idle re-check after the checkpoint and right before the stop
def test_seat_gone_busy_during_checkpoint_exits_busy_with_no_change(fx):
    def cp(**kw):
        fx.events.append("checkpoint")
        fx.busy_pane = True
        return chk.Outcome("written", "t", 0)

    out = rot.rotate(fx.cfg(), run_cmd=fx.run_cmd, sleep=fx.sleep, clock=fx.clock, send=fx.send,
                     pane_busy=fx.pane_busy, checkpoint_fn=cp, archive_fn=fx.archive_fn,
                     session_end_fn=fx.session_end_fn)
    assert (out.status, out.exit_code) == ("busy", 0)
    assert fx.events == ["checkpoint"] and fx.thread_id() == OLD_ID and not fx.history()


def test_mail_arriving_during_archive_exits_busy_before_any_stop(fx):
    orig = fx.archive_fn

    def archive(cfg, tid, rollout, ts):
        (fx.state / "spool").mkdir(exist_ok=True)
        (fx.state / "spool" / "m.json").write_text("{}")
        return orig(cfg, tid, rollout, ts)

    out = rot.rotate(fx.cfg(), run_cmd=fx.run_cmd, sleep=fx.sleep, clock=fx.clock, send=fx.send,
                     pane_busy=fx.pane_busy, checkpoint_fn=fx.checkpoint_fn, archive_fn=archive,
                     session_end_fn=fx.session_end_fn)
    assert (out.status, out.exit_code) == ("busy", 0)
    assert not any(e.startswith("stop:") for e in fx.events) and fx.active == {WRITER: True, DAEMON: True}


# 3: only archived/paired with a record counts
@pytest.mark.parametrize("res", [{"status": "already-archived"}, {"status": "busy"}, {"status": "unpinned-refused"},
                                 {"status": "pane-live-skip"}, {"status": "no-session"}, {"status": "failed"},
                                 {"status": "archived"}])
def test_session_end_must_actually_archive_or_the_rotation_restores(fx, res):
    fx.session_end_fn = lambda cfg, pointer, tid, when=None: {"status": "ok", "results": [res]}
    out = fx.go()
    assert (out.status, out.exit_code) == ("restored", 5), out
    assert fx.thread_id() == OLD_ID and fx.active == {WRITER: True, DAEMON: True} and not fx.history()


def test_session_end_with_no_results_restores(fx):
    fx.session_end_fn = lambda cfg, pointer, tid, when=None: {"status": "ok", "results": []}
    assert fx.go().exit_code == 5


def test_paired_with_record_counts_as_success(fx):
    fx.session_end_fn = lambda cfg, pointer, tid, when=None: {"status": "ok", "results": [
        {"status": "paired", "record": "/r"}]}
    assert fx.go().status == "rotated"


def test_prior_none_claim_does_not_block_rotation_on_a_seat_with_empty_runtime_json(fx):
    # consultant's runtime.json is {}: rt_started is None. An older done claim with rt_started_at None
    # used to match (None == None) and make session-end answer already-archived.
    (fx.pstate / "codex/runtime-controller").mkdir(parents=True)
    (fx.pstate / "codex/runtime-controller/runtime.json").write_text("{}")
    claims = fx.chron / "codex" / ".claims"
    claims.mkdir(parents=True)
    (claims / "old.json").write_text(json.dumps({"state": "done", "key": "old", "persona": "codex",
                                                 "rt_started_at": None, "at": "2026-10-01T00:00:00Z"}))
    _live_log(fx)
    out = _real(fx)
    assert out.status == "rotated", out
    meta = json.loads(next((fx.chron / "codex").glob("*/*.session.json")).read_text())
    assert meta["cause"] == "thread-rotation" and meta["native_transcript"]["path"]


def test_prior_done_claim_for_the_same_runtime_restores_instead_of_proceeding(fx):
    _runtime(fx, RT_STARTED)
    _live_log(fx)
    assert _real(fx).status == "rotated"
    # same runtime, rotated again (e.g. stale runtime.json): the second session-end answers already-archived
    fx.active = {WRITER: True, DAEMON: True}
    (fx.state / "thread.json").write_text(json.dumps(fx.record))
    _live_log(fx, stamp="2026-11-01 08:00:05")
    out = _real(fx)
    assert out.exit_code == 5 and fx.thread_id() == OLD_ID


# 4: rollback failure paths restart both units
def _rotated(fx):
    assert fx.go().status == "rotated"
    fx.events.clear()


def test_rollback_failure_restarts_both_units_best_effort(fx):
    _rotated(fx)
    orig = fx.run_cmd
    n = {"w": 0}

    def run(argv):
        if argv[2:] == ["start", WRITER]:
            n["w"] += 1
            if n["w"] == 1:
                fx.events.append(f"start:{WRITER}")
                return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        return orig(argv)

    out = rot.rotate(fx.cfg(rollback=True), run_cmd=run, sleep=fx.sleep, clock=fx.clock, send=fx.send,
                     pane_busy=fx.pane_busy)
    assert (out.status, out.exit_code) == ("rollback-failed", 5), out
    assert fx.active == {WRITER: True, DAEMON: True}
    assert fx.events[-2:] == [f"start:{DAEMON}", f"start:{WRITER}"] or fx.events[-1] == f"start:{WRITER}"


def test_rollback_failure_is_exit_6_only_if_the_restart_also_fails(fx):
    _rotated(fx)
    orig = fx.run_cmd

    def run(argv):
        if argv[2] == "start":
            return SimpleNamespace(returncode=1, stdout="", stderr="down")
        return orig(argv)

    out = rot.rotate(fx.cfg(rollback=True), run_cmd=run, sleep=fx.sleep, clock=fx.clock, send=fx.send,
                     pane_busy=fx.pane_busy)
    assert out.exit_code == 6


def test_rollback_stop_that_does_not_stop_still_restarts(fx):
    _rotated(fx)
    orig = fx.run_cmd

    def run(argv):
        if argv[2:] == ["stop", DAEMON]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return orig(argv)

    out = rot.rotate(fx.cfg(rollback=True), run_cmd=run, sleep=fx.sleep, clock=fx.clock, send=fx.send,
                     pane_busy=fx.pane_busy)
    assert out.exit_code == 5 and fx.active == {WRITER: True, DAEMON: True}
    assert len(fx.history()) == 1  # the entry is still there to retry


# 5a: interruption after the stop
@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
def test_interrupt_after_stop_restores_then_reraises(fx, exc):
    def boom(cfg, pointer, tid, when=None):
        raise exc(143)

    fx.session_end_fn = boom
    with pytest.raises(exc):
        fx.go()
    assert fx.thread_id() == OLD_ID and fx.active == {WRITER: True, DAEMON: True} and not fx.history()


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
def test_interrupt_with_failed_restore_reraises_the_interrupt_not_the_restore_error(fx, monkeypatch, exc):
    def boom(cfg, pointer, tid, when=None):
        raise exc(143)

    def bad_restore(*a, **k):
        raise rot.RotateFail("restore broke")

    fx.session_end_fn = boom
    monkeypatch.setattr(rot, "_restore", bad_restore)
    with pytest.raises(exc):
        fx.go()


def test_sigterm_is_turned_into_an_exception_by_main(monkeypatch):
    import signal
    seen = {}
    monkeypatch.setattr(rot.signal, "signal", lambda sig, h: seen.update({sig: h}))
    monkeypatch.setattr(rot, "rotate", lambda cfg, **kw: rot.Result("not-due", 0, ""))
    monkeypatch.setattr(chk, "current_account", lambda: "codex")
    assert rot.main(["--persona", "codex", "--dry-run"]) == 0
    with pytest.raises(SystemExit):
        seen[signal.SIGTERM](signal.SIGTERM, None)


def test_main_derives_the_persona_from_the_account(monkeypatch):
    got = {}
    monkeypatch.setattr(rot.signal, "signal", lambda sig, h: None)
    monkeypatch.setattr(rot, "rotate", lambda cfg, **kw: got.update(p=cfg.persona) or rot.Result("not-due", 0, ""))
    monkeypatch.setattr(chk, "current_account", lambda: "codex-fallback")
    assert rot.main(["--dry-run"]) == 0
    assert got["p"] == "watchman"


def test_main_refuses_a_persona_that_disagrees_with_the_account(monkeypatch):
    monkeypatch.setattr(rot.signal, "signal", lambda sig, h: None)
    monkeypatch.setattr(chk, "current_account", lambda: "codex")
    with pytest.raises(SystemExit) as e:
        rot.main(["--persona", "watchman", "--dry-run"])
    assert e.value.code != 0


# 5b: a failed unlink must not leave a history entry rollback could pick up
def test_failed_unlink_in_rebind_removes_the_history_entry(fx, monkeypatch):
    real = os.remove

    def remove(path, *a, **k):
        if Path(path).name == "thread.json":
            raise OSError("EPERM")
        return real(path, *a, **k)

    monkeypatch.setattr(rot.os, "remove", remove)
    out = fx.go()
    assert (out.status, out.exit_code) == ("restored", 5)
    assert fx.thread_id() == OLD_ID and not fx.history()
    assert not list((fx.state / "thread-history").glob("*"))
    assert fx.go(rollback=True).exit_code == 1  # nothing for rollback to pick up
