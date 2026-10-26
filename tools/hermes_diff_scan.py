"""Scan git unified-diff output for byte-sequence discipline violations.

Used by `.github/workflows/hermes-audit.yml` to enforce the HERMES byte-sequence
discipline on the server side. Pairs with `.githooks/pre-commit` (local) and
`tools/watch_runner.py` (auto-commit path) to form the three-layer prevention
stack documented in `incidents/2026-05-02-hermes-contamination/README.md`.

Public surface:
    Violation        — dataclass: path + lineno of a detected violation.
    AddedLine        — dataclass: one added line (or one piece of an over-cap
                       line): path, lineno, text, is_new_file, continued.
    iter_diff_events — the shared structural framing (T-4202): parse
                       unified-diff lines lazily; yield an AddedLine per added
                       line, over-cap lines as pieces, in bounded memory. Used
                       by adr_scoping_lint, ledger_placement_lint and
                       mutation_gate (through iter_added_lines) and by
                       scan_diff_stream below.
    iter_added_lines — iter_diff_events with over-cap pieces joined back into
                       whole lines (for consumers of small text files).
    scan_diff_stream — parse unified-diff LINES lazily (any iterable of bytes
                       lines, e.g. `sys.stdin.buffer`); yield Violations one at
                       a time in bounded memory. A thin filter over
                       iter_diff_events.
    scan_diff_lines  — parse unified-diff text (str or bytes) in one call;
                       return list[Violation]. Thin wrapper over the stream core.
    format_annotation — render a GitHub Actions `::error` annotation string.
    main             — CLI entry point; reads diff from stdin or file path arg;
                       exits 0 (clean) or 1 (violations found).

Accepted input (both are `git` output; nothing else is promised):
    git diff --text A B  /  git diff --text A...B
    git log -p --diff-merges=separate --text --no-textconv --no-ext-diff
            --format= --no-color RANGE     (per-commit form; one diff block per
                                            commit, per parent for merges)
    git show <sha> -p --text --diff-merges=remerge --no-textconv --no-ext-diff
                                           (history audit; per-parent
                                            `separate` for octopus merges)

    Producers MUST pass `--text` (T-4211). Without it git prints only `Binary
    files ... differ` for a file with a NUL byte in its first 8000 bytes or one
    marked `binary` / `-diff` in .gitattributes, and its content never reaches
    this scanner. Under `--text` such a file's content arrives as ordinary
    `+` lines, possibly one newline-free line the size of the file, which is
    why input is read in capped segments (see Streaming below).

How the parser frames a patch (T-4185):

    A diff line's first byte says what it is ONLY once you know whether you are
    inside a hunk. An added line whose content begins with two plus signs and a
    space renders as a line beginning with three plus signs and a space — byte
    for byte a file header — and a removed line whose content begins with two
    dashes and a space renders as a `---` line. So headers are never recognised
    by prefix alone. The parser tracks hunk state from the `@@ -a,b +c,d @@`
    counts, exactly as git and `patch` do:

      * INSIDE a hunk (old/new counts not yet exhausted) every line is content
        by its first byte: `+` added, `-` removed, space/empty context, `\\`
        the no-newline marker. Nothing inside a hunk is ever a header.
      * OUTSIDE a hunk (header position) a `+++ ` line is a file header only
        when it directly follows a `--- ` line; `diff ` starts a new file; `@@`
        starts a hunk. A `+`-prefixed line anywhere else is scanned as an added
        line (fail-closed): a wrong or truncated count can only cause extra
        scanning, never a skipped line. Added lines with no known file are
        reported under path `<unknown>` rather than dropped.

    Input is split on the newline byte ONLY, working in bytes. `str.splitlines()`
    and text-mode stdin also break on a lone carriage return, form feed, NEL,
    U+2028 and friends, which are ordinary content to git; splitting there let a
    trigger hide in the tail of an added line.

    Known non-goals: combined diffs (`git show <merge>` / `--cc`, hunks marked
    `@@@`) are not hunk-tracked, though their `+`-prefixed lines are still
    scanned fail-closed; commit messages are not diff lines (the log form
    suppresses messages via `--format=`). No in-repo consumer reads combined
    diffs: every producer above emits two-endpoint hunks (`separate` and
    `remerge` both diff against one tree), and the lints read two-endpoint
    `git diff` (T-4202). A
    c-quoted header path (`+++ "b/caf\\303\\251.txt"`) is unquoted to the real
    path since T-4202 (it was reported as git printed it before).

Streaming (T-4187): `main()` feeds `sys.stdin.buffer` (or the file argument,
opened `rb`) straight into `scan_diff_stream`; only the violations found so far
are retained, so peak memory does not grow with the input size (a 204 MB
full-history stream previously peaked at ~1.8 GB). Since T-4211 it does not grow
with line length either: a file-like input is read with `readline(_LINE_CAP)`,
so an over-cap line arrives in segments; every segment of an added line is
searched, with a `_CHUNK_OVERLAP`-byte carry across each boundary. An oversized
line is never skipped (fail-closed).

Source-cleanliness convention:
    The trigger byte sequence is never written contiguously in this file.
    `_HERMES_PATTERN` is imported from `tools._git_utils`, where the same
    convention applies. This protects `git log -p` of pantheon-config from
    re-introducing the exact byte sequence per the standing rule in
    `/home/claude/CLAUDE.md`.

    Force-push edge case: when `github.event.before` is a valid SHA but the
    push rewound history, `git diff <before> HEAD` may include context lines
    for commits being abandoned. Because this scanner only flags lines with a
    `+` prefix (added lines), removed / context lines are correctly ignored.
    The scanner is therefore safe on force-pushes without special handling.

Verification: `grep -cF` for the unescaped trigger against this file must
return 0.
"""

