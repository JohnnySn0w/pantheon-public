"""test_repos_manifest.py — T-1998: the committed repos.yaml ORG-046 manifest
(auth_refs + per-repo `remotes:`) is well-formed and read-failover resolves.

Hermetic: fake git_runner (NO network), stub signature_verifier. Proves the
REGISTERED manifest for pantheon-config (GitHub-primary + git.your-self-hosted-git.example mirror,
per Q1) resolves to the primary when reachable and fails over to the mirror when
the primary is unreachable — the ORG-046 §2 read-only failover contract — with
host-bindings (finding 6) enforced and the unsigned-manifest gate (the reason
this registration is inert until a production signature-verifier lands).
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

import tools.remote_resolve as rr

REPOS_YAML = Path(__file__).resolve().parent.parent / "repos.yaml"


def _cp(argv, rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=argv, returncode=rc, stdout=stdout, stderr=stderr)


def _load(*, signed: bool = True):
    return rr.load_manifest(REPOS_YAML, signature_verifier=lambda p, c: signed)


class ReposManifestTest(unittest.TestCase):
    def setUp(self):
        self.m = _load()

    def test_pantheon_config_registered_and_wellformed(self):
        pc = self.m.repos.get("pantheon-config")
        self.assertIsNotNone(pc, "pantheon-config missing its ORG-046 remotes: block")
        primaries = [r for r in pc.remotes if r.role == "primary"]
        mirrors = [r for r in pc.remotes if r.role == "mirror"]
        self.assertEqual(len(primaries), 1, "exactly one primary (ORG-046 §1 invariant)")
        self.assertGreaterEqual(len(mirrors), 1, "at least one mirror for failover")
        # Q1 disposition: GitHub-primary first, git.your-self-hosted-git.example the failover mirror.
        self.assertIn("github.com", primaries[0].url)
        self.assertIn("git.your-self-hosted-git.example", mirrors[0].url)

    def test_every_remote_host_is_bound(self):
        # finding 6: a remote's url host must be inside its auth ref's allowed_hosts.
        pc = self.m.repos["pantheon-config"]
        for r in pc.remotes:
            host = rr._host_of(r.url)
            auth = self.m.auth_refs.get(r.auth_ref)
            self.assertIsNotNone(auth, f"auth ref {r.auth_ref!r} not in manifest")
            self.assertIn(host, auth.allowed_hosts,
                          f"host {host!r} not bound for auth {r.auth_ref!r}")

    def test_resolves_to_primary_when_reachable(self):
        runner = lambda argv, cwd, timeout: _cp(argv, 0, stdout="ref\tHEAD\n")
        res = rr.resolve_remote("pantheon-config", manifest=self.m, git_runner=runner)
        self.assertIn("github.com", res.url)
        self.assertFalse(res.is_healed, "primary reachable → no failover")

    def test_fails_over_to_mirror_when_primary_unreachable(self):
        def runner(argv, cwd, timeout):
            url = argv[-1]
            up = "git.your-self-hosted-git.example" in url  # GitHub unreachable, mirror up
            return _cp(argv, 0 if up else 2, stdout=("ref\tHEAD\n" if up else ""))
        res = rr.resolve_remote("pantheon-config", manifest=self.m, git_runner=runner)
        self.assertIn("git.your-self-hosted-git.example", res.url)
        self.assertTrue(res.is_healed, "primary down → failover to mirror")
        self.assertIn("github.com", res.healed_from or "")

    def test_unsigned_manifest_is_refused(self):
        # The production gate: resolve_remote refuses an unsigned manifest, which is
        # why registering these remotes is INERT until a signature-verifier lands
        # (a T-1998 follow-up). No consumer resolves an unsigned manifest.
        unsigned = _load(signed=False)
        with self.assertRaises(rr.ManifestIntegrityError):
            rr.resolve_remote("pantheon-config", manifest=unsigned,
                              git_runner=lambda a, c, t: _cp(a, 0))


if __name__ == "__main__":
    unittest.main()
