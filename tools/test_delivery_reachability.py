"""test_delivery_reachability.py — can each seat actually receive a ticket? (T-2232)

The property under test is the one T-2144's classification guard could not see: artisan was
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
    "artisan": {"account": "suborch2", "pane": "suborch2-main:0"},
    "mason": {"account": "suborch1", "pane": "suborch1-main:0"},
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
    return {"suborch2-main:0": "artisan", "suborch1-main:0": "mason",
            "claude-research-bot:0": "specialist"}.get(pane, "?")


class StateTest(unittest.TestCase):
    def _probe(self, agent, mode="send-keys", **kw):
        return dr.probe_agent(agent, mode, SUB[agent], FakeOps(**kw))

    # ⭐ the split that a pause manifest cannot express -----------------------
    def test_never_installed_is_MISSING_not_disabled(self):
        r = self._probe("artisan", units={"artisan": "missing"})
        self.assertEqual(r["state"], dr.MISSING)
        self.assertIn("never installed", r["detail"])

    def test_switched_off_is_DISABLED_and_does_not_alert(self):
        r = self._probe("mason", units={"mason": "disabled"})
        self.assertEqual(r["state"], dr.DISABLED)
        self.assertNotIn(r["state"], dr.ALERTING,
                         "somebody ran `disable`; a deliberate act is an authorised no")

    def test_MISSING_alerts_but_DISABLED_does_not(self):
        self.assertIn(dr.MISSING, dr.ALERTING)
        self.assertNotIn(dr.DISABLED, dr.ALERTING)

    # -- enabled but pointed at nothing --------------------------------------
    def test_enabled_with_no_tmux_session_is_BROKEN(self):
        r = self._probe("artisan", units={"artisan": "enabled"}, sessions={"artisan": False})
        self.assertEqual(r["state"], dr.BROKEN)

    def test_enabled_with_a_live_session_is_READY(self):
        r = self._probe("artisan", units={"artisan": "enabled"}, sessions={"artisan": True})
        self.assertEqual(r["state"], dr.READY)

    # -- unaskable is never green -------------------------------------------
    def test_unqueryable_manager_is_UNKNOWN(self):
        ops = FakeOps()
        ops.unit_state = lambda account, agent: None
        r = dr.probe_agent("artisan", "send-keys", SUB["artisan"], ops)
        self.assertEqual(r["state"], dr.UNKNOWN)
        self.assertIn(dr.UNKNOWN, dr.ALERTING)

    def test_unqueryable_tmux_is_UNKNOWN_not_broken(self):
        ops = FakeOps(units={"artisan": "enabled"})
        ops.tmux_session_exists = lambda account, pane: None
        r = dr.probe_agent("artisan", "send-keys", SUB["artisan"], ops)
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
        self.assertIsNone(self._ops_with(1, "").unit_state("suborch2", "artisan"),
                          "a denied/failed command yields empty stdout too — it is not evidence "
                          "that the unit is absent")

    def test_timeout_is_UNASKABLE(self):
        self.assertIsNone(self._ops_with(255, "").unit_state("suborch2", "artisan"))

    def test_successful_empty_query_IS_absent(self):
        self.assertEqual(self._ops_with(0, "").unit_state("suborch2", "artisan"), "missing",
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
            # T-4758: an account guaranteed NOT to be ours. A fleet name like "suborch2" is
            # the current user on that seat, where _sudo_user rightly skips sudo.
            other = "not-" + dr._me()
            ops._sudo_user(other, "true")
        self.assertEqual(seen["argv"][:4], ["sudo", "-n", "-u", other])


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
        modes = {"artisan": "send-keys", "mason": "send-keys", "specialist": "mailbox"}
        out = dr.probe_all(FakeOps(), substrate=SUB, modes=modes)
        self.assertEqual(sorted(r["agent"] for r in out), ["specialist", "artisan", "mason"])

    def test_never_consults_consumer_outcomes(self):
        """The whole reason this module exists: mason's consumer passed for weeks on
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