from __future__ import annotations

import io
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, List

# ---------------------------------------------------------------------------
# Bootstrap: ensure repo root is on sys.path so tools._git_utils is importable
# regardless of cwd (mirrors the approach in .githooks/pre-commit).
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from tools._git_utils import _HERMES_PATTERN  # noqa: E402

# Matches hunk headers: `@@ -l[,s] +l[,s] @@`. Groups: 1 old start, 2 old count,
# 3 new start, 4 new count. A missing count means 1 (git omits ",1").
_HUNK_HEADER = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# Path reported for an added line that cannot be attributed to a file (no header
# seen, or `+++ /dev/null`). Never expected from git output; reported, not
# dropped, so an odd shape can only over-report.
_UNKNOWN_PATH = "<unknown>"

# First-byte constants (iterating / indexing `bytes` yields ints).
_PLUS, _MINUS, _SPACE, _NL = 0x2B, 0x2D, 0x20, 0x0A
_BACKSLASH, _AT, _LOWER_D, _LOWER_N = 0x5C, 0x40, 0x64, 0x6E


# Per-line read cap (T-4211). A file-like input is read with `readline(cap)`,
# so no single read ever materialises more than this many bytes; a longer line
# arrives as several segments that are ALL searched (never skipped). 4 MiB sits
# far above every line git renders as binary in pantheon-config history (the
# longest is ~11 KB; images carry newline bytes by chance) and above all but
# one text line (a 12.3 MB single-line HTML export), so the segmented path is
# the rare case, while peak memory stays O(cap) even for a newline-free file at
# GitHub's 100 MB per-file limit.
_LINE_CAP = 4 * 1024 * 1024

# Bytes carried from the end of one segment into the search of the next, so a
# trigger straddling a segment boundary is still matched. Must be at least
# (trigger length - 1); the trigger is 9 bytes, 64 leaves headroom should the
# pattern ever grow. Pinned by a test against the live pattern.
_CHUNK_OVERLAP = 64


