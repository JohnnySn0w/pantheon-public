"""T-2950 Codex Telegram bridge safety tests."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import importlib.util
import json
import os
import pwd
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping
from unittest import mock

import yaml

from tools import codex_telegram_bridge as bridge
from tools import persona_bringup
from tools import runtime_controller as rc
from tools import t2611_recovery_persona_apply as persona_apply
from tools.harness_adapter import CodexHarnessAdapter
from tools.persona_apply_path import controlled_apply_environment
from tools.persona_installer import (
    ActorContext,
    LocalHostBackend,
    Manifest,
    ManifestError,
    PersonaInstaller,
    PreflightResult,
    SimulatedHostBackend,
    _all_plan_capabilities,
    codex_standing_instructions_reference,
    _read_verified_standing_instructions,
)


SECRET = "123456:SECRET_token-value"
REPO_ROOT = Path(__file__).resolve().parent.parent
R10_ORCHESTRATOR_CODEX_SHA256 = "36f0d5eb9ff0bcd6396bd82e658fa68fd49d46b1d7415186943a357a865cc688"


def _is_seat_core_periodic_timer(unit: str) -> bool:
    """T-5447: the allowlisted seat-core periodic timers (everything but consume)."""
    from tools import persona_installer as _pim

    try:
        _pim._seat_timer_template_unit(unit)
    except _pim.OperationFailed:
        return False
    return True


class LocalHostCommandSheetStageBackend(LocalHostBackend):
    def __init__(self, expected_env: dict[str, str]) -> None:
        super().__init__(command_timeout=1, timed_fire_timeout=1)
        self.commands: list[tuple[str, ...]] = []
        self.expected_env = expected_env

    def _run(self, argv, *, timeout=None):  # type: ignore[override]
        del timeout
        command = tuple(str(item) for item in argv)
        self.commands.append(command)
        if command[:4] == ("systemctl", "--user", "show", "codex-telegram-poller.service"):
            return subprocess.CompletedProcess(command, 0, "ActiveState=inactive\nMainPID=0\n", "")
        if command[:3] == ("systemctl", "--user", "show") and (command[3].startswith("pantheon-persona-consume@") or _is_seat_core_periodic_timer(command[3])) and command[3].endswith(".timer"):
            return subprocess.CompletedProcess(command, 0, "LoadState=loaded\nUnitFileState=disabled\nActiveState=inactive\nSubState=dead\n", "")
        if command[:3] == ("systemctl", "--user", "show") and command[3].startswith("pantheon-persona-consume@") and command[3].endswith(".service"):
            # T-5471: the stale-reset preflight now reads the consume unit. A clean fresh
            # seat reports not-found / inactive / dead (STALE_RESET); SERVICE_OBSERVE
            # (read only for a loaded service) answers the same shape.
            wanted = [command[i + 1] for i, arg in enumerate(command) if arg == "-p"]
            clean = {
                "LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead",
                "Result": "success", "ExecMainStatus": "0", "InvocationID": "", "Job": "",
                "SuccessExitStatus": "",
            }
            return subprocess.CompletedProcess(command, 0, "".join(f"{k}={clean[k]}\n" for k in wanted), "")
        if command[:3] == ("systemctl", "--user", "show-environment"):
            for key, value in self.expected_env.items():
                if os.environ.get(key) != value:
                    return subprocess.CompletedProcess(
                        command,
                        1,
                        "",
                        f"{key} missing from controlled target-account env",
                    )
            return subprocess.CompletedProcess(command, 0, "PATH=/usr/bin\n", "")
        if command[:3] == ("systemctl", "--user", "is-enabled"):
            return subprocess.CompletedProcess(command, 1, "disabled\n", "")
        if command[:3] == ("systemctl", "--user", "cat"):
            return subprocess.CompletedProcess(command, 0, "[Unit]\nDescription=test\n", "")
        if len(command) >= 4 and command[0] == "git" and command[2] == "cat-file":
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    def preflight(self, operation) -> PreflightResult:  # type: ignore[override]
        if operation.provenance.get("install_root") != "/":
            operation = dataclasses.replace(
                operation,
                provenance={**operation.provenance, "install_root": "/"},
            )
        return super().preflight(operation)

    def _current_account_only(self, account: str) -> PreflightResult:
        return PreflightResult(True, "current-account", f"test account {account}")

    def _unit_fire_bootstrap_blocker(self, operation):  # type: ignore[override]
        """T-5471: a ready host; the real gates are tested in test_persona_installer_converge."""
        return None

    def _bootstrap_fire_pending(self, timer):  # type: ignore[override]
        """T-5473: a ready host has no runtime state to quarantine and no user manager to ask;
        the quarantine is tested for real in test_persona_installer_phase_q."""
        return False

    def _identity_ready(self, operation) -> PreflightResult:  # type: ignore[override]
        return PreflightResult(True, "identity", f"test identity {operation.owner}")


def _parse_env_scoped_target_command(command: str) -> tuple[dict[str, str], str, list[str]]:
    outer = shlex.split(command)
    if outer[:4] != ["sudo", "-iu", outer[2], "bash"]:
        raise AssertionError(f"not a target-account sudo/bash command: {outer!r}")
    inner = outer[outer.index("-lc") + 1]
    env_argv = shlex.split(inner)
    if env_argv[0] != "/usr/bin/env":
        raise AssertionError(f"inner command is not /usr/bin/env wrapped: {env_argv!r}")
    assignments: dict[str, str] = {}
    index = 1
    while index < len(env_argv) and "=" in env_argv[index] and not env_argv[index].startswith("/"):
        key, value = env_argv[index].split("=", 1)
        assignments[key] = value
        index += 1
    if env_argv[index:index + 2] != ["/bin/bash", "-c"]:
        raise AssertionError(f"inner shell is not /bin/bash -c: {env_argv!r}")
    return assignments, env_argv[index + 2], env_argv


def _assert_target_env_shape(testcase: unittest.TestCase, command: str) -> tuple[dict[str, str], str]:
    assignments, compound, env_argv = _parse_env_scoped_target_command(command)
    testcase.assertEqual(list(assignments), ["PATH", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"])
    testcase.assertNotIn("/bin/bash -lc", command)
    testcase.assertEqual(env_argv[:6], [
        "/usr/bin/env",
        f"PATH={assignments['PATH']}",
        f"XDG_RUNTIME_DIR={assignments['XDG_RUNTIME_DIR']}",
        f"DBUS_SESSION_BUS_ADDRESS={assignments['DBUS_SESSION_BUS_ADDRESS']}",
        "/bin/bash",
        "-c",
    ])
    return assignments, compound


def _step(sheet: Mapping[str, object], step_id: str) -> Mapping[str, object]:
    steps = sheet["steps"]
    assert isinstance(steps, list)
    for step in steps:
        assert isinstance(step, Mapping)
        if step.get("step_id") == step_id:
            return step
    raise AssertionError(f"step {step_id!r} not found")


def _last_json_line(stdout: str) -> Mapping[str, object]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError("command produced no JSON output")
    payload = json.loads(lines[-1])
    assert isinstance(payload, Mapping)
    return payload


def _manifest() -> dict:
    source_hashes = dict(bridge.CODEX_TELEGRAM_SOURCE_SHA256)
    return {
        "manifest_version": 2,
        "instance": {"id": "test"},
        "persona_installer": {
            "schema": "pantheon-persona-install/v1",
            "framework": {"source": "pantheon-config", "revision": "test"},
            "paths": {
                "repo": "pantheon-config",
                "unit_dir": ".config/systemd/user",
                "state_dir": ".local/state/pantheon/persona-installer",
            },
            "mailbox": {"hub": "/mailbox", "group": "pantheon-mailbox"},
            "systemd": {
                "service_template": "tools/systemd/pantheon-persona-consume@.service",
                "timer_template": "tools/systemd/pantheon-persona-consume@.timer",
            },
            "policy": {"bundle": ["rulebook/", "sop/", "architecture/adr/"]},
            "security": {"credential_mode": "references-only", "prohibit_auth_copy": True},
            "lifecycle": {
                "require_disabled_first_fire": True,
                "require_mailbox_send_receive": True,
                "require_finite_next_elapse": True,
                "require_observed_timed_fire": True,
            },
            "codex_telegram_bridge": {
                "schema": "pantheon-codex-telegram-bridge/v1",
                "source": {
                    "repo": "pantheon-config",
                    "revision": bridge.CODEX_TELEGRAM_SOURCE_REVISION,
                    "subdir": bridge.CODEX_TELEGRAM_SOURCE_SUBDIR,
                    "paths": list(bridge.CODEX_TELEGRAM_SOURCE_PATHS),
                    "sha256": source_hashes,
                },
                "unit": {"name": "codex-telegram-poller.service"},
                "operator_chat_id": 424242424,
                "personas": {
                    "specialist": {
                        "account": "claude-research",
                        "claude_receiver_topology": "runtime-owned",
                        "claude_receiver_unit": "claude-telegram-research.service",
                        "tmux_session": "claude-research-bot",
                        "tmux_pane": "0.0",
                        "codex_receiver_unit": "codex-telegram-poller.service",
                        "credential_env": ".claude/channels/telegram/.env",
                        "state_dir": ".config/codex-telegram",
                        "release_root": ".local/lib/codex-telegram",
                        "mcp_name": "telegram",
                    },
                    "recovery": {
                        "account": "claude-fallback",
                        "claude_receiver_topology": "runtime-owned",
                        "claude_receiver_unit": "claude-telegram-fallback.service",
                        "tmux_session": "claude-telegram-bot-fallback",
                        "tmux_pane": "0.0",
                        "codex_receiver_unit": "codex-telegram-poller.service",
                        "credential_env": ".claude/channels/telegram/.env",
                        "state_dir": ".config/codex-telegram",
                        "release_root": ".local/lib/codex-telegram",
                        "mcp_name": "telegram",
                        "watchdog_units": [
                            "recovery-telegram-child-watchdog.timer",
                            "recovery-telegram-child-watchdog.service",
                        ],
                    },
                },
            },
        },
        "roles": {
            "specialist": {"persona": "specialist", "account": "claude-research", "harness": "claude-code"},
            "recovery": {"persona": "recovery", "account": "claude-fallback", "harness": "claude-code"},
        },
    }


def _pinned_codex_config_source(relative: str) -> bytes:
    """The bridge deploy path ``relative`` at the bridge pin, read from THIS repo's history.

    T-4790 (T-4750 C4b-3): the pin is pantheon-config's own harness/codex tree, so
    the object path carries ``CODEX_TELEGRAM_SOURCE_SUBDIR``. The history below
    describes the earlier codex-config root-level pin (1f84f64).

    T-4763 (ORG-091 / T-4750): C1 imported codex-config into harness/codex/ with its
    history, so the pin (``CODEX_TELEGRAM_SOURCE_REVISION``) is an ancestor here and
    its original root-level tree answers ``git show <pin>:<path>``. These tests used to
    read ``/home/codex/codex-config``, which only UID codex can, so the whole BridgeCase
    sat behind a skipUnless (T-3547 slice 4) on every other account and in CI. Needs
    full history (CI checks out with fetch-depth 0); a shallow clone fails loudly.
    Live /home/codex paths that are runtime CONFIG (the planned source_root, the
    source-check command line) are still asserted as such."""
    return subprocess.run(
        (
            "git",
            "-C",
            str(REPO_ROOT),
            "show",
            f"{bridge.CODEX_TELEGRAM_SOURCE_REVISION}:{bridge.CODEX_TELEGRAM_SOURCE_SUBDIR}/{relative}",
        ),
        check=True,
        capture_output=True,
    ).stdout


def _seed_claude_mcp_servers(home: Path, document: Mapping) -> None:
    """Seed ``home``/.claude.json with orchestrator's manifest MCP servers. The fixtures' fake
    claude CLI registers nothing, so without this the installer's MCP registration
    step (added after these tests went behind the T-3547 skip guard) fails its
    after-check; seeded, it is a matching no-op. T-4763."""
    servers = document["persona_installer"]["claude_mcp_servers"]["personas"]["orchestrator"]["servers"]
    (home / ".claude.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


class BridgeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest_path = self.root / "instance.yaml"
        self.manifest_path.write_text(yaml.safe_dump(_manifest(), sort_keys=False), encoding="utf-8")
        self.manifest = Manifest.load(self.manifest_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _mailbox_roundtrip_manifest(self) -> tuple[Path, str]:
        account = pwd.getpwuid(os.geteuid()).pw_name
        data = _manifest()
        data["roles"]["orchestrator"] = {
            "persona": "orchestrator",
            "account": account,
            "harness": "claude-code",
            "persona_brief_ref": "CLAUDE.md",
        }
        data["harness_runtime"] = {
            "schema": "pantheon-harness-runtime/v1",
            "harnesses": {
                "claude": {
                    "default_profile": "interactive-subscription",
                    "profiles": {
                        "interactive-subscription": {
                            "executable": "{account_home}/.npm-global/bin/claude",
                            "mode": "interactive",
                            "meter": "subscription",
                            "quota_source": "statusline",
                        }
                    },
                },
                "codex": {
                    "default_profile": "interactive-chatgpt",
                    "profiles": {
                        "interactive-chatgpt": {
                            "executable": "/usr/bin/codex",
                            "mode": "interactive",
                            "meter": "subscription",
                            "quota_source": "app-server",
                        }
                    },
                },
            },
            "roles": {
                "orchestrator": {
                    "fallbacks": ["codex/interactive-chatgpt"],
                    "tmux": {"session": "claude-telegram-bot"},
                    "claude": {"profile": "interactive-subscription"},
                    "codex": {
                        "profile": "interactive-chatgpt",
                        "mailbox_writable_roots": [
                            {"kind": "bound-mailbox"},
                            {"kind": "persona-mailbox", "persona": "codex"},
                        ],
                        "native": {"sandbox": "workspace-write"},
                    },
                }
            },
        }
        data["persona_installer"]["codex_telegram_bridge"]["personas"]["orchestrator"] = {
            "account": account,
            "claude_receiver_topology": "runtime-owned",
            "predecessor_claude_receiver_topology": "systemd-user",
            "claude_receiver_unit": "claude-telegram.service",
            "tmux_session": "claude-telegram-bot",
            "tmux_pane": "0.0",
            "codex_receiver_unit": "codex-telegram-poller.service",
            "credential_env": ".claude/channels/telegram/.env",
            "state_dir": ".config/codex-telegram",
            "release_root": ".local/lib/codex-telegram",
            "mcp_name": "telegram",
        }
        self.manifest_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return self.manifest_path, account

    def _telegram_evidence_manifest(self) -> tuple[Path, str]:
        manifest_path, account = self._mailbox_roundtrip_manifest()
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        instruction_text = "fixture Codex instructions\n"
        data["persona_installer"]["codex_standing_instructions"] = {
            "schema": "pantheon-codex-standing-instructions/v1",
            "personas": {
                "orchestrator": {
                    "source_repo": "pantheon-config",
                    "source": "pinned-rules/orchestrator-codex.md",
                    "source_commit": "0" * 40,
                    "source_sha256": hashlib.sha256(instruction_text.encode("utf-8")).hexdigest(),
                    "target": "AGENTS.md",
                    "override_target": "CLAUDE.md",
                    "identity": "Orchestrator",
                    "canonical_source_of_truth": "fixture",
                    "forbidden_source_paths": [],
                }
            },
        }
        manifest_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        repo_manifest = self.root / "home" / account / "pantheon-config" / "instance.yaml"
        repo_manifest.parent.mkdir(parents=True, exist_ok=True)
        source = repo_manifest.parent / "pinned-rules" / "orchestrator-codex.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(instruction_text, encoding="utf-8")
        repo_manifest.write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")
        return repo_manifest, account

    def _run_generated_bridge_command(self, step: Mapping[str, object]) -> tuple[int, Mapping[str, object]]:
        _assignments, compound = _assert_target_env_shape(self, str(step["command"]))
        argv = shlex.split(compound.rsplit(" && ", 1)[1])
        self.assertGreaterEqual(len(argv), 3)
        self.assertEqual(argv[0], "python3")
        self.assertEqual(argv[2], "telegram-unattended-evidence")
        with mock.patch("builtins.print") as printed:
            rc = bridge.main(argv[2:])
        payload = json.loads(printed.call_args.args[0])
        self.assertIsInstance(payload, Mapping)
        return rc, payload

    def _write_runtime_observation(
        self,
        prepare_receipt: Mapping[str, object],
        *,
        step_id: str,
        tool: str,
        canary: str,
        correlation_id: str,
        result_status: str = "success",
        permission_prompt_observed: bool = False,
        ui_approval_clicked: bool = False,
        invocation_tool: str | None = None,
    ) -> Path:
        steps = prepare_receipt["steps"]
        self.assertIsInstance(steps, Mapping)
        step = steps[step_id]
        self.assertIsInstance(step, Mapping)
        path = Path(str(step["runtime_observation_path"]))
        payload = {
            "schema": bridge.TELEGRAM_UNATTENDED_OBSERVATION_SCHEMA,
            "prepare_nonce": str(prepare_receipt["nonce"]),
            "step_id": step_id,
            "tool": tool,
            "canary": canary,
            "result_status": result_status,
            "correlation_id": correlation_id,
            "permission_prompt_observed": permission_prompt_observed,
            "ui_approval_clicked": ui_approval_clicked,
            "invocation": {"tool": invocation_tool or tool},
        }
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        path.chmod(0o600)
        return path

    def _provision_roundtrip_dirs(self) -> None:
        for persona in ("orchestrator", "codex"):
            for subdir in ("tmp", "new", "cur"):
                (self.root / "mailbox" / persona / subdir).mkdir(parents=True, exist_ok=True)
        (self.root / "home" / pwd.getpwuid(os.geteuid()).pw_name / ".local" / "state" / "pantheon" / "personas" / "orchestrator").mkdir(parents=True, exist_ok=True)

    def _load_roundtrip_state(self, account: str, correlation_id: str) -> Mapping[str, object]:
        state_path = (
            self.root
            / "home"
            / account
            / ".local"
            / "state"
            / "pantheon"
            / "personas"
            / "orchestrator"
            / "mailbox-roundtrip"
            / f"{correlation_id}.json"
        )
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertIsInstance(payload, Mapping)
        return payload

    def _append_controller_acceptance(self, *, account: str, message_id: str, correlation_id: str, recorded_at: str = "9999-01-01T00:00:00Z") -> None:
        evidence = (
            self.root
            / "home"
            / account
            / ".local"
            / "state"
            / "pantheon"
            / "personas"
            / "orchestrator"
            / "controller-evidence.jsonl"
        )
        evidence.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "schema": "pantheon-runtime-controller-evidence/v1",
            "event": "runtime_submission_accepted",
            "persona": "orchestrator",
            "recorded_at": recorded_at,
            "message_id": message_id,
            "correlation_id": correlation_id,
        }
        with evidence.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")

    def _ack_and_reply_roundtrip(self, *, account: str, correlation_id: str) -> Mapping[str, object]:
        state = self._load_roundtrip_state(account, correlation_id)
        message_id = str(state["source_message_id"])
        nonce = str(state["nonce"])
        inbox = self.root / "mailbox" / "orchestrator"
        (inbox / "cur").mkdir(parents=True, exist_ok=True)
        os.rename(inbox / "new" / message_id, inbox / "cur" / message_id)
        with (inbox / ".acks.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"id": message_id, "label": "codex-tui"}, sort_keys=True) + "\n")
        bridge.pantheon_mailbox.send(
            str(self.root / "mailbox" / "codex"),
            {
                "kind": "t2953_mailbox_roundtrip_reply",
                "recipient": "codex",
                "correlation_id": correlation_id,
                "nonce": nonce,
            },
            "orchestrator",
            subject=f"t2953-mailbox-reply:{correlation_id}",
            dedupe_pending=False,
        )
        return state

    def _write_fake_sudo(self, bin_dir: Path) -> Path:
        bin_dir.mkdir(parents=True, exist_ok=True)
        sudo = bin_dir / "sudo"
        sudo.write_text(
            "\n".join(
                (
                    "#!/usr/bin/env python3",
                    "import os, sys",
                    "expected = os.environ['EXPECT_SUDO_ACCOUNT']",
                    "if sys.argv[1:4] != ['-iu', expected, 'bash'] or sys.argv[4:5] != ['-lc'] or len(sys.argv) != 6:",
                    "    print('unexpected sudo argv: ' + repr(sys.argv[1:]), file=sys.stderr)",
                    "    raise SystemExit(97)",
                    "home = os.environ.get('EXPECT_SUDO_HOME')",
                    "if home:",
                    "    os.environ['HOME'] = home",
                    "os.execv('/bin/bash', ['bash', '-lc', sys.argv[5]])",
                    "",
                )
            ),
            encoding="utf-8",
        )
        sudo.chmod(0o755)
        return sudo

    def _run_operator_literal(
        self,
        command: str,
        *,
        account: str,
        cwd: Path,
        extra_env: Mapping[str, str] | None = None,
        fake_bin: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        sudo_bin = fake_bin or (self.root / "fake-bin")
        self._write_fake_sudo(sudo_bin)
        fixture_home = str((cwd.parent if cwd.name == "pantheon-config" else cwd))
        env = {
            "PATH": f"{sudo_bin}:/usr/local/bin:/usr/bin:/bin",
            # T-4763: pin the operator shell's HOME to the fixture home too. Without it
            # bash falls back to the PASSWD home, so `~` and the `-l` login profile read
            # the real account's files: green on a seat that has ~/pantheon-config, red
            # on the CI runner (/home/runner-pc) that does not.
            "HOME": fixture_home,
            "EXPECT_SUDO_ACCOUNT": account,
            "EXPECT_SUDO_HOME": fixture_home,
            **(dict(extra_env or {})),
        }
        return subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _run_operator_live_copy_literal(
        self,
        command: str,
        *,
        account: str,
        cwd: Path,
        extra_env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        sudo_bin = self.root / "fake-bin-live"
        self._write_fake_sudo(sudo_bin)
        fixture_home = str((cwd.parent if cwd.name == "pantheon-config" else cwd))
        env = {
            "PATH": f"{sudo_bin}:/usr/local/bin:/usr/bin:/bin",
            # T-4763: pin the operator shell's HOME to the fixture home too. Without it
            # bash falls back to the PASSWD home, so `~` and the `-l` login profile read
            # the real account's files: green on a seat that has ~/pantheon-config, red
            # on the CI runner (/home/runner-pc) that does not.
            "HOME": fixture_home,
            "EXPECT_SUDO_ACCOUNT": account,
            "EXPECT_SUDO_HOME": fixture_home,
            **(dict(extra_env or {})),
        }
        return subprocess.run(
            f'/bin/bash -lc "{command}"',
            shell=True,
            executable="/bin/bash",
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _write_bridge_shim(self, repo: Path) -> None:
        tool = repo / "tools" / "codex_telegram_bridge.py"
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_text(
            "\n".join(
                (
                    "#!/usr/bin/env python3",
                    "import sys",
                    f"sys.path.insert(0, {str(REPO_ROOT)!r})",
                    "from tools.codex_telegram_bridge import main",
                    "raise SystemExit(main())",
                    "",
                )
            ),
            encoding="utf-8",
        )
        tool.chmod(0o755)

    def _rooted_orchestrator_repo(self) -> tuple[Path, Path, Path, str, str, str]:
        account = pwd.getpwuid(os.geteuid()).pw_name
        root = self.root / "r11-root"
        home = root / "home" / account
        repo = home / "pantheon-config"
        source = repo / "pinned-rules" / "orchestrator-codex.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source_text = (REPO_ROOT / "pinned-rules" / "orchestrator-codex.md").read_text(encoding="utf-8")
        source.write_text(source_text, encoding="utf-8")
        (home / "CLAUDE.md").parent.mkdir(parents=True, exist_ok=True)
        (home / "CLAUDE.md").write_text("fixture brief\n", encoding="utf-8")
        (home / ".local" / "lib").mkdir(parents=True, exist_ok=True)
        fake_claude = home / ".npm-global" / "bin" / "claude"
        fake_claude.parent.mkdir(parents=True, exist_ok=True)
        fake_claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_claude.chmod(0o755)
        self._write_bridge_shim(repo)
        # T-5454: the seat clone's shell-box wrapper (operator prerequisite), committed with
        # the clone so the checkout stays clean and the local backend can exec it
        shell_box = repo / "tools" / "oom" / "pantheon-shell-box.sh"
        shell_box.parent.mkdir(parents=True, exist_ok=True)
        shell_box.write_text("#!/bin/sh\n", encoding="utf-8")
        shell_box.chmod(0o755)
        subprocess.run(("git", "init", "--initial-branch=main", str(repo)), check=True, capture_output=True, text=True)
        self._git(repo, "config", "user.email", "test@example.invalid")
        self._git(repo, "config", "user.name", "Test")
        self._git(repo, "add", "pinned-rules/orchestrator-codex.md", "tools/codex_telegram_bridge.py",
                  "tools/oom/pantheon-shell-box.sh")
        self._git(repo, "commit", "-m", "source")
        source_commit = self._git_text(repo, "rev-parse", "HEAD")
        source_sha = hashlib.sha256(source_text.encode("utf-8")).hexdigest()

        document = yaml.safe_load((REPO_ROOT / "instance.yaml").read_text(encoding="utf-8"))
        document["roles"]["head-engineering-orchestrator"]["account"] = account
        document["persona_installer"]["codex_telegram_bridge"]["personas"]["orchestrator"]["account"] = account
        document["persona_installer"]["claude_mcp_servers"]["personas"]["orchestrator"]["account"] = account
        _seed_claude_mcp_servers(home, document)
        ref = document["persona_installer"]["codex_standing_instructions"]["personas"]["orchestrator"]
        ref["source_commit"] = source_commit
        ref["source_sha256"] = source_sha
        manifest_path = repo / "instance.yaml"
        manifest_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        self._git(repo, "add", "instance.yaml")
        self._git(repo, "commit", "-m", "manifest")
        expected = self._git_text(repo, "rev-parse", "HEAD")
        return root, home, repo, expected, source_sha, account

    def _enable_repo_module_entrypoints(self, repo: Path) -> str:
        tools_dir = repo / "tools"
        (repo / ".gitignore").write_text(
            "\n".join(("__pycache__/", "tools/__pycache__/", "tools/bringup_acceptance/__pycache__/", "")),
            encoding="utf-8",
        )
        (tools_dir / "codex_telegram_bridge.py").write_text(
            (REPO_ROOT / "tools" / "codex_telegram_bridge.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (tools_dir / "codex_telegram_bridge.py").chmod(0o755)
        init = tools_dir / "__init__.py"
        init.write_text(
            "\n".join(
                (
                    "from pathlib import Path",
                    f"__path__.append(str(Path({str(REPO_ROOT / 'tools')!r})))",
                    "",
                )
            ),
            encoding="utf-8",
        )
        runtime_link = tools_dir / "runtime_controller.py"
        if not runtime_link.exists():
            runtime_link.symlink_to(REPO_ROOT / "tools" / "runtime_controller.py")
        self._git(repo, "add", ".gitignore", "tools/__init__.py", "tools/codex_telegram_bridge.py", "tools/runtime_controller.py")
        self._git(repo, "commit", "-m", "module entrypoints")
        return self._git_text(repo, "rev-parse", "HEAD")

    def _write_initial_viability_fixture(self, state_root: Path, *, persona: str = "orchestrator") -> None:
        path = state_root / persona / "runtime-controller" / "initial-viability.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": "pantheon-runtime-initial-viability/v1",
                    "persona": persona,
                    "status": "selected",
                    "selected_runtime": "codex/interactive-chatgpt",
                    "selected_harness": "codex",
                    "selected_profile": "interactive-chatgpt",
                    "attempts": [
                        {
                            "runtime": "claude/interactive-subscription",
                            "status": "failed",
                            "failure_class": "operational-readiness",
                        },
                        {
                            "runtime": "codex/interactive-chatgpt",
                            "status": "selected",
                            "readiness": {"event": "runtime_started"},
                            "receiver": {
                                "ok": True,
                                "operations": [
                                    "probe-claude-absent",
                                    "start-codex-poller",
                                    "probe-codex-present",
                                    "health-codex-no-409",
                                ],
                            },
                            "source_provenance": {
                                "revision": "1" * 40,
                                "source_manifest_sha256": "2" * 64,
                                "runtime_start_boundary_at": "2026-08-14T00:00:00Z",
                                "runtime_start_observed_at": "2026-08-14T00:00:01Z",
                            },
                        },
                    ],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def _write_proactive_swap_fixture(self, repo: Path) -> str:
        tools_dir = repo / "tools"
        (tools_dir / "proactive_swap_init.py").write_text(
            (REPO_ROOT / "tools" / "proactive_swap_init.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (tools_dir / "proactive_swap_init.py").chmod(0o755)
        (tools_dir / "persona_bringup.py").write_text(
            "\n".join(
                (
                    "from dataclasses import dataclass",
                    "",
                    "@dataclass",
                    "class Receipt:",
                    "    schema: str = 'pantheon-runtime-operator-target-swap/v1'",
                    "    persona: str = 'orchestrator'",
                    "    trigger: str = 'operator'",
                    "    correlation_id: str = ''",
                    "    destination: str = ''",
                    "    status: str = 'completed'",
                    "    phase: str = 'completed'",
                    "    source_runtime: str = 'codex/interactive-chatgpt'",
                    "    source_transition_id: str = 'fixture'",
                    "    outgoing_harness: str = 'codex'",
                    "    outgoing_profile: str = 'interactive-chatgpt'",
                    "    incoming_harness: str = 'claude'",
                    "    incoming_profile: str = 'interactive-subscription'",
                    "    receiver_transferred: bool = True",
                    "",
                    "def operator_target_swap(*, persona, destination, trigger, correlation_id, **_kwargs):",
                    "    return Receipt(persona=persona, trigger=trigger, correlation_id=correlation_id, destination=destination)",
                    "",
                )
            ),
            encoding="utf-8",
        )
        self._git(repo, "add", "tools/proactive_swap_init.py", "tools/persona_bringup.py")
        self._git(repo, "commit", "-m", "proactive swap fixture")
        return self._git_text(repo, "rev-parse", "HEAD")

    def _assert_no_shell_expansion_proof(self, command: str) -> None:
        self.assertNotIn("$", command)
        self.assertNotIn("`", command)
        self.assertNotIn("~", command)

    def _healthy_systemd_predecessor_backend(self, binding: bridge.CodexTelegramBinding):
        class HealthySystemdPredecessorBackend(bridge.LocalReceiverBackend):
            def _require_current_account(self, _binding):
                return None

            def _unit_probe(self, unit: str) -> bridge.UnitProbe:
                if unit == binding.claude_receiver_unit:
                    return bridge.UnitProbe(unit, "active", "running", "2415630")
                return bridge.UnitProbe(unit, "inactive", "dead", "0")

            def _unit_enabled_state(self, unit: str) -> str:
                if unit == binding.claude_receiver_unit:
                    return "enabled"
                return "disabled"

            def _tmux_pane_snapshot(self, _binding) -> bridge.TmuxPaneSnapshot:
                return bridge.TmuxPaneSnapshot(True, False, 2415640, "claude", 2415635)

            def _pid_cgroup_text(self, pid: int) -> str:
                if pid in {2415630, 2415635}:
                    return "0::/user.slice/user-1000.slice/user@1000.service/app.slice/claude-telegram.service\n"
                if pid == 2415640:
                    return "0::/user.slice/user-1000.slice/user@1000.service/app.slice/tmux-spawn-2415640.scope\n"
                raise bridge.BridgeError("cgroup evidence is unreadable")

            def _process_snapshots(self, _binding) -> tuple[bridge.ProcessSnapshot, ...]:
                return (
                    bridge.ProcessSnapshot(2415630, 1, "/usr/lib/systemd/user/claude-telegram.service"),
                    bridge.ProcessSnapshot(2415635, 1, "tmux new-session -s claude-telegram-bot"),
                    bridge.ProcessSnapshot(
                        2415640,
                        2415635,
                        f"{binding.account_home}/.npm-global/bin/claude --channels plugin:telegram@claude-plugins-official",
                    ),
                    bridge.ProcessSnapshot(
                        2415645,
                        2415640,
                        f"{binding.account_home}/.bun/bin/bun run --cwd {binding.account_home}/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6 --shell=bun --silent start",
                    ),
                    bridge.ProcessSnapshot(
                        2415650,
                        2415645,
                        "bun server.ts",
                        cwd=f"{binding.account_home}/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6",
                    ),
                )

            def _matching_processes(self, _binding, *, kind: str) -> list[str]:
                if kind == "codex":
                    return []
                return [
                    f"2415640 {binding.account_home}/.npm-global/bin/claude --channels plugin:telegram@claude-plugins-official"
                ]

        return HealthySystemdPredecessorBackend(timeout_seconds=0)

    def _service_cgroup_v2(self, unit: str) -> str:
        return f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}\n"

    def _service_cgroup_v1(self, unit: str) -> str:
        return f"9:cpu,cpuacct:/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}\n"

    def _write_proc_cgroups(self, proc_root: Path, entries: Mapping[int, str]) -> None:
        for pid, content in entries.items():
            path = proc_root / str(pid) / "cgroup"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def _service_owned_processes(self, binding: bridge.CodexTelegramBinding) -> tuple[bridge.ProcessSnapshot, ...]:
        return (
            bridge.ProcessSnapshot(2415630, 1, "/usr/lib/systemd/user/claude-telegram.service"),
            bridge.ProcessSnapshot(2415635, 1, "tmux new-session -s claude-telegram-bot"),
            bridge.ProcessSnapshot(
                2415640,
                2415635,
                f"{binding.account_home}/.npm-global/bin/claude --channels plugin:telegram@claude-plugins-official",
            ),
            bridge.ProcessSnapshot(
                2415645,
                2415640,
                f"{binding.account_home}/.bun/bin/bun run --cwd {binding.account_home}/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6 --shell=bun --silent start",
            ),
            bridge.ProcessSnapshot(2415650, 2415645, "bun server.ts"),
        )

    def _service_owned_tmux_backend(
        self,
        binding: bridge.CodexTelegramBinding,
        *,
        proc_root: Path,
        tmux_stdout: str = "2415635\t0\t2415640\tclaude\n",
        processes: tuple[bridge.ProcessSnapshot, ...] | None = None,
    ):
        if processes is None:
            processes = self._service_owned_processes(binding)

        class ServiceOwnedTmuxBackend(bridge.LocalReceiverBackend):
            def _require_current_account(self, _binding):
                return None

            def _run(self, argv):
                command = tuple(argv)
                if command[:3] == ("systemctl", "--user", "show"):
                    return subprocess.CompletedProcess(
                        list(command),
                        0,
                        stdout="ActiveState=active\nSubState=running\nMainPID=2415630\n",
                        stderr="",
                    )
                if command[:3] == ("systemctl", "--user", "is-enabled"):
                    return subprocess.CompletedProcess(list(command), 0, stdout="enabled\n", stderr="")
                if command[:3] == ("tmux", "display-message", "-p"):
                    return subprocess.CompletedProcess(list(command), 0, stdout=tmux_stdout, stderr="")
                if command[:3] == ("ps", "-u", binding.account):
                    if "pid=,ppid=,args=" in command:
                        stdout = "".join(f"{process.pid} {process.ppid} {process.args}\n" for process in processes)
                        return subprocess.CompletedProcess(list(command), 0, stdout=stdout, stderr="")
                    if "pid=,args=" in command:
                        stdout = "".join(f"{process.pid} {process.args}\n" for process in processes)
                        return subprocess.CompletedProcess(list(command), 0, stdout=stdout, stderr="")
                return subprocess.CompletedProcess(list(command), 1, stdout="", stderr="unexpected command")

        return ServiceOwnedTmuxBackend(timeout_seconds=0, proc_root=proc_root)

    def _stage_orchestrator_target_via_installer(self, *, root: Path, home: Path, repo: Path, account: str):
        for mailbox_persona in ("orchestrator", "codex"):
            for subdir in ("tmp", "new", "cur"):
                (root / "home" / "post-office" / "mailbox" / mailbox_persona / subdir).mkdir(parents=True, exist_ok=True)
        manifest = Manifest.load(repo / "instance.yaml")
        installer = PersonaInstaller(REPO_ROOT)
        plan = installer.plan(manifest, "orchestrator", harness_override="claude", root=root)
        archive = self._operation(plan, "codex-telegram.source-archive")
        source = bridge.CodexTelegramSource(
            bridge.CODEX_TELEGRAM_SOURCE_REPO,
            archive.desired["revision"],
            tuple(json.loads(archive.desired["paths"])),
            dict(json.loads(archive.desired["sha256"])),
            owner_account=archive.desired.get("source_account") or None,
            source_root=Path(archive.desired["source_root"]),
            subdir=archive.desired.get("source_subdir", ""),
        )
        # The plan's source_root is the codex-owned pantheon-config clone (T-4770);
        # the archive is built from the same pinned objects in this repo (T-4763).
        self.assertEqual(archive.desired["source_root"], "/home/codex/pantheon-config")
        bridge.create_verified_source_archive(
            REPO_ROOT,
            Path(archive.desired["archive_path"]),
            source,
        )
        expected_env = dict(controlled_apply_environment(home, pwd.getpwnam(account).pw_uid))
        backend = LocalHostCommandSheetStageBackend(expected_env)
        with mock.patch.dict(os.environ, expected_env, clear=True):
            report = installer.stage_disabled(
                plan,
                plan.digest,
                backend,
                ActorContext(
                    uid=os.geteuid(),
                    persona=plan.persona,
                    role=plan.role,
                    instance=plan.instance,
                    capabilities=_all_plan_capabilities(plan),
                ),
                manifest=manifest,
            )
        self.assertIn(report.status, {"applied", "noop"}, report.errors)
        activation_ids = {
            operation.operation_id
            for operation in plan.operations[installer._activation_index(plan) :]
        }
        self.assertFalse(set(backend.events) & activation_ids)
        return manifest, plan, backend

    def test_plan_specialist_is_disabled_first_and_secret_free(self) -> None:
        installer = PersonaInstaller(Path(__file__).resolve().parent.parent)
        plan = installer.plan(self.manifest, "specialist", harness_override="codex", root=self.root / "target")

        ids = [operation.operation_id for operation in plan.operations]
        self.assertLess(ids.index("codex-telegram.unit-install"), ids.index("codex-telegram.disabled-first"))
        self.assertLess(ids.index("codex-telegram.unit-install"), ids.index("codex-telegram.daemon-reload"))
        self.assertLess(ids.index("codex-telegram.daemon-reload"), ids.index("codex-telegram.disabled-first"))
        reload = self._operation(plan, "codex-telegram.daemon-reload")
        self.assertEqual(reload.desired["start"], "forbidden")
        self.assertEqual(reload.desired["enable"], "forbidden")
        payload = json.dumps(plan.to_dict(), sort_keys=True)
        self.assertIn('"claude_receiver_topology": "runtime-owned"', payload)
        self.assertIn("codex-telegram-poller.service", payload)
        self.assertNotIn(SECRET, payload)
        self.assertNotIn("EnvironmentFile", self._operation(plan, "codex-telegram.unit-install").desired["content"])

    def test_manifest_requires_explicit_valid_claude_receiver_topology(self) -> None:
        for persona in ("specialist", "recovery"):
            missing = _manifest()
            del missing["persona_installer"]["codex_telegram_bridge"]["personas"][persona]["claude_receiver_topology"]
            with self.subTest(persona=persona, case="missing"):
                with self.assertRaises(ManifestError):
                    Manifest.from_bytes(self.manifest_path, yaml.safe_dump(missing).encode("utf-8"))

            unknown = _manifest()
            unknown["persona_installer"]["codex_telegram_bridge"]["personas"][persona]["claude_receiver_topology"] = "tmux"
            with self.subTest(persona=persona, case="unknown"):
                with self.assertRaises(ManifestError):
                    Manifest.from_bytes(self.manifest_path, yaml.safe_dump(unknown).encode("utf-8"))

            missing_pane = _manifest()
            del missing_pane["persona_installer"]["codex_telegram_bridge"]["personas"][persona]["tmux_pane"]
            with self.subTest(persona=persona, case="missing-pane"):
                with self.assertRaises(ManifestError):
                    Manifest.from_bytes(self.manifest_path, yaml.safe_dump(missing_pane).encode("utf-8"))

            malformed_pane = _manifest()
            malformed_pane["persona_installer"]["codex_telegram_bridge"]["personas"][persona]["tmux_pane"] = "../0"
            with self.subTest(persona=persona, case="malformed-pane"):
                with self.assertRaises(ManifestError):
                    Manifest.from_bytes(self.manifest_path, yaml.safe_dump(malformed_pane).encode("utf-8"))

    def test_render_unit_specialist_uses_manifest_pane_env(self) -> None:
        binding = self.manifest.codex_telegram_binding("specialist")
        unit = bridge.render_unit(binding)
        self.assertIn("Environment=CODEX_TG_PANE=claude-research-bot:0.0\n", unit)
        self.assertNotIn("EnvironmentFile", unit)

    def test_render_unit_recovery_uses_manifest_pane_env(self) -> None:
        binding = self.manifest.codex_telegram_binding("recovery")
        unit = bridge.render_unit(binding)
        self.assertIn("Environment=CODEX_TG_PANE=claude-telegram-bot-fallback:0.0\n", unit)
        self.assertNotIn("EnvironmentFile", unit)

    def test_render_unit_has_no_codex_session_default(self) -> None:
        unit = bridge.render_unit(self.manifest.codex_telegram_binding("specialist"))
        self.assertNotIn("codex-session", unit)

    def test_manifest_rejects_unsafe_tmux_session_for_pane_env(self) -> None:
        bad_sessions = (
            "claude research",
            "claude:research",
            "claude'research",
            'claude"research',
            "claude%research",
            "claude/research",
            "..",
            "claude\nresearch",
        )
        for bad in bad_sessions:
            with self.subTest(session=bad):
                manifest = _manifest()
                manifest["persona_installer"]["codex_telegram_bridge"]["personas"]["specialist"]["tmux_session"] = bad
                with self.assertRaises(ManifestError):
                    Manifest.from_bytes(self.manifest_path, yaml.safe_dump(manifest).encode("utf-8"))

    def test_instance_specialist_and_recovery_declare_runtime_owned_topology(self) -> None:
        live = Manifest.load(Path(__file__).resolve().parent.parent / "instance.yaml")
        self.assertEqual(live.codex_telegram_binding("specialist").claude_receiver_topology, "runtime-owned")
        self.assertEqual(live.codex_telegram_binding("specialist").predecessor_claude_receiver_topology, "runtime-owned")
        self.assertEqual(live.codex_telegram_binding("specialist").tmux_pane, "0.0")
        self.assertEqual(live.codex_telegram_binding("recovery").claude_receiver_topology, "runtime-owned")
        self.assertEqual(live.codex_telegram_binding("recovery").predecessor_claude_receiver_topology, "runtime-owned")
        self.assertEqual(live.codex_telegram_binding("recovery").tmux_pane, "0.0")

    def test_manifest_orchestrator_bridge_binding_uses_account_local_runtime_owned_receiver(self) -> None:
        live = Manifest.load(Path(__file__).resolve().parent.parent / "instance.yaml")
        binding = live.codex_telegram_binding("orchestrator")
        self.assertEqual(binding.persona, "orchestrator")
        self.assertEqual(binding.account, "claude")
        self.assertEqual(binding.account_home, Path("/home/claude"))
        self.assertEqual(binding.claude_receiver_topology, "runtime-owned")
        self.assertEqual(binding.predecessor_claude_receiver_topology, "systemd-user")
        self.assertEqual(binding.claude_receiver_unit, "claude-telegram.service")
        self.assertEqual(binding.tmux_session, "claude-telegram-bot")
        self.assertEqual(binding.tmux_pane, "0.0")
        self.assertEqual(binding.codex_receiver_unit, "codex-telegram-poller.service")
        self.assertEqual(binding.source.owner_account, "codex")
        self.assertEqual(binding.source.source_root, Path("/home/codex/pantheon-config"))
        self.assertEqual(binding.source.repo, "pantheon-config")
        self.assertEqual(binding.source.subdir, "harness/codex")

    def test_render_unit_orchestrator_uses_manifest_pane_env(self) -> None:
        live = Manifest.load(Path(__file__).resolve().parent.parent / "instance.yaml")
        unit = bridge.render_unit(live.codex_telegram_binding("orchestrator"))
        self.assertIn("Environment=CODEX_TG_PANE=claude-telegram-bot:0.0\n", unit)
        self.assertNotIn("EnvironmentFile", unit)

    def test_orchestrator_cutover_stops_bespoke_receiver_before_codex_poller_start(self) -> None:
        _current, binding = persona_bringup.resolve_configured_runtime_target(
            Path(__file__).resolve().parent.parent / "instance.yaml",
            persona="orchestrator",
            destination="codex/interactive-chatgpt",
            state_root=self.root / "runtime-state",
        )
        events: list[str] = []

        class RecordingBackend:
            def execute(self, action: str, _binding) -> dict:
                events.append(action)
                return {"ok": True, "action": action}

        backend = RecordingBackend()
        pre = rc.run_receiver_handoff(binding, "claude-to-codex", "pre", backend=backend)
        post = rc.run_receiver_handoff(binding, "claude-to-codex", "post", backend=backend)
        self.assertIn("stop-dormant-claude-receiver", events)
        old_absent = [index for index, action in enumerate(events) if action == "probe-claude-absent"]
        self.assertTrue(old_absent)
        self.assertLess(old_absent[-1], events.index("start-codex-poller"))
        self.assertLess(events.index("start-codex-poller"), events.index("probe-codex-present"))
        self.assertEqual(post.operations, ("probe-claude-absent", "start-codex-poller", "probe-codex-present", "health-codex-no-409"))
        self.assertIn("stop-dormant-claude-receiver", pre.operations)

    def test_orchestrator_restore_stops_codex_poller_before_bespoke_receiver_start(self) -> None:
        binding = persona_bringup.load_binding(
            Path(__file__).resolve().parent.parent / "instance.yaml",
            "orchestrator",
        )
        events: list[str] = []

        class RecordingBackend:
            def execute(self, action: str, _binding) -> dict:
                events.append(action)
                return {"ok": True, "action": action}

        backend = RecordingBackend()
        pre = rc.run_receiver_handoff(binding, "rollback-to-claude", "pre", backend=backend)
        post = rc.run_receiver_handoff(binding, "rollback-to-claude", "post", backend=backend)
        self.assertLess(events.index("stop-codex-poller"), events.index("probe-codex-absent"))
        self.assertLess(events.index("probe-codex-absent"), events.index("probe-claude-present"))
        self.assertEqual(pre.operations, ("stop-codex-poller", "probe-codex-absent"))
        self.assertEqual(post.operations, ("probe-claude-present", "complete-receiver-topology-snapshot"))

    def test_rendered_allowlist_matches_pinned_codex_config_loader_contract(self) -> None:
        config_path = self.root / "pinned_config.py"
        config_path.write_bytes(_pinned_codex_config_source("codex_telegram/config.py"))
        spec = importlib.util.spec_from_file_location("pinned_codex_telegram_config", config_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        state = self.root / "state"
        state.mkdir()
        (state / "allowlist.json").write_text(bridge.render_allowlist(), encoding="utf-8")
        with mock.patch.dict(os.environ, {"CODEX_TG_STATE_DIR": str(state)}):
            allow = module.load_allowlist()
        self.assertIn(424242424, allow)
        self.assertNotIn(123, allow)

    def test_credential_wrapper_exports_only_telegram_bot_token(self) -> None:
        env_path = self.root / ".env"
        env_path.write_text(f"TELEGRAM_BOT_TOKEN={SECRET}\n", encoding="utf-8")
        env_path.chmod(0o600)
        token = bridge.load_telegram_token(env_path, expected_uid=os.geteuid())
        env = bridge.child_environment(token, {"PATH": "/bin", "OTHER_TOKEN": "leak", "BOT_SECRET": "leak"})
        self.assertEqual(env["TELEGRAM_BOT_TOKEN"], SECRET)
        self.assertNotIn("OTHER_TOKEN", env)
        self.assertNotIn("BOT_SECRET", env)

    def test_credential_wrapper_rejects_missing_duplicate_malformed_and_insecure_source(self) -> None:
        cases = {
            "missing": "",
            "duplicate": f"TELEGRAM_BOT_TOKEN={SECRET}\nTELEGRAM_BOT_TOKEN={SECRET}\n",
            "malformed": "export TELEGRAM_BOT_TOKEN=not a token\n",
            "ambiguous": f"BOT_TOKEN={SECRET}\n",
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                path = self.root / f"{name}.env"
                path.write_text(text, encoding="utf-8")
                path.chmod(0o600)
                with self.assertRaises(bridge.BridgeError):
                    bridge.load_telegram_token(path, expected_uid=os.geteuid())
        insecure = self.root / "insecure.env"
        insecure.write_text(f"TELEGRAM_BOT_TOKEN={SECRET}\n", encoding="utf-8")
        insecure.chmod(0o644)
        with self.assertRaises(bridge.BridgeError):
            bridge.load_telegram_token(insecure, expected_uid=os.geteuid())

    def test_active_webhook_fails_closed_without_delete(self) -> None:
        # Spec-derived fake shape from Telegram Bot API getWebhookInfo:
        # https://core.telegram.org/bots/api#getwebhookinfo checked 2026-08-13.
        client = bridge.FakeTelegramClient({"getWebhookInfo": {"ok": True, "result": {"url": "https://hook"}}})
        with self.assertRaises(bridge.BridgeError):
            bridge.telegram_preflight(client)
        self.assertEqual(client.calls, ["getWebhookInfo"])
        self.assertNotIn("deleteWebhook", client.calls)

    def test_telegram_preflight_cli_uses_existing_credential_loader_and_never_leaks_token(self) -> None:
        binding = SimpleNamespace(persona="specialist", account=pwd.getpwuid(os.geteuid()).pw_name, credential_env=self.root / "token.env")

        class Client:
            def __init__(self, token: str, *, timeout_seconds: float):
                self.calls: list[str] = []
                self.token = token
                self.timeout_seconds = timeout_seconds

            def call(self, method: str):
                self.calls.append(method)
                if method == "getWebhookInfo":
                    return {"ok": True, "result": {"url": ""}}
                return {"ok": True, "result": {"id": 123, "username": "specialist_bot"}}

        with mock.patch.object(bridge, "binding_for_current_account", return_value=binding) as current, mock.patch.object(
            bridge, "load_telegram_token", return_value=SECRET
        ) as loader, mock.patch.object(bridge, "TelegramApiClient", side_effect=Client), mock.patch("builtins.print") as printed:
            code = bridge.main(["telegram-preflight", "--persona", "specialist", "--timeout-seconds", "0.25", "--json"])
        self.assertEqual(code, 0)
        current.assert_called_once()
        loader.assert_called_once_with(binding.credential_env, expected_uid=os.geteuid())
        output = printed.call_args.args[0]
        self.assertNotIn(SECRET, output)
        payload = json.loads(output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["methods"], ["getWebhookInfo", "getMe"])
        self.assertEqual(payload["bot_id"], 123)

    def test_telegram_preflight_orders_getwebhookinfo_then_getme_only_when_empty_webhook(self) -> None:
        binding = SimpleNamespace(persona="recovery", account="claude-fallback", credential_env=self.root / "token.env")
        client = bridge.FakeTelegramClient(
            {
                "getWebhookInfo": {"ok": True, "result": {"url": ""}},
                "getMe": {"ok": True, "result": {"id": 456, "username": "recovery_bot"}},
            }
        )
        receipt = bridge.telegram_preflight_receipt(binding, client=client)
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["methods"], ["getWebhookInfo", "getMe"])
        self.assertEqual(client.calls, ["getWebhookInfo", "getMe"])

        active = bridge.FakeTelegramClient({"getWebhookInfo": {"ok": True, "result": {"url": "https://hook"}}})
        receipt = bridge.telegram_preflight_receipt(binding, client=active)
        self.assertFalse(receipt["ok"])
        self.assertTrue(receipt["webhook_active"])
        self.assertEqual(active.calls, ["getWebhookInfo"])

    def test_telegram_preflight_sanitizes_http_url_body_redirect_location_timeout_invalid_json_and_ok_false(self) -> None:
        with self.assertRaises(bridge.BridgeError) as raised:
            bridge._require_telegram_envelope(
                {"ok": False, "description": f"bad {SECRET} https://api.telegram.org/bot{SECRET}/getMe"},
                method="getMe",
                token=SECRET,
            )
        message = str(raised.exception)
        self.assertNotIn(SECRET, message)
        self.assertNotIn("api.telegram.org", message)

        class TimeoutOpener:
            def open(self, request, timeout):
                del request, timeout
                raise TimeoutError(f"timed out {SECRET}")

        with self.assertRaises(bridge.BridgeError) as raised:
            bridge.TelegramApiClient(SECRET, opener=TimeoutOpener()).call("getMe")
        self.assertNotIn(SECRET, str(raised.exception))

    def test_telegram_preflight_sanitizes_urlerror_reason_token_and_url_from_cli_json(self) -> None:
        binding = SimpleNamespace(persona="specialist", account=pwd.getpwuid(os.geteuid()).pw_name, credential_env=self.root / "token.env")

        class RaisingClient:
            def __init__(self, token: str, *, timeout_seconds: float):
                self.token = token
                self.timeout_seconds = timeout_seconds

            def call(self, method: str):
                raise urllib.error.URLError(f"boom {SECRET} https://api.telegram.org/bot{SECRET}/{method}")

        with mock.patch.object(bridge, "binding_for_current_account", return_value=binding), mock.patch.object(
            bridge, "load_telegram_token", return_value=SECRET
        ), mock.patch.object(bridge, "TelegramApiClient", side_effect=RaisingClient), mock.patch("builtins.print") as printed:
            code = bridge.main(["telegram-preflight", "--persona", "specialist", "--json"])
        self.assertEqual(code, 1)
        output = printed.call_args.args[0]
        self.assertNotIn(SECRET, output)
        self.assertNotIn("api.telegram.org", output)
        payload = json.loads(output)
        self.assertFalse(payload["ok"])
        self.assertLessEqual(len(payload["error"]), bridge.TELEGRAM_ERROR_LIMIT)

    def test_telegram_preflight_omits_malicious_reflected_username_from_json(self) -> None:
        binding = SimpleNamespace(persona="recovery", account="claude-fallback", credential_env=self.root / "token.env")
        client = bridge.FakeTelegramClient(
            {
                "getWebhookInfo": {"ok": True, "result": {"url": ""}},
                "getMe": {
                    "ok": True,
                    "result": {
                        "id": 789,
                        "username": f"{SECRET}_https://api.telegram.org/bot{SECRET}/getMe",
                    },
                },
            }
        )
        receipt = bridge.telegram_preflight_receipt(binding, client=client)
        payload = json.dumps(receipt, sort_keys=True)
        self.assertTrue(receipt["ok"])
        self.assertIsNone(receipt["bot_username"])
        self.assertNotIn(SECRET, payload)
        self.assertNotIn("api.telegram.org", payload)

    def test_apply_is_idempotent_and_verifies_pinned_source_hashes(self) -> None:
        source = self.root / "src"
        self._write_source_tree(source)
        receipt = bridge.compute_source_hashes(source, self.manifest.codex_telegram_source())
        self.assertEqual(receipt.revision, bridge.CODEX_TELEGRAM_SOURCE_REVISION)
        bad = dict(_manifest())
        bad["persona_installer"]["codex_telegram_bridge"]["source"]["sha256"]["run_server.py"] = "b" * 64
        with self.assertRaises(ManifestError):
            Manifest.from_bytes(self.manifest_path, yaml.safe_dump(bad).encode("utf-8"))

    def test_source_archive_extra_missing_and_path_traversal_rejected(self) -> None:
        source = self.manifest.codex_telegram_source()
        good = self._archive({"run_poller.py": b"x", "run_server.py": b"x"}, include_all=False)
        with self.assertRaises(bridge.BridgeError):
            bridge.verify_archive(good, source)
        extra = self._archive({path: b"x" for path in bridge.CODEX_TELEGRAM_SOURCE_PATHS} | {"extra.py": b"x"})
        with self.assertRaises(bridge.BridgeError):
            bridge.verify_archive(extra, source)
        traversal = self._archive({path: b"x" for path in bridge.CODEX_TELEGRAM_SOURCE_PATHS} | {"../escape": b"x"})
        with self.assertRaises(bridge.BridgeError):
            bridge.verify_archive(traversal, source)

    def test_receipts_and_command_output_never_contain_secret_values(self) -> None:
        redacted = bridge.redact_secrets({"argv": [SECRET], "nested": {"token": SECRET}}, [SECRET])
        self.assertNotIn(SECRET, json.dumps(redacted, sort_keys=True))

    def test_command_sheet_has_explicit_target_paths_and_executable_bidirectional_swap(self) -> None:
        expected_home = {"specialist": "/home/claude-research", "recovery": "/home/claude-fallback"}
        expected_revision = "f" * 40
        for persona, home in expected_home.items():
            with self.subTest(persona=persona):
                sheet = bridge.command_sheet(persona, expected_pantheon_revision=expected_revision)
                commands = sheet["commands"]
                joined = "\n".join(commands)
                self.assertNotIn("~", joined)
                self.assertNotIn("controller replaces", joined)
                self.assertNotIn("placeholder", joined.lower())
                self.assertNotIn("direct-fire", joined)
                self.assertNotIn("harness.direct-fire", joined)
                self.assertIn(f"{home}/pantheon-config/tools/codex_telegram_bridge.py credential-check", joined)
                self.assertIn(f"{home}/pantheon-config/tools/codex_telegram_bridge.py bridge-stage", joined)
                self.assertIn(f"{home}/pantheon-config/tools/proactive_swap_init.py --persona {persona}", joined)
                self.assertIn(f"PANTHEON_PERSONA_STATE_DIR={home}/.local/state/pantheon/personas", joined)
                self.assertIn(f"pantheon-persona-consume@{persona}.service", joined)
                self.assertIn("--destination codex/interactive-chatgpt", joined)
                self.assertIn("--destination claude/interactive-subscription", joined)
                self.assertEqual(sheet["commands_authoritative"], False)
                generate = next(index for index, item in enumerate(commands) if "--source-repo /home/codex/pantheon-config" in item)
                install = next(index for index, item in enumerate(commands) if " && sudo install " in item)
                stage = next(index for index, item in enumerate(commands) if " bridge-stage " in item)
                swap = next(index for index, item in enumerate(commands) if "--destination codex/interactive-chatgpt" in item)
                restore = next(index for index, item in enumerate(commands) if "--destination claude/interactive-subscription" in item)
                self.assertLess(generate, install)
                self.assertLess(install, stage)
                self.assertLess(stage, swap)
                self.assertLess(swap, restore)
                for command in commands:
                    argv = shlex.split(command)
                    self.assertIn(argv[0], {"sudo", "python3", "CODEX_TG_SRC_DIR=$(mktemp"})
                    for index, token in enumerate(argv):
                        if token.endswith(".py") and "pantheon-config/tools" in token:
                            self.assertTrue((Path(__file__).resolve().parent / Path(token).name).is_file())
                with mock.patch("builtins.print"):
                    self.assertEqual(bridge.main(["command-sheet", "--persona", persona, "--expected-pantheon-revision", expected_revision]), 0)

    def test_t5066_archives_staged_in_private_mktemp_dirs_not_fixed_tmp_paths(self) -> None:
        for persona in ("specialist", "recovery"):
            with self.subTest(persona=persona):
                sheet = bridge.command_sheet(persona, expected_pantheon_revision="a" * 40)
                remediation = sheet["amendment_i_remediation"]
                # (a) no fixed predictable path anywhere in the emitted sheet
                self.assertNotIn("/tmp/codex-telegram-", str(sheet))
                self.assertNotIn("/tmp/codex-telegram-", str(remediation))
                for steps, create_id, stage_id in (
                    (sheet["steps"], "04-source-archive", "06-bridge-stage"),
                    (remediation["steps"], "ai-01-source-archive", "ai-07-bridge-stage-disabled"),
                ):
                    by_id = {step["step_id"]: step["command"] for step in steps if step["type"] == "command"}
                    create = by_id[create_id]
                    # (b) unpredictable dirs: source dir owned by the writer, target dir owned by the seat account
                    self.assertIn("CODEX_TG_SRC_DIR=$(mktemp -d)", create)
                    self.assertRegex(create, r"CODEX_TG_DST_DIR=\$\(sudo -u \S+ mktemp -d\)")
                    self.assertIn('--archive "$CODEX_TG_SRC_DIR/', create)
                    self.assertLess(create.index("mktemp -d"), create.index("source-check"))
                    install = next(c for c in by_id.values() if " && sudo install " in c)
                    self.assertIn('"$CODEX_TG_SRC_DIR/', install)
                    self.assertIn('"$CODEX_TG_DST_DIR/', install)
                    stage = by_id[stage_id]
                    # the seat account gets the dir as a positional arg across the sudo hop
                    self.assertIn(' _ "$CODEX_TG_DST_DIR/', stage)
                    self.assertIn('--archive "$1" --json', stage)
                    # (c) cleanup emitted, only after the stage succeeds, guarded against empty vars
                    self.assertIn('rm -rf -- "${CODEX_TG_SRC_DIR:?}"', stage)
                    self.assertIn('sudo rm -rf -- "${CODEX_TG_DST_DIR:?}"', stage)
                    self.assertLess(stage.index("bridge-stage"), stage.index("rm -rf"))
                    self.assertTrue(stage.rstrip().endswith("unset CODEX_TG_SRC_DIR CODEX_TG_DST_DIR"))

    def test_t5068_orchestrator_sheet_stages_archives_in_private_mktemp_dirs(self) -> None:
        sheet = bridge.command_sheet("orchestrator", expected_pantheon_revision="4" * 40)
        self.assertNotIn("/tmp/codex-telegram-", str(sheet))
        by_id = {step["step_id"]: step["command"] for step in sheet["steps"] if step["type"] == "command"}
        create = by_id["05-source-archive"]
        # source dir owned by the source account that writes it; target dir by the seat account
        self.assertRegex(create, r"^CODEX_TG_SRC_DIR=\$\(sudo -u codex mktemp -d\) && ")
        self.assertRegex(create, r"CODEX_TG_DST_DIR=\$\(sudo -u claude mktemp -d\) && ")
        self.assertLess(create.index("mktemp -d"), create.index("source-check"))
        stem = f"codex-telegram-orchestrator-{bridge.CODEX_TELEGRAM_SOURCE_REVISION}"
        self.assertTrue(create.rstrip().endswith(f' _ "$CODEX_TG_SRC_DIR/{stem}.codex.tar"'))
        outer = shlex.split(create.split(" && ", 2)[2])
        self.assertIn('--archive "$1"', outer[outer.index("-lc") + 1].replace("'", ""))
        install = by_id["06-install-archive"]
        self.assertIn('"$CODEX_TG_SRC_DIR/', install)
        self.assertIn('"$CODEX_TG_DST_DIR/', install)
        stage = by_id["08-stage-disabled-instructions-and-bridge"]
        self.assertTrue(stage.rstrip().endswith(' _ "$CODEX_TG_DST_DIR"'))
        _assignments, compound = _assert_target_env_shape(self, stage)
        self.assertIn('--codex-telegram-archive-dir "$1"', compound)
        # activation preflight at step 14 still reads the archive: cleanup rides there, not earlier
        for early in ("05-source-archive", "06-install-archive", "07-stage1-cutover-dry-run", "08-stage-disabled-instructions-and-bridge"):
            self.assertNotIn("rm -rf", by_id[early])
        execute = by_id["14-stage1-cutover-execute"]
        self.assertIn('rm -rf -- "${CODEX_TG_SRC_DIR:?}"', execute)
        self.assertIn('sudo rm -rf -- "${CODEX_TG_DST_DIR:?}"', execute)
        self.assertLess(execute.index("persona_cutover.sh"), execute.index("rm -rf"))
        self.assertTrue(execute.rstrip().endswith("unset CODEX_TG_SRC_DIR CODEX_TG_DST_DIR"))

    def test_command_sheet_uses_ordered_steps_and_legacy_commands_are_non_authoritative(self) -> None:
        sheet = bridge.command_sheet("specialist", expected_pantheon_revision="a" * 40)
        self.assertEqual(sheet["schema"], bridge.COMMAND_SHEET_SCHEMA)
        self.assertFalse(sheet["commands_authoritative"])
        self.assertEqual([step["command"] for step in sheet["steps"] if step["type"] == "command"], sheet["commands"])
        for step in sheet["steps"]:
            self.assertIn(step["type"], {"command", "operator-gate"})
            if step["type"] == "command":
                argv = shlex.split(step["command"])
                self.assertTrue(argv)
                # T-5066: the first archive step opens with the private staging-dir assignment.
                self.assertIn(argv[0], {"sudo", "python3", "CODEX_TG_SRC_DIR=$(mktemp"})
                if "bash" in argv:
                    inner = argv[argv.index("-lc") + 1]
                    shlex.split(inner)
                    for tool in ("codex_telegram_bridge.py", "proactive_swap_init.py"):
                        if tool in inner:
                            self.assertTrue((Path(__file__).resolve().parent / tool).is_file())

    def test_ordered_steps_put_codex_acceptance_gates_before_reverse(self) -> None:
        steps = bridge.command_sheet("recovery", expected_pantheon_revision="b" * 40)["steps"]
        ids = [step["step_id"] for step in steps]
        codex_done = ids.index("09-gate-codex-completed")
        reverse = ids.index("15-swap-to-claude")
        for step_id in (
            "10-telegram-text-canary",
            "11-telegram-document-canary",
            "12-telegram-photo-canary",
            "13-mailbox-codex-consume-reply-wake",
            "14-restart-offset-preservation",
        ):
            self.assertLess(codex_done, ids.index(step_id))
            self.assertLess(ids.index(step_id), reverse)
            self.assertEqual(steps[ids.index(step_id)]["type"], "operator-gate")

    def test_restore_acceptance_gates_follow_claude_completion(self) -> None:
        steps = bridge.command_sheet("specialist", expected_pantheon_revision="c" * 40)["steps"]
        ids = [step["step_id"] for step in steps]
        claude_done = ids.index("17-gate-claude-completed")
        self.assertLess(claude_done, ids.index("18-restore-receiver-state"))
        self.assertLess(claude_done, ids.index("19-restore-mailbox-delivery"))

    def test_source_remediation_has_exact_expected_sha_proof_before_ff_only_and_no_destructive_git(self) -> None:
        expected = "d" * 40
        remediation = bridge.source_remediation("claude-research", Path("/home/claude-research/pantheon-config"), expected_head=expected)
        joined = "\n".join(remediation["commands"])
        inner = "\n".join(bridge._source_remediation_inner_commands(Path("/home/claude-research/pantheon-config"), expected_head=expected))
        self.assertIn(expected, joined)
        self.assertIn("fetch origin +main:refs/remotes/origin/main", joined)
        self.assertIn("rev-parse --verify 'refs/remotes/origin/main^{commit}'", inner)
        self.assertNotIn(" reset ", joined)
        self.assertNotIn(" clean ", joined)
        self.assertNotIn(" checkout ", joined)
        for forbidden in ("receiver", "systemd", "tmux", "operator"):
            self.assertNotIn(forbidden, inner)
        self.assertLess(inner.index("status --porcelain=v1"), inner.index("fetch origin +main:refs/remotes/origin/main"))
        self.assertLess(inner.index("rev-parse --verify 'refs/remotes/origin/main^{commit}'"), inner.index(f"merge --ff-only {expected}"))
        for command in remediation["commands"]:
            argv = shlex.split(command)
            self.assertEqual(argv[:4], ["sudo", "-iu", "claude-research", "bash"])
            inner = argv[argv.index("-lc") + 1]
            self.assertIn("set -euo pipefail;", inner)
            if "$(" in inner:
                self.assertIn("git -C /home/claude-research/pantheon-config", inner)

    def test_source_remediation_synthetic_bare_remote_refreshes_stale_origin_main_and_merges_expected(self) -> None:
        _remote, _source, target, first, expected = self._source_remediation_fixture("refresh")
        self.assertEqual(self._git_text(target, "rev-parse", "refs/remotes/origin/main"), first)
        failed, result = self._run_source_remediation_inner(target, expected)
        self.assertIsNone(failed, result.stderr + result.stdout)
        self.assertEqual(self._git_text(target, "rev-parse", "refs/remotes/origin/main"), expected)
        self.assertEqual(self._git_text(target, "rev-parse", "HEAD"), expected)

    def test_source_remediation_wrong_expected_fails_before_merge(self) -> None:
        _remote, _source, target, first, expected = self._source_remediation_fixture("wrong-expected")
        failed, _result = self._run_source_remediation_inner(target, first)
        self.assertEqual(failed, 3)
        self.assertEqual(self._git_text(target, "rev-parse", "refs/remotes/origin/main"), expected)
        self.assertEqual(self._git_text(target, "rev-parse", "HEAD"), first)

    def test_source_remediation_dirty_worktree_fails_before_fetch(self) -> None:
        _remote, _source, target, first, expected = self._source_remediation_fixture("dirty")
        (target / "payload.txt").write_text("dirty\n", encoding="utf-8")
        failed, _result = self._run_source_remediation_inner(target, expected)
        self.assertEqual(failed, 1)
        self.assertEqual(self._git_text(target, "rev-parse", "refs/remotes/origin/main"), first)
        self.assertEqual(self._git_text(target, "rev-parse", "HEAD"), first)

    def test_source_remediation_divergent_local_head_fails_merge_without_head_change(self) -> None:
        _remote, _source, target, _first, expected = self._source_remediation_fixture("divergent")
        (target / "local.txt").write_text("local\n", encoding="utf-8")
        self._git(target, "add", "local.txt")
        self._git(target, "commit", "-m", "local")
        local_head = self._git_text(target, "rev-parse", "HEAD")
        failed, _result = self._run_source_remediation_inner(target, expected)
        self.assertEqual(failed, 4)
        self.assertEqual(self._git_text(target, "rev-parse", "refs/remotes/origin/main"), expected)
        self.assertEqual(self._git_text(target, "rev-parse", "HEAD"), local_head)

    def test_source_remediation_remote_rewind_refreshes_ref_but_ff_only_prevents_worktree_rewind(self) -> None:
        remote, source, target, first, second = self._source_remediation_fixture("rewind", clone_after_second=True)
        self._git(source, "reset", "--hard", first)
        self._git(source, "push", "--force", "origin", "main")
        self.assertEqual(self._git_text(target, "rev-parse", "HEAD"), second)
        self.assertEqual(self._git_text(target, "rev-parse", "refs/remotes/origin/main"), second)
        failed, _result = self._run_source_remediation_inner(target, first)
        self.assertEqual(failed, 5)
        self.assertEqual(self._git_text(target, "rev-parse", "refs/remotes/origin/main"), first)
        self.assertEqual(self._git_text(target, "rev-parse", "HEAD"), second)
        self.assertEqual(self._git_text(Path(remote), "rev-parse", "main"), first)

    def test_missing_target_source_currency_tool_fails_closed_until_remediation(self) -> None:
        expected = "e" * 40
        sheet = bridge.command_sheet("recovery", expected_pantheon_revision=expected)
        self.assertEqual(sheet["source_remediation"]["expected_pantheon_revision"], expected)
        for step in sheet["steps"]:
            if step["type"] == "command" and step["step_id"] in {
                "05-install-archive",
                "06-bridge-stage",
                "07-swap-to-codex",
                "08-start-codex-consume",
                "09-gate-codex-completed",
                "15-swap-to-claude",
                "16-start-claude-consume",
                "17-gate-claude-completed",
            }:
                command = step["command"]
                self.assertIn(" source-currency ", command)
                self.assertIn(expected, command)
        install = next(step["command"] for step in sheet["steps"] if step["step_id"] == "05-install-archive")
        self.assertRegex(install, r"^sudo -iu claude-fallback bash -lc .* && sudo install ")

    def test_command_sheet_requires_explicit_valid_expected_pantheon_revision(self) -> None:
        with self.assertRaises(TypeError):
            bridge.command_sheet("specialist")  # type: ignore[call-arg]
        for bad in ("", "g" * 40, "a" * 39, "A" * 40):
            with self.subTest(bad=bad):
                with self.assertRaises(bridge.BridgeError):
                    bridge.command_sheet("specialist", expected_pantheon_revision=bad)

    def test_command_sheet_embeds_supplied_expected_revision_in_remediation_and_all_gates(self) -> None:
        expected = "1" * 40
        sheet = bridge.command_sheet("specialist", expected_pantheon_revision=expected)
        self.assertIn(expected, "\n".join(sheet["source_remediation"]["commands"]))
        gated = [
            step["command"]
            for step in sheet["steps"]
            if step["type"] == "command" and "source-currency" in step["command"]
        ]
        self.assertGreaterEqual(len(gated), 8)
        self.assertTrue(all(expected in command for command in gated))

    def test_command_sheet_rerun_gate_is_manual_retry_safe_and_blocks_later_steps_on_pending(self) -> None:
        sheet = bridge.command_sheet("specialist", expected_pantheon_revision="2" * 40)
        self.assertEqual(sheet["completion_gate"], {"retry_safe_same_command": True, "blind_loop": False})
        gate = next(step for step in sheet["steps"] if step["step_id"] == "09-gate-codex-completed")
        command = gate["command"]
        self.assertIn("set -euo pipefail", command)
        self.assertIn("--correlation-id t2950-specialist-to-codex", command)
        self.assertIn("--completion-gate", command)
        self.assertIn("--expect-correlation-id t2950-specialist-to-codex", command)
        self.assertIn("--expect-destination codex/interactive-chatgpt", command)

    def test_orchestrator_command_sheet_requires_codex_instruction_discovery_before_swap(self) -> None:
        sheet = bridge.command_sheet("orchestrator", expected_pantheon_revision="4" * 40)
        steps = sheet["steps"]
        ids = [step["step_id"] for step in steps]
        target = next(index for index, step_id in enumerate(ids) if step_id.endswith("postinstall-instruction-target"))
        discovery = next(
            index
            for index, step in enumerate(steps)
            if step["type"] == "command" and step["step_id"].endswith("codex-instruction-discovery")
        )
        discovery_gate = next(
            index
            for index, step in enumerate(steps)
            if step["type"] == "operator-gate" and step["step_id"].endswith("gate-codex-instruction-discovery")
        )
        stage_disabled = next(
            index
            for index, step in enumerate(steps)
            if step["type"] == "command" and step["step_id"].endswith("stage-disabled-instructions-and-bridge")
        )
        execute = next(
            index
            for index, step in enumerate(steps)
            if step["type"] == "command" and step["step_id"].endswith("stage1-cutover-execute")
        )
        viability = next(
            index
            for index, step in enumerate(steps)
            if step["type"] == "command" and step["step_id"].endswith("gate-initial-viability")
        )
        self.assertLess(target, discovery)
        self.assertLess(discovery, discovery_gate)
        self.assertLess(discovery_gate, execute)
        self.assertLess(execute, viability)
        self.assertNotIn("11-start-controller-consume-viability", ids)
        stage_command = steps[stage_disabled]["command"]
        self.assertIn("t2611_recovery_persona_apply.py stage-disabled", stage_command)
        self.assertIn("--stamp", stage_command)
        self.assertIn("<operator-stamp>", stage_command)
        source_command = next(step["command"] for step in steps if step["step_id"] == "04-instruction-source-check")
        self.assertIn("instruction-source-proof", source_command)
        self.assertIn("--root /", source_command)
        self.assertNotIn("awk", source_command)
        self.assertNotIn("sha256sum", source_command)
        target_command = steps[target]["command"]
        self.assertIn("instruction-target-proof", target_command)
        self.assertIn("--root /", target_command)
        self.assertNotIn("awk", target_command)
        self.assertNotIn("sha256sum", target_command)
        viability_command = steps[viability]["command"]
        self.assertIn("cd /home/claude/pantheon-config && python3 -m tools.runtime_controller initial-viability", viability_command)
        self.assertIn("--expect-selected codex/interactive-chatgpt", viability_command)
        self.assertIn("--expect-failed claude/interactive-subscription", viability_command)
        self.assertIn("--expect-failure-class subscription-cap", viability_command)
        self.assertIn("--expect-failure-class operational-launch", viability_command)
        self.assertIn("--expect-failure-class operational-readiness", viability_command)
        self.assertIn("--require-source-provenance", viability_command)
        self.assertIn("--require-receiver-receipt", viability_command)

        command = steps[discovery]["command"]
        argv = shlex.split(command)
        self.assertEqual(argv[:4], ["sudo", "-iu", "claude", "bash"])
        _assignments, compound = _assert_target_env_shape(self, command)
        self.assertIn(" source-currency ", compound)
        codex_argv = shlex.split(compound.split(" && ", 1)[1])
        self.assertEqual(codex_argv[:4], ["/usr/bin/timeout", "120s", "/usr/bin/codex", "exec"])
        self.assertIn("--ephemeral", codex_argv)
        self.assertEqual(codex_argv[codex_argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(codex_argv[codex_argv.index("--cd") + 1], "/home/claude/pantheon-config")
        prompt = codex_argv[-1]
        for required in (
            "Do not run commands",
            "mutate files",
            "contact the network",
            "start Telegram polling",
            "secret values",
            "at most 8 bullets",
            "Orchestrator/HO identity and authority",
            "mandatory canonical /home/claude/CLAUDE.md read-before-action",
            "Telegram reply mapping mcp__telegram__reply",
            "canonical Orchestrator rules remain source-of-truth",
        ):
            self.assertIn(required, prompt)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", command)

        gate = steps[discovery_gate]["gate"]
        self.assertTrue(gate["confirmation_required"])
        self.assertEqual(gate["kind"], "codex-instruction-discovery-human-confirmation")
        self.assertEqual(
            gate["expected_facts"],
            [
                "Orchestrator/HO identity and authority",
                "mandatory canonical /home/claude/CLAUDE.md read-before-action",
                "Telegram reply mapping mcp__telegram__reply",
                "canonical Orchestrator rules remain source-of-truth",
            ],
        )

    def test_orchestrator_predecessor_topology_accepts_healthy_systemd_claude_before_cutover(self) -> None:
        manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        binding = manifest.codex_telegram_binding("orchestrator")
        self.assertEqual(binding.claude_receiver_topology, "runtime-owned")
        self.assertEqual(binding.predecessor_claude_receiver_topology, "systemd-user")
        sheet = bridge.command_sheet("orchestrator", expected_pantheon_revision="4" * 40)
        command = _step(sheet, "13-gate-claude-present")["command"]
        assert isinstance(command, str)
        _assignments, compound = _assert_target_env_shape(self, command)
        receiver_argv = shlex.split(compound.split(" && ", 1)[1])
        self.assertEqual(receiver_argv[2], "receiver-state")
        self.assertEqual(receiver_argv[receiver_argv.index("--topology-phase") + 1], "predecessor")
        backend = self._healthy_systemd_predecessor_backend(binding)
        with mock.patch.object(bridge, "binding_for_current_account", return_value=binding), \
             mock.patch.object(bridge, "LocalReceiverBackend", return_value=backend), \
             mock.patch("builtins.print") as printed:
            code = bridge.main(receiver_argv[2:])
        self.assertEqual(code, 0)
        payload = json.loads(printed.call_args.args[0])
        self.assertEqual(payload["evidence"]["topology_phase"], "predecessor")
        self.assertEqual(payload["evidence"]["topology"], "systemd-user")
        self.assertEqual(payload["evidence"]["managed_claude_receiver_topology"], "runtime-owned")

    def test_orchestrator_predecessor_topology_accepts_service_cgroup_owned_tmux_sibling_before_cutover(self) -> None:
        manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        binding = manifest.codex_telegram_binding("orchestrator")
        sheet = bridge.command_sheet("orchestrator", expected_pantheon_revision="4" * 40)
        command = _step(sheet, "13-gate-claude-present")["command"]
        assert isinstance(command, str)
        _assignments, compound = _assert_target_env_shape(self, command)
        receiver_argv = shlex.split(compound.split(" && ", 1)[1])
        self.assertEqual(receiver_argv[2], "receiver-state")
        self.assertEqual(receiver_argv[receiver_argv.index("--topology-phase") + 1], "predecessor")
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = Path(tmp) / "proc"
            self._write_proc_cgroups(
                proc_root,
                {
                    2415630: self._service_cgroup_v2(binding.claude_receiver_unit),
                    2415635: self._service_cgroup_v1(binding.claude_receiver_unit),
                    2415640: "0::/user.slice/user-1000.slice/user@1000.service/app.slice/tmux-spawn-2415640.scope\n",
                },
            )
            backend = self._service_owned_tmux_backend(binding, proc_root=proc_root)
            with mock.patch.object(bridge, "binding_for_current_account", return_value=binding), \
                 mock.patch.object(bridge, "LocalReceiverBackend", return_value=backend), \
                 mock.patch("builtins.print") as printed:
                code = bridge.main(receiver_argv[2:])
            self.assertEqual(code, 0)
            payload = json.loads(printed.call_args.args[0])
            evidence = payload["evidence"]
            self.assertEqual(evidence["topology_phase"], "predecessor")
            self.assertEqual(evidence["topology"], "systemd-user")
            self.assertEqual(evidence["unit_main_cgroup_match"], "true")
            self.assertEqual(evidence["tmux_server_cgroup_match"], "true")
            self.assertEqual(evidence["tmux_server_pid"], "2415635")
            self.assertEqual(evidence["tmux_pane_pid"], "2415640")
            self.assertEqual(evidence["plugin_launcher_descendant_count"], "1")
            self.assertEqual(evidence["plugin_server_descendant_count"], "1")
            self.assertEqual(evidence["plugin_orphan_count"], "0")
            with self.assertRaisesRegex(
                bridge.BridgeError,
                "runtime-owned Claude receiver has active/enabled dormant systemd supervisor",
            ):
                backend._require_present(binding, kind="claude", topology_phase="managed")

    def test_systemd_predecessor_rejects_tmux_server_outside_service_cgroup(self) -> None:
        binding = bridge.dataclasses.replace(Manifest.load(REPO_ROOT / "instance.yaml").codex_telegram_binding("orchestrator"), claude_receiver_topology="systemd-user")
        processes = self._service_owned_processes(binding)
        with self.assertRaisesRegex(bridge.BridgeError, "tmux server is outside service cgroup"):
            bridge._evaluate_systemd_user_claude_state(
                binding,
                expected="present",
                unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "active", "running", "2415630"),
                tmux=bridge.TmuxPaneSnapshot(True, False, 2415640, "claude", 2415635),
                processes=processes,
                unit_main_cgroup=self._service_cgroup_v2(binding.claude_receiver_unit),
                tmux_server_cgroup="0::/user.slice/not-claude-telegram.service\n",
            )
        with self.assertRaisesRegex(bridge.BridgeError, "main PID is outside service cgroup"):
            bridge._evaluate_systemd_user_claude_state(
                binding,
                expected="present",
                unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "active", "running", "2415630"),
                tmux=bridge.TmuxPaneSnapshot(True, False, 2415640, "claude", 2415635),
                processes=processes,
                unit_main_cgroup="0::/user.slice/claude-telegram.service-extra\n",
                tmux_server_cgroup=self._service_cgroup_v2(binding.claude_receiver_unit),
            )

    def test_systemd_predecessor_rejects_wrong_named_session_or_multiple_server_pid_evidence(self) -> None:
        binding = bridge.dataclasses.replace(Manifest.load(REPO_ROOT / "instance.yaml").codex_telegram_binding("orchestrator"), claude_receiver_topology="systemd-user")
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = Path(tmp) / "proc"
            self._write_proc_cgroups(
                proc_root,
                {
                    2415630: self._service_cgroup_v2(binding.claude_receiver_unit),
                    2415635: self._service_cgroup_v2(binding.claude_receiver_unit),
                },
            )
            missing = self._service_owned_tmux_backend(binding, proc_root=proc_root, tmux_stdout="")
            with mock.patch.object(
                missing,
                "_run",
                return_value=subprocess.CompletedProcess(["tmux"], 1, stdout="", stderr="can't find pane"),
            ):
                snapshot = missing._tmux_pane_snapshot(binding)
            self.assertFalse(snapshot.exists)
            with self.assertRaisesRegex(bridge.BridgeError, "tmux server PID is invalid"):
                bridge._evaluate_systemd_user_claude_state(
                    binding,
                    expected="present",
                    unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "active", "running", "2415630"),
                    tmux=snapshot,
                    processes=self._service_owned_processes(binding),
                    unit_main_cgroup=self._service_cgroup_v2(binding.claude_receiver_unit),
                    tmux_server_cgroup=self._service_cgroup_v2(binding.claude_receiver_unit),
                )

            for stdout in (
                "2415635\t0\t2415640\tclaude\n2415636\t0\t2415641\tclaude\n",
                "server\t0\t2415640\tclaude\n",
                "2415635\t0\tpane\tclaude\n",
                "2415635\t0\t2415640\n",
            ):
                backend = self._service_owned_tmux_backend(binding, proc_root=proc_root, tmux_stdout=stdout)
                with self.subTest(stdout=stdout):
                    with self.assertRaises(bridge.BridgeError):
                        backend._tmux_pane_snapshot(binding)

    def test_systemd_predecessor_rejects_pane_not_descended_from_resolved_server(self) -> None:
        binding = bridge.dataclasses.replace(Manifest.load(REPO_ROOT / "instance.yaml").codex_telegram_binding("orchestrator"), claude_receiver_topology="systemd-user")
        processes = (
            bridge.ProcessSnapshot(2415630, 1, "/usr/lib/systemd/user/claude-telegram.service"),
            bridge.ProcessSnapshot(2415635, 1, "tmux new-session -s claude-telegram-bot"),
            bridge.ProcessSnapshot(
                2415640,
                1,
                f"{binding.account_home}/.npm-global/bin/claude --channels plugin:telegram@claude-plugins-official",
            ),
            bridge.ProcessSnapshot(
                2415645,
                2415640,
                f"{binding.account_home}/.bun/bin/bun run --cwd {binding.account_home}/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6 --shell=bun --silent start",
            ),
            bridge.ProcessSnapshot(2415650, 2415645, "bun server.ts"),
        )
        with self.assertRaisesRegex(bridge.BridgeError, "not anchored to tmux server"):
            bridge._evaluate_systemd_user_claude_state(
                binding,
                expected="present",
                unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "active", "running", "2415630"),
                tmux=bridge.TmuxPaneSnapshot(True, False, 2415640, "claude", 2415635),
                processes=processes,
                unit_main_cgroup=self._service_cgroup_v2(binding.claude_receiver_unit),
                tmux_server_cgroup=self._service_cgroup_v2(binding.claude_receiver_unit),
            )

    def test_systemd_predecessor_rejects_duplicate_claude_pane_mismatch_and_bad_cgroup_evidence(self) -> None:
        binding = bridge.dataclasses.replace(Manifest.load(REPO_ROOT / "instance.yaml").codex_telegram_binding("orchestrator"), claude_receiver_topology="systemd-user")
        processes = self._service_owned_processes(binding)
        unit_cgroup = self._service_cgroup_v2(binding.claude_receiver_unit)

        def evaluate(
            candidate: tuple[bridge.ProcessSnapshot, ...] = processes,
            *,
            unit_main_cgroup: str = unit_cgroup,
            tmux_server_cgroup: str = unit_cgroup,
            tmux: bridge.TmuxPaneSnapshot = bridge.TmuxPaneSnapshot(True, False, 2415640, "claude", 2415635),
        ) -> Mapping[str, str]:
            return bridge._evaluate_systemd_user_claude_state(
                binding,
                expected="present",
                unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "active", "running", "2415630"),
                tmux=tmux,
                processes=candidate,
                unit_main_cgroup=unit_main_cgroup,
                tmux_server_cgroup=tmux_server_cgroup,
            )

        with self.assertRaisesRegex(bridge.BridgeError, "ambiguous tmux second supervisor"):
            evaluate(
                processes
                + (
                    bridge.ProcessSnapshot(2415641, 2415635, f"{binding.account_home}/.npm-global/bin/claude --channels plugin:telegram@claude-plugins-official"),
                )
            )
        with self.assertRaisesRegex(bridge.BridgeError, "ambiguous tmux second supervisor"):
            evaluate(tmux=bridge.TmuxPaneSnapshot(True, False, 2415635, "claude", 2415635))
        with self.assertRaisesRegex(bridge.BridgeError, "plugin group is not present"):
            evaluate(tuple(process for process in processes if process.pid != 2415645))
        with self.assertRaisesRegex(bridge.BridgeError, "plugin group is duplicate"):
            evaluate(processes + (bridge.ProcessSnapshot(2415646, 2415640, processes[3].args),))
        with self.assertRaisesRegex(bridge.BridgeError, "plugin group is not present"):
            evaluate(tuple(process for process in processes if process.pid != 2415650))
        with self.assertRaisesRegex(bridge.BridgeError, "plugin group is duplicate"):
            evaluate(processes + (bridge.ProcessSnapshot(2415651, 2415645, "bun server.ts"),))
        with self.assertRaisesRegex(bridge.BridgeError, "orphan supervisor"):
            evaluate(processes + (bridge.ProcessSnapshot(2415652, 1, "bun server.ts", cwd=f"{binding.account_home}/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6"),))
        for cgroup in ("", "not:a:relative", "malformed", "0::/user.slice/not-claude-telegram.service\n"):
            with self.subTest(cgroup=cgroup):
                with self.assertRaises(bridge.BridgeError):
                    evaluate(unit_main_cgroup=cgroup)
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = Path(tmp) / "proc"
            backend = bridge.LocalReceiverBackend(timeout_seconds=0, proc_root=proc_root)
            with self.assertRaisesRegex(bridge.BridgeError, "PID is invalid"):
                backend._pid_cgroup_text(0)
            with self.assertRaisesRegex(bridge.BridgeError, "unreadable"):
                backend._pid_cgroup_text(2415630)

    def test_orchestrator_managed_runtime_owned_topology_still_rejects_active_systemd_supervisor(self) -> None:
        binding = Manifest.load(REPO_ROOT / "instance.yaml").codex_telegram_binding("orchestrator")
        backend = self._healthy_systemd_predecessor_backend(binding)
        with self.assertRaisesRegex(
            bridge.BridgeError,
            "runtime-owned Claude receiver has active/enabled dormant systemd supervisor",
        ):
            backend._require_present(binding, kind="claude", topology_phase="managed")
        evidence = backend._require_present(binding, kind="claude", topology_phase="predecessor")
        self.assertEqual(evidence["topology"], "systemd-user")
        self.assertEqual(evidence["topology_phase"], "predecessor")

    def test_orchestrator_command_sheet_step13_uses_predecessor_topology_and_step16_uses_managed_topology(self) -> None:
        sheet = bridge.command_sheet("orchestrator", expected_pantheon_revision="4" * 40)
        step12 = _step(sheet, "12-prove-codex-absent")["command"]
        step13 = _step(sheet, "13-gate-claude-present")["command"]
        step16 = _step(sheet, "16-prove-claude-absent")["command"]
        step17 = _step(sheet, "17-prove-codex-present")["command"]
        assert isinstance(step12, str)
        assert isinstance(step13, str)
        assert isinstance(step16, str)
        assert isinstance(step17, str)
        self.assertIn("receiver-state --persona orchestrator --kind claude --expected present --topology-phase predecessor", step13)
        self.assertIn("receiver-state --persona orchestrator --kind claude --expected absent --topology-phase managed", step16)
        self.assertNotIn("--topology-phase", step12)
        self.assertNotIn("--topology-phase", step17)
        with mock.patch.object(bridge, "binding_for_current_account", return_value=Manifest.load(REPO_ROOT / "instance.yaml").codex_telegram_binding("orchestrator")), \
             mock.patch("builtins.print"):
            self.assertEqual(
                bridge.main(
                    [
                        "receiver-state",
                        "--persona",
                        "orchestrator",
                        "--kind",
                        "codex",
                        "--expected",
                        "absent",
                        "--topology-phase",
                        "managed",
                        "--json",
                    ]
                ),
                1,
            )
        restore_gate = _step(sheet, "27-restore-receiver-state")["gate"]
        self.assertEqual(restore_gate["claude_topology_phase"], "predecessor")
        self.assertEqual(restore_gate["codex"], "absent")
        self.assertEqual(restore_gate["claude"], "present")

    def test_orchestrator_receiver_handoff_snapshots_predecessor_but_proves_managed_absent_before_codex_start(self) -> None:
        sheet = bridge.command_sheet("orchestrator", expected_pantheon_revision="4" * 40)
        step13 = _step(sheet, "13-gate-claude-present")["command"]
        assert isinstance(step13, str)
        self.assertIn("--topology-phase predecessor", step13)
        _current, binding = persona_bringup.resolve_configured_runtime_target(
            REPO_ROOT / "instance.yaml",
            persona="orchestrator",
            destination="codex/interactive-chatgpt",
            state_root=self.root / "runtime-state",
        )
        self.assertEqual(binding.communications["claude_receiver_topology"], "runtime-owned")
        self.assertEqual(binding.communications["predecessor_claude_receiver_topology"], "systemd-user")
        pre_actions = rc.receiver_handoff_actions(binding, "claude-to-codex", "pre")
        post_actions = rc.receiver_handoff_actions(binding, "claude-to-codex", "post")
        self.assertIn("snapshot-dormant-claude-unit", pre_actions)
        self.assertNotIn("snapshot-claude-receiver", pre_actions)
        self.assertLess(post_actions.index("probe-claude-absent"), post_actions.index("start-codex-poller"))

        class RecordingBackend:
            def __init__(self):
                self.events: list[str] = []
                self.topologies: list[str] = []

            def execute(self, action: str, handoff_binding) -> dict:
                self.events.append(action)
                self.topologies.append(handoff_binding.communications["claude_receiver_topology"])
                return {"ok": True, "action": action}

        backend = RecordingBackend()
        rc.run_receiver_handoff(binding, "claude-to-codex", "pre", backend=backend)
        rc.run_receiver_handoff(binding, "claude-to-codex", "post", backend=backend)
        self.assertTrue(backend.topologies)
        self.assertEqual(set(backend.topologies), {"runtime-owned"})
        self.assertLess(backend.events.index("probe-claude-absent"), backend.events.index("start-codex-poller"))

    def test_orchestrator_rollback_accepts_restored_predecessor_without_weakening_post_cutover_runtime_owned_invariant(self) -> None:
        binding = Manifest.load(REPO_ROOT / "instance.yaml").codex_telegram_binding("orchestrator")
        sheet = bridge.command_sheet("orchestrator", expected_pantheon_revision="4" * 40)
        restore_gate = _step(sheet, "27-restore-receiver-state")["gate"]
        self.assertEqual(restore_gate["claude_topology_phase"], "predecessor")
        pre = bridge.handoff_actions(binding, "codex-to-claude", "pre")
        post = bridge.handoff_actions(binding, "codex-to-claude", "post")
        self.assertEqual(pre[:2], ("stop-codex-poller", "probe-codex-absent"))
        self.assertIn("probe-claude-present", post)
        self.assertNotIn("restore-claude-receiver", post)
        rollback = (REPO_ROOT / "tools" / "persona_rollback.sh").read_text(encoding="utf-8")
        self.assertLess(rollback.index('systemctl --user stop "$CODEX_RECEIVER_UNIT"'), rollback.index('systemctl --user enable "$PCX_SERVICE"'))
        self.assertLess(rollback.index('systemctl --user stop "$CODEX_RECEIVER_UNIT"'), rollback.index('systemctl --user restart "$PCX_SERVICE"'))
        backend = self._healthy_systemd_predecessor_backend(binding)
        self.assertEqual(backend._require_present(binding, kind="claude", topology_phase="predecessor")["topology"], "systemd-user")
        with self.assertRaisesRegex(bridge.BridgeError, "runtime-owned Claude receiver has active/enabled dormant systemd supervisor"):
            backend._require_present(binding, kind="claude", topology_phase="managed")

    def test_orchestrator_instruction_source_check_executes_literal_command_through_operator_shell_without_losing_hash_field(self) -> None:
        root, home, repo, expected, source_sha, account = self._rooted_orchestrator_repo()
        sheet = bridge.command_sheet(
            "orchestrator",
            expected_pantheon_revision=expected,
            manifest_path=repo / "instance.yaml",
            root=root,
        )
        command = _step(sheet, "04-instruction-source-check")["command"]
        assert isinstance(command, str)
        self.assertIn("instruction-source-proof", command)
        self.assertIn(f"--root {root}", command)
        self._assert_no_shell_expansion_proof(command)

        live_target = Path("/home") / account / ".codex" / "AGENTS.md"
        existed_before = live_target.exists()
        result = self._run_operator_literal(command, account=account, cwd=repo)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        receipt = _last_json_line(result.stdout)
        self.assertEqual(receipt["schema"], bridge.INSTRUCTION_PROOF_SCHEMA)
        self.assertEqual(receipt["kind"], "source")
        self.assertEqual(receipt["source_sha256"], source_sha)
        self.assertEqual(receipt["object_sha256"], source_sha)
        self.assertEqual(receipt["worktree_sha256"], source_sha)
        self.assertEqual(receipt["root"], str(root.resolve()))
        self.assertIn("mcp__telegram__reply", (repo / "pinned-rules" / "orchestrator-codex.md").read_text(encoding="utf-8"))
        self.assertEqual(live_target.exists(), existed_before)
        with self.assertRaises(bridge.BridgeError):
            bridge._instruction_source_proof(
                "orchestrator",
                manifest_path=repo / "instance.yaml",
                repo_root=repo,
                root=Path("relative-root"),
            )

        manifest = Manifest.load(repo / "instance.yaml")
        reference = codex_standing_instructions_reference(manifest, "orchestrator", root=root)
        self.assertIsNotNone(reference)
        assert reference is not None
        old_source_check = (
            "cd ~/pantheon-config && "
            f"git cat-file -e {shlex.quote(reference.source_commit + '^{commit}')} && "
            f"test \"$(sha256sum {shlex.quote(str(reference.source))} | awk '{{print $1}}')\" = "
            f"{shlex.quote(reference.source_sha256)}"
        )
        old_shape = bridge._sudo_bash(
            account,
            bridge._target_account_shell_command(
                account,
                root / "home" / account,
                f"set -euo pipefail; {old_source_check}",
            ),
        )
        self.assertIn("$", old_shape)
        fake_git = home / ".npm-global" / "bin" / "git"
        fake_git.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = cat-file ]; then exit 0; fi\n"
            "exec /usr/bin/git \"$@\"\n",
            encoding="utf-8",
        )
        fake_git.chmod(0o755)
        old = self._run_operator_live_copy_literal(old_shape, account=account, cwd=repo)
        # The deliberate negative: the OLD shape nests shlex quoting ('"'"') two layers
        # deep, and an operator pasting it into `bash -lc "..."` ends that wrapper's
        # double quotes at the first embedded `"`. The command is re-split at the wrong
        # layer: the innermost `bash -c` gets only a fragment with a dangling `"` (its $0
        # becomes `-euo`, hence "-euo: -c: ... unexpected EOF"), and the rest runs one
        # layer out, where `git cat-file` receives the object name with literal quote
        # characters glued on. Both are pure quoting effects; with HOME pinned (see
        # _run_operator_live_copy_literal) `cd ~/pantheon-config` lands in the fixture
        # repo, so neither depends on the runner's home (T-4763).
        failure = old.stderr + old.stdout
        self.assertEqual(old.returncode, 128, failure)  # git's fatal exit, not cd's 1
        self.assertIn("-euo: -c: line 1: unexpected EOF while looking for matching", failure)
        self.assertRegex(failure, r"fatal: Not a valid object name \S*'\S*\^\{commit\}")
        self.assertNotIn("No such file or directory", failure)

    def test_orchestrator_postinstall_instruction_target_executes_literal_command_through_operator_shell_without_losing_hash_field(self) -> None:
        root, home, repo, expected, source_sha, account = self._rooted_orchestrator_repo()
        manifest, _plan, backend = self._stage_orchestrator_target_via_installer(root=root, home=home, repo=repo, account=account)
        reference = codex_standing_instructions_reference(manifest, "orchestrator", root=root)
        self.assertIsNotNone(reference)
        assert reference is not None
        self.assertTrue(reference.target.is_file())

        sheet = bridge.command_sheet(
            "orchestrator",
            expected_pantheon_revision=expected,
            manifest_path=repo / "instance.yaml",
            root=root,
        )
        command = _step(sheet, "09-postinstall-instruction-target")["command"]
        assert isinstance(command, str)
        self.assertIn("instruction-target-proof", command)
        self.assertIn(f"--root {root}", command)
        self._assert_no_shell_expansion_proof(command)
        self.assertNotIn(str(Path("/home") / account / ".codex"), command)

        live_target = Path("/home") / account / ".codex" / "AGENTS.md"
        existed_before = live_target.exists()
        result = self._run_operator_literal(command, account=account, cwd=repo)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        receipt = _last_json_line(result.stdout)
        self.assertEqual(receipt["schema"], bridge.INSTRUCTION_PROOF_SCHEMA)
        self.assertEqual(receipt["kind"], "target")
        self.assertEqual(receipt["target_sha256"], source_sha)
        self.assertEqual(receipt["target"], str(reference.target))
        self.assertEqual(receipt["root"], str(root.resolve()))
        self.assertEqual(live_target.exists(), existed_before)
        events_after_stage = tuple(backend.events)

        old_target_check = (
            f"test -f {shlex.quote(str(reference.target))} && "
            f"test \"$(sha256sum {shlex.quote(str(reference.target))} | awk '{{print $1}}')\" = "
            f"{shlex.quote(reference.source_sha256)} && "
            f"test ! -s {shlex.quote(str(reference.override_target))}"
        )
        old_shape = bridge._gated_target_command(
            account,
            home,
            repo / "tools" / "codex_telegram_bridge.py",
            repo,
            expected,
            old_target_check,
        )
        self.assertIn("$", old_shape)
        old = self._run_operator_live_copy_literal(old_shape, account=account, cwd=repo)
        self.assertNotEqual(old.returncode, 0)
        self.assertRegex(old.stderr + old.stdout, r"\{print \}|awk")
        self.assertEqual(tuple(backend.events), events_after_stage)

        reference.target.write_text("drift\n", encoding="utf-8")
        drift = self._run_operator_literal(command, account=account, cwd=repo)
        self.assertNotEqual(drift.returncode, 0)
        self.assertIn("target hash mismatch", drift.stdout)
        self.assertEqual(tuple(backend.events), events_after_stage)
        reference.target.unlink()
        deleted = self._run_operator_literal(command, account=account, cwd=repo)
        self.assertNotEqual(deleted.returncode, 0)
        self.assertIn("target missing", deleted.stdout)
        self.assertEqual(tuple(backend.events), events_after_stage)
        reference.target.write_text((repo / reference.source).read_text(encoding="utf-8"), encoding="utf-8")
        reference.override_target.parent.mkdir(parents=True, exist_ok=True)
        reference.override_target.write_text("override\n", encoding="utf-8")
        override = self._run_operator_literal(command, account=account, cwd=repo)
        self.assertNotEqual(override.returncode, 0)
        self.assertIn("override is not empty", override.stdout)
        self.assertEqual(tuple(backend.events), events_after_stage)

    def test_command_sheet_shell_expansion_surfaces_are_audited_or_expansion_free(self) -> None:
        expected = "4" * 40
        surfaces: list[tuple[str, str]] = []
        for persona in ("specialist", "recovery", "orchestrator"):
            sheet = bridge.command_sheet(persona, expected_pantheon_revision=expected)
            for step in sheet["steps"]:
                if step["type"] == "command":
                    surfaces.append((f"{persona}:{step['step_id']}", step["command"]))
            remediation = sheet.get("amendment_i_remediation")
            if isinstance(remediation, Mapping):
                for step in remediation["steps"]:
                    if step["type"] == "command":
                        surfaces.append((f"{persona}:amendment:{step['step_id']}", step["command"]))
            source = sheet.get("source_remediation")
            if isinstance(source, Mapping):
                for index, command in enumerate(source["commands"]):
                    surfaces.append((f"{persona}:source-remediation:{index}", command))

        allowed = {
            "specialist:amendment:ai-03-snapshot-unit-offset",
            "specialist:amendment:ai-12-prove-startup-pane",
            "specialist:amendment:ai-13-prove-no-sustained-409",
            "recovery:amendment:ai-03-snapshot-unit-offset",
            "recovery:amendment:ai-12-prove-startup-pane",
            "recovery:amendment:ai-13-prove-no-sustained-409",
            "orchestrator:amendment:ai-03-snapshot-unit-offset",
            "orchestrator:amendment:ai-12-prove-startup-pane",
            "orchestrator:amendment:ai-13-prove-no-sustained-409",
        }
        # T-5066: the archive-staging steps expand operator-shell staging-dir variables on purpose.
        for expanding_persona in ("specialist", "recovery"):
            allowed.update(f"{expanding_persona}:{step_id}" for step_id in ("04-source-archive", "05-install-archive", "06-bridge-stage"))
        # T-5068: so does the orchestrator sheet (create, install, stage-disabled, and cleanup-on-cutover steps).
        allowed.update(
            f"orchestrator:{step_id}"
            for step_id in ("05-source-archive", "06-install-archive", "08-stage-disabled-instructions-and-bridge", "14-stage1-cutover-execute")
        )
        for expanding_persona in ("specialist", "recovery", "orchestrator"):
            allowed.update(
                f"{expanding_persona}:amendment:{step_id}"
                for step_id in ("ai-01-source-archive", "ai-02-install-archive", "ai-07-bridge-stage-disabled")
            )
        offenders = []
        for label, command in surfaces:
            has_expansion = "$" in command or "`" in command or "~" in command
            allowed_expansion = label in allowed or ":source-remediation:" in label
            if has_expansion and not allowed_expansion:
                offenders.append(label)
        self.assertEqual(offenders, [])
        for step_id in ("04-instruction-source-check", "09-postinstall-instruction-target"):
            command = _step(bridge.command_sheet("orchestrator", expected_pantheon_revision=expected), step_id)["command"]
            assert isinstance(command, str)
            self._assert_no_shell_expansion_proof(command)

    def test_legacy_and_amendment_commands_survive_operator_shell_quote_layers(self) -> None:
        account = pwd.getpwuid(os.geteuid()).pw_name
        root = self.root / "legacy-root"
        home = root / "home" / account
        repo = home / "pantheon-config"
        repo.mkdir(parents=True)
        self._write_bridge_shim(repo)
        document = _manifest()
        document["roles"]["specialist"]["account"] = account
        document["persona_installer"]["codex_telegram_bridge"]["personas"]["specialist"]["account"] = account
        manifest_path = repo / "instance.yaml"
        manifest_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        subprocess.run(("git", "init", "--initial-branch=main", str(repo)), check=True, capture_output=True, text=True)
        self._git(repo, "config", "user.email", "test@example.invalid")
        self._git(repo, "config", "user.name", "Test")
        self._git(repo, "add", "instance.yaml", "tools/codex_telegram_bridge.py")
        self._git(repo, "commit", "-m", "fixture")
        expected = self._git_text(repo, "rev-parse", "HEAD")

        bin_dir = home / ".npm-global" / "bin"
        bin_dir.mkdir(parents=True)
        fake_systemctl = bin_dir / "systemctl"
        fake_systemctl.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "if sys.argv[1:4] == ['--user', 'cat', 'codex-telegram-poller.service']:\n"
            "    print('[Service]')\n"
            "elif sys.argv[1:5] == ['--user', 'show', '-p', 'MainPID']:\n"
            "    print(os.getppid())\n"
            "else:\n"
            "    print('ok')\n",
            encoding="utf-8",
        )
        fake_systemctl.chmod(0o755)
        fake_journal = bin_dir / "journalctl"
        fake_journal.write_text("#!/bin/sh\nprintf 'ready\\n'\n", encoding="utf-8")
        fake_journal.chmod(0o755)

        sheet = bridge.command_sheet(
            "specialist",
            expected_pantheon_revision=expected,
            manifest_path=manifest_path,
            root=root,
        )
        remediation = sheet["amendment_i_remediation"]
        steps = {step["step_id"]: step["command"] for step in remediation["steps"] if step["type"] == "command"}
        env = {"CODEX_TG_PANE": "claude-research-bot:0.0"}
        for step_id in ("ai-03-snapshot-unit-offset", "ai-12-prove-startup-pane", "ai-13-prove-no-sustained-409"):
            with self.subTest(step=step_id):
                result = self._run_operator_literal(steps[step_id], account=account, cwd=repo, extra_env=env, fake_bin=bin_dir)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertTrue((home / ".config" / "codex-telegram" / "receipts" / "amendment-i-pane-restage" / "unit.before.sha256").is_file())

        remote, source, target, _first, second = self._source_remediation_fixture("literal-source-remediation")
        del remote, source
        source_sheet = bridge.source_remediation(account, target, expected_head=second)
        for command in source_sheet["commands"]:
            result = self._run_operator_literal(command, account=account, cwd=target)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_orchestrator_command_sheet_stages_instructions_before_cutover_activation(self) -> None:
        sheet = bridge.command_sheet("orchestrator", expected_pantheon_revision="4" * 40)
        steps = sheet["steps"]
        ids = [step["step_id"] for step in steps]
        self.assertLess(
            ids.index("08-stage-disabled-instructions-and-bridge"),
            ids.index("09-postinstall-instruction-target"),
        )
        self.assertLess(
            ids.index("09-postinstall-instruction-target"),
            ids.index("10-codex-instruction-discovery"),
        )
        self.assertLess(
            ids.index("12-prove-codex-absent"),
            ids.index("14-stage1-cutover-execute"),
        )
        self.assertLess(
            ids.index("14-stage1-cutover-execute"),
            ids.index("15-gate-initial-viability"),
        )
        self.assertNotIn("start-controller-consume-viability", "\n".join(ids))

    def test_orchestrator_compatibility_source_and_discovery_contract_require_exact_telegram_reply_identifier(self) -> None:
        root = self.root / "r10-root"
        home = root / "home" / "claude"
        repo = home / "pantheon-config"
        (home / ".npm-global" / "bin").mkdir(parents=True)
        (home / ".local" / "lib").mkdir(parents=True)
        (home / "CLAUDE.md").write_text("Orchestrator brief fixture\n", encoding="utf-8")
        for mailbox_persona in ("orchestrator", "codex"):
            for subdir in ("tmp", "new", "cur"):
                (root / "home" / "post-office" / "mailbox" / mailbox_persona / subdir).mkdir(parents=True, exist_ok=True)
        fake_claude = home / ".npm-global" / "bin" / "claude"
        fake_claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_claude.chmod(0o755)
        _seed_claude_mcp_servers(home, yaml.safe_load((REPO_ROOT / "instance.yaml").read_text(encoding="utf-8")))
        repo.symlink_to(REPO_ROOT, target_is_directory=True)

        manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        reference = codex_standing_instructions_reference(manifest, "orchestrator", root=root)
        self.assertIsNotNone(reference)
        assert reference is not None
        source_text = _read_verified_standing_instructions(reference, repo_root=REPO_ROOT)
        self.assertIn("mcp__telegram__reply", source_text)
        self.assertEqual(reference.source_sha256, R10_ORCHESTRATOR_CODEX_SHA256)

        installer = PersonaInstaller(REPO_ROOT)
        plan = installer.plan(manifest, "orchestrator", harness_override="claude", root=root)
        archive = self._operation(plan, "codex-telegram.source-archive")
        source = bridge.CodexTelegramSource(
            bridge.CODEX_TELEGRAM_SOURCE_REPO,
            archive.desired["revision"],
            tuple(json.loads(archive.desired["paths"])),
            dict(json.loads(archive.desired["sha256"])),
            owner_account=archive.desired.get("source_account") or None,
            source_root=Path(archive.desired["source_root"]),
            subdir=archive.desired.get("source_subdir", ""),
        )
        # The plan's source_root is the codex-owned pantheon-config clone (T-4770);
        # the archive is built from the same pinned objects in this repo (T-4763).
        self.assertEqual(archive.desired["source_root"], "/home/codex/pantheon-config")
        bridge.create_verified_source_archive(
            REPO_ROOT,
            Path(archive.desired["archive_path"]),
            source,
        )
        expected_env = dict(controlled_apply_environment(home, pwd.getpwnam("claude").pw_uid))
        with mock.patch.dict(os.environ, expected_env, clear=True):
            report = installer.stage_disabled(
                plan,
                plan.digest,
                LocalHostCommandSheetStageBackend(expected_env),
                ActorContext(
                    uid=os.geteuid(),
                    persona=plan.persona,
                    role=plan.role,
                    instance=plan.instance,
                    capabilities=_all_plan_capabilities(plan),
                ),
                manifest=manifest,
            )
        self.assertIn(report.status, {"applied", "noop"}, report.errors)
        target = home / ".codex" / "AGENTS.md"
        target_text = target.read_text(encoding="utf-8")
        self.assertEqual(target_text, source_text)
        self.assertIn("mcp__telegram__reply", target_text)

        with mock.patch.object(bridge.subprocess, "run", side_effect=AssertionError("live Codex subprocess forbidden")):
            sheet = bridge.command_sheet("orchestrator", expected_pantheon_revision="4" * 40)
        steps = sheet["steps"]
        discovery = next(step for step in steps if step["step_id"] == "10-codex-instruction-discovery")
        gate = next(step for step in steps if step["step_id"] == "11-gate-codex-instruction-discovery")
        _assignments, compound = _assert_target_env_shape(self, discovery["command"])
        codex_argv = shlex.split(compound.split(" && ", 1)[1])
        self.assertEqual(codex_argv[:4], ["/usr/bin/timeout", "120s", "/usr/bin/codex", "exec"])
        self.assertIn("--ephemeral", codex_argv)
        self.assertEqual(codex_argv[codex_argv.index("--sandbox") + 1], "read-only")
        self.assertIn("mcp__telegram__reply", codex_argv[-1])
        self.assertIn("at most 8 bullets", codex_argv[-1])
        self.assertIn(
            "Telegram reply mapping mcp__telegram__reply",
            gate["gate"]["expected_facts"],
        )

    def test_orchestrator_stage_disabled_command_uses_controlled_target_account_env_under_restricted_login_env(self) -> None:
        root = self.root / "r8-root"
        home = root / "home" / "claude"
        repo = home / "pantheon-config"
        (home / ".npm-global" / "bin").mkdir(parents=True)
        (home / ".local" / "lib").mkdir(parents=True)
        (home / "CLAUDE.md").write_text("Orchestrator brief fixture\n", encoding="utf-8")
        for mailbox_persona in ("orchestrator", "codex"):
            for subdir in ("tmp", "new", "cur"):
                (root / "home" / "post-office" / "mailbox" / mailbox_persona / subdir).mkdir(parents=True, exist_ok=True)
        fake_claude = home / ".npm-global" / "bin" / "claude"
        fake_claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_claude.chmod(0o755)
        _seed_claude_mcp_servers(home, yaml.safe_load((REPO_ROOT / "instance.yaml").read_text(encoding="utf-8")))
        repo.symlink_to(REPO_ROOT, target_is_directory=True)

        manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        binding = manifest.codex_telegram_binding("orchestrator", root=root)
        sheet_manifest = SimpleNamespace(
            installer=manifest.installer,
            path=manifest.path,
            binding=manifest.binding,
            codex_telegram_binding=lambda persona, root=Path("/"): binding,
        )
        with mock.patch.object(bridge, "_load_manifest", return_value=sheet_manifest):
            sheet = bridge.command_sheet("orchestrator", expected_pantheon_revision="4" * 40)
        steps = sheet["steps"]
        stage_step = next(step for step in steps if step["step_id"] == "08-stage-disabled-instructions-and-bridge")
        dry_step = next(step for step in steps if step["step_id"] == "07-stage1-cutover-dry-run")
        execute_step = next(step for step in steps if step["step_id"] == "14-stage1-cutover-execute")
        self.assertIn("persona_cutover.sh", dry_step["command"])
        self.assertIn("persona_cutover.sh", execute_step["command"])

        outer = shlex.split(stage_step["command"])
        self.assertEqual(outer[:4], ["sudo", "-iu", "claude", "bash"])
        assignments, compound = _assert_target_env_shape(self, stage_step["command"])
        expected_env = dict(controlled_apply_environment(home, pwd.getpwnam("claude").pw_uid))
        self.assertEqual(assignments, expected_env)
        self.assertIn(" source-currency ", compound)
        stage_text = compound.split(" && ", 1)[1]
        stage_argv = shlex.split(stage_text)
        self.assertEqual(stage_argv[:3], ["python3", str(repo / "tools" / "t2611_recovery_persona_apply.py"), "stage-disabled"])

        manifest_arg = Path(stage_argv[stage_argv.index("--manifest") + 1])
        repo_arg = Path(stage_argv[stage_argv.index("--repo-root") + 1])
        persona_arg = stage_argv[stage_argv.index("--persona") + 1]
        harness_arg = stage_argv[stage_argv.index("--harness") + 1]
        stamp_arg = stage_argv[stage_argv.index("--stamp") + 1].replace("<operator-stamp>", "r9-test")

        installer = PersonaInstaller(repo_arg)
        plan = installer.plan(Manifest.load(manifest_arg), persona_arg, harness_override=harness_arg, root=root)
        archive = self._operation(plan, "codex-telegram.source-archive")
        source = bridge.CodexTelegramSource(
            bridge.CODEX_TELEGRAM_SOURCE_REPO,
            archive.desired["revision"],
            tuple(json.loads(archive.desired["paths"])),
            dict(json.loads(archive.desired["sha256"])),
            owner_account=archive.desired.get("source_account") or None,
            source_root=Path(archive.desired["source_root"]),
            subdir=archive.desired.get("source_subdir", ""),
        )
        # The plan's source_root is the codex-owned pantheon-config clone (T-4770);
        # the archive is built from the same pinned objects in this repo (T-4763).
        self.assertEqual(archive.desired["source_root"], "/home/codex/pantheon-config")
        bridge.create_verified_source_archive(
            REPO_ROOT,
            Path(archive.desired["archive_path"]),
            source,
        )

        restricted_path = "/usr/local/bin:/usr/bin:/bin"
        with mock.patch.dict(os.environ, {"PATH": restricted_path}, clear=True):
            self.assertIsNone(shutil.which("claude"))
            restricted_backend = LocalHostCommandSheetStageBackend(expected_env)
            restricted = restricted_backend.preflight(self._operation(plan, "codex-telegram.source-archive"))
            self.assertFalse(restricted.ok)
            self.assertIn("PATH missing from controlled target-account env", restricted.detail)
            with mock.patch.dict(os.environ, assignments, clear=True):
                self.assertEqual(shutil.which("claude"), str(fake_claude))
                backend = LocalHostCommandSheetStageBackend(expected_env)
                exit_code, summary = persona_apply.stage_disabled(
                    manifest_arg,
                    persona_arg,
                    harness_arg,
                    repo_arg,
                    stamp_arg,
                    backend=backend,
                    root=root,
                )
        self.assertEqual(exit_code, 0, summary)
        activation_ids = {
            operation.operation_id
            for operation in plan.operations[installer._activation_index(plan) :]
        }
        self.assertFalse(set(backend.events) & activation_ids)
        self.assertFalse(any(command[:3] == ("systemctl", "--user", "start") for command in backend.commands))
        self.assertFalse(any(command[:4] == ("systemctl", "--user", "enable", "--now") for command in backend.commands))
        self.assertNotIn(("systemctl", "--user", "daemon-reload"), backend.commands)
        self.assertFalse(any(command and command[0].endswith("tmux") for command in backend.commands))

    def test_orchestrator_initial_viability_command_runs_from_login_cwd_without_pythonpath(self) -> None:
        root, home, repo, expected, _source_sha, account = self._rooted_orchestrator_repo()
        expected = self._enable_repo_module_entrypoints(repo)
        self._write_initial_viability_fixture(home / ".local" / "state" / "pantheon" / "personas")
        sheet = bridge.command_sheet(
            "orchestrator",
            expected_pantheon_revision=expected,
            manifest_path=repo / "instance.yaml",
            root=root,
        )
        command = _step(sheet, "15-gate-initial-viability")["command"]
        assert isinstance(command, str)
        assignments, compound = _assert_target_env_shape(self, command)
        self.assertEqual(assignments["PATH"].split(":", 1)[0], str(home / ".npm-global" / "bin"))
        self.assertIn(f"cd {shlex.quote(str(repo))} && python3 -m tools.runtime_controller initial-viability", compound)
        self.assertNotIn("runtime_controller.py initial-viability", compound)

        outside_cwd = self.root / "outside-login-cwd"
        outside_cwd.mkdir()
        result = self._run_operator_literal(command, account=account, cwd=outside_cwd)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        receipt = _last_json_line(result.stdout)
        self.assertEqual(receipt["schema"], "pantheon-runtime-initial-viability-check/v1")
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(receipt["state"]["selected_runtime"], "codex/interactive-chatgpt")

    def test_orchestrator_initial_viability_command_red_on_direct_script_import_shape(self) -> None:
        root, home, repo, expected, _source_sha, account = self._rooted_orchestrator_repo()
        expected = self._enable_repo_module_entrypoints(repo)
        self._write_initial_viability_fixture(home / ".local" / "state" / "pantheon" / "personas")
        old_initial = shlex.join(
            (
                "python3",
                str(repo / "tools" / "runtime_controller.py"),
                "initial-viability",
                "--persona",
                "orchestrator",
                "--state-root",
                str(home / ".local" / "state" / "pantheon" / "personas"),
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
            )
        )
        old_command = bridge._gated_target_command(
            account,
            home,
            repo / "tools" / "codex_telegram_bridge.py",
            repo,
            expected,
            old_initial,
        )
        outside_cwd = self.root / "outside-direct-red"
        outside_cwd.mkdir()
        result = self._run_operator_literal(old_command, account=account, cwd=outside_cwd)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ModuleNotFoundError", result.stderr + result.stdout)
        self.assertIn("No module named 'tools'", result.stderr + result.stdout)

    def test_orchestrator_codex_ready_commands_use_repo_module_entrypoints(self) -> None:
        root, _home, repo, expected, _source_sha, account = self._rooted_orchestrator_repo()
        expected = self._enable_repo_module_entrypoints(repo)
        sheet = bridge.command_sheet(
            "orchestrator",
            expected_pantheon_revision=expected,
            manifest_path=repo / "instance.yaml",
            root=root,
        )
        expected_modules = {
            "18-codex-ready-real-cli": "tools.bringup_acceptance.harness_codex_ready",
            "19-codex-rc-ready-real-cli": "tools.bringup_acceptance.harness_codex_rc_ready",
        }
        outside_cwd = self.root / "outside-ready-cwd"
        outside_cwd.mkdir()
        for step_id, module in expected_modules.items():
            with self.subTest(step=step_id):
                command = _step(sheet, step_id)["command"]
                assert isinstance(command, str)
                _assignments, compound = _assert_target_env_shape(self, command)
                self.assertIn(f"cd {shlex.quote(str(repo))} && python3 -m {module}", compound)
                self.assertNotIn(module.rsplit(".", 1)[-1] + ".py", compound)
                help_command = bridge._gated_target_command(
                    account,
                    root / "home" / account,
                    repo / "tools" / "codex_telegram_bridge.py",
                    repo,
                    expected,
                    bridge._repo_module_command(repo, module, "--help"),
                )
                sentinel = self.root / f"{step_id}.codex-launched"
                result = self._run_operator_literal(help_command, account=account, cwd=outside_cwd)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertIn("usage:", result.stdout)
                self.assertFalse(sentinel.exists())

    def test_command_sheet_steps18_19_use_reversible_acceptance_config_guard(self) -> None:
        root, home, repo, expected, _source_sha, account = self._rooted_orchestrator_repo()
        expected = self._enable_repo_module_entrypoints(repo)
        package = repo / "tools" / "bringup_acceptance"
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text(
            "\n".join(
                (
                    "from pathlib import Path",
                    f"__path__.append(str(Path({str(REPO_ROOT / 'tools' / 'bringup_acceptance')!r})))",
                    "",
                )
            ),
            encoding="utf-8",
        )
        module_source = (
            "from __future__ import annotations\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "from tools.bringup_acceptance.codex_acceptance_config_guard import ReversibleAcceptanceConfigGuard\n"
            "from tools.harness_adapter.codex import ensure_project_trusted, ensure_update_check_disabled\n"
            "\n"
            "def main(argv=None):\n"
            "    argv = list(sys.argv[1:] if argv is None else argv)\n"
            "    work_root = Path(argv[argv.index('--work-root') + 1])\n"
            "    codex_home = Path.home() / '.codex'\n"
            "    record = Path(os.environ['R25_GUARD_RECORD'])\n"
            "    record.parent.mkdir(parents=True, exist_ok=True)\n"
            "    module_name = __spec__.name if __spec__ is not None else __name__\n"
            "    with ReversibleAcceptanceConfigGuard(codex_home=codex_home, project_cwd=work_root, owner=module_name):\n"
            "        ensure_project_trusted(codex_home, work_root)\n"
            "        ensure_update_check_disabled(codex_home)\n"
            "    record.write_text(json.dumps({'module': module_name, 'config_exists': (codex_home / 'config.toml').exists()}, sort_keys=True) + '\\n', encoding='utf-8')\n"
            "    print(json.dumps({'ok': True, 'module': module_name}, sort_keys=True))\n"
            "    return 0\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    raise SystemExit(main())\n"
        )
        for name in ("harness_codex_ready.py", "harness_codex_rc_ready.py"):
            (package / name).write_text(module_source, encoding="utf-8")
        self._git(repo, "add", "tools/bringup_acceptance/__init__.py", "tools/bringup_acceptance/harness_codex_ready.py", "tools/bringup_acceptance/harness_codex_rc_ready.py")
        self._git(repo, "commit", "-m", "acceptance guard fixture modules")
        expected = self._git_text(repo, "rev-parse", "HEAD")
        sheet = bridge.command_sheet(
            "orchestrator",
            expected_pantheon_revision=expected,
            manifest_path=repo / "instance.yaml",
            root=root,
        )
        modules = {
            "18-codex-ready-real-cli": "tools.bringup_acceptance.harness_codex_ready",
            "19-codex-rc-ready-real-cli": "tools.bringup_acceptance.harness_codex_rc_ready",
        }
        outside_cwd = self.root / "outside-guard-cwd"
        outside_cwd.mkdir()
        for step_id, module in modules.items():
            with self.subTest(step=step_id):
                command = _step(sheet, step_id)["command"]
                assert isinstance(command, str)
                self.assertIn(f"python3 -m {module}", command)
                self.assertNotIn(module.rsplit(".", 1)[-1] + ".py", command)
                record = self.root / f"{step_id}.json"
                result = self._run_operator_literal(
                    command,
                    account=account,
                    cwd=outside_cwd,
                    extra_env={"R25_GUARD_RECORD": str(record)},
                )
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                payload = json.loads(record.read_text(encoding="utf-8"))
                self.assertEqual(payload["module"], module)
                self.assertFalse(payload["config_exists"])

    def test_orchestrator_restore_proactive_swap_pipe_is_import_context_safe(self) -> None:
        root, _home, repo, expected, _source_sha, account = self._rooted_orchestrator_repo()
        expected = self._write_proactive_swap_fixture(repo)
        sheet = bridge.command_sheet(
            "orchestrator",
            expected_pantheon_revision=expected,
            manifest_path=repo / "instance.yaml",
            root=root,
        )
        command = _step(sheet, "26-gate-claude-completed")["command"]
        assert isinstance(command, str)
        _assignments, compound = _assert_target_env_shape(self, command)
        self.assertIn("set -euo pipefail", compound)
        self.assertIn("proactive_swap_init.py --persona orchestrator", compound)
        self.assertIn("| python3", compound)
        self.assertIn("--completion-gate", compound)
        self.assertNotIn("python3 -m", compound)

        outside_cwd = self.root / "outside-restore-cwd"
        outside_cwd.mkdir()
        result = self._run_operator_literal(command, account=account, cwd=outside_cwd)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        receipt = _last_json_line(result.stdout)
        self.assertEqual(receipt["schema"], "pantheon-operator-completion-gate/v1")
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(receipt["correlation_id"], "t2953-orchestrator-to-claude")
        self.assertEqual(receipt["destination"], "claude/interactive-subscription")

    def test_command_sheet_generated_python_entrypoints_are_classified_standalone_or_repo_module(self) -> None:
        sheets = [
            bridge.command_sheet("specialist", expected_pantheon_revision="3" * 40),
            bridge.command_sheet("recovery", expected_pantheon_revision="3" * 40),
            bridge.command_sheet("orchestrator", expected_pantheon_revision="4" * 40),
        ]
        commands: list[str] = []
        for sheet in sheets:
            commands.extend(step["command"] for step in sheet["steps"] if step["type"] == "command")
            commands.extend(sheet.get("source_remediation", {}).get("commands", ()))
            remediation = sheet.get("amendment_i_remediation")
            if isinstance(remediation, dict):
                commands.extend(step["command"] for step in remediation["steps"] if step["type"] == "command")

        joined = "\n".join(commands)
        self.assertNotIn("runtime_controller.py initial-viability", joined)
        self.assertNotIn("harness_codex_ready.py", joined)
        self.assertNotIn("harness_codex_rc_ready.py", joined)
        for module in (
            "tools.runtime_controller",
            "tools.bringup_acceptance.harness_codex_ready",
            "tools.bringup_acceptance.harness_codex_rc_ready",
        ):
            self.assertIn(f"python3 -m {module}", joined)
            self.assertIn(module, bridge._REPO_MODULE_ENTRYPOINTS)
        for command in commands:
            if "python3 -m " in command:
                fragments = command.split("python3 -m ")[1:]
                for fragment in fragments:
                    module = fragment.strip().split()[0].strip("'\"")
                    self.assertIn(module, bridge._REPO_MODULE_ENTRYPOINTS)
        with self.assertRaises(bridge.BridgeError):
            bridge._repo_module_command(Path("/repo"), "tools.not_a_command")
        with self.assertRaises(bridge.BridgeError):
            bridge._repo_module_command(Path("relative"), "tools.runtime_controller")

    def test_command_sheet_direct_systemctl_commands_use_target_account_env(self) -> None:
        sheets = [
            bridge.command_sheet("specialist", expected_pantheon_revision="3" * 40),
            bridge.command_sheet("recovery", expected_pantheon_revision="3" * 40),
            bridge.command_sheet("orchestrator", expected_pantheon_revision="4" * 40),
        ]
        commands: list[str] = []
        for sheet in sheets:
            commands.extend(step["command"] for step in sheet["steps"] if step["type"] == "command")
            remediation = sheet.get("amendment_i_remediation")
            if isinstance(remediation, dict):
                commands.extend(step["command"] for step in remediation["steps"] if step["type"] == "command")

        systemctl_commands = [command for command in commands if "systemctl --user" in command]
        self.assertGreaterEqual(len(systemctl_commands), 10)
        for command in systemctl_commands:
            with self.subTest(command=command):
                assignments, compound = _assert_target_env_shape(self, command)
                self.assertIn("systemctl --user", compound)
                self.assertRegex(assignments["PATH"], r"/home/[^:]+/[.]npm-global/bin:/usr/local/bin:/usr/bin:/bin")
                self.assertRegex(assignments["XDG_RUNTIME_DIR"], r"^/run/user/[0-9]+$")
                self.assertEqual(
                    assignments["DBUS_SESSION_BUS_ADDRESS"],
                    f"unix:path={assignments['XDG_RUNTIME_DIR']}/bus",
                )

    def test_command_sheet_unknown_target_account_fails_closed(self) -> None:
        binding = Manifest.load(REPO_ROOT / "instance.yaml").codex_telegram_binding("orchestrator")
        missing = dataclasses.replace(binding, account="missing-account-r9")
        manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        sheet_manifest = SimpleNamespace(
            installer=manifest.installer,
            path=manifest.path,
            binding=manifest.binding,
            codex_telegram_binding=lambda persona, root=Path("/"): missing,
        )
        with mock.patch.object(bridge, "_load_manifest", return_value=sheet_manifest), mock.patch.object(
            bridge.pwd,
            "getpwnam",
            side_effect=KeyError("missing-account-r9"),
        ):
            with self.assertRaises(bridge.BridgeError):
                bridge.command_sheet("orchestrator", expected_pantheon_revision="4" * 40)

    def test_command_sheet_stops_codex_before_specialist_pane_restage(self) -> None:
        expected = "3" * 40
        sheet = bridge.command_sheet("specialist", expected_pantheon_revision=expected)
        self.assertEqual(len(sheet["steps"]), 19)
        remediation = sheet["amendment_i_remediation"]
        self.assertEqual(remediation["effective_pane"], "claude-research-bot:0.0")
        steps = remediation["steps"]
        ids = [step["step_id"] for step in steps]
        self.assertLess(ids.index("ai-04-stop-codex"), ids.index("ai-05-prove-codex-absent"))
        self.assertLess(ids.index("ai-05-prove-codex-absent"), ids.index("ai-06-prove-claude-absent"))
        self.assertLess(ids.index("ai-06-prove-claude-absent"), ids.index("ai-07-bridge-stage-disabled"))
        self.assertLess(ids.index("ai-07-bridge-stage-disabled"), ids.index("ai-08-daemon-reload"))
        self.assertLess(ids.index("ai-08-daemon-reload"), ids.index("ai-09-start-codex"))
        self.assertLess(ids.index("ai-09-start-codex"), ids.index("ai-12-prove-startup-pane"))
        commands = "\n".join(remediation["commands"])
        self.assertIn("source-check --persona specialist --source-repo /home/codex/pantheon-config", commands)
        self.assertIn("source-currency", commands)
        self.assertIn(expected, commands)
        self.assertIn('unit=codex-telegram-poller.service', commands)
        self.assertIn('systemctl --user cat "$unit"', commands)
        self.assertIn("poller.offset", commands)
        self.assertIn("systemctl --user stop codex-telegram-poller.service", commands)
        self.assertIn("receiver-state --persona specialist --kind codex --expected absent", commands)
        self.assertIn("receiver-state --persona specialist --kind claude --expected absent", commands)
        self.assertIn("bridge-stage --persona specialist", commands)
        self.assertIn("systemctl --user start codex-telegram-poller.service", commands)
        self.assertIn("CODEX_TG_PANE=claude-research-bot:0.0", commands)
        self.assertIn("409", commands)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", json.dumps(remediation, sort_keys=True))
        self.assertIn("not Telegram delivery restored", remediation["rollback"]["receiver_process"])

    def test_source_currency_receipt_requires_exact_head_and_clean_porcelain_without_mutation(self) -> None:
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            del kwargs
            if argv[-2:] == ("rev-parse", "HEAD"):
                return subprocess.CompletedProcess(argv, 0, "f" * 40 + "\n", "")
            if argv[-1] == "--porcelain=v1":
                return subprocess.CompletedProcess(argv, 0, "", "")
            raise AssertionError(argv)

        with mock.patch.object(bridge.subprocess, "run", side_effect=fake_run):
            receipt = bridge.source_currency_receipt(Path("/repo"), expected_head="f" * 40)
        self.assertTrue(receipt["ok"])
        joined = " ".join(" ".join(call) for call in calls)
        for forbidden in (" fetch ", " pull ", " reset ", " clean "):
            self.assertNotIn(forbidden, joined)

        def dirty_run(argv, **kwargs):
            del kwargs
            if argv[-2:] == ("rev-parse", "HEAD"):
                return subprocess.CompletedProcess(argv, 0, "f" * 40 + "\n", "")
            return subprocess.CompletedProcess(argv, 0, " M tools/x.py\n", "")

        with mock.patch.object(bridge.subprocess, "run", side_effect=dirty_run):
            dirty = bridge.source_currency_receipt(Path("/repo"), expected_head="f" * 40)
        self.assertFalse(dirty["ok"])
        self.assertEqual(dirty["status_line_count"], 1)

    def test_telegram_client_rejects_redirects_oversize_non_https_wrong_host_and_bad_envelopes(self) -> None:
        token = SECRET
        redirect = urllib.error.HTTPError(
            f"https://api.telegram.org/bot{token}/getMe",
            302,
            "Found",
            {"Location": f"https://evil.test/bot{token}"},
            None,
        )

        class RaisingOpener:
            def open(self, request, timeout):
                del request, timeout
                raise redirect

        with self.assertRaises(bridge.BridgeError) as raised:
            bridge.TelegramApiClient(token, opener=RaisingOpener()).call("getMe")
        self.assertNotIn(token, str(raised.exception))
        self.assertNotIn("evil.test", str(raised.exception))
        self.assertNotIn("Location", str(raised.exception))

        class Response:
            def __init__(self, payload: bytes, url: str = "https://api.telegram.org/botx/getMe"):
                self.payload = payload
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                return self.payload[:size]

            def geturl(self):
                return self.url

        class StaticOpener:
            def __init__(self, response):
                self.response = response

            def open(self, request, timeout):
                del request, timeout
                return self.response

        oversize = b"{" + b"x" * bridge.TELEGRAM_RESPONSE_LIMIT_BYTES
        with self.assertRaisesRegex(bridge.BridgeError, "bounded"):
            bridge.TelegramApiClient(token, opener=StaticOpener(Response(oversize))).call("getMe")
        with self.assertRaisesRegex(bridge.BridgeError, "approved HTTPS host"):
            bridge.TelegramApiClient(token, opener=StaticOpener(Response(b'{"ok":true,"result":{}}', "https://evil.test/x"))).call("getMe")
        for payload in ([], {"ok": "true"}, {"ok": True, "result": []}):
            with self.subTest(payload=payload):
                with self.assertRaises(bridge.BridgeError):
                    bridge._require_telegram_envelope(payload, method="getMe", token=token)

    def test_telegram_preflight_requires_integer_non_bool_bot_id_and_bounds_username(self) -> None:
        with self.assertRaisesRegex(bridge.BridgeError, "bot id"):
            bridge._bot_identity({"id": True, "username": "bot"})
        bot_id, username = bridge._bot_identity({"id": 123, "username": "valid_bot"})
        self.assertEqual(bot_id, 123)
        self.assertEqual(username, "valid_bot")
        _bot_id, invalid = bridge._bot_identity({"id": 123, "username": "x" * 100})
        self.assertIsNone(invalid)

    def test_config_registration_idempotence_mismatch_and_rollback(self) -> None:
        binding = self.manifest.codex_telegram_binding("specialist")
        config = self.root / "config.toml"
        receipt = bridge.ensure_mcp_registration(config, binding)
        self.assertTrue(receipt.created)
        second = bridge.ensure_mcp_registration(config, binding)
        self.assertFalse(second.created)
        config.write_text("[mcp_servers.telegram]\ncommand = \"wrong\"\n", encoding="utf-8")
        with self.assertRaises(bridge.BridgeError):
            bridge.ensure_mcp_registration(config, binding)
        bridge.rollback_mcp_registration(config, binding, receipt)
        self.assertTrue(config.exists())
        config.write_text(bridge.expected_mcp_snippet(binding), encoding="utf-8")
        bridge.rollback_mcp_registration(config, binding, receipt)
        self.assertFalse(config.exists())

    def test_mcp_registration_preserves_unrelated_toml_and_rolls_back_exact_suffixes(self) -> None:
        binding = self.manifest.codex_telegram_binding("specialist")
        for suffix in ("\n", ""):
            with self.subTest(trailing_newline=bool(suffix)):
                config = self.root / f"config-{bool(suffix)}.toml"
                original = '[settings]\napi_key = "possible-secret"\n' + suffix
                config.write_text(original, encoding="utf-8")
                receipt = bridge.ensure_mcp_registration(config, binding)
                self.assertTrue(receipt.created)
                self.assertFalse(receipt.created_file)
                self.assertNotIn("possible-secret", json.dumps(bridge.dataclasses.asdict(receipt)))
                self.assertTrue(config.read_text(encoding="utf-8").startswith(original))
                bridge.rollback_mcp_registration(config, binding, receipt)
                self.assertEqual(config.read_text(encoding="utf-8"), original)

    def _mcp_registration_with_additive_tools(self, binding) -> str:
        return bridge.expected_mcp_snippet(binding) + """
