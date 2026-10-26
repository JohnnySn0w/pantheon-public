"""T-4935: bypass-permissions launch + dialog pre-consent contract."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools import bypass_consent

ROOT = Path(__file__).resolve().parent
SUPERVISE_SCRIPTS = [
    ROOT / "systemd" / "claude-bot-supervise.sh",
    ROOT / "systemd" / "recovery-bot-supervise.sh",
    ROOT / "systemd" / "specialist-bot-supervise.sh",
    ROOT / "systemd" / "exo-bot-supervise.sh",
]


class EnsureBypassConsentTest(unittest.TestCase):
    def test_creates_and_merges_preserving_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / ".claude"
            self.assertTrue(bypass_consent.ensure_bypass_consent(d))
            self.assertEqual(json.loads((d / "settings.json").read_text()),
                             {bypass_consent.KEY: True})
            (d / "settings.json").write_text(json.dumps({"model": "x"}))
            self.assertTrue(bypass_consent.ensure_bypass_consent(d))
            self.assertEqual(json.loads((d / "settings.json").read_text()),
                             {"model": "x", bypass_consent.KEY: True})

    def test_idempotent_no_rewrite_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "settings.json"
            p.write_text('{"skipDangerousModePermissionPrompt":true}')
            before = p.stat().st_mtime_ns
            self.assertTrue(bypass_consent.ensure_bypass_consent(Path(tmp)))
            self.assertEqual(p.stat().st_mtime_ns, before)

    def test_corrupt_file_untouched_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "settings.json"
            p.write_text("{not json")
            self.assertFalse(bypass_consent.ensure_bypass_consent(Path(tmp)))
            self.assertEqual(p.read_text(), "{not json")


class ShellSnippetTest(unittest.TestCase):
    def _run(self, config_dir: Path) -> None:
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)}
        subprocess.run(["bash", "-c", bypass_consent.SHELL_SNIPPET], check=True, env=env)

    def test_snippet_merges_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "cfg"
            d.mkdir()
            (d / "settings.json").write_text(json.dumps({"model": "x"}))
            self._run(d)
            self._run(d)
            self.assertEqual(json.loads((d / "settings.json").read_text()),
                             {"model": "x", bypass_consent.KEY: True})

    def test_snippet_corrupt_file_exits_zero_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "settings.json").write_text("{bad")
            self._run(d)
            self.assertEqual((d / "settings.json").read_text(), "{bad")


class SuperviseScriptsTest(unittest.TestCase):
    def test_every_script_launches_bypass_and_embeds_consent(self) -> None:
        for script in SUPERVISE_SCRIPTS:
            text = script.read_text()
            with self.subTest(script=script.name):
                args = [l for l in text.splitlines() if l.startswith("CLAUDE_ARGS=")]
                self.assertEqual(len(args), 1)
                self.assertIn("--permission-mode bypassPermissions", args[0])
                self.assertNotIn("permission-mode default", args[0])
                self.assertIn(bypass_consent.SHELL_SNIPPET, text)
                # consent must run before the session is created
                self.assertLess(text.index(bypass_consent.SHELL_SNIPPET),
                                re.search(r"^\s+.*tmux new-session -d", text, re.M).start())

    def test_scripts_pass_bash_syntax_check(self) -> None:
        for script in SUPERVISE_SCRIPTS:
            with self.subTest(script=script.name):
                subprocess.run(["bash", "-n", str(script)], check=True)


if __name__ == "__main__":
    unittest.main()
