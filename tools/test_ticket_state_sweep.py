"""Tests for tools/ticket_state_sweep.py.

Per T-0149 §Brief-named tests (10 verbatim) + supporting tests for
HERMES self-cleanliness + regex/parsing examples.

Fixture pattern: real synthetic git repos via tempfile + `_make_origin_and_clone`
helper for tests 5/8/9/10 (auto-fix path tests). In-memory dicts for
non-git scans (1/2/3/4/6/7).

Lift `_make_origin_and_clone` shape from `tools/test_pre_push_hook.py`
lines 104-133 — adapted for pantheon-config-tickets-shape repo (creates
tickets/{inbox,active,done}/ + ledger/2026.jsonl).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools._test_fixtures import copy_tools_import_closure  # noqa: E402
from tools.ticket_state_sweep import (  # noqa: E402
    OPERATOR_MOOT_RECENCY_DAYS,
    ForwardMismatch,
    SweepReport,
    _auto_fix_forward_mismatch,
    _extract_ticket_id_from_action,
    _is_resolution_action,
    _load_ledger_entries,
    _scan_forward,
    _scan_reverse,
    _scan_target_operator,
    main,
    sweep,
)


# ---------------------------------------------------------------------------
# Helpers — synthetic git repo fixtures
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path, check: bool = False) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


def _make_pantheon_repo(tmpdir: Path) -> Path:
    """Create a synthetic pantheon-config-shape repo (clone with origin).

    Returns the clone path. Repo has tickets/{inbox,active,done}/
    + ledger/ + tools/ticket.py (real one symlinked from REPO_ROOT for
    the integration-seam test).
    """
    origin = tmpdir / "origin.git"
    origin.mkdir()
    _git("init", "--bare", "-b", "main", str(origin), cwd=tmpdir, check=True)

    # Build the staging repo with the directory shape, then push to origin.
    staging = tmpdir / "staging"
    staging.mkdir()
    _git("init", "-b", "main", cwd=staging, check=True)
    _git("remote", "add", "origin", str(origin), cwd=staging, check=True)

    for sub in ("inbox", "active", "done"):
        (staging / "tickets" / sub).mkdir(parents=True, exist_ok=True)
        (staging / "tickets" / sub / ".gitkeep").write_text("", encoding="utf-8")
    # Minimal tickets/README.md with the "## Current state" marker that
    # tools/ticket.py expects when rebuilding the index table.
    (staging / "tickets" / "README.md").write_text(
        "# Tickets — synthetic test fixture\n\n## Current state\n\n",
        encoding="utf-8",
    )
    (staging / "ledger").mkdir(exist_ok=True)
    (staging / "ledger" / "2026.jsonl").write_text("", encoding="utf-8")
    (staging / "tools").mkdir(exist_ok=True)
    (staging / "tools" / ".gitkeep").write_text("", encoding="utf-8")

    _git("add", "-A", cwd=staging, check=True)
    _git("commit", "-m", "initial pantheon-shape", cwd=staging, check=True)
    _git("push", "-u", "origin", "main", cwd=staging, check=True)

    clone = tmpdir / "local"
    subprocess.run(
        ["git", "clone", str(origin), str(clone)],
        capture_output=True,
        text=True,
        check=True,
    )
    # Keep real ticket.py subprocess commits hermetic across worker accounts;
    # they must not depend on an ambient global Git identity.
    _git("config", "user.name", "Test", cwd=clone, check=True)
    _git("config", "user.email", "test@example.com", cwd=clone, check=True)
    return clone


def _write_ticket(
    clone: Path, lane: str, ticket_id: str, body: str = "# Test ticket\n"
) -> Path:
    """Write a ticket .md with proper front-matter + body sections."""
    front = (
        f"---\n"
        f"id: {ticket_id}\n"
        f"created: 2026-05-01T00:00:00Z\n"
        f"requester: orchestrator\n"
        f"target: orchestrator\n"
        f"state: {lane}\n"
        f"parent_ticket: null\n"
        f"depends_on: []\n"
        f"labels: [test]\n"
        f"stale_after: 14d\n"
        f"---\n\n"
    )
    transition = "## Transition log\n\n- 2026-05-01T00:00:00Z [test] filed in inbox\n"
    path = clone / "tickets" / lane / f"{ticket_id}.md"
    path.write_text(front + body + "\n## Ask\n\nTest.\n\n" + transition, encoding="utf-8")
    return path


def _write_ledger_entry(
    clone: Path,
    entry_id: str,
    action: str,
    rationale: str = "test",
    ts: str | None = None,
) -> None:
    """Append a ledger entry to ledger/2026.jsonl."""
    if ts is None:
        ts = "2026-05-09T15:10:00Z"
    entry = {
        "id": entry_id,
        "ts": ts,
        "agent": "orchestrator",
        "action": action,
        "rationale": rationale,
        "justifies_via": ["P4"],
        "links": [],
    }
    ledger_path = clone / "ledger" / "2026.jsonl"
    with ledger_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Brief-named tests (10 verbatim)
# ---------------------------------------------------------------------------


class TestBriefNamedSweep(unittest.TestCase):
    """Verbatim §Brief-named tests from T-0149 + b33 ratified plan."""

    def test_ticket_state_sweep_forward_matches_ledger_resolved_action_to_inbox_ticket(self):
        """In-memory: ledger says T-0141 resolved; ticket file in inbox."""
        tickets = {
            "T-0141": {
                "lane": "inbox",
                "target": "orchestrator",
                "depends_on": [],
            }
        }
        ledger = [
            {
                "id": "2026-05-09T15:10:00Z-001",
                "ts": "2026-05-09T15:10:00Z",
                "agent": "orchestrator",
                "action": "t0141_resolved_spine_index_groom_landed",
                "rationale": "test",
            }
        ]
        mismatches, ambiguous = _scan_forward(tickets, ledger)
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0].ticket_id, "T-0141")
        self.assertEqual(mismatches[0].current_lane, "inbox")
        self.assertEqual(mismatches[0].ledger_entry_id, "2026-05-09T15:10:00Z-001")
        self.assertEqual(ambiguous, [])

    def test_ticket_state_sweep_reverse_finds_done_ticket_with_no_resolution_ledger_entry(self):
        """In-memory: T-0099 in done; no ledger entry references it."""
        tickets = {
            "T-0099": {"lane": "done", "target": "orchestrator", "depends_on": []}
        }
        ledger = [
            {
                "id": "2026-05-08T00:00:00Z-001",
                "ts": "2026-05-08T00:00:00Z",
                "agent": "orchestrator",
                "action": "some_other_action",
                "rationale": "unrelated",
            }
        ]
        gaps = _scan_reverse(tickets, ledger)
        self.assertEqual(gaps, ["T-0099"])

    def test_ticket_state_sweep_operator_target_moot_detects_operator_ticket_with_recent_dispositional_ledger_entry(self):
        """In-memory: target=operator ticket in inbox; recent ledger entry references it."""
        tickets = {
            "T-0050": {
                "lane": "inbox",
                "target": "operator",
                "depends_on": [],
            }
        }
        # Recent entry (within 30d window) referencing T-0050
        recent_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat().replace("+00:00", "Z")
        ledger = [
            {
                "id": "recent-disposition-001",
                "ts": recent_ts,
                "agent": "orchestrator",
                "action": "ratify_t0050_per_operator_msg",
                "rationale": "test",
                "links": [],
            }
        ]
        candidates = _scan_target_operator(tickets, ledger)
        self.assertEqual(candidates, ["T-0050"])

    def test_ticket_state_sweep_forward_unambiguous_match_auto_resolves_via_ticket_py_resolve(self):
        """Real synthetic repo: T-0099 in inbox + matching ledger; sweep auto-resolves."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            clone = _make_pantheon_repo(tmp)
            _write_ticket(clone, "inbox", "T-0099")
            _write_ledger_entry(
                clone,
                entry_id="2026-05-09T00:00:00Z-001",
                action="t0099_resolved_test",
            )
            _git("add", "-A", cwd=clone, check=True)
            _git("commit", "-m", "fixture seed", cwd=clone, check=True)

            captured_calls: list[list[str]] = []

            def fake_run(args, **kwargs):
                captured_calls.append(args if isinstance(args, list) else list(args))
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="", stderr=""
                )

            with patch("tools.ticket_state_sweep.subprocess.run", side_effect=fake_run):
                report, ambiguous, error_count = sweep(clone, dry_run=False)

            self.assertEqual(error_count, 0)
            self.assertEqual(len(report.forward_mismatches), 1)
            self.assertEqual(report.forward_mismatches[0].ticket_id, "T-0099")
            # First call: claim T-0099
            self.assertIn("claim", captured_calls[0])
            self.assertIn("T-0099", captured_calls[0])
            # Second call: resolve T-0099 with [sweep] rationale
            self.assertIn("resolve", captured_calls[1])
            self.assertIn("T-0099", captured_calls[1])
            self.assertTrue(
                any("[sweep] sync" in arg for arg in captured_calls[1])
            )

    def test_ticket_state_sweep_forward_ambiguous_multiple_ledger_entries_surfaces_to_stderr_no_autoresolve(self):
        """Two ledger entries both with resolution actions for T-0099."""
        tickets = {
            "T-0099": {
                "lane": "inbox",
                "target": "orchestrator",
                "depends_on": [],
            }
        }
        ledger = [
            {
                "id": "entry-1",
                "ts": "2026-05-08T00:00:00Z",
                "agent": "orchestrator",
                "action": "t0099_resolved_first_attempt",
                "rationale": "test",
            },
            {
                "id": "entry-2",
                "ts": "2026-05-09T00:00:00Z",
                "agent": "orchestrator",
                "action": "t0099_resolved_second_attempt",
                "rationale": "test",
            },
        ]
        mismatches, ambiguous_messages = _scan_forward(tickets, ledger)
        self.assertEqual(mismatches, [])  # No auto-fix attempted
        self.assertEqual(len(ambiguous_messages), 1)
        self.assertIn("T-0099", ambiguous_messages[0])
        self.assertIn("multiple", ambiguous_messages[0].lower())

    def test_ticket_state_sweep_reverse_finding_emits_warning_does_not_author_ledger_entry(self):
        """Real synthetic repo: ticket in done/ with no ledger ref; sweep does NOT mutate ledger."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            clone = _make_pantheon_repo(tmp)
            _write_ticket(clone, "done", "T-0500")
            _git("add", "-A", cwd=clone, check=True)
            _git("commit", "-m", "fixture seed", cwd=clone, check=True)

            ledger_path = clone / "ledger" / "2026.jsonl"
            byte_len_before = ledger_path.stat().st_size

            report, ambiguous, error_count = sweep(clone, dry_run=False)

            byte_len_after = ledger_path.stat().st_size
            self.assertEqual(byte_len_before, byte_len_after)  # Ledger untouched
            self.assertIn("T-0500", report.reverse_gaps)
            self.assertEqual(error_count, 0)

    def test_ticket_state_sweep_idempotent_on_clean_state_no_diff_no_commit(self):
        """Clean state: no resolved-via-ledger drift, no moot.

        Run sweep twice; HEAD-sha unchanged after both runs; report empty.
        """
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            clone = _make_pantheon_repo(tmp)
            # Clean state: ticket in done with matching ledger entry
            _write_ticket(clone, "done", "T-0700")
            _write_ledger_entry(
                clone, entry_id="entry-clean-001",
                action="t0700_resolved_clean",
            )
            _git("add", "-A", cwd=clone, check=True)
            _git("commit", "-m", "fixture seed", cwd=clone, check=True)

            sha_before = _git("rev-parse", "HEAD", cwd=clone).stdout.strip()

            report1, amb1, err1 = sweep(clone, dry_run=True)
            report2, amb2, err2 = sweep(clone, dry_run=True)

            sha_after = _git("rev-parse", "HEAD", cwd=clone).stdout.strip()
            self.assertEqual(sha_before, sha_after)  # No commits from sweep
            # Both reports empty across all three sweeps
            for r in (report1, report2):
                self.assertEqual(r.forward_mismatches, [])
                self.assertEqual(r.reverse_gaps, [])
                self.assertEqual(r.target_operator_candidates, [])
            self.assertEqual(amb1, [])
            self.assertEqual(amb2, [])

    def test_ticket_state_sweep_fails_open_on_transient_git_error_continues_processing(self):
        """Transient subprocess error on T-0001 does not halt T-0002's auto-fix attempt."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            clone = _make_pantheon_repo(tmp)
            _write_ticket(clone, "inbox", "T-0001")
            _write_ticket(clone, "inbox", "T-0002")
            _write_ledger_entry(
                clone, entry_id="entry-1", action="t0001_resolved_a"
            )
            _write_ledger_entry(
                clone, entry_id="entry-2", action="t0002_resolved_b"
            )
            _git("add", "-A", cwd=clone, check=True)
            _git("commit", "-m", "fixture seed", cwd=clone, check=True)

            call_log: list[list[str]] = []

            def flaky_run(args, **kwargs):
                args_list = args if isinstance(args, list) else list(args)
                call_log.append(args_list)
                if "T-0001" in args_list:
                    raise OSError("transient error simulation")
                return subprocess.CompletedProcess(
                    args=args_list, returncode=0, stdout="", stderr=""
                )

            stderr_buf = io.StringIO()
            with redirect_stderr(stderr_buf):
                with patch(
                    "tools.ticket_state_sweep.subprocess.run", side_effect=flaky_run
                ):
                    report, ambiguous, error_count = sweep(clone, dry_run=False)

            # T-0001 logged to stderr
            self.assertIn("T-0001", stderr_buf.getvalue())
            # T-0002's auto-fix subprocess WAS attempted
            t0002_calls = [c for c in call_log if "T-0002" in c]
            self.assertGreater(len(t0002_calls), 0)
            # Error count reflects T-0001 failure
            self.assertEqual(error_count, 1)
            # T-0002 succeeded (in fixed list)
            fixed_ids = [m.ticket_id for m in report.forward_mismatches]
            self.assertIn("T-0002", fixed_ids)

    def test_ticket_state_sweep_end_to_end_against_synthetic_repo_with_t0141_drift_case(self):
        """Full integration-seam: real ticket.py subprocess invoked.

        Synthetic pantheon-config-shape repo with T-0141 in inbox/ + matching
        ledger entry. Run sweep with REAL ticket.py (NOT mocked). Verify
        T-0141 moved to done/ + transition log has claim+resolve entries.
        """
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            clone = _make_pantheon_repo(tmp)
            # Copy real ticket.py + _git_utils.py + agent_role_map.py +
            # any modules ticket.py imports into the synthetic repo's
            # tools/ dir so subprocess invocation works.
            real_tools = REPO_ROOT / "tools"
            # resolve lazily imports ticket_lint, so make it a second entry
            # point; each entry's import-time dependencies are derived.
            copy_tools_import_closure(
                clone / "tools",
                source_tools=real_tools,
                entry_modules=("ticket.py", "ticket_lint.py"),
            )

            _write_ticket(clone, "inbox", "T-0141")
            _write_ledger_entry(
                clone,
                entry_id="2026-05-09T15:10:00Z-001",
                action="t0141_resolved_spine_index_groom_landed",
            )
            _git("add", "-A", cwd=clone, check=True)
            _git("commit", "-m", "fixture seed", cwd=clone, check=True)

            report, ambiguous, error_count = sweep(clone, dry_run=False)

            # Verify T-0141 moved to done/
            inbox_path = clone / "tickets" / "inbox" / "T-0141.md"
            done_path = clone / "tickets" / "done" / "T-0141.md"
            self.assertFalse(inbox_path.exists(),
                             "T-0141 should no longer be in inbox/")
            self.assertTrue(done_path.exists(),
                            "T-0141 should be in done/")

            # Verify transition log has claim + resolve entries
            content = done_path.read_text(encoding="utf-8")
            self.assertIn("claimed", content.lower())
            self.assertIn("resolved", content.lower())
            self.assertIn("[sweep] sync", content)


