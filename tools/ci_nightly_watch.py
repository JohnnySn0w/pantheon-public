"""tools/ci_nightly_watch.py — a red nightly CI run must reach orchestrator the same day (T-4402).

THE FAILURE THIS EXISTS FOR, measured: ci.yml's scheduled run (`cron: '0 9 * * *'`, T-3566)
concluded `failure` every day from 2026-09-08 to 09-27 and nobody noticed. Its red came from
tests that read live repo content (a ticket's project_id, a watch topic's TOPIC_POLICY entry)
plus, on the 3.11 leg, a filesystem race. CI is informational and unrequired, and a red
scheduled run notifies nobody, so twenty consecutive reds were indistinguishable from silence.
This watcher is the "someone hears about it" half of a trustworthy CI gate; the other half is
`tools/staged_content_gate.py`, which stops the content-coupled reds at the writer.

WHAT IT WATCHES: the newest scheduled runs of workflow 304978944 (ci.yml), read GitHub-side.
This runs on the `claude` seat, the one that holds gh auth (recovery holds none; see
tools/ci_audit_liveness.py, whose GitHub-free journal probe is the runner-side counterpart).

VERDICTS, and which alert (every alerting verdict mails orchestrator's ORG-026 mailbox and nobody else):

    OK             the newest completed run succeeded.
    RED            the newest completed run concluded failure, timed_out or startup_failure.
                   Mailed ONCE per (run id, run attempt): the failed jobs, their failed steps,
                   and the extracted `FAILED <nodeid>` lines plus the shell-suite and
                   floor-check failure blocks. A re-run that fails again is a new attempt and
                   mails again. If the job logs cannot be read the RED alert still goes, saying so.
    DEAD_SCHEDULE  no scheduled run was CREATED in the last 36h. 24h cadence plus the observed
                   creation lateness (12:33Z-17:27Z) makes ~29h the worst normal gap; 09-22 was
                   genuinely skipped by GitHub, a 46.5h gap. 12h cooldown per verdict.
    PENDING_STUCK  the newest run has sat queued / in progress for over 8h (the serial runner
                   backlog of 2026-09-28 held runs for hours). 12h cooldown.
    INCONCLUSIVE   the two newest completed runs were BOTH cancelled, so nothing has measured the
                   suite for two nights. 12h cooldown.
    UNKNOWN        cannot measure: gh missing / unauthenticated / API error / unparseable reply /
                   an empty read that stays empty / an unrecognised conclusion. ALERTS. It does not
                   fall through to OK: a monitor whose broken state reads as health is the failure
                   it was built to catch (same discipline as ci_audit_liveness).
    STALE_READ     (T-4528) the runs listing's newest run is OLDER than the high-water mark
                   (highest run id seen, kept in state as `high_water`, only ever moves forward).
                   GitHub intermittently serves an old page; re-queried once, and if still stale
                   it is silent, with a consecutive-count in state (`stale_reads`) and the detail.
                   RED/INCONCLUSIVE/DEAD from the stale listing are skipped; DEAD_SCHEDULE is
                   judged from the mark's created_at, so a real dead schedule still alerts. A
                   corrupt/absent mark means no mark (today's behaviour).
    CANCELLED      one cancelled nightly. Recorded, silent. See the note below.
    RUNNING        in flight, under the stuck threshold. Silent.
    DISABLED       an authorised, expiring mute is in force. Silent.

⚠️ AN EMPTY READ IS NOT "NO RUN". The workflow-level endpoint occasionally returns an empty
list on one call and full results on the next. Empty is retried (3 reads); if it stays empty the
verdict is UNKNOWN, never OK and never DEAD_SCHEDULE: the workflow has months of history, so a
truly empty list is not a state the schedule can be in. The REPO-level `actions/runs?event=schedule`
returns partial, shifting results and is deliberately never used.

⚠️ GITHUB-SIDE LATENESS, noted and not fixed: GitHub creates the 09:00Z scheduled run 4-6h late
(~13-17Z). The timer is hourly all day rather than a 13-21Z window so the same-day promise holds
whenever the run lands, and the dead-schedule check is valid at any hour.

NOTE ON CANCELLED: the nightly shares ci.yml's `concurrency` group with push runs
(`CI-refs/heads/main`, cancel-in-progress). Run 36458276520 (09-28) was cancelled 106s after
creation by push run 36458364733, so its 3.11 lower-bound leg never ran. One cancel is common
enough (2 of 21 days) to stay silent; two in a row alert. The durable fix is a separate
concurrency group for `schedule` in ci.yml, which is outside this tool.

DELIVERY: `mailbox.send` to orchestrator. A failed send is NOT marked as alerted (the next hourly fire
retries it) and the process exits non-zero so the unit's OnFailure fires: a dead alert path must
be loud. (ci_audit_liveness swallows delivery failure and logs; this deliberately does not.)
An alerting verdict that DID deliver exits 0, so OnFailure does not double-report a monitor
that worked.

AUTHORISED NO: `<state>/ci-nightly-watch.disabled`, JSON {reason, until}; expired or unparseable
means not muted, so a mute cannot become permanent by neglect (ci_audit_liveness.disabled_reason).

State: `~/.local/state/pantheon/ci-nightly-watch.json` {alerted_runs, last_alert, last_verdict,
last_check_epoch, last_run_id}. `--dry-run` decides and prints, sends nothing, writes nothing.

NOT COVERED: whether the failures are real regressions (a human reads the mail); pull-request or
push CI runs (only event=schedule); a scheduled run that never appears because the schedule was
DISABLED by GitHub after 60 days of repo inactivity reads as DEAD_SCHEDULE, which is the right
alert.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.ci_audit_liveness import disabled_reason  # noqa: E402

GH_REPO = "example-org/pantheon-config"
# ci.yml, addressed by the workflow's numeric id: it survives a file rename, and a deleted
# workflow 404s into UNKNOWN (loud) rather than into silence.
WORKFLOW_ID = "304978944"
SENDER = "ci_nightly_watch"

OK, RED, DEAD_SCHEDULE, PENDING_STUCK, INCONCLUSIVE, UNKNOWN, CANCELLED, RUNNING, DISABLED = (
    "OK", "RED", "DEAD_SCHEDULE", "PENDING_STUCK", "INCONCLUSIVE", "UNKNOWN", "CANCELLED",
    "RUNNING", "DISABLED")
STALE_READ = "STALE_READ"
# Highest priority first; the first active condition is the run's headline verdict.
_PRIORITY = (RED, DEAD_SCHEDULE, PENDING_STUCK, INCONCLUSIVE, UNKNOWN, STALE_READ, CANCELLED,
             RUNNING)
RED_CONCLUSIONS = ("failure", "timed_out", "startup_failure")

DEFAULT_DEAD_SECS = int(os.environ.get("PANTHEON_CI_NIGHTLY_DEAD_SECS", str(36 * 3600)))
DEFAULT_PENDING_SECS = int(os.environ.get("PANTHEON_CI_NIGHTLY_PENDING_SECS", str(8 * 3600)))
DEFAULT_COOLDOWN_SECS = int(os.environ.get("PANTHEON_CI_NIGHTLY_COOLDOWN_SECS", str(12 * 3600)))
# T-4528: a read whose newest run is OLDER than the high-water mark is a stale GitHub listing.
# It is asked once more after this pause; 0 disables the re-query (detection stays on).
DEFAULT_STALE_REQUERY_SECS = int(os.environ.get("PANTHEON_CI_NIGHTLY_STALE_REQUERY_SECS", "20"))
RUNS_PER_PAGE = 10
EMPTY_READ_ATTEMPTS = 3
EMPTY_READ_SLEEP_SECS = 10
GH_TIMEOUT = 45

MAX_LINE_CHARS = 300
MAX_FAILED_LINES = 40
MAX_CHECKER_LINES = 25
MAX_ERROR_LINES = 5
MAX_JOBS = 4
MAX_BODY_CHARS = 9000
MAX_ALERTED_RUNS = 50

_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
_STATE_NAME = "ci-nightly-watch.json"
_DISABLE_NAME = "ci-nightly-watch.disabled"


class GhError(Exception):
    """A GitHub read that could not be completed or understood."""


def _log(msg: str) -> None:
    print(f"[ci_nightly_watch] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Log extraction: pure text in, structured lines out.
# ---------------------------------------------------------------------------

_TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ?")
# A pytest short-summary line for a test or a collection error. The `.py` requirement keeps
# unrelated `ERROR: ...` noise (pip, setup steps) out.
_FAILED_RE = re.compile(r"^(?:FAILED|ERROR) [\w./-]+\.py(?:::\S+)?(?: - .*)?$")
_SUMMARY_RE = re.compile(r"^\d+ (?:failed|passed|errors?)\b.* in [\d.]+s")
_CHECKER_HEADS = ("check_shell_suite_outcomes:", "check_suite_outcome_floor:")


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[: MAX_LINE_CHARS - 3] + "..."


def extract_failure_lines(log_text: str) -> dict:
    """Pull the diagnosable lines out of one job's log.

    Returns {failed, failed_omitted, summary, checker, errors}:
      failed    pytest `FAILED|ERROR <path>[::node] - reason` lines, first line only, deduped,
                capped at MAX_FAILED_LINES (the rest counted in failed_omitted)
      summary   pytest's last `N failed, M passed ... in Ts` line, or None
      checker   the failure blocks the two outcome checkers print (shell suites, suite floor):
                the header line then its two-space-indented detail lines
      errors    GitHub `##[error]` annotations (which command failed)
    """
    failed: list[str] = []
    seen: set[str] = set()
    summary: str | None = None
    checker: list[str] = []
    errors: list[str] = []
    in_block = False
    for raw in log_text.splitlines():
        line = _TS_RE.sub("", raw, count=1).rstrip()
        if line.startswith(_CHECKER_HEADS):
            in_block = True
            if len(checker) < MAX_CHECKER_LINES:
                checker.append(_clip(line))
            continue
        if in_block:
            if line.startswith("  ") and line.strip():
                if len(checker) < MAX_CHECKER_LINES:
                    checker.append(_clip(line.strip()))
                continue
            in_block = False
        if line.startswith("##[error]"):
            text = line[len("##[error]"):].strip()
            if text and text not in errors and len(errors) < MAX_ERROR_LINES:
                errors.append(text)
            continue
        if _FAILED_RE.match(line):
            if line not in seen:
                seen.add(line)
                failed.append(line)
            continue
        candidate = re.sub(r"^=+ | =+$", "", line).strip()
        if _SUMMARY_RE.match(candidate):
            summary = candidate
    omitted = max(0, len(failed) - MAX_FAILED_LINES)
    return {"failed": [_clip(f) for f in failed[:MAX_FAILED_LINES]], "failed_omitted": omitted,
            "summary": summary, "checker": checker, "errors": errors}


# ---------------------------------------------------------------------------
# The side-effect seam.
# ---------------------------------------------------------------------------


def orchestrator_inbox() -> str:
    """orchestrator's ORG-026 mailbox path; PANTHEON_ORCHESTRATOR_INBOX overrides (tests never need a hub)."""
    override = os.environ.get("PANTHEON_ORCHESTRATOR_INBOX")
    if override:
        return override
    from tools import agent_role_map as arm
    return f"{arm.mailbox_hub()}/orchestrator"


