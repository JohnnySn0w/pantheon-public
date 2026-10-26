"""Unit + smoke tests for tools/hermes_history_audit.py.

Run from repo root:
    python3 -m unittest tools.test_hermes_history_audit -v

Covers:
  - Clean history: zero occurrences, correct commit count.
  - Dirty history: trigger on an added (+) line caught; context/removed lines ignored.
  - Multiple dirty commits: count and SHA list.
  - SHA cap at 5 with "and N more" overflow.
  - git log failure: graceful ERROR line returned.
  - git show failure on one commit: skip + continue.
  - Empty history (zero commits): reports 0 across 0.
  - Performance warning when wall-clock exceeds 30s.
  - Self-audit: this test file and the audit module must not contain the
    unescaped trigger as contiguous source bytes.
  - Real-git smoke: throwaway tempdir repo; one contaminated commit + one
    clean commit; verifies count=1, SHA in output, 2 commits scanned.

Fixture-string note: the unescaped HERMES trigger is constructed at runtime
via source-time string concatenation (`b"HERME" + b"S.md"` / `"HERME" + "S.md"`)
so the contiguous 9-byte sequence never appears in this source file. This
protects `git log -p` of pantheon-config from re-introducing the trigger phrase
per the standing rule in `/home/claude/CLAUDE.md` and the byte-sequence
discipline convention in `tools/_git_utils.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools import hermes_history_audit  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _make_show_output(sha: str, added_lines: list[bytes], removed_lines: list[bytes] | None = None) -> bytes:
    """Build a minimal `git show <sha> -p` byte payload with one hunk."""
    removed_lines = removed_lines or []
    header = (
        f"commit {sha}\nAuthor: Test <t@t.com>\n\n    msg\n\n"
        "diff --git a/f.txt b/f.txt\n"
        "index 0000000..1111111 100644\n"
        "--- a/f.txt\n"
        "+++ b/f.txt\n"
        f"@@ -1,{len(removed_lines)} +1,{len(added_lines)} @@\n"
    ).encode()
    body = b""
    for line in removed_lines:
        body += b"-" + line
    for line in added_lines:
        body += b"+" + line
    return header + body


def _clean_show(sha: str) -> bytes:
    return _make_show_output(sha, [b"clean content\n"])


def _dirty_show(sha: str) -> bytes:
    """A git show output whose added lines contain the trigger bytes."""
    trigger = b"HERME" + b"S.md"
    return _make_show_output(sha, [b"mentions " + trigger + b" inline\n"])


def _context_line_show(sha: str) -> bytes:
    """Trigger bytes appear on a context line (space-prefix), not an added line."""
    trigger = b"HERME" + b"S.md"
    # Build raw bytes manually: no added lines; one context line with trigger.
    header = (
        f"commit {sha}\n\n    msg\n\n"
        "diff --git a/f.txt b/f.txt\n"
        "--- a/f.txt\n"
        "+++ b/f.txt\n"
        "@@ -1,1 +1,1 @@\n"
    ).encode()
    context = b" " + trigger + b" context\n"
    return header + context


def _removed_line_show(sha: str) -> bytes:
    """Trigger bytes on a removed line only."""
    trigger = b"HERME" + b"S.md"
    header = (
        f"commit {sha}\n\n    msg\n\n"
        "diff --git a/f.txt b/f.txt\n"
        "--- a/f.txt\n"
        "+++ b/f.txt\n"
        "@@ -1,1 +0,0 @@\n"
    ).encode()
    removed = b"-" + trigger + b"\n"
    return header + removed


# ---------------------------------------------------------------------------
# Unit tests — mocked subprocess.
# ---------------------------------------------------------------------------


class TestAuditCleanHistory(unittest.TestCase):
    """Clean history: zero occurrences reported."""

    def test_three_clean_commits(self) -> None:
        shas = ["aaa0001", "bbb0002", "ccc0003"]

        def fake_run(argv, **kwargs):
            if argv[1] == "log":
                return MagicMock(returncode=0, stdout=("\n".join(shas) + "\n").encode(), stderr=b"")
            if argv[1] == "show":
                sha = argv[2]
                return MagicMock(returncode=0, stdout=_clean_show(sha), stderr=b"")
            return MagicMock(returncode=0, stdout=b"", stderr=b"")

        with patch("tools.hermes_history_audit.subprocess.run", side_effect=fake_run):
            line = hermes_history_audit.run_history_audit(REPO_ROOT)

        self.assertIn("0 unescaped occurrence(s)", line)
        self.assertIn("3 commit", line)

    def test_empty_history(self) -> None:
        def fake_run(argv, **kwargs):
            if argv[1] == "log":
                return MagicMock(returncode=0, stdout=b"", stderr=b"")
            return MagicMock(returncode=0, stdout=b"", stderr=b"")

        with patch("tools.hermes_history_audit.subprocess.run", side_effect=fake_run):
            line = hermes_history_audit.run_history_audit(REPO_ROOT)

        self.assertIn("0 unescaped occurrence(s)", line)
        self.assertIn("0 commit", line)


class TestAuditDirtyHistory(unittest.TestCase):
    """Dirty history: trigger on added lines is caught."""

    def test_one_dirty_commit(self) -> None:
        shas = ["bad0001", "ok00002"]

        def fake_run(argv, **kwargs):
            if argv[1] == "log":
                return MagicMock(returncode=0, stdout=("\n".join(shas) + "\n").encode(), stderr=b"")
            sha = argv[2]
            output = _dirty_show(sha) if sha == "bad0001" else _clean_show(sha)
            return MagicMock(returncode=0, stdout=output, stderr=b"")

        with patch("tools.hermes_history_audit.subprocess.run", side_effect=fake_run):
            line = hermes_history_audit.run_history_audit(REPO_ROOT)

        self.assertIn("1 unescaped occurrence(s)", line)
        self.assertIn("1 commit(s)", line)
        self.assertIn("bad0001", line)
        self.assertNotIn("ok00002", line)
        self.assertIn("2 commit", line)

    def test_context_line_not_flagged(self) -> None:
        """Trigger on a context line must NOT be counted."""
        shas = ["ctx0001"]

        def fake_run(argv, **kwargs):
            if argv[1] == "log":
                return MagicMock(returncode=0, stdout=b"ctx0001\n", stderr=b"")
            return MagicMock(returncode=0, stdout=_context_line_show("ctx0001"), stderr=b"")

        with patch("tools.hermes_history_audit.subprocess.run", side_effect=fake_run):
            line = hermes_history_audit.run_history_audit(REPO_ROOT)

        self.assertIn("0 unescaped occurrence(s)", line)

    def test_removed_line_not_flagged(self) -> None:
        """Trigger on a removed line must NOT be counted."""
        shas = ["rem0001"]

        def fake_run(argv, **kwargs):
            if argv[1] == "log":
                return MagicMock(returncode=0, stdout=b"rem0001\n", stderr=b"")
            return MagicMock(returncode=0, stdout=_removed_line_show("rem0001"), stderr=b"")

        with patch("tools.hermes_history_audit.subprocess.run", side_effect=fake_run):
            line = hermes_history_audit.run_history_audit(REPO_ROOT)

        self.assertIn("0 unescaped occurrence(s)", line)

    def test_multiple_dirty_commits(self) -> None:
        """Multiple contaminated commits: all SHAs reported up to cap."""
        shas = [f"bad{i:04d}" for i in range(3)] + ["ok000000"]

        def fake_run(argv, **kwargs):
            if argv[1] == "log":
                return MagicMock(returncode=0, stdout=("\n".join(shas) + "\n").encode(), stderr=b"")
            sha = argv[2]
            output = _dirty_show(sha) if sha.startswith("bad") else _clean_show(sha)
            return MagicMock(returncode=0, stdout=output, stderr=b"")

        with patch("tools.hermes_history_audit.subprocess.run", side_effect=fake_run):
            line = hermes_history_audit.run_history_audit(REPO_ROOT)

        self.assertIn("3 unescaped occurrence(s)", line)
        self.assertIn("3 commit(s)", line)
        for i in range(3):
            self.assertIn(f"bad{i:04d}", line)

    def test_sha_cap_at_five_with_overflow(self) -> None:
        """Seven dirty commits: first 5 SHAs shown, 'and 2 more'."""
        dirty_shas = [f"bad{i:04d}" for i in range(7)]
        all_shas = dirty_shas + ["ok000000"]

        def fake_run(argv, **kwargs):
            if argv[1] == "log":
                return MagicMock(returncode=0, stdout=("\n".join(all_shas) + "\n").encode(), stderr=b"")
            sha = argv[2]
            output = _dirty_show(sha) if sha.startswith("bad") else _clean_show(sha)
            return MagicMock(returncode=0, stdout=output, stderr=b"")

        with patch("tools.hermes_history_audit.subprocess.run", side_effect=fake_run):
            line = hermes_history_audit.run_history_audit(REPO_ROOT)

        self.assertIn("7 unescaped occurrence(s)", line)
        # First 5 SHAs present.
        for i in range(5):
            self.assertIn(f"bad{i:04d}", line)
        # 6th and 7th NOT present as individual SHAs.
        self.assertNotIn("bad0005", line)
        self.assertNotIn("bad0006", line)
        # Overflow note present.
        self.assertIn("and 2 more", line)


class TestAuditErrorHandling(unittest.TestCase):
    """Error paths: git log failure, git show failure."""

    def test_git_log_failure_returns_error_line(self) -> None:
        def fake_run(argv, **kwargs):
            if argv[1] == "log":
                return MagicMock(returncode=128, stdout=b"", stderr=b"fatal: not a git repo")
            return MagicMock(returncode=0, stdout=b"", stderr=b"")

        with patch("tools.hermes_history_audit.subprocess.run", side_effect=fake_run):
            line = hermes_history_audit.run_history_audit(REPO_ROOT)

        self.assertIn("ERROR", line)
        self.assertIn("HERMES audit", line)

    def test_git_show_failure_skips_commit(self) -> None:
        """If git show fails for one commit, keep scanning; report ERROR."""
        shas = ["ok00001", "err0002", "ok00003"]

        def fake_run(argv, **kwargs):
            if argv[1] == "log":
                return MagicMock(returncode=0, stdout=("\n".join(shas) + "\n").encode(), stderr=b"")
            sha = argv[2]
            if sha == "err0002":
                return MagicMock(returncode=128, stdout=b"", stderr=b"error")
            return MagicMock(returncode=0, stdout=_clean_show(sha), stderr=b"")

        with patch("tools.hermes_history_audit.subprocess.run", side_effect=fake_run):
            line = hermes_history_audit.run_history_audit(REPO_ROOT)

        # Scan continues, but the line is an ERROR, never a clean result (T-4211).
        self.assertTrue(line.startswith("HERMES audit: ERROR: git show failed for 1 of 3"), line)
        # 2 commits successfully scanned, 1 errored.
        self.assertIn("in the 2 commit(s) scanned", line)


class TestAuditPerformanceWarning(unittest.TestCase):
    """Performance: if scan exceeds 30s wall-clock, warning appended."""

    def test_slow_scan_appends_warning(self) -> None:
        shas = ["ok00001"]

        def fake_run(argv, **kwargs):
            if argv[1] == "log":
                return MagicMock(returncode=0, stdout=b"ok00001\n", stderr=b"")
            return MagicMock(returncode=0, stdout=_clean_show("ok00001"), stderr=b"")

        # Fake time: start=0, end=35 → elapsed=35s > 30s threshold.
        with patch("tools.hermes_history_audit.subprocess.run", side_effect=fake_run), \
             patch("tools.hermes_history_audit.time.monotonic", side_effect=[0.0, 35.0]):
            line = hermes_history_audit.run_history_audit(REPO_ROOT)

        self.assertIn("WARNING", line)
        self.assertIn("35", line)

    def test_fast_scan_no_warning(self) -> None:
        shas = ["ok00001"]

        def fake_run(argv, **kwargs):
            if argv[1] == "log":
                return MagicMock(returncode=0, stdout=b"ok00001\n", stderr=b"")
            return MagicMock(returncode=0, stdout=_clean_show("ok00001"), stderr=b"")

        with patch("tools.hermes_history_audit.subprocess.run", side_effect=fake_run), \
             patch("tools.hermes_history_audit.time.monotonic", side_effect=[0.0, 1.5]):
            line = hermes_history_audit.run_history_audit(REPO_ROOT)

        self.assertNotIn("WARNING", line)


# ---------------------------------------------------------------------------
# Self-audit: source-cleanliness checks.
# ---------------------------------------------------------------------------


class TestSourceCleanliness(unittest.TestCase):
    """This test file and the audit module must not contain the unescaped
    trigger as contiguous source bytes.
    """

    def _assert_clean(self, path: Path) -> None:
        trigger = b"HERME" + b"S.md"
        content = path.read_bytes()
        self.assertNotIn(
            trigger,
            content,
            f"{path.relative_to(REPO_ROOT)} must not contain the unescaped "
            "trigger as contiguous source bytes (byte-sequence discipline).",
        )

    def test_this_test_file_source_clean(self) -> None:
        self._assert_clean(Path(__file__))

    def test_audit_module_source_clean(self) -> None:
        self._assert_clean(REPO_ROOT / "tools" / "hermes_history_audit.py")


# ---------------------------------------------------------------------------
# Real-git smoke test — throwaway tempdir repo.
# ---------------------------------------------------------------------------


class TestRealGitSmoke(unittest.TestCase):
    """End-to-end: real git repo in tempdir; one dirty commit + one clean commit.

    No mocking. The audit module is called with the tempdir as repo_root.
    Verifies:
      - count = 1 occurrence
      - the dirty commit's SHA appears in the output
      - total commits scanned = 2
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.repo = Path(cls._tmpdir.name)
        trigger = b"HERME" + b"S.md"

        def git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["git", *args],
                cwd=cls.repo,
                capture_output=True,
                check=False,
            )

        git("init")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")
        # Disable hooks so there's no interference from pantheon-config's pre-commit.
        git("config", "core.hooksPath", "/dev/null")

        # Commit 1: contains the trigger on an added line.
        bad_file = cls.repo / "bad.txt"
        bad_file.write_bytes(b"see " + trigger + b" here\n")
        git("add", "bad.txt")
        res = git("commit", "-m", "add bad file")
        # Extract the SHA of commit 1.
        sha_res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cls.repo,
            capture_output=True,
            text=True,
            check=False,
        )
        cls.dirty_sha = sha_res.stdout.strip()

        # Commit 2: clean.
        clean_file = cls.repo / "clean.txt"
        clean_file.write_bytes(b"hello world\n")
        git("add", "clean.txt")
        git("commit", "-m", "add clean file")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    def test_smoke_one_dirty_one_clean(self) -> None:
        line = hermes_history_audit.run_history_audit(self.repo)
        self.assertIn("1 unescaped occurrence(s)", line, f"got: {line!r}")
        self.assertIn(self.dirty_sha, line, f"dirty SHA {self.dirty_sha!r} missing; got: {line!r}")
        self.assertIn("2 commit", line, f"expected 2 commits scanned; got: {line!r}")

    def test_smoke_result_starts_with_hermes_audit(self) -> None:
        line = hermes_history_audit.run_history_audit(self.repo)
        self.assertTrue(line.startswith("HERMES audit:"), f"got: {line!r}")


