"""Tests for tools/unit_failure_alert.py (T-1868).

Delivery seams (`tools.mailbox.send`, `_send_telegram_alert`) are always
mocked — a test run must never write the real ORG-026 hub or send live
Telegram (mirrors test_disk_usage_monitor.py). Run with
PANTHEON_DISABLE_LIVE_TELEGRAM=1 as belt-and-suspenders.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import notify_policy
from tools import unit_failure_alert as ufa


class _StateDirMixin(unittest.TestCase):
    """Point STATE_PATH at a tempdir so tests never touch real throttle state."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        state_dir = Path(self._tmp.name)
        # Point the notify-mute registry at an (absent) temp path so the ORG-076 mute
        # gate reads an empty registry — the delivery/throttle tests below must never
        # depend on whatever the LIVE registry happens to hold for the test account.
        self._mute_registry = str(state_dir / "notify_mute.json")
        self._patches = [
            mock.patch.object(ufa, "STATE_DIR", state_dir),
            mock.patch.object(ufa, "STATE_PATH", state_dir / "state.json"),
            mock.patch.dict(os.environ, {"PANTHEON_MUTE_REGISTRY": self._mute_registry}),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)


class TestDelivery(_StateDirMixin):
    def test_happy_path_delivers_to_recovery_mailbox_and_records_state(self) -> None:
        with mock.patch("tools.mailbox.send") as m_send, \
             mock.patch.object(ufa, "_journal_tail", return_value="boom"):
            rc = ufa.run("pantheon-grooming.service")
        self.assertEqual(rc, 0)
        args, kwargs = m_send.call_args
        self.assertEqual(args[0], "/home/post-office/mailbox/recovery")
        self.assertIn("systemd unit FAILED: pantheon-grooming.service", args[1])
        self.assertIn("boom", args[1])
        self.assertEqual(kwargs["subject"], "unit-failure: pantheon-grooming.service")
        state = json.loads(ufa.STATE_PATH.read_text())
        self.assertIn("pantheon-grooming.service", state)

    def test_mailbox_failure_falls_back_to_telegram_exit_0(self) -> None:
        with mock.patch("tools.mailbox.send", side_effect=PermissionError("hub")), \
             mock.patch.object(ufa, "_deliver_telegram_fallback") as m_tg, \
             mock.patch.object(ufa, "_journal_tail", return_value="boom"):
            rc = ufa.run("pantheon-welltrod.service")
        self.assertEqual(rc, 0)
        self.assertIn("recovery mailbox unreachable", m_tg.call_args[0][0])

    def test_both_paths_failing_exits_1(self) -> None:
        with mock.patch("tools.mailbox.send", side_effect=PermissionError("hub")), \
             mock.patch.object(ufa, "_deliver_telegram_fallback",
                               side_effect=OSError("net down")), \
             mock.patch.object(ufa, "_journal_tail", return_value="boom"):
            rc = ufa.run("pantheon-welltrod.service")
        self.assertEqual(rc, 1)

    def test_fallback_failure_does_not_record_state(self) -> None:
        """A totally-failed alert must NOT start the cooldown — the next
        failure should try again rather than being throttled into silence."""
        with mock.patch("tools.mailbox.send", side_effect=PermissionError("hub")), \
             mock.patch.object(ufa, "_deliver_telegram_fallback",
                               side_effect=OSError("net down")), \
             mock.patch.object(ufa, "_journal_tail", return_value="boom"):
            ufa.run("pantheon-welltrod.service")
        self.assertFalse(ufa.STATE_PATH.exists())


class TestThrottle(_StateDirMixin):
    def test_second_alert_within_cooldown_is_skipped(self) -> None:
        with mock.patch("tools.mailbox.send") as m_send, \
             mock.patch.object(ufa, "_journal_tail", return_value="boom"):
            ufa.run("pantheon-grooming.service")
            ufa.run("pantheon-grooming.service")
        self.assertEqual(m_send.call_count, 1)

    def test_different_units_throttle_independently(self) -> None:
        with mock.patch("tools.mailbox.send") as m_send, \
             mock.patch.object(ufa, "_journal_tail", return_value="boom"):
            ufa.run("pantheon-grooming.service")
            ufa.run("pantheon-welltrod.service")
        self.assertEqual(m_send.call_count, 2)

    def test_alert_fires_again_after_cooldown(self) -> None:
        with mock.patch("tools.mailbox.send") as m_send, \
             mock.patch.object(ufa, "_journal_tail", return_value="boom"):
            ufa.run("pantheon-grooming.service")
            stale = json.loads(ufa.STATE_PATH.read_text())
            stale["pantheon-grooming.service"] -= ufa.ALERT_COOLDOWN_SECONDS + 1
            ufa.STATE_PATH.write_text(json.dumps(stale))
            ufa.run("pantheon-grooming.service")
        self.assertEqual(m_send.call_count, 2)


