#!/usr/bin/env python3
"""Transcript-mining observability — governance-doc traversal report (T-1805).

Parses Claude Code session transcript jsonl files and reports how an agent
actually traversed the repo's documentation surface: which governance docs
(sop/, adr/, rulebook/, HANDOFF.md, repo README, notes/recent-policy-changes.md)
were touched, whether boot-time doc adherence happened (HANDOFF + top-level
README within the first 15 tool_use events), and whether index reads (e.g.
sop/standard/README.md) were FOLLOWED by a read of a doc under that index —
the on-demand-traversal contract made observable.

Transcript format (verified against real 2.1.x transcripts):
  - one JSON object per line; junk/malformed lines are skipped silently;
  - relevant records carry .timestamp (ISO) and .message.content as a LIST;
    tool calls are content items with type=="tool_use", .name, .input (dict);
  - records with truthy .isSidechain are subagent echoes — skipped;
  - Read input: file_path. Grep/Glob input: pattern/path. Bash input: command.
    Task/Agent input: prompt (subagent spawn — counted, not traversed into).

Bash-mediated file touches are extracted HEURISTICALLY from Bash commands:
paths with a doc/code extension appearing after a known reader command
(cat/head/tail/sed/grep/...). `python3 path.py` is execution, not a read, and
is NOT counted. These touches are reported separately and marked approximate.

Cross-account access: orchestrator (user `claude`) and specialist (user `claude-research`)
have 700-mode homes. Listing + reading their transcripts goes through
`sudo -nu <user>`; the glob is expanded INSIDE the sudo'd shell (a bare glob
from this account silently expands to nothing — see CLAUDE.md §3). Recovery's own
transcripts are read directly, no sudo.

Read-only: writes nothing anywhere except stdout/stderr.

CLI:
    python3 tools/doc_traversal_report.py <session.jsonl> [...more]
    python3 tools/doc_traversal_report.py --agent recovery --last 5
    python3 tools/doc_traversal_report.py --agent orchestrator --last 3 --json
    python3 tools/doc_traversal_report.py --agent specialist --repo-root /path/to/repo

Exit codes: 0 = report produced; 2 = argv error; 3 = no sessions loadable.
"""

from __future__ import annotations

import argparse
import glob as _glob
import json
import os
import shlex
import subprocess
import sys
from collections import Counter
from pathlib import Path

try:  # run-as-script (python3 tools/doc_traversal_report.py): tools/ on sys.path[0]
    import agent_role_map
except ImportError:  # run-as-package (import tools.doc_traversal_report)
    from tools import agent_role_map

# ---------------------------------------------------------------------------
# Configuration

REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[1]

# T-2687: projects_dir is derived from the shared agent_role_map helper (single
# source of truth for the control-plane `-pantheon-config` slug), not a hand-copied
# per-seat literal. Fixes the recovery/specialist control-plane seats, whose plain
# `-home-<account>` slug read a stale/other project dir than the live session writes.
AGENTS = {
    "recovery": {
        "user": None,  # own account — read directly, no sudo
        "projects_dir": agent_role_map.live_session_projects_dir("claude-fallback"),
    },
    "orchestrator": {
        "user": "claude",
        "projects_dir": agent_role_map.live_session_projects_dir("claude"),
    },
    "specialist": {
        "user": "claude-research",
        "projects_dir": agent_role_map.live_session_projects_dir("claude-research"),
    },
}

BOOT_WINDOW = 15  # first N tool_use events examined for boot adherence
TOP_FILES = 15
UNTOUCHED_TRUNCATE = 40

# Bash-read heuristic knobs
READER_BASENAMES = {"cat", "head", "tail", "sed", "grep", "egrep", "fgrep", "rg"}
READ_EXTS = (".md", ".py", ".sh", ".json")
SHELL_SEPARATORS = {"|", "||", "&&", ";", "&"}
SPAWN_TOOLS = {"Task", "Agent"}  # subagent spawns (tool named Agent on 2.1.x)
GOV_DIR_SEGMENTS = ("sop", "adr", "rulebook")

# ---------------------------------------------------------------------------
# Transcript parsing