# ---------------------------------------------------------------------------
# T-4185 regression: content-shaped blind spots in `_scan_show_output`.
# An added line whose CONTENT starts with `+` renders as `++...` (skipped by the
# old "not startswith ++" header guard); `bytes.splitlines()` also split on a
# lone carriage return and skipped the tail as "metadata".
# ---------------------------------------------------------------------------


class TestPlusPrefixedContentBypass(unittest.TestCase):

    _TRIGGER = b"HERME" + b"S.md"

    def test_added_line_with_plus_prefixed_content_is_flagged(self) -> None:
        for content in (b"+ ", b"++ ", b"+++ ", b"+"):
            with self.subTest(content=content):
                raw = _make_show_output("abc0001", [content + self._TRIGGER + b"\n"])
                self.assertTrue(hermes_history_audit._scan_show_output(raw))

    def test_in_hunk_header_lookalike_pair_is_flagged(self) -> None:
        """Removed `-- old` + added `++ <trigger>` render as `--- old` / `+++ <trigger>`."""
        raw = (
            b"commit abc0001\n\n    msg\n\n"
            b"diff --git a/f.txt b/f.txt\n"
            b"--- a/f.txt\n+++ b/f.txt\n"
            b"@@ -1 +1 @@\n"
            b"--- old\n"
            b"+++ " + self._TRIGGER + b"\n"
        )
        self.assertTrue(hermes_history_audit._scan_show_output(raw))

    def test_carriage_return_inside_added_line_does_not_hide_trigger(self) -> None:
        raw = _make_show_output("abc0001", [b"pre\r" + self._TRIGGER + b"\n"])
        self.assertTrue(hermes_history_audit._scan_show_output(raw))

    def test_clean_plus_prefixed_content_is_not_flagged(self) -> None:
        raw = _make_show_output("abc0001", [b"++ harmless\n", b"+++ also harmless\n"])
        self.assertFalse(hermes_history_audit._scan_show_output(raw))

    def test_real_git_plusplus_line_counted_by_run_history_audit(self) -> None:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        }
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)

            def git(*args: str) -> None:
                subprocess.run(
                    ["git", "-c", "core.hooksPath=" + os.devnull, *args],
                    cwd=repo, env=env, capture_output=True, check=True,
                )

            git("init", "-q")
            (repo / "f.txt").write_bytes(b"base\n")
            git("add", "-A")
            git("commit", "-q", "-m", "base")
            (repo / "f.txt").write_bytes(b"base\n++ " + self._TRIGGER + b"\n")
            git("add", "-A")
            git("commit", "-q", "-m", "plant")
            line = hermes_history_audit.run_history_audit(repo)
        self.assertIn("1 unescaped occurrence(s)", line, f"got: {line!r}")
        self.assertIn("2 commit", line, f"got: {line!r}")


