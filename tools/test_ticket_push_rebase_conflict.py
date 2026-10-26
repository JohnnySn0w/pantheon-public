"""T-4380: a rebase conflict on ticket push must never strand the shared checkout.

Incident 2026-09-30 ~05:1xZ: `ticket.py resolve T-4326` committed locally, the push
lost a race, and the pre-push freshness hook's `git pull --rebase --autostash`
conflicted in the GENERATED `tickets/README.md` (both sides edited adjacent
rows). The hook exits 1 mid-rebase without aborting; `push_with_retry` called that
"non-retryable" and returned, so the shared checkout was left detached in
"interactive rebase in progress" with `UU tickets/README.md`, and the caller's
board resolver (T-0846) never got a turn.

These tests are real git end-to-end: a bare origin, two clones, and the REAL
`.githooks/pre-push` wired via `core.hooksPath` on the "shared checkout" clone,
driving `ticket._commit_and_push` (the pipeline every ticket op ends in).

Fixture-string discipline: the HERMES trigger bytes never appear in this source.

Run:  python3 -m unittest tools.test_ticket_push_rebase_conflict -v
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import ticket
from tools._git_utils import push_with_retry

HOOKS_DIR = Path(__file__).resolve().parent.parent / ".githooks"

_README_SKELETON = (
    "# Tickets\n\nStatic intro prose.\n\n## Current state\n\n"
    "| ID | State | Title | Requester → Target | Filed |\n|---|---|---|---|---|\n\n"
    "## Footer\n\nStatic footer prose.\n"
)
_SHARD_REL = "ledger/appetite/2026-W40.jsonl"
_BASE_EVENT = {"tid": "T-0000", "outcome": "shipped", "ts": "2026-09-28T00:00:00Z"}


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args!r} failed in {cwd}: {r.stderr}\n{r.stdout}")
    return r


def _ticket_text(tid: str, state: str, title: str, target: str = "orchestrator") -> str:
    return (
        "---\n"
        f"id: {tid}\ncreated: 2026-09-30T05:00:00Z\nrequester: orchestrator\n"
        f"target: {target}\nstate: {state}\nparent_ticket: null\ndepends_on: []\n"
        "---\n\n"
        f"# {title}\n\n## Ask\n\nask\n\n## Context\n\nctx\n\n## Transition log\n\n"
        "- 2026-09-30T05:00:00Z [claude] filed in inbox\n"
    )


def _event(tid: str) -> str:
    return json.dumps({"tid": tid, "outcome": "shipped", "ts": "2026-09-30T05:17:00Z"}, sort_keys=True)


def _regenerates_to_itself(clone: Path) -> bool:
    """True iff the clone's tickets/README.md is already the regenerated form."""
    return ticket._rebuild_readme_table(clone / "tickets", clone / "tickets" / "README.md") is False


