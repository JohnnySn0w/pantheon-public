"""T-4496 / T-4499 / T-4494 — ticket.py tells a caller's mistake (TicketRefusal,
exit 3) from its own fault (TicketError, exit 1) by EXIT CODE, refuses junk at
file time, and its help text matches what it enforces.

Run from repo root:
    python3 -m unittest tools.test_ticket_refusal_exit_codes -v

Every test drives `ticket.main(argv)` through the REAL parser against a temp
repo (TICKETS_ROOT / REPO_ROOT patched, allocation lockfile redirected, git
mocked), so the exit code and the `error: ...` stderr line — the contract a
wrapper keys on — are what is asserted, not an internal raise.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import _git_utils, ticket
from tools import ticket_purpose as tp


def _error_line(err: str) -> str:
    """The `error: ...` line (stderr may carry warnings before it)."""
    lines = [l for l in err.splitlines() if l.startswith("error: ")]
    assert len(lines) == 1, err
    return lines[0]


def _run_main(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = ticket.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class _Hermetic(unittest.TestCase):
    """A temp repo root: tickets/{inbox,active,done}, no real git, no real lock."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.tickets = self.root / "tickets"
        for state in ("inbox", "active", "done"):
            (self.tickets / state).mkdir(parents=True)
        for target, attr, value in (
            (ticket, "TICKETS_ROOT", self.tickets),
            (ticket, "REPO_ROOT", self.root),
            (_git_utils, "REPO_ROOT", self.root),
        ):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        env = patch.dict(
            os.environ, {"PANTHEON_TICKET_ALLOC_LOCKFILE": str(self.root / "alloc.lock")}
        )
        env.start()
        self.addCleanup(env.stop)

    def _write_ticket(self, state: str, tid: str) -> Path:
        path = self.tickets / state / f"{tid}.md"
        path.write_text(f"---\nid: {tid}\n---\n", encoding="utf-8")
        return path

    def _inbox_files(self) -> list[str]:
        return sorted(p.name for p in (self.tickets / "inbox").glob("T-*.md"))


