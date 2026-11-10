"""T-4382: neutralize_at_mentions, the shared '@<path>' file-mention neutralizer.

Claude Code expands an '@<path>' token in a submitted composer message before the
model runs and without a tool call. The helper deletes the characters the TUI
strips from pasted text, then escapes every token-initial '@' as '\\@' (which the
T-4382 probe showed does not expand).

The adversarial tests run the helper's output through a Python port of Claude
Code v2.1.316's paste strip and mention extractors (read from the installed
bundle; the r1 reviewer's extractor.js logic), so a pass means "the TUI would find
no mention", not "the helper did what it says". Pure-function tests only: no
pane, no seat home, no /tmp read, no node.
"""
from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from inject_sanitize import neutralize_at_mentions as n  # noqa: E402

# --------------------------------------------------------------------------- port
# JavaScript's \s, exactly (Python's \s differs: it adds U+001C..U+001F and U+0085
# and lacks U+FEFF).
JS_WS = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
RAE = "\u3002\u3001\uff1f\uff01"
GQ = f"[{JS_WS}{RAE}]"


def bundle_invisible(cp: int) -> bool:
    """Port of the bundle's paste-strip predicate N (used by Ele)."""
    e = cp
    if e < 160:
        return (e < 32 and e != 9 and e != 10) or e >= 127
    if e < 8192:
        return e in (173, 847, 1564, 4447, 4448, 6068, 6069) or 6155 <= e <= 6159
    if e < 65536:
        return (8203 <= e <= 8207 or 8232 <= e <= 8238 or 8288 <= e <= 8303
                or e == 12644 or 65024 <= e <= 65039 or e == 65279 or e == 65440
                or 65520 <= e <= 65531)
    return (e == 69759 or 78896 <= e <= 78911 or e == 94180 or 113824 <= e <= 113827
            or 119155 <= e <= 119162 or 917504 <= e <= 921599)


def tui_paste_delete(text: str) -> str:
    """TUI model A: every N-invisible character is deleted."""
    return "".join(c for c in text if not bundle_invisible(ord(c)))


def tui_paste_newline(text: str) -> str:
    """TUI model B: CRLF/CR and the line separators become LF (Ele's line-break
    branch), VT/FF too, then the remaining N-invisible characters are deleted."""
    text = text.replace("\r\n", "\n")
    for c in ("\r", "\u2028", "\u2029", "\u0085", "\v", "\f"):
        text = text.replace(c, "\n")
    return tui_paste_delete(text)


def _rfn(s: str, i: int) -> bool:
    if i < 0 or i >= len(s):
        return False  # JS charCodeAt out of range is NaN: every comparison false
    c = ord(s[i])
    return 48 <= c <= 57 or 65 <= c <= 90 or c == 95 or 97 <= c <= 122


def _aan(e: str, mcp: bool) -> list:
    """Port of aAn: per JS-whitespace token, the first '@' at token start or after
    a RAE mark; with mcp, it needs a ':' at least two past the '@'."""
    out = []
    if "@" not in e:
        return out
    for m in re.finditer(f"[^{JS_WS}]+", e):
        s = m.group(0)
        h = s.find("@")
        while h > 0 and s[h - 1] not in RAE:
            h = s.find("@", h + 1)
        if h == -1:
            continue
        y = s.find(":", h + 2) if mcp else h
        if y == -1:
            continue
        end = len(s)
        while end >= y + 2 and _rfn(s, end - 1) == _rfn(s, end):
            end -= 1
        if end >= y + 2:
            out.append(s[h + 1:end])
    return out


def _xpr(e: str) -> list:
    quoted = [m.group(2) for m in re.finditer(f'(^|{GQ})@"([^"]+)"', e)
              if m.group(2) and not m.group(2).endswith(" (agent)")]
    return quoted + [x for x in _aan(e, False) if not x.startswith('"')]


def _lan(e: str) -> list:
    return [m.group(2) for m in re.finditer(f'(^|{GQ})@"([A-Za-z0-9_:.@-]+) \\(agent\\)"', e)]


def mentions(text: str) -> list:
    """Every file, MCP-resource and agent mention the TUI would extract."""
    return _xpr(text) + _aan(text, True) + _lan(text)


def tui_mentions(text: str) -> list:
    return mentions(tui_paste_delete(text)) + mentions(tui_paste_newline(text))


