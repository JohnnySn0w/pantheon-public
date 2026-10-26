"""Tests for tools.bringup_acceptance.optionals.recovery (T-2556 phase 12 / T-2567)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.bringup_acceptance import registry
from tools.bringup_acceptance.optionals import recovery
from tools.bringup_acceptance.types import HardStop, RunContext, TargetInventory


def _ctx(*, run_id: str, persona: str = "recovery", evidence_root: Path) -> RunContext:
    return RunContext(
        run_id=run_id, persona=persona, account="claude-fallback", harness="test-harness",
        profile="test-profile", hub_path=evidence_root / "hub", state_path=evidence_root / "state",
        evidence_root=evidence_root / "evidence", baseline_ref="test-ref",
    )


class _TempEvidenceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="t2567-recovery-test-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.evidence_root = Path(self._tmp)

    def ctx(self, run_id: str, *, persona: str = "recovery") -> RunContext:
        return _ctx(run_id=run_id, persona=persona, evidence_root=self.evidence_root)


ANCHOR_SESSION = "t4774-anchor"


class _PrivateTmuxServerMixin:
    """T-4774: each test gets its OWN tmux server, kept alive by an anchor session.

    conftest's T-4433 fixture already gives every test process a private TMUX_TMPDIR,
    but that one server is shared by every test the process runs. RecoverySelfRestartModule
    kills its disposable session and then creates the next one straight away; when the
    killed session is the server's last, the server is exiting as the next client
    connects, which is a plausible source of the CI "server exited unexpectedly"
    HardStop. (Not reproduced locally: 0/300 kill-then-create cycles, idle and under
    confined_load.) Here the module's bare `tmux` calls, and self_restart.sh's
    has-session, inherit a per-test TMUX_TMPDIR, and the anchor session means that
    server never goes empty mid-test.

    The directory is a short /tmp mkdtemp, not a deep scratch path, for the same
    reason as conftest's: unix socket paths are limited to ~108 bytes. Cleanup only
    ever runs `tmux -S <this test's socket> kill-server`, so it can reach no other
    server."""

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        self.tmux_tmpdir = tempfile.mkdtemp(prefix="t4774-tmux-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmux_tmpdir, ignore_errors=True)  # type: ignore[attr-defined]
        self.tmux_socket = Path(self.tmux_tmpdir) / f"tmux-{os.getuid()}" / "default"
        env_patch = mock.patch.dict(os.environ, {"TMUX_TMPDIR": self.tmux_tmpdir})
        env_patch.start()
        self.addCleanup(env_patch.stop)  # type: ignore[attr-defined]
        os.environ.pop("TMUX", None)
        os.environ.pop("TMUX_PANE", None)
        self.tmux_env = dict(os.environ)
        # Registered before the anchor exists, so a half-started server is still torn down.
        self.addCleanup(  # type: ignore[attr-defined]
            subprocess.run, ["tmux", "-S", str(self.tmux_socket), "kill-server"],
            env=self.tmux_env, capture_output=True, timeout=10,
        )
        # The anchor resolves its socket through TMUX_TMPDIR, exactly as the module's
        # own calls do; the socket existing at the -S path proves cleanup targets it.
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", ANCHOR_SESSION, "sleep 600"],
            env=self.tmux_env, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise AssertionError(f"could not start the private anchor session: {result.stderr!r}")
        if not self.tmux_socket.is_socket():
            raise AssertionError(f"private tmux socket not at {self.tmux_socket}")

    def private_has_session(self, name: str) -> int:
        return subprocess.run(
            ["tmux", "-S", str(self.tmux_socket), "has-session", "-t", name],
            env=self.tmux_env, capture_output=True, timeout=10,
        ).returncode


BOOT_KICK_TEST_GRACE_S = 60


class _BootKickTestSeamsMixin:
    """T-4774: a test-only seam for RecoveryBootKickModule's recovery_boot_kick.sh runs.

    RUN_CASE_GRACE_S, the module's hang guard past each scenario's own sentinel
    timeout, is 10s in production (12s total for the 2s scenarios). CI under xdist
    load overran it (TimeoutExpired after 12s). Tests widen it to 60s, which still
    cuts off a real hang; the production value is unchanged.

    T-5075: the logger stub is no longer a test seam. The module itself points
    RECOVERY_KICK_LOGGER at its own scratch stub on every run, so tests drop any inherited
    value to prove the production path needs no help (see BootKickLoggerStubTest)."""

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        grace_patch = mock.patch.object(recovery.RecoveryBootKickModule, "RUN_CASE_GRACE_S", BOOT_KICK_TEST_GRACE_S)
        grace_patch.start()
        self.addCleanup(grace_patch.stop)  # type: ignore[attr-defined]
        env_patch = mock.patch.dict(os.environ, {})
        env_patch.start()
        self.addCleanup(env_patch.stop)  # type: ignore[attr-defined]
        os.environ.pop("RECOVERY_KICK_LOGGER", None)


def _make_recorder(path: Path, record: Path) -> Path:
    """An executable at ``path`` that appends its argv (one line per call) to ``record``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env bash\n" f'printf \'%s\\n\' "$*" >> "{record}"\n' "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# build_optional_modules() + real registry conformance
