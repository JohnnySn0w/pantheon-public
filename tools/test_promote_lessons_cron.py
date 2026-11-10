"""promote_lessons_cron tests (T-0649 inc 4): digest text, the run() orchestration
(filing + commit/send seams), and the scoped HERMES commit guard. No live git or
Telegram — the commit/send sites are injected stubs."""

from __future__ import annotations

import os
import subprocess
import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

os.environ.setdefault("PANTHEON_DISABLE_LIVE_MAILBOX", "1")  # never the live hub (T-6370)

from tools import promote_lessons as pl
from tools import promote_lessons_cron as cron


def _cand(source="feedback_x.md", invariant="never do X with `state:`"):
    return pl.Candidate(
        source=source, corpus="memory", title="T", invariant=invariant,
        check_kind="pre-commit", draft="d", rationale="bit twice", confidence="high",
    )


def _args(tmp: Path, **over):
    base = dict(
        lessons_dir=tmp / "lessons", bugs_dir=tmp / "bugs", memory_dir=tmp / "mem",
        candidates_dir=tmp / "cands", ledger=tmp / "ledger.json",
        min_confidence="high", no_commit=False, no_push=False, no_digest=True,
        dry_run=False,
    )
    base.update(over)
    return Namespace(**base)


class BuildDigest(unittest.TestCase):
    def test_zero_pending(self):
        d = cron.build_digest(0, 0, [], committed=True)
        self.assertIn("0 promotion candidate(s)", d)
        self.assertIn("Nothing to review", d)

    def test_pending_listed_with_review_hint(self):
        pend = [{"confidence": "high", "fingerprint": "a.md:1"}]
        d = cron.build_digest(1, 1, pend, committed=True)
        self.assertIn("a.md:1", d)
        self.assertIn("--list-pending", d)

    def test_uncommitted_warns(self):
        d = cron.build_digest(1, 1, [{"fingerprint": "a.md:1"}],
                              committed=False, commit_note="push failed: boom")
        self.assertIn("NOT committed", d)
        self.assertIn("boom", d)


