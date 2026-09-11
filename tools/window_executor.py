#!/usr/bin/env python3
"""Pantheon maintenance-window executor — the D13 queue-drainer.

Implements the MECHANISM half of T-2876 (D13 "Request the window; don't wait
to be told", `rulebook/directives.md` D13). D13 says an agent that needs a
maintenance/coordination *window* REQUESTS it (by tagging the ticket with a
`event:window:<param>` anchor, per T-2833) rather than idling until told to go.
This tool is the drainer over that request queue:

    queue = every ticket in inbox/ or active/ whose `not_before_anchor`
            begins with `event:window:` (NOT `event:cadence:` — those are
            periodic re-checks, never disruptive windows, per D13).

For each queued request it resolves two axes and reports the disposition:

  1. WINDOW-OPEN?  Is `not_before` reached (the requested timing arrived)?
  2. ENVELOPE?     Under the ALREADY-RATIFIED proven-rollback bar (T-2664,
                   the grant line D13 rides — this tool creates NO new
                   authorization):
                     - AUTO-DRAIN  — reversible with a PROVEN rollback
                       (trust-posture tiers b/c). The agent self-grants the
                       window; the executor would run it health-gated,
                       one-at-a-time, rollback-staged, and report. A ticket
                       opts in ONLY by carrying the `window-auto-drain` label.
                     - RESERVED    — irreversible / rollback-unproven (tier a,
                       e.g. a never-exercised recovery live-fire). Routes to
                       the operator's queue as a READY-TO-EXECUTE request (fully
                       speced: action, timing, rollback), not an open ask.
                   FAIL-SAFE: absence/ambiguity ⇒ RESERVED. A task auto-runs
                   only on an explicit, deliberate opt-in.

## Increment scope (report-only)

This is increment 1: the queue reader + envelope classifier + reporter. It
READS tickets and REPORTS what would happen. It does NOT execute any
disruptive action — `execute_item()` is an inert stub that refuses, and the
`--execute` flag is hard-gated off. Auto-execution of disruptive actions
(health-gate wiring, per-task rollback staging, systemd-timer install) is
increment 2, a focused/attentive build — deliberately NOT bundled into a
report-only tool, per D12 (do-it-right) and the T-2876 "not a tail-burst" note.
Because this increment only reads and reports, it is fully reversible.

CLI:
    python3 tools/window_executor.py                 # scan + report (dry-run)
    python3 tools/window_executor.py --json          # machine-readable plan
    python3 tools/window_executor.py --execute       # REFUSED (increment 2)

ORG-064-aligned, not ORG-064-blocked: designed to fold into the ORG-064
General Scheduler's reconciler (T-2662) when that lands, but rides plain
systemd timers until then (per the operator TG 7625 — no cron). No timer is
installed by this increment.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Make `from tools.ticket import ...` work both as a script
# (`python3 tools/window_executor.py`) and as a package import
# (`python3 -m unittest tools.test_window_executor`). Mirrors the bootstrap in
# ticket_grooming.py: script mode prepends `tools/` not the repo root.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.ticket import parse_ticket  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TICKETS_ROOT = REPO_ROOT / "tickets"

# The anchor prefix that marks a maintenance-window REQUEST (T-2833 / D13).
# `event:cadence:` anchors (periodic re-checks) are deliberately excluded —
# they are never drained as disruptive windows.
WINDOW_ANCHOR_PREFIX = "event:window:"
CADENCE_ANCHOR_PREFIX = "event:cadence:"

# A ticket opts INTO auto-draining (envelope=AUTO_DRAIN) only by carrying this
# label, asserting it is reversible-with-proven-rollback per T-2664. Absence ⇒
# RESERVED (fail-safe). Chosen as a LABEL, not a new frontmatter field, to
# avoid colliding with the ORG-065 BT1 typed-edge schema work (T-2841) and
# because labels are already free-form.
AUTO_DRAIN_LABEL = "window-auto-drain"

# States whose tickets can carry a live window request. `done/` is terminal;
# grooming/consumer conventions never drain it.
STATES_TO_DRAIN = ("inbox", "active")

ENVELOPE_AUTO_DRAIN = "auto-drain"
ENVELOPE_RESERVED = "reserved"


def _parse_iso(raw: str) -> datetime:
    """Parse a full ISO-8601 UTC timestamp (`...Z` or `+00:00`) to an aware
    datetime. Window `not_before` values are always full timestamps."""
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class WindowItem:
    """One maintenance-window request drawn from the queue."""

    tid: str
    title: str
    state: str
    target: str
    anchor: str
    param: str  # the `<param>` after event:window:
    not_before: str | None
    labels: list[str] = field(default_factory=list)
    window_open: bool = False
    envelope: str = ENVELOPE_RESERVED

    @property
    def disposition(self) -> str:
        """The action the executor WOULD take (report-only: 'would')."""
        if self.envelope == ENVELOPE_AUTO_DRAIN:
            if self.window_open:
                return (
                    "WOULD AUTO-DRAIN — execute health-gated, one-at-a-time, "
                    "rollback-staged, report (increment 2)"
                )
            return f"queued — auto-drain when window opens (not_before {self.not_before})"
        # RESERVED
        return "route to the operator's queue as a ready-to-execute request"


def _iter_ticket_paths(tickets_root: Path):
    for state in STATES_TO_DRAIN:
        d = tickets_root / state
        if not d.exists():
            continue
        for entry in sorted(d.iterdir()):
            if entry.is_file() and entry.name.startswith("T-") and entry.suffix == ".md":
                yield state, entry


def scan_window_queue(
    tickets_root: Path = TICKETS_ROOT, *, now: datetime | None = None
) -> list[WindowItem]:
    """Return the maintenance-window request queue, one WindowItem per ticket
    carrying an `event:window:` anchor in inbox/ or active/.

    `now` is injectable for deterministic tests; defaults to current UTC.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    items: list[WindowItem] = []
    for state, path in _iter_ticket_paths(tickets_root):
        try:
            ticket = parse_ticket(path)
        except Exception:
            # A malformed ticket must never crash the drainer; skip it. The
            # ticket_lint suite is the surface that flags parse failures.
            continue

        anchor = ticket.fields.get("not_before_anchor")
        if not isinstance(anchor, str) or not anchor.startswith(WINDOW_ANCHOR_PREFIX):
            continue  # not a window request (cadence anchors fall out here too)

        labels = ticket.fields.get("labels") or []
        if not isinstance(labels, list):
            labels = []

        not_before = ticket.fields.get("not_before")
        window_open = False
        if isinstance(not_before, str) and not_before.strip():
            try:
                window_open = _parse_iso(not_before) <= now
            except Exception:
                # Unparseable date ⇒ treat window as NOT open (fail-safe: never
                # auto-run on a date we can't read).
                window_open = False
        else:
            # No not_before but a window anchor: the request has no timing gate,
            # so the window is considered open (the anchor alone is the request).
            window_open = True

        envelope = (
            ENVELOPE_AUTO_DRAIN if AUTO_DRAIN_LABEL in labels else ENVELOPE_RESERVED
        )

        items.append(
            WindowItem(
                tid=path.stem,
                title=ticket.title,
                state=state,
                target=str(ticket.fields.get("target") or "?"),
                anchor=anchor,
                param=anchor[len(WINDOW_ANCHOR_PREFIX):],
                not_before=not_before if isinstance(not_before, str) else None,
                labels=labels,
                window_open=window_open,
                envelope=envelope,
            )
        )
    return items


