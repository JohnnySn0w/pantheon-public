#!/usr/bin/env python3
"""Tests for tools/git_reconciler.py (T-4690, ORG-101 §3).

Hermetic but REAL git: two local bare repos stand in for GitHub and Forgejo, so ancestry,
fast-forward pushes and server-side non-ff refusal are git's own behavior, not a mock's.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.git_reconciler as gr

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def hermetic_git(argv, cwd, env):
    # Same runner shape as production, minus the operator's global config/hooks.
    return subprocess.run(["git", *argv], cwd=cwd, env={**env, **{k: ENV[k] for k in (
        "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}}, capture_output=True, text=True, timeout=60)


def offline_git(argv, cwd, env):
    """Inventory-mode runs build real github.com / git.your-self-hosted-git.example urls: never touch the network.
    Every ls-remote answers 'branch absent', so a repo reads OK (absent on both)."""
    return subprocess.CompletedProcess(argv, 0, "", "")


class Hosts(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.gh = self.root / "github.git"
        self.fj = self.root / "forgejo.git"
        self.cache = self.root / "cache.git"
        for bare in (self.gh, self.fj, self.cache):
            git("init", "--quiet", "--bare", "-b", "main", str(bare), cwd=self.root)
        self.work = self.root / "work"
        git("init", "--quiet", "-b", "main", str(self.work), cwd=self.root)
        self.base = self.commit("base")
        self.push_to(self.gh, self.base)
        self.push_to(self.fj, self.base)
        self.synced = []

    def tearDown(self):
        self._td.cleanup()

    def commit(self, msg, parent=None):
        if parent:
            git("checkout", "--quiet", "--detach", parent, cwd=self.work)
        (self.work / "f").write_text(msg)
        git("add", "f", cwd=self.work)
        git("commit", "--quiet", "-m", msg, cwd=self.work)
        return git("rev-parse", "HEAD", cwd=self.work)

    def push_to(self, bare, sha, branch="main"):
        git("push", "--quiet", "--force", str(bare), f"{sha}:refs/heads/{branch}", cwd=self.work)

    def head(self, bare, branch="main"):
        r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                           cwd=bare, env=ENV, capture_output=True, text=True)
        return r.stdout.strip() or None

    def reconcile(self, *, gh_push=True, mirror=False, dry_run=False, branch="main", gh_url=None):
        env = dict(os.environ)
        github = gr.Side("github", gh_url or str(self.gh), env, can_push=gh_push,
                         push_blocker="" if gh_push else "no GitHub write credential")
        forgejo = gr.Side("forgejo", str(self.fj), env, can_push=not mirror,
                          push_blocker="still a pull-mirror" if mirror else "")
        return gr.reconcile_branch(
            repo="r", branch=branch, github=github, forgejo=forgejo, cache=self.cache,
            git=hermetic_git, forgejo_is_mirror=mirror,
            mirror_sync=lambda: self.synced.append(True), dry_run=dry_run)

    # --- the five states ---------------------------------------------------------------

    def test_equal_is_ok_and_changes_nothing(self):
        r = self.reconcile()
        self.assertEqual(r.status, gr.OK)
        self.assertEqual(self.head(self.fj), self.base)

    def test_forgejo_behind_is_fast_forwarded(self):
        new = self.commit("pr merge on github")
        self.push_to(self.gh, new)
        r = self.reconcile()
        self.assertEqual(r.status, gr.HEALED, r.detail)
        self.assertEqual(self.head(self.fj), new)

    def test_github_behind_is_replayed(self):
        new = self.commit("outage-time push to forgejo")
        self.push_to(self.fj, new)
        r = self.reconcile()
        self.assertEqual(r.status, gr.HEALED, r.detail)
        self.assertEqual(self.head(self.gh), new)

    def test_github_behind_without_credential_is_pending_not_pushed(self):
        new = self.commit("outage-time push")
        self.push_to(self.fj, new)
        r = self.reconcile(gh_push=False)
        self.assertEqual(r.status, gr.REPLAY_PENDING)
        self.assertEqual(self.head(self.gh), self.base)  # untouched

    def test_divergence_alerts_and_changes_nothing(self):
        a = self.commit("github side", parent=self.base)
        self.push_to(self.gh, a)
        b = self.commit("forgejo side", parent=self.base)
        self.push_to(self.fj, b)
        r = self.reconcile()
        self.assertEqual(r.status, gr.DIVERGED)
        self.assertEqual((self.head(self.gh), self.head(self.fj)), (a, b))

    # --- pull-mirror phase -------------------------------------------------------------

    def test_mirror_behind_triggers_sync_not_push(self):
        new = self.commit("merge")
        self.push_to(self.gh, new)
        r = self.reconcile(mirror=True)
        self.assertEqual(r.status, gr.HEALED)
        self.assertEqual(self.synced, [True])
        self.assertEqual(self.head(self.fj), self.base)  # the forge pulls; we never push

    def test_mirror_ahead_is_diverged(self):
        new = self.commit("impossible")
        self.push_to(self.fj, new)
        r = self.reconcile(mirror=True)
        self.assertEqual(r.status, gr.DIVERGED)
        self.assertEqual(self.head(self.gh), self.base)

    # --- edges -------------------------------------------------------------------------

    def test_missing_branch_is_created(self):
        new = self.commit("feature")
        self.push_to(self.gh, new, branch="rel")
        r = self.reconcile(branch="rel")
        self.assertEqual(r.status, gr.HEALED, r.detail)
        self.assertEqual(self.head(self.fj, "rel"), new)

    def test_unreachable_host_is_error_not_absent(self):
        # An unreadable host must never read as "branch absent" (which would trigger a create).
        r = self.reconcile(gh_url=str(self.root / "nope.git"))
        self.assertEqual(r.status, gr.ERROR)
        self.assertEqual(self.head(self.fj), self.base)

    def test_dry_run_changes_nothing(self):
        new = self.commit("merge")
        self.push_to(self.gh, new)
        r = self.reconcile(dry_run=True)
        self.assertEqual(r.status, gr.HEALED)
        self.assertIn("dry-run", r.detail)
        self.assertEqual(self.head(self.fj), self.base)

    def test_push_never_forces_even_if_classification_were_wrong(self):
        # Defense in depth: _push itself must be refused by the server on a non-ff.
        a = self.commit("github side", parent=self.base)
        self.push_to(self.gh, a)
        b = self.commit("forgejo side", parent=self.base)
        self.push_to(self.fj, b)
        side = gr.Side("forgejo", str(self.fj), dict(os.environ), can_push=True)
        git("fetch", "--quiet", str(self.gh), "main:refs/github/main", cwd=self.cache)
        with self.assertRaises(RuntimeError):
            gr._push(hermetic_git, self.cache, side, a, "main")
        self.assertEqual(self.head(self.fj), b)


    def test_archived_github_behind_is_skipped_not_pushed(self):
        new = self.commit("forgejo ahead of an archived repo")
        self.push_to(self.fj, new)
        env = dict(os.environ)
        r = gr.reconcile_branch(
            repo="r", branch="main", github=gr.Side("github", str(self.gh), env, False, "archived on GitHub"),
            forgejo=gr.Side("forgejo", str(self.fj), env, True), cache=self.cache, git=hermetic_git,
            forgejo_is_mirror=False, mirror_sync=lambda: None, dry_run=False, github_archived=True)
        self.assertEqual(r.status, gr.SKIPPED)
        self.assertNotIn(gr.SKIPPED, gr.BAD)
        self.assertEqual(self.head(self.gh), self.base)

    def test_archived_forgejo_behind_still_heals(self):
        new = self.commit("last commit before archiving")
        self.push_to(self.gh, new)
        env = dict(os.environ)
        r = gr.reconcile_branch(
            repo="r", branch="main", github=gr.Side("github", str(self.gh), env, False, "archived on GitHub"),
            forgejo=gr.Side("forgejo", str(self.fj), env, True), cache=self.cache, git=hermetic_git,
            forgejo_is_mirror=False, mirror_sync=lambda: None, dry_run=False, github_archived=True)
        self.assertEqual(r.status, gr.HEALED)
        self.assertEqual(self.head(self.fj), new)

    def test_github_missing_branch_is_created_on_github(self):
        new = self.commit("forgejo-only branch")
        self.push_to(self.fj, new, branch="rel")
        r = self.reconcile(branch="rel")
        self.assertEqual(r.status, gr.HEALED, r.detail)
        self.assertEqual(self.head(self.gh, "rel"), new)

    def test_absent_on_both_is_ok(self):
        r = self.reconcile(branch="nope")
        self.assertEqual((r.status, r.detail), (gr.OK, "absent on both"))

    def test_mirror_sync_raising_is_error_not_crash(self):
        new = self.commit("merge")
        self.push_to(self.gh, new)
        env = dict(os.environ)
        def boom():
            raise OSError("api down")
        r = gr.reconcile_branch(
            repo="r", branch="main", github=gr.Side("github", str(self.gh), env, True),
            forgejo=gr.Side("forgejo", str(self.fj), env, False, "still a pull-mirror"),
            cache=self.cache, git=hermetic_git, forgejo_is_mirror=True, mirror_sync=boom,
            dry_run=False)
        self.assertEqual(r.status, gr.ERROR)
        self.assertIn("mirror-sync failed", r.detail)

    def test_hung_git_is_error_not_abort(self):
        # Review finding 2: a TimeoutExpired escaping reconcile_branch aborted the whole run.
        def hung(argv, cwd, env):
            raise subprocess.TimeoutExpired(cmd=["git", *argv], timeout=120)
        r = gr.reconcile_branch(
            repo="r", branch="main", github=gr.Side("github", str(self.gh), {}, True),
            forgejo=gr.Side("forgejo", str(self.fj), {}, True), cache=self.cache, git=hung,
            forgejo_is_mirror=False, mirror_sync=lambda: None, dry_run=False)
        self.assertEqual(r.status, gr.ERROR)

    def test_default_git_converts_timeout_and_oserror(self):
        with mock.patch.object(gr.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(cmd="git", timeout=1)):
            self.assertEqual(gr._default_git(["ls-remote"], self.root, {}).returncode, 124)
        with mock.patch.object(gr.subprocess, "run", side_effect=OSError("no git")):
            self.assertEqual(gr._default_git(["ls-remote"], self.root, {}).returncode, 127)


class Inventory(unittest.TestCase):
    def test_mapping_is_pinned_and_unsafe_names_dropped(self):
        ts = gr.inventory_targets([
            {"name": "ok-repo", "default_branch": "master", "archived": True},
            {"name": "../etc", "default_branch": "main"},
            {"name": "a/b", "default_branch": "main"},
            {"name": "..", "default_branch": "main"},
        ])
        self.assertEqual([t.name for t in ts], ["ok-repo"])
        t = ts[0]
        self.assertEqual(t.github_url, "https://github.com/example-org/ok-repo.git")
        self.assertEqual(t.forgejo_url, "https://git.your-self-hosted-git.example/github-mirror/ok-repo.git")
        self.assertEqual((t.default_branch, t.archived, t.source), ("master", True, "inventory"))

    def test_github_inventory_filters_owner_and_paginates(self):
        import io, json as _json
        pages = [[{"name": "a", "owner": {"login": "example-org"}},
                  {"name": "x", "owner": {"login": "someone-else"}}], []]
        def opener(req, timeout):
            return io.BytesIO(_json.dumps(pages.pop(0)).encode())
        self.assertEqual([r["name"] for r in gr.github_inventory("t", opener=opener)], ["a"])

    def test_github_inventory_page_cap(self):
        import io, json as _json
        def opener(req, timeout):  # a server that ignores `page`: always the same full page
            return io.BytesIO(_json.dumps([{"name": "a", "owner": {"login": "example-org"}}]).encode())
        with self.assertRaises(RuntimeError):
            gr.github_inventory("t", opener=opener)

    def test_manifest_retry_absorbs_one_pin_mismatch(self):
        calls, slept = [], []
        def flaky(only, manifest=None):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("pin mismatch")
            return [("r", "u", "v")], []
        with mock.patch.object(gr, "repos_from_manifest", side_effect=flaky):
            got = gr._load_manifest_targets(None, None, slept.append)
        self.assertEqual(got, ([("r", "u", "v")], []))
        self.assertEqual(slept, [gr.MANIFEST_RETRY_SECONDS])

    def test_manifest_retry_still_fails_a_real_mismatch(self):
        with mock.patch.object(gr, "repos_from_manifest", side_effect=RuntimeError("pin")):
            with self.assertRaises(RuntimeError):
                gr._load_manifest_targets(None, None, lambda s: None)


class Alerting(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.state = Path(self._td.name) / "alert_state.json"
        self.sent = []

    def tearDown(self):
        self._td.cleanup()

    def send(self, subject, body):
        self.sent.append((subject, body))

    def alert(self, *results):
        return gr.alert_new_problems(list(results), self.state, self.send)

    def test_dedups_same_state_and_rearms_after_clear(self):
        bad = gr.Result("r", "main", gr.DIVERGED, "x", "a", "b")
        self.alert(bad)
        self.alert(bad)
        self.assertEqual(len(self.sent), 1)
        self.alert(gr.Result("r", "main", gr.OK, ""))
        self.alert(bad)
        self.assertEqual(len(self.sent), 2)

    def test_changed_shas_realert(self):
        self.alert(gr.Result("r", "main", gr.DIVERGED, "x", "a", "b"))
        self.alert(gr.Result("r", "main", gr.DIVERGED, "x", "a", "c"))
        self.assertEqual(len(self.sent), 2)

    def test_error_mails_at_three_of_last_six_runs(self):
        err = gr.Result("r", "main", gr.ERROR, "net")
        ok = gr.Result("r", "main", gr.OK, "")
        self.alert(err)
        self.alert(ok)
        self.alert(err)
        self.assertEqual(self.sent, [])  # 2 of the window so far: not yet
        self.alert(err)
        self.assertEqual(len(self.sent), 1)
        self.alert(err)
        self.assertEqual(len(self.sent), 1)  # same standing ERROR: mailed once

    def test_partial_run_keeps_other_repos_state(self):
        # Review finding 8: a manual --repo X run must not forget repo Y's mailed state.
        self.alert(gr.Result("y", "main", gr.DIVERGED, "x", "a", "b"))
        self.alert(gr.Result("x", "main", gr.OK, ""))
        self.alert(gr.Result("y", "main", gr.DIVERGED, "x", "a", "b"))
        self.assertEqual(len(self.sent), 1)

    def test_alternating_error_still_mails(self):
        # Review follow-up A: strictly-consecutive counting never mailed a host that fails
        # every other run (12 alternating runs, 0 mails). The window catches it.
        err = gr.Result("r", "main", gr.ERROR, "net")
        ok = gr.Result("r", "main", gr.OK, "")
        for i in range(12):
            self.alert(err if i % 2 == 0 else ok)
        self.assertEqual(len(self.sent), 1)

    def test_run_scoped_key_clears_and_recurrence_mails_again(self):
        # Review follow-up B: '(config):-' style keys used to stick forever after one mail.
        cfg = gr.Result("(config)", "-", gr.ERROR, "token unreadable", immediate=True)
        self.alert(cfg)
        self.alert(gr.Result("r", "main", gr.OK, ""))  # a healthy run: (config) not produced
        self.alert(cfg)
        self.assertEqual(len(self.sent), 2)

    def test_branch_key_is_run_scoped(self):
        bad = gr.Result("r", "(branch)", gr.ERROR, "unsafe branch name", immediate=True)
        self.alert(bad)
        self.alert(gr.Result("r", "main", gr.OK, ""))  # default_branch fixed: key not produced
        self.alert(bad)
        self.assertEqual(len(self.sent), 2)

    def test_corrupt_state_fails_open_to_alerting(self):
        self.state.write_text("{not json")
        self.alert(gr.Result("r", "main", gr.DIVERGED, "x"))
        self.assertEqual(len(self.sent), 1)

    def test_send_failure_propagates(self):
        def boom(subject, body):
            raise PermissionError("hub")
        with self.assertRaises(PermissionError):
            gr.alert_new_problems([gr.Result("r", "main", gr.DIVERGED, "x")], self.state, boom)


def _manifest(primary_url, mirror_url, gh_hosts=("github.com",), fj_hosts=("git.your-self-hosted-git.example",)):
    from tools.remote_resolve import AuthRef, Manifest, RemoteEntry, RepoManifestEntry
    return Manifest(
        repos={"r": RepoManifestEntry("r", (RemoteEntry("primary", primary_url, "gh"),
                                            RemoteEntry("mirror", mirror_url, "fj")))},
        auth_refs={"gh": AuthRef("gh", gh_hosts), "fj": AuthRef("fj", fj_hosts)},
        signature_verified=True)


class TrustBoundary(unittest.TestCase):
    def test_unverified_manifest_is_refused(self):
        # A pin mismatch must never be used: load_manifest only RECORDS the verdict.
        m = _manifest("git@github.com:O/r.git", "https://git.your-self-hosted-git.example/github-mirror/r.git")
        import dataclasses
        with self.assertRaises(gr.UnverifiedManifest):
            gr.repos_from_manifest(None, dataclasses.replace(m, signature_verified=False))

    def test_good_manifest_entry_is_selected_and_userinfo_stripped(self):
        repos, refused = gr.repos_from_manifest(None, _manifest(
            "git@github.com:O/r.git", "https://pantheon-fleet@git.your-self-hosted-git.example/github-mirror/r.git"))
        self.assertEqual(repos, [("r", "git@github.com:O/r.git",
                                  "https://git.your-self-hosted-git.example/github-mirror/r.git")])
        self.assertEqual(refused, [])

    def test_lookalike_host_never_gets_a_token(self):
        # Review finding 4: the old substring match let git.your-self-hosted-git.example.evil.com through.
        repos, _ = gr.repos_from_manifest(None, _manifest(
            "git@github.com:O/r.git", "https://git.your-self-hosted-git.example.evil.com/github-mirror/r.git"))
        self.assertEqual(repos, [])

    def test_host_outside_allowed_hosts_is_refused(self):
        repos, refused = gr.repos_from_manifest(None, _manifest(
            "git@github.com:O/r.git", "https://git.your-self-hosted-git.example/github-mirror/r.git",
            fj_hosts=("elsewhere.example",)))
        self.assertEqual(repos, [])
        self.assertEqual(len(refused), 1)


class MainExitCodes(unittest.TestCase):
    """run()/main() wiring (review: run() had no tests). Real git, fake forge API."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.gh = self.root / "github.git"
        self.fj = self.root / "forgejo.git"
        work = self.root / "w"
        for bare in (self.gh, self.fj):
            git("init", "--quiet", "--bare", "-b", "main", str(bare), cwd=self.root)
        git("init", "--quiet", "-b", "main", str(work), cwd=self.root)
        (work / "f").write_text("a")
        git("add", "f", cwd=work)
        git("commit", "--quiet", "-m", "a", cwd=work)
        self.a = git("rev-parse", "HEAD", cwd=work)
        git("push", "--quiet", str(self.gh), "HEAD:refs/heads/main", cwd=work)
        (work / "f").write_text("b")
        git("commit", "--quiet", "-am", "b", cwd=work)
        git("push", "--quiet", str(self.fj), "HEAD:refs/heads/main", cwd=work)  # forgejo ahead
        tok = self.root / "fj.tok"
        tok.write_text("FJTOKEN\n")
        self.patches = [
            mock.patch.dict(os.environ, {"PANTHEON_STATE_DIR": str(self.root / "state")}),
            mock.patch.object(gr, "FORGEJO_TOKEN_FILE", tok),
            mock.patch.object(gr, "GITHUB_TOKEN_FILE", self.root / "absent.tok"),
            mock.patch.object(gr, "repos_from_manifest",
                              lambda only, manifest=None: ([("r", str(self.gh), str(self.fj))], [])),
        ]
        for p in self.patches:
            p.start()
        self.sent = []

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._td.cleanup()

    def main(self, *argv, send=None):
        return gr.main(list(argv), git=hermetic_git,
                       send=send or (lambda s, b: self.sent.append(s)),
                       forgejo_api_fn=lambda m, p, t: {"mirror": False})

    def test_findings_exit_1_without_flag_0_with_flag_after_mailing(self):
        # github behind + no GitHub credential -> REPLAY_PENDING (a finding)
        self.assertEqual(self.main(), 1)
        self.assertEqual(self.main("--findings-exit-zero"), 0)
        self.assertEqual(len(self.sent), 1)  # deduped across the two runs

    def test_flag_does_not_mask_findings_when_not_mailed(self):
        self.assertEqual(self.main("--findings-exit-zero", "--no-alert"), 1)

    def test_undeliverable_alert_exits_3_even_with_flag(self):
        def boom(s, b):
            raise PermissionError("hub")
        self.assertEqual(self.main("--findings-exit-zero", send=boom), gr.EXIT_ALERT)

    def test_missing_forgejo_token_exits_2_even_with_flag(self):
        with mock.patch.object(gr, "FORGEJO_TOKEN_FILE", self.root / "missing"):
            self.assertEqual(self.main("--findings-exit-zero"), gr.EXIT_CONFIG)
        self.assertEqual(len(self.sent), 1)  # and it was mailed

    def test_held_lock_skips_with_0(self):
        lock = self.root / "state" / "git_reconciler" / "run.lock"
        lock.parent.mkdir(parents=True)
        import fcntl
        with open(lock, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            self.assertEqual(self.main(), 0)
        self.assertEqual(self.sent, [])

    def test_inventory_failure_is_loud_but_manifest_repos_still_run(self):
        gh_tok = self.root / "gh.tok"
        gh_tok.write_text("GHTOKEN\n")
        def boom(tok):
            raise OSError("api down")
        with mock.patch.object(gr, "GITHUB_TOKEN_FILE", gh_tok):
            rc = gr.run(only=None, branches=None, dry_run=False, alert=True, git=hermetic_git,
                        send=lambda s, b: self.sent.append(b),
                        forgejo_api_fn=lambda m, p, t: {"mirror": False}, inventory_fn=boom,
                        sleep=lambda s: None)
        self.assertEqual(rc, 1)
        self.assertTrue(any("GitHub inventory failed" in b for b in self.sent))
        # the manifest repo was still reconciled: GitHub was behind and got the replay
        heads = [subprocess.run(["git", "rev-parse", "refs/heads/main"], cwd=b, env=ENV,
                                capture_output=True, text=True).stdout.strip() for b in (self.gh, self.fj)]
        self.assertEqual(heads[0], heads[1])

    def test_manifest_takes_precedence_and_refused_entry_never_falls_back(self):
        gh_tok = self.root / "gh.tok"
        gh_tok.write_text("GHTOKEN\n")
        inv = [{"name": "r", "owner": {"login": "example-org"}, "default_branch": "main"},
               {"name": "evil", "owner": {"login": "example-org"}, "default_branch": "main"}]
        seen = []
        def git(argv, cwd, env):
            if argv[0] == "ls-remote":
                seen.append(argv[2])
            return hermetic_git(argv, cwd, env)
        with mock.patch.object(gr, "GITHUB_TOKEN_FILE", gh_tok), \
             mock.patch.object(gr, "repos_from_manifest",
                               lambda only, manifest=None: ([("r", str(self.gh), str(self.fj))],
                                                            [("evil", "host not allowed", "evil")])):
            gr.run(only=None, branches=None, dry_run=True, alert=False, git=git, send=None,
                   forgejo_api_fn=lambda m, p, t: {"mirror": False}, inventory_fn=lambda t: inv,
                   sleep=lambda s: None)
        self.assertIn(str(self.gh), seen)                       # manifest url, not the inventory one
        self.assertFalse(any("evil" in u for u in seen))         # refused entry: no fallback

    def test_circuit_breaker_stops_calling_a_dead_host(self):
        gh_tok = self.root / "gh.tok"
        gh_tok.write_text("GHTOKEN\n")
        inv = [{"name": f"r{i}", "owner": {"login": "example-org"}, "default_branch": "main"}
               for i in range(6)]
        github_calls = []
        def git(argv, cwd, env):
            if argv[0] == "ls-remote" and "github.com" in argv[2]:
                github_calls.append(argv[2])
                return subprocess.CompletedProcess(argv, 124, "", "timed out")
            return hermetic_git(argv, cwd, env)
        with mock.patch.object(gr, "GITHUB_TOKEN_FILE", gh_tok), \
             mock.patch.object(gr, "repos_from_manifest", lambda only, manifest=None: ([], [])):
            gr.run(only=None, branches=None, dry_run=True, alert=False, git=git, send=None,
                   forgejo_api_fn=lambda m, p, t: {"mirror": False}, inventory_fn=lambda t: inv,
                   sleep=lambda s: None)
        self.assertEqual(len(github_calls), gr.CIRCUIT_AFTER)

    def _inv_run(self, inv, *, git=None, api=None, clock=None, dry_run=True, only=None, **kw):
        gh_tok = self.root / "gh.tok"
        gh_tok.write_text("GHTOKEN\n")
        with mock.patch.object(gr, "GITHUB_TOKEN_FILE", gh_tok), \
             mock.patch.object(gr, "repos_from_manifest", lambda only, manifest=None: ([], [])):
            return gr.run(only=only, branches=None, dry_run=dry_run, alert=False, git=git or offline_git,
                          send=None, forgejo_api_fn=api or (lambda m, p, t: {"mirror": False}),
                          inventory_fn=lambda t: inv, sleep=lambda s: None,
                          **({"clock": clock} if clock else {}), **kw)

    def _inv(self, n):
        return [{"name": f"r{i}", "owner": {"login": "example-org"}, "default_branch": "main"}
                for i in range(n)]

    def test_hanging_forgejo_api_trips_the_breaker(self):
        # Review REBOUND (a): lookup failures did not count, so 30 hanging lookups ran to ~900s.
        calls = []
        def api(m, p, t):
            calls.append(p)
            raise OSError("timed out")
        self._inv_run(self._inv(10), api=api)
        self.assertEqual(len(calls), gr.CIRCUIT_AFTER)

    def test_non_consecutive_failures_still_trip_the_breaker(self):
        # Review REBOUND (b): a host failing every third repo never tripped a consecutive breaker.
        calls = []
        def api(m, p, t):
            calls.append(p)
            if len(calls) % 3 == 0:
                raise OSError("timed out")
            return {"mirror": False}
        self._inv_run(self._inv(30), api=api)
        self.assertEqual(len(calls), 3 * gr.CIRCUIT_AFTER)  # stops at the 3rd failure

    def test_run_budget_bounds_the_run_and_rotates_the_start(self):
        t = [0.0]
        def clock():
            t[0] += 100.0  # every check costs 100s of wall clock
            return t[0]
        seen = []
        def api(m, p, tok):
            seen.append(p.rsplit("/", 1)[1])
            return {"mirror": False}
        self._inv_run(self._inv(10), api=api, clock=clock, dry_run=False)
        first = list(seen)
        self.assertLess(len(first), 10)               # the budget ended the loop early
        state = gr._state_dir()
        self.assertEqual(gr._load_next_start(state), f"r{len(first)}")
        seen.clear()
        t[0] = 0.0
        self._inv_run(self._inv(10), api=api, clock=clock, dry_run=False)
        self.assertEqual(seen[0], f"r{len(first)}")   # the next run starts where this one stopped

    def test_budget_exhaustion_is_reported_not_silent(self):
        t = [0.0]
        def clock():
            t[0] += 1000.0
            return t[0]
        rc = self._inv_run(self._inv(3), clock=clock)
        self.assertEqual(rc, 1)

    def test_unsafe_default_branch_is_never_used(self):
        seen = []
        def git(argv, cwd, env):
            seen.append(argv)
            return offline_git(argv, cwd, env)
        inv = [{"name": "r", "owner": {"login": "example-org"}, "default_branch": "--upload-pack=x"}]
        rc = self._inv_run(inv, git=git)
        self.assertEqual(rc, 1)
        self.assertFalse(any("--upload-pack=x" in " ".join(a) for a in seen))

    def test_missing_forgejo_copy_is_no_copy(self):
        import urllib.error
        def api(m, p, t):
            raise urllib.error.HTTPError(p, 404, "not found", {}, None)
        self._inv_run(self._inv(1), api=api)
        self.assertIn(gr.NO_COPY, gr.BAD)

    def test_per_repo_failures_never_trip_the_breaker(self):
        # REBOUND #2: three repos answering "Repository not found" (deleted/renamed/unreadable)
        # opened the github breaker and starved every healthy repo after them, every run.
        lookups = []
        def api(m, p, t):
            lookups.append(p)
            return {"mirror": False}
        def git(argv, cwd, env):
            if argv[0] == "ls-remote" and "github.com" in argv[2] and argv[2].rsplit("/", 1)[1] in (
                    "r0.git", "r1.git", "r2.git"):
                return subprocess.CompletedProcess(argv, 128, "", "remote: Repository not found.")
            return offline_git(argv, cwd, env)
        self._inv_run(self._inv(10), api=api, git=git, dry_run=False)
        self.assertEqual(len(lookups), 10)  # every repo was still reconciled

    def test_breaker_trip_moves_the_start_to_the_first_skipped_repo(self):
        def api(m, p, t):
            raise OSError("timed out")
        self._inv_run(self._inv(10), api=api, dry_run=False)
        self.assertEqual(gr._load_next_start(gr._state_dir()), f"r{gr.CIRCUIT_AFTER}")

    def test_dry_run_and_filtered_runs_do_not_move_the_rotation(self):
        state = gr._state_dir()
        gr._save_next_start(state, "r5")
        t = [0.0]
        def clock():
            t[0] += 1000.0
            return t[0]
        self._inv_run(self._inv(10), clock=clock)                          # dry-run
        self.assertEqual(gr._load_next_start(state), "r5")
        self._inv_run(self._inv(10), clock=clock, dry_run=False, only=["r1"])  # filtered
        self.assertEqual(gr._load_next_start(state), "r5")

    def test_host_down_classification(self):
        down = gr.Result("r", "main", gr.ERROR, "github ls-remote failed: git ls-remote timed out after 120s")
        gone = gr.Result("r", "main", gr.ERROR, "github ls-remote failed: remote: Repository not found.")
        auth = gr.Result("r", "main", gr.ERROR, "forgejo repo lookup failed: HTTPError 401")
        api5 = gr.Result("r", "main", gr.ERROR, "forgejo repo lookup failed: HTTP Error 503: Service Unavailable")
        self.assertEqual(gr._failed_host(down), "github")
        self.assertIsNone(gr._failed_host(gone))
        self.assertIsNone(gr._failed_host(auth))
        self.assertEqual(gr._failed_host(api5), "forgejo")

    def test_repo_name_is_not_host_evidence(self):
        # "ssl-playground" in the URL once tripped the github breaker for a per-repo failure.
        named = gr.Result("ssl-playground", "main", gr.ERROR,
                          "github ls-remote failed: fatal: repository "
                          "'https://github.com/example-org/ssl-playground.git/' not found")
        tls = gr.Result("r", "main", gr.ERROR, "github fetch failed: fatal: unable to access "
                        "'https://github.com/O/r.git/': OpenSSL SSL_connect: SSL_ERROR_SYSCALL")
        refused = gr.Result("r", "main", gr.ERROR, "forgejo repo lookup failed: "
                            "URLError(ConnectionRefusedError(111, 'Connection refused'))")
        http5 = gr.Result("r", "main", gr.ERROR, "forgejo repo lookup failed: <HTTPError 502: 'Bad Gateway'>")
        self.assertIsNone(gr._failed_host(named))
        self.assertEqual(gr._failed_host(tls), "github")
        self.assertEqual(gr._failed_host(refused), "forgejo")
        self.assertEqual(gr._failed_host(http5), "forgejo")

    def test_secret_never_in_log_output(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.main("--no-alert")
        self.assertNotIn("FJTOKEN", buf.getvalue())


class Helpers(unittest.TestCase):
    def test_token_never_in_argv_only_env_and_trace_env_stripped(self):
        with mock.patch.dict(os.environ, {"GIT_TRACE_CURL": "1", "GIT_CURL_VERBOSE": "1"}):
            env = gr._auth_env("u", "SECRET")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.extraHeader")
        self.assertNotIn("SECRET", env["GIT_CONFIG_VALUE_0"])  # base64'd, not the raw secret
        self.assertNotIn("GIT_TRACE_CURL", env)
        self.assertNotIn("GIT_CURL_VERBOSE", env)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_no_token_means_no_header(self):
        self.assertNotIn("GIT_CONFIG_KEY_0", gr._auth_env("u", None))

    def test_read_token_raw_key_value_and_padding(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t"
            p.write_text("abc123\n")
            self.assertEqual(gr._read_token(p), "abc123")
            p.write_text("gh_pat=github_pat_X\n")
            self.assertEqual(gr._read_token(p), "github_pat_X")
            p.write_text("dG9rZW4==\n")  # review finding 7: a raw token with '=' stays whole
            self.assertEqual(gr._read_token(p), "dG9rZW4==")
            self.assertIsNone(gr._read_token(Path(td) / "missing"))

    def test_redact(self):
        gr._SECRETS.add("s3cr3t-value")
        self.assertNotIn("s3cr3t-value", gr._redact("fatal: s3cr3t-value rejected"))
        self.assertEqual(gr._redact("Authorization: Basic QWxhZGRpbg=="),
                         "Authorization: Basic <redacted>")
        self.assertEqual(gr._redact("https://user:pw@github.com/x"), "https://<redacted>@github.com/x")

    def test_repo_name_from_url(self):
        self.assertEqual(gr._repo_name_from_url("git@github.com:O/pantheon-config.git"), "pantheon-config")
        self.assertEqual(gr._repo_name_from_url("https://github.com/O/r.git"), "r")

    def test_safe_ref(self):
        for good in ("main", "master", "release/1.2", "dev_x"):
            self.assertTrue(gr._SAFE_REF.match(good), good)
        for bad in ("-x", "a..b", "x.lock", "x/", "a b", "--upload-pack=x", ""):
            self.assertFalse(gr._SAFE_REF.match(bad), bad)

    def test_url_helpers(self):
        self.assertEqual(gr.forgejo_full_name(
            "https://pantheon-fleet@git.your-self-hosted-git.example/github-mirror/pantheon-config.git"),
            "github-mirror/pantheon-config")
        self.assertEqual(gr._strip_userinfo("https://u@git.your-self-hosted-git.example/a/b.git"),
                         "https://git.your-self-hosted-git.example/a/b.git")
        self.assertEqual(gr.github_https("git@github.com:O/R.git"), "https://github.com/O/R.git")


if __name__ == "__main__":
    unittest.main()
