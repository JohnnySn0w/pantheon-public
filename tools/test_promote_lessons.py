#!/usr/bin/env python3
"""Tests for tools/promote_lessons.py (T-0649 increment 2, report-only detector).

Run: python3 -m unittest tools.test_promote_lessons
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from tools import promote_lessons as pl


def _write(d: Path, name: str, text: str) -> Path:
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


# --- Representative corpus fixtures (modeled on the real entries) ------------

POS_BACKTICKS = """# Never put backticks in a bash double-quoted string

Backticks inside a bash double-quoted string trigger COMMAND SUBSTITUTION — bash
EXECUTES them. Never put backticks in shell args. This bit me twice and nearly
tripped the bridge-kill landmine (T-0123, T-0456).
"""

POS_SELFTAG = """# Ticket files must carry a state field

Every ticket frontmatter must have a `state:` field. This should be a
ticket_lint rule / pre-commit check so a hand-filed ticket can never omit it.
"""

POS_IRREVERSIBLE = """# Never force-push the shared main branch

A force-push to origin/main can destroy history irreversibly. Never force-push;
require a plain push. The byte-sequence guard is the model.
"""

NEG_SOFT_JUDGMENT = """# Verify root cause before asserting it

Check the journal and timestamps before naming a cause; don't ship a plausible
guess. Beware recency bias toward what you just built. A held diagnosis beats a
retracted one. This is about judgment, not a mechanical rule.
"""

NEG_AUTONOMY = """# Autonomy bar higher overnight

Hold the bar higher overnight; decide on the merits. This is contextual
judgment about how to weigh autonomy — stays soft.
"""

NEG_NO_INVARIANT = """# A conceptual lesson with no mechanical rule

This entry recurred twice and describes an irreversible outage, but it contains
no quotable byte/path/field rule — it's a philosophy about distributed systems.
"""

# T-4660 fixtures, modeled on the 2026-10-05 review's false positives.
NEG_MENTION_ONLY = """# A guard script crashes on a renamed config key

The pre-commit hook runs ticket_lint on every commit.
The loader must not read `config.yaml:old_key`; it was renamed in T-0999.
"""

REQUEST_BASE = """# Ticket files must carry a state field

Every ticket frontmatter must have a `state:` field.
"""

NEG_LANDED_GUARD = """# Tests appended after the __main__ guard never run

Appending a test class with cat >> can land it after `if __name__ == "__main__":`,
and it never runs. This recurred twice.
Never put a test class after `if __name__ == "__main__":`. The fix
landed as `tools/main_guard_lint.py`, wired into .githooks/pre-commit.
"""

BUG_EXTERNAL_QUIRK = """# CSS intrinsic content min by default

## Symptom
Grid/flex children overflow.

