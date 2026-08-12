"""Tests for tools/acp_bridge_backlog_watchdog.py (T-1633 Unit 2).

Real Maildir-shaped fixtures via tools.mailbox.send() (D11 fixture corollary —
filenames come from real code, never hand-invented). The Telegram transmit
primitive (tools.inbox_consumer._send_telegram_alert) is patched directly (not
the module's own _alert wrapper) so alert-content-building logic is actually
exercised while guaranteeing no real network call; PANTHEON_DISABLE_LIVE_TELEGRAM
is forced on in setUp as a second, independent belt (the unittest analogue of
tools/conftest.py's autouse TELEGRAM_ENV-neutralization fixture, which is
pytest-only).

NOTE (T-1991): mechanically ported from pytest (fixtures / monkeypatch /
tmp_path / capsys) to unittest.TestCase so it runs under `python3 -m unittest`
— pytest is not installed in this environment and the repo is unittest-native.
Every test's behavior and coverage is preserved 1:1. The pytest autouse
`_isolate_state` fixture (STATE_PATH -> per-test tmp) becomes _WatchdogBase.setUp;
monkeypatch.setattr -> unittest.mock.patch(.object); capsys -> redirect_stderr.
"""
from __future__ import annotations

import io
import sys
import time
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.acp_bridge_backlog_watchdog as watchdog
import tools.mailbox as mailbox

NOW = time.time()  # real wall-clock "now" — unpatched mailbox.send() calls
# embed the real current time.time_ns(), so their age relative to NOW is small
# by construction; only the explicit ts_ns= override manufactures an old one.

_ALERT_TARGET = "tools.inbox_consumer._send_telegram_alert"


