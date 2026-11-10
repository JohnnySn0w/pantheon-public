"""Hermetic tests for tools/parity_audit_runner.py (T-3750).

NON-HERMETIC RISK AVOIDED (per feedback_test_with_real_pane_probe_is_nonhermetic):
these tests NEVER run the real fleet audit — no parity_render.Runner, no sudo, no
tmux, no mailbox, no ticket.py. They construct synthetic parity_render.SeatReport
objects and drive the PURE classify/decide layer, and inject fake sinks for the
side-effecting layer. The one filesystem test uses a tmpdir.
"""
import json
import sys
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parity_render as pr  # noqa: E402
import parity_audit_runner as par  # noqa: E402

UTC = timezone.utc


def _outcome(seat, axis, tier, verdict):
    return pr.CheckOutcome(axis, seat, verdict, "2026-09-16T00:00:00+00:00", "ev", tier)


def _report(seat, cells):
    """cells: list of (axis, tier, verdict). State via parity_render's own aggregation."""
    outcomes = tuple(_outcome(seat, a, t, v) for (a, t, v) in cells)
    state = pr.aggregate_seat_state(list(outcomes))
    return pr.SeatReport(seat, outcomes, state)


def _full_seat(seat):
    return _report(seat, [("unix-account-linger-enabled", "HARD", "PASS"),
                          ("monitor-context-fill", "SOFT", "PASS")])


class TestClassify(unittest.TestCase):
    def test_full_fleet_is_clean(self):
        f = par.classify([_full_seat("recovery"), _full_seat("orchestrator")])
        self.assertEqual(f.hard_fails, ())
        self.assertEqual(f.carve_outs_broken, ())
        self.assertEqual(f.partial_seats, ())
        self.assertEqual(f.unmeasurable_cells, ())
        self.assertEqual(f.seat_state, {"recovery": "FULL", "orchestrator": "FULL"})

    def test_hard_fail_detected_and_seat_broken(self):
        f = par.classify([_report("artisan", [("harness-cli-installed-authed", "HARD", "FAIL")])])
        self.assertEqual(f.hard_fails, (("artisan", "harness-cli-installed-authed", "FAIL"),))
        self.assertEqual(f.seat_state["artisan"], "BROKEN")
        self.assertEqual(f.partial_seats, ())

    def test_billing_and_domain_fail_tiers_are_hard(self):
        # A BILLING/DOMAIN FAIL is a hard fail; a DOMAIN UNMEASURABLE is NOT — it goes
        # to the unmeasurable path even though it still makes the seat BROKEN.
        f = par.classify([_report("mason", [("billing-model-declared", "BILLING", "FAIL"),
                                             ("domain-scope-declared", "DOMAIN", "UNMEASURABLE")])])
        self.assertEqual(f.seat_state["mason"], "BROKEN")
        self.assertEqual(f.hard_fails, (("mason", "billing-model-declared", "FAIL"),))
        self.assertIn(("mason", "domain-scope-declared"), f.unmeasurable_cells)
        self.assertEqual(f.fail_broken_seats, ("mason",))

    def test_soft_fail_is_partial_not_hard(self):
        f = par.classify([_report("archivist", [("unix-account-linger-enabled", "HARD", "PASS"),
                                                ("monitor-activity", "SOFT", "FAIL")])])
        self.assertEqual(f.partial_seats, ("archivist",))
        self.assertEqual(f.hard_fails, ())
        self.assertEqual(f.seat_state["archivist"], "PARTIAL")

    def test_carve_out_broken_flagged(self):
        f = par.classify([_report("specialist", [("hc-controller-service", "SOFT", "CARVE_OUT_BROKEN")])])
        self.assertEqual(f.carve_outs_broken, (("specialist", "hc-controller-service"),))

    def test_unmeasurable_hard_axis_is_not_a_hard_fail(self):
        # THE CONFLATION-BUG REGRESSION GUARD (T-3750): a HARD axis reading UNMEASURABLE
        # makes parity_render aggregate the seat BROKEN, but it must NOT be treated as a
        # HARD FAIL (that would page orchestrator urgently on any unmeasurable axis — the exact
        # cause of the 8/8-BROKEN first-run false fire). It belongs to unmeasurable_cells.
        f = par.classify([_report("recovery", [("mailbox-can-send-to-peers", "HARD", "UNMEASURABLE")])])
        self.assertEqual(f.unmeasurable_cells, (("recovery", "mailbox-can-send-to-peers"),))
        self.assertEqual(f.seat_state["recovery"], "BROKEN")   # parity_render still aggregates BROKEN
        self.assertEqual(f.hard_fails, ())                 # ...but it is NOT a hard fail
        self.assertEqual(f.fail_broken_seats, ())          # ...and not a FAIL-driven break

    def test_fail_broken_vs_unmeasurable_broken_seats(self):
        f = par.classify([
            _report("codex", [("permissions-allow-populated", "HARD", "FAIL")]),
            _report("archivist", [("delivery-mode-substrate-match", "HARD", "UNMEASURABLE")]),
        ])
        self.assertEqual(f.seat_state, {"codex": "BROKEN", "archivist": "BROKEN"})
        self.assertEqual(f.fail_broken_seats, ("codex",))  # only codex FAILed


