"""T-5449: static fleet completeness lint.

Intent: one installer run puts a WHOLE seat on the control plane; this fails the
tree if any seat in the real instance.yaml would be planned incomplete (T-5446).
Pure plan inspection: no host, no backend, no tmp state beyond a throwaway root.

Flip assertions (T-5451): every persona with a manifest fallback has cap-swap,
fallback-return and the planned-quota check effective-ON (fleet policy, T-5446).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import persona_bringup
from tools.persona_installer import (
    HO_RESUMER_UNITS,
    LEGACY_UNIT_SETS,
    Manifest,
    PersonaInstaller,
)
from tools.test_persona_installer import REPO_ROOT

CONSUME = ("connective.service-unit", "connective.timer-unit", "connective.timer-enable")
CONTROLLER = ("service.runtime-controller.unit", "service.runtime-controller.enable-observe")
PERIODIC_TIMERS = ("quota", "inbox-consumer", "restart-drain")
KEEP_GOING_OPS = (
    "periodic.keep-going.service-unit", "periodic.keep-going.timer-unit",
    "periodic.keep-going.timer-enable", "periodic.keep-going.timer-observe",
)
PERIODIC_UNITS = (
    "periodic.inbox-consumer.service-unit", "periodic.inbox-consumer.timer-unit",
    "periodic.restart-drain.service-unit", "periodic.restart-drain.timer-unit",
    "periodic.restart-drain.dropin", "periodic.unit-failure-alert.service-unit",
    "quota.service-unit", "quota.timer-unit",
)


class FleetCompletenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        cls.installer = PersonaInstaller(REPO_ROOT)
        cls.personas = sorted(
            {raw["persona"] for raw in cls.manifest.roles.values()
             if isinstance(raw, dict) and raw.get("persona")}
        )
        mailbox = cls.manifest.installer["mailbox"]
        cls.wake = set(mailbox.get("wake_timer_personas") or ()) | set(
            mailbox.get("wake_timer_secondary_personas") or ())
        cls.tmp = tempfile.TemporaryDirectory()
        cls.plans = {}
        for persona in cls.personas:
            # no swallowed planning errors: a persona that stops planning fails loudly
            cls.plans[persona] = cls.installer.plan(cls.manifest, persona, root=Path(cls.tmp.name) / persona)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def ops(self, persona):
        return {op.operation_id: op for op in self.plans[persona].operations}

    def test_the_real_instance_has_personas(self) -> None:
        self.assertGreaterEqual(len(self.personas), 8)
        self.assertTrue(self.wake)
        self.assertTrue(self.wake <= set(self.personas), self.wake - set(self.personas))

    def test_every_seat_plans_the_consume_units_and_timer_enable(self) -> None:
        for persona in self.personas:
            with self.subTest(persona=persona):
                ops = self.ops(persona)
                for operation_id in CONSUME:
                    self.assertIn(operation_id, ops)
                self.assertEqual(ops["connective.timer-enable"].kind, "timer-enable")

    def test_every_seat_plans_the_controller_unit_and_enable_observe(self) -> None:
        for persona in self.personas:
            with self.subTest(persona=persona):
                ops = self.ops(persona)
                for operation_id in CONTROLLER:
                    self.assertIn(operation_id, ops)
                self.assertEqual(ops[CONTROLLER[1]].kind, "seat-service-enable-observe")
                self.assertEqual(ops[CONTROLLER[1]].target, f"pantheon-runtime-controller@{persona}.service")

    def test_every_seat_enables_quota_inbox_consumer_and_restart_drain(self) -> None:
        for persona in self.personas:
            with self.subTest(persona=persona):
                ops = self.ops(persona)
                for name in PERIODIC_TIMERS:
                    enable = ops[f"periodic.{name}.timer-enable"]
                    self.assertEqual(enable.kind, "seat-periodic-timer-enable")
                    self.assertTrue(enable.target.endswith(".timer"), enable.target)
                    self.assertEqual(ops[f"periodic.{name}.timer-observe"].kind, "seat-periodic-timer-observe")
                for operation_id in PERIODIC_UNITS:
                    self.assertIn(operation_id, ops)

    def test_every_seat_gets_fleet_usage_retention_and_codex_seats_the_export(self) -> None:
        # T-5961: retention is installer-owned on every seat (no hand step); codex seats
        # (harness codex) also get the Codex usage export. Named by the fleet-usage seat.
        expected_seat = {"codex": "consultant"}
        for persona in self.personas:
            with self.subTest(persona=persona):
                ops = self.ops(persona)
                seat = expected_seat.get(persona, persona)
                codex = self.manifest.binding(persona).configured_harness == "codex"
                names = ["fleet-usage-prune"] + (["fleet-usage-codex-export"] if codex else [])
                for name in names:
                    for suffix in ("service-unit", "timer-unit"):
                        self.assertEqual(ops[f"periodic.{name}.{suffix}"].kind, "seat-periodic-unit-install")
                    timer = f"pantheon-{name}@{seat}.timer"
                    self.assertEqual(ops[f"periodic.{name}.timer-enable"].target, timer)
                    self.assertEqual(ops[f"periodic.{name}.timer-observe"].target, timer)
                if not codex:
                    self.assertNotIn("periodic.fleet-usage-codex-export.timer-enable", ops)
        self.assertIn("codex", self.personas)
        self.assertIn("watchman", self.personas)

    def test_keep_going_is_planned_exactly_for_the_personas_keep_going_maps(self) -> None:
        # T-5748: only Archivist remains deliberately exempt; both Codex seats
        # receive the shared harness-aware cadence.
        from tools import keep_going

        for persona in self.personas:
            with self.subTest(persona=persona):
                ops = self.ops(persona)
                if persona in keep_going.AGENT_PANE:
                    for operation_id in KEEP_GOING_OPS:
                        self.assertIn(operation_id, ops)
                else:
                    self.assertIn(persona, keep_going._NO_KEEP_GOING)
                    for operation_id in KEEP_GOING_OPS:
                        self.assertNotIn(operation_id, ops)

    def test_keep_going_skip_is_driven_by_the_exemption_registry(self) -> None:
        from tools import persona_installer as pim

        self.assertTrue(pim._keep_going_planned("recovery"))
        for persona in ("codex", "watchman"):
            with self.subTest(persona=persona):
                self.assertTrue(pim._keep_going_planned(persona))
        self.assertFalse(pim._keep_going_planned("archivist"))

    def test_the_mailbox_wake_timer_is_enabled_exactly_for_the_wake_listed_personas(self) -> None:
        for persona in self.personas:
            with self.subTest(persona=persona):
                ops = self.ops(persona)
                if persona in self.wake:
                    self.assertEqual(ops["periodic.mailbox-wake.timer-enable"].kind, "seat-periodic-timer-enable")
                    self.assertIn("periodic.mailbox-wake.timer-unit", ops)
                    self.assertIn("periodic.mailbox-wake.service-unit", ops)
                else:
                    self.assertNotIn("periodic.mailbox-wake.timer-enable", ops)

    def test_the_ho_resumer_is_decommissioned_exactly_where_declared_before_the_first_fire(self) -> None:
        declared = {
            persona: self.manifest.binding(persona).legacy_units
            for persona in self.personas
            if self.manifest.binding(persona).legacy_units
        }
        self.assertEqual(declared, {"orchestrator": ("ho-resumer",), "codex": ("codex-keep-going",)})
        for persona in self.personas:
            with self.subTest(persona=persona):
                plan = self.plans[persona]
                ids = [op.operation_id for op in plan.operations]
                if persona not in declared:
                    self.assertNotIn("seat-service-decommission", {op.kind for op in plan.operations})
                    continue
                legacy_set = declared[persona][0]
                op = self.ops(persona)[f"service.{legacy_set}.decommission"]
                self.assertEqual(op.kind, "seat-service-decommission")
                self.assertEqual(op.desired["units"], ",".join(LEGACY_UNIT_SETS[legacy_set]))
                self.assertLess(ids.index(op.operation_id), ids.index("connective.unit-fire"))

    def test_every_periodic_and_the_controller_are_ordered_after_the_consume_timer(self) -> None:
        for persona in self.personas:
            with self.subTest(persona=persona):
                ids = [op.operation_id for op in self.plans[persona].operations]
                for name in PERIODIC_TIMERS:
                    self.assertLess(ids.index("connective.timer-enable"), ids.index(f"periodic.{name}.timer-enable"))
                self.assertLess(ids.index(CONTROLLER[1]), ids.index("connective.timer-enable"))

    def test_every_seat_with_a_fallback_flips(self) -> None:
        """T-5446 / TG 12274: no seat is left off the codex flip by a missing key."""
        manifest_path = REPO_ROOT / "instance.yaml"
        with_fallback = []
        for persona in self.personas:
            with self.subTest(persona=persona):
                binding = persona_bringup.load_binding(manifest_path, persona)
                if not binding.controller.policy.fallbacks:
                    continue  # structurally no-op (archivist, watchman, ...)
                with_fallback.append(persona)
                self.assertTrue(binding.controller.mid_session_cap_swap_enabled)
                self.assertTrue(binding.controller.fallback_return_enabled)
                supported, reason = persona_bringup.quota_check_supported(binding, manifest_path=manifest_path)
                self.assertTrue(supported, reason)
        self.assertTrue({"orchestrator", "artisan", "specialist", "recovery", "mason"} <= set(with_fallback), with_fallback)


if __name__ == "__main__":
    unittest.main()
