"""State-machine subsystem for the planner_sandbox reference-implementation.

Covers ORG-080 C2 (parked-resume state-machine reachability + closure) and
the state-store dimension of C4 (fail-closed on primitive unavailability).
See T-3293 (Tranche B) and `tools/germinate/planner_sandbox/README.md`.
"""

from __future__ import annotations
