"""test_remote_resolve.py — unit tests for tools/remote_resolve.py (T-1996).

All tests inject a fake git_runner — NO network calls, NO real git
operations. The fake git_runner signature matches tools/provision_repos.py's
convention: (argv, cwd, timeout) -> CompletedProcess.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import tools.remote_resolve as rr


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _cp(argv, rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=argv, returncode=rc, stdout=stdout, stderr=stderr)


class _ScriptedRunner:
    """Fake git_runner: routes calls to canned responses keyed by argv
    prefix, and records every call for ordering/count assertions."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self._routes: list[tuple[list[str], subprocess.CompletedProcess]] = []
        self.default_rc = 0

    def route(self, argv_prefix: list[str], result: subprocess.CompletedProcess) -> None:
        self._routes.append((argv_prefix, result))

    def __call__(self, argv, cwd, timeout):
        self.calls.append(list(argv))
        for prefix, result in self._routes:
            if argv[: len(prefix)] == prefix:
                return result
        return _cp(argv, rc=self.default_rc)


def _manifest(
    *,
    repo_name: str = "pantheon-config",
    remotes: tuple[rr.RemoteEntry, ...],
    auth_refs: dict[str, rr.AuthRef],
    signature_verified: bool = True,
) -> rr.Manifest:
    entry = rr.RepoManifestEntry(name=repo_name, remotes=remotes)
    return rr.Manifest(
        repos={repo_name: entry},
        auth_refs=auth_refs,
        signature_verified=signature_verified,
    )


def _primary_mirror_manifest(signature_verified: bool = True) -> rr.Manifest:
    """Two-remote manifest: primary (github alias) + mirror (self-hosted),
    both host-bound correctly. Mirrors ORG-046 §1's worked example."""
    auth_refs = {
        "deploy-key:pantheon-config": rr.AuthRef(
            name="deploy-key:pantheon-config",
            allowed_hosts=("github.com",),
        ),
        "token:forgejo-pantheon-config": rr.AuthRef(
            name="token:forgejo-pantheon-config",
            allowed_hosts=("git.your-self-hosted-git.example",),
        ),
    }
    remotes = (
        rr.RemoteEntry(
            role="primary",
            url="git@github.com:example-org/pantheon-config.git",
            auth_ref="deploy-key:pantheon-config",
        ),
        rr.RemoteEntry(
            role="mirror",
            url="https://git.your-self-hosted-git.example/pantheon/pantheon-config.git",
            auth_ref="token:forgejo-pantheon-config",
        ),
    )
    return _manifest(remotes=remotes, auth_refs=auth_refs, signature_verified=signature_verified)


PRIMARY_URL = "git@github.com:example-org/pantheon-config.git"
MIRROR_URL = "https://git.your-self-hosted-git.example/pantheon/pantheon-config.git"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class HappyPathTest(unittest.TestCase):
    def test_primary_reachable_returns_primary_not_healed(self):
        manifest = _primary_mirror_manifest()
        runner = _ScriptedRunner()
        runner.route(["git", "ls-remote", PRIMARY_URL], _cp(["git", "ls-remote", PRIMARY_URL], rc=0))

        result = rr.resolve_remote("pantheon-config", manifest=manifest, git_runner=runner)

        self.assertEqual(result.url, PRIMARY_URL)
        self.assertFalse(result.is_healed)
        self.assertIsNone(result.healed_from)
        self.assertEqual(result.host_binding, "github.com")


# ---------------------------------------------------------------------------
# Self-heal: transient (read-only, never persisted)
# ---------------------------------------------------------------------------


