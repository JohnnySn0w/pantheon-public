"""T-2968 BT2 — unit-file contract test for pantheon-runtime-controller@.service.

Validates the systemd unit template's directives match the mid-life
restart-fixable supervisor contract (ORG-068 SS87): Type=simple +
Restart=always turns BT1's single-cycle `controller_main` exit into the
swap-on-death loop. Does NOT re-test `controller_main`'s internal selection
logic — that's already covered by `test_runtime_controller.py` via its
injectable seams.
"""

import unittest
from pathlib import Path

UNIT_PATH = Path(__file__).resolve().parent / "systemd" / "pantheon-runtime-controller@.service"


def _parse_unit(text: str) -> dict[str, list[str]]:
    """Group directive lines by section; systemd allows repeated keys per section."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return sections


class PantheonRuntimeControllerUnitTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(UNIT_PATH.is_file(), f"missing unit template: {UNIT_PATH}")
        self.text = UNIT_PATH.read_text(encoding="utf-8")
        self.sections = _parse_unit(self.text)

    def test_templated_instance_name(self):
        self.assertTrue(UNIT_PATH.name.startswith("pantheon-runtime-controller@"))
        self.assertIn("%i", self.text)

    def test_unit_section_has_failure_alert_and_start_limit(self):
        unit_lines = self.sections.get("Unit", [])
        self.assertIn("OnFailure=pantheon-unit-failure-alert@%n.service", unit_lines)
        self.assertIn("StartLimitIntervalSec=600", unit_lines)
        self.assertIn("StartLimitBurst=20", unit_lines)

    def test_service_is_simple_restart_always(self):
        service_lines = self.sections.get("Service", [])
        self.assertIn("Type=simple", service_lines)
        self.assertIn("Restart=always", service_lines)
        self.assertIn("RestartSec=5", service_lines)

    def test_exec_start_invokes_controller_main_with_persona(self):
        service_lines = self.sections.get("Service", [])
        exec_start = next((line for line in service_lines if line.startswith("ExecStart=")), None)
        self.assertIsNotNone(exec_start, "no ExecStart= line found")
        # T-2970 BT5: must be module-mode (`-m tools.runtime_controller`),
        # NOT a bare-script path (`.../tools/runtime_controller.py`) -- the
        # bare-script shape crashes on the module's own `from tools import
        # ...` self-imports (only the script's own dir lands on
        # sys.path[0], never its parent), confirmed live-fire on specialist
        # (immediate ModuleNotFoundError, crash-looped until fixed). This
        # assertion guards against regressing back to that shape.
        self.assertIn("-m tools.runtime_controller", exec_start)
        self.assertNotIn("runtime_controller.py", exec_start)
        self.assertIn("controller-main", exec_start)
        self.assertIn("--persona %i", exec_start)

    def test_install_section_wants_default_target(self):
        install_lines = self.sections.get("Install", [])
        self.assertIn("WantedBy=default.target", install_lines)

    def test_no_timer_companion(self):
        # Type=simple + Restart=always is continuously-supervised, not
        # cron-fired -- a .timer sibling would be the wrong shape (matches
        # pantheon-acp-bridge@/claude-telegram, neither
        # of which have a .timer either).
        timer_path = UNIT_PATH.with_suffix(".timer")
        self.assertFalse(timer_path.exists(), "controller unit must not have a .timer companion")

    def test_no_anthropic_or_claude_code_environment_leak(self):
        # Money-trap guard documented in pantheon-acp-bridge@.service: a
        # leaked ANTHROPIC_*/CLAUDE_CODE_* Environment= line silently flips
        # subscription billing to metered API billing.
        for line in self.sections.get("Service", []):
            if line.startswith("Environment="):
                self.assertNotRegex(line, r"^Environment=(ANTHROPIC_|CLAUDE_CODE_)")


if __name__ == "__main__":
    unittest.main()
