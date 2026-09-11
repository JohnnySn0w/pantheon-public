"""Tests for T-0492: persona-canonical target/requester normalization.

Covers:
  - Unit: resolve_persona() helper in agent_role_map
  - Unit: cmd_file normalizes target + requester (role-ID → persona)
  - Unit: cmd_retarget normalizes target (role-ID → persona)
  - Unit: legacy role-ID tickets parse correctly (no migration forced at read time)
  - Integ: round-trip file → parse → target/requester is persona name
  - Integ: inbox_consumer dual-match for orchestrator (target=orchestrator OR orchestrator)
  - Integ: inbox_consumer dual-match for specialist (target=specialist OR specialist) [Amendment 2]
  - ticket_lint AUTO-FIX class 3: role-ID target detected + normalized

Run from repo root:
    python3 -m unittest tools.test_ticket_normalization -v
"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import _git_utils, ticket
from tools.agent_role_map import resolve_instance_role, resolve_persona
from tools.inbox_consumer import SkipCycle, list_eligible_tickets
from tools import ticket_lint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_file_args(
    *,
    target: str,
    requester: str = "operator",
    title: str = "Test ticket",
    no_push: bool = True,
) -> MagicMock:
    args = MagicMock()
    args.target = target
    args.requester = requester
    args.title = title
    args.labels = None
    args.depends_on = None
    args.stale_after = None
    args.not_before = None
    args.ratifiable_file = None
    args.ask = ""
    args.context = ""
    args.parent_ticket = None
    args.intent = None
    args.no_push = no_push
    # T-0852 ratify-spawn fields: cmd_file reads these via getattr(args, ..., default).
    # A bare MagicMock auto-creates them as truthy mocks, defeating the getattr default
    # and tripping the "--spawn-impl/--terminal are ratify-only" guard. Set the real
    # defaults explicitly so non-ratify file paths behave.
    args.spawn_impl = False
    args.terminal = False
    args.impl_title = None
    args.impl_ask = None
    args.appetite = None  # T-3440/ORG-089: getattr-read default
    args.bet_ref = None
    args.impl_target = None
    # T-3328: this module tests persona-normalization mechanics, orthogonal
    # to DAG connectivity — orphan escape hatch, not a real lineage edge.
    args.orphan = "test fixture: T-0492 persona-canonical target/requester normalization"
    return args


def _make_retarget_args(tid: str, to: str, *, no_push: bool = True, keep_state: bool = False) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.ticket = tid
    ns.to = to
    ns.note = None
    ns.no_push = no_push
    ns.keep_state = keep_state
    ns.intent = None
    return ns


def _write_inbox_ticket(tickets_root: Path, ticket_id: str, target: str,
                        created: str = "2026-05-04T12:00:00Z") -> Path:
    """Write a minimal well-formed inbox ticket file."""
    state_dir = tickets_root / "inbox"
    state_dir.mkdir(parents=True, exist_ok=True)
    content = (
        f"---\n"
        f"id: {ticket_id}\n"
        f"created: {created}\n"
        f"requester: operator\n"
        f"target: {target}\n"
        f"state: inbox\n"
        f"depends_on: []\n"
        f"labels: []\n"
        f"---\n"
        f"\n"
        f"# Test ticket\n"
        f"\n"
        f"## Ask\n"
        f"\n"
        f"Do the thing.\n"
        f"\n"
        f"## Transition log\n"
        f"\n"
        f"- {created} [operator] filed in inbox\n"
    )
    path = state_dir / f"{ticket_id}.md"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Unit: resolve_persona()
# ---------------------------------------------------------------------------


class TestResolvePersona(unittest.TestCase):
    """agent_role_map.resolve_persona() — inverse of resolve_role()."""

    def test_orchestrator_resolves_to_orchestrator(self) -> None:
        self.assertEqual(resolve_persona("orchestrator"), "orchestrator")

    def test_specialist_resolves_to_specialist(self) -> None:
        self.assertEqual(resolve_persona("specialist"), "specialist")

    def test_orchestrator_passthrough(self) -> None:
        self.assertEqual(resolve_persona("orchestrator"), "orchestrator")

    def test_specialist_passthrough(self) -> None:
        self.assertEqual(resolve_persona("specialist"), "specialist")

    def test_recovery_identity(self) -> None:
        """recovery is the same in both namespaces — passthrough."""
        self.assertEqual(resolve_persona("recovery"), "recovery")

    def test_operator_passthrough(self) -> None:
        self.assertEqual(resolve_persona("operator"), "operator")

    def test_unknown_passthrough(self) -> None:
        """Unknown strings pass through unchanged (backward compat)."""
        self.assertEqual(resolve_persona("typo_agent"), "typo_agent")
        self.assertEqual(resolve_persona("echo"), "echo")


# ---------------------------------------------------------------------------
# Unit: resolve_instance_role (ORG-030 Phase 1)
# ---------------------------------------------------------------------------


class TestResolveInstanceRole(unittest.TestCase):
    """agent_role_map.resolve_instance_role() — persona/audit-id → instance role-key."""

    def test_orchestrator_to_orchestrator(self) -> None:
        self.assertEqual(resolve_instance_role("orchestrator"), "orchestrator")

    def test_specialist_to_specialist(self) -> None:
        self.assertEqual(resolve_instance_role("specialist"), "specialist")

    def test_recovery_to_recovery(self) -> None:
        self.assertEqual(resolve_instance_role("recovery"), "recovery")

    def test_audit_id_specialist_normalizes_to_specialist(self) -> None:
        """Audit role-id specialist → specialist → specialist (two-step resolution)."""
        self.assertEqual(resolve_instance_role("specialist"), "specialist")

    def test_audit_id_orchestrator_normalizes_to_orchestrator_key(self) -> None:
        """Audit role-id orchestrator → orchestrator → orchestrator (hyphenated)."""
        self.assertEqual(resolve_instance_role("orchestrator"), "orchestrator")

    def test_operator_passthrough(self) -> None:
        """Unregistered names pass through unchanged."""
        self.assertEqual(resolve_instance_role("operator"), "operator")

    def test_unknown_passthrough(self) -> None:
        self.assertEqual(resolve_instance_role("echo"), "echo")


# ---------------------------------------------------------------------------
# Unit: cmd_file target normalization
# ---------------------------------------------------------------------------


class TestCmdFileTargetNormalization(unittest.TestCase):
    """cmd_file normalizes role-ID targets to persona names on write."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._sub_patch = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._sub_patch.start()
        self.mock_run.return_value = MagicMock(
            returncode=0, stdout="T-0001\n", stderr=""
        )
        self._commit_patch = patch.object(ticket, "_commit_and_push")
        self._commit_patch.start()
        # Stub _next_ticket_id for determinism
        self._id_patch = patch.object(ticket, "_next_ticket_id", return_value="T-0001")
        self._id_patch.start()

    def tearDown(self) -> None:
        self._id_patch.stop()
        self._commit_patch.stop()
        self._sub_patch.stop()
        self._gu_repo_patch.stop()
        self._repo_patch.stop()
        self._tickets_patch.stop()
        self._tmp.cleanup()

    def _file_and_read(self, target: str, requester: str = "operator") -> dict:
        args = _make_file_args(target=target, requester=requester)
        rc = ticket.cmd_file(args)
        self.assertEqual(rc, 0)
        filed = list((self.root / "inbox").iterdir())
        self.assertEqual(len(filed), 1)
        fields, _ = ticket.parse_front_matter(filed[0].read_text(encoding="utf-8"))
        return fields

    def test_orchestrator_target_normalized_to_orchestrator(self) -> None:
        fields = self._file_and_read(target="orchestrator")
        self.assertEqual(fields["target"], "orchestrator")

    def test_specialist_target_normalized_to_specialist(self) -> None:
        fields = self._file_and_read(target="specialist")
        self.assertEqual(fields["target"], "specialist")

    def test_orchestrator_target_passthrough(self) -> None:
        fields = self._file_and_read(target="orchestrator")
        self.assertEqual(fields["target"], "orchestrator")

    def test_recovery_target_passthrough(self) -> None:
        fields = self._file_and_read(target="recovery")
        self.assertEqual(fields["target"], "recovery")

    def test_operator_target_passthrough(self) -> None:
        # operator target requires intent; auto-defaults to 'action'
        fields = self._file_and_read(target="operator")
        self.assertEqual(fields["target"], "operator")


