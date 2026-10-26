"""A fake ``org.freedesktop.systemd1`` for the T-2917 5c.2 user-bus tests.

Not a test module (leading underscore, the ``_host_control_testbus.py``
precedent). Run as::

    /usr/bin/python3 -m tools._host_control_fake_systemd1 <bus_address> <scenario.json> <calls.jsonl>

It owns ``org.freedesktop.systemd1`` on the given PRIVATE bus and exports
the Manager object plus one object per loaded unit, returning correctly
TYPED values (InvocationID ``ay``, Job ``(uo)``, SuccessExitStatus
``(aiai)``, NextElapse* ``t``, ExecMainStatus ``i``, MainPID ``u``, NRestarts ``u``,
NeedDaemonReload ``b``, EnvironmentFiles ``a(sb)``). Every call is appended
to ``calls.jsonl`` as ``{"member", "interface", "path", "args"}`` and
flushed per call.

Scenario JSON (all keys optional)::

    {"system_state": "running", "version": "255-fake", "environment": ["PATH=/x"],
     "job_polls": 1,                       # GetJob answers a job this many times
     "units": {"<unit>": {
         "props": {"LoadState": "loaded", "InvocationID": "<hex>", "Job": [0, "/"],
                   "SuccessExitStatus": [[3, 4], ["SIGTERM"|15]], ...},
         "unit_file_state": "disabled",   # absent/null -> unit_file_error
         "unit_file_error": "org.freedesktop.systemd1.NoSuchUnit",
         "load_error": "<error name>",    # LoadUnit raises
         "reset_failed_error": "<error name>",
         "on_start": [{"loads": 1, "props": {...}}, {"loads": 0, "props": {...}}],
         "on_stop": {...props}, "on_reset_failed": {...props},
         "on_enable": {...props}, "on_disable": {...props},
         "enable_symlink": {"link": "<abs path>", "target": "<abs path>"}}}}

``on_start`` phases: phase 0 applies at StartUnit and is observed by the
next ``loads`` LoadUnit calls; then the next phase applies (``loads`` 0 is
final). An unknown unit loads as ``not-found``/``inactive``/``dead``.

Fixture evidence only: agreement with a real systemd user manager is T-2924.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

NAME = "org.freedesktop.systemd1"
MANAGER_PATH = "/org/freedesktop/systemd1"
MANAGER_IFACE = "org.freedesktop.systemd1.Manager"
UNIT_IFACE = "org.freedesktop.systemd1.Unit"
SERVICE_IFACE = "org.freedesktop.systemd1.Service"
TIMER_IFACE = "org.freedesktop.systemd1.Timer"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
JOB_PREFIX = "/org/freedesktop/systemd1/job/"

PROPERTY_INTERFACES = {
    "LoadState": UNIT_IFACE,
    "ActiveState": UNIT_IFACE,
    "SubState": UNIT_IFACE,
    "UnitFileState": UNIT_IFACE,
    "InvocationID": UNIT_IFACE,
    "Job": UNIT_IFACE,
    "NeedDaemonReload": UNIT_IFACE,
    "Result": SERVICE_IFACE,
    "ExecMainStatus": SERVICE_IFACE,
    "SuccessExitStatus": SERVICE_IFACE,
    "MainPID": SERVICE_IFACE,
    "NRestarts": SERVICE_IFACE,
    "EnvironmentFiles": SERVICE_IFACE,
    "NextElapseUSecRealtime": TIMER_IFACE,
    "NextElapseUSecMonotonic": TIMER_IFACE,
}

NOT_FOUND_PROPS = {
    "LoadState": "not-found",
    "ActiveState": "inactive",
    "SubState": "dead",
    "UnitFileState": "",
    "InvocationID": "",
    "Job": [0, "/"],
    "NeedDaemonReload": False,
    "Result": "success",
    "ExecMainStatus": 0,
    "SuccessExitStatus": [[], []],
    "MainPID": 0,
    "NRestarts": 0,
    "EnvironmentFiles": [],
    "NextElapseUSecRealtime": 0,
    "NextElapseUSecMonotonic": 0,
}

_SIGNALS = {"SIGHUP": 1, "SIGINT": 2, "SIGTERM": 15, "SIGKILL": 9, "SIGUSR1": 10, "SIGUSR2": 12}


def _error(name: str, message: str = "") -> dbus.exceptions.DBusException:
    return dbus.exceptions.DBusException(message or name, name=name)


def typed(name: str, value: Any) -> Any:
    """Scenario JSON value -> the D-Bus type real systemd uses."""

    if name == "InvocationID":
        return dbus.ByteArray(bytes.fromhex(str(value)))
    if name == "Job":
        job_id, path = value
        return dbus.Struct((dbus.UInt32(int(job_id)), dbus.ObjectPath(str(path))), signature="uo")
    if name == "SuccessExitStatus":
        codes, signals = value
        signal_numbers = [_SIGNALS[item] if isinstance(item, str) else int(item) for item in signals]
        return dbus.Struct(
            (
                dbus.Array([dbus.Int32(int(code)) for code in codes], signature="i"),
                dbus.Array([dbus.Int32(number) for number in signal_numbers], signature="i"),
            ),
            signature="aiai",
        )
    if name in ("NextElapseUSecRealtime", "NextElapseUSecMonotonic"):
        return dbus.UInt64(int(value))
    if name == "ExecMainStatus":
        return dbus.Int32(int(value))
    if name in ("MainPID", "NRestarts"):
        return dbus.UInt32(int(value))
    if name == "NeedDaemonReload":
        return dbus.Boolean(bool(value))
    if name == "EnvironmentFiles":
        return dbus.Array(
            [dbus.Struct((dbus.String(path), dbus.Boolean(bool(flag))), signature="sb") for path, flag in value],
            signature="(sb)",
        )
    return dbus.String(str(value))


def _jsonable(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, dbus.Boolean):
        return bool(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, int):
        return int(value)
    return str(value)


def escape_unit(unit: str) -> str:
    out = []
    for character in unit:
        if character.isalnum() and character.isascii():
            out.append(character)
        else:
            out.append(f"_{ord(character):02x}")
    return MANAGER_PATH + "/unit/" + "".join(out)


class FakeSystemd1:
    def __init__(self, connection: dbus.bus.BusConnection, scenario: dict, calls_path: Path) -> None:
        self.connection = connection
        self.scenario = scenario
        self.calls = open(calls_path, "a", encoding="utf-8")  # noqa: SIM115 - closed at exit
        self.units: dict[str, dict] = {}
        self.objects: dict[str, "UnitObject"] = {}
        self.jobs: dict[int, int] = {}
        self.next_job = 1
        for unit, spec in scenario.get("units", {}).items():
            self.units[unit] = self._unit_state(spec)

    @staticmethod
    def _unit_state(spec: dict) -> dict:
        props = dict(NOT_FOUND_PROPS)
        props.update(spec.get("props", {}))
        return {"spec": spec, "props": props, "script": None}

    def record(self, member: str, interface: str, path: str, args: tuple) -> None:
        entry = {"member": member, "interface": interface, "path": path, "args": [_jsonable(item) for item in args]}
        self.calls.write(json.dumps(entry, sort_keys=True) + "\n")
        self.calls.flush()

    def unit(self, name: str) -> dict:
        if name not in self.units:
            self.units[name] = self._unit_state({})
        return self.units[name]

    def tick(self, state: dict) -> None:
        script = state["script"]
        if not script:
            return
        if script["remaining"] <= 0 and script["index"] + 1 < len(script["phases"]):
            script["index"] += 1
            phase = script["phases"][script["index"]]
            state["props"].update(phase.get("props", {}))
            script["remaining"] = int(phase.get("loads", 0))
        script["remaining"] -= 1

    def new_job(self) -> str:
        job_id = self.next_job
        self.next_job += 1
        self.jobs[job_id] = int(self.scenario.get("job_polls", 1))
        return f"{JOB_PREFIX}{job_id}"

    def object_for(self, unit: str) -> str:
        path = escape_unit(unit)
        if path not in self.objects:
            self.objects[path] = UnitObject(self.connection, path, self, unit)
        return path


class ManagerObject(dbus.service.Object):
    def __init__(self, connection: dbus.bus.BusConnection, fake: FakeSystemd1) -> None:
        super().__init__(connection, MANAGER_PATH)
        self.fake = fake

    def _r(self, member: str, *args: Any) -> None:
        self.fake.record(member, MANAGER_IFACE, MANAGER_PATH, args)

    @dbus.service.method(PROPS_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface: str, name: str) -> Any:
        self.fake.record("Get", PROPS_IFACE, MANAGER_PATH, (interface, name))
        if interface != MANAGER_IFACE:
            raise _error("org.freedesktop.DBus.Error.InvalidArgs", "wrong interface")
        scenario = self.fake.scenario
        if name == "SystemState":
            return dbus.String(scenario.get("system_state", "running"))
        if name == "Version":
            return dbus.String(scenario.get("version", "255-fake"))
        if name == "Environment":
            return dbus.Array([dbus.String(item) for item in scenario.get("environment", ["PATH=/fake"])], signature="s")
        raise _error("org.freedesktop.DBus.Error.UnknownProperty", name)

    @dbus.service.method(MANAGER_IFACE, in_signature="s", out_signature="o")
    def LoadUnit(self, unit: str) -> str:
        self._r("LoadUnit", unit)
        state = self.fake.unit(str(unit))
        error = state["spec"].get("load_error")
        if error:
            raise _error(error, f"cannot load {unit}")
        self.fake.tick(state)
        return dbus.ObjectPath(self.fake.object_for(str(unit)))

    @dbus.service.method(MANAGER_IFACE, in_signature="s", out_signature="s")
    def GetUnitFileState(self, unit: str) -> str:
        self._r("GetUnitFileState", unit)
        spec = self.fake.units.get(str(unit), {"spec": {}})["spec"]
        state = spec.get("unit_file_state")
        if state is None:
            raise _error(spec.get("unit_file_error", "org.freedesktop.systemd1.NoSuchUnit"), f"no unit file {unit}")
        return dbus.String(state)

    @dbus.service.method(MANAGER_IFACE, in_signature="", out_signature="")
    def Reload(self) -> None:
        self._r("Reload")
        error = self.fake.scenario.get("reload_error")
        if error:
            raise _error(error, "reload refused")

    @dbus.service.method(MANAGER_IFACE, in_signature="s", out_signature="")
    def ResetFailedUnit(self, unit: str) -> None:
        self._r("ResetFailedUnit", unit)
        state = self.fake.unit(str(unit))
        error = state["spec"].get("reset_failed_error")
        if error:
            raise _error(error, f"cannot reset {unit}")
        state["props"].update(state["spec"].get("on_reset_failed", {}))

    @dbus.service.method(MANAGER_IFACE, in_signature="ss", out_signature="o")
    def StartUnit(self, unit: str, mode: str) -> str:
        self._r("StartUnit", unit, mode)
        state = self.fake.unit(str(unit))
        error = state["spec"].get("start_error")
        if error:
            raise _error(error, f"cannot start {unit}")
        phases = state["spec"].get("on_start") or []
        if phases:
            state["props"].update(phases[0].get("props", {}))
            state["script"] = {"phases": phases, "index": 0, "remaining": int(phases[0].get("loads", 0))}
        return dbus.ObjectPath(self.fake.new_job())

    @dbus.service.method(MANAGER_IFACE, in_signature="ss", out_signature="o")
    def StopUnit(self, unit: str, mode: str) -> str:
        self._r("StopUnit", unit, mode)
        state = self.fake.unit(str(unit))
        error = state["spec"].get("stop_error")
        if error:
            raise _error(error, f"Unit {unit} not loaded.")
        state["script"] = None
        state["props"].update(state["spec"].get("on_stop", {}))
        return dbus.ObjectPath(self.fake.new_job())

    @dbus.service.method(MANAGER_IFACE, in_signature="asbb", out_signature="ba(sss)")
    def EnableUnitFiles(self, files: list, runtime: bool, force: bool) -> tuple:
        self._r("EnableUnitFiles", list(files), bool(runtime), bool(force))
        changes = []
        for unit in files:
            state = self.fake.unit(str(unit))
            error = state["spec"].get("enable_error")
            if error:
                raise _error(error, f"cannot enable {unit}")
            link = state["spec"].get("enable_symlink")
            if link:
                Path(link["link"]).parent.mkdir(parents=True, exist_ok=True)
                if not os.path.lexists(link["link"]):
                    os.symlink(link["target"], link["link"])
                changes.append(("symlink", link["link"], link["target"]))
            state["props"].update(state["spec"].get("on_enable", {}))
            state["spec"]["unit_file_state"] = "enabled"
        return (
            dbus.Boolean(True),
            dbus.Array([dbus.Struct(item, signature="sss") for item in changes], signature="(sss)"),
        )

    @dbus.service.method(MANAGER_IFACE, in_signature="asb", out_signature="a(sss)")
    def DisableUnitFiles(self, files: list, runtime: bool) -> list:
        self._r("DisableUnitFiles", list(files), bool(runtime))
        changes = []
        for unit in files:
            state = self.fake.unit(str(unit))
            link = state["spec"].get("enable_symlink")
            if link and os.path.lexists(link["link"]):
                os.unlink(link["link"])
                changes.append(("unlink", link["link"], ""))
            state["props"].update(state["spec"].get("on_disable", {}))
            state["spec"]["unit_file_state"] = "disabled"
        return dbus.Array([dbus.Struct(item, signature="sss") for item in changes], signature="(sss)")

    @dbus.service.method(MANAGER_IFACE, in_signature="u", out_signature="o")
    def GetJob(self, job_id: int) -> str:
        self._r("GetJob", int(job_id))
        remaining = self.fake.jobs.get(int(job_id), 0)
        if remaining <= 0:
            self.fake.jobs.pop(int(job_id), None)
            raise _error("org.freedesktop.systemd1.NoSuchJob", f"Job {job_id} does not exist.")
        self.fake.jobs[int(job_id)] = remaining - 1
        return dbus.ObjectPath(f"{JOB_PREFIX}{int(job_id)}")


class UnitObject(dbus.service.Object):
    def __init__(self, connection: dbus.bus.BusConnection, path: str, fake: FakeSystemd1, unit: str) -> None:
        super().__init__(connection, path)
        self.fake = fake
        self.unit = unit
        self.path = path

    @dbus.service.method(PROPS_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface: str, name: str) -> Any:
        self.fake.record("Get", PROPS_IFACE, self.path, (interface, name))
        expected = PROPERTY_INTERFACES.get(str(name))
        if expected is None:
            raise _error("org.freedesktop.DBus.Error.UnknownProperty", name)
        if expected != interface:
            raise _error("org.freedesktop.DBus.Error.InvalidArgs", f"{name} is not on {interface}")
        props = self.fake.unit(self.unit)["props"]
        if str(name) not in props:
            raise _error("org.freedesktop.DBus.Error.UnknownProperty", name)
        return typed(str(name), props[str(name)])


def main(argv: list[str]) -> int:
    address, scenario_path, calls_path = argv
    scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8"))
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    connection = dbus.bus.BusConnection(address)
    fake = FakeSystemd1(connection, scenario, Path(calls_path))
    _manager = ManagerObject(connection, fake)
    _name = dbus.service.BusName(NAME, connection, do_not_queue=True)
    loop = GLib.MainLoop()
    signal.signal(signal.SIGTERM, lambda *_: loop.quit())
    loop.run()
    del _manager, _name
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