class _WatchdogBase(unittest.TestCase):
    """Every test in this file must never touch the real production
    alert-state file (~/.local/state/pantheon/...) — point STATE_PATH at a
    per-test tmp location. Ported from the pytest autouse `_isolate_state`
    fixture. Also forces PANTHEON_DISABLE_LIVE_TELEGRAM=1 as a belt against any
    un-patched send path (the conftest.py TELEGRAM_ENV-neutralization analogue)."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

        state_patch = patch.object(watchdog, "STATE_PATH", self.tmp_path / "state.json")
        state_patch.start()
        self.addCleanup(state_patch.stop)

        env_patch = patch.dict("os.environ", {"PANTHEON_DISABLE_LIVE_TELEGRAM": "1"})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _send(self, inbox, sender="peer", *, ts_ns=None):
        """Deliver one real message via mailbox.send(), optionally pinning the
        embedded ts_ns (by patching the stdlib time.time_ns() that mailbox.py
        calls internally) so age-threshold tests can construct an aged message
        without hand-writing a filename.

        dedupe_pending=False (T-1991 port note): these tests were authored
        against a pre-T-1935 mailbox.send() that did NOT collapse identical
        pending sends. mailbox.send() now defaults dedupe_pending=True, so N
        identical (sender, subject, payload) sends into an un-drained new/ would
        collapse to ONE message — silently defeating test_count_over_threshold's
        `count=3` assertion (3 sends -> 1 file). Passing dedupe_pending=False
        restores the fixture's original "N distinct messages" semantics without
        touching a single assertion; mailbox.send documents this exact knob for
        "producers whose identical payloads are intentionally distinct events"."""
        mailbox.provision(Path(inbox))  # T-2216: send() no longer auto-provisions;
        # idempotent, so safe to call on every _send() even across repeat calls
        # into the same inbox within one test.
        if ts_ns is not None:
            with patch.object(time, "time_ns", lambda: ts_ns):
                return mailbox.send(str(inbox), {"hello": "world"}, sender,
                                    dedupe_pending=False)
        return mailbox.send(str(inbox), {"hello": "world"}, sender,
                            dedupe_pending=False)


class BacklogWatchdogTest(_WatchdogBase):
    def test_empty_new_no_alert(self):
        sent = []
        with patch(_ALERT_TARGET, lambda text: sent.append(text)):
            inbox = self.tmp_path / "inbox"
            (inbox / "new").mkdir(parents=True)  # provision an empty new/, no messages

            rc = watchdog.check("orchestrator", str(inbox), dry_run=False, now=NOW)

        self.assertEqual(rc, 0)
        self.assertEqual(sent, [])

    def test_below_threshold_no_alert(self):
        sent = []
        with patch(_ALERT_TARGET, lambda text: sent.append(text)):
            inbox = self.tmp_path / "inbox"
            for _ in range(3):
                self._send(inbox)

            rc = watchdog.check("orchestrator", str(inbox), dry_run=False, now=NOW)

        self.assertEqual(rc, 0)
        self.assertEqual(sent, [])

    def test_count_over_threshold_alerts(self):
        sent = []
        with patch(_ALERT_TARGET, lambda text: sent.append(text)), \
                patch.object(watchdog, "COUNT_WARN", 2):
            inbox = self.tmp_path / "inbox"
            for _ in range(3):
                self._send(inbox)

            rc = watchdog.check("orchestrator", str(inbox), dry_run=False, now=NOW)

        self.assertEqual(rc, 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("orchestrator", sent[0])
        self.assertTrue("count=3" in sent[0] or "3 message" in sent[0])

    def test_age_over_threshold_alerts(self):
        sent = []
        with patch(_ALERT_TARGET, lambda text: sent.append(text)):
            inbox = self.tmp_path / "inbox"
            old_ts_ns = int((NOW - watchdog.AGE_WARN_SECONDS - 60) * 1_000_000_000)
            self._send(inbox, ts_ns=old_ts_ns)

            rc = watchdog.check("orchestrator", str(inbox), dry_run=False, now=NOW)

        self.assertEqual(rc, 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("orchestrator", sent[0])

    def test_dry_run_never_alerts(self):
        sent = []
        with patch(_ALERT_TARGET, lambda text: sent.append(text)), \
                patch.object(watchdog, "COUNT_WARN", 0):
            inbox = self.tmp_path / "inbox"
            self._send(inbox)

            rc = watchdog.check("orchestrator", str(inbox), dry_run=True, now=NOW)

        self.assertEqual(rc, 1)  # still reports breach...
        self.assertEqual(sent, [])  # ...but never sends

    def test_cooldown_suppresses_repeat_alert(self):
        sent = []
        with patch(_ALERT_TARGET, lambda text: sent.append(text)), \
                patch.object(watchdog, "COUNT_WARN", 0):
            inbox = self.tmp_path / "inbox"
            self._send(inbox)

            watchdog.check("orchestrator", str(inbox), dry_run=False, now=NOW)
            watchdog.check("orchestrator", str(inbox), dry_run=False, now=NOW + 60)  # well inside cooldown

            self.assertEqual(len(sent), 1)

            watchdog.check(
                "orchestrator", str(inbox), dry_run=False, now=NOW + watchdog.ALERT_COOLDOWN_SECONDS + 1
            )
            self.assertEqual(len(sent), 2)

    def test_read_backlog_missing_dir_raises(self):
        """An absent inbox is NOT a benign empty state (REBOUND 2): a piloted
        agent's inbox dirs are provisioned once and persist, so absence here
        means a config error (typo'd --inbox, never-provisioned agent, vanished
        hub dir) — must raise BacklogUnreadable, same as an unreadable dir."""
        with self.assertRaises(watchdog.BacklogUnreadable):
            watchdog.read_backlog(self.tmp_path / "nonexistent", now=NOW)

    def test_classify_boundaries(self):
        self.assertEqual(watchdog.classify(watchdog.COUNT_WARN, None), "OK")
        self.assertEqual(watchdog.classify(watchdog.COUNT_WARN + 1, None), "ALERT")
        self.assertEqual(watchdog.classify(0, watchdog.AGE_WARN_SECONDS), "OK")
        self.assertEqual(watchdog.classify(0, watchdog.AGE_WARN_SECONDS + 1), "ALERT")

    def test_main_refuses_flagless_run(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = watchdog.main(["--agent", "orchestrator"])
        self.assertEqual(rc, 64)
        self.assertIn("refusing", err.getvalue())

    def test_read_backlog_raises_on_permission_denied(self):
        """Real-perms fixture (not simulated) — reproduces the exact T-1015 live
        failure the REBOUND diagnosed: an existing new/ that this process cannot
        traverse must raise BacklogUnreadable, never silently report (0, None)."""
        inbox = self.tmp_path / "inbox"
        new_dir = inbox / "new"
        new_dir.mkdir(parents=True)
        new_dir.chmod(0o000)
        try:
            with self.assertRaises(watchdog.BacklogUnreadable):
                watchdog.read_backlog(str(inbox), now=NOW)
        finally:
            new_dir.chmod(0o755)  # restore before tmp_path's own teardown runs

    def test_check_alerts_on_unreadable_inbox(self):
        """check() must alert unconditionally on BacklogUnreadable — never fold
        it into the OK/zero path, and never gate it behind COUNT_WARN/AGE_WARN.
        Uses a patched os.listdir (deterministic regardless of whether the
        test happens to run as root, where chmod 0000 wouldn't actually block
        access) as the second, independent mechanism for the same guarantee."""
        sent = []

        def _raise_permission_error(*_args, **_kwargs):
            raise PermissionError("simulated: missing sg pantheon-mailbox group wrap")

        with patch(_ALERT_TARGET, lambda text: sent.append(text)), \
                patch.object(watchdog.os, "listdir", _raise_permission_error):
            inbox = self.tmp_path / "inbox"
            (inbox / "new").mkdir(parents=True)

            rc = watchdog.check("orchestrator", str(inbox), dry_run=False, now=NOW)

        self.assertEqual(rc, 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("orchestrator", sent[0])
        self.assertIn("could NOT be checked", sent[0])

    def test_check_alerts_on_missing_inbox(self):
        """check() must alert unconditionally when the inbox dir doesn't exist
        at all — never the OK/zero path (REBOUND 2's ENOENT-as-alert decision)."""
        sent = []
        with patch(_ALERT_TARGET, lambda text: sent.append(text)):
            inbox = self.tmp_path / "never-provisioned"  # not created at all

            rc = watchdog.check("orchestrator", str(inbox), dry_run=False, now=NOW)

        self.assertEqual(rc, 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("orchestrator", sent[0])
        self.assertIn("could NOT be checked", sent[0])


class DlqNoiseAndClaimsTest(unittest.TestCase):
    """T-2187: one stuck message became ~20 alerts to the operator over 10 hours, each
    asserting a category its own payload contradicted, about a fault fixed in the
    first 7 minutes. Three defects: no set-based dedupe, a false claim, no
    staleness signal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inbox = Path(self.tmp.name) / "inbox"
        for sub in ("new", "cur", "tmp", "failed"):
            (self.inbox / sub).mkdir(parents=True)

    def _quarantine(self, name, mtime=1000.0):
        p = self.inbox / "failed" / name
        p.write_text("{}")
        os.utime(p, (mtime, mtime))
        return p

    def _ack(self, name, mtime=2000.0):
        p = self.inbox / "cur" / name
        p.write_text("{}")
        os.utime(p, (mtime, mtime))
        return p

    # ---------------------------------------------------- (1) set-based dedupe
    def test_unchanged_set_is_suppressed_inside_the_backstop(self):
        """THE BUG: a quarantine never self-clears, so a plain cooldown re-fires forever."""
        fp = watchdog._dlq_fingerprint(["a", "b"])
        state = {"specialist:failed": {"ts": 0.0, "fp": fp}}
        self.assertFalse(watchdog._should_alert("specialist:failed", 3600.0, state, fp),
                         "same quarantine set an hour later must NOT re-alert")

    def test_changed_set_alerts_immediately(self):
        state = {"specialist:failed": {"ts": 0.0, "fp": watchdog._dlq_fingerprint(["a"])}}
        self.assertTrue(watchdog._should_alert("specialist:failed", 60.0, state,
                                        watchdog._dlq_fingerprint(["a", "b"])),
                        "a new quarantine is a new condition — alert now")

    def test_swap_keeping_count_equal_still_alerts(self):
        """Count-based dedupe would miss this; the fingerprint is over the SET."""
        state = {"specialist:failed": {"ts": 0.0, "fp": watchdog._dlq_fingerprint(["a"])}}
        self.assertTrue(watchdog._should_alert("specialist:failed", 60.0, state,
                                        watchdog._dlq_fingerprint(["b"])))

    def test_backstop_refires_after_the_long_interval(self):
        fp = watchdog._dlq_fingerprint(["a"])
        state = {"specialist:failed": {"ts": 0.0, "fp": fp}}
        self.assertTrue(watchdog._should_alert("specialist:failed",
                                        watchdog.PERSISTENT_REALERT_SECONDS + 1, state, fp))

    def test_legacy_bare_timestamp_state_still_works(self):
        """Old state files hold a bare float; upgrading must not crash or spam."""
        self.assertTrue(watchdog._should_alert("specialist:failed", 60.0,
                                        {"specialist:failed": 0.0}, watchdog._dlq_fingerprint(["a"])))

    def test_transient_backlog_alert_regime_is_unchanged(self):
        """No fingerprint => the original cooldown behaviour, bit for bit."""
        state = {"specialist": 0.0}
        self.assertFalse(watchdog._should_alert("specialist", watchdog.ALERT_COOLDOWN_SECONDS - 1, state))
        self.assertTrue(watchdog._should_alert("specialist", watchdog.ALERT_COOLDOWN_SECONDS + 1, state))

    # ------------------------------------------- (3) leftover-vs-live signal
    def test_acked_since_quarantine_means_leftover(self):
        self._quarantine("q1", mtime=1000.0)
        self._ack("m1", mtime=2000.0)
        self.assertIs(watchdog.quarantine_is_leftover(self.inbox, ["q1"]), True)

    def test_nothing_acked_since_means_possibly_live(self):
        self._ack("m1", mtime=500.0)
        self._quarantine("q1", mtime=1000.0)
        self.assertIs(watchdog.quarantine_is_leftover(self.inbox, ["q1"]), False)

    def test_no_acked_messages_at_all_is_unknown(self):
        self._quarantine("q1")
        self.assertIsNone(watchdog.quarantine_is_leftover(self.inbox, ["q1"]))

    def test_unreadable_cur_is_unknown_not_a_guess(self):
        self._quarantine("q1")
        self.assertIsNone(watchdog.quarantine_is_leftover(self.inbox / "nope", ["q1"]))

    # -------------------------------------------------- (2) no false claims
    def test_emitted_text_makes_no_claim_about_the_payload(self):
        """The claim that broke trust: it told the operator every quarantine is
        'genuinely poisoned content worth reading', ~20 times, about a benign
        keep_going nudge that died on the context-wedge path.

        Asserted against the EMITTED STRING, not a grep of the source — the source
        legitimately quotes the old wording in a comment explaining why it went, and
        a whole-file grep cannot tell an assertion from its own post-mortem. The
        property is about what reaches the reader, so test what reaches the reader.
        """
        for leftover in (True, False, None):
            body = watchdog.build_dlq_body("specialist", 1, ["q1"], leftover)
            with self.subTest(leftover=leftover):
                self.assertNotIn("genuinely poisoned", body)
                self.assertNotIn("not a false-positive heartbeat timeout", body)

    def test_emitted_text_names_what_it_did_not_determine(self):
        body = watchdog.build_dlq_body("specialist", 1, ["q1"], None)
        self.assertIn("NOT DETERMINED HERE", body)
        self.assertIn("-32603", body, "the context-wedge path must be named as an alternative")

    def test_leftover_verdict_says_leftover_and_live_says_stuck(self):
        self.assertIn("LEFTOVERS", watchdog.build_dlq_body("specialist", 1, ["q1"], True))
        self.assertIn("STILL be stuck", watchdog.build_dlq_body("specialist", 1, ["q1"], False))
        self.assertIn("Could not determine",
                      watchdog.build_dlq_body("specialist", 1, ["q1"], None))

    def test_unknown_leftover_never_claims_either_direction(self):
        """None is UNKNOWN: it must not imply stale OR live."""
        body = watchdog.build_dlq_body("specialist", 1, ["q1"], None)
        self.assertNotIn("LEFTOVERS", body)
        self.assertNotIn("STILL be stuck", body)


if __name__ == "__main__":
    unittest.main()
