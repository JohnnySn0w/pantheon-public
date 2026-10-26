"""validate_successor_ack for operator-triggered transitions (T-4732).

A transition written by the 2026-08-14 target-live recovery recorded the operator
handoff only as ``successor_read_target`` and left ``chronicle_evidence`` null, so the
sanctioned ack could never pass and the seat's runtime transitions stayed refused.
The validator now falls back to ``successor_read_target`` for that legacy shape; the
sha256 check against ``operator_handoff_sha256`` stays mandatory either way.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from tools.harness_adapter.integration import integration_for


class OperatorSuccessorAckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.handoff = Path(self.tmp.name) / "operator-handoff-t.md"
        self.handoff.write_text("operator handoff\n")
        self.sha = hashlib.sha256(self.handoff.read_bytes()).hexdigest()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _state(self, **over):
        state = {
            "trigger": "operator",
            "expected_successor_read_evidence": "hook:runtime-transfer:t:operator-handoff-t.md",
            "operator_handoff_sha256": self.sha,
            "chronicle_evidence": str(self.handoff),
        }
        state.update(over)
        return state

    def _ack(self, state, harness: str = "claude") -> None:
        integration_for(harness).validate_successor_ack(state, state["expected_successor_read_evidence"])

    def test_current_shape_passes(self) -> None:
        self._ack(self._state())

    def test_legacy_shape_falls_back_to_successor_read_target(self) -> None:
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                self._ack(self._state(chronicle_evidence=None, successor_read_target=str(self.handoff)), harness)

    def test_legacy_shape_still_checks_the_hash(self) -> None:
        self.handoff.write_text("tampered\n")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self._ack(self._state(chronicle_evidence=None, successor_read_target=str(self.handoff)))

    def test_missing_both_targets_still_refuses(self) -> None:
        with self.assertRaisesRegex(ValueError, "operator handoff target is missing"):
            self._ack(self._state(chronicle_evidence=None))

    def test_chronicle_evidence_wins_over_successor_read_target(self) -> None:
        other = Path(self.tmp.name) / "other.md"
        other.write_text("other\n")
        self._ack(self._state(successor_read_target=str(other)))


if __name__ == "__main__":
    unittest.main()