class TransientSelfHealTest(unittest.TestCase):
    def test_primary_unreachable_falls_back_to_mirror_ephemeral(self):
        manifest = _primary_mirror_manifest()
        runner = _ScriptedRunner()
        runner.route(["git", "ls-remote", PRIMARY_URL], _cp(["git", "ls-remote", PRIMARY_URL], rc=1, stderr="fatal: could not read"))
        runner.route(["git", "ls-remote", MIRROR_URL], _cp(["git", "ls-remote", MIRROR_URL], rc=0))

        result = rr.resolve_remote("pantheon-config", manifest=manifest, git_runner=runner)

        self.assertEqual(result.url, MIRROR_URL)
        self.assertTrue(result.is_healed)
        self.assertEqual(result.healed_from, PRIMARY_URL)

        # Never persisted — no `git config` write call at all (a write argv
        # is ["git", "config", "<key>", "<value>"]; a read is
        # ["git", "config", "--get", "<key>"] — distinguish on "--get").
        config_write_calls = [c for c in runner.calls if c[:2] == ["git", "config"] and "--get" not in c]
        self.assertEqual(config_write_calls, [], f"unexpected config-write calls: {runner.calls}")

    def test_bounded_retry_does_not_try_a_third_candidate(self):
        auth_refs = {
            "a": rr.AuthRef(name="a", allowed_hosts=("host-a",)),
            "b": rr.AuthRef(name="b", allowed_hosts=("host-b",)),
            "c": rr.AuthRef(name="c", allowed_hosts=("host-c",)),
        }
        remotes = (
            rr.RemoteEntry(role="primary", url="git@host-a:org/repo.git", auth_ref="a"),
            rr.RemoteEntry(role="mirror", url="git@host-b:org/repo.git", auth_ref="b"),
            rr.RemoteEntry(role="mirror", url="git@host-c:org/repo.git", auth_ref="c"),
        )
        manifest = _manifest(remotes=remotes, auth_refs=auth_refs)
        runner = _ScriptedRunner()
        runner.default_rc = 1  # everything fails reachability

        with self.assertRaises(rr.NoReachableRemoteError):
            rr.resolve_remote("pantheon-config", manifest=manifest, git_runner=runner)

        ls_remote_calls = [c for c in runner.calls if c[:2] == ["git", "ls-remote"]]
        self.assertEqual(len(ls_remote_calls), 2, f"expected exactly 2 bounded attempts, got {ls_remote_calls}")
        third_host_tried = any("host-c" in " ".join(c) for c in ls_remote_calls)
        self.assertFalse(third_host_tried, "resolver tried a third candidate past the bounded retry")


# ---------------------------------------------------------------------------
# Self-heal: permanent identity change (may persist, managed-only)
# ---------------------------------------------------------------------------


class PermanentIdentityChangeTest(unittest.TestCase):
    def test_redirect_hint_matching_manifest_repoints_after_managed_check(self):
        manifest = _primary_mirror_manifest()
        runner = _ScriptedRunner()
        runner.route(
            ["git", "config", "--get", "remote.origin.pantheon-managed"],
            _cp(["git", "config", "--get", "remote.origin.pantheon-managed"], rc=0, stdout="true\n"),
        )
        runner.route(
            ["git", "config", "--get", "remote.origin.url"],
            _cp(["git", "config", "--get", "remote.origin.url"], rc=0, stdout=PRIMARY_URL + "\n"),
        )
        runner.route(
            ["git", "config", "remote.origin.url", MIRROR_URL],
            _cp(["git", "config", "remote.origin.url", MIRROR_URL], rc=0),
        )

        with tempfile.TemporaryDirectory() as tmp:
            git_dir = Path(tmp)
            result = rr.resolve_remote(
                "pantheon-config",
                manifest=manifest,
                git_dir=git_dir,
                git_runner=runner,
                redirect_hint=MIRROR_URL,
            )

        self.assertEqual(result.url, MIRROR_URL)
        self.assertTrue(result.is_healed)
        self.assertEqual(result.healed_from, PRIMARY_URL)

        # Ordering: the managed-marker check must precede the config write.
        managed_check_idx = next(
            i for i, c in enumerate(runner.calls) if c == ["git", "config", "--get", "remote.origin.pantheon-managed"]
        )
        write_idx = next(
            i for i, c in enumerate(runner.calls) if c == ["git", "config", "remote.origin.url", MIRROR_URL]
        )
        self.assertLess(managed_check_idx, write_idx, "config-write happened before the managed-marker check")

    def test_redirect_hint_not_in_manifest_is_discarded_not_trusted(self):
        """A redirect is never authority on its own — an unmatched hint must
        fall back to ordinary reachability resolution, not a bare trust."""
        manifest = _primary_mirror_manifest()
        runner = _ScriptedRunner()
        runner.route(["git", "ls-remote", PRIMARY_URL], _cp(["git", "ls-remote", PRIMARY_URL], rc=0))

        with tempfile.TemporaryDirectory() as tmp:
            result = rr.resolve_remote(
                "pantheon-config",
                manifest=manifest,
                git_dir=Path(tmp),
                git_runner=runner,
                redirect_hint="https://evil.example.com/example-org/pantheon-config.git",
            )

        self.assertEqual(result.url, PRIMARY_URL)
        self.assertFalse(result.is_healed)
        config_write_calls = [c for c in runner.calls if c[:2] == ["git", "config"] and "--get" not in c]
        self.assertEqual(config_write_calls, [])