class MaintenanceHoldTest(unittest.TestCase):
    """T-5254: a seat in an ACTIVE seat-maintenance window is HELD, not alerting."""
    MODES = {"mason": "send-keys", "artisan": "send-keys"}

    def _ops(self):
        return FakeOps(units={"mason": "missing", "artisan": "missing"})

    def _held(self, *held):
        return unittest.mock.patch("tools.notify_policy.in_maintenance",
                                   side_effect=lambda a, *x, **k: a in held)

    def test_held_seat_is_held_not_alerting_and_never_probed(self):
        ops = self._ops()
        ops.unit_state = unittest.mock.Mock(side_effect=ops.unit_state)
        with self._held("mason"):
            out = {r["agent"]: r for r in dr.probe_all(ops, substrate=SUB, modes=self.MODES)}
        self.assertEqual(out["mason"]["state"], dr.HELD)
        self.assertNotIn(dr.HELD, dr.ALERTING)
        self.assertEqual(out["artisan"]["state"], dr.MISSING)
        probed = [c.args[1] for c in ops.unit_state.call_args_list]
        self.assertNotIn("mason", probed)

    def test_main_does_not_page_for_held_seat(self):
        ops = self._ops()
        with self._held("mason", "artisan"), \
                unittest.mock.patch.object(dr, "Ops", return_value=ops), \
                unittest.mock.patch.object(dr, "_substrate", return_value=SUB), \
                unittest.mock.patch.object(dr, "_delivery_modes", return_value=self.MODES):
            dr.main([])
        self.assertEqual(ops.alerts, [])

    def test_not_held_still_alerts_as_today(self):
        ops = self._ops()
        with self._held(), \
                unittest.mock.patch.object(dr, "Ops", return_value=ops), \
                unittest.mock.patch.object(dr, "_substrate", return_value=SUB), \
                unittest.mock.patch.object(dr, "_delivery_modes", return_value=self.MODES):
            dr.main([])
        self.assertEqual(len(ops.alerts), 1)
        self.assertIn("MISSING mason", ops.alerts[0][0])

    def test_held_seat_does_not_mask_other_seat_alert(self):
        ops = self._ops()
        with self._held("mason"), \
                unittest.mock.patch.object(dr, "Ops", return_value=ops), \
                unittest.mock.patch.object(dr, "_substrate", return_value=SUB), \
                unittest.mock.patch.object(dr, "_delivery_modes", return_value=self.MODES):
            dr.main([])
        self.assertEqual(len(ops.alerts), 1)
        self.assertIn("MISSING artisan", ops.alerts[0][0])
        self.assertNotIn("mason", ops.alerts[0][0])

    def _seat(self, **kw):
        from tools.seat_state_testkit import eff
        return unittest.mock.patch("tools.seat_state.safe_effective_for", return_value=eff(**kw))

    def test_draining_deep_sleep_seat_is_held_and_never_probed_T5527(self):
        # T-6302: only a seat whose phase is ASLEEP (expected_down) leaves HELD; a seat still
        # draining toward sleep is unchanged.
        ops = self._ops()
        ops.unit_state = unittest.mock.Mock(side_effect=ops.unit_state)
        with self._held(), self._seat(state="deep-sleep", asleep=False):
            out = {r["agent"]: r for r in dr.probe_all(ops, substrate=SUB, modes=self.MODES)}
        self.assertEqual(out["mason"]["state"], dr.HELD)
        self.assertIn("deep-sleep", out["mason"]["detail"])
        self.assertNotIn("mason", [c.args[1] for c in ops.unit_state.call_args_list])

    def test_paused_intake_and_oom_only_seats_are_probed_normally_T5527(self):
        for seat in (self._seat(state="paused-intake"), self._seat(oom=True)):
            ops = self._ops()
            with self._held(), seat:
                out = {r["agent"]: r for r in dr.probe_all(ops, substrate=SUB, modes=self.MODES)}
            self.assertEqual(out["mason"]["state"], dr.MISSING)

    def test_live_oom_shape_oom_hold_plus_maintenance_rearm_is_held_T5527(self):
        ops = self._ops()
        with self._held("mason"), self._seat(oom=True, maintenance=True):
            out = {r["agent"]: r for r in dr.probe_all(ops, substrate=SUB, modes=self.MODES)}
        self.assertEqual(out["mason"]["state"], dr.HELD)
        self.assertIn("seat-maintenance ACTIVE", out["mason"]["detail"])   # text unchanged

    def test_main_does_not_page_for_a_sleeping_seat_T5527(self):
        ops = self._ops()
        with self._held(), self._seat(state="deep-sleep", asleep=True), \
                unittest.mock.patch.object(dr, "Ops", return_value=ops), \
                unittest.mock.patch.object(dr, "_substrate", return_value=SUB), \
                unittest.mock.patch.object(dr, "_delivery_modes", return_value=self.MODES):
            dr.main([])
        self.assertEqual(ops.alerts, [])

    def test_policy_fault_fails_open(self):
        ops = self._ops()
        with unittest.mock.patch("tools.notify_policy.in_maintenance", side_effect=OSError("boom")):
            out = {r["agent"]: r for r in dr.probe_all(ops, substrate=SUB, modes=self.MODES)}
        self.assertEqual(out["mason"]["state"], dr.MISSING)
        self.assertFalse(dr._seat_in_maintenance("mason"))


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "seat_state")

