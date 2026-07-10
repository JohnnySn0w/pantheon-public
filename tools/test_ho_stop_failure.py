"""Unit tests for tools/hooks/ho_stop_failure.py.

Run from repo root:
    python3 -m unittest tools.test_ho_stop_failure -v

Covers:
  - rate_limit event writes state file with correct keys + values
  - non-rate_limit StopFailure event does NOT write state file
  - malformed JSON stdin does not crash (exits, no file)
  - empty stdin does not crash
  - resets_at parse fallback on unknown error_message format (written_at + 5h)
  - resets_at parsed from same-day "resets H:MMpm" pattern
  - resets_at parsed from day-of-week "resets Mon H:MMam" pattern
  - parsed time "in the past" relative to written_at → advances by 24h
  - state file JSON has all required keys
  - self-audit: hook source must not contain unescaped HERMES trigger

Fixture-string note: the unescaped HERMES trigger is constructed at runtime
via source-time string concatenation (`b"HERME" + b"S.md"` / `"HERME" + "S.md"`)
so the contiguous bytes never appear in this source file. Byte-sequence discipline
per `/home/claude/CLAUDE.md` and `tools/_git_utils.py`.

PANTHEON_STATE_DIR env override: the hook script reads this env var (defaults to
~/.local/state/pantheon/) so tests can redirect output to a tempdir without manual
prep. The hook script does mkdir -p on whatever path resolves.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = REPO_ROOT / "tools" / "hooks" / "ho_stop_failure.py"
STATE_FILE_NAME = "ho_limit_hit.json"

sys.path.insert(0, str(REPO_ROOT))
from tools._git_utils import _HERMES_PATTERN  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_hook(
    event: dict,
    state_dir: str,
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run the hook script with the given event JSON on stdin.

    Overrides PANTHEON_STATE_DIR so output lands in a tempdir.
    """
    env = os.environ.copy()
    env["PANTHEON_STATE_DIR"] = state_dir
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def _rate_limit_event(
    *,
    error_message: str = "You've hit your session limit · resets 3:45pm",
    session_id: str = "sess-abc123",
    transcript_path: str = "/tmp/transcript.jsonl",
) -> dict:
    return {
        "hook_event_name": "StopFailure",
        "error_type": "rate_limit",
        "error_message": error_message,
        "session_id": session_id,
        "transcript_path": transcript_path,
        "cwd": "/home/claude",
        "permission_mode": "default",
    }


def _non_rate_limit_event(error_type: str = "server_error") -> dict:
    return {
        "hook_event_name": "StopFailure",
        "error_type": error_type,
        "error_message": "Internal server error",
        "session_id": "sess-xyz",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/home/claude",
        "permission_mode": "default",
    }


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------


class TestRateLimitEventWritesStateFile(unittest.TestCase):
    def test_state_file_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _run_hook(_rate_limit_event(), tmpdir)
            self.assertEqual(result.returncode, 0, result.stderr)
            state_path = Path(tmpdir) / STATE_FILE_NAME
            self.assertTrue(state_path.exists(), "state file must be created for rate_limit event")

    def test_state_file_has_all_required_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _run_hook(_rate_limit_event(), tmpdir)
            state = json.loads((Path(tmpdir) / STATE_FILE_NAME).read_text())
            for key in ("error_type", "error_message", "transcript_path", "session_id", "resets_at", "written_at"):
                self.assertIn(key, state, f"state file missing key: {key}")

    def test_state_file_values_match_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            event = _rate_limit_event(session_id="sess-test-42", transcript_path="/home/claude/.claude/transcripts/t.jsonl")
            _run_hook(event, tmpdir)
            state = json.loads((Path(tmpdir) / STATE_FILE_NAME).read_text())
            self.assertEqual(state["error_type"], "rate_limit")
            self.assertEqual(state["session_id"], "sess-test-42")
            self.assertEqual(state["transcript_path"], "/home/claude/.claude/transcripts/t.jsonl")

    def test_written_at_is_recent_unix_epoch(self) -> None:
        before = int(time.time())
        with tempfile.TemporaryDirectory() as tmpdir:
            _run_hook(_rate_limit_event(), tmpdir)
            state = json.loads((Path(tmpdir) / STATE_FILE_NAME).read_text())
        after = int(time.time())
        self.assertIsInstance(state["written_at"], (int, float))
        self.assertGreaterEqual(state["written_at"], before)
        self.assertLessEqual(state["written_at"], after + 2)