def parse_transcript_lines(lines):
    """Yield tool_use events from raw jsonl lines, in chronological order.

    Event: {"idx": int, "ts": str|None, "tool": str, "input": dict}.
    Malformed lines and sidechain records are skipped silently. Ordering is
    by timestamp when every event has one, else by line position.
    """
    events = []
    for lineno, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("isSidechain"):
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        ts = rec.get("timestamp")
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            name = item.get("name")
            inp = item.get("input")
            if not isinstance(name, str):
                continue
            events.append(
                {
                    "lineno": lineno,
                    "ts": ts if isinstance(ts, str) else None,
                    "tool": name,
                    "input": inp if isinstance(inp, dict) else {},
                }
            )
    if events and all(e["ts"] for e in events):
        events.sort(key=lambda e: (e["ts"], e["lineno"]))
    # (stable — equal timestamps keep line order; no timestamps → line order)
    for i, e in enumerate(events):
        e["idx"] = i
    return events


# ---------------------------------------------------------------------------
# Bash-read extraction (heuristic, reported as approximate)


def extract_bash_read_paths(command, _depth=0):
    """Best-effort extraction of file paths READ by a bash command.

    A path counts when it has a READ_EXTS extension, is not a flag, and
    appears after a reader command (cat/head/tail/sed/grep/...) within the
    same pipeline segment. `python3 foo.py` is execution, not a read.
    One level of `bash -c '...'` nesting is unwrapped.
    """
    if not isinstance(command, str):
        return []
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    paths = []
    reader_active = False
    prev = None
    for tok in tokens:
        if _depth == 0 and (" " in tok or "\t" in tok):
            prev_base = os.path.basename(prev) if prev else ""
            if prev == "-c" or prev_base in ("bash", "sh"):
                paths.extend(extract_bash_read_paths(tok, _depth=1))
                prev = tok
                continue
        if tok in SHELL_SEPARATORS or tok.startswith(">") or tok.startswith("<"):
            reader_active = False
            prev = tok
            continue
        if os.path.basename(tok) in READER_BASENAMES:
            reader_active = True
            prev = tok
            continue
        if (
            reader_active
            and not tok.startswith("-")
            and tok.endswith(READ_EXTS)
            and not any(c in tok for c in "*?[")  # glob patterns aren't file reads
        ):
            paths.append(tok)
        prev = tok
    return paths


# ---------------------------------------------------------------------------
# Path classification


def is_governance_path(path, repo_root):
    """Governance doc: under sop/ adr/ rulebook/, HANDOFF.md, repo README,
    or notes/recent-policy-changes.md — suffix-matched so any checkout
    prefix (/home/claude/pantheon-config vs this one) classifies alike."""
    if not isinstance(path, str) or not path:
        return False
    p = path.rstrip("/")
    if any(("/%s/" % seg) in p or p.startswith(seg + "/") for seg in GOV_DIR_SEGMENTS):
        return True
    if p.endswith("/HANDOFF.md") or p == "HANDOFF.md":
        return True
    if p.endswith("notes/recent-policy-changes.md"):
        return True
    if _is_repo_readme(p, repo_root):
        return True
    return False


def _is_repo_readme(path, repo_root):
    root = Path(repo_root)
    if path == str(root / "README.md"):
        return True
    return path.endswith("/%s/README.md" % root.name) or path.endswith(
        "/pantheon-config/README.md"
    )


def is_index_path(path):
    """Index file: any */README.md whose directory chain includes a
    sop/adr/rulebook segment (covers sop/standard/README.md,
    sop/remediation/README.md, adr/README.md, rulebook READMEs)."""
    if not isinstance(path, str) or os.path.basename(path) != "README.md":
        return False
    parts = path.split("/")[:-1]
    return any(seg in parts for seg in GOV_DIR_SEGMENTS)


def normalize_gov_key(path, repo_root):
    """Canonical repo-relative key for a governance path, so touches from
    different checkout prefixes aggregate together in the rollup."""
    for seg in GOV_DIR_SEGMENTS:
        anchor = "/%s/" % seg
        i = path.find(anchor)
        if i >= 0:
            return path[i + 1 :]
        if path.startswith(seg + "/"):
            return path
    if path.endswith("/HANDOFF.md") or path == "HANDOFF.md":
        return "HANDOFF.md"
    if path.endswith("notes/recent-policy-changes.md"):
        return "notes/recent-policy-changes.md"
    if _is_repo_readme(path, repo_root):
        return "README.md"
    return os.path.basename(path)


