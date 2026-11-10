"""T-5868 Phase A (ORG-106 amendment, notes s10): the HALTED seat state, readers only.

Covers the record rules, the reader contribution, precedence, the janitor (never reaps),
reader behaviour for the consumers that need no bespoke harness, and Recovery's conditions
C1 (readers first: the U5 writer pin and the un-updated-reader simulation) and C2
(is_run_state_pause is a repair-veto input only). Tempdirs and mocks only."""

from __future__ import annotations

import ast
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools import fleet_health as fh
from tools import harness_state as hs
from tools import health_export as he
from tools import seat_state as ss
from tools import seat_state_janitor as sj
from tools import seat_state_testkit as kit

NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parent.parent


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def halted(**over):
    doc = {
        "schema": hs.SCHEMA_SEAT_STATE, "persona": "specialist", "state": "halted", "phase": "halted",
        "phase_ts": iso(NOW - timedelta(hours=2)), "set_ts": iso(NOW - timedelta(hours=3)),
        "set_by": "germinate:harness-manager run_state", "owner": "operator",
        "reason": "fleet bringdown: harness-manager run_state=paused (rs-x)", "ticket": None,
        "expires": None, "indefinite": True, "telegram_dark_ack": False,
        "pre_expiry_notified": False, "ledger_entry": None,
        "run_id": "r1", "op_id": "rs-drain", "halted_at": iso(NOW - timedelta(hours=2)),
        "rollback_cmd": "python3 -m tools.germinate rollback --run r1", "pause_id": "rs-specialist-20261103T090000Z",
    }
    doc.update(over)
    return doc


class TestValidate(unittest.TestCase):
    def test_valid_halted(self):
        self.assertEqual(ss.validate_record(halted()), [])

    def test_halted_in_states_and_phase(self):
        self.assertIn("halted", ss.RECORD_STATES)
        self.assertEqual(ss.PHASES_BY_STATE["halted"], ("halted",))
        self.assertEqual(ss.REPAIR_ONLY_VETO_STATES, ("halted",))

    def test_each_required_key(self):
        for key in ("run_id", "op_id", "pause_id", "rollback_cmd", "halted_at"):
            doc = halted()
            del doc[key]
            self.assertTrue(ss.validate_record(doc), key)

    def test_must_be_indefinite_without_expiry(self):
        self.assertTrue(ss.validate_record(halted(indefinite=False)))
        self.assertTrue(ss.validate_record(halted(expires=iso(NOW + timedelta(hours=1)))))
        self.assertTrue(ss.validate_record(halted(indefinite=False, expires=iso(NOW))))

    def test_rollback_cmd_shape(self):
        self.assertTrue(ss.validate_record(halted(rollback_cmd="rm -rf /")))
        self.assertTrue(ss.validate_record(halted(rollback_cmd="python3 -m tools.germinate rollback --run r1\nx")))
        self.assertTrue(ss.validate_record(halted(halted_at="yesterday")))

    def test_phase_must_be_halted(self):
        self.assertTrue(ss.validate_record(halted(phase="set")))


class TestParse(unittest.TestCase):
    def test_flags(self):
        c = ss.parse_record(halted(), now=NOW)
        self.assertEqual(c.state, "halted")
        self.assertTrue(c.intake_blocked and c.repair_vetoed and c.alertable)
        self.assertFalse(c.launch_blocked or c.remediation_vetoed or c.expected_down)
        self.assertEqual(c.rollback_cmd, "python3 -m tools.germinate rollback --run r1")

    def test_alertable_false_elsewhere(self):
        paused = {**halted(), "state": "paused-intake", "phase": "set"}
        for k in ("run_id", "op_id", "halted_at", "rollback_cmd"):
            paused.pop(k)
        c = ss.parse_record(paused, now=NOW)
        self.assertFalse(c.alertable)
        self.assertIsNone(c.rollback_cmd)
        self.assertFalse(ss.Contribution().alertable)

    def test_never_expires(self):
        far = NOW + timedelta(days=3650)
        self.assertTrue(ss.parse_record(halted(), now=far).active)

    def test_precedence_oom_halted_sleep_maint_paused(self):
        h = ss.parse_record(halted(), now=NOW)
        sleep = ss.Contribution(source=ss.SRC_SEAT_STATE, state="deep-sleep", intake_blocked=True,
                                owner="sleep")
        maint = ss.Contribution(source=ss.SRC_MAINTENANCE, intake_blocked=True, owner="maint")
        oom = ss.Contribution(source=ss.SRC_OOM, intake_blocked=True, launch_blocked=True, owner="oom")
        none = ss.Contribution()
        self.assertEqual(ss._combine(h, maint, oom, none).owner, "oom")
        self.assertEqual(ss._combine(h, maint, none, none).owner, "operator")
        self.assertEqual(ss._combine(sleep, maint, none, none).owner, "sleep")
        e = ss._combine(h, maint, none, none)
        self.assertTrue(e.alertable)
        self.assertEqual(e.rollback_cmd, "python3 -m tools.germinate rollback --run r1")
        self.assertEqual(e.sources[0], ss.SRC_SEAT_STATE)

    def test_alertable_aggregates_with_any(self):
        e = ss._combine(ss.Contribution(), ss.Contribution(source=ss.SRC_MAINTENANCE, intake_blocked=True),
                        ss.Contribution(), ss.Contribution())
        self.assertFalse(e.alertable)

    def test_vetoes_repair_via_repair_only_list(self):
        e = ss._combine(ss.parse_record(halted(), now=NOW), *(ss.Contribution(),) * 3)
        self.assertTrue(ss.vetoes_repair(e))
        self.assertFalse(e.remediation_vetoed)

    def test_format_text_lists_alertable(self):
        e = ss._combine(ss.parse_record(halted(), now=NOW), *(ss.Contribution(),) * 3)
        self.assertIn("alertable", ss._format_text("specialist", e))


