"""T-4184: recovery-telegram-child-watchdog must not self_restart a CODEX runtime.

After T-3026's move the runtime controller can launch codex in Recovery's session. There is
no claude plugin child then, and before T-4184 the watchdog read that as ABSENT and, after
its debounce, fired self_restart on the live codex runtime. Hermetic: an isolated tmux
server (TMUX_TMPDIR, TMUX unset) with a session named like Recovery's, a tmp HOME for the
state/log files, and --dry-run --simulate-absent so neither the live bun child nor a real
restart is ever involved.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "systemd" / "recovery-telegram-child-watchdog.sh"
SESSION = "claude-telegram-bot-fallback"


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class HarnessGateTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        (self.base / "tmux").mkdir(mode=0o700)
        self.env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        self.env.update(HOME=str(self.home), TMUX_TMPDIR=str(self.base / "tmux"))
        self.addCleanup(lambda: subprocess.run(["tmux", "kill-server"], env=self.env,
                                               capture_output=True))
        self.log = self.home / ".claude" / "logs" / "recovery-telegram-watchdog.log"

    def _session(self, *argv: str) -> None:
        subprocess.run(["tmux", "new-session", "-d", "-s", SESSION, *argv],
                       env=self.env, check=True)
        deadline = time.time() + 5
        while time.time() < deadline:
            pid = subprocess.run(["tmux", "list-panes", "-t", SESSION, "-F", "#{pane_pid}"],
                                 env=self.env, capture_output=True, text=True).stdout.strip()
            if pid and Path(f"/proc/{pid}/cmdline").read_bytes().strip(b"\0"):
                return
            time.sleep(0.05)

    def _fire(self) -> None:
        r = subprocess.run(["bash", str(SCRIPT), "--dry-run", "--simulate-absent"],
                           env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def _log(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def test_control_a_non_codex_pane_reaches_would_fire(self) -> None:
        # Proves the fixture reaches the restart decision, so the codex "no fire" means something.
        self._session("sleep", "300")
        self._fire()
        self._fire()
        self.assertIn("WOULD FIRE self_restart", self._log())

    def test_a_codex_pane_never_reaches_absent(self) -> None:
        # argv[0] keeps the codex path, as the real `node .../bin/codex ...` launch does.
        fake = self.base / "bin" / "codex"
        fake.parent.mkdir()
        fake.symlink_to(shutil.which("sleep"))
        self._session(str(fake), "300")
        for _ in range(3):
            self._fire()
        log = self._log()
        self.assertNotIn("ABSENT", log)
        self.assertNotIn("WOULD FIRE", log)


if __name__ == "__main__":
    unittest.main()
