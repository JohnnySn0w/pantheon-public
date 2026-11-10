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

import fcntl
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import pathlib
import tempfile
import shutil
import unittest
import tools.watch_runner as wr
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import _git_utils, ticket, watch_runner
from tools.watch_runner import Finding, Topic

# T-1774: env-override for git_mutation_lock's lockfile path (same pattern as
# tools/test_git_mutation_lock.py). Any test that exercises the real
# git_commit_and_push pipeline with subprocess mocked MUST set this to an
# isolated tempfile path — never let it fall through to the repo-anchored
# default. Without it, the fallback resolution in `_git_utils._git_common_dir`
# (triggered when `git rev-parse --git-common-dir` is mocked to empty stdout)
# caches `REPO_ROOT / ".git"` process-globally; `REPO_ROOT / ".git"` is a FILE
# (not a directory) in a linked worktree checkout, so a later real (unmocked)
# git_mutation_lock caller in the same test process — e.g.
# TestWatchTicketArgvAcceptedByRealTicketPy's real `ticket.main` — inherits
# the poisoned cache and crashes on `path.parent.mkdir()`.
_GIT_MUTATION_LOCK_ENV = "PANTHEON_GIT_MUTATION_LOCKFILE"

# T-4597: tests drive actuation paths that append to RUNNER_LOG. Without this redirect they
# wrote fake "HERMES guard refused" ERROR lines into the live ~/.local/log/pantheon log of
# whichever seat ran the suite, which reads as a real guard refusal during triage. Tests that
# assert on the log still patch RUNNER_LOG to their own path on top of this.
try:
    from tools import _seat_state_isolation
except ImportError:  # pragma: no cover - run from tools/
    import _seat_state_isolation  # type: ignore[no-redef]

_LIVE_RUNNER_LOG = watch_runner.RUNNER_LOG
_runner_log_dir: tempfile.TemporaryDirectory | None = None
_runner_log_patch = None


def setUpModule() -> None:
    global _runner_log_dir, _runner_log_patch
    _seat_state_isolation.start()  # T-4940 (see tools/_seat_state_isolation.py)
    _runner_log_dir = tempfile.TemporaryDirectory()
    _runner_log_patch = patch.object(
        watch_runner, "RUNNER_LOG", Path(_runner_log_dir.name) / "watch_runner.log"
    )
    _runner_log_patch.start()


def tearDownModule() -> None:
    _seat_state_isolation.stop()  # T-4940
    if _runner_log_patch is not None:
        _runner_log_patch.stop()
    if _runner_log_dir is not None:
        _runner_log_dir.cleanup()


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


class _SnapshotResponse:
    """Small urllib response double that preserves an arbitrary byte body."""

    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        final_url: str = "https://code.claude.com/docs/en/legal-and-compliance.md",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.body = body
        self.status = status
        self.final_url = final_url
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self.final_url

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


def _snapshot_opener(response: _SnapshotResponse):
    def open_response(_request, *, timeout):
        if timeout <= 0:
            raise AssertionError("timeout must be bounded")
        return response
    return open_response


class TestLegalSnapshotRegistry(unittest.TestCase):
    def test_only_policy_topic_has_exact_fixed_spec(self) -> None:
        self.assertEqual(
            set(watch_runner.TOPIC_SNAPSHOTS), {"anthropic-policy-and-billing"}
        )
        spec = watch_runner.TOPIC_SNAPSHOTS["anthropic-policy-and-billing"]
        self.assertEqual(
            spec.url, "https://code.claude.com/docs/en/legal-and-compliance.md"
        )
        self.assertEqual(spec.final_url, spec.url)
        self.assertEqual(spec.slug, "legal-and-compliance")

    def test_exact_dated_destination(self) -> None:
        spec = watch_runner.TOPIC_SNAPSHOTS["anthropic-policy-and-billing"]
        self.assertEqual(
            watch_runner.snapshot_relpath(spec, "2026-11-01").as_posix(),
            "knowledge/watch/snapshots/legal-and-compliance/2026-11-01.md",
        )

    def test_invalid_spec_variants_refused(self) -> None:
        bad_specs = [
            watch_runner.SnapshotSpec(
                url="http://code.claude.com/docs/en/legal-and-compliance.md",
                final_url="http://code.claude.com/docs/en/legal-and-compliance.md",
                slug="legal-and-compliance",
            ),
            watch_runner.SnapshotSpec(
                url="https://evil.example/docs/en/legal-and-compliance.md",
                final_url="https://evil.example/docs/en/legal-and-compliance.md",
                slug="legal-and-compliance",
            ),
            watch_runner.SnapshotSpec(
                url="https://code.claude.com/docs/en/legal-and-compliance.md?x=1",
                final_url="https://code.claude.com/docs/en/legal-and-compliance.md?x=1",
                slug="legal-and-compliance",
            ),
            watch_runner.SnapshotSpec(
                url="https://code.claude.com/docs/en/legal-and-compliance.md",
                final_url="https://code.claude.com/docs/en/legal-and-compliance.md",
                slug="../escape",
            ),
        ]
        for spec in bad_specs:
            with self.subTest(spec=spec), self.assertRaises(watch_runner.SnapshotCaptureError):
                watch_runner._validate_snapshot_spec(spec)
        spec = watch_runner.TOPIC_SNAPSHOTS["anthropic-policy-and-billing"]
        for invalid_date in ("2026-1-1", "../2026-11-01", "not-a-date"):
            with self.subTest(date=invalid_date), self.assertRaises(
                watch_runner.SnapshotCaptureError
            ):
                watch_runner.snapshot_relpath(spec, invalid_date)


class TestLegalSnapshotCapture(unittest.TestCase):
    TOPIC = "anthropic-policy-and-billing"
    TODAY = date(2026, 11, 1)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.relpath = Path(
            "knowledge/watch/snapshots/legal-and-compliance/2026-11-01.md"
        )
        self.log_patch = patch.object(watch_runner, "_append_runner_log")
        self.log_patch.start()

    def tearDown(self) -> None:
        self.log_patch.stop()
        self._tmp.cleanup()

    def _capture(self, body: bytes, **response_kwargs) -> Path | None:
        response = _SnapshotResponse(body, **response_kwargs)
        return watch_runner.capture_topic_snapshot(
            self.TOPIC,
            self.TODAY,
            repo_root=self.root,
            opener=_snapshot_opener(response),
        )

    def test_non_utf8_and_newline_body_is_hash_identical(self) -> None:
        body = b"# raw\r\nline\ninvalid:\xff\x00tail\r"
        relpath = self._capture(body)
        self.assertEqual(relpath, self.relpath)
        landed = (self.root / self.relpath).read_bytes()
        self.assertEqual(landed, body)
        self.assertEqual(hashlib.sha256(landed).digest(), hashlib.sha256(body).digest())

    def test_http_status_timeout_redirect_oversize_and_empty_refused(self) -> None:
        spec = watch_runner.TOPIC_SNAPSHOTS[self.TOPIC]
        cases = {
            "status": lambda: watch_runner.fetch_snapshot_bytes(
                spec, opener=_snapshot_opener(_SnapshotResponse(b"x", status=503))
            ),
            "redirect": lambda: watch_runner.fetch_snapshot_bytes(
                spec,
                opener=_snapshot_opener(
                    _SnapshotResponse(b"x", final_url="https://example.com/page.md")
                ),
            ),
            "oversize": lambda: watch_runner.fetch_snapshot_bytes(
                spec,
                opener=_snapshot_opener(_SnapshotResponse(b"x" * (spec.max_bytes + 1))),
            ),
            "declared-oversize": lambda: watch_runner.fetch_snapshot_bytes(
                spec,
                opener=_snapshot_opener(
                    _SnapshotResponse(b"x", headers={"Content-Length": str(spec.max_bytes + 1)})
                ),
            ),
            "declared-negative": lambda: watch_runner.fetch_snapshot_bytes(
                spec,
                opener=_snapshot_opener(
                    _SnapshotResponse(b"x", headers={"Content-Length": "-1"})
                ),
            ),
            "declared-malformed": lambda: watch_runner.fetch_snapshot_bytes(
                spec,
                opener=_snapshot_opener(
                    _SnapshotResponse(b"x", headers={"Content-Length": "many"})
                ),
            ),
            "empty": lambda: watch_runner.fetch_snapshot_bytes(
                spec, opener=_snapshot_opener(_SnapshotResponse(b""))
            ),
            "timeout": lambda: watch_runner.fetch_snapshot_bytes(
                spec,
                opener=lambda _request, timeout: (_ for _ in ()).throw(TimeoutError(timeout)),
            ),
        }
        for name, operation in cases.items():
            with self.subTest(name=name), self.assertRaises(
                watch_runner.SnapshotCaptureError
            ):
                operation()
        self.assertFalse((self.root / self.relpath).exists())

    def test_no_content_length_and_chunked_responses_preserve_exact_bytes(self) -> None:
        spec = watch_runner.TOPIC_SNAPSHOTS[self.TOPIC]
        body = b"raw\xff\r\nbody\n"
        for headers in ({}, {"Transfer-Encoding": "chunked"}):
            with self.subTest(headers=headers):
                fetched = watch_runner.fetch_snapshot_bytes(
                    spec,
                    opener=_snapshot_opener(_SnapshotResponse(body, headers=headers)),
                )
                self.assertEqual(fetched, body)

    def test_write_failure_cleans_temp_and_preserves_prior_destination(self) -> None:
        destination = self.root / self.relpath
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"old-body")
        with patch("tools.watch_runner.os.replace", side_effect=OSError("disk")), \
             self.assertRaises(watch_runner.SnapshotCaptureError):
            self._capture(b"new-body")
        self.assertEqual(destination.read_bytes(), b"old-body")
        self.assertEqual(list(destination.parent.glob(".*.tmp")), [])

    def test_fetch_failure_preserves_prior_destination(self) -> None:
        destination = self.root / self.relpath
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"old-body")
        with self.assertRaises(watch_runner.SnapshotCaptureError):
            watch_runner.capture_topic_snapshot(
                self.TOPIC,
                self.TODAY,
                repo_root=self.root,
                opener=lambda _request, timeout: (_ for _ in ()).throw(
                    TimeoutError(timeout)
                ),
            )
        self.assertEqual(destination.read_bytes(), b"old-body")
        self.assertEqual(list(destination.parent.glob(".*.tmp")), [])

    def test_symlink_destination_components_refused(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        snapshot_root = self.root / "knowledge" / "watch" / "snapshots"
        snapshot_root.parent.mkdir(parents=True)
        snapshot_root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(watch_runner.SnapshotCaptureError):
            self._capture(b"body")
        self.assertEqual(list(outside.iterdir()), [])

    def test_same_bytes_idempotent_without_mtime_change(self) -> None:
        self._capture(b"same")
        destination = self.root / self.relpath
        os.utime(destination, ns=(1_000_000_000, 1_000_000_000))
        before = destination.stat().st_mtime_ns
        self._capture(b"same")
        self.assertEqual(destination.stat().st_mtime_ns, before)
        self.assertEqual(destination.read_bytes(), b"same")

    def test_changed_same_day_atomically_replaces_one_file(self) -> None:
        self._capture(b"first")
        self._capture(b"second")
        destination = self.root / self.relpath
        self.assertEqual(destination.read_bytes(), b"second")
        self.assertEqual(list(destination.parent.glob("2026-11-01.md")), [destination])
        self.assertEqual(list(destination.parent.glob(".*.tmp")), [])


class TestNoRedirectHTTPIntegration(unittest.TestCase):
    def test_redirect_is_refused_before_target_request(self) -> None:
        import http.server
        import threading
        import urllib.error
        import urllib.request

        counts = {"source": 0, "target": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/source":
                    counts["source"] += 1
                    self.send_response(302)
                    self.send_header("Location", "/target")
                    self.end_headers()
                    return
                if self.path == "/target":
                    counts["target"] += 1
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"must-not-be-fetched")
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, _format, *_args) -> None:
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            request = urllib.request.Request(f"http://{host}:{port}/source")
            error_code = None
            try:
                watch_runner._open_snapshot_request(request, timeout=2)
            except urllib.error.HTTPError as exc:
                error_code = exc.code
            self.assertEqual(counts, {"source": 1, "target": 0})
            self.assertEqual(error_code, 302)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class TestLegalSnapshotBaseline(unittest.TestCase):
    def test_baseline_size_and_hash_unchanged(self) -> None:
        baseline = (
            Path(__file__).resolve().parent.parent
            / "knowledge/watch/snapshots/legal-and-compliance/2026-09-30.md"
        )
        body = baseline.read_bytes()
        self.assertEqual(len(body), 6149)
        self.assertEqual(
            hashlib.sha256(body).hexdigest(),
            "4a4cc1762fb1d992a66347dcf94c60a27d328391d8074c13e8ae9d5c4182413b",
        )


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
        # T-1774: isolate the git_mutation_lock lockfile — see module-level
        # comment on _GIT_MUTATION_LOCK_ENV.
        os.environ[_GIT_MUTATION_LOCK_ENV] = os.path.join(
            self._tmp.name, "gitlock", "ops.lock"
        )

    def tearDown(self) -> None:
        self._patcher.stop()
        self._scan_patcher.stop()
        os.environ.pop(_GIT_MUTATION_LOCK_ENV, None)
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
        # T-3600: push_with_retry runs a read-only symbolic-ref/log probe before
        # pushing (branch-safety warn); exclude it from the lifecycle sequence.
        kinds = [a[1] for a in argvs
                 if a[0] == "git" and a[1] not in ("symbolic-ref", "log")]
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


