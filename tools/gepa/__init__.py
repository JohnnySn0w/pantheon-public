"""GEPA reviewer-persona optimization package (T-2809 Phase 2, ORG-074 build-step 1).

Wires the frozen `knowledge/eval/reviewer-v1/` eval set to the `gepa` 0.1.4
library (installed in the dedicated venv at `/home/claude/.venv-gepa`, NOT on
system python) so a human-gated overnight run can propose an improved
`claude-config/agents/reviewer.md` persona with before/after holdout numbers.

Module-import discipline (deliberate, see `adapter.py` docstring): every module
in this package EXCEPT `run_phase2` is free of any runtime dependency on the
`gepa` PyPI package, so `tools/gepa/*` stays importable and unit-testable under
plain system `python3` — mirroring the `tools/a2a` interpreter carve-out
already documented in the `pantheon-dispatch` skill. `run_phase2.py` is the one
module that imports `gepa` at top level; it is a script entrypoint (not
`test_*.py`), so it is never pulled in by `python3 -m pytest tools/ -q` under
system python unless a test explicitly imports it (those tests are
skip-guarded — see `tools/test_gepa_run_phase2.py`).

This package BUILDS the optimization wiring. It does not run it: no file here
invokes `gepa.optimize(...)` except `run_phase2.py`, which is a script the
human/orchestrator runs explicitly (see `run_phase2.sh`).
"""

from __future__ import annotations
