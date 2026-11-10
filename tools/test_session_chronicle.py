"""Tests for tools/session_chronicle.py, the session-end Chronicle duty (T-5016).

Every path is a tmp_path stand-in: chronicle root, persona state root, pane-log
dir, account home and HANDOFF. No real seat, state dir, hub or tmux server is
touched (the tmux tests run on conftest's private TMUX_TMPDIR server)."""

from __future__ import annotations

import grp
import gzip
import hashlib
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
OWN_GROUP = grp.getgrgid(os.getegid()).gr_name  # the test process's own group: no pantheon-shared needed


def _codex_day_dir(home: Path) -> Path:
    """Today's UTC rollout dir: the codex resolver searches the last 12 days of the REAL clock
    (T-6118), so a fixture dated to the test's frozen NOW would fall outside the window."""
    today = datetime.now(timezone.utc)
    return home / ".codex" / "sessions" / f"{today:%Y}" / f"{today:%m}" / f"{today:%d}"


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
                    log_dir=self.logs, archive_group=OWN_GROUP)
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
    codex = _codex_day_dir(seat.home) / "rollout-2026-10-14T20-00-02-2222-codex.jsonl"
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
    pointer = meta["native_transcript"]
    if harness == "hermes":  # T-6118: only claude/codex are archived; hermes keeps the bare pointer
        assert pointer["path"] == str(expected_path)
    else:
        assert pointer["source_path"] == str(expected_path)
        assert pointer["path"].endswith(".transcript.jsonl.gz")
    assert pointer["session_id"] == expected_id
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


def test_t5132_coverage_keeps_a_fresh_pane_unknown_marker_at_warn(seat):
    sc._mark_pane_unknown(seat.root, seat.agent, NOW - timedelta(minutes=30))

    result = _coverage(seat, NOW - timedelta(minutes=1))

    assert result["status"] == "WARN", result
    assert "session end pending: pane state unknown" in result["detail"]


def test_t5132_coverage_escalates_a_stale_pane_unknown_marker_to_fail(seat):
    seat.runtime()
    seat.live_log()
    seat.run("death")
    sc._mark_pane_unknown(
        seat.root,
        seat.agent,
        NOW - timedelta(seconds=sc.PANE_UNKNOWN_STALE_SECONDS + 1),
    )

    result = _coverage(seat, NOW + timedelta(seconds=30))

    assert result["status"] == "FAIL", result
    assert "stale pane-unknown marker" in result["detail"]


def test_t5132_coverage_reports_the_real_miss_behind_a_stale_marker(seat):
    sc._mark_pane_unknown(
        seat.root,
        seat.agent,
        NOW - timedelta(seconds=sc.PANE_UNKNOWN_STALE_SECONDS + 1),
    )

    result = _coverage(seat, NOW - timedelta(minutes=1))

    assert result["status"] == "FAIL", result
    assert "stale pane-unknown marker" in result["detail"]
    assert "no-archive-after-last-restart" in result["detail"]


# ------------------------------------------------------------------ T-6115 D-3: thread-rotation
ROTATION_POINTER = {"path": "/chron/codex/2026-10-14/x.transcript.jsonl.gz", "bytes": 10,
                    "sha256": "ab" * 32, "source_bytes": 99}


def test_thread_rotation_is_a_known_cause():
    assert "thread-rotation" in sc.CAUSES


def test_native_transcript_override_is_recorded_instead_of_the_resolver_pointer(tmp_path):
    seat = Seat(tmp_path, harness="codex")
    _transcripts(seat)  # the resolver WOULD find a codex rollout
    seat.runtime()
    seat.write_handoff()
    seat.live_log("bye")
    out = seat.run("thread-rotation", harness="codex", native_transcript=dict(ROTATION_POINTER))
    assert out["results"][0]["status"] == "archived", out
    assert seat.meta()["native_transcript"] == ROTATION_POINTER
    assert seat.meta()["cause"] == "thread-rotation"


def test_without_the_override_the_resolver_pointer_is_unchanged(tmp_path):
    seat = Seat(tmp_path, harness="codex")
    _transcripts(seat)
    seat.runtime()
    seat.write_handoff()
    seat.live_log("bye")
    seat.run("death", harness="codex")
    pointer = seat.meta()["native_transcript"]
    assert pointer is not None and pointer != ROTATION_POINTER
    assert sc.SessionEndRequest(persona="x", cause="death").native_transcript is None


def test_cli_native_transcript_json_is_parsed_and_bad_json_is_a_usage_error(seat, capsys):
    ns = sc.build_parser().parse_args(["session-end", "--cause", "thread-rotation",
                                       "--native-transcript-json", json.dumps(ROTATION_POINTER)])
    assert ns.native_transcript_json == ROTATION_POINTER
    assert sc.main(["session-end", "--cause", "thread-rotation", "--native-transcript-json", "{not json"]) != 0
    assert sc.main(["session-end", "--cause", "thread-rotation", "--native-transcript-json", "[1]"]) != 0


# ------------------------------------------------------------------ T-6118 PR-2: transcript archive + export perms
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "chronicle"


def _run_claude(tmp_path, **kw):
    seat = Seat(tmp_path)
    _transcripts(seat)
    seat.runtime()
    seat.write_handoff()
    seat.live_log("bye")
    return seat, seat.run("death", **kw)


def _gzs(seat: Seat) -> list[Path]:
    return sorted((seat.root / seat.agent).glob("*/*.transcript.jsonl.gz*"))


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_session_end_gz_is_0640_in_group_and_pointer_matches(tmp_path):
    seat, out = _run_claude(tmp_path)
    assert out["results"][0]["status"] == "archived", out
    (gz,) = _gzs(seat)
    assert _mode(gz) == 0o640 and gz.stat().st_gid == os.getegid()
    pointer = seat.meta()["native_transcript"]
    data = gz.read_bytes()
    src = seat.home / ".claude" / "projects" / "-home-x-pantheon-config" / "1111-claude.jsonl"
    assert pointer["path"] == str(gz)
    assert pointer["bytes"] == len(data) and pointer["sha256"] == hashlib.sha256(data).hexdigest()
    assert pointer["source_path"] == str(src)
    assert pointer["source_sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    assert pointer["source_bytes"] == src.stat().st_size
    assert pointer["session_id"] == "1111-claude" and pointer["candidates"] == 1
    assert gzip.decompress(data) == src.read_bytes()


def test_md_and_session_json_are_0640_in_group(tmp_path):
    seat, _ = _run_claude(tmp_path)
    (md,) = seat.files(".md")
    (meta,) = seat.files(".session.json")
    for f in (md, meta):
        assert _mode(f) == 0o640 and f.stat().st_gid == os.getegid()


def test_md_is_written_temp_then_rename(tmp_path, monkeypatch):
    seat = Seat(tmp_path)
    seat.runtime()
    seat.write_handoff()
    seat.live_log("bye")
    replaced = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b, *x, **k: (replaced.append((Path(a), Path(b))), real(a, b, *x, **k))[1])
    seat.run("death")
    md = [(a, b) for a, b in replaced if b.suffix == ".md"]
    assert len(md) == 1 and md[0][0].name.startswith(".") and md[0][0].name.endswith(".tmp")
    assert md[0][0].parent == md[0][1].parent


def test_rotation_pointer_mints_no_gz_in_session_chronicle(tmp_path):
    seat = Seat(tmp_path, harness="codex")
    _transcripts(seat)
    seat.runtime()
    seat.write_handoff()
    seat.live_log("bye")
    seat.run("thread-rotation", harness="codex", native_transcript=dict(ROTATION_POINTER))
    assert _gzs(seat) == []
    assert seat.meta()["native_transcript"] == ROTATION_POINTER


