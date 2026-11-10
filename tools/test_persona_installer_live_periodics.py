"""T-6140: on an already-live seat, --only periodic timer-enable ops drop their bootstrap edges.

Hermetic: tmp root, probe callables, the simulated backend. Nothing touches systemd.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import persona_installer_apply as apply_cli
from tools.persona_installer import InstallPlan

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "instance.yaml"
CODEX = ["checkpoint", "rotate"]
SUBSET = [
    "periodic.codex-handoff-checkpoint.service-unit",
    "periodic.codex-handoff-checkpoint.timer-unit",
    "periodic.codex-thread-rotate.service-unit",
    "periodic.codex-thread-rotate.timer-unit",
    "periodic.daemon-reload",
    "periodic.codex-handoff-checkpoint@codex.timer-enable",
    "periodic.codex-handoff-checkpoint@codex.timer-observe",
    "periodic.codex-thread-rotate@codex.timer-enable",
    "periodic.codex-thread-rotate@codex.timer-observe",
]
BOOTSTRAP = ("connective.timer-observe", "service.runtime-controller.enable-observe")


class LivePeriodicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def build(self, live, only=None, persona="recovery") -> InstallPlan:
        probe = None if live is None else (lambda timer, v=live: v)
        return apply_cli.build_plan(
            MANIFEST, persona, repo_root=REPO, root=self.root, only=only, live_probe=probe
        )[1]

    def by_id(self, plan):
        return {o.operation_id: o for o in plan.operations}

    def test_a_live_seat_subset_plans(self) -> None:
        plan = self.build(True, SUBSET)
        self.assertEqual([o.operation_id for o in plan.operations], SUBSET)
        after = self.by_id(plan)["periodic.codex-thread-rotate@codex.timer-enable"].desired["after"]
        self.assertEqual(
            after,
            "periodic.daemon-reload,periodic.codex-thread-rotate.service-unit,"
            "periodic.codex-thread-rotate.timer-unit",
        )

    def test_a_live_singleton_subset_plans(self) -> None:
        full = self.build(True, persona="orchestrator")
        ids = [o.operation_id for o in full.operations]
        prune = [i for i in ids if i.startswith("periodic.echo-target-prune.")]
        self.assertTrue(prune, "orchestrator owns the echo-target prune in instance.yaml")
        subset = [*prune, "periodic.daemon-reload"]
        plan = self.build(True, subset, persona="orchestrator")
        self.assertEqual(len(plan.operations), len(subset))
        with self.assertRaisesRegex(apply_cli.ApplyRefused, "needs connective.timer-observe"):
            self.build(False, subset, persona="orchestrator")

    def test_fresh_seat_is_refused_as_before(self) -> None:
        for live in (False, None):
            with self.assertRaisesRegex(apply_cli.ApplyRefused, "needs connective.timer-observe"):
                self.build(live, SUBSET)

    def test_probe_error_means_fresh_edges(self) -> None:
        def boom(_timer):
            raise RuntimeError("manager unreadable")

        with self.assertRaisesRegex(apply_cli.ApplyRefused, "needs connective.timer-observe"):
            apply_cli.build_plan(
                MANIFEST, "recovery", repo_root=REPO, root=self.root, only=SUBSET, live_probe=boom
            )

    def test_b_fresh_edges_and_order_unchanged(self) -> None:
        for probe in (False, None):
            plan = self.build(probe)
            ids = [o.operation_id for o in plan.operations]
            for op in plan.operations:
                if op.kind == "seat-periodic-timer-enable":
                    self.assertEqual(
                        op.desired["after"],
                        "connective.timer-observe,periodic.daemon-reload,"
                        "service.runtime-controller.enable-observe",
                    )
                    for need in BOOTSTRAP:
                        self.assertLess(ids.index(need), ids.index(op.operation_id))
            self.assertLess(
                ids.index("connective.unit-fire"),
                ids.index("service.runtime-controller.enable-observe"),
            )

    def test_c_digest_stable_and_state_bound(self) -> None:
        a, b = self.build(True, SUBSET), self.build(True, SUBSET)
        self.assertEqual(a.digest, b.digest)
        self.assertNotEqual(a.digest, self.build(True, SUBSET[:5]).digest)

    def test_d_no_other_op_loses_a_prerequisite(self) -> None:
        fresh, live = self.by_id(self.build(False)), self.by_id(self.build(True))
        self.assertEqual(list(fresh), list(live))
        for op_id, op in fresh.items():
            if op.kind == "seat-periodic-timer-enable":
                continue
            self.assertEqual(op, live[op_id], op_id)

    def test_d_full_plan_ignores_the_probe(self) -> None:
        self.assertEqual(self.build(True).digest, self.build(False).digest)
        self.assertEqual(self.build(True).digest, self.build(None).digest)

    def test_live_enable_without_its_unit_files_is_refused(self) -> None:
        only = ["periodic.daemon-reload", "periodic.codex-thread-rotate@codex.timer-enable"]
        with self.assertRaisesRegex(
            apply_cli.ApplyRefused, "timer-enable needs periodic.codex-thread-rotate.service-unit"
        ):
            self.build(True, only)

    def test_live_enable_without_daemon_reload_is_refused(self) -> None:
        only = [i for i in SUBSET if i != "periodic.daemon-reload"]
        with self.assertRaisesRegex(apply_cli.ApplyRefused, "needs periodic.daemon-reload"):
            self.build(True, only)


class LiveExecuteTests(unittest.TestCase):
    def test_f_live_subset_applies_without_unit_fire_on_the_simulated_backend(self) -> None:
        from tools.persona_installer import SimulatedHostBackend

        backend = SimulatedHostBackend()
        backend.timer_enabled = True
        with tempfile.TemporaryDirectory() as tmp:
            installer, plan, manifest = apply_cli.build_plan(
                MANIFEST, "recovery", repo_root=REPO, root=Path(tmp), only=SUBSET,
                live_probe=backend.consume_timer_live,
            )
            report = installer.apply(
                plan, plan.digest, backend, apply_cli.actor_for_plan(plan, uid=None), manifest=manifest
            )
        self.assertEqual(report.status, "applied", report.errors)
        self.assertNotIn("connective.unit-fire", backend.events)
        self.assertIn("periodic.codex-thread-rotate@codex.timer-enable", backend.events)


class ProbeWiringTests(unittest.TestCase):
    def test_backends_expose_the_shared_predicate(self) -> None:
        from tools.persona_installer import SimulatedHostBackend

        sim = SimulatedHostBackend()
        self.assertFalse(sim.consume_timer_live("t"))
        sim.timer_enabled = True
        self.assertTrue(sim.consume_timer_live("t"))

    def test_dry_run_passes_the_backend_probe(self) -> None:
        seen = []
        real = apply_cli.build_plan

        def spy(*a, **kw):
            seen.append(kw.get("live_probe"))
            return real(*a, **kw)

        class B:
            def consume_timer_live(self, timer):
                return True

        apply_cli.build_plan = spy
        try:
            try:
                apply_cli.dry_run(MANIFEST, "recovery", backend=B(), only=["x"])
            except Exception:
                pass
        finally:
            apply_cli.build_plan = real
        self.assertEqual(seen[0].__self__.__class__, B)
        seen.clear()
        apply_cli.build_plan = spy
        try:
            with self.assertRaises(apply_cli.ApplyRefused):  # wrong digest: refused after the build
                apply_cli.execute(MANIFEST, "recovery", plan_digest="sha256:0", backend=B(), only=SUBSET)
        finally:
            apply_cli.build_plan = real
        self.assertEqual(seen[0].__self__.__class__, B)


if __name__ == "__main__":
    unittest.main()
