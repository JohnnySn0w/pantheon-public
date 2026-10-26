#!/usr/bin/env python3
"""tools/fleet_usage_gate.py — fleet usage gate for the periodic anti-idle pokes (T-4510).

The operator (TG 10709, after the 2026-10-01 freeze): at >=95% of the fleet's SHARED weekly
Max usage, the periodic anti-idle pokes (keep_going nudges, activity_monitor P1.a/P1.b)
suppress themselves and resume after the reset, so the fleet does not burn its last 5%
on make-work and then sit capped for days.

SIGNAL / PRODUCER. Claude Code 2.1.289 pushes a ``session.measure`` event after each turn
and whenever a rate-limit window moves a whole point; its input carries ``rateLimits``,
each ``{kind, percentUsed, resetsAt}`` (kind five_hour | seven_day). The Claude Code mod in
``tools/mods/fleet-usage`` (the operator TG 10723; loaded via CLAUDE_CODE_PLUGIN_DIRS, which a
controller's --settings cannot shadow) records one file per seat in the shared dir:
``{schema, ts, seat, seven_day:{used_percentage, resets_at}|null, five_hour:{...}|null}``
(epoch seconds). Observed live on specialist 2026-10-04T18:2xZ (Orchestrator):
``{"five_hour": {"resets_at": 1791151200, "used_percentage": 13},
"seven_day": {"resets_at": 1791738000, "used_percentage": 3}}`` — note used_percentage
arrives as an INTEGER in practice, so int and float are both accepted (bool is not).
The committed fixture ``tools/mods/fleet-usage/contract/fleet-usage-record.ts`` is read by
the mod's test (the mod must produce it) and by this module's test (the gate must accept
it), pinning both halves. This module is the READ side that both consumers share.

CONTRACT.
  - The Max window is fleet-shared, so any seat's reading is the fleet's reading. Per
    window the gate takes the FRESHEST (by the producer's ts) VALID reading across seats.
  - A reading is valid only while its ``resets_at`` is in the future (and within a
    plausibility horizon, and its ts is not from the future) — a garbage file must not be
    able to pin the fleet shut.
  - Threshold is config-driven: seven_day default 95; five_hour OFF unless configured
    (a 5h window self-clears within hours; the incident that motivated this was the
    multi-day weekly freeze).
  - FAIL OPEN on everything: missing/unparseable/expired/unreadable => pokes continue,
    which is the behaviour before this module existed. A broken producer must never
    silence the anti-park heartbeat.
  - RECOVERY pokes (ho_resumer, restart_drain, context_fill de-wedge, Recovery
    verify_startup, activity_monitor's modal/cap/cyber/wedge/P1.d paths) must NOT consult
    this module — tools/test_fleet_usage_gate.py pins that structurally.

Config (env, same convention as KEEP_GOING_* / ACTIVITY_MONITOR_*):
  PANTHEON_USAGE_GATE_ENABLED         — default on; 0/false/off/no disables the gate.
  PANTHEON_USAGE_GATE_SEVEN_DAY_PCT   — default 95.
  PANTHEON_USAGE_GATE_FIVE_HOUR_PCT   — default unset (five_hour does not gate).
  PANTHEON_FLEET_USAGE_DIR            — default /var/lib/pantheon/fleet-usage.

CLI: ``python3 tools/fleet_usage_gate.py status`` prints the gate's current view as JSON
(the install-verification command; also the input for any dashboard surface).

Stdlib only, no repo imports: both consumers import it cheaply.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "pantheon-fleet-usage/v1"
DEFAULT_DIR = "/var/lib/pantheon/fleet-usage"
DEFAULT_SEVEN_DAY_PCT = 95.0

ENV_ENABLED = "PANTHEON_USAGE_GATE_ENABLED"
ENV_SEVEN_DAY_PCT = "PANTHEON_USAGE_GATE_SEVEN_DAY_PCT"
ENV_FIVE_HOUR_PCT = "PANTHEON_USAGE_GATE_FIVE_HOUR_PCT"
ENV_DIR = "PANTHEON_FLEET_USAGE_DIR"

WINDOWS = ("seven_day", "five_hour")
_LABEL = {"seven_day": "weekly (7d)", "five_hour": "5-hour"}
# Plausibility horizons: a reading whose resets_at is further out than its window can
# possibly last is garbage, not a reason to hold the fleet.
HORIZON_SECONDS = {"seven_day": 8 * 24 * 3600, "five_hour": 6 * 3600}
TS_FUTURE_SLACK_SECONDS = 300
MAX_READING_BYTES = 64 * 1024

_OFF_VALUES = ("0", "false", "off", "no")


@dataclass(frozen=True)
class GateConfig:
    enabled: bool = True
    seven_day_pct: float = DEFAULT_SEVEN_DAY_PCT
    five_hour_pct: float | None = None
    dir: Path = Path(DEFAULT_DIR)


@dataclass(frozen=True)
class Reading:
    """One VALID window reading (already filtered for validity)."""

    window: str
    pct: float
    resets_at: float
    ts: float
    seat: str


@dataclass(frozen=True)
class GateDecision:
    suppress: bool
    reason: str
    window_key: str | None = None
    reading: Reading | None = None


def _num(value) -> bool:
    """A real JSON number: int or float (the live payload carries ints), never a bool.
    An int too large for a float (math.isfinite raises OverflowError) is not a usable number."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _threshold(raw) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and 0 < value <= 100 else None


