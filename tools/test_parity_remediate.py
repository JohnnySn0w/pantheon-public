#!/usr/bin/env python3
"""Unittest coverage for tools/parity_remediate.py (T-3798) — Recovery's bounded
seat-parity self-repair. pytest is absent on the reference host; unittest only.

Locks the two properties that make auto-remediation SAFE:
  1. plan_repairs only ever selects a genuine FAIL on a SAFE axis — never a
     carve-out (intentional exemption), never an UNMEASURABLE probe, never a
     non-mechanical axis (those stay the audit's orchestrator escalation).
  2. apply_repair enables THEN re-verifies: an enable that returns success but
     does not actually bring the timer up is reported not-ok (-> escalation),
     not silently treated as fixed.
"""
import getpass
import subprocess
import sys
import unittest
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import parity_render as pr
import parity_remediate as prem

AX = "inbox-consumer-timer-present"


def _outcome(seat, axis, tier, verdict):
    return pr.CheckOutcome(axis, seat, verdict, "2026-09-16T00:00:00+00:00", "ev", tier)


def _report(seat, cells):
    outcomes = tuple(_outcome(seat, a, t, v) for (a, t, v) in cells)
    state = pr.aggregate_seat_state(list(outcomes))
    return pr.SeatReport(seat, outcomes, state)


class TestPlanRepairs(unittest.TestCase):
    def test_fail_on_safe_axis_is_planned(self):
        plan = prem.plan_repairs([_report("archivist", [(AX, "HARD", "FAIL")])])
        self.assertEqual([(r.seat, r.axis_id) for r in plan], [("archivist", AX)])
        self.assertEqual(plan[0].unit_template, "pantheon-inbox-consumer@{persona}.timer")

    def test_pass_is_not_planned(self):
        self.assertEqual(prem.plan_repairs([_report("artisan", [(AX, "HARD", "PASS")])]), [])

    def test_carve_out_is_never_repaired(self):
        # An intentional exemption reads CARVE_OUT_* — never "FAIL" — so a seat
        # deliberately mid-bringup or exempt is never auto-"fixed".
        for v in ("CARVE_OUT_VERIFIED", "CARVE_OUT_BROKEN"):
            self.assertEqual(prem.plan_repairs([_report("newseat", [(AX, "HARD", v)])]), [],
                             f"{v} must not be repaired")

    def test_unmeasurable_is_not_repaired(self):
        self.assertEqual(prem.plan_repairs([_report("seat", [(AX, "HARD", "UNMEASURABLE")])]), [])

    def test_fail_on_unsafe_axis_is_left_to_orchestrator(self):
        rep = [_report("seat", [("permissions-allow-populated", "HARD", "FAIL")])]
        self.assertEqual(prem.plan_repairs(rep), [])

    def test_only_the_failing_safe_cells_across_a_fleet(self):
        reports = [
            _report("archivist", [(AX, "HARD", "FAIL")]),
            _report("artisan", [(AX, "HARD", "PASS")]),
            _report("mason", [(AX, "HARD", "FAIL"),
                              ("harness-cli-installed-authed", "HARD", "FAIL")]),
        ]
        self.assertEqual(sorted((r.seat, r.axis_id) for r in prem.plan_repairs(reports)),
                         [("mason", AX), ("archivist", AX)])


