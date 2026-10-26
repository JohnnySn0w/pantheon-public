"""T-5239 / ORG-088 amendment: tickets route by ROLE only, never by persona.

The operator TG 12076/12077 (2026-10-19). Covers:
  - agent_role_map.persona_pool_role / pool_roles_for (the one persona->role map)
  - ticket.py file / retarget REFUSE a persona (or alias / bare role-id) target with a
    typed error naming the role(s); role:<R> and operator are accepted
  - ticket.py derived-target paths (close-action review child) emit a role
  - ticket_lint FLAGs a persona target on open tickets; done tickets are exempt
  - the reading side stays tolerant of legacy persona targets
  - a `subject:<persona>` LABEL is descriptive only: no delivery/visibility path reads
    it as a target

Run: python3 -m unittest tools.test_ticket_role_only_targets -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import _git_utils, ticket, ticket_lint
from tools.agent_role_map import persona_pool_role, pool_roles_for
from tools.inbox_consumer import SkipCycle, list_eligible_tickets
from tools.test_ticket_normalization import (
    _make_file_args,
    _make_retarget_args,
    _write_inbox_ticket,
)


class TestPersonaPoolRole(unittest.TestCase):
    def test_re_pool_mapping(self) -> None:
        expected = {
            "orchestrator": "engineering_orchestrator",
            "mason": "engineering_orchestrator",
            "artisan": "engineering_orchestrator",
            "recovery": "system_orchestrator",
            "watchman": "system_orchestrator",
            "archivist": "memory_orchestrator",
            "specialist": "research_orchestrator",
        }
        for persona, role in expected.items():
            with self.subTest(persona=persona):
                self.assertEqual(persona_pool_role(persona), role)

    def test_audit_ids_and_aliases_resolve(self) -> None:
        self.assertEqual(persona_pool_role("orchestrator"), "engineering_orchestrator")
        self.assertEqual(persona_pool_role("specialist"), "research_orchestrator")

    def test_operator_and_unknown_have_no_pool_role(self) -> None:
        self.assertIsNone(persona_pool_role("operator"))
        self.assertIsNone(persona_pool_role("nobody-here"))

    def test_pool_roles_for_lists_recommended_first(self) -> None:
        roles = pool_roles_for("orchestrator")
        self.assertEqual(roles[0], "engineering_orchestrator")
        self.assertIn("head_engineering_orchestrator", roles)


class _RepoFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        patches = [
            patch.object(ticket, "TICKETS_ROOT", self.root),
            patch.object(ticket, "REPO_ROOT", self.root),
            patch.object(_git_utils, "REPO_ROOT", self.root),
            patch("tools._git_utils.subprocess.run",
                  return_value=MagicMock(returncode=0, stdout="T-0001\n", stderr="")),
            patch.object(ticket, "_commit_and_push"),
            patch.object(ticket, "_next_ticket_id", return_value="T-0001"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def _filed(self) -> dict:
        files = list((self.root / "inbox").iterdir())
        self.assertEqual(len(files), 1)
        fields, _ = ticket.parse_front_matter(files[0].read_text(encoding="utf-8"))
        return fields


class TestFileRefusesPersonaTarget(_RepoFixture):
    def test_persona_alias_and_roleid_targets_refused_naming_role(self) -> None:
        cases = {
            "orchestrator": "role:engineering_orchestrator",
            "Recovery": "role:system_orchestrator",
            "specialist": "role:research_orchestrator",
            "archivist": "role:memory_orchestrator",
            "orchestrator": "role:engineering_orchestrator",
            "specialist": "role:research_orchestrator",
            "head_engineering_orchestrator": "role:head_engineering_orchestrator",
            "engineering_orchestrator": "role:engineering_orchestrator",
        }
        for target, hint in cases.items():
            with self.subTest(target=target):
                with self.assertRaises(ticket.TicketRefusal) as cm:
                    ticket.cmd_file(_make_file_args(target=target))
                self.assertIn(hint, str(cm.exception))
                self.assertEqual(list((self.root / "inbox").iterdir()), [])

    def test_role_target_accepted_unchanged(self) -> None:
        self.assertEqual(ticket.cmd_file(_make_file_args(target="role:engineering_orchestrator")), 0)
        self.assertEqual(self._filed()["target"], "role:engineering_orchestrator")

    def test_operator_target_accepted(self) -> None:
        self.assertEqual(ticket.cmd_file(_make_file_args(target="operator")), 0)
        self.assertEqual(self._filed()["target"], "operator")

    def test_requester_stays_persona(self) -> None:
        ticket.cmd_file(_make_file_args(target="role:system_orchestrator", requester="orchestrator"))
        self.assertEqual(self._filed()["requester"], "orchestrator")


class TestRetargetRefusesPersonaTarget(_RepoFixture):
    def _seed(self, target: str = "role:engineering_orchestrator") -> None:
        _write_inbox_ticket(self.root, "T-0001", target)

    def test_retarget_to_persona_refused_naming_role(self) -> None:
        self._seed()
        with self.assertRaises(ticket.TicketRefusal) as cm:
            ticket.cmd_retarget(_make_retarget_args("T-0001", "recovery"))
        self.assertIn("role:system_orchestrator", str(cm.exception))

    def test_retarget_to_role_ok(self) -> None:
        self._seed()
        self.assertEqual(ticket.cmd_retarget(_make_retarget_args("T-0001", "role:system_orchestrator")), 0)
        fields, _ = ticket.parse_front_matter(
            next(self.root.rglob("T-0001.md")).read_text(encoding="utf-8"))
        self.assertEqual(fields["target"], "role:system_orchestrator")


class TestDerivedTargets(unittest.TestCase):
    def test_derive_pool_target(self) -> None:
        self.assertEqual(ticket._derive_pool_target("orchestrator"), "role:engineering_orchestrator")
        self.assertEqual(ticket._derive_pool_target("head_research_orchestrator"),
                         "role:research_orchestrator")
        self.assertEqual(ticket._derive_pool_target("operator"), "operator")
        self.assertEqual(ticket._derive_pool_target("role:system_orchestrator"),
                         "role:system_orchestrator")

    def test_derive_pool_target_unknown_refuses(self) -> None:
        with self.assertRaises(ticket.TicketRefusal):
            ticket._derive_pool_target("nobody-here")


def _lint_ticket(tmp: Path, target: str, state: str = "inbox", labels: str = "[]") -> Path:
    d = tmp / "tickets" / state
    d.mkdir(parents=True, exist_ok=True)
    path = d / "T-0001.md"
    path.write_text(
        "---\nid: T-0001\ncreated: 2026-10-19T12:00:00Z\nrequester: operator\n"
        f"target: {target}\nstate: {state}\ndepends_on: []\nlabels: {labels}\n---\n\n"
        "# Test\n\n## Transition log\n\n- 2026-10-19T12:00:00Z [operator] filed in inbox\n",
        encoding="utf-8",
    )
    return path


class TestLintFlagsPersonaTarget(unittest.TestCase):
    def test_persona_and_roleid_targets_flagged_on_open_tickets(self) -> None:
        for state in ("inbox", "active"):
            for target in ("orchestrator", "recovery", "specialist", "orchestrator", "specialist"):
                with self.subTest(state=state, target=target), tempfile.TemporaryDirectory() as tmp:
                    path = _lint_ticket(Path(tmp), target, state)
                    result = ticket_lint.detect_malformations(path, repo_root=Path(tmp))
                    self.assertEqual(result.action, ticket_lint.LintAction.FLAG)
                    self.assertIn("persona", result.description)
                    self.assertIn("role:", result.description)

    def test_role_and_operator_targets_not_flagged_as_persona(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _lint_ticket(Path(tmp), "role:engineering_orchestrator")
            result = ticket_lint.detect_malformations(path, repo_root=Path(tmp))
            self.assertNotIn("persona", result.description)

    def test_done_tickets_exempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _lint_ticket(Path(tmp), "orchestrator", "done")
            results = ticket_lint.scan_tickets(Path(tmp) / "tickets", repo_root=Path(tmp))
            self.assertEqual(results, [])

    def test_auto_fix_never_rewrites_to_persona(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _lint_ticket(Path(tmp), "orchestrator")
            before = path.read_text(encoding="utf-8")
            with self.assertRaises(ticket_lint.LintError):  # FLAG class: not mechanical
                ticket_lint.apply_auto_fix(path, repo_root=Path(tmp))
            self.assertEqual(path.read_text(encoding="utf-8"), before)


class TestReposGrantCheckForRolePools(unittest.TestCase):
    """T-5239: every open ticket is role-targeted now, so the repos: grant check must
    judge a POOL (satisfiable iff some member holds the grant), not look up the literal
    `role:<R>` string as an instance role-key."""

    REGISTRY = {"repos": [
        {"name": "repo-a", "grants": {"head-system-orchestrator": "rw"}},
        {"name": "repo-b", "grants": {"specialist": "ro"}},
    ]}

    def test_pool_passes_when_one_member_holds_the_grant(self) -> None:
        # role:system_orchestrator = recovery (head-system-orchestrator: rw) + watchman (no grant)
        self.assertIsNone(ticket_lint.validate_repos_field(
            ["repo-a:rw"], "role:system_orchestrator", self.REGISTRY))

    def test_pool_fails_when_no_member_holds_the_grant(self) -> None:
        err = ticket_lint.validate_repos_field(
            ["repo-b:ro"], "role:system_orchestrator", self.REGISTRY)
        self.assertIsNotNone(err)
        self.assertIn("no grant", err)

    def test_pool_rw_needs_an_rw_member(self) -> None:
        self.assertIsNone(ticket_lint.validate_repos_field(
            ["repo-b:ro"], "role:research_orchestrator", self.REGISTRY))
        err = ticket_lint.validate_repos_field(
            ["repo-b:rw"], "role:research_orchestrator", self.REGISTRY)
        self.assertIn("rw", err)


class TestReadSideTolerantAndSubjectLabelInert(unittest.TestCase):
    def test_consumer_still_matches_legacy_persona_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_inbox_ticket(root, "T-0001", "orchestrator")
            with patch("tools.inbox_consumer.TICKETS_ROOT", root):
                self.assertEqual([t.id for t in list_eligible_tickets("orchestrator")], ["T-0001"])

    def test_subject_label_is_not_a_delivery_target(self) -> None:
        """A `subject:<persona>` label describes the seat a ticket measures; it must
        never route. recovery is NOT eligible for a role:engineering_orchestrator ticket
        labelled subject:recovery; an engineering seat (mason) is."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = _write_inbox_ticket(root, "T-0001", "role:engineering_orchestrator")
            path.write_text(
                path.read_text(encoding="utf-8").replace("labels: []", "labels: [subject:recovery]"),
                encoding="utf-8",
            )
            with patch("tools.inbox_consumer.TICKETS_ROOT", root):
                with self.assertRaises(SkipCycle):
                    list_eligible_tickets("recovery")
                self.assertEqual([t.id for t in list_eligible_tickets("mason")], ["T-0001"])

    def test_subject_label_not_a_list_view_target(self) -> None:
        fields = {"target": "role:engineering_orchestrator", "labels": ["subject:recovery"]}
        roles = frozenset({"system_orchestrator", "head_system_orchestrator"})
        self.assertFalse(ticket._visible_to_target(fields["target"], "recovery", roles))


if __name__ == "__main__":
    unittest.main()