def config_from_env(env=None) -> GateConfig:
    """Build the config from env; any malformed value falls back to its default."""
    env = os.environ if env is None else env
    enabled = str(env.get(ENV_ENABLED, "1")).strip().lower() not in _OFF_VALUES
    seven = _threshold(env.get(ENV_SEVEN_DAY_PCT))
    five = _threshold(env.get(ENV_FIVE_HOUR_PCT))
    directory = env.get(ENV_DIR) or DEFAULT_DIR
    return GateConfig(
        enabled=enabled,
        seven_day_pct=DEFAULT_SEVEN_DAY_PCT if seven is None else seven,
        five_hour_pct=five,
        dir=Path(directory),
    )


def _read_doc(path: Path):
    # O_NONBLOCK then fstat: a plain open() of a FIFO named *.json blocks forever when it has
    # no writer, and the shared dir is group-writable. Only regular files are read.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return None
            raw = fh.read(MAX_READING_BYTES + 1)
        if len(raw) > MAX_READING_BYTES:
            return None
        doc = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError):  # JSONDecode/UnicodeDecode are ValueErrors
        return None
    return doc if isinstance(doc, dict) else None


def _valid_window(window: str, body, ts: float, seat: str, now: float) -> Reading | None:
    if not isinstance(body, dict):
        return None
    pct, resets = body.get("used_percentage"), body.get("resets_at")
    if not (_num(pct) and _num(resets)) or pct < 0:
        return None
    if resets <= now or resets > now + HORIZON_SECONDS[window]:
        return None
    return Reading(window=window, pct=pct, resets_at=resets, ts=ts, seat=seat)


def load_readings(directory, now: float | None = None) -> dict[str, Reading]:
    """Freshest VALID reading per window across every ``*.json`` in `directory`.

    Never raises: an unreadable dir/file is simply absent from the result."""
    now = time.time() if now is None else now
    best: dict[str, Reading] = {}
    try:
        entries = sorted(Path(directory).iterdir())
    except OSError:
        return best
    for path in entries:
        if path.name.startswith(".") or not path.name.endswith(".json"):
            continue
        try:
            doc = _read_doc(path)
            if doc is None:
                continue
            ts = doc.get("ts")
            if not _num(ts) or ts < 0 or ts > now + TS_FUTURE_SLACK_SECONDS:
                continue
            found = {}
            for window in WINDOWS:
                reading = _valid_window(window, doc.get(window), ts, path.stem, now)
                if reading is not None:
                    found[window] = reading
        except Exception:  # noqa: BLE001 — one hostile file must never blind the gate to a
            continue       # valid sibling: skip THAT file only (the fleet dir is group-writable)
        for window, reading in found.items():
            held = best.get(window)
            if held is None or (reading.ts, reading.seat) > (held.ts, held.seat):
                best[window] = reading
    return best


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _suppression(reading: Reading, threshold: float, now: float) -> GateDecision:
    reason = (f"fleet {_LABEL[reading.window]} usage {reading.pct:g}% >= {threshold:g}% "
              f"(read from {reading.seat} {int(now - reading.ts)}s ago; "
              f"resets {_iso(reading.resets_at)})")
    return GateDecision(True, reason, f"{reading.window}:{int(reading.resets_at)}", reading)