class Ops:
    """Every side effect in one seam, so check_once() is pure decision (and testable)."""

    def gh_api(self, endpoint: str) -> str:
        try:
            r = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True,
                               timeout=GH_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise GhError(f"gh api timed out after {GH_TIMEOUT}s: {endpoint}") from exc
        except OSError as exc:
            raise GhError(f"gh could not be run: {exc}") from exc
        if r.returncode != 0:
            raise GhError(f"gh api exited {r.returncode} for {endpoint}: "
                          f"{(r.stderr or r.stdout).strip()[:300]}")
        return r.stdout

    def _json(self, endpoint: str) -> dict:
        try:
            data = json.loads(self.gh_api(endpoint))
        except ValueError as exc:
            raise GhError(f"unparseable JSON from {endpoint}: {exc}") from exc
        if not isinstance(data, dict):
            raise GhError(f"unexpected reply shape from {endpoint}: {type(data).__name__}")
        return data

    def scheduled_runs(self) -> list[dict]:
        """Newest scheduled runs of ci.yml. Never returns an empty list: an empty read is
        retried and, if it stays empty, raised (see the module docstring)."""
        endpoint = (f"repos/{GH_REPO}/actions/workflows/{WORKFLOW_ID}/runs"
                    f"?event=schedule&per_page={RUNS_PER_PAGE}")
        for attempt in range(EMPTY_READ_ATTEMPTS):
            runs = self._json(endpoint).get("workflow_runs")
            if not isinstance(runs, list):
                raise GhError(f"reply from {endpoint} has no workflow_runs list")
            runs = [r for r in runs if isinstance(r, dict)]
            if runs:
                return runs
            if attempt < EMPTY_READ_ATTEMPTS - 1:
                self.sleep(EMPTY_READ_SLEEP_SECS)
        raise GhError(f"{EMPTY_READ_ATTEMPTS} reads of the workflow-level runs endpoint all came "
                      f"back EMPTY. An empty read is not 'no run': the workflow has a long run "
                      f"history, so this is the API misbehaving, not the schedule being idle")

    def run_jobs(self, run_id) -> list[dict]:
        jobs = self._json(f"repos/{GH_REPO}/actions/runs/{run_id}/jobs?per_page=100").get("jobs")
        if not isinstance(jobs, list):
            raise GhError(f"jobs reply for run {run_id} has no jobs list")
        return [j for j in jobs if isinstance(j, dict)]

    def job_log(self, job_id) -> str:
        return self.gh_api(f"repos/{GH_REPO}/actions/jobs/{job_id}/logs")

    def alert(self, subject: str, body: str) -> None:
        """Mail orchestrator. Raises on failure: the caller must NOT record an undelivered alert."""
        from tools import mailbox
        mailbox.send(orchestrator_inbox(), body, SENDER, subject=subject)

    def now(self) -> int:
        return int(time.time())

    def sleep(self, secs: float) -> None:
        time.sleep(secs)


