"""Subprocess driver for tools/test_persona_controller_health_dbus_e2e.py (T-5410).

Runs ONLY against the scratch bus named by DBUS_SESSION_BUS_ADDRESS (the test sets it
explicitly). Refuses to start unless the address is a private unix:path socket.

Modes (argv[1]):
  serve  <workdir>   -- the production bootstrap `_start_persona_controller_dbus_service`
                        with the two live-state reads replaced by file-backed stubs
                        (`<workdir>/state.json`); writes `<workdir>/ready`; parks, and emits
                        PropertiesChanged whenever `<workdir>/emit` appears.
  introspect <outfile> -- Introspect + Get(leg property), reports what the wiring flag exposes.
  listen <outfile>   -- subscribes to PropertiesChanged, writes the first one to outfile.
  getall <outfile>   -- dbus-python GetAll, writes {name: [signature, value]} to outfile.
Every mode has a hard wall-clock bound.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

PERSONA = "e2e"
IFACE = "org.pantheon.PersonaController1"
BOUND_S = 40.0


def _require_scratch_bus() -> str:
    addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not addr.startswith("unix:path=/tmp/"):
        sys.exit(f"refusing: not a scratch-bus address: {addr!r}")
    return addr


def signature_of(value: object) -> str:
    import dbus

    if isinstance(value, dbus.Boolean):
        return "b"
    if isinstance(value, dbus.String):
        return "s"
    if isinstance(value, dbus.Dictionary):
        return f"a{{{value.signature}}}"
    if isinstance(value, dbus.Array):
        return f"a{value.signature}"
    raise AssertionError(f"unhandled type {type(value)}")


def _plain(value: object) -> object:
    import dbus

    if isinstance(value, dbus.Boolean):
        return bool(value)
    if isinstance(value, dbus.Dictionary):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, dbus.Array):
        return [str(v) for v in value]
    return str(value)


def _snapshot(props: object) -> dict:
    return {str(k): [signature_of(v), _plain(v)] for k, v in props.items()}


def serve(workdir: Path) -> None:
    from tools import persona_controller_health_leg as leg
    from tools import runtime_controller as rc

    state_path = workdir / "state.json"

    def read() -> dict:
        return json.loads(state_path.read_text(encoding="utf-8"))

    rc.dbus_health_properties = lambda persona, **_kw: read()["base"]  # type: ignore[assignment]
    leg.snapshot = lambda persona, **_kw: read()["leg"]  # type: ignore[assignment]
    service = rc._start_persona_controller_dbus_service(PERSONA)
    if service is None:
        sys.exit("production bootstrap returned None")
    (workdir / "ready").write_text("1", encoding="utf-8")
    deadline = time.monotonic() + BOUND_S * 3
    emit = workdir / "emit"
    while time.monotonic() < deadline:
        if emit.exists():
            emit.unlink()
            rc._dbus_emit_health(PERSONA, service)
        time.sleep(0.05)


def _bus():
    import dbus.bus

    return dbus.bus.BusConnection(_require_scratch_bus())


def getall(outfile: Path) -> None:
    from tools.persona_controller_health_dbus import object_path_for, bus_name_for
    import dbus

    obj = _bus().get_object(bus_name_for(PERSONA), object_path_for(PERSONA))
    props = obj.GetAll(IFACE, dbus_interface="org.freedesktop.DBus.Properties", timeout=20)
    outfile.write_text(json.dumps(_snapshot(props), sort_keys=True), encoding="utf-8")


def introspect(outfile: Path) -> None:
    """Writes {"xml_props": {name: type}, "get_leg": "ok"|<error name>} -- Introspect and Get
    are the surfaces that honour the health_leg wiring flag (GetAll marshals whatever the
    provider returns)."""
    import xml.etree.ElementTree as ET
    from tools.persona_controller_health_dbus import object_path_for, bus_name_for

    obj = _bus().get_object(bus_name_for(PERSONA), object_path_for(PERSONA))
    xml = str(obj.Introspect(dbus_interface="org.freedesktop.DBus.Introspectable", timeout=20))
    props = {}
    for iface in ET.fromstring(xml).iter("interface"):
        if iface.get("name") == IFACE:
            props = {p.get("name"): p.get("type") for p in iface.iter("property")}
    try:
        obj.Get(IFACE, "SecurityDrift", dbus_interface="org.freedesktop.DBus.Properties", timeout=20)
        get_leg = "ok"
    except Exception as exc:  # noqa: BLE001 - reporting the error name is the point
        get_leg = exc.get_dbus_name() if hasattr(exc, "get_dbus_name") else repr(exc)
    outfile.write_text(json.dumps({"xml_props": props, "get_leg": get_leg}), encoding="utf-8")


def listen(outfile: Path) -> None:
    import dbus.mainloop.glib
    from gi.repository import GLib
    from tools.persona_controller_health_dbus import object_path_for

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = _bus()
    loop = GLib.MainLoop()

    def on_signal(iface, changed, invalidated):
        outfile.with_suffix(".tmp").write_text(
            json.dumps({"iface": str(iface), "changed": _snapshot(changed),
                        "invalidated": [str(i) for i in invalidated]}, sort_keys=True),
            encoding="utf-8")
        outfile.with_suffix(".tmp").rename(outfile)
        loop.quit()

    bus.add_signal_receiver(
        on_signal, signal_name="PropertiesChanged",
        dbus_interface="org.freedesktop.DBus.Properties", path=object_path_for(PERSONA))
    bus.get_name_owner("org.freedesktop.DBus")  # round-trip: the match rule is installed
    outfile.with_suffix(".ready").write_text("1", encoding="utf-8")
    GLib.timeout_add_seconds(int(BOUND_S), loop.quit)
    loop.run()


def main(argv: list[str]) -> None:
    _require_scratch_bus()
    mode, target = argv[1], Path(argv[2])
    {"serve": serve, "getall": getall, "introspect": introspect, "listen": listen}[mode](target)


if __name__ == "__main__":
    main(sys.argv)
