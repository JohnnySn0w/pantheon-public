"""Structural-parse + streaming tests for tools/hermes_diff_scan.py (T-4185, T-4187).

Run from repo root:
    python3 -m unittest tools.test_hermes_diff_scan -v

Why this file exists (separate from `test_hermes_action`, which pins the
original public behaviour and stays untouched):

  T-4185  The scanner used to treat ANY line beginning with three plus signs and
          a space as a file header. An added line whose CONTENT begins with two
          plus signs and a space renders in a diff as exactly that shape, so a
          planted trigger on it was skipped (guard bypass). The same defect class
          hid behind `str.splitlines()` / text-mode stdin, which split on a lone
          carriage return, form feed, NEL, U+2028 etc. and then skipped the tail
          of the split line as "metadata". The fix frames the patch the way git
          does (hunk counts) and splits on the newline byte only.
  T-4187  The scanner used to read all of stdin before parsing (1.3-1.76 GB peak
          RSS on a 204 MB full-history stream). It now consumes line by line.
  T-4211  Files git renders as binary (NUL in the first 8000 bytes, or
          .gitattributes `binary` / `-diff`) were invisible: the producers now
          pass --text. A newline-free binary then becomes one huge line, so the
          scanner reads in `_LINE_CAP` segments and searches every segment of an
          added line with an overlap carry (fail-closed, bounded memory).

Fixtures use REAL temp git repos, in the two input forms the guard receives:
    git diff A B                                       (push / PR form)
    git log -p -m --format= --no-color A..B            (per-commit push-path form)

Fixture-string note: the unescaped trigger is built at runtime by concatenation
("HERME" + "S.md") so the contiguous 9-byte sequence never appears in this
source file (standing rule in /home/claude/CLAUDE.md; convention shared with
tools/test_hermes_action.py and tools/_git_utils.py).
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools._git_utils import _HERMES_PATTERN  # noqa: E402
from tools import hermes_diff_scan  # noqa: E402
from tools.hermes_diff_scan import (  # noqa: E402
    format_annotation,
    scan_diff_lines,
    scan_diff_stream,
)

SCANNER = REPO_ROOT / "tools" / "hermes_diff_scan.py"
TRIGGER = "HERME" + "S.md"
TRIGGER_B = TRIGGER.encode()
UNKNOWN_PATH = "<unknown>"

# Hermetic git: no user/system config, no hooks, no signing, fixed identity.
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}
_GIT_FLAGS = [
    "-c", "core.hooksPath=" + os.devnull,
    "-c", "commit.gpgsign=false",
    "-c", "core.autocrlf=false",
    "-c", "diff.renames=true",
]


class _Repo:
    """Throwaway git repo with a tiny command surface."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True)
        self.git("init", "-q")

    def git(self, *args: str) -> bytes:
        r = subprocess.run(
            ["git", *_GIT_FLAGS, *args],
            cwd=self.root, env=_GIT_ENV, capture_output=True,
        )
        if r.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {r.stderr!r}")
        return r.stdout

    def write(self, rel: str, data: "str | bytes") -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode() if isinstance(data, str) else data)

    def commit(self, msg: str = "c") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD").decode().strip()

    def diff(self, a: str, b: str, *extra: str) -> bytes:
        return self.git("diff", *extra, a, b)

    def log(self, rng: str) -> bytes:
        return self.git("log", "-p", "-m", "--format=", "--no-color", rng)


def _pairs(violations) -> list:
    return sorted((v.path, v.lineno) for v in violations)


def _cli(data: bytes, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCANNER), *args], input=data, capture_output=True,
    )


