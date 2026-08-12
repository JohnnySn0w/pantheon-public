#!/usr/bin/env python3
"""Run the repo's `tools/test_*.sh` suites under the normal pytest smoke loop.

T-2278. Six shell suites lived in `tools/`, were documented, and were executed
by NOTHING: the smoke loop is `python3 -m unittest tools.test_<module>` /
`pytest tools`, neither of which can collect a bash file. They fired only when a
human remembered them.

WHY THAT IS THE ARC'S OWN THESIS, not a nit. T-2245 exists because 4266 unit
tests stayed green through a 90-minute delivery outage — tests aimed one stratum
below where the failure lived. A test no runner executes is that defect taken to
its limit: **it cannot even be green.** Its firing probability is "did somebody
think to run it", which `lessons/2026-07-28-failure-presents-as-absence.md` names
as the absence of a detection strategy rather than one.

What they actually guard is not theoretical — e.g.
`test_acp_bridge_supervise_session_death.sh` kills a supervised session
underneath the supervisor and asserts the loop EXITS within 15s, because a
supervisor that outlives its dead child keeps the unit `active` and nothing ever
restarts it. That is a guard on the exact 2026-07-28 failure.

DISCOVERY IS BY GLOB, deliberately. Enumerating the six by name here would
reproduce the defect one layer up: a seventh suite would be added and silently
not run, and the list would read as coverage. `test_every_shell_suite_is_run`
below pins that the glob is non-empty, so an empty match fails loudly instead of
passing vacuously — a zero-length parametrize is otherwise indistinguishable
from "all green".

COST, measured 2026-08-02 rather than estimated: ~99s serial for all six, of
which `test_handoff_and_restart.sh` alone is ~72s. That is roughly +50% on a
~200s full run. Accepted rather than optimised away — an opt-out defaulting to
"skip the slow one" is how this becomes manual-only-by-accident again. If the
cost becomes a real problem that is a measured future decision, not a
preemptive one.

PARALLELISM: not tested, and not currently in play — the documented smoke
command is `python3 -m pytest tools -q` with no `-n`, so collection is serial in
one process. Stated rather than claimed either way; if xdist is ever adopted,
these want re-checking (several allocate a sandbox HOME and a stub tmux on PATH).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent
SHELL_SUITES = sorted(TOOLS.glob("test_*.sh"))

# Generous: the slowest measured at ~72s. A shell suite that runs long has
# either hung or regressed, and both should surface as a failure rather than as
# a pytest-level timeout with no attribution.
_TIMEOUT_SECONDS = 300


def test_every_shell_suite_is_run() -> None:
    """The glob must match something.

    Without this, deleting or renaming every `test_*.sh` would leave a
    zero-length parametrize that reports all-green — the vacuous-pass shape this
    file exists to close.
    """
    assert SHELL_SUITES, (
        "no tools/test_*.sh matched — either they were removed (update this file) "
        "or the glob broke, and a zero-length parametrize passes vacuously"
    )


@pytest.mark.parametrize("script", SHELL_SUITES, ids=lambda p: p.name)
def test_shell_suite_passes(script: Path) -> None:
    """Each `tools/test_*.sh` must exit 0.

    Output is attached to the failure rather than swallowed: a shell suite that
    fails silently under a runner is barely better than one that never runs.
    """
    proc = subprocess.run(
        ["bash", str(script)],
        cwd=str(TOOLS.parent),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-40:])
        pytest.fail(f"{script.name} exited {proc.returncode}\n--- last 40 lines ---\n{tail}")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
