"""T-4680: `ticket.py resolve` refuses a `sop:<stem>` ticket while
sop/standard/<stem>.md is still a STUB that did not change in the ticket's arc
(writing-sops-from-first-use.md step 3), unless `--sop-updated <sha>` names a
landed commit that touched it or `--sop-no-change '<why>'` waives it.

Real git in a tmp repo (the arc check reads `git log`), no network, no push.

Run: python3 -m pytest tools/test_ticket_sop_stub_gate.py
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import _git_utils, ticket

STUB = """# Repository extraction

**Status:** STUB, not yet run (test fixture).

## When to use this

- extracting a repo.

## First run: record the actual procedure here

(empty)
"""

FILLED = STUB.replace("**Status:** STUB, not yet run", "**Status:** Adopted 2026-11-02").replace(
    "(empty)", "1. did the thing.")

MIDLINE = STUB.replace(
    "**Status:** STUB, not yet run (test fixture).",
    "**Status:** Ratified 2026-10-07. The live part is a STUB, not yet run.")

TID = "T-9100"


def _ticket_text(tid: str, labels: list[str]) -> str:
    return "\n".join([
        "---", f"id: {tid}", "created: 2026-10-01T00:00:00Z", "requester: orchestrator",
        "target: role:engineering_orchestrator", "state: active",
        "parent_ticket: null", "depends_on: []", f"labels: [{', '.join(labels)}]",
        "---", "", f"# {tid} title", "", "## Ask", "", "## Context", "",
        "## Transition log", "", "- 2026-10-01T00:00:00Z [orchestrator] filed in inbox", "",
    ])


class SopStubGate(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        ident = {f"GIT_{w}_{k}": v for w in ("AUTHOR", "COMMITTER")
                 for k, v in (("NAME", "t"), ("EMAIL", "t@example.invalid"))}
        p = patch.dict(os.environ, ident)
        p.start()
        self.addCleanup(p.stop)
        for s in ticket.STATES:
            (self.root / "tickets" / s).mkdir(parents=True)
        (self.root / "tickets" / "README.md").write_text(
            "# Tickets\n\n## Current state\n\n| stale |\n\n## After\n", encoding="utf-8")
        (self.root / "repos.yaml").write_text("repos: []\n", encoding="utf-8")
        self.sop_dir = self.root / "sop" / "standard"
        self.sop_dir.mkdir(parents=True)
        self.sop = self.sop_dir / "repository-extraction.md"
        self.sop.write_text(STUB, encoding="utf-8")
        for target, val in [("REPO_ROOT", self.root), ("TICKETS_ROOT", self.root / "tickets")]:
            p = patch.object(ticket, target, val)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(_git_utils, "REPO_ROOT", self.root)
        p.start()
        self.addCleanup(p.stop)
        self._run("init", "-q", "-b", "main")
        self._run("config", "commit.gpgsign", "false")
        self._add_ticket(TID, ["build", "sop:repository-extraction"], commit=False)
        self._run("add", "-A")
        self._run("commit", "-q", "-m", "fixture: stub lands (T-4680 test)")

        def real_git(*args, cwd=None):
            return subprocess.run(["git", *args], cwd=cwd or self.root,
                                  capture_output=True, text=True, check=False)

        for name, fake in [("_git", real_git), ("_pull_if_needed", lambda *a, **k: None),
                           ("_record_appetite_outcome", lambda *a, **k: None)]:
            p = patch.object(ticket, name, fake)
            p.start()
            self.addCleanup(p.stop)

    # helpers -------------------------------------------------------------
    def _run(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True,
                              text=True, check=True).stdout

    def _add_ticket(self, tid: str, labels: list[str], commit: bool = True) -> None:
        (self.root / "tickets" / "active" / f"{tid}.md").write_text(
            _ticket_text(tid, labels), encoding="utf-8")
        if commit and (self.root / ".git").exists():
            self._run("add", "-A")
            self._run("commit", "-q", "--allow-empty", "-m", f"file {tid}")

    def _commit_sop(self, text: str, message: str, path: Path | None = None) -> str:
        (path or self.sop).write_text(text, encoding="utf-8")
        self._run("add", "-A")
        self._run("commit", "-q", "-m", message)
        return self._run("rev-parse", "HEAD").strip()

    def _resolve(self, tid: str = TID, *extra: str) -> int:
        args = ticket._build_parser().parse_args(["resolve", tid, "--no-push", *extra])
        return args.func(args)

    def _done(self, tid: str = TID) -> Path:
        return self.root / "tickets" / "done" / f"{tid}.md"

    def _assert_refused(self, *extra: str, tid: str = TID) -> str:
        with self.assertRaises(ticket.SopStubNotFilled) as cm:
            self._resolve(tid, *extra)
        self.assertEqual(cm.exception.exit_code, ticket.EXIT_REFUSAL)
        self.assertTrue((self.root / "tickets" / "active" / f"{tid}.md").exists())
        self.assertFalse(self._done(tid).exists())
        return str(cm.exception)

    # 1 -------------------------------------------------------------------
    def test_stub_without_arc_change_is_refused_with_first_run_pointer(self) -> None:
        msg = self._assert_refused()
        self.assertIn("sop:repository-extraction", msg)
        self.assertIn("still a STUB", msg)
        self.assertIn("sop/standard/repository-extraction.md:9", msg)
        self.assertIn("## First run", msg)
        self.assertIn("--sop-no-change", msg)
        self.assertIn("--sop-updated", msg)

    def test_refusal_exits_three_through_main(self) -> None:
        with patch("sys.stderr"):
            rc = ticket.main(["resolve", TID, "--no-push"])
        self.assertEqual(rc, 3)

    # 2 -------------------------------------------------------------------
    def test_passes_after_an_arc_commit_edits_the_stub(self) -> None:
        self._commit_sop(STUB + "\nfirst-run notes\n", f"{TID}: fill first run")
        self.assertEqual(self._resolve(), 0)
        self.assertTrue(self._done().exists())

    def test_passes_once_the_stub_is_filled_and_flipped(self) -> None:
        self._commit_sop(FILLED, "unrelated commit, no id")
        self.assertEqual(self._resolve(), 0)

    # 3 -------------------------------------------------------------------
    def test_lookalike_ticket_ids_do_not_count_as_the_arc(self) -> None:
        self._commit_sop(STUB + "\na\n", "T-91000: longer id")
        self._commit_sop(STUB + "\nb\n", "T-910: prefix id")
        self._commit_sop(STUB + "\nc\n", "T-9100-x: suffixed token")
        self._assert_refused()

    def test_arc_commit_not_touching_the_sop_does_not_count(self) -> None:
        (self.root / "other.txt").write_text("x\n", encoding="utf-8")
        self._run("add", "-A")
        self._run("commit", "-q", "-m", f"{TID}: unrelated file")
        self._assert_refused()

    # 4/5 -----------------------------------------------------------------
    def test_waiver_passes_and_is_recorded_in_the_resolve_note(self) -> None:
        self.assertEqual(self._resolve(TID, "--with", "shipped",
                                       "--sop-no-change", "run matched the invariants"), 0)
        text = self._done().read_text(encoding="utf-8")
        self.assertIn("shipped; sop-no-change(repository-extraction): run matched the invariants",
                      text)

    def test_empty_or_whitespace_waiver_is_refused(self) -> None:
        for why in ("", "   ", "\t\n"):
            with self.subTest(why=why):
                msg = self._assert_refused("--sop-no-change", why)
                self.assertIn("non-empty reason", msg)

    # 6 -------------------------------------------------------------------
    def test_non_stub_sop_is_ignored(self) -> None:
        self.sop.write_text(FILLED, encoding="utf-8")
        self.assertEqual(self._resolve(), 0)

    def test_midline_stub_in_a_ratified_status_is_not_a_stub(self) -> None:
        self.sop.write_text(MIDLINE, encoding="utf-8")
        self.assertEqual(self._resolve(), 0)

    # 7 -------------------------------------------------------------------
    def test_no_sop_label_returns_before_any_git_call(self) -> None:
        """No sop: label costs one extra ticket read and NO git or subprocess
        call (regression pin for the shared resolve path)."""
        self._add_ticket("T-9200", ["build", "sopx", "not-sop:x"])
        with patch("subprocess.run") as run, patch.object(ticket, "_sop_git") as sg:
            self.assertEqual(ticket._refuse_resolve_if_sop_stub("T-9200", None, None), [])
        run.assert_not_called()
        sg.assert_not_called()

    def test_no_sop_label_resolves(self) -> None:
        self._add_ticket("T-9200", ["build"])
        self.assertEqual(self._resolve("T-9200"), 0)

    # 8/9 -----------------------------------------------------------------
    def test_missing_sop_file_fails_closed(self) -> None:
        self._add_ticket("T-9300", ["sop:no-such-sop"])
        msg = self._assert_refused(tid="T-9300")
        self.assertIn("sop/standard/no-such-sop.md does not exist", msg)

    def test_missing_sop_file_with_waiver_passes(self) -> None:
        self._add_ticket("T-9300", ["sop:no-such-sop"])
        self.assertEqual(self._resolve("T-9300", "--sop-no-change", "label was speculative"), 0)
        self.assertIn("sop-no-change(no-such-sop): label was speculative",
                      self._done("T-9300").read_text(encoding="utf-8"))

    # 10/11 ---------------------------------------------------------------
    def test_sop_updated_with_a_touching_landed_sha_passes_and_is_recorded(self) -> None:
        sha = self._commit_sop(STUB + "\nnotes\n", "fill (squash, no id)")
        self.assertEqual(self._resolve(TID, "--sop-updated", sha[:12]), 0)
        self.assertIn(f"sop-updated(repository-extraction): {sha[:10]}",
                      self._done().read_text(encoding="utf-8"))

    def test_sop_updated_with_a_sha_not_touching_the_file_is_refused(self) -> None:
        (self.root / "other.txt").write_text("x\n", encoding="utf-8")
        self._run("add", "-A")
        self._run("commit", "-q", "-m", "other")
        sha = self._run("rev-parse", "HEAD").strip()
        self.assertIn("does not touch", self._assert_refused("--sop-updated", sha))

    def test_sop_updated_with_an_unknown_sha_is_refused(self) -> None:
        self.assertIn("is not a commit", self._assert_refused("--sop-updated", "deadbeef" * 5))

    def test_sop_updated_with_an_unmerged_sha_is_refused(self) -> None:
        self._run("checkout", "-q", "-b", "agent/T-9100")
        sha = self._commit_sop(STUB + "\nbranch notes\n", "fill on branch")
        self._run("checkout", "-q", "main")
        msg = self._assert_refused("--sop-updated", sha)
        self.assertIn("resolve after the merge lands; the squash sha is the one to cite",
                      msg.lower())

    # 12/13/14/15 ---------------------------------------------------------
    def test_two_sop_labels_one_filled_one_stub_refuses_naming_the_stub(self) -> None:
        (self.sop_dir / "assembly-update.md").write_text(FILLED, encoding="utf-8")
        self._add_ticket("T-9400", ["sop:assembly-update", "sop:repository-extraction"])
        msg = self._assert_refused(tid="T-9400")
        self.assertIn("sop:repository-extraction", msg)
        self.assertNotIn("sop:assembly-update", msg)

    def test_invalid_or_traversal_stem_is_refused(self) -> None:
        for stem in ("../../etc/passwd", "Upper", ""):
            with self.subTest(stem=stem):
                self._add_ticket("T-9500", [f"sop:{stem}"])
                self.assertIn("not a valid SOP file stem", self._assert_refused(tid="T-9500"))

    def test_sop_updated_and_waiver_are_mutually_exclusive(self) -> None:
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            ticket._build_parser().parse_args(
                ["resolve", TID, "--sop-updated", "abc", "--sop-no-change", "why"])

    def test_squash_commit_with_the_id_only_in_the_body_counts(self) -> None:
        self._commit_sop(STUB + "\nsquash\n",
                         f"Fill repository-extraction first run (#1500)\n\nRefs {TID}.")
        self.assertEqual(self._resolve(), 0)

    # review MED-1: the commit that CREATES the stub is not a fill ---------
    def _fresh_stub_ticket(self, tid: str = "T-9600") -> Path:
        self._add_ticket(tid, ["sop:fresh-op"])
        return self.sop_dir / "fresh-op.md"

    def test_stub_created_in_a_commit_naming_the_ticket_is_still_refused(self) -> None:
        path = self._fresh_stub_ticket()
        self._commit_sop(STUB, "T-9600: stub the SOP (sweep also lists T-9100)", path=path)
        self._assert_refused(tid="T-9600")

    def test_later_modifying_commit_naming_the_ticket_passes(self) -> None:
        path = self._fresh_stub_ticket()
        self._commit_sop(STUB, "T-9600: stub the SOP", path=path)
        self._commit_sop(STUB + "\nfirst-run notes\n", "T-9600: fill first run", path=path)
        self.assertEqual(self._resolve("T-9600"), 0)

    def test_sop_updated_with_a_sha_that_only_adds_the_file_is_refused(self) -> None:
        path = self._fresh_stub_ticket()
        sha = self._commit_sop(STUB, "add stub", path=path)
        self.assertIn("only adds", self._assert_refused("--sop-updated", sha, tid="T-9600"))

    # nit: a merge commit lists no files in diff-tree ----------------------
    def test_sop_updated_with_a_merge_commit_points_at_the_squash_or_underlying(self) -> None:
        self._run("checkout", "-q", "-b", "side")
        self._commit_sop(STUB + "\nside notes\n", "fill on side")
        self._run("checkout", "-q", "main")
        (self.root / "other.txt").write_text("x\n", encoding="utf-8")
        self._run("add", "-A")
        self._run("commit", "-q", "-m", "main moves")
        self._run("merge", "-q", "--no-ff", "--no-edit", "side")
        merge = self._run("rev-parse", "HEAD").strip()
        msg = self._assert_refused("--sop-updated", merge)
        self.assertIn("merge commit", msg)
        self.assertIn("squash commit, or the underlying commit", msg)

    # review MED-2: "Rejected:" resolves are exempt (T-0641) ---------------
    def test_rejected_resolve_on_a_stub_ticket_passes(self) -> None:
        self.assertEqual(self._resolve(TID, "--with", "Rejected: artifact declined"), 0)
        self.assertTrue(self._done().exists())

    def test_rejected_later_in_the_note_is_not_exempt(self) -> None:
        self._assert_refused("--with", "Shipped. Rejected: one sub-option")

    # review LOW-3: undecodable SOP fails closed ----------------------------
    def test_undecodable_sop_is_refused_not_a_traceback(self) -> None:
        self.sop.write_bytes(b"**Status:** \xff\xfe STUB\n")
        msg = self._assert_refused()
        self.assertIn("could not be read as UTF-8", msg)

    def test_undecodable_sop_with_waiver_passes(self) -> None:
        self.sop.write_bytes(b"\xff\xfe\x00")
        self.assertEqual(self._resolve(TID, "--sop-no-change", "file is being repaired"), 0)


class SopStubDetection(unittest.TestCase):
    """Review LOW-4: STUB detection on the status line forms, outside fences."""

    def _stub(self, text: str) -> bool:
        return ticket._sop_stub_status(text)[0]

    def test_fenced_stub_example_above_a_real_adopted_status(self) -> None:
        text = "# X\n\n```\n**Status:** STUB, example\n```\n\n**Status:** Adopted\n"
        self.assertFalse(self._stub(text))

    def test_fenced_adopted_example_above_a_real_stub_status(self) -> None:
        text = "# X\n\n```md\n**Status:** Adopted\n```\n\n**Status:** STUB, not yet run\n"
        self.assertTrue(self._stub(text))

    def test_frontmatter_status_stub(self) -> None:
        self.assertTrue(self._stub("---\ntitle: x\nstatus: STUB\n---\n# X\n"))

    def test_frontmatter_status_wins_over_a_later_body_status(self) -> None:
        self.assertFalse(self._stub("---\nstatus: adopted\n---\n**Status:** STUB\n"))

    def test_bold_status_with_colon_outside(self) -> None:
        self.assertTrue(self._stub("# X\n\n**Status**: STUB, not yet run\n"))

    def test_lowercase_and_bolded_stub(self) -> None:
        self.assertTrue(self._stub("**Status:** stub, not yet run\n"))
        self.assertTrue(self._stub("**Status:** **STUB**, not yet run\n"))

    def test_no_status_line_is_not_a_stub(self) -> None:
        self.assertFalse(self._stub("# X\n\nSTUB, not yet run\n"))

    def test_stub_must_be_a_whole_word(self) -> None:
        self.assertFalse(self._stub("**Status:** Stubborn draft\n"))

    def test_first_run_heading_inside_a_fence_is_ignored(self) -> None:
        text = "**Status:** STUB\n```\n## First run example\n```\n## First run: here\n"
        self.assertEqual(ticket._sop_stub_status(text), (True, 5))


if __name__ == "__main__":
    unittest.main()
