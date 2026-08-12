"""tools/ci_audit_liveness.py — alert when the origin-side byte-sequence audit STOPS RUNNING (T-2440).

THE FAILURE THIS EXISTS FOR, measured: 2026-07-30 -> 08-02, pve2 left the tailnet and took the
only self-hosted runner with it. The HERMES discipline audit — CLAUDE.md's Option A, the layer
covering a hookless clone or a `--no-verify` push — did not execute on origin for two days and
NOTHING SAID SO. Not one alert. The outage produced no red runs because it produced no runs.

WHY NOT A REQUIRED STATUS CHECK, which is the obvious fix and is a no-op here (T-2392):
protect-main's bypass_actors are DeployKey and User 33181572, both bypass_mode "always", and
every fleet push lands directly on main with no PR — proven by pushing d65d42248 that way while
the ruleset's pull_request rule was active. A required_status_checks rule would gate a PR from a
non-bypass contributor, i.e. nobody, while making the ruleset READ as though the audit were
enforced. Presence is not activation. The gap is liveness, so liveness is what this watches.

SIGNAL — the RUNNER's own journal, not GitHub's API. Two reasons, and the second is the one that
decided it:
  1. recovery holds no GitHub auth (only the `claude` account is logged in), so a GitHub-side probe
     would run through another account's credential on every fire.
  2. `gh run list --limit N` returns the NEWEST n runs while the runner takes the OLDEST queued
     job. During the 2026-08-03 backlog that read 95-queued, 95-queued across 45s — a frozen
     queue — while the runner finished a job every six seconds. A windowed instrument pointed
     away from where the work happens reports zero motion no matter how fast the system runs.
     Liveness must never be read off a bounded window.
The journal is the authority for "did the job actually execute". A GitHub-side corroborating read
is deliberately NOT implemented here — see NOT COVERED at the bottom.

FOUR VERDICTS, NOT A BOOLEAN. The ticket asked for host-down to be split out and it matters: the
journal goes quiet for "the audit stopped" AND for "the whole host is gone", and those are
different incidents wanting different first moves. Collapsing them would have described the 2-day
outage as a stale audit when the actual fault was a saturated home uplink.

    OK         a successful audit newer than the threshold — or no commits since the last
               success, so no audit was DUE. (A quiet weekend is not a fault.)
    STALE      commits landed on origin/main after the last successful audit, and that gap
               exceeds the threshold. The guard is not running. ALERT.
    HOST_DOWN  pve2 does not answer. ALERT, with its own text — check the host, not the runner.
    UNKNOWN    reachable but unmeasurable (unit gone, journal unreadable, git failed). ALERT.

⚠️ UNKNOWN ALERTS. It does not fall through to OK. A monitor whose broken state is indistinguishable
from health is the exact shape of the failure it was built to catch, and this one would inherit it
for free: every "cannot measure" path here returns no timestamp, and "no recent success" and "could
not look" are the same absence at the call site if you let them be.

AUTHORISED NO. A deliberately stopped runner (fleet pause, maintenance, a planned pve2 outage) is a
legitimate quiet, and a monitor with no way to be TOLD that gets muted by hand and stays muted. So
the mute is a file with an EXPIRY: `<state>/ci-audit-liveness.disabled`, JSON {reason, until}. Past
`until`, or unparseable, the mute does not apply — it fails toward alerting, and it cannot become
permanent by neglect.

⚠️ TIMEZONE. `journalctl --since` is parsed by the CONTAINER's clock, and a container's TZ is not
its host's (boxon runs CDT while its LXCs run UTC). So the window is deliberately WIDE and the
authoritative timestamp is the one the runner writes INSIDE its own message ("2026-08-03 02:24:23Z:
Job ... Succeeded") — an explicit Z, parsed here. A generous window plus an unambiguous timestamp is
immune to an offset that a tight window would silently mis-slice.

⚠️⚠️ WHAT "OK" DOES NOT MEAN — caught by running this against the live system before landing it,
and it is the same error one layer up from the one that motivated the whole detector. The runner
takes the OLDEST queued job, so during a backlog a success timestamp is MINUTES old while the job
it ran was for a commit from DAYS ago. On 2026-08-03 this returned OK ("last audit 0m ago") with
505 runs still queued and the tip's own audit among them. So:

    OK means THE AUDIT IS EXECUTING. It does NOT mean the tip has been audited.

That is the property this instrument can actually see from the runner side, and it is the one the
2-day outage needed. Tip COVERAGE is a different question requiring queue depth, which lives on the
GitHub side that recovery cannot read. Do not let the OK wording drift back toward implying coverage.

SIGNAL 2 — TIP COVERAGE, added T-2441, and it answers a DIFFERENT QUESTION. The runner-side probe
above can only see "is the audit executing". This one asks "has the audit CONCLUDED for the sha
that is currently origin/main's tip", which is the property CLAUDE.md's caveat block and the
local-stand-in retire condition actually need. They are kept as two signals rather than merged
because they fail independently: the runner can be alive while the tip is hours back in a queue,
and GitHub can hold a run the runner would happily have taken.

  ⚠️ THE TRAP THIS AVOIDS BY CONSTRUCTION. `gh run list --limit N` returns the NEWEST n runs while
  the runner takes the OLDEST queued job. Sampled 45s apart during the 2026-08-03 backlog that
  reported 95-queued then 95-queued — a frozen queue — while the runner finished a job every six
  seconds. So this NEVER reads a bounded window: it queries the workflow's runs filtered BY
  head_sha, which returns only that sha's runs and an exact total. There is no window to mis-slice
  and no depth to under-count. Do not "optimise" this into a list-and-scan.

  WHY IT LIVES ON THE `claude` SEAT. recovery holds no GitHub auth at all (`gh auth status` on
  claude-fallback: not logged into any host), which is what left this gap open in T-2440. The
  alternative — recovery shelling through `sudo -u claude gh` on every timer fire — would make one
  seat's credential a standing dependency of another seat's monitor. So signal 1 runs on recovery and
  signal 2 runs here, each on the seat that holds what it needs.

FIVE TIP VERDICTS:
    COVERED        a run for the tip sha concluded successfully. THIS is what "the audit is green
                   on origin" means, and the only thing that should retire the local stand-in.
    AUDIT_FAILED   a run for the tip sha reached a terminal NON-success (failure, cancelled,
                   timed_out, action_required). ALERT — from a coverage standpoint a cancelled
                   audit leaves the tip exactly as unaudited as a failed one.
    PENDING        a run exists and is queued or in progress. Below the pending threshold this is
                   normal and silent; above it, it is a real backlog and ALERTS.
    NOT_DISPATCHED no run exists for that sha at all. Below a short grace this is just GitHub
                   lag; above it, GitHub never created the run — a DIFFERENT fault from a backlog
                   and one no amount of runner health will fix. ALERT.
    UNKNOWN        could not measure (gh missing, unauthenticated, workflow renamed, API error).
                   ALERTS, same discipline as signal 1: unmeasurable is not green.

NOT COVERED, stated rather than left to be discovered:
  - backlog DEPTH. Tip coverage answers "is the tip audited"; it deliberately does not count the
    queue, because every cheap way to count it is the windowing trap above.
  - whether an OLDER commit on main was ever audited. Coverage is evaluated at the tip only. A
    commit that landed and was superseded before its audit ran is not visible here.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

AGENT = "recovery"                       # whose failure lane the alert rides (T-0722 routing)
# Signal 2 runs on the `claude` seat, so its failures are orchestrator's — which route THROUGH recovery for
# triage (route_failure_alert sends non-recovery agents to recovery, recovery-owned ones direct to the operator).
TIP_AGENT = "orchestrator"
GH_REPO = "example-org/pantheon-config"
# Referenced by FILENAME, which is how the API addresses a workflow. If the file is renamed the
# endpoint 404s and we go UNKNOWN (loud) rather than silently reporting on a workflow that is gone
# — the same failure mode SUCCESS_JOB guards against on the runner side.
AUDIT_WORKFLOW_FILE = "hermes-audit.yml"
PVE_HOST = "pve2"                    # ssh alias; the IP lives in ~/.ssh/config
RUNNER_CT = "202"                    # LXC on pve2 that holds the runner (NOT the host itself)
RUNNER_UNIT = ("actions.runner.example-org-pantheon-config"
               ".pve2-pantheon-config.service")
# The runner's own success line. Matched on the JOB NAME because the workflow's display name is
# what the runner logs; if the workflow job is renamed this stops matching and the monitor goes
# UNKNOWN (loud), rather than silently reporting OK forever on a job that no longer exists.
SUCCESS_JOB = "Scan added lines for byte-sequence violations"
_SUCCESS_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})Z: Job "
    + re.escape(SUCCESS_JOB)
    + r" completed with result: Succeeded"
)

# Wide on purpose — see the TIMEZONE note. This bounds journal READ cost, not the decision.
JOURNAL_WINDOW = os.environ.get("PANTHEON_CI_AUDIT_JOURNAL_WINDOW", "-4 days")
# Commits-without-a-successful-audit before we call it stale. The observed outage was 48h+, so
# anything under a day catches it; a tighter threshold on a home-lab runner cries at every reboot.
DEFAULT_THRESHOLD_SECS = int(os.environ.get("PANTHEON_CI_AUDIT_THRESHOLD_SECS", str(12 * 3600)))
DEFAULT_COOLDOWN_SECS = int(os.environ.get("PANTHEON_CI_AUDIT_COOLDOWN_SECS", str(6 * 3600)))
SSH_TIMEOUT = 45
GH_TIMEOUT = 60

# How long the tip may sit queued before that is a fault rather than ordinary latency. 4h: long
# enough that a normal push never pages (runs conclude in minutes), short enough that a real
# backlog is caught the same day. The 2026-08-03 backlog held the tip's own audit for hours.
DEFAULT_PENDING_SECS = int(os.environ.get("PANTHEON_CI_AUDIT_PENDING_SECS", str(4 * 3600)))
# How long we allow for GitHub to CREATE the run at all. Normally seconds; 15m is pure slack.
DEFAULT_DISPATCH_GRACE_SECS = int(os.environ.get("PANTHEON_CI_AUDIT_DISPATCH_GRACE_SECS", str(900)))

_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
_STATE_NAME = "ci-audit-liveness.json"
_DISABLE_NAME = "ci-audit-liveness.disabled"
# Signal 2 gets its OWN state and its OWN mute. They are independent instruments and muting one
# must not mute the other — a planned runner outage is a reason to silence execution liveness, but
# "the tip is not audited" stays true and stays worth knowing throughout it.
_TIP_STATE_NAME = "ci-audit-tip-coverage.json"
_TIP_DISABLE_NAME = "ci-audit-tip-coverage.disabled"

OK, STALE, HOST_DOWN, UNKNOWN, DISABLED = "OK", "STALE", "HOST_DOWN", "UNKNOWN", "DISABLED"
ALERTING = (STALE, HOST_DOWN, UNKNOWN)

COVERED, AUDIT_FAILED, PENDING, NOT_DISPATCHED = (
    "COVERED", "AUDIT_FAILED", "PENDING", "NOT_DISPATCHED")
# Terminal GitHub conclusions that are NOT a successful audit. Coverage-wise a cancelled run and a
# failed one are the same fact: the tip is not audited and a human has to look.
_TERMINAL_NON_SUCCESS = ("failure", "cancelled", "timed_out", "action_required", "startup_failure",
                         "stale", "neutral")


def _log(msg: str) -> None:
    print(f"[ci_audit_liveness] {msg}", file=sys.stderr)


def parse_success_epoch(journal_text: str) -> int | None:
    """Newest successful-audit epoch in `journal_text`, or None if it holds none.

    Reads the timestamp the RUNNER wrote (explicit Z = UTC), never journald's own field —
    see the TIMEZONE note. Takes the max rather than the last line so an out-of-order or
    interleaved journal cannot hand back an older success than one that is present."""
    best: int | None = None
    for line in journal_text.splitlines():
        m = _SUCCESS_RE.match(line.strip())
        if not m:
            continue
        try:
            dt = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        epoch = int(dt.timestamp())
        if best is None or epoch > best:
            best = epoch
    return best


class Ops:
    """Every side effect, in one seam, so check_once() is pure decision (and testable)."""

    def __init__(self, repo: Path | None = None):
        self.repo = Path(repo) if repo else Path.home() / "pantheon-config"

    # -- signal 1: is the host even there? ---------------------------------
    def host_reachable(self) -> bool | None:
        """True/False, or None if WE could not run the probe (never 'down')."""
        try:
            r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                PVE_HOST, "true"],
                               capture_output=True, text=True, timeout=SSH_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError):
            return False            # ssh timing out IS the host being unreachable, for our purpose
        return r.returncode == 0

    # -- signal 2: when did the audit last actually run? -------------------
    def journal_text(self) -> str | None:
        """Raw runner journal, or None if it could not be read (-> UNKNOWN, never OK)."""
        remote = (f"pct exec {RUNNER_CT} -- journalctl -u {RUNNER_UNIT} "
                  f"--since '{JOURNAL_WINDOW}' --no-pager -o cat")
        try:
            r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                PVE_HOST, remote],
                               capture_output=True, text=True, timeout=SSH_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError):
            return None
        if r.returncode != 0:
            return None
        return r.stdout

    # -- signal 3: was an audit even DUE? ----------------------------------
    def origin_last_commit_epoch(self) -> int | None:
        """Commit time of origin/main's tip, or None if it could not be determined.

        Fetches first: the whole point is the state of ORIGIN, and a stale local ref would
        answer a question nobody asked (the standing 'the check must reach the authority')."""
        try:
            subprocess.run(["git", "-C", str(self.repo), "fetch", "--quiet", "origin", "main"],
                           capture_output=True, text=True, timeout=120)
            r = subprocess.run(["git", "-C", str(self.repo), "log", "-1", "--format=%ct",
                                "origin/main"],
                               capture_output=True, text=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError):
            return None
        if r.returncode != 0:
            return None
        try:
            return int(r.stdout.strip())
        except ValueError:
            return None

    # -- signal 2a: what IS the tip? ---------------------------------------
    def origin_tip_sha(self) -> str | None:
        """origin/main's tip sha, or None. Fetches first, for the same reason as above."""
        try:
            subprocess.run(["git", "-C", str(self.repo), "fetch", "--quiet", "origin", "main"],
                           capture_output=True, text=True, timeout=120)
            r = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "origin/main"],
                               capture_output=True, text=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError):
            return None
        if r.returncode != 0:
            return None
        sha = r.stdout.strip()
        return sha if len(sha) == 40 else None

    # -- signal 2b: has the audit concluded for THAT sha? ------------------
    def audit_runs_for_sha(self, sha: str) -> list[dict] | None:
        """Every audit run for exactly `sha`, or None if the read failed (-> UNKNOWN).

        ⚠️ A PER-SHA QUERY, DELIBERATELY, NOT A LISTING. `head_sha` filters server-side and
        returns only this commit's runs, so the answer does not depend on how deep the queue is
        or which end of it we happened to sample. Rewriting this as `gh run list --limit N` and
        scanning reintroduces exactly the trap that produced two wrong queue numbers on
        2026-08-03. An empty list is a real answer here (no run exists) — it is only None when
        the call itself failed."""
        endpoint = (f"repos/{GH_REPO}/actions/workflows/{AUDIT_WORKFLOW_FILE}/runs"
                    f"?head_sha={sha}&per_page=100")
        try:
            r = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True,
                               timeout=GH_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError):
            return None                 # gh absent, or the API hung
        if r.returncode != 0:
            return None                 # unauthenticated, 404 (workflow renamed), rate-limited
        try:
            data = json.loads(r.stdout)
        except ValueError:
            return None
        runs = data.get("workflow_runs")
        if not isinstance(runs, list):
            return None
        return runs

    def alert(self, msg: str, vocab: str, agent: str = AGENT) -> None:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from inbox_consumer import route_failure_alert  # late: heavy side-effect surface
            route_failure_alert(agent, msg, vocab=vocab)
        except Exception as exc:                       # an alert path must never crash the probe
            _log(f"alert delivery FAILED ({exc!r}); verdict still recorded in state")

    def now(self) -> int:
        return int(time.time())


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except OSError as exc:
        _log(f"state write failed: {exc!r}")