def health_gate() -> bool:
    """Per-task pre-execution health gate (D13 mandatory safety). Inert in
    increment 1 — the real fleet-health probe is wired in increment 2. Present
    so the execution shape is established, not so it runs."""
    return True


def execute_item(item: WindowItem) -> None:
    """Execute an auto-drainable window request (health-gated, rollback-staged).

    INTENTIONALLY UNIMPLEMENTED in increment 1. Auto-execution of disruptive
    actions is a focused/attentive build (increment 2), not a report-only tool.
    Refusing here is the explicit off-by-default gate that keeps this increment
    fully reversible.
    """
    raise NotImplementedError(
        f"window auto-execution is not implemented in this increment "
        f"(would drain {item.tid}); increment 2 wires health-gate + rollback "
        f"staging + reporting. This tool is report-only."
    )


def format_report(items: list[WindowItem]) -> str:
    """Human-readable dry-run report of the window queue."""
    if not items:
        return "[window-executor] queue empty — no event:window: requests in inbox/active."

    auto = [i for i in items if i.envelope == ENVELOPE_AUTO_DRAIN]
    reserved = [i for i in items if i.envelope == ENVELOPE_RESERVED]
    open_auto = [i for i in auto if i.window_open]

    lines = [
        "[window-executor] maintenance-window queue (report-only — nothing executed)",
        f"  {len(items)} request(s): {len(auto)} auto-drain, {len(reserved)} reserved→the operator; "
        f"{len(open_auto)} auto-drainable window(s) currently OPEN",
        "",
    ]
    for i in items:
        flag = "OPEN " if i.window_open else "waiting"
        lines.append(f"  {i.tid} [{i.state}] envelope={i.envelope} window={flag}")
        lines.append(f"      window:{i.param}  →  {i.disposition}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--json", action="store_true", help="emit the plan as JSON instead of text"
    )
    ap.add_argument(
        "--execute",
        action="store_true",
        help="REFUSED in this increment (auto-execution is increment 2)",
    )
    args = ap.parse_args(argv)

    if args.execute:
        print(
            "[window-executor] --execute is not available in this increment. "
            "Auto-execution of disruptive window actions (health-gate, rollback "
            "staging, per-task reporting, timer install) is a focused increment-2 "
            "build. This tool is report-only; nothing was executed.",
            file=sys.stderr,
        )
        return 2

    items = scan_window_queue()
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "tid": i.tid,
                        "title": i.title,
                        "state": i.state,
                        "target": i.target,
                        "anchor": i.anchor,
                        "param": i.param,
                        "not_before": i.not_before,
                        "labels": i.labels,
                        "window_open": i.window_open,
                        "envelope": i.envelope,
                        "disposition": i.disposition,
                    }
                    for i in items
                ],
                indent=2,
            )
        )
    else:
        print(format_report(items))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
