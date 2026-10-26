"""Tests for tools/session_chronicle.py, the session-end Chronicle duty (T-5016).

Every path is a tmp_path stand-in: chronicle root, persona state root, pane-log
dir, account home and HANDOFF. No real seat, state dir, hub or tmux server is
touched (the tmux tests run on conftest's private TMUX_TMPDIR server)."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools import chronicle
from tools import fleet_health
from tools import session_chronicle as sc
from tools.harness_adapter import integration as harness_integration

REPO = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 14, 23, 0, 0, tzinfo=timezone.utc)
STARTED = "2026-10-14T20:00:00.123456Z"


class Seat:
    """One seat's worth of tmp stand-ins."""

    def __init__(self, tmp: Path, persona: str = "specialist", harness: str = "claude") -> None:
        self.persona = persona
        self.harness = harness
        self.root = tmp / "chronicle"
        self.state = tmp / "personas"
        self.logs = tmp / "logs"
        self.home = tmp / "home"
        for d in (self.root, self.state, self.logs, self.home):
            d.mkdir(parents=True, exist_ok=True)
        self.handoff = self.home / "HANDOFF.md"
        self.agent = chronicle.agent_dir_name(persona)
        self.alerts: list[tuple[str, str, str]] = []

    def runtime(self, started: str | None = STARTED, harness: str | None = None) -> None:
        path = self.state / self.persona / "runtime-controller" / "runtime.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"persona": self.persona, "harness": harness or self.harness, "profile": "p",
                   "model": "m", "tmux_target": "seat:harness.0"}
        if started is not None:
            payload["started_at"] = started
        path.write_text(json.dumps(payload))

    def write_handoff(self, text: str = "# Handoff\nnext: ship T-5016\n", mtime: datetime | None = None) -> None:
        self.handoff.write_text(text)
        stamp = (mtime or NOW - timedelta(minutes=5)).timestamp()
        os.utime(self.handoff, (stamp, stamp))

    def live_log(self, body: str = "hello", stamp: str = "2026-10-14 20:00:01") -> Path:
        path = self.logs / f"pane-timed-{self.persona}.log"
        path.write_text(f"[{stamp}] {body}\n[{stamp}] password=hunter2\n")
        return path

    def request(self, cause: str = "death", **kw) -> sc.SessionEndRequest:
        base = dict(persona=self.persona, cause=cause, account_home=self.home, handoff_file=self.handoff,
                    harness=None, stage=True, state_root=self.state, chronicle_root=self.root,
                    log_dir=self.logs)
        base.update(kw)
        return sc.SessionEndRequest(**base)

    def run(self, cause: str = "death", now: datetime = NOW, **kw) -> dict:
        return sc.session_end(self.request(cause, **kw), now=now,
                              route_fn=lambda *a: self.alerts.append(a))

    def index(self) -> list[dict]:
        return chronicle.entries(self.agent, root=self.root)

    def files(self, suffix: str) -> list[Path]:
        return sorted((self.root / self.agent).glob(f"*/*{suffix}"))

    def meta(self) -> dict:
        (path,) = self.files(".session.json")
        return json.loads(path.read_text())


@pytest.fixture
def seat(tmp_path):
    return Seat(tmp_path)


# ------------------------------------------------------------------ per harness
def _transcripts(seat: Seat) -> dict[str, tuple[Path, str]]:
    claude = seat.home / ".claude" / "projects" / "-home-x-pantheon-config" / "1111-claude.jsonl"
    codex = seat.home / ".codex" / "sessions" / "2026" / "10" / "14" / "rollout-2026-10-14T20-00-02-2222-codex.jsonl"
    hermes = seat.home / ".hermes" / "sessions" / "3333-hermes.json"
    for path in (claude, codex, hermes):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"turn": 1}\n')
    return {"claude": (claude, "1111-claude"), "codex": (codex, "2222-codex"), "hermes": (hermes, "3333-hermes")}


@pytest.mark.parametrize("harness", ["claude", "codex", "hermes"])
def test_each_harness_archives_one_full_record(tmp_path, harness):
    seat = Seat(tmp_path, harness=harness)
    expected_path, expected_id = _transcripts(seat)[harness]
    seat.runtime()
    seat.write_handoff()
    seat.live_log("last words")

    summary = seat.run("death")

    assert summary["status"] == "ok", summary
    (entry,) = seat.index()
    assert entry["reason"] == "session-end/death"
    assert entry["uuid"] == expected_id
    assert entry["summary"] == "Handoff"
    (snapshot,) = seat.files(".md")
    assert snapshot.read_text() == seat.handoff.read_text()
    (gz,) = seat.files(".pane.log.gz")
    body = gzip.open(gz, "rt").read()
    assert "last words" in body and "hunter2" not in body  # chronicle's credential scrub
    assert gz.name.startswith(snapshot.name[: -len(".md")])  # self-paired stem
    meta = seat.meta()
    assert meta["harness"] == harness
    assert meta["handoff_fresh"] is True
    assert meta["native_transcript"]["path"] == str(expected_path)
    assert meta["native_transcript"]["session_id"] == expected_id
    assert meta["pane_log"] == "archived"
    assert not (seat.logs / f"pane-timed-{seat.persona}.log").exists()
    assert seat.alerts == []


def test_harness_without_transcript_still_archives(seat):
    seat.runtime(harness="hermes")
    seat.live_log()
    summary = seat.run("death")
    assert summary["status"] == "ok"
    assert seat.meta()["native_transcript"] is None
    assert seat.index()[0]["uuid"].startswith("rt-")


def test_transcript_resolver_failure_is_not_an_archive_failure(seat, monkeypatch):
    class Broken:
        name = "claude"

        def session_transcript(self, **_):
            raise RuntimeError("boom")

    seat.runtime()
    seat.live_log()
    with harness_integration.registered_integration(Broken()):
        summary = seat.run("death")
    assert summary["status"] == "ok"
    assert seat.meta()["native_transcript"] is None


def test_integration_registry_resolves_each_harness(tmp_path):
    seat = Seat(tmp_path)
    expected = _transcripts(seat)
    for name, (path, session_id) in expected.items():
        found = harness_integration.integration_for(name).session_transcript(
            account_home=seat.home, started_at=None)
        assert (found.path, found.session_id) == (path, session_id)
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert harness_integration.integration_for("codex").session_transcript(
        account_home=seat.home, started_at=later) is None


