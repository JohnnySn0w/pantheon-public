"""T-6005: dark base_url backend override for the claude adapter (fail-closed guard)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.harness_adapter import claude as claude_mod
from tools.harness_adapter.claude import ClaudeBackendOverrideError, ClaudeHarnessAdapter
from tools.harness_adapter.types import (
    ExecutionRequest,
    HarnessLaunchConfig,
    SessionHandle,
    TmuxTarget,
)
from tools.runtime_controller import _runtime_command_argv

TARGET = TmuxTarget(session="s", window="w", pane="0")
KEY = "sk-or-SENTINEL-0123456789abcdef"
URL = "https://openrouter.ai/api"
FLAG = claude_mod.BACKEND_OVERRIDE_FLAG
HELPER = Path(claude_mod.__file__).resolve().parents[1] / "claude_apikey_helper.py"


def _launch(native=None, meter="api") -> HarnessLaunchConfig:
    return HarnessLaunchConfig(
        harness="claude", profile="t", executable=Path("/usr/bin/claude"), model=None,
        mode="interactive", meter=meter, quota_source="none",
        native=dict(native or {}), sources=(),
    )


def _write_key(home: Path, text: str = KEY + "\n", mode: int = 0o600) -> Path:
    path = home / ".claude" / "backend-api-key"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        env = mock.patch.dict(os.environ, {FLAG: "1"}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

    def adapter(self, native=None, meter="api") -> ClaudeHarnessAdapter:
        return ClaudeHarnessAdapter(
            "r", claude_home=self.home, launch=_launch({"base_url": URL, **(native or {})}, meter)
        )

    def launch_spec(self, adapter):
        return adapter.build_interactive_launch(TARGET, cwd=Path("/tmp/x"))

    def assertRefused(self, adapter, fragment=None):
        with self.assertRaises(ClaudeBackendOverrideError) as ctx:
            self.launch_spec(adapter)
        self.assertNotIn(KEY, str(ctx.exception))
        if fragment:
            self.assertIn(fragment, str(ctx.exception))


class GuardPassTests(_Base):
    def test_guard_passes_and_key_goes_nowhere_but_the_helper(self) -> None:
        _write_key(self.home)
        adapter = self.adapter()
        spec = self.launch_spec(adapter)
        self.assertEqual(spec.env["ANTHROPIC_BASE_URL"], URL)
        self.assertNotIn("ANTHROPIC_API_KEY", spec.env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", spec.env)
        settings = adapter.interactive_statusline_settings(output=self.home / "o.json")
        key_path = self.home / ".claude" / "backend-api-key"
        self.assertEqual(
            settings["apiKeyHelper"],
            f"/usr/bin/python3 {HELPER} --key-file {key_path}",
        )
        self.assertTrue(HELPER.is_absolute() and HELPER.is_file())
        blobs = [
            json.dumps(list(spec.argv)),
            json.dumps(dict(spec.env)),
            json.dumps(settings),
            json.dumps(list(_runtime_command_argv(spec.argv, spec.env))),
        ]
        for blob in blobs:
            self.assertNotIn(KEY, blob)
        done = subprocess.run(
            [sys.executable, str(HELPER), "--key-file", str(key_path)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual((done.returncode, done.stdout), (0, KEY + "\n"))

    def test_validate_launch_config_knows_base_url(self) -> None:
        _write_key(self.home)
        obs = {o.check_id: o for o in self.adapter().validate_launch_config()}
        self.assertEqual(obs["claude.runtime_profile"].status, "pass")
        self.assertEqual(obs["claude.backend_override"].status, "pass")

    def test_validate_reports_failed_guard(self) -> None:
        obs = {o.check_id: o for o in self.adapter().validate_launch_config()}
        self.assertEqual(obs["claude.backend_override"].status, "fail")


class FailClosedTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        _write_key(self.home)

    def test_a_subscription_login_refused(self) -> None:
        cases = {
            "credentials": lambda: (self.home / ".claude" / ".credentials.json").write_text("{}"),
            "empty-credentials": lambda: (self.home / ".claude" / ".credentials.json").write_text(""),
            "oauthAccount": lambda: (self.home / ".claude.json").write_text(
                json.dumps({"oauthAccount": {"emailAddress": "a@b"}})
            ),
        }
        for name, setup in cases.items():
            with self.subTest(name):
                for f in (".claude/.credentials.json", ".claude.json"):
                    (self.home / f).unlink(missing_ok=True)
                setup()
                self.assertRefused(self.adapter(), "subscription login")
                with self.assertRaises(ClaudeBackendOverrideError):
                    self.adapter().interactive_statusline_settings(output=self.home / "o")
        for f in (".claude/.credentials.json", ".claude.json"):
            (self.home / f).unlink(missing_ok=True)
        with self.subTest("oauth-env"), mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "x"}):
            self.assertRefused(self.adapter(), "subscription login")

    def test_b_key_file_variants_refused(self) -> None:
        path = self.home / ".claude" / "backend-api-key"
        with self.subTest("missing"):
            path.unlink()
            self.assertRefused(self.adapter(), "missing")
        for mode in (0o640, 0o644, 0o400, 0o700):
            with self.subTest(mode=oct(mode)):
                _write_key(self.home, mode=mode)
                self.assertRefused(self.adapter(), "0600")
        with self.subTest("directory"):
            path.unlink()
            path.mkdir()
            path.chmod(0o600)  # only the S_ISREG check can refuse it
            self.assertRefused(self.adapter(), "regular file")
        with self.subTest("fifo"):
            path.rmdir()
            os.mkfifo(path, 0o600)
            path.chmod(0o600)
            self.assertRefused(self.adapter(), "regular file")
        with self.subTest("foreign-owner"):
            path.unlink()
            _write_key(self.home)
            with mock.patch("os.geteuid", return_value=os.geteuid() + 1):
                self.assertRefused(self.adapter(), "owned")

    def test_c_meter_and_flag_mismatch_refused(self) -> None:
        with self.subTest("non-api meter"):
            self.assertRefused(self.adapter(meter="subscription"), "meter: api")
        with self.subTest("flag absent"), mock.patch.dict(os.environ):
            os.environ.pop(FLAG)
            self.assertRefused(self.adapter())
        with self.subTest("flag not 1"), mock.patch.dict(os.environ, {FLAG: "0"}):
            self.assertRefused(self.adapter())
        with self.subTest("flag without base_url"):
            plain = ClaudeHarnessAdapter("r", claude_home=self.home, launch=_launch())
            self.assertRefused(plain, "no base_url")

    def test_d_non_https_refused(self) -> None:
        for url in ("http://openrouter.ai/api", "openrouter.ai/api", "file:///etc/passwd",
                    "https://user:pw@openrouter.ai/", "https://tok@openrouter.ai/", "https://", "https://a b.example/", 7):
            with self.subTest(url=url):
                adapter = ClaudeHarnessAdapter(
                    "r", claude_home=self.home, launch=_launch({"base_url": url})
                )
                self.assertRefused(adapter, "https")

    def test_headless_refused_when_base_url_set(self) -> None:
        adapter = self.adapter()
        with self.assertRaises(ClaudeBackendOverrideError):
            adapter.build_exec(ExecutionRequest(prompt="p", cwd=Path("/tmp")))
        with self.assertRaises(ClaudeBackendOverrideError):
            adapter.build_resume(SessionHandle(session_id="s", name="n", resumable=True, cwd=Path("/tmp")))


class AdversarialTests(_Base):
    def test_symlinked_key_file_refused(self) -> None:
        real = self.home / "real-key"
        real.write_text(KEY)
        real.chmod(0o600)
        link = self.home / ".claude" / "backend-api-key"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(real)
        self.assertRefused(self.adapter(), "symlink")
        done = subprocess.run(
            [sys.executable, str(HELPER), "--key-file", str(link)],
            capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn(KEY, done.stdout + done.stderr)

    def test_trailing_newline_key_not_in_argv(self) -> None:
        _write_key(self.home, text=KEY + "\n\n")
        spec = self.launch_spec(self.adapter())
        wrapped = _runtime_command_argv(spec.argv, spec.env)
        self.assertFalse(any(KEY in part for part in wrapped))
        done = subprocess.run(
            [sys.executable, str(HELPER), "--key-file", str(self.home / ".claude" / "backend-api-key")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(done.stdout, KEY + "\n")


class DefaultUnchangedTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        os.environ.pop(FLAG, None)

    def test_subscription_launch_is_unchanged(self) -> None:
        adapter = ClaudeHarnessAdapter(
            "r", claude_home=self.home, launch=_launch({}, meter="subscription")
        )
        spec = self.launch_spec(adapter)
        self.assertEqual(
            spec.argv, ("/usr/bin/claude", "--permission-mode", "bypassPermissions")
        )
        self.assertEqual(
            dict(spec.env),
            {
                "PANTHEON_TMUX_SESSION": "s",
                "PANTHEON_TMUX_WINDOW": "w",
                "PANTHEON_TMUX_PANE": "0",
                "CLAUDE_CODE_DISABLE_FILE_MENTIONS": "1",
            },
        )
        settings = adapter.interactive_statusline_settings(output=self.home / "o.json")
        self.assertEqual(set(settings), {"statusLine"})
        obs = {o.check_id for o in adapter.validate_launch_config()}
        self.assertEqual(obs, {"claude.runtime_profile"})

    def test_bare_api_meter_without_flag_unchanged(self) -> None:
        adapter = ClaudeHarnessAdapter("r", claude_home=self.home, launch=_launch({}, meter="api"))
        self.assertNotIn("ANTHROPIC_BASE_URL", self.launch_spec(adapter).env)


if __name__ == "__main__":
    unittest.main()
