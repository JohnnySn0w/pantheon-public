"""Prevent-at-creation lint for ADR build-scoping (T-2806, the operator TG 7590).

The operator, TG 7590 (2026-08-10): "ticket system shouldn't accept an adr without
a connected ticket that will scope planning and followup tickets when the
adr is ratified." This is the STRUCTURAL PRIMARY for that rule.
`tools/ticket_grooming.py`'s ratified-unbuilt-ADR check
(`find_ratified_unbuilt_adrs`) is the BACKSTOP — it catches anything that
predates this lint or slips through once an ADR is ratified. This lint only
inspects genuinely NEW files in a diff; existing ADRs are grandfathered.

Rejects a newly-added `architecture/adr/(ADR|ORG)-NNN*.md` file whose frontmatter doesn't
declare either:
  - `build_disposition: no-build-needed` or `built` (exempt — no
    implementation is expected), OR
  - a `scoping_ticket: T-NNNN` (the ticket that will file the ADR's planning
    + follow-up build tickets on ratification).

Absence of `build_disposition` is treated as `needs-build` (stricter
reading, HO-ratified 2026-08-10 per the operator's TG 7590 framing: a landing ADR
must declare itself, not silently skip the question).

Public surface:
    check_adr_frontmatter  — pure function: one ADR file's full text ->
                              violation message or None.
    scan_diff_for_new_adrs — parse unified-diff text; return violation
                              messages for newly-added ADR files.
    main                   — CLI entry point; reads diff from stdin or a
                              file path arg; exits 0 (clean) or 1 (violations).

Used by `.github/workflows/adr-scoping-lint.yml`. Mirrors
`tools/hermes_diff_scan.py`'s shape: a pure function over diff text, no git
subprocess, fully unit-testable without a real repo.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: ensure repo root is on sys.path so tools.adr_reeval_walk is
# importable regardless of cwd (mirrors tools/hermes_diff_scan.py).
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from tools.adr_reeval_walk import _load_yaml, _split_frontmatter  # noqa: E402
from tools.hermes_diff_scan import iter_added_lines  # noqa: E402

_EXEMPT_DISPOSITIONS = frozenset({"no-build-needed", "built"})

# A newly-added ADR file path, e.g. "architecture/adr/ORG-069-foo.md" /
# "architecture/adr/ADR-500-x.md" (path per ORG-094 architecture umbrella; the
# repo-root adr/ tree was moved to architecture/adr/ on 2026-09-06, T-3472).
_NEW_ADR_PATH_RE = re.compile(r"^architecture/adr/(?:ADR|ORG)-\d+[^/]*\.md$")


def check_adr_frontmatter(path_display: str, text: str) -> str | None:
    """Return a violation message for `text` (one newly-added ADR file's
    full content) if it needs a `scoping_ticket` and lacks one; `None` if
    compliant.

    `path_display` is used only for the message — no filesystem access here,
    this is a pure function on already-read text.
    """
    fm_block, _body = _split_frontmatter(text)
    fm = _load_yaml(fm_block, source=path_display)
    disposition = fm.get("build_disposition")
    disposition = disposition.strip() if isinstance(disposition, str) else None
    if disposition in _EXEMPT_DISPOSITIONS:
        return None
    scoping_ticket = fm.get("scoping_ticket")
    scoping_ticket = scoping_ticket.strip() if isinstance(scoping_ticket, str) else None
    if scoping_ticket:
        return None
    return (
        f"{path_display}: needs a build_disposition of no-build-needed/built, "
        "or a scoping_ticket: T-NNNN (the operator TG 7590 / T-2806 — a landing "
        "ADR must declare whether it needs implementation)"
    )


def scan_diff_for_new_adrs(diff_text: "str | bytes") -> list[str]:
    """Parse unified-diff text; return violation messages for every newly
    ADDED `architecture/adr/(ADR|ORG)-NNN...md` file whose reconstructed content fails
    `check_adr_frontmatter`.

    Only genuinely NEW files are considered — a `new file mode` line must
    appear in this file's header block (after its `diff --git` line).
    Modified existing ADRs are deliberately out of scope for this
    prevent-AT-CREATION lint (see module docstring); a wholly-new file's
    diff hunk is 100% added (`+`) lines, so the reconstructed content is
    exact.

    Framing is the shared structural parser (T-4202,
    `hermes_diff_scan.iter_added_lines`): hunk-count tracked, split on the
    newline byte only, so a body line starting `++ ` (rendered `+++ `) is
    content, not a header, and a form feed / U+2028 inside a line does not
    split it. Input is a two-endpoint `git diff` (adr-scoping-lint.yml); no
    combined (`@@@`) diffs are read. `str` input is UTF-8 encoded first.
    """
    if isinstance(diff_text, str):
        diff_text = diff_text.encode("utf-8", "surrogateescape")
    # path -> added content lines, in first-seen order. A path seen twice
    # (not produced by a single git diff) accumulates, never resets.
    files: dict[str, list[bytes]] = {}
    for ev in iter_added_lines(diff_text):
        if ev.is_new_file and ev.path is not None and _NEW_ADR_PATH_RE.match(ev.path):
            text = ev.text
            # A CRLF file's line ending is not frontmatter content: drop the
            # one trailing CR so `_split_frontmatter`'s "---\n" test still
            # sees a CRLF ADR's frontmatter (the old splitlines() did the
            # same). A CR elsewhere in the line stays.
            if text.endswith(b"\r"):
                text = text[:-1]
            files.setdefault(ev.path, []).append(text)

    violations: list[str] = []
    for path, content_lines in files.items():
        content = b"\n".join(content_lines).decode("utf-8", "replace")
        msg = check_adr_frontmatter(path, content)
        if msg:
            violations.append(msg)
    return violations


# T-4692: dirs whose frontmatter must parse. A state check, not a diff check:
# an edit to an EXISTING ADR (the #748 status flip with an unquoted ': ') never
# reaches the creation-only scan above, and docs-only PRs skip ci.yml, so this
# was the only lint that ran on it.
_FRONTMATTER_DIRS = ("architecture/adr", "architecture/rfc")


def scan_tree_frontmatter(root: Path) -> list[str]:
    """Return a violation per ADR/RFC under `root` whose frontmatter is not
    valid YAML. No frontmatter at all is legitimate; unparseable is not."""
    import yaml

    violations: list[str] = []
    for rel_dir in _FRONTMATTER_DIRS:
        for path in sorted((root / rel_dir).glob("*.md")):
            fm_block, _body = _split_frontmatter(path.read_text(encoding="utf-8"))
            if not fm_block.strip():
                continue
            try:
                yaml.safe_load(fm_block)
            except Exception as exc:  # noqa: BLE001 — any YAML error is a finding
                violations.append(
                    f"{rel_dir}/{path.name}: frontmatter is not valid YAML "
                    f"({type(exc).__name__}); quote any value containing ': '"
                )
    return violations


def main() -> int:
    """CLI entry point.

    Usage:
        git diff <base> HEAD | python3 tools/adr_scoping_lint.py
        python3 tools/adr_scoping_lint.py <diff-file>
        python3 tools/adr_scoping_lint.py --tree [<repo-root>]

    Diff mode exits 0 if no violations (including "no new ADR files in the
    diff"), 1 if any newly-added ADR fails the scoping-ticket check. Tree mode
    (T-4692) exits 1 if any ADR/RFC frontmatter in the tree fails to parse.
    """
    if len(sys.argv) > 1 and sys.argv[1] == "--tree":
        root = Path(sys.argv[2]) if len(sys.argv) > 2 else _REPO_ROOT
        broken = scan_tree_frontmatter(root)
        for msg in broken:
            print(f"  {msg}", file=sys.stderr)
        return 1 if broken else 0

    # Bytes in (T-4202): the framing splits on the newline byte only, so text
    # decoding / universal newlines must not touch the diff first.
    if len(sys.argv) > 1:
        diff_text = Path(sys.argv[1]).read_bytes()
    else:
        stdin_buffer = getattr(sys.stdin, "buffer", None)
        diff_text = stdin_buffer.read() if stdin_buffer is not None else sys.stdin.read()

    violations = scan_diff_for_new_adrs(diff_text)
    if not violations:
        return 0

    print("", file=sys.stderr)
    print(
        "ADR scoping-lint violation(s) — push refused (the operator TG 7590 / T-2806):",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for msg in violations:
        print(f"  {msg}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "  Add a build_disposition: no-build-needed/built, or a "
        "scoping_ticket: T-NNNN, to the new ADR's frontmatter.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)

    return 1


if __name__ == "__main__":
    sys.exit(main())