class _RepoCase(unittest.TestCase):
    """Base: a fresh repo per test plus form-agnostic scan assertions."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp_root = Path(tmp.name)
        self._n_repos = 0
        self.repo = self.new_repo()

    def new_repo(self) -> _Repo:
        self._n_repos += 1
        return _Repo(self._tmp_root / f"r{self._n_repos}")

    def assertScan(self, raw: bytes, expected: list, msg: str = "") -> None:
        """Both public entry points must agree on (path, lineno) pairs."""
        expected = sorted(expected)
        self.assertEqual(
            _pairs(scan_diff_stream(io.BytesIO(raw))), expected,
            f"scan_diff_stream {msg}\n{raw!r}",
        )
        self.assertEqual(
            _pairs(scan_diff_lines(raw.decode("utf-8", "replace"))), expected,
            f"scan_diff_lines {msg}\n{raw!r}",
        )

    def assertBothForms(self, repo: _Repo, base: str, head: str, expected: list) -> None:
        self.assertScan(repo.diff(base, head), expected, "[git diff form]")
        self.assertScan(repo.log(f"{base}..{head}"), expected, "[git log -p -m form]")


# ---------------------------------------------------------------------------
# T-4185 — structural header recognition, real git output
# ---------------------------------------------------------------------------


class TestHeaderBypassRealGit(_RepoCase):

    def test_added_line_whose_content_starts_with_plusplus_is_flagged(self) -> None:
        r = self.repo
        r.write("a.txt", "base\n")
        base = r.commit()
        r.write("a.txt", f"base\n++ {TRIGGER}\n")
        head = r.commit()
        raw = r.diff(base, head)
        self.assertIn(b"\n+++ " + TRIGGER_B, raw, "fixture must render as a header lookalike")
        self.assertBothForms(r, base, head, [("a.txt", 2)])

    def test_plus_prefixed_content_variants_all_flagged(self) -> None:
        r = self.repo
        r.write("a.txt", "base\n")
        base = r.commit()
        variants = ["+ ", "++ ", "+++ ", "++", "+++", "+"]
        r.write("a.txt", "base\n" + "".join(f"{v}{TRIGGER}\n" for v in variants))
        head = r.commit()
        expected = [("a.txt", 2 + i) for i in range(len(variants))]
        self.assertBothForms(r, base, head, expected)

    def test_removed_dashdash_then_added_plusplus_pair_inside_one_hunk(self) -> None:
        """Removed content `-- old` and added content `++ <trigger>` render as
        `--- old` / `+++ <trigger>`: byte-for-byte a header pair, mid-hunk."""
        r = self.repo
        r.write("a.txt", "keep\n-- old\nkeep2\n")
        base = r.commit()
        r.write("a.txt", f"keep\n++ {TRIGGER}\nkeep2\n")
        head = r.commit()
        raw = r.diff(base, head)
        self.assertIn(b"\n--- old\n+++ " + TRIGGER_B, raw, "fixture must render as a header pair")
        self.assertBothForms(r, base, head, [("a.txt", 2)])

    def test_trigger_right_after_context_line_starting_with_plusplus(self) -> None:
        r = self.repo
        r.write("a.txt", "++ ctx\nx\n")
        base = r.commit()
        r.write("a.txt", f"++ ctx\n++ {TRIGGER}\nx\n")
        head = r.commit()
        raw = r.diff(base, head)
        self.assertIn(b"\n ++ ctx\n+++ ", raw)
        self.assertBothForms(r, base, head, [("a.txt", 2)])

    def test_trigger_in_second_hunk_reports_right_line(self) -> None:
        r = self.repo
        lines = [f"l{i}" for i in range(1, 41)]
        r.write("a.txt", "\n".join(lines) + "\n")
        base = r.commit()
        edited = list(lines)
        edited[1] = "changed2"
        edited[37] = f"++ {TRIGGER}"
        r.write("a.txt", "\n".join(edited) + "\n")
        head = r.commit()
        self.assertEqual(r.diff(base, head).count(b"\n@@ "), 2, "fixture needs two hunks")
        self.assertBothForms(r, base, head, [("a.txt", 38)])
        # -U0 hunks omit counts of 1 (`@@ -38 +38 @@`); framing must still hold.
        self.assertScan(r.diff(base, head, "-U0"), [("a.txt", 38)], "[-U0]")


class TestHeaderShapesRealGit(_RepoCase):
    """Header detection still works for every file shape git emits."""

    def test_new_deleted_and_modified_files(self) -> None:
        r = self.repo
        r.write("a.txt", "base\n")
        r.write("gone.txt", f"old {TRIGGER}\n")  # trigger only on a REMOVED line
        base = r.commit()
        r.write("a.txt", f"base\nnew {TRIGGER}\n")
        os.remove(r.root / "gone.txt")
        r.write("new.txt", f"{TRIGGER}\n")
        head = r.commit()
        self.assertIn(b"\n+++ /dev/null\n", r.diff(base, head))
        self.assertIn(b"\n--- /dev/null\n", r.diff(base, head))
        self.assertBothForms(r, base, head, [("a.txt", 2), ("new.txt", 1)])

    def test_rename_with_edit_and_pure_rename(self) -> None:
        r = self.repo
        body = "".join(f"line number {i}\n" for i in range(30))
        r.write("r.txt", body)
        r.write("pure.txt", "p1\np2\np3\n")
        r.write("z.txt", "z\n")
        base = r.commit()
        r.git("mv", "r.txt", "r2.txt")
        r.write("r2.txt", body + f"tail {TRIGGER}\n")
        r.git("mv", "pure.txt", "pure2.txt")
        r.write("z.txt", f"z\n++ {TRIGGER}\n")
        head = r.commit()
        raw = r.diff(base, head)
        self.assertIn(b"rename from r.txt", raw)
        self.assertIn(b"rename from pure.txt", raw)
        self.assertBothForms(r, base, head, [("r2.txt", 31), ("z.txt", 2)])

    def test_binary_file_has_no_hunk_and_does_not_confuse_next_file(self) -> None:
        r = self.repo
        r.write("a.txt", "base\n")
        base = r.commit()
        r.write("bin.dat", b"\x00" + TRIGGER_B + b"\x00")
        r.write("text.txt", f"++ {TRIGGER}\n")
        head = r.commit()
        self.assertIn(b"Binary files", r.diff(base, head))
        self.assertBothForms(r, base, head, [("text.txt", 1)])

    def test_merge_commit_minus_m_output(self) -> None:
        r = self.repo
        r.write("a.txt", "base\n")
        base = r.commit()
        main_branch = r.git("symbolic-ref", "--short", "HEAD").decode().strip()
        r.git("checkout", "-q", "-b", "side")
        r.write("s.txt", f"x\n++ {TRIGGER}\n")
        r.commit("side")
        r.git("checkout", "-q", main_branch)
        r.write("m.txt", "clean\n")
        r.commit("mainx")
        r.git("merge", "-q", "--no-ff", "side", "-m", "merge")
        head = r.git("rev-parse", "HEAD").decode().strip()
        # -m: the merge is diffed against EACH parent, plus the side commit itself.
        log_raw = r.log(f"{base}..{head}")
        self.assertGreaterEqual(log_raw.count(b"diff --git"), 4)
        self.assertScan(log_raw, [("s.txt", 2), ("s.txt", 2)], "[log -p -m over a merge]")
        self.assertScan(r.diff(base, head), [("s.txt", 2)], "[git diff form]")

    def test_filename_with_spaces_path_has_no_git_trailing_tab(self) -> None:
        """Git terminates ---/+++ paths containing spaces with a tab. The
        annotation's file= field must be the bare name (T-4185 Q1 decision)."""
        r = self.repo
        r.write("a.txt", "base\n")
        r.write("sp ace.txt", "one\n")
        base = r.commit()
        r.write("sp ace.txt", f"one\n++ {TRIGGER}\n")
        r.write("new sp.txt", f"{TRIGGER}\n")
        head = r.commit()
        raw = r.diff(base, head)
        self.assertIn(b"+++ b/sp ace.txt\t\n", raw, "fixture must carry git's trailing tab")
        self.assertBothForms(r, base, head, [("sp ace.txt", 2), ("new sp.txt", 1)])
        res = _cli(raw)
        self.assertEqual(res.returncode, 1)
        fields = re.findall(rb"::error file=([^,]*),line=(\d+)::", res.stdout)
        self.assertEqual(sorted(fields), [(b"new sp.txt", b"1"), (b"sp ace.txt", b"2")])
        for name, _ in fields:
            self.assertNotIn(b"\t", name)

    def test_c_quoted_filename_is_still_detected(self) -> None:
        """git c-quotes non-ASCII names in headers (`+++ "b/caf\\303\\251.txt"`).
        Left as today (path shown quoted); only detection is pinned here."""
        r = self.repo
        r.write("a.txt", "base\n")
        base = r.commit()
        r.write("café.txt", f"++ {TRIGGER}\n")
        head = r.commit()
        raw = r.diff(base, head)
        self.assertIn(b'+++ "b/caf\\303\\251.txt"', raw)
        for form in (raw, r.log(f"{base}..{head}")):
            got = list(scan_diff_stream(io.BytesIO(form)))
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].lineno, 1)

    def test_no_newline_at_eof_marker_does_not_disturb_line_numbers(self) -> None:
        r = self.repo
        r.write("a.txt", "a\nb")  # no trailing newline
        base = r.commit()
        r.write("a.txt", f"a\nb\n++ {TRIGGER}\n")
        head = r.commit()
        self.assertIn(b"\\ No newline at end of file", r.diff(base, head))
        self.assertBothForms(r, base, head, [("a.txt", 3)])

        r2 = self.new_repo()
        r2.write("a.txt", "a\nb\n")
        base2 = r2.commit()
        r2.write("a.txt", f"a\nb\n++ {TRIGGER}")  # marker AFTER the added line
        head2 = r2.commit()
        self.assertIn(b"\\ No newline at end of file", r2.diff(base2, head2))
        self.assertBothForms(r2, base2, head2, [("a.txt", 3)])