# ---------------------------------------------------------------------------
# Decision.
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)  # atomic: a reader never sees a half-written state file
    except OSError as exc:
        _log(f"state write failed: {exc!r}")


def _write_state_merged(path: Path, state: dict) -> None:
    """T-4528: write `state`, but never regress the high-water mark. Under an flock on a sibling
    lock file, re-read the file and keep whichever mark has the higher run_id, so two overlapping
    probes (timer + a manual run) cannot move it backward. stale_reads stays last-writer-wins
    (best-effort, display only). A lock that cannot be taken degrades to the unlocked merge."""
    lock = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = open(path.with_name(path.name + ".lock"), "a")
        fcntl.flock(lock, fcntl.LOCK_EX)
    except OSError as exc:
        _log(f"state lock unavailable ({exc!r}); merging without it")
    try:
        disk = _high_water(_read_json(path))
        mine = _high_water(state)
        if disk is not None and (mine is None or disk[0] > mine[0]):
            state["high_water"] = _read_json(path)["high_water"]
        _write_json(path, state)
    finally:
        if lock is not None:
            lock.close()


def _parse_iso_z(value) -> int | None:
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def _hours(secs: float) -> str:
    return f"{secs / 3600:.1f}h"


def _high_water(state: dict) -> tuple[int, int, str] | None:
    """(run_id, created_epoch, created_at) of the stored mark, or None if absent or invalid
    (invalid means NO mark: fail-safe toward today's alerting behaviour)."""
    hw = state.get("high_water")
    if not isinstance(hw, dict):
        return None
    rid, created = hw.get("run_id"), _parse_iso_z(hw.get("created_at"))
    if not isinstance(rid, int) or isinstance(rid, bool) or created is None:
        return None
    return rid, created, str(hw.get("created_at"))


