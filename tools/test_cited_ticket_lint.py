#!/usr/bin/env python3
"""Tests for cited_ticket_lint (T-2300).

The load-bearing test in here is test_forward_reference_is_caught. Everything else
is scaffolding around it, because the ONLY interesting property of this tool is that
it catches the case an existence check cannot: an id that resolves fine today and
was wrong when written.

There is also a deliberate POSITIVE CONTROL on the end-to-end sweep. A sweep that
reports zero findings is indistinguishable from a sweep that cannot find anything —
the repo currently has no forward references, so a green run over real history is
consistent with the tool being entirely broken. The e2e test therefore builds a real
git repo containing a KNOWN forward reference and requires it to be found.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import cited_ticket_lint as lint  # noqa: E402


class TestCitedIds(unittest.TestCase):
    def test_extracts_ids_in_order_without_duplicates(self):
        self.assertEqual(
            lint.cited_ids("fix T-2300 per T-2272; see T-2300 again"),
            ["T-2300", "T-2272"],
        )

    def test_ignores_ids_embedded_in_longer_tokens(self):
        """`XT-2300` and `T-23001abc` are not citations. A lint that fires on those
        is noise, and a noisy gate gets ignored."""
        self.assertEqual(lint.cited_ids("XT-2300 and T-23001abc and T-9999x"), [])

    def test_requires_at_least_four_digits(self):
        self.assertEqual(lint.cited_ids("T-123 is not a ticket id"), [])


class TestClassify(unittest.TestCase):
    """Ordering is `git rev-list` position: SMALLER index == NEWER commit."""

    def setUp(self):
        #      newest .................. oldest
        self.order = {"c_new": 0, "c_mid": 1, "c_old": 2}

    def test_ok_when_ticket_predates_the_citing_commit(self):
        birth = {"T-1000": "c_old"}
        self.assertEqual(
            lint.classify("T-1000", "c_new", self.order, birth), lint.STATUS_OK)

    def test_ok_when_the_commit_creates_the_ticket_it_names(self):
        """`tickets: file T-2357` adds the very file it cites — equal position."""
        birth = {"T-2357": "c_mid"}
        self.assertEqual(
            lint.classify("T-2357", "c_mid", self.order, birth), lint.STATUS_OK)

    def test_forward_reference_is_caught(self):
        """THE DEFECT. The id exists NOW (it is in `birth`), so any existence check
        passes — but it was created AFTER the commit citing it, so at that moment it
        pointed at nothing, and today it points at somebody else's ticket."""
        birth = {"T-2356": "c_new"}
        self.assertEqual(
            lint.classify("T-2356", "c_old", self.order, birth),
            lint.STATUS_FORWARD,
            "a citation to a ticket created later must not pass merely because the "
            "id resolves today",
        )

    def test_placeholder_ids_are_info_not_failure(self):
        """`T-9999` is what people type for a throwaway ("verified on a throwaway
        T-9999 stage"), not a citation. Failing on it is the noise that gets a gate
        ignored — the exact failure mode T-2272 documents."""
        self.assertEqual(
            lint.classify("T-9999", "c_new", self.order, {}),
            lint.STATUS_PLACEHOLDER)

    def test_placeholder_exemption_cannot_hide_a_forward_reference(self):
        """The exemption is only reachable when the id resolves to NO ticket. If a
        real T-9999 is ever allocated it must be judged on ancestry like any other."""
        birth = {"T-9999": "c_new"}
        self.assertEqual(
            lint.classify("T-9999", "c_old", self.order, birth),
            lint.STATUS_FORWARD)

    def test_unallocated_when_no_such_ticket_ever(self):
        self.assertEqual(
            lint.classify("T-9998", "c_new", self.order, {}), lint.STATUS_UNALLOCATED)

    def test_unorderable_commit_does_not_cry_wolf(self):
        """A commit that is not on the ref cannot be ordered. Guessing there would
        make every in-flight branch reference a finding."""
        birth = {"T-1000": "c_old"}
        self.assertEqual(
            lint.classify("T-1000", "not_on_ref", self.order, birth), lint.STATUS_OK)


