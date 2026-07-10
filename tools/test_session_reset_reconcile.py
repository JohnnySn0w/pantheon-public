"""Unit tests for tools/session_reset_reconcile.py — T-0257."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.session_reset_reconcile as srr
from tools.session_reset_reconcile import (
    build_ledger_entry,
    find_unattributed_restarts,
    journal_started_events,
    load_watermark,
    main,
    save_watermark,
    _entry_id_for,
    _rationale_for,
)


# ---------------------------------------------------------------------------
# Ledger entry construction
# ---------------------------------------------------------------------------


class TestBuildLedgerEntry(unittest.TestCase):
    def test_basic_shape(self):
        src = {
            "ts": 1000,
            "ts_iso": "2026-05-12T12:00:00Z",
            "class": "cap_hit_auto_resume",
            "trigger": "ho_limit_hit_state_consumed",
            "agent_account": "claude",
            "session_id": "sess-abc",
            "resets_at": 12345,
        }
        le = build_ledger_entry(src)
        self.assertEqual(le["action"], "session_reset")
        self.assertEqual(le["agent"], "recovery")
        self.assertEqual(le["ts"], "2026-05-12T12:00:00Z")
        self.assertEqual(le["justifies_via"], ["D1"])
        self.assertIsNone(le["cost_usd"])
        self.assertIsNone(le["parent_id"])
        self.assertIsNone(le["supersedes"])
        # ID format
        self.assertTrue(le["id"].startswith("2026-05-12T12:00:00Z-session-reset-cap-hit-auto-resume-"))
        # Links
        self.assertEqual(le["links"][0]["type"], "session_reset_log_entry")
        self.assertEqual(le["links"][0]["ref"], "cap_hit_auto_resume:2026-05-12T12:00:00Z")

    def test_rationale_includes_extras(self):
        src = {
            "ts": 1000,
            "ts_iso": "2026-05-12T12:00:00Z",
            "class": "handoff_restart",
            "trigger": "operator_initiated",
            "agent_account": "claude",
            "service": "claude-telegram.service",
            "target_pane": "claude-telegram-bot:0",
        }
        le = build_ledger_entry(src)
        self.assertIn("class=handoff_restart", le["rationale"])
        self.assertIn("trigger=operator_initiated", le["rationale"])
        self.assertIn("service=claude-telegram.service", le["rationale"])
        self.assertIn("target_pane=claude-telegram-bot:0", le["rationale"])

    def test_entry_id_is_deterministic(self):
        src = {
            "ts_iso": "2026-05-12T12:00:00Z",
            "class": "cap_hit_auto_resume",
            "trigger": "x",
            "agent_account": "claude",
        }
        self.assertEqual(_entry_id_for(src), _entry_id_for(src))

    def test_entry_id_differs_by_class(self):
        common = {"ts_iso": "2026-05-12T12:00:00Z", "trigger": "x", "agent_account": "claude"}
        a = _entry_id_for({**common, "class": "cap_hit_auto_resume"})
        b = _entry_id_for({**common, "class": "handoff_restart"})
        self.assertNotEqual(a, b)


# ---------------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------------


class TestAppendToLedgerRouting(unittest.TestCase):
    """T-0148 Phase 2: append_to_ledger routes through current_write_target."""

    def test_legacy_present_writes_to_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_dir = Path(tmp) / "ledger"
            ledger_dir.mkdir()
            legacy = ledger_dir / "2026.jsonl"
            legacy.write_text("")  # legacy exists → must append here
            entry = {"id": "x", "ts": "2026-05-29T21:00:00Z"}
            written = srr.append_to_ledger([entry], ledger_dir=ledger_dir)
            self.assertEqual(written, legacy)
            self.assertEqual(len(legacy.read_text().strip().splitlines()), 1)

    def test_legacy_absent_writes_to_week_shard_and_mkdirs(self):
        from tools.ledger_shard import current_write_target
        with tempfile.TemporaryDirectory() as tmp:
            ledger_dir = Path(tmp) / "ledger"
            ledger_dir.mkdir()
            entry = {"id": "x", "ts": "2026-05-29T21:00:00Z"}
            written = srr.append_to_ledger([entry], ledger_dir=ledger_dir)
            expected = current_write_target(ledger_dir, datetime.now(timezone.utc))
            self.assertEqual(written, expected)
            self.assertTrue(written.parent.is_dir())  # ensure_segment_dir ran
            self.assertTrue(written.exists())


class TestWatermark(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.wm_path = self.tmp_path / ".reconcile_watermark.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_load_missing_returns_zeros(self):
        wm = load_watermark(path=self.wm_path)
        for c in ("cap_hit_auto_resume", "handoff_restart", "rate_limit_retry_inject", "service_restart_unattributed"):
            self.assertEqual(wm[c], 0)

    def test_save_then_load_round_trips(self):
        save_watermark({"cap_hit_auto_resume": 1000, "handoff_restart": 2000,
                        "rate_limit_retry_inject": 0, "service_restart_unattributed": 0},
                       path=self.wm_path)
        wm = load_watermark(path=self.wm_path)
        self.assertEqual(wm["cap_hit_auto_resume"], 1000)
        self.assertEqual(wm["handoff_restart"], 2000)

    def test_load_malformed_returns_zeros(self):
        self.wm_path.write_text("not json")
        wm = load_watermark(path=self.wm_path)
        self.assertEqual(wm["cap_hit_auto_resume"], 0)

    def test_save_preserves_class_subset(self):
        """Even if save is called with a partial dict, all classes appear in saved file."""
        save_watermark({"cap_hit_auto_resume": 5}, path=self.wm_path)
        data = json.loads(self.wm_path.read_text())
        self.assertEqual(data["last_consumed_ts_by_class"]["handoff_restart"], 0)


# ---------------------------------------------------------------------------
# Journal sweep
# ---------------------------------------------------------------------------


class TestJournalStartedEvents(unittest.TestCase):
    def test_parses_iso_lines(self):
        sample = (
            "2026-05-11T13:30:00+0000 host systemd[123]: Started Claude Telegram bot\n"
            "2026-05-11T18:00:00+0000 host systemd[123]: Started Claude Telegram bot\n"
        )
        with patch("tools.session_reset_reconcile.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout=sample, stderr="")
            out = journal_started_events()
        self.assertEqual(len(out), 2)
        # Both timestamps decoded correctly to epoch.
        from datetime import datetime, timezone as _tz
        expected_0 = int(datetime(2026, 5, 11, 13, 30, 0, tzinfo=_tz.utc).timestamp())
        self.assertEqual(out[0], expected_0)

    def test_returns_empty_on_subprocess_failure(self):
        with patch("tools.session_reset_reconcile.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="", stderr="denied")
            out = journal_started_events()
        self.assertEqual(out, [])

    def test_returns_empty_on_timeout(self):
        with patch("tools.session_reset_reconcile.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(cmd="journalctl", timeout=60)
            out = journal_started_events()
        self.assertEqual(out, [])

    def test_skips_unparseable_lines(self):
        sample = (
            "this line has no timestamp\n"
            "2026-05-11T13:30:00+0000 host systemd[123]: Started X\n"
        )
        with patch("tools.session_reset_reconcile.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout=sample, stderr="")
            out = journal_started_events()
        self.assertEqual(len(out), 1)


class TestFindUnattributedRestarts(unittest.TestCase):
    def test_match_within_tolerance_skips(self):
        producer_entries = [
            {"class": "cap_hit_auto_resume", "ts": 1000},
        ]
        # 5s after producer entry → within ±10s tolerance → attributed.
        result = find_unattributed_restarts([1005], producer_entries)
        self.assertEqual(result, [])

    def test_no_match_returns_unattributed(self):
        producer_entries = [
            {"class": "cap_hit_auto_resume", "ts": 1000},
        ]
        # 100s gap → outside tolerance → unattributed.
        result = find_unattributed_restarts([1100], producer_entries)
        self.assertEqual(result, [1100])

    def test_rate_limit_retry_inject_does_not_attribute(self):
        # rate_limit_retry_inject is NOT a session reset, just a retry-inject —
        # so it MUST NOT attribute a Started event. Only cap_hit + handoff do.
        producer_entries = [
            {"class": "rate_limit_retry_inject", "ts": 1000},
        ]
        result = find_unattributed_restarts([1005], producer_entries)
        self.assertEqual(result, [1005])

    def test_mixed_attributed_and_unattributed(self):
        producer_entries = [
            {"class": "cap_hit_auto_resume", "ts": 1000},
            {"class": "handoff_restart", "ts": 2000},
        ]
        # 1005 attributed by cap-hit; 2003 attributed by handoff; 3000 unattributed.
        result = find_unattributed_restarts([1005, 2003, 3000], producer_entries)
        self.assertEqual(result, [3000])


# ---------------------------------------------------------------------------
# Main flow — dry run
# ---------------------------------------------------------------------------


class TestMainDryRun(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        os.environ["PANTHEON_STATE_DIR"] = str(self.tmp_path)

    def tearDown(self) -> None:
        os.environ.pop("PANTHEON_STATE_DIR", None)
        self.tmp.cleanup()

    def test_dry_run_with_no_entries_bails_clean(self):
        with patch.object(srr, "pull_repo_or_warn"), \
             patch.object(srr, "read_events_cross_account", return_value=[]), \
             patch.object(srr, "journal_started_events", return_value=[]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(["--dry-run", "--no-journal-sweep"])  # default-safe via --dry-run
        self.assertEqual(rc, 0)
        self.assertIn("no new entries", buf.getvalue())

    def test_dry_run_with_entries_emits_to_stdout(self):
        sample = [{
            "ts": 1000,
            "ts_iso": "2026-05-12T12:00:00Z",
            "class": "cap_hit_auto_resume",
            "trigger": "x",
            "agent_account": "claude",
        }]
        with patch.object(srr, "pull_repo_or_warn"), \
             patch.object(srr, "read_events_cross_account", return_value=sample), \
             patch.object(srr, "journal_started_events", return_value=[]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(["--dry-run", "--no-journal-sweep"])  # default-safe via --dry-run
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("would append", out)
        self.assertIn("session_reset", out)

    def test_dry_run_does_not_write_watermark_or_ledger(self):
        sample = [{
            "ts": 1000, "ts_iso": "2026-05-12T12:00:00Z",
            "class": "cap_hit_auto_resume", "trigger": "x", "agent_account": "claude",
        }]
        with patch.object(srr, "pull_repo_or_warn"), \
             patch.object(srr, "read_events_cross_account", return_value=sample), \
             patch.object(srr, "journal_started_events", return_value=[]):
            with redirect_stdout(io.StringIO()):
                main(["--dry-run", "--no-journal-sweep"])  # default-safe via --dry-run
        # Watermark NOT touched.
        wm_path = self.tmp_path / "session_resets" / ".reconcile_watermark.json"
        self.assertFalse(wm_path.exists())


# ---------------------------------------------------------------------------
# Main flow — watermark dedup
# ---------------------------------------------------------------------------


class TestMainWatermark(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        os.environ["PANTHEON_STATE_DIR"] = str(self.tmp_path)

    def tearDown(self) -> None:
        os.environ.pop("PANTHEON_STATE_DIR", None)
        self.tmp.cleanup()

    def test_entries_below_watermark_are_skipped(self):
        # Seed a watermark: cap-hit ≥1000 already consumed.
        (self.tmp_path / "session_resets").mkdir(parents=True)
        wm_path = self.tmp_path / "session_resets" / ".reconcile_watermark.json"
        wm_path.write_text(json.dumps({
            "last_consumed_ts_by_class": {
                "cap_hit_auto_resume": 1000,
                "handoff_restart": 0,
                "rate_limit_retry_inject": 0,
                "service_restart_unattributed": 0,
            }
        }))
        sample = [
            # Below watermark — skipped.
            {"ts": 900, "ts_iso": "2026-05-12T11:00:00Z",
             "class": "cap_hit_auto_resume", "trigger": "x", "agent_account": "claude"},
            # Equal to watermark — skipped (ts <= wm).
            {"ts": 1000, "ts_iso": "2026-05-12T12:00:00Z",
             "class": "cap_hit_auto_resume", "trigger": "y", "agent_account": "claude"},
            # Above watermark — INCLUDED.
            {"ts": 1500, "ts_iso": "2026-05-12T13:00:00Z",
             "class": "cap_hit_auto_resume", "trigger": "z", "agent_account": "claude"},
        ]
        with patch.object(srr, "pull_repo_or_warn"), \
             patch.object(srr, "read_events_cross_account", return_value=sample), \
             patch.object(srr, "journal_started_events", return_value=[]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(["--dry-run", "--no-journal-sweep"])  # default-safe via --dry-run
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        # Only the >1000 entry's id should appear.
        self.assertIn("2026-05-12T13:00:00Z", out)
        self.assertNotIn("2026-05-12T11:00:00Z", out)
        self.assertNotIn("2026-05-12T12:00:00Z", out)


# ---------------------------------------------------------------------------
# Main flow — HERMES rationale belt
# ---------------------------------------------------------------------------


class TestMainHermesBelt(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        os.environ["PANTHEON_STATE_DIR"] = str(self.tmp_path)

    def tearDown(self) -> None:
        os.environ.pop("PANTHEON_STATE_DIR", None)
        self.tmp.cleanup()

    def test_dirty_rationale_blocks_with_exit_1(self):
        # Construct an entry whose `trigger` would surface the unescaped trigger
        # phrase in the rationale → HERMES belt refuses.
        dirty_trigger = "HERMES" + ".md"  # split avoids leaking the byte-sequence here
        sample = [{
            "ts": 1000, "ts_iso": "2026-05-12T12:00:00Z",
            "class": "cap_hit_auto_resume", "trigger": dirty_trigger,
            "agent_account": "claude",
        }]
        with patch.object(srr, "pull_repo_or_warn"), \
             patch.object(srr, "read_events_cross_account", return_value=sample), \
             patch.object(srr, "journal_started_events", return_value=[]):
            with redirect_stdout(io.StringIO()):
                rc = main(["--dry-run", "--no-journal-sweep"])  # default-safe via --dry-run
        self.assertEqual(rc, 1)


# ---------------------------------------------------------------------------
# Default-safe contract (T-0259)
# ---------------------------------------------------------------------------


class TestDefaultSafeContract(unittest.TestCase):
    """The script must refuse to run without an explicit mode flag."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        os.environ["PANTHEON_STATE_DIR"] = str(self.tmp_path)

    def tearDown(self) -> None:
        os.environ.pop("PANTHEON_STATE_DIR", None)
        self.tmp.cleanup()

    def test_no_flag_refuses_with_exit_64(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main([])
        self.assertEqual(rc, 64)

    def test_commit_prod_plus_dry_run_is_ambiguous(self):
        rc = main(["--commit-prod", "--dry-run"])
        self.assertEqual(rc, 64)

    def test_commit_prod_plus_no_commit_is_ambiguous(self):
        rc = main(["--commit-prod", "--no-commit"])
        self.assertEqual(rc, 64)

    def test_dry_run_alone_is_accepted(self):
        with patch.object(srr, "pull_repo_or_warn"), \
             patch.object(srr, "read_events_cross_account", return_value=[]), \
             patch.object(srr, "journal_started_events", return_value=[]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(["--dry-run", "--no-journal-sweep"])
        self.assertEqual(rc, 0)

    def test_no_commit_alone_is_accepted(self):
        with patch.object(srr, "pull_repo_or_warn"), \
             patch.object(srr, "read_events_cross_account", return_value=[]), \
             patch.object(srr, "journal_started_events", return_value=[]):
            with redirect_stdout(io.StringIO()):
                rc = main(["--no-commit", "--no-journal-sweep"])
        self.assertEqual(rc, 0)

    def test_ledger_dir_override_is_accepted(self):
        # Overriding --ledger-dir is sufficient as a safe-mode signal.
        with patch.object(srr, "pull_repo_or_warn"), \
             patch.object(srr, "read_events_cross_account", return_value=[]), \
             patch.object(srr, "journal_started_events", return_value=[]):
            with redirect_stdout(io.StringIO()):
                rc = main(["--ledger-dir", str(self.tmp_path / "tmp_ledger"), "--no-journal-sweep"])
        self.assertEqual(rc, 0)

    def test_commit_prod_alone_is_accepted(self):
        with patch.object(srr, "pull_repo_or_warn"), \
             patch.object(srr, "read_events_cross_account", return_value=[]), \
             patch.object(srr, "journal_started_events", return_value=[]):
            with redirect_stdout(io.StringIO()):
                rc = main(["--commit-prod", "--no-journal-sweep"])
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# Layer-3 integration tests (T-0259)
# ---------------------------------------------------------------------------


class TestLayer3Integration(unittest.TestCase):
    """End-to-end: spawn the real script in a subprocess against tmp git repo +
    tmp ledger dir + tmp state dir; assert real pantheon-config is untouched.
    """

    def setUp(self) -> None:
        from tools._test_fixtures import setup_integration_env, snapshot_real_repo
        self.real_snapshot = snapshot_real_repo()
        self.env = setup_integration_env()

    def tearDown(self) -> None:
        from tools._test_fixtures import teardown_integration_env, assert_real_repo_untouched
        teardown_integration_env(self.env)
        assert_real_repo_untouched(self.real_snapshot)

    def test_no_flag_invocation_refuses_loudly(self):
        from tools._test_fixtures import run_script
        rc, _stdout, stderr = run_script(
            "session_reset_reconcile", [],
            env=self.env.env_overrides,
        )
        self.assertEqual(rc, 64)
        self.assertIn("refusing to run without explicit mode", stderr)

    def test_dry_run_returns_zero_with_no_state(self):
        from tools._test_fixtures import run_script
        rc, _stdout, _stderr = run_script(
            "session_reset_reconcile", ["--dry-run", "--no-journal-sweep"],
            env=self.env.env_overrides,
        )
        self.assertEqual(rc, 0)

    def test_no_commit_against_tmp_ledger_appends_but_does_not_push(self):
        """--no-commit + seeded entry → ledger gets the entry, real repo untouched."""
        from tools._test_fixtures import run_script
        from tools._test_fixtures import git_head
        # Seed an entry into the tmp state dir.
        rc_seed, _, _ = run_script(
            "session_reset_log",
            ["emit", "--class", "handoff_restart", "--trigger", "layer3_test",
             "--extras", "service=integration.service"],
            env=self.env.env_overrides,
        )
        self.assertEqual(rc_seed, 0)
        # Run reconciler with --no-commit + --ledger-dir into the tmp repo.
        tmp_ledger = self.env.repo_path / "ledger"
        tmp_ledger.mkdir()
        rc, _stdout, _stderr = run_script(
            "session_reset_reconcile",
            ["--no-commit", "--no-journal-sweep", "--ledger-dir", str(tmp_ledger)],
            env=self.env.env_overrides,
        )
        self.assertEqual(rc, 0)
        # tmp ledger should have the entry. T-0148 Phase 2: with no legacy
        # 2026.jsonl present in the fresh tmp ledger dir, current_write_target
        # resolves to the current ISO-week shard (and ensure_segment_dir
        # mkdirs its parent). Resolve the expected path the same way the writer
        # does so the assertion tracks the helper.
        from tools.ledger_shard import current_write_target
        shard = current_write_target(tmp_ledger, datetime.now(timezone.utc))
        self.assertTrue(shard.exists())
        self.assertTrue(shard.parent.is_dir())
        # Real repo HEAD untouched (the tearDown's assert_real_repo_untouched
        # is the canonical check; here we also confirm tmp repo HEAD didn't
        # move — since --no-commit means no git commit even in tmp).
        self.assertEqual(git_head(self.env.repo_path), self.env.baseline_head)


class TestNormalizeIso(unittest.TestCase):
    """T-1383 / ORG-A4: normalize ISO ts so fromisoformat is portable to py3.10
    (which rejects a trailing 'Z' and compact '+HHMM' offsets)."""

    def test_compact_positive_offset(self):
        # journalctl short-iso form.
        self.assertEqual(srr._normalize_iso("2026-05-11T13:30:00+0000"), "2026-05-11T13:30:00+00:00")

    def test_compact_negative_offset(self):
        self.assertEqual(srr._normalize_iso("2026-05-11T13:30:00-0500"), "2026-05-11T13:30:00-05:00")

    def test_z_suffix(self):
        self.assertEqual(srr._normalize_iso("2026-05-11T13:30:00Z"), "2026-05-11T13:30:00+00:00")

    def test_already_extended_is_idempotent(self):
        self.assertEqual(srr._normalize_iso("2026-05-11T13:30:00+00:00"), "2026-05-11T13:30:00+00:00")

    def test_naive_unchanged(self):
        self.assertEqual(srr._normalize_iso("2026-05-11T13:30:00"), "2026-05-11T13:30:00")

    def test_roundtrips_through_fromisoformat(self):
        # The point of the helper: the normalized form parses on every >=3.10,
        # to the same instant as the equivalent explicit-offset timestamp.
        from datetime import datetime
        dt = datetime.fromisoformat(srr._normalize_iso("2026-05-11T13:30:00+0000"))
        expected = datetime.fromisoformat("2026-05-11T13:30:00+00:00")
        self.assertIsNotNone(dt.tzinfo)
        self.assertEqual(dt, expected)


class TestCurrentAccountOverride(unittest.TestCase):
    """T-1386 hermeticity: _current_account honors PANTHEON_ACCOUNT so the
    cross-account own-account read branch fires regardless of host login name
    (a clean CI runner is e.g. 'runner', not in KNOWN_ACCOUNTS)."""

    def test_override_takes_precedence(self):
        import tools.session_reset_log as srl
        with patch.dict(os.environ, {"PANTHEON_ACCOUNT": "claude"}):
            self.assertEqual(srl._current_account(), "claude")

    def test_falls_back_to_getuser_when_unset(self):
        import getpass
        import tools.session_reset_log as srl
        env = dict(os.environ)
        env.pop("PANTHEON_ACCOUNT", None)
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(srl._current_account(), getpass.getuser())


if __name__ == "__main__":
    unittest.main()
