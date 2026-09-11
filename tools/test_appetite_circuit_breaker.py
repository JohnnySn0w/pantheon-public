"""T-3440 / ORG-089 — appetite circuit-breaker + DROPPED completion predicate.

Run from repo root:
    python3 -m unittest tools.test_appetite_circuit_breaker -v

Houses the THREE required falsifiers (named in the T-3440 dispatch):
  1. Transition-log interval algebra   — TestActiveSpendStateMachine
  2. Multi-dep completion predicate     — TestDroppedDepDoesNotUnblock
  3. Automated-filer file-safety        — TestAutomatedFilerFileSafety

Plus unit coverage for _is_shipped / _dep_is_shipped, the re-decision verbs,
the file-time applicability refusal, the grooming checks, and the calibration
gate / fail-safe paths.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import shutil
from unittest.mock import MagicMock

from tools import _git_utils, ticket, ticket_lint
from tools import ticket_grooming as tg
from tools import ticket_purpose as tp


def _ticket_text(
    *,
    tid: str,
    state: str,
    depends_on: list[str] | None = None,
    substate: str | None = None,
    dropped_reason: str | None = None,
    appetite: int | None = None,
    labels: list[str] | None = None,
    serves: str | None = None,
    target: str = "metis",
    created: str = "2026-09-06T01:00:00Z",
    transition_lines: list[str] | None = None,
    title: str = "Example bet",
) -> str:
    fm = [
        "---",
        f"id: {tid}",
        f"created: {created}",
        "requester: orchestrator",
        f"target: {target}",
        f"state: {state}",
        "parent_ticket: null",
        f"depends_on: [{', '.join(depends_on)}]" if depends_on else "depends_on: []",
    ]
    if labels is not None:
        fm.append(f"labels: [{', '.join(labels)}]")
    if serves is not None:
        fm.append(f"serves: {serves}")
    if appetite is not None:
        fm.append(f"appetite: {appetite}")
    if substate is not None:
        fm.append(f"substate: {substate}")
    if dropped_reason is not None:
        fm.append(f"dropped_reason: {dropped_reason}")
    fm.append("---")
    body = [
        "",
        f"# {title}",
        "",
        "## Ask",
        "",
        "## Transition log",
        "",
        f"- {created} [orchestrator] filed in inbox",
    ]
    if transition_lines:
        body.extend(transition_lines)
    body.append("")
    return "\n".join(fm + body)


class _TicketsRootFixture(unittest.TestCase):
    """Patches ticket.TICKETS_ROOT to a tempdir with the three state dirs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.tickets = self.root / "tickets"
        for s in ("inbox", "active", "done"):
            (self.tickets / s).mkdir(parents=True, exist_ok=True)
        self._patch = patch.object(ticket, "TICKETS_ROOT", self.tickets)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _write(self, state: str, **kw) -> Path:
        text = _ticket_text(state=state, **kw)
        p = self.tickets / state / f"{kw['tid']}.md"
        p.write_text(text, encoding="utf-8")
        return p


