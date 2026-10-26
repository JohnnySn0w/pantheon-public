#!/usr/bin/env python3
"""bugs_entry_lint.py — a NEW bugs/ entry must carry a `## Verification` section (T-4562).

T-2525 made `## Verification` a required bugs/ field, but only in the README's
template, and bugs/README.md itself says a README-only criterion is a policy, and
policies decay: 14 of 59 entries added after 2026-08-04 shipped without it. This
is the structural half. The pre-commit hook calls `findings()` on the bugs/*.md
files a commit ADDS (template-forward: legacy entries are not touched, and a
missing section on one does not make it wrong, per bugs/README.md).

Run by hand: python3 tools/bugs_entry_lint.py bugs/<entry>.md [...]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

VERIFICATION_RE = re.compile(r"^##[ \t]+Verification\b", re.MULTILINE)
EXEMPT = {"bugs/README.md"}


def is_entry(path: str) -> bool:
    """A bugs/ entry: a top-level bugs/*.md other than the README."""
    return (path.startswith("bugs/") and path.endswith(".md")
            and path.count("/") == 1 and path not in EXEMPT)


def has_verification(text: str) -> bool:
    return VERIFICATION_RE.search(text) is not None


def findings(entries: dict[str, str]) -> list[str]:
    """{path: text} of ADDED files -> one message per bugs/ entry missing the section."""
    return [
        f"{path}: new bugs/ entry has no '## Verification' section "
        "(a one-line repro or the measurement that shows the symptom; see bugs/README.md)"
        for path, text in sorted(entries.items())
        if is_entry(path) and not has_verification(text)
    ]


def main(argv: list[str]) -> int:
    entries = {p: Path(p).read_text(encoding="utf-8", errors="replace") for p in argv}
    problems = findings(entries)
    for line in problems:
        print(line, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
