"""YAML block scalars (`|` / `>`) in Pantheon frontmatter -- one shared reader (T-5689).

Pure leaf module (stdlib `re` only, imports nothing from tools/), so both
tools/ticket.py and tools/ticket_purpose.py (which is deliberately independent
of tools.ticket) can import it without an import cycle, and so can the lenient
line readers (weekly_ticket_summary, spine_index_groom, ...).

Why it exists: on 2026-10-27 T-5686 carried `threat_path: |`, and
ticket_purpose's parser (which special-cased only `evidence_snapshot`) raised on
it, which broke load_ticket_purpose_index and with it `ticket.py file` /
`purpose`, the staged-content gate and the lint digest. The rule here applies to
ANY key, so no future block scalar can stop ticket filing.

Supported: a header value of `|` or `>`, optionally with a chomping indicator
(`-` strip, `+` keep, default clip) and/or an explicit indentation indicator
(`1`-`9`), in either order (`|2-`, `|-2`). The content is the following lines
until the first non-blank line indented less than the block indent: the explicit
indicator if given, else the indent of the first non-blank content line (YAML
auto-detection). Blank (whitespace-only) lines inside the block are empty lines.
Exception (T-5689 MED-1): ticket.py's writer-owned BLOCK_SCALAR_FIELDS with no
explicit indicator always use the fixed 2-space indent their pre-T-5689 writer and
reader used, so old-writer blocks with leading-space content read back unchanged.
`|` keeps line breaks; `>` folds them per YAML (a single break between two
ordinary lines becomes a space, more-indented lines keep their breaks).

Not general YAML: no nested mappings, no tabs as indentation, no comments.
"""

from __future__ import annotations

import re

# ticket.py's writer-owned block fields (T-4707 / T-5678); ticket.BLOCK_SCALAR_FIELDS
# IS this tuple. Their pre-T-5689 writer always used a fixed 2-space indent with a
# bare `|`, see `consume_block`'s legacy fallback.
BLOCK_SCALAR_FIELDS = (
    "evidence_snapshot",
    # T-5678: free-text gate fields (may hold colons, commas, quotes)
    "threat_path", "threat_justification", "not_security_reason",
)
LEGACY_FIXED_INDENT = 2

BLOCK_HEADER_RE = re.compile(r"^([|>])(?:([1-9])([+-])?|([+-])([1-9])?)?$")


def parse_block_header(raw: str) -> tuple[str, str, int | None] | None:
    """Return (style, chomp, explicit_indent) if `raw` (a frontmatter value with
    surrounding whitespace already stripped) is a block-scalar header, else None.
    style is "|" or ">"; chomp is "", "-" or "+"."""
    m = BLOCK_HEADER_RE.match(raw)
    if m is None:
        return None
    style = m.group(1)
    if m.group(2) is not None:
        return style, m.group(3) or "", int(m.group(2))
    indent = int(m.group(5)) if m.group(5) is not None else None
    return style, m.group(4) or "", indent


def is_continuation_line(line: str) -> bool:
    """True iff `line` is indented (starts with a space or tab), so it can never
    be a top-level `key: value` line. The guard the lenient readers apply."""
    return line[:1] in (" ", "\t")


def _leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _legacy_indent(detected: int) -> int:
    """T-5689 MED-1: the pre-T-5689 ticket.py writer rendered a BLOCK_SCALAR_FIELDS
    value with a bare `|` and a FIXED 2-space indent, and its reader stripped
    exactly 2. So a value whose first line starts with spaces ("  a\\nb\\n") was
    written as "    a" / "  b": YAML auto-detection would read indent 4, end the
    block at "  b", and that line would then fail to parse as a key (the purpose
    index breaks again). And a value whose EVERY line starts with spaces would
    parse cleanly but lose them. For these writer-owned fields with no explicit
    indicator, any detected indent >= 2 therefore means the fixed 2 -- exactly
    the old reader. (A 1-space block, which the old reader rejected, keeps 1.)"""
    return LEGACY_FIXED_INDENT if detected >= LEGACY_FIXED_INDENT else detected


