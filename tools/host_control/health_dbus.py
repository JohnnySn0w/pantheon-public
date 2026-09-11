"""D-Bus Properties surface for ``org.pantheon.Health1``."""

from __future__ import annotations

from typing import Final, Protocol

from tools.host_control.health import (
    HEALTH_INTERFACE,
    HEALTH_PROPERTIES,
    HEALTH_PROPERTY_SIGNATURES,
    HealthSnapshot,
    serialize_health,
    serialized_changed_properties,
)

try:  # pragma: no cover - exercised implicitly on every import
    import dbus
    import dbus.service

    DBUS_AVAILABLE = True
except ImportError:  # pragma: no cover - dbus-less runners only
    DBUS_AVAILABLE = False


PROPERTIES_INTERFACE: Final[str] = "org.freedesktop.DBus.Properties"
PROPERTIES_CHANGED_SIGNAL: Final[str] = "PropertiesChanged"
ACCESS_DENIED_ERROR: Final[str] = "org.freedesktop.DBus.Error.AccessDenied"
ACCESS_DENIED_BODY: Final[str] = "access denied"


class HealthSnapshotProvider(Protocol):
    def snapshot(self) -> HealthSnapshot: ...


def broker_health_path() -> str:
    return "/org/pantheon/HostBroker1/Health"


def approval_health_path() -> str:
    return "/org/pantheon/HostApproval1/Health"


def health_interface_xml() -> str:
    lines = [f'  <interface name="{HEALTH_INTERFACE}">']
    for name in HEALTH_PROPERTIES:
        signature = HEALTH_PROPERTY_SIGNATURES[name]
        lines.append(
            f'    <property name="{name}" type="{signature}" access="read"/>'
        )
    lines.append("  </interface>")
    return "\n".join(lines) + "\n"


def add_health_introspection(xml: str) -> str:
    marker = "</node>"
    if marker not in xml:
        raise ValueError("dbus introspection XML has no closing node")
    return xml.replace(marker, health_interface_xml() + marker, 1)


if DBUS_AVAILABLE:

    def _access_denied() -> "dbus.exceptions.DBusException":
        return dbus.exceptions.DBusException(
            ACCESS_DENIED_BODY,
            name=ACCESS_DENIED_ERROR,
        )

    def _invalid_property(body: str) -> "dbus.exceptions.DBusException":
        return dbus.exceptions.DBusException(
            body,
            name="org.freedesktop.DBus.Error.InvalidArgs",
        )

    def _marshal_value(name: str, value: object) -> object:
        signature = HEALTH_PROPERTY_SIGNATURES[name]
        if signature == "s":
            return dbus.String(str(value), variant_level=1)
        if signature == "a{ss}":
            return dbus.Dictionary(value, signature="ss", variant_level=1)
        raise AssertionError(f"unhandled health signature {signature}")

    def _marshal_properties(payload: dict[str, object]) -> "dbus.Dictionary":
        return dbus.Dictionary(
            {
                name: _marshal_value(name, value)
                for name, value in payload.items()
            },
            signature="sv",
        )

    class Health1Object(dbus.service.Object):
        def __init__(
            self,
            connection: "dbus.bus.BusConnection",
            *,
            object_path: str,
            provider: HealthSnapshotProvider,
            monitor_uids: frozenset[int],
        ) -> None:
            super().__init__(connection, object_path)
            if not monitor_uids:
                raise ValueError("health monitor uid set must not be empty")
            self._provider = provider
            self._monitor_uids = frozenset(int(uid) for uid in monitor_uids)

        def _get_unix_user(self, sender: str) -> int:
            return int(self.connection.get_unix_user(sender))

        def _authorize_monitor(self, sender: str | None) -> int:
            if not sender:
                raise _access_denied()
            try:
                uid = self._get_unix_user(sender)
            except Exception as exc:
                raise _access_denied() from exc
            if uid not in self._monitor_uids:
                raise _access_denied()
            return uid

        def _payload(self, sender: str | None) -> dict[str, object]:
            self._authorize_monitor(sender)
            return serialize_health(self._provider.snapshot())

        @dbus.service.method(
            PROPERTIES_INTERFACE,
            in_signature="ss",
            out_signature="v",
            sender_keyword="sender",
        )
        def Get(
            self,
            interface_name: str,
            property_name: str,
            sender: str | None = None,
        ) -> object:
            payload = self._payload(sender)
            if str(interface_name) != HEALTH_INTERFACE:
                raise _invalid_property("unknown health interface")
            name = str(property_name)
            if name not in HEALTH_PROPERTY_SIGNATURES:
                raise _invalid_property("unknown health property")
            return _marshal_value(name, payload[name])

        @dbus.service.method(
            PROPERTIES_INTERFACE,
            in_signature="s",
            out_signature="a{sv}",
            sender_keyword="sender",
        )
        def GetAll(
            self,
            interface_name: str,
            sender: str | None = None,
        ) -> "dbus.Dictionary":
            payload = self._payload(sender)
            if str(interface_name) != HEALTH_INTERFACE:
                raise _invalid_property("unknown health interface")
            return _marshal_properties(payload)

        @dbus.service.signal(
            PROPERTIES_INTERFACE,
            signature="sa{sv}as",
        )
        def PropertiesChanged(
            self,
            interface_name: str,
            changed_properties: "dbus.Dictionary",
            invalidated_properties: list,
        ) -> None:
            pass

        def emit_properties_changed(
            self,
            current: HealthSnapshot,
            previous: HealthSnapshot | None = None,
        ) -> None:
            self.PropertiesChanged(
                HEALTH_INTERFACE,
                _marshal_properties(
                    serialized_changed_properties(current, previous)
                ),
                [],
            )

        def Introspect(
            self,
            object_path: str,
            connection: "dbus.bus.BusConnection",
        ) -> str:
            return add_health_introspection(
                super().Introspect(object_path, connection)
            )
