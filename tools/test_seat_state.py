"""Tests for tools/seat_state.py (ORG-106 L0, T-5523). Tempdirs only."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import types
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools import harness_state as hs
from tools import notify_policy
from tools import seat_state as ss

NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def record(**over):
    doc = {
        "schema": "pantheon-seat-state/v1",
        "persona": "specialist",
        "state": "paused-intake",
        "phase": "set",
        "phase_ts": iso(NOW - timedelta(hours=1)),
        "set_ts": iso(NOW - timedelta(hours=1)),
        "set_by": "operator:orchestrator",
        "owner": "role:head_engineering_orchestrator",
        "reason": "waiting on a fix",
        "ticket": None,
        "expires": iso(NOW + timedelta(hours=3)),
        "indefinite": False,
        "telegram_dark_ack": False,
        "pre_expiry_notified": False,
        "ledger_entry": "x",
    }
    doc.update(over)
    return doc


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.root = self.dir / "state"
        self.maint = str(self.dir / "maint.json")
        self.oom = self.dir / "oom.json"  # absent unless a test writes it

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, doc):
        p = hs.seat_state_path("specialist", root=self.root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(doc if isinstance(doc, str) else json.dumps(doc))

    def eff(self, **kw):
        kw.setdefault("now", NOW)
        kw.setdefault("state_root", self.root)
        kw.setdefault("maintenance_registry", self.maint)
        kw.setdefault("oom_holds_path", self.oom)
        return ss.effective("specialist", **kw)


class TestConstants(unittest.TestCase):
    def test_values(self):
        self.assertEqual(ss.TTL_DEFAULT[ss.STATE_PAUSED_INTAKE], timedelta(hours=4))
        self.assertEqual(ss.TTL_MAX[ss.STATE_PAUSED_INTAKE], timedelta(days=7))
        self.assertEqual(ss.TTL_DEFAULT[ss.STATE_DEEP_SLEEP], timedelta(hours=24))
        self.assertEqual(ss.TTL_MAX[ss.STATE_DEEP_SLEEP], timedelta(days=14))
        self.assertEqual(ss.PRE_EXPIRY_NOTICE, timedelta(hours=1))
        self.assertEqual(ss.INDEFINITE_FLAG_AGE, timedelta(days=7))
        self.assertEqual(ss.TELEGRAM_DARK_THRESHOLD, timedelta(hours=20))
        self.assertEqual(ss.SCHEMA, hs.SCHEMA_SEAT_STATE)


class TestValidate(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(ss.validate_record(record()), [])
        self.assertEqual(
            ss.validate_record(record(state="deep-sleep", phase="draining")), [])
        self.assertEqual(
            ss.validate_record(record(state="deep-sleep", phase="asleep")), [])
        self.assertEqual(
            ss.validate_record(record(expires=None, indefinite=True)), [])

    def test_not_a_dict(self):
        self.assertTrue(ss.validate_record([1]))

    def test_each_invariant(self):
        cases = {
            "schema": record(schema="nope"),
            "state": record(state="napping"),
            "reason-empty": record(reason=""),
            "reason-long": record(reason="x" * 201),
            "reason-unprintable": record(reason="a\nb"),
            "reason-type": record(reason=5),
            "expires-null-not-indef": record(expires=None, indefinite=False),
            "expires-with-indef": record(indefinite=True),
            "phase-for-paused": record(phase="asleep"),
            "phase-for-sleep": record(state="deep-sleep", phase="set"),
            "ts-expires": record(expires="tomorrow"),
            "ts-set": record(set_ts="2026-10-26"),
            "ts-phase": record(phase_ts=None),
            "indef-type": record(indefinite="yes"),
        }
        for name, doc in cases.items():
            with self.subTest(name):
                self.assertTrue(ss.validate_record(doc), name)

    def test_unhashable_state_does_not_raise(self):
        for bad in ([], ["paused-intake"], {}, {"a": 1}):
            with self.subTest(bad=bad):
                self.assertTrue(ss.validate_record(record(state=bad)))

    def test_wrong_typed_fields_never_raise(self):
        fields = ("schema", "state", "reason", "indefinite", "expires", "phase",
                  "owner", "set_ts", "phase_ts")
        for field in fields:
            for bad in ([], {}, 5, None, ["x"], {"k": "v"}, 1.5, True):
                with self.subTest(field=field, bad=bad):
                    doc = record(**{field: bad})
                    ss.validate_record(doc)
                    c = ss.parse_record(doc, now=NOW)
                    self.assertIsInstance(c, ss.Contribution)

    def test_reason_boundary(self):
        self.assertEqual(ss.validate_record(record(reason="x" * 200)), [])


class TestParse(unittest.TestCase):
    def test_paused(self):
        c = ss.parse_record(record(), now=NOW)
        self.assertEqual(c.state, "paused-intake")
        self.assertTrue(c.intake_blocked)
        self.assertFalse(c.launch_blocked)
        self.assertFalse(c.remediation_vetoed)
        self.assertFalse(c.expected_down)
        self.assertIsNone(c.fault)

    def test_sleep_phases(self):
        d = ss.parse_record(record(state="deep-sleep", phase="draining"), now=NOW)
        self.assertTrue(d.intake_blocked and d.launch_blocked and d.remediation_vetoed)
        self.assertFalse(d.expected_down)
        a = ss.parse_record(record(state="deep-sleep", phase="asleep"), now=NOW)
        self.assertTrue(a.expected_down)

    def test_expired_contributes_nothing(self):
        c = ss.parse_record(record(expires=iso(NOW)), now=NOW)  # expires <= now
        self.assertEqual(c.state, "active")
        self.assertFalse(c.intake_blocked)
        self.assertIsNone(c.fault)

    def test_indefinite_never_expires(self):
        far = NOW + timedelta(days=400)
        c = ss.parse_record(record(expires=None, indefinite=True), now=far)
        self.assertTrue(c.intake_blocked)

    def test_malformed_is_fault(self):
        c = ss.parse_record(record(state="bogus"), now=NOW)
        self.assertEqual(c.state, "active")
        self.assertFalse(c.intake_blocked)
        self.assertIn("seat-state", c.fault)
        self.assertTrue(ss.parse_record("junk", now=NOW).fault)


class TestEffective(Base):
    def test_no_sources(self):
        e = self.eff()
        self.assertEqual(e.state, "active")
        self.assertFalse(e.intake_blocked or e.launch_blocked
                         or e.remediation_vetoed or e.expected_down)
        self.assertEqual(e.sources, ())
        self.assertIsNone(e.fault)
        self.assertIsNone(e.owner)

    def test_live_paused(self):
        self.put(record())
        e = self.eff()
        self.assertEqual(e.state, "paused-intake")
        self.assertTrue(e.intake_blocked)
        self.assertFalse(e.launch_blocked)
        self.assertEqual(e.sources, ("seat-state",))
        self.assertEqual(e.owner, "role:head_engineering_orchestrator")
        self.assertEqual(e.reason, "waiting on a fix")
        self.assertEqual(e.expires, iso(NOW + timedelta(hours=3)))

    def test_expired_paused(self):
        self.put(record(expires=iso(NOW - timedelta(minutes=1))))
        e = self.eff()
        self.assertEqual(e.state, "active")
        self.assertFalse(e.intake_blocked)
        self.assertEqual(e.sources, ())

    def test_sleep_draining_vs_asleep(self):
        self.put(record(state="deep-sleep", phase="draining"))
        e = self.eff()
        self.assertEqual(e.state, "deep-sleep")
        self.assertTrue(e.intake_blocked and e.launch_blocked and e.remediation_vetoed)
        self.assertFalse(e.expected_down)
        self.put(record(state="deep-sleep", phase="asleep"))
        self.assertTrue(self.eff().expected_down)

    def test_indefinite(self):
        self.put(record(expires=None, indefinite=True))
        e = self.eff(now=NOW + timedelta(days=100))
        self.assertTrue(e.intake_blocked)
        self.assertIsNone(e.expires)

    def test_malformed_record_faults_but_does_not_block(self):
        self.put(record(state="bogus"))
        e = self.eff()
        self.assertFalse(e.intake_blocked)
        self.assertEqual(e.state, "active")
        self.assertIn("seat-state", e.fault)

    def test_wrong_typed_fields_fail_open_through_effective(self):
        fields = ("schema", "state", "reason", "indefinite", "expires", "phase",
                  "owner", "set_ts", "phase_ts")
        for field in fields:
            for bad in ([], {}, 5, None, ["x"], {"k": "v"}):
                with self.subTest(field=field, bad=bad):
                    self.put(record(**{field: bad}))
                    e = self.eff()  # must not raise
                    if field == "owner" and not isinstance(bad, str):
                        continue  # owner is display-only; a bad one is tolerated
                    self.assertFalse(e.launch_blocked)
                    if e.fault:
                        self.assertFalse(e.intake_blocked)

    def test_unhashable_state_faults_without_raising(self):
        for bad in ([], {}):
            self.put(record(state=bad))
            e = self.eff()
            self.assertIn("seat-state", e.fault)
            self.assertFalse(e.intake_blocked)

    def test_parse_exception_is_contained(self):
        from unittest import mock
        self.put(record())
        with mock.patch.object(ss, "parse_record", side_effect=RuntimeError("boom")):
            e = self.eff()
        self.assertIn("boom", e.fault)
        self.assertFalse(e.intake_blocked)

    def test_corrupt_json_faults_but_does_not_block(self):
        self.put("{nope")
        e = self.eff()
        self.assertFalse(e.intake_blocked)
        self.assertTrue(e.fault)

    def test_invalid_persona_raises(self):
        with self.assertRaises(hs.HarnessStateError):
            ss.effective("../x", now=NOW, state_root=self.root)

    def _enter_maint(self, expires):
        Path(self.maint).write_text(json.dumps({
            "schema": notify_policy.MAINTENANCE_SCHEMA,
            "maintenance": {"specialist": {"by": "recovery", "reason": "cutover",
                                      "expires": expires}}}))

    def test_maintenance_active(self):
        self._enter_maint(iso(NOW + timedelta(minutes=30)))
        e = self.eff()
        self.assertTrue(e.intake_blocked and e.remediation_vetoed)
        self.assertFalse(e.launch_blocked)
        self.assertEqual(e.state, "active")
        self.assertEqual(e.sources, ("maintenance",))
        self.assertEqual((e.owner, e.reason), ("recovery", "cutover"))
        self.assertEqual(e.expires, iso(NOW + timedelta(minutes=30)))

    def test_maintenance_expired(self):
        self._enter_maint(iso(NOW - timedelta(minutes=1)))
        e = self.eff()
        self.assertFalse(e.intake_blocked)
        self.assertEqual(e.sources, ())

    def test_oom_hold(self):
        self.oom.write_text(json.dumps(
            {"specialist": {"since": "2026-10-26T11:00:00Z", "reason": "oom kill pid 9"}}))
        e = self.eff()
        self.assertTrue(e.intake_blocked and e.launch_blocked)
        self.assertFalse(e.remediation_vetoed)
        self.assertEqual(e.sources, ("oom-hold",))
        self.assertEqual(e.reason, "oom kill pid 9")
        self.assertIsNone(e.expires)

    def test_oom_other_persona_ignored(self):
        self.oom.write_text(json.dumps({"recovery": {"since": "x", "reason": "y"}}))
        self.assertEqual(self.eff().sources, ())

    def test_oom_unreadable_faults_but_does_not_block(self):
        self.oom.write_text("{garbage")
        e = self.eff()
        self.assertFalse(e.intake_blocked or e.launch_blocked)
        self.assertIn("oom-hold", e.fault)
        self.oom.write_text("[1]")
        self.assertIn("oom-hold", self.eff().fault)

    def test_autonomy_off_absent_path_not_consulted(self):
        f = self.dir / "autonomy_off"
        f.write_text("off")
        self.assertEqual(self.eff().sources, ())  # path not passed
        self.assertEqual(self.eff(autonomy_off_path=self.dir / "missing").sources, ())

    def test_autonomy_off_present(self):
        f = self.dir / "autonomy_off"
        f.write_text("off")
        e = self.eff(autonomy_off_path=f)
        self.assertTrue(e.intake_blocked)
        self.assertFalse(e.launch_blocked)
        self.assertEqual(e.sources, ("autonomy_off",))
        self.assertEqual(e.owner, "legacy")
        self.assertEqual(e.state, "active")

    def test_precedence(self):
        f = self.dir / "autonomy_off"
        f.write_text("off")
        self.put(record(owner="operator", reason="paused"))
        self._enter_maint(iso(NOW + timedelta(minutes=30)))
        e = self.eff(autonomy_off_path=f)
        self.assertEqual(e.owner, "recovery")  # maintenance > paused-intake > autonomy_off
        self.assertEqual(set(e.sources),
                         {"seat-state", "maintenance", "autonomy_off"})
        self.put(record(state="deep-sleep", phase="asleep", owner="operator",
                        reason="sleeping"))
        e = self.eff(autonomy_off_path=f)
        self.assertEqual(e.owner, "operator")  # deep-sleep > maintenance
        self.assertEqual(e.state, "deep-sleep")
        self.oom.write_text(json.dumps({"specialist": {"since": "s", "reason": "oom"}}))
        e = self.eff(autonomy_off_path=f)
        self.assertEqual(e.reason, "oom")  # oom > deep-sleep
        self.assertEqual(e.state, "deep-sleep")  # state stays the record's
        self.assertEqual(len(e.sources), 4)

    def test_paused_beats_autonomy_off(self):
        f = self.dir / "autonomy_off"
        f.write_text("off")
        self.put(record(owner="operator"))
        self.assertEqual(self.eff(autonomy_off_path=f).owner, "operator")

    def test_faults_from_two_sources_joined(self):
        self.put("{nope")
        self.oom.write_text("{garbage")
        e = self.eff()
        self.assertIn("seat-state", e.fault)
        self.assertIn("oom-hold", e.fault)

    def test_default_now(self):
        self.assertEqual(ss.effective(
            "specialist", state_root=self.root, maintenance_registry=self.maint,
            oom_holds_path=self.oom).state, "active")


class TestCli(Base):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ss.main(list(argv) + [
                "--state-root", str(self.root),
                "--maintenance-registry", self.maint,
                "--oom-holds", str(self.oom)])
        return rc, out.getvalue()

    def test_json(self):
        # The CLI reads the real clock, so expiry must be relative to it, not NOW.
        self.put(record(expires=iso(datetime.now(timezone.utc) + timedelta(hours=3))))
        rc, out = self.run_cli("status", "specialist", "--json")
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertEqual(doc["state"], "paused-intake")
        self.assertTrue(doc["intake_blocked"])
        self.assertEqual(doc["sources"], ["seat-state"])

    def test_text_active(self):
        rc, out = self.run_cli("status", "specialist")
        self.assertEqual(rc, 0)
        self.assertIn("specialist", out)
        self.assertIn("active", out)

    def test_text_paused_shows_owner_reason(self):
        # The CLI reads the real clock, so expiry must be relative to it, not NOW.
        self.put(record(expires=iso(datetime.now(timezone.utc) + timedelta(hours=3))))
        _, out = self.run_cli("status", "specialist")
        self.assertIn("paused-intake", out)
        self.assertIn("waiting on a fix", out)
        self.assertIn("role:head_engineering_orchestrator", out)

    def test_bad_persona(self):
        rc, _ = self.run_cli("status", "../x")
        self.assertEqual(rc, 2)


class TestEffectiveFor(Base):
    """T-5525: cross-account-aware entry point. Runner mocked; no real privilege helper."""

    def _runner(self, rc=0, out="", err="", exc=None):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            if exc:
                raise exc
            return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)
        run.calls = calls
        return run

    def call(self, persona="specialist", runner=None, user="someone-else", **kw):
        with unittest.mock.patch("tools.seat_state.getpass.getuser", return_value=user):
            return ss.effective_for(
                persona, now=NOW, state_root=self.root, maintenance_registry=self.maint,
                oom_holds_path=self.oom, runner=runner or self._runner(), **kw)

    def test_other_account_ok(self):
        run = self._runner(out=json.dumps(record()))
        e = self.call(runner=run)
        self.assertTrue(e.intake_blocked)
        self.assertEqual(e.sources, ("seat-state",))
        argv = run.calls[0][0]
        self.assertEqual(argv[:5], ["sudo", "-n", "-u", "claude-research", "cat"])
        self.assertEqual(
            argv[5], "/home/claude-research/.local/state/pantheon/personas/specialist/"
                     "overrides/seat-state.json")

    def test_other_account_missing_is_no_record(self):
        e = self.call(runner=self._runner(rc=1, err="cat: x: No such file or directory"))
        self.assertFalse(e.intake_blocked)
        self.assertIsNone(e.fault)

    def test_other_account_faults_fail_open(self):
        for run in (self._runner(rc=1, err="a password is required"),
                    self._runner(exc=subprocess.TimeoutExpired("x", 15)),
                    self._runner(exc=OSError("boom")),
                    self._runner(out="{not json")):
            e = self.call(runner=run)
            self.assertFalse(e.intake_blocked)
            self.assertIn("seat-state", e.fault)

    def test_fault_still_combines_other_sources(self):
        self.oom.write_text(json.dumps({"specialist": {"reason": "oom"}}))
        e = self.call(runner=self._runner(rc=1, err="denied"))
        self.assertTrue(e.intake_blocked)
        self.assertEqual(e.sources, ("oom-hold",))
        self.assertIn("seat-state", e.fault)

    def test_self_account_reads_directly_without_runner(self):
        self.put(record())
        run = self._runner()
        e = self.call(runner=run, user="claude-research")
        self.assertTrue(e.intake_blocked)
        self.assertEqual(run.calls, [])

    def test_matches_effective(self):
        self.put(record())
        a = ss.effective("specialist", now=NOW, state_root=self.root,
                         maintenance_registry=self.maint, oom_holds_path=self.oom)
        b = self.call(user="claude-research")
        self.assertEqual(a, b)

    def test_autonomy_off_path_honoured(self):
        off = self.dir / "autonomy_off"
        off.touch()
        e = self.call(autonomy_off_path=off)
        self.assertEqual(e.sources, ("autonomy_off",))


class TestMonitorHelpers(Base):
    """ORG-106 L4b (T-5527): fail-open helpers the monitors share."""

    def test_safe_effective_for_returns_none_on_raise(self):
        with unittest.mock.patch.object(ss, "effective_for", side_effect=RuntimeError("x")):
            self.assertIsNone(ss.safe_effective_for("specialist"))

    def test_safe_effective_for_passes_through(self):
        sentinel = object()
        with unittest.mock.patch.object(ss, "effective_for", return_value=sentinel) as m:
            self.assertIs(ss.safe_effective_for("specialist", now=NOW), sentinel)
        m.assert_called_once_with("specialist", now=NOW)

    def test_remediation_vetoed_true_for_deep_sleep_oom_and_maintenance(self):
        for kw in ({"deep": True}, {"maint": True}):
            with unittest.mock.patch.object(ss, "effective_for", return_value=_fake(**kw)):
                self.assertTrue(ss.remediation_vetoed("specialist"), kw)

    def test_remediation_vetoed_false_for_paused_and_active(self):
        for kw in ({}, {"paused": True}):
            with unittest.mock.patch.object(ss, "effective_for", return_value=_fake(**kw)):
                self.assertFalse(ss.remediation_vetoed("specialist"), kw)

    def test_maintenance_active_reports_a_fault_to_the_caller_hook(self):
        seen = []
        with unittest.mock.patch.object(notify_policy, "in_maintenance", side_effect=ValueError("x")):
            self.assertFalse(ss.maintenance_active("specialist", on_fault=seen.append))
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], ValueError)

    def test_remediation_vetoed_fail_open(self):
        with unittest.mock.patch.object(ss, "effective_for", side_effect=OSError("x")):
            self.assertFalse(ss.remediation_vetoed("specialist"))

    def test_maintenance_active_wraps_in_maintenance_failopen(self):
        with unittest.mock.patch.object(notify_policy, "in_maintenance", return_value=True) as m:
            self.assertIs(ss.maintenance_active("specialist"), True)
        m.assert_called_once_with("specialist")
        with unittest.mock.patch.object(notify_policy, "in_maintenance", side_effect=ValueError):
            self.assertIs(ss.maintenance_active("specialist"), False)
        with unittest.mock.patch.object(notify_policy, "in_maintenance", return_value=0):
            self.assertIs(ss.maintenance_active("specialist"), False)


def u5_record(**over):
    """The record a harness-manager run_state pause writes (T-5858 U5, harness-manager.py
    _plan_rs_pause): paused-intake, indefinite, plus pause_id / anchor / plan_revision.
    test_harness_manager_run_state.py pins that the planner's real record matches it."""
    return record(**{"state": "paused-intake", "phase": "set", "set_by": "germinate:harness-manager run_state",
                     "owner": "operator", "reason": "fleet bringdown: harness-manager run_state=paused (rs-x)",
                     "expires": None, "indefinite": True, "ledger_entry": None,
                     "pause_id": "rs-claude-research-20261026T110000Z", "anchor": "operator:TG-12733",
                     "plan_revision": "a" * 40, **over})


