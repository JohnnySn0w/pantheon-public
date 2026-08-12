#!/usr/bin/env python3
"""Unittest coverage for the T-1985 fire-and-forget ack behavior in
tools/acp_bridge.py.

Written as unittest.TestCase (NOT pytest) on purpose: the existing
tools/test_acp_bridge.py is pytest-only and pytest is not installed in this
environment, so those suites cannot run under `python3 -m unittest`. This
module reuses acp_bridge.FakeAcpTransport (the same duck-typed double) so the
new behavior is exercised against the real handle_message / run_once path.

The defect (T-1985): a keep_going nudge to a research seat starts a legitimate
turn that outruns prompt_timeout; the client raises on timeout, the message is
never acked, the retry stacks a second prompt behind the running turn, and
three strikes dead-letter a HEALTHY session. The fix acks a fire-and-forget
nudge on timeout (the turn is underway = the nudge did its job) while every
genuine failure — a JSON-RPC AcpError, a wrapper-exit AcpTransportError — still
raises for every sender.
"""
import sys
import unittest
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import acp_bridge
from tools import mailbox


_INIT_OK = {"protocolVersion": 1}
_NEW_SESSION_OK = {"sessionId": "sess-abc"}


def _base_responses(**overrides):
    resp = {"initialize": _INIT_OK, "session/new": _NEW_SESSION_OK}
    resp.update(overrides)
    return resp


class FireAndForgetAckTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.inbox = self.tmp_path / "inbox"
        mailbox.provision(self.inbox)  # T-2216: consume() no longer auto-provisions

    def tearDown(self):
        self._tmp.cleanup()

    def _make_bridge(self, responses):
        transport = acp_bridge.FakeAcpTransport(responses=responses)
        bridge = acp_bridge.AcpBridge(
            agent="test",
            cwd=str(self.tmp_path),
            inbox=self.inbox,
            transport=transport,
            state_path=self.tmp_path / "state" / "acp-bridge-test.json",
        )
        # Pre-attach the double + a session id so run_once() skips start().
        # start()'s assert_clean_room() would trip inside a live Claude Code
        # session (CLAUDE_CODE_* env present); the pytest suite runs in CI
        # where those are absent. Bypassing start() keeps this module hermetic
        # and scopes it to the handle_message ack contract under test.
        bridge.transport = transport
        bridge.session_id = "sess-test"
        return bridge, transport

    # -- Fix #1a: the timeout exception is a backward-compatible subclass -----
    def test_timeout_error_is_subclass_of_transport_error(self):
        self.assertTrue(
            issubclass(acp_bridge.AcpTimeoutError, acp_bridge.AcpTransportError),
            "AcpTimeoutError must subclass AcpTransportError so every existing "
            "`except AcpTransportError` / bare-propagation path is unchanged.",
        )

    # -- Fix #1b: a nudge that TIMES OUT is acked (turn underway = success) ---
    def test_keep_going_nudge_acked_on_timeout(self):
        responses = _base_responses(**{
            "session/prompt": acp_bridge.AcpTimeoutError(
                "timeout waiting for 'session/prompt' response"
            )
        })
        bridge, _ = self._make_bridge(responses)
        mid = mailbox.send(self.inbox, "please keep going",
                           sender="keep_going", subject="keep-going nudge")

        result = bridge.run_once()

        self.assertEqual(result["errors"], [], "a timed-out nudge must not record an error")
        self.assertIn(mid, result["processed"], "the nudge must be acked (processed)")
        self.assertTrue((self.inbox / "cur" / mid).exists(),
                        "acked message moves to cur/")
        self.assertFalse((self.inbox / "new" / mid).exists(),
                         "acked message must not remain in new/ (no retry -> no prompt stacking)")

    # -- a nudge whose turn completes fast still acks (unchanged path) --------
    def test_keep_going_nudge_fast_turn_still_acks(self):
        responses = _base_responses(**{"session/prompt": {"stopReason": "end_turn"}})
        bridge, _ = self._make_bridge(responses)
        mid = mailbox.send(self.inbox, "please keep going",
                           sender="keep_going", subject="keep-going nudge")

        result = bridge.run_once()

        self.assertIn(mid, result["processed"])
        self.assertTrue((self.inbox / "cur" / mid).exists())

    # -- a nudge that hits a GENUINE failure still raises (retries) -----------
    def test_keep_going_nudge_raises_on_wrapper_exit(self):
        responses = _base_responses(**{
            "session/prompt": acp_bridge.AcpTransportError("wrapper exited (rc=1) while waiting")
        })
        bridge, _ = self._make_bridge(responses)
        mid = mailbox.send(self.inbox, "please keep going",
                           sender="keep_going", subject="keep-going nudge")

        result = bridge.run_once()

        self.assertTrue(result["errors"], "a wrapper-exit is a genuine failure and must record")
        self.assertTrue((self.inbox / "new" / mid).exists(),
                        "a genuinely-failed nudge stays in new/ for retry")
        self.assertFalse((self.inbox / "cur" / mid).exists())

    def test_keep_going_nudge_raises_on_acp_error(self):
        responses = _base_responses(**{
            "session/prompt": acp_bridge.AcpError(-32000, "Authentication required")
        })
        bridge, _ = self._make_bridge(responses)
        mid = mailbox.send(self.inbox, "please keep going",
                           sender="keep_going", subject="keep-going nudge")

        result = bridge.run_once()

        self.assertTrue(result["errors"])
        self.assertTrue((self.inbox / "new" / mid).exists())

    # -- REGRESSION GUARD: a normal task message is unchanged on timeout -----
    def test_non_nudge_message_still_raises_on_timeout(self):
        responses = _base_responses(**{
            "session/prompt": acp_bridge.AcpTimeoutError(
                "timeout waiting for 'session/prompt' response"
            )
        })
        bridge, _ = self._make_bridge(responses)
        mid = mailbox.send(self.inbox, {"task": "do a thing"},
                           sender="orchestrator", subject="a real task")

        result = bridge.run_once()

        self.assertTrue(result["errors"],
                        "a normal task turn you want to wait for must still raise on timeout")
        self.assertTrue((self.inbox / "new" / mid).exists(),
                        "the task message stays in new/ for retry (unchanged behavior)")
        self.assertFalse((self.inbox / "cur" / mid).exists())


