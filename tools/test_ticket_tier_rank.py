"""Tests for ``ticket.py tier-rank`` (T-5715)."""

from __future__ import annotations

import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import ticket


def _write_ticket(path: Path, *, state: str = "inbox", tier_rank: int | None = None) -> None:
    rank_line = f"tier_rank: {tier_rank}\n" if tier_rank is not None else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "id: T-0001\n"
        "created: 2026-11-07T00:00:00Z\n"
        "requester: operator\n"
        "target: role:engineering_orchestrator\n"
        f"state: {state}\n"
        "parent_ticket: null\n"
        "depends_on: []\n"
        "labels: [echo]\n"
        f"{rank_line}"
        "---\n\n"
        "# Ordered work\n\n"
        "## Ask\n\n"
        "Do it.\n\n"
        "## Context\n\n"
        "## Transition log\n\n"
        "- 2026-11-07T00:00:00Z [operator] filed in inbox\n",
        encoding="utf-8",
    )


class TestTierRankCommand(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tickets_root = self.root / "tickets"
        for state in ticket.STATES:
            (self.tickets_root / state).mkdir(parents=True)
        self.path = self.tickets_root / "inbox" / "T-0001.md"
        _write_ticket(self.path)

        self._patches = [
            patch.object(ticket, "TICKETS_ROOT", self.tickets_root),
            patch.object(ticket, "REPO_ROOT", self.root),
            patch.object(ticket, "_pull_if_needed"),
            patch.object(ticket, "_commit_and_push"),
            patch.object(ticket, "_actor", return_value="artisan"),
            patch.object(
                ticket, "git_mutation_lock",
                side_effect=lambda **_kwargs: contextlib.nullcontext(),
            ),
        ]
        self.mocks = [item.start() for item in self._patches]
        self.commit = self.mocks[3]

    def tearDown(self) -> None:
        for item in reversed(self._patches):
            item.stop()
        self._tmp.cleanup()

    @staticmethod
    def _args(**overrides) -> argparse.Namespace:
        values = {
            "ticket": "T-0001",
            "rank": None,
            "clear": False,
            "reason": "Echo P2 delivery sequence",
            "no_push": True,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def _read(self) -> tuple[dict, str]:
        return ticket.parse_front_matter(self.path.read_text(encoding="utf-8"))

    def test_set_writes_registered_int_and_audited_reason(self) -> None:
        rc = ticket.cmd_tier_rank(self._args(rank=10))

        self.assertEqual(rc, 0)
        fields, body = self._read()
        self.assertEqual(fields["tier_rank"], "10")
        self.assertIn("tier-ranked", body)
        self.assertIn("tier_rank -> 10; reason: Echo P2 delivery sequence", body)
        self.commit.assert_called_once()

    def test_clear_removes_rank_and_records_reason(self) -> None:
        _write_ticket(self.path, tier_rank=10)

        rc = ticket.cmd_tier_rank(
            self._args(clear=True, reason="Return peers to FIFO")
        )

        self.assertEqual(rc, 0)
        fields, body = self._read()
        self.assertNotIn("tier_rank", fields)
        self.assertIn("tier-rank-cleared", body)
        self.assertIn("tier_rank 10 removed; reason: Return peers to FIFO", body)
        self.commit.assert_called_once()

    def test_blank_reason_refuses_without_write(self) -> None:
        before = self.path.read_bytes()

        with self.assertRaisesRegex(ticket.TicketRefusal, "non-empty --reason"):
            ticket.cmd_tier_rank(self._args(rank=10, reason="  "))

        self.assertEqual(self.path.read_bytes(), before)
        self.commit.assert_not_called()

    def test_done_ticket_refused(self) -> None:
        self.path.unlink()
        self.path = self.tickets_root / "done" / "T-0001.md"
        _write_ticket(self.path, state="done")

        with self.assertRaisesRegex(ticket.TicketRefusal, "done/.*terminal"):
            ticket.cmd_tier_rank(self._args(rank=10))

        self.commit.assert_not_called()

    def test_cli_surface_requires_one_mode_and_reason(self) -> None:
        parser = ticket._build_parser()
        args = parser.parse_args(
            ["tier-rank", "T-0001", "--rank", "10", "--reason", "sequence"]
        )
        self.assertIs(args.func, ticket.cmd_tier_rank)
        self.assertEqual(args.rank, 10)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["tier-rank", "T-0001", "--reason", "missing mode"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["tier-rank", "T-0001", "--rank", "10"])


if __name__ == "__main__":
    unittest.main()
