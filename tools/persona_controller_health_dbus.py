"""D-Bus health surface for the T-2953 harness controller (T-2969 BT3).

ORG-068 SS92's uniform health interface, wired for the stateless
per-persona runtime controller (`tools.runtime_controller.controller_main`).
Session bus, per persona: the bus name and object path are
persona-parameterized (`org.pantheon.PersonaController.<persona>` /
`/org/pantheon/PersonaController/<persona>`), but the D-Bus *interface*
name is persona-independent and versioned (`org.pantheon.PersonaController1`)
-- a consumer (Recovery's T-2959 channel-liveness monitor) binds ONE interface
string across every persona, only the bus name/path vary. Mirrors
`tools.host_control.health_dbus`'s `org.pantheon.Health1` shape, minus its
`monitor_uids` authorization gate -- a session bus is already scoped to the
persona's own unix user, so there is no cross-uid caller to authorize here.

Session bus, not the system bus `tools.host_control` rides: this
controller runs unprivileged, as the persona's own account, with no
service-account/root-install leg -- the session bus is the natural fit
(ratified in `notes/2026-08-18-t2953-phase1-spec.md` SS D-Bus health
interface: "D-Bus, session bus, per SS92 pattern").

T-2959 consumer contract (coordination note for Recovery's channel-liveness
monitor -- F4, T-2969 REBOUND):

    Bus name:      org.pantheon.PersonaController.<persona>  (session bus)
    Object path:   /org/pantheon/PersonaController/<persona>
    Interface:     org.pantheon.PersonaController1            (uniform, versioned)

    | Property              | Type   | Meaning                                                  |
    |------------------------|--------|----------------------------------------------------------|
    | ActiveRuntime          | s      | current live runtime slug (e.g. "claude/anthropic-code"), or "" if none |
    | ViabilityStatus        | s      | "viable" \\| "cooldown-hold" \\| "all-held" \\| "exhausted"  |
    | LastSelectionTs        | s      | ISO8601 of the last controller invocation (any outcome)  |
    | LastFailureClass       | s      | failure class of the most recent recorded runtime death, or "" |
    | NextCooldownExpiryTs   | s      | ISO8601 of the earliest cooldown expiry across non-held runtimes, or "" |
    | Holds                  | as     | sorted list of held runtime refs                          |
    | ModalPresent           | b      | T-4047: mid-session-cap CONFIRMED present (read_mid_session_cap_state) |
    | ModalKind              | s      | "session-limit" \\| "weekly-limit" \\| "opus-limit" \\| "monthly-spend-limit", or "" |
    | ModalResetAt           | s      | ISO8601 reset instant PARSED from the pane, or "" if unparsed/absent |

    Read modes: `Get(interface, name)` / `GetAll(interface)` for pull
    (fresh state on every call -- SS88, never cached); subscribe to the
    standard `org.freedesktop.DBus.Properties.PropertiesChanged` signal
    for push (fires the FULL current snapshot on every controller
    transition -- started / start-failed / no-viable-candidate /
    death-cleared -- never a diff).

    **Real-death semantics:** `ActiveRuntime == ""` for longer than a
    bounded window while `ViabilityStatus != "all-held"` is a real death
    -- the controller SHOULD have a runtime up but doesn't. A held-all
    state (`ViabilityStatus == "all-held"`) is a legitimate
    not-supposed-to-be-running condition, NOT a fire alarm -- someone
    deliberately disabled every runtime via `harness hold`.
"""

from __future__ import annotations

import threading
from typing import Callable, Final, Protocol

try:  # pragma: no cover - exercised implicitly on every import
    import dbus
    import dbus.service

    DBUS_AVAILABLE = True
except ImportError:  # pragma: no cover - dbus-less runners only
    DBUS_AVAILABLE = False


PERSONA_CONTROLLER_INTERFACE: Final[str] = "org.pantheon.PersonaController1"
PROPERTIES_INTERFACE: Final[str] = "org.freedesktop.DBus.Properties"
PROPERTIES_CHANGED_SIGNAL: Final[str] = "PropertiesChanged"

PERSONA_CONTROLLER_PROPERTIES: Final[tuple[str, ...]] = (
    "ActiveRuntime",
    "ViabilityStatus",
    "LastSelectionTs",
    "LastFailureClass",
    "NextCooldownExpiryTs",
    "Holds",
    "ModalPresent",
    "ModalKind",
    "ModalResetAt",
    # ORG-106 L4a (T-5527): additive seat-state publication, APPENDED after the nine.
    "SeatState",
    "SeatStateSources",
    "SeatStateOwner",
    "SeatStateReason",
    "SeatStateExpires",
    "SeatStateFault",
)
PERSONA_CONTROLLER_PROPERTY_SIGNATURES: Final[dict[str, str]] = {
    "ActiveRuntime": "s",
    "ViabilityStatus": "s",
    "LastSelectionTs": "s",
    "LastFailureClass": "s",
    "NextCooldownExpiryTs": "s",
    "Holds": "as",
    "ModalPresent": "b",
    "ModalKind": "s",
    "ModalResetAt": "s",
    "SeatState": "s",
    "SeatStateSources": "as",
    "SeatStateOwner": "s",
    "SeatStateReason": "s",
    "SeatStateExpires": "s",
    "SeatStateFault": "s",
}

