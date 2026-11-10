"""Unit tests for tools/fleet_usage_codex.py (T-4536, Codex seat usage rows).

Run from repo root:
    python3 -m pytest tools/test_fleet_usage_codex.py -q

Fixture provenance (D11): tools/fixtures/fleet_usage/codex/sessions holds two rollouts built
in the Codex 0.147.0 shape Consultant verified live in T-2957 and codex_context_lifecycle.py
parses (session_meta at ordinal 0; event_msg/token_count with info.last_token_usage /
total_token_usage; task_started / turn_context / turn_aborted carrying turn_id), with the
duplicate token_count lines real rollouts carry (cf. tools/fixtures/harness_idle/codex/).
Values are synthetic. A sentinel sits in base_instructions, a message, and the subagent's
nickname to prove none is ever copied into a row.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools import fleet_usage_codex as fuc  # noqa: E402
from tools import fleet_usage_ledger as ful  # noqa: E402
from tools.harness_adapter.quota import QuotaSnapshot, QuotaWindow  # noqa: E402

SESSIONS = REPO_ROOT / "tools" / "fixtures" / "fleet_usage" / "codex" / "sessions"
DAY = SESSIONS / "2026" / "10" / "28"
ROOT = "01a1803a-ae9e-7a33-8988-89e3ce3d3e07"
SUB = "01a1803a-0000-7000-8000-00000000a9e7"
T1 = "01a1803c-8047-7463-962d-b1a3a078ae09"
T2 = "01a18242-6499-78d0-a71a-a1bceb2c2037"
T3 = "01a18300-0000-7000-8000-000000000003"
SENTINEL = "SENTINEL-T4536-CONTENT-MUST-NOT-LEAK"
NOW = 1793232000  # 2026-10-29T00:00:00Z
CONSULTANT = {"PANTHEON_FLEET_USAGE_SEAT": "consultant"}


def rows():
    return {r["turn_id"]: r for r in fuc.collect_rows(SESSIONS, "consultant")}


class MappingTests(unittest.TestCase):
    def test_one_row_per_turn_with_total_deltas(self):
        r = rows()
        self.assertEqual(set(r), {T1, T2, T3})
        t1 = r[T1]
        # 22000 input incl. 19000 cached -> 3000 uncached; duplicate token_count adds nothing
        self.assertEqual((t1["input_tokens"], t1["cache_read_input_tokens"],
                          t1["cache_creation_input_tokens"], t1["output_tokens"],
                          t1["reasoning_output_tokens"]), (3000, 19000, 500, 500, 150))
        t2 = r[T2]
        self.assertEqual((t2["input_tokens"], t2["cache_read_input_tokens"],
                          t2["output_tokens"]), (2000, 1000, 400))

    def test_a_falling_total_uses_last_token_usage(self):
        t3 = rows()[T3]
        self.assertEqual((t3["input_tokens"], t3["cache_read_input_tokens"],
                          t3["output_tokens"]), (5100, 600, 150))

    def test_model_source_chain(self):
        r = rows()
        self.assertEqual((r[T1]["model"], r[T1]["model_source"]), ("gpt-5.6-sol", "turn_context"))
        # turn_context without a model -> the seat's appserver_seats.json model
        self.assertEqual((r[T2]["model"], r[T2]["model_source"]), ("gpt-5.6-sol", "seat_config"))
        # a model failing the safe pattern is not trusted
        self.assertEqual(r[T3]["model_source"], "seat_config")

    def test_unknown_when_no_seat_config(self):
        r = {x["turn_id"]: x for x in fuc.collect_rows(SESSIONS, "no-such-seat")}
        self.assertEqual((r[T2]["model"], r[T2]["model_source"]), ("unknown", "unknown"))
        self.assertEqual(r[T1]["model_source"], "turn_context")

    def test_trailing_newline_never_passes_a_pattern(self):
        # review N4: Python's $ matches before a trailing newline; \Z does not
        for pattern in (fuc.ID_RE, fuc.TYPE_RE, fuc.MODEL_RE):
            self.assertIsNotNone(pattern.match("abc"))
            self.assertIsNone(pattern.match("abc\n"))
        self.assertIsNone(fuc.ROLLOUT_RE.match(f"rollout-2026-10-28T01-58-16-{ROOT}.jsonl\n"))

    def test_seat_config_reads_the_repo_file(self):
        self.assertEqual(fuc.seat_config_model("consultant"), "gpt-5.6-sol")
        self.assertEqual(fuc.seat_config_model("watchman"), "gpt-5.6-sol")
        self.assertIsNone(fuc.seat_config_model("orchestrator"))

    def test_root_and_subagent_attribution(self):
        r = rows()
        self.assertEqual((r[T1]["agent_id"], r[T1]["agent_type"], r[T1]["parent_agent_id"]),
                         (None, None, None))
        self.assertEqual((r[T3]["agent_id"], r[T3]["agent_type"], r[T3]["parent_agent_id"]),
                         (SUB, "worker", ROOT))
        self.assertEqual(r[T1]["session_id"], ROOT)

    def test_reason_ts_and_duration(self):
        r = rows()
        self.assertEqual(r[T2]["reason"], "aborted")
        self.assertEqual(r[T1]["reason"], "unknown")
        self.assertEqual(r[T1]["ts"], 1793152820)  # 2026-10-28T02:00:20Z, the last token event
        self.assertEqual(r[T1]["duration_ms"], 9705)

    def test_rows_are_valid_ledger_rows_with_only_allowlisted_keys(self):
        for row in rows().values():
            self.assertEqual(tuple(row), ful.CODEX_ROW_KEYS)
            self.assertEqual(ful.normalize_row(row, "consultant"), row)
            self.assertEqual(row["harness"], "codex")

    def test_no_content_ever(self):
        self.assertNotIn(SENTINEL, json.dumps(list(rows().values())))
        raw = "".join(p.read_text() for p in DAY.glob("rollout-*.jsonl"))
        self.assertIn(SENTINEL, raw)  # the sentinel really is in the source

    def test_non_rollouts_and_symlinks_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / "2026" / "10" / "28"
            day.mkdir(parents=True)
            src = DAY / f"rollout-2026-10-28T01-58-16-{ROOT}.jsonl"
            os.symlink(src, day / src.name)
            (day / "notes.jsonl").write_text("{}\n")
            self.assertEqual(fuc.collect_rows(Path(tmp), "consultant"), [])

    def test_garbage_rollout_gives_no_rows_and_no_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / f"rollout-2026-10-28T01-58-16-{ROOT}.jsonl"
            p.write_bytes(b"\xff\xfe not json\n[1,2]\n{\"type\":\"event_msg\",\"payload\":7}\n")
            self.assertEqual(fuc.rows_from_rollout(p, "consultant", None), [])


class ExportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "fleet-usage"
        self.dir.mkdir()

    def test_dry_run_by_default_writes_nothing(self):
        out = fuc.export(SESSIONS, "consultant", self.dir, days=2, now=NOW, env=CONSULTANT)
        self.assertEqual(out["days"], {"2026-10-28": 3, "2026-10-29": 0})
        self.assertFalse((self.dir / "turns").exists())

    def test_commit_writes_day_files_the_ledger_reads(self):
        fuc.export(SESSIONS, "consultant", self.dir, days=2, now=NOW, commit=True, env=CONSULTANT)
        target = self.dir / "turns" / "consultant" / "2026-10-28.jsonl"
        self.assertEqual(len(target.read_text().splitlines()), 3)
        self.assertEqual(oct(target.stat().st_mode & 0o777), "0o644")
        # review N2: turns/ setgid + sticky, as the mod creates it
        self.assertEqual(oct((self.dir / "turns").stat().st_mode & 0o7777), "0o3775")
        self.assertEqual(oct((self.dir / "turns" / "consultant").stat().st_mode & 0o7777), "0o2755")
        res = ful.read_rows(self.dir)
        self.assertEqual(len(res.rows), 3)
        self.assertEqual(res.skipped["bad_row"], 0)
        self.assertFalse((self.dir / "turns" / "consultant" / "2026-10-29.jsonl").exists())

    def test_export_is_idempotent(self):
        fuc.export(SESSIONS, "consultant", self.dir, days=2, now=NOW, commit=True, env=CONSULTANT)
        target = self.dir / "turns" / "consultant" / "2026-10-28.jsonl"
        first = target.read_bytes()
        fuc.export(SESSIONS, "consultant", self.dir, days=2, now=NOW, commit=True, env=CONSULTANT)
        self.assertEqual(target.read_bytes(), first)
        self.assertEqual([p.name for p in target.parent.iterdir()], ["2026-10-28.jsonl"])

    def test_never_creates_the_top_dir(self):
        with self.assertRaises(ValueError):
            fuc.export(SESSIONS, "consultant", self.dir / "absent", days=2, now=NOW, commit=True, env=CONSULTANT)
        self.assertFalse((self.dir / "absent").exists())

    def test_refuses_a_symlinked_day_file(self):
        seat_dir = self.dir / "turns" / "consultant"
        seat_dir.mkdir(parents=True)
        victim = self.dir / "victim.txt"
        victim.write_text("keep\n")
        os.symlink(victim, seat_dir / "2026-10-28.jsonl")
        with self.assertRaises(ValueError):
            fuc.export(SESSIONS, "consultant", self.dir, days=2, now=NOW, commit=True, env=CONSULTANT)
        self.assertEqual(victim.read_text(), "keep\n")

    def test_bad_seat_refused(self):
        for seat in ("../x", ".", ".."):
            with self.assertRaisesRegex(ValueError, "bad seat name"):
                fuc.export(SESSIONS, seat, self.dir, now=NOW, commit=True,
                           env={"PANTHEON_FLEET_USAGE_SEAT": seat})
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_refuses_a_seat_that_is_not_the_callers_own(self):
        # review F5: run as orchestrator, an export for "orchestrator" or "consultant" must not write
        with self.assertRaisesRegex(ValueError, "not this caller's own seat"):
            fuc.export(SESSIONS, "consultant", self.dir, days=2, now=NOW, commit=True,
                       env={"PANTHEON_FLEET_USAGE_SEAT": "orchestrator"})
        self.assertFalse((self.dir / "turns").exists())

    def test_never_replaces_a_day_file_holding_claude_rows(self):
        # review F5: even as the seat's own account, Claude rows are never overwritten
        seat_dir = self.dir / "turns" / "consultant"
        seat_dir.mkdir(parents=True)
        claude = json.dumps({"schema": ful.SCHEMA, "harness": "claude", "seat": "consultant"}) + "\n"
        target = seat_dir / "2026-10-28.jsonl"
        target.write_text(claude)
        with self.assertRaisesRegex(ValueError, "non-codex rows"):
            fuc.export(SESSIONS, "consultant", self.dir, days=2, now=NOW, commit=True, env=CONSULTANT)
        self.assertEqual(target.read_text(), claude)

    def test_cli_rejects_an_abbreviated_commit_flag(self):
        # review F8: allow_abbrev=False, so `--comm` is an error, not `--commit`
        import subprocess
        out = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "fleet_usage_codex.py"), "export",
             "--sessions", str(SESSIONS), "--seat", "consultant", "--dir", str(self.dir), "--comm"],
            capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PANTHEON_FLEET_USAGE_SEAT="consultant"))
        self.assertEqual(out.returncode, 2)
        self.assertIn("unrecognized arguments: --comm", out.stderr)
        self.assertFalse((self.dir / "turns").exists())


class QuotaGateRecordTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name) / "fleet-usage"
        self.directory.mkdir()
        (self.directory / "turns" / "consultant").mkdir(parents=True)
        self.now = datetime.fromtimestamp(NOW, timezone.utc)

    def snapshot(self, weekly=6.0, five=25.0):
        windows = (
            QuotaWindow("5h", five, 100.0 - five, self.now + timedelta(hours=2)),
            QuotaWindow("weekly", weekly, 100.0 - weekly, self.now + timedelta(days=3)),
        )
        return QuotaSnapshot("codex/interactive-chatgpt", windows, self.now, "fresh", {
            "provider": "codex", "source": "app-server"
        })

    def test_app_server_snapshot_becomes_provider_tagged_gate_record(self):
        record = fuc.quota_gate_record("consultant", self.snapshot(), now=self.now)
        self.assertEqual(record["schema"], "pantheon-fleet-usage/v1")
        self.assertEqual((record["harness"], record["seat"]), ("codex", "consultant"))
        self.assertEqual(record["seven_day"]["used_percentage"], 6.0)
        self.assertEqual(record["five_hour"]["used_percentage"], 25.0)

    def test_unavailable_snapshot_writes_null_windows_to_replace_stale_truth(self):
        snapshot = QuotaSnapshot("codex/x", (), self.now, "unavailable", {"reason": "offline"})
        record = fuc.quota_gate_record("consultant", snapshot, now=self.now)
        self.assertIsNone(record["seven_day"])
        self.assertIsNone(record["five_hour"])

    def test_write_is_atomic_mode_0644_and_readable_by_the_gate(self):
        path = fuc.write_quota_gate_record(
            self.directory, "consultant", self.snapshot(weekly=96.0), now=self.now
        )
        self.assertEqual(path, self.directory / "turns" / "consultant" / "quota.json")
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        from tools import fleet_usage_gate
        readings = fleet_usage_gate.load_readings(
            self.directory, NOW, harness="codex", seat="consultant"
        )
        self.assertEqual(readings["seven_day"].pct, 96.0)

    def test_write_refuses_symlink_target(self):
        victim = self.directory / "victim"
        victim.write_text("keep\n")
        (self.directory / "turns" / "consultant" / "quota.json").symlink_to(victim)
        with self.assertRaisesRegex(ValueError, "symlink"):
            fuc.write_quota_gate_record(self.directory, "consultant", self.snapshot(), now=self.now)
        self.assertEqual(victim.read_text(), "keep\n")


if __name__ == "__main__":
    unittest.main()