# ------------------------------------------------------------------ idempotency
def test_stop_then_death_detection_archives_once(seat):
    seat.runtime()
    seat.live_log()
    first = seat.run("death")
    second = seat.run("start", now=NOW + timedelta(seconds=5))  # H1 at the next start
    third = seat.run("death", now=NOW + timedelta(seconds=9))
    assert first["results"][0]["status"] == "archived"
    assert second["results"][0]["status"] == "already-archived"
    assert third["results"][0]["status"] == "already-archived"
    assert len(seat.index()) == 1
    assert len(seat.files(".session.json")) == 1


def test_new_runtime_record_is_a_new_session(seat):
    seat.runtime()
    seat.live_log(stamp="2026-10-14 20:00:01")
    seat.run("death")
    seat.runtime(started="2026-10-14T22:00:00Z")
    seat.live_log(stamp="2026-10-14 22:00:01")
    seat.run("death", now=NOW + timedelta(minutes=1))
    assert [e["reason"] for e in seat.index()] == ["session-end/death", "session-end/death"]


def test_stale_runtime_record_with_a_new_pane_log_is_a_new_session(seat):
    # artisan: seat-relaunch respawns never rewrite runtime.json, so the pane log's
    # first stamp is what tells two sessions apart.
    seat.runtime()
    seat.live_log(stamp="2026-10-14 20:00:01")
    seat.run("ctxfill-relaunch", certain=True)
    seat.live_log(stamp="2026-10-14 21:00:01")
    seat.run("ctxfill-relaunch", certain=True, now=NOW + timedelta(minutes=1))
    assert len(seat.index()) == 2


def test_claim_is_exclusive_and_reclaims_failed_or_stale(tmp_path):
    claim = tmp_path / "k.json"
    assert sc._try_claim(claim, NOW)[0] == "claimed"
    assert sc._try_claim(claim, NOW + timedelta(seconds=10))[0] == "busy"
    assert sc._try_claim(claim, NOW + timedelta(seconds=sc.CLAIM_STALE_SECONDS + 1))[0] == "claimed"
    claim.write_text(json.dumps({"state": "failed", "at": sc._iso(NOW)}))
    assert sc._try_claim(claim, NOW)[0] == "claimed"
    claim.write_text(json.dumps({"state": "done", "pane_log": "archived"}))
    assert sc._try_claim(claim, NOW)[0] == "done"
    claim.write_text(json.dumps({"state": "done", "pane_log": "pending"}))
    assert sc._try_claim(claim, NOW)[0] == "pending"


def test_a_fresh_empty_claim_is_busy_not_stale(tmp_path):
    """T-5037: a claim file that exists but has no content yet is a live claim
    mid-write, not a stale one. Before the fix, a racer read it as {} (no state,
    no time), judged it stale, moved it aside and claimed too, so the session was
    archived twice."""
    claim = tmp_path / "k.json"
    claim.write_text("")
    assert sc._try_claim(claim, NOW)[0] == "busy"
    assert claim.read_text() == "", "a busy caller must not touch the live claim"


def test_an_old_empty_claim_is_reclaimed_with_a_full_payload(tmp_path):
    claim = tmp_path / "k.json"
    claim.write_text("")
    old = time.time() - sc.CLAIM_STALE_SECONDS - 60
    os.utime(claim, (old, old))
    assert sc._try_claim(claim, NOW)[0] == "claimed"
    doc = json.loads(claim.read_text())
    assert doc["state"] == "claimed" and doc["pid"] == os.getpid()


def test_a_racer_during_a_slow_claim_write_does_not_also_claim(tmp_path, monkeypatch):
    """T-5037, the CI two-record mode, made deterministic. Writer 1 stalls while
    serialising its claim; writer 2 claims during the stall. Before the fix,
    writer 1 had already created an EMPTY claim (O_EXCL), so writer 2 read {} as
    stale, moved it aside and claimed too: two owners, two records. Now nothing
    exists at the path until a full claim is linked in, so exactly one claims."""
    import threading

    claim = tmp_path / "k.json"
    in_write = threading.Event()
    real_dumps = sc.json.dumps
    calls = {"n": 0}

    def slow_first_dumps(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            in_write.set()
            time.sleep(0.5)
        return real_dumps(*args, **kwargs)

    monkeypatch.setattr(sc.json, "dumps", slow_first_dumps)
    results: list[str] = []
    writer1 = threading.Thread(target=lambda: results.append(sc._try_claim(claim, NOW)[0]))
    writer1.start()
    assert in_write.wait(5), "writer 1 never reached its claim write"
    results.append(sc._try_claim(claim, NOW)[0])
    writer1.join(10)
    assert sorted(results) == ["busy", "claimed"], results


def test_a_claim_is_created_whole_and_leaves_no_temp_file(tmp_path):
    claim = tmp_path / "k.json"
    assert sc._try_claim(claim, NOW)[0] == "claimed"
    assert json.loads(claim.read_text())["state"] == "claimed"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "k.json"]
    assert leftovers == [], leftovers


def test_a_writer_without_a_pane_log_defers_to_one_still_archiving_the_session(seat):
    """T-5037, the CI two-record mode. The legacy writer claims the session under
    its pane-log key and moves the staged log; the controller then finds no log,
    keys the same session on the runtime record alone (a different claim file)
    and, checking only DONE claims, filed a second record. A fresh 'claimed'
    claim for the same runtime session must now read as busy."""
    seat.runtime()
    seat.write_handoff()
    claims = seat.root / seat.agent / sc.CLAIMS_DIRNAME
    claims.mkdir(parents=True, exist_ok=True)
    (claims / "legacy-pane-key.json").write_text(json.dumps({
        "state": "claimed", "key": "legacy-pane-key", "persona": seat.persona,
        "rt_started_at": STARTED, "pane_first_stamp": "2026-10-14 20:00:01",
        "cause": "graceful-restart", "pid": 4242, "at": sc._iso(NOW),
    }))
    out = seat.run("death", now=NOW + timedelta(seconds=5))
    statuses = [r["status"] for r in out["results"]]
    assert statuses == ["busy"], out
    assert seat.index() == [], "no second record while the first writer is archiving"