def _newest(runs: list[dict]) -> dict:
    return max(runs, key=lambda r: _parse_iso_z(r.get("created_at")) or 0)


def _is_stale(runs: list[dict], mark: tuple[int, int, str] | None) -> bool:
    """True when the newest run in this read is older (by run id, which is monotonic) than the mark."""
    if mark is None:
        return False
    rid = _newest(runs).get("id")
    return isinstance(rid, int) and not isinstance(rid, bool) and rid < mark[0]


def _stale_conditions(runs: list[dict], mark: tuple[int, int, str], count: int, now: int,
                      dead_secs: int) -> list[_Cond]:
    newest = _newest(runs)
    conds = [_Cond(
        STALE_READ,
        f"the runs listing is STALE: its newest run {newest.get('id')} is older than run "
        f"{mark[0]} (created {mark[2]}), which this watcher already saw. {count} consecutive "
        f"stale read(s). Verdicts from the stale listing are skipped; not an alert.",
        alerting=False)]
    age = now - mark[1]
    if age > dead_secs:
        conds.append(_Cond(
            DEAD_SCHEDULE,
            f"no scheduled CI run has been created for {_hours(age)} (threshold "
            f"{_hours(dead_secs)}; newest KNOWN run is {mark[0]} created {mark[2]}, from the "
            f"high-water mark, because the listing read is stale). The schedule is not firing."))
    return conds