# ---------------------------------------------------------------------------
# Unit: cmd_file requester normalization (Amendment 1)
# ---------------------------------------------------------------------------


class TestCmdFileRequesterNormalization(unittest.TestCase):
    """cmd_file normalizes role-ID requesters to persona names on write."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._sub_patch = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._sub_patch.start()
        self.mock_run.return_value = MagicMock(returncode=0, stdout="T-0001\n", stderr="")
        self._commit_patch = patch.object(ticket, "_commit_and_push")
        self._commit_patch.start()
        self._id_patch = patch.object(ticket, "_next_ticket_id", return_value="T-0001")
        self._id_patch.start()

    def tearDown(self) -> None:
        self._id_patch.stop()
        self._commit_patch.stop()
        self._sub_patch.stop()
        self._gu_repo_patch.stop()
        self._repo_patch.stop()
        self._tickets_patch.stop()
        self._tmp.cleanup()

    def _file_and_read_requester(self, requester: str, target: str = "recovery") -> str:
        args = _make_file_args(target=target, requester=requester)
        rc = ticket.cmd_file(args)
        self.assertEqual(rc, 0)
        filed = list((self.root / "inbox").iterdir())
        self.assertEqual(len(filed), 1)
        fields, _ = ticket.parse_front_matter(filed[0].read_text(encoding="utf-8"))
        return fields["requester"]

    def test_orchestrator_requester_normalized_to_orchestrator(self) -> None:
        self.assertEqual(self._file_and_read_requester("orchestrator"), "orchestrator")

    def test_specialist_requester_normalized_to_specialist(self) -> None:
        self.assertEqual(self._file_and_read_requester("specialist"), "specialist")

    def test_operator_requester_passthrough(self) -> None:
        self.assertEqual(self._file_and_read_requester("operator"), "operator")

    def test_orchestrator_requester_passthrough(self) -> None:
        self.assertEqual(self._file_and_read_requester("orchestrator"), "orchestrator")

    def test_recovery_requester_passthrough(self) -> None:
        self.assertEqual(self._file_and_read_requester("recovery"), "recovery")


# ---------------------------------------------------------------------------
# Unit: cmd_retarget normalization (Amendment 3)
# ---------------------------------------------------------------------------


class TestCmdRetargetNormalization(unittest.TestCase):
    """cmd_retarget normalizes role-ID targets to persona names on write."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._sub_patch = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._sub_patch.start()
        self.mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        self._commit_patch = patch.object(ticket, "_commit_and_push")
        self.mock_commit = self._commit_patch.start()

    def tearDown(self) -> None:
        self._commit_patch.stop()
        self._sub_patch.stop()
        self._gu_repo_patch.stop()
        self._repo_patch.stop()
        self._tickets_patch.stop()
        self._tmp.cleanup()

    def _seed(self, tid: str, state: str, target: str = "recovery") -> Path:
        path = self.root / state / f"{tid}.md"
        content = (
            f"---\n"
            f"id: {tid}\n"
            f"created: 2026-05-04T12:00:00Z\n"
            f"requester: operator\n"
            f"target: {target}\n"
            f"state: {state}\n"
            f"parent_ticket: null\n"
            f"depends_on: []\n"
            f"---\n"
            f"\n"
            f"# Test\n"
            f"\n"
            f"## Transition log\n"
            f"\n"
            f"- 2026-05-04T12:00:00Z [operator] filed in inbox\n"
        )
        path.write_text(content, encoding="utf-8")
        return path

    def test_retarget_orchestrator_normalizes_to_orchestrator(self) -> None:
        path = self._seed("T-0001", "inbox", target="recovery")
        rc = ticket.cmd_retarget(_make_retarget_args("T-0001", "orchestrator"))
        self.assertEqual(rc, 0)
        fields, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["target"], "orchestrator")

    def test_retarget_specialist_normalizes_to_specialist(self) -> None:
        path = self._seed("T-0002", "inbox", target="recovery")
        rc = ticket.cmd_retarget(_make_retarget_args("T-0002", "specialist"))
        self.assertEqual(rc, 0)
        fields, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["target"], "specialist")

    def test_retarget_orchestrator_on_orchestrator_ticket_is_noop(self) -> None:
        """retarget --to orchestrator on target=orchestrator ticket must no-op (invariant)."""
        import io, sys
        path = self._seed("T-0003", "inbox", target="orchestrator")
        # Capture stderr for the no-op message
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            rc = ticket.cmd_retarget(_make_retarget_args("T-0003", "orchestrator"))
        self.assertEqual(rc, 0)
        self.mock_commit.assert_not_called()
        # File unchanged
        fields, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["target"], "orchestrator")

    def test_retarget_persona_still_works(self) -> None:
        path = self._seed("T-0004", "inbox", target="recovery")
        rc = ticket.cmd_retarget(_make_retarget_args("T-0004", "orchestrator"))
        self.assertEqual(rc, 0)
        fields, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["target"], "orchestrator")