class TestContentShapedSplitBypass(_RepoCase):
    """The second content-shaped blind spot: separators that splitlines() and
    text-mode stdin treat as line breaks but git does not."""

    _SEPARATORS = [
        b"\r", b"\x0b", b"\x0c", b"\x1c", b"\x1d", b"\x1e",
        "\u0085".encode(), " ".encode(), " ".encode(), b"\x85",
    ]

    def test_separator_before_trigger_inside_an_added_line(self) -> None:
        r = self.repo
        r.write("a.txt", "base\n")
        prev = r.commit()
        for sep in self._SEPARATORS:
            with self.subTest(sep=sep):
                r.write("a.txt", b"base\npre" + sep + TRIGGER_B + b"\n")
                head = r.commit()
                self.assertBothForms(r, prev, head, [("a.txt", 2)])
                res = _cli(r.diff(prev, head))
                self.assertEqual(res.returncode, 1, res.stderr)
                self.assertEqual(res.stdout.count(b"::error "), 1)
                prev = head

    def test_crlf_file_line_numbers_and_detection(self) -> None:
        r = self.repo
        r.write("a.txt", b"base\r\n")
        base = r.commit()
        r.write("a.txt", b"base\r\nx " + TRIGGER_B + b"\r\n")
        head = r.commit()
        self.assertBothForms(r, base, head, [("a.txt", 2)])

    def test_non_utf8_content_does_not_crash_and_still_flags(self) -> None:
        r = self.repo
        r.write("a.txt", "base\n")
        base = r.commit()
        r.write("a.txt", b"caf\xe9\nx " + TRIGGER_B + b"\n")
        head = r.commit()
        raw = r.diff(base, head)
        res = _cli(raw)
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertNotIn(b"Traceback", res.stderr)
        self.assertEqual(re.findall(rb"file=([^,]*),line=(\d+)", res.stdout), [(b"a.txt", b"2")])


# ---------------------------------------------------------------------------
# T-4185 — synthetic shapes git would not normally emit (fail-closed posture)
# ---------------------------------------------------------------------------


