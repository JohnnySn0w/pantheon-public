"""D6 (T-2917 5c): the broker-synthesized timer-enable rollback intent.

A timer-enable declares no inverse (its before-state is only knowable at the
executor), so the broker synthesizes ONE write-ahead intent for it. This
pins the exact shape: kind-limited to timer-enable, seq in plan order,
before_basis executor-precondition, cleanup_authority
requires-owned-mutation-record, and no other kind ever synthesized.
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

from tools import test_host_control_core as _core  # module import: never re-collect its TestCases
from tools.host_control.operations import (
    SYNTHESIZED_INTENT_KINDS,
    SYNTHESIZED_TIMER_ENABLE_INVERSE,
    OperationService,
)
from tools.host_control.planner import partition_installer_plan
from tools.persona_installer import Manifest, PersonaInstaller

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_INVERSE = {
    "action": "timer-enable-rollback",
    "synthesized_by": "broker",
    "before_basis": "executor-precondition",
    "cleanup_authority": "requires-owned-mutation-record",
}


def _recovery_plan():
    with tempfile.TemporaryDirectory() as work:
        manifest = Path(work) / "instance.yaml"
        manifest.write_text(yaml.safe_dump(_core._manifest_dict(), sort_keys=False), encoding="utf-8")
        full = PersonaInstaller(REPO_ROOT).plan(
            Manifest.load(manifest), "recovery", harness_override="codex", root=Path("/projection")
        )
        # T-5447: the BROKER plan (admitted kinds only); the seat-core periodic ops
        # are seat-local residue and never reach the broker's intents.
        return partition_installer_plan(full)[0]


def _intents(operations):
    record = SimpleNamespace(operation_handle="ab" * 32)
    plan = SimpleNamespace(installer_plan=SimpleNamespace(operations=tuple(operations)))
    return OperationService._plan_rollback_intents(record, plan, created_at="2026-10-13T00:00:00+00:00")


class SynthesizedTimerEnableIntentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan = _recovery_plan()

    def test_constants_are_the_ruled_shape(self) -> None:
        self.assertEqual(SYNTHESIZED_INTENT_KINDS, frozenset({"timer-enable"}))
        self.assertEqual(dict(SYNTHESIZED_TIMER_ENABLE_INVERSE), EXPECTED_INVERSE)
        with self.assertRaises(TypeError):
            SYNTHESIZED_TIMER_ENABLE_INVERSE["cleanup_authority"] = "x"  # type: ignore[index]

    def test_real_plan_gets_exactly_one_synthesized_timer_enable_intent(self) -> None:
        operations = self.plan.operations
        intents = _intents(operations)
        declared = [op for op in operations if op.inverse]
        self.assertEqual(len(declared), 14)
        self.assertEqual(len(intents), 15)
        synthesized = [item for item in intents if dict(item.inverse) == EXPECTED_INVERSE]
        self.assertEqual(len(synthesized), 1)
        intent = synthesized[0]
        self.assertEqual(intent.kind, "timer-enable")
        timer_enable = next(op for op in operations if op.kind == "timer-enable")
        self.assertEqual(intent.operation_id, timer_enable.operation_id)
        self.assertEqual(intent.target, timer_enable.target)
        self.assertEqual(intent.owner, timer_enable.owner)

    def test_seq_follows_plan_order(self) -> None:
        operations = self.plan.operations
        intents = _intents(operations)
        self.assertEqual([item.seq for item in intents], list(range(1, len(intents) + 1)))
        order = {op.operation_id: index for index, op in enumerate(operations)}
        self.assertEqual(
            [item.operation_id for item in intents],
            sorted((item.operation_id for item in intents), key=order.__getitem__),
        )
        # declared inverses are carried unchanged
        for item in intents:
            if item.kind != "timer-enable":
                source = next(op for op in operations if op.operation_id == item.operation_id)
                self.assertEqual(dict(item.inverse), dict(source.inverse))

    def test_no_other_kind_is_ever_synthesized(self) -> None:
        base = self.plan.operations[0]
        kinds = sorted({op.kind for op in self.plan.operations} | {"daemon-reload", "unit-fire", "timer-observe",
                                                                   "direct-fire", "stale-unit-failure-reset",
                                                                   "mkdir", "future-kind"})
        for kind in kinds:
            with self.subTest(kind=kind):
                op = dataclasses.replace(base, operation_id=f"x.{kind}", kind=kind, inverse=None)
                if kind in SYNTHESIZED_INTENT_KINDS:
                    intents = _intents((op,))
                    self.assertEqual(len(intents), 1)
                    self.assertEqual(dict(intents[0].inverse), EXPECTED_INVERSE)
                else:
                    with self.assertRaises(Exception) as caught:
                        _intents((op,))
                    self.assertIn("no invertible operations", str(caught.exception))

    def test_a_declared_timer_enable_inverse_wins(self) -> None:
        base = next(op for op in self.plan.operations if op.kind == "timer-enable")
        declared = dataclasses.replace(base, inverse={"action": "declared"})
        intents = _intents((declared,))
        self.assertEqual(dict(intents[0].inverse), {"action": "declared"})


if __name__ == "__main__":
    unittest.main()