#: T-5137: the pantheon-health/v1 leg, APPENDED after the nine above when
#: `PANTHEON_PERSONA_HEALTH_LEG=on` (default off). Same append-only growth rule as
#: `tools.host_control.health`; names are bound as literals by the T-5395 collector.
#: Kept as separate tables so the flag-off surface is byte-identical by construction.
HEALTH_LEG_PROPERTIES: Final[tuple[str, ...]] = (
    "ContractRevision",
    "Status",
    "Subcomponents",
    "Reasons",
    "Revisions",
    "InvariantListRef",
    "SecurityDrift",
)
HEALTH_LEG_SIGNATURES: Final[dict[str, str]] = {
    "ContractRevision": "s",
    "Status": "s",
    "Subcomponents": "a{ss}",
    "Reasons": "a{ss}",
    "Revisions": "a{ss}",
    "InvariantListRef": "s",
    "SecurityDrift": "b",
}


def seat_state_properties(persona: str, *, state_root=None, effective_fn=None) -> dict[str, object]:
    """ORG-106 L4a: the six SeatState* property values for *persona*.

    Pure (no dbus). The controller runs as the seat, so the default reader is
    `seat_state.effective` (direct read, honouring *state_root*). FAIL-OPEN: any reader
    exception yields state "active" with SeatStateFault naming the exception class, so
    a broken record can never take the whole health payload down."""
    try:
        if effective_fn is None:
            from tools import seat_state

            effective_fn = lambda p: seat_state.effective(p, state_root=state_root)  # noqa: E731
        e = effective_fn(persona)
        return {
            "SeatState": e.state,
            "SeatStateSources": list(e.sources),
            "SeatStateOwner": e.owner or "",
            "SeatStateReason": e.reason or "",
            "SeatStateExpires": e.expires or "",
            "SeatStateFault": e.fault or "",
        }
    except Exception as exc:  # noqa: BLE001 - fail-open
        return {
            "SeatState": "active",
            "SeatStateSources": [],
            "SeatStateOwner": "",
            "SeatStateReason": "",
            "SeatStateExpires": "",
            "SeatStateFault": f"seat-state: {type(exc).__name__}",
        }


def _signatures(health_leg: bool) -> dict[str, str]:
    if health_leg:
        return {**PERSONA_CONTROLLER_PROPERTY_SIGNATURES, **HEALTH_LEG_SIGNATURES}
    return PERSONA_CONTROLLER_PROPERTY_SIGNATURES


class HealthPropertiesProvider(Protocol):
    def __call__(self) -> dict[str, object]: ...


def bus_name_for(persona: str) -> str:
    return f"org.pantheon.PersonaController.{persona}"


def object_path_for(persona: str) -> str:
    return f"/org/pantheon/PersonaController/{persona}"


def persona_controller_interface_xml(health_leg: bool = False) -> str:
    lines = [f'  <interface name="{PERSONA_CONTROLLER_INTERFACE}">']
    names = PERSONA_CONTROLLER_PROPERTIES + (HEALTH_LEG_PROPERTIES if health_leg else ())
    for name in names:
        signature = _signatures(health_leg)[name]
        lines.append(f'    <property name="{name}" type="{signature}" access="read"/>')
    lines.append("  </interface>")
    return "\n".join(lines) + "\n"


def add_persona_controller_introspection(xml: str, health_leg: bool = False) -> str:
    marker = "</node>"
    if marker not in xml:
        raise ValueError("dbus introspection XML has no closing node")
    return xml.replace(marker, persona_controller_interface_xml(health_leg) + marker, 1)


