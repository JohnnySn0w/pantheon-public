"""Tests for tools/health_collector_check.py (T-5102): the fleet health aggregate freshness reader.

FIXTURE. tools/fixtures/health_collector/fleet_health_sample.json is what the REAL collector code
writes: ``health_collector.collect()`` run over the CAPTURED emitter replies (T-5410's verbatim
PersonaController1 GetAll from a scratch bus, flag off; the captured busctl stderr for a
non-monitor uid and an unowned name) at a fixed clock. That is today's production shape: the
broker's Health1 is denied / no_service and every persona controller lacks the health leg, so it
reads ``not_configured``. The live file (/run/pantheon-health, mode 2750 claude-fallback) is not
readable from the authoring seat and is never read here; ``test_sample_is_what_the_collector_writes``
regenerates the sample and fails if the collector's output shape moves.

Every test reads only tmp files or the committed fixture: no /run, no seat home, no bus, no sudo.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import health_collector as hc
from tools import health_collector_check as C

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools" / "health_collector_check.py"
FIXTURE_DIR = REPO / "tools" / "fixtures" / "health_collector"
SAMPLE_PATH = FIXTURE_DIR / "fleet_health_sample.json"
PC_TODAY = (FIXTURE_DIR / "persona_getall_flag_off.json").read_text(encoding="utf-8")
STDERR_DENIED = "Call failed: Access denied"
STDERR_NO_SERVICE = "Call failed: The name org.x was not provided by any .service files"
SAMPLE_NOW = 1_800_000_000
SAMPLE_PERSONAS = (("orchestrator", "claude", None), ("recovery", "claude-fallback", None), ("artisan", "suborch2", None))


class _CapturedReader:
    """Replays captured replies in CollectorReader's (reach_class, stdout|None) shape."""

    def getall_system(self, bus_name, path, interface):
        if bus_name == "org.pantheon.HostBroker1":
            return hc.classify_stderr(STDERR_DENIED), None
        return hc.classify_stderr(STDERR_NO_SERVICE), None

    def getall_session(self, account, persona, interface):
        if persona == "artisan":
            return "unreachable", None
        return "ok", PC_TODAY


def regenerate_sample() -> dict:
    return hc.collect(SAMPLE_PERSONAS, reader=_CapturedReader(), now=SAMPLE_NOW, mode="signal")


def sample() -> dict:
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="t5102check-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "fleet-health.json"

    def put(self, doc) -> Path:
        text = doc if isinstance(doc, str) else json.dumps(doc)
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def check(self, now=SAMPLE_NOW + 30, max_age=180, path=None):
        return C.check(str(path or self.path), max_age, now)


def _timeless(doc: dict) -> dict:
    """duration_ms is wall-clock: the one field a regenerate cannot reproduce."""

    doc = json.loads(json.dumps(doc))
    doc["collector"]["duration_ms"] = 0
    return doc


class FixtureTests(unittest.TestCase):
    def test_sample_is_what_the_collector_writes(self):
        self.assertEqual(_timeless(sample()), _timeless(regenerate_sample()))

    def test_sample_carries_todays_production_shape(self):
        by_id = {e["component_id"]: e for e in sample()["entries"]}
        self.assertEqual(by_id["host-broker"]["reachability"], "denied")
        self.assertEqual(by_id["host-approval"]["reachability"], "no_service")
        self.assertEqual(by_id["persona-controller.recovery"]["current_status"], "not_configured")
        self.assertEqual(by_id["persona-controller.recovery"]["health_leg"], "absent")
        self.assertEqual(by_id["persona-controller.artisan"]["reachability"], "unreachable")


