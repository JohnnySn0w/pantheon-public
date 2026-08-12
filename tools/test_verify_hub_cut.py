#!/usr/bin/env python3
"""T-2295: the post-cut gate must catch a Shape B survivor and must not
false-positive on Shape A.

The property under test is the one that makes Shape B dangerous in the first
place: a well-formed leftover is INDISTINGUISHABLE from a healthy quiet inbox by
every cheap proxy, so the gate has to key on the defining surface (tmp+new+cur)
rather than on directory existence.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import mailbox as _mailbox  # noqa: E402
from tools import verify_hub_cut  # noqa: E402

_SUBDIRS = ("tmp", "new", "cur")


def _make_maildir(root: Path) -> Path:
    for sub in _SUBDIRS:
        (root / sub).mkdir(parents=True)
    return root


class SurvivorDetection(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.hub = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def test_shape_A_gone_is_clean(self):
        """Nothing at the old hub at all -> no survivors."""
        self.assertEqual(verify_hub_cut.survivors(self.hub, ("orchestrator", "specialist")), [])

    def test_shape_B_wellformed_leftover_is_caught(self):
        """THE REGRESSION this gate exists for."""
        _make_maildir(self.hub / "orchestrator")
        self.assertEqual(verify_hub_cut.survivors(self.hub, ("orchestrator", "specialist")), ["orchestrator"])

    def test_compat_symlink_is_caught(self):
        """The specific trap: a symlink to the NEW hub is still a well-formed
        maildir at the OLD path, and is the likeliest way Shape B happens."""
        new_hub = Path(self._td.name) / "_new"
        _make_maildir(new_hub / "orchestrator")
        (self.hub / "orchestrator").symlink_to(new_hub / "orchestrator")
        self.assertEqual(verify_hub_cut.survivors(self.hub, ("orchestrator",)), ["orchestrator"])

    def test_bare_dir_is_NOT_a_survivor(self):
        """A leftover that is not a maildir cannot swallow mail, so flagging it
        would be a false positive — and would push someone toward deleting a
        directory that is not the hazard."""
        (self.hub / "orchestrator").mkdir()
        self.assertEqual(verify_hub_cut.survivors(self.hub, ("orchestrator",)), [])

    def test_partially_cleaned_is_NOT_a_survivor(self):
        """tmp/ and new/ but no cur/ — mailbox.exists() is all-three, and a
        partial cannot silently accept a drain."""
        d = self.hub / "orchestrator"
        (d / "tmp").mkdir(parents=True)
        (d / "new").mkdir()
        self.assertEqual(verify_hub_cut.survivors(self.hub, ("orchestrator",)), [])

    def test_the_nine_inboxes_include_specialist_a2a(self):
        """§4g found specialist-a2a in neither §1's 'Inboxes: 8' nor the literal
        inventory, while being live and hardcoded at specialist_seat_server.py:169.
        A gate that inherits the undercount inherits the silence."""
        self.assertIn("specialist-a2a", verify_hub_cut.DEFAULT_INBOXES)
        self.assertEqual(len(verify_hub_cut.DEFAULT_INBOXES), 9)
        self.assertEqual(len(set(verify_hub_cut.DEFAULT_INBOXES)), 9)


class ExitCodes(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.hub = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def test_exit_0_when_provably_dead(self):
        self.assertEqual(verify_hub_cut.main(["--old-hub", str(self.hub), "--inbox", "orchestrator"]), 0)

    def test_exit_1_when_a_survivor_exists(self):
        _make_maildir(self.hub / "orchestrator")
        self.assertEqual(verify_hub_cut.main(["--old-hub", str(self.hub), "--inbox", "orchestrator"]), 1)


class ShapeBIsIndistinguishableFromHealthy(unittest.TestCase):
    """The premise the gate rests on, pinned so it cannot rot silently.

    If a future mailbox.py change made a Shape B leftover self-announcing, this
    gate would be redundant rather than load-bearing — and we should find that
    out from a failing test, not by assuming it either way.
    """

    def test_decoy_consume_is_byte_identical_to_a_healthy_quiet_inbox(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            decoy = _make_maildir(root / "decoy")
            healthy = _make_maildir(root / "healthy")
            seen: list = []
            self.assertEqual(_mailbox.consume(decoy, seen.append),
                             _mailbox.consume(healthy, seen.append),
                             "if these ever differ, a leftover announces itself and this "
                             "gate's rationale needs revisiting")

    def test_shape_A_by_contrast_is_loud(self):
        with tempfile.TemporaryDirectory() as td:
            gone = Path(td) / "gone"
            result = _mailbox.consume(gone, lambda m: None)
            self.assertTrue(result.get("skipped"))
            self.assertEqual(result.get("reason"), "inbox_missing")


if __name__ == "__main__":
    unittest.main()