def u5_halted(**over):
    """Recovery D15 (T-5858) (r1): a halted U5 pause KEEPS its record and adds run_state_halt."""
    return u5_record(run_state_halt={"run_id": "r1", "op_id": "rs-drain", "at": iso(NOW),
                                     "rollback": "python3 -m tools.germinate rollback --run r1"}, **over)


class TestRunStatePauseVetoesRepair(Base):
    """T-5918: a U5 run_state pause vetoes automated unit repair (pantheon-parity-remediate)
    without changing what the monitor-facing fields say."""

    def test_parse_u5_record_sets_repair_vetoed_only(self):
        for doc in (u5_record(), u5_halted()):
            c = ss.parse_record(doc, now=NOW)
            self.assertTrue(c.repair_vetoed)
            # every other reader sees exactly today's paused-intake
            self.assertTrue(c.intake_blocked)
            self.assertFalse(c.remediation_vetoed or c.launch_blocked or c.expected_down)

    def test_operator_pause_and_bad_markers_do_not_veto(self):
        for doc in (record(), record(indefinite=True, expires=None), u5_record(pause_id=""),
                    u5_record(pause_id="   "), u5_record(pause_id=7), u5_record(pause_id=None)):
            self.assertFalse(ss.parse_record(doc, now=NOW).repair_vetoed, doc.get("pause_id"))

    def test_marker_on_deep_sleep_changes_nothing(self):
        doc = record(state="deep-sleep", phase="asleep", pause_id="rs-x")
        c = ss.parse_record(doc, now=NOW)
        self.assertTrue(c.remediation_vetoed)
        self.assertFalse(c.repair_vetoed)  # already vetoed via remediation_vetoed
        self.assertFalse(ss.is_run_state_pause(doc))

    def test_expired_u5_record_does_not_veto(self):
        doc = u5_record(indefinite=False, expires=iso(NOW - timedelta(seconds=1)))
        self.assertFalse(ss.parse_record(doc, now=NOW).repair_vetoed)

    def test_effective_carries_repair_vetoed_and_leaves_monitor_fields(self):
        self.put(u5_record())
        e = self.eff()
        self.assertTrue(e.repair_vetoed and ss.vetoes_repair(e))
        self.assertEqual((e.state, e.intake_blocked, e.launch_blocked, e.remediation_vetoed, e.expected_down),
                         ("paused-intake", True, False, False, False))
        self.assertEqual(e.sources, ("seat-state",))

    def test_vetoes_repair_union_and_fail_open(self):
        from tools.seat_state_testkit import eff
        self.assertTrue(ss.vetoes_repair(eff("deep-sleep", asleep=True)))
        self.assertTrue(ss.vetoes_repair(eff(maintenance=True)))
        self.assertTrue(ss.vetoes_repair(eff("paused-intake", run_state_pause=True)))
        self.assertFalse(ss.vetoes_repair(eff("paused-intake")))
        self.assertFalse(ss.vetoes_repair(eff(oom=True)))  # note section 6: unchanged
        self.assertFalse(ss.vetoes_repair(eff()))
        self.assertFalse(ss.vetoes_repair(None))

    def test_remediation_vetoed_true_while_paused_and_halted_false_after_restore(self):
        kw = {"now": NOW, "state_root": self.root, "maintenance_registry": self.maint,
              "oom_holds_path": self.oom}
        with unittest.mock.patch("tools.seat_state.getpass.getuser", return_value="claude-research"):
            self.assertFalse(ss.remediation_vetoed("specialist", **kw))   # before the pause
            self.put(u5_record())
            self.assertTrue(ss.remediation_vetoed("specialist", **kw))    # paused
            self.put(u5_halted())
            self.assertTrue(ss.remediation_vetoed("specialist", **kw))    # halted: record kept (r1)
            hs.seat_state_path("specialist", root=self.root).unlink()     # restore clears it LAST (p)
            self.assertFalse(ss.remediation_vetoed("specialist", **kw))   # restored

    def test_cross_account_read_carries_the_veto(self):
        run = TestEffectiveFor._runner(self, out=json.dumps(u5_record()))
        with unittest.mock.patch("tools.seat_state.getpass.getuser", return_value="recovery"):
            e = ss.effective_for("specialist", now=NOW, state_root=self.root, maintenance_registry=self.maint,
                                 oom_holds_path=self.oom, runner=run)
        self.assertEqual(run.calls[0][0][2:4], ["-u", "claude-research"])
        self.assertTrue(ss.vetoes_repair(e))
        self.assertFalse(e.remediation_vetoed)

    def test_repair_only_veto_states_slot_for_t5868(self):
        self.assertEqual(ss.REPAIR_ONLY_VETO_STATES, ("halted",))  # T-5868 filled the slot
        doc = record()
        with unittest.mock.patch.object(ss, "REPAIR_ONLY_VETO_STATES", ("paused-intake",)):
            c = ss.parse_record(doc, now=NOW)
        self.assertTrue(c.repair_vetoed)
        self.assertFalse(c.remediation_vetoed)

    def test_text_output_names_repair_vetoed(self):
        self.put(u5_record())
        self.assertIn("repair_vetoed", ss._format_text("specialist", self.eff()))


def _fake(deep=False, paused=False, maint=False):
    from tools.seat_state_testkit import eff
    if deep:
        return eff("deep-sleep", asleep=True)
    if paused:
        return eff("paused-intake")
    return eff(maintenance=maint)


if __name__ == "__main__":
    unittest.main()
