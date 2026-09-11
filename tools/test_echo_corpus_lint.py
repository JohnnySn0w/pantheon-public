"""Tests for tools/echo_corpus_lint.py (T-3495).

Covers: the echo-scoped finding filter (union scan over both ADR dirs,
narrowed to echo-scoped findings only), the signature+cooldown alert-gating
logic, and a mocked-mailbox smoke test of run()'s alert-vs-skip behavior.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import echo_corpus_lint
from tools.test_typed_edges import _write_adr, _write_ticket

# ---------------------------------------------------------------------------
# scan_echo_scoped_findings
# ---------------------------------------------------------------------------


class TestScanEchoScopedFindings(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.tickets = self.repo_root / "tickets"
        self.pantheon_adr_dir = self.repo_root / "architecture" / "adr"
        self.echo_adr_dir = self.repo_root / "echo-adr"
        for st in ("inbox", "active", "done"):
            (self.tickets / st).mkdir(parents=True)
        self.pantheon_adr_dir.mkdir(parents=True)
        self.echo_adr_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _scan(self):
        return echo_corpus_lint.scan_echo_scoped_findings(self.repo_root, self.echo_adr_dir)

    def test_clean_corpus_no_findings(self) -> None:
        _write_ticket(self.tickets / "inbox" / "T-0001.md", tid="T-0001")
        self.assertEqual(self._scan(), [])

    def test_echo_dangling_target_surfaced(self) -> None:
        """A ticket edge targeting a non-existent echo obligation IS
        surfaced — this companion unit reads the echo dir, unlike recovery's own
        scan (which now excludes it)."""
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", implements=["ADR-207#E1"])
        findings = self._scan()
        dangling = [f for f in findings if f.kind == "dangling_target"]
        self.assertEqual(len(dangling), 1)
        self.assertIn("ADR-207#E1", dangling[0].detail)

    def test_echo_resolvable_and_verified_target_not_flagged(self) -> None:
        _write_adr(self.echo_adr_dir / "ADR-207-test.md", adr_id="ADR-207",
                   elements_block="  - id: E1\n    title: t\n")
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", implements=["ADR-207#E1"], verifies=["ADR-207#E1"])
        self.assertEqual(self._scan(), [])

    def test_echo_implemented_only_surfaces_transitional_finding(self) -> None:
        """implements: with no verifies: is the expected-transitional shape
        (ORG-065 Tranche A) — surfaced as implemented_unverified_element,
        not silently dropped, and not a dangling_target either."""
        _write_adr(self.echo_adr_dir / "ADR-207-test.md", adr_id="ADR-207",
                   elements_block="  - id: E1\n    title: t\n")
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", implements=["ADR-207#E1"])
        findings = self._scan()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "implemented_unverified_element")
        self.assertIn("ADR-207#E1", findings[0].detail)

    def test_pantheon_only_dangling_target_filtered_out(self) -> None:
        """A pantheon-only (ORG-NNN) dangling target is a real finding of
        the underlying scan but is NOT echo-scoped — filtered out of this
        companion's output (recovery's own lint already covers it)."""
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", implements=["ORG-999#E1"])
        self.assertEqual(self._scan(), [])

    def test_echo_unverified_element_surfaced(self) -> None:
        _write_adr(self.echo_adr_dir / "ADR-210-test.md", adr_id="ADR-210",
                   elements_block="  - id: E1\n    title: t\n")
        findings = self._scan()
        unverified = [f for f in findings if f.kind == "unverified_element"]
        self.assertEqual(len(unverified), 1)
        self.assertIn("ADR-210#E1", unverified[0].detail)

    def test_pantheon_unverified_element_filtered_out(self) -> None:
        _write_adr(self.pantheon_adr_dir / "ORG-065-test.md", adr_id="ORG-065",
                   elements_block="  - id: E1\n    title: t\n")
        self.assertEqual(self._scan(), [])


# ---------------------------------------------------------------------------
# _finding_signature / _should_alert
# ---------------------------------------------------------------------------


class TestFindingSignatureAndCooldown(unittest.TestCase):
    def setUp(self) -> None:
        self.f1 = echo_corpus_lint.TypedGraphFinding(
            kind="dangling_target", detail="T-0001 implements -> ADR-207#E1, dangling",
        )
        self.f2 = echo_corpus_lint.TypedGraphFinding(
            kind="unverified_element", detail="ADR-210#E1 unverified",
        )

    def test_signature_stable_and_order_independent(self) -> None:
        sig_a = echo_corpus_lint._finding_signature([self.f1, self.f2])
        sig_b = echo_corpus_lint._finding_signature([self.f2, self.f1])
        self.assertEqual(sig_a, sig_b)

    def test_signature_changes_with_content(self) -> None:
        sig_a = echo_corpus_lint._finding_signature([self.f1])
        sig_b = echo_corpus_lint._finding_signature([self.f1, self.f2])
        self.assertNotEqual(sig_a, sig_b)

    def test_new_signature_always_alerts(self) -> None:
        state: dict = {}
        self.assertTrue(echo_corpus_lint._should_alert(state, "sig-a", now=1000.0, cooldown_seconds=3600))

    def test_changed_signature_bypasses_cooldown(self) -> None:
        state = {"last_signature": "sig-a", "last_alert_ts": 1000.0}
        # Only 1 second elapsed, well within cooldown, but signature changed.
        self.assertTrue(
            echo_corpus_lint._should_alert(state, "sig-b", now=1001.0, cooldown_seconds=3600)
        )

    def test_same_signature_within_cooldown_suppressed(self) -> None:
        state = {"last_signature": "sig-a", "last_alert_ts": 1000.0}
        self.assertFalse(
            echo_corpus_lint._should_alert(state, "sig-a", now=1001.0, cooldown_seconds=3600)
        )

    def test_same_signature_past_cooldown_alerts_again(self) -> None:
        state = {"last_signature": "sig-a", "last_alert_ts": 1000.0}
        self.assertTrue(
            echo_corpus_lint._should_alert(state, "sig-a", now=1000.0 + 3600, cooldown_seconds=3600)
        )


# ---------------------------------------------------------------------------
# run() — mocked-mailbox smoke test (findings-present alerts; findings-empty
# and --no-mailbox both skip the send).
# ---------------------------------------------------------------------------


class TestRunMailboxIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.tickets = self.repo_root / "tickets"
        self.pantheon_adr_dir = self.repo_root / "architecture" / "adr"
        self.echo_adr_dir = self.repo_root / "echo-adr"
        for st in ("inbox", "active", "done"):
            (self.tickets / st).mkdir(parents=True)
        self.pantheon_adr_dir.mkdir(parents=True)
        self.echo_adr_dir.mkdir(parents=True)
        self.state_path = self.repo_root / "state.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, **overrides):
        kwargs = dict(
            repo_root=self.repo_root,
            echo_adr_dir=self.echo_adr_dir,
            state_path=self.state_path,
            cooldown_seconds=3600,
            sender="claude",
            recovery_inbox="/fake/recovery/inbox",
            send_mailbox=True,
        )
        kwargs.update(overrides)
        return echo_corpus_lint.run(**kwargs)

    @patch("tools.echo_corpus_lint._alert_recovery")
    def test_alerts_on_findings_present(self, mock_alert) -> None:
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", implements=["ADR-207#E1"])
        rc = self._run()
        self.assertEqual(rc, 0)
        mock_alert.assert_called_once()
        body = mock_alert.call_args[0][0]
        self.assertIn("ADR-207#E1", body)

    @patch("tools.echo_corpus_lint._alert_recovery")
    def test_skips_on_findings_empty(self, mock_alert) -> None:
        _write_ticket(self.tickets / "inbox" / "T-0001.md", tid="T-0001")
        rc = self._run()
        self.assertEqual(rc, 0)
        mock_alert.assert_not_called()

    @patch("tools.echo_corpus_lint._alert_recovery")
    def test_no_mailbox_flag_skips_alert_even_with_findings(self, mock_alert) -> None:
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", implements=["ADR-207#E1"])
        rc = self._run(send_mailbox=False)
        self.assertEqual(rc, 0)
        mock_alert.assert_not_called()

    @patch("tools.echo_corpus_lint._alert_recovery")
    def test_second_run_within_cooldown_throttled(self, mock_alert) -> None:
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", implements=["ADR-207#E1"])
        now = time.time()
        self._run(now=now)
        mock_alert.assert_called_once()
        mock_alert.reset_mock()
        self._run(now=now + 10)  # same finding set, well within cooldown
        mock_alert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