# The routed axes.yaml change (T-6302): the parse snippet of delivery-mode-substrate-match with
# the PASS set widened from READY to READY|ASLEEP. Proven here, as a string, before Orchestrator applies it.
PROPOSED_CHECK_REF = (
    "python3 /home/<account>/pantheon-config/tools/delivery_reachability.py --json --dry-run | "
    "python3 -c 'import json,sys; d=json.load(sys.stdin); "
    "items=d if isinstance(d,list) else d.get(\"results\",[]); "
    "r=[x for x in items if x.get(\"agent\")==\"<persona>\"]; "
    "print(\"true\" if r and r[0].get(\"state\") in (\"READY\",\"ASLEEP\") else \"false\")'"
)


def _fixture_seat(name):
    """EffectiveSeatState built from a committed seat-state record via the real parser."""
    import json
    from tools import seat_state as ss
    with open(os.path.join(FIXTURES, name)) as fh:
        doc = json.load(fh)
    assert ss.validate_record(doc) == []
    c = ss.parse_record(doc)
    return ss._combine(c, ss.Contribution(), ss.Contribution(), ss.Contribution())


class AsleepStateTest(unittest.TestCase):
    """T-6302: a deep-sleeping (expected_down) seat is ASLEEP, not READY and not failing."""
    MODES = {"mason": "send-keys", "artisan": "send-keys"}

    def _run(self, seat, sessions=None, units=None):
        ops = FakeOps(units=units or {}, sessions=sessions or {})
        with unittest.mock.patch("tools.notify_policy.in_maintenance", return_value=False), \
                unittest.mock.patch("tools.seat_state.safe_effective_for",
                                    side_effect=lambda a, **k: seat if a == "mason" else None):
            out = {r["agent"]: r for r in dr.probe_all(ops, substrate=SUB, modes=self.MODES)}
        return out, ops

    def test_expected_down_without_pane_is_ASLEEP_not_READY_not_alerting(self):
        out, _ = self._run(_fixture_seat("deep_sleep_asleep.json"), sessions={"mason": False})
        self.assertEqual(out["mason"]["state"], dr.ASLEEP)
        self.assertNotEqual(out["mason"]["state"], dr.READY)
        self.assertNotIn(dr.ASLEEP, dr.ALERTING)

    def test_not_expected_down_without_pane_still_fails_as_today(self):
        for fx in ("paused_intake.json", None):
            seat = _fixture_seat(fx) if fx else None
            out, _ = self._run(seat, sessions={"mason": False})
            self.assertEqual(out["mason"]["state"], dr.BROKEN, fx)
            self.assertIn(dr.BROKEN, dr.ALERTING)

    def test_expected_down_with_pane_present_is_visible_SLEEP_BREACH(self):
        out, _ = self._run(_fixture_seat("deep_sleep_asleep.json"), sessions={"mason": True})
        st = out["mason"]["state"]
        self.assertEqual(st, dr.SLEEP_BREACH)
        self.assertNotIn(st, (dr.READY, dr.ASLEEP, dr.HELD))
        self.assertIn("recovery_watcher", out["mason"]["detail"])
        self.assertNotIn(st, dr.ALERTING)   # recovery_watcher / fleet_health own the page

    def test_expected_down_with_unaskable_tmux_is_UNKNOWN(self):
        out, _ = self._run(_fixture_seat("deep_sleep_asleep.json"), sessions={"mason": None})
        self.assertEqual(out["mason"]["state"], dr.UNKNOWN)
        self.assertIn(dr.UNKNOWN, dr.ALERTING)

    def test_draining_fixture_stays_HELD(self):
        out, _ = self._run(_fixture_seat("deep_sleep_draining.json"), sessions={"mason": False})
        self.assertEqual(out["mason"]["state"], dr.HELD)

    def test_asleep_seat_never_pages_via_main(self):
        ops = FakeOps(sessions={"mason": False})
        with unittest.mock.patch("tools.notify_policy.in_maintenance", return_value=False), \
                unittest.mock.patch("tools.seat_state.safe_effective_for",
                                    side_effect=lambda a, **k: _fixture_seat("deep_sleep_asleep.json")
                                    if a == "mason" else None), \
                unittest.mock.patch.object(dr, "Ops", return_value=ops), \
                unittest.mock.patch.object(dr, "_substrate", return_value=SUB), \
                unittest.mock.patch.object(dr, "_delivery_modes", return_value=self.MODES):
            dr.main([])
        self.assertEqual(ops.alerts, [])


