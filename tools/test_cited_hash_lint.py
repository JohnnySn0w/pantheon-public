"""Tests for tools/cited_hash_lint.py (T-2272).

Built against a REAL throwaway git repo rather than mocked git output: the whole
lint is a set of claims about reachability, and mocking `git` would mean asserting
that my model of git matches my model of git.
"""
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import cited_hash_lint as chl  # noqa: E402


def _git(repo, *a, **kw):
    return subprocess.run(["git", "-C", str(repo), *a],
                          capture_output=True, text=True, **kw)


class CitationDetection(unittest.TestCase):
    """A token counts only in a CITATION position. Naive hex matching is where the
    noise comes from: the original audit found 81 distinct hex tokens that were
    session ids, UUID fragments and md5 digests, none of them commits."""

    def test_backticks_and_lead_in_words_count(self):
        for text in ("landed `abc1234`", "landed abc1234", "commit abc1234",
                     "resolved @abc1234", "at abc1234"):
            self.assertIn("abc1234", chl.cited_tokens(text), text)

    def test_bare_hex_in_prose_does_not_count(self):
        """This is the noise gate. A hex run in ordinary prose is not a citation,
        and treating it as one is how a lint earns being ignored."""
        self.assertEqual(chl.cited_tokens("the value was abc1234def and then"), [])
        self.assertEqual(chl.cited_tokens("session 0168440d9771ae0c3ab57de9 ended"), [])

    def test_a_long_uuid_is_not_split_into_a_false_hash(self):
        uuid = "c57288d7-0168-440d-9771-ae0c3ab57de9"
        self.assertEqual(chl.cited_tokens(f"session {uuid} wedged"), [])

    def test_short_runs_are_ignored(self):
        self.assertEqual(chl.cited_tokens("landed `abc12`"), [])

    def test_duplicates_collapse_in_order(self):
        self.assertEqual(chl.cited_tokens("`abc1234` and `def5678` and `abc1234`"),
                         ["abc1234", "def5678"])


class ClassificationAgainstARealRepo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self._tmp.name)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / "f").write_text("1")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "one")
        self.on_main = _git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_commit_on_the_ref_is_OK(self):
        self.assertEqual(chl.classify(self.on_main, ref="main", cwd=self.repo),
                         chl.STATUS_OK)

    def test_a_nonexistent_object_is_MISSING_not_a_failure(self):
        self.assertEqual(chl.classify("0" * 40, ref="main", cwd=self.repo),
                         chl.STATUS_MISSING)

    def test_a_commit_on_ANOTHER_branch_is_UNLANDED_not_UNREACHABLE(self):
        """The split that came out of the lint's own first run. A commit on a live
        branch is VERIFIABLE — the reader can reach it — so calling it the same
        thing as an orphaned commit would make the gate fire on every in-flight
        reference, and a noisy gate gets ignored."""
        _git(self.repo, "checkout", "-q", "-b", "side")
        (self.repo / "f").write_text("2")
        _git(self.repo, "commit", "-qam", "two")
        side = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        _git(self.repo, "checkout", "-q", "main")
        self.assertEqual(chl.classify(side, ref="main", cwd=self.repo),
                         chl.STATUS_UNLANDED)

    def test_an_ORPHANED_commit_is_UNREACHABLE(self):
        """The real defect class: written, cited, then rewritten or abandoned, so
        the citation cannot be verified by anyone. This is what a rebase does."""
        _git(self.repo, "checkout", "-q", "-b", "tmp")
        (self.repo / "f").write_text("3")
        _git(self.repo, "commit", "-qam", "three")
        orphan = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "branch", "-qD", "tmp")
        self.assertEqual(chl.classify(orphan, ref="main", cwd=self.repo),
                         chl.STATUS_UNREACHABLE)

    def test_sweep_reports_the_file_and_only_fails_on_unreachable(self):
        _git(self.repo, "checkout", "-q", "-b", "tmp2")
        (self.repo / "f").write_text("4")
        _git(self.repo, "commit", "-qam", "four")
        orphan = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "branch", "-qD", "tmp2")
        t = self.repo / "t.md"
        t.write_text(f"landed `{orphan}` and `{self.on_main}` "
                     f"and session {'a' * 32} ended\n")
        out = chl.sweep([t], ref="main", cwd=self.repo)
        self.assertEqual([tok for _, tok in out[chl.STATUS_UNREACHABLE]], [orphan])
        self.assertEqual(out[chl.STATUS_UNLANDED], [])
        self.assertEqual(out["files_scanned"], 1)