# ---------------------------------------------------------------------------
# Security invariant: auth-to-host binding
# ---------------------------------------------------------------------------


class HostBindingTest(unittest.TestCase):
    def test_url_outside_allowed_hosts_raises_before_any_subprocess(self):
        auth_refs = {
            "deploy-key:pantheon-config": rr.AuthRef(
                name="deploy-key:pantheon-config",
                allowed_hosts=("only-this-host",),  # url below is a different host
            ),
        }
        remotes = (
            rr.RemoteEntry(
                role="primary",
                url=PRIMARY_URL,  # host is github.com, not only-this-host
                auth_ref="deploy-key:pantheon-config",
            ),
        )
        manifest = _manifest(remotes=remotes, auth_refs=auth_refs)
        runner = _ScriptedRunner()

        with self.assertRaises(rr.HostBindingViolation):
            rr.resolve_remote("pantheon-config", manifest=manifest, git_runner=runner)

        self.assertEqual(runner.calls, [], "no subprocess should be spawned on a binding violation")


# ---------------------------------------------------------------------------
# Security invariant: managed-remote marker gate
# ---------------------------------------------------------------------------


class ManagedRemoteGateTest(unittest.TestCase):
    def test_unmanaged_remote_refuses_repoint_even_on_valid_trigger(self):
        manifest = _primary_mirror_manifest()
        runner = _ScriptedRunner()
        runner.route(
            ["git", "config", "--get", "remote.origin.pantheon-managed"],
            _cp(["git", "config", "--get", "remote.origin.pantheon-managed"], rc=1),  # absent/unset
        )

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(rr.UnmanagedRemoteError):
                rr.resolve_remote(
                    "pantheon-config",
                    manifest=manifest,
                    git_dir=Path(tmp),
                    git_runner=runner,
                    redirect_hint=MIRROR_URL,
                )

        config_write_calls = [c for c in runner.calls if c[:2] == ["git", "config"] and "--get" not in c]
        self.assertEqual(config_write_calls, [], "must never clobber unmanaged local config")

    def test_managed_remote_predicate_direct(self):
        runner = _ScriptedRunner()
        runner.route(
            ["git", "config", "--get", "remote.origin.pantheon-managed"],
            _cp(["git", "config", "--get", "remote.origin.pantheon-managed"], rc=0, stdout="true\n"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(rr.managed_remote(Path(tmp), "origin", git_runner=runner))

        runner2 = _ScriptedRunner()
        runner2.default_rc = 1  # key absent
        with tempfile.TemporaryDirectory() as tmp2:
            self.assertFalse(rr.managed_remote(Path(tmp2), "origin", git_runner=runner2))


# ---------------------------------------------------------------------------
# Reachability probe shape
# ---------------------------------------------------------------------------


class ReachabilityProbeShapeTest(unittest.TestCase):
    def test_probe_uses_git_ls_remote_not_ping_or_dns(self):
        manifest = _primary_mirror_manifest()
        runner = _ScriptedRunner()
        runner.route(["git", "ls-remote", PRIMARY_URL], _cp(["git", "ls-remote", PRIMARY_URL], rc=0))

        rr.resolve_remote("pantheon-config", manifest=manifest, git_runner=runner)

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0], ["git", "ls-remote", PRIMARY_URL])


# ---------------------------------------------------------------------------
# Manifest integrity (Q2: signing-as-default)
# ---------------------------------------------------------------------------


