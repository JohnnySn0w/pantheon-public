"""Unit tests for tools/watch_runner.py.

Run from repo root:
    python3 -m unittest tools.test_watch_runner -v

Covers the auto-commit-and-push subsystem (HERMES guard, scope filter, commit
message construction, empty-fire short-circuit). Also covers the B11 actuation
phase (topic policy, finding extraction, idempotency, file_finding argv +
retries, ledger writes, summary log) and the ORG-025 in-session modes
(--emit-due-briefs, --land-commit, --file-watch-ticket) + the default-safe gate.
Does NOT shell out to git or claude; subprocess primitives are mocked.

NOTE (T-1209): the legacy headless `--commit-prod` dispatch path was retired,
along with its tests — dispatch_researcher stream-json parsing, classify_failure,
is_ho_in_hit_window freshness check, and the main() pre-fire-skip / repo-root
worktree flows. The live surface is the four in-session modes above.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import _git_utils, ticket, watch_runner
from tools.watch_runner import Finding, Topic


def _topic(slug: str) -> Topic:
    """Minimal Topic instance for tests; only `slug` is read by the commit path."""
    return Topic(
        slug=slug,
        cadence="daily",
        budget=300,
        scope="(test scope)",
        seed_sources=[],
        notes="",
    )


def _landed_report_text(headline: str = "Test headline.", body: str = "") -> str:
    """Build report text shaped like a real landed report — frontmatter with a
    `headline:` key, plus at least one `## ` section — padded past
    `watch_runner.REPORT_MIN_BYTES` so it passes `report_landed_ok` (T-1581).

    Mirrors the real shape of
    `knowledge/watch/reports/2026-07-06/llm-agent-failure-modes-research.md`:
    leading frontmatter block, `# ` title, `## `-headed sections, `## Sources`.
    Pulled out as a pure string-builder (rather than folded into
    `_write_landed_report` below) so `tools/test_git_utils_integ.py`'s
    dict-of-content fixtures (`_seed_commit`) can import and reuse the exact
    same shape instead of hand-rolling a second one.
    """
    filler = body or ("Filler body text to cross the substantive-report size floor. " * 10)
    return (
        f"---\nsurface: routine\nheadline: {headline}\n---\n\n"
        f"# Test report\n\n"
        f"## Findings\n\n{filler}\n\n"
        f"## Sources\n\n- (test)\n"
    )


def _write_landed_report(path: Path, headline: str = "Test headline.", body: str = "") -> None:
    """Write `_landed_report_text(...)` to `path` — see that function for the
    shape rationale. Used anywhere a test needs a report fixture file that is
    genuinely "landed" rather than merely present.
    """
    path.write_text(_landed_report_text(headline=headline, body=body), encoding="utf-8")


class TestHermesGuardScan(unittest.TestCase):
    """The HERMES guard rejects only the unescaped `HERMES\\.md` substring.
    Files using the discipline-escaped form `HERMES\\.md` (literal backslash
    between S and .) must pass cleanly.

    Fixture-string note: the trigger byte sequence is constructed at runtime
    via concatenation (`"HERME" + "S.md"`-style) so the contiguous 9-byte
    sequence never appears in source. This protects `git log -p` of this repo
    from re-introducing the trigger phrase that was scrubbed from history.
    Concern is separate from runtime guard scope — this file is outside the
    guard's scan path (`knowledge/watch/reports/<date>/`), but git-history-hygiene
    discipline applies to every committed source byte.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_clean_file_passes(self) -> None:
        (self.dir / "clean.md").write_text(
            "# clean report\n\nNo trigger phrase anywhere in this file.\n",
            encoding="utf-8",
        )
        ok, detail = watch_runner._hermes_scan_dir(self.dir)
        self.assertTrue(ok)
        self.assertIsNone(detail)

    def test_escaped_form_passes(self) -> None:
        # The discipline form: literal backslash between S and . — `HERMES\.md`.
        # The bytes are H E R M E S \ . m d, so a regex requiring S directly
        # followed by . cannot match.
        (self.dir / "escaped.md").write_text(
            "We refer to the trigger as HERMES\\.md throughout this doc.\n",
            encoding="utf-8",
        )
        ok, detail = watch_runner._hermes_scan_dir(self.dir)
        self.assertTrue(ok, f"escaped form should pass; got detail={detail!r}")
        self.assertIsNone(detail)

    def test_unescaped_form_refused(self) -> None:
        # Source-time concatenation: yields the contiguous 9-byte trigger at
        # runtime, but the literal sequence never appears in source.
        (self.dir / "bad.md").write_text(
            "Line 1 ok\nLine 2 mentions " + "HERME" + "S.md unescaped\nLine 3 ok\n",
            encoding="utf-8",
        )
        ok, detail = watch_runner._hermes_scan_dir(self.dir)
        self.assertFalse(ok)
        self.assertIsNotNone(detail)
        # Detail format: "<path>:<lineno>"
        self.assertIn("bad.md", detail)
        self.assertIn(":2", detail)

    def test_unescaped_in_binary_position_still_caught(self) -> None:
        # Even outside a markdown context, raw bytes match. Source-time
        # concatenation per the fixture-string note above.
        (self.dir / "weird.md").write_text(
            "preamble " + "HERME" + "S.md tail\n", encoding="utf-8"
        )
        ok, detail = watch_runner._hermes_scan_dir(self.dir)
        self.assertFalse(ok)
        self.assertIn("weird.md", detail)


class TestHermesCommitMessageBelt(unittest.TestCase):
    """The commit message itself is also scanned. Belt for an impossible case
    (we construct it from slugs/statuses) but cheap and per spec.
    """

    def test_clean_message_passes(self) -> None:
        msg = "research(watch): land 2026-05-01 reports\n\n1 topic(s): foo\nStatuses: foo=ok\n"
        self.assertTrue(watch_runner._hermes_scan_message(msg))

    def test_unescaped_in_message_refused(self) -> None:
        # Source-time concatenation per the fixture-string note in
        # TestHermesGuardScan; runtime yields the 9-byte trigger, source bytes
        # never contain it contiguously.
        msg = "research(watch): land 2026-05-01 reports\n\nsee " + "HERME" + "S.md\n"
        self.assertFalse(watch_runner._hermes_scan_message(msg))

    def test_escaped_in_message_passes(self) -> None:
        msg = "research(watch): land 2026-05-01 reports\n\nsee HERMES\\.md\n"
        self.assertTrue(watch_runner._hermes_scan_message(msg))


