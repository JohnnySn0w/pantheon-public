"""Shared real-host fixture surface for the T-3274 planner_sandbox tranches.

See tools/germinate/planner_sandbox/README.md (Shared harness) and
adr/ORG-080-germination-engine.md (Architectural-tier scope carve, C1-C5)
for what this proves and why. T-3290 lands this harness as the earliest
tranche work; tranches A-D build their own red-before-green acceptance
suites on top of these fixtures.
"""

from .exceptions import HarnessUnavailable
from .fixtures import (
    GateTree,
    StateStore,
    temp_gate_tree,
    temp_scratch_dir,
    temp_state_store,
)
from .sandbox_runner import RunResult, probe_systemd_user, run_in_sandbox
from .target_fixture import TestTarget, install_test_target

__all__ = [
    "GateTree",
    "HarnessUnavailable",
    "RunResult",
    "StateStore",
    "TestTarget",
    "install_test_target",
    "probe_systemd_user",
    "run_in_sandbox",
    "temp_gate_tree",
    "temp_scratch_dir",
    "temp_state_store",
]
