#!/usr/bin/env python3
"""Well-trod pathing stats — recurring fleet doc-traversal snapshot (T-1811).

Productizes T-1805's `doc_traversal_report.py` into a recurring, visible
measure (the operator, TG 3809: "part of the stats, something like a well-trod
pathing measure. possibly visually represented.").

Architecture (why a committed snapshot instead of a live kino shell-out):
the kino service runs as user `kino`, which cannot `sudo -nu` into sibling
agents' `~/.claude/projects` transcript dirs. Collection therefore runs on
the Recovery account (which holds the NOPASSWD cross-account sudo grants) on a
daily systemd user timer, and commits a JSON snapshot into the repo. The
kino stats panel then serves the committed snapshot via this tool's
read-only serve mode (`--format json`) behind its usual pull-gate — the
same decoupling `continuity_counter.py` gets from reading committed state.

Metric ("well-trod" score): per governance doc, the number of DISTINCT
sessions that touched it inside a rolling window (default 14 days, capped
at --max-sessions newest sessions per agent). Distinct-session breadth
beats raw touch counts as a "trodden path" measure — one session grinding a
file 20x is not a well-trod path. Raw touches are kept as a secondary
field, split per agent. Also carried: per-agent HANDOFF-at-boot adherence
(the operator called the T-1805 validation of this "great") and inventory
coverage (touched vs never-touched governance docs).

Modes (default-safe contract, sop/standard/auto-commit-cron-default-safe-contract.md):
  --format json               serve: print the committed snapshot (read-only; kino's entry)
  --collect --dry-run         compute the snapshot, print JSON, write nothing
  --collect --commit-prod     real run: write snapshot files, scope-guarded commit + push
  --collect --commit-prod --repo-root X [--no-push]
                              redirect to a test repo (hermetic smoke)
  (no mode flag)              refuse, exit 64 — no silent prod default

Snapshot files (SNAPSHOT_DIR under repo root):
  knowledge/stats/welltrod/latest.json            the served payload
  knowledge/stats/welltrod/history/YYYY-MM-DD.json  daily trail (pruned past HISTORY_KEEP_DAYS)

Usage:
    python3 tools/welltrod_stats.py --format json
    python3 tools/welltrod_stats.py --collect --dry-run --window-days 14
    python3 tools/welltrod_stats.py --collect --commit-prod   # timer invocation
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shlex
import subprocess
import sys
from collections import Counter
from pathlib import Path

# Bootstrap so `from tools.X import ...` works whether run as a script
# (`python3 tools/welltrod_stats.py`) or imported as a package module.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import doc_traversal_report as dtr  # noqa: E402

SCHEMA_VERSION = 1
DEFAULT_WINDOW_DAYS = 14
DEFAULT_MAX_SESSIONS = 60
SNAPSHOT_DIR = "knowledge/stats/welltrod"
HISTORY_KEEP_DAYS = 180
EX_USAGE = 64


# ---------------------------------------------------------------------------
# Session discovery (window-bounded, per agent)


def list_sessions_in_window(agent, cfg, cutoff_epoch, max_sessions):
    """Newest-first main-chain session paths with mtime >= cutoff_epoch.

    Local agent: plain glob + getmtime. Remote agents: mtime must be read
    INSIDE the sudo'd shell (700-mode homes; a bare glob from this account
    silently expands to nothing — CLAUDE.md §3), so use find -newermt.

    Returns None (NOT []) when the listing itself failed — a broken sudo
    grant or moved projects_dir must surface as listing_failed in the
    snapshot, never masquerade as "0 sessions" on the kino panel.
    """
    projects_dir = cfg["projects_dir"]
    if cfg["user"] is None:
        paths = [
            p
            for p in dtr.list_local_sessions(projects_dir, 10**6)
            if os.path.getmtime(p) >= cutoff_epoch
        ]
        return paths[:max_sessions]

    cmd = [
        "sudo",
        "-nu",
        cfg["user"],
        "bash",
        "-c",
        "find %s -maxdepth 1 -name '*.jsonl' -newermt @%d -printf '%%T@ %%p\\n' 2>/dev/null"
        % (shlex.quote(projects_dir), int(cutoff_epoch)),
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print("warn: listing %s sessions failed: %s" % (agent, exc), file=sys.stderr)
        return None
    if out.returncode != 0:
        print(
            "warn: listing %s sessions failed (rc=%d): %s"
            % (agent, out.returncode, out.stderr.strip()),
            file=sys.stderr,
        )
        return None
    entries = []
    for ln in out.stdout.splitlines():
        parts = ln.strip().split(" ", 1)
        if len(parts) == 2:
            try:
                entries.append((float(parts[0]), parts[1]))
            except ValueError:
                continue
    entries.sort(reverse=True)
    paths = dtr.filter_main_chain([p for _, p in entries])
    return paths[:max_sessions]


def gather_session_reports(agent, cfg, cutoff_epoch, max_sessions, repo_root):
    """Analyze every in-window session for one agent → list of report dicts.

    Returns None when the session listing failed (propagates the
    listing-failure sentinel from list_sessions_in_window)."""
    user = cfg["user"]
    paths = list_sessions_in_window(agent, cfg, cutoff_epoch, max_sessions)
    if paths is None:
        return None
    reports = []
    for path in paths:
        lines = dtr.load_session_lines(path, user=user)
        if lines is None:
            continue
        events = dtr.parse_transcript_lines(lines)
        if not events:
            continue
        session_id = os.path.basename(path)[: -len(".jsonl")]
        reports.append(dtr.analyze_session(session_id, events, repo_root))
    return reports


# ---------------------------------------------------------------------------
# Aggregation (pure — the unit-testable core)


def build_snapshot(
    reports_by_agent, repo_root, window_days, generated_at=None, failed_agents=None
):
    """Aggregate per-agent session reports into the snapshot payload.

    reports_by_agent: {agent: [analyze_session() dicts]}. Pure function of
    its inputs plus the repo's governance-doc inventory. Agents named in
    failed_agents get listing_failed=true — their zeros are a measurement
    outage, not a real quiet fortnight.
    """
    failed_agents = failed_agents or set()
    generated_at = generated_at or _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    per_doc = {}  # key -> {"sessions": set, "touches": int, per_agent: {...}}
    agents_out = {}
    total_sessions = 0

    for agent, reports in sorted(reports_by_agent.items()):
        handoff_reads = sum(1 for r in reports if r["boot"]["handoff_read"])
        agents_out[agent] = {
            "sessions_analyzed": len(reports),
            "boot_handoff_read": handoff_reads,
            "boot_handoff_rate": (
                round(handoff_reads / len(reports), 3) if reports else None
            ),
        }
        if agent in failed_agents:
            agents_out[agent]["listing_failed"] = True
        total_sessions += len(reports)

        for r in reports:
            for touch in r["governance_touches"]:
                key = dtr.normalize_gov_key(touch["path"], repo_root)
                slot = per_doc.setdefault(
                    key, {"sessions": set(), "touches": 0, "per_agent": {}}
                )
                slot["sessions"].add((agent, r["session_id"]))
                slot["touches"] += touch["count"]
                a = slot["per_agent"].setdefault(
                    agent, {"sessions": set(), "touches": 0}
                )
                a["sessions"].add(r["session_id"])
                a["touches"] += touch["count"]

    docs = []
    for key, slot in per_doc.items():
        docs.append(
            {
                "path": key,
                "well_trod": len(slot["sessions"]),
                "touches": slot["touches"],
                "per_agent": {
                    agent: {"sessions": len(a["sessions"]), "touches": a["touches"]}
                    for agent, a in sorted(slot["per_agent"].items())
                },
            }
        )
    docs.sort(key=lambda d: (-d["well_trod"], -d["touches"], d["path"]))

    inventory = dtr.inventory_governance_docs(repo_root)
    touched_keys = set(per_doc)
    never_touched = sorted(k for k in inventory if k not in touched_keys)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "window_days": window_days,
        "agents": agents_out,
        "totals": {"sessions_analyzed": total_sessions},
        "docs": docs,
        "coverage": {
            "inventory_size": len(inventory),
            "touched_count": len(touched_keys & set(inventory)),
            "never_touched": never_touched,
        },
    }


# ---------------------------------------------------------------------------
# Snapshot IO + prune


def write_snapshot(snapshot, repo_root):
    """Write latest.json + dated history file; prune old history.

    Returns repo-relative paths staged for commit (including deletions,
    which `git add` stages as removals)."""
    base = Path(repo_root) / SNAPSHOT_DIR
    hist = base / "history"
    hist.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(snapshot, indent=1, sort_keys=False) + "\n"
    (base / "latest.json").write_text(payload, encoding="utf-8")
    day = snapshot["generated_at"][:10]
    (hist / ("%s.json" % day)).write_text(payload, encoding="utf-8")

    cutoff_day = (
        _dt.datetime.strptime(day, "%Y-%m-%d")
        - _dt.timedelta(days=HISTORY_KEEP_DAYS)
    ).strftime("%Y-%m-%d")
    for old in hist.glob("*.json"):
        if old.stem < cutoff_day:
            old.unlink()
    return [SNAPSHOT_DIR + "/"]


def collect(repo_root, window_days, max_sessions, agents=None, gather_fn=None):
    """Run the full collection → snapshot dict. gather_fn injectable for tests."""
    gather = gather_fn or gather_session_reports
    cutoff = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=window_days)
    ).timestamp()
    reports_by_agent = {}
    failed = set()
    for agent in sorted(agents or dtr.AGENTS):
        cfg = dtr.AGENTS[agent]
        reports = gather(agent, cfg, cutoff, max_sessions, repo_root)
        if reports is None:
            failed.add(agent)
            reports = []
        reports_by_agent[agent] = reports
    return build_snapshot(
        reports_by_agent, repo_root, window_days, failed_agents=failed
    )


# ---------------------------------------------------------------------------
# CLI


def build_arg_parser():
    ap = argparse.ArgumentParser(
        description="Well-trod pathing stats — fleet doc-traversal snapshot (T-1811)"
    )
    ap.add_argument(
        "--collect", action="store_true", help="run fleet collection (needs a mode flag)"
    )
    ap.add_argument(
        "--commit-prod",
        action="store_true",
        help="collect mode: write snapshot + scope-guarded commit/push",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="collect mode: print JSON, write nothing"
    )
    ap.add_argument("--no-push", action="store_true", help="commit locally, skip push")
    ap.add_argument(
        "--format",
        choices=["json"],
        help="serve mode: print the committed latest.json (read-only)",
    )
    ap.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    ap.add_argument("--max-sessions", type=int, default=DEFAULT_MAX_SESSIONS)
    ap.add_argument(
        "--agents",
        default=None,
        help="comma-separated subset (default: all of %s)" % ",".join(sorted(dtr.AGENTS)),
    )
    ap.add_argument(
        "--repo-root",
        default=None,
        help="repo root override (tests/smoke; default: this checkout)",
    )
    return ap


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    repo_root = os.path.abspath(
        args.repo_root
        if args.repo_root
        else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    )

    # Serve mode — read-only, safe without --commit-prod. A missing snapshot
    # exits 0 with an error payload: the kino route treats non-zero exit as a
    # 500 and drops stdout, but "collector hasn't run yet" is a renderable
    # empty state, not a server fault.
    if args.format == "json" and not args.collect:
        latest = Path(repo_root) / SNAPSHOT_DIR / "latest.json"
        if not latest.exists():
            print(
                json.dumps({"error": "no_snapshot", "expected": str(latest)}),
                file=sys.stdout,
            )
            return 0
        sys.stdout.write(latest.read_text(encoding="utf-8"))
        return 0

    # Default-safe refusal (sop/standard/auto-commit-cron-default-safe-contract.md):
    # absence of an explicit mode NEVER resolves to a prod write.
    if not args.collect or not (args.commit_prod or args.dry_run):
        print(
            "welltrod_stats: refusing — pick a mode: --format json (serve), "
            "--collect --dry-run, or --collect --commit-prod  [exit %d]" % EX_USAGE,
            file=sys.stderr,
        )
        return EX_USAGE
    if args.commit_prod and args.dry_run:
        print("welltrod_stats: --commit-prod and --dry-run conflict", file=sys.stderr)
        return EX_USAGE

    agents = (
        [a.strip() for a in args.agents.split(",") if a.strip()]
        if args.agents
        else None
    )
    if agents:
        unknown = [a for a in agents if a not in dtr.AGENTS]
        if unknown:
            print("welltrod_stats: unknown agents %s" % unknown, file=sys.stderr)
            return EX_USAGE

    snapshot = collect(repo_root, args.window_days, args.max_sessions, agents)

    if args.dry_run:
        json.dump(snapshot, sys.stdout, indent=1)
        sys.stdout.write("\n")
        return 0

    from tools import _git_utils

    commit_msg = "chore(welltrod): refresh well-trod doc-traversal snapshot (T-1811)"
    if args.repo_root:
        # Hermetic test/smoke repo: direct write + synced commit is fine there.
        scope_paths = write_snapshot(snapshot, repo_root)
        ok, detail = _git_utils.commit_scoped(
            scope_paths,
            commit_msg,
            cwd=Path(repo_root),
            push=not args.no_push,
            allow_prefix=True,
        )
    else:
        # Real fire: never dirty the live shared checkout (T-0471 class — a
        # failed push would strand snapshot droppings and wedge every later
        # daily fire on "unstaged changes"). Write + commit inside a fresh
        # detached worktree off origin/main; push goes HEAD:main.
        def _land(wt_path):
            scope_paths = write_snapshot(snapshot, str(wt_path))
            return _git_utils.commit_scoped(
                scope_paths,
                commit_msg,
                cwd=wt_path,
                push=not args.no_push,
                sync=False,
                allow_prefix=True,
            )

        try:
            ok, detail = _git_utils.with_worktree(_land)
        except _git_utils.WorktreeError as exc:
            print("welltrod_stats: worktree failed: %s" % exc, file=sys.stderr)
            return 1

    if not ok:
        print("welltrod_stats: commit failed: %s" % detail, file=sys.stderr)
        return 1
    print("welltrod_stats: snapshot committed (%s)" % detail, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
