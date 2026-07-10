"""Unit + smoke tests for .githooks/pre-commit and .githooks/commit-msg.

Run from repo root:
    python3 -m unittest tools.test_hermes_hook -v

Covers:
  - Pre-commit hook scan logic (clean / unescaped trigger / escaped form / binary)
  - Commit-msg hook scan logic (clean / trigger / escaped form)
  - Self-audit: hook source files must not contain the unescaped trigger
  - Real-git smoke: throwaway tempdir repo; hook refuses contaminated commit,
    passes escaped form, refuses contaminated commit message

Fixture-string note: the unescaped HERMES trigger is constructed at runtime via
source-time string concatenation (`b"HERME" + b"S.md"` / `"HERME" + "S.md"`)
so the contiguous 9-byte sequence never appears in this source file. This
protects `git log -p` of pantheon-config from re-introducing the trigger phrase
per the standing rule in `/home/claude/CLAUDE.md` and the byte-sequence
discipline convention in `tools/_git_utils.py`.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Resolve repo root and hook paths.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / ".githooks"
PRE_COMMIT_HOOK = HOOKS_DIR / "pre-commit"
COMMIT_MSG_HOOK = HOOKS_DIR / "commit-msg"


# ---------------------------------------------------------------------------
# Import hook scan helpers directly (the hooks themselves call these via
# tools._git_utils; we test the same logic through that module, mirroring the
# pattern used in test_watch_runner.py and test_ticket_grooming.py).
# ---------------------------------------------------------------------------

sys.path.insert(0, str(REPO_ROOT))
from tools._git_utils import _HERMES_PATTERN, _hermes_scan_message  # noqa: E402


def _scan_blob(blob: bytes) -> tuple[bool, str | None]:
    """Minimal replica of the hook's per-file scan: returns (ok, detail).
    detail is '<label>:<lineno>' on match, None otherwise.
    This mirrors what the hook does for each staged blob.
    """
    m = _HERMES_PATTERN.search(blob)
    if m:
        lineno = blob[: m.start()].count(b"\n") + 1
        return False, f"<blob>:{lineno}"
    return True, None


# ---------------------------------------------------------------------------
# Unit tests — staged-file scan logic.
# ---------------------------------------------------------------------------


class TestPreCommitScanLogic(unittest.TestCase):
    """Tests against the scan logic the pre-commit hook uses.  No subprocess —
    exercises _HERMES_PATTERN and _scan_blob directly.
    """

    def test_clean_file_passes(self) -> None:
        blob = b"# clean report\n\nNo trigger phrase here.\n"
        ok, detail = _scan_blob(blob)
        self.assertTrue(ok)
        self.assertIsNone(detail)

    def test_unescaped_trigger_refused(self) -> None:
        # Source-time concatenation; runtime yields the contiguous 9-byte
        # trigger that the regex matches.
        blob = b"Line 1 ok\nLine 2 mentions " + (b"HERME" + b"S.md") + b" inline\nLine 3 ok\n"
        ok, detail = _scan_blob(blob)
        self.assertFalse(ok)
        self.assertIsNotNone(detail)
        self.assertIn(":2", detail)

    def test_escaped_form_passes(self) -> None:
        # Discipline-escaped form: literal backslash between S and dot.
        # Bytes: H E R M E S \ . m d — regex requires S immediately followed
        # by '.', so this does not match.
        blob = b"We refer to the trigger as HERMES\\.md throughout this doc.\n"
        ok, detail = _scan_blob(blob)
        self.assertTrue(ok, f"escaped form must pass; got detail={detail!r}")
        self.assertIsNone(detail)

    def test_trigger_on_first_line_reported_as_line_1(self) -> None:
        blob = (b"HERME" + b"S.md") + b" at start\n"
        ok, detail = _scan_blob(blob)
        self.assertFalse(ok)
        self.assertIn(":1", detail)

    def test_binary_bytes_do_not_crash(self) -> None:
        # Arbitrary non-UTF-8 bytes — the hook scans bytes not text, so no
        # decode step to fail.  Trigger absent → passes cleanly.
        blob = bytes(range(256))
        ok, detail = _scan_blob(blob)
        self.assertTrue(ok)
        self.assertIsNone(detail)

    def test_binary_bytes_with_trigger_still_caught(self) -> None:
        # Binary file that happens to embed the trigger bytes.
        blob = b"\x00\x01\x02" + (b"HERME" + b"S.md") + b"\xff\xfe"
        ok, detail = _scan_blob(blob)
        self.assertFalse(ok)
        self.assertIsNotNone(detail)

    def test_empty_file_passes(self) -> None:
        ok, detail = _scan_blob(b"")
        self.assertTrue(ok)
        self.assertIsNone(detail)

    def test_only_escaped_form_in_large_file_passes(self) -> None:
        # 1 000 lines; escaped form appears on line 500.
        lines = [b"line content\n"] * 499
        lines.append(b"see HERMES\\.md for details\n")
        lines.extend([b"line content\n"] * 500)
        blob = b"".join(lines)
        ok, detail = _scan_blob(blob)
        self.assertTrue(ok)


# ---------------------------------------------------------------------------
# Unit tests — commit-message scan logic.
# ---------------------------------------------------------------------------


class TestCommitMsgScanLogic(unittest.TestCase):
    """Tests against _hermes_scan_message (same function the commit-msg hook
    uses).
    """

    def test_clean_message_passes(self) -> None:
        msg = "chore: update README\n\nSmall improvement.\n"
        self.assertTrue(_hermes_scan_message(msg))

    def test_unescaped_trigger_in_message_refused(self) -> None:
        # Source-time concatenation per fixture-string note.
        msg = "chore: fix\n\nsee " + ("HERME" + "S.md") + " for context\n"
        self.assertFalse(_hermes_scan_message(msg))

    def test_escaped_in_message_passes(self) -> None:
        msg = "chore: fix\n\nsee HERMES\\.md for context\n"
        self.assertTrue(_hermes_scan_message(msg))

    def test_empty_message_passes(self) -> None:
        self.assertTrue(_hermes_scan_message(""))

    def test_trigger_in_subject_line_refused(self) -> None:
        msg = ("HERME" + "S.md") + ": do not do this\n"
        self.assertFalse(_hermes_scan_message(msg))


# ---------------------------------------------------------------------------
# Self-audit: hook source files must be clean.
# ---------------------------------------------------------------------------


class TestHookSourceCleanliness(unittest.TestCase):
    """Verify that the hook files themselves never contain the unescaped trigger
    as contiguous source bytes — same discipline as test_ticket_grooming.py's
    TestHermesGuardBelt.test_no_unescaped_trigger_in_source.
    """

    def _assert_source_clean(self, path: Path) -> None:
        trigger = b"HERME" + b"S.md"
        content = path.read_bytes()
        self.assertNotIn(
            trigger,
            content,
            f"{path.relative_to(REPO_ROOT)} must not contain the unescaped "
            f"trigger as contiguous bytes (byte-sequence discipline).",
        )

    def test_pre_commit_hook_source_clean(self) -> None:
        self._assert_source_clean(PRE_COMMIT_HOOK)

    def test_commit_msg_hook_source_clean(self) -> None:
        self._assert_source_clean(COMMIT_MSG_HOOK)

    def test_install_hooks_sh_source_clean(self) -> None:
        self._assert_source_clean(REPO_ROOT / "tools" / "install_hooks.sh")

    def test_this_test_file_source_clean(self) -> None:
        self._assert_source_clean(Path(__file__))

    def test_hooks_are_executable(self) -> None:
        for hook in [PRE_COMMIT_HOOK, COMMIT_MSG_HOOK]:
            mode = hook.stat().st_mode
            self.assertTrue(
                mode & stat.S_IXUSR,
                f"{hook.name} must be executable (chmod +x)",
            )


# ---------------------------------------------------------------------------
# Real-git smoke tests — throwaway tempdir repo.
# ---------------------------------------------------------------------------


class TestGitSmoke(unittest.TestCase):
    """End-to-end: spin up a real git repo in a tempdir, install the hooks via
    core.hooksPath, then make commits and verify the hooks fire correctly.

    These tests run actual `git commit` subprocess calls — no mocking.  The
    throwaway repo is isolated in tempfile.TemporaryDirectory() and never
    touches pantheon-config.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.repo = Path(cls._tmpdir.name)

        # Initialise bare-minimum git repo.
        def git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["git", *args],
                cwd=cls.repo,
                capture_output=True,
                text=True,
                check=False,
            )

        git("init")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")

        # Point core.hooksPath at the live .githooks from pantheon-config.
        git("config", "core.hooksPath", str(HOOKS_DIR))

        cls._git = staticmethod(git)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    def _write(self, name: str, content: bytes) -> Path:
        p = self.repo / name
        p.write_bytes(content)
        return p

    def _commit(self, msg: str, *files: str) -> subprocess.CompletedProcess:
        self._git("add", *files)
        return self._git("commit", "-m", msg)

    # -- staged file tests --------------------------------------------------

    def test_smoke_refuse_contaminated_file(self) -> None:
        """Commit a file containing the unescaped trigger — hook must refuse."""
        content = b"Line 1\nLine 2: " + (b"HERME" + b"S.md") + b"\nLine 3\n"
        self._write("bad.txt", content)
        result = self._commit("add bad file", "bad.txt")
        self.assertNotEqual(
            result.returncode,
            0,
            "pre-commit hook should have refused this commit",
        )
        # Stderr should mention the file name and a line number.
        self.assertIn("bad.txt", result.stderr)
        self.assertIn(":2", result.stderr)

    def test_smoke_pass_escaped_file(self) -> None:
        """Commit a file with the escaped form — hook must pass."""
        content = b"See HERMES\\.md for the discipline rationale.\n"
        self._write("escaped.txt", content)
        result = self._commit("add escaped file", "escaped.txt")
        self.assertEqual(
            result.returncode,
            0,
            f"escaped form should pass; stderr={result.stderr!r}",
        )

    def test_smoke_pass_clean_file(self) -> None:
        """Ordinary commit with no trigger — must pass."""
        self._write("clean.txt", b"hello world\n")
        result = self._commit("add clean file", "clean.txt")
        self.assertEqual(
            result.returncode,
            0,
            f"clean commit should pass; stderr={result.stderr!r}",
        )

    # -- commit message tests -----------------------------------------------

    def test_smoke_refuse_contaminated_commit_message(self) -> None:
        """Commit with trigger in message — commit-msg hook must refuse."""
        self._write("msg_test.txt", b"clean file content\n")
        bad_msg = "chore: see " + ("HERME" + "S.md") + " for context"
        result = self._commit(bad_msg, "msg_test.txt")
        self.assertNotEqual(
            result.returncode,
            0,
            "commit-msg hook should have refused this commit",
        )
        self.assertIn("commit message", result.stderr.lower())

    def test_smoke_pass_escaped_commit_message(self) -> None:
        """Commit with escaped form in message — must pass."""
        self._write("msg_escaped.txt", b"clean file content\n")
        ok_msg = "chore: see HERMES\\.md for the discipline rationale"
        result = self._commit(ok_msg, "msg_escaped.txt")
        self.assertEqual(
            result.returncode,
            0,
            f"escaped form in message should pass; stderr={result.stderr!r}",
        )

    def test_smoke_pass_clean_commit_message(self) -> None:
        """Ordinary message — must pass."""
        self._write("msg_clean.txt", b"another clean file\n")
        result = self._commit("chore: routine update", "msg_clean.txt")
        self.assertEqual(
            result.returncode,
            0,
            f"clean message should pass; stderr={result.stderr!r}",
        )


if __name__ == "__main__":
    unittest.main()