# ---------------------------------------------------------------------------


class BuildOptionalModulesTest(unittest.TestCase):
    def test_returns_three_uniquely_ided_optional_tier_modules(self) -> None:
        modules = recovery.build_optional_modules()
        self.assertEqual(len(modules), 3)
        ids = [m.id for m in modules]
        self.assertEqual(len(ids), len(set(ids)))
        for module in modules:
            self.assertEqual(module.tier, "optional")

    def test_real_registry_discovers_recovery_via_dotted_import(self) -> None:
        # Genuine integration point: registry.discover_optional_modules("recovery")
        # dotted-imports THIS FILE for real -- no sys.modules injection.
        modules = registry.discover_optional_modules("recovery")
        self.assertEqual(len(modules), 3)
        self.assertEqual(
            {m.id for m in modules},
            {"recovery-recovery-watchdog", "recovery-self-restart", "recovery-boot-kick"},
        )


# ---------------------------------------------------------------------------
# declare() -- persona self-declaration, shared shape across all three
# ---------------------------------------------------------------------------


class DeclareTest(unittest.TestCase):
    def test_all_three_modules_apply_for_recovery_persona(self) -> None:
        target = TargetInventory(persona="recovery", entries=())
        for module in recovery.build_optional_modules():
            declaration = module.declare(target)
            self.assertTrue(declaration.applies, module.id)
            self.assertEqual(declaration.tier, "optional")

    def test_all_three_modules_absent_for_non_recovery_persona(self) -> None:
        target = TargetInventory(persona="orchestrator", entries=())
        for module in recovery.build_optional_modules():
            declaration = module.declare(target)
            self.assertFalse(declaration.applies, module.id)


# ---------------------------------------------------------------------------
# RecoveryRecoveryWatchdogModule -- static contract-only
# ---------------------------------------------------------------------------


class RecoveryRecoveryWatchdogModuleTest(_TempEvidenceCase):
    def test_prove_against_real_repo_files_is_proven(self) -> None:
        module = recovery.RecoveryRecoveryWatchdogModule()
        ctx = self.ctx("run-rw-1")
        evidence = module.prove(ctx)
        self.assertEqual(evidence.status, "proven")
        self.assertGreater(evidence.detail["checks_verified"], 0)
        row = module.report(evidence)
        self.assertEqual(row.status, "proven")
        self.assertIn("STATIC", "".join(evidence.detail["scope"].split()).upper()[:6] + evidence.detail["scope"])

    def test_prove_gaps_when_canonical_files_missing(self) -> None:
        empty_root = Path(tempfile.mkdtemp(prefix="t2567-empty-repo-"))
        self.addCleanup(shutil.rmtree, empty_root, ignore_errors=True)
        module = recovery.RecoveryRecoveryWatchdogModule(repo_root=empty_root)
        ctx = self.ctx("run-rw-2")
        evidence = module.prove(ctx)
        self.assertEqual(evidence.status, "gap")
        self.assertEqual(len(evidence.detail["missing"]), 3)

    def test_restore_is_a_no_op(self) -> None:
        module = recovery.RecoveryRecoveryWatchdogModule()
        ctx = self.ctx("run-rw-3")
        restore_evidence = module.restore(ctx)
        self.assertTrue(restore_evidence.restored)

    def test_prove_hardstops_on_a_tampered_contract_marker(self) -> None:
        # Copy the real files into a scratch repo, then corrupt one contract
        # marker -- proves the assertions are load-bearing, not decorative.
        scratch_repo = Path(tempfile.mkdtemp(prefix="t2567-tampered-repo-"))
        self.addCleanup(shutil.rmtree, scratch_repo, ignore_errors=True)
        real_root = recovery._repo_root()
        for rel in (recovery.WATCHDOG_TIMER_REL, recovery.WATCHDOG_SERVICE_REL, recovery.WATCHDOG_SCRIPT_REL):
            dst = scratch_repo / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(real_root / rel, dst)
        script_dst = scratch_repo / recovery.WATCHDOG_SCRIPT_REL
        script_dst.write_text(
            script_dst.read_text(encoding="utf-8").replace("--simulate-absent", "--REMOVED"),
            encoding="utf-8",
        )
        module = recovery.RecoveryRecoveryWatchdogModule(repo_root=scratch_repo)
        with self.assertRaises(HardStop):
            module.prove(self.ctx("run-rw-4"))


