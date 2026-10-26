"""T-4792: live-harness resolution from the controller's runtime.json."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import live_harness  # noqa: E402


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(self, payload, agent="specialist"):
        rc = self.root / agent / "runtime-controller"
        rc.mkdir(parents=True, exist_ok=True)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (rc / "runtime.json").write_text(text, encoding="utf-8")

    def resolve(self, default="claude-code", agent="specialist"):
        return live_harness.resolve(agent, default, state_root=self.root)

    def test_codex_runtime_maps_to_codex_profile(self):
        self._write({"schema": "pantheon-runtime-state/v1", "harness": "codex"})
        self.assertEqual(self.resolve(), "codex")

    def test_claude_runtime_maps_to_claude_code_profile(self):
        self._write({"schema": "pantheon-runtime-state/v1", "harness": "claude"})
        self.assertEqual(self.resolve(default="codex"), "claude-code")

    def test_missing_state_falls_back(self):
        self.assertEqual(self.resolve(default="codex"), "codex")

    def test_malformed_wrong_schema_unknown_or_nonstring_fall_back(self):
        for payload in ("{not json", [], {"harness": "codex"},
                        {"schema": "pantheon-runtime-state/v1", "harness": "gemini"},
                        {"schema": "pantheon-runtime-state/v1", "harness": ["codex"]},
                        {"schema": "pantheon-runtime-state/v1"}):
            with self.subTest(payload=payload):
                self._write(payload)
                self.assertEqual(self.resolve(), "claude-code")

    def test_default_state_root_layout(self):
        self.assertEqual(live_harness.default_state_root("/home/x"),
                         Path("/home/x/.local/state/pantheon/personas"))


if __name__ == "__main__":
    unittest.main()
