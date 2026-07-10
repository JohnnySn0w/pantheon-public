#!/usr/bin/env python3
"""Tests for auto_commit_contract_lint — the auto-commit-cron default-safe contract linter (T-1072).

Pure-function tests over synthetic fixture dirs (temp scripts + unit files), so
they stay stable as the REAL scripts get remediated. Run:
    python3 -m unittest tools.test_auto_commit_contract_lint
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.auto_commit_contract_lint import (
    Severity,
    exit_code,
    lint,
)

# Source snippets modelling the structural signals the linter keys on.
_ADD_AND_PUSH = '_git("add", "--", rel, cwd=cwd)\n    push_with_retry(cwd)\n'
_PUSH_ONLY = '_git("fetch", "origin", cwd=cwd)\n    push_with_retry(cwd)\n'  # no git add
_DELEGATOR = 'subprocess.run(["python3", "tools/ticket.py", "file", ...])\n'  # commits via ticket.py
_DEFAULT_REFUSE = 'def _enforce_default_safe(args):\n    return 64\n'


class _Fixture:
    """Build a temp repo-shaped dir: tools/<name>.py + tools/systemd/<unit>.service."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tools = Path(self._tmp.name) / "tools"
        self.systemd = self.tools / "systemd"
        self.systemd.mkdir(parents=True)

    def script(self, name: str, body: str) -> "_Fixture":
        (self.tools / name).write_text(f"#!/usr/bin/env python3\n{body}\n")
        return self

    def unit(self, unit_name: str, script: str, flag: str = "") -> "_Fixture":
        execstart = f"/usr/bin/python3 %h/pantheon-config/tools/{script} {flag}".rstrip()
        (self.systemd / unit_name).write_text(
            f"[Service]\nType=oneshot\nExecStart={execstart}\n"
        )
        return self

    def run(self) -> list:
        return lint(tools_dir=self.tools, systemd_dir=self.systemd)

    def cleanup(self):
        self._tmp.cleanup()


def _by_name(findings, name):
    return next((f for f in findings if f.script == name), None)


