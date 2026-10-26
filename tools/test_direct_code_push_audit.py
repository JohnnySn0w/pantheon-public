"""Tests for tools/direct_code_push_audit.py -- the server-side backstop for the seat
pre-push guard (T-4226).

Run from repo root:
    python3 -m unittest tools.test_direct_code_push_audit -v

WHY THE TOOL EXISTS. `.githooks/pre-push` stops a seat pushing CODE (tools/, .github/,
.githooks/) straight to main, but `--no-verify` and a hookless clone bypass a client-side
hook. This audit runs on origin after every push that touches a code path and flags any code
commit with no MERGED PR behind it (GitHub `commits/{sha}/pulls`). It can only ALERT: the
push has already landed.

Conventions here:
  - Range / commit-listing cases use a REAL temp git repo; only the HTTP call is faked.
  - The PR-association shapes were captured from the real API on 2026-09-29 (D11), not
    invented: a squash-merged commit and every commit of a `--merge` PR (merge commit AND
    branch commits) return the one merged PR; a directly pushed commit returns `[]`.
  - Fixture-string note: the byte-sequence trigger is built at runtime via source-time
    concatenation ("HERME" + "S.md") so the contiguous bytes never appear in this file.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import yaml

from tools import direct_code_push_audit as dcpa

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "direct-code-push-audit.yml"
HOOK = REPO_ROOT / ".githooks" / "pre-push"
TRIGGER = "HERME" + "S.md"  # never written contiguously in source
ZERO = "0" * 40
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Seat Author",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "Seat Author",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}

# Shapes captured from the real API (see module docstring).
MERGED_527 = [{"number": 527, "state": "closed", "merged_at": "2026-09-29T00:26:44Z",
               "base": {"ref": "main"}}]
MERGED_523 = [{"number": 523, "state": "closed", "merged_at": "2026-09-28T22:16:56Z",
               "base": {"ref": "main"}}]
OPEN_ONLY = [{"number": 900, "state": "open", "merged_at": None, "base": {"ref": "main"}}]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, env=GIT_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def commit(repo: Path, relpath: str, subject: str | None = None) -> str:
    f = repo / relpath
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text((f.read_text() if f.exists() else "") + "x\n")
    git(repo, "add", relpath)
    git(repo, "commit", "-q", "-m", subject or f"touch {relpath}")
    return git(repo, "rev-parse", "HEAD")


class RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        git(self.repo, "init", "-q", "-b", "main")
        self.base = commit(self.repo, "base.txt", "base")


class FakeApi:
    """Records calls; answers per-sha from a mapping (default: no PR)."""

    def __init__(self, mapping=None, script=None):
        self.mapping = mapping or {}
        self.script = script  # optional {sha: [answer1, answer2, ...]} consumed in order
        self.calls: list[str] = []

    def __call__(self, sha: str) -> list:
        self.calls.append(sha)
        if self.script and sha in self.script:
            answers = self.script[sha]
            return answers.pop(0) if len(answers) > 1 else answers[0]
        return self.mapping.get(sha, [])


class SourceCleanTest(unittest.TestCase):
    def test_new_files_do_not_contain_the_trigger_bytes(self) -> None:
        for path in (Path(dcpa.__file__), Path(__file__), WORKFLOW):
            with self.subTest(path=path.name):
                self.assertNotIn(TRIGGER.encode(), path.read_bytes())


class CodePathTest(unittest.TestCase):
    def test_code_roots_are_code(self) -> None:
        for p in ("tools/x.py", "tools/sub/y.sh", ".github/workflows/ci.yml", ".githooks/pre-push"):
            with self.subTest(path=p):
                self.assertTrue(dcpa.is_code_path(p))

    def test_non_code_paths(self) -> None:
        for p in ("tickets/active/T-1.md", "ledger/2026-W40/x.jsonl", "notes/x.md", "toolsx/a.py",
                  "pinned-rules/recovery.md", "README.md"):
            with self.subTest(path=p):
                self.assertFalse(dcpa.is_code_path(p))

    def test_spine_readme_is_the_one_exemption_and_exact(self) -> None:
        self.assertFalse(dcpa.is_code_path("tools/README.md"))
        for p in ("tools/systemd/README.md", "tools/sub/README.md", "tools/README.md.py",
                  "tools/README.mdx"):
            with self.subTest(path=p):
                self.assertTrue(dcpa.is_code_path(p))

    def test_regexes_match_the_client_side_hook(self) -> None:
        """One definition of 'code path' across the hook and the audit: drift between
        them would make the backstop flag (or miss) what the hook allows (or blocks)."""
        text = HOOK.read_text(encoding="utf-8")
        self.assertIn(f"CODE_RE='{dcpa.CODE_RE}'", text)
        self.assertIn(f"CODE_EXEMPT_RE='{dcpa.EXEMPT_RE}'", text)

    def test_exemption_needs_the_whole_name_including_a_trailing_newline(self) -> None:
        """Python's `$` matches BEFORE a trailing newline, so `tools/README.md<LF>` (one
        file, name ending in a newline) would take the exemption; bash's does not. The
        audit anchors the exemption with \\Z so the two agree."""
        self.assertTrue(re.match(r"^tools/README\.md$", "tools/README.md\n"),
                        "premise: plain `$` really does match before a trailing newline")
        self.assertTrue(dcpa.is_code_path("tools/README.md\n"))
        self.assertTrue(dcpa.is_code_path("tools/README.md\nx"))



class ParsePathsTest(unittest.TestCase):
    """`git diff-tree -z --name-only <sha>` prints NUL-separated names and NOTHING else:
    there is no header to forge, so a name is never split, quoted, or confused with
    anything (N-a: the earlier one-log parse split on a header byte a filename could
    hold)."""

    def test_splits_on_nul_only(self) -> None:
        self.assertEqual(dcpa.parse_paths("tools/a.py\0tickets/x.md\0"),
                         ("tools/a.py", "tickets/x.md"))

    def test_a_name_holding_a_newline_or_control_bytes_stays_one_path(self) -> None:
        name = "tools/README.md\nx\x01y\x02z"
        self.assertEqual(dcpa.parse_paths(f"{name}\0"), (name,))

    def test_empty_output_and_trailing_nul(self) -> None:
        self.assertEqual(dcpa.parse_paths(""), ())
        self.assertEqual(dcpa.parse_paths("\0"), ())


class ListCodeCommitsTest(RepoCase):
    def _list(self, *range_args: str):
        return dcpa.list_code_commits(list(range_args), cwd=self.repo)

    def test_control_bytes_in_a_name_cannot_hide_a_code_path(self) -> None:
        """N-a: parsing one `git log` stream on \\x01/\\x02 let a filename holding those
        bytes (e.g. `docs/a\\x01zz\\x02q`, sorting before tools/) truncate the commit's
        paths, and the remainder chunk was silently dropped -> "no code-path commits",
        exit 0. A commit's paths now come from a per-commit diff-tree of NUL-separated
        names, with no header to forge."""
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "a\x01zz\x02q").write_text("x\n")
        (self.repo / "tools").mkdir()
        (self.repo / "tools" / "evil.py").write_text("x\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "sneaky")
        sha = git(self.repo, "rev-parse", "HEAD")
        got = self._list(f"{self.base}..HEAD")
        self.assertEqual([c.sha for c in got], [sha])
        self.assertEqual(got[0].paths, ("tools/evil.py",))

    def test_a_forged_header_in_a_name_cannot_misattribute_paths(self) -> None:
        """A name shaped like a valid header (`\\x01<40 hex>\\x02`) is just a name."""
        forged = "tools/x\x01" + "f" * 40 + "\x02"
        (self.repo / "tools").mkdir()
        (self.repo / forged).write_text("x\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "forged")
        sha = git(self.repo, "rev-parse", "HEAD")
        got = self._list(f"{self.base}..HEAD")
        self.assertEqual([(c.sha, c.paths) for c in got], [(sha, (forged,))])

    def test_a_root_commit_is_audited(self) -> None:
        """diff-tree needs --root or a root commit's paths would read as empty."""
        git(self.repo, "checkout", "-q", "--orphan", "fresh")
        git(self.repo, "rm", "-rf", "-q", ".")
        root = commit(self.repo, "tools/first.py")
        got = self._list("-1", root)
        self.assertEqual([(c.sha, c.paths) for c in got], [(root, ("tools/first.py",))])

    def test_a_failing_per_commit_diff_raises_rather_than_reading_as_clean(self) -> None:
        """A commit whose paths cannot be read must never look like 'no code paths'."""
        commit(self.repo, "tools/x.py")
        real = dcpa._git

        def flaky(args, *, cwd):
            if args and args[0] == "diff-tree":
                return subprocess.CompletedProcess(args, 128, "", "fatal: bad object")
            return real(args, cwd=cwd)

        with mock.patch.object(dcpa, "_git", flaky):
            with self.assertRaises(dcpa.ApiError):
                self._list(f"{self.base}..HEAD")

    def test_an_unrecognised_sha_token_raises_rather_than_being_skipped(self) -> None:
        """rev-list output this tool cannot interpret (e.g. a 64-hex SHA-256 sha, or
        garbage) used to be `continue`d past, so the audit read clean without having looked
        at that commit. It must raise (-> exit 2), never silently narrow the scan."""
        real = dcpa._git
        for label, token in {"sha-256 (64 hex)": "a" * 64, "garbage": "not-a-sha",
                             "too short": "abc12"}.items():
            with self.subTest(token=label):
                def fake(args, *, cwd, _tok=token):
                    if args and args[0] == "rev-list":
                        return subprocess.CompletedProcess(args, 0, _tok + "\n", "")
                    return real(args, cwd=cwd)

                with mock.patch.object(dcpa, "_git", fake):
                    with self.assertRaisesRegex(dcpa.ApiError, "unrecognised"):
                        self._list(f"{self.base}..HEAD")

    def test_a_metadata_timeout_is_cosmetic_not_fatal(self) -> None:
        """N-d: author/subject only decorate the annotation; a timeout fetching them
        must blank them, not abort the audit (which would exit 2 over a cosmetic step)."""
        sha = commit(self.repo, "tools/x.py")
        real = dcpa._git

        def slow_metadata(args, *, cwd):
            if args and args[0] == "log" and "--no-walk=unsorted" in args:
                raise subprocess.TimeoutExpired(args, 60)
            return real(args, cwd=cwd)

        with mock.patch.object(dcpa, "_git", slow_metadata):
            got = self._list(f"{self.base}..HEAD")
        self.assertEqual([(c.sha, c.author, c.subject) for c in got], [(sha, "", "")])

    def test_ticket_and_ledger_commits_are_not_listed(self) -> None:
        commit(self.repo, "tickets/active/T-1.md")
        commit(self.repo, "ledger/2026-W40/x.jsonl")
        self.assertEqual(self._list(f"{self.base}..HEAD"), [])

    def test_code_commit_is_listed_with_only_its_code_paths(self) -> None:
        commit(self.repo, "tools/x.py")
        sha = commit(self.repo, "tools/y.py")
        (self.repo / "tickets").mkdir()
        (self.repo / "tickets" / "T-2.md").write_text("t\n")
        (self.repo / "tools" / "y.py").write_text("changed\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "mixed")
        mixed = git(self.repo, "rev-parse", "HEAD")
        got = {c.sha: c for c in self._list(f"{self.base}..HEAD")}
        self.assertIn(sha, got)
        self.assertEqual(got[mixed].paths, ("tools/y.py",))

    def test_spine_readme_only_commit_is_exempt(self) -> None:
        commit(self.repo, "tools/README.md")
        self.assertEqual(self._list(f"{self.base}..HEAD"), [])

    def test_readme_plus_code_lists_only_the_code_path(self) -> None:
        (self.repo / "tools").mkdir()
        (self.repo / "tools" / "README.md").write_text("r\n")
        (self.repo / "tools" / "z.py").write_text("z\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "both")
        got = self._list(f"{self.base}..HEAD")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].paths, ("tools/z.py",))

    def test_range_is_respected(self) -> None:
        old = commit(self.repo, "tools/old.py")
        new = commit(self.repo, "tools/new.py")
        self.assertEqual([c.sha for c in self._list(f"{old}..HEAD")], [new])

    def test_merge_commit_is_excluded_but_its_branch_commits_are_listed(self) -> None:
        """The `--merge` PR shape: the merge commit itself is skipped (no diff of its
        own) and every branch commit is audited individually."""
        git(self.repo, "checkout", "-q", "-b", "agent/T-1")
        b1 = commit(self.repo, "tools/b1.py")
        b2 = commit(self.repo, "tools/b2.py")
        git(self.repo, "checkout", "-q", "main")
        commit(self.repo, "tickets/active/T-3.md")  # forces a real (non-ff) merge
        git(self.repo, "merge", "--no-ff", "-q", "-m", "Merge pull request #523", "agent/T-1")
        merge = git(self.repo, "rev-parse", "HEAD")
        got = [c.sha for c in self._list(f"{self.base}..HEAD")]
        self.assertEqual(set(got), {b1, b2})
        self.assertNotIn(merge, got)

    def test_a_newline_in_a_name_cannot_take_the_readme_exemption(self) -> None:
        """Same class as the hook's N1b: `git log --name-only` newline-separated
        `tools/README.md<LF>x` into the exempt path plus `x`, so the commit vanished
        from the audit. The name must arrive whole."""
        for rel in ("tools/README.md\nx", "tools/README.md\n"):
            with self.subTest(path=rel):
                before = git(self.repo, "rev-parse", "HEAD")
                sha = commit(self.repo, rel)
                got = self._list(f"{before}..HEAD")
                self.assertEqual([c.sha for c in got], [sha])
                self.assertEqual(got[0].paths, (rel,))

    def test_invalid_utf8_path_is_listed_not_a_traceback(self) -> None:
        """N3: a name holding an invalid-UTF-8 byte made `git log`'s output undecodable,
        so the audit died with UnicodeDecodeError. It must list the commit instead
        (the bytes survive as lone surrogates via surrogateescape)."""
        before = git(self.repo, "rev-parse", "HEAD")
        sha = commit(self.repo, "tools/caf\udce9.py")  # surrogateescape = raw \xe9
        got = self._list(f"{before}..HEAD")
        self.assertEqual([c.sha for c in got], [sha])
        self.assertTrue(dcpa.is_code_path(got[0].paths[0]))

    def test_unusual_filenames_are_still_code(self) -> None:
        """A non-ASCII / quote-bearing name under tools/ must not dodge the audit (the
        same trick the client-side hook closes)."""
        for rel in ("tools/café.py", 'tools/a"b.py'):
            with self.subTest(path=rel):
                before = git(self.repo, "rev-parse", "HEAD")
                sha = commit(self.repo, rel)
                got = self._list(f"{before}..HEAD")
                self.assertEqual([c.sha for c in got], [sha])

    def test_commit_metadata_is_captured(self) -> None:
        commit(self.repo, "tools/x.py", "the subject")
        c = self._list(f"{self.base}..HEAD")[0]
        self.assertEqual((c.author, c.subject), ("Seat Author", "the subject"))


