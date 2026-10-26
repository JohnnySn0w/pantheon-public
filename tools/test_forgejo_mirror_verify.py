#!/usr/bin/env python3
"""Contract tests for tools/forgejo_mirror_verify.py (T-2217).

classify() is pure by design so the interesting cases are testable without a forge.
The cases that matter are the ones that produce FALSE ALARMS, because a verifier that
cries wolf gets disabled and then there is no verifier at all.

Run: python3 -m unittest tools.test_forgejo_mirror_verify -v
"""
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock
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
        # Missing dominates for a REAL gap: even with retained extras + a fresh sync,
        # a drift LARGER than the T-2879 transient-lag ceiling DRIFTs (it is not the
        # small new-ref lag the grace covers). up has b0,b1,+3 tags; mirror has
        # b0,b1,other → 3 tags missing (> TRANSIENT_LAG_MAX_MISSING).
        up = refs(2) | {"refs/tags/v1", "refs/tags/v2", "refs/tags/v3"}
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


class TestMirrorCredentialIsExplicit(unittest.TestCase):
    """T-5067: the mirror read is pantheon-mirror whatever the store file's line order."""
    URL = "https://pantheon-mirror@git.your-self-hosted-git.example/pantheon/pantheon-bridge.git"

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.cred = self.tmp / "creds"
        # fleet FIRST: the order a `git credential approve` as pantheon-fleet leaves behind
        self.cred.write_text("https://pantheon-fleet:FLEETPW@git.your-self-hosted-git.example\n"
                             "https://pantheon-mirror:MIRROR%2FPW@git.your-self-hosted-git.example\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _capture(self, url=None, token=None):
        captured = {}

        def run(cmd, **kw):
            import types
            captured["cmd"], captured["env"] = list(cmd), kw.get("env")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        got = v.ref_set(url or self.URL, token=str(token or self.cred), run=run)
        return got, captured

    def test_store_password_picks_by_user_not_order(self):
        self.assertEqual(v.store_password(str(self.cred), "git.your-self-hosted-git.example", "pantheon-mirror"), "MIRROR/PW")
        self.assertEqual(v.store_password(str(self.cred), "git.your-self-hosted-git.example", "pantheon-fleet"), "FLEETPW")
        self.assertIsNone(v.store_password(str(self.cred), "github.com", "pantheon-mirror"))

    def test_fleet_first_file_still_sends_the_mirror_password_via_env_only(self):
        _, cap = self._capture()
        joined = " ".join(cap["cmd"])
        self.assertEqual(cap["env"]["PANTHEON_FORGE_PW"], "MIRROR/PW")
        self.assertNotIn("MIRROR/PW", joined)
        self.assertNotIn("FLEETPW", joined)
        self.assertNotIn("store", joined, "the order-dependent store helper is gone")
        self.assertIn("credential.helper=", cap["cmd"])
        self.assertIn("credential.https://git.your-self-hosted-git.example.helper=", cap["cmd"])

    def test_no_matching_entry_is_unreadable_and_runs_nothing(self):
        self.cred.write_text("https://pantheon-fleet:FLEETPW@git.your-self-hosted-git.example\n")
        got, cap = self._capture()
        self.assertIsNone(got)
        self.assertEqual(cap, {})

    def test_real_git_resolves_pantheon_mirror_despite_an_inherited_host_helper(self):
        import shutil
        if not shutil.which("git"):
            self.skipTest("git not installed")
        home = self.tmp / "home"
        home.mkdir()
        (home / ".gitconfig").write_text(
            "[credential \"https://git.your-self-hosted-git.example\"]\n"
            f"\thelper = store --file={self.cred}\n")
        _, cap = self._capture()
        prefix = cap["cmd"][:cap["cmd"].index("ls-remote")]
        env = {**cap["env"], "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
               "GIT_CONFIG_NOSYSTEM": "1"}
        r = subprocess.run(prefix + ["credential", "fill"], input="protocol=https\nhost=git.your-self-hosted-git.example\n\n",
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("username=pantheon-mirror", r.stdout)
        self.assertIn("password=MIRROR/PW", r.stdout)
        self.assertNotIn("FLEETPW", r.stdout)


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
             mock.patch.object(v, "github_repo_private", return_value=None), \
             mock.patch("builtins.open", mock.mock_open(read_data="forge-token")):
            v.main(["--quiet-ok"])
        upstream = {url: pat for (url, pat) in calls if "github.com" in url}
        self.assertEqual(upstream["https://github.com/example-org/sec.git"], "PAT123",
                         "private upstream must be authed with the PAT")
        self.assertIsNone(upstream["https://github.com/example-org/pub.git"],
                          "public upstream must NOT carry the PAT")


class TestVisibilityParity(unittest.TestCase):
    """T-2881: flag ONLY the exposure direction — PUBLIC on forge + PRIVATE on github."""

    def test_public_mirror_private_upstream_is_EXPOSED(self):
        verdict, detail = v.classify(repo(name="leak", private=False), refs(3), refs(3),
                                     now=NOW, upstream_private=True)
        self.assertEqual(verdict, v.EXPOSED)
        self.assertIn("PUBLIC on forge but PRIVATE on github", detail)

    def test_public_mirror_public_upstream_is_not_exposed(self):
        verdict, _ = v.classify(repo(name="pub", private=False), refs(3), refs(3),
                                now=NOW, upstream_private=False)
        self.assertEqual(verdict, v.OK)

    def test_private_mirror_public_upstream_is_benign_over_privacy(self):
        # A private mirror of a public upstream is safe — over-privacy is never flagged.
        verdict, _ = v.classify(repo(name="over", private=True), refs(3), refs(3),
                                now=NOW, upstream_private=False)
        self.assertEqual(verdict, v.OK)

    def test_private_mirror_private_upstream_is_ok_not_exposed(self):
        verdict, _ = v.classify(repo(name="both-priv", private=True), refs(2), refs(2),
                                now=NOW, upstream_private=True)
        self.assertEqual(verdict, v.OK)

    def test_unreadable_upstream_visibility_is_not_flagged_failsafe(self):
        # None = GitHub visibility unreadable → never cry wolf; falls through to ref/sync.
        verdict, _ = v.classify(repo(name="unk", private=False), refs(2), refs(2),
                                now=NOW, upstream_private=None)
        self.assertEqual(verdict, v.OK)

    def test_exposure_outranks_ref_drift_on_same_repo(self):
        # A public-of-private mirror that is ALSO missing refs surfaces EXPOSED first
        # (the security signal); the ref check resumes next run once visibility is fixed.
        verdict, _ = v.classify(repo(name="both-bad", private=False), refs(5), refs(2),
                                now=NOW, upstream_private=True)
        self.assertEqual(verdict, v.EXPOSED)

    def test_exposure_outranks_stale(self):
        old = (NOW - timedelta(hours=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
        verdict, _ = v.classify(repo(name="stale-exposed", private=False, mirror_updated=old),
                                refs(2), refs(2), now=NOW, upstream_private=True)
        self.assertEqual(verdict, v.EXPOSED)

    def test_default_upstream_private_none_preserves_legacy_behavior(self):
        # Omitting the new kwarg must not change any pre-T-2881 verdict.
        verdict, _ = v.classify(repo(name="legacy", private=False), refs(3), refs(3), now=NOW)
        self.assertEqual(verdict, v.OK)


class TestGithubRepoPrivate(unittest.TestCase):
    """T-2881: the read-only GitHub visibility GET — graceful None on any unreadable."""

    def _opener(self, payload=None, exc=None):
        import io

        def opener(req, timeout=None):
            if exc is not None:
                raise exc
            # capture the request for header assertions
            self._req = req
            return io.BytesIO(json.dumps(payload).encode())
        return opener

    def test_returns_true_for_private_upstream(self):
        got = v.github_repo_private("secret", "PAT", opener=self._opener({"private": True}))
        self.assertIs(got, True)

    def test_returns_false_for_public_upstream(self):
        got = v.github_repo_private("open", "PAT", opener=self._opener({"private": False}))
        self.assertIs(got, False)

    def test_no_pat_is_none_no_call(self):
        # No PAT → None WITHOUT any network attempt (opener that would explode if called).
        def boom(req, timeout=None):
            raise AssertionError("must not open a connection without a PAT")
        self.assertIsNone(v.github_repo_private("x", None, opener=boom))

    def test_http_error_is_none_failsafe(self):
        import urllib.error
        err = urllib.error.HTTPError("u", 404, "nf", {}, None)
        self.assertIsNone(v.github_repo_private("gone", "PAT", opener=self._opener(exc=err)))

    def test_missing_private_field_is_none(self):
        self.assertIsNone(v.github_repo_private("weird", "PAT", opener=self._opener({"name": "x"})))

    def test_uses_bearer_and_github_accept_header(self):
        v.github_repo_private("r", "PAT9", opener=self._opener({"private": True}))
        self.assertEqual(self._req.get_header("Authorization"), "Bearer PAT9")
        self.assertEqual(self._req.get_header("Accept"), "application/vnd.github+json")
        self.assertIn("api.github.com/repos/example-org/r", self._req.full_url)


class TestTransientLagGrace(unittest.TestCase):
    """T-2879: a mirror syncing ON SCHEDULE, showing active churn (retained
    upstream-deleted refs), missing only a SMALL number of refs = transient
    new-ref lag → WARN (surfaced, not a bad/exit-1 alert). A quiet repo, a stale
    mirror, or a large drift still DRIFTs. This is the echo-prismatic case that
    daily-alerted on benign lag."""

    def _churning(self, missing_n, extra_n, *, age_h=1, interval="24h0m0s"):
        # A repo with `extra_n` retained (churn) and `missing_n` upstream refs the
        # mirror lacks. up = base ∪ missing-tags; mir = base ∪ retained-extras.
        base = refs(3)
        up = base | {f"refs/heads/new{i}" for i in range(missing_n)}
        mir = base | {f"refs/heads/retained{i}" for i in range(extra_n)}
        updated = (NOW - timedelta(hours=age_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return v.classify(repo(mirror_interval=interval, mirror_updated=updated),
                          up, mir, now=NOW)

    def test_fresh_churn_small_missing_is_WARN_not_drift(self):
        # The echo-prismatic case: 1 new ref not yet pulled, 2 branches retained,
        # synced 1h ago on a 24h interval → transient lag, WARN.
        verdict, detail = self._churning(missing_n=1, extra_n=2)
        self.assertEqual(verdict, v.WARN, detail)
        self.assertIn("transient new-ref lag", detail)
        self.assertIn("self-resolves", detail)

    def test_WARN_at_the_ceiling_but_DRIFT_above_it(self):
        # Boundary: missing == ceiling → WARN; ceiling+1 → DRIFT (a bigger gap is
        # not benign lag even under active churn).
        at, _ = self._churning(missing_n=v.TRANSIENT_LAG_MAX_MISSING, extra_n=1)
        over, _ = self._churning(missing_n=v.TRANSIENT_LAG_MAX_MISSING + 1, extra_n=1)
        self.assertEqual(at, v.WARN)
        self.assertEqual(over, v.DRIFT)

    def test_quiet_repo_no_churn_still_DRIFTs_on_any_missing(self):
        # No retained extras = no active-churn signal → a missing ref is a real gap
        # even on a fresh sync. The ticket's explicit guard: quiet repos still alert.
        verdict, detail = self._churning(missing_n=1, extra_n=0)
        self.assertEqual(verdict, v.DRIFT, detail)
        self.assertIn("MISSING", detail)

    def test_stale_mirror_with_small_churn_drift_is_STALE_not_WARN(self):
        # Sync staleness (tier 1) is untouched and outranks the grace: a mirror that
        # is NOT syncing on schedule is a real problem regardless of churn.
        verdict, detail = self._churning(missing_n=1, extra_n=2, age_h=100)
        self.assertEqual(verdict, v.STALE, detail)

    def test_overdue_but_not_stale_sync_does_not_get_the_grace(self):
        # Age between the interval and the 2.5x STALE threshold is NOT "on schedule"
        # (a pull was missed) → no grace → DRIFT, even with churn + small missing.
        verdict, detail = self._churning(missing_n=1, extra_n=2, age_h=30)  # >24h, <60h
        self.assertEqual(verdict, v.DRIFT, detail)

    def test_WARN_does_not_count_as_a_bad_verdict(self):
        # The whole point: WARN must not fail the run. main() counts only
        # DRIFT/STALE/HOLLOW/EXPOSED as bad; assert WARN is excluded from that set.
        self.assertNotIn(v.WARN, (v.DRIFT, v.STALE, v.HOLLOW, v.EXPOSED))


class TestStateBasedGrace(unittest.TestCase):
    """T-3114: the state-based 1-cycle grace. A missing ref is transient (WARN) until it
    SURVIVES a sync (mirror_updated advanced, ref still absent) → real gap (DRIFT). Handles
    the pure-ADDITION churn case T-2879's stateless grace cannot (no retained-extra signal)."""

    S1 = "2026-08-24T00:00:00Z"
    S2 = "2026-08-24T12:00:00Z"  # a later mirror_updated = a sync ran between runs

    def test_first_observation_of_missing_is_WARN_fresh_grace(self):
        # No prior state → a newly-missing ref gets its 1-cycle grace even though a sync
        # stamp exists (it hasn't been observed-then-survived yet).
        v_, d, entry = v.apply_transient_grace(
            "add", v.DRIFT, "add: 1 MISSING", missing={"b2"}, mirror_updated=self.S1, prev=None)
        self.assertEqual(v_, v.WARN, d)
        self.assertEqual(entry, {"mirror_updated": self.S1, "missing": ["b2"]})

    def test_missing_ref_that_survived_a_sync_is_DRIFT(self):
        prev = {"mirror_updated": self.S1, "missing": ["b2"]}
        v_, d, _ = v.apply_transient_grace(
            "add", v.DRIFT, "add: 1 MISSING", missing={"b2"}, mirror_updated=self.S2, prev=prev)
        self.assertEqual(v_, v.DRIFT)
        self.assertIn("survived a sync", d)

    def test_missing_ref_no_sync_since_stays_WARN(self):
        # Same missing ref, but mirror_updated UNCHANGED → no sync ran → still in grace.
        prev = {"mirror_updated": self.S1, "missing": ["b2"]}
        v_, _, _ = v.apply_transient_grace(
            "add", v.DRIFT, "add: 1 MISSING", missing={"b2"}, mirror_updated=self.S1, prev=prev)
        self.assertEqual(v_, v.WARN)

    def test_new_missing_ref_after_a_sync_gets_its_own_grace(self):
        # Prior gap resolved; a DIFFERENT ref is missing now → transient (its first cycle).
        prev = {"mirror_updated": self.S1, "missing": ["b2"]}
        v_, _, _ = v.apply_transient_grace(
            "add", v.DRIFT, "add: 1 MISSING", missing={"b9"}, mirror_updated=self.S2, prev=prev)
        self.assertEqual(v_, v.WARN)

    def test_partial_some_survived_some_new_is_DRIFT(self):
        prev = {"mirror_updated": self.S1, "missing": ["b2"]}
        v_, d, _ = v.apply_transient_grace(
            "add", v.DRIFT, "add: 2 MISSING", missing={"b2", "b9"}, mirror_updated=self.S2, prev=prev)
        self.assertEqual(v_, v.DRIFT)
        self.assertIn("1 upstream ref", d)  # only the survived-a-sync one counts as real

    def test_no_mirror_updated_stamp_keeps_DRIFT(self):
        # Cannot reason about 'survived a sync' without a stamp → do NOT silence (keep DRIFT).
        prev = {"mirror_updated": self.S1, "missing": ["b2"]}
        v_, _, _ = v.apply_transient_grace(
            "add", v.DRIFT, "add: 1 MISSING", missing={"b2"}, mirror_updated=None, prev=prev)
        self.assertEqual(v_, v.DRIFT)

    def test_non_drift_passes_through_but_records_missing(self):
        # A T-2879 WARN (or OK) is untouched; its current missing-set is still persisted.
        v_, d, entry = v.apply_transient_grace(
            "churn", v.WARN, "churn: transient", missing={"b2"}, mirror_updated=self.S2,
            prev={"mirror_updated": self.S1, "missing": ["b2"]})
        self.assertEqual((v_, d), (v.WARN, "churn: transient"))
        self.assertEqual(entry["missing"], ["b2"])

    def test_state_roundtrip_and_failopen(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "sub", "state.json")
            self.assertEqual(v.load_state(p), {})  # missing file → {} (fail-open)
            st = {"add": {"mirror_updated": self.S1, "missing": ["b2"]}}
            v.save_state(p, st)
            self.assertEqual(v.load_state(p), st)
            open(p, "w").write("{ not json")  # malformed → {} (fail-open)
            self.assertEqual(v.load_state(p), {})

    def test_main_pure_addition_lag_is_WARN_first_then_DRIFT_after_sync(self):
        from unittest import mock
        # The class-level self.S1/self.S2 are hard-coded 2026-08-24 stamps and
        # work for the pure-function apply_transient_grace tests (which
        # receive a pinned NOW). This test drives v.main(), which reads
        # datetime.now(timezone.utc) live — so as real time drifts past the
        # 24h mirror_interval since S1, the pre-classify STALE gate fires
        # first and the transient-grace path never runs (rc=1 instead of 0,
        # T-3547 class 5). Rebase the two stamps onto real-now so the
        # freshness window holds regardless of when the suite runs.
        now = datetime.now(timezone.utc)
        s1 = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        s2 = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        up_refs = refs(3)
        mir_refs = set(list(up_refs)[:2])  # missing exactly 1, NO extra retained (pure-addition)
        missing_sorted = sorted(up_refs - mir_refs)

        def fake_ref_set(url, *, token=None, pat=None, run=None):
            return up_refs if "github.com" in url else mir_refs

        saved = {}

        def fake_save(path, state):
            saved.clear()
            saved.update(state)

        common = dict(read_github_pat=None, github_repo_private=None)
        # Run 1: no prior state → WARN (fresh grace), run must not fail.
        r1 = repo(name="add", private=False, mirror_updated=s1)
        with mock.patch.object(v, "ref_set", side_effect=fake_ref_set), \
             mock.patch.object(v, "_api", return_value=[r1]), \
             mock.patch.object(v, "read_github_pat", return_value=None), \
             mock.patch.object(v, "github_repo_private", return_value=None), \
             mock.patch.object(v, "load_state", return_value={}), \
             mock.patch.object(v, "save_state", side_effect=fake_save), \
             mock.patch("builtins.open", mock.mock_open(read_data="forge-token")):
            rc1 = v.main(["--quiet-ok"])
        self.assertEqual(rc1, 0, "pure-addition transient lag must WARN, not fail the run")
        self.assertEqual(saved["add"]["missing"], missing_sorted)

        # Run 2: same ref still missing, but mirror_updated ADVANCED (a sync ran) → DRIFT → exit 1.
        r2 = repo(name="add", private=False, mirror_updated=s2)
        prev = {"add": dict(saved["add"])}
        with mock.patch.object(v, "ref_set", side_effect=fake_ref_set), \
             mock.patch.object(v, "_api", return_value=[r2]), \
             mock.patch.object(v, "read_github_pat", return_value=None), \
             mock.patch.object(v, "github_repo_private", return_value=None), \
             mock.patch.object(v, "load_state", return_value=prev), \
             mock.patch.object(v, "save_state", side_effect=fake_save), \
             mock.patch("builtins.open", mock.mock_open(read_data="forge-token")):
            rc2 = v.main(["--quiet-ok"])
        self.assertEqual(rc2, 1, "a ref still missing after a sync is a real gap → DRIFT → exit 1")


def fresh(**kw):
    """A mirror that synced a minute ago by the REAL clock (main() compares against now)."""
    return repo(mirror_updated=(datetime.now(timezone.utc) - timedelta(minutes=1))
                .strftime("%Y-%m-%dT%H:%M:%SZ"), **kw)


class TestBlindVerifier(unittest.TestCase):
    """T-4690: the verifier was blind for days (UNKNOWN=50, exit 0) for three reasons."""

    def _main(self, api, ref_set):
        with mock.patch.object(v, "ref_set", side_effect=ref_set), \
             mock.patch.object(v, "_api", side_effect=api), \
             mock.patch.object(v, "read_github_pat", return_value=None), \
             mock.patch.object(v, "github_repo_private", return_value=None), \
             mock.patch.object(v, "load_state", return_value={}), \
             mock.patch.object(v, "save_state"), \
             mock.patch("builtins.open", mock.mock_open(read_data="forge-token")):
            return v.main(["--quiet-ok"])

    def test_unreadable_mirror_fails_the_run(self):
        # Our own credential failing must be loud, not UNKNOWN-and-exit-0.
        rc = self._main(lambda path, tok: [fresh(name="a", private=False)],
                        lambda url, **kw: None if "git.your-self-hosted-git.example" in url else {"x refs/heads/main"})
        self.assertEqual(rc, 1)

    def test_unreadable_upstream_alone_stays_benign(self):
        rc = self._main(lambda path, tok: [fresh(name="a", private=True)],
                        lambda url, **kw: None if "github.com" in url else {"x refs/heads/main"})
        self.assertEqual(rc, 0)

    def test_mirror_url_names_the_mirror_identity(self):
        seen = []
        def ref_set(url, **kw):
            seen.append(url)
            return {"x refs/heads/main"}
        self._main(lambda path, tok: [fresh(name="a", private=False)], ref_set)
        self.assertIn("https://pantheon-mirror@git.your-self-hosted-git.example/github-mirror/a.git", seen)

    def test_paginates_past_fifty(self):
        pages = {1: [fresh(name=f"r{i}", private=False) for i in range(50)],
                 2: [fresh(name="r50", private=False)]}
        def api(path, tok):
            page = int(path.rsplit("page=", 1)[1])
            return pages.get(page, [])
        names = []
        def ref_set(url, **kw):
            names.append(url)
            return {"x refs/heads/main"}
        self.assertEqual(self._main(api, ref_set), 0)
        self.assertTrue(any(u.endswith("/r50.git") and "git.your-self-hosted-git.example" in u for u in names),
                        "repo 51 (page 2) was never checked")

    def test_pager_that_never_ends_is_bounded_and_loud(self):
        calls = []
        def api(path, tok):
            calls.append(path)
            if len(calls) > v.MAX_PAGES + 5:
                raise AssertionError("pager did not stop")
            return [fresh(name="same", private=False)] * v.PAGE
        self.assertEqual(self._main(api, lambda url, **kw: {"x refs/heads/main"}), 65)
        self.assertEqual(len(calls), v.MAX_PAGES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