def test_archive_failure_is_fail_open_with_bare_pointer_error_and_alert(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise chronicle.ChronicleDurabilityError("disk on fire token=hunter2abcdef")
    monkeypatch.setattr(chronicle, "archive_native_transcript", boom)
    seat, out = _run_claude(tmp_path)
    assert out["results"][0]["status"] == "archived", out
    meta = seat.meta()
    assert "native_transcript_error" in meta and "hunter2abcdef" not in meta["native_transcript_error"]
    assert meta["native_transcript"]["path"].endswith("1111-claude.jsonl")  # bare resolver pointer
    assert "source_path" not in meta["native_transcript"]
    assert seat.alerts, "an archive failure must alert"
    assert _gzs(seat) == []


def test_unavailable_group_writes_record_ungrouped_with_group_error_and_alert(tmp_path):
    seat, out = _run_claude(tmp_path, archive_group="no-such-group-t6118")
    assert out["results"][0]["status"] == "archived", out
    assert "does not exist" in seat.meta()["group_error"]
    for f in seat.files(".md") + seat.files(".session.json") + _gzs(seat):
        assert _mode(f) == 0o640
    assert len(_gzs(seat)) == 1
    assert len(seat.alerts) == 1


def test_real_fchown_permission_error_takes_the_same_fallback(tmp_path, monkeypatch):
    def deny(*a, **k):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(os, "fchown", deny)
    monkeypatch.setattr(os, "chown", deny)
    seat, out = _run_claude(tmp_path)
    assert out["results"][0]["status"] == "archived", out
    assert "group_error" in seat.meta()
    assert len(seat.files(".md")) == 1 and len(_gzs(seat)) == 1
    assert all(_mode(f) == 0o640 for f in seat.files(".md") + _gzs(seat))
    assert len(seat.alerts) == 1


def test_gz_only_permission_error_falls_back_ungrouped(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "chown", lambda *a, **k: (_ for _ in ()).throw(PermissionError(1, "EPERM")))
    seat, out = _run_claude(tmp_path)
    assert out["results"][0]["status"] == "archived", out
    assert "group_error" in seat.meta()
    (gz,) = _gzs(seat)
    assert seat.meta()["native_transcript"]["path"] == str(gz)


def test_rerun_is_already_archived_and_mints_no_second_gz(tmp_path):
    seat, _ = _run_claude(tmp_path)
    again = seat.run("death", now=NOW + timedelta(minutes=1))
    assert again["results"][0]["status"] == "already-archived", again
    assert len(_gzs(seat)) == 1


def _stem_gz(seat: Seat, stem_uuid: str, payload: bytes, name_suffix: str = "") -> Path:
    day = seat.root / seat.agent / "2026-10-14"
    day.mkdir(parents=True, exist_ok=True)
    gz = day / f"2026-10-14T230000Z-{stem_uuid}.transcript.jsonl.gz{name_suffix}"
    gz.write_bytes(gzip.compress(payload))
    return gz


def _pointer_for(src: Path) -> dict:
    return {"path": str(src), "session_id": "s", "bytes": src.stat().st_size,
            "sha256": hashlib.sha256(src.read_bytes()).hexdigest(), "candidates": 1}


def test_reused_path_reuses_a_matching_gz(tmp_path):
    seat = Seat(tmp_path)
    src = tmp_path / "src.jsonl"
    src.write_bytes(b'{"turn": 1}\n')
    prior = _stem_gz(seat, "u1", src.read_bytes())
    out = sc._archive_session_transcript(_pointer_for(src), agent=seat.agent, root=seat.root,
                                         stem_ts="2026-10-14T23:00:00Z", stem_uuid="u1",
                                         group=None, notes={})
    assert out["path"] == str(prior)
    assert not list(prior.parent.glob("*.2.gz"))
    assert out["sha256"] == hashlib.sha256(prior.read_bytes()).hexdigest()
    assert out["source_sha256"] == _pointer_for(src)["sha256"]


def test_reused_path_does_not_reuse_a_nonmatching_or_corrupt_gz(tmp_path):
    seat = Seat(tmp_path)
    src = tmp_path / "src.jsonl"
    src.write_bytes(b'{"turn": 1}\n')
    stale = _stem_gz(seat, "u1", b"something else\n")
    out = sc._archive_session_transcript(_pointer_for(src), agent=seat.agent, root=seat.root,
                                         stem_ts="2026-10-14T23:00:00Z", stem_uuid="u1",
                                         group=None, notes={})
    assert out["path"] != str(stale) and out["path"].endswith(".2.gz")
    assert gzip.decompress(Path(out["path"]).read_bytes()) == src.read_bytes()
    stale.write_bytes(b"not gzip at all")
    assert sc._reusable_transcript_gz(seat.agent, seat.root, "2026-10-14T23:00:00Z", "u1",
                                      _pointer_for(src)["sha256"]) is not None  # the .2.gz now matches
    assert sc._gz_sha_matches(stale, _pointer_for(src)["sha256"]) is False


def test_session_end_archives_at_compresslevel_6(tmp_path, monkeypatch):
    seen = {}
    real = chronicle.archive_native_transcript

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)
    monkeypatch.setattr(chronicle, "archive_native_transcript", spy)
    _run_claude(tmp_path)
    assert seen["compresslevel"] == 6


def test_hermes_transcript_is_not_archived(tmp_path):
    seat = Seat(tmp_path, harness="hermes")
    expected, _ = _transcripts(seat)["hermes"]
    seat.runtime()
    seat.write_handoff()
    seat.live_log("bye")
    seat.run("death")
    assert _gzs(seat) == []
    assert seat.meta()["native_transcript"]["path"] == str(expected)
    assert "native_transcript_error" not in seat.meta()


def test_every_controller_outcome_cause_is_an_accepted_cause():
    from tools import runtime_controller

    missing = set(runtime_controller._SESSION_END_CAUSE_BY_OUTCOME.values()) - set(sc.CAUSES)
    assert not missing, missing


def test_cli_accepts_authentication_loss_swap():
    ns = sc.build_parser().parse_args(["session-end", "--cause", "authentication-loss-swap"])
    assert ns.cause == "authentication-loss-swap"


def test_committed_fixtures_archive_through_session_end(tmp_path):
    seat = Seat(tmp_path)
    claude = seat.home / ".claude" / "projects" / "-home-x-pantheon-config" / "a0c2ef6a-c90e-443b-af6c-ba8b32733ecb.jsonl"
    claude.parent.mkdir(parents=True)
    shutil.copy(FIXTURES / "claude_top_level.jsonl", claude)
    seat.runtime()
    seat.write_handoff()
    seat.live_log("bye")
    seat.run("death")
    pointer = seat.meta()["native_transcript"]
    assert gzip.decompress(Path(pointer["path"]).read_bytes()) == (FIXTURES / "claude_top_level.jsonl").read_bytes()
    assert pointer["session_id"] == "a0c2ef6a-c90e-443b-af6c-ba8b32733ecb"

    cseat = Seat(tmp_path / "c", harness="codex")
    rollout = _codex_day_dir(cseat.home) / "rollout-2026-10-14T20-00-02-01a1980f-76c5-7c70-b712-3a11c93dd180.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copy(FIXTURES / "codex_rollout_top_level.jsonl", rollout)
    cseat.runtime()
    cseat.write_handoff()
    cseat.live_log("bye")
    cseat.run("death")
    cp = cseat.meta()["native_transcript"]
    assert gzip.decompress(Path(cp["path"]).read_bytes()) == (FIXTURES / "codex_rollout_top_level.jsonl").read_bytes()
    assert cp["source_path"] == str(rollout)


# ------------------------------------------------------------------ T-6118 PR-2: resolvers
def _rollout(home: Path, name: str, fixture: str | None = None, *, days_ago: int = 0, age_days: float = 0.0) -> Path:
    day = datetime.now(timezone.utc) - timedelta(days=days_ago)
    path = home / ".codex" / "sessions" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if fixture:
        shutil.copy(FIXTURES / fixture, path)
    else:
        path.write_text('{"turn": 1}\n')
    when = time.time() - age_days * 86400
    os.utime(path, (when, when))
    return path


def _codex(tmp_path):
    return Seat(tmp_path, harness="codex"), harness_integration.integration_for("codex")


def test_codex_resolver_with_a_session_id_picks_that_threads_rollout_not_the_newest(tmp_path):
    seat, codex = _codex(tmp_path)
    mine = _rollout(seat.home, "rollout-2026-11-04T01-00-00-aaaa-1111.jsonl", age_days=0.5)
    _rollout(seat.home, "rollout-2026-11-04T02-00-00-bbbb-2222.jsonl")  # newer, but another thread
    found = codex.session_transcript(account_home=seat.home, started_at=None, session_id="aaaa-1111")
    assert (found.path, found.session_id, found.candidates) == (mine, "aaaa-1111", 1)


def test_codex_resolver_drops_a_sidechain_or_exec_rollout_even_when_hinted(tmp_path):
    seat, codex = _codex(tmp_path)
    side = _rollout(seat.home, "rollout-2026-11-04T01-00-00-side-1.jsonl", "codex_rollout_sidechain.jsonl")
    execd = _rollout(seat.home, "rollout-2026-11-04T01-00-00-exec-1.jsonl", "codex_rollout_exec.jsonl")
    top = _rollout(seat.home, "rollout-2026-11-04T01-00-00-top-1.jsonl", "codex_rollout_top_level.jsonl", age_days=1)
    for sid in ("side-1", "exec-1"):
        found = codex.session_transcript(account_home=seat.home, started_at=None, session_id=sid)
        assert found.path == top, (sid, found.path)  # the hint matched nothing usable: newest top-level
    assert side.exists() and execd.exists()