def disabled_reason(state_dir: Path, now: int, name: str = _DISABLE_NAME) -> str | None:
    """The AUTHORISED no, or None. Expired/malformed/absent all mean NOT disabled.

    Fails toward alerting on purpose: a mute that outlives its reason is how a monitor dies
    quietly, and a mute nobody can read is indistinguishable from one nobody set."""
    data = _read_json(state_dir / name)
    if not data:
        return None
    until = data.get("until")
    try:
        expiry = int(datetime.fromisoformat(str(until).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None                                    # unparseable expiry -> not a valid mute
    if expiry <= now:
        return None                                    # lapsed -> the monitor speaks again
    reason = str(data.get("reason") or "no reason recorded")
    return f"{reason} (until {until})"


def check_once(*, ops: Ops | None = None, state_dir: Path | None = None,
               threshold_secs: int = DEFAULT_THRESHOLD_SECS,
               cooldown_secs: int = DEFAULT_COOLDOWN_SECS,
               dry_run: bool = False) -> dict:
    """One probe. Returns {verdict, detail, last_success, last_commit, alerted}."""
    ops = ops or Ops()
    state_dir = Path(state_dir) if state_dir else _DEFAULT_STATE_DIR
    now = ops.now()

    muted = disabled_reason(state_dir, now)
    if muted:
        return _finish(state_dir, {"verdict": DISABLED, "detail": f"authorised quiet: {muted}",
                                   "last_success": None, "last_commit": None, "alerted": False},
                       now, ops, cooldown_secs, dry_run)

    reachable = ops.host_reachable()
    if reachable is False:
        return _finish(state_dir, {
            "verdict": HOST_DOWN,
            "detail": (f"{PVE_HOST} does not answer ssh. The runner is ON that host (LXC "
                       f"{RUNNER_CT}), so the audit is not running — but the fault to chase is "
                       f"the HOST, not the runner. Last time this happened the cause was a "
                       f"saturated home uplink, nothing to do with CI."),
            "last_success": None, "last_commit": None, "alerted": False},
            now, ops, cooldown_secs, dry_run)

    journal = ops.journal_text()
    if journal is None:
        return _finish(state_dir, {
            "verdict": UNKNOWN,
            "detail": (f"{PVE_HOST} answers but its runner journal could not be read (unit "
                       f"missing/renamed, pct failed, or no permission). This is NOT a green "
                       f"result — the audit's liveness is currently unmeasurable."),
            "last_success": None, "last_commit": None, "alerted": False},
            now, ops, cooldown_secs, dry_run)

    last_success = parse_success_epoch(journal)
    last_commit = ops.origin_last_commit_epoch()
    if last_commit is None:
        return _finish(state_dir, {
            "verdict": UNKNOWN,
            "detail": ("could not determine origin/main's tip commit time, so 'was an audit "
                       "due?' is unanswerable. Not green."),
            "last_success": last_success, "last_commit": None, "alerted": False},
            now, ops, cooldown_secs, dry_run)

    if last_success is None:
        # Nothing in a 4-day window. Only a fault if work landed that should have triggered one.
        if last_commit <= now - threshold_secs:
            return _finish(state_dir, {
                "verdict": OK,
                "detail": ("no audit in the journal window, but no commits either — nothing "
                           "was due."),
                "last_success": None, "last_commit": last_commit, "alerted": False},
                now, ops, cooldown_secs, dry_run)
        return _finish(state_dir, {
            "verdict": STALE,
            "detail": (f"NO successful audit in the journal window ({JOURNAL_WINDOW}) while "
                       f"origin/main was committed to {_ago(now, last_commit)} ago. The guard "
                       f"is not running."),
            "last_success": None, "last_commit": last_commit, "alerted": False},
            now, ops, cooldown_secs, dry_run)

    if last_commit <= last_success:
        return _finish(state_dir, {
            "verdict": OK,
            "detail": (f"audit EXECUTING — a job succeeded {_ago(now, last_success)} ago, after "
                       f"the newest commit ({_ago(now, last_commit)} ago). Note this is "
                       f"execution liveness, NOT tip coverage: the runner works oldest-first, so "
                       f"a fresh success can belong to an old commit."),
            "last_success": last_success, "last_commit": last_commit, "alerted": False},
            now, ops, cooldown_secs, dry_run)

    gap = last_commit - last_success
    if gap <= threshold_secs:
        return _finish(state_dir, {
            "verdict": OK,
            "detail": (f"commits landed {_ago(now, last_commit)} ago, {_dur(gap)} after the last "
                       f"successful audit — inside the {_dur(threshold_secs)} threshold "
                       f"(a backlog draining is not a fault)."),
            "last_success": last_success, "last_commit": last_commit, "alerted": False},
            now, ops, cooldown_secs, dry_run)

    return _finish(state_dir, {
        "verdict": STALE,
        "detail": (f"origin/main has commits the audit has never seen: last success "
                   f"{_ago(now, last_success)} ago, newest commit {_ago(now, last_commit)} ago, "
                   f"gap {_dur(gap)} > threshold {_dur(threshold_secs)}. The byte-sequence guard "
                   f"is not covering what has landed. CLAUDE.md's claim that it 'runs green' is "
                   f"false while this holds."),
        "last_success": last_success, "last_commit": last_commit, "alerted": False},
        now, ops, cooldown_secs, dry_run)


def _parse_iso_z(value: str | None) -> int | None:
    """GitHub's `created_at` ('2026-08-03T03:25:07Z') as an epoch, or None."""
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def classify_tip_coverage(runs: list[dict], *, now: int, tip_sha: str,
                          commit_epoch: int | None,
                          pending_secs: int = DEFAULT_PENDING_SECS,
                          dispatch_grace_secs: int = DEFAULT_DISPATCH_GRACE_SECS) -> dict:
    """Pure decision over the tip sha's audit runs. Returns {verdict, detail, should_alert}.

    PRECEDENCE — success > terminal-non-success > in-flight > none — and the middle one is the
    only debatable step, so: if a run for this sha already concluded red and a re-run is queued,
    we report the RED. A terminal negative on the tip is actionable NOW, the re-run does not undo
    it, and the next fire reports COVERED the moment the re-run succeeds. Biasing toward speaking
    is the right error here; the failure this whole file exists for was a silence."""
    short = tip_sha[:9]
    succeeded = [r for r in runs if r.get("conclusion") == "success"]
    if succeeded:
        when = _parse_iso_z(succeeded[0].get("updated_at") or succeeded[0].get("created_at"))
        stamp = f" (concluded {_ago(now, when)} ago)" if when else ""
        return {"verdict": COVERED, "should_alert": False,
                "detail": (f"origin/main tip {short} HAS been audited{stamp}: "
                           f"{AUDIT_WORKFLOW_FILE} concluded success for that exact sha. This is "
                           f"the claim CLAUDE.md's Option A needs — the guard covered what is "
                           f"actually on main.")}

    bad = [r for r in runs if r.get("conclusion") in _TERMINAL_NON_SUCCESS]
    if bad:
        concl = bad[0].get("conclusion")
        url = bad[0].get("html_url") or f"run {bad[0].get('id')}"
        return {"verdict": AUDIT_FAILED, "should_alert": True,
                "detail": (f"the audit for origin/main tip {short} reached a terminal "
                           f"NON-success: {concl}. The tip is NOT covered — a cancelled or timed "
                           f"out audit leaves it exactly as unaudited as a red one. {url}")}

    inflight = [r for r in runs if r.get("status") in ("queued", "in_progress", "waiting",
                                                       "requested", "pending")]
    if inflight:
        created = min((e for e in (_parse_iso_z(r.get("created_at")) for r in inflight)
                       if e is not None), default=None)
        waited = (now - created) if created is not None else None
        over = waited is not None and waited > pending_secs
        age = f"{_dur(waited)}" if waited is not None else "an unknown time"
        return {"verdict": PENDING, "should_alert": over,
                "detail": (f"origin/main tip {short} is NOT YET audited — a run exists and has "
                           f"been {inflight[0].get('status')} for {age}"
                           + (f", past the {_dur(pending_secs)} threshold. That is a real backlog: "
                              f"the runner works oldest-first, so the tip waits behind everything "
                              f"queued before it. Execution liveness will read OK throughout."
                              if over else
                              f" (under the {_dur(pending_secs)} threshold — ordinary latency)."))}

    # No runs at all for this sha. Distinguished from PENDING on purpose: a run that GitHub never
    # created is not waiting on the runner, and no amount of runner health will produce it.
    since = (now - commit_epoch) if commit_epoch is not None else None
    fresh = since is not None and since <= dispatch_grace_secs
    return {"verdict": NOT_DISPATCHED, "should_alert": not fresh,
            "detail": (f"NO {AUDIT_WORKFLOW_FILE} run exists for origin/main tip {short}"
                       + (f", and the commit landed {_dur(since)} ago" if since is not None
                          else ", and the commit's age is unknown")
                       + (f" — inside the {_dur(dispatch_grace_secs)} dispatch grace, so this is "
                          f"most likely GitHub not having created it yet."
                          if fresh else
                          f". This is NOT a backlog — a queued run would be visible. GitHub never "
                          f"dispatched one, so the workflow trigger, the ruleset, or the workflow "
                          f"file itself is the thing to check, not the runner."))}


def check_tip_once(*, ops: Ops | None = None, state_dir: Path | None = None,
                   pending_secs: int = DEFAULT_PENDING_SECS,
                   dispatch_grace_secs: int = DEFAULT_DISPATCH_GRACE_SECS,
                   cooldown_secs: int = DEFAULT_COOLDOWN_SECS,
                   dry_run: bool = False) -> dict:
    """One tip-coverage probe. Returns {verdict, detail, tip_sha, alerted}."""
    ops = ops or Ops()
    state_dir = Path(state_dir) if state_dir else _DEFAULT_STATE_DIR
    now = ops.now()

    muted = disabled_reason(state_dir, now, name=_TIP_DISABLE_NAME)
    if muted:
        return _finish(state_dir, {"verdict": DISABLED, "detail": f"authorised quiet: {muted}",
                                   "tip_sha": None, "alerted": False},
                       now, ops, cooldown_secs, dry_run,
                       state_name=_TIP_STATE_NAME, agent=TIP_AGENT, label="ci-audit-tip-coverage")

    tip_sha = ops.origin_tip_sha()
    if tip_sha is None:
        return _finish(state_dir, {
            "verdict": UNKNOWN,
            "detail": ("could not determine origin/main's tip sha, so 'is the tip audited?' is "
                       "unanswerable. Not green."),
            "tip_sha": None, "alerted": False},
            now, ops, cooldown_secs, dry_run,
            state_name=_TIP_STATE_NAME, agent=TIP_AGENT, label="ci-audit-tip-coverage")

    runs = ops.audit_runs_for_sha(tip_sha)
    if runs is None:
        return _finish(state_dir, {
            "verdict": UNKNOWN,
            "detail": (f"origin/main tip is {tip_sha[:9]} but the GitHub run query FAILED (gh "
                       f"missing, not authenticated, {AUDIT_WORKFLOW_FILE} renamed/deleted, or "
                       f"the API errored). Tip coverage is currently unmeasurable — this is NOT "
                       f"a green result, and must not be read as one."),
            "tip_sha": tip_sha, "alerted": False},
            now, ops, cooldown_secs, dry_run,
            state_name=_TIP_STATE_NAME, agent=TIP_AGENT, label="ci-audit-tip-coverage")

    decided = classify_tip_coverage(runs, now=now, tip_sha=tip_sha,
                                    commit_epoch=ops.origin_last_commit_epoch(),
                                    pending_secs=pending_secs,
                                    dispatch_grace_secs=dispatch_grace_secs)
    return _finish(state_dir, {**decided, "tip_sha": tip_sha, "alerted": False},
                   now, ops, cooldown_secs, dry_run,
                   state_name=_TIP_STATE_NAME, agent=TIP_AGENT, label="ci-audit-tip-coverage")


def _finish(state_dir: Path, result: dict, now: int, ops: Ops,
            cooldown_secs: int, dry_run: bool, *, state_name: str = _STATE_NAME,
            agent: str = AGENT, label: str = "ci-audit-liveness") -> dict:
    """Record the verdict; alert if it is an alerting one and the cooldown has lapsed.

    `should_alert` in `result` overrides membership in ALERTING — signal 2 decides alerting by
    AGE (a queued tip is fine at 10m and a fault at 10h), which a verdict name cannot express."""
    state_path = state_dir / state_name
    state = _read_json(state_path)
    verdict = result["verdict"]
    should_alert = result.pop("should_alert", verdict in ALERTING)

    if should_alert and not dry_run:
        last_alert = state.get("last_alert_epoch")
        last_verdict = state.get("last_alert_verdict")
        # A CHANGED verdict always speaks: HOST_DOWN -> STALE is new information about a
        # different fault, and holding it for the cooldown would suppress the more actionable
        # of the two. Only a repeat of the SAME verdict is throttled.
        due = (not isinstance(last_alert, int)
               or last_verdict != verdict
               or now - last_alert >= cooldown_secs)
        if due:
            ops.alert(f"[{label}] {verdict}: {result['detail']}",
                      vocab=f"{label} ({verdict})", agent=agent)
            result["alerted"] = True
            state["last_alert_epoch"] = now
            state["last_alert_verdict"] = verdict

    state["last_verdict"] = verdict
    state["last_check_epoch"] = now
    _write_json(state_path, state)
    return result


def _dur(secs: int) -> str:
    secs = int(secs)
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


def _ago(now: int, then: int) -> str:
    return _dur(max(0, now - then))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    # Default stays `liveness` so recovery's existing timer invocation is unchanged by this addition.
    # The tip probe is opted INTO by the seat that holds GitHub auth, not defaulted on everywhere.
    ap.add_argument("--mode", choices=("liveness", "tip", "both"), default="liveness",
                    help="liveness = runner-side execution (needs ssh to pve2); "
                         "tip = GitHub-side coverage of origin/main's tip (needs gh auth)")
    ap.add_argument("--threshold-secs", type=int, default=DEFAULT_THRESHOLD_SECS)
    ap.add_argument("--pending-secs", type=int, default=DEFAULT_PENDING_SECS)
    ap.add_argument("--dispatch-grace-secs", type=int, default=DEFAULT_DISPATCH_GRACE_SECS)
    ap.add_argument("--cooldown-secs", type=int, default=DEFAULT_COOLDOWN_SECS)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="decide and print; never alert, never touch alert state")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    state_dir = Path(args.state_dir) if args.state_dir else None
    results: dict[str, dict] = {}
    if args.mode in ("liveness", "both"):
        results["liveness"] = check_once(state_dir=state_dir,
                                         threshold_secs=args.threshold_secs,
                                         cooldown_secs=args.cooldown_secs,
                                         dry_run=args.dry_run)
    if args.mode in ("tip", "both"):
        results["tip"] = check_tip_once(state_dir=state_dir,
                                        pending_secs=args.pending_secs,
                                        dispatch_grace_secs=args.dispatch_grace_secs,
                                        cooldown_secs=args.cooldown_secs,
                                        dry_run=args.dry_run)

    # Single-mode output keeps the shape T-2440's callers already parse; --mode both nests.
    result = results[args.mode] if args.mode in results else results
    if args.json:
        print(json.dumps(result))
    else:
        for name, r in results.items():
            _log(f"[{name}] {r['verdict']}: {r['detail']}"
                 + (" [ALERTED]" if r["alerted"] else ""))
    # Exit 0 even on an alerting verdict: the ALERT is the output, and a non-zero exit would
    # make systemd's OnFailure fire a SECOND alert about the monitor that just worked correctly.
    return 0


if __name__ == "__main__":
    sys.exit(main())
