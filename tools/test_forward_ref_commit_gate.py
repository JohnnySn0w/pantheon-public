#!/usr/bin/env python3
"""Tests for the forward-ref commit gate (T-2404).

Built against REAL throwaway git repos rather than mocked `git ls-files`, for the
same reason test_cited_hash_lint gives: the whole check is a claim about what the
index contains, and mocking git would mean asserting my model of git against my
model of git. The same-commit case in particular only means anything if a real
index is involved.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.forward_ref_gate as gate  # noqa: E402

# The hook git actually runs. Tests that assert WIRING copy this, not the module
# above — a gate that is correct but never invoked is the failure being prevented.
HOOK = REPO_ROOT / ".githooks" / "commit-msg"


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a],
                          capture_output=True, text=True)


class CitationExtraction(unittest.TestCase):
    def test_a_plain_citation_counts(self):
        self.assertEqual(gate.cited_ids("fix the thing (T-2404)"), ["T-2404"])

    def test_refs_and_prose_forms_count(self):
        for text in ("Refs: T-2404", "per T-2404", "T-2404: do it", "see `T-2404`"):
            self.assertIn("T-2404", gate.cited_ids(text), text)

    def test_embedded_tokens_are_NOT_citations(self):
        """False positives here block commits, so the noise gate is load-bearing."""
        for text in ("XT-2300 is not an id", "T-23001abc", "aT-2404"):
            self.assertEqual(gate.cited_ids(text), [], text)

    def test_short_ids_are_ignored(self):
        self.assertEqual(gate.cited_ids("T-240"), [])

    def test_duplicates_collapse_in_order(self):
        self.assertEqual(gate.cited_ids("T-2404 and T-2401 and T-2404"),
                         ["T-2404", "T-2401"])

    def test_git_template_COMMENT_lines_are_ignored(self):
        """git's own commit template quotes branch names and paths in `#` lines;
        a citation there was never written by the author."""
        self.assertEqual(gate.cited_ids("real msg\n# On branch T-2404-wip\n"),
                         [])


class PlaceholderPolicy(unittest.TestCase):
    """Same policy the weekly lint already holds — id-shaped scratch strings."""

    # A NON-EMPTY registry throughout: with an empty one the no-judgement guard
    # short-circuits first and these would pass without exercising the
    # placeholder policy at all.
    REGISTRY = {"T-1000"}

    def test_placeholders_never_trip_the_gate(self):
        self.assertEqual(
            gate.find_forward_refs("staged on T-9999", self.REGISTRY), [])
        self.assertEqual(
            gate.find_forward_refs("used T-0000", self.REGISTRY), [])

    def test_a_real_missing_id_alongside_a_placeholder_still_trips(self):
        """The exemption must not launder its neighbours."""
        self.assertEqual(
            gate.find_forward_refs("T-9999 and T-2404", self.REGISTRY), ["T-2404"])


class AgainstARealIndex(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")

    def _add_ticket(self, state, tid, commit=True):
        d = self.repo / "tickets" / state
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{tid}.md").write_text(f"# {tid}\n")
        _git(self.repo, "add", "-A")
        if commit:
            _git(self.repo, "commit", "-qm", "add ticket")

    def _known(self):
        return gate.tickets_in_tree(cwd=self.repo)

    def test_POSITIVE_CONTROL_a_planted_forward_reference_is_rejected(self):
        """The known-positive for the whole gate. A check whose only demonstrated
        behaviour is 'allows things' is indistinguishable from a broken one."""
        self._add_ticket("inbox", "T-1000")
        self.assertEqual(
            gate.find_forward_refs("cites T-9998 which does not exist", self._known()),
            ["T-9998"])

    def test_an_existing_ticket_is_accepted(self):
        self._add_ticket("inbox", "T-1000")
        self.assertEqual(gate.find_forward_refs("per T-1000", self._known()), [])

    def test_a_ticket_STAGED_IN_THE_SAME_COMMIT_is_accepted(self):
        """The case a HEAD-only check would break: `ticket.py file` writes the
        ticket and commits it with a message naming its own id, so consulting
        HEAD would reject the very workflow that allocates ids correctly."""
        self._add_ticket("inbox", "T-1000")          # registry is non-empty
        self._add_ticket("inbox", "T-1001", commit=False)  # staged, not committed
        self.assertEqual(gate.find_forward_refs("files T-1001", self._known()), [])

    def test_a_ticket_in_ANY_state_dir_is_accepted(self):
        """Tickets move inbox -> active -> done keeping their id; scanning one
        directory would reject every citation of finished work.

        The inbox seed is load-bearing, not scenery: without it a mutation
        narrowing the scan to tickets/inbox/ leaves the registry EMPTY, the
        no-judgement guard short-circuits, and this test passes while the bug is
        present. Caught by mutation — the guard added for the fleet-propagation
        case silently weakened the directory-coverage test beside it.
        """
        for state, tid in (("inbox", "T-1000"), ("active", "T-1002"),
                           ("done", "T-1003")):
            self._add_ticket(state, tid)
        known = self._known()
        self.assertEqual(gate.find_forward_refs("T-1002 and T-1003", known), [])
        self.assertEqual(known, {"T-1000", "T-1002", "T-1003"})

    def test_an_UNSTAGED_ticket_file_does_NOT_count(self):
        """The would-be TREE is the index. A file merely present on disk is not
        part of the commit, so citing it would still publish a dangling ref."""
        self._add_ticket("inbox", "T-1000")  # registry non-empty
        d = self.repo / "tickets" / "inbox"
        (d / "T-1004.md").write_text("# T-1004\n")   # written, never staged
        self.assertEqual(gate.find_forward_refs("T-1004", self._known()), ["T-1004"])

    def test_a_non_ticket_path_does_not_register_as_a_ticket(self):
        d = self.repo / "tickets" / "inbox"
        d.mkdir(parents=True, exist_ok=True)
        (d / "T-1005.md.bak").write_text("x")
        (self.repo / "tickets" / "README.md").write_text("T-1006\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "noise")
        known = self._known()
        self.assertNotIn("T-1005", known)
        self.assertNotIn("T-1006", known)

    def test_outside_a_git_repo_the_registry_reads_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(gate.tickets_in_tree(cwd=d), set())


class NoRegistryMeansNoJudgement(unittest.TestCase):
    """An EMPTY registry means "this tree has no tickets/", not "the ticket does
    not exist". Only the second licenses rejecting anything.

    Without this the gate is fail-CLOSED precisely where it is least wanted:
    this hook is meant to propagate fleet-wide, and in any repo without a
    tickets/ directory EVERY commit citing an id would be rejected, with
    guidance pointing at a registry that is not there.
    """

    def test_an_empty_registry_rejects_NOTHING(self):
        self.assertEqual(gate.find_forward_refs("cites T-9998", set()), [])

    def test_a_NON_empty_registry_still_rejects(self):
        """The control. Without this pair the guard above could be a blanket
        'never reject' and both tests would look the same."""
        self.assertEqual(gate.find_forward_refs("cites T-9998", {"T-1000"}),
                         ["T-9998"])

    def test_git_ACCEPTS_a_citation_in_a_repo_with_no_tickets_dir(self):
        """End-to-end for the propagation case."""
        with tempfile.TemporaryDirectory() as d:
            repo = pathlib.Path(d)
            _git(repo, "init", "-q", "-b", "main")
            _git(repo, "config", "user.email", "t@t")
            _git(repo, "config", "user.name", "t")
            _git(repo, "config", "core.hooksPath", str(HOOK.parent))
            (repo / "f.txt").write_text("x")
            _git(repo, "add", "-A")
            r = _git(repo, "commit", "-m", "unrelated repo, cites T-9998")
            self.assertEqual(r.returncode, 0, r.stderr)


class MainFailsOpen(unittest.TestCase):
    """A gate that cannot run must not block committing. The cost of it being
    absent is one bad citation the weekly lint still catches; the cost of it
    wedging is an agent that cannot commit at all."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_no_argument_exits_0(self):
        self.assertEqual(gate.main([]), 0)

    def test_an_unreadable_message_file_exits_0(self):
        self.assertEqual(gate.main([str(self.d / "nope.txt")]), 0)

    def test_a_clean_message_exits_0(self):
        p = self.d / "msg"
        p.write_text("chore: tidy up\n")
        self.assertEqual(gate.main([str(p)]), 0)