class _TwoCloneCase(unittest.TestCase):
    """Bare origin + clone A (the shared checkout: attached to main, real hook)
    + clone B (another persona). Live rows in README order (ID desc):
    T-0004 inbox, T-0003 active, T-0002 inbox, T-0001 active."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.origin = base / "origin.git"
        self.origin.mkdir()
        _git(base, "init", "-q", "--bare", "-b", "main", str(self.origin))

        seed = base / "seed"
        seed.mkdir()
        _git(seed, "init", "-q", "-b", "main")
        _git(seed, "config", "user.email", "seed@example")
        _git(seed, "config", "user.name", "Seed")
        _git(seed, "remote", "add", "origin", str(self.origin))
        for tid, state, title in (
            ("T-0001", "active", "First"),
            ("T-0002", "inbox", "Second"),
            ("T-0003", "active", "Third"),
            ("T-0004", "inbox", "Fourth"),
        ):
            p = seed / "tickets" / state / f"{tid}.md"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_ticket_text(tid, state, title), encoding="utf-8")
        (seed / "tickets" / "done").mkdir(parents=True, exist_ok=True)
        (seed / "tickets" / "done" / ".gitkeep").write_text("", encoding="utf-8")
        (seed / "tickets" / "README.md").write_text(_README_SKELETON, encoding="utf-8")
        ticket._rebuild_readme_table(seed / "tickets", seed / "tickets" / "README.md")
        shard = seed / _SHARD_REL
        shard.parent.mkdir(parents=True, exist_ok=True)
        shard.write_text(json.dumps(_BASE_EVENT, sort_keys=True) + "\n", encoding="utf-8")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "seed")
        _git(seed, "push", "-q", "origin", "main")

        self.a = base / "a"
        self.b = base / "b"
        for clone in (self.a, self.b):
            _git(base, "clone", "-q", str(self.origin), str(clone))
            _git(clone, "config", "user.email", f"{clone.name}@example")
            _git(clone, "config", "user.name", clone.name.upper())
        # The real hook, exactly as the shared checkout has it.
        _git(self.a, "config", "core.hooksPath", str(HOOKS_DIR))
        self.base = base

        p1 = patch.object(ticket, "REPO_ROOT", self.a)
        p2 = patch.object(ticket, "TICKETS_ROOT", self.a / "tickets")
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        self.addCleanup(self._tmp.cleanup)

    # -- persona B: lands first on origin --------------------------------
    def b_claims_t4(self, *, appetite_event: str | None = None) -> str:
        _git(self.b, "mv", "tickets/inbox/T-0004.md", "tickets/active/T-0004.md")
        (self.b / "tickets/active/T-0004.md").write_text(
            _ticket_text("T-0004", "active", "Fourth"), encoding="utf-8"
        )
        if appetite_event is not None:
            with (self.b / _SHARD_REL).open("a", encoding="utf-8") as fh:
                fh.write(appetite_event + "\n")
        ticket._rebuild_readme_table(self.b / "tickets", self.b / "tickets" / "README.md")
        _git(self.b, "add", "-A")
        _git(self.b, "commit", "-q", "-m", "tickets: claim T-0004")
        _git(self.b, "push", "-q", "origin", "main")
        return _git(self.b, "rev-parse", "HEAD").stdout.strip()

    # -- persona A: the stale shared checkout ---------------------------
    def a_resolves_t3(self, *, appetite_event: str | None = None) -> None:
        old = self.a / "tickets/active/T-0003.md"
        new = self.a / "tickets/done/T-0003.md"
        _git(self.a, "mv", "tickets/active/T-0003.md", "tickets/done/T-0003.md")
        new.write_text(_ticket_text("T-0003", "done", "Third"), encoding="utf-8")
        paths = [old, new]
        if appetite_event is not None:
            shard = self.a / _SHARD_REL
            with shard.open("a", encoding="utf-8") as fh:
                fh.write(appetite_event + "\n")
            paths.append(shard)
        ticket._commit_and_push(paths, "tickets: resolve T-0003 — test")

    # -- shard-only shapes (no ticket moves, so the README never conflicts) ----
    def shard_change(self, clone: Path, text: str | None, message: str, *, push: bool) -> str:
        """Commit a whole-file rewrite of the appetite shard (text=None deletes it)."""
        if text is None:
            _git(clone, "rm", "-q", _SHARD_REL)
        else:
            (clone / _SHARD_REL).write_text(text, encoding="utf-8")
            _git(clone, "add", _SHARD_REL)
        _git(clone, "commit", "-q", "-m", message)
        if push:
            _git(clone, "push", "-q", "origin", "main")
        return _git(clone, "rev-parse", "HEAD").stdout.strip()

    def a_pushes(self) -> tuple[bool, str]:
        """Push A's local commit the way every ticket op does: HEAD:main with the board
        resolver, through the real pre-push hook."""
        return push_with_retry(
            self.a, refspec="HEAD:main", conflict_resolver=ticket._board_conflict_resolver
        )

    def origin_tip(self) -> str:
        return _git(self.base, "ls-remote", str(self.origin), "refs/heads/main").stdout.split()[0]

    # -- assertions ------------------------------------------------------
    def assert_a_not_stranded(self) -> None:
        git_dir = self.a / ".git"
        self.assertFalse(
            (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists(),
            "shared checkout must never be left mid-rebase",
        )
        self.assertEqual(_git(self.a, "symbolic-ref", "--short", "HEAD").stdout.strip(), "main",
                         "shared checkout must be back on its branch, not detached")
        self.assertEqual(_git(self.a, "status", "--porcelain").stdout.strip(), "",
                         "no UU / dirty debris may be left behind")

    def origin_clone(self) -> Path:
        v = self.base / "verify"
        _git(self.base, "clone", "-q", str(self.origin), str(v))
        return v


class TestFixtureIsNonVacuous(_TwoCloneCase):
    def test_adjacent_row_edits_really_conflict_in_the_readme_only(self) -> None:
        """Guard the fixture: without this the landing test could pass trivially."""
        self.b_claims_t4()
        old = self.a / "tickets/active/T-0003.md"
        new = self.a / "tickets/done/T-0003.md"
        _git(self.a, "mv", "tickets/active/T-0003.md", "tickets/done/T-0003.md")
        new.write_text(_ticket_text("T-0003", "done", "Third"), encoding="utf-8")
        ticket._commit_and_push([old, new], "tickets: resolve T-0003 — test", no_push=True)
        _git(self.a, "fetch", "-q", "origin", "main")
        r = _git(self.a, "rebase", "origin/main", check=False)
        self.assertNotEqual(r.returncode, 0, "the fixture must produce a real rebase conflict")
        unmerged = _git(self.a, "diff", "--name-only", "--diff-filter=U").stdout.split()
        self.assertEqual(unmerged, ["tickets/README.md"])
        _git(self.a, "rebase", "--abort")

    def test_concurrent_appetite_appends_really_conflict_alongside_the_readme(self) -> None:
        self.b_claims_t4(appetite_event=_event("T-0004"))
        old = self.a / "tickets/active/T-0003.md"
        new = self.a / "tickets/done/T-0003.md"
        _git(self.a, "mv", "tickets/active/T-0003.md", "tickets/done/T-0003.md")
        new.write_text(_ticket_text("T-0003", "done", "Third"), encoding="utf-8")
        shard = self.a / _SHARD_REL
        with shard.open("a", encoding="utf-8") as fh:
            fh.write(_event("T-0003") + "\n")
        ticket._commit_and_push([old, new, shard], "tickets: resolve T-0003 — test", no_push=True)
        _git(self.a, "fetch", "-q", "origin", "main")
        r = _git(self.a, "rebase", "origin/main", check=False)
        self.assertNotEqual(r.returncode, 0)
        unmerged = sorted(_git(self.a, "diff", "--name-only", "--diff-filter=U").stdout.split())
        self.assertEqual(unmerged, sorted(["tickets/README.md", _SHARD_REL]))
        _git(self.a, "rebase", "--abort")


class TestReadmeConflictLands(_TwoCloneCase):
    def test_adjacent_row_readme_conflict_lands_with_regenerated_readme(self) -> None:
        b_sha = self.b_claims_t4()
        self.a_resolves_t3()  # must not raise

        self.assert_a_not_stranded()
        v = self.origin_clone()
        log = _git(v, "log", "--format=%s").stdout
        self.assertIn("tickets: claim T-0004", log)
        self.assertIn("tickets: resolve T-0003", log)
        self.assertTrue(_git(v, "merge-base", "--is-ancestor", b_sha, "HEAD", check=False).returncode == 0,
                        "B's commit must still be in origin history (no clobber)")
        self.assertTrue((v / "tickets/done/T-0003.md").exists())
        self.assertTrue((v / "tickets/active/T-0004.md").exists())
        readme = (v / "tickets" / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("<<<<<<<", readme)
        self.assertNotIn(">>>>>>>", readme)
        row3 = next(l for l in readme.splitlines() if l.startswith("|") and "T-0003" in l)
        row4 = next(l for l in readme.splitlines() if l.startswith("|") and "T-0004" in l)
        self.assertIn("done", row3)
        self.assertIn("active", row4)
        self.assertIn("Static footer prose.", readme)
        self.assertTrue(_regenerates_to_itself(v),
                        "README on origin must equal a fresh regeneration from its own tree")


class TestNonGeneratedConflictAbortsCleanly(_TwoCloneCase):
    def test_real_conflict_aborts_restores_branch_and_names_the_commit(self) -> None:
        # Both sides rewrite the SAME line of the SAME ticket file (target).
        tpath_b = self.b / "tickets/active/T-0001.md"
        tpath_b.write_text(_ticket_text("T-0001", "active", "First", target="mason"), encoding="utf-8")
        ticket._rebuild_readme_table(self.b / "tickets", self.b / "tickets" / "README.md")
        _git(self.b, "add", "-A")
        _git(self.b, "commit", "-q", "-m", "tickets: retarget T-0001 (b)")
        _git(self.b, "push", "-q", "origin", "main")
        origin_tip = _git(self.b, "rev-parse", "HEAD").stdout.strip()

        tpath_a = self.a / "tickets/active/T-0001.md"
        tpath_a.write_text(_ticket_text("T-0001", "active", "First", target="artisan"), encoding="utf-8")
        with self.assertRaises(ticket.TicketError) as ctx:
            ticket._commit_and_push([tpath_a], "tickets: retarget T-0001 (a)")
        msg = str(ctx.exception)

        self.assert_a_not_stranded()
        a_head = _git(self.a, "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(a_head, origin_tip)
        self.assertEqual(
            _git(self.a, "log", "-1", "--format=%s").stdout.strip(), "tickets: retarget T-0001 (a)",
            "the unpushed commit must still be on the branch, untouched",
        )
        a_short = _git(self.a, "rev-parse", "--short", "HEAD").stdout.strip()
        self.assertIn(f"    {a_short} tickets: retarget T-0001 (a)", msg,
                      "the message must list the unpushed commit as a `git log --oneline` line")
        self.assertIn("DID NOT LAND", msg)
        self.assertIn("git push failed", msg, "cmd_file's re-allocation loop keys on this")
        # origin is exactly what B left it as: A landed nothing.
        self.assertEqual(
            _git(self.base, "ls-remote", str(self.origin), "refs/heads/main").stdout.split()[0],
            origin_tip,
        )


class TestOnlyAppetiteShardsAreUnionMerged(_TwoCloneCase):
    def test_conflict_in_any_other_ledger_file_aborts_instead_of_merging(self) -> None:
        """The union rule is scoped to ledger/appetite/*.jsonl. The main ledger (and
        anything else) is a real conflict the resolver must decline, so the rebase
        is aborted and the op reports that it did not land."""
        rel = "ledger/2026-W40/2026-W40.jsonl"
        for clone, line in ((self.b, "from-b"), (self.a, "from-a")):
            f = clone / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(line + "\n", encoding="utf-8")
        _git(self.b, "add", "-A")
        _git(self.b, "commit", "-q", "-m", "ledger: b entry")
        _git(self.b, "push", "-q", "origin", "main")
        origin_tip = _git(self.b, "rev-parse", "HEAD").stdout.strip()

        with self.assertRaises(ticket.TicketError) as ctx:
            ticket._commit_and_push([self.a / rel], "ledger: a entry")

        self.assert_a_not_stranded()
        self.assertIn("DID NOT LAND", str(ctx.exception))
        a_short = _git(self.a, "rev-parse", "--short", "HEAD").stdout.strip()
        self.assertIn(f"    {a_short} ledger: a entry", str(ctx.exception))
        self.assertEqual(
            _git(self.base, "ls-remote", str(self.origin), "refs/heads/main").stdout.split()[0],
            origin_tip,
        )

    def test_a_genuinely_conflicted_lookalike_aborts(self) -> None:
        """A nested `ledger/appetite/<sub>/x.jsonl` is NOT a shard. Both sides create
        it (a real add/add conflict, so all index stages are present): the resolver
        must still decline on the path alone, and the rebase aborts."""
        rel = "ledger/appetite/nested/2026-W40.jsonl"
        for clone, line in ((self.b, "from-b"), (self.a, "from-a")):
            f = clone / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(line + "\n", encoding="utf-8")
        _git(self.b, "add", "-A")
        _git(self.b, "commit", "-q", "-m", "ledger: b nested")
        _git(self.b, "push", "-q", "origin", "main")
        origin_tip = _git(self.b, "rev-parse", "HEAD").stdout.strip()

        with self.assertRaises(ticket.TicketError) as ctx:
            ticket._commit_and_push([self.a / rel], "ledger: a nested")

        self.assert_a_not_stranded()
        self.assertIn("DID NOT LAND", str(ctx.exception))
        self.assertEqual(
            _git(self.base, "ls-remote", str(self.origin), "refs/heads/main").stdout.split()[0],
            origin_tip,
        )

    def test_the_shard_pattern_matches_exactly_the_flat_jsonl_shards(self) -> None:
        rx = ticket._APPETITE_SHARD_RE
        for ok in ("ledger/appetite/2026-W40.jsonl", "ledger/appetite/x.jsonl"):
            with self.subTest(match=ok):
                self.assertTrue(rx.match(ok))
        for bad in (
            "ledger/appetite/nested/2026-W40.jsonl",   # nested
            "ledger/appetite/2026-W40.txt",            # wrong suffix
            "ledger/appetite/2026-W40.jsonl.bak",      # suffix is not the end
            "ledger/appetite/.jsonl",                  # no shard name
            "ledger/appetite/",                        # the directory itself
            "ledger/appetite2/2026-W40.jsonl",         # sibling directory
            "xledger/appetite/2026-W40.jsonl",         # not anchored at the root
            "ledger/2026-W40/2026-W40.jsonl",          # the main ledger
        ):
            with self.subTest(no_match=bad):
                self.assertIsNone(rx.match(bad))

    def test_resolver_declines_sets_holding_a_non_generated_path_before_any_git_call(self) -> None:
        # The cwd does not exist: a single git spawn would raise FileNotFoundError, so
        # returning False proves the subset check ran first.
        missing = self.base / "does-not-exist"
        for paths in (
            [],
            [_SHARD_REL, "tickets/active/T-0001.md"],
            ["tickets/README.md", "tools/ticket.py"],
        ):
            with self.subTest(paths=paths):
                self.assertFalse(ticket._board_conflict_resolver(paths, missing))


class TestAppetiteShardUnion(_TwoCloneCase):
    def test_concurrent_appetite_appends_are_union_merged_upstream_first(self) -> None:
        b_event = _event("T-0004")
        a_event = _event("T-0003")
        self.b_claims_t4(appetite_event=b_event)
        self.a_resolves_t3(appetite_event=a_event)

        self.assert_a_not_stranded()
        v = self.origin_clone()
        lines = (v / _SHARD_REL).read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            lines,
            [json.dumps(_BASE_EVENT, sort_keys=True), b_event, a_event],
            "union keeps every event once; upstream's line first, the replayed commit's second",
        )
        self.assertTrue(_regenerates_to_itself(v))


    def test_event_both_sides_appended_is_not_duplicated_by_the_union(self) -> None:
        """`git merge-file --union` alone would emit E2 and E1 twice here (both
        writers appended the same two events in opposite order); the resolver's
        dedupe keeps each line the union ADDED exactly once, end to end."""
        e1, e2 = _event("T-0005"), _event("T-0006")
        self.b_claims_t4(appetite_event=f"{e1}\n{e2}")
        self.a_resolves_t3(appetite_event=f"{e2}\n{e1}")

        self.assert_a_not_stranded()
        lines = (self.origin_clone() / _SHARD_REL).read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, [json.dumps(_BASE_EVENT, sort_keys=True), e1, e2])


class TestShardConflictShapes(_TwoCloneCase):
    """Shard-only conflicts (no README involvement). The union is for PURE APPENDS
    only: the base's lines must be a prefix of BOTH sides. Anything that rewrites or
    deletes history declines, and the rebase aborts with the op reported un-landed."""

    BASE_LINE = json.dumps(_BASE_EVENT, sort_keys=True)

    def assert_declined_and_aborted(self, ok: bool, msg: str, upstream_tip: str) -> None:
        self.assertFalse(ok, f"a non-append shard conflict must not land; got {msg!r}")
        self.assertIn("DID NOT LAND", msg)
        self.assert_a_not_stranded()
        self.assertEqual(self.origin_tip(), upstream_tip, "origin must be exactly what upstream left")

    def test_pure_appends_on_both_sides_land_upstream_first(self) -> None:
        """The most common real shape: only the shard conflicts (README rows far apart
        or untouched), and the union lands end to end."""
        e_b, e_a = _event("T-0004"), _event("T-0003")
        self.shard_change(self.b, f"{self.BASE_LINE}\n{e_b}\n", "ledger: b event", push=True)
        self.shard_change(self.a, f"{self.BASE_LINE}\n{e_a}\n", "ledger: a event", push=False)

        ok, msg = self.a_pushes()

        self.assertTrue(ok, msg)
        self.assert_a_not_stranded()
        lines = (self.origin_clone() / _SHARD_REL).read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, [self.BASE_LINE, e_b, e_a])

    def test_upstream_deleted_shard_declines_and_aborts(self) -> None:
        """delete/modify: a missing stage must not be read as an empty file, or the
        deleted shard would be resurrected with only the local line in it."""
        tip = self.shard_change(self.b, None, "ledger: b deletes shard", push=True)
        self.shard_change(self.a, f"{self.BASE_LINE}\n{_event('T-0003')}\n", "ledger: a appends", push=False)
        ok, msg = self.a_pushes()
        self.assert_declined_and_aborted(ok, msg, tip)

    def test_locally_deleted_shard_declines_and_aborts(self) -> None:
        """modify/delete: the mirror image of the above."""
        tip = self.shard_change(self.b, f"{self.BASE_LINE}\n{_event('T-0004')}\n", "ledger: b appends", push=True)
        self.shard_change(self.a, None, "ledger: a deletes shard", push=False)
        ok, msg = self.a_pushes()
        self.assert_declined_and_aborted(ok, msg, tip)

    def _empty_the_shard_everywhere(self) -> None:
        """Both clones end up with the shard tracked but EMPTY, so the merge base of
        the next conflict has no lines and the prefix check is vacuously true."""
        self.shard_change(self.b, "", "ledger: b empties shard", push=True)
        _git(self.a, "pull", "-q", "--ff-only", "origin", "main")

    def test_upstream_deleted_empty_shard_still_declines(self) -> None:
        """With an empty base the prefix check cannot object, so the missing-stage
        check itself must decline (else the deleted shard comes back)."""
        self._empty_the_shard_everywhere()
        tip = self.shard_change(self.b, None, "ledger: b deletes shard", push=True)
        self.shard_change(self.a, f"{_event('T-0003')}\n", "ledger: a appends", push=False)
        ok, msg = self.a_pushes()
        self.assert_declined_and_aborted(ok, msg, tip)

    def test_locally_deleted_empty_shard_still_declines(self) -> None:
        self._empty_the_shard_everywhere()
        tip = self.shard_change(self.b, f"{_event('T-0004')}\n", "ledger: b appends", push=True)
        self.shard_change(self.a, None, "ledger: a deletes shard", push=False)
        ok, msg = self.a_pushes()
        self.assert_declined_and_aborted(ok, msg, tip)

    def test_both_sides_editing_the_same_line_declines_and_aborts(self) -> None:
        """Both variants of a rewritten line would otherwise be kept side by side."""
        tip = self.shard_change(self.b, "edited-by-b\n", "ledger: b rewrites line", push=True)
        self.shard_change(self.a, "edited-by-a\n", "ledger: a rewrites line", push=False)
        ok, msg = self.a_pushes()
        self.assert_declined_and_aborted(ok, msg, tip)

    def test_a_local_rewrite_against_an_upstream_append_declines(self) -> None:
        """Only the LOCAL side rewrites history: the prefix check must cover 'theirs'."""
        tip = self.shard_change(self.b, f"{self.BASE_LINE}\n{_event('T-0004')}\n", "ledger: b appends", push=True)
        self.shard_change(self.a, "edited-by-a\n", "ledger: a rewrites line", push=False)
        ok, msg = self.a_pushes()
        self.assert_declined_and_aborted(ok, msg, tip)

    def test_an_upstream_rewrite_against_a_local_append_declines(self) -> None:
        """Only the UPSTREAM side rewrites history: the prefix check must cover 'ours'."""
        tip = self.shard_change(self.b, "edited-by-b\n", "ledger: b rewrites line", push=True)
        self.shard_change(self.a, f"{self.BASE_LINE}\n{_event('T-0003')}\n", "ledger: a appends", push=False)
        ok, msg = self.a_pushes()
        self.assert_declined_and_aborted(ok, msg, tip)


class TestIsPureAppend(unittest.TestCase):
    def test_appends_on_both_sides_are_pure(self) -> None:
        self.assertTrue(ticket._is_pure_append("a\nb\n", "a\nb\nx\n", "a\nb\ny\n"))

    def test_a_base_without_a_trailing_newline_is_still_a_prefix(self) -> None:
        self.assertTrue(ticket._is_pure_append("a", "a\nx\n", "a\ny\n"))

    def test_an_empty_base_is_a_prefix_of_anything(self) -> None:
        self.assertTrue(ticket._is_pure_append("", "x\n", "y\n"))

    def test_a_rewritten_or_dropped_base_line_on_either_side_is_not_an_append(self) -> None:
        self.assertFalse(ticket._is_pure_append("a\nb\n", "a\nB\nx\n", "a\nb\ny\n"))   # ours edits
        self.assertFalse(ticket._is_pure_append("a\nb\n", "a\nb\nx\n", "a\nB\ny\n"))   # theirs edits
        self.assertFalse(ticket._is_pure_append("a\nb\n", "a\n", "a\nb\ny\n"))         # ours truncates
        self.assertFalse(ticket._is_pure_append("a\nb\n", "a\nb\nx\n", ""))            # theirs empties


class TestDedupeAddedLines(unittest.TestCase):
    def test_new_line_added_by_both_sides_is_kept_once_in_first_position(self) -> None:
        self.assertEqual(ticket._dedupe_added_lines(["b\n"], ["b\n", "x\n", "x\n", "z\n"]),
                         ["b\n", "x\n", "z\n"])

    def test_lines_already_in_base_are_never_dropped(self) -> None:
        # Pre-existing repeats are history, not this merge's business.
        self.assertEqual(ticket._dedupe_added_lines(["d\n", "d\n"], ["d\n", "d\n", "n\n"]),
                         ["d\n", "d\n", "n\n"])

    def test_order_is_stable(self) -> None:
        self.assertEqual(ticket._dedupe_added_lines([], ["c\n", "a\n", "b\n", "a\n"]),
                         ["c\n", "a\n", "b\n"])


if __name__ == "__main__":
    unittest.main()
