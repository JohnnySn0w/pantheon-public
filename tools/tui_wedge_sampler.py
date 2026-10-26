#!/usr/bin/env python3
"""Measure how long a HEALTHY agent can look wedged (T-1369, last branch).

The wedge we want to detect is "TUI alive but not emitting": the pane keeps
changing (spinner animating) while the session jsonl stays frozen. The problem
is that a perfectly healthy agent in a long silent turn produces THE SAME
OBSERVATION:

    wedged-but-alive : pane changes, jsonl frozen  -> wants a restart
    long silent turn : pane changes, jsonl frozen  -> wants to be left alone

Only DURATION separates them, and the branch's action is a RESTART — the one
action in the pre-poke gate family that destroys work instead of merely being
useless. So the threshold cannot be picked by intuition; a number that fails
high leaves the wedge undetected, and a number that fails low kills sessions
mid-turn. This script exists to produce the number by measuring it, the same way
the monthly-cap specimen produced the wire string for branches 2+3 instead of a
guessed regex.

It is READ-ONLY and has no restart authority by construction: it captures panes,
stats jsonl mtimes, and appends samples. It never sends keys and never routes an
alert. Run it on a short timer, let it accumulate over known-healthy operation,
then read the run-length distribution off `--report`. The empirical ceiling of
that distribution is the floor any threshold has to clear.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from activity_monitor import (  # noqa: E402
    AGENT_CONFIG,
    capture_pane,
    jsonl_turn_signal,
)
# T-4159: SAMPLE_TARGETS jsonl_dir derivation. Imported as tools.agent_role_map,
# the same module object activity_monitor uses (its import put the repo root on path).
from tools import agent_role_map  # noqa: E402

# ⚠️ THE SAMPLER OWNS ITS OWN TARGET LIST, deliberately not AGENT_CONFIG's.
# activity_monitor.AGENT_CONFIG is an ACTUATOR's config — it lists the agents that
# get POKED, and it currently holds exactly one (orchestrator). Inheriting it meant this
# measurement covered one agent, and 8.5 hours produced only 3 usable runs: far too
# thin a distribution to set a threshold whose action is a RESTART.
#
# Worse, the obvious "fix" would have been to add recovery to AGENT_CONFIG — which
# would have enrolled recovery in the POKE path and violated T-0559 (recovery must never be
# self-injected). A read-only measurement must not be able to widen an actuator's
# blast radius as a side effect of wanting more data. Hence a separate list.
#
# Sampling recovery's OWN pane is safe and is not self-injection: capture-pane is a
# read. It also adds genuine workload diversity — recovery's turn shape (long tool
# chains, cross-account probes) differs markedly from orchestrator's, and a ceiling
# derived from one agent's habits is a ceiling for that agent, not for the fleet.
# T-4159: jsonl_dir is DERIVED from the seat's launch shape (agent_role_map), not
# hardcoded. Bespoke seats write under the home-cwd slug, control-plane seats under
# -pantheon-config, and recovery/orchestrator flip at their T-3650 hand-move. The old literals
# were wrong in both directions: recovery pointed at -pantheon-config while bespoke
# (that dir holds no jsonl; checked 2026-09-28), and orchestrator would freeze on home.
SAMPLE_TARGETS = {
    "orchestrator": {"tmux_target_pane": "claude-telegram-bot:0",
              "sudo_user": "claude",
              "jsonl_dir": agent_role_map.live_session_projects_dir("claude")},
    "recovery":  {"tmux_target_pane": "claude-telegram-bot-fallback:0",
              "sudo_user": None,   # own account; _sudo() short-circuits on self
              "jsonl_dir": agent_role_map.live_session_projects_dir("claude-fallback")},
}

DEFAULT_SAMPLE_PATH = (
    Path.home() / ".local" / "state" / "pantheon" / "tui_wedge_samples.jsonl"
)


# ------------------------------------------------------------------ pure core
def pane_fingerprint(pane_text: str | None) -> str | None:
    """Stable digest of a pane capture, or None if the capture failed.

    A digest rather than the text itself: samples accumulate on a short timer for
    days, and the only question asked of a pane here is "is this the same frame
    as last time". Storing transcripts would make the sample file enormous and
    would copy sibling agents' working content into a measurement artifact for no
    analytic gain.
    """
    if pane_text is None:
        return None
    return hashlib.sha256(pane_text.encode("utf-8", "replace")).hexdigest()[:16]


def frozen_runs(samples: list[dict]) -> list[dict]:
    """Maximal runs of "pane MOVED while jsonl did NOT" — the ambiguous state.

    A run extends while each consecutive pair shows a changed pane fingerprint
    and an unchanged jsonl mtime. Its duration is measured from the FIRST sample
    of the run, because that is the moment the ambiguity began — the last
    observation that was still unambiguous.

    Samples with a missing fingerprint or mtime BREAK the run rather than
    extending it: a failed capture is not evidence of stillness, and silently
    bridging over it would inflate exactly the number this script exists to
    bound. Fails toward SHORTER runs, i.e. toward a threshold that is too low to
    be dangerous rather than one that is too high to detect anything.
    """
    runs: list[dict] = []
    start: dict | None = None
    prev: dict | None = None
    for cur in samples:
        ok = (
            prev is not None
            and prev.get("pane") is not None and cur.get("pane") is not None
            and prev.get("mtime") is not None and cur.get("mtime") is not None
        )
        moved = ok and cur["pane"] != prev["pane"] and cur["mtime"] == prev["mtime"]
        if moved:
            if start is None:
                start = prev
        elif start is not None:
            runs.append({
                "agent": start.get("agent"),
                "start_ts": start["ts"],
                "end_ts": prev["ts"],
                "seconds": prev["ts"] - start["ts"],
                "samples": 0,
            })
            start = None
        prev = cur
    if start is not None and prev is not None:
        runs.append({
            "agent": start.get("agent"),
            "start_ts": start["ts"],
            "end_ts": prev["ts"],
            "seconds": prev["ts"] - start["ts"],
            "samples": 0,
            "open": True,  # still running at the end of the sample window
        })
    return runs


def summarize(runs: list[dict]) -> dict:
    """Distribution facts a threshold can actually be set from.

    Reports the max and the count, and says explicitly whether the longest run
    was still OPEN when sampling ended — an open run is a LOWER BOUND, not a
    measurement, and reading it as a ceiling is how a threshold ends up under
    the real one.
    """
    if not runs:
        return {"count": 0, "max_seconds": None, "longest_was_open": False}
    longest = max(runs, key=lambda r: r["seconds"])
    return {
        "count": len(runs),
        "max_seconds": longest["seconds"],
        "longest_was_open": bool(longest.get("open")),
        "p50_seconds": sorted(r["seconds"] for r in runs)[len(runs) // 2],
    }


# ----------------------------------------------------------------- impure shell
def _capture_self(pane: str):
    """Capture OUR OWN pane without sudo.

    activity_monitor._sudo() unconditionally builds `sudo -nu <user>`, and
    `sudo -nu claude-fallback` FROM claude-fallback is denied by the sudoers
    policy — an account is not granted sudo to itself. So the self case cannot
    go through capture_pane() at all; it needs a direct call. Reading our own
    pane is a capture, not an inject, so T-0559 (recovery must never be
    self-injected) is untouched.
    """
    cmd = ["bash", "-c",
           "XDG_RUNTIME_DIR=/run/user/$(id -u) tmux capture-pane -p -t " + pane]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (subprocess.SubprocessError, OSError):
        return None
    return r.stdout if r.returncode == 0 else None


def _mtime_self(jsonl_dir) -> int | None:
    """Newest jsonl mtime under our own dir — no sudo, same reason as above."""
    try:
        newest = max((f.stat().st_mtime for f in Path(jsonl_dir).glob("*.jsonl")),
                     default=None)
    except OSError:
        return None
    return int(newest) if newest is not None else None


def sample_agent(agent: str, cfg: dict) -> dict:
    pane = cfg.get("tmux_target_pane")
    user = cfg.get("sudo_user")
    is_self = not user or user == getpass.getuser()
    if pane is None:
        text = None
    elif is_self:
        text = _capture_self(pane)
    else:
        text = capture_pane(pane, user)
    mtime = (_mtime_self(cfg["jsonl_dir"]) if is_self
             else jsonl_turn_signal(cfg.get("jsonl_dir"), user))  # T-5400
    return {
        "agent": agent,
        "ts": int(time.time()),
        "pane": pane_fingerprint(text),
        "mtime": mtime,
    }


def append_samples(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def load_samples(path: Path, agent: str | None = None) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue  # a torn tail line must not lose the whole history
        if isinstance(rec, dict) and (agent is None or rec.get("agent") == agent):
            out.append(rec)
    out.sort(key=lambda r: r.get("ts", 0))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", action="store_true", help="take one sample per agent")
    ap.add_argument("--report", action="store_true", help="summarize collected runs")
    ap.add_argument("--agent", default=None, help="restrict to one agent")
    ap.add_argument("--path", default=str(DEFAULT_SAMPLE_PATH))
    args = ap.parse_args(argv)
    path = Path(args.path)

    if args.sample:
        agents = ([args.agent] if args.agent else list(SAMPLE_TARGETS))
        recs = [sample_agent(a, SAMPLE_TARGETS[a]) for a in agents if a in SAMPLE_TARGETS]
        append_samples(path, recs)
        for r in recs:
            print(f"  {r['agent']:<10} pane={r['pane']} mtime={r['mtime']}")
    if args.report:
        for agent in ([args.agent] if args.agent else sorted(
                {r.get("agent") for r in load_samples(path)} - {None})):
            s = summarize(frozen_runs(load_samples(path, agent)))
            print(f"  {agent}: {s}")
            if s["count"] and s["longest_was_open"]:
                print("    ⚠️ longest run was still OPEN — this is a LOWER BOUND "
                      "on the healthy ceiling, not the ceiling. Keep sampling.")
    if not args.sample and not args.report:
        ap.error("nothing to do — pass --sample and/or --report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
