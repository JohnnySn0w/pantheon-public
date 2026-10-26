"""Seat-state isolation for test modules run under plain ``unittest`` (T-4940).

Under pytest, ``tools/conftest.py``'s autouse ``_isolate_state_dir`` fixture (T-4697)
points ``PANTHEON_STATE_DIR`` at an empty per-test dir, so no test reads the seat's REAL
``~/.local/state/pantheon``. Plain ``python3 -m unittest`` never loads conftest. On a seat
whose production repos manifest is pin-verified with a writable mirror (Recovery: the
Forgejo write mirror), ``_git_utils._resolve_write_mirror`` then arms and adds a
``git remote get-url`` call to every mocked push path. Every test that pins an exact
git-verb sequence fails on that seat only, and passes in CI and on orchestrator's seat.

Modules that assert git-command sequences call ``start()`` from ``setUpModule`` and
``stop()`` from ``tearDownModule``. Under pytest the conftest fixture still sets its own
per-test dir afterwards and wins, so this changes nothing there.
``tools/test_seat_state_isolation_guard.py`` fails if such a module forgets.
"""
from __future__ import annotations

import os
import tempfile

_ENV = "PANTHEON_STATE_DIR"
_state: list[tuple[tempfile.TemporaryDirectory, str | None]] = []


def start() -> str:
    """Point PANTHEON_STATE_DIR at a fresh empty dir; return its path. Nestable."""
    tmp = tempfile.TemporaryDirectory(prefix="seat-state-isolated-")
    _state.append((tmp, os.environ.get(_ENV)))
    os.environ[_ENV] = tmp.name
    return tmp.name


def stop() -> None:
    """Restore the previous PANTHEON_STATE_DIR (or unset it) and remove the dir."""
    if not _state:
        return
    tmp, previous = _state.pop()
    if previous is None:
        os.environ.pop(_ENV, None)
    else:
        os.environ[_ENV] = previous
    tmp.cleanup()