class _VerbFixture(_TicketsRootFixture):
    """_TicketsRootFixture + mocked git so the committing verbs (drop / reopen /
    re-earn / adjust-appetite / file) run with no real git or network. Trimmed
    sibling of test_ticket.py::TestFullLifecycleGitCommands."""

    def setUp(self) -> None:
        super().setUp()
        _git_utils._git_common_dir_cache = None
        self.addCleanup(setattr, _git_utils, "_git_common_dir_cache", None)

        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self.addCleanup(self._repo_patch.stop)
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self.addCleanup(self._gu_repo_patch.stop)
        # Isolate the appetite outcome-log writer: _appetite_outcome_log resolves
        # the ledger root from ticket_grooming.REPO_ROOT — pin it to the tempdir
        # so terminal-verb outcome logging never writes to the real repo ledger.
        self._tg_repo_patch = patch.object(tg, "REPO_ROOT", self.root)
        self._tg_repo_patch.start()
        self.addCleanup(self._tg_repo_patch.stop)

        readme = self.tickets / "README.md"
        readme.write_text(
            "# Tickets\n\n## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n\n## Related\n\n- Link\n",
            encoding="utf-8",
        )

        self._sub_patch = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._sub_patch.start()
        self.addCleanup(self._sub_patch.stop)
        self.mock_run.side_effect = self._fake_run

    def _fake_run(self, argv, **kwargs):
        if argv[:2] == ["git", "mv"] and len(argv) >= 4:
            src = self.root / argv[2]
            dst = self.root / argv[3]
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.exists():
                shutil.move(str(src), str(dst))
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=128, stdout="", stderr="fatal: bad source\n")
        if argv[:2] == ["git", "add"] and "--" in argv:
            idx = argv.index("--")
            missing = [p for p in argv[idx + 1:] if not (self.root / p).exists()]
            if missing:
                return MagicMock(returncode=128, stdout="",
                                 stderr=f"fatal: pathspec '{missing[0]}' did not match\n")
            return MagicMock(returncode=0, stdout="", stderr="")
        if argv[:2] == ["git", "diff"]:
            # Mirror `git diff --cached --name-only --no-renames`: a `git mv`
            # stages BOTH the deletion (old) and the addition (new); `git add`
            # stages the named path. Walk call history; a commit clears staging.
            staged: set[str] = set()
            for prior in self.mock_run.call_args_list:
                pa = prior.args[0]
                if pa[:2] == ["git", "mv"] and len(pa) >= 4:
                    staged.add(pa[2])  # old (deletion)
                    staged.add(pa[3])  # new (addition)
                elif pa[:2] == ["git", "add"] and "--" in pa:
                    i = pa.index("--")
                    for p in pa[i + 1:]:
                        if (self.root / p).exists():
                            staged.add(p)
                elif pa[:2] == ["git", "commit"]:
                    staged.clear()
            return MagicMock(returncode=0,
                             stdout="\n".join(sorted(staged)) + ("\n" if staged else ""),
                             stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    def _ns(self, **kw) -> argparse.Namespace:
        kw.setdefault("no_push", True)
        return argparse.Namespace(**kw)


# ---------------------------------------------------------------------------
# _is_shipped / _dep_is_shipped
# ---------------------------------------------------------------------------


class TestIsShipped(_TicketsRootFixture):
    def test_done_not_dropped_is_shipped(self) -> None:
        p = self._write("done", tid="T-0001")
        self.assertTrue(ticket._is_shipped(ticket.parse_ticket(p)))
        self.assertTrue(ticket._dep_is_shipped("T-0001"))

    def test_dropped_is_not_shipped(self) -> None:
        p = self._write("done", tid="T-0002", substate="dropped",
                        dropped_reason="not worth it")
        self.assertFalse(ticket._is_shipped(ticket.parse_ticket(p)))
        self.assertFalse(ticket._dep_is_shipped("T-0002"))

    def test_open_is_not_shipped(self) -> None:
        p = self._write("active", tid="T-0003")
        self.assertFalse(ticket._is_shipped(ticket.parse_ticket(p)))
        self.assertFalse(ticket._dep_is_shipped("T-0003"))

    def test_dep_is_shipped_fail_closed_on_malformed_or_missing(self) -> None:
        self.assertFalse(ticket._dep_is_shipped("not-an-id"))
        self.assertFalse(ticket._dep_is_shipped("T-9999"))  # absent


# ---------------------------------------------------------------------------
# FALSIFIER 2 — multi-dependency completion predicate.
# A depends_on [T-X, T-Y]; drop T-X (substate:dropped); resolve T-Y (shipped).
# ORG-013 must NOT unblock A because T-X is not _is_shipped().
# ---------------------------------------------------------------------------


class TestDroppedDepDoesNotUnblock(_TicketsRootFixture):
    def setUp(self) -> None:
        super().setUp()
        # T-X: dropped (terminal, in done/). T-Y: freshly resolved (in done/).
        self._write("done", tid="T-0100", substate="dropped",
                    dropped_reason="bet abandoned at score 3")
        self._write("done", tid="T-0101")  # simulates the just-resolved dep
        # A: still gated on both.
        self.a_path = self._write("inbox", tid="T-0102",
                                  depends_on=["T-0100", "T-0101"])

    def test_guard_treats_dropped_sibling_as_unsatisfied(self) -> None:
        self.assertFalse(ticket._dep_is_shipped("T-0100"))
        self.assertTrue(ticket._dep_is_shipped("T-0101"))
        # The load-bearing ORG-013 guard: with T-Y just resolved, the OTHER dep
        # (T-X, dropped) must keep A gated.
        self.assertFalse(
            ticket._all_other_deps_shipped(["T-0100", "T-0101"], "T-0101")
        )

    def test_cascade_does_not_unblock_A(self) -> None:
        affected = ticket._cascade_unblock_dependants("T-0101")
        self.assertEqual(affected, [])
        # A's depends_on is untouched — T-X still gates it.
        a = ticket.parse_ticket(self.a_path)
        self.assertIn("T-0100", a.fields["depends_on"])

    def test_inbox_classifies_A_as_dependency_blocked(self) -> None:
        from tools import inbox_consumer
        with patch.object(inbox_consumer, "TICKETS_ROOT", self.tickets):
            with self.assertRaises(inbox_consumer.SkipCycle) as ctx:
                inbox_consumer.list_eligible_tickets("metis")
        self.assertEqual(str(ctx.exception), "dependency_blocked")

    def test_lint_emits_dropped_dep_finding_not_stale(self) -> None:
        findings = ticket_lint.detect_dep_graph_issues(self.tickets)
        kinds = {f.kind for f in findings}
        self.assertIn("dropped_dep", kinds)
        self.assertNotIn("stale_done_dep", kinds)
        dropped = [f for f in findings if f.kind == "dropped_dep"]
        self.assertTrue(any("T-0100" in f.detail for f in dropped))


# ---------------------------------------------------------------------------
# FALSIFIER 1 — transition-log interval algebra.
# claim -> annotate -> block -> unblock -> resolve yields
#   active_spend = (block_ts - claim_ts) + (resolve_ts - unblock_ts),
# NOT resolve_ts - claim_ts.
# ---------------------------------------------------------------------------


def _spend_body(events: list[tuple[str, str]]) -> str:
    """Build a body with a ## Transition log of (iso_ts, verb) bullets."""
    lines = ["", "# t", "", "## Transition log", ""]
    for ts, verb in events:
        lines.append(f"- {ts} [metis] {verb}")
    lines.append("")
    return "\n".join(lines)


class TestActiveSpendStateMachine(unittest.TestCase):
    def _t(self, s: str) -> datetime:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    def test_falsifier_interval_algebra(self) -> None:
        # T0 claim, T1 annotate (noop), T2 block, T3 unblock, T4 resolve.
        T0, T1, T2 = "2026-09-06T00:00:00Z", "2026-09-06T00:30:00Z", "2026-09-06T01:00:00Z"
        T3, T4 = "2026-09-06T05:00:00Z", "2026-09-06T06:00:00Z"
        body = _spend_body([
            (T0, "claimed"), (T1, "annotate: a note"),
            (T2, "blocked"), (T3, "unblock"), (T4, "resolved"),
        ])
        spend = tg.active_spend_from_transition_log(body, now=self._t(T4))
        expected = int((self._t(T2) - self._t(T0)).total_seconds()
                       + (self._t(T4) - self._t(T3)).total_seconds())
        wrong = int((self._t(T4) - self._t(T0)).total_seconds())
        self.assertEqual(spend, expected)  # (block-claim)+(resolve-unblock)
        self.assertNotEqual(spend, wrong)  # NOT resolve-claim

    def test_annotate_does_not_advance_state(self) -> None:
        T0, T1, T2 = "2026-09-06T00:00:00Z", "2026-09-06T00:10:00Z", "2026-09-06T00:20:00Z"
        body = _spend_body([(T0, "claimed"), (T1, "annotate: x"), (T2, "resolved")])
        spend = tg.active_spend_from_transition_log(body, now=self._t(T2))
        self.assertEqual(spend, int((self._t(T2) - self._t(T0)).total_seconds()))

    def test_in_flight_open_interval_accrues_to_now(self) -> None:
        T0 = "2026-09-06T00:00:00Z"
        now = self._t("2026-09-06T02:00:00Z")
        body = _spend_body([(T0, "claimed")])  # never closed
        self.assertEqual(tg.active_spend_from_transition_log(body, now=now), 7200)

    def test_reset_verb_restarts_clock(self) -> None:
        # claim, (long), re-earn resets, then resolve — only post-reset counts.
        T0, T1, T2 = "2026-09-06T00:00:00Z", "2026-09-06T10:00:00Z", "2026-09-06T11:00:00Z"
        body = _spend_body([(T0, "claimed"), (T1, "re-earn"), (T2, "resolved")])
        spend = tg.active_spend_from_transition_log(body, now=self._t(T2))
        self.assertEqual(spend, 3600)  # only T1->T2

    def test_malformed_order_never_raises_and_flags(self) -> None:
        # close (resolve) with no open interval — skipped, flagged, no crash.
        body = _spend_body([("2026-09-06T00:00:00Z", "resolved")])
        seconds, malformed = tg._active_spend_impl(body, self._t("2026-09-06T01:00:00Z"))
        self.assertEqual(seconds, 0)
        self.assertTrue(malformed)

    def test_cascade_auto_unblock_actor_resumes(self) -> None:
        T0, T1, T2, T3 = ("2026-09-06T00:00:00Z", "2026-09-06T01:00:00Z",
                          "2026-09-06T05:00:00Z", "2026-09-06T06:00:00Z")
        lines = ["", "# t", "", "## Transition log", "",
                 f"- {T0} [metis] claimed",
                 f"- {T1} [metis] blocked",
                 f"- {T2} [auto-unblock] dep resolved",  # cascade resume
                 f"- {T3} [metis] resolved", ""]
        body = "\n".join(lines)
        spend = tg.active_spend_from_transition_log(body, now=self._t(T3))
        # (block-claim) + (resolve-autounblock) = 3600 + 3600
        self.assertEqual(spend, 7200)


# ---------------------------------------------------------------------------
# Re-decision verbs: drop / reopen / re-earn / adjust-appetite.
# ---------------------------------------------------------------------------


class TestReDecisionVerbs(_VerbFixture):
    def _read(self, state: str, tid: str) -> dict:
        fields, _ = ticket.parse_front_matter(
            (self.tickets / state / f"{tid}.md").read_text(encoding="utf-8")
        )
        return fields

    def test_drop_moves_to_done_with_substate(self) -> None:
        self._write("active", tid="T-0200", appetite=7)
        rc = ticket.cmd_drop(self._ns(ticket="T-0200",
                                      dropped_reason="not worth it at score 3"))
        self.assertEqual(rc, 0)
        self.assertFalse((self.tickets / "active" / "T-0200.md").exists())
        f = self._read("done", "T-0200")
        self.assertEqual(f["substate"], "dropped")
        self.assertEqual(f["dropped_reason"], "not worth it at score 3")
        self.assertEqual(f["dropped_from_state"], "active")

    def test_drop_from_inbox_records_origin(self) -> None:
        self._write("inbox", tid="T-0201", appetite=5)
        ticket.cmd_drop(self._ns(ticket="T-0201", dropped_reason="deprioritized"))
        self.assertEqual(self._read("done", "T-0201")["dropped_from_state"], "inbox")

    def test_drop_refuses_empty_reason(self) -> None:
        self._write("active", tid="T-0202", appetite=5)
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_drop(self._ns(ticket="T-0202", dropped_reason="  "))

    def test_reopen_dropped_returns_to_inbox_and_clears_disposition(self) -> None:
        self._write("done", tid="T-0210", substate="dropped",
                    dropped_reason="was abandoned", appetite=3)
        rc = ticket.cmd_reopen(self._ns(ticket="T-0210", re_earn_appetite="8",
                                        rationale="new evidence makes it worth it"))
        self.assertEqual(rc, 0)
        self.assertFalse((self.tickets / "done" / "T-0210.md").exists())
        f = self._read("inbox", "T-0210")
        self.assertNotIn("substate", f)
        self.assertNotIn("dropped_reason", f)
        self.assertNotIn("dropped_from_state", f)
        self.assertEqual(f["appetite"], "8")

    def test_reopen_refuses_non_dropped_done_ticket(self) -> None:
        self._write("done", tid="T-0211", appetite=5)  # shipped, not dropped
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_reopen(self._ns(ticket="T-0211", re_earn_appetite="8",
                                       rationale="x"))

    def test_re_earn_clears_overrun_and_sets_appetite(self) -> None:
        self._write("active", tid="T-0220", substate="appetite-overrun", appetite=5)
        rc = ticket.cmd_re_earn(self._ns(ticket="T-0220", appetite="8",
                                         rationale="still worth it, deeper scope"))
        self.assertEqual(rc, 0)
        f = self._read("active", "T-0220")
        self.assertNotIn("substate", f)
        self.assertEqual(f["appetite"], "8")
        # transition log carries the re-earn verb (clock-reset marker).
        body = (self.tickets / "active" / "T-0220.md").read_text(encoding="utf-8")
        self.assertIn("re-earn", body)

    def test_re_earn_refuses_when_not_overrun(self) -> None:
        self._write("active", tid="T-0221", appetite=5)
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_re_earn(self._ns(ticket="T-0221", appetite="8", rationale="x"))

    def test_adjust_appetite_corrects_score_without_requiring_overrun(self) -> None:
        self._write("active", tid="T-0230", appetite=9)
        rc = ticket.cmd_adjust_appetite(self._ns(ticket="T-0230", appetite="4",
                                                 rationale="original 9 was wrong"))
        self.assertEqual(rc, 0)
        self.assertEqual(self._read("active", "T-0230")["appetite"], "4")

    def test_adjust_appetite_also_clears_overrun(self) -> None:
        self._write("active", tid="T-0231", substate="appetite-overrun", appetite=8)
        ticket.cmd_adjust_appetite(self._ns(ticket="T-0231", appetite="3",
                                            rationale="mis-scored"))
        self.assertNotIn("substate", self._read("active", "T-0231"))

    def test_verbs_refuse_bad_appetite(self) -> None:
        self._write("active", tid="T-0240", substate="appetite-overrun", appetite=5)
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_re_earn(self._ns(ticket="T-0240", appetite="99", rationale="x"))


# ---------------------------------------------------------------------------
# resolve refusal on appetite-overrun (typed AppetiteOverrunNotResolvable).
# ---------------------------------------------------------------------------


class TestResolveRefusesOverrun(_VerbFixture):
    def test_resolve_refuses_on_overrun(self) -> None:
        self._write("active", tid="T-0300", substate="appetite-overrun", appetite=5)
        with self.assertRaises(ticket.AppetiteOverrunNotResolvable):
            ticket.cmd_resolve(self._ns(ticket="T-0300", with_summary="done", receipt=None))

    def test_resolve_allows_normal_active(self) -> None:
        self._write("active", tid="T-0301", appetite=5)
        rc = ticket.cmd_resolve(self._ns(ticket="T-0301", with_summary="done", receipt=None))
        self.assertEqual(rc, 0)
        self.assertTrue((self.tickets / "done" / "T-0301.md").exists())


# ---------------------------------------------------------------------------
# T-3451: appetite outcome-log shard write folded into the resolve/drop/reopen
# commit (previously landed UNCOMMITTED as a disconnected post-transition
# write — metis hit it on T-3440 resolve, HO hit it on T-3439 resolve).
# ---------------------------------------------------------------------------


class TestAppetiteOutcomeLogFoldIntoCommit(_VerbFixture):
    def _staged_paths(self) -> list[str]:
        """The pathspecs from the LAST `git add -- ...` call — what the scope
        guard's `git diff --cached --name-only` would report as newly staged
        by this transition's own add (mirrors _commit_and_push step 4/5)."""
        for call in reversed(self.mock_run.call_args_list):
            argv = call.args[0]
            if argv[:2] == ["git", "add"] and "--" in argv:
                idx = argv.index("--")
                return list(argv[idx + 1:])
        return []

    def _commit_call_count(self) -> int:
        return sum(
            1 for c in self.mock_run.call_args_list
            if c.args[0][:2] == ["git", "commit"]
        )

    def test_fold_verification_resolve_stages_shard_in_same_commit(self) -> None:
        self._write("active", tid="T-0500", appetite=5)
        rc = ticket.cmd_resolve(
            self._ns(ticket="T-0500", with_summary="shipped", receipt=None)
        )
        self.assertEqual(rc, 0)
        # (a) the outcome-log line landed on disk — glob rather than predict
        # the week-shard filename, so this isn't flaky across a week boundary.
        shard_files = list((self.root / "ledger" / "appetite").glob("*.jsonl"))
        self.assertEqual(len(shard_files), 1)
        shard_path = shard_files[0]
        event = json.loads(shard_path.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(event["tid"], "T-0500")
        self.assertEqual(event["outcome"], "shipped")
        # ...and it is in the SAME commit as the ticket transition: its
        # repo-relative path was part of the one `git add` call feeding the
        # scope guard.
        shard_rel = str(shard_path.relative_to(self.root))
        self.assertIn(shard_rel, self._staged_paths())
        # (b)/(c) no separate commit needed — exactly ONE `git commit` fired
        # for the whole resolve (proves atomicity, not a two-step land).
        self.assertEqual(self._commit_call_count(), 1)

    def test_narrow_admission_wrong_week_shard_refused(self) -> None:
        self._write("active", tid="T-0501", appetite=5)
        # Pre-stage a DIFFERENT week's appetite shard — simulates a stray/
        # leftover staged file riding the resolve commit. Never the one the
        # fold would legitimately admit (that's always the CURRENT week).
        wrong_week_rel = "ledger/appetite/2000-W01.jsonl"
        wrong_week_path = self.root / wrong_week_rel
        wrong_week_path.parent.mkdir(parents=True, exist_ok=True)
        wrong_week_path.write_text('{"tid": "T-0000"}\n', encoding="utf-8")
        ticket._git("add", "--", wrong_week_rel)
        with self.assertRaises(ticket.TicketError) as ctx:
            ticket.cmd_resolve(
                self._ns(ticket="T-0501", with_summary="shipped", receipt=None)
            )
        self.assertIn("scope guard refused", str(ctx.exception))

    def test_verb_conditional_annotate_does_not_admit_current_week_shard(self) -> None:
        self._write("active", tid="T-0502", appetite=5)
        # Pre-stage the CURRENT week's exact shard filename — the one path
        # resolve/drop/reopen WOULD admit — but drive a verb that never
        # passes appetite_outcome to _do_transition (annotate doesn't even
        # go through _do_transition). Admission must be verb-conditional,
        # not merely path-conditional.
        shard_name = tg._iso_week_shard_name(datetime.now(timezone.utc))
        shard_rel = f"ledger/appetite/{shard_name}"
        shard_path = self.root / shard_rel
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        shard_path.write_text('{"tid": "T-0000"}\n', encoding="utf-8")
        ticket._git("add", "--", shard_rel)
        with self.assertRaises(ticket.TicketError) as ctx:
            ticket.cmd_annotate(argparse.Namespace(
                ticket="T-0502", note="unrelated note", no_push=True,
            ))
        self.assertIn("scope guard refused", str(ctx.exception))

    def test_legacy_ticket_without_appetite_still_commits_cleanly(self) -> None:
        # No appetite= kwarg → no `appetite:` field at all (pre-ORG-089 shape).
        self._write("active", tid="T-0503")
        rc = ticket.cmd_resolve(
            self._ns(ticket="T-0503", with_summary="shipped", receipt=None)
        )
        self.assertEqual(rc, 0)
        self.assertTrue((self.tickets / "done" / "T-0503.md").exists())
        self.assertEqual(self._commit_call_count(), 1)
        shard_files = list((self.root / "ledger" / "appetite").glob("*.jsonl"))
        self.assertEqual(len(shard_files), 1)
        shard_path = shard_files[0]
        event = json.loads(shard_path.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertIsNone(event["appetite"])
        # The legacy (no-appetite) path must ALSO fold the shard into the same
        # commit — this is the assertion that actually distinguishes the fix
        # from pre-T-3451 behavior (the write itself was already unconditional
        # before this fix; only its commit-atomicity is new).
        shard_rel = str(shard_path.relative_to(self.root))
        self.assertIn(shard_rel, self._staged_paths())


# ---------------------------------------------------------------------------
# file-time appetite applicability (§Decision.4e) — refusal + medium-default.
# ---------------------------------------------------------------------------


class TestFileAppetiteApplicability(_VerbFixture):
    def _file_ns(self, **kw) -> argparse.Namespace:
        base = dict(
            title="a bet", requester="orchestrator", target="metis",
            labels=None, depends_on=None, implements=None, verifies=None,
            supersedes=None, informs=None, parent_ticket=None, orphan=None,
            project_id=None, nearest_intent_ref=None, serves=None,
            stale_after=None, not_before=None, not_before_anchor=None,
            ratifiable_file=None, intent=None, retest_status=None,
            spawn_impl=False, terminal=False, impl_title=None, impl_ask=None,
            impl_target=None, ask=None, context=None, appetite=None, bet_ref=None,
            no_push=True,
        )
        base.update(kw)
        return argparse.Namespace(**base)

    def _filed_fields(self, tid_expected: str = "T-0001") -> dict:
        fields, _ = ticket.parse_front_matter(
            (self.tickets / "inbox" / f"{tid_expected}.md").read_text(encoding="utf-8")
        )
        return fields

    def test_bet_with_serves_refuses_without_appetite(self) -> None:
        with self.assertRaises(ticket.AppetiteRequired):
            ticket.cmd_file(self._file_ns(serves="ship the ORG-089 surfaces"))

    def test_bet_with_appetite_files_successfully(self) -> None:
        rc = ticket.cmd_file(self._file_ns(serves="ship the surfaces", appetite="7"))
        self.assertEqual(rc, 0)
        self.assertEqual(self._filed_fields()["appetite"], "7")

    def test_bet_ref_triggers_requirement(self) -> None:
        with self.assertRaises(ticket.AppetiteRequired):
            ticket.cmd_file(self._file_ns(serves=None, bet_ref="ORG-089",
                                          parent_ticket=None,
                                          orphan="genuine root with a bet ref, no other edge"))

    def test_non_vault_adr_parent_triggers_requirement(self) -> None:
        # A non-vault (ORG-NNN) ADR obligation edge marks the ticket a bet.
        with self.assertRaises(ticket.AppetiteRequired):
            ticket.cmd_file(self._file_ns(serves=None, implements="ORG-089#E1"))

    def test_vault_adr_parent_alone_does_not_require_appetite(self) -> None:
        # A vault ADR (ADR-NNN < 200) is operator-authored — not a bet trigger.
        # With no serves/bet_ref either, appetite stays optional (files clean).
        rc = ticket.cmd_file(self._file_ns(serves=None, implements="ADR-011#E1"))
        self.assertEqual(rc, 0)
        f = self._filed_fields()
        self.assertNotIn("appetite", f)


# ---------------------------------------------------------------------------
# FALSIFIER 3 — automated-filer file-safety (Q3 required guard).
# The AUTOMATED_FILER_EXPECTATIONS shapes (hygiene label and/or
# serves: goal-neutral-maintenance) MUST file successfully with NO
# AppetiteRequired — the carve-out must cover automated filing.
# ---------------------------------------------------------------------------


class TestAutomatedFilerFileSafety(_VerbFixture):
    def _file_ns(self, **kw) -> argparse.Namespace:
        base = dict(
            title="automated finding", requester="orchestrator", target="metis",
            labels=None, depends_on=None, implements=None, verifies=None,
            supersedes=None, informs=None, parent_ticket=None, orphan=None,
            project_id="null", nearest_intent_ref="maintenance:ticket-board",
            serves=None, stale_after=None, not_before=None, not_before_anchor=None,
            ratifiable_file=None, intent=None, retest_status=None,
            spawn_impl=False, terminal=False, impl_title=None, impl_ask=None,
            impl_target=None, ask=None, context=None, appetite=None, bet_ref=None,
            no_push=True,
        )
        base.update(kw)
        return argparse.Namespace(**base)

    def _last_filed_fields(self) -> dict:
        files = sorted((self.tickets / "inbox").glob("T-*.md"))
        fields, _ = ticket.parse_front_matter(files[-1].read_text(encoding="utf-8"))
        return fields

    def test_goal_neutral_maintenance_serves_files_without_appetite(self) -> None:
        # knowledge_sweep / mirror_drift_sweep shape: serves goal-neutral-maintenance.
        rc = ticket.cmd_file(self._file_ns(serves="goal-neutral-maintenance"))
        self.assertEqual(rc, 0)
        # medium-default stamped so the field exists going forward.
        self.assertEqual(self._last_filed_fields()["appetite"], str(tp.APPETITE_MEDIUM_DEFAULT))

    def test_hygiene_label_files_without_appetite(self) -> None:
        rc = ticket.cmd_file(self._file_ns(serves="tidy the board", labels="hygiene"))
        self.assertEqual(rc, 0)
        self.assertEqual(self._last_filed_fields()["appetite"], str(tp.APPETITE_MEDIUM_DEFAULT))

    def test_chore_and_cleanup_labels_file_without_appetite(self) -> None:
        for lbl in ("chore", "cleanup", "noodle", "noop"):
            rc = ticket.cmd_file(self._file_ns(serves="routine work", labels=lbl))
            self.assertEqual(rc, 0, f"{lbl} filer must not be refused")


# ---------------------------------------------------------------------------
# Grooming: completeness check, overrun check (calibration gate), outcome log.
# ---------------------------------------------------------------------------


def _adr_text(*, adr_id: str, status: str, appetite: str | None,
              appetite_body: str, riskiest_body: str, date: str = "2026-09-06") -> str:
    fm = ["---", f"id: {adr_id}", f"date: {date}", "build_disposition: needs-build"]
    if appetite is not None:
        fm.append(f"appetite: {appetite}")
    fm.append("---")
    body = [
        "", f"# {adr_id} — test ADR", "",
        f"**Status:** {status}", "",
        "## Appetite", "", appetite_body, "",
        "## Riskiest assumption", "", riskiest_body, "",
    ]
    return "\n".join(fm + body)


class TestAppetiteCompletenessCheck(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.adr_dir = Path(self._tmp.name) / "architecture" / "adr"
        self.adr_dir.mkdir(parents=True)
        self.tickets = Path(self._tmp.name) / "tickets"
        self.now = datetime(2026, 9, 10, tzinfo=timezone.utc)

    def _write_adr(self, name: str, text: str) -> None:
        (self.adr_dir / name).write_text(text, encoding="utf-8")

    def test_complete_adr_not_flagged(self) -> None:
        self._write_adr("ORG-100-x.md", _adr_text(
            adr_id="ORG-100", status="**Ratified 2026-09-08 by the operator**",
            appetite="7",
            appetite_body="This is worth a medium bet; ceiling is one ADR + surfaces.",
            riskiest_body="The pass degrades into ritual; cheap test is the 90-day re-eval."))
        findings = tg._appetite_completeness_check([self.adr_dir], self.tickets, self.now)
        self.assertEqual(findings, [])

    def test_missing_appetite_field_flagged(self) -> None:
        self._write_adr("ORG-101-x.md", _adr_text(
            adr_id="ORG-101", status="**Ratified 2026-09-08 by the operator**",
            appetite=None,
            appetite_body="A substantive appetite paragraph about the bet size here.",
            riskiest_body="A substantive riskiest-assumption paragraph with a cheap test."))
        findings = tg._appetite_completeness_check([self.adr_dir], self.tickets, self.now)
        self.assertIn("appetite-field", {f.reason for f in findings})

    def test_boilerplate_sections_flagged(self) -> None:
        self._write_adr("ORG-102-x.md", _adr_text(
            adr_id="ORG-102", status="**Ratified 2026-09-08 by the operator**",
            appetite="5", appetite_body="TODO", riskiest_body="TBD"))
        reasons = {f.reason for f in tg._appetite_completeness_check(
            [self.adr_dir], self.tickets, self.now)}
        self.assertIn("appetite-section", reasons)
        self.assertIn("riskiest-section", reasons)

    def test_legacy_adr_grandfathered_by_ratify_date(self) -> None:
        self._write_adr("ORG-050-x.md", _adr_text(
            adr_id="ORG-050", status="**Ratified 2026-05-01 by the operator**",
            appetite=None, appetite_body="TODO", riskiest_body="TODO",
            date="2026-05-01"))
        findings = tg._appetite_completeness_check([self.adr_dir], self.tickets, self.now)
        self.assertEqual(findings, [])  # ratified before ORG-089 landing → exempt

    def test_vault_adr_exempt(self) -> None:
        self._write_adr("ADR-011-x.md", _adr_text(
            adr_id="ADR-011", status="**Ratified 2026-09-08 by the operator**",
            appetite=None, appetite_body="TODO", riskiest_body="TODO"))
        findings = tg._appetite_completeness_check([self.adr_dir], self.tickets, self.now)
        self.assertEqual(findings, [])  # ADR-011 < 200 → vault, exempt

    def test_unratified_adr_not_checked(self) -> None:
        self._write_adr("ORG-103-x.md", _adr_text(
            adr_id="ORG-103", status="**Proposed** — awaiting the operator",
            appetite=None, appetite_body="TODO", riskiest_body="TODO"))
        findings = tg._appetite_completeness_check([self.adr_dir], self.tickets, self.now)
        self.assertEqual(findings, [])

    def test_org089_itself_is_not_grandfathered_dogfood(self) -> None:
        # DECISION-10 dogfood: ORG-089 (ratified 2026-09-06, the landing date) is
        # NOT grandfathered — the completeness check evaluates it. (The grandfathering
        # BOUNDARY logic is proven synthetically by
        # test_legacy_adr_grandfathered_by_ratify_date; here we dogfood the real
        # corpus.) ORG-089 is the ADR that INTRODUCED the appetite requirement, so it
        # must satisfy its own rule: frontmatter `appetite: 7` (landed with the ratify,
        # 2026-09-06) + substantive §Appetite / §Riskiest assumption body sections.
        # The check therefore produces NO finding for ORG-089 — the requirement's
        # author dogfoods clean.
        # (History: the frontmatter appetite: field was initially absent across the
        # corpus; the completeness check correctly flagged ORG-089 during the T-3440
        # build, which was bubbled up and fixed by landing appetite: 7 on ORG-089.)
        repo_adr = Path(__file__).resolve().parents[1] / "architecture" / "adr"
        findings = tg._appetite_completeness_check([repo_adr], repo_adr.parent / "tickets",
                                                   self.now)
        org089 = {f.reason for f in findings if f.adr_id == "ORG-089"}
        self.assertEqual(org089, set(),
                         "ORG-089 dogfoods its own requirement: appetite: 7 frontmatter "
                         "+ substantive body sections → no completeness finding")


class TestAppetiteOverrunCheck(_TicketsRootFixture):
    def setUp(self) -> None:
        super().setUp()
        self.policy_dir = self.root / "rulebook" / "policies"
        self.policy_dir.mkdir(parents=True)
        self.policy_path = self.policy_dir / "appetite-time-budget.yaml"
        self.now = datetime(2026, 9, 6, tzinfo=timezone.utc)

    def _generate_only(self) -> None:
        self.policy_path.write_text(
            "mode: generate-only\ncalibration_version: 0\nbands: {}\n", encoding="utf-8")

    def _active(self) -> None:
        self.policy_path.write_text(
            "mode: active\ncalibration_version: 1\nbands:\n  5: 3600\n", encoding="utf-8")

    def _overrun_ticket(self) -> None:
        # claimed 10h ago, still active, appetite 5 (budget 3600s) → overrun.
        self._write("active", tid="T-0400", appetite=5, transition_lines=[
            "- 2026-09-05T15:00:00Z [metis] claimed",
        ])

    def test_silent_when_policy_absent(self) -> None:
        self.assertEqual(
            tg._appetite_overrun_check(self.policy_path, self.tickets, self.now), [])

    def test_silent_in_generate_only(self) -> None:
        self._generate_only()
        self._overrun_ticket()
        found = tg._appetite_overrun_check(self.policy_path, self.tickets, self.now,
                                           apply=True, send_fn=lambda *a, **k: None)
        self.assertEqual(found, [])
        # no substate written
        f, _ = ticket.parse_front_matter(
            (self.tickets / "active" / "T-0400.md").read_text(encoding="utf-8"))
        self.assertNotIn("substate", f)

    def test_fires_in_active_mode_writes_substate_and_advises(self) -> None:
        self._active()
        self._overrun_ticket()
        sent = []
        found = tg._appetite_overrun_check(
            self.policy_path, self.tickets, self.now, apply=True,
            send_fn=lambda inbox, payload, **kw: sent.append((inbox, kw.get("subject"))))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].tid, "T-0400")
        f, _ = ticket.parse_front_matter(
            (self.tickets / "active" / "T-0400.md").read_text(encoding="utf-8"))
        self.assertEqual(f["substate"], "appetite-overrun")
        # advisory sent to claimant (metis) + orchestrator
        self.assertTrue(sent)

    def test_missing_appetite_never_fires(self) -> None:
        self._active()
        self._write("active", tid="T-0401", transition_lines=[
            "- 2026-09-05T15:00:00Z [metis] claimed"])  # no appetite
        found = tg._appetite_overrun_check(self.policy_path, self.tickets, self.now,
                                           apply=True, send_fn=lambda *a, **k: None)
        self.assertEqual(found, [])  # fail-safe: missing appetite → no fire

    def test_under_budget_not_flagged(self) -> None:
        self._active()
        self._write("active", tid="T-0402", appetite=5, transition_lines=[
            f"- {tg._now_iso(self.now)} [metis] claimed"])  # just claimed → 0s
        found = tg._appetite_overrun_check(self.policy_path, self.tickets, self.now,
                                           apply=True, send_fn=lambda *a, **k: None)
        self.assertEqual(found, [])


class TestAppetiteOutcomeLog(unittest.TestCase):
    def test_appends_jsonl_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ledger" / "appetite"
            now = datetime(2026, 9, 6, tzinfo=timezone.utc)
            tg._appetite_outcome_log(tid="T-1", appetite=7, outcome="shipped",
                                     now=now, active_spend_s=1234, ledger_root=root)
            shard = root / tg._iso_week_shard_name(now)
            self.assertTrue(shard.exists())
            event = json.loads(shard.read_text().strip())
            self.assertEqual(event["tid"], "T-1")
            self.assertEqual(event["outcome"], "shipped")
            self.assertEqual(event["appetite"], 7)
            self.assertEqual(event["active_spend_s"], 1234)

    def test_never_raises_on_bad_root(self) -> None:
        # A file where a dir is expected → mkdir fails → swallowed, no raise.
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocker"
            blocker.write_text("x")
            tg._appetite_outcome_log(tid="T-2", appetite=None, outcome="dropped",
                                     now=datetime.now(timezone.utc),
                                     ledger_root=blocker / "sub")


class TestInboxOverrunSkip(_TicketsRootFixture):
    def test_appetite_overrun_ticket_is_skipped(self) -> None:
        from tools import inbox_consumer
        self._write("inbox", tid="T-0500", appetite=5, substate="appetite-overrun")
        with patch.object(inbox_consumer, "TICKETS_ROOT", self.tickets):
            with self.assertRaises(inbox_consumer.SkipCycle) as ctx:
                inbox_consumer.list_eligible_tickets("metis")
        self.assertEqual(str(ctx.exception), "appetite_overrun_blocked")

    def test_overrun_skip_is_never_alert(self) -> None:
        from tools import inbox_consumer
        self.assertIn("appetite_overrun_blocked",
                      inbox_consumer.NEVER_ALERT_SKIP_VOCABS)

    def test_normal_ticket_still_eligible(self) -> None:
        from tools import inbox_consumer
        self._write("inbox", tid="T-0501", appetite=5)
        with patch.object(inbox_consumer, "TICKETS_ROOT", self.tickets):
            eligible = inbox_consumer.list_eligible_tickets("metis")
        self.assertEqual([t.id for t in eligible], ["T-0501"])


class TestDigestWiring(unittest.TestCase):
    def test_appetite_sections_surface_in_digest(self) -> None:
        digest = tg.build_digest(
            "2026-09-06", [], {"active": 0},
            appetite_completeness_findings=[
                tg.AppetiteCompletenessFinding("ORG-089", "appetite-field")],
            appetite_overrun_findings=[
                tg.AppetiteOverrunFinding("T-9", 5, 99999, 3600, "metis")],
            dropped_this_week=[
                tg.DroppedTicket("T-8", "a bet", "not worth it",
                                 datetime(2026, 9, 6, tzinfo=timezone.utc))],
        )
        self.assertIn("ORG-089", digest)
        self.assertIn("appetite-field", digest)
        self.assertIn("Appetite-budget overruns", digest)
        self.assertIn("Dropped this week", digest)

    def test_empty_appetite_findings_add_no_sections(self) -> None:
        digest = tg.build_digest("2026-09-06", [], {"active": 0})
        self.assertNotIn("Appetite-budget overruns", digest)
        self.assertNotIn("Dropped this week", digest)


if __name__ == "__main__":
    unittest.main()
