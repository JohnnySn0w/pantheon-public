"""Tests for tools.bringup_acceptance.recorder (T-2558 phase 1)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.bringup_acceptance.recorder import (
    TRACE_SCHEMA,
    ZERO_HASH,
    TraceRecorder,
    make_module_evidence_root,
    require_live_gates,
    validate_token,
    write_json_evidence,
)
from tools.bringup_acceptance.types import HardStop


class TraceRecorderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.evidence_root = Path(self._tmp.name)

    def test_append_and_read_round_trip_in_order(self) -> None:
        recorder = TraceRecorder(self.evidence_root, module_id="mailbox-roundtrip", run_id="run-1")
        first = recorder.append("init", {"operator": "orchestrator"})
        second = recorder.append("observe", {"nonce": "abc123"})
        events = recorder.read()
        self.assertEqual([event.phase for event in events], ["init", "observe"])
        self.assertEqual(events[0].previous_hash, ZERO_HASH)
        self.assertEqual(events[1].previous_hash, first.event_hash)
        self.assertEqual(events[-1].event_hash, second.event_hash)
        self.assertEqual(events[0].schema, TRACE_SCHEMA)
        self.assertEqual(events[0].module_id, "mailbox-roundtrip")
        self.assertEqual(events[0].run_id, "run-1")

    def test_evidence_path_is_module_id_keyed_under_run_id(self) -> None:
        recorder = TraceRecorder(self.evidence_root, module_id="hook-emissions", run_id="run-2")
        self.assertEqual(
            recorder.root, self.evidence_root / "run-2" / "modules" / "hook-emissions"
        )
        self.assertEqual(recorder.path, recorder.root / "trace.jsonl")

    def test_tampering_with_a_stored_line_is_detected_on_read(self) -> None:
        recorder = TraceRecorder(self.evidence_root, module_id="mailbox-roundtrip", run_id="run-3")
        recorder.append("init", {"operator": "orchestrator"})
        recorder.append("observe", {"nonce": "abc123"})
        document = json.loads(recorder.path.read_text(encoding="utf-8").splitlines()[1])
        document["payload"]["nonce"] = "tampered"
        lines = recorder.path.read_text(encoding="utf-8").splitlines()
        lines[1] = json.dumps(document)
        recorder.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(HardStop, "event hash mismatch"):
            recorder.read()

    def test_reordered_lines_break_the_sequence_check(self) -> None:
        recorder = TraceRecorder(self.evidence_root, module_id="mailbox-roundtrip", run_id="run-4")
        recorder.append("init", {})
        recorder.append("observe", {})
        lines = recorder.path.read_text(encoding="utf-8").splitlines()
        recorder.path.write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")
        with self.assertRaises(HardStop):
            recorder.read()

    def test_a_trace_from_a_different_module_id_is_rejected(self) -> None:
        recorder = TraceRecorder(self.evidence_root, module_id="mailbox-roundtrip", run_id="run-5")
        recorder.append("init", {})
        other = TraceRecorder.__new__(TraceRecorder)
        other.module_id = "different-module"
        other.run_id = "run-5"
        other.root = recorder.root
        other.path = recorder.path
        with self.assertRaisesRegex(HardStop, "module_id mismatch"):
            other.read()

    def test_recorder_never_interprets_a_verdict_field_it_only_stores_the_payload(self) -> None:
        # The invariant lives in the caller contract (a module re-observes ground
        # truth before calling append), not in the recorder rejecting a key name --
        # the recorder is a dumb, faithful, tamper-evident store of whatever payload
        # a module hands it after that re-observation.
        recorder = TraceRecorder(self.evidence_root, module_id="mailbox-roundtrip", run_id="run-6")
        recorder.append("observe", {"verdict": "passed", "ground_truth_path": "/tmp/x"})
        events = recorder.read()
        self.assertEqual(events[0].payload["verdict"], "passed")


class TokenValidationTest(unittest.TestCase):
    def test_rejects_empty_and_path_traversal_tokens(self) -> None:
        for bad in ("", "..", "a/b", "a/../b", "../../etc"):
            with self.assertRaises(HardStop):
                validate_token(bad, label="module_id")

    def test_accepts_bounded_identifier_tokens(self) -> None:
        for good in ("mailbox-roundtrip", "T2382_LEG", "run.20260805", "a"):
            self.assertEqual(validate_token(good, label="module_id"), good)

    def test_make_module_evidence_root_rejects_bad_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HardStop):
                make_module_evidence_root(Path(tmp), run_id="../escape", module_id="ok")


class WriteJsonEvidenceTest(unittest.TestCase):
    def test_write_json_evidence_is_readable_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "value.json"
            write_json_evidence(target, {"a": 1, "b": Path("/tmp/x")})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"a": 1, "b": "/tmp/x"})


class RequireLiveGatesTest(unittest.TestCase):
    def test_missing_gate_raises_hard_stop(self) -> None:
        with self.assertRaisesRegex(HardStop, "MY_GATE"):
            require_live_gates(module_id="swap", required_gates=("MY_GATE",), env={})

    def test_present_gate_is_a_no_op(self) -> None:
        require_live_gates(module_id="swap", required_gates=("MY_GATE",), env={"MY_GATE": "1"})

    def test_gate_names_are_caller_supplied_not_hardcoded(self) -> None:
        # No T2382_LIVE_* residue: any gate name the caller passes works.
        require_live_gates(
            module_id="swap", required_gates=("ANYTHING_AT_ALL",), env={"ANYTHING_AT_ALL": "yes"}
        )


if __name__ == "__main__":
    unittest.main()