class TestNonRateLimitEventNoFile(unittest.TestCase):
    def test_server_error_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _run_hook(_non_rate_limit_event("server_error"), tmpdir)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(
                (Path(tmpdir) / STATE_FILE_NAME).exists(),
                "state file must NOT be created for server_error event",
            )

    def test_billing_error_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _run_hook(_non_rate_limit_event("billing_error"), tmpdir)
            self.assertFalse((Path(tmpdir) / STATE_FILE_NAME).exists())

    def test_auth_error_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _run_hook(_non_rate_limit_event("authentication_failed"), tmpdir)
            self.assertFalse((Path(tmpdir) / STATE_FILE_NAME).exists())


class TestMalformedInput(unittest.TestCase):
    def _run_raw(self, stdin_text: str, state_dir: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["PANTHEON_STATE_DIR"] = state_dir
        return subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input=stdin_text,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )

    def test_malformed_json_does_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self._run_raw("{not valid json{{", tmpdir)
            # Must exit (any code) without an unhandled exception traceback
            self.assertNotIn("Traceback", result.stderr, "unhandled exception in hook")
            self.assertFalse((Path(tmpdir) / STATE_FILE_NAME).exists())

    def test_empty_stdin_does_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self._run_raw("", tmpdir)
            self.assertNotIn("Traceback", result.stderr, "unhandled exception in hook")
            self.assertFalse((Path(tmpdir) / STATE_FILE_NAME).exists())

    def test_json_missing_error_type_does_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self._run_raw(json.dumps({"session_id": "x"}), tmpdir)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse((Path(tmpdir) / STATE_FILE_NAME).exists())


# ---------------------------------------------------------------------------
# resets_at parsing
# ---------------------------------------------------------------------------


class TestResetsAtParsing(unittest.TestCase):
    """Exercises the human-readable timestamp parser in the hook script.

    We inject a fake written_at via an env var (PANTHEON_WRITTEN_AT_OVERRIDE)
    so we can control "now" for deterministic relative-time assertions.
    """

    def _state(self, error_message: str, written_at_override: int | None = None) -> dict:
        """Run hook with given error_message; return parsed state dict."""
        event = _rate_limit_event(error_message=error_message)
        extra = {}
        if written_at_override is not None:
            extra["PANTHEON_WRITTEN_AT_OVERRIDE"] = str(written_at_override)
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _run_hook(event, tmpdir, extra_env=extra)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads((Path(tmpdir) / STATE_FILE_NAME).read_text())

    def test_unknown_format_falls_back_to_written_at_plus_5h(self) -> None:
        state = self._state("You've hit your limit · resets unknown-format-here")
        self.assertIsNotNone(state["resets_at"])
        diff = state["resets_at"] - state["written_at"]
        self.assertAlmostEqual(diff, 5 * 3600, delta=5, msg="fallback must be written_at + 5h")

    def test_same_day_hhmm_am(self) -> None:
        # Pin written_at to 2026-05-03 08:00:00 UTC (a Sunday)
        # Message says "resets 10:30am" — same day, 10:30 local.
        # Since VPS may be in any tz, we just verify the delta is ~2.5h if
        # the local time is UTC (most common on VPS). We verify resets_at > written_at
        # and < written_at + 24h, and that it's NOT the fallback.
        anchor = 1746259200  # 2026-05-03 08:00:00 UTC
        state = self._state("You've hit your session limit · resets 10:30am", written_at_override=anchor)
        self.assertIsNotNone(state["resets_at"])
        # Must not be the 5h fallback (that would be anchor + 18000)
        self.assertNotAlmostEqual(state["resets_at"] - anchor, 5 * 3600, delta=60)
        # Must be positive offset and within 24h
        self.assertGreater(state["resets_at"], anchor)
        self.assertLess(state["resets_at"], anchor + 86400 + 60)

    def test_same_day_hhmm_pm(self) -> None:
        # Anchor: 2026-05-03 10:00:00 UTC; message: "resets 3:45pm"
        anchor = 1746266400  # 2026-05-03 10:00:00 UTC
        state = self._state("You've hit your session limit · resets 3:45pm", written_at_override=anchor)
        self.assertIsNotNone(state["resets_at"])
        self.assertGreater(state["resets_at"], anchor)
        self.assertLess(state["resets_at"], anchor + 86400 + 60)

    def test_day_of_week_prefix(self) -> None:
        # Anchor: 2026-05-03 (Sunday) 23:00 UTC; message: "resets Mon 12:00am"
        # Should advance to Monday 2026-05-04 00:00 local.
        anchor = 1746313200  # 2026-05-03 23:00:00 UTC
        state = self._state("You've hit your weekly limit · resets Mon 12:00am", written_at_override=anchor)
        self.assertIsNotNone(state["resets_at"])
        self.assertGreater(state["resets_at"], anchor)
        self.assertLess(state["resets_at"], anchor + 7 * 86400 + 60)

    def test_past_time_advances_by_24h(self) -> None:
        """If the parsed local reset time is in the past relative to written_at,
        the hook must advance it by 24h rather than returning a stale epoch.

        Anchor: 2026-05-03 16:00:00 UTC.
        Message: "resets 3:45pm" (15:45 local = 3:45pm).
        15:45 UTC < 16:00 UTC → 15:45 is in the past → advance to next day 15:45.
        Expected resets_at ≈ anchor + (~23h 45min).
        """
        anchor = 1746288000  # 2026-05-03 16:00:00 UTC
        state = self._state("You've hit your session limit · resets 3:45pm", written_at_override=anchor)
        self.assertIsNotNone(state["resets_at"])
        # Must be at least 20h ahead (clearly the next-day slot, not the past slot)
        self.assertGreater(state["resets_at"], anchor + 20 * 3600)
        # Must be within 25h (so it picked the +24h path, not the +48h path)
        self.assertLess(state["resets_at"], anchor + 25 * 3600)

    def test_no_resets_suffix_uses_fallback(self) -> None:
        # Message has no "resets" keyword at all
        state = self._state("You've hit your session limit")
        diff = state["resets_at"] - state["written_at"]
        self.assertAlmostEqual(diff, 5 * 3600, delta=5)


