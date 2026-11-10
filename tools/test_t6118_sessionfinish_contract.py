"""T-6118: the contract of tools/conftest.py::pytest_sessionfinish, which is sanctioned in
test_ci_integrity._SANCTIONED_HOOKS on exactly this basis.

The hook may only flip exit status 0 to TESTS_FAILED (and only when the tripwire sees changes);
it never changes any other status, never changes anything under PANTHEON_REAL_STATE_TRIPWIRE=warn
or =off, and never raises. Driven directly with a fake session; no sub-pytest. Skipped under plain
unittest, where conftest is not loaded."""
from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock

import pytest

from tools import _real_state_tripwire as tw

_BEFORE = {"personas": {}, "chronicle": {}}
_CHANGED = {"personas": {"recovery": (None, None)}, "chronicle": {}}
_ALL_NONZERO = (1, 2, 3, 4, 5) + tuple(c for c in pytest.ExitCode if int(c) != 0)


def _loaded_conftest():
    for name in ("tools.conftest", "conftest"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "pytest_sessionfinish"):
            return module
    return None


class SessionFinishContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conftest = _loaded_conftest()
        if self.conftest is None:
            self.skipTest("tools/conftest.py not loaded (plain unittest)")

    def _run(self, start, after, *, env=None, baseline=_BEFORE, snapshot_error=None):
        session = types.SimpleNamespace(exitstatus=start, config=types.SimpleNamespace())
        environ = {"PANTHEON_REAL_STATE_TRIPWIRE": env} if env else {}
        snap = mock.patch.object(tw, "take_snapshot", return_value=after, side_effect=snapshot_error)
        with mock.patch.object(self.conftest, "_REAL_STATE_BASELINE", baseline), snap, \
                mock.patch.dict(os.environ, environ), mock.patch("sys.stderr"):
            if env is None:
                os.environ.pop("PANTHEON_REAL_STATE_TRIPWIRE", None)
            self.conftest.pytest_sessionfinish(session, start)
        return session.exitstatus

    def test_nonzero_status_is_never_changed_even_with_changes(self) -> None:
        for start in _ALL_NONZERO:
            with self.subTest(start=repr(start)):
                self.assertIs(self._run(start, _CHANGED), start)

    def test_zero_flips_to_tests_failed_only_with_changes(self) -> None:
        with self.subTest(case="changes"):
            self.assertEqual(self._run(0, _CHANGED), pytest.ExitCode.TESTS_FAILED)
        with self.subTest(case="no changes"):
            self.assertEqual(self._run(0, dict(_BEFORE)), 0)
        with self.subTest(case="pytest.ExitCode.OK with changes"):
            self.assertEqual(self._run(pytest.ExitCode.OK, _CHANGED), pytest.ExitCode.TESTS_FAILED)

    def test_warn_and_off_never_change_any_status(self) -> None:
        for env in ("warn", "off"):
            for start in (0,) + _ALL_NONZERO:
                with self.subTest(env=env, start=repr(start)):
                    self.assertIs(self._run(start, _CHANGED, env=env), start)

    def test_hook_never_raises(self) -> None:
        for label, kwargs in (
            ("snapshot raises", {"snapshot_error": OSError("boom")}),
            ("malformed baseline", {"baseline": "not-a-snapshot"}),
            ("malformed after", {"after": None}),
        ):
            for start in (0, 1):
                with self.subTest(case=label, start=start):
                    kw = {"after": _CHANGED, **kwargs}
                    self.assertEqual(self._run(start, **kw), start)


if __name__ == "__main__":
    unittest.main()