def evaluate(now: float | None = None, cfg: GateConfig | None = None) -> GateDecision:
    """The gate decision. suppress=True only on a valid reading at/above threshold.
    Never raises; every unknown fails open (suppress=False)."""
    try:
        now = time.time() if now is None else float(now)
        if cfg is None:
            cfg = config_from_env()
        if not cfg.enabled:
            return GateDecision(False, "fleet usage gate disabled")
        readings = load_readings(cfg.dir, now)
        seven, five = readings.get("seven_day"), readings.get("five_hour")
        if seven is not None and seven.pct >= cfg.seven_day_pct:
            return _suppression(seven, cfg.seven_day_pct, now)
        if cfg.five_hour_pct is not None and five is not None and five.pct >= cfg.five_hour_pct:
            return _suppression(five, cfg.five_hour_pct, now)
        if seven is None and five is None:
            return GateDecision(False, "no valid fleet usage reading (fail open)")
        shown = seven if seven is not None else five
        return GateDecision(False, f"below threshold ({_LABEL[shown.window]} {shown.pct:g}%)",
                            reading=shown)
    except Exception as exc:  # noqa: BLE001 — the gate must never break a consumer
        return GateDecision(False, f"gate error {type(exc).__name__} (fail open)")


def default_state_dir() -> Path:
    """Where once-per-window log markers live: $PANTHEON_STATE_DIR or ~/.local/state/pantheon."""
    configured = os.environ.get("PANTHEON_STATE_DIR")
    return Path(configured) if configured else Path.home() / ".local" / "state" / "pantheon"


def should_log_once(consumer_key: str, window_key: str | None, state_dir=None) -> bool:
    """True the first time `consumer_key` sees `window_key`, then False until the window
    changes. If the marker cannot be read or written, returns True (noisy beats silent)."""
    if window_key is None:
        return True
    try:
        base = Path(state_dir) if state_dir is not None else default_state_dir()
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(consumer_key))[:120] or "consumer"
        marker = base / "fleet_usage_gate" / f"{safe}.json"
        try:
            previous = json.loads(marker.read_text(encoding="utf-8")).get("window_key")
        except (OSError, ValueError, AttributeError):
            previous = None
        if previous == window_key:
            return False
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"window_key": window_key, "logged_at": time.time()}),
                          encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001 — OSError, or RuntimeError from Path.home(): noisy, not failing
        return True


def status(now: float | None = None, cfg: GateConfig | None = None) -> dict:
    """JSON-able view of the gate (decision, thresholds, per-window readings)."""
    now = time.time() if now is None else float(now)
    if cfg is None:
        cfg = config_from_env()
    decision = evaluate(now, cfg)
    readings = {
        window: {"pct": r.pct, "resets_at": r.resets_at, "resets_at_iso": _iso(r.resets_at),
                 "ts": r.ts, "age_s": int(now - r.ts), "seat": r.seat}
        for window, r in load_readings(cfg.dir, now).items()
    }
    return {
        "now": now,
        "enabled": cfg.enabled,
        "dir": str(cfg.dir),
        "seven_day_threshold": cfg.seven_day_pct,
        "five_hour_threshold": cfg.five_hour_pct,
        "suppress": decision.suppress,
        "reason": decision.reason,
        "window_key": decision.window_key,
        "readings": readings,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Fleet usage gate (T-4510).")
    sub = parser.add_subparsers(dest="command", required=True)
    st = sub.add_parser("status", help="print the gate's current view as JSON")
    st.add_argument("--dir", default=None, help="override PANTHEON_FLEET_USAGE_DIR")
    args = parser.parse_args(argv)
    cfg = config_from_env()
    if args.dir:
        cfg = GateConfig(cfg.enabled, cfg.seven_day_pct, cfg.five_hour_pct, Path(args.dir))
    json.dump(status(cfg=cfg), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