class ResolveRangeTest(RepoCase):
    def test_normal_push_is_before_dotdot_sha(self) -> None:
        head = commit(self.repo, "tools/x.py")
        self.assertEqual(dcpa.resolve_range_args(self.base, head, cwd=self.repo),
                         [f"{self.base}..{head}"])

    def test_new_branch_all_zero_before_checks_only_the_head_commit(self) -> None:
        head = commit(self.repo, "tools/x.py")
        self.assertEqual(dcpa.resolve_range_args(ZERO, head, cwd=self.repo), ["-1", head])

    def test_unreachable_before_checks_only_the_head_commit(self) -> None:
        head = commit(self.repo, "tools/x.py")
        self.assertEqual(dcpa.resolve_range_args("1" * 40, head, cwd=self.repo), ["-1", head])

    def test_empty_before_checks_only_the_head_commit(self) -> None:
        head = commit(self.repo, "tools/x.py")
        self.assertEqual(dcpa.resolve_range_args("", head, cwd=self.repo), ["-1", head])


class HasMergedPrTest(unittest.TestCase):
    def test_squash_shape(self) -> None:
        self.assertTrue(dcpa.has_merged_pr(MERGED_527))

    def test_empty_means_direct_push(self) -> None:
        self.assertFalse(dcpa.has_merged_pr([]))

    def test_open_only_pr_is_not_review(self) -> None:
        self.assertFalse(dcpa.has_merged_pr(OPEN_ONLY))

    def test_any_merged_pr_among_several_counts(self) -> None:
        self.assertTrue(dcpa.has_merged_pr(OPEN_ONLY + MERGED_523))

    def test_malformed_entries_do_not_count(self) -> None:
        self.assertFalse(dcpa.has_merged_pr([None, "x", {}, {"merged_at": ""}]))


