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

T-6118: ``start()`` now also mirrors what ``tools/conftest.py`` does for the Chronicle and persona
roots, because plain ``unittest`` is the repo's documented smoke form and, without conftest,
a test that drives runtime_controller's death / cap-swap / fallback-return paths ran the REAL
session_chronicle subprocess against the account's real ``~/.local/state/pantheon`` (Recovery: 477
fixture session records). It
  * drops PANTHEON_PERSONA_STATE_DIR / PANTHEON_CHRONICLE_ROOT (they outrank PANTHEON_STATE_DIR),
  * sets PANTHEON_SESSION_CHRONICLE_HOOKS=off and PANTHEON_SESSION_CHRONICLE_ALERT=off,
  * stubs runtime_controller's ``_SESSION_CHRONICLE_RUNNER`` seam when that module is loaded.
It does NOT repoint the import-time home constants (DEFAULT_STATE_ROOT, CHRONICLE_ROOT): the resolvers'
fallbacks must keep agreeing (test_t5028_mr2). chronicle.py resolves its default root at CALL time
(round 2), so PANTHEON_STATE_DIR reaches every chronicle write; the session tripwire catches residual leaks.
A test that exercises the hooks sets its own env / seam inside the test body, as under pytest.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

_ENV = "PANTHEON_STATE_DIR"
#: Dropped (they outrank PANTHEON_STATE_DIR) and restored by stop().
_DROPPED_ENV = ("PANTHEON_PERSONA_STATE_DIR", "PANTHEON_CHRONICLE_ROOT")
#: Set to "off" and restored by stop().
_OFF_ENV = ("PANTHEON_SESSION_CHRONICLE_HOOKS", "PANTHEON_SESSION_CHRONICLE_ALERT")


def _stub_session_chronicle_runner(argv, *, timeout):
    del argv, timeout
    return {"status": "stubbed"}


_state: list[tuple[tempfile.TemporaryDirectory, dict[str, str | None], list]] = []


def start() -> str:
    """Point PANTHEON_STATE_DIR at a fresh empty dir; return its path. Nestable."""
    tmp = tempfile.TemporaryDirectory(prefix="seat-state-isolated-")
    names = (_ENV, *_DROPPED_ENV, *_OFF_ENV)
    saved = {name: os.environ.get(name) for name in names}
    patches: list = []
    _state.append((tmp, saved, patches))
    os.environ[_ENV] = tmp.name
    for name in _DROPPED_ENV:
        os.environ.pop(name, None)
    for name in _OFF_ENV:
        os.environ[name] = "off"
    for name in ("tools.runtime_controller", "runtime_controller"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "_SESSION_CHRONICLE_RUNNER"):
            patcher = mock.patch.object(module, "_SESSION_CHRONICLE_RUNNER", _stub_session_chronicle_runner)
            patcher.start()
            patches.append(patcher)
    oom_holds_path = Path(tmp.name) / "no-oom-holds.json"
    for name in ("tools.harness", "harness"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "_OOM_HOLDS_PATH"):
            patcher = mock.patch.object(module, "_OOM_HOLDS_PATH", oom_holds_path)
            patcher.start()
            patches.append(patcher)
    return tmp.name


def stop() -> None:
    """Restore the previous PANTHEON_STATE_DIR (or unset it) and remove the dir."""
    if not _state:
        return
    tmp, saved, patches = _state.pop()
    while patches:
        patches.pop().stop()
    for name, previous in saved.items():
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous
    tmp.cleanup()