def _run_key(run: dict) -> str:
    return f"{run.get('id')}.{run.get('run_attempt') or 1}"


class _Cond:
    """One active condition. `alerting` False = recorded but silent."""

    def __init__(self, verdict: str, detail: str, *, alerting: bool = True, run: dict | None = None):
        self.verdict, self.detail, self.alerting, self.run = verdict, detail, alerting, run


def _conditions(runs: list[dict], now: int, dead_secs: int, pending_secs: int) -> list[_Cond]:
    parsed: list[tuple[int, dict]] = []
    for r in runs:
        created = _parse_iso_z(r.get("created_at"))
        if created is None:
            return [_Cond(UNKNOWN, f"run {r.get('id')} has an unparseable created_at "
                                   f"{r.get('created_at')!r}, so run age is unmeasurable. This is "
                                   f"NOT a green result.")]
        parsed.append((created, r))
    parsed.sort(key=lambda p: p[0], reverse=True)
    newest_created, newest = parsed[0]
    age = now - newest_created
    completed = [r for _, r in parsed if r.get("status") == "completed"]
    conds: list[_Cond] = []

    if completed:
        latest = completed[0]
        concl = latest.get("conclusion")
        url = latest.get("html_url") or f"run {latest.get('id')}"
        if concl in RED_CONCLUSIONS:
            conds.append(_Cond(RED, f"scheduled run {latest.get('id')} concluded {concl}: {url}",
                               run=latest))
        elif concl == "cancelled":
            if len(completed) > 1 and completed[1].get("conclusion") == "cancelled":
                conds.append(_Cond(
                    INCONCLUSIVE,
                    f"the two newest completed scheduled runs ({latest.get('id')}, "
                    f"{completed[1].get('id')}) were BOTH cancelled, so nothing has measured the "
                    f"suite for two nights and the 3.11 lower-bound leg has not run. The nightly "
                    f"shares ci.yml's concurrency group with push runs, which cancel it: {url}"))
            else:
                conds.append(_Cond(
                    CANCELLED,
                    f"scheduled run {latest.get('id')} was cancelled (most likely superseded by a "
                    f"push run through the shared concurrency group; its 3.11 leg did not run): "
                    f"{url}", alerting=False))
        elif concl != "success":
            conds.append(_Cond(UNKNOWN, f"scheduled run {latest.get('id')} completed with the "
                                        f"unrecognised conclusion {concl!r}: {url}. Not treated "
                                        f"as green."))

    if age > dead_secs:
        conds.append(_Cond(
            DEAD_SCHEDULE,
            f"no scheduled CI run has been created for {_hours(age)} (threshold "
            f"{_hours(dead_secs)}; newest is run {newest.get('id')} created "
            f"{newest.get('created_at')}). GitHub creates the 09:00Z run 4-6h late, so a normal "
            f"gap is under ~29h. The schedule is not firing (GitHub skipped a day, or disabled "
            f"the schedule)."))

    if newest.get("status") != "completed":
        if age > pending_secs:
            conds.append(_Cond(
                PENDING_STUCK,
                f"scheduled run {newest.get('id')} has been {newest.get('status')} for "
                f"{_hours(age)} (threshold {_hours(pending_secs)}). The self-hosted runner is "
                f"backlogged or wedged: {newest.get('html_url') or ''}".rstrip()))
        else:
            conds.append(_Cond(RUNNING, f"scheduled run {newest.get('id')} is "
                                        f"{newest.get('status')} ({_hours(age)} old)",
                               alerting=False))
    return conds