class TestC2(unittest.TestCase):
    def test_is_run_state_pause_true_for_halted_as_repair_input(self):
        self.assertTrue(ss.is_run_state_pause(halted()))
        self.assertFalse(ss.is_run_state_pause(halted(pause_id="")))
        self.assertFalse(ss.is_run_state_pause({**halted(), "state": "deep-sleep"}))

    def test_no_other_non_test_caller_C2(self):
        callers = []
        for path in sorted((REPO / "tools").rglob("*.py")):
            if path.name.startswith("test_") or "tests" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if "is_run_state_pause" in line and not line.lstrip().startswith(("#", '"""')):
                    callers.append((path.name, line.strip()))
        names = {c[0] for c in callers}
        self.assertEqual(names, {"seat_state.py"}, callers)
        uses = [l for f, l in callers if "is_run_state_pause(" in l and "def " not in l]
        self.assertEqual(len(uses), 1, uses)
        self.assertIn("repair_vetoed=", uses[0])


class TestJanitor(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.sent = []

    def mail(self, who, subject, payload):
        self.sent.append((who, subject))

    def tick(self, now, **kw):
        return sj.tick("specialist", now=now, state_root=self.root, send_mail=self.mail, **kw)

    def test_never_reaps_or_nags_and_lists_after_an_hour(self):
        hs.write_seat_state("specialist", halted(), root=self.root)
        for delta in (timedelta(hours=2), timedelta(days=30), timedelta(days=3650)):
            self.assertEqual(self.tick(NOW + delta), ["halted_listed"])
        self.assertEqual(self.sent, [])
        self.assertIsNotNone(hs.load_seat_state("specialist", root=self.root))
        self.assertFalse(hs.seat_state_tombstones_path("specialist", root=self.root).exists())
        self.assertFalse(sj.sidecar_path("specialist", root=self.root).exists())

    def test_not_listed_inside_an_hour(self):
        hs.write_seat_state("specialist", halted(halted_at=iso(NOW - timedelta(minutes=10))), root=self.root)
        self.assertEqual(self.tick(NOW), [])

    def test_dry_run_reports_without_acting(self):
        hs.write_seat_state("specialist", halted(), root=self.root)
        self.assertEqual(self.tick(NOW, dry_run=True), ["halted_listed"])
        self.assertIsNotNone(hs.load_seat_state("specialist", root=self.root))


class TestReaders(unittest.TestCase):
    """Halted vs paused-intake vs deep-sleep through the cheap readers."""

    def test_fleet_health_note_is_a_failure_with_rollback(self):
        note = fh._seat_note(kit.eff("halted", rollback_cmd="python3 -m tools.germinate rollback --run r9"))
        self.assertEqual(note["status"], fh.FAIL)
        self.assertIn("python3 -m tools.germinate rollback --run r9", note["detail"])
        self.assertIn("HALTED", note["detail"])

    def test_fleet_health_paused_and_sleep_unchanged(self):
        self.assertEqual(fh._seat_note(kit.eff("paused-intake"))["status"], fh.OK)
        self.assertEqual(fh._seat_note(kit.eff("deep-sleep"))["status"], fh.OK)
        self.assertIsNone(fh._seat_note(kit.eff("active")))

    def test_testkit_halted_flags(self):
        e = kit.eff("halted")
        self.assertTrue(e.intake_blocked and e.alertable and e.repair_vetoed)
        self.assertFalse(e.remediation_vetoed or e.launch_blocked)

    def test_inbox_consumer_holds_intake_for_halted(self):
        from tools import inbox_consumer as ic
        for state, reason in (("halted", "seat_paused"), ("paused-intake", "seat_paused"),
                              ("deep-sleep", "seat_sleep")):
            with self.subTest(state), mock.patch.object(ic._seat_state, "effective_for",
                                                        return_value=kit.eff(state)):
                with self.assertRaises(ic.SkipCycle) as cm:
                    ic.check_seat_intake("specialist")
                self.assertIn(reason, str(cm.exception))

    def test_channel_liveness_logs_incident_and_keeps_judging(self):
        from tools import channel_liveness_monitor as clm
        logs = []
        with mock.patch.object(clm, "probe_bridge", return_value=("up", "ok")), \
                mock.patch.dict(clm.AGENT_CONFIG, {"specialist": {"sudo_user": "x"}}), \
                mock.patch.object(clm.seat_state, "safe_effective_for", return_value=kit.eff("halted")), \
                mock.patch.object(clm, "_log", side_effect=lambda a, m: logs.append(m)), \
                mock.patch.object(clm, "_org076_emit"):
            result = clm.check_agent("specialist", dry_run=True, now=1000)
        self.assertTrue(any("HALTED" in m and "NOT treated as an authorised pause" in m for m in logs), logs)
        self.assertEqual(result, "up")  # judged normally, not "asleep"


class TestDriftCheck(unittest.TestCase):
    def test_halted_is_not_authorised_quiet(self):
        from tools import activity_monitor_drift_check as dc
        with mock.patch.object(dc, "_seat_in_maintenance", return_value=False), \
                mock.patch.object(dc.seat_state, "safe_effective_for", return_value=kit.eff("halted")), \
                mock.patch.object(dc, "_configured_runtimes", return_value=[]):
            self.assertIsNone(dc._persona_quiesced("specialist"))
        for state, want in (("paused-intake", "seat-paused"), ("deep-sleep", "seat-sleep")):
            with mock.patch.object(dc, "_seat_in_maintenance", return_value=False), \
                    mock.patch.object(dc.seat_state, "safe_effective_for", return_value=kit.eff(state)):
                self.assertEqual(dc._persona_quiesced("specialist"), want)


class TestDelivery(unittest.TestCase):
    MODES = {"specialist": "send-keys", "mason": "send-keys"}

    def _probe(self, state):
        from tools import delivery_reachability as dr
        probed = []
        with mock.patch.object(dr, "_seat_in_maintenance", return_value=False), \
                mock.patch("tools.seat_state.safe_effective_for", return_value=kit.eff(state)), \
                mock.patch.object(dr, "probe_agent",
                                  side_effect=lambda a, m, sub, ops: probed.append(a) or
                                  {"agent": a, "mode": m, "state": dr.READY, "detail": ""}):
            out = dr.probe_all(mock.Mock(), substrate={}, modes=self.MODES)
        return out, probed, dr

    def test_halted_is_probed_not_held(self):
        out, probed, dr = self._probe("halted")
        self.assertEqual(sorted(probed), ["specialist", "mason"])
        self.assertNotIn(dr.HELD, {r["state"] for r in out})

    def test_deep_sleep_still_held_differential(self):
        out, probed, dr = self._probe("deep-sleep")
        self.assertEqual(probed, [])
        self.assertEqual({r["state"] for r in out}, {dr.HELD})


class TestContextFill(unittest.TestCase):
    def _gate(self, state):
        from tools import context_fill_monitor as cfm
        with mock.patch.object(cfm, "_seat_in_maintenance", return_value=False), \
                mock.patch("tools.seat_state.safe_effective_for", return_value=kit.eff(state)), \
                mock.patch.object(cfm, "_wedge_suppressed_by_flag", return_value=False), \
                mock.patch.object(cfm, "_pane_in_interactive_modal", return_value=None):
            return cfm._wedge_restart_suppressed("specialist", {})

    def test_halted_does_not_suppress_the_wedge_restart_gate(self):
        # Existing behaviour (review note D): the gate reads remediation_vetoed, not vetoes_repair.
        self.assertIsNone(self._gate("halted"))

    def test_deep_sleep_still_suppresses_differential(self):
        self.assertIn("vetoes remediation", self._gate("deep-sleep"))


class _Reader:
    def __init__(self, text):
        self.text = text

    def read_text_tail(self, account, path, n):
        return self.text


class TestHealthExport(unittest.TestCase):
    def test_halted_is_exported_not_dropped(self):
        self.assertIn("halted", he.SEAT_STATES)
        out = he._seat_state(_Reader(json.dumps(halted())), "acct", "specialist", NOW.timestamp())
        self.assertEqual(out["seat_state"], "halted")
        self.assertFalse(out["expected_down"])
        self.assertTrue(out["seat_indefinite"])

    def test_collector_enum_matches(self):
        from tools import health_collector as hc
        self.assertEqual(hc.SEAT_STATES, he.SEAT_STATES)


class TestParityRemediate(unittest.TestCase):
    def test_halted_vetoes_repair_through_vetoes_repair(self):
        # The real veto path and its reason text: test_parity_remediate (halted_seat_is_vetoed...).
        self.assertTrue(ss.vetoes_repair(kit.eff("halted")))
        self.assertFalse(ss.vetoes_repair(kit.eff("active")))


class TestC1(unittest.TestCase):
    U5_FILES = ("harness_manager_run_state.py", "harness-manager.py", "harness-manager_handlers.py")

    def test_u5_writer_writes_halted_phase_b(self):
        """C1 flipped (T-5868 Phase B, after Recovery's every-clone attestation): the U5 sources now write the
        halted state, through RS.halted_record (the data module) and the handler's on_halted hook."""
        consts = {}
        for name in self.U5_FILES:
            path = REPO / "tools" / "germinate" / "components" / name
            tree = ast.parse(path.read_text(encoding="utf-8"))
            consts[name] = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == "halted"]
        self.assertTrue(consts["harness_manager_run_state.py"], "STATE_HALTED literal lives in the U5 data module")
        handlers = (REPO / "tools" / "germinate" / "components" / "harness-manager_handlers.py").read_text()
        self.assertIn("def on_halted(self, run_id, op, exc)", handlers)
        self.assertIn("RS.halted_record(", handlers)
        self.assertNotIn("run_state_halt", handlers)

    def test_rollback_cmd_parity_with_the_engine_formatter(self):
        """seat_state must not import germinate, so it mirrors the formatter; the two stay equal."""
        from tools.germinate.engine.escalation import rollback_command
        for run_id in ("r1", "01HZZZZZZZZZZZZZZZZZZZZZZZ", "01J0ABCDEFGHJKMNPQRSTVWXYZ", "x-y_z"):
            self.assertEqual(ss.halted_rollback_cmd(run_id), rollback_command(run_id))
        tree = ast.parse((REPO / "tools" / "seat_state.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn("germinate", node.module or "")
                self.assertFalse(any("germinate" in a.name for a in node.names))
            if isinstance(node, ast.Import):
                self.assertFalse(any("germinate" in a.name for a in node.names))

    def test_rollback_cmd_is_exact_for_the_records_own_run_id(self):
        self.assertEqual(ss.validate_record(halted()), [])
        self.assertEqual(ss.validate_record(halted(run_id="r7", rollback_cmd=ss.halted_rollback_cmd("r7"))), [])
        for bad in (ss.halted_rollback_cmd("r2"),                       # another run's command
                    ss.halted_rollback_cmd("r1") + " --force",           # suffix
                    "x " + ss.halted_rollback_cmd("r1"),                 # prefix
                    ss.halted_rollback_cmd("r1") + ";"):
            self.assertTrue(ss.validate_record(halted(rollback_cmd=bad)), bad)

    def test_unupdated_reader_alerts_but_does_not_hold_intake(self):
        """C1 residual, accepted for the rollout window: a reader that predates 'halted'
        faults (alerts) and does NOT block intake, so it would still deliver."""
        with mock.patch.object(ss, "RECORD_STATES", ("paused-intake", "deep-sleep")):
            c = ss.parse_record(halted(), now=NOW)
            e = ss._combine(c, *(ss.Contribution(),) * 3)
        self.assertIsNotNone(c.fault)
        self.assertIn("not in", c.fault)
        self.assertFalse(c.intake_blocked)
        self.assertFalse(e.intake_blocked)
        self.assertIsNotNone(e.fault)
        # the updated reader holds intake for the same record
        self.assertTrue(ss.parse_record(halted(), now=NOW).intake_blocked)


if __name__ == "__main__":
    unittest.main()