# ---------------------------------------------------------------------------
# T-4211: files git renders as BINARY. Without `git show --text` the output is
# only "Binary files ... differ" and the trigger inside is never scanned.
# ---------------------------------------------------------------------------


class TestBinaryRenderedFilesCaught(unittest.TestCase):

    _TRIGGER = b"HERME" + b"S.md"

    def _audit(self, files: dict) -> str:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        }
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)

            def git(*args: str) -> bytes:
                return subprocess.run(
                    ["git", "-c", "core.hooksPath=" + os.devnull, *args],
                    cwd=repo, env=env, capture_output=True, check=True,
                ).stdout

            git("init", "-q")
            (repo / ".gitattributes").write_bytes(b"*.blob binary\n*.nd -diff\n")
            (repo / "base.txt").write_bytes(b"base\n")
            git("add", "-A")
            git("commit", "-q", "-m", "base")
            for name, data in files.items():
                (repo / name).write_bytes(data)
            git("add", "-A")
            git("commit", "-q", "-m", "plant")
            # Fixture sanity: plain `git show -p` hides the content (the gap).
            self.assertIn(b"Binary files", git("show", "HEAD", "-p"))
            return hermes_history_audit.run_history_audit(repo)

    def test_nul_file_with_trigger_is_counted(self) -> None:
        line = self._audit({"bin.dat": b"\x00\x01" + self._TRIGGER + b"\x00"})
        self.assertIn("1 unescaped occurrence(s)", line, f"got: {line!r}")

    def test_gitattributes_binary_file_with_trigger_is_counted(self) -> None:
        line = self._audit({"x.blob": b"text " + self._TRIGGER + b"\n"})
        self.assertIn("1 unescaped occurrence(s)", line, f"got: {line!r}")

    def test_gitattributes_minus_diff_file_with_trigger_is_counted(self) -> None:
        line = self._audit({"y.nd": self._TRIGGER + b"\n"})
        self.assertIn("1 unescaped occurrence(s)", line, f"got: {line!r}")

    def test_clean_binary_is_not_counted(self) -> None:
        line = self._audit({"bin.dat": b"\x00\x01clean\x00" * 1000})
        self.assertIn("0 unescaped occurrence(s)", line, f"got: {line!r}")

    def test_show_is_invoked_with_text(self) -> None:
        seen = []

        def fake_run(argv, **kwargs):
            if argv[1] == "log":
                return MagicMock(returncode=0, stdout=b"aaa0001 ppp0001 ppp0002\n", stderr=b"")
            seen.append(argv)
            return MagicMock(returncode=0, stdout=_clean_show("aaa0001"), stderr=b"")

        with patch("tools.hermes_history_audit.subprocess.run", side_effect=fake_run):
            hermes_history_audit.run_history_audit(Path("/fake"))
        self.assertEqual(len(seen), 1)
        for flag in ("-p", "--text", "--diff-merges=remerge", "--no-textconv", "--no-ext-diff"):
            self.assertIn(flag, seen[0])


