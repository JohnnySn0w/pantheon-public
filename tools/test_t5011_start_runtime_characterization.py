"""T-5011 PR-A: characterization of ``runtime_controller._start_runtime``'s
pre-launch preparation for claude and codex.

Written BEFORE the refactor that moves codex's ``ensure_*`` config seeding and
claude's bypass-consent seeding out of ``_start_runtime`` (T-4924 plan §2.4),
and required to pass unchanged on both sides of it. It records, in one ordered
event list:

* every pre-launch seeding call (name, positional args, keyword args), and
* the tmux command the launch then runs (its full argv),

plus the stderr the claude path prints when consent cannot be ensured.

The recorders replace the seeding functions under every module name the
controller could resolve them through (``tools.runtime_controller``,
``tools.harness_adapter.codex``, ``tools.harness_adapter.claude``,
``tools.harness_adapter.integration``, ``tools.bypass_consent``), so the test
pins WHAT runs and in which order, not WHERE the call site lives. No real
config is written and no codex binary is run.
"""

from __future__ import annotations

import contextlib
import io
import os
import pwd
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from unittest import mock

from tools import bypass_consent as bypass_consent_module
from tools import runtime_controller as rc
from tools.harness_adapter import claude as claude_module
from tools.harness_adapter import codex as codex_module
from tools.harness_adapter import integration as integration_module
from tools.harness_adapter.claude import ClaudeHarnessAdapter
from tools.harness_adapter.codex import CodexHarnessAdapter
from tools.harness_adapter.types import (
    CommandSpec,
    FailoverPolicy,
    HarnessLaunchConfig,
    RuntimeControllerConfig,
    TmuxTarget,
)


_SEEDERS = (
    "ensure_project_trusted",
    "ensure_update_check_disabled",
    "ensure_model_migration_prompts_hidden",
    "ensure_worker_guard_hook",
    "ensure_bypass_consent",
)
_MODULES = (rc, codex_module, claude_module, integration_module, bypass_consent_module)

ACCOUNT_HOME = Path("/tmp/t5011-account")
LAUNCH_CWD = Path("/tmp/t5011-account/work dir")


def _binding(harness: str) -> SimpleNamespace:
    executable = Path("/usr/bin/codex" if harness == "codex" else "/usr/bin/claude")
    return SimpleNamespace(
        persona="recovery",
        role="recovery",
        account=pwd.getpwuid(os.geteuid()).pw_name,
        account_home=ACCOUNT_HOME,
        mailbox=ACCOUNT_HOME / "mail" / "recovery",
        launch_cwd=LAUNCH_CWD,
        selected_harness=harness,
        configured_harness=harness,
        communications=None,
        launch=HarnessLaunchConfig(
            harness=harness,
            profile="interactive",
            executable=executable,
            model="pinned-model",
            mode="interactive",
            meter="subscription",
            quota_source="app-server" if harness == "codex" else "statusline",
            native={},
            sources=("test",),
        ),
        controller=RuntimeControllerConfig(
            tmux=TmuxTarget("pantheon-recovery", "harness", "0"),
            policy=FailoverPolicy(5.0, ()),
        ),
    )


class _FakeAdapter:
    """The minimal adapter shape the controller's own tests use."""

    def __init__(self, binding: Any) -> None:
        self.binding = binding

    def build_interactive_launch(self, target: TmuxTarget, *, cwd: Path) -> CommandSpec:
        del target
        return CommandSpec(
            argv=(str(self.binding.launch.executable), "--model", "pinned-model"),
            stdin=None,
            cwd=cwd,
            env={"PANTHEON_T5011": "value with space"},
        )


def _real_adapter(binding: Any) -> Any:
    if binding.launch.harness == "codex":
        # codex_home deliberately NOT account_home/.codex: the seeding must
        # keep using binding.account_home/".codex" (as _start_runtime does).
        return CodexHarnessAdapter(
            binding.role,
            codex_home=Path("/tmp/t5011-elsewhere"),
            mailbox_path=binding.mailbox,
            launch=binding.launch,
        )
    return ClaudeHarnessAdapter(
        binding.role,
        claude_home=Path("/tmp/t5011-elsewhere"),
        mailbox_path=binding.mailbox,
        launch=binding.launch,
    )