class BatchedClassificationT4529(unittest.TestCase):
    """T-4529: the weekly run timed out at 15min. ~15k cited tokens at 2-3 git
    subprocesses each, on a loaded 4-core host. The sweep now resolves every
    token in one cat-file --batch-check and answers reachability from two
    rev-list sets, so the git-call count is a constant, not a per-token cost."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self._tmp.name)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / "f").write_text("1")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "one")
        self.on_main = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.tree = _git(self.repo, "rev-parse", "HEAD^{tree}").stdout.strip()
        _git(self.repo, "checkout", "-q", "-b", "side")
        (self.repo / "f").write_text("2")
        _git(self.repo, "commit", "-qam", "two")
        self.side = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        _git(self.repo, "checkout", "-q", "-b", "tmp")
        (self.repo / "f").write_text("3")
        _git(self.repo, "commit", "-qam", "three")
        self.orphan = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "branch", "-qD", "tmp")

    def tearDown(self):
        self._tmp.cleanup()

    def test_batch_matches_the_single_token_answers(self):
        expected = {
            self.on_main: chl.STATUS_OK,
            self.on_main[:9]: chl.STATUS_OK,          # short sha resolves
            self.side: chl.STATUS_UNLANDED,
            self.orphan: chl.STATUS_UNREACHABLE,
            "0" * 40: chl.STATUS_MISSING,
            self.tree: chl.STATUS_MISSING,            # an object, but not a commit
        }
        got = chl.classify_many(list(expected), ref="main", cwd=self.repo)
        self.assertEqual(got, expected)

    def test_sweep_git_calls_do_not_scale_with_token_count(self):
        toks = [self.on_main, self.side, self.orphan] + [f"{i:07x}abcdef" for i in range(20)]
        for name in ("a.md", "b.md"):
            (self.repo / name).write_text("".join(f"landed `{t}`\n" for t in toks))
        real_run = subprocess.run
        calls = []

        def counting_run(cmd, *a, **kw):
            if cmd and cmd[0] == "git":
                calls.append(cmd)
            return real_run(cmd, *a, **kw)

        with patch.object(chl.subprocess, "run", side_effect=counting_run):
            out = chl.sweep([self.repo / "a.md", self.repo / "b.md"],
                            ref="main", cwd=self.repo)
        self.assertLessEqual(len(calls), 3, calls)
        # Findings still report per (file, token): the orphan in both files.
        self.assertEqual(sorted(p for p, t in out[chl.STATUS_UNREACHABLE]), ["a.md", "b.md"])
        self.assertEqual(sorted(p for p, t in out[chl.STATUS_UNLANDED]), ["a.md", "b.md"])

    def test_a_commit_kept_only_by_a_PR_head_is_UNLANDED(self):
        """T-4537: squash-merge deletes agent/<ticket>, orphaning review-time
        citations of its head. GitHub keeps refs/pull/<N>/head, which main()
        fetches into refs/pr/*, so the reader can still verify the commit."""
        _git(self.repo, "update-ref", "refs/pr/1", self.orphan)
        self.assertEqual(chl.classify(self.orphan, ref="main", cwd=self.repo),
                         chl.STATUS_UNLANDED)




class BaselinePartitionT2401(unittest.TestCase):
    """T-2401: the weekly run must page on NEW findings only.

    It exits 1 whenever any UNREACHABLE citation exists, and the standing set
    does not shrink — T-2347 audited it and refused to rewrite append-only
    transition logs to make a lint go green. Under T-2237(a) that failure is
    classified URGENT, so the same static number paged urgently every week,
    which teaches the reader to skip urgent pages.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.path = self.d / "baseline.txt"

    def test_a_finding_in_the_baseline_is_STANDING_not_NEW(self):
        new, standing, _ = chl.partition_baseline(
            [("tickets/done/T-1.md", "abc1234")], {("abc1234", "tickets/done/T-1.md")})
        self.assertEqual(new, [])
        self.assertEqual(len(standing), 1)

    def test_an_UNBASELINED_finding_is_NEW(self):
        """The known-positive. A baseline whose only demonstrated behaviour is
        'nothing fails' proves nothing — this is the case that must still fire."""
        new, standing, _ = chl.partition_baseline(
            [("tickets/done/T-2.md", "def5678")], {("abc1234", "tickets/done/T-1.md")})
        self.assertEqual(len(new), 1)
        self.assertEqual(standing, [])

    def test_the_SAME_hash_cited_somewhere_NEW_still_reports(self):
        """Keyed on the pair, not the token: an exemption excuses the exact
        citation it names, never the hash everywhere it later appears."""
        new, _, _ = chl.partition_baseline(
            [("tickets/done/T-9.md", "abc1234")], {("abc1234", "tickets/done/T-1.md")})
        self.assertEqual(len(new), 1)

    def test_a_MOVED_file_loses_its_exemption(self):
        """Fails toward showing a finding rather than hiding one."""
        new, _, stale = chl.partition_baseline(
            [("tickets/archive/T-1.md", "abc1234")],
            {("abc1234", "tickets/done/T-1.md")})
        self.assertEqual(len(new), 1)
        self.assertEqual(stale, [("abc1234", "tickets/done/T-1.md")])

    def test_a_ticket_changing_STATE_keeps_its_exemption(self):
        """T-4537: inbox -> active -> done is the same document, not a move. A
        baselined citation in an open ticket must not re-page when it resolves."""
        new, standing, stale = chl.partition_baseline(
            [("tickets/done/T-1.md", "abc1234")],
            {("abc1234", "tickets/inbox/T-1.md")})
        self.assertEqual(new, [])
        self.assertEqual(len(standing), 1)
        self.assertEqual(stale, [])

    def test_a_repaired_citation_leaves_a_STALE_ROW(self):
        """A dead exemption pre-authorises whatever next occupies that pair, so
        it is reported — but never fatally, or this reintroduces the noise."""
        new, standing, stale = chl.partition_baseline(
            [], {("abc1234", "tickets/done/T-1.md")})
        self.assertEqual((new, standing), ([], []))
        self.assertEqual(stale, [("abc1234", "tickets/done/T-1.md")])

    def test_an_ABSENT_baseline_means_nothing_is_exempt(self):
        """Degrades to the pre-T-2401 behaviour rather than to silence — a
        missing file must not become a blanket suppression."""
        self.assertEqual(chl.load_baseline(self.d / "nope.txt"), set())
        new, _, _ = chl.partition_baseline(
            [("tickets/done/T-1.md", "abc1234")], set())
        self.assertEqual(len(new), 1)

    def test_comments_and_blank_lines_are_ignored(self):
        self.path.write_text(
            "# header\n\nabc1234 tickets/done/T-1.md   # why\n\n# trailing\n")
        self.assertEqual(chl.load_baseline(self.path),
                         {("abc1234", "tickets/done/T-1.md")})

    def test_roundtrip_through_the_written_format(self):
        """The writer and the reader must agree, or --update-baseline produces a
        file that exempts nothing and the next run pages for all of it."""
        findings = [("tickets/done/T-1.md", "abc1234"),
                    ("tickets/done/T-2.md", "def5678")]
        chl._write_baseline(self.path, findings)
        self.assertEqual(chl.load_baseline(self.path),
                         {("abc1234", "tickets/done/T-1.md"),
                          ("def5678", "tickets/done/T-2.md")})
        _, standing, stale = chl.partition_baseline(
            findings, chl.load_baseline(self.path))
        self.assertEqual(len(standing), 2)
        self.assertEqual(stale, [])


class ExitCodeIsTheDeliverableT2401(unittest.TestCase):
    """main()'s EXIT CODE is what the weekly unit reads and what pages the operator.

    Added because a mutation exposed the gap: replacing main()'s `if not new`
    with `if True` — blanket suppression, the exact failure this ticket warns
    about — left every partition_baseline test green. Testing the pure helper
    says nothing about what the unit actually does.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.baseline = self.d / "baseline.txt"

    def _run(self, unreachable, baseline_rows=()):
        chl._write_baseline(self.baseline, list(baseline_rows))
        findings = {
            chl.STATUS_MISSING: [], chl.STATUS_UNLANDED: [],
            chl.STATUS_UNREACHABLE: list(unreachable), "files_scanned": 1,
        }
        with patch.object(chl, "sweep", return_value=findings), \
             patch.object(chl, "_git"):
            return chl.main(["--path", str(self.d), "--baseline", str(self.baseline)])

    def test_a_NEW_finding_still_EXITS_1(self):
        """The known-positive for the whole change. A baseline whose only shown
        behaviour is 'exit 0' would be indistinguishable from a broken gate."""
        self.assertEqual(self._run([("tickets/done/T-1.md", "abc1234")]), 1)

    def test_an_entirely_STANDING_set_EXITS_0(self):
        rows = [("tickets/done/T-1.md", "abc1234")]
        self.assertEqual(self._run(rows, rows), 0)

    def test_ONE_new_finding_among_standing_ones_still_EXITS_1(self):
        """Partial coverage must not read as clean — the regression hides behind
        the standing set otherwise."""
        rows = [("tickets/done/T-1.md", "abc1234")]
        self.assertEqual(
            self._run(rows + [("tickets/done/T-2.md", "def5678")], rows), 1)

    def test_no_findings_at_all_EXITS_0(self):
        self.assertEqual(self._run([]), 0)

    def test_update_baseline_EXITS_0_and_writes_what_it_found(self):
        found = [("tickets/done/T-3.md", "aaa1111")]
        findings = {
            chl.STATUS_MISSING: [], chl.STATUS_UNLANDED: [],
            chl.STATUS_UNREACHABLE: found, "files_scanned": 1,
        }
        with patch.object(chl, "sweep", return_value=findings), \
             patch.object(chl, "_git"):
            rc = chl.main(["--path", str(self.d), "--baseline",
                           str(self.baseline), "--update-baseline"])
        self.assertEqual(rc, 0)
        self.assertEqual(chl.load_baseline(self.baseline),
                         {("aaa1111", "tickets/done/T-3.md")})


class BaselineCannotBeAutoUpdatedT2401(unittest.TestCase):
    """Want 4: a finding must not slide into 'standing' without a reviewable
    commit. --update-baseline exists for the deliberate case; the SCHEDULED unit
    must never carry it, or every new finding self-suppresses on the next fire."""

    def test_the_scheduled_unit_does_not_pass_update_baseline(self):
        unit = (REPO_ROOT / "tools" / "systemd"
                / "pantheon-cited-hash-lint.service")
        if not unit.exists():
            self.skipTest(f"unit file not present at {unit}")
        self.assertNotIn("--update-baseline", unit.read_text())

    def test_the_shipped_baseline_parses_and_is_non_empty(self):
        """A baseline that silently fails to parse would exempt nothing and page
        for everything — the failure would look like a regression, not a typo."""
        loaded = chl.load_baseline(chl.BASELINE_PATH)
        self.assertTrue(loaded, "shipped baseline parsed to zero rows")
        for tok, path in loaded:
            self.assertRegex(tok, r"^[0-9a-f]{7,40}$")
            self.assertTrue(path.endswith(".md"))

if __name__ == "__main__":
    unittest.main()