class ProposedCheckRefTest(unittest.TestCase):
    """The routed axes.yaml one-liner, run as a string on tool-shaped JSON."""

    @staticmethod
    def _verdict(items, persona="mason"):
        import json
        import subprocess
        snippet = PROPOSED_CHECK_REF.split("python3 -c ", 1)[1].strip()
        assert snippet[0] == snippet[-1] == "'"
        snippet = snippet[1:-1].replace("<persona>", persona)
        r = subprocess.run([sys.executable, "-c", snippet], input=json.dumps(items),
                           capture_output=True, text=True, check=True)
        return r.stdout.strip()

    def _item(self, state, agent="mason"):
        return [{"agent": agent, "mode": "send-keys", "state": state, "detail": "x"}]

    def test_passes_on_READY_and_ASLEEP(self):
        self.assertEqual(self._verdict(self._item("READY")), "true")
        self.assertEqual(self._verdict(self._item("ASLEEP")), "true")

    def test_fails_on_everything_else(self):
        for st in ("SLEEP_BREACH", "HELD", "BROKEN", "MISSING", "DISABLED", "UNKNOWN"):
            self.assertEqual(self._verdict(self._item(st)), "false", st)
        self.assertEqual(self._verdict([]), "false")

    def test_snippet_is_fed_the_tools_real_output(self):
        # Round-trip: the real probe_all output for a fixture sleeper through the one-liner.
        import json
        out, _ = AsleepStateTest()._run(_fixture_seat("deep_sleep_asleep.json"), sessions={"mason": False})
        self.assertEqual(self._verdict(json.loads(json.dumps(list(out.values())))), "true")
        out, _ = AsleepStateTest()._run(_fixture_seat("deep_sleep_asleep.json"), sessions={"mason": True})
        self.assertEqual(self._verdict(list(out.values())), "false")


if __name__ == "__main__":
    unittest.main()
