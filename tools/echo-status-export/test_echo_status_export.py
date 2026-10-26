"""Tests for echo_status_export (T-4420). Run: python3 -m unittest test_echo_status_export
from this directory, or pytest tools/ from the repo root."""

import contextlib
import io
import json
import os
import pwd
import socket
import stat
import struct
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import echo_status_export as m  # noqa: E402


SAMPLE_STATUS = {
    "started_at": "2026-09-30T10:50:49.310858480Z",
    "active_speaker": {"speaker_id": "alice", "tier": "admin", "since": "2026-09-30T10:55:00Z"},
    "broker": {"connected": True, "host": "127.0.0.1", "port": 1883, "last_reconnect_at": None},
    "recent_activity": {"utterances_last_5m": 2, "last_utterance_at": "2026-09-30T10:55:00Z"},
    "subsystems": [{"name": "stt-relay", "policy": "escalate_to_exit", "restart_attempts": 0,
                    "last_restart_at": None, "healthy": True}],
    "metrics": {"identity_warmup": {"kitchen-0f3e2a91": {"locked": True}}},
    "tool_policy": {"drift_held": [{"tool": "ha:HassTurnOn", "rule": "no_baseline", "pointer": ""}]},
}


def _lat(p50, p95, n):
    return {"p50_ms": p50, "p95_ms": p95, "sample_count": n}


# Every StatusSnapshot field group, shaped per echo-prismatic origin/main
# crates/echo/src/status.rs (StatusSnapshot, SubsystemEntry, BrokerStatus,
# ActiveSpeakerEntry, RecentActivity, PipelineStageEntry, MetricsRollup,
# LatencyStats, IdentityWarmup, ToolPolicyHealth, DriftHeldTool).
REALISTIC_STATUS = {
    "started_at": "2026-09-30T10:50:49.310858480Z",
    "subsystems": [
        {"name": "stt-relay", "policy": "escalate_to_exit", "restart_attempts": 0,
         "last_restart_at": None, "healthy": True},
        {"name": "j17-bridge", "policy": "restart_with_backoff", "restart_attempts": 3,
         "last_restart_at": "2026-09-30T11:02:13.004211Z", "healthy": False},
        {"name": "ha-mcp-client", "policy": "restart_with_backoff", "restart_attempts": 1,
         "last_restart_at": "2026-09-30T10:58:00Z", "healthy": True},
    ],
    "broker": {"connected": True, "host": "127.0.0.1", "port": 1883,
               "last_reconnect_at": "2026-09-30T10:51:00.123Z"},
    "active_speaker": {"speaker_id": "spk_0f3e2a91", "tier": "admin", "since": "2026-09-30T10:55:00Z"},
    "recent_activity": {"utterances_last_5m": 4, "last_utterance_at": "2026-09-30T11:04:59.999999Z"},
    "pipeline": [
        {"stage": "vad", "provider": "silero", "state": "running"},
        {"stage": "stt", "provider": "speechmatics", "state": "running", "operating_point": "enhanced"},
        {"stage": "speaker_id", "provider": "speechmatics", "state": "degraded"},
        {"stage": "classification", "provider": "claude", "state": "running"},
    ],
    "metrics": {
        "stage_latency": {
            "identified_to_authorized": _lat(3, 9, 200),
            "authorized_to_classified": _lat(812, 2240, 200),
            "classified_to_executed": _lat(140, 610, 187),
            "executed_to_terminal": _lat(None, None, 0),
        },
        "classify_latency_by_model": {
            "claude-sonnet-4-5-20250929": _lat(1204, 2811, 120),
            "claude-haiku-4-5-20251001": _lat(512, 998, 80),
            "claude-opus-5-5": _lat(2004, 4100, 3),
        },
        "perceived_latency": _lat(1420, 3050, 57),
        "resolution_counts": {"COMPLETED": 91, "DROPPED": 7, "FAILED": 2},
        "dropped_digested_ambient_count": 5,
        "guest_count": 3,
        "terminal_count": 100,
        "identity_warmup": {
            "kitchen-0f3e2a91": {"locked": True, "utterances_since_start": 0, "utterances_to_lock": 2,
                                 "ms_to_lock": 5310, "first_speech_start_at": "2026-09-30T10:52:01.5Z"},
            "livingroom-7c1d44e0": {"locked": False, "utterances_since_start": 1, "utterances_to_lock": None,
                                    "ms_to_lock": None, "first_speech_start_at": None},
        },
        "self_echo_suppressed_count": 12,
        "speaker_id_flip_count": 1,
        "address_signal_shadow_disagreement_count": 0,
        "stt_reconnect_count": 2,
        "stt_label_covered_at_flush_count": 40,
        "stt_label_covered_in_wait_count": 11,
        "stt_label_empty_uncovered_count": 1,
        "stt_label_empty_mixed_count": 0,
        "stt_label_empty_none_count": 2,
        "stt_label_empty_close_flush_count": 0,
        "stt_label_final_count": 54,
        "stt_label_held_count": 9,
        "stt_label_cut_by_next_flush_count": 1,
        "stt_label_hold_total_ms": 4410,
        "stt_label_hold_max_ms": 980,
        "enrollment_offered_count": 1,
        "enrollment_consent_captured_count": 1,
        "enrollment_consent_rejected_count": 0,
    },
    "tool_policy": {
        "drift_held": [
            {"tool": "ha:HassLightSet", "rule": "range_widened",
             "pointer": "/properties/brightness_pct/anyOf/1/maximum"},
            {"tool": "ha:HassTurnOn", "rule": "enum_widened", "pointer": "/properties/domain/items/enum"},
            {"tool": "ha:HassMediaPlayerUnmute", "rule": "no_baseline", "pointer": ""},
            {"tool": "ha:HassClimateSetTemperature", "rule": None, "pointer": None},
        ],
        "never_assessed_count": 2,
        "updated_at": "2026-09-30T11:00:00Z",
    },
}


class TestSanitizer(unittest.TestCase):
    def test_denied_keys_dropped_anywhere(self):
        data = {"a": {"transcript": "turn on the lights", "ok": 1},
                "list": [{"api_key": "x", "n": 2}], "Password": "hunter2"}
        dropped = []
        out = m.sanitize(data, dropped)
        self.assertEqual(out, {"a": {"ok": 1}, "list": [{"n": 2}]})
        self.assertEqual(sorted(dropped), ["Password", "a.transcript", "list[0].api_key"])

    def test_speaker_id_path_dropped_tier_kept(self):
        dropped = []
        out = m.sanitize(SAMPLE_STATUS, dropped)
        self.assertEqual(out["active_speaker"], {"tier": "admin", "since": "2026-09-30T10:55:00Z"})
        self.assertEqual(dropped, ["active_speaker.speaker_id"])
        self.assertEqual(out["broker"], SAMPLE_STATUS["broker"])
        self.assertEqual(out["tool_policy"], SAMPLE_STATUS["tool_policy"])

    def test_high_confidence_patterns_refuse_all(self):
        for v in (
            "sk-ant-api03-abcdefghijklmnopqrstuv",
            "Bearer abcdefghijklmnop",
            "mqtt://user:pw@host:1883",
            "-----BEGIN RSA PRIVATE KEY-----",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig",
        ):
            with self.subTest(v=v):
                with self.assertRaises(m.SecretSuspected) as cm:
                    m.sanitize({"x": {"y": [v]}})
                self.assertEqual(cm.exception.path, "x.y[0]")
                self.assertNotIn(v, str(cm.exception))

    def test_lower_confidence_values_redacted_per_value(self):
        for v in (
            "https://x/y?token=abc",
            "Zq8XvT2mB7nK4pR9wL3yH6cF1dS5gJ0aQeUiOtYz",
            "ghp_abcdefghijklmnopqrstuvwxyz",
        ):
            with self.subTest(v=v):
                redacted = []
                out = m.sanitize({"x": {"y": [v, "fine"]}, "z": 1}, redacted=redacted)
                self.assertEqual(out, {"x": {"y": [m.REDACTED, "fine"]}, "z": 1})
                self.assertEqual(redacted, ["x.y[0]"])

    def test_secret_like_key_dropped_and_recorded(self):
        redacted = []
        out = m.sanitize({"a": {"Zq8XvT2mB7nK4pR9wL3yH6cF1dS5gJ0aQeUiOtYz": 1, "b": 2}}, redacted=redacted)
        self.assertEqual(out, {"a": {"b": 2}})
        self.assertEqual(redacted, ["a.<key>"])

    def test_benign_values_pass(self):
        for v in ("2026-09-30T10:50:49.310858480Z", "0f3e2a91-1b2c-4d5e-8f90-a1b2c3d4e5f6",
                  "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                  "restart_with_backoff", "ha:HassMediaPlayerUnmute",
                  "/properties/brightness_pct/anyOf/1/maximum",
                  "/properties/SomeLongerFieldName2/anyOf/10/items/properties/Xy9/maximum",
                  "claude-sonnet-4-5-20250929", "stt_label_empty_close_flush_count"):
            with self.subTest(v=v):
                self.assertFalse(m.looks_secret(v))

    def test_realistic_snapshot_exports_with_zero_redactions(self):
        rec, ok = m.build_status_record({}, lambda e: {"ok": True, "data": REALISTIC_STATUS})
        self.assertTrue(ok, rec.get("error"))
        self.assertEqual(rec["sanitizer"], {"dropped_paths": ["active_speaker.speaker_id"], "redacted_paths": []})
        expected = json.loads(json.dumps(REALISTIC_STATUS))
        del expected["active_speaker"]["speaker_id"]
        self.assertEqual(rec["status"], expected)
        self.assertEqual(rec["status"]["tool_policy"]["drift_held"][0]["pointer"],
                         "/properties/brightness_pct/anyOf/1/maximum")

    def test_sanitizer_refusal_writes_error_not_status(self):
        env = {}
        rec, ok = m.build_status_record(env, lambda e: {"ok": True, "data": {"note": "Bearer abcdefghijklmnop"}})
        self.assertFalse(ok)
        self.assertEqual(rec["error"], "sanitizer refused: credential-like value at note")
        self.assertNotIn("status", rec)
        self.assertNotIn("sanitizer", rec)

    def test_redaction_still_exports_ok(self):
        data = {"subsystems": [{"name": "x", "note": "Zq8XvT2mB7nK4pR9wL3yH6cF1dS5gJ0aQeUiOtYz"}]}
        rec, ok = m.build_status_record({}, lambda e: {"ok": True, "data": data})
        self.assertTrue(ok)
        self.assertEqual(rec["status"]["subsystems"][0]["note"], m.REDACTED)
        self.assertEqual(rec["sanitizer"]["redacted_paths"], ["subsystems[0].note"])
        self.assertNotIn("Zq8XvT2m", json.dumps(rec))


