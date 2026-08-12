"""Unit + smoke tests for tools/ho_resumer.py.

Run from repo root:
    python3 -m unittest tools.test_ho_resumer -v

Covers:
  - State file missing → bail clean
  - State file stale mtime (>24h) → bail
  - resets_at in the past → bail (stale cap-hit)
  - resets_at far future (> now + 90s) → bail (not yet time, next poll)
  - resets_at imminent (within window) → fire resume
  - HO already running (pgrep match) → bail
  - claude --resume invoked with correct argv (mocked subprocess)
  - State file rotated post-fire (.consumed.<epoch> suffix)
  - Self-audit: new source files must not contain the unescaped HERMES trigger

Fixture-string note: the unescaped HERMES trigger is constructed at runtime via
source-time string concatenation (`b"HERME" + b"S.md"` / `"HERME" + "S.md"`)
so the contiguous 9-byte sequence never appears in this source file.
"""

from __future__ import annotations

import json
import os
import pwd
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch, call

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.ho_resumer as resumer
from tools.ho_resumer import (
    _DEFAULT_STATE_FILE_NAME,
    RESUMER_PROMPT,
    _load_state,
    _is_stale_mtime,
    _ho_already_running,
    _rotate_state_file,
    main,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_state(path: Path, resets_at: int, session_id: str = "sess-abc123") -> None:
    now = int(time.time())
    state = {
        "error_type": "rate_limit",
        "error_message": "You've hit your session limit · resets 3:45pm",
        "transcript_path": "/home/claude/.claude/projects/test/transcript.json",
        "session_id": session_id,
        "resets_at": resets_at,
        "written_at": now,
    }
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# _load_state
# ---------------------------------------------------------------------------


class TestLoadState(unittest.TestCase):
    def test_missing_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ho_limit_hit.json"
            result = _load_state(path)
            self.assertIsNone(result)

    def test_valid_file_returns_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ho_limit_hit.json"
            _write_state(path, int(time.time()) + 300)
            result = _load_state(path)
            self.assertIsNotNone(result)
            self.assertEqual(result["error_type"], "rate_limit")
            self.assertEqual(result["session_id"], "sess-abc123")

    def test_malformed_json_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ho_limit_hit.json"
            path.write_text("not json {{{", encoding="utf-8")
            result = _load_state(path)
            self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _is_stale_mtime
# ---------------------------------------------------------------------------


class TestIsStaleMtime(unittest.TestCase):
    def test_fresh_mtime_not_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ho_limit_hit.json"
            path.write_text("{}", encoding="utf-8")
            # mtime is just now → not stale
            self.assertFalse(_is_stale_mtime(path))

    def test_old_mtime_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ho_limit_hit.json"
            path.write_text("{}", encoding="utf-8")
            # Backdate mtime by 25 hours
            old_ts = time.time() - (25 * 3600)
            os.utime(path, (old_ts, old_ts))
            self.assertTrue(_is_stale_mtime(path))

    def test_exactly_24h_ago_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ho_limit_hit.json"
            path.write_text("{}", encoding="utf-8")
            # Exactly 24h + 1s ago → stale
            old_ts = time.time() - (24 * 3600 + 1)
            os.utime(path, (old_ts, old_ts))
            self.assertTrue(_is_stale_mtime(path))


# ---------------------------------------------------------------------------
# _ho_already_running
# ---------------------------------------------------------------------------


class TestHoAlreadyRunning(unittest.TestCase):
    def test_not_running_when_pgrep_empty(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            self.assertFalse(_ho_already_running())
            args = mock_run.call_args[0][0]
            # T-1956: scoped by uid — pgrep -u <uid> -f <pattern>.
            self.assertEqual(args[0], "pgrep")
            self.assertEqual(args[1], "-u")
            self.assertEqual(args[2], str(os.getuid()))
            self.assertEqual(args[3], "-f")
            self.assertIn("--resume", args[4])

    def test_running_when_pgrep_returns_pid(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="12345\n")
            self.assertTrue(_ho_already_running())


# ---------------------------------------------------------------------------
# _ho_already_running — cross-account false positive (T-1956), live regression
# ---------------------------------------------------------------------------


class TestHoAlreadyRunningCrossAccountRegression(unittest.TestCase):
    """Live regression against REAL pgrep (no subprocess mock) proving
    uid-scoping is what fixes the cross-account false positive. Per D11,
    pgrep's matching semantics are the third-party surface under test here,
    not something to assume from a mock.

    Fixture note: `sleep 600 --resume <marker>` does NOT work — GNU sleep
    rejects `--resume` as an unrecognized option and exits before pgrep can
    ever observe it. `bash -c "sleep 600" claude --resume-fixture-<unique>`
    ALSO does not work: when a `-c` script is a single simple external
    command, bash tail-call-execs it in place, replacing its own process
    image (and its own argv — including our trailing $0/$1 marker tokens)
    with `sleep`'s. Verified empirically (a standalone probe showed
    /proc/<pid>/cmdline collapsing to just `sleep 600`). Using a compound
    script (`sleep 600; :`) forces bash to fork a child for `sleep` and run
    the builtin `:` after, so bash itself never image-replaces and stays
    alive with its ORIGINAL argv — marker tokens intact — for the fixture's
    lifetime, without needing the actual `claude` binary.
    """

    def setUp(self) -> None:
        self.marker = f"resume-fixture-{uuid.uuid4().hex}"
        self.proc = subprocess.Popen(
            ["bash", "-c", "sleep 600; :", "claude", f"--{self.marker}"],
            start_new_session=True,
        )
        deadline = time.time() + 2
        while time.time() < deadline:
            probe = subprocess.run(
                ["pgrep", "-f", self.marker], capture_output=True, text=True
            )
            if probe.stdout.strip():
                return
            time.sleep(0.05)
        os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        self.proc.wait(timeout=5)
        self.fail("fixture process never became visible to pgrep")

    def tearDown(self) -> None:
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait(timeout=5)

    def test_own_uid_matches_via_real_pgrep(self) -> None:
        """Positive control: the fixture's real pid must appear among the
        matches for our OWN uid against the real _PGREP_PATTERN. Assert
        membership, not exclusivity — this account may legitimately have an
        unrelated `claude --resume` live at test time, and that is not a
        false positive to guard against here."""
        result = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-f", resumer._PGREP_PATTERN],
            capture_output=True, text=True,
        )
        matched_pids = {int(p) for p in result.stdout.split()}
        self.assertIn(self.proc.pid, matched_pids)
        self.assertTrue(_ho_already_running())

    def test_foreign_uid_does_not_match_via_real_pgrep(self) -> None:
        """T-1956 regression: the SAME live argv, queried under a different
        uid (`nobody` — real, valid, runs no claude processes), must NOT
        match. This is the cross-account false-positive shape: a sibling
        account's `claude --resume` has a different uid than the HO
        account's own, so uid-scoping excludes it.

        `nobody`'s uid is resolved via pwd, not hardcoded — and NOT
        os.getuid()+1, since account uids on this box are adjacent and +1
        could be a real sibling agent account legitimately running
        `claude --resume` at test time (a flaky false failure)."""
        foreign_uid = pwd.getpwnam("nobody").pw_uid

        # Pin "pgrep accepted the uid and found nothing" (returncode 1) apart
        # from "pgrep choked on the uid" (returncode 2+) — _ho_already_running
        # treats any non-zero exit the same way, so a silently-erroring pgrep
        # would pass a naive negative assertion vacuously.
        direct = subprocess.run(
            ["pgrep", "-u", str(foreign_uid), "-f", resumer._PGREP_PATTERN],
            capture_output=True, text=True,
        )
        self.assertIn(
            direct.returncode, (0, 1),
            f"pgrep usage error under foreign uid: {direct.stderr!r}",
        )
        self.assertEqual(direct.returncode, 1, "must find no match under the foreign uid")

        with patch("tools.ho_resumer.os.getuid", return_value=foreign_uid):
            self.assertFalse(_ho_already_running())


# ---------------------------------------------------------------------------
# _rotate_state_file
# ---------------------------------------------------------------------------


class TestRotateStateFile(unittest.TestCase):
    def test_rotated_file_has_consumed_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ho_limit_hit.json"
            path.write_text('{"session_id":"x"}', encoding="utf-8")
            consumed = _rotate_state_file(path)
            self.assertFalse(path.exists(), "original state file should be gone")
            self.assertTrue(consumed.exists(), "consumed file should exist")
            self.assertIn(".consumed.", consumed.name)
            # Verify suffix is numeric (epoch)
            suffix = consumed.name.split(".consumed.")[-1]
            self.assertTrue(suffix.isdigit(), f"suffix should be numeric epoch, got {suffix!r}")

    def test_rotated_file_preserves_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ho_limit_hit.json"
            content = '{"session_id":"preserve-me"}'
            path.write_text(content, encoding="utf-8")
            consumed = _rotate_state_file(path)
            self.assertEqual(consumed.read_text(encoding="utf-8"), content)


# ---------------------------------------------------------------------------
# main() — integration paths via env override
# ---------------------------------------------------------------------------


class TestMainMissingStateFile(unittest.TestCase):
    def test_exits_zero_when_no_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"PANTHEON_STATE_DIR": tmp}):
                # Should exit 0 cleanly — no file to process
                result = resumer.main()
                self.assertEqual(result, 0)


