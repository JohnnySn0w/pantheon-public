"""ORG-076 BT5a (T-6001): the mail-delivery staleness detector's WRITER half, onto the episode store.

The Recovery-hosted ``mail_staleness_monitor`` classifies every inbox each cycle (``raw_sample_verdicts``
there, built on ``mail_staleness.detect`` so it is the same classification the monitor already does,
BEFORE any cooldown) and hands the result here. This module turns each per-inbox verdict into one
schema-2 drop file (``org076_store_runner`` envelope, T-5999) and writes it atomically to the shared drop
dir, exactly like ``quota_probe_detector.feed`` does for schema 1. The runner (not this module) owns
routing: the ``mail-delivery`` ``REGISTRY`` entry builds the episode spec with N=3, M=2 and derives the
faulted seat's harness and account from ``seat_for`` below, never from the record.

Domain ``mail-delivery:<inbox>``. Verdicts:
  healthy  -- no unread mail, a busy/lease-held pane, or (pane unreadable) unread within the coarse budget
  broken   -- ``mail-stale:<inbox>``: idle pane with unread mail, or a never-received (wiring) inbox
  unknown  -- ``mail-stale-unmeasurable:<inbox>``: unreadable inbox, or an unreadable pane whose unread mail
              is older than the monitor's coarse-tier budget (COARSE_SAMPLES * SAMPLE_INTERVAL_SECONDS)
A held (seat-maintenance / paused / deep-sleep), muted or non-seat inbox gets NO verdict and is logged as
suppressed (mute is not blind).

EVIDENCE. The envelope ``detail`` is built only from the inbox name (validated against PERSONA_RE) and
integers/ages: unread count, in-flight count, oldest-unread age, pane state. It never reads a message,
sender or subject (RFC-004 s6.15-6.17).

DARK by default: ``PANTHEON_ORG076_MAIL_DELIVERY_DETECTOR`` must be exactly ``"1"`` (read at call time);
any other value, including unset, "", "0", "true", "1 " or "yes", writes nothing. The write is fail-safe:
a failure is logged and the monitor's alerting is unaffected. The drop dir is the quota writer's
(``PANTHEON_ORG076_SAMPLES_DIR`` or its default); this module never creates it.

KNOWN LIMIT (stated, not fixed): ``seat_for`` is the STATIC harness in ``mailbox_wake.AGENT_PANE``. A seat
currently running its fallback harness (the live harness lives in the seat's own runtime.json, which the
Recovery-hosted runner cannot read) is routed as its primary harness. A misclassification only reorders the
repair candidates; the subject-persona exclusion still holds.

ROLLOUT: runner with the registry entry live in the dedicated clone FIRST (an older runner quarantines and
pages every schema-2 file), then Recovery sets the switch on the monitor's unit.
UNINSTALL (ORG-068 Remove), in order: unset the switch (stops new drops); ``org076_store_runner.py
--forget-persona <inbox>`` for every enrolled inbox (closes its episodes; do this BEFORE removing the
registry entry); remove the ``mail-delivery`` REGISTRY entry and ``seat_for`` plumbing; delete this
module and its tests; remove the one call in ``mail_staleness_monitor.main``. Queued drops are left.

Tests: python3 -m pytest tools/test_org076_mail_delivery.py tools/test_org076_drop_envelope.py
"""
from __future__ import annotations

import dataclasses
import json
import os
import secrets
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from tools import quota_probe_detector as qpd
except ImportError:  # script run with tools/ on sys.path
    import quota_probe_detector as qpd  # type: ignore

ENV_SWITCH = "PANTHEON_ORG076_MAIL_DELIVERY_DETECTOR"
DETECTOR = "mail-delivery"
SUBSYSTEM = "mail-delivery"
FAULT_STALE = "mail-stale"
FAULT_UNMEASURABLE = "mail-stale-unmeasurable"
FAULT_KINDS = frozenset({FAULT_STALE, FAULT_UNMEASURABLE})
N, M = 3, 2
# Mirrors org076_store_runner's envelope constants (the runner imports this module, so no import back).
ENVELOPE_SCHEMA = 2
ENVELOPE_KIND = "org076-detector-sample"

HEALTHY, BROKEN, UNKNOWN = "healthy", "broken", "unknown"


@dataclasses.dataclass(frozen=True)
class Sample:
    """One inbox's raw per-sample verdict plus the numbers that explain it (counts/ages only)."""
    inbox: str
    verdict: str                 # healthy | broken | unknown
    kind: str                    # the mail_staleness kind, or "ok"
    unread: int
    in_flight: int
    oldest_age_s: "float | None"
    pane: str                    # idle | busy | unknown


