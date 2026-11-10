#!/usr/bin/env python3
"""codex_thread_rotate.py: bound a codex seat's thread growth by rotating to a fresh thread (T-6115 D-3).

Intent: a codex thread's rollout grows without bound (cold resume ~20s at 116 MB). The org
record, not the thread, is the seat's durable memory: HANDOFF.md plus the Chronicle transcript
archive (the operator TG 12976/12979). So when a thread is old or big AND the seat is idle, refresh
the HANDOFF, archive the rollout, and move the seat to a new thread.

Run AS the seat account (homes are 0750), from a timer or by hand:

    python3 ~/pantheon-config/tools/codex_thread_rotate.py --persona codex [--dry-run | --rollback]

Sequence (live, persona codex): gate (due AND idle) -> HANDOFF checkpoint -> archive the rollout
-> STOP the delivery writer, then the daemon -> Chronicle session-end (cause thread-rotation, the
archive pointer recorded) -> re-bind (thread.json moves to thread-history/) -> START the daemon,
wait for the NEW thread id -> START the writer -> seed mail. The writer
(codex-seat-delivery.service) re-reads thread.json at every (re)connect and holds the resumed
connection, so it must be down across the re-bind and up only once the new id exists. Any failure
after the stop restores the old binding and restarts both units (exit 5). The rollout is never
deleted. --rollback restores the newest history entry.

Exit: 0 ok (rotated, not-due, busy, dry-run, rolled-back, no-thread); 1 nothing to roll back;
3 checkpoint not acked (no change); 4 refused (watchman live: pending T-6113); 5 failed and the
old binding was restored; 6 failed AND the restore failed (loud: the seat needs a human);
7 rotated but the seed mail failed; 8 the archive group cannot be applied (no change, also in --dry-run).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import chronicle  # noqa: E402
from tools import codex_handoff_checkpoint as chk  # noqa: E402
from tools import codex_delivery as cd  # noqa: E402
from tools import session_chronicle as sc  # noqa: E402

PERSONAS = ("codex", "watchman")
WRITER_UNIT = "codex-seat-delivery.service"
DAEMON_UNIT = "codex-daemon.service"
NEW_THREAD_TIMEOUT_S = 180.0
POLL_S = 2.0
MAILBOX_ROOT = chk.MAILBOX_ROOT
PANE_SESSION = "codex-session"
BUSY_MARK = "Working ("
WATCHMAN_REFUSAL = "watchman live rotation pending T-6113"
HISTORY_DIR = "thread-history"
IN_FLIGHT = (cd.QUEUED, cd.ACCEPTED)
SEED_SUBJECT = "Fresh thread after rotation (T-6115)"
SEED_TEXT = ("Fresh thread after rotation (T-6115); read ~/HANDOFF.md first; "
             "previous thread {old} archived at {path}.")


@dataclass
class Config:
    persona: str
    home: Path
    state_dir: Path
    codex_home: Path
    max_age_days: float = 3.0
    max_mb: float = 60.0
    dry_run: bool = False
    rollback: bool = False
    chronicle_root: Optional[Path] = None
    persona_state_root: Optional[Path] = None
    log_dir: Optional[Path] = None
    archive_group: Optional[str] = chronicle.NATIVE_TRANSCRIPT_GROUP


@dataclass(frozen=True)
class Result:
    status: str
    exit_code: int
    detail: str = ""


class RotateFail(Exception):
    pass


# ------------------------------------------------------------------ gate
def find_rollout(codex_home: Path, thread_id: str) -> Optional[Path]:
    hits = sorted((codex_home / "sessions").glob(f"*/*/*/rollout-*-{thread_id}.jsonl"))
    return hits[-1] if hits else None


def thread_start(rollout: Path) -> float:
    """The thread's birth: first line's session_meta timestamp, else the file's ctime."""
    try:
        with rollout.open("r", encoding="utf-8") as fh:
            doc = json.loads(fh.readline())
        payload = doc.get("payload") if isinstance(doc, dict) else None
        stamp = (payload or {}).get("timestamp") or doc.get("timestamp")
        if isinstance(stamp, str):
            return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError, AttributeError):
        pass
    return rollout.stat().st_ctime