# --------------------------------------------------------------------------- corpus
# The r1 reviewer's strip_bypass list, plus one representative per class the
# bundle strips: bidi, zero-width, word joiner, tags, variation selectors (BMP and
# supplement), braille blank, Hangul fillers, C0/C1, soft hyphen, CGJ, ALM, Khmer
# and Mongolian format chars, Egyptian format controls, shorthand formats,
# musical formatting, noncharacter/unassigned, private use, lone surrogate.
INVISIBLES = (
    0x200B, 0x200E, 0x200F, 0x2060, 0x202A, 0x202E, 0x2066, 0x2069, 0xE0041,
    0xFE0F, 0x2800, 0x01, 0x7F, 0x85, 0x1C, 0xAD,
    0x200C, 0x200D, 0x2061, 0x206F, 0xFEFF, 0xFE00, 0xE0100, 0xE01EF, 0xE0000,
    0xE007F, 0x3164, 0x115F, 0x1160, 0xFFA0, 0x034F, 0x061C, 0x17B4, 0x180B,
    0x180E, 0x13430, 0x1BCA0, 0x1D173, 0x1107F, 0x110BD, 0x16FE4, 0xFFF0, 0xFFFB,
    0x0B, 0x0C, 0x0D, 0x9F, 0x2028, 0x2029, 0xE000, 0xF0000, 0x0378, 0xFFFF,
    0xD800,
)
PATHS = ("/etc/hosts", "~/x", "./a", "a.txt", '"/a b/c"', "srv:res", "@/etc/hosts")
SEPS = (" ", "\t", "\n", "\u00a0", "\u3000", "\u2003", "\u3002", "\u3001", "\uff1f", "\uff01")


def invisible_corpus():
    for cp in INVISIBLES:
        c = chr(cp)
        for p in PATHS:
            yield f"see {c}@{p} end"            # before the '@'
            yield f"see @{c}{p} end"            # between '@' and the path
            yield f"see {c * 3}@{c * 2}{p} end"  # runs on both sides
            yield f"{c}@{p}"                    # field start
            yield f"@{c}{c}{p}"
            yield f"see{c}@{p}"                 # invisible as the only separator
            yield f"x \u200b{c}\u2060@\ufe0f{c}{p}"  # mixed runs
            for s in SEPS:
                yield f"x{s}{c}@{p}"
                yield f"x{c}{s}{c}@{c}{p}"


def separator_corpus():
    for p in PATHS:
        yield "@" + p
        for s in SEPS + ("\r", "\v", "\f", "\u2028", "\u2029", "\ufeff", "\x1c", "\x85"):
            yield f"x{s}@{p}"
            yield f"{s}@{p}"
            yield f"x{s}{s}@{p}{s}y"
            yield f"x @{s}{p}"


# --------------------------------------------------------------------------- tests
class ExtractorPortSanityTest(unittest.TestCase):
    """The port must agree with the live probes, or the adversarial tests prove
    nothing."""

    def test_known_live_results(self):
        self.assertEqual(mentions("ref @/home/x/canary.txt end"), ["/home/x/canary.txt"])
        self.assertEqual(mentions("x=@/path"), [])                       # probe: no
        self.assertEqual(mentions("x \\@/path"), [])                     # probe: no
        self.assertEqual(mentions("x @ /path"), [])                      # probe: no
        self.assertEqual(mentions("x \uff20/path"), [])                  # fullwidth: no
        self.assertEqual(tui_mentions("x @\u200b/path"), ["/path", "/path"])   # probe: yes
        self.assertEqual(tui_mentions("x \u200b@/path"), ["/path", "/path"])   # r1: yes
        self.assertEqual(mentions("x\u3002@/p"), ["/p"])
        self.assertEqual(mentions('read @"a b"'), ["a b"])

    def test_raw_corpus_bites(self):
        """Without the helper, the invisible corpus is full of live mentions."""
        live = sum(1 for t in invisible_corpus() if tui_mentions(t))
        self.assertGreater(live, 1000)


