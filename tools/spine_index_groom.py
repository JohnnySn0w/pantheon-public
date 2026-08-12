#!/usr/bin/env python3
"""Pantheon spine-index groomer — idempotent sentinel-region updater for all spine READMEs.

Generalizes the single-purpose ledger_index_groom.py into an all-spine-indexes
pass covering every directory README whose contents table can be mechanically
regenerated from disk state. Every registered dir is scanned every fire.

NO MTIME CACHE — removed under T-2184, and the reason is worth keeping. This
tool runs exclusively inside `with_worktree` (`run_all`, unconditional, no
bypass), and `git worktree add` stamps every checked-out file at checkout time.
So the mtime a fire caches is ~its own start time, and the next fire's freshly
stamped files are always NEWER — `current <= cached` can never hold. Measured:
a worktree's adr/*.md came out at checkout-time while the checkout's real
mtimes were 21 days old. The cache's hit rate was structurally ZERO on every
path the tool actually runs, however it was keyed, while it grew ~15
permanently unreachable keys per fire into a state file nothing pruned.

It was NOT replaced with a content hash. That would have turned caching ON for
the first time here, and a groomer whose failure mode is "the index silently
stops reflecting reality" has already paid for that twice (T-2103: a handler
fix was inert because no watched dir changed; T-2182: --dry-run went blind by
reading a cache written under different mtime semantics). What always-scanning
costs, measured: 1.25s wall for a full 15-region pass INCLUDING interpreter
startup, on a daily timer. That is not a trade worth a silent-staleness surface.

Worth recording so the diagnosis is not re-litigated: at removal time the live
cache held 15 correctly-keyed relative entries ("adr", "bugs", "incidents"),
so T-2184's earlier keying fix had landed and worked. The cache still could not
hit. Keying was never the blocker — the mtime SEMANTICS are, and no key repairs
that.

Per fire (single-shot, no subcommands):

1. Pull repo (fail-open on transient errors per T-0118/T-0120 pattern).
2. For each registered spine dir:
   a. Call per-dir handler → list[str] of Markdown table rows.
   b. Write rows between AUTOGEN-START/END sentinel comment regions in README.md.
   c. If region changed: commit + push via _git_utils discipline (HERMES-guarded).

CLI:
    python3 tools/spine_index_groom.py                # normal fire
    python3 tools/spine_index_groom.py --dry-run      # scan + print diff, no write
    python3 tools/spine_index_groom.py --no-push      # write + commit, skip push

Designed for daily cron at 12:30 UTC on `claude-fallback` (Recovery), replacing
`pantheon-ledger-index-groom.timer` → `pantheon-spine-index-groom.timer`.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._git_utils import (  # noqa: E402
    commit_scoped,
    pull_repo_or_warn,
    with_worktree,
)
from tools.ledger_shard import (  # noqa: E402
    entry_ts,
    iter_ledger_entry_files,
    segment_for_ts,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

SENTINEL_START = "<!-- AUTOGEN-START spine-index -->"
SENTINEL_END = "<!-- AUTOGEN-END spine-index -->"


# ---------------------------------------------------------------------------
# Sentinel-region parser (line-by-line state machine — D4 justification).
# ---------------------------------------------------------------------------


def read_sentinel_region(text: str) -> tuple[str, str, str] | None:
    """Split text into (before, inside, after) around sentinel comments.

    Returns None if sentinels are not found (dir has no sentinel region yet).
    State machine: BEFORE → INSIDE (on start sentinel) → AFTER (on end sentinel).
    """
    before: list[str] = []
    inside: list[str] = []
    after: list[str] = []
    state = "BEFORE"

    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n").rstrip("\r")
        if state == "BEFORE":
            if stripped == SENTINEL_START:
                state = "INSIDE"
                before.append(line)
            else:
                before.append(line)
        elif state == "INSIDE":
            if stripped == SENTINEL_END:
                state = "AFTER"
                after.append(line)
            else:
                inside.append(line)
        else:
            after.append(line)

    if state == "BEFORE":
        return None
    return "".join(before), "".join(inside), "".join(after)


def write_sentinel_region(readme_path: Path, new_rows: list[str]) -> bool:
    """Write new_rows between sentinels in readme_path. Returns True if changed.

    Never touches content outside the sentinel block. Only the lines between
    AUTOGEN-START and AUTOGEN-END are replaced. Per P1: invariant is structurally
    enforced by the state machine, not by a regex.
    """
    text = readme_path.read_text()
    parts = read_sentinel_region(text)
    if parts is None:
        print(
            f"[spine_index_groom] {readme_path}: no sentinel region found; skipping",
            file=sys.stderr,
        )
        return False

    before, old_inside, after = parts
    new_inside = ("\n".join(new_rows) + "\n") if new_rows else ""
    if new_inside == old_inside:
        return False

    new_text = before + new_inside + after
    readme_path.write_text(new_text)
    return True


# ---------------------------------------------------------------------------
# Markdown row emission — the single choke point (T-1929).
# ---------------------------------------------------------------------------
#
# A literal `|` inside a generated value splits the markdown table cell, so the
# row renders with more columns than its header and the value is truncated at
# the first pipe. Found during T-1912: 7 frontmatter values across 5 bugs/ files
# carried pipes, breaking 3 rows in bugs/README.md and 2 in
# knowledge/episodic/README.md.
#
# Escaping CANNOT happen downstream in write_sentinel_region(): that receives
# already-formatted row strings and cannot distinguish a structural delimiter
# pipe from a value pipe. It therefore happens at cell level, before the
# delimiters are added — and every handler routes through _md_row so a new
# handler inherits escaping by construction rather than by remembering (P1).
#
# _md_cell / _md_row were re-homed to the leaf module tools/_md_table.py under
# T-1936 so the four raw-f-string table emitters (ticket.py, weekly_ticket_summary,
# watch_runner, s3star_audit_runner) share this ONE implementation rather than
# each growing a copy that could drift. Imported below (not redefined) — the
# single-implementation invariant is `grep -rn "def _md_cell" tools/` == 1.
from tools._md_table import _UNESCAPED_PIPE, _md_cell, _md_row  # noqa: F401,E402


# ---------------------------------------------------------------------------
# Per-dir handler functions.
# ---------------------------------------------------------------------------


def research_handler(d: Path) -> list[str]:
    """List dated <date>-<topic>/ subdirs with first-paragraph description."""
    rows = []
    for subdir in sorted(d.iterdir()):
        if not subdir.is_dir():
            continue
        # Try README.md, then notes.md, then ANY other .md in the subdir (T-2103).
        # Without the third leg, a subdir holding only e.g. report.md or
        # findings.md indexed as a bare "—": present in the index, correctly
        # listed by FILENAME, and carrying ZERO searchable description. That
        # defeats the prior-art grep the ORG-012 discipline depends on — it is
        # how the T-1968 step-1 inventory was nearly rebuilt from scratch, and
        # it affected 27 of 120 episodic subdirs when this landed. Falling back
        # to the first other .md fixes every such case with no authoring effort
        # and no retroactive make-work.
        desc = _first_paragraph_after_title(subdir / "README.md") or \
               _first_paragraph_after_title(subdir / "notes.md") or \
               _first_paragraph_of_any_md(subdir) or "—"
        rows.append(_md_row(f"[`{subdir.name}/`]({subdir.name}/)", desc))
    return rows


def _first_paragraph_of_any_md(subdir: Path) -> str:
    """First usable paragraph from any .md in subdir, README/notes already tried.

    T-2103 third fallback leg. Deterministic: sorted() so the row is stable
    across runs (the groomer is idempotent and an unstable pick would produce
    spurious diffs). Skips README.md / notes.md because the caller has already
    tried them, and returns "" when the subdir holds no other .md at all — a
    dir of only .txt/.json still falls through to "—", which is correct: there
    is no prose to lift, and inventing one from filenames would be a different
    (weaker) option than this one.
    """
    for md in sorted(subdir.glob("*.md")):
        if md.name in ("README.md", "notes.md"):
            continue
        desc = _first_paragraph_after_title(md)
        if desc:
            return desc
    return ""


def _first_paragraph_after_title(path: Path) -> str:
    """Extract first non-blank paragraph after the title from a Markdown file.

    T-2182: falls back to the YAML frontmatter `title:` when no paragraph is
    found. A note written frontmatter-first with NO `#` heading — a normal way to
    write a research note — never sets past_title, so this returned "" and the
    row indexed as a bare em-dash despite the file carrying a perfectly good
    one-line description. Reading frontmatter here is not a new capability: the
    bugs handler already does it (`fm.get("symptom", ...)`).
    """
    if not path.exists():
        return ""
    text = path.read_text()
    lines = text.splitlines()
    past_title = False
    para_lines: list[str] = []
    for line in lines:
        if not past_title:
            if line.startswith("#"):
                past_title = True
            continue
        if not line.strip():
            if para_lines:
                break
            continue
        para_lines.append(line.strip())
    if para_lines:
        return " ".join(para_lines)
    # No `#` heading anywhere, so the loop above collected nothing. Take the
    # first paragraph from the top instead, skipping any frontmatter block.
    # T-2183: six episodic READMEs are exactly this shape — prose on line 1, no
    # heading, no frontmatter — and render only because a sibling notes.md
    # rescues them. Rename the sibling and they silently become em-dashes.
    body = _first_paragraph_from_top(lines)
    if body:
        return body
    return _frontmatter_title(lines)


def _first_paragraph_from_top(lines: list[str]) -> str:
    """First non-blank paragraph, skipping a leading YAML frontmatter block.

    Only used when no `#` heading exists — with a heading present the caller's
    after-the-title scan is the correct reading, and taking the top paragraph
    would grab pre-title matter instead.
    """
    start = 0
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                start = i + 1
                break
        else:
            return ""  # unterminated frontmatter — treat the file as opaque
    para: list[str] = []
    for line in lines[start:]:
        if not line.strip():
            if para:
                break
            continue
        para.append(line.strip())
    return " ".join(para)


def _frontmatter_title(lines: list[str]) -> str:
    """`title:` value from a leading YAML frontmatter block, else "".

    Deliberately a hand parse rather than a YAML dependency: this reads one
    scalar from a block the groomer already treats as opaque, and the module has
    no yaml import today. Only a frontmatter block that STARTS the file counts —
    a stray `---` mid-document is a horizontal rule, not frontmatter.
    """
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("title:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return ""


def tools_handler(d: Path) -> list[str]:
    """List *.py scripts with first-line purpose from module docstring."""
    rows = []
    for script in sorted(d.glob("*.py")):
        if script.name.startswith("test_") or script.name.startswith("_"):
            continue
        purpose = _first_docstring_line(script)
        rows.append(_md_row(f"[`{script.name}`]({script.name})", purpose))
    return rows


def _first_docstring_line(path: Path) -> str:
    """Extract first line of module-level docstring from a Python file."""
    try:
        source = path.read_text()
        tree = ast.parse(source)
        docstring = ast.get_docstring(tree)
        if docstring:
            return docstring.splitlines()[0].strip()
    except (SyntaxError, ValueError):
        pass
    return "—"


def notes_handler(d: Path) -> list[str]:
    """List *.md files with first paragraph after title."""
    rows = []
    for md in sorted(d.glob("*.md")):
        if md.name == "README.md":
            continue
        purpose = _first_paragraph_after_title(md) or "—"
        rows.append(_md_row(f"[`{md.name}`]({md.name})", purpose))
    return rows


def bugs_handler(d: Path) -> list[str]:
    """List per-bug *.md files with symptom + domain from YAML frontmatter."""
    rows = []
    for md in sorted(d.glob("*.md")):
        if md.name == "README.md":
            continue
        fm = _parse_frontmatter(md)
        symptom = fm.get("symptom", "—")
        domain = fm.get("domain", "—")
        rows.append(_md_row(f"`{md.name}`", symptom, domain))
    return rows


def _parse_frontmatter(path: Path) -> dict[str, str]:
    """Parse simple YAML-style frontmatter (key: value lines between --- delimiters)."""
    text = path.read_text()
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    result: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip().strip('"').strip("'")
    return result


def incidents_handler(d: Path) -> list[str]:
    """List dated <date>-<slug>/ subdirs with general-failure-shape line."""
    rows = []
    for subdir in sorted(d.iterdir()):
        if not subdir.is_dir():
            continue
        shape = _read_general_failure_shape(subdir / "README.md") or "—"
        rows.append(_md_row(f"[{subdir.name}/]({subdir.name}/)", shape))
    return rows


def _read_general_failure_shape(path: Path) -> str:
    """Read the first non-blank line after '## General failure shape' heading."""
    if not path.exists():
        return ""
    lines = path.read_text().splitlines()
    in_section = False
    for line in lines:
        if re.match(r"^##\s+General failure shape", line, re.IGNORECASE):
            in_section = True
            continue
        if in_section:
            stripped = line.strip()
            if stripped.startswith("##"):
                break
            if stripped:
                return stripped
    return ""


def watch_reports_handler(d: Path) -> list[str]:
    """List <date>/ subfolders (already auto-maintained by watch_runner)."""
    rows = []
    for subdir in sorted(d.iterdir()):
        if subdir.is_dir():
            rows.append(_md_row(f"[{subdir.name}/]({subdir.name}/)"))
    return rows


def pinned_rules_handler(d: Path) -> list[str]:
    """List per-agent pinned-rule *.md files with first-paragraph description.

    Mirrors notes_handler: flat .md alphabetical list with first-paragraph
    description extracted via _first_paragraph_after_title. Excludes README.md.
    Current content: recovery.md; future agents will add entries here.
    """
    rows = []
    for md in sorted(d.glob("*.md")):
        if md.name == "README.md":
            continue
        purpose = _first_paragraph_after_title(md) or "—"
        rows.append(_md_row(f"[`{md.name}`]({md.name})", purpose))
    return rows


def skills_handler(d: Path) -> list[str]:
    """List subdir-bundled skills by reading each SKILL.md for description.

    Mirrors incidents_handler: walks immediate subdirs, reads SKILL.md YAML
    frontmatter for the description field. Subdirs without SKILL.md are skipped.
    """
    rows = []
    for subdir in sorted(d.iterdir()):
        if not subdir.is_dir():
            continue
        skill_md = subdir / "SKILL.md"
        if not skill_md.exists():
            continue
        fm = _parse_frontmatter(skill_md)
        desc = fm.get("description", "—")
        rows.append(_md_row(f"[`{subdir.name}/`]({subdir.name}/)", desc))
    return rows


def adr_handler(d: Path) -> list[str]:
    """List *.md ADR files with title + status + date from frontmatter."""
    rows = []
    for md in sorted(d.glob("*.md")):
        if md.name == "README.md":
            continue
        fm = _parse_frontmatter(md)
        title = fm.get("title", "—")
        status = fm.get("status", "—")
        date = fm.get("date", "—")
        stem = md.name
        rows.append(_md_row(f"[{stem}]({stem})", title, status, date))
    return rows


# ---------------------------------------------------------------------------
# Ledger handler — preserves exact ledger_index_groom.py logic.
# ---------------------------------------------------------------------------

# Copied from ledger_index_groom.py to preserve behavior (regression-guarded).
_INDEX_ROW_ID_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|", re.MULTILINE)
_YEAR_HEADING_RE = re.compile(r"^## (\d{4})\s*$", re.MULTILINE)
LEDGER_DIR = REPO_ROOT / "ledger"
INDEX_PATH = LEDGER_DIR / "INDEX.md"


def _read_jsonl_entries(jsonl_path: Path) -> list[dict]:
    if not jsonl_path.exists():
        return []
    entries: list[dict] = []
    for lineno, raw in enumerate(jsonl_path.read_text().splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            entries.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"spine_index_groom: malformed JSON at {jsonl_path}:{lineno}: {exc}"
            )
    return entries


def _parse_indexed_ids(index_text: str) -> set[str]:
    return set(_INDEX_ROW_ID_RE.findall(index_text))


def _format_justifies(entry: dict) -> str:
    raw = entry.get("justifies_via") or []
    return ", ".join(raw) if raw else "—"


def _build_index_row(entry: dict) -> str:
    eid = entry["id"]
    action = entry.get("action", "?")
    agent = entry.get("agent", "?")
    justifies = _format_justifies(entry)
    return _md_row(f"`{eid}`", action, agent, justifies)


# ---------------------------------------------------------------------------
# Per-segment SPINE.md (T-0148 weekly-subfolder layout).
# ---------------------------------------------------------------------------

# SPINE.md carries a ts column (the INDEX top-level table omits it — INDEX is
# the cross-segment global spine; the per-segment SPINE is the local detail).
SPINE_TABLE_HEADER = (
    "| ID | TS | Action | Agent | Justifies-via |\n"
    "|---|---|---|---|---|\n"
)


def _build_spine_row(entry: dict) -> str:
    eid = entry["id"]
    ts = entry_ts(entry) or "?"
    action = entry.get("action", "?")
    agent = entry.get("agent", "?")
    justifies = _format_justifies(entry)
    return _md_row(f"`{eid}`", ts, action, agent, justifies)


def _build_segment_spine(segment: str, entries: list[dict]) -> str:
    """Render the full SPINE.md text for one segment from its entries.

    Deterministic regeneration from disk state (entries in source order),
    mirroring the row style of the top-level INDEX. SPINE.md is the dedicated
    per-segment index; it is rebuilt wholesale (not incrementally appended)
    because it is cheap and the shard is small (~85 entries/week).
    """
    rows = [_build_spine_row(e) for e in entries]
    body = "\n".join(rows) + ("\n" if rows else "")
    return (
        f"# Ledger segment {segment}\n\n"
        f"Per-segment spine for `{segment}/{segment}.jsonl` "
        f"({len(entries)} entr" + ("y" if len(entries) == 1 else "ies") + ").\n"
        "Auto-maintained by `tools/spine_index_groom.py`. "
        "The top-level `ledger/INDEX.md` is the cross-segment global spine.\n\n"
        + SPINE_TABLE_HEADER
        + body
    )


def _segment_spine_path(ledger_dir: Path, segment: str) -> Path:
    return ledger_dir / segment / "SPINE.md"


def _append_rows_to_year(index_text: str, year: str, new_rows: list[str]) -> str:
    if not new_rows:
        return index_text
    year_match = re.search(rf"^## {re.escape(year)}\s*$", index_text, re.MULTILINE)
    if not year_match:
        new_section = (
            f"\n## {year}\n\n"
            "| ID | Action | Agent | Justifies-via |\n"
            "|---|---|---|---|\n" + "\n".join(new_rows) + "\n"
        )
        return index_text.rstrip() + "\n" + new_section
    after_heading_idx = year_match.end()
    next_section_match = re.search(r"^## ", index_text[after_heading_idx:], re.MULTILINE)
    if next_section_match:
        section_end = after_heading_idx + next_section_match.start()
    else:
        section_end = len(index_text)
    section = index_text[after_heading_idx:section_end]
    section_lines = section.splitlines(keepends=True)
    last_row_line = -1
    for i, line in enumerate(section_lines):
        if line.startswith("|"):
            last_row_line = i
    if last_row_line < 0:
        new_block = "\n".join(new_rows) + "\n"
        return index_text[:after_heading_idx] + "\n" + new_block + index_text[after_heading_idx:]
    insert_offset = after_heading_idx + sum(len(s) for s in section_lines[: last_row_line + 1])
    insertion = "\n".join(new_rows) + "\n"
    return index_text[:insert_offset] + insertion + index_text[insert_offset:]


def _build_ledger_commit_message(
    year: str, new_count: int, sample_ids: list[str], extra_rel_paths: list[str] | None = None
) -> str:
    sample = ", ".join(sample_ids[:3])
    if len(sample_ids) > 3:
        sample += f", … (+{len(sample_ids) - 3} more)"
    extra_rel_paths = extra_rel_paths or []
    # Scope line names every file the commit touches (T-0148: INDEX.md plus
    # any regenerated per-segment SPINE.md).
    scope = ", ".join(["ledger/INDEX.md"] + sorted(extra_rel_paths))
    return (
        f"ledger(index): groom {year} — append {new_count} entr"
        + ("y" if new_count == 1 else "ies")
        + "\n\n"
        + "Auto-committed by spine_index_groom.py (Recovery, daily 12:30 UTC).\n"
        + f"Appended ids: {sample}\n"
        + f"Scope: {scope}.\n"
    )


def _ledger_commit_and_push(
    new_count: int,
    year: str,
    sample_ids: list[str],
    *,
    push: bool,
    index_path: Path,
    cwd: Path | None = None,
    extra_rel_paths: list[str] | None = None,
) -> None:
    # cwd defaults to REPO_ROOT (legacy/direct-call path); worktree callers
    # pass the worktree path so git operations target the isolated working tree.
    _cwd = cwd if cwd is not None else REPO_ROOT
    # T-0148: extra_rel_paths carries any per-segment SPINE.md files that
    # changed this fire (repo-relative). They are scoped alongside INDEX.md —
    # the commit still touches ONLY ledger/INDEX.md + ledger/<seg>/SPINE.md.
    extra_rel_paths = sorted(extra_rel_paths or [])
    rel_index = "ledger/INDEX.md"
    expected = [rel_index] + extra_rel_paths
    del index_path  # commit_scoped derives the file from cwd/rel_index (== index_path)

    msg = _build_ledger_commit_message(year, new_count, sample_ids, extra_rel_paths)
    # Delegates to the shared commit_scoped primitive (T-1088 convergence). sync=False:
    # preserve this caller's prior best-effort posture — the legacy ff-only pull here
    # was swallowed (rc unchecked), and push_with_retry already rebases on non-ff, so a
    # pre-pull would only turn a best-effort step into a hard abort. The leak-guard is
    # behavior-equivalent to the prior exact-match check (all `expected` files were just
    # written, so all are staged).
    ok, detail = commit_scoped(expected, msg, cwd=_cwd, push=push, sync=False)
    if not ok:
        raise SystemExit(detail)


def _sync_segment_spines(
    ledger_dir: Path, entries_by_segment: dict[str, list[dict]], cwd: Path | None
) -> list[str]:
    """Regenerate SPINE.md for each existing segment subfolder that changed.

    Pre-migration there are no segment subfolders → returns []. Post-migration
    each `ledger/<seg>/` gets its SPINE.md regenerated from its entries; only
    subfolders that actually exist on disk are touched (the migration script
    creates them; this groomer never creates segment dirs itself).

    Returns the list of repo-relative SPINE.md paths whose content changed.
    """
    _cwd = cwd if cwd is not None else REPO_ROOT
    changed: list[str] = []
    for segment, seg_entries in sorted(entries_by_segment.items()):
        seg_dir = ledger_dir / segment
        if not seg_dir.is_dir():
            continue
        spine_path = _segment_spine_path(ledger_dir, segment)
        new_text = _build_segment_spine(segment, seg_entries)
        old_text = spine_path.read_text() if spine_path.exists() else None
        if old_text == new_text:
            continue
        spine_path.write_text(new_text)
        try:
            changed.append(str(spine_path.relative_to(_cwd)))
        except ValueError:
            changed.append(str(spine_path.relative_to(ledger_dir.parent)))
    return changed


def ledger_handler(ledger_dir: Path, *, push: bool = False, cwd: Path | None = None) -> bool:
    """Maintain ledger/INDEX.md (and post-migration per-segment SPINE.md files).

    Reads the ledger entry-file union (ledger_shard.iter_ledger_entry_files,
    T-0148): pre-migration just the flat 2026.jsonl (behavior unchanged —
    appends missing rows to INDEX.md's `## <year>` sections); post-migration
    the per-week subfolder shards, in which case each segment's SPINE.md is
    also regenerated. Both INDEX.md and any changed SPINE.md land in one
    scope-narrowed commit.

    `cwd` is passed through to the commit helper; worktree callers set this to
    the worktree path so git operations run in the isolated tree.

    Returns True if a commit was made (INDEX.md and/or a segment SPINE.md
    changed), False otherwise — including the early no-op returns below. The
    caller (groom_dirs, T-1881) uses this to print a status line distinguishing
    a genuine no-op from a real commit; previously both were silent.
    """
    index_path = ledger_dir / "INDEX.md"
    if not index_path.exists():
        return False

    entry_files = iter_ledger_entry_files(ledger_dir)
    entries: list[dict] = []
    for f in entry_files:
        entries.extend(_read_jsonl_entries(f))
    if not entries:
        return False

    # Group entries by their ISO-week segment (for SPINE.md) and by their
    # entry-ts calendar year (for the INDEX `## <year>` sections — the INDEX
    # remains year-grouped per its existing shape).
    entries_by_segment: dict[str, list[dict]] = {}
    for e in entries:
        ts = entry_ts(e)
        if not isinstance(ts, str):
            continue
        try:
            seg = segment_for_ts(ts)
        except ValueError:
            continue
        entries_by_segment.setdefault(seg, []).append(e)

    index_text = index_path.read_text()
    indexed_ids = _parse_indexed_ids(index_text)
    missing = [e for e in entries if e["id"] not in indexed_ids]

    # Regenerate per-segment SPINE.md files (no-op pre-migration / when
    # subfolders absent). Uses ALL entries for each segment, not just the
    # INDEX-missing ones — SPINE.md is a full deterministic regeneration.
    spine_changed = _sync_segment_spines(ledger_dir, entries_by_segment, cwd)

    new_text = index_text
    if missing:
        missing_by_year: dict[str, list[dict]] = {}
        for e in missing:
            ts = entry_ts(e) or ""
            yr = ts[:4] if len(ts) >= 4 and ts[:4].isdigit() else datetime.now(timezone.utc).strftime("%Y")
            missing_by_year.setdefault(yr, []).append(e)
        for yr in sorted(missing_by_year):
            rows = [_build_index_row(e) for e in missing_by_year[yr]]
            new_text = _append_rows_to_year(new_text, yr, rows)

    index_changed = new_text != index_text
    if index_changed:
        index_path.write_text(new_text)

    if not index_changed and not spine_changed:
        return False

    # The commit message year header uses the newest entry's year for context.
    msg_year = max(
        (yr for e in entries if (yr := (entry_ts(e) or "")[:4]).isdigit()),
        default=datetime.now(timezone.utc).strftime("%Y"),
    )
    _ledger_commit_and_push(
        new_count=len(missing),
        year=msg_year,
        sample_ids=[e["id"] for e in missing],
        push=push,
        index_path=index_path,
        cwd=cwd,
        extra_rel_paths=spine_changed,
    )
    return True


# ---------------------------------------------------------------------------
# Commit helper for non-ledger dirs.
# ---------------------------------------------------------------------------


def _commit_dir_change(
    readme_path: Path, dir_label: str, *, push: bool, cwd: Path | None = None
) -> None:
    """Commit + push a changed README sentinel region. HERMES-guarded.

    `cwd` defaults to REPO_ROOT (legacy path); worktree callers pass the
    worktree path so git operations run in the isolated working tree.
    """
    _cwd = cwd if cwd is not None else REPO_ROOT
    rel = readme_path.relative_to(_cwd)
    msg = f"spine(index): groom {dir_label} — regenerate sentinel region\n\nAuto-committed by spine_index_groom.py (Recovery, daily 12:30 UTC).\nScope: {rel} only.\n"
    # Delegates to commit_scoped (T-1088 convergence). sync=False: this caller never
    # pulled (worktree starts at origin/main HEAD; legacy direct-call never pulled either).
    ok, detail = commit_scoped([str(rel)], msg, cwd=_cwd, push=push, sync=False)
    if not ok:
        raise SystemExit(detail)


# ---------------------------------------------------------------------------
# Core groom loop.
# ---------------------------------------------------------------------------


def groom_dirs(
    handler_map: dict[Path, object],
    *,
    dry_run: bool = False,
    push: bool = False,
    cwd: Path | None = None,
) -> None:
    """Run groom pass over all dirs in handler_map.

    `cwd` is passed through to commit helpers; worktree callers set this to
    the worktree path so git operations run in the isolated working tree.
    """
    for d, handler in sorted(handler_map.items(), key=lambda kv: str(kv[0])):
        # Special-case: ledger handler manages its own README.
        if handler is ledger_handler:
            if not dry_run:
                try:
                    committed = ledger_handler(d, push=push, cwd=cwd)
                # Catches SystemExit too (T-1881): _ledger_commit_and_push raises
                # SystemExit on a commit_scoped failure, and SystemExit is a
                # BaseException, not an Exception — an `except Exception`-only
                # catch here would let it escape uncaught, silently aborting
                # every dir that sorts after "ledger" in this loop for the rest
                # of the fire. Never catches KeyboardInterrupt/GeneratorExit.
                except (Exception, SystemExit) as exc:
                    print(f"[spine_index_groom] {d.name}: error in ledger handler: {exc}", file=sys.stderr)
                else:
                    # T-1881: the success path previously printed nothing at
                    # all — a real no-op and a silent break were indistinguishable
                    # from the log. Always report which one happened.
                    if committed:
                        print(f"[spine_index_groom] {d.name}: ledger handler committed changes")
                    else:
                        print(f"[spine_index_groom] {d.name}: ledger handler ran — noop (no changes)")
            else:
                print(f"[spine_index_groom] {d.name}: dry-run ledger handler")
            continue

        # Generic sentinel-region handlers.
        readme = d / "README.md"
        if not readme.exists():
            print(f"[spine_index_groom] {d.name}: no README.md; skipping", file=sys.stderr)
            continue

        try:
            rows = handler(d)
        except Exception as exc:
            print(f"[spine_index_groom] {d.name}: handler error: {exc}", file=sys.stderr)
            continue

        if dry_run:
            print(f"[spine_index_groom] {d.name}: dry-run — would write {len(rows)} row(s)")
            for row in rows:
                print(f"  {row}")
            continue

        changed = write_sentinel_region(readme, rows)
        if not changed:
            print(f"[spine_index_groom] {d.name}: noop — region unchanged")
            continue

        print(f"[spine_index_groom] {d.name}: wrote {len(rows)} row(s)")
        try:
            _commit_dir_change(readme, d.name, push=push, cwd=cwd)
        except Exception as exc:
            print(f"[spine_index_groom] {d.name}: commit error: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Public aliases for legacy test compatibility (test_spine_index_groom_ledger_handler_legacy).
# These expose the private ledger helpers under the names the original
# test_ledger_index_groom.py used against ledger_index_groom.py.
# ---------------------------------------------------------------------------

parse_indexed_ids = _parse_indexed_ids
build_index_row = _build_index_row
append_rows_to_year = _append_rows_to_year
commit_and_push = _ledger_commit_and_push
read_jsonl_entries = _read_jsonl_entries


def run(year: str, *, dry_run: bool, push: bool) -> int:
    """Legacy single-fire entry for ledger/INDEX.md grooming only.

    Preserves the exact interface of the old ledger_index_groom.run() for
    backward-compatibility with the legacy regression tests.
    """
    jsonl_path = LEDGER_DIR / f"{year}.jsonl"
    entries = _read_jsonl_entries(jsonl_path)
    if not entries:
        print(f"[spine_index_groom] {year}: noop — no entries in {jsonl_path.name}")
        return 0

    if not INDEX_PATH.exists():
        print(f"[spine_index_groom] {year}: noop — INDEX.md not found at {INDEX_PATH}")
        return 0

    index_text = INDEX_PATH.read_text()
    indexed_ids = _parse_indexed_ids(index_text)

    missing = [e for e in entries if e["id"] not in indexed_ids]
    if not missing:
        print(f"[spine_index_groom] {year}: noop — INDEX.md current ({len(entries)} entries indexed)")
        return 0

    new_rows = [_build_index_row(e) for e in missing]
    new_text = _append_rows_to_year(index_text, year, new_rows)

    if dry_run:
        print(f"[spine_index_groom] {year}: dry-run — would append {len(missing)} row(s):")
        for row in new_rows:
            print(f"  {row}")
        return 0

    INDEX_PATH.write_text(new_text)
    print(f"[spine_index_groom] {year}: appended {len(missing)} row(s)")
    commit_and_push(
        new_count=len(missing),
        year=year,
        sample_ids=[e["id"] for e in missing],
        push=push,
        index_path=INDEX_PATH,
    )
    return 0


# ---------------------------------------------------------------------------
# Handler registry (per-dir config dict — D2: function references).
# ---------------------------------------------------------------------------

# tickets/ is intentionally excluded from this map.
# tickets/README.md is auto-maintained by tools/ticket.py's state-transition
# logic (claim/resolve/block/unblock each update the Current state table).
# Adding tickets/ here would create a second writer and risk clobber races.

def _build_handler_map(root: Path) -> dict[Path, object]:
    """Build the handler map for a given repo root. Used both for the default
    REPO_ROOT and for per-fire worktree paths (T-0471 isolation).

    tickets/ is intentionally excluded — README is auto-maintained by ticket.py.

    Dirs explicitly excluded from spine grooming (T-0667 audit):
    - knowledge/       parent README indexes children (episodic/, watch/reports/)
                       that have their own spines; parent itself is a curated
                       landing page.
    - ledger/          has its own dedicated ledger_handler (in-map above);
                       not covered by the generic sentinel-region flow.
    - sop/             parent README indexes children (standard/, remediation/)
                       that have their own spines; parent itself is a curated
                       landing page.
    - screenshots/     pure artifact dir (image files only, T-0629 subdir);
                       no docs to index.
    - tests/           pure code/fixtures; no docs.
    """
    return {
        root / "knowledge" / "episodic": research_handler,
        # tools/README.md: hand-curated detailed table in ## Index is preserved;
        # ## Auto-index sentinel section added by T-0434 — groom is now active.
        root / "tools": tools_handler,
        root / "notes": notes_handler,
        root / "bugs": bugs_handler,
        root / "incidents": incidents_handler,
        root / "knowledge" / "watch" / "reports": watch_reports_handler,
        root / "adr": adr_handler,
        root / "ledger": ledger_handler,
        # Five directories added per T-0434 (the operator msg_1523 ratification:
        # "all folders and subfolders should be part of sweep").
        # All use notes_handler: flat .md alphabetical list with first-paragraph
        # description — the canonical shape for these dirs.
        root / "lessons": notes_handler,
        root / "rulebook": notes_handler,          # indexes principles.md + directives.md
        root / "rulebook" / "policies": notes_handler,
        root / "sop" / "standard": notes_handler,
        root / "sop" / "remediation": notes_handler,
        # Two directories added per T-0667 spine-coverage gap audit.
        root / "pinned-rules": pinned_rules_handler,
        root / "skills": skills_handler,
    }


# Default handler map (used by tests that pass handler_map= explicitly or by
# run_all when called with a non-worktree root).
_HANDLER_MAP: dict[Path, object] = _build_handler_map(REPO_ROOT)


# ---------------------------------------------------------------------------
# Main entry.
# ---------------------------------------------------------------------------


def run_all(
    *,
    handler_map: dict[Path, object] | None = None,
    dry_run: bool,
    push: bool,
) -> int:
    """Single-fire entry for all-spine groom. Returns process exit code.

    T-0471: wraps the groom loop in with_worktree so each cron fire runs in
    an isolated /tmp/ git worktree off origin/main. Race where a dirty shared
    working tree caused scope-leak assertion failures (2026-05-19 12:30Z) is
    structurally eliminated — commits originate from a clean isolated tree.
    pull_repo_or_warn removed: with_worktree fetches origin/main before
    worktree creation; operating on the shared working tree is superseded
    (justifies-via P1).
    """
    if dry_run:
        # Dry-run: no git writes; skip worktree overhead.
        groom_dirs(handler_map or _HANDLER_MAP, dry_run=True, push=False)
        return 0

    def _do_groom(wt_path: Path) -> int:
        wt_handler_map = handler_map or _build_handler_map(wt_path)
        groom_dirs(wt_handler_map, dry_run=False, push=push, cwd=wt_path)
        return 0

    return with_worktree(_do_groom)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--commit-prod", action="store_true",
                   help="real run: regenerate + commit + push to prod (the cron path; required to write prod)")
    p.add_argument("--dry-run", action="store_true", help="scan + print diff, no write")
    p.add_argument("--no-push", action="store_true", help="commit but don't push")
    return p.parse_args(argv)


def _enforce_default_safe(args: argparse.Namespace) -> int | None:
    """Default-safe contract (T-0259 / T-0825): refuse a flagless invocation.

    The prod action is regenerate + commit + push to origin/main. The caller must
    explicitly opt in via --commit-prod, OR opt into a reduced mode (--dry-run /
    --no-push). A flagless run refuses (exit 64) rather than silently writing prod
    — the no-prod-default invariant. The systemd unit passes --commit-prod
    explicitly. Returns the exit code (64) to reject the invocation, else None.
    """
    safe_explicit = args.dry_run or args.no_push
    if args.commit_prod and safe_explicit:
        print(
            "ERROR: --commit-prod conflicts with a safe-mode flag (--dry-run / --no-push).",
            file=sys.stderr,
        )
        return 64
    if not (args.commit_prod or safe_explicit):
        print(
            "ERROR: refusing to run with no mode flag. Pass --commit-prod (the cron "
            "path) or a safe mode (--dry-run / --no-push).",
            file=sys.stderr,
        )
        return 64
    return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    refusal = _enforce_default_safe(args)
    if refusal is not None:
        return refusal
    return run_all(dry_run=args.dry_run, push=not args.no_push)


if __name__ == "__main__":
    sys.exit(main())