class _FileFlow(_Hermetic):
    """_Hermetic + the git-touching steps of `file` stubbed, so a request either
    refuses on its own merits or files into the temp tree."""

    def setUp(self) -> None:
        super().setUp()
        for name, kwargs in (
            ("_assert_cli_checkout_allowed", {}),
            ("_next_ticket_id", {"return_value": "T-0099"}),
            ("_pull_if_needed", {}),
            ("_commit_and_push", {}),
        ):
            p = patch.object(ticket, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    # A request that satisfies every file-time gate on its own merits (the
    # automated-filer shape: hygiene serves + a typed purpose, so no warnings).
    FILE_OK = (
        "file", "--requester=orchestrator", "--target=role:system_orchestrator", "--title=t",
        "--serves=goal-neutral-maintenance", "--project-id=null",
        "--nearest-intent-ref=maintenance:ticket-board",
    )


# ---------------------------------------------------------------------------
# T-4496 — the exit-code split.
# ---------------------------------------------------------------------------


class TestExitCodeContract(unittest.TestCase):
    def test_refusal_is_a_ticket_error_with_the_distinct_documented_code(self) -> None:
        self.assertTrue(issubclass(ticket.TicketRefusal, ticket.TicketError))
        self.assertEqual(ticket.TicketError.exit_code, 1)
        self.assertEqual(ticket.TicketRefusal.exit_code, 3)
        # 3 must collide with neither argparse's usage code nor the fault code.
        self.assertNotIn(ticket.EXIT_REFUSAL, (0, 1, 2))

    def test_appetite_gates_are_refusals(self) -> None:
        self.assertTrue(issubclass(ticket.AppetiteRequired, ticket.TicketRefusal))
        self.assertTrue(issubclass(ticket.AppetiteOverrunNotResolvable, ticket.TicketRefusal))
        # T-4680: the first-use SOP capture gate is a refusal too.
        self.assertTrue(issubclass(ticket.SopStubNotFilled, ticket.TicketRefusal))

    def test_help_documents_the_exit_codes(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            ticket.main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        text = " ".join(out.getvalue().split())
        self.assertIn("1 = fault", text)
        self.assertIn("2 = usage error", text)
        self.assertIn("3 = refusal", text)
        # The conflict ruling (T-4496): a stale-view conflict is a refusal.
        self.assertIn("stale-view conflict", text)
        self.assertIn("is a refusal too", text)


class TestCallerRefusalsExitThree(_FileFlow):
    """One test per refusal class: the caller's request was invalid -> exit 3,
    with the `error: ...` line exactly as before."""

    def _assert_refusal(self, argv: tuple[str, ...], marker: str) -> None:
        rc, out, err = _run_main(*argv)
        self.assertEqual(rc, ticket.EXIT_REFUSAL, f"stdout={out!r} stderr={err!r}")
        self.assertIn(marker, _error_line(err))
        self.assertEqual(out, "")

    def test_orphan_lineage(self) -> None:
        self._assert_refusal(
            ("file", "--requester=orchestrator", "--target=role:system_orchestrator", "--title=t"),
            "orphan",
        )

    def test_appetite_required(self) -> None:
        self._assert_refusal(
            ("file", "--requester=orchestrator", "--target=role:system_orchestrator", "--title=t",
             "--serves=ship a thing"),
            "appetite required",
        )

    def test_bad_parent_ticket_format(self) -> None:
        self._assert_refusal(
            ("file", "--requester=orchestrator", "--target=role:system_orchestrator", "--title=t",
             "--parent-ticket=banana"),
            "T-NNNN format",
        )

    def test_unknown_target(self) -> None:
        self._assert_refusal(
            ("file", "--requester=orchestrator", "--target=nobody", "--title=t",
             "--serves=goal-neutral-maintenance"),
            "unknown target",
        )

    def test_bad_value_shape_not_before(self) -> None:
        self._assert_refusal(
            ("file", "--requester=orchestrator", "--target=role:system_orchestrator", "--title=t",
             "--serves=goal-neutral-maintenance", "--not-before=banana"),
            "not_before",
        )

    def test_flag_combination_on_block(self) -> None:
        self._assert_refusal(("block", "T-0001"), "must specify at least one")

    def test_invalid_ticket_id(self) -> None:
        self._assert_refusal(("annotate", "banana", "--note=x"), "invalid ticket id")

    def test_ticket_not_found(self) -> None:
        self._assert_refusal(("annotate", "T-9999", "--note=x"), "ticket not found")

    def test_state_mismatch_on_a_done_ticket(self) -> None:
        self._write_ticket("done", "T-0001")
        self._assert_refusal(("annotate", "T-0001", "--note=x"), "state-mismatch")

    def test_a_refusal_writes_nothing(self) -> None:
        self._assert_refusal(
            ("file", "--requester=orchestrator", "--target=role:system_orchestrator", "--title=t"), "orphan"
        )
        self.assertEqual(self._inbox_files(), [])

    def test_a_valid_request_still_exits_zero(self) -> None:
        rc, out, err = _run_main(*self.FILE_OK)
        self.assertEqual((rc, err), (0, ""), err)
        self.assertEqual(self._inbox_files(), ["T-0099.md"])
        self.assertIn("filed T-0099", out)


class TestToolFaultsExitOne(_Hermetic):
    """Infra / state faults are NOT the caller's mistake -> exit 1, same line shape."""

    def test_refusing_to_run_from_a_non_main_branch(self) -> None:
        git = MagicMock(return_value=MagicMock(returncode=0, stdout="agent/x\n", stderr=""))
        with patch.object(ticket, "_git", git):
            rc, _out, err = _run_main("annotate", "T-0001", "--note=x")
        self.assertEqual(rc, ticket.EXIT_FAULT)
        self.assertTrue(_error_line(err).startswith("error: refusing to run ticket.py from"), err)

    def test_git_fetch_failure_while_allocating_an_id(self) -> None:
        def git(*argv, **kwargs):
            if argv[0] == "fetch":
                return MagicMock(returncode=128, stdout="", stderr="fatal: unable to access\n")
            return MagicMock(returncode=0, stdout="main\n", stderr="")

        # T-4885: file now pulls BEFORE allocating; stub the pull so this test
        # still isolates the allocation fetch (and never syncs a real checkout).
        with patch.object(ticket, "_git", side_effect=git), \
                patch.object(ticket, "_pull_if_needed"):
            rc, _out, err = _run_main(*_FileFlow.FILE_OK)
        self.assertEqual(rc, ticket.EXIT_FAULT)
        self.assertTrue(_error_line(err).startswith("error: git fetch failed"), err)

    def test_git_ff_pull_failure_before_a_write(self) -> None:
        not_ahead = (False, "git merge --ff-only failed (not ahead, no rebase): diverged")
        with patch.object(ticket, "_assert_cli_checkout_allowed"), \
                patch.object(ticket, "_next_ticket_id", return_value="T-0099"), \
                patch.object(ticket, "pull_or_rebase_strict", return_value=not_ahead):
            rc, _out, err = _run_main(*_FileFlow.FILE_OK)
        self.assertEqual(rc, ticket.EXIT_FAULT)
        self.assertTrue(_error_line(err).startswith("error: git merge --ff-only failed"), err)
        self.assertEqual(self._inbox_files(), [])


# ---------------------------------------------------------------------------
# T-4499 — junk is refused at file time instead of stored verbatim.
# ---------------------------------------------------------------------------


class TestFileTimeInputValidation(_FileFlow):
    BASE = ("file", "--title=t", "--serves=goal-neutral-maintenance",
            "--project-id=null", "--nearest-intent-ref=maintenance:ticket-board")

    def _file(self, *flags: str) -> tuple[int, str, str]:
        return _run_main(*self.BASE, *flags)

    def _filed_fields(self) -> dict:
        (name,) = self._inbox_files()
        fields, _body = ticket.parse_front_matter(
            (self.tickets / "inbox" / name).read_text(encoding="utf-8")
        )
        return fields

    def _assert_refused_unwritten(self, flags: tuple[str, ...], marker: str) -> None:
        rc, out, err = self._file(*flags)
        self.assertEqual(rc, ticket.EXIT_REFUSAL, f"stdout={out!r} stderr={err!r}")
        self.assertIn(marker, _error_line(err))
        self.assertEqual(self._inbox_files(), [], "a refused request must write nothing")

    # -- --depends-on: each element must be a T-NNNN id (a gate on 'banana'
    #    never unblocks, so it must never be stored).

    def test_depends_on_dash_x_is_refused(self) -> None:
        self._assert_refused_unwritten(
            ("--requester=orchestrator", "--target=role:system_orchestrator", "--depends-on=-x"),
            "--depends-on value '-x' does not match the T-NNNN format",
        )

    def test_depends_on_banana_is_refused(self) -> None:
        self._assert_refused_unwritten(
            ("--requester=orchestrator", "--target=role:system_orchestrator", "--depends-on=banana"),
            "--depends-on value 'banana' does not match the T-NNNN format",
        )

    def test_depends_on_one_bad_element_in_a_list_refuses_the_whole_request(self) -> None:
        self._assert_refused_unwritten(
            ("--requester=orchestrator", "--target=role:system_orchestrator", "--depends-on=T-7001,-x"),
            "--depends-on value '-x' does not match the T-NNNN format",
        )

    def test_depends_on_valid_list_is_stored(self) -> None:
        rc, _out, err = self._file(
            "--requester=orchestrator", "--target=role:system_orchestrator", "--depends-on=T-7001, T-7002"
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._filed_fields()["depends_on"], ["T-7001", "T-7002"])

    def test_parent_ticket_check_is_unchanged(self) -> None:
        self._assert_refused_unwritten(
            ("--requester=orchestrator", "--target=role:system_orchestrator", "--parent-ticket=-x"),
            "--parent-ticket value '-x' does not match the T-NNNN format",
        )

    # -- --requester / --target: the persona/role vocabulary (single source:
    #    agent_role_map.valid_target_values), role-id aliases normalized (T-0492).

    def test_requester_dash_x_is_refused(self) -> None:
        self._assert_refused_unwritten(
            ("--requester=-x", "--target=role:system_orchestrator"), "unknown requester '-x'"
        )

    def test_target_dash_x_is_refused(self) -> None:
        self._assert_refused_unwritten(
            ("--requester=orchestrator", "--target=-x"), "unknown target '-x'"
        )

    def test_requester_outside_the_vocabulary_is_refused(self) -> None:
        for bad in ("test", "implementer (T-0434)", "role:engineering_orchestrator"):
            with self.subTest(requester=bad):
                self._assert_refused_unwritten(
                    (f"--requester={bad}", "--target=role:system_orchestrator"), "unknown requester"
                )

    def test_valid_requester_forms_are_stored_as_the_persona(self) -> None:
        cases = {
            "orchestrator": "orchestrator",
            "operator": "operator",
            "orchestrator": "orchestrator",      # T-0492 role-id alias
            "specialist": "specialist",           # legacy alias
            "consultant": "codex",                  # persona alias
            # T-4044: a bare role-class id is a valid PROVENANCE token (only a
            # TARGET must name a single seat), stored unchanged.
            "engineering_orchestrator": "engineering_orchestrator",
            # An OS account name is a real filer identity (specialist's session files
            # as `claude-research`); it normalizes to the persona it is bound to.
            "claude-research": "specialist",
            "claude": "orchestrator",
        }
        for given, stored in cases.items():
            with self.subTest(requester=given):
                for old in (self.tickets / "inbox").glob("T-*.md"):
                    old.unlink()
                rc, _out, err = self._file(f"--requester={given}", "--target=role:system_orchestrator")
                self.assertEqual(rc, 0, err)
                self.assertEqual(self._filed_fields()["requester"], stored)

    # -- T-4499 follow-up: the requester is matched case-insensitively and
    #    whitespace-tolerantly. The operator's kino form is free text on a phone
    #    keyboard that auto-capitalizes ("the operator"); refusing that fails a live
    #    filer. The STORED value is still the canonical lowercase persona.

    def test_requester_case_and_whitespace_variants_are_stored_as_the_persona(self) -> None:
        cases = {
            "the operator": "operator",
            "OPERATOR": "operator",
            " operator ": "operator",
            "Orchestrator": "orchestrator",
            "Claude-Research": "specialist",      # OS-account lookup is folded too
            "Consultant": "codex",                # persona alias
            "Head_Orchestrator": "orchestrator",    # role-id alias
            "  SCIENCE_TEAM ": "specialist",      # legacy alias, both folds at once
        }
        for given, stored in cases.items():
            with self.subTest(requester=given):
                for old in (self.tickets / "inbox").glob("T-*.md"):
                    old.unlink()
                rc, _out, err = self._file(f"--requester={given}", "--target=role:system_orchestrator")
                self.assertEqual(rc, 0, err)
                self.assertEqual(self._filed_fields()["requester"], stored)

    def test_resolve_requester_normalizes_before_lookup(self) -> None:
        # The function itself (not only the cmd_file strip in front of it).
        for given, stored in (
            ("the operator", "operator"), ("OPERATOR", "operator"), (" operator ", "operator"),
            ("Orchestrator", "orchestrator"), ("\tClaude-Research\n", "specialist"),
        ):
            with self.subTest(requester=given):
                self.assertEqual(ticket._resolve_requester(given), stored)

    def test_requester_junk_is_still_refused_after_normalization(self) -> None:
        for bad in ("-x", "test", "   ", "BOGUS", "Role:Engineering_Orchestrator"):
            with self.subTest(requester=bad):
                self._assert_refused_unwritten(
                    (f"--requester={bad}", "--target=role:system_orchestrator"), "unknown requester"
                )

    def test_requester_refusal_shows_the_callers_original_value(self) -> None:
        with self.assertRaises(ticket.TicketRefusal) as cm:
            ticket._resolve_requester("  BoGuS ")
        # Not the folded 'bogus': the caller must see what they actually typed.
        self.assertIn(repr("  BoGuS "), str(cm.exception))
        self.assertNotIn("'bogus'", str(cm.exception))

    def test_lowercasing_cannot_shadow_or_merge_vocabulary_entries(self) -> None:
        # The fold is only sound while every accepted name is already
        # lowercase+stripped and no two differ only by case. Guarded here so a
        # future mixed-case persona / instance.yaml account fails loudly instead
        # of becoming silently unreachable (P1).
        from tools.agent_role_map import valid_target_values

        vocab = valid_target_values()
        accounts = set(ticket._OS_USER_PERSONA)
        for name in vocab | accounts | set(ticket._OS_USER_PERSONA.values()):
            self.assertEqual(name, name.strip().lower(), name)
        self.assertEqual(len({v.lower() for v in vocab}), len(vocab))

    # -- T-4499 follow-up: --target is folded the same way (kino's File form and
    #    its retarget box are free text on the same auto-capitalizing keyboard).
    #    ONLY case + surrounding whitespace are forgiven: a target must resolve
    #    to exactly the seat/pool its lowercase form names; an OS account is
    #    still not a delivery vector; junk and unknown roles are still refused.

    def test_target_case_and_whitespace_variants_are_stored_as_the_role_pool(self) -> None:
        cases = {
            "Role:Head_Auditor": "role:head_auditor",  # pool target, folded + stored lowercase
            " ROLE:System_Orchestrator ": "role:system_orchestrator",
            "role:engineering_orchestrator": "role:engineering_orchestrator",
        }
        for given, stored in cases.items():
            with self.subTest(target=given):
                for old in (self.tickets / "inbox").glob("T-*.md"):
                    old.unlink()
                rc, _out, err = self._file("--requester=orchestrator", f"--target={given}")
                self.assertEqual(rc, 0, err)
                self.assertEqual(self._filed_fields()["target"], stored)

    def test_persona_target_variants_are_refused_in_any_case(self) -> None:
        # T-5239 / ORG-088: a seat is not a target in ANY spelling; the refusal names the role.
        for bad in ("Recovery", "RECOVERY", " orchestrator ", "Head_Orchestrator", "Consultant", "\tORCHESTRATOR\n"):
            with self.subTest(target=bad):
                self._assert_refused_unwritten(
                    ("--requester=orchestrator", f"--target={bad}"), "persona targeting is removed"
                )

    def test_resolve_or_reject_target_normalizes_before_lookup(self) -> None:
        for given, stored in (
            ("Role:Head_Auditor", "role:head_auditor"),
            (" ROLE:System_Orchestrator\n", "role:system_orchestrator"),
        ):
            with self.subTest(target=given):
                self.assertEqual(ticket._resolve_or_reject_target(given), stored)

    def test_target_accounts_stay_refused_in_any_case(self) -> None:
        for bad in ("claude-research", "Claude-Research", "  CLAUDE  ", "Suborch1"):
            with self.subTest(target=bad):
                self._assert_refused_unwritten(
                    ("--requester=orchestrator", f"--target={bad}"), "unknown target"
                )

    def test_target_role_pool_junk_stays_refused_in_any_case(self) -> None:
        for bad in ("role:x", "Role:X", "ROLE:", "role:Head Auditor"):
            with self.subTest(target=bad):
                self._assert_refused_unwritten(
                    ("--requester=orchestrator", f"--target={bad}"), "unknown"
                )

    def test_target_junk_stays_refused_after_normalization(self) -> None:
        for bad in ("-x", "test", "   ", "NoBody"):
            with self.subTest(target=bad):
                self._assert_refused_unwritten(
                    ("--requester=orchestrator", f"--target={bad}"), "unknown target"
                )

    def test_target_non_injective_role_class_is_still_refused_in_any_case(self) -> None:
        # Folding must not let a role-class pick a seat: it names 2 seats, so a
        # TARGET must still refuse it (T-4044), however it was capitalized.
        for bad in ("engineering_orchestrator", "Engineering_Orchestrator"):
            with self.subTest(target=bad):
                self._assert_refused_unwritten(
                    ("--requester=orchestrator", f"--target={bad}"), "role:engineering_orchestrator"
                )

    def test_target_refusal_shows_the_callers_original_value(self) -> None:
        with self.assertRaises(ticket.TicketRefusal) as cm:
            ticket._resolve_or_reject_target("  NoBody ")
        self.assertIn(repr("  NoBody "), str(cm.exception))
        self.assertNotIn("'nobody'", str(cm.exception))
        with self.assertRaises(ticket.TicketRefusal) as cm:
            ticket._resolve_or_reject_target("Role:X")
        self.assertIn(repr("Role:X"), str(cm.exception))
        self.assertIn(repr("X"), str(cm.exception))

    def test_role_vocabulary_is_lowercase_so_the_target_fold_is_sound(self) -> None:
        from tools.agent_role_map import known_role_ids

        for role in known_role_ids():
            self.assertEqual(role, role.strip().lower(), role)


class TestRetargetCaseFold(_FileFlow):
    """`retarget --to` goes through the same fold as `file --target`."""

    def _seed(self, target: str = "role:system_orchestrator") -> Path:
        path = self.tickets / "inbox" / "T-0001.md"
        path.write_text(
            "---\nid: T-0001\ncreated: 2026-05-04T12:00:00Z\nrequester: operator\n"
            f"target: {target}\nstate: inbox\nparent_ticket: null\ndepends_on: []\n---\n\n"
            "# Test\n\n## Transition log\n\n- 2026-05-04T12:00:00Z [operator] filed in inbox\n",
            encoding="utf-8",
        )
        return path

    def _target(self, path: Path) -> str:
        fields, _body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        return fields["target"]

    def test_retarget_to_a_capitalized_role_is_accepted(self) -> None:
        for given, stored in (
            ("Role:Engineering_Orchestrator", "role:engineering_orchestrator"),
            (" ROLE:ENGINEERING_ORCHESTRATOR ", "role:engineering_orchestrator"),
            ("Role:System_Orchestrator", "role:system_orchestrator"),
        ):
            with self.subTest(to=given):
                path = self._seed(target="role:research_orchestrator")
                rc, _out, err = _run_main("retarget", "T-0001", f"--to={given}")
                self.assertEqual(rc, 0, err)
                self.assertEqual(self._target(path), stored)

    def test_retarget_fold_happens_before_the_noop_check(self) -> None:
        path = self._seed(target="role:engineering_orchestrator")
        rc, _out, err = _run_main("retarget", "T-0001", "--to=Role:Engineering_Orchestrator")
        self.assertEqual(rc, 0, err)
        self.assertIn("no-op", err)
        self.assertEqual(self._target(path), "role:engineering_orchestrator")

    def test_retarget_accounts_and_junk_stay_refused_and_write_nothing(self) -> None:
        for bad in ("claude-research", "Claude-Research", "role:x", "-x", "NoBody", "Orchestrator", "Recovery"):
            with self.subTest(to=bad):
                path = self._seed()
                before = path.read_text(encoding="utf-8")
                rc, out, err = _run_main("retarget", "T-0001", f"--to={bad}")
                self.assertEqual(rc, ticket.EXIT_REFUSAL, f"stdout={out!r} stderr={err!r}")
                self.assertEqual(path.read_text(encoding="utf-8"), before)


# ---------------------------------------------------------------------------
# T-4494 — --help and the refusal messages say what the appetite gate enforces,
# so a caller who follows the help succeeds. (Documented, not exempted: the
# ORG-089 gate itself is unchanged.)
# ---------------------------------------------------------------------------


def _file_option_help(dest: str) -> str:
    parser = ticket._build_parser()
    (subs,) = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    (action,) = [a for a in subs.choices["file"]._actions if a.dest == dest]
    return " ".join(action.help.split())


class TestAppetiteHelpMatchesTheGate(_FileFlow):
    BASE = ("file", "--requester=orchestrator", "--target=role:system_orchestrator", "--title=t",
            "--project-id=null")

    def test_serves_help_says_any_other_serves_needs_appetite(self) -> None:
        text = _file_option_help("serves")
        self.assertIn("operator-ad-hoc", text)
        self.assertIn("goal-neutral-maintenance", text)
        self.assertIn("other than 'goal-neutral-maintenance'", text)
        self.assertIn("REQUIRES --appetite", text)
        # ...and does not overclaim: the hygiene exemptions are named.
        self.assertIn("maintenance:<asset>", text)

    def test_appetite_help_names_the_trigger_and_every_exemption(self) -> None:
        text = _file_option_help("appetite")
        self.assertIn("other than goal-neutral-maintenance", text)
        self.assertIn("operator-ad-hoc", text)
        self.assertIn("refused", text)
        self.assertIn("maintenance:<asset>", text)
        # Drift guard: every label the gate treats as hygiene is in the help.
        for label in sorted(tp.HYGIENE_LABELS):
            self.assertIn(label, text)

    def test_appetite_required_message_names_the_fix(self) -> None:
        rc, _out, err = _run_main(*self.BASE, "--serves=operator-ad-hoc")
        self.assertEqual(rc, ticket.EXIT_REFUSAL, err)
        msg = _error_line(err)
        self.assertIn("appetite required", msg)
        self.assertIn("--appetite <1-10>", msg)
        self.assertIn("operator-ad-hoc", msg)  # the case the help used to omit
        self.assertIn("goal-neutral-maintenance", msg)

    def test_orphan_refusal_does_not_steer_into_the_appetite_trap(self) -> None:
        rc, _out, err = _run_main(*self.BASE[:-1])
        self.assertEqual(rc, ticket.EXIT_REFUSAL, err)
        msg = " ".join(err.split())  # the orphan refusal spans several lines
        # It suggests operator-ad-hoc, so it must also say that needs --appetite.
        self.assertIn("'operator-ad-hoc'", msg)
        self.assertIn("needs --appetite", msg)

    def test_ad_hoc_via_operational_root_is_still_a_bet(self) -> None:
        """T-3332 r1 MED-1: the help says file ad-hoc the operator asks with
        --nearest-intent-ref ticket:T-5677 PLUS a descriptive --serves, which
        the ORG-089 appetite gate refuses without --appetite and accepts with
        it, with no ORG-054 purpose warning (T-5677 is an operational root).
        T-5677 is the captured real root from tools/fixtures/ticket_lineage."""
        shutil.copy(
            Path(__file__).resolve().parent / "fixtures" / "ticket_lineage"
            / "tickets" / "inbox" / "T-5677.md",
            self.tickets / "inbox" / "T-5677.md",
        )
        for dest in ("serves", "nearest_intent_ref"):
            with self.subTest(help=dest):
                text = _file_option_help(dest)
                self.assertIn("--nearest-intent-ref ticket:T-5677", text)
                self.assertIn("descriptive --serves (which requires --appetite", text)
        ad_hoc = (*self.BASE, "--nearest-intent-ref=ticket:T-5677",
                  "--serves=the operator ad-hoc ask: compare two model backends")
        rc, _out, err = _run_main(*ad_hoc)
        self.assertEqual(rc, ticket.EXIT_REFUSAL, err)
        self.assertIn("appetite required", _error_line(err))
        self.assertEqual(self._inbox_files(), ["T-5677.md"])
        rc, _out, err = _run_main(*ad_hoc, "--appetite=4")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("[ticket-purpose] WARN", err)
        self.assertEqual(self._inbox_files(), ["T-0099.md", "T-5677.md"])

    def test_following_the_help_succeeds(self) -> None:
        # operator-ad-hoc + --appetite files...
        rc, _out, err = _run_main(*self.BASE, "--serves=operator-ad-hoc", "--appetite=5")
        self.assertEqual(rc, 0, err)
        # ...goal-neutral-maintenance alone files (exempt, medium default stamped)...
        for old in (self.tickets / "inbox").glob("T-*.md"):
            old.unlink()
        rc, _out, err = _run_main(*self.BASE, "--serves=goal-neutral-maintenance")
        self.assertEqual(rc, 0, err)
        # ...and so do the two other exemptions the help names.
        for flag in ("--labels=hygiene", "--nearest-intent-ref=maintenance:ticket-board"):
            for old in (self.tickets / "inbox").glob("T-*.md"):
                old.unlink()
            with self.subTest(exemption=flag):
                rc, _out, err = _run_main(*self.BASE, "--serves=operator-ad-hoc", flag)
                self.assertEqual(rc, 0, err)


# ---------------------------------------------------------------------------
# The classification itself is pinned structurally (P1): a NEW bare
# `raise TicketError(` must be a deliberate fault-class decision, not a default.
# ---------------------------------------------------------------------------

# Function -> number of bare `raise TicketError(` sites that are FAULTS (exit 1).
# Every other TicketError-family raise in tools/ticket.py is a TicketRefusal.
_EXPECTED_FAULT_SITES = {
    "parse_front_matter": 4,            # a ticket file's own front matter is malformed
    "_parse_value": 1,                  # ditto (empty list element)
    "serialize_front_matter": 1,        # internal: required field missing at write
    "_next_ticket_id": 1,               # git fetch failed
    "append_transition": 1,             # ticket body lacks the transition-log header
    "_build_commit_message": 1,         # internal: unknown verb
    "_rebuild_readme_table": 1,         # README lacks its table marker
    "_pull_if_needed": 1,               # pre-write pull failed (git ff / rebase / fetch)
    "_stage_and_commit_scoped": 6,      # path escape, add/diff/commit failed, scope guard, --name-status diff failed (T-4885)
    "_commit_and_push": 2,              # README HERMES guard; git push failed
    "cmd_file": 1,                      # git push failed after N attempts
    "_do_transition": 1,                # git mv failed
    "cmd_retarget": 1,                  # git mv failed
    "cmd_close_action": 1,              # ticket on disk has no requester field
    "_resolve_or_reject_target": 2,     # role vocabulary file unverifiable (role: validation; persona->role hint)
    "_derive_pool_target": 1,           # role vocabulary file unverifiable (T-5239 derived targets)
    "_current_branch": 1,               # cannot read the git branch
    "_assert_cli_checkout_allowed": 1,  # checkout is not on main
    "_refuse_if_mid_operation": 1,      # checkout is mid-rebase/merge/cherry-pick/revert (T-4422)
    "_register_insert_index": 1,        # decision register has no table (T-5015)
    "_validate_register": 1,            # decision register file missing (T-5015)
    "_assert_cascade_write_shape": 1,   # internal: a cascade write is not remove-only (T-4760)
}
_FAMILY = {"TicketError", "TicketRefusal", "AppetiteRequired", "AppetiteOverrunNotResolvable"}


def _raise_sites() -> list[tuple[str, str, int]]:
    """(enclosing function, raised class name, line) for every TicketError-family raise."""
    tree = ast.parse(Path(ticket.__file__).read_text(encoding="utf-8"))
    parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        exc = node.exc
        func = exc.func if isinstance(exc, ast.Call) else exc
        name = func.id if isinstance(func, ast.Name) else None
        if name not in _FAMILY:
            continue
        cur, owner = node, "<module>"
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = cur.name
                break
        sites.append((owner, name, node.lineno))
    return sites


class TestClassificationIsPinned(unittest.TestCase):
    def test_bare_ticket_error_sites_are_exactly_the_audited_faults(self) -> None:
        actual: dict[str, int] = {}
        for owner, name, _line in _raise_sites():
            if name == "TicketError":
                actual[owner] = actual.get(owner, 0) + 1
        self.assertEqual(
            actual,
            _EXPECTED_FAULT_SITES,
            "A bare `raise TicketError(` was added/removed. Classify it: if the CALLER's "
            "request is invalid (re-issued against a healthy tool and the current ticket "
            "state it would refuse again), raise TicketRefusal; only a tool/environment "
            "fault stays TicketError — then update _EXPECTED_FAULT_SITES.",
        )

    def test_refusal_family_is_the_bulk_of_the_sites(self) -> None:
        refusals = [s for s in _raise_sites() if s[1] != "TicketError"]
        self.assertGreater(len(refusals), 90, "reclassification did not land")


if __name__ == "__main__":
    unittest.main()
