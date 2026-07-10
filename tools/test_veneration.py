"""test_veneration.py — resume-the-dead harness orchestration (tools/veneration.py, T-0766).

Pure-orchestration tests via an injected fake Ops + fake lease — no claude subprocess. The
harvest is now a single headless `claude --print` call (Ops.harvest); these cover the happy
path, every failure branch, and the load-bearing invariant: the lease is ALWAYS released,
regardless of outcome. HarvestArgvTest covers the real argv the FakeOps stubs out.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import veneration as ven  # noqa: E402


class FakeLease:
    def __init__(self, acquirable=True):
        self._acquirable = acquirable
        self.acquired = False
        self.released = False

    def acquire(self):
        if self._acquirable:
            self.acquired = True
        return self._acquirable

    def release(self):
        self.released = True

    def read_record(self):
        return {"pid": 999, "label": "veneration:other"}


class FakeOps(ven.Ops):
    """Records calls; each step's result is configurable."""
    def __init__(self, *, harvest=True, written=True):
        self._harvest, self._written = harvest, written
        self.calls = []

    def harvest(self, dead_jsonl, prompt, *, claude_bin, run_as, timeout):
        self.calls.append(("harvest", dead_jsonl, prompt, run_as)); return self._harvest

    def handoff_exists_nonempty(self, path):
        self.calls.append(("exists", path)); return self._written

    def _names(self):
        return [c[0] for c in self.calls]


class ResurrectTest(unittest.TestCase):
    def _run(self, ops, lease=None):
        lease = lease or FakeLease()
        res = ven.resurrect("recovery", "/dead.jsonl", "/home/claude-fallback/HANDOFF.md",
                            ops=ops, lease=lease, run_as="the-ancestor")
        return res, lease

    # ------------------------------------------------------------------- happy
    def test_happy_path(self):
        ops = FakeOps()
        res, lease = self._run(ops)
        self.assertTrue(res.ok, res.detail)
        self.assertEqual(res.status, "ok")
        # harvest, then confirm-written, then released
        self.assertEqual(ops._names(), ["harvest", "exists"])
        self.assertTrue(lease.released)

    def test_harvest_carries_channelless_prompt(self):
        p = ven.harvest_prompt("/home/claude/HANDOFF.md").lower()
        self.assertIn("write /home/claude/handoff.md", p)
        self.assertIn("stop", p)
        self.assertIn("channel", p)  # the no-channel safety reminder

    # ------------------------------------------------------------------ skipped
    def test_lease_held_skips_without_harvesting(self):
        ops = FakeOps()
        res, lease = self._run(ops, lease=FakeLease(acquirable=False))
        self.assertEqual(res.status, "skipped")
        self.assertEqual(ops._names(), [], "must not harvest when the lease is held")

    # ------------------------------------------------------------------- failed
    def test_harvest_subprocess_failure_releases(self):
        ops = FakeOps(harvest=False)
        res, lease = self._run(ops)
        self.assertEqual(res.status, "failed")
        self.assertIn("harvest subprocess", res.detail)
        self.assertEqual(ops._names(), ["harvest"], "no exists-check after a failed harvest")
        self.assertTrue(lease.released)

    def test_clean_exit_but_no_handoff_written_fails_and_releases(self):
        ops = FakeOps(written=False)
        res, lease = self._run(ops)
        self.assertEqual(res.status, "failed")
        self.assertIn("no HANDOFF", res.detail)
        self.assertEqual(ops._names(), ["harvest", "exists"])
        self.assertTrue(lease.released)

    # --------------------------------------------------------------- invariants
    def test_lease_released_on_exception(self):
        class BoomOps(FakeOps):
            def harvest(self, dead_jsonl, prompt, *, claude_bin, run_as, timeout):
                raise RuntimeError("boom")
        lease = FakeLease()
        with self.assertRaises(RuntimeError):
            ven.resurrect("recovery", "/d.jsonl", "/h", ops=BoomOps(), lease=lease)
        self.assertTrue(lease.released, "lease must release even if an op raises")


