#!/usr/bin/env python3
"""Tests for tools/git_conformance_sweep.py (ORG-101 §7, T-4690 leg 4)."""
from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.git_conformance_sweep as sw
from tools import remote_resolve as rr


def gh(name, private=True, archived=False, branch="main"):
    return {"name": name, "private": private, "archived": archived, "default_branch": branch}


def fj(mirror=False, private=True, branch="main", empty=False):
    return {"mirror": mirror, "private": private, "default_branch": branch, "empty": empty}


def kinds(findings, info=False):
    return sorted((f.kind, f.subject) for f in findings if f.info == info)


class Repos(unittest.TestCase):
    def run_repos(self, gh_list, fj_map, gh_prot=(), fj_prot=()):
        return sw.repo_findings(gh_list, fj_map, gh_protected=lambda n, b: n in gh_prot,
                                fj_protected=lambda n, b: n in fj_prot)

    def test_conformant_repo_has_no_findings(self):
        self.assertEqual(self.run_repos([gh("a")], {"a": fj()}, gh_prot=("a",), fj_prot=("a",)), [])

    def test_each_repo_departure_is_flagged(self):
        got = self.run_repos(
            [gh("nocopy"), gh("mirror"), gh("leak"), gh("branch", branch="trunk"), gh("prot"), gh("bad name")],
            {"mirror": fj(mirror=True), "leak": fj(private=False), "branch": fj(branch="main"),
             "prot": fj(), "bad name": fj()},
            gh_prot=("prot",))
        self.assertEqual(kinds(got), sorted([
            ("NO_COPY", "nocopy"), ("PULL_MIRROR", "mirror"), ("EXPOSURE", "leak"),
            ("DEFAULT_BRANCH", "branch"), ("PROTECTION", "prot"), ("UNCOVERED", "bad name")]))

    def test_safe_directions_and_dispositions_are_info_only(self):
        got = self.run_repos([gh("pub", private=False), gh("arch", archived=True)],
                             {"pub": fj(private=True), "arch": fj(mirror=True), "orphan": fj()})
        self.assertEqual(kinds(got), [])
        self.assertEqual(kinds(got, info=True),
                         [("ORPHAN", "orphan"), ("PULL_MIRROR", "arch"), ("VISIBILITY", "pub")])

    def test_forgejo_stricter_than_github_is_fine(self):
        self.assertEqual(self.run_repos([gh("a")], {"a": fj()}, fj_prot=("a",)), [])

    def test_empty_copy_default_branch_is_not_compared(self):
        self.assertEqual(self.run_repos([gh("a", branch="trunk")], {"a": fj(empty=True)}), [])

    def test_one_repos_check_error_is_a_finding_not_an_abort(self):
        def boom(n, b):
            if n == "bad":
                raise OSError("api down")
            return n == "prot"
        got = sw.repo_findings([gh("bad"), gh("prot")], {"bad": fj(), "prot": fj()},
                               gh_protected=boom, fj_protected=lambda n, b: False)
        self.assertEqual(kinds(got), [("PROTECTION", "prot"), ("UNVERIFIED", "bad")])


def manifest(writable=True):
    return rr.Manifest(
        repos={
            "pantheon-config": rr.RepoManifestEntry("pantheon-config", (
                rr.RemoteEntry("primary", "git@github.com:example-org/pantheon-config.git", "k"),
                rr.RemoteEntry("mirror", "https://u@git.your-self-hosted-git.example/github-mirror/pantheon-config.git",
                               "t", writable=writable))),
            "no-remotes": rr.RepoManifestEntry("no-remotes", ()),
        },
        auth_refs={}, signature_verified=True)


class Seats(unittest.TestCase):
    YAML = "repos: []\n"

    def fake(self, origin="git@github.com:example-org/pantheon-config.git",
             rewrite=None, pin="match", armed=True):
        def run(account, argv):
            def cp(rc, out=""):
                return subprocess.CompletedProcess(argv, rc, out, "")
            if argv[0] == "grep":  # T-5672: is the seat's read failover armed?
                return cp(0 if armed else 1)
            if argv[-1] == "origin":
                return cp(0, origin + "\n") if origin else cp(128)
            if argv[0] == "git" and argv[3] == "config":
                return cp(0, rewrite) if rewrite else cp(1)
            if argv[1].endswith(rr.PIN_FILENAME):
                if pin is None:
                    return cp(1)
                return cp(0, hashlib.sha256(self.YAML.encode()).hexdigest() if pin == "match" else "0" * 64)
            if argv[1].endswith("repos.yaml"):
                return cp(0, self.YAML)
            raise AssertionError(argv)
        return run

    def seats(self, **kw):
        return sw.seat_findings(manifest(), ["acct"], run=self.fake(**kw), home=lambda a: Path("/h/acct"))

    def test_alias_origin_with_matching_pin_is_conformant(self):
        self.assertEqual(self.seats(), [])

    def test_foreign_origin_is_flagged(self):
        self.assertEqual(kinds(self.seats(origin="git@github.com:someone/fork.git")),
                         [("ORIGIN", "acct:~/pantheon-config")])

    def test_url_rewrite_is_flagged(self):
        self.assertEqual(kinds(self.seats(rewrite="url.x.pushinsteadof y\n")),
                         [("URL_REWRITE", "acct:~/pantheon-config")])

    def test_missing_and_stale_pin_are_flagged(self):
        self.assertEqual(kinds(self.seats(pin=None)), [("PIN", "acct:~/pantheon-config")])
        self.assertEqual(kinds(self.seats(pin="stale")), [("PIN", "acct:~/pantheon-config")])

    def test_stale_pin_on_an_unarmed_seat_is_info_not_a_finding(self):
        # T-5672 (10-27: codex, codex-fallback, suborch1): an unarmed seat never consults
        # its pin and its pull never refreshes it, so a stale pin there is not a refusal.
        got = self.seats(pin="stale", armed=False)
        self.assertEqual([(f.kind, f.subject, f.info) for f in got],
                         [("PIN", "acct:~/pantheon-config", True)])
        self.assertIn("refresh before arming", got[0].detail)

    def test_missing_pin_on_an_unarmed_seat_is_still_flagged(self):
        self.assertEqual(kinds(self.seats(pin=None, armed=False)), [("PIN", "acct:~/pantheon-config")])

    def test_no_clone_is_not_this_checks_business(self):
        self.assertEqual(self.seats(origin=None), [])

    def test_pin_check_keys_on_the_repo_name_not_the_checkout_dir(self):
        # Regression: keyed on REPO_ROOT.name, so a sweep run from a worktree never checked pins.
        with mock.patch.object(sw, "REPO_ROOT", Path("/somewhere/wt-x")):
            self.assertEqual(kinds(self.seats(pin=None)), [("PIN", "acct:~/pantheon-config")])


