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

T-4490 additions (all in tools/welltrod_alltime.py; the 14d `docs` array and
schema_version are unchanged, so the kino panel is unaffected):
  * `long_window` — a second window (--long-window-days, default 30) reported next
    to the primary one. ONE transcript pass at the long window; both views are cut
    from it by transcript mtime.
  * `trend` — per doc, the 14d well-trod count now vs the history snapshots 7 and
    14 days ago. Each baseline names who was measured in it (a 0 for an agent is
    unmeasured, not quiet) and the block carries its own overlap/caveat text.
  * `all_time` — a durable cumulative tally from alltime.jsonl: one row per
    (session_id, doc), appended each run only when new or grown, so counts survive
    transcript cleanup (~30 days) and re-counting a live session never doubles it.
    A frozen, APPROXIMATE pre-store estimate recovered from history/ rides alongside,
    reported separately (--backfill-from-history).

Modes (continued):
  --backfill-from-history --until D (--dry-run | --write)
                              estimate the pre-store period from history/ snapshots;
                              reads no transcripts, no sudo, no git

Snapshot files (SNAPSHOT_DIR under repo root):
  knowledge/stats/welltrod/latest.json            the served payload (+ trend, all_time)
  knowledge/stats/welltrod/history/YYYY-MM-DD.json  daily trail, the windowed
                                                  measurement only (pruned past
                                                  HISTORY_KEEP_DAYS)
  knowledge/stats/welltrod/alltime.jsonl          durable all-time store (never pruned)
  knowledge/stats/welltrod/alltime-pre-store.json frozen approximate estimate, pre-store

Usage:
    python3 tools/welltrod_stats.py --format json
    python3 tools/welltrod_stats.py --collect --dry-run --window-days 14
    python3 tools/welltrod_stats.py --collect --commit-prod   # timer invocation
    python3 tools/welltrod_stats.py --backfill-from-history --until 2026-09-01 --dry-run
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob as _glob
import json
import os
import shlex
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

# Bootstrap so `from tools.X import ...` works whether run as a script
# (`python3 tools/welltrod_stats.py`) or imported as a package module.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import doc_traversal_report as dtr  # noqa: E402
from tools import welltrod_alltime as wa  # noqa: E402

SCHEMA_VERSION = 1
DEFAULT_WINDOW_DAYS = 14
# Second window (T-4490): ~ the transcript-retention horizon, so a longer one would
# only be empty. 0 disables it.
DEFAULT_LONG_WINDOW_DAYS = 30
# Per-agent runaway guard on the (long-window) pass, not a sampling knob: recovery alone
# runs >60 sessions in 30 days, which the old cap of 60 would silently truncate.
DEFAULT_MAX_SESSIONS = 250
# Subagent transcripts outnumber main sessions ~4:1 (T-4509), so they get their own cap.
DEFAULT_MAX_SUBAGENT_SESSIONS = 1000
SNAPSHOT_DIR = wa.SNAPSHOT_DIR
HISTORY_KEEP_DAYS = 180
EX_USAGE = 64


# ---------------------------------------------------------------------------
# Session discovery (window-bounded, per agent)


def list_session_entries(agent, cfg, cutoff_epoch):
    """Newest-first [(mtime, path)] of main-chain sessions with mtime >= cutoff_epoch.

    Local agent: plain glob + getmtime. Remote agents: mtime must be read
    INSIDE the sudo'd shell (700-mode homes; a bare glob from this account
    silently expands to nothing — CLAUDE.md §3), so use find -newermt.

    Returns None (NOT []) when the listing itself failed — a broken sudo
    grant or moved projects_dir must surface as listing_failed in the
    snapshot, never masquerade as "0 sessions" on the kino panel.
    """
    projects_dir = cfg["projects_dir"]
    if cfg["user"] is None:
        entries = [
            (os.path.getmtime(p), p) for p in dtr.list_local_sessions(projects_dir, 10**6)
        ]
        return sorted((e for e in entries if e[0] >= cutoff_epoch), reverse=True)

    # T-4671: seat_argv drops the sudo when the collector IS this account (sudo-to-self
    # fails, which would read as listing_failed).
    cmd = dtr.seat_argv(cfg["user"], [
        "bash",
        "-c",
        "find %s -maxdepth 1 -name '*.jsonl' -newermt @%d -printf '%%T@ %%p\\n' 2>/dev/null"
        % (shlex.quote(projects_dir), int(cutoff_epoch)),
    ])
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
    keep = set(dtr.filter_main_chain([p for _, p in entries]))
    return [(m, p) for m, p in entries if p in keep]