class TestJournalNormalizer(unittest.TestCase):
    def test_strips_dynamic_parts(self):
        line = ("\x1b[33m2026-09-30T10:51:02.123456Z  WARN\x1b[0m echo::capture: capture-watchdog: "
                "stall detected after 1234 ms for 0f3e2a91-1b2c-4d5e-8f90-a1b2c3d4e5f6, text: \"turn on the lights\"")
        level, target, sig = m.parse_log_line(line)
        self.assertEqual((level, target), ("WARN", "echo::capture"))
        self.assertEqual(sig, "capture-watchdog: stall detected after <n> ms for <uuid>")
        self.assertNotIn("lights", sig)

    def test_same_template_same_signature(self):
        a = m.parse_log_line('2026-09-30T10:00:00Z ERROR echo::j17: j17-bridge: 3 cumulative append failures: io "x"')
        b = m.parse_log_line("2026-09-30T11:59:59Z ERROR echo::j17: j17-bridge: 41 cumulative append failures: timeout")
        self.assertEqual(a, b)
        self.assertEqual(a[2], "j17-bridge: <n> cumulative append failures")

    def test_quoted_and_kv_text_cut(self):
        # " utterance=" has the shape of a trailing tracing field, so the cut is before its key.
        _, _, sig = m.parse_log_line("WARN echo::x: heard utterance='play some jazz' id=7")
        self.assertEqual(sig, "heard")
        _, _, sig = m.parse_log_line("WARN echo::x: heard utterance 'play some jazz' id=7")
        self.assertEqual(sig, "heard utterance")
        _, _, sig = m.parse_log_line('WARN echo::x: rejected "please unlock the door"')
        self.assertEqual(sig, "rejected")

    def test_json_format_and_info_ignored(self):
        j = json.dumps({"level": "ERROR", "target": "echo::config_control",
                        "fields": {"message": "config-control: failed to publish reply to echo/config/reply",
                                   "error": "secret stuff"}})
        self.assertEqual(m.parse_log_line(j), ("ERROR", "echo::config_control",
                                               "config-control: failed to publish reply to echo/config/reply"))
        self.assertIsNone(m.parse_log_line(json.dumps({"level": "INFO", "target": "t", "fields": {"message": "m"}})))
        self.assertIsNone(m.parse_log_line("2026-09-30T10:00:00Z  INFO echo::x: all good"))

    def test_em_dash_and_identifiers_cut(self):
        _, _, sig = m.parse_log_line(
            "2026-09-30T10:00:00Z ERROR echo::ha: ha-mcp: upstream 500 \u2014 Entity light.bedroom_lamp not found")
        self.assertEqual(sig, "ha-mcp: upstream <n>")
        self.assertNotIn("bedroom", sig)
        _, _, sig = m.parse_log_line("WARN echo::ha: ha-mcp: entity light.bedroom_lamp unavailable")
        self.assertEqual(sig, "ha-mcp: entity <id> unavailable")
        self.assertNotIn("bedroom", sig)

    def test_level_anchored_to_header(self):
        self.assertIsNone(m.parse_log_line(
            "2026-09-30T10:00:00Z  INFO echo::x: replayed ERROR counters from the last run"))
        self.assertIsNone(m.parse_log_line("\x1b[32m INFO\x1b[0m echo::x: saw WARN from upstream"))
        self.assertEqual(m.parse_log_line("\x1b[2m2026-09-30T10:00:00Z\x1b[0m \x1b[31mERROR\x1b[0m echo::x: bad")[:2],
                         ("ERROR", "echo::x"))
        sigs = m.summarize_journal([
            {"MESSAGE": "2026-09-30T10:00:00Z  INFO echo::x: the ERROR budget is fine", "PRIORITY": "6"},
            {"MESSAGE": json.dumps({"level": "INFO", "target": "t", "fields": {"message": "ERROR in text"}}),
             "PRIORITY": "6"},
        ])
        self.assertEqual(sigs, [])

    def test_pretty_continuation_lines_not_parsed(self):
        # Pretty format prints field values on their own lines; a value that
        # starts with a non-timestamp token and then WARN/ERROR is not a header.
        for line in ("Status: ERROR upstream reports the operator away from home",
                     "body: WARN the operator bedroom door open",
                     "    \x1b[3mStatus\x1b[0m: ERROR upstream reports the operator away from home",
                     "at: ERROR x",
                     "10:00:00 WARN not an RFC 3339 timestamp"):
            with self.subTest(line=line):
                self.assertIsNone(m.parse_log_line(line))
        self.assertEqual(m.summarize_journal([
            {"MESSAGE": "Status: ERROR upstream reports the operator away from home", "PRIORITY": "6"},
            {"MESSAGE": "body: WARN the operator bedroom door open", "PRIORITY": "6"},
        ]), [])

    def test_real_header_lines_still_parse(self):
        # Full format, no ANSI.
        self.assertEqual(m.parse_log_line("2026-09-30T10:00:00.123456Z  WARN echo::x: vad stop error: io")[:2],
                         ("WARN", "echo::x"))
        # Full format without a timestamp (level first).
        self.assertEqual(m.parse_log_line("ERROR echo::x: vad stop error: io")[:2], ("ERROR", "echo::x"))
        # Pretty format header with ANSI (tracing-subscriber Pretty: indented,
        # dimmed timestamp, bold coloured padded level, bold target).
        pretty = ("  \x1b[2m2026-09-30T10:00:00.123456Z\x1b[0m \x1b[1;33m WARN\x1b[0m "
                  "\x1b[1;33mecho::lifecycle\x1b[0m\x1b[33m: \x1b[0m\x1b[33mvad stop error: io\x1b[0m"
                  "\x1b[33m, \x1b[0m\x1b[1;3;33mconv_id\x1b[0m\x1b[33m: c1\x1b[0m")
        self.assertEqual(m.parse_log_line(pretty), ("WARN", "echo::lifecycle", "vad stop error"))
        pretty_err = ("  \x1b[2m2026-09-30T10:00:00.123456+00:00\x1b[0m \x1b[1;31mERROR\x1b[0m "
                      "\x1b[1;31mecho::lifecycle\x1b[0m\x1b[31m: \x1b[0m\x1b[31mvad stop error: io\x1b[0m")
        self.assertEqual(m.parse_log_line(pretty_err), ("ERROR", "echo::lifecycle", "vad stop error"))

    def test_span_prefix_and_panic_and_priority(self):
        self.assertEqual(m.parse_log_line("WARN run{ctx=kitchen}: echo::lifecycle: subsystem failed")[1],
                         "echo::lifecycle")
        self.assertEqual(m.parse_log_line("thread 'main' panicked at crates/echo/src/x.rs:12:5:"),
                         ("PANIC", "rust", "panicked at crates/echo/src/x.rs"))
        self.assertEqual(m.parse_log_line("plain stderr failure 42", priority=3), ("ERROR", "?", "plain stderr failure <n>"))

    def test_summarize_counts_and_seen(self):
        entries = [
            {"MESSAGE": "ERROR echo::j17: j17-bridge: 3 cumulative append failures", "__REALTIME_TIMESTAMP": "1790000000000000"},
            {"MESSAGE": "INFO echo::x: fine", "__REALTIME_TIMESTAMP": "1790000001000000"},
            {"MESSAGE": "ERROR echo::j17: j17-bridge: 9 cumulative append failures", "__REALTIME_TIMESTAMP": "1790000100000000"},
            {"MESSAGE": "WARN echo::a: b", "__REALTIME_TIMESTAMP": "1790000050000000"},
        ]
        sigs = m.summarize_journal(entries)
        self.assertEqual(len(sigs), 2)
        self.assertEqual(sigs[0]["count"], 2)
        self.assertEqual(sigs[0]["first_seen"], "2026-09-21T14:13:20Z")
        self.assertEqual(sigs[0]["last_seen"], "2026-09-21T14:15:00Z")

    INV = "d09da5837efb4590841a8f94a3263512"
    WIN = {"invocation_id": INV, "active_state": "active"}

    def test_journal_cmd_filters_server_side(self):
        cmd = m.journal_cmd("echo.service", "user", self.WIN)
        self.assertIn("--grep=WARN|ERROR|panicked", cmd)
        self.assertIn("--output-fields=MESSAGE,PRIORITY,__REALTIME_TIMESTAMP", cmd)
        self.assertEqual(cmd[:2], ["journalctl", "--user"])
        self.assertNotIn("--user", m.journal_cmd("echo.service", "system", self.WIN))

    def test_journal_cmd_scoped_to_current_run_never_boot(self):
        since = {"since": "@1790000000", "active_state": "inactive"}
        for scope in ("user", "system"):
            inv_cmd = m.journal_cmd("echo.service", scope, self.WIN)
            self.assertIn(f"_SYSTEMD_INVOCATION_ID={self.INV}", inv_cmd)
            self.assertFalse(any(a.startswith("--since") for a in inv_cmd))
            since_cmd = m.journal_cmd("echo.service", scope, since)
            self.assertIn("--since=@1790000000", since_cmd)
            self.assertFalse(any("INVOCATION" in a for a in since_cmd))
            for cmd in (inv_cmd, since_cmd):
                self.assertNotIn("-b", cmd)
                self.assertFalse(any(a.startswith("--boot") for a in cmd))
        for bad in ({}, {"since": "Wed 2026-09-30 11:54:03 UTC"}, {"since": "@"},
                    {"invocation_id": "not-an-id"}, {"active_state": "active"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                m.journal_cmd("echo.service", "user", bad)

    @staticmethod
    def _show(props):
        calls = []

        def show(unit, scope):
            calls.append((unit, scope))
            if isinstance(props, BaseException):
                raise props
            return dict(props)
        return show, calls

    def test_journal_window_prefers_invocation_id(self):
        for state in ("active", "inactive", "failed"):
            with self.subTest(state=state):
                show, calls = self._show({"InvocationID": self.INV, "ActiveState": state,
                                          "ActiveEnterTimestamp": "@1790000000"})
                self.assertEqual(m.journal_window("echo.service", "user", show),
                                 {"invocation_id": self.INV, "active_state": state})
                # One systemctl call covers every property.
                self.assertEqual(calls, [("echo.service", "user")])

    def test_journal_window_since_fallback_unix_epoch(self):
        for inv in ("", "not-an-id", None):
            with self.subTest(inv=inv):
                props = {"ActiveState": "active", "ActiveEnterTimestamp": "@1790000000"}
                if inv is not None:
                    props["InvocationID"] = inv
                show, _ = self._show(props)
                self.assertEqual(m.journal_window("echo.service", "user", show),
                                 {"since": "@1790000000", "active_state": "active"})

    def test_journal_window_active_state_sanitized(self):
        for state, want in (("", "unknown"), ("ACTIVE; rm", "unknown"), ("x" * 40, "unknown"),
                            ("deactivating", "deactivating")):
            with self.subTest(state=state):
                show, _ = self._show({"InvocationID": self.INV, "ActiveState": state})
                self.assertEqual(m.journal_window("echo.service", "user", show)["active_state"], want)

    def test_journal_window_unit_never_started(self):
        # A non-@epoch timestamp (localized text, n/a, empty, an option-like
        # value) is treated as unavailable.
        for ts in ("", "n/a", "--boot", "Wed 2026-09-30 11:54:03 UTC", "@", "@12x", "@1790000000 junk"):
            with self.subTest(ts=ts):
                show, _ = self._show({"InvocationID": "", "ActiveState": "inactive",
                                      "ActiveEnterTimestamp": ts})
                with self.assertRaises(m.UnitNeverStarted) as cm:
                    m.journal_window("echo.service", "user", show)
                self.assertEqual(str(cm.exception),
                                 "unit never started (or wrong ECHO_JOURNAL_UNIT/SCOPE)")

    def test_journal_window_passes_systemctl_failures_through(self):
        for exc in (RuntimeError("systemctl show exited 1"),
                    m.subprocess.TimeoutExpired(["systemctl"], 5), OSError("no systemctl")):
            with self.subTest(exc=exc):
                show, _ = self._show(exc)
                with self.assertRaises(type(exc)):
                    m.journal_window("echo.service", "user", show)

    def test_systemctl_show_single_call_parses_key_value(self):
        seen = []

        def fake_run(cmd, **kw):
            seen.append((cmd, kw.get("timeout")))
            out = (f"InvocationID={self.INV}\nActiveState=inactive\n"
                   "ActiveEnterTimestamp=@1790000000\nnoise line\n")
            return m.subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        with mock.patch.object(m.subprocess, "run", fake_run):
            got = m._systemctl_show("echo.service", "user")
            self.assertEqual(got, {"InvocationID": self.INV, "ActiveState": "inactive",
                                   "ActiveEnterTimestamp": "@1790000000"})
            self.assertEqual(len(seen), 1)
            cmd, timeout = seen[0]
            self.assertEqual(cmd[:4], ["systemctl", "--user", "show", "--timestamp=unix"])
            for prop in ("InvocationID", "ActiveState", "ActiveEnterTimestamp"):
                self.assertIn(prop, cmd[cmd.index("-p", 0):])
            self.assertEqual(cmd.count("-p"), 3)
            self.assertNotIn("--value", cmd)
            self.assertEqual(cmd[-2:], ["--", "echo.service"])
            self.assertEqual(timeout, m.SYSTEMCTL_TIMEOUT_S)
            m._systemctl_show("echo.service", "system")
            self.assertNotIn("--user", seen[-1][0])
        with mock.patch.object(m.subprocess, "run",
                               lambda cmd, **kw: m.subprocess.CompletedProcess(cmd, 1, "", "")):
            with self.assertRaises(RuntimeError):
                m._systemctl_show("echo.service", "user")

    def _export(self, d, reader, window_fn):
        return m.export_journal({"ECHO_EXPORT_DIR": d, "ECHO_EXPORT_GROUP": ""}, reader, window_fn)

    def test_export_journal_records_window(self):
        seen = {}

        def reader(unit, scope, window):
            seen["window"] = window
            return iter([{"MESSAGE": "ERROR echo::x: boom"}])
        with tempfile.TemporaryDirectory() as d:
            for win in ({"invocation_id": self.INV, "active_state": "failed"},
                        {"since": "@1790000000", "active_state": "active"}):
                rec = self._export(d, reader, lambda u, s, w=win: w)
                self.assertEqual(rec["window"], win)
                self.assertEqual(seen["window"], win)
                self.assertNotIn("boot", rec)
                with open(os.path.join(d, m.JOURNAL_FILE)) as f:
                    self.assertEqual(json.load(f)["window"], win)

    def test_export_journal_unit_never_started_and_systemctl_failures(self):
        def reader(unit, scope, window):
            raise AssertionError("must not scan without a window")
        never = "unit never started (or wrong ECHO_JOURNAL_UNIT/SCOPE)"
        cases = [
            (m.UnitNeverStarted(never), never),
            (RuntimeError("systemctl show exited 1"), "systemctl: systemctl show exited 1"),
            (m.subprocess.TimeoutExpired(["systemctl"], 5), "systemctl: TimeoutExpired after 5s"),
            (FileNotFoundError("systemctl"), "systemctl: FileNotFoundError"),
            (KeyError("x"), "exporter error (KeyError)"),
        ]
        with tempfile.TemporaryDirectory() as d:
            for exc, want in cases:
                with self.subTest(want=want):
                    def window_fn(unit, scope, exc=exc):
                        raise exc
                    rec = self._export(d, reader, window_fn)
                    self.assertEqual(rec["error"], want)
                    self.assertNotIn("window", rec)
                    with open(os.path.join(d, m.JOURNAL_FILE)) as f:
                        on_disk = json.load(f)
                    self.assertEqual(on_disk["error"], want)
                    self.assertIn("exported_at", on_disk)

    def test_export_journal_end_to_end_via_real_journal_window(self):
        # journal_window -> journal_cmd with systemctl and journalctl both faked.
        # A stopped unit: systemd has no id here, so the @epoch fallback is used.
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            out = "InvocationID=\nActiveState=inactive\nActiveEnterTimestamp=@1790000000\n"
            return m.subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        built = {}
        real_cmd = m.journal_cmd

        def spy_cmd(unit, scope, window):
            built["cmd"] = real_cmd(unit, scope, window)
            return [sys.executable, "-c", "import sys; sys.exit(1)"]
        with tempfile.TemporaryDirectory() as d, mock.patch.object(m.subprocess, "run", fake_run), \
                mock.patch.object(m, "journal_cmd", spy_cmd):
            rec = m.export_journal({"ECHO_EXPORT_DIR": d, "ECHO_EXPORT_GROUP": ""})
        self.assertEqual(len(calls), 1)
        self.assertEqual(rec["window"], {"since": "@1790000000", "active_state": "inactive"})
        self.assertEqual(rec["entries_matched"], 0)
        self.assertIn("--since=@1790000000", built["cmd"])
        self.assertNotIn("-b", built["cmd"])

    def test_signatures_capped_with_truncated_count(self):
        entries = [{"MESSAGE": f"ERROR echo::x: failure kind {chr(65 + i % 26)}{chr(65 + i // 26)}"}
                   for i in range(250)]
        with tempfile.TemporaryDirectory() as d:
            rec = self._export(d, lambda u, s, w: iter(entries), lambda u, s: self.WIN)
        self.assertEqual(len(rec["signatures"]), m.MAX_SIGNATURES)
        self.assertEqual(rec["truncated"], 50)
        self.assertEqual(rec["totals"]["ERROR"], 250)
        self.assertEqual(rec["entries_matched"], 250)

    def test_read_journal_streams_and_handles_no_match_rc(self):
        script = ("import sys,json\nfor i in range(3): print(json.dumps({'MESSAGE': 'ERROR t: m %d' % i}))\n"
                  "print('not json')\nsys.exit(0)\n")
        w = self.WIN
        with mock.patch.object(m, "journal_cmd", return_value=[sys.executable, "-c", script]):
            self.assertEqual(len(list(m.read_journal("u", "user", w))), 3)
        with mock.patch.object(m, "journal_cmd", return_value=[sys.executable, "-c", "import sys; sys.exit(1)"]):
            self.assertEqual(list(m.read_journal("u", "user", w)), [])
        with mock.patch.object(m, "journal_cmd", return_value=[sys.executable, "-c", "import sys; sys.exit(2)"]):
            with self.assertRaises(RuntimeError):
                list(m.read_journal("u", "user", w))
        with mock.patch.object(m, "journal_cmd",
                               return_value=[sys.executable, "-c", "import time; time.sleep(30)"]):
            with self.assertRaises(m.subprocess.TimeoutExpired):
                list(m.read_journal("u", "user", w, timeout=0.5))

    def test_export_journal_timeout_names_the_subprocess(self):
        # T-4727: a bare "TimeoutExpired" did not say which call ran long.
        def reader(unit, scope, window):
            raise m.subprocess.TimeoutExpired(["journalctl", "--user"], 75)
        with tempfile.TemporaryDirectory() as d:
            rec = self._export(d, reader, lambda u, s: self.WIN)
            self.assertEqual(rec["error"], "journalctl: TimeoutExpired after 75s")

    def test_export_journal_error_path(self):
        with tempfile.TemporaryDirectory() as d:
            def boom(unit, scope, window):
                raise RuntimeError("journalctl exited 1")
            rec = self._export(d, boom, lambda u, s: self.WIN)
            self.assertEqual(rec["error"], "journalctl exited 1")
            self.assertEqual(rec["window"], self.WIN)
            with open(os.path.join(d, m.JOURNAL_FILE)) as f:
                self.assertEqual(json.load(f)["error"], "journalctl exited 1")


# Real echo WARN/ERROR call sites (echo-prismatic origin/main), rendered with
# "{e}" standing for the error value, and the signature each must reduce to.
ECHO_CALL_SITES = [
    # enrollment_orchestrator.rs:2636 error!(conv_id, "profile persist failed: {e}")
    ("echo_adapter_speechmatics::enrollment_orchestrator", "profile persist failed: {e}",
     "profile persist failed"),
    # echo-adapter-mqtt-audio-source lib.rs:164
    ("echo_adapter_mqtt_audio_source", "mqtt audio source: resubscribe after broker reconnect failed: {e}",
     "mqtt audio source: resubscribe after broker reconnect failed"),
    # enrollment_orchestrator.rs:1224 warn!(conv_id, "enrollment sweep: CSM query failed ({e}) — kept")
    ("echo_adapter_speechmatics::enrollment_orchestrator", "enrollment sweep: CSM query failed ({e}) — kept",
     "enrollment sweep: CSM query failed"),
    # echo-j17-bridge consumer.rs:207 ("{}" is the failure count)
    ("echo_j17_bridge::consumer", "j17-bridge: 41 cumulative append failures — j17 may be unavailable",
     "j17-bridge: <n> cumulative append failures"),
    # echo-adapter-claude classify.rs:762
    ("echo_adapter_claude::classify", "claude.classify: GetLiveContext executor error: {e}",
     "claude.classify: GetLiveContext executor error"),
    # echo-core audio_locator/disk.rs:104
    ("echo_core::audio_locator::disk", "DiskBackend sweep error: {e}", "DiskBackend sweep error"),
    # enrollment_orchestrator.rs:2775
    ("echo_adapter_speechmatics::enrollment_orchestrator", "enrollment: audio ref resolve error (skipping): {e}",
     "enrollment: audio ref resolve error"),
    # echo-adapter-silero-vad processor.rs:183
    ("echo_adapter_silero_vad::processor", "silero-vad: inference error on frame — skipping: {e}",
     "silero-vad: inference error on frame"),
    # echo lifecycle.rs:1037
    ("echo::lifecycle", "ha-mcp: startup failed — HA unavailable at launch, will retry on first use: {e}",
     "ha-mcp: startup failed"),
    # echo lifecycle.rs:2191
    ("echo::lifecycle", "vad stop error: {e}", "vad stop error"),
    # echo lifecycle.rs:2364
    ("echo::lifecycle", "profiles-changed: STT restart failed: {e}", "profiles-changed: STT restart failed"),
    # echo-core wrapper.rs:860 (two labels: the second ': ' cap keeps both labels only)
    ("echo_core::wrapper", "reference_wrapper: speechmatics-tts: speak failed: {e}",
     "reference_wrapper: speechmatics-tts"),
    # enrollment_orchestrator.rs:2312 "{what} TTS failed: {e}"
    ("echo_adapter_speechmatics::enrollment_orchestrator", "enrollment offer TTS failed: {e}",
     "enrollment offer TTS failed"),
    # echo-turn-sense observer.rs:109
    ("echo_turn_sense::observer", "turn-sense: inference failed: {e}", "turn-sense: inference failed"),
    # echo-core digest.rs:252 (no error value; static text kept)
    ("echo_core::digest", "digest worker: classify failed; leaving for the reconciler",
     "digest worker: classify failed; leaving for the reconciler"),
    # echo-adapter-claude classify.rs:737 (leading label kept readable, snake_case name -> <id>)
    ("echo_adapter_claude::classify", "claude.classify: execute_get_live_context called without executor",
     "claude.classify: <id> called without executor"),
]

# Error values with user-derived words, ": " separators, parens, quotes, ids.
ERROR_PAYLOADS = [
    "io error: No such file or directory (os error 2)",
    'HTTP 500: {"message":"Entity light.bedroom_lamp not found"}',
    "user said turn on the kitchen lights please: timeout",
    "Alice asked for jazz",
]
PAYLOAD_WORDS = ("directory", "bedroom", "kitchen", "lights", "Alice", "jazz", "timeout", "HTTP")


class TestJournalRealCallSites(unittest.TestCase):
    def _render(self, template, payload):
        return template.replace("{e}", payload)

    def test_text_format_call_sites(self):
        for target, template, expected in ECHO_CALL_SITES:
            for payload in ERROR_PAYLOADS:
                with self.subTest(template=template, payload=payload):
                    line = (f"2026-09-30T10:00:00.123456Z  WARN {target}: "
                            f"{self._render(template, payload)} conv_id=c-{len(payload)}")
                    level, tgt, sig = m.parse_log_line(line)
                    self.assertEqual((level, tgt, sig), ("WARN", target, expected))
                    for w in PAYLOAD_WORDS:
                        self.assertNotIn(w, sig)

    def test_pretty_format_call_sites(self):
        # echo's default LogFormat::Pretty with ANSI: fields render as ", key: value".
        for target, template, expected in ECHO_CALL_SITES:
            for level, colour in (("WARN", "33"), ("ERROR", "31")):
                for payload in ERROR_PAYLOADS:
                    with self.subTest(template=template, level=level, payload=payload):
                        line = (f"  \x1b[2m2026-09-30T10:00:00.123456Z\x1b[0m \x1b[1;{colour}m{level:>5}\x1b[0m "
                                f"\x1b[1;{colour}m{target}\x1b[0m\x1b[{colour}m: \x1b[0m"
                                f"\x1b[{colour}m{self._render(template, payload)}\x1b[0m"
                                f"\x1b[{colour}m, \x1b[0m\x1b[1;3;{colour}mconv_id\x1b[0m\x1b[{colour}m: c1\x1b[0m")
                        self.assertEqual(m.parse_log_line(line), (level, target, expected))
                        sig = m.parse_log_line(line)[2]
                        for w in PAYLOAD_WORDS:
                            self.assertNotIn(w, sig)

    def test_pretty_format_plain_call_sites(self):
        # Pretty without ANSI (NO_COLOR / non-tty journal): same signatures.
        for target, template, expected in ECHO_CALL_SITES:
            for payload in ERROR_PAYLOADS:
                with self.subTest(template=template, payload=payload):
                    line = (f"  2026-09-30T10:00:00.123456Z  WARN {target}: "
                            f"{self._render(template, payload)}, conv_id: c1")
                    self.assertEqual(m.parse_log_line(line), ("WARN", target, expected))

    def test_json_format_call_sites(self):
        for target, template, expected in ECHO_CALL_SITES:
            with self.subTest(template=template):
                j = json.dumps({"level": "ERROR", "target": target,
                                "fields": {"message": self._render(template, ERROR_PAYLOADS[1]), "conv_id": "c"}})
                self.assertEqual(m.parse_log_line(j), ("ERROR", target, expected))

    def test_different_payloads_same_signature(self):
        """Property: a template's signature does not depend on its error value."""
        for target, template, _ in ECHO_CALL_SITES:
            with self.subTest(template=template):
                sigs = {m.normalize_message(self._render(template, p)) for p in ERROR_PAYLOADS}
                self.assertEqual(len(sigs), 1, sigs)

    def test_failure_word_rule(self):
        self.assertEqual(m.normalize_message("classify failed: user said turn on the kitchen lights please"),
                         "classify failed")
        # "failures" is kept as the last static word, the value after it is dropped.
        self.assertEqual(m.normalize_message("j17-bridge: 3 cumulative append failures: io timeout"),
                         "j17-bridge: <n> cumulative append failures")
        # A segment that merely contains a failure word mid-text is not a cut point.
        self.assertEqual(m.normalize_message("failed dial retry scheduled: 5 left"), "failed dial retry scheduled: <n> left")
        self.assertEqual(m.normalize_message("request errored: reset by peer"), "request errored")

    def test_leading_label_kept_entity_ids_still_masked(self):
        self.assertEqual(m.normalize_message("reference_wrapper: stream closed"), "reference_wrapper: stream closed")
        sig = m.normalize_message("ha-mcp: upstream 500 — Entity light.bedroom_lamp not found")
        self.assertEqual(sig, "ha-mcp: upstream <n>")
        self.assertNotIn("bedroom", sig)
        self.assertEqual(m.normalize_message("ha-mcp: entity light.bedroom_lamp unavailable"),
                         "ha-mcp: entity <id> unavailable")
        # An entity id in the leading position is not a label.
        for lead in ("light.bedroom_lamp", "person.alice", "media_player.kitchen_speaker",
                     "group.operators_phones", "sun.sun", "timer.kitchen_timer", "counter.operator_visits",
                     "schedule.operator_workdays", "lawn_mower.back_yard", "tag.operators_keys",
                     "ai_task.operator_summary", "wake_word.hey_operator", "input_boolean.operator_home"):
            with self.subTest(lead=lead):
                sig = m.normalize_message(f"{lead}: unavailable")
                self.assertEqual(sig, "<id>: unavailable")

    def test_group_lead_masks_name(self):
        sig = m.normalize_message("group.operators_phones: stale")
        self.assertEqual(sig, "<id>: stale")
        self.assertNotIn("operator", sig)


class TestVerify(unittest.TestCase):
    me = pwd.getpwuid(os.getuid()).pw_name

    def _export(self, d, fetch=lambda e: {"ok": True, "data": SAMPLE_STATUS}):
        m.export_status({"ECHO_EXPORT_DIR": d, "ECHO_EXPORT_GROUP": ""}, fetch)

    def _sub(self, parent):
        sub = os.path.join(parent, "status")
        self._export(sub)
        return sub

    def test_fresh_trusted_export_ok(self):
        with tempfile.TemporaryDirectory() as p:
            sub = self._sub(p)
            code, reason = m.verify_export(sub, 300, self.me)
            self.assertEqual(code, m.VERIFY_OK, reason)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(m.main(["--verify", "--dir", sub, "--owner", self.me]), 0)
            self.assertIn("ok: fresh", out.getvalue())

    def test_daemon_error_record_is_1(self):
        with tempfile.TemporaryDirectory() as p:
            sub = os.path.join(p, "status")

            def silent(e):
                raise m.NoReply("no reply")
            self._export(sub, silent)
            code, reason = m.verify_export(sub, 300, self.me)
            self.assertEqual(code, m.VERIFY_DAEMON_ERROR)
            self.assertIn("no reply", reason)

    def test_stale_is_alert(self):
        with tempfile.TemporaryDirectory() as p:
            sub = self._sub(p)
            path = os.path.join(sub, m.STATUS_FILE)
            with open(path) as f:
                rec = json.load(f)
            rec["exported_at"] = "2026-01-01T00:00:00Z"
            with open(path, "w") as f:
                json.dump(rec, f)
            code, reason = m.verify_export(sub, 300, self.me)
            self.assertEqual(code, m.VERIFY_ALERT)
            self.assertIn("stale", reason)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m.main(["--verify", "--dir", sub, "--owner", self.me, "--max-age", "1e9"]), 0)

    def test_future_skew_is_alert(self):
        with tempfile.TemporaryDirectory() as p:
            sub = self._sub(p)
            path = os.path.join(sub, m.STATUS_FILE)
            with open(path) as f:
                rec = json.load(f)
            future = m._dt.datetime.now(m._dt.timezone.utc) + m._dt.timedelta(seconds=600)
            rec["exported_at"] = future.strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(path, "w") as f:
                json.dump(rec, f)
            code, reason = m.verify_export(sub, 300, self.me)
            self.assertEqual(code, m.VERIFY_ALERT)
            self.assertIn("in the future", reason)
            # Within the 60 s skew allowance is still ok.
            rec["exported_at"] = (m._dt.datetime.now(m._dt.timezone.utc) + m._dt.timedelta(seconds=30)
                                  ).strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(path, "w") as f:
                json.dump(rec, f)
            self.assertEqual(m.verify_export(sub, 300, self.me)[0], m.VERIFY_OK)

    def test_max_age_rejects_non_finite_or_non_positive(self):
        for bad in ("nan", "inf", "-inf", "-1", "0", "abc"):
            with self.subTest(bad=bad):
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    with self.assertRaises(SystemExit) as cm:
                        m.main(["--verify", "--max-age", bad])
                self.assertEqual(cm.exception.code, 2)
                self.assertIn("--max-age", err.getvalue())
        self.assertEqual(m._positive_seconds("0.5"), 0.5)

    def test_wrong_owner_is_alert(self):
        with tempfile.TemporaryDirectory() as p:
            sub = self._sub(p)
            with mock.patch.object(pwd, "getpwnam", return_value=mock.Mock(pw_uid=os.getuid() + 1)):
                code, reason = m.verify_export(sub, 300, "claude")
            self.assertEqual(code, m.VERIFY_ALERT)
            self.assertIn("expected claude", reason)
            code, reason = m.verify_export(sub, 300, "no-such-user-t4420")
            self.assertEqual(code, m.VERIFY_ALERT)

    def test_symlinked_file_or_dir_is_alert(self):
        with tempfile.TemporaryDirectory() as p:
            sub = self._sub(p)
            link_dir = os.path.join(p, "link")
            os.symlink(sub, link_dir)
            code, reason = m.verify_export(link_dir, 300, self.me)
            self.assertEqual(code, m.VERIFY_ALERT)
            self.assertIn("symlink", reason)

            other = os.path.join(p, "other.json")
            os.rename(os.path.join(sub, m.STATUS_FILE), other)
            os.symlink(other, os.path.join(sub, m.STATUS_FILE))
            code, reason = m.verify_export(sub, 300, self.me)
            self.assertEqual(code, m.VERIFY_ALERT)
            self.assertIn("symlink", reason)

    def test_writable_or_missing_is_alert(self):
        with tempfile.TemporaryDirectory() as p:
            sub = self._sub(p)
            os.chmod(os.path.join(sub, m.STATUS_FILE), 0o660)
            code, reason = m.verify_export(sub, 300, self.me)
            self.assertEqual(code, m.VERIFY_ALERT)
            self.assertIn("writable", reason)
            os.unlink(os.path.join(sub, m.STATUS_FILE))
            code, _ = m.verify_export(sub, 300, self.me)
            self.assertEqual(code, m.VERIFY_ALERT)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m.main(["--verify", "--dir", sub, "--owner", self.me]), 2)