# ---------------------------------------------------------------------------
# Unit: legacy parse (no migration forced at read time)
# ---------------------------------------------------------------------------


class TestLegacyParse(unittest.TestCase):
    """Existing role-ID targets parse without error; no forced migration."""

    def test_orchestrator_target_parses(self) -> None:
        text = (
            "---\n"
            "id: T-0001\n"
            "created: 2026-05-04T12:00:00Z\n"
            "requester: operator\n"
            "target: orchestrator\n"
            "state: inbox\n"
            "depends_on: []\n"
            "---\n"
            "\n"
            "# Legacy ticket\n"
        )
        fields, _ = ticket.parse_front_matter(text)
        # Value preserved as-is at parse time — no migration
        self.assertEqual(fields["target"], "orchestrator")

    def test_specialist_target_parses(self) -> None:
        text = (
            "---\n"
            "id: T-0002\n"
            "created: 2026-05-04T12:00:00Z\n"
            "requester: operator\n"
            "target: specialist\n"
            "state: inbox\n"
            "depends_on: []\n"
            "---\n"
            "\n"
            "# Legacy ticket\n"
        )
        fields, _ = ticket.parse_front_matter(text)
        self.assertEqual(fields["target"], "specialist")


# ---------------------------------------------------------------------------
# Integ: round-trip — file with role-ID alias → parse → persona name stored
# ---------------------------------------------------------------------------