class _FakeRun:
    """Scripts systemctl responses by sub-verb; records the argv of every call."""
    def __init__(self, enable_rc=0, is_enabled="enabled", is_active="active"):
        self.enable_rc, self.is_enabled, self.is_active = enable_rc, is_enabled, is_active
        self.calls = []

    def __call__(self, cmd, capture_output=True, text=True):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        if "enable --now" in joined:
            return subprocess.CompletedProcess(cmd, self.enable_rc, "",
                                               "boom" if self.enable_rc else "")
        if "is-enabled" in joined:
            return subprocess.CompletedProcess(cmd, 0, self.is_enabled + "\n", "")
        if "is-active" in joined:
            return subprocess.CompletedProcess(cmd, 0, self.is_active + "\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


_R = prem.Repair("archivist", AX, "pantheon-inbox-consumer@{persona}.timer")


class TestApplyRepair(unittest.TestCase):
    def test_success_enables_then_verifies(self):
        run = _FakeRun(enable_rc=0, is_enabled="enabled", is_active="active")
        res = prem.apply_repair(_R, "exocortex", run=run)
        self.assertTrue(res.ok)
        first = " ".join(run.calls[0])
        self.assertIn("sudo -u exocortex", first)                     # crossed the boundary
        self.assertIn("pantheon-inbox-consumer@archivist.timer", first)  # persona-keyed unit
        self.assertIn("enable --now", first)

    def test_enable_failure_is_not_ok(self):
        res = prem.apply_repair(_R, "exocortex", run=_FakeRun(enable_rc=1))
        self.assertFalse(res.applied)
        self.assertFalse(res.ok)

    def test_enable_ok_but_still_down_is_not_verified(self):
        # enable returned 0 yet the timer did not come up -> not ok -> escalates.
        run = _FakeRun(enable_rc=0, is_enabled="disabled", is_active="inactive")
        res = prem.apply_repair(_R, "exocortex", run=run)
        self.assertTrue(res.applied)
        self.assertFalse(res.verified)
        self.assertFalse(res.ok)

    def test_own_account_runs_without_sudo(self):
        run = _FakeRun()
        res = prem.apply_repair(prem.Repair("recovery", AX, _R.unit_template),
                                getpass.getuser(), run=run)
        self.assertTrue(res.ok)
        self.assertNotIn("sudo", " ".join(run.calls[0]))


class TestMainHonoursSeatMaintenance(unittest.TestCase):
    """T-5222: a seat held down on purpose (seat-maintenance armed) must not have
    its consumer timer re-enabled behind the operator's back."""

    def _run_main(self, held):
        from types import SimpleNamespace
        from unittest import mock
        reports = [_report("specialist", [(AX, "HARD", "FAIL")]),
                   _report("mason", [(AX, "HARD", "FAIL")])]
        seats = [SimpleNamespace(persona="specialist", account="claude-research"),
                 SimpleNamespace(persona="mason", account="suborch1")]
        applied = []

        def fake_apply(repair, account, run=None):
            applied.append(repair.seat)
            return prem.RepairResult(seat=repair.seat, axis_id=repair.axis_id,
                                     unit="u", applied=True, verified=True,
                                     detail="ok")

        with mock.patch.object(prem.par, "run_fleet_audit", return_value=(reports, None)), \
             mock.patch.object(prem.pr, "load_seats", return_value=seats), \
             mock.patch.object(prem, "apply_repair", side_effect=fake_apply), \
             mock.patch.object(prem, "_seat_in_maintenance", side_effect=lambda s: s in held), \
             mock.patch.object(prem, "_log"):
            self.assertEqual(prem.main([]), 0)
        return applied

    def test_halted_seat_is_vetoed_with_the_halted_reason_T5868(self):
        from types import SimpleNamespace
        from unittest import mock
        from tools.seat_state_testkit import eff
        reports = [_report("specialist", [(AX, "HARD", "FAIL")]), _report("mason", [(AX, "HARD", "FAIL")])]
        seats = [SimpleNamespace(persona="specialist", account="claude-research"),
                 SimpleNamespace(persona="mason", account="suborch1")]
        applied, logs = [], []
        res = prem.RepairResult(seat="mason", axis_id=AX, unit="u", applied=True, verified=True, detail="ok")
        with mock.patch.object(prem.par, "run_fleet_audit", return_value=(reports, None)), \
             mock.patch.object(prem.pr, "load_seats", return_value=seats), \
             mock.patch.object(prem, "apply_repair", side_effect=lambda r, a, run=None: applied.append(r.seat) or res), \
             mock.patch.object(prem, "_seat_in_maintenance", return_value=False), \
             mock.patch("tools.seat_state.safe_effective_for",
                        side_effect=lambda p, **kw: eff("halted") if p == "specialist" else None), \
             mock.patch.object(prem, "_log", side_effect=logs.append):
            self.assertEqual(prem.main([]), 0)
        self.assertEqual(applied, ["mason"])
        self.assertTrue(any("specialist" in l and "HALTED mid-pause" in l and "vetoes repair" in l for l in logs), logs)

    def test_held_seat_is_skipped_others_repaired(self):
        self.assertEqual(self._run_main(held={"specialist"}), ["mason"])

    def _run_main_seat(self, vetoed_by_state: set[str], held: set[str] = frozenset()):
        from tools.seat_state_testkit import eff
        from unittest import mock

        def fake(persona, **kw):
            return eff("deep-sleep", asleep=True) if persona in vetoed_by_state else None

        with mock.patch("tools.seat_state.safe_effective_for", side_effect=fake):
            return self._run_main(held=held)

    def test_deep_sleep_seat_is_not_repaired_T5527(self):
        self.assertEqual(self._run_main_seat({"specialist"}), ["mason"])

    def test_live_oom_shape_oom_hold_plus_maintenance_rearm_is_not_repaired_T5527(self):
        self.assertEqual(self._run_main_seat(set(), held={"specialist"}), ["mason"])

    def test_paused_intake_oom_only_and_unknown_are_repaired_T5527(self):
        from tools.seat_state_testkit import eff
        from unittest import mock
        for seat in (eff("paused-intake"), eff(oom=True), None):
            with mock.patch("tools.seat_state.safe_effective_for", return_value=seat):
                self.assertEqual(self._run_main(held=set()), ["specialist", "mason"])

    def test_no_maintenance_repairs_all(self):
        self.assertEqual(self._run_main(held=set()), ["specialist", "mason"])



class TestRunStatePauseVetoesRepairEndToEnd(unittest.TestCase):
    """T-5918: a harness-manager run_state pause (U5) disables the seat's wake timers,
    inbox-consumer included, and records a paused-intake seat-state record carrying
    pause_id. pantheon-parity-remediate must not re-enable them: no repair, and no
    "DRY-RUN would repair" line, for the paused seat, through the REAL seat-state reader
    over a real record on disk; the other seat is still repaired."""

    NOW = "2026-11-02T12:00:00Z"

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "state"

    def tearDown(self):
        self._tmp.cleanup()

    def _record(self, halted=False):
        rec = {
            "schema": "pantheon-seat-state/v1", "persona": "artisan", "state": "paused-intake",
            "phase": "set", "phase_ts": "2026-11-02T11:00:00Z", "set_ts": "2026-11-02T11:00:00Z",
            "set_by": "germinate:harness-manager run_state", "owner": "operator",
            "reason": "fleet bringdown: harness-manager run_state=paused (rs-artisan-20261102T110000Z)",
            "ticket": None, "expires": None, "indefinite": True, "telegram_dark_ack": False,
            "pre_expiry_notified": False, "ledger_entry": None,
            "pause_id": "rs-artisan-20261102T110000Z", "anchor": "operator:TG-12733", "plan_revision": "b" * 40,
        }
        if halted:  # Recovery D15 (T-5858) (r1): the record is kept and annotated
            rec["run_state_halt"] = {"run_id": "r1", "op_id": "rs-drain", "at": "2026-11-02T11:05:00Z",
                                     "rollback": "python3 -m tools.germinate rollback --run r1"}
        return rec

    def _put(self, rec):
        import json
        from tools import harness_state as hs
        path = hs.seat_state_path("artisan", root=self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec))
        return path

    def _main(self, argv):
        from datetime import datetime, timezone
        from types import SimpleNamespace
        from unittest import mock
        from tools import seat_state

        real = seat_state.effective_for
        now = datetime.strptime(self.NOW, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

        def reader(persona, **kw):
            """safe_effective_for over the REAL effective_for (tools/conftest.py stubs the
            safe helper to None for hermeticity); only the roots point at the tmp dir.
            Fail-open exactly like the real helper (any raise reads None)."""
            try:
                return real(persona, now=now, state_root=self.root,
                            maintenance_registry=str(Path(self._tmp.name) / "maint.json"),
                            oom_holds_path=Path(self._tmp.name) / "oom.json")
            except Exception:  # noqa: BLE001
                return None

        reports = [_report("artisan", [(AX, "HARD", "FAIL")]), _report("mason", [(AX, "HARD", "FAIL")])]
        seats = [SimpleNamespace(persona="artisan", account="artisan"),
                 SimpleNamespace(persona="mason", account="suborch1")]
        applied, logs = [], []

        def fake_apply(repair, account, run=None):
            applied.append(repair.seat)
            return prem.RepairResult(seat=repair.seat, axis_id=repair.axis_id, unit="u",
                                     applied=True, verified=True, detail="ok")

        with mock.patch.object(prem.par, "run_fleet_audit", return_value=(reports, None)), \
             mock.patch.object(prem.pr, "load_seats", return_value=seats), \
             mock.patch.object(prem, "apply_repair", side_effect=fake_apply), \
             mock.patch.object(prem, "_seat_in_maintenance", return_value=False), \
             mock.patch.object(seat_state, "safe_effective_for", side_effect=reader), \
             mock.patch("tools.seat_state.getpass.getuser", return_value="artisan"), \
             mock.patch("tools.seat_state.agent_role_map.instance_binding",
                        return_value={"account": "artisan"}), \
             mock.patch.object(prem, "_log", side_effect=logs.append):
            self.assertEqual(prem.main(argv), 0)
        return applied, logs

    def _assert_vetoed(self, applied, logs):
        self.assertNotIn("artisan", applied, logs)
        self.assertFalse([m for m in logs if "would repair artisan" in m], logs)
        self.assertTrue([m for m in logs if m.startswith("SKIP artisan/") and "repair_vetoed" in m], logs)

    def test_dry_run_on_a_paused_seat_makes_no_repair(self):
        self._put(self._record())
        applied, logs = self._main(["--dry-run"])
        self._assert_vetoed(applied, logs)
        self.assertEqual(applied, [])
        self.assertTrue([m for m in logs if "DRY-RUN would repair mason" in m], logs)

    def test_live_pass_on_a_paused_seat_makes_no_repair(self):
        self._put(self._record())
        applied, logs = self._main([])
        self._assert_vetoed(applied, logs)
        self.assertEqual(applied, ["mason"])

    def test_halted_pause_keeps_vetoing(self):
        self._put(self._record(halted=True))
        applied, logs = self._main([])
        self._assert_vetoed(applied, logs)
        self.assertEqual(applied, ["mason"])

    def test_after_restore_the_seat_is_repaired_again(self):
        self._put(self._record()).unlink()  # restore clears the record LAST (Recovery (p))
        applied, logs = self._main(["--dry-run"])
        self.assertTrue([m for m in logs if "DRY-RUN would repair artisan" in m], logs)
        applied, _logs = self._main([])
        self.assertEqual(applied, ["artisan", "mason"])

    def test_an_operator_pause_without_pause_id_is_still_repaired(self):
        rec = self._record()
        del rec["pause_id"]
        self._put(rec)
        applied, _logs = self._main([])
        self.assertEqual(applied, ["artisan", "mason"])

    def test_testkit_run_state_pause_is_skipped(self):
        from tools.seat_state_testkit import eff
        from unittest import mock
        with mock.patch("tools.seat_state.safe_effective_for",
                        side_effect=lambda p, **kw: eff("paused-intake", run_state_pause=True) if p == "specialist" else None):
            self.assertEqual(TestMainHonoursSeatMaintenance._run_main(self, held=set()), ["mason"])


if __name__ == "__main__":
    unittest.main()