if DBUS_AVAILABLE:

    def _invalid_property(body: str) -> "dbus.exceptions.DBusException":
        return dbus.exceptions.DBusException(
            body, name="org.freedesktop.DBus.Error.InvalidArgs"
        )

    def _marshal_value(name: str, value: object) -> object:
        signature = _signatures(True)[name]
        if signature == "s":
            return dbus.String(str(value), variant_level=1)
        if signature == "as":
            return dbus.Array([str(item) for item in value], signature="s", variant_level=1)
        if signature == "b":
            return dbus.Boolean(bool(value), variant_level=1)
        if signature == "a{ss}":
            return dbus.Dictionary(
                {str(k): str(v) for k, v in dict(value).items()}, signature="ss", variant_level=1
            )
        raise AssertionError(f"unhandled persona-controller signature {signature}")

    def _marshal_properties(payload: dict[str, object]) -> "dbus.Dictionary":
        return dbus.Dictionary(
            {name: _marshal_value(name, value) for name, value in payload.items()},
            signature="sv",
        )

    class PersonaController1Object(dbus.service.Object):
        """Exports the six ORG-068 SS92 health properties for one
        persona's controller. `provider` is called fresh on every
        `Get`/`GetAll` -- no caching, so a restart-fixable respawn always
        answers current truth (SS88) and a live process never answers a
        value captured at export time."""

        def __init__(
            self,
            connection: "dbus.bus.BusConnection",
            *,
            object_path: str,
            provider: HealthPropertiesProvider,
            bus_name_handle: "dbus.service.BusName | None" = None,
            health_leg: bool = False,
        ) -> None:
            super().__init__(connection, object_path)
            self._provider = provider
            self._health_leg = health_leg
            # `dbus.service.BusName.__del__` releases the well-known name
            # on garbage collection -- holding the handle here (not just
            # in `start_service`'s local scope) keeps the name claimed
            # for exactly this object's lifetime. Losing this reference
            # was T-2969's first real bug: the name got claimed then
            # released within the same call, so callers saw
            # `ServiceUnknown` instead of ever reaching this object.
            self._bus_name_handle = bus_name_handle

        @dbus.service.method(PROPERTIES_INTERFACE, in_signature="ss", out_signature="v")
        def Get(self, interface_name: str, property_name: str) -> object:
            if str(interface_name) != PERSONA_CONTROLLER_INTERFACE:
                raise _invalid_property("unknown persona-controller interface")
            name = str(property_name)
            if name not in _signatures(self._health_leg):
                raise _invalid_property("unknown persona-controller property")
            payload = self._provider()
            return _marshal_value(name, payload[name])

        @dbus.service.method(PROPERTIES_INTERFACE, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface_name: str) -> "dbus.Dictionary":
            if str(interface_name) != PERSONA_CONTROLLER_INTERFACE:
                raise _invalid_property("unknown persona-controller interface")
            return _marshal_properties(self._provider())

        @dbus.service.signal(PROPERTIES_INTERFACE, signature="sa{sv}as")
        def PropertiesChanged(
            self,
            interface_name: str,
            changed_properties: "dbus.Dictionary",
            invalidated_properties: list,
        ) -> None:
            pass

        def emit_properties_changed(self) -> None:
            """Emits the full current property snapshot (T-2969 DECISION:
            no in-memory diffing against a remembered previous value --
            see `tools.runtime_controller._dbus_emit_health`)."""
            self.PropertiesChanged(
                PERSONA_CONTROLLER_INTERFACE,
                _marshal_properties(self._provider()),
                [],
            )

        def Introspect(
            self, object_path: str, connection: "dbus.bus.BusConnection"
        ) -> str:
            return add_persona_controller_introspection(
                super().Introspect(object_path, connection), self._health_leg
            )

    _mainloop_lock = threading.Lock()
    _mainloop_started = False

    def ensure_glib_mainloop_running() -> None:
        """Starts exactly one GLib mainloop on a background daemon thread
        for this process. dbus-python's method dispatch needs a running
        mainloop to answer `Get`/`GetAll` while `controller_main` blocks
        in its own sleep-poll loop (T-2969 DECISION #2) -- without this,
        the exported object would never actually respond to callers.
        Idempotent: a second call in the same process is a no-op."""
        global _mainloop_started
        with _mainloop_lock:
            if _mainloop_started:
                return
            import dbus.mainloop.glib
            from gi.repository import GLib

            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            loop = GLib.MainLoop()
            thread = threading.Thread(
                target=loop.run, name="persona-controller-dbus-mainloop", daemon=True
            )
            thread.start()
            _mainloop_started = True

    def start_service(
        persona: str, provider: HealthPropertiesProvider, health_leg: bool = False
    ) -> "PersonaController1Object":
        """Registers `org.pantheon.PersonaController.<persona>` on the
        session bus and exports the health object. Raises on failure (no
        session bus reachable, name already owned by another connection,
        etc.) -- the caller
        (`tools.runtime_controller._start_persona_controller_dbus_service`)
        owns the fail-open wrap; this function stays honest about
        failure rather than silently swallowing it itself."""
        ensure_glib_mainloop_running()
        bus = dbus.SessionBus()
        bus_name_handle = dbus.service.BusName(bus_name_for(persona), bus)
        return PersonaController1Object(
            bus,
            object_path=object_path_for(persona),
            provider=provider,
            bus_name_handle=bus_name_handle,
            health_leg=health_leg,
        )
