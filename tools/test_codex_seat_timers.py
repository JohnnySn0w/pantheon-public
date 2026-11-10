"""T-6125: the daily codex HANDOFF-checkpoint and thread-rotate timers are repo templates,
instanced on the codex seat's ACCOUNT, and listed in expected-units.yaml for recovery.

Static content checks only; nothing touches systemd or a live seat.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SYSTEMD = REPO / "tools" / "systemd"
UNITS = {
    "pantheon-codex-handoff-checkpoint": {
        "tool": "codex_handoff_checkpoint.py", "calendar": "06:00:00 UTC", "delay": "20min",
        "timeout": "30min",
    },
    "pantheon-codex-thread-rotate": {
        "tool": "codex_thread_rotate.py", "calendar": "09:00:00 UTC", "delay": "30min",
        "timeout": "40min",
    },
}
LEGACY_STEMS = ("recovery-codex-handoff-checkpoint", "recovery-codex-thread-rotate")


def _read(name: str) -> str:
    return (SYSTEMD / name).read_text(encoding="utf-8")


class ServiceTemplateTests(unittest.TestCase):
    def test_service_content(self) -> None:
        for stem, spec in UNITS.items():
            with self.subTest(stem=stem):
                text = _read(f"{stem}@.service")
                self.assertIn("OnFailure=pantheon-unit-failure-alert@%n.service", text)
                self.assertIn("Type=oneshot", text)
                self.assertIn("SuccessExitStatus=3", text)
                self.assertIn(f"TimeoutStartSec={spec['timeout']}", text)
                execs = re.findall(r"^ExecStart=(.*)$", text, re.M)
                self.assertEqual(len(execs), 1)
                # instanced on the account: sudo to it, run from its own clone, mailbox group
                self.assertEqual(
                    execs[0],
                    '/usr/bin/sudo -n -u %i /usr/bin/sg pantheon-mailbox -c '
                    f'"python3 /home/%i/pantheon-config/tools/{spec["tool"]}"',
                )

    def test_no_persona_is_hard_coded_in_a_unit(self) -> None:
        for stem in UNITS:
            for suffix in ("service", "timer"):
                text = _read(f"{stem}@.{suffix}")
                for persona in ("--persona", "watchman", "consultant", "codex-fallback"):
                    self.assertNotIn(persona, text, f"{stem}@.{suffix} names {persona}")

    def test_rotate_timeout_exceeds_the_checkpoint_ack_wait(self) -> None:
        import tools.codex_handoff_checkpoint as chk

        ack_min = chk.DEFAULT_ACK_TIMEOUT_SECONDS / 60
        self.assertGreater(40, ack_min)
        self.assertGreater(30, ack_min)


class TimerTemplateTests(unittest.TestCase):
    def test_timer_content(self) -> None:
        for stem, spec in UNITS.items():
            with self.subTest(stem=stem):
                text = _read(f"{stem}@.timer")
                self.assertIn(f"OnCalendar=*-*-* {spec['calendar']}", text)
                self.assertIn(f"RandomizedDelaySec={spec['delay']}", text)
                self.assertIn("Persistent=true", text)
                self.assertIn("WantedBy=timers.target", text)

    def test_new_stems_never_collide_with_the_hand_installed_recovery_units(self) -> None:
        # a shared name would be adopted/overwritten; distinct names mean the cutover must
        # disable the old pair first (documented), and a stray old pair is visible, not silent
        for stem in UNITS:
            self.assertNotIn(stem, LEGACY_STEMS)
            self.assertFalse(stem.startswith("recovery-"))


class ExpectedUnitsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = yaml.safe_load((SYSTEMD / "expected-units.yaml").read_text(encoding="utf-8"))
        self.recovery = {e["unit"]: e["enabled"] for e in self.doc["recovery"]}

    def test_recovery_expects_templates_and_the_consultant_instances(self) -> None:
        for stem in UNITS:
            self.assertIs(self.recovery[f"{stem}@.service"], False)
            self.assertIs(self.recovery[f"{stem}@.timer"], False)
            self.assertIs(self.recovery[f"{stem}@codex.timer"], True)

    def test_watchman_is_not_expected_before_its_soak(self) -> None:
        self.assertFalse([u for u in self.recovery if "codex-fallback" in u])

    def test_expected_instances_match_the_manifest_gate(self) -> None:
        from tools import persona_installer as pim

        live = pim.Manifest.load(REPO / "instance.yaml")
        cfg = live.installer["codex_seat_timers"]
        accounts = {live.binding(p).account for p in cfg["personas"]}
        for stem in UNITS:
            expected = {u.split("@")[1][: -len(".timer")] for u in self.recovery
                        if u.startswith(f"{stem}@") and u.endswith(".timer") and u != f"{stem}@.timer"}
            self.assertEqual(expected, accounts)

    def test_every_expected_unit_has_a_template(self) -> None:
        for unit in self.recovery:
            if unit.startswith("pantheon-codex-"):
                tmpl = re.sub(r"@[^.]+\.", "@.", unit)
                self.assertTrue((SYSTEMD / tmpl).is_file(), unit)


if __name__ == "__main__":
    unittest.main()