class TestMainStaleMtime(unittest.TestCase):
    def test_exits_zero_on_stale_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / _DEFAULT_STATE_FILE_NAME
            _write_state(state_path, int(time.time()) + 300)
            # Backdate mtime by 25h
            old_ts = time.time() - (25 * 3600)
            os.utime(state_path, (old_ts, old_ts))
            with patch.dict(os.environ, {"PANTHEON_STATE_DIR": tmp}):
                result = resumer.main()
                self.assertEqual(result, 0)
            # State file should NOT be rotated (stale = don't consume)
            self.assertTrue(state_path.exists())


class TestMainResetsAtInPast(unittest.TestCase):
    def test_exits_zero_when_resets_at_ancient(self) -> None:
        """resets_at > 24h in the past → secondary stale check bails clean."""
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / _DEFAULT_STATE_FILE_NAME
            # resets_at is 25 hours in the past — ancient cap-hit
            _write_state(state_path, int(time.time()) - (25 * 3600))
            with patch.dict(os.environ, {"PANTHEON_STATE_DIR": tmp}):
                result = resumer.main()
                self.assertEqual(result, 0)
            # State file should NOT be rotated (bailed, didn't fire)
            self.assertTrue(state_path.exists())

    def test_resets_at_10min_past_fires(self) -> None:
        """resets_at 10min in the past + fresh mtime → fire window has arrived."""
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / _DEFAULT_STATE_FILE_NAME
            # resets_at 10 min ago → fire_at (resets_at+90s) is 8.5 min in the past
            _write_state(state_path, int(time.time()) - 600)

            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""

            with patch.dict(os.environ, {
                "PANTHEON_STATE_DIR": tmp,
                "PANTHEON_CLAUDE_BIN": "/usr/local/bin/claude",
            }):
                with patch("tools.ho_resumer._ho_already_running", return_value=False):
                    with patch("subprocess.run", return_value=mock_result) as mock_sub:
                        result = resumer.main()

            self.assertEqual(result, 0)
            self.assertTrue(mock_sub.called)