# ---------------------------------------------------------------------------
# Supporting tests
# ---------------------------------------------------------------------------


class TestLoadLedgerEntriesUnion(unittest.TestCase):
    """T-0148: _load_ledger_entries must read both layouts via the union."""

    def _entry(self, eid: str) -> str:
        return json.dumps({
            "id": eid, "ts": eid, "agent": "recovery",
            "action": "x", "justifies_via": ["P5"],
        })

    def test_picks_up_subfolder_shards_post_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ledger = repo / "ledger"
            for seg in ("2026-W18", "2026-W19"):
                (ledger / seg).mkdir(parents=True)
                (ledger / seg / f"{seg}.jsonl").write_text(
                    self._entry(f"2026-{seg}") + "\n", encoding="utf-8"
                )
            entries = _load_ledger_entries(repo)
        ids = {e["id"] for e in entries}
        self.assertEqual(ids, {"2026-2026-W18", "2026-2026-W19"})

    def test_pre_migration_flat_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "ledger").mkdir()
            (repo / "ledger" / "2026.jsonl").write_text(
                self._entry("flat-1") + "\n", encoding="utf-8"
            )
            entries = _load_ledger_entries(repo)
        self.assertEqual([e["id"] for e in entries], ["flat-1"])

    def test_archive_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ledger = repo / "ledger"
            (ledger / "2026-W18").mkdir(parents=True)
            (ledger / "2026-W18" / "2026-W18.jsonl").write_text(
                self._entry("shard-1") + "\n", encoding="utf-8"
            )
            (ledger / "2026.jsonl.archive-pre-d10-shard").write_text(
                self._entry("archived-1") + "\n", encoding="utf-8"
            )
            entries = _load_ledger_entries(repo)
        self.assertEqual([e["id"] for e in entries], ["shard-1"])