def test_codex_fallback_excludes_sidechain_and_exec_even_when_newest(tmp_path):
    seat, codex = _codex(tmp_path)
    _rollout(seat.home, "rollout-2026-11-04T03-00-00-side-1.jsonl", "codex_rollout_sidechain.jsonl")
    _rollout(seat.home, "rollout-2026-11-04T03-00-00-exec-1.jsonl", "codex_rollout_exec.jsonl")
    top = _rollout(seat.home, "rollout-2026-11-04T01-00-00-top-1.jsonl", "codex_rollout_top_level.jsonl", age_days=0.2)
    found = codex.session_transcript(account_home=seat.home, started_at=None)
    assert (found.path, found.candidates) == (top, 1)


def test_codex_fallback_handles_a_missing_started_at_and_bounds_the_window(tmp_path):
    seat, codex = _codex(tmp_path)
    assert codex.session_transcript(account_home=seat.home, started_at=None) is None  # no sessions dir at all
    _rollout(seat.home, "rollout-2026-10-01T01-00-00-old-1.jsonl", days_ago=13, age_days=13)
    assert codex.session_transcript(account_home=seat.home, started_at=None) is None  # older than 12 days
    recent = _rollout(seat.home, "rollout-2026-11-01T01-00-00-new-1.jsonl", days_ago=3, age_days=3)
    ancient_start = datetime.now(timezone.utc) - timedelta(days=90)
    assert codex.session_transcript(account_home=seat.home, started_at=ancient_start).path == recent  # clamped
    naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=5)
    assert codex.session_transcript(account_home=seat.home, started_at=naive).path == recent
    late = datetime.now(timezone.utc) - timedelta(days=1)
    assert codex.session_transcript(account_home=seat.home, started_at=late) is None


def test_codex_resolver_ignores_a_session_id_with_glob_characters(tmp_path):
    seat, codex = _codex(tmp_path)
    top = _rollout(seat.home, "rollout-2026-11-04T01-00-00-top-1.jsonl")
    for bad in ("*", "../x", "a/b", "a[bc]"):
        assert codex.session_transcript(account_home=seat.home, started_at=None, session_id=bad).path == top


def test_claude_resolver_excludes_subagent_transcripts(tmp_path):
    seat = Seat(tmp_path)
    cwd = Path("/home/x/pantheon-config")
    proj = seat.home / ".claude" / "projects" / harness_integration.claude_project_slug(cwd)
    main = proj / "sess-1.jsonl"
    sub = proj / "sess-1" / "subagents" / "agent-9.jsonl"
    for path in (main, sub):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
    os.utime(main, (time.time() - 100, time.time() - 100))  # the subagent file is NEWER
    claude = harness_integration.integration_for("claude")
    found = claude.session_transcript(account_home=seat.home, started_at=None, cwd=cwd, session_id="ignored")
    assert (found.path, found.candidates) == (main, 1)


def test_transcript_pointer_passes_session_id_only_when_known(tmp_path):
    seen = []

    class Fake:
        def session_transcript(self, **kw):
            seen.append(kw)
            return None

    with harness_integration.registered_integration(type("F", (Fake,), {"name": "fake"})()):
        sc._transcript_pointer("fake", tmp_path, None, None)
        sc._transcript_pointer("fake", tmp_path, None, None, "sid-1")
    assert "session_id" not in seen[0] and seen[1]["session_id"] == "sid-1"


def test_session_end_request_session_id_reaches_the_codex_resolver(tmp_path):
    seat = Seat(tmp_path, harness="codex")
    mine = _rollout(seat.home, "rollout-2026-11-04T01-00-00-pinned-1.jsonl", age_days=0.5)
    _rollout(seat.home, "rollout-2026-11-04T02-00-00-other-2.jsonl")
    seat.runtime()
    seat.write_handoff()
    seat.live_log("bye")
    seat.run("death", harness="codex", session_id="pinned-1")
    assert seat.meta()["native_transcript"]["source_path"] == str(mine)


# ------------------------------------------------------------------ T-6118 PR-4 (a): synthesized summary + hollow records
START_DT = sc._parse_time(STARTED)


def _fixture_transcript(seat: Seat, kind: str) -> Path:
    if kind == "claude":
        dest = seat.home / ".claude" / "projects" / "-home-x-pantheon-config" / "1111-claude.jsonl"
        src = FIXTURES / "summary_claude.jsonl"
    else:
        dest = _codex_day_dir(seat.home) / "rollout-2026-10-14T20-00-02-2222-codex.jsonl"
        src = FIXTURES / "summary_codex.jsonl"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return dest


def _summary_seat(tmp_path, kind="claude", **kw):
    seat = Seat(tmp_path, harness=kind)
    src = _fixture_transcript(seat, kind)
    seat.runtime()
    seat.write_handoff()
    seat.live_log("bye")
    out = seat.run("death", **kw)
    return seat, src, out


def _summaries(seat: Seat) -> list[Path]:
    return seat.files(".summary.md")


def _front_matter(text: str) -> dict:
    head = text.split("---\n")[1]
    return dict(line.split(": ", 1) for line in head.splitlines())


@pytest.mark.parametrize("kind,prompt,answer", [
    ("claude", "synthetic prompt two: ship it", "synthetic final answer: shipped"),
    ("codex", "synthetic codex prompt: rotate the thread", "synthetic codex answer: thread rotated"),
])
def test_stale_handoff_with_a_transcript_gets_a_synthesized_summary_beside_the_untouched_md(tmp_path, kind, prompt, answer):
    seat, src, out = _summary_seat(tmp_path, kind, handoff_fresh=False)
    assert out["results"][0]["status"] == "archived", out
    (summary,) = _summaries(seat)
    (md,) = [p for p in seat.files(".md") if not p.name.endswith(".summary.md")]
    assert md.read_bytes() == seat.handoff.read_bytes()  # the HANDOFF .md is never replaced
    assert summary.name == md.name[: -len(".md")] + ".summary.md"
    text = summary.read_text()
    fm = _front_matter(text)
    assert fm["summary_source"] == "synthesized" and fm["synthesizer"] == "session_chronicle/v1"
    assert fm["persona"] == "specialist" and fm["transcript_source"] == src.name
    assert fm["transcript_sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    assert fm["session_started_at"] == sc._iso(START_DT) and fm["ended_at"] == sc._iso(NOW)
    assert prompt in text and answer in text
    assert "hunter2secretvalue" not in text  # credential scrub
    for banned in ("SECRET-THINKING", "TOOL-USE-INPUT", "TOOL-RESULT-BODY", "SIDECHAIN-TEXT",
                   "DEVELOPER-INSTRUCTIONS", "environment_context"):
        assert banned not in text
    info = seat.meta()["summary"]
    assert info == {"source": "synthesized", "path": str(summary), "bytes": summary.stat().st_size,
                    "sha256": hashlib.sha256(summary.read_bytes()).hexdigest()}
    assert _mode(summary) == 0o640 and "summary_error" not in seat.meta()
    assert seat.meta()["hollow"] is False


def test_a_fresh_handoff_gets_no_summary(tmp_path):
    seat, _, out = _summary_seat(tmp_path, "claude", handoff_fresh=True)
    assert out["results"][0]["status"] == "archived"
    assert _summaries(seat) == [] and seat.meta()["summary"] is None


def test_a_stale_handoff_without_a_transcript_gets_no_summary(tmp_path):
    seat = Seat(tmp_path)
    seat.runtime()
    seat.write_handoff()
    seat.live_log("bye")
    seat.run("death", handoff_fresh=False)
    assert _summaries(seat) == [] and seat.meta()["summary"] is None


def test_a_swap_paired_record_gets_no_summary(tmp_path):
    seat = Seat(tmp_path)
    _fixture_transcript(seat, "claude")
    seat.runtime()
    seat.write_handoff()
    swap_ts = sc._iso(START_DT + timedelta(minutes=30))
    chronicle.archive_required(seat.agent, "# swap\n", jsonl_uuid="swapuuid", reason="planned-runtime-swap",
                               ts=swap_ts, root=seat.root)
    out = seat.run("death", handoff_fresh=False)
    assert out["results"][0]["status"] == "paired", out
    assert _summaries(seat) == [] and seat.meta()["summary"] is None


def _synth(src: Path, **kw) -> str:
    args = dict(persona="specialist", session_start=START_DT, ended_at=NOW, handoff_mtime=None, sha256=None)
    args.update(kw)
    return sc.synthesize_summary(src, **args)[0]


def test_synthesis_is_deterministic_and_has_no_wall_clock(tmp_path):
    src = _fixture_transcript(Seat(tmp_path), "claude")
    first = _synth(src)
    os.utime(src, (1, 1))
    time.sleep(0.01)
    assert _synth(src) == first and first.encode() == _synth(src).encode()


def test_synthesis_reads_only_the_tail_and_drops_the_partial_first_line(tmp_path):
    src = tmp_path / "big.jsonl"
    old = json.dumps({"type": "user", "message": {"role": "user", "content": "OLD-HEAD-PROMPT"}}) + "\n"
    filler = json.dumps({"type": "assistant", "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "x" * 900}]}}) + "\n"
    tail_user = json.dumps({"type": "user", "message": {"role": "user", "content": "TAIL-PROMPT"}}) + "\n"
    with src.open("w") as fh:
        fh.write(old)
        for _ in range(sc.SUMMARY_TAIL_BYTES // len(filler) + 50):
            fh.write(filler)
        fh.write(tail_user)
    text, tail_bytes = sc.synthesize_summary(src, persona="specialist", session_start=None, ended_at=NOW,
                                             handoff_mtime=None, sha256=None)
    assert tail_bytes <= sc.SUMMARY_TAIL_BYTES
    assert "TAIL-PROMPT" in text and "OLD-HEAD-PROMPT" not in text
    assert _front_matter(text)["tail_bytes"] == str(tail_bytes)
    assert _front_matter(text)["session_started_at"] == "null"


def _long_transcript(path: Path, a_fill: str, u_fill: str) -> Path:
    lines = []
    for i in range(12):
        lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant",
                     "content": [{"type": "text", "text": f"A{i}-" + a_fill * 5000}]}}))
        lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": f"U{i}-" + u_fill * 5000}}))
    path.write_text("\n".join(lines) + "\n")
    return path


