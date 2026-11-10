"""T-6370: tools/owner_digest.py — owner-mailbox delivery for unattended digests."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import owner_digest as od  # noqa: E402

_GUARDS = ("PANTHEON_DISABLE_LIVE_TELEGRAM", "PANTHEON_DISABLE_LIVE_MAILBOX")


class SendOwnerDigest(unittest.TestCase):
    def _unguarded(self, **env):
        clean = {k: v for k, v in os.environ.items() if k not in _GUARDS}
        clean.update(env)
        return mock.patch.dict(os.environ, clean, clear=True)

    def test_suppressed_under_test_guard(self):
        with mock.patch.dict(os.environ, {"PANTHEON_DISABLE_LIVE_MAILBOX": "1"}), \
                mock.patch("tools.mailbox.send") as send:
            self.assertIsNone(od.send_owner_digest("x", sender="s", subject="d"))
        send.assert_not_called()

    def test_sends_to_owner_inbox_on_the_hub(self):
        with self._unguarded(), mock.patch("tools.mailbox.send", return_value="id1") as send, \
                mock.patch("tools.agent_role_map.mailbox_hub", return_value="/hub"):
            self.assertEqual(od.send_owner_digest("body", sender="artifact-gc", subject="gc"), "id1")
        inbox, text = send.call_args.args
        self.assertEqual(inbox, "/hub/recovery")
        self.assertEqual(text, "body")
        self.assertEqual(send.call_args.kwargs["sender"], "artifact-gc")
        self.assertTrue(send.call_args.kwargs["subject"].startswith("gc "))
        self.assertFalse(send.call_args.kwargs["dedupe_pending"])

    def test_owner_env_override(self):
        with self._unguarded(PANTHEON_DIGEST_OWNER="orchestrator"), \
                mock.patch("tools.mailbox.send", return_value="id") as send, \
                mock.patch("tools.agent_role_map.mailbox_hub", return_value="/hub"):
            od.send_owner_digest("b", sender="s", subject="d")
        self.assertEqual(send.call_args.args[0], "/hub/orchestrator")

    def test_delivery_failure_raises(self):
        with self._unguarded(), mock.patch("tools.mailbox.send", side_effect=PermissionError("x")), \
                mock.patch("tools.agent_role_map.mailbox_hub", return_value="/hub"):
            with self.assertRaises(PermissionError):
                od.send_owner_digest("b", sender="s", subject="d")


if __name__ == "__main__":
    unittest.main()
