from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.claude_quota_probe import (
    ClaudeQuotaProbeError,
    _cap_sentinel,
    _fresh_statusline,
    _ready_for_submission,
    probe_session_id,
    run_probe,
)
from tools.claude_statusline_capture import SCHEMA as STATUSLINE_SCHEMA


NOW = datetime(2026, 8, 4, 1, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self) -> None:
        self.seconds = 0.0

    def monotonic(self) -> float:
        return self.seconds

    def sleep(self, seconds: float) -> None:
        self.seconds += seconds

    def now(self) -> datetime:
        return NOW + timedelta(seconds=self.seconds)


class ClaudeQuotaProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        (self.repo / "tools").mkdir(parents=True)
        for name in ("claude_statusline_capture.py", "send_keys_submit.sh"):
            path = self.repo / "tools" / name
            path.write_text("#!/bin/sh\n", encoding="utf-8")
            path.chmod(0o755)
        self.executable = self.root / "claude"
        self.executable.write_text("#!/bin/sh\n", encoding="utf-8")
        self.executable.chmod(0o755)
        self.state_root = self.root / "state"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exact_usage_cap_text_is_negative_authority(self) -> None:
        self.assertEqual(
            _cap_sentinel("You've hit your session limit · resets 5:10pm (UTC)"),
            "You've hit your session limit · resets 5:10pm (UTC)",
        )
        self.assertIsNone(_cap_sentinel("PONG — quota appears fine"))
        self.assertIsNone(
            _cap_sentinel("Documentation: you've hit your weekly limit means retry later.")
        )
        self.assertEqual(
            _cap_sentinel("You've hit your Opus limit · resets 5:10pm"),
            "You've hit your Opus limit · resets 5:10pm",
        )

    def test_fresh_statusline_requires_post_submit_rate_limits(self) -> None:
        path = self.root / "quota.json"
        path.write_text(
            json.dumps(
                {
                    "schema": STATUSLINE_SCHEMA,
                    "observed_at": "2026-08-04T00:59:00Z",
                    "rate_limits": {"seven_day": {"used_percentage": 20}},
                }
            ),
            encoding="utf-8",
        )
        self.assertIsNone(_fresh_statusline(path, submitted_at=NOW))
        path.write_text(
            json.dumps(
                {
                    "schema": STATUSLINE_SCHEMA,
                    "observed_at": "2026-08-04T01:00:01Z",
                }
            ),
            encoding="utf-8",
        )
        self.assertIsNone(_fresh_statusline(path, submitted_at=NOW))

    def test_fresh_statusline_rejects_unchanged_future_dated_baseline(self) -> None:
        path = self.root / "quota.json"
        path.write_text(
            json.dumps(
                {
                    "schema": STATUSLINE_SCHEMA,
                    "observed_at": "2026-08-04T01:05:00Z",
                    "rate_limits": {"seven_day": {"used_percentage": 20}},
                }
            ),
            encoding="utf-8",
        )
        import hashlib

        baseline = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertIsNone(
            _fresh_statusline(
                path,
                submitted_at=NOW,
                baseline_sha256=baseline,
            )
        )

    def test_ready_for_submission_requires_claude_input_frame(self) -> None:
        self.assertFalse(_ready_for_submission("Claude starting\n"))
        self.assertFalse(_ready_for_submission("Documentation mentions ❯ in prose\n"))
        self.assertTrue(_ready_for_submission("Claude ready\n  ❯ \n  manual mode on\n"))
        self.assertTrue(
            _ready_for_submission('Claude ready\n❯ Try "fix a bug"\n')
        )
        self.assertTrue(_ready_for_submission("Claude ready\n❯\u00a0\n"))
        self.assertTrue(
            _ready_for_submission('Claude ready\n❯\u00a0Try "fix a bug"\n')
        )

    def test_probe_accepts_only_fresh_structured_positive_and_kills_scratch_tmux(self) -> None:
        clock = _Clock()
        calls: list[tuple[str, ...]] = []
        submitted_at: list[float] = []

        def runner(argv, **kwargs):
            command = tuple(argv)
            calls.append(command)
            if command[:2] == ("tmux", "capture-pane"):
                if clock.seconds < 1.0:
                    text = "Claude starting\n"
                elif clock.seconds < 2.0:
                    text = "Claude ready\n❯ \n"
                else:
                    text = "Claude ready\nPONG\n"
                return subprocess.CompletedProcess(command, 0, text, "")
            if command[0].endswith("send_keys_submit.sh"):
                submitted_at.append(clock.seconds)
                quota = self.state_root / "recovery" / "quota" / "claude-statusline.json"
                quota.parent.mkdir(parents=True, exist_ok=True)
                quota.write_text(
                    json.dumps(
                        {
                            "schema": STATUSLINE_SCHEMA,
                            "observed_at": "2026-08-04T01:00:01Z",
                            "session_id": probe_session_id(
                                "fixture", "recovery", "20260804T010000Z"
                            ),
                            "rate_limits": {
                                "seven_day": {"used_percentage": 20.0}
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                clock.seconds = 2.0
            return subprocess.CompletedProcess(command, 0, "", "")

        result = run_probe(
            account="fixture",
            persona="recovery",
            executable=self.executable,
            repo_root=self.repo,
            state_root=self.state_root,
            run_id="20260804T010000Z",
            timeout_seconds=10.0,
            runner=runner,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            now=clock.now,
            enforce_identity=False,
        )
        self.assertEqual(result.status, "observed")
        self.assertTrue(any(call[:2] == ("tmux", "kill-session") for call in calls))
        self.assertIsNotNone(result.statusline_sha256)
        self.assertTrue(result.pong_observed)
        self.assertEqual(submitted_at, [1.0])

    def test_probe_never_submits_on_persistent_nonempty_startup_banner(self) -> None:
        clock = _Clock()
        calls: list[tuple[str, ...]] = []

        def runner(argv, **kwargs):
            command = tuple(argv)
            calls.append(command)
            if command[:2] == ("tmux", "capture-pane"):
                return subprocess.CompletedProcess(command, 0, "Claude starting\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        with self.assertRaisesRegex(ClaudeQuotaProbeError, "never accepted"):
            run_probe(
                account="fixture",
                persona="recovery",
                executable=self.executable,
                repo_root=self.repo,
                state_root=self.state_root,
                run_id="20260804T010006Z",
                timeout_seconds=10.0,
                runner=runner,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                now=clock.now,
                enforce_identity=False,
            )
        self.assertFalse(
            any(call[0].endswith("send_keys_submit.sh") for call in calls)
        )
        self.assertTrue(any(call[:2] == ("tmux", "kill-session") for call in calls))

    def test_probe_cap_sentinel_fails_fast_without_positive_quota(self) -> None:
        clock = _Clock()
        calls: list[tuple[str, ...]] = []

        def runner(argv, **kwargs):
            command = tuple(argv)
            calls.append(command)
            if command[:2] == ("tmux", "capture-pane"):
                return subprocess.CompletedProcess(
                    command,
                    0,
                    "You've hit your weekly limit · resets Sunday\n",
                    "",
                )
            return subprocess.CompletedProcess(command, 0, "", "")

        result = run_probe(
            account="fixture",
            persona="recovery",
            executable=self.executable,
            repo_root=self.repo,
            state_root=self.state_root,
            run_id="20260804T010001Z",
            timeout_seconds=10.0,
            runner=runner,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            now=clock.now,
            enforce_identity=False,
        )
        self.assertEqual(result.status, "capped")
        self.assertIsNone(result.statusline_sha256)
        self.assertFalse(result.pong_observed)
        self.assertFalse(any(call[0].endswith("send_keys_submit.sh") for call in calls))

    def test_probe_refuses_pre_submit_pong_scrollback(self) -> None:
        clock = _Clock()

        def runner(argv, **kwargs):
            command = tuple(argv)
            if command[:2] == ("tmux", "capture-pane"):
                return subprocess.CompletedProcess(command, 0, "Claude ready\nPONG\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        with self.assertRaisesRegex(ClaudeQuotaProbeError, "pre-submit PONG"):
            run_probe(
                account="fixture",
                persona="recovery",
                executable=self.executable,
                repo_root=self.repo,
                state_root=self.state_root,
                run_id="20260804T010003Z",
                timeout_seconds=10.0,
                runner=runner,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                now=clock.now,
                enforce_identity=False,
            )

    def test_probe_rejects_statusline_from_another_session(self) -> None:
        clock = _Clock()

        def runner(argv, **kwargs):
            command = tuple(argv)
            if command[:2] == ("tmux", "capture-pane"):
                text = (
                    "Claude ready\nPONG\n"
                    if clock.seconds >= 1.0
                    else "Claude ready\n❯ \n"
                )
                return subprocess.CompletedProcess(command, 0, text, "")
            if command[0].endswith("send_keys_submit.sh"):
                quota = self.state_root / "recovery" / "quota" / "claude-statusline.json"
                quota.parent.mkdir(parents=True, exist_ok=True)
                quota.write_text(
                    json.dumps(
                        {
                            "schema": STATUSLINE_SCHEMA,
                            "observed_at": "2026-08-04T01:00:01Z",
                            "session_id": "00000000-0000-0000-0000-000000000000",
                            "rate_limits": {"seven_day": {"used_percentage": 20.0}},
                        }
                    ),
                    encoding="utf-8",
                )
                clock.seconds = 1.0
            return subprocess.CompletedProcess(command, 0, "", "")

        with self.assertRaisesRegex(ClaudeQuotaProbeError, "timed out"):
            run_probe(
                account="fixture",
                persona="recovery",
                executable=self.executable,
                repo_root=self.repo,
                state_root=self.state_root,
                run_id="20260804T010004Z",
                timeout_seconds=10.0,
                runner=runner,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                now=clock.now,
                enforce_identity=False,
            )

    def test_probe_timeout_is_fail_closed(self) -> None:
        clock = _Clock()

        def runner(argv, **kwargs):
            command = tuple(argv)
            if command[:2] == ("tmux", "capture-pane"):
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        with self.assertRaisesRegex(ClaudeQuotaProbeError, "never accepted"):
            run_probe(
                account="fixture",
                persona="recovery",
                executable=self.executable,
                repo_root=self.repo,
                state_root=self.state_root,
                run_id="20260804T010002Z",
                timeout_seconds=10.0,
                runner=runner,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                now=clock.now,
                enforce_identity=False,
            )

    def test_runner_timeout_is_typed_and_cleanup_cannot_mask_it(self) -> None:
        clock = _Clock()

        def runner(argv, **kwargs):
            command = tuple(argv)
            if command[:2] == ("tmux", "capture-pane"):
                return subprocess.CompletedProcess(command, 0, "Claude ready\n❯ \n", "")
            if command[0].endswith("send_keys_submit.sh"):
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            if command[:2] == ("tmux", "kill-session"):
                raise FileNotFoundError("tmux vanished during cleanup")
            return subprocess.CompletedProcess(command, 0, "", "")

        with self.assertRaisesRegex(ClaudeQuotaProbeError, "command timed out"):
            run_probe(
                account="fixture",
                persona="recovery",
                executable=self.executable,
                repo_root=self.repo,
                state_root=self.state_root,
                run_id="20260804T010005Z",
                timeout_seconds=10.0,
                runner=runner,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                now=clock.now,
                enforce_identity=False,
            )


if __name__ == "__main__":
    unittest.main()