class TestSyntheticShapes(unittest.TestCase):

    @staticmethod
    def _scan(text: str):
        return list(scan_diff_lines(text))

    def test_plus_header_lookalike_outside_header_position_is_scanned(self) -> None:
        text = (
            "diff --git a/f b/f\n"
            "index 1111111..2222222 100644\n"
            f"+++ {TRIGGER}\n"
        )
        got = self._scan(text)
        self.assertEqual([v.path for v in got], [UNKNOWN_PATH])

    def test_added_lines_with_no_file_header_are_flagged_not_dropped(self) -> None:
        got = self._scan(f"@@ -0,0 +1 @@\n+{TRIGGER}\n")
        self.assertEqual([(v.path, v.lineno) for v in got], [(UNKNOWN_PATH, 1)])

    def test_over_declared_hunk_then_next_diff_recovers_file_attribution(self) -> None:
        text = (
            "diff --git a/one b/one\n--- a/one\n+++ b/one\n"
            "@@ -1,5 +1,5 @@\n"  # declares 5/5 but carries 1 line
            " ctx\n"
            "diff --git a/two b/two\n--- a/two\n+++ b/two\n"
            "@@ -0,0 +1 @@\n"
            f"+{TRIGGER}\n"
        )
        self.assertEqual([(v.path, v.lineno) for v in self._scan(text)], [("two", 1)])

    def test_header_pair_after_count_terminated_hunk_without_diff_line(self) -> None:
        text = (
            "--- a/one\n+++ b/one\n"
            "@@ -1 +1 @@\n-old\n+new\n"
            "--- a/two\n+++ b/two\n"
            "@@ -0,0 +1 @@\n"
            f"+{TRIGGER}\n"
        )
        self.assertEqual([(v.path, v.lineno) for v in self._scan(text)], [("two", 1)])

    def test_omitted_hunk_counts_default_to_one(self) -> None:
        text = f"--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+{TRIGGER}\n"
        self.assertEqual([(v.path, v.lineno) for v in self._scan(text)], [("f", 1)])

    def test_empty_context_line_inside_hunk_counts_as_context(self) -> None:
        text = f"--- a/f\n+++ b/f\n@@ -1,3 +1,4 @@\n a\n\n+{TRIGGER}\n c\n"
        self.assertEqual([(v.path, v.lineno) for v in self._scan(text)], [("f", 3)])

    def test_crlf_terminated_headers_yield_clean_path(self) -> None:
        text = "diff --git a/f b/f\r\n--- a/f\r\n+++ b/f\r\n@@ -0,0 +1 @@\r\n" + f"+{TRIGGER}\r\n"
        self.assertEqual([(v.path, v.lineno) for v in self._scan(text)], [("f", 1)])

    def test_trailing_tab_stripped_exactly_once(self) -> None:
        def path_of(header_path: str) -> str:
            text = f"--- a/x\n+++ {header_path}\n@@ -0,0 +1 @@\n+{TRIGGER}\n"
            return self._scan(text)[0].path

        self.assertEqual(path_of("b/sp ace.txt\t"), "sp ace.txt")
        self.assertEqual(path_of("b/x\t\t"), "x\t")
        self.assertEqual(path_of("b/plain.txt"), "plain.txt")

    def test_violations_reported_in_order_of_appearance(self) -> None:
        text = (
            f"--- a/b\n+++ b/b\n@@ -0,0 +1 @@\n+{TRIGGER}\n"
            f"--- a/a\n+++ b/a\n@@ -0,0 +1 @@\n+{TRIGGER}\n"
        )
        self.assertEqual([v.path for v in self._scan(text)], ["b", "a"])

    def test_scan_diff_lines_accepts_bytes_and_str_identically(self) -> None:
        text = f"--- a/f\n+++ b/f\n@@ -0,0 +1 @@\n++ {TRIGGER}\n"
        self.assertEqual(scan_diff_lines(text), scan_diff_lines(text.encode()))

    def test_lines_without_trailing_newline_are_accepted(self) -> None:
        raw = f"--- a/f\n+++ b/f\n@@ -0,0 +1 @@\n++ {TRIGGER}\n".encode()
        with_nl = list(scan_diff_stream(io.BytesIO(raw)))
        without_nl = list(scan_diff_stream(raw.split(b"\n")))
        self.assertEqual(with_nl, without_nl)
        self.assertEqual(len(with_nl), 1)


# ---------------------------------------------------------------------------
# T-4187 — streaming: laziness, bounded RSS, identical output
# ---------------------------------------------------------------------------

_FILL = b"+" + b"a" * 79 + b"\n"          # 81 bytes per added line
_LINES_PER_CHUNK = 13_000                 # ~1.05 MB per chunk
_CHUNK = _FILL * _LINES_PER_CHUNK
_PLANT = b"+++ " + TRIGGER_B + b"\n"      # added line, content `++ <trigger>`


def _big_plan(n_chunks: int, planted_after: tuple):
    """(total_hunk_lines, expected [(path, lineno)]) for `_big_chunks`."""
    total = n_chunks * _LINES_PER_CHUNK + len(planted_after)
    expected = [
        ("big.txt", k * _LINES_PER_CHUNK + i + 1)
        for i, k in enumerate(sorted(planted_after))
    ]
    expected.append(("tail.txt", 1))
    return total, expected


def _big_chunks(n_chunks: int, planted_after: tuple):
    """Generate a git-diff-shaped stream on the fly: one huge hunk with a
    planted `++ <trigger>` line before chunk k for each k in planted_after,
    then a second small file that must still be parsed after the huge hunk."""
    total, _ = _big_plan(n_chunks, planted_after)
    planted = set(planted_after)
    yield (
        b"diff --git a/big.txt b/big.txt\nnew file mode 100644\n"
        b"--- /dev/null\n+++ b/big.txt\n"
        + f"@@ -0,0 +1,{total} @@\n".encode()
    )
    for k in range(n_chunks + 1):
        if k in planted:
            yield _PLANT
        if k < n_chunks:
            yield _CHUNK
    yield (
        b"diff --git a/tail.txt b/tail.txt\nnew file mode 100644\n"
        b"--- /dev/null\n+++ b/tail.txt\n@@ -0,0 +1 @@\n+" + TRIGGER_B + b"\n"
    )


