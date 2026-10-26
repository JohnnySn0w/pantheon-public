#!/usr/bin/env python3
"""Tests for tools/soak_filer.py — the standing per-account suite-health soak
filer (T-0992). Tests-first per the T-0992 spec.

The filer is mocked at the `_invoke_ticket_file` boundary (no real git / ticket
allocation): we assert the per-agent file/skip DECISIONS and the exact argv it
would pass to `ticket.py file`. Idempotency is exercised against real on-disk
ticket fixtures in a tmp tickets/ tree.
"""

from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tools import soak_filer

TODAY = date(2026, 6, 9)


def _write_ticket(tickets_root: Path, state: str, tid: str, target: str, labels: list[str]) -> None:
    """Write a minimal valid ticket fixture into <repo_root>/tickets/<state>/<tid>.md."""
    d = tickets_root / "tickets" / state
    d.mkdir(parents=True, exist_ok=True)
    labels_str = "[" + ", ".join(labels) + "]"
    content = (
        "---\n"
        f"id: {tid}\n"
        "created: 2026-06-09T00:00:00Z\n"
        "requester: recovery\n"
        f"target: {target}\n"
        f"state: {state}\n"
        f"labels: {labels_str}\n"
        "---\n\n"
        f"# {tid} soak leg\n\nbody\n"
    )
    (d / f"{tid}.md").write_text(content, encoding="utf-8")


def _targets(mock_invoke: mock.Mock) -> list[str]:
    """The --target value from each _invoke_ticket_file call, in call order."""
    out = []
    for call in mock_invoke.call_args_list:
        argv = call.args[0]
        out.append(argv[argv.index("--target") + 1])
    return out


def _subjects(mock_invoke: mock.Mock) -> list[str]:
    """The `subject:<agent>` label from each call (T-5239: the seat a leg MEASURES)."""
    out = []
    for call in mock_invoke.call_args_list:
        argv = call.args[0]
        labels = argv[argv.index("--labels") + 1].split(",")
        out.extend(l.split(":", 1)[1] for l in labels if l.startswith("subject:"))
    return out


class SoakFilerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # Pre-create the three live states so scans see real dirs.
        for state in ("inbox", "active", "done"):
            (self.root / "tickets" / state).mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._tmp.cleanup)

    def _file(self, *, force=False):
        """Call file_soak_tickets against the tmp root with _invoke_ticket_file mocked."""
        with mock.patch.object(soak_filer, "_invoke_ticket_file") as m:
            m.return_value = ("filed", "T-9999", "")
            rc = soak_filer.file_soak_tickets(
                TODAY, force=force, repo_root=self.root,
                ticket_bin=self.root / "tools" / "ticket.py", env={},
            )
        return rc, m

    # 1 -----------------------------------------------------------------
    def test_files_all_three_when_none_exist(self):
        rc, m = self._file()
        self.assertEqual(rc, 0)
        # T-5239 / ORG-088: routed to the seat's POOL ROLE; the measured seat is a label.
        self.assertEqual(_targets(m), [
            "role:research_orchestrator", "role:engineering_orchestrator", "role:system_orchestrator",
        ])
        self.assertEqual(_subjects(m), ["specialist", "orchestrator", "recovery"])
        for call in m.call_args_list:
            argv = call.args[0]
            self.assertTrue(argv[argv.index("--target") + 1].startswith("role:"))
            self.assertIn("SUBJECT SEAT", argv[argv.index("--ask") + 1])
            # carries the idempotency label
            labels = argv[argv.index("--labels") + 1]
            self.assertIn(soak_filer.SOAK_LABEL, labels.split(","))
            # requester is recovery
            self.assertEqual(argv[argv.index("--requester") + 1], "recovery")
            # regression guard (pitfall e): --intent is operator-only, never passed
            self.assertNotIn("--intent", argv)
            # the guarded invocation is embedded in the ask
            ask = argv[argv.index("--ask") + 1]
            self.assertIn("PANTHEON_DISABLE_LIVE_TELEGRAM=1", ask)

    # 2 -----------------------------------------------------------------
    def test_skip_when_unresolved_in_inbox(self):
        _write_ticket(self.root, "inbox", "T-1000", "specialist", ["suite-health", soak_filer.SOAK_LABEL])
        rc, m = self._file()
        self.assertEqual(rc, 0)
        self.assertEqual(_subjects(m), ["orchestrator", "recovery"])  # specialist skipped

    # 3 -----------------------------------------------------------------  (critical)
    def test_skip_when_unresolved_in_active(self):
        # A claimed soak leg lives in active/ — an inbox-only scan would miss it
        # and refile a duplicate. This is the core bug the review flagged.
        _write_ticket(self.root, "active", "T-1001", "specialist", [soak_filer.SOAK_LABEL])
        rc, m = self._file()
        self.assertEqual(rc, 0)
        self.assertEqual(_subjects(m), ["orchestrator", "recovery"])  # specialist still skipped

    # 4 -----------------------------------------------------------------
    def test_per_account_isolation(self):
        # recovery's unresolved leg must NOT suppress specialist/orchestrator.
        _write_ticket(self.root, "active", "T-1002", "recovery", [soak_filer.SOAK_LABEL])
        rc, m = self._file()
        self.assertEqual(rc, 0)
        self.assertEqual(_subjects(m), ["specialist", "orchestrator"])  # recovery skipped only

    # 5 -----------------------------------------------------------------
    def test_refile_after_resolved(self):
        # A prior soak ticket only in done/ is resolved → re-file the agent.
        _write_ticket(self.root, "done", "T-1003", "specialist", [soak_filer.SOAK_LABEL])
        rc, m = self._file()
        self.assertEqual(rc, 0)
        self.assertEqual(_subjects(m), ["specialist", "orchestrator", "recovery"])

    # 6 -----------------------------------------------------------------
    def test_label_match_not_substring(self):
        # A non-soak ticket whose BODY merely mentions the marker token must NOT
        # suppress filing — we match the frontmatter label, not free text.
        d = self.root / "tickets" / "inbox"
        (d / "T-1004.md").write_text(
            "---\n"
            "id: T-1004\ncreated: 2026-06-09T00:00:00Z\nrequester: recovery\n"
            "target: specialist\nstate: inbox\nlabels: [unrelated]\n---\n\n"
            "# unrelated\n\nthis mentions suite-health-soak-auto in prose only\n",
            encoding="utf-8",
        )
        rc, m = self._file()
        self.assertEqual(rc, 0)
        self.assertEqual(_subjects(m), ["specialist", "orchestrator", "recovery"])  # specialist NOT suppressed

    # 7 -----------------------------------------------------------------
    def test_produced_text_is_hermes_clean(self):
        # The filer's authored strings must not contain the unescaped HERMES
        # byte-sequence trigger (would trip the commit guard inside ticket.py).
        forbidden = "HERMES" + ".md"
        for agent in soak_filer.SOAK_AGENTS:
            blob = (
                soak_filer._build_ask(agent)
                + soak_filer._build_context(agent, TODAY)
                + soak_filer.SOAK_LABELS_CSV
                + f"Suite-health soak leg — {agent}"
            )
            self.assertNotIn(forbidden, blob)

    # 8 -----------------------------------------------------------------
    def test_force_bypasses_idempotency(self):
        for agent in soak_filer.SOAK_AGENTS:
            _write_ticket(self.root, "active", f"T-200-{agent}", agent, [soak_filer.SOAK_LABEL])
        rc, m = self._file(force=True)
        self.assertEqual(rc, 0)
        self.assertEqual(_subjects(m), ["specialist", "orchestrator", "recovery"])  # all re-filed

    # 9 -----------------------------------------------------------------
    def test_partial_failure_is_resilient(self):
        # orchestrator (2nd) errors; specialist (1st) and recovery (3rd) are still attempted; rc=1.
        with mock.patch.object(soak_filer, "_invoke_ticket_file") as m:
            m.side_effect = [
                ("filed", "T-1", ""),
                ("errored", None, "boom"),
                ("filed", "T-3", ""),
            ]
            rc = soak_filer.file_soak_tickets(
                TODAY, repo_root=self.root,
                ticket_bin=self.root / "tools" / "ticket.py", env={},
            )
        self.assertEqual(rc, 1)
        self.assertEqual(_subjects(m), ["specialist", "orchestrator", "recovery"])

    # 10 ----------------------------------------------------------------
    def test_dry_run_makes_no_subprocess_calls(self):
        with mock.patch.object(soak_filer, "_invoke_ticket_file") as m, \
             mock.patch.object(soak_filer, "REPO_ROOT", self.root):
            rc = soak_filer.main(["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(m.call_count, 0)

    # 11 ----------------------------------------------------------------
    def test_recovery_leg_targets_its_pool_role_and_labels_its_subject(self):
        rc, m = self._file()
        self.assertEqual(rc, 0)
        recovery_call = next(
            c for c in m.call_args_list
            if "subject:recovery" in c.args[0][c.args[0].index("--labels") + 1].split(",")
        )
        argv = recovery_call.args[0]
        self.assertEqual(argv[argv.index("--requester") + 1], "recovery")
        self.assertEqual(argv[argv.index("--target") + 1], "role:system_orchestrator")

    # 11b ---------------------------------------------------------------
    def test_idempotency_matches_the_subject_label_on_a_role_targeted_leg(self):
        # New-shape leg: target is a shared role pool, the measured seat is the label.
        _write_ticket(self.root, "active", "T-1005", "role:engineering_orchestrator",
                      [soak_filer.SOAK_LABEL, "subject:orchestrator"])
        rc, m = self._file()
        self.assertEqual(rc, 0)
        self.assertEqual(_subjects(m), ["specialist", "recovery"])  # orchestrator skipped, others not

    def test_a_role_pool_alone_does_not_suppress_other_subjects(self):
        # Same pool, different subject: must NOT be read as orchestrator's leg.
        _write_ticket(self.root, "active", "T-1006", "role:system_orchestrator",
                      [soak_filer.SOAK_LABEL, "subject:recovery"])
        rc, m = self._file()
        self.assertEqual(_subjects(m), ["specialist", "orchestrator"])

    # 12 ----------------------------------------------------------------
    def test_ask_hardens_against_flake_on_red(self):
        # The soak leg must instruct re-run-on-RED before declaring failure, so a
        # rare suite flake (T-0993) cannot make the monthly soak cry-wolf.
        for agent in soak_filer.SOAK_AGENTS:
            ask = soak_filer._build_ask(agent)
            self.assertIn("Re-run", ask)
            self.assertIn("T-0993", ask)

    # default-safe -------------------------------------------------------
    def test_refuses_without_explicit_mode(self):
        self.assertEqual(soak_filer.main([]), 64)

    def test_prod_and_dry_run_mutually_exclusive(self):
        self.assertEqual(soak_filer.main(["--file-soak-tickets", "--dry-run"]), 64)


if __name__ == "__main__":
    unittest.main()