class TestJournalTail(unittest.TestCase):
    """_journal_tail is subprocess.run-only (no Popen — the whole surface is
    patched here; audit note per the patch-vs-Popen lesson)."""

    def test_captures_stdout(self) -> None:
        fake = subprocess.CompletedProcess([], 0, stdout="line1\nline2\n", stderr="")
        with mock.patch.object(ufa.subprocess, "run", return_value=fake) as m_run:
            out = ufa._journal_tail("x.service")
        self.assertEqual(out, "line1\nline2")
        self.assertIn("--no-pager", m_run.call_args[0][0])

    def test_timeout_yields_placeholder_not_exception(self) -> None:
        with mock.patch.object(
            ufa.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd="journalctl", timeout=1),
        ):
            out = ufa._journal_tail("x.service")
        self.assertIn("journal tail unavailable", out)

    def test_empty_journal_yields_placeholder(self) -> None:
        fake = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(ufa.subprocess, "run", return_value=fake):
            self.assertEqual(ufa._journal_tail("x.service"), "(journal tail empty)")


class TestMuteGate(_StateDirMixin):
    """ORG-076 §7 / T-3844: a muted persona's unit-failure is withheld (cap-collateral),
    while an unmuted persona's still pages, and the gate fails OPEN when it can't map a
    unit to a KNOWN persona. The suppress path leaves throttle state untouched so the
    alert resurfaces the moment the mute lifts."""

    def _mute(self, persona: str) -> None:
        notify_policy.mute(persona, by="cap-auto", reason="usage-cap",
                           registry=self._mute_registry)

    def test_templated_unit_for_muted_persona_is_suppressed(self) -> None:
        self._mute("mason")
        with mock.patch("tools.mailbox.send") as m_send, \
             mock.patch.object(ufa, "_deliver_telegram_fallback") as m_tg, \
             mock.patch.object(ufa, "_journal_tail", return_value="boom"):
            rc = ufa.run("pantheon-keep-going@mason.service")
        self.assertEqual(rc, 0)
        m_send.assert_not_called()          # no recovery-mailbox delivery
        m_tg.assert_not_called()            # and no operator-fallback either
        self.assertFalse(ufa.STATE_PATH.exists())  # throttle NOT consumed

    def test_templated_unit_for_unmuted_persona_still_alerts(self) -> None:
        self._mute("mason")  # a DIFFERENT persona is muted
        with mock.patch("tools.mailbox.send") as m_send, \
             mock.patch.object(ufa, "_journal_tail", return_value="boom"):
            rc = ufa.run("pantheon-keep-going@artisan.service")
        self.assertEqual(rc, 0)
        m_send.assert_called_once()

    def test_unknown_instance_fails_open_and_alerts(self) -> None:
        # An @instance that is not a known persona must NOT be silenced by any mute.
        self._mute("mason")
        with mock.patch("tools.mailbox.send") as m_send, \
             mock.patch.object(ufa, "_journal_tail", return_value="boom"):
            rc = ufa.run("pantheon-thing@notapersona.service")
        self.assertEqual(rc, 0)
        m_send.assert_called_once()

    def test_unknown_instance_fails_open_even_when_account_persona_muted(self) -> None:
        # T-4043 SEAT-INDEPENDENT regression: an @instance that is not a known persona
        # must fail OPEN and alert EVEN when THIS account's own persona is muted — the
        # account fallback is for BARE units only. By pinning account_persona to a muted
        # persona this fails pre-fix on ANY seat (the original test only tripped the bug
        # on suborch1, where the account persona mason happened to be the muted one, and
        # was seat-lucky-green on recovery). Without the fix _persona_for_unit falls the
        # unknown @instance through to the (muted) account persona and suppresses.
        self._mute("mason")
        with mock.patch("tools.agent_role_map.account_persona", return_value="mason"), \
             mock.patch("tools.mailbox.send") as m_send, \
             mock.patch.object(ufa, "_journal_tail", return_value="boom"):
            rc = ufa.run("pantheon-thing@notapersona.service")
        self.assertEqual(rc, 0)
        m_send.assert_called_once()  # unattributable @instance -> fail open -> alert

    def test_bare_unit_maps_to_account_persona_and_is_suppressed(self) -> None:
        acct_persona = ufa._persona_for_unit("pantheon-grooming.service")
        if acct_persona is None:
            self.skipTest("test account does not map to a known persona")
        self._mute(acct_persona)
        with mock.patch("tools.mailbox.send") as m_send, \
             mock.patch.object(ufa, "_journal_tail", return_value="boom"):
            rc = ufa.run("pantheon-grooming.service")
        self.assertEqual(rc, 0)
        m_send.assert_not_called()

    def test_no_mute_is_a_dormant_noop(self) -> None:
        # Nothing muted: a templated per-persona unit alerts exactly as before.
        with mock.patch("tools.mailbox.send") as m_send, \
             mock.patch.object(ufa, "_journal_tail", return_value="boom"):
            rc = ufa.run("pantheon-context-fill-monitor@artisan.service")
        self.assertEqual(rc, 0)
        m_send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