@unittest.skipUnless(sys.platform.startswith("linux"), "os.wait4 ru_maxrss is Linux-specific here")
class TestStreamingBoundedMemory(unittest.TestCase):

    @staticmethod
    def _run_measured(chunks):
        """Feed `chunks` through a pipe to the CLI; return (rc, stdout, stderr,
        peak RSS in KB of THAT child only, via wait4)."""
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            p = subprocess.Popen(
                [sys.executable, str(SCANNER)],
                stdin=subprocess.PIPE, stdout=out, stderr=err,
            )
            try:
                for c in chunks:
                    p.stdin.write(c)
            except BrokenPipeError:
                pass
            finally:
                try:
                    p.stdin.close()
                except BrokenPipeError:
                    pass
            _, status, ru = os.wait4(p.pid, 0)
            p.returncode = os.waitstatus_to_exitcode(status)
            out.seek(0)
            err.seek(0)
            return p.returncode, out.read(), err.read(), ru.ru_maxrss

    def test_scanner_pulls_input_lazily(self) -> None:
        """Deterministic no-buffering proof: the first violation must surface
        after only a handful of input lines were pulled, not all 300k."""
        pulled = 0

        def gen():
            nonlocal pulled
            head = (
                b"diff --git a/f b/f\n", b"--- a/f\n", b"+++ b/f\n",
                b"@@ -0,0 +1,300001 @@\n", _PLANT,
            )
            for line in head:
                pulled += 1
                yield line
            for i in range(300_000):
                pulled += 1
                yield b"+filler %d\n" % i

        it = scan_diff_stream(gen())
        first = next(it)
        self.assertEqual((first.path, first.lineno), ("f", 1))
        self.assertLess(pulled, 100, "scanner buffered input instead of streaming")
        self.assertEqual(list(it), [])
        self.assertEqual(pulled, 300_005)

    def test_large_pipe_stream_is_scanned_in_bounded_rss(self) -> None:
        n_chunks = 48                                  # ~50 MB
        planted = (0, 7, 23, 48)
        total, expected = _big_plan(n_chunks, planted)
        self.assertGreater(total * len(_FILL), 50_000_000)

        rc0, _, _, small_kb = self._run_measured(_big_chunks(1, (0,)))
        self.assertEqual(rc0, 1)

        rc, out, err, big_kb = self._run_measured(_big_chunks(n_chunks, planted))
        self.assertEqual(rc, 1, err[-500:])
        got = [
            (m[0].decode(), int(m[1]))
            for m in re.findall(rb"::error file=([^,]*),line=(\d+)::", out)
        ]
        self.assertEqual(sorted(got), sorted(expected))
        self.assertEqual(out.count(b"::error "), len(expected))

        grown_mb = (big_kb - small_kb) / 1024
        self.assertLess(
            grown_mb, 24,
            f"peak RSS grew {grown_mb:.1f} MB scanning ~50 MB: input is being buffered "
            f"(small={small_kb} KB, big={big_kb} KB)",
        )

    def test_streamed_result_equals_non_streaming_baseline(self) -> None:
        planted = (0, 2, 5)
        chunks = list(_big_chunks(6, planted))
        raw = b"".join(chunks)
        _, expected = _big_plan(6, planted)
        streamed = _pairs(scan_diff_stream(io.BytesIO(raw)))
        baseline = _pairs(scan_diff_lines(raw.decode("utf-8", "replace")))
        self.assertEqual(streamed, sorted(expected))
        self.assertEqual(baseline, sorted(expected))


# ---------------------------------------------------------------------------
# CLI contract — output and exit codes unchanged by streaming
# ---------------------------------------------------------------------------


class TestCliContract(unittest.TestCase):

    def _diff(self) -> bytes:
        return (
            "diff --git a/n.md b/n.md\n--- a/n.md\n+++ b/n.md\n@@ -1,2 +1,3 @@\n"
            f" ctx\n+see {TRIGGER}\n end\n"
        ).encode()

    def test_clean_input_exits_zero_and_silent(self) -> None:
        clean = b"diff --git a/n b/n\n--- a/n\n+++ b/n\n@@ -0,0 +1 @@\n+fine\n"
        res = _cli(clean)
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, b"", b""))
        self.assertEqual(_cli(b"").returncode, 0)

    def test_violation_output_and_exit_code(self) -> None:
        res = _cli(self._diff())
        self.assertEqual(res.returncode, 1)
        v = scan_diff_lines(self._diff().decode())[0]
        self.assertEqual(res.stdout.decode(), format_annotation(v) + "\n")
        err = res.stderr.decode()
        self.assertIn("HERMES discipline violation — push refused.", err)
        self.assertIn("  n.md:2\n", err)
        self.assertTrue(err.endswith("  Fix the file(s), amend / re-push.\n\n"))

    def test_stdin_and_file_argument_are_byte_identical(self) -> None:
        data = self._diff()
        via_stdin = _cli(data)
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.diff"
            f.write_bytes(data)
            via_file = _cli(b"", str(f))
        self.assertEqual(
            (via_stdin.returncode, via_stdin.stdout, via_stdin.stderr),
            (via_file.returncode, via_file.stdout, via_file.stderr),
        )
        self.assertEqual(via_file.returncode, 1)


# ---------------------------------------------------------------------------
# T-4211 — files git renders as BINARY, and the per-line read cap
# ---------------------------------------------------------------------------

# The producer forms the guard runs, WITH --text (T-4211). Kept here as the
# single statement of the command shape these tests exercise.
_TEXT_DIFF = ("diff", "--text")
_TEXT_LOG = ("log", "-p", "-m", "--text", "--format=", "--no-color")