class TestSignature(unittest.TestCase):
    def test_empty_is_blank(self):
        self.assertEqual(par._signature([]), "")

    def test_stable_and_order_independent(self):
        a = par._signature([("x", "y"), ("a", "b")])
        b = par._signature([("a", "b"), ("x", "y")])
        self.assertEqual(a, b)
        self.assertNotEqual(a, "")

    def test_differs_on_content(self):
        self.assertNotEqual(par._signature([("a", "b")]), par._signature([("a", "c")]))


class TestDecideHardFail(unittest.TestCase):
    def _findings(self, hard):
        return par.Findings(hard_fails=tuple(hard), carve_outs_broken=(), partial_seats=(),
                            all_seats=("artisan",), unmeasurable_cells=(), seat_state={"artisan": "BROKEN"})

    def test_new_hard_fail_alerts_urgent(self):
        f = self._findings([("artisan", "harness-cli-installed-authed", "FAIL")])
        actions, state = par.decide(f, par._blank_state(), datetime(2026, 9, 16, tzinfo=UTC))
        mailboxes = [a for a in actions if a.kind == "mailbox"]
        self.assertEqual(len(mailboxes), 1)
        self.assertEqual(mailboxes[0].severity, "urgent")
        self.assertIn("HARD FAIL", mailboxes[0].subject)
        self.assertNotEqual(state["alerted_hard"], "")

    def test_same_hard_fail_deduped(self):
        f = self._findings([("artisan", "harness-cli-installed-authed", "FAIL")])
        now = datetime(2026, 9, 16, tzinfo=UTC)
        _, state1 = par.decide(f, par._blank_state(), now)
        actions2, _ = par.decide(f, state1, now)
        self.assertEqual([a for a in actions2 if a.kind == "mailbox"], [])

    def test_changed_hard_fail_set_realerts(self):
        now = datetime(2026, 9, 16, tzinfo=UTC)
        f1 = self._findings([("artisan", "axisA", "FAIL")])
        _, state1 = par.decide(f1, par._blank_state(), now)
        f2 = self._findings([("artisan", "axisA", "FAIL"), ("artisan", "axisB", "FAIL")])
        actions2, _ = par.decide(f2, state1, now)
        self.assertEqual(len([a for a in actions2 if a.kind == "mailbox"]), 1)

    def test_clear_then_reappear_realerts(self):
        now = datetime(2026, 9, 16, tzinfo=UTC)
        f_bad = self._findings([("artisan", "axisA", "FAIL")])
        f_clear = par.Findings((), (), (), ("artisan",), (), {"artisan": "FULL"})
        _, s1 = par.decide(f_bad, par._blank_state(), now)
        _, s2 = par.decide(f_clear, s1, now)
        self.assertEqual(s2["alerted_hard"], "")
        actions3, _ = par.decide(f_bad, s2, now)
        self.assertEqual(len([a for a in actions3 if a.kind == "mailbox"]), 1)