class TestSweepEndToEnd(unittest.TestCase):
    """Positive control: a real repo with a KNOWN forward reference."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name)
        env = {**os.environ,
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        self.env = env
        self._run("git", "init", "-q", "-b", "main")
        (self.repo / "tickets" / "done").mkdir(parents=True)

        # Commit 1 cites T-0002, which does not exist yet -> FORWARD once it does.
        (self.repo / "tickets" / "done" / "T-0001.md").write_text("first\n")
        self._run("git", "add", "-A")
        self._run("git", "commit", "-q", "-m", "work on T-0001, follow-up is T-0002")

        # Commit 2 creates T-0002 — retroactively making commit 1 a forward ref.
        (self.repo / "tickets" / "done" / "T-0002.md").write_text("second\n")
        self._run("git", "add", "-A")
        self._run("git", "commit", "-q", "-m", "tickets: file T-0002")

        # Commit 3 cites an id that never exists at all.
        (self.repo / "f.txt").write_text("x\n")
        self._run("git", "add", "-A")
        self._run("git", "commit", "-q", "-m", "unrelated, refs T-7777")

    def _run(self, *args):
        r = subprocess.run(args, cwd=self.repo, env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_finds_the_planted_forward_reference_and_the_unallocated_id(self):
        f = lint.sweep(ref="main", limit=50, cwd=self.repo)

        self.assertEqual(f["commits_scanned"], 3)
        self.assertEqual(f["tickets_known"], 2, "both ticket files must be seen")

        fwd_ids = [cid for _sha, cid, _s in f[lint.STATUS_FORWARD]]
        self.assertIn("T-0002", fwd_ids,
                      "the planted forward reference was NOT found — a green run "
                      "over real history would therefore have meant nothing")

        unalloc_ids = [cid for _sha, cid, _s in f[lint.STATUS_UNALLOCATED]]
        self.assertIn("T-7777", unalloc_ids)

        # The commit that CREATES T-0002 while naming it must not be flagged.
        self.assertNotIn(
            "tickets: file T-0002",
            [subj for _sha, _cid, subj in f[lint.STATUS_FORWARD]],
            "filing a ticket cites its own new id and is correct, not a defect",
        )

    def test_baseline_suppresses_only_the_exact_audited_citation(self):
        """The baseline exists because commit messages are immutable. It must be
        incapable of hiding anything but the specific rows audited into it — a
        baseline that generalises is a mute button."""
        f_all = lint.sweep(ref="main", limit=50, cwd=self.repo, baseline=set())
        sha, cid, _ = f_all[lint.STATUS_FORWARD][0]
        self.assertEqual(cid, "T-0002")

        # Exact (sha, id) -> baselined, not a failure, and still reported.
        f = lint.sweep(ref="main", limit=50, cwd=self.repo, baseline={(sha, cid)})
        self.assertEqual(f[lint.STATUS_FORWARD], [])
        self.assertEqual([c for _s, c, _x in f[lint.STATUS_BASELINED]], ["T-0002"],
                         "a baselined defect must stay VISIBLE, not vanish")

        # Same id, different sha -> NOT suppressed. A new commit inherits nothing.
        f2 = lint.sweep(ref="main", limit=50, cwd=self.repo,
                        baseline={("0000000ff", cid)})
        self.assertEqual([c for _s, c, _x in f2[lint.STATUS_FORWARD]], ["T-0002"],
                         "baseline keyed on the id alone would let every future "
                         "forward reference to that id through")

    def test_clean_repo_reports_clean(self):
        """Denominator check: the tool must be capable of returning zero, so that a
        zero on real history is a real result rather than a broken code path."""
        self._run("git", "commit", "-q", "--allow-empty", "-m", "no ids here")
        f = lint.sweep(ref="main", limit=1, cwd=self.repo)
        self.assertEqual(f[lint.STATUS_FORWARD], [])
        self.assertEqual(f[lint.STATUS_UNALLOCATED], [])
        self.assertEqual(f["commits_scanned"], 1)


if __name__ == "__main__":
    unittest.main()
