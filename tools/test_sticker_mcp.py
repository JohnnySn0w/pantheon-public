"""test_sticker_mcp.py — the send_sticker MCP server (T-1024).

End to end over the real stdio pipe: spawn tools/sticker_mcp.py, drive initialize ->
tools/list -> tools/call, and point its Bot API base (TELEGRAM_API_BASE) at a local
stub HTTP server that records each request. No call reaches Telegram, and the fixture
manifest and token file live in a tempdir.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

_TOOLS_DIR = Path(__file__).resolve().parent
_SERVER = _TOOLS_DIR / "sticker_mcp.py"
_HAS_MCP = importlib.util.find_spec("mcp") is not None
_TOKEN = "123456:TEST-TOKEN-never-echo"

sys.path.insert(0, str(_TOOLS_DIR))
if _HAS_MCP:
    from agent_role_map import account_persona_map  # noqa: E402
    _CALLER_USER = "claude"
    _CALLER_PERSONA = account_persona_map()[_CALLER_USER]  # the server's own map, never reimplemented
else:
    _CALLER_USER = _CALLER_PERSONA = None


class _Stub(BaseHTTPRequestHandler):
    """Records sendSticker requests; replies ok unless the chat_id is 'refuse'."""
    hits: list = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        form = {k: v[0] for k, v in parse_qs(body).items()}
        type(self).hits.append({"path": self.path, "form": form})
        if form.get("chat_id") == "refuse":
            out = {"ok": False, "description": "Bad Request: chat not found"}
        else:
            out = {"ok": True, "result": {"message_id": 777}}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # keep test output quiet
        pass


@unittest.skipUnless(_HAS_MCP, "mcp SDK not installed")
class StickerMCPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), _Stub)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.manifest = d / "manifest.json"
        cls.manifest.write_text(json.dumps({_CALLER_PERSONA: {"stickers": [
            {"reaction": "approved", "file_id": "FILE-APPROVED"},
            {"reaction": "shipped", "file_id": "FILE-SHIPPED"},
            {"reaction": "wedged", "file_id": ""},  # no file_id => not sendable
        ]}}))
        cls.token_file = d / ".env"
        cls.token_file.write_text(f"OTHER=1\nTELEGRAM_BOT_TOKEN={_TOKEN}\n")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        _Stub.hits.clear()

    def _session(self, calls, user=None):
        """Run one server process: initialize, then each (method, params); return the responses."""
        env = dict(os.environ, USER=user or _CALLER_USER,
                   TELEGRAM_API_BASE=f"http://127.0.0.1:{self.httpd.server_port}",
                   STICKER_MCP_MANIFEST=str(self.manifest), STICKER_MCP_TOKEN_FILE=str(self.token_file))
        # Keep stdin open and read each reply before sending the next request: closing stdin
        # early lets the server exit before it answers.
        p = subprocess.Popen([sys.executable, str(_SERVER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env)
        seen = []

        def ask(msg):
            p.stdin.write(json.dumps(msg) + "\n"); p.stdin.flush()
            if "id" not in msg:
                return None
            while True:
                line = p.stdout.readline()
                if not line:
                    raise AssertionError("server exited early: " + p.stderr.read())
                seen.append(line)
                reply = json.loads(line)
                if reply.get("id") == msg["id"]:
                    return reply

        try:
            ask({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"}}})
            ask({"jsonrpc": "2.0", "method": "notifications/initialized"})
            replies = [ask({"jsonrpc": "2.0", "id": i, "method": m, "params": prm})
                       for i, (m, prm) in enumerate(calls, start=1)]
        finally:
            p.stdin.close()
            err = p.stderr.read()
            p.wait(timeout=60)
        return replies, "".join(seen) + err

    def _call(self, args, user=None):
        (resp,), raw = self._session([("tools/call", {"name": "send_sticker", "arguments": args})], user)
        return resp["result"], raw

    def test_lists_send_sticker_with_required_fields(self):
        (resp,), _ = self._session([("tools/list", {})])
        tools = {t["name"]: t for t in resp["result"]["tools"]}
        self.assertEqual(set(tools), {"send_sticker"})
        self.assertEqual(sorted(tools["send_sticker"]["inputSchema"]["required"]), ["chat_id", "reaction"])

    def test_sends_callers_sticker_and_returns_message_id(self):
        result, raw = self._call({"chat_id": "424242424", "reaction": "approved"})
        self.assertFalse(result.get("isError"), result)
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload, {"sent": True, "persona": _CALLER_PERSONA, "reaction": "approved", "message_id": 777})
        self.assertEqual(len(_Stub.hits), 1)
        hit = _Stub.hits[0]
        self.assertEqual(hit["path"], f"/bot{_TOKEN}/sendSticker")
        self.assertEqual(hit["form"], {"chat_id": "424242424", "sticker": "FILE-APPROVED"})
        self.assertNotIn(_TOKEN, raw)  # the token never reaches the client

    def test_reply_to_threads_under_message(self):
        result, _ = self._call({"chat_id": "424242424", "reaction": "shipped", "reply_to": 11250})
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(json.loads(_Stub.hits[0]["form"]["reply_parameters"]), {"message_id": 11250})

    def test_bad_reply_to_is_rejected_before_any_request(self):
        for bad in ("abc", 0, -5, True):
            result, _ = self._call({"chat_id": "1", "reaction": "approved", "reply_to": bad})
            self.assertTrue(result.get("isError"), (bad, result))
        self.assertEqual(_Stub.hits, [])

    def test_unknown_or_unsendable_reaction_lists_available_and_sends_nothing(self):
        for reaction in ("dancing", "wedged"):
            result, _ = self._call({"chat_id": "1", "reaction": reaction})
            self.assertTrue(result.get("isError"), result)
            text = result["content"][0]["text"]
            self.assertIn("approved", text)
            self.assertIn("shipped", text)
        self.assertEqual(_Stub.hits, [])

    def test_unknown_user_fails_closed_before_any_request(self):
        result, _ = self._call({"chat_id": "1", "reaction": "approved"}, user="nobody-here")
        self.assertTrue(result.get("isError"), result)
        self.assertIn("Unrecognized $USER", result["content"][0]["text"])
        self.assertEqual(_Stub.hits, [])

    def test_telegram_refusal_is_an_error_with_its_reason(self):
        result, raw = self._call({"chat_id": "refuse", "reaction": "approved"})
        self.assertTrue(result.get("isError"), result)
        self.assertIn("chat not found", result["content"][0]["text"])
        self.assertNotIn(_TOKEN, raw)


if __name__ == "__main__":
    unittest.main()