def categorize_path(path):
    if path.endswith(".md"):
        return "repo-docs"
    if path.endswith((".py", ".sh", ".json", ".js", ".ts")):
        return "code"
    return "other"


# ---------------------------------------------------------------------------
# Per-session analysis


def extract_touches(events):
    """File touches from events. Touch: {"event_idx", "path", "kind"} with
    kind in tool_read | bash_read | grep_glob. Kept in event order."""
    touches = []
    for e in events:
        tool, inp = e["tool"], e["input"]
        if tool == "Read":
            fp = inp.get("file_path")
            if isinstance(fp, str) and fp:
                touches.append({"event_idx": e["idx"], "path": fp, "kind": "tool_read"})
        elif tool == "Bash":
            for p in extract_bash_read_paths(inp.get("command")):
                touches.append({"event_idx": e["idx"], "path": p, "kind": "bash_read"})
        elif tool in ("Grep", "Glob"):
            p = inp.get("path")
            if isinstance(p, str) and p.endswith(READ_EXTS):
                touches.append({"event_idx": e["idx"], "path": p, "kind": "grep_glob"})
    return touches


def analyze_session(session_id, events, repo_root):
    touches = extract_touches(events)
    reads = [t for t in touches if t["kind"] in ("tool_read", "bash_read")]

    tool_counts = Counter(e["tool"] for e in events)
    task_spawns = sum(n for t, n in tool_counts.items() if t in SPAWN_TOOLS)

    # Boot adherence — first BOOT_WINDOW tool_use events
    boot_touches = [t for t in touches if t["event_idx"] < BOOT_WINDOW]
    boot_handoff = any(
        t["path"].endswith("/HANDOFF.md") or t["path"] == "HANDOFF.md"
        for t in boot_touches
    )
    boot_readme = any(_is_repo_readme(t["path"], repo_root) for t in boot_touches)
    boot_gov = [
        t["path"] for t in boot_touches if is_governance_path(t["path"], repo_root)
    ]

    # Governance touches, ordered (first-seen) + counted
    gov_counts, gov_order = Counter(), []
    for t in touches:
        if is_governance_path(t["path"], repo_root):
            if t["path"] not in gov_counts:
                gov_order.append(t["path"])
            gov_counts[t["path"]] += 1

    # Index-chain: index read → later non-README read under its directory?
    index_chain = []
    seen_idx = set()
    for t in reads:
        p = t["path"]
        if not is_index_path(p) or p in seen_idx:
            continue
        seen_idx.add(p)
        idx_dir = os.path.dirname(p)
        followed_by = None
        for later in reads:
            if later["event_idx"] <= t["event_idx"]:
                continue
            lp = later["path"]
            if (
                lp.startswith(idx_dir + "/")
                and os.path.basename(lp) != "README.md"
            ):
                followed_by = lp
                break
        index_chain.append(
            {"index": p, "followed": followed_by is not None, "followed_by": followed_by}
        )

    bash_counts = Counter(t["path"] for t in touches if t["kind"] == "bash_read")

    freq = Counter(t["path"] for t in touches)
    top_files = [
        {"path": p, "count": n, "category": categorize_path(p)}
        for p, n in freq.most_common(TOP_FILES)
    ]

    return {
        "session_id": session_id,
        "tool_use_total": len(events),
        "tool_use_by_tool": dict(tool_counts),
        "task_spawns": task_spawns,
        "boot": {
            "handoff_read": boot_handoff,
            "repo_readme_read": boot_readme,
            "governance_touched": boot_gov,
        },
        "governance_touches": [
            {"path": p, "count": gov_counts[p]} for p in gov_order
        ],
        "index_chain": index_chain,
        "bash_touches": [{"path": p, "count": n} for p, n in bash_counts.most_common()],
        "top_files": top_files,
        "touched_paths": sorted({t["path"] for t in touches}),
    }


# ---------------------------------------------------------------------------
# Fleet rollup


