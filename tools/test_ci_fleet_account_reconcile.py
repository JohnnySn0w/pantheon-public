"""Tests for ci_fleet_account_reconcile (T-2219 / ORG-066 Rung 1).

The load-bearing guarantees, in priority order:
  1. --check NEVER mutates (C3-d) — the drift detector cannot create an account.
  2. FAIL-LOUD on conflict, never clobber (C3-b) — a foreign account on our uid stops --apply
     before any mutation; a diverged fleet account is DRIFT, not silently re-homed.
  3. MANIFEST-SET ONLY (C3-a) — non-fleet accounts (the runner, root) are never created/touched.
  4. UID CONTRACT — recovery=1001 / specialist=1002 (the test-pinned seats) or the getpwnam tests break.
  5. Idempotent — an in-sync host yields no actions and exit 0.
"""
import shlex
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_fleet_account_reconcile as r  # noqa: E402


def E(name, uid, home=None, shell="/usr/sbin/nologin", gid=None):
    return r.PasswdEntry(name=name, uid=uid, gid=gid if gid is not None else uid,
                         home=home or f"/home/{name}", shell=shell)


class FakeOps:
    """Records create calls; serves a scripted passwd. Never shells out."""

    def __init__(self, passwd):
        self._passwd = list(passwd)
        self.created = []

    def read_passwd(self):
        return list(self._passwd)

    def create_account(self, spec):
        self.created.append(spec)
        self._passwd.append(E(spec.name, spec.uid, spec.home, spec.shell))


# The runner account CT202 actually carries — must always be left untouched.
RUNNER = E("runner", 1000)


class DesiredSet(unittest.TestCase):
    def test_eight_accounts_pinned_uids_no_dups(self):
        specs = r.desired_accounts()
        self.assertEqual(len(specs), 8)
        by_name = {s.name: s.uid for s in specs}
        self.assertEqual(by_name["claude-fallback"], 1001)   # test contract
        self.assertEqual(by_name["claude-research"], 1002)   # test contract
        uids = [s.uid for s in specs]
        self.assertEqual(len(set(uids)), len(uids), "no duplicate uids")
        # No desired uid collides with the CT202 runner (1000).
        self.assertNotIn(1000, uids)

    def test_desired_uids_are_exact_frozen_constants(self):
        # orchestrator T-2219 review: the six non-pinned uids are FROZEN explicit constants
        # (_RESERVED_UIDS), not index-derived. Assert the WHOLE map exactly, so any silent
        # renumber — the footgun the explicit map exists to prevent — is caught by CI.
        by_name = {s.name: s.uid for s in r.desired_accounts()}
        self.assertEqual(by_name, {
            "claude": 6000,
            "claude-fallback": 1001,   # test contract (pinned)
            "claude-research": 1002,   # test contract (pinned)
            "codex": 6003,
            "exocortex": 6004,
            "suborch1": 6005,
            "suborch2": 6006,
            "the-ancestor": 6007,
        })

    def test_unmapped_fleet_account_raises(self):
        # orchestrator T-2219 review forcing-function: a fleet account with neither a pinned nor a
        # reserved uid must fail LOUD (surface the identity decision) — never auto-number.
        import agent_role_map as arm  # noqa: E402
        with mock.patch.object(arm, "fleet_accounts",
                               return_value={"claude", "brand-new-daemon"}):
            with self.assertRaises(SystemExit) as ctx:
                r.desired_accounts()
        self.assertIn("brand-new-daemon", str(ctx.exception))

    def test_posture_is_inert(self):
        for s in r.desired_accounts():
            self.assertEqual(s.shell, "/usr/sbin/nologin")
            self.assertEqual(s.home, f"/home/{s.name}")
            self.assertEqual(s.gid, s.uid)  # private group