def _segments(lines, cap: int) -> Iterator["tuple[bytes, bool]"]:
    """Yield (segment, continued) pairs.

    For a file-like input (anything with `readline`, e.g. `sys.stdin.buffer`,
    an `rb` file, `io.BytesIO`) each read is `readline(cap)`: at most `cap`
    bytes, so memory is bounded even for a newline-free line of any size. A
    segment that neither ends in b"\\n" nor is the last one is continued by the
    next, which is yielded with `continued=True`.

    Any other iterable is taken as already-split lines (the caller holds them
    in memory anyway); each item is one whole line, `continued=False`.
    """
    readline = getattr(lines, "readline", None)
    if readline is None:
        for line in lines:
            yield line, False
        return
    continued = False
    while True:
        seg = readline(cap)
        if not seg:
            return
        yield seg, continued
        continued = not seg.endswith(b"\n")


@dataclass
class Violation:
    """A single detected byte-sequence discipline violation in a diff."""
    path: str
    lineno: int


# C-escape table for git's quoted path form (`core.quotePath`); octal `\NNN`
# escapes are handled separately.
_C_ESCAPES = {
    ord("a"): 0x07, ord("b"): 0x08, ord("t"): 0x09, ord("n"): 0x0A,
    ord("v"): 0x0B, ord("f"): 0x0C, ord("r"): 0x0D, ord('"'): 0x22,
    ord("\\"): 0x5C,
}


def _unquote_c_path(raw: bytes) -> bytes:
    """Undo git's C-style quoting of a path: `"b/caf\\303\\251.txt"` ->
    `b/café.txt` (as UTF-8 bytes). `raw` must include the surrounding double
    quotes. An unknown escape is kept verbatim (backslash + byte)."""
    out = bytearray()
    i, n = 1, len(raw) - 1  # skip the surrounding quotes
    while i < n:
        b = raw[i]
        if b != 0x5C or i + 1 >= n:
            out.append(b)
            i += 1
            continue
        nxt = raw[i + 1]
        if 0x30 <= nxt <= 0x37:
            j = i + 1
            while j < n and j < i + 4 and 0x30 <= raw[j] <= 0x37:
                j += 1
            out.append(int(raw[i + 1:j], 8) & 0xFF)
            i = j
        elif nxt in _C_ESCAPES:
            out.append(_C_ESCAPES[nxt])
            i += 2
        else:
            out += raw[i:i + 2]
            i += 2
    return bytes(out)


def _header_path(line: bytes) -> str | None:
    """Extract the path from a `+++ ` file-header line.

    Strips the line terminator, git's single trailing tab (it marks names that
    contain spaces), undoes git's C-quoting (`+++ "b/caf\\303\\251.txt"`, used
    for non-ASCII and control bytes; T-4202), and strips the `b/` prefix.
    Returns None for `/dev/null` (deleted file: nothing is added) and for an
    empty path.
    """
    raw = line[4:]
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    if raw.endswith(b"\t"):
        raw = raw[:-1]
    if len(raw) >= 2 and raw.startswith(b'"') and raw.endswith(b'"'):
        raw = _unquote_c_path(raw)
    if raw.startswith(b"b/"):
        raw = raw[2:]
    path = raw.decode("utf-8", "replace")
    if not path or path == "/dev/null":
        return None
    return path


def _content(line: bytes, start: int) -> bytes:
    """`line[start:]` minus one trailing b"\\n" (only the newline: a `\\r`,
    form feed, U+2028 etc. is content)."""
    return line[start:-1] if line.endswith(b"\n") else line[start:]