def _red_alert(ops: Ops, run: dict) -> tuple[str, str]:
    """(subject, body) for a red run. Job/log read failures degrade the body, never the alert."""
    rid, concl = run.get("id"), run.get("conclusion")
    lines = [
        f"Scheduled CI run {rid} (attempt {run.get('run_attempt') or 1}) concluded {concl}.",
        f"run:     {run.get('html_url') or rid}",
        f"commit:  {str(run.get('head_sha') or '?')[:9]}",
        f"created: {run.get('created_at')}",
        "",
    ]
    node_ids: set[str] = set()
    try:
        jobs = ops.run_jobs(rid)
    except GhError as exc:
        jobs = None
        lines.append(f"job details unavailable: {exc}")
    if jobs is not None:
        failed_jobs = [j for j in jobs if j.get("conclusion") in ("failure", "timed_out")]
        if not failed_jobs:
            lines.append(f"no job logs: the run concluded {concl} with no failed job "
                         f"(a startup failure, or a run-level timeout).")
        for j in failed_jobs[:MAX_JOBS]:
            steps = ", ".join(s.get("name", "?") for s in j.get("steps") or []
                              if s.get("conclusion") in ("failure", "timed_out")) or "?"
            lines.append(f"== {j.get('name')}: {j.get('conclusion')} at step(s): {steps}")
            try:
                got = extract_failure_lines(ops.job_log(j.get("id")))
            except GhError as exc:
                lines.append(f"   log unavailable: {exc}")
                continue
            if got["summary"]:
                lines.append(f"   {got['summary']}")
            for f in got["failed"]:
                lines.append(f"   {f}")
                parts = f.split()
                if len(parts) > 1:
                    node_ids.add(parts[1])
            if got["failed_omitted"]:
                lines.append(f"   (+{got['failed_omitted']} more FAILED lines omitted)")
            for c in got["checker"]:
                lines.append(f"   {c}")
            for e in got["errors"]:
                lines.append(f"   ##[error] {e}")
            if not (got["failed"] or got["checker"] or got["errors"]):
                lines.append("   (no FAILED lines or checker blocks found in the log; open the run)")
        if len(failed_jobs) > MAX_JOBS:
            lines.append(f"(+{len(failed_jobs) - MAX_JOBS} more failed jobs not shown)")
    body = "\n".join(lines)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS - 20] + "\n... (truncated)"
    tests = f" — {len(node_ids)} failing test(s)" if node_ids else ""
    return f"nightly CI RED: run {rid} {concl}{tests}", body