class ManifestIntegrityTest(unittest.TestCase):
    def test_unsigned_manifest_object_is_refused(self):
        manifest = _primary_mirror_manifest(signature_verified=False)
        runner = _ScriptedRunner()

        with self.assertRaises(rr.ManifestIntegrityError):
            rr.resolve_remote("pantheon-config", manifest=manifest, git_runner=runner)

        self.assertEqual(runner.calls, [], "no subprocess should run against an unverified manifest")

    def test_load_manifest_records_failed_verification_and_resolver_refuses(self):
        yaml_text = """
auth_refs:
  deploy-key:pantheon-config:
    allowed_hosts: [github.com]
repos:
  - name: pantheon-config
    remotes:
      - role: primary
        url: git@github.com:example-org/pantheon-config.git
        auth: deploy-key:pantheon-config
"""
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "repos.yaml"
            manifest_path.write_text(yaml_text, encoding="utf-8")

            tampered_verifier = MagicMock(return_value=False)
            manifest = rr.load_manifest(manifest_path, signature_verifier=tampered_verifier)

            tampered_verifier.assert_called_once()
            self.assertFalse(manifest.signature_verified)

            with self.assertRaises(rr.ManifestIntegrityError):
                rr.resolve_remote("pantheon-config", manifest=manifest, git_runner=_ScriptedRunner())

    def test_load_manifest_valid_signature_resolves_normally(self):
        yaml_text = """
auth_refs:
  deploy-key:pantheon-config:
    allowed_hosts: [github.com]
repos:
  - name: pantheon-config
    remotes:
      - role: primary
        url: git@github.com:example-org/pantheon-config.git
        auth: deploy-key:pantheon-config
"""
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "repos.yaml"
            manifest_path.write_text(yaml_text, encoding="utf-8")

            good_verifier = MagicMock(return_value=True)
            manifest = rr.load_manifest(manifest_path, signature_verifier=good_verifier)
            self.assertTrue(manifest.signature_verified)

            runner = _ScriptedRunner()
            runner.route(["git", "ls-remote", PRIMARY_URL], _cp(["git", "ls-remote", PRIMARY_URL], rc=0))
            result = rr.resolve_remote("pantheon-config", manifest=manifest, git_runner=runner)
            self.assertEqual(result.url, PRIMARY_URL)


# ---------------------------------------------------------------------------
# Prefer override
# ---------------------------------------------------------------------------


class PreferOverrideTest(unittest.TestCase):
    def test_prefer_mirror_tries_mirror_first(self):
        manifest = _primary_mirror_manifest()
        runner = _ScriptedRunner()
        runner.route(["git", "ls-remote", MIRROR_URL], _cp(["git", "ls-remote", MIRROR_URL], rc=0))

        result = rr.resolve_remote("pantheon-config", manifest=manifest, git_runner=runner, prefer="mirror")

        self.assertEqual(result.url, MIRROR_URL)
        # Preferred-and-reached-first-try is not a "heal" — nothing failed.
        self.assertFalse(result.is_healed)
        self.assertEqual(runner.calls, [["git", "ls-remote", MIRROR_URL]])


# ---------------------------------------------------------------------------
# Repo-local lock (finding 8) — concurrent resolve+repoint serializes
# ---------------------------------------------------------------------------


class RepoLockConcurrencyTest(unittest.TestCase):
    def test_two_holders_never_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / ".git" / rr.LOCK_FILENAME
            barrier = threading.Barrier(2)
            events: list[tuple[str, int, float]] = []
            events_guard = threading.Lock()

            def worker(tid: int) -> None:
                barrier.wait()
                with rr._RepoLock(lock_path):
                    with events_guard:
                        events.append(("enter", tid, time.monotonic()))
                    time.sleep(0.05)
                    with events_guard:
                        events.append(("exit", tid, time.monotonic()))

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

            intervals: dict[int, dict[str, float]] = {}
            for kind, tid, ts in events:
                intervals.setdefault(tid, {})[kind] = ts

            self.assertEqual(set(intervals.keys()), {0, 1}, f"both threads must complete: {events}")
            (e0, x0) = (intervals[0]["enter"], intervals[0]["exit"])
            (e1, x1) = (intervals[1]["enter"], intervals[1]["exit"])
            overlap = e0 < x1 and e1 < x0
            self.assertFalse(overlap, f"lock holders overlapped: {intervals}")


