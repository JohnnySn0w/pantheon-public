#!/usr/bin/env python3
"""tools/priority_list_health_watchdog.py — priority-list persistent-broken
health watchdog (T-3020 BT4, ORG-078 §4 + §Follow-through).

`ticket.py list`'s priority-sort path (T-3020 BT3) appends one JSON line per
invocation to `~/.local/log/pantheon/priority_list_health.jsonl`, recording
whether `rulebook/priority-list.md` parsed cleanly (`parse_ok`) and, on
failure, which `priority_list_parse.PriorityListParseError` failure-class
fired (`parse_fail_class`). A single bad read is expected noise (a
concurrent edit mid-save, e.g.) and self-heals on the next `list` call; ORG-078
§4 names the thing actually worth alerting on as a PERSISTENT break: the last
N consecutive entries all broken, with the SAME failure class.

This watchdog reads that tail, classifies persistent-vs-transient, and — on a
persistent-broken verdict — routes an alert into Recovery's ORG-026 mailbox
(fleet-hygiene domain owner; also sidesteps the HO-self-alerting circularity
named in the ADR, since HO drives priority-list edits and may be the source
of the break).

ORG-076's full detector -> durable episode-store -> idempotent-ticket-emission
pipeline (the ADR's aspirational "real ticket, not just a stderr note" framing)
is NOT built: ORG-076 itself is REBOUNDED rev 2 (2026-08-12), rev 3 deferred,
its first build tranche (T-2941) gated on ratification (T-2944). This watchdog
therefore routes via `tools.mailbox.send()` direct to Recovery's inbox — the SAME
primitive the fleet's other ~13 monitors already use for their per-agent
failure alerts (see `credential_expiry_monitor._wake_recovery`,
`acp_bridge_backlog_watchdog._route_dlq_recovery`) — rather than inventing a new
ticket-emission mechanism. When ORG-076's episode-store lands, this module's
`_route_recovery` is the natural swap point.

Pure file-read + one mailbox send — no LLM/API call, so it keeps working
during a full auth outage, mirroring the other fleet watchdogs' resilience
posture.

Usage:
    python3 tools/priority_list_health_watchdog.py --check     # timer path
    python3 tools/priority_list_health_watchdog.py --dry-run   # classify + print, no alert

Default-safe (T-0261): refuses a flagless run (exit 64).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:  # T-4096: the mailbox hub path comes from instance.yaml, never a literal.
    from tools.agent_role_map import mailbox_hub  # noqa: E402
except ImportError:  # bare run with cwd=tools/
    from agent_role_map import mailbox_hub  # type: ignore  # noqa: E402

STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
STATE_PATH = STATE_DIR / "priority_list_health_alert_state.json"

# Matches ticket.py's PRIORITY_LIST_HEALTH_PATH (T-3020 BT3) — the telemetry
# sink this watchdog reads. Not imported from tools.ticket to keep this
# module import-light (no ticket.py dependency chain) and independently
# overridable for tests without touching ticket.py's own module state.
HEALTH_LOG_PATH = Path.home() / ".local" / "log" / "pantheon" / "priority_list_health.jsonl"

RECOVERY_INBOX = os.environ.get("PANTHEON_RECOVERY_INBOX") or f"{mailbox_hub()}/recovery"

# Persistent-broken window: N most-recent telemetry entries, ALL parse_ok=false
# with the SAME parse_fail_class (ORG-078 §4). The health log is written per
# `ticket.py list` invocation, not on a fixed wall-clock cadence — 24 is an
# EVENT-COUNT, not a time window. Chosen large enough to absorb a transient
# mid-edit read (the operator saving rulebook/priority-list.md non-atomically) but
# small enough to trip inside a normal day's multi-persona invocation cadence
# (`inbox_consumer` pull loops + timers + interactive CLI use call `ticket.py
# list` far more than 24 times/day on a live fleet).
PERSISTENT_WINDOW = int(os.environ.get("PANTHEON_PRIORITY_LIST_HEALTH_WINDOW", "24"))

# Re-alert cadence for an UNCHANGED persistent-broken class. A class CHANGE
# always re-alerts regardless of cooldown (see _should_alert) — a different
# failure class is a genuinely new condition worth saying.
ALERT_COOLDOWN_SECONDS = int(
    os.environ.get("PANTHEON_PRIORITY_LIST_HEALTH_COOLDOWN_SECONDS", str(24 * 60 * 60))
)


def _log(msg: str) -> None:
    print(f"[priority_list_health_watchdog ts={int(time.time())}] {msg}", flush=True)


def read_last_entries(path: Path, n: int) -> list[dict]:
    """Return up to the last `n` well-formed JSON objects from `path`
    (JSONL). A missing file returns `[]` (no observations yet — a brand-new
    install has no history, which is a healthy no-op, not a couldn't-check
    alert). Malformed lines are SKIPPED, never raised — telemetry corruption
    on one line must not blind this watchdog to the genuinely-broken entries
    surrounding it. Reads the whole file rather than seeking from the end;
    the health log is a bounded per-CLI-invocation append, not a
    high-volume stream, so this is cheap in practice."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    entries: list[dict] = []
    for line in lines[-n:] if n < len(lines) else lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries[-n:]