class TestTimeoutsAndLastGood(unittest.TestCase):
    def test_status_timeout_capped_at_20(self):
        seen = {}

        def fake(host, port, request, **kw):
            seen.update(kw)
            return {"request_id": request["request_id"], "ok": True, "data": {}}

        with mock.patch.object(m, "mqtt_request", fake):
            m.fetch_status({"ECHO_STATUS_TIMEOUT_S": "300"})
            self.assertEqual(seen["timeout"], 20.0)
            m.fetch_status({"ECHO_STATUS_TIMEOUT_S": "7"})
            self.assertEqual(seen["timeout"], 7.0)
        for bad in ("0", "-1", "x"):
            with self.subTest(bad=bad), self.assertRaises(m.MqttError):
                m.fetch_status({"ECHO_STATUS_TIMEOUT_S": bad})

    def test_service_timeout_covers_journal_plus_status(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "echo-status-export.service")
        with open(path) as f:
            line = next(ln for ln in f if ln.startswith("TimeoutStartSec="))
        budget = m.JOURNAL_TIMEOUT_S + m.MAX_STATUS_TIMEOUT_S + m.SYSTEMCTL_TIMEOUT_S
        unit_budget = int(line.split("=")[1])
        self.assertGreater(unit_budget, budget)
        # T-4727: keep >= 10 s of startup/write margin, and stay inside the
        # 2-minute refresh interval so runs never overlap the next fire.
        self.assertGreaterEqual(unit_budget - budget, 10)
        self.assertLessEqual(unit_budget, 120)

    def test_failed_last_good_write_warns_not_failed(self):
        real = m.atomic_write_json

        def flaky(directory, name, data, group=None):
            if name == m.LAST_GOOD_FILE:
                raise OSError("disk full")
            return real(directory, name, data, group)

        with tempfile.TemporaryDirectory() as d:
            env = {"ECHO_EXPORT_DIR": d, "ECHO_EXPORT_GROUP": ""}
            err = io.StringIO()
            with mock.patch.object(m, "atomic_write_json", flaky), contextlib.redirect_stderr(err):
                line, ok = m._summary("status", lambda e: m.export_status(e, lambda _: {"ok": True,
                                                                                        "data": SAMPLE_STATUS}), env)
            self.assertTrue(ok)
            self.assertEqual(line, "status=ok")
            self.assertIn("could not refresh", err.getvalue())
            self.assertEqual(os.listdir(d), [m.STATUS_FILE])