def test_two_concurrent_cli_writers_produce_one_record(seat):
    seat.runtime()
    seat.write_handoff()
    log = seat.live_log()
    staged = log.with_name(log.name + ".dying")
    log.rename(staged)
    env = dict(os.environ, PANTHEON_CHRONICLE_ROOT=str(seat.root), PANE_LOG_DIR=str(seat.logs),
               PANTHEON_PERSONA_STATE_DIR=str(seat.state), PANTHEON_SESSION_CHRONICLE_ALERT="off")
    argv = ["python3", str(REPO / "tools" / "session_chronicle.py"), "session-end", "--persona", seat.persona,
            "--account-home", str(seat.home), "--handoff-file", str(seat.handoff)]
    legacy = subprocess.Popen(argv + ["--cause", "graceful-restart", "--source", str(staged), "--certain"],
                              env=env, stdout=subprocess.PIPE, text=True)
    controller = subprocess.Popen(argv + ["--cause", "start", "--stage"], env=env, stdout=subprocess.PIPE, text=True)
    outs = [json.loads(p.communicate(timeout=60)[0].strip().splitlines()[-1]) for p in (legacy, controller)]
    statuses = sorted(r["status"] for o in outs for r in o["results"])
    assert len(seat.index()) == 1, (outs, seat.index())
    assert statuses.count("archived") == 1, statuses
    # T-5037: "unpinned-refused" is the legacy writer's designed safe refusal
    # when the controller has already archived the session and moved the staged
    # pane log: its --source is gone and, being --certain without --stage, it
    # files nothing rather than risk the successor session. One record either way.
    allowed = ("archived", "already-archived", "busy", "unpinned-refused")
    assert all(s in allowed for s in statuses), statuses


# ------------------------------------------------------------------ pairing, deferral, orphans
def test_pairs_with_a_planned_swap_archive_instead_of_a_second_line(seat):
    seat.runtime()
    seat.live_log("swap session")
    swap = chronicle.archive_required(seat.agent, "model handoff", jsonl_uuid="msg-1",
                                      reason="planned-runtime-swap", ts="2026-10-14T21:00:00Z", root=seat.root)
    summary = seat.run("start")
    assert summary["results"][0]["status"] == "paired"
    assert [e["reason"] for e in seat.index()] == ["planned-runtime-swap"]
    stem = swap.name[: -len(".md")]
    assert (swap.parent / f"{stem}.pane.log.gz").exists()
    assert (swap.parent / f"{stem}.session.json").exists()
    assert swap.read_text() == "model handoff"


def test_fleet_reboot_defers_the_pane_log_then_the_next_start_attaches_it(seat):
    seat.runtime()
    seat.write_handoff()
    seat.live_log("before reboot")
    paused = seat.run("fleet-reboot", stage=False, defer_pane_log=True, certain=True, handoff_fresh=True)
    assert paused["results"][0]["status"] == "archived"
    assert seat.files(".pane.log.gz") == []
    assert seat.meta()["pane_log"] == "pending"
    after = seat.run("start", now=NOW + timedelta(minutes=3))
    assert after["results"][0]["status"] == "attached"
    assert len(seat.index()) == 1
    (gz,) = seat.files(".pane.log.gz")
    assert "before reboot" in gzip.open(gz, "rt").read()
    again = seat.run("start", now=NOW + timedelta(minutes=4))
    assert again["results"][0]["status"] in ("no-session", "already-archived")
    assert len(seat.index()) == 1


def test_orphan_staged_log_is_archived_as_its_own_session(seat):
    seat.runtime()
    orphan = seat.logs / f"pane-timed-{seat.persona}.log.dying"
    orphan.write_text("[2026-10-13 10:00:00] an older session\n")
    seat.live_log("current", stamp="2026-10-14 20:00:01")
    summary = seat.run("death")
    causes = sorted(r["cause"] for r in summary["results"])
    assert causes == ["death", "orphan-staged"]
    assert len(seat.index()) == 2


def test_no_identity_is_a_noop_unless_the_caller_is_certain(seat):
    assert seat.run("death")["results"][0]["status"] == "no-session"
    assert seat.index() == []
    certain = seat.run("forced-restart", certain=True, handoff_fresh=False)
    assert certain["results"][0]["status"] == "archived"
    assert seat.meta()["handoff_fresh"] is False
    assert seat.index()[0]["reason"] == "session-end/forced-restart"


def test_missing_handoff_records_a_stub(seat):
    seat.runtime()
    seat.live_log()
    seat.run("death")
    (snapshot,) = seat.files(".md")
    assert snapshot.read_text().startswith("# (no HANDOFF at session end")
    assert seat.meta()["handoff_fresh"] is False


def test_stale_handoff_is_marked_not_fresh(seat):
    seat.runtime()
    seat.write_handoff(mtime=datetime(2026, 10, 14, 19, 0, tzinfo=timezone.utc))  # before STARTED
    seat.live_log()
    seat.run("death")
    assert seat.meta()["handoff_fresh"] is False

# ------------------------------------------------------------------ fail-open + Recovery alert
def test_unwritable_chronicle_fails_open_and_alerts_recovery_once(seat):
    seat.runtime()
    seat.live_log()
    seat.root.rmdir()
    seat.root.write_text("not a directory")  # every write under it fails
    first = seat.run("death")
    second = seat.run("death", now=NOW + timedelta(minutes=1))
    assert first["status"] == "failed" and "error" in first
    assert second["status"] == "failed"
    assert len(seat.alerts) >= 1  # the stamp cannot be written either, so no cooldown applies
    assert seat.alerts and seat.alerts[0][0] == seat.persona and seat.alerts[0][2] == sc.ALERT_VOCAB


def test_failed_archive_marks_the_claim_and_alerts_with_cooldown(seat, monkeypatch):
    seat.runtime()
    seat.live_log()

    def broken(*a, **k):
        raise chronicle.ChronicleDurabilityError("disk full")

    monkeypatch.setattr(chronicle, "archive_required", broken)
    first = seat.run("death")
    assert first["status"] == "failed"
    claims = sc._claims_for(seat.root, seat.agent)
    assert [c["state"] for c in claims] == ["failed"]
    assert len(seat.alerts) == 1
    seat.live_log(stamp="2026-10-14 21:00:00")
    seat.run("death", now=NOW + timedelta(minutes=5))
    assert len(seat.alerts) == 1  # cooldown
    monkeypatch.undo()
    # The failed claim is retried by the next fire and then succeeds, once.
    seat.runtime()
    retry = sc.session_end(seat.request("death", sources=tuple(seat.logs.glob("*.dying*"))),
                           now=NOW + timedelta(minutes=6), route_fn=lambda *a: seat.alerts.append(a))
    assert any(r["status"] == "archived" for r in retry["results"]), retry


def test_recovery_failure_routes_through_the_failure_chokepoint_as_recovery(tmp_path, monkeypatch):
    seat = Seat(tmp_path, persona="recovery")
    calls: list[tuple] = []
    import tools.inbox_consumer as inbox_consumer

    monkeypatch.setattr(inbox_consumer, "route_failure_alert", lambda *a: calls.append(a))
    monkeypatch.delenv(sc.ALERT_ENV, raising=False)
    outcome = sc.alert_failure("recovery", "disk full", root=seat.root, agent="recovery", now=NOW)
    assert outcome == "routed"
    assert calls and calls[0][0] == "recovery" and calls[0][2] == sc.ALERT_VOCAB  # the chokepoint applies recovery->orchestrator
    assert sc.alert_failure("recovery", "again", root=seat.root, agent="recovery", now=NOW) == "cooldown"