# ---------------------------------------------------------------------------
# State dir creation
# ---------------------------------------------------------------------------


class TestStateDirCreation(unittest.TestCase):
    def test_nested_state_dir_created_automatically(self) -> None:
        """Hook must mkdir -p the state dir even if it doesn't exist."""
        with tempfile.TemporaryDirectory() as base:
            nested = os.path.join(base, "deeply", "nested", "dir")
            result = _run_hook(_rate_limit_event(), nested)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(nested) / STATE_FILE_NAME).exists())


# ---------------------------------------------------------------------------
# Self-audit: hook source must be clean of unescaped HERMES trigger
# ---------------------------------------------------------------------------


class TestSelfAudit(unittest.TestCase):
    def _assert_clean(self, path: Path) -> None:
        trigger = b"HERME" + b"S.md"
        content = path.read_bytes()
        self.assertNotIn(
            trigger,
            content,
            f"{path.name} must not contain the unescaped trigger (byte-sequence discipline)",
        )

    def test_hook_source_clean(self) -> None:
        self._assert_clean(HOOK_SCRIPT)

    def test_this_test_file_clean(self) -> None:
        self._assert_clean(Path(__file__))


# ---------------------------------------------------------------------------
# /tmp mirror behaviour (T-0072)
# ---------------------------------------------------------------------------

_TMP_MIRROR_DEFAULT = Path("/tmp/pantheon_ho_limit_hit.json")