class EndToEndAgainstGitItself(unittest.TestCase):
    """Installed as a real commit-msg hook and driven by `git commit`.

    The unit tests above prove the logic; this proves the WIRING — that git
    invokes it, passes the message path, and honours the exit code. A gate that
    is correct but never invoked is the failure this ticket is preventing.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")
        _git(self.repo, "config", "core.hooksPath", str(HOOK.parent))

    def _commit(self, message, path="f.txt", body="x"):
        (self.repo / path).write_text(body)
        _git(self.repo, "add", "-A")
        return _git(self.repo, "commit", "-m", message)

    def test_git_REJECTS_a_planted_forward_reference(self):
        d = self.repo / "tickets" / "inbox"
        d.mkdir(parents=True, exist_ok=True)
        (d / "T-1000.md").write_text("# T-1000\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "seed registry")
        r = self._commit("do a thing (T-9998)")
        self.assertNotEqual(r.returncode, 0, "git should have refused the commit")
        self.assertIn("forward reference", r.stderr)
        self.assertIn("file the ticket FIRST", r.stderr)
        self.assertEqual(_git(self.repo, "rev-list", "--count", "--all").stdout.strip(),
                         "1", "only the registry seed should exist — the "
                              "rejected commit must not have been created")

    def test_git_ACCEPTS_a_ticket_filed_in_the_SAME_commit(self):
        d = self.repo / "tickets" / "inbox"
        d.mkdir(parents=True, exist_ok=True)
        (d / "T-1007.md").write_text("# T-1007\n")
        r = self._commit("file T-1007")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_git_ACCEPTS_an_ordinary_message(self):
        self.assertEqual(self._commit("chore: tidy").returncode, 0)


if __name__ == "__main__":
    unittest.main()