def _commit(sha: str, paths=("tools/x.py",)) -> dcpa.Commit:
    return dcpa.Commit(sha=sha, author="A", subject="s", paths=tuple(paths))


class FindUnreviewedTest(unittest.TestCase):
    def _find(self, commits, api, **kw):
        sleeps: list[float] = []
        out = dcpa.find_unreviewed(commits, fetch=api, sleep=sleeps.append, **kw)
        return out, sleeps

    def test_squash_merged_commit_is_reviewed(self) -> None:
        api = FakeApi({"aaa": MERGED_527})
        out, sleeps = self._find([_commit("aaa")], api)
        self.assertEqual(out, [])
        self.assertEqual(sleeps, [])  # nothing pending -> no wait

    def test_merge_pr_shape_merge_commit_and_branch_commits_all_resolve_to_the_pr(self) -> None:
        """`--merge` (stacked series): the API returns the one merged PR for the merge
        commit AND for each branch commit (spot-checked on #523 and #526)."""
        api = FakeApi({"m": MERGED_523, "b1": MERGED_523, "b2": MERGED_523})
        out, _ = self._find([_commit("b1"), _commit("b2"), _commit("m")], api)
        self.assertEqual(out, [])

    def test_merge_pr_shape_twin_branch_commits_are_checked_individually(self) -> None:
        """Non-vacuous twin: if only the merge commit resolved, the branch commits WOULD
        be flagged -- the audit does not take a merge commit's PR on the branch's behalf."""
        api = FakeApi({"m": MERGED_523})
        out, _ = self._find([_commit("b1"), _commit("b2")], api)
        self.assertEqual([c.sha for c in out], ["b1", "b2"])

    def test_direct_push_is_flagged_after_all_attempts(self) -> None:
        api = FakeApi({})
        out, sleeps = self._find([_commit("aaa")], api, attempts=3, delay=5.0)
        self.assertEqual([c.sha for c in out], ["aaa"])
        self.assertEqual(api.calls, ["aaa", "aaa", "aaa"])
        self.assertEqual(sleeps, [5.0, 5.0])

    def test_association_lag_is_absorbed_by_the_retry(self) -> None:
        api = FakeApi(script={"aaa": [[], MERGED_527]})
        out, sleeps = self._find([_commit("aaa")], api)
        self.assertEqual(out, [])
        self.assertEqual(len(sleeps), 1)

    def test_only_the_still_pending_commits_are_rechecked_and_the_wait_is_shared(self) -> None:
        api = FakeApi({"ok": MERGED_527})
        out, sleeps = self._find([_commit("ok"), _commit("bad1"), _commit("bad2")], api,
                                 attempts=3, delay=1.0)
        self.assertEqual([c.sha for c in out], ["bad1", "bad2"])
        self.assertEqual(api.calls.count("ok"), 1)
        self.assertEqual(len(sleeps), 2)  # O(attempts) waits, not O(commits)

    def test_open_only_pr_is_flagged(self) -> None:
        api = FakeApi({"aaa": OPEN_ONLY})
        out, _ = self._find([_commit("aaa")], api, attempts=1)
        self.assertEqual([c.sha for c in out], ["aaa"])

    def test_api_error_propagates(self) -> None:
        def boom(sha: str) -> list:
            raise dcpa.ApiError("HTTP 403")
        with self.assertRaises(dcpa.ApiError):
            dcpa.find_unreviewed([_commit("aaa")], fetch=boom, sleep=lambda s: None)


