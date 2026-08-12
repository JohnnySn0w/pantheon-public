"""T-2259: mail was pinned to the ACP seat, so ONE long turn blocked delivery
for 83 minutes while the agent's main seat sat idle at the prompt.

With --prefer-main-seat, an AFFIRMATIVELY idle main pane takes the drain: the
bridge injects the standard drain-your-inbox kick (mailbox_wake) and leaves
every message in new/ — the main seat acks by consuming. These tests pin the
routing table and the two constraints that must survive:

  - never wake anything but an affirmatively idle pane (tri-state: unknown
    falls back to the ACP path, busy defers);
  - never fall through to the ACP dispatch in the same pass as a wake attempt,
    or a swallowed kick becomes a double-delivery race.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# BARE imports on purpose, matching test_acp_bridge.py exactly: that module does
# `import acp_bridge` with tools/ on sys.path, so its bridge instances belong to
# the bare-identity class. Importing `tools.acp_bridge` here would patch a
# DIFFERENT class object and every mock would silently miss — the double-import
# trap (feedback_full_suite_leak_and_double_import_trap), which is precisely how
# this file's first draft failed.
import acp_bridge  # noqa: E402
import mailbox  # noqa: E402,F401
from test_acp_bridge import _CleanRoomBase, _base_responses, _seed_message  # noqa: E402


def _idle(value):
    return patch.object(acp_bridge.AcpBridge, "_main_pane_idle", lambda self: value)


class SeatRoutingTest(_CleanRoomBase):
    def _routing_bridge(self, **kw):
        kw.setdefault("main_pane", "fake-main:0")
        kw.setdefault("prefer_main_seat", True)
        return self._make_bridge(responses=_base_responses(
            **{"session/prompt": {"stopReason": "end_turn"}}), **kw)

    def test_idle_main_seat_takes_the_drain(self):
        bridge, transport, inbox = self._routing_bridge()
        mid = _seed_message(inbox)
        calls = []
        with _idle(True), patch.object(
                acp_bridge.AcpBridge, "_wake_main_seat",
                lambda self: calls.append(True) or True):
            result = bridge.run_once()
        self.assertEqual(result["reason"], "routed_to_main_seat")
        self.assertEqual(calls, [True], "the wake must actually be attempted")
        # The whole point: the message is UNTOUCHED for the main seat to consume.
        self.assertTrue((inbox / "new" / mid).exists())
        self.assertEqual(result["processed"], [])
        # And no ACP turn was spent on it.
        self.assertNotIn("session/prompt", [m for m, _ in transport.calls])

    def test_busy_main_seat_still_defers_everything(self):
        bridge, _, inbox = self._routing_bridge()
        mid = _seed_message(inbox)
        with _idle(False):
            result = bridge.run_once()
        self.assertEqual(result["reason"], "main_session_busy")
        self.assertTrue((inbox / "new" / mid).exists())

    def test_unknown_pane_falls_back_to_acp_dispatch(self):
        """A broken probe degrades to the OLD behavior, never to silence."""
        bridge, _, inbox = self._routing_bridge()
        mid = _seed_message(inbox)
        with _idle(None):
            result = bridge.run_once()
        self.assertIs(result["skipped"], False)
        self.assertIn(mid, result["processed"])

    def test_flag_off_keeps_the_acp_path_even_when_idle(self):
        bridge, _, inbox = self._routing_bridge(prefer_main_seat=False)
        mid = _seed_message(inbox)
        with _idle(True):
            result = bridge.run_once()
        self.assertIs(result["skipped"], False)
        self.assertIn(mid, result["processed"])

    def test_empty_inbox_never_wakes(self):
        bridge, _, _ = self._routing_bridge()
        calls = []
        with _idle(True), patch.object(
                acp_bridge.AcpBridge, "_wake_main_seat",
                lambda self: calls.append(True) or True):
            bridge.run_once()
        self.assertEqual(calls, [], "an empty queue needs no wake")

    def test_failed_wake_does_not_fall_through_to_acp(self):
        """The constraint: never both paths in one pass. A swallowed kick that
        then ALSO dispatched via ACP would race the main seat's eventual consume
        into double delivery."""
        bridge, transport, inbox = self._routing_bridge()
        mid = _seed_message(inbox)
        with _idle(True), patch.object(
                acp_bridge.AcpBridge, "_wake_main_seat", lambda self: False):
            result = bridge.run_once()
        self.assertEqual(result["reason"], "main_seat_wake_failed")
        self.assertTrue((inbox / "new" / mid).exists())
        self.assertNotIn("session/prompt", [m for m, _ in transport.calls])

    def test_wake_helper_survives_mailbox_wake_blowup(self):
        """_wake_main_seat must degrade to False, not propagate — run_once's
        contract is retry-next-poll, not crash-the-bridge."""
        bridge, _, _ = self._routing_bridge()
        import mailbox_wake as mw
        with patch.object(mw, "run", side_effect=RuntimeError("boom")):
            self.assertIs(bridge._wake_main_seat(), False)


if __name__ == "__main__":
    unittest.main()