class TestDecideCarveOut(unittest.TestCase):
    def test_carve_out_broken_alerts_and_dedupes(self):
        now = datetime(2026, 9, 16, tzinfo=UTC)
        f = par.Findings((), (("specialist", "hc-controller-service"),), (), ("specialist",), (),
                         {"specialist": "PARTIAL"})
        actions, state = par.decide(f, par._blank_state(), now)
        carve = [a for a in actions if "CARVE_OUT_BROKEN" in a.subject]
        self.assertEqual(len(carve), 1)
        actions2, _ = par.decide(f, state, now)
        self.assertEqual([a for a in actions2 if "CARVE_OUT_BROKEN" in a.subject], [])


class TestDecideSoftFail(unittest.TestCase):
    def _partial(self, seat="archivist"):
        return par.Findings((), (), (seat,), (seat,), (), {seat: "PARTIAL"})

    def test_first_seen_recorded_no_ticket_on_fresh(self):
        now = datetime(2026, 9, 16, tzinfo=UTC)
        actions, state = par.decide(self._partial(), par._blank_state(), now)
        self.assertEqual([a for a in actions if a.kind == "ticket"], [])
        self.assertIn("archivist", state["soft_fail_first_seen"])

    def test_rebaseline_ticket_after_horizon(self):
        first = datetime(2026, 8, 1, tzinfo=UTC)
        now = first + timedelta(days=31)
        prior = par._blank_state()
        prior["soft_fail_first_seen"] = {"archivist": par._iso(first)}
        actions, state = par.decide(self._partial(), prior, now)
        tickets = [a for a in actions if a.kind == "ticket"]
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].subject, "archivist")
        self.assertIn("archivist", state["rebaseline_filed"])

    def test_rebaseline_deduped_once_filed(self):
        first = datetime(2026, 8, 1, tzinfo=UTC)
        now = first + timedelta(days=40)
        prior = par._blank_state()
        prior["soft_fail_first_seen"] = {"archivist": par._iso(first)}
        prior["rebaseline_filed"] = {"archivist": par._iso(first + timedelta(days=31))}
        actions, _ = par.decide(self._partial(), prior, now)
        self.assertEqual([a for a in actions if a.kind == "ticket"], [])

    def test_recovery_clears_tracking(self):
        first = datetime(2026, 8, 1, tzinfo=UTC)
        now = first + timedelta(days=40)
        prior = par._blank_state()
        prior["soft_fail_first_seen"] = {"archivist": par._iso(first)}
        prior["rebaseline_filed"] = {"archivist": par._iso(first)}
        recovered = par.Findings((), (), (), ("archivist",), (), {"archivist": "FULL"})
        _, state = par.decide(recovered, prior, now)
        self.assertNotIn("archivist", state["soft_fail_first_seen"])
        self.assertNotIn("archivist", state["rebaseline_filed"])


class TestDecideUnmeasurable(unittest.TestCase):
    def _un(self, cells, seats=("recovery",)):
        return par.Findings((), (), (), tuple(seats), tuple(cells),
                            {s: "FULL" for s in seats})

    def test_streak_builds_and_escalates_at_n(self):
        now = datetime(2026, 9, 16, tzinfo=UTC)
        state = par._blank_state()
        cell = [("recovery", "mailbox-can-send-to-peers")]
        actions = None
        for i in range(par.UNMEASURABLE_ESCALATE_RUNS):
            actions, state = par.decide(self._un(cell), state, now)
            if i < par.UNMEASURABLE_ESCALATE_RUNS - 1:
                self.assertEqual([a for a in actions if "UNMEASURABLE" in a.subject], [],
                                 f"escalated too early at run {i+1}")
        self.assertEqual(len([a for a in actions if "UNMEASURABLE" in a.subject]), 1)

    def test_escalation_deduped_after_alert(self):
        now = datetime(2026, 9, 16, tzinfo=UTC)
        state = par._blank_state()
        cell = [("recovery", "axisX")]
        for _ in range(par.UNMEASURABLE_ESCALATE_RUNS):
            _, state = par.decide(self._un(cell), state, now)
        actions, _ = par.decide(self._un(cell), state, now)  # N+1
        self.assertEqual([a for a in actions if "UNMEASURABLE" in a.subject], [])

    def test_measurable_again_resets_streak(self):
        now = datetime(2026, 9, 16, tzinfo=UTC)
        state = par._blank_state()
        cell = [("recovery", "axisX")]
        for _ in range(2):
            _, state = par.decide(self._un(cell), state, now)
        # cell measurable this run
        _, state = par.decide(self._un([]), state, now)
        self.assertNotIn("recovery\x1faxisX", state["unmeasurable_streak"])