class TestRoundTripNormalization(unittest.TestCase):
    """Integration: cmd_file with role-ID alias stores persona name on disk."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._sub_patch = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._sub_patch.start()
        self.mock_run.return_value = MagicMock(returncode=0, stdout="T-0001\n", stderr="")
        self._commit_patch = patch.object(ticket, "_commit_and_push")
        self._commit_patch.start()
        self._id_patch = patch.object(ticket, "_next_ticket_id", return_value="T-0001")
        self._id_patch.start()

    def tearDown(self) -> None:
        self._id_patch.stop()
        self._commit_patch.stop()
        self._sub_patch.stop()
        self._gu_repo_patch.stop()
        self._repo_patch.stop()
        self._tickets_patch.stop()
        self._tmp.cleanup()

    def test_file_orchestrator_roundtrip_stores_orchestrator(self) -> None:
        args = _make_file_args(target="orchestrator", requester="operator")
        ticket.cmd_file(args)
        filed = list((self.root / "inbox").iterdir())
        self.assertEqual(len(filed), 1)
        fields, _ = ticket.parse_front_matter(filed[0].read_text(encoding="utf-8"))
        self.assertEqual(fields["target"], "orchestrator")
        # Confirm raw file text also has persona form
        raw = filed[0].read_text(encoding="utf-8")
        self.assertIn("target: orchestrator", raw)
        self.assertNotIn("target: orchestrator", raw)

    def test_file_orchestrator_requester_roundtrip(self) -> None:
        args = _make_file_args(target="recovery", requester="orchestrator")
        ticket.cmd_file(args)
        filed = list((self.root / "inbox").iterdir())
        fields, _ = ticket.parse_front_matter(filed[0].read_text(encoding="utf-8"))
        self.assertEqual(fields["requester"], "orchestrator")
        raw = filed[0].read_text(encoding="utf-8")
        self.assertIn("requester: orchestrator", raw)
        self.assertNotIn("requester: orchestrator", raw)


# ---------------------------------------------------------------------------
# Integ: inbox_consumer dual-match (orchestrator + specialist sides)
# ---------------------------------------------------------------------------


class TestInboxConsumerDualMatch(unittest.TestCase):
    """Inbox consumer picks up legacy role-ID tickets for both orchestrator and specialist."""

    def test_orchestrator_consumer_finds_orchestrator_ticket(self) -> None:
        """Regression guard: orchestrator consumer matches target=orchestrator (role-ID legacy)."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_inbox_ticket(tickets_root, "T-0001", "orchestrator")
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                tickets = list_eligible_tickets("orchestrator")
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].id, "T-0001")

    def test_orchestrator_consumer_finds_orchestrator_ticket(self) -> None:
        """Regression guard: orchestrator consumer still matches target=orchestrator (persona form)."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_inbox_ticket(tickets_root, "T-0001", "orchestrator")
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                tickets = list_eligible_tickets("orchestrator")
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].id, "T-0001")

    def test_specialist_consumer_finds_specialist_ticket(self) -> None:
        """Amendment 2: specialist consumer matches target=specialist (role-ID legacy)."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_inbox_ticket(tickets_root, "T-0001", "specialist")
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                tickets = list_eligible_tickets("specialist")
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].id, "T-0001")

    def test_specialist_consumer_finds_specialist_ticket(self) -> None:
        """Regression guard: specialist consumer still matches target=specialist (persona form)."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_inbox_ticket(tickets_root, "T-0001", "specialist")
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                tickets = list_eligible_tickets("specialist")
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].id, "T-0001")

    def test_specialist_consumer_does_not_find_orchestrator_ticket(self) -> None:
        """Specialist consumer must not pick up orchestrator/orchestrator tickets."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_inbox_ticket(tickets_root, "T-0001", "orchestrator")
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                with self.assertRaises(SkipCycle):
                    list_eligible_tickets("specialist")

    def test_orchestrator_consumer_does_not_find_specialist_ticket(self) -> None:
        """Orchestrator consumer must not pick up specialist/specialist tickets."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_inbox_ticket(tickets_root, "T-0001", "specialist")
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                with self.assertRaises(SkipCycle):
                    list_eligible_tickets("orchestrator")


# ---------------------------------------------------------------------------
# Unit + Integ: ticket_lint AUTO-FIX class 3 (role-ID target → persona)
# ---------------------------------------------------------------------------


class TestTicketLintRoleIdAutoFix(unittest.TestCase):
    """ticket_lint detects role-ID targets and normalizes them to persona names."""

    def _write_ticket(self, tmp_dir: Path, target: str) -> Path:
        path = tmp_dir / "T-0001.md"
        content = (
            f"---\n"
            f"id: T-0001\n"
            f"created: 2026-05-04T12:00:00Z\n"
            f"requester: operator\n"
            f"target: {target}\n"
            f"state: inbox\n"
            f"depends_on: []\n"
            f"---\n"
            f"\n"
            f"# Test\n"
            f"\n"
            f"## Transition log\n"
            f"\n"
            f"- 2026-05-04T12:00:00Z [operator] filed in inbox\n"
        )
        path.write_text(content, encoding="utf-8")
        return path

    def test_detect_orchestrator_target_as_auto_fix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_ticket(Path(tmp), "orchestrator")
            result = ticket_lint.detect_malformations(path, repo_root=Path(tmp))
        self.assertEqual(result.action, ticket_lint.LintAction.AUTO_FIX)
        self.assertIn("orchestrator", result.description)

    def test_detect_specialist_target_as_auto_fix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_ticket(Path(tmp), "specialist")
            result = ticket_lint.detect_malformations(path, repo_root=Path(tmp))
        self.assertEqual(result.action, ticket_lint.LintAction.AUTO_FIX)
        self.assertIn("specialist", result.description)

    def test_detect_orchestrator_target_as_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_ticket(Path(tmp), "orchestrator")
            result = ticket_lint.detect_malformations(path, repo_root=Path(tmp))
        self.assertEqual(result.action, ticket_lint.LintAction.CLEAN)

    def test_detect_recovery_target_as_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_ticket(Path(tmp), "recovery")
            result = ticket_lint.detect_malformations(path, repo_root=Path(tmp))
        self.assertEqual(result.action, ticket_lint.LintAction.CLEAN)

    def test_apply_auto_fix_orchestrator_to_orchestrator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_ticket(Path(tmp), "orchestrator")
            ticket_lint.apply_auto_fix(path, repo_root=Path(tmp))
            fields, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["target"], "orchestrator")

    def test_apply_auto_fix_specialist_to_specialist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_ticket(Path(tmp), "specialist")
            ticket_lint.apply_auto_fix(path, repo_root=Path(tmp))
            fields, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["target"], "specialist")

    def test_apply_auto_fix_idempotent_on_clean(self) -> None:
        """apply_auto_fix on an orchestrator ticket must be a no-op (idempotent)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_ticket(Path(tmp), "orchestrator")
            original_text = path.read_text(encoding="utf-8")
            ticket_lint.apply_auto_fix(path, repo_root=Path(tmp))
            self.assertEqual(path.read_text(encoding="utf-8"), original_text)


if __name__ == "__main__":
    unittest.main()