class TestAtomicWrite(unittest.TestCase):
    def test_mode_rename_no_tmp_left(self):
        with tempfile.TemporaryDirectory() as d:
            path = m.atomic_write_json(d, "x.json", {"a": 1})
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)
            self.assertEqual(os.listdir(d), ["x.json"])
            m.atomic_write_json(d, "x.json", {"a": 2})
            with open(path) as f:
                self.assertEqual(json.load(f), {"a": 2})
            self.assertEqual(os.listdir(d), ["x.json"])

    def test_fchmod_before_fsync(self):
        order = []
        real_fchmod, real_fsync = os.fchmod, os.fsync
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(m.os, "fchmod", side_effect=lambda *a: (order.append("fchmod"), real_fchmod(*a))), \
                mock.patch.object(m.os, "fsync", side_effect=lambda *a: (order.append("fsync"), real_fsync(*a))):
            m.atomic_write_json(d, "o.json", {})
        self.assertEqual(order[:2], ["fchmod", "fsync"])

    def test_uses_rename(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("os.replace", wraps=os.replace) as rep:
            m.atomic_write_json(d, "y.json", {})
            src, dst = rep.call_args[0]
            self.assertEqual(os.path.dirname(src), d)
            self.assertEqual(dst, os.path.join(d, "y.json"))

    def test_failed_write_leaves_old_file_and_no_tmp(self):
        with tempfile.TemporaryDirectory() as d:
            m.atomic_write_json(d, "z.json", {"a": 1})
            with self.assertRaises(TypeError):
                m.atomic_write_json(d, "z.json", {"a": object()})
            self.assertEqual(os.listdir(d), ["z.json"])
            with open(os.path.join(d, "z.json")) as f:
                self.assertEqual(json.load(f), {"a": 1})


class TestExportStatus(unittest.TestCase):
    def test_ok_writes_status_and_last_good(self):
        with tempfile.TemporaryDirectory() as d:
            rec = m.export_status({"ECHO_EXPORT_DIR": d}, lambda e: {"ok": True, "data": SAMPLE_STATUS})
            self.assertIn("exported_at", rec)
            self.assertEqual(sorted(os.listdir(d)), sorted([m.LAST_GOOD_FILE, m.STATUS_FILE]))
            with open(os.path.join(d, m.STATUS_FILE)) as f:
                data = json.load(f)
            self.assertNotIn("alice", json.dumps(data))
            self.assertEqual(data["sanitizer"]["dropped_paths"], ["active_speaker.speaker_id"])

    def test_no_reply_writes_error_and_keeps_last_good(self):
        with tempfile.TemporaryDirectory() as d:
            env = {"ECHO_EXPORT_DIR": d}
            m.export_status(env, lambda e: {"ok": True, "data": SAMPLE_STATUS})

            def silent(e):
                raise m.NoReply("no reply")
            rec = m.export_status(env, silent)
            self.assertEqual(rec["error"], "no reply")
            with open(os.path.join(d, m.STATUS_FILE)) as f:
                cur = json.load(f)
            with open(os.path.join(d, m.LAST_GOOD_FILE)) as f:
                good = json.load(f)
            self.assertEqual(set(cur), {"schema", "exported_at", "source", "error"})
            self.assertIn("status", good)

    def test_daemon_error(self):
        rec, ok = m.build_status_record({}, lambda e: {"ok": False, "error": "status snapshot not available"})
        self.assertFalse(ok)
        self.assertEqual(rec["error"], "daemon error: status snapshot not available")


def _boot_status(started_at, final=0, held=0, hold_max=0):
    s = json.loads(json.dumps(REALISTIC_STATUS))
    if started_at is None:
        del s["started_at"]
    else:
        s["started_at"] = started_at
    s["metrics"].update(stt_label_final_count=final, stt_label_held_count=held, stt_label_hold_max_ms=hold_max)
    return s


class TestBootArchive(unittest.TestCase):
    B1 = "2026-09-30T10:50:49.310858480Z"
    B2 = "2026-09-30T13:31:03.5Z"
    B1_FILE = "echo-status.boot-2026-09-30T10-50-49Z.json"

    def _env(self, d):
        return {"ECHO_EXPORT_DIR": d, "ECHO_EXPORT_GROUP": ""}

    def _run(self, d, status, exported_at=None):
        with mock.patch.object(m, "utc_now_iso", return_value=exported_at or m.utc_now_iso()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            m.export_status(self._env(d), lambda e: {"ok": True, "data": status})
        return err.getvalue()

    def _boots(self, d):
        b = os.path.join(d, m.BOOTS_DIR)
        return sorted(os.listdir(b)) if os.path.isdir(b) else []

    def test_boot_file_name(self):
        self.assertEqual(m.boot_file_name(self.B1), self.B1_FILE)
        self.assertEqual(m.boot_file_name("2026-09-30T13:31:03Z"), "echo-status.boot-2026-09-30T13-31-03Z.json")
        for bad in (None, 5, "", "2026-09-30T13:31:03+02:00", "../../etc/passwd", "2026-09-30T13:31:03Z/x",
                    "2026-09-30T13:31:03Z\n", "2026-09-30T13:31:03.5Z\n"):
            with self.subTest(bad=bad):
                self.assertIsNone(m.boot_file_name(bad))

    def test_archive_on_boot_change(self):
        with tempfile.TemporaryDirectory() as p:
            d = os.path.join(p, "status")
            self._run(d, _boot_status(self.B1, final=10), "2026-09-30T13:28:00Z")
            self._run(d, _boot_status(self.B1, final=54, held=9), "2026-09-30T13:30:00Z")
            self.assertEqual(self._boots(d), [])
            self._run(d, _boot_status(self.B2, final=1), "2026-09-30T13:32:00Z")
            self.assertEqual(self._boots(d), [self.B1_FILE])
            boots = os.path.join(d, m.BOOTS_DIR)
            path = os.path.join(boots, self.B1_FILE)
            with open(path) as f:
                arch = json.load(f)
            self.assertEqual(arch["exported_at"], "2026-09-30T13:30:00Z")
            self.assertEqual(arch["status"]["metrics"]["stt_label_final_count"], 54)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)
            self.assertEqual(stat.S_IMODE(os.stat(boots).st_mode) & 0o777, 0o750)
            with open(os.path.join(d, m.LAST_GOOD_FILE)) as f:
                self.assertEqual(json.load(f)["status"]["started_at"], self.B2)
            # Further runs of the new boot archive nothing more.
            self._run(d, _boot_status(self.B2, final=3), "2026-09-30T13:34:00Z")
            self.assertEqual(self._boots(d), [self.B1_FILE])

    def test_no_archive_same_boot_or_first_run_or_error(self):
        with tempfile.TemporaryDirectory() as d:
            self._run(d, _boot_status(self.B1))
            self._run(d, _boot_status(self.B1))
            with contextlib.redirect_stderr(io.StringIO()):
                m.export_status(self._env(d), lambda e: (_ for _ in ()).throw(m.NoReply("no reply")))
            self.assertEqual(self._boots(d), [])
            self.assertNotIn(m.BOOTS_DIR, os.listdir(d))

    def test_missing_started_at_no_archive_warns(self):
        for first, second in ((None, self.B2), (self.B1, None)):
            with self.subTest(first=first, second=second), tempfile.TemporaryDirectory() as d:
                self._run(d, _boot_status(first))
                err = self._run(d, _boot_status(second))
                self.assertEqual(self._boots(d), [])
                self.assertIn("started_at missing", err)

    def test_boot_file_pattern_rejects_trailing_newline(self):
        self.assertIsNotNone(m._BOOT_FILE.fullmatch(self.B1_FILE))
        self.assertIsNone(m._BOOT_FILE.fullmatch(self.B1_FILE + "\n"))
        with tempfile.TemporaryDirectory() as d:
            for n in (self.B1_FILE, "echo-status.boot-2026-09-30T09-00-00Z.json\n"):
                with open(os.path.join(d, n), "w") as f:
                    f.write("{}")
            self.assertEqual(m.prune_boots(d, keep=0), [self.B1_FILE])

    def test_non_utc_started_at_no_archive_warns(self):
        with tempfile.TemporaryDirectory() as d:
            self._run(d, _boot_status("2026-09-30T10:50:49+00:00"))
            err = self._run(d, _boot_status(self.B2))
            self.assertEqual(self._boots(d), [])
            self.assertIn("not a UTC timestamp", err)
            with open(os.path.join(d, m.LAST_GOOD_FILE)) as f:
                self.assertEqual(json.load(f)["status"]["started_at"], self.B2)

    def test_idempotent_overwrite_only_if_newer(self):
        with tempfile.TemporaryDirectory() as d:
            boots = os.path.join(d, m.BOOTS_DIR)
            os.makedirs(boots)
            path = os.path.join(boots, self.B1_FILE)
            with open(path, "w") as f:
                json.dump({"exported_at": "2026-09-30T13:30:00Z", "marker": "existing"}, f)
            # Older last-good of the same boot: the existing archive stays.
            self._run(d, _boot_status(self.B1), "2026-09-30T13:20:00Z")
            self._run(d, _boot_status(self.B2), "2026-09-30T13:32:00Z")
            with open(path) as f:
                self.assertEqual(json.load(f)["marker"], "existing")
            # Newer last-good of the same boot: replaced.
            self._run(d, _boot_status(self.B1), "2026-09-30T13:40:00Z")
            self._run(d, _boot_status(self.B2), "2026-09-30T13:42:00Z")
            with open(path) as f:
                got = json.load(f)
            self.assertNotIn("marker", got)
            self.assertEqual(got["exported_at"], "2026-09-30T13:40:00Z")

    def test_retention_keeps_newest_30(self):
        with tempfile.TemporaryDirectory() as d:
            boots = os.path.join(d, m.BOOTS_DIR)
            os.makedirs(boots)
            for i in range(35):
                name = m.boot_file_name(f"2026-09-{i // 24 + 1:02d}T{i % 24:02d}:00:00Z")
                with open(os.path.join(boots, name), "w") as f:
                    json.dump({"exported_at": "2026-09-01T00:00:00Z"}, f)
            with open(os.path.join(boots, "unrelated.txt"), "w") as f:
                f.write("x")
            self._run(d, _boot_status("2026-09-30T09:00:00Z"))
            self._run(d, _boot_status(self.B2))
            names = [n for n in self._boots(d) if n.startswith(m.BOOT_PREFIX)]
            self.assertEqual(len(names), m.BOOTS_KEEP)
            self.assertEqual(names[-1], "echo-status.boot-2026-09-30T09-00-00Z.json")
            self.assertNotIn(m.boot_file_name("2026-09-01T00:00:00Z"), names)
            self.assertIn(m.boot_file_name("2026-09-01T06:00:00Z"), names)
            self.assertIn("unrelated.txt", self._boots(d))

    def test_archive_failure_warns_and_last_good_still_refreshes(self):
        with tempfile.TemporaryDirectory() as d:
            self._run(d, _boot_status(self.B1))
            with mock.patch.object(m, "archive_previous_boot", side_effect=OSError("disk full")):
                err = self._run(d, _boot_status(self.B2))
            self.assertIn("could not archive", err)
            with open(os.path.join(d, m.LAST_GOOD_FILE)) as f:
                self.assertEqual(json.load(f)["status"]["started_at"], self.B2)


class TestBootsReader(unittest.TestCase):
    me = pwd.getpwuid(os.getuid()).pw_name

    def _setup(self, p):
        d = os.path.join(p, "status")
        env = {"ECHO_EXPORT_DIR": d, "ECHO_EXPORT_GROUP": ""}
        runs = [("2026-09-30T08:00:00Z", 40, 5, 700), ("2026-09-30T08:00:00Z", 50, 6, 900),
                ("2026-09-30T10:00:00Z", 20, 2, 1200), ("2026-09-30T12:00:00Z", 7, 1, 300)]
        for started, final, held, mx in runs:
            status = _boot_status(started, final, held, mx)
            with contextlib.redirect_stderr(io.StringIO()):
                m.export_status(env, lambda e, s=status: {"ok": True, "data": s})
        return d

    def test_rows_and_totals(self):
        with tempfile.TemporaryDirectory() as p:
            d = self._setup(p)
            keys = list(m.DEFAULT_BOOT_KEYS)
            rows = m.read_boots(d, keys, self.me)
            self.assertEqual([r["started_at"] for r in rows],
                             ["2026-09-30T08:00:00Z", "2026-09-30T10:00:00Z", "2026-09-30T12:00:00Z"])
            self.assertEqual(rows[0]["values"]["metrics.stt_label_final_count"], 50)
            self.assertEqual(rows[-1]["source"], m.STATUS_FILE)
            totals = m.boot_totals(rows, keys)
            self.assertEqual(totals, {"metrics.stt_label_final_count": 77, "metrics.stt_label_held_count": 9,
                                      "metrics.stt_label_hold_max_ms": 1200})
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = m.main(["--boots", "--dir", d, "--owner", self.me])
            self.assertEqual(code, 0)
            text = out.getvalue()
            self.assertIn("TOTAL (3 boots)", text)
            total_line = next(ln for ln in text.splitlines() if ln.startswith("TOTAL"))
            self.assertEqual(total_line.split()[3:], ["77", "9", "1200"])

    def test_custom_keys_missing_and_non_numeric(self):
        with tempfile.TemporaryDirectory() as p:
            d = self._setup(p)
            keys = ["metrics.guest_count", "metrics.no_such", "broker.host"]
            rows = m.read_boots(d, keys, self.me)
            totals = m.boot_totals(rows, keys)
            self.assertEqual(totals, {"metrics.guest_count": 9, "metrics.no_such": None, "broker.host": None})
            with contextlib.redirect_stdout(io.StringIO()) as out:
                m.main(["--boots", "--dir", d, "--owner", self.me, "--key", "metrics.guest_count"])
            self.assertIn("guest_count", out.getvalue())

    def test_current_error_falls_back_to_last_good(self):
        with tempfile.TemporaryDirectory() as p:
            d = self._setup(p)
            with contextlib.redirect_stderr(io.StringIO()):
                m.export_status({"ECHO_EXPORT_DIR": d, "ECHO_EXPORT_GROUP": ""},
                                lambda e: (_ for _ in ()).throw(m.NoReply("no reply")))
            rows = m.read_boots(d, list(m.DEFAULT_BOOT_KEYS), self.me)
            self.assertEqual(rows[-1]["started_at"], "2026-09-30T12:00:00Z")
            self.assertIn("current has error", rows[-1]["source"])

    def test_totals_only_where_meaningful(self):
        keys = ["metrics.stt_label_final_count", "metrics.stt_label_hold_max_ms", "metrics.stt_p95_ms",
                "metrics.stt_p50_ms", "metrics.mean_latency"]
        rows = [{"values": {k: v for k in keys}} for v in (10, 30)]
        self.assertEqual(m.boot_totals(rows, keys), {
            "metrics.stt_label_final_count": 40, "metrics.stt_label_hold_max_ms": 30,
            "metrics.stt_p95_ms": None, "metrics.stt_p50_ms": None, "metrics.mean_latency": None})
        with tempfile.TemporaryDirectory() as p:
            d = self._setup(p)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = m.main(["--boots", "--dir", d, "--owner", self.me, "--key", "metrics.stt_label_final_count",
                               "--key", "metrics.stt_label_hold_max_ms", "--key", "metrics.stt_p95_ms"])
            self.assertEqual(code, 0)
            total_line = next(ln for ln in out.getvalue().splitlines() if ln.startswith("TOTAL"))
            self.assertEqual(total_line.split()[3:], ["77", "1200", "-"])

    def test_current_unavailable_is_a_visible_row(self):
        with tempfile.TemporaryDirectory() as p:
            d = self._setup(p)
            with contextlib.redirect_stderr(io.StringIO()):
                m.export_status({"ECHO_EXPORT_DIR": d, "ECHO_EXPORT_GROUP": ""},
                                lambda e: (_ for _ in ()).throw(m.NoReply("no reply")))
            os.chmod(os.path.join(d, m.LAST_GOOD_FILE), 0o666)
            keys = list(m.DEFAULT_BOOT_KEYS)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                rows = m.read_boots(d, keys, self.me)
            self.assertIn("current boot unavailable", err.getvalue())
            self.assertEqual(len(rows), 3)
            self.assertTrue(rows[-1]["unavailable"])
            self.assertTrue(rows[-1]["source"].startswith("current: unavailable (current has error; last-good: "))
            self.assertIn("writable", rows[-1]["source"])
            self.assertEqual(rows[-1]["values"], {k: None for k in keys})
            with contextlib.redirect_stdout(io.StringIO()) as out, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                code = m.main(["--boots", "--dir", d, "--owner", self.me])
            self.assertEqual(code, 0)
            self.assertIn("current boot unavailable", err.getvalue())
            text = out.getvalue()
            self.assertIn("current: unavailable (", text)
            total_line = next(ln for ln in text.splitlines() if ln.startswith("TOTAL"))
            self.assertIn("TOTAL (2 boots; current unavailable)", total_line)
            # Missing last-good: same visible row.
            os.unlink(os.path.join(d, m.LAST_GOOD_FILE))
            with contextlib.redirect_stderr(io.StringIO()):
                rows = m.read_boots(d, keys, self.me)
            self.assertTrue(rows[-1]["unavailable"])
            # An untrusted boot file is still a hard failure (exit 2).
            boots = os.path.join(d, m.BOOTS_DIR)
            os.chmod(os.path.join(boots, sorted(os.listdir(boots))[0]), 0o666)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(m.main(["--boots", "--dir", d, "--owner", self.me]), 2)

    def test_no_boots_dir_is_current_only(self):
        with tempfile.TemporaryDirectory() as p:
            d = os.path.join(p, "status")
            with contextlib.redirect_stderr(io.StringIO()):
                m.export_status({"ECHO_EXPORT_DIR": d, "ECHO_EXPORT_GROUP": ""},
                                lambda e: {"ok": True, "data": _boot_status("2026-09-30T08:00:00Z", 3)})
            rows = m.read_boots(d, ["metrics.stt_label_final_count"], self.me)
            self.assertEqual(len(rows), 1)

    def test_refuses_untrusted(self):
        with tempfile.TemporaryDirectory() as p:
            d = self._setup(p)
            # Wrong owner (same pattern as TestVerify.test_wrong_owner_is_alert).
            with mock.patch.object(pwd, "getpwnam", return_value=mock.Mock(pw_uid=os.getuid() + 1)), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(m.main(["--boots", "--dir", d, "--owner", "claude"]), 2)
            self.assertIn("expected claude", out.getvalue())
            # A writable or symlinked boot file.
            boots = os.path.join(d, m.BOOTS_DIR)
            name = sorted(os.listdir(boots))[0]
            os.chmod(os.path.join(boots, name), 0o660)
            with self.assertRaises(m.Untrusted) as cm:
                m.read_boots(d, [], self.me)
            self.assertIn("writable", str(cm.exception))
            os.chmod(os.path.join(boots, name), 0o640)
            other = os.path.join(p, "other.json")
            os.rename(os.path.join(boots, name), other)
            os.symlink(other, os.path.join(boots, name))
            with self.assertRaises(m.Untrusted) as cm:
                m.read_boots(d, [], self.me)
            self.assertIn("symlink", str(cm.exception))
            # A symlinked boots/ dir.
            os.unlink(os.path.join(boots, name))
            os.rename(boots, os.path.join(p, "realboots"))
            os.symlink(os.path.join(p, "realboots"), boots)
            with self.assertRaises(m.Untrusted) as cm:
                m.read_boots(d, [], self.me)
            self.assertIn("symlink", str(cm.exception))


class _FakeBroker(threading.Thread):
    """One-connection MQTT 3.1.1 broker stub: CONNACK, SUBACK, then answers
    the config request with a foreign-id reply followed by (optionally) the
    matching one."""

    def __init__(self, answer=True, mode="normal", data=None, reply_topic=None):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.answer = answer
        self.mode = mode  # normal | reset (RST after the request) | short_publish
        self.data = data
        self.connect_packet = None
        self.reply_topic = reply_topic or m.REPLY_TOPIC  # topic the stub answers on
        self.subscribed_topic = None
        self.published_topic = None

    def _read(self, c):
        hdr = c.recv(1)
        if not hdr:
            return None, b""
        mult, n = 1, 0
        while True:
            b = c.recv(1)[0]
            n += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        body = b""
        while len(body) < n:
            body += c.recv(n - len(body))
        return hdr[0], body

    def run(self):
        c, _ = self.srv.accept()
        with c:
            t, self.connect_packet = self._read(c)
            c.sendall(b"\x20\x02\x00\x00")
            t, body = self._read(c)
            pid = body[:2]
            slen = struct.unpack("!H", body[2:4])[0]
            self.subscribed_topic = body[4:4 + slen].decode()
            c.sendall(b"\x90\x03" + pid + b"\x00")
            t, body = self._read(c)
            tlen = struct.unpack("!H", body[:2])[0]
            self.published_topic = body[2:2 + tlen].decode()
            req = json.loads(body[2 + tlen:])
            if self.mode == "reset":
                c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                c.close()
                self.srv.close()
                return
            if self.mode == "short_publish":
                c.sendall(b"\x30\x01\x00")  # PUBLISH whose body is too short for a topic length
            elif self.answer:
                for rid in ("someone-else", req["request_id"]):
                    data = self.data if self.data is not None else {"who": rid}
                    payload = json.dumps({"request_id": rid, "ok": True, "data": data}).encode()
                    # QoS1 publish (daemon publishes QoS 1) with packet id
                    var = m._enc_str(self.reply_topic) + b"\x00\x07" + payload
                    c.sendall(bytes([0x32]) + m._enc_len(len(var)) + var)
            try:
                c.recv(16)
            except OSError:
                pass
        self.srv.close()


class TestMqttClient(unittest.TestCase):
    def test_round_trip_matches_request_id(self):
        b = _FakeBroker()
        b.start()
        reply = m.mqtt_request("127.0.0.1", b.port, {"request_id": "r-1", "op": "get", "resource": "status"},
                               timeout=5, username="u", password="p")
        b.join(5)
        self.assertEqual(reply["data"], {"who": "r-1"})
        self.assertIn(b"\x00\x01u\x00\x01p", b.connect_packet)

    def test_no_reply_times_out(self):
        b = _FakeBroker(answer=False)
        b.start()
        with self.assertRaises(m.NoReply):
            m.mqtt_request("127.0.0.1", b.port, {"request_id": "r-2"}, timeout=0.5)
        b.join(5)

    def test_large_reply_multibyte_remaining_length(self):
        big = {"blob": "x" * 20000, "n": list(range(500))}
        b = _FakeBroker(data=big)
        b.start()
        reply = m.mqtt_request("127.0.0.1", b.port, {"request_id": "r-big"}, timeout=5)
        b.join(5)
        self.assertEqual(reply["data"], big)
        self.assertGreater(len(json.dumps(reply)), 16 * 1024)

    def test_connection_reset_mid_read_is_mqtt_error(self):
        b = _FakeBroker(mode="reset")
        b.start()
        with self.assertRaises(m.MqttError) as cm:
            m.mqtt_request("127.0.0.1", b.port, {"request_id": "r-4"}, timeout=5)
        b.join(5)
        self.assertIn("ConnectionResetError", str(cm.exception))

    def test_short_publish_body_is_mqtt_error(self):
        b = _FakeBroker(mode="short_publish")
        b.start()
        with self.assertRaises(m.MqttError) as cm:
            m.mqtt_request("127.0.0.1", b.port, {"request_id": "r-5"}, timeout=5)
        b.join(5)
        self.assertEqual(str(cm.exception), "malformed packet from broker")

    def test_broker_unreachable(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(m.MqttError):
            m.mqtt_request("127.0.0.1", port, {"request_id": "r-3"}, timeout=1)


class TestStatusTopics(unittest.TestCase):
    """T-4431: status request/reply topics come from a pair allowlist, default unchanged.

    Broker round trips use the in-process _FakeBroker on its own ephemeral port; every
    other test patches mqtt_request, so no test can reach a real broker.
    """

    STATUS_PAIR = {"ECHO_STATUS_REQUEST_TOPIC": "echo/config/status/request",
                   "ECHO_STATUS_REPLY_TOPIC": "echo/config/status/reply"}
    BAD = "bad config: ECHO_STATUS_REQUEST_TOPIC / ECHO_STATUS_REPLY_TOPIC"

    def _env(self, port, timeout="5", **extra):
        env = {"ECHO_MQTT_HOST": "127.0.0.1", "ECHO_MQTT_PORT": str(port),
               "ECHO_STATUS_TIMEOUT_S": timeout}
        env.update(extra)
        return env

    def _fetch_via_broker(self, env_extra, reply_topic):
        b = _FakeBroker(reply_topic=reply_topic)
        b.start()
        reply = m.fetch_status(self._env(b.port, **env_extra))
        b.join(5)
        return b, reply

    def test_default_topics_unchanged(self):
        self.assertEqual(m.status_topics({}), ("echo/config/request", "echo/config/reply"))
        b, reply = self._fetch_via_broker({}, "echo/config/reply")
        self.assertTrue(reply["ok"])
        self.assertEqual(b.published_topic, "echo/config/request")
        self.assertEqual(b.subscribed_topic, "echo/config/reply")

    def test_empty_values_mean_default(self):
        empty = {"ECHO_STATUS_REQUEST_TOPIC": "", "ECHO_STATUS_REPLY_TOPIC": ""}
        self.assertEqual(m.status_topics(empty), (m.REQUEST_TOPIC, m.REPLY_TOPIC))
        self.assertEqual(m.status_topics({"ECHO_STATUS_REQUEST_TOPIC": ""}), (m.REQUEST_TOPIC, m.REPLY_TOPIC))
        # Legacy pair spelled out explicitly is also accepted.
        self.assertEqual(m.status_topics({"ECHO_STATUS_REQUEST_TOPIC": m.REQUEST_TOPIC,
                                          "ECHO_STATUS_REPLY_TOPIC": m.REPLY_TOPIC}),
                         (m.REQUEST_TOPIC, m.REPLY_TOPIC))

    def test_status_pair_from_env(self):
        self.assertEqual((m.STATUS_REQUEST_TOPIC, m.STATUS_REPLY_TOPIC),
                         ("echo/config/status/request", "echo/config/status/reply"))
        b, reply = self._fetch_via_broker(self.STATUS_PAIR, "echo/config/status/reply")
        self.assertTrue(reply["ok"])
        self.assertEqual(b.published_topic, "echo/config/status/request")
        self.assertEqual(b.subscribed_topic, "echo/config/status/reply")

    def test_reply_on_other_topic_is_ignored(self):
        # Configured for the status pair, a reply on the shared topic does not count.
        b = _FakeBroker(reply_topic="echo/config/reply")
        b.start()
        with self.assertRaises(m.NoReply):
            m.fetch_status(self._env(b.port, timeout="0.5", **self.STATUS_PAIR))
        b.join(5)

    def test_non_allowlisted_topics_are_bad_config(self):
        cases = {
            "mixed: status request only": {"ECHO_STATUS_REQUEST_TOPIC": m.STATUS_REQUEST_TOPIC},
            "mixed: status reply only": {"ECHO_STATUS_REPLY_TOPIC": m.STATUS_REPLY_TOPIC},
            "mixed: status request, legacy reply": {"ECHO_STATUS_REQUEST_TOPIC": m.STATUS_REQUEST_TOPIC,
                                                    "ECHO_STATUS_REPLY_TOPIC": m.REPLY_TOPIC},
            "mixed: legacy request, status reply": {"ECHO_STATUS_REQUEST_TOPIC": m.REQUEST_TOPIC,
                                                    "ECHO_STATUS_REPLY_TOPIC": m.STATUS_REPLY_TOPIC},
            "valid-looking command topic": {"ECHO_STATUS_REQUEST_TOPIC": "echo/config/set",
                                            "ECHO_STATUS_REPLY_TOPIC": m.REPLY_TOPIC},
            "wildcard reply": {"ECHO_STATUS_REPLY_TOPIC": "echo/config/#"},
            "plus wildcard": {"ECHO_STATUS_REQUEST_TOPIC": "echo/+/request"},
            "nul": {"ECHO_STATUS_REQUEST_TOPIC": "echo/config\0/request"},
        }
        for name, env in cases.items():
            with self.subTest(name):
                with mock.patch.object(m, "mqtt_request", side_effect=AssertionError("sent")):
                    with self.assertRaises(m.MqttError) as cm:
                        m.fetch_status(env)
                    self.assertEqual(str(cm.exception), self.BAD)
                    rec, ok = m.build_status_record(env, m.fetch_status)
                self.assertFalse(ok)
                self.assertEqual(rec["error"], self.BAD)
                self.assertEqual(rec["source"]["transport"], "mqtt op=get resource=status (bad topic config)")
                dumped = json.dumps(rec)
                for value in env.values():  # the rejected pair is never written to the export
                    self.assertNotIn(json.dumps(value)[1:-1], dumped)

    def test_transport_reports_topics_used(self):
        ok_fetch = lambda e: {"ok": True, "data": SAMPLE_STATUS}  # noqa: E731
        rec, ok = m.build_status_record({}, ok_fetch)
        self.assertTrue(ok)
        self.assertEqual(rec["source"]["transport"],
                         "mqtt op=get resource=status request=echo/config/request reply=echo/config/reply")
        rec, ok = m.build_status_record(dict(self.STATUS_PAIR), ok_fetch)
        self.assertTrue(ok)
        self.assertEqual(rec["source"]["transport"],
                         "mqtt op=get resource=status request=echo/config/status/request "
                         "reply=echo/config/status/reply")


class TestNoSilentStale(unittest.TestCase):
    """Every failure mode still rewrites echo-status.json with an error."""

    def _run(self, env_extra, fetch=m.fetch_status):
        with tempfile.TemporaryDirectory() as d:
            env = {"ECHO_EXPORT_DIR": d, "ECHO_EXPORT_GROUP": ""}
            env.update(env_extra)
            m.export_status(env, lambda e: {"ok": True, "data": SAMPLE_STATUS})
            rec = m.export_status(env, fetch)
            with open(os.path.join(d, m.STATUS_FILE)) as f:
                on_disk = json.load(f)
            with open(os.path.join(d, m.LAST_GOOD_FILE)) as f:
                good = json.load(f)
        self.assertEqual(on_disk, rec)
        self.assertIn("status", good)
        self.assertNotIn("status", on_disk)
        return on_disk

    def test_connection_reset_writes_error_record(self):
        b = _FakeBroker(mode="reset")
        b.start()
        rec = self._run({"ECHO_MQTT_HOST": "127.0.0.1", "ECHO_MQTT_PORT": str(b.port), "ECHO_STATUS_TIMEOUT_S": "5"})
        b.join(5)
        self.assertEqual(rec["error"], "broker protocol/io error (ConnectionResetError)")

    def test_short_publish_writes_error_record(self):
        b = _FakeBroker(mode="short_publish")
        b.start()
        rec = self._run({"ECHO_MQTT_HOST": "127.0.0.1", "ECHO_MQTT_PORT": str(b.port), "ECHO_STATUS_TIMEOUT_S": "5"})
        b.join(5)
        self.assertEqual(rec["error"], "malformed packet from broker")

    def test_bad_port_env_writes_error_record(self):
        for port in ("not-a-port", "70000", ""):
            with self.subTest(port=port):
                rec = self._run({"ECHO_MQTT_PORT": port})
                self.assertEqual(rec["error"], "bad config: ECHO_MQTT_PORT / ECHO_STATUS_TIMEOUT_S")

    def test_unexpected_exception_writes_error_record(self):
        def boom(env):
            raise KeyError("surprise")
        rec = self._run({}, boom)
        self.assertEqual(rec["error"], "exporter error (KeyError)")

    def test_non_dict_reply(self):
        rec, ok = m.build_status_record({}, lambda e: ["not", "a", "dict"])
        self.assertFalse(ok)
        self.assertEqual(rec["error"], "malformed reply")

    def test_main_runs_journal_even_if_status_raises_and_vice_versa(self):
        calls = []

        def status_boom(env):
            calls.append("status")
            raise OSError("disk full")

        def journal_ok(env):
            calls.append("journal")
            return {"signatures": []}

        with mock.patch.object(m, "export_status", status_boom), mock.patch.object(m, "export_journal", journal_ok):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m.main(), 1)
        self.assertEqual(calls, ["status", "journal"])

        calls.clear()

        def status_ok(env):
            calls.append("status")
            return {"status": {}}

        def journal_boom(env):
            calls.append("journal")
            raise OSError("disk full")

        with mock.patch.object(m, "export_status", status_ok), mock.patch.object(m, "export_journal", journal_boom):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m.main(), 1)
        self.assertEqual(calls, ["status", "journal"])

    def test_request_is_hard_coded_read_only(self):
        seen = {}

        def fake(host, port, request, **kw):
            seen.update(request)
            return {"request_id": request["request_id"], "ok": True, "data": {}}

        with mock.patch.object(m, "mqtt_request", fake):
            m.fetch_status({"ECHO_OP": "set", "ECHO_RESOURCE": "config"})
        self.assertEqual((seen["op"], seen["resource"]), ("get", "status"))