def check_once(*, ops: Ops | None = None, state_dir: Path | None = None,
               dead_secs: int = DEFAULT_DEAD_SECS, pending_secs: int = DEFAULT_PENDING_SECS,
               cooldown_secs: int = DEFAULT_COOLDOWN_SECS, dry_run: bool = False,
               stale_requery_secs: int = DEFAULT_STALE_REQUERY_SECS) -> dict:
    """One probe. Returns {verdict, detail, run_id, conditions, alerted, delivery_failed
    [, would_alert on dry-run]}."""
    ops = ops or Ops()
    state_dir = Path(state_dir) if state_dir else _DEFAULT_STATE_DIR
    state_path = state_dir / _STATE_NAME
    now = ops.now()
    state = _read_json(state_path)
    result: dict = {"verdict": OK, "detail": "", "run_id": None, "conditions": [],
                    "alerted": [], "delivery_failed": False}
    if dry_run:
        result["would_alert"] = []

    muted = disabled_reason(state_dir, now, name=_DISABLE_NAME)
    if muted:
        result.update(verdict=DISABLED, detail=f"authorised quiet: {muted}")
        state["last_verdict"], state["last_check_epoch"] = DISABLED, now
        if not dry_run:
            _write_json(state_path, state)
        return result

    mark = _high_water(state)
    fresh_runs: list[dict] | None = None
    stale_reads = 0
    try:
        runs = ops.scheduled_runs()
        if not runs:  # the real Ops raises on empty; a substitute must not turn it into "no run"
            raise GhError("the scheduled-run read returned an empty list, which is not a "
                          "measurement")
        if _is_stale(runs, mark) and stale_requery_secs > 0:
            ops.sleep(stale_requery_secs)
            try:
                again = ops.scheduled_runs()
            except GhError as exc:
                _log(f"stale-read re-query failed ({exc}); keeping the first read")
                again = None
            if again and not _is_stale(again, mark):
                _log("stale runs listing recovered on re-query")
                runs = again
        if _is_stale(runs, mark):
            prev = state.get("stale_reads")
            stale_reads = (prev if isinstance(prev, int) and prev >= 0 else 0) + 1
            conds = _stale_conditions(runs, mark, stale_reads, now, dead_secs)
        else:
            conds = _conditions(runs, now, dead_secs, pending_secs)
            fresh_runs = runs
        result["run_id"] = max(runs, key=lambda r: _parse_iso_z(r.get("created_at")) or 0).get("id")
    except GhError as exc:
        conds = [_Cond(UNKNOWN, f"the scheduled-run read FAILED ({exc}). Nightly CI state is "
                                f"currently unmeasurable. This is NOT a green result and must not "
                                f"be read as one.")]
    conds.sort(key=lambda c: _PRIORITY.index(c.verdict))
    result["conditions"] = [c.verdict for c in conds]
    if conds:
        result["verdict"], result["detail"] = conds[0].verdict, conds[0].detail
    else:
        result["detail"] = "the newest completed scheduled run succeeded"

    alerted_runs: dict = state.get("alerted_runs") if isinstance(state.get("alerted_runs"), dict) else {}
    last_alert: dict = state.get("last_alert") if isinstance(state.get("last_alert"), dict) else {}
    for cond in conds:
        if not cond.alerting:
            continue
        if cond.verdict == RED:
            due = _run_key(cond.run) not in alerted_runs
        else:
            last = last_alert.get(cond.verdict)
            due = not isinstance(last, int) or now - last >= cooldown_secs
        if not due:
            continue
        if dry_run:
            result["would_alert"].append(cond.verdict)
            continue
        if cond.verdict == RED:
            subject, body = _red_alert(ops, cond.run)
        else:
            subject = f"nightly CI watch: {cond.verdict}"
            body = f"[ci-nightly-watch] {cond.verdict}: {cond.detail}"
        try:
            ops.alert(subject, body)
        except Exception as exc:  # noqa: BLE001 - any send failure must be loud AND retried
            _log(f"alert delivery FAILED for {cond.verdict} ({exc!r}); NOT marked as sent, "
                 f"the next fire retries it")
            result["delivery_failed"] = True
            continue
        result["alerted"].append(cond.verdict)
        if cond.verdict == RED:
            alerted_runs[_run_key(cond.run)] = now
        else:
            last_alert[cond.verdict] = now

    if dry_run:
        return result
    while len(alerted_runs) > MAX_ALERTED_RUNS:
        alerted_runs.pop(next(iter(alerted_runs)))
    if fresh_runs:
        newest = _newest(fresh_runs)
        nid, ncreated = newest.get("id"), _parse_iso_z(newest.get("created_at"))
        if (isinstance(nid, int) and not isinstance(nid, bool) and ncreated is not None
                and (mark is None or nid > mark[0])):
            state["high_water"] = {"run_id": nid, "created_at": newest.get("created_at")}
    if fresh_runs is not None or stale_reads:
        state["stale_reads"] = stale_reads
    state.update(alerted_runs=alerted_runs, last_alert=last_alert,
                 last_verdict=result["verdict"], last_check_epoch=now,
                 last_run_id=result["run_id"])
    _write_state_merged(state_path, state)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dead-secs", type=int, default=DEFAULT_DEAD_SECS)
    ap.add_argument("--pending-secs", type=int, default=DEFAULT_PENDING_SECS)
    ap.add_argument("--cooldown-secs", type=int, default=DEFAULT_COOLDOWN_SECS)
    ap.add_argument("--stale-requery-secs", type=int, default=DEFAULT_STALE_REQUERY_SECS,
                    help="pause before re-reading a stale runs listing; 0 disables the re-query")
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="decide and print; never mail, never touch state")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    result = check_once(ops=Ops(), state_dir=Path(args.state_dir) if args.state_dir else None,
                        dead_secs=args.dead_secs, pending_secs=args.pending_secs,
                        cooldown_secs=args.cooldown_secs, dry_run=args.dry_run,
                        stale_requery_secs=args.stale_requery_secs)
    if args.json:
        print(json.dumps(result))
    else:
        tags = "".join(f" [ALERTED {v}]" for v in result["alerted"])
        tags += "".join(f" [WOULD ALERT {v}]" for v in result.get("would_alert", []))
        _log(f"{result['verdict']}: {result['detail']}{tags}")
    # An alerting verdict that DELIVERED exits 0 (OnFailure would double-report a monitor that
    # worked). An alert that could NOT be delivered exits 1 so the unit's OnFailure speaks.
    return 1 if result["delivery_failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