class MainConsumeTtlPlumbingTest(unittest.TestCase):
    """T-1990: --consume-ttl must reach AcpBridge(consume_ttl_secs=...); it was
    previously unplumbed (constructor-only, hardcoded 7200 in production)."""

    def _run_main_capturing(self, argv):
        from unittest.mock import MagicMock, patch
        captured = {}

        def fake_ctor(*a, **kw):
            captured.update(kw)
            m = MagicMock()
            m.run_once.return_value = {"skipped": True, "processed": [], "errors": [], "quarantined": []}
            m.session_id = None
            return m

        with patch.object(acp_bridge, "AcpBridge", side_effect=fake_ctor):
            acp_bridge.main(argv)
        return captured

    def test_consume_ttl_flag_reaches_constructor(self):
        cap = self._run_main_capturing([
            "--agent", "t", "--cwd", "/tmp", "--inbox", "/tmp/ibx",
            "--consume-ttl", "9999", "--max-iterations", "0",
        ])
        self.assertEqual(cap.get("consume_ttl_secs"), 9999)

    def test_consume_ttl_default_tracks_the_shared_constant(self):
        """Was `test_consume_ttl_defaults_to_7200`, asserting a literal.

        T-2287 lowered the value (the lease no longer spans a handler), and a
        test pinned to a literal can only ever say "somebody changed this",
        which is not the property worth guarding. The property is that the CLI
        default and the library constant are the SAME number — drift between
        two copies of this value is what caused T-2236."""
        # ⚠️ NOT a bare `import mailbox` (fixed 2026-08-03, T-2384 sweep): run from
        # the repo root — the documented invocation for these `tools.` suites —
        # tools/ is not on sys.path and the bare name binds Python's STDLIB mailbox,
        # so this test ERRORED on AttributeError from one cwd and passed from the
        # other. The module-level `from tools import mailbox` is unambiguous.
        cap = self._run_main_capturing([
            "--agent", "t", "--cwd", "/tmp", "--inbox", "/tmp/ibx",
            "--max-iterations", "0",
        ])
        self.assertEqual(cap.get("consume_ttl_secs"),
                         mailbox._DEFAULT_CONSUME_TTL_SECS)


if __name__ == "__main__":
    unittest.main()
