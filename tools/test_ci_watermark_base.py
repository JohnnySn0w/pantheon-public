"""Tests for tools/ci_watermark_base.py -- the high-watermark diff base (T-4182).

Run from repo root:
    python3 -m unittest tools.test_ci_watermark_base -v

WHY THE TOOL EXISTS. hermes-audit.yml scans a DIFF RANGE (cascade-on-merge.yml did too, until
T-4186 retired it). Its base used to be `github.event.before`, so superseding (concurrency) a
queued run lost that run's commits for good. The base is now the head of the last SUCCESSFUL run of the same workflow on
the same branch, so the run that replaces a cancelled one covers the cancelled one's range too.

Conventions here:
  - Ancestry cases use a REAL temp git repo; only the HTTP call is faked.
  - Fixture-string note: the byte-sequence trigger is built at runtime via source-time
    concatenation ("HERME" + "S.md") so the contiguous bytes never appear in this file
    (standing rule, /home/claude/CLAUDE.md).
"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

import yaml

from tools import ci_watermark_base as cwb
from tools.hermes_diff_scan import scan_diff_lines

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
TRIGGER = "HERME" + "S.md"  # never written contiguously in source
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}
TOKEN = "ghs_TESTTOKEN_do_not_leak"


class Repo:
    """A throwaway real git repo."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._n = 0
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.path), *args],
            env=GIT_ENV, capture_output=True, text=True, check=True,
        )
        return proc.stdout.strip()

    def commit(self, name: str | None = None, content: str | None = None) -> str:
        self._n += 1
        fname = name or f"f{self._n}.txt"
        (self.path / fname).write_text(content if content is not None else f"line {self._n}\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"commit {self._n}")
        return self.git("rev-parse", "HEAD")

    def rev_list(self, spec: str) -> set[str]:
        return set(self.git("rev-list", spec).split())


class GitTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Repo(Path(self._tmp.name))
        self.cwd = self.repo.path


class LinearMainMixin:
    """main: c1..c6 (HEAD = c6), plus a side commit X forked from c2 (NOT an ancestor of c6)."""

    def build_linear(self) -> None:
        r = self.repo
        self.c = {1: r.commit()}
        self.c[2] = r.commit()
        r.git("checkout", "-q", "-b", "side")
        self.x = r.commit("side.txt")
        r.git("checkout", "-q", "main")
        for i in range(3, 7):
            self.c[i] = r.commit()


class ChooseBaseTest(LinearMainMixin, GitTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.build_linear()

    def test_superseded_range_is_covered_by_pending_runs_base(self) -> None:
        # Runs for c2..c5 were superseded/cancelled; only c1 ever succeeded. The pending run
        # (head c6, before c5) must scan everything from c1 forward, not just c5..c6.
        d = cwb.choose_base(self.cwd, heads=[self.c[1]], before=self.c[5])
        self.assertEqual(d.base, self.c[1])
        self.assertEqual(d.range_spec, f"{self.c[1]}..HEAD")
        self.assertEqual(
            self.repo.rev_list(d.range_spec), {self.c[2], self.c[3], self.c[4], self.c[5], self.c[6]}
        )

    def test_watermark_older_than_before_is_used(self) -> None:
        d = cwb.choose_base(self.cwd, heads=[self.c[3]], before=self.c[5])
        self.assertEqual(d.base, self.c[3])

    def test_before_older_than_watermark_is_used(self) -> None:
        d = cwb.choose_base(self.cwd, heads=[self.c[5]], before=self.c[3])
        self.assertEqual(d.base, self.c[3])

    def test_diverged_before_uses_merge_base(self) -> None:
        # before = X (side branch, exists locally, not an ancestor of HEAD): the base must be an
        # ancestor of BOTH the watermark and before, i.e. their fork point c2.
        d = cwb.choose_base(self.cwd, heads=[self.c[4]], before=self.x)
        self.assertEqual(d.base, self.c[2])

    def test_watermark_equal_to_head_and_before_gives_empty_range(self) -> None:
        d = cwb.choose_base(self.cwd, heads=[self.c[6]], before=self.c[6])
        self.assertEqual(self.repo.rev_list(d.range_spec), set())

    def test_non_ancestor_watermark_falls_back_to_full_history(self) -> None:
        # force-push shape: the recorded success is not reachable from HEAD.
        d = cwb.choose_base(self.cwd, heads=[self.x], before=self.c[5])
        self.assertIsNone(d.base)
        self.assertEqual(d.range_spec, "HEAD")
        self.assertTrue(d.reason.startswith("full-history"), d.reason)

    def test_older_ancestor_is_used_when_newest_success_is_not_an_ancestor(self) -> None:
        d = cwb.choose_base(self.cwd, heads=[self.x, self.c[2]], before=self.c[5])
        self.assertEqual(d.base, self.c[2])

    def test_no_successful_runs_falls_back_to_full_history(self) -> None:
        d = cwb.choose_base(self.cwd, heads=[], before=self.c[5])
        self.assertIsNone(d.base)
        self.assertEqual(d.range_spec, "HEAD")

    def test_full_history_range_includes_the_root_commit(self) -> None:
        d = cwb.choose_base(self.cwd, heads=[], before=self.c[5])
        self.assertIn(self.c[1], self.repo.rev_list(d.range_spec))

    def test_unknown_before_sha_falls_back_to_full_history(self) -> None:
        d = cwb.choose_base(self.cwd, heads=[self.c[1]], before="a" * 40)
        self.assertIsNone(d.base)

    def test_malformed_or_empty_before_falls_back_to_full_history(self) -> None:
        for bad in ("not-a-sha", "", "--upload-pack=x"):
            with self.subTest(before=bad):
                d = cwb.choose_base(self.cwd, heads=[self.c[1]], before=bad)
                self.assertIsNone(d.base)


class NewBranchTest(GitTestCase):
    """O2(b): the first push of a branch (all-zero before) uses MAIN's last successful run."""

    def setUp(self) -> None:
        super().setUp()
        r = self.repo
        self.m = {i: r.commit() for i in (1, 2, 3)}
        r.git("checkout", "-q", "-b", "agent/T-1")
        self.f1 = r.commit("feat1.txt")
        self.f2 = r.commit("feat2.txt")
        r.git("checkout", "-q", "main")
        self.m[4] = r.commit()  # main moves on after the fork
        r.git("checkout", "-q", "agent/T-1")

    def test_main_watermark_at_the_fork_point_is_the_merge_base(self) -> None:
        d = cwb.choose_base_new_branch(self.cwd, default_branch_heads=[self.m[3]])
        self.assertEqual(d.base, self.m[3])
        self.assertEqual(self.repo.rev_list(d.range_spec), {self.f1, self.f2})

    def test_newest_main_success_that_is_not_an_ancestor_still_gives_the_fork_point(self) -> None:
        # D16: m4 landed on main after the branch forked, so it is NOT an ancestor of the branch.
        # merge-base(m4, HEAD) is the fork point m3, which is reachable from m4 and so already
        # audited; the range is exactly the branch's own commits.
        d = cwb.choose_base_new_branch(self.cwd, default_branch_heads=[self.m[4], self.m[2]])
        self.assertEqual(d.base, self.m[3])
        self.assertEqual(d.reason, "default-branch-watermark")
        self.assertEqual(self.repo.rev_list(d.range_spec), {self.f1, self.f2})

    def test_only_the_newest_locally_present_main_head_is_used(self) -> None:
        # The lookup can be newer than the checkout's fetch (main moved in between), so the newest
        # listed head may be absent locally: skip it, use the next one that exists.
        ghost = "e" * 40
        d = cwb.choose_base_new_branch(self.cwd, default_branch_heads=[ghost, self.m[4], self.m[1]])
        self.assertEqual(d.base, self.m[3])  # via m4, not m1

    def test_main_watermark_missing_locally_gives_full_history(self) -> None:
        d = cwb.choose_base_new_branch(self.cwd, default_branch_heads=["e" * 40, "f" * 40])
        self.assertIsNone(d.base)
        self.assertEqual(d.range_spec, "HEAD")
        self.assertIn("full-history", d.reason)

    def test_main_watermark_with_no_merge_base_gives_full_history(self) -> None:
        r = self.repo
        r.git("checkout", "-q", "--orphan", "unrelated")
        r.git("rm", "-rfq", ".")
        unrelated = r.commit("other.txt")
        r.git("checkout", "-q", "agent/T-1")
        d = cwb.choose_base_new_branch(self.cwd, default_branch_heads=[unrelated])
        self.assertIsNone(d.base)
        self.assertIn("no-merge-base", d.reason)

    def test_no_main_successes_gives_full_history(self) -> None:
        d = cwb.choose_base_new_branch(self.cwd, default_branch_heads=[])
        self.assertIsNone(d.base)

    def test_compute_looks_up_the_default_branch_for_a_zero_before(self) -> None:
        calls: list[str] = []

        def fetch(branch: str) -> list[str]:
            calls.append(branch)
            return [self.m[3]]

        d = cwb.compute_base(self.cwd, fetch_heads=fetch, branch="agent/T-1", before=cwb.ZERO_SHA)
        self.assertEqual(calls, ["main"])
        self.assertEqual(d.base, self.m[3])

    def test_compute_looks_up_the_pushed_branch_for_a_real_before(self) -> None:
        calls: list[str] = []

        def fetch(branch: str) -> list[str]:
            calls.append(branch)
            return [self.m[1]]

        cwb.compute_base(self.cwd, fetch_heads=fetch, branch="agent/T-1", before=self.f1)
        self.assertEqual(calls, ["agent/T-1"])

    def test_new_branch_lookup_failure_gives_full_history(self) -> None:
        def fetch(branch: str) -> list[str]:
            raise cwb.WatermarkLookupError("boom")

        d = cwb.compute_base(self.cwd, fetch_heads=fetch, branch="agent/T-1", before=cwb.ZERO_SHA)
        self.assertIsNone(d.base)
        self.assertIn("lookup-failed", d.reason)


class NewBranchForkedFromOldMainTest(GitTestCase):
    """D16: the common shape. main is far ahead of the fork point; the newest main success is
    NOT an ancestor of the branch, and there may be more than a page of successes since the fork."""

    def setUp(self) -> None:
        super().setUp()
        r = self.repo
        self.m = {i: r.commit() for i in (1, 2)}
        r.git("checkout", "-q", "-b", "agent/T-2")
        self.f = [r.commit("feat1.txt"), r.commit("feat2.txt"), r.commit("feat3.txt")]
        r.git("checkout", "-q", "main")
        for i in range(3, 13):
            self.m[i] = r.commit()  # main is 10 commits ahead of the fork point
        r.git("checkout", "-q", "agent/T-2")

    def test_base_is_the_fork_point_and_range_is_only_the_branchs_commits(self) -> None:
        self.assertFalse(cwb.is_ancestor(self.cwd, self.m[12], "HEAD"))  # premise: not an ancestor
        d = cwb.choose_base_new_branch(self.cwd, default_branch_heads=[self.m[12]])
        self.assertEqual(d.base, self.m[2])
        self.assertEqual(d.range_spec, f"{self.m[2]}..HEAD")
        self.assertEqual(self.repo.rev_list(d.range_spec), set(self.f))

    def test_compute_base_takes_the_same_path_for_a_zero_before(self) -> None:
        d = cwb.compute_base(
            self.cwd, fetch_heads=lambda b: [self.m[12], self.m[11]], branch="agent/T-2",
            before=cwb.ZERO_SHA,
        )
        self.assertEqual(d.base, self.m[2])
        self.assertEqual(self.repo.rev_list(d.range_spec), set(self.f))

    def test_main_end_to_end_with_a_far_ahead_main_watermark(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        rc = cwb.main(
            ["--workflow", "hermes-audit.yml", "--event", "push", "--branch", "agent/T-2",
             "--before", cwb.ZERO_SHA, "--repo-dir", str(self.cwd)],
            environ={"GITHUB_TOKEN": TOKEN, "GITHUB_REPOSITORY": "o/r"},
            urlopen=_Urlopen(_body(_run(self.m[12]), _run(self.m[11]))),
            sleep=lambda s: None, stdout=out, stderr=err,
        )
        self.assertEqual((rc, out.getvalue()), (0, f"{self.m[2]}..HEAD\n"))

    def test_existing_branch_watermark_rule_is_unchanged(self) -> None:
        # Same-ref watermarks keep the strict "first ancestor of HEAD" rule (D16 is new-branch only):
        # a non-ancestor same-branch success is NOT used, so this falls back to full history.
        d = cwb.choose_base(self.cwd, heads=[self.m[12]], before=self.f[1])
        self.assertIsNone(d.base)


class ComputeBaseFallbackTest(LinearMainMixin, GitTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.build_linear()

    def test_lookup_failure_falls_back_to_full_history_not_before(self) -> None:
        def fetch(branch: str) -> list[str]:
            raise cwb.WatermarkLookupError("HTTP 503")

        d = cwb.compute_base(self.cwd, fetch_heads=fetch, branch="main", before=self.c[5])
        self.assertIsNone(d.base)  # event.before would skip a cancelled run's range
        self.assertIn("lookup-failed", d.reason)

    def test_unexpected_exception_falls_back_to_full_history(self) -> None:
        def fetch(branch: str) -> list[str]:
            raise RuntimeError("unexpected")

        d = cwb.compute_base(self.cwd, fetch_heads=fetch, branch="main", before=self.c[5])
        self.assertIsNone(d.base)
        self.assertIn("unexpected", d.reason)

    def test_unusable_before_never_looks_up_the_pushed_branch(self) -> None:
        # T-4186/N3: `before` is unusable (force-push shape), so there is no ancestor question to ask
        # of THIS branch's runs and it is not looked up. It used to skip every lookup and go straight
        # to full history; it now consults ONLY the default branch's watermark first (see
        # ExistingBranchDefaultFallbackTest for why that is coverage-safe). With no usable
        # main watermark here (the fake returns the branch's own single head), it still lands on
        # full history, and never on event.before.
        fetch = mock.Mock(return_value=[])
        d = cwb.compute_base(self.cwd, fetch_heads=fetch, branch="agent/x", before="b" * 40)
        self.assertIsNone(d.base)
        self.assertEqual([c.args[0] for c in fetch.call_args_list], ["main"])
        self.assertIn("before-unusable", d.reason)

    def test_broken_git_never_narrows(self) -> None:
        with tempfile.TemporaryDirectory() as not_a_repo:
            d = cwb.compute_base(
                Path(not_a_repo), fetch_heads=lambda b: [self.c[1]], branch="main", before=self.c[5]
            )
        self.assertIsNone(d.base)


class ExistingBranchDefaultFallbackTest(GitTestCase):
    """T-4199 N3 (T-4186): an EXISTING branch with no usable watermark of its own tries
    merge-base(newest present default-branch success W, HEAD) BEFORE full history.

    COVERAGE-SAFE BY THE MODULE INVARIANT: every commit reachable from the head of any successful
    run has been scanned, so everything reachable from W has been, and merge-base(W, HEAD) is
    reachable from W. The range base..HEAD is then exactly what W has not vouched for. The common
    case it fixes: a fresh branch whose first push run was superseded/cancelled, so the second push
    has a real `before` but no successful run of its own and used to pay a ~70 s / 1.3 GB
    full-history scan."""

    def setUp(self) -> None:
        super().setUp()
        r = self.repo
        self.m = {i: r.commit() for i in (1, 2, 3)}
        r.git("checkout", "-q", "-b", "side")
        self.x = r.commit("side.txt")  # forked from m3, on no path to HEAD
        r.git("checkout", "-q", "main")
        r.git("checkout", "-q", "-b", "agent/T-1")
        self.f1 = r.commit("feat1.txt")
        self.f2 = r.commit("feat2.txt")
        r.git("checkout", "-q", "main")
        self.m[4] = r.commit()  # main moves on after the fork
        r.git("checkout", "-q", "agent/T-1")

    def fetcher(self, own: list[str], main: list[str] | Exception):
        calls: list[str] = []

        def fetch(branch: str) -> list[str]:
            calls.append(branch)
            heads = main if branch == "main" else own
            if isinstance(heads, Exception):
                raise heads
            return heads

        fetch.calls = calls  # type: ignore[attr-defined]
        return fetch

    def compute(self, fetch, **kw):
        kw.setdefault("before", self.f1)
        kw.setdefault("branch", "agent/T-1")
        return cwb.compute_base(self.cwd, fetch_heads=fetch, **kw)

    def test_no_successful_run_of_its_own_uses_the_main_watermark(self) -> None:
        fetch = self.fetcher(own=[], main=[self.m[4], self.m[3]])
        d = self.compute(fetch)
        self.assertEqual(d.base, self.m[3])  # merge-base(m4, HEAD): the fork point
        self.assertTrue(d.reason.startswith("default-branch-watermark"), d.reason)
        self.assertIn("no-successful-run", d.reason)
        self.assertEqual(self.repo.rev_list(d.range_spec), {self.f1, self.f2})
        self.assertEqual(fetch.calls, ["agent/T-1", "main"])

    def test_only_a_non_ancestor_own_watermark_uses_the_main_watermark(self) -> None:
        d = self.compute(self.fetcher(own=[self.x], main=[self.m[4]]))
        self.assertEqual(d.base, self.m[3])
        self.assertIn("no-ancestor-watermark", d.reason)

    def test_an_unusable_before_uses_the_main_watermark_without_asking_about_the_branch(self) -> None:
        fetch = self.fetcher(own=[self.f1], main=[self.m[4]])
        d = self.compute(fetch, before="a" * 40)  # force-push: the old tip is gone from the clone
        self.assertEqual(d.base, self.m[3])
        self.assertEqual(fetch.calls, ["main"])
        self.assertIn("before-unusable", d.reason)

    def test_the_range_still_covers_everything_the_main_watermark_does_not(self) -> None:
        # The invariant, directly: base..HEAD must contain every commit reachable from HEAD that is
        # NOT reachable from W. (If it did not, a commit could be skipped by the audit.)
        w = self.m[4]
        d = self.compute(self.fetcher(own=[], main=[w]))
        unvouched = self.repo.rev_list("HEAD") - self.repo.rev_list(w)
        self.assertLessEqual(unvouched, self.repo.rev_list(d.range_spec))

    def test_a_usable_own_watermark_wins_and_main_is_never_consulted(self) -> None:
        fetch = self.fetcher(own=[self.f1], main=[self.m[4]])
        d = self.compute(fetch, before=self.f2)
        self.assertEqual(d.base, self.f1)
        self.assertEqual(d.reason, "watermark")
        self.assertEqual(fetch.calls, ["agent/T-1"])

    def test_a_failed_main_lookup_keeps_full_history_never_before(self) -> None:
        d = self.compute(self.fetcher(own=[], main=cwb.WatermarkLookupError("HTTP 503")))
        self.assertIsNone(d.base)
        self.assertIn("full-history", d.reason)

    def test_a_main_watermark_missing_locally_keeps_full_history(self) -> None:
        d = self.compute(self.fetcher(own=[], main=["e" * 40]))
        self.assertIsNone(d.base)
        self.assertTrue(d.reason.startswith("full-history"), d.reason)  # the ORIGINAL broad decision stands

    def test_no_main_successes_keeps_full_history(self) -> None:
        d = self.compute(self.fetcher(own=[], main=[]))
        self.assertIsNone(d.base)
        self.assertTrue(d.reason.startswith("full-history"), d.reason)  # the ORIGINAL broad decision stands

    def test_a_main_watermark_with_no_merge_base_keeps_full_history(self) -> None:
        r = self.repo
        r.git("checkout", "-q", "--orphan", "unrelated")
        r.git("rm", "-rfq", ".")
        unrelated = r.commit("other.txt")
        r.git("checkout", "-q", "agent/T-1")
        d = self.compute(self.fetcher(own=[], main=[unrelated]))
        self.assertIsNone(d.base)
        self.assertTrue(d.reason.startswith("full-history"), d.reason)  # the ORIGINAL broad decision stands

    def test_a_push_to_main_itself_falls_back_to_its_own_older_successes(self) -> None:
        # branch == default branch (e.g. a force-pushed main): the "main lookup" is the same list.
        # HEAD here is agent/T-1's tip, standing in for a rewritten main; W = m4 is not an ancestor.
        d = self.compute(self.fetcher(own=[self.m[4]], main=[self.m[4]]), branch="main")
        self.assertEqual(d.base, self.m[3])


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


class _Urlopen:
    """Scripted urlopen: each item is bytes (a 200 body) or an exception to raise."""

    def __init__(self, *script: object) -> None:
        self.script = list(script)
        self.requests: list = []
        self.timeouts: list = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        self.timeouts.append(timeout)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return _Resp(item)


def _run(sha: str, conclusion: str = "success", **extra: object) -> dict:
    return {"head_sha": sha, "conclusion": conclusion, "event": "push", "head_branch": "main", **extra}


def _body(*runs: object) -> bytes:
    return json.dumps({"total_count": len(runs), "workflow_runs": list(runs)}).encode()


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.github.com/x", code, f"HTTP {code}", {}, None)


def _fetch(urlopen, sleeps=None, **kw):
    args = dict(repo="o/r", workflow="hermes-audit.yml", branch="main", event="push", token=TOKEN)
    args.update(kw)
    return cwb.fetch_successful_heads(
        **args, urlopen=urlopen, sleep=(sleeps.append if sleeps is not None else (lambda s: None))
    )


class FetchSuccessfulHeadsTest(unittest.TestCase):
    A, B, C = "a" * 40, "b" * 40, "c" * 40

    def test_request_shape(self) -> None:
        uo = _Urlopen(_body(_run(self.A)))
        _fetch(uo, branch="agent/T-4182")
        req = uo.requests[0]
        parsed = urllib.parse.urlparse(req.full_url)
        self.assertEqual(parsed.netloc, "api.github.com")
        self.assertEqual(parsed.path, "/repos/o/r/actions/workflows/hermes-audit.yml/runs")
        q = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(q["branch"], ["agent/T-4182"])
        self.assertEqual(q["event"], ["push"])
        self.assertEqual(q["status"], ["success"])
        self.assertEqual(q["per_page"], ["100"])
        self.assertEqual(req.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertIn("json", req.get_header("Accept"))
        self.assertNotIn(TOKEN, req.full_url)
        self.assertEqual(uo.timeouts, [20.0])

    def test_custom_api_url_is_honoured(self) -> None:
        uo = _Urlopen(_body())
        _fetch(uo, api_url="https://ghe.example.invalid/api/v3/")
        self.assertTrue(uo.requests[0].full_url.startswith("https://ghe.example.invalid/api/v3/repos/o/r/"))

    def test_failed_and_cancelled_runs_are_never_watermarks(self) -> None:
        # The last run FAILED and the one before it was cancelled; the older success is kept.
        uo = _Urlopen(_body(_run(self.C, "failure"), _run(self.B, "cancelled"), _run(self.A)))
        self.assertEqual(_fetch(uo), [self.A])

    def test_filters_dedups_and_preserves_order(self) -> None:
        uo = _Urlopen(_body(
            _run(self.A), _run(self.A), _run(self.B, event="pull_request"),
            _run(self.B, head_branch="other"), _run("not-a-sha"), "junk", None,
            {"conclusion": "success"}, _run(self.C),
        ))
        self.assertEqual(_fetch(uo), [self.A, self.C])

    def test_no_runs_is_an_empty_list_not_an_error(self) -> None:
        self.assertEqual(_fetch(_Urlopen(_body())), [])

    def test_transient_5xx_is_retried_then_succeeds(self) -> None:
        sleeps: list[float] = []
        uo = _Urlopen(_http_error(502), _body(_run(self.A)))
        self.assertEqual(_fetch(uo, sleeps), [self.A])
        self.assertEqual(len(uo.requests), 2)
        self.assertEqual(len(sleeps), 1)

    def test_persistent_5xx_raises_after_all_attempts(self) -> None:
        sleeps: list[float] = []
        uo = _Urlopen(_http_error(500))
        with self.assertRaises(cwb.WatermarkLookupError):
            _fetch(uo, sleeps)
        self.assertEqual(len(uo.requests), 3)
        self.assertEqual(len(sleeps), 2)

    def test_auth_and_not_found_are_not_retried(self) -> None:
        for code in (401, 403, 404):
            with self.subTest(code=code):
                uo = _Urlopen(_http_error(code))
                with self.assertRaises(cwb.WatermarkLookupError):
                    _fetch(uo)
                self.assertEqual(len(uo.requests), 1)

    def test_network_errors_are_retried_then_raise(self) -> None:
        for exc in (
            urllib.error.URLError("dns down"), socket.timeout("timed out"),
            TimeoutError("timed out"), ConnectionResetError("reset"),
        ):
            with self.subTest(exc=type(exc).__name__):
                uo = _Urlopen(exc)
                with self.assertRaises(cwb.WatermarkLookupError):
                    _fetch(uo)
                self.assertEqual(len(uo.requests), 3)

    def test_malformed_bodies_raise_without_retry(self) -> None:
        bodies = [
            b"<html>not json</html>", b"", b"\xff\xfe\x00garbage", b"[]", b'"str"', b"null",
            json.dumps({"total_count": 0}).encode(),
            json.dumps({"workflow_runs": "nope"}).encode(),
            json.dumps({"workflow_runs": {"a": 1}}).encode(),
        ]
        for body in bodies:
            with self.subTest(body=body[:20]):
                uo = _Urlopen(body)
                with self.assertRaises(cwb.WatermarkLookupError):
                    _fetch(uo)
                self.assertEqual(len(uo.requests), 1)


class MainTest(LinearMainMixin, GitTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.build_linear()

    def run_main(self, urlopen, *, before: str, branch: str = "main", token: str | None = TOKEN,
                 repo_dir: Path | None = None):
        out, err = io.StringIO(), io.StringIO()
        env = {"GITHUB_REPOSITORY": "o/r"}
        if token is not None:
            env["GITHUB_TOKEN"] = token
        rc = cwb.main(
            ["--workflow", "hermes-audit.yml", "--event", "push", "--branch", branch,
             "--before", before, "--repo-dir", str(repo_dir or self.cwd)],
            environ=env, urlopen=urlopen, sleep=lambda s: None, stdout=out, stderr=err,
        )
        return rc, out.getvalue(), err.getvalue()

    def assertSingleLine(self, out: str) -> None:
        self.assertTrue(out.endswith("\n"))
        self.assertEqual(len(out.splitlines()), 1)
        self.assertTrue(out.strip())

    def test_happy_path_prints_only_the_range(self) -> None:
        rc, out, err = self.run_main(_Urlopen(_body(_run(self.c[3]))), before=self.c[5])
        self.assertEqual(rc, 0)
        self.assertEqual(out, f"{self.c[3]}..HEAD\n")
        self.assertIn("reason=", err)

    def test_api_failure_prints_full_history_and_exits_zero(self) -> None:
        rc, out, err = self.run_main(_Urlopen(_http_error(500)), before=self.c[5])
        self.assertEqual((rc, out), (0, "HEAD\n"))
        self.assertIn("lookup-failed", err)

    def test_missing_token_prints_full_history(self) -> None:
        uo = _Urlopen(_body(_run(self.c[3])))
        rc, out, err = self.run_main(uo, before=self.c[5], token=None)
        self.assertEqual((rc, out), (0, "HEAD\n"))
        self.assertEqual(uo.requests, [])

    def test_new_branch_queries_main_and_uses_its_watermark(self) -> None:
        uo = _Urlopen(_body(_run(self.c[3])))
        rc, out, _ = self.run_main(uo, before=cwb.ZERO_SHA, branch="agent/T-9")
        self.assertEqual(rc, 0)
        self.assertEqual(out, f"{self.c[3]}..HEAD\n")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(uo.requests[0].full_url).query)
        self.assertEqual(q["branch"], ["main"])

    def test_new_branch_lookup_failure_prints_full_history(self) -> None:
        rc, out, _ = self.run_main(_Urlopen(urllib.error.URLError("down")), before=cwb.ZERO_SHA,
                                   branch="agent/T-9")
        self.assertEqual((rc, out), (0, "HEAD\n"))

    def test_unexpected_internal_error_prints_full_history(self) -> None:
        with mock.patch.object(cwb, "choose_base", side_effect=RuntimeError("bug")):
            rc, out, err = self.run_main(_Urlopen(_body(_run(self.c[3]))), before=self.c[5])
        self.assertEqual((rc, out), (0, "HEAD\n"))
        self.assertIn("unexpected", err)

    def test_not_a_git_repo_never_narrows(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rc, out, _ = self.run_main(_Urlopen(_body(_run(self.c[3]))), before=self.c[5],
                                       repo_dir=Path(d))
        self.assertEqual((rc, out), (0, "HEAD\n"))

    def test_stdout_is_always_exactly_one_nonempty_line(self) -> None:
        cases = [
            (_Urlopen(_body(_run(self.c[3]))), self.c[5]),
            (_Urlopen(_http_error(500)), self.c[5]),
            (_Urlopen(b"garbage"), self.c[5]),
            (_Urlopen(_body()), cwb.ZERO_SHA),
            (_Urlopen(_body()), "junk"),
        ]
        for uo, before in cases:
            with self.subTest(before=before):
                rc, out, _ = self.run_main(uo, before=before)
                self.assertEqual(rc, 0)
                self.assertSingleLine(out)

    def test_the_token_never_reaches_stdout_or_stderr(self) -> None:
        for uo in (_Urlopen(urllib.error.URLError(f"proxy said {TOKEN}")), _Urlopen(_body(_run(self.c[3])))):
            _, out, err = self.run_main(uo, before=self.c[5])
            self.assertNotIn(TOKEN, out)
            self.assertNotIn(TOKEN, err)

    def test_missing_required_flag_is_an_argparse_error(self) -> None:
        with self.assertRaises(SystemExit) as cm, mock.patch("sys.stderr", io.StringIO()):
            cwb.main(["--workflow", "hermes-audit.yml"], environ={}, stdout=io.StringIO())
        self.assertEqual(cm.exception.code, 2)


class PerCommitScanTest(GitTestCase):
    """Why the push path scans per-commit patches, not one net diff (O1)."""

    def test_net_diff_hides_an_add_then_remove_pair_that_the_commit_log_shows(self) -> None:
        r = self.repo
        base = r.commit("keep.txt")
        r.commit("leak.txt", f"see {TRIGGER}\n")       # commit A adds the trigger
        r.git("rm", "-q", "leak.txt")
        r.git("commit", "-q", "-m", "fix-forward")      # commit B removes it
        r.commit()

        net = r.git("diff", base, "HEAD")
        self.assertEqual(scan_diff_lines(net), [])      # the hole: a cumulative net diff is clean

        per_commit = r.git("log", "-p", "-m", "--format=", "--no-color", f"{base}..HEAD")
        found = scan_diff_lines(per_commit)
        self.assertEqual([v.path for v in found], ["leak.txt"])

    def test_merge_only_content_is_visible_with_dash_m(self) -> None:
        r = self.repo
        r.commit("a.txt")
        r.git("checkout", "-q", "-b", "side")
        r.commit("s.txt")
        r.git("checkout", "-q", "main")
        r.commit("m.txt")
        base = r.git("rev-parse", "HEAD~1")
        r.git("merge", "-q", "--no-ff", "-m", "merge", "side")
        (r.path / "evil.txt").write_text(f"{TRIGGER}\n")
        r.git("add", "-A")
        r.git("commit", "-q", "--amend", "--no-edit")   # evil-merge: content only in the merge commit
        log = r.git("log", "-p", "-m", "--format=", "--no-color", f"{base}..HEAD")
        self.assertIn("evil.txt", [v.path for v in scan_diff_lines(log)])


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _step(doc: dict, step_id: str) -> dict:
    for job in doc["jobs"].values():
        for step in job["steps"]:
            if step.get("id") == step_id:
                return step
    raise AssertionError(f"no step with id {step_id}")


SUPERSEDABLE = ("hermes-audit.yml",)  # cascade-on-merge.yml was retired by T-4186
GROUP = "${{ github.workflow }}-${{ github.ref }}-${{ github.event_name }}"


class WorkflowWiringTest(unittest.TestCase):
    """The concurrency group is only safe WITH the watermark; these bind the two together (P1)."""

    def test_superseding_is_paired_with_the_watermark_tool(self) -> None:
        for name in SUPERSEDABLE:
            with self.subTest(workflow=name):
                doc = _load(name)
                self.assertEqual(doc["concurrency"], {"group": GROUP, "cancel-in-progress": False})
                run = _step(doc, "diff-base")["run"]
                self.assertIn("tools/ci_watermark_base.py", run)
                self.assertIn(f"--workflow {name}", run)  # a rename would 404 -> full scans forever

    def test_permissions_are_least_privilege_and_include_the_lookup_scope(self) -> None:
        for name in SUPERSEDABLE:
            with self.subTest(workflow=name):
                self.assertEqual(_load(name)["permissions"], {"contents": "read", "actions": "read"})

    def test_the_lookup_step_gets_the_token_via_env(self) -> None:
        for name in SUPERSEDABLE:
            with self.subTest(workflow=name):
                step = _step(_load(name), "diff-base")
                self.assertEqual(step["env"]["GITHUB_TOKEN"], "${{ github.token }}")
                self.assertEqual(step["shell"], "bash")  # bash -eo pipefail: a failed tool is red
                self.assertNotIn("${{ github.ref_name }}", step["run"])  # no script injection

    def test_hermes_push_path_scans_per_commit_and_pr_path_is_unchanged(self) -> None:
        run = _step(_load("hermes-audit.yml"), "diff-base")["run"]
        # T-4227: explicit per-parent + raw-bytes producer forms (was `git log -p -m`).
        self.assertIn("git log -p --diff-merges=separate --text --no-textconv --no-ext-diff --format= --no-color", run)
        self.assertIn("git diff --text --no-textconv --no-ext-diff ${BASE}...HEAD", run)  # PR path: cumulative
        self.assertNotIn("git diff ${BEFORE}", run)

    def test_branch_is_passed_with_an_equals_sign_so_a_leading_dash_is_not_a_flag(self) -> None:
        # T-4199 N4: `--branch "${BRANCH}"` lets argparse read a branch named `-x` as an option.
        run = _step(_load("hermes-audit.yml"), "diff-base")["run"]
        self.assertIn('--branch="${BRANCH}"', run)
        self.assertNotIn('--branch "', run)

    def test_hermes_scans_a_full_history_fallback_rather_than_refusing_it(self) -> None:
        # Its full scan is ~75 s, so the fallback is its safety net. (The cascade workflow, which
        # had to refuse a full-history walk, is retired.)
        self.assertNotIn('= "HEAD"', _step(_load("hermes-audit.yml"), "diff-base")["run"])

    def test_cascade_on_merge_stays_retired(self) -> None:
        # T-4186: it could never push (read-only GITHUB_TOKEN; protect-main's bypass actors are a
        # deploy key and one user), so it never produced a single cascade commit while costing two
        # runner jobs per ticket push. tools/cascade_sweep.py (seat-side, level-triggered) replaced it.
        self.assertFalse((WORKFLOWS / "cascade-on-merge.yml").exists())
        self.assertFalse((REPO_ROOT / "tools" / "cascade_diff_scan.py").exists())

    def test_tool_and_test_and_workflows_are_free_of_the_trigger_bytes(self) -> None:
        paths = [
            REPO_ROOT / "tools" / "ci_watermark_base.py", Path(__file__),
            *(WORKFLOWS / n for n in SUPERSEDABLE), WORKFLOWS / "ci.yml",
        ]
        for p in paths:
            with self.subTest(path=p.name):
                self.assertNotIn(TRIGGER, p.read_text(encoding="utf-8"))


# T-4940: isolate the seat's state dir for plain-unittest runs (pytest's conftest
# _isolate_state_dir already does this per test). See tools/_seat_state_isolation.py.
try:
    from tools import _seat_state_isolation
except ImportError:  # pragma: no cover - run from tools/
    import _seat_state_isolation  # type: ignore[no-redef]


def setUpModule() -> None:
    _seat_state_isolation.start()


def tearDownModule() -> None:
    _seat_state_isolation.stop()


if __name__ == "__main__":
    unittest.main()