class AdversarialCorpusTest(unittest.TestCase):
    def test_invisible_chars_before_between_and_in_runs(self):
        bad = [(t, n(t)) for t in invisible_corpus() if tui_mentions(n(t))]
        self.assertEqual(bad[:5], [])

    def test_separators(self):
        bad = [(t, n(t)) for t in separator_corpus() if tui_mentions(n(t))]
        self.assertEqual(bad[:5], [])

    def test_every_bmp_code_point_before_and_after_the_at(self):
        """Every BMP code point bar plain other-letters (CJK ideographs, Hangul
        syllables: ~45k, neither stripped nor a boundary), which keeps this fast.
        The Lo Hangul fillers the TUI strips are still covered."""
        import unicodedata
        bad = []
        for cp in range(0x10000):
            c = chr(cp)
            if unicodedata.category(c) == "Lo" and not bundle_invisible(cp):
                continue
            for t in (f"x{c}@/p", f"x {c}@/p", f"@{c}p", f"x @{c}/p"):
                if tui_mentions(n(t)):
                    bad.append((hex(cp), t))
        self.assertEqual(bad[:5], [])

    def test_supplementary_strip_ranges(self):
        bad = []
        for lo, hi in ((0xE0000, 0xE0FFF), (0x13430, 0x1343F), (0x1D173, 0x1D17A),
                       (0x1BCA0, 0x1BCA3), (0xF0000, 0xF0010), (0x10FFF0, 0x10FFFF)):
            for cp in range(lo, hi + 1):
                c = chr(cp)
                for t in (f"x {c}@/p", f"x @{c}/p"):
                    if tui_mentions(n(t)):
                        bad.append((hex(cp), t))
        self.assertEqual(bad[:5], [])

    def test_idempotent_on_the_corpus(self):
        for t in list(invisible_corpus())[::7] + list(separator_corpus()):
            self.assertEqual(n(n(t)), n(t), repr(t))


class PositionTest(unittest.TestCase):
    def test_start_of_string(self):
        self.assertEqual(n("@/etc/passwd"), "\\@/etc/passwd")

    def test_after_space(self):
        self.assertEqual(n("see @/etc/passwd now"), "see \\@/etc/passwd now")

    def test_after_tab(self):
        self.assertEqual(n("see\t@/etc/passwd"), "see\t\\@/etc/passwd")

    def test_after_newline(self):
        self.assertEqual(n("line one\n@/etc/passwd"), "line one\n\\@/etc/passwd")

    def test_after_unicode_space(self):
        self.assertEqual(n("a\u00a0@x"), "a\u00a0\\@x")   # NBSP is JS \s
        self.assertEqual(n("a\u3000@x"), "a\u3000\\@x")   # ideographic space

    def test_line_breaks_are_mapped_to_lf_like_the_tui(self):
        """r2: the TUI maps CRLF to one LF and a lone CR, VT, FF, NEL, U+2028 or
        U+2029 to LF; the helper does the same, so lines stay intact and LF is the
        boundary step 2 judges."""
        self.assertEqual(n("a\r\n@x"), "a\n\\@x")
        for brk in ("\r", "\v", "\f", "\x85", "\u2028", "\u2029"):
            with self.subTest(brk=hex(ord(brk))):
                self.assertEqual(n(f"a{brk}@x"), "a\n\\@x")
                self.assertEqual(n(f"a @{brk}/etc/passwd"), "a @\n/etc/passwd")
                self.assertEqual(tui_mentions(n(f"a @{brk}/etc/passwd")), [])
        self.assertEqual(n("one\r\ntwo\rthree @x"), "one\ntwo\nthree \\@x")

    def test_tui_stripped_controls_are_deleted_first(self):
        """U+001C and BOM are stripped by the TUI, so the helper deletes them and
        judges what remains."""
        self.assertEqual(n("a\x1c@x"), "a@x")      # joins: mid-word, cannot expand
        self.assertEqual(n("a\ufeff@x"), "a@x")
        self.assertEqual(n("x \U0001107f@/etc/hosts"), "x \\@/etc/hosts")   # r2 F1
        self.assertEqual(n("x \u200b@/path"), "x \\@/path")   # the r1 live bypass

    def test_after_cjk_boundary_marks(self):
        for mark in ("\u3002", "\u3001", "\uff1f", "\uff01"):
            with self.subTest(mark=hex(ord(mark))):
                self.assertEqual(n(f"x{mark}@/etc/passwd"), f"x{mark}\\@/etc/passwd")

    def test_every_token_in_a_field(self):
        self.assertEqual(n("@a and @b\n@c"), "\\@a and \\@b\n\\@c")


class UnchangedTest(unittest.TestCase):
    def test_mid_word(self):
        for s in ("a@b", "x=@y", "foo(@bar", "'@x", "path/@scope/pkg"):
            with self.subTest(s=s):
                self.assertEqual(n(s), s)

    def test_email(self):
        self.assertEqual(n("mail ops@example.com today"), "mail ops@example.com today")

    def test_lone_at_names_nothing(self):
        for s in ("@", "a @", "a @ b", "@\n", "x @\tb"):
            with self.subTest(s=s):
                self.assertEqual(n(s), s)

    def test_empty(self):
        self.assertEqual(n(""), "")

    def test_field_without_at_is_untouched_even_with_invisibles(self):
        """No '@', no mention: emoji ZWJ sequences and CRLF survive in such fields."""
        s = "family \U0001f468\u200d\U0001f469 line\r\nend \ufe0f"
        self.assertEqual(n(s), s)

    def test_non_str_is_returned_unchanged(self):
        for v in (None, 5, 1.5, b"@/etc/passwd", ["@x"], {"a": "@x"}):
            with self.subTest(v=v):
                self.assertIs(n(v), v)