class TestDecidePurity(unittest.TestCase):
    def test_prior_state_not_mutated(self):
        now = datetime(2026, 9, 16, tzinfo=UTC)
        prior = par._blank_state()
        prior["soft_fail_first_seen"] = {"seatA": par._iso(now)}
        snapshot = json.dumps(prior, sort_keys=True)
        f = par.Findings((), (), ("seatB",), ("seatB",), (), {"seatB": "PARTIAL"})
        par.decide(f, prior, now)
        self.assertEqual(json.dumps(prior, sort_keys=True), snapshot,
                         "decide() mutated the prior state in place")


class TestExecuteActions(unittest.TestCase):
    def test_routes_to_correct_sinks(self):
        mails, tickets = [], []
        sinks = par.Sinks(
            mailbox=lambda s, b, sev: mails.append((s, sev)),
            file_ticket=lambda seat, b: tickets.append(seat),
        )
        actions = [par.Action("mailbox", "subj", "body", "urgent"),
                   par.Action("ticket", "archivist", "## Ask\na\n## Context\nb")]
        failures = par.execute_actions(actions, sinks)
        self.assertEqual(failures, [])
        self.assertEqual(mails, [("subj", "urgent")])
        self.assertEqual(tickets, ["archivist"])

    def test_fail_open_one_sink_raises(self):
        fired = []

        def boom(*a):
            raise RuntimeError("mailbox down")

        sinks = par.Sinks(mailbox=boom, file_ticket=lambda seat, b: fired.append(seat))
        actions = [par.Action("mailbox", "s", "b"), par.Action("ticket", "seatZ", "body")]
        failures = par.execute_actions(actions, sinks)
        self.assertEqual(len(failures), 1)
        self.assertEqual(fired, ["seatZ"], "a raising mailbox sink swallowed the ticket action")


class TestHelpers(unittest.TestCase):
    def test_status_line_counts_and_tails(self):
        # c is BROKEN with a real FAIL; d is BROKEN only because unmeasurable.
        f = par.Findings((("c", "ax", "FAIL"),), (), (), ("a", "b", "c", "d"), (),
                         {"a": "FULL", "b": "PARTIAL", "c": "BROKEN", "d": "BROKEN"},
                         fail_broken_seats=("c",))
        line = par._status_line(f, datetime(2026, 9, 16, 18, 0, tzinfo=UTC))
        self.assertIn("1 FULL, 1 PARTIAL, 2 BROKEN", line)
        self.assertIn("BROKEN-FAIL=c", line)
        self.assertIn("BROKEN-UNMEASURABLE=d", line)
        self.assertIn("PARTIAL=b", line)

    def test_split_ask_context(self):
        ask, ctx = par._split_ask_context("preamble\n## Ask\nthe ask\n## Context\nthe context")
        self.assertEqual(ask, "the ask")
        self.assertEqual(ctx, "the context")

    def test_split_ask_context_no_headings(self):
        ask, ctx = par._split_ask_context("just a body")
        self.assertEqual(ask, "just a body")
        self.assertEqual(ctx, "")


