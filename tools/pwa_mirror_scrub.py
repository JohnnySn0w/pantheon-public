#!/usr/bin/env python3
"""T-3316: the pwa-release-mirror option-2 scrub (the operator's T-2131 ruling).

The pwa mirror build merges pantheon-pwa into its persistent `genericize` branch.
This scrub is a RULE that every refresh re-applies to that merged tree, after the
merge and before the leak scan, so the classes below can never return with an
upstream delta. Option 2 of the ruling:

- persona names -> roles. Orchestrator/Specialist/Recovery -> orchestrator/researcher/recovery,
  verbatim per the ruling. The other personas take the pantheon-public seed's
  words (neutralize_seed): mason->mason, artisan->artisan, archivist->archivist,
  consultant->consultant, watchman->watchman.
- `pantheon-config@<sha>` commit refs: the sha is dropped.
- `msg_NNNN` operator message numbers: dropped, with their `+`/`,` joiners.
- the `swarm` hostname: dropped. The common word ("swarm-wide") is not the host
  and is kept; the host only appears as a hostname form (`swarm:`, `@swarm`,
  `hosts/swarm`, `on swarm`).

- the retired role label `orchestrator` -> orchestrator (the pwa name-map).

KEPT: T-NNNN / ADR-NNN / ORG-NNN refs (the T-1987 provenance convention).

Usage: python3 tools/pwa_mirror_scrub.py <tree> [--check]
  --check: change nothing; print the per-class counts and exit 1 if any is
  non-zero (the refresh gate). Exit 2: a FIXUPS entry no longer matches.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PERSONAS = [
    ("orchestrator", "orchestrator"), ("specialist", "researcher"), ("recovery", "recovery"),
    ("mason", "mason"), ("artisan", "artisan"), ("archivist", "archivist"),
    ("consultant", "consultant"), ("watchman", "watchman"),
]


def _case_forms(src: str, dst: str):
    return [(src.upper(), dst.upper()), (src.capitalize(), dst.capitalize()), (src, dst)]


# A persona token is a whole word or an identifier component: not inside a longer
# word ("toward", "warden", "artisans"), but split at '_', '-', '.', digits and a
# camelCase hump ("orchestratorSeat"). UPPER forms are bounded by other capitals.
_PERSONA_RULES: list[tuple[re.Pattern, str]] = []
for _src, _dst in PERSONAS:
    for _s, _d in _case_forms(_src, _dst):
        if _s.isupper():
            pat = rf"(?<![A-Z]){_s}(?![A-Z])"
        else:
            pat = rf"(?<![A-Za-z]){_s}(?![a-z])"
        _PERSONA_RULES.append((re.compile(pat), _d))

# The retired role label (the pwa name-map: orchestrator -> orchestrator).
# Five survivors sat in ADR prose on the mirror at c8fab557. Role ids such as
# head_engineering_orchestrator do not match.
_PERSONA_RULES.append((re.compile(r"(?<![A-Za-z_])orchestrator(?![A-Za-z_])"), "orchestrator"))

_MSG_SEQ = r"msg_\d+(?:[ \t]*(?:\+|,|and)[ \t]*msg_\d+)*"
# Drop rules and their tidy-up run LINE BY LINE with [ \t] only, never \s, and
# tidy touches only a line a drop changed: joining lines or collapsing runs of
# spaces would wreck code layout, tables and box art.
_DROP_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"pantheon-config@[0-9a-f]{6,40}\b"), "pantheon-config"),
    # a list item that is only msg refs plus a dash: "- msg_1 + msg_2 — text"
    (re.compile(rf"^([ \t]*- ){_MSG_SEQ}[ \t]*—[ \t]*"), r"\1"),
    (re.compile(rf"[ \t]*{_MSG_SEQ}"), ""),
    (re.compile(r"(?<![\w-])swarm(?=:|/)|(?<=@)swarm\b|(?<=hosts/)swarm\b"), "host"),
    (re.compile(r"\bon swarm\b"), "on the host"),
]
# punctuation a dropped ref leaves behind, on that line only
_TIDY_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\([ \t]*,[ \t]*"), "("),
    (re.compile(r",[ \t]*,"), ","),
    (re.compile(r"\([ \t]+"), "("),
    (re.compile(r"[ \t]+\)"), ")"),
]

# Load-bearing test data the rename changes the MEANING of (orchestrator -> orchestrator
# collides with the generic word in role ids and hints; renamed agents sort in a
# new order). Applied to the scrubbed text, each must match EXACTLY expect times,
# or the scrub fails, so an upstream change to these tests surfaces instead of
# silently shipping a red mirror. (path, find, replace, expect)
FIXUPS: list[tuple[str, str, str, int]] = [
    ("tests/tickets_target_filter.test.ts",
     'get("?target=orchestrator");', 'get("?target=recovery");', 1),
    ("tests/tickets_target_filter.test.ts",
     'expect(text).not.toContain("orchestrator");', 'expect(text).not.toContain("recovery");', 1),
    ("tests/ticketTargets.test.ts",
     'expect(toRoutingTarget("orchestrator")).toBeUndefined();',
     'expect(toRoutingTarget("retired_head")).toBeUndefined();', 1),
    ("tests/repository_listagents.test.ts",
     '["researcher", "orchestrator", "recovery"]', '["orchestrator", "recovery", "researcher"]', 1),
    ("web/tests/WellTrodPage.test.ts",
     "toBe('researcher: 3 sessions')", "toBe('orchestrator: 17 sessions')", 1),
]


class FixupError(RuntimeError):
    pass


def apply_fixups(root: Path, fixups=None) -> int:
    done = 0
    for rel, find, rep, expect in (FIXUPS if fixups is None else fixups):
        p = root / rel
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        got = text.count(find)
        if got != expect:
            raise FixupError(f"fixup {rel}: {find!r} found {got}x, expected {expect}x")
        if find != rep:
            p.write_text(text.replace(find, rep), encoding="utf-8")
        done += 1
    return done


CLASS_PATTERNS = {
    "persona": re.compile(r"(?i)(?<![a-z])(?:orchestrator|specialist|recovery|mason|artisan|archivist|consultant|watchman)(?![a-z])"),
    "commit-ref": re.compile(r"pantheon-config@[0-9a-f]{6,40}"),
    "msg-id": re.compile(r"\bmsg_\d+"),
    "orchestrator": re.compile(r"(?<![A-Za-z_])orchestrator(?![A-Za-z_])"),
    "swarm-host": re.compile(r"(?<![\w-])swarm(?=:|/)|@swarm\b|hosts/swarm\b|\bon swarm\b"),
}

_SKIP_DIRS = {".git", "node_modules", "build", ".svelte-kit", "dist"}
_TEXT_EXT = {".md", ".ts", ".js", ".svelte", ".json", ".yaml", ".yml", ".txt", ".sh",
             ".html", ".css", ".py", ".toml", ".example", ".service", ".conf", ""}


def scrub_text(text: str) -> tuple[str, int]:
    n = 0
    for rx, rep in _PERSONA_RULES:
        text, k = rx.subn(rep, text)
        n += k
    lines = text.split("\n")
    for i, line in enumerate(lines):
        new = line
        for rx, rep in _DROP_RULES:
            new, k = rx.subn(rep, new)
            n += k
        if new != line:
            for rx, rep in _TIDY_RULES:
                new = rx.sub(rep, new)
            lines[i] = new
    return "\n".join(lines), n


def _files(root: Path):
    for p in sorted(root.rglob("*")):
        if any(part in _SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if p.is_file() and not p.is_symlink() and (p.suffix in _TEXT_EXT or p.name.startswith(".")):
            yield p


def _rename(name: str) -> str:
    for rx, rep in _PERSONA_RULES:
        name = rx.sub(rep, name)
    return name


def scrub_tree(root: Path) -> dict:
    report = {"files_changed": 0, "substitutions": 0, "renamed": 0}
    for p in list(_files(root)):
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new, n = scrub_text(text)
        if n:
            p.write_text(new, encoding="utf-8")
            report["files_changed"] += 1
            report["substitutions"] += n
        renamed = _rename(p.name)
        if renamed != p.name:
            p.rename(p.with_name(renamed))
            report["renamed"] += 1
    return report


def count_classes(root: Path) -> dict:
    counts = {k: 0 for k in CLASS_PATTERNS}
    for p in _files(root):
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for k, rx in CLASS_PATTERNS.items():
            counts[k] += len(rx.findall(text)) + len(rx.findall(p.name))
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tree", type=Path)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    if not a.check:
        report = scrub_tree(a.tree)
        try:
            report["fixups"] = apply_fixups(a.tree)
        except FixupError as e:
            print(f"FIXUP FAILED: {e}", file=sys.stderr)
            return 2
        print(report)
    counts = count_classes(a.tree)
    print(counts)
    return 1 if any(counts.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
