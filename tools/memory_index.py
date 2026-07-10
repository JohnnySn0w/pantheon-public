#!/usr/bin/env python3
"""tools/memory_index.py — keep MEMORY.md's pin inventory drift-free (ORG-035 / T-1130).

The split-out half of T-1129: `memory_lint` *detects* index drift ("pin not
indexed in MEMORY.md"); this *closes* it. MEMORY.md is loaded into context every
session, so the design problem (why T-1130 was split from T-1129) is that a naive
type-flatten regen would CLOBBER two kinds of curated value:

  1. The prose intro ("Process discipline" → D4-D10 pointer).
  2. The hand-curated SEMANTIC sections ('Reference', 'Project state',
     'User preferences', 'Cognitive-load drift catches', 'P1-flavor design
     defaults') — which do NOT map 1:1 to `metadata.type` (the last two are both
     feedback-type, split by meaning) and whose one-line *hooks* are deliberately
     context-load-optimized, NOT copies of each pin's recall `description`.

DESIGN DECISION (T-1130, justifies-via P4 + D11):
We do NOT regenerate the whole file, and we do NOT mirror a full by-type inventory
(that would DOUBLE the every-session context cost — every pin once in its curated
section, once in the inventory — on the hottest path in the system; the complete
by-type view is already available on demand via memory_lint / memory_search).

Instead we maintain ONE sentinel-bounded **backstop** region listing only the
pins that are NOT yet hand-filed anywhere in the curated body — the exact thing a
human forgets when adding a pin. A pin counts as "filed" iff a `(<filename>)`
link to it appears OUTSIDE the sentinel region (same definition memory_lint uses
for "indexed"). File a pin into a curated section → it leaves the backstop on the
next run; forget to → it stays, visible and grouped by type for triage. Steady
state (all filed) leaves the region empty but for an "all filed" marker, so
MEMORY.md stays lean and drift is structurally impossible to hide.

The sentinel-region state machine mirrors `spine_index_groom.py` (D4: invariant
enforced by the parser, never a regex over content). Region is created append-only
on first run if absent; all curated content outside it is preserved byte-for-byte.

MANUAL TOOL — NO CRON. Auto-rewriting a curated, every-session, operator-curated
file is high-blast-radius; drift *detection* is already live (memory_lint, daily
via the knowledge sweep). Run by hand, dry-run first, eyeball, then --apply.

  python3 tools/memory_index.py <memory_dir>            # dry-run (default): show diff
  python3 tools/memory_index.py <memory_dir> --apply    # write the backstop region
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

# memory_lint is a sibling ORG-035 baseline tool; reuse its frontmatter parser so
# the two never drift on what a pin's `type`/`name`/`description` mean. Dual-use
# bootstrap (same trick as inbox_consumer.py): when run as a script (`python3
# tools/memory_index.py`) __package__ is empty → put the repo root on the path so
# `from tools.X` resolves; when imported as `tools.memory_index` (the canonical
# smoke `python3 -m unittest tools.test_memory_index`) the shim is a no-op.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import memory_lint as ml  # noqa: E402

SENTINEL_START = "<!-- AUTOGEN-START memory-index -->"
SENTINEL_END = "<!-- AUTOGEN-END memory-index -->"
SECTION_HEADING = "## Unfiled pins (auto-generated — `tools/memory_index.py`)"
ALL_FILED_MARKER = "<!-- all pins are filed in the curated sections above; nothing unfiled -->"

# Stable display order for the by-type grouping inside the backstop region.
TYPE_ORDER = ("user", "feedback", "project", "reference")
LINK_RE = re.compile(r"\(([A-Za-z0-9_.-]+\.md)\)")


# ---------------------------------------------------------------------------
# Sentinel-region parser (line-by-line state machine — mirrors spine_index_groom).
# ---------------------------------------------------------------------------


def read_sentinel_region(text: str) -> tuple[str, str, str] | None:
    """Split text into (before, inside, after) around the sentinels.

    Returns None if the start sentinel is not found (file has no region yet).
    """
    before: list[str] = []
    inside: list[str] = []
    after: list[str] = []
    state = "BEFORE"
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n").rstrip("\r")
        if state == "BEFORE":
            before.append(line)
            if stripped == SENTINEL_START:
                state = "INSIDE"
        elif state == "INSIDE":
            if stripped == SENTINEL_END:
                after.append(line)
                state = "AFTER"
            else:
                inside.append(line)
        else:
            after.append(line)
    if state == "BEFORE":
        return None
    return "".join(before), "".join(inside), "".join(after)


# ---------------------------------------------------------------------------
# Pin scanning + backstop rendering (pure — testable without touching disk).
# ---------------------------------------------------------------------------


def scan_pins(memory_dir: Path) -> list[dict]:
    """Return [{filename, type, name, description}] for every pin (sorted by name)."""
    pins: list[dict] = []
    for path in sorted(memory_dir.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        block, _body = ml.split_frontmatter(path.read_text(encoding="utf-8"))
        fm = ml.parse_frontmatter(block) if block else {}
        pins.append(
            {
                "filename": path.name,
                "type": fm.get("_type") or "untyped",
                "name": fm.get("name") or path.stem,
                "description": fm.get("description") or "",
            }
        )
    return pins


def filed_filenames(curated_text: str) -> set[str]:
    """Pin filenames already linked in the curated body (outside the region)."""
    return set(LINK_RE.findall(curated_text))


def render_region_body(unfiled: list[dict]) -> str:
    """Render the inside-sentinel text for the given unfiled pins (grouped by type).

    Pure: returns the block that goes BETWEEN the sentinels (no sentinels), with a
    trailing newline. Empty inventory → a single 'all filed' marker line.
    """
    if not unfiled:
        return f"\n{ALL_FILED_MARKER}\n\n"
    by_type: dict[str, list[dict]] = {}
    for pin in unfiled:
        by_type.setdefault(pin["type"], []).append(pin)
    ordered = [t for t in TYPE_ORDER if t in by_type]
    ordered += sorted(t for t in by_type if t not in TYPE_ORDER)
    lines = [""]
    for t in ordered:
        lines.append(f"### {t}")
        for pin in sorted(by_type[t], key=lambda p: p["name"]):
            desc = f" — {pin['description']}" if pin["description"] else ""
            lines.append(f"- [{pin['name']}]({pin['filename']}){desc}")
        lines.append("")
    return "\n".join(lines) + "\n"


def build_new_text(current: str, pins: list[dict]) -> str:
    """Return MEMORY.md text with the backstop region refreshed (idempotent).

    Creates the region append-only if absent; preserves all curated content.
    """
    parts = read_sentinel_region(current)
    if parts is None:
        before, after = current, ""
    else:
        before, _old_inside, after = parts
        # Strip the whole generated block (heading + sentinels) from before/after;
        # we rebuild it. Cut `before` back to the section heading if present (so the
        # heading isn't duplicated), else to the start sentinel.
        cut = before.rfind(SECTION_HEADING)
        if cut == -1:
            cut = before.rfind(SENTINEL_START)
        before = before[:cut] if cut != -1 else before
        after = after[after.find(SENTINEL_END) + len(SENTINEL_END):] if SENTINEL_END in after else after
        after = after.lstrip("\n")

    curated = before + after
    unfiled = [p for p in pins if p["filename"] not in filed_filenames(curated)]
    region_body = render_region_body(unfiled)

    block = f"{SECTION_HEADING}\n\n{SENTINEL_START}{region_body}{SENTINEL_END}\n"

    head = before.rstrip("\n")
    tail = after.strip("\n")
    pieces = [head, block]
    if tail:
        pieces.append(tail)
    return "\n\n".join(p for p in pieces if p) + "\n"


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("memory_dir", help="the pin directory (must contain MEMORY.md)")
    ap.add_argument("--apply", action="store_true",
                    help="write the change (default is dry-run: show the diff only)")
    args = ap.parse_args(argv)

    mem = Path(args.memory_dir)
    if not mem.is_dir():
        print(f"memory_index: {mem} is not a directory", file=sys.stderr)
        return 2
    index = mem / "MEMORY.md"
    if not index.exists():
        print(f"memory_index: {index} does not exist (won't create from scratch)",
              file=sys.stderr)
        return 2

    current = index.read_text(encoding="utf-8")
    pins = scan_pins(mem)
    new_text = build_new_text(current, pins)

    if new_text == current:
        print("memory_index: MEMORY.md backstop region already up to date.",
              file=sys.stderr)
        return 0

    if args.apply:
        index.write_text(new_text, encoding="utf-8")
        print(f"memory_index: wrote {index}", file=sys.stderr)
        return 0

    diff = difflib.unified_diff(
        current.splitlines(keepends=True), new_text.splitlines(keepends=True),
        fromfile="MEMORY.md (current)", tofile="MEMORY.md (proposed)")
    sys.stdout.writelines(diff)
    print("\nmemory_index: dry-run — re-run with --apply to write.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
