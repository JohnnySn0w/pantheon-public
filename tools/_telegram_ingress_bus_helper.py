"""Hold T-2919's private-bus approval + ingress stack open for a test in ANOTHER interpreter (T-4858).

    PANTHEON_BROKER_PYTHON -m tools._telegram_ingress_bus_helper

tools/test_approval_mcp_adapter_telegram_ingress.py runs in the hash-locked mcp 2.x venv, which
has no dbus-python on a runner whose system python is not the lock's 3.12 (CT202). Everything
that needs dbus-python in-process lives here instead, under the broker python: the stack is
tools.test_host_control_telegram_ingress_dbus._IngressBusCase's own setUpClass (private
dbus-daemon, dbus-monitor, the REAL approval and ingress drivers), with the offsets that test
used, and its own open_review / nonce_for / tearDownClass. Nothing is re-implemented, so the
proof is the same stack, only driven from a second process.

Protocol: JSON lines. Each reply is one stdout line prefixed with PREFIX (any other stdout line
is noise to the client). First reply: {"bus_address": ...} once the stack is ready, or
{"error": ...}. Then per request line on stdin: {"op": "open_review"} -> {"review_handle": h};
{"op": "nonce_for", "review_handle": h} -> {"nonce": n}; {"op": "stop"} -> {"stopped": true}.
A failure is {"error": "<Type>: <message>"}. EOF on stdin also stops; the stack is torn down
either way (its daemons carry PDEATHSIG, so a killed helper leaves nothing behind).
"""

from __future__ import annotations

import json
import sys

PREFIX = "T4858-BUS-HELPER "


def _emit(body: dict) -> None:
    sys.stdout.write(PREFIX + json.dumps(body, sort_keys=True) + "\n")
    sys.stdout.flush()


def main() -> int:
    import tools.test_host_control_telegram_ingress_dbus as ingress_bus

    if not (ingress_bus.ingress_dbus.DBUS_AVAILABLE and ingress_bus.DBUS_DAEMON and ingress_bus.DBUS_MONITOR):
        _emit({"error": f"{sys.executable} lacks the private-bus stack: needs dbus-python, dbus-daemon and dbus-monitor"})
        return 2

    class _Stack(ingress_bus._IngressBusCase):
        # the values AdapterToolOutputBusTests used: the test process IS the approver, the
        # registered ingress uid differs (review finding 7)
        INGRESS_OFFSET = 3000
        AGENT_OFFSET = 0

        def runTest(self) -> None:  # a TestCase instance is needed for open_review's asserts
            pass

    try:
        _Stack.setUpClass()
    except Exception as exc:  # noqa: BLE001 - reported to the client, not a traceback
        _emit({"error": f"stack setup failed: {type(exc).__name__}: {exc}"})
        return 1
    case = _Stack()
    try:
        _emit({"bus_address": _Stack.bus_address})
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                op = request.get("op")
                if op == "open_review":
                    reply = {"review_handle": case.open_review()}
                elif op == "nonce_for":
                    reply = {"nonce": case.nonce_for(str(request["review_handle"]))}
                elif op == "stop":
                    _emit({"stopped": True})
                    break
                else:
                    reply = {"error": f"unknown op {op!r}"}
            except Exception as exc:  # noqa: BLE001
                reply = {"error": f"{type(exc).__name__}: {exc}"}
            _emit(reply)
    finally:
        _Stack.tearDownClass()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