def consume_block(
    lines: list[str], idx: int, header: str, *, key: str | None = None
) -> tuple[str | None, int]:
    """Read the block scalar whose header value is `header`, starting at
    `lines[idx]` (the line AFTER the `key: |` line). `key` names the field:
    for a BLOCK_SCALAR_FIELDS key with no explicit indentation indicator, the
    legacy fixed 2-space indent applies (`_legacy_indent`); every other key
    gets plain YAML indentation auto-detection.

    Returns (value, next_idx). `value` is None when the block has no content
    line at all (the next non-blank line is not indented, or the frontmatter
    ends); the caller then decides the back-compat value (ticket.py keeps a bare
    `serves: |` as the plain scalar "|"). In that case `next_idx` is `idx`, so
    nothing is consumed. Raises ValueError if `header` is not a block header.
    """
    parsed = parse_block_header(header)
    if parsed is None:
        raise ValueError(f"not a block-scalar header: {header!r}")
    style, chomp, indent = parsed
    if indent is None:
        for nxt in lines[idx:]:
            if nxt.strip():
                indent = _leading_spaces(nxt)
                break
        if not indent:  # no non-blank line, or it is not indented
            return None, idx
        if key in BLOCK_SCALAR_FIELDS:
            indent = _legacy_indent(indent)
    content: list[str] = []
    j = idx
    while j < len(lines):
        nxt = lines[j]
        if not nxt.strip():
            content.append("")
        elif _leading_spaces(nxt) >= indent:
            content.append(nxt[indent:])
        else:
            break
        j += 1
    trailing = 0
    while content and content[-1] == "":
        content.pop()
        trailing += 1
    if not content:
        return None, idx
    if style == "|":
        body = "\n".join(content)
    else:
        body = _fold(content)
    if chomp == "-":
        value = body
    elif chomp == "+":
        value = body + "\n" + "\n" * trailing
    else:
        value = body + "\n"
    return value, j


def _fold(content: list[str]) -> str:
    """YAML folding of a `>` block's content lines (indent already stripped,
    trailing empty lines already removed, at least one non-empty line)."""
    out: list[str] = []
    prev_text: str | None = None  # last non-empty line seen
    empties = 0
    for line in content:
        if line == "":
            empties += 1
            continue
        if prev_text is None:
            out.append("\n" * empties)  # leading empty lines are kept as breaks
        else:
            more = line[:1] in (" ", "\t") or prev_text[:1] in (" ", "\t")
            if empties == 0:
                out.append("\n" if more else " ")
            else:
                out.append("\n" * (empties + 1 if more else empties))
        out.append(line)
        prev_text = line
        empties = 0
    return "".join(out)


def plain_scalar_safe(value: str) -> bool:
    """True iff `value` can be written as an unquoted single-line `key: value`
    and every frontmatter reader (old and new ticket.parse_front_matter and
    ticket_purpose, which take the text after `key:` verbatim, stripped) reads
    back exactly `value`. Pantheon's readers do NOT unquote, so quoting is not an
    option: anything else (a newline, surrounding whitespace, empty/`null`, a
    `[...]` flow list, a block header like `|`) must stay a `|` block."""
    return (
        bool(value)
        # The parsers split with str.splitlines(), which also breaks on \x0b \x0c
        # \x1c-\x1e \x85     (r2 HIGH); this subsumes "\n" and "\r".
        and value.splitlines() == [value]
        and value == value.strip()
        and value.lower() != "null"
        and not (value.startswith("[") and value.endswith("]"))
        and parse_block_header(value) is None
    )


def render_block(value: str, *, always_clip: bool = False) -> str:
    """Render a string as a `|` block (header + 2-space-indented content lines,
    joined by newlines, no trailing newline).

    Default: choose the chomping indicator that reads back to exactly `value`.
    `always_clip=True` is ticket.py's pre-T-5689 BLOCK_SCALAR_FIELDS format
    (always a bare `|`, trailing newlines dropped -- reads back with exactly
    one), byte-identical to the old writer in every case, so a seat still on
    old code reads it; those keys read back with the fixed 2-space indent.
    Otherwise an explicit indentation indicator (`|2`) is added when the first
    non-blank content line itself starts with a space, since auto-detection
    would swallow its leading spaces."""
    stripped = value.rstrip("\n")
    trailing = len(value) - len(stripped)
    if always_clip or trailing == 1:
        chomp = ""
        trailing = min(trailing, 1)
    elif trailing == 0:
        chomp = "-"
    else:
        chomp = "+"
    body_lines = stripped.split("\n")
    first = next((ln for ln in body_lines if ln.strip()), "")
    indicator = "2" if first[:1] == " " and not always_clip else ""
    rendered = "\n".join(("  " + ln) if ln else "" for ln in body_lines)
    if chomp == "+":
        # Kept trailing empty lines are written indent-only ("  "), not bare:
        # a bare empty last line before the closing `---` is dropped by the
        # frontmatter slice + splitlines, which would lose one newline.
        rendered += "\n  " * (trailing - 1)
    return f"|{indicator}{chomp}\n{rendered}"