class VerdictTests(_TmpCase):
    def test_fresh_sample_is_exit_0(self):
        self.put(sample())
        out = self.check()
        self.assertEqual((out["status"], out["exit"]), ("fresh", C.EXIT_FRESH))
        self.assertEqual(out["age_secs"], 30)
        self.assertEqual(out["collected_at"], SAMPLE_NOW)
        self.assertEqual(out["mode"], "signal")
        self.assertEqual(out["entry_count"], 5)

    def test_age_equal_to_threshold_is_fresh_one_more_is_stale(self):
        self.put(sample())
        self.assertEqual(self.check(now=SAMPLE_NOW + 180)["status"], "fresh")
        out = self.check(now=SAMPLE_NOW + 181)
        self.assertEqual((out["status"], out["exit"]), ("stale", C.EXIT_STALE))
        self.assertEqual(out["age_secs"], 181)

    def test_missing_file_is_exit_11(self):
        out = self.check()
        self.assertEqual((out["status"], out["exit"]), ("missing", C.EXIT_MISSING))
        self.assertIsNone(out["age_secs"])

    def test_directory_in_place_of_the_file_is_unreadable(self):
        self.path.mkdir()
        out = self.check()
        self.assertEqual((out["status"], out["exit"]), ("unreadable", C.EXIT_UNREADABLE))

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode-000 file")
    def test_permission_denied_is_unreadable(self):
        self.put(sample()).chmod(0)
        self.addCleanup(self.path.chmod, 0o600)
        out = self.check()
        self.assertEqual((out["status"], out["exit"]), ("unreadable", C.EXIT_UNREADABLE))

    def test_future_dated_beyond_skew_is_malformed_never_fresh(self):
        self.put(sample())
        out = self.check(now=SAMPLE_NOW - C.FUTURE_SKEW_SECS - 1)
        self.assertEqual((out["status"], out["exit"]), ("malformed", C.EXIT_MALFORMED))

    def test_future_dated_within_skew_is_fresh_with_zero_age(self):
        self.put(sample())
        out = self.check(now=SAMPLE_NOW - C.FUTURE_SKEW_SECS)
        self.assertEqual((out["status"], out["age_secs"]), ("fresh", 0))

    def test_deeply_nested_json_is_malformed_not_a_crash(self):
        self.put("[" * 200_000 + "]" * 200_000)       # json raises RecursionError, not ValueError
        out = self.check()
        self.assertEqual((out["status"], out["exit"]), ("malformed", C.EXIT_MALFORMED))

    def test_check_doc_refuses_a_non_finite_clock(self):
        for now in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(now=now), self.assertRaises(ValueError):
                C.check_doc(sample(), 180, now)

    def test_oversized_file_is_malformed(self):
        self.put(" " * (C.MAX_BYTES + 1))
        self.assertEqual(self.check()["status"], "malformed")

    def test_malformed_shapes(self):
        base = sample()

        def variant(fn):
            doc = json.loads(json.dumps(base))
            fn(doc)
            return doc

        cases = {
            "not json": "{nope",
            "a list": [],
            "a string": "\"x\"",
            "wrong schema": variant(lambda d: d.update(schema="pantheon-fleet-health/2")),
            "wrong schema_version": variant(lambda d: d.update(schema_version=2)),
            "collected_at missing": variant(lambda d: d.pop("collected_at")),
            "collected_at zero (sanitizer fallback)": variant(lambda d: d.update(collected_at=0)),
            "collected_at bool": variant(lambda d: d.update(collected_at=True)),
            "collected_at string": variant(lambda d: d.update(collected_at=str(SAMPLE_NOW))),
            "collector not a mapping": variant(lambda d: d.update(collector=[])),
            "collector id": variant(lambda d: d["collector"].update(component_id="other")),
            "collector mode": variant(lambda d: d["collector"].update(mode="cron")),
            "entry_count mismatch": variant(lambda d: d["collector"].update(entry_count=99)),
            "entries not a list": variant(lambda d: d.update(entries={})),
            "entry not a mapping": variant(lambda d: d["entries"].append("x")),
        }
        for name, doc in cases.items():
            with self.subTest(name):
                self.put(doc)
                out = self.check()
                self.assertEqual((out["status"], out["exit"]), ("malformed", C.EXIT_MALFORMED), out["detail"])

    def test_check_doc_is_the_same_verdict_on_a_parsed_document(self):
        doc = sample()
        self.assertEqual(C.check_doc(doc, 180, SAMPLE_NOW + 30)["status"], "fresh")
        self.assertEqual(C.check_doc(doc, 180, SAMPLE_NOW + 999)["status"], "stale")
        self.assertEqual(C.check_doc({"schema": "x"}, 180, SAMPLE_NOW)["status"], "malformed")


