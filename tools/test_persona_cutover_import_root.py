"""Import-root regression tests for persona_cutover.sh's Codex verifier."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tools" / "persona_cutover.sh"


class PersonaCutoverImportRootCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.outside_cwd = self.root / "outside cwd"
        self.outside_cwd.mkdir()
        self.fixture_repo = self.root / "repo fixture with space"
        self.fixture_tools = self.fixture_repo / "tools"
        self.fixture_tools.mkdir(parents=True)
        shutil.copy2(SCRIPT, self.fixture_tools / "persona_cutover.sh")
        (self.fixture_tools / "t2590_cutover_verify_lib.sh").write_text(
            textwrap.dedent(
                """\
                select_cutover_claude_pid() { awk '$2 == "claude" { print $1; exit 0 }'; }
                cutover_companion_rejoined() { return 0; }
                cutover_urgent_env_confirmed() { return 0; }
                cutover_telegram_poller_present() { return 0; }
                companion_snapshot_path() { printf '%s\\n' /dev/null; }
                companion_snapshot_write() { return 0; }
                companion_should_restore() { return 0; }
                """
            ),
            encoding="utf-8",
        )
        self.repo_root = self.root / "target repo with space"
        self.repo_tools = self.repo_root / "tools"
        self.repo_tools.mkdir(parents=True)
        (self.repo_tools / "__init__.py").write_text("", encoding="utf-8")
        (self.repo_tools / "sibling.py").write_text("VALUE = 'import-ok'\n", encoding="utf-8")
        (self.repo_tools / "runtime_controller.py").write_text(
            textwrap.dedent(
                """\
                import json
                import os
                import sys
                from pathlib import Path
                from tools import sibling

                if sibling.VALUE != 'import-ok':
                    raise SystemExit(12)
                log = Path(os.environ['CUTOVER_RUNTIME_LOG'])
                record = {
                    'argv': sys.argv[1:],
                    'cwd': os.getcwd(),
                    'path': os.environ.get('PATH', ''),
                }
                with log.open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(record, sort_keys=True) + '\\n')
                raise SystemExit(int(os.environ.get('CUTOVER_RUNTIME_RC', '0')))
                """
            ),
            encoding="utf-8",
        )
        self.home = self.root / "home target"
        self.home.mkdir()
        for relative in (
            ".config/systemd/user/legacy.service",
            ".local/bin/supervise",
            ".claude/settings.json",
        ):
            path = self.home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n", encoding="utf-8")
        self.state_dir = self.home / ".local/state/pantheon/personas/orchestrator/runtime-controller"
        self.state_dir.mkdir(parents=True)
        (self.state_dir / "runtime.json").write_text(
            json.dumps(
                {
                    "harness": "codex",
                    "profile": "interactive-chatgpt",
                    "runtime_scope_unit": "pantheon-runtime-tmux.scope",
                }
            ),
            encoding="utf-8",
        )
        self.xdg = self.root / "run-user"
        self.xdg.mkdir()
        self.fakebin = self.root / "fake bin"
        self.fakebin.mkdir()
        self.runtime_log = self.root / "runtime-log.jsonl"
        self.python_log = self.root / "python-log.jsonl"
        self._write_fake_commands()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_executable(self, name: str, content: str) -> None:
        path = self.fakebin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _write_fake_commands(self) -> None:
        real_python = sys.executable
        uid = os.geteuid()
        account = pwd.getpwuid(uid).pw_name
        apply_path = f"{self.fakebin}:/usr/bin:/bin"
        env_payload = {
            "PCX_ROLE": "orchestrator",
            "PCX_ACCOUNT": account,
            "PCX_UID": str(uid),
            "PCX_HOME": str(self.home),
            "PCX_REPO_ROOT": str(self.repo_root),
            "PCX_MANIFEST_PATH": str(self.repo_root / "instance.yaml"),
            "PCX_APPLY_PATH": apply_path,
            "PCX_XDG_RUNTIME_DIR": str(self.xdg),
            "PCX_DBUS_SESSION_BUS_ADDRESS": f"unix:path={self.xdg}/bus",
            "PCX_SERVICE": "legacy.service",
            "PCX_SERVICE_UNIT_PATH": str(self.home / ".config/systemd/user/legacy.service"),
            "PCX_SUPERVISE_WRAPPER_PATH": str(self.home / ".local/bin/supervise"),
            "PCX_SETTINGS_JSON_PATH": str(self.home / ".claude/settings.json"),
            "PCX_CONSUME_SERVICE": "pantheon-persona-consume@orchestrator.service",
            "PCX_CONSUME_TIMER": "pantheon-persona-consume@orchestrator.timer",
            "PCX_TMUX_SESSION": "orchestrator-session:0",
            "PCX_COMPANION_UNITS": "",
            "PCX_CHANNELS": "telegram",
            "PCX_MODEL": "gpt-5",
        }
        resolver_output = "\n".join(f"{key}={value!r}" for key, value in env_payload.items())
        self._write_executable(
            "python3",
            textwrap.dedent(
                f"""\
                #!{real_python}
                import json
                import os
                import subprocess
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                with Path({str(self.python_log)!r}).open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps({{'argv': args, 'cwd': os.getcwd(), 'path': os.environ.get('PATH', '')}}, sort_keys=True) + '\\n')
                if args and args[0].endswith('persona_cutover_resolve.py'):
                    print({resolver_output!r})
                    raise SystemExit(0)
                if args and args[0].endswith('t2611_recovery_persona_apply.py'):
                    if 'activate-staged' in args:
                        Path({str(self.root / "session-active")!r}).write_text('1', encoding='utf-8')
                    print('APPLY_OK')
                    raise SystemExit(0)
                os.execv({real_python!r}, [{real_python!r}] + args)
                """
            ),
        )
        self._write_executable(
            "id",
            textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                if [[ "${{1:-}}" == "-u" ]]; then echo {uid}; exit 0; fi
                exec /usr/bin/id "$@"
                """
            ),
        )
        self._write_executable(
            "systemctl",
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                if [[ "${1:-}" == "--user" && "${2:-}" == "show" ]]; then
                  if [[ "${*: -1}" == "--value" ]]; then echo ""; exit 0; fi
                  echo "OK"; exit 0
                fi
                exit 0
                """
            ),
        )
        active = self.root / "session-active"
        self._write_executable(
            "tmux",
            textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                case "${{1:-}}" in
                  kill-session) rm -f {str(active)!r}; exit 0 ;;
                  has-session) [[ -f {str(active)!r} ]] && exit 0 || exit 1 ;;
                  -N) echo 4242; exit 0 ;;
                  capture-pane) printf '------------------------------\\n'; exit 0 ;;
                  display-message) echo 100; exit 0 ;;
                esac
                exit 0
                """
            ),
        )
        self._write_executable(
            "grep",
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                for arg in "$@"; do
                  if [[ "$arg" == "/proc/4242/cgroup" ]]; then exit 0; fi
                done
                exec /bin/grep "$@"
                """
            ),
        )
        self._write_executable(
            "pgrep",
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                echo 777
                exit 0
                """
            ),
        )
        self._write_executable(
            "ps",
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                if [[ "$*" == *"comm="* ]]; then echo claude; exit 0; fi
                if [[ "$*" == *"args="* ]]; then echo '/home/x/.npm-global/bin/claude --channels telegram --model gpt-5 --permission-mode bypassPermissions'; exit 0; fi
                echo 1
                exit 0
                """
            ),
        )
        self._write_executable("sleep", "#!/usr/bin/env bash\nexit 0\n")

    def _run_cutover(self, *, runtime_rc: int = 0) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.update(
            {
                "PATH": f"{self.fakebin}:/usr/bin:/bin",
                "CUTOVER_RUNTIME_LOG": str(self.runtime_log),
                "CUTOVER_RUNTIME_RC": str(runtime_rc),
            }
        )
        return subprocess.run(
            (
                "bash",
                str(self.fixture_tools / "persona_cutover.sh"),
                "--persona",
                "orchestrator",
                "--stamp",
                "stamp-r23",
                "--execute",
            ),
            cwd=self.outside_cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )

    def _runtime_records(self) -> list[dict[str, object]]:
        if not self.runtime_log.exists():
            return []
        return [json.loads(line) for line in self.runtime_log.read_text(encoding="utf-8").splitlines()]

    def test_persona_cutover_codex_initial_viability_uses_repo_module_from_outside_cwd(self) -> None:
        result = self._run_cutover()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        records = self._runtime_records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["cwd"], str(self.repo_root))
        self.assertEqual(record["path"], f"{self.fakebin}:/usr/bin:/bin")
        self.assertEqual(
            record["argv"],
            [
                "initial-viability",
                "--persona",
                "orchestrator",
                "--state-root",
                str(self.home / ".local/state/pantheon/personas"),
                "--expect-selected",
                "codex/interactive-chatgpt",
                "--expect-failed",
                "claude/interactive-subscription",
                "--expect-failure-class",
                "subscription-cap",
                "--expect-failure-class",
                "operational-launch",
                "--expect-failure-class",
                "operational-readiness",
                "--require-source-provenance",
                "--require-receiver-receipt",
            ],
        )
        failed = self._run_cutover(runtime_rc=7)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("verify: initial viability selected Codex evidence MISSING/MISMATCH FAIL", failed.stdout)
        self.assertIn("CUTOVER-ABORT: cutover ran but verification FAILED", failed.stderr)

    def test_persona_cutover_codex_initial_viability_direct_script_shape_is_red(self) -> None:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        direct = subprocess.run(
            (sys.executable, str(self.repo_tools / "runtime_controller.py"), "initial-viability"),
            cwd=self.outside_cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(direct.returncode, 0)
        self.assertIn("ModuleNotFoundError", direct.stderr)
        module = subprocess.run(
            (sys.executable, "-m", "tools.runtime_controller", "initial-viability"),
            cwd=self.repo_root,
            env={**env, "CUTOVER_RUNTIME_LOG": str(self.runtime_log), "CUTOVER_RUNTIME_RC": "0"},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(module.returncode, 0, module.stderr)

    def test_persona_cutover_python_entrypoints_are_import_root_safe(self) -> None:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        for script in (
            REPO_ROOT / "tools" / "persona_cutover_resolve.py",
            REPO_ROOT / "tools" / "t2611_recovery_persona_apply.py",
        ):
            with self.subTest(script=script.name):
                result = subprocess.run(
                    (sys.executable, str(script), "--help"),
                    cwd=self.outside_cwd,
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("ModuleNotFoundError", result.stderr)
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('tools/runtime_controller.py" initial-viability', source)
        self.assertIn("python3 -m tools.runtime_controller initial-viability", source)
        result = self._run_cutover()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        python_records = [
            json.loads(line)
            for line in self.python_log.read_text(encoding="utf-8").splitlines()
        ]
        runtime_invocations = [
            item for item in python_records if item["argv"][:2] == ["-m", "tools.runtime_controller"]
        ]
        self.assertEqual(len(runtime_invocations), 1)
        direct_runtime = [
            item for item in python_records if item["argv"] and str(item["argv"][0]).endswith("tools/runtime_controller.py")
        ]
        self.assertEqual(direct_runtime, [])
        stdlib_snippets = [
            item
            for item in python_records
            if item["argv"][:1] == ["-c"] and "import json,sys" in str(item["argv"][1])
        ]
        self.assertGreaterEqual(len(stdlib_snippets), 3)


if __name__ == "__main__":
    unittest.main()
