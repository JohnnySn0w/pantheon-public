"""T-4679: secret-safe structural MCP allowlist audit.

The fixtures in tools/fixtures/mcp_allowlist_audit are sanitized captures of the
repository-backed shapes named in test_captured_real_shape_fixtures... below.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from unittest import mock

import yaml

from tools import mcp_allowlist_audit as audit
from tools import mailbox


REPO = Path(__file__).resolve().parent.parent
AUTHORITY = REPO / "tools" / "mcp_allowlist.yaml"
FIXTURES = REPO / "tools" / "fixtures" / "mcp_allowlist_audit"


def _manifest(roles: dict[str, tuple[str, str, str]]) -> dict:
    return {
        "manifest_version": 2,
        "persona_installer": {"paths": {"repo": "pantheon-config"}},
        "roles": {
            role: {"persona": persona, "account": account, "harness": harness}
            for role, (persona, account, harness) in roles.items()
        },
    }


class AuditFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.homes = self.root / "home"
        self.homes.mkdir()
        self.manifest = self.root / "instance.yaml"
        self.managed = self.root / "managed"
        self.managed.mkdir()
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.write_manifest({"r1": ("orchestrator", "fixture", "claude-code")})
        self.repo()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_manifest(self, roles: dict[str, tuple[str, str, str]]) -> None:
        self.manifest.write_text(yaml.safe_dump(_manifest(roles)), encoding="utf-8")

    def home(self, account: str = "fixture") -> Path:
        path = self.homes / account
        path.mkdir(parents=True, exist_ok=True)
        return path

    def repo(self, account: str = "fixture") -> Path:
        path = self.home(account) / "pantheon-config"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_json(self, relative: str, value, account: str = "fixture") -> Path:
        path = self.home(account) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def scan(self, **kwargs):
        return audit.scan_fleet(
            authority_path=kwargs.pop("authority_path", AUTHORITY),
            manifest_path=kwargs.pop("manifest_path", self.manifest),
            homes_root=kwargs.pop("homes_root", self.homes),
            managed_root=kwargs.pop("managed_root", self.managed),
            proc_root=kwargs.pop("proc_root", self.proc),
            **kwargs,
        )

    def mailbox(self, account="fixture") -> dict:
        return {
            "type": "stdio",
            "command": "python3",
            "args": [str(self.home(account) / "pantheon-config/tools/mailbox_mcp.py")],
            "env": {},
        }

    def fake_sudo_topology(
        self, target: list[str], *, startup_delay: float = 0,
    ) -> tuple[list[str], tuple[Path, Path, Path]]:
        """Return an outer -> setsid monitor -> target fixture with TERM relay."""
        outer_pid = self.root / "outer.pid"
        monitor_pid = self.root / "monitor.pid"
        target_pid = self.root / "target.pid"
        target_json = json.dumps(target)
        monitor = self.root / "monitor.py"
        monitor.write_text(
            "import json, os, signal, subprocess, sys, time\n"
            f"open({str(monitor_pid)!r}, 'w').write(str(os.getpid()))\n"
            f"time.sleep({startup_delay!r})\n"
            f"child = subprocess.Popen(json.loads({target_json!r}), stdin=sys.stdin, "
            "stdout=sys.stdout, stderr=sys.stderr)\n"
            f"open({str(target_pid)!r}, 'w').write(str(child.pid))\n"
            "def relay(*_):\n"
            "    try:\n"
            "        child.send_signal(signal.SIGTERM)\n"
            "    except ProcessLookupError:\n"
            "        pass\n"
            "signal.signal(signal.SIGTERM, relay)\n"
            "raise SystemExit(child.wait())\n",
            encoding="utf-8",
        )
        outer = self.root / "outer.py"
        outer.write_text(
            "import os, signal, subprocess, sys\n"
            f"open({str(outer_pid)!r}, 'w').write(str(os.getpid()))\n"
            f"monitor = subprocess.Popen([{sys.executable!r}, {str(monitor)!r}], "
            "stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, start_new_session=True)\n"
            "def relay(*_):\n"
            "    try:\n"
            "        monitor.send_signal(signal.SIGTERM)\n"
            "    except ProcessLookupError:\n"
            "        pass\n"
            "signal.signal(signal.SIGTERM, relay)\n"
            "raise SystemExit(monitor.wait())\n",
            encoding="utf-8",
        )
        pid_paths = (outer_pid, monitor_pid, target_pid)

        def cleanup() -> None:
            for path in reversed(pid_paths):
                if not path.exists():
                    continue
                try:
                    os.kill(int(path.read_text(encoding="utf-8")), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass

        self.addCleanup(cleanup)
        return [sys.executable, str(outer)], pid_paths

    def assert_processes_gone(self, pid_paths: tuple[Path, ...], timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            live = [path for path in pid_paths if path.exists()
                    and Path(f"/proc/{path.read_text(encoding='utf-8')}").exists()]
            if not live:
                return
            time.sleep(0.01)
        self.assertFalse(
            [path.read_text(encoding="utf-8") for path in pid_paths
             if path.exists() and Path(f"/proc/{path.read_text(encoding='utf-8')}").exists()],
            "probe topology left live processes",
        )

    def sticker(self, account="fixture") -> dict:
        return {
            "type": "stdio",
            "command": "python3",
            "args": [str(self.home(account) / "pantheon-config/tools/sticker_mcp.py")],
        }

    def enable_telegram_plugin(self, account="fixture") -> Path:
        home = self.home(account)
        root = home / ".claude/plugins/cache/claude-plugins-official/telegram/0.0.7"
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin/plugin.json").write_text(
            (FIXTURES / "plugin_manifest.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        (root / ".mcp.json").write_text(
            (FIXTURES / "plugin_mcp.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.write_json(
            ".claude/settings.json",
            {"enabledPlugins": {"telegram@claude-plugins-official": True}},
            account,
        )
        self.write_json(
            ".claude/plugins/installed_plugins.json",
            {
                "plugins": {
                    "telegram@claude-plugins-official": [
                        {"installPath": str(root), "version": "0.0.7"}
                    ]
                }
            },
            account,
        )
        return root


class ScanTests(AuditFixture):
    def test_all_allowlisted_configured_surfaces_clean(self):
        self.write_json(
            ".claude.json",
            {
                "mcpServers": {
                    "mailbox": self.mailbox(),
                    "pantheon-mcp-codex": {
                        "type": "stdio",
                        "command": "python3",
                        "args": [str(self.home() / ".claude/mcp/pantheon-mcp-codex/server.py")],
                        "env": {},
                    },
                },
                "projects": {
                    str(self.repo()): {"mcpServers": {"sticker": self.sticker()}}
                },
            },
        )
        self.enable_telegram_plugin()
        codex = self.home() / ".codex/config.toml"
        codex.parent.mkdir(parents=True)
        codex.write_text(
            '[mcp_servers.telegram]\ncommand="python3"\n'
            f'args=["{self.repo()}/tools/codex_telegram_bridge.py","exec","--mode","server"]\n',
            encoding="utf-8",
        )
        self.assertEqual(self.scan().verdict, "CLEAN")

    def test_unknown_server_is_drift(self):
        self.write_json(".claude.json", {"mcpServers": {"rogue": {"command": "npx"}}})
        result = self.scan()
        self.assertEqual(result.verdict, "DRIFT")
        self.assertEqual(result.findings[0].reason, "UNALLOWLISTED_SERVER")

    def test_allowlisted_server_on_disallowed_seat_is_drift(self):
        self.write_manifest({"r": ("recovery", "fixture", "claude-code")})
        self.write_json(".claude.json", {"mcpServers": {"mailbox": self.mailbox()}})
        result = self.scan()
        self.assertEqual(result.verdict, "DRIFT")
        self.assertIn("SEAT_NOT_ADMITTED", {f.reason for f in result.findings})

    def test_malformed_json_is_unknown(self):
        path = self.home() / ".claude.json"
        path.write_text("{", encoding="utf-8")
        self.assertEqual(self.scan().verdict, "UNKNOWN")

    def test_malformed_toml_is_unknown(self):
        path = self.home() / ".codex/config.toml"
        path.parent.mkdir(parents=True)
        path.write_text("[mcp_servers", encoding="utf-8")
        self.assertEqual(self.scan().verdict, "UNKNOWN")

    def test_wrong_root_type_is_unknown(self):
        self.write_json(".claude.json", [])
        result = self.scan()
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertIn("WRONG_ROOT_TYPE", {f.reason for f in result.findings})

    def test_unreadable_or_permission_failure_is_unknown(self):
        self.write_json(".claude.json", {"mcpServers": {}})
        original = audit._read_regular_file

        def deny(path, *args, **kwargs):
            if path.name == ".claude.json":
                raise PermissionError("secret-bearing diagnostic must not surface")
            return original(path, *args, **kwargs)

        with mock.patch.object(audit, "_read_regular_file", side_effect=deny):
            result = self.scan()
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertNotIn("secret-bearing", result.to_json())

    def test_inert_plugin_marketplace_template_is_ignored(self):
        root = self.home() / ".claude/plugins/marketplaces/vendor/telegram"
        root.mkdir(parents=True)
        (root / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"rogue": {"command": "npx"}}}), encoding="utf-8"
        )
        self.assertEqual(self.scan().verdict, "CLEAN")

    def test_enabled_plugin_manifest_is_discovered(self):
        root = self.enable_telegram_plugin()
        data = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
        data["mcpServers"]["rogue"] = {"command": "npx"}
        (root / ".mcp.json").write_text(json.dumps(data), encoding="utf-8")
        result = self.scan()
        self.assertEqual(result.verdict, "DRIFT")
        self.assertIn("plugin", {f.category for f in result.findings})

    def test_project_scope_claude_mcp_is_discovered(self):
        self.write_json(
            ".claude.json",
            {"projects": {str(self.repo()): {"mcpServers": {"rogue": {"command": "npx"}}}}},
        )
        result = self.scan()
        self.assertEqual(result.verdict, "DRIFT")
        self.assertTrue(any(f.category == "claude-project" for f in result.findings))

    def test_explicit_home_and_safe_multi_project_roots_are_discovered(self):
        fixture = (FIXTURES / "claude_user_multi_project.json").read_text(encoding="utf-8")
        fixture = fixture.replace("/home/fixture", str(self.home()))
        (self.home() / ".claude.json").write_text(fixture, encoding="utf-8")
        explicit = self.home() / "work/explicit-project"
        explicit.mkdir(parents=True)
        (self.home() / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"rogue-home": {"command": "npx"}}}), encoding="utf-8"
        )
        (explicit / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"rogue-project": {"command": "npx"}}}), encoding="utf-8"
        )
        result = self.scan()
        self.assertEqual(result.verdict, "DRIFT")
        self.assertEqual({f.server for f in result.findings if f.classification == "DRIFT"},
                         {"rogue-home", "rogue-project"})

    def test_explicit_project_roots_require_absolute_nonsymlink_home_descendants(self):
        safe = self.home() / "work/safe"
        safe.mkdir(parents=True)
        outside = self.root / "outside-project"
        outside.mkdir()
        link = self.home() / "work/link"
        link.symlink_to(outside, target_is_directory=True)
        self.write_json(".claude.json", {"projects": {
            "relative/project": {"mcpServers": {}},
            str(outside): {"mcpServers": {}},
            str(link): {"mcpServers": {}},
            str(safe): {"mcpServers": {"mailbox": self.mailbox()}},
        }})
        result = self.scan()
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertEqual(result.discovered, 1)
        rendered = result.to_json()
        self.assertNotIn(str(outside), rendered)
        self.assertNotIn("relative/project", rendered)
        self.assertIn("UNSAFE_PROJECT_PATH", rendered)

    def test_configured_project_duplicate_discovery_is_deterministic(self):
        safe = self.home() / "work/safe"
        safe.mkdir(parents=True)
        config = {"mcpServers": {"mailbox": self.mailbox()}}
        self.write_json(".claude.json", {"projects": {str(safe): config}})
        (safe / ".mcp.json").write_text(json.dumps(config), encoding="utf-8")
        first = self.scan()
        second = self.scan()
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.discovered, 1)

    def test_stale_missing_safe_project_is_clean_but_embedded_servers_are_audited(self):
        fixture = (FIXTURES / "claude_user_stale_project.json").read_text(encoding="utf-8")
        fixture = fixture.replace("/home/fixture", str(self.home()))
        (self.home() / ".claude.json").write_text(fixture, encoding="utf-8")
        clean = self.scan()
        self.assertEqual(clean.verdict, "CLEAN")
        self.assertEqual(clean.discovered, 1)
        raw = json.loads(fixture)
        config = next(iter(raw["projects"].values()))
        config["mcpServers"] = {"rogue-stale": {"command": "npx"}}
        (self.home() / ".claude.json").write_text(json.dumps(raw), encoding="utf-8")
        drift = self.scan()
        self.assertEqual(drift.verdict, "DRIFT")
        self.assertIn("rogue-stale", {f.server for f in drift.findings})
        self.assertNotIn("UNSAFE_PROJECT_PATH", drift.to_json())

    def test_project_mcp_json_is_discovered(self):
        (self.repo() / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"rogue": {"command": "npx"}}}), encoding="utf-8"
        )
        self.assertEqual(self.scan().verdict, "DRIFT")

    def test_codex_server_is_discovered(self):
        self.write_json(".claude.json", {"mcpServers": {}})
        path = self.home() / ".codex/config.toml"
        path.parent.mkdir(parents=True)
        path.write_text('[mcp_servers.rogue]\ncommand="npx"\n', encoding="utf-8")
        result = self.scan()
        self.assertEqual(result.verdict, "DRIFT")
        self.assertTrue(any(f.category == "codex-user" for f in result.findings))

    def test_codex_sticker_source_grounded_fingerprint_and_arbitrary_command_rejected(self):
        path = self.home() / ".codex/config.toml"
        path.parent.mkdir(parents=True)
        path.write_text(
            '[mcp_servers.sticker]\ncommand="python3"\n'
            f'args=["{self.repo()}/tools/sticker_mcp.py"]\n', encoding="utf-8",
        )
        self.assertEqual(self.scan().verdict, "CLEAN")
        path.write_text('[mcp_servers.sticker]\ncommand="npx"\nargs=["sticker"]\n', encoding="utf-8")
        result = self.scan()
        self.assertEqual(result.verdict, "DRIFT")
        self.assertIn("FINGERPRINT_MISMATCH", {f.reason for f in result.findings})

    def test_duplicate_discovery_is_deterministically_deduped(self):
        cfg = {"mcpServers": {"mailbox": self.mailbox()}}
        self.write_json(".claude.json", cfg)
        (self.repo() / ".mcp.json").write_text(json.dumps(cfg), encoding="utf-8")
        first = self.scan()
        second = self.scan()
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.discovered, 1)

    def test_secret_args_and_env_values_absent_from_all_output(self):
        secret = "sk-super-secret-arg"
        self.write_json(
            ".claude.json",
            {"mcpServers": {"rogue": {"command": secret, "args": [secret], "env": {"TOKEN": secret}}}},
        )
        result = self.scan()
        for rendered in (result.to_json(), result.to_human()):
            self.assertNotIn(secret, rendered)
            self.assertNotIn("TOKEN", rendered)

    def test_fleet_accounts_and_homes_derive_from_manifest(self):
        self.write_manifest(
            {
                "one": ("orchestrator", "alpha", "claude-code"),
                "two": ("specialist", "beta", "claude-code"),
            }
        )
        for account in ("alpha", "beta"):
            self.repo(account)
            self.write_json(".claude.json", {"mcpServers": {}}, account)
        result = self.scan()
        self.assertEqual(result.seats, ("specialist", "orchestrator"))
        self.assertNotIn("fixture", result.to_json())

    def test_missing_optional_config_is_clean(self):
        self.assertEqual(self.scan().verdict, "CLEAN")

    def test_missing_or_malformed_authority_is_unknown_fail_closed(self):
        missing = self.root / "missing.yaml"
        self.assertEqual(self.scan(authority_path=missing).verdict, "UNKNOWN")
        bad = self.root / "bad.yaml"
        bad.write_text("servers: []\n", encoding="utf-8")
        self.assertEqual(self.scan(authority_path=bad).verdict, "UNKNOWN")

    def test_missing_or_malformed_manifest_is_unknown_fail_closed(self):
        self.manifest.unlink()
        self.assertEqual(self.scan().verdict, "UNKNOWN")
        self.manifest.write_text("roles: []\n", encoding="utf-8")
        self.assertEqual(self.scan().verdict, "UNKNOWN")

    def test_cross_account_reader_timeout_is_bounded_and_unknown(self):
        start = time.monotonic()
        result = audit.run_probe_command(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            seat="orchestrator",
            timeout_seconds=0.05,
        )
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertEqual(result.findings[0].reason, "READER_TIMEOUT")

    def test_production_shaped_sudo_monitor_escape_is_reaped(self):
        command, pid_paths = self.fake_sudo_topology(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        result = audit.run_probe_command(
            command, seat="orchestrator", timeout_seconds=0.25,
            input_text="x" * (2 * 1024 * 1024),
        )
        self.assertEqual(result.findings[0].reason, "READER_TIMEOUT")
        self.assert_processes_gone(pid_paths)

    def test_inner_deadline_reaps_target_monitor_threads_and_fds(self):
        deadlines = audit._probe_deadlines(0.8)
        command, pid_paths = self.fake_sudo_topology(
            audit._probe_python_command(deadlines.child)
        )
        before_threads = {thread.ident for thread in threading.enumerate()}
        before_fds = len(tuple(Path("/proc/self/fd").iterdir()))
        result = audit.run_probe_command(
            command, seat="orchestrator", input_text="import time\ntime.sleep(60)\n",
            deadlines=deadlines,
        )
        self.assertEqual(result.findings[0].reason, "READER_TIMEOUT")
        self.assert_processes_gone(pid_paths)
        self.assertEqual(
            {thread.ident for thread in threading.enumerate()} - before_threads, set()
        )
        self.assertLessEqual(len(tuple(Path("/proc/self/fd").iterdir())), before_fds)

    def test_inner_deadline_bounds_blocked_and_partial_stdin(self):
        deadlines = audit._probe_deadlines(0.8)
        command = audit._probe_python_command(deadlines.child)
        started = time.monotonic()
        result = audit.run_probe_command(
            command, seat="orchestrator", account="orchestrator", deadlines=deadlines,
            input_text="while True: pass\n" + ("#" * (512 * 1024)),
        )
        self.assertLess(time.monotonic() - started, 0.9)
        self.assertEqual(result.findings[0].reason, "READER_TIMEOUT")

        partial_deadlines = audit._probe_deadlines(0.8)
        partial = subprocess.Popen(
            audit._probe_python_command(partial_deadlines.child), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        self.addCleanup(lambda: partial.poll() is None and os.killpg(partial.pid, signal.SIGKILL))
        assert partial.stdin is not None
        partial.stdin.write(b"import time\ntime.sleep(60)")
        partial.stdin.flush()
        self.assertEqual(partial.wait(timeout=0.9), audit.PROBE_TIMEOUT_EXIT)
        self.assertEqual(partial.stdout.read(), b"")
        self.assertEqual(partial.stderr.read(), b"")
        partial.stdin.close()
        partial.stdout.close()
        partial.stderr.close()

    def test_parent_term_relay_reaps_target_startup_stall(self):
        command, pid_paths = self.fake_sudo_topology(
            [sys.executable, "-c", "import time; time.sleep(60)"], startup_delay=60,
        )
        deadlines = audit._probe_deadlines(0.8)
        result = audit.run_probe_command(
            command, seat="orchestrator", deadlines=deadlines,
            input_text="x" * (2 * 1024 * 1024),
        )
        self.assertEqual(result.findings[0].reason, "READER_TIMEOUT")
        self.assert_processes_gone(pid_paths)

    def test_inner_timeout_is_reported_as_timeout_unknown(self):
        deadlines = audit._probe_deadlines(0.8)
        result = audit.run_probe_command(
            audit._probe_python_command(deadlines.child), seat="orchestrator",
            account="orchestrator", deadlines=deadlines,
            input_text="import time\ntime.sleep(60)\n",
        )
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertEqual(result.findings[0].reason, "READER_TIMEOUT")

    def test_probe_bootstrap_normal_clean_and_stdin_globals(self):
        deadlines = audit._probe_deadlines(1.5)
        envelope = {
            "schema": audit.PROBE_SCHEMA,
            "account": "orchestrator",
            "audit": audit.ScanResult("CLEAN", (), ("orchestrator",), 0).safe_dict(),
        }
        source = (
            "assert __name__ == '__main__'\n"
            "assert __file__ == '<stdin>'\n"
            "assert __builtins__\n"
            f"print({json.dumps(json.dumps(envelope))})\n"
        )
        result = audit.run_probe_command(
            audit._probe_python_command(deadlines.child), seat="orchestrator",
            account="orchestrator", deadlines=deadlines, input_text=source,
        )
        self.assertEqual(result.verdict, "CLEAN")

    def test_probe_command_deadlines_and_numeric_args_are_strict(self):
        deadlines = audit._probe_deadlines(2.0)
        command = audit._probe_python_command(deadlines.child)
        self.assertEqual(command[:4], ["/usr/bin/python3", "-I", "-c", audit.PROBE_BOOTSTRAP])
        self.assertEqual(len(command), 6)
        self.assertRegex(command[4], r"^[0-9]+(?:\.[0-9]+)?$")
        self.assertEqual(command[5], str(audit.DEFAULT_MAX_BYTES))
        with self.assertRaises(audit.AuditError):
            audit._probe_python_command(float("nan"))
        with self.assertRaises(audit.AuditError):
            audit._probe_python_command(time.monotonic() + audit.PROBE_MAX_HORIZON_SECONDS + 1)
        with self.assertRaises(audit.AuditError):
            audit._probe_deadlines(0.01)

    def test_probe_output_oversize_reaps_complete_topology(self):
        command, pid_paths = self.fake_sudo_topology(
            [sys.executable, "-c", "print('x' * 5000)"]
        )
        result = audit.run_probe_command(
            command, seat="orchestrator", timeout_seconds=0.8, max_output_bytes=256,
        )
        self.assertEqual(result.findings[0].reason, "READER_OUTPUT_OVERSIZE")
        self.assert_processes_gone(pid_paths)

    def test_probe_stdin_transfer_is_inside_deadline_and_kills_process_group(self):
        pid_file = self.root / "probe-pids"
        child = (
            "import os,subprocess,sys,time;"
            "grand=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
            f"open({str(pid_file)!r},'w').write(f'{{os.getpid()}} {{grand.pid}}');"
            "time.sleep(0.5)"
        )

        def cleanup() -> None:
            if not pid_file.exists():
                return
            for raw in pid_file.read_text(encoding="utf-8").split():
                try:
                    os.kill(int(raw), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass

        self.addCleanup(cleanup)
        started = time.monotonic()
        result = audit.run_probe_command(
            [sys.executable, "-c", child], seat="orchestrator", timeout_seconds=0.3,
            input_text="not-a-secret:" + ("x" * (2 * 1024 * 1024)),
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.55)
        self.assertEqual(result.findings[0].reason, "READER_TIMEOUT")
        self.assertNotIn("not-a-secret", result.to_json())
        pids = [int(raw) for raw in pid_file.read_text(encoding="utf-8").split()]
        deadline = time.monotonic() + 1
        while any(Path(f"/proc/{pid}").exists() for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse([pid for pid in pids if Path(f"/proc/{pid}").exists()])

    def test_cross_account_probe_uses_reviewed_stdin_and_exact_target_principal(self):
        authority = audit.load_authority(AUTHORITY)
        deadlines = audit._probe_deadlines(3)
        seat = audit.Seat("specialist", "claude-research", "claude-code",
                          Path("/home/claude-research"), Path("/home/claude-research/pantheon-config"))
        with mock.patch("tools.seat_exec.is_own_account", return_value=False):
            command, source = audit.build_probe_invocation(
                seat, authority=authority, max_bytes=4096,
                managed_root=Path("/etc/claude-code"), proc_root=Path("/proc"),
                child_deadline=deadlines.child,
            )
        self.assertEqual(command[:5], ["sudo", "-n", "-u", "claude-research", "/usr/bin/python3"])
        self.assertEqual(command[5:8], ["-I", "-c", audit.PROBE_BOOTSTRAP])
        self.assertEqual(len(command), 10)
        self.assertRegex(command[8], r"^[0-9]+(?:\.[0-9]+)?$")
        self.assertEqual(command[9], str(audit.DEFAULT_MAX_BYTES))
        self.assertNotIn("/usr/bin/timeout", command)
        self.assertNotIn(str(REPO), " ".join(command))
        self.assertNotIn(str(REPO), source)
        self.assertIn("pantheon-mcp-allowlist-audit-probe/v1", source)

    def test_embedded_stdin_probe_executes_against_tmp_roots(self):
        account = __import__("pwd").getpwuid(os.getuid()).pw_name
        seat = audit.Seat("orchestrator", account, "claude-code", self.home(), self.repo())
        authority = audit.load_authority(AUTHORITY)
        deadlines = audit._probe_deadlines(3)
        command, source = audit.build_probe_invocation(
            seat, authority=authority, max_bytes=4096,
            managed_root=self.managed, proc_root=self.proc,
            child_deadline=deadlines.child,
        )
        self.assertLessEqual(len(source.encode()), audit.DEFAULT_MAX_BYTES)
        self.assertEqual(command[:4], ["/usr/bin/python3", "-I", "-c", audit.PROBE_BOOTSTRAP])
        result = audit.run_probe_command(
            command, seat="orchestrator", account=account, deadlines=deadlines,
            input_text=source, cwd=Path("/"),
        )
        self.assertEqual(result.verdict, "CLEAN")
        self.assertEqual(result.seats, ("orchestrator",))

    def test_probe_invocation_uses_isolated_python_and_ignores_target_sitecustomize(self):
        account = __import__("pwd").getpwuid(os.getuid()).pw_name
        seat = audit.Seat("orchestrator", account, "claude-code", self.home(), self.repo())
        authority = audit.load_authority(AUTHORITY)
        deadlines = audit._probe_deadlines(3)
        command, source = audit.build_probe_invocation(
            seat, authority=authority, max_bytes=4096,
            managed_root=self.managed, proc_root=self.proc,
            child_deadline=deadlines.child,
        )
        target_python = self.root / "target-python"
        target_python.mkdir()
        marker = self.root / "sitecustomize-loaded"
        (target_python / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('loaded')\n"
            "print('target sitecustomize loaded', file=__import__('sys').stderr)\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(target_python)}):
            result = audit.run_probe_command(
                command, seat="orchestrator", account=account, deadlines=deadlines,
                input_text=source, cwd=Path("/"),
            )
        self.assertEqual(command[:4], ["/usr/bin/python3", "-I", "-c", audit.PROBE_BOOTSTRAP])
        self.assertFalse(marker.exists())
        self.assertEqual(result.verdict, "CLEAN")

    def test_embedded_stdin_probe_principal_mismatch_is_rejected(self):
        actual = __import__("pwd").getpwuid(os.getuid()).pw_name
        seat = audit.Seat("orchestrator", f"{actual}-wrong", "claude-code", self.home(), self.repo())
        authority = audit.load_authority(AUTHORITY)
        deadlines = audit._probe_deadlines(3)
        with mock.patch("tools.seat_exec.is_own_account", return_value=False):
            _, source = audit.build_probe_invocation(
                seat, authority=authority, max_bytes=4096,
                managed_root=self.managed, proc_root=self.proc,
                child_deadline=deadlines.child,
            )
        result = audit.run_probe_command(
            ["/usr/bin/python3", "-"], seat="orchestrator", account=seat.account,
            timeout_seconds=3, input_text=source,
        )
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertEqual(result.findings[0].reason, "READER_PRINCIPAL_MISMATCH")

    def test_target_repo_tampering_and_inaccessible_controller_paths_are_not_probe_inputs(self):
        authority = audit.load_authority(AUTHORITY)
        deadlines = audit._probe_deadlines(3)
        seat = audit.Seat("specialist", "victim", "claude-code", self.home("victim"), self.repo("victim"))
        marker = "MALICIOUS_TARGET_REPO_MARKER"
        (seat.repo / "tools").mkdir()
        (seat.repo / "tools/mcp_allowlist_audit.py").write_text(marker, encoding="utf-8")
        (seat.repo / "tools/mcp_allowlist.yaml").write_text(marker, encoding="utf-8")
        with mock.patch("tools.seat_exec.is_own_account", return_value=False):
            command, source = audit.build_probe_invocation(
                seat, authority=authority, max_bytes=4096,
                managed_root=self.managed, proc_root=self.proc,
                child_deadline=deadlines.child,
            )
        self.assertNotIn(marker, source)
        self.assertNotIn(str(seat.repo / "tools/mcp_allowlist_audit.py"), command)
        self.assertNotIn(str(REPO), command)

    def test_invalid_oversized_or_stderr_probe_output_is_unknown(self):
        invalid = audit.run_probe_command(
            [sys.executable, "-c", "print('not-json')"], seat="orchestrator", timeout_seconds=1,
        )
        self.assertEqual(invalid.findings[0].reason, "READER_OUTPUT_INVALID")
        oversized = audit.run_probe_command(
            [sys.executable, "-c", "print('x' * 5000)"], seat="orchestrator", timeout_seconds=1,
            max_output_bytes=256,
        )
        self.assertEqual(oversized.findings[0].reason, "READER_OUTPUT_OVERSIZE")
        stderr = audit.run_probe_command(
            [sys.executable, "-c", "import sys; print('{}'); print('raw secret', file=sys.stderr)"],
            seat="orchestrator", timeout_seconds=1,
        )
        self.assertEqual(stderr.findings[0].reason, "READER_STDERR_NONEMPTY")
        self.assertNotIn("raw secret", stderr.to_json())
        wrong_principal = {
            "schema": audit.PROBE_SCHEMA,
            "account": "other",
            "audit": audit.ScanResult("CLEAN", (), ("orchestrator",), 0).safe_dict(),
        }
        mismatch = audit.run_probe_command(
            [sys.executable, "-c", f"print({json.dumps(json.dumps(wrong_principal))})"],
            seat="orchestrator", timeout_seconds=1,
        )
        self.assertEqual(mismatch.findings[0].reason, "READER_PRINCIPAL_MISMATCH")

    def test_probe_protocol_rejects_invalid_classification_verdict_and_bool_count(self):
        def probe(raw):
            envelope = {"schema": audit.PROBE_SCHEMA, "account": "orchestrator", "audit": raw}
            return audit.run_probe_command(
                [sys.executable, "-c", f"print({json.dumps(json.dumps(envelope))})"],
                seat="orchestrator", timeout_seconds=1,
            )

        base = audit.ScanResult("CLEAN", (), ("orchestrator",), 0).safe_dict()
        bad_class = dict(base)
        bad_class["verdict"] = "DRIFT"
        bad_class["findings"] = [{
            "classification": "SURPRISE", "seat": "orchestrator", "source_id": "x",
            "category": "global", "server": "-", "reason": "X",
        }]
        self.assertEqual(probe(bad_class).findings[0].reason, "READER_OUTPUT_INVALID")
        bad_verdict = dict(base, verdict="SURPRISE")
        self.assertEqual(probe(bad_verdict).findings[0].reason, "READER_OUTPUT_INVALID")
        bad_count = dict(base, discovered=True)
        self.assertEqual(probe(bad_count).findings[0].reason, "READER_OUTPUT_INVALID")

    def test_unsafe_project_paths_are_unknown_and_opaque(self):
        outside = self.root / "attacker-controlled"
        outside.mkdir()
        self.write_json(
            ".claude.json",
            {"projects": {str(outside): {"mcpServers": {"mailbox": self.mailbox()}}}},
        )
        result = self.scan()
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertNotIn(str(outside), result.to_json())
        self.assertIn("UNSAFE_PROJECT_PATH", result.to_json())

    def test_symlink_and_oversize_known_paths_are_unknown(self):
        target = self.root / "target.json"
        target.write_text("{}", encoding="utf-8")
        (self.repo() / ".mcp.json").symlink_to(target)
        self.assertEqual(self.scan().verdict, "UNKNOWN")
        (self.repo() / ".mcp.json").unlink()
        (self.repo() / ".mcp.json").write_bytes(b" " * 65)
        self.assertEqual(self.scan(max_bytes=64).verdict, "UNKNOWN")
        (self.repo() / ".mcp.json").unlink()
        outside = self.root / "outside-claude"
        outside.mkdir()
        (outside / "settings.json").write_text("{}", encoding="utf-8")
        (self.home() / ".claude").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.scan().verdict, "UNKNOWN")

    def test_managed_and_runtime_settings_discovery_and_precedence(self):
        root = self.enable_telegram_plugin()
        data = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
        data["mcpServers"]["rogue"] = {"command": "npx"}
        (root / ".mcp.json").write_text(json.dumps(data), encoding="utf-8")
        self.write_json(".claude/settings.json", {"enabledPlugins": {"telegram@claude-plugins-official": False}})
        (self.managed / "managed-settings.json").write_text(
            (FIXTURES / "managed_settings.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.assertEqual(self.scan().verdict, "DRIFT")

        (self.managed / "managed-settings.json").write_text(
            json.dumps({"enabledPlugins": {"telegram@claude-plugins-official": False}}), encoding="utf-8"
        )
        runtime = json.loads((FIXTURES / "runtime_settings.json").read_text(encoding="utf-8"))
        pid = self.proc / "123"
        pid.mkdir()
        argv = [a.replace("/home/fixture", str(self.home())) for a in runtime["argv"]]
        (pid / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (pid / "cwd").symlink_to(self.repo(), target_is_directory=True)
        self.assertEqual(self.scan().verdict, "CLEAN", "managed false overrides runtime true")
        (self.managed / "managed-settings.json").unlink()
        self.assertEqual(self.scan().verdict, "DRIFT", "runtime --settings enables plugin")

    def test_runtime_settings_path_and_disable_precedence(self):
        root = self.enable_telegram_plugin()
        data = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
        data["mcpServers"]["rogue"] = {"command": "npx"}
        (root / ".mcp.json").write_text(json.dumps(data), encoding="utf-8")
        flag = self.repo() / "runtime-settings.json"
        flag.write_text(json.dumps({"enabledPlugins": {"telegram@claude-plugins-official": True}}), encoding="utf-8")
        pid = self.proc / "456"
        pid.mkdir()
        (pid / "cmdline").write_bytes(
            b"claude\0--settings\0runtime-settings.json\0"
        )
        (pid / "cwd").symlink_to(self.repo(), target_is_directory=True)
        self.assertEqual(self.scan().verdict, "DRIFT")
        local = self.repo() / ".claude/settings.local.json"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(json.dumps({"enabledPlugins": {"telegram@claude-plugins-official": False}}), encoding="utf-8")
        self.assertEqual(self.scan().verdict, "DRIFT", "runtime scope overrides local disable")

    def test_managed_dropins_are_bounded_sorted_and_override_lower_scopes(self):
        root = self.enable_telegram_plugin()
        data = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
        data["mcpServers"]["rogue"] = {"command": "npx"}
        (root / ".mcp.json").write_text(json.dumps(data), encoding="utf-8")
        dropins = self.managed / "managed-settings.d"
        dropins.mkdir()
        (dropins / "10-enable.json").write_text(
            json.dumps({"enabledPlugins": {"telegram@claude-plugins-official": True}}), encoding="utf-8"
        )
        (dropins / "20-disable.json").write_text(
            json.dumps({"enabledPlugins": {"telegram@claude-plugins-official": False}}), encoding="utf-8"
        )
        self.assertEqual(self.scan().verdict, "CLEAN")
        (dropins / "20-disable.json").write_text(
            json.dumps({"enabledPlugins": {"telegram@claude-plugins-official": True}}), encoding="utf-8"
        )
        self.assertEqual(self.scan().verdict, "DRIFT")

    def test_malformed_unreadable_managed_or_runtime_settings_are_unknown(self):
        (self.managed / "managed-settings.json").write_text("{", encoding="utf-8")
        self.assertEqual(self.scan().verdict, "UNKNOWN")
        (self.managed / "managed-settings.json").unlink()
        pid = self.proc / "789"
        pid.mkdir()
        (pid / "cmdline").write_bytes(b"claude\0--settings\0{\0")
        (pid / "cwd").symlink_to(self.repo(), target_is_directory=True)
        result = self.scan()
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertNotIn("--settings", result.to_json())
        original = audit._read_regular_file
        with mock.patch.object(
            audit, "_read_regular_file",
            side_effect=lambda path, *a, **kw: (_ for _ in ()).throw(PermissionError("secret"))
            if path == self.managed / "managed-settings.json" else original(path, *a, **kw),
        ):
            (self.managed / "managed-settings.json").write_text("{}", encoding="utf-8")
            denied = self.scan()
        self.assertEqual(denied.verdict, "UNKNOWN")
        self.assertNotIn("secret", denied.to_json())

    def test_unreadable_same_principal_runtime_source_is_unknown(self):
        pid = self.proc / "790"
        pid.mkdir()
        cmdline = pid / "cmdline"
        cmdline.write_bytes(b"claude\0--settings\0{}\0")
        original = audit._read_regular_file
        with mock.patch.object(
            audit, "_read_regular_file",
            side_effect=lambda path, *a, **kw: (_ for _ in ()).throw(PermissionError("secret"))
            if path == cmdline else original(path, *a, **kw),
        ):
            result = self.scan()
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertNotIn("secret", result.to_json())


class MonitorTests(AuditFixture):
    def setUp(self):
        super().setUp()
        self.state = self.root / "state/monitor.json"
        self.mail = self.root / "mailbox"
        mailbox.provision(self.mail / "orchestrator")

    def drift(self, name="rogue"):
        self.write_json(".claude.json", {"mcpServers": {name: {"command": "npx"}}})
        return self.scan()

    def messages(self):
        path = self.mail / "orchestrator/new"
        return sorted(path.iterdir()) if path.exists() else []

    def test_unchanged_findings_are_deduped(self):
        result = self.drift()
        self.assertTrue(audit.monitor_result(result, self.state, self.mail).alerted)
        self.assertFalse(audit.monitor_result(result, self.state, self.mail).alerted)
        self.assertEqual(len(self.messages()), 1)

    def test_finding_set_or_class_change_realerts(self):
        first = self.drift()
        audit.monitor_result(first, self.state, self.mail)
        second = self.drift("other")
        self.assertTrue(audit.monitor_result(second, self.state, self.mail).alerted)
        unknown = audit.ScanResult.unknown_global("AUTHORITY_INVALID")
        self.assertTrue(audit.monitor_result(unknown, self.state, self.mail).alerted)

    def test_recovery_clears_alert_state(self):
        audit.monitor_result(self.drift(), self.state, self.mail)
        (self.home() / ".claude.json").unlink()
        clean = self.scan()
        outcome = audit.monitor_result(clean, self.state, self.mail)
        self.assertEqual(outcome.reason, "RECOVERED")
        self.assertFalse(self.state.exists())
        self.assertTrue(audit.monitor_result(self.drift(), self.state, self.mail).alerted)

    def test_mailbox_failure_is_visible_and_not_acknowledged(self):
        result = self.drift()
        with mock.patch.object(audit, "_deliver_mailbox", side_effect=OSError("no mailbox")):
            outcome = audit.monitor_result(result, self.state, self.mail)
        self.assertFalse(outcome.delivered)
        self.assertFalse(self.state.exists())
        self.assertIn("MAILBOX_DELIVERY_FAILED", outcome.reason)

    def test_mailbox_delivery_uses_consumable_envelope(self):
        secret = "sk-envelope-secret"
        self.write_json(
            ".claude.json",
            {"mcpServers": {"rogue": {"command": "npx", "args": [secret], "env": {"TOKEN": secret}}}},
        )
        result = self.scan()
        outcome = audit.monitor_result(result, self.state, self.mail)
        self.assertTrue(outcome.delivered)
        got = []
        mailbox.consume(self.mail / "orchestrator", lambda message: got.append(message))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["sender"], "recovery")
        self.assertEqual(got[0]["subject"], "MCP allowlist audit DRIFT")
        self.assertEqual(got[0]["payload"]["audit"]["verdict"], "DRIFT")
        self.assertNotIn(secret, json.dumps(got[0]))


class CliAndInstallTests(AuditFixture):
    def run_cli(self, *extra):
        return subprocess.run(
            [
                sys.executable,
                str(REPO / "tools/mcp_allowlist_audit.py"),
                "scan",
                "--authority", str(AUTHORITY),
                "--manifest", str(self.manifest),
                "--homes-root", str(self.homes),
                "--managed-root", str(self.managed),
                "--proc-root", str(self.proc),
                *extra,
            ],
            cwd=REPO,
            text=True,
            capture_output=True,
            timeout=5,
        )

    def test_cli_clean_drift_unknown_exit_and_structured_verdict_contract(self):
        clean = self.run_cli("--format", "json")
        self.assertEqual(clean.returncode, 0)
        self.assertEqual(json.loads(clean.stdout)["verdict"], "CLEAN")
        self.write_json(".claude.json", {"mcpServers": {"rogue": {"command": "npx"}}})
        drift = self.run_cli("--format", "human")
        self.assertEqual(drift.returncode, 2)
        self.assertIn("DRIFT", drift.stdout)
        (self.home() / ".claude.json").write_text("{", encoding="utf-8")
        unknown = self.run_cli("--format", "json")
        self.assertEqual(unknown.returncode, 3)
        self.assertEqual(json.loads(unknown.stdout)["verdict"], "UNKNOWN")

    def test_systemd_units_are_default_dark_and_syntax_valid(self):
        service = (REPO / "tools/systemd/pantheon-mcp-allowlist-audit.service").read_text()
        timer = (REPO / "tools/systemd/pantheon-mcp-allowlist-audit.timer").read_text()
        self.assertIn("Type=oneshot", service)
        self.assertIn('ExecStart=/usr/bin/sg pantheon-mailbox -c "/usr/bin/python3 %h/pantheon-config/tools/mcp_allowlist_audit.py monitor --format json"', service)
        self.assertIn("TimeoutStartSec=90s", service)
        self.assertIn("OnFailure=pantheon-unit-failure-alert@%n.service", service)
        self.assertIn("Persistent=true", timer)
        self.assertIn("[Install]", timer)
        self.assertNotIn("WantedBy=default.target", service)
        for text in (service, timer):
            self.assertNotIn("enable --now", text)
            self.assertNotIn("systemctl start", text)
        verifier = shutil.which("systemd-analyze")
        if verifier:
            proc = subprocess.run(
                [verifier, "verify", str(REPO / "tools/systemd/pantheon-mcp-allowlist-audit.service"),
                 str(REPO / "tools/systemd/pantheon-mcp-allowlist-audit.timer")],
                text=True, capture_output=True,
            )
            errors = "\n".join(line for line in proc.stderr.splitlines() if "not found" not in line)
            self.assertEqual(errors, "", errors)

    def test_installer_convergence_installs_but_does_not_enable_or_fire(self):
        home = self.root / "recovery-home"
        calls = []
        changed = audit.install_units(REPO, home, runner=lambda argv: calls.append(tuple(argv)) or 0)
        self.assertEqual(set(changed), {
            "pantheon-mcp-allowlist-audit.service", "pantheon-mcp-allowlist-audit.timer"
        })
        self.assertEqual(calls, [("systemctl", "--user", "daemon-reload")])
        state_dir = home / ".local/state/pantheon"
        self.assertTrue(state_dir.is_dir())
        self.assertEqual(stat.S_IMODE(state_dir.stat().st_mode), 0o700)
        calls.clear()
        self.assertEqual(audit.install_units(REPO, home, runner=lambda argv: calls.append(tuple(argv)) or 0), ())
        self.assertEqual(calls, [])
        wants = home / ".config/systemd/user/timers.target.wants/pantheon-mcp-allowlist-audit.timer"
        wants.parent.mkdir(parents=True)
        wants.symlink_to(home / ".config/systemd/user/pantheon-mcp-allowlist-audit.timer")
        calls.clear()
        self.assertEqual(audit.install_units(REPO, home, runner=lambda argv: calls.append(tuple(argv)) or 0), ())
        self.assertTrue(wants.is_symlink())

    def test_standalone_installer_refuses_symlink_state_dir_without_touching_target(self):
        home = self.root / "recovery-home-symlink"
        target = self.root / "outside-state"
        target.mkdir(mode=0o755)
        marker = target / "marker"
        marker.write_text("unchanged", encoding="utf-8")
        state_dir = home / ".local/state/pantheon"
        state_dir.parent.mkdir(parents=True)
        state_dir.symlink_to(target, target_is_directory=True)

        with self.assertRaises(audit.AuditError):
            audit.install_units(REPO, home, runner=lambda argv: 0)
        self.assertTrue(state_dir.is_symlink())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")

    def test_canonical_persona_installer_wires_recovery_units_default_dark_and_transition_ready(self):
        from tools import persona_installer as installer
        manifest = installer.Manifest.load(REPO / "instance.yaml")
        recovery = installer.PersonaInstaller(REPO).plan(manifest, "recovery", root=self.root / "projection")
        ops = {op.operation_id: op for op in recovery.operations}
        for name in ("mcp-allowlist-audit.service-unit", "mcp-allowlist-audit.timer-unit"):
            self.assertIn(name, ops)
            self.assertEqual(ops[name].kind, "user-unit-install")
        self.assertEqual(ops["connective.pantheon-state-dir"].desired["mode"], "0700")
        self.assertEqual(ops["connective.pantheon-state-dir"].owner, "claude-fallback")
        audit_timer = "pantheon-mcp-allowlist-audit.timer"
        self.assertFalse(any(
            audit_timer in str(op.target) and op.kind in {
                "timer-enable", "seat-periodic-timer-enable", "direct-fire", "unit-fire",
                "seat-service-decommission",
            }
            for op in recovery.operations
        ))
        self.assertLess(
            [op.operation_id for op in recovery.operations].index("mcp-allowlist-audit.timer-unit"),
            [op.operation_id for op in recovery.operations].index("connective.daemon-reload"),
        )
        orchestrator = installer.PersonaInstaller(REPO).plan(manifest, "orchestrator", root=self.root / "projection-orchestrator")
        self.assertFalse(any(op.operation_id.startswith("mcp-allowlist-audit.") for op in orchestrator.operations))

    def test_real_script_tmp_roots_leave_real_repo_and_homes_unchanged(self):
        before = subprocess.run(["git", "status", "--porcelain"], cwd=REPO, text=True, capture_output=True, check=True).stdout
        real_home_stats = {p: p.stat().st_mtime_ns for p in (Path.home(), REPO)}
        proc = self.run_cli("--format", "json")
        self.assertEqual(proc.returncode, 0)
        after = subprocess.run(["git", "status", "--porcelain"], cwd=REPO, text=True, capture_output=True, check=True).stdout
        self.assertEqual(after, before)
        self.assertEqual(real_home_stats, {p: p.stat().st_mtime_ns for p in real_home_stats})


class ContractTests(unittest.TestCase):
    def test_machine_registry_matches_claude_provisioner_and_plugin_codex_expectations(self):
        authority = audit.load_authority(AUTHORITY)
        text = (REPO / "rulebook/policies/mcp-adoption.md").read_text(encoding="utf-8")
        policy = {}
        for line in text.splitlines():
            if not line.startswith("| `"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            name_match = re.fullmatch(r"`([^`]+)`", cells[0])
            if not name_match:
                continue
            name = name_match.group(1)
            self.assertNotIn(name, policy, f"duplicate policy row for {name}")
            seats = set(re.findall(r"\b(orchestrator|specialist|recovery|archivist|mason|artisan|codex|watchman)\b", cells[1]))
            if "listed, not admitted" in cells[1]:
                seats = set()
            policy[name] = frozenset(seats)
        machine = {name: frozenset(entry.seats) for name, entry in authority.servers.items()}
        self.assertEqual(policy, machine)

        from tools import persona_installer as installer
        self.assertEqual(installer._CLAUDE_MCP_ALLOWLIST, machine)
        instance = yaml.safe_load((REPO / "instance.yaml").read_text(encoding="utf-8"))
        provisioned = instance["persona_installer"]["claude_mcp_servers"]["personas"]
        provisioned_by_server = {}
        for seat, config in provisioned.items():
            self.assertLessEqual(set(config["servers"]), {n for n, seats in machine.items() if seat in seats})
            fixture_seat = audit.Seat(
                seat, config["account"], "claude-code", Path("/home") / config["account"],
                Path("/home") / config["account"] / "pantheon-config",
            )
            for name, entry in config["servers"].items():
                provisioned_by_server.setdefault(name, set()).add(seat)
                allowed = authority.servers[name]
                self.assertTrue(any(
                    audit._fingerprint_matches(
                        authority.fingerprints[profile], entry, category="claude",
                        seat=fixture_seat, plugin_key=None,
                    )
                    for profile in allowed.profiles
                ), f"{seat}/{name} provisioner fingerprint drift")
        self.assertEqual(provisioned_by_server["mailbox"], set(authority.servers["mailbox"].seats))
        self.assertEqual(provisioned_by_server["pantheon-mcp-codex"], {"orchestrator"})
        bridge_seats = set(instance["persona_installer"]["codex_telegram_bridge"]["personas"])
        self.assertEqual(bridge_seats, set(authority.servers["telegram"].seats))
        self.assertEqual(
            set(authority.servers["sticker"].seats),
            {"orchestrator", "specialist", "recovery", "archivist"},
        )
        self.assertEqual(set(authority.servers["sticker"].profiles), {"claude-sticker", "codex-sticker"})
        from tools.context_fill_monitor import PLUGIN_CONTENT_PINS
        self.assertIn("telegram@claude-plugins-official", PLUGIN_CONTENT_PINS)
        from tools.harness_adapter import host_mcp_registration
        self.assertEqual(
            set(host_mcp_registration.REGISTRATIONS_BY_NAME),
            {"pantheon-host", "pantheon-approval"},
        )
        for name in host_mcp_registration.REGISTRATIONS_BY_NAME:
            self.assertEqual(authority.servers[name].seats, frozenset())

    def test_captured_real_shape_fixtures_match_sanitized_repo_sources(self):
        user = json.loads((FIXTURES / "claude_user.json").read_text(encoding="utf-8"))
        self.assertEqual(set(user), {"mcpServers", "projects"})
        self.assertEqual(user["mcpServers"]["mailbox"]["args"][0].split("/pantheon-config/")[1], "tools/mailbox_mcp.py")
        self.assertEqual(next(iter(user["projects"].values()))["mcpServers"]["sticker"]["args"][0].split("/pantheon-config/")[1], "tools/sticker_mcp.py")

        from tools.test_context_fill_monitor import TestPluginContentPinMcpJson
        self.assertEqual(
            json.loads((FIXTURES / "plugin_mcp.json").read_text(encoding="utf-8")),
            TestPluginContentPinMcpJson.UPSTREAM,
        )
        installed = json.loads((FIXTURES / "installed_plugins.json").read_text(encoding="utf-8"))
        self.assertEqual(set(installed), {"plugins"})
        codex = tomllib.loads((FIXTURES / "codex_config.toml").read_text(encoding="utf-8"))
        self.assertEqual(codex["mcp_servers"]["telegram"]["args"][-3:], ["exec", "--mode", "server"])
        managed = json.loads((FIXTURES / "managed_settings.json").read_text(encoding="utf-8"))
        self.assertEqual(managed, {"enabledPlugins": {"telegram@claude-plugins-official": True}})
        runtime = json.loads((FIXTURES / "runtime_settings.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(runtime["argv"][0]).name, "claude")
        self.assertEqual(runtime["argv"][1], "--settings")
        self.assertIn("enabledPlugins", json.loads(runtime["argv"][2]))
        multi = json.loads((FIXTURES / "claude_user_multi_project.json").read_text(encoding="utf-8"))
        self.assertEqual(set(multi["projects"]), {
            "/home/fixture", "/home/fixture/work/explicit-project",
        })
        stale = json.loads((FIXTURES / "claude_user_stale_project.json").read_text(encoding="utf-8"))
        self.assertIn("/.claude/plugins/cache/", next(iter(stale["projects"])))

    def test_strict_registry_parser_parity_rejects_broadened_shapes(self):
        from tools import persona_installer as installer
        from tools import host_broker_install as broker
        base = yaml.safe_load(AUTHORITY.read_text(encoding="utf-8"))
        mutations = []
        extra_root = json.loads(json.dumps(base))
        extra_root["unexpected"] = True
        mutations.append(extra_root)
        extra_server = json.loads(json.dumps(base))
        extra_server["servers"]["sticker"]["unexpected"] = True
        mutations.append(extra_server)
        duplicate_profile = json.loads(json.dumps(base))
        duplicate_profile["servers"]["sticker"]["profiles"].append("claude-sticker")
        mutations.append(duplicate_profile)
        invalid_fingerprint = json.loads(json.dumps(base))
        invalid_fingerprint["fingerprints"]["codex-telegram-bridge"]["plugin"] = "unexpected"
        mutations.append(invalid_fingerprint)
        invalid_plugin_key = json.loads(json.dumps(base))
        invalid_plugin_key["fingerprints"]["claude-telegram-plugin"]["plugin"] = "../bad@marketplace"
        mutations.append(invalid_plugin_key)
        for index, raw in enumerate(mutations):
            with self.subTest(index=index):
                path = Path(self._testMethodName + f"-{index}.yaml")
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / path
                    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
                    with self.assertRaises(audit.AuditError):
                        audit.load_authority(path)
                    with self.assertRaises(installer.ManifestError):
                        installer._load_mcp_allowlist(path)
                    repo = Path(tmp) / "repo"
                    (repo / "tools").mkdir(parents=True)
                    (repo / "tools/mcp_allowlist.yaml").write_text(
                        path.read_text(encoding="utf-8"), encoding="utf-8"
                    )
                    with self.assertRaises(broker.InstallPlanError):
                        broker._mcp_allowlist(repo)

    def test_shared_registry_loader_rejects_symlink_directory_and_oversize(self):
        from tools import host_broker_install as broker
        from tools import mcp_allowlist_registry as registry
        from tools import persona_installer as installer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = root / "valid.yaml"
            valid.write_bytes(AUTHORITY.read_bytes())
            bad_paths = []
            symlink = root / "symlink.yaml"
            symlink.symlink_to(valid)
            bad_paths.append(symlink)
            directory = root / "directory.yaml"
            directory.mkdir()
            bad_paths.append(directory)
            oversize = root / "oversize.yaml"
            oversize.write_bytes(b"x" * (registry.MAX_REGISTRY_BYTES + 1))
            bad_paths.append(oversize)

            for bad in bad_paths:
                with self.subTest(kind=bad.stem):
                    with self.assertRaises(registry.RegistryError):
                        registry.load_registry(bad)
                    with self.assertRaises(audit.AuditError):
                        audit.load_authority(bad)
                    with self.assertRaises(installer.ManifestError):
                        installer._load_mcp_allowlist(bad)
                    repo = root / f"repo-{bad.stem}"
                    (repo / "tools").mkdir(parents=True)
                    (repo / "tools/mcp_allowlist.yaml").symlink_to(bad)
                    with self.assertRaises(broker.InstallPlanError):
                        broker._mcp_allowlist(repo)


if __name__ == "__main__":
    unittest.main()
