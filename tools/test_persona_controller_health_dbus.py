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

import unittest

from tools import persona_controller_health_dbus as pchd


class PersonaControllerIntrospectionTests(unittest.TestCase):
    def test_bus_name_and_object_path_are_persona_parameterized(self) -> None:
        self.assertEqual(pchd.bus_name_for("orchestrator"), "org.pantheon.PersonaController.orchestrator")
        self.assertEqual(pchd.object_path_for("orchestrator"), "/org/pantheon/PersonaController/orchestrator")

    def test_interface_name_is_persona_independent(self) -> None:
        # T-2969 DECISION: the interface stays ONE string across every
        # persona (Recovery's T-2959 monitor binds it once); only the bus
        # name/object path vary per persona.
        self.assertEqual(pchd.PERSONA_CONTROLLER_INTERFACE, "org.pantheon.PersonaController1")

    def test_interface_xml_declares_all_six_documented_properties(self) -> None:
        xml = pchd.persona_controller_interface_xml()
        for name in ("ActiveRuntime", "ViabilityStatus", "LastSelectionTs", "LastFailureClass", "NextCooldownExpiryTs", "Holds"):
            self.assertIn(f'name="{name}"', xml)
        self.assertIn('name="Holds" type="as"', xml)
        self.assertIn('name="ActiveRuntime" type="s"', xml)

    def test_add_persona_controller_introspection_inserts_before_closing_node(self) -> None:
        base = "<node>\n  <interface name=\"org.freedesktop.DBus.Introspectable\"/>\n</node>"
        merged = pchd.add_persona_controller_introspection(base)
        self.assertIn(pchd.PERSONA_CONTROLLER_INTERFACE, merged)
        self.assertTrue(merged.strip().endswith("</node>"))

    def test_add_persona_controller_introspection_requires_closing_node(self) -> None:
        with self.assertRaises(ValueError):
            pchd.add_persona_controller_introspection("<node>")


if __name__ == "__main__":
    unittest.main()