class ValidationTest(unittest.TestCase):
    def test_accepts_clean_inputs(self):
        self.assertIsNone(ven.validate_request(
            "recovery", "/home/claude-fallback/.claude/projects/x/abc-123.jsonl"))

    def test_rejects_injection_in_jsonl(self):
        for bad in ["/x.jsonl' --channels telegram '", "/x.jsonl; rm -rf ~",
                    "/x.jsonl $(whoami)", "/x.jsonl\nfoo", "../etc/x.jsonl",
                    "/x.txt", "relative.jsonl", "/has space.jsonl"]:
            self.assertIsNotNone(ven.validate_request("recovery", bad), f"should reject {bad!r}")

    def test_rejects_bad_agent(self):
        for bad in ["recovery; rm -rf ~ #", "Recovery", "../x", "a b", "", "1agent", "a'b"]:
            self.assertIsNotNone(ven.validate_request(bad, "/x.jsonl"), f"should reject {bad!r}")


class SecurityTest(unittest.TestCase):
    def test_resurrect_rejects_injection_before_lease_or_harvest(self):
        # The critical bug: an untrusted dead_jsonl smuggling --channels must be rejected
        # BEFORE the lease is acquired or any command is built.
        ops = FakeOps()
        lease = FakeLease()
        res = ven.resurrect("recovery", "/x.jsonl' --channels telegram '", "/h",
                            ops=ops, lease=lease)
        self.assertEqual(res.status, "failed")
        self.assertIn("rejected", res.detail)
        self.assertFalse(lease.acquired, "must reject before acquiring the lease")
        self.assertEqual(ops._names(), [], "must never touch ops on an injection payload")

    def test_resurrect_rejects_bad_agent(self):
        ops = FakeOps()
        res = ven.resurrect("recovery; rm -rf ~ #", "/d.jsonl", "/h", ops=ops, lease=FakeLease())
        self.assertEqual(res.status, "failed")
        self.assertEqual(ops._names(), [])


class HarvestArgvTest(unittest.TestCase):
    """The real Ops.harvest argv construction (FakeOps stubs it out, so this is the only
    coverage). Regression guard for the T-0766 first-fire's surfaced bugs: `--resume` takes a
    SESSION ID (the .jsonl basename) not a path; the unattended write needs headless --print +
    bypassPermissions (no consent screen); --safe-mode strips hooks/plugins; NO --channels."""

    DEAD = ("/home/the-ancestor/.claude/projects/-home-the-ancestor/"
            "5c9d9490-9757-4059-9aa8-f9e75157d484.jsonl")

    def _capture_argv(self, *, run_as):
        import subprocess
        captured = {}

        class _Done:
            returncode = 0

        def fake_run(argv, **kw):
            captured["argv"] = argv
            captured["kw"] = kw
            return _Done()

        orig = subprocess.run
        subprocess.run = fake_run
        try:
            ok = ven.Ops().harvest(
                self.DEAD, ven.harvest_prompt("/home/the-ancestor/.veneration/h.md"),
                claude_bin="/home/the-ancestor/.npm-global/bin/claude",
                run_as=run_as, timeout=300,
            )
        finally:
            subprocess.run = orig
        return ok, captured["argv"]

    def test_headless_argv_shape(self):
        ok, argv = self._capture_argv(run_as=None)
        self.assertTrue(ok)
        # bash -c <script>; the cd-into-home + exec live in the script body (last element).
        self.assertEqual(argv[-3], "bash")
        self.assertEqual(argv[-2], "-c")
        script = argv[-1]
        self.assertTrue(script.startswith("cd "), "must cd into the project home first")
        self.assertIn(" exec ", script)
        # session id, not the transcript path.
        self.assertIn("--resume 5c9d9490-9757-4059-9aa8-f9e75157d484", script)
        self.assertNotIn(".jsonl", script, "must pass the session id, never the transcript path")
        # headless, unattended, sandboxed.
        self.assertIn("--print", script)
        self.assertIn("--permission-mode bypassPermissions", script)
        self.assertIn("--safe-mode", script)
        self.assertNotIn("--channels", script, "channel-less safety invariant: never --channels")

    def test_run_as_prefixes_sudo(self):
        # An unknown user fails closed; a known one (root always exists) gets a sudo prefix.
        self.assertFalse(
            ven.Ops().harvest("/x/abc.jsonl", "p", claude_bin="/c",
                              run_as="nonexistent_user_zzz", timeout=10),
            "unknown run_as must fail-closed (no passwd entry → no harvest)")
        ok, argv = self._capture_argv(run_as="root")
        self.assertEqual(argv[:4], ["sudo", "-n", "-u", "root"])


if __name__ == "__main__":
    unittest.main()