class TestContractLint(unittest.TestCase):
    def setUp(self):
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_violation_true_positive(self):
        """Ticket-mandated: add+push, named in a BARE unit, no refuse gate -> VIOLATION."""
        self.fx.script("groomy.py", _ADD_AND_PUSH).unit("groomy.service", "groomy.py")  # bare
        f = _by_name(self.fx.run(), "groomy.py")
        self.assertIsNotNone(f)
        self.assertIs(f.severity, Severity.VIOLATION)
        self.assertTrue(any("bare" in r for r in f.reasons))
        self.assertTrue(any("default-refuse" in r for r in f.reasons))

    def test_pantheon_config_sync_true_negative(self):
        """Ticket-mandated: the exception (push, NO git add) is not flagged."""
        self.fx.script("pantheon_config_sync.py", _PUSH_ONLY).unit("sync.service", "pantheon_config_sync.py")
        self.assertIsNone(_by_name(self.fx.run(), "pantheon_config_sync.py"))

    def test_whitelist_invariant_breach_is_loud(self):
        """If the whitelisted exception ever authors commits, fire a LOUD VIOLATION."""
        self.fx.script("pantheon_config_sync.py", _ADD_AND_PUSH).unit("sync.service", "pantheon_config_sync.py")
        f = _by_name(self.fx.run(), "pantheon_config_sync.py")
        self.assertIsNotNone(f)
        self.assertIs(f.severity, Severity.VIOLATION)
        self.assertTrue(any("exemption rationale" in r for r in f.reasons))

    def test_pushonly_not_flagged(self):
        """A non-whitelisted push-only script (no git add) is out of scope structurally."""
        self.fx.script("deployer.py", _PUSH_ONLY).unit("deployer.service", "deployer.py")
        self.assertIsNone(_by_name(self.fx.run(), "deployer.py"))

    def test_delegator_not_flagged(self):
        """Commits only via ticket.py subprocess (no direct _git add) -> out of scope."""
        self.fx.script("sweeper.py", _DELEGATOR).unit("sweeper.service", "sweeper.py")
        self.assertIsNone(_by_name(self.fx.run(), "sweeper.py"))

    def test_interactive_not_in_scope(self):
        """Authors+pushes but NOT wired to any unit -> interactive/one-off, not a cron."""
        self.fx.script("oneoff.py", _ADD_AND_PUSH)  # no unit
        self.assertIsNone(_by_name(self.fx.run(), "oneoff.py"))

    def test_conforming_script_passes(self):
        """add+push + refuse gate + unit passes --commit-prod -> clean."""
        self.fx.script("good.py", _ADD_AND_PUSH + _DEFAULT_REFUSE).unit("good.service", "good.py", "--commit-prod")
        self.assertIsNone(_by_name(self.fx.run(), "good.py"))

    def test_non_canonical_mode_flag_passes(self):
        """Unit uses a non --commit-prod mode flag + script has the gate -> clean (watch_runner shape)."""
        self.fx.script("watch.py", _ADD_AND_PUSH + _DEFAULT_REFUSE).unit("watch.service", "watch.py", "--file-watch-ticket")
        self.assertIsNone(_by_name(self.fx.run(), "watch.py"))

    def test_safe_default_is_advisory_not_violation(self):
        """In scope, unit passes a flag, but NO refuse gate -> ADVISORY, not VIOLATION."""
        self.fx.script("weekly.py", _ADD_AND_PUSH).unit("weekly.service", "weekly.py", "--file-request")
        f = _by_name(self.fx.run(), "weekly.py")
        self.assertIsNotNone(f)
        self.assertIs(f.severity, Severity.ADVISORY)

    def test_self_excluded(self):
        """The linter must never flag itself (read-only, no git add)."""
        # The real module name is excluded even if a fixture happens to name a file that way.
        self.fx.script("auto_commit_contract_lint.py", _ADD_AND_PUSH).unit("acl.service", "auto_commit_contract_lint.py")
        self.assertIsNone(_by_name(self.fx.run(), "auto_commit_contract_lint.py"))

    def test_exit_code_semantics(self):
        findings_viol = self.fx.script("g.py", _ADD_AND_PUSH).unit("g.service", "g.py").run()
        self.assertEqual(exit_code(findings_viol), 1)

        fx2 = _Fixture()
        self.addCleanup(fx2.cleanup)
        findings_adv = fx2.script("w.py", _ADD_AND_PUSH).unit("w.service", "w.py", "--file-request").run()
        self.assertTrue(all(f.severity is Severity.ADVISORY for f in findings_adv))
        self.assertEqual(exit_code(findings_adv), 0)
        self.assertEqual(exit_code(findings_adv, strict=True), 1)


class TestRealRepoSnapshot(unittest.TestCase):
    """Pin the real-repo invariant: the live fleet has NO contract VIOLATIONs.

    History: the T-1072 build found 2 live VIOLATIONs (spine_index_groom,
    promote_lessons_cron); both were remediated under T-1077 (2026-06-11), adding
    --commit-prod + an _enforce_default_safe gate and updating their units. This
    test now guards against a REGRESSION — if a new bare-unit auto-commit cron
    lands, it fails here. (ADVISORY-tier findings, e.g. weekly_ticket_summary, are
    tolerated — they don't fail the gate.)
    """

    def test_real_repo_has_no_violations(self):
        findings = lint()  # real repo
        viols = [f.script for f in findings if f.severity is Severity.VIOLATION]
        self.assertEqual(
            viols, [],
            f"live auto-commit-cron contract VIOLATION(s) detected: {viols} — a new "
            f"bare-unit auto-commit cron regressed the fleet; remediate per the SOP.",
        )

    def test_exception_not_flagged(self):
        findings = lint()
        self.assertNotIn("pantheon_config_sync.py", {f.script for f in findings})


if __name__ == "__main__":
    unittest.main()
