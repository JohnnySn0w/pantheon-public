"""Subprocess fake of ``org.pantheon.HostApproval1.Consumer`` for the
T-2917 5a broker-side client private-bus test.

NOT a test module: zero test cases (same convention as
``tools/test_host_control_dbus_driver.py``). It exports ONLY the two
Consumer methods with the ratified signatures and scripted replies keyed
by approval handle, so ``DBusConsumerTransport`` is exercised over a real
private dbus-daemon without standing up the whole approval domain.

Usage: ``python3 -m tools.test_host_control_approval_client_driver
<bus_address> <ready_file>``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BOUND = "1" * 64
DENIED_EXPIRED = "2" * 64
UNKNOWN = "3" * 64
METADATA_DOWN = "4" * 64
DENIED_UNKNOWN_CODE = "5" * 64
AUTHORITY_RECORD = "e" * 64
PLAN = "e" * 64

_ERRORS = "org.pantheon.HostApproval1.Error"


def serve(bus_address: str, ready_file: str) -> int:
    import dbus
    import dbus.bus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    def named(name: str, body: str) -> "dbus.exceptions.DBusException":
        return dbus.exceptions.DBusException(body, name=f"{_ERRORS}.{name}")

    class FakeConsumer(dbus.service.Object):
        @dbus.service.method(
            "org.pantheon.HostApproval1.Consumer",
            in_signature="ssss",
            out_signature="ss",
        )
        def ValidateAndBind(self, approval_handle, plan_handle, expected_digest, operation_handle):
            if approval_handle == BOUND:
                return ("bound", AUTHORITY_RECORD)
            if approval_handle == DENIED_EXPIRED:
                raise named("BindDenied", "expired")
            if approval_handle == DENIED_UNKNOWN_CODE:
                raise named("BindDenied", "not-a-closed-code")
            if approval_handle == METADATA_DOWN:
                raise named("MetadataUnavailable", "broker metadata unavailable")
            raise named("UnknownHandle", "unknown handle")

        @dbus.service.method(
            "org.pantheon.HostApproval1.Consumer",
            in_signature="s",
            out_signature="ss",
        )
        def ApprovalStatus(self, approval_handle):
            if approval_handle == BOUND:
                payload = {
                    "schema": "pantheon-approval-status/v2",
                    "approval_handle": approval_handle,
                    "state": "bound",
                    "expires_at": "2026-10-10T01:00:00+00:00",
                    "summary": "bounded",
                    "plan_handle": PLAN,
                    "plan_owner": "alpha",
                }
                return ("bound", json.dumps(payload, sort_keys=True, separators=(",", ":")))
            raise named("UnknownHandle", "unknown handle")

    connection = dbus.bus.BusConnection(bus_address)
    bus_name = dbus.service.BusName("org.pantheon.HostApproval1", connection)
    service_object = FakeConsumer(connection, "/org/pantheon/HostApproval1")
    Path(ready_file).write_text("ready", encoding="utf-8")
    _keepalive = (bus_name, service_object)
    GLib.MainLoop().run()
    del _keepalive
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: test_host_control_approval_client_driver <bus_address> <ready_file>")
        return 2
    return serve(argv[1], argv[2])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
