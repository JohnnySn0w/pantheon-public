#!/usr/bin/env python3
"""Tests for tools/hooks/handoff-auto-read.sh — the SessionStart orientation hook.

Focus is the T-2166 oversize guard: a handoff past the ~50KB single-Read limit must
announce its own incompleteness BEFORE the session reads it. The hook is fail-open by
contract (it must never block session start), so every test also asserts exit 0.
"""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "tools" / "hooks" / "handoff-auto-read.sh"


class HookHarness(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.handoff = self.home / "HANDOFF.md"

    def write_handoff(self, nbytes, age_seconds=0):
        """Write a HANDOFF.md of ~nbytes, aged `age_seconds` into the past."""
        line = "x" * 79 + "\n"
        self.handoff.write_text(line * (nbytes // 80))
        if age_seconds:
            past = time.time() - age_seconds
            os.utime(self.handoff, (past, past))
        return self.handoff.stat().st_size

    def run_hook(self, **env_extra):
        env = dict(os.environ, HOME=str(self.home))
        env.update({k: str(v) for k, v in env_extra.items()})
        proc = subprocess.run(
            ["bash", str(HOOK)], input="{}", capture_output=True, text=True, env=env
        )
        self.assertEqual(proc.returncode, 0, msg=f"hook must never fail: {proc.stderr}")
        return proc.stdout


class TestReadInstruction(HookHarness):
    def test_absent_handoff_injects_no_read_instruction(self):
        out = self.run_hook()
        self.assertNotIn("HANDOFF.md", out)

    def test_fresh_handoff_injects_read_instruction(self):
        self.write_handoff(1000)
        out = self.run_hook()
        self.assertIn("HANDOFF.md and follow it", out)

    def test_stale_handoff_is_not_injected(self):
        self.write_handoff(1000, age_seconds=7200)
        out = self.run_hook()
        self.assertNotIn("HANDOFF.md and follow it", out)


class TestOversizeGuard(HookHarness):
    def test_small_handoff_gets_no_warning(self):
        self.write_handoff(1000)
        self.assertNotIn("WARNING", self.run_hook())

    def test_just_under_threshold_gets_no_warning(self):
        self.write_handoff(44000)
        self.assertNotIn("WARNING", self.run_hook())

    def test_oversize_handoff_warns(self):
        size = self.write_handoff(60000)
        out = self.run_hook()
        self.assertIn("WARNING", out)
        self.assertIn(str(size), out, "the warning must name the ACTUAL byte count")
        self.assertIn("offset=0", out, "the remedy must be stated, not implied")

    def test_warning_names_the_line_count(self):
        self.write_handoff(60000)
        lines = len(self.handoff.read_text().splitlines())
        self.assertIn(f"{lines} lines", self.run_hook())

    def test_warning_precedes_nothing_when_read_is_not_injected(self):
        """A stale oversize handoff is not read, so it must not warn either."""
        self.write_handoff(60000, age_seconds=7200)
        self.assertNotIn("WARNING", self.run_hook())

    def test_pass_count_scales_with_size(self):
        self.write_handoff(60000)
        self.assertIn("at least 2 passes", self.run_hook())
        self.write_handoff(140000)
        self.assertIn("at least 4 passes", self.run_hook())

    def test_threshold_is_overridable(self):
        self.write_handoff(1000)
        self.assertIn("WARNING", self.run_hook(HANDOFF_WARN_BYTES=500))

    def test_warning_does_not_call_bytes_the_cap(self):
        """T-2166: the cap's char-vs-byte semantics are unsettled; don't assert them."""
        self.write_handoff(60000)
        out = self.run_hook()
        self.assertNotIn("45000 byte cap", out)
        self.assertIn("~50KB", out)

    def test_warning_does_not_claim_one_read_fails_inside_the_margin(self):
        """T-2317: the guard fires at 45,000 but real truncation is ~50KB.

        Between the two, the old text ("past the ~50KB single-Read limit: ONE
        Read will NOT deliver it") was simply FALSE — one Read does deliver the
        whole file. A warning that overstates in the band it most often fires in
        trains the reader to discount it, which costs exactly when the file IS
        oversize. Sister to test_warning_does_not_call_bytes_the_cap: both keep
        this message from asserting more than was measured.
        """
        self.write_handoff(47000)  # inside the margin: warns, but one Read suffices
        out = self.run_hook()
        self.assertIn("WARNING", out, "45,000 is still the right place to warn")
        self.assertNotIn("ONE Read will NOT deliver it", out)
        # both numbers, and the relationship between them, must be stated
        self.assertIn("45,000", out)
        self.assertIn("~50KB", out)
        self.assertIn("still delivers", out)

    def test_warning_still_states_the_tail_loss_above_real_truncation(self):
        """The correction must not soften the case it exists for."""
        self.write_handoff(140000)
        out = self.run_hook()
        self.assertIn("TAIL", out)
        self.assertIn("at least 4 passes", out)

    def test_warning_points_at_the_prune_sop(self):
        self.write_handoff(60000)
        self.assertIn("end-of-work-notes-consolidation.md", self.run_hook())


class TestFailOpen(HookHarness):
    def test_unreadable_handoff_still_exits_zero(self):
        self.write_handoff(60000)
        os.chmod(self.handoff, 0o000)
        self.addCleanup(os.chmod, self.handoff, 0o644)
        self.run_hook()  # the assertion is inside run_hook: returncode must be 0

    def test_org_readme_injected_when_present(self):
        (self.home / "pantheon-config").mkdir()
        (self.home / "pantheon-config" / "README.md").write_text("# org")
        self.assertIn("for org orientation", self.run_hook())


class TestLiveHandoffSizes(unittest.TestCase):
    """T-2166 asked for a measurement, not just a guard: report (do not fail on) the
    live handoff sizes this account can read. Informational — a persona's handoff being
    oversize is that persona's prune, and failing another agent's suite for it would be
    both wrong-owner and flaky."""

    def test_this_accounts_handoff_is_within_the_guard(self):
        own = Path.home() / "HANDOFF.md"
        if not own.exists():
            self.skipTest("no HANDOFF.md for this account")
        size = own.stat().st_size
        self.assertLess(
            size, 45000,
            msg=f"{own} is {size} bytes — past the guard threshold; prune it per "
                "sop/standard/end-of-work-notes-consolidation.md item 3 (durable-first).",
        )


if __name__ == "__main__":
    unittest.main()