class AnnotationTest(unittest.TestCase):
    def test_names_sha_author_subject_and_paths(self) -> None:
        c = dcpa.Commit("a" * 40, "Recovery", "tools: fix thing", ("tools/x.py", ".github/y.yml"))
        line = dcpa.annotation(c)
        self.assertTrue(line.startswith("::error "))
        for needle in ("aaaaaaa", "Recovery", "tools: fix thing", "tools/x.py", ".github/y.yml"):
            self.assertIn(needle, line)

    def test_lone_surrogates_render_printable_not_as_an_encode_error(self) -> None:
        """A path/author holding an invalid-UTF-8 byte arrives as a lone surrogate;
        printing it to a UTF-8 stdout would raise UnicodeEncodeError. Render it
        readably (`\\xe9`) instead."""
        c = dcpa.Commit("c" * 40, "au\udce9thor", "su\udce9bject", ("tools/caf\udce9.py",))
        line = dcpa.annotation(c)
        line.encode("utf-8")  # must not raise
        self.assertIn("tools/caf\\xe9.py", line)

    def test_hostile_commit_text_cannot_inject_a_workflow_command(self) -> None:
        c = dcpa.Commit("b" * 40, "x\n::warning::pwned", "s\r\n::set-output name=a::b%",
                        ("tools/x.py",))
        line = dcpa.annotation(c)
        self.assertEqual(len(line.splitlines()), 1)
        self.assertNotIn("\r", line)
        self.assertIn("%0A", line)
        self.assertIn("%25", line)


