#!/usr/bin/env python3
"""Unittest coverage for T-2797 proactive ACP-bridge session recycling.

The wrapper session accumulates context across every delivered message for the
whole process life (start() resolves ONE session; run_once() reuses it), so a
heavy-mail seat (specialist) eventually crosses the model context ceiling and every
delivery fails ACP -32603 "Prompt is too long" until manually recycled
(bugs/acp-bridge-context-full-prompt-too-long-mailbox-backlog.md). With
--recycle-after-turns N, run_once() proactively recycles the session (close +
delete the persisted state file -> next drain's start() takes session/new, fresh
empty context) once N messages have been delivered on the current session AND the
drain is idle. OFF by default (recycle_after_turns=None) so every existing
caller/test is unchanged.

These tests assert the TRIGGER + IDLE-GATE + FRESH-SESSION correctness against a
FakeAcpTransport. Threshold calibration + the real-wrapper context-clear are the
separate acp_bridge_wetrun.py rung (a live bridge), not covered here.

unittest (NOT pytest) — mirrors tools/test_acp_bridge_cap.py.
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import acp_bridge
from tools import mailbox

_INIT_OK = {"protocolVersion": 1}
_ENV_PREFIXES = ("ANTHROPIC", "CLAUDE_CODE")


class RecycleTest(unittest.TestCase):
    def setUp(self):
        # run_once() -> start() -> assert_clean_room() runs for real; clear any
        # ambient trigger-prefixed env so it doesn't trip inside a live session.
        clean_env = {k: v for k, v in os.environ.items()
                     if not k.startswith(_ENV_PREFIXES)}
        self._env_patch = patch.dict(os.environ, clean_env, clear=True)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.inbox = self.tmp_path / "inbox"
        mailbox.provision(self.inbox)
        self.state_path = self.tmp_path / "state" / "acp-bridge-test.json"
        self.session_ids = []       # every session id handed out by session/new
        self.prompt_sessions = []   # sessionId used on each session/prompt

    def _handler(self):
        counter = {"n": 0}

        def handler(method, params):
            if method == "initialize":
                return _INIT_OK
            if method == "session/new":
                counter["n"] += 1
                sid = f"sess-{counter['n']}"
                self.session_ids.append(sid)
                return {"sessionId": sid}
            if method == "session/load":
                # A recycle deletes the state file, so post-recycle drains must
                # NEVER reach session/load. Kept here so a regression that
                # resumes instead of recycling shows up as this id, not a crash.
                return {"sessionId": params.get("sessionId")}
            if method == "session/prompt":
                self.prompt_sessions.append(params["sessionId"])
                return {"stopReason": "end_turn"}
            raise AssertionError(f"unexpected method {method}")

        return handler

    def _make_bridge(self, **kwargs):
        transport = acp_bridge.FakeAcpTransport(handler=self._handler())
        bridge = acp_bridge.AcpBridge(
            agent="test", cwd=str(self.tmp_path), inbox=self.inbox,
            transport=transport, state_path=self.state_path, **kwargs,
        )
        return bridge, transport

    # ------------------------------------------------------------------ off
    def test_off_by_default_never_recycles(self):
        bridge, transport = self._make_bridge()  # recycle_after_turns=None
        mailbox.send(self.inbox, {"i": 1}, sender="t", subject="one")
        bridge.run_once()                      # deliver
        for _ in range(5):
            bridge.run_once()                  # idle drains
        new_calls = [c for c in transport.calls if c[0] == "session/new"]
        self.assertEqual(len(new_calls), 1,
                         "recycle_after_turns=None must never recycle")

    # -------------------------------------------------------------- fires
    def test_recycle_fires_after_threshold_on_idle_drain(self):
        bridge, transport = self._make_bridge(recycle_after_turns=2)
        mailbox.send(self.inbox, {"i": 1}, sender="t", subject="one")
        mailbox.send(self.inbox, {"i": 2}, sender="t", subject="two")

        r1 = bridge.run_once()                 # 2 delivered on sess-1; NOT idle
        self.assertEqual(len(r1["processed"]), 2)
        self.assertEqual(bridge._delivered_since_session, 2)
        self.assertTrue(self.state_path.exists())
        self.assertIsNotNone(bridge.transport)

        r2 = bridge.run_once()                 # idle + threshold crossed -> recycle
        self.assertEqual(r2["processed"], [])
        self.assertIsNone(bridge.transport, "recycle must tear the transport down")
        self.assertIsNone(bridge.session_id)
        self.assertFalse(self.state_path.exists(),
                         "recycle must delete the persisted state file")
        self.assertEqual(bridge._delivered_since_session, 0,
                         "turn counter resets on recycle")

        # A later message establishes a FRESH session via session/new, never load.
        mailbox.send(self.inbox, {"i": 3}, sender="t", subject="three")
        bridge.run_once()
        self.assertEqual(self.prompt_sessions, ["sess-1", "sess-1", "sess-2"])
        load_calls = [c for c in transport.calls if c[0] == "session/load"]
        self.assertFalse(load_calls,
                         "recycle forces session/new (fresh context), never resume")

    # -------------------------------------------------------- below threshold
    def test_no_recycle_below_threshold(self):
        bridge, transport = self._make_bridge(recycle_after_turns=5)
        mailbox.send(self.inbox, {"i": 1}, sender="t", subject="one")
        bridge.run_once()                      # 1 delivered
        bridge.run_once()                      # idle, counter=1 < 5
        self.assertEqual(len(self.session_ids), 1, "no recycle below threshold")
        self.assertTrue(self.state_path.exists())
        self.assertEqual(bridge._delivered_since_session, 1)

    # ------------------------------------------- never mid-delivery (not idle)
    def test_no_recycle_on_a_delivering_drain(self):
        # A drain that DELIVERS (processed non-empty) must never recycle, even
        # at/over threshold — recycle only fires on a subsequent IDLE drain, so
        # no message is ever dropped between arrival and delivery.
        bridge, transport = self._make_bridge(recycle_after_turns=1)
        mailbox.send(self.inbox, {"i": 1}, sender="t", subject="one")
        r1 = bridge.run_once()                 # counter 1>=1 but processed non-empty
        self.assertEqual(len(r1["processed"]), 1)
        self.assertIsNotNone(bridge.transport, "a delivering drain must not recycle")
        self.assertEqual(len(self.session_ids), 1)
        self.assertTrue(self.state_path.exists())

    # ----------------------------------------------------- capped -> no recycle
    def test_no_recycle_while_capped(self):
        bridge, transport = self._make_bridge(recycle_after_turns=1)
        mailbox.send(self.inbox, {"i": 1}, sender="t", subject="one")
        bridge.run_once()                      # deliver 1, counter=1
        bridge._capped_until = time.monotonic() + 300   # breaker open
        r = bridge.run_once()                  # capped: whole drain deferred
        self.assertTrue(r.get("skipped"))
        self.assertIsNotNone(bridge.transport,
                             "a transient cap is not a context wedge — never recycle it")
        self.assertEqual(len(self.session_ids), 1)
        self.assertTrue(self.state_path.exists())

    # -------------------------------------------- recycle re-arms for next cycle
    def test_recycle_re_arms_after_a_fresh_session(self):
        bridge, transport = self._make_bridge(recycle_after_turns=1)
        mailbox.send(self.inbox, {"i": 1}, sender="t", subject="one")
        bridge.run_once()                      # deliver on sess-1, counter=1
        bridge.run_once()                      # idle -> recycle #1
        self.assertEqual(bridge._delivered_since_session, 0)
        mailbox.send(self.inbox, {"i": 2}, sender="t", subject="two")
        bridge.run_once()                      # fresh sess-2, deliver, counter=1
        bridge.run_once()                      # idle -> recycle #2
        # two recycles => three session/new calls (sess-1, sess-2, sess-3-on-next)
        new_calls = [c for c in transport.calls if c[0] == "session/new"]
        self.assertEqual(len(new_calls), 2, "each cycle establishes exactly one session")
        self.assertIsNone(bridge.transport)
        self.assertFalse(self.state_path.exists())


if __name__ == "__main__":
    unittest.main()