class SkipList(unittest.TestCase):
    def test_skip_suppresses_and_expires_with_its_ticket(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "done").mkdir()
            sl = td / "skip.yaml"
            sl.write_text("skips:\n"
                          "  - {kind: PIN, subject: 'a:~/r', disposition_ticket: T-1, reason: x}\n"
                          "  - {kind: PIN, subject: 'b:~/r', disposition_ticket: T-2, reason: x}\n"
                          "  - {kind: PIN, subject: 'c:~/r', reason: no ticket}\n")
            (td / "done" / "T-2.md").write_text("")
            skips = sw.load_skips(sl, td / "done")
            findings = [sw.Finding("PIN", s, "d") for s in ("a:~/r", "b:~/r", "c:~/r")]
            kept, skipped = sw.apply_skips(findings, skips)
        self.assertEqual([f.subject for f in skipped], ["a:~/r"])
        self.assertEqual([f.subject for f in kept], ["b:~/r", "c:~/r"])

    def test_broken_skip_list_skips_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.yaml"
            p.write_text("skips: [unclosed\n")
            self.assertEqual(sw.load_skips(p, Path(td)), [])

    def test_repo_skip_list_parses(self):
        live = sw.load_skips(sw.SKIP_LIST, Path("/nonexistent"))
        self.assertTrue(live and all(e["kind"] and e["subject"] for e in live))


class GithubProtected(unittest.TestCase):
    @staticmethod
    def http_error(code):
        return urllib.error.HTTPError("https://api.github.com/x", code, "x", {}, None)

    def test_empty_repo_branch_404_is_unprotected(self):
        # An empty GitHub repo (size 0) has no default branch: the branch API returns 404.
        with mock.patch.object(sw.ro, "_req", side_effect=self.http_error(404)):
            self.assertFalse(sw.github_protected("empty-repo", "main", "t"))

    def test_other_http_errors_stay_loud(self):
        with mock.patch.object(sw.ro, "_req", side_effect=self.http_error(500)):
            with self.assertRaises(urllib.error.HTTPError):
                sw.github_protected("repo", "main", "t")

    def test_protected_flag_is_read(self):
        with mock.patch.object(sw.ro, "_req", return_value={"protected": True}):
            self.assertTrue(sw.github_protected("repo", "main", "t"))


class Main(unittest.TestCase):
    def run_main(self, gh_list, fj_map, argv=("--no-seats",)):
        with mock.patch.object(sw.ro, "read_token", return_value="t"), \
             mock.patch.object(sw.ro, "github_repos", return_value=gh_list), \
             mock.patch.object(sw.ro, "forgejo_repos", return_value=fj_map), \
             mock.patch.object(sw, "github_protected", return_value=False), \
             mock.patch.object(sw, "load_skips", return_value=[]), \
             mock.patch("tools.mailbox.send") as send:
            rc = sw.main(list(argv))
        return rc, send

    def test_findings_are_mailed_to_the_steward(self):
        rc, send = self.run_main([gh("a")], {})
        self.assertEqual(rc, 0)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], sw.STEWARD_INBOX)
        self.assertIn("NO_COPY", send.call_args.args[1])

    def test_clean_run_and_dry_run_send_nothing(self):
        with mock.patch.object(sw.ro, "is_protected", return_value=False):
            self.assertFalse(self.run_main([gh("a")], {"a": fj()})[1].called)
        self.assertFalse(self.run_main([gh("a")], {}, ("--no-seats", "--dry-run"))[1].called)

    def test_inventory_failure_exits_1(self):
        with mock.patch.object(sw.ro, "read_token", side_effect=OSError("gone")):
            self.assertEqual(sw.main(["--no-seats"]), 1)


if __name__ == "__main__":
    unittest.main()
