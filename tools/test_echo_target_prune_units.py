"""T-6121: the echo-target-prune unit pair carries the size cap and runs hourly, and is
listed for orchestrator in expected-units.yaml. Static content checks; nothing touches systemd."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SYSTEMD = REPO / "tools" / "systemd"


class EchoTargetPruneUnitTests(unittest.TestCase):
    def test_service_passes_the_cap(self) -> None:
        text = (SYSTEMD / "pantheon-echo-target-prune.service").read_text(encoding="utf-8")
        execs = re.findall(r"^ExecStart=(.*)$", text, re.M)
        self.assertEqual(execs, [
            "/usr/bin/python3 %h/pantheon-config/tools/echo_target_prune.py --superseded-hours 6 --max-gb 16"])
        self.assertIn("SuccessExitStatus=0", text)

    def test_timer_is_hourly_at_minute_40(self) -> None:
        text = (SYSTEMD / "pantheon-echo-target-prune.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* *:40:00 UTC", text)
        self.assertNotIn("00/4", text)

    def test_expected_units_lists_it_under_orchestrator(self) -> None:
        doc = yaml.safe_load((SYSTEMD / "expected-units.yaml").read_text(encoding="utf-8"))
        orchestrator = {e["unit"]: e["enabled"] for e in doc["orchestrator"]}
        self.assertIs(orchestrator["pantheon-echo-target-prune.timer"], True)
        self.assertIs(orchestrator["pantheon-echo-target-prune.service"], False)


if __name__ == "__main__":
    unittest.main()
