"""T-4697: the autouse conftest guard points PANTHEON_STATE_DIR at a per-test tmp dir, so
no test reads the seat's REAL state (~/.local/state/pantheon), e.g. the manifest pin that
arms _git_utils._resolve_write_mirror and adds a `git remote` call to mocked push paths.

These fail when the guard is missing and the suite runs on a seat with real state. In CI
there is no seat state, so there they pin that the guard stays wired.
"""
from __future__ import annotations

import os
from pathlib import Path

from tools import remote_resolve


def test_state_dir_is_not_the_seats_real_one():
    real = Path.home() / ".local" / "state" / "pantheon"
    d = os.environ.get("PANTHEON_STATE_DIR", "")
    assert d, "the guard must set PANTHEON_STATE_DIR"
    assert Path(d).resolve() != real.resolve()


def test_manifest_pin_is_not_the_seats_real_pin():
    pin = remote_resolve.default_pin_path()
    assert not pin.exists(), pin
    assert Path.home() / ".local" / "state" / "pantheon" not in pin.parents


def test_import_time_state_readers_are_isolated_too():
    """T-4703: modules that read PANTHEON_STATE_DIR at IMPORT (collection time, before
    any fixture runs) must not resolve to the seat's real state dir either."""
    from tools import activity_monitor_drift_check, ci_runner_disk_monitor

    real = (Path.home() / ".local" / "state" / "pantheon").resolve()
    for path in (ci_runner_disk_monitor.STATE_PATH, activity_monitor_drift_check.STATE_DIR):
        assert real not in Path(path).resolve().parents and Path(path).resolve() != real, path
