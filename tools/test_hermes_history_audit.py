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
        """If git show fails for one commit, skip it and continue."""
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

        # Scan continues; error noted in output.
        self.assertIn("HERMES audit", line)
        # 2 commits successfully scanned, 1 errored.
        self.assertIn("2 commit", line)


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


if __name__ == "__main__":
    unittest.main()