class TestMainFarFuture(unittest.TestCase):
    def test_exits_zero_when_far_future(self) -> None:
        """resets_at + 90s is more than 90s away → bail (next poll will retry)."""
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / _DEFAULT_STATE_FILE_NAME
            # resets_at is 10 minutes from now → resets_at + 90s = 11.5min from now
            _write_state(state_path, int(time.time()) + 600)
            with patch.dict(os.environ, {"PANTHEON_STATE_DIR": tmp}):
                result = resumer.main()
                self.assertEqual(result, 0)
            # State file should still be present (not consumed)
            self.assertTrue(state_path.exists())


class TestMainHoAlreadyRunning(unittest.TestCase):
    def test_exits_zero_when_ho_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / _DEFAULT_STATE_FILE_NAME
            # resets_at + 90s is in the past (would normally fire)
            _write_state(state_path, int(time.time()) - 200)
            # Override now so resets_at is in fire window
            now_override = int(time.time())
            resets_at = now_override - 200  # already past
            _write_state(state_path, resets_at)
            with patch.dict(os.environ, {"PANTHEON_STATE_DIR": tmp}):
                with patch("tools.ho_resumer._ho_already_running", return_value=True):
                    result = resumer.main()
                    self.assertEqual(result, 0)
            # State file should NOT be rotated (we bailed, didn't fire)
            self.assertTrue(state_path.exists())


