#!/usr/bin/env python3
"""Closure-gate health monitor (T-2845 / ORG-065 BT5 flip prerequisite).

Closes the ORG-068 "health-monitor" gap the T-2842 reviewer named when the
closure-validation gate (ORG-065 §3.3) landed warn-only: nothing observed
(a) gate-mode DRIFT — is the effective PANTHEON_CLOSURE_GATE_MODE the mode
ORG-065's sequencing says should be active right now — or (b) finding-VOLUME
TREND — how many closure-gate findings warn-mode is currently emitting over
the corpus, so a human can see the corpus converging toward zero findings
BEFORE the validator flips to gating.

Detection is a pure function in tools/ticket_lint.py
(detect_closure_gate_health) — this module is the thin enforcement-adjacent
wrapper: it resolves the REAL effective mode (via tools.ticket's own
_closure_gate_mode(), so this monitor can never drift from what resolve-time
enforcement actually computes), prints stable greppable lines, and
optionally persists one JSONL snapshot per run to a durable per-user state
file so multiple runs form a TREND, not just a single number.

CONSUMER CONTRACT for the T-2845 flip leg (or any other reader): read the
`open_*` fields/tokens (inbox+active only) for a flip-readiness go/no-go
signal — that is the slice the gate will actually fire on once flipped. The
`corpus_*` / `by_state` fields cover every ticket state including `done` and
track full-corpus migration-walk progress; they are NOT a flip-readiness
number on their own (a done ticket's stale finding is migration debt, not
something the gate will ever re-evaluate).

Output line prefix is deliberately `[closure-gate-health]`, distinct from
ticket.py's own resolve-time `[closure-gate] <tid> <kind>:` warn lines (see
ticket.py's _check_closure_gate docstring) — the two are different data
sources (one ticket's resolve vs. a corpus-wide re-scan) and must not be
cross-matched by a naive grep for `[closure-gate]`.

Usage:
    python3 tools/closure_gate_health_monitor.py                    # print only
    python3 tools/closure_gate_health_monitor.py --record           # print + append trend
    python3 tools/closure_gate_health_monitor.py --repo-root <path> # redirect (tests/smoke)

No T-0260/261-style "refuse bare invocation" default-safe gate: that
contract guards commit/push-to-origin and Telegram-alert actions, neither of
which this tool has. Its only side effect — appending to a local per-user
state file — is opt-in via --record; the print-only default is inherently
safe (read-only corpus scan, no repo mutation, no network call).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import ticket_lint
from tools.ticket import _closure_gate_mode  # noqa: E402 — see module docstring

REPO_ROOT = Path(__file__).resolve().parent.parent

# T-2845 flip leg: the mode ORG-065's sequencing says SHOULD be active right
# now. FLIPPED warn->gate 2026-08-12 (T-2934: migration walk pass 1 + live-fire
# both green). Update this constant together with ticket.py's
# _closure_gate_mode() default — the pair is the flip; rollback = revert both
# — so drift detection stays meaningful post-flip instead of permanently
# alarming. Fail-safe direction on a garbage env value remains warn
# (fail-open); the drift line here is what catches a silent demotion.
INTENDED_MODE = "gate"

STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
STATE_PATH = STATE_DIR / "closure_gate_health.jsonl"


def run(repo_root: Path) -> ticket_lint.ClosureGateHealthReport:
    """Compute one health snapshot against `repo_root`. Reads the REAL
    effective PANTHEON_CLOSURE_GATE_MODE via tools.ticket._closure_gate_mode()
    (single source of truth — see module docstring)."""
    return ticket_lint.detect_closure_gate_health(
        repo_root / "tickets",
        repo_root / "architecture" / "adr",
        effective_mode=_closure_gate_mode(),
        intended_mode=INTENDED_MODE,
    )


def _kinds(d: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in sorted(d.items())) or "none"


def _print_report(report: ticket_lint.ClosureGateHealthReport) -> None:
    print(
        f"[closure-gate-health] mode effective={report.effective_mode} "
        f"intended={report.intended_mode} "
        f"drift={'yes' if report.mode_drift else 'no'}"
    )
    print(
        f"[closure-gate-health] open scanned={report.open_tickets_scanned} "
        f"with_obligations={report.open_tickets_with_obligations} "
        f"with_findings={report.open_tickets_with_findings} "
        f"total_findings={report.open_total_findings} "
        f"by_kind={_kinds(report.open_findings_by_kind)}"
    )
    print(
        f"[closure-gate-health] corpus scanned={report.corpus_tickets_scanned} "
        f"with_obligations={report.corpus_tickets_with_obligations} "
        f"with_findings={report.corpus_tickets_with_findings} "
        f"total_findings={report.corpus_total_findings} "
        f"by_kind={_kinds(report.corpus_findings_by_kind)}"
    )
    for st, counts in sorted(report.by_state.items()):
        print(
            f"[closure-gate-health] by_state state={st} "
            f"scanned={counts['scanned']} with_obligations={counts['with_obligations']} "
            f"with_findings={counts['with_findings']} total_findings={counts['total_findings']}"
        )


def _record(report: ticket_lint.ClosureGateHealthReport, state_path: Path) -> None:
    """Append one JSON line to `state_path` — the durable trend record. A
    single run's number is a snapshot; repeated appends over time are what
    let the flip leg see the corpus converging (or not) toward zero
    findings. Uses dataclasses.asdict so every report field round-trips
    without hand-duplicating field names (a manual dict here would itself be
    a drift risk against the dataclass)."""
    import dataclasses

    entry = dataclasses.asdict(report)
    entry["ts"] = int(time.time())
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="closure_gate_health_monitor.py",
        description=(
            "Closure-gate corpus-health snapshot (T-2845 / ORG-065 BT5): "
            "gate-mode drift + finding-volume trend, ahead of the warn->gate flip."
        ),
    )
    p.add_argument(
        "--repo-root", type=Path, default=REPO_ROOT,
        help="redirect the corpus scan to a tmp pantheon-config dir (tests/smoke)",
    )
    p.add_argument(
        "--record", action="store_true",
        help="append this run's snapshot to the durable trend file "
             f"(default: {STATE_PATH}); without this flag, print only",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = run(args.repo_root.resolve())
    _print_report(report)
    if args.record:
        _record(report, STATE_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