def test_synthesis_clips_each_text_and_hard_caps_the_file(tmp_path):
    text, _ = sc.synthesize_summary(_long_transcript(tmp_path / "long.jsonl", "a", "u"), persona="specialist",
                                    session_start=START_DT, ended_at=NOW, handoff_mtime=None, sha256=None)
    assert len(text.encode("utf-8")) <= sc.SUMMARY_MAX_BYTES and text.endswith("[truncated]\n")
    assert "A11-" in text and "A7-" in text and "A6-" not in text  # last N=5 assistant texts
    assert "...[clipped]" in text and "a" * (sc.SUMMARY_TEXT_CLIP + 1) not in text


def test_the_cap_clips_on_a_character_boundary(tmp_path):
    text, _ = sc.synthesize_summary(_long_transcript(tmp_path / "long.jsonl", "\u00e9", "\u00fc"), persona="specialist",
                                    session_start=START_DT, ended_at=NOW, handoff_mtime=None, sha256=None)
    raw = text.encode("utf-8")
    assert len(raw) <= sc.SUMMARY_MAX_BYTES and raw.decode("utf-8") == text and text.endswith("[truncated]\n")


def test_synthesis_without_extractable_text_still_writes_the_marker(tmp_path):
    src = tmp_path / "empty.jsonl"
    src.write_text('{"turn": 1}\nnot json\n')
    text = _synth(src)
    assert _front_matter(text)["summary_source"] == "synthesized"
    assert "(no assistant/user text in transcript tail)" in text


def test_a_synthesis_error_is_fail_open_recorded_and_alerted(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("synth exploded")
    monkeypatch.setattr(sc, "synthesize_summary", boom)
    seat, _, out = _summary_seat(tmp_path, "claude", handoff_fresh=False)
    assert out["results"][0]["status"] == "archived", out
    meta = seat.meta()
    assert meta["summary"] is None and "synth exploded" in meta["summary_error"]
    assert _summaries(seat) == [] and len(seat.alerts) == 1


def test_summary_write_group_denied_retries_ungrouped(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "chown", lambda *a, **k: (_ for _ in ()).throw(PermissionError(1, "EPERM")))
    monkeypatch.setattr(os, "fchown", lambda *a, **k: (_ for _ in ()).throw(PermissionError(1, "EPERM")))
    seat, _, out = _summary_seat(tmp_path, "claude", handoff_fresh=False)
    assert out["results"][0]["status"] == "archived", out
    assert len(_summaries(seat)) == 1 and seat.meta()["summary"]["source"] == "synthesized"
    assert "group_error" in seat.meta()


# ---- hollow records
def _hollow_seat(tmp_path, **seat_kw):
    seat = Seat(tmp_path, **seat_kw)
    seat.runtime()
    seat.write_handoff()
    return seat


def _hollow_run(seat: Seat, seconds: float, **kw):
    kw.setdefault("handoff_fresh", False)
    kw.setdefault("stage", False)
    return seat.run("start-reconcile", now=START_DT + timedelta(seconds=seconds), **kw)


def test_a_hollow_end_writes_session_json_only(tmp_path):
    seat = _hollow_seat(tmp_path)
    out = _hollow_run(seat, 60)
    res = out["results"][0]
    assert res["status"] == "hollow" and res["record"] is None, out
    assert seat.files(".md") == [] and seat.index() == [] and not (seat.root / seat.agent / "index.md").exists()
    meta = seat.meta()
    assert meta["hollow"] is True and meta["transcript"] == "absent" and meta["transcript_absent_reason"]
    assert meta["summary"] is None
    assert meta["export"]["status"] == "skipped" and meta["export"]["detail"] == "hollow" and meta["export"]["files"] == []
    (claim,) = [json.loads(p.read_text()) for p in (seat.root / seat.agent / ".claims").glob("*.json")]
    assert claim["state"] == "done" and claim["hollow"] is True and claim["record"] is None
    assert res["export"] == "skipped" and "export_session_json" not in res and seat.alerts == []


def test_a_hollow_end_exports_nothing(tmp_path):
    seat = _hollow_seat(tmp_path)
    root = tmp_path / "export"
    (root / "specialist").mkdir(parents=True)
    os.chown(root / "specialist", -1, os.getegid())
    _hollow_run(seat, 60, export_root=root)
    assert [p for p in root.rglob("*") if p.is_file()] == []


def test_a_repeat_call_after_a_hollow_end_is_already_archived(tmp_path):
    seat = _hollow_seat(tmp_path)
    _hollow_run(seat, 60)
    again = _hollow_run(seat, 70)
    assert again["results"][0]["status"] == "already-archived", again
    assert len(seat.files(".session.json")) == 1


def test_coverage_is_ok_after_a_hollow_end(tmp_path):
    seat = _hollow_seat(tmp_path)
    _hollow_run(seat, 60)
    cov = sc.coverage("specialist", state_root=seat.state, root=seat.root, now=START_DT + timedelta(seconds=90),
                      restart=(START_DT, "runtime.json"))
    assert cov["status"] == "OK", cov


@pytest.mark.parametrize("seconds,hollow", [(119, True), (119.9, True), (120, False), (300, False)])
def test_hollow_lifetime_boundary(tmp_path, seconds, hollow):
    seat = _hollow_seat(tmp_path)
    res = _hollow_run(seat, seconds)["results"][0]
    assert (res["status"] == "hollow") is hollow, res
    assert seat.meta()["hollow"] is hollow
    assert len(seat.files(".md")) == (0 if hollow else 1)


def test_an_unknown_session_start_is_not_hollow(tmp_path):
    seat = Seat(tmp_path)
    seat.write_handoff()  # no runtime.json: the start is unknowable
    res = seat.run("start-reconcile", handoff_fresh=False, stage=False, certain=True, rt_pinned=True,
                   rt_started_at=None)["results"][0]
    assert res["status"] == "archived", res
    assert seat.meta()["hollow"] is False and len(seat.files(".md")) == 1


def test_a_pane_log_makes_it_not_hollow(tmp_path):
    seat = _hollow_seat(tmp_path)
    seat.live_log("bye", stamp="2026-10-14 20:00:01")
    res = _hollow_run(seat, 60, stage=True)["results"][0]
    assert res["status"] == "archived" and seat.meta()["hollow"] is False


def test_a_fresh_handoff_makes_it_not_hollow_and_keeps_the_pr2_handoff_export(tmp_path):
    seat = _hollow_seat(tmp_path)
    res = _hollow_run(seat, 60, handoff_fresh=True)["results"][0]
    assert res["status"] == "archived" and seat.meta()["hollow"] is False
    assert len(seat.files(".md")) == 1 and seat.meta()["summary"] is None


def test_a_transcript_makes_it_not_hollow(tmp_path):
    seat = _hollow_seat(tmp_path)
    _fixture_transcript(seat, "claude")
    res = _hollow_run(seat, 60)["results"][0]
    assert res["status"] == "archived" and seat.meta()["hollow"] is False
    assert len(_summaries(seat)) == 1


def test_a_caller_supplied_native_transcript_is_never_hollow(tmp_path):
    seat = _hollow_seat(tmp_path)
    res = _hollow_run(seat, 60, harness="codex", native_transcript=dict(ROTATION_POINTER))["results"][0]
    assert res["status"] == "archived", res


# ------------------------------------------------------------------ transcript-coverage (T-6118 PR-4b)
import contextlib  # noqa: E402
import io  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402

_TC_NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)


