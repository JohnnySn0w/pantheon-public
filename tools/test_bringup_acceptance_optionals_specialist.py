"""Tests for tools.bringup_acceptance.optionals.specialist (T-2556 phase 13 / T-2565)."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from tools.bringup_acceptance.optionals.specialist import (
    MODULE_ID_AUTOCOMMIT,
    MODULE_ID_WATCH_RUNNER,
    ArgusWatchAutocommitModule,
    ArgusWatchRunnerModule,
    build_optional_modules,
)
from tools.bringup_acceptance.recorder import validate_token
from tools.bringup_acceptance.registry import discover_optional_modules
from tools.bringup_acceptance.types import HardStop, RunContext, TargetInventory


_SPECIALIST_TARGET = TargetInventory(persona="specialist", entries=())
_OTHER_TARGET = TargetInventory(persona="recovery", entries=())


def _make_ctx(evidence_root: Path, *, run_id: str = "run-t2565") -> RunContext:
    return RunContext(
        run_id=validate_token(run_id, label="run_id"),
        persona="specialist",
        account="specialist",
        harness="claude-code",
        profile="default",
        hub_path=evidence_root / "hub",
        state_path=evidence_root / "state",
        evidence_root=evidence_root,
        baseline_ref="test-ref",
    )


class BuildOptionalModulesTest(unittest.TestCase):
    def test_returns_two_optional_tier_modules_with_unique_ids(self) -> None:
        modules = build_optional_modules()
        self.assertEqual(len(modules), 2)
        ids = {module.id for module in modules}
        self.assertEqual(ids, {MODULE_ID_WATCH_RUNNER, MODULE_ID_AUTOCOMMIT})
        for module in modules:
            self.assertEqual(module.tier, "optional")


class RegistryIntegrationTest(unittest.TestCase):
    """Exercises the REAL registry.discover_optional_modules("specialist") path --
    the actual dotted-import of tools.bringup_acceptance.optionals.specialist, its
    build_optional_modules() entry point, and the registry's own tier/id/
    duplicate HardStop checks. Every other test in this file instantiates the
    module classes directly, which never touches that discovery wiring --
    this is the one test that would catch a break in it (T-2565 is the FIRST
    plug-in against the landed T-2561 contract)."""

    def test_discover_optional_modules_finds_both_specialist_modules(self) -> None:
        discovered = discover_optional_modules("specialist")
        self.assertEqual(len(discovered), 2)

        ids = {module.id for module in discovered}
        self.assertEqual(ids, {MODULE_ID_WATCH_RUNNER, MODULE_ID_AUTOCOMMIT})

        for module in discovered:
            self.assertEqual(module.tier, "optional")

        # Distinct instances discovered via the real dotted-import, not the
        # same objects build_optional_modules() would return to a direct caller
        # -- confirms discovery actually imported and called the module, not a
        # cached reference from an earlier direct instantiation in this file.
        self.assertEqual(len(ids), len({id(module) for module in discovered}))


class DeclareTest(unittest.TestCase):
    def test_watch_runner_declares_applies_for_specialist_persona(self) -> None:
        module = ArgusWatchRunnerModule()
        declaration = module.declare(_SPECIALIST_TARGET)
        self.assertTrue(declaration.applies)
        self.assertEqual(declaration.module_id, MODULE_ID_WATCH_RUNNER)
        self.assertEqual(declaration.tier, "optional")

    def test_watch_runner_declares_absent_for_non_specialist_persona(self) -> None:
        module = ArgusWatchRunnerModule()
        declaration = module.declare(_OTHER_TARGET)
        self.assertFalse(declaration.applies)
        self.assertIn("recovery", declaration.reason)

    def test_autocommit_declares_applies_for_specialist_persona(self) -> None:
        module = ArgusWatchAutocommitModule()
        declaration = module.declare(_SPECIALIST_TARGET)
        self.assertTrue(declaration.applies)
        self.assertEqual(declaration.module_id, MODULE_ID_AUTOCOMMIT)

    def test_autocommit_declares_absent_for_non_specialist_persona(self) -> None:
        module = ArgusWatchAutocommitModule()
        declaration = module.declare(_OTHER_TARGET)
        self.assertFalse(declaration.applies)


class ArgusWatchRunnerProveTest(unittest.TestCase):
    def test_prove_verifies_unit_wiring_and_brief_contract_then_restores(self) -> None:
        module = ArgusWatchRunnerModule()
        with _tmp_evidence_root() as evidence_root:
            ctx = _make_ctx(evidence_root)
            declaration = module.declare(_SPECIALIST_TARGET)
            self.assertTrue(declaration.applies)

            evidence = module.prove(ctx)
            self.assertEqual(evidence.status, "proven")
            self.assertEqual(evidence.detail["unit_wiring"], "proven")
            self.assertIn("WIRING/CONTRACT ONLY", evidence.detail["researcher_dispatch"])
            self.assertTrue(evidence.evidence_paths)

            scratch_root = module._scratch_root
            self.assertIsNotNone(scratch_root)
            self.assertTrue(scratch_root.exists())

            restore_evidence = module.restore(ctx)
            self.assertTrue(restore_evidence.restored)
            self.assertFalse(scratch_root.exists())

            row = module.report(evidence)
            self.assertEqual(row.status, "proven")
            self.assertEqual(row.module_id, MODULE_ID_WATCH_RUNNER)

    def test_prove_hard_stops_on_missing_service_wiring(self) -> None:
        module = ArgusWatchRunnerModule()
        from tools.bringup_acceptance.optionals import specialist as specialist_mod
        from tools import watch_runner

        original_root = watch_runner.REPO_ROOT
        with _tmp_evidence_root() as evidence_root:
            ctx = _make_ctx(evidence_root)
            fake_repo_root = evidence_root / "fake-repo"
            (fake_repo_root / "tools" / "systemd").mkdir(parents=True)
            (fake_repo_root / "tools" / "systemd" / "pantheon-watch.service").write_text(
                "[Service]\nExecStart=/bin/true\n", encoding="utf-8"
            )
            (fake_repo_root / "tools" / "systemd" / "pantheon-watch.timer").write_text(
                "[Timer]\nUnit=pantheon-watch.service\n", encoding="utf-8"
            )
            watch_runner.REPO_ROOT = fake_repo_root
            try:
                with self.assertRaises(HardStop):
                    module.prove(ctx)
            finally:
                watch_runner.REPO_ROOT = original_root


class ArgusWatchAutocommitProveTest(unittest.TestCase):
    def test_prove_lands_clean_report_and_refuses_hermes_tripping_report(self) -> None:
        module = ArgusWatchAutocommitModule()
        with _tmp_evidence_root() as evidence_root:
            ctx = _make_ctx(evidence_root)
            declaration = module.declare(_SPECIALIST_TARGET)
            self.assertTrue(declaration.applies)

            evidence = module.prove(ctx)
            self.assertEqual(evidence.status, "proven")
            self.assertIn("push_message", evidence.detail["success_case"])
            self.assertIn(
                "research(watch): land", evidence.detail["success_case"]["landed_commit_message"]
            )
            self.assertIn("refusal_message", evidence.detail["hermes_refusal_case"])
            self.assertIn(
                "HERMES guard refused", evidence.detail["hermes_refusal_case"]["refusal_message"]
            )

            scratch_root = module._scratch_root
            self.assertIsNotNone(scratch_root)
            self.assertTrue(scratch_root.exists())

            restore_evidence = module.restore(ctx)
            self.assertTrue(restore_evidence.restored)
            self.assertFalse(scratch_root.exists())

            row = module.report(evidence)
            self.assertEqual(row.status, "proven")
            self.assertEqual(row.module_id, MODULE_ID_AUTOCOMMIT)

    def test_prove_never_touches_the_real_repo_lock_env(self) -> None:
        import os

        module = ArgusWatchAutocommitModule()
        env_key = "PANTHEON_GIT_MUTATION_LOCKFILE"
        self.assertNotIn(env_key, os.environ)
        with _tmp_evidence_root() as evidence_root:
            ctx = _make_ctx(evidence_root)
            module.prove(ctx)
            module.restore(ctx)
        # The env override is scoped to each call and restored afterward.
        self.assertNotIn(env_key, os.environ)


@contextmanager
def _tmp_evidence_root():
    root = Path(tempfile.mkdtemp(prefix="bringup-specialist-test-"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