def persistent_broken_class(entries: list[dict], window: int) -> str | None:
    """None unless `entries` has >= `window` items AND the last `window` are
    ALL `parse_ok: false` sharing the SAME `parse_fail_class` — the ORG-078
    §4 persistent-broken condition. Returns that shared class string.

    Fewer than `window` total entries -> None (not enough history yet;
    fail-safe against a false positive on a fresh install, mirroring
    ORG-076 §1's open-threshold "N consecutive" episode-open semantics)."""
    if len(entries) < window:
        return None
    tail = entries[-window:]
    classes = {e.get("parse_fail_class") for e in tail if e.get("parse_ok") is False}
    all_broken = all(e.get("parse_ok") is False for e in tail)
    if all_broken and len(classes) == 1:
        (cls,) = classes
        if cls is not None:
            return cls
    return None


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state))
    except OSError as e:
        print(f"[priority_list_health_watchdog] could not persist state: {e}", file=sys.stderr)


def _should_alert(cls: str, now: float, state: dict) -> bool:
    """Re-alert when the persistent-broken CLASS changes (always — a
    different failure class is a genuinely new condition), or when the
    unchanged class's cooldown has elapsed."""
    last = state.get("last") or {}
    if last.get("class") != cls:
        return True
    return (now - last.get("ts", 0)) >= ALERT_COOLDOWN_SECONDS


def _route_recovery(body: str) -> None:
    """Alert Recovery via a direct mailbox send (ORG-078 §4 "goes to Recovery's
    queue" — see module docstring for why this is `mailbox.send`, not a
    ticket-emission call). Lazy import mirrors the other fleet watchdogs'
    convention (e.g. credential_expiry_monitor's `_wake_recovery`)."""
    from tools import mailbox

    mailbox.send(
        RECOVERY_INBOX,
        body,
        "priority_list_health_watchdog",
        subject="priority-list persistently broken",
        severity="urgent",
    )


def check(*, dry_run: bool, now: float | None = None) -> int:
    """Read the telemetry tail; alert (throttled) on a persistent-broken
    verdict. Returns 1 if breached, 0 if healthy (or not enough history)."""
    now = time.time() if now is None else now
    entries = read_last_entries(HEALTH_LOG_PATH, PERSISTENT_WINDOW)
    cls = persistent_broken_class(entries, PERSISTENT_WINDOW)

    if cls is None:
        _log(f"OK: no persistent-broken condition (entries={len(entries)}, window={PERSISTENT_WINDOW})")
        return 0

    _log(f"ALERT: persistent-broken, class={cls}, window={PERSISTENT_WINDOW}")
    body = (
        f"\U0001f4cc [priority-list-health] rulebook/priority-list.md has been "
        f"unparseable ({cls}) for the last {PERSISTENT_WINDOW} `ticket.py list` "
        f"invocations — this is a PERSISTENT break, not a transient/mid-edit read. "
        f"The queue is running on created-ASC FIFO fallback in the meantime "
        f"(ORG-078 §4). Check `~/.local/log/pantheon/priority_list_health.jsonl` "
        f"and validate with `python3 tools/ticket.py priority-list validate`."
    )
    if dry_run:
        _log("[dry-run] would alert Recovery")
        return 1

    state = _load_state()
    if _should_alert(cls, now, state):
        _route_recovery(body)
        state["last"] = {"ts": now, "class": cls}
        _save_state(state)
        _log("alerted Recovery")
    else:
        _log("alert suppressed (cooldown, unchanged class)")
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--check", action="store_true", help="check health + alert on persistent breach (timer path)")
    p.add_argument("--dry-run", action="store_true", help="classify + print, send no alert")
    args = p.parse_args(argv)

    if not (args.check or args.dry_run):
        print(
            "ERROR: refusing to run with no mode flag. Pass --check (timer path) "
            "or --dry-run to preview without alerting.",
            file=sys.stderr,
        )
        return 64

    check(dry_run=args.dry_run)
    # Exit 0 always: a monitor must not fail its own timer. Breach state is in the log.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