class TranscriptCoverage(unittest.TestCase):
    """transcript_coverage + the transcript-coverage CLI over tempdir roots only."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.root = tmp / "chronicle"
        self.export = tmp / "export"
        self.home = tmp / "home"
        for d in (self.root, self.export, self.home):
            d.mkdir()
        self.agent = "specialist"
        self.base = json.loads((REPO / "tools/fixtures/chronicle/session_record.session.json").read_text())
        self.n = 0

    def _record(self, ended_at, *, gz="present", receipt=None, hollow=None, counter=None,
                reason=None, drop_hollow=False):
        """Write one record. gz: present|missing|none. receipt: None|ok|mismatch."""
        self.n += 1
        date = str(ended_at)[:10] if ended_at and str(ended_at)[:4].isdigit() else "2026-11-03"
        stem = f"{date}T10-00-{self.n:02d}Z-uuid{self.n}"
        d = self.root / self.agent / date
        d.mkdir(parents=True, exist_ok=True)
        meta = dict(self.base, agent=self.agent, persona=self.agent, ended_at=ended_at)
        if hollow is not None:
            meta["hollow"] = hollow
        gzname = f"{stem}.transcript.jsonl.gz"
        if gz == "none":
            meta["native_transcript"] = None
            meta["transcript"] = "absent"
            if reason:
                meta["transcript_absent_reason"] = reason
        else:
            meta["native_transcript"] = {"path": str(d / gzname), "bytes": 3, "sha256": "x"}
            meta["transcript"] = "archived"
            if gz == "present":
                (d / gzname).write_bytes(b"gz")
        if receipt:
            rd = self.export / ".ingested" / self.agent / date
            rd.mkdir(parents=True, exist_ok=True)
            file = f"{self.agent}/{date}/{gzname}" if receipt == "ok" else f"{self.agent}/{date}/other.gz"
            (rd / f"{gzname}.receipt.json").write_text(json.dumps(
                {"schema": "chronicle-ingest-receipt/v1", "file": file}))
        name = f"{stem}.session.json" + (f".{counter}" if counter else "")
        (d / name).write_text(json.dumps(meta))
        return d / name

    def _build_mixed(self):
        self._record("2026-11-03T10:00:00Z")                              # gz present -> counts
        self._record("2026-11-03T11:00:00Z", counter=1)                  # gz present, .N record form
        self._record("2026-11-02T10:00:00Z", gz="missing", receipt="ok")  # pruned + receipt -> counts
        self._record("2026-11-02T11:00:00Z", gz="missing", receipt="mismatch")  # mismatch -> no
        self._record("2026-11-02T12:00:00Z", gz="none", reason="harness-not-archived")  # no transcript
        self._record("2026-11-01T10:00:00Z", gz="none", hollow=True)      # hollow -> excluded
        self._record("2026-11-01T11:00:00Z", hollow=True)                 # hollow w/ gz -> excluded
        self._record("2026-10-01T10:00:00Z", gz="none")                   # outside window
        self._record("2026-11-03T12:00:00Z", hollow=False)                # explicit non-hollow, gz present
        self._record("not-a-date")                                        # unparseable ended_at

    def _cov(self, **kw):
        import tools.session_chronicle as mod
        return mod.transcript_coverage(self.agent, root=self.root, export_root=self.export,
                                       account_home=self.home, now=_TC_NOW, **kw)

    def test_mixed_counts_and_ratio(self):
        self._build_mixed()
        out = self._cov()
        self.assertEqual(out["records"], 8)       # in-window, parseable (the old and unparseable ones drop)
        self.assertEqual(out["unparseable"], 1)
        self.assertEqual(out["hollow"], 2)
        self.assertEqual(out["eligible"], 6)      # gz, gz.N, receipted, mismatch, none, explicit non-hollow
        self.assertEqual(out["with_transcript"], 4)
        self.assertEqual(out["receipted_only"], 1)
        self.assertEqual(out["ratio"], 0.6667)
        self.assertEqual(out["status"], "FAIL")
        self.assertEqual(out["absent_reasons"].get("harness-not-archived"), 1)
        self.assertEqual(out["days"], 7)
        self.assertEqual(out["threshold"], 0.9)

    def test_old_record_without_hollow_field_counts_as_non_hollow(self):
        p = self._record("2026-11-03T10:00:00Z", gz="none")
        meta = json.loads(p.read_text())
        self.assertNotIn("hollow", meta)
        out = self._cov()
        self.assertEqual((out["eligible"], out["with_transcript"], out["hollow"]), (1, 0, 0))

    def test_empty_and_hollow_only_is_ok_with_null_ratio(self):
        out = self._cov()
        self.assertEqual((out["status"], out["ratio"], out["eligible"]), ("OK", None, 0))
        self.assertEqual(out["detail"], "no non-hollow session ends in window")
        self._record("2026-11-03T10:00:00Z", gz="none", hollow=True)
        out = self._cov()
        self.assertEqual((out["status"], out["ratio"], out["eligible"], out["hollow"]), ("OK", None, 0, 1))

    def test_read_only(self):
        self._build_mixed()
        def snap():
            return sorted((str(p), p.stat().st_mtime_ns) for r in (self.root, self.export) for p in r.rglob("*"))
        before = snap()
        self._cov()
        self.assertEqual(before, snap())

    def _cli(self, *argv):
        import tools.session_chronicle as mod
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod.main(["transcript-coverage", "--persona", self.agent, *argv])
        self.assertEqual(rc, 0)
        return buf.getvalue().strip()

    def _cli_env(self, *argv):
        env = {"PANTHEON_CHRONICLE_ROOT": str(self.root), "PANTHEON_CHRONICLE_EXPORT_ROOT": str(self.export)}
        with mock_patch_env(env):
            return self._cli("--now", _TC_NOW.isoformat(), "--account-home", str(self.home), *argv)

    def _ten(self, with_gz):
        """Ten eligible records, `with_gz` of them with a transcript."""
        for i in range(10):
            self._record(f"2026-11-03T{i:02d}:00:00Z", gz="present" if i < with_gz else "none")

    def test_cli_bool_at_threshold_true_below_false_hollow_only_true(self):
        self._ten(9)  # exactly 0.9
        self.assertEqual(self._cli_env("--bool"), "true")
        self.assertEqual(json.loads(self._cli_env("--json"))["ratio"], 0.9)
        self._record("2026-11-03T20:00:00Z", gz="none")  # 9/11 = 0.8182
        self.assertEqual(self._cli_env("--bool"), "false")
        out = json.loads(self._cli_env())
        self.assertEqual(out["status"], "FAIL")
        self.assertEqual(out["ratio"], 0.8182)

    def test_cli_hollow_only_is_true(self):
        self._record("2026-11-03T10:00:00Z", gz="none", hollow=True)
        self.assertEqual(self._cli_env("--bool"), "true")

    def test_cli_error_is_warn_and_false_for_bool(self):
        import tools.session_chronicle as mod
        with mock_patch_fn(mod, "transcript_coverage", boom):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(mod.main(["transcript-coverage", "--persona", "specialist"]), 0)
            self.assertEqual(json.loads(buf.getvalue())["status"], "WARN")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(mod.main(["transcript-coverage", "--persona", "specialist", "--bool"]), 0)
            self.assertEqual(buf.getvalue().strip(), "false")

    def test_axis_check_ref_runs_against_temp_roots(self):
        import yaml
        axes = yaml.safe_load((REPO / "sop/standard/seat-parity/axes.yaml").read_text())["axes"]
        axis = next(a for a in axes if a["axis_id"] == "transcript-coverage-7d")
        self.assertEqual((axis["tier"], axis["check_method"], axis["gap_history"]), ("SOFT", "canary", ["T-6118"]))
        prev = next(a for a in axes if a["axis_id"] == "chronicle-archive-after-last-restart")
        self.assertEqual(axes.index(axis), axes.index(prev) + 1)
        ref = axis["check_ref"].replace("/home/<account>/pantheon-config", str(REPO)).replace(
            "<persona>", self.agent)
        self.assertTrue(ref.rstrip().endswith("transcript-coverage --persona specialist --days 7 --bool"))
        env = dict(os.environ, PANTHEON_CHRONICLE_ROOT=str(self.root),
                   PANTHEON_CHRONICLE_EXPORT_ROOT=str(self.export), HOME=str(self.home))
        run = lambda: subprocess.run(["bash", "-c", ref], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(run().stdout.strip(), "true")        # no records: OK
        recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
        for _ in range(2):
            self._record(recent, gz="none")
        r = run()
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "false"))


def boom(*a, **k):
    raise RuntimeError("boom")


@contextlib.contextmanager
def mock_patch_fn(mod, name, fn):
    orig = getattr(mod, name)
    setattr(mod, name, fn)
    try:
        yield
    finally:
        setattr(mod, name, orig)


@contextlib.contextmanager
def mock_patch_env(env):
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ------------------------------------------------------------------ T-6302: abrupt death, partial records, backfill
def _grow(path: Path, line: str = '{"turn": 2}\n') -> None:
    with path.open("a") as handle:
        handle.write(line)


def _claude_one(seat: Seat) -> Path:
    path, _sid = _transcripts(seat)["claude"]
    return path


def _stamp(path: Path, when: datetime) -> None:
    os.utime(path, (when.timestamp(), when.timestamp()))


def test_abrupt_death_then_boot_archives_the_prior_session(tmp_path):
    """No session-end ever ran (hand-run host reboot). The controller's start after boot
    archives the prior session and coverage reads OK."""
    seat = Seat(tmp_path)
    _claude_one(seat)
    seat.runtime()
    seat.write_handoff()
    seat.live_log("last words")
    boot = NOW + timedelta(minutes=30)
    out = seat.run("start", now=boot)  # pids gone, no tmux target: start-reconcile
    assert out["results"][0]["status"] == "archived", out
    (entry,) = seat.index()
    assert entry["reason"] == "session-end/start-reconcile"
    assert _coverage_at(seat, boot - timedelta(seconds=5), boot)["status"] == "OK"


def _coverage_at(seat: Seat, restart: datetime, now: datetime):
    return sc.coverage(seat.persona, state_root=seat.state, root=seat.root, now=now, restart=(restart, "test"))


def test_partial_record_is_extended_not_skipped_by_the_next_start(tmp_path):
    """A fleet-reboot archive was written during a handoff; the transcript kept growing until
    the host died. The start after boot must not read the old done claim as 'covered'."""
    seat = Seat(tmp_path)
    transcript = _claude_one(seat)
    seat.runtime()
    seat.write_handoff()
    seat.live_log("early")
    first = seat.run("fleet-reboot", stage=False, defer_pane_log=True)
    assert first["results"][0]["status"] == "archived", first
    _grow(transcript)
    boot = NOW + timedelta(minutes=30)
    out = seat.run("start", now=boot)
    assert out["results"][0]["status"] == "archived", out
    old, new = seat.index()
    assert old["uuid"] == new["uuid"] == "1111-claude"
    assert new["reason"] == "session-end/start-reconcile" and new["ts"] > old["ts"]
    assert len(seat.files(".session.json")) == 2
    newest = max(seat.files(".session.json"))
    meta = json.loads(newest.read_text())
    assert meta["extends"]["stem_ts"] == old["ts"]
    assert meta["native_transcript"]["source_bytes"] == transcript.stat().st_size
    assert _coverage_at(seat, boot - timedelta(seconds=5), boot)["status"] == "OK"
    again = seat.run("start", now=boot + timedelta(seconds=5))  # no further growth: no third record
    assert again["results"][0]["status"] == "already-archived", again
    assert len(seat.index()) == 2


def test_graceful_restart_after_a_handoff_archive_with_an_unchanged_transcript_writes_one_record(tmp_path):
    seat = Seat(tmp_path)
    _claude_one(seat)
    seat.runtime()
    seat.write_handoff()
    seat.live_log("bye")
    first = seat.run("fleet-reboot", stage=False, defer_pane_log=True)
    second = seat.run("graceful-restart", now=NOW + timedelta(seconds=30))
    third = seat.run("start", now=NOW + timedelta(seconds=60))
    assert first["results"][0]["status"] == "archived"
    assert {r["results"][0]["status"] for r in (second, third)} <= {"already-archived", "attached"}
    assert len(seat.index()) == 1
    assert len(seat.files(".session.json")) == 1


def test_hollow_claim_does_not_trigger_an_extension_without_a_transcript(tmp_path):
    seat = Seat(tmp_path)
    seat.runtime(started=_iso_ago(NOW, 30))
    seat.write_handoff(mtime=NOW - timedelta(days=1))
    first = seat.run("start-reconcile")
    second = seat.run("start", now=NOW + timedelta(seconds=5))
    assert first["results"][0]["status"] == "hollow", first
    assert second["results"][0]["status"] == "already-archived", second


def _iso_ago(now: datetime, seconds: int) -> str:
    return (now - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


LIVE_STARTED = "2026-10-14T22:33:25.000000Z"
DEAD_SID = "ebdee140-97d9-4d43-a49f-f4bab9714669"


def _dead_session_fixture(tmp_path):
    """Dead session archived once at 22:18 (partial), written until 22:28, host rebooted, live record at 22:33."""
    seat = Seat(tmp_path)
    path = seat.home / ".claude" / "projects" / "-home-x" / f"{DEAD_SID}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"turn": 1}\n')
    seat.runtime(started=STARTED)
    seat.write_handoff()
    early = datetime(2026, 10, 14, 22, 18, 9, tzinfo=timezone.utc)
    first = seat.run("fleet-reboot", now=early, stage=False, defer_pane_log=True, session_id=DEAD_SID)
    assert first["results"][0]["status"] == "archived", first
    _grow(path)
    _stamp(path, datetime(2026, 10, 14, 22, 28, 0, tzinfo=timezone.utc))
    seat.runtime(started=LIVE_STARTED)
    return seat, path


def _backfill(seat: Seat, **kw):
    opts = dict(dry_run=False, min_quiet_seconds=300.0, now=NOW, route_fn=lambda *a: seat.alerts.append(a))
    # Default to a fake /proc whose boot predates every fixture: the real host's btime must not leak in.
    opts["proc_root"] = kw.pop("proc_root") if "proc_root" in kw else _fake_proc(seat.root.parent / "fakeproc",
                                                                                 datetime(2026, 1, 1, tzinfo=timezone.utc))
    req_kw = dict(stage=False, harness="claude", rt_pinned=True, rt_started_at=STARTED, session_id=DEAD_SID)
    for name in ("dry_run", "min_quiet_seconds", "now"):
        if name in kw:
            opts[name] = kw.pop(name)
    req_kw.update(kw)
    return sc.backfill_dead_session(seat.request("fleet-reboot", **req_kw), **opts)


def test_backfill_archives_a_dead_session_as_an_extension_and_is_idempotent(tmp_path):
    seat, path = _dead_session_fixture(tmp_path)
    live_files = {p: p.read_bytes() for p in (seat.state / seat.persona / "runtime-controller").glob("*")}
    dry = _backfill(seat, dry_run=True)
    assert dry["status"] == "dry-run" and dry["would"] == "extend", dry
    assert len(seat.index()) == 1
    out = _backfill(seat)
    assert out["status"] == "archived", out
    old, new = seat.index()
    assert old["uuid"] == new["uuid"] == DEAD_SID and new["ts"] > old["ts"]
    meta = json.loads(max(seat.files(".session.json")).read_text())
    assert meta["extends"]["stem_ts"] == old["ts"] and meta["backfill"] is True
    assert meta["handoff_fresh"] is False
    assert meta["native_transcript"]["source_bytes"] == path.stat().st_size
    assert meta["pane_log"] == "absent"
    assert _backfill(seat)["status"] == "already-archived"
    assert len(seat.index()) == 2
    assert live_files == {p: p.read_bytes() for p in (seat.state / seat.persona / "runtime-controller").glob("*")}


def test_backfill_with_no_prior_record_writes_one_new_record(tmp_path):
    seat, _path = _dead_session_fixture(tmp_path)
    for claim in (seat.root / seat.agent / ".claims").glob("*.json"):
        claim.unlink()
    for entry in (seat.root / seat.agent).glob("*/*"):
        entry.unlink()
    (seat.root / seat.agent / "INDEX.jsonl").unlink(missing_ok=True)
    out = _backfill(seat)
    assert out["status"] == "archived", out
    assert "extends" not in json.loads(max(seat.files(".session.json")).read_text())


def test_backfill_refuses_the_live_session_by_runtime_identity(tmp_path):
    seat, _path = _dead_session_fixture(tmp_path)
    out = _backfill(seat, rt_started_at=LIVE_STARTED)
    assert out["status"] == "refused" and out["reason"] == "live-session", out
    assert len(seat.index()) == 1


def test_backfill_refuses_a_transcript_written_since_the_live_session_started(tmp_path):
    seat, path = _dead_session_fixture(tmp_path)
    _stamp(path, datetime(2026, 10, 14, 22, 40, 0, tzinfo=timezone.utc))  # after the live start
    out = _backfill(seat)
    assert out["status"] == "refused" and out["reason"] == "live-session", out
    assert len(seat.index()) == 1


def test_backfill_refuses_a_transcript_still_being_written(tmp_path):
    seat, path = _dead_session_fixture(tmp_path)
    seat.runtime(started="2026-10-14T20:05:00.000000Z")  # live start older than the transcript's last write
    _stamp(path, NOW - timedelta(seconds=60))
    out = _backfill(seat)
    assert out["status"] == "refused" and out["reason"] in ("still-written", "live-session"), out
    seat.runtime(started="2026-10-14T23:30:00.000000Z")  # live start after it: only the quiet rule remains
    out = _backfill(seat)
    assert out["status"] == "refused" and out["reason"] == "still-written", out
    assert len(seat.index()) == 1


def test_backfill_fails_closed_when_liveness_cannot_be_determined(tmp_path):
    seat, _path = _dead_session_fixture(tmp_path)
    (seat.state / seat.persona / "runtime-controller" / "runtime.json").unlink()  # no record, no pane
    out = _backfill(seat)
    assert out["status"] == "refused" and out["reason"] == "liveness-unknown", out
    assert len(seat.index()) == 1


def test_backfill_refuses_an_unknown_or_unsafe_session_id(tmp_path):
    seat, _path = _dead_session_fixture(tmp_path)
    assert _backfill(seat, session_id="nope-0000")["reason"] == "transcript-not-found"
    assert _backfill(seat, session_id="../../etc/passwd")["reason"] == "bad-session-id"
    assert _backfill(seat, session_id=None)["reason"] == "bad-session-id"
    assert _backfill(seat, rt_started_at=None)["reason"] == "rt-started-at-required"
    assert len(seat.index()) == 1


def test_backfill_cli_flag_is_wired(tmp_path, capsys, monkeypatch):
    seat, _path = _dead_session_fixture(tmp_path)
    monkeypatch.setattr(chronicle, "chronicle_root", lambda: seat.root)  # never the real chronicle
    argv = ["session-end", "--backfill-dead-session", "--dry-run", "--persona", seat.persona,
            "--cause", "fleet-reboot", "--account-home", str(seat.home), "--harness", "claude",
            "--persona-state-dir", str(seat.state), "--session-id", DEAD_SID, "--rt-started-at", STARTED]
    assert sc.main(argv) in (0, 1)
    out = json.loads(capsys.readouterr().out)
    assert out["status"] in ("dry-run", "refused"), out


# ------------------------------------------------------------------ T-6302 rebound
def test_a_hollow_claim_is_never_extended_when_a_transcript_appears_later(tmp_path):
    seat = Seat(tmp_path)
    seat.runtime(started=_iso_ago(NOW, 30))
    seat.write_handoff(mtime=NOW - timedelta(days=1))
    first = seat.run("start-reconcile")
    assert first["results"][0]["status"] == "hollow", first
    _claude_one(seat)  # a transcript the resolver would pick: here the successor's, in real life
    second = seat.run("start", now=NOW + timedelta(seconds=5))
    assert second["results"][0]["status"] == "already-archived", second
    assert seat.index() == [] and "extends" not in second["results"][0]


def test_only_the_same_transcript_grown_extends_a_different_bigger_file_does_not(tmp_path):
    seat = Seat(tmp_path)
    transcript = _claude_one(seat)
    seat.runtime()
    seat.write_handoff()
    assert seat.run("fleet-reboot", stage=False)["results"][0]["status"] == "archived"
    other = transcript.parent / "9999-rotated.jsonl"  # e.g. a thread rotation
    other.write_text('{"turn": 1}\n' * 50)
    assert other.stat().st_size > transcript.stat().st_size
    rotated = seat.run("start", now=NOW + timedelta(seconds=5), transcript_path=other)
    assert rotated["results"][0]["status"] == "already-archived", rotated
    assert len(seat.index()) == 1
    _grow(transcript)
    # #1758 CI: the resolver picks the newest top-level transcript by mtime. A fast runner's
    # coarse filesystem clock can give `other` and the grown transcript the SAME mtime, and
    # the directory listing order then decides. Pin the order so the grown file is newest.
    newest = transcript.stat().st_mtime
    os.utime(other, (newest - 60, newest - 60))
    grown = seat.run("start", now=NOW + timedelta(seconds=10))
    assert grown["results"][0]["status"] == "archived" and "extends" in grown["results"][0], grown
    assert len(seat.index()) == 2


def test_a_claim_written_before_the_extent_fields_still_extends_via_session_json(tmp_path):
    seat = Seat(tmp_path)
    transcript = _claude_one(seat)
    seat.runtime()
    seat.write_handoff()
    seat.live_log("x")
    seat.run("fleet-reboot", stage=False, defer_pane_log=True)
    for path in (seat.root / seat.agent / ".claims").glob("*.json"):
        doc = json.loads(path.read_text())
        for field_name in ("transcript_bytes", "transcript_source", "transcript_session_id", "ended_at"):
            doc.pop(field_name, None)
        path.write_text(json.dumps(doc))
    _grow(transcript)
    out = seat.run("start", now=NOW + timedelta(seconds=5))
    assert out["results"][0]["status"] == "archived" and "extends" in out["results"][0], out


def test_an_extension_settles_the_older_claims_pending_pane_log(tmp_path):
    seat = Seat(tmp_path)
    transcript = _claude_one(seat)
    seat.runtime()
    seat.write_handoff()
    seat.live_log("early")
    seat.run("fleet-reboot", stage=False, defer_pane_log=True)
    claims = seat.root / seat.agent / ".claims"
    (old_claim,) = [json.loads(p.read_text()) for p in claims.glob("*.json")]
    assert old_claim["pane_log"] == "pending"
    _grow(transcript)
    out = seat.run("start", now=NOW + timedelta(minutes=30))
    assert out["results"][0]["status"] == "archived", out
    docs = {json.loads(p.read_text())["key"]: json.loads(p.read_text()) for p in claims.glob("*.json")}
    assert docs[old_claim["key"]]["pane_log"] == "superseded-by-extension"
    assert docs[old_claim["key"]]["superseded_by"]["stem_ts"] == seat.index()[-1]["ts"]
    assert [d["pane_log"] for d in docs.values()].count("pending") == 0


def test_extension_export_supersedes_the_earlier_gz_with_no_orphan(tmp_path):
    seat = Seat(tmp_path)
    transcript = _claude_one(seat)
    seat.runtime()
    seat.write_handoff()
    seat.live_log("early")
    export = tmp_path / "export"
    (export / seat.persona).mkdir(parents=True)
    os.chown(export / seat.persona, -1, os.getegid())  # a provisioned tree
    first = seat.run("fleet-reboot", stage=False, defer_pane_log=True, export_root=export)
    assert first["results"][0]["export"] == "exported", first
    old_meta = json.loads(max(seat.files(".session.json")).read_text())
    old_gz = Path(old_meta["native_transcript"]["path"]).name
    _grow(transcript)
    second = seat.run("start", now=NOW + timedelta(minutes=30), export_root=export)
    assert second["results"][0]["export"] == "exported", second
    new_meta = json.loads(max(seat.files(".session.json")).read_text())
    new_gz = Path(new_meta["native_transcript"]["path"]).name
    assert new_gz != old_gz
    assert [f["file"].split("/")[-1] for f in new_meta["supersedes"]] == [old_gz]
    exported_gz = sorted(p.name for p in export.rglob("*.transcript.jsonl*.gz"))
    assert exported_gz == sorted([old_gz, new_gz])  # the old copy is named superseded, never deleted or doubled


def test_backfill_records_no_handoff_of_the_live_successor(tmp_path):
    seat, _path = _dead_session_fixture(tmp_path)
    seat.write_handoff("# SUCCESSOR handoff\nlive work\n")
    assert _backfill(seat)["status"] == "archived"
    meta = json.loads(max(seat.files(".session.json")).read_text())
    assert meta["handoff_unavailable"] is True and meta["handoff_file"] is None and meta["handoff_fresh"] is False
    newest_md = max(p for p in seat.files(".md") if not p.name.endswith(".summary.md"))
    assert "SUCCESSOR" not in newest_md.read_text() and "unavailable" in newest_md.read_text()


def _runtime_with(seat: Seat, **fields) -> None:
    path = seat.state / seat.persona / "runtime-controller" / "runtime.json"
    doc = json.loads(path.read_text())
    doc.update(fields)
    path.write_text(json.dumps({k: v for k, v in doc.items() if v is not None}))


def test_backfill_refuses_a_session_that_starts_after_the_live_one_even_when_its_ids_differ(tmp_path):
    """Isolates the `rt-started-at after live start` rule: not equal to the live start, transcript quiet
    and older than the live start, session id not live."""
    seat, _path = _dead_session_fixture(tmp_path)
    out = _backfill(seat, rt_started_at="2026-10-14T22:35:00.000000Z")
    assert out["status"] == "refused" and out["reason"] == "live-session", out
    assert "after the live session's start" in out["why"]


def test_backfill_refuses_the_record_start_by_identity_alone(tmp_path):
    """Isolates the identity rule: no pane, so the live start IS the record's start; the check that
    would otherwise also catch it is the strict `after` rule, which equality does not trip."""
    seat, _path = _dead_session_fixture(tmp_path)
    out = _backfill(seat, rt_started_at=LIVE_STARTED)
    assert out["status"] == "refused" and out["reason"] == "live-session", out
    assert "current runtime record" in out["why"]


def test_backfill_uses_the_pane_process_start_as_the_live_start(tmp_path, monkeypatch):
    seat, _path = _dead_session_fixture(tmp_path)
    _runtime_with(seat, started_at=None)  # the record names no start: only the pane can say
    monkeypatch.setattr(sc, "_pane_query", lambda target, sock, fmt: "4242")
    monkeypatch.setattr(sc, "_proc_start_time", lambda pid, *a, **k: datetime(2026, 10, 14, 22, 33, 25, tzinfo=timezone.utc))
    ok = _backfill(seat, dry_run=True)
    assert ok["status"] == "dry-run" and ok["would"] == "extend", ok
    refused = _backfill(seat, rt_started_at="2026-10-14T22:40:00.000000Z")  # after the pane's start
    assert refused["status"] == "refused" and refused["reason"] == "live-session", refused
    monkeypatch.setattr(sc, "_proc_start_time", lambda pid, *a, **k: datetime(2026, 10, 14, 19, 0, 0, tzinfo=timezone.utc))
    early = _backfill(seat)  # the pane started BEFORE the named session: the named session is the live one
    assert early["status"] == "refused" and early["reason"] == "live-session", early


def test_backfill_refuses_the_live_sessions_own_id_from_the_record(tmp_path):
    seat, _path = _dead_session_fixture(tmp_path)
    _runtime_with(seat, session_id=DEAD_SID)  # no writes since the live start: only the id says so
    out = _backfill(seat)
    assert out["status"] == "refused" and out["reason"] == "live-session", out
    assert "session-id" in out["why"] and len(seat.index()) == 1


def test_backfill_refuses_the_live_sessions_own_id_from_the_pane_process(tmp_path, monkeypatch):
    seat, _path = _dead_session_fixture(tmp_path)
    proc = tmp_path / "proc" / "4242"
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"claude\0--resume\0" + DEAD_SID.encode() + b"\0")
    monkeypatch.setattr(sc, "_pane_query", lambda target, sock, fmt: "4242")
    monkeypatch.setattr(sc, "_proc_start_time", lambda pid, *a, **k: datetime(2026, 10, 14, 22, 33, 25, tzinfo=timezone.utc))
    out = _backfill(seat, proc_root=tmp_path / "proc")
    assert out["status"] == "refused" and out["reason"] == "live-session", out
    assert "session-id" in out["why"] and len(seat.index()) == 1


# ------------------------------------------------------------------ T-6306 pre-boot record
BOOT = datetime(2026, 10, 14, 22, 29, 1, tzinfo=timezone.utc)


def _fake_proc(root: Path, boot: datetime | None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if boot is not None:
        (root / "stat").write_text(f"cpu 1 2 3\nbtime {int(boot.timestamp())}\nprocesses 9\n")
    return root


def _preboot_seat(tmp_path):
    """The watchman shape: runtime.json still names the pre-boot runtime (STARTED 20:00), no pane,
    and the dead session's transcript last written 22:28, before the 22:29 boot."""
    seat, path = _dead_session_fixture(tmp_path)
    seat.runtime(started=STARTED)
    _stamp(path, datetime(2026, 10, 14, 22, 28, 0, tzinfo=timezone.utc))
    return seat, path