@contextlib.contextmanager
def _recorded(*, consent: bool = True) -> Iterator[list[tuple[Any, ...]]]:
    events: list[tuple[Any, ...]] = []

    def recorder(name: str):
        def record(*args: Any, **kwargs: Any) -> bool:
            events.append(("seed", name, args, tuple(sorted(kwargs.items()))))
            return consent if name == "ensure_bypass_consent" else True

        return record

    def run_tmux(*args: Any, **kwargs: Any) -> Any:
        events.append(("tmux", args, tuple(sorted(kwargs))))
        return mock.Mock(returncode=0, stdout="", stderr="")

    with contextlib.ExitStack() as stack:
        for module in _MODULES:
            for name in _SEEDERS:
                if hasattr(module, name):
                    stack.enter_context(mock.patch.object(module, name, recorder(name)))
        stack.enter_context(mock.patch.object(rc, "_target_known", return_value=True))
        stack.enter_context(mock.patch.object(rc, "_session_exists", return_value=True))
        stack.enter_context(mock.patch.object(rc, "_default_tmux_server_pid", return_value=123))
        stack.enter_context(mock.patch.object(rc, "_pid_in_scope", return_value=True))
        stack.enter_context(mock.patch.object(rc, "_run_tmux", side_effect=run_tmux))
        yield events


def _expected_seeds(harness: str) -> list[tuple[Any, ...]]:
    codex_home = ACCOUNT_HOME / ".codex"
    if harness == "codex":
        return [
            ("seed", "ensure_project_trusted", (codex_home, LAUNCH_CWD), ()),
            ("seed", "ensure_update_check_disabled", (codex_home,), ()),
            ("seed", "ensure_model_migration_prompts_hidden", (codex_home,), ()),
            (
                "seed",
                "ensure_worker_guard_hook",
                (codex_home, ACCOUNT_HOME),
                (("executable", Path("/usr/bin/codex")),),
            ),
        ]
    return [("seed", "ensure_bypass_consent", (ACCOUNT_HOME / ".claude",), ())]


class StartRuntimeCharacterizationTests(unittest.TestCase):
    def _run(self, harness: str, adapter_kind: str, *, consent: bool = True):
        binding = _binding(harness)
        adapter = _FakeAdapter(binding) if adapter_kind == "fake" else _real_adapter(binding)
        stderr = io.StringIO()
        with _recorded(consent=consent) as events, contextlib.redirect_stderr(stderr):
            rc._start_runtime(binding, adapter, replace=False)
        return binding, adapter, events, stderr.getvalue()

    def test_seeding_calls_order_and_args(self) -> None:
        for harness in ("codex", "claude"):
            for adapter_kind in ("fake", "real"):
                with self.subTest(harness=harness, adapter=adapter_kind):
                    _, _, events, stderr = self._run(harness, adapter_kind)
                    seeds = [event for event in events if event[0] == "seed"]
                    self.assertEqual(seeds, _expected_seeds(harness))
                    self.assertEqual(stderr, "")

    def test_seeding_runs_before_the_tmux_launch(self) -> None:
        for harness in ("codex", "claude"):
            with self.subTest(harness=harness):
                _, _, events, _ = self._run(harness, "fake")
                kinds = [event[0] for event in events]
                self.assertEqual(kinds, ["seed"] * len(_expected_seeds(harness)) + ["tmux"])

    def test_launch_argv_is_pinned(self) -> None:
        """The exact respawn-pane argv, recorded on main before the refactor."""
        for harness in ("codex", "claude"):
            with self.subTest(harness=harness):
                _, _, events, _ = self._run(harness, "fake")
                tmux = [event for event in events if event[0] == "tmux"]
                self.assertEqual(
                    tmux,
                    [
                        (
                            "tmux",
                            (
                                "respawn-pane",
                                "-k",
                                "-t",
                                "pantheon-recovery:harness.0",
                                "-c",
                                "/tmp/t5011-account/work dir",
                                "/usr/bin/env",
                                "--unset=PANTHEON_INSTANCE_MANIFEST",
                                "--",
                                "PATH=/tmp/t5011-account/.bun/bin:/tmp/t5011-account/.npm-global/bin"
                                ":/usr/local/bin:/usr/bin:/bin",
                                "PANTHEON_T5011=value with space",
                                f"/usr/bin/{harness}",
                                "--model",
                                "pinned-model",
                            ),
                            (),
                        )
                    ],
                )

    def test_claude_consent_failure_prints_the_same_warning(self) -> None:
        _, _, events, stderr = self._run("claude", "fake", consent=False)
        self.assertEqual([event for event in events if event[0] == "seed"], _expected_seeds("claude"))
        self.assertEqual(
            stderr,
            f"[runtime_controller] bypass consent NOT ensured in {ACCOUNT_HOME / '.claude'}/settings.json; "
            "the seat may stall on the bypass-permissions warning dialog\n",
        )


if __name__ == "__main__":
    unittest.main()