class TestResolutionRegex(unittest.TestCase):
    """Verify _is_resolution_action against locked-answer-10 examples."""

    def test_match_examples(self):
        for action in (
            "t0141_resolved_spine_index_groom_landed",
            "complete_t0054_bridge_hermes_guards",
            "t0102_phase1_resolved_something",
            "resolve_t0050_something",
            "t0050_complete_something",
        ):
            self.assertTrue(
                _is_resolution_action(action),
                f"should match: {action!r}",
            )

    def test_non_match_examples(self):
        for action in (
            "dispatch_t0050_to_team",
            "ledger_init_2026",
            "post_cap_hit_resume",
            "t0050_dispatched_to_team",
        ):
            self.assertFalse(
                _is_resolution_action(action),
                f"should NOT match: {action!r}",
            )


class TestExtractTicketId(unittest.TestCase):
    """Verify _extract_ticket_id_from_action against locked-answer-11 examples."""

    def test_single_id_examples(self):
        cases = [
            ("t0141_resolved_something", "T-0141"),
            ("complete_t0054_bridge", "T-0054"),
            ("T0050_complete_capslocked", "T-0050"),
        ]
        for action, expected in cases:
            tid, ambiguous = _extract_ticket_id_from_action(action)
            self.assertEqual(tid, expected, f"action={action!r}")
            self.assertFalse(ambiguous, f"action={action!r}")

    def test_ambiguous_multiple_distinct_ids(self):
        tid, ambiguous = _extract_ticket_id_from_action(
            "t0141_resolved_and_t0142_also"
        )
        self.assertIsNone(tid)
        self.assertTrue(ambiguous)

    def test_no_id_in_action(self):
        tid, ambiguous = _extract_ticket_id_from_action("ledger_init_2026")
        self.assertIsNone(tid)
        self.assertFalse(ambiguous)


