#!/usr/bin/env python3
"""ORG-076 BT3b -- external-witness heartbeat READER (RFC-004 acceptance 6.9), T-4709.

Serves ORG-076 and D1: the whole ORG-076 substrate going dark (store host process killed, timers all
stopped) is invisible to every in-band check by construction. The episode store writes a heartbeat
file every cycle (``episode_store.write_heartbeat``); a watcher in a DIFFERENT failure domain
(ORG-048's out-of-band Codex watcher, another account) polls it with THIS reader. This module is only
the reader CONTRACT -- not the watcher, and not the page.

CONTRACT (stable; the watcher depends on it)
  Invocation   python3 tools/org076_heartbeat_check.py [--path P] [--max-age-secs S] [--now EPOCH]
  Path         default /var/lib/pantheon/org076/heartbeat/org076-episode-store.json (file 0644 in a
               2775 claude-fallback:pantheon-shared dir): the reader needs READ access only.
  Imports      stdlib only; NO ``tools.*``; no network; writes nothing; safe to vendor/copy.
  Heartbeat    JSON object, schema = ``heartbeat_write.build_heartbeat``'s 6 keys:
               agent ("org076-episode-store"), ts (UTC ISO), epoch (int), pid (int),
               source ("episode_store"), schema_version (1).
  Stdout       exactly ONE JSON line:
               {"status": "fresh|stale|missing|unreadable|malformed", "exit": N, "path": P,
                "age_secs": N|null, "max_age_secs": N, "epoch": N|null, "agent": S|null,
                "pid": N|null, "detail": S}
  Exit codes   0 fresh | 10 stale (age > max) | 11 missing | 12 unreadable (permission / IO) |
               13 malformed (not JSON, wrong shape, wrong agent/schema, or future-dated beyond
               skew) | 2 usage. Every non-zero code means "page"; the codes only say WHICH.
  Threshold    default 1200 s (20 min). The runner timer fires every 5 min (OnUnitActiveSec) and the
               unit has TimeoutStartSec=10min, so the worst gap between two writes of a HEALTHY
               system is one hung cycle killed at the timeout, about 10 min; 20 min adds a full
               missed-cycle margin without hiding a real stop. Detection latency = threshold + the
               watcher's poll interval (RFC-004 6.9 "within its polling window"): poll every <= 10 min.
  Future dates A heartbeat dated more than 120 s ahead of ``now`` is malformed, never fresh -- a
               skewed writer clock must not make a dead store look alive forever.

WHAT THE WATCHER MUST DO (not done here; Consultant's watcher lives in another repo)
  Run this reader each poll; on any non-zero exit PAGE ONCE per outage and stay quiet until the
  reader returns 0 again (re-arm on recovery). The page-once / dedupe state is the watcher's: this
  reader is stateless by design so it cannot itself storm.

Uninstall leg (ORG-068 Remove): stop the watcher's call to this reader, then delete this module and
``tools/test_org076_heartbeat_check.py``. Nothing else references it.

Tests: python3 -m pytest tools/test_org076_heartbeat_check.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time

DEFAULT_PATH = "/var/lib/pantheon/org076/heartbeat/org076-episode-store.json"
DEFAULT_MAX_AGE_SECS = 1200
FUTURE_SKEW_SECS = 120
AGENT = "org076-episode-store"
SCHEMA_VERSION = 1

EXIT_FRESH, EXIT_STALE, EXIT_MISSING, EXIT_UNREADABLE, EXIT_MALFORMED = 0, 10, 11, 12, 13


def check(path: str, max_age_secs: int, now: float) -> dict:
    out = {"status": "", "exit": 0, "path": path, "age_secs": None, "max_age_secs": max_age_secs,
           "epoch": None, "agent": None, "pid": None, "detail": ""}

    def done(status: str, code: int, detail: str = "") -> dict:
        out.update(status=status, exit=code, detail=detail)
        return out

    try:
        with open(path, "rb") as fh:
            raw = fh.read(65536)
    except FileNotFoundError:
        return done("missing", EXIT_MISSING, "no heartbeat file")
    except OSError as exc:
        return done("unreadable", EXIT_UNREADABLE, f"{type(exc).__name__}: {exc}")
    try:
        hb = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return done("malformed", EXIT_MALFORMED, f"not JSON: {exc}")
    if not isinstance(hb, dict):
        return done("malformed", EXIT_MALFORMED, "not a JSON object")
    epoch = hb.get("epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int):
        return done("malformed", EXIT_MALFORMED, "epoch missing or not an integer")
    out.update(epoch=epoch, agent=hb.get("agent") if isinstance(hb.get("agent"), str) else None,
               pid=hb.get("pid") if isinstance(hb.get("pid"), int) else None)
    if hb.get("agent") != AGENT or hb.get("schema_version") != SCHEMA_VERSION:
        return done("malformed", EXIT_MALFORMED,
                    f"unexpected agent/schema: {hb.get('agent')!r}/{hb.get('schema_version')!r}")
    age = int(now) - epoch
    out["age_secs"] = age
    if age < -FUTURE_SKEW_SECS:
        return done("malformed", EXIT_MALFORMED, f"heartbeat is {-age}s in the future")
    if age > max_age_secs:
        return done("stale", EXIT_STALE, f"heartbeat is {age}s old (max {max_age_secs}s)")
    return done("fresh", EXIT_FRESH, "")


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--path", default=DEFAULT_PATH)
    ap.add_argument("--max-age-secs", type=int, default=DEFAULT_MAX_AGE_SECS)
    ap.add_argument("--now", type=float, default=None, help="TEST ONLY: override the current epoch")
    args = ap.parse_args(argv)
    result = check(args.path, args.max_age_secs, args.now if args.now is not None else time.time())
    print(json.dumps(result, sort_keys=True))
    return result["exit"]


if __name__ == "__main__":
    sys.exit(main())
