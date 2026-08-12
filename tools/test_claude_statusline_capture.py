from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.claude_statusline_capture import StatuslineCaptureError, capture


NOW = datetime(2026, 8, 4, 0, 30, tzinfo=timezone.utc)


class ClaudeStatuslineCaptureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_capture_writes_minimal_private_atomic_quota_observation(self) -> None:
        output = self.root / "state" / "quota" / "claude-statusline.json"
        raw = {
            "session_id": "session-1",
            "transcript_path": "/tmp/session.jsonl",
            "model": {"id": "claude-opus-4-1"},
            "rate_limits": {
                "seven_day": {"used_percentage": 42.0, "resets_at": 1785800000}
            },
            "workspace": {"current_dir": "/secret/project"},
            "cost": {"total_cost_usd": 1.25},
        }
        observation = capture(
            io.BytesIO(json.dumps(raw).encode("utf-8")),
            output,
            now=lambda: NOW,
        )

        self.assertEqual(observation["session_id"], "session-1")
        self.assertEqual(observation["observed_at"], "2026-08-04T00:30:00Z")
        self.assertIn("rate_limits", observation)
        self.assertNotIn("workspace", observation)
        self.assertNotIn("cost", observation)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), observation)
        self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)

    def test_missing_rate_limits_is_recorded_but_never_invented(self) -> None:
        output = self.root / "quota.json"
        observation = capture(
            io.BytesIO(b'{"session_id":"session-2"}'),
            output,
            now=lambda: NOW,
        )
        self.assertNotIn("rate_limits", observation)

    def test_invalid_json_does_not_replace_prior_observation(self) -> None:
        output = self.root / "quota.json"
        output.write_text('{"prior":true}\n', encoding="utf-8")
        with self.assertRaisesRegex(StatuslineCaptureError, "not valid JSON"):
            capture(io.BytesIO(b"not-json"), output, now=lambda: NOW)
        self.assertEqual(output.read_text(encoding="utf-8"), '{"prior":true}\n')

    def test_symlink_output_is_refused(self) -> None:
        target = self.root / "target.json"
        target.write_text("{}", encoding="utf-8")
        output = self.root / "quota.json"
        output.symlink_to(target)
        with self.assertRaisesRegex(StatuslineCaptureError, "must not be a symlink"):
            capture(io.BytesIO(b"{}"), output, now=lambda: NOW)


if __name__ == "__main__":
    unittest.main()
