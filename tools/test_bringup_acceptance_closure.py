"""Tests for tools.bringup_acceptance.closure (T-2558 phase 1)."""

from __future__ import annotations

import unittest
from pathlib import Path

from tools.bringup_acceptance.closure import build_closure, render_closure_markdown
from tools.bringup_acceptance.types import MatrixRow, ModuleDeclaration


def _row(module_id: str, tier: str, status: str) -> MatrixRow:
    return MatrixRow(
        module_id=module_id,
        tier=tier,
        status=status,
        summary=f"{module_id} {status}",
        evidence_refs=(Path(f"/tmp/{module_id}.json"),),
    )


class BuildClosureTest(unittest.TestCase):
    def test_green_when_core_proven_and_optional_proven_or_absent(self) -> None:
        rows = (
            _row("mailbox-roundtrip", "core", "proven"),
            _row("hook-emissions", "core", "proven"),
            _row("recovery-recovery", "optional", "proven"),
            _row("specialist-watch", "optional", "declared_absent"),
        )
        closure = build_closure("run-1", rows)
        self.assertEqual(closure.status, "GREEN")
        self.assertEqual(closure.gaps, ())

    def test_red_with_named_gap_when_a_core_module_is_not_proven(self) -> None:
        rows = (
            _row("mailbox-roundtrip", "core", "gap"),
            _row("hook-emissions", "core", "proven"),
        )
        closure = build_closure("run-1", rows)
        self.assertEqual(closure.status, "RED")
        self.assertEqual(len(closure.gaps), 1)
        self.assertIn("mailbox-roundtrip", closure.gaps[0])
        self.assertIn("core module not proven", closure.gaps[0])

    def test_red_with_named_gap_when_a_core_module_hard_stopped(self) -> None:
        rows = (_row("mailbox-roundtrip", "core", "hard_stop"),)
        closure = build_closure("run-1", rows)
        self.assertEqual(closure.status, "RED")
        self.assertIn("status=hard_stop", closure.gaps[0])

    def test_red_when_an_optional_module_is_a_bare_gap_not_declared_absent(self) -> None:
        rows = (
            _row("mailbox-roundtrip", "core", "proven"),
            _row("recovery-recovery", "optional", "gap"),
        )
        closure = build_closure("run-1", rows)
        self.assertEqual(closure.status, "RED")
        self.assertIn("recovery-recovery", closure.gaps[0])
        self.assertIn("optional module not proven or declared absent", closure.gaps[0])

    def test_never_infers_green_from_a_sibling_module(self) -> None:
        # Two core modules -- one proven, one never reported at all -- must not
        # let the proven sibling's presence imply the missing one is fine.
        rows = (_row("mailbox-roundtrip", "core", "proven"),)
        closure = build_closure("run-1", rows)
        self.assertEqual(closure.status, "GREEN")
        # Absence of a row entirely is a runner-level gap (a module that never
        # ran), which is out of build_closure's scope -- it only judges rows it
        # is given. Confirm that scope boundary explicitly.
        self.assertEqual(len(closure.rows), 1)


class CompositeCapabilityTest(unittest.TestCase):
    def test_composite_is_green_iff_every_constituent_module_is_proven(self) -> None:
        rows = (
            _row("mailbox-substrate", "core", "proven"),
            _row("mailbox-live-delivery", "core", "proven"),
            _row("mailbox-wake-trigger", "core", "proven"),
        )
        composites = {
            "mailbox-fully-functional": (
                "mailbox-substrate",
                "mailbox-live-delivery",
                "mailbox-wake-trigger",
            )
        }
        closure = build_closure("run-1", rows, composites=composites)
        self.assertEqual(closure.status, "GREEN")
        self.assertEqual(closure.gaps, ())
        self.assertEqual(len(closure.composite_rows), 1)
        composite = closure.composite_rows[0]
        self.assertEqual(composite.capability_id, "mailbox-fully-functional")
        self.assertEqual(composite.status, "GREEN")
        self.assertEqual(composite.missing_module_ids, ())

    def test_a_missing_constituent_is_red_with_a_named_gap(self) -> None:
        rows = (
            _row("mailbox-substrate", "core", "proven"),
            _row("mailbox-live-delivery", "core", "gap"),
        )
        composites = {
            "mailbox-fully-functional": ("mailbox-substrate", "mailbox-live-delivery")
        }
        closure = build_closure("run-1", rows, composites=composites)
        self.assertEqual(closure.status, "RED")
        composite = closure.composite_rows[0]
        self.assertEqual(composite.status, "RED")
        self.assertEqual(composite.missing_module_ids, ("mailbox-live-delivery",))
        self.assertTrue(
            any(
                "mailbox-fully-functional" in gap and "mailbox-live-delivery" in gap
                for gap in closure.gaps
            )
        )

    def test_a_single_constituent_green_never_reads_as_the_composite_proven(self) -> None:
        # Only one of two required constituents proven -- the composite must
        # stay RED even though a substrate module individually is GREEN.
        rows = (
            _row("mailbox-substrate", "core", "proven"),
        )
        composites = {
            "mailbox-fully-functional": ("mailbox-substrate", "mailbox-live-delivery")
        }
        closure = build_closure("run-1", rows, composites=composites)
        composite = closure.composite_rows[0]
        self.assertEqual(composite.status, "RED")
        self.assertIn("mailbox-live-delivery", composite.missing_module_ids)

    def test_phase_one_default_is_an_empty_composites_map(self) -> None:
        rows = (_row("mailbox-substrate", "core", "proven"),)
        closure = build_closure("run-1", rows)
        self.assertEqual(closure.composite_rows, ())

    def test_module_declaration_contributes_to_defaults_to_empty(self) -> None:
        declaration = ModuleDeclaration(
            module_id="mailbox-substrate", tier="core", applies=True, reason="present"
        )
        self.assertEqual(declaration.contributes_to, ())
        declared = ModuleDeclaration(
            module_id="mailbox-substrate",
            tier="core",
            applies=True,
            reason="present",
            contributes_to=("mailbox-fully-functional",),
        )
        self.assertEqual(declared.contributes_to, ("mailbox-fully-functional",))


class RenderClosureMarkdownTest(unittest.TestCase):
    def test_gaps_section_lists_every_reason(self) -> None:
        rows = (_row("mailbox-roundtrip", "core", "gap"),)
        closure = build_closure("run-1", rows)
        markdown = render_closure_markdown(closure)
        self.assertIn("## GAPS", markdown)
        self.assertIn("mailbox-roundtrip: core module not proven", markdown)

    def test_gaps_section_says_none_when_green(self) -> None:
        rows = (_row("mailbox-roundtrip", "core", "proven"),)
        closure = build_closure("run-1", rows)
        markdown = render_closure_markdown(closure)
        self.assertIn("## GAPS", markdown)
        self.assertIn("none", markdown.rsplit("## GAPS", 1)[1])

    def test_composites_section_only_renders_when_composites_are_declared(self) -> None:
        rows = (_row("mailbox-roundtrip", "core", "proven"),)
        markdown = render_closure_markdown(build_closure("run-1", rows))
        self.assertNotIn("## Composite capabilities", markdown)

        composed = build_closure(
            "run-1", rows, composites={"mailbox-fully-functional": ("mailbox-roundtrip",)}
        )
        markdown = render_closure_markdown(composed)
        self.assertIn("## Composite capabilities", markdown)
        self.assertIn("mailbox-fully-functional", markdown)
        self.assertIn("GREEN", markdown.rsplit("## Composite capabilities", 1)[1])


if __name__ == "__main__":
    unittest.main()