class PinnedHashVerifierTest(unittest.TestCase):
    """Production PinnedHashVerifier (T-3723, ORG-046 §4 pinned-hash).

    Every test injects a HERMETIC tmp pin path (codex ORG-041 #5): none omit
    pin_path, read a real/default store, or derive the 'wrong' pin from the same
    fixture bytes — else a trivially-True verifier could accidentally validate.
    """

    CONTENT = b"repos:\n  - name: pantheon-config\n"
    # A valid-shape but deliberately non-matching pin (NOT derived from CONTENT).
    WRONG_PIN = "0" * 64

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pin_path = Path(self._tmp.name) / "repos.yaml.pin"

    def _write_pin(self, value: str) -> None:
        self.pin_path.write_text(value, encoding="utf-8")

    def _good_pin(self) -> str:
        return hashlib.sha256(self.CONTENT).hexdigest()

    # -- positive control: a matching pin verifies (so False carries meaning) --
    def test_matching_pin_verifies(self):
        self._write_pin(self._good_pin())
        v = rr.PinnedHashVerifier(self.pin_path)
        self.assertTrue(v(Path("repos.yaml"), self.CONTENT))

    def test_matching_pin_with_trailing_newline_verifies(self):
        self._write_pin(self._good_pin() + "\n")  # pin file may have a trailing \n
        v = rr.PinnedHashVerifier(self.pin_path)
        self.assertTrue(v(Path("repos.yaml"), self.CONTENT))

    # -- false-control: garbage/absent/wrong pins CANNOT verify (the mandate) --
    def test_absent_pin_file_cannot_verify(self):
        # No pin written at all.
        v = rr.PinnedHashVerifier(self.pin_path)
        self.assertFalse(v(Path("repos.yaml"), self.CONTENT))

    def test_empty_pin_cannot_verify(self):
        self._write_pin("")
        self.assertFalse(rr.PinnedHashVerifier(self.pin_path)(Path("repos.yaml"), self.CONTENT))

    def test_truncated_pin_cannot_verify(self):
        self._write_pin(self._good_pin()[:32])  # 32 hex chars, not 64
        self.assertFalse(rr.PinnedHashVerifier(self.pin_path)(Path("repos.yaml"), self.CONTENT))

    def test_nonhex_garbage_pin_cannot_verify(self):
        self._write_pin("z" * 64)  # 64 chars but not hex
        self.assertFalse(rr.PinnedHashVerifier(self.pin_path)(Path("repos.yaml"), self.CONTENT))

    def test_wrong_value_pin_cannot_verify(self):
        self._write_pin(self.WRONG_PIN)  # valid shape, wrong value (not from CONTENT)
        self.assertFalse(rr.PinnedHashVerifier(self.pin_path)(Path("repos.yaml"), self.CONTENT))

    def test_uppercase_hex_pin_cannot_verify(self):
        # sha256().hexdigest() is lowercase; an uppercase pin must not match
        # (the regex is lowercase-only — no accidental case-folding).
        self._write_pin(self._good_pin().upper())
        self.assertFalse(rr.PinnedHashVerifier(self.pin_path)(Path("repos.yaml"), self.CONTENT))

    # -- tamper detection: one flipped content byte fails against a good pin --
    def test_one_byte_tamper_fails(self):
        self._write_pin(self._good_pin())
        tampered = bytearray(self.CONTENT)
        tampered[0] ^= 0x01
        self.assertFalse(rr.PinnedHashVerifier(self.pin_path)(Path("repos.yaml"), bytes(tampered)))

    # -- fail-closed on an unreadable pin (a directory at the path) --
    def test_unreadable_pin_fails_closed_no_raise(self):
        self.pin_path.mkdir()  # a directory where a file is expected
        v = rr.PinnedHashVerifier(self.pin_path)
        self.assertFalse(v(Path("repos.yaml"), self.CONTENT))  # returns False, does not raise

    # -- offline invariant (codex ORG-041 #3): verify never runs a subprocess --
    def test_verify_is_offline_no_subprocess(self):
        self._write_pin(self._good_pin())
        v = rr.PinnedHashVerifier(self.pin_path)
        with unittest.mock.patch("subprocess.run") as run:
            self.assertTrue(v(Path("repos.yaml"), self.CONTENT))
        run.assert_not_called()

    # -- integration: production verifier through load_manifest -> resolve_remote --
    def test_pinned_verifier_end_to_end_valid_and_tampered(self):
        yaml_text = (
            "auth_refs:\n"
            "  deploy-key:pantheon-config:\n"
            "    allowed_hosts: [github.com]\n"
            "repos:\n"
            "  - name: pantheon-config\n"
            "    remotes:\n"
            "      - role: primary\n"
            "        url: git@github.com:example-org/pantheon-config.git\n"
            "        auth: deploy-key:pantheon-config\n"
        )
        raw = yaml_text.encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "repos.yaml"
            manifest_path.write_bytes(raw)
            pin_path = Path(tmp) / "pin"

            # Valid: pin matches the manifest bytes -> verified -> resolves.
            pin_path.write_text(hashlib.sha256(raw).hexdigest(), encoding="utf-8")
            manifest = rr.load_manifest(manifest_path, signature_verifier=rr.PinnedHashVerifier(pin_path))
            self.assertTrue(manifest.signature_verified)
            runner = _ScriptedRunner()
            runner.route(["git", "ls-remote", PRIMARY_URL], _cp(["git", "ls-remote", PRIMARY_URL], rc=0))
            self.assertEqual(rr.resolve_remote("pantheon-config", manifest=manifest, git_runner=runner).url, PRIMARY_URL)

            # Tampered: same pin, but the on-disk manifest was mutated -> refused.
            manifest_path.write_bytes(raw + b"# tamper\n")
            tampered = rr.load_manifest(manifest_path, signature_verifier=rr.PinnedHashVerifier(pin_path))
            self.assertFalse(tampered.signature_verified)
            with self.assertRaises(rr.ManifestIntegrityError):
                rr.resolve_remote("pantheon-config", manifest=tampered, git_runner=_ScriptedRunner())


