"""Tests for tools.bringup_acceptance.hook_emissions (T-2556 phase 6 / T-2562).

Every settings.json fixture wires the REAL, repo-checked-in tools/hooks/*.py
scripts (never the host-specific /home/claude/.claude/hooks/* deployed
copies), so this suite is hermetic against any clone/CI. Each prover's own
sandboxing (HOME + state-dir env overrides) is exercised for real -- the
scripts genuinely run as subprocesses; nothing here mocks the hook contract
itself, only where hook_emissions.py looks for the settings.json it reads.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Mapping, Sequence
from unittest.mock import patch

from tools.bringup_acceptance import hook_emissions as he
from tools.bringup_acceptance.hook_emissions import (
    BACKLOG_SURFACE_PORTABILITY_FINDING,
    SNAPSHOT_SETTINGS_NOT_VERSION_CONTROLLED_FINDING,
    HookEmissionsModule,
    _ACCOUNT_HOME_ENV,
    _basename_of,
    build_hook_emissions_modules,
)
from tools.bringup_acceptance.inventory import discover_hooks
from tools.bringup_acceptance.types import (
    HardStop,
    InventoryPathEntry,
    RunContext,
    TargetInventory,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ACCOUNT = "t2562-probe-account"  # deliberately not any real Pantheon account

# adaptive_finish_detect.py lives at tools/, not tools/hooks/ like every other
# hook -- ground-truth-verified against the real settings.json (D11); this is
# also the brief-vs-reality correction hook_emissions.py documents.
_TOOLS_ROOT_BASENAMES = {"adaptive_finish_detect.py"}

# snapshot-settings.sh is genuinely NOT version-controlled anywhere in
# pantheon-config (verified: no git history, no tools/hooks/ entry) -- unlike
# every sibling hook, it exists ONLY as a deployed file at the host-specific
# ~/.claude/hooks/snapshot-settings.sh (see hook_emissions.
# SNAPSHOT_SETTINGS_NOT_VERSION_CONTROLLED_FINDING for the full D11 note).
# Materializing its known-deployed content here (captured verbatim while
# reading the real file) is what keeps this suite hermetic without depending
# on that host-specific path.
_SNAPSHOT_SETTINGS_SCRIPT = """#!/usr/bin/env bash
# PreToolUse hook: when prime writes/edits a settings*.json under any .claude/, snapshot the existing file first.
# Stdin is the hook event JSON.
input=$(cat)
file=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty' 2>/dev/null)
case "$file" in
  */.claude/settings*.json|*/.claude/settings.local.json)
    if [ -f "$file" ]; then
      ts=$(date +%Y%m%d-%H%M%S)
      cp -p -- "$file" "$file.bak.$ts" 2>/dev/null || true
    fi
    ;;
