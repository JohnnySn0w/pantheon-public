"""Tests for `tools.persona_controller_health_dbus` (T-2969 BT3).

Pure XML/marshalling helpers only -- no dbus import required, always run,
fast and deterministic.

**No committed live private-bus tier (T-2969 DECISION).** A live
`dbus-daemon --session` + `start_service()` integration tier was written
and did catch a real bug (`start_service` originally discarded the
`dbus.service.BusName` return value; it was garbage-collected almost
immediately, releasing the claimed name -- fixed by retaining the handle
on the exported object, see `PersonaController1Object.__init__`'s
`bus_name_handle`). That fix was verified correct by a standalone script
run directly against a private bus (daemon spinup, `SessionBus()`
connect, name claim+retention, a real wire-level `GetAll()` round-trip
via a second connection -- all completed in under two seconds).

The SAME operations, run inside a `unittest.TestCase` under this
environment's pytest, reproducibly hung past 60-90s regardless of which
specific dbus operation the first live-bus test performed (including a
no-op `emit_properties_changed()` smoke) -- narrowed to `setUp`/cleanup
combined with the background GLib-mainloop-thread pattern
(`ensure_glib_mainloop_running`), not the module's actual logic (already
proven correct standalone). This matches why the repo's existing D-Bus
precedent (`tools/test_host_control_health_dbus.py`) always exports its
object in a DEDICATED SUBPROCESS rather than a same-process background
thread -- avoiding exactly this class of interaction. Reproducing that
full subprocess-driver shape (~300+ lines) for what the ticket's own
verification target calls "nice-to-have" was judged not worth the
footprint (P7) here; a hanging test would also be strictly worse than no
test (it would block the smoke loop indefinitely). See T-2969 DECISIONS
for the full writeup and a pointer to the verification script.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import persona_controller_health_dbus as pchd

REPO = Path(__file__).resolve().parent.parent

# Captured from origin/main d7e4cb5eb BEFORE T-5137 edits (flag-off identity golden), then
# APPENDED with the six ORG-106 L4a (T-5527) SeatState* properties: the nine-property prefix
# is byte-identical to the original capture; seat state is additive and not flag-gated.
GOLDEN_XML = (
    '  <interface name="org.pantheon.PersonaController1">\n'
    '    <property name="ActiveRuntime" type="s" access="read"/>\n'
    '    <property name="ViabilityStatus" type="s" access="read"/>\n'
    '    <property name="LastSelectionTs" type="s" access="read"/>\n'
    '    <property name="LastFailureClass" type="s" access="read"/>\n'
    '    <property name="NextCooldownExpiryTs" type="s" access="read"/>\n'
    '    <property name="Holds" type="as" access="read"/>\n'
    '    <property name="ModalPresent" type="b" access="read"/>\n'
    '    <property name="ModalKind" type="s" access="read"/>\n'
    '    <property name="ModalResetAt" type="s" access="read"/>\n'
    '    <property name="SeatState" type="s" access="read"/>\n'
    '    <property name="SeatStateSources" type="as" access="read"/>\n'
    '    <property name="SeatStateOwner" type="s" access="read"/>\n'
    '    <property name="SeatStateReason" type="s" access="read"/>\n'
    '    <property name="SeatStateExpires" type="s" access="read"/>\n'
    '    <property name="SeatStateFault" type="s" access="read"/>\n'
    '  </interface>\n'
)
NINE = (
    "ActiveRuntime", "ViabilityStatus", "LastSelectionTs", "LastFailureClass",
    "NextCooldownExpiryTs", "Holds", "ModalPresent", "ModalKind", "ModalResetAt",
)
SEAT = (
    ("SeatState", "s"), ("SeatStateSources", "as"), ("SeatStateOwner", "s"),
    ("SeatStateReason", "s"), ("SeatStateExpires", "s"), ("SeatStateFault", "s"),
)
LEG = (
    ("ContractRevision", "s"), ("Status", "s"), ("Subcomponents", "a{ss}"),
    ("Reasons", "a{ss}"), ("Revisions", "a{ss}"), ("InvariantListRef", "s"),
    ("SecurityDrift", "b"),
)


class PersonaControllerIntrospectionTests(unittest.TestCase):
    def test_bus_name_and_object_path_are_persona_parameterized(self) -> None:
        self.assertEqual(pchd.bus_name_for("orchestrator"), "org.pantheon.PersonaController.orchestrator")
        self.assertEqual(pchd.object_path_for("orchestrator"), "/org/pantheon/PersonaController/orchestrator")

    def test_interface_name_is_persona_independent(self) -> None:
        # T-2969 DECISION: the interface stays ONE string across every
        # persona (Recovery's T-2959 monitor binds it once); only the bus
        # name/object path vary per persona.
        self.assertEqual(pchd.PERSONA_CONTROLLER_INTERFACE, "org.pantheon.PersonaController1")

    def test_interface_xml_declares_all_nine_documented_properties(self) -> None:
        xml = pchd.persona_controller_interface_xml()
        for name in (
            "ActiveRuntime", "ViabilityStatus", "LastSelectionTs", "LastFailureClass",
            "NextCooldownExpiryTs", "Holds",
            "ModalPresent", "ModalKind", "ModalResetAt",  # T-4047
        ):
            self.assertIn(f'name="{name}"', xml)
        self.assertIn('name="Holds" type="as"', xml)
        self.assertIn('name="ActiveRuntime" type="s"', xml)
        self.assertIn('name="ModalPresent" type="b"', xml)
        self.assertIn('name="ModalKind" type="s"', xml)
        self.assertIn('name="ModalResetAt" type="s"', xml)

    def test_add_persona_controller_introspection_inserts_before_closing_node(self) -> None:
        base = "<node>\n  <interface name=\"org.freedesktop.DBus.Introspectable\"/>\n</node>"
        merged = pchd.add_persona_controller_introspection(base)
        self.assertIn(pchd.PERSONA_CONTROLLER_INTERFACE, merged)
        self.assertTrue(merged.strip().endswith("</node>"))

    def test_add_persona_controller_introspection_requires_closing_node(self) -> None:
        with self.assertRaises(ValueError):
            pchd.add_persona_controller_introspection("<node>")


class HealthLegFlagOffIdentityTests(unittest.TestCase):
    def test_default_xml_equals_pre_t5137_golden(self) -> None:
        self.assertEqual(pchd.persona_controller_interface_xml(), GOLDEN_XML)
        self.assertEqual(pchd.persona_controller_interface_xml(health_leg=False), GOLDEN_XML)

    def test_base_property_tables_unchanged(self) -> None:
        self.assertEqual(pchd.PERSONA_CONTROLLER_PROPERTIES, NINE + tuple(n for n, _ in SEAT))
        self.assertEqual(tuple(pchd.PERSONA_CONTROLLER_PROPERTY_SIGNATURES), NINE + tuple(n for n, _ in SEAT))
        self.assertEqual({n: pchd.PERSONA_CONTROLLER_PROPERTY_SIGNATURES[n] for n, _ in SEAT}, dict(SEAT))

    def test_default_introspection_equals_base_plus_golden(self) -> None:
        base = "<node>\n</node>"
        self.assertEqual(
            pchd.add_persona_controller_introspection(base), "<node>\n" + GOLDEN_XML + "</node>"
        )

    def test_flag_parse_only_exact_on(self) -> None:
        from tools import runtime_controller as rc

        self.assertEqual(rc.PERSONA_HEALTH_LEG_ENV, "PANTHEON_PERSONA_HEALTH_LEG")
        for value, expected in (("on", True), ("ON", False), ("1", False), ("true", False), ("", False), ("off", False)):
            with mock.patch.dict(os.environ, {rc.PERSONA_HEALTH_LEG_ENV: value}):
                self.assertIs(rc._health_leg_enabled(), expected, value)
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop(rc.PERSONA_HEALTH_LEG_ENV, None)
            self.assertIs(rc._health_leg_enabled(), False)

    def test_flag_off_wiring_is_unchanged_and_imports_no_germinate(self) -> None:
        # Fresh interpreter: with the env unset, bootstrapping the service must call
        # start_service(persona, provider) exactly as before and never import the
        # leaf module or tools.germinate.
        code = (
            "import sys\n"
            "from unittest import mock\n"
            "from tools import runtime_controller as rc\n"
            "from tools import persona_controller_health_dbus as p\n"
            "seen = {}\n"
            "def fake(*a, **k):\n"
            "    seen['a'], seen['k'] = a, k\n"
            "    seen['payload'] = a[1]()\n"
            "    return 'svc'\n"
            "with mock.patch.object(p, 'DBUS_AVAILABLE', True), mock.patch.object(p, 'start_service', fake, create=True), "
            "mock.patch.object(rc, 'dbus_health_properties', return_value={'ActiveRuntime': ''}):\n"
            "    r = rc._start_persona_controller_dbus_service('orchestrator')\n"
            "assert r == 'svc', r\n"
            "assert seen['k'] == {} and len(seen['a']) == 2, seen\n"
            "assert seen['payload'] == {'ActiveRuntime': ''}, seen\n"
            "bad = [m for m in sys.modules if m.startswith('tools.germinate') or m == 'tools.persona_controller_health_leg']\n"
            "assert not bad, bad\n"
            "print('OK')\n"
        )
        env = {k: v for k, v in os.environ.items() if k != "PANTHEON_PERSONA_HEALTH_LEG"}
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, timeout=120
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OK", proc.stdout)


class HealthLegFlagOnTests(unittest.TestCase):
    def test_xml_appends_seven_properties_after_unchanged_nine(self) -> None:
        xml = pchd.persona_controller_interface_xml(health_leg=True)
        self.assertTrue(xml.startswith(GOLDEN_XML.rsplit("  </interface>\n", 1)[0]))
        tail = xml.split('name="SeatStateFault" type="s" access="read"/>\n', 1)[1]
        expected = "".join(f'    <property name="{n}" type="{t}" access="read"/>\n' for n, t in LEG)
        self.assertEqual(tail, expected + "  </interface>\n")

    def test_leg_tables(self) -> None:
        self.assertEqual(pchd.HEALTH_LEG_PROPERTIES, tuple(n for n, _ in LEG))
        self.assertEqual(pchd.HEALTH_LEG_SIGNATURES, dict(LEG))

    def test_introspection_flag_on(self) -> None:
        merged = pchd.add_persona_controller_introspection("<node></node>", health_leg=True)
        self.assertIn('name="SecurityDrift" type="b"', merged)

    def test_flag_on_wiring_merges_leg_into_provider(self) -> None:
        from tools import runtime_controller as rc

        seen = {}

        def fake(persona, provider, **kw):
            seen["kw"], seen["payload"] = kw, provider()
            return "svc"

        leg_out = {"Status": "unhealthy"}
        with mock.patch.dict(os.environ, {rc.PERSONA_HEALTH_LEG_ENV: "on"}), \
                mock.patch.object(pchd, "DBUS_AVAILABLE", True), \
                mock.patch.object(pchd, "start_service", fake, create=True), \
                mock.patch.object(rc, "dbus_health_properties", return_value={"ActiveRuntime": ""}), \
                mock.patch("tools.persona_controller_health_leg.snapshot", return_value=leg_out):
            self.assertEqual(rc._start_persona_controller_dbus_service("orchestrator"), "svc")
        self.assertEqual(seen["kw"], {"health_leg": True})
        self.assertEqual(seen["payload"], {"ActiveRuntime": "", "Status": "unhealthy"})

    @unittest.skipUnless(pchd.DBUS_AVAILABLE, "dbus-python not available")
    def test_marshal_dict_of_strings(self) -> None:
        import dbus

        v = pchd._marshal_value("Subcomponents", {"a": "ready"})
        self.assertIsInstance(v, dbus.Dictionary)
        self.assertEqual(v.signature, "ss")
        self.assertEqual(dict(v), {"a": "ready"})
        self.assertEqual(bool(pchd._marshal_value("SecurityDrift", True)), True)


class SeatStatePropertiesTests(unittest.TestCase):
    """ORG-106 L4a (T-5527): the pure payload function. No dbus needed."""

    def _write(self, root: Path, doc: dict) -> None:
        p = root / "personas" / "orchestrator" / "overrides" / "seat-state.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps(doc), encoding="utf-8")

    def test_no_record_reads_active_with_empty_strings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            got = pchd.seat_state_properties("orchestrator", state_root=Path(tmp))
        self.assertEqual(got["SeatState"], "active")
        self.assertEqual(got["SeatStateSources"], [])
        for k in ("SeatStateOwner", "SeatStateReason", "SeatStateExpires", "SeatStateFault"):
            self.assertEqual(got[k], "", k)
        self.assertEqual(set(got), {n for n, _ in SEAT})

    def test_paused_record_is_published(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._write(Path(tmp), {
                "schema": "pantheon-seat-state/v1", "state": "paused-intake", "phase": "set",
                "reason": "ship freeze", "owner": "operator", "indefinite": True, "expires": None,
                "set_ts": "2026-10-26T00:00:00Z", "phase_ts": "2026-10-26T00:00:00Z"})
            got = pchd.seat_state_properties("orchestrator", state_root=Path(tmp))
        self.assertEqual(got["SeatState"], "paused-intake")
        self.assertEqual(got["SeatStateSources"], ["seat-state"])
        self.assertEqual(got["SeatStateReason"], "ship freeze")
        self.assertEqual(got["SeatStateExpires"], "")

    def test_reader_fault_is_fail_open_active_with_fault(self) -> None:
        def boom(_p):
            raise OSError("nope")

        got = pchd.seat_state_properties("orchestrator", effective_fn=boom)
        self.assertEqual(got["SeatState"], "active")
        self.assertEqual(got["SeatStateFault"], "seat-state: OSError")

    def test_source_fault_is_carried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "personas" / "orchestrator" / "overrides" / "seat-state.json"
            p.parent.mkdir(parents=True)
            p.write_text("{not json", encoding="utf-8")
            got = pchd.seat_state_properties("orchestrator", state_root=Path(tmp))
        self.assertEqual(got["SeatState"], "active")
        self.assertTrue(got["SeatStateFault"].startswith("seat-state:"))


if __name__ == "__main__":
    unittest.main()
