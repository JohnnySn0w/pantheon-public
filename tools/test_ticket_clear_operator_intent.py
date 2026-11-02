"""T-5522: `clear-operator-intent` -- HO-only clear of a STALE operator_intent.

Layer-3: real tmp git repo, reusing the T-5015 decision-register fixture
(tools/test_ticket_decision_register.py). The register and tickets live in
the tmp repo; the real repo is never touched (no_push=True throughout).

Run: python3 -m unittest tools.test_ticket_clear_operator_intent -v
"""

from __future__ import annotations

import argparse
import os
import re
import unittest
from unittest.mock import patch

from tools import ticket
from tools.test_ticket_decision_register import FULL, RegisterGateTest, _args, _git

TID = "T-9101"
VERB = "clear-operator-intent"


def _stale_text(tid: str, *, target: str = "role:engineering_orchestrator",
                operator_intent: str | None = "action", intent: str | None = None) -> str:
    fm = [
        "---", f"id: {tid}", "created: 2026-10-14T10:00:00Z", "requester: orchestrator",
        f"target: {target}", "state: active", "parent_ticket: null", "depends_on: []",
        "labels: [test]",
    ]
    if intent:
        fm.append(f"intent: {intent}")
    if operator_intent:
        fm.append(f"operator_intent: {operator_intent}")
    fm += ["---", "", f"# Title for {tid}", "", "## Ask", "", "Do it.", "",
           "## Transition log", "", "- 2026-10-14T10:00:00Z [orchestrator] filed in inbox", ""]
    return "\n".join(fm)


def _cargs(tid: str = TID, **kw) -> argparse.Namespace:
    d = dict(ticket=tid, decided_by="orchestrator", ref="T-5522 note 2026-10-27",
             why="pulled to agent tier and decided there", no_push=True)
    d.update(kw)
    return argparse.Namespace(**d)


