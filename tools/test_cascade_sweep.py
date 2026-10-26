"""Tests for tools/cascade_sweep.py -- the level-triggered ORG-013 cascade sweep (T-4186).

Run from repo root:
    python3 -m unittest tools.test_cascade_sweep -v

WHY THE TOOL EXISTS. `ticket.py resolve` cascades on the resolving seat, but a ticket that reaches
done/ any other way (PR squash-merge, admin merge, hand move, a missed run) never did, and the
Action that was meant to catch those could never push (read-only GITHUB_TOKEN, protect-main bypass
is a deploy key / one user). The sweep reconciles STATE instead of reacting to an EVENT: every stale
`depends_on` entry that names a SHIPPED ticket is cascaded through `ticket.py cascade`.

Layers:
  - detection            : pure over a fixture tickets/ tree (ticket.TICKETS_ROOT patched)
  - orchestration        : fake `cascade` callable -- ordering, cap, sibling side effects, outcomes
  - CLI                  : main() exit codes and --check being strictly read-only
  - hermetic real-git e2e: a bare origin + two clones, real `ticket.main(["cascade", ...])`,
                           real lock/pull/commit/push -- the "verified with a real dependant" leg.
                           Never touches the live tickets/ tree.
  - unit files           : the oneshot + timer templates.

Fixture-string note: the byte-sequence trigger is built at runtime via source-time concatenation
("HERME" + "S.md") so the contiguous bytes never appear in this file (standing rule).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import _git_utils, cascade_sweep, ticket

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEMD = REPO_ROOT / "tools" / "systemd"
TRIGGER = "HERME" + "S.md"  # never written contiguously in source


def _fm(tid: str, state: str, depends_on: list[str], substate: str | None, extra: list[str]) -> str:
    lines = [
        "---",
        f"id: {tid}",
        "created: 2026-09-01T00:00:00Z",
        "requester: orchestrator",
        "target: orchestrator",
        f"state: {state}",
        "parent_ticket: null",
        f"depends_on: [{', '.join(depends_on)}]",
    ]
    if substate:
        lines.append(f"substate: {substate}")
    lines += extra
    lines += [
        "---",
        "",
        f"# Ticket {tid}",
        "",
        "## Ask",
        "",
        "x",
        "",
        "## Transition log",
        "",
        "- 2026-09-01T00:00:00Z [orchestrator] filed in inbox",
        "",
    ]
    return "\n".join(lines)


def _write(root: Path, tid: str, state: str, *, depends_on=(), substate=None, extra=()) -> Path:
    path = root / "tickets" / state / f"{tid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_fm(tid, state, list(depends_on), substate, list(extra)), encoding="utf-8")
    return path


def _deps(path: Path) -> list[str]:
    fields, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
    return list(fields.get("depends_on") or [])


class _TreeCase(unittest.TestCase):
    """A throwaway tickets/ tree bound into `ticket` (no git)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for state in ticket.STATES:
            (self.root / "tickets" / state).mkdir(parents=True)
        p = mock.patch.object(ticket, "TICKETS_ROOT", self.root / "tickets")
        p.start()
        self.addCleanup(p.stop)
        # T-4220: `main()` keeps a consecutive-deferral counter under PANTHEON_STATE_DIR. Pin it to
        # the throwaway tree so no test can read, write or DELETE the live seat's counter.
        env = mock.patch.dict(os.environ, {"PANTHEON_STATE_DIR": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)

    def strip(self, dependant: Path, *gone: str) -> None:
        """Emulate what `ticket.py cascade` does to one dependant's depends_on."""
        text = dependant.read_text(encoding="utf-8")
        keep = [d for d in _deps(dependant) if d not in gone]
        dependant.write_text(
            re.sub(r"^depends_on: \[.*\]$", f"depends_on: [{', '.join(keep)}]", text, flags=re.M),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

class FindStaleTest(_TreeCase):
    def test_a_shipped_dep_in_an_inbox_or_active_dependant_is_stale(self) -> None:
        _write(self.root, "T-2001", "done")
        _write(self.root, "T-2002", "inbox", depends_on=["T-2001"])
        _write(self.root, "T-2003", "active", depends_on=["T-2001"])
        self.assertEqual(cascade_sweep.find_stale(), {"T-2001": ["T-2002", "T-2003"]})

    def test_an_open_dep_is_not_stale(self) -> None:
        _write(self.root, "T-2001", "inbox")
        _write(self.root, "T-2004", "active")
        _write(self.root, "T-2002", "inbox", depends_on=["T-2001", "T-2004"])
        self.assertEqual(cascade_sweep.find_stale(), {})

    def test_a_dropped_dep_is_not_stale_because_dropped_is_not_resolved(self) -> None:
        # ORG-089: a dropped bet does not satisfy dependencies. Stripping it would un-gate work
        # whose dependency never shipped -- the whole reason the predicate is _dep_is_shipped.
        _write(self.root, "T-2001", "done", substate="dropped")
        _write(self.root, "T-2002", "inbox", depends_on=["T-2001"])
        self.assertEqual(cascade_sweep.find_stale(), {})

    def test_a_missing_or_malformed_dep_is_left_alone(self) -> None:
        _write(self.root, "T-2002", "inbox", depends_on=["T-8888", "not-a-tid", "T-1"])
        self.assertEqual(cascade_sweep.find_stale(), {})

    def test_only_the_shipped_member_of_a_mixed_list_is_reported(self) -> None:
        _write(self.root, "T-2001", "done")
        _write(self.root, "T-2004", "inbox")
        _write(self.root, "T-2002", "inbox", depends_on=["T-2004", "T-2001"])
        self.assertEqual(cascade_sweep.find_stale(), {"T-2001": ["T-2002"]})

    def test_a_done_dependant_is_never_swept(self) -> None:
        # Same scope as _find_dependants: cascade only edits inbox/ + active/.
        _write(self.root, "T-2001", "done")
        _write(self.root, "T-2002", "done", depends_on=["T-2001"])
        self.assertEqual(cascade_sweep.find_stale(), {})

    def test_an_unparseable_dependant_is_skipped_not_fatal(self) -> None:
        _write(self.root, "T-2001", "done")
        (self.root / "tickets" / "inbox" / "T-2005.md").write_text("no front matter\n", encoding="utf-8")
        _write(self.root, "T-2002", "inbox", depends_on=["T-2001"])
        self.assertEqual(cascade_sweep.find_stale(), {"T-2001": ["T-2002"]})

    def test_non_ticket_files_and_non_list_depends_on_are_ignored(self) -> None:
        _write(self.root, "T-2001", "done")
        (self.root / "tickets" / "inbox" / "notes.md").write_text("depends_on: [T-2001]\n", encoding="utf-8")
        odd = _write(self.root, "T-2002", "inbox")
        odd.write_text(odd.read_text(encoding="utf-8").replace("depends_on: []", "depends_on: T-2001"),
                       encoding="utf-8")
        self.assertEqual(cascade_sweep.find_stale(), {})  # a bare string is not a dep list

    def test_tids_are_ordered_numerically_not_lexically(self) -> None:
        for tid in ("T-10000", "T-9999"):
            _write(self.root, tid, "done")
        _write(self.root, "T-2002", "inbox", depends_on=["T-10000", "T-9999"])
        self.assertEqual(list(cascade_sweep.find_stale()), ["T-9999", "T-10000"])


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _nosync() -> None:
    return None


def _on_main() -> str:
    return "main"


class SweepTest(_TreeCase):
    def run_sweep(self, cascade, **kw):
        kw.setdefault("sync", _nosync)
        kw.setdefault("current_branch", _on_main)
        return cascade_sweep.sweep(cascade=cascade, **kw)

    def fixer(self, calls: list[str]):
        """A cascade that behaves like the real one: strips the tid from every dependant."""

        def cascade(tid: str) -> int:
            calls.append(tid)
            for state in ("inbox", "active"):
                for p in (self.root / "tickets" / state).glob("T-*.md"):
                    if tid in _deps(p):
                        self.strip(p, tid)
            return 0

        return cascade

    def two_stale(self):
        _write(self.root, "T-2001", "done")
        _write(self.root, "T-2002", "done")
        self.dep_a = _write(self.root, "T-2010", "inbox", depends_on=["T-2001"])
        self.dep_b = _write(self.root, "T-2011", "active", depends_on=["T-2002"])

    def test_every_stale_tid_is_cascaded_in_order_and_reported_fixed(self) -> None:
        self.two_stale()
        calls: list[str] = []
        res = self.run_sweep(self.fixer(calls))
        self.assertEqual(calls, ["T-2001", "T-2002"])
        self.assertEqual(res.fixed, ["T-2001", "T-2002"])
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(cascade_sweep.find_stale(), {})

    def test_nothing_stale_calls_nothing_and_exits_zero(self) -> None:
        _write(self.root, "T-2001", "inbox")
        calls: list[str] = []
        res = self.run_sweep(self.fixer(calls))
        self.assertEqual((calls, res.fixed, res.exit_code), ([], [], 0))

    def test_a_sibling_cleared_as_a_side_effect_is_skipped_not_recascaded(self) -> None:
        # cascade(T-2001) also prunes every other SHIPPED sibling dep (T-3933), so T-2002 is gone
        # by the time its turn comes; re-running it would be a wasted lock+pull+no-op.
        _write(self.root, "T-2001", "done")
        _write(self.root, "T-2002", "done")
        dep = _write(self.root, "T-2010", "inbox", depends_on=["T-2001", "T-2002"])
        calls: list[str] = []

        def cascade(tid: str) -> int:
            calls.append(tid)
            self.strip(dep, "T-2001", "T-2002")
            return 0

        res = self.run_sweep(cascade)
        self.assertEqual(calls, ["T-2001"])
        self.assertEqual((res.fixed, res.skipped), (["T-2001"], ["T-2002"]))
        self.assertEqual(res.exit_code, 0)

    def test_the_cap_leaves_the_remainder_for_the_next_tick_without_failing(self) -> None:
        self.two_stale()
        calls: list[str] = []
        res = self.run_sweep(self.fixer(calls), max_tids=1)
        self.assertEqual(calls, ["T-2001"])
        self.assertEqual(res.untouched, ["T-2002"])
        self.assertEqual(res.exit_code, 0)

    def test_a_nonzero_rc_is_a_failure_that_does_not_block_the_others(self) -> None:
        self.two_stale()
        calls: list[str] = []
        real = self.fixer(calls)

        def cascade(tid: str) -> int:
            if tid == "T-2001":
                calls.append(tid)
                return 1
            return real(tid)

        res = self.run_sweep(cascade)
        self.assertEqual(calls, ["T-2001", "T-2002"])
        self.assertEqual([t for t, _ in res.failed], ["T-2001"])
        self.assertEqual(res.fixed, ["T-2002"])
        self.assertEqual(res.exit_code, 1)

    def test_rc_zero_that_left_the_tid_stale_is_reported_unconverged(self) -> None:
        # A cascade that claims success but changed nothing must not read as "fixed" -- that is
        # how a silently-broken sweep would look green forever.
        self.two_stale()
        res = self.run_sweep(lambda tid: 0)
        self.assertEqual(res.fixed, [])
        self.assertEqual(len(res.failed), 2)
        self.assertIn("still stale", res.failed[0][1])
        self.assertEqual(res.exit_code, 1)

    def test_each_tid_is_attempted_at_most_once_per_run(self) -> None:
        self.two_stale()
        calls: list[str] = []

        def cascade(tid: str) -> int:
            calls.append(tid)
            return 0  # never converges

        self.run_sweep(cascade)
        self.assertEqual(sorted(calls), ["T-2001", "T-2002"])  # no retry loop

    def test_a_busy_lock_defers_the_run_and_is_not_a_failure(self) -> None:
        self.two_stale()
        calls: list[str] = []

        def cascade(tid: str) -> int:
            calls.append(tid)
            raise _git_utils.GitMutationLockTimeout("busy")

        res = self.run_sweep(cascade)
        self.assertEqual(calls, ["T-2001"])  # stops at the first deferral: the lock is per-checkout
        self.assertEqual(res.deferred, ["T-2001", "T-2002"])
        self.assertIn("lock", res.deferred_reason)
        self.assertEqual((res.failed, res.exit_code), ([], 0))

    def test_an_unexpected_exception_is_a_failure_not_a_crash(self) -> None:
        self.two_stale()

        def cascade(tid: str) -> int:
            raise RuntimeError("boom")

        res = self.run_sweep(cascade)
        self.assertEqual(len(res.failed), 2)
        self.assertIn("RuntimeError", res.failed[0][1])
        self.assertEqual(res.exit_code, 1)

    def test_a_named_non_main_branch_defers_everything(self) -> None:
        self.two_stale()
        calls: list[str] = []
        res = self.run_sweep(self.fixer(calls), current_branch=lambda: "agent/T-1")
        self.assertEqual(calls, [])
        self.assertEqual(res.deferred, ["T-2001", "T-2002"])
        self.assertIn("agent/T-1", res.deferred_reason)
        self.assertEqual(res.exit_code, 0)

    def test_a_detached_head_is_allowed(self) -> None:
        self.two_stale()
        calls: list[str] = []
        res = self.run_sweep(self.fixer(calls), current_branch=lambda: "")
        self.assertEqual(calls, ["T-2001", "T-2002"])
        self.assertEqual(res.exit_code, 0)

    def test_the_checkout_is_synced_before_detection(self) -> None:
        # The sync stands in for `git pull`: it is what brings a freshly-merged done/ ticket in.
        _write(self.root, "T-2010", "inbox", depends_on=["T-2001"])

        def sync() -> None:
            _write(self.root, "T-2001", "done")

        calls: list[str] = []
        res = self.run_sweep(self.fixer(calls), sync=sync)
        self.assertEqual(calls, ["T-2001"])
        self.assertEqual(res.exit_code, 0)

    def test_a_failing_sync_is_not_fatal(self) -> None:
        self.two_stale()

        def sync() -> None:
            raise OSError("network")

        calls: list[str] = []
        res = self.run_sweep(self.fixer(calls), sync=sync)
        self.assertEqual(calls, ["T-2001", "T-2002"])
        self.assertEqual(res.exit_code, 0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class MainTest(_TreeCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cascade_sweep.main(argv)
            except SystemExit as exc:  # argparse
                rc = exc.code
        return rc, out.getvalue(), err.getvalue()

    def test_check_lists_the_work_and_never_writes_pulls_or_cascades(self) -> None:
        _write(self.root, "T-2001", "done")
        dep = _write(self.root, "T-2010", "inbox", depends_on=["T-2001"])
        before = dep.read_text(encoding="utf-8")
        boom = mock.Mock(side_effect=AssertionError("--check must be strictly read-only"))
        with mock.patch.object(cascade_sweep, "default_cascade", boom), \
                mock.patch.object(cascade_sweep, "_sync_checkout", boom):
            rc, out, _ = self.run_main(["--check"])
        self.assertEqual(rc, 0)
        self.assertIn("stale: T-2001 -> T-2010", out)
        self.assertIn("1 stale tid(s), 1 dependant(s)", out)
        self.assertEqual(dep.read_text(encoding="utf-8"), before)

    def test_check_with_nothing_stale_says_so_and_exits_zero(self) -> None:
        rc, out, _ = self.run_main(["--check"])
        self.assertEqual(rc, 0)
        self.assertIn("0 stale tid(s)", out)

    def test_run_exit_code_is_the_sweeps(self) -> None:
        for res, want in ((cascade_sweep.SweepResult(), 0),
                          (cascade_sweep.SweepResult(failed=[("T-1", "x")]), 1)):
            with self.subTest(want=want), mock.patch.object(cascade_sweep, "sweep", return_value=res):
                rc, out, _ = self.run_main([])
                self.assertEqual(rc, want)
                self.assertIn("cascade_sweep:", out)

    def test_max_tids_is_forwarded_and_must_be_positive(self) -> None:
        with mock.patch.object(cascade_sweep, "sweep", return_value=cascade_sweep.SweepResult()) as sw:
            self.run_main(["--max-tids", "3"])
        self.assertEqual(sw.call_args.kwargs["max_tids"], 3)
        rc, _, _ = self.run_main(["--max-tids", "0"])
        self.assertEqual(rc, 2)

    def test_summary_names_every_outcome_class(self) -> None:
        res = cascade_sweep.SweepResult(
            stale={"T-1": ["T-2"]}, fixed=["T-1"], skipped=["T-3"], deferred=["T-4"],
            failed=[("T-5", "rc=1")], untouched=["T-6"], deferred_reason="lock busy")
        with mock.patch.object(cascade_sweep, "sweep", return_value=res):
            rc, out, _ = self.run_main([])
        self.assertEqual(rc, 1)
        for needle in ("fixed", "T-3", "deferred", "lock busy", "FAILED", "T-5", "rc=1", "T-6"):
            self.assertIn(needle, out)


# ---------------------------------------------------------------------------
# Hermetic real-git end-to-end
# ---------------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr or proc.stdout}")
    return proc.stdout.strip()


class GitWorld:
    """A bare origin and two clones of it. `a` is the seat the sweep runs in; `b` is a racer."""

    README = (
        "# Tickets\n\n## Current state\n\n"
        "| ID | State | Title | Requester → Target | Filed |\n|---|---|---|---|---|\n\n"
        "## Related\n\n- Link\n"
    )

    def __init__(self, base: Path) -> None:
        self.origin = base / "origin.git"
        self.a, self.b = base / "a", base / "b"
        _git(base, "init", "-q", "--bare", "-b", "main", str(self.origin))
        _git(base, "clone", "-q", str(self.origin), str(self.a))
        _git(self.a, "symbolic-ref", "HEAD", "refs/heads/main")
        _git(self.a, "config", "user.name", "Sweep Test")
        _git(self.a, "config", "user.email", "sweep@example.invalid")
        for state in ticket.STATES:
            (self.a / "tickets" / state).mkdir(parents=True)
            (self.a / "tickets" / state / ".gitkeep").write_text("", encoding="utf-8")
        (self.a / "tickets" / "README.md").write_text(self.README, encoding="utf-8")

    def seed(self, tid: str, state: str, **kw) -> Path:
        return _write(self.a, tid, state, **kw)

    def publish_seed(self) -> None:
        _git(self.a, "add", "-A")
        _git(self.a, "commit", "-q", "-m", "seed")
        _git(self.a, "push", "-q", "origin", "main")
        _git(self.origin.parent, "clone", "-q", str(self.origin), str(self.b))
        _git(self.b, "config", "user.name", "Racer")
        _git(self.b, "config", "user.email", "racer@example.invalid")

    def origin_head(self) -> str:
        return _git(self.origin, "rev-parse", "main")

    def origin_file(self, rel: str) -> str:
        return _git(self.origin, "show", f"main:{rel}")

    def origin_subjects(self) -> list[str]:
        return _git(self.origin, "log", "--format=%s", "main").splitlines()


class HermeticGitE2ETest(unittest.TestCase):
    """Real `ticket.main(["cascade", ...])` against real git repos. The sweep's contract with the
    rest of the fleet is: reuse the audited lock / pull / commit / push path, and converge."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.world = GitWorld(base)
        w = self.world

        env = mock.patch.dict(os.environ, {
            "PANTHEON_GIT_MUTATION_LOCKFILE": str(base / "ticket-ops.lock"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        })
        env.start()
        self.addCleanup(env.stop)
        for name, val in (("REPO_ROOT", w.a), ("TICKETS_ROOT", w.a / "tickets")):
            p = mock.patch.object(ticket, name, val)
            p.start()
            self.addCleanup(p.stop)
        # ticket.py binds pull_or_rebase_strict's cwd default at import time (the REAL checkout).
        # Rebind it to the hermetic seat -- otherwise this test would fetch/merge the real repo.
        real_pull = _git_utils.pull_or_rebase_strict
        p = mock.patch.object(
            ticket, "pull_or_rebase_strict",
            lambda conflict_resolver=None: real_pull(cwd=w.a, conflict_resolver=conflict_resolver),
        )
        p.start()
        self.addCleanup(p.stop)

    def sweep(self, **kw):
        kw.setdefault("sync", _nosync)
        return cascade_sweep.sweep(current_branch=lambda: ticket._current_branch(self.world.a), **kw)

    def quiet_sweep(self, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.sweep(**kw)

    # -- the core claim ------------------------------------------------------

    def test_a_stale_dependant_is_cascaded_committed_and_pushed_then_the_next_run_is_a_no_op(self) -> None:
        w = self.world
        w.seed("T-2001", "done")
        w.seed("T-2010", "inbox", depends_on=["T-2001"])
        w.publish_seed()
        seed_head = w.origin_head()

        res = self.quiet_sweep()

        self.assertEqual((res.fixed, res.failed, res.exit_code), (["T-2001"], [], 0))
        self.assertNotEqual(w.origin_head(), seed_head)
        self.assertTrue(w.origin_subjects()[0].startswith("cascade: T-2001 → 1 dependant updated"))
        fields, body = ticket.parse_front_matter(w.origin_file("tickets/inbox/T-2010.md"))
        self.assertEqual(fields["depends_on"], [])
        self.assertIn("[auto-unblock] dep resolved", body)
        self.assertEqual(_git(w.a, "status", "--porcelain"), "")  # nothing left dirty

        head_after = w.origin_head()
        res2 = self.quiet_sweep()
        self.assertEqual((res2.stale, res2.fixed, res2.exit_code), ({}, [], 0))
        self.assertEqual(w.origin_head(), head_after)  # level-triggered: converged means silent

    def test_partial_strip_keeps_the_open_gate_and_full_unblock_clears_the_stamp(self) -> None:
        w = self.world
        w.seed("T-2001", "done")
        w.seed("T-2002", "inbox")  # still open
        w.seed("T-2010", "inbox", depends_on=["T-2001", "T-2002"])
        w.seed("T-2011", "inbox", depends_on=["T-2001"],
               extra=["auto_delivered: true", "auto_delivered_at: 2026-09-01T00:00:00Z"])
        w.publish_seed()

        self.assertEqual(self.quiet_sweep().exit_code, 0)

        gated, gated_body = ticket.parse_front_matter(w.origin_file("tickets/inbox/T-2010.md"))
        self.assertEqual(gated["depends_on"], ["T-2002"])  # stripped T-2001, still gated on T-2002
        self.assertIn("[cascade] stale-done dep T-2001 stripped", gated_body)
        free, _ = ticket.parse_front_matter(w.origin_file("tickets/inbox/T-2011.md"))
        self.assertEqual(free["depends_on"], [])
        self.assertNotIn("auto_delivered", free)

    def test_a_dropped_dep_is_never_touched(self) -> None:
        w = self.world
        w.seed("T-2001", "done", substate="dropped")
        w.seed("T-2010", "inbox", depends_on=["T-2001"])
        w.publish_seed()
        head = w.origin_head()

        res = self.quiet_sweep()

        self.assertEqual((res.stale, res.exit_code), ({}, 0))
        self.assertEqual(w.origin_head(), head)
        self.assertEqual(_deps(w.a / "tickets" / "inbox" / "T-2010.md"), ["T-2001"])

    def test_several_stale_tids_each_get_their_own_cascade_commit(self) -> None:
        w = self.world
        for tid in ("T-2001", "T-2002"):
            w.seed(tid, "done")
        w.seed("T-2010", "inbox", depends_on=["T-2001"])
        w.seed("T-2011", "active", depends_on=["T-2002"])
        w.publish_seed()

        res = self.quiet_sweep()

        self.assertEqual(res.fixed, ["T-2001", "T-2002"])
        self.assertEqual(sum(s.startswith("cascade: ") for s in w.origin_subjects()), 2)

    # -- race safety ---------------------------------------------------------

    def test_a_racing_push_between_pull_and_push_is_rebased_over_not_lost(self) -> None:
        """origin advances AFTER the cascade's pull and BEFORE its push (another seat's ticket op).
        push_with_retry must rebase our commit over it; the sweep must still report success."""
        w = self.world
        w.seed("T-2001", "done")
        w.seed("T-2010", "inbox", depends_on=["T-2001"])
        w.publish_seed()
        real = ticket._cascade_unblock_dependants
        raced: list[bool] = []

        def racing(parent_tid, stats=None):
            if not raced:  # inside the lock, after the pull: someone else lands a commit
                raced.append(True)
                (w.b / "elsewhere.txt").write_text("another seat's commit\n", encoding="utf-8")
                _git(w.b, "add", "-A")
                _git(w.b, "commit", "-q", "-m", "racer: unrelated commit")
                _git(w.b, "push", "-q", "origin", "main")
            return real(parent_tid, stats=stats)

        with mock.patch.object(ticket, "_cascade_unblock_dependants", racing):
            res = self.quiet_sweep()

        self.assertEqual(raced, [True])
        self.assertEqual((res.fixed, res.failed, res.exit_code), (["T-2001"], [], 0))
        subjects = w.origin_subjects()
        self.assertIn("racer: unrelated commit", subjects)
        self.assertTrue(any(s.startswith("cascade: T-2001") for s in subjects))
        self.assertEqual(_deps(w.a / "tickets" / "inbox" / "T-2010.md"), [])

    def test_a_concurrent_writer_that_already_fixed_it_makes_the_sweep_a_clean_no_op(self) -> None:
        """Another seat cascades the same tid first (resolve-time path). The sweep's own pull
        brings that in, so detection sees nothing and it must not fabricate a commit."""
        w = self.world
        w.seed("T-2001", "done")
        w.seed("T-2010", "inbox", depends_on=["T-2001"])
        w.publish_seed()
        text = (w.b / "tickets" / "inbox" / "T-2010.md").read_text(encoding="utf-8")
        (w.b / "tickets" / "inbox" / "T-2010.md").write_text(
            text.replace("depends_on: [T-2001]", "depends_on: []"), encoding="utf-8")
        _git(w.b, "commit", "-q", "-am", "someone else cascaded")
        _git(w.b, "push", "-q", "origin", "main")
        head = w.origin_head()

        real_pull = _git_utils.pull_or_rebase_strict
        res = self.quiet_sweep(sync=lambda: real_pull(cwd=w.a))

        self.assertEqual((res.stale, res.fixed, res.exit_code), ({}, [], 0))
        self.assertEqual(w.origin_head(), head)

    def test_a_dirty_seat_checkout_fails_loudly_and_changes_nothing(self) -> None:
        """A wedged clone (uncommitted edit to the very file origin changed) is a REAL fault:
        the cascade's pull refuses, so the sweep reports failed (exit 1 -> OnFailure alert)."""
        w = self.world
        w.seed("T-2001", "done")
        w.seed("T-2010", "inbox", depends_on=["T-2001"])
        w.publish_seed()
        text = (w.b / "tickets" / "inbox" / "T-2010.md").read_text(encoding="utf-8")
        (w.b / "tickets" / "inbox" / "T-2010.md").write_text(text + "\nremote edit\n", encoding="utf-8")
        _git(w.b, "commit", "-q", "-am", "remote edit of the dependant")
        _git(w.b, "push", "-q", "origin", "main")
        dirty = w.a / "tickets" / "inbox" / "T-2010.md"
        dirty.write_text(dirty.read_text(encoding="utf-8") + "\nlocal uncommitted\n", encoding="utf-8")
        head = w.origin_head()

        res = self.quiet_sweep()

        self.assertEqual([t for t, _ in res.failed], ["T-2001"])
        self.assertEqual(res.exit_code, 1)
        self.assertEqual(w.origin_head(), head)

    def test_a_seat_checkout_on_a_feature_branch_is_deferred_not_failed(self) -> None:
        w = self.world
        w.seed("T-2001", "done")
        w.seed("T-2010", "inbox", depends_on=["T-2001"])
        w.publish_seed()
        _git(w.a, "checkout", "-q", "-b", "some/feature")
        head = w.origin_head()

        res = self.quiet_sweep()

        self.assertEqual((res.deferred, res.failed, res.exit_code), (["T-2001"], [], 0))
        self.assertEqual(w.origin_head(), head)


# ---------------------------------------------------------------------------
# Persistent-deferral escalation (T-4220)
# ---------------------------------------------------------------------------
#
# A deferral (busy git_mutation_lock, or the seat checkout on a named non-main branch) exits 0 so a
# transient never pages. But a PERMANENT deferral is a sweep that never sweeps, and exit 0 forever
# never pages either. `main()` counts consecutive pure-deferral runs in a small state file and,
# from DEFERRAL_ESCALATE_AFTER on, exits 1 so the unit's OnFailure alert fires. The counter is
# advisory: any fault reading or writing it fails OPEN (logged, never a red run).

def _deferral(reason: str = "git_mutation_lock busy (another ticket writer is mid-mutation)",
              tids=("T-2001",)) -> cascade_sweep.SweepResult:
    return cascade_sweep.SweepResult(stale={t: ["T-2010"] for t in tids}, deferred=list(tids),
                                     deferred_reason=reason)


def _capture(fn, *args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        value = fn(*args, **kwargs)
    return value, out.getvalue()


class DeferralOutcomeTest(unittest.TestCase):
    def test_only_a_pure_deferral_counts(self) -> None:
        R = cascade_sweep.SweepResult
        cases = [
            (R(deferred=["T-1"]), True),
            (R(deferred=["T-1"], fixed=["T-2"]), False),       # progress: contention, not stuck
            (R(deferred=["T-1"], failed=[("T-2", "x")]), False),  # already pages via exit 1
            (R(fixed=["T-2"]), False),
            (R(untouched=["T-3"]), False),                     # over the cap is not a deferral
            (R(), False),                                      # nothing stale
        ]
        for res, want in cases:
            with self.subTest(res=res):
                self.assertEqual(res.is_deferral, want)


class DeferralCounterTest(_TreeCase):
    """`note_deferral_outcome`: the counter's I/O and its fail-open posture."""

    def setUp(self) -> None:
        super().setUp()
        self.path = cascade_sweep.deferral_state_path()

    def note(self, res):
        return _capture(cascade_sweep.note_deferral_outcome, res)

    def stored(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    # -- where the state lives ----------------------------------------------

    def test_state_lives_under_pantheon_state_dir_and_is_resolved_at_call_time(self) -> None:
        self.assertEqual(self.path, self.root / "state" / "cascade_sweep_deferrals.json")
        with mock.patch.dict(os.environ, {"PANTHEON_STATE_DIR": str(self.root / "elsewhere")}):
            self.assertEqual(cascade_sweep.deferral_state_path().parent, self.root / "elsewhere")

    def test_default_state_dir_is_the_per_user_pantheon_state_dir(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "PANTHEON_STATE_DIR"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(Path, "home", return_value=self.root / "home"):
            self.assertEqual(cascade_sweep.deferral_state_path(),
                             self.root / "home" / ".local" / "state" / "pantheon"
                             / "cascade_sweep_deferrals.json")

    # -- counting ------------------------------------------------------------

    def test_consecutive_deferrals_count_up_and_record_the_latest_reason(self) -> None:
        for want in (1, 2, 3):
            n, out = self.note(_deferral(reason=f"reason {want}"))
            self.assertEqual(n, want)
            self.assertEqual(out, "")  # a healthy counter is silent
        self.assertEqual(self.stored(), {"count": 3, "reason": "reason 3"})

    def test_any_non_deferral_outcome_resets_the_counter_to_zero(self) -> None:
        R = cascade_sweep.SweepResult
        for res in (R(), R(fixed=["T-2"]), R(deferred=["T-1"], fixed=["T-2"]),
                    R(failed=[("T-2", "x")]), R(untouched=["T-3"])):
            with self.subTest(res=res):
                self.note(_deferral())
                self.note(_deferral())
                self.assertTrue(self.path.exists())
                n, _ = self.note(res)
                self.assertEqual(n, 0)
                self.assertFalse(self.path.exists())

    def test_the_count_restarts_from_one_after_a_reset(self) -> None:
        self.note(_deferral())
        self.note(_deferral())
        self.note(cascade_sweep.SweepResult())
        self.assertEqual(self.note(_deferral())[0], 1)

    def test_a_clean_steady_state_run_writes_nothing(self) -> None:
        n, out = self.note(cascade_sweep.SweepResult())
        self.assertEqual((n, out), (0, ""))
        self.assertFalse(self.path.parent.exists(), "a steady-state tick must not create state")

    # -- atomic write --------------------------------------------------------

    def test_a_failed_write_leaves_the_previous_file_intact_and_no_temp_debris(self) -> None:
        self.note(_deferral(reason="first"))
        self.note(_deferral(reason="second"))
        with mock.patch.object(cascade_sweep.os, "replace", side_effect=PermissionError("nope")):
            n, out = self.note(_deferral(reason="third"))
        self.assertEqual(n, 3)  # the run still knows its own count
        self.assertEqual(self.stored(), {"count": 2, "reason": "second"})
        self.assertEqual([p.name for p in self.path.parent.iterdir()], [self.path.name])
        self.assertIn("deferral counter not persisted", out)

    def test_a_successful_write_leaves_only_the_state_file(self) -> None:
        self.note(_deferral())
        self.assertEqual([p.name for p in self.path.parent.iterdir()], [self.path.name])

    # -- fail-open -----------------------------------------------------------

    def test_a_corrupt_counter_reads_as_zero_says_so_and_self_heals(self) -> None:
        for junk in ("", "{not json", "[]", "{}", '{"count": "x"}', '{"count": -1}',
                     '{"count": true}', '{"count": 1.5}'):
            with self.subTest(junk=junk):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(junk, encoding="utf-8")
                n, out = self.note(_deferral(reason="r"))
                self.assertEqual(n, 1)
                self.assertIn("deferral counter unreadable", out)
                self.assertIn(str(self.path), out)
                self.assertIn("counting from 0", out)
                self.assertEqual(self.stored(), {"count": 1, "reason": "r"})  # rewritten valid

    def test_undecodable_or_pathological_bytes_read_as_zero_and_self_heal(self) -> None:
        """B1 (rev T-4220 r1): UnicodeDecodeError is a ValueError, not an OSError, and deep JSON
        nesting raises RecursionError. Both used to escape as a traceback."""
        cases = {
            "non-utf8": b"\xff\xfe\x00garbage\x80",
            "lone continuation byte": b"\x80",
            "utf-16 valid json": '{"count": 3}'.encode("utf-16"),
            "deeply nested": b"[" * 200_000,
            "huge integer": b'{"count": ' + b"9" * 6000 + b"}",
        }
        for name, junk in cases.items():
            with self.subTest(case=name):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_bytes(junk)
                n, out = self.note(_deferral(reason="r"))  # must not raise
                self.assertEqual(n, 1)
                self.assertIn("deferral counter unreadable", out)
                self.assertIn(str(self.path), out)
                self.assertIn("counting from 0", out)
                self.assertEqual(self.stored(), {"count": 1, "reason": "r"})  # rewritten valid
                self.assertEqual(self.note(_deferral(reason="r"))[0], 2)  # and counting again

    def test_a_corrupt_counter_is_also_dropped_by_a_clean_run(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"\xff\xfe\x00garbage\x80")
        n, out = self.note(cascade_sweep.SweepResult())
        self.assertEqual((n, out), (0, ""))  # a reset never needs to read it
        self.assertFalse(self.path.exists())

    def test_a_reason_with_a_lone_surrogate_is_persisted_not_an_encode_crash(self) -> None:
        n, out = self.note(_deferral(reason="branch \udcff"))  # e.g. a surrogate-escaped branch name
        self.assertEqual((n, out), (1, ""))
        self.assertEqual(self.stored()["count"], 1)

    def test_a_missing_counter_is_normal_and_silent(self) -> None:
        n, out = self.note(_deferral())
        self.assertEqual((n, out), (1, ""))

    def test_a_directory_where_the_file_should_be_never_raises(self) -> None:
        self.path.mkdir(parents=True)
        n, out = self.note(_deferral())
        self.assertEqual(n, 1)
        self.assertIn("deferral counter unreadable", out)
        self.assertIn("deferral counter not persisted", out)
        n, out = self.note(cascade_sweep.SweepResult())  # the reset path must not raise either
        self.assertEqual(n, 0)
        self.assertIn("deferral counter", out)

    def test_an_uncreatable_state_dir_never_raises(self) -> None:
        blocker = self.root / "blocker"
        blocker.write_text("a file, not a dir", encoding="utf-8")
        with mock.patch.dict(os.environ, {"PANTHEON_STATE_DIR": str(blocker / "sub")}):
            n, out = _capture(cascade_sweep.note_deferral_outcome, _deferral())
        self.assertEqual(n, 1)
        self.assertIn("deferral counter not persisted", out)


class EscalationMainTest(_TreeCase):
    """`main()`: a persistent deferral becomes exit 1 so OnFailure pages; nothing else does."""

    def setUp(self) -> None:
        super().setUp()
        _write(self.root, "T-2001", "done")
        self.dep = _write(self.root, "T-2010", "inbox", depends_on=["T-2001"])
        self.branch = "agent/T-1"
        for name, val in (("_sync_checkout", _nosync), ("_current_branch", lambda: self.branch)):
            p = mock.patch.object(cascade_sweep, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.state = cascade_sweep.deferral_state_path()
        # `sweep()` binds default_cascade at import, so patching that name would not take effect;
        # `default_cascade` calls `ticket.main` at call time, so that is the seam. Default: a
        # tripwire -- no test here may reach the real `ticket.py cascade` (the live checkout).
        self.tripwire = mock.Mock(side_effect=AssertionError("real ticket.py cascade reached"))
        p = mock.patch.object(ticket, "main", self.tripwire)
        p.start()
        self.addCleanup(p.stop)

    def run_main(self, argv=()):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cascade_sweep.main(list(argv))
            except SystemExit as exc:  # argparse
                rc = exc.code
        return rc, out.getvalue()

    def cascade_with(self, behaviour):
        """Patch `ticket.main` so `ticket.py cascade --tid <tid>` runs `behaviour(tid) -> rc`."""
        def fake_main(argv):
            self.assertEqual(argv[:2], ["cascade", "--tid"])
            return behaviour(argv[2])
        return mock.patch.object(ticket, "main", fake_main)

    def busy_lock(self):
        def behaviour(tid: str) -> int:
            raise _git_utils.GitMutationLockTimeout("busy")
        return self.cascade_with(behaviour)

    def fixing_cascade(self):
        def behaviour(tid: str) -> int:
            self.strip(self.dep, tid)
            return 0
        return self.cascade_with(behaviour)

    # -- the escalation ------------------------------------------------------

    def test_the_default_threshold_is_eight_runs_about_two_hours_at_the_fifteen_minute_cadence(self) -> None:
        self.assertEqual(cascade_sweep.DEFERRAL_ESCALATE_AFTER, 8)

    def test_a_named_branch_deferral_exits_zero_seven_times_then_one_naming_reason_and_count(self) -> None:
        for run in range(1, 8):
            rc, out = self.run_main()
            self.assertEqual(rc, 0, f"run {run} must not page")
            self.assertNotIn("ESCALATING", out)
        rc, out = self.run_main()
        self.assertEqual(rc, 1)
        self.assertIn("ESCALATING", out)
        self.assertIn("8 consecutive deferred runs", out)
        self.assertIn("agent/T-1", out)          # the reason: which branch the checkout is stuck on
        self.assertIn("T-2001", out)             # what is waiting
        self.assertIn("OnFailure", out)

    def test_a_busy_lock_deferral_escalates_too_and_keeps_paging_each_run_after(self) -> None:
        self.branch = "main"  # so the lock, not the branch guard, is what defers
        with self.busy_lock():
            rcs = [self.run_main() for _ in range(10)]
        self.assertEqual([rc for rc, _ in rcs], [0] * 7 + [1, 1, 1])
        self.assertIn("git_mutation_lock busy", rcs[7][1])
        self.assertIn("8 consecutive", rcs[7][1])
        self.assertIn("10 consecutive", rcs[9][1])

    def test_one_clean_run_resets_the_count_so_the_next_deferral_starts_over(self) -> None:
        for _ in range(8):
            rc, _out = self.run_main()
        self.assertEqual(rc, 1)
        self.branch = "main"
        with self.fixing_cascade():
            rc, out = self.run_main()
        self.assertEqual(rc, 0)
        self.assertNotIn("ESCALATING", out)
        self.assertFalse(self.state.exists())
        # stale again, and stuck again: the count starts from 1, not from where it left off.
        self.dep.write_text(self.dep.read_text(encoding="utf-8").replace(
            "depends_on: []", "depends_on: [T-2001]"), encoding="utf-8")
        self.branch = "agent/T-1"
        for run in range(1, 8):
            self.assertEqual(self.run_main()[0], 0, f"run {run} after the reset")

    def test_a_run_that_made_progress_before_hitting_the_lock_is_not_counted(self) -> None:
        _write(self.root, "T-2002", "done")
        dep2 = _write(self.root, "T-2011", "inbox", depends_on=["T-2002"])
        self.branch = "main"

        def behaviour(tid: str) -> int:
            if tid == "T-2002":
                raise _git_utils.GitMutationLockTimeout("busy")
            self.strip(self.dep, tid)
            return 0

        with self.cascade_with(behaviour):
            for _ in range(12):
                self.dep.write_text(self.dep.read_text(encoding="utf-8").replace(
                    "depends_on: []", "depends_on: [T-2001]"), encoding="utf-8")
                rc, out = self.run_main()
                self.assertEqual(rc, 0)
                self.assertIn("fixed=1", out)      # T-2001 was fixed ...
                self.assertIn("deferred=1", out)   # ... and T-2002 hit the busy lock
        self.assertFalse(self.state.exists())
        self.assertEqual(_deps(dep2), ["T-2002"])

    def test_the_threshold_is_a_flag_and_must_be_positive(self) -> None:
        self.assertEqual(self.run_main(["--escalate-after", "2"])[0], 0)
        rc, out = self.run_main(["--escalate-after", "2"])
        self.assertEqual(rc, 1)
        self.assertIn("2 consecutive deferred runs", out)
        self.assertEqual(self.run_main(["--escalate-after", "0"])[0], 2)

    def test_check_never_reads_or_writes_the_counter(self) -> None:
        boom = mock.Mock(side_effect=AssertionError("--check must not touch the deferral counter"))
        with mock.patch.object(cascade_sweep, "note_deferral_outcome", boom):
            self.assertEqual(self.run_main(["--check"])[0], 0)
        self.assertFalse(self.state.exists())
        self.state.parent.mkdir(parents=True)
        self.state.write_text('{"count": 7, "reason": "r"}', encoding="utf-8")
        self.run_main(["--check"])
        self.assertEqual(self.state.read_text(encoding="utf-8"), '{"count": 7, "reason": "r"}')

    def test_a_genuine_failure_still_exits_one_and_never_reads_as_a_deferral(self) -> None:
        self.branch = "main"
        with self.cascade_with(lambda tid: 3):
            rc, out = self.run_main()
        self.assertEqual(rc, 1)
        self.assertIn("FAILED T-2001", out)
        self.assertNotIn("ESCALATING", out)

    # -- fail-open, end to end ----------------------------------------------

    def test_a_corrupt_counter_does_not_turn_the_run_red_and_is_logged(self) -> None:
        self.state.parent.mkdir(parents=True)
        self.state.write_text("{{{", encoding="utf-8")
        rc, out = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("deferral counter unreadable", out)
        self.assertIn("run outcome unaffected", out)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["count"], 1)

    def test_a_non_utf8_counter_is_a_logged_zero_not_a_traceback_and_self_heals(self) -> None:
        """B1 (rev T-4220 r1): this used to end the run in a UnicodeDecodeError traceback + exit 1
        on EVERY pure-deferral run, so the ESCALATING line never appeared and the count never moved."""
        self.state.parent.mkdir(parents=True)
        self.state.write_bytes(b"\xff\xfe\x00garbage\x80")
        rc, out = self.run_main()  # run_main only traps SystemExit: a raised exception fails here
        self.assertEqual(rc, 0)
        self.assertIn("deferral counter unreadable", out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["count"], 1)
        for run in range(2, 8):  # healed: it counts on from there and escalates at the 8th
            self.assertEqual(self.run_main()[0], 0, f"run {run}")
        rc, out = self.run_main()
        self.assertEqual(rc, 1)
        self.assertIn("8 consecutive deferred runs", out)

    def test_a_non_utf8_counter_is_dropped_by_the_next_clean_run(self) -> None:
        self.state.parent.mkdir(parents=True)
        self.state.write_bytes(b"\xff\xfe\x00garbage\x80")
        self.branch = "main"
        with self.fixing_cascade():
            rc, out = self.run_main()
        self.assertEqual(rc, 0)
        self.assertFalse(self.state.exists())

    def test_an_unresolvable_home_never_changes_the_exit_code(self) -> None:
        """`Path.home()` can raise RuntimeError when there is no HOME to resolve."""
        env = {k: v for k, v in os.environ.items() if k != "PANTHEON_STATE_DIR"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(Path, "home", side_effect=RuntimeError("Could not determine home directory.")):
            rc, out = self.run_main(["--escalate-after", "1"])  # would page on the first deferral
            self.assertEqual(rc, 0)
            self.assertIn("deferral counter error (RuntimeError: Could not determine home directory.); "
                          "run outcome unaffected", out)
            self.branch = "main"
            with self.cascade_with(lambda tid: 3):  # a genuine failure still pages, unlowered
                rc, out = self.run_main()
            self.assertEqual(rc, 1)
            self.assertIn("FAILED T-2001", out)
            self.assertIn("deferral counter error", out)

    def test_any_exception_from_the_counter_is_advisory_backstop_in_main(self) -> None:
        """The local handlers cover the faults a bad file can cause; `main()` is the backstop for
        everything else, so NO counter failure can alter the exit code."""
        boom = mock.Mock(side_effect=ValueError("boom"))
        with mock.patch.object(cascade_sweep, "note_deferral_outcome", boom):
            rc, out = self.run_main(["--escalate-after", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("deferral counter error (ValueError: boom); run outcome unaffected", out)
        self.assertNotIn("ESCALATING", out)

    def test_a_counter_that_can_never_persist_never_turns_a_run_red(self) -> None:
        self.state.mkdir(parents=True)  # a directory where the file should be: unreadable AND unwritable
        for _ in range(12):  # well past N: it cannot count, so it cannot escalate -- by design
            rc, out = self.run_main()
            self.assertEqual(rc, 0)
            self.assertIn("deferral counter not persisted", out)  # ... but it says so, every run


# ---------------------------------------------------------------------------
# Unit files + hygiene
# ---------------------------------------------------------------------------

class UnitFilesTest(unittest.TestCase):
    service = SYSTEMD / "pantheon-cascade-sweep.service"
    timer = SYSTEMD / "pantheon-cascade-sweep.timer"

    def _directives(self, path: Path) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(("#", "[")) or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out.setdefault(key.strip(), []).append(value.strip())
        return out

    def test_the_service_runs_this_tool_as_a_bounded_oneshot_that_alerts_on_failure(self) -> None:
        d = self._directives(self.service)
        self.assertEqual(d["Type"], ["oneshot"])
        self.assertEqual(d["OnFailure"], ["pantheon-unit-failure-alert@%n.service"])
        self.assertEqual(d["ExecStart"], ["/usr/bin/python3 %h/pantheon-config/tools/cascade_sweep.py"])
        self.assertTrue((REPO_ROOT / "tools" / "cascade_sweep.py").is_file())
        self.assertEqual(d["WorkingDirectory"], ["%h/pantheon-config"])
        self.assertRegex(d["TimeoutStartSec"][0], r"^\d+(min|s)$")

    def test_the_timer_fires_this_service_every_fifteen_minutes_and_catches_up(self) -> None:
        d = self._directives(self.timer)
        self.assertEqual(d["Unit"], ["pantheon-cascade-sweep.service"])
        self.assertEqual(d["OnCalendar"], ["*-*-* *:04,19,34,49:00"])
        self.assertEqual(d["Persistent"], ["true"])
        self.assertEqual(d["WantedBy"], ["timers.target"])


class HygieneTest(unittest.TestCase):
    def test_new_files_are_free_of_the_trigger_bytes(self) -> None:
        for path in (REPO_ROOT / "tools" / "cascade_sweep.py", Path(__file__),
                     SYSTEMD / "pantheon-cascade-sweep.service", SYSTEMD / "pantheon-cascade-sweep.timer"):
            with self.subTest(path=path.name):
                self.assertNotIn(TRIGGER, path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