class TestBinaryRenderedFilesRealGit(_RepoCase):
    """Without --text git prints only `Binary files ... differ` for these files;
    the scanner never sees their content (RED rows). With --text it does."""

    def _assert_hidden_then_caught(self, r: _Repo, base: str, head: str, expected: list) -> None:
        plain = r.diff(base, head)
        self.assertIn(b"Binary files", plain, "fixture no longer binary-rendered")
        # RED: the pre-T-4211 producers hide the content entirely.
        self.assertScan(plain, [], "[plain git diff: the blind spot]")
        self.assertScan(r.log(f"{base}..{head}"), [], "[plain git log: the blind spot]")
        # GREEN: the --text producers expose it.
        text_diff = r.git(*_TEXT_DIFF, f"{base}...{head}")
        self.assertNotIn(b"Binary files", text_diff)
        self.assertScan(text_diff, expected, "[git diff --text]")
        self.assertScan(r.git(*_TEXT_LOG, f"{base}..{head}"), expected, "[git log -p -m --text]")

    def test_nul_in_first_8k_with_trigger_is_caught(self) -> None:
        r = self.repo
        r.write("a.txt", "base\n")
        base = r.commit()
        r.write("bin.dat", b"\x00header\n" + b"x" * 5000 + b"\n" + TRIGGER_B + b"\x00tail\n")
        r.write("nonl.bin", b"\x00\x01\x02" + TRIGGER_B + b"\xff")  # no newline at all
        head = r.commit()
        self._assert_hidden_then_caught(r, base, head, [("bin.dat", 3), ("nonl.bin", 1)])

    def test_gitattributes_binary_and_minus_diff_with_trigger_are_caught(self) -> None:
        r = self.repo
        r.write(".gitattributes", "*.blob binary\n*.nd -diff\n")
        r.write("a.txt", "base\n")
        base = r.commit()
        r.write("x.blob", f"plain text\nsee {TRIGGER}\n")
        r.write("y.nd", f"{TRIGGER}\n")
        head = r.commit()
        self._assert_hidden_then_caught(r, base, head, [("x.blob", 2), ("y.nd", 1)])

    def test_merge_minus_m_text_shows_binary_per_parent(self) -> None:
        """-m --text: per-parent diffs (no combined `@@@`), binary content
        included, so a binary brought in by a merge is caught against the
        parent that lacks it."""
        r = self.repo
        r.write("a.txt", "base\n")
        base = r.commit()
        main_branch = r.git("symbolic-ref", "--short", "HEAD").decode().strip()
        r.git("checkout", "-q", "-b", "side")
        r.write("s.bin", b"\x00" + TRIGGER_B + b"\n")
        r.commit("side")
        r.git("checkout", "-q", main_branch)
        r.write("m.txt", "clean\n")
        r.commit("mainx")
        r.git("merge", "-q", "--no-ff", "side", "-m", "merge")
        raw = r.git(*_TEXT_LOG, f"{base}..HEAD")
        self.assertNotIn(b"@@@", raw)
        self.assertNotIn(b"Binary files", raw)
        # side commit + merge-vs-first-parent both add s.bin.
        self.assertScan(raw, [("s.bin", 1), ("s.bin", 1)], "[log -p -m --text over a merge]")

    def test_text_is_a_no_op_for_ordinary_text_diffs(self) -> None:
        r = self.repo
        r.write("a.txt", "base\n")
        base = r.commit()
        r.write("a.txt", f"base\n++ {TRIGGER}\n")
        head = r.commit()
        self.assertEqual(r.git(*_TEXT_DIFF, base, head), r.diff(base, head))


def _added_line_diff(content: bytes, tail_file: bool = True) -> bytes:
    """One-hunk diff adding `content` as line 1 of big.txt, then `+ok` as line
    2, then (optionally) a second file whose line 1 carries the trigger: proves
    hunk framing survives an oversized line."""
    out = (
        b"diff --git a/big.txt b/big.txt\nnew file mode 100644\n"
        b"--- /dev/null\n+++ b/big.txt\n@@ -0,0 +1,3 @@\n"
        b"+" + content + b"\n+ok\n+" + TRIGGER_B + b"\n"
    )
    if tail_file:
        out += (
            b"diff --git a/t.txt b/t.txt\nnew file mode 100644\n"
            b"--- /dev/null\n+++ b/t.txt\n@@ -0,0 +1 @@\n+" + TRIGGER_B + b"\n"
        )
    return out