class Run(unittest.TestCase):
    def test_dry_run_touches_nothing(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            commit, send = mock.Mock(), mock.Mock()
            with mock.patch.object(pl, "scan_all", return_value=[_cand()]):
                rc = cron.run(_args(tmp, dry_run=True), do_commit=commit, do_send=send)
            self.assertEqual(rc, 0)
            commit.assert_not_called()
            send.assert_not_called()
            self.assertFalse((tmp / "cands").exists())  # nothing filed
            self.assertFalse((tmp / "ledger.json").exists())

    def test_no_commit_files_to_surface_only(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            commit = mock.Mock()
            with mock.patch.object(pl, "scan_all", return_value=[_cand()]):
                rc = cron.run(_args(tmp, no_commit=True), do_commit=commit)
            self.assertEqual(rc, 0)
            commit.assert_not_called()
            self.assertTrue(list((tmp / "cands").glob("*.md")))  # filed
            self.assertTrue((tmp / "ledger.json").exists())

    def test_commit_and_send_invoked(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            commit = mock.Mock(return_value=(True, "committed + pushed"))
            send = mock.Mock(return_value=True)
            with mock.patch.object(pl, "scan_all", return_value=[_cand()]):
                rc = cron.run(_args(tmp, no_digest=False), do_commit=commit, do_send=send)
            self.assertEqual(rc, 0)
            commit.assert_called_once()
            send.assert_called_once()
            # the digest (3rd positional to send) names the new candidate count
            (text,) = send.call_args.args
            self.assertIn("1 new this run", text)

    def test_commit_failure_returns_nonzero_but_still_digests(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            commit = mock.Mock(return_value=(False, "push failed: boom"))
            send = mock.Mock(return_value=True)
            with mock.patch.object(pl, "scan_all", return_value=[_cand()]):
                rc = cron.run(_args(tmp, no_digest=False), do_commit=commit, do_send=send)
            self.assertEqual(rc, 8)
            send.assert_called_once()
            (text,) = send.call_args.args
            self.assertIn("NOT committed", text)

    def test_zero_candidates_still_sends_proof_of_life(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            send = mock.Mock(return_value="id")
            with mock.patch.object(pl, "scan_all", return_value=[]):
                rc = cron.run(_args(tmp, no_digest=False), do_commit=mock.Mock(), do_send=send)
            self.assertEqual(rc, 0)
            send.assert_called_once()
            self.assertIn("0 new this run", send.call_args.args[0])

    def test_owner_send_failure_is_loud_and_never_masks_a_commit_failure(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            boom = mock.Mock(side_effect=PermissionError("hub"))
            with mock.patch.object(pl, "scan_all", return_value=[_cand()]):
                rc = cron.run(_args(tmp, no_digest=False),
                              do_commit=mock.Mock(return_value=(True, "ok")), do_send=boom)
            self.assertEqual(rc, cron.EXIT_DIGEST_FAILED)
        with TemporaryDirectory() as t:
            tmp = Path(t)
            with mock.patch.object(pl, "scan_all", return_value=[_cand()]):
                rc = cron.run(_args(tmp, no_digest=False),
                              do_commit=mock.Mock(return_value=(False, "push failed")), do_send=boom)
            self.assertEqual(rc, 8)

    def test_default_sender_is_the_owner_mailbox_not_telegram(self):
        self.assertNotIn("424242424", Path(cron.__file__).read_text())
        with TemporaryDirectory() as t:
            tmp = Path(t)
            with mock.patch.object(pl, "scan_all", return_value=[]), \
                    mock.patch.object(cron, "send_owner_digest") as owner:
                cron.run(_args(tmp, no_digest=False), do_commit=mock.Mock())
            owner.assert_called_once()
            self.assertEqual(owner.call_args.kwargs["sender"], "promote-lessons")

    def test_no_push_does_not_commit_or_push(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            commit = mock.Mock()
            with mock.patch.object(pl, "scan_all", return_value=[_cand()]):
                rc = cron.run(_args(tmp, no_push=True), do_commit=commit)
            self.assertEqual(rc, 0)
            commit.assert_not_called()  # --no-push short-circuits before do_commit


class CommitSurfaceGuard(unittest.TestCase):
    def test_hermes_guard_refuses_a_staged_candidate(self):
        # Post-T-1088, commit_surface delegates to commit_scoped, which HERMES-
        # scans the ACTUALLY-STAGED files. So the trigger must live in a candidate
        # STAGED under promotion-candidates/ in a real repo (with a bare origin so
        # the pre-commit fetch+ff-merge succeeds). The guard must refuse before any
        # commit lands.
        trigger = "HERME" + "S.md"  # built at runtime; never in source bytes
        with TemporaryDirectory() as t:
            base = Path(t)
            repo = base / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, capture_output=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=repo, capture_output=True)
            bare = base / "origin.git"
            bare.mkdir()
            subprocess.run(["git", "init", "--bare"], cwd=bare, capture_output=True, check=True)
            subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, capture_output=True)
            (repo / cron.SURFACE_REL).mkdir()
            (repo / cron.SURFACE_REL / ".keep").write_text("", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
            subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, capture_output=True, check=True)
            subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, capture_output=True, check=True)

            bad = repo / cron.SURFACE_REL / "cand.md"
            bad.write_text(f"see {trigger} for details\n", encoding="utf-8")
            ok, detail = cron.commit_surface([bad], "promote: candidate", cwd=repo)
            self.assertFalse(ok)
            self.assertIn("HERMES guard refused", detail)
            # No commit landed beyond the seed; staging reset.
            log = subprocess.run(["git", "log", "--oneline"], cwd=repo,
                                 capture_output=True, text=True).stdout
            self.assertEqual(log.strip().count("\n"), 0, f"only seed commit expected: {log!r}")
            staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo,
                                    capture_output=True, text=True).stdout.strip()
            self.assertEqual(staged, "", "staging must be reset after HERMES refusal")


if __name__ == "__main__":
    unittest.main()