class TestPullRepoStrict(unittest.TestCase):
    """T-0442: pull_repo_strict shape — fail-closed counterpart of
    pull_repo_or_warn. Verifies the FETCH_HEAD-safe fetch+ff-merge two-step,
    success-case empty-msg, and per-step failure attribution.
    """

    def test_success_emits_fetch_then_merge_and_empty_msg(self) -> None:
        from tools._git_utils import pull_repo_strict
        with patch("tools._git_utils.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            ok, msg = pull_repo_strict()
        self.assertTrue(ok)
        self.assertEqual(msg, "")
        self.assertEqual(mock_sub.run.call_count, 2)
        first_args, _ = mock_sub.run.call_args_list[0]
        self.assertEqual(first_args[0], ["git", "fetch", "origin", "main"])
        second_args, _ = mock_sub.run.call_args_list[1]
        self.assertEqual(
            second_args[0],
            ["git", "merge", "--ff-only", "refs/remotes/origin/main"],
        )

    def test_fetch_failure_returns_fetch_step_attribution(self) -> None:
        from tools._git_utils import pull_repo_strict
        with patch("tools._git_utils.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(
                returncode=1, stdout="", stderr="fatal: unable to access"
            )
            ok, msg = pull_repo_strict()
        self.assertFalse(ok)
        self.assertIn("git fetch failed", msg)
        self.assertIn("unable to access", msg)
        # Stops at fetch — does NOT proceed to merge.
        self.assertEqual(mock_sub.run.call_count, 1)

    def test_merge_failure_returns_merge_step_attribution(self) -> None:
        from tools._git_utils import pull_repo_strict
        with patch("tools._git_utils.subprocess") as mock_sub:
            def fake_run(argv, **_kwargs):
                if argv[:2] == ["git", "fetch"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:2] == ["git", "merge"]:
                    return MagicMock(
                        returncode=128, stdout="", stderr="non-fast-forward"
                    )
                return MagicMock(returncode=0, stdout="", stderr="")
            mock_sub.run.side_effect = fake_run
            ok, msg = pull_repo_strict()
        self.assertFalse(ok)
        self.assertIn("git merge --ff-only failed", msg)
        self.assertIn("non-fast-forward", msg)


class TestCommitAndPushScopeFilter(unittest.TestCase):
    """End-to-end on git_commit_and_push with subprocess.run mocked."""

    def setUp(self) -> None:
        # `_git` lives in tools._git_utils post-extraction; patch subprocess.run
        # on that module so the wrapper's call site is intercepted.
        self._patcher = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._patcher.start()
        # Default: every git invocation succeeds with empty stdout.
        self.mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        # Bypass the filesystem HERMES scan — separately tested.
        self._scan_patcher = patch(
            "tools.watch_runner._hermes_scan_dir", return_value=(True, None)
        )
        self._scan_patcher.start()
        # Tmp dir acting as the worktree cwd so the partial-stage guard can
        # find real files (T-0662: guard checks filesystem, not staged index).
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_cwd = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._patcher.stop()
        self._scan_patcher.stop()
        self._tmp.cleanup()

    def _git_calls(self) -> list[list[str]]:
        """Return the argv of each git invocation in order."""
        return [call.args[0] for call in self.mock_run.call_args_list]

    def test_out_of_scope_path_rejects_and_resets(self) -> None:
        date_iso = "2026-05-01"

        def fake_run(argv, **kwargs):
            # pull → ok; add → ok; diff --cached --name-only → leaked path; reset → ok
            if argv[:2] == ["git", "diff"]:
                return MagicMock(returncode=0, stdout="ledger/leaked.md\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        self.mock_run.side_effect = fake_run

        ok, msg = watch_runner.git_commit_and_push(
            date_iso, [_topic("foo")], {"foo": "ok"}
        )

        self.assertFalse(ok)
        self.assertIn("scope", msg.lower())
        # Verify reset HEAD was issued, and commit was NOT.
        argvs = self._git_calls()
        self.assertTrue(
            any(a[:3] == ["git", "reset", "HEAD"] for a in argvs),
            f"expected `git reset HEAD` in calls; got {argvs!r}",
        )
        self.assertFalse(
            any(a[:2] == ["git", "commit"] for a in argvs),
            f"commit must not be invoked when scope guard rejects; got {argvs!r}",
        )
        self.assertFalse(
            any(a[:2] == ["git", "push"] for a in argvs),
            f"push must not be invoked when scope guard rejects; got {argvs!r}",
        )

    def test_in_scope_paths_accepted_full_flow(self) -> None:
        date_iso = "2026-05-01"
        # Create the report file so the partial-stage guard (T-0662/T-1581)
        # passes — must be landed-ok (exists+size+structure), not just present.
        reports_dir = self.tmp_cwd / "knowledge" / "watch" / "reports" / date_iso
        reports_dir.mkdir(parents=True)
        _write_landed_report(reports_dir / "foo.md")

        def fake_run(argv, **kwargs):
            if argv[:2] == ["git", "diff"]:
                return MagicMock(
                    returncode=0,
                    stdout=f"knowledge/watch/reports/{date_iso}/foo.md\nknowledge/watch/reports/{date_iso}/README.md\n",
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        self.mock_run.side_effect = fake_run

        ok, msg = watch_runner.git_commit_and_push(
            date_iso, [_topic("foo")], {"foo": "ok"}, cwd=self.tmp_cwd
        )

        self.assertTrue(ok, f"expected success; got msg={msg!r}")
        argvs = self._git_calls()
        # Order matters: add → diff → commit → push (worktree mode: pull is
        # skipped since cwd is provided; T-0442 fetch+merge pair only runs
        # in legacy cwd=None mode).
        kinds = [a[1] for a in argvs if a[0] == "git"]
        self.assertEqual(kinds, ["add", "diff", "commit", "push"])

    def test_pull_non_ff_refuses(self) -> None:
        date_iso = "2026-05-01"

        def fake_run(argv, **kwargs):
            if argv[:2] in (["git", "pull"], ["git", "fetch"], ["git", "merge"]):
                return MagicMock(returncode=128, stdout="", stderr="non-fast-forward")
            return MagicMock(returncode=0, stdout="", stderr="")

        self.mock_run.side_effect = fake_run

        ok, msg = watch_runner.git_commit_and_push(
            date_iso, [_topic("foo")], {"foo": "ok"}
        )

        self.assertFalse(ok)
        # T-0442: error message now reports "fetch" (the first step of the
        # resilient pull) rather than the legacy "pull --ff-only".
        self.assertIn("fetch", msg.lower())
        argvs = self._git_calls()
        self.assertFalse(any(a[:2] == ["git", "commit"] for a in argvs))
        self.assertFalse(any(a[:2] == ["git", "push"] for a in argvs))


class TestCommitMessageConstruction(unittest.TestCase):
    """The commit message template renders deterministically from inputs."""

    def test_two_topics_mixed_status(self) -> None:
        msg = watch_runner._build_commit_message(
            "2026-05-01",
            [_topic("alpha"), _topic("beta")],
            {"alpha": "ok", "beta": "fail"},
        )
        # Subject line per spec
        self.assertTrue(msg.startswith("research(watch): land 2026-05-01 reports\n"))
        # Body parts
        self.assertIn("2 topic(s): alpha, beta", msg)
        self.assertIn("Statuses: alpha=ok, beta=fail", msg)
        self.assertIn("Auto-committed by watch_runner.py (specialist unattended).", msg)
        self.assertIn("Scope: knowledge/watch/reports/2026-05-01/ only.", msg)
        # No Co-Authored-By trailer (intentional per spec)
        self.assertNotIn("Co-Authored-By", msg)
        self.assertNotIn("Co-authored-by", msg)

    def test_single_topic_render(self) -> None:
        msg = watch_runner._build_commit_message(
            "2026-05-01", [_topic("solo")], {"solo": "ok"}
        )
        self.assertIn("1 topic(s): solo", msg)
        self.assertIn("Statuses: solo=ok", msg)


class TestEmptyFireShortCircuit(unittest.TestCase):
    """If `git diff --cached --name-only` returns empty after staging, the
    function must short-circuit to (True, "no changes") without invoking
    commit or push.
    """

    def setUp(self) -> None:
        self._patcher = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._patcher.start()
        self._scan_patcher = patch(
            "tools.watch_runner._hermes_scan_dir", return_value=(True, None)
        )
        self._scan_patcher.start()
        # Tmp dir acting as worktree cwd. The report file must exist so the
        # partial-stage guard (T-0662) does not fire before the empty-staged
        # short-circuit.  "Nothing staged" means the file was already committed
        # in a prior run and is present in the working tree.
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_cwd = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._patcher.stop()
        self._scan_patcher.stop()
        self._tmp.cleanup()

    def test_empty_staged_short_circuits(self) -> None:
        date_iso = "2026-05-01"
        # Report file exists (already committed in a prior run) — so the
        # partial-stage guard passes (must be landed-ok, T-1581).  git diff
        # --cached returns empty → short-circuit fires.
        reports_dir = self.tmp_cwd / "knowledge" / "watch" / "reports" / date_iso
        reports_dir.mkdir(parents=True)
        _write_landed_report(reports_dir / "foo.md")

        def fake_run(argv, **kwargs):
            if argv[:2] == ["git", "diff"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        self.mock_run.side_effect = fake_run

        ok, msg = watch_runner.git_commit_and_push(
            date_iso, [_topic("foo")], {"foo": "ok"}, cwd=self.tmp_cwd
        )

        self.assertTrue(ok)
        self.assertEqual(msg, "no changes")
        argvs = [call.args[0] for call in self.mock_run.call_args_list]
        self.assertFalse(any(a[:2] == ["git", "commit"] for a in argvs))
        self.assertFalse(any(a[:2] == ["git", "push"] for a in argvs))


# ---------------------------------------------------------------------------
# B11 actuation phase tests
# ---------------------------------------------------------------------------


class TestTopicPolicy(unittest.TestCase):
    def test_known_slug_returns_policy(self) -> None:
        self.assertTrue(
            watch_runner.TOPIC_POLICY["mcp-supply-chain"]["interactive_required"]
        )

    def test_unknown_slug_falls_back(self) -> None:
        got = watch_runner.TOPIC_POLICY.get(
            "nonexistent-topic", watch_runner._DEFAULT_POLICY
        )
        self.assertIs(got, watch_runner._DEFAULT_POLICY)

    def test_default_policy_surfaces(self) -> None:
        self.assertTrue(watch_runner._DEFAULT_POLICY["interactive_required"])


class TestExtractFindings(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.date_dir = self.tmp / "2026-05-04"
        self.date_dir.mkdir()
        # Three reports: ok+TL;DR, fail (skip), ok+no-TL;DR (skip).
        (self.date_dir / "alpha.md").write_text(
            "# Alpha report\n\nBody paragraph for alpha. Some content.\n\n## Section\n\nmore\n",
            encoding="utf-8",
        )
        (self.date_dir / "bravo.md").write_text(
            "# Bravo report\n\nNot used (status=fail).\n",
            encoding="utf-8",
        )
        (self.date_dir / "charlie.md").write_text(
            "# Charlie report\n\nNot used (no TL;DR).\n",
            encoding="utf-8",
        )
        (self.date_dir / "README.md").write_text(
            "# Watch reports — 2026-05-04\n\n"
            "| Topic | Status | One-line summary |\n"
            "|---|---|---|\n"
            "| [alpha](alpha.md) | ok | TL;DR: alpha finding line one |\n"
            "| [bravo](bravo.md) | fail | TL;DR: bravo failed dispatch |\n"
            "| [charlie](charlie.md) | ok | summary lacks TL;DR prefix |\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_only_ok_with_tldr_kept(self) -> None:
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            findings = watch_runner.extract_findings(self.date_dir, {})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].slug, "alpha")
        self.assertEqual(findings[0].tldr, "alpha finding line one")
        self.assertIn("Body paragraph for alpha", findings[0].headline_excerpt)

    def test_skip_reasons_to_stderr(self) -> None:
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            watch_runner.extract_findings(self.date_dir, {})
        out = buf.getvalue()
        self.assertIn("bravo", out)
        self.assertIn("charlie", out)


class TestAlreadyFiled(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tickets_root = Path(self._tmp.name)
        for sub in ("inbox", "active", "done"):
            (self.tickets_root / sub).mkdir(parents=True)
        # Pre-seed an inbox ticket containing the relpath.
        (self.tickets_root / "inbox" / "T-0099.md").write_text(
            "---\nid: T-0099\n---\n\n## Ask\n\n"
            "knowledge/watch/reports/2026-05-04/mcp-supply-chain.md was the source.\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_match_found(self) -> None:
        self.assertTrue(
            watch_runner.already_filed(
                "knowledge/watch/reports/2026-05-04/mcp-supply-chain.md", self.tickets_root
            )
        )

    def test_no_match(self) -> None:
        self.assertFalse(
            watch_runner.already_filed(
                "knowledge/watch/reports/2026-05-04/other.md", self.tickets_root
            )
        )


class TestFileFindingArgv(unittest.TestCase):
    def setUp(self) -> None:
        self.finding = Finding(
            slug="mcp-supply-chain",
            tldr="MCP supply-chain campaign uses SessionStart hook for persistence",
            report_relpath="knowledge/watch/reports/2026-05-04/mcp-supply-chain.md",
            headline_excerpt="excerpt body",
        )
        self.policy = watch_runner.TOPIC_POLICY["mcp-supply-chain"]

    def test_argv_and_outcome(self) -> None:
        with patch("tools.watch_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="filed T-0099 → tickets/inbox/T-0099.md\n",
                stderr="",
            )
            outcome, ticket_id, detail = watch_runner.file_finding(
                self.finding, self.policy
            )
        self.assertEqual(outcome, "filed")
        self.assertEqual(ticket_id, "T-0099")
        self.assertIn("retries=0", detail)

        argv = mock_run.call_args.args[0]
        self.assertIn("file", argv)
        self.assertEqual(argv[argv.index("--requester") + 1], "specialist")
        self.assertEqual(argv[argv.index("--target") + 1], "recovery")
        labels_csv = argv[argv.index("--labels") + 1]
        labels = labels_csv.split(",")
        self.assertEqual(labels[0], "b11")
        self.assertEqual(labels[1], "actuation")
        self.assertEqual(labels[2], "source-topic-mcp-supply-chain")
        self.assertIn("watch-finding", labels)
        self.assertIn("security", labels)
        self.assertIn("interactive-required", labels)

        # env arg passed PANTHEON_ACTOR=specialist
        passed_env = mock_run.call_args.kwargs["env"]
        self.assertEqual(passed_env.get("PANTHEON_ACTOR"), "specialist")


class TestFileFindingRetryThenFail(unittest.TestCase):
    def setUp(self) -> None:
        self.finding = Finding(
            slug="mcp-supply-chain",
            tldr="t",
            report_relpath="knowledge/watch/reports/2026-05-04/mcp-supply-chain.md",
            headline_excerpt="x",
        )
        self.policy = watch_runner.TOPIC_POLICY["mcp-supply-chain"]
        self._tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self._tmp.name) / "log"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_three_retries_then_errored(self) -> None:
        with patch("tools.watch_runner.subprocess.run") as mock_run, \
             patch("tools.watch_runner.time.sleep") as mock_sleep, \
             patch("tools.watch_runner.RUNNER_LOG", self.log_path):
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="git push failed: remote rejected\n",
            )
            outcome, ticket_id, detail = watch_runner.file_finding(
                self.finding, self.policy
            )
        self.assertEqual(outcome, "errored")
        self.assertIsNone(ticket_id)
        self.assertEqual(mock_run.call_count, watch_runner._MAX_ACTUATION_RETRIES)
        # sleep called between attempts (n-1 times for n attempts).
        self.assertEqual(mock_sleep.call_count, watch_runner._MAX_ACTUATION_RETRIES - 1)
        # ERROR line appended to RUNNER_LOG.
        self.assertTrue(self.log_path.exists())
        self.assertIn("ERROR file_finding", self.log_path.read_text(encoding="utf-8"))


class TestFileFindingNoRetryOnHermesFail(unittest.TestCase):
    def test_hermes_failure_no_retry(self) -> None:
        finding = Finding(
            slug="mcp-supply-chain",
            tldr="t",
            report_relpath="knowledge/watch/reports/2026-05-04/mcp-supply-chain.md",
            headline_excerpt="x",
        )
        policy = watch_runner.TOPIC_POLICY["mcp-supply-chain"]
        with patch("tools.watch_runner.subprocess.run") as mock_run, \
             patch("tools.watch_runner.time.sleep"):
            mock_run.return_value = MagicMock(
                returncode=2, stdout="", stderr="HERMES guard refused\n"
            )
            outcome, ticket_id, _ = watch_runner.file_finding(finding, policy)
        self.assertEqual(outcome, "errored")
        self.assertIsNone(ticket_id)
        self.assertEqual(mock_run.call_count, 1)


class TestInvokeTicketFileLogError(unittest.TestCase):
    """T-1583 REBOUND: `_invoke_ticket_file` gained an optional `log_error`
    callable so callers outside this module (tools/s3star_audit_runner.py)
    can attribute failures to their own log file/tag instead of silently
    landing in watch_runner's own RUNNER_LOG under an "[specialist] actuation:"
    tag. Pins the DEFAULT (log_error=None) path byte-identical to the
    pre-REBOUND behavior for every existing in-module caller.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self._tmp.name) / "log"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_default_log_error_none_writes_own_runner_log(self) -> None:
        with patch("tools.watch_runner.subprocess.run") as mock_run, \
             patch("tools.watch_runner.time.sleep"), \
             patch("tools.watch_runner.RUNNER_LOG", self.log_path):
            mock_run.return_value = MagicMock(
                returncode=2, stdout="", stderr="HERMES guard refused\n"
            )
            outcome, ticket_id, detail = watch_runner._invoke_ticket_file(
                ["ticket.py", "file"], "some_tag", {}
            )
        self.assertEqual(outcome, "errored")
        self.assertIsNone(ticket_id)
        logged = self.log_path.read_text(encoding="utf-8")
        self.assertIn("[specialist] actuation: ERROR some_tag", logged)
        self.assertIn(detail, logged)

    def test_log_error_callable_overrides_default_and_bypasses_own_runner_log(self) -> None:
        calls: list[tuple[str, str]] = []

        def _capture(log_tag: str, detail: str) -> None:
            calls.append((log_tag, detail))

        with patch("tools.watch_runner.subprocess.run") as mock_run, \
             patch("tools.watch_runner.time.sleep"), \
             patch("tools.watch_runner.RUNNER_LOG", self.log_path):
            mock_run.return_value = MagicMock(
                returncode=2, stdout="", stderr="HERMES guard refused\n"
            )
            outcome, _tid, detail = watch_runner._invoke_ticket_file(
                ["ticket.py", "file"], "some_tag", {}, log_error=_capture
            )
        self.assertEqual(outcome, "errored")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ("some_tag", detail))
        # The default RUNNER_LOG must NOT have been written when a log_error
        # override is supplied — this is the exact misattribution the
        # REBOUND caught (an external caller's failure landing in
        # watch_runner's own log under an "[specialist]" tag).
        self.assertFalse(self.log_path.exists())


class TestActuatePhaseIdempotency(unittest.TestCase):
    """If a finding's report_relpath already appears in any ticket file, do
    not re-file. Pre-seeded inbox ticket → file_finding never called."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # Mirror REPO_ROOT layout: tickets/, knowledge/watch/reports/<date>/
        (self.tmp / "tickets" / "inbox").mkdir(parents=True)
        for sub in ("active", "done"):
            (self.tmp / "tickets" / sub).mkdir(parents=True)
        date_dir = self.tmp / "watch" / "reports" / "2026-05-04"
        date_dir.mkdir(parents=True)
        (date_dir / "mcp-supply-chain.md").write_text(
            "# MCP supply chain\n\nbody body body\n",
            encoding="utf-8",
        )
        (date_dir / "README.md").write_text(
            "# 2026-05-04\n\n| Topic | Status | One-line summary |\n"
            "|---|---|---|\n"
            "| [mcp-supply-chain](mcp-supply-chain.md) | ok | TL;DR: pretend finding |\n",
            encoding="utf-8",
        )
        (self.tmp / "tickets" / "inbox" / "T-0099.md").write_text(
            "knowledge/watch/reports/2026-05-04/mcp-supply-chain.md\n",
            encoding="utf-8",
        )
        self.date_dir = date_dir

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_already_filed_skipped(self) -> None:
        call_count = {"n": 0}

        def fake_file_finding(*a, **kw):
            call_count["n"] += 1
            return ("filed", "T-9999", "retries=0")

        with patch("tools.watch_runner.REPO_ROOT", self.tmp), \
             patch("tools.watch_runner.REPORTS_ROOT", self.tmp / "watch" / "reports"), \
             patch("tools.watch_runner.file_finding", side_effect=fake_file_finding), \
             patch("tools.watch_runner.log_actuation_summary"), \
             patch("tools.watch_runner.write_actuation_ledger"), \
             patch("tools.watch_runner.git_commit_ledger_entry", return_value=(True, "ok")):
            watch_runner.actuate_phase(date(2026, 5, 4), [], {})
        self.assertEqual(call_count["n"], 0)


class TestLedgerWrite(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_writes_one_line_with_schema(self) -> None:
        ledger_path = self.tmp / "2026.jsonl"
        watch_runner.write_actuation_ledger(
            date(2026, 5, 4),
            ["T-0100", "T-0101"],
            "2026-05-04",
            ledger_path=ledger_path,
        )
        lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["agent"], "specialist")
        self.assertEqual(entry["action"], "actuation_2026-05-04")
        self.assertIn("P1", entry["justifies_via"])
        refs = [link["ref"] for link in entry["links"]]
        self.assertIn("T-0100", refs)
        self.assertIn("T-0101", refs)
        self.assertIn("knowledge/watch/reports/2026-05-04/", refs)
        # T-1088 §Auditable: actuation entries now carry the FULL ORG-001
        # 10-field schema (this writer previously omitted the provenance trio
        # — the convergence onto _audit.build_ledger_entry closes that gap).
        self.assertEqual(
            set(entry.keys()),
            {"id", "ts", "agent", "action", "rationale", "justifies_via",
             "links", "cost_usd", "parent_id", "supersedes"},
        )
        self.assertIsNone(entry["cost_usd"])
        self.assertIsNone(entry["parent_id"])
        self.assertIsNone(entry["supersedes"])

    def test_no_filed_ids_no_op(self) -> None:
        ledger_path = self.tmp / "2026.jsonl"
        result = watch_runner.write_actuation_ledger(
            date(2026, 5, 4), [], "2026-05-04", ledger_path=ledger_path
        )
        self.assertFalse(ledger_path.exists())
        self.assertIsNone(result)

    def test_routes_to_legacy_when_present(self) -> None:
        # T-0148 Phase 2: REPO_ROOT/ledger/2026.jsonl present → legacy target.
        ledger_dir = self.tmp / "ledger"
        ledger_dir.mkdir()
        legacy = ledger_dir / "2026.jsonl"
        legacy.write_text("")
        with patch("tools.watch_runner.REPO_ROOT", self.tmp):
            written = watch_runner.write_actuation_ledger(
                date(2026, 5, 4), ["T-0100"], "2026-05-04"
            )
        self.assertEqual(written, legacy)
        self.assertEqual(len(legacy.read_text().strip().splitlines()), 1)

    def test_routes_to_week_shard_when_legacy_absent(self) -> None:
        # T-0148 Phase 2: no legacy file → current ISO-week shard + mkdir.
        from tools.ledger_shard import current_write_target
        from datetime import datetime as _dt, timezone as _tz
        ledger_dir = self.tmp / "ledger"
        ledger_dir.mkdir()
        with patch("tools.watch_runner.REPO_ROOT", self.tmp):
            written = watch_runner.write_actuation_ledger(
                date(2026, 5, 4), ["T-0100"], "2026-05-04"
            )
        expected = current_write_target(ledger_dir, _dt.now(_tz.utc))
        self.assertEqual(written, expected)
        self.assertTrue(written.parent.is_dir())
        self.assertEqual(len(written.read_text().strip().splitlines()), 1)


class TestLedgerCommitScopeReadback(unittest.TestCase):
    """T-0148 Phase 2: git_commit_ledger_entry stages + scope-asserts the
    DYNAMIC write target, accepting a shard path post-migration."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _delegate(self, ledger_path, commit_result):
        """Run git_commit_ledger_entry with commit_scoped mocked. Returns
        (ok, msg, mock_commit_scoped, mock_runner_log)."""
        with (
            patch("tools.watch_runner.REPO_ROOT", self.tmp),
            patch("tools.watch_runner.commit_scoped", return_value=commit_result) as m,
            patch("tools.watch_runner._append_runner_log") as mlog,
        ):
            ok, msg = watch_runner.git_commit_ledger_entry(date(2026, 5, 4), ledger_path)
        return ok, msg, m, mlog

    def test_delegates_with_shard_rel(self) -> None:
        # Post-migration: the passed-in shard path resolves to its rel and is the
        # commit_scoped scope (not hardcoded ledger/2026.jsonl).
        shard = self.tmp / "ledger" / "2026-W22" / "2026-W22.jsonl"
        rel = "ledger/2026-W22/2026-W22.jsonl"
        ok, msg, m, _ = self._delegate(shard, (True, "committed 1 file(s)"))
        self.assertTrue(ok, msg)
        self.assertEqual(msg, f"pushed {rel}")
        args, kwargs = m.call_args
        self.assertEqual(args[0], [rel])
        self.assertEqual(kwargs["cwd"], self.tmp)
        self.assertTrue(kwargs["sync"])
        self.assertTrue(kwargs["push"])

    def test_delegates_with_legacy_rel(self) -> None:
        legacy = self.tmp / "ledger" / "2026.jsonl"
        rel = "ledger/2026.jsonl"
        ok, msg, m, _ = self._delegate(legacy, (True, "committed 1 file(s)"))
        self.assertTrue(ok, msg)
        self.assertEqual(msg, f"pushed {rel}")
        self.assertEqual(m.call_args.args[0], [rel])

    def test_propagates_failure_and_logs_to_runner(self) -> None:
        # commit_scoped's leak-guard / scope refusal propagates, and the
        # RUNNER_LOG side-effect is preserved on failure.
        shard = self.tmp / "ledger" / "2026-W22" / "2026-W22.jsonl"
        ok, msg, _m, mlog = self._delegate(
            shard, (False, "scope guard refused: out-of-scope paths staged: ['x']")
        )
        self.assertFalse(ok)
        self.assertIn("scope", msg.lower())
        mlog.assert_called_once()  # [specialist] actuation: <detail> logged


class TestActuationSummaryLog(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_format(self) -> None:
        log_path = self.tmp / "log"
        watch_runner.log_actuation_summary(3, 1, 0, 1, log_path=log_path)
        text = log_path.read_text(encoding="utf-8")
        self.assertIn(
            "[specialist] actuation: filed 3 tickets, surfaced 1 findings, retried 0, errored 1",
            text,
        )


class TestComputeDueBriefs(unittest.TestCase):
    """ORG-025/T-0855: --emit-due-briefs in-session dispatch input.

    compute_due_briefs() must reuse the same cadence/state due-logic main() uses,
    emit one record per due topic with a self-contained brief + the real reports
    output_path, and mutate no state / fire nothing.
    """

    def setUp(self) -> None:
        self._topics = [
            Topic(slug="due-weekly", cadence="weekly", budget=300,
                  scope="s1", seed_sources=["http://a"], notes=""),
            Topic(slug="paused-one", cadence="paused", budget=300,
                  scope="s2", seed_sources=[], notes=""),
        ]
        self._tp = patch.object(watch_runner, "parse_topics", return_value=self._topics)
        self._ls = patch.object(watch_runner, "load_state",
                                return_value={"schema_version": 1, "topics": {}})
        self._tp.start(); self._ls.start()

    def tearDown(self) -> None:
        self._tp.stop(); self._ls.stop()

    def test_emits_due_skips_paused(self) -> None:
        recs = watch_runner.compute_due_briefs(date(2026, 6, 5))
        self.assertEqual([r["slug"] for r in recs], ["due-weekly"])
        r = recs[0]
        self.assertEqual(set(r), {"slug", "cadence", "output_path", "brief"})
        self.assertEqual(r["cadence"], "weekly")
        self.assertTrue(r["output_path"].endswith("/2026-06-05/due-weekly.md"))
        # Brief is the self-contained researcher brief (no claude -p / no agent dep).
        self.assertIn("researcher", r["brief"])
        self.assertIn("surface: above-bar | routine", r["brief"])

    def test_already_fired_this_week_not_due(self) -> None:
        with patch.object(watch_runner, "load_state", return_value={
            "schema_version": 1,
            "topics": {"due-weekly": {"last_fire": "2026-06-03", "last_outcome": "ok"}},
        }):
            recs = watch_runner.compute_due_briefs(date(2026, 6, 5))
        self.assertEqual(recs, [])  # already fired in ISO week 23

    def test_force_emits_all_non_paused_regardless_of_state(self) -> None:
        with patch.object(watch_runner, "load_state", return_value={
            "schema_version": 1,
            "topics": {"due-weekly": {"last_fire": "2026-06-05", "last_outcome": "ok"}},
        }):
            recs = watch_runner.compute_due_briefs(date(2026, 6, 5), force=True)
        self.assertEqual([r["slug"] for r in recs], ["due-weekly"])

    def test_topic_filter_selects_one(self) -> None:
        recs = watch_runner.compute_due_briefs(date(2026, 6, 5), topic_filter="due-weekly")
        self.assertEqual([r["slug"] for r in recs], ["due-weekly"])

    def test_cli_emit_is_safe_mode_no_prod_flag_required(self) -> None:
        """--emit-due-briefs alone passes the default-safe gate and prints JSON."""
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = watch_runner.main(["--emit-due-briefs"])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual([r["slug"] for r in payload], ["due-weekly"])


class TestReportHeadlineAndIndexRow(unittest.TestCase):
    """Helpers for the in-session land path (ORG-025 / T-0855)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_headline_extracted_from_frontmatter(self) -> None:
        rp = self.tmp / "r.md"
        rp.write_text(
            "---\nsurface: routine\nheadline: Nothing material changed this week.\n---\n\n# Body\n",
            encoding="utf-8",
        )
        self.assertEqual(
            watch_runner.parse_report_headline(rp),
            "Nothing material changed this week.",
        )

    def test_headline_strips_quotes(self) -> None:
        rp = self.tmp / "r.md"
        rp.write_text('---\nheadline: "Quoted line."\n---\n', encoding="utf-8")
        self.assertEqual(watch_runner.parse_report_headline(rp), "Quoted line.")

    def test_no_frontmatter_returns_none(self) -> None:
        rp = self.tmp / "r.md"
        rp.write_text("# No frontmatter\n\nbody\n", encoding="utf-8")
        self.assertIsNone(watch_runner.parse_report_headline(rp))

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(watch_runner.parse_report_headline(self.tmp / "nope.md"))

    def test_day_index_has_row(self) -> None:
        date_dir = self.tmp / "2026-06-05"
        date_dir.mkdir()
        self.assertFalse(watch_runner.day_index_has_row(date_dir, "alpha"))
        watch_runner.append_day_index_entry(date_dir, "alpha", "ok", "TL;DR: x")
        self.assertTrue(watch_runner.day_index_has_row(date_dir, "alpha"))
        self.assertFalse(watch_runner.day_index_has_row(date_dir, "beta"))


class TestReportLandedOk(unittest.TestCase):
    """T-1581: `report_landed_ok` extends the T-0662 exists-only check to
    exists+nontrivial+structured (>= REPORT_MIN_BYTES, plus frontmatter
    `headline:` + at least one `## ` heading), per 2606.09863's
    structural-detector evidence — bare existence passes stubs/error-dumps.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_file_is_not_landed(self) -> None:
        self.assertFalse(watch_runner.report_landed_ok(self.tmp / "nope.md"))

    def test_empty_file_is_not_landed(self) -> None:
        rp = self.tmp / "empty.md"
        rp.write_text("", encoding="utf-8")
        self.assertFalse(watch_runner.report_landed_ok(rp))

    def test_small_stub_with_frontmatter_is_not_landed(self) -> None:
        # Has the right shape but is well under REPORT_MIN_BYTES.
        rp = self.tmp / "stub.md"
        rp.write_text("---\nheadline: too short\n---\n\n# x\n", encoding="utf-8")
        self.assertFalse(watch_runner.report_landed_ok(rp))

    def test_large_error_dump_without_structure_is_not_landed(self) -> None:
        # Well over REPORT_MIN_BYTES but no frontmatter/heading — the exact
        # "confident closing, no verified structure" shape 2606.09863 flags.
        rp = self.tmp / "errordump.md"
        rp.write_text("I could not complete this task. " * 40, encoding="utf-8")
        self.assertFalse(watch_runner.report_landed_ok(rp))

    def test_genuine_report_shape_is_landed(self) -> None:
        # Derived from knowledge/watch/reports/2026-07-06/
        # llm-agent-failure-modes-research.md's real shape via the shared
        # test helper (frontmatter+headline, `## `-headed sections, >500B).
        rp = self.tmp / "real.md"
        _write_landed_report(rp)
        self.assertTrue(watch_runner.report_landed_ok(rp))


class TestLandReports(unittest.TestCase):
    """ORG-025/T-0855 in-session land path: derive status from disk, append
    day-index rows, update state, commit, actuate.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.reports_root = self.tmp / "knowledge" / "watch" / "reports"
        self.date_dir = self.reports_root / "2026-06-05"
        self.date_dir.mkdir(parents=True)
        self._topics = [
            Topic(slug="alpha", cadence="weekly", budget=300,
                  scope="s", seed_sources=[], notes=""),
            Topic(slug="paused-one", cadence="paused", budget=300,
                  scope="s", seed_sources=[], notes=""),
        ]
        self._patches = [
            patch.object(watch_runner, "parse_topics", return_value=self._topics),
            patch.object(watch_runner, "load_state",
                         return_value={"schema_version": 1, "topics": {}}),
            patch.object(watch_runner, "REPORTS_ROOT", self.reports_root),
        ]
        for p in self._patches:
            p.start()
        self._saved_state: dict = {}
        self._save = patch.object(
            watch_runner, "save_state",
            side_effect=lambda s: self._saved_state.update(s),
        )
        self._save.start()

    def tearDown(self) -> None:
        self._save.stop()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _write_report(self, slug: str, headline: str) -> None:
        # Must be landed-ok (exists+size+structure, T-1581), not just present.
        _write_landed_report(self.date_dir / f"{slug}.md", headline=headline)

    def test_landed_report_ok_appends_row_updates_state_commits(self) -> None:
        self._write_report("alpha", "Alpha state-of-world line.")
        with patch.object(watch_runner, "git_commit_and_push",
                          return_value=(True, "pushed 2 file(s)")) as mock_gcp, \
             patch.object(watch_runner, "actuate_phase") as mock_act:
            rc = watch_runner.land_reports(date(2026, 6, 5), force=True)
        self.assertEqual(rc, 0)
        # Day-index row landed with TL;DR derived from the report headline.
        index = (self.date_dir / "README.md").read_text(encoding="utf-8")
        self.assertIn("| [alpha](alpha.md) | ok | TL;DR: Alpha state-of-world line. |", index)
        # paused topic is not landed.
        self.assertNotIn("paused-one", index)
        # State updated for alpha only.
        self.assertEqual(self._saved_state["topics"]["alpha"]["last_outcome"], "ok")
        self.assertNotIn("paused-one", self._saved_state["topics"])
        # Commit scoped to alpha (ok), actuation invoked.
        self.assertTrue(mock_gcp.called)
        fired_arg = mock_gcp.call_args.args[1]
        self.assertEqual([t.slug for t in fired_arg], ["alpha"])
        self.assertEqual(mock_gcp.call_args.args[2], {"alpha": "ok"})
        self.assertTrue(mock_act.called)

    def test_missing_report_is_fail_and_nonzero_exit(self) -> None:
        # No report written for alpha.
        with patch.object(watch_runner, "git_commit_and_push",
                          return_value=(True, "ok")), \
             patch.object(watch_runner, "actuate_phase"):
            rc = watch_runner.land_reports(date(2026, 6, 5), force=True)
        self.assertEqual(rc, 1)
        index = (self.date_dir / "README.md").read_text(encoding="utf-8")
        self.assertIn("| [alpha](alpha.md) | fail |", index)
        self.assertEqual(self._saved_state["topics"]["alpha"]["last_outcome"], "fail")

    def test_idempotent_rerun_no_duplicate_rows(self) -> None:
        self._write_report("alpha", "Alpha line.")
        with patch.object(watch_runner, "git_commit_and_push",
                          return_value=(True, "ok")), \
             patch.object(watch_runner, "actuate_phase"):
            watch_runner.land_reports(date(2026, 6, 5), force=True)
            watch_runner.land_reports(date(2026, 6, 5), force=True)
        index = (self.date_dir / "README.md").read_text(encoding="utf-8")
        self.assertEqual(index.count("[alpha](alpha.md)"), 1)

    def test_commit_failure_yields_nonzero_exit(self) -> None:
        self._write_report("alpha", "Alpha line.")
        with patch.object(watch_runner, "git_commit_and_push",
                          return_value=(False, "HERMES guard refused: x")), \
             patch.object(watch_runner, "actuate_phase"):
            rc = watch_runner.land_reports(date(2026, 6, 5), force=True)
        self.assertEqual(rc, 1)

    def test_no_actuate_skips_actuation(self) -> None:
        self._write_report("alpha", "Alpha line.")
        with patch.object(watch_runner, "git_commit_and_push",
                          return_value=(True, "ok")), \
             patch.object(watch_runner, "actuate_phase") as mock_act:
            watch_runner.land_reports(date(2026, 6, 5), force=True, no_actuate=True)
        self.assertFalse(mock_act.called)

    def test_topic_filter_unknown_returns_2(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            rc = watch_runner.land_reports(date(2026, 6, 5), topic_filter="ghost")
        self.assertEqual(rc, 2)

    def test_degenerate_report_is_fail(self) -> None:
        """A report file that EXISTS but fails the structural/size check
        (bare stub) must derive status 'fail', not 'ok' (T-1581)."""
        (self.date_dir / "alpha.md").write_text("# stub\n", encoding="utf-8")
        with patch.object(watch_runner, "git_commit_and_push",
                          return_value=(True, "ok")), \
             patch.object(watch_runner, "actuate_phase"):
            rc = watch_runner.land_reports(date(2026, 6, 5), force=True)
        self.assertEqual(rc, 1)
        index = (self.date_dir / "README.md").read_text(encoding="utf-8")
        self.assertIn("| [alpha](alpha.md) | fail |", index)
        self.assertEqual(self._saved_state["topics"]["alpha"]["last_outcome"], "fail")


class TestFileWatchTicket(unittest.TestCase):
    """ORG-025/T-0855 migrated timer entrypoint: file a target=specialist watch
    dispatch ticket when topics are due; no claude -p / no researcher.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "tickets" / "inbox").mkdir(parents=True)
        self._topics = [
            Topic(slug="alpha", cadence="weekly", budget=300,
                  scope="s", seed_sources=[], notes=""),
            Topic(slug="paused-one", cadence="paused", budget=300,
                  scope="s", seed_sources=[], notes=""),
        ]
        self._patches = [
            patch.object(watch_runner, "parse_topics", return_value=self._topics),
            patch.object(watch_runner, "load_state",
                         return_value={"schema_version": 1, "topics": {}}),
            patch.object(watch_runner, "REPO_ROOT", self.tmp),
            patch.object(watch_runner, "_append_runner_log"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_files_ticket_when_topics_due(self) -> None:
        with patch.object(watch_runner, "_invoke_ticket_file",
                          return_value=("filed", "T-9001", "retries=0")) as mock_file:
            rc = watch_runner.file_watch_ticket(date(2026, 6, 5), force=True)
        self.assertEqual(rc, 0)
        self.assertTrue(mock_file.called)
        argv = mock_file.call_args.args[0]
        # target=specialist, requester=specialist, carries due slug + recipe + marker.
        self.assertIn("--target", argv)
        self.assertEqual(argv[argv.index("--target") + 1], "specialist")
        self.assertEqual(argv[argv.index("--requester") + 1], "specialist")
        ask = argv[argv.index("--ask") + 1]
        self.assertIn("alpha", ask)
        self.assertIn("--emit-due-briefs", ask)
        self.assertIn("--land-commit", ask)
        self.assertNotIn("paused-one", ask)
        context = argv[argv.index("--context") + 1]
        self.assertIn("watch-dispatch-for: 2026-06-05", context)

    def test_no_ticket_when_nothing_due(self) -> None:
        # Not forced + empty state but is_due False for weekly already fired today.
        with patch.object(watch_runner, "load_state", return_value={
            "schema_version": 1,
            "topics": {"alpha": {"last_fire": "2026-06-05", "last_outcome": "ok"}},
        }), patch.object(watch_runner, "_invoke_ticket_file") as mock_file:
            rc = watch_runner.file_watch_ticket(date(2026, 6, 5))
        self.assertEqual(rc, 0)
        self.assertFalse(mock_file.called)

    def test_idempotent_skip_when_already_in_inbox(self) -> None:
        (self.tmp / "tickets" / "inbox" / "T-9000.md").write_text(
            "---\nid: T-9000\n---\n\nwatch-dispatch-for: 2026-06-05\n", encoding="utf-8"
        )
        with patch.object(watch_runner, "_invoke_ticket_file") as mock_file:
            rc = watch_runner.file_watch_ticket(date(2026, 6, 5), force=True)
        self.assertEqual(rc, 0)
        self.assertFalse(mock_file.called)

    def test_filing_failure_returns_nonzero(self) -> None:
        with patch.object(watch_runner, "_invoke_ticket_file",
                          return_value=("errored", None, "retries=2; stderr=git push")), \
             patch("sys.stderr", new_callable=io.StringIO):
            rc = watch_runner.file_watch_ticket(date(2026, 6, 5), force=True)
        self.assertEqual(rc, 1)


class TestWatchTicketArgvAcceptedByRealTicketPy(unittest.TestCase):
    """Integration smoke for the ORG-025 watch-dispatch argv.

    TestFileWatchTicket above mocks _invoke_ticket_file, so it asserts the argv
    file_watch_ticket *builds* but never runs it through ticket.py's real parser
    + cmd_file validation. That is the same mock-hides-integration gap that let
    the 2026-06-06 sweep `--intent` failure ship (fixed 4d93c0e7; sweep-side
    smoke added in test_knowledge_sweep) — and this is the MORE critical path:
    the migrated timer files THIS ticket every Monday (8 weekly topics roll the
    ISO-week boundary), so a target=specialist-rejected arg here breaks the live
    watch loop, not a recoverable sweep.

    This replays the production argv through ticket.main() in-process against a
    temp REPO_ROOT with git stubbed (--no-push skips fetch/push); ticket.py's
    real arg-validation, tid allocation, file write, and scope guard all run.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # Watch-side fixtures: forced fire with one due weekly topic.
        self._topics = [
            Topic(slug="alpha", cadence="weekly", budget=300,
                  scope="s", seed_sources=[], notes=""),
        ]
        self._wr_patches = [
            patch.object(watch_runner, "parse_topics", return_value=self._topics),
            patch.object(watch_runner, "load_state",
                         return_value={"schema_version": 1, "topics": {}}),
            patch.object(watch_runner, "REPO_ROOT", self.tmp),
            patch.object(watch_runner, "_append_runner_log"),
        ]
        for p in self._wr_patches:
            p.start()

        # ticket.py-side fixtures: temp board + stubbed git (mirrors the
        # sweep-side smoke and TestFileCommandSequence in test_ticket).
        self.root = self.tmp
        for s in ticket.STATES:
            (self.root / "tickets" / s).mkdir(parents=True, exist_ok=True)
        (self.root / "tickets" / "README.md").write_text(
            "# Tickets\n\n## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n\n## Related\n\n- Link\n",
            encoding="utf-8",
        )
        self._tk_patches = [
            patch.object(ticket, "REPO_ROOT", self.root),
            patch.object(ticket, "TICKETS_ROOT", self.root / "tickets"),
            patch.object(_git_utils, "REPO_ROOT", self.root),
        ]
        for p in self._tk_patches:
            p.start()

        self._staged: set[str] = set()

        def fake_run(argv, **kwargs):
            if argv[:2] == ["git", "add"] and "--" in argv:
                for p in argv[argv.index("--") + 1:]:
                    if (self.root / p).exists():
                        self._staged.add(p)
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "diff"]:
                out = "\n".join(sorted(self._staged))
                return MagicMock(
                    returncode=0, stdout=out + ("\n" if out else ""), stderr=""
                )
            if argv[:2] == ["git", "commit"]:
                self._staged.clear()
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        self._sub_patch = patch(
            "tools._git_utils.subprocess.run", side_effect=fake_run
        )
        self._sub_patch.start()

    def tearDown(self) -> None:
        self._sub_patch.stop()
        for p in self._tk_patches:
            p.stop()
        for p in self._wr_patches:
            p.stop()
        self._tmp.cleanup()

    def _production_argv(self) -> list[str]:
        captured: dict[str, list[str]] = {}

        def cap(argv, *a, **k):
            captured["argv"] = argv
            return ("filed", "T-9001", "retries=0")

        with patch.object(watch_runner, "_invoke_ticket_file", side_effect=cap):
            watch_runner.file_watch_ticket(date(2026, 6, 8), force=True)
        return captured["argv"]

    def test_production_argv_accepted_by_real_ticket_py(self) -> None:
        argv = self._production_argv()
        rc = ticket.main(argv[2:] + ["--no-push"])
        self.assertEqual(
            rc, 0,
            "ticket.py rejected the watch-dispatch argv — arg drift like the "
            "2026-06-06 --intent regression, on the live Monday watch path.",
        )
        inbox = list((self.root / "tickets" / "inbox").glob("T-*.md"))
        self.assertEqual(len(inbox), 1, "watch dispatch ticket did not land")

    def test_intent_injection_is_caught_by_this_smoke(self) -> None:
        argv = self._production_argv()
        tail = list(argv[2:])
        i = tail.index("--target")
        bad = tail[:i] + ["--intent", "action"] + tail[i:]
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = ticket.main(bad + ["--no-push"])
        self.assertEqual(rc, 1)
        self.assertIn("--intent is target=operator-specific", err.getvalue())


class TestLandCommitGate(unittest.TestCase):
    """Prod-mode interaction with the default-safe contract (T-0261), incl. the
    ORG-025 modes --land-commit and --file-watch-ticket.
    """

    @staticmethod
    def _args(**over) -> MagicMock:
        base = dict(
            land_commit=False, file_watch_ticket=False,
            actuate_only=False, emit_due_briefs=False,
        )
        base.update(over)
        return MagicMock(**base)

    def _assert_refused(self, args) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(watch_runner._enforce_default_safe(args), 64)

    def test_land_commit_alone_is_accepted_mode(self) -> None:
        self.assertIsNone(watch_runner._enforce_default_safe(self._args(land_commit=True)))

    def test_file_watch_ticket_alone_is_accepted_mode(self) -> None:
        self.assertIsNone(
            watch_runner._enforce_default_safe(self._args(file_watch_ticket=True))
        )

    def test_land_and_file_watch_ticket_together_refused(self) -> None:
        self._assert_refused(self._args(land_commit=True, file_watch_ticket=True))

    def test_land_commit_with_safe_flag_refused(self) -> None:
        self._assert_refused(self._args(land_commit=True, actuate_only=True))

    def test_file_watch_ticket_with_safe_flag_refused(self) -> None:
        self._assert_refused(self._args(file_watch_ticket=True, emit_due_briefs=True))


class TestPullRepoOrWarn(unittest.TestCase):
    """pull_repo_or_warn fail-open semantics for watch_runner — never raises, never blocks."""

    def test_pull_success_silent(self):
        with patch("tools._git_utils.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            watch_runner.pull_repo_or_warn("watch_runner")  # no exception
        # T-0218: pull_repo_or_warn now fetches + merges (resilient to FETCH_HEAD pollution).
        self.assertEqual(mock_sub.run.call_count, 2)
        first_args, _ = mock_sub.run.call_args_list[0]
        self.assertEqual(first_args[0], ["git", "fetch", "origin", "main"])
        second_args, _ = mock_sub.run.call_args_list[1]
        self.assertEqual(second_args[0], ["git", "merge", "--ff-only", "refs/remotes/origin/main"])

    def test_pull_nonzero_proceeds(self):
        import contextlib
        with patch("tools._git_utils.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(
                returncode=1, stdout="", stderr="fatal: refusing to merge unrelated histories\n"
            )
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                watch_runner.pull_repo_or_warn("watch_runner")  # must not raise
            self.assertIn("fetch failed", buf.getvalue())
            self.assertIn("refusing to merge", buf.getvalue())

    def test_pull_timeout_proceeds(self):
        import contextlib
        import subprocess as real_sub
        with patch("tools._git_utils.subprocess") as mock_sub:
            mock_sub.TimeoutExpired = real_sub.TimeoutExpired
            mock_sub.run.side_effect = real_sub.TimeoutExpired(cmd="git", timeout=30)
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                watch_runner.pull_repo_or_warn("watch_runner")  # must not raise
            self.assertIn("fetch skipped", buf.getvalue())


class TestDefaultSafeContractT0261(unittest.TestCase):
    """T-0261: default-safe contract — refuse to run without an explicit mode flag."""

    def test_no_flag_refuses_with_exit_64(self) -> None:
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = watch_runner.main([])
        self.assertEqual(rc, 64)
        self.assertIn("refusing to run without explicit mode", buf.getvalue())
        self.assertIn("--land-commit", buf.getvalue())

    def test_land_commit_plus_emit_due_briefs_is_ambiguous(self) -> None:
        from contextlib import redirect_stderr
        with redirect_stderr(io.StringIO()):
            rc = watch_runner.main(["--land-commit", "--emit-due-briefs"])
        self.assertEqual(rc, 64)

    def test_file_watch_ticket_plus_actuate_only_is_ambiguous(self) -> None:
        from contextlib import redirect_stderr
        with redirect_stderr(io.StringIO()):
            rc = watch_runner.main(["--file-watch-ticket", "--actuate-only"])
        self.assertEqual(rc, 64)

    def test_actuate_only_alone_is_accepted(self) -> None:
        with patch.object(watch_runner, "actuate_phase"):
            rc = watch_runner.main(["--actuate-only"])
        self.assertEqual(rc, 0)


class TestT0261Layer3Integration(unittest.TestCase):
    """T-0261 Layer-3 integration: spawn the real script as a subprocess against
    a tmp pantheon-config equivalent; assert the real repo is untouched.
    """

    def setUp(self) -> None:
        from tools._test_fixtures import setup_integration_env, snapshot_real_repo
        self.real_snapshot = snapshot_real_repo()
        self.env = setup_integration_env()

    def tearDown(self) -> None:
        from tools._test_fixtures import teardown_integration_env, assert_real_repo_untouched
        teardown_integration_env(self.env)
        assert_real_repo_untouched(self.real_snapshot)

    def test_no_flag_invocation_refuses_loudly(self) -> None:
        from tools._test_fixtures import run_script
        rc, _stdout, stderr = run_script(
            "watch_runner", [],
            env=self.env.env_overrides,
        )
        self.assertEqual(rc, 64)
        self.assertIn("refusing to run without explicit mode", stderr)


# ---------------------------------------------------------------------------
# T-0662: partial-stage guard — researcher writes file outside worktree
# ---------------------------------------------------------------------------


class TestPartialStageGuard(unittest.TestCase):
    """T-0662: git_commit_and_push must refuse with (False, msg) when an
    ok-status topic's report file is absent from the worktree's reports_dir.

    Reproduces the T-0660 failure mode: the researcher subprocess resolves a
    relative output path against a different cwd (e.g. the real REPO_ROOT
    rather than the isolated worktree), landing the file outside the worktree.
    The worktree's reports_dir/ lacks the file → git add stages nothing for
    that topic → partial-stage guard fires before commit.

    Test shape:
    - real tmp dir acting as the worktree (cwd=tmp_path)
    - topic-b's file written into reports_dir (correct)
    - topic-a's file NOT written into reports_dir (simulates wrong-cwd write)
    - git subprocess mocked: add no-ops, diff --cached returns only topic-b
    - HERMES scan bypassed (separately tested)
    - RUNNER_LOG redirected to a tmp file so log line is verifiable
    - assert returns (False, msg) with "partial-stage refused" and "topic-a"
    - assert no git commit was invoked
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.date_iso = "2026-05-25"
        self.reports_dir = self.tmp_path / "knowledge" / "watch" / "reports" / self.date_iso
        self.reports_dir.mkdir(parents=True)
        self.log_path = self.tmp_path / "runner.log"

        # Write topic-b's file into the worktree (landed correctly, and
        # landed-ok per T-1581 — real structure/size, not just present).
        _write_landed_report(self.reports_dir / "topic-b.md")
        # topic-a's file is intentionally NOT written — simulates researcher
        # writing to the wrong path (outside the worktree).

        self._git_patcher = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._git_patcher.start()

        # HERMES scan: pass cleanly (separately tested).
        self._hermes_patcher = patch(
            "tools.watch_runner._hermes_scan_dir", return_value=(True, None)
        )
        self._hermes_patcher.start()

        # git diff --cached --name-only: only topic-b staged (topic-a file
        # never landed in the worktree, so git add found nothing for it).
        def fake_git_run(argv, **kwargs):
            if argv[:3] == ["git", "diff", "--cached"]:
                scope = f"knowledge/watch/reports/{self.date_iso}/"
                return MagicMock(
                    returncode=0,
                    stdout=f"{scope}topic-b.md\n",
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        self.mock_run.side_effect = fake_git_run

    def tearDown(self) -> None:
        self._git_patcher.stop()
        self._hermes_patcher.stop()
        self._tmp.cleanup()

    def _git_calls(self) -> list[list[str]]:
        return [call.args[0] for call in self.mock_run.call_args_list]

    def test_missing_ok_topic_file_refuses(self) -> None:
        """Partial-stage guard fires when an ok-topic file is absent."""
        topics = [_topic("topic-a"), _topic("topic-b")]
        statuses = {"topic-a": "ok", "topic-b": "ok"}

        with patch("tools.watch_runner.RUNNER_LOG", self.log_path):
            ok, msg = watch_runner.git_commit_and_push(
                self.date_iso, topics, statuses, cwd=self.tmp_path
            )

        self.assertFalse(ok, f"expected False; got ok=True with msg={msg!r}")
        self.assertIn("partial-stage refused", msg)
        self.assertIn("topic-a", msg)

        # RUNNER_LOG must have been written with the greppable line.
        self.assertTrue(self.log_path.exists(), "RUNNER_LOG must be written on partial-stage refusal")
        log_text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("partial-stage refused", log_text)
        self.assertIn("topic-a", log_text)

        # No commit must have been issued.
        argvs = self._git_calls()
        self.assertFalse(
            any(a[:2] == ["git", "commit"] for a in argvs),
            f"git commit must not be invoked on partial-stage refusal; calls={argvs!r}",
        )

    def test_fail_status_topic_not_checked(self) -> None:
        """A topic with status 'fail' is not expected to have a file;
        the guard must not fire for it.  Only topic-b (ok) has a file.
        """
        topics = [_topic("topic-a"), _topic("topic-b")]
        # topic-a status is 'fail' — no file expected; guard must not fire.
        statuses = {"topic-a": "fail", "topic-b": "ok"}

        with patch("tools.watch_runner.RUNNER_LOG", self.log_path):
            ok, msg = watch_runner.git_commit_and_push(
                self.date_iso, topics, statuses, cwd=self.tmp_path
            )

        # topic-b's file exists and topic-a is fail → guard passes → commit
        # path proceeds (mock returns 0 for commit).
        self.assertTrue(ok, f"expected True (fail-status topic not checked); got msg={msg!r}")

    def test_present_but_degenerate_ok_topic_file_refuses(self) -> None:
        """A file that EXISTS but fails the structural/size check (stub,
        error-dump) must refuse exactly like an absent file (T-1581)."""
        topics = [_topic("topic-b"), _topic("topic-c")]
        statuses = {"topic-b": "ok", "topic-c": "ok"}
        # topic-c's file exists but is a bare stub — below REPORT_MIN_BYTES
        # and lacking the frontmatter/heading structure.
        (self.reports_dir / "topic-c.md").write_text("# stub\n", encoding="utf-8")

        with patch("tools.watch_runner.RUNNER_LOG", self.log_path):
            ok, msg = watch_runner.git_commit_and_push(
                self.date_iso, topics, statuses, cwd=self.tmp_path
            )

        self.assertFalse(ok, f"expected False; got ok=True with msg={msg!r}")
        self.assertIn("partial-stage refused", msg)
        self.assertIn("topic-c", msg)
        argvs = self._git_calls()
        self.assertFalse(
            any(a[:2] == ["git", "commit"] for a in argvs),
            f"git commit must not be invoked on partial-stage refusal; calls={argvs!r}",
        )


class TestParseFollowups(unittest.TestCase):
    """Unit tests for watch_runner.parse_followups()."""

    def test_section_present_two_items(self) -> None:
        body = (
            "# Title\n\nIntro.\n\n"
            "## Follow-ups\n"
            "title-a — ask-a\n"
            "title-b — ask-b\n"
        )
        result = watch_runner.parse_followups(body)
        self.assertEqual(result, [("title-a", "ask-a"), ("title-b", "ask-b")])

    def test_section_absent(self) -> None:
        body = "# Title\n\nSome content without a follow-ups section.\n"
        self.assertEqual(watch_runner.parse_followups(body), [])

    def test_leading_list_marker_stripped(self) -> None:
        """`- ` / `* ` / `1. ` bullets do not leak into the title (T-0946 shape).

        Regression for the broken-heading bug the operator hand-fixed on T-0946:
        `- Overflow toggle decision — enable …` must yield title
        `Overflow toggle decision`, NOT `- Overflow toggle decision`.
        """
        body = (
            "## Follow-ups\n"
            "- Overflow toggle decision — enable usage-credits overflow\n"
            "* star-bullet — ask-s\n"
            "1. ordered-bullet — ask-o\n"
        )
        result = watch_runner.parse_followups(body)
        self.assertEqual(
            result,
            [
                ("Overflow toggle decision", "enable usage-credits overflow"),
                ("star-bullet", "ask-s"),
                ("ordered-bullet", "ask-o"),
            ],
        )

    def test_bolded_title_with_inner_em_dash(self) -> None:
        """`- **Title — sub** — ask` keeps the whole bold span as the title (T-0859 shape).

        Regression for T-0859 where the em-dash split fell INSIDE the bolded
        title, producing the broken H1 `- **Agent SDK credit claim`.
        """
        body = (
            "## Follow-ups\n"
            "- **Agent SDK credit claim — verify the operator's account** — "
            "confirm which account owns the Max\n"
        )
        result = watch_runner.parse_followups(body)
        self.assertEqual(
            result,
            [(
                "Agent SDK credit claim — verify the operator's account",
                "confirm which account owns the Max",
            )],
        )

    def test_plain_bold_title_stripped(self) -> None:
        """A simply bolded title (`**Title** — ask`) drops the emphasis markers."""
        body = "## Follow-ups\n**Bolded title** — the ask\n"
        self.assertEqual(
            watch_runner.parse_followups(body),
            [("Bolded title", "the ask")],
        )

    def test_blank_lines_skipped(self) -> None:
        body = (
            "## Follow-ups\n"
            "\n"
            "title-a — ask-a\n"
            "\n"
            "title-b — ask-b\n"
            "\n"
        )
        result = watch_runner.parse_followups(body)
        self.assertEqual(result, [("title-a", "ask-a"), ("title-b", "ask-b")])

    def test_malformed_line_skipped(self) -> None:
        """Line without em-dash is skipped; valid lines are still returned."""
        body = (
            "## Follow-ups\n"
            "no-dash-here\n"
            "good-title — good-ask\n"
        )
        import io
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            result = watch_runner.parse_followups(body)
        self.assertEqual(result, [("good-title", "good-ask")])
        self.assertIn("skip malformed", buf.getvalue())

    def test_section_ends_at_next_heading(self) -> None:
        body = (
            "## Follow-ups\n"
            "title-a — ask-a\n"
            "## Sources\n"
            "should-not-appear — ignored\n"
        )
        result = watch_runner.parse_followups(body)
        self.assertEqual(result, [("title-a", "ask-a")])

    def test_section_ends_at_hr(self) -> None:
        body = (
            "## Follow-ups\n"
            "title-a — ask-a\n"
            "---\n"
            "after-hr — ignored\n"
        )
        result = watch_runner.parse_followups(body)
        self.assertEqual(result, [("title-a", "ask-a")])

    def test_empty_section(self) -> None:
        body = (
            "## Follow-ups\n"
            "## Sources\n"
            "source-line — ignored\n"
        )
        self.assertEqual(watch_runner.parse_followups(body), [])

    def test_soft_cap_truncates_and_warns(self) -> None:
        """More than _FOLLOWUP_SOFT_CAP items → only first N filed, warning emitted."""
        cap = watch_runner._FOLLOWUP_SOFT_CAP
        lines = "\n".join(f"title-{i} — ask-{i}" for i in range(cap + 5))
        body = f"## Follow-ups\n{lines}\n"
        import io
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            result = watch_runner.parse_followups(body)
        self.assertEqual(len(result), cap)
        self.assertIn("follow-up cap", buf.getvalue())


class TestActuatePhaseFollowups(unittest.TestCase):
    """Integration tests for follow-up filing in actuate_phase."""

    def _make_tmp(self, report_body: str) -> tuple:
        """Build a minimal repo layout under a tempdir. Returns (tmp_path, date_dir)."""
        tmp = tempfile.mkdtemp()
        # T-1308: register removal (the no-op tearDown never cleaned these, leaking
        # one /tmp dir per call). Matches the codebase idiom (test_usage_monitor).
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        tmp_path = Path(tmp)
        (tmp_path / "tickets" / "inbox").mkdir(parents=True)
        for sub in ("active", "done"):
            (tmp_path / "tickets" / sub).mkdir(parents=True)
        date_dir = tmp_path / "watch" / "reports" / "2026-05-04"
        date_dir.mkdir(parents=True)
        (date_dir / "mcp-supply-chain.md").write_text(report_body, encoding="utf-8")
        (date_dir / "README.md").write_text(
            "# 2026-05-04\n\n"
            "| Topic | Status | One-line summary |\n"
            "|---|---|---|\n"
            "| [mcp-supply-chain](mcp-supply-chain.md) | ok | TL;DR: test finding |\n",
            encoding="utf-8",
        )
        return tmp_path, date_dir

    def test_followups_filed_with_headline(self) -> None:
        """Headline filed first; then both follow-ups filed with correct parent_id."""
        report_body = (
            "# Test\n\nBody.\n\n"
            "## Follow-ups\n"
            "fu-title-1 — fu-ask-1\n"
            "fu-title-2 — fu-ask-2\n"
        )
        tmp_path, _ = self._make_tmp(report_body)
        file_finding_calls = []
        file_followup_calls = []

        def fake_file_finding(*a, **kw):
            file_finding_calls.append((a, kw))
            return ("filed", "T-1001", "retries=0")

        def fake_file_followup(title, ask, finding, *, parent_id, **kw):
            file_followup_calls.append((title, ask, finding, parent_id))
            return ("filed", f"T-{2000 + len(file_followup_calls)}", "retries=0")

        with patch("tools.watch_runner.REPO_ROOT", tmp_path), \
             patch("tools.watch_runner.REPORTS_ROOT", tmp_path / "watch" / "reports"), \
             patch("tools.watch_runner.file_finding", side_effect=fake_file_finding), \
             patch("tools.watch_runner.file_followup", side_effect=fake_file_followup), \
             patch("tools.watch_runner.log_actuation_summary"), \
             patch("tools.watch_runner.write_actuation_ledger"), \
             patch("tools.watch_runner.git_commit_ledger_entry", return_value=(True, "ok")):
            watch_runner.actuate_phase(date(2026, 5, 4), [], {})

        self.assertEqual(len(file_finding_calls), 1)
        self.assertEqual(len(file_followup_calls), 2)
        # Both follow-ups get the headline's ticket id as parent
        self.assertEqual(file_followup_calls[0][3], "T-1001")
        self.assertEqual(file_followup_calls[1][3], "T-1001")
        # Correct title/ask unpacked
        self.assertEqual(file_followup_calls[0][0], "fu-title-1")
        self.assertEqual(file_followup_calls[0][1], "fu-ask-1")
        self.assertEqual(file_followup_calls[1][0], "fu-title-2")
        self.assertEqual(file_followup_calls[1][1], "fu-ask-2")

    def test_no_followups_section(self) -> None:
        """Report without ## Follow-ups → file_followup never called."""
        report_body = "# Test\n\nBody only, no follow-ups section.\n"
        tmp_path, _ = self._make_tmp(report_body)
        file_followup_calls = []

        def fake_file_followup(*a, **kw):
            file_followup_calls.append((a, kw))
            return ("filed", "T-9999", "retries=0")

        with patch("tools.watch_runner.REPO_ROOT", tmp_path), \
             patch("tools.watch_runner.REPORTS_ROOT", tmp_path / "watch" / "reports"), \
             patch("tools.watch_runner.file_finding", return_value=("filed", "T-1001", "retries=0")), \
             patch("tools.watch_runner.file_followup", side_effect=fake_file_followup), \
             patch("tools.watch_runner.log_actuation_summary"), \
             patch("tools.watch_runner.write_actuation_ledger"), \
             patch("tools.watch_runner.git_commit_ledger_entry", return_value=(True, "ok")):
            watch_runner.actuate_phase(date(2026, 5, 4), [], {})

        self.assertEqual(len(file_followup_calls), 0)

    def test_idempotency_second_run(self) -> None:
        """Pre-seeded ticket containing report_relpath → file_finding AND
        file_followup both NOT called (the critical second-run-files-zero test)."""
        report_body = (
            "# Test\n\nBody.\n\n"
            "## Follow-ups\n"
            "fu-title — fu-ask\n"
        )
        tmp_path, _ = self._make_tmp(report_body)
        # Pre-seed a ticket whose body contains report_relpath verbatim.
        relpath = "knowledge/watch/reports/2026-05-04/mcp-supply-chain.md"
        (tmp_path / "tickets" / "inbox" / "T-0099.md").write_text(
            f"Source: {relpath}\n",
            encoding="utf-8",
        )
        file_finding_calls = []
        file_followup_calls = []

        with patch("tools.watch_runner.REPO_ROOT", tmp_path), \
             patch("tools.watch_runner.REPORTS_ROOT", tmp_path / "watch" / "reports"), \
             patch("tools.watch_runner.file_finding", side_effect=lambda *a, **kw: file_finding_calls.append(1) or ("filed", "T-X", "retries=0")), \
             patch("tools.watch_runner.file_followup", side_effect=lambda *a, **kw: file_followup_calls.append(1) or ("filed", "T-Y", "retries=0")), \
             patch("tools.watch_runner.log_actuation_summary"), \
             patch("tools.watch_runner.write_actuation_ledger"), \
             patch("tools.watch_runner.git_commit_ledger_entry", return_value=(True, "ok")):
            watch_runner.actuate_phase(date(2026, 5, 4), [], {})

        self.assertEqual(len(file_finding_calls), 0, "file_finding must not be called on second run")
        self.assertEqual(len(file_followup_calls), 0, "file_followup must not be called on second run")

    def test_followups_filed_when_headline_id_none(self) -> None:
        """Headline filing returns id=None (push success but no T-NNNN in stdout)
        → follow-ups still filed with parent_id=None (no --parent-ticket flag)."""
        report_body = (
            "# Test\n\nBody.\n\n"
            "## Follow-ups\n"
            "fu-title — fu-ask\n"
        )
        tmp_path, _ = self._make_tmp(report_body)
        file_followup_calls = []

        def fake_file_followup(title, ask, finding, *, parent_id, **kw):
            file_followup_calls.append(parent_id)
            return ("filed", "T-2001", "retries=0")

        with patch("tools.watch_runner.REPO_ROOT", tmp_path), \
             patch("tools.watch_runner.REPORTS_ROOT", tmp_path / "watch" / "reports"), \
             patch("tools.watch_runner.file_finding", return_value=("filed", None, "retries=0")), \
             patch("tools.watch_runner.file_followup", side_effect=fake_file_followup), \
             patch("tools.watch_runner.log_actuation_summary"), \
             patch("tools.watch_runner.write_actuation_ledger"), \
             patch("tools.watch_runner.git_commit_ledger_entry", return_value=(True, "ok")):
            watch_runner.actuate_phase(date(2026, 5, 4), [], {})

        self.assertEqual(len(file_followup_calls), 1)
        self.assertIsNone(file_followup_calls[0])


class TestFileFollowupArgv(unittest.TestCase):
    """Unit test for file_followup ticket.py argv construction."""

    def setUp(self) -> None:
        self.finding = Finding(
            slug="agentic-harness-landscape",
            tldr="agentic harness headline",
            report_relpath="knowledge/watch/reports/2026-05-04/agentic-harness-landscape.md",
            headline_excerpt="excerpt",
        )

    def test_argv_with_parent(self) -> None:
        with patch("tools.watch_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="filed T-0200 → tickets/inbox/T-0200.md\n",
                stderr="",
            )
            outcome, ticket_id, detail = watch_runner.file_followup(
                "my-title",
                "my-ask",
                self.finding,
                parent_id="T-0100",
            )
        self.assertEqual(outcome, "filed")
        self.assertEqual(ticket_id, "T-0200")

        argv = mock_run.call_args.args[0]
        self.assertIn("file", argv)
        # T-1217: follow-ups route to specialist (the watch-loop owner triages its own
        # inflow + routes domain ones onward), NOT orchestrator — the
        # target-mis-route orchestrator flagged via mailbox 2026-06-16.
        self.assertEqual(argv[argv.index("--target") + 1], "specialist")
        self.assertNotIn("orchestrator", argv)
        labels_csv = argv[argv.index("--labels") + 1]
        labels = labels_csv.split(",")
        self.assertIn("low-pri", labels)
        self.assertIn("triage", labels)
        self.assertIn("from-watch", labels)
        self.assertIn(f"source-topic-{self.finding.slug}", labels)
        # parent-ticket present
        self.assertIn("--parent-ticket", argv)
        self.assertEqual(argv[argv.index("--parent-ticket") + 1], "T-0100")
        # context contains report_relpath verbatim
        context_val = argv[argv.index("--context") + 1]
        self.assertIn(self.finding.report_relpath, context_val)

    def test_argv_without_parent(self) -> None:
        with patch("tools.watch_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="filed T-0201 → tickets/inbox/T-0201.md\n",
                stderr="",
            )
            outcome, ticket_id, _ = watch_runner.file_followup(
                "my-title",
                "my-ask",
                self.finding,
                parent_id=None,
            )
        self.assertEqual(outcome, "filed")
        argv = mock_run.call_args.args[0]
        self.assertNotIn("--parent-ticket", argv)


if __name__ == "__main__":
    unittest.main()