@dataclass
class AddedLine:
    """One added (`+`) diff line, or one PIECE of an oversized one (T-4202).

    path:        the file it belongs to, from the `+++ ` header (C-unquoted,
                 `b/` stripped); None when no file is known (no header seen,
                 or `+++ /dev/null`).
    lineno:      its line number in the new file (from the `@@` counts).
    text:        the content bytes: the leading `+` and the line's trailing
                 b"\\n" are stripped, nothing else.
    is_new_file: a `new file mode` line appeared in this file's header block
                 (after its `diff ` line, before its first hunk).
    continued:   False for the first (usually only) piece of a line; True for
                 each further piece of a line longer than the read cap. A
                 continued piece always belongs to the most recent
                 continued=False event and repeats its path/lineno.
    """
    path: "str | None"
    lineno: int
    text: bytes
    is_new_file: bool
    continued: bool = False


def iter_diff_events(lines: Iterable[bytes]) -> Iterator[AddedLine]:
    """Lazily parse unified-diff lines; yield an AddedLine per added line.

    The ONE structural diff framing shared by every in-repo diff-walking check
    (T-4185 framing, T-4211 cap, T-4202 extraction). See the module docstring
    for the parse contract.

    Piece semantics (T-4211 bounded memory): a file-like input is read with
    `readline(_LINE_CAP)`, so a line longer than the cap is yielded as
    several events: the first with continued=False, the rest with
    continued=True. Nothing is buffered, so memory stays O(cap) whatever the
    line length. Consumers that want whole lines and only ever see small
    files use `iter_added_lines`, which joins the pieces; a consumer that must
    stay bounded (the byte-sequence guard) handles pieces itself, as
    `scan_diff_stream` does. An already-split iterable (a list of lines)
    never produces continued pieces.

    Two-endpoint diffs only: every in-repo consumer reads `git diff A B` /
    `A...B`, `git log -p --diff-merges=separate` or `git show
    --diff-merges=remerge` output. Combined diffs (`@@@`, from a bare `git
    show <merge>` / `--cc`) are not hunk-tracked; their `+` lines still come
    out fail-closed (see the module docstring).
    """
    current_file: str | None = None
    is_new = False        # "new file mode" seen in this file's header block
    lineno = 0            # next new-file line number
    old_rem = 0           # hunk lines still owed on the old side
    new_rem = 0           # hunk lines still owed on the new side
    pending_plus = False  # previous header-position line was a `--- ` line
    in_added = False      # the line now being read in pieces is an added line
    added_path: "str | None" = None
    added_lineno = 0

    for line, continued in _segments(lines, _LINE_CAP):
        if continued:
            # Continuation of an oversized line: never re-classified, never
            # counted; passed through only if the line is an added line.
            if in_added:
                yield AddedLine(added_path, added_lineno, _content(line, 0),
                                is_new, True)
            continue
        in_added = False
        c = line[0] if line else _NL

        # ---- inside a hunk: every line is content, never a header ----------
        if old_rem or new_rem:
            if c == _PLUS:
                in_added, added_path, added_lineno = True, current_file, lineno
                yield AddedLine(current_file, lineno, _content(line, 1), is_new)
                lineno += 1
                if new_rem:
                    new_rem -= 1
                continue
            if c == _MINUS:
                if old_rem:
                    old_rem -= 1
                continue
            if c == _SPACE or c == _NL:  # context (git may strip a blank one)
                lineno += 1
                if old_rem:
                    old_rem -= 1
                if new_rem:
                    new_rem -= 1
                continue
            if c == _BACKSLASH:  # "\ No newline at end of file"
                continue
            # Not a legal hunk-line prefix: the declared counts were larger
            # than the hunk. End the hunk and treat this as a header-position
            # line.
            old_rem = new_rem = 0

        # ---- header position ------------------------------------------------
        was_pending, pending_plus = pending_plus, False
        if c == _PLUS:
            if was_pending and line.startswith(b"+++ "):
                current_file = _header_path(line)
                lineno = 0
                continue
            # A `+` line that is not a header here: an added line (fail-closed).
            in_added, added_path, added_lineno = True, current_file, lineno
            yield AddedLine(current_file, lineno, _content(line, 1), is_new)
            lineno += 1
        elif c == _MINUS:
            if line.startswith(b"--- "):
                pending_plus = True
        elif c == _SPACE:
            lineno += 1
        elif c == _AT:
            m = _HUNK_HEADER.match(line)
            if m:
                lineno = int(m.group(3))
                old_rem = int(m.group(2)) if m.group(2) is not None else 1
                new_rem = int(m.group(4)) if m.group(4) is not None else 1
        elif c == _LOWER_D:
            if line.startswith(b"diff "):
                current_file = None
                is_new = False
                lineno = 0
        elif c == _LOWER_N:
            if line.startswith(b"new file mode"):
                is_new = True
        # Anything else (index, other mode lines, rename/copy, similarity,
        # `Binary files ... differ`, the no-newline marker, blank separators)
        # is metadata — ignored.