class TestSeedBaseline(unittest.TestCase):
    def test_seeded_baseline_suppresses_current_findings(self):
        now = datetime(2026, 9, 16, tzinfo=UTC)
        f = par.Findings(
            hard_fails=(("codex", "permissions-allow-populated", "FAIL"),),
            carve_outs_broken=(("specialist", "hc-controller-service"),),
            partial_seats=("archivist",),
            all_seats=("codex", "specialist", "archivist"),
            unmeasurable_cells=(("recovery", "delivery-mode-substrate-match"),),
            seat_state={"codex": "BROKEN", "specialist": "PARTIAL", "archivist": "PARTIAL"},
            fail_broken_seats=("codex",),
        )
        seeded = par.seed_baseline_state(f, now)
        # re-running decide() on the SAME findings against the seeded state = no escalation
        actions, _ = par.decide(f, seeded, now)
        self.assertEqual(actions, [], "seeded baseline should suppress the current result")

    def test_seeded_baseline_still_alerts_on_new_drift(self):
        now = datetime(2026, 9, 16, tzinfo=UTC)
        base = par.Findings((("codex", "axA", "FAIL"),), (), (), ("codex",), (),
                            {"codex": "BROKEN"}, fail_broken_seats=("codex",))
        seeded = par.seed_baseline_state(base, now)
        # a NEW hard fail appears on another seat
        drift = par.Findings((("codex", "axA", "FAIL"), ("mason", "axB", "FAIL")), (), (),
                             ("codex", "mason"), (), {"codex": "BROKEN", "mason": "BROKEN"},
                             fail_broken_seats=("codex", "mason"))
        actions, _ = par.decide(drift, seeded, now)
        self.assertEqual(len([a for a in actions if a.kind == "mailbox"]), 1)