def test_alert_env_off_keeps_the_alert_in_the_log(seat, monkeypatch):
    monkeypatch.setenv(sc.ALERT_ENV, "off")
    assert sc.alert_failure(seat.persona, "x", root=seat.root, agent=seat.agent, now=NOW) == "disabled"


def test_session_end_never_raises(seat, monkeypatch):
    monkeypatch.setattr(sc, "_runtime_record", lambda *a: (_ for _ in ()).throw(RuntimeError("bad")))
    summary = seat.run("death")
    assert summary["status"] == "failed"
    assert seat.alerts


def test_cli_always_exits_zero_and_prints_json(seat, capsys):
    assert sc.main(["session-end", "--cause", "death"]) == 0
    assert json.loads(capsys.readouterr().out)["error"] == "cannot resolve persona"
    assert sc.main(["coverage", "--persona", seat.persona, "--bool", "--persona-state-dir", str(seat.state)]) == 0
    assert capsys.readouterr().out.strip() in ("true", "false")
    assert sc.main(["session-end", "--cause", "not-a-cause"]) != 0  # argparse usage error, still no traceback


# ------------------------------------------------------------------ fleet check
def _coverage(seat: Seat, restart: datetime | None):
    return sc.coverage(seat.persona, state_root=seat.state, root=seat.root, now=NOW,
                       restart=(restart, "test"))


def test_coverage_ok_after_the_restart_was_archived(seat):
    seat.runtime()
    seat.live_log()
    seat.run("death")
    assert _coverage(seat, NOW + timedelta(seconds=30))["status"] == "OK"


def test_coverage_fails_when_the_last_restart_has_no_archive(seat):
    seat.runtime()
    seat.live_log()
    seat.run("death")
    late = NOW + timedelta(days=1)
    result = _coverage(seat, late)
    assert result["status"] == "FAIL" and result["detail"].startswith("no-archive-after-last-restart")
    assert _coverage(seat, None)["status"] == "WARN"


def test_coverage_fails_on_a_failed_claim(seat, monkeypatch):
    seat.runtime()
    seat.live_log()
    monkeypatch.setattr(chronicle, "archive_required",
                        lambda *a, **k: (_ for _ in ()).throw(chronicle.ChronicleDurabilityError("x")))
    seat.run("death")
    assert _coverage(seat, NOW)["status"] == "FAIL"


def test_coverage_last_restart_falls_back_to_the_runtime_record(seat):
    seat.runtime()
    started, source = sc.last_restart(seat.persona, tmux_target=None, tmux_socket=None, state_root=seat.state)
    assert source == "runtime.json" and started == sc._parse_time(STARTED)


def test_fleet_health_chronicle_sweep_reads_coverage_per_seat():
    replies = {
        "recovery": json.dumps({"status": "OK", "detail": "fine"}),
        "specialist": json.dumps({"status": "FAIL", "detail": "no-archive-after-last-restart"}),
        "artisan": "garbage",
    }
    seen: list[tuple[str, str]] = []

    def run(user, inner, timeout=15):
        seen.append((user, inner))
        persona = inner.split("--persona ")[1].split()[0]
        if persona == "watchman":
            return (None, "", False)
        return (0, replies[persona], True)

    records = fleet_health.chronicle_coverage_sweep(["recovery", "specialist", "artisan", "watchman"], run=run)
    assert [r["status"] for r in records] == ["OK", "FAIL", "WARN", "WARN"]
    assert seen[0][0] == fleet_health.FLEET["recovery"]["user"]
    assert "coverage --persona recovery --tmux-target" in seen[0][1]
    text = fleet_health.render_chronicle(records)
    assert "no archive after last restart on specialist" in text


# ------------------------------------------------------------------ real tmux (private server)
def _tmux(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=10, **kw)


def _wait_for(path: Path, word: str, seconds: float = 30.0) -> str:
    deadline = time.monotonic() + seconds
    text = ""
    while time.monotonic() < deadline:
        text = path.read_text(errors="replace") if path.exists() else ""
        if word in text:
            return text
        time.sleep(0.2)
    return text


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_real_tmux_close_then_rearm_after_respawn_gives_a_fresh_writing_log(tmp_path, monkeypatch):
    """Risk 3 (T-5016 plan): a pipe-pane tap SURVIVES `respawn-pane -k`. Prove that
    the duty's close + the controller's re-arm (arm_pane_log.sh) leaves the old
    session's log complete in the archive and the new session writing a FRESH log.
    Runs on conftest's private TMUX_TMPDIR server; TMUX is unset there."""
    seat = Seat(tmp_path, persona="probe")
    monkeypatch.setenv("PANE_LOG_DIR", str(seat.logs))
    go_old, go_new = tmp_path / "go-old", tmp_path / "go-new"
    old = f"while [ ! -e '{go_old}' ]; do sleep 0.1; done; echo old-session-line; sleep 60"
    assert _tmux("new-session", "-d", "-s", "t5016", "-x", "80", "-y", "20", "sh", "-c", old).returncode == 0
    try:
        arm = subprocess.run(["bash", str(REPO / "tools" / "arm_pane_log.sh"), "t5016:0.0", "probe"],
                             capture_output=True, text=True, timeout=20)
        assert "armed:" in arm.stderr, arm.stderr
        live = seat.logs / "pane-timed-probe.log"
        go_old.touch()
        assert "old-session-line" in _wait_for(live, "old-session-line")
        seat.runtime()

        summary = seat.run("start", tmux_target="t5016:0.0")  # what H1 runs before the respawn

        assert summary["cause"] == "replace" and summary["closed_pipe"] is True, summary
        assert _tmux("display-message", "-p", "-t", "t5016:0.0", "#{pane_pipe}").stdout.strip() == "0"
        new = f"while [ ! -e '{go_new}' ]; do sleep 0.1; done; echo new-session-line; sleep 60"
        assert _tmux("respawn-pane", "-k", "-t", "t5016:0.0", "sh", "-c", new).returncode == 0
        rearm = subprocess.run(["bash", str(REPO / "tools" / "arm_pane_log.sh"), "t5016:0.0", "probe"],
                               capture_output=True, text=True, timeout=20)  # what H1's re-arm runs
        assert "armed:" in rearm.stderr, rearm.stderr
        go_new.touch()
        fresh = _wait_for(live, "new-session-line")
        assert "new-session-line" in fresh and "old-session-line" not in fresh
        (gz,) = seat.files(".pane.log.gz")
        archived = gzip.open(gz, "rt").read()
        assert "old-session-line" in archived and "new-session-line" not in archived
    finally:
        _tmux("kill-session", "-t", "t5016")


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_real_tmux_without_the_close_the_tap_survives_respawn(tmp_path, monkeypatch):
    """The baseline the close fixes: re-arm is refused ('already piped') and the
    new session keeps writing into the old (renamed) file."""
    logs = tmp_path / "logs"
    monkeypatch.setenv("PANE_LOG_DIR", str(logs))
    assert _tmux("new-session", "-d", "-s", "t5016b", "sh", "-c", "echo first; sleep 60").returncode == 0
    try:
        subprocess.run(["bash", str(REPO / "tools" / "arm_pane_log.sh"), "t5016b:0.0", "base"],
                       capture_output=True, text=True, timeout=20)
        assert _tmux("respawn-pane", "-k", "-t", "t5016b:0.0", "sh", "-c", "sleep 60").returncode == 0
        assert _tmux("display-message", "-p", "-t", "t5016b:0.0", "#{pane_pipe}").stdout.strip() == "1"
        again = subprocess.run(["bash", str(REPO / "tools" / "arm_pane_log.sh"), "t5016b:0.0", "base"],
                               capture_output=True, text=True, timeout=20)
        assert "already piped" in again.stderr
    finally:
        _tmux("kill-session", "-t", "t5016b")


