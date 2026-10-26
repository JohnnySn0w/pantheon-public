"""T-4940 guard: every test module that pins an exact git-verb sequence ending in a push
must isolate the seat's state dir for plain-unittest runs (tools/_seat_state_isolation.py).
Otherwise, on a seat with a pin-verified writable mirror, the real production manifest
arms _git_utils._resolve_write_mirror and the sequence gains a 'remote' verb (T-4940:
3 test_ticket/test_ticket_grooming tests and 1 test_watch_runner test failed on Recovery).
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
_PUSH_TERMINATED_VERB_LIST = re.compile(r"""["']push["'],?\s*\]""")
_USES_ISOLATION = re.compile(r"_seat_state_isolation\.start\(\)")


def _offenders() -> list[str]:
    bad = []
    for path in sorted(TOOLS.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        if _PUSH_TERMINATED_VERB_LIST.search(text) and not _USES_ISOLATION.search(text):
            bad.append(path.name)
    return bad


class SeatStateIsolationGuard(unittest.TestCase):
    def test_every_push_sequence_module_isolates_seat_state(self):
        self.assertEqual(_offenders(), [], "these modules pin a push-terminated git sequence "
                         "but never call _seat_state_isolation.start() in setUpModule")

    def test_helper_isolates_and_restores(self):
        try:
            from tools import _seat_state_isolation as iso
        except ImportError:  # pragma: no cover
            import _seat_state_isolation as iso  # type: ignore[no-redef]
        before = os.environ.get("PANTHEON_STATE_DIR")
        path = iso.start()
        try:
            self.assertEqual(os.environ["PANTHEON_STATE_DIR"], path)
            self.assertTrue(Path(path).is_dir())
            self.assertEqual(list(Path(path).iterdir()), [])
            self.assertTrue(path.startswith(tempfile.gettempdir()))
        finally:
            iso.stop()
        self.assertEqual(os.environ.get("PANTHEON_STATE_DIR"), before)
        self.assertFalse(Path(path).exists())


if __name__ == "__main__":
    unittest.main()
