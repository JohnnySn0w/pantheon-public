#!/usr/bin/env python3
"""tools/memory_lint.py — validate an agent's auto-memory pins (ORG-035 baseline; T-1129).

Lints a memory directory of one-fact-per-file pins (`<type>_<slug>.md` + a
`MEMORY.md` index). Part of the ORG-035 per-agent baseline; it is the mechanism
that closes ORG-012 §5 cross-user coverage (T-0872) — HO runs it over its OWN
pins (`python3 tools/memory_lint.py ~/.claude/projects/-home-claude/memory`), so
HO's over-pinning/staleness risk finally has a scripted check.

LENIENT BY SEVERITY (read-only; never mutates). Existing pins predate the
ORG-035 schema and come in two frontmatter shapes — top-level `type:` (+ top-level
`originSessionId`, sentence `name`) and nested `metadata.type:` — so the lint
accepts both and grades issues:

  ERROR  — structurally broken: no frontmatter / no `name` / no `description` /
           `type` missing or not in {user, feedback, project, reference} /
           no body / filename-prefix disagrees with the declared type.
  WARN   — ORG-035 recommendations not yet met: missing provenance/freshness
           fields (owner/source/created/updated/verified/scope); a `verified`
           older than --stale-days; a `[[link]]` that resolves to no pin;
           MEMORY.md index drift (a pin not indexed, or an index entry with no pin).

Exit 0 if no ERRORs (WARN-only still exits 0 unless --strict); 1 if any ERROR
(or any WARN under --strict).

  python3 tools/memory_lint.py <memory_dir>            # lint
  python3 tools/memory_lint.py <memory_dir> --strict   # warns are failures too
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

VALID_TYPES = {"user", "feedback", "project", "reference"}
PROVENANCE_FIELDS = ("owner", "source", "created", "updated", "verified", "scope")
DEFAULT_STALE_DAYS = 120


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Return (frontmatter_block, body). frontmatter is None if absent/malformed."""
    if not text.startswith("---"):
        return None, text
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return None, text
    return m.group(1), m.group(2)


def parse_frontmatter(block: str) -> dict:
    """Minimal YAML-ish parse: top-level `key: value` + one nested `metadata:` map.

    Sufficient for the pin schema (no lists/multiline); avoids a yaml dependency.
    Nested metadata keys are surfaced flat too (so `type` is found in either shape).
    """
    out: dict = {}
    meta: dict = {}
    in_meta = False
    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indented = raw[0] in " \t"
        line = raw.strip()
        if line.rstrip().endswith(":") and line.rstrip()[:-1] == "metadata":
            in_meta = True
            continue
        if ":" not in line:
            in_meta = False
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if in_meta and indented:
            meta[key] = val
        else:
            in_meta = False
            out[key] = val
    out["metadata"] = meta
    # Surface type from either location for callers.
    out["_type"] = out.get("type") or meta.get("type")
    out["_all_keys"] = set(out) | set(meta)
    return out


def lint_pin(path: Path, stale_days: int, today_ord: int | None) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for one pin file."""
    errors: list[str] = []
    warns: list[str] = []
    text = path.read_text(encoding="utf-8")
    block, body = split_frontmatter(text)
    if block is None:
        errors.append("no/malformed frontmatter")
        return errors, warns
    fm = parse_frontmatter(block)

    if not fm.get("name"):
        errors.append("missing `name`")
    if not fm.get("description"):
        errors.append("missing `description`")
    t = fm.get("_type")
    if not t:
        errors.append("missing `type` (top-level or metadata.type)")
    elif t not in VALID_TYPES:
        errors.append(f"type {t!r} not in {sorted(VALID_TYPES)}")
    if not body.strip():
        errors.append("empty body")

    # Filename prefix must agree with the declared type (feedback_*.md → feedback).
    prefix = path.name.split("_", 1)[0]
    if t and prefix in VALID_TYPES and prefix != t:
        errors.append(f"filename prefix {prefix!r} disagrees with type {t!r}")

    # WARN: ORG-035 provenance/freshness not yet present.
    missing_prov = [f for f in PROVENANCE_FIELDS if f not in fm["_all_keys"]]
    if missing_prov:
        warns.append(f"missing provenance fields: {', '.join(missing_prov)}")
    verified = fm.get("verified") or fm["metadata"].get("verified")
    if verified and today_ord is not None:
        vo = _date_ord(verified)
        if vo is not None and today_ord - vo > stale_days:
            warns.append(f"`verified` {verified} older than {stale_days}d (re-verify)")

    return errors, warns


def _date_ord(s: str) -> int | None:
    """YYYY-MM-DD → a comparable ordinal (days), without importing datetime at import.

    Pure arithmetic so tests can pass a fixed `today_ord`.
    """
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    return y * 372 + mo * 31 + d  # monotonic enough for day-granularity staleness


def check_index(memory_dir: Path) -> list[str]:
    """MEMORY.md index drift: pins not linked, and links with no pin (warnings)."""
    warns: list[str] = []
    index = memory_dir / "MEMORY.md"
    pins = {p.name for p in memory_dir.glob("*.md") if p.name != "MEMORY.md"}
    if not index.exists():
        if pins:
            warns.append("no MEMORY.md index present but pins exist")
        return warns
    itext = index.read_text(encoding="utf-8")
    linked = set(re.findall(r"\(([A-Za-z0-9_.-]+\.md)\)", itext))
    for p in sorted(pins - linked):
        warns.append(f"pin not indexed in MEMORY.md: {p}")
    for link in sorted(linked - pins):
        warns.append(f"MEMORY.md links a missing pin: {link}")
    return warns


def lint_dir(memory_dir: Path, stale_days: int, today_ord: int | None):
    """Yield (filename, errors, warns) per pin, plus ('MEMORY.md', [], index_warns)."""
    for path in sorted(memory_dir.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        errs, warns = lint_pin(path, stale_days, today_ord)
        if errs or warns:
            yield path.name, errs, warns
    iw = check_index(memory_dir)
    if iw:
        yield "MEMORY.md", [], iw


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("memory_dir", help="the pin directory to lint")
    ap.add_argument("--strict", action="store_true", help="warnings fail too")
    ap.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS)
    ap.add_argument("--today-ord", type=int, default=None,
                    help="fixed today ordinal (tests); default derives from clock")
    args = ap.parse_args(argv)

    today_ord = args.today_ord
    if today_ord is None:
        import time
        lt = time.gmtime()
        today_ord = lt.tm_year * 372 + lt.tm_mon * 31 + lt.tm_mday

    mem = Path(args.memory_dir)
    if not mem.is_dir():
        print(f"memory_lint: {mem} is not a directory", file=sys.stderr)
        return 2

    n_err = n_warn = 0
    for name, errs, warns in lint_dir(mem, args.stale_days, today_ord):
        for e in errs:
            print(f"ERROR  {name}: {e}")
            n_err += 1
        for w in warns:
            print(f"WARN   {name}: {w}")
            n_warn += 1
    print(f"memory_lint: {n_err} error(s), {n_warn} warning(s) over {mem}", file=sys.stderr)
    if n_err or (args.strict and n_warn):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