# ------------------------------------------------------------------ review rebound 1
HANDOFF_SCRIPT = REPO / "tools" / "handoff_and_restart.sh"
NEW_STARTED = "2026-10-14T23:30:00Z"


def _script_stage(seat: Seat) -> tuple[Path, str]:
    """What handoff_and_restart.sh does before its restart: stage the live log and
    pin the ending session's identity (the `identity` subcommand)."""
    live = seat.logs / f"pane-timed-{seat.persona}.log"
    staged = live.with_name(live.name + ".dying")
    live.rename(staged)
    rt = sc._runtime_record(seat.persona, seat.state).get("started_at") or "-"
    return staged, rt


def _script_duty(seat: Seat, staged: Path, rt: str, now: datetime) -> dict:
    return seat.run("graceful-restart", now=now, stage=False, sources=(staged,), certain=True,
                    handoff_fresh=True, rt_pinned=True, rt_started_at=None if rt == "-" else rt)


def _successor_starts(seat: Seat) -> Path:
    seat.runtime(started=NEW_STARTED)  # the restart rewrote runtime.json
    return seat.live_log("successor output", stamp="2026-10-14 23:30:05")  # post_start re-armed a tap


def test_b1_controller_hook_first_then_the_script_is_a_noop(seat):
    seat.runtime()
    seat.write_handoff()
    seat.live_log("old session")
    staged, rt = _script_stage(seat)
    h1 = seat.run("start")                            # H1 inside the restart
    assert h1["results"][0]["status"] == "archived"
    live = _successor_starts(seat)
    after = _script_duty(seat, staged, rt, NOW + timedelta(minutes=1))
    assert [r["status"] for r in after["results"]] == ["already-archived"], after
    assert live.exists() and "successor output" in live.read_text()  # never consumed
    assert len(seat.index()) == 1
    # No false done claim on the successor: its own end archives normally.
    end = seat.run("death", now=NOW + timedelta(hours=1))
    assert end["results"][0]["status"] == "archived"
    assert len(seat.index()) == 2


def test_b1_script_first_then_the_controller_hook_is_a_noop(seat):
    seat.runtime()
    seat.write_handoff()
    seat.live_log("old session")
    staged, rt = _script_stage(seat)
    first = _script_duty(seat, staged, rt, NOW)
    assert first["results"][0]["status"] == "archived"
    h1 = seat.run("start", now=NOW + timedelta(seconds=20))
    assert h1["results"][0]["status"] == "already-archived"
    live = _successor_starts(seat)
    assert live.exists()
    assert len(seat.index()) == 1
    (gz,) = seat.files(".pane.log.gz")
    assert "old session" in gzip.open(gz, "rt").read()


def test_b1_reviewer_repro_unpinned_post_restart_call_writes_nothing(seat):
    """The reviewer's t5016repro shape: H1 archives the staged log, the successor
    starts, then a --certain call with no source, no stage and no pinned identity
    runs. It used to file a second record + a false done claim on the successor."""
    seat.runtime()
    seat.live_log("old session")
    _script_stage(seat)
    seat.run("start")
    live = _successor_starts(seat)
    after = seat.run("graceful-restart", now=NOW + timedelta(seconds=10), stage=False, certain=True)
    assert [r["status"] for r in after["results"]] == ["unpinned-refused"]
    assert live.exists() and len(seat.index()) == 1
    assert seat.run("death", now=NOW + timedelta(hours=1))["results"][0]["status"] == "archived"


def test_b1_a_non_stage_caller_never_consumes_the_live_log(seat):
    seat.runtime()
    live = seat.live_log("someone's live session")
    for kw in (dict(stage=False), dict(stage=False, certain=True, rt_pinned=True, rt_started_at=STARTED)):
        seat.run("graceful-restart", **kw)
        assert live.exists() and "live session" in live.read_text()


def test_b1_the_script_pins_identity_before_its_restart():
    text = HANDOFF_SCRIPT.read_text()
    restart = text.index("# Step 5: systemctl restart.")
    assert text.index('session_chronicle.py" identity') < restart
    tail = text[restart:]
    assert '--rt-started-at "${SC_RT_STARTED:--}"' in tail
    assert "pane-timed-*.log.dying" not in tail  # the post-restart call reuses the PRE-restart list


def test_identity_cli_prints_the_recorded_start(seat, capsys, monkeypatch):
    monkeypatch.setenv("PANTHEON_PERSONA_STATE_DIR", str(seat.state))
    assert sc.main(["identity", "--persona", seat.persona]) == 0
    assert capsys.readouterr().out.strip() == "-"
    seat.runtime()
    sc.main(["identity", "--source", str(seat.logs / f"pane-timed-{seat.persona}.log.dying")])
    assert capsys.readouterr().out.strip() == STARTED


def test_m1_stage_if_dead_leaves_a_live_pane_alone(seat, monkeypatch):
    seat.runtime()
    live = seat.live_log("destination session after an operator swap")
    # T-5019: the post-death check reads the pane through _tmux_checked (so a
    # timeout is distinguishable); the fake answers like a live pane.
    monkeypatch.setattr(sc, "_tmux_checked", lambda socket, *a: (_completed(0, "0\n"), False))
    closed = []
    monkeypatch.setattr(sc, "close_pane_pipe", lambda *a: closed.append(a) or True)
    summary = seat.run("death", tmux_target="seat:harness.0", stage_if_dead=True)
    assert summary["results"] == [{"status": "pane-live-skip"}]
    assert closed == [] and live.exists() and seat.index() == []
    monkeypatch.setattr(sc, "_tmux_checked", lambda socket, *a: (_completed(1, ""), False))  # pane gone
    monkeypatch.setattr(sc, "_pane_query", lambda target, socket, fmt: None)
    summary = seat.run("death", tmux_target="seat:harness.0", stage_if_dead=True)
    assert summary["results"][0]["status"] == "archived"


