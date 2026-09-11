"""C4 (fail-closed on primitive unavailability) -- Tranche A's slice.

This file is designed for cross-tranche appendability (PO broadcast,
T-3292 plan approval): tranche-specific naming
(``test_fail_closed_c4_admission.py``) avoids merge collisions with
sibling tranches' own C4 files (``_state_store.py`` / ``_postboot.py`` /
``_no_swallow.py``). Each test function is independent -- no shared
module-scope state that presumes only this tranche's test exists.
"""

from __future__ import annotations

import pytest

from tools.germinate.planner_sandbox.admission import bootstrap_component, gate
from tools.germinate.planner_sandbox.admission.exceptions import PrimitiveUnavailable
from tools.germinate.planner_sandbox.harness.fixtures import temp_gate_tree


def test_c4_coordination_primitive_unreachable() -> None:
    """Rename the gate tree root; the next admission attempt refuses
    with the primitive name in the error, not a permit."""

    with temp_gate_tree() as tree:
        bootstrap_component(tree.system_dir, "c4-instance", "c4-component")

        with gate.acquire(
            tree.system_dir, "c4-instance", "c4-component", "plan", scope="system", uid=1000
        ):
            pass  # prove the primitive works at all before breaking it

        renamed_root = tree.root.with_name(tree.root.name + "-renamed-unreachable")
        tree.root.rename(renamed_root)
        try:
            with pytest.raises(PrimitiveUnavailable) as exc_info:
                with gate.acquire(
                    tree.system_dir,
                    "c4-instance",
                    "c4-component",
                    "plan",
                    scope="system",
                    uid=1000,
                ):
                    pass
            assert exc_info.value.name == "coordination-lock"
            assert exc_info.value.reason, "PrimitiveUnavailable must carry a concrete reason"
        finally:
            renamed_root.rename(tree.root)
