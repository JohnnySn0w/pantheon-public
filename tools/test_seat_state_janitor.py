"""Tests for tools/seat_state_janitor.py (ORG-106 L5, T-5529). Tempdirs and mocks only."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tools import harness_state as hs
from tools import seat_state_janitor as sj

NOW = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def record(*, expires=None, indefinite=False, set_ts=None, owner="specialist", notified=False):
    set_ts = set_ts or NOW - timedelta(hours=3)
    if expires is None and not indefinite:
        expires = NOW + timedelta(hours=5)
    return {
        "schema": hs.SCHEMA_SEAT_STATE, "persona": "specialist", "state": "paused-intake",
        "phase": "set", "phase_ts": ts(set_ts), "set_ts": ts(set_ts),
        "set_by": "operator:orchestrator", "owner": owner, "reason": "testing",
        "ticket": None, "expires": None if indefinite else ts(expires),
        "indefinite": indefinite, "telegram_dark_ack": False,
        "pre_expiry_notified": notified, "ledger_entry": "e1",
    }


class Mail:
    def __init__(self, fail=False):
        self.sent: list[tuple[str, str, str]] = []
        self.fail = fail

    def __call__(self, who, subject, payload):
        if self.fail:
            raise OSError("mailbox down")
        self.sent.append((who, subject, payload))


class JanitorBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.mail = Mail()

    def put(self, rec):
        hs.write_seat_state("specialist", rec, root=self.root)

    def load(self):
        return hs.load_seat_state("specialist", root=self.root)

    def tick(self, now=NOW, mail=None, **kw):
        return sj.tick("specialist", now=now, state_root=self.root,
                       send_mail=mail or self.mail, **kw)

    def tombstones(self):
        p = hs.seat_state_tombstones_path("specialist", root=self.root)
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


class TestPreExpiry(JanitorBase):
    def test_notice_once_then_marks_notified(self):
        self.put(record(expires=NOW + timedelta(minutes=30)))
        self.assertEqual(self.tick(), ["pre_expiry_notice"])
        self.assertEqual(len(self.mail.sent), 1)
        self.assertIn("harness specialist pause --for", self.mail.sent[0][2])
        rec = self.load()
        self.assertTrue(rec["pre_expiry_notified"])
        self.assertEqual(stat.S_IMODE(os.stat(hs.seat_state_path("specialist", root=self.root)).st_mode), 0o600)
        self.assertEqual(self.tick(), [])
        self.assertEqual(len(self.mail.sent), 1)

    def test_no_notice_far_from_expiry(self):
        self.put(record(expires=NOW + timedelta(hours=3)))
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.mail.sent, [])
        self.assertFalse(self.load()["pre_expiry_notified"])

    def test_send_failure_does_not_mark_and_retries(self):
        self.put(record(expires=NOW + timedelta(minutes=30)))
        self.assertIn("pre_expiry_notice_failed", self.tick(mail=Mail(fail=True)))
        self.assertFalse(self.load()["pre_expiry_notified"])
        self.assertEqual(self.tick(), ["pre_expiry_notice"])
        self.assertTrue(self.load()["pre_expiry_notified"])

    def test_dry_run_writes_and_sends_nothing(self):
        self.put(record(expires=NOW + timedelta(minutes=30)))
        self.assertEqual(self.tick(dry_run=True), ["pre_expiry_notice"])
        self.assertEqual(self.mail.sent, [])
        self.assertFalse(self.load()["pre_expiry_notified"])


class TestExtendAdvice(JanitorBase):
    """T-5623: the extend line follows the record's state (pause would downgrade a deep sleep)."""

    def _sleep(self, **kw):
        rec = record(**kw)
        rec.update(state="deep-sleep", phase="asleep")
        return rec

    def test_deep_sleep_pre_expiry_says_sleep(self):
        self.put(self._sleep(expires=NOW + timedelta(minutes=30)))
        self.assertEqual(self.tick(), ["pre_expiry_notice"])
        text = self.mail.sent[0][2]
        self.assertIn("harness specialist sleep --for", text)
        self.assertNotIn("pause --for", text)

    def test_deep_sleep_indefinite_says_sleep(self):
        self.put(self._sleep(indefinite=True, set_ts=NOW - timedelta(days=8)))
        self.assertEqual(self.tick(), ["indefinite_nag"])
        text = self.mail.sent[0][2]
        self.assertIn("harness specialist sleep --for", text)
        self.assertNotIn("pause --for", text)

    def test_paused_indefinite_keeps_pause(self):
        self.put(record(indefinite=True, set_ts=NOW - timedelta(days=8)))
        self.tick()
        self.assertIn("harness specialist pause --for", self.mail.sent[0][2])

    def test_halted_gets_no_extend_line(self):
        self.assertNotIn("extend", sj._how_to_extend("specialist", "halted"))
        self.assertIn("wake", sj._how_to_extend("specialist", "paused-intake"))


