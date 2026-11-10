#!/usr/bin/env python3
"""health_collector_check -- freshness READER for the fleet health aggregate (T-5102, ORG-080 SS6).

Serves ORG-080 SS6 (the collector's liveness watcher sits in a DIFFERENT failure domain from the
collector) and ORG-076 SSSteward (in-band plus outside monitoring). tools/health_collector.py writes
ONE aggregate, /run/pantheon-health/fleet-health.json, whose ``collected_at`` is stamped only by a
completed full collect (a dead watcher or a stopped timer cannot make it look fresher). This module
says whether that aggregate is fresh and well formed. It is the ONE staleness rule: the
health-collector component's verify invariants HC-5..HC-7
(tools/germinate/components/health-collector_handlers.py) and the out-of-domain witness (T-5978,
watchman's recovery_watcher) both call it. It is only the reader CONTRACT -- not the witness, not the
alert. Same shape as tools/org076_heartbeat_check.py.

CONTRACT (stable; the witness depends on it)
  Invocation   python3 tools/health_collector_check.py [--path P] [--max-age-secs S] [--now EPOCH]
  Path         default /run/pantheon-health/fleet-health.json (dir 2750 claude-fallback:pantheon-health;
               the reader needs group read only).
  Imports      stdlib only; NO ``tools.*``; no network; writes nothing; safe to vendor/copy.
  Well formed  a JSON object (<= MAX_BYTES) with exactly the keys the collector writes:
               schema "pantheon-fleet-health/1", schema_version 1, collected_at a positive int epoch
               (0 is the collector sanitizer's fallback for "no valid time", so it is malformed),
               collector {component_id "health-collector", mode poll|signal|startup, entry_count ==
               len(entries), read_ok_count, duration_ms ints}, entries a list of mappings.
  Stdout       exactly ONE JSON line with OUTPUT_KEYS:
               {"status": "fresh|stale|missing|unreadable|malformed|usage", "exit": N, "path": P|null,
                "age_secs": N|null, "max_age_secs": N|null, "collected_at": N|null, "mode": S|null,
                "entry_count": N|null, "detail": S}
               (a usage error, e.g. a nan / inf --now, also prints its one line, status "usage").
  Exit codes   0 fresh | 10 stale (age > max) | 11 missing | 12 unreadable (permission / IO) |
               13 malformed (incl. JSON nested too deeply to parse) | 2 usage. Every non-zero code
               means "alert"; the codes say WHICH.
  Threshold    default 180 s: three ticks of the 60 s collector timer (pantheon-health-collector.timer,
               OnUnitActiveSec=1min; Recovery measured a 61.1 s max gap, AccuracySec 5 s). Recovery-signed
               2026-11-02; the witness may pass its own --max-age-secs.
  Future dates An aggregate dated more than FUTURE_SKEW_SECS ahead of ``now`` is malformed, never fresh:
               a skewed writer clock must not make a dead collector look alive forever.

The reader is stateless, so it cannot itself storm; page-once / dedupe state is the witness's.

Uninstall leg (ORG-068 Remove): stop the witness's and the component's calls to this reader, then
delete this module and tools/test_health_collector_check.py.

Tests: python3 -m pytest tools/test_health_collector_check.py
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time

DEFAULT_PATH = "/run/pantheon-health/fleet-health.json"
DEFAULT_MAX_AGE_SECS = 180
FUTURE_SKEW_SECS = 120
MAX_BYTES = 4 * 1024 * 1024
SCHEMA = "pantheon-fleet-health/1"
SCHEMA_VERSION = 1
COLLECTOR_ID = "health-collector"
MODES = frozenset({"poll", "signal", "startup"})
DOC_KEYS = frozenset({"schema", "schema_version", "collected_at", "collector", "entries"})
COLLECTOR_KEYS = frozenset({"component_id", "mode", "entry_count", "read_ok_count", "duration_ms"})

EXIT_FRESH, EXIT_STALE, EXIT_MISSING, EXIT_UNREADABLE, EXIT_MALFORMED = 0, 10, 11, 12, 13
EXIT_USAGE = 2
OUTPUT_KEYS = ("status", "exit", "path", "age_secs", "max_age_secs", "collected_at", "mode", "entry_count", "detail")
_EXITS = {"fresh": EXIT_FRESH, "stale": EXIT_STALE, "missing": EXIT_MISSING,
          "unreadable": EXIT_UNREADABLE, "malformed": EXIT_MALFORMED, "usage": EXIT_USAGE}


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def shape_problem(doc) -> str | None:
    """None when ``doc`` is a well-formed aggregate, else a short reason."""

    if not isinstance(doc, dict):
        return "top level is not a JSON object"
    if set(doc) != DOC_KEYS:
        return f"top-level keys {sorted(doc)} != {sorted(DOC_KEYS)}"
    if doc["schema"] != SCHEMA:
        return f"schema is not {SCHEMA}"
    if not _is_int(doc["schema_version"]) or doc["schema_version"] != SCHEMA_VERSION:
        return f"schema_version is not {SCHEMA_VERSION}"
    if not _is_int(doc["collected_at"]) or doc["collected_at"] <= 0:
        return "collected_at is not a positive int epoch"
    col, entries = doc["collector"], doc["entries"]
    if not isinstance(col, dict) or set(col) != COLLECTOR_KEYS:
        return "collector block is missing or has the wrong keys"
    if col["component_id"] != COLLECTOR_ID:
        return f"collector.component_id is not {COLLECTOR_ID}"
    if col["mode"] not in MODES:
        return "collector.mode is not poll|signal|startup"
    if not all(_is_int(col[k]) and col[k] >= 0 for k in ("entry_count", "read_ok_count", "duration_ms")):
        return "collector counters are not non-negative ints"
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        return "entries is not a list of objects"
    if col["entry_count"] != len(entries):
        return "collector.entry_count != len(entries)"
    return None


def _result(path: str, max_age_secs: int, status: str, detail: str, **fields) -> dict:
    out = {key: None for key in OUTPUT_KEYS}
    out.update(status=status, exit=_EXITS[status], path=path, max_age_secs=max_age_secs, detail=detail)
    out.update(fields)
    return out


def check_doc(doc, max_age_secs: int, now: float, path: str = "<document>") -> dict:
    """The verdict on an already-parsed document (fresh | stale | malformed).

    ``now`` must be finite: a nan / inf clock is a caller bug, never a verdict (ValueError)."""

    if not math.isfinite(now):
        raise ValueError(f"now must be a finite epoch, got {now!r}")
    problem = shape_problem(doc)
    if problem is not None:
        return _result(path, max_age_secs, "malformed", problem)
    stamp = doc["collected_at"]
    fields = {"collected_at": stamp, "mode": doc["collector"]["mode"], "entry_count": doc["collector"]["entry_count"]}
    age = int(now) - stamp
    if age < -FUTURE_SKEW_SECS:
        return _result(path, max_age_secs, "malformed", f"collected_at is {-age}s in the future (skew > {FUTURE_SKEW_SECS}s)", **fields)
    age = max(age, 0)
    if age > max_age_secs:
        return _result(path, max_age_secs, "stale", f"collected {age}s ago (> {max_age_secs}s)", age_secs=age, **fields)
    return _result(path, max_age_secs, "fresh", f"collected {age}s ago", age_secs=age, **fields)


def check(path: str, max_age_secs: int, now: float) -> dict:
    """Read ``path`` and return the verdict dict (see OUTPUT_KEYS). Never raises on file problems."""

    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return _result(path, max_age_secs, "missing", "no aggregate at this path")
    except OSError as exc:
        return _result(path, max_age_secs, "unreadable", f"cannot read: {type(exc).__name__}")
    if len(raw) > MAX_BYTES:
        return _result(path, max_age_secs, "malformed", f"larger than {MAX_BYTES} bytes")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _result(path, max_age_secs, "malformed", "not UTF-8 JSON")
    except RecursionError:                              # deeply nested JSON: the parser's stack, not ValueError
        return _result(path, max_age_secs, "malformed", "JSON nested too deeply")
    return check_doc(doc, max_age_secs, now, path)


def _positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError("must be a finite number (not nan or inf)")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument("--max-age-secs", type=_positive_int, default=DEFAULT_MAX_AGE_SECS)
    parser.add_argument("--now", type=_finite_float, default=None, help="epoch seconds (tests); default: the clock")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code == 0:                               # --help is not a usage error
            return 0
        print(json.dumps(_result(None, None, "usage", "bad arguments (argparse wrote the reason to stderr)"), sort_keys=True))
        return EXIT_USAGE
    out = check(args.path, args.max_age_secs, time.time() if args.now is None else args.now)
    print(json.dumps(out, sort_keys=True))
    return out["exit"]


if __name__ == "__main__":
    sys.exit(main())
