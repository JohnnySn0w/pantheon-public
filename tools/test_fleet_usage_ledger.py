"""Unit tests for tools/fleet_usage_ledger.py (T-4536, the per-seat usage ledger reader).

Run from repo root:
    python3 -m pytest tools/test_fleet_usage_ledger.py -q

Fixture provenance (D11): tools/fixtures/fleet_usage/ledger holds rows in the
pantheon-fleet-usage-turn/v1 schema the fleet-usage mod writes (the Claude row's shape is the
committed contract tools/mods/fleet-usage/contract/fleet-usage-turn-row.ts, whose first row is
copied there) plus a gate reading in the T-4510 contract shape (seven_day 30%, resets
2026-10-11T17:00Z). Values are synthetic; NOW sits one hour after the contract row.
tools/fixtures/fleet_usage/share.golden.json is this reader's share output on that fixture,
committed so the kino card (a separate pantheon-pwa PR) can assert parity with it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools import fleet_usage_ledger as ful  # noqa: E402

FIX = REPO_ROOT / "tools" / "fixtures" / "fleet_usage"
LEDGER = FIX / "ledger"
GOLDEN = FIX / "share.golden.json"
CONTRACT_TS = REPO_ROOT / "tools" / "mods" / "fleet-usage" / "contract" / "fleet-usage-turn-row.ts"
REGISTER_TS = REPO_ROOT / "tools" / "mods" / "fleet-usage" / "hooks" / "register.ts"
NOW = 1791143600  # 2026-10-04T19:53:20Z
WINDOW_START = 1791738000 - 7 * 24 * 3600
SENTINEL = "SENTINEL-T4536-CONTENT-MUST-NOT-LEAK"


def load_contract() -> dict:
    """Strict JSON after the LAST `export default` of the committed .ts fixture."""
    return json.loads(CONTRACT_TS.read_text(encoding="utf-8").rpartition("export default")[2])


def units(w, i, o, cr, cw):
    return w[0] * i + w[1] * o + w[2] * cr + w[3] * cw


class ContractTests(unittest.TestCase):
    def test_contract_row_is_accepted_and_kept_whole(self):
        doc = load_contract()
        row = ful.normalize_row(doc, "specialist")
        self.assertEqual(row, doc)

    def test_key_allowlists_match_the_contract_and_the_mod(self):
        self.assertEqual(tuple(load_contract().keys()), ful.CLAUDE_ROW_KEYS)
        src = REGISTER_TS.read_text(encoding="utf-8")
        block = src.split("export const TURN_ROW_KEYS = [", 1)[1].split("]", 1)[0]
        mod_keys = tuple(k.strip().strip("',") for k in block.split("\n") if k.strip())
        self.assertEqual(mod_keys, ful.CLAUDE_ROW_KEYS)

    def test_schema_strings_agree(self):
        self.assertIn(f"TURN_SCHEMA = '{ful.SCHEMA}'", REGISTER_TS.read_text(encoding="utf-8"))


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        self.base = load_contract()

    def test_extra_keys_are_dropped(self):
        doc = dict(self.base, answer=SENTINEL, prompt=SENTINEL, content=[SENTINEL])
        row = ful.normalize_row(doc, "specialist")
        self.assertEqual(tuple(row.keys()), ful.CLAUDE_ROW_KEYS)
        self.assertNotIn(SENTINEL, json.dumps(row))

    def test_rejects(self):
        cases = [
            dict(self.base, schema="pantheon-fleet-usage-turn/v2"),
            dict(self.base, harness="other"),
            dict(self.base, seat="orchestrator"),  # not the dir it sits in
            dict(self.base, ts=-1),
            dict(self.base, ts=True),
            dict(self.base, input_tokens=-5),
            dict(self.base, output_tokens=1.5),
            dict(self.base, cache_read_input_tokens=True),
            dict(self.base, model=""),
            dict(self.base, agent_id=7),
            [],
            "x",
        ]
        for doc in cases:
            self.assertIsNone(ful.normalize_row(doc, "specialist"), doc)

    def test_codex_row_keeps_reasoning_field(self):
        doc = dict(self.base, harness="codex", seat="consultant", reasoning_output_tokens=9)
        self.assertEqual(ful.normalize_row(doc, "consultant")["reasoning_output_tokens"], 9)
        doc.pop("reasoning_output_tokens")
        self.assertEqual(ful.normalize_row(doc, "consultant")["reasoning_output_tokens"], 0)


class WeightsTests(unittest.TestCase):
    def test_default_and_per_model_ratios(self):
        d = ful.weights_for("claude-sonnet-5-5")
        self.assertEqual((d.input, d.output, d.cache_read, d.cache_creation), (1.0, 5.0, 0.1, 1.25))
        self.assertEqual(ful.weights_for("claude-opus-5-5").cache_read, 0.05)
        self.assertEqual(ful.weights_for("claude-opus-5-5[1m]").cache_read, 0.05)
        self.assertEqual(ful.weights_for("claude-fable-5-1").cache_read, 0.025)
        self.assertEqual(ful.weights_for("claude-mythos-5-1").cache_read, 0.025)
        # prefix boundary: a different model that merely starts with the key is not it
        self.assertEqual(ful.weights_for("claude-opus-5-50").cache_read, 0.1)
        self.assertEqual(ful.weights_for("claude-opus-5").cache_read, 0.1)
        self.assertEqual(ful.weights_for("unknown").cache_read, 0.1)

    def test_override_applies_to_every_model(self):
        w = ful.parse_weights("in=2,out=3")
        self.assertEqual(ful.weights_for("claude-opus-5-5", w), w)
        self.assertEqual((w.input, w.output, w.cache_read, w.cache_creation), (2.0, 3.0, 0.1, 1.25))

    def test_bad_weights_refused(self):
        for text in ("x=1", "in", "in=-1", "in=nan", "in=inf"):
            with self.assertRaises(ValueError):
                ful.parse_weights(text)

    def test_the_estimate_is_labelled_and_sourced(self):
        self.assertIn("NOT Anthropic's quota formula", ful.WEIGHTS_NOTE)
        self.assertIn("claude-api skill", ful.WEIGHTS_SOURCE)
        self.assertIn("NOT Anthropic's quota formula", ful.__doc__)


class ReadAndRollupTests(unittest.TestCase):
    def test_window_read_skips_and_dedupes(self):
        res = ful.read_rows(LEDGER, start_ts=WINDOW_START, end_ts=NOW + 1)
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.skipped, {"bad_line": 1, "bad_row": 1, "bad_file": 1, "duplicate": 1})
        turns = sorted((r["seat"], r["turn_id"]) for r in res.rows)
        self.assertEqual(turns, [
            ("specialist", "t-extra"), ("specialist", "t-sub"),
            ("specialist", "turn_01HZX9Q2V7K3M8N4P5R6S7T8U9"),
            ("consultant", "ct1"), ("orchestrator", "th1"), ("orchestrator", "th2"),
        ])
        self.assertNotIn(SENTINEL, json.dumps(res.rows))

    def test_all_rows_include_yesterday_and_pre_window(self):
        res = ful.read_rows(LEDGER)
        ids = {r["turn_id"] for r in res.rows}
        self.assertTrue({"t-yesterday", "t-early"} <= ids)

    def test_rollup_by_seat_hand_computed(self):
        res = ful.read_rows(LEDGER, start_ts=WINDOW_START, end_ts=NOW + 1)
        groups = {g["key"]["seat"]: g for g in ful.rollup(res.rows, ("seat",))}
        opus, sonnet, fable = (1, 5, 0.05, 1.25), (1, 5, 0.1, 1.25), (1, 5, 0.025, 1.25)
        specialist = (units(opus, 1200, 850, 48000, 3100) + units(opus, 100, 10, 0, 0)
                 + units(sonnet, 400, 200, 10000, 0))
        orchestrator = units(fable, 3000, 1000, 200000, 8000) + units(opus, 500, 500, 20000, 0)
        self.assertAlmostEqual(groups["specialist"]["units"], specialist)
        self.assertAlmostEqual(groups["orchestrator"]["units"], orchestrator)
        self.assertIsNone(groups["consultant"]["units"])  # codex: separate pool, no units
        self.assertEqual(groups["specialist"]["turns"], 3)
        self.assertEqual(groups["specialist"]["tokens"]["input_tokens"], 1700)

    def test_rollup_dimensions(self):
        res = ful.read_rows(LEDGER, start_ts=WINDOW_START, end_ts=NOW + 1)
        keys = [g["key"] for g in ful.rollup(res.rows, ("seat", "agent_type", "worker"))]
        self.assertIn({"seat": "specialist", "agent_type": "Explore", "worker": "ag1"}, keys)
        self.assertIn({"seat": "specialist", "agent_type": "main", "worker": "main"}, keys)
        days = {g["key"]["day"] for g in ful.rollup(ful.read_rows(LEDGER).rows, ("day",))}
        self.assertEqual(days, {"2026-10-03", "2026-10-04"})
        roles = {g["key"]["role"] for g in ful.rollup(res.rows, ("role",))}
        self.assertIn("head_research_orchestrator", roles)
        harness = {g["key"]["harness"] for g in ful.rollup(res.rows, ("harness",))}
        self.assertEqual(harness, {"claude", "codex"})
        with self.assertRaises(ValueError):
            ful.rollup(res.rows, ("colour",))

    def test_missing_dir_is_no_data(self):
        res = ful.read_rows("/nonexistent/fleet-usage")
        self.assertEqual((res.rows, res.status), ([], "no-data"))


class ShareTests(unittest.TestCase):
    def test_share_matches_the_golden_file(self):
        doc = ful.seven_day_share(LEDGER, NOW)
        self.assertEqual(json.loads(json.dumps(doc)), json.loads(GOLDEN.read_text()))

    def test_share_arithmetic(self):
        doc = ful.seven_day_share(LEDGER, NOW)
        seats = {s["seat"]: s for s in doc["seats"]}
        total = seats["specialist"]["units"] + seats["orchestrator"]["units"]
        self.assertAlmostEqual(seats["specialist"]["share"], seats["specialist"]["units"] / total, places=5)
        self.assertAlmostEqual(
            seats["specialist"]["est_pct_points"] + seats["orchestrator"]["est_pct_points"], 30, places=2)
        self.assertEqual(doc["window"]["start"], WINDOW_START)
        self.assertEqual([c["seat"] for c in doc["codex"]], ["consultant"])
        self.assertNotIn("consultant", seats)
        self.assertTrue(doc["estimate"])

    def test_no_valid_reading_gives_shares_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "turns" / "specialist"
            dest.mkdir(parents=True)
            (dest / "2026-10-04.jsonl").write_text(json.dumps(load_contract()) + "\n")
            doc = ful.seven_day_share(tmp, NOW)
        self.assertIsNone(doc["window"])
        self.assertIn("no valid seven_day reading", doc["reason"])
        self.assertEqual(doc["seats"][0]["share"], 1.0)
        self.assertIsNone(doc["seats"][0]["est_pct_points"])

    def test_empty_dir_is_no_data_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = ful.seven_day_share(tmp, NOW)
        self.assertEqual((doc["status"], doc["seats"]), ("no-data", []))

    def test_cli_share_json_equals_golden(self):
        out = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "fleet_usage_ledger.py"),
             "--dir", str(LEDGER), "--now", str(NOW), "--json", "share"],
            capture_output=True, text=True, timeout=60, check=True)
        self.assertEqual(json.loads(out.stdout), json.loads(GOLDEN.read_text()))

    def test_cli_text_output_states_the_estimate(self):
        out = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "fleet_usage_ledger.py"),
             "--dir", str(LEDGER), "--now", str(NOW), "share"],
            capture_output=True, text=True, timeout=60, check=True)
        self.assertIn("NOT Anthropic's quota formula", out.stdout)
        self.assertIn("claude-api skill", out.stdout)


class PruneTests(unittest.TestCase):
    TODAY = date(2026, 11, 2)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.seat_dir = self.dir / "turns" / "specialist"
        self.seat_dir.mkdir(parents=True)
        for name in ("2026-09-01.jsonl", "2026-09-20.jsonl", "2026-10-30.jsonl"):
            (self.seat_dir / name).write_text("{}\n")
        self.env = {"PANTHEON_FLEET_USAGE_SEAT": "specialist"}

    def names(self):
        # T-5962: the seat's aggregate and its lock live beside the day files
        return sorted(n for n in os.listdir(self.seat_dir) if not ful.is_reserved_name(n))

    def test_dry_run_by_default_deletes_nothing(self):
        out = ful.prune(self.dir, "specialist", 35, today=self.TODAY, env=self.env)
        self.assertEqual(out["would_delete"], ["2026-09-01.jsonl", "2026-09-20.jsonl"])
        self.assertEqual(len(self.names()), 3)

    def test_commit_deletes_only_old_well_formed_files(self):
        out = ful.prune(self.dir, "specialist", 35, commit=True, today=self.TODAY, env=self.env)
        self.assertEqual(out["deleted"], ["2026-09-01.jsonl", "2026-09-20.jsonl"])
        self.assertEqual(self.names(), ["2026-10-30.jsonl"])
        # T-5962: every deleted day was folded and sealed first
        doc = json.loads((self.seat_dir / ful.AGG_FILE).read_text())
        self.assertIsNotNone(doc["days"]["2026-09-01"]["sealed_at"])
        self.assertIsNone(doc["days"]["2026-10-30"]["sealed_at"])

    def test_refuses_another_seats_dir(self):
        (self.dir / "turns" / "orchestrator").mkdir()
        (self.dir / "turns" / "orchestrator" / "2026-09-01.jsonl").write_text("{}\n")
        with self.assertRaises(ful.PruneRefused):
            ful.prune(self.dir, "orchestrator", 35, commit=True, today=self.TODAY, env=self.env)
        self.assertTrue((self.dir / "turns" / "orchestrator" / "2026-09-01.jsonl").exists())

    def test_refuses_bad_seat_names_and_short_retention(self):
        for seat in ("../specialist", "", "a/b", ".", "..", "specialist\n"):
            with self.assertRaises(ful.PruneRefused):
                ful.prune(self.dir, seat, 35, today=self.TODAY, env={"PANTHEON_FLEET_USAGE_SEAT": seat})
        with self.assertRaises(ful.PruneRefused):
            ful.prune(self.dir, "specialist", 7, today=self.TODAY, env=self.env)

    def test_malformed_names_and_symlinks_are_never_deleted(self):
        keep = ["notes.txt", "2026-9-1.jsonl", "2026-02-30.jsonl", "2026-09-01.jsonl.bak",
                ".2026-09-01.jsonl.tmp"]
        for name in keep:
            (self.seat_dir / name).write_text("x\n")
        outside = self.dir / "outside.jsonl"
        outside.write_text("precious\n")
        os.symlink(outside, self.seat_dir / "2026-08-01.jsonl")
        (self.seat_dir / "2026-08-02.jsonl").mkdir()
        out = ful.prune(self.dir, "specialist", 35, commit=True, today=self.TODAY, env=self.env)
        self.assertEqual(out["deleted"], ["2026-09-01.jsonl", "2026-09-20.jsonl"])
        for name in keep + ["2026-08-01.jsonl", "2026-08-02.jsonl"]:
            self.assertTrue(os.path.lexists(self.seat_dir / name), name)
        self.assertEqual(outside.read_text(), "precious\n")

    def test_refuses_a_symlinked_seat_dir(self):
        real = self.dir / "elsewhere"
        real.mkdir()
        (real / "2026-09-01.jsonl").write_text("{}\n")
        link_root = self.dir / "linked"
        (link_root / "turns").mkdir(parents=True)
        os.symlink(real, link_root / "turns" / "specialist")
        with self.assertRaises(ful.PruneRefused):
            ful.prune(link_root, "specialist", 35, commit=True, today=self.TODAY, env=self.env)
        self.assertTrue((real / "2026-09-01.jsonl").exists())

    def test_cli_unflagged_run_never_deletes(self):
        env = dict(os.environ, PANTHEON_FLEET_USAGE_SEAT="specialist")
        out = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "fleet_usage_ledger.py"),
             "--dir", str(self.dir), "--now", "1793577600", "prune", "--seat", "specialist",
             "--older-than-days", "35"],
            capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("dry run", out.stdout)
        self.assertEqual(len(self.names()), 3)

    def test_dotdot_seat_never_reaches_the_top_dir(self):
        # review F1: `prune --seat ..` once listed a top-level file as would_delete
        (self.dir / "turns" / "2026-09-01.jsonl").write_text("{}\n")
        for seat in (".", ".."):
            with self.assertRaises(ful.PruneRefused):
                ful.prune(self.dir, seat, 35, commit=True, today=self.TODAY,
                          env={"PANTHEON_FLEET_USAGE_SEAT": seat})
        self.assertTrue((self.dir / "turns" / "2026-09-01.jsonl").exists())
        self.assertIsNone(ful.own_seat({"PANTHEON_FLEET_USAGE_SEAT": "..", "USER": "."}))

    def test_trailing_newline_is_refused(self):
        # review N4: agree with the mod's JS patterns (Python's $ matches before a final \n)
        self.assertIsNone(ful.SEAT_RE.match("specialist\n"))
        self.assertIsNotNone(ful.SEAT_RE.match("specialist"))
        self.assertEqual(ful.own_seat({"PANTHEON_FLEET_USAGE_SEAT": "specialist\n", "USER": "ok"}), "ok")
        self.assertIsNone(ful.DAY_FILE_RE.match("2026-09-01.jsonl\n"))
        (self.seat_dir / "2026-09-02.jsonl\n").write_text("{}\n")
        out = ful.prune(self.dir, "specialist", 35, commit=True, today=self.TODAY, env=self.env)
        self.assertIn("2026-09-02.jsonl\n", out["ignored"])
        self.assertTrue((self.seat_dir / "2026-09-02.jsonl\n").exists())

    def test_cli_rejects_an_abbreviated_commit_flag(self):
        # review F8: allow_abbrev=False, so `--comm` is an error, not `--commit`
        env = dict(os.environ, PANTHEON_FLEET_USAGE_SEAT="specialist")
        out = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "fleet_usage_ledger.py"),
             "--dir", str(self.dir), "prune", "--seat", "specialist", "--older-than-days", "35",
             "--comm"],
            capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(out.returncode, 2)
        self.assertIn("unrecognized arguments: --comm", out.stderr)
        self.assertEqual(len(self.names()), 3)

    def test_cli_refusal_exits_2(self):
        env = dict(os.environ, PANTHEON_FLEET_USAGE_SEAT="specialist")
        out = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "fleet_usage_ledger.py"),
             "--dir", str(self.dir), "prune", "--seat", "orchestrator", "--older-than-days", "35",
             "--commit"],
            capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(out.returncode, 2)
        self.assertIn("refus", out.stderr)


if __name__ == "__main__":
    unittest.main()