class DifferentialTests(unittest.TestCase):
    """The reader's well-formedness rule against the collector's OWN schema gate, sanitize_doc:
    two independent implementations. A shape the reader accepts is a sanitize_doc fixed point;
    every shape mutation the reader refuses is one sanitize_doc rewrites, with ONE deliberate
    exception (STRICTER): collected_at 0 is sanitize_doc's own "no valid time" fallback, so it is
    a fixed point there, but a reader must never call an undated aggregate fresh."""

    STRICTER = frozenset({"collected_at zero"})

    def _mutations(self):
        base = sample()

        def variant(fn):
            doc = json.loads(json.dumps(base))
            fn(doc)
            return doc

        return {
            "sample": base,
            "no entries": variant(lambda d: (d.update(entries=[]), d["collector"].update(entry_count=0, read_ok_count=0))),
            "poll mode": variant(lambda d: d["collector"].update(mode="poll")),
            "startup mode": variant(lambda d: d["collector"].update(mode="startup")),
            "wrong schema": variant(lambda d: d.update(schema="pantheon-fleet-health/2")),
            "wrong schema_version": variant(lambda d: d.update(schema_version=2)),
            "collected_at zero": variant(lambda d: d.update(collected_at=0)),
            "collected_at string": variant(lambda d: d.update(collected_at="1")),
            "collector id": variant(lambda d: d["collector"].update(component_id="other")),
            "collector mode": variant(lambda d: d["collector"].update(mode="cron")),
            "entry_count mismatch": variant(lambda d: d["collector"].update(entry_count=99)),
            "entry not a mapping": variant(lambda d: d["entries"].append("x")),
        }

    def test_reader_accepts_exactly_the_sanitize_doc_fixed_points(self):
        for name, doc in self._mutations().items():
            with self.subTest(name):
                accepted = C.check_doc(doc, 10 ** 9, SAMPLE_NOW)["status"] == "fresh"
                fixed_point = hc.sanitize_doc(doc) == doc
                if accepted:
                    self.assertTrue(fixed_point, "the reader accepted a shape the collector would rewrite")
                elif name in self.STRICTER:
                    self.assertTrue(fixed_point, "a STRICTER entry must be a sanitize_doc fixed point")
                else:
                    self.assertFalse(fixed_point, "the reader refused a shape the collector writes")

    def test_constants_agree_with_the_collector(self):
        self.assertEqual(C.SCHEMA, hc.SCHEMA)
        self.assertEqual(C.SCHEMA_VERSION, hc.SCHEMA_VERSION)
        self.assertEqual(C.COLLECTOR_ID, hc.COLLECTOR_ID)
        self.assertEqual(C.MODES, hc.MODES)
        self.assertEqual(C.DEFAULT_PATH, str(hc.DEFAULT_OUT))


class CliTests(_TmpCase):
    def run_cli(self, *args):
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=60)
        return proc

    def test_one_json_line_and_exit_code_match(self):
        self.put(sample())
        for now, want in ((SAMPLE_NOW + 5, 0), (SAMPLE_NOW + 10_000, 10)):
            with self.subTest(now=now):
                proc = self.run_cli("--path", str(self.path), "--now", str(now))
                lines = proc.stdout.splitlines()
                self.assertEqual(len(lines), 1, proc.stdout)
                out = json.loads(lines[0])
                self.assertEqual(proc.returncode, want)
                self.assertEqual(out["exit"], want)
                self.assertEqual(sorted(out), sorted(C.OUTPUT_KEYS))

    def test_missing_path_exit_11(self):
        proc = self.run_cli("--path", str(self.dir / "absent.json"), "--now", str(SAMPLE_NOW))
        self.assertEqual(proc.returncode, C.EXIT_MISSING)

    def assert_one_line(self, proc, status, rc):
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, proc.stdout)
        out = json.loads(lines[0])
        self.assertEqual((proc.returncode, out["exit"], out["status"]), (rc, rc, status))
        self.assertEqual(sorted(out), sorted(C.OUTPUT_KEYS))

    def test_usage_errors_exit_2_with_one_json_line(self):
        for args in (("--max-age-secs", "0"), ("--max-age-secs", "-5"), ("--max-age-secs", "x"), ("--bogus",)):
            with self.subTest(args=args):
                self.assert_one_line(self.run_cli("--path", str(self.path), *args), "usage", C.EXIT_USAGE)

    def test_non_finite_now_is_a_usage_error(self):
        self.put(sample())
        for now in ("nan", "inf", "-inf", "NaN", "infinity"):
            with self.subTest(now=now):
                self.assert_one_line(self.run_cli("--path", str(self.path), "--now", now), "usage", C.EXIT_USAGE)

    def test_deeply_nested_json_cli_is_malformed_one_line(self):
        self.put("[" * 200_000 + "]" * 200_000)
        self.assert_one_line(self.run_cli("--path", str(self.path), "--now", str(SAMPLE_NOW)), "malformed", C.EXIT_MALFORMED)

    def test_help_exits_0(self):
        self.assertEqual(self.run_cli("--help").returncode, 0)

    def test_default_threshold_is_three_collector_ticks(self):
        self.assertEqual(C.DEFAULT_MAX_AGE_SECS, 180)


class ContractTests(unittest.TestCase):
    def test_stdlib_only_no_tools_imports(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                self.assertFalse(name == "tools" or name.startswith("tools."), name)
                self.assertIn(name.split(".")[0], sys.stdlib_module_names | {"__future__"}, name)

    def test_writes_nothing(self):
        src = SCRIPT.read_text(encoding="utf-8")
        for needle in ("write_text", "open(", ".write(", "os.replace", "unlink"):
            if needle == "open(":
                self.assertNotIn('"w', src)
                continue
            self.assertNotIn(needle, src)


if __name__ == "__main__":
    unittest.main()
