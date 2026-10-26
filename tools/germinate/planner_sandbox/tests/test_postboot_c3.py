"""C3 red-before-green tests: postboot-completion precondition, universal
across invocation surfaces (adr/ORG-080-germination-engine.md
Architectural-tier scope carve).

Four tests, one per invocation-surface transport, all real primitives
(no mocks): a subprocess argv-driven CLI shim (C3-1), a real
``systemd-run --user`` transient unit whose entry point bypasses any CLI
(C3-2, the Consultant v9 BLOCK-3 witness), a bare in-process library call with
zero CLI code loaded (C3-3), and a boot-id-staleness check exercised at
the library tier (C3-4) -- see the T-3294 DECISIONS (ticket transition
log) for why staleness isn't independently re-proven across all three
transports: C3-1/2/3 already establish every surface calls the
identical ``enforcement.require_or_refuse()``, so proving the staleness
branch once at the shared call site is structurally sufficient coverage,
not a shortcut.

C3-2 typed-SKIPs (never a bare "skipped") via conftest's
``require_systemd_user`` fixture when systemd-user isn't usable on the
host running the suite.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.germinate.planner_sandbox.harness.fixtures import temp_scratch_dir
from tools.germinate.planner_sandbox.harness.sandbox_runner import (
    DEFAULT_PROPERTIES,
    run_in_sandbox,
)
from tools.germinate.planner_sandbox.postboot import enforcement, marker, substrate_ordering
from tools.germinate.planner_sandbox.postboot.exceptions import (
    PostbootPreconditionUnmet,
    PrimitiveUnavailable,
)

#: tests/ -> planner_sandbox -> germinate -> tools -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]

#: CLI-surface test-only shim (C3-1). A minimal argv-driven entry point
#: standing in for a real ``germinate <verb>`` invocation -- this is a
#: ref-impl and no real CLI exists yet (T-3294 ask). Faithful to "CLI
#: surface" because (a) it's invoked as a genuinely separate OS process
#: via argv, matching a real ``germinate <verb>`` exec boundary, and (b)
#: it does NOT reimplement its own postboot check -- it calls the exact
#: same ``enforcement.require_or_refuse()`` every other surface calls,
#: which is the property C3 needs proven (the CLI doesn't have a
#: parallel/divergent gate, it just consumes the shared one).
_CLI_SHIM_TEMPLATE = """
import sys
from pathlib import Path
from tools.germinate.planner_sandbox.postboot import marker, enforcement
from tools.germinate.planner_sandbox.postboot.exceptions import (
    PostbootPreconditionUnmet,
    PrimitiveUnavailable,
)

marker.MARKER_PATH = Path({marker_path!r})
verb = sys.argv[1] if len(sys.argv) > 1 else "plan"
try:
    enforcement.require_or_refuse()
except (PostbootPreconditionUnmet, PrimitiveUnavailable) as exc:
    sys.stderr.write("REFUSED: " + str(exc) + "\\n")
    sys.exit(1)
sys.stdout.write("OK: verb=" + verb + "\\n")
sys.exit(0)
"""

#: Systemd-unit-surface probe (C3-2). Deliberately bypasses any CLI --
#: imports and calls the engine internal directly, exactly the Consultant v9
#: BLOCK-3 concern (a component unit's ExecStart invoking engine
#: internals directly, skipping a CLI-tier-only check). Proves closure
#: is at the enforcement tier BELOW where bypass could occur (inside the
#: library both the CLI and this probe call), not at a CLI wrapper.
_EXECSTART_PROBE_TEMPLATE = """
import sys
from pathlib import Path
from tools.germinate.planner_sandbox.postboot import marker, enforcement
from tools.germinate.planner_sandbox.postboot.exceptions import (
    PostbootPreconditionUnmet,
    PrimitiveUnavailable,
)

marker.MARKER_PATH = Path({marker_path!r})
try:
    enforcement.require_or_refuse()
except (PostbootPreconditionUnmet, PrimitiveUnavailable) as exc:
    sys.stdout.write("REFUSED: " + str(exc) + "\\n")
    sys.exit(1)
