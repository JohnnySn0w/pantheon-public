"""Self-tests for the T-3290 shared real-host fixture surface.

Five tests, one per harness contribution: temp_gate_tree, temp_scratch_dir,
temp_state_store, run_in_sandbox, install_test_target. All exercise real
primitives (real filesystem, real systemd-run --user) -- no mocks. Tests 4
and 5 typed-SKIP (never bare ``OK (skipped=N)``) when systemd-user isn't
usable on the host running the suite (see conftest.require_systemd_user).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from tools.germinate.planner_sandbox.harness.fixtures import (
    temp_gate_tree,
    temp_scratch_dir,
    temp_state_store,
)
from tools.germinate.planner_sandbox.harness.sandbox_runner import run_in_sandbox
from tools.germinate.planner_sandbox.harness.target_fixture import install_test_target

_SEAT_UID_1 = 9999901
_SEAT_UID_2 = 9999902


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_temp_gate_tree_creates_expected_layout() -> None:
    with temp_gate_tree(seat_uids=[_SEAT_UID_1, _SEAT_UID_2]) as tree:
        assert tree.system_dir.is_dir()
        assert _mode(tree.system_dir) == 0o755

        seat1 = tree.seat_dir(_SEAT_UID_1)
        seat2 = tree.seat_dir(_SEAT_UID_2)
        assert seat1.is_dir() and _mode(seat1) == 0o755
        assert seat2.is_dir() and _mode(seat2) == 0o755

        # Real filesystem I/O -- write + read back a leaf gate file (leaf
        # creation is deliberately NOT the harness's job in production
        # shape; here it's just proving the ancestor chain is writable).
        leaf_dir = tree.system_dir / "test-instance" / "test-component"
        leaf_dir.mkdir(parents=True)
        leaf = leaf_dir / "_component_gate.json"
        leaf.write_text(json.dumps({"active_run_id": None}), encoding="utf-8")
        assert json.loads(leaf.read_text(encoding="utf-8")) == {"active_run_id": None}

        root = tree.root

    assert not root.exists()  # real cleanup, not a mocked teardown


def test_temp_scratch_dir_isolated_and_cleaned() -> None:
    with temp_scratch_dir() as scratch_a, temp_scratch_dir() as scratch_b:
        assert scratch_a != scratch_b
        assert _mode(scratch_a) == 0o700
        assert _mode(scratch_b) == 0o700

        (scratch_a / "marker.txt").write_text("a", encoding="utf-8")
        (scratch_b / "marker.txt").write_text("b", encoding="utf-8")
        assert (scratch_a / "marker.txt").read_text(encoding="utf-8") == "a"
        assert (scratch_b / "marker.txt").read_text(encoding="utf-8") == "b"

        path_a, path_b = scratch_a, scratch_b

    assert not path_a.exists()
    assert not path_b.exists()


def test_temp_state_store_round_trips_a_real_record() -> None:
    with temp_state_store() as store:
        record_path = store.record_path(
            instance="test-instance",
            component="test-component",
            scope="system",
            leg="install",
            run_id="01JATESTRUNID00000000000",
        )
        record_path.parent.mkdir(parents=True)
        payload = {"schema": "pantheon-run-state/v1", "state": "PENDING"}

        # Real atomic temp-file + rename, matching the ADR's own pattern
        # (Base engine mechanics: "atomic-file-per-record (temp-file +
        # rename)").
        tmp_path = record_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        os.rename(tmp_path, record_path)

        assert json.loads(record_path.read_text(encoding="utf-8")) == payload


def test_run_in_sandbox_executes_real_systemd_unit(require_systemd_user: None) -> None:
    with temp_scratch_dir() as scratch:
        result = run_in_sandbox("print('hello-from-sandbox')", scratch_dir=scratch)

    assert result.returncode == 0
    assert "hello-from-sandbox" in result.stdout
    # --pipe isolation: systemd-run's own status chatter must never leak
    # into the unit's stdout (empirically verified during Phase 1
    # planning; regression-pinned here).
    assert "Running as unit" not in result.stdout
    assert "Finished with result" not in result.stdout

    # Signal-kill path: systemd-run --wait does NOT follow the shell's
    # 128+signal exit-code convention -- empirically verified (this test,
    # real systemd 255) to return a flat 255 on signal-kill, with the
    # signal evidence recoverable only from stderr. T-3290 Phase 1 flagged
    # this as something to verify rather than assume; real behavior
    # differed from the documented shell convention, so this assertion
    # pins the OBSERVED contract, not the assumed one.
    with temp_scratch_dir() as scratch:
        kill_source = "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n"
        killed = run_in_sandbox(kill_source, scratch_dir=scratch)

    assert killed.returncode == 255
    assert "Finished with result: signal" in killed.stderr
    assert "KILL" in killed.stderr


def test_install_test_target_starts_and_tears_down(require_systemd_user: None) -> None:
    with install_test_target() as target:
        status = subprocess.run(
            ["systemctl", "--user", "is-active", target.unit_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert status.stdout.strip() in ("active", "activating")
        unit_name = target.unit_name

    listed = subprocess.run(
        ["systemctl", "--user", "list-units", "--all", unit_name],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert unit_name not in listed.stdout