class SupersetTest(unittest.TestCase):
    def test_helper_strip_set_is_a_superset_of_the_bundle_predicate(self):
        """r2 F1: one pass over every code point. Wherever the port of the bundle's
        N says the TUI strips it, the helper must treat it as invisible too (line
        breaks the TUI maps to LF count: the helper maps them before deleting)."""
        import inject_sanitize as mod
        mapped = {ord(c) for c in mod._LINE_BREAKS}
        missing = [hex(cp) for cp in range(0x110000)
                   if not 0xD800 <= cp <= 0xDFFF and bundle_invisible(cp)
                   and not mod._is_tui_invisible(cp) and cp not in mapped]
        self.assertEqual(missing, [])


class FailClosedTest(unittest.TestCase):
    def test_internal_error_escapes_every_at(self):
        """r2 F2: if a step raises, every '@' is escaped, so the output stays safe
        after the TUI strip (returning the input unchanged would not)."""
        from unittest import mock
        import inject_sanitize as mod
        with mock.patch.object(mod, "_strip_tui_invisible", side_effect=RuntimeError("boom")):
            got = n("x \u200b@/p and a@b")
        self.assertEqual(got, "x \u200b\\@/p and a\\@b")
        self.assertEqual(tui_mentions(got), [])


class IdempotenceTest(unittest.TestCase):
    def test_already_escaped_is_not_double_escaped(self):
        self.assertEqual(n("\\@/etc/passwd"), "\\@/etc/passwd")
        self.assertEqual(n("x \\@y"), "x \\@y")

    def test_applying_twice_equals_once(self):
        for s in ("@a", "a @b @@c", "\\@d", "x\u3002@e", "", "@", "a@b", "x \u200b@y\r\n"):
            with self.subTest(s=s):
                self.assertEqual(n(n(s)), n(s))


class DoubleAtTest(unittest.TestCase):
    def test_only_the_token_initial_at_of_a_run_is_escaped(self):
        self.assertEqual(n("@@x"), "\\@@x")
        self.assertEqual(n("a @@x"), "a \\@@x")
        self.assertEqual(tui_mentions(n("@@@x")), [])

    def test_at_at_alone(self):
        self.assertEqual(n("@@"), "\\@@")


class ShapesTest(unittest.TestCase):
    def test_home_relative_and_absolute_paths(self):
        for p in ("@~/.ssh/id_ed25519", "@./notes.txt", "@/etc/passwd",
                  "@../secret", "@~/.claude/.credentials.json"):
            with self.subTest(p=p):
                got = n(f"ref {p} end")
                self.assertEqual(got, f"ref \\{p} end")
                self.assertEqual(tui_mentions(got), [])

    def test_quoted_path_form(self):
        self.assertEqual(n('read @"my file.txt"'), 'read \\@"my file.txt"')

    def test_mcp_resource_style(self):
        self.assertEqual(n("@server:resource/path"), "\\@server:resource/path")

    def test_mention_followed_by_cjk_mark_is_still_a_token(self):
        self.assertEqual(n("@\u3002x"), "\\@\u3002x")

    def test_unicode_around_it(self):
        self.assertEqual(n("caf\u00e9 @/etc/passwd \u2014 \u65e5\u672c"),
                         "caf\u00e9 \\@/etc/passwd \u2014 \u65e5\u672c")
        self.assertEqual(n("\U0001f4a5 @x"), "\U0001f4a5 \\@x")
        self.assertEqual(n("\u65e5@x"), "\u65e5@x")   # a CJK letter is not a boundary


class SliceSafetyTest(unittest.TestCase):
    def test_no_prefix_of_an_escaped_string_has_a_mention(self):
        """Callers slice after neutralizing (mailbox_wake's 110-char subject). A
        prefix can drop the '@' after a '\\' but never the '\\' before an '@'."""
        s = n("x @/etc/passwd @@y \u3002@z \u200b@w " * 3)
        for i in range(len(s) + 1):
            self.assertEqual(tui_mentions(s[:i]), [], i)


if __name__ == "__main__":
    unittest.main()
