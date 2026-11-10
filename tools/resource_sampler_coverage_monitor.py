#!/usr/bin/env python3
"""Independent freshness guard for resource_sampler evidence (T-6211).

The sampler intentionally swallows failures and has no OnFailure dependency so it
can keep collecting during host pressure.  This separate, low-cadence monitor reads
only the newest record and alerts Recovery when that evidence stream goes stale.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import sys
import tempfile
import time
from pathlib import Path

# The systemd unit executes this file by path, so sys.path[0] is ``tools/``.
# Add the repository parent before any late ``tools.*`` import (the alert path).
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LOG_PATH = Path.home() / ".local/state/pantheon/resource_samples.jsonl"
STATE_PATH = Path.home() / ".local/state/pantheon/resource_sampler_coverage_state.json"
STALE_AFTER_SECONDS = int(os.environ.get("PANTHEON_RESOURCE_SAMPLER_STALE_SECONDS", "360"))
FUTURE_SKEW_SECONDS = 60
MAX_TAIL_BYTES = int(os.environ.get("PANTHEON_RESOURCE_SAMPLER_MAX_TAIL_BYTES", "65536"))
PROBLEMS = frozenset({"MISSING", "MALFORMED", "STALE"})
MAX_DELIVERY_ATTEMPTS = 2
DELIVERY_ID_RE = re.compile(r"\d{20}\.\d+\.\d{6}\.resource-sampler-coverage")
INCIDENT_KEY_RE = re.compile(r"[0-9a-f]{32}")


def _last_nonempty_line(path: Path) -> str:
    with path.open("rb") as fh:
        fh.seek(0, 2)
        end = fh.tell()
        if end == 0:
            return ""
        start = max(0, end - MAX_TAIL_BYTES)
        preceding = b""
        if start:
            fh.seek(start - 1)
            preceding = fh.read(1)
        fh.seek(start)
        chunk = fh.read(MAX_TAIL_BYTES)
    lines = chunk.splitlines()
    # A bounded read may begin inside a record. Never parse that fragment as a
    # complete record; a too-large final record therefore becomes MALFORMED.
    # The byte BEFORE the window decides this, not chunk[0]: when a complete
    # newest record is exactly MAX_TAIL_BYTES, chunk begins with its first byte
    # and the preceding byte is the older record's newline.
    if start and preceding not in (b"\n", b"\r"):
        lines = lines[1:]
    for line in reversed(lines):
        if line.strip():
            return line.decode("utf-8")
    return ""


def classify(path: Path = LOG_PATH, *, now: float | None = None) -> tuple[str, str]:
    now = time.time() if now is None else now
    try:
        tail = _last_nonempty_line(path)
    except FileNotFoundError:
        return "MISSING", f"sample log missing: {path}"
    except (OSError, UnicodeError) as exc:
        return "MALFORMED", f"sample log unreadable: {exc}"
    if not tail:
        return "MALFORMED", "sample log is empty"
    try:
        record = json.loads(tail)
        stamp = record.get("ts") if isinstance(record, dict) else None
        if (not isinstance(stamp, (int, float)) or isinstance(stamp, bool)
                or not math.isfinite(float(stamp))):
            raise ValueError("record has no numeric ts")
    except (ValueError, json.JSONDecodeError) as exc:
        return "MALFORMED", f"newest sample is unrecognizable: {exc}"
    age = now - float(stamp)
    if age < -FUTURE_SKEW_SECONDS:
        return "MALFORMED", f"newest sample timestamp is {int(-age)}s in the future"
    if age > STALE_AFTER_SECONDS:
        return "STALE", f"newest sample is {int(age)}s old (> {STALE_AFTER_SECONDS}s)"
    return "HEALTHY", f"newest sample is {max(0, int(age))}s old"


def load_state(path: Path = STATE_PATH) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return {}
        incident_open = value.get("incident_open") is True
        last_class_value = value.get("last_class")
        last_class = (last_class_value if isinstance(last_class_value, str)
                      and last_class_value in PROBLEMS | {"HEALTHY"} else None)
        attempts_value = value.get("delivery_attempts", 0)
        attempts = (min(MAX_DELIVERY_ATTEMPTS, max(0, attempts_value))
                    if isinstance(attempts_value, int)
                    and not isinstance(attempts_value, bool) else 0)
        payload = value.get("alert_payload")
        payload = payload if isinstance(payload, str) and payload.strip() else None
        incident_key = value.get("incident_key")
        incident_key = (incident_key if isinstance(incident_key, str)
                        and INCIDENT_KEY_RE.fullmatch(incident_key) else None)
        delivered_id = value.get("delivered_id")
        delivered_id = (delivered_id if isinstance(delivered_id, str)
                        and DELIVERY_ID_RE.fullmatch(delivered_id) else None)
        # Terminal evidence is meaningful only as part of a complete open
        # incident. Malformed combinations rearm rather than suppress an alert.
        if (not incident_open or last_class not in PROBLEMS or not payload
                or not incident_key or (delivered_id is not None and attempts < 1)):
            incident_open = False
            attempts = 0
            delivered_id = None
            payload = None
            incident_key = None
        return {
            "incident_open": incident_open,
            "last_class": last_class,
            "delivery_attempts": attempts,
            "delivered_id": delivered_id,
            "alert_payload": payload,
            "incident_key": incident_key,
        }
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def should_alert(cls: str, now: float, state: dict) -> bool:
    """Whether this incident still has a bounded, unconfirmed delivery attempt."""
    del cls, now
    return (not state.get("delivered_id")
            and int(state.get("delivery_attempts", 0)) < MAX_DELIVERY_ATTEMPTS)


def build_alert_payload(cls: str, detail: str, incident_key: str) -> str:
    return (
        f"📉 [resource-sampler-{cls.lower()}] {detail}. "
        "The per-process memory evidence stream is incomplete; inspect and restart "
        "pantheon-resource-sampler.timer, then confirm the log advances. "
        f"Incident receipt: {incident_key}."
    )


def mark_alerted(cls: str, now: float, state: dict, *, incident_key: str,
                 alert_payload: str) -> None:
    del now
    state.update(last_class=cls, incident_open=True, delivery_attempts=0,
                 delivered_id=None, alert_payload=alert_payload,
                 incident_key=incident_key)


def record_class(cls: str, state: dict) -> None:
    state["last_class"] = cls
    if cls == "HEALTHY":
        state.update(incident_open=False, delivery_attempts=0, delivered_id=None,
                     alert_payload=None, incident_key=None)


def resolve_alert_sender():
    from tools import agent_role_map, mailbox

    def _durable_send(msg: str, *, vocab: str) -> str:
        inbox = Path(f"{agent_role_map.mailbox_hub()}/recovery")
        subject = f"[{vocab}] evidence stream unhealthy"
        return mailbox.send(
            inbox, msg,
            "resource-sampler-coverage",
            subject=subject,
            severity="urgent",
        )
    return _durable_send


def send_alert(payload: str, *, sender=None) -> str:
    sender = sender or resolve_alert_sender()
    delivered_id = sender(
        payload,
        vocab="resource-sampler-coverage",
    )
    if not isinstance(delivered_id, str) or not DELIVERY_ID_RE.fullmatch(delivered_id):
        raise RuntimeError("resource-sampler alert delivery returned no durable message id")
    return delivered_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="run the scheduled coverage check")
    parser.add_argument("--dry-run", action="store_true", help="classify without alerting or state writes")
    args = parser.parse_args(argv)
    if not args.check:
        print("ERROR: refusing flagless run; pass --check (and optionally --dry-run)", file=sys.stderr)
        return 64
    now = time.time()
    cls, detail = classify(now=now)
    print(f"[resource_sampler_coverage] {cls}: {detail}")
    if args.dry_run:
        return 0
    state = load_state()
    if cls in PROBLEMS:
        # A terminal incident must be a true no-work state: do not even resolve the
        # sender, since a later dependency failure would retrigger OnFailure forever.
        if state.get("incident_open") is True and not should_alert(cls, now, state):
            return 0

        is_new = state.get("incident_open") is not True
        # For a new incident, resolve BEFORE the first persisted gate. Otherwise an
        # import/config failure could strand an open incident without any attempt.
        sender = resolve_alert_sender()
        if is_new:
            incident_key = secrets.token_hex(16)
            mark_alerted(cls, now, state, incident_key=incident_key,
                         alert_payload=build_alert_payload(cls, detail, incident_key))
            try:
                save_state(state)
            except OSError as exc:
                print(f"[resource_sampler_coverage] state write failed before incident: {exc}", file=sys.stderr)
                raise
        elif not state.get("alert_payload"):
            # Upgrade a pre-payload nonterminal incident without changing its retry
            # budget. From this write onward every retry uses byte-identical content.
            incident_key = secrets.token_hex(16)
            state["incident_key"] = incident_key
            state["alert_payload"] = build_alert_payload(cls, detail, incident_key)
            try:
                save_state(state)
            except OSError as exc:
                print(f"[resource_sampler_coverage] state write failed before payload: {exc}", file=sys.stderr)
                raise
        state["last_class"] = cls
        if not should_alert(cls, now, state):
            return 0
        # Count BEFORE attempting delivery: even ambiguous failures are bounded.
        state["delivery_attempts"] = int(state.get("delivery_attempts", 0)) + 1
        try:
            save_state(state)
        except OSError as exc:
            print(f"[resource_sampler_coverage] state write failed before attempt: {exc}", file=sys.stderr)
            raise
        delivered_id = send_alert(state["alert_payload"], sender=sender)
        state["delivered_id"] = delivered_id
        try:
            save_state(state)
        except OSError as exc:
            print(f"[resource_sampler_coverage] delivery confirmed but state write failed: {exc}", file=sys.stderr)
            raise
        return 0
    else:
        record_class(cls, state)
    try:
        save_state(state)
    except OSError as exc:
        print(f"[resource_sampler_coverage] state write failed: {exc}", file=sys.stderr)
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