sys.stdout.write("ADMITTED\\n")
sys.exit(0)
"""


def _run_cli_shim(shim_source: str, *argv: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-c", shim_source, *argv],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )


def test_c3_1_cli_surface_refuses_before_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with temp_scratch_dir() as scratch:
        marker_path = scratch / "postboot-complete.marker"
        shim_source = _CLI_SHIM_TEMPLATE.format(marker_path=str(marker_path))

        before = _run_cli_shim(shim_source, "plan")
        assert before.returncode == 1
        assert "postboot precondition unmet" in before.stderr
        assert "absent" in before.stderr

        monkeypatch.setattr(marker, "MARKER_PATH", marker_path)
        marker.write_marker(marker.current_boot_id())

        after = _run_cli_shim(shim_source, "plan")
        assert after.returncode == 0
        assert "OK: verb=plan" in after.stdout


def test_c3_2_systemd_unit_surface_refuses_before_marker(
    require_systemd_user: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with temp_scratch_dir() as scratch:
        marker_path = scratch / "postboot-complete.marker"
        probe_source = _EXECSTART_PROBE_TEMPLATE.format(marker_path=str(marker_path))
        properties = (*DEFAULT_PROPERTIES, f"Environment=PYTHONPATH={_REPO_ROOT}")

        # Demonstrate the closure via the CHOSEN substrate primitive
        # (T-3294 ask): the probe this test runs under a real
        # `systemd-run --user` unit is itself gated per the structural
        # lint substrate_ordering.py provides -- and an ungated script
        # (e.g. one that never touches postboot at all) is correctly
        # NOT flagged as gated, proving the check isn't a tautology.
        assert substrate_ordering.source_is_gated(probe_source)
        assert not substrate_ordering.source_is_gated("print('no gate here')")
        # The module's own reference prelude (what a real production
        # unit-generator would emit) must pass its own lint too.
        assert substrate_ordering.source_is_gated(substrate_ordering.gate_prelude_source())

        before = run_in_sandbox(probe_source, scratch_dir=scratch, properties=properties)
        assert before.returncode != 0
        assert "REFUSED" in before.stdout
        assert "absent" in before.stdout

        monkeypatch.setattr(marker, "MARKER_PATH", marker_path)
        marker.write_marker(marker.current_boot_id())

        after = run_in_sandbox(probe_source, scratch_dir=scratch, properties=properties)
        assert after.returncode == 0
        assert "ADMITTED" in after.stdout


def test_c3_3_library_call_surface_refuses_before_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No CLI-shim code loaded anywhere in this process -- the structural
    # proof that the gate lives INSIDE the library, not at a CLI
    # wrapper: the identical exception fires on a bare import+call with
    # zero CLI machinery involved.
    with temp_scratch_dir() as scratch:
        marker_path = scratch / "postboot-complete.marker"
        monkeypatch.setattr(marker, "MARKER_PATH", marker_path)

        with pytest.raises(PostbootPreconditionUnmet) as exc_info:
            enforcement.require_or_refuse()
        assert "absent" in str(exc_info.value)

        marker.write_marker(marker.current_boot_id())
        enforcement.require_or_refuse()  # admitted -- no raise


def test_c3_4_boot_id_staleness_refused_as_stale_not_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with temp_scratch_dir() as scratch:
        marker_path = scratch / "postboot-complete.marker"
        monkeypatch.setattr(marker, "MARKER_PATH", marker_path)

        real_boot_id = marker.current_boot_id()
        stale_boot_id = "00000000-0000-0000-0000-000000000000"
        if stale_boot_id == real_boot_id:
            stale_boot_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        marker.write_marker(stale_boot_id)

        with pytest.raises(PostbootPreconditionUnmet) as exc_info:
            enforcement.require_or_refuse()

        reason = str(exc_info.value)
        assert "stale" in reason
        assert "absent" not in reason
        assert stale_boot_id in reason
        assert real_boot_id in reason