class TestExpiry(JanitorBase):
    def test_expired_tombstoned_removed_notified(self):
        rec = record(expires=NOW - timedelta(minutes=1), set_ts=NOW - timedelta(hours=5))
        self.put(rec)
        self.assertEqual(self.tick(), ["expire"])
        self.assertIsNone(self.load())
        rows = self.tombstones()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ended_by"], "expiry")
        self.assertEqual(rows[0]["reason"], "testing")
        self.assertEqual(len(self.mail.sent), 1)
        self.assertIn("active again", self.mail.sent[0][2])
        self.assertEqual(self.tick(), [])

    def test_tombstone_precedes_unlink(self):
        self.put(record(expires=NOW - timedelta(minutes=1), set_ts=NOW - timedelta(hours=5)))
        order = []
        real_t, real_c = hs.append_seat_tombstone, hs.clear_seat_state
        with patch.object(hs, "append_seat_tombstone",
                          side_effect=lambda *a, **k: (order.append("t"), real_t(*a, **k))[1]), \
             patch.object(hs, "clear_seat_state",
                          side_effect=lambda *a, **k: (order.append("c"), real_c(*a, **k))[1]):
            self.tick()
        self.assertEqual(order, ["t", "c"])

    def test_mail_failure_on_expiry_still_clears(self):
        self.put(record(expires=NOW - timedelta(minutes=1), set_ts=NOW - timedelta(hours=5)))
        self.tick(mail=Mail(fail=True))
        self.assertIsNone(self.load())
        self.assertEqual(len(self.tombstones()), 1)


class TestIndefinite(JanitorBase):
    def test_no_nag_before_seven_days(self):
        self.put(record(indefinite=True, set_ts=NOW - timedelta(days=6, hours=23)))
        self.assertEqual(self.tick(), [])

    def test_nag_once_per_24h(self):
        self.put(record(indefinite=True, set_ts=NOW - timedelta(days=8)))
        self.assertEqual(self.tick(), ["indefinite_nag"])
        self.assertEqual(self.tick(now=NOW + timedelta(hours=23)), [])
        self.assertEqual(self.tick(now=NOW + timedelta(hours=24)), ["indefinite_nag"])
        self.assertEqual(len(self.mail.sent), 2)
        self.assertIn("8d", self.mail.sent[0][1])

    def test_record_schema_untouched_sidecar_used(self):
        rec = record(indefinite=True, set_ts=NOW - timedelta(days=8))
        self.put(rec)
        self.tick()
        self.assertEqual(self.load(), rec)
        side = json.loads(sj.sidecar_path("specialist", root=self.root).read_text())
        self.assertEqual(side["set_ts"], rec["set_ts"])

    def test_refreshed_record_resets_nag_history(self):
        self.put(record(indefinite=True, set_ts=NOW - timedelta(days=8)))
        self.tick()
        self.put(record(indefinite=True, set_ts=NOW - timedelta(days=7, minutes=1)))
        self.assertEqual(self.tick(now=NOW + timedelta(minutes=5)), ["indefinite_nag"])

    def test_failed_nag_not_recorded(self):
        self.put(record(indefinite=True, set_ts=NOW - timedelta(days=8)))
        self.tick(mail=Mail(fail=True))
        self.assertFalse(sj.sidecar_path("specialist", root=self.root).exists())
        self.assertEqual(self.tick(), ["indefinite_nag"])


