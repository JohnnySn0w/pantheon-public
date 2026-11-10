"""Neutralize Claude Code @-mention file expansion in untrusted text (T-4382).

Claude Code expands an ``@<path>`` file mention anywhere in a submitted composer
message, before the model runs and with no tool call: the file arrives as a
``file`` attachment, so the load never meets a permission prompt, a PreToolUse
hook or worker_guard (T-4382 STEP 1, proven 2026-11-02 on v2.1.316). Fleet code
that pastes attacker-influenceable text (ticket titles and Asks from automated
filers, mailbox senders, subjects and payloads) into a live seat must therefore
neutralize those tokens in exactly the untrusted fields it interpolates, and
never in the trusted fixed text around them.

Two steps, in this order:

1. Delete every character the TUI would strip from pasted text before it looks
   for mentions (line breaks it converts are mapped to LF the same way). Claude Code removes "invisible characters from the pasted text"
   first, so an invisible character next to an '@' would otherwise hide a
   boundary from step 2 and then vanish, leaving a live mention (r1 review,
   live-confirmed: 'x <U+200B>@/path' expanded). Only fields that contain an
   '@' are touched, since a field with no '@' cannot hold a mention.
2. Escape every token-initial '@' with a backslash: ``@/path`` becomes
   ``\\@/path``. The T-4382 probe found that ``\\@/path`` does not expand, while a
   zero-width space after the '@' still did, so no invisible character is used.
   ASCII-only and readable; the receiving model still sees the intended text.

Stdlib only, never raises; a non-str input comes back unchanged (callers that may
hold a non-str field pass ``str(x)``).
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["neutralize_at_mentions"]

# --------------------------------------------------------------------------- step 1
# The paste-strip predicate in the Claude Code v2.1.316 bundle (function N, used by
# the paste sanitizer Ele that reports "Removed N invisible characters from the
# pasted text"), transcribed as code-point ranges:
#   <160:   C0 controls except TAB and LF; DEL and C1 (127..159)
#   <8192:  U+00AD U+034F U+061C U+115F U+1160 U+17B4 U+17B5 U+180B..U+180F
#   <65536: U+200B..U+200F U+2028..U+202E U+2060..U+206F U+3164 U+FE00..U+FE0F
#           U+FEFF U+FFA0 U+FFF0..U+FFFB
#   else:   U+1107F U+13430..U+1343F U+16FE4 U+1BCA0..U+1BCA3 U+1D173..U+1D17A
#           U+E0000..U+E0FFF (tags, variation selectors supplement)
# Line breaks are not deleted but MAPPED, as the TUI's line-break branch (function z
# in Ele) does: CRLF becomes one LF, and a lone CR, VT, FF, NEL, U+2028 or U+2029
# becomes LF. LF is JavaScript whitespace, so step 2 sees the same boundary the TUI's
# extractor will, and the text keeps its line structure. They are mapped before the
# deletion pass, so their entries in the table below never fire on mapped text.
_BUNDLE_STRIP_RANGES = (
    (0x00, 0x08), (0x0B, 0x1F), (0x7F, 0x9F),
    (0xAD, 0xAD), (0x34F, 0x34F), (0x61C, 0x61C), (0x115F, 0x1160),
    (0x17B4, 0x17B5), (0x180B, 0x180F),
    (0x200B, 0x200F), (0x2028, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
    (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFFB),
    (0x1107F, 0x1107F), (0x13430, 0x1343F), (0x16FE4, 0x16FE4),
    (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)
# Deliberately wider than the bundle, so a Claude Code upgrade that widens its set
# (other bundle paths use [\p{Cf}\p{Co}\p{Cn}\p{Cs}\p{Default_Ignorable_Code_Point}
# \u2800]) does not reopen the hole: the braille blank U+2800 plus every code point
# in categories Cc (bar TAB and LF), Cf, Co, Cn and Cs. Deleting more is always safe:
# step 2 then judges the text exactly as the TUI will receive it.
_EXTRA_STRIP = frozenset({0x2800})
_STRIP_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cn", "Cs"})
_KEEP = frozenset({0x09, 0x0A})


def _is_tui_invisible(cp: int) -> bool:
    if cp in _KEEP:
        return False
    if cp in _EXTRA_STRIP:
        return True
    for lo, hi in _BUNDLE_STRIP_RANGES:
        if lo <= cp <= hi:
            return True
    return unicodedata.category(chr(cp)) in _STRIP_CATEGORIES


_LINE_BREAKS = ("\r", "\v", "\f", "\x85", "\u2028", "\u2029")


def _map_line_breaks(text: str) -> str:
    text = text.replace("\r\n", "\n")
    for c in _LINE_BREAKS:
        text = text.replace(c, "\n")
    return text


def _strip_tui_invisible(text: str) -> str:
    if text.isascii() and text.isprintable():
        return text
    text = _map_line_breaks(text)
    return "".join(c for c in text if not _is_tui_invisible(ord(c)))


# --------------------------------------------------------------------------- step 2
# The extractor (Claude Code v2.1.316, read from the installed bundle) splits the
# message into tokens on JavaScript \s, then takes an '@' that starts a token OR
# follows one of U+3002 U+3001 U+FF1F U+FF01 (ideographic full stop and comma,
# fullwidth ? and !); the quoted form '@"a path"' uses the same boundary class.
# Both classes below are exactly JavaScript's \s (not Python's, which differs).
_JS_WS = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_BOUNDARY = _JS_WS + "\u3002\u3001\uff1f\uff01"
# Token-initial '@' (string start, or after a boundary) followed by a non-separator:
# the extractor keeps '@' + the rest of its token, so '@\u3002x' is still a mention.
#
# Only the FIRST '@' of a run is token-initial: in '@@x' the second '@' follows an
# '@', which is not a boundary, so it cannot start a mention once the first is
# escaped. That yields '\@@x' (one backslash, the run otherwise intact).
#
# Idempotence: in an already-escaped '\@' the '@' follows a backslash, not a
# boundary or the string start, so it never matches again; and step 1 leaves
# nothing for a second pass to map or delete.
#
# Field start counts as token-initial even where the caller's fixed text puts a
# non-space before the field (INJECT_PROMPT wraps the title in "('...')"): the
# field is escaped on its own, so the result does not depend on that context.
_TOKEN_INITIAL_AT = re.compile(f"(?:^|(?<=[{_BOUNDARY}]))@(?=[^{_JS_WS}])")


def neutralize_at_mentions(text: str) -> str:
    """Return ``text`` with Claude Code @-mentions neutralized.

    If ``text`` contains an ``@``: first map line breaks to LF and delete every
    character the TUI strips from pasted text (see the module docstring), then
    escape every token-initial ``@`` (at the start, or after JavaScript whitespace
    or one of the CJK/fullwidth boundary marks, and followed by a non-whitespace
    character) as ``\\@``.

    Left unchanged: a field with no ``@``; mid-word ``@`` (``a@b.com``, ``x=@y``),
    which does not expand; a bare ``@`` followed by whitespace or ending the
    string, which names nothing; an already-escaped ``\\@`` (idempotent). For a
    run such as ``@@x`` only the token-initial ``@`` is escaped (``\\@@x``).

    Never raises. A non-str input is returned unchanged.
    """
    if not isinstance(text, str):
        return text
    try:
        if "@" not in text:
            return text
        return _TOKEN_INITIAL_AT.sub(r"\\@", _strip_tui_invisible(text))
    except Exception:  # noqa: BLE001 - a sanitizer must never cost a delivery
        # Fail closed: escape every '@' (over-escapes mid-word ones, never misses).
        return text.replace("@", "\\@")