# ---------------------------------------------------------------------------
# T-4211: merge commits. The default `git show <merge>` is a dense combined
# diff (`@@@`), which the scanner does not hunk-track: a resolution that
# removes a `- x` line (present in both parents) and adds `+ <trigger>`
# renders as `--- x` / `+++ <trigger>`, a header lookalike pair, and was
# skipped. The audit now uses --diff-merges=remerge: a merge is charged with
# exactly the bytes its resolution introduced (clean merge = empty patch).
# ---------------------------------------------------------------------------


_TRIG = b"HERME" + b"S.md"


class _Git:
    """Throwaway repo with hermetic config."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        }
        self("init", "-q")

    def __call__(self, *args: str, check: bool = True) -> bytes:
        return subprocess.run(
            ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false", *args],
            cwd=self.root, env=self.env, capture_output=True, check=check,
        ).stdout

    def commit(self, files: dict, msg: str) -> str:
        for name, data in files.items():
            (self.root / name).write_bytes(data)
        self("add", "-A")
        self("commit", "-q", "-m", msg)
        return self.head()

    def head(self) -> str:
        return self("rev-parse", "--short=7", "HEAD").decode().strip()

    def branch_off(self, name: str) -> str:
        trunk = self("symbolic-ref", "--short", "HEAD").decode().strip()
        self("checkout", "-q", "-b", name)
        return trunk


class TestEvilMergeCaught(unittest.TestCase):

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.g = _Git(Path(tmp.name))

    def _two_branches(self, base: bytes, side: dict, trunk: dict) -> "tuple[str, str]":
        g = self.g
        g.commit({"f.txt": base}, "base")
        trunk_name = g.branch_off("side")
        side_sha = g.commit(side, "side")
        g("checkout", "-q", trunk_name)
        g.commit(trunk, "trunk")
        return trunk_name, side_sha

    def _evil_merge(self, removed: bytes, added: bytes) -> str:
        """Clean branches; the merge resolution replaces `removed` (present in
        both parents) with `added` (present in neither)."""
        self._two_branches(b"top\n" + removed + b"\nbottom\n", {"s.txt": b"side\n"}, {"m.txt": b"trunk\n"})
        g = self.g
        g("merge", "-q", "--no-ff", "--no-commit", "side")
        return g.commit({"f.txt": b"top\n" + added + b"\nbottom\n"}, "merge")

    def _conflicted_merge(self, side_line: bytes, resolution: bytes) -> "tuple[str, str]":
        _, side_sha = self._two_branches(
            b"top\nmid\nbottom\n", {"f.txt": b"top\n" + side_line + b"\nbottom\n"},
            {"f.txt": b"top\nTRUNK\nbottom\n"},
        )
        g = self.g
        g("merge", "-q", "side", check=False)  # conflicts: exit 1 is expected
        return side_sha, g.commit({"f.txt": resolution}, "merge")

    def _audit(self) -> str:
        return hermes_history_audit.run_history_audit(self.g.root)

    def test_header_lookalike_evil_merge_is_caught(self) -> None:
        merge_sha = self._evil_merge(b"- x", b"+ " + _TRIG)
        # Fixture sanity: the default combined diff renders the lookalike
        # pair, and the scanner misses it there (the pre-fix blind spot).
        cc = self.g("show", "HEAD", "-p", "--text")
        self.assertIn(b"@@@", cc)
        self.assertIn(b"\n--- x\n+++ " + _TRIG, cc)
        self.assertFalse(hermes_history_audit._scan_show_output(cc))
        line = self._audit()
        self.assertIn(f"1 unescaped occurrence(s) in 1 commit(s): {merge_sha}.", line, f"got: {line!r}")
        self.assertIn("(4 commit(s) scanned)", line, f"got: {line!r}")

    def test_plain_evil_merge_line_is_caught(self) -> None:
        merge_sha = self._evil_merge(b"middle", b"evil " + _TRIG)
        line = self._audit()
        self.assertIn(f"1 unescaped occurrence(s) in 1 commit(s): {merge_sha}.", line, f"got: {line!r}")

    def test_clean_evil_shaped_merge_is_not_flagged(self) -> None:
        self._evil_merge(b"- x", b"+ fine")
        line = self._audit()
        self.assertEqual(line, "HERMES audit: 0 unescaped occurrence(s) across 4 commit(s) scanned.")

    def test_clean_merge_of_dirty_side_charges_only_the_side_commit(self) -> None:
        _, side_sha = self._two_branches(
            b"base\n", {"s.txt": b"x " + _TRIG + b"\n"}, {"m.txt": b"trunk\n"},
        )
        self.g("merge", "-q", "--no-ff", "side", "-m", "merge")
        merge_sha = self.g.head()
        line = self._audit()
        self.assertIn(f"1 unescaped occurrence(s) in 1 commit(s): {side_sha}.", line, f"got: {line!r}")
        self.assertNotIn(merge_sha, line)
        # Under `separate` the clean merge would be charged too (the over-report).
        sep = self.g("show", "HEAD", *hermes_history_audit._OCTOPUS_SHOW_ARGS)
        self.assertTrue(hermes_history_audit._scan_show_output(sep))

    def test_conflict_resolution_that_adds_the_trigger_is_caught(self) -> None:
        side_sha, merge_sha = self._conflicted_merge(b"SIDE", b"top\nTRUNK " + _TRIG + b"\nbottom\n")
        line = self._audit()
        self.assertIn(f"1 unescaped occurrence(s) in 1 commit(s): {merge_sha}.", line, f"got: {line!r}")
        self.assertNotIn(side_sha, line)

    def test_conflict_markers_are_removed_lines_only(self) -> None:
        """remerge shows the re-merge's conflict markers as REMOVED lines; a
        clean resolution adds nothing that can hit. Marker labels carry commit
        subjects, so a trigger in a subject must not be charged to the merge."""
        g = self.g
        g.commit({"f.txt": b"top\nmid\nbottom\n"}, "base")
        trunk = g.branch_off("side")
        g.commit({"f.txt": b"top\nSIDE\nbottom\n"}, "side " + _TRIG.decode())
        g("checkout", "-q", trunk)
        g.commit({"f.txt": b"top\nTRUNK\nbottom\n"}, "trunk")
        g("merge", "-q", "side", check=False)
        g.commit({"f.txt": b"top\nTRUNK\nSIDE\nbottom\n"}, "merge")
        out = g("show", "HEAD", *hermes_history_audit._SHOW_ARGS)
        self.assertIn(b"\n-<<<<<<< ", out)
        self.assertIn(b"\n->>>>>>> ", out)
        self.assertIn(_TRIG, out)  # the marker label, on a removed line
        self.assertNotIn(b"\n+<<<<<<<", out)
        self.assertNotIn(b"\n+>>>>>>>", out)
        self.assertEqual(
            self._audit(), "HERMES audit: 0 unescaped occurrence(s) across 4 commit(s) scanned.",
        )

    def test_conflict_resolution_taking_the_sides_trigger_charges_the_side(self) -> None:
        side_line = b"S " + _TRIG
        side_sha, merge_sha = self._conflicted_merge(side_line, b"top\n" + side_line + b"\nbottom\n")
        line = self._audit()
        self.assertIn(f"1 unescaped occurrence(s) in 1 commit(s): {side_sha}.", line, f"got: {line!r}")
        self.assertNotIn(merge_sha, line)

    def test_octopus_evil_merge_is_caught_via_separate_fallback(self) -> None:
        """git skips remerge for 3+ parents (warning, no patch)."""
        g = self.g
        g.commit({"f.txt": b"base\n"}, "base")
        trunk = g("symbolic-ref", "--short", "HEAD").decode().strip()
        for b in ("x", "y"):
            g("checkout", "-q", "-b", b, trunk)
            g.commit({b + ".txt": b.encode() + b"\n"}, b)
        g("checkout", "-q", trunk)
        g("merge", "-q", "--no-ff", "--no-commit", "x", "y")
        merge_sha = g.commit({"evil.txt": b"e " + _TRIG + b"\n"}, "octopus")
        self.assertIn(b"Skipping remerge-diff", g("show", "HEAD", *hermes_history_audit._SHOW_ARGS))
        line = self._audit()
        self.assertIn(f"1 unescaped occurrence(s) in 1 commit(s): {merge_sha}.", line, f"got: {line!r}")

    def test_user_log_diffmerges_config_cannot_change_the_mode(self) -> None:
        """`-m` follows log.diffMerges; the explicit flag must not. Under
        first-parent config a `-m` clean merge of a dirty side would be charged."""
        _, side_sha = self._two_branches(
            b"base\n", {"s.txt": b"x " + _TRIG + b"\n"}, {"m.txt": b"trunk\n"},
        )
        self.g("merge", "-q", "--no-ff", "side", "-m", "merge")
        cfg = self.g.root / ".." / (self.g.root.name + ".gitconfig")
        cfg.write_text("[log]\n\tdiffMerges = first-parent\n")
        self.addCleanup(cfg.unlink)
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(cfg)}):
            line = self._audit()
        self.assertIn(f"1 unescaped occurrence(s) in 1 commit(s): {side_sha}.", line, f"got: {line!r}")


# ---------------------------------------------------------------------------
# T-4211: fail-closed on `git show` failure (e.g. a git too old for a flag).
# ---------------------------------------------------------------------------


class TestShowFailureIsNotClean(unittest.TestCase):

    def test_every_show_failing_on_real_git_is_an_error_not_clean(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            g = _Git(Path(d))
            g.commit({"a.txt": b"a\n"}, "one")
            g.commit({"b.txt": b"b\n"}, "two")
            bogus = (*hermes_history_audit._SHOW_ARGS, "--diff-merges=no-such-mode")
            with patch.object(hermes_history_audit, "_SHOW_ARGS", bogus):
                line = hermes_history_audit.run_history_audit(g.root)
        self.assertTrue(line.startswith("HERMES audit: ERROR: git show failed for 2 of 2 commit(s)"), line)
        self.assertIn("0 unescaped occurrence(s) in the 0 commit(s) scanned.", line)
        self.assertNotIn("across", line)

    def test_partial_failure_keeps_occurrences_and_is_an_error(self) -> None:
        shas = ["bad0001", "err0002", "ok00003"]

        def fake_run(argv, **kwargs):
            if argv[1] == "log":
                return MagicMock(returncode=0, stdout=("\n".join(shas) + "\n").encode(), stderr=b"")
            if argv[2] == "err0002":
                return MagicMock(returncode=128, stdout=b"", stderr=b"fatal: boom\nmore\n")
            if argv[2] == "bad0001":
                return MagicMock(returncode=0, stdout=_dirty_show("bad0001"), stderr=b"")
            return MagicMock(returncode=0, stdout=_clean_show(argv[2]), stderr=b"")

        with patch("tools.hermes_history_audit.subprocess.run", side_effect=fake_run):
            line = hermes_history_audit.run_history_audit(Path("/fake"))
        self.assertEqual(
            line,
            "HERMES audit: ERROR: git show failed for 1 of 3 commit(s) (first: err0002: fatal: boom); "
            "1 unescaped occurrence(s) in 1 commit(s): bad0001. (2 commit(s) scanned)",
        )



# ---------------------------------------------------------------------------
# T-4308: the clean-verdict cache. Real temp repos, real git.
# ---------------------------------------------------------------------------


class TestCleanVerdictCache(unittest.TestCase):

    _TRIGGER = b"HERME" + b"S.md"

    def setUp(self) -> None:
        from tools import hermes_history_audit
        self.audit = hermes_history_audit
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        self.cache = Path(self._tmp.name) / "state" / "cache.txt"
        for args in (("init", "-q"), ("config", "user.email", "t@example.com"),
                     ("config", "user.name", "T"), ("config", "core.hooksPath", "/dev/null")):
            self._git(*args)
        for i in range(3):
            self._commit(f"f{i}.txt", b"clean %d\n" % i)

    def _git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                              check=True).stdout

    def _commit(self, name: str, content: bytes) -> str:
        (self.repo / name).write_bytes(content)
        self._git("add", name)
        self._git("commit", "-q", "-m", name)
        return self._git("rev-parse", "HEAD").strip()

    def _run(self, **kw):
        """(line, number of `git show` calls)."""
        real = self.audit._git
        shows = []

        def counting(*args, cwd):
            if args and args[0] == "show":
                shows.append(args[1])
            return real(*args, cwd=cwd)

        with patch.object(self.audit, "_git", side_effect=counting):
            line = self.audit.run_history_audit(self.repo, **kw)
        return line, len(shows)

    def test_warm_run_matches_cold_and_shows_nothing(self) -> None:
        cold, cold_shows = self._run(cache_path=self.cache)
        warm, warm_shows = self._run(cache_path=self.cache)
        self.assertEqual((cold_shows, warm_shows), (3, 0))
        self.assertIn("0 unescaped occurrence(s) across 3 commit(s) scanned.", cold)
        self.assertEqual(warm, cold + " (3 clean verdict(s) from the T-4308 cache.)")

    def test_a_new_dirty_commit_is_caught_on_a_warm_cache(self) -> None:
        self._run(cache_path=self.cache)
        dirty = self._commit("bad.txt", b"see " + self._TRIGGER + b"\n")
        line, shows = self._run(cache_path=self.cache)
        self.assertEqual(shows, 1)
        self.assertIn(f"1 unescaped occurrence(s) in 1 commit(s): {dirty[:7]}", line)

    def test_a_dirty_commit_is_never_cached(self) -> None:
        dirty = self._commit("bad.txt", b"see " + self._TRIGGER + b"\n")
        self._run(cache_path=self.cache)
        self.assertNotIn(dirty, self.cache.read_text())
        line, shows = self._run(cache_path=self.cache)
        self.assertEqual(shows, 1)
        self.assertIn(dirty[:7], line)

    def test_changed_show_flags_invalidate_the_cache(self) -> None:
        self._run(cache_path=self.cache)
        with patch.object(self.audit, "_SHOW_ARGS", (*self.audit._SHOW_ARGS, "--no-color")):
            _, shows = self._run(cache_path=self.cache)
        self.assertEqual(shows, 3)

    def test_a_corrupt_or_foreign_cache_means_a_full_scan(self) -> None:
        self.cache.parent.mkdir(parents=True)
        # The foreign-key file lists this repo's REAL SHAs: only the key check
        # keeps them from being trusted.
        real = self._git("log", "--format=%H").encode()
        for content in (b"\xff\xfe garbage", b"key not-this-one\n" + real):
            self.cache.write_bytes(content)
            line, shows = self._run(cache_path=self.cache)
            with self.subTest(content=content[:12]):
                self.assertEqual(shows, 3)
                self.assertIn("across 3 commit(s) scanned.", line)

    def test_no_cache_path_does_no_cache_io(self) -> None:
        line, shows = self._run()
        self.assertEqual(shows, 3)
        self.assertFalse(self.cache.exists())
        self.assertNotIn("cache", line)

    def test_a_killed_cold_run_keeps_its_checkpointed_progress(self) -> None:
        """A cold scan can outlast grooming's 10-min kill. The progress made before
        the kill must survive, or every later run stays cold and is killed too."""
        real = self.audit._git
        shows = []

        def killed_after_two_shows(*args, cwd):
            if args and args[0] == "show":
                if len(shows) == 2:
                    raise KeyboardInterrupt("TimeoutStartSec")
                shows.append(args[1])
            return real(*args, cwd=cwd)

        with patch.object(self.audit, "_CACHE_CHECKPOINT_EVERY", 1), \
                patch.object(self.audit, "_git", side_effect=killed_after_two_shows), \
                self.assertRaises(KeyboardInterrupt):
            self.audit.run_history_audit(self.repo, cache_path=self.cache)
        _, resumed_shows = self._run(cache_path=self.cache)
        self.assertEqual(resumed_shows, 1)  # only the commit the kill interrupted

    def _assert_rescans_all_after(self, change) -> None:
        """Warm the cache, apply `change` (a context manager), and require a FULL
        rescan: a changed key input must never reuse old verdicts."""
        self._run(cache_path=self.cache)
        with change:
            _, shows = self._run(cache_path=self.cache)
        self.assertEqual(shows, 3)

    def test_every_key_input_invalidates_the_cache(self) -> None:
        import re as _re
        audit = self.audit
        src_copy = Path(self._tmp.name) / "audit_src_copy.py"
        src_copy.write_bytes(Path(audit.__file__).read_bytes() + b"# changed\n")
        scan_copy = Path(self._tmp.name) / "scan_src_copy.py"
        scan_copy.write_bytes(Path(audit._diff_scan_module.__file__).read_bytes() + b"# changed\n")
        real_run = subprocess.run

        def other_git_version(cmd, *a, **kw):
            if list(cmd) == ["git", "--version"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=b"git version 0.0.0\n")
            return real_run(cmd, *a, **kw)

        pat = audit._HERMES_PATTERN
        changes = {
            "octopus args": patch.object(audit, "_OCTOPUS_SHOW_ARGS", (*audit._OCTOPUS_SHOW_ARGS, "--no-color")),
            "audit source": patch.object(audit, "__file__", str(src_copy)),
            "diff-scan source": patch.object(audit._diff_scan_module, "__file__", str(scan_copy)),
            "pattern text": patch.object(audit, "_HERMES_PATTERN", _re.compile(pat.pattern + b"x")),
            "pattern flags": patch.object(audit, "_HERMES_PATTERN", _re.compile(pat.pattern, pat.flags | _re.IGNORECASE)),
            "git version": patch.object(audit.subprocess, "run", side_effect=other_git_version),
        }
        for name, change in changes.items():
            with self.subTest(name):
                self._assert_rescans_all_after(change)

    def test_a_failed_git_show_is_never_cached(self) -> None:
        real = self.audit._git
        failing = self._git("rev-parse", "HEAD").strip()

        def show_fails_for_head(*args, cwd):
            if args[:2] == ("show", failing):
                return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"boom")
            return real(*args, cwd=cwd)

        with patch.object(self.audit, "_git", side_effect=show_fails_for_head):
            line = self.audit.run_history_audit(self.repo, cache_path=self.cache)
        self.assertIn("ERROR: git show failed", line)
        self.assertNotIn(failing, self.cache.read_text())
        _, shows = self._run(cache_path=self.cache)
        self.assertEqual(shows, 1)  # only the commit that failed is shown again

    def test_an_unreadable_key_input_disables_the_cache(self) -> None:
        """Fail CLOSED: when any key input can't be read, trust no cached verdict and
        write nothing, on two runs in a row (a fail-open key would re-arm on the 2nd)."""
        audit = self.audit
        self._run(cache_path=self.cache)
        before = self.cache.read_bytes()
        real_run = subprocess.run

        def on_version(behaviour):
            def run(cmd, *a, **kw):
                return behaviour(*a, **kw) if list(cmd) == ["git", "--version"] else real_run(cmd, *a, **kw)
            return run

        def git_missing(*a, **kw):
            raise FileNotFoundError("git")

        def git_exits_1(*a, **kw):  # a REAL failing process, so the check=True path is what's tested
            return real_run(["false"], *a, **kw)

        unreadable = {
            "git not found": patch.object(audit.subprocess, "run", side_effect=on_version(git_missing)),
            "git --version exits 1": patch.object(audit.subprocess, "run", side_effect=on_version(git_exits_1)),
            "audit source missing": patch.object(audit, "__file__", "/nonexistent/hermes_history_audit.py"),
            "diff-scan source missing": patch.object(audit._diff_scan_module, "__file__", "/nonexistent/hermes_diff_scan.py"),
            "diff-scan __file__ None": patch.object(audit._diff_scan_module, "__file__", None),
        }
        for name, change in unreadable.items():
            with self.subTest(name), change:
                for _ in range(2):
                    _, shows = self._run(cache_path=self.cache)
                    self.assertEqual(shows, 3)
                self.assertEqual(self.cache.read_bytes(), before)

    def test_save_does_not_write_through_a_planted_tmp_symlink(self) -> None:
        self._run(cache_path=self.cache)
        victim = Path(self._tmp.name) / "victim.txt"
        victim.write_text("untouched\n")
        for pid in range(os.getpid() - 2, os.getpid() + 3):  # the old predictable name
            (self.cache.parent / f"{self.cache.name}.tmp.{pid}").symlink_to(victim)
        self._commit("f9.txt", b"clean 9\n")
        self._run(cache_path=self.cache)
        self.assertEqual(victim.read_text(), "untouched\n")
        self.assertFalse(self.cache.is_symlink())

    def test_cache_file_is_private(self) -> None:
        self._run(cache_path=self.cache)
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)

    def test_default_cache_path_honours_pantheon_state_dir(self) -> None:
        with patch.dict(os.environ, {"PANTHEON_STATE_DIR": "/x/state"}):
            self.assertEqual(self.audit.default_cache_path(),
                             Path("/x/state/hermes_history_audit_clean.txt"))


if __name__ == "__main__":
    unittest.main()
