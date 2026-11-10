"""Regression contract for resource_sampler_coverage_monitor (T-6211).

The JSON fixture is the leading, schema-bearing portion of a live sampler record
captured on 2026-11-05; the monitor deliberately needs only its ``ts`` field.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tools.resource_sampler_coverage_monitor as m
from tools import agent_role_map, mailbox


NOW = 1_793_885_029
VALID_ID = "01793892534857781800.3600766.000000.resource-sampler-coverage"
LIVE_RECORD = {
    "ts": NOW,
    "mem_total_kb": 8_131_780,
    "mem_available_kb": 3_758_032,
    "mem_used_pct": 53.8,
    "swap_total_kb": 4_194_300,
    "swap_used_kb": 4_089_624,
    "swap_used_pct": 97.5,
    "loadavg": 10.48583984375,
}


class Classification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "resource_samples.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, record=LIVE_RECORD):
        self.path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    def test_missing_log_is_missing(self):
        self.assertEqual(m.classify(self.path, now=NOW)[0], "MISSING")

    def test_empty_log_is_malformed(self):
        self.path.write_text("", encoding="utf-8")
        self.assertEqual(m.classify(self.path, now=NOW)[0], "MALFORMED")

    def test_fresh_live_record_is_healthy(self):
        self.write()
        cls, detail = m.classify(self.path, now=NOW + 120)
        self.assertEqual(cls, "HEALTHY")
        self.assertIn("120s", detail)

    def test_stale_record_is_stale(self):
        self.write()
        self.assertEqual(m.classify(self.path, now=NOW + m.STALE_AFTER_SECONDS + 1)[0], "STALE")

    def test_future_timestamp_is_malformed(self):
        self.write({**LIVE_RECORD, "ts": NOW + m.FUTURE_SKEW_SECONDS + 1})
        self.assertEqual(m.classify(self.path, now=NOW)[0], "MALFORMED")

    def test_unrecognizable_tail_is_malformed(self):
        self.path.write_text("not-json\n", encoding="utf-8")
        self.assertEqual(m.classify(self.path, now=NOW)[0], "MALFORMED")

    def test_trailing_blank_lines_use_last_nonempty_record(self):
        self.path.write_text(json.dumps(LIVE_RECORD) + "\n\n \n", encoding="utf-8")
        self.assertEqual(m.classify(self.path, now=NOW)[0], "HEALTHY")

    def test_invalid_utf8_is_malformed(self):
        self.path.write_bytes(b"\xff\xfe\n")
        self.assertEqual(m.classify(self.path, now=NOW)[0], "MALFORMED")

    def test_nonfinite_timestamps_are_malformed(self):
        for stamp in (math.nan, math.inf, -math.inf):
            with self.subTest(stamp=stamp):
                self.write({**LIVE_RECORD, "ts": stamp})
                self.assertEqual(m.classify(self.path, now=NOW)[0], "MALFORMED")

    def test_tail_scan_is_bounded_and_rejects_oversize_final_record(self):
        self.path.write_bytes(b'{"ts":1}\n' + b"x" * (m.MAX_TAIL_BYTES + 1))
        self.assertEqual(m.classify(self.path, now=NOW)[0], "MALFORMED")

    def test_exact_limit_complete_final_record_is_accepted_but_plus_one_is_not(self):
        prefix = b'{"ts":1}\n'
        base = json.dumps(LIVE_RECORD, separators=(",", ":")).encode()
        exact = base[:-1] + b',"padding":"' + b"x" * (
            m.MAX_TAIL_BYTES - len(base[:-1]) - len(b',"padding":"') - len(b'"}\n')
        ) + b'"}\n'
        self.assertEqual(len(exact), m.MAX_TAIL_BYTES)
        self.path.write_bytes(prefix + exact)
        self.assertEqual(m.classify(self.path, now=NOW)[0], "HEALTHY")

        plus_one = exact[:-3] + b"x" + exact[-3:]
        self.assertEqual(len(plus_one), m.MAX_TAIL_BYTES + 1)
        self.path.write_bytes(prefix + plus_one)
        self.assertEqual(m.classify(self.path, now=NOW)[0], "MALFORMED")


class RepeatedFire(unittest.TestCase):
    def test_alerts_once_then_suppresses_until_recovery(self):
        state = {}
        self.assertTrue(m.should_alert("STALE", NOW, state))
        m.mark_alerted("STALE", NOW, state, incident_key="a" * 32,
                       alert_payload="stable")
        self.assertTrue(m.should_alert("STALE", NOW + 60, state))
        state["delivery_attempts"] = m.MAX_DELIVERY_ATTEMPTS
        self.assertFalse(m.should_alert("MALFORMED", NOW + 10_000_000, state))
        m.record_class("HEALTHY", state)
        self.assertTrue(m.should_alert("STALE", NOW + 10_000_001, state))

    def test_recovery_clears_prior_problem_state(self):
        state = {"last_class": "STALE", "incident_open": True}
        m.record_class("HEALTHY", state)
        self.assertEqual(state, {"last_class": "HEALTHY", "incident_open": False,
                                 "delivery_attempts": 0, "delivered_id": None,
                                 "alert_payload": None, "incident_key": None})

    def test_malformed_persisted_state_is_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text('{"incident_open":"yes","last_class":"BOGUS","last_alert":"bad"}')
            self.assertEqual(m.load_state(path), {"incident_open": False, "last_class": None,
                                                  "delivery_attempts": 0, "delivered_id": None,
                                                  "alert_payload": None, "incident_key": None})

    def test_bool_blank_and_inconsistent_terminal_state_rearm(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps({
                "incident_open": True,
                "last_class": "STALE",
                "delivery_attempts": True,
                "delivered_id": "   ",
                "alert_payload": "stable",
                "incident_key": "not-a-key",
            }))
            state = m.load_state(path)
            self.assertEqual(state["delivery_attempts"], 0)
            self.assertIsNone(state["delivered_id"])
            self.assertIsNone(state["incident_key"])
            self.assertFalse(state["incident_open"])
            self.assertIsNone(state["alert_payload"])

    def test_delivered_without_attempt_and_healthy_open_state_rearm(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            for bad in (
                {"incident_open": True, "last_class": "STALE", "delivery_attempts": 0,
                 "delivered_id": VALID_ID, "alert_payload": "stable", "incident_key": "a" * 32},
                {"incident_open": True, "last_class": "HEALTHY", "delivery_attempts": 1,
                 "delivered_id": VALID_ID, "alert_payload": "stable", "incident_key": "a" * 32},
            ):
                with self.subTest(bad=bad):
                    path.write_text(json.dumps(bad))
                    state = m.load_state(path)
                    self.assertFalse(state["incident_open"])
                    self.assertEqual(state["delivery_attempts"], 0)
                    self.assertIsNone(state["delivered_id"])

    def test_unhashable_last_class_is_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            for malformed in (["STALE"], {"class": "STALE"}):
                with self.subTest(malformed=malformed):
                    path.write_text(json.dumps({"last_class": malformed}))
                    self.assertIsNone(m.load_state(path)["last_class"])


class Main(unittest.TestCase):
    def test_service_runs_monitor_with_mailbox_group(self):
        service = (
            Path(__file__).resolve().parent
            / "systemd"
            / "pantheon-resource-sampler-coverage.service"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'ExecStart=/usr/bin/sg pantheon-mailbox -c '
            '"/usr/bin/python3 %h/pantheon-config/tools/'
            'resource_sampler_coverage_monitor.py --check"',
            service,
        )

    def test_literal_service_path_can_import_alert_dependency(self):
        script = Path(m.__file__).resolve()
        code = (
            "import runpy; "
            f"d=runpy.run_path({str(script)!r}, run_name='service_path_probe'); "
            "assert callable(d['resolve_alert_sender']())"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", code], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_literal_service_path_unhealthy_fire_reaches_delivery_seam(self):
        script = Path(m.__file__).resolve()
        code = "\n".join((
            "import json, runpy, tempfile",
            "from pathlib import Path",
            f"d=runpy.run_path({str(script)!r}, run_name='service_path_probe')",
            "g=d['main'].__globals__",
            "tmp_obj=tempfile.TemporaryDirectory()",
            "tmp=Path(tmp_obj.name)",
            "state_path=tmp/'state.json'",
            "orig=g['classify']",
            "g['classify']=lambda **kw: orig(tmp/'missing.jsonl', **kw)",
            "g['load_state']=lambda: json.loads(state_path.read_text()) if state_path.exists() else {}",
            "g['save_state']=lambda state: state_path.write_text(json.dumps(state))",
            "delivered=[]",
            f"g['resolve_alert_sender']=lambda: (lambda *a, **kw: delivered.append((a, kw)) or {VALID_ID!r})",
            "assert d['main'](['--check']) == 0",
            "state=json.loads(state_path.read_text())",
            "assert state['incident_open'] is True",
            f"assert state['delivered_id'] == {VALID_ID!r}",
            "assert len(delivered) == 1",
            "tmp_obj.cleanup()",
        ))
        result = subprocess.run(
            [sys.executable, "-I", "-c", code], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_flagless_run_refuses(self):
        self.assertEqual(m.main([]), 64)

    def test_dry_run_never_alerts(self):
        with mock.patch.object(m, "classify", return_value=("STALE", "old")), \
             mock.patch.object(m, "send_alert", side_effect=AssertionError("must not alert")):
            self.assertEqual(m.main(["--check", "--dry-run"]), 0)

    def test_state_is_persisted_before_alert_delivery(self):
        order = []
        with mock.patch.object(m, "classify", return_value=("STALE", "old")), \
             mock.patch.object(m, "load_state", return_value={}), \
             mock.patch.object(m, "save_state", side_effect=lambda state: order.append(("save", dict(state)))), \
             mock.patch.object(m, "send_alert", side_effect=lambda *_, **__: order.append(("send", None)) or "id"):
            self.assertEqual(m.main(["--check"]), 0)
        self.assertEqual([x[0] for x in order], ["save", "save", "send", "save"])
        self.assertTrue(order[0][1]["incident_open"])

    def test_failed_pre_alert_state_write_sends_nothing(self):
        with mock.patch.object(m, "classify", return_value=("STALE", "old")), \
             mock.patch.object(m, "load_state", return_value={}), \
             mock.patch.object(m, "save_state", side_effect=OSError("ENOSPC")), \
             mock.patch.object(m, "send_alert", side_effect=AssertionError("must not send")), \
             self.assertRaises(OSError):
            m.main(["--check"])

    def test_alert_dependency_failure_does_not_persist_terminal_gate(self):
        with mock.patch.object(m, "classify", return_value=("STALE", "old")), \
             mock.patch.object(m, "load_state", return_value={}), \
             mock.patch.object(m, "resolve_alert_sender", side_effect=ImportError("missing")), \
             mock.patch.object(m, "save_state", side_effect=AssertionError("must remain rearmed")), \
             self.assertRaises(ImportError):
            m.main(["--check"])

    def test_open_incident_never_realerts_until_healthy(self):
        state = {"incident_open": True, "last_class": "STALE", "delivery_attempts": 1,
                 "delivered_id": VALID_ID, "alert_payload": "stable"}
        with mock.patch.object(m, "classify", return_value=("STALE", "old")), \
             mock.patch.object(m, "load_state", return_value=state), \
             mock.patch.object(m, "resolve_alert_sender", side_effect=AssertionError("must not resolve")), \
             mock.patch.object(m, "save_state", side_effect=AssertionError("must not save")), \
             mock.patch.object(m, "send_alert", side_effect=AssertionError("must not resend")):
            self.assertEqual(m.main(["--check"]), 0)

    def test_bounded_incident_does_not_resolve_sender(self):
        state = {"incident_open": True, "last_class": "STALE",
                 "delivery_attempts": m.MAX_DELIVERY_ATTEMPTS,
                 "delivered_id": None, "alert_payload": "stable"}
        with mock.patch.object(m, "classify", return_value=("STALE", "older")), \
             mock.patch.object(m, "load_state", return_value=state), \
             mock.patch.object(m, "resolve_alert_sender", side_effect=AssertionError("must not resolve")), \
             mock.patch.object(m, "save_state", side_effect=AssertionError("must not save")):
            self.assertEqual(m.main(["--check"]), 0)

    def test_failed_delivery_is_retried_once_then_bounded(self):
        state = {"incident_open": True, "last_class": "STALE",
                 "delivery_attempts": 1, "delivered_id": None, "alert_payload": "stable"}
        saved = []
        with mock.patch.object(m, "classify", return_value=("STALE", "old")), \
             mock.patch.object(m, "load_state", return_value=state), \
             mock.patch.object(m, "save_state", side_effect=lambda s: saved.append(dict(s))), \
             mock.patch.object(m, "send_alert", side_effect=RuntimeError("delivery failed")), \
             self.assertRaises(RuntimeError):
            m.main(["--check"])
        self.assertEqual(saved[-1]["delivery_attempts"], m.MAX_DELIVERY_ATTEMPTS)
        self.assertFalse(m.should_alert("STALE", NOW, state))

    def test_pre_attempt_state_failure_is_nonzero_and_sends_nothing(self):
        state = {"incident_open": True, "last_class": "STALE",
                 "delivery_attempts": 1, "delivered_id": None,
                 "alert_payload": "stable", "incident_key": "a" * 32}
        with mock.patch.object(m, "classify", return_value=("STALE", "old")), \
             mock.patch.object(m, "load_state", return_value=state), \
             mock.patch.object(m, "save_state", side_effect=OSError("ENOSPC")), \
             mock.patch.object(m, "send_alert", side_effect=AssertionError("must not send")), \
             self.assertRaises(OSError):
            m.main(["--check"])

    def test_healthy_state_failure_is_nonzero_for_onfailure(self):
        with mock.patch.object(m, "classify", return_value=("HEALTHY", "fresh")), \
             mock.patch.object(m, "load_state", return_value={}), \
             mock.patch.object(m, "save_state", side_effect=OSError("ENOSPC")), \
             self.assertRaises(OSError):
            m.main(["--check"])

    def test_confirmed_message_id_ends_retries(self):
        state = {"incident_open": True, "last_class": "STALE",
                 "delivery_attempts": 0, "delivered_id": None, "alert_payload": "stable"}
        with mock.patch.object(m, "classify", return_value=("STALE", "old")), \
             mock.patch.object(m, "load_state", return_value=state), \
             mock.patch.object(m, "save_state"), \
             mock.patch.object(m, "send_alert", return_value="durable-id"):
            self.assertEqual(m.main(["--check"]), 0)
        self.assertEqual(state["delivered_id"], "durable-id")
        self.assertFalse(m.should_alert("STALE", NOW, state))

    def test_first_failure_then_next_fire_delivers_and_stops(self):
        stored = {}
        sends = [RuntimeError("first failed"), VALID_ID]

        def save(state):
            stored.clear()
            stored.update(state)

        def send(*_args, **_kwargs):
            result = sends.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        common = (
            mock.patch.object(m, "classify", return_value=("STALE", "400s old")),
            mock.patch.object(m, "load_state", side_effect=lambda: dict(stored)),
            mock.patch.object(m, "save_state", side_effect=save),
            mock.patch.object(m, "send_alert", side_effect=send),
        )
        with common[0], common[1], common[2], common[3], self.assertRaises(RuntimeError):
            m.main(["--check"])
        self.assertEqual(stored["delivery_attempts"], 1)
        with mock.patch.object(m, "classify", return_value=("STALE", "700s old")), \
             mock.patch.object(m, "load_state", side_effect=lambda: dict(stored)), \
             mock.patch.object(m, "save_state", side_effect=save), \
             mock.patch.object(m, "send_alert", side_effect=send):
            self.assertEqual(m.main(["--check"]), 0)
        self.assertEqual(stored["delivery_attempts"], 2)
        self.assertEqual(stored["delivered_id"], VALID_ID)
        self.assertIn("400s old", stored["alert_payload"])

    def test_post_send_crash_then_consumed_retry_is_bounded_at_least_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "recovery"
            mailbox.provision(inbox)
            stored = {}
            confirmation_crashes = [True]

            def load():
                return dict(stored)

            def save(state):
                if state.get("delivered_id") and confirmation_crashes:
                    confirmation_crashes.pop()
                    raise OSError("simulated post-send crash")
                stored.clear()
                stored.update(state)

            with mock.patch.object(m, "classify", return_value=("STALE", "400s old")), \
                 mock.patch.object(m, "load_state", side_effect=load), \
                 mock.patch.object(m, "save_state", side_effect=save), \
                 mock.patch.object(agent_role_map, "mailbox_hub", return_value=Path(tmp)), \
                 self.assertRaises(OSError):
                m.main(["--check"])
            first_id = next((inbox / "new").iterdir()).name
            self.assertIsNone(stored.get("delivered_id"))
            (inbox / "new" / first_id).replace(inbox / "cur" / first_id)

            with mock.patch.object(m, "classify", return_value=("STALE", "700s old")), \
                 mock.patch.object(m, "load_state", side_effect=load), \
                 mock.patch.object(m, "save_state", side_effect=save), \
                 mock.patch.object(agent_role_map, "mailbox_hub", return_value=Path(tmp)):
                self.assertEqual(m.main(["--check"]), 0)
            pending = list((inbox / "new").iterdir())
            consumed = list((inbox / "cur").iterdir())
            self.assertEqual(len(pending), 1)
            self.assertEqual([path.name for path in consumed], [first_id])
            self.assertNotEqual(pending[0].name, first_id)
            self.assertEqual(stored["delivered_id"], pending[0].name)
            self.assertEqual(stored["delivery_attempts"], m.MAX_DELIVERY_ATTEMPTS)
            self.assertIn("400s old", stored["alert_payload"])


if __name__ == "__main__":
    unittest.main()