def inventory_governance_docs(repo_root):
    """All *.md under <repo>/sop|adr|rulebook (recursive) + README.md, as
    repo-relative posix paths."""
    root = Path(repo_root)
    rels = []
    for seg in GOV_DIR_SEGMENTS:
        base = root / seg
        if base.is_dir():
            rels.extend(
                p.relative_to(root).as_posix() for p in sorted(base.rglob("*.md"))
            )
    if (root / "README.md").is_file():
        rels.append("README.md")
    return sorted(set(rels))


def build_rollup(session_reports, repo_root):
    agg = Counter()
    all_gov_paths = set()
    for rep in session_reports:
        for g in rep["governance_touches"]:
            agg[normalize_gov_key(g["path"], repo_root)] += g["count"]
            all_gov_paths.add(g["path"])

    inventory = inventory_governance_docs(repo_root)
    untouched = [
        rel
        for rel in inventory
        if not any(t == rel or t.endswith("/" + rel) for t in all_gov_paths)
    ]

    boot_table = [
        {
            "session_id": rep["session_id"],
            "handoff": rep["boot"]["handoff_read"],
            "readme": rep["boot"]["repo_readme_read"],
            "governance_touches": sum(g["count"] for g in rep["governance_touches"]),
        }
        for rep in session_reports
    ]

    return {
        "governance_touch_counts": [
            {"path": p, "count": n} for p, n in agg.most_common()
        ],
        "inventory_size": len(inventory),
        "never_touched": untouched,
        "boot_adherence": boot_table,
    }


# ---------------------------------------------------------------------------
# Session discovery / loading


def filter_main_chain(paths):
    """Drop agent-*.jsonl (subagent transcripts) from a candidate list."""
    return [p for p in paths if not os.path.basename(p).startswith("agent-")]


def list_local_sessions(projects_dir, last_n):
    paths = _glob.glob(os.path.join(projects_dir, "*.jsonl"))
    paths = filter_main_chain(paths)
    paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return paths[:last_n]


