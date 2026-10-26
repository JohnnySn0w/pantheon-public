"""test_read_failover.py — tests for tools/read_failover.py + its _git_utils wiring (T-1998).

Hermetic: real git runs only against tmp repos (a local clone plus a bare
"mirror"); the resolver and manifest loader are injected fakes, so no network
and no real manifest. PANTHEON_STATE_DIR points at a tmp dir, so the real pin
store is never read or written.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

import tools._git_utils as gu
import tools.read_failover as rf
from tools.remote_resolve import (
    Manifest,
    ManifestIntegrityError,
    RemoteEntry,
    RepoManifestEntry,
    ResolvedRemote,
)

PRIMARY_URL = "git@github.com:example-org/pantheon-config.git"


def _git(cwd: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name + "\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


class _Env:
    """Armed/unarmed env: PANTHEON_STATE_DIR -> tmp, optional switch + pin."""

    def __init__(self, state: Path, *, switch: bool, pin: bool):
        self.state = state
        self.switch = switch
        self.pin = pin
        self._patch = None

    def __enter__(self):
        env = {"PANTHEON_STATE_DIR": str(self.state)}
        if self.switch:
            env[rf.ENV_SWITCH] = "1"
        self._patch = patch.dict(os.environ, env, clear=False)
        self._patch.start()
        if not self.switch:
            os.environ.pop(rf.ENV_SWITCH, None)
        if self.pin:
            self.state.mkdir(parents=True, exist_ok=True)
            (self.state / "repos.yaml.pin").write_text("0" * 64 + "\n", encoding="utf-8")
        return self

    def __exit__(self, *exc):
        self._patch.stop()


class _Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.state = base / "state"
        # A work repo with history A-B; the "mirror" is a bare repo we point main at.
        self.src = base / "src"
        self.src.mkdir()
        _git(self.src, "init", "-q", "-b", "main")
        _git(self.src, "config", "user.email", "t@example")
        _git(self.src, "config", "user.name", "T")
        self.a = _commit(self.src, "a")
        self.b = _commit(self.src, "b")
        self.mirror = base / "mirror.git"
        _git(base, "init", "-q", "--bare", str(self.mirror))
        # The seat clone whose refs/remotes/origin/main we manage.
        self.repo = base / "repo"
        _git(base, "clone", "-q", str(self.src), str(self.repo))
        self.resolver_calls = 0

    def tearDown(self):
        self._tmp.cleanup()

    def _mirror_main(self, sha: str) -> None:
        _git(self.src, "push", "-q", "--force", str(self.mirror), f"{sha}:refs/heads/main")

    def _set_tracking(self, sha: str | None) -> None:
        if sha is None:
            _git(self.repo, "update-ref", "-d", rf.TRACKING_REF)
        else:
            _git(self.repo, "update-ref", rf.TRACKING_REF, sha)

    def _tracking(self) -> str | None:
        res = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", rf.TRACKING_REF],
            cwd=self.repo, capture_output=True, text=True,
        )
        return res.stdout.strip() or None

    def _resolver(self, *, healed: bool = True, url: str | None = None, raises: Exception | None = None):
        def fake(repo_name, *, manifest, git_dir, git_runner):
            self.resolver_calls += 1
            self.assertEqual(repo_name, "pantheon-config")
            if raises is not None:
                raise raises
            return ResolvedRemote(
                url=url or str(self.mirror),
                auth_ref="token:forgejo-pantheon-config",
                host_binding="git.your-self-hosted-git.example",
                is_healed=healed,
                healed_from="git@github.com:x/y.git" if healed else None,
            )
        return fake

    def _manifest(self, *, verified: bool = True, mirror_url: str | None = None) -> Manifest:
        remotes = (
            RemoteEntry(role="primary", url=PRIMARY_URL, auth_ref="deploy-key:pantheon-config"),
            RemoteEntry(role="mirror", url=mirror_url or str(self.mirror), auth_ref="token:forgejo-pantheon-config"),
        )
        return Manifest(
            repos={"pantheon-config": RepoManifestEntry(name="pantheon-config", remotes=remotes)},
            auth_refs={},
            signature_verified=verified,
        )

    def _run(self, **kw):
        manifest = kw.pop("manifest", None) or self._manifest()
        kw.setdefault("manifest_loader", lambda **_: manifest)
        return rf.fetch_main_via_mirror(self.repo, **kw)


class TestArming(_Fixture):
    def test_unarmed_without_switch_never_resolves(self):
        with _Env(self.state, switch=False, pin=True):
            res = self._run(resolver=self._resolver())
        self.assertFalse(res.ok)
        self.assertIn("not armed", res.detail)
        self.assertEqual(self.resolver_calls, 0)

    def test_unarmed_without_pin_never_resolves(self):
        with _Env(self.state, switch=True, pin=False):
            res = self._run(resolver=self._resolver())
        self.assertFalse(res.ok)
        self.assertEqual(self.resolver_calls, 0)

    def test_refresh_does_not_bootstrap_a_missing_pin(self):
        # Bootstrap is operator-only (T-3723 §8.1); refresh must never create the first pin.
        with _Env(self.state, switch=True, pin=False):
            self.assertIsNone(rf.refresh_pin_if_armed(self.repo))
        self.assertFalse((self.state / "repos.yaml.pin").exists())

    def test_refresh_pins_origin_main_blob_when_armed(self):
        (self.src / "repos.yaml").write_text("repos: []\n", encoding="utf-8")
        _git(self.src, "add", "repos.yaml")
        _git(self.src, "commit", "-q", "-m", "manifest")
        _git(self.repo, "fetch", "-q", "origin")
        with _Env(self.state, switch=True, pin=True):
            pin = rf.refresh_pin_if_armed(self.repo)
        self.assertEqual(pin, hashlib.sha256(b"repos: []\n").hexdigest())
        self.assertEqual((self.state / "repos.yaml.pin").read_text().strip(), pin)


class TestFailover(_Fixture):
    def test_mirror_ahead_fast_forwards_tracking_ref(self):
        c = _commit(self.src, "c")
        self._mirror_main(c)
        self._set_tracking(self.b)
        with _Env(self.state, switch=True, pin=True):
            res = self._run(resolver=self._resolver())
        self.assertTrue(res.ok, res.detail)
        self.assertEqual(self._tracking(), c)
        self.assertIn("advanced", res.detail)

    def test_mirror_behind_is_a_noop(self):
        self._mirror_main(self.a)
        self._set_tracking(self.b)
        with _Env(self.state, switch=True, pin=True):
            res = self._run(resolver=self._resolver())
        self.assertTrue(res.ok, res.detail)
        self.assertEqual(self._tracking(), self.b)  # never rewound
        self.assertIn("behind", res.detail)

    def test_mirror_equal_is_a_noop(self):
        self._mirror_main(self.b)
        self._set_tracking(self.b)
        with _Env(self.state, switch=True, pin=True):
            res = self._run(resolver=self._resolver())
        self.assertTrue(res.ok, res.detail)
        self.assertEqual(self._tracking(), self.b)

    def test_diverged_mirror_is_refused(self):
        # Fork off A: a real pull-mirror can't do this, so it reads as tampering.
        _git(self.src, "checkout", "-q", "-b", "fork", self.a)
        forked = _commit(self.src, "evil")
        self._mirror_main(forked)
        self._set_tracking(self.b)
        with _Env(self.state, switch=True, pin=True):
            res = self._run(resolver=self._resolver())
        self.assertFalse(res.ok)
        self.assertIn("DIVERGES", res.detail)
        self.assertEqual(self._tracking(), self.b)

    def test_missing_tracking_ref_is_populated(self):
        self._mirror_main(self.b)
        self._set_tracking(None)
        with _Env(self.state, switch=True, pin=True):
            res = self._run(resolver=self._resolver())
        self.assertTrue(res.ok, res.detail)
        self.assertEqual(self._tracking(), self.b)

    def test_concurrent_origin_update_is_not_clobbered(self):
        # A successful origin fetch lands between our read of origin/main and our
        # update-ref: the compare-and-swap must refuse rather than overwrite it.
        c = _commit(self.src, "c")
        d = _commit(self.src, "d")
        self._mirror_main(c)
        self._set_tracking(self.b)
        real = rf._default_git_runner

        def racing_runner(argv, cwd, timeout):
            if argv[:2] == ["git", "update-ref"]:
                _git(self.repo, "fetch", "-q", "origin")  # brings d
                _git(self.repo, "update-ref", rf.TRACKING_REF, d)
            return real(argv, cwd, timeout)

        with _Env(self.state, switch=True, pin=True):
            res = self._run(resolver=self._resolver(), git_runner=racing_runner)
        self.assertFalse(res.ok)
        self.assertIn("update refused", res.detail)
        self.assertEqual(self._tracking(), d)

    def test_resolver_refusal_means_no_failover(self):
        self._set_tracking(self.b)
        with _Env(self.state, switch=True, pin=True):
            res = self._run(resolver=self._resolver(raises=ManifestIntegrityError("pin mismatch")))
        self.assertFalse(res.ok)
        self.assertIn("resolver refused", res.detail)
        self.assertEqual(self._tracking(), self.b)

    def test_unverified_manifest_means_no_failover(self):
        self._set_tracking(self.b)
        with _Env(self.state, switch=True, pin=True):
            res = self._run(resolver=self._resolver(), manifest=self._manifest(verified=False))
        self.assertFalse(res.ok)
        self.assertIn("does not verify", res.detail)
        self.assertEqual(self.resolver_calls, 0)

    def test_insteadof_rewrite_of_mirror_url_is_refused(self):
        # codex T-1998 High: host binding checks the manifest string, but git would
        # rewrite it and hand the credential to another host.
        mirror_url = "https://pantheon-fleet@git.your-self-hosted-git.example/github-mirror/pantheon-config.git"
        _git(self.repo, "config", "url.https://evil.example/.insteadOf", "https://pantheon-fleet@git.your-self-hosted-git.example/")
        c = _commit(self.src, "c")
        self._mirror_main(c)
        self._set_tracking(self.b)
        with _Env(self.state, switch=True, pin=True):
            res = self._run(resolver=self._resolver(), manifest=self._manifest(mirror_url=mirror_url))
        self.assertFalse(res.ok)
        self.assertIn("rewrites manifest url", res.detail)
        self.assertIn("evil.example", res.detail)
        self.assertEqual(self.resolver_calls, 0)  # no credential-bearing probe ran
        self.assertEqual(self._tracking(), self.b)

    def test_insteadof_rewrite_of_primary_url_is_refused(self):
        _git(self.repo, "config", "url.ssh://evil.example/.insteadOf", "git@github.com:")
        self._set_tracking(self.b)
        with _Env(self.state, switch=True, pin=True):
            res = self._run(resolver=self._resolver())
        self.assertFalse(res.ok)
        self.assertIn("rewrites manifest url", res.detail)
        self.assertEqual(self.resolver_calls, 0)

    def test_manifest_load_failure_means_no_failover(self):
        def boom(**_):
            raise KeyError("role")
        with _Env(self.state, switch=True, pin=True):
            res = self._run(resolver=self._resolver(), manifest_loader=boom)
        self.assertFalse(res.ok)
        self.assertEqual(self.resolver_calls, 0)

    def test_reachable_primary_means_no_failover(self):
        c = _commit(self.src, "c")
        self._mirror_main(c)
        self._set_tracking(self.b)
        with _Env(self.state, switch=True, pin=True):
            res = self._run(resolver=self._resolver(healed=False))
        self.assertFalse(res.ok)
        self.assertEqual(self._tracking(), self.b)

    def test_unreachable_mirror_fails_cleanly(self):
        self._set_tracking(self.b)
        with _Env(self.state, switch=True, pin=True):
            res = self._run(resolver=self._resolver(url=str(self.repo.parent / "nope.git")))
        self.assertFalse(res.ok)
        self.assertIn("mirror fetch failed", res.detail)
        self.assertEqual(self._tracking(), self.b)


class TestPullRepoOrWarnWiring(unittest.TestCase):
    """The _git_utils hook: unarmed is byte-for-byte the old path; armed routes
    a failed fetch through the failover and a good fetch through the pin refresh."""

    def _run_pull(self, fetch, *, env_on: bool, failover=None):
        fake_mod = MagicMock()
        if failover is not None:
            fake_mod.fetch_main_via_mirror.side_effect = failover
        env = {gu._READ_FAILOVER_ENV: "1"} if env_on else {}
        buf = io.StringIO()
        with patch.dict(os.environ, env, clear=False), \
                patch("tools._git_utils.subprocess") as mock_sub, \
                patch("tools._git_utils._read_failover_module", return_value=fake_mod) as mod_getter, \
                redirect_stderr(buf):
            if not env_on:
                os.environ.pop(gu._READ_FAILOVER_ENV, None)
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            mock_sub.run.side_effect = fetch
            gu.pull_repo_or_warn("t")
        return mock_sub, mod_getter, fake_mod, buf.getvalue()

    @staticmethod
    def _fetch_rc(rc):
        def run(argv, **_):
            return MagicMock(returncode=rc if argv[1] == "fetch" else 0, stdout="", stderr="fatal: unreachable\n")
        return run

    def test_unarmed_failed_fetch_never_touches_failover(self):
        sub, getter, _, out = self._run_pull(self._fetch_rc(128), env_on=False)
        getter.assert_not_called()
        self.assertEqual(sub.run.call_count, 1)  # no merge
        self.assertIn("using stale clone", out)

    def test_unarmed_good_fetch_never_refreshes_pin(self):
        sub, getter, _, _ = self._run_pull(self._fetch_rc(0), env_on=False)
        getter.assert_not_called()
        self.assertEqual(sub.run.call_count, 2)

    def test_armed_failed_fetch_with_good_failover_merges(self):
        sub, _, mod, out = self._run_pull(
            self._fetch_rc(128), env_on=True,
            failover=lambda root: rf.FailoverResult(True, "advanced origin/main"),
        )
        mod.fetch_main_via_mirror.assert_called_once_with(gu.REPO_ROOT)
        self.assertEqual(sub.run.call_args_list[1][0][0], ["git", "merge", "--ff-only", "refs/remotes/origin/main"])
        self.assertIn("trying read-failover", out)
        self.assertIn("read-failover: advanced", out)
        self.assertNotIn("using stale clone", out)  # the journal must not claim a stale clone it didn't use

    def test_armed_failed_fetch_with_refused_failover_does_not_merge(self):
        sub, _, _, out = self._run_pull(
            self._fetch_rc(128), env_on=True,
            failover=lambda root: rf.FailoverResult(False, "resolver refused"),
        )
        self.assertEqual(sub.run.call_count, 1)
        self.assertIn("read-failover: resolver refused; using stale clone", out)

    def test_armed_failover_exception_is_fail_open(self):
        def boom(root):
            raise RuntimeError("kaboom")
        sub, _, _, out = self._run_pull(self._fetch_rc(128), env_on=True, failover=boom)
        self.assertEqual(sub.run.call_count, 1)
        self.assertIn("read-failover error", out)

    def test_armed_fetch_timeout_still_tries_failover(self):
        def run(argv, **_):
            if argv[1] == "fetch":
                raise subprocess.TimeoutExpired(cmd="git", timeout=30)
            return MagicMock(returncode=0, stdout="", stderr="")
        sub, _, mod, _ = self._run_pull(
            run, env_on=True, failover=lambda root: rf.FailoverResult(True, "ok"),
        )
        mod.fetch_main_via_mirror.assert_called_once()
        self.assertEqual(sub.run.call_count, 2)

    def test_armed_good_fetch_refreshes_pin(self):
        _, _, mod, _ = self._run_pull(self._fetch_rc(0), env_on=True)
        mod.refresh_pin_if_armed.assert_called_once_with(gu.REPO_ROOT)
        mod.fetch_main_via_mirror.assert_not_called()

    def test_armed_pin_refresh_failure_is_fail_open(self):
        fake_err = ManifestIntegrityError("show failed")
        env = {gu._READ_FAILOVER_ENV: "1"}
        fake_mod = MagicMock()
        fake_mod.refresh_pin_if_armed.side_effect = fake_err
        buf = io.StringIO()
        with patch.dict(os.environ, env, clear=False), \
                patch("tools._git_utils.subprocess") as mock_sub, \
                patch("tools._git_utils._read_failover_module", return_value=fake_mod), \
                redirect_stderr(buf):
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            gu.pull_repo_or_warn("t")
        self.assertEqual(mock_sub.run.call_count, 2)  # merge still ran
        self.assertIn("manifest pin refresh failed", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