class Classify(unittest.TestCase):
    def _one(self, name):
        return next(s for s in r.desired_accounts() if s.name == name)

    def test_missing(self):
        spec = self._one("claude-fallback")
        v = r.classify(spec, {}, {})
        self.assertEqual(v["verdict"], r.MISSING)

    def test_ok_when_present_and_correct(self):
        spec = self._one("claude-fallback")
        e = E("claude-fallback", 1001)
        v = r.classify(spec, {"claude-fallback": e}, {1001: e})
        self.assertEqual(v["verdict"], r.OK)

    def test_drift_wrong_shell(self):
        spec = self._one("claude-fallback")
        e = E("claude-fallback", 1001, shell="/bin/bash")
        v = r.classify(spec, {"claude-fallback": e}, {1001: e})
        self.assertEqual(v["verdict"], r.DRIFT)

    def test_drift_wrong_uid(self):
        spec = self._one("claude-fallback")
        e = E("claude-fallback", 9999)
        v = r.classify(spec, {"claude-fallback": e}, {9999: e})
        self.assertEqual(v["verdict"], r.DRIFT)

    def test_conflict_foreign_account_on_our_uid(self):
        # Someone unrelated holds our target uid -> CONFLICT, never clobber.
        spec = self._one("claude-fallback")  # wants 1001
        foreign = E("someone-else", 1001)
        v = r.classify(spec, {}, {1001: foreign})
        self.assertEqual(v["verdict"], r.CONFLICT)
        self.assertIn("someone-else", v["detail"])


class CheckNeverMutates(unittest.TestCase):
    def test_check_on_bare_host_reports_all_missing_and_creates_nothing(self):
        ops = FakeOps([RUNNER])  # only the runner exists
        results, code = r.reconcile(ops=ops, apply=False)
        self.assertEqual(ops.created, [], "--check must not create anything (C3-d)")
        self.assertEqual(code, 1, "drift present -> non-zero")
        self.assertTrue(all(x["verdict"] == r.MISSING for x in results))
        # The runner account is never in the result set (manifest-set only, C3-a).
        self.assertNotIn("runner", [x["account"] for x in results])

    def test_check_in_sync_is_exit_zero(self):
        passwd = [RUNNER] + [E(s.name, s.uid) for s in r.desired_accounts()]
        ops = FakeOps(passwd)
        results, code = r.reconcile(ops=ops, apply=False)
        self.assertEqual(code, 0)
        self.assertTrue(all(x["verdict"] == r.OK for x in results))
        self.assertEqual(ops.created, [])


class ApplyBoundaries(unittest.TestCase):
    def test_apply_creates_missing_only(self):
        ops = FakeOps([RUNNER])
        results, code = r.reconcile(ops=ops, apply=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(ops.created), 8)
        self.assertNotIn("runner", [s.name for s in ops.created])  # never the runner
        # Re-apply is idempotent: everything now OK, nothing created.
        results2, code2 = r.reconcile(ops=ops, apply=True)
        self.assertEqual(code2, 0)
        self.assertEqual(len(ops.created), 8, "idempotent: no second creation")

    def test_apply_refuses_all_mutation_on_conflict(self):
        # A foreign account squats claude-fallback's uid (1001) -> --apply must create NOTHING.
        ops = FakeOps([RUNNER, E("squatter", 1001)])
        results, code = r.reconcile(ops=ops, apply=True)
        self.assertEqual(code, 2, "conflict -> hard refuse")
        self.assertEqual(ops.created, [], "no account created when any conflict is present")
        self.assertTrue(any(x["verdict"] == r.CONFLICT for x in results))

    def test_apply_refuses_all_mutation_on_drift(self):
        # An existing fleet account with the wrong shell blocks --apply (never silently fix).
        drifted = E("claude-fallback", 1001, shell="/bin/bash")
        ops = FakeOps([RUNNER, drifted])
        results, code = r.reconcile(ops=ops, apply=True)
        self.assertEqual(code, 2)
        self.assertEqual(ops.created, [])


class NoLingerInvariant(unittest.TestCase):
    def test_create_account_argv_has_no_linger_or_systemd(self):
        # C1: the reconciler must never emit linger / user-manager / systemd.
        captured = {}

        class RecordingOps(r.Ops):
            def _run(self, argv, *, check=False):
                captured["argv"] = argv
                class _R:  # noqa: N801
                    returncode = 0
                    stderr = ""
                return _R()

        spec = r.AccountSpec(name="codex", uid=6001)
        RecordingOps().create_account(spec)
        argv = captured["argv"]
        joined = " ".join(argv)
        self.assertIn("useradd", joined)
        self.assertIn("/usr/sbin/nologin", joined)
        # Forbidden as substrings anywhere in the command (linger / systemd surface).
        for forbidden in ("loginctl", "linger", "systemctl", "systemd-run"):
            self.assertNotIn(forbidden, joined, f"C1 violation: {forbidden} in useradd argv")
        # systemd's `--user` flag is an EXACT token; useradd's `--user-group` (private group)
        # is legitimate and must not trip this. So check token equality, not substring.
        self.assertNotIn("--user", argv, "C1 violation: systemd --user flag in useradd argv")


