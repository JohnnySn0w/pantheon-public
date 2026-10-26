#!/usr/bin/env python3
"""Dispatch the byte-sequence audit on main when main's tip is not yet covered (T-5354).

WHY THIS EXISTS. T-5001 took `push:` off hermes-audit.yml and put main on a `*/15` cron. GitHub
does not keep that cron on this repo: by 2026-10-22 08:40Z, two hours after the merge, the new
schedule had never fired, and ci.yml's `0 9` nightly has been firing 5-7h late
(14:23Z / 14:10Z / 16:17Z on 10-21 / 10-20 / 10-19). A schedule that late cannot hold the
tip-coverage monitor's 60 min grace. `workflow_dispatch` events are created promptly, so this
timer turns the cadence into one we own. The cron stays in the workflow as a backstop; when it
does fire, this script finds the tip covered and does nothing.

ONE TICK, BOUNDED:
  1. Fetch origin/main's tip.
  2. Covered (a successful main audit whose head contains the tip) -> nothing to do.
  3. A main audit already queued or in progress -> nothing to do; it will cover the tip or the
     next tick will dispatch again. At most one dispatch is ever outstanding.
  4. Otherwise dispatch hermes-audit.yml on main.
Anything it cannot measure (gh missing, API error) is a SKIP, not a dispatch: the tip-coverage
monitor is the instrument that reports an unaudited tip, and it alerts on UNKNOWN itself.

This is an ACTUATOR, kept apart from pantheon-ci-audit-tip-coverage on purpose. That unit is
detect + alert only and stays so; if this one dies, the monitor still pages.

Runs on the `claude` seat only: it needs gh auth, and recovery holds none (see the tip-coverage unit).
Exit 0 on every non-dispatch outcome; exit 1 only when a dispatch was attempted and failed, so the
unit's OnFailure alert fires on the case that needs a look.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_audit_liveness as cal  # noqa: E402  (shares Ops: tip read + watermark coverage)

COVERED, IN_FLIGHT, DISPATCHED, SKIP, DISPATCH_FAILED = (
    "COVERED", "IN_FLIGHT", "DISPATCHED", "SKIP", "DISPATCH_FAILED")


def _log(msg: str) -> None:
    print(f"[hermes_audit_dispatch] {msg}", file=sys.stderr)


class Ops(cal.Ops):
    def runs_in_flight(self) -> int | None:
        """How many FRESH main audit runs are queued or in progress (any event), or None if unknown.

        A run queued longer than STALE_IN_FLIGHT_SECS is not counted: on 2026-10-22 a push run
        from 2026-09-13 still read `queued`, and counting it would block every dispatch forever.
        A real backlog that old is the tip-coverage monitor's to report, not this script's to wait on."""
        now = self.now()
        total = 0
        for status in ("queued", "in_progress"):
            endpoint = (f"repos/{cal.GH_REPO}/actions/workflows/{cal.AUDIT_WORKFLOW_FILE}/runs"
                        f"?branch=main&status={status}&per_page=100")
            try:
                r = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True,
                                   timeout=cal.GH_TIMEOUT)
                if r.returncode != 0:
                    return None
                runs = json.loads(r.stdout).get("workflow_runs")
            except (subprocess.TimeoutExpired, OSError, ValueError, AttributeError):
                return None
            if not isinstance(runs, list):
                return None
            total += count_fresh(runs, now=now)
        return total

    def dispatch(self) -> bool:
        try:
            r = subprocess.run(["gh", "workflow", "run", cal.AUDIT_WORKFLOW_FILE, "--ref", "main",
                                "-R", cal.GH_REPO], capture_output=True, text=True,
                               timeout=cal.GH_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError) as exc:
            _log(f"dispatch failed: {exc!r}")
            return False
        if r.returncode != 0:
            _log(f"dispatch failed rc={r.returncode}: {r.stderr.strip()[:300]}")
            return False
        return True


STALE_IN_FLIGHT_SECS = 2 * 3600


def count_fresh(runs: list, *, now: int, stale_secs: int = STALE_IN_FLIGHT_SECS) -> int:
    """Runs created within `stale_secs` of `now`. An unparseable timestamp counts as fresh
    (the safe side: it can only delay a dispatch, never add one)."""
    fresh = 0
    for run in runs:
        if not isinstance(run, dict):
            continue
        created = cal._parse_iso_z(run.get("created_at"))
        if created is None or now - created < stale_secs:
            fresh += 1
    return fresh


def tick(ops: Ops, *, dry_run: bool = False) -> dict:
    """One bounded tick. Returns {verdict, tip_sha, detail}."""
    tip = ops.origin_tip_sha()
    if tip is None:
        return {"verdict": SKIP, "tip_sha": None, "detail": "could not read origin/main"}
    wm = ops.watermark_coverage(tip)
    if wm is None:
        return {"verdict": SKIP, "tip_sha": tip, "detail": "coverage unmeasurable"}
    if wm.get("covered_by"):
        return {"verdict": COVERED, "tip_sha": tip,
                "detail": f"covered by run {wm['covered_by'].get('id')}"}
    flying = ops.runs_in_flight()
    if flying is None:
        return {"verdict": SKIP, "tip_sha": tip, "detail": "in-flight count unmeasurable"}
    if flying:
        return {"verdict": IN_FLIGHT, "tip_sha": tip, "detail": f"{flying} run(s) queued/running"}
    if dry_run:
        return {"verdict": DISPATCHED, "tip_sha": tip, "detail": "dry run: would dispatch"}
    if ops.dispatch():
        return {"verdict": DISPATCHED, "tip_sha": tip, "detail": "dispatched on main"}
    return {"verdict": DISPATCH_FAILED, "tip_sha": tip, "detail": "gh workflow run failed"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="decide, but do not dispatch")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    result = tick(Ops(), dry_run=args.dry_run)
    tip = (result["tip_sha"] or "?")[:9]
    _log(f"{result['verdict']} tip={tip}: {result['detail']}")
    if args.json:
        print(json.dumps(result))
    return 1 if result["verdict"] == DISPATCH_FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
