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

# T-1971: the caller's inbox is $USER-derived by the SERVER (mailbox_mcp's
# `_caller_persona` → `_USER_TO_PERSONA`). A test that seeds a hardcoded "orchestrator"
# box only matches the server when $USER == "claude"; on any seat it seeds one
# box while the server drains another ($USER=suborch1 → metis, suborch2 → atlas)
# and asserts 0 != N. Fix: pin a KNOWN caller and derive its box from the
# server's OWN map — import it, never reimplement, so the test and server cannot
# disagree on the mapping (lessons/2026-07-22-false-red-verifier-must-call-prod-
# parser.md). This is account-portable: the same pinned identity resolves the
# same box on every account. Was the last real defect in the T-1934 seat-suite
# triage.
if _HAS_MCP:
    from mailbox_mcp import _USER_TO_PERSONA  # noqa: E402
    _CALLER_USER = "claude"
    _CALLER_PERSONA = _USER_TO_PERSONA[_CALLER_USER]  # → "orchestrator", from the map
else:  # keep names defined so a no-SDK env skips cleanly rather than NameError-ing
    _CALLER_USER = _CALLER_PERSONA = None

# T-1971 (second item): the subprocess harness decodes the tool-call result as
# `result["content"][0]["text"]` / `result.get(...)`. Under mcp >= 1.28 the
# structured-content envelope changed shape (a field the harness `.get`s became
# a list → "'list' object has no attribute .get"), so the decode breaks before
# any assertion. Seats are PINNED to mcp==1.27.1 (fleet-aligned, T-1934), so this
# is LATENT today — but a future SDK bump would surface it as a cryptic harness
# error, not an honest signal. Bound the assumption explicitly: on >= 1.28, skip
# the subprocess-decode tests with a reason pointing here, rather than adapt the
# decode blind to a shape this environment cannot exercise (writing unverifiable
# branches is the trap; a documented skip is the honest boundary). The in-process
# escaping test does not use the subprocess and is unaffected.
def _mcp_ge_128() -> bool:
    """True iff the installed mcp is >= 1.28 (the structured-content shape bump).

    Fail-OPEN (return False) on any parse ambiguity: the guard is a courtesy that
    turns a KNOWN-bad version into a clear skip; when uncertain, run rather than
    silently hide coverage.
    """
    if not _HAS_MCP:
        return False
    try:
        import importlib.metadata
        parts = importlib.metadata.version("mcp").split(".")
        major, minor = int(parts[0]), int(parts[1])
    except Exception:
        return False
    return (major, minor) >= (1, 28)


