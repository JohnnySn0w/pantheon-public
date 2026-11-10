"""Regression coverage for the Codex tombstone restart PID witness (T-6149)."""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "harness/codex/codex_self_restart.sh"


def _inner_pattern() -> str:
    source = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"^INNER_PGREP='([^']+)'$", source, re.MULTILINE)
    if match is None:
        raise AssertionError("codex_self_restart.sh must declare a literal INNER_PGREP")
    return match.group(1)


def _ere_matches(pattern: str, argv: str) -> bool:
    result = subprocess.run(
        ["grep", "-E", "-q", "--", pattern],
        input=argv,
        text=True,
        check=False,
    )
    return result.returncode == 0


class CodexSelfRestartPredicateTests(unittest.TestCase):
    """Fixtures captured from ``pgrep -a -u codex`` on 2026-11-05."""

    APP_SERVER = (
        "/usr/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/"
        "vendor/x86_64-unknown-linux-musl/bin/codex app-server --listen "
        "unix:///run/user/1003/codex-appserver/.server.sock"
    )
    REMOTE_TUI = (
        "/usr/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/"
        "vendor/x86_64-unknown-linux-musl/bin/codex --remote "
        "unix:///run/user/1003/codex-appserver/as.sock"
    )

    def test_native_app_server_and_controller_remote_processes_match(self) -> None:
        pattern = _inner_pattern()
        self.assertTrue(_ere_matches(pattern, self.APP_SERVER))
        self.assertTrue(_ere_matches(pattern, self.REMOTE_TUI))

    def test_wrappers_and_code_mode_host_do_not_match(self) -> None:
        pattern = _inner_pattern()
        for argv in (
            "node /usr/bin/codex app-server --listen unix:///run/user/1003/codex-appserver/.server.sock",
            "tmux new-session -d -s codex-session /usr/bin/codex --remote unix:///run/user/1003/codex-appserver/as.sock",
            self.APP_SERVER.replace("bin/codex app-server", "bin/codex-code-mode-host"),
            "/tmp/codex-linux-x64/fake/bin/codex unrelated-command",
        ):
            with self.subTest(argv=argv):
                self.assertFalse(_ere_matches(pattern, argv))

    def test_pgrep_is_scoped_to_the_running_account_uid(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('pgrep -u "$(id -u)" -f "$INNER_PGREP"', source)


if __name__ == "__main__":
    unittest.main()