class TestMainImminentFire(unittest.TestCase):
    """resets_at is in the past (window has refreshed) → should fire resume."""

    def test_fires_with_correct_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / _DEFAULT_STATE_FILE_NAME
            session_id = "sess-fire-test-001"
            # resets_at 5 minutes ago → resets_at + 90s is in the past → fire
            _write_state(state_path, int(time.time()) - 300, session_id=session_id)

            fake_claude = "/usr/local/bin/claude"
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""

            with patch.dict(os.environ, {
                "PANTHEON_STATE_DIR": tmp,
                "PANTHEON_CLAUDE_BIN": fake_claude,
            }):
                with patch("tools.ho_resumer._ho_already_running", return_value=False):
                    with patch("subprocess.run", return_value=mock_result) as mock_sub:
                        result = resumer.main()

            self.assertEqual(result, 0)
            # Verify subprocess.run was called
            self.assertTrue(mock_sub.called)
            call_args = mock_sub.call_args[0][0]
            self.assertEqual(call_args[0], fake_claude)
            self.assertEqual(call_args[1], "--resume")
            self.assertEqual(call_args[2], session_id)
            self.assertEqual(call_args[3], "-p")
            self.assertEqual(call_args[4], RESUMER_PROMPT)

    def test_state_file_rotated_after_fire(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / _DEFAULT_STATE_FILE_NAME
            _write_state(state_path, int(time.time()) - 300)

            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""

            with patch.dict(os.environ, {
                "PANTHEON_STATE_DIR": tmp,
                "PANTHEON_CLAUDE_BIN": "/usr/local/bin/claude",
            }):
                with patch("tools.ho_resumer._ho_already_running", return_value=False):
                    with patch("subprocess.run", return_value=mock_result):
                        resumer.main()

            # Original state file should be gone
            self.assertFalse(state_path.exists(), "original state file should be rotated away")
            # A consumed file should exist
            consumed_files = list(Path(tmp).glob("ho_limit_hit.json.consumed.*"))
            self.assertEqual(len(consumed_files), 1)
            suffix = consumed_files[0].name.split(".consumed.")[-1]
            self.assertTrue(suffix.isdigit())

    def test_non_zero_claude_exit_retries_once_then_returns_one(self) -> None:
        # T-0909: a persistent non-zero exit (HO not running) triggers ONE bounded
        # retry, then rotates and returns 1. Both attempts fail here.
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / _DEFAULT_STATE_FILE_NAME
            _write_state(state_path, int(time.time()) - 300)

            mock_result = MagicMock()
            mock_result.returncode = 1
            mock_result.stdout = ""
            mock_result.stderr = "error: session not found"

            with patch.dict(os.environ, {
                "PANTHEON_STATE_DIR": tmp,
                "PANTHEON_CLAUDE_BIN": "/usr/local/bin/claude",
            }):
                with patch("tools.ho_resumer._ho_already_running", return_value=False):
                    with patch("tools.ho_resumer.time.sleep") as mock_sleep:
                        with patch("subprocess.run", return_value=mock_result) as mock_sub:
                            result = resumer.main()

            self.assertEqual(result, 1)
            # Retried exactly once before giving up (two resume attempts, one sleep).
            self.assertEqual(mock_sub.call_count, 2)
            self.assertEqual(mock_sleep.call_count, 1)
            # State file should still be rotated (fire was attempted).
            self.assertFalse(state_path.exists())

    def test_transient_failure_succeeds_on_retry(self) -> None:
        # T-0909: first resume exits non-zero (the transient auto-update
        # binary-absent window) with HO not up; the bounded retry then succeeds.
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / _DEFAULT_STATE_FILE_NAME
            _write_state(state_path, int(time.time()) - 300)

            fail = MagicMock()
            fail.returncode = 1
            fail.stderr = "Error: claude native binary not installed"
            ok = MagicMock()
            ok.returncode = 0
            ok.stderr = ""

            with patch.dict(os.environ, {
                "PANTHEON_STATE_DIR": tmp,
                "PANTHEON_CLAUDE_BIN": "/usr/local/bin/claude",
            }):
                with patch("tools.ho_resumer._ho_already_running", return_value=False):
                    with patch("tools.ho_resumer.time.sleep") as mock_sleep:
                        with patch("subprocess.run", side_effect=[fail, ok]) as mock_sub:
                            result = resumer.main()

            self.assertEqual(result, 0, "retry should succeed → exit 0")
            self.assertEqual(mock_sub.call_count, 2, "fired once, retried once")
            self.assertEqual(mock_sleep.call_count, 1)
            # State consumed exactly once (after the successful retry).
            self.assertFalse(state_path.exists())
            self.assertEqual(
                len(list(Path(tmp).glob("ho_limit_hit.json.consumed.*"))), 1
            )

    def test_non_zero_but_ho_running_skips_retry(self) -> None:
        # T-0909: if the first attempt actually brought HO up (pgrep matches on the
        # retry-guard check), do NOT retry — avoid double-starting HO.
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / _DEFAULT_STATE_FILE_NAME
            _write_state(state_path, int(time.time()) - 300)

            mock_result = MagicMock()
            mock_result.returncode = 1
            mock_result.stderr = ""

            with patch.dict(os.environ, {
                "PANTHEON_STATE_DIR": tmp,
                "PANTHEON_CLAUDE_BIN": "/usr/local/bin/claude",
            }):
                # pre-fire check False (so it fires); retry-guard check True (HO now
                # up → skip retry).
                with patch("tools.ho_resumer._ho_already_running",
                           side_effect=[False, True]):
                    with patch("tools.ho_resumer.time.sleep") as mock_sleep:
                        with patch("subprocess.run", return_value=mock_result) as mock_sub:
                            result = resumer.main()

            self.assertEqual(result, 1)
            self.assertEqual(mock_sub.call_count, 1, "must NOT retry when HO already up")
            self.assertEqual(mock_sleep.call_count, 0, "no sleep when retry skipped")
            self.assertFalse(state_path.exists(), "state still rotated")

    def test_first_attempt_filenotfound_rotates_no_retry(self) -> None:
        # T-0909: a genuinely missing binary PATH (FileNotFoundError on the first
        # exec) → rotate + return 1 with NO sleep/retry — distinct from the
        # transient runtime binary-absent case.
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / _DEFAULT_STATE_FILE_NAME
            _write_state(state_path, int(time.time()) - 300)

            with patch.dict(os.environ, {
                "PANTHEON_STATE_DIR": tmp,
                "PANTHEON_CLAUDE_BIN": "/usr/local/bin/claude",
            }):
                with patch("tools.ho_resumer._ho_already_running", return_value=False):
                    with patch("tools.ho_resumer.time.sleep") as mock_sleep:
                        with patch("subprocess.run",
                                   side_effect=FileNotFoundError()) as mock_sub:
                            result = resumer.main()

            self.assertEqual(result, 1)
            self.assertEqual(mock_sub.call_count, 1, "no retry on a missing binary path")
            self.assertEqual(mock_sleep.call_count, 0, "no sleep on a missing binary path")
            self.assertFalse(state_path.exists(), "state rotated")

    def test_retry_filenotfound_rotates_and_returns_one(self) -> None:
        # T-0909: first attempt exits non-zero (transient), but the binary path
        # vanishes by the retry (retry raises FileNotFoundError) → rotate, return 1.
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / _DEFAULT_STATE_FILE_NAME
            _write_state(state_path, int(time.time()) - 300)

            fail = MagicMock()
            fail.returncode = 1
            fail.stderr = "Error: claude native binary not installed"

            with patch.dict(os.environ, {
                "PANTHEON_STATE_DIR": tmp,
                "PANTHEON_CLAUDE_BIN": "/usr/local/bin/claude",
            }):
                with patch("tools.ho_resumer._ho_already_running", return_value=False):
                    with patch("tools.ho_resumer.time.sleep") as mock_sleep:
                        with patch("subprocess.run",
                                   side_effect=[fail, FileNotFoundError()]) as mock_sub:
                            result = resumer.main()

            self.assertEqual(result, 1)
            self.assertEqual(mock_sub.call_count, 2, "fired, then retried (which raised)")
            self.assertEqual(mock_sleep.call_count, 1)
            self.assertFalse(state_path.exists(), "state rotated")

    def test_resumer_prompt_verbatim(self) -> None:
        """Locked prompt text must match exactly."""
        expected = (
            "Continue from where you left off. "
            "The previous halt was a Max-window cap-hit; "
            "the auto-resumer fired this session. "
            "If you were mid-dispatch when halted, "
            "evaluate whether to resume the worker or re-dispatch fresh. "
            "Confirm state via the latest ledger entry before proceeding."
        )
        self.assertEqual(RESUMER_PROMPT, expected)


# ---------------------------------------------------------------------------
# Boundary: resets_at + 90s window edge
# ---------------------------------------------------------------------------


class TestFireWindowEdge(unittest.TestCase):
    """resets_at + 90s is exactly at 'now' → should fire (edge is inclusive)."""

    def test_edge_fires(self) -> None:
        now = int(time.time())
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / _DEFAULT_STATE_FILE_NAME
            # resets_at + 90 == now → window has arrived
            _write_state(state_path, now - 90)

            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""

            with patch.dict(os.environ, {
                "PANTHEON_STATE_DIR": tmp,
                "PANTHEON_CLAUDE_BIN": "/usr/local/bin/claude",
            }):
                with patch("tools.ho_resumer._ho_already_running", return_value=False):
                    with patch("subprocess.run", return_value=mock_result) as mock_sub:
                        result = resumer.main()

            self.assertEqual(result, 0)
            self.assertTrue(mock_sub.called)

    def test_one_second_before_edge_does_not_fire(self) -> None:
        now = int(time.time())
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / _DEFAULT_STATE_FILE_NAME
            # resets_at + 90 == now + 1 → one second too early
            _write_state(state_path, now - 89)

            with patch.dict(os.environ, {"PANTHEON_STATE_DIR": tmp}):
                with patch("tools.ho_resumer._ho_already_running", return_value=False):
                    with patch("subprocess.run") as mock_sub:
                        result = resumer.main()

            self.assertEqual(result, 0)
            self.assertFalse(mock_sub.called)
            self.assertTrue(state_path.exists())


# ---------------------------------------------------------------------------
# Self-audit: source files must not contain unescaped HERMES trigger
# ---------------------------------------------------------------------------


class TestHermesSourceCleanliness(unittest.TestCase):
    """Byte-sequence discipline: contiguous trigger must not appear in source."""

    def _assert_clean(self, path: Path) -> None:
        trigger = b"HERME" + b"S.md"
        content = path.read_bytes()
        self.assertNotIn(
            trigger,
            content,
            f"{path.relative_to(REPO_ROOT)} must not contain the unescaped "
            f"trigger as contiguous bytes (byte-sequence discipline).",
        )

    def test_ho_resumer_py_clean(self) -> None:
        self._assert_clean(REPO_ROOT / "tools" / "ho_resumer.py")

    def test_this_test_file_clean(self) -> None:
        self._assert_clean(Path(__file__))

    def test_service_unit_clean(self) -> None:
        path = REPO_ROOT / "tools" / "systemd" / "pantheon-ho-resumer.service"
        if path.exists():
            self._assert_clean(path)

    def test_timer_unit_clean(self) -> None:
        path = REPO_ROOT / "tools" / "systemd" / "pantheon-ho-resumer.timer"
        if path.exists():
            self._assert_clean(path)


if __name__ == "__main__":
    unittest.main()
