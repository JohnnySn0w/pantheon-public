#!/usr/bin/env python3
"""tools/skill_trigger_count.py: count Skill-tool invocations in Claude Code session jsonls (T-4782).

Read-only. Measures which rendered pantheon-sop-* skills actually fire, so never-firing
skills can be pruned before more are rendered (orchestrator DECISION 2026-10-16 on T-4782: skills
must earn their resident description cost). Two signals per rendered skill:

* calls: explicit ``Skill`` tool_use invocations, deduplicated by tool_use id (transcripts
  can mirror a call, e.g. in api-request-blob records).
* reads: ``Read`` tool calls, or ``Bash`` commands, that name the skill's SOP file
  (``sop/standard/<x>.md`` or ``sop/remediation/<x>.md``), also deduplicated by id. A seat
  often follows an SOP by reading the file instead of invoking the skill (Recovery, 2026-11-03).
  Reads in a SOP-MAINTENANCE session (one that also edits or writes under sop/ or runs
  persona_orientation/render.py) are NOT reads: they are reported in their own
  maint_reads column (authoring, not use; Recovery found every read of six "read-only" skills
  was maintenance, 2026-11-03). Known limits: a session that follows an SOP and later edits
  it moves that read to maint_reads; research sessions that grep SOPs while editing other
  trees stay in reads. Skill calls are always counted (the prune rule's safety net).

A PRUNE candidate is zero on BOTH signals (orchestrator ruling 2026-11-03); read-but-not-invoked
is a description-fix candidate. Skills injected at session start are not counted.
"Never fired" is only as good as the ROOTS passed: pass every seat account's
~/.claude/projects to get a fleet view.

Usage:
  python3 tools/skill_trigger_count.py ROOT [ROOT ...] [--since ISO] [--manifest PATH]

ROOT is a ~/.claude/projects directory (one per seat account; run as an account that can
read it). With the manifest (default tools/persona_orientation/sop-skills.yaml), every
rendered pantheon-sop-* skill is listed, including the ones that never fired.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SKILL_PREFIX = "pantheon-sop-"
DEFAULT_MANIFEST = Path(__file__).resolve().parent / "persona_orientation" / "sop-skills.yaml"


def iter_tool_uses(root: str, since: str):
    """Yield (tool_use dict, session_key) for every tool_use at or after ``since``.

    Records without a timestamp (e.g. api-request-blob mirrors) are skipped; callers
    also dedupe on the tool_use id."""
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, name)
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
                if mtime.strftime("%Y-%m-%dT%H:%M:%S") < since:
                    continue  # untouched since the window opened: cannot hold a call in it
                fh = open(path, encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    if '"tool_use"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if str(rec.get("timestamp") or "") < since:
                        continue
                    for item in (rec.get("message") or {}).get("content") or []:
                        if isinstance(item, dict) and item.get("type") == "tool_use":
                            yield item, str(rec.get("sessionId") or path)


def manifest_entries(path: Path) -> list[dict]:
    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("skills") or [])


def manifest_skills(path: Path) -> list[str]:
    return [SKILL_PREFIX + entry["slug"] for entry in manifest_entries(path)]


def _read_target(item: dict) -> str:
    """The text a Read/Bash tool_use names, for SOP-path matching ('' otherwise)."""
    inp = item.get("input") or {}
    if item.get("name") == "Read":
        return str(inp.get("file_path") or "")
    if item.get("name") == "Bash":
        return str(inp.get("command") or "")
    return ""


_SOP_EDIT_TOOLS = ("Edit", "Write", "NotebookEdit", "MultiEdit")


def is_sop_maintenance(item: dict) -> bool:
    """Does this tool_use maintain the SOP set (edit or write under sop/, or run the skill
    renderer)? Reads in a session that does this are SOP maintenance, not a seat reaching
    for guidance (Recovery, T-4782 2026-11-03), so they are not counted as reads."""
    inp = item.get("input") or {}
    if item.get("name") in _SOP_EDIT_TOOLS:
        path = str(inp.get("file_path") or inp.get("notebook_path") or "")
        return path.startswith("sop/") or "/sop/" in path
    if item.get("name") == "Bash":
        return "persona_orientation/render.py" in str(inp.get("command") or "")
    return False


def tally(roots: list[str], since: str, sop_paths: dict[str, str] | None = None,
          maintenance_out: "collections.Counter | None" = None):
    """Return (calls, call_sessions, reads, read_sessions).

    Reads in a SOP-maintenance session are not in ``reads``; when ``maintenance_out`` is
    given they are counted there per skill instead (labelled, not discarded: orchestrator 2026-11-03).

    ``sop_paths`` maps a skill name to its SOP repo path (e.g. sop/standard/x.md); a Read or
    Bash tool_use naming that path counts as one read of the skill, UNLESS its session also
    maintains SOPs (``is_sop_maintenance``): such reads are excluded (see that function).
    Skill calls are always counted."""
    sop_paths = sop_paths or {}
    calls: collections.Counter = collections.Counter()
    call_sessions: dict[str, set] = collections.defaultdict(set)
    pending: list[tuple[str, str]] = []  # (skill, session) reads, filtered after the walk
    maintenance: set[str] = set()
    seen: set[str] = set()
    for root in roots:
        for item, session in iter_tool_uses(root, since):
            tid = str(item.get("id") or "")
            if tid:
                if tid in seen:
                    continue
                seen.add(tid)
            if item.get("name") == "Skill":
                skill = str((item.get("input") or {}).get("skill") or "")
                calls[skill] += 1
                call_sessions[skill].add(session)
                continue
            if is_sop_maintenance(item):
                maintenance.add(session)
                continue
            target = _read_target(item)
            if not target:
                continue
            for skill, sop in sop_paths.items():
                if sop and sop in target:
                    pending.append((skill, session))
    reads: collections.Counter = collections.Counter()
    read_sessions: dict[str, set] = collections.defaultdict(set)
    for skill, session in pending:
        if session in maintenance:
            if maintenance_out is not None:
                maintenance_out[skill] += 1
            continue
        reads[skill] += 1
        read_sessions[skill].add(session)
    return calls, call_sessions, reads, read_sessions


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("roots", nargs="+")
    ap.add_argument("--since", default="2026-10-16T00:00:00")
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = ap.parse_args(argv)
    entries = manifest_entries(args.manifest) if args.manifest and args.manifest.exists() else []
    rendered = [SKILL_PREFIX + e["slug"] for e in entries]
    sop_paths = {SKILL_PREFIX + e["slug"]: str(e.get("sop") or "") for e in entries}
    desc_bytes = {SKILL_PREFIX + e["slug"]: len(str(e.get("description") or "").encode("utf-8")) for e in entries}
    maint: collections.Counter = collections.Counter()
    calls, call_sessions, reads, read_sessions = tally(args.roots, args.since, sop_paths, maint)
    print(f"since={args.since} roots={len(args.roots)} rendered_sop_skills={len(rendered)}")
    print("calls sess | reads sess | maint_reads | desc_bytes  skill  [class]")
    for skill in rendered:
        c, r = calls.get(skill, 0), reads.get(skill, 0)
        cls = "used" if c else ("read-only: description-fix candidate" if r else "PRUNE candidate (zero on both)")
        print(f"{c:5d} {len(call_sessions.get(skill, ())):4d} | {r:5d} {len(read_sessions.get(skill, ())):4d} | "
              f"{maint.get(skill, 0):11d} | {desc_bytes.get(skill, 0):10d}  {skill}  [{cls}]")
    never = [s for s in rendered if not calls.get(s)]
    prune = [s for s in never if not reads.get(s)]
    print(f"never_fired={len(never)}/{len(rendered)} prune_candidates={len(prune)}/{len(rendered)} "
          f"prune_desc_bytes={sum(desc_bytes.get(s, 0) for s in prune)}")
    others = sorted((s for s in calls if s not in rendered), key=lambda s: -calls[s])
    for skill in others:
        print(f"{calls[skill]:5d} calls {len(call_sessions[skill]):4d} sessions  {skill}  (not a rendered sop skill)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