class TestTmpMirror(unittest.TestCase):
    """Covers the /tmp mirror write added in T-0072.

    The hook honours PANTHEON_TMP_MIRROR_OVERRIDE to redirect the mirror path
    so subprocess-level tests can control it without mock.patch.
    """

    def setUp(self) -> None:
        # Clean up any leftover mirror file from a previous test run.
        if _TMP_MIRROR_DEFAULT.exists():
            _TMP_MIRROR_DEFAULT.unlink()

    def tearDown(self) -> None:
        if _TMP_MIRROR_DEFAULT.exists():
            _TMP_MIRROR_DEFAULT.unlink()

    def _run_with_mirror_override(
        self,
        event: dict,
        state_dir: str,
        mirror_path: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        env = extra_env.copy() if extra_env else {}
        if mirror_path is not None:
            env["PANTHEON_TMP_MIRROR_OVERRIDE"] = mirror_path
        return _run_hook(event, state_dir, extra_env=env)

    def test_happy_path_both_files_written_and_mirror_mode(self) -> None:
        """Happy path: canonical and mirror both written; mirror mode is 0o644."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with tempfile.NamedTemporaryFile(
                suffix=".json", delete=False
            ) as mirror_f:
                mirror_path = mirror_f.name
            try:
                result = self._run_with_mirror_override(
                    _rate_limit_event(), tmpdir, mirror_path=mirror_path
                )
                self.assertEqual(result.returncode, 0, result.stderr)

                # Canonical written
                canonical = Path(tmpdir) / STATE_FILE_NAME
                self.assertTrue(canonical.exists(), "canonical state file must exist")

                # Mirror written
                mirror = Path(mirror_path)
                self.assertTrue(mirror.exists(), "mirror file must exist")

                # Mode is 0o644
                mode = mirror.stat().st_mode & 0o777
                self.assertEqual(
                    mode,
                    0o644,
                    f"mirror file mode must be 0o644, got 0o{mode:03o}",
                )

                # Identical JSON payload
                canonical_data = json.loads(canonical.read_text())
                mirror_data = json.loads(mirror.read_text())
                self.assertEqual(
                    canonical_data,
                    mirror_data,
                    "mirror JSON must be identical to canonical JSON",
                )
            finally:
                Path(mirror_path).unlink(missing_ok=True)

    def test_mirror_failure_does_not_block_canonical(self) -> None:
        """Mirror-only failure: canonical still written; hook still exits 0; mirror absent."""
        # Point mirror at a path whose parent directory does not exist — guarantees OSError.
        bad_mirror = "/nonexistent_pantheon_test_dir_T0072/pantheon_mirror.json"
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self._run_with_mirror_override(
                _rate_limit_event(), tmpdir, mirror_path=bad_mirror
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            # Canonical must be written
            canonical = Path(tmpdir) / STATE_FILE_NAME
            self.assertTrue(
                canonical.exists(), "canonical file must be written despite mirror failure"
            )

            # Mirror must NOT exist
            self.assertFalse(
                Path(bad_mirror).exists(), "mirror file must not exist after OSError"
            )

            # Failure logged to stderr, no traceback
            self.assertIn("mirror", result.stderr.lower(), "mirror failure must be logged to stderr")
            self.assertNotIn("Traceback", result.stderr, "no unhandled exception allowed")

    def test_canonical_failure_mirror_not_run(self) -> None:
        """Canonical write failure: hook exits 0 and mirror is NOT written."""
        # Canonical dir that cannot be created even as root: a path *under an
        # existing regular file* makes the hook's mkdir(parents=True) raise
        # NotADirectoryError (an OSError → the hook's early-return path)
        # regardless of privilege. A bare "/nonexistent/state" is creatable by
        # root (e.g. in a container), which made this test pass only as non-root.
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as mirror_f:
            mirror_path = mirror_f.name
        with tempfile.NamedTemporaryFile(suffix=".notadir", delete=False) as notdir_f:
            bad_canonical_dir = os.path.join(notdir_f.name, "state")
        try:
            result = self._run_with_mirror_override(
                _rate_limit_event(), bad_canonical_dir, mirror_path=mirror_path
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            # Mirror must NOT be written — hook returned early on canonical failure.
            # Assert unconditionally that the mirror is empty (NamedTemporaryFile
            # pre-creates it empty; if the hook incorrectly wrote to it the content
            # would be non-empty JSON and this assertion would catch it).
            mirror = Path(mirror_path)
            content = mirror.read_text() if mirror.exists() else ""
            self.assertEqual(
                content.strip(),
                "",
                "mirror must be empty when canonical write failed",
            )
        finally:
            Path(mirror_path).unlink(missing_ok=True)
            Path(notdir_f.name).unlink(missing_ok=True)

    def test_idempotence_overwrite_not_append(self) -> None:
        """Running the hook twice produces a fresh mirror each time (overwrite, not append)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with tempfile.NamedTemporaryFile(
                suffix=".json", delete=False
            ) as mirror_f:
                mirror_path = mirror_f.name
            try:
                # First run
                result1 = self._run_with_mirror_override(
                    _rate_limit_event(), tmpdir, mirror_path=mirror_path,
                    extra_env={"PANTHEON_WRITTEN_AT_OVERRIDE": "1000000"},
                )
                self.assertEqual(result1.returncode, 0, result1.stderr)
                data1 = json.loads(Path(mirror_path).read_text())

                # Second run with different written_at
                result2 = self._run_with_mirror_override(
                    _rate_limit_event(), tmpdir, mirror_path=mirror_path,
                    extra_env={"PANTHEON_WRITTEN_AT_OVERRIDE": "2000000"},
                )
                self.assertEqual(result2.returncode, 0, result2.stderr)
                content2 = Path(mirror_path).read_text()
                data2 = json.loads(content2)

                # written_at reflects the second run, not doubled content
                self.assertEqual(data2["written_at"], 2000000, "second run must overwrite mirror")
                self.assertEqual(data1["written_at"], 1000000, "first run value captured correctly")
                self.assertNotEqual(data1["written_at"], data2["written_at"])

                # Content is valid JSON exactly once (no append artefacts)
                # Parsing above would have raised if doubled/corrupted
            finally:
                Path(mirror_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
