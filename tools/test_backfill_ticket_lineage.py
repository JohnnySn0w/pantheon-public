"""Tests for tools/backfill_ticket_lineage.py (T-3331).

Fixture tickets are built in a TemporaryDirectory; the real tickets/ corpus is
never read. The TestReviewRegressions cases reconstruct, in minimal form, each
WRONG tier-A edge the r1 review found (seed-5150 sample) so the pattern that
produced it stays excluded.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools import backfill_ticket_lineage as bf
from tools import ticket as tk

TS = "2026-10-26T12:00:00Z"
ACTOR = "fixture-actor"
NO_PT = object()  # sentinel: omit the parent_ticket line


def _ticket(tid: str, title: str, ask: str = "", *, context: str = "", state: str = "done",
            parent=None, extra_fm: str = "", log: str = "",
            created: str = "2026-06-01T00:00:00Z") -> str:
    pt = "" if parent is NO_PT else f"parent_ticket: {parent if parent else 'null'}\n"
    return (
        "---\n"
        f"id: {tid}\n"
        f"created: {created}\n"
        "requester: orchestrator\n"
        "target: orchestrator\n"
        f"state: {state}\n"
        f"{pt}"
        f"{extra_fm}"
        "labels: [x, y]\n"
        "---\n"
        "\n"
        f"# {title}\n"
        "\n"
        "## Ask\n"
        "\n"
        f"{ask}\n"
        "\n"
        "## Context\n"
        "\n"
        f"{context}\n"
        "\n"
        "## Transition log\n"
        "\n"
        f"- 2026-06-01T00:00:00Z [claude] filed in inbox\n{log}"
    )


class _Corpus:
    def __init__(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name) / "tickets"
        for d in ("inbox", "active", "blocked", "done", "plans"):
            (self.root / d).mkdir(parents=True)

    def add(self, tid: str, text: str, d: str = "done") -> Path:
        p = self.root / d / f"{tid}.md"
        p.write_text(text, encoding="utf-8")
        return p

    def close(self) -> None:
        self._td.cleanup()


class BackfillTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.c = _Corpus()
        self.addCleanup(self.c.close)
        # Low-id targets that exist and are not orphans.
        for tid in ("T-0100", "T-0200", "T-0300"):
            self.c.add(tid, _ticket(tid, "existing target", parent="T-0001"))

    def decisions(self, edge: str = bf.DEFAULT_EDGE) -> dict:
        return {d.tid: d for d in bf.scan(self.c.root, edge=edge).decisions}

    def one(self, tid: str, title: str, ask: str = "", **kw):
        self.c.add(tid, _ticket(tid, title, ask, **kw))
        return self.decisions()[tid]

    def run_apply(self, edge: str = bf.DEFAULT_EDGE) -> list[str]:
        return bf.apply(bf.scan(self.c.root, edge=edge), TS, ACTOR)


class TestTierAPrecedence(BackfillTestBase):
    def test_title_citation(self) -> None:
        d = self.one("T-1001", "harness sweep for T-0300 constants", "x")
        self.assertEqual((d.tier, d.target, d.rule), ("A", "T-0300", "title"))
        self.assertEqual((d.field_name, d.value), ("parent_ticket", "T-0300"))

    def test_title_strong_phrase_beats_earlier_title_mention(self) -> None:
        d = self.one("T-1002", "retire T-0100 shim (T-0200 follow-up)", "x")
        self.assertEqual((d.target, d.rule), ("T-0200", "title-phrase"))

    def test_strong_phrase_in_opening_sentence_of_ask(self) -> None:
        d = self.one("T-1003", "plain title", "Surfaced during T-0200 smoke: it broke. More.")
        self.assertEqual((d.tier, d.target, d.rule), ("A", "T-0200", "opening-body"))
        self.assertIn("Surfaced during T-0200", d.span)
        self.assertLessEqual(len(d.span), bf.SPAN_MAX)

    def test_strong_phrase_in_opening_sentence_of_context(self) -> None:
        d = self.one("T-1004", "plain title", "No refs here.",
                     context="Split from T-0100 at review time. Unrelated T-0200 later.")
        self.assertEqual((d.target, d.rule), ("T-0100", "opening-context"))

    def test_strong_phrase_after_first_sentence_is_tier_b(self) -> None:
        d = self.one("T-1005", "plain title",
                     "Long opening sentence without refs. Surfaced during T-0200 smoke.")
        self.assertEqual((d.tier, d.rule), ("B", "incidental"))
        self.assertIn("only incidental", d.value)

    def test_title_beats_body_strong_phrase(self) -> None:
        d = self.one("T-1006", "install the T-0100 monitor", "Follow-up to T-0200 really.")
        self.assertEqual((d.target, d.rule), ("T-0100", "title"))

    def test_bare_title_ref_mention_without_phrase_in_body_is_b(self) -> None:
        d = self.one("T-1007", "plain", "T-0200 added a thing that broke.")
        self.assertEqual((d.tier, d.rule), ("B", "incidental"))

    def test_declared_lineage_label_anywhere(self) -> None:
        d = self.one("T-1008", "plain", "Opening. Middle sentence here.",
                     context="Some prose. Parent lineage: T-0100 (the original fix).")
        self.assertEqual((d.target, d.rule), ("T-0100", "declared"))

    def test_from_the_arc_and_while_diagnosing(self) -> None:
        d = self.one("T-1009", "plain", "x", context="From the T-0100 cycle's flagged drift: y.")
        self.assertEqual((d.target, d.rule), ("T-0100", "opening-context"))
        d = self.one("T-1010", "plain", "While diagnosing T-0200, the journal was empty.")
        self.assertEqual(d.target, "T-0200")

    def test_weak_phrases_never_count_outside_title(self) -> None:
        for i, ask in enumerate(("Fires after T-0100 ratifies.", "Data from T-0100 here.",
                                 "Seen during T-0100 runs.", "Per T-0100 the shape holds.",
                                 "The post-T-0100 world.")):
            d = self.one(f"T-11{i:02d}", "plain", ask)
            self.assertEqual(d.tier, "B", ask)

    def test_weak_phrase_in_title_counts_as_title_citation(self) -> None:
        d = self.one("T-1020", "cron-fire test post-T-0100", "x")
        self.assertEqual((d.target, d.rule), ("T-0100", "title"))

    def test_value_passes_ticket_purpose_validator_in_intent_mode(self) -> None:
        from tools.ticket_purpose import parse_intent_ref
        self.c.add("T-1021", _ticket("T-1021", "t", "Follow-up from T-0100."))
        d = self.decisions(edge="nearest_intent_ref")["T-1021"]
        self.assertEqual(d.value, "ticket:T-0100")
        parse_intent_ref(d.value)


class TestReviewRegressions(BackfillTestBase):
    """Each WRONG tier-A edge from the r1 review, reconstructed minimally."""

    def test_quoted_gate_rationale(self) -> None:  # T-0429 -> T-0307
        d = self.one("T-1200", "re-evaluate the paused premise",
                     'Both blocked with rationale "this ticket fires after T-0100 ratifies." Done.',
                     context='"Follow-up to T-0200" was the old wording.')
        self.assertEqual(d.tier, "B")
        d = self.one("T-1201", "re-evaluate premise", "> Surfaced during T-0100 review, quoted.")
        self.assertEqual(d.tier, "B")

    def test_sibling_title_and_post_aside(self) -> None:  # T-0667 -> T-0412
        d = self.one("T-1202", "fill in-repo gaps — sibling to T-0100",
                     "Sibling to T-0100. The vestigial post-T-0200 path stays.")
        self.assertEqual(d.tier, "B")

    def test_known_residual_risk(self) -> None:  # T-0884 -> T-0824
        d = self.one("T-1203", "soak-confirm live fire", "Watch the known T-0100 residual risk.")
        self.assertEqual(d.tier, "B")

    def test_possessive(self) -> None:  # T-1656 -> T-1652
        d = self.one("T-1204", "stand up a control MCP",
                     "Split from T-0100's design: the tool is missing.",
                     context="Surfaced during T-0200’s wet-run.")
        self.assertEqual(d.tier, "B")

    def test_methodology_discipline_from(self) -> None:  # T-2171 -> T-2157
        d = self.one("T-1205", "gate parse omits a bucket", "x",
                     context="Found by using recovery's own discipline from T-0100: check guards fail.")
        self.assertEqual(d.tier, "B")

    def test_title_beats_post_phrase(self) -> None:  # T-2200 -> T-2158
        d = self.one("T-1206", "install the monitor timer (T-0100 shell is built)",
                     "Install it.", context="Derived against the post-T-0200 latency; follow-up to T-0200.")
        self.assertEqual((d.target, d.rule), ("T-0100", "title"))

    def test_quoted_text_from(self) -> None:  # T-2331 -> T-2313
        d = self.one("T-1207", "prescribed fix drops failing items",
                     "Amend per the quoted text from T-0100 in the entry.")
        self.assertEqual(d.tier, "B")

    def test_parallel_precedent_in_title(self) -> None:  # T-3081 -> T-0971
        d = self.one("T-1208", "strip stamp on unclaim (T-0100 arm-A parallel; TG diagnosis)", "x")
        self.assertEqual(d.tier, "B")

    def test_sister_and_dependency_senses(self) -> None:
        d = self.one("T-1209", "memory dir unversioned (orchestrator-side sister to T-0100)", "x")
        self.assertEqual(d.tier, "B")
        d = self.one("T-1210", "repoint the inbox map (deferred behind T-0100)", "x")
        self.assertEqual(d.tier, "B")

    def test_path_negation_and_analogy_excluded(self) -> None:
        d = self.one("T-1211", "see tickets/active/T-0100.md and a T-0200-style guard",
                     "Not from T-0300 at all.")
        self.assertEqual((d.tier, d.rule), ("B", "incidental"))


class TestIneligibleRefs(BackfillTestBase):
    def test_nonexistent_and_higher_ids_ignored(self) -> None:
        d = self.one("T-1300", "Follow-up to T-0999 and T-5000", "Split from T-0042 too.")
        self.assertEqual((d.tier, d.rule), ("B", "none"))
        self.assertIn("no earlier-ticket reference", d.value)

    def test_self_reference_ignored(self) -> None:
        self.assertEqual(self.one("T-1301", "T-1301 item", "Follow-up to T-1301.").tier, "B")

    def test_transition_log_refs_ignored(self) -> None:
        d = self.one("T-1302", "plain", "nothing here",
                     log="- 2026-06-02T00:00:00Z [claude] note; follow-up to T-0100\n")
        self.assertEqual((d.tier, d.rule), ("B", "none"))

    def test_autogenerated_brief_ignored(self) -> None:
        d = self.one("T-1303", "audit dispatch",
                     "Run it.\n\n--- BRIEF (paste) ---\n\nFollow-up to T-0100 template text.")
        self.assertEqual(d.rule, "none")

    def test_ticket_with_edge_or_orphan_reason_skipped(self) -> None:
        self.c.add("T-1304", _ticket("T-1304", "Follow-up to T-0100", extra_fm="serves: x\n"))
        self.c.add("T-1305", _ticket("T-1305", "Follow-up to T-0100",
                                     extra_fm="orphan_reason: already justified root\n"))
        self.c.add("T-1306", _ticket("T-1306", "Follow-up to T-0100", parent="T-0200"))
        ds = self.decisions()
        for tid in ("T-1304", "T-1305", "T-1306"):
            self.assertNotIn(tid, ds)


class TestGuards(BackfillTestBase):
    def test_inverse_edge_demoted(self) -> None:  # T-2823 -> T-2809 shape
        self.c.add("T-0400", _ticket("T-0400", "child scoped from the canonicalize ticket", parent="T-1400"))
        self.c.add("T-0401", _ticket("T-0401", "dep child", parent="T-0001",
                                     extra_fm="depends_on: [T-1401]\n"))
        self.c.add("T-0402", _ticket("T-0402", "intent child", parent="T-0001",
                                     extra_fm="nearest_intent_ref: ticket:T-1402\n"))
        self.c.add("T-1400", _ticket("T-1400", "canonicalize the draft + anchor T-0400", "x"))
        self.c.add("T-1401", _ticket("T-1401", "decide (T-0401 follow-up)", "x"))
        self.c.add("T-1402", _ticket("T-1402", "lift T-0402 deliverable", "x"))
        ds = self.decisions()
        for tid, tgt in (("T-1400", "T-0400"), ("T-1401", "T-0401"), ("T-1402", "T-0402")):
            self.assertEqual((ds[tid].tier, ds[tid].rule, ds[tid].guard_target),
                             ("B", "guard-inverse", tgt), tid)
            self.assertIn("points back at this ticket", ds[tid].value)

    def test_cycle_via_existing_chain_demoted(self) -> None:
        self.c.add("T-0500", _ticket("T-0500", "mid", parent="T-1500"))
        self.c.add("T-0510", _ticket("T-0510", "low", parent="T-0500"))
        self.c.add("T-1500", _ticket("T-1500", "T-0510 residuals", "x"))
        d = self.decisions()["T-1500"]
        self.assertEqual((d.tier, d.rule, d.guard_target), ("B", "guard-cycle", "T-0510"))

    def test_cycle_through_a_batch_edge_demoted(self) -> None:
        # existing upward edge T-0600 -> T-1610; new batch edge T-1600 -> T-0600;
        # then T-1610 -> T-1600 would close T-1610 -> T-1600 -> T-0600 -> T-1610.
        self.c.add("T-0600", _ticket("T-0600", "low", parent="T-1610"))
        self.c.add("T-1600", _ticket("T-1600", "T-0600 residuals", "x"))
        self.c.add("T-1610", _ticket("T-1610", "T-1600 follow-up", "x"))
        ds = self.decisions()
        self.assertEqual((ds["T-1600"].tier, ds["T-1600"].target), ("A", "T-0600"))
        self.assertEqual((ds["T-1610"].tier, ds["T-1610"].rule), ("B", "guard-cycle"))

    def test_no_guard_when_no_back_edge(self) -> None:
        self.c.add("T-1700", _ticket("T-1700", "T-0100 residuals", "x"))
        self.assertEqual(self.decisions()["T-1700"].tier, "A")


class TestScope(BackfillTestBase):
    def test_open_orphans_left_for_owners_and_untouched(self) -> None:
        paths = {d: self.c.add(f"T-18{i}0", _ticket(f"T-18{i}0", "T-0100 follow-up", "x", state=d), d=d)
                 for i, d in enumerate(("inbox", "active", "blocked"))}
        done = self.c.add("T-1890", _ticket("T-1890", "T-0100 follow-up", "x"))
        snap = {d: p.read_bytes() for d, p in paths.items()}
        res = bf.scan(self.c.root)
        self.assertEqual([d.tid for d in res.decisions], ["T-1890"])
        self.assertEqual(sorted(d.tid for d in res.left_for_owners), ["T-1800", "T-1810", "T-1820"])
        bf.apply(res, TS, ACTOR)
        self.assertEqual({d: p.read_bytes() for d, p in paths.items()}, snap)
        self.assertIn("parent_ticket: T-0100", done.read_text(encoding="utf-8"))
        rep = bf.build_report(res)
        self.assertEqual(sorted(r["tid"] for r in rep["left_for_owners"]), ["T-1800", "T-1810", "T-1820"])
        self.assertIn("left for owners", bf.human_summary(rep))

    def test_apply_refuses_an_open_ticket_decision(self) -> None:
        p = self.c.add("T-1891", _ticket("T-1891", "plain", "x", state="inbox"), d="inbox")
        d = bf.Decision(tid="T-1891", path=p, state_dir="inbox", tier="B", rule="none",
                        field_name="orphan_reason", value="x" * 30, detail="d")
        with self.assertRaises(bf.BackfillError):
            bf.apply(bf.ScanResult(decisions=[d]), TS, ACTOR)


class TestTierB(BackfillTestBase):
    def test_tier_b_text_pre_and_post_guard(self) -> None:
        self.c.add("T-1900", _ticket("T-1900", "plain", "nothing"))
        self.c.add("T-4000", _ticket("T-4000", "plain", "nothing", created="2026-10-01T00:00:00Z"))
        ds = self.decisions()
        self.assertEqual(ds["T-1900"].value, (
            "historical root filed before the T-3328 file-time guard; T-3331 backfill "
            "found no earlier-ticket reference in its title or body"))
        self.assertTrue(ds["T-4000"].value.startswith(
            "filed without a lineage edge despite the T-3328 file-time guard;"))
        for d in ds.values():
            self.assertGreaterEqual(len(d.value), 20)
        self.assertEqual(ds["T-1900"].detail,
                         "lineage backfill (T-3331): orphan_reason set (no earlier-ticket reference)")


class TestUnparseable(BackfillTestBase):
    def test_unparseable_file_skipped_and_untouched(self) -> None:
        bad = "no front matter here\n# T-0100 follow-up\n"
        p = self.c.add("T-2000", bad)
        plan = self.c.add("T-2001-plan", "# plan doc\n", d="plans")
        res = bf.scan(self.c.root)
        paths = [u[0] for u in res.unparseable]
        self.assertTrue(any(x.endswith("done/T-2000.md") for x in paths))
        self.assertTrue(any(x.endswith("plans/T-2001-plan.md") for x in paths))
        bf.apply(res, TS, ACTOR)
        self.assertEqual(p.read_text(encoding="utf-8"), bad)
        self.assertEqual(plan.read_text(encoding="utf-8"), "# plan doc\n")


class TestApply(BackfillTestBase):
    def _seed(self) -> dict:
        texts = {
            "T-2100": _ticket("T-2100", "t", "Follow-up to T-0100.", extra_fm="depends_on: []\n"),
            "T-2101": _ticket("T-2101", "plain", "nothing"),
            # unknown key mid-block + stale_after after labels (legacy shape)
            "T-2102": _ticket("T-2102", "t", "Surfaced during T-0200 run.",
                              extra_fm="resolved: 2026-06-02T00:00:00Z\n").replace(
                                  "labels: [x, y]\n", "labels: [x, y]\nstale_after: 3d\n"),
        }
        return {tid: (self.c.add(tid, t), t) for tid, t in texts.items()}

    def test_parent_ticket_default_replaces_null_line_in_place(self) -> None:
        seeded = self._seed()
        changed = self.run_apply()
        self.assertEqual(sorted(changed), ["T-2100", "T-2101", "T-2102"])
        for tid in ("T-2100", "T-2102"):
            p, old = seeded[tid]
            new = p.read_text(encoding="utf-8")
            o, n = old.split("\n"), new.split("\n")
            # same line count + 1 appended log line; exactly one line differs in place
            self.assertEqual(len(n), len(o) + 1, tid)
            diffs = [(a, b) for a, b in zip(o, n) if a != b]
            self.assertEqual(len(diffs), 2, (tid, diffs))  # replaced line + the old trailing ""
            (old_line, new_line), (trail_old, log_line) = diffs
            self.assertEqual(old_line, "parent_ticket: null")
            self.assertRegex(new_line, r"^parent_ticket: T-0[12]00$")
            self.assertEqual(trail_old, "")
            self.assertTrue(new.endswith(log_line + "\n"))
            self.assertRegex(log_line, r"^- 2026-10-26T12:00:00Z \[fixture-actor\] note; "
                                       r"lineage backfill \(T-3331, parent_ticket\): parent_ticket T-0[12]00 from ")
            self.assertNotIn(" @", log_line.split(";", 1)[0])  # no stale repo-sha stamp
            # reverting the one line + dropping the log line == original bytes
            self.assertEqual(new.replace(new_line, old_line, 1)[: -len(log_line) - 1], old)
            fields, _ = tk.parse_front_matter(new)
            self.assertNotIn("project_id", fields)
            self.assertNotIn("nearest_intent_ref", fields)

    def test_tier_b_inserts_orphan_reason_keeps_parent_null(self) -> None:
        seeded = self._seed()
        self.run_apply()
        p, old = seeded["T-2101"]
        new = p.read_text(encoding="utf-8")
        self.assertIn("parent_ticket: null\norphan_reason: historical root", new)
        self.assertEqual(len(new.split("\n")), len(old.split("\n")) + 2)
        fields, body = tk.parse_front_matter(new)
        self.assertEqual(tk.serialize_front_matter(fields) + "\n" + body, new)

    def test_nearest_intent_ref_mode_inserts_and_keeps_null_parent(self) -> None:
        seeded = self._seed()
        self.run_apply(edge="nearest_intent_ref")
        t2100 = seeded["T-2100"][0].read_text(encoding="utf-8")
        self.assertIn("state: done\nnearest_intent_ref: ticket:T-0100\nparent_ticket: null\n", t2100)
        self.assertIn("(T-3331, nearest_intent_ref)", t2100)
        t2102 = seeded["T-2102"][0].read_text(encoding="utf-8")
        self.assertIn("nearest_intent_ref: ticket:T-0200\nparent_ticket: null\n", t2102)

    def test_parent_ticket_inserted_when_key_absent(self) -> None:
        p = self.c.add("T-2103", _ticket("T-2103", "T-0100 residuals", "x", parent=NO_PT))
        old = p.read_text(encoding="utf-8")
        self.run_apply()
        new = p.read_text(encoding="utf-8")
        self.assertIn("state: done\nparent_ticket: T-0100\nlabels:", new)
        self.assertEqual(len(new.split("\n")), len(old.split("\n")) + 2)

    def test_replace_refuses_non_null_value(self) -> None:
        text = _ticket("T-2104", "t", parent="T-0100")
        with self.assertRaises(bf.BackfillError):
            bf._replace_null_line(text, "parent_ticket", "T-0200")

    def test_edge_flag_default_is_parent_ticket(self) -> None:
        self.assertEqual(bf.DEFAULT_EDGE, "parent_ticket")
        self._seed()
        rep = Path(self.c.root.parent) / "out" / "r.json"
        self.assertEqual(bf.main(["--dry-run", "--tickets-root", str(self.c.root), "--report", str(rep)]), 0)
        data = json.loads(rep.read_text(encoding="utf-8"))
        self.assertEqual(data["edge"], "parent_ticket")
        self.assertTrue(all(r["field"] == "parent_ticket" for r in data["all"] if r["tier"] == "A"))
        self.assertEqual(bf.main(["--dry-run", "--edge", "nearest_intent_ref", "--tickets-root",
                                  str(self.c.root), "--report", str(rep)]), 0)
        data = json.loads(rep.read_text(encoding="utf-8"))
        self.assertEqual(data["edge"], "nearest_intent_ref")
        with self.assertRaises(SystemExit):
            bf.main(["--dry-run", "--edge", "project_id", "--tickets-root", str(self.c.root),
                     "--report", str(rep)])

    def test_idempotent_second_apply_changes_nothing(self) -> None:
        self._seed()
        self.run_apply()
        snap = {p: p.read_bytes() for p in self.c.root.rglob("*.md")}
        self.assertEqual(self.run_apply(), [])
        self.assertEqual({p: p.read_bytes() for p in self.c.root.rglob("*.md")}, snap)

    def test_dry_run_writes_report_not_tickets(self) -> None:
        self._seed()
        snap = {p: p.read_bytes() for p in self.c.root.rglob("*.md")}
        rep = Path(self.c.root.parent) / "out" / "r.json"
        rc = bf.main(["--dry-run", "--tickets-root", str(self.c.root), "--report", str(rep)])
        self.assertEqual(rc, 0)
        self.assertEqual({p: p.read_bytes() for p in self.c.root.rglob("*.md")}, snap)
        data = json.loads(rep.read_text(encoding="utf-8"))
        self.assertEqual((data["tier_a"], data["tier_b"]), (2, 1))
        self.assertEqual(data["tier_a_by_rule"], {"opening-body": 2})
        self.assertEqual(len(data["tier_a_sample"]), 2)
        self.assertTrue(all(r["span"] for r in data["tier_a_sample"]))
        self.assertTrue(rep.with_suffix(".txt").is_file())

    def test_refuses_without_mode(self) -> None:
        self.assertEqual(bf.main(["--tickets-root", str(self.c.root)]), 64)
        self.assertEqual(bf.main(["--dry-run", "--tickets-root", str(self.c.root)]), 64)


if __name__ == "__main__":
    unittest.main()
