"""test_manifest_pin.py — unit tests for tools/manifest_pin.py (T-3764).

Hermetic: a fake PinWriterRunner supplies the "authenticated origin" bytes, so
no real git/network runs. Every test passes an EXPLICIT tmp pin_path (out of
repo) and repo_root — no test reads the real ~/.local/state store or derives the
"wrong" pin from the same fixture bytes (codex ORG-041 #5).
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import tools.manifest_pin as mp
import tools.remote_resolve as rr


class _FakePinRunner:
    """PinWriterRunner fake: argv -> (rc, bytes). Routes on 'fetch'/'show'.
    `show_bytes` is the bytes the authenticated origin serves for repos.yaml."""

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


ORIGIN = b"repos:\n  - name: pantheon-config\n"
TAMPERED = b"repos:\n  - name: attacker\n"


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)               # the clone (working tree)
        self.pin_path = self.root / "out-of-repo" / "repos.yaml.pin"  # OUT of repo

    def _write_worktree(self, data: bytes) -> None:
        (self.root / "repos.yaml").write_bytes(data)


class BootstrapTest(_Base):
    def test_bootstrap_pins_origin_bytes(self):
        self._write_worktree(ORIGIN)
        runner = _FakePinRunner(show_bytes=ORIGIN)
        res = mp.bootstrap_pin(repo_root=self.root, pin_path=self.pin_path, runner=runner)
        self.assertEqual(res["action"], "bootstrapped")
        self.assertEqual(res["pin"], hashlib.sha256(ORIGIN).hexdigest())
        self.assertTrue(res["worktree_matches"])  # worktree == origin
        # the written pin actually verifies the origin bytes end-to-end
        self.assertTrue(rr.PinnedHashVerifier(self.pin_path)(Path("repos.yaml"), ORIGIN))

    def test_bootstrap_worktree_mismatch_reported(self):
        # working tree tampered vs the origin bytes the pin is derived from
        self._write_worktree(TAMPERED)
        runner = _FakePinRunner(show_bytes=ORIGIN)
        res = mp.bootstrap_pin(repo_root=self.root, pin_path=self.pin_path, runner=runner)
        self.assertEqual(res["pin"], hashlib.sha256(ORIGIN).hexdigest())
        self.assertFalse(res["worktree_matches"])  # tampered worktree does NOT verify

    def test_bootstrap_refuses_existing_pin_and_does_not_clobber(self):
        # first pin
        runner = _FakePinRunner(show_bytes=ORIGIN)
        mp.bootstrap_pin(repo_root=self.root, pin_path=self.pin_path, runner=runner)
        original = self.pin_path.read_text()
        # a second bootstrap (no force) must refuse + leave the pin untouched,
        # even though the origin would now serve DIFFERENT bytes
        runner2 = _FakePinRunner(show_bytes=TAMPERED)
        res = mp.bootstrap_pin(repo_root=self.root, pin_path=self.pin_path, runner=runner2)
        self.assertEqual(res["action"], "refused_exists")
        self.assertFalse(any("show" in c for c in runner2.calls), "must not fetch/pin on a refusal")
        self.assertEqual(self.pin_path.read_text(), original, "pin must be untouched on refusal")

    def test_bootstrap_force_overwrites(self):
        runner = _FakePinRunner(show_bytes=ORIGIN)
        mp.bootstrap_pin(repo_root=self.root, pin_path=self.pin_path, runner=runner)
        runner2 = _FakePinRunner(show_bytes=TAMPERED)
        res = mp.bootstrap_pin(repo_root=self.root, pin_path=self.pin_path, force=True, runner=runner2)
        self.assertEqual(res["action"], "bootstrapped")
        self.assertEqual(res["pin"], hashlib.sha256(TAMPERED).hexdigest())

    def test_bootstrap_fetch_failure_writes_no_pin(self):
        runner = _FakePinRunner(fetch_rc=128, show_bytes=ORIGIN)
        with self.assertRaises(rr.ManifestIntegrityError):
            mp.bootstrap_pin(repo_root=self.root, pin_path=self.pin_path, runner=runner)
        self.assertFalse(self.pin_path.exists(), "fail-closed: no pin on fetch failure")


class RefreshTest(_Base):
    def _seed(self, data: bytes) -> None:
        mp.bootstrap_pin(repo_root=self.root, pin_path=self.pin_path,
                         runner=_FakePinRunner(show_bytes=data))

    def test_refresh_tracks_authorized_change_changed_true(self):
        self._seed(ORIGIN)
        self._write_worktree(TAMPERED)  # pretend the working tree was pulled to the new manifest
        res = mp.refresh_pin(repo_root=self.root, pin_path=self.pin_path,
                             runner=_FakePinRunner(show_bytes=TAMPERED))
        self.assertEqual(res["action"], "refreshed")
        self.assertTrue(res["changed"])
        self.assertEqual(res["pin"], hashlib.sha256(TAMPERED).hexdigest())
        self.assertTrue(res["worktree_matches"])  # worktree now == new origin

    def test_refresh_idempotent_unchanged(self):
        self._seed(ORIGIN)
        res = mp.refresh_pin(repo_root=self.root, pin_path=self.pin_path,
                             runner=_FakePinRunner(show_bytes=ORIGIN))
        self.assertFalse(res["changed"])
        self.assertEqual(res["pin"], hashlib.sha256(ORIGIN).hexdigest())

    def test_refresh_worktree_stale_reported_false(self):
        # origin advanced but the working tree has NOT been pulled yet (phase-2)
        self._seed(ORIGIN)
        self._write_worktree(ORIGIN)  # working tree still on the old manifest
        res = mp.refresh_pin(repo_root=self.root, pin_path=self.pin_path,
                             runner=_FakePinRunner(show_bytes=TAMPERED))
        self.assertTrue(res["changed"])
        self.assertFalse(res["worktree_matches"], "stale worktree must not verify against the new pin")

    def test_refresh_fetch_failure_leaves_prior_pin(self):
        self._seed(ORIGIN)
        prior = self.pin_path.read_text()
        with self.assertRaises(rr.ManifestIntegrityError):
            mp.refresh_pin(repo_root=self.root, pin_path=self.pin_path,
                           runner=_FakePinRunner(fetch_rc=128, show_bytes=TAMPERED))
        self.assertEqual(self.pin_path.read_text(), prior, "fail-closed: prior pin intact")


class StatusTest(_Base):
    def test_status_absent_pin(self):
        self._write_worktree(ORIGIN)
        res = mp.pin_status(repo_root=self.root, pin_path=self.pin_path)
        self.assertFalse(res["pin_present"])
        self.assertFalse(res["worktree_verifies"])
        self.assertTrue(res["worktree_present"])

    def test_status_present_pin_verifies(self):
        self._write_worktree(ORIGIN)
        mp.bootstrap_pin(repo_root=self.root, pin_path=self.pin_path,
                         runner=_FakePinRunner(show_bytes=ORIGIN))
        res = mp.pin_status(repo_root=self.root, pin_path=self.pin_path)
        self.assertTrue(res["pin_present"])
        self.assertTrue(res["worktree_verifies"])
        self.assertEqual(res["worktree_hash"], hashlib.sha256(ORIGIN).hexdigest())

    def test_status_present_pin_stale_worktree(self):
        self._write_worktree(ORIGIN)
        mp.bootstrap_pin(repo_root=self.root, pin_path=self.pin_path,
                         runner=_FakePinRunner(show_bytes=ORIGIN))
        self._write_worktree(TAMPERED)  # working tree drifted after the pin
        res = mp.pin_status(repo_root=self.root, pin_path=self.pin_path)
        self.assertTrue(res["pin_present"])
        self.assertFalse(res["worktree_verifies"])


class CliTest(_Base):
    def test_cli_bootstrap_then_refuse_then_status(self):
        self._write_worktree(ORIGIN)
        runner = _FakePinRunner(show_bytes=ORIGIN)
        rc = mp.main(["bootstrap", "--repo-root", str(self.root), "--pin-path", str(self.pin_path)],
                     runner=runner)
        self.assertEqual(rc, mp.EXIT_OK)
        # second bootstrap refuses
        rc2 = mp.main(["bootstrap", "--repo-root", str(self.root), "--pin-path", str(self.pin_path)],
                      runner=_FakePinRunner(show_bytes=ORIGIN))
        self.assertEqual(rc2, mp.EXIT_REFUSED)
        # status ok
        rc3 = mp.main(["status", "--repo-root", str(self.root), "--pin-path", str(self.pin_path)])
        self.assertEqual(rc3, mp.EXIT_OK)

    def test_cli_fetch_failure_exit_error(self):
        self._write_worktree(ORIGIN)
        rc = mp.main(["bootstrap", "--repo-root", str(self.root), "--pin-path", str(self.pin_path)],
                     runner=_FakePinRunner(fetch_rc=128, show_bytes=ORIGIN))
        self.assertEqual(rc, mp.EXIT_ERROR)
        self.assertFalse(self.pin_path.exists())

    def test_cli_refresh_ok(self):
        self._write_worktree(ORIGIN)
        mp.bootstrap_pin(repo_root=self.root, pin_path=self.pin_path,
                         runner=_FakePinRunner(show_bytes=ORIGIN))
        rc = mp.main(["refresh", "--repo-root", str(self.root), "--pin-path", str(self.pin_path)],
                     runner=_FakePinRunner(show_bytes=ORIGIN))
        self.assertEqual(rc, mp.EXIT_OK)


if __name__ == "__main__":
    unittest.main()
