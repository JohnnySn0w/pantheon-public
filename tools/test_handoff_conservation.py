"""test_handoff_conservation.py — HANDOFF detail-loss detection (tools/handoff_conservation.py, T-2638).

Covers the four fingerprint extractors, the three independent conservation
checks (each must be provably independent — a section-loss-only fixture must
NOT trip the size/entity checks), the "quiet on normal churn" requirement
that motivates the whole design, and the CLI's non-fatal contract (no prior
snapshot / unreadable handoff / unresolvable agent all degrade to exit 0,
never raise), plus the T-5347 open-ticket-loss check (an id still open
per a tmp-built tickets/ tree that vanishes from NEW is listed; done/unknown
ids and a missing tickets root stay quiet).
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chronicle as cy  # noqa: E402
import handoff_conservation as hc  # noqa: E402


# --------------------------------------------------------------- extractors
class FingerprintExtractorsTest(unittest.TestCase):
    def test_ticket_ids(self):
        fp = hc.compute_fingerprint("Working T-1234 and T-99, blocked by T-1234 again.")
        self.assertEqual(fp["ticket_ids"], {"T-1234", "T-99"})

    def test_owner_refs(self):
        text = "target: orchestrator\nRequester: Specialist\nowner:recovery\nunrelated: nope-not-owner-key"
        fp = hc.compute_fingerprint(text)
        self.assertEqual(fp["owner_refs"], {"orchestrator", "specialist", "recovery"})

    def test_headings(self):
        text = "# Title\nprose\n## Blockers\nmore prose\n### Next   actions\n"
        fp = hc.compute_fingerprint(text)
        self.assertEqual(fp["headings"], {"Title", "Blockers", "Next actions"})

    def test_code_blocks_normalized_ignores_whitespace_reformatting(self):
        a = "```bash\n  git status\n\n  git log\n```"
        b = "```bash\ngit status\ngit log\n```"
        fp_a = hc.compute_fingerprint(a)
        fp_b = hc.compute_fingerprint(b)
        self.assertEqual(fp_a["code_blocks"], fp_b["code_blocks"])
        self.assertEqual(fp_a["code_blocks"], {"git status\ngit log"})

    def test_empty_text_yields_empty_fingerprint(self):
        fp = hc.compute_fingerprint("")
        self.assertEqual(fp, {"ticket_ids": set(), "owner_refs": set(),
                               "headings": set(), "code_blocks": set()})


# ------------------------------------------------------- conservation predicate
def _synthetic_old() -> str:
    # Deliberately >500 chars (clears MIN_OLD_CHARS) with 15 distinct
    # entities across all four classes (clears MIN_OLD_ENTITIES) and 4
    # headings (clears MIN_OLD_HEADINGS) -- big enough that every check is
    # actually gated "on" for the fixtures below. owner_refs are on their
    # own line (the regex is anchored to line-start, mirroring this repo's
    # ticket-frontmatter convention -- see the module docstring).
    return (
        "# HANDOFF\n"
        "target: orchestrator\n\n"
        "## Blockers\n"
        "T-1001 waiting on the operator to approve budget before the arc can "
        "proceed further; this has been open a while. T-1002 waiting on "
        "code review from the reviewer persona before it can land.\n\n"
        "## Next actions\n"
        "Pick up T-1003 and T-1004 once the blockers above clear -- these "
        "are next in priority order for the arc once it resumes.\n\n"
        "## Durable pointers\n"
        "```bash\ncat /home/claude/root.env\n```\n"
        "See tools/chronicle.py for the archive format and "
        "tools/handoff_and_restart.sh for the restart pipeline. Also "
        "T-1005 and T-1006 are tracked here, along with T-1007 and T-1008.\n\n"
        "requester: specialist\n"
    )


class ConservationCheckTest(unittest.TestCase):
    def test_normal_churn_stays_quiet(self):
        old = _synthetic_old()
        # Normal cycle: two tickets resolved (T-1001, T-1005 gone), two new
        # ones added, headings persist verbatim, most content carries forward.
        new = (
            "# HANDOFF\n"
            "target: orchestrator\n\n"
            "## Blockers\n"
            "T-1002 still waiting on review. T-1009 new blocker.\n\n"
            "## Next actions\n"
            "Pick up T-1003 and T-1004, then T-1010.\n\n"
            "## Durable pointers\n"
            "```bash\ncat /home/claude/root.env\n```\n"
            "See tools/chronicle.py for the archive format. T-1006, T-1007, "
            "T-1008 still tracked.\n\n"
            "requester: specialist\n"
        )
        self.assertEqual(hc.check_conservation(old, new), [])

    def test_cliff_collapse_fires_size_and_entity_checks(self):
        old = _synthetic_old()
        new = "# HANDOFF\nDone for now.\n"
        alerts = hc.check_conservation(old, new)
        reasons = " | ".join(alerts)
        self.assertIn("size-cliff", reasons)
        self.assertIn("entity-cliff", reasons)

    def test_section_loss_only_fires_independently(self):
        # Old has 4 headings + 15 entities. New KEEPS all 8 ticket ids + both
        # owner refs (11/15 = 73% entity retention -- well above the 35%
        # entity-cliff floor, and plenty of length -- well above the size-
        # cliff floor) but flattens the section structure down to a single
        # heading (1/4 = 25% heading survival -- below the 40% floor). This
        # is the realistic false-negative shape a naive size/entity-only
        # check would miss: content nominally "all still there", but the
        # T-1771 whole-section-loss signature is present. Proves section-loss
        # fires on its own, independent of the other two checks.
        old = _synthetic_old()
        new = (
            "# HANDOFF\n"
            "target: orchestrator\n"
            "requester: specialist\n"
            "T-1001 T-1002 T-1003 T-1004 T-1005 T-1006 T-1007 T-1008 all still "
            "tracked here in one flat paragraph now that the section structure "
            "collapsed away. Padding follows to keep this comfortably longer "
            "than the original. " + ("More filler prose. " * 30)
        )
        alerts = hc.check_conservation(old, new)
        reasons = " | ".join(alerts)
        self.assertIn("section-loss", reasons)
        self.assertNotIn("size-cliff", reasons)
        self.assertNotIn("entity-cliff", reasons)

    def test_heading_parenthetical_churn_stays_quiet(self):
        # T-2666 regression: real HANDOFF headings carry VOLATILE trailing
        # parentheticals (running counts, dates, status) that change every
        # refresh without the section being lost. A legitimate refresh that only
        # updates those suffixes (and advances the H1 title date) must stay quiet
        # -- the stem match (heading minus a trailing "(...)") is what achieves
        # it. Without the fix this fixture reads as 0% section survival and
        # section-loss false-fires (the exact dead-alert the real wet-run hit).
        old = (
            "# HANDOFF -- Orchestrator -- 2026-08-08 ~01:35 UTC (Recovery restart)\n"
            "target: orchestrator\n\n"
            "## RESOLVED THIS SESSION (7 tickets)\n"
            "T-1001 T-1002 T-1003 landed; T-1004 T-1005 stale-closed. Each is "
            "ledgered with verification notes and PR numbers for the record.\n\n"
            "## OPEN TICKETS (awaiting the operator)\n"
            "T-1006 and T-1007 are gated on ratification; T-1008 is long-tail.\n\n"
            "## GOTCHAS (durable)\n"
            "```bash\ngit push origin main\n```\n"
            "Direct push succeeds via admin bypass. See tools/chronicle.py.\n\n"
            "requester: specialist\n"
        )
        # Same four sections; only the trailing parentheticals + the title date
        # changed (counts bumped, status reworded) -- a normal refresh.
        new = (
            "# HANDOFF -- Orchestrator -- 2026-08-08 ~04:30 UTC (mid-session refresh)\n"
            "target: orchestrator\n\n"
            "## RESOLVED THIS SESSION (10 tickets, 4 arcs)\n"
            "T-1002 T-1003 landed; T-1009 T-1010 new this cycle. Ledgered "
            "with PRs and independent smoke runs noted for each.\n\n"
            "## OPEN TICKETS (gated / long-tail)\n"
            "T-1006 T-1007 still gated on ratification; T-1008 long-tail.\n\n"
            "## GOTCHAS (durable + this-session additions)\n"
            "```bash\ngit push origin main\n```\n"
            "Direct push succeeds via admin bypass. See tools/chronicle.py.\n\n"
            "requester: specialist\n"
        )
        self.assertEqual(hc.check_conservation(old, new), [])

    def test_below_gate_old_text_stays_quiet_regardless_of_ratio(self):
        # OLD is tiny (below MIN_OLD_CHARS / MIN_OLD_ENTITIES / MIN_OLD_HEADINGS)
        # -- a bootstrap/degenerate prior snapshot must never trip any check.
        old = "# H\ntarget: orchestrator\n"
        new = ""
        self.assertEqual(hc.check_conservation(old, new), [])

    def test_identical_text_is_quiet(self):
        old = _synthetic_old()
        self.assertEqual(hc.check_conservation(old, old), [])



# ------------------------------------------------------ open-ticket loss (T-5347)
def _make_tickets_root(base: Path, inbox=(), active=(), done=()) -> Path:
    """Build a throwaway tickets/ tree (inbox/active/done) under a tmp dir."""
    root = base / "tickets"
    for sub, ids in (("inbox", inbox), ("active", active), ("done", done)):
        d = root / sub
        d.mkdir(parents=True)
        for tid in ids:
            (d / f"{tid}.md").write_text(f"---\nid: {tid}\n---\n")
    return root


class OpenTicketLossTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _alerts(self, old, new, root):
        return [a for a in hc.check_conservation(old, new, tickets_root=root)
                if a.startswith("open-ticket-loss")]

    def test_open_id_dropped_alerts(self):
        root = _make_tickets_root(self.base, inbox=["T-0101"], active=["T-0102"])
        alerts = self._alerts("T-0101 and T-0102 open", "nothing here", root)
        self.assertEqual(len(alerts), 1)
        self.assertIn("T-0101", alerts[0])
        self.assertIn("T-0102", alerts[0])
        self.assertIn("2 ", alerts[0])

    def test_done_id_dropped_no_alert(self):
        root = _make_tickets_root(self.base, done=["T-0201"])
        self.assertEqual(self._alerts("T-0201 shipped", "new text", root), [])

    def test_unknown_id_dropped_no_alert(self):
        root = _make_tickets_root(self.base, active=["T-0301"])
        self.assertEqual(self._alerts("T-9999 mystery", "new text", root), [])

    def test_id_present_in_both_no_alert(self):
        root = _make_tickets_root(self.base, active=["T-0401"])
        self.assertEqual(self._alerts("T-0401 open", "still T-0401", root), [])

    def test_only_open_dropped_ids_listed(self):
        root = _make_tickets_root(self.base, active=["T-0501", "T-0502"],
                                  done=["T-0503"])
        alerts = self._alerts("T-0501 T-0502 T-0503 T-0504", "T-0502", root)
        self.assertEqual(len(alerts), 1)
        self.assertIn("T-0501", alerts[0])
        for absent in ("T-0502", "T-0503", "T-0504"):
            self.assertNotIn(absent, alerts[0])

    def test_missing_tickets_root_no_alert_no_exception(self):
        missing = self.base / "does-not-exist"
        self.assertEqual(self._alerts("T-0601 open", "gone", missing), [])

    def test_no_tickets_root_skips_check(self):
        # Pure-function default: no tickets root -> the check does not run.
        self.assertEqual(
            [a for a in hc.check_conservation("T-0701", "x") if "open-ticket" in a], [])

    def test_cap_and_more_tail(self):
        ids = [f"T-{n:04d}" for n in range(1000, 1025)]  # 25 open ids
        root = _make_tickets_root(self.base, active=ids)
        alerts = self._alerts(" ".join(ids), "", root)
        self.assertEqual(len(alerts), 1)
        line = alerts[0]
        listed = [i for i in ids if i in line]
        self.assertEqual(len(listed), hc.OPEN_TICKET_LIST_CAP)
        self.assertIn(f"+{25 - hc.OPEN_TICKET_LIST_CAP} more", line)
        self.assertIn("25 ", line)

    def test_ids_sorted_numerically(self):
        root = _make_tickets_root(self.base, active=["T-0900", "T-10000"])
        line = self._alerts("T-10000 T-0900", "", root)[0]
        self.assertLess(line.index("T-0900"), line.index("T-10000"))

    def test_existing_alerts_unchanged_with_tickets_root(self):
        # Adding the fourth check must not alter the other three checks' output.
        root = _make_tickets_root(self.base, active=["T-1001", "T-1002"])
        old = _synthetic_old()
        new = "# HANDOFF\nDone for now.\n"
        without = hc.check_conservation(old, new)
        with_root = hc.check_conservation(old, new, tickets_root=root)
        self.assertEqual(
            [a for a in with_root if not a.startswith("open-ticket-loss")], without)
        self.assertEqual(len(with_root), len(without) + 1)
        self.assertTrue(with_root[-1].startswith("open-ticket-loss"))

    def test_normal_churn_with_empty_tickets_root_quiet(self):
        root = _make_tickets_root(self.base)
        old = _synthetic_old()
        self.assertEqual(hc.check_conservation(old, old, tickets_root=root), [])


# --------------------------------------------------------------------------- CLI
class CliCheckTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "chronicle"
        self.handoff_dir = Path(self._tmp.name) / "handoff"
        self.handoff_dir.mkdir()
        self.handoff_file = self.handoff_dir / "HANDOFF.md"

    def tearDown(self):
        self._tmp.cleanup()

    def _run_check(self, agent="orchestrator"):
        argv = ["check", "--handoff-file", str(self.handoff_file),
                "--agent", agent, "--root", str(self.root)]
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            rc = hc.main(argv)
        return rc, out.getvalue()

    def test_no_prior_snapshot_is_quiet_exit_0(self):
        self.handoff_file.write_text("# HANDOFF\nfresh start\n")
        rc, err = self._run_check()
        self.assertEqual(rc, 0)
        self.assertNotIn("ALERT", err)

    def test_unreadable_handoff_is_quiet_exit_0(self):
        rc, err = self._run_check()  # handoff_file never written
        self.assertEqual(rc, 0)
        self.assertNotIn("ALERT", err)

    def test_alerting_pair_exits_1_and_prints_reasons(self):
        cy.archive("orchestrator", _synthetic_old(), jsonl_uuid="uuid-old",
                   reason="graceful_restart", ts="2026-08-01T00:00:00Z", root=self.root)
        self.handoff_file.write_text("# HANDOFF\nDone for now.\n")
        rc, err = self._run_check()
        self.assertEqual(rc, 1)
        self.assertIn("ALERT", err)
        self.assertIn("size-cliff", err)

    def test_quiet_pair_exits_0_no_alerts(self):
        cy.archive("orchestrator", _synthetic_old(), jsonl_uuid="uuid-old",
                   reason="graceful_restart", ts="2026-08-01T00:00:00Z", root=self.root)
        self.handoff_file.write_text(_synthetic_old())  # unchanged -> quiet
        rc, err = self._run_check()
        self.assertEqual(rc, 0)
        self.assertNotIn("ALERT", err)


    def _run_check_tickets(self, tickets_root):
        argv = ["check", "--handoff-file", str(self.handoff_file),
                "--agent", "orchestrator", "--root", str(self.root),
                "--tickets-root", str(tickets_root)]
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            rc = hc.main(argv)
        return rc, out.getvalue()

    def test_cli_open_ticket_loss_alerts_via_tickets_root(self):
        troot = _make_tickets_root(Path(self._tmp.name), active=["T-1001"])
        cy.archive("orchestrator", _synthetic_old(), jsonl_uuid="uuid-old",
                   reason="graceful_restart", ts="2026-08-01T00:00:00Z", root=self.root)
        self.handoff_file.write_text(_synthetic_old().replace("T-1001", "T-1009"))
        rc, err = self._run_check_tickets(troot)
        self.assertEqual(rc, 1)
        self.assertIn("ALERT: open-ticket-loss", err)
        self.assertIn("T-1001", err)

    def test_cli_missing_tickets_root_is_quiet(self):
        cy.archive("orchestrator", _synthetic_old(), jsonl_uuid="uuid-old",
                   reason="graceful_restart", ts="2026-08-01T00:00:00Z", root=self.root)
        self.handoff_file.write_text(_synthetic_old().replace("T-1001", "T-1009"))
        rc, err = self._run_check_tickets(Path(self._tmp.name) / "nope")
        self.assertEqual(rc, 0)
        self.assertNotIn("ALERT", err)

    def test_default_tickets_root_is_repo_tickets(self):
        self.assertEqual(hc.default_tickets_root(),
                         Path(hc.__file__).resolve().parent.parent / "tickets")

if __name__ == "__main__":
    unittest.main()