def test_backfill_archives_a_session_whose_record_predates_the_boot_with_no_pane(tmp_path):
    seat, _path = _preboot_seat(tmp_path)
    out = _backfill(seat, proc_root=_fake_proc(tmp_path / "proc", BOOT))
    assert out["status"] == "archived", out
    assert out["liveness"] == "record-predates-boot"
    assert len(seat.index()) == 2


def test_backfill_refuses_a_preboot_record_with_a_live_pane_as_inconsistent(tmp_path, monkeypatch):
    seat, _path = _preboot_seat(tmp_path)
    proc = _fake_proc(tmp_path / "proc", BOOT)
    monkeypatch.setattr(sc, "_pane_query", lambda target, sock, fmt: "4242")
    monkeypatch.setattr(sc, "_proc_start_time", lambda pid, *a, **k: datetime(2026, 10, 14, 22, 40, 0, tzinfo=timezone.utc))
    out = _backfill(seat, proc_root=proc)
    assert out["status"] == "refused" and out["reason"] == "liveness-inconsistent", out
    assert len(seat.index()) == 1


def test_backfill_still_refuses_a_record_after_the_boot_as_the_live_session(tmp_path):
    seat, _path = _preboot_seat(tmp_path)
    out = _backfill(seat, proc_root=_fake_proc(tmp_path / "proc", datetime(2026, 10, 14, 19, 0, 0, tzinfo=timezone.utc)))
    assert out["status"] == "refused" and out["reason"] == "live-session", out
    assert "current runtime record" in out["why"] and "liveness" not in out
    assert len(seat.index()) == 1


