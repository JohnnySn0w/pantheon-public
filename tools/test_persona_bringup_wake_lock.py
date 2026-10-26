"""T-5283: the consume wake path (persona_bringup.wake_persistent_runtime) shares
mailbox_wake's <box>/.wake.lock with the standalone pantheon-mailbox-wake@ unit.

With the lock held by the other waker, consume must skip its wake step: no
submit_to_runtime (a second submit landing mid-turn raises and records a false unit
failure), no raise, no marker write, and the skip reason recorded in the result the
way the T-2681/T-4292 deferrals record theirs. Real fcntl on a per-test temp dir."""
from __future__ import annotations

import fcntl
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import mailbox
from tools import mailbox_wake
from tools import runtime_controller as rc
from tools.harness_adapter.types import DeliveryReceipt
from tools.persona_bringup import wake_persistent_runtime
from tools.test_runtime_controller import (
    _Adapter,
    _binding,
    _provenance_patch,
    _record_handle_runtime_state,
)


class ConsumeWakeLockTest(unittest.TestCase):
    def _wake(self, root: Path, binding, submit_mock):
        handle = rc.RuntimeHandle(
            binding.launch, binding.controller.tmux, "session-1", rc._utc_now())
        with mock.patch.object(
            rc,
            "ensure_runtime",
            side_effect=lambda selected, adapter, *, state_root=None: _record_handle_runtime_state(
                selected, handle, Path(state_root)),
        ), mock.patch.object(
            rc, "submit_to_runtime", submit_mock
        ), mock.patch(
            "tools.mailbox_wake.session_key", return_value="1:2"
        ), mock.patch(
            "tools.mailbox_wake.pane_is_idle", return_value=True
        ), _provenance_patch():
            return wake_persistent_runtime(
                binding,
                adapter=_Adapter(binding),
                state_dir=root / "state",
                cwd=root,
                now=100.0,
            )

    def _setup(self, tmp: str):
        root = Path(tmp)
        binding = _binding(root)
        mailbox.provision(binding.mailbox)
        message_id = mailbox.send(
            binding.mailbox, {"message": "do the work"}, "tester", subject="work")
        return root, binding, message_id

    def _hold(self, box: Path) -> int:
        fd = os.open(box / mailbox_wake._WAKE_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o660)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd

    def test_lock_held_skips_wake_without_submit_or_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, binding, message_id = self._setup(tmp)
            held = self._hold(binding.mailbox)
            submit = mock.Mock()
            try:
                result = self._wake(root, binding, submit)
            finally:
                fcntl.flock(held, fcntl.LOCK_UN)
                os.close(held)
            submit.assert_not_called()
            self.assertEqual(result["action"], "debounced")
            self.assertIn(mailbox_wake.WAKE_LOCK_BUSY_REASON, result["reason"])
            self.assertTrue((binding.mailbox / "new" / message_id).is_file())
            self.assertFalse((binding.mailbox / ".wake_marker").exists())

    def test_wake_holds_lock_across_submit_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, binding, _ = self._setup(tmp)
            lock = binding.mailbox / mailbox_wake._WAKE_LOCK_NAME
            busy_during_submit = []
            receipt = DeliveryReceipt(
                harness="codex", correlation_id="", state="accepted",
                mechanism="tmux-submit-verify", retryable=False, evidence={"acked": "false"})

            def submit(*args, **kwargs):
                fd = os.open(lock, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    busy_during_submit.append(False)
                except BlockingIOError:
                    busy_during_submit.append(True)
                finally:
                    os.close(fd)
                return receipt

            result = self._wake(root, binding, mock.Mock(side_effect=submit))
            self.assertEqual(result["action"], "wake")
            self.assertEqual(busy_during_submit, [True])
            self.assertTrue((binding.mailbox / ".wake_marker").is_file())
            fd = self._hold(binding.mailbox)  # raises BlockingIOError if leaked
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