class ClearOperatorIntentTest(unittest.TestCase):
    setUp = RegisterGateTest.setUp
    tearDown = RegisterGateTest.tearDown
    _run = RegisterGateTest._run
    _register = RegisterGateTest._register
    _rows = RegisterGateTest._rows

    def _put(self, tid: str = TID, state: str = "active", **kw) -> None:
        path = self.repo / "tickets" / state / f"{tid}.md"
        path.write_text(_stale_text(tid, **kw), encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", f"add {tid}")

    def _path(self, tid: str = TID, state: str = "active"):
        return self.repo / "tickets" / state / f"{tid}.md"

    def _head(self) -> str:
        return _git(self.repo, "rev-parse", "HEAD").strip()

    def _assert_refused(self, args, needle: str, *, state: str = "active") -> None:
        before = self._path(args.ticket, state).read_bytes()
        head = self._head()
        with self.assertRaises(ticket.TicketRefusal) as cm:
            self._run(ticket.cmd_clear_operator_intent, args)
        self.assertIn(needle, str(cm.exception))
        self.assertEqual(self._path(args.ticket, state).read_bytes(), before)
        self.assertEqual(self._head(), head)

    # -- actor / role ------------------------------------------------------
    def test_non_ho_actor_refused(self) -> None:
        self._put()
        for actor in ("mason", "artisan", "engineering_orchestrator", "specialist", "operator"):
            with self.subTest(actor=actor), patch.dict(os.environ, {"PANTHEON_ACTOR": actor}):
                self._assert_refused(_cargs(), "not the head engineering orchestrator")

    def test_os_user_non_ho_refused_when_actor_unset(self) -> None:
        self._put()
        env = {k: v for k, v in os.environ.items() if k != "PANTHEON_ACTOR"}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(ticket.getpass, "getuser", return_value="suborch1"):
            self.assertEqual(ticket._claiming_persona(), "mason")
            self._assert_refused(_cargs(), "'mason'")

    def test_ho_role_id_actor_accepted(self) -> None:
        self._put()
        with patch.dict(os.environ, {"PANTHEON_ACTOR": "head_engineering_orchestrator"}):
            self.assertEqual(self._run(ticket.cmd_clear_operator_intent, _cargs()), 0)

    def test_ho_os_user_accepted_when_actor_unset(self) -> None:
        self._put()
        env = {k: v for k, v in os.environ.items() if k != "PANTHEON_ACTOR"}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(ticket.getpass, "getuser", return_value="claude"):
            self.assertEqual(self._run(ticket.cmd_clear_operator_intent, _cargs()), 0)

    # -- flags -------------------------------------------------------------
    def test_empty_flags_refused(self) -> None:
        self._put()
        for flag, dest in (("--decided-by", "decided_by"), ("--ref", "ref"), ("--why", "why")):
            for bad in ("", "   ", None):
                with self.subTest(flag=flag, bad=bad):
                    self._assert_refused(_cargs(**{dest: bad}), f"empty {flag}")

    def test_argparse_requires_all_three(self) -> None:
        parser = ticket._build_parser()
        for drop in ("--decided-by", "--ref", "--why"):
            argv = [VERB, TID, "--decided-by", "orchestrator", "--ref", "r", "--why", "w"]
            i = argv.index(drop)
            del argv[i:i + 2]
            with self.subTest(drop=drop), self.assertRaises(SystemExit), \
                    patch("sys.stderr"):
                parser.parse_args(argv)

    def test_decided_by_operator_refused_outright(self) -> None:
        self._put()
        for who in ("operator", "the operator", "OPERATOR", " operator ", "role:operator"):
            with self.subTest(who=who):
                # even with a TG anchor: a real ruling goes through resolve --ruling
                self._assert_refused(_cargs(decided_by=who, ref="TG 11474"), "--ruling")

    # -- ticket-state refusals ---------------------------------------------
    def test_no_operator_intent_refused(self) -> None:
        self._put(operator_intent=None)
        self._assert_refused(_cargs(), "no operator_intent")

    def test_target_operator_refused(self) -> None:
        self._put(target="operator")
        self._assert_refused(_cargs(), "target: operator")

    def test_live_intent_refused(self) -> None:
        self._put(intent="ratify")
        self._assert_refused(_cargs(), "live intent")

    def test_done_refused(self) -> None:
        self._put(state="done")
        self._assert_refused(_cargs(), "done/", state="done")

    def test_second_call_refused(self) -> None:
        self._put()
        self.assertEqual(self._run(ticket.cmd_clear_operator_intent, _cargs()), 0)
        self._assert_refused(_cargs(), "no operator_intent")

    # -- success -----------------------------------------------------------
    def test_success_removes_field_and_logs_one_line(self) -> None:
        self._put(operator_intent="ratify")
        before, _ = ticket.parse_front_matter(self._path().read_text(encoding="utf-8"))
        head = self._head()
        self.assertEqual(self._run(ticket.cmd_clear_operator_intent, _cargs()), 0)
        text = self._path().read_text(encoding="utf-8")
        after, body = ticket.parse_front_matter(text)
        self.assertNotIn("operator_intent", after)
        expected = dict(before)
        expected.pop("operator_intent")
        self.assertEqual(after, expected)  # nothing else in the frontmatter changed
        log = [l for l in body.splitlines() if f"] {VERB}" in l]
        self.assertEqual(len(log), 1)
        # ticket.py's shared entry format: "- <ts> [<actor>] <verb> @<head-sha>; <detail>"
        self.assertRegex(log[0], re.compile(
            r"^- \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \[orchestrator\] clear-operator-intent "
            r"@[0-9a-f]+; operator_intent=ratify cleared; decided-by=orchestrator; "
            r"ref=T-5522 note 2026-10-27; why=pulled to agent tier and decided there$"))
        self.assertEqual(_git(self.repo, "rev-list", "--count", f"{head}..HEAD").strip(), "1")
        msg = _git(self.repo, "log", "-1", "--format=%B")
        self.assertTrue(msg.startswith(f"tickets: {VERB} {TID}\n"))
        self.assertIn("decided-by=orchestrator; ref=T-5522 note 2026-10-27", msg)

    # -- register gate interaction -----------------------------------------
    def test_resolve_after_clear_needs_no_flags_and_writes_no_row(self) -> None:
        self._put()
        self.assertEqual(self._run(ticket.cmd_clear_operator_intent, _cargs()), 0)
        register_before = self._register()
        self.assertEqual(self._run(ticket.cmd_resolve, _args(TID)), 0)
        self.assertTrue(self._path(state="done").is_file())
        self.assertEqual(self._register(), register_before)
        self.assertNotIn(TID, self._register())

    def test_gate_still_fires_on_uncleared_ticket(self) -> None:
        self._put()
        with self.assertRaises(ticket.TicketRefusal) as cm:
            self._run(ticket.cmd_resolve, _args(TID))
        self.assertIn("--ruling", str(cm.exception))
        self.assertEqual(self._run(ticket.cmd_resolve, _args(TID, **FULL)), 0)
        self.assertEqual(len(self._rows()), 2)

    def test_round_trip_retarget_to_operator_is_fresh_escalation(self) -> None:
        self._put(operator_intent="ratify")
        self.assertEqual(self._run(ticket.cmd_clear_operator_intent, _cargs()), 0)
        rt = argparse.Namespace(ticket=TID, to="operator", note="re-escalate", no_push=True,
                                intent=None, keep_state=True, serves=None,
                                ratifiable_file=None, no_file="x" * 25)
        self._run(ticket.cmd_retarget, rt)
        fields, _ = ticket.parse_front_matter(self._path().read_text(encoding="utf-8"))
        # fresh default (no ratifiable_files -> action), NOT the cleared "ratify"
        self.assertEqual(fields.get("intent"), "action")
        self.assertNotIn("operator_intent", fields)


if __name__ == "__main__":
    unittest.main()