class TestRouting(unittest.TestCase):
    def test_role_members(self):
        with patch.object(sj.agent_role_map, "role_members", return_value=frozenset({"artisan", "mason"})):
            self.assertEqual(sj.resolve_recipients("role:engineering_orchestrator"), ["artisan", "mason"])

    def test_role_falls_back_to_audit_role(self):
        with patch.object(sj.agent_role_map, "role_members", return_value=frozenset()), \
             patch.object(sj.agent_role_map, "personas_for_role_id", return_value=["artisan"]):
            self.assertEqual(sj.resolve_recipients("role:x"), ["artisan"])

    def test_role_fault_falls_back_to_audit_role(self):
        with patch.object(sj.agent_role_map, "role_members", side_effect=RuntimeError("x")), \
             patch.object(sj.agent_role_map, "personas_for_role_id", return_value=["mason"]):
            self.assertEqual(sj.resolve_recipients("role:x"), ["mason"])

    def test_unknown_role_goes_to_orchestrator(self):
        with patch.object(sj.agent_role_map, "role_members", return_value=frozenset()), \
             patch.object(sj.agent_role_map, "personas_for_role_id", return_value=[]):
            self.assertEqual(sj.resolve_recipients("role:nope"), ["orchestrator"])

    def test_operator_goes_to_orchestrator(self):
        self.assertEqual(sj.resolve_recipients("operator"), ["orchestrator"])

    def test_persona_name(self):
        self.assertEqual(sj.resolve_recipients("specialist"), ["specialist"])

    def test_invalid_persona_goes_to_orchestrator(self):
        self.assertEqual(sj.resolve_recipients("Bad Name!"), ["orchestrator"])

    def test_operator_resolves(self):
        with patch.object(sj.agent_role_map, "account_persona", return_value="artisan"):
            self.assertEqual(sj.resolve_recipients("operator:suborch2"), ["artisan"])

    def test_operator_unresolvable_goes_to_orchestrator(self):
        with patch.object(sj.agent_role_map, "account_persona", return_value=None):
            self.assertEqual(sj.resolve_recipients("operator:ghost"), ["orchestrator"])

    def test_non_string_owner_goes_to_orchestrator(self):
        self.assertEqual(sj.resolve_recipients(None), ["orchestrator"])

    def test_recipients_capped(self):
        many = frozenset(f"p{i}" for i in range(20))
        with patch.object(sj.agent_role_map, "role_members", return_value=many):
            self.assertEqual(len(sj.resolve_recipients("role:big")), sj.MAX_RECIPIENTS)

    def test_default_sender_uses_hub_and_seat_sender(self):
        calls = []
        with patch.object(sj.agent_role_map, "current_mailbox_hub", return_value="/hub"), \
             patch("tools.mailbox.send", side_effect=lambda *a, **k: calls.append((a, k))):
            sj._default_send_mail("specialist")("orchestrator", "s", "p")
        (args, kwargs), = calls
        self.assertEqual(args, ("/hub/orchestrator", "p"))
        self.assertEqual(kwargs["sender"], "specialist")
        self.assertEqual(kwargs["severity"], "normal")