class TestSweepSourceClean(unittest.TestCase):
    """HERMES self-cleanliness — ensure the sweep tool's source bytes don't contain
    the trigger byte sequence (uppercase, contiguous, no escape).

    Source-time concatenation: the trigger pattern is built at runtime
    so the literal bytes never appear in this test file's source.
    """

    def test_sweep_module_source_clean(self):
        trigger = (b"HERME" + b"S.md")  # source-time concat; no contiguous bytes here
        src_path = REPO_ROOT / "tools" / "ticket_state_sweep.py"
        self.assertNotIn(trigger, src_path.read_bytes())

    def test_test_module_source_clean(self):
        trigger = (b"HERME" + b"S.md")
        src_path = REPO_ROOT / "tools" / "test_ticket_state_sweep.py"
        self.assertNotIn(trigger, src_path.read_bytes())


class TestMainCLI(unittest.TestCase):
    """CLI smoke: --dry-run flag exists; prints summary; exits 0 on clean."""

    def test_dry_run_flag_no_op_on_clean_state(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            clone = _make_pantheon_repo(tmp)
            # Patch REPO_ROOT to point at clone for this invocation
            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                with patch("tools.ticket_state_sweep.REPO_ROOT", clone):
                    rc = main(["--dry-run"])
            self.assertEqual(rc, 0)
            self.assertIn("ticket_state_sweep summary", stdout_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