def test_minor3_invalid_persona_is_refused_before_any_path(tmp_path, capsys):
    seat = Seat(tmp_path)
    for bad in ("../etc", "Recovery", "a/b", "", "x y"):
        out = sc.session_end(seat.request("death", persona=bad), now=NOW)
        assert out["status"] == "failed" and "invalid persona" in out["error"]
    assert list(seat.root.iterdir()) == []
    assert sc.main(["session-end", "--persona", "../x", "--cause", "death"]) == 0
    assert json.loads(capsys.readouterr().out)["error"] == "invalid persona"
    with pytest.raises(ValueError):
        sc.coverage("../x", state_root=seat.state, root=seat.root)


def test_minor4_a_killed_writer_is_finished_not_duplicated(seat):
    seat.runtime()
    seat.write_handoff()
    seat.live_log("killed mid-archive")
    key = sc._session_key(seat.persona, STARTED, "2026-10-14 20:00:01")
    # The predecessor wrote the record, then was killed before marking done.
    stem_ts = "2026-10-14T22:00:00Z"
    chronicle.archive_required(seat.agent, "handoff", jsonl_uuid="rt-x", reason="session-end/death",
                               ts=stem_ts, root=seat.root)
    claims = sc._claims_dir(seat.root, seat.agent)
    claims.mkdir(parents=True, exist_ok=True)
    (claims / f"{key}.json").write_text(json.dumps({
        "state": "claimed", "pid": 1, "at": sc._iso(NOW - timedelta(hours=1)),
        "stem_ts": stem_ts, "stem_uuid": "rt-x"}))
    summary = seat.run("death")
    result = summary["results"][0]
    assert result["status"] == "archived" and result.get("reused_record") is True, result
    assert len(seat.index()) == 1
    assert len(seat.files(".pane.log.gz")) == 1
    assert json.loads((claims / f"{key}.json").read_text())["state"] == "done"


def test_minor4_busy_is_reported_not_silent(seat, capsys):
    seat.runtime()
    seat.live_log()
    key = sc._session_key(seat.persona, STARTED, "2026-10-14 20:00:01")
    claims = sc._claims_dir(seat.root, seat.agent)
    claims.mkdir(parents=True, exist_ok=True)
    (claims / f"{key}.json").write_text(json.dumps({"state": "claimed", "pid": 4242, "at": sc._iso(NOW)}))
    result = seat.run("death")["results"][0]
    assert result["status"] == "busy" and result["busy_holder"]["pid"] == 4242
    assert "being archived by pid 4242" in capsys.readouterr().err


def test_minor2_claude_lookup_is_scoped_and_ambiguity_never_names_the_stem(tmp_path):
    seat = Seat(tmp_path)
    cwd = Path("/home/claude-research/pantheon-config")
    mine = seat.home / ".claude" / "projects" / "-home-claude-research-pantheon-config"
    other = seat.home / ".claude" / "projects" / "-home-claude-research"
    for path in (mine / "aaaa.jsonl", other / "zzzz.jsonl", mine / "aaaa" / "subagents" / "sub.jsonl"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
    claude = harness_integration.integration_for("claude")
    found = claude.session_transcript(account_home=seat.home, started_at=None, cwd=cwd)
    assert (found.path, found.candidates) == (mine / "aaaa.jsonl", 1)  # not the other project, not a subagent
    unscoped = claude.session_transcript(account_home=seat.home, started_at=None)
    assert unscoped.candidates == 2
    # An ambiguous pointer is recorded but the stem falls back to rt-<key>.
    seat.runtime()
    seat.live_log()
    seat.run("death")
    assert seat.index()[0]["uuid"].startswith("rt-")
    assert seat.meta()["native_transcript"]["candidates"] == 2


def test_minor1_coverage_names_a_stale_runtime_record(seat):
    seat.runtime(started="2026-10-01T00:00:00Z")
    result = _coverage(seat, NOW)
    assert result["status"] == "FAIL"
    assert "stale-runtime-record" in result["detail"]


# ------------------------------------------------------------------ T-5019 follow-ups
STALE_STARTED = "2026-10-01T00:00:00Z"
FIRST_STAMP = "2026-10-14 20:00:01"


def _completed(returncode: int, stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["tmux"], returncode, stdout=stdout, stderr="")


def _active(seat: Seat, started_ts: str | None) -> None:
    path = seat.state / seat.persona / "active-runtime.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if started_ts is None:
        path.unlink(missing_ok=True)
        return
    path.write_text(json.dumps({"schema": "pantheon-persona-active-runtime/v1", "started_ts": started_ts}))


def _claim_docs(seat: Seat) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(sc._claims_dir(seat.root, seat.agent).glob("*.json"))]


# item 2: the restart script's identity capture failed ('-')
def test_t5019_unknown_identity_consumed_sources_noop_when_stamp_done(seat):
    seat.runtime()
    seat.write_handoff()
    seat.live_log("old session", stamp=FIRST_STAMP)
    staged, _ = _script_stage(seat)
    assert seat.run("start")["results"][0]["status"] == "archived"   # H1 consumed the staged log
    assert not staged.exists()
    after = seat.run("graceful-restart", now=NOW + timedelta(minutes=1), stage=False, sources=(staged,),
                     certain=True, handoff_fresh=True, rt_pinned=True, rt_started_at=None,
                     pane_stamps=(FIRST_STAMP,))
    assert [r["status"] for r in after["results"]] == ["already-archived"], after
    assert len(seat.index()) == 1


def test_t5019_unknown_identity_source_still_present_noop(seat):
    seat.runtime()
    seat.live_log("old session", stamp=FIRST_STAMP)
    staged, _ = _script_stage(seat)
    assert seat.run("start")["results"][0]["status"] == "archived"
    staged.write_text(f"[{FIRST_STAMP}] old session\n")  # H1's unlink failed: the file survived
    after = seat.run("graceful-restart", now=NOW + timedelta(minutes=1), stage=False, sources=(staged,),
                     certain=True, rt_pinned=True, rt_started_at=None)
    assert [r["status"] for r in after["results"]] == ["already-archived"], after
    assert len(seat.index()) == 1