class TestRefreshRace(JanitorBase):
    """REBOUND 1: a concurrent `harness pause` refresh must never be overwritten or deleted."""

    def _refreshed(self):
        return record(expires=NOW + timedelta(hours=9), set_ts=NOW - timedelta(minutes=1),
                      owner="newowner")

    def test_refresh_during_pre_expiry_mail_survives_intact(self):
        self.put(record(expires=NOW + timedelta(minutes=30)))
        fresh = self._refreshed()

        def refresh_mid_send(who, subject, payload):
            self.mail.sent.append((who, subject, payload))
            self.put(fresh)

        self.assertEqual(self.tick(mail=refresh_mid_send), ["pre_expiry_notice", "pre_expiry_notice_superseded"])
        self.assertEqual(self.load(), fresh)
        self.assertFalse(self.load()["pre_expiry_notified"])

    def test_refresh_during_nag_mail_does_not_write_sidecar(self):
        self.put(record(indefinite=True, set_ts=NOW - timedelta(days=8)))

        def refresh_mid_send(who, subject, payload):
            self.put(self._refreshed())

        self.tick(mail=refresh_mid_send)
        self.assertFalse(sj.sidecar_path("specialist", root=self.root).exists())

    def test_refresh_before_lock_is_not_deleted_or_tombstoned(self):
        self.put(record(expires=NOW - timedelta(minutes=1), set_ts=NOW - timedelta(hours=5)))
        fresh = self._refreshed()
        real = hs.seat_state_lock

        def refresh_then_lock(*a, **k):
            self.put(fresh)  # the pause lands after the janitor's last look, before it locks
            return real(*a, **k)

        with patch.object(hs, "seat_state_lock", side_effect=refresh_then_lock):
            self.assertEqual(self.tick(), [])
        self.assertEqual(self.load(), fresh)
        self.assertEqual(self.tombstones(), [])
        self.assertEqual(self.mail.sent, [])

    def test_flag_merges_into_fresh_record_when_unchanged(self):
        rec = record(expires=NOW + timedelta(minutes=30))
        self.put(rec)
        self.tick()
        self.assertEqual(self.load(), {**rec, "pre_expiry_notified": True})

    def test_pause_waits_for_janitor_lock_then_proceeds(self):
        """The lock is real mutual exclusion: a held lock blocks a second blocking taker."""
        with hs.seat_state_lock("specialist", root=self.root):
            with self.assertRaises(hs.SeatStateBusy):
                with hs.seat_state_lock("specialist", root=self.root, timeout_s=0.1):
                    pass


class TestSafety(JanitorBase):
    def test_corrupt_record_no_write(self):
        path = hs.seat_state_path("specialist", root=self.root)
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        self.assertEqual(self.tick(), [])
        self.assertEqual(path.read_text(), "{not json")
        self.assertEqual(self.tombstones(), [])
        self.assertEqual(self.mail.sent, [])

    def test_invalid_record_no_write(self):
        bad = record(expires=NOW - timedelta(hours=1))
        bad["reason"] = ""
        path = hs.seat_state_path("specialist", root=self.root)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(bad))
        self.assertEqual(self.tick(), [])
        self.assertEqual(json.loads(path.read_text()), bad)
        self.assertEqual(self.tombstones(), [])

    def test_no_record_is_noop(self):
        self.assertEqual(self.tick(), [])

    def test_busy_lock_skips_without_waiting(self):
        self.put(record(expires=NOW + timedelta(minutes=30)))
        lp = hs.seat_state_lock_path("specialist", root=self.root)
        fd = os.open(lp, os.O_WRONLY | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.mail.sent, [])

    def test_concurrent_ticks_converge(self):
        self.put(record(expires=NOW + timedelta(minutes=30)))
        gate = threading.Barrier(4)
        results = []

        def run():
            gate.wait()
            results.append(self.tick())

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # mail is outside the lock, so ticks that decided before the flag landed may each
        # mail (mailbox dedupe_pending collapses those); the end state must converge.
        self.assertGreaterEqual(len(self.mail.sent), 1)
        self.assertLessEqual(len(self.mail.sent), 4)
        self.assertTrue(self.load()["pre_expiry_notified"])
        before = len(self.mail.sent)
        self.assertEqual(self.tick(), [])
        self.assertEqual(len(self.mail.sent), before)

    def test_deadline_stops_further_sends(self):
        self.put(record(expires=NOW + timedelta(minutes=30), owner="role:big"))
        many = frozenset(f"p{i}" for i in range(5))
        with patch.object(sj.agent_role_map, "role_members", return_value=many), \
             patch.object(sj, "TICK_DEADLINE_SECONDS", -1.0):
            self.tick()
        self.assertEqual(self.mail.sent, [])
        self.assertFalse(self.load()["pre_expiry_notified"])

    def test_cli_dry_run(self):
        self.put(record(expires=NOW - timedelta(minutes=1), set_ts=NOW - timedelta(hours=5)))
        self.assertEqual(sj.main(["tick", "specialist", "--dry-run", "--state-root", str(self.root)]), 0)
        self.assertIsNotNone(self.load())


if __name__ == "__main__":
    unittest.main()
