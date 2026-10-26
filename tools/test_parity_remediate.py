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


if __name__ == "__main__":
    unittest.main()