class _CountingReader(io.BytesIO):
    """BytesIO that records the longest single readline() result."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.max_read = 0
        self.reads = 0

    def readline(self, size: int = -1) -> bytes:  # type: ignore[override]
        seg = super().readline(size)
        self.reads += 1
        self.max_read = max(self.max_read, len(seg))
        return seg


class TestOversizedLineCap(unittest.TestCase):
    """Fail-closed per-line cap: an over-cap line is read in segments, every
    segment of an added line is searched, and a trigger straddling a segment
    boundary is still caught."""

    CAP = 4096

    def setUp(self) -> None:
        p = mock.patch.object(hermes_diff_scan, "_LINE_CAP", self.CAP)
        p.start()
        self.addCleanup(p.stop)

    def _scan(self, raw: bytes) -> "tuple[list, _CountingReader]":
        rd = _CountingReader(raw)
        got = _pairs(scan_diff_stream(rd))
        self.assertEqual(got, _pairs(scan_diff_lines(raw)), "bytes entry point disagrees")
        return got, rd

    def test_overlap_covers_the_live_trigger(self) -> None:
        self.assertGreaterEqual(hermes_diff_scan._CHUNK_OVERLAP, len(TRIGGER_B) - 1)
        self.assertIsNotNone(_HERMES_PATTERN.fullmatch(TRIGGER_B))

    def test_trigger_straddling_every_boundary_offset_is_caught(self) -> None:
        # Segment k covers raw-line bytes [k*CAP, (k+1)*CAP) of the rendered
        # line `+<content>\n`; content offset o sits at line offset o+1.
        n = len(TRIGGER_B)
        for boundary in (self.CAP, 2 * self.CAP, 3 * self.CAP):
            for start in range(boundary - n - 1, boundary + 2):
                o = start - 1  # content offset
                content = b"a" * o + TRIGGER_B + b"b" * (4 * self.CAP - o)
                with self.subTest(boundary=boundary, start=start):
                    got, rd = self._scan(_added_line_diff(content))
                    self.assertEqual(got, [("big.txt", 1), ("big.txt", 3), ("t.txt", 1)])
                    self.assertLessEqual(rd.max_read, self.CAP)

    def test_trigger_in_last_segment_of_huge_line_is_caught(self) -> None:
        content = b"z" * (10 * self.CAP + 17) + TRIGGER_B
        got, rd = self._scan(_added_line_diff(content, tail_file=False))
        self.assertEqual(got, [("big.txt", 1), ("big.txt", 3)])
        self.assertLessEqual(rd.max_read, self.CAP)
        self.assertGreater(rd.reads, 10, "line was not read in segments")

    def test_oversized_clean_line_no_false_positive_and_bounded(self) -> None:
        content = b"q" * (25 * self.CAP)
        raw = _added_line_diff(content).replace(b"+" + TRIGGER_B, b"+clean")
        got, rd = self._scan(raw)
        self.assertEqual(got, [])
        self.assertLessEqual(rd.max_read, self.CAP)
        self.assertGreaterEqual(rd.reads, 25)

    def test_continuation_segment_is_never_reclassified(self) -> None:
        """A continuation that happens to begin with a header shape must not
        reset the file, open a hunk, or be counted as a line."""
        for lookalike in (b"diff --git a/x b/x", b"@@ -1,999 +1,999 @@", b"+++ b/evil", b"-"):
            pad = b"c" * (self.CAP - 1)  # '+' + pad fills segment 1 exactly
            content = pad + lookalike + b"d" * 50
            with self.subTest(lookalike=lookalike):
                got, _ = self._scan(_added_line_diff(content))
                self.assertEqual(got, [("big.txt", 3), ("t.txt", 1)])

    def test_trigger_in_continuation_of_removed_or_context_line_is_ignored(self) -> None:
        body = b"e" * (2 * self.CAP) + TRIGGER_B
        raw = (
            b"diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n"
            b"-" + body + b"\n"
            b" " + body + b"\n"
            b"+fine\n"
        )
        got, _ = self._scan(raw)
        self.assertEqual(got, [])

    def test_one_violation_per_oversized_line(self) -> None:
        content = (TRIGGER_B + b"-") * (3 * self.CAP // len(TRIGGER_B))
        got, _ = self._scan(_added_line_diff(content, tail_file=False))
        self.assertEqual(got, [("big.txt", 1), ("big.txt", 3)])

    def test_real_git_newline_free_binary_over_cap(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(Path(d) / "r")
            r.write("a.txt", "base\n")
            base = r.commit()
            blob = b"\x00" + b"\x01" * (3 * self.CAP - 5) + TRIGGER_B + b"\x02" * self.CAP
            self.assertNotIn(b"\n", blob)
            r.write("big.bin", blob)
            head = r.commit()
            for raw in (r.git(*_TEXT_DIFF, base, head), r.git(*_TEXT_LOG, f"{base}..{head}")):
                got, rd = self._scan(raw)
                self.assertEqual(got, [("big.bin", 1)])
                self.assertLessEqual(rd.max_read, self.CAP)


class TestOversizedLineRealCapMemory(unittest.TestCase):
    """At the real cap: a newline-free added line several times the cap is
    scanned without ever materialising it (tracemalloc peak, and child RSS via
    the CLI's stdin path)."""

    def test_tracemalloc_peak_is_bounded_by_cap_not_line(self) -> None:
        cap = hermes_diff_scan._LINE_CAP
        line_len = 6 * cap
        raw = _added_line_diff(b"w" * (line_len - 20) + TRIGGER_B, tail_file=False)
        rd = _CountingReader(raw)
        tracemalloc.start()
        try:
            got = _pairs(scan_diff_stream(rd))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(got, [("big.txt", 1), ("big.txt", 3)])
        self.assertLessEqual(rd.max_read, cap)
        self.assertLess(peak, 3 * cap, f"peak {peak} B for a {line_len} B line")

    @unittest.skipUnless(sys.platform.startswith("linux"), "ru_maxrss via wait4 is Linux-specific here")
    def test_cli_rss_does_not_grow_with_a_64mb_line(self) -> None:
        def chunks(n_mb: int):
            yield (
                b"diff --git a/b.bin b/b.bin\nnew file mode 100644\n"
                b"--- /dev/null\n+++ b/b.bin\n@@ -0,0 +1 @@\n+"
            )
            block = b"\x01" * (1024 * 1024)
            for _ in range(n_mb):
                yield block
            yield TRIGGER_B + b"\n"

        run = TestStreamingBoundedMemory._run_measured
        rc0, _, _, small_kb = run(chunks(1))
        rc, out, err, big_kb = run(chunks(64))
        self.assertEqual((rc0, rc), (1, 1), err[-300:])
        self.assertIn(b"::error file=b.bin,line=1::", out)
        grown_mb = (big_kb - small_kb) / 1024
        self.assertLess(grown_mb, 24, f"RSS grew {grown_mb:.1f} MB for a 64 MB line")


# ---------------------------------------------------------------------------
# T-4202 — the shared event API (iter_diff_events / iter_added_lines)
# ---------------------------------------------------------------------------


def _events(raw: bytes) -> list:
    return [
        (e.path, e.lineno, e.text, e.is_new_file, e.continued)
        for e in hermes_diff_scan.iter_diff_events(io.BytesIO(raw))
    ]


class TestIterDiffEvents(unittest.TestCase):
    """The generic framing every diff-walking consumer shares (T-4202)."""

    def test_fields_new_then_modified_file(self) -> None:
        raw = (
            b"diff --git a/n.md b/n.md\nnew file mode 100644\nindex 0..1\n"
            b"--- /dev/null\n+++ b/n.md\n@@ -0,0 +1,3 @@\n"
            b"+one\n+++ two\n+++three\n"
            b"diff --git a/m.txt b/m.txt\nindex 1..2 100644\n"
            b"--- a/m.txt\n+++ b/m.txt\n@@ -4,2 +4,3 @@\n"
            b" ctx\n-gone\n+new\n+\n"
        )
        self.assertEqual(_events(raw), [
            ("n.md", 1, b"one", True, False),
            ("n.md", 2, b"++ two", True, False),   # rendered `+++ two`: content
            ("n.md", 3, b"++three", True, False),
            ("m.txt", 5, b"new", False, False),     # is_new_file resets per file
            ("m.txt", 6, b"", False, False),
        ])

    def test_split_on_newline_byte_only(self) -> None:
        content = "a\x0cb c\u0085d\re\x1cf".encode()
        raw = (
            b"diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1,2 @@\n"
            b" k\n+" + content + b"\n"
        )
        self.assertEqual(_events(raw), [("x", 2, content, False, False)])
        whole = list(hermes_diff_scan.iter_added_lines(raw))
        self.assertEqual([(e.lineno, e.text) for e in whole], [(2, content)])

    def test_header_path_tab_and_c_quoting_are_undone(self) -> None:
        for header, want in (
            (b"+++ b/architecture/adr/ORG-1 sp.md\t\n", "architecture/adr/ORG-1 sp.md"),
            (b'+++ "b/caf\\303\\251.md"\n', "café.md"),
            (b'+++ "b/q\\"t\\\\b\\tc.md"\n', 'q"t\\b\tc.md'),
            (b"+++ /dev/null\n", None),
        ):
            raw = b"diff --git a/p b/p\n--- a/p\n" + header + b"@@ -0,0 +1 @@\n+z\n"
            with self.subTest(header=header):
                self.assertEqual(_events(raw), [(want, 1, b"z", False, False)])

    def test_new_file_mode_outside_header_position_is_content(self) -> None:
        raw = (
            b"diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1,2 @@\n"
            b" new file mode 100644\n+q\n"
        )
        self.assertEqual(_events(raw), [("x", 2, b"q", False, False)])

    def test_oversized_line_arrives_as_pieces_and_joins_back(self) -> None:
        cap = 64
        content = bytes(range(0x30, 0x7A)) * 5  # 370 bytes, no newline
        raw = (
            b"diff --git a/b b/b\nnew file mode 100644\n--- /dev/null\n"
            b"+++ b/b\n@@ -0,0 +1,2 @@\n+" + content + b"\n+++ after\n"
        )
        with mock.patch.object(hermes_diff_scan, "_LINE_CAP", cap):
            rd = _CountingReader(raw)
            evs = list(hermes_diff_scan.iter_diff_events(rd))
            self.assertLessEqual(rd.max_read, cap)
            whole = list(hermes_diff_scan.iter_added_lines(raw))
        head, pieces, last = evs[0], evs[1:-1], evs[-1]
        self.assertFalse(head.continued)
        self.assertEqual(len(head.text), cap - 1)  # the `+` took one byte
        self.assertTrue(pieces and all(p.continued for p in pieces))
        self.assertTrue(all((p.path, p.lineno, p.is_new_file) == ("b", 1, True) for p in pieces))
        self.assertEqual(head.text + b"".join(p.text for p in pieces), content)
        self.assertEqual((last.lineno, last.text, last.continued), (2, b"++ after", False))
        self.assertEqual(
            [(e.path, e.lineno, e.text, e.continued) for e in whole],
            [("b", 1, content, False), ("b", 2, b"++ after", False)],
        )

    def test_list_input_never_yields_pieces(self) -> None:
        lines = [b"--- a/x", b"+++ b/x", b"@@ -0,0 +1 @@", b"+" + b"y" * 10_000]
        with mock.patch.object(hermes_diff_scan, "_LINE_CAP", 16):
            evs = list(hermes_diff_scan.iter_diff_events(lines))
        self.assertEqual([(e.lineno, len(e.text), e.continued) for e in evs], [(1, 10_000, False)])


class TestIterDiffEventsRealGit(_RepoCase):
    def test_new_file_flag_and_plus_content_from_real_git(self) -> None:
        r = self.repo
        r.write("old.txt", "a\n")
        base = r.commit()
        r.write("old.txt", "a\n++ b\n")
        r.write("café.md", "---\n++ not a header\n++x\n")
        head = r.commit()
        raw = r.diff(base, head, "--text")
        self.assertIn(b'+++ "b/caf\\303\\251.md"', raw)
        self.assertEqual(sorted(_events(raw)), sorted([
            ("café.md", 1, b"---", True, False),
            ("café.md", 2, b"++ not a header", True, False),
            ("café.md", 3, b"++x", True, False),
            ("old.txt", 2, b"++ b", False, False),
        ]))
        # The guard now reports the real (unquoted) path.
        r.write("café.md", f"---\n++ {TRIGGER}\n")
        head2 = r.commit()
        self.assertEqual(_pairs(scan_diff_stream(io.BytesIO(r.diff(head, head2)))),
                         [("café.md", 2)])


# ---------------------------------------------------------------------------
# Self-audit — this file and the scanner must never hold the contiguous trigger
# ---------------------------------------------------------------------------


class TestSourceCleanliness(unittest.TestCase):

    def _assert_clean(self, path: Path) -> None:
        self.assertIsNone(
            _HERMES_PATTERN.search(path.read_bytes()),
            f"{path.relative_to(REPO_ROOT)} holds the unescaped trigger as contiguous bytes",
        )

    def test_this_file_clean(self) -> None:
        self._assert_clean(Path(__file__))

    def test_scanner_clean(self) -> None:
        self._assert_clean(SCANNER)


if __name__ == "__main__":
    unittest.main()