class TestSnapshotCommitScope(unittest.TestCase):
    DATE = "2026-11-01"
    TOPIC = _topic("anthropic-policy-and-billing")
    SNAPSHOT_REL = "knowledge/watch/snapshots/legal-and-compliance/2026-11-01.md"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.report_dir = self.root / "knowledge" / "watch" / "reports" / self.DATE
        self.report_dir.mkdir(parents=True)
        _write_landed_report(self.report_dir / f"{self.TOPIC.slug}.md")
        self.snapshot = self.root / self.SNAPSHOT_REL
        self.snapshot.parent.mkdir(parents=True)
        self.snapshot.write_bytes(b"snapshot-body")
        os.environ[_GIT_MUTATION_LOCK_ENV] = str(self.root / "lock" / "ops.lock")
        self.git_patch = patch("tools._git_utils.subprocess.run")
        self.git_run = self.git_patch.start()
        self.scan_patch = patch(
            "tools.watch_runner._hermes_scan_dir", return_value=(True, None)
        )
        self.scan_patch.start()

    def tearDown(self) -> None:
        self.scan_patch.stop()
        self.git_patch.stop()
        os.environ.pop(_GIT_MUTATION_LOCK_ENV, None)
        self._tmp.cleanup()

    def _calls(self) -> list[list[str]]:
        return [call.args[0] for call in self.git_run.call_args_list]

    def _run_with_staged(self, paths: list[str]) -> tuple[bool, str]:
        def fake_run(argv, **_kwargs):
            if argv[:3] == ["git", "diff", "--cached"]:
                return MagicMock(returncode=0, stdout="\n".join(paths) + "\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        self.git_run.side_effect = fake_run
        return watch_runner.git_commit_and_push(
            self.DATE, [self.TOPIC], {self.TOPIC.slug: "ok"}, cwd=self.root
        )

    def test_exact_report_and_snapshot_paths_accepted(self) -> None:
        report_rel = f"knowledge/watch/reports/{self.DATE}/{self.TOPIC.slug}.md"
        ok, msg = self._run_with_staged([report_rel, self.SNAPSHOT_REL])
        self.assertTrue(ok, msg)
        add_call = next(args for args in self._calls() if args[:2] == ["git", "add"])
        self.assertIn(f"knowledge/watch/reports/{self.DATE}/", add_call)
        self.assertIn(self.SNAPSHOT_REL, add_call)
        self.assertNotIn("knowledge/watch/snapshots/", add_call)

    def test_missing_expected_snapshot_refuses_and_resets(self) -> None:
        self.snapshot.unlink()
        report_rel = f"knowledge/watch/reports/{self.DATE}/{self.TOPIC.slug}.md"
        ok, msg = self._run_with_staged([report_rel])
        self.assertFalse(ok)
        self.assertIn("snapshot guard refused", msg)
        self.assertTrue(any(args[:3] == ["git", "reset", "HEAD"] for args in self._calls()))
        self.assertFalse(any(args[:2] == ["git", "commit"] for args in self._calls()))

    def test_sibling_date_slug_traversal_other_topic_and_prestaged_refuse(self) -> None:
        refused_paths = [
            "knowledge/watch/snapshots/legal-and-compliance/2026-10-31.md",
            "knowledge/watch/snapshots/other-slug/2026-11-01.md",
            "knowledge/watch/snapshots/legal-and-compliance/../escape.md",
            "knowledge/watch/snapshots/other-topic/2026-11-01.md",
        ]
        report_rel = f"knowledge/watch/reports/{self.DATE}/{self.TOPIC.slug}.md"
        for leaked in refused_paths:
            with self.subTest(leaked=leaked):
                self.git_run.reset_mock()
                ok, msg = self._run_with_staged([report_rel, self.SNAPSHOT_REL, leaked])
                self.assertFalse(ok)
                self.assertIn("scope guard refused", msg)
                self.assertTrue(
                    any(args[:3] == ["git", "reset", "HEAD"] for args in self._calls())
                )
                self.assertFalse(any(args[:2] == ["git", "commit"] for args in self._calls()))

    def test_snapshot_byte_guard_refuses_and_resets(self) -> None:
        self.snapshot.write_text("bad " + "HERME" + "S.md bytes\n", encoding="utf-8")
        report_rel = f"knowledge/watch/reports/{self.DATE}/{self.TOPIC.slug}.md"
        ok, msg = self._run_with_staged([report_rel, self.SNAPSHOT_REL])
        self.assertFalse(ok)
        self.assertIn("guard refused", msg)
        self.assertTrue(any(args[:3] == ["git", "reset", "HEAD"] for args in self._calls()))


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

    def test_configured_ok_topic_names_only_exact_snapshot_scope(self) -> None:
        topic = _topic("anthropic-policy-and-billing")
        msg = watch_runner._build_commit_message(
            "2026-11-01", [topic], {topic.slug: "ok"}
        )
        self.assertIn(
            "exact snapshot(s): knowledge/watch/snapshots/legal-and-compliance/2026-11-01.md",
            msg,
        )
        self.assertNotIn("knowledge/watch/snapshots/ only", msg)

    def test_configured_failed_topic_has_no_snapshot_scope(self) -> None:
        topic = _topic("anthropic-policy-and-billing")
        msg = watch_runner._build_commit_message(
            "2026-11-01", [topic], {topic.slug: "fail"}
        )
        self.assertIn("Scope: knowledge/watch/reports/2026-11-01/ only.", msg)


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
        # T-1774: isolate the git_mutation_lock lockfile — see module-level
        # comment on _GIT_MUTATION_LOCK_ENV.
        os.environ[_GIT_MUTATION_LOCK_ENV] = os.path.join(
            self._tmp.name, "gitlock", "ops.lock"
        )

    def tearDown(self) -> None:
        self._patcher.stop()
        self._scan_patcher.stop()
        os.environ.pop(_GIT_MUTATION_LOCK_ENV, None)
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


class TestGitCommitAndPushHoldsMutationLock(unittest.TestCase):
    """T-1774: git_commit_and_push's pull->push span now runs inside
    `git_mutation_lock()` — the same critical-section primitive
    `commit_scoped` and `git_commit_ledger_entry` already serialize on
    (T-1770 Part b), closing the residual unlocked-writer gap flagged off
    T-1770 (`bugs/ticket-py-shared-checkout-interleaved-writer-race.md`).

    Reuses the env-override + tempfile pattern from
    `tools/test_git_mutation_lock.py` (`PANTHEON_GIT_MUTATION_LOCKFILE`) —
    never exercises the live checkout's real lockfile.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_cwd = Path(self._tmp.name)
        self._lockfile = os.path.join(self._tmp.name, "gitlock", "ops.lock")
        os.environ[_GIT_MUTATION_LOCK_ENV] = self._lockfile

        self._patcher = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._patcher.start()
        self._scan_patcher = patch(
            "tools.watch_runner._hermes_scan_dir", return_value=(True, None)
        )
        self._scan_patcher.start()

    def tearDown(self) -> None:
        self._patcher.stop()
        self._scan_patcher.stop()
        os.environ.pop(_GIT_MUTATION_LOCK_ENV, None)
        self._tmp.cleanup()

    def test_lock_is_genuinely_held_across_the_add_through_push_span(self) -> None:
        """Prove the lock is actually HELD (not merely invoked-and-released
        before the pipeline runs): intercept `git add` mid-pipeline and, from
        this same test process, attempt an independent non-blocking flock on
        the SAME lockfile — it must be refused (EAGAIN/EACCES/EWOULDBLOCK),
        proving git_commit_and_push's own hold is still in effect at that
        point. Then, after the call returns, prove the lock was released: the
        same independent acquire must now succeed immediately.
        """
        date_iso = "2026-07-14"
        reports_dir = self.tmp_cwd / "knowledge" / "watch" / "reports" / date_iso
        reports_dir.mkdir(parents=True)
        _write_landed_report(reports_dir / "foo.md")

        checked = {"during_add": False}

        def fake_run(argv, **kwargs):
            if argv[:2] == ["git", "add"]:
                self.assertTrue(
                    os.path.exists(self._lockfile),
                    "lockfile must already exist once git_mutation_lock is held",
                )
                fd = os.open(self._lockfile, os.O_WRONLY)
                try:
                    with self.assertRaises(OSError):
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd)
                # The holder record (written by git_mutation_lock on acquire)
                # must name this call's label.
                content = Path(self._lockfile).read_text(encoding="utf-8")
                self.assertIn(f"watch_runner land {date_iso}", content)
                checked["during_add"] = True
            if argv[:2] == ["git", "diff"]:
                return MagicMock(
                    returncode=0,
                    stdout=f"knowledge/watch/reports/{date_iso}/foo.md\n",
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        self.mock_run.side_effect = fake_run

        ok, msg = watch_runner.git_commit_and_push(
            date_iso, [_topic("foo")], {"foo": "ok"}, cwd=self.tmp_cwd
        )

        self.assertTrue(ok, f"expected success; got msg={msg!r}")
        self.assertTrue(
            checked["during_add"],
            "the `git add` call was never intercepted — the in-band lock-hold "
            "assertion never ran",
        )

        # Released after return: an independent acquire must now succeed.
        fd = os.open(self._lockfile, os.O_WRONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def test_lock_released_on_refusal_path_too(self) -> None:
        """A non-success return (scope-guard refusal here) must still release
        the lock — the `with` block's cleanup runs on early `return`, not
        only on the success fallthrough."""
        date_iso = "2026-07-14"

        def fake_run(argv, **kwargs):
            if argv[:2] == ["git", "diff"]:
                return MagicMock(returncode=0, stdout="ledger/leaked.md\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        self.mock_run.side_effect = fake_run

        ok, msg = watch_runner.git_commit_and_push(
            date_iso, [_topic("foo")], {"foo": "ok"}, cwd=self.tmp_cwd
        )

        self.assertFalse(ok)
        self.assertIn("scope", msg.lower())

        fd = os.open(self._lockfile, os.O_WRONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


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

    def test_every_active_topic_has_policy_entry(self) -> None:
        """T-3890: land-time structural guard — every ACTIVE topic in topics.md
        must have a TOPIC_POLICY entry. The runtime _warn_unregistered_topic
        warning is a pin, not a floor; a landed-topic-without-policy silently
        routes to _DEFAULT_POLICY (unknown-topic + interactive-required) and
        inbox_consumer skips those, dropping findings for a full cycle.
        T-3853 (glop-it-blog + free-frontier-adjacent-models) is the class
        instance this test catches at CI-time before any cycle silently drops.
        Keyed on cadence != paused, matching the runtime warning shape."""
        topics = watch_runner.parse_topics(watch_runner.TOPICS_FILE)
        missing = [
            t.slug
            for t in topics
            if t.cadence != "paused"
            and t.cadence in watch_runner.VALID_CADENCES
            and t.slug not in watch_runner.TOPIC_POLICY
        ]
        self.assertFalse(
            missing,
            f"ACTIVE topics missing TOPIC_POLICY entry in tools/watch_runner.py: "
            f"{missing}. Each such topic falls through to _DEFAULT_POLICY and "
            "its findings are silently undelivered. Add a TOPIC_POLICY entry "
            "(target / labels / interactive_required) for each.",
        )


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

    def test_readback_marker_is_stripped_from_tldr(self) -> None:
        # T-5500: the day-index marker is for humans; it must not leak into the
        # tldr that actuation turns into ticket titles.
        index = self.date_dir / "README.md"
        index.write_text(
            index.read_text(encoding="utf-8").replace(
                "TL;DR: alpha finding line one",
                "TL;DR: alpha finding line one" + watch_runner.READBACK_MISSING_MARK,
            ),
            encoding="utf-8",
        )
        with patch("sys.stderr", io.StringIO()):
            findings = watch_runner.extract_findings(self.date_dir, {})
        self.assertEqual(findings[0].tldr, "alpha finding line one")

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
        self.assertEqual(argv[argv.index("--target") + 1], "role:system_orchestrator")
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
    def test_runner_log_is_redirected_away_from_the_live_log(self) -> None:
        """T-4597: the module-level redirect is in force for every test here."""
        self.assertNotEqual(watch_runner.RUNNER_LOG, _LIVE_RUNNER_LOG)

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
        # T-4737: ORG-001 role id + seat slot, not the persona name.
        self.assertEqual(entry["agent"], "head_research_orchestrator")
        self.assertEqual(entry["executor"], "specialist")
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
             "links", "cost_usd", "parent_id", "supersedes", "executor"},
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


class TestActuationValidatesBeforeFilingTickets(unittest.TestCase):
    """T-4739 (orchestrator requirement 2): under ORG-001 ENFORCE a refusable ledger entry means
    NO ticket is filed (never tickets filed but unrecorded)."""

    def _run(self, agent: str):
        with patch.object(watch_runner, "_LEDGER_AGENT", agent), \
             patch("tools.watch_runner.extract_findings", return_value=[]) as ext, \
             patch("tools.watch_runner.file_finding") as filed, \
             patch("tools.watch_runner.file_followup") as followup, \
             patch("tools.watch_runner.write_actuation_ledger") as written, \
             patch("tools.watch_runner._append_runner_log") as log:
            watch_runner.actuate_phase(date(2026, 5, 4), [], {})
        return ext, filed, followup, written, log

    def test_refusable_entry_files_no_ticket_and_reads_no_findings(self) -> None:
        ext, filed, followup, written, log = self._run("artisan")  # persona names are history-only
        ext.assert_not_called()
        filed.assert_not_called()
        followup.assert_not_called()
        written.assert_not_called()
        self.assertIn("SKIPPED, no tickets filed", log.call_args[0][0])

    def test_conforming_entry_proceeds_to_actuation(self) -> None:
        ext, *_ = self._run("head_research_orchestrator")
        ext.assert_called_once()


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

    def test_uncleared_corrections_thread_into_brief(self) -> None:
        """T-2614: un-cleared state corrections for a due topic must appear
        in the emitted brief; the record's key-set stays unchanged.
        """
        with patch.object(watch_runner, "load_state", return_value={
            "schema_version": 1,
            "topics": {},
            "brief_corrections": {
                "due-weekly": [{"id": "c1", "text": "seed URL is 404",
                                 "source_date": "2026-06-01",
                                 "source_report": "knowledge/watch/reports/2026-06-01/due-weekly.md"}],
            },
        }):
            recs = watch_runner.compute_due_briefs(date(2026, 6, 5))
        self.assertEqual([r["slug"] for r in recs], ["due-weekly"])
        r = recs[0]
        self.assertEqual(set(r), {"slug", "cadence", "output_path", "brief"})
        self.assertIn("seed URL is 404", r["brief"])
        self.assertIn("c1", r["brief"])

    def test_no_corrections_key_in_state_does_not_error(self) -> None:
        # Baseline setUp's state has no "brief_corrections" key at all —
        # .get()-defensive read must not KeyError.
        recs = watch_runner.compute_due_briefs(date(2026, 6, 5))
        self.assertNotIn("Known brief corrections", recs[0]["brief"])


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


class TestBuildBrief(unittest.TestCase):
    """T-1739 GAP 2: build_brief must carry the HERMES byte-discipline
    instruction so researchers escape the trigger phrase instead of emitting
    it bare (which then trips the --land-commit guard and orphans reports).
    """

    def test_brief_carries_escape_instruction(self) -> None:
        topic = _topic("anthropic-policy-and-billing")
        brief = watch_runner.build_brief(topic, Path("/tmp/out.md"))
        self.assertIn("HERMES\\.md", brief)

    def test_brief_escape_instruction_does_not_trip_the_guard(self) -> None:
        # Verify against the ACTUAL guard regex (not an assumed shape) that the
        # escaped form we instruct researchers to use is genuinely safe.
        topic = _topic("anthropic-policy-and-billing")
        brief = watch_runner.build_brief(topic, Path("/tmp/out.md"))
        self.assertIsNone(_git_utils._HERMES_PATTERN.search(brief.encode("utf-8")))

    def test_no_corrections_is_byte_identical_to_baseline(self) -> None:
        """T-2614: the `corrections` kwarg must be a pure no-op when absent
        or empty — every pre-T-2614 call site (and every existing test in
        this class) depends on build_brief's output being unchanged.
        """
        topic = _topic("due-weekly")
        baseline = watch_runner.build_brief(topic, Path("/tmp/out.md"))
        self.assertEqual(
            watch_runner.build_brief(topic, Path("/tmp/out.md"), corrections=None),
            baseline,
        )
        self.assertEqual(
            watch_runner.build_brief(topic, Path("/tmp/out.md"), corrections=[]),
            baseline,
        )

    def test_corrections_appended_as_operator_context_section(self) -> None:
        topic = _topic("due-weekly")
        corrections = [{
            "id": "abc123def456",
            "text": "seed source X is 404; correct URL is Y",
            "source_date": "2026-08-01",
            "source_report": "knowledge/watch/reports/2026-08-01/due-weekly.md",
        }]
        brief = watch_runner.build_brief(topic, Path("/tmp/out.md"), corrections=corrections)
        self.assertIn("Known brief corrections", brief)
        self.assertIn("abc123def456", brief)
        self.assertIn("seed source X is 404; correct URL is Y", brief)
        self.assertIn("2026-08-01", brief)
        self.assertIn("knowledge/watch/reports/2026-08-01/due-weekly.md", brief)
        # Existing sections must survive untouched.
        self.assertIn("## Operator-side notes", brief)
        self.assertIn("## Output", brief)
        # Instructs the researcher not to self-edit topics.md.
        self.assertIn("do NOT edit `knowledge/watch/topics.md`", brief)

    def test_recovery_metric_pilot_only_attaches_to_failure_modes_lane(self) -> None:
        pilot_topic = _topic("llm-agent-failure-modes-research")
        pilot_brief = watch_runner.build_brief(pilot_topic, Path("/tmp/out.md"))
        self.assertIn("Recovery metric pilot (T-4904)", pilot_brief)
        self.assertIn("task_success", pilot_brief)
        self.assertIn("decisive_file_acquisition", pilot_brief)
        self.assertIn("early / late / missed / not-applicable / unknown", pilot_brief)
        self.assertIn("ultimately edited or test-relevant files/context", pilot_brief)
        self.assertIn("recovery_outcome", pilot_brief)
        self.assertIn("cascade_radius", pilot_brief)
        self.assertIn("failure_event: none", pilot_brief)

        ordinary_brief = watch_runner.build_brief(_topic("context-management"), Path("/tmp/out.md"))
        self.assertNotIn("Recovery metric pilot (T-4904)", ordinary_brief)

    def test_hn_recipe_is_url_encoded_with_runner_computed_window(self) -> None:
        """T-5593: two 2026-10-26 researchers lost their HN pass to hand-built
        Algolia URLs (HTTP 400). The brief hands them the encoded form and a
        window start the runner computed, so nobody shells out to `date`.
        """
        topic = _topic("context-management")  # cadence daily -> 2-day window
        brief = watch_runner.build_brief(topic, Path("/tmp/out.md"), today=date(2026, 10, 26))
        self.assertIn("curl -sG 'https://hn.algolia.com/api/v1/search'", brief)
        self.assertIn("--data-urlencode 'query=", brief)
        self.assertIn("-w '%{http_code}'", brief)
        # 2026-10-24T00:00:00Z == 1792800000
        self.assertIn("created_at_i>1792800000", brief)
        self.assertIn("report the status", brief)

    def test_hn_window_follows_cadence(self) -> None:
        weekly = Topic(slug="w", cadence="weekly", budget=300, scope="s", seed_sources=[], notes="")
        brief = watch_runner.build_brief(weekly, Path("/tmp/out.md"), today=date(2026, 10, 26))
        # 8-day window: 2026-10-18T00:00:00Z == 1792281600
        self.assertIn("created_at_i>1792281600", brief)

    def test_hn_window_is_stable_within_a_day(self) -> None:
        # The byte-identical checks above call build_brief twice; a clock read
        # finer than a day would make them flaky across a second boundary.
        topic = _topic("due-weekly")
        a = watch_runner.build_brief(topic, Path("/tmp/out.md"), today=date(2026, 10, 26))
        b = watch_runner.build_brief(topic, Path("/tmp/out.md"), today=date(2026, 10, 26))
        self.assertEqual(a, b)


class TestParseReportBriefCorrections(unittest.TestCase):
    """T-2614: extract the `brief_corrections:` frontmatter list — the
    researcher-authored half of the back-propagation convention.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_wellformed_list_parses(self) -> None:
        rp = self.tmp / "r.md"
        rp.write_text(
            "---\nheadline: x\nbrief_corrections:\n"
            "  - seed source X is 404; correct URL is Y\n"
            '  - "the scope paragraph cites a nonexistent study"\n'
            "---\n\n# Body\n",
            encoding="utf-8",
        )
        self.assertEqual(
            watch_runner.parse_report_brief_corrections(rp),
            [
                "seed source X is 404; correct URL is Y",
                "the scope paragraph cites a nonexistent study",
            ],
        )

    def test_missing_key_returns_empty_no_warning(self) -> None:
        rp = self.tmp / "r.md"
        rp.write_text("---\nheadline: x\n---\n\n# Body\n", encoding="utf-8")
        warnings: list[str] = []
        self.assertEqual(watch_runner.parse_report_brief_corrections(rp, warnings=warnings), [])
        self.assertEqual(warnings, [])

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(watch_runner.parse_report_brief_corrections(self.tmp / "nope.md"), [])

    def test_no_frontmatter_returns_empty(self) -> None:
        rp = self.tmp / "r.md"
        rp.write_text("# No frontmatter\n\nbody\n", encoding="utf-8")
        self.assertEqual(watch_runner.parse_report_brief_corrections(rp), [])

    def test_inline_scalar_value_is_malformed_and_warns(self) -> None:
        rp = self.tmp / "r.md"
        rp.write_text(
            "---\nheadline: x\nbrief_corrections: oops not a list\n---\n\n# Body\n",
            encoding="utf-8",
        )
        warnings: list[str] = []
        self.assertEqual(watch_runner.parse_report_brief_corrections(rp, warnings=warnings), [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("inline value", warnings[0])

    def test_key_with_zero_items_warns_once(self) -> None:
        rp = self.tmp / "r.md"
        rp.write_text(
            "---\nheadline: x\nbrief_corrections:\nsurface: routine\n---\n\n# Body\n",
            encoding="utf-8",
        )
        warnings: list[str] = []
        self.assertEqual(watch_runner.parse_report_brief_corrections(rp, warnings=warnings), [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("no items parsed", warnings[0])

    def test_never_raises_on_garbage_frontmatter(self) -> None:
        rp = self.tmp / "r.md"
        rp.write_text("---\n:::not valid: [[\nbrief_corrections\n---\n", encoding="utf-8")
        # Must not raise.
        watch_runner.parse_report_brief_corrections(rp)


class TestAccumulateBriefCorrections(unittest.TestCase):
    """T-2614: the land-path accumulation half — a correction enters state
    exactly when the report that found it lands.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._rr = patch.object(watch_runner, "REPO_ROOT", self.tmp)
        self._rr.start()
        self._log = patch.object(watch_runner, "_append_runner_log")
        self._log.start()

    def tearDown(self) -> None:
        self._log.stop()
        self._rr.stop()
        self._tmp.cleanup()

    def _report(self, slug: str, corrections_yaml: str) -> Path:
        rp = self.tmp / f"{slug}.md"
        rp.write_text(
            f"---\nheadline: x\nbrief_corrections:\n{corrections_yaml}\n---\n\n# Body\n",
            encoding="utf-8",
        )
        return rp

    def test_new_correction_is_appended_with_stable_id(self) -> None:
        rp = self._report("alpha", "  - the scope is stale")
        state: dict = {"schema_version": 1, "topics": {}}
        added = watch_runner.accumulate_brief_corrections(state, "alpha", rp, date(2026, 8, 6))
        self.assertEqual(len(added), 1)
        bucket = state["brief_corrections"]["alpha"]
        self.assertEqual(len(bucket), 1)
        self.assertEqual(bucket[0]["id"], added[0])
        self.assertEqual(bucket[0]["text"], "the scope is stale")
        self.assertEqual(bucket[0]["source_date"], "2026-08-06")
        self.assertEqual(bucket[0]["source_report"], "alpha.md")

    def test_reaccumulating_identical_text_is_deduped(self) -> None:
        rp = self._report("alpha", "  - the scope is stale")
        state: dict = {"schema_version": 1, "topics": {}}
        watch_runner.accumulate_brief_corrections(state, "alpha", rp, date(2026, 8, 6))
        added_again = watch_runner.accumulate_brief_corrections(
            state, "alpha", rp, date(2026, 8, 7)
        )
        self.assertEqual(added_again, [])
        self.assertEqual(len(state["brief_corrections"]["alpha"]), 1)

    def test_separate_topics_get_separate_buckets(self) -> None:
        rp_a = self._report("alpha", "  - alpha defect")
        rp_b = self._report("beta", "  - beta defect")
        state: dict = {"schema_version": 1, "topics": {}}
        watch_runner.accumulate_brief_corrections(state, "alpha", rp_a, date(2026, 8, 6))
        watch_runner.accumulate_brief_corrections(state, "beta", rp_b, date(2026, 8, 6))
        self.assertEqual(len(state["brief_corrections"]["alpha"]), 1)
        self.assertEqual(len(state["brief_corrections"]["beta"]), 1)
        self.assertNotEqual(
            state["brief_corrections"]["alpha"][0]["id"],
            state["brief_corrections"]["beta"][0]["id"],
        )

    def test_no_corrections_leaves_state_untouched(self) -> None:
        rp = self.tmp / "alpha.md"
        rp.write_text("---\nheadline: x\n---\n\n# Body\n", encoding="utf-8")
        state: dict = {"schema_version": 1, "topics": {}}
        added = watch_runner.accumulate_brief_corrections(state, "alpha", rp, date(2026, 8, 6))
        self.assertEqual(added, [])
        self.assertNotIn("brief_corrections", state)


class TestClearCorrection(unittest.TestCase):
    """T-2614: the operator ack/clear path — run after folding a correction
    into knowledge/watch/topics.md by hand.
    """

    def test_clears_existing_id_and_pops_empty_bucket(self) -> None:
        state = {
            "schema_version": 1,
            "topics": {},
            "brief_corrections": {"alpha": [{"id": "abc123", "text": "x",
                                              "source_date": "2026-08-06",
                                              "source_report": "r.md"}]},
        }
        found = watch_runner.clear_correction(state, "alpha", "abc123")
        self.assertTrue(found)
        self.assertNotIn("alpha", state["brief_corrections"])

    def test_clears_one_of_several_leaves_bucket(self) -> None:
        state = {
            "schema_version": 1,
            "topics": {},
            "brief_corrections": {"alpha": [
                {"id": "a1", "text": "x", "source_date": "d", "source_report": "r"},
                {"id": "a2", "text": "y", "source_date": "d", "source_report": "r"},
            ]},
        }
        found = watch_runner.clear_correction(state, "alpha", "a1")
        self.assertTrue(found)
        self.assertEqual([c["id"] for c in state["brief_corrections"]["alpha"]], ["a2"])

    def test_unknown_id_returns_false_state_untouched(self) -> None:
        state = {
            "schema_version": 1,
            "topics": {},
            "brief_corrections": {"alpha": [{"id": "abc123", "text": "x",
                                              "source_date": "d", "source_report": "r"}]},
        }
        found = watch_runner.clear_correction(state, "alpha", "ghost")
        self.assertFalse(found)
        self.assertEqual(len(state["brief_corrections"]["alpha"]), 1)

    def test_unknown_topic_returns_false(self) -> None:
        state = {"schema_version": 1, "topics": {}}
        self.assertFalse(watch_runner.clear_correction(state, "nope", "id"))


class _LandHarness:
    """Shared TestLandReports fixture/helpers (no tests of its own)."""

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

    # T-5500: an above-bar report that skipped the T-5213 readback step gets a
    # stderr WARN and a day-index marker, but still lands (warn, never refuse).
    def _write_fm_report(self, slug: str, fm_lines: str) -> None:
        (self.date_dir / f"{slug}.md").write_text(
            "---\n" + fm_lines + "headline: Alpha line.\n---\n\n# Test report\n\n## Findings\n\n"
            + ("Filler body text to cross the substantive-report size floor. " * 10)
            + "\n\n## Sources\n\n- (test)\n",
            encoding="utf-8",
        )

    def _land_capturing_stderr(self) -> tuple[int, str, str]:
        stderr = io.StringIO()
        with patch.object(watch_runner, "git_commit_and_push",
                          return_value=(True, "ok")), \
             patch.object(watch_runner, "actuate_phase"), \
             patch("sys.stderr", stderr):
            rc = watch_runner.land_reports(date(2026, 6, 5), force=True)
        index = (self.date_dir / "README.md").read_text(encoding="utf-8")
        return rc, stderr.getvalue(), index


class TestLandReports(_LandHarness, unittest.TestCase):
    """ORG-025/T-0855 in-session land path: derive status from disk, append
    day-index rows, update state, commit, actuate.
    """

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
        stderr = io.StringIO()
        with patch.object(watch_runner, "git_commit_and_push",
                          return_value=(False, "HERMES guard refused: x")), \
             patch.object(watch_runner, "actuate_phase") as mock_act, \
             patch("sys.stderr", stderr):
            rc = watch_runner.land_reports(date(2026, 6, 5), force=True)
        self.assertEqual(rc, 1)
        # T-1739 GAP 1: a failed land (reports uncommitted/unpushed) must not
        # actuate — that's what filed 37 tickets off orphaned reports.
        self.assertFalse(mock_act.called)
        self.assertIn("--actuate-only", stderr.getvalue())

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

    def test_above_bar_without_readback_warns_and_marks_but_lands(self) -> None:
        self._write_fm_report("alpha", "surface: above-bar\n")
        rc, err, index = self._land_capturing_stderr()
        self.assertEqual(rc, 0)
        self.assertIn("WARN", err)
        self.assertIn("alpha.md", err)
        self.assertIn(
            "| [alpha](alpha.md) | ok | TL;DR: Alpha line." + watch_runner.READBACK_MISSING_MARK + " |",
            index,
        )
        self.assertEqual(self._saved_state["topics"]["alpha"]["last_outcome"], "ok")

    def test_above_bar_with_readback_is_silent(self) -> None:
        self._write_fm_report("alpha", "surface: above-bar\nreadback: confirmed: https://nvd.nist.gov/x\n")
        rc, err, index = self._land_capturing_stderr()
        self.assertEqual(rc, 0)
        self.assertNotIn("readback", err)
        self.assertNotIn(watch_runner.READBACK_MISSING_MARK, index)

    def test_above_bar_with_empty_readback_still_warns(self) -> None:
        self._write_fm_report("alpha", "surface: above-bar\nreadback:\n")
        _rc, err, index = self._land_capturing_stderr()
        self.assertIn("alpha.md", err)
        self.assertIn(watch_runner.READBACK_MISSING_MARK, index)

    def test_routine_without_readback_is_silent(self) -> None:
        self._write_fm_report("alpha", "surface: routine\n")
        _rc, err, index = self._land_capturing_stderr()
        self.assertNotIn("readback", err)
        self.assertNotIn(watch_runner.READBACK_MISSING_MARK, index)

    def test_landed_report_with_corrections_accumulates_into_state(self) -> None:
        rp = self.date_dir / "alpha.md"
        rp.write_text(
            "---\nsurface: routine\nheadline: Alpha line.\nbrief_corrections:\n"
            "  - seed source is 404; correct URL is Y\n"
            "---\n\n# Test report\n\n## Findings\n\n"
            + ("Filler body text to cross the substantive-report size floor. " * 10)
            + "\n\n## Sources\n\n- (test)\n",
            encoding="utf-8",
        )
        with patch.object(watch_runner, "git_commit_and_push",
                          return_value=(True, "ok")), \
             patch.object(watch_runner, "actuate_phase"):
            watch_runner.land_reports(date(2026, 6, 5), force=True)
        bucket = self._saved_state["brief_corrections"]["alpha"]
        self.assertEqual(len(bucket), 1)
        self.assertEqual(bucket[0]["text"], "seed source is 404; correct URL is Y")
        self.assertEqual(bucket[0]["source_date"], "2026-06-05")

    def test_rerun_does_not_duplicate_accumulated_correction(self) -> None:
        rp = self.date_dir / "alpha.md"
        rp.write_text(
            "---\nsurface: routine\nheadline: Alpha line.\nbrief_corrections:\n"
            "  - seed source is 404\n"
            "---\n\n# Test report\n\n## Findings\n\n"
            + ("Filler body text to cross the substantive-report size floor. " * 10)
            + "\n\n## Sources\n\n- (test)\n",
            encoding="utf-8",
        )
        with patch.object(watch_runner, "git_commit_and_push",
                          return_value=(True, "ok")), \
             patch.object(watch_runner, "actuate_phase"):
            watch_runner.land_reports(date(2026, 6, 5), force=True)
            watch_runner.land_reports(date(2026, 6, 5), force=True)
        self.assertEqual(len(self._saved_state["brief_corrections"]["alpha"]), 1)

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


class TestLandReportsSnapshotGate(unittest.TestCase):
    TODAY = date(2026, 11, 1)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.reports_root = self.root / "knowledge" / "watch" / "reports"
        self.date_dir = self.reports_root / self.TODAY.isoformat()
        self.date_dir.mkdir(parents=True)
        self.state = {"schema_version": 1, "topics": {}}
        self.policy = Topic(
            slug="anthropic-policy-and-billing",
            cadence="weekly",
            budget=300,
            scope="s",
            seed_sources=[],
            notes="",
        )
        self.alpha = _topic("alpha")
        self.patches = [
            patch.object(watch_runner, "REPORTS_ROOT", self.reports_root),
            patch.object(watch_runner, "load_state", return_value=self.state),
            patch.object(watch_runner, "_append_runner_log"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in self.patches:
            item.stop()
        self._tmp.cleanup()

    def test_configured_capture_precedes_ok_state_index_commit_and_actuation(self) -> None:
        _write_landed_report(self.date_dir / f"{self.policy.slug}.md")
        events: list[str] = []
        real_append = watch_runner.append_day_index_entry
        real_update = watch_runner.update_state
        with patch.object(watch_runner, "parse_topics", return_value=[self.policy]), \
             patch.object(
                 watch_runner,
                 "capture_topic_snapshot",
                 side_effect=lambda *_args, **_kwargs: events.append("snapshot"),
             ) as capture, \
             patch.object(
                 watch_runner,
                 "append_day_index_entry",
                 side_effect=lambda *args: (events.append("index"), real_append(*args))[1],
             ), \
             patch.object(
                 watch_runner,
                 "update_state",
                 side_effect=lambda *args: (events.append("state"), real_update(*args))[1],
             ), \
             patch.object(
                 watch_runner,
                 "save_state",
                 side_effect=lambda *_args: events.append("save"),
             ), \
             patch.object(
                 watch_runner,
                 "git_commit_and_push",
                 side_effect=lambda *_args, **_kwargs: (events.append("commit") or True, "ok"),
             ), \
             patch.object(
                 watch_runner,
                 "actuate_phase",
                 side_effect=lambda *_args, **_kwargs: events.append("actuate"),
             ):
            rc = watch_runner.land_reports(self.TODAY, force=True)
        self.assertEqual(rc, 0)
        capture.assert_called_once_with(self.policy.slug, self.TODAY)
        self.assertEqual(events, ["snapshot", "index", "state", "save", "commit", "actuate"])

    def test_capture_failure_leaves_batch_without_bookkeeping_commit_or_actuation(self) -> None:
        _write_landed_report(self.date_dir / "alpha.md")
        _write_landed_report(self.date_dir / f"{self.policy.slug}.md")
        with patch.object(watch_runner, "parse_topics", return_value=[self.alpha, self.policy]), \
             patch.object(
                 watch_runner,
                 "capture_topic_snapshot",
                 side_effect=watch_runner.SnapshotCaptureError("network down"),
             ), \
             patch.object(watch_runner, "save_state") as save, \
             patch.object(watch_runner, "git_commit_and_push") as commit, \
             patch.object(watch_runner, "actuate_phase") as actuate, \
             patch("sys.stderr", new_callable=io.StringIO):
            rc = watch_runner.land_reports(self.TODAY, force=True)
        self.assertEqual(rc, 1)
        self.assertFalse((self.date_dir / "README.md").exists())
        self.assertEqual(self.state, {"schema_version": 1, "topics": {}})
        save.assert_not_called()
        commit.assert_not_called()
        actuate.assert_not_called()

    def test_failed_configured_report_does_not_capture_or_require_snapshot(self) -> None:
        with patch.object(watch_runner, "parse_topics", return_value=[self.policy]), \
             patch.object(watch_runner, "capture_topic_snapshot") as capture, \
             patch.object(watch_runner, "save_state"), \
             patch.object(
                 watch_runner, "git_commit_and_push", return_value=(True, "ok")
             ) as commit, \
             patch.object(watch_runner, "actuate_phase"):
            rc = watch_runner.land_reports(self.TODAY, force=True)
        self.assertEqual(rc, 1)
        capture.assert_not_called()
        self.assertEqual(commit.call_args.args[2], {self.policy.slug: "fail"})
        index = (self.date_dir / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"[{self.policy.slug}]({self.policy.slug}.md) | fail", index)

    def test_unrelated_topic_never_fetches_or_requires_snapshot(self) -> None:
        _write_landed_report(self.date_dir / "alpha.md")
        with patch.object(watch_runner, "parse_topics", return_value=[self.alpha]), \
             patch.object(watch_runner, "capture_topic_snapshot") as capture, \
             patch.object(watch_runner, "save_state"), \
             patch.object(
                 watch_runner, "git_commit_and_push", return_value=(True, "ok")
             ), \
             patch.object(watch_runner, "actuate_phase"):
            rc = watch_runner.land_reports(self.TODAY, force=True)
        self.assertEqual(rc, 0)
        capture.assert_not_called()


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
        self.assertEqual(argv[argv.index("--target") + 1], "role:research_orchestrator")
        self.assertEqual(argv[argv.index("--requester") + 1], "specialist")
        ask = argv[argv.index("--ask") + 1]
        self.assertIn("alpha", ask)
        self.assertIn("--emit-due-briefs", ask)
        self.assertIn("--land-commit", ask)
        # T-5213: above-bar headline claims get a guarded primary-source readback
        # before land; routine reports skip it.
        self.assertIn("surface: above-bar", ask)
        self.assertIn("readback:", ask)
        self.assertLess(ask.index("readback:"), ask.index("--land-commit"))
        self.assertIn("web-research-isolation", ask)
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
                # T-4885: the file verb also asks `--name-status` (ADD assertion).
                out = "\n".join(
                    (f"A\t{x}" if "--name-status" in argv else x)
                    for x in sorted(self._staged)
                )
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
        self.assertEqual(rc, ticket.EXIT_REFUSAL)  # T-4496: a caller refusal, not a fault
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
            clear_correction=False, topic=None, id=None,
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

    def test_clear_correction_requires_topic_and_id(self) -> None:
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = watch_runner.main(["--clear-correction"])
        self.assertEqual(rc, 64)
        self.assertIn("--topic", buf.getvalue())
        self.assertIn("--id", buf.getvalue())

    def test_clear_correction_requires_id_even_with_topic(self) -> None:
        from contextlib import redirect_stderr
        with redirect_stderr(io.StringIO()):
            rc = watch_runner.main(["--clear-correction", "--topic", "alpha"])
        self.assertEqual(rc, 64)

    def test_clear_correction_plus_land_commit_is_ambiguous(self) -> None:
        from contextlib import redirect_stderr
        with redirect_stderr(io.StringIO()):
            rc = watch_runner.main([
                "--clear-correction", "--topic", "alpha", "--id", "x",
                "--land-commit",
            ])
        self.assertEqual(rc, 64)

    def test_clear_correction_plus_emit_due_briefs_is_ambiguous(self) -> None:
        from contextlib import redirect_stderr
        with redirect_stderr(io.StringIO()):
            rc = watch_runner.main([
                "--clear-correction", "--topic", "alpha", "--id", "x",
                "--emit-due-briefs",
            ])
        self.assertEqual(rc, 64)


class TestClearCorrectionCLI(unittest.TestCase):
    """T-2614: `--clear-correction` end-to-end through main()."""

    def test_found_clears_and_saves_returns_0(self) -> None:
        state = {
            "schema_version": 1,
            "topics": {},
            "brief_corrections": {"alpha": [{"id": "abc123", "text": "x",
                                              "source_date": "d", "source_report": "r"}]},
        }
        saved: dict = {}
        with patch.object(watch_runner, "load_state", return_value=state), \
             patch.object(watch_runner, "save_state", side_effect=lambda s: saved.update(s)), \
             patch.object(watch_runner, "_append_runner_log") as mock_log, \
             patch("sys.stdout", new_callable=io.StringIO):
            rc = watch_runner.main([
                "--clear-correction", "--topic", "alpha", "--id", "abc123",
                "--note", "folded into topics.md",
            ])
        self.assertEqual(rc, 0)
        self.assertNotIn("alpha", saved["brief_corrections"])
        self.assertTrue(mock_log.called)
        self.assertIn("folded into topics.md", mock_log.call_args.args[0])

    def test_not_found_returns_1_no_save(self) -> None:
        state = {"schema_version": 1, "topics": {}}
        with patch.object(watch_runner, "load_state", return_value=state), \
             patch.object(watch_runner, "save_state") as mock_save, \
             patch("sys.stderr", new_callable=io.StringIO):
            rc = watch_runner.main([
                "--clear-correction", "--topic", "alpha", "--id", "ghost",
            ])
        self.assertEqual(rc, 1)
        self.assertFalse(mock_save.called)


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


class TestSnapshotLayer3Integration(unittest.TestCase):
    DATE = "2026-11-01"
    TOPIC = _topic("anthropic-policy-and-billing")
    SNAPSHOT_REL = "knowledge/watch/snapshots/legal-and-compliance/2026-11-01.md"
    REPORT_REL = "knowledge/watch/reports/2026-11-01/anthropic-policy-and-billing.md"

    def setUp(self) -> None:
        from tools._test_fixtures import setup_integration_env, snapshot_real_repo
        self.real_snapshot = snapshot_real_repo()
        self.env = setup_integration_env()
        os.environ[_GIT_MUTATION_LOCK_ENV] = str(self.env.base_dir / "watch.lock")

    def tearDown(self) -> None:
        from tools._test_fixtures import teardown_integration_env, assert_real_repo_untouched
        os.environ.pop(_GIT_MUTATION_LOCK_ENV, None)
        teardown_integration_env(self.env)
        assert_real_repo_untouched(self.real_snapshot)

    def _run_git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or self.env.repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def _write_report(self, headline: str) -> None:
        path = self.env.repo_path / self.REPORT_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_landed_report(path, headline=headline)

    def _capture(self, body: bytes) -> None:
        with patch.object(watch_runner, "_append_runner_log"):
            watch_runner.capture_topic_snapshot(
                self.TOPIC.slug,
                date.fromisoformat(self.DATE),
                repo_root=self.env.repo_path,
                opener=_snapshot_opener(_SnapshotResponse(body)),
            )

    def _land(self) -> tuple[bool, str]:
        return watch_runner.git_commit_and_push(
            self.DATE,
            [self.TOPIC],
            {self.TOPIC.slug: "ok"},
            cwd=self.env.repo_path,
        )

    def test_exact_paths_commit_to_tmp_origin_and_same_day_history_is_recoverable(self) -> None:
        self._write_report("first")
        self._capture(b"first-body")
        ok, msg = self._land()
        self.assertTrue(ok, msg)

        self._write_report("second")
        self._capture(b"second-body")
        ok, msg = self._land()
        self.assertTrue(ok, msg)

        current = self._run_git(
            f"--git-dir={self.env.bare_origin}", "show", f"main:{self.SNAPSHOT_REL}"
        ).encode()
        previous = self._run_git(
            f"--git-dir={self.env.bare_origin}", "show", f"main^:{self.SNAPSHOT_REL}"
        ).encode()
        self.assertEqual(current, b"second-body")
        self.assertEqual(previous, b"first-body")
        paths = self._run_git(
            f"--git-dir={self.env.bare_origin}",
            "ls-tree", "-r", "--name-only", "main", "knowledge/watch/snapshots",
        ).splitlines()
        self.assertEqual(paths, [self.SNAPSHOT_REL])

    def test_unexpected_prestaged_snapshot_refuses_with_clean_index_and_remote(self) -> None:
        self._write_report("first")
        self._capture(b"body")
        rogue_rel = "knowledge/watch/snapshots/other/2026-11-01.md"
        rogue = self.env.repo_path / rogue_rel
        rogue.parent.mkdir(parents=True)
        rogue.write_bytes(b"rogue")
        self._run_git("add", "--", rogue_rel)

        ok, msg = self._land()
        self.assertFalse(ok)
        self.assertIn("scope guard refused", msg)
        self.assertEqual(self._run_git("diff", "--cached", "--name-only"), "")
        remote_head = self._run_git(
            f"--git-dir={self.env.bare_origin}", "rev-parse", "main"
        ).strip()
        self.assertEqual(remote_head, self.env.baseline_head)


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

        # T-1774: isolate the git_mutation_lock lockfile — see module-level
        # comment on _GIT_MUTATION_LOCK_ENV.
        os.environ[_GIT_MUTATION_LOCK_ENV] = os.path.join(
            self._tmp.name, "gitlock", "ops.lock"
        )

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
        os.environ.pop(_GIT_MUTATION_LOCK_ENV, None)
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

    def test_t2505_exact_dropped_specimens_are_lossless(self) -> None:
        """The observed sentinel is ignored and both actionable colon forms parse."""
        trigger_name = "HERME" + "S.md"
        body = (
            "## Follow-ups\n"
            "None. Routine monitoring sufficient.\n"
            "- **Entitlement error remediation:** ...file a ticket to HO with full "
            "JSON-RPC error log...\n"
            f"- **{trigger_name} classifier verification:** Monitor for public "
            "changelog or incident report confirming classifier fix...\n"
        )
        result = watch_runner.parse_followups_detailed(body)
        self.assertEqual(
            result.parsed,
            (
                (
                    "Entitlement error remediation",
                    "...file a ticket to HO with full JSON-RPC error log...",
                ),
                (
                    f"{trigger_name} classifier verification",
                    "Monitor for public changelog or incident report confirming "
                    "classifier fix...",
                ),
            ),
        )
        self.assertEqual(result.rejected, ())

    def test_accepts_ascii_hyphen_plain_colon_and_bold_outside_colon(self) -> None:
        body = (
            "## Follow-ups\n"
            "ASCII title - ASCII ask\n"
            "Plain title: plain ask\n"
            "**Bold title**: bold ask\n"
        )
        self.assertEqual(
            watch_runner.parse_followups(body),
            [
                ("ASCII title", "ASCII ask"),
                ("Plain title", "plain ask"),
                ("Bold title", "bold ask"),
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
        """The compatibility wrapper omits rejected lines but reports preservation."""
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
        self.assertIn("preserving malformed", buf.getvalue())

        detailed = watch_runner.parse_followups_detailed(body)
        self.assertEqual(detailed.rejected, ("no-dash-here",))

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
        """More than the cap preserves overflow for a single recovery ticket."""
        cap = watch_runner._FOLLOWUP_SOFT_CAP
        lines = "\n".join(f"title-{i} — ask-{i}" for i in range(cap + 5))
        body = f"## Follow-ups\n{lines}\n"
        import io
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            result = watch_runner.parse_followups_detailed(body)
        self.assertEqual(len(result.parsed), cap)
        self.assertEqual(len(result.rejected), 5)
        self.assertEqual(result.rejected[0], f"title-{cap} — ask-{cap}")
        self.assertIn("follow-up cap", buf.getvalue())

    def test_t3069_none_titled_line_with_prose_ask_is_no_op(self) -> None:
        """T-3069: `- None — <explanation>` parses as (title='None', ask='<explanation>')
        but is still a no-follow-up declaration. Pre-split sentinel-check catches
        bare `- None` / `- No follow-ups`; post-split guard extends coverage to lines
        where researcher wrote a None-declaration WITH a trailing explanation after
        the em-dash separator.

        Observed live 2026-08-21 (T-3057 specialist watch fire): a Follow-ups section
        containing `- None — all findings inform existing Pantheon operational
        posture...` produced a ticket titled 'None' with the explanation as its ask.
        Both no-file semantics under the fix."""
        body_bare = (
            "## Follow-ups\n"
            "- None\n"
        )
        # Pre-existing bare-sentinel behavior (regression guard) — no items filed.
        self.assertEqual(watch_runner.parse_followups(body_bare), [])

        body_with_prose = (
            "## Follow-ups\n"
            "- None — all findings inform existing Pantheon operational posture. "
            "No new action items surface from this review.\n"
        )
        # Post-split guard (this ADR's fix) — the 'None' title is still a no-op declaration.
        self.assertEqual(watch_runner.parse_followups(body_with_prose), [])

        # And a real follow-up on the same section still files normally.
        body_mixed = (
            "## Follow-ups\n"
            "- None — no items surface from this cycle.\n"
            "- Real ask — actionable finding here.\n"
        )
        self.assertEqual(
            watch_runner.parse_followups(body_mixed),
            [("Real ask", "actionable finding here.")],
        )

        # Case-insensitive sentinel matching — 'NONE' should also be treated as no-op.
        body_upper = (
            "## Follow-ups\n"
            "- NONE — explanation.\n"
        )
        self.assertEqual(watch_runner.parse_followups(body_upper), [])


class TestParseFollowupsHeadingShapes(unittest.TestCase):
    """T-4302: numbered / section-sign follow-up headings must parse (not silently drop),
    and any follow-ups-like heading that yields nothing must fail LOUDLY.

    Before this fix ``parse_followups_detailed`` matched only the exact heading
    ``## Follow-ups``; real reports also use ``## 5. Follow-ups`` (numbered) and
    ``## § Follow-ups`` (section-sign). Those sections were dropped silently — no
    parsed item, no rejected entry, no trace (7 items lost from the 2026-09-29
    context-management report; 13 numbered/section reports since 2026-08-09).

    Invariant: every follow-up reaches a ticket (parsed) or fails loudly
    (rejected) — never a silent drop.
    """

    def test_t4302_plain_heading_two_items_preserved(self) -> None:
        """Existing behavior preserved: bare ``## Follow-ups`` still parses both items."""
        body = (
            "## Follow-ups\n"
            "title-a — ask-a\n"
            "title-b — ask-b\n"
        )
        result = watch_runner.parse_followups_detailed(body)
        self.assertEqual(result.parsed, (("title-a", "ask-a"), ("title-b", "ask-b")))
        self.assertEqual(result.rejected, ())

    def test_t4302_numbered_heading_parsed(self) -> None:
        """``## 5. Follow-ups`` and its ``5``/``5)`` variants parse (was silently dropped)."""
        for heading in ("## 5. Follow-ups", "## 5 Follow-ups", "## 5) Follow-ups"):
            with self.subTest(heading=heading):
                body = (
                    f"{heading}\n"
                    "title-a — ask-a\n"
                    "title-b — ask-b\n"
                )
                result = watch_runner.parse_followups_detailed(body)
                self.assertEqual(
                    result.parsed,
                    (("title-a", "ask-a"), ("title-b", "ask-b")),
                )
                self.assertEqual(result.rejected, ())

    def test_t4302_section_sign_heading_parsed(self) -> None:
        """``## § Follow-ups`` and its ``§5``/``§5.`` variants parse (was silently dropped)."""
        for heading in ("## § Follow-ups", "## §5 Follow-ups", "## §5. Follow-ups"):
            with self.subTest(heading=heading):
                body = (
                    f"{heading}\n"
                    "title-a — ask-a\n"
                    "title-b — ask-b\n"
                )
                result = watch_runner.parse_followups_detailed(body)
                self.assertEqual(
                    result.parsed,
                    (("title-a", "ask-a"), ("title-b", "ask-b")),
                )
                self.assertEqual(result.rejected, ())

    def test_t4302_resolved_from_heading_ignored_quietly(self) -> None:
        """``## Follow-ups Resolved From ...`` (trailing words) is a non-filing section:
        zero parsed AND zero rejected, and it must NOT trip the loud path.

        mcp-supply-chain 2026-09-21 uses that heading legitimately. The ``\\s*$``
        end-anchor excludes it from the strict regex; the ``resolved`` guard excludes
        it from the loose fail-loud detector.
        """
        body = (
            "## Follow-ups Resolved From last week\n"
            "- item-a — done last cycle\n"
            "- item-b — also done\n"
        )
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            result = watch_runner.parse_followups_detailed(body)
        self.assertEqual(result.parsed, ())
        self.assertEqual(result.rejected, ())
        self.assertNotIn("[actuation] followup:", buf.getvalue())

    def test_t4302_empty_or_sentinel_section_not_loud(self) -> None:
        """A legitimately empty or sentinel-only ``## Follow-ups`` is intentional absence:
        zero parsed, zero rejected, and NOT loud."""
        body_empty = (
            "## Follow-ups\n"
            "## Sources\n"
            "source-line — ignored\n"
        )
        body_sentinel = (
            "## Follow-ups\n"
            "- None\n"
        )
        for body in (body_empty, body_sentinel):
            with self.subTest(body=body):
                buf = io.StringIO()
                with patch("sys.stderr", buf):
                    result = watch_runner.parse_followups_detailed(body)
                self.assertEqual(result.parsed, ())
                self.assertEqual(result.rejected, ())
                self.assertNotIn("[actuation] followup:", buf.getvalue())

    def test_t4302_unparsed_heading_fails_loud(self) -> None:
        """A follow-ups-like heading the strict regex declines but the loose detector
        flags (content present) must produce at least one LOUD rejected entry —
        never a silent zero/zero drop."""
        for heading in ("## Follow-up items:", "## Followups (misc)"):
            with self.subTest(heading=heading):
                body = (
                    f"{heading}\n"
                    "- something actionable that would otherwise vanish\n"
                )
                buf = io.StringIO()
                with patch("sys.stderr", buf):
                    result = watch_runner.parse_followups_detailed(body)
                self.assertEqual(result.parsed, ())
                self.assertGreaterEqual(len(result.rejected), 1)
                self.assertTrue(
                    any(
                        r.startswith("[unparsed-heading]") and heading in r
                        for r in result.rejected
                    ),
                    result.rejected,
                )
                self.assertIn("[actuation] followup:", buf.getvalue())


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

    def test_rejected_catchall_files_exactly_once_across_two_actuations(self) -> None:
        """The recovery ticket alone carries the report-level dedup key."""
        report_body = "## Follow-ups\nunstructured actionable prose\n"
        tmp_path, _ = self._make_tmp(report_body)
        recovery_calls = []

        def fake_recovery(lines, finding, *, parent_id, **kwargs):
            recovery_calls.append((tuple(lines), finding.report_relpath, parent_id))
            (tmp_path / "tickets" / "inbox" / "T-2001.md").write_text(
                f"Source: {finding.report_relpath}\n",
                encoding="utf-8",
            )
            return ("filed", "T-2001", "retries=0")

        patches = (
            patch("tools.watch_runner.REPO_ROOT", tmp_path),
            patch(
                "tools.watch_runner.REPORTS_ROOT",
                tmp_path / "watch" / "reports",
            ),
            patch(
                "tools.watch_runner.file_finding",
                return_value=("errored", None, "synthetic headline failure"),
            ),
            patch(
                "tools.watch_runner.file_unparsed_followups",
                side_effect=fake_recovery,
            ),
            patch("tools.watch_runner.log_actuation_summary"),
            patch("tools.watch_runner.write_actuation_ledger"),
            patch(
                "tools.watch_runner.git_commit_ledger_entry",
                return_value=(True, "ok"),
            ),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6]:
            watch_runner.actuate_phase(date(2026, 5, 4), [], {})
            watch_runner.actuate_phase(date(2026, 5, 4), [], {})

        self.assertEqual(
            recovery_calls,
            [(
                ("unstructured actionable prose",),
                "watch/reports/2026-05-04/mcp-supply-chain.md",
                None,
            )],
        )

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


class TestNormalizeFollowupTitle(unittest.TestCase):
    """T-3172: unit tests for the sorted-token-bag dedup key."""

    def test_exact_duplicate_normalizes_equal(self) -> None:
        self.assertEqual(
            watch_runner._normalize_followup_title("MCP supply-chain audit"),
            watch_runner._normalize_followup_title("MCP supply chain audit"),
        )

    def test_reordered_words_normalize_equal(self) -> None:
        """Word-order variance is the shape plain punctuation-strip would miss."""
        self.assertEqual(
            watch_runner._normalize_followup_title("MCP supply chain audit"),
            watch_runner._normalize_followup_title("Audit MCP supply chain"),
        )

    def test_distinct_titles_normalize_different(self) -> None:
        self.assertNotEqual(
            watch_runner._normalize_followup_title("MCP supply chain audit"),
            watch_runner._normalize_followup_title("Echo SDK check"),
        )

    def test_noise_words_dropped(self) -> None:
        self.assertEqual(
            watch_runner._normalize_followup_title("Verify the permission mode"),
            watch_runner._normalize_followup_title("Verify permission mode"),
        )


class TestActuatePhaseFollowupDedup(unittest.TestCase):
    """T-3172: cross-report follow-up dedup within a single actuate_phase() fire."""

    def _make_tmp(self, reports: dict) -> Path:
        """Build a minimal repo layout with one report file per slug."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        tmp_path = Path(tmp)
        (tmp_path / "tickets" / "inbox").mkdir(parents=True)
        for sub in ("active", "done"):
            (tmp_path / "tickets" / sub).mkdir(parents=True)
        date_dir = tmp_path / "watch" / "reports" / "2026-05-04"
        date_dir.mkdir(parents=True)
        rows = []
        for slug, body in reports.items():
            (date_dir / f"{slug}.md").write_text(body, encoding="utf-8")
            rows.append(f"| [{slug}]({slug}.md) | ok | TL;DR: test finding {slug} |")
        (date_dir / "README.md").write_text(
            "# 2026-05-04\n\n"
            "| Topic | Status | One-line summary |\n"
            "|---|---|---|\n" + "\n".join(rows) + "\n",
            encoding="utf-8",
        )
        return tmp_path

    def test_duplicate_across_reports_skipped(self) -> None:
        """A reordered-title duplicate from a second report collapses onto the first."""
        reports = {
            "topic-a": "# T\n\nBody.\n\n## Follow-ups\nMCP supply chain audit — ask-a\n",
            "topic-b": "# T\n\nBody.\n\n## Follow-ups\nAudit MCP supply chain — ask-b\n",
        }
        tmp_path = self._make_tmp(reports)
        file_followup_calls: list = []

        def fake_file_followup(title, ask, finding, *, parent_id, **kw):
            file_followup_calls.append(title)
            return ("filed", f"T-{2000 + len(file_followup_calls)}", "retries=0")

        with patch("tools.watch_runner.REPO_ROOT", tmp_path), \
             patch("tools.watch_runner.REPORTS_ROOT", tmp_path / "watch" / "reports"), \
             patch("tools.watch_runner.file_finding", return_value=("filed", "T-1001", "retries=0")), \
             patch("tools.watch_runner.file_followup", side_effect=fake_file_followup), \
             patch("tools.watch_runner.log_actuation_summary"), \
             patch("tools.watch_runner.write_actuation_ledger"), \
             patch("tools.watch_runner.git_commit_ledger_entry", return_value=(True, "ok")):
            watch_runner.actuate_phase(date(2026, 5, 4), [], {})

        self.assertEqual(file_followup_calls, ["MCP supply chain audit"])

    def test_distinct_titles_both_filed(self) -> None:
        """Genuinely different follow-up titles across reports both get filed."""
        reports = {
            "topic-a": "# T\n\nBody.\n\n## Follow-ups\nEcho SDK check — ask-a\n",
            "topic-b": "# T\n\nBody.\n\n## Follow-ups\nMCP supply chain audit — ask-b\n",
        }
        tmp_path = self._make_tmp(reports)
        file_followup_calls: list = []

        def fake_file_followup(title, ask, finding, *, parent_id, **kw):
            file_followup_calls.append(title)
            return ("filed", f"T-{2000 + len(file_followup_calls)}", "retries=0")

        with patch("tools.watch_runner.REPO_ROOT", tmp_path), \
             patch("tools.watch_runner.REPORTS_ROOT", tmp_path / "watch" / "reports"), \
             patch("tools.watch_runner.file_finding", return_value=("filed", "T-1001", "retries=0")), \
             patch("tools.watch_runner.file_followup", side_effect=fake_file_followup), \
             patch("tools.watch_runner.log_actuation_summary"), \
             patch("tools.watch_runner.write_actuation_ledger"), \
             patch("tools.watch_runner.git_commit_ledger_entry", return_value=(True, "ok")):
            watch_runner.actuate_phase(date(2026, 5, 4), [], {})

        self.assertEqual(sorted(file_followup_calls), ["Echo SDK check", "MCP supply chain audit"])

    def test_skip_log_emitted(self) -> None:
        """The duplicate-skip line names both the skipped title and its canonical."""
        reports = {
            "topic-a": "# T\n\nBody.\n\n## Follow-ups\nMCP supply chain audit — ask-a\n",
            "topic-b": "# T\n\nBody.\n\n## Follow-ups\nAudit MCP supply chain — ask-b\n",
        }
        tmp_path = self._make_tmp(reports)

        with patch("tools.watch_runner.REPO_ROOT", tmp_path), \
             patch("tools.watch_runner.REPORTS_ROOT", tmp_path / "watch" / "reports"), \
             patch("tools.watch_runner.file_finding", return_value=("filed", "T-1001", "retries=0")), \
             patch("tools.watch_runner.file_followup", return_value=("filed", "T-2001", "retries=0")), \
             patch("tools.watch_runner.log_actuation_summary"), \
             patch("tools.watch_runner.write_actuation_ledger"), \
             patch("tools.watch_runner.git_commit_ledger_entry", return_value=(True, "ok")), \
             patch("sys.stderr", new_callable=io.StringIO) as err:
            watch_runner.actuate_phase(date(2026, 5, 4), [], {})

        self.assertIn("skipping duplicate", err.getvalue())
        self.assertIn("Audit MCP supply chain", err.getvalue())
        self.assertIn("MCP supply chain audit", err.getvalue())


class TestFollowupFireCeiling(unittest.TestCase):
    """T-3172: per-fire soft ceiling with overflow-to-single-summary-ticket."""

    def _make_tmp(self, report_body: str) -> Path:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
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
        return tmp_path

    def test_ceiling_not_triggered_no_summary(self) -> None:
        """Follow-up count under the ceiling never invokes the overflow filer."""
        report_body = (
            "# Test\n\nBody.\n\n"
            "## Follow-ups\n"
            "fu-title-1 — fu-ask-1\n"
            "fu-title-2 — fu-ask-2\n"
        )
        tmp_path = self._make_tmp(report_body)
        overflow_calls: list = []

        with patch("tools.watch_runner.REPO_ROOT", tmp_path), \
             patch("tools.watch_runner.REPORTS_ROOT", tmp_path / "watch" / "reports"), \
             patch("tools.watch_runner._FOLLOWUP_FIRE_CEILING", 2), \
             patch("tools.watch_runner.file_finding", return_value=("filed", "T-1001", "retries=0")), \
             patch("tools.watch_runner.file_followup", return_value=("filed", "T-2001", "retries=0")), \
             patch(
                 "tools.watch_runner.file_followup_overflow_summary",
                 side_effect=lambda *a, **kw: overflow_calls.append(a) or ("filed", "T-9999", "retries=0"),
             ), \
             patch("tools.watch_runner.log_actuation_summary"), \
             patch("tools.watch_runner.write_actuation_ledger"), \
             patch("tools.watch_runner.git_commit_ledger_entry", return_value=(True, "ok")):
            watch_runner.actuate_phase(date(2026, 5, 4), [], {})

        self.assertEqual(len(overflow_calls), 0)

    def test_ceiling_triggered_summary_filed_with_overflow_items(self) -> None:
        """Follow-ups beyond the ceiling stop individual filing and land in one summary call."""
        report_body = (
            "# Test\n\nBody.\n\n"
            "## Follow-ups\n"
            "fu-title-1 — fu-ask-1\n"
            "fu-title-2 — fu-ask-2\n"
            "fu-title-3 — fu-ask-3\n"
            "fu-title-4 — fu-ask-4\n"
        )
        tmp_path = self._make_tmp(report_body)
        file_followup_calls: list = []
        overflow_calls: list = []

        def fake_file_followup(title, ask, finding, *, parent_id, **kw):
            file_followup_calls.append(title)
            return ("filed", f"T-{2000 + len(file_followup_calls)}", "retries=0")

        def fake_overflow(overflow, today, ceiling, **kw):
            overflow_calls.append((list(overflow), today, ceiling))
            return ("filed", "T-9999", "retries=0")

        with patch("tools.watch_runner.REPO_ROOT", tmp_path), \
             patch("tools.watch_runner.REPORTS_ROOT", tmp_path / "watch" / "reports"), \
             patch("tools.watch_runner._FOLLOWUP_FIRE_CEILING", 2), \
             patch("tools.watch_runner.file_finding", return_value=("filed", "T-1001", "retries=0")), \
             patch("tools.watch_runner.file_followup", side_effect=fake_file_followup), \
             patch("tools.watch_runner.file_followup_overflow_summary", side_effect=fake_overflow), \
             patch("tools.watch_runner.log_actuation_summary"), \
             patch("tools.watch_runner.write_actuation_ledger"), \
             patch("tools.watch_runner.git_commit_ledger_entry", return_value=(True, "ok")):
            watch_runner.actuate_phase(date(2026, 5, 4), [], {})

        self.assertEqual(len(file_followup_calls), 2)
        self.assertEqual(len(overflow_calls), 1)
        overflow_items, _today_arg, ceiling_arg = overflow_calls[0]
        self.assertEqual(ceiling_arg, 2)
        self.assertEqual([item[0] for item in overflow_items], ["fu-title-3", "fu-title-4"])


class TestFollowupOverflowSummary(unittest.TestCase):
    """T-3172: unit tests for the overflow-summary ticket body and argv shape."""

    def setUp(self) -> None:
        self.finding_a = Finding(
            slug="topic-a",
            tldr="tldr-a",
            report_relpath="knowledge/watch/reports/2026-05-04/topic-a.md",
            headline_excerpt="",
        )
        self.finding_b = Finding(
            slug="topic-b",
            tldr="tldr-b",
            report_relpath="knowledge/watch/reports/2026-05-04/topic-b.md",
            headline_excerpt="",
        )

    def test_overflow_body_enumerates_items(self) -> None:
        overflow = [
            ("fu-title-1", "fu-ask-1", self.finding_a),
            ("fu-title-2", "fu-ask-2", self.finding_b),
        ]
        ask_body, context_body = watch_runner.build_followup_overflow_body(
            overflow, date(2026, 5, 4), 2
        )
        self.assertIn("fu-title-1", ask_body)
        self.assertIn("fu-ask-1", ask_body)
        self.assertIn(self.finding_a.report_relpath, ask_body)
        self.assertIn("fu-title-2", ask_body)
        self.assertIn("fu-ask-2", ask_body)
        self.assertIn(self.finding_b.report_relpath, ask_body)
        self.assertIn("2026-05-04", context_body)
        self.assertIn("Ceiling: 2", context_body)
        self.assertIn("Overflow count: 2", context_body)

    def test_overflow_summary_argv_shape(self) -> None:
        overflow = [("fu-title-1", "fu-ask-1", self.finding_a)]
        captured = {}

        def fake_invoke(argv, log_tag, env, **kw):
            captured["argv"] = argv
            return ("filed", "T-9999", "retries=0")

        with patch("tools.watch_runner._invoke_ticket_file", side_effect=fake_invoke):
            outcome, ticket_id, _detail = watch_runner.file_followup_overflow_summary(
                overflow, date(2026, 5, 4), 2
            )

        self.assertEqual(outcome, "filed")
        self.assertEqual(ticket_id, "T-9999")
        argv = captured["argv"]
        self.assertEqual(argv[argv.index("--target") + 1], "role:research_orchestrator")
        labels = argv[argv.index("--labels") + 1]
        self.assertIn("actuation-overflow", labels.split(","))
        title = argv[argv.index("--title") + 1]
        self.assertLessEqual(len(title), 120)


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
        self.assertEqual(argv[argv.index("--target") + 1], "role:research_orchestrator")
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

    def test_unparsed_catchall_escapes_classifier_trigger_at_commit_surface(self) -> None:
        """Every ticket.py argument is safe before the auto-commit boundary."""
        trigger_name = "HERME" + "S.md"
        escaped_name = "HERME" + r"S\.md"
        raw_line = (
            f"- **{trigger_name} classifier verification** malformed but actionable"
        )
        with patch("tools.watch_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="filed T-0202 → tickets/inbox/T-0202.md\n",
                stderr="",
            )
            outcome, ticket_id, _ = watch_runner.file_unparsed_followups(
                [raw_line],
                self.finding,
                parent_id="T-0100",
            )

        self.assertEqual((outcome, ticket_id), ("filed", "T-0202"))
        argv = mock_run.call_args.args[0]
        commit_surface = "\n".join(argv)
        self.assertNotIn(trigger_name, commit_surface)
        self.assertIn(escaped_name, commit_surface)
        self.assertIn(self.finding.report_relpath, commit_surface)


class TestIndexUnindexedReports(unittest.TestCase):
    """T-2148 finding 1: reports that reach disk outside topics.md were invisible
    to the day index forever, because the index was driven by the DISPATCH LIST
    rather than by what is actually on disk."""

    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="t2148-")) / "2026-07-27"
        self.d.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.d.parent, ignore_errors=True)

    def _report(self, slug, *, ok=True):
        p = self.d / f"{slug}.md"
        if ok:
            p.write_text("---\nheadline: 'a real finding'\n---\n\n## Section\n\n"
                         + ("body text. " * 200), encoding="utf-8")
        else:
            p.write_text("stub", encoding="utf-8")
        return p

    def test_indexes_a_report_absent_from_topics(self):
        """The actual 2026-07-27 case: an ad-hoc dispatch with no topics.md entry."""
        self._report("semantic-search-graph-db-stack")
        added = wr.index_unindexed_reports(self.d)
        self.assertEqual(added, ["semantic-search-graph-db-stack"])
        self.assertTrue(wr.day_index_has_row(self.d, "semantic-search-graph-db-stack"))

    def test_skips_underscore_runner_artifacts(self):
        """_topic-warnings.md is written with deliberately no README row
        (write_topic_warnings docstring); the sweep must not index it."""
        self._report("_topic-warnings")
        self._report("topic-a")
        self.assertEqual(wr.index_unindexed_reports(self.d), ["topic-a"])
        self.assertFalse(wr.day_index_has_row(self.d, "_topic-warnings"))

    def test_indexes_adhoc_report_without_frontmatter_by_title(self):
        """Hand-delivered one-off reports (e.g. 2026-10-12's ad-hoc evals) carry a
        `# ` title and `## ` sections but no watch-template frontmatter; they are
        real reports, so the sweep indexes them by title instead of leaving them
        unindexed forever."""
        (self.d / "adhoc-eval.md").write_text(
            "# Hook Fail-Closed Mode Evaluation\n\n## Findings\n\n" + ("body. " * 200),
            encoding="utf-8")
        self.assertEqual(wr.index_unindexed_reports(self.d), ["adhoc-eval"])
        row = [l for l in (self.d / "README.md").read_text().splitlines() if "[adhoc-eval]" in l][0]
        self.assertIn("Hook Fail-Closed Mode Evaluation", row)

    def test_still_skips_small_or_unsectioned_adhoc_files(self):
        (self.d / "stub.md").write_text("# Title only\n", encoding="utf-8")
        (self.d / "nosections.md").write_text("# Title\n\n" + ("text " * 300), encoding="utf-8")
        self.assertEqual(wr.index_unindexed_reports(self.d), [])

    def test_is_idempotent(self):
        self._report("topic-a")
        wr.index_unindexed_reports(self.d)
        again = wr.index_unindexed_reports(self.d)
        self.assertEqual(again, [], "a second sweep must not duplicate rows")
        body = (self.d / "README.md").read_text()
        self.assertEqual(body.count("[topic-a]"), 1)

    def test_never_indexes_the_index_itself(self):
        """The README skip must be exercised by the NAME check, not incidentally.

        First version of this test passed even with the name check deleted: a real
        README fails report_landed_ok anyway, so the skip was never reached. Caught
        by mutation. Here the README is deliberately given valid report structure,
        so the name check is the ONLY thing that can prevent it being indexed.
        """
        (self.d / "README.md").write_text(
            "---\nheadline: 'looks exactly like a report'\n---\n\n## Section\n\n"
            + ("body text. " * 200), encoding="utf-8")
        self.assertTrue(wr.report_landed_ok(self.d / "README.md"),
                        "fixture must be structurally valid or the test is vacuous")
        added = wr.index_unindexed_reports(self.d)
        self.assertNotIn("README", added)
        self.assertFalse(wr.day_index_has_row(self.d, "README"))

    def test_skips_a_stub_that_did_not_land(self):
        """Indexing a stub would assert a landing that did not happen — the same
        'created is not delivered' shape this sweep exists to fix."""
        self._report("half-written", ok=False)
        self.assertEqual(wr.index_unindexed_reports(self.d), [])
        self.assertFalse(wr.day_index_has_row(self.d, "half-written"))

    def test_carries_the_headline_into_the_summary(self):
        self._report("with-headline")
        wr.index_unindexed_reports(self.d)
        self.assertIn("a real finding", (self.d / "README.md").read_text())

    def test_missing_directory_is_not_an_error(self):
        self.assertEqual(wr.index_unindexed_reports(self.d.parent / "2099-01-01"), [])


class TopicParseWarningsTest(unittest.TestCase):
    """T-2353: an ordinary topics.md edit must not SILENTLY retire a topic.

    Three paths, all previously indistinguishable from a deliberate `paused`:
    missing cadence (defaulted to paused), typo'd cadence (is_due returns False
    forever), and missing scope (stanza dropped from parse output entirely).
    The third is overloaded: `return None` also legitimately filters prose
    sections, so the fix distinguishes "no recognized field bullets at all"
    (prose — silent) from "looks like a topic but fails to parse" (warn).
    """

    def _topic(self, slug, cadence_line="- **cadence:** weekly", scope_line="- **scope:** watch things"):
        lines = ["", f"## {slug}"]
        if cadence_line:
            lines.append(cadence_line)
        if scope_line:
            lines.append(scope_line)
        return "\n".join(lines) + "\n"

    def _parse(self, body_md):
        import tempfile as _tf, os as _os
        w: list[str] = []
        fd, path = _tf.mkstemp(suffix=".md")
        self.addCleanup(_os.unlink, path)
        _os.write(fd, ("# topics\n" + body_md).encode()); _os.close(fd)
        topics = watch_runner.parse_topics(pathlib.Path(path), warnings=w)
        return topics, w

    def test_missing_cadence_warns_and_stays_fail_safe(self):
        topics, w = self._parse(self._topic("t-a", cadence_line=None))
        self.assertEqual(len(w), 1)
        self.assertIn("t-a", w[0]); self.assertIn("cadence", w[0].lower())
        # fail-safe preserved: the topic exists but does not fire
        self.assertEqual([t.slug for t in topics], ["t-a"])
        self.assertEqual(topics[0].cadence, "paused")

    def test_typod_cadence_warns_and_never_fires(self):
        topics, w = self._parse(self._topic("t-b", cadence_line="- **cadence:** wekly"))
        self.assertEqual(len(w), 1)
        self.assertIn("wekly", w[0])
        self.assertFalse(topics[0].is_due(None, watch_runner.date(2026, 8, 2)))

    def test_missing_scope_with_fields_warns_and_drops(self):
        topics, w = self._parse(self._topic("t-c", scope_line=None))
        self.assertEqual(topics, [])
        self.assertEqual(len(w), 1)
        self.assertIn("t-c", w[0]); self.assertIn("scope", w[0].lower())

    def test_prose_section_stays_silent(self):
        # The load-bearing existing behavior: an H2 with no recognized field
        # bullets is prose (e.g. "Report output convention"), not a failed topic.
        topics, w = self._parse("\n## Report output convention\nJust prose, no field bullets.\n")
        self.assertEqual(topics, [])
        self.assertEqual(w, [])

    def test_clean_topic_produces_no_warnings(self):
        # T-2433: slug must be one that IS in TOPIC_POLICY. "Clean" now means
        # both well-formed AND registered — an active topic missing a policy
        # entry is itself a warnable condition (see UnregisteredTopicPolicyTest).
        # Asserted here rather than hard-coded so the test cannot silently go
        # vacuous if the topic is ever renamed or de-registered.
        slug = "context-management"
        self.assertIn(slug, watch_runner.TOPIC_POLICY)
        topics, w = self._parse(self._topic(slug))
        self.assertEqual([t.slug for t in topics], [slug])
        self.assertEqual(w, [])

    def test_warnings_param_is_optional(self):
        # The four existing mocks return bare lists; real callers may omit the
        # accumulator. Signature must tolerate both.
        import tempfile as _tf, os as _os
        fd, path = _tf.mkstemp(suffix=".md")
        self.addCleanup(_os.unlink, path)
        _os.write(fd, b"# topics\n\n## t-d\n- **cadence:** weekly\n- **scope:** s\n"); _os.close(fd)
        topics = watch_runner.parse_topics(pathlib.Path(path))
        self.assertEqual(len(topics), 1)


class UnregisteredTopicPolicyTest(unittest.TestCase):
    """T-2433: an ACTIVE topic with no TOPIC_POLICY entry must warn LOUDLY.

    The failure it prevents is silent by construction: such a topic falls
    through to _DEFAULT_POLICY, so its findings are filed `unknown-topic` +
    `interactive-required`, and inbox_consumer skips interactive-required —
    the dispatch succeeds, the report lands, the ticket exists, and only
    DELIVERY is disabled. Nine tickets across five topics accumulated for 6-13
    days before a hand sweep found them.
    """

    def _parse(self, body_md):
        import tempfile as _tf, os as _os
        w: list[str] = []
        fd, path = _tf.mkstemp(suffix=".md")
        self.addCleanup(_os.unlink, path)
        _os.write(fd, ("# topics\n" + body_md).encode()); _os.close(fd)
        topics = watch_runner.parse_topics(pathlib.Path(path), warnings=w)
        return topics, w

    @staticmethod
    def _stanza(slug, cadence):
        return f"\n## {slug}\n- **cadence:** {cadence}\n- **scope:** watch things\n"

    def test_active_unregistered_topic_warns(self):
        slug = "totally-unregistered-topic"
        self.assertNotIn(slug, watch_runner.TOPIC_POLICY)
        topics, w = self._parse(self._stanza(slug, "weekly"))
        self.assertEqual([t.slug for t in topics], [slug])
        self.assertEqual(len(w), 1)
        self.assertIn(slug, w[0])
        # The warning must name the CONSEQUENCE, not just the condition — an
        # operator reading the digest needs to know delivery is off.
        self.assertIn("TOPIC_POLICY", w[0])
        self.assertIn("interactive-required", w[0])

    def test_paused_unregistered_topic_does_not_warn(self):
        # Paused topics do not dispatch, so they need no policy entry. Warning
        # on them would make the digest noisy forever for five paused stanzas.
        slug = "paused-unregistered-topic"
        self.assertNotIn(slug, watch_runner.TOPIC_POLICY)
        _, w = self._parse(self._stanza(slug, "paused"))
        self.assertEqual(w, [])

    def test_unpausing_without_registering_warns(self):
        # The gate is CADENCE, not presence: the moment a paused topic is made
        # active without a policy entry, it must fail loudly — not a fortnight
        # later when someone notices the silence.
        slug = "paused-unregistered-topic"
        _, w_paused = self._parse(self._stanza(slug, "paused"))
        _, w_active = self._parse(self._stanza(slug, "weekly"))
        self.assertEqual(w_paused, [])
        self.assertEqual(len(w_active), 1)

    def test_invalid_cadence_does_not_stack_a_second_warning(self):
        # An out-of-vocab cadence already warns and can never fire; telling the
        # operator to also register it is noise on a louder problem.
        slug = "bad-cadence-unregistered"
        _, w = self._parse(self._stanza(slug, "wekly"))
        self.assertEqual(len(w), 1)
        self.assertIn("wekly", w[0])

    def test_registered_active_topic_does_not_warn(self):
        slug = "voice-as-interface"
        self.assertIn(slug, watch_runner.TOPIC_POLICY)
        _, w = self._parse(self._stanza(slug, "weekly"))
        self.assertEqual(w, [])

    def test_warning_survives_missing_accumulator(self):
        import tempfile as _tf, os as _os
        fd, path = _tf.mkstemp(suffix=".md")
        self.addCleanup(_os.unlink, path)
        _os.write(fd, ("# topics\n" + self._stanza("x-unregistered", "weekly")).encode())
        _os.close(fd)
        # Must not raise when callers omit the accumulator.
        topics = watch_runner.parse_topics(pathlib.Path(path))
        self.assertEqual([t.slug for t in topics], ["x-unregistered"])

    def test_every_active_topic_in_the_real_topics_file_is_registered(self):
        """The regression this ticket exists to close, asserted on REAL config.

        Non-vacuity: the file must actually contain active topics, or an empty
        topics.md would make this pass while proving nothing.
        """
        w: list[str] = []
        topics = watch_runner.parse_topics(watch_runner.TOPICS_FILE, warnings=w)
        active = [t for t in topics if t.cadence != "paused"]
        self.assertTrue(active, "topics.md has no active topics — test is vacuous")
        unregistered = [t.slug for t in active if t.slug not in watch_runner.TOPIC_POLICY]
        self.assertEqual(
            unregistered, [],
            f"active topics missing a TOPIC_POLICY entry: {unregistered}",
        )


class TopicWarningsReportFileTest(unittest.TestCase):
    """The warning must reach a channel the DIGEST carries — a report-convention
    file in the day's dir. Chosen surface: the digest globs *.md (non-README)
    and reads surface/headline frontmatter, while ACTUATION reads only the
    README index rows — so this file is digest-visible without feeding the
    auto-filing path (bugs/actuator-followup-ticket-explosion-no-dedup)."""

    def test_writes_warning_report_when_warnings_exist(self):
        with tempfile.TemporaryDirectory() as td:
            date_dir = pathlib.Path(td) / "2026-08-02"
            watch_runner.write_topic_warnings(date_dir, ["topic 'x': missing scope — dropped"])
            f = date_dir / "_topic-warnings.md"
            self.assertTrue(f.exists())
            text = f.read_text()
            self.assertIn("surface: above-bar", text)
            self.assertIn("headline:", text)
            self.assertIn("missing scope", text)

    def test_no_file_when_no_warnings(self):
        with tempfile.TemporaryDirectory() as td:
            date_dir = pathlib.Path(td) / "2026-08-02"
            watch_runner.write_topic_warnings(date_dir, [])
            self.assertFalse((date_dir / "_topic-warnings.md").exists())
            self.assertFalse(date_dir.exists(), "no warnings must not even create the dir")



class TestParseReportFrontmatter(unittest.TestCase):
    """T-2523: the line-oriented `--- ... ---` frontmatter reader that carries
    the cross-topic-ownership declaration into extract_findings."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, text: str) -> Path:
        p = self.tmp / "r.md"
        p.write_text(text, encoding="utf-8")
        return p

    def test_reads_flat_keys(self) -> None:
        p = self._write(
            "---\nsurface: above-bar\n"
            "belongs_to_topic: echo-stack-vendor-changes\n---\n\n# body\n"
        )
        fm = watch_runner._parse_report_frontmatter(p)
        self.assertEqual(fm["surface"], "above-bar")
        self.assertEqual(fm["belongs_to_topic"], "echo-stack-vendor-changes")

    def test_no_frontmatter_returns_empty(self) -> None:
        p = self._write("# just a heading\n\nno frontmatter here\n")
        self.assertEqual(watch_runner._parse_report_frontmatter(p), {})

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(
            watch_runner._parse_report_frontmatter(self.tmp / "nope.md"), {}
        )


class TestExtractFindingsBelongsToTopic(unittest.TestCase):
    """T-2523: extract_findings lifts `belongs_to_topic` (and its alias
    `duplicate_of_topic`) from the report frontmatter, and drops a
    self-referential declaration so a stray self-tag cannot suppress a report's
    own actuation."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.date_dir = self.tmp / "2026-08-03"
        self.date_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _readme(self, rows: str) -> None:
        (self.date_dir / "README.md").write_text(
            "# Watch reports — 2026-08-03\n\n"
            "| Topic | Status | One-line summary |\n"
            "|---|---|---|\n" + rows,
            encoding="utf-8",
        )

    def test_declaration_is_lifted(self) -> None:
        (self.date_dir / "voice-as-interface.md").write_text(
            "---\nsurface: above-bar\n"
            "belongs_to_topic: echo-stack-vendor-changes\n---\n\n"
            "# Voice\n\nSpeechmatics billing body.\n",
            encoding="utf-8",
        )
        self._readme(
            "| [voice-as-interface](voice-as-interface.md) | ok | "
            "TL;DR: Speechmatics credit billing |\n"
        )
        with patch("sys.stderr", io.StringIO()):
            findings = watch_runner.extract_findings(self.date_dir, {})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].belongs_to_topic, "echo-stack-vendor-changes")

    def test_alias_duplicate_of_topic_is_lifted(self) -> None:
        (self.date_dir / "voice-as-interface.md").write_text(
            "---\nduplicate_of_topic: echo-stack-vendor-changes\n---\n\n"
            "# Voice\n\nbody.\n",
            encoding="utf-8",
        )
        self._readme(
            "| [voice-as-interface](voice-as-interface.md) | ok | "
            "TL;DR: x |\n"
        )
        with patch("sys.stderr", io.StringIO()):
            findings = watch_runner.extract_findings(self.date_dir, {})
        self.assertEqual(findings[0].belongs_to_topic, "echo-stack-vendor-changes")

    def test_self_reference_is_dropped(self) -> None:
        (self.date_dir / "voice-as-interface.md").write_text(
            "---\nbelongs_to_topic: voice-as-interface\n---\n\n# Voice\n\nbody.\n",
            encoding="utf-8",
        )
        self._readme(
            "| [voice-as-interface](voice-as-interface.md) | ok | TL;DR: x |\n"
        )
        with patch("sys.stderr", io.StringIO()):
            findings = watch_runner.extract_findings(self.date_dir, {})
        self.assertIsNone(findings[0].belongs_to_topic)

    def test_no_declaration_is_none(self) -> None:
        # The different-asks / ordinary case: no frontmatter declaration → the
        # finding files as a normal peer (belongs_to_topic stays None).
        (self.date_dir / "voice-as-interface.md").write_text(
            "---\nsurface: routine\n---\n\n# Voice\n\nbody.\n", encoding="utf-8"
        )
        self._readme(
            "| [voice-as-interface](voice-as-interface.md) | ok | TL;DR: x |\n"
        )
        with patch("sys.stderr", io.StringIO()):
            findings = watch_runner.extract_findings(self.date_dir, {})
        self.assertIsNone(findings[0].belongs_to_topic)


class TestActuatePhaseCrossReference(unittest.TestCase):
    """T-2523: the routing decision in actuate_phase.

    The Context caveat this class defends verbatim from the ticket: distinguish
    genuine duplicates (same event, same ask — a declared cross-ref) from the
    same event legitimately raising DIFFERENT asks in different topics (no
    declaration — both file as peers). A naive same-entity dedupe would destroy
    the second; only an EXPLICIT researcher declaration routes to the cross-ref
    path, so the un-declared different-asks shape is never touched.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "tickets" / "inbox").mkdir(parents=True)
        for sub in ("active", "done"):
            (self.tmp / "tickets" / sub).mkdir(parents=True)
        self.date_dir = self.tmp / "watch" / "reports" / "2026-08-03"
        self.date_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_with(self, findings, calls):
        def fake_finding(finding, policy, **kw):
            calls.append(("finding", finding.slug))
            return ("filed", "T-1000", "retries=0")

        def fake_xref(finding, primary, policy, **kw):
            calls.append(("xref", finding.slug, primary.slug))
            return ("filed", "T-1001", "retries=0")

        with patch("tools.watch_runner.REPO_ROOT", self.tmp), \
             patch("tools.watch_runner.REPORTS_ROOT", self.tmp / "watch" / "reports"), \
             patch("tools.watch_runner.extract_findings", return_value=findings), \
             patch("tools.watch_runner.file_finding", side_effect=fake_finding), \
             patch("tools.watch_runner.file_cross_reference", side_effect=fake_xref), \
             patch("tools.watch_runner._read_report_body", return_value=""), \
             patch("tools.watch_runner.log_actuation_summary"), \
             patch("tools.watch_runner.write_actuation_ledger"), \
             patch("tools.watch_runner.git_commit_ledger_entry", return_value=(True, "ok")):
            watch_runner.actuate_phase(date(2026, 8, 3), [], {})

    def _finding(self, slug, belongs=None):
        return Finding(
            slug=slug,
            tldr=f"{slug} tldr",
            report_relpath=f"knowledge/watch/reports/2026-08-03/{slug}.md",
            headline_excerpt="excerpt",
            belongs_to_topic=belongs,
        )

    def test_genuine_dup_files_cross_reference(self) -> None:
        # T-2488/T-2502 shape: echo-stack primary present + voice declares it.
        primary = self._finding("echo-stack-vendor-changes")
        secondary = self._finding(
            "voice-as-interface", belongs="echo-stack-vendor-changes"
        )
        calls: list = []
        self._run_with([primary, secondary], calls)
        self.assertIn(("finding", "echo-stack-vendor-changes"), calls)
        self.assertIn(
            ("xref", "voice-as-interface", "echo-stack-vendor-changes"), calls
        )
        # The secondary must NOT also have filed a peer finding.
        self.assertNotIn(("finding", "voice-as-interface"), calls)

    def test_different_asks_both_file_peers(self) -> None:
        # Same underlying event, but NO declaration → both are peers. This is
        # the shape a naive dedupe would wrongly collapse.
        a = self._finding("echo-stack-vendor-changes")
        b = self._finding("voice-as-interface")  # no belongs_to_topic
        calls: list = []
        self._run_with([a, b], calls)
        self.assertIn(("finding", "echo-stack-vendor-changes"), calls)
        self.assertIn(("finding", "voice-as-interface"), calls)
        self.assertFalse([c for c in calls if c[0] == "xref"])

    def test_declaration_with_absent_primary_files_peer(self) -> None:
        # Declares a topic that did NOT produce a finding this run (paused /
        # not-due / errored). We must NOT suppress against a primary that never
        # filed — the secondary files as a normal peer so the event is not lost.
        secondary = self._finding(
            "voice-as-interface", belongs="echo-stack-vendor-changes"
        )
        calls: list = []
        self._run_with([secondary], calls)  # primary absent from the run
        self.assertIn(("finding", "voice-as-interface"), calls)
        self.assertFalse([c for c in calls if c[0] == "xref"])


class TestTopicProbes(unittest.TestCase):
    """T-4543: a due topic with a change probe skips its researcher only when
    the probe sees no change inside the quiet window; every failure fails open.
    """

    TODAY = date(2026, 10, 6)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.reports_root = self.tmp / "reports"
        self._patches = [
            patch.object(watch_runner, "STATE_DIR", self.tmp / "state"),
            patch.object(watch_runner, "PROBE_STATE_FILE", self.tmp / "state" / "probes.json"),
            patch.object(watch_runner, "REPORTS_ROOT", self.reports_root),
            patch.object(watch_runner, "_append_runner_log"),
            patch.dict(watch_runner.TOPIC_PROBES, {
                "cvp": {"kind": "json_ld_date_modified", "url": "https://example.test/a",
                        "baseline": "B0", "max_quiet_days": 7},
            }, clear=True),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    @staticmethod
    def _html(value: str) -> str:
        return f'<script type="application/ld+json">{{"@type":"Article","dateModified":"{value}"}}</script>'

    def _state(self, days_ago: int | None) -> dict:
        topics = {}
        if days_ago is not None:
            last = date.fromordinal(self.TODAY.toordinal() - days_ago)
            topics["cvp"] = {"last_fire": last.isoformat(), "last_outcome": "ok"}
        return {"schema_version": 1, "topics": topics}

    def _skip(self, state: dict, fetch, today: date | None = None) -> tuple[bool, str]:
        return watch_runner.probe_skip("cvp", state, today or self.TODAY, fetch=fetch)

    def test_unchanged_inside_quiet_window_skips(self) -> None:
        skip, reason = self._skip(self._state(1), lambda url: self._html("B0"))
        self.assertTrue(skip)
        self.assertIn("unchanged", reason)

    def test_changed_value_dispatches(self) -> None:
        skip, reason = self._skip(self._state(1), lambda url: self._html("B1"))
        self.assertFalse(skip)
        self.assertIn("B0 -> B1", reason)

    def test_fetch_error_fails_open(self) -> None:
        def boom(url):
            raise OSError("network down")
        skip, reason = self._skip(self._state(1), boom)
        self.assertFalse(skip)
        self.assertIn("fail-open", reason)

    def test_missing_or_ambiguous_date_modified_fails_open(self) -> None:
        for page in ("<html>no metadata</html>", self._html("B0") + self._html("B9")):
            with self.subTest(page=page[:20]):
                watch_runner.PROBE_STATE_FILE.unlink(missing_ok=True)
                skip, reason = self._skip(self._state(1), lambda url, page=page: page)
                self.assertFalse(skip)
                self.assertIn("fail-open", reason)

    def test_quiet_ceiling_dispatches_even_when_unchanged(self) -> None:
        skip, reason = self._skip(self._state(7), lambda url: self._html("B0"))
        self.assertFalse(skip)
        self.assertIn("no full fire in 7d", reason)

    def test_never_fired_topic_dispatches(self) -> None:
        skip, _ = self._skip(self._state(None), lambda url: self._html("B0"))
        self.assertFalse(skip)

    def test_decision_is_cached_for_the_day(self) -> None:
        fetch = MagicMock(return_value=self._html("B0"))
        self.assertTrue(self._skip(self._state(1), fetch)[0])
        self.assertTrue(self._skip(self._state(1), fetch)[0])
        self.assertEqual(fetch.call_count, 1)
        # The next day probes afresh.
        next_day = date.fromordinal(self.TODAY.toordinal() + 1)
        self._skip(self._state(2), fetch, today=next_day)
        self.assertEqual(fetch.call_count, 2)

    def test_unprobed_topic_never_skips_and_writes_nothing(self) -> None:
        self.assertEqual(watch_runner.probe_skip("other", self._state(1), self.TODAY),
                         (False, "no probe"))
        self.assertFalse(watch_runner.PROBE_STATE_FILE.exists())

    def test_baseline_advances_only_after_an_observed_fire(self) -> None:
        self._skip(self._state(1), lambda url: self._html("B1"))
        watch_runner.advance_probe_baseline("cvp", self.TODAY)
        next_day = date.fromordinal(self.TODAY.toordinal() + 1)
        skip, _ = self._skip(self._state(1), lambda url: self._html("B1"), today=next_day)
        self.assertTrue(skip)

    def test_fail_open_fire_does_not_move_baseline(self) -> None:
        def boom(url):
            raise OSError("down")
        self._skip(self._state(1), boom)
        watch_runner.advance_probe_baseline("cvp", self.TODAY)
        self.assertEqual(watch_runner.load_probe_state()["cvp"]["baseline"], "B0")

    def _topics(self) -> list[Topic]:
        return [Topic(slug=s, cadence="daily", budget=150, scope="s", seed_sources=[], notes="")
                for s in ("cvp", "other")]

    def test_compute_due_briefs_drops_unchanged_probe_topic(self) -> None:
        state = {"schema_version": 1, "topics": {
            "cvp": {"last_fire": "2026-10-05", "last_outcome": "ok"},
            "other": {"last_fire": "2026-10-05", "last_outcome": "ok"},
        }}
        with patch.object(watch_runner, "parse_topics", return_value=self._topics()), \
             patch.object(watch_runner, "load_state", return_value=state), \
             patch.object(watch_runner, "_fetch_url", return_value=self._html("B0")) as fetch, \
             patch("sys.stderr", new_callable=io.StringIO):
            recs = watch_runner.compute_due_briefs(self.TODAY)
            self.assertEqual([r["slug"] for r in recs], ["other"])
            forced = watch_runner.compute_due_briefs(self.TODAY, force=True)
            self.assertEqual([r["slug"] for r in forced], ["cvp", "other"])
        self.assertEqual(fetch.call_count, 1)  # force never probes

    def test_land_reports_treats_probe_skip_as_no_fire(self) -> None:
        state = {"schema_version": 1, "topics": {
            "cvp": {"last_fire": "2026-10-05", "last_outcome": "ok"}}}
        topics = [Topic(slug="cvp", cadence="daily", budget=150, scope="s", seed_sources=[], notes="")]
        with patch.object(watch_runner, "parse_topics", return_value=topics), \
             patch.object(watch_runner, "load_state", return_value=state), \
             patch.object(watch_runner, "save_state") as save, \
             patch.object(watch_runner, "_fetch_url", return_value=self._html("B0")), \
             patch.object(watch_runner, "git_commit_and_push") as gcp, \
             patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = watch_runner.land_reports(self.TODAY)
        self.assertEqual(rc, 0)
        self.assertIn("skipped (probe", out.getvalue())
        gcp.assert_not_called()
        save.assert_not_called()
        self.assertEqual(state["topics"]["cvp"]["last_fire"], "2026-10-05")

    def test_registered_probe_matches_a_real_topic(self) -> None:
        # Guards the live registry (setUp swaps in a test registry).
        for p in reversed(self._patches[-1:]):
            p.stop()
        try:
            slugs = {t.slug for t in watch_runner.parse_topics(watch_runner.TOPICS_FILE)}
            for slug, spec in watch_runner.TOPIC_PROBES.items():
                self.assertIn(slug, slugs)
                self.assertEqual(spec["kind"], "json_ld_date_modified")
                self.assertTrue(spec["url"].startswith("https://"))
        finally:
            self._patches[-1].start()


# ---------------------------------------------------------------------------
# T-4877: spot-check (independent re-check of a sample of claims per fire)
# ---------------------------------------------------------------------------

_SPOT_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "watch_spot_check"
_SPOT_LINE = "spot_check: sampled=4 supported=2 refinement=1 over_broad=1 unsupported=0 verifier=explore\n"


def _spot_fixture_text(name: str, with_spot: bool = False) -> str:
    """Real-report bytes (copied from knowledge/watch/reports/) as a fixture."""
    text = (_SPOT_FIXTURES / name).read_text(encoding="utf-8")
    if with_spot:
        head, sep, rest = text.partition("\n---\n")
        text = head + "\n" + _SPOT_LINE.rstrip("\n") + sep + rest
    return text


class TestParseSpotCheck(unittest.TestCase):
    def test_well_formed(self) -> None:
        got = watch_runner.parse_spot_check({"spot_check": _SPOT_LINE.split(": ", 1)[1].strip()})
        self.assertEqual(got, {"sampled": 4, "supported": 2, "refinement": 1,
                               "over_broad": 1, "unsupported": 0, "verifier": "explore"})

    def test_empty_and_absent_are_none(self) -> None:
        self.assertIsNone(watch_runner.parse_spot_check({}))
        self.assertIsNone(watch_runner.parse_spot_check({"spot_check": ""}))
        self.assertIsNone(watch_runner.parse_spot_check(None))  # type: ignore[arg-type]

    def test_partial_is_none(self) -> None:
        self.assertIsNone(watch_runner.parse_spot_check(
            {"spot_check": "sampled=3 supported=3"}))

    def test_malformed_never_raises(self) -> None:
        for bad in ("garbage", "sampled=x supported=1 refinement=0 over_broad=0 unsupported=0",
                    "sampled=-1 supported=0 refinement=0 over_broad=0 unsupported=0",
                    "sampled=3 supported=1 refinement=0 over_broad=0 unsupported=0",  # sum mismatch
                    "=== ===", "sampled=1=2", 7, ["a"]):
            self.assertIsNone(watch_runner.parse_spot_check({"spot_check": bad}), bad)  # type: ignore[arg-type]

    def test_real_fixture_frontmatter_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "r.md"
            p.write_text(_spot_fixture_text("above_bar_with_followups.md", True), encoding="utf-8")
            got = watch_runner.parse_spot_check(watch_runner._parse_report_frontmatter(p))
        self.assertEqual(got["sampled"], 4)
        self.assertEqual(got["verifier"], "explore")


class TestSpotCheckPolicy(unittest.TestCase):
    def test_every_risk_tagged_key_names_an_existing_topic(self) -> None:
        topics = {t.slug for t in watch_runner.parse_topics(watch_runner.TOPICS_FILE)}
        tagged = {s for s, pol in watch_runner.TOPIC_POLICY.items() if "spot_check" in pol}
        self.assertTrue(tagged)
        self.assertEqual(tagged - topics, set())

    def test_exact_risk_tagged_set(self) -> None:
        tagged = {s for s, pol in watch_runner.TOPIC_POLICY.items()
                  if pol.get("spot_check") == "always"}
        self.assertEqual(tagged, {
            "anthropic-policy-and-billing", "mcp-supply-chain",
            "echo-stack-vendor-changes", "flipper-one-launch-watch",
            "anthropic-claude-code-changelog", "anthropic-quota-interface"})

    def test_build_brief_does_not_mention_spot_check(self) -> None:
        brief = watch_runner.build_brief(_topic("due-weekly"), Path("/tmp/out.md"))
        self.assertNotIn("spot_check", brief)
        self.assertNotIn("spot-check", brief)


class TestSpotCheckQualifies(unittest.TestCase):
    def _q(self, slug: str, fixture: str) -> bool:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / f"{slug}.md"
            p.write_text(_spot_fixture_text(fixture), encoding="utf-8")
            return watch_runner.spot_check_qualifies(slug, p)

    def test_above_bar_arm(self) -> None:
        self.assertTrue(self._q("some-other-topic", "above_bar_with_followups.md"))

    def test_above_bar_only_arm(self) -> None:
        # Real above-bar report with NO follow-ups and an untagged slug: only the
        # above-bar arm can qualify it (the other fixtures mask this arm).
        self.assertTrue(self._q("compositional-independence-vs-pantheon", "above_bar_only.md"))

    def test_followups_arm(self) -> None:
        self.assertTrue(self._q("voice-as-interface", "routine_with_followups.md"))

    def test_risk_tag_arm(self) -> None:
        self.assertTrue(self._q("flipper-one-launch-watch", "routine_risk_tagged.md"))

    def test_routine_without_followups_does_not_qualify(self) -> None:
        self.assertFalse(self._q("glop-it-blog", "routine_no_followups.md"))

    def test_cadence_alone_does_not_qualify(self) -> None:
        # a daily-cadence topic with no tag is still routine
        self.assertFalse(self._q("not-tagged-daily", "routine_no_followups.md"))

    def test_missing_report_is_false(self) -> None:
        self.assertFalse(watch_runner.spot_check_qualifies("x", Path("/nonexistent/x.md")))


class TestSpotCheckLand(_LandHarness, unittest.TestCase):
    """Reuses the shared land harness without re-running TestLandReports tests."""

    def _put(self, fixture: str, with_spot: bool) -> None:
        (self.date_dir / "alpha.md").write_text(
            _spot_fixture_text(fixture, with_spot), encoding="utf-8")

    def _land(self) -> tuple[int, str, str]:
        return self._land_capturing_stderr()

    def test_qualifying_without_spot_check_warns_logs_and_lands(self) -> None:
        self._put("above_bar_with_followups.md", False)
        # ensure readback present so only the spot-check WARN is under test
        rc, err, index = self._land()
        self.assertEqual(rc, 0)
        self.assertIn("WARN [spot_check]", err)
        self.assertIn("alpha.md", err)
        log = watch_runner.RUNNER_LOG.read_text(encoding="utf-8")
        self.assertIn("spot_check", log)
        self.assertIn("alpha", log)
        self.assertEqual(self._saved_state["topics"]["alpha"]["last_outcome"], "ok")

    def test_qualifying_with_spot_check_is_silent(self) -> None:
        self._put("above_bar_with_followups.md", True)
        _rc, err, _index = self._land()
        self.assertNotIn("WARN [spot_check]", err)

    def test_routine_is_silent(self) -> None:
        self._put("routine_no_followups.md", False)
        _rc, err, _index = self._land()
        self.assertNotIn("spot_check", err)

    def test_no_day_index_suffix_mark(self) -> None:
        self._put("above_bar_with_followups.md", False)
        _rc, _err, index = self._land()
        row = [ln for ln in index.splitlines() if ln.startswith("| [alpha]")][0]
        self.assertNotIn("spot", row.lower())

    def test_extract_findings_unchanged_by_spot_check(self) -> None:
        # Same day-index + report, with/without spot_check: identical titles.
        def findings(with_spot: bool):
            self._put("above_bar_with_followups.md", with_spot)
            (self.date_dir / "README.md").unlink(missing_ok=True)
            self._land()
            return [(f.slug, f.tldr) for f in
                    watch_runner.extract_findings(self.date_dir, {"alpha": "ok"})]
        a = findings(False)
        b = findings(True)
        self.assertEqual(a, b)
        self.assertNotIn("spot", a[0][1].lower())

    def test_readme_tally_line_present_with_data_absent_without(self) -> None:
        self._put("above_bar_with_followups.md", True)
        _rc, _err, index = self._land()
        self.assertIn("Spot-check (8 weeks): sampled=4 supported=2 refinement=1 over_broad=1 unsupported=0", index)
        # no data -> omitted
        (self.date_dir / "README.md").unlink()
        self._put("routine_no_followups.md", False)
        _rc, _err, index = self._land()
        self.assertNotIn("Spot-check", index)


class TestSpotCheckIndexLine(unittest.TestCase):
    _TALLY = {"samples": 4, "supported": 2, "refinement": 1, "over_broad": 1,
              "unsupported": 0, "weeks": 1}

    def _mk_index(self) -> tuple[tempfile.TemporaryDirectory, Path]:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        d = Path(td.name) / "2026-11-03"
        d.mkdir()
        watch_runner.append_day_index_entry(d, "alpha", "ok", "TL;DR: x")
        return td, d

    def test_rewrite_is_idempotent(self) -> None:
        _td, d = self._mk_index()
        watch_runner._write_spot_check_index_line(d, self._TALLY)
        once = (d / "README.md").read_bytes()
        for _ in range(4):
            watch_runner._write_spot_check_index_line(d, self._TALLY)
        self.assertEqual((d / "README.md").read_bytes(), once)
        self.assertIn(b"Spot-check (8 weeks)", once)

    def test_data_to_no_data_restores_header_exactly(self) -> None:
        _td, d = self._mk_index()
        before = (d / "README.md").read_bytes()
        watch_runner._write_spot_check_index_line(d, self._TALLY)
        self.assertNotEqual((d / "README.md").read_bytes(), before)
        watch_runner._write_spot_check_index_line(d, None)
        self.assertEqual((d / "README.md").read_bytes(), before)

    def test_row_appended_after_tally_stays_last(self) -> None:
        _td, d = self._mk_index()
        watch_runner._write_spot_check_index_line(d, self._TALLY)
        watch_runner.append_day_index_entry(d, "beta", "ok", "TL;DR: y")
        self.assertTrue((d / "README.md").read_text().rstrip().endswith("TL;DR: y |"))


class TestSpotCheckTally(unittest.TestCase):
    def _mk(self, root: Path, day: str, n: int, sup: int, slug: str = "t") -> None:
        d = root / day
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{slug}.md").write_text(
            f"---\nsurface: routine\nspot_check: sampled={n} supported={sup} "
            f"refinement={n - sup} over_broad=0 unsupported=0 verifier=v\n---\n\n# x\n",
            encoding="utf-8")

    def test_no_data_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(watch_runner.spot_check_tally(Path(td), date(2026, 11, 3)))

    def test_sums_and_weeks_and_window(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            r = Path(td)
            self._mk(r, "2026-11-03", 4, 3)
            self._mk(r, "2026-10-27", 3, 3)
            self._mk(r, "2026-08-01", 5, 0)  # outside 8 weeks
            (r / "2026-11-02").mkdir()
            (r / "2026-11-02" / "bad.md").write_text("---\nspot_check: junk\n---\n", encoding="utf-8")
            t = watch_runner.spot_check_tally(r, date(2026, 11, 3))
        self.assertEqual((t["samples"], t["supported"], t["weeks"]), (7, 6, 2))

    def test_lines_the_same_in_digest_module_threshold_inputs(self) -> None:
        # tally keys consumed by watch_digest.spot_check_alert
        with tempfile.TemporaryDirectory() as td:
            self._mk(Path(td), "2026-11-03", 2, 1)
            t = watch_runner.spot_check_tally(Path(td), date(2026, 11, 3))
        for k in ("samples", "supported", "refinement", "over_broad", "unsupported", "weeks"):
            self.assertIn(k, t)


if __name__ == "__main__":
    unittest.main()
