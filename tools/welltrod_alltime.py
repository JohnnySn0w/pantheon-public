"""Well-trod all-time tally, trend and history estimate (T-4490, under T-1811).

welltrod_stats.py publishes a rolling 14-day window. Transcripts are cleaned after
~30 days and every daily snapshot is just that window, so on its own the measure
forgets. This module adds the three things that make it durable and comparable:

  * a durable ALL-TIME store (`alltime.jsonl`): one row per (session_id, doc),
    append-only, deduped, so counts survive transcript cleanup;
  * a TREND per doc from `history/` snapshots (14d count now vs 7 and 14 days ago);
  * a frozen, APPROXIMATE pre-store estimate recovered from `history/` for the
    period before the store existed (snapshots carry counts, never session ids).

Pure functions over files under <repo>/knowledge/stats/welltrod/; no sudo, no git.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import doc_traversal_report as dtr  # noqa: E402

SNAPSHOT_DIR = "knowledge/stats/welltrod"
STORE_FILE = "alltime.jsonl"
PRE_STORE_FILE = "alltime-pre-store.json"
TREND_OFFSETS = (7, 14)
TREND_TOLERANCE_DAYS = 1
TILE_DAYS = 14
SCHEMA_VERSION = 1

# The one place the "a 0 is not quiet" fact lives; both caveats below embed it (P1: one
# source, so the trend and the estimate cannot drift apart). Every claim in it is read off
# history/*.json (recovery 0 for 30 straight snapshots, no listing_failed; specialist 14 -> 2 and
# recovery 44 -> 18 between 2026-08-09 and 08-10) and the T-2687 / T-4159 commits and ticket.
UNMEASURED_NOTE = (
    "A 0 in a sessions_analyzed_by_agent map means that agent was unmeasured, not quiet: "
    "recovery reads 0 in every history snapshot from 2026-08-30 through 2026-09-28 with no "
    "listing_failed flag, because the projects dir welltrod read for it held none of its "
    "live sessions (T-4159 fixed that lookup on 2026-09-28). The same fault can leave a "
    "count partial instead of zero (specialist and recovery both fall sharply on 2026-08-10, the day "
    "after T-2687 changed how that dir is derived), so read each agent's count against its "
    "neighbours before trusting a doc's change as real."
)

ESTIMATE_CAVEAT = (
    "APPROXIMATE. Recovered from history/ snapshots, which carry per-doc distinct-session "
    "COUNTS but no session ids: this sums disjoint 14-day tiles, so it cannot be deduped "
    "against the exact store. A long-lived session that straddles a tile edge is counted "
    "once per tile, and a session active on both sides of `until` can appear in both the "
    "estimate and the exact store. Nothing before the first snapshot is recoverable. "
    + UNMEASURED_NOTE
    + " The estimate therefore undercounts an agent in every tile where its count is 0 "
    "or partial."
)

TREND_CAVEAT = (
    "Each baseline is itself a rolling 14-day window: the 7d_ago snapshot overlaps the "
    "current window by 50%, the 14d_ago snapshot is the adjacent, non-overlapping "
    "fortnight. "
    + UNMEASURED_NOTE
    + " A doc then reads as if that agent had not touched it, so compare `now` against a "
    "baseline only for agents measured in both."
)


def _day_of(epoch):
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime("%Y-%m-%d")


def _date(s):
    return _dt.datetime.strptime(s, "%Y-%m-%d").date()


def _shift(date_str, days):
    return (_date(date_str) + _dt.timedelta(days=days)).strftime("%Y-%m-%d")


def _canon(path):
    """History keys written before `adr/` moved under `architecture/` (2026-09-06,
    T-3472) say `adr/X.md`; later ones say `architecture/adr/X.md`. Same doc."""
    if path.startswith("adr/"):
        return "architecture/" + path
    return path


# ---------------------------------------------------------------------------
# Rows + durable store


def session_doc_rows(reports_by_agent, repo_root, fallback_day, source="main"):
    """Flatten session reports into store rows (one per agent, session, doc).

    Raw touch paths that normalize to the same governance key are summed. `day` is
    the transcript mtime date (UTC) when the report carries one, else fallback_day.

    source="subagent" stamps `source: "subagent"` on each row (T-4509); main rows
    carry no `source` field, so they stay byte-identical to pre-T-4509 rows and the
    loader reads an absent field as main."""
    rows = []
    for agent, reports in sorted(reports_by_agent.items()):
        for r in reports:
            day = _day_of(r["mtime"]) if r.get("mtime") is not None else fallback_day
            per_doc = {}
            for touch in r["governance_touches"]:
                key = dtr.normalize_gov_key(touch["path"], repo_root)
                per_doc[key] = per_doc.get(key, 0) + touch["count"]
            for doc in sorted(per_doc):
                row = {
                    "session_id": r["session_id"],
                    "agent": agent,
                    "doc": doc,
                    "touches": per_doc[doc],
                    "day": day,
                }
                if source != "main":
                    row["source"] = source
                rows.append(row)
    return rows


def store_path(repo_root):
    return Path(repo_root) / SNAPSHOT_DIR / STORE_FILE


def _parse_store_row(line):
    """The normalized row for one JSONL line, or raises ValueError/KeyError/TypeError/
    OverflowError if it is not a usable store row. Every field the rest of the module
    hashes, compares, sums or min()s is type-checked HERE, so a parseable-but-mistyped
    row is skipped at the door instead of crashing a later step (T-4490 review r1)."""
    rec = json.loads(line)
    sid, doc, raw = rec["session_id"], rec["doc"], rec["touches"]
    if not isinstance(sid, str) or not isinstance(doc, str):
        raise TypeError("session_id and doc must be strings")
    if isinstance(raw, bool):
        raise TypeError("touches must be a number, not a bool")
    rec["touches"] = int(raw)  # "5" -> 5; Infinity -> OverflowError; NaN -> ValueError
    # Optional fields feed a dict key (agent) and a min() (day): coerce rather than
    # drop, because the count on the row is still good.
    if not isinstance(rec.get("agent"), str):
        rec["agent"] = "?"
    if not isinstance(rec.get("day"), str):
        rec["day"] = None
    # T-4509: rows written before subagents were counted have no `source`: they are main.
    if not isinstance(rec.get("source"), str):
        rec["source"] = "main"
    return rec


def load_store(path):
    """{(session_id, doc): row} with the highest `touches` winning; malformed
    lines are skipped with a stderr warning (the daily run must not die on them)."""
    store = {}
    path = Path(path)
    if not path.exists():
        return store
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = _parse_store_row(line)
            except (ValueError, KeyError, TypeError, OverflowError):
                print(
                    "warn: %s line %d is not a store row; skipped" % (path.name, lineno),
                    file=sys.stderr,
                )
                continue
            key = (rec["session_id"], rec["doc"])
            if key not in store or rec["touches"] > store[key]["touches"]:
                store[key] = rec
    return store


def rows_to_append(store, rows):
    """Rows that are new, or whose touches exceed the stored count (a live session
    that kept growing). Unchanged and shrunken rows are dropped; a key repeated inside
    one batch collapses to its highest-touch row."""
    best = {k: v["touches"] for k, v in store.items()}
    chosen = {}
    for r in rows:
        key = (r["session_id"], r["doc"])
        if key not in best or r["touches"] > best[key]:
            best[key] = r["touches"]
            chosen[key] = r
    return list(chosen.values())


def append_rows(path, rows):
    """Append rows as JSONL; repairs a missing final newline first. Returns count."""
    if not rows:
        return 0
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_nl = False
    if path.exists() and path.stat().st_size:
        with open(path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            needs_nl = fh.read(1) != b"\n"
    with open(path, "a", encoding="utf-8") as fh:
        if needs_nl:
            fh.write("\n")
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    return len(rows)


def ingest(repo_root, rows):
    """load_store + rows_to_append + append_rows. Returns the number appended."""
    path = store_path(repo_root)
    return append_rows(path, rows_to_append(load_store(path), rows))


def _merged(store, pending_rows):
    merged = dict(store)
    for r in pending_rows:
        key = (r["session_id"], r["doc"])
        if key not in merged or r["touches"] > merged[key]["touches"]:
            merged[key] = r
    return merged


def summarize_all_time(store, pre_store=None):
    """The `all_time` block of latest.json from a loaded store (+ optional estimate)."""
    per_doc = {}
    sub_doc = {}  # T-4509: subagent rows, a separate column; never folded into per_doc
    sessions = set()
    sub_sessions = set()
    days = []
    for (sid, doc), r in store.items():
        if r.get("source", "main") == "subagent":
            sub_sessions.add(sid)
            s = sub_doc.setdefault(doc, {"sessions": 0, "touches": 0, "per_agent": {}})
            s["sessions"] += 1
            s["touches"] += r["touches"]
            sa = s["per_agent"].setdefault(r.get("agent", "?"), {"sessions": 0, "touches": 0})
            sa["sessions"] += 1
            sa["touches"] += r["touches"]
            continue
        sessions.add(sid)
        days.append(r.get("day"))
        slot = per_doc.setdefault(doc, {"sessions": 0, "touches": 0, "per_agent": {}})
        slot["sessions"] += 1
        slot["touches"] += r["touches"]
        a = slot["per_agent"].setdefault(r.get("agent", "?"), {"sessions": 0, "touches": 0})
        a["sessions"] += 1
        a["touches"] += r["touches"]

    est = {d["path"]: d for d in (pre_store or {}).get("docs", [])}
    docs = []
    for doc in set(per_doc) | set(est) | set(sub_doc):
        slot = per_doc.get(doc, {"sessions": 0, "touches": 0, "per_agent": {}})
        entry = {
            "path": doc,
            "sessions": slot["sessions"],
            "touches": slot["touches"],
            "per_agent": {a: slot["per_agent"][a] for a in sorted(slot["per_agent"])},
        }
        sd = sub_doc.get(doc, {"sessions": 0, "touches": 0, "per_agent": {}})
        entry["subagent_sessions"] = sd["sessions"]
        entry["subagent_touches"] = sd["touches"]
        entry["per_agent_subagent"] = {a: sd["per_agent"][a] for a in sorted(sd["per_agent"])}
        if doc in est:
            entry["pre_store_sessions_est"] = est[doc]["sessions"]
            entry["pre_store_touches_est"] = est[doc]["touches"]
        docs.append(entry)
    docs.sort(
        key=lambda d: (-d["sessions"], -d["touches"], -d.get("pre_store_sessions_est", 0), d["path"])
    )

    out = {
        "store": SNAPSHOT_DIR + "/" + STORE_FILE,
        "sessions_counted": len(sessions),
        "subagent_sessions_counted": len(sub_sessions),
        "earliest_session_day": min((d for d in days if d), default=None),
        "docs": docs,
    }
    if pre_store:
        out["pre_store_estimate"] = {
            "approximate": True,
            "until": pre_store.get("until"),
            "tiles": [t["snapshot"] for t in pre_store.get("tiles", [])],
            # who was measured in each tile; `tiles` stays a plain date list for
            # existing readers, so this rides alongside rather than replacing it
            "tile_sessions_analyzed_by_agent": {
                t["snapshot"]: t.get("sessions_analyzed_by_agent", {})
                for t in pre_store.get("tiles", [])
            },
            "skipped": pre_store.get("skipped", []),
            "caveat": pre_store.get("caveat", ESTIMATE_CAVEAT),
        }
    return out


# ---------------------------------------------------------------------------
# History snapshots: trend + pre-store estimate


def _history_dates(repo_root):
    hist = Path(repo_root) / SNAPSHOT_DIR / "history"
    if not hist.is_dir():
        return []
    return sorted(p.stem for p in hist.glob("*.json"))


def _load_history(repo_root, date):
    """The snapshot dict for `date`, or None if unreadable or not a snapshot."""
    path = Path(repo_root) / SNAPSHOT_DIR / "history" / (date + ".json")
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(snap, dict) or not isinstance(snap.get("docs"), list):
        return None
    return snap


def _nearest_snapshot(repo_root, dates, target, tolerance_days):
    """(date, snapshot) for the readable snapshot closest to `target` within
    tolerance (tie -> earlier), else None."""
    ranked = sorted(dates, key=lambda d: (abs((_date(d) - _date(target)).days), d))
    for cand in ranked:
        if abs((_date(cand) - _date(target)).days) > tolerance_days:
            break
        snap = _load_history(repo_root, cand)
        if snap is not None:
            return cand, snap
    return None


def _sessions_by_agent(snap):
    """{agent: sessions_analyzed} from a history snapshot's `agents` block. A 0 here with
    no listing_failed flag is NOT a quiet agent: see TREND_CAVEAT / ESTIMATE_CAVEAT."""
    agents = snap.get("agents")
    if not isinstance(agents, dict):
        return {}
    return {
        a: v["sessions_analyzed"]
        for a, v in sorted(agents.items())
        if isinstance(v, dict) and isinstance(v.get("sessions_analyzed"), int)
    }


def build_trend(repo_root, today, now_docs):
    """The `trend` block: per doc, well_trod now vs the 14d counts in the history
    snapshots dated today-7d and today-14d (None where no snapshot exists).

    The snapshots are themselves rolling 14-day windows: the 14d-ago one is the
    adjacent, non-overlapping fortnight; the 7d-ago one overlaps this one by half.
    Each basis entry carries the snapshot date and who was measured in it
    (`sessions_analyzed_by_agent`); the same warnings ride in `caveat`."""
    available = _history_dates(repo_root)
    labels = ["%dd_ago" % n for n in TREND_OFFSETS]
    basis, prior = {}, {}
    for label, n in zip(labels, TREND_OFFSETS):
        pick = _nearest_snapshot(repo_root, available, _shift(today, -n), TREND_TOLERANCE_DAYS)
        basis[label] = (
            {"snapshot": pick[0], "sessions_analyzed_by_agent": _sessions_by_agent(pick[1])}
            if pick
            else None
        )
        prior[label] = None
        if pick:
            prior[label] = {}
            for d in pick[1]["docs"]:
                key = _canon(d["path"])
                prior[label][key] = prior[label].get(key, 0) + d["well_trod"]

    now = {d["path"]: d["well_trod"] for d in now_docs}
    paths = set(now)
    for m in prior.values():
        if m:
            paths |= set(m)
    docs = []
    for path in paths:
        entry = {"path": path, "now": now.get(path, 0)}
        for label in labels:
            entry[label] = None if prior[label] is None else prior[label].get(path, 0)
        docs.append(entry)
    docs.sort(key=lambda d: (-d["now"], -(d["14d_ago"] or 0), -(d["7d_ago"] or 0), d["path"]))
    return {"basis": basis, "caveat": TREND_CAVEAT, "docs": docs}


def build_pre_store_estimate(repo_root, until, generated_at=None):
    """Sum disjoint 14d history tiles ending at or before `until` into an
    APPROXIMATE per-doc estimate. Skips tiles whose agents had listing_failed."""
    generated_at = generated_at or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    available = _history_dates(repo_root)
    tiles, skipped, totals = [], [], {}

    # the chain starts at the latest snapshot on or before `until`
    on_or_before = [d for d in available if d <= until]
    anchor = on_or_before[-1] if on_or_before else None
    seen = set()
    while anchor is not None and anchor not in seen:
        seen.add(anchor)
        snap = _load_history(repo_root, anchor)
        failed = sorted(
            a
            for a, v in ((snap or {}).get("agents") or {}).items()
            if isinstance(v, dict) and v.get("listing_failed")
        )
        if snap is None:
            skipped.append({"snapshot": anchor, "reason": "unreadable snapshot"})
        elif snap.get("window_days") != TILE_DAYS:
            skipped.append(
                {"snapshot": anchor, "reason": "window_days=%s, not %d" % (snap.get("window_days"), TILE_DAYS)}
            )
        elif failed:
            skipped.append({"snapshot": anchor, "reason": "listing_failed: " + ",".join(failed)})
        else:
            tiles.append(
                {
                    "snapshot": anchor,
                    "covers": [_shift(anchor, -TILE_DAYS), anchor],
                    "sessions_analyzed": (snap.get("totals") or {}).get("sessions_analyzed"),
                    "sessions_analyzed_by_agent": _sessions_by_agent(snap),
                }
            )
            for d in snap["docs"]:
                slot = totals.setdefault(_canon(d["path"]), {"sessions": 0, "touches": 0, "per_agent": {}})
                slot["sessions"] += d["well_trod"]
                slot["touches"] += d.get("touches", 0)
                for agent, v in (d.get("per_agent") or {}).items():
                    a = slot["per_agent"].setdefault(agent, {"sessions": 0, "touches": 0})
                    a["sessions"] += v.get("sessions", 0)
                    a["touches"] += v.get("touches", 0)
        # the next tile must END one window earlier; a gap ends the chain (never leap a hole)
        nxt = _nearest_snapshot(repo_root, available, _shift(anchor, -TILE_DAYS), TREND_TOLERANCE_DAYS)
        anchor = nxt[0] if nxt else None

    docs = [
        {
            "path": path,
            "sessions": v["sessions"],
            "touches": v["touches"],
            "per_agent": {a: v["per_agent"][a] for a in sorted(v["per_agent"])},
        }
        for path, v in totals.items()
    ]
    docs.sort(key=lambda d: (-d["sessions"], -d["touches"], d["path"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "history_estimate",
        "approximate": True,
        "generated_at": generated_at,
        "until": until,
        "tile_days": TILE_DAYS,
        "tiles": tiles,
        "skipped": skipped,
        "caveat": ESTIMATE_CAVEAT,
        "docs": docs,
    }


def write_pre_store_estimate(repo_root, estimate):
    path = Path(repo_root) / SNAPSHOT_DIR / PRE_STORE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(estimate, indent=1) + "\n", encoding="utf-8")
    return path


def load_pre_store_estimate(repo_root):
    path = Path(repo_root) / SNAPSHOT_DIR / PRE_STORE_FILE
    try:
        est = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return est if isinstance(est, dict) else None


# ---------------------------------------------------------------------------
# Assembly


def finalize_snapshot(base, repo_root, pending_rows=()):
    """base snapshot + `trend` + `all_time`, computed against the store under
    repo_root (plus pending_rows merged in memory, for dry-run previews)."""
    out = dict(base)
    today = base["generated_at"][:10]
    out["trend"] = build_trend(repo_root, today, base.get("docs", []))
    store = _merged(load_store(store_path(repo_root)), pending_rows)
    out["all_time"] = summarize_all_time(store, load_pre_store_estimate(repo_root))
    return out
