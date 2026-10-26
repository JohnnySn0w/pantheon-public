"""T-5015 P1: close-path gate + the operator decision register writer.

Layer-3: real tmp git repo (same pattern as test_ticket_ratify_lift.py). The
register lives in the tmp repo; the real notes/ file is never touched.

Run: python3 -m unittest tools.test_ticket_decision_register -v
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import _git_utils, ticket

REGISTER_REL = "notes/operator-decision-register.md"
REGISTER_SEED = (
    "# the operator decision register\n\n"
    "| Date | Ticket | Ask (short) | Ruling | Category | Why it reached the operator "
    "| Rule it implies | Delegable next time? |\n"
    "|---|---|---|---|---|---|---|---|\n"
    "| 2026-10-14 | T-0001 | seed | \"ok\" | product | why | rule | Yes |\n"
    "\n## Open questions\n- none\n"
)


def _git(repo: Path, *a: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True)
    return r.stdout


def _ticket_text(tid: str, *, intent: str | None, target: str = "operator",
                 requester: str = "orchestrator") -> str:
    fm = [
        "---", f"id: {tid}", "created: 2026-10-14T10:00:00Z", f"requester: {requester}",
        f"target: {target}", "state: active", "parent_ticket: null", "depends_on: []",
        "labels: [test]",
    ]
    if intent:
        fm.append(f"intent: {intent}")
    fm += ["---", "", f"# Title for {tid}", "", "## Ask", "", "Do it.", "",
           "## Transition log", "", "- 2026-10-14T10:00:00Z [orchestrator] filed in inbox", ""]
    return "\n".join(fm)


def _args(tid: str, **kw) -> argparse.Namespace:
    d = dict(ticket=tid, with_summary=None, no_push=True, ruling=None, implies=None,
             delegable=None, category=None, why=None, receipt=None,
             spawn_to=None, as_action=False)
    d.update(kw)
    return argparse.Namespace(**d)


FULL = dict(ruling='"yes" (TG 1)', implies="rule X", delegable="yes", category="access")


class RegisterGateTest(unittest.TestCase):
    def setUp(self) -> None:
        from tools._test_fixtures import setup_integration_env
        self._env = setup_integration_env()
        self.repo = self._env.repo_path
        for s in ticket.STATES:
            (self.repo / "tickets" / s).mkdir(parents=True, exist_ok=True)
        (self.repo / "tickets" / "README.md").write_text(
            "# Tickets\n\n## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n|---|---|---|---|---|\n"
            "\n## Related\n\n- Link\n", encoding="utf-8")
        (self.repo / "notes").mkdir()
        (self.repo / REGISTER_REL).write_text(REGISTER_SEED, encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "seed")

        real_git = _git_utils._git
        repo = self.repo

        def in_tmp(*a: str, cwd=None):
            return real_git(*a, cwd=repo)

        self._patches = [
            patch.object(ticket, "REPO_ROOT", repo),
            patch.object(_git_utils, "REPO_ROOT", repo),
            patch.object(ticket, "TICKETS_ROOT", repo / "tickets"),
            patch.object(_git_utils, "_git", in_tmp),
            patch.object(ticket, "_git", in_tmp),
            patch.dict(os.environ, {"PANTHEON_ACTOR": "orchestrator"}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        from tools._test_fixtures import teardown_integration_env
        for p in reversed(self._patches):
            p.stop()
        teardown_integration_env(self._env)

    def _file(self, tid: str, **kw) -> None:
        path = self.repo / "tickets" / "active" / f"{tid}.md"
        path.write_text(_ticket_text(tid, **kw), encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", f"add {tid}")

    def _run(self, fn, args) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(args)

    def _register(self) -> str:
        return (self.repo / REGISTER_REL).read_text(encoding="utf-8")

    def _rows(self) -> list[str]:
        return [l for l in self._register().splitlines()
                if l.startswith("| 20")]

    # -- refusal -----------------------------------------------------------
    def test_refuses_without_flags_each_verb(self) -> None:
        cases = [("T-9001", "ratify", ticket.cmd_resolve),
                 ("T-9010", "action", ticket.cmd_close_action),
                 ("T-9020", "noodle", ticket.cmd_close_noodle)]
        for tid, intent, fn in cases:
            with self.subTest(verb=fn.__name__):
                self._file(tid, intent=intent)
                head = _git(self.repo, "rev-parse", "HEAD")
                with self.assertRaises(ticket.TicketRefusal) as cm:
                    self._run(fn, _args(tid))
                self.assertIn("--ruling", str(cm.exception))
                self.assertTrue((self.repo / "tickets/active" / f"{tid}.md").exists())
                self.assertEqual(_git(self.repo, "rev-parse", "HEAD"), head)
                self.assertEqual(len(self._rows()), 1)

    def test_partial_flags_name_the_missing_ones(self) -> None:
        self._file("T-9001", intent="ratify")
        with self.assertRaises(ticket.TicketRefusal) as cm:
            self._run(ticket.cmd_resolve, _args("T-9001", ruling="x"))
        self.assertIn("--implies", str(cm.exception))
        self.assertNotIn("Missing: --ruling", str(cm.exception))

    def test_unclassified_category_not_accepted_from_flag(self) -> None:
        self._file("T-9001", intent="ratify")
        with self.assertRaises(ticket.TicketRefusal):
            self._run(ticket.cmd_resolve,
                      _args("T-9001", **{**FULL, "category": "unclassified"}))

    def test_missing_register_refuses_before_mutation(self) -> None:
        self._file("T-9001", intent="ratify")
        (self.repo / REGISTER_REL).unlink()
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "rm register")
        head = _git(self.repo, "rev-parse", "HEAD")
        with self.assertRaises(ticket.TicketError):
            self._run(ticket.cmd_resolve, _args("T-9001", **FULL))
        self.assertTrue((self.repo / "tickets/active/T-9001.md").exists())
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD"), head)

    def test_register_without_table_refuses_before_mutation(self) -> None:
        self._file("T-9001", intent="ratify")
        (self.repo / REGISTER_REL).write_text("# no table\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "break register")
        with self.assertRaises(ticket.TicketError):
            self._run(ticket.cmd_resolve, _args("T-9001", **FULL))
        self.assertTrue((self.repo / "tickets/active/T-9001.md").exists())

    # -- success + same commit --------------------------------------------
    def test_row_written_in_same_commit_each_verb(self) -> None:
        cases = [("T-9001", "ratify", ticket.cmd_resolve),
                 ("T-9010", "action", ticket.cmd_close_action),
                 ("T-9020", "noodle", ticket.cmd_close_noodle)]
        for tid, intent, fn in cases:
            with self.subTest(verb=fn.__name__):
                self._file(tid, intent=intent, requester="orchestrator")
                before = _git(self.repo, "rev-list", "--count", "HEAD").strip()
                self.assertEqual(self._run(fn, _args(tid, **FULL)), 0)
                after = _git(self.repo, "rev-list", "--count", "HEAD").strip()
                self.assertEqual(int(after), int(before) + 1)
                names = _git(self.repo, "show", "--name-only", "--format=", "HEAD").split()
                self.assertIn(REGISTER_REL, names)
                self.assertIn(f"tickets/done/{tid}.md", names)
                last = self._rows()[-1]
                cols = [c.strip() for c in last.strip("|").split(" | ")]
                self.assertEqual(len(cols), 8)
                self.assertEqual(cols[1], tid)
                self.assertEqual(cols[2], f"Title for {tid}")
                self.assertEqual(cols[3], '"yes" (TG 1)')
                self.assertEqual(cols[4], "access")
                self.assertEqual(cols[5], intent)  # --why defaults to intent
                self.assertEqual(cols[6], "rule X")
                self.assertEqual(cols[7], "yes")
        # appended after the last table row, before the trailing prose
        txt = self._register()
        self.assertLess(txt.index("T-9020"), txt.index("## Open questions"))

    def test_why_override(self) -> None:
        self._file("T-9001", intent="ratify")
        self._run(ticket.cmd_resolve, _args("T-9001", why="custom why", **FULL))
        self.assertIn("| custom why |", self._rows()[-1])

    # -- retarget ----------------------------------------------------------
    def test_retargeted_carrier_still_gated(self) -> None:
        self._file("T-9001", intent="ratify", target="role:engineering_orchestrator")
        with self.assertRaises(ticket.TicketRefusal):
            self._run(ticket.cmd_resolve, _args("T-9001"))
        self.assertEqual(self._run(ticket.cmd_resolve, _args("T-9001", **FULL)), 0)
        self.assertEqual(len(self._rows()), 2)

    # -- operator fallback --------------------------------------------------
    def test_operator_actor_fallback_never_fails(self) -> None:
        self._file("T-9001", intent="ratify")
        long = "Rejected: " + "z" * 400
        with patch.dict(os.environ, {"PANTHEON_ACTOR": "operator"}):
            rc = self._run(ticket.cmd_resolve, _args("T-9001", with_summary=long))
        self.assertEqual(rc, 0)
        cols = [c.strip() for c in self._rows()[-1].strip("|").split(" | ")]
        self.assertTrue(cols[3].startswith("resolve by the operator (PWA): Rejected: "))
        self.assertLessEqual(len(cols[3]), len("resolve by the operator (PWA): ") + 200)
        self.assertEqual(cols[4], "unclassified")
        self.assertEqual(cols[6], "PENDING")
        self.assertEqual(cols[7], "unknown")
        names = _git(self.repo, "show", "--name-only", "--format=", "HEAD").split()
        self.assertIn(REGISTER_REL, names)

    def test_operator_fallback_no_summary_text(self) -> None:
        self._file("T-9010", intent="action", requester="orchestrator")
        with patch.dict(os.environ, {"PANTHEON_ACTOR": "operator"}):
            self.assertEqual(self._run(ticket.cmd_close_action, _args("T-9010")), 0)
        self.assertIn("| close-action by the operator (PWA) |", self._rows()[-1])

    # -- non-intent --------------------------------------------------------
    def test_non_intent_ticket_unaffected(self) -> None:
        self._file("T-9001", intent=None, target="role:engineering_orchestrator")
        before = self._register()
        self.assertEqual(self._run(ticket.cmd_resolve, _args("T-9001")), 0)
        self.assertEqual(self._register(), before)
        names = _git(self.repo, "show", "--name-only", "--format=", "HEAD").split()
        self.assertNotIn(REGISTER_REL, names)

    # -- escaping ----------------------------------------------------------
    def test_pipe_escaping(self) -> None:
        self._file("T-9001", intent="ratify")
        self._run(ticket.cmd_resolve,
                  _args("T-9001", **{**FULL, "ruling": "a | b\nc", "implies": "x|y"}))
        row = self._rows()[-1]
        self.assertIn("a \\| b c", row)
        self.assertIn("x\\|y", row)
        self.assertEqual(len(row.replace("\\|", "").strip("|").split("|")), 8)

    # -- retarget provenance (coordinator amendment) -----------------------
    def test_real_retarget_away_from_operator_keeps_gate(self) -> None:
        self._file("T-9001", intent="ratify", target="operator")
        _git(self.repo, "add", "-A")
        rt = argparse.Namespace(ticket="T-9001", to="role:engineering_orchestrator", note="carrier", no_push=True,
                                intent=None, keep_state=True, serves=None,
                                ratifiable_file=None, no_file="x" * 25)
        self._run(ticket.cmd_retarget, rt)
        text = (self.repo / "tickets/active/T-9001.md").read_text(encoding="utf-8")
        fields, _ = ticket.parse_front_matter(text)
        self.assertNotIn("intent", fields)
        self.assertEqual(fields.get("operator_intent"), "ratify")
        with self.assertRaises(ticket.TicketRefusal) as cm:
            self._run(ticket.cmd_resolve, _args("T-9001"))
        self.assertIn("--ruling", str(cm.exception))
        self.assertEqual(self._run(ticket.cmd_resolve, _args("T-9001", **FULL)), 0)
        self.assertEqual(len(self._rows()), 2)
        self.assertIn("| ratify |", self._rows()[-1].replace("| access |", "|"))  # why=ratify

    def test_retarget_back_to_operator_clears_operator_intent(self) -> None:
        self._file("T-9001", intent="action", target="operator")
        for to in ("role:engineering_orchestrator", "operator"):
            rt = argparse.Namespace(ticket="T-9001", to=to, note="x", no_push=True,
                                    intent=None, keep_state=True, serves=None,
                                ratifiable_file=None, no_file="x" * 25)
            self._run(ticket.cmd_retarget, rt)
        fields, _ = ticket.parse_front_matter(
            (self.repo / "tickets/active/T-9001.md").read_text(encoding="utf-8"))
        self.assertEqual(fields.get("intent"), "action")
        self.assertNotIn("operator_intent", fields)


if __name__ == "__main__":
    unittest.main()
