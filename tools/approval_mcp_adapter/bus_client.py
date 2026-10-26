"""The approver adapter's only D-Bus surface (ORG-052 Unit 4b, T-2916).

Mirrors ``tools/host_mcp_adapter/bus_client.py`` (T-2915) with ONE fixed
destination: ``org.pantheon.HostApproval1`` at ``/org/pantheon/HostApproval1``,
interface ``org.pantheon.HostApproval1.Reviewer``. The public API takes a
closed ``contract.Method`` member and a tuple of strings - there is no
destination, path, interface, member, or signature parameter, so the
adapter cannot be turned into a generic D-Bus passthrough, and the
Consumer / HumanIngress interfaces are unreachable from this binary.

The reconnect and fault-classification semantics are identical to the
host adapter's (pinned by the shared fault matrix in
``tools/test_approval_mcp_adapter.py``):

- no connection is opened at construction or at MCP initialize;
- each call (re)connects lazily within ``CONNECT_TIMEOUT`` if there is no
  live connection, then makes ONE call bounded by the tool's timeout;
- any transport fault drops the connection, so the next call starts from
  scratch (service restart, unique-name change, bus restart all heal);
- the adapter NEVER retries a call itself: exactly-once is the approval
  service's job via the caller's idempotency key.

Identity is never sent: the approval service resolves the caller from the
bus-attested uid of this connection (ORG-052 §2).
"""

from __future__ import annotations

import asyncio

from jeepney import DBusAddress, HeaderFields, MessageType, new_method_call
from jeepney.io.asyncio import DBusRouter, open_dbus_connection

from tools.approval_mcp_adapter.contract import (
    BUS_NAME,
    CONNECT_TIMEOUT,
    OBJECT_PATH,
    REVIEWER_INTERFACE,
    SYSTEM_BUS_ADDRESS,
    AdapterError,
    Method,
    error_for_dbus_name,
)

_REVIEWER = DBusAddress(OBJECT_PATH, bus_name=BUS_NAME, interface=REVIEWER_INTERFACE)
_BUS_DAEMON = DBusAddress(
    "/org/freedesktop/DBus",
    bus_name="org.freedesktop.DBus",
    interface="org.freedesktop.DBus",
)


class ReviewerClient:
    """Lazily connected, self-healing client of ``HostApproval1.Reviewer``.

    ``address`` defaults to the fixed system bus socket. It is a
    constructor argument only so that test drivers (never packaged) can
    point at a private bus; the production entrypoint passes nothing and
    no environment variable is consulted (ORG-052 §7).
    """

    def __init__(
        self,
        address: str = SYSTEM_BUS_ADDRESS,
        *,
        connect_timeout: float = CONNECT_TIMEOUT,
    ) -> None:
        self._address = address
        self._connect_timeout = connect_timeout
        self._conn = None
        self._router: DBusRouter | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._live()

    def _live(self) -> bool:
        router = self._router
        # jeepney 0.9.0 (lock-pinned): the router's receiver task ends on
        # EOF, so a done task means the connection is dead BEFORE we send.
        return router is not None and not router._rcv_task.done()

    async def _drop(self) -> None:
        router, conn = self._router, self._conn
        self._router = None
        self._conn = None
        if router is not None:
            try:
                await router.__aexit__(None, None, None)
            except BaseException:  # noqa: BLE001 - teardown of a dead link
                pass
        if conn is not None:
            try:
                await conn.close()
            except BaseException:  # noqa: BLE001 - teardown of a dead link
                pass

    async def _connect(self) -> None:
        await self._drop()
        try:
            conn = await asyncio.wait_for(
                open_dbus_connection(self._address),
                timeout=self._connect_timeout,
            )
        except (OSError, EOFError, asyncio.TimeoutError, RuntimeError, ValueError):
            raise AdapterError("authority_unavailable") from None
        except Exception:  # noqa: BLE001 - auth/parse faults from the library
            raise AdapterError("authority_unavailable") from None
        self._conn = conn
        self._router = DBusRouter(conn)

    async def call(self, method: Method, args: tuple[str, ...], *, timeout: float) -> tuple:
        if not isinstance(method, Method):
            raise TypeError("method must be a contract.Method member")
        if len(args) != len(method.in_signature) or not all(
            isinstance(arg, str) for arg in args
        ):
            raise TypeError("arguments must match the method's string signature")
        async with self._lock:
            return await self._call_locked(method, args, timeout)

    async def _call_locked(self, method: Method, args: tuple[str, ...], timeout: float) -> tuple:
        if not self._live():
            await self._connect()
        message = new_method_call(
            _REVIEWER,
            method.member,
            method.in_signature or None,
            args,
        )
        assert self._router is not None
        try:
            reply = await asyncio.wait_for(
                self._router.send_and_get_reply(message),
                timeout=timeout,
            )
        except asyncio.CancelledError:
            # client gave up waiting: abandon the wait only; the reply (if
            # any) is discarded by the router. Do not tear the link down.
            raise
        except asyncio.TimeoutError:
            await self._drop()
            raise AdapterError("authority_no_reply") from None
        except Exception:  # noqa: BLE001 - RouterClosed / write faults
            await self._drop()
            raise AdapterError("authority_no_reply") from None
        return self._unwrap(method, reply)

    def _unwrap(self, method: Method, reply) -> tuple:
        if reply.header.message_type == MessageType.error:
            name = reply.header.fields.get(HeaderFields.error_name)
            # an error REPLY means the link itself is healthy: keep it
            raise error_for_dbus_name(name, reply.body)
        signature = reply.header.fields.get(HeaderFields.signature, "")
        if signature != method.out_signature:
            raise AdapterError("authority_contract_mismatch")
        return tuple(reply.body)

    async def service_name_state(self, *, timeout: float) -> tuple[bool, bool]:
        """(owned, activatable) for the approval name - used by --self-check."""

        async with self._lock:
            if not self._live():
                await self._connect()
            assert self._router is not None
            try:
                owned = await asyncio.wait_for(
                    self._router.send_and_get_reply(
                        new_method_call(_BUS_DAEMON, "NameHasOwner", "s", (BUS_NAME,))
                    ),
                    timeout=timeout,
                )
                activatable = await asyncio.wait_for(
                    self._router.send_and_get_reply(
                        new_method_call(_BUS_DAEMON, "ListActivatableNames")
                    ),
                    timeout=timeout,
                )
            except Exception:  # noqa: BLE001
                await self._drop()
                raise AdapterError("authority_unavailable") from None
        if owned.header.message_type == MessageType.error or activatable.header.message_type == MessageType.error:
            raise AdapterError("authority_unavailable")
        return bool(owned.body[0]), BUS_NAME in list(activatable.body[0])

    async def aclose(self) -> None:
        async with self._lock:
            await self._drop()