esac
exit 0
"""


def _hook_path(basename: str, *, fixtures_root: Path) -> Path:
    if basename == "snapshot-settings.sh":
        path = fixtures_root / "_fixtures" / basename
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_SNAPSHOT_SETTINGS_SCRIPT, encoding="utf-8")
            path.chmod(0o755)
        return path
    root = _REPO_ROOT / "tools" if basename in _TOOLS_ROOT_BASENAMES else _REPO_ROOT / "tools" / "hooks"
    path = root / basename
    assert path.is_file(), f"fixture assumes {path} exists in this checkout"
    return path


def _command_for(basename: str, *, fixtures_root: Path) -> str:
    path = _hook_path(basename, fixtures_root=fixtures_root)
    return f"python3 {path}" if basename.endswith(".py") else str(path)


def _settings_json_text(hooks_by_event: Mapping[str, Sequence[str]]) -> str:
    hooks: dict[str, list[dict]] = {}
    for event, commands in hooks_by_event.items():
        hooks[event] = [{"hooks": [{"type": "command", "command": c}]} for c in commands]
    return json.dumps({"hooks": hooks})


def _stage_account_home(root: Path, hooks_by_event: Mapping[str, Sequence[str]]) -> None:
    settings_path = root / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(_settings_json_text(hooks_by_event), encoding="utf-8")


def _run_context(evidence_root: Path, *, run_id: str = "20260805T000000Z") -> RunContext:
    return RunContext(
        run_id=run_id,
        persona="t2562-probe-persona",
        account=_ACCOUNT,
        harness="claude",
        profile="interactive",
        hub_path=Path("/tmp/hub"),
        state_path=Path("/tmp/state"),
        evidence_root=evidence_root,
        baseline_ref="deadbeef" * 5,
    )


def _wired_entry(module_id: str, command: str) -> InventoryPathEntry:
    return InventoryPathEntry(path=Path(command), tier="optional", status="applies", module_id=module_id)


class BasenameExtractionTest(unittest.TestCase):
    def test_interpreter_prefixed_command(self) -> None:
        self.assertEqual(
            _basename_of("python3 /home/claude/pantheon-config/tools/hooks/mcp-list-guard.py"),
            "mcp-list-guard.py",
        )

    def test_bare_script_path(self) -> None:
        self.assertEqual(
            _basename_of("/home/claude/.claude/hooks/handoff-auto-read.sh"), "handoff-auto-read.sh"
        )

    def test_trailing_bare_word_args_are_not_mistaken_for_the_basename(self) -> None:
        self.assertEqual(
            _basename_of('node "/home/claude/GitHub/Monitor/scripts/hook-handler.js" PreToolUse'),
            "hook-handler.js",
        )


class DeclareTest(unittest.TestCase):
    def test_no_wired_entries_does_not_apply(self) -> None:
        module = HookEmissionsModule(event="PreToolUse", module_id="hook-pre-tool-use")
        target = TargetInventory(persona="p", entries=())
        declaration = module.declare(target)
        self.assertFalse(declaration.applies)
        self.assertEqual(declaration.tier, "optional")

    def test_wired_entries_apply(self) -> None:
        module = HookEmissionsModule(event="PreToolUse", module_id="hook-pre-tool-use")
        target = TargetInventory(
            persona="p",
            entries=(_wired_entry("hook-pre-tool-use", "python3 /x/worker-guard.py"),),
        )
        declaration = module.declare(target)
        self.assertTrue(declaration.applies)

    def test_entries_from_other_module_ids_are_ignored(self) -> None:
        module = HookEmissionsModule(event="Stop", module_id="hook-stop")
        target = TargetInventory(
            persona="p",
            entries=(_wired_entry("hook-pre-tool-use", "python3 /x/worker-guard.py"),),
        )
        self.assertFalse(module.declare(target).applies)


class ProvePerHookTest(unittest.TestCase):
    """One case per hook this module knows how to prove: stage a settings.json
    wiring ONLY that hook, prove() it, assert nonce-correlated emission, then
    restore() and assert the scratch tree is gone."""

    def _run_one(self, event: str, module_id: str, basename: str):
        with tempfile.TemporaryDirectory() as account_home, tempfile.TemporaryDirectory() as evidence_root:
            command = _command_for(basename, fixtures_root=Path(account_home))
            _stage_account_home(Path(account_home), {event: [command]})
            with patch.dict(os.environ, {_ACCOUNT_HOME_ENV: account_home}):
                module = HookEmissionsModule(event=event, module_id=module_id)
                evidence = module.prove(_run_context(Path(evidence_root)))
                scratch_root = module._scratch_root
                self.assertIsNotNone(scratch_root)
                self.assertTrue(scratch_root.exists())
                restore_evidence = module.restore(_run_context(Path(evidence_root)))
                self.assertTrue(restore_evidence.restored)
                self.assertFalse(scratch_root.exists())
        return evidence

    def test_mcp_list_guard(self) -> None:
        evidence = self._run_one("PreToolUse", "hook-pre-tool-use", "mcp-list-guard.py")
        self.assertEqual(evidence.status, "proven")
        hook = next(iter(evidence.detail["hooks"].values()))
        self.assertEqual(hook["proof"]["allow_case"], "silent")
        self.assertEqual(
            hook["proof"]["deny_case"]["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_recall_feedback(self) -> None:
        evidence = self._run_one("PreToolUse", "hook-pre-tool-use", "recall-feedback.py")
        hook = next(iter(evidence.detail["hooks"].values()))
        self.assertIn(hook["proof"]["nonce"], hook["proof"]["stdout"])

    def test_worker_guard(self) -> None:
        evidence = self._run_one("PreToolUse", "hook-pre-tool-use", "worker-guard.py")
        hook = next(iter(evidence.detail["hooks"].values()))
        self.assertEqual(
            hook["proof"]["deny_case"]["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        self.assertEqual(hook["proof"]["allow_case"], "silent")

    def test_background_pipe_gate(self) -> None:
        evidence = self._run_one("PreToolUse", "hook-pre-tool-use", "background-pipe-gate.py")
        hook = next(iter(evidence.detail["hooks"].values()))
        self.assertEqual(
            hook["proof"]["deny_case"]["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_snapshot_settings(self) -> None:
        # The prover itself already asserts backup-bytes == original bytes
        # (raising HardStop on mismatch) before returning; by the time we get
        # a "proven" evidence back that byte-check has already passed. Restore
        # deletes the scratch tree (incl. the backup file) before this
        # assertion runs, so this checks the path SHAPE, not a re-read.
        evidence = self._run_one("PreToolUse", "hook-pre-tool-use", "snapshot-settings.sh")
        hook = next(iter(evidence.detail["hooks"].values()))
        backup_path = Path(hook["proof"]["backup"])
        self.assertTrue(backup_path.name.startswith("settings.json.bak."))
        self.assertEqual(
            hook["proof"]["finding"], SNAPSHOT_SETTINGS_NOT_VERSION_CONTROLLED_FINDING
        )

    def test_telegram_reply_gate(self) -> None:
        evidence = self._run_one("Stop", "hook-stop", "telegram-reply-gate.py")
        hook = next(iter(evidence.detail["hooks"].values()))
        self.assertEqual(hook["proof"]["payload"]["decision"], "block")

    def test_ho_stop_failure(self) -> None:
        evidence = self._run_one("StopFailure", "hook-stop-failure", "ho_stop_failure.py")
        hook = next(iter(evidence.detail["hooks"].values()))
        self.assertEqual(hook["proof"]["state"]["session_id"], hook["proof"]["nonce_session"])

    def test_adaptive_finish_detect(self) -> None:
        evidence = self._run_one("SubagentStop", "hook-subagent-stop", "adaptive_finish_detect.py")
        hook = next(iter(evidence.detail["hooks"].values()))
        self.assertTrue(
            any(hook["proof"]["nonce"] in line.get("message_excerpt", "") for line in hook["proof"]["log_lines"])
        )

    def test_handoff_auto_read(self) -> None:
        evidence = self._run_one("SessionStart", "hook-session-start", "handoff-auto-read.sh")
        hook = next(iter(evidence.detail["hooks"].values()))
        self.assertIn("nonce", hook["proof"])

    def test_verify_hook_integrity(self) -> None:
        evidence = self._run_one("SessionStart", "hook-session-start", "verify-hook-integrity.sh")
        self.assertEqual(evidence.status, "proven")

    def test_ensure_hooks_active(self) -> None:
        evidence = self._run_one("SessionStart", "hook-session-start", "ensure-hooks-active.sh")
        hook = next(iter(evidence.detail["hooks"].values()))
        self.assertTrue(hook["proof"]["repo"].endswith("probe-repo"))

    def test_backlog_surface(self) -> None:
        evidence = self._run_one("SessionStart", "hook-session-start", "backlog-surface.sh")
        hook = next(iter(evidence.detail["hooks"].values()))
        self.assertTrue(hook["proof"]["contract_verified"])
        self.assertFalse(hook["proof"]["forced_branch"])
        self.assertEqual(hook["proof"]["portability_finding"], BACKLOG_SURFACE_PORTABILITY_FINDING)


class McpListGuardProverRobustnessTest(unittest.TestCase):
    """T-4412: the mcp-list-guard prover's live-poller stand-in must be OBSERVABLE
    before the hook is run against it, and an empty emission must explain itself.

    Root cause this pins: ``subprocess.Popen`` returns when the child's exec has
    closed the errpipe, which the kernel does BEFORE it publishes the new argv
    (``mm->arg_start/arg_end``). Until then ``/proc/<pid>/cmdline`` reads EMPTY --
    a window measured at up to ~37 ms under a pinned-CPU load on the fleet VPS,
    and stretched by exec-time page-ins on a disk-bound CI host. The hook's
    ``own_poller_live()`` greps that cmdline for ``bun`` + ``server.ts``; on the
    empty window it returns False (by design: fail-open) and the hook silently
    ALLOWS, so the prover saw rc 0 / empty stdout where it expected a deny.
    """

    _HOOK_CMD = f"python3 {_REPO_ROOT / 'tools' / 'hooks' / 'mcp-list-guard.py'}"

    def _prove(self, command: str):
        with tempfile.TemporaryDirectory() as scratch:
            return he._prove_mcp_list_guard(command, Path(scratch))

    def test_read_proc_cmdline_reads_a_live_process_and_is_none_for_a_reaped_one(self) -> None:
        own = he._read_proc_cmdline(os.getpid())
        self.assertIsInstance(own, str)
        self.assertTrue(own)
        self.assertNotIn("\0", own)  # NULs folded to spaces, like own_poller_live()
        done = subprocess.Popen(["true"])
        done.wait()
        self.assertIsNone(he._read_proc_cmdline(done.pid))

    def test_empty_emission_hardstop_carries_diagnostics(self) -> None:
        # A "hook" that always allows silently (empty stdout, rc 0) but leaves a
        # marker on stderr -- the exact CI symptom. The HardStop must explain
        # itself: hook stderr + rc, the stand-in's pid / cmdline / status as read
        # at that moment, and whether bot.pid existed.
        command = f"{sys.executable} -c \"import sys; sys.stderr.write('STDERR-MARKER-4412')\""
        with self.assertRaises(HardStop) as ctx:
            self._prove(command)
        message = str(ctx.exception)
        for needle in (
            "no parseable emission",
            "STDERR-MARKER-4412",
            "hook_rc=0",
            "bun_probe_server.ts",  # the stand-in's /proc cmdline as read
            "State:",  # the stand-in's /proc status as read
            "bot.pid_exists=True",
            "pid=",
        ):
            self.assertIn(needle, message)

    def test_nonzero_exit_hardstop_carries_diagnostics_too(self) -> None:
        command = (
            f"{sys.executable} -c \"import os, sys; sys.stderr.write('E-MARKER-4412'); "
            "sys.exit(3 if os.path.exists(os.path.expanduser('~/.claude/channels/telegram/bot.pid')) else 0)\""
        )
        with self.assertRaises(HardStop) as ctx:
            self._prove(command)
        message = str(ctx.exception)
        self.assertIn("exit 3", message)
        self.assertIn("E-MARKER-4412", message)
        self.assertIn("bun_probe_server.ts", message)

    def test_hook_only_runs_after_stand_in_cmdline_is_visible(self) -> None:
        real_read = he._read_proc_cmdline
        reads: list[int] = []

        # The kernel window (Popen returned, argv not yet published) reads empty or
        # unreadable; a PARTIAL view (one marker of two) must not count as visible either.
        window = ["", "bun_probe_server", None]

        def flaky_read(pid: int):
            reads.append(pid)
            if len(reads) <= len(window):
                return window[len(reads) - 1]
            return real_read(pid)

        reads_at_hook_call: list[int] = []
        real_run = he._run_hook

        def spy_run(command, **kwargs):
            reads_at_hook_call.append(len(reads))
            return real_run(command, **kwargs)

        with patch.object(he, "_read_proc_cmdline", flaky_read), patch.object(he, "_run_hook", spy_run):
            proof = self._prove(self._HOOK_CMD)
        self.assertEqual(reads_at_hook_call[0], 0)  # case A: no stand-in exists yet
        self.assertGreaterEqual(reads_at_hook_call[1], 4)  # case B waited out the whole window
        self.assertEqual(proof["deny_case"]["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_stand_in_never_visible_is_a_bounded_hardstop_and_the_hook_never_runs(self) -> None:
        spawned: list[subprocess.Popen] = []
        real_popen = subprocess.Popen

        def spy_popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            spawned.append(proc)
            return proc

        hook_calls: list[str] = []
        real_run = he._run_hook

        def spy_run(command, **kwargs):
            hook_calls.append(command)
            return real_run(command, **kwargs)

        with (
            patch.object(he, "_read_proc_cmdline", lambda pid: ""),
            patch.object(he, "_STAND_IN_READY_TIMEOUT_S", 0.3),
            patch.object(he, "_STAND_IN_POLL_INTERVAL_S", 0.01),
            patch.object(he, "_run_hook", spy_run),
            patch.object(he.subprocess, "Popen", spy_popen),
        ):
            started = time.monotonic()
            with self.assertRaises(HardStop) as ctx:
                self._prove(self._HOOK_CMD)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0)
        self.assertEqual(len(hook_calls), 1)  # case A only; the hook never saw the invisible stand-in
        self.assertIn("never showed", str(ctx.exception))
        stand_in = next(p for p in spawned if p.args[0] == "bun_probe_server.ts")
        self.assertIsNotNone(stand_in.poll())  # stand-in reaped on the failure path

    def test_stand_in_that_dies_before_becoming_visible_fails_fast(self) -> None:
        real_popen = subprocess.Popen

        def dying_popen(*args, **kwargs):
            if kwargs.get("args", [None])[0] == "bun_probe_server.ts":
                return real_popen(args=["true"], executable="/bin/true")
            return real_popen(*args, **kwargs)

        with (
            patch.object(he, "_STAND_IN_READY_TIMEOUT_S", 8.0),
            patch.object(he.subprocess, "Popen", dying_popen),
        ):
            started = time.monotonic()
            with self.assertRaises(HardStop) as ctx:
                self._prove(self._HOOK_CMD)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 4.0)  # noticed the exit; did not sit out the deadline
        self.assertIn("exited", str(ctx.exception))


class OutOfScopeClassificationTest(unittest.TestCase):
    def test_hook_handler_js_is_named_out_of_scope_not_a_gap(self) -> None:
        with tempfile.TemporaryDirectory() as account_home, tempfile.TemporaryDirectory() as evidence_root:
            third_party = 'node "/home/claude/GitHub/Monitor/scripts/hook-handler.js" Stop'
            reply_gate = _command_for("telegram-reply-gate.py", fixtures_root=Path(account_home))
            _stage_account_home(Path(account_home), {"Stop": [third_party, reply_gate]})
            with patch.dict(os.environ, {_ACCOUNT_HOME_ENV: account_home}):
                module = HookEmissionsModule(event="Stop", module_id="hook-stop")
                evidence = module.prove(_run_context(Path(evidence_root)))
                module.restore(_run_context(Path(evidence_root)))
        self.assertEqual(evidence.status, "proven")
        statuses = {detail["basename"]: detail["status"] for detail in evidence.detail["hooks"].values()}
        self.assertEqual(statuses["hook-handler.js"], "out_of_scope")
        self.assertEqual(statuses["telegram-reply-gate.py"], "proven")


class PerHookHardStopNotRollupTest(unittest.TestCase):
    def test_one_unrecognized_wired_hook_hard_stops_naming_itself_not_the_event(self) -> None:
        with tempfile.TemporaryDirectory() as account_home, tempfile.TemporaryDirectory() as evidence_root:
            worker_guard = _command_for("worker-guard.py", fixtures_root=Path(account_home))
            _stage_account_home(
                Path(account_home),
                {"PreToolUse": [worker_guard, "python3 /nonexistent/mystery-hook.py"]},
            )
            with patch.dict(os.environ, {_ACCOUNT_HOME_ENV: account_home}):
                module = HookEmissionsModule(event="PreToolUse", module_id="hook-pre-tool-use")
                with self.assertRaisesRegex(HardStop, "mystery-hook.py"):
                    module.prove(_run_context(Path(evidence_root)))
                module.restore(_run_context(Path(evidence_root)))

    def test_a_registered_hook_that_stops_emitting_hard_stops_naming_itself(self) -> None:
        # worker-guard.py wired at a path that does not exist -> the basename
        # still resolves and dispatches to its registered prover, but the
        # subprocess itself fails, which must surface as a named HardStop.
        with tempfile.TemporaryDirectory() as account_home, tempfile.TemporaryDirectory() as evidence_root:
            broken_command = "python3 /nonexistent/path/worker-guard.py"
            _stage_account_home(Path(account_home), {"PreToolUse": [broken_command]})
            with patch.dict(os.environ, {_ACCOUNT_HOME_ENV: account_home}):
                module = HookEmissionsModule(event="PreToolUse", module_id="hook-pre-tool-use")
                with self.assertRaisesRegex(HardStop, "worker-guard.py"):
                    module.prove(_run_context(Path(evidence_root)))
                module.restore(_run_context(Path(evidence_root)))


class NoHooksWiredIsAGapNotASilentPassTest(unittest.TestCase):
    def test_prove_called_despite_nothing_wired_is_a_gap(self) -> None:
        with tempfile.TemporaryDirectory() as account_home, tempfile.TemporaryDirectory() as evidence_root:
            _stage_account_home(Path(account_home), {})
            with patch.dict(os.environ, {_ACCOUNT_HOME_ENV: account_home}):
                module = HookEmissionsModule(event="PreToolUse", module_id="hook-pre-tool-use")
                evidence = module.prove(_run_context(Path(evidence_root)))
                module.restore(_run_context(Path(evidence_root)))
        self.assertEqual(evidence.status, "gap")


class ReportTest(unittest.TestCase):
    def test_report_carries_status_and_evidence_refs(self) -> None:
        with tempfile.TemporaryDirectory() as account_home, tempfile.TemporaryDirectory() as evidence_root:
            worker_guard = _command_for("worker-guard.py", fixtures_root=Path(account_home))
            _stage_account_home(Path(account_home), {"PreToolUse": [worker_guard]})
            with patch.dict(os.environ, {_ACCOUNT_HOME_ENV: account_home}):
                module = HookEmissionsModule(event="PreToolUse", module_id="hook-pre-tool-use")
                evidence = module.prove(_run_context(Path(evidence_root)))
                module.restore(_run_context(Path(evidence_root)))
        row = module.report(evidence)
        self.assertEqual(row.module_id, "hook-pre-tool-use")
        self.assertEqual(row.status, "proven")
        self.assertEqual(row.evidence_refs, evidence.evidence_paths)
        self.assertTrue(row.evidence_refs)


class BuildHookEmissionsModulesTest(unittest.TestCase):
    def test_returns_one_module_per_landed_event_module_id(self) -> None:
        modules = build_hook_emissions_modules()
        self.assertEqual(
            {module.id for module in modules},
            {
                "hook-session-start",
                "hook-pre-tool-use",
                "hook-stop",
                "hook-stop-failure",
                "hook-subagent-stop",
            },
        )
        self.assertTrue(all(module.tier == "optional" for module in modules))


class PathScopingSafetyTest(unittest.TestCase):
    """No real /home/claude path is ever read-mutated by a prove()+restore()
    cycle -- the guarantee every other channel's sandboxing rests on. Snapshots
    every REAL sensitive path this module's provers are capable of touching
    (per each hook's own source) before and after a full multi-hook cycle run
    against a synthetic (non-real) account_home, and asserts byte/listing
    identity throughout. Per orch design-review, relayed by team-lead."""

    _REAL_SENSITIVE_PATHS = (
        Path.home() / ".claude" / "hooks" / ".recall_seen",
        Path.home() / ".claude" / "hooks" / ".telegram_reply_markers",
        Path.home() / ".local" / "state" / "pantheon" / "ho_limit_hit.json",
        Path.home() / ".local" / "state" / "pantheon" / "adaptive_finish_log.jsonl",
        Path.home() / ".claude" / "channels" / "telegram" / "bot.pid",
        Path.home() / "pantheon-config" / "BACKLOG.md",
    )

    def _snapshot(self) -> dict[str, object]:
        snapshot: dict[str, object] = {}
        for path in self._REAL_SENSITIVE_PATHS:
            if path.is_dir():
                snapshot[str(path)] = sorted(p.name for p in path.iterdir())
            elif path.is_file():
                snapshot[str(path)] = path.read_bytes()
            else:
                snapshot[str(path)] = None
        return snapshot

    def test_full_multi_hook_cycle_touches_no_real_account_path(self) -> None:
        before = self._snapshot()
        real_home_git_config = None
        pantheon_config = Path.home() / "pantheon-config"
        if (pantheon_config / ".git").exists():
            import subprocess

            real_home_git_config = subprocess.run(
                ["git", "-C", str(pantheon_config), "config", "--local", "core.hooksPath"],
                capture_output=True, text=True, timeout=10,
            ).stdout

        with tempfile.TemporaryDirectory() as account_home, tempfile.TemporaryDirectory() as evidence_root:
            fixtures_root = Path(account_home)

            def _cmd(basename: str) -> str:
                return _command_for(basename, fixtures_root=fixtures_root)

            _stage_account_home(
                Path(account_home),
                {
                    "PreToolUse": [
                        _cmd("mcp-list-guard.py"),
                        _cmd("recall-feedback.py"),
                        _cmd("worker-guard.py"),
                        _cmd("background-pipe-gate.py"),
                        _cmd("snapshot-settings.sh"),
                    ],
                    "Stop": [_cmd("telegram-reply-gate.py")],
                    "StopFailure": [_cmd("ho_stop_failure.py")],
                    "SubagentStop": [_cmd("adaptive_finish_detect.py")],
                    "SessionStart": [
                        _cmd("handoff-auto-read.sh"),
                        _cmd("verify-hook-integrity.sh"),
                        _cmd("ensure-hooks-active.sh"),
                        _cmd("backlog-surface.sh"),
                    ],
                },
            )
            with patch.dict(os.environ, {_ACCOUNT_HOME_ENV: account_home}):
                for module in build_hook_emissions_modules():
                    ctx = _run_context(Path(evidence_root))
                    evidence = module.prove(ctx)
                    self.assertEqual(evidence.status, "proven", module.id)
                    module.restore(ctx)

        after = self._snapshot()
        self.assertEqual(before, after)

        if real_home_git_config is not None:
            import subprocess

            post_git_config = subprocess.run(
                ["git", "-C", str(pantheon_config), "config", "--local", "core.hooksPath"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            self.assertEqual(real_home_git_config, post_git_config)


class DiscoverHooksIntegrationTest(unittest.TestCase):
    """Confirms this module reads settings.json through the SAME landed,
    pure inventory.discover_hooks() every other module uses -- no private
    re-implementation of the settings.json parsing contract."""

    def test_fixture_settings_round_trips_through_the_landed_parser(self) -> None:
        with tempfile.TemporaryDirectory() as fixtures_root:
            worker_guard = _command_for("worker-guard.py", fixtures_root=Path(fixtures_root))
        text = _settings_json_text({"PreToolUse": [worker_guard]})
        entries = discover_hooks(text, _ACCOUNT)
        pretooluse = [e for e in entries if e.module_id == "hook-pre-tool-use"]
        self.assertEqual(len(pretooluse), 1)
        self.assertEqual(pretooluse[0].status, "applies")


if __name__ == "__main__":
    unittest.main()
