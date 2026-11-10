"""Tests for the ORG-101 §2 gated push order in _git_utils.push_with_retry (T-4690).

Fake remotes are local bare repos; nothing touches the network (the "unreachable"
origin is a loopback port nothing listens on). HERMES discipline: the trigger is
only ever assembled at runtime from split pieces.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import _git_utils
from tools._git_utils import _classify_push_failure, push_with_retry

REAL_HOOKS = Path(__file__).resolve().parent.parent / ".githooks"
DEAD_ORIGIN = "https://127.0.0.1:1/nothing.git"  # connection refused, loopback only

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com",
}
_ENV = {
    **os.environ,
    **_GIT_IDENTITY,
    "GIT_TERMINAL_PROMPT": "0",
}


def _run(cmd, cwd):
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, env=_ENV)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd!r}: {r.stderr}")
    return r


def _head(path: Path, ref="refs/heads/main") -> str:
    r = subprocess.run(["git", "ls-remote", str(path), ref], capture_output=True, text=True)
    return r.stdout.split("\t")[0] if r.stdout.strip() else ""


def _commit(repo: Path, name: str, msg: str = "c") -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _run(["git", "add", name], repo)
    _run(["git", "commit", "-q", "-m", msg], repo)
    return _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()


def _hook(bare: Path, body: str) -> None:
    h = bare / "hooks" / "pre-receive"
    h.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    h.chmod(h.stat().st_mode | stat.S_IEXEC)


def _advance(base: Path, bare: Path, name: str) -> str:
    clone = base / f"clone-{name}"
    _run(["git", "clone", "-q", str(bare), str(clone)], base)
    _commit(clone, name)
    _run(["git", "push", "-q", "origin", "HEAD:main"], clone)
    return _head(bare)


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.origin = self.base / "origin.git"
        self.mirror = self.base / "mirror.git"
        for b in (self.origin, self.mirror):
            b.mkdir()
            _run(["git", "init", "-q", "--bare", "-b", "main"], b)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        _run(["git", "init", "-q", "-b", "main"], self.repo)
        _run(["git", "remote", "add", "origin", str(self.origin)], self.repo)
        self.baseline = _commit(self.repo, "BASE", "baseline")
        _run(["git", "push", "-q", "origin", "main"], self.repo)
        _run(["git", "push", "-q", str(self.mirror), "main"], self.repo)
        _run(["git", "checkout", "-q", "--detach"], self.repo)

    def new_commit(self, name="new") -> str:
        return _commit(self.repo, name)

    def push(self, **kw):
        # push_with_retry owns its fetch/rebase subprocesses.  Give those children the
        # same fixture identity as the setup commits; a clean CI worker must not need
        # ~/.gitconfig merely because a non-FF path rewrites the temporary commit.
        with unittest.mock.patch.dict(os.environ, _GIT_IDENTITY):
            return push_with_retry(self.repo, refspec="HEAD:main", **kw)

    def _fake_origin_push(self, stderr):
        real = _git_utils._git

        def fake(*args, cwd=None):
            if args[:2] == ("push", "origin"):
                return subprocess.CompletedProcess(args, 128, "", stderr)
            return real(*args, cwd=cwd) if cwd is not None else real(*args)

        return unittest.mock.patch.object(_git_utils, "_git", side_effect=fake)


class WritableMirrorTest(_Base):
    def test_origin_accepts_then_mirror_pushed(self):
        sha = self.new_commit()
        ok, msg = self.push(mirror_url=str(self.mirror))
        self.assertTrue(ok, msg)
        self.assertEqual(_head(self.origin), sha)
        self.assertEqual(_head(self.mirror), sha)

    def test_mirror_failure_is_non_fatal(self):
        _hook(self.mirror, "echo 'remote: nope' >&2; exit 1")
        sha = self.new_commit()
        ok, msg = self.push(mirror_url=str(self.mirror))
        self.assertTrue(ok, msg)
        self.assertEqual(_head(self.origin), sha)
        self.assertEqual(_head(self.mirror), self.baseline)
        self.assertIn("mirror push failed", msg)
        self.assertIn("reconciler", msg)

    def test_mirror_non_ff_is_non_fatal_and_not_retried(self):
        _advance(self.base, self.mirror, "mirror-only")
        self.new_commit()
        ok, msg = self.push(mirror_url=str(self.mirror))
        self.assertTrue(ok, msg)
        self.assertIn("mirror push failed", msg)

    def test_non_writable_default_pushes_no_mirror(self):
        _hook(self.mirror, f"touch '{self.base}/mirror-was-pushed'; exit 0")
        self.new_commit()
        ok, msg = self.push(mirror_url=None)
        self.assertTrue(ok, msg)
        self.assertFalse((self.base / "mirror-was-pushed").exists())
        self.assertEqual(_head(self.mirror), self.baseline)
        self.assertNotIn("mirror", msg)

    def test_unset_mirror_resolves_from_manifest_and_noops_when_none(self):
        self.new_commit()
        with unittest.mock.patch.object(_git_utils, "_resolve_write_mirror", return_value=None) as m:
            ok, msg = self.push()
        self.assertTrue(ok, msg)
        m.assert_called_once()
        self.assertEqual(_head(self.mirror), self.baseline)


class OriginUnreachableTest(_Base):
    def setUp(self):
        super().setUp()
        _run(["git", "remote", "set-url", "origin", DEAD_ORIGIN], self.repo)

    def test_unreachable_origin_pushes_to_mirror_only(self):
        sha = self.new_commit()
        ok, msg = self.push(mirror_url=str(self.mirror))
        self.assertTrue(ok, msg)
        self.assertEqual(_head(self.mirror), sha)
        self.assertIn("pushed to Forgejo only", msg)
        self.assertIn("replay to GitHub pending", msg)

    def test_unreachable_without_writable_mirror_fails_as_today(self):
        self.new_commit()
        ok, msg = self.push(mirror_url=None)
        self.assertFalse(ok)
        self.assertIn("non-retryable", msg)
        self.assertEqual(_head(self.mirror), self.baseline)

    def test_non_ff_in_mirror_mode_rebases_against_mirror(self):
        theirs = _advance(self.base, self.mirror, "theirs")
        ours = self.new_commit("ours")
        ok, msg = self.push(mirror_url=str(self.mirror))
        self.assertTrue(ok, msg)
        self.assertIn("replay to GitHub pending", msg)
        new_head = _head(self.mirror)
        self.assertNotEqual(new_head, ours)  # rebased, not the original sha
        log = _run(["git", "log", "--format=%s", "-n", "3", new_head], self.repo).stdout
        self.assertIn("c", log)
        self.assertTrue((self.repo / "theirs").exists())  # picked up the mirror's commit
        self.assertTrue(
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", theirs, new_head], cwd=self.repo
            ).returncode == 0
        )


class NeverFailOverTest(_Base):
    def test_origin_non_ff_retries_origin_only(self):
        theirs = _advance(self.base, self.origin, "theirs")
        self.new_commit("ours")
        ok, msg = self.push(mirror_url=str(self.mirror))
        self.assertTrue(ok, msg)
        # origin got theirs + ours; mirror followed origin (never ahead of it).
        self.assertEqual(_head(self.mirror), _head(self.origin))
        self.assertTrue(
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", theirs, _head(self.origin)], cwd=self.repo
            ).returncode == 0
        )
        self.assertNotIn("Forgejo only", msg)

    def test_origin_rejecting_everything_never_pushes_mirror_alone(self):
        _hook(self.origin, "echo 'remote: protected branch hook declined' >&2; exit 1")
        self.new_commit()
        ok, msg = self.push(mirror_url=str(self.mirror))
        self.assertFalse(ok)
        self.assertEqual(_head(self.mirror), self.baseline)
        self.assertNotIn("Forgejo only", msg)

    def test_auth_failure_no_mirror_fallback(self):
        self.new_commit()
        with self._fake_origin_push("fatal: Authentication failed for 'https://github.com/x/y.git/'"):
            ok, msg = self.push(mirror_url=str(self.mirror))
        self.assertFalse(ok)
        self.assertIn("non-retryable", msg)
        self.assertEqual(_head(self.mirror), self.baseline)

    def test_persistent_publickey_rejection_no_mirror_fallback(self):
        self.new_commit()
        with self._fake_origin_push(
            "git@github.com: Permission denied (publickey).\n"
            "fatal: Could not read from remote repository."
        ), unittest.mock.patch.object(_git_utils, "_origin_reachable", return_value=False):
            ok, msg = self.push(mirror_url=str(self.mirror))
        self.assertFalse(ok)
        self.assertIn("non-retryable", msg)
        self.assertEqual(_head(self.mirror), self.baseline)

    def test_transient_publickey_rejection_with_authenticated_probe_retries_origin(self):
        sha = self.new_commit()
        real = _git_utils._git
        pushes = []

        def fail_once(*args, cwd=None):
            if args[:2] == ("push", "origin"):
                pushes.append("origin")
                if len(pushes) == 1:
                    return subprocess.CompletedProcess(
                        args, 128, "", "git@github.com: Permission denied (publickey)."
                    )
            return real(*args, cwd=cwd) if cwd is not None else real(*args)

        with unittest.mock.patch.object(_git_utils, "_git", side_effect=fail_once), \
             unittest.mock.patch.object(_git_utils, "_origin_reachable", return_value=True):
            ok, msg = self.push(mirror_url=str(self.mirror))
        self.assertTrue(ok, msg)
        self.assertEqual(pushes, ["origin", "origin"])
        self.assertEqual(_head(self.origin), sha)
        self.assertEqual(_head(self.mirror), sha)
        self.assertNotIn("replay to GitHub pending", msg)

    def test_repeated_publickey_rejection_after_recovered_probe_fails_closed(self):
        self.new_commit()
        pushes = []
        real = _git_utils._git

        def always_reject(*args, cwd=None):
            if args[:2] == ("push", "origin"):
                pushes.append("origin")
                return subprocess.CompletedProcess(
                    args, 128, "", "git@github.com: Permission denied (publickey)."
                )
            return real(*args, cwd=cwd) if cwd is not None else real(*args)

        with unittest.mock.patch.object(_git_utils, "_git", side_effect=always_reject), \
             unittest.mock.patch.object(_git_utils, "_origin_reachable", return_value=True):
            ok, msg = self.push(mirror_url=str(self.mirror))
        self.assertFalse(ok)
        self.assertEqual(pushes, ["origin", "origin"])
        self.assertEqual(_head(self.mirror), self.baseline)
        self.assertIn("non-retryable", msg)

    def test_compound_publickey_and_policy_refusal_never_retries_or_fails_over(self):
        self.new_commit()
        stderr = (
            "git@github.com: Permission denied (publickey).\n"
            "remote: error: GH006: Protected branch update failed."
        )
        with self._fake_origin_push(stderr), \
             unittest.mock.patch.object(_git_utils, "_origin_reachable") as probe:
            ok, msg = self.push(mirror_url=str(self.mirror))
        self.assertFalse(ok)
        probe.assert_not_called()
        self.assertEqual(_head(self.mirror), self.baseline)
        self.assertIn("non-retryable", msg)

    def test_compound_publickey_and_gh013_never_retries_or_fails_over(self):
        self.new_commit()
        stderr = (
            "git@github.com: Permission denied (publickey).\n"
            "remote: error: GH013: Repository rule violations found for refs/heads/main."
        )
        with self._fake_origin_push(stderr), \
             unittest.mock.patch.object(_git_utils, "_origin_reachable") as probe:
            ok, msg = self.push(mirror_url=str(self.mirror))
        self.assertFalse(ok)
        probe.assert_not_called()
        self.assertEqual(_head(self.mirror), self.baseline)
        self.assertIn("non-retryable", msg)

    def test_compound_publickey_and_generic_auth_or_permission_never_retries(self):
        for index, extra in enumerate((
            "fatal: Authentication failed for 'https://github.com/x/y.git/'",
            "remote: Permission to x/y.git denied to user.",
        )):
            with self.subTest(extra=extra):
                self.new_commit(f"auth-{index}")
                stderr = "git@github.com: Permission denied (publickey).\n" + extra
                with self._fake_origin_push(stderr), \
                     unittest.mock.patch.object(_git_utils, "_origin_reachable") as probe:
                    ok, msg = self.push(mirror_url=str(self.mirror))
                self.assertFalse(ok)
                probe.assert_not_called()
                self.assertEqual(_head(self.mirror), self.baseline)
                self.assertIn("non-retryable", msg)

    def test_arbitrary_publickey_mention_never_retries_or_fails_over(self):
        self.new_commit()
        stderr = "fatal: custom publickey helper returned an unknown state"
        with self._fake_origin_push(stderr), \
             unittest.mock.patch.object(_git_utils, "_origin_reachable") as probe:
            ok, msg = self.push(mirror_url=str(self.mirror))
        self.assertFalse(ok)
        probe.assert_not_called()
        self.assertEqual(_head(self.mirror), self.baseline)
        self.assertIn("non-retryable", msg)

    def test_network_marker_but_origin_still_reachable_no_fallback(self):
        # Classifier says "network" but the confirming ls-remote succeeds -> fail closed.
        self.new_commit()
        with self._fake_origin_push("fatal: unable to access 'x': Could not resolve host: github.com"):
            ok, msg = self.push(mirror_url=str(self.mirror))
        self.assertFalse(ok)
        self.assertEqual(_head(self.mirror), self.baseline)

    def test_ambiguous_stderr_hard_fails(self):
        self.new_commit()
        with self._fake_origin_push("fatal: something odd happened"):
            ok, msg = self.push(mirror_url=str(self.mirror))
        self.assertFalse(ok)
        self.assertEqual(_head(self.mirror), self.baseline)


class ServerErrorFailoverTest(_Base):
    def test_origin_5xx_and_mirror_failing_terminates_false(self):
        # Failover happens at most once: a persistently failing mirror must not loop.
        self.new_commit()
        real = _git_utils._git
        pushes = []

        def fake(*args, cwd=None):
            if args and args[0] == "push":
                pushes.append(args[1])
                if len(pushes) > 10:
                    raise AssertionError("push loop: failover re-entered")
                return subprocess.CompletedProcess(args, 128, "", "error: RPC failed; HTTP 500 curl 22")
            return real(*args, cwd=cwd) if cwd is not None else real(*args)

        with unittest.mock.patch.object(_git_utils, "_git", side_effect=fake):
            ok, msg = self.push(mirror_url=str(self.mirror))
        self.assertFalse(ok)
        self.assertEqual(pushes, ["origin", str(self.mirror)])
        self.assertEqual(_head(self.mirror), self.baseline)

    """ORG-101 §2 (amended): GitHub reachable but answering 'I failed' -> fail over."""

    def test_server_fault_fails_over_without_ls_remote_failure(self):
        for stderr in (
            "remote: fatal error in commit_refs\nerror: failed to push some refs to 'github.com:x/y.git'",
            "error: RPC failed; HTTP 500 curl 22 The requested URL returned error: 500\n"
            "send-pack: unexpected disconnect while reading sideband packet",
        ):
            with self.subTest(stderr=stderr[:40]):
                sha = self.new_commit(f"f{abs(hash(stderr)) % 1000}")
                with self._fake_origin_push(stderr):
                    ok, msg = self.push(mirror_url=str(self.mirror))
                self.assertTrue(ok, msg)
                self.assertEqual(_head(self.mirror), sha)
                self.assertIn("replay to GitHub pending", msg)

    # Server-ANSWERED rejection (real pre-receive hook decline): must NOT fail over.
    def test_pre_receive_declined_never_fails_over(self):
        _hook(self.origin, "echo 'remote: error: GH006: Protected branch update failed' >&2; exit 1")
        self.new_commit()
        ok, msg = self.push(mirror_url=str(self.mirror))
        self.assertFalse(ok)
        self.assertIn("declined", msg)
        self.assertEqual(_head(self.mirror), self.baseline)


class ResolveWriteMirrorTest(_Base):
    """origin (incl. ssh host-alias form) -> manifest primary -> writable mirror."""

    MIRROR = "https://pantheon-fleet@git.your-self-hosted-git.example/pantheon/pantheon-config.git"

    def _manifest(self, *, writable=True, verified=True):
        from tools import remote_resolve as rr

        return rr.Manifest(
            repos={"pantheon-config": rr.RepoManifestEntry("pantheon-config", (
                rr.RemoteEntry("primary", "git@github.com:example-org/pantheon-config.git", "d"),
                rr.RemoteEntry("mirror", self.MIRROR, "t", writable=writable),
            ))},
            auth_refs={"d": rr.AuthRef("d", ("github.com",)), "t": rr.AuthRef("t", ("git.your-self-hosted-git.example",))},
            signature_verified=verified,
        )

    def resolve(self, origin_url, **kw):
        _run(["git", "remote", "set-url", "origin", origin_url], self.repo)
        m = self._manifest(**kw)
        return _git_utils._resolve_write_mirror(self.repo, manifest_loader=lambda: m)

    def test_alias_form_origin_resolves(self):
        self.assertEqual(
            self.resolve("git@github.com:example-org/pantheon-config.git"), self.MIRROR
        )

    def test_canonical_origin_resolves(self):
        self.assertEqual(self.resolve("git@github.com:example-org/pantheon-config.git"), self.MIRROR)

    def test_unmatchable_origin_noops(self):
        self.assertIsNone(self.resolve("git@github-other:example-org/other.git"))
        self.assertIsNone(self.resolve(str(self.origin)))  # local path

    def test_not_writable_or_unverified_noops(self):
        url = "git@github.com:example-org/pantheon-config.git"
        self.assertIsNone(self.resolve(url, writable=False))
        self.assertIsNone(self.resolve(url, verified=False))

    def test_loader_failure_noops(self):
        def boom():
            raise RuntimeError("no pin")

        self.assertIsNone(_git_utils._resolve_write_mirror(self.repo, manifest_loader=boom))

    def test_pushinsteadof_rewrite_noops(self):
        # `ls-remote --get-url` ignores pushInsteadOf, but the push honors it: the
        # token-bearing push would go to the redirected host, past host binding.
        _run(["git", "config", "url.https://evil.example/.pushInsteadOf", self.MIRROR], self.repo)
        self.assertIsNone(self.resolve("git@github.com:example-org/pantheon-config.git"))

    def test_insteadof_rewrite_noops(self):
        _run(["git", "config", f"url.https://evil.example/.insteadOf", "https://pantheon-fleet@git.your-self-hosted-git.example/"], self.repo)
        self.assertIsNone(self.resolve("git@github.com:example-org/pantheon-config.git"))


class ClassifierTableTest(unittest.TestCase):
    CASES = {
        "real_answer": [
            "! [rejected]        HEAD -> main (non-fast-forward)\nerror: failed to push some refs",
            "! [rejected]        HEAD -> main (fetch first)",
            "remote: error: GH006: Protected branch update failed for refs/heads/main.",
            "! [remote rejected] HEAD -> main (pre-receive hook declined)",
            "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.",
            "fatal: Authentication failed for 'https://github.com/x/y.git/'",
            "remote: Permission to x/y.git denied to user.\nfatal: unable to access 'x': The requested URL returned error: 403",
            "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
            "remote: error: GH013: Repository rule violations found for refs/heads/main.",
            # a hook-declined line must win even if a 5xx-looking token rides along
            "remote: fatal error in hook\n! [remote rejected] HEAD -> main (pre-receive hook declined)",
        ],
        "server_error": [
            "remote: fatal error in commit_refs\nerror: failed to push some refs to 'github.com:x/y.git'",
            "error: RPC failed; HTTP 500 curl 22 The requested URL returned error: 500",
            "fatal: unable to access 'https://github.com/x/y.git/': The requested URL returned error: 502",
            "remote: Internal Server Error",
            "error: RPC failed; HTTP 503 curl 22 The requested URL returned error: 503\nfatal: the remote end hung up unexpectedly",
            "remote: error: internal failure\nerror: failed to push some refs to 'github.com:x/y.git'",
        ],
        "network": [
            "fatal: unable to access 'https://github.com/x/y.git/': Could not resolve host: github.com",
            "fatal: unable to access 'https://github.com/x/y.git/': Failed to connect to github.com port 443: Connection refused",
            "ssh: connect to host github.com port 22: Network is unreachable\nfatal: Could not read from remote repository.",
            "fatal: unable to access 'x': Connection timed out after 10001 milliseconds",
            "ssh: Could not resolve hostname github.com: Temporary failure in name resolution",
        ],
        "other": [
            "",
            "fatal: something odd happened",
            "fatal: the remote end hung up unexpectedly",  # bare hang-up: ambiguous
            "error: failed to push some refs to 'x'",       # no remote error line
            "fatal: '/x' does not appear to be a git repository",
            # policy refusal carrying a remote error line: not a server fault
            "remote: error: refusing to update checked out branch: refs/heads/main\nerror: failed to push some refs to 'x'",
            "remote: error: denied by custom policy\nerror: failed to push some refs to 'x'",
        ],
    }

    def test_table(self):
        for want, samples in self.CASES.items():
            for s in samples:
                with self.subTest(want=want, sample=s[:50]):
                    self.assertEqual(_classify_push_failure(s), want)


class RealHooksTest(_Base):
    """The real .githooks/pre-push on the mirror leg (pushed by URL)."""

    def setUp(self):
        super().setUp()
        _run(["git", "checkout", "-q", "main"], self.repo)
        _run(["git", "config", "core.hooksPath", str(REAL_HOOKS)], self.repo)

    def push_main(self, **kw):
        return push_with_retry(self.repo, refspec="main", **kw)

    def test_hook_does_not_rebase_onto_mirror(self):
        _advance(self.base, self.mirror, "mirror-ahead")
        sha = self.new_commit("ours")
        ok, msg = self.push_main(mirror_url=str(self.mirror))
        self.assertTrue(ok, msg)
        self.assertEqual(_head(self.origin), sha)
        # Local branch untouched: the freshness auto-rebase never ran against the mirror.
        self.assertEqual(_run(["git", "rev-parse", "HEAD"], self.repo).stdout.strip(), sha)
        self.assertIn("mirror push failed", msg)  # non-ff on the mirror: best-effort, healed later

    def test_real_hook_mirror_push_succeeds(self):
        sha = self.new_commit("ours")
        ok, msg = self.push_main(mirror_url=str(self.mirror))
        self.assertTrue(ok, msg)
        self.assertEqual(_head(self.mirror), sha)

    def test_hermes_trigger_commit_cannot_exist_to_reach_either_leg(self):
        # The byte-sequence guard lives at commit time (commit-msg / pre-commit), not in
        # pre-push, so it gates BOTH legs structurally: a trigger-bearing commit is
        # refused before there is anything to push to origin or the mirror.
        trigger = "HERME" + "S.md"
        (self.repo / "x.txt").write_text("x", encoding="utf-8")
        _run(["git", "add", "x.txt"], self.repo)
        r = subprocess.run(
            ["git", "commit", "-q", "-m", f"mention {trigger}"],
            cwd=self.repo, capture_output=True, text=True, env=_ENV,
        )
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("HERMES discipline violation", r.stderr)
        ok, msg = self.push_main(mirror_url=str(self.mirror))  # nothing new to push
        self.assertTrue(ok, msg)
        self.assertEqual(_head(self.mirror), self.baseline)


class MirrorPushAfterOriginTest(_Base):
    """T-4691: the dual-write leg alone, for push sites with their own retry/rebase
    semantics (pantheon_config_sync, delivery_rebase_controller). Never fails over."""

    def test_mirror_pushed_after_caller_pushed_origin(self):
        sha = self.new_commit()
        _run(["git", "push", "-q", "origin", "HEAD:main"], self.repo)
        note = _git_utils.mirror_push_after_origin(self.repo, "HEAD:main", mirror_url=str(self.mirror))
        self.assertEqual(note, "mirror pushed")
        self.assertEqual(_head(self.mirror), sha)

    def test_mirror_failure_is_a_note_not_an_error(self):
        _hook(self.mirror, "echo 'remote: nope' >&2; exit 1")
        self.new_commit()
        note = _git_utils.mirror_push_after_origin(self.repo, "HEAD:main", mirror_url=str(self.mirror))
        self.assertIn("mirror push failed", note)
        self.assertIn("reconciler will heal", note)
        self.assertEqual(_head(self.mirror), self.baseline)

    def test_no_writable_mirror_is_inert(self):
        _hook(self.mirror, f"touch '{self.base}/mirror-was-pushed'; exit 0")
        self.new_commit()
        self.assertIsNone(_git_utils.mirror_push_after_origin(self.repo, "HEAD:main", mirror_url=None))
        self.assertFalse((self.base / "mirror-was-pushed").exists())

    def test_default_resolves_from_the_manifest(self):
        with unittest.mock.patch.object(_git_utils, "_resolve_write_mirror", return_value=None) as r:
            self.assertIsNone(_git_utils.mirror_push_after_origin(self.repo, "HEAD:main"))
        r.assert_called_once_with(self.repo)


if __name__ == "__main__":
    unittest.main()