_MCP_DECODE_SKIP = (
    "mcp >= 1.28 changed the structured-content envelope shape; this harness "
    "decodes the 1.27.x shape (T-1971). Adapt the decode or pin mcp==1.27.1."
)


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

    def _client(self, user: str | None = _CALLER_USER) -> _MCPClient:
        # T-1971: DEFAULT the caller to the pinned _CALLER_USER, not ambient
        # $USER. This makes every subprocess test resolve the SAME box on every
        # account (claude→orchestrator here regardless of who runs the suite), so a
        # seeded `caller_inbox` always matches what the server drains. Tests that
        # need a different identity (the fail-closed cases) pass `user=` explicitly
        # and override this. Ambient $USER was the T-1934 seat-suite defect.
        #
        # Also the single choke point for the mcp>=1.28 decode guard: skip-with-
        # reason here so the in-process escaping test (which never calls _client)
        # keeps running on 1.28.
        if _mcp_ge_128():
            self.skipTest(_MCP_DECODE_SKIP)
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

    def test_initialize_instructions_frame_data_not_instruction(self):
        """T-2514/T-2631: server-level `instructions` (shown at session init)
        must frame mailbox content as data, not instructions to execute."""
        with self._client() as c:
            resp = c.initialize()
        self.assertIn("result", resp, f"expected result, got {resp}")
        instructions = resp["result"].get("instructions", "")
        self.assertTrue(instructions, "server must declare instructions")
        self.assertIn("not", instructions.lower())
        self.assertIn("instruction", instructions.lower())
        self.assertIn("provenance", instructions)

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

    def test_mailbox_consume_description_frames_data_not_instruction(self):
        """T-2514/T-2631: the tool description (shown at tools/list) must
        frame payloads as data-not-instruction and document `provenance`."""
        with self._client() as c:
            c.initialize()
            resp = c.send("tools/list")
        tools = {t["name"]: t for t in resp["result"]["tools"]}
        description = tools["mailbox_consume"]["description"]
        self.assertIn("DATA", description)
        self.assertIn("not instructions", description)
        self.assertIn("provenance", description)

    # -------------------------------------------------------------- mailbox_send
    def test_send_delivers_message_to_inbox_new(self):
        """tools/call mailbox_send → message lands in hub/<recipient>/new/."""
        # T-2216: send() no longer auto-provisions — the recipient's box must
        # already exist, mirroring every real production recipient (an
        # established persona inbox).
        _mailbox.provision(self.hub / "recovery")
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

    # ------------------------------------------------------- T-2522 audit logging
    @staticmethod
    def _drain_stderr(c: _MCPClient) -> str:
        """Close stdin so the server exits on stdin-EOF, then read everything it
        wrote to stderr (where the audit lines land). The `with`-context close()
        shuts stderr before it can be read, so the audit tests manage the client
        manually and drain here. Fail-open is structural (the audit's try/except
        swallows its own errors), so these tests assert the two observable halves:
        the audit line appears, AND the operation still succeeds."""
        try:
            c._proc.stdin.close()
        except OSError:
            pass
        out = c._proc.stderr.read() or ""
        try:
            c._proc.wait(timeout=5)
        except Exception:
            c._proc.kill()
        for stream in (c._proc.stdout, c._proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
        return out

    def test_audit_send_emits_metadata_line_without_body(self):
        _mailbox.provision(self.hub / "recovery")
        c = self._client()
        try:
            c.initialize()
            resp = c.send("tools/call", params={"name": "mailbox_send", "arguments": {
                "recipient": "recovery", "subject": "s", "body": "SECRET_BODY_MARKER_9x"}})
            self.assertFalse(resp["result"].get("isError", False), resp)  # op succeeds WITH audit
        finally:
            stderr = self._drain_stderr(c)
        self.assertIn("mailbox-audit", stderr, stderr)
        self.assertIn("tool=mailbox_send", stderr)
        self.assertIn("target=recovery", stderr)
        self.assertIn("outcome=ok", stderr)
        self.assertIn(f"persona={_CALLER_PERSONA}", stderr)
        self.assertNotIn("SECRET_BODY_MARKER_9x", stderr)  # metadata only — never the body

    def test_audit_consume_emits_line(self):
        _mailbox.provision(self.hub / _CALLER_PERSONA)  # caller drains its own box
        c = self._client()
        try:
            c.initialize()
            resp = c.send("tools/call", params={"name": "mailbox_consume", "arguments": {}})
            self.assertFalse(resp["result"].get("isError", False), resp)
        finally:
            stderr = self._drain_stderr(c)
        self.assertIn("mailbox-audit", stderr, stderr)
        self.assertIn("tool=mailbox_consume", stderr)
        self.assertIn("outcome=ok", stderr)

    def test_audit_failed_send_emits_error_outcome(self):
        # A failure that REACHES the handler: a fail-closed unknown $USER with a
        # VALID recipient. (An unknown RECIPIENT is rejected by the schema enum
        # before the handler runs, so it never reaches the audit — the handler-
        # level failure that DOES is an unauthorized/misconfigured caller, exactly
        # the security-relevant event the maildir never records.)
        _mailbox.provision(self.hub / "recovery")
        c = self._client(user="ghost_user_unmapped")
        try:
            c.initialize()
            resp = c.send("tools/call", params={"name": "mailbox_send", "arguments": {
                "recipient": "recovery", "subject": "s", "body": "x"}})
            self.assertTrue(resp["result"].get("isError", False), resp)
        finally:
            stderr = self._drain_stderr(c)
        self.assertIn("mailbox-audit", stderr, stderr)
        self.assertIn("tool=mailbox_send", stderr)
        self.assertIn("outcome=error", stderr)

    # ----------------------------------------------------------- mailbox_consume
    def test_consume_drains_messages_and_returns_batch(self):
        """mailbox_consume returns all new messages and acks them to cur/."""
        # Seed the caller's own box — derived from the server's $USER→persona
        # map for the pinned _CALLER_USER (T-1971), not hardcoded.
        caller_inbox = self.hub / _CALLER_PERSONA  # the box the server (as _CALLER_USER) drains
        _mailbox.provision(caller_inbox)  # T-2216: send() no longer auto-provisions
        _mailbox.send(caller_inbox, {"task": "A"}, sender="specialist", subject="first")
        _mailbox.send(caller_inbox, {"task": "B"}, sender="specialist", subject="second")

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
        new_files = list((caller_inbox / "new").iterdir())
        non_dot = [f for f in new_files if not f.name.startswith(".")]
        self.assertEqual(non_dot, [], "all messages must be moved to cur/ after consume")

        cur_files = list((caller_inbox / "cur").iterdir())
        self.assertEqual(len(cur_files), 2)

    def test_consume_messages_carry_provenance_field(self):
        """T-2514/T-2631: each returned message carries an additive `provenance`
        field naming the sender, with every pre-existing key unchanged."""
        caller_inbox = self.hub / _CALLER_PERSONA
        _mailbox.provision(caller_inbox)
        _mailbox.send(caller_inbox, {"task": "A"}, sender="specialist", subject="first")

        with self._client() as c:
            c.initialize()
            resp = c.send(
                "tools/call",
                params={"name": "mailbox_consume", "arguments": {}},
            )

        messages = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(len(messages), 1)
        msg = messages[0]
        # Pre-existing keys pass through byte-identical.
        self.assertEqual(msg["sender"], "specialist")
        self.assertEqual(msg["subject"], "first")
        self.assertEqual(msg["payload"], {"task": "A"})
        self.assertIn("id", msg)
        self.assertIn("ts", msg)
        # New field: names the sender, frames the content as data-not-instruction.
        self.assertIn("provenance", msg)
        self.assertIn("specialist", msg["provenance"])
        self.assertIn("not", msg["provenance"].lower())
        self.assertIn("instruction", msg["provenance"].lower())

    def test_consume_empty_inbox_returns_empty_list(self):
        """Consuming an empty (but EXISTING) inbox returns [] not an error.

        T-2216: distinct from a MISSING inbox, which now returns a distinguishable
        skipped/reason shape instead — see test_consume_missing_inbox_* below."""
        _mailbox.provision(self.hub / _CALLER_PERSONA)
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

    def test_consume_missing_inbox_is_distinguishable_from_lease_held(self):
        """T-2216: a never-provisioned caller inbox must be reported distinctly
        from a lease-held one — a caller retrying "lease held" forever is fine
        (transient); retrying "not provisioned" forever never resolves anything
        (a standing setup problem) and must read differently."""
        caller_inbox = self.hub / _CALLER_PERSONA
        self.assertFalse(caller_inbox.exists(), "fixture must start un-provisioned")

        with self._client() as c:
            c.initialize()
            resp = c.send(
                "tools/call",
                params={"name": "mailbox_consume", "arguments": {}},
            )
        self.assertIn("result", resp)
        result = resp["result"]
        self.assertFalse(result.get("isError", False))
        payload = json.loads(result["content"][0]["text"])
        self.assertTrue(payload.get("skipped"))
        self.assertIn("not provisioned", payload.get("reason", ""))
        self.assertNotIn("lease", payload.get("reason", ""))
        # Consuming a missing inbox must never create it.
        self.assertFalse(caller_inbox.exists(),
                          "a missing-inbox consume must not provision anything")

    def test_consume_skipped_when_lease_held(self):
        """If another consumer holds the lease, consume returns skipped:true result."""
        from _lease import Lease  # noqa: E402

        # server (as _CALLER_USER) → _CALLER_PERSONA box (T-1971)
        caller_inbox = self.hub / _CALLER_PERSONA  # the box the server (as _CALLER_USER) drains
        _mailbox.provision(caller_inbox)
        _mailbox.send(caller_inbox, {"x": 1}, sender="specialist")

        # Hold the lease externally.
        lease = Lease(caller_inbox / ".consumer.lock")
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
        caller_inbox = self.hub / _CALLER_PERSONA  # the box the server (as _CALLER_USER) drains
        _mailbox.provision(caller_inbox)  # T-2216: send() no longer auto-provisions
        for i in range(3):
            _mailbox.send(caller_inbox, {"i": i}, sender="recovery", subject=f"msg-{i}")

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
        caller_inbox = self.hub / _CALLER_PERSONA  # the box the server (as _CALLER_USER) drains
        _mailbox.provision(caller_inbox)
        # One good message (will ack) + one corrupt file (read error → errors[]).
        _mailbox.send(caller_inbox, {"ok": 1}, sender="specialist", subject="good")
        (caller_inbox / "new" / "00-corrupt").write_text("{ not valid json")

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

    def test_consume_partial_messages_carry_provenance(self):
        """T-2514/T-2631: acked messages returned on the partial-drain path
        (errors[] non-empty) also carry the `provenance` field."""
        caller_inbox = self.hub / _CALLER_PERSONA
        _mailbox.provision(caller_inbox)
        _mailbox.send(caller_inbox, {"ok": 1}, sender="recovery", subject="good")
        (caller_inbox / "new" / "00-corrupt").write_text("{ not valid json")

        with self._client() as c:
            c.initialize()
            resp = c.send(
                "tools/call",
                params={"name": "mailbox_consume", "arguments": {}},
            )

        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertTrue(payload.get("partial"), f"expected partial shape: {payload}")
        good = [m for m in payload["messages"] if m["subject"] == "good"][0]
        self.assertIn("provenance", good)
        self.assertIn("recovery", good["provenance"])

    def test_consume_partial_on_escaping_exception_returns_collected(self):
        """If consume() raises outright, the wrapper returns the already-collected
        messages + an error marker (in-process; covers the except branch)."""
        import mailbox_mcp  # noqa: E402 — guarded by class skipUnless(_HAS_MCP)
        import asyncio

        caller_inbox = self.hub / _CALLER_PERSONA  # the box the server (as _CALLER_USER) drains
        _mailbox.provision(caller_inbox)

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
        # T-2631: the escaping-exception path also carries provenance.
        self.assertIn("provenance", payload["messages"][0])
        self.assertIn("specialist", payload["messages"][0]["provenance"])

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


class AdapterLibraryParityT2289(unittest.TestCase):
    """T-2289: the adapter's parameter set must be DERIVED from the library's.

    Three findings landed as three bugs and were one defect — `supersedes`
    shipped in mailbox.send() and unreachable here; `label` dropped so
    `.consumer.lock` recorded `mailbox-consume:<inbox>`, making the actor field
    a tautology on the surface agents use; and a `reason` key the library does
    not carry. Fixing the parameters alone leaves the drift mechanism intact
    and the fourth arrives silently.

    So this test does not check three names. It introspects the LIVE signatures
    and fails when ANY parameter is not deliberately classified in
    mailbox_mcp.py's disposition tables.

    Deliberately dependency-free: the `mcp` SDK is absent on this host and in
    CI, so every other test in this file SKIPS. A parity guard that skips where
    it is needed is not a guard, so the tables are read from SOURCE via ast
    rather than by importing the adapter.
    """

    ADAPTER = _TOOLS_DIR / "mailbox_mcp.py"

    @staticmethod
    def _disposition_keys(src: str, name: str) -> set:
        import ast as _ast
        tree = _ast.parse(src)
        for node in tree.body:
            targets = getattr(node, "targets", []) or ([node.target] if hasattr(node, "target") else [])
            for t in targets:
                if isinstance(t, _ast.Name) and t.id == name:
                    return {k.value for k in node.value.keys}
        raise AssertionError(f"{name} not found in {'mailbox_mcp.py'} — the parity table was removed or renamed")

    def _check(self, fn, table_name):
        import inspect
        src = self.ADAPTER.read_text(encoding="utf-8")
        classified = self._disposition_keys(src, table_name)
        actual = {p for p in inspect.signature(fn).parameters}
        unclassified = actual - classified
        self.assertFalse(
            unclassified,
            f"mailbox.{fn.__name__}() has parameter(s) {sorted(unclassified)} with no deliberate "
            f"disposition in {table_name}. Classify each as EXPOSED / FIXED / OMITTED with the "
            f"reason — do not add it to the table without deciding.",
        )
        stale = classified - actual
        self.assertFalse(
            stale,
            f"{table_name} classifies {sorted(stale)}, which mailbox.{fn.__name__}() no longer "
            f"takes — the table has drifted the OTHER way, which is how `reason` happened.",
        )

    def test_send_every_parameter_is_classified(self):
        from tools import mailbox as _mb
        self._check(_mb.send, "_SEND_PARAM_DISPOSITION")

    def test_consume_every_parameter_is_classified(self):
        from tools import mailbox as _mb
        self._check(_mb.consume, "_CONSUME_PARAM_DISPOSITION")

    def test_CONTROL_the_guard_can_fail(self):
        """Without this, both tests above could pass because the tables are
        simply supersets of everything."""
        src = self.ADAPTER.read_text(encoding="utf-8")
        keys = self._disposition_keys(src, "_SEND_PARAM_DISPOSITION")
        self.assertIn("supersedes", keys, "the parameter this ticket exists for must be classified")
        self.assertTrue(keys, "disposition table parsed empty — the ast extraction is broken")


@unittest.skipUnless(_HAS_MCP, "mcp SDK not installed; mailbox_mcp cannot be imported")
class KnownRecipientsCoverageT2285(unittest.TestCase):
    """T-2285: every registry persona is addressable or exempt-with-a-reason.

    The defect class is SILENCE. codex sat outside `_KNOWN_RECIPIENTS` not by a
    decision but by drift — the enum predated ORG-031 integration — so an MCP
    seat could not mail a persona for which mailbox delivery IS the wake, while
    anything importing `tools/mailbox.py` directly could. Nothing in the code
    could tell that omission from an intentional one, which is exactly what
    `_NO_MCP_RECIPIENT` now makes impossible.

    Same mapped-or-exempt shape as `ancestor_consume._NO_REPLY_INBOX` (T-2252).
    """

    def _registry(self) -> set[str]:
        try:
            from tools.agent_role_map import account_persona_map
        except ImportError:
            from agent_role_map import account_persona_map  # type: ignore[no-redef]
        return set(account_persona_map().values())

    def test_every_registry_persona_is_addressable_or_exempt(self) -> None:
        import mailbox_mcp  # guarded by class skipUnless(_HAS_MCP)

        missing = sorted(
            self._registry()
            - set(mailbox_mcp._KNOWN_RECIPIENTS)
            - set(mailbox_mcp._NO_MCP_RECIPIENT)
        )
        self.assertEqual(
            missing, [],
            f"persona(s) {missing} are in NEITHER _KNOWN_RECIPIENTS (addressable) "
            "nor _NO_MCP_RECIPIENT (why not). A registry persona in neither is "
            "unreachable through this MCP for no stated reason — the T-2285 defect.",
        )

    def test_no_persona_is_both_addressable_and_exempt(self) -> None:
        import mailbox_mcp

        both = sorted(
            set(mailbox_mcp._KNOWN_RECIPIENTS) & set(mailbox_mcp._NO_MCP_RECIPIENT)
        )
        self.assertEqual(both, [], f"contradictory entries: {both}")

    def test_every_exemption_states_a_reason(self) -> None:
        import mailbox_mcp

        for persona, reason in mailbox_mcp._NO_MCP_RECIPIENT.items():
            self.assertTrue(
                reason and reason.strip(),
                f"{persona} is exempt with no stated reason — an unexplained "
                "exemption is the silence this structure exists to prevent",
            )

    def test_codex_is_addressable(self) -> None:
        """The specific T-2285 regression: codex reachable by the library must
        be reachable through the MCP send path too (the T-2144 asymmetry)."""
        import mailbox_mcp

        self.assertIn("codex", mailbox_mcp._KNOWN_RECIPIENTS)

    def test_client_facing_enum_regenerates_from_the_set(self) -> None:
        """Implementation note (1) of the ticket, checked rather than assumed.

        The schema's enum derives from `_KNOWN_RECIPIENTS`. If it ever forks, a
        recipient could be authorized by `_handle_send` while never being
        offered to clients — the two-layer exclusion codex was caught in.
        """
        import mailbox_mcp

        enum = mailbox_mcp._SEND_SCHEMA["properties"]["recipient"]["enum"]
        self.assertEqual(sorted(enum), sorted(mailbox_mcp._KNOWN_RECIPIENTS))
        self.assertIn("codex", enum)

    def test_every_model_facing_recipient_list_is_derived(self) -> None:
        """T-2285: note (1) DID find a second hand-carried copy — one layer up.

        The enum regenerated, and the schema's own `description` was fixed by
        T-2256. But `list_tools()` carried a THIRD copy in the tool description
        — the literal "orchestrator, specialist, recovery", the pre-ORG-042 three — which
        T-2256 missed because it is not in the schema. That string is
        MODEL-FACING: a stale list there means a model never attempts the
        recipients it omits, which is indistinguishable from those recipients
        being unreachable. So it is pinned in the same place as the enum.
        """
        import asyncio

        import mailbox_mcp

        tools = asyncio.run(mailbox_mcp.list_tools())
        send = next(t for t in tools if t.name == "mailbox_send")
        for persona in sorted(mailbox_mcp._KNOWN_RECIPIENTS):
            self.assertIn(
                persona, send.description,
                f"{persona} is addressable but absent from the model-facing "
                "tool description — the stale-enumeration defect this pins",
            )
        for persona in mailbox_mcp._NO_MCP_RECIPIENT:
            self.assertNotIn(persona, send.description)
        # and the schema's own description, fixed by T-2256, must not re-fork
        desc = mailbox_mcp._SEND_SCHEMA["properties"]["recipient"]["description"]
        for persona in sorted(mailbox_mcp._KNOWN_RECIPIENTS):
            self.assertIn(persona, desc)


@unittest.skipUnless(_HAS_MCP, "mcp SDK not installed; mailbox_mcp cannot be imported")
class SkipReasonAttributionT2288(unittest.TestCase):
    """T-2288: an unclassified skip shape must not be reported as a lease contest.

    `_handle_consume` used to branch binary — `reason == "inbox_missing"` else
    "inbox lease held by another consumer" — so every shape the enumeration did
    not know about was ATTRIBUTED to a lease contest that was never established.
    Same defect class as the parameter drift above, one function over, and the
    same one specialist's 12-ack cluster showed a layer down: a plausible name on the
    wrong actor is worse than no name, because the reader cannot tell it was
    never established.

    The bridge's five reasons are LATENT (the adapter calls the library, not the
    bridge). They are classified anyway — an enumeration listing only what is
    reachable today is how this arrives again.
    """

    def test_known_shapes_translate(self) -> None:
        import mailbox_mcp

        self.assertEqual(
            mailbox_mcp._skip_reason_prose(None),
            "inbox lease held by another consumer",
            "mailbox.py's bare lease-held skip carries NO reason key (T-2216)",
        )
        self.assertIn("not provisioned", mailbox_mcp._skip_reason_prose("inbox_missing"))

    def test_every_skip_shape_in_the_tree_is_classified(self) -> None:
        """The enumeration is checked against the SOURCE, not against itself."""
        import re

        import mailbox_mcp

        root = Path(__file__).resolve().parent
        found: set[str] = set()
        for name in ("mailbox.py", "acp_bridge.py"):
            src = (root / name).read_text(encoding="utf-8")
            # Only literal reasons returned alongside an actual skip. The
            # window covers the ternary form too (acp_bridge.py:964-965 returns
            # `"routed_to_main_seat" if ok else "main_seat_wake_failed"`).
            for chunk in re.findall(r'"skipped":\s*True(.{0,200})', src, re.S):
                head = re.search(
                    r'"reason":\s*("[a-z_]+"(?:\s+if\s+[^\n]+?\s+else\s+"[a-z_]+")?)',
                    chunk,
                )
                if head:
                    found.update(re.findall(r'"([a-z_]+)"', head.group(1)))
        self.assertTrue(found, "reason extraction found nothing — the regex has drifted")
        unclassified = found - set(mailbox_mcp._SKIP_REASON_PROSE)
        self.assertFalse(
            unclassified,
            f"skip reason(s) {sorted(unclassified)} are returned somewhere in the tree but carry "
            "no prose in _SKIP_REASON_PROSE, so the adapter would report them as an unrecognized "
            "cause. Classify each with caller-facing prose.",
        )

    def test_unknown_shape_does_NOT_claim_a_lease_contest(self) -> None:
        """The regression itself. This is what the else-branch got wrong."""
        import mailbox_mcp

        prose = mailbox_mcp._skip_reason_prose("some_future_reason")
        self.assertNotIn("lease", prose.lower())
        self.assertIn("unrecognized", prose.lower())
        self.assertIn("some_future_reason", prose, "must report the shape it did not recognize")

    def test_model_facing_description_admits_the_unknown_shape(self) -> None:
        """The description is what a model reads to decide how to react.

        It promised exactly two skip reasons. A third now exists, and the whole
        point of it is that the cause is NOT established — a model told only
        about "lease held (transient, retry later)" and "not provisioned" has no
        branch for it and will pick the closer-sounding one. Same stale
        model-facing-string defect T-2285 fixed in the sibling description.
        """
        import asyncio

        import mailbox_mcp

        tools = asyncio.run(mailbox_mcp.list_tools())
        consume = next(t for t in tools if t.name == "mailbox_consume")
        self.assertIn("unrecognized skip reason", consume.description)
        self.assertIn("do not infer one", consume.description)

    def test_CONTROL_the_guard_can_fail(self) -> None:
        """Without this, the coverage test could pass on an empty extraction or
        a table that is simply a superset of everything."""
        import mailbox_mcp

        self.assertIn(None, mailbox_mcp._SKIP_REASON_PROSE, "the no-reason shape must be classified")
        self.assertIn("main_seat_wake_failed", mailbox_mcp._SKIP_REASON_PROSE,
                      "the latent bridge shapes are the point — they must be carried")
        # a non-str, non-None reason must not crash and must not name a cause
        self.assertIn("unrecognized", mailbox_mcp._skip_reason_prose(object()).lower())


if __name__ == "__main__":
    unittest.main()