def list_subagent_entries(agent, cfg, cutoff_epoch):
    """Newest-first [(mtime, path)] of subagent transcripts (T-4509):
    <projects_dir>/<session-id>/subagents/agent-*.jsonl with mtime >= cutoff_epoch.

    Same local/remote split and the same None-on-failure sentinel as
    list_session_entries (listing_failed, never a silent 0)."""
    projects_dir = cfg["projects_dir"]
    if cfg["user"] is None:
        if not os.path.isdir(projects_dir):
            # glob turns a missing dir into [], which would read as "0 subagents".
            print("warn: %s projects_dir missing: %s" % (agent, projects_dir), file=sys.stderr)
            return None
        pattern = os.path.join(projects_dir, "*", "subagents", "agent-*.jsonl")
        entries = []
        for p in _glob.glob(pattern):
            try:
                entries.append((os.path.getmtime(p), p))
            except OSError:
                continue
        return sorted((e for e in entries if e[0] >= cutoff_epoch), reverse=True)

    # T-4671: seat_argv drops the sudo when the collector IS this account (sudo-to-self
    # fails, which would read as listing_failed).
    cmd = dtr.seat_argv(cfg["user"], [
        "bash",
        "-c",
        "find %s -maxdepth 3 -path '*/subagents/*' -name 'agent-*.jsonl' "
        "-newermt @%d -printf '%%T@ %%p\\n' 2>/dev/null"
        % (shlex.quote(projects_dir), int(cutoff_epoch)),
    ])
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print("warn: listing %s subagents failed: %s" % (agent, exc), file=sys.stderr)
        return None
    if out.returncode != 0:
        print(
            "warn: listing %s subagents failed (rc=%d): %s"
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
    return entries


def list_sessions_in_window(agent, cfg, cutoff_epoch, max_sessions):
    """Newest-first main-chain session paths with mtime >= cutoff_epoch, capped.

    Returns None when the listing failed (see list_session_entries)."""
    entries = list_session_entries(agent, cfg, cutoff_epoch)
    if entries is None:
        return None
    return [p for _, p in entries][:max_sessions]


def gather_session_reports(agent, cfg, cutoff_epoch, max_sessions, repo_root):
    """Analyze every in-window session for one agent → list of report dicts.

    Each report carries `mtime` (transcript mtime, epoch seconds) so one pass at
    the longest window can be re-cut into shorter windows. Returns None when the
    session listing failed (propagates the listing-failure sentinel)."""
    user = cfg["user"]
    entries = list_session_entries(agent, cfg, cutoff_epoch)
    if entries is None:
        return None
    reports = []
    for mtime, path in entries[:max_sessions]:
        lines = dtr.load_session_lines(path, user=user)
        if lines is None:
            continue
        events = dtr.parse_transcript_lines(lines)
        if not events:
            continue
        session_id = os.path.basename(path)[: -len(".jsonl")]
        report = dtr.analyze_session(session_id, events, repo_root)
        report["mtime"] = mtime
        reports.append(report)
    return reports


def gather_subagent_reports(agent, cfg, cutoff_epoch, max_sessions, repo_root):
    """gather_session_reports for subagent transcripts (T-4509). Parses with
    include_sidechain=True (every record in them is a sidechain record); session id
    is the agent file's stem, unique across the fleet. None on listing failure."""
    user = cfg["user"]
    entries = list_subagent_entries(agent, cfg, cutoff_epoch)
    if entries is None:
        return None
    reports = []
    for mtime, path in entries[:max_sessions]:
        lines = dtr.load_session_lines(path, user=user)
        if lines is None:
            continue
        events = dtr.parse_transcript_lines(lines, include_sidechain=True)
        if not events:
            continue
        report = dtr.analyze_session(
            os.path.basename(path)[: -len(".jsonl")], events, repo_root
        )
        report["mtime"] = mtime
        reports.append(report)
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


def write_snapshot(snapshot, repo_root, rows=None):
    """Write latest.json + dated history file; prune old history.

    rows=None is the legacy shape: latest.json and the history file are the same
    payload. With rows (a list, possibly empty), the all-time store under repo_root
    ingests them first, and latest.json becomes snapshot + `trend` + `all_time`
    while the history file stays the windowed measurement alone — the store is the
    source of truth for all-time, and copying it into up to 180 daily files would
    only bloat the trail.

    Returns repo-relative paths staged for commit (including deletions,
    which `git add` stages as removals). The store and the frozen estimate live
    in the same directory, so the one scope path covers them."""
    base = Path(repo_root) / SNAPSHOT_DIR
    hist = base / "history"
    hist.mkdir(parents=True, exist_ok=True)

    history_payload = json.dumps(snapshot, indent=1, sort_keys=False) + "\n"
    if rows is None:
        latest_payload = history_payload
    else:
        wa.ingest(repo_root, rows)
        latest = wa.finalize_snapshot(snapshot, repo_root)
        latest_payload = json.dumps(latest, indent=1, sort_keys=False) + "\n"
    (base / "latest.json").write_text(latest_payload, encoding="utf-8")
    day = snapshot["generated_at"][:10]
    (hist / ("%s.json" % day)).write_text(history_payload, encoding="utf-8")

    cutoff_day = (
        _dt.datetime.strptime(day, "%Y-%m-%d")
        - _dt.timedelta(days=HISTORY_KEEP_DAYS)
    ).strftime("%Y-%m-%d")
    for old in hist.glob("*.json"):
        if old.stem < cutoff_day:
            old.unlink()
    return [SNAPSHOT_DIR + "/"]


def filter_reports_window(reports_by_agent, cutoff_epoch):
    """Reports whose transcript mtime is >= cutoff_epoch. A report with no mtime
    (hand-built fixtures) is kept: absence of the field must not drop data."""
    return {
        agent: [
            r for r in reports if r.get("mtime") is None or r["mtime"] >= cutoff_epoch
        ]
        for agent, reports in reports_by_agent.items()
    }


def build_long_window(reports_by_agent, repo_root, window_days):
    """The compact long-window block: distinct-session counts per doc, no per-agent
    split or raw touches (those live in the primary window and the all-time store)."""
    snap = build_snapshot(reports_by_agent, repo_root, window_days)
    return {
        "window_days": window_days,
        "sessions_analyzed": snap["totals"]["sessions_analyzed"],
        "well_trod": {d["path"]: d["well_trod"] for d in snap["docs"]},
    }


def collect_all(
    repo_root,
    window_days,
    max_sessions,
    agents=None,
    gather_fn=None,
    long_window_days=DEFAULT_LONG_WINDOW_DAYS,
    subagent_gather_fn=None,
    max_subagent_sessions=DEFAULT_MAX_SUBAGENT_SESSIONS,
):
    """Run the full collection → (snapshot dict, all-time store rows).

    ONE transcript pass per agent, at the longest window (the cost is a sudo cat
    per transcript); the primary window and the long window are cut from it by
    transcript mtime, and every session the pass saw feeds the all-time rows.
    gather_fn injectable for tests. Subagent transcripts (T-4509) ride the same pass
    into a separate `subagents` block + `source: "subagent"` rows.
    """
    # Hermetic-skip: an injected gather_fn without subagent_gather_fn skips subagents,
    # so fake-gather tests never reach live seats; production (no injection) gathers them.
    do_subagents = gather_fn is None or subagent_gather_fn is not None
    sub_gather = subagent_gather_fn or gather_subagent_reports
    gather = gather_fn or gather_session_reports
    now = time.time()
    pass_days = max(window_days, long_window_days or 0)
    cutoff = now - pass_days * 86400
    reports_by_agent = {}
    failed = set()
    for agent in sorted(agents or dtr.AGENTS):
        cfg = dtr.AGENTS[agent]
        reports = gather(agent, cfg, cutoff, max_sessions, repo_root)
        if reports is None:
            failed.add(agent)
            reports = []
        reports_by_agent[agent] = reports

    sub_by_agent, sub_failed = {}, set()
    if do_subagents:
        for agent in sorted(agents or dtr.AGENTS):
            reports = sub_gather(
                agent, dtr.AGENTS[agent], cutoff, max_subagent_sessions, repo_root
            )
            if reports is None:
                sub_failed.add(agent)
                reports = []
            sub_by_agent[agent] = reports

    generated_at = _dt.datetime.fromtimestamp(now, _dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    primary = filter_reports_window(reports_by_agent, now - window_days * 86400)
    snapshot = build_snapshot(
        primary,
        repo_root,
        window_days,
        generated_at=generated_at,
        failed_agents=failed,
    )
    if long_window_days:
        snapshot["long_window"] = build_long_window(
            filter_reports_window(reports_by_agent, now - long_window_days * 86400),
            repo_root,
            long_window_days,
        )
    if do_subagents:
        sub_snap = build_snapshot(
            filter_reports_window(sub_by_agent, now - window_days * 86400),
            repo_root,
            window_days,
            generated_at=generated_at,
            failed_agents=sub_failed,
        )
        snapshot["subagents"] = {
            "window_days": window_days,
            "agents": sub_snap["agents"],
            "totals": sub_snap["totals"],
            "docs": sub_snap["docs"],
        }
    rows = wa.session_doc_rows(reports_by_agent, repo_root, generated_at[:10])
    if do_subagents:
        rows += wa.session_doc_rows(
            sub_by_agent, repo_root, generated_at[:10], source="subagent"
        )
    return snapshot, rows


def collect(
    repo_root,
    window_days,
    max_sessions,
    agents=None,
    gather_fn=None,
    long_window_days=0,
    subagent_gather_fn=None,
):
    """collect_all, snapshot only (the pre-T-4490 shape; the long window is opt-in here)."""
    return collect_all(
        repo_root,
        window_days,
        max_sessions,
        agents,
        gather_fn,
        long_window_days,
        subagent_gather_fn,
    )[0]


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
    ap.add_argument(
        "--long-window-days",
        type=int,
        default=DEFAULT_LONG_WINDOW_DAYS,
        help="second, longer window reported alongside --window-days; the single "
        "transcript pass reaches back this far and every session it sees feeds the "
        "all-time store (0 disables; default %d)" % DEFAULT_LONG_WINDOW_DAYS,
    )
    ap.add_argument("--max-sessions", type=int, default=DEFAULT_MAX_SESSIONS)
    ap.add_argument(
        "--max-subagent-sessions",
        type=int,
        default=DEFAULT_MAX_SUBAGENT_SESSIONS,
        help="per-agent cap on subagent transcripts (default %d)"
        % DEFAULT_MAX_SUBAGENT_SESSIONS,
    )
    ap.add_argument(
        "--backfill-from-history",
        action="store_true",
        help="estimate the pre-store period from history/ snapshots (needs --until and "
        "exactly one of --dry-run / --write; read-only otherwise, never touches git)",
    )
    ap.add_argument(
        "--until",
        default=None,
        help="backfill: last snapshot date (YYYY-MM-DD) the estimate may use; set it to "
        "where the exact store's coverage begins",
    )
    ap.add_argument(
        "--write",
        action="store_true",
        help="backfill mode: write alltime-pre-store.json under --repo-root (no commit)",
    )
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

    # Backfill mode (T-4490): history/ -> frozen pre-store estimate. Same default-safe
    # shape: it needs --until and EXACTLY one of --dry-run (print) / --write (one file
    # under --repo-root, no git). It reads no transcripts and never needs sudo.
    if args.backfill_from_history:
        if (
            not args.until
            or args.dry_run == args.write
            or args.collect
            or args.commit_prod
        ):
            print(
                "welltrod_stats: --backfill-from-history needs --until YYYY-MM-DD and "
                "exactly one of --dry-run / --write (and no --collect)  [exit %d]"
                % EX_USAGE,
                file=sys.stderr,
            )
            return EX_USAGE
        try:
            _dt.datetime.strptime(args.until, "%Y-%m-%d")
        except ValueError:
            print("welltrod_stats: --until must be YYYY-MM-DD", file=sys.stderr)
            return EX_USAGE
        estimate = wa.build_pre_store_estimate(repo_root, args.until)
        if args.write:
            path = wa.write_pre_store_estimate(repo_root, estimate)
            print("welltrod_stats: wrote %s" % path, file=sys.stderr)
        else:
            json.dump(estimate, sys.stdout, indent=1)
            sys.stdout.write("\n")
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

    snapshot, rows = collect_all(
        repo_root,
        args.window_days,
        args.max_sessions,
        agents,
        long_window_days=args.long_window_days,
        max_subagent_sessions=args.max_subagent_sessions,
    )

    if args.dry_run:
        # Preview what a real run would publish: the store as committed here plus
        # this run's rows merged in memory. Nothing is written.
        preview = wa.finalize_snapshot(snapshot, repo_root, pending_rows=rows)
        json.dump(preview, sys.stdout, indent=1)
        sys.stdout.write("\n")
        return 0

    from tools import _git_utils

    commit_msg = "chore(welltrod): refresh well-trod doc-traversal snapshot (T-1811)"
    if args.repo_root:
        # Hermetic test/smoke repo: direct write + synced commit is fine there.
        scope_paths = write_snapshot(snapshot, repo_root, rows=rows)
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
            # The store is read and appended HERE, in the fresh origin/main
            # worktree — never from the live checkout — so the dedupe base is
            # exactly what is committed, and a failed push strands nothing.
            scope_paths = write_snapshot(snapshot, str(wt_path), rows=rows)
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