## Fix
Set min-width: 0. External CSS behavior; not an org invariant.
"""


class DetectTest(unittest.TestCase):
    def _detect(self, name, text, corpus="memory"):
        with TemporaryDirectory() as td:
            p = _write(Path(td), name, text)
            return pl.detect(p, text, corpus)

    def test_recurrence_plus_mechanical_qualifies(self):
        c = self._detect("feedback_backticks.md", POS_BACKTICKS)
        self.assertIsNotNone(c)
        self.assertEqual(c.check_kind, "pre-commit")
        self.assertTrue(c.signals["recurrence"])
        self.assertTrue(c.signals["mechanical"])

    def test_self_tag_high_confidence_and_ticket_lint_kind(self):
        c = self._detect("feedback_state_field.md", POS_SELFTAG)
        self.assertIsNotNone(c)
        self.assertEqual(c.confidence, "high")
        self.assertEqual(c.check_kind, "ticket_lint-rule")
        self.assertTrue(c.signals["self_tag"])

    def test_irreversible_alone_qualifies_at_medium(self):
        c = self._detect("feedback_force_push.md", POS_IRREVERSIBLE)
        self.assertIsNotNone(c)
        # "The byte-sequence guard is the model" MENTIONS a guard; it does not ask
        # to become one (T-4660), so only (recurrence+irreversible) could make it high.
        self.assertEqual(c.confidence, "medium")
        self.assertFalse(c.signals["self_tag"])
        self.assertTrue(c.signals["irreversible"])

    # -- T-4660: the first recovery review found 0 of 19 filed candidates promotable --
    def test_passing_mention_of_precommit_or_hook_is_not_a_self_tag(self):
        c = self._detect("bug_mention.md", NEG_MENTION_ONLY, "bugs")
        self.assertIsNone(c, "describing an existing pre-commit hook is not asking for one")

    def test_request_forms_are_self_tags(self):
        for phrase in ("This should be a pre-commit check.",
                       "Promote this to a ticket_lint guard.",
                       "It is a candidate for a lint rule.",
                       "This needs to become a hook check."):
            with self.subTest(phrase=phrase):
                c = self._detect("feedback_ask.md", REQUEST_BASE + phrase + "\n")
                self.assertIsNotNone(c)
                self.assertTrue(c.signals["self_tag"])
                self.assertEqual(c.confidence, "high")

    def test_entry_naming_its_landed_guard_is_not_proposed(self):
        self.assertIsNone(self._detect("bug_landed.md", NEG_LANDED_GUARD, "bugs"),
                          "the entry already points at the guard that enforces it")

    def test_guard_path_without_a_landed_verb_still_qualifies(self):
        text = POS_SELFTAG + "\nSee `tools/ticket_lint.py` for the existing rule shapes.\n"
        self.assertIsNotNone(self._detect("feedback_state_field.md", text))

    def test_landed_verb_without_a_guard_path_still_qualifies(self):
        text = POS_SELFTAG + "\nA partial fix landed in the docs only.\n"
        self.assertIsNotNone(self._detect("feedback_state_field.md", text))

    def test_soft_judgment_vetoed(self):
        self.assertIsNone(self._detect("feedback_verify_root.md", NEG_SOFT_JUDGMENT))

    def test_autonomy_vetoed(self):
        self.assertIsNone(self._detect("feedback_autonomy.md", NEG_AUTONOMY))

    def test_no_mechanical_invariant_not_emitted(self):
        self.assertIsNone(self._detect("lesson_concept.md", NEG_NO_INVARIANT, "lessons"))

    def test_external_quirk_bug_not_promoted(self):
        self.assertIsNone(self._detect("css.md", BUG_EXTERNAL_QUIRK, "bugs"))


class ScanAndReportTest(unittest.TestCase):
    def _corpus(self, td: Path):
        les, bug, mem = td / "lessons", td / "bugs", td / "memory"
        for d in (les, bug, mem):
            d.mkdir()
        # README must be skipped.
        _write(les, "README.md", "# Lessons\n")
        _write(mem, "feedback_backticks.md", POS_BACKTICKS)
        _write(mem, "feedback_state_field.md", POS_SELFTAG)
        _write(mem, "feedback_verify_root.md", NEG_SOFT_JUDGMENT)
        # non-feedback memory file must be ignored.
        _write(mem, "project_random.md", POS_BACKTICKS)
        _write(bug, "css.md", BUG_EXTERNAL_QUIRK)
        return les, bug, mem

    def test_scan_picks_only_positives(self):
        with TemporaryDirectory() as t:
            les, bug, mem = self._corpus(Path(t))
            cands = pl.scan_all(les, bug, mem)
            sources = {Path(c.source).name for c in cands}
            self.assertEqual(sources, {"feedback_backticks.md", "feedback_state_field.md"})

    def test_sorted_high_confidence_first(self):
        with TemporaryDirectory() as t:
            les, bug, mem = self._corpus(Path(t))
            cands = pl.scan_all(les, bug, mem)
            self.assertEqual(cands[0].confidence, "high")

    def test_min_confidence_filter_via_main_json(self):
        import contextlib, io
        with TemporaryDirectory() as t:
            les, bug, mem = self._corpus(Path(t))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = pl.main([
                    "--lessons-dir", str(les), "--bugs-dir", str(bug),
                    "--memory-dir", str(mem), "--min-confidence", "high", "--json",
                ])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertTrue(all(c["confidence"] == "high" for c in data))
            self.assertTrue(len(data) >= 1)

    def test_report_renders_empty(self):
        self.assertIn("0 promotable", pl.render_report([]))


class LedgerTest(unittest.TestCase):
    def _cand(self, source="feedback_x.md", invariant="never do X with `state:`"):
        return pl.Candidate(
            source=source, corpus="memory", title="t", invariant=invariant,
            check_kind="pre-commit", draft="d", rationale="r", confidence="high",
        )

    def test_fingerprint_stable_and_invariant_sensitive(self):
        a = self._cand()
        b = self._cand()  # same source + invariant
        c = self._cand(invariant="a different invariant")
        self.assertEqual(a.fingerprint, b.fingerprint)
        self.assertNotEqual(a.fingerprint, c.fingerprint)
        self.assertTrue(a.fingerprint.startswith("feedback_x.md:"))

    def test_load_missing_or_corrupt_ledger_is_empty(self):
        with TemporaryDirectory() as t:
            self.assertEqual(pl.load_ledger(Path(t) / "nope.json"), {})
            bad = Path(t) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            self.assertEqual(pl.load_ledger(bad), {})

    def test_record_then_filter_is_idempotent(self):
        with TemporaryDirectory() as t:
            led = Path(t) / "ledger.json"
            cands = [self._cand(source="a.md"), self._cand(source="b.md")]
            pl.record_ledger(led, cands, "filed")
            # a re-sweep finds the same candidates → all filtered out.
            remaining = pl.filter_unledgered(cands, pl.load_ledger(led))
            self.assertEqual(remaining, [])
            # a genuinely new candidate survives.
            fresh = self._cand(source="c.md")
            self.assertEqual(
                pl.filter_unledgered(cands + [fresh], pl.load_ledger(led)),
                [fresh],
            )

    def test_record_does_not_downgrade_existing_status(self):
        with TemporaryDirectory() as t:
            led = Path(t) / "ledger.json"
            c = self._cand(source="a.md")
            pl.record_ledger(led, [c], "declined")
            pl.record_ledger(led, [c], "filed")  # must NOT overwrite declined
            self.assertEqual(pl.load_ledger(led)[c.fingerprint]["status"], "declined")

    def test_record_rejects_bad_status(self):
        with TemporaryDirectory() as t:
            with self.assertRaises(ValueError):
                pl.record_ledger(Path(t) / "l.json", [self._cand()], "bogus")

    # --- T-2827 defect 1: decision timestamps ------------------------------

    def test_filed_at_stamped_on_new_filed_entry(self):
        with TemporaryDirectory() as t:
            led = Path(t) / "l.json"
            c = self._cand(source="a.md")
            fixed = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)
            pl.record_ledger(led, [c], "filed", now=fixed)
            entry = pl.load_ledger(led)[c.fingerprint]
            self.assertEqual(entry["filed_at"], "2026-08-11T12:00:00Z")

    def test_filed_at_not_stamped_for_non_filed_status(self):
        """filed_at means 'first recorded as filed' — a record_ledger call
        under a different status (e.g. a direct 'declined' record, exercised
        by test_record_does_not_downgrade_existing_status-style callers) must
        not stamp it, or the field would lie about what happened (P4)."""
        with TemporaryDirectory() as t:
            led = Path(t) / "l.json"
            c = self._cand(source="a.md")
            fixed = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)
            pl.record_ledger(led, [c], "declined", now=fixed)
            entry = pl.load_ledger(led)[c.fingerprint]
            self.assertNotIn("filed_at", entry)

    def test_decided_at_stamped_on_land_and_decline(self):
        with TemporaryDirectory() as t:
            led = Path(t) / "l.json"
            fixed = datetime(2026, 8, 11, 13, 0, 0, tzinfo=timezone.utc)

            c1 = self._cand(source="a.md")
            pl.record_ledger(led, [c1], "filed")
            pl.set_status(led, c1.fingerprint, "landed", now=fixed)
            entry1 = pl.load_ledger(led)[c1.fingerprint]
            self.assertEqual(entry1["decided_at"], "2026-08-11T13:00:00Z")
            self.assertEqual(entry1["status"], "landed")

            c2 = self._cand(source="b.md")
            pl.record_ledger(led, [c2], "filed")
            pl.set_status(led, c2.fingerprint, "declined", now=fixed)
            entry2 = pl.load_ledger(led)[c2.fingerprint]
            self.assertEqual(entry2["decided_at"], "2026-08-11T13:00:00Z")
            self.assertEqual(entry2["status"], "declined")

    def test_decided_at_not_bumped_by_redundant_identical_call(self):
        with TemporaryDirectory() as t:
            led = Path(t) / "l.json"
            c = self._cand(source="a.md")
            pl.record_ledger(led, [c], "filed")
            first = datetime(2026, 8, 11, 13, 0, 0, tzinfo=timezone.utc)
            later = datetime(2026, 8, 12, 9, 0, 0, tzinfo=timezone.utc)
            pl.set_status(led, c.fingerprint, "declined", now=first)
            pl.set_status(led, c.fingerprint, "declined", now=later)  # no-op re-call
            entry = pl.load_ledger(led)[c.fingerprint]
            self.assertEqual(entry["decided_at"], "2026-08-11T13:00:00Z")

    def test_decided_at_not_fabricated_for_legacy_terminal_entry_on_noop_recall(self):
        """A pre-existing (T-2827-predating) terminal entry has NO decided_at —
        10 of 18 live ledger entries are exactly this shape. A redundant
        identical set_status re-call (the module's own documented idempotent
        no-op — e.g. a recovery double-run) must NOT invent a decision timestamp
        for a decision that happened at an unknown earlier time (P4:
        backward-compat means tolerate absence, never fabricate)."""
        with TemporaryDirectory() as t:
            led = Path(t) / "l.json"
            led.write_text(json.dumps({
                "legacy.md:deadbeef0000": {
                    "status": "declined", "source": "x", "title": "t",
                    "check_kind": "pre-commit",
                    # no filed_at, no decided_at — legacy shape.
                },
            }), encoding="utf-8")
            later = datetime(2026, 8, 12, 9, 0, 0, tzinfo=timezone.utc)
            pl.set_status(led, "legacy.md:deadbeef0000", "declined", now=later)
            entry = pl.load_ledger(led)["legacy.md:deadbeef0000"]
            self.assertNotIn("decided_at", entry)
            self.assertEqual(entry["status"], "declined")

    def test_decided_at_re_stamps_on_genuine_status_change(self):
        with TemporaryDirectory() as t:
            led = Path(t) / "l.json"
            c = self._cand(source="a.md")
            pl.record_ledger(led, [c], "filed")
            first = datetime(2026, 8, 11, 13, 0, 0, tzinfo=timezone.utc)
            later = datetime(2026, 8, 12, 9, 0, 0, tzinfo=timezone.utc)
            pl.set_status(led, c.fingerprint, "landed", now=first)
            pl.set_status(led, c.fingerprint, "declined", now=later)  # a real correction
            entry = pl.load_ledger(led)[c.fingerprint]
            self.assertEqual(entry["decided_at"], "2026-08-12T09:00:00Z")
            self.assertEqual(entry["status"], "declined")

    def test_pre_existing_timestampless_entries_still_load(self):
        """Entries written before T-2827 lack filed_at/decided_at — the ledger
        must tolerate absence, never fabricate the missing timestamps (P4)."""
        with TemporaryDirectory() as t:
            led = Path(t) / "l.json"
            led.write_text(json.dumps({
                "old.md:abcd1234": {
                    "status": "filed", "source": "x", "title": "t",
                    "check_kind": "pre-commit",
                },
            }), encoding="utf-8")
            ledger = pl.load_ledger(led)
            entry = ledger["old.md:abcd1234"]
            self.assertNotIn("filed_at", entry)
            self.assertNotIn("decided_at", entry)
            self.assertEqual(entry["status"], "filed")


class FileCandidatesTest(unittest.TestCase):
    """inc 3b: filing candidates to the review surface (no tickets, no git)."""

    def _cand(self, source="feedback_x.md", invariant="never do X with `state:`"):
        return pl.Candidate(
            source=source, corpus="memory", title="Title here", invariant=invariant,
            check_kind="pre-commit", draft="def check():\n    return True",
            rationale="bit twice", confidence="high",
        )

    def test_writes_md_and_records_filed(self):
        with TemporaryDirectory() as t:
            surface, led = Path(t) / "cands", Path(t) / "ledger.json"
            cands = [self._cand(source="a.md"), self._cand(source="b.md")]
            written = pl.file_candidates(cands, surface, led)
            self.assertEqual(len(written), 2)
            for p in written:
                self.assertTrue(p.exists())
                self.assertTrue(p.name.endswith(".md"))
            ledger = pl.load_ledger(led)
            for c in cands:
                self.assertEqual(ledger[c.fingerprint]["status"], "filed")

    def test_md_content_has_frontmatter_and_draft(self):
        c = self._cand()
        md = pl.render_candidate_md(c)
        self.assertIn(f"fingerprint: {c.fingerprint}", md)
        self.assertIn("status: pending-review", md)
        self.assertIn(c.invariant, md)
        self.assertIn("def check():", md)  # the draft, fenced
        self.assertIn("ACCEPT", md)
        self.assertIn("DECLINE", md)

    def test_idempotent_second_call_writes_nothing(self):
        with TemporaryDirectory() as t:
            surface, led = Path(t) / "cands", Path(t) / "ledger.json"
            cands = [self._cand(source="a.md")]
            self.assertEqual(len(pl.file_candidates(cands, surface, led)), 1)
            # a re-sweep finds the same candidate already ledgered → no-op.
            self.assertEqual(pl.file_candidates(cands, surface, led), [])

    def test_declined_candidate_is_not_refiled(self):
        with TemporaryDirectory() as t:
            surface, led = Path(t) / "cands", Path(t) / "ledger.json"
            c = self._cand(source="a.md")
            pl.record_ledger(led, [c], "declined")  # recovery rejected it earlier
            written = pl.file_candidates([c], surface, led)
            self.assertEqual(written, [])  # never re-proposed
            self.assertFalse((surface / pl._candidate_filename(c)).exists())
            # and the decline is preserved, not overwritten to 'filed'.
            self.assertEqual(pl.load_ledger(led)[c.fingerprint]["status"], "declined")

    def test_filename_is_fs_safe(self):
        c = self._cand(source="feedback_x.md")
        name = pl._candidate_filename(c)
        self.assertNotIn(":", name)
        self.assertTrue(name.startswith("feedback_x__"))

    def test_main_file_requires_ledger(self):
        with TemporaryDirectory() as t:
            with self.assertRaises(SystemExit):
                pl.main(["--file", "--lessons-dir", t, "--bugs-dir", t,
                         "--memory-dir", t, "--candidates-dir", t])


class ReviewTest(unittest.TestCase):
    """inc 3b/4: recovery review verbs over a filed surface (set_status / review)."""

    def _cand(self, source="feedback_x.md", invariant="never do X with `state:`"):
        return pl.Candidate(
            source=source, corpus="memory", title="T", invariant=invariant,
            check_kind="pre-commit", draft="d", rationale="bit twice",
            confidence="high",
        )

    def test_set_status_transitions_filed_to_terminal(self):
        with TemporaryDirectory() as t:
            led = Path(t) / "l.json"
            c = self._cand()
            pl.record_ledger(led, [c], "filed")
            pl.set_status(led, c.fingerprint, "declined")
            self.assertEqual(pl.load_ledger(led)[c.fingerprint]["status"], "declined")

    def test_set_status_rejects_non_terminal(self):
        with TemporaryDirectory() as t:
            with self.assertRaises(ValueError):
                pl.set_status(Path(t) / "l.json", "fp", "filed")

    def test_review_decline_removes_file_and_records(self):
        with TemporaryDirectory() as t:
            surface, led = Path(t) / "cands", Path(t) / "l.json"
            c = self._cand()
            pl.file_candidates([c], surface, led)
            fpath = pl.find_candidate_file(surface, c.fingerprint)
            self.assertIsNotNone(fpath)
            removed = pl.review_candidate(surface, led, c.fingerprint, "decline")
            self.assertTrue(removed)
            self.assertFalse(fpath.exists())
            self.assertEqual(pl.load_ledger(led)[c.fingerprint]["status"], "declined")
            # a re-sweep must NOT re-file a declined candidate.
            self.assertEqual(pl.file_candidates([c], surface, led), [])

    def test_review_land_marks_landed(self):
        with TemporaryDirectory() as t:
            surface, led = Path(t) / "cands", Path(t) / "l.json"
            c = self._cand()
            pl.file_candidates([c], surface, led)
            pl.review_candidate(surface, led, c.fingerprint, "land")
            self.assertEqual(pl.load_ledger(led)[c.fingerprint]["status"], "landed")

    def test_list_pending_reads_frontmatter(self):
        with TemporaryDirectory() as t:
            surface, led = Path(t) / "cands", Path(t) / "l.json"
            cands = [self._cand(source="a.md"), self._cand(source="b.md")]
            pl.file_candidates(cands, surface, led)
            pending = pl.list_pending(surface)
            self.assertEqual(len(pending), 2)
            self.assertTrue(all(fm.get("status") == "pending-review" for fm in pending))
            self.assertTrue(all("fingerprint" in fm for fm in pending))

    def test_list_pending_excludes_readme(self):
        with TemporaryDirectory() as t:
            surface = Path(t) / "cands"
            surface.mkdir()
            (surface / "README.md").write_text("# surface\n", encoding="utf-8")
            self.assertEqual(pl.list_pending(surface), [])

    def test_main_decline_requires_ledger(self):
        with self.assertRaises(SystemExit):
            pl.main(["--decline", "feedback_x.md:abc", "--candidates-dir", "/tmp/x"])

    # --- T-2827 defect 2: preserve decided candidates, never delete --------

    def test_review_land_moves_to_decided_not_deletes(self):
        with TemporaryDirectory() as t:
            surface, led = Path(t) / "cands", Path(t) / "l.json"
            c = self._cand()
            pl.file_candidates([c], surface, led)
            fpath = pl.find_candidate_file(surface, c.fingerprint)
            self.assertIsNotNone(fpath)
            fixed = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
            removed = pl.review_candidate(surface, led, c.fingerprint, "land", now=fixed)
            self.assertTrue(removed)
            self.assertFalse(fpath.exists())  # gone from the top-level surface
            decided_path = surface / "decided" / fpath.name
            self.assertTrue(decided_path.exists())  # NOT deleted — moved
            text = decided_path.read_text(encoding="utf-8")
            self.assertIn("status: landed", text)
            self.assertIn("decided_at: 2026-08-11T14:00:00Z", text)
            # ledger's decided_at matches the archived file's stamp exactly.
            self.assertEqual(
                pl.load_ledger(led)[c.fingerprint]["decided_at"],
                "2026-08-11T14:00:00Z",
            )

    def test_review_decline_moves_to_decided_and_preserves_body(self):
        with TemporaryDirectory() as t:
            surface, led = Path(t) / "cands", Path(t) / "l.json"
            c = self._cand()
            pl.file_candidates([c], surface, led)
            pl.review_candidate(surface, led, c.fingerprint, "decline")
            decided_files = list((surface / "decided").glob("*.md"))
            self.assertEqual(len(decided_files), 1)
            text = decided_files[0].read_text(encoding="utf-8")
            self.assertIn("status: declined", text)
            self.assertIn(c.invariant, text)  # candidate body preserved
            self.assertIn("bit twice", text)  # rationale preserved

    def test_list_pending_excludes_decided_subdir(self):
        with TemporaryDirectory() as t:
            surface, led = Path(t) / "cands", Path(t) / "l.json"
            c = self._cand()
            pl.file_candidates([c], surface, led)
            pl.review_candidate(surface, led, c.fingerprint, "decline")
            self.assertEqual(pl.list_pending(surface), [])
            # a re-sweep still must not re-file the declined candidate.
            self.assertEqual(pl.file_candidates([c], surface, led), [])

    def test_archive_overwrites_idempotently_if_decided_name_pre_exists(self):
        with TemporaryDirectory() as t:
            surface, led = Path(t) / "cands", Path(t) / "l.json"
            c = self._cand()
            pl.file_candidates([c], surface, led)
            fpath = pl.find_candidate_file(surface, c.fingerprint)
            decided_dir = surface / "decided"
            decided_dir.mkdir(parents=True)
            (decided_dir / fpath.name).write_text("stale pre-existing content",
                                                   encoding="utf-8")
            removed = pl.review_candidate(surface, led, c.fingerprint, "land")
            self.assertTrue(removed)
            text = (decided_dir / fpath.name).read_text(encoding="utf-8")
            self.assertIn("status: landed", text)
            self.assertNotIn("stale pre-existing content", text)


if __name__ == "__main__":
    unittest.main()