def _in_flight(state_dir: Path) -> Optional[str]:
    """Why the delivery state says busy, or None. Unreadable = busy (fail closed)."""
    try:
        raw = (state_dir / "deliveries.jsonl").read_text(encoding="utf-8")
    except FileNotFoundError:
        raw = ""
    except OSError as exc:
        return f"deliveries.jsonl unreadable: {exc}"
    latest: dict[str, str] = {}
    for line in raw.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("delivery_id") and rec.get("state") in cd.STATES:
            latest[rec["delivery_id"]] = rec["state"]
    flying = sorted(d for d, s in latest.items() if s in IN_FLIGHT)
    if flying:
        return f"deliveries in flight: {', '.join(flying[:3])}"
    for sub in ("spool", "pending"):
        try:
            if any(not p.name.startswith(".") for p in (state_dir / sub).iterdir()):
                return f"{sub}/ is not empty"
        except FileNotFoundError:
            continue
        except OSError as exc:
            return f"{sub}/ unreadable: {exc}"
    return None


def _busy_reason(cfg: Config, pane_busy: Callable[[], bool]) -> Optional[str]:
    if pane_busy():
        return "pane shows a turn in progress"
    return _in_flight(cfg.state_dir)


def default_pane_busy() -> bool:
    try:
        out = subprocess.run(["tmux", "capture-pane", "-p", "-t", PANE_SESSION], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return True
    return out.returncode != 0 or BUSY_MARK in out.stdout


def seat_env(base: Optional[dict] = None) -> dict:
    """The user-bus environment `systemctl --user` needs under `sudo -u`/cron, where it is absent
    and every query would print 'Failed to connect to bus'."""
    env = dict(os.environ if base is None else base)
    run_dir = env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.geteuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={run_dir}/bus")
    return env


def default_run_cmd(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=120, env=seat_env())


# ------------------------------------------------------------------ units / binding
def _utc(clock_value: float) -> datetime:
    return datetime.fromtimestamp(clock_value, tz=timezone.utc)


def _stamp(clock_value: float) -> str:
    return _utc(clock_value).strftime("%Y%m%dT%H%M%SZ")


def _atomic_write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_id(path: Path) -> Optional[str]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tid = doc.get("thread_id") if isinstance(doc, dict) else None
    return tid if isinstance(tid, str) else None


class Units:
    """systemctl --user, trusting only the TEXT of is-active: a bus failure is rc!=0 with an empty
    stdout and must never read as 'stopped'."""

    def __init__(self, run_cmd):
        self.run = run_cmd

    def _run(self, verb: str, unit: str):
        try:
            return self.run(["systemctl", "--user", verb, unit])
        except (OSError, subprocess.SubprocessError) as exc:
            raise RotateFail(f"systemctl {verb} {unit}: {exc}") from exc

    def state(self, unit: str) -> str:
        return (self._run("is-active", unit).stdout or "").strip()

    def stop(self, unit: str) -> None:
        self._run("stop", unit)
        state = self.state(unit)
        if state not in ("inactive", "failed"):
            raise RotateFail(f"{unit} is not confirmed stopped after stop (is-active said {state!r})")

    def start(self, unit: str) -> None:
        if self._run("start", unit).returncode != 0:
            raise RotateFail(f"systemctl start {unit} failed")
        state = self.state(unit)
        if state != "active":
            raise RotateFail(f"{unit} is not active after start (is-active said {state!r})")


def _restore(cfg: Config, units: Units, hist: Optional[Path], old_id: str, *, now: float) -> None:
    """Put the old binding back and bring both units up. Raises RotateFail if that fails."""
    problems = []
    for unit in (WRITER_UNIT, DAEMON_UNIT):
        try:
            units.stop(unit)
        except RotateFail as exc:
            if hist is not None:   # binding already moved: a unit we cannot stop would race the restore
                problems.append(str(exc))
    thread_json = cfg.state_dir / "thread.json"
    if hist is not None and hist.exists():
        cur = _read_id(thread_json)
        if cur is not None and cur != old_id:
            os.replace(thread_json, hist.parent / f"{_stamp(now)}-{cur}.superseded")
        _atomic_write(thread_json, json.loads(hist.read_text(encoding="utf-8"))["record"])
        os.replace(hist, hist.with_name(hist.name + ".restored"))
    try:
        units.start(DAEMON_UNIT)
        units.start(WRITER_UNIT)
    except RotateFail as exc:
        problems.append(str(exc))
    if _read_id(thread_json) != old_id:
        problems.append(f"thread.json does not name {old_id}")
    if problems:
        raise RotateFail("; ".join(problems))


# ------------------------------------------------------------------ default chronicle seams
def default_archive(cfg: Config, thread_id: str, rollout: Path, ts: str) -> dict:
    return chronicle.archive_native_transcript(
        chronicle.agent_dir_name(cfg.persona), rollout, jsonl_uuid=thread_id, ts=ts,
        root=cfg.chronicle_root, group=cfg.archive_group)


def default_session_end(cfg: Config, pointer: dict, thread_id: str, when: datetime) -> dict:
    """Runs AFTER the stop, so the pane log is dead and may be staged. stage+certain make the
    claim key (runtime started_at, pane first stamp), which the supervisor's start-reconcile
    (stage+certain, same runtime) then finds done: one record, not two."""
    state_root = cfg.persona_state_root or sc.persona_state_root()
    rt_started = sc._effective_runtime_record(cfg.persona, state_root).get("started_at")
    pin = {}
    if not isinstance(rt_started, str) or not rt_started:
        # FALLBACK only: a bare codex seat's supervisor now writes runtime.json with started_at
        # (T-6118 PR-3), and then the claim keys on it so start-reconcile dedupes. When it is
        # absent or {} (older supervisor, record lost), every sourceless claim would key on None
        # and read as 'already-archived'; pin a unique identity for this rotation instead.
        pin = {"rt_pinned": True, "rt_started_at": f"thread-rotation:{thread_id}:{sc._iso(when)}"}
    req = sc.SessionEndRequest(
        persona=cfg.persona, cause="thread-rotation", account_home=cfg.home,
        handoff_file=cfg.home / "HANDOFF.md", harness="codex", stage=True, certain=True,
        uuid=thread_id, entrypoint="codex_thread_rotate", native_transcript=pointer, **pin,
        state_root=cfg.persona_state_root, chronicle_root=cfg.chronicle_root, log_dir=cfg.log_dir)
    return sc.session_end(req, now=when)


def archive_group_problem(group: Optional[str]) -> Optional[str]:
    """Why the archive could not chown its file to ``group``, else None (delegates to
    chronicle.archive_group_problem, its single home)."""
    return chronicle.archive_group_problem(group)


# ------------------------------------------------------------------ main flow
def rotate(cfg: Config, *, run_cmd=default_run_cmd, sleep: Callable[[float], None] = time.sleep,
           clock: Callable[[], float] = time.time, send=chk._mailbox_send,
           pane_busy: Callable[[], bool] = default_pane_busy,
           checkpoint_fn=chk.checkpoint, archive_fn=default_archive,
           session_end_fn=default_session_end,
           group_problem: Callable[[Optional[str]], Optional[str]] = archive_group_problem) -> Result:
    if cfg.persona not in PERSONAS:
        return Result("usage", 2, f"unknown persona {cfg.persona!r}")
    if cfg.persona == "watchman" and (cfg.rollback or not cfg.dry_run):
        return Result("refused", 4, WATCHMAN_REFUSAL)
    units = Units(run_cmd)
    if cfg.rollback:
        return _rollback(cfg, units, clock)

    thread_json = cfg.state_dir / "thread.json"
    old_id = _read_id(thread_json)
    if old_id is None:
        return Result("no-thread", 0, f"no thread record at {thread_json}")
    rollout = find_rollout(cfg.codex_home, old_id)
    if rollout is None:
        return Result("no-rollout", 0, f"thread {old_id} has no rollout (nothing to rotate)")
    age_days = (clock() - thread_start(rollout)) / 86400.0
    size_mb = rollout.stat().st_size / (1024 * 1024)
    if age_days <= cfg.max_age_days and size_mb <= cfg.max_mb:
        return Result("not-due", 0, f"thread {old_id}: {age_days:.1f}d, {size_mb:.1f}MB")
    why = f"thread {old_id} is due: {age_days:.1f}d (max {cfg.max_age_days:g}), {size_mb:.1f}MB (max {cfg.max_mb:g})"
    problem = group_problem(cfg.archive_group)
    if problem:
        return Result("archive-unwritable", 8, f"{why}; {problem}")
    busy = _busy_reason(cfg, pane_busy)
    if busy:
        return Result("busy", 0, f"{why}; {busy}")
    if cfg.dry_run:
        return Result("dry-run", 0, f"{why}; idle; would rotate")

    out = checkpoint_fn(persona=cfg.persona, home=cfg.home, send=send, sleep=sleep)
    if out.status not in ("written", "fresh"):
        return Result("checkpoint-failed", 3, f"HANDOFF checkpoint: {out.status}: {out.reason}")

    busy = _busy_reason(cfg, pane_busy)   # the checkpoint can take minutes: re-check
    if busy:
        return Result("busy", 0, f"seat went busy during the checkpoint ({busy}); no change")
    when = _utc(clock())
    pointer = archive_fn(cfg, old_id, rollout, when.isoformat().replace("+00:00", "Z"))
    busy = _busy_reason(cfg, pane_busy)   # the archive is slow: last look before the stop
    if busy:
        return Result("busy", 0, f"seat went busy during the archive ({busy}); no change")

    hist: Optional[Path] = None
    try:
        units.stop(WRITER_UNIT)   # holds a resumed connection to the old thread
        units.stop(DAEMON_UNIT)
        end = session_end_fn(cfg, pointer, old_id, when)
        results = end.get("results") or []
        first = results[0] if results else {}
        if (end.get("status") == "failed" or any(r.get("status") == "failed" for r in results)
                or first.get("status") not in ("archived", "paired") or not first.get("record")):
            raise RotateFail(f"Chronicle session-end did not archive this session: {end.get('error') or results}")
        hist = _rebind(cfg, old_id, rollout, pointer, clock())
        units.start(DAEMON_UNIT)
        deadline = clock() + NEW_THREAD_TIMEOUT_S
        new_id = None
        while True:
            cur = _read_id(thread_json)
            if cur and cur != old_id:
                new_id = cur
                break
            if clock() >= deadline:
                raise RotateFail(f"no new thread id within {NEW_THREAD_TIMEOUT_S:g}s")
            sleep(POLL_S)
        units.start(WRITER_UNIT)
    except BaseException as exc:  # noqa: BLE001 -- anything after the stop must restore, even SIGTERM
        err = f"{type(exc).__name__}: {exc}"
        print(f"[codex_thread_rotate] FAILED after stop: {err}; restoring thread {old_id}", file=sys.stderr)
        try:
            _restore(cfg, units, hist, old_id, now=clock())
        except Exception as rexc:  # noqa: BLE001
            print(f"[codex_thread_rotate] RESTORE FAILED: {rexc}", file=sys.stderr)
            if not isinstance(exc, Exception):
                raise exc from rexc  # the original interrupt, not the restore error (review note)
            return Result("restore-failed", 6, f"{err}; restore failed: {rexc}")
        if not isinstance(exc, Exception):
            raise   # KeyboardInterrupt/SystemExit: the seat is back; let the interrupt through
        return Result("restored", 5, f"{err}; old thread {old_id} restored and resumed")

    inbox = str(MAILBOX_ROOT / "codex")
    try:
        send(inbox, SEED_TEXT.format(old=old_id, path=pointer.get("path")), SEED_SUBJECT)
    except Exception as exc:  # noqa: BLE001 -- the new thread is live; do not undo it
        print(f"[codex_thread_rotate] SEED MAIL FAILED: {exc}", file=sys.stderr)
        return Result("rotated-unseeded", 7, f"rotated {old_id} -> {new_id}; seed mail failed: {exc}")
    return Result("rotated", 0, f"rotated {old_id} -> {new_id}; archive {pointer.get('path')}")


def _rebind(cfg: Config, old_id: str, rollout: Path, pointer: dict, now: float) -> Path:
    thread_json = cfg.state_dir / "thread.json"
    record = json.loads(thread_json.read_text(encoding="utf-8"))
    hist = cfg.state_dir / HISTORY_DIR / f"{_stamp(now)}-{old_id}.json"
    _atomic_write(hist, {"old_thread_id": old_id, "record": record, "rollout": str(rollout),
                         "archive": pointer, "rotated_at": _utc(now).isoformat()})
    try:
        os.remove(thread_json)   # history is durable first: a crash leaves a duplicate, never a loss
    except OSError:
        try:
            hist.unlink()        # never leave an entry --rollback could pick up for a rebind that did not happen
        except OSError:
            os.replace(hist, hist.with_name(hist.name + ".aborted"))
        raise
    return hist


def _rollback(cfg: Config, units: Units, clock) -> Result:
    hdir = cfg.state_dir / HISTORY_DIR
    entries = sorted(hdir.glob("*.json")) if hdir.is_dir() else []
    if not entries:
        return Result("no-history", 1, f"no entries in {hdir}")
    hist = entries[-1]
    try:
        doc = json.loads(hist.read_text(encoding="utf-8"))
        old_id, record = doc["old_thread_id"], doc["record"]
    except (OSError, ValueError, KeyError) as exc:
        return Result("bad-history", 1, f"{hist}: {exc!r}; nothing changed")
    thread_json = cfg.state_dir / "thread.json"
    try:
        units.stop(WRITER_UNIT)
        units.stop(DAEMON_UNIT)
        cur = _read_id(thread_json)
        if cur is not None and cur != old_id:
            os.replace(thread_json, hdir / f"{_stamp(clock())}-{cur}.superseded")
        _atomic_write(thread_json, record)
        units.start(DAEMON_UNIT)
        units.start(WRITER_UNIT)
        os.replace(hist, hist.with_name(hist.name + ".rolledback"))
    except BaseException as exc:  # noqa: BLE001
        print(f"[codex_thread_rotate] ROLLBACK FAILED: {exc!r}; restarting both units", file=sys.stderr)
        down = []
        for unit in (DAEMON_UNIT, WRITER_UNIT):
            try:
                units.start(unit)
            except Exception as sexc:  # noqa: BLE001
                down.append(str(sexc))
        if not isinstance(exc, Exception):
            raise
        if down:
            return Result("rollback-failed", 6, f"{exc}; restart failed: {'; '.join(down)}")
        return Result("rollback-failed", 5, f"{exc}; units restarted")
    return Result("rolled-back", 0, f"restored thread {old_id}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--persona", default=None, choices=PERSONAS,
                   help="optional; derived from the running account, and must agree with it")
    p.add_argument("--max-age-days", type=float, default=3.0)
    p.add_argument("--max-mb", type=float, default=60.0)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    a = p.parse_args(argv)
    try:
        a.persona = chk.resolve_persona(a.persona)
    except chk.PersonaError as exc:
        p.error(str(exc))

    def _term(signum, frame):  # SIGTERM must unwind through the restore, not skip it
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _term)
    home = chk.account_home()
    cfg = Config(persona=a.persona, home=home,
                 state_dir=home / ".local/state/pantheon/codex-appserver", codex_home=home / ".codex",
                 max_age_days=a.max_age_days, max_mb=a.max_mb, dry_run=a.dry_run, rollback=a.rollback)
    res = rotate(cfg)
    print(f"[codex_thread_rotate persona={a.persona}] {res.status}: {res.detail}")
    return res.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