class GithubFetchTest(unittest.TestCase):
    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fetch(self, urlopen):
        return dcpa.github_fetch("Owner/repo", "ghs_TOKEN", urlopen=urlopen, sleep=lambda s: None)

    def test_request_shape_and_json_body(self) -> None:
        seen = {}

        def urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["auth"] = req.get_header("Authorization")
            return self._Resp(b'[{"number": 527, "merged_at": "2026-09-29T00:26:44Z"}]')

        prs = self._fetch(urlopen)("abc1234")
        self.assertEqual(prs[0]["number"], 527)
        self.assertEqual(seen["url"], "https://api.github.com/repos/Owner/repo/commits/abc1234/pulls")
        self.assertEqual(seen["auth"], "Bearer ghs_TOKEN")

    def test_transient_5xx_is_retried_then_succeeds(self) -> None:
        calls = {"n": 0}

        def urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(req.full_url, 502, "bad gateway", {}, None)
            return self._Resp(b"[]")

        self.assertEqual(self._fetch(urlopen)("abc1234"), [])
        self.assertEqual(calls["n"], 2)

    def test_403_is_an_api_error_that_never_leaks_the_token(self) -> None:
        def urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, None)

        with self.assertRaises(dcpa.ApiError) as cm:
            self._fetch(urlopen)("abc1234")
        self.assertIn("403", str(cm.exception))
        self.assertNotIn("ghs_TOKEN", str(cm.exception))

    def test_network_failure_after_retries_is_an_api_error(self) -> None:
        def urlopen(req, timeout=None):
            raise urllib.error.URLError("no route")

        with self.assertRaisesRegex(dcpa.ApiError, "after 3 attempts"):
            self._fetch(urlopen)("abc1234")

    def test_malformed_body_is_an_api_error(self) -> None:
        with self.assertRaisesRegex(dcpa.ApiError, "malformed JSON"):
            self._fetch(lambda req, timeout=None: self._Resp(b"{not json"))("abc1234")

    def test_non_list_body_is_an_api_error(self) -> None:
        with self.assertRaisesRegex(dcpa.ApiError, "not a list"):
            self._fetch(lambda req, timeout=None: self._Resp(b'{"message": "x"}'))("abc1234")

    def test_unusable_repo_name_is_rejected(self) -> None:
        for repo in ("../etc", "owner/..", "a/b/c", "", "owner"):
            with self.subTest(repo=repo):
                with self.assertRaisesRegex(dcpa.ApiError, "repository name"):
                    dcpa.github_fetch(repo, "t")

    def test_non_hex_sha_is_rejected_before_any_request(self) -> None:
        def urlopen(req, timeout=None):
            raise AssertionError("must not be called")

        with self.assertRaisesRegex(dcpa.ApiError, "commit sha"):
            self._fetch(urlopen)("../../x")