def iter_added_lines(lines: "Iterable[bytes] | bytes") -> Iterator[AddedLine]:
    """Like `iter_diff_events`, but every event is a WHOLE added line: the
    pieces of an over-cap line are joined, so `continued` is always False.

    For consumers whose diffs are small text files (lints over ADRs, ledger
    JSONL, a Python source). Joining costs memory proportional to the longest
    line, which is why the bounded byte-sequence guard does not use it.
    `bytes` input is read through the same segmented path as the CLI.
    """
    if isinstance(lines, (bytes, bytearray)):
        lines = io.BytesIO(bytes(lines))
    held: "AddedLine | None" = None
    parts: "list[bytes]" = []
    for ev in iter_diff_events(lines):
        if ev.continued:
            parts.append(ev.text)  # a piece always follows its first piece
            continue
        if held is not None:
            if parts:
                held.text += b"".join(parts)
                parts = []
            yield held
        held = ev
    if held is not None:
        if parts:
            held.text += b"".join(parts)
        yield held


def scan_diff_stream(lines: Iterable[bytes]) -> Iterator[Violation]:
    """Lazily parse unified-diff lines; yield a Violation per added line that
    contains the trigger.

    Args:
        lines: an iterable of `bytes` lines, one per diff line. A single
            trailing b"\\n" on each line is tolerated (so `sys.stdin.buffer`,
            an open binary file or `io.BytesIO` can be passed directly).

    A thin filter over `iter_diff_events` (T-4202). The framing holds only a
    handful of scalars of state; nothing is buffered. A file-like input is
    read in segments of at most `_LINE_CAP` bytes (T-4211), so memory is
    bounded by the cap, not by the longest line: an over-cap line arrives as
    a first piece plus continued pieces, and every piece of an added line is
    searched with a `_CHUNK_OVERLAP`-byte carry (fail-closed; one violation
    per line). Only added lines are ever examined for the trigger; removed
    and context lines are skipped — the discipline applies to bytes being
    introduced, not removed.
    """
    search = _HERMES_PATTERN.search
    overlap = _CHUNK_OVERLAP
    # Oversized-line state (T-4211). `owed` is the (path, lineno) of the added
    # line currently being read in pieces that has NOT yet matched; None once
    # reported. `tail` holds the last `overlap` content bytes seen for that
    # line, so a trigger that straddles a piece boundary is still found. It
    # briefly holds the Violation found for the current piece, which is
    # yielded only after the piece is dropped (see below).
    owed: "tuple[str, int] | Violation | None" = None
    tail = b""

    for ev in iter_diff_events(lines):
        text = ev.text
        if ev.continued:
            if owed is not None:
                if search(tail + text[:overlap]) or search(text):
                    owed = Violation(*owed)
                elif len(text) >= overlap:
                    tail = text[-overlap:]
                else:
                    tail = (tail + text)[-overlap:]
        else:
            path = ev.path or _UNKNOWN_PATH
            if search(text):
                owed = Violation(path, ev.lineno)
            else:
                owed = (path, ev.lineno)
                tail = text[-overlap:]
        # Drop this piece BEFORE the generator reads the next one: `text` is a
        # copy of the raw segment (the `+` is sliced off), so holding it across
        # the next `readline(cap)` would keep three cap-sized buffers alive at
        # once instead of two (pinned by the tracemalloc test).
        del ev, text
        if isinstance(owed, Violation):
            yield owed
            owed = None