class TestExportDir(unittest.TestCase):
    def test_creates_2750_subdir_and_writes_there(self):
        with tempfile.TemporaryDirectory() as parent:
            os.chmod(parent, 0o2775)
            sub = os.path.join(parent, "status")
            m.export_status({"ECHO_EXPORT_DIR": sub, "ECHO_EXPORT_GROUP": ""},
                            lambda e: {"ok": True, "data": SAMPLE_STATUS})
            self.assertEqual(stat.S_IMODE(os.stat(sub).st_mode), 0o2750)
            self.assertIn(m.STATUS_FILE, os.listdir(sub))
            self.assertEqual(sorted(os.listdir(parent)), ["status"])

    def test_default_dir_is_status_subdir(self):
        self.assertEqual(m._out_dir({}), "/var/lib/pantheon/echo-evidence/status")

    def test_refuses_foreign_owned_or_symlinked_dir(self):
        with tempfile.TemporaryDirectory() as parent:
            sub = os.path.join(parent, "status")
            os.mkdir(sub)
            with mock.patch.object(m.os, "getuid", return_value=os.getuid() + 1):
                with self.assertRaises(m.ExportDirError):
                    m.prepare_export_dir(sub)
            link = os.path.join(parent, "link")
            os.symlink(sub, link)
            with self.assertRaises(m.ExportDirError):
                m.prepare_export_dir(link)

    def test_stale_tmp_removed_fresh_and_foreign_kept(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.path.join(d, f".{m.STATUS_FILE}.abc123.tmp")
            fresh = os.path.join(d, f".{m.JOURNAL_FILE}.def456.tmp")
            other = os.path.join(d, ".unrelated.old.tmp")
            for p in (old, fresh, other):
                open(p, "w").close()
            past = m.time.time() - 3600
            os.utime(old, (past, past))
            os.utime(other, (past, past))
            m.prepare_export_dir(d)
            self.assertEqual(sorted(os.listdir(d)), sorted([os.path.basename(fresh), os.path.basename(other)]))


class TestBinaryHashCache(unittest.TestCase):
    def test_cache_hit_skips_rehash_and_change_invalidates(self):
        with tempfile.TemporaryDirectory() as d:
            binp = os.path.join(d, "echo")
            with open(binp, "wb") as f:
                f.write(b"v1")
            first = m.binary_identity(binp, d)
            self.assertIn(m.HASH_CACHE_FILE, os.listdir(d))
            with mock.patch.object(m.hashlib, "sha256", side_effect=AssertionError("rehashed")):
                self.assertEqual(m.binary_identity(binp, d), first)
            with open(binp, "wb") as f:
                f.write(b"version-2")
            second = m.binary_identity(binp, d)
            self.assertNotEqual(first["sha256"], second["sha256"])
            import hashlib
            self.assertEqual(second["sha256"], hashlib.sha256(b"version-2").hexdigest())

    def test_missing_binary(self):
        self.assertEqual(m.binary_identity("/nonexistent/echo", None), {"path": "/nonexistent/echo",
                                                                         "error": "FileNotFoundError"})


if __name__ == "__main__":
    unittest.main()
