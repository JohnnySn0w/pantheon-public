"""Tests for the ORG-101 §2 `writable` manifest signal (T-4690).

Covers: the parse/validate rules on `writable`, and the origin -> manifest-primary
match used by the write path (`writable_mirror_for`), including SSH host-alias
origins and the fail-closed no-match case. Pure: no git, no network.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import tools.remote_resolve as rr

_PRIMARY = "git@github.com:example-org/pantheon-config.git"
_MIRROR = "https://pantheon-fleet@git.your-self-hosted-git.example/pantheon/pantheon-config.git"


def _yaml(*, mirror_extra: str = "", primary_extra: str = "") -> str:
    return f"""
schema_version: 1
auth_refs:
  deploy-key:pc:
    allowed_hosts: [github.com]
  token:fj:
    allowed_hosts: [git.your-self-hosted-git.example]
repos:
  - name: pantheon-config
    remotes:
      - role: primary
        url: {_PRIMARY}
        auth: deploy-key:pc
{primary_extra}
      - role: mirror
        url: {_MIRROR}
        auth: token:fj
{mirror_extra}
"""


def _load(text: str) -> rr.Manifest:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "repos.yaml"
        p.write_text(text, encoding="utf-8")
        return rr.load_manifest(p, signature_verifier=lambda _p, _b: True)


class WritableParseTest(unittest.TestCase):
    def test_default_is_not_writable(self):
        m = _load(_yaml())
        self.assertFalse(any(r.writable for r in m.repos["pantheon-config"].remotes))

    def test_writable_true_on_mirror_parses(self):
        m = _load(_yaml(mirror_extra="        writable: true"))
        mirror = m.repos["pantheon-config"].remotes[1]
        self.assertTrue(mirror.writable)

    def test_non_bool_writable_is_rejected(self):
        with self.assertRaises(rr.ManifestIntegrityError):
            _load(_yaml(mirror_extra='        writable: "yes"'))

    def test_writable_on_primary_is_rejected(self):
        with self.assertRaises(rr.ManifestIntegrityError):
            _load(_yaml(primary_extra="        writable: true"))

    # Repos whose Forgejo mirror has been converted, put under the reconciler and
    # protection-parity proven (ORG-101 §4/§5). Adding a repo here is the deliberate,
    # reviewed step that switches on its write path; anything else must stay off.
    WRITABLE_ALLOWLIST = {
        ("pantheon-config", "https://pantheon-fleet@git.your-self-hosted-git.example/github-mirror/pantheon-config.git"),
    }

    def test_live_repos_yaml_writable_only_on_allowlist(self):
        m = rr.load_manifest(
            Path(__file__).resolve().parent.parent / "repos.yaml",
            signature_verifier=lambda _p, _b: True,
        )
        writable = {(repo.name, r.url) for repo in m.repos.values()
                    for r in repo.remotes if r.writable}
        self.assertEqual(writable, self.WRITABLE_ALLOWLIST)


class WritableMirrorForTest(unittest.TestCase):
    def setUp(self):
        self.m = _load(_yaml(mirror_extra="        writable: true"))
        self.m_off = _load(_yaml())

    def test_canonical_origin_matches(self):
        r = rr.writable_mirror_for(self.m, _PRIMARY)
        self.assertEqual(r.url, _MIRROR)

    def test_https_origin_matches(self):
        r = rr.writable_mirror_for(self.m, "https://github.com/example-org/pantheon-config")
        self.assertEqual(r.url, _MIRROR)

    def test_ssh_host_alias_origin_matches(self):
        # Real seat clones use per-repo ssh aliases (github-<repo>).
        for url in (
            "git@github.com:example-org/pantheon-config.git",
            "git@github-sseed-presence:example-org/pantheon-config.git",
            "ssh://git@github.com/example-org/pantheon-config.git",
        ):
            with self.subTest(url=url):
                self.assertEqual(rr.writable_mirror_for(self.m, url).url, _MIRROR)

    def test_owner_repo_match_is_case_insensitive(self):
        r = rr.writable_mirror_for(self.m, "git@github.com:example-org/Pantheon-Config.git")
        self.assertIsNotNone(r)

    def test_unmatchable_origins_return_none(self):
        for url in (
            "git@github-other:example-org/other-repo.git",       # different repo
            "git@github.com:SomeoneElse/pantheon-config.git",   # different owner
            "git@gitlab.com:example-org/pantheon-config.git",    # different host class
            "git@evilgithub-x:example-org/pantheon-config.git",  # not a github-* alias
            "https://example.com/x/y.git",
            "/tmp/some/local/path",
            "",
            "garbage",
        ):
            with self.subTest(url=url):
                self.assertIsNone(rr.writable_mirror_for(self.m, url))

    def test_not_writable_returns_none(self):
        self.assertIsNone(rr.writable_mirror_for(self.m_off, _PRIMARY))

    def test_unverified_manifest_returns_none(self):
        unverified = rr.Manifest(
            repos=self.m.repos, auth_refs=self.m.auth_refs, signature_verified=False
        )
        self.assertIsNone(rr.writable_mirror_for(unverified, _PRIMARY))

    def test_host_binding_violation_returns_none(self):
        bad = rr.Manifest(
            repos=self.m.repos,
            auth_refs={**self.m.auth_refs, "token:fj": rr.AuthRef("token:fj", ("other.host",))},
            signature_verified=True,
        )
        self.assertIsNone(rr.writable_mirror_for(bad, _PRIMARY))


if __name__ == "__main__":
    unittest.main()