def _iter_text_lines(text: str) -> Iterator[bytes]:
    """Split a `str` on the newline character only (NOT str.splitlines) and
    yield UTF-8 bytes. (`bytes` input goes through the segmented file-like
    path instead; see `scan_diff_lines`.)

    A trailing newline does not produce a final empty line, matching the old
    `splitlines()` behaviour for well-formed input.
    """
    start, n = 0, len(text)
    while start < n:
        end = text.find("\n", start)
        if end == -1:
            end = n
        yield text[start:end].encode("utf-8", errors="replace")
        start = end + 1


def scan_diff_lines(diff_text: "str | bytes") -> List[Violation]:
    """Parse unified-diff text and return all violations on added lines.

    Convenience wrapper over `scan_diff_stream` for callers that already hold
    the whole diff in memory. See the module docstring for the parse contract.

    Args:
        diff_text: The full output of one of the producer forms in the
            module docstring; `str` or `bytes`.

    Returns:
        A list of Violation objects (path + lineno) in order of appearance.
        Empty list means the diff is clean.
    """
    if isinstance(diff_text, (bytes, bytearray)):
        # Same bounded, segmented read as the CLI (T-4211).
        return list(scan_diff_stream(io.BytesIO(diff_text)))
    return list(scan_diff_stream(_iter_text_lines(diff_text)))


def format_annotation(v: Violation) -> str:
    """Format a GitHub Actions workflow annotation for a violation.

    Uses the `::error file=...,line=...::` syntax so the violation appears
    inline in the PR diff view. Message wording mirrors the local pre-commit
    hook output for consistency.

    See: https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/workflow-commands-for-github-actions#setting-an-error-message
    """
    # Escape the period in the advice string so the advice text itself is clean
    # per the byte-sequence discipline (same convention as all other tooling).
    msg = (
        f"HERMES discipline violation — {v.path}:{v.lineno} — "
        "Escape the period (HERMES\\.md) per CLAUDE.md byte-sequence discipline."
    )
    return f"::error file={v.path},line={v.lineno}::{msg}"


def main() -> int:
    """CLI entry point.

    Usage:
        git diff --text <base> HEAD | python3 tools/hermes_diff_scan.py
        git log -p --diff-merges=separate --text --no-textconv --no-ext-diff
            --format= --no-color <range> | python3 tools/hermes_diff_scan.py
        python3 tools/hermes_diff_scan.py <diff-file>

    Exits 0 if no violations found, 1 if any violations found.
    Output (stderr):
        Annotations on stdout (captured by GitHub Actions runner for inline PR
        comments). Human-readable summary on stderr.

    Input is consumed as a byte stream, line by line (T-4187): memory does not
    grow with input size.
    """
    if len(sys.argv) > 1:
        with open(sys.argv[1], "rb") as fh:
            violations = list(scan_diff_stream(fh))
    else:
        violations = list(scan_diff_stream(sys.stdin.buffer))

    if not violations:
        return 0

    # Emit GitHub Actions annotations on stdout.
    for v in violations:
        print(format_annotation(v))

    # Emit human-readable summary on stderr (matches local hook style).
    print("", file=sys.stderr)
    print("HERMES discipline violation — push refused.", file=sys.stderr)
    print("", file=sys.stderr)
    for v in violations:
        print(f"  {v.path}:{v.lineno}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "  Escape the period (HERMES\\.md) per CLAUDE.md byte-sequence discipline.",
        file=sys.stderr,
    )
    print("  Fix the file(s), amend / re-push.", file=sys.stderr)
    print("", file=sys.stderr)

    return 1


if __name__ == "__main__":
    sys.exit(main())