class _FakePinRunner:
    """PinWriterRunner fake: argv -> (rc, bytes). Routes on 'fetch'/'show'."""

    def __init__(self, *, fetch_rc: int = 0, show_rc: int = 0, show_bytes: bytes = b""):
        self.fetch_rc = fetch_rc
        self.show_rc = show_rc
        self.show_bytes = show_bytes
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(argv)
        if "fetch" in argv:
            return (self.fetch_rc, b"")
        if "show" in argv:
            return (self.show_rc, self.show_bytes)
        return (0, b"")


class EstablishPinTest(unittest.TestCase):
    """Pin-writer (T-3723 §8.1). Hermetic — the fake runner supplies the
    'authenticated origin' bytes; no real git/network."""

    ORIGIN = b"repos:\n  - name: pantheon-config\n"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.pin_path = self.root / "out-of-repo" / "repos.yaml.pin"  # out-of-repo store

    def test_pins_authenticated_origin_bytes_and_round_trips(self):
        runner = _FakePinRunner(show_bytes=self.ORIGIN)
        pin = rr.establish_pin(repo_root=self.root, pin_path=self.pin_path, runner=runner)
        self.assertEqual(pin, hashlib.sha256(self.ORIGIN).hexdigest())
        # end-to-end: the written pin verifies the origin bytes
        self.assertTrue(rr.PinnedHashVerifier(self.pin_path)(Path("repos.yaml"), self.ORIGIN))

    def test_never_pins_working_tree_bytes(self):
        # §8.1 (a): a tampered working-tree manifest is on disk, but the pin must
        # hash the ORIGIN bytes from `git show`, never the local file.
        (self.root / "repos.yaml").write_bytes(b"tampered: true\n")
        runner = _FakePinRunner(show_bytes=self.ORIGIN)
        rr.establish_pin(repo_root=self.root, pin_path=self.pin_path, runner=runner)
        v = rr.PinnedHashVerifier(self.pin_path)
        self.assertTrue(v(Path("repos.yaml"), self.ORIGIN))            # origin verifies
        self.assertFalse(v(Path("repos.yaml"), b"tampered: true\n"))   # working-tree does NOT

    def test_fetch_failure_aborts_pinless(self):
        # §8.1 (c): an unauthenticated / unknown-host fetch aborts without a pin.
        runner = _FakePinRunner(fetch_rc=128, show_bytes=self.ORIGIN)
        with self.assertRaises(rr.ManifestIntegrityError):
            rr.establish_pin(repo_root=self.root, pin_path=self.pin_path, runner=runner)
        self.assertFalse(self.pin_path.exists(), "no pin written on fetch failure")
        self.assertFalse(any("show" in argv for argv in runner.calls),
                         "must not read the blob after a failed fetch")

    def test_show_failure_aborts_pinless(self):
        runner = _FakePinRunner(show_rc=128, show_bytes=b"")
        with self.assertRaises(rr.ManifestIntegrityError):
            rr.establish_pin(repo_root=self.root, pin_path=self.pin_path, runner=runner)
        self.assertFalse(self.pin_path.exists(), "no pin written on show failure")

    def test_pin_file_mode_0600(self):
        import stat
        runner = _FakePinRunner(show_bytes=self.ORIGIN)
        rr.establish_pin(repo_root=self.root, pin_path=self.pin_path, runner=runner)
        self.assertEqual(stat.S_IMODE(self.pin_path.stat().st_mode), 0o600)

    def test_do_fetch_false_skips_fetch(self):
        runner = _FakePinRunner(show_bytes=self.ORIGIN)
        rr.establish_pin(repo_root=self.root, pin_path=self.pin_path, do_fetch=False, runner=runner)
        self.assertFalse(any("fetch" in argv for argv in runner.calls), "do_fetch=False must not fetch")
        self.assertTrue(self.pin_path.exists())


