#!/usr/bin/env python3
"""tools/fleet_usage_gate.py — scheduled fleet usage pacing gate (T-4510, T-4889).

The operator (TG 10709, after the 2026-10-01 freeze): at >=95% of the fleet's SHARED weekly
Max usage, the periodic anti-idle pokes suppress themselves. T-4889 replaces the flat
default with the operator's weekly schedule: 50% is the ceiling before the provider window's
midpoint; the ceiling rises linearly to 95% at Friday 18:00 America/Chicago; Friday evening
through that provider's reset is a weekend hold. Recovery remains live throughout.

SIGNAL / PRODUCER. Claude Code 2.1.289 pushes a ``session.measure`` event after each turn
and whenever a rate-limit window moves a whole point; its input carries ``rateLimits``,
each ``{kind, percentUsed, resetsAt}`` (kind five_hour | seven_day). The Claude Code mod in
``tools/mods/fleet-usage`` (the operator TG 10723; loaded as the managed org mod from
/opt/pantheon/claude-plugins since T-4643, which a controller's --settings cannot shadow) records one file per seat in the shared dir:
``{schema, ts, seat, seven_day:{used_percentage, resets_at}|null, five_hour:{...}|null}``
(epoch seconds). Observed live on specialist 2026-10-04T18:2xZ (Orchestrator):
``{"five_hour": {"resets_at": 1791151200, "used_percentage": 13},
"seven_day": {"resets_at": 1791738000, "used_percentage": 3}}`` — note used_percentage
arrives as an INTEGER in practice, so int and float are both accepted (bool is not).
Codex seats have separate subscriptions. Their existing hourly private-rollout exporter also
queries ``account/rateLimits/read`` through the same app-server observer used by
``subscription_usability`` and publishes ``turns/<seat>/quota.json`` with ``harness=codex``.
Codex evaluation is seat-specific; Claude evaluation uses the freshest shared Claude record.

The committed fixture ``tools/mods/fleet-usage/contract/fleet-usage-record.ts`` is read by
the mod's test (the mod must produce it) and by this module's test (the gate must accept
it), pinning both halves. This module is the READ side that both consumers share.

CONTRACT.
  - The Max window is fleet-shared, so any seat's reading is the fleet's reading. Per
    window the gate takes the FRESHEST (by the producer's ts) VALID reading across seats.
  - A reading is valid only while its ``resets_at`` is in the future (and within a
    plausibility horizon, and its ts is not from the future) — a garbage file must not be
    able to pin the fleet shut.
  - The schedule is on by default. The configured seven_day threshold remains an absolute
    ceiling (default 95); five_hour is OFF unless configured.
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
  PANTHEON_USAGE_GATE_SCHEDULE_ENABLED — default on; false restores the flat ceiling.

CLI: ``python3 tools/fleet_usage_gate.py status`` prints the gate's current view as JSON
(the install-verification command; also the input for any dashboard surface).

Stdlib only, no repo imports: both consumers import it cheaply.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA = "pantheon-fleet-usage/v1"
DEFAULT_DIR = "/var/lib/pantheon/fleet-usage"
DEFAULT_SEVEN_DAY_PCT = 95.0

ENV_ENABLED = "PANTHEON_USAGE_GATE_ENABLED"
ENV_SEVEN_DAY_PCT = "PANTHEON_USAGE_GATE_SEVEN_DAY_PCT"
ENV_FIVE_HOUR_PCT = "PANTHEON_USAGE_GATE_FIVE_HOUR_PCT"
ENV_DIR = "PANTHEON_FLEET_USAGE_DIR"
ENV_SCHEDULE_ENABLED = "PANTHEON_USAGE_GATE_SCHEDULE_ENABLED"

WINDOWS = ("seven_day", "five_hour")
_LABEL = {"seven_day": "weekly (7d)", "five_hour": "5-hour"}
# Plausibility horizons: a reading whose resets_at is further out than its window can
# possibly last is garbage, not a reason to hold the fleet.
HORIZON_SECONDS = {"seven_day": 8 * 24 * 3600, "five_hour": 6 * 3600}
TS_FUTURE_SLACK_SECONDS = 300
MAX_READING_BYTES = 64 * 1024

_OFF_VALUES = ("0", "false", "off", "no")
_CHICAGO = ZoneInfo("America/Chicago")
MIDWEEK_PCT = 50.0
FRIDAY_CEILING_PCT = 95.0


@dataclass(frozen=True)
class GateConfig:
    enabled: bool = True
    seven_day_pct: float = DEFAULT_SEVEN_DAY_PCT
    five_hour_pct: float | None = None
    dir: Path = Path(DEFAULT_DIR)
    schedule_enabled: bool = True


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
    band: str | None = None
    effective_threshold: float | None = None


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
    scheduled = str(env.get(ENV_SCHEDULE_ENABLED, "1")).strip().lower() not in _OFF_VALUES
    seven = _threshold(env.get(ENV_SEVEN_DAY_PCT))
    five = _threshold(env.get(ENV_FIVE_HOUR_PCT))
    directory = env.get(ENV_DIR) or DEFAULT_DIR
    return GateConfig(
        enabled=enabled,
        seven_day_pct=DEFAULT_SEVEN_DAY_PCT if seven is None else seven,
        five_hour_pct=five,
        dir=Path(directory),
        schedule_enabled=scheduled,
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


def load_readings(directory, now: float | None = None, *, harness: str = "claude",
                  seat: str | None = None) -> dict[str, Reading]:
    """Freshest valid reading per window for the requested provider/pool.

    Never raises: an unreadable dir/file is simply absent from the result."""
    now = time.time() if now is None else now
    best: dict[str, Reading] = {}
    root = Path(directory)
    if harness == "codex":
        # Codex accounts cannot write the top-level shared directory. Their quota
        # record lives in the pre-provisioned account-owned rollout directory. Do
        # not accept an alternate top-level Codex record: one pool has one canonical
        # path, and consumers name the pool they intend to gate.
        if seat is not None:
            entries = [root / "turns" / seat / "quota.json"]
        else:
            try:
                entries = sorted((root / "turns").glob("*/quota.json"))
            except OSError:
                return best
    else:
        try:
            entries = sorted(root.iterdir())
        except OSError:
            return best
    for path in entries:
        if path.name.startswith(".") or not path.name.endswith(".json"):
            continue
        try:
            doc = _read_doc(path)
            if doc is None:
                continue
            if doc.get("schema") != SCHEMA:
                continue
            doc_harness = doc.get("harness", "claude")
            if doc_harness != harness:
                continue
            doc_seat = doc.get("seat")
            if not isinstance(doc_seat, str) or not doc_seat:
                continue
            if seat is not None and doc_seat != seat:
                continue
            ts = doc.get("ts")
            if not _num(ts) or ts < 0 or ts > now + TS_FUTURE_SLACK_SECONDS:
                continue
            found = {}
            for window in WINDOWS:
                reading = _valid_window(window, doc.get(window), ts, doc_seat, now)
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


@dataclass(frozen=True)
class PacingBand:
    name: str
    threshold: float
    weekend_hold: bool = False


def pacing_band(reading: Reading, now: float) -> PacingBand:
    """T-4889's provider-window schedule.

    Every weekly record carries its provider-observed reset. The cycle midpoint is
    therefore exact for both Claude's Sunday reset and Codex's Saturday reset. The
    Friday boundary is 18:00 America/Chicago, DST-aware.
    """
    reset = datetime.fromtimestamp(reading.resets_at, timezone.utc)
    midpoint = reset - timedelta(days=3, hours=12)
    reset_local = reset.astimezone(_CHICAGO)
    days_back = (reset_local.weekday() - 4) % 7  # Friday == 4
    friday = (reset_local - timedelta(days=days_back)).replace(
        hour=18, minute=0, second=0, microsecond=0
    )
    if friday >= reset_local:
        friday -= timedelta(days=7)
    friday_utc = friday.astimezone(timezone.utc)
    current = datetime.fromtimestamp(now, timezone.utc)
    # Friday evening is an absolute operator boundary. A provider may move its
    # observed reset (live Codex did in Nov 2026), placing the mathematical
    # midpoint after Friday 18:00; the weekend hold still starts on Friday.
    if current >= friday_utc:
        return PacingBand("weekend-hold", FRIDAY_CEILING_PCT, weekend_hold=True)
    if current < midpoint:
        return PacingBand("pre-midweek", MIDWEEK_PCT)
    if current < friday_utc:
        span = (friday_utc - midpoint).total_seconds()
        elapsed = max(0.0, (current - midpoint).total_seconds())
        fraction = min(1.0, elapsed / span) if span > 0 else 1.0
        threshold = MIDWEEK_PCT + fraction * (FRIDAY_CEILING_PCT - MIDWEEK_PCT)
        return PacingBand("paced", threshold)
    return PacingBand("weekend-hold", FRIDAY_CEILING_PCT, weekend_hold=True)


def _suppression(reading: Reading, threshold: float, now: float, *, harness: str = "claude",
                 band: str | None = None, weekend_hold: bool = False) -> GateDecision:
    if weekend_hold:
        reason = (f"{harness} weekend hold after Friday 18:00 Chicago; fleet "
                  f"{_LABEL[reading.window]} usage {reading.pct:g}% (read from "
                  f"{reading.seat} {int(now - reading.ts)}s ago; resets "
                  f"{_iso(reading.resets_at)}); recovery remains live")
    else:
        reason = (f"{harness} {_LABEL[reading.window]} usage {reading.pct:g}% >= "
                  f"{threshold:g}%{f' ({band})' if band else ''} "
                  f"(read from {reading.seat} {int(now - reading.ts)}s ago; "
                  f"resets {_iso(reading.resets_at)})")
    if band is None:
        key = f"{reading.window}:{int(reading.resets_at)}"
    else:
        key = f"{harness}:{reading.window}:{int(reading.resets_at)}:{band}"
    return GateDecision(True, reason, key, reading, band, threshold)


def evaluate(now: float | None = None, cfg: GateConfig | None = None, *,
             harness: str = "claude", seat: str | None = None) -> GateDecision:
    """The gate decision. suppress=True only on a valid reading at/above threshold.
    Never raises; every unknown fails open (suppress=False)."""
    try:
        now = time.time() if now is None else float(now)
        if cfg is None:
            cfg = config_from_env()
        if not cfg.enabled:
            return GateDecision(False, "fleet usage gate disabled")
        readings = load_readings(cfg.dir, now, harness=harness, seat=seat)
        seven, five = readings.get("seven_day"), readings.get("five_hour")
        if seven is not None:
            if cfg.schedule_enabled:
                band = pacing_band(seven, now)
                threshold = min(cfg.seven_day_pct, band.threshold, FRIDAY_CEILING_PCT)
                if band.weekend_hold or seven.pct >= threshold:
                    return _suppression(seven, threshold, now, harness=harness,
                                        band=band.name, weekend_hold=band.weekend_hold)
            elif seven.pct >= cfg.seven_day_pct:
                return _suppression(seven, cfg.seven_day_pct, now, harness=harness)
        if cfg.five_hour_pct is not None and five is not None and five.pct >= cfg.five_hour_pct:
            return _suppression(five, cfg.five_hour_pct, now, harness=harness)
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


def usage_trip_state_dir(directory=None) -> Path:
    """One cross-user state root for trip-mail receipts and locks.

    Consumers run under different Unix accounts, so per-home state cannot deduplicate
    them. The fleet-usage directory is the existing shared, setgid substrate.
    """
    root = Path(directory) if directory is not None else config_from_env().dir
    return root / ".gate-state" / "trip-mail"


def _trip_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:64] or "unknown"


def _open_dir_no_symlinks(path: Path, mode: int) -> int:
    """Create/open an absolute directory path without following any component symlink."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in absolute.parts[1:]:
            created = False
            try:
                child_fd = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=mode, dir_fd=fd)
                    created = True
                except FileExistsError:
                    # Another first-fire consumer won mkdir after our failed open.
                    pass
                child_fd = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
            if created:
                os.fchmod(child_fd, mode)
            os.close(fd)
            fd = child_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def notify_usage_trip_once(agent: str, decision: GateDecision, send, *, log=None,
                           state_dir=None) -> None:
    """Send the pacing-trip mail once per (agent, recipient, window), across processes.

    ``send(recipient, subject, body)`` is caller-owned so this module remains stdlib-only.
    Each recipient's due -> send -> receipt sequence is serialized with ``flock``. A send
    failure writes no receipt and is therefore retried by the next consumer/cycle.
    """
    key = decision.window_key
    key_provider = str(key or "").partition(":")[0]
    provider = (key_provider if key_provider in {"claude", "codex"} else
                "codex" if str(decision.reason).startswith("codex ") else "claude")
    subject = f"T-4889 usage pacing trip: {provider} / {agent}"
    body = (
        f"The structural fleet-usage pacer tripped for {agent}.\n\n"
        f"{decision.reason}\n\n"
        "Engineering orchestration should narrow to one ticket or enter a hold. "
        "During the Friday-evening weekend band, no active movement resumes until "
        "that provider resets unless the operator explicitly pings. Recovery, liveness, "
        "cap handling, and de-wedge paths remain live."
    )
    root = Path(state_dir) if state_dir is not None else usage_trip_state_dir()
    for recipient in dict.fromkeys(("orchestrator", agent)):
        safe_agent = _trip_component(agent)
        safe_recipient = _trip_component(recipient)
        stem = f"{safe_agent}_{safe_recipient}"
        lock_path = root / f"{stem}.lock"
        receipt = root / f"{stem}.json"
        root_fd = None
        tmp_name = None
        tmp_fd = None
        try:
            root_fd = _open_dir_no_symlinks(root, 0o2770)
            root_st = os.fstat(root_fd)
            if not stat.S_ISDIR(root_st.st_mode):
                raise OSError(f"trip-mail root is not a directory: {root}")
            # Adjust the already-validated directory by fd. Path.chmod would follow a
            # prepositioned root symlink before O_NOFOLLOW got a chance to reject it.
            try:
                os.fchmod(root_fd, 0o2770)
            except OSError:
                pass
            lock_name = lock_path.name
            receipt_name = receipt.name
            fd = os.open(
                lock_name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o660,
                dir_fd=root_fd,
            )
            try:
                os.fchmod(fd, 0o660)
            except OSError:
                pass
            with os.fdopen(fd, "r+") as lock_file:
                if not stat.S_ISREG(os.fstat(lock_file.fileno()).st_mode):
                    raise OSError(f"trip-mail lock is not a regular file: {lock_path}")
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    receipt_fd = os.open(
                        receipt_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
                except FileNotFoundError:
                    previous = None
                else:
                    with os.fdopen(receipt_fd, "r", encoding="utf-8") as receipt_file:
                        if not stat.S_ISREG(os.fstat(receipt_file.fileno()).st_mode):
                            raise OSError(
                                f"trip-mail receipt is not a regular file: {receipt}")
                        try:
                            previous = json.load(receipt_file).get("window_key")
                        except (ValueError, AttributeError):
                            previous = None
                if key is not None and previous == key:
                    continue
                if key is not None:
                    digest = hashlib.sha256(
                        f"{agent}\0{recipient}\0{key}".encode()).hexdigest()[:16]
                    tmp_name = (f".{receipt_name}.tmp.{os.getpid()}.{digest}."
                                f"{secrets.token_hex(16)}")
                    tmp_fd = os.open(
                        tmp_name,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                        0o660,
                        dir_fd=root_fd,
                    )
                    os.fchmod(tmp_fd, 0o660)
                try:
                    send(recipient, subject, body)
                    if tmp_fd is not None:
                        with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_file:
                            json.dump({"window_key": key, "sent_at": time.time()}, tmp_file)
                            tmp_file.flush()
                            os.fsync(tmp_file.fileno())
                        tmp_fd = None
                        os.replace(
                            tmp_name, receipt_name,
                            src_dir_fd=root_fd, dst_dir_fd=root_fd,
                        )
                        tmp_name = None
                finally:
                    if tmp_fd is not None:
                        os.close(tmp_fd)
                        tmp_fd = None
        except Exception as exc:  # noqa: BLE001 — advisory mail cannot break a gate consumer
            if log is not None:
                log(f"usage pacing trip mail to {recipient} failed (ignored): "
                    f"{type(exc).__name__}: {exc}")
        finally:
            if tmp_name is not None and root_fd is not None:
                try:
                    os.unlink(tmp_name, dir_fd=root_fd)
                except OSError:
                    pass
            if tmp_fd is not None:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            if root_fd is not None:
                os.close(root_fd)


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


def status(now: float | None = None, cfg: GateConfig | None = None, *,
           harness: str = "claude", seat: str | None = None) -> dict:
    """JSON-able view of the gate (decision, thresholds, per-window readings)."""
    now = time.time() if now is None else float(now)
    if cfg is None:
        cfg = config_from_env()
    decision = evaluate(now, cfg, harness=harness, seat=seat)
    readings = {
        window: {"pct": r.pct, "resets_at": r.resets_at, "resets_at_iso": _iso(r.resets_at),
                 "ts": r.ts, "age_s": int(now - r.ts), "seat": r.seat}
        for window, r in load_readings(cfg.dir, now, harness=harness, seat=seat).items()
    }
    return {
        "now": now,
        "enabled": cfg.enabled,
        "dir": str(cfg.dir),
        "harness": harness,
        "seat": seat,
        "schedule_enabled": cfg.schedule_enabled,
        "seven_day_threshold": cfg.seven_day_pct,
        "five_hour_threshold": cfg.five_hour_pct,
        "suppress": decision.suppress,
        "reason": decision.reason,
        "window_key": decision.window_key,
        "band": decision.band,
        "effective_threshold": decision.effective_threshold,
        "readings": readings,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Fleet usage gate (T-4510).")
    sub = parser.add_subparsers(dest="command", required=True)
    st = sub.add_parser("status", help="print the gate's current view as JSON")
    st.add_argument("--dir", default=None, help="override PANTHEON_FLEET_USAGE_DIR")
    st.add_argument("--harness", choices=("claude", "codex"), default="claude")
    st.add_argument("--seat", default=None, help="subscription seat (required for codex)")
    args = parser.parse_args(argv)
    cfg = config_from_env()
    if args.dir:
        cfg = GateConfig(enabled=cfg.enabled, seven_day_pct=cfg.seven_day_pct,
                         five_hour_pct=cfg.five_hour_pct, dir=Path(args.dir),
                         schedule_enabled=cfg.schedule_enabled)
    json.dump(status(cfg=cfg, harness=args.harness, seat=args.seat), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
