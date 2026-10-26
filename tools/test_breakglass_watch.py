#!/usr/bin/env python3
"""Tests for tools/breakglass_watch.py (T-4392 phase 2a): mail Recovery ONCE when a live flag
appears and ONCE 1h before it expires, never every cycle; prune gone flags; retry a failed
send; ignore rejected files. Plus the real listing path over a temp base, and the monitor
wiring on recovery's cycle."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "tools" / "hooks"))
from tools import breakglass_watch as bw  # noqa: E402
import _failclosed as fc  # noqa: E402

NOW = 1_800_000_000.0


def _live(account="claude", expiry=NOW + 6 * 3600, ino=11, ctime_ns=22, hooks=("mcp-list-guard",)):
    return fc.FlagStatus(account, live=True, reason="live", owner_uid=0, owner_name="root",
                         expiry=expiry, hooks=hooks, note="probe wedged", ino=ino, ctime_ns=ctime_ns)


class Dedup(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.state = Path(td.name) / "state" / "breakglass_alerts.json"
        self.mail = []

    def cycle(self, statuses, now=NOW, send=None):
        return bw.check_and_alert(statuses=statuses, state_path=self.state,
                                  send=send or (lambda m, v: self.mail.append((v, m))), now=now)

    def test_created_once_then_silent(self):
        s = _live()
        self.assertEqual(len(self.cycle([s])), 1)
        self.assertEqual(self.mail[0][0], "breakglass")
        self.assertIn("LIVE break-glass flag on claude", self.mail[0][1])
        self.assertIn("hooks=mcp-list-guard owner=root", self.mail[0][1])
        self.assertIn("probe wedged", self.mail[0][1])
        for k in range(1, 6):  # five more 5-min cycles
            self.assertEqual(self.cycle([s], now=NOW + 300 * k), [])
        self.assertEqual(len(self.mail), 1)

    def test_expiring_once_at_t_minus_1h(self):
        s = _live(expiry=NOW + 3 * 3600)
        self.cycle([s])
        self.assertEqual(self.cycle([s], now=NOW + 3600), [])           # 2h left
        sent = self.cycle([s], now=NOW + 2 * 3600 + 60)                  # 59m left
        self.assertEqual(len(sent), 1)
        self.assertIn("expires within 1h", sent[0])
        self.assertEqual(self.cycle([s], now=NOW + 2 * 3600 + 360), [])
        self.assertEqual(len(self.mail), 2)

    def test_short_flag_gets_both_on_first_sight(self):
        self.assertEqual(len(self.cycle([_live(expiry=NOW + 1800)])), 2)

    def test_gone_flag_is_pruned_and_recreation_alerts_again(self):
        self.cycle([_live()])
        self.cycle([], now=NOW + 300)
        self.assertEqual(json.loads(self.state.read_text()), {})
        self.assertEqual(len(self.cycle([_live(ctime_ns=99)], now=NOW + 600)), 1)

    def test_rewritten_flag_alerts_again(self):
        self.cycle([_live()])
        self.assertEqual(len(self.cycle([_live(expiry=NOW + 7 * 3600, ctime_ns=23)], now=NOW + 300)), 1)

    def test_per_account(self):
        sent = self.cycle([_live("claude"), _live("suborch1", ino=12)])
        self.assertEqual(len(sent), 2)
        self.assertEqual(self.cycle([_live("claude"), _live("suborch1", ino=12)], now=NOW + 300), [])

    def test_failed_send_retries_next_cycle(self):
        def down(m, v):
            raise OSError("router down")
        self.assertEqual(self.cycle([_live()], send=down), [])
        self.assertEqual(len(self.cycle([_live()], now=NOW + 300)), 1)

    def test_unwritable_state_never_re_mails(self):
        # The state is saved BEFORE sending: a failing save sends nothing (the monitor wrapper
        # logs the raise), rather than mailing every cycle with no record of having done so.
        with mock.patch.object(bw, "_save", side_effect=OSError("read-only fs")):
            for k in range(3):
                with self.assertRaises(OSError):
                    self.cycle([_live()], now=NOW + 300 * k)
        self.assertEqual(self.mail, [])
        self.assertEqual(len(self.cycle([_live()], now=NOW + 900)), 1)  # state writable again
        self.assertEqual(self.cycle([_live()], now=NOW + 1200), [])

    def test_state_recorded_before_the_send(self):
        seen = []

        def send(m, v):
            seen.append(json.loads(self.state.read_text()))
        self.cycle([_live()], send=send)
        self.assertEqual(len(seen), 1)
        self.assertIn("created_sent", next(iter(seen[0].values())))

    def test_failed_send_with_unwritable_revert_does_not_storm(self):
        calls = {"n": 0}
        real_save = bw._save

        def save(path, state):
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError("disk full")  # the revert save fails
            real_save(path, state)

        def down(m, v):
            raise OSError("router down")
        with mock.patch.object(bw, "_save", side_effect=save):
            self.assertEqual(self.cycle([_live()], send=down), [])
        self.assertEqual(self.cycle([_live()], now=NOW + 300), [])  # recorded: no repeat

    def test_rejected_files_never_alert(self):
        rej = fc.FlagStatus("claude", reason="flag is owned by the hook's own uid", owner_name="claude")
        self.assertEqual(self.cycle([rej]), [])
        self.assertFalse(self.state.exists())

    def test_corrupt_state_is_treated_as_empty(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text("{not json")
        self.assertEqual(len(self.cycle([_live()])), 1)


class RealListing(unittest.TestCase):
    def test_list_flags_over_a_temp_base(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "base"
            (base / "acct").mkdir(parents=True)
            os.chmod(base, 0o755)
            os.chmod(base / "acct", 0o755)
            flag = base / "acct" / "flag"
            flag.write_text(f"v1 {int(time.time()) + 3600} *\n")
            os.chmod(flag, 0o644)
            (base / "empty").mkdir(mode=0o755)
            statuses = fc.list_flags(base=str(base))
            self.assertEqual([s.account for s in statuses], ["acct"])  # no flag -> not listed
            mail = []
            sent = bw.check_and_alert(statuses=statuses, state_path=Path(td) / "st.json",
                                      send=lambda m, v: mail.append(m))
            if os.geteuid() != 0:  # the test user's base is not root-owned -> REJECTED, silent
                self.assertFalse(statuses[0].live)
                self.assertEqual(sent, [])

    def test_absent_base_lists_nothing(self):
        self.assertEqual(fc.list_flags(base="/nonexistent-t4392-base"), [])


class MonitorWiring(unittest.TestCase):
    def test_recovery_cycle_runs_the_watch_and_never_raises(self):
        from tools import context_fill_monitor as cfm
        with mock.patch.object(bw, "check_and_alert", return_value=["m1"]) as cycle, \
                mock.patch.object(cfm, "_log") as log:
            cfm.maybe_alert_breakglass("recovery")
        cycle.assert_called_once_with()
        self.assertIn("breakglass: alerted Recovery: m1", log.call_args[0][1])
        with mock.patch.object(bw, "check_and_alert", side_effect=RuntimeError("x")), \
                mock.patch.object(cfm, "_log") as log:
            cfm.maybe_alert_breakglass("recovery")  # must not raise
        self.assertIn("watch cycle failed", log.call_args[0][1])

    def test_called_only_in_the_recovery_branch(self):
        src = (_REPO / "tools" / "context_fill_monitor.py").read_text(encoding="utf-8")
        i = src.index('    if agent == "recovery":\n')
        self.assertIn("maybe_alert_breakglass(agent)", src[i:i + 400])
        self.assertEqual(src.count("maybe_alert_breakglass(agent)"), 1)


if __name__ == "__main__":
    unittest.main()