def test_t5019_unknown_identity_stamp_without_done_claim_still_archives(seat):
    seat.runtime()
    seat.live_log("old session", stamp=FIRST_STAMP)
    staged, _ = _script_stage(seat)
    after = seat.run("graceful-restart", stage=False, sources=(staged,), certain=True,
                     rt_pinned=True, rt_started_at=None, pane_stamps=(FIRST_STAMP,))
    assert [r["status"] for r in after["results"]] == ["archived"], after
    assert len(seat.index()) == 1


def test_t5019_cli_pane_stamp_is_repeatable_and_malformed_values_are_dropped(seat, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(sc, "session_end", lambda req: seen.append(req) or {"status": "ok"})
    sc.main(["session-end", "--persona", seat.persona, "--cause", "graceful-restart",
             "--rt-started-at", "-", "--pane-stamp", FIRST_STAMP, "--pane-stamp", "not a stamp",
             "--pane-stamp", "2026-10-14 21:00:00"])
    capsys.readouterr()
    assert seen[0].pane_stamps == (FIRST_STAMP, "2026-10-14 21:00:00")
    assert seen[0].rt_pinned and seen[0].rt_started_at is None


def test_t5019_the_script_pins_pane_stamps_before_its_restart():
    text = HANDOFF_SCRIPT.read_text()
    restart = text.index("# Step 5: systemctl restart.")
    assert text.index('_sc_stamps+=(--pane-stamp "$_st")') < restart
    assert '"${_sc_sources[@]}" "${_sc_stamps[@]}"' in text[restart:]


# item 3: a hung tmux is unknown, never dead
def test_t5019_stage_if_dead_tmux_timeout_is_unknown_and_touches_nothing(seat, monkeypatch):
    seat.runtime()
    live = seat.live_log("maybe still running")

    def hung(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

    monkeypatch.setattr(sc.subprocess, "run", hung)
    summary = seat.run("death", tmux_target="seat:harness.0", stage_if_dead=True)
    assert summary["results"] == [{"status": "pane-unknown-skip"}], summary
    assert live.exists() and "maybe still running" in live.read_text()
    assert not list(seat.logs.glob("*.dying*"))
    assert seat.index() == [] and _claim_docs(seat) == []


def test_t5019_stage_if_dead_pane_gone_still_stages(seat, monkeypatch):
    seat.runtime()
    seat.live_log("dead session")
    monkeypatch.setattr(sc.subprocess, "run", lambda argv, **kw: _completed(1, ""))
    summary = seat.run("death", tmux_target="seat:harness.0", stage_if_dead=True)
    assert summary["results"][0]["status"] == "archived"
    assert sc._pane_dead_state(None, None) == "dead"


def test_t5019_tmux_wrapper_keeps_none_on_timeout_for_other_callers(monkeypatch):
    def hung(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(sc.subprocess, "run", hung)
    assert sc._tmux(None, "display-message") is None
    assert sc._pane_query("t", None, "#{pane_pid}") is None
    assert sc.close_pane_pipe("t", None) is False


# item 4: a failed claim keeps its stem, so the retry reuses the record
def test_t5019_failed_claim_carries_stem_and_retry_reuses_record(seat, monkeypatch):
    seat.runtime()
    seat.write_handoff()
    seat.live_log("session", stamp=FIRST_STAMP)
    real = chronicle.write_companion_text
    calls = []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk hiccup")
        return real(*a, **kw)

    monkeypatch.setattr(chronicle, "write_companion_text", flaky)
    first = seat.run("death")
    assert first["results"][0]["status"] == "failed"
    (doc,) = _claim_docs(seat)
    assert doc["state"] == "failed" and doc.get("stem_ts") and doc.get("stem_uuid"), doc
    assert len(seat.index()) == 1                    # archive_required succeeded before the failure
    seat.live_log("session", stamp=FIRST_STAMP)      # the next stage run sees the same session's log
    retry = seat.run("death", now=NOW + timedelta(minutes=1))
    result = retry["results"][0]
    assert result["status"] == "archived" and result.get("reused_record") is True, result
    assert len(seat.index()) == 1


# item 5: a stale runtime.json (launch record not persisted) with no pane log
def test_t5019_stale_runtime_record_active_runtime_separates_sessions(seat):
    seat.runtime(started=STALE_STARTED)
    _active(seat, "2026-10-14T20:00:00Z")
    first = seat.run("death", stage_if_dead=True)
    assert first["results"][0]["status"] == "archived"
    _active(seat, "2026-10-14T22:00:00Z")            # the next session's launch, record still stale
    second = seat.run("death", now=NOW + timedelta(hours=1), stage_if_dead=True)
    assert second["results"][0]["status"] == "archived", second
    assert len(seat.index()) == 2
    assert all(d.get("rt_record_started_at") == STALE_STARTED for d in _claim_docs(seat))


def test_t5019_h2_then_cleared_active_then_h1_is_a_noop(seat):
    seat.runtime(started=STALE_STARTED)
    _active(seat, "2026-10-14T20:00:00Z")
    assert seat.run("death", stage_if_dead=True)["results"][0]["status"] == "archived"   # H2
    _active(seat, None)                                                                    # clear_active_runtime
    h1 = seat.run("start", now=NOW + timedelta(seconds=30))
    assert [r["status"] for r in h1["results"]] == ["already-archived"], h1
    assert len(seat.index()) == 1


def test_t5019_healthy_runtime_record_key_and_claim_unchanged(seat):
    seat.runtime()
    _active(seat, STARTED)                           # the controller writes both from one handle
    result = seat.run("death")["results"][0]
    assert result["key"] == sc._session_key(seat.persona, STARTED, None)
    (doc,) = _claim_docs(seat)
    assert "rt_record_started_at" not in doc and doc["rt_started_at"] == STARTED


def test_t5019_older_active_runtime_loses_to_runtime_json(seat):
    seat.runtime()
    _active(seat, "2026-10-14T19:00:00Z")
    result = seat.run("death")["results"][0]
    assert result["key"] == sc._session_key(seat.persona, STARTED, None)


def test_t5019_identity_prints_the_effective_start(seat, capsys, monkeypatch):
    monkeypatch.setenv("PANTHEON_PERSONA_STATE_DIR", str(seat.state))
    seat.runtime(started=STALE_STARTED)
    _active(seat, "2026-10-14T20:00:00Z")
    sc.main(["identity", "--persona", seat.persona])
    assert capsys.readouterr().out.strip() == "2026-10-14T20:00:00Z"
    _active(seat, None)
    sc.main(["identity", "--persona", seat.persona])
    assert capsys.readouterr().out.strip() == STALE_STARTED


# ------------------------------------------------------------------ T-5019 rebound 1
ACTIVE_S2 = "2026-10-14T20:00:00Z"


def _pane_gone(monkeypatch) -> None:
    monkeypatch.setattr(sc, "_tmux_checked", lambda socket, *a: (_completed(1, ""), False))


def _stale_seat_with_log(seat: Seat) -> None:
    seat.runtime(started=STALE_STARTED)
    _active(seat, ACTIVE_S2)
    seat.write_handoff()
    seat.live_log("s2", stamp=FIRST_STAMP)


def _h1_after_active_cleared(seat: Seat) -> dict:
    _active(seat, None)  # controller_main's clear_active_runtime after H2
    return seat.run("start", stage=True, now=NOW + timedelta(seconds=30))


# MAJOR 1: the reviewer's repro (H2's pane-log write failed, so the log is pending)
def test_t5019_r1_stale_h2_companion_fails_then_h1_attaches_not_duplicates(seat, monkeypatch):
    _stale_seat_with_log(seat)
    real = chronicle.archive_companion
    monkeypatch.setattr(chronicle, "archive_companion", lambda *a, **k: None)
    _pane_gone(monkeypatch)
    h2 = seat.run("death", stage=True, stage_if_dead=True, tmux_target="s:h.0")
    assert h2["results"][0]["pane_log_state"] == "pending", h2
    monkeypatch.setattr(chronicle, "archive_companion", real)
    h1 = _h1_after_active_cleared(seat)
    assert [r["status"] for r in h1["results"]] == ["attached"], h1
    assert len(seat.index()) == 1
    assert len(seat.files(".pane.log.gz")) == 1


# MAJOR 1 variant: H2 archived fully but could not delete the source (stray .dying)
def test_t5019_r1_stale_stray_dying_after_h2_is_a_noop(seat, monkeypatch):
    _stale_seat_with_log(seat)
    _pane_gone(monkeypatch)
    h2 = seat.run("death", stage=True, stage_if_dead=True, tmux_target="s:h.0")
    assert h2["results"][0]["status"] == "archived"
    stray = seat.logs / f"pane-timed-{seat.persona}.log.dying"
    stray.write_text(f"[{FIRST_STAMP}] s2\n")  # the unlink after the gzip failed
    h1 = _h1_after_active_cleared(seat)
    assert [r["status"] for r in h1["results"]] == ["already-archived"], h1
    assert len(seat.index()) == 1


# MAJOR 1 variant: a fleet-reboot pause recorded the session and left the live log
def test_t5019_r1_stale_fleet_reboot_deferred_log_attaches_once(seat):
    _stale_seat_with_log(seat)
    pause = seat.run("fleet-reboot", stage=False, defer_pane_log=True, certain=True)
    assert pause["results"][0]["status"] == "archived"
    h1 = _h1_after_active_cleared(seat)
    assert [r["status"] for r in h1["results"]] == ["attached"], h1
    assert len(seat.index()) == 1
    assert len(seat.files(".pane.log.gz")) == 1


# MAJOR 1 variant: H2 took pane-unknown-skip, then the active file was cleared
def test_t5019_r1_stale_unknown_skip_then_h1_archives_once(seat, monkeypatch):
    _stale_seat_with_log(seat)
    monkeypatch.setattr(sc, "_tmux_checked", lambda socket, *a: (None, True))
    skip = seat.run("death", stage=True, stage_if_dead=True, tmux_target="s:h.0")
    assert skip["results"] == [{"status": "pane-unknown-skip"}]
    _pane_gone(monkeypatch)
    h1 = _h1_after_active_cleared(seat)
    assert [r["status"] for r in h1["results"]] == ["archived"], h1
    again = seat.run("start", stage=True, now=NOW + timedelta(minutes=2))
    assert all(r["status"] != "archived" for r in again["results"]), again
    assert len(seat.index()) == 1


# MAJOR 1 variant: a FAILED H2 claim (stale seat) is retried under its own stem
def test_t5019_r1_stale_failed_h2_claim_is_retried_not_duplicated(seat, monkeypatch):
    _stale_seat_with_log(seat)
    real = chronicle.write_companion_text
    calls = []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk hiccup")
        return real(*a, **kw)

    monkeypatch.setattr(chronicle, "write_companion_text", flaky)
    _pane_gone(monkeypatch)
    assert seat.run("death", stage=True, stage_if_dead=True, tmux_target="s:h.0")["results"][0]["status"] == "failed"
    seat.live_log("s2", stamp=FIRST_STAMP)  # the same session's log, seen again
    h1 = _h1_after_active_cleared(seat)
    assert h1["results"][0]["status"] == "archived" and h1["results"][0].get("reused_record"), h1
    assert len(seat.index()) == 1


# MINOR 2: the stamp match attaches a still-pending pane log
def test_t5019_r1_unknown_identity_stamp_match_attaches_pending_log(seat, monkeypatch):
    seat.runtime()
    seat.write_handoff()
    seat.live_log("old session", stamp=FIRST_STAMP)
    staged, _ = _script_stage(seat)
    real = chronicle.archive_companion
    monkeypatch.setattr(chronicle, "archive_companion", lambda *a, **k: None)
    h1 = seat.run("start")
    assert h1["results"][0]["pane_log_state"] == "pending" and staged.exists(), h1
    monkeypatch.setattr(chronicle, "archive_companion", real)
    after = seat.run("graceful-restart", now=NOW + timedelta(minutes=1), stage=False, sources=(staged,),
                     certain=True, rt_pinned=True, rt_started_at=None, pane_stamps=(FIRST_STAMP,))
    assert [r["status"] for r in after["results"]] == ["attached"], after
    assert len(seat.index()) == 1
    assert len(seat.files(".pane.log.gz")) == 1


# MINOR 3: an unknown-pane skip is visible to coverage until a later run resolves it
def test_t5019_r1_coverage_reports_an_unknown_pane_skip_until_resolved(seat, monkeypatch):
    seat.runtime()
    seat.live_log("maybe running", stamp=FIRST_STAMP)
    monkeypatch.setattr(sc, "_tmux_checked", lambda socket, *a: (None, True))
    seat.run("death", stage=True, stage_if_dead=True, tmux_target="s:h.0")
    pending = _coverage(seat, NOW - timedelta(minutes=1))
    assert pending["status"] == "WARN", pending
    assert "session end pending: pane state unknown" in pending["detail"]
    _pane_gone(monkeypatch)
    assert seat.run("start", stage=True, now=NOW + timedelta(seconds=30))["results"][0]["status"] == "archived"
    resolved = _coverage(seat, NOW - timedelta(minutes=1))
    assert resolved["status"] == "OK", resolved
