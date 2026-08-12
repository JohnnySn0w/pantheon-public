"""Tests for tools.bringup_acceptance.optionals.recovery (T-2556 phase 12 / T-2567)."""

from __future__ import annotations

import shutil
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
# WardRecoveryWatchdogModule -- static contract-only
# ---------------------------------------------------------------------------


class WardRecoveryWatchdogModuleTest(_TempEvidenceCase):
    def test_prove_against_real_repo_files_is_proven(self) -> None:
        module = recovery.WardRecoveryWatchdogModule()
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
        module = recovery.WardRecoveryWatchdogModule(repo_root=empty_root)
        ctx = self.ctx("run-rw-2")
        evidence = module.prove(ctx)
        self.assertEqual(evidence.status, "gap")
        self.assertEqual(len(evidence.detail["missing"]), 3)

    def test_restore_is_a_no_op(self) -> None:
        module = recovery.WardRecoveryWatchdogModule()
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
        module = recovery.WardRecoveryWatchdogModule(repo_root=scratch_repo)
        with self.assertRaises(HardStop):
            module.prove(self.ctx("run-rw-4"))


# ---------------------------------------------------------------------------
# WardSelfRestartModule -- dynamic, disposable-tmux-safe
# ---------------------------------------------------------------------------


class WardSelfRestartModuleTest(_TempEvidenceCase):
    def test_prove_against_real_script_is_proven_and_restore_leaves_no_residue(self) -> None:
        module = recovery.WardSelfRestartModule()
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
        module = recovery.WardSelfRestartModule(repo_root=empty_root)
        evidence = module.prove(self.ctx("run-sr-2"))
        self.assertEqual(evidence.status, "gap")

    def test_never_touches_the_real_recovery_session_name(self) -> None:
        module = recovery.WardSelfRestartModule()
        module.prove(self.ctx("run-sr-3"))
        for name in module._created_sessions:
            self.assertNotEqual(name, "claude-telegram-bot-fallback")
        module.restore(self.ctx("run-sr-3"))

    def test_restore_hardstops_on_scratch_root_residue(self) -> None:
        module = recovery.WardSelfRestartModule()
        ctx = self.ctx("run-sr-4")
        module.prove(ctx)
        with mock.patch("tools.bringup_acceptance.optionals.recovery.shutil.rmtree"):
            with self.assertRaises(HardStop):
                module.restore(ctx)
        # Clean up for real regardless of the mocked call above.
        if module._scratch_root is not None:
            shutil.rmtree(module._scratch_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# WardBootKickModule -- dynamic, fully seam-driven, zero tmux
# ---------------------------------------------------------------------------


class WardBootKickModuleTest(_TempEvidenceCase):
    def test_prove_proves_all_five_guard_scenarios(self) -> None:
        module = recovery.WardBootKickModule()
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
        restore_evidence = module.restore(ctx)
        self.assertTrue(restore_evidence.restored)

    def test_prove_gaps_when_script_missing(self) -> None:
        empty_root = Path(tempfile.mkdtemp(prefix="t2567-empty-repo-bk-"))
        self.addCleanup(shutil.rmtree, empty_root, ignore_errors=True)
        module = recovery.WardBootKickModule(repo_root=empty_root)
        evidence = module.prove(self.ctx("run-bk-2"))
        self.assertEqual(evidence.status, "gap")

    def test_restore_hardstops_on_scratch_root_residue(self) -> None:
        module = recovery.WardBootKickModule()
        ctx = self.ctx("run-bk-3")
        module.prove(ctx)
        with mock.patch("tools.bringup_acceptance.optionals.recovery.shutil.rmtree"):
            with self.assertRaises(HardStop):
                module.restore(ctx)
        if module._scratch_root is not None:
            shutil.rmtree(module._scratch_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# End-to-end via registry.run_persona_optionals -- the real lifecycle glue.
# ---------------------------------------------------------------------------


class RunPersonaOptionalsIntegrationTest(_TempEvidenceCase):
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
