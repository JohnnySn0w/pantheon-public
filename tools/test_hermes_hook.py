"""Unit + smoke tests for .githooks/pre-commit and .githooks/commit-msg.

Run from repo root:
    python3 -m unittest tools.test_hermes_hook -v

Covers:
  - Pre-commit hook scan logic (clean / unescaped trigger / escaped form / binary)
  - Commit-msg hook scan logic (clean / trigger / escaped form)
  - Self-audit: hook source files must not contain the unescaped trigger
  - Real-git smoke: throwaway tempdir repo; hook refuses contaminated commit,
    passes escaped form, refuses contaminated commit message
  - Main-guard lint wiring (T-2314): staged-blob scoping, fail-open on
    infrastructural error, fail-closed on a finding, real-git smoke

Fixture-string note: the unescaped HERMES trigger is constructed at runtime via
source-time string concatenation (`b"HERME" + b"S.md"` / `"HERME" + "S.md"`)
so the contiguous 9-byte sequence never appears in this source file. This
protects `git log -p` of pantheon-config from re-introducing the trigger phrase
per the standing rule in `/home/claude/CLAUDE.md` and the byte-sequence
discipline convention in `tools/_git_utils.py`.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
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


def _load_pre_commit_hook_module():
    """Load `.githooks/pre-commit` as a module (T-2314).

    It has no `.py` extension, so `spec_from_file_location` can't infer a
    loader by suffix alone — pass an explicit `SourceFileLoader`.  This sets
    `__file__` correctly, so the hook's own `_REPO_ROOT = _HOOK.parent.parent`
    bootstrap still resolves to the real pantheon-config root regardless of
    test cwd.
    """
    loader = importlib.machinery.SourceFileLoader(
        "_pre_commit_hook_under_test", str(PRE_COMMIT_HOOK)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


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
# Unit tests — main-guard lint wiring (T-2314).
# ---------------------------------------------------------------------------


class TestMainGuardLintWiring(unittest.TestCase):
    """Unit tests for `_main_guard_lint_findings`, the pre-commit hook's staged-
    blob integration of `tools/main_guard_lint.py` (T-2314).

    Loads the hook file directly via importlib (see `_load_pre_commit_hook_module`)
    and stubs `_read_staged_blob` so no real git repo is needed for these — the
    real-git end-to-end firing is covered separately by `TestGitSmoke` below,
    per Amendment H.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.hook = _load_pre_commit_hook_module()

    def test_non_tools_py_file_is_not_scanned(self) -> None:
        """A staged file outside tools/*.py must not even be read — no call,
        no findings."""
        with patch.object(self.hook, "_read_staged_blob") as mock_read:
            result = self.hook._main_guard_lint_findings(
                ["README.md", "tools/hooks/background-pipe-gate.sh", "notes/foo.md"]
            )
        mock_read.assert_not_called()
        self.assertEqual(result, [])

    def test_no_staged_files_short_circuits(self) -> None:
        with patch.object(self.hook, "_read_staged_blob") as mock_read:
            result = self.hook._main_guard_lint_findings([])
        mock_read.assert_not_called()
        self.assertEqual(result, [])

    def test_clean_staged_tools_file_allows(self) -> None:
        clean_src = (
            b'import unittest\n'
            b'class TestA(unittest.TestCase):\n'
            b'    def test_a(self):\n'
            b'        pass\n'
            b'\n'
            b'if __name__ == "__main__":\n'
            b'    unittest.main()\n'
        )
        with patch.object(self.hook, "_read_staged_blob", return_value=clean_src):
            result = self.hook._main_guard_lint_findings(["tools/test_t2314_fixture.py"])
        self.assertEqual(result, [])

    def test_stranded_class_is_flagged(self) -> None:
        bad_src = (
            b'import unittest\n'
            b'if __name__ == "__main__":\n'
            b'    unittest.main()\n'
            b'\n'
            b'class TestStranded(unittest.TestCase):\n'
            b'    pass\n'
        )
        with patch.object(self.hook, "_read_staged_blob", return_value=bad_src):
            result = self.hook._main_guard_lint_findings(["tools/test_t2314_fixture.py"])
        self.assertEqual(len(result), 1)
        self.assertIn("tools/test_t2314_fixture.py:5", result[0])
        self.assertIn("TestStranded", result[0])
        self.assertIn("class", result[0])

    def test_lint_crash_on_one_file_fails_open_for_that_file_only(self) -> None:
        """An unexpected exception inside lint_source for a single staged file
        must not block the commit — PER-FILE fail-open (round-1 REBOUND fix):
        with no OTHER staged file, the overall result is `[]` (not None,
        which is reserved for the whole-module-unavailable arm)."""
        with patch.object(self.hook, "_read_staged_blob", return_value=b"whatever"):
            with patch(
                "tools.main_guard_lint.lint_source", side_effect=RuntimeError("boom")
            ):
                with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
                    result = self.hook._main_guard_lint_findings(["tools/test_t2314_fixture.py"])
        self.assertEqual(result, [])
        stderr_text = mock_stderr.getvalue()
        self.assertIn("fail-open", stderr_text)
        self.assertIn("tools/test_t2314_fixture.py", stderr_text)
        self.assertIn("boom", stderr_text)

    def test_whole_module_import_failure_fails_open_with_warning(self) -> None:
        """Simulates main_guard_lint being unavailable ENTIRELY (e.g. shadowed
        or renamed) — the single shared `from tools.main_guard_lint import
        lint_source` at the top raises, caught by the outer (not per-file)
        except-Exception arm. This is the only arm that returns None."""
        with patch.dict(sys.modules, {"tools.main_guard_lint": None}):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
                result = self.hook._main_guard_lint_findings(["tools/test_t2314_fixture.py"])
        self.assertIsNone(result)
        self.assertFalse(result)  # falsy like [] — main() must not block on this
        self.assertIn("fail-open", mock_stderr.getvalue())

    def test_decode_failure_on_one_file_fails_open_for_that_file_only(self) -> None:
        """Non-UTF-8 staged blob content on a single file is a per-file
        infrastructural surprise, not a finding — that file alone is
        skipped, `[]` is returned (no other staged files), warning names it."""
        with patch.object(self.hook, "_read_staged_blob", return_value=b"\xff\xfe\x00bad"):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
                result = self.hook._main_guard_lint_findings(["tools/test_t2314_fixture.py"])
        self.assertEqual(result, [])
        stderr_text = mock_stderr.getvalue()
        self.assertIn("fail-open", stderr_text)
        self.assertIn("tools/test_t2314_fixture.py", stderr_text)

    def test_finding_survives_a_sibling_files_decode_failure(self) -> None:
        """The round-1 REBOUND regression, falsified directly: a real finding
        on one staged tools/*.py file must SURVIVE an infrastructural failure
        (undecodable bytes) on a DIFFERENT staged file in the same commit —
        both orderings, plus the single-file case is already the control in
        `test_stranded_class_is_flagged` above."""
        stranded_src = (
            b'import unittest\n'
            b'if __name__ == "__main__":\n'
            b'    unittest.main()\n'
            b'\n'
            b'class TestStranded(unittest.TestCase):\n'
            b'    pass\n'
        )
        undecodable_src = b"\xff\xfe\x00bad"
        blobs = {
            "tools/a_stranded.py": stranded_src,
            "tools/b_undecodable.py": undecodable_src,
        }

        for order, targets in [
            ("stranded-first", ["tools/a_stranded.py", "tools/b_undecodable.py"]),
            ("undecodable-first", ["tools/b_undecodable.py", "tools/a_stranded.py"]),
        ]:
            with self.subTest(order=order):
                with patch.object(
                    self.hook, "_read_staged_blob", side_effect=lambda p: blobs[p]
                ):
                    with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
                        result = self.hook._main_guard_lint_findings(list(targets))
                self.assertIsNotNone(
                    result, f"{order}: a sibling's infra error must not fail the whole scan open"
                )
                self.assertEqual(len(result), 1, f"{order}: {result!r}")
                self.assertIn("TestStranded", result[0])
                self.assertIn("tools/b_undecodable.py", mock_stderr.getvalue())


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

    def tearDown(self) -> None:
        """Unconditional per-test isolation (round-1 REBOUND fix).

        A REFUSED commit leaves its file(s) STAGED — `git add` already ran
        before the hook fired and rejected the `git commit`. `self.repo` is
        shared across EVERY test method in this class (one tempdir repo for
        the whole TestCase, via setUpClass), so a refused test's leftover
        staged content would otherwise bleed into whichever test happens to
        run next — not just the tests that "know" they refuse. A per-method
        `finally: self._git("reset")` is a policy fix (only as good as every
        author remembering it); `tearDown` is the structural one — it runs
        after every test unconditionally, reset is idempotent when nothing is
        staged, so this is a no-op for the passing tests.
        """
        self._git("reset")

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

    # -- main-guard lint tests (T-2314) --------------------------------------

    def test_smoke_main_guard_refuses_stranded_class(self) -> None:
        """A staged tools/*.py file with a class stranded below a `__main__`
        guard must be refused at real commit time."""
        (self.repo / "tools").mkdir(exist_ok=True)
        content = (
            b'import unittest\n'
            b'if __name__ == "__main__":\n'
            b'    unittest.main()\n'
            b'\n'
            b'class TestStranded(unittest.TestCase):\n'
            b'    pass\n'
        )
        self._write("tools/test_t2314_stranded.py", content)
        result = self._commit("add stranded test class", "tools/test_t2314_stranded.py")
        # No try/finally needed — tearDown() resets the index unconditionally
        # after every test in this class (round-1 REBOUND fix).
        self.assertNotEqual(
            result.returncode,
            0,
            "pre-commit hook should have refused a stranded post-guard class",
        )
        self.assertIn("main-guard lint violation", result.stderr)
        self.assertIn("TestStranded", result.stderr)

    def test_smoke_main_guard_passes_clean_tools_file(self) -> None:
        """A staged tools/*.py file with the guard as the last top-level
        statement must pass."""
        (self.repo / "tools").mkdir(exist_ok=True)
        content = (
            b'import unittest\n'
            b'class TestClean(unittest.TestCase):\n'
            b'    def test_a(self):\n'
            b'        pass\n'
            b'\n'
            b'if __name__ == "__main__":\n'
            b'    unittest.main()\n'
        )
        self._write("tools/test_t2314_clean.py", content)
        result = self._commit("add clean test class", "tools/test_t2314_clean.py")
        self.assertEqual(
            result.returncode,
            0,
            f"clean tools/*.py file should pass; stderr={result.stderr!r}",
        )

    def test_smoke_main_guard_ignores_non_tools_file(self) -> None:
        """A stranded-guard-shaped file OUTSIDE tools/ must not be scanned —
        staged-blob scoping is to tools/*.py only."""
        content = (
            b'import unittest\n'
            b'if __name__ == "__main__":\n'
            b'    unittest.main()\n'
            b'\n'
            b'class TestStranded(unittest.TestCase):\n'
            b'    pass\n'
        )
        self._write("not_scanned.py", content)
        result = self._commit("add out-of-scope stranded file", "not_scanned.py")
        self.assertEqual(
            result.returncode,
            0,
            f"non-tools/ file must not be main-guard-scanned; stderr={result.stderr!r}",
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


class TestQuotedStagedPathsAreScanned(unittest.TestCase):
    """T-2325: a staged file whose NAME git quotes must not skip either scan.

    With `core.quotePath` at its default, `git diff --cached --name-only`
    renders a non-UTF-8 filename ESCAPED AND DOUBLE-QUOTED, so the returned
    string starts with a literal `"`. That defeated both consumers of
    `_staged_files()`: the byte-sequence loop handed the bogus path to
    `git show` (which fails -> silent skip), and `_main_guard_lint_findings`
    filtered on `p.startswith("tools/")`, which is false for the quoted form.

    THESE TESTS USE REAL GIT ON REAL FILES DELIBERATELY. A unit test over a
    synthetic list of path strings exercises neither git's quoting nor its
    binary detection and would pass against the bug — the ticket's Amendment H
    bar is to observe the guard MISSING the file before the fix and CATCHING it
    after. Each case is paired with an ordinary-named CONTROL: without one, a
    refusal cannot be distinguished from a refusal for some unrelated reason,
    and a MISS cannot be distinguished from a broken harness.

    This class does not reuse TestGitSmoke's helpers because those run git with
    text=True and str paths, which cannot express a non-UTF-8 filename.
    """

    ODD_NAME = b"tools/bad_\xff\xfe_name.py"
    ORDINARY = b"tools/ordinary_control.py"

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmpdir.name)
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        self._git("config", "core.hooksPath", str(HOOKS_DIR))
        (self.repo / "tools").mkdir()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _git(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=self.repo, capture_output=True, check=False
        )

    def _stage(self, name: bytes, content: bytes) -> bool:
        """Write + stage a file by raw bytes name. False if the FS rejects it."""
        try:
            with open(os.path.join(os.fsencode(self.repo), name), "wb") as fh:
                fh.write(content)
        except (OSError, ValueError):
            return False
        return self._git("add", "--", name).returncode == 0

    def _commit(self) -> subprocess.CompletedProcess:
        return self._git("commit", "-m", "test: staged path scan")

    def test_quoted_path_is_still_byte_sequence_scanned(self) -> None:
        """Contaminated content under a git-quoted NAME must refuse the commit."""
        body = b"# note about " + (b"HERME" + b"S.md") + b"\n"
        if not self._stage(self.ODD_NAME, body):
            self.skipTest("filesystem will not hold a non-UTF-8 filename")
        listing = self._git("diff", "--cached", "--name-only").stdout
        self.assertTrue(
            listing.strip().startswith(b'"'),
            f"precondition: git should quote this path, got {listing!r}",
        )
        result = self._commit()
        self.assertNotEqual(
            result.returncode, 0, "quoted-path file must not bypass the scan"
        )
        self.assertIn(b"discipline violation", result.stdout + result.stderr)

    def test_ordinary_path_control_is_scanned(self) -> None:
        """CONTROL: same content, ordinary name — proves the harness bites."""
        body = b"# note about " + (b"HERME" + b"S.md") + b"\n"
        self.assertTrue(self._stage(self.ORDINARY, body))
        result = self._commit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"discipline violation", result.stdout + result.stderr)

    def test_clean_quoted_path_still_commits(self) -> None:
        """The fix must not refuse an oddly-named file that is CLEAN."""
        if not self._stage(self.ODD_NAME, b"# nothing to see\n"):
            self.skipTest("filesystem will not hold a non-UTF-8 filename")
        result = self._commit()
        self.assertEqual(
            result.returncode,
            0,
            f"clean odd-named file should commit; stderr={result.stderr!r}",
        )

    def test_quoted_path_reaches_main_guard_lint(self) -> None:
        """The OTHER consumer of _staged_files: the tools/*.py prefix filter."""
        body = (
            b'if __name__ == "__main__":\n    pass\n\n\nclass Stranded:\n    pass\n'
        )
        if not self._stage(self.ODD_NAME, body):
            self.skipTest("filesystem will not hold a non-UTF-8 filename")
        result = self._commit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"main-guard lint violation", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
