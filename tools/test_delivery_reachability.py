"""test_delivery_reachability.py — can each seat actually receive a ticket? (T-2232)

The property under test is the one T-2144's classification guard could not see: atlas was
DECLARED deliverable and had no unit at all, and the guard stayed green for that seat's whole
existence. These tests pin the distinction between a declaration and a capability, and — the
load-bearing one — between a unit somebody switched OFF and a unit that was never installed.
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import delivery_reachability as dr  # noqa: E402

SUB = {
    "atlas": {"account": "suborch2", "pane": "suborch2-main:0"},
    "metis": {"account": "suborch1", "pane": "suborch1-main:0"},
    "specialist": {"account": "claude-research", "pane": "claude-research-bot:0"},
}


class FakeOps(dr.Ops):
    def __init__(self, *, units=None, sessions=None, inboxes=None):
        self.units = units or {}
        self.sessions = sessions or {}
        self.inboxes = inboxes or {}
        self.alerts = []
        self.consumer_reads = 0          # must stay 0 — see test_never_consults_consumer_outcomes

    def unit_state(self, account, agent):
        return self.units.get(agent, "enabled")

    def tmux_session_exists(self, account, pane):
        return self.sessions.get(agent_of(pane), True)

    def inbox_exists(self, agent):
        return self.inboxes.get(agent, True)

    def alert(self, msg, vocab):
        self.alerts.append((msg, vocab))


def agent_of(pane):
    return {"suborch2-main:0": "atlas", "suborch1-main:0": "metis",
            "claude-research-bot:0": "specialist"}.get(pane, "?")


class StateTest(unittest.TestCase):
    def _probe(self, agent, mode="send-keys", **kw):
        return dr.probe_agent(agent, mode, SUB[agent], FakeOps(**kw))

    # ⭐ the split that a pause manifest cannot express -----------------------
    def test_never_installed_is_MISSING_not_disabled(self):
        r = self._probe("atlas", units={"atlas": "missing"})
        self.assertEqual(r["state"], dr.MISSING)
        self.assertIn("never installed", r["detail"])

    def test_switched_off_is_DISABLED_and_does_not_alert(self):
        r = self._probe("metis", units={"metis": "disabled"})
        self.assertEqual(r["state"], dr.DISABLED)
        self.assertNotIn(r["state"], dr.ALERTING,
                         "somebody ran `disable`; a deliberate act is an authorised no")

    def test_MISSING_alerts_but_DISABLED_does_not(self):
        self.assertIn(dr.MISSING, dr.ALERTING)
        self.assertNotIn(dr.DISABLED, dr.ALERTING)

    # -- enabled but pointed at nothing --------------------------------------
    def test_enabled_with_no_tmux_session_is_BROKEN(self):
        r = self._probe("atlas", units={"atlas": "enabled"}, sessions={"atlas": False})
        self.assertEqual(r["state"], dr.BROKEN)

    def test_enabled_with_a_live_session_is_READY(self):
        r = self._probe("atlas", units={"atlas": "enabled"}, sessions={"atlas": True})
        self.assertEqual(r["state"], dr.READY)

    # -- unaskable is never green -------------------------------------------
    def test_unqueryable_manager_is_UNKNOWN(self):
        ops = FakeOps()
        ops.unit_state = lambda account, agent: None
        r = dr.probe_agent("atlas", "send-keys", SUB["atlas"], ops)
        self.assertEqual(r["state"], dr.UNKNOWN)
        self.assertIn(dr.UNKNOWN, dr.ALERTING)

    def test_unqueryable_tmux_is_UNKNOWN_not_broken(self):
        ops = FakeOps(units={"atlas": "enabled"})
        ops.tmux_session_exists = lambda account, pane: None
        r = dr.probe_agent("atlas", "send-keys", SUB["atlas"], ops)
        self.assertEqual(r["state"], dr.UNKNOWN,
                         "'could not ask' must not be reported as 'provably gone'")

    def test_agent_absent_from_the_substrate_map_is_UNKNOWN(self):
        r = dr.probe_agent("ghost", "send-keys", {}, FakeOps())
        self.assertEqual(r["state"], dr.UNKNOWN)

    # -- the mailbox mode ----------------------------------------------------
    def test_mailbox_mode_missing_inbox_is_BROKEN(self):
        r = dr.probe_agent("specialist", "mailbox", SUB["specialist"], FakeOps(inboxes={"specialist": False}))
        self.assertEqual(r["state"], dr.BROKEN)

    def test_mailbox_mode_present_inbox_is_READY(self):
        r = dr.probe_agent("specialist", "mailbox", SUB["specialist"], FakeOps(inboxes={"specialist": True}))
        self.assertEqual(r["state"], dr.READY)


class ProbeSeamTest(unittest.TestCase):
    """The seam the state tests could not see, because FakeOps stubs unit_state whole.

    THE BUG THESE EXIST FOR, found by running the probe against the real fleet: it reported
    MISSING for recovery — a seat with a demonstrably live pantheon-inbox-consumer@recovery.timer.
    `sudo -n -u claude-fallback` FROM claude-fallback is denied (recovery's grant covers the OTHER
    accounts; sudo-to-self is a separate grant nobody made), the denial goes to stderr, and the
    empty stdout was read as 'no such unit'. A fabricated fault, one layer below every fixture.
    """

    def _ops_with(self, rc, out):
        ops = dr.Ops()
        ops._sudo_user = lambda account, cmd: (rc, out)
        return ops

    def test_failed_probe_is_UNASKABLE_not_absent(self):
        self.assertIsNone(self._ops_with(1, "").unit_state("suborch2", "atlas"),
                          "a denied/failed command yields empty stdout too — it is not evidence "
                          "that the unit is absent")

    def test_timeout_is_UNASKABLE(self):
        self.assertIsNone(self._ops_with(255, "").unit_state("suborch2", "atlas"))

    def test_successful_empty_query_IS_absent(self):
        self.assertEqual(self._ops_with(0, "").unit_state("suborch2", "atlas"), "missing",
                         "rc==0 with no rows is the one case that really means absent")

    def test_own_account_is_probed_WITHOUT_sudo(self):
        seen = {}
        ops = dr.Ops()
        real = subprocess_run_capture(seen)
        with unittest.mock.patch.object(dr.subprocess, "run", real):
            ops._sudo_user(dr._me(), "true")
        self.assertNotIn("sudo", seen["argv"],
                         "sudo-to-self is denied; probing our own seat must not go through it")

    def test_other_account_IS_probed_with_sudo(self):
        seen = {}
        ops = dr.Ops()
        with unittest.mock.patch.object(dr.subprocess, "run", subprocess_run_capture(seen)):
            ops._sudo_user("suborch2", "true")
        self.assertEqual(seen["argv"][:3], ["sudo", "-n", "-u"])


def subprocess_run_capture(seen):
    class R:
        returncode, stdout = 0, ""

    def fake(argv, **kw):
        seen["argv"] = argv
        return R()
    return fake


class InventoryTest(unittest.TestCase):
    def test_unreadable_delivery_mode_is_UNKNOWN_not_an_empty_clean_sweep(self):
        # A guard that iterates an empty inventory reports zero faults over zero seats and
        # calls it healthy. The denominator has to be checked.
        out = dr.probe_all(FakeOps(), substrate=SUB, modes={})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["state"], dr.UNKNOWN)
        self.assertIn("not the same as empty", out[0]["detail"])

    def test_every_declared_agent_gets_a_verdict(self):
        modes = {"atlas": "send-keys", "metis": "send-keys", "specialist": "mailbox"}
        out = dr.probe_all(FakeOps(), substrate=SUB, modes=modes)
        self.assertEqual(sorted(r["agent"] for r in out), ["specialist", "atlas", "metis"])

    def test_never_consults_consumer_outcomes(self):
        """The whole reason this module exists: metis's consumer passed for weeks on
        `skip:empty_inbox`, never reaching the inject step. A run that does not exercise
        the path cannot testify about it — so no verdict here may depend on one."""
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "delivery_reachability.py")) as fh:
            src = fh.read()
        body = src.split('"""', 2)[2]      # skip the module docstring, which discusses them
        for forbidden in ("skip:empty_inbox", "log_outcome", "journalctl", "last_outcome"):
            self.assertNotIn(forbidden, body,
                             f"a verdict must never be derived from {forbidden!r} — that is the "
                             f"vacuous-pass this module was built to break")


if __name__ == "__main__":
    unittest.main()
