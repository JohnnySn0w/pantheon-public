"""Shared markdown-table cell/row rendering with pipe-escaping (T-1936).

A LEAF module: depends only on `re`, so it is safe to import from anywhere —
including the commit path (`ticket.py`) and low-level runners — without dragging
in a groomer's dependency surface. This is the single, shared implementation of
table-cell escaping for the whole repo: `spine_index_groom` (which first grew
`_md_cell`/`_md_row` under T-1929) and every raw-f-string table emitter route
through it, so a generated table cannot silently break its own column structure
when a cell value contains a `|`.

Why one implementation and not per-emitter copies: a second escaper drifts, and
drift here re-opens the exact malformed-index class T-1929 closed (see
lessons/2026-07-22-false-red-verifier-must-call-prod-parser.md — the same
"share the choke point, don't duplicate the logic" reason). `grep -rn
"def _md_cell" tools/` must return exactly one hit: this file.
"""

from __future__ import annotations

import re

#: Matches a pipe NOT already backslash-escaped. Makes _md_cell idempotent.
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def _md_cell(value: object) -> str:
    """Escape a value for use as one markdown table cell.

    Idempotent by construction: only *unescaped* pipes are escaped, so an
    author who already wrote `\\|` in their frontmatter keeps exactly one
    escape rather than accumulating `\\\\|` (which renders as a literal
    backslash followed by a live cell break — i.e. still broken). Escaping
    twice equals escaping once, which keeps the choke point safe if escaping
    is ever applied at more than one layer.
    """
    return _UNESCAPED_PIPE.sub(r"\\|", str(value))


def _md_row(*cells: object) -> str:
    """Render cells as one markdown table row, escaping each cell's pipes.

    Byte-identical to the hand-written `f"| {a} | {b} | {c} |"` form for any
    pipe-free cells: `"| " + " | ".join(map(_md_cell, cells)) + " |"` expands to
    the same string. That equivalence is what lets emitters adopt this with a
    provable zero-diff on existing (pipe-free) content — the change only bites
    when a cell actually contains a `|`.
    """
    return "| " + " | ".join(_md_cell(c) for c in cells) + " |"