class TestReportPersistence(unittest.TestCase):
    def test_write_reports_and_status(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            rd = Path(d)
            reports = [_full_seat("recovery"), _report("artisan", [("x", "HARD", "FAIL")])]
            axes = pr.load_axes()  # read-only manifest load; no probing
            f = par.classify(reports)
            now = datetime(2026, 9, 16, 18, 0, tzinfo=UTC)
            md_path = par.write_reports(rd, reports, axes, f, now)
            self.assertTrue(md_path.exists())
            self.assertTrue((rd / "2026-09-16-18.json").exists())
            status = (rd / par.LATEST_STATUS_BASENAME).read_text()
            self.assertIn("BROKEN-FAIL=artisan", status)

    def test_prune_keeps_most_recent(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            rd = Path(d)
            for i in range(par.REPORT_RETENTION + 5):
                (rd / f"2026-01-{i:02d}-00.md").write_text("x")
                (rd / f"2026-01-{i:02d}-00.json").write_text("{}")
            par._prune_reports(rd)
            remaining = sorted(rd.glob("*.md"))
            self.assertEqual(len(remaining), par.REPORT_RETENTION)
            # newest kept, oldest pruned
            self.assertTrue((rd / f"2026-01-{par.REPORT_RETENTION + 4:02d}-00.md").exists())
            self.assertFalse((rd / "2026-01-00-00.md").exists())

    def test_state_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            self.assertEqual(par.load_state(p), par._blank_state())  # absent -> blank
            st = par._blank_state()
            st["alerted_hard"] = "abc123"
            par.save_state(p, st)
            self.assertEqual(par.load_state(p)["alerted_hard"], "abc123")

    def test_corrupt_state_falls_back_to_blank(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            p.write_text("{not json")
            self.assertEqual(par.load_state(p), par._blank_state())


class TestMaintenanceHold(unittest.TestCase):
    """T-5254: held seats are excluded from findings BEFORE decide(), so nothing pages
    and no dedupe stamp is written for them."""

    def _reports(self):
        return [_report("mason", [("harness-cli-installed-authed", "HARD", "FAIL"),
                                  ("carve", "SOFT", "CARVE_OUT_BROKEN"),
                                  ("u", "SOFT", "UNMEASURABLE")]),
                _full_seat("recovery")]

    def _held(self, *held):
        return lambda seat: seat in held

    def test_held_seat_excluded_from_all_alert_sets(self):
        f = par.exclude_held(par.classify(self._reports()), self._held("mason"))
        self.assertEqual((f.hard_fails, f.carve_outs_broken, f.unmeasurable_cells,
                          f.partial_seats, f.fail_broken_seats), ((), (), (), (), ()))
        # table-facing fields stay honest
        self.assertEqual(f.seat_state["mason"], "BROKEN")
        self.assertIn("mason", f.all_seats)
        actions, _ = par.decide(f, par._blank_state(), datetime(2026, 10, 20, tzinfo=UTC))
        self.assertEqual(actions, [])

    def test_not_held_unchanged(self):
        f = par.classify(self._reports())
        self.assertEqual(par.exclude_held(f, self._held()), f)

    def test_fault_fails_open(self):
        f = par.classify(self._reports())
        with unittest.mock.patch("tools.notify_policy.in_maintenance", side_effect=OSError("x")):
            self.assertEqual(par.held_seats(f), ())
            self.assertEqual(par.exclude_held(f), f)

    def test_fault_is_silent_as_before(self):
        # T-5585 pin: the old private wrapper logged nothing on a registry fault; the
        # shared seat_state.maintenance_active (no on_fault) must stay silent too.
        import contextlib
        import io
        f = par.classify(self._reports())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                unittest.mock.patch("tools.notify_policy.in_maintenance", side_effect=OSError("x")):
            par.held_seats(f)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))

    def test_seat_names_are_notify_policy_personas(self):
        # SeatReport.seat is SeatBinding.persona — the same key space as notify_policy.
        f = par.classify(self._reports())
        with unittest.mock.patch("tools.notify_policy.in_maintenance",
                                 side_effect=lambda a, *x, **k: a == "mason") as m:
            self.assertEqual(par.held_seats(f), ("mason",))
        self.assertEqual({c.args[0] for c in m.call_args_list}, {"mason", "recovery"})

    def test_default_check_is_the_shared_seat_state_helper(self):
        f = par.classify(self._reports())
        with unittest.mock.patch("tools.seat_state.maintenance_active",
                                 side_effect=lambda p: p == "recovery") as m:
            self.assertEqual(par.held_seats(f), ("recovery",))
        self.assertEqual({c.args[0] for c in m.call_args_list}, {"mason", "recovery"})

    def test_injected_seam_overrides_default(self):
        f = par.classify(self._reports())
        with unittest.mock.patch("tools.seat_state.maintenance_active") as m:
            self.assertEqual(par.held_seats(f, self._held("recovery")), ("recovery",))
        m.assert_not_called()

    def test_deep_sleep_and_halted_alone_are_not_held(self):
        # T-5585 pin: the hold is maintenance-only (observation-side). A deep-sleep or
        # halted seat is NOT held here (no maintenance entry) and no seat-state read happens.
        f = par.classify(self._reports())
        for state in ("deep-sleep", "halted", "paused"):
            eff = unittest.mock.Mock(state=state, remediation_vetoed=True)
            with self.subTest(state=state), \
                    unittest.mock.patch("tools.notify_policy.in_maintenance", return_value=False), \
                    unittest.mock.patch("tools.seat_state.safe_effective_for",
                                        return_value=eff) as m:
                self.assertEqual(par.held_seats(f), ())
                m.assert_not_called()

    def test_first_post_hold_alert_not_swallowed(self):
        now = datetime(2026, 10, 20, tzinfo=UTC)
        state = par._blank_state()
        for _ in range(2):  # held runs
            f = par.exclude_held(par.classify(self._reports()), self._held("mason"))
            actions, state = par.decide(f, state, now)
            self.assertEqual(actions, [])
        self.assertEqual(state["alerted_hard"], "")
        # hold expires: same failure now pages, urgently
        actions, state = par.decide(
            par.exclude_held(par.classify(self._reports()), self._held()), state, now)
        hard = [a for a in actions if "HARD FAIL" in a.subject]
        self.assertEqual(len(hard), 1)
        self.assertEqual(hard[0].severity, "urgent")

    def test_status_line_still_shows_real_state_and_held_tail(self):
        f = par.classify(self._reports())
        now = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
        line = par._status_line(f, now, held=("mason",))
        self.assertIn("BROKEN-FAIL=mason", line)
        self.assertTrue(line.endswith(" held=mason"))
        self.assertNotIn("held=", par._status_line(f, now))


if __name__ == "__main__":
    unittest.main()
