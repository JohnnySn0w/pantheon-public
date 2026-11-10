"""T-6084: an ORG-076 repair ticket names the faulted seat as `subject:` and the
seat chosen to fix it as `repair-target:` (mail-stale-unmeasurable:codex routed
to recovery used to read 'Subject seat: recovery')."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import episode_store  # noqa: E402


def _argv(fault_id: str, target: str = "recovery") -> list[str]:
    spec = episode_store.DetectorSpec(detector="mail-stale", domain_key="d", subsystem="mailbox",
                                      requester="specialist", harness="codex", account="codex")
    ep = {"episode_id": "ep-1", "fault_id": fault_id, "verdict_kind": "unknown"}
    route = {"kind": "candidate", "target": target, "harness": "claude"}
    return episode_store._file_argv(ep, spec, route, Path("/nonexistent/fields.json"))


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


class RepairTicketLabels(unittest.TestCase):
    def test_subject_is_the_faulted_seat_and_repair_target_is_the_route(self) -> None:
        argv = _argv("mail-stale-unmeasurable:codex")
        labels = _flag(argv, "--labels").split(",")
        self.assertIn("subject:codex", labels)
        self.assertIn("repair-target:recovery", labels)
        self.assertNotIn("subject:recovery", labels)
        ask = _flag(argv, "--ask")
        self.assertIn("Faulted seat: codex.", ask)
        self.assertIn("Repair target seat: recovery", ask)
        self.assertNotIn("Subject seat:", ask)

    def test_fault_id_without_a_persona_gets_no_subject_label(self) -> None:
        argv = _argv("cycle-rate-low")
        labels = _flag(argv, "--labels").split(",")
        self.assertFalse([l for l in labels if l.startswith("subject:")])
        self.assertIn("repair-target:recovery", labels)
        self.assertNotIn("Faulted seat:", _flag(argv, "--ask"))

    def test_routing_target_is_still_the_role_pool(self) -> None:
        argv = _argv("mail-stale-unmeasurable:codex")
        self.assertTrue(_flag(argv, "--target").startswith("role:"))


if __name__ == "__main__":
    unittest.main()