class MainTest(RepoCase):
    def _run(self, before: str, sha: str, api) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dcpa.main(["--before", before, "--sha", sha], fetch=api, cwd=self.repo)
        return code, out.getvalue(), err.getvalue()

    def test_no_code_commits_is_clean_and_makes_no_api_call(self) -> None:
        head = commit(self.repo, "tickets/active/T-1.md")
        api = FakeApi()
        code, out, _ = self._run(self.base, head, api)
        self.assertEqual(code, dcpa.EXIT_CLEAN)
        self.assertEqual(api.calls, [])

    def test_pr_merged_code_is_clean(self) -> None:
        head = commit(self.repo, "tools/x.py")
        code, _, _ = self._run(self.base, head, FakeApi({head: MERGED_527}))
        self.assertEqual(code, dcpa.EXIT_CLEAN)

    def test_direct_code_push_exits_1_with_an_annotation(self) -> None:
        head = commit(self.repo, "tools/x.py", "sneaky direct push")
        code, out, _ = self._run(self.base, head, FakeApi())
        self.assertEqual(code, dcpa.EXIT_UNREVIEWED)
        self.assertIn("::error ", out)
        self.assertIn("tools/x.py", out)
        self.assertIn("sneaky direct push", out)

    def test_api_failure_exits_2_not_silently_clean(self) -> None:
        head = commit(self.repo, "tools/x.py")

        def boom(sha: str) -> list:
            raise dcpa.ApiError("HTTP 403")

        code, _, err = self._run(self.base, head, boom)
        self.assertEqual(code, dcpa.EXIT_API_FAILURE)
        self.assertIn("403", err)

    def test_invalid_utf8_code_path_exits_1_with_an_encodable_annotation(self) -> None:
        """N3: was a UnicodeDecodeError traceback and exit 1 by accident, not the
        documented contract. Now a clean exit 1 (unreviewed code) whose output a
        UTF-8 stdout can carry."""
        head = commit(self.repo, "tools/caf\udce9.py", "sneaky")
        code, out, err = self._run(self.base, head, FakeApi())
        self.assertEqual(code, dcpa.EXIT_UNREVIEWED)
        out.encode("utf-8")
        err.encode("utf-8")
        self.assertIn("::error ", out)

    def test_an_unreadable_commit_is_exit_2_not_a_silent_pass(self) -> None:
        """N-a: if one commit's paths cannot be read the audit must say so (2), never
        report the push as having no code-path commits (0)."""
        head = commit(self.repo, "tools/x.py")
        real = dcpa._git

        def flaky(args, *, cwd):
            if args and args[0] == "diff-tree":
                return subprocess.CompletedProcess(args, 128, "", "fatal: bad object")
            return real(args, cwd=cwd)

        with mock.patch.object(dcpa, "_git", flaky):
            code, out, err = self._run(self.base, head, FakeApi())
        self.assertEqual(code, dcpa.EXIT_API_FAILURE)
        self.assertIn("could not verify", err)

    def test_a_decode_or_value_error_anywhere_is_exit_2_not_a_traceback(self) -> None:
        """Belt and braces: any ValueError (UnicodeDecodeError is one) escaping the
        audit is 'could not verify' -> exit 2, never an uncaught traceback."""
        head = commit(self.repo, "tools/x.py")

        def boom(sha: str) -> list:
            raise UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid continuation byte")

        code, out, err = self._run(self.base, head, boom)
        self.assertEqual(code, dcpa.EXIT_API_FAILURE)
        self.assertIn("could not verify", err)

    def test_missing_token_or_repo_exits_2(self) -> None:
        head = commit(self.repo, "tools/x.py")
        old = {k: os.environ.pop(k, None) for k in ("GITHUB_TOKEN", "GITHUB_REPOSITORY")}
        self.addCleanup(lambda: os.environ.update({k: v for k, v in old.items() if v}))
        out, err = io.StringIO(), io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dcpa.main(["--before", self.base, "--sha", head], cwd=self.repo)
        self.assertEqual(code, dcpa.EXIT_API_FAILURE)


class WorkflowStructureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        cls.on = cls.doc.get("on", cls.doc.get(True))  # PyYAML reads a bare `on` as True
        cls.job = next(iter(cls.doc["jobs"].values()))

    def test_fires_only_on_pushes_to_main_that_touch_a_code_path(self) -> None:
        self.assertEqual(set(self.on), {"push"})
        self.assertEqual(self.on["push"]["branches"], ["main"])
        self.assertEqual(set(self.on["push"]["paths"]),
                         {".github/**", ".githooks/**", "tools/**"})

    def test_token_is_read_only_and_least_privilege(self) -> None:
        self.assertEqual(self.doc["permissions"], {"contents": "read", "pull-requests": "read"})

    def test_no_concurrency_group_because_the_range_is_per_push(self) -> None:
        """Superseding a queued run would lose that run's commits: this scanner has no
        watermark (T-2344 / T-4182 reasoning), so it must never be cancelled."""
        self.assertNotIn("concurrency", self.doc)

    def test_self_hosted_pve2_and_bounded(self) -> None:
        self.assertIn("self-hosted", self.job["runs-on"])
        self.assertIn("pve2", self.job["runs-on"])
        self.assertIsInstance(self.job["timeout-minutes"], int)
        self.assertLessEqual(self.job["timeout-minutes"], 15)

    def test_runs_the_tool_with_full_history_and_the_push_range(self) -> None:
        checkout = next(s for s in self.job["steps"] if "checkout" in s.get("uses", ""))
        self.assertEqual(checkout["with"]["fetch-depth"], 0)
        step = next(s for s in self.job["steps"]
                    if "direct_code_push_audit.py" in s.get("run", ""))
        self.assertIn("--before", step["run"])
        self.assertIn("--sha", step["run"])
        # attacker-influenced values reach the shell through env, never ${{ }} in run:
        self.assertNotIn("${{", step["run"])
        self.assertEqual(step["env"]["GITHUB_TOKEN"], "${{ github.token }}")


# T-4940: isolate the seat's state dir for plain-unittest runs (pytest's conftest
# _isolate_state_dir already does this per test). See tools/_seat_state_isolation.py.
# The guard in test_seat_state_isolation_guard.py requires it of any module whose text
# matches a push-terminated list (here the workflow's `on: push` key assertions).
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
