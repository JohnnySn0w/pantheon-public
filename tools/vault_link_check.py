#!/usr/bin/env python3
"""Vault link check — verify every `[[wikilink]]` in the repo resolves for Obsidian (T-1597).

Obsidian resolves ``[[target]]`` against (a) any markdown file's basename
(sans ``.md``) anywhere in the vault and (b) any name listed in a file's YAML
frontmatter ``aliases:``. This tool applies the same rules across the repo and
classifies every wikilink occurrence OUTSIDE fenced code blocks:

  resolved       — matches a basename, alias, or vault-relative path.
  memory-pointer — matches the documented agent-memory naming convention
                   (``feedback_*``, ``reference_*``, ``project_*``, …). These
                   name files in an agent's PRIVATE memory dir, not the vault;
                   they are intentional cross-references, not dead links (see
                   sop/standard/obsidian-vault-conventions.md).
  cross-repo     — points into a sibling repo's namespace (echo ADRs:
                   ``ADR-NNN``, ``adr/ADR-…``, ``Supplementary Material/…``).
                   Resolves once that repo sits in the same parent-folder
                   vault and adopts the aliases convention.
  citation       — pure-numeric ``[[1]]``-style markers (historical watch-
                   report idiom; new reports should use ``[^1]`` footnotes).
  annotation     — confidence/severity markup containing ``: `` (historical
                   watch-report idiom, e.g. ``[[single-source: …]]``).
  dead           — resolves to nothing. FAILS in curated dirs; WARNS in
                   historical record dirs (tickets/done, watch reports,
                   incidents, ledger) which are never rewritten.

Exit codes: 0 = no dead links in curated dirs; 1 = curated dead links found;
64 = usage error.

Usage:
  python3 tools/vault_link_check.py [--repo-root PATH] [--list-memory-pointers]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    from tools._tree_walk import walk_paths
except ImportError:  # bare run with cwd=tools/ (tools/ on sys.path, repo root not)
    from _tree_walk import walk_paths  # type: ignore[no-redef]

WIKILINK_RE = re.compile(r"\[\[([^\]\[|#]+)(?:#[^\]\[|]*)?(?:\|[^\]\[]*)?\]\]")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE_RE = re.compile(r"`[^`]*`")

# The agent-memory naming convention (memory files live in each agent's
# ~/.claude/.../memory/, deliberately OUTSIDE the repo vault).
MEMORY_PREFIXES = (
    "feedback_", "reference_", "project_", "identity_", "role_",
    "handoff_", "recovery_", "lesson_", "user_",
)

# Sibling-repo namespaces (echo ADRs et al.) — resolvable only in the
# parent-folder multi-repo vault, once that repo adopts the convention.
CROSS_REPO_RE = re.compile(r"^(ADR-\d+$|adr/ADR-|Supplementary Material/)")

# Files that seed another agent's private vault — wikilinks inside them point
# INTO that agent's namespace (their memory + their vault sections), which live
# outside this repo. Treated as cross-agent-vault references, not dead. Example:
# pinned-rules/archivist.md is Orchestrator's seed for archivist (ORG-042); its
# [[feedback-*]], [[project-*]], [[rulebook-*]] links target archivist's own
# vault under /home/exocortex/, unreachable from this repo.
AGENT_VAULT_SEED_PREFIXES = ("pinned-rules/",)

# Code-identifier shape (T-1597 follow-up 2026-07-12): `[[tool_executor.source]]`,
# `[[hooks.<Event>]]` etc. are code refs written in prose, not vault links — a
# dotted identifier (a.b) with no path separator and not a real note filename.
# No markdown note is named this way, so treating them as a distinct class stops
# the recurring false-positive without editing every author's prose.
CODE_REF_RE = re.compile(r"^[A-Za-z_][\w<>]*\.[\w<>.]+$")

# Historical record dirs: never rewritten, so dead links there WARN not FAIL.
HISTORICAL_PREFIXES = ("tickets/done/", "knowledge/watch/reports/", "incidents/", "ledger/")

# Directories that are not vault content.
SKIP_DIRS = {".git", ".obsidian", "node_modules", "__pycache__"}


def iter_md_files(root: Path):
    # walk_paths, not rglob: tolerate entries vanishing mid-walk (T-4450).
    for p in walk_paths(root, "*.md"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        yield p


def frontmatter_aliases(text: str) -> list[str]:
    """Parse ``aliases:`` from a leading YAML frontmatter block. Supports the
    inline-list form ``aliases: [A, B]`` and the dash-list form. Tolerant of
    files with no frontmatter; never raises."""
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    if end == -1:
        return []
    aliases: list[str] = []
    lines = text[3:end].splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^aliases:\s*\[(.*)\]\s*$", line)
        if m:
            aliases += [a.strip().strip("'\"") for a in m.group(1).split(",") if a.strip()]
        elif re.match(r"^aliases:\s*$", line):
            j = i + 1
            while j < len(lines):
                d = re.match(r"^\s*-\s*(.+?)\s*$", lines[j])
                if not d:
                    break
                aliases.append(d.group(1).strip("'\""))
                j += 1
            i = j - 1
        i += 1
    return aliases


def extract_links(text: str) -> list[str]:
    """All wikilink targets outside fenced code blocks and inline code spans."""
    targets: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = INLINE_CODE_RE.sub("", line)
        for m in WIKILINK_RE.finditer(line):
            targets.append(m.group(1).strip())
    return targets


def run(repo_root: Path, list_memory_pointers: bool = False) -> int:
    resolvable: set[str] = set()
    paths: set[str] = set()
    files = list(iter_md_files(repo_root))
    texts: dict[Path, str] = {}
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        texts[p] = text
        resolvable.add(p.stem)
        resolvable.update(frontmatter_aliases(text))
        rel = str(p.relative_to(repo_root))
        paths.add(rel)
        paths.add(rel[:-3])  # sans .md — Obsidian accepts both path forms

    dead_fail: list[tuple[str, str]] = []
    dead_warn: list[tuple[str, str]] = []
    memory_ptrs: list[tuple[str, str]] = []
    counts = {"resolved": 0, "cross-repo": 0, "citation": 0, "annotation": 0, "code-ref": 0, "agent-vault": 0}
    n_links = 0
    for p, text in texts.items():
        rel = str(p.relative_to(repo_root))
        for target in extract_links(text):
            n_links += 1
            if target in resolvable or target in paths:
                counts["resolved"] += 1
            elif target.startswith(MEMORY_PREFIXES):
                memory_ptrs.append((rel, target))
            elif CROSS_REPO_RE.match(target):
                counts["cross-repo"] += 1
            elif target.isdigit():
                counts["citation"] += 1
            elif ": " in target:
                counts["annotation"] += 1
            elif "/" not in target and CODE_REF_RE.match(target):
                counts["code-ref"] += 1
            elif rel.startswith(AGENT_VAULT_SEED_PREFIXES):
                counts["agent-vault"] += 1
            elif rel.startswith(HISTORICAL_PREFIXES):
                dead_warn.append((rel, target))
            else:
                dead_fail.append((rel, target))

    print(
        f"[vault-link-check] {n_links} wikilinks in {len(files)} files: "
        f"{counts['resolved']} resolved, {len(memory_ptrs)} agent-memory pointers, "
        f"{counts['cross-repo']} cross-repo, {counts['agent-vault']} agent-vault, "
        f"{counts['citation']} citation, {counts['annotation']} annotation, "
        f"{counts['code-ref']} code-ref, "
        f"{len(dead_warn)} dead-historical (warn), "
        f"{len(dead_fail)} dead-curated (FAIL)"
    )
    if list_memory_pointers:
        for f, t in sorted(set(memory_ptrs)):
            print(f"  memory-pointer: [[{t}]] in {f}")
    for f, t in sorted(set(dead_warn)):
        print(f"  warn (historical): [[{t}]] in {f}")
    for f, t in sorted(set(dead_fail)):
        print(f"  DEAD: [[{t}]] in {f}")
    return 1 if dead_fail else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--list-memory-pointers", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.repo_root)
    if not root.is_dir():
        print(f"ERROR: not a directory: {root}", file=sys.stderr)
        return 64
    return run(root, args.list_memory_pointers)


if __name__ == "__main__":
    sys.exit(main())