# ---------------------------------------------------------------------------
# RecoverySelfRestartModule -- dynamic, disposable-tmux-safe
# ---------------------------------------------------------------------------


class RecoverySelfRestartModuleTest(_PrivateTmuxServerMixin, _TempEvidenceCase):
    def test_disposable_sessions_land_on_the_private_server(self) -> None:
        module = recovery.RecoverySelfRestartModule()
        name = module._create_session("run-sr-5")
        try:
            self.assertEqual(self.private_has_session(name), 0)
        finally:
            module._kill_session(name)
        self.assertEqual(module._created_sessions, [])

    def test_anchor_keeps_private_server_alive_across_kill_then_create(self) -> None:
        module = recovery.RecoverySelfRestartModule()
        first = module._create_session("run-sr-6")
        module._kill_session(first)
        self.assertNotEqual(self.private_has_session(first), 0)
        self.assertEqual(self.private_has_session(ANCHOR_SESSION), 0)
        second = module._create_session("run-sr-6")
        try:
            self.assertEqual(self.private_has_session(second), 0)
        finally:
            module._kill_session(second)

    def test_prove_against_real_script_is_proven_and_restore_leaves_no_residue(self) -> None:
        module = recovery.RecoverySelfRestartModule()
        ctx = self.ctx("run-sr-1")
        evidence = module.prove(ctx)
        self.assertEqual(evidence.status, "proven")
        cases = evidence.detail["cases"]
        for expected_case in (
            "missing---service", "missing---target-pane", "invalid---mode",
            "missing-tmux-session", "missing-restart-script", "happy-path-dry-run",
        ):
            self.assertIn(expected_case, cases)
        restore_evidence = module.restore(ctx)
        self.assertTrue(restore_evidence.restored)
        self.assertEqual(module._created_sessions, [])

    def test_prove_gaps_when_script_missing(self) -> None:
        empty_root = Path(tempfile.mkdtemp(prefix="t2567-empty-repo-sr-"))
        self.addCleanup(shutil.rmtree, empty_root, ignore_errors=True)
        module = recovery.RecoverySelfRestartModule(repo_root=empty_root)
        evidence = module.prove(self.ctx("run-sr-2"))
        self.assertEqual(evidence.status, "gap")

    def test_never_touches_the_real_recovery_session_name(self) -> None:
        module = recovery.RecoverySelfRestartModule()
        module.prove(self.ctx("run-sr-3"))
        for name in module._created_sessions:
            self.assertNotEqual(name, "claude-telegram-bot-fallback")
        module.restore(self.ctx("run-sr-3"))

    def test_restore_hardstops_on_scratch_root_residue(self) -> None:
        module = recovery.RecoverySelfRestartModule()
        ctx = self.ctx("run-sr-4")
        module.prove(ctx)
        with mock.patch("tools.bringup_acceptance.optionals.recovery.shutil.rmtree"):
            with self.assertRaises(HardStop):
                module.restore(ctx)
        # Clean up for real regardless of the mocked call above.
        if module._scratch_root is not None:
            shutil.rmtree(module._scratch_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# RecoveryBootKickModule -- dynamic, fully seam-driven, zero tmux
# ---------------------------------------------------------------------------


class BootKickGraceSeamTest(unittest.TestCase):
    """The seam keeps production's bound: sentinel timeout + 10s, unless widened."""

    def _bound_for(self, module: recovery.RecoveryBootKickModule, sentinel_timeout: int) -> float:
        scratch = Path(tempfile.mkdtemp(prefix="t4774-grace-"))
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch.object(recovery.subprocess, "run", return_value=done) as run:
            module._run_case(
                scratch / "recovery_boot_kick.sh", scratch / "witness", scratch / "stub.sh",
                logger=scratch / "fake_logger.sh", inflight="false", capture="true", send_rc=0, timeout=sentinel_timeout,
            )
        return run.call_args.kwargs["timeout"]

    def test_production_default_is_unchanged(self) -> None:
        self.assertEqual(recovery.RecoveryBootKickModule.RUN_CASE_GRACE_S, 10)
        self.assertEqual(self._bound_for(recovery.RecoveryBootKickModule(), 2), 12)

    def test_widened_grace_reaches_the_subprocess_bound(self) -> None:
        with mock.patch.object(recovery.RecoveryBootKickModule, "RUN_CASE_GRACE_S", 60):
            self.assertEqual(self._bound_for(recovery.RecoveryBootKickModule(), 2), 62)


class RecoveryBootKickModuleTest(_BootKickTestSeamsMixin, _TempEvidenceCase):
    def test_prove_proves_all_five_guard_scenarios(self) -> None:
        module = recovery.RecoveryBootKickModule()
        ctx = self.ctx("run-bk-1")
        evidence = module.prove(ctx)
        self.assertEqual(evidence.status, "proven")
        scenarios = evidence.detail["scenarios"]
        self.assertEqual(scenarios["in-flight-kicker-skip"]["sent"], 0)
        self.assertEqual(scenarios["bare-idle-kicks-once"]["sent"], 1)
        self.assertEqual(scenarios["no-sentinel-timeout"]["sent"], 0)
        self.assertEqual(scenarios["dialog-modal-excluded"]["sent"], 0)
        self.assertEqual(scenarios["send-fails-fail-open"]["sent"], 1)
        for name, row in scenarios.items():
            self.assertEqual(row["exit"], 0, name)
        # T-5075: the module's own logger stub took -- every scenario logged to it.
        for name, row in scenarios.items():
            self.assertGreaterEqual(row["logged"], 1, name)
        restore_evidence = module.restore(ctx)
        self.assertTrue(restore_evidence.restored)

    def test_prove_gaps_when_script_missing(self) -> None:
        empty_root = Path(tempfile.mkdtemp(prefix="t2567-empty-repo-bk-"))
        self.addCleanup(shutil.rmtree, empty_root, ignore_errors=True)
        module = recovery.RecoveryBootKickModule(repo_root=empty_root)
        evidence = module.prove(self.ctx("run-bk-2"))
        self.assertEqual(evidence.status, "gap")

    def test_restore_hardstops_on_scratch_root_residue(self) -> None:
        module = recovery.RecoveryBootKickModule()
        ctx = self.ctx("run-bk-3")
        module.prove(ctx)
        with mock.patch("tools.bringup_acceptance.optionals.recovery.shutil.rmtree"):
            with self.assertRaises(HardStop):
                module.restore(ctx)
        if module._scratch_root is not None:
            shutil.rmtree(module._scratch_root, ignore_errors=True)


class BootKickLoggerStubTest(_BootKickTestSeamsMixin, _TempEvidenceCase):
    """T-5075: the production module, with no RECOVERY_KICK_LOGGER from the caller, passes
    its own scratch stub to every scenario, and the real `logger` is never invoked."""

    def setUp(self) -> None:
        super().setUp()
        self.seam_dir = Path(tempfile.mkdtemp(prefix="t5075-logger-"))
        self.addCleanup(shutil.rmtree, self.seam_dir, ignore_errors=True)

    def _shim_path(self) -> Path:
        """Put a recording `logger` shim at the front of PATH; return its record file."""
        record = self.seam_dir / "logger-shim-calls"
        _make_recorder(self.seam_dir / "bin" / "logger", record)
        os.environ["PATH"] = f"{self.seam_dir / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
        self.assertEqual(shutil.which("logger"), str(self.seam_dir / "bin" / "logger"))
        return record

    def _cleanup_module(self, module: recovery.RecoveryBootKickModule) -> None:
        if module._scratch_root is not None:
            shutil.rmtree(module._scratch_root, ignore_errors=True)

    def test_production_module_passes_its_own_logger_stub(self) -> None:
        self.assertNotIn("RECOVERY_KICK_LOGGER", os.environ)
        real_logger = shutil.which("logger")
        module = recovery.RecoveryBootKickModule()
        self.addCleanup(self._cleanup_module, module)
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        sent_counts = iter([0, 1, 0, 0, 1])
        with mock.patch.object(recovery.subprocess, "run", return_value=done) as run, \
                mock.patch.object(recovery.RecoveryBootKickModule, "_kick_count",
                                  side_effect=lambda _w: next(sent_counts)):
            evidence = module.prove(self.ctx("run-t5075-a"))
        self.assertEqual(evidence.status, "proven")
        self.assertEqual(run.call_count, 5)
        loggers = {call.kwargs["env"]["RECOVERY_KICK_LOGGER"] for call in run.call_args_list}
        self.assertEqual(len(loggers), 1, loggers)
        stub = Path(loggers.pop())
        self.assertEqual(stub.parent, module._scratch_root)
        self.assertTrue(stub.is_file())
        self.assertTrue(os.access(stub, os.X_OK))
        self.assertNotEqual(stub.name, "logger")
        if real_logger is not None:
            self.assertNotEqual(stub.resolve(), Path(real_logger).resolve())
        self.assertNotIn("logger -t", stub.read_text(encoding="utf-8"))

    def test_inherited_logger_env_is_overridden(self) -> None:
        poison_record = self.seam_dir / "poison-calls"
        poison = _make_recorder(self.seam_dir / "poison_logger.sh", poison_record)
        os.environ["RECOVERY_KICK_LOGGER"] = str(poison)
        module = recovery.RecoveryBootKickModule()
        ctx = self.ctx("run-t5075-b")
        evidence = module.prove(ctx)
        self.addCleanup(self._cleanup_module, module)
        self.assertEqual(evidence.status, "proven")
        self.assertFalse(poison_record.exists(), "inherited RECOVERY_KICK_LOGGER was used")
        for name, row in evidence.detail["scenarios"].items():
            self.assertGreaterEqual(row["logged"], 1, name)
        module.restore(ctx)

    def test_real_logger_never_on_invocation_path(self) -> None:
        record = self._shim_path()
        module = recovery.RecoveryBootKickModule()
        ctx = self.ctx("run-t5075-c")
        evidence = module.prove(ctx)
        self.addCleanup(self._cleanup_module, module)
        self.assertEqual(evidence.status, "proven")
        self.assertFalse(
            record.exists() and record.read_text(encoding="utf-8").strip(),
            "the PATH `logger` was invoked during an acceptance run",
        )
        for name, row in evidence.detail["scenarios"].items():
            self.assertGreaterEqual(row["logged"], 1, name)
        module.restore(ctx)

    def test_path_shim_catches_unstubbed_logger(self) -> None:
        # Negative control: without the seam the script does reach the PATH `logger`,
        # so the shim above would see a leak. The shim shadows the real logger, so the
        # host journal is never touched.
        record = self._shim_path()
        script = recovery._repo_root() / recovery.BOOT_KICK_REL
        env = dict(os.environ)
        env.pop("RECOVERY_KICK_LOGGER", None)
        env.update({"RECOVERY_KICK_INFLIGHT_CMD": "true", "RECOVERY_KICK_SENTINEL_TIMEOUT": "1"})
        result = subprocess.run(
            [str(script), "t5075-negative-control:0", "recovery"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        calls = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
        self.assertEqual(len(calls), 1, calls)
        self.assertTrue(calls[0].startswith("-t pantheon-recovery-boot-kick -- "), calls[0])


# ---------------------------------------------------------------------------
# End-to-end via registry.run_persona_optionals -- the real lifecycle glue.
# ---------------------------------------------------------------------------


class RunPersonaOptionalsIntegrationTest(_BootKickTestSeamsMixin, _PrivateTmuxServerMixin, _TempEvidenceCase):
    def test_registry_builds_the_class_the_seams_patch(self) -> None:
        # The class patch only reaches the e2e path if the registry's dotted import
        # yields this same module object.
        modules = {m.id: m for m in registry.discover_optional_modules("recovery")}
        boot_kick = modules["recovery-boot-kick"]
        self.assertIs(type(boot_kick), recovery.RecoveryBootKickModule)
        self.assertEqual(boot_kick.RUN_CASE_GRACE_S, BOOT_KICK_TEST_GRACE_S)

    def test_recovery_persona_runs_all_three_modules_to_completion(self) -> None:
        target = TargetInventory(persona="recovery", entries=())
        ctx = self.ctx("run-e2e-1")
        rows = registry.run_persona_optionals("recovery", target=target, ctx=ctx)
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertIn(row.status, ("proven", "gap"))

    def test_non_recovery_persona_gets_declared_absent_rows(self) -> None:
        target = TargetInventory(persona="orchestrator", entries=())
        ctx = self.ctx("run-e2e-2", persona="orchestrator")
        rows = registry.run_persona_optionals("recovery", target=target, ctx=ctx)
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row.status, "declared_absent")

    def test_bare_persona_with_no_plugin_file_is_unaffected(self) -> None:
        target = TargetInventory(persona="some-bare-daemon", entries=())
        ctx = self.ctx("run-e2e-3", persona="some-bare-daemon")
        rows = registry.run_persona_optionals("some-bare-daemon", target=target, ctx=ctx)
        self.assertEqual(rows, ())


if __name__ == "__main__":
    unittest.main()