[mcp_servers.telegram.tools.reply]
approval_mode = "approve"

[mcp_servers.telegram.tools.react]
approval_mode = "approve"

[mcp_servers.telegram.tools.edit_message]
approval_mode = "approve"

[mcp_servers.telegram.tools.download_attachment]
approval_mode = "approve"
"""

    def test_mcp_registration_accepts_additive_tools_policy_and_preserves_bytes(self) -> None:
        binding = self.manifest.codex_telegram_binding("specialist")
        config = self.root / "config.toml"
        original = self._mcp_registration_with_additive_tools(binding)
        config.write_text(original, encoding="utf-8")
        receipt = bridge.ensure_mcp_registration(config, binding)
        self.assertFalse(receipt.created)
        self.assertEqual(config.read_text(encoding="utf-8"), original)
        bridge.rollback_mcp_registration(config, binding, receipt)
        self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_mcp_registration_rejects_mismatched_owned_fields_and_bad_shapes(self) -> None:
        binding = self.manifest.codex_telegram_binding("specialist")
        config = self.root / "config.toml"
        additive = """
[mcp_servers.telegram.tools.reply]
approval_mode = "approve"
"""
        cases = {
            "wrong-command": bridge.expected_mcp_snippet(binding).replace('command = "python3"', 'command = "wrong"') + additive,
            "missing-command": bridge.expected_mcp_snippet(binding).replace('command = "python3"\n', "") + additive,
            "wrong-args": bridge.expected_mcp_snippet(binding).replace('"server"', '"poller"') + additive,
            "missing-args": bridge.expected_mcp_snippet(binding).split("args = ", 1)[0] + additive,
            "non-table-mcp-servers": 'mcp_servers = "not-table"\n',
            "non-table-named-server": '[mcp_servers]\ntelegram = "not-table"\n',
            "malformed": "[mcp_servers.telegram\n",
        }
        for name, content in cases.items():
            with self.subTest(name=name):
                config.write_text(content, encoding="utf-8")
                with self.assertRaises(bridge.BridgeError):
                    bridge.ensure_mcp_registration(config, binding)

    def test_mcp_registration_accepts_exact_table_and_rejects_mismatch_without_comment_false_positive(self) -> None:
        binding = self.manifest.codex_telegram_binding("specialist")
        config = self.root / "config.toml"
        original = '# [mcp_servers.telegram]\n[settings]\nnote = "[mcp_servers.telegram]"\n'
        config.write_text(original, encoding="utf-8")
        receipt = bridge.ensure_mcp_registration(config, binding)
        self.assertTrue(receipt.created)
        self.assertIn("[settings]", config.read_text(encoding="utf-8"))
        second = bridge.ensure_mcp_registration(config, binding)
        self.assertFalse(second.created)
        config.write_text('[mcp_servers.telegram]\ncommand = "wrong"\n', encoding="utf-8")
        with self.assertRaises(bridge.BridgeError):
            bridge.ensure_mcp_registration(config, binding)

    def test_expected_mcp_snippet_requires_exact_telegram_tool_approvals(self) -> None:
        live = Manifest.load(REPO_ROOT / "instance.yaml")
        orchestrator = live.codex_telegram_binding("orchestrator")
        specialist = live.codex_telegram_binding("specialist")
        recovery = live.codex_telegram_binding("recovery")

        orchestrator_text = bridge.expected_mcp_snippet(orchestrator)
        self.assertTrue(orchestrator.unattended_telegram_tools)
        for tool in ("reply", "react", "edit_message", "download_attachment"):
            self.assertIn(f"[mcp_servers.telegram.tools.{tool}]", orchestrator_text)
            self.assertIn('approval_mode = "approve"', orchestrator_text)
        self.assertFalse(specialist.unattended_telegram_tools)
        self.assertFalse(recovery.unattended_telegram_tools)
        self.assertEqual(bridge.expected_mcp_snippet(specialist), bridge.expected_mcp_snippet(specialist).split("\n\n", 1)[0])
        self.assertNotIn("[mcp_servers.telegram.tools.", bridge.expected_mcp_snippet(specialist))
        self.assertNotIn("[mcp_servers.telegram.tools.", bridge.expected_mcp_snippet(recovery))

    def test_specialist_no_permission_policy_ref_preserves_danger_full_access_never_and_emitted_bytes(self) -> None:
        live = Manifest.load(REPO_ROOT / "instance.yaml")
        specialist = live.codex_telegram_binding("specialist")
        recovery = live.codex_telegram_binding("recovery")
        expected_specialist = (
            "[mcp_servers.telegram]\n"
            "command = \"python3\"\n"
            "args = [\"/home/claude-research/pantheon-config/tools/codex_telegram_bridge.py\", \"exec\", \"--mode\", \"server\"]\n"
        )
        expected_recovery = (
            "[mcp_servers.telegram]\n"
            "command = \"python3\"\n"
            "args = [\"/home/claude-fallback/pantheon-config/tools/codex_telegram_bridge.py\", \"exec\", \"--mode\", \"server\"]\n"
        )
        self.assertEqual(bridge.expected_mcp_snippet(specialist), expected_specialist)
        self.assertEqual(bridge.expected_mcp_snippet(recovery), expected_recovery)

        specialist_launch = persona_bringup.load_binding(
            REPO_ROOT / "instance.yaml",
            "specialist",
            harness_override="codex",
        ).launch
        # T-4025 (the operator TG 7477, 2026-09-26): specialist (an HC codex fallback) now runs
        # danger-full-access after the T-2654 UID-cordon tightening; was workspace-write.
        self.assertEqual(specialist_launch.native["sandbox"], "danger-full-access")
        self.assertEqual(specialist_launch.native["config_overrides"]["approval_policy"], "never")
        self.assertNotIn("permission_posture", specialist_launch.native)

    def test_mcp_registration_rejects_missing_wrong_duplicate_or_broad_tool_policy(self) -> None:
        binding = Manifest.load(REPO_ROOT / "instance.yaml").codex_telegram_binding("orchestrator")
        config = self.root / "config.toml"
        good = bridge.expected_mcp_snippet(binding)
        cases = {
            "missing-tool": good.replace(
                '[mcp_servers.telegram.tools.react]\napproval_mode = "approve"\n\n',
                "",
            ),
            "wrong-mode": good.replace(
                '[mcp_servers.telegram.tools.reply]\napproval_mode = "approve"',
                '[mcp_servers.telegram.tools.reply]\napproval_mode = "prompt"',
            ),
            "broad-default": good.replace(
                'args = ["/home/claude/pantheon-config/tools/codex_telegram_bridge.py", "exec", "--mode", "server"]',
                'args = ["/home/claude/pantheon-config/tools/codex_telegram_bridge.py", "exec", "--mode", "server"]\ndefault_tools_approval_mode = "approve"',
            ),
            "undeclared-tool": good + '[mcp_servers.telegram.tools.delete_message]\napproval_mode = "approve"\n',
            "duplicate": good + '[mcp_servers.telegram.tools.reply]\napproval_mode = "approve"\n',
        }
        for name, content in cases.items():
            with self.subTest(name=name):
                config.write_text(content, encoding="utf-8")
                with self.assertRaises(bridge.BridgeError):
                    bridge.ensure_mcp_registration(config, binding)

    def test_receiver_process_identity_excludes_self_and_requires_exact_claude_plugin(self) -> None:
        binding = self.manifest.codex_telegram_binding("recovery")
        backend = bridge.LocalReceiverBackend()
        self.assertFalse(
            backend._process_matches(
                binding,
                "123 python3 /home/claude-fallback/pantheon-config/tools/codex_telegram_bridge.py exec --mode poller",
                kind="claude",
            )
        )
        self.assertFalse(
            backend._process_matches(
                binding,
                "123 /home/other/.npm-global/bin/claude --channels plugin:telegram@claude-plugins-official",
                kind="claude",
            )
        )
        self.assertTrue(
            backend._process_matches(
                binding,
                "123 /home/claude-fallback/.npm-global/bin/claude --channels plugin:telegram@claude-plugins-official",
                kind="claude",
            )
        )
        self.assertTrue(
            backend._process_matches(
                binding,
                "124 /home/claude-fallback/.bun/bin/bun /home/claude-fallback/.claude/plugins/claude-plugins-official/telegram/server.ts",
                kind="claude",
            )
        )

    def test_runtime_owned_present_requires_exact_claude_argv_and_plugin_descendant(self) -> None:
        binding = self.manifest.codex_telegram_binding("specialist")
        evidence = bridge._evaluate_runtime_owned_claude_state(
            binding,
            expected="present",
            unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "inactive", "dead", "0"),
            unit_enabled="disabled",
            tmux=bridge.TmuxPaneSnapshot(True, False, 1400118, "claude"),
            processes=self._runtime_owned_processes(binding),
        )
        self.assertEqual(evidence["topology"], "runtime-owned")
        self.assertEqual(evidence["claude_receiver_process_count"], "1")
        self.assertEqual(evidence["plugin_launcher_descendant_count"], "1")
        self.assertEqual(evidence["plugin_server_descendant_count"], "1")

    def test_live_recovery_binding_carries_its_configured_claude_executable(self) -> None:
        # T-5038: recovery launches the root-owned /opt copy; every other role keeps npm.
        live = Manifest.load(Path(__file__).resolve().parent.parent / "instance.yaml")
        recovery = live.codex_telegram_binding("recovery")
        self.assertEqual(recovery.claude_executable, Path("/opt/pantheon/claude/claude-fallback/current/claude.exe"))
        self.assertEqual(recovery.claude_pane_commands, frozenset({"claude", "claude.exe"}))
        self.assertIsNone(live.codex_telegram_binding("specialist").claude_executable)

    def test_runtime_owned_present_accepts_the_configured_executable(self) -> None:
        exe = Path("/opt/pantheon/claude/claude-fallback/current/claude.exe")
        binding = dataclasses.replace(self.manifest.codex_telegram_binding("specialist"), claude_executable=exe)
        processes = (
            bridge.ProcessSnapshot(1400118, 1, f"{exe} --channels plugin:telegram@claude-plugins-official --model claude-opus-5-5"),
        ) + self._runtime_owned_processes(binding)[1:]
        evidence = bridge._evaluate_runtime_owned_claude_state(
            binding,
            expected="present",
            unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "inactive", "dead", "0"),
            unit_enabled="disabled",
            tmux=bridge.TmuxPaneSnapshot(True, False, 1400118, "claude.exe"),
            processes=processes,
        )
        self.assertEqual(evidence["claude_receiver_process_count"], "1")
        # The same /opt receiver is still present, so an absent proof must refuse.
        with self.assertRaises(bridge.BridgeError):
            bridge._evaluate_runtime_owned_claude_state(
                binding,
                expected="absent",
                unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "inactive", "dead", "0"),
                unit_enabled="disabled",
                tmux=bridge.TmuxPaneSnapshot(True, False, 1400118, "claude.exe"),
                processes=processes,
            )
        # A role WITHOUT a configured executable does not accept the /opt argv.
        npm_only = dataclasses.replace(binding, claude_executable=None)
        with self.assertRaises(bridge.BridgeError):
            bridge._evaluate_runtime_owned_claude_state(
                npm_only,
                expected="present",
                unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "inactive", "dead", "0"),
                unit_enabled="disabled",
                tmux=bridge.TmuxPaneSnapshot(True, False, 1400118, "claude.exe"),
                processes=processes,
            )

    def test_manifest_claude_executable_must_be_an_absolute_literal(self) -> None:
        for bad in ("claude.exe", "{account_home}/.npm-global/bin/claude", 7):
            with self.subTest(bad=bad):
                manifest = dataclasses.make_dataclass("M", [("harness_runtime", dict)])(
                    {"roles": {"r": {"claude": {"executable": bad}}}}
                )
                with self.assertRaises(bridge.BridgeError):
                    bridge._manifest_role_claude_executable(manifest, role="r")

    def test_runtime_owned_present_rejects_substring_wrappers_or_duplicate_receivers(self) -> None:
        binding = self.manifest.codex_telegram_binding("specialist")
        wrapped = (
            bridge.ProcessSnapshot(
                1400118,
                1,
                f"python3 /tmp/diagnostic.py {binding.account_home}/.npm-global/bin/claude --channels plugin:telegram@claude-plugins-official",
            ),
            bridge.ProcessSnapshot(
                1400120,
                1400118,
                f"{binding.account_home}/.bun/bin/bun run --cwd {binding.account_home}/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6 --shell=bun --silent start",
            ),
        )
        with self.assertRaises(bridge.BridgeError):
            bridge._evaluate_runtime_owned_claude_state(
                binding,
                expected="present",
                unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "inactive", "dead", "0"),
                unit_enabled="disabled",
                tmux=bridge.TmuxPaneSnapshot(True, False, 1400118, "claude"),
                processes=wrapped,
            )

        duplicate = self._runtime_owned_processes(binding) + (
            bridge.ProcessSnapshot(
                1400200,
                1,
                f"{binding.account_home}/.npm-global/bin/claude --channels plugin:telegram@claude-plugins-official --model claude-opus-4-8",
            ),
        )
        with self.assertRaises(bridge.BridgeError):
            bridge._evaluate_runtime_owned_claude_state(
                binding,
                expected="present",
                unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "inactive", "dead", "0"),
                unit_enabled="disabled",
                tmux=bridge.TmuxPaneSnapshot(True, False, 1400118, "claude"),
                processes=duplicate,
            )

        duplicate_server = self._runtime_owned_processes(binding) + (
            bridge.ProcessSnapshot(1400122, 1400120, "bun server.ts"),
        )
        with self.assertRaises(bridge.BridgeError):
            bridge._evaluate_runtime_owned_claude_state(
                binding,
                expected="present",
                unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "inactive", "dead", "0"),
                unit_enabled="disabled",
                tmux=bridge.TmuxPaneSnapshot(True, False, 1400118, "claude"),
                processes=duplicate_server,
            )

        extra_orphan_server = self._runtime_owned_processes(binding) + (
            bridge.ProcessSnapshot(
                1400300,
                1,
                "bun server.ts",
                cwd=f"{binding.account_home}/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6",
            ),
        )
        with self.assertRaises(bridge.BridgeError):
            bridge._evaluate_runtime_owned_claude_state(
                binding,
                expected="present",
                unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "inactive", "dead", "0"),
                unit_enabled="disabled",
                tmux=bridge.TmuxPaneSnapshot(True, False, 1400118, "claude"),
                processes=extra_orphan_server,
            )

    def test_runtime_owned_absent_allows_codex_in_same_tmux_but_rejects_orphan_plugin(self) -> None:
        binding = self.manifest.codex_telegram_binding("specialist")
        evidence = bridge._evaluate_runtime_owned_claude_state(
            binding,
            expected="absent",
            unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "inactive", "dead", "0"),
            unit_enabled="disabled",
            tmux=bridge.TmuxPaneSnapshot(True, False, 1400118, "codex"),
            processes=(
                bridge.ProcessSnapshot(
                    1400118,
                    1,
                    "/usr/bin/codex",
                ),
            ),
        )
        self.assertEqual(evidence["state"], "absent")

        with self.assertRaises(bridge.BridgeError):
            bridge._evaluate_runtime_owned_claude_state(
                binding,
                expected="absent",
                unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "inactive", "dead", "0"),
                unit_enabled="disabled",
                tmux=bridge.TmuxPaneSnapshot(True, False, 1400118, "codex"),
                processes=(
                    bridge.ProcessSnapshot(
                        1400200,
                        1,
                        f"{binding.account_home}/.bun/bin/bun run --cwd {binding.account_home}/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6 --shell=bun --silent start",
                    ),
                ),
            )

        with self.assertRaises(bridge.BridgeError):
            bridge._evaluate_runtime_owned_claude_state(
                binding,
                expected="absent",
                unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "inactive", "dead", "0"),
                unit_enabled="disabled",
                tmux=bridge.TmuxPaneSnapshot(True, False, 1400118, "codex"),
                processes=(
                    bridge.ProcessSnapshot(
                        1400300,
                        1,
                        "bun server.ts",
                        cwd=f"{binding.account_home}/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6",
                    ),
                ),
            )

        wrong_path = bridge._evaluate_runtime_owned_claude_state(
            binding,
            expected="absent",
            unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "inactive", "dead", "0"),
            unit_enabled="disabled",
            tmux=bridge.TmuxPaneSnapshot(True, False, 1400118, "codex"),
            processes=(
                bridge.ProcessSnapshot(1400300, 1, "python wrapper server.ts", cwd="/tmp/not-telegram"),
                bridge.ProcessSnapshot(
                    1400301,
                    1,
                    "bun server.ts",
                    cwd="/home/other/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6",
                ),
            ),
        )
        self.assertEqual(wrong_path["state"], "absent")

    def test_systemd_topology_ignores_unrelated_tmux_and_rejects_matching_second_supervisor(self) -> None:
        binding = bridge.dataclasses.replace(self.manifest.codex_telegram_binding("specialist"), claude_receiver_topology="systemd-user")
        unit_cgroup_v2 = f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{binding.claude_receiver_unit}\n"
        unit_cgroup_v1 = f"9:cpu,cpuacct:/user.slice/user-1000.slice/user@1000.service/app.slice/{binding.claude_receiver_unit}\n"
        healthy_processes = (
            bridge.ProcessSnapshot(900, 1, "systemd supervisor"),
            bridge.ProcessSnapshot(905, 1, "tmux server"),
            bridge.ProcessSnapshot(910, 905, f"{binding.account_home}/.npm-global/bin/claude --channels plugin:telegram@claude-plugins-official"),
            bridge.ProcessSnapshot(
                920,
                910,
                f"{binding.account_home}/.bun/bin/bun run --cwd {binding.account_home}/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6 --shell=bun --silent start",
            ),
            bridge.ProcessSnapshot(
                921,
                920,
                "bun server.ts",
                cwd=f"{binding.account_home}/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6",
            ),
        )
        evidence = bridge._evaluate_systemd_user_claude_state(
            binding,
            expected="present",
            unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "active", "running", "900"),
            tmux=bridge.TmuxPaneSnapshot(True, False, 910, "claude", 905),
            processes=healthy_processes,
            unit_main_cgroup=unit_cgroup_v2,
            tmux_server_cgroup=unit_cgroup_v2,
        )
        self.assertEqual(evidence["state"], "present")
        self.assertEqual(evidence["plugin_launcher_descendant_count"], "1")
        v1_success = bridge._evaluate_systemd_user_claude_state(
            binding,
            expected="present",
            unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "active", "running", "900"),
            tmux=bridge.TmuxPaneSnapshot(True, False, 910, "claude", 905),
            processes=healthy_processes,
            unit_main_cgroup=unit_cgroup_v1,
            tmux_server_cgroup=unit_cgroup_v1,
        )
        self.assertEqual(v1_success["tmux_server_cgroup_match"], "true")
        with self.assertRaises(bridge.BridgeError):
            bridge._evaluate_systemd_user_claude_state(
                binding,
                expected="present",
                unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "active", "running", "900"),
                tmux=bridge.TmuxPaneSnapshot(False, True, 0, ""),
                processes=(),
                unit_main_cgroup=unit_cgroup_v2,
                tmux_server_cgroup=unit_cgroup_v2,
            )
        with self.assertRaises(bridge.BridgeError):
            bridge._evaluate_systemd_user_claude_state(
                binding,
                expected="present",
                unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "active", "running", "900"),
                tmux=bridge.TmuxPaneSnapshot(True, False, 1400118, "codex", 905),
                processes=healthy_processes,
                unit_main_cgroup=unit_cgroup_v2,
                tmux_server_cgroup=unit_cgroup_v2,
            )
        with self.assertRaises(bridge.BridgeError):
            bridge._evaluate_systemd_user_claude_state(
                binding,
                expected="present",
                unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "active", "running", "900"),
                tmux=bridge.TmuxPaneSnapshot(True, False, 1400118, "claude", 905),
                processes=(
                    bridge.ProcessSnapshot(900, 1, f"{binding.account_home}/.npm-global/bin/claude --channels plugin:telegram@claude-plugins-official"),
                    bridge.ProcessSnapshot(1400118, 1, f"{binding.account_home}/.npm-global/bin/claude --channels plugin:telegram@claude-plugins-official"),
                ),
                unit_main_cgroup=unit_cgroup_v2,
                tmux_server_cgroup=unit_cgroup_v2,
            )
        with self.assertRaisesRegex(bridge.BridgeError, "ambiguous tmux second supervisor"):
            bridge._evaluate_systemd_user_claude_state(
                binding,
                expected="present",
                unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "active", "running", "900"),
                tmux=bridge.TmuxPaneSnapshot(True, False, 910, "claude", 905),
                processes=healthy_processes
                + (
                    bridge.ProcessSnapshot(911, 905, f"{binding.account_home}/.npm-global/bin/claude --channels plugin:telegram@claude-plugins-official"),
                ),
                unit_main_cgroup=unit_cgroup_v2,
                tmux_server_cgroup=unit_cgroup_v2,
            )
        with self.assertRaisesRegex(bridge.BridgeError, "not anchored"):
            bridge._evaluate_systemd_user_claude_state(
                binding,
                expected="present",
                unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "active", "running", "900"),
                tmux=bridge.TmuxPaneSnapshot(True, False, 910, "claude", 905),
                processes=(
                    bridge.ProcessSnapshot(900, 1, "systemd supervisor"),
                    bridge.ProcessSnapshot(910, 1, f"{binding.account_home}/.npm-global/bin/claude --channels plugin:telegram@claude-plugins-official"),
                    healthy_processes[3],
                    healthy_processes[4],
                ),
                unit_main_cgroup=unit_cgroup_v2,
                tmux_server_cgroup=unit_cgroup_v2,
            )
        with self.assertRaisesRegex(bridge.BridgeError, "ambiguous tmux second supervisor"):
            bridge._evaluate_systemd_user_claude_state(
                binding,
                expected="present",
                unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "active", "running", "900"),
                tmux=bridge.TmuxPaneSnapshot(True, False, 905, "claude", 905),
                processes=healthy_processes,
                unit_main_cgroup=unit_cgroup_v2,
                tmux_server_cgroup=unit_cgroup_v2,
            )

    def test_systemd_absent_rejects_exact_plugin_launcher_and_orphan_server_remnants(self) -> None:
        binding = bridge.dataclasses.replace(self.manifest.codex_telegram_binding("specialist"), claude_receiver_topology="systemd-user")
        for process in (
            bridge.ProcessSnapshot(
                1400200,
                1,
                f"{binding.account_home}/.bun/bin/bun run --cwd {binding.account_home}/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6 --shell=bun --silent start",
            ),
            bridge.ProcessSnapshot(
                1400300,
                1,
                "bun server.ts",
                cwd=f"{binding.account_home}/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6",
            ),
        ):
            with self.subTest(process=process.args):
                with self.assertRaises(bridge.BridgeError):
                    bridge._evaluate_systemd_user_claude_state(
                        binding,
                        expected="absent",
                        unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "inactive", "dead", "0"),
                        tmux=bridge.TmuxPaneSnapshot(False, True, 0, ""),
                        processes=(process,),
                    )

        evidence = bridge._evaluate_systemd_user_claude_state(
            binding,
            expected="absent",
            unit_probe=bridge.UnitProbe(binding.claude_receiver_unit, "inactive", "dead", "0"),
            tmux=bridge.TmuxPaneSnapshot(False, True, 0, ""),
            processes=(bridge.ProcessSnapshot(1400300, 1, "bun server.ts", cwd="/tmp/not-telegram"),),
        )
        self.assertEqual(evidence["state"], "absent")

    def test_runtime_owned_handoff_actions_do_not_respawn_or_start_dormant_unit(self) -> None:
        binding = self.manifest.codex_telegram_binding("recovery")
        for direction in ("claude-to-codex", "rollback-to-codex"):
            actions = bridge.handoff_actions(binding, direction, "pre")
            self.assertNotIn("probe-claude-absent", actions)
            self.assertNotIn("respawn-pane-to-codex", actions)
            self.assertIn("probe-claude-present", actions)
        post = bridge.handoff_actions(binding, "codex-to-claude", "post")
        self.assertNotIn("enable-claude-receiver", post)
        self.assertNotIn("start-claude-receiver", post)
        self.assertNotIn("respawn-pane-to-claude", post)
        self.assertIn("probe-claude-present", post)
        self.assertIn("restore-watchdog-units", post)

    def test_runtime_owned_receiver_state_waits_for_transient_convergence_and_times_out(self) -> None:
        binding = self.manifest.codex_telegram_binding("specialist")

        class SequencedBackend(bridge.LocalReceiverBackend):
            def __init__(self, outcomes):
                super().__init__(timeout_seconds=0.05)
                self.outcomes = list(outcomes)
                self.calls = 0

            def _claude_state_once(self, binding, expected, *, topology_phase="managed"):
                del binding, expected, topology_phase
                self.calls += 1
                outcome = self.outcomes.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

        backend = SequencedBackend(
            (
                bridge.BridgeError("runtime-owned Claude receiver still present"),
                {"state": "absent"},
            )
        )
        backend._require_absent(binding, kind="claude")
        self.assertEqual(backend.calls, 2)

        timeout = SequencedBackend((bridge.BridgeError("runtime-owned Claude receiver still present"),) * 20)
        with self.assertRaisesRegex(bridge.BridgeError, "timed out"):
            timeout._require_absent(binding, kind="claude")
        self.assertGreater(timeout.calls, 1)

        hard = SequencedBackend((bridge.BridgeError("runtime-owned Claude receiver has active/enabled dormant systemd supervisor"),))
        with self.assertRaisesRegex(bridge.BridgeError, "active/enabled"):
            hard._require_present(binding, kind="claude")
        self.assertEqual(hard.calls, 1)

        present = SequencedBackend(
            (
                bridge.BridgeError("runtime-owned Claude receiver process is not present"),
                {"state": "present"},
            )
        )
        present._require_present(binding, kind="claude")
        self.assertEqual(present.calls, 2)

    def test_watchdog_snapshot_restore_preserves_exact_enabled_and_active_states(self) -> None:
        binding = self.manifest.codex_telegram_binding("recovery", root=self.root)

        class SnapshotBackend(bridge.LocalReceiverBackend):
            def __init__(self):
                super().__init__()
                self.systemctl_calls = []

            def _require_current_account(self, binding):
                del binding

            def _snapshot_unit(self, unit):
                if unit.endswith(".timer"):
                    return {"active_state": "active", "sub_state": "running", "main_pid": "88", "enabled": "enabled"}
                return {"active_state": "inactive", "sub_state": "dead", "main_pid": "0", "enabled": "disabled"}

            def _systemctl(self, *args):
                self.systemctl_calls.append(args)
                return SimpleNamespace(returncode=0, stdout="", stderr="")

        backend = SnapshotBackend()
        backend.execute("snapshot-watchdog-units", binding)
        backend.execute("restore-watchdog-units", binding)
        self.assertIn(("enable", "recovery-telegram-child-watchdog.timer"), backend.systemctl_calls)
        self.assertIn(("start", "recovery-telegram-child-watchdog.timer"), backend.systemctl_calls)
        self.assertIn(("disable", "recovery-telegram-child-watchdog.service"), backend.systemctl_calls)
        self.assertIn(("stop", "recovery-telegram-child-watchdog.service"), backend.systemctl_calls)

    def test_source_unit_snapshot_retry_preserves_original_until_complete(self) -> None:
        binding = bridge.dataclasses.replace(self.manifest.codex_telegram_binding("recovery", root=self.root), claude_receiver_topology="systemd-user")

        class RetryBackend(bridge.LocalReceiverBackend):
            def __init__(self):
                super().__init__()
                self.snapshots = [
                    {"active_state": "active", "sub_state": "running", "main_pid": "77", "enabled": "enabled"},
                    {"active_state": "inactive", "sub_state": "dead", "main_pid": "0", "enabled": "disabled"},
                ]

            def _snapshot_unit(self, unit):
                del unit
                return self.snapshots.pop(0)

        backend = RetryBackend()
        backend._snapshot_units(binding, units=(binding.claude_receiver_unit,))
        backend._snapshot_units(binding, units=(binding.claude_receiver_unit,))
        payload = backend._read_topology_snapshot(binding)
        original = payload["units"][binding.claude_receiver_unit]
        self.assertFalse(payload["complete"])
        self.assertEqual(original["active_state"], "active")
        self.assertEqual(original["enabled"], "enabled")
        backend._complete_topology_snapshot(binding)
        backend._snapshot_units(binding, units=(binding.claude_receiver_unit,))
        completed = backend._read_topology_snapshot(binding)
        self.assertFalse(completed["complete"])
        self.assertEqual(completed["units"][binding.claude_receiver_unit]["active_state"], "inactive")

    def test_topology_snapshot_identity_mismatch_fails_closed(self) -> None:
        binding = self.manifest.codex_telegram_binding("recovery", root=self.root)
        snapshot = binding.state_dir / "receipts" / "receiver-topology-snapshot.json"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text(
            json.dumps(
                {
                    "schema": bridge.TOPOLOGY_SNAPSHOT_SCHEMA,
                    "persona": "specialist",
                    "account": binding.account,
                    "claude_receiver_topology": binding.claude_receiver_topology,
                    "complete": False,
                    "units": {},
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(bridge.BridgeError):
            bridge.LocalReceiverBackend()._read_topology_snapshot(binding)

    def test_systemd_cli_handoff_failure_restores_source_unit_and_watchdog(self) -> None:
        binding = bridge.dataclasses.replace(self.manifest.codex_telegram_binding("recovery"), claude_receiver_topology="systemd-user")

        class CliBackend:
            def __init__(self, *, timeout_seconds):
                del timeout_seconds
                self.events = []

            def execute(self, action, binding):
                del binding
                if action == "probe-claude-absent":
                    raise bridge.BridgeError("still present")
                self.events.append(action)

        backend = CliBackend(timeout_seconds=1)
        with mock.patch.object(bridge, "binding_for_current_account", return_value=binding), mock.patch.object(
            bridge,
            "LocalReceiverBackend",
            return_value=backend,
        ), mock.patch("builtins.print"):
            code = bridge.main(
                [
                    "handoff",
                    "--persona",
                    "recovery",
                    "--direction",
                    "claude-to-codex",
                    "--phase",
                    "pre",
                    "--reason",
                    "test",
                    "--json",
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("restore-claude-receiver", backend.events)
        self.assertIn("restore-watchdog-units", backend.events)
        self.assertIn("complete-receiver-topology-snapshot", backend.events)

    def test_installer_desired_and_replay_preserve_topology_and_tmux_pane(self) -> None:
        installer = PersonaInstaller(Path(__file__).resolve().parent.parent)
        plan = installer.plan(self.manifest, "recovery", harness_override="codex", root=self.root / "target")
        operation = self._operation(plan, "codex-telegram.source-archive")
        self.assertEqual(operation.desired["claude_receiver_topology"], "runtime-owned")
        self.assertEqual(operation.desired["predecessor_claude_receiver_topology"], "runtime-owned")
        self.assertEqual(operation.desired["tmux_pane"], "0.0")
        self.assertEqual(operation.desired["effective_pane"], "claude-telegram-bot-fallback:0.0")
        replayed = __import__("tools.persona_installer", fromlist=["_bridge_binding_from_operation"])._bridge_binding_from_operation(operation)
        self.assertEqual(replayed.claude_receiver_topology, "runtime-owned")
        self.assertEqual(replayed.predecessor_claude_receiver_topology, "runtime-owned")
        self.assertEqual(replayed.tmux_pane, "0.0")
        self.assertEqual(replayed.effective_pane, "claude-telegram-bot-fallback:0.0")
        bad_operation = bridge.dataclasses.replace(operation, desired={**operation.desired, "effective_pane": "wrong-session:0.0"})
        with self.assertRaisesRegex(Exception, "effective pane"):
            __import__("tools.persona_installer", fromlist=["_bridge_binding_from_operation"])._bridge_binding_from_operation(bad_operation)
        installer_module = __import__("tools.persona_installer", fromlist=["ActorContext", "_all_plan_capabilities"])
        actor = installer_module.ActorContext(
            uid=os.geteuid(),
            persona="recovery",
            role=plan.role,
            instance=plan.instance,
            capabilities=installer_module._all_plan_capabilities(plan),
        )
        report = installer.apply(plan, plan.digest, SimulatedHostBackend(), actor, manifest=self.manifest)
        self.assertEqual(report.status, "applied")
        receipt_evidence = next(item.evidence for item in report.receipt.evidence if item.operation_id == "codex-telegram.source-archive")
        self.assertEqual(receipt_evidence["claude_receiver_topology"], "runtime-owned")
        self.assertEqual(receipt_evidence["predecessor_claude_receiver_topology"], "runtime-owned")
        self.assertEqual(receipt_evidence["tmux_pane"], "0.0")
        self.assertEqual(receipt_evidence["effective_pane"], "claude-telegram-bot-fallback:0.0")

    def test_stage_extracts_the_verified_bytes_not_a_swapped_archive(self) -> None:
        # T-4860: another account swaps the (predictable /tmp) archive between
        # verification and extraction. Staging must promote what was verified.
        binding = self.manifest.codex_telegram_binding("specialist", root=self.root)
        archive = self._canonical_archive()
        evil = self._archive({path: b"evil" for path in bridge.CODEX_TELEGRAM_SOURCE_PATHS})
        real_verify = bridge.verify_archive

        def verify_then_swap(source_archive, source):
            receipt = real_verify(source_archive, source)
            os.replace(evil, archive)  # the swap lands after verification
            return receipt

        with mock.patch.object(bridge, "verify_archive", side_effect=verify_then_swap):
            bridge.stage_archive_release(archive, binding)
        release_dir = binding.release_root / "releases" / binding.source.revision
        self.assertTrue(bridge._release_tree_matches(release_dir, binding.source))
        with self.assertRaises(bridge.BridgeError):  # the swap really happened on disk
            real_verify(archive, binding.source)
        for path in bridge.CODEX_TELEGRAM_SOURCE_PATHS:
            self.assertNotEqual((release_dir / path).read_bytes(), b"evil")

    def test_bridge_stage_failure_restores_owned_allowlist_unit_current_mcp_and_skips_receipt(self) -> None:
        binding = self.manifest.codex_telegram_binding("specialist", root=self.root)
        archive = self._archive({path: bridge.CODEX_TELEGRAM_SOURCE_SHA256[path].encode("ascii") for path in bridge.CODEX_TELEGRAM_SOURCE_PATHS})
        original_allow = binding.state_dir / "allowlist.json"
        original_unit = binding.account_home / ".config" / "systemd" / "user" / binding.codex_receiver_unit
        original_mcp = binding.account_home / ".codex" / "config.toml"
        original_allow.parent.mkdir(parents=True)
        original_allow.write_text('{"allow":[1]}\n', encoding="utf-8")
        original_unit.parent.mkdir(parents=True)
        original_unit.write_text("old-unit", encoding="utf-8")
        original_mcp.parent.mkdir(parents=True)
        original_mcp.write_text("[settings]\nkeep = true", encoding="utf-8")
        release_dir = binding.release_root / "releases" / "old"
        release_dir.mkdir(parents=True)
        binding.current_release.symlink_to(release_dir)
        with mock.patch.object(bridge, "verify_archive", return_value=bridge.SourceHashReceipt("s", binding.source.revision, binding.source.sha256, "m")), \
             mock.patch("tools.codex_telegram_bridge.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="reload failed")):
            with self.assertRaises(bridge.BridgeError):
                bridge.bridge_stage(archive, binding)
        self.assertEqual(original_allow.read_text(encoding="utf-8"), '{"allow":[1]}\n')
        self.assertEqual(original_unit.read_text(encoding="utf-8"), "old-unit")
        self.assertEqual(original_mcp.read_text(encoding="utf-8"), "[settings]\nkeep = true")
        self.assertEqual(os.readlink(binding.current_release), str(release_dir))
        self.assertFalse((binding.state_dir / "receipts" / f"stage-{binding.source.revision}.json").exists())

    def test_stage_fault_boundaries_restore_exact_prior_state_without_receipt(self) -> None:
        fault_points = [
            "release-promote",
            "release-allowlist",
            "release-current",
            "bridge-allowlist",
            "bridge-unit",
            "bridge-daemon-reload",
            "bridge-mcp",
            "bridge-disabled-first",
                ]
        for fault_after in fault_points:
            with self.subTest(fault_after=fault_after):
                binding = self.manifest.codex_telegram_binding("specialist", root=self.root / fault_after)
                archive = self._canonical_archive()
                allowlist = binding.state_dir / "allowlist.json"
                unit = binding.account_home / ".config" / "systemd" / "user" / binding.codex_receiver_unit
                mcp = binding.account_home / ".codex" / "config.toml"
                allowlist.parent.mkdir(parents=True)
                allowlist.write_text('{"allow":[1]}\n', encoding="utf-8")
                unit.parent.mkdir(parents=True)
                unit.write_text("old-unit", encoding="utf-8")
                mcp.parent.mkdir(parents=True)
                mcp.write_text("[settings]\nkeep = true", encoding="utf-8")
                release_dir = binding.release_root / "releases" / "old"
                release_dir.mkdir(parents=True)
                binding.current_release.symlink_to(release_dir)
                before = {
                    "allowlist": allowlist.read_text(encoding="utf-8"),
                    "unit": unit.read_text(encoding="utf-8"),
                    "mcp": mcp.read_text(encoding="utf-8"),
                    "current": os.readlink(binding.current_release),
                }

                def fake_run(argv, **kwargs):
                    if tuple(argv[:3]) == ("systemctl", "--user", "show"):
                        return SimpleNamespace(returncode=0, stdout="ActiveState=inactive\nSubState=dead\nMainPID=0\n", stderr="")
                    if tuple(argv[:3]) == ("systemctl", "--user", "is-enabled"):
                        return SimpleNamespace(returncode=1, stdout="disabled\n", stderr="")
                    if tuple(argv[:3]) == ("systemctl", "--user", "cat"):
                        return SimpleNamespace(returncode=0, stdout=bridge.render_unit(binding), stderr="")
                    return SimpleNamespace(returncode=0, stdout="", stderr="")

                with mock.patch.object(bridge.LocalReceiverBackend, "_require_current_account", return_value=None), \
                     mock.patch("tools.codex_telegram_bridge.subprocess.run", side_effect=fake_run):
                    with self.assertRaises(bridge.BridgeError):
                        bridge.bridge_stage(archive, binding, fault_after=fault_after)
                self.assertEqual(allowlist.read_text(encoding="utf-8"), before["allowlist"])
                self.assertEqual(unit.read_text(encoding="utf-8"), before["unit"])
                self.assertEqual(mcp.read_text(encoding="utf-8"), before["mcp"])
                self.assertEqual(os.readlink(binding.current_release), before["current"])
                self.assertFalse((binding.state_dir / "receipts" / f"stage-{binding.source.revision}.json").exists())
                promoted = binding.release_root / "releases" / binding.source.revision
                self.assertFalse(promoted.exists())

    def test_bridge_stage_receipt_records_effective_pane(self) -> None:
        binding = self.manifest.codex_telegram_binding("specialist", root=self.root)
        archive = self._archive({path: bridge.CODEX_TELEGRAM_SOURCE_SHA256[path].encode("ascii") for path in bridge.CODEX_TELEGRAM_SOURCE_PATHS})

        def fake_run(argv, **kwargs):
            if tuple(argv[:3]) == ("systemctl", "--user", "show"):
                return SimpleNamespace(returncode=0, stdout="ActiveState=inactive\nSubState=dead\nMainPID=0\n", stderr="")
            if tuple(argv[:3]) == ("systemctl", "--user", "is-enabled"):
                return SimpleNamespace(returncode=1, stdout="disabled\n", stderr="")
            if tuple(argv[:3]) == ("systemctl", "--user", "cat"):
                return SimpleNamespace(returncode=0, stdout=bridge.render_unit(binding), stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(bridge, "verify_archive", return_value=bridge.SourceHashReceipt("s", binding.source.revision, binding.source.sha256, "m")), \
             mock.patch.object(bridge.LocalReceiverBackend, "_require_current_account", return_value=None), \
             mock.patch("tools.codex_telegram_bridge.subprocess.run", side_effect=fake_run):
            receipt = bridge.bridge_stage(archive, binding)
        self.assertEqual(receipt["effective_pane"], "claude-research-bot:0.0")
        on_disk = json.loads(Path(receipt["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["effective_pane"], receipt["effective_pane"])

    def test_bridge_stage_replaces_old_no_env_unit_while_disabled(self) -> None:
        binding = self.manifest.codex_telegram_binding("specialist", root=self.root)
        archive = self._archive({path: bridge.CODEX_TELEGRAM_SOURCE_SHA256[path].encode("ascii") for path in bridge.CODEX_TELEGRAM_SOURCE_PATHS})
        unit = binding.account_home / ".config" / "systemd" / "user" / binding.codex_receiver_unit
        unit.parent.mkdir(parents=True)
        unit.write_text(
            "[Service]\nExecStart=/usr/bin/python3 %h/pantheon-config/tools/codex_telegram_bridge.py exec --mode poller\n",
            encoding="utf-8",
        )

        def fake_run(argv, **kwargs):
            if tuple(argv[:3]) == ("systemctl", "--user", "show"):
                return SimpleNamespace(returncode=0, stdout="ActiveState=inactive\nSubState=dead\nMainPID=0\n", stderr="")
            if tuple(argv[:3]) == ("systemctl", "--user", "is-enabled"):
                return SimpleNamespace(returncode=1, stdout="disabled\n", stderr="")
            if tuple(argv[:3]) == ("systemctl", "--user", "cat"):
                return SimpleNamespace(returncode=0, stdout=unit.read_text(encoding="utf-8"), stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(bridge, "verify_archive", return_value=bridge.SourceHashReceipt("s", binding.source.revision, binding.source.sha256, "m")), \
             mock.patch.object(bridge.LocalReceiverBackend, "_require_current_account", return_value=None), \
             mock.patch("tools.codex_telegram_bridge.subprocess.run", side_effect=fake_run):
            bridge.bridge_stage(archive, binding)
        installed = unit.read_text(encoding="utf-8")
        self.assertIn("Environment=CODEX_TG_PANE=claude-research-bot:0.0\n", installed)
        self.assertNotIn("codex-session", installed)

    def test_stage_fault_restores_prior_unit_with_pane_env_change(self) -> None:
        binding = self.manifest.codex_telegram_binding("specialist", root=self.root)
        archive = self._canonical_archive()
        unit = binding.account_home / ".config" / "systemd" / "user" / binding.codex_receiver_unit
        unit.parent.mkdir(parents=True)
        prior = bridge.render_unit(bridge.dataclasses.replace(binding, tmux_session="old-codex-pane"))
        unit.write_text(prior, encoding="utf-8")

        def fake_run(argv, **kwargs):
            if tuple(argv[:3]) == ("systemctl", "--user", "show"):
                return SimpleNamespace(returncode=0, stdout="ActiveState=inactive\nSubState=dead\nMainPID=0\n", stderr="")
            if tuple(argv[:3]) == ("systemctl", "--user", "is-enabled"):
                return SimpleNamespace(returncode=1, stdout="disabled\n", stderr="")
            if tuple(argv[:3]) == ("systemctl", "--user", "cat"):
                return SimpleNamespace(returncode=0, stdout=unit.read_text(encoding="utf-8"), stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(bridge.LocalReceiverBackend, "_require_current_account", return_value=None), \
             mock.patch("tools.codex_telegram_bridge.subprocess.run", side_effect=fake_run):
            with self.assertRaises(bridge.BridgeError):
                bridge.bridge_stage(archive, binding, fault_after="bridge-daemon-reload")
        self.assertEqual(unit.read_text(encoding="utf-8"), prior)

    def test_bridge_stage_writes_durable_redacted_receipt_after_disabled_first(self) -> None:
        binding = self.manifest.codex_telegram_binding("specialist", root=self.root)
        archive = self._archive({path: bridge.CODEX_TELEGRAM_SOURCE_SHA256[path].encode("ascii") for path in bridge.CODEX_TELEGRAM_SOURCE_PATHS})
        calls: list[tuple[str, ...]] = []

        def fake_run(argv, **kwargs):
            calls.append(tuple(argv))
            if tuple(argv[:3]) == ("systemctl", "--user", "show"):
                return SimpleNamespace(returncode=0, stdout="ActiveState=inactive\nSubState=dead\nMainPID=0\n", stderr="")
            if tuple(argv[:3]) == ("systemctl", "--user", "is-enabled"):
                return SimpleNamespace(returncode=1, stdout="disabled\n", stderr="")
            if tuple(argv[:3]) == ("systemctl", "--user", "cat"):
                return SimpleNamespace(returncode=0, stdout=bridge.render_unit(binding), stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(bridge, "verify_archive", return_value=bridge.SourceHashReceipt("s", binding.source.revision, binding.source.sha256, "m")), \
             mock.patch.object(bridge.LocalReceiverBackend, "_require_current_account", return_value=None), \
             mock.patch("tools.codex_telegram_bridge.subprocess.run", side_effect=fake_run):
            receipt = bridge.bridge_stage(archive, binding)
        self.assertEqual(receipt["operations"], ["verified-archive-release", "allowlist", "unit-write", "daemon-reload", "mcp-registration", "disabled-first", "receipt"])
        path = Path(receipt["receipt_path"])
        self.assertTrue(path.is_file())
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(bridge._sha256_bytes(path.read_bytes()), receipt["receipt_sha256"])
        self.assertEqual(on_disk["operations"], receipt["operations"])
        self.assertNotIn(SECRET, path.read_text(encoding="utf-8"))
        self.assertIn(("systemctl", "--user", "daemon-reload"), calls)

    def test_precutover_mailbox_probe_proves_delivery_not_consumption(self) -> None:
        receipt = bridge.precutover_mailbox_probe("specialist", self.root / "mailbox")
        self.assertTrue(receipt["durable_delivery"])
        self.assertFalse(receipt["consumption_verified"])

    def test_orchestrator_codex_launch_adds_only_manifest_declared_mailbox_roots(self) -> None:
        binding = persona_bringup.load_binding(
            REPO_ROOT / "instance.yaml",
            "orchestrator",
            harness_override="codex",
        )
        adapter = CodexHarnessAdapter("orchestrator", codex_home=Path("/tmp/codex-home"), launch=binding.launch)
        spec = adapter.build_interactive_launch(binding.controller.tmux, cwd=REPO_ROOT)
        pairs = [
            spec.argv[index + 1]
            for index, token in enumerate(spec.argv[:-1])
            if token == "--add-dir"
        ]
        self.assertEqual(
            pairs,
            [
                "/home/post-office/mailbox/orchestrator",
                "/home/post-office/mailbox/codex",
            ],
        )
        self.assertNotIn("/home/post-office/mailbox", pairs)
        self.assertFalse(any(item.startswith("--config") and "writable" in item for item in spec.argv))

    def test_codex_launch_repeats_add_dir_for_typed_writable_roots(self) -> None:
        launch = dataclasses.replace(
            persona_bringup.load_binding(
                REPO_ROOT / "instance.yaml",
                "orchestrator",
                harness_override="codex",
            ).launch,
            writable_roots=(Path("/mailbox/orchestrator"), Path("/mailbox/codex")),
        )
        spec = CodexHarnessAdapter("orchestrator", launch=launch).build_interactive_launch(
            persona_bringup.load_binding(REPO_ROOT / "instance.yaml", "orchestrator").controller.tmux,
            cwd=REPO_ROOT,
        )
        self.assertEqual(
            [item for item in spec.argv if item == "--add-dir"],
            ["--add-dir", "--add-dir"],
        )
        self.assertLess(spec.argv.index("/mailbox/orchestrator"), spec.argv.index("/mailbox/codex"))

    def test_mailbox_writable_roots_reject_hub_traversal_symlink_and_undeclared_siblings(self) -> None:
        from tools.mailbox_writable_roots import MailboxWritableRootError, resolve_mailbox_writable_roots

        manifest_path, _account = self._mailbox_roundtrip_manifest()
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        with self.assertRaises(MailboxWritableRootError):
            data["harness_runtime"]["roles"]["orchestrator"]["codex"]["mailbox_writable_roots"].append({"kind": "persona-mailbox", "persona": "../recovery"})
            resolve_mailbox_writable_roots(
                data,
                role="orchestrator",
                harness="codex",
                bound_persona="orchestrator",
                bound_mailbox=self.root / "mailbox" / "orchestrator",
                mailbox_hub=self.root / "mailbox",
            )
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        data["harness_runtime"]["roles"]["orchestrator"]["codex"]["mailbox_writable_roots"].append({"kind": "persona-mailbox", "persona": "orchestrator"})
        with self.assertRaises(MailboxWritableRootError):
            resolve_mailbox_writable_roots(
                data,
                role="orchestrator",
                harness="codex",
                bound_persona="orchestrator",
                bound_mailbox=self.root / "mailbox" / "orchestrator",
                mailbox_hub=self.root / "mailbox",
            )
        self._provision_roundtrip_dirs()
        symlink_hub = self.root / "hub-link"
        symlink_hub.symlink_to(self.root / "mailbox")
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        with self.assertRaises(MailboxWritableRootError):
            resolve_mailbox_writable_roots(
                data,
                role="orchestrator",
                harness="codex",
                bound_persona="orchestrator",
                bound_mailbox=symlink_hub / "orchestrator",
                mailbox_hub=symlink_hub,
                require_existing=True,
            )
        missing = self.root / "mailbox" / "codex" / "cur"
        missing.rmdir()
        with self.assertRaises(bridge.BridgeError):
            bridge.mailbox_roundtrip_prepare(
                manifest_path=manifest_path,
                persona="orchestrator",
                reply_persona="codex",
                correlation_id="missing-subdir",
                root=self.root,
            )

    def test_non_orchestrator_codex_roles_default_to_zero_additional_mailbox_roots(self) -> None:
        for persona in ("recovery", "specialist", "codex"):
            with self.subTest(persona=persona):
                binding = persona_bringup.load_binding(
                    REPO_ROOT / "instance.yaml",
                    persona,
                    harness_override="codex",
                )
                self.assertEqual(binding.launch.writable_roots, ())

    def test_orchestrator_command_sheet_step23_declares_nonce_correlated_consume_ack_wake_and_reply_evidence(self) -> None:
        sheet = bridge.command_sheet(
            "orchestrator",
            expected_pantheon_revision="0" * 40,
            manifest_path=REPO_ROOT / "instance.yaml",
        )
        prepare = _step(sheet, "23a-mailbox-codex-roundtrip-prepare")
        gate = _step(sheet, "23b-mailbox-codex-consume-reply-wake")
        verify = _step(sheet, "23c-mailbox-codex-roundtrip-verify")
        self.assertIn("mailbox-roundtrip-prepare", str(prepare["command"]))
        self.assertIn("mailbox-roundtrip-verify", str(verify["command"]))
        self.assertIn("--reply-persona codex", str(prepare["command"]))
        self.assertIn("--reply-persona codex", str(verify["command"]))
        gate_payload = gate["gate"]
        self.assertIsInstance(gate_payload, Mapping)
        expected = " ".join(gate_payload["expected"])  # type: ignore[index]
        for token in ("message_id", "correlation_id", "ack record", "reply lands"):
            self.assertIn(token, expected)

    def test_command_sheet_steps20_22_require_unattended_tool_invocation_evidence_without_ui_click(self) -> None:
        sheet = bridge.command_sheet(
            "orchestrator",
            expected_pantheon_revision="0" * 40,
            manifest_path=REPO_ROOT / "instance.yaml",
        )
        expectations = {
            "20-telegram-text-canary": ("reply", "telegram-text-inbound-reply"),
            "21-telegram-document-canary": ("download_attachment", "telegram-document-download"),
            "22-telegram-photo-canary": ("download_attachment", "telegram-photo-download"),
        }
        prepare = _step(sheet, "19a-telegram-unattended-evidence-prepare")
        self.assertEqual(prepare["type"], "command")
        prepare_command = str(prepare["command"])
        self.assertIn("telegram-unattended-evidence", prepare_command)
        self.assertIn("--action prepare", prepare_command)
        self.assertIn("--persona orchestrator", prepare_command)
        self.assertIn("--stamp", prepare_command)
        self.assertIn("<operator-stamp>", prepare_command)
        self.assertNotIn("--evidence", prepare_command)
        for step_id, (tool, canary) in expectations.items():
            with self.subTest(step=step_id):
                gate = _step(sheet, f"{step_id}-operator-action")
                self.assertEqual(gate["type"], "operator-gate")
                gate_payload = gate["gate"]
                self.assertIsInstance(gate_payload, Mapping)
                self.assertEqual(gate_payload["correlation_id"], bridge.telegram_unattended_correlation_id("<operator-stamp>", step_id))
                self.assertEqual(gate_payload["tool"], tool)
                self.assertEqual(gate_payload["canary"], canary)
                self.assertIn("rollback", json.dumps(gate_payload))
                step = _step(sheet, step_id)
                self.assertEqual(step["type"], "command")
                command = str(step["command"])
                self.assertIn("telegram-unattended-evidence", command)
                self.assertIn("--action record", command)
                self.assertIn("--persona orchestrator", command)
                self.assertIn("--stamp", command)
                self.assertIn("<operator-stamp>", command)
                self.assertIn(f"--step-id {step_id}", command)
                self.assertIn(f"--tool {tool}", command)
                self.assertIn(f"--canary {canary}", command)
                self.assertIn("--observed-correlation-id", command)
                self.assertIn(bridge.telegram_unattended_correlation_id("<operator-stamp>", step_id), command)
                self.assertIn("--permission-prompt-observed false", command)
                self.assertIn("--ui-approval-clicked false", command)
                self.assertIn("--manifest /home/claude/pantheon-config/instance.yaml", command)
                self.assertNotIn("--evidence", command)
                self.assertNotIn("confirmation_required", step)
                self.assertNotIn("ui_click", command)

    def test_orchestrator_command_sheet_forbids_target_receipt_writes_or_names_runtime_observation_surface(self) -> None:
        sheet = bridge.command_sheet(
            "orchestrator",
            expected_pantheon_revision="0" * 40,
            manifest_path=REPO_ROOT / "instance.yaml",
        )
        for step_id in ("20-telegram-text-canary", "21-telegram-document-canary", "22-telegram-photo-canary"):
            with self.subTest(step=step_id):
                gate = _step(sheet, f"{step_id}-operator-action")
                gate_payload = gate["gate"]
                self.assertIsInstance(gate_payload, Mapping)
                gate_json = json.dumps(gate_payload, sort_keys=True)
                self.assertIn("runtime observation", gate_json)
                self.assertIn("runtime-observations", gate_json)
                self.assertIn("operator receipt", gate_json)
                self.assertIn("MUST NOT", gate_json)
                self.assertNotIn(f"operator-receipts/{step_id}.json", gate_json)

    def test_unattended_target_same_account_prewrite_does_not_collide_with_operator_receipt(self) -> None:
        repo_manifest, _account = self._telegram_evidence_manifest()
        sheet = bridge.command_sheet(
            "orchestrator",
            expected_pantheon_revision="0" * 40,
            manifest_path=repo_manifest,
            root=self.root,
            telegram_evidence_stamp="stampCollision",
        )
        rc, prepare_receipt = self._run_generated_bridge_command(
            _step(sheet, "19a-telegram-unattended-evidence-prepare")
        )
        self.assertEqual(rc, 0)
        observation = self._write_runtime_observation(
            prepare_receipt,
            step_id="21-telegram-document-canary",
            tool="download_attachment",
            canary="telegram-document-download",
            correlation_id=bridge.telegram_unattended_correlation_id("stampCollision", "21-telegram-document-canary"),
        )
        self.assertIn("runtime-observations", observation.parts)
        legacy_root_claim = Path(str(prepare_receipt["root"])) / "21-telegram-document-canary.json"
        legacy_root_claim.write_text(
            json.dumps({"schema": bridge.TELEGRAM_UNATTENDED_OBSERVATION_SCHEMA, "correlation_id": "legacy-wrong"}) + "\n",
            encoding="utf-8",
        )
        legacy_root_claim.chmod(0o600)

        rc, receipt = self._run_generated_bridge_command(_step(sheet, "21-telegram-document-canary"))
        self.assertEqual(rc, 0, receipt)
        self.assertTrue(receipt["ok"])
        self.assertEqual(Path(str(receipt["runtime_observation_path"])), observation)
        operator_receipt = Path(str(receipt["operator_receipt_path"]))
        self.assertIn("operator-receipts", operator_receipt.parts)
        self.assertTrue(operator_receipt.is_file())

    def test_telegram_unattended_record_preserves_exclusive_no_replace_and_correlation_checks(self) -> None:
        repo_manifest, _account = self._telegram_evidence_manifest()
        base_args = [
            "telegram-unattended-evidence",
            "--persona",
            "orchestrator",
            "--manifest",
            str(repo_manifest),
            "--root",
            str(self.root),
            "--json",
        ]

        def run(args: list[str]) -> tuple[int, Mapping[str, object]]:
            with mock.patch("builtins.print") as printed:
                rc = bridge.main(args)
            payload = json.loads(printed.call_args.args[0])
            self.assertIsInstance(payload, Mapping)
            return rc, payload

        rc, prepare_receipt = run([*base_args, "--action", "prepare", "--stamp", "stampExclusive"])
        self.assertEqual(rc, 0, prepare_receipt)
        step_id = "20-telegram-text-canary"
        correlation_id = bridge.telegram_unattended_correlation_id("stampExclusive", step_id)
        self._write_runtime_observation(
            prepare_receipt,
            step_id=step_id,
            tool="reply",
            canary="telegram-text-inbound-reply",
            correlation_id=correlation_id,
        )
        record_args = [
            *base_args,
            "--action",
            "record",
            "--stamp",
            "stampExclusive",
            "--step-id",
            step_id,
            "--tool",
            "reply",
            "--canary",
            "telegram-text-inbound-reply",
            "--observed-correlation-id",
            correlation_id,
            "--result-status",
            "success",
            "--permission-prompt-observed",
            "false",
            "--ui-approval-clicked",
            "false",
            "--invocation-tool",
            "reply",
        ]
        rc, receipt = run(record_args)
        self.assertEqual(rc, 0, receipt)
        operator_receipt = Path(str(receipt["operator_receipt_path"]))
        first_sha = receipt["operator_receipt_sha256"]
        self.assertNotIn("operator_receipt_sha256", operator_receipt.read_text(encoding="utf-8"))
        rc, duplicate = run(record_args)
        self.assertEqual(rc, 1)
        self.assertIn("already exists", str(duplicate["error"]))
        self.assertEqual(bridge._sha256_bytes(operator_receipt.read_bytes()), first_sha)

        rc, mismatch_prepare = run([*base_args, "--action", "prepare", "--stamp", "stampMismatch"])
        self.assertEqual(rc, 0)
        self._write_runtime_observation(
            mismatch_prepare,
            step_id=step_id,
            tool="reply",
            canary="telegram-text-inbound-reply",
            correlation_id=bridge.telegram_unattended_correlation_id("stampMismatch", step_id),
        )
        mismatch_args = list(record_args)
        mismatch_args[mismatch_args.index("--stamp") + 1] = "stampMismatch"
        mismatch_args[mismatch_args.index("--observed-correlation-id") + 1] = "wrong-correlation"
        rc, mismatch = run(mismatch_args)
        self.assertEqual(rc, 1)
        self.assertIn("correlation_id mismatch", str(mismatch["error"]))

    def test_telegram_unattended_evidence_requires_secure_subdirs_and_matching_operator_assertions(self) -> None:
        repo_manifest, _account = self._telegram_evidence_manifest()
        base_args = [
            "telegram-unattended-evidence",
            "--persona",
            "orchestrator",
            "--manifest",
            str(repo_manifest),
            "--root",
            str(self.root),
            "--json",
        ]

        def run(args: list[str]) -> tuple[int, Mapping[str, object]]:
            with mock.patch("builtins.print") as printed:
                rc = bridge.main(args)
            payload = json.loads(printed.call_args.args[0])
            self.assertIsInstance(payload, Mapping)
            return rc, payload

        rc, prepare_receipt = run([*base_args, "--action", "prepare", "--stamp", "stampModes"])
        self.assertEqual(rc, 0, prepare_receipt)
        for key in ("runtime_observations_dir", "operator_receipts_dir"):
            path = Path(str(prepare_receipt[key]))
            self.assertTrue(path.is_dir())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            self.assertEqual(path.stat().st_uid, os.geteuid())

        step_id = "20-telegram-text-canary"
        correlation_id = bridge.telegram_unattended_correlation_id("stampModes", step_id)
        observation = self._write_runtime_observation(
            prepare_receipt,
            step_id=step_id,
            tool="reply",
            canary="telegram-text-inbound-reply",
            correlation_id=correlation_id,
            invocation_tool="react",
        )
        record_args = [
            *base_args,
            "--action",
            "record",
            "--stamp",
            "stampModes",
            "--step-id",
            step_id,
            "--tool",
            "reply",
            "--canary",
            "telegram-text-inbound-reply",
            "--observed-correlation-id",
            correlation_id,
            "--result-status",
            "success",
            "--permission-prompt-observed",
            "false",
            "--ui-approval-clicked",
            "false",
            "--invocation-tool",
            "reply",
        ]
        rc, payload = run(record_args)
        self.assertEqual(rc, 1)
        self.assertIn("invocation", str(payload["error"]))

        observation.unlink()
        symlink_target = self.root / "outside-observation.json"
        symlink_target.write_text("{}", encoding="utf-8")
        observation.symlink_to(symlink_target)
        rc, payload = run(record_args)
        self.assertEqual(rc, 1)
        self.assertIn("symlink", str(payload["error"]))

        observation.unlink()
        observation.write_text("x" * (bridge.TELEGRAM_UNATTENDED_EVIDENCE_LIMIT_BYTES + 1), encoding="utf-8")
        observation.chmod(0o600)
        rc, payload = run(record_args)
        self.assertEqual(rc, 1)
        self.assertIn("bounded size", str(payload["error"]))

    def test_telegram_reply_download_unattended_green_and_undeclared_tool_still_gated(self) -> None:
        prepare_nonce = "nonce-1"
        green_reply = {
            "schema": bridge.TELEGRAM_UNATTENDED_OBSERVATION_SCHEMA,
            "prepare_nonce": prepare_nonce,
            "step_id": "20-telegram-text-canary",
            "tool": "reply",
            "canary": "telegram-text-inbound-reply",
            "result_status": "success",
            "correlation_id": bridge.telegram_unattended_correlation_id("stampA", "20-telegram-text-canary"),
            "permission_prompt_observed": False,
            "ui_approval_clicked": False,
            "invocation": {"tool": "reply"},
        }
        self.assertEqual(
            bridge.validate_unattended_tool_invocation_evidence(
                green_reply,
                step_id="20-telegram-text-canary",
                tool="reply",
                canary_kind="telegram-text-inbound-reply",
                expected_correlation_id=bridge.telegram_unattended_correlation_id("stampA", "20-telegram-text-canary"),
                prepare_nonce=prepare_nonce,
            ),
            green_reply,
        )
        green_download = {
            "schema": bridge.TELEGRAM_UNATTENDED_OBSERVATION_SCHEMA,
            "prepare_nonce": prepare_nonce,
            "step_id": "21-telegram-document-canary",
            "tool": "download_attachment",
            "canary": "telegram-document-download",
            "result_status": "success",
            "correlation_id": bridge.telegram_unattended_correlation_id("stampA", "21-telegram-document-canary"),
            "permission_prompt_observed": False,
            "ui_approval_clicked": False,
            "invocation": {"tool": "download_attachment"},
        }
        self.assertEqual(
            bridge.validate_unattended_tool_invocation_evidence(
                green_download,
                step_id="21-telegram-document-canary",
                tool="download_attachment",
                canary_kind="telegram-document-download",
                expected_correlation_id=bridge.telegram_unattended_correlation_id("stampA", "21-telegram-document-canary"),
                prepare_nonce=prepare_nonce,
            ),
            green_download,
        )
        blocked = dict(green_reply, tool="delete_message", invocation={"tool": "delete_message"})
        with self.assertRaises(bridge.BridgeError):
            bridge.validate_unattended_tool_invocation_evidence(
                blocked,
                step_id="20-telegram-text-canary",
                tool="reply",
                canary_kind="telegram-text-inbound-reply",
                expected_correlation_id=bridge.telegram_unattended_correlation_id("stampA", "20-telegram-text-canary"),
                prepare_nonce=prepare_nonce,
            )

        repo_manifest, _account = self._telegram_evidence_manifest()
        sheet = bridge.command_sheet(
            "orchestrator",
            expected_pantheon_revision="0" * 40,
            manifest_path=repo_manifest,
            root=self.root,
            telegram_evidence_stamp="stampA",
        )
        rc, prepare_receipt = self._run_generated_bridge_command(
            _step(sheet, "19a-telegram-unattended-evidence-prepare")
        )
        self.assertEqual(rc, 0)
        self.assertEqual(prepare_receipt["stamp"], "stampA")
        self.assertEqual(prepare_receipt["steps"]["20-telegram-text-canary"]["correlation_id"], green_reply["correlation_id"])
        for step_id in ("20-telegram-text-canary", "21-telegram-document-canary"):
            tool = "reply" if step_id == "20-telegram-text-canary" else "download_attachment"
            canary = "telegram-text-inbound-reply" if step_id == "20-telegram-text-canary" else "telegram-document-download"
            self._write_runtime_observation(
                prepare_receipt,
                step_id=step_id,
                tool=tool,
                canary=canary,
                correlation_id=bridge.telegram_unattended_correlation_id("stampA", step_id),
            )
            rc, receipt = self._run_generated_bridge_command(_step(sheet, step_id))
            self.assertEqual(rc, 0)
            self.assertTrue(receipt["ok"])
            self.assertEqual(receipt["correlation_id"], bridge.telegram_unattended_correlation_id("stampA", step_id))
            evidence_path = Path(str(receipt["evidence_path"]))
            self.assertEqual(stat.S_IMODE(evidence_path.stat().st_mode), 0o600)

        with mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                bridge.main(
                    [
                        "telegram-unattended-evidence",
                        "--action",
                        "record",
                        "--persona",
                        "orchestrator",
                        "--stamp",
                        "stampA",
                        "--evidence",
                        str(self.root / "bad.json"),
                    ]
                )

    def test_telegram_unattended_evidence_rejects_unsafe_or_stale_protocol_state(self) -> None:
        repo_manifest, _account = self._telegram_evidence_manifest()
        base_args = [
            "telegram-unattended-evidence",
            "--persona",
            "orchestrator",
            "--manifest",
            str(repo_manifest),
            "--root",
            str(self.root),
            "--json",
        ]

        def run(args: list[str]) -> tuple[int, Mapping[str, object]]:
            with mock.patch("builtins.print") as printed:
                rc = bridge.main(args)
            payload = json.loads(printed.call_args.args[0])
            self.assertIsInstance(payload, Mapping)
            return rc, payload

        rc, _payload = run([*base_args, "--action", "prepare", "--stamp", "freshA"])
        self.assertEqual(rc, 0)
        rc, payload = run([*base_args, "--action", "prepare", "--stamp", "freshA"])
        self.assertEqual(rc, 1)
        self.assertIn("already exists", str(payload["error"]))

        record_args = [
            *base_args,
            "--action",
            "record",
            "--stamp",
            "freshA",
            "--step-id",
            "20-telegram-text-canary",
            "--tool",
            "reply",
            "--canary",
            "telegram-text-inbound-reply",
            "--observed-correlation-id",
            bridge.telegram_unattended_correlation_id("freshA", "20-telegram-text-canary"),
            "--result-status",
            "success",
            "--permission-prompt-observed",
            "false",
            "--ui-approval-clicked",
            "false",
            "--invocation-tool",
            "reply",
        ]
        self._write_runtime_observation(
            _payload,
            step_id="20-telegram-text-canary",
            tool="reply",
            canary="telegram-text-inbound-reply",
            correlation_id=bridge.telegram_unattended_correlation_id("freshA", "20-telegram-text-canary"),
        )
        rc, _payload = run(record_args)
        self.assertEqual(rc, 0)

        for bad_stamp in ("../escape", "bad/stamp", ".hidden"):
            with self.subTest(stamp=bad_stamp):
                rc, payload = run([*base_args, "--action", "prepare", "--stamp", bad_stamp])
                self.assertEqual(rc, 1)
                self.assertIn("stamp", str(payload["error"]))

        rc, payload = run([*base_args, "--action", "record", "--stamp", "noprepare", *record_args[record_args.index("--step-id"):]])
        self.assertEqual(rc, 1)
        self.assertIn("missing", str(payload["error"]))

        rc, _payload = run([*base_args, "--action", "prepare", "--stamp", "freshB"])
        self.assertEqual(rc, 0)
        self._write_runtime_observation(
            _payload,
            step_id="20-telegram-text-canary",
            tool="reply",
            canary="telegram-text-inbound-reply",
            correlation_id=bridge.telegram_unattended_correlation_id("freshB", "20-telegram-text-canary"),
        )
        bad_corr = list(record_args)
        bad_corr[bad_corr.index("--stamp") + 1] = "freshB"
        bad_corr[bad_corr.index("--observed-correlation-id") + 1] = "wrong-correlation"
        rc, payload = run(bad_corr)
        self.assertEqual(rc, 1)
        self.assertIn("correlation_id mismatch", str(payload["error"]))

        rc, _payload = run([*base_args, "--action", "prepare", "--stamp", "freshC"])
        self.assertEqual(rc, 0)
        prompt_true = list(record_args)
        prompt_true[prompt_true.index("--stamp") + 1] = "freshC"
        prompt_true[prompt_true.index("--observed-correlation-id") + 1] = bridge.telegram_unattended_correlation_id("freshC", "20-telegram-text-canary")
        prompt_true[prompt_true.index("--permission-prompt-observed") + 1] = "true"
        rc, payload = run(prompt_true)
        self.assertEqual(rc, 1)
        self.assertIn("permission prompt", str(payload["error"]))

        evidence_root = (
            self.root
            / "home"
            / pwd.getpwuid(os.geteuid()).pw_name
            / ".local"
            / "state"
            / "pantheon"
            / "t2953-telegram-evidence"
        )
        symlink_target = self.root / "outside-evidence"
        symlink_target.mkdir()
        os.symlink(symlink_target, evidence_root / "linked")
        rc, payload = run([*base_args, "--action", "record", "--stamp", "linked", *record_args[record_args.index("--step-id"):]])
        self.assertEqual(rc, 1)
        self.assertIn("symlink", str(payload["error"]))

        rc, _payload = run([*base_args, "--action", "prepare", "--stamp", "freshD"])
        self.assertEqual(rc, 0)
        prepare_path = evidence_root / "freshD" / "prepare.json"
        prepare_path.chmod(0o644)
        bad_mode = list(record_args)
        bad_mode[bad_mode.index("--stamp") + 1] = "freshD"
        bad_mode[bad_mode.index("--observed-correlation-id") + 1] = bridge.telegram_unattended_correlation_id("freshD", "20-telegram-text-canary")
        rc, payload = run(bad_mode)
        self.assertEqual(rc, 1)
        self.assertIn("mode must be 0600", str(payload["error"]))

    def test_orchestrator_mailbox_roundtrip_fixture_distinguishes_controller_wake_from_tui_ack(self) -> None:
        manifest_path, account = self._mailbox_roundtrip_manifest()
        self._provision_roundtrip_dirs()
        correlation_id = "roundtrip-green"
        prepare = bridge.mailbox_roundtrip_prepare(
            manifest_path=manifest_path,
            persona="orchestrator",
            reply_persona="codex",
            correlation_id=correlation_id,
            root=self.root,
        )
        message_id = str(prepare["source_message_id"])
        self._append_controller_acceptance(account=account, message_id=message_id, correlation_id=correlation_id)
        with self.assertRaises(bridge.BridgeError):
            bridge.mailbox_roundtrip_verify(
                manifest_path=manifest_path,
                persona="orchestrator",
                reply_persona="codex",
                correlation_id=correlation_id,
                root=self.root,
            )
        self._ack_and_reply_roundtrip(account=account, correlation_id=correlation_id)
        verified = bridge.mailbox_roundtrip_verify(
            manifest_path=manifest_path,
            persona="orchestrator",
            reply_persona="codex",
            correlation_id=correlation_id,
            root=self.root,
        )
        self.assertTrue(verified["ok"])
        self.assertEqual(verified["controller_wake_event"], "runtime_submission_accepted")

    def test_orchestrator_mailbox_roundtrip_refuses_missing_ack_or_reply_landing(self) -> None:
        for mode in ("missing-ack", "missing-reply", "wrong-inbox", "reply-only", "ack-only", "stale-prior"):
            with self.subTest(mode=mode):
                self.tearDown()
                self.setUp()
                manifest_path, account = self._mailbox_roundtrip_manifest()
                self._provision_roundtrip_dirs()
                correlation_id = f"roundtrip-{mode}"
                prepare = bridge.mailbox_roundtrip_prepare(
                    manifest_path=manifest_path,
                    persona="orchestrator",
                    reply_persona="codex",
                    correlation_id=correlation_id,
                    root=self.root,
                )
                message_id = str(prepare["source_message_id"])
                self._append_controller_acceptance(account=account, message_id=message_id, correlation_id=correlation_id)
                state = self._load_roundtrip_state(account, correlation_id)
                nonce = str(state["nonce"])
                if mode in {"missing-reply", "ack-only", "stale-prior"}:
                    inbox = self.root / "mailbox" / "orchestrator"
                    os.rename(inbox / "new" / message_id, inbox / "cur" / message_id)
                    with (inbox / ".acks.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps({"id": message_id}, sort_keys=True) + "\n")
                if mode in {"missing-ack", "reply-only"}:
                    bridge.pantheon_mailbox.send(
                        str(self.root / "mailbox" / "codex"),
                        {"kind": "t2953_mailbox_roundtrip_reply", "recipient": "codex", "correlation_id": correlation_id, "nonce": nonce},
                        "orchestrator",
                        dedupe_pending=False,
                    )
                if mode == "wrong-inbox":
                    inbox = self.root / "mailbox" / "orchestrator"
                    os.rename(inbox / "new" / message_id, inbox / "cur" / message_id)
                    with (inbox / ".acks.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps({"id": message_id}, sort_keys=True) + "\n")
                    for subdir in ("tmp", "new", "cur"):
                        (self.root / "mailbox" / "recovery" / subdir).mkdir(parents=True, exist_ok=True)
                    bridge.pantheon_mailbox.send(
                        str(self.root / "mailbox" / "recovery"),
                        {"kind": "t2953_mailbox_roundtrip_reply", "recipient": "codex", "correlation_id": correlation_id, "nonce": nonce},
                        "orchestrator",
                        dedupe_pending=False,
                    )
                if mode == "stale-prior":
                    bridge.pantheon_mailbox.send(
                        str(self.root / "mailbox" / "codex"),
                        {"kind": "t2953_mailbox_roundtrip_reply", "recipient": "codex", "correlation_id": correlation_id, "nonce": "old"},
                        "orchestrator",
                        dedupe_pending=False,
                    )
                with self.assertRaises(bridge.BridgeError):
                    bridge.mailbox_roundtrip_verify(
                        manifest_path=manifest_path,
                        persona="orchestrator",
                        reply_persona="codex",
                        correlation_id=correlation_id,
                        root=self.root,
                    )

    def test_orchestrator_mailbox_roundtrip_accepts_same_second_fractional_utc_controller_evidence(self) -> None:
        manifest_path, account = self._mailbox_roundtrip_manifest()
        self._provision_roundtrip_dirs()
        correlation_id = "roundtrip-fractional-utc"
        prepare = bridge.mailbox_roundtrip_prepare(
            manifest_path=manifest_path,
            persona="orchestrator",
            reply_persona="codex",
            correlation_id=correlation_id,
            root=self.root,
        )
        state_path = (
            self.root
            / "home"
            / account
            / ".local"
            / "state"
            / "pantheon"
            / "personas"
            / "orchestrator"
            / "mailbox-roundtrip"
            / f"{correlation_id}.json"
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["prepared_at"] = "2026-08-15T00:00:00Z"
        state_path.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
        message_id = str(prepare["source_message_id"])
        self._append_controller_acceptance(
            account=account,
            message_id=message_id,
            correlation_id=correlation_id,
            recorded_at="2026-08-15T00:00:00.500000Z",
        )
        self._ack_and_reply_roundtrip(account=account, correlation_id=correlation_id)
        verified = bridge.mailbox_roundtrip_verify(
            manifest_path=manifest_path,
            persona="orchestrator",
            reply_persona="codex",
            correlation_id=correlation_id,
            root=self.root,
        )
        self.assertTrue(verified["ok"])

    def test_orchestrator_mailbox_roundtrip_rejects_stale_malformed_naive_or_non_utc_controller_evidence(self) -> None:
        for mode, recorded_at in (
            ("stale", "2026-08-14T23:59:59.999999Z"),
            ("malformed", "not-a-timestamp"),
            ("naive", "2026-08-15T00:00:00"),
            ("non-utc", "2026-08-15T00:00:01+01:00"),
        ):
            with self.subTest(mode=mode):
                self.tearDown()
                self.setUp()
                manifest_path, account = self._mailbox_roundtrip_manifest()
                self._provision_roundtrip_dirs()
                correlation_id = f"roundtrip-{mode}"
                prepare = bridge.mailbox_roundtrip_prepare(
                    manifest_path=manifest_path,
                    persona="orchestrator",
                    reply_persona="codex",
                    correlation_id=correlation_id,
                    root=self.root,
                )
                state_path = (
                    self.root
                    / "home"
                    / account
                    / ".local"
                    / "state"
                    / "pantheon"
                    / "personas"
                    / "orchestrator"
                    / "mailbox-roundtrip"
                    / f"{correlation_id}.json"
                )
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["prepared_at"] = "2026-08-15T00:00:00Z"
                state_path.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
                message_id = str(prepare["source_message_id"])
                self._append_controller_acceptance(
                    account=account,
                    message_id=message_id,
                    correlation_id=correlation_id,
                    recorded_at=recorded_at,
                )
                self._ack_and_reply_roundtrip(account=account, correlation_id=correlation_id)
                with self.assertRaises(bridge.BridgeError):
                    bridge.mailbox_roundtrip_verify(
                        manifest_path=manifest_path,
                        persona="orchestrator",
                        reply_persona="codex",
                        correlation_id=correlation_id,
                        root=self.root,
                    )

    def test_restart_preserves_offset_and_delivers_next_update_once(self) -> None:
        receipt = bridge.offset_restart_receipt(40, [39, 41, 42])
        self.assertEqual(receipt["after_offset"], 42)
        self.assertEqual(receipt["delivered_update_ids"], [41, 42])
        self.assertTrue(receipt["delivered_once"])

    def _operation(self, plan, operation_id: str):
        return next(operation for operation in plan.operations if operation.operation_id == operation_id)

    def _git(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ("git", "-C", str(repo), *args),
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {result.stderr or result.stdout}")
        return result

    def _git_text(self, repo: Path, *args: str) -> str:
        return self._git(repo, *args).stdout.strip()

    def _source_remediation_fixture(self, name: str, *, clone_after_second: bool = False) -> tuple[Path, Path, Path, str, str]:
        root = self.root / name
        remote = root / "remote.git"
        source = root / "source"
        target = root / "target"
        root.mkdir()
        subprocess.run(("git", "init", "--bare", "--initial-branch=main", str(remote)), check=True, capture_output=True, text=True)
        subprocess.run(("git", "init", "--initial-branch=main", str(source)), check=True, capture_output=True, text=True)
        self._git(source, "config", "user.email", "test@example.invalid")
        self._git(source, "config", "user.name", "Test")
        (source / "payload.txt").write_text("one\n", encoding="utf-8")
        self._git(source, "add", "payload.txt")
        self._git(source, "commit", "-m", "one")
        first = self._git_text(source, "rev-parse", "HEAD")
        self._git(source, "remote", "add", "origin", str(remote))
        self._git(source, "push", "-u", "origin", "main")
        if not clone_after_second:
            subprocess.run(("git", "clone", str(remote), str(target)), check=True, capture_output=True, text=True)
            self._git(target, "config", "user.email", "test@example.invalid")
            self._git(target, "config", "user.name", "Test")
        (source / "payload.txt").write_text("two\n", encoding="utf-8")
        self._git(source, "commit", "-am", "two")
        second = self._git_text(source, "rev-parse", "HEAD")
        self._git(source, "push", "origin", "main")
        if clone_after_second:
            subprocess.run(("git", "clone", str(remote), str(target)), check=True, capture_output=True, text=True)
            self._git(target, "config", "user.email", "test@example.invalid")
            self._git(target, "config", "user.name", "Test")
        return remote, source, target, first, second

    def _run_source_remediation_inner(self, repo: Path, expected: str) -> tuple[int | None, subprocess.CompletedProcess[str]]:
        last = subprocess.CompletedProcess((), 0, "", "")
        for index, command in enumerate(bridge._source_remediation_inner_commands(repo, expected_head=expected)):
            result = subprocess.run(
                command,
                shell=True,
                executable="/bin/bash",
                check=False,
                capture_output=True,
                text=True,
            )
            last = result
            if result.returncode != 0:
                return index, result
        return None, last

    def _write_source_tree(self, root: Path) -> None:
        # compute_source_hashes reads under the pinned subdir (T-4790).
        for path in bridge.CODEX_TELEGRAM_SOURCE_PATHS:
            file_path = root / bridge.CODEX_TELEGRAM_SOURCE_SUBDIR / path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("x", encoding="utf-8")

    def _archive(self, members: dict[str, bytes], *, include_all: bool = True) -> Path:
        path = self.root / f"archive-{len(list(self.root.glob('archive-*')))}.tar"
        with tarfile.open(path, "w") as archive:
            for name, content in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return path

    def _canonical_archive(self) -> Path:
        members = {}
        for path in bridge.CODEX_TELEGRAM_SOURCE_PATHS:
            members[path] = _pinned_codex_config_source(path)
        return self._archive(members)

    def _runtime_owned_processes(self, binding) -> tuple:
        return (
            bridge.ProcessSnapshot(
                1400118,
                1,
                f"{binding.account_home}/.npm-global/bin/claude --channels plugin:telegram@claude-plugins-official --model claude-opus-4-8 --permission-mode bypassPermissions",
            ),
            bridge.ProcessSnapshot(
                1400120,
                1400118,
                f"{binding.account_home}/.bun/bin/bun run --cwd {binding.account_home}/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6 --shell=bun --silent start",
            ),
            bridge.ProcessSnapshot(
                1400121,
                1400120,
                "bun server.ts",
            ),
        )


class TopologySnapshotPendingTests(unittest.TestCase):
    """T-4178: the read-only pending-snapshot predicate the runtime controller
    gates its codex-to-claude post phase on. Not gated on the codex UID: the
    snapshot lives in a temp state_dir and unit probes are scripted."""

    class _Backend(bridge.LocalReceiverBackend):
        def _require_current_account(self, binding):  # type: ignore[override]
            del binding

        def _snapshot_unit(self, unit):  # type: ignore[override]
            del unit
            return {"active_state": "active", "sub_state": "waiting", "main_pid": "0", "enabled": "enabled"}

    def _binding(self, root: Path, persona: str = "recovery"):
        return SimpleNamespace(
            persona=persona,
            account="claude-fallback",
            claude_receiver_topology="runtime-owned",
            state_dir=root,
            watchdog_units=("recovery-telegram-child-watchdog.timer", "recovery-telegram-child-watchdog.service"),
        )

    def test_missing_snapshot_is_not_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(self._Backend().receiver_topology_snapshot_pending(self._binding(Path(tmp))))

    def test_pre_phase_snapshot_is_pending_until_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binding = self._binding(Path(tmp))
            backend = self._Backend()
            backend.execute("snapshot-watchdog-units", binding)
            self.assertTrue(backend.receiver_topology_snapshot_pending(binding))
            backend.execute("complete-receiver-topology-snapshot", binding)
            self.assertFalse(backend.receiver_topology_snapshot_pending(binding))

    def test_foreign_identity_snapshot_raises_instead_of_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._Backend().execute("snapshot-watchdog-units", self._binding(root, persona="specialist"))
            with self.assertRaises(bridge.BridgeError):
                self._Backend().receiver_topology_snapshot_pending(self._binding(root))

    def test_cli_reports_pending_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binding = self._binding(Path(tmp))
            self._Backend().execute("snapshot-watchdog-units", binding)
            out = io.StringIO()
            with mock.patch.object(bridge, "binding_for_current_account", return_value=binding), \
                    mock.patch.object(bridge.LocalReceiverBackend, "_require_current_account", return_value=None), \
                    contextlib.redirect_stdout(out):
                code = bridge.main(["topology-snapshot-state", "--persona", "recovery", "--json"])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertIs(payload["ok"], True)
            self.assertIs(payload["pending"], True)

    def test_cli_error_is_ok_false_exit_1(self) -> None:
        out = io.StringIO()
        with mock.patch.object(bridge, "binding_for_current_account", side_effect=bridge.BridgeError("no binding")), \
                contextlib.redirect_stdout(out):
            code = bridge.main(["topology-snapshot-state", "--persona", "recovery", "--json"])
        self.assertEqual(code, 1)
        payload = json.loads(out.getvalue())
        self.assertIs(payload["ok"], False)
        self.assertIn("no binding", payload["error"])


class UnprovenPriorRuntimeHandoffTests(unittest.TestCase):
    """T-4184: the controller's claude-to-codex quiesce (``--prior-runtime
    unproven``) composes with T-4178's codex-to-claude POST through the
    receiver topology snapshot. Not gated on the codex UID: systemctl is a
    stateful fake and the snapshot lives in a temp state_dir."""

    TIMER = "recovery-telegram-child-watchdog.timer"
    SERVICE = "recovery-telegram-child-watchdog.service"
    CLAUDE_UNIT = "claude-telegram-fallback.service"

    class _FakeSystemd(bridge.LocalReceiverBackend):
        def __init__(self, units):
            super().__init__(timeout_seconds=0)
            self.units = units  # unit -> {"enabled": bool, "active": bool}
            self.claude_present_results: list[bool] = []

        def _require_current_account(self, binding):  # type: ignore[override]
            del binding

        def _require_present(self, binding, *, kind, topology_phase="managed"):  # type: ignore[override]
            del binding, topology_phase
            if kind == "claude" and not self.claude_present_results.pop(0):
                raise bridge.BridgeError("runtime-owned Claude receiver process is not present")
            return {"kind": kind, "expected": "present"}

        def _run(self, argv, *args, **kwargs):  # type: ignore[override]
            argv = tuple(argv)
            assert argv[:2] == ("systemctl", "--user"), argv
            verb, rest = argv[2], argv[3:]
            if verb == "show":
                state = self.units[rest[0]]
                active = "active" if state["active"] else "inactive"
                return subprocess.CompletedProcess(argv, 0, f"ActiveState={active}\nSubState=x\nMainPID=0\n", "")
            if verb == "is-enabled":
                enabled = self.units[rest[0]]["enabled"]
                return subprocess.CompletedProcess(argv, 0 if enabled else 1, "enabled\n" if enabled else "disabled\n", "")
            now = rest[0] == "--now"
            unit = rest[-1]
            if verb == "disable":
                self.units[unit]["enabled"] = False
                if now:
                    self.units[unit]["active"] = False
            elif verb == "enable":
                self.units[unit]["enabled"] = True
            elif verb == "start":
                self.units[unit]["active"] = True
            elif verb == "stop":
                self.units[unit]["active"] = False
            else:
                raise AssertionError(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

    def _binding(self, root: Path):
        return SimpleNamespace(
            persona="recovery",
            account="claude-fallback",
            claude_receiver_topology="runtime-owned",
            claude_receiver_unit=self.CLAUDE_UNIT,
            state_dir=root,
            watchdog_units=(self.TIMER, self.SERVICE),
        )

    def _run(self, backend, binding, direction, phase, *, prior_runtime="live"):
        for action in bridge.handoff_actions(binding, direction, phase, prior_runtime=prior_runtime):
            backend.execute(action, binding)

    def test_repeated_swaps_with_a_middle_post_failure_restore_the_original_enabled_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binding = self._binding(Path(tmp))
            units = {
                self.TIMER: {"enabled": True, "active": True},
                self.SERVICE: {"enabled": False, "active": False},
                self.CLAUDE_UNIT: {"enabled": False, "active": False},
            }
            backend = self._FakeSystemd(units)
            backend.claude_present_results = [False, True]

            self._run(backend, binding, "claude-to-codex", "pre", prior_runtime="unproven")
            self.assertEqual(units[self.TIMER], {"enabled": False, "active": False})
            self.assertTrue(backend.receiver_topology_snapshot_pending(binding))

            with self.assertRaises(bridge.BridgeError):  # the codex-to-claude POST fails
                self._run(backend, binding, "codex-to-claude", "post")
            self.assertEqual(units[self.TIMER], {"enabled": False, "active": False})
            self.assertTrue(backend.receiver_topology_snapshot_pending(binding))

            # codex again (claude capped): the quiesce re-runs over the pending snapshot
            self._run(backend, binding, "claude-to-codex", "pre", prior_runtime="unproven")
            snapshot = backend._read_topology_snapshot(binding)
            self.assertEqual(snapshot["units"][self.TIMER]["enabled"], "enabled")  # first-wins

            self._run(backend, binding, "codex-to-claude", "post")
            self.assertEqual(units[self.TIMER], {"enabled": True, "active": True})
            self.assertEqual(units[self.SERVICE], {"enabled": False, "active": False})
            self.assertFalse(backend.receiver_topology_snapshot_pending(binding))

    def test_handoff_cli_threads_prior_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binding = self._binding(Path(tmp))
            for extra, expect_probe in (((), True), (("--prior-runtime", "unproven"), False)):
                with self.subTest(extra=extra):
                    executed: list[str] = []
                    out = io.StringIO()
                    with mock.patch.object(bridge, "binding_for_current_account", return_value=binding), \
                            mock.patch.object(
                                bridge.LocalReceiverBackend, "execute",
                                side_effect=lambda action, b: executed.append(action) or {"ok": "true"},
                            ), contextlib.redirect_stdout(out):
                        code = bridge.main([
                            "handoff", "--persona", "recovery", "--direction", "claude-to-codex",
                            "--phase", "pre", "--reason", "r", *extra, "--json",
                        ])
                    self.assertEqual(code, 0)
                    payload = json.loads(out.getvalue())
                    self.assertEqual(payload["operations"], executed)
                    self.assertEqual("probe-claude-present" in executed, expect_probe)
                    self.assertIn("stop-watchdog-service", executed)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                bridge.main([
                    "handoff", "--persona", "recovery", "--direction", "claude-to-codex", "--phase", "pre",
                    "--reason", "r", "--prior-runtime", "dead", "--json",
                ])


class CodexPollerConflictHealthTests(unittest.TestCase):
    """T-4184: `health-codex-no-409` counts only the poller's getUpdates 409
    Conflict lines logged since its CURRENT activation, from message bodies.
    Not gated on the codex UID: systemctl and journalctl are scripted."""

    UNIT = "codex-telegram-poller.service"
    CONFLICT = "2026-09-28 12:00:01,512 ERROR codex_telegram.poller: 409 Conflict — another getUpdates poller holds the token; backing off 2s"
    OK_LINE = "2026-09-28 12:00:00,409 INFO codex_telegram.poller: codex telegram poller starting — pane=x"

    class _Scripted(bridge.LocalReceiverBackend):
        def __init__(self, *, start_epoch: float, entries):
            super().__init__(timeout_seconds=0)
            self.start_epoch = start_epoch
            self.entries = entries  # (epoch, pid, message)
            self.journal_argv: tuple[str, ...] = ()

        def _require_current_account(self, binding):  # type: ignore[override]
            del binding

        def _run(self, argv, *args, **kwargs):  # type: ignore[override]
            argv = tuple(argv)
            if argv[0] == "journalctl":
                self.journal_argv = argv
                since = float(argv[argv.index("--since") + 1].lstrip("@")) if "--since" in argv else 0.0
                cat = "-o" in argv and argv[argv.index("-o") + 1] == "cat"
                lines = [
                    message if cat else f"Sep 28 12:00:00 host python3[{pid}]: {message}"
                    for epoch, pid, message in self.entries
                    if epoch >= since
                ]
                return subprocess.CompletedProcess(argv, 0, "".join(f"{line}\n" for line in lines), "")
            if "ActiveEnterTimestampMonotonic" in argv:
                mono = int((time.monotonic() - (time.time() - self.start_epoch)) * 1_000_000)
                return subprocess.CompletedProcess(argv, 0, f"{mono}\n", "")
            if argv[2] == "show":  # _unit_probe for the presence proof
                return subprocess.CompletedProcess(argv, 0, "ActiveState=active\nSubState=running\nMainPID=140923\n", "")
            raise AssertionError(argv)

    def _binding(self):
        return SimpleNamespace(persona="recovery", account="claude-fallback", codex_receiver_unit=self.UNIT)

    def _health(self, backend):
        backend.execute("health-codex-no-409", self._binding())

    def test_stale_conflicts_before_the_current_start_do_not_fail(self) -> None:
        start = time.time() - 30
        backend = self._Scripted(
            start_epoch=start,
            entries=[(start - 100, 111, self.CONFLICT), (start - 99, 111, self.CONFLICT), (start + 5, 140923, self.OK_LINE)],
        )
        self._health(backend)  # no raise
        self.assertIn("--since", backend.journal_argv)

    def test_pid_or_timestamp_containing_409_does_not_match(self) -> None:
        start = time.time() - 30
        backend = self._Scripted(
            start_epoch=start, entries=[(start + i, 140923, self.OK_LINE) for i in range(5)]
        )
        self._health(backend)  # the old bare-"409" count would have failed here
        self.assertEqual(backend.journal_argv[backend.journal_argv.index("-o") + 1], "cat")

    def test_one_conflict_since_start_is_below_the_threshold(self) -> None:
        start = time.time() - 30
        self._health(self._Scripted(start_epoch=start, entries=[(start + 2, 140923, self.CONFLICT)]))

    def test_two_conflicts_since_start_fail_with_the_shared_prefix(self) -> None:
        start = time.time() - 30
        backend = self._Scripted(
            start_epoch=start, entries=[(start + 2, 140923, self.CONFLICT), (start + 4, 140923, self.CONFLICT)]
        )
        with self.assertRaises(bridge.BridgeError) as ctx:
            self._health(backend)
        self.assertTrue(str(ctx.exception).startswith(bridge.HEALTH_409_ERROR_PREFIX))

    def test_no_activation_timestamp_fails_closed(self) -> None:
        backend = self._Scripted(start_epoch=time.time(), entries=[])
        real_run = backend._run
        backend._run = lambda argv, *a, **k: (  # type: ignore[method-assign]
            subprocess.CompletedProcess(tuple(argv), 0, "0\n", "")
            if "ActiveEnterTimestampMonotonic" in argv else real_run(argv, *a, **k)
        )
        with self.assertRaisesRegex(bridge.BridgeError, "no current activation"):
            self._health(backend)


class StopCodexPollerNotFoundTests(unittest.TestCase):
    """T-4158: `stop-codex-poller` treats a unit that does not exist on the
    account (LoadState=not-found; `systemctl stop` exits 5) as already stopped
    and lets `probe-codex-absent` decide; any other stop failure still raises.
    Not gated on the codex UID: every systemctl/ps call is scripted."""

    UNIT = "codex-telegram-poller.service"

    class _Scripted(bridge.LocalReceiverBackend):
        def __init__(self, responses, *, processes=""):
            super().__init__(timeout_seconds=0)
            self.responses = responses
            self.processes = processes
            self.calls: list[tuple[str, ...]] = []

        def _run(self, argv, *args, **kwargs):  # type: ignore[override]
            argv = tuple(argv)
            self.calls.append(argv)
            if argv[0] == "ps":
                return subprocess.CompletedProcess(argv, 0, self.processes, "")
            code, out, err = self.responses[argv[2:]]
            return subprocess.CompletedProcess(argv, code, out, err)

    def _binding(self):
        return SimpleNamespace(
            persona="recovery",
            account="claude-fallback",
            codex_receiver_unit=self.UNIT,
            current_release=Path("/nonexistent/release"),
        )

    def _run_actions(self, backend, *actions):
        binding = self._binding()
        with mock.patch.object(bridge.LocalReceiverBackend, "_require_current_account", return_value=None):
            for action in actions:
                backend.execute(action, binding)

    def _responses(self, *, stop, load_state):
        return {
            ("stop", self.UNIT): stop,
            ("show", self.UNIT, "-p", "LoadState", "--value"): (0, f"{load_state}\n", ""),
            ("show", self.UNIT, "-p", "ActiveState", "-p", "SubState", "-p", "MainPID"): (
                0, "ActiveState=inactive\nSubState=dead\nMainPID=0\n", ""
            ),
        }

    def test_not_loaded_unit_stop_is_tolerated_and_probe_proves_absent(self) -> None:
        backend = self._Scripted(
            self._responses(stop=(5, "", f"Failed to stop {self.UNIT}: Unit {self.UNIT} not loaded."), load_state="not-found")
        )
        self._run_actions(backend, "stop-codex-poller", "probe-codex-absent")
        self.assertIn(("ps", "-u", "claude-fallback", "-o", "pid=,args="), backend.calls)

    def test_not_loaded_unit_with_stray_poller_process_still_fails_the_probe(self) -> None:
        backend = self._Scripted(
            self._responses(stop=(5, "", "Unit not loaded."), load_state="not-found"),
            processes="4242 /usr/bin/python3 /x/codex_telegram_bridge.py exec --mode poller\n",
        )
        self._run_actions(backend, "stop-codex-poller")  # tolerated
        with self.assertRaisesRegex(bridge.BridgeError, "codex receiver still present"):
            self._run_actions(backend, "probe-codex-absent")

    def test_real_stop_failure_on_a_loaded_unit_still_raises(self) -> None:
        backend = self._Scripted(
            self._responses(stop=(1, "", "Job for unit canceled."), load_state="loaded")
        )
        with self.assertRaisesRegex(bridge.BridgeError, "systemctl stop codex-telegram-poller.service failed"):
            self._run_actions(backend, "stop-codex-poller")

    def test_successful_stop_does_not_query_load_state(self) -> None:
        backend = self._Scripted(self._responses(stop=(0, "", ""), load_state="loaded"))
        self._run_actions(backend, "stop-codex-poller")
        self.assertEqual(backend.calls, [("systemctl", "--user", "stop", self.UNIT)])


class PluginGroupShapeTests(unittest.TestCase):
    """T-4461: the Claude receiver "present" proof must accept both Telegram plugin shapes.

    Upstream's `.mcp.json` runs `bun run --cwd <plugin> --shell=bun --silent start`, which forks a
    `bun server.ts` child (two processes). Recovery's (T-2624, absolute bun) runs
    `<abs bun> run --cwd <plugin> --silent server.ts`: that one process IS the server. Before
    T-4461 the proof demanded the child, so every recovery receiver-state retried "plugin group is not
    present" until its 90s deadline. Pure evaluator calls, so no BridgeCase codex-UID skip.
    """

    CLAUDE_PID = 1400118
    LAUNCHER_PID = 1400120

    def setUp(self) -> None:
        self.binding = Manifest.load(REPO_ROOT / "instance.yaml").codex_telegram_binding("recovery")
        home = self.binding.account_home
        plugin = f"{home}/.claude/plugins/cache/claude-plugins-official/telegram/0.0.7"
        self.claude = bridge.ProcessSnapshot(
            self.CLAUDE_PID,
            1,
            f"{home}/.npm-global/bin/claude --channels plugin:telegram@claude-plugins-official --model claude-opus-5-5",
        )
        self.direct = bridge.ProcessSnapshot(self.LAUNCHER_PID, self.CLAUDE_PID, f"{home}/.bun/bin/bun run --cwd {plugin} --silent server.ts")
        self.wrapper = bridge.ProcessSnapshot(self.LAUNCHER_PID, self.CLAUDE_PID, f"{home}/.bun/bin/bun run --cwd {plugin} --shell=bun --silent start")
        self.server_child = bridge.ProcessSnapshot(1400121, self.LAUNCHER_PID, "bun server.ts")

    def _runtime_owned(self, *plugin: bridge.ProcessSnapshot) -> Mapping[str, str]:
        return bridge._evaluate_runtime_owned_claude_state(
            self.binding,
            expected="present",
            unit_probe=bridge.UnitProbe(self.binding.claude_receiver_unit, "inactive", "dead", "0"),
            unit_enabled="disabled",
            tmux=bridge.TmuxPaneSnapshot(True, False, self.CLAUDE_PID, "claude"),
            processes=(self.claude,) + plugin,
        )

    def _systemd_user(self, *plugin: bridge.ProcessSnapshot) -> Mapping[str, str]:
        unit = self.binding.claude_receiver_unit
        cgroup = f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}\n"
        tmux_server = bridge.ProcessSnapshot(1400110, 1, "tmux new-session -s claude-telegram-bot-fallback")
        claude = dataclasses.replace(self.claude, ppid=tmux_server.pid)
        return bridge._evaluate_systemd_user_claude_state(
            self.binding,
            expected="present",
            unit_probe=bridge.UnitProbe(unit, "active", "running", "1400100"),
            tmux=bridge.TmuxPaneSnapshot(True, False, self.CLAUDE_PID, "claude", tmux_server.pid),
            processes=(bridge.ProcessSnapshot(1400100, 1, "unit main"), tmux_server, claude) + plugin,
            unit_main_cgroup=cgroup,
            tmux_server_cgroup=cgroup,
        )

    def test_direct_spawn_launcher_without_server_child_is_a_complete_group(self) -> None:
        for evaluate in (self._runtime_owned, self._systemd_user):
            with self.subTest(evaluate=evaluate.__name__):
                evidence = evaluate(self.direct)
                self.assertEqual(evidence["plugin_launcher_descendant_count"], "1")
                self.assertEqual(evidence["plugin_server_descendant_count"], "0")
                self.assertEqual(evidence["plugin_group_shape"], "direct-spawn")

    def test_wrapper_launcher_still_needs_its_server_child(self) -> None:
        for evaluate in (self._runtime_owned, self._systemd_user):
            with self.subTest(evaluate=evaluate.__name__):
                self.assertEqual(evaluate(self.wrapper, self.server_child)["plugin_group_shape"], "wrapper")
                with self.assertRaisesRegex(bridge.BridgeError, "plugin group is not present"):
                    evaluate(self.wrapper)

    def test_mixed_or_duplicate_shapes_are_refused(self) -> None:
        second_direct = dataclasses.replace(self.direct, pid=1400122)
        for evaluate in (self._runtime_owned, self._systemd_user):
            with self.subTest(evaluate=evaluate.__name__):
                # A server child under a direct-spawn launcher is a second server.
                with self.assertRaisesRegex(bridge.BridgeError, "plugin group is duplicate"):
                    evaluate(self.direct, self.server_child)
                with self.assertRaisesRegex(bridge.BridgeError, "plugin group is duplicate"):
                    evaluate(self.direct, second_direct)

    def test_shell_box_wrapper_is_not_a_second_launcher(self) -> None:
        # T-5481: recovery 2026-10-25, claude -> bash pantheon-shell-box.sh <bun ...> -> bun
        box = f"/bin/bash {self.binding.account_home}/pantheon-config/tools/oom/pantheon-shell-box.sh"
        for name, launcher, children in (
            ("direct-spawn", self.direct, ()),
            ("wrapper", self.wrapper, (self.server_child,)),
        ):
            wrapper_proc = bridge.ProcessSnapshot(1400119, self.CLAUDE_PID, f"{box} {launcher.args}")
            boxed = dataclasses.replace(launcher, ppid=wrapper_proc.pid)
            for evaluate in (self._runtime_owned, self._systemd_user):
                with self.subTest(shape=name, evaluate=evaluate.__name__):
                    evidence = evaluate(wrapper_proc, boxed, *children)
                    self.assertEqual(evidence["plugin_group_shape"], name)
                    self.assertEqual(evidence["plugin_launcher_descendant_count"], "1")
        # a genuinely duplicated boxed launcher is still refused
        wrapper_proc = bridge.ProcessSnapshot(1400119, self.CLAUDE_PID, f"{box} {self.direct.args}")
        boxed = dataclasses.replace(self.direct, ppid=wrapper_proc.pid)
        with self.assertRaisesRegex(bridge.BridgeError, "plugin group is duplicate"):
            self._runtime_owned(wrapper_proc, boxed, dataclasses.replace(self.direct, pid=1400122))

    def test_server_ts_must_be_a_whole_argv_token(self) -> None:
        lookalike = dataclasses.replace(self.direct, args=self.direct.args + ".bak")
        with self.assertRaisesRegex(bridge.BridgeError, "plugin group is not present"):
            self._runtime_owned(lookalike)


#: T-4790 (T-4750 C4b-3, T-2950 Q2): the approved 13-path bridge manifest at the
#: pantheon-config harness/codex pin 0f4d81d99 (adds persona_orientation.py).
#: manifest_sha256 = sha256 of the canonical JSON of the 13 per-file sha256s.
APPROVED_BRIDGE_MANIFEST_SHA256 = "e21ea6f9ca3e49d0d71a01c93eef3c0b79ecae86b1272a391fb09fe8d422e06a"
#: T-4224's go-live record of the previous 12-path bridge manifest (codex-config
#: pin 1f84f64, subdir ""). Kept so the rollback pin stays provably reproducible.
HISTORICAL_BRIDGE_MANIFEST_SHA256 = "81c4dc06be31cf2840f14138c12550d8de3142a5923b0bf081cd5b51288a7b51"
HISTORICAL_BRIDGE_REVISION = "1f84f64382f75f86bd4064d62e7dffd48794a687"
HISTORICAL_BRIDGE_SHA256 = {
    "codex_telegram/__init__.py": "1471d4fbf3b6e95c5a796d8ec67afaecb4c99a3077836961b2b77c659573eb13",
    "codex_telegram/active_step.py": "e27c2c2dc4e72b9d9a825aa6de6580a684aecfe29f0e26afc8c2a49f66730304",
    "codex_telegram/allowlist.py": "ca802c955f258b781f1d23fbc73183e3c70077c1b231338e99182af1be6a472f",
    "codex_telegram/codex_hooks.py": "7f03f96e49bcc2245c7d99eb756dab252ad07088fb419fe977b09405b71389ba",
    "codex_telegram/config.py": "f5f44bb64379129646ec99c38a033a36c243730f72e56efa9fda02f1b64d2bdd",
    "codex_telegram/keepgoing.py": "239e3dad1cd001a3a2465db894ad46a976bec2694647ee7f162f6681155c870e",
    "codex_telegram/poller.py": "533484213902c12a3af1569572c8dafb84529d5b82017ecb40873da956e93078",
    "codex_telegram/server.py": "02bda3340d5dc4e0d64bd20b2514504188b6139ff3298ed3911ec1bdefdee38b",
    "codex_telegram/standing_instructions.py": "de1ce09fa39cd795c295ddc3168c8876885453f4e7cbe1b1aaaf76c6a29ec2f6",
    "codex_telegram/submit_verify.py": "1f913de5d9c6eef8967f033642abb12664ba617e47a27556e0b445b5775be9d0",
    "run_poller.py": "4802329fad20ee1a30ecfd729803244ac858365b4ba6dce898e8dd64c0fd12ef",
    "run_server.py": "2c21a2d2a4df17e2704bddbc2eefa1ac453216d30be657c961be362f8affea74",
}
#: The modules the poller and server pull in at import time (T-4790 Q2 condition b).
BRIDGE_IMPORT_SMOKE_MODULES = (
    "codex_telegram.poller",
    "codex_telegram.server",
    "codex_telegram.codex_hooks",
    "codex_telegram.keepgoing",
    "codex_telegram.persona_orientation",
    "codex_telegram.standing_instructions",
)


class PantheonConfigSourceRegressionTests(unittest.TestCase):
    """T-4790 (T-4750 C4b-3): the bridge archive is pinned to pantheon-config's own
    harness/codex tree (subdir harness/codex) at 0f4d81d99, 13 paths. T-4770 (C4b-1)
    first moved the build locator onto the pantheon-config clone at the old pin.

    Deliberately NOT under BridgeCase's /home/codex reachability guard: it reads
    only THIS repo's history (codex-config's commits arrived with the T-4755
    import), so it runs on every seat and in CI (ci.yml checks out with
    fetch-depth 0). A shallow clone fails loudly rather than skipping."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _instance_source_block(self) -> Mapping:
        document = yaml.safe_load((REPO_ROOT / "instance.yaml").read_text(encoding="utf-8"))
        return document["persona_installer"]["codex_telegram_bridge"]["source"]

    def test_archive_from_this_repo_at_pin_reproduces_approved_manifest(self) -> None:
        raw = self._instance_source_block()
        self.assertEqual(raw["repo"], "pantheon-config")
        self.assertEqual(raw["source_root"], "/home/codex/pantheon-config")
        self.assertEqual(raw["subdir"], "harness/codex")
        self.assertEqual(raw["revision"], "0f4d81d99b06837efbc0e836264f0bd85617e3b9")
        self.assertEqual(len(raw["paths"]), 13)
        self.assertIn("codex_telegram/persona_orientation.py", raw["paths"])

        source = Manifest.load(REPO_ROOT / "instance.yaml").codex_telegram_source()
        self.assertEqual(source.repo, "pantheon-config")
        self.assertEqual(source.subdir, "harness/codex")
        archive_path = self.root / "bridge.tar"
        receipt = bridge.create_verified_source_archive(REPO_ROOT, archive_path, source)

        self.assertEqual(receipt.revision, "0f4d81d99b06837efbc0e836264f0bd85617e3b9")
        self.assertEqual(receipt.manifest_sha256, APPROVED_BRIDGE_MANIFEST_SHA256)
        self.assertEqual(dict(receipt.file_sha256), dict(raw["sha256"]))
        with tarfile.open(archive_path, "r") as archive:
            members = [member for member in archive.getmembers() if member.isfile()]
            actual = {
                member.name: hashlib.sha256(archive.extractfile(member).read()).hexdigest()
                for member in members
            }
        # Members are the unprefixed deploy paths, never harness/codex/...
        self.assertEqual(sorted(actual), sorted(raw["paths"]))
        for relative, digest in raw["sha256"].items():
            self.assertEqual(actual[relative], digest, relative)
        # The source-check receipt (no archive) agrees with the archive build.
        check = bridge.source_check_receipt(source, source_repo=REPO_ROOT)
        self.assertEqual(check["manifest_sha256"], APPROVED_BRIDGE_MANIFEST_SHA256)

    def test_unchanged_paths_are_byte_identical_to_the_historical_pin(self) -> None:
        """T-2950 Q2 condition (a): only codex_hooks, keepgoing and
        standing_instructions changed vs 1f84f64; persona_orientation is new."""
        changed = {
            "codex_telegram/codex_hooks.py",
            "codex_telegram/keepgoing.py",
            "codex_telegram/standing_instructions.py",
        }
        for relative, digest in bridge.CODEX_TELEGRAM_SOURCE_SHA256.items():
            with self.subTest(relative=relative):
                if relative == "codex_telegram/persona_orientation.py":
                    self.assertNotIn(relative, HISTORICAL_BRIDGE_SHA256)
                elif relative in changed:
                    self.assertNotEqual(digest, HISTORICAL_BRIDGE_SHA256[relative])
                else:
                    self.assertEqual(digest, HISTORICAL_BRIDGE_SHA256[relative])

    def test_historical_pin_still_reproduces_its_manifest(self) -> None:
        """Rollback reproducibility: 1f84f64, subdir "", 12 paths still builds 81c4dc06."""
        historical = bridge.CodexTelegramSource(
            "pantheon-config",
            HISTORICAL_BRIDGE_REVISION,
            tuple(HISTORICAL_BRIDGE_SHA256),
            dict(HISTORICAL_BRIDGE_SHA256),
            subdir="",
        )
        receipt = bridge.create_verified_source_archive(REPO_ROOT, self.root / "historical.tar", historical)
        self.assertEqual(receipt.manifest_sha256, HISTORICAL_BRIDGE_MANIFEST_SHA256)
        self.assertEqual(dict(receipt.file_sha256), HISTORICAL_BRIDGE_SHA256)

    def test_extracted_archive_imports_the_bridge_modules(self) -> None:
        """T-2950 Q2 condition (b): the 13-path archive is import-complete. Built
        from this repo, extracted into an empty dir, imported under ``python3 -I``
        with only that dir prepended to sys.path (no network, no /home/codex)."""
        source = Manifest.load(REPO_ROOT / "instance.yaml").codex_telegram_source()
        archive_path = self.root / "bridge.tar"
        bridge.create_verified_source_archive(REPO_ROOT, archive_path, source)
        extracted = self.root / "extracted"
        extracted.mkdir()
        with tarfile.open(archive_path, "r") as archive:
            archive.extractall(extracted, filter="data")
        self.assertEqual(
            sorted(str(path.relative_to(extracted)) for path in extracted.rglob("*") if path.is_file()),
            sorted(bridge.CODEX_TELEGRAM_SOURCE_PATHS),
        )
        script = (
            "import importlib, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "for name in sys.argv[2:]:\n"
            "    module = importlib.import_module(name)\n"
            "    assert module.__file__.startswith(sys.argv[1]), (name, module.__file__)\n"
            "print('ok')\n"
        )
        result = subprocess.run(
            (sys.executable, "-I", "-c", script, str(extracted), *BRIDGE_IMPORT_SMOKE_MODULES),
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_parse_bridge_block_requires_pantheon_config_repo(self) -> None:
        raw = _manifest()["persona_installer"]["codex_telegram_bridge"]
        source, _ = bridge.parse_bridge_block(raw)
        self.assertEqual(source.repo, "pantheon-config")
        self.assertEqual(source.subdir, "harness/codex")
        stale = json.loads(json.dumps(raw))
        stale["source"]["repo"] = "codex-config"
        with self.assertRaisesRegex(bridge.BridgeError, "must be pantheon-config"):
            bridge.parse_bridge_block(stale)
        for subdir in ("", None, "harness"):
            unpinned = json.loads(json.dumps(raw))
            if subdir is None:
                del unpinned["source"]["subdir"]
            else:
                unpinned["source"]["subdir"] = subdir
            with self.subTest(subdir=subdir), self.assertRaisesRegex(bridge.BridgeError, "subdir does not match"):
                bridge.parse_bridge_block(unpinned)

    def test_parse_source_subdir_rejects_unclean_paths(self) -> None:
        self.assertEqual(bridge.parse_source_subdir(None), "")
        self.assertEqual(bridge.parse_source_subdir(""), "")
        self.assertEqual(bridge.parse_source_subdir("harness/codex"), "harness/codex")
        for bad in ("/harness/codex", "harness/codex/", "harness//codex", "../codex", "harness/./codex", 3):
            with self.subTest(bad=bad), self.assertRaises(bridge.BridgeError):
                bridge.parse_source_subdir(bad)

    def test_source_repo_root_derives_from_binding(self) -> None:
        base = bridge.CodexTelegramSource(
            "pantheon-config",
            bridge.CODEX_TELEGRAM_SOURCE_REVISION,
            bridge.CODEX_TELEGRAM_SOURCE_PATHS,
            dict(bridge.CODEX_TELEGRAM_SOURCE_SHA256),
        )
        self.assertEqual(bridge.source_repo_root(base), Path("/home/codex/pantheon-config"))
        owned = dataclasses.replace(base, owner_account="consultant")
        self.assertEqual(bridge.source_repo_root(owned), Path("/home/consultant/pantheon-config"))
        rooted = dataclasses.replace(owned, source_root=Path("/srv/elsewhere"))
        self.assertEqual(bridge.source_repo_root(rooted), Path("/srv/elsewhere"))

    def test_subdir_reads_paths_under_prefix_with_unprefixed_members(self) -> None:
        """C4b-3 shape (subdir harness/codex), on a synthetic repo. The root-level
        decoys differ from the prefixed content, so reading the wrong tree fails."""
        repo = self.root / "repo"
        repo.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }

        def git(*args: str) -> str:
            return subprocess.run(
                ("git", "-C", str(repo), *args), check=True, capture_output=True, text=True, env=env
            ).stdout

        git("init", "-q")
        paths = ("codex_telegram/__init__.py", "run_poller.py")
        hashes = {}
        for relative in paths:
            prefixed = repo / "harness" / "codex" / relative
            prefixed.parent.mkdir(parents=True, exist_ok=True)
            content = f"prefixed {relative}\n".encode("utf-8")
            prefixed.write_bytes(content)
            hashes[relative] = hashlib.sha256(content).hexdigest()
            decoy = repo / relative
            decoy.parent.mkdir(parents=True, exist_ok=True)
            decoy.write_bytes(b"root decoy\n")
        git("add", "-A")
        git("-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "synthetic")
        revision = git("rev-parse", "HEAD").strip()

        source = bridge.CodexTelegramSource("pantheon-config", revision, paths, hashes, subdir="harness/codex")
        self.assertEqual(source.object_path("run_poller.py"), "harness/codex/run_poller.py")
        archive_path = self.root / "prefixed.tar"
        receipt = bridge.create_verified_source_archive(repo, archive_path, source)
        self.assertEqual(dict(receipt.file_sha256), hashes)
        with tarfile.open(archive_path, "r") as archive:
            self.assertEqual(sorted(member.name for member in archive.getmembers()), sorted(paths))
        check = bridge.source_check_receipt(source, source_repo=repo)
        self.assertEqual(check["manifest_sha256"], receipt.manifest_sha256)
        self.assertEqual(dict(bridge.compute_source_hashes(repo, source).file_sha256), hashes)

        # The same pin read at the commit root sees the decoys and fails closed.
        unprefixed = dataclasses.replace(source, subdir="")
        with self.assertRaisesRegex(bridge.BridgeError, "hash mismatch"):
            bridge.create_verified_source_archive(repo, self.root / "root.tar", unprefixed)


# --- T-4963: a '//'-prefixed path is refused as a typed BridgeError ---


def test_t4963_rooted_path_refuses_double_slash(tmp_path):
    import pytest

    # Before T-4963 a non-'/' root raised a bare ValueError from relative_to("/").
    with pytest.raises(bridge.BridgeError):
        bridge._rooted_path(tmp_path, Path("//x"))
    with pytest.raises(bridge.BridgeError):
        bridge._rooted_path(Path("/"), Path("/" + str(tmp_path)))
    assert bridge._rooted_path(tmp_path, Path("/x")) == tmp_path / "x"


if __name__ == "__main__":
    unittest.main()
