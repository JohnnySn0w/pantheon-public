"""T-2919 live approver-adapter leg of the gate-21 canary: the REAL approver
MCP adapter (pinned mcp 2.x runtime) against the T-2919 private-bus stack,
asserting the human-card nonce never reaches tool output.

Deliberately NOT named ``test_host_control_*``: the D-Bus tiers workflow
runs that glob and forbids skips, and its runner has dbus-python but not the
hash-locked mcp-2 adapter runtime. This file belongs to the T-4858
hash-locked-runtime class (the same gate as the T-2916 runtime tier in
tools/test_approval_mcp_adapter.py): it skips unless that runtime is
importable. The always-on gate-21 tool-output proof is the in-process
``LeakCanaryTests.test_adapter_tool_output_never_carries_nonce_token_or_attestation``
in tools/test_host_control_telegram_ingress.py, which runs in BOTH ci.yml and
the D-Bus tiers.

T-4858: the test process needs NO dbus-python. The private-bus stack (daemon,
dbus-monitor, the real approval and ingress drivers) and the reviewer's own
D-Bus calls run in tools/_telegram_ingress_bus_helper.py under the broker
python (PANTHEON_BROKER_PYTHON, default /usr/bin/python3), which reuses
_IngressBusCase's setup and helpers unchanged. This process holds only the
mcp 2.x adapter client, so the leg runs in the hash-locked runtime venv even
where no interpreter carries both mcp 2.x and dbus-python (a runner whose
system python is not the lock's 3.12).
"""

from __future__ import annotations

import asyncio
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools.test_approval_mcp_adapter import SYSTEM_PYTHON, runtime_skip_reason
from tools.test_host_control_telegram_ingress_fixtures import (
    CANARY_DIGEST,
    CANARY_TOKEN_TEXT,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_PREFIX = "T4858-BUS-HELPER "  # tools._telegram_ingress_bus_helper.PREFIX (not imported: it is dbus-side)
_RUNTIME_SKIP = runtime_skip_reason()
requires_adapter_runtime = unittest.skipIf(
    _RUNTIME_SKIP is not None,
    f"T-4858 hash-locked adapter runtime required (mcp 2.x + jeepney): {_RUNTIME_SKIP}",
)


def _bus_stack_skip() -> str | None:
    if _RUNTIME_SKIP is not None:
        return _RUNTIME_SKIP
    if not (shutil.which("dbus-daemon") and shutil.which("dbus-monitor")):
        return "private-bus integration needs dbus-daemon and dbus-monitor"
    probe = subprocess.run([SYSTEM_PYTHON, "-c", "import dbus, gi, yaml"], capture_output=True, timeout=30) \
        if os.path.exists(SYSTEM_PYTHON) else None
    if probe is None or probe.returncode != 0:
        return f"private-bus integration needs {SYSTEM_PYTHON} (PANTHEON_BROKER_PYTHON) with dbus-python, PyGObject and PyYAML"
    return None


_BUS_SKIP = _bus_stack_skip()
requires_bus_stack = unittest.skipIf(_BUS_SKIP is not None, _BUS_SKIP or "")


_BUS_BINDINGS = ("dbus", "_dbus_bindings", "_dbus_glib_bindings", "gi")


def _loaded_bus_bindings() -> set[str]:
    return {m for m in sys.modules if m.split(".", 1)[0] in _BUS_BINDINGS}


class _BusStack:
    """Client for the helper subprocess that owns the private-bus stack."""

    def __init__(self) -> None:
        self._stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8")  # noqa: SIM115 - closed in stop()
        self.process = subprocess.Popen(
            [SYSTEM_PYTHON, "-m", "tools._telegram_ingress_bus_helper"],
            cwd=str(REPO_ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
            text=True, bufsize=1,
        )
        self.bus_address = self._reply(timeout=180)["bus_address"]

    def _reply(self, timeout: float = 60) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.process.stdout], [], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                break
            line = self.process.stdout.readline()
            if not line:
                break  # helper exited
            if line.startswith(HELPER_PREFIX):
                body = json.loads(line[len(HELPER_PREFIX):])
                if "error" in body:
                    raise AssertionError(f"bus helper: {body['error']}\n{self._stderr_tail()}")
                return body
        raise AssertionError(f"bus helper gave no reply within {timeout}s\n{self._stderr_tail()}")

    def _stderr_tail(self) -> str:
        self._stderr.seek(0)
        return self._stderr.read()[-3000:]

    def request(self, op: str, **fields) -> dict:
        self.process.stdin.write(json.dumps({"op": op, **fields}) + "\n")
        self.process.stdin.flush()
        return self._reply()

    def stop(self) -> None:
        try:
            if self.process.poll() is None:
                self.request("stop")
                self.process.stdin.close()
                self.process.wait(timeout=60)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=10)
            self._stderr.close()


@requires_adapter_runtime
@requires_bus_stack
class AdapterToolOutputBusTests(unittest.TestCase):
    """Moved verbatim from HumanDecisionCallerGateBusTests: the test
    process IS the approver (ingress uid differs, review finding 7). T-4858:
    the stack and the reviewer's D-Bus calls run in the bus helper, under the
    same uid, so the authorization seen by the services is unchanged."""

    @classmethod
    def setUpClass(cls) -> None:
        # Other modules in the same session may load dbus legitimately (signature parity);
        # this class must ADD none. Where dbus is not importable at all (the isolated runtime
        # venv on a non-3.12 runner) the test passing is the stronger proof.
        cls.preloaded = _loaded_bus_bindings()
        cls.stack = _BusStack()
        cls.bus_address = cls.stack.bus_address

    @classmethod
    def tearDownClass(cls) -> None:
        cls.stack.stop()

    def test_adapter_tool_output_never_carries_the_nonce(self) -> None:
        from mcp import Client

        from tools.test_approval_mcp_adapter import _driver_params

        review = self.stack.request("open_review")["review_handle"]

        async def body():
            async with Client(_driver_params(self.bus_address), mode="2026-07-28") as client:
                human = await client.call_tool("approval.request_human", {
                    "review_handle": review, "expected_digest": CANARY_DIGEST, "idempotency_key": "adapter-1"})
                got = await client.call_tool("approval.review.get", {"review_handle": review})
                return human, got

        human, got = asyncio.run(body())
        self.assertFalse(human.is_error, human.content[0].text)
        self.assertEqual(human.structured_content["state"], "presented")
        nonce = self.stack.request("nonce_for", review_handle=review)["nonce"]
        for result in (human, got):
            text = result.content[0].text + json.dumps(result.structured_content)
            self.assertNotIn(nonce, text)
            self.assertNotIn(CANARY_TOKEN_TEXT, text)
        # T-4858: nothing here loaded the D-Bus bindings (they live in the helper).
        self.assertEqual(sorted(_loaded_bus_bindings() - self.preloaded), [])


if __name__ == "__main__":
    unittest.main()