def list_remote_sessions(user, projects_dir, last_n):
    """Cross-account listing — glob MUST expand inside the sudo'd shell."""
    cmd = [
        "sudo",
        "-nu",
        user,
        "bash",
        "-c",
        "ls -t %s/*.jsonl 2>/dev/null" % shlex.quote(projects_dir),
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print("warn: listing %s sessions failed: %s" % (user, exc), file=sys.stderr)
        return []
    paths = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    return filter_main_chain(paths)[:last_n]


def load_session_lines(path, user=None):
    """Return the file's lines, via sudo cat for cross-account paths.
    Returns None (with a stderr warning) on failure."""
    try:
        if user is None:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.readlines()
        out = subprocess.run(
            ["sudo", "-nu", user, "cat", path],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if out.returncode != 0:
            print(
                "warn: cannot read %s as %s: %s"
                % (path, user, out.stderr.strip()[:200]),
                file=sys.stderr,
            )
            return None
        return out.stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired) as exc:
        print("warn: cannot read %s: %s" % (path, exc), file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Rendering


def _yn(b):
    return "yes" if b else "no"


def render_markdown(session_reports, rollup):
    out = []
    out.append("# Doc-traversal report")
    out.append("")
    for rep in session_reports:
        out.append("## Session %s" % rep["session_id"])
        by_tool = ", ".join(
            "%s %d" % (t, n)
            for t, n in sorted(
                rep["tool_use_by_tool"].items(), key=lambda kv: -kv[1]
            )
        )
        out.append(
            "- tool_use events: %d (%s)" % (rep["tool_use_total"], by_tool or "none")
        )
        out.append("- Task spawns: %d" % rep["task_spawns"])
        out.append("")
        out.append("### Boot adherence (first %d tool_use events)" % BOOT_WINDOW)
        out.append("- HANDOFF.md read: %s" % _yn(rep["boot"]["handoff_read"]))
        out.append("- top-level README read: %s" % _yn(rep["boot"]["repo_readme_read"]))
        if rep["boot"]["governance_touched"]:
            out.append("- governance docs touched, in order:")
            for p in rep["boot"]["governance_touched"]:
                out.append("  1. %s" % p)
        else:
            out.append("- governance docs touched: none")
        out.append("")
        out.append("### Governance-doc touches (ordered by first touch)")
        if rep["governance_touches"]:
            for g in rep["governance_touches"]:
                out.append("- %s (x%d)" % (g["path"], g["count"]))
        else:
            out.append("- none")
        out.append("")
        out.append("### Index-chain")
        if rep["index_chain"]:
            for ic in rep["index_chain"]:
                if ic["followed"]:
                    out.append("- %s: followed (-> %s)" % (ic["index"], ic["followed_by"]))
                else:
                    out.append("- %s: NOT followed" % ic["index"])
        else:
            out.append("- no index reads")
        out.append("")
        out.append("### Bash-mediated file touches (approximate heuristic)")
        if rep["bash_touches"]:
            for b in rep["bash_touches"]:
                out.append("- %s (x%d)" % (b["path"], b["count"]))
        else:
            out.append("- none detected")
        out.append("")
        out.append("### Top files by touch frequency (top %d)" % TOP_FILES)
        for tf in rep["top_files"]:
            out.append("- [%s] %s (x%d)" % (tf["category"], tf["path"], tf["count"]))
        if not rep["top_files"]:
            out.append("- none")
        out.append("")

    if rollup is not None:
        out.append("## Fleet rollup (%d sessions)" % len(session_reports))
        out.append("")
        out.append("### Aggregated governance-doc touches")
        for g in rollup["governance_touch_counts"]:
            out.append("- %s (x%d)" % (g["path"], g["count"]))
        if not rollup["governance_touch_counts"]:
            out.append("- none")
        out.append("")
        nt = rollup["never_touched"]
        out.append(
            "### Never-touched governance docs (%d of %d in inventory)"
            % (len(nt), rollup["inventory_size"])
        )
        for rel in nt[:UNTOUCHED_TRUNCATE]:
            out.append("- %s" % rel)
        if len(nt) > UNTOUCHED_TRUNCATE:
            out.append("- ... and %d more" % (len(nt) - UNTOUCHED_TRUNCATE))
        out.append("")
        out.append("### Boot-adherence table")
        out.append("| session | HANDOFF | README | gov touches |")
        out.append("|---|---|---|---|")
        for row in rollup["boot_adherence"]:
            out.append(
                "| %s | %s | %s | %d |"
                % (
                    row["session_id"],
                    _yn(row["handoff"]),
                    _yn(row["readme"]),
                    row["governance_touches"],
                )
            )
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI


def build_arg_parser():
    ap = argparse.ArgumentParser(
        description="Governance-doc traversal report over Claude Code transcripts"
    )
    ap.add_argument("files", nargs="*", help="explicit transcript jsonl files")
    ap.add_argument("--agent", choices=sorted(AGENTS), help="pick sessions by agent")
    ap.add_argument(
        "--last", type=int, default=5, help="newest N main-chain sessions (default 5)"
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument(
        "--repo-root",
        default=str(REPO_ROOT_DEFAULT),
        help="governance-doc inventory root (default: this checkout)",
    )
    return ap


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    repo_root = args.repo_root

    targets = []  # (path, sudo_user_or_None)
    for f in args.files:
        targets.append((f, None))
    if args.agent:
        cfg = AGENTS[args.agent]
        if cfg["user"] is None:
            paths = list_local_sessions(cfg["projects_dir"], args.last)
            targets.extend((p, None) for p in paths)
        else:
            paths = list_remote_sessions(cfg["user"], cfg["projects_dir"], args.last)
            targets.extend((p, cfg["user"]) for p in paths)
    if not targets:
        print("error: no transcript files given or discovered", file=sys.stderr)
        return 3

    session_reports = []
    for path, user in targets:
        lines = load_session_lines(path, user=user)
        if lines is None:
            continue
        session_id = Path(path).stem
        events = parse_transcript_lines(lines)
        session_reports.append(analyze_session(session_id, events, repo_root))
    if not session_reports:
        print("error: no sessions loadable", file=sys.stderr)
        return 3

    rollup = build_rollup(session_reports, repo_root) if len(session_reports) > 1 else None

    if args.json:
        doc = {"sessions": session_reports, "rollup": rollup}
        print(json.dumps(doc, indent=2))
    else:
        print(render_markdown(session_reports, rollup))
    return 0


if __name__ == "__main__":
    sys.exit(main())