def enabled(env: Any = None) -> bool:
    """Dark switch: exact ``"1"`` only, read at call time."""
    return (env if env is not None else os.environ).get(ENV_SWITCH) == "1"


def _log(msg: str) -> None:
    print(f"[org076_mail_delivery] {msg}", file=sys.stderr)


def seat_for(persona: str) -> "tuple[str, str] | None":
    """(harness, account) of an inbox's seat from the static ``mailbox_wake.AGENT_PANE`` map, or None for a
    non-seat inbox. The runner uses this to build the spec and to cross-check the record's account."""
    try:
        try:
            from tools import mailbox_wake as mw
        except ImportError:
            import mailbox_wake as mw  # type: ignore
        cfg = mw.AGENT_PANE.get(persona)
        if not cfg:
            return None
        return ("codex" if cfg.get("harness") == "codex" else "claude", Path(cfg["home"]).name)
    except Exception:  # noqa: BLE001 -- unknown seat => the runner rejects (fail closed)
        return None


def fault_id(sample: Sample) -> "str | None":
    if sample.verdict == HEALTHY:
        return None
    return f"{FAULT_STALE if sample.verdict == BROKEN else FAULT_UNMEASURABLE}:{sample.inbox}"


def detail_for(s: Sample) -> str:
    age = "none" if s.oldest_age_s is None else str(int(s.oldest_age_s))
    return (f"inbox={s.inbox} kind={s.kind} unread={int(s.unread)} in_flight={int(s.in_flight)} "
            f"oldest_age_s={age} pane={s.pane}")[:qpd.DETAIL_LIMIT]


def envelope(s: Sample, account: str, run_id: str, now: datetime) -> dict:
    return {
        "schema": ENVELOPE_SCHEMA, "kind": ENVELOPE_KIND, "detector": DETECTOR,
        "domain_key": f"{DETECTOR}:{s.inbox}", "persona": s.inbox, "account": account,
        "verdict": s.verdict, "fault_id": fault_id(s), "detail": detail_for(s), "run_id": run_id,
        "sample_id": str(uuid.uuid4()),
        "sampled_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def write_drop(body: dict, drop: "Path | str | None" = None, now: "datetime | None" = None) -> str:
    """Atomic append-only drop (tmp -> fsync -> link), the quota writer's mechanics. May raise."""
    persona = body["persona"]
    if not qpd.PERSONA_RE.match(persona or ""):
        raise ValueError(f"persona {persona!r} is not a valid drop-file persona")
    directory = qpd.drop_dir(drop)
    now = now or datetime.now(timezone.utc)
    payload = (json.dumps(body, sort_keys=True) + chr(10)).encode("utf-8")
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    run = qpd._slug(body["run_id"])
    fd, tmp = qpd._mkstemp(directory)
    try:
        try:
            os.write(fd, payload)
            os.fchmod(fd, qpd.SAMPLE_MODE)
            os.fsync(fd)
        finally:
            os.close(fd)
        for _ in range(20):
            name = f"{stamp}__{persona}__{run}__{os.getpid()}{secrets.token_hex(3)}.json"
            try:
                os.link(tmp, directory / name)
            except FileExistsError:
                continue
            qpd._fsync_dir(directory)
            return name
        raise FileExistsError("could not find an unused drop file name")
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def run_token(now: datetime) -> str:
    """ONE id per monitor invocation (not a time bucket): dedupe then catches only a true replay."""
    return f"{int(now.timestamp() * 1e9):x}{secrets.token_hex(2)}"


def feed_safely(samples: Iterable[Sample], *, suppressed: Iterable[tuple] = (), drop: "Path | str | None" = None,
                now: "datetime | None" = None, env: Any = None) -> list:
    """Write one drop per sample. Dark unless the switch is exactly "1". Never raises: a failure is logged
    and the monitor's alerting is unaffected (a per-inbox failure does not stop the others)."""
    names: list = []
    try:
        if not enabled(env):
            return names
        now = now or datetime.now(timezone.utc)
        token = run_token(now)
        for inbox, why in suppressed:
            _log(f"{inbox}: suppressed ({why}) -- no verdict written")
        for s in samples:
            try:
                seat = seat_for(s.inbox)
                if seat is None:
                    _log(f"{s.inbox}: suppressed (not a seat inbox) -- no verdict written")
                    continue
                names.append(write_drop(envelope(s, seat[1], f"mds-{s.inbox}-{token}", now), drop, now))
            except Exception as exc:  # noqa: BLE001
                _log(f"{s.inbox}: drop failed (ignored; monitor alerting unaffected): {type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        _log(f"detector failed (ignored; monitor alerting unaffected): {type(exc).__name__}: {exc}")
    return names
