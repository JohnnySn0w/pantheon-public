#!/usr/bin/env python3
"""Contract tests for tools/forgejo_mirror_verify.py (T-2217).

classify() is pure by design so the interesting cases are testable without a forge.
The cases that matter are the ones that produce FALSE ALARMS, because a verifier that
cries wolf gets disabled and then there is no verifier at all.

Run: python3 -m unittest tools.test_forgejo_mirror_verify -v
"""
import unittest
from datetime import datetime, timedelta, timezone

from tools import forgejo_mirror_verify as v

NOW = datetime(2026, 7, 28, 20, 0, 0, tzinfo=timezone.utc)


def repo(**kw):
    d = {"name": "r", "mirror": True, "mirror_interval": "24h0m0s",
         "mirror_updated": (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    d.update(kw)
    return d


def refs(n):
    return {f"sha{i} refs/heads/b{i}" for i in range(n)}


class TestParseInterval(unittest.TestCase):
    def test_parses_forgejo_format(self):
        self.assertEqual(v.parse_interval("24h0m0s"), 86400)
        self.assertEqual(v.parse_interval("8h30m0s"), 30600)
        self.assertEqual(v.parse_interval("45m0s"), 2700)

    def test_unparseable_is_zero_not_crash(self):
        self.assertEqual(v.parse_interval(""), 0)
        self.assertEqual(v.parse_interval("garbage"), 0)


class TestFalseAlarmCases(unittest.TestCase):
    """These are the ones that make a verifier get switched off."""

    def test_EMPTY_UPSTREAM_MIRRORED_AS_EMPTY_IS_OK(self):
        """data-mapping: zero refs on GitHub. An empty mirror is FAITHFUL, not broken.

        My first verifier called this VERIFY FAILED; it was correct all along.
        """
        verdict, detail = v.classify(repo(name="data-mapping"), set(), set(), now=NOW)
        self.assertEqual(verdict, v.OK, detail)

    def test_UNREADABLE_UPSTREAM_IS_UNKNOWN_NOT_DRIFT(self):
        """Private mirrors: recovery holds no GitHub credential. 40 false DRIFTs at once."""
        verdict, detail = v.classify(repo(name="secret"), None, refs(3), now=NOW)
        self.assertEqual(verdict, v.UNKNOWN)
        self.assertIn("upstream unreadable", detail)

    def test_unreadable_mirror_is_unknown_not_failure(self):
        verdict, _ = v.classify(repo(), refs(2), None, now=NOW)
        self.assertEqual(verdict, v.UNKNOWN)

    def test_fresh_sync_with_matching_refs_is_ok(self):
        verdict, detail = v.classify(repo(), refs(5), refs(5), now=NOW)
        self.assertEqual(verdict, v.OK)
        self.assertIn("5 refs match", detail)


class TestRealFailuresAreCaught(unittest.TestCase):
    def test_HOLLOW_created_but_never_pulled(self):
        """The exact failure the ticket names: a repo that exists and holds nothing."""
        verdict, detail = v.classify(repo(name="hollow"), refs(7), set(), now=NOW)
        self.assertEqual(verdict, v.HOLLOW)
        self.assertIn("never pulled", detail)

    def test_STALE_configured_but_not_syncing(self):
        old = (NOW - timedelta(hours=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
        verdict, detail = v.classify(repo(mirror_updated=old), refs(2), refs(2), now=NOW)
        self.assertEqual(verdict, v.STALE)
        self.assertIn("not syncing", detail)

    def test_stale_is_detected_even_when_refs_match(self):
        """Sync-liveness must not be masked by ref parity — a dead mirror of a dead repo."""
        old = (NOW - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        verdict, _ = v.classify(repo(mirror_updated=old), refs(1), refs(1), now=NOW)
        self.assertEqual(verdict, v.STALE)

    def test_DRIFT_missing_tags_despite_shared_head(self):
        """A mirror can share HEAD and be missing every tag — HEAD alone is not enough."""
        up = refs(3) | {"refs/tags/v1"}
        verdict, detail = v.classify(repo(), up, refs(3), now=NOW)
        self.assertEqual(verdict, v.DRIFT)
        self.assertIn("1 upstream ref(s) MISSING", detail)

    def test_extra_only_refs_are_ok_retained_not_drift(self):
        # T-2694: the mirror holds every upstream ref (0 missing) plus 2 it retained
        # after upstream deleted them (Forgejo pull-mirrors don't prune) — that is
        # NOT drift; only MISSING upstream data is. Alerting on retained refs would
        # cry wolf on every repo as branches churn (echo-prismatic kept a merged
        # agent branch + a closed-PR canary branch).
        verdict, detail = v.classify(repo(), refs(2), refs(4), now=NOW)
        self.assertEqual(verdict, v.OK)
        self.assertIn("retained", detail)

    def test_missing_and_extra_together_is_drift_on_the_missing(self):
        # Missing dominates: if the mirror lacks an upstream ref, it DRIFTs even if it
        # also retains extras. up has b0,b1,tag; mirror has b0,b1,other → tag missing.
        up = refs(2) | {"refs/tags/v1"}
        mir = refs(2) | {"refs/heads/retained-old"}
        verdict, detail = v.classify(repo(), up, mir, now=NOW)
        self.assertEqual(verdict, v.DRIFT)
        self.assertIn("MISSING", detail)
        self.assertIn("1 extra retained", detail)

    def test_non_mirror_repo_is_unknown(self):
        verdict, _ = v.classify(repo(mirror=False), refs(1), refs(1), now=NOW)
        self.assertEqual(verdict, v.UNKNOWN)


class TestThresholdIsDerivedNotHardcoded(unittest.TestCase):
    def test_short_interval_goes_stale_sooner_than_long_one(self):
        age = (NOW - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        short, _ = v.classify(repo(mirror_interval="1h0m0s", mirror_updated=age),
                              refs(1), refs(1), now=NOW)
        long_, _ = v.classify(repo(mirror_interval="24h0m0s", mirror_updated=age),
                              refs(1), refs(1), now=NOW)
        self.assertEqual(short, v.STALE, "1h interval, 6h old -> stale")
        self.assertEqual(long_, v.OK, "24h interval, 6h old -> fine")

    def test_unparseable_interval_skips_staleness_rather_than_false_alarming(self):
        old = (NOW - timedelta(days=99)).strftime("%Y-%m-%dT%H:%M:%SZ")
        verdict, _ = v.classify(repo(mirror_interval="", mirror_updated=old),
                                refs(1), refs(1), now=NOW)
        self.assertEqual(verdict, v.OK)


class TestRefSetNoneVsEmpty(unittest.TestCase):
    """None (unreadable) and set() (genuinely no refs) must never be conflated."""

    def _run(self, rc, out=""):
        def run(cmd, **kw):
            import types
            return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")
        return run

    def test_failure_returns_None(self):
        self.assertIsNone(v.ref_set("u", run=self._run(128)))

    def test_success_with_no_refs_returns_empty_set(self):
        got = v.ref_set("u", run=self._run(0, ""))
        self.assertEqual(got, set())
        self.assertIsNotNone(got, "empty upstream must be set(), never None")

    def test_parses_ref_lines_by_name_not_sha(self):
        # T-2694: ref_set returns ref NAMES (not sha+name) so a mirror that is a few
        # commits behind between syncs is not a false DRIFT — only added/removed
        # branches/tags differ. Two different SHAs for the same branch collapse to one.
        got = v.ref_set("u", run=self._run(
            0, "abc\trefs/heads/main\ndef\trefs/tags/v1\n"))
        self.assertEqual(got, {"refs/heads/main", "refs/tags/v1"})

    def test_moved_branch_same_name_is_not_a_diff(self):
        # The sync-lag case that motivated the by-name change: upstream main at a
        # newer sha than the mirror's main is the SAME ref name → parity holds.
        upstream = v.ref_set("u", run=self._run(0, "newsha\trefs/heads/main\n"))
        mirror = v.ref_set("u", run=self._run(0, "oldsha\trefs/heads/main\n"))
        self.assertEqual(upstream, mirror)


class TestReadGithubPat(unittest.TestCase):
    """T-2694: the Contents:read PAT reader — key=value, graceful on absence."""

    def _write(self, text):
        import os
        import tempfile
        f = tempfile.NamedTemporaryFile("w", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_parses_key_value_form(self):
        self.assertEqual(v.read_github_pat(self._write("gh_pat=github_pat_ABC\n")),
                         "github_pat_ABC")

    def test_plain_value_without_key(self):
        self.assertEqual(v.read_github_pat(self._write("rawtoken\n")), "rawtoken")

    def test_missing_file_is_none_not_error(self):
        # Graceful: a missing/rotated PAT degrades private repos to sync-liveness,
        # it must never raise and break the daily verify run.
        self.assertIsNone(v.read_github_pat("/nonexistent/pantheon/forgejo.tok"))


class TestPrivateUpstreamAuth(unittest.TestCase):
    """T-2694: a PRIVATE upstream is ls-remote'd with the PAT supplied via an
    env-scoped credential helper — NEVER on argv (ps-visible) or on disk."""

    def _capturing_run(self):
        captured = {}

        def run(cmd, **kw):
            import types
            captured["cmd"] = list(cmd)
            captured["env"] = kw.get("env")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return run, captured

    def test_pat_travels_via_env_never_argv(self):
        run, cap = self._capturing_run()
        v.ref_set("https://github.com/example-org/secret.git",
                  pat="github_pat_SECRET", run=run)
        joined = " ".join(cap["cmd"])
        self.assertNotIn("github_pat_SECRET", joined,
                         "the PAT must never appear on argv (visible to `ps`)")
        self.assertIn("credential.https://github.com.helper", joined,
                      "a github.com-scoped credential helper must be configured")
        self.assertEqual(cap["env"].get("PANTHEON_GH_PAT"), "github_pat_SECRET",
                         "the PAT is passed to the inline helper via env")

    def test_no_pat_injects_no_helper_and_no_env(self):
        run, cap = self._capturing_run()
        v.ref_set("https://github.com/example-org/pub.git", run=run)
        self.assertNotIn("credential.https://github.com.helper", " ".join(cap["cmd"]))
        self.assertIsNone(cap["env"], "no pat → inherit env, nothing injected")


class TestMainRoutesPatToPrivateOnly(unittest.TestCase):
    """T-2694: main() ls-remotes PRIVATE upstreams WITH the PAT, PUBLIC ones without."""

    def test_private_carries_pat_public_does_not(self):
        from unittest import mock
        calls = []

        def fake_ref_set(url, *, token=None, pat=None, run=None):
            calls.append((url, pat))
            return set()  # empty upstream + empty mirror → classify OK(empty-both)

        forge_repos = [repo(name="pub", private=False), repo(name="sec", private=True)]
        with mock.patch.object(v, "ref_set", side_effect=fake_ref_set), \
             mock.patch.object(v, "_api", return_value=forge_repos), \
             mock.patch.object(v, "read_github_pat", return_value="PAT123"), \
             mock.patch("builtins.open", mock.mock_open(read_data="forge-token")):
            v.main(["--quiet-ok"])
        upstream = {url: pat for (url, pat) in calls if "github.com" in url}
        self.assertEqual(upstream["https://github.com/example-org/sec.git"], "PAT123",
                         "private upstream must be authed with the PAT")
        self.assertIsNone(upstream["https://github.com/example-org/pub.git"],
                          "public upstream must NOT carry the PAT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