class ProductionWiringTest(unittest.TestCase):
    """T-3764: pin-store path + the load_production_manifest() 'build_config'
    factory that wires the REAL PinnedHashVerifier onto <root>/repos.yaml."""

    YAML = (
        "auth_refs:\n"
        "  deploy-key:pantheon-config:\n"
        "    allowed_hosts: [github.com]\n"
        "repos:\n"
        "  - name: pantheon-config\n"
        "    remotes:\n"
        "      - role: primary\n"
        "        url: git@github.com:example-org/pantheon-config.git\n"
        "        auth: deploy-key:pantheon-config\n"
    )

    # -- pin path: PANTHEON_STATE_DIR override + the ~/.local/state default --
    def test_default_pin_path_honors_state_dir_override(self):
        with patch.dict(os.environ, {"PANTHEON_STATE_DIR": "/tmp/xyz-state"}, clear=False):
            self.assertEqual(rr.default_pin_path(), Path("/tmp/xyz-state") / "repos.yaml.pin")

    def test_default_pin_path_defaults_to_local_state_pantheon(self):
        env = {k: v for k, v in os.environ.items() if k != "PANTHEON_STATE_DIR"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                rr.default_pin_path(),
                Path.home() / ".local" / "state" / "pantheon" / "repos.yaml.pin",
            )

    def test_default_pin_path_is_out_of_repo(self):
        # ORG-046 §3 root-of-trust: the pin must not live inside the repo it verifies.
        self.assertNotIn(rr.default_repo_root(), rr.default_pin_path().parents)

    # -- default_repo_root points at the clone this tools/ tree lives in --
    def test_default_repo_root_contains_this_module(self):
        root = rr.default_repo_root()
        self.assertTrue((root / "tools" / "remote_resolve.py").is_file())

    # -- build_config: matching pin -> verified True (resolve_remote non-inert) --
    def test_load_production_manifest_matching_pin_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = self.YAML.encode("utf-8")
            (root / "repos.yaml").write_bytes(raw)
            pin_path = root / "out-of-repo" / "repos.yaml.pin"
            pin_path.parent.mkdir()
            pin_path.write_text(hashlib.sha256(raw).hexdigest(), encoding="utf-8")

            manifest = rr.load_production_manifest(repo_root=root, pin_path=pin_path)
            self.assertTrue(manifest.signature_verified)
            self.assertIn("pantheon-config", manifest.repos)

    # -- build_config: mismatched pin -> verified False (fail-closed) --
    def test_load_production_manifest_mismatched_pin_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repos.yaml").write_bytes(self.YAML.encode("utf-8"))
            pin_path = root / "out-of-repo" / "repos.yaml.pin"
            pin_path.parent.mkdir()
            pin_path.write_text("0" * 64, encoding="utf-8")  # wrong hash

            manifest = rr.load_production_manifest(repo_root=root, pin_path=pin_path)
            self.assertFalse(manifest.signature_verified)

    # -- build_config: absent pin -> verified False (the pre-bootstrap inert state) --
    def test_load_production_manifest_absent_pin_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repos.yaml").write_bytes(self.YAML.encode("utf-8"))
            pin_path = root / "out-of-repo" / "repos.yaml.pin"  # never created

            manifest = rr.load_production_manifest(repo_root=root, pin_path=pin_path)
            self.assertFalse(manifest.signature_verified)


if __name__ == "__main__":
    unittest.main()