class ExecQuoting(unittest.TestCase):
    """Regression: argv routed through an ssh-style exec_prefix is flattened + re-parsed by the
    REMOTE shell, so tokens with spaces/parens (the --comment) must be shell-quoted or the
    remote shell splits/syntax-errors. Reproduces the first-CT202-apply failure."""

    _PREFIX = ["ssh", "pve2", "pct", "exec", "202", "--"]

    def _capture_full(self, ops, method_call):
        captured = {}

        def fake_run(full, **kw):
            captured["full"] = full
            return subprocess.CompletedProcess(full, 0, "", "")

        with mock.patch.object(r.subprocess, "run", fake_run):
            method_call(ops)
        return captured["full"]

    def test_remote_comment_survives_shell_reparse(self):
        ops = r.Ops(exec_prefix=self._PREFIX)
        full = self._capture_full(ops, lambda o: o.create_account(r.AccountSpec("claude", 6000)))
        # Prefix is passed through verbatim; only the argv tail is quoted.
        self.assertEqual(full[: len(self._PREFIX)], self._PREFIX)
        # ssh concatenates the tail into one string the REMOTE shell re-splits — emulate that.
        remote_cmd = " ".join(full[len(self._PREFIX):])
        argv = shlex.split(remote_cmd)
        self.assertEqual(argv[0], "useradd")
        ci = argv.index("--comment")
        # The whole comment (spaces + the "(T-2219)" that broke the bare form) is ONE token.
        self.assertEqual(argv[ci + 1], r.ACCOUNT_COMMENT)
        self.assertIn("(T-2219)", argv[ci + 1])
        self.assertEqual(argv[-1], "claude")

    def test_local_exec_is_not_shell_quoted(self):
        # No prefix => argv is exec'd directly (no shell in the path); tokens must stay literal,
        # NOT wrapped in shell quotes (which would reach useradd as part of the value).
        ops = r.Ops(exec_prefix=[])
        full = self._capture_full(ops, lambda o: o.create_account(r.AccountSpec("claude", 6000)))
        self.assertEqual(full[0], "useradd")
        self.assertIn(r.ACCOUNT_COMMENT, full)  # the raw comment token, unquoted


class CiCheckHookWired(unittest.TestCase):
    """The reconciler --check drift guard must stay WIRED into ci.yml (T-2219). The T-2330
    lesson: presence in the tree is not activation. If a ci.yml refactor drops the step, the
    26 getpwnam failures return silently as a false-red — this makes that a test failure."""

    def _ci_yml(self):
        import pathlib
        return (pathlib.Path(__file__).resolve().parent.parent
                / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    def test_ci_yml_runs_the_check_drift_guard_before_the_suite(self):
        ci = self._ci_yml()
        needle = "ci_fleet_account_reconcile.py --check"
        self.assertIn(needle, ci,
                      "ci.yml must run the fleet-account --check drift guard (T-2219) — without "
                      "it a runner missing the 8 fleet accounts fails the suite with 26 opaque "
                      "getpwnam KeyErrors instead of one actionable line")
        # It must precede the suite run, or it stops being a fail-FAST pre-job gate.
        self.assertLess(ci.index(needle), ci.index("pytest tools/ -q"),
                        "the --check guard must run BEFORE the pytest suite (fail-fast)")


class RemedyFooter(unittest.TestCase):
    def test_check_drift_names_the_apply_remedy(self):
        # A failing --check must surface the actionable fix, not just per-account lines.
        # main() builds its own Ops, so drive the drift by patching the passwd read to a
        # bare host (only the runner present) — all 8 fleet accounts then read MISSING.
        import contextlib
        import io
        bare = [r.PasswdEntry("runner", 1000, 1000, "/home/runner", "/bin/bash")]
        err = io.StringIO()
        with mock.patch.object(r.Ops, "read_passwd", lambda self: list(bare)):
            with contextlib.redirect_stderr(err):
                code = r.main(["--check"])
        out = err.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("REMEDY", out)
        self.assertIn("--apply", out)


if __name__ == "__main__":
    unittest.main()
