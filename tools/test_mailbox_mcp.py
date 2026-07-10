"""test_mailbox_mcp.py — MCP server wrapper for tools/mailbox.py (T-1112).

End-to-end subprocess-pipe tests: spawns the server as a child process, drives
the MCP stdio protocol (initialize → tools/list → tools/call) against a real
tempdir mailbox, and asserts on the JSON-RPC responses.

Real-surface validation per D11 — the subprocess pipe exercises the actual
stdio framing, initialize lifecycle, and the SDK's JSON-RPC envelope; function-
call-only tests would mirror our assumptions rather than surface reality.

Test isolation: the server's MAILBOX_MCP_HUB env var is overridden to a tempdir
so no reads/writes touch /home/the-ancestor/mailbox.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Locate the server script relative to this test file.
_TOOLS_DIR = Path(__file__).resolve().parent
_SERVER_SCRIPT = _TOOLS_DIR / "mailbox_mcp.py"

# Import mailbox for fixture setup (provision + direct send).
sys.path.insert(0, str(_TOOLS_DIR))
import mailbox as _mailbox  # noqa: E402

# The MCP server subprocess hard-requires the optional `mcp` SDK (mailbox_mcp.py:
# `from mcp.server import Server`). Where it is absent (e.g. a fleet-wide
# `unittest discover` in an env without the dep), the server cannot start — these
# tests must SKIP, not ERROR. (T-1119 finding 5, Recovery.)
import importlib.util  # noqa: E402
_HAS_MCP = importlib.util.find_spec("mcp") is not None


class _MCPClient:
    """Minimal synchronous MCP stdio client for tests.

    Drives initialize → (tests run) → process teardown. Newline-delimited
    JSON-RPC 2.0 over stdin/stdout.
    """

    def __init__(self, hub: Path, user: str | None = None):
        env = os.environ.copy()
        env["MAILBOX_MCP_HUB"] = str(hub)
        if user is not None:
            env["USER"] = user  # override caller identity (fail-closed tests)
        self._proc = subprocess.Popen(
            [sys.executable, str(_SERVER_SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,  # line-buffered
        )
        self._id = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def send(self, method: str, params: dict | None = None) -> dict:
        """Send a JSON-RPC request and return the parsed response."""
        req: dict = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params is not None:
            req["params"] = params
        line = json.dumps(req) + "\n"
        self._proc.stdin.write(line)
        self._proc.stdin.flush()
        raw = self._proc.stdout.readline()
        if not raw:
            raise RuntimeError(
                f"Server closed stdout after request {method!r}. "
                f"stderr: {self._proc.stderr.read()}"
            )
        return json.loads(raw)

    def notify(self, method: str, params: dict | None = None) -> None:
        """Send a notification (no id, no response expected)."""
        req: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            req["params"] = params
        self._proc.stdin.write(json.dumps(req) + "\n")
        self._proc.stdin.flush()

    def initialize(self) -> dict:
        resp = self.send(
            "initialize",
            params={
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        )
        # Send the required initialized notification.
        self.notify("notifications/initialized")
        return resp

    def close(self) -> None:
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.stdout.close()
        except OSError:
            pass
        try:
            self._proc.stderr.close()
        except OSError:
            pass
        self._proc.wait(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@unittest.skipUnless(_HAS_MCP, "mcp SDK not installed; mailbox_mcp server cannot start")
class MCPServerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.hub = Path(self._tmp.name) / "mailbox"
        self.hub.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _client(self, user: str | None = None) -> _MCPClient:
        return _MCPClient(hub=self.hub, user=user)

    # ---------------------------------------------------------------- initialize
    def test_initialize_returns_server_info(self):
        with self._client() as c:
            resp = c.initialize()
        self.assertIn("result", resp, f"expected result, got {resp}")
        result = resp["result"]
        self.assertIn("serverInfo", result)
        self.assertEqual(result["serverInfo"]["name"], "mailbox")
        # Tools capability must be declared.
        caps = result.get("capabilities", {})
        self.assertIn("tools", caps)

    # -------------------------------------------------------------- tools/list
    def test_tools_list_contains_both_tools(self):
        with self._client() as c:
            c.initialize()
            resp = c.send("tools/list")
        self.assertIn("result", resp)
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertIn("mailbox_send", names)
        self.assertIn("mailbox_consume", names)

    def test_mailbox_send_schema_has_required_fields(self):
        with self._client() as c:
            c.initialize()
            resp = c.send("tools/list")
        tools = {t["name"]: t for t in resp["result"]["tools"]}
        schema = tools["mailbox_send"]["inputSchema"]
        self.assertEqual(schema["type"], "object")
        required = set(schema.get("required", []))
        self.assertIn("recipient", required)
        self.assertIn("subject", required)
        self.assertIn("body", required)

    def test_mailbox_consume_schema_has_no_required_fields(self):
        with self._client() as c:
            c.initialize()
            resp = c.send("tools/list")
        tools = {t["name"]: t for t in resp["result"]["tools"]}
        schema = tools["mailbox_consume"]["inputSchema"]
        self.assertEqual(schema.get("required", []), [])

    # -------------------------------------------------------------- mailbox_send
    def test_send_delivers_message_to_inbox_new(self):
        """tools/call mailbox_send → message lands in hub/<recipient>/new/."""
        with self._client() as c:
            c.initialize()
            resp = c.send(
                "tools/call",
                params={
                    "name": "mailbox_send",
                    "arguments": {
                        "recipient": "recovery",
                        "subject": "hello",
                        "body": "test body",
                    },
                },
            )
        self.assertIn("result", resp, f"unexpected: {resp}")
        result = resp["result"]
        self.assertFalse(result.get("isError", False), f"isError set: {result}")

        # Extract message id from response text.
        content = result["content"]
        self.assertTrue(len(content) > 0)
        msg_id = content[0]["text"].strip()
        self.assertTrue(len(msg_id) > 0)

        # Verify it landed in new/.
        recovery_inbox = self.hub / "recovery"
        new_file = recovery_inbox / "new" / msg_id
        self.assertTrue(new_file.exists(), f"expected {new_file} to exist")

        # Verify payload shape.
        msg = json.loads(new_file.read_text())
        self.assertEqual(msg["subject"], "hello")
        self.assertEqual(msg["payload"], "test body")
        # sender derived from $USER → persona map; at minimum must be non-empty.
        self.assertTrue(msg["sender"])

    def test_send_unknown_recipient_returns_error(self):
        with self._client() as c:
            c.initialize()
            resp = c.send(
                "tools/call",
                params={
                    "name": "mailbox_send",
                    "arguments": {
                        "recipient": "nobody",
                        "subject": "hi",
                        "body": "x",
                    },
                },
            )
        self.assertIn("result", resp)
        self.assertTrue(resp["result"].get("isError", False), f"expected isError: {resp}")

    # ----------------------------------------------------------- mailbox_consume
    def test_consume_drains_messages_and_returns_batch(self):
        """mailbox_consume returns all new messages and acks them to cur/."""
        # Send two messages directly into orchestrator's inbox (the caller's inbox is
        # $USER-derived; use "orchestrator" which maps to "claude" which is $USER here).
        orchestrator_inbox = self.hub / "orchestrator"
        _mailbox.send(orchestrator_inbox, {"task": "A"}, sender="specialist", subject="first")
        _mailbox.send(orchestrator_inbox, {"task": "B"}, sender="specialist", subject="second")

        with self._client() as c:
            c.initialize()
            resp = c.send(
                "tools/call",
                params={"name": "mailbox_consume", "arguments": {}},
            )

        self.assertIn("result", resp)
        result = resp["result"]
        self.assertFalse(result.get("isError", False), f"isError: {result}")

        content_text = result["content"][0]["text"]
        messages = json.loads(content_text)
        self.assertEqual(len(messages), 2)

        # Sorted by send order (lexical name sort = chronological).
        self.assertEqual(messages[0]["subject"], "first")
        self.assertEqual(messages[1]["subject"], "second")
        self.assertEqual(messages[0]["payload"], {"task": "A"})

        # Both messages must be acked to cur/ (no longer in new/).
        new_files = list((orchestrator_inbox / "new").iterdir())
        non_dot = [f for f in new_files if not f.name.startswith(".")]
        self.assertEqual(non_dot, [], "all messages must be moved to cur/ after consume")

        cur_files = list((orchestrator_inbox / "cur").iterdir())
        self.assertEqual(len(cur_files), 2)

    def test_consume_empty_inbox_returns_empty_list(self):
        """Consuming an empty inbox returns [] not an error."""
        with self._client() as c:
            c.initialize()
            resp = c.send(
                "tools/call",
                params={"name": "mailbox_consume", "arguments": {}},
            )
        self.assertIn("result", resp)
        result = resp["result"]
        self.assertFalse(result.get("isError", False))
        messages = json.loads(result["content"][0]["text"])
        self.assertEqual(messages, [])

    def test_consume_skipped_when_lease_held(self):
        """If another consumer holds the lease, consume returns skipped:true result."""
        from _lease import Lease  # noqa: E402

        # USER → orchestrator → self.hub / "orchestrator"
        orchestrator_inbox = self.hub / "orchestrator"
        _mailbox.provision(orchestrator_inbox)
        _mailbox.send(orchestrator_inbox, {"x": 1}, sender="specialist")

        # Hold the lease externally.
        lease = Lease(orchestrator_inbox / ".consumer.lock")
        self.assertTrue(lease.acquire())
        try:
            with self._client() as c:
                c.initialize()
                resp = c.send(
                    "tools/call",
                    params={"name": "mailbox_consume", "arguments": {}},
                )
        finally:
            lease.release()

        self.assertIn("result", resp)
        result = resp["result"]
        self.assertFalse(result.get("isError", False))
        content_text = result["content"][0]["text"]
        # The response text for a skipped drain contains the word "skipped".
        self.assertIn("skipped", content_text.lower())

    def test_send_three_messages_then_consume_ordered(self):
        """Send 3, consume: returned list must match send order."""
        orchestrator_inbox = self.hub / "orchestrator"
        for i in range(3):
            _mailbox.send(orchestrator_inbox, {"i": i}, sender="recovery", subject=f"msg-{i}")

        with self._client() as c:
            c.initialize()
            resp = c.send(
                "tools/call",
                params={"name": "mailbox_consume", "arguments": {}},
            )

        messages = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(len(messages), 3)
        subjects = [m["subject"] for m in messages]
        self.assertEqual(subjects, ["msg-0", "msg-1", "msg-2"])

    # ------------------------------------------------- fail-closed identity (1)
    def test_consume_unknown_user_fails_closed(self):
        """An unrecognized $USER must error before any fs op, not drain hub/<raw-user>."""
        with self._client(user="nobody-xyz") as c:
            c.initialize()
            resp = c.send(
                "tools/call",
                params={"name": "mailbox_consume", "arguments": {}},
            )
        self.assertIn("result", resp)
        self.assertTrue(resp["result"].get("isError", False),
                        f"expected isError for unknown user: {resp}")
        # No hub/<raw-user> directory should have been created.
        self.assertFalse((self.hub / "nobody-xyz").exists(),
                         "fail-closed must not touch the filesystem for an unknown user")

    def test_send_unknown_user_fails_closed(self):
        """An unrecognized $USER cannot send-as an arbitrary persona."""
        with self._client(user="nobody-xyz") as c:
            c.initialize()
            resp = c.send(
                "tools/call",
                params={
                    "name": "mailbox_send",
                    "arguments": {"recipient": "recovery", "subject": "x", "body": "y"},
                },
            )
        self.assertIn("result", resp)
        self.assertTrue(resp["result"].get("isError", False),
                        f"expected isError for unknown user: {resp}")

    # --------------------------------------------- partial-consume surfacing (3)
    def test_consume_partial_surfaces_acked_and_errors(self):
        """A drain that part-fails returns the acked messages + an error marker,
        never a bare error that strands already-acked work."""
        orchestrator_inbox = self.hub / "orchestrator"
        _mailbox.provision(orchestrator_inbox)
        # One good message (will ack) + one corrupt file (read error → errors[]).
        _mailbox.send(orchestrator_inbox, {"ok": 1}, sender="specialist", subject="good")
        (orchestrator_inbox / "new" / "00-corrupt").write_text("{ not valid json")

        with self._client() as c:
            c.initialize()
            resp = c.send(
                "tools/call",
                params={"name": "mailbox_consume", "arguments": {}},
            )

        self.assertIn("result", resp)
        result = resp["result"]
        # A partial drain is a normal (non-error) result carrying the partial shape.
        self.assertFalse(result.get("isError", False), f"isError: {result}")
        payload = json.loads(result["content"][0]["text"])
        self.assertTrue(payload.get("partial"), f"expected partial shape: {payload}")
        self.assertTrue(payload.get("errors"), "the corrupt file must be reported in errors")
        subjects = [m["subject"] for m in payload["messages"]]
        self.assertIn("good", subjects, "the acked message must be returned, not lost")

    def test_consume_partial_on_escaping_exception_returns_collected(self):
        """If consume() raises outright, the wrapper returns the already-collected
        messages + an error marker (in-process; covers the except branch)."""
        import mailbox_mcp  # noqa: E402 — guarded by class skipUnless(_HAS_MCP)
        import asyncio

        orchestrator_inbox = self.hub / "orchestrator"
        _mailbox.provision(orchestrator_inbox)

        orig_hub = mailbox_mcp.MAILBOX_HUB
        orig_consume = mailbox_mcp._mailbox.consume

        def _boom(inbox, handler, **kwargs):
            handler({"id": "m1", "sender": "specialist", "subject": "s", "ts": 0, "payload": {}})
            raise RuntimeError("fs exploded mid-drain")

        mailbox_mcp.MAILBOX_HUB = self.hub
        mailbox_mcp._mailbox.consume = _boom
        try:
            out = asyncio.run(mailbox_mcp._handle_consume({}))
        finally:
            mailbox_mcp.MAILBOX_HUB = orig_hub
            mailbox_mcp._mailbox.consume = orig_consume

        payload = json.loads(out[0].text)
        self.assertTrue(payload.get("partial"))
        self.assertIn("RuntimeError", payload.get("error", ""))
        self.assertEqual([m["id"] for m in payload["messages"]], ["m1"])

    def test_cold_start_registration_command(self):
        """Smoke: server starts cleanly with no preloaded venv (rider 1 verification).

        Spawns under system python3 (not the current interpreter) using the absolute
        path, as the registration command implies, and verifies initialize succeeds.
        The test is a no-op when system python3 == current interpreter, but it also
        exercises the Popen path cleanly.
        """
        # Use the absolute path for the interpreter — same form as the registration cmd.
        python_abs = "/usr/bin/python3"
        if not Path(python_abs).exists():
            self.skipTest(f"{python_abs} not available; skipping cold-start check")

        env = os.environ.copy()
        env["MAILBOX_MCP_HUB"] = str(self.hub)
        proc = subprocess.Popen(
            [python_abs, str(_SERVER_SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )
        try:
            init_req = json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "cold-start-test", "version": "0"},
                },
            }) + "\n"
            proc.stdin.write(init_req)
            proc.stdin.flush()
            raw = proc.stdout.readline()
            resp = json.loads(raw)
            self.assertIn("result", resp, f"cold-start init failed: {resp}")
            self.assertEqual(resp["result"]["serverInfo"]["name"], "mailbox")
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
