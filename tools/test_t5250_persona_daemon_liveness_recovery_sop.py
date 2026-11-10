"""T-5250 contract tests for the controller-owned recovery recipe."""

from __future__ import annotations

import unittest
from pathlib import Path

from tools.persona_orientation import render


REPO_ROOT = Path(__file__).resolve().parents[1]
SOP_PATH = REPO_ROOT / "sop" / "remediation" / "persona-daemon-liveness-recovery.md"
HANDOFF_FLAG = "--handoff-file /home/<ACCT>/HANDOFF.md"
SINGLE_OWNER_WARNING = "sole owner of the cycle"
RACE_WARNING = "respawn the selected runtime over the fresh pane"


def controller_cycle_step(text: str) -> str:
    """Return only ranked remediation step 1 for controller-owned seats."""
    start = text.index("1. **Cycle the session through the controller**")
    end = text.index("2. **Pane wedged so hard", start)
    return text[start:end]


class PersonaDaemonLivenessRecoveryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sop = SOP_PATH.read_text(encoding="utf-8")
        self.step = controller_cycle_step(self.sop)

    def test_canonical_cycle_is_seat_explicit_and_single_owner(self) -> None:
        self.assertIn(HANDOFF_FLAG, self.step)
        self.assertIn(SINGLE_OWNER_WARNING, self.step)
        self.assertIn("Do not separately restart `pantheon-persona-consume@<p>.service`", self.step)
        self.assertIn("before, during, or immediately after", self.step)
        self.assertIn(RACE_WARNING, self.step)
        self.assertIn("after this command returns", self.step)

    def test_dual_harness_renders_preserve_the_recovery_contract(self) -> None:
        artifacts, _bindings = render.render_sop_skills(REPO_ROOT, ("claude", "codex"))
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                skill = artifacts[f"sop:persona-daemon-liveness-recovery:{harness}"]
                step = controller_cycle_step(skill)
                self.assertIn(HANDOFF_FLAG, step)
                self.assertIn(SINGLE_OWNER_WARNING, step)
                self.assertIn(RACE_WARNING, step)


if __name__ == "__main__":
    unittest.main()