def test_backfill_unreadable_btime_keeps_todays_refusal(tmp_path):
    seat, _path = _preboot_seat(tmp_path)
    out = _backfill(seat, proc_root=_fake_proc(tmp_path / "proc", None))  # no stat file
    assert out["status"] == "refused" and out["reason"] == "live-session", out
    assert "current runtime record" in out["why"]
    (tmp_path / "proc2").mkdir()
    (tmp_path / "proc2" / "stat").write_text("cpu 1\nbtime notanumber\n")
    out = _backfill(seat, proc_root=tmp_path / "proc2")
    assert out["status"] == "refused" and out["reason"] == "live-session", out


def test_backfill_preboot_record_still_requires_a_quiet_transcript(tmp_path):
    # Written 60s ago, just BEFORE a boot 30s ago: the reboot killed it, but it is not yet quiet.
    seat, path = _preboot_seat(tmp_path)
    _stamp(path, NOW - timedelta(seconds=60))
    out = _backfill(seat, proc_root=_fake_proc(tmp_path / "proc", NOW - timedelta(seconds=30)))
    assert out["status"] == "refused" and out["reason"] == "still-written", out
    assert len(seat.index()) == 1


def test_backfill_preboot_record_refuses_a_transcript_written_after_the_boot(tmp_path):
    # Review residual: stale pre-boot record, no readable pane, but the transcript was written
    # after this boot, so a post-boot (possibly live, idle) session owns it. Refuse.
    seat, path = _preboot_seat(tmp_path)
    _stamp(path, BOOT + timedelta(minutes=10))
    out = _backfill(seat, proc_root=_fake_proc(tmp_path / "proc", BOOT))
    assert out["status"] == "refused" and out["reason"] == "live-session", out
    assert "since boot" in out["why"] and "liveness" not in out
    assert len(seat.index()) == 1


def test_boot_time_reads_btime_from_the_given_proc_root(tmp_path):
    assert sc._boot_time(_fake_proc(tmp_path / "p", BOOT)) == BOOT
    assert sc._boot_time(tmp_path / "missing") is None
