"""tools/urgent_escalation.py — the escalation-ladder leg of severity=urgent
(T-1641, ORG-026 Phase 2; T-1640 design note §"Urgent path" item 2).

WHAT THIS IS FOR. The hook-injection leg (tools/hooks/urgent-mail-inject.py)
covers a working Claude-Code agent. It cannot cover: a wedged/capped seat that
never takes another tool call, or codex (no PreToolUse parity). This module is
the backstop for both — "urgent mail unconsumed after N minutes additionally
fires the existing route_failure_alert (-> Recovery/the operator), so a wedged/capped/
codex recipient never silently eats an urgent message" (design note). It reuses
`inbox_consumer.route_failure_alert` — the existing alert path — deliberately;
this file invents no new delivery mechanism.

DELIBERATELY NOT `mail_staleness.py`/`mail_staleness_monitor.py`. That pair is
READ-ONLY-BY-CONTRACT, Recovery-hosted, and needs cross-account tmux pane capture
because its whole design is "idle + unread accumulates" — it EXPLICITLY REJECTS
a pure duration threshold (see its own module docstring: "unconditional
busy-suppression is structurally identical to the T-2158 defect"). Urgent's
requirement inverts that: "acting on stale state is worse than interrupting"
means escalate regardless of whether the pane looks busy. That is a different
predicate needing no pane capture at all (filesystem-only), so it is a small
sibling module, not an extension of a module whose design explicitly excludes
this shape.

READ-ONLY toward the mailbox itself, same discipline as mail_staleness_monitor:
this module NEVER kicks, consumes, acks, or moves a message — it only reads
new/ and writes its OWN `.urgent_escalated/` sidecar (which marker per already-
alerted message id, so a re-run does not re-alert for the same message).

BUILD-NOT-ACTIVATE (T-1641): `run()` is a flag-gated no-op
(`mailbox.urgent_phase2_enabled()`, same kill switch as the rest of Phase 2) and
this module is NOT installed as a timer/cron in this ticket — both are the
separate, operator-coordinated activation step.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterable, Mapping

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import mailbox  # noqa: E402
import mailbox_wake  # noqa: E402 (deliberately import-light; see its own docstring)

# How long unconsumed urgent mail sits before the escalation ladder fires, once
# Phase 2 is enabled. Env-overridable, matching the rest of this file's family.
#
# Derivation: urgent's whole design goal is to beat the 5-min timer-only baseline
# (T-1642 correction — mailbox_wake's normal path has no faster mechanism than
# that sweep). 600s is already 2x that baseline with drain margin, so anything
# STILL unconsumed past it despite bypassing debounce (mailbox_wake) and
# hook-injecting (urgent-mail-inject.py) is itself the anomaly signal — a
# wedged/capped seat or codex's structural no-hook-parity gap — not ordinary
# latency. Same "~2x floor" shape mail_staleness.py uses for its own WARN tier,
# scaled down because urgent is supposed to be faster than normal mail, not
# equally slow.
URGENT_ESCALATION_THRESHOLD_SECONDS = float(
    os.environ.get("PANTHEON_MAILBOX_URGENT_ESCALATION_SECONDS", "600")
)

# Vocab passed to route_failure_alert — a distinct string from every other
# caller's vocab so Recovery/the operator can tell this class of alert apart at a glance.
ALERT_VOCAB = "urgent-unconsumed"

_ESCALATED_DIR = ".urgent_escalated"

# Bound the per-inbox scan so a pathological backlog cannot make one run
# unbounded — the same defensive-cap shape as mailbox.py's _DEDUPE_SCAN_CAP /
# mailbox_wake's _URGENT_SCAN_CAP.
_SCAN_CAP = 1000


# --------------------------------------------------------------------- pure core
def stale_urgent(
    records: Iterable[Mapping],
    *,
    now: float,
    threshold_seconds: float,
    already_alerted,
) -> list[Mapping]:
    """The subset of `records` (as read from an inbox's new/ — i.e. already
    unconsumed by definition) that are severity=="urgent", older than
    `threshold_seconds`, and not already in `already_alerted` (a container
    supporting `in`, keyed by record "id").

    Pure: no I/O, no clock, no mailbox knowledge beyond the record shape. A
    record missing "ts_ns" is skipped rather than guessed at — "cannot compute
    age" must never silently become "assume stale" or "assume fresh".
    """
    out: list[Mapping] = []
    for rec in records:
        if rec.get("severity", "normal") != "urgent":
            continue
        rid = rec.get("id")
        if rid is None or rid in already_alerted:
            continue
        ts_ns = rec.get("ts_ns")
        if ts_ns is None:
            continue
        age = now - (ts_ns / 1_000_000_000)
        if age >= threshold_seconds:
            out.append(rec)
    return out


# ------------------------------------------------------------------- impure shell
def _escalated_dir(inbox: Path) -> Path:
    return inbox / _ESCALATED_DIR


def _already_alerted(inbox: Path) -> set[str]:
    try:
        return set(os.listdir(_escalated_dir(inbox)))
    except OSError:
        return set()


def _mark_escalated(inbox: Path, msg_id: str) -> None:
    """Best-effort; a failure here means at most a duplicate re-alert on the next
    run (route_failure_alert's own vocab-throttle already dampens that), never a
    lost escalation."""
    try:
        d = _escalated_dir(inbox)
        d.mkdir(parents=True, exist_ok=True)
        (d / msg_id).write_text("")
    except OSError:
        pass


def _prune_escalated(inbox: Path, live_ids: set[str]) -> None:
    """Remove `.urgent_escalated/` markers for messages no longer in new/ (drained
    or otherwise gone) — same orphan-sidecar hygiene as mailbox.py's
    _prune_orphan_dlq_sidecars. Best-effort: a stale marker is harmless clutter
    (message names are globally unique), never a correctness issue."""
    try:
        names = os.listdir(_escalated_dir(inbox))
    except OSError:
        return
    for name in names[:_SCAN_CAP]:
        if name not in live_ids:
            try:
                (_escalated_dir(inbox) / name).unlink()
            except OSError:
                continue


def _read_new_records(inbox: Path) -> list[dict]:
    """Best-effort parse of every message currently in `inbox`/new/. A malformed
    file contributes nothing and never breaks the sweep (same posture as
    mailbox_wake's _senders/_queued_subjects)."""
    out: list[dict] = []
    try:
        paths = [p for p in (inbox / "new").iterdir() if p.is_file()]
    except OSError:
        return out
    for p in paths[:_SCAN_CAP]:
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _iter_inboxes(hub: Path) -> Iterable[Path]:
    """Every maildir directly under `hub`, found BY FILESYSTEM, never by a persona
    list — mail_staleness_monitor.py's own stated principle (a persona-keyed
    enumeration missed a real non-persona inbox once already; see its
    docstring)."""
    try:
        candidates = sorted(hub.iterdir())
    except OSError:
        return
    for c in candidates:
        if mailbox.exists(c):
            yield c


def _alert(agent: str, msg: str) -> None:
    """Forward to the EXISTING route_failure_alert path (T-1641 brief: reuse it,
    invent nothing new). Lazy/late import — inbox_consumer carries heavy
    side-effect surface (live Telegram, tmux) that a read-only sweep module
    should not pay at import time, matching every other caller of this function
    in this codebase (ci_audit_liveness.py, ...)."""
    try:
        from tools.inbox_consumer import route_failure_alert
    except ImportError:
        from inbox_consumer import route_failure_alert  # type: ignore
    route_failure_alert(agent, msg, ALERT_VOCAB)


def _format_alert(agent: str, rec: Mapping) -> str:
    subject = rec.get("subject") or "(no subject)"
    sender = rec.get("sender") or "?"
    return (
        f"mailbox: {agent} has an URGENT message from {sender} ('{subject}') "
        f"unconsumed after {URGENT_ESCALATION_THRESHOLD_SECONDS:.0f}s — hook-"
        f"injection and mailbox_wake's debounce-bypass should have surfaced this "
        f"already; a wedged/capped seat or a no-hook-parity harness (codex) is "
        f"the likely cause (T-1641 escalation ladder)"
    )


def run(*, hub: Path | None = None, now: float | None = None,
       dry_run: bool = False) -> dict:
    """Evaluate every hub maildir once; escalate stale unconsumed urgent mail.

    Flag-gated no-op (see module docstring) — returns {"enabled": False,
    "escalated": []} immediately if `mailbox.urgent_phase2_enabled()` is False,
    without touching disk at all.

    `dry_run=True` observes and reports without alerting or marking anything
    escalated (mirrors mail_staleness_monitor.py's own --dry-run contract).
    """
    if not mailbox.urgent_phase2_enabled():
        return {"enabled": False, "escalated": []}
    now = time.time() if now is None else now
    hub = Path(hub) if hub is not None else mailbox_wake.MAILBOX_HUB
    escalated: list[dict] = []
    for inbox in _iter_inboxes(hub):
        records = _read_new_records(inbox)
        live_ids = {r.get("id") for r in records if r.get("id")}
        if not dry_run:
            _prune_escalated(inbox, live_ids)
        already = _already_alerted(inbox)
        hits = stale_urgent(records, now=now,
                            threshold_seconds=URGENT_ESCALATION_THRESHOLD_SECONDS,
                            already_alerted=already)
        agent = inbox.name
        for rec in hits:
            if not dry_run:
                _alert(agent, _format_alert(agent, rec))
                _mark_escalated(inbox, rec["id"])
            escalated.append({"inbox": agent, "id": rec.get("id"),
                              "sender": rec.get("sender"),
                              "subject": rec.get("subject")})
    return {"enabled": True, "escalated": escalated}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Escalate stale unconsumed urgent mail (T-1641 Ph2)."
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="observe + report, do not alert or mark escalated")
    ap.add_argument("--hub", default=None, help="override the mailbox hub path")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    result = run(hub=Path(args.hub) if args.hub else None, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result))
    else:
        if not result["enabled"]:
            print("[urgent_escalation] Phase 2 disabled — no-op")
        elif not result["escalated"]:
            print("[urgent_escalation] nothing stale")
        else:
            for e in result["escalated"]:
                print(f"[urgent_escalation] {e['inbox']}: {e['id']} "
                     f"from {e['sender']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
