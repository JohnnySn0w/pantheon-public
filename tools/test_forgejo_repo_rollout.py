#!/usr/bin/env python3
"""Tests for tools/forgejo_repo_rollout.py (T-4690 leg 3): the plan is pure, so test it directly."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.forgejo_repo_rollout as ro


def gh(name, private=True, owner="example-org", archived=False):
    return {"name": name, "private": private, "owner": {"login": owner}, "archived": archived}


class Plan(unittest.TestCase):
    def test_states(self):
        items = {i.name: i for i in ro.plan(
            [gh("a"), gh("b"), gh("c")],
            {"b": {"mirror": True, "private": True}, "c": {"mirror": False, "private": True}}, None)}
        self.assertEqual((items["a"].state, items["b"].state, items["c"].state),
                         (ro.MISSING, ro.MIRROR, ro.REGULAR))

    def test_only_exposure_direction_is_flagged(self):
        items = {i.name: i for i in ro.plan(
            [gh("leak", private=True), gh("safe", private=False)],
            {"leak": {"mirror": True, "private": False}, "safe": {"mirror": True, "private": True}}, None)}
        self.assertTrue(items["leak"].exposure)
        self.assertFalse(items["safe"].exposure)

    def test_only_filter(self):
        self.assertEqual([i.name for i in ro.plan([gh("a"), gh("b")], {}, ["b"])], ["b"])

    def test_dry_run_makes_no_calls(self):
        with mock.patch.object(ro, "github_repos", return_value=[gh("new")]), \
             mock.patch.object(ro, "forgejo_repos", return_value={}), \
             mock.patch.object(ro, "read_token", return_value="t"), \
             mock.patch.object(ro, "create_mirror") as create:
            self.assertEqual(ro.main([]), 0)
        create.assert_not_called()

    def test_apply_creates_with_github_visibility_and_continues_after_failure(self):
        calls = []

        def create(item, fj, ghr):
            calls.append((item.name, item.github_private))
            if item.name == "bad":
                raise OSError("api")
        with mock.patch.object(ro, "github_repos", return_value=[gh("bad"), gh("pub", private=False)]), \
             mock.patch.object(ro, "forgejo_repos", return_value={}), \
             mock.patch.object(ro, "read_token", return_value="t"), \
             mock.patch.object(ro, "create_mirror", side_effect=create):
            self.assertEqual(ro.main(["--apply"]), 1)
        self.assertEqual(calls, [("bad", True), ("pub", False)])


class Convert(unittest.TestCase):
    """--convert: only an in-sync, non-archived pull-mirror converts; protection follows."""

    def run_main(self, repos, fj, heads, argv, protected=()):
        log = []
        with mock.patch.object(ro, "github_repos", return_value=repos), \
             mock.patch.object(ro, "forgejo_repos", return_value=fj), \
             mock.patch.object(ro, "read_token", return_value="t"), \
             mock.patch.object(ro, "heads", side_effect=lambda it, g, f: heads[it.name]), \
             mock.patch.object(ro, "is_protected", side_effect=lambda it, f: it.name in protected), \
             mock.patch.object(ro, "convert", side_effect=lambda it, f: log.append(("convert", it.name))), \
             mock.patch.object(ro, "protect",
                               side_effect=lambda it, f: log.append(("protect", it.name, it.default_branch))), \
             mock.patch.object(ro, "mirror_sync", side_effect=lambda it, f: log.append(("sync", it.name))), \
             mock.patch.object(ro, "create_mirror") as create:
            rc = ro.main(argv)
        create.assert_not_called()
        return rc, log

    def test_in_sync_converts_and_protects_default_branch(self):
        repos = [dict(gh("a"), default_branch="trunk")]
        rc, log = self.run_main(repos, {"a": {"mirror": True, "private": True}},
                                {"a": ("abc", "abc")}, ["--convert", "--apply"])
        self.assertEqual(rc, 0)
        self.assertEqual(log, [("convert", "a"), ("protect", "a", "trunk")])

    def test_behind_copy_is_synced_not_converted(self):
        rc, log = self.run_main([gh("a")], {"a": {"mirror": True, "private": True}},
                                {"a": ("new", "old")}, ["--convert", "--apply"])
        self.assertEqual((rc, log), (0, [("sync", "a")]))

    def test_empty_repo_on_both_sides_converts(self):
        rc, log = self.run_main([gh("a")], {"a": {"mirror": True, "private": True}},
                                {"a": (None, None)}, ["--convert", "--apply"])
        self.assertEqual(log, [("convert", "a"), ("protect", "a", "main")])

    def test_archived_and_missing_are_left_alone(self):
        rc, log = self.run_main([gh("arch", archived=True), gh("gone")],
                                {"arch": {"mirror": True, "private": True}}, {}, ["--convert", "--apply"])
        self.assertEqual((rc, log), (0, []))

    def test_unprotected_regular_copy_is_repaired_protected_one_left(self):
        rc, log = self.run_main([gh("half"), gh("done")],
                                {"half": {"mirror": False, "private": True},
                                 "done": {"mirror": False, "private": True}}, {},
                                ["--convert", "--apply"], protected=("done",))
        self.assertEqual(log, [("protect", "half", "main")])

    def test_limit_caps_conversions(self):
        fj = {n: {"mirror": True, "private": True} for n in "abc"}
        rc, log = self.run_main([gh("a"), gh("b"), gh("c")], fj,
                                {n: ("x", "x") for n in "abc"}, ["--convert", "--apply", "--limit", "1"])
        self.assertEqual([e for e in log if e[0] == "convert"], [("convert", "a")])

    def test_dry_run_mutates_nothing(self):
        rc, log = self.run_main([gh("a"), gh("b"), gh("r")],
                                {"a": {"mirror": True, "private": True}, "b": {"mirror": True, "private": True},
                                 "r": {"mirror": False, "private": True}},
                                {"a": ("x", "x"), "b": ("y", "z")}, ["--convert"])
        self.assertEqual((rc, log), (0, []))

    def test_failure_is_reported_and_next_repo_still_runs(self):
        repos = [gh("bad"), gh("ok")]
        fj = {n: {"mirror": True, "private": True} for n in ("bad", "ok")}
        hs = {"bad": ("x", "x"), "ok": ("y", "y")}
        with mock.patch.object(ro, "convert", side_effect=[OSError("api"), None]) as conv, \
             mock.patch.object(ro, "github_repos", return_value=repos), \
             mock.patch.object(ro, "forgejo_repos", return_value=fj), \
             mock.patch.object(ro, "read_token", return_value="t"), \
             mock.patch.object(ro, "heads", side_effect=lambda it, g, f: hs[it.name]), \
             mock.patch.object(ro, "protect") as prot:
            self.assertEqual(ro.main(["--convert", "--apply"]), 1)
        self.assertEqual(conv.call_count, 2)
        self.assertEqual([c.args[0].name for c in prot.call_args_list], ["ok"])

    def test_limit_without_convert_is_refused(self):
        with self.assertRaises(SystemExit):
            ro.main(["--limit", "1"])

    def test_protection_allowlists_only_the_reconciler(self):
        self.assertEqual(ro.PROTECTION["push_whitelist_usernames"], ["pantheon-mirror"])
        self.assertFalse(ro.PROTECTION["push_whitelist_deploy_keys"])
        self.assertTrue(ro.PROTECTION["apply_to_admins"])


class ReadToken(unittest.TestCase):
    def test_raw_key_value_and_padding(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t"
            p.write_text("gh_pat=github_pat_X\n")
            self.assertEqual(ro.read_token(p), "github_pat_X")
            p.write_text("abc==\n")
            self.assertEqual(ro.read_token(p), "abc==")
            p.write_text("rawtoken\n")
            self.assertEqual(ro.read_token(p), "rawtoken")


if __name__ == "__main__":
    unittest.main()
