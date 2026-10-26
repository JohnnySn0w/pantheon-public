"""T-4988 / ORG-103: tests for tools/codex_delivery.py against a scripted fake app-server.

No network and no real codex: the fake speaks the app-server JSON-RPC shapes
recorded in the T-4988 probe traces (codex 0.147.0), in-process over queues, and
once over a real stdio subprocess and once over a WebSocket-on-AF_UNIX socket.

Run: python3 -m unittest tools.test_codex_delivery
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import codex_delivery as cd  # noqa: E402

THREAD = "01a138a3-9e7f-7b22-890b-6b4e1359084b"
ID_RE = re.compile(r"\[delivery-id: ([^\]]+)\]")


# ------------------------------------------------------------------ fake server
class FakeAppServer:
    """Scripted app-server. Handles one connection; records everything it receives.

    Knobs: status/active_turn at resume, reply mode ('telegram': answer through the
    telegram reply tool, the T-5413 receipt | 'echo': only repeat the id in an agent
    message, no longer a receipt | 'plain'),
    auto_complete, steer_error / start_error, resume_overrides, and per-method
    `on` hooks that replace the default handler."""

    def __init__(self, out, *, status="idle", active_turn=None, reply="telegram",
                 auto_complete=True):
        self.out = out
        self.status = status
        self.active = active_turn
        self.reply = reply
        self.auto_complete = auto_complete
        self.steer_error = None
        self.start_error = None
        self.resume_overrides: dict = {}
        self.on: dict = {}
        self.received: list[dict] = []
        self.client_replies: dict = {}
        self._reply_cv = threading.Condition()
        self._n = 0
        self._sreq = 1000

    # -- emit helpers
    def respond(self, rid, result):
        self.out({"id": rid, "result": result})

    def error(self, rid, code, message):
        self.out({"id": rid, "error": {"code": code, "message": message}})

    def notify(self, method, params):
        self.out({"method": method, "params": params})

    def server_request(self, method, params, wait=5.0):
        self._sreq += 1
        rid = self._sreq
        self.out({"id": rid, "method": method, "params": params})
        return rid

    def wait_reply(self, rid, wait=5.0):
        with self._reply_cv:
            self._reply_cv.wait_for(lambda: rid in self.client_replies, timeout=wait)
            return self.client_replies.get(rid)

    def new_turn_id(self):
        self._n += 1
        return f"turn-{self._n}"

    def start_turn(self, tid):
        self.active = tid
        self.notify("thread/status/changed", {"threadId": THREAD,
                                              "status": {"type": "active", "activeFlags": []}})
        self.notify("turn/started", {"threadId": THREAD,
                                     "turn": {"id": tid, "status": "inProgress", "items": []}})

    def start_human_turn(self):
        tid = "human-" + self.new_turn_id()
        self.start_turn(tid)
        return tid

    def agent_message(self, tid, text):
        item = {"type": "agentMessage", "id": "msg_" + tid, "text": text, "phase": "final_answer"}
        self.notify("item/completed", {"threadId": THREAD, "turnId": tid, "item": item})

    def user_message(self, tid, text):
        """The carrying item, as the real server streams it (the tag finds the carrier)."""
        item = {"type": "userMessage", "id": "user_" + tid,
                "content": [{"type": "text", "text": text}]}
        self.notify("item/completed", {"threadId": THREAD, "turnId": tid, "item": item})

    def telegram_reply(self, tid, chat_id="0", text="on it", status="completed", error=None):
        self._n += 1
        item = {"type": "mcpToolCall", "id": f"mcp_{tid}_{self._n}", "server": "telegram",
                "tool": "reply", "status": status, "error": error,
                "arguments": {"chat_id": chat_id, "text": text}}
        self.notify("item/completed", {"threadId": THREAD, "turnId": tid, "item": item})

    def answer(self, tid, input_text):
        """Default end of a turn: the user item, then the reply per the mode."""
        self.user_message(tid, input_text)
        if self.reply == "telegram" and ID_RE.search(input_text):
            self.telegram_reply(tid, cd.operator_chat_id(input_text) or "0")
        self.complete(tid, text=self.reply_text(input_text))

    def complete(self, tid, status="completed", text=None):
        if text is not None:
            self.agent_message(tid, text)
        self.notify("thread/status/changed", {"threadId": THREAD, "status": {"type": "idle"}})
        self.notify("turn/completed", {"threadId": THREAD, "turn": {
            "id": tid, "status": status, "items": [], "error": None}})
        if self.active == tid:
            self.active = None

    def reply_text(self, input_text):
        m = ID_RE.search(input_text)
        if self.reply == "echo" and m:
            return f"ack {m.group(1)}: on it"
        return "done"

    # -- dispatch
    def handle(self, msg):
        self.received.append(msg)
        if "method" not in msg:
            with self._reply_cv:
                self.client_replies[msg.get("id")] = msg
                self._reply_cv.notify_all()
            return
        method = msg["method"]
        if "id" not in msg:
            return
        if method in self.on:
            self.on[method](self, msg)
            return
        getattr(self, "h_" + method.replace("/", "_"), self.h_unknown)(msg)

    def h_unknown(self, msg):
        self.error(msg["id"], -32601, "method not found")

    def h_initialize(self, msg):
        self.respond(msg["id"], {"userAgent": "fake/0.147.0", "codexHome": "/nonexistent",
                                 "platformFamily": "unix", "platformOs": "linux"})

    def h_thread_resume(self, msg):
        p = msg["params"]
        turns = [{"id": self.active, "status": "inProgress", "items": []}] if self.active else []
        status = {"type": "active", "activeFlags": []} if self.active else {"type": self.status}
        res = {"thread": {"id": p["threadId"], "status": status, "turns": turns},
               "sandbox": {"type": cd._SANDBOX_WIRE[p["sandbox"]], "networkAccess": False},
               "approvalPolicy": p["approvalPolicy"], "model": p.get("model", "gpt-fake"),
               "cwd": p.get("cwd", "/tmp"), "modelProvider": "openai", "approvalsReviewer": "user"}
        res.update(self.resume_overrides)
        self.respond(msg["id"], res)

    def _text(self, msg):
        return "".join(i.get("text", "") for i in msg["params"]["input"])

    def h_turn_start(self, msg):
        if self.start_error:
            self.error(msg["id"], *self.start_error)
            return
        tid = self.new_turn_id()
        self.respond(msg["id"], {"turn": {"id": tid, "status": "inProgress", "items": []}})
        self.start_turn(tid)
        if self.auto_complete:
            self.answer(tid, self._text(msg))

    def h_turn_steer(self, msg):
        if self.steer_error:
            self.error(msg["id"], *self.steer_error)
            return
        tid = msg["params"]["expectedTurnId"]
        self.respond(msg["id"], {"turnId": tid})
        if self.auto_complete:
            self.answer(tid, self._text(msg))

    def h_turn_interrupt(self, msg):
        tid = msg["params"]["turnId"]
        self.respond(msg["id"], {})
        self.complete(tid, status="interrupted")

    def methods(self):
        return [m["method"] for m in self.received if "method" in m]

    def requests(self, method):
        return [m for m in self.received if m.get("method") == method]


class QueueTransport:
    """Client side of an in-process pipe to a FakeAppServer running on a thread."""

    def __init__(self, **server_kwargs):
        self.to_client: queue.Queue = queue.Queue()
        self.to_server: queue.Queue = queue.Queue()
        self.closed = False
        self.server = FakeAppServer(self._server_out, **server_kwargs)
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _server_out(self, obj):
        if not self.closed:
            self.to_client.put(json.loads(json.dumps(obj)))

    def _serve(self):
        while True:
            msg = self.to_server.get()
            if msg is None:
                return
            self.server.handle(msg)

    def disconnect(self):
        """Server side drops the connection."""
        self.closed = True
        self.to_client.put(None)

    def send(self, obj):
        if self.closed:
            raise ConnectionError("closed")
        self.to_server.put(json.loads(json.dumps(obj)))

    def recv(self):
        return self.to_client.get()

    def close(self):
        if not self.closed:
            self.closed = True
            self.to_client.put(None)
        self.to_server.put(None)


READ_ONLY = cd.SeatPolicy("read-only", "never", model="gpt-fake", cwd="/tmp/seat")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = Path(self._tmp.name) / "deliveries.jsonl"

    def client(self, policy=READ_ONLY, resume=True, **server_kwargs):
        t = QueueTransport(**server_kwargs)
        c = cd.CodexDeliveryClient(t, policy, self.log_path, seat="consultant", request_timeout=5)
        self.addCleanup(c.close)
        if resume:
            c.connect(THREAD)
        return c, t.server, t

    def records(self, **match):
        out = []
        for rec in cd.DeliveryLog(self.log_path).records():
            if all(rec.get(k) == v for k, v in match.items()):
                out.append(rec)
        return out


# ------------------------------------------------------------------ routing
class RoutingTests(Base):
    def test_idle_operator_dm_starts_turn_and_is_delivered(self):
        c, srv, _ = self.client()
        res = c.deliver("hello from the operator", "tg-8401", "operator_dm", timeout=5)
        self.assertEqual(res.state, cd.DELIVERED, res.evidence)
        self.assertEqual(res.route, "start")
        self.assertEqual(srv.methods(), ["initialize", "initialized", "thread/resume", "turn/start"])
        start = srv.requests("turn/start")[0]["params"]
        text = start["input"][0]["text"]
        self.assertTrue(text.startswith("[delivery-id: tg-8401]\nhello from the operator"))
        # T-5413: no echo instruction; the id appears only on the internal tag line.
        self.assertNotIn("include the delivery id", text)
        self.assertIn("Do not repeat it", text)
        self.assertEqual(text.count("tg-8401"), 1, text)
        self.assertEqual(res.evidence.get("receipt"), "telegram_reply")
        self.assertEqual(start["approvalPolicy"], "never")
        self.assertEqual(start["sandboxPolicy"], {"type": "readOnly", "networkAccess": False})
        self.assertEqual(start["model"], "gpt-fake")
        states = [r["state"] for r in self.records(delivery_id="tg-8401")]
        self.assertEqual(states, [cd.ACCEPTED, cd.DELIVERED])

    def test_busy_operator_dm_steers_into_active_turn(self):
        c, srv, _ = self.client(active_turn="turn-busy")
        self.assertTrue(c.busy)
        res = c.deliver("are you there", "tg-8402", "operator_dm", timeout=5)
        self.assertEqual(res.state, cd.DELIVERED, res.evidence)
        self.assertEqual(res.route, "steer")
        self.assertEqual(res.turn_id, "turn-busy")
        self.assertNotIn("turn/start", srv.methods())
        self.assertEqual(srv.requests("turn/steer")[0]["params"]["expectedTurnId"], "turn-busy")

    def test_steer_race_no_active_turn_falls_back_to_turn_start(self):
        c, srv, _ = self.client(active_turn="turn-ending")
        srv.steer_error = (-32600, "no active turn to steer")
        res = c.deliver("race me", "tg-8403", "operator_dm", timeout=5)
        self.assertEqual(res.state, cd.DELIVERED, res.evidence)
        self.assertEqual(res.route, "steer->start")
        self.assertEqual(srv.methods()[-2:], ["turn/steer", "turn/start"])
        self.assertEqual(len(self.records(event="steer_race")), 1)

    def test_other_steer_error_fails_without_start(self):
        c, srv, _ = self.client(active_turn="turn-x")
        srv.steer_error = (-32600, "expected turn id mismatch")
        res = c.deliver("x", "tg-8404", "operator_dm", timeout=5)
        self.assertEqual(res.state, cd.FAILED)
        self.assertNotIn("turn/start", srv.methods())

    def test_operator_urgent_interrupts_then_starts(self):
        c, srv, _ = self.client(active_turn="turn-long")
        res = c.deliver("STOP", "tg-8405", "operator_urgent", timeout=5)
        self.assertEqual(res.state, cd.DELIVERED, res.evidence)
        self.assertEqual(res.route, "interrupt+start")
        self.assertEqual(srv.methods()[-2:], ["turn/interrupt", "turn/start"])
        self.assertEqual(srv.requests("turn/interrupt")[0]["params"],
                         {"threadId": THREAD, "turnId": "turn-long"})
        self.assertNotEqual(res.turn_id, "turn-long")

    def test_operator_urgent_idle_just_starts(self):
        c, srv, _ = self.client()
        res = c.deliver("STOP", "tg-8406", "operator_urgent", timeout=5)
        self.assertEqual(res.state, cd.DELIVERED)
        self.assertNotIn("turn/interrupt", srv.methods())

    def test_mailbox_while_busy_is_queued_and_nothing_sent(self):
        for cls in ("mailbox", "poke"):
            with self.subTest(cls=cls):
                c, srv, _ = self.client(active_turn="turn-busy")
                before = list(srv.methods())
                res = c.deliver("you have mail", f"mb-{cls}-1", cls, timeout=5)
                self.assertEqual(res.state, cd.QUEUED)
                self.assertIsNone(res.turn_id)
                self.assertEqual(srv.methods(), before)
                self.assertEqual([r["state"] for r in self.records(delivery_id=f"mb-{cls}-1")],
                                 [cd.QUEUED])

    def test_mailbox_idle_starts_and_needs_no_echo(self):
        c, srv, _ = self.client(reply="plain")
        res = c.deliver("you have mail", "mb-0002", "mailbox", timeout=5)
        self.assertEqual(res.state, cd.DELIVERED, res.evidence)
        text = srv.requests("turn/start")[0]["params"]["input"][0]["text"]
        self.assertIn("[delivery-id: mb-0002]", text)
        self.assertNotIn("acknowledge", text)

    def test_queued_mailbox_can_be_sent_later(self):
        c, srv, _ = self.client(active_turn="turn-busy")
        self.assertEqual(c.deliver("m", "mb-0003", "mailbox").state, cd.QUEUED)
        srv.complete("turn-busy")
        self.assertTrue(_wait_for(lambda: not c.busy))
        self.assertEqual(c.deliver("m", "mb-0003", "mailbox", timeout=5).state, cd.DELIVERED)

    def test_controller_busy_without_interrupt_does_not_interrupt(self):
        c, srv, _ = self.client(active_turn="turn-busy")
        srv.on["turn/start"] = lambda s, m: s.error(m["id"], -32600, "turn already active")
        res = c.deliver("ctl", "ctl-0001", "controller", timeout=5)
        self.assertNotIn("turn/interrupt", srv.methods())
        self.assertIn("turn/start", srv.methods())
        self.assertEqual(res.state, cd.FAILED)

    def test_controller_with_interrupt_interrupts_then_starts(self):
        c, srv, _ = self.client(active_turn="turn-busy")
        res = c.deliver("ctl", "ctl-0002", "controller", interrupt=True, timeout=5)
        self.assertEqual(res.state, cd.DELIVERED)
        self.assertEqual(srv.methods()[-2:], ["turn/interrupt", "turn/start"])

    def test_interrupt_flag_rejected_for_non_controller(self):
        c, _, _ = self.client()
        with self.assertRaises(ValueError):
            c.deliver("x", "tg-9999", "operator_dm", interrupt=True)


# ------------------------------------------------------------------ outcomes
class OutcomeTests(Base):
    def test_disconnect_after_accept_is_ambiguous(self):
        c, srv, t = self.client(auto_complete=False)

        def start_then_drop(s, m):
            s.respond(m["id"], {"turn": {"id": "turn-drop", "status": "inProgress"}})
            s.start_turn("turn-drop")
            t.disconnect()
        srv.on["turn/start"] = start_then_drop
        res = c.deliver("hi", "tg-8410", "operator_dm", timeout=5)
        self.assertEqual(res.state, cd.AMBIGUOUS)
        self.assertEqual(res.turn_id, "turn-drop")
        self.assertIn("disconnected", res.evidence["reason"])
        self.assertEqual([r["state"] for r in self.records(delivery_id="tg-8410")],
                         [cd.ACCEPTED, cd.AMBIGUOUS])

    def test_disconnect_before_start_response_is_ambiguous_not_failed(self):
        c, srv, t = self.client()
        srv.on["turn/start"] = lambda s, m: t.disconnect()
        res = c.deliver("hi", "tg-8411", "operator_dm", timeout=5)
        self.assertEqual(res.state, cd.AMBIGUOUS)

    def test_timeout_after_accept_is_ambiguous(self):
        c, _, _ = self.client(auto_complete=False)
        res = c.deliver("hi", "tg-8412", "mailbox", timeout=0.3)
        self.assertEqual(res.state, cd.AMBIGUOUS)
        self.assertIn("timeout", res.evidence["reason"])

    def test_operator_dm_completed_without_a_reply_is_ambiguous(self):
        c, _, _ = self.client(reply="plain")
        res = c.deliver("hi", "tg-8413", "operator_dm", timeout=5)
        self.assertEqual(res.state, cd.AMBIGUOUS)
        self.assertEqual(res.evidence["reason"], "completed without a Telegram reply")
        self.assertEqual(res.evidence["turn_status"], "completed")

    def _turn(self, srv, steps):
        """A scripted turn: steps(server, tid, input_text) emits its items."""
        def start(s, m):
            s.respond(m["id"], {"turn": {"id": "turn-2m", "status": "inProgress"}})
            s.start_turn("turn-2m")
            steps(s, "turn-2m", s._text(m))
            s.complete("turn-2m", text="all done, tests pass")
        srv.on["turn/start"] = start

    DM = "[inbound Telegram DM from the operator | chat_id=424242424 | message_id=77] hi"

    def test_reply_mid_turn_among_other_tools_is_delivered(self):
        """A tool-heavy turn: the reply is one completed item among others."""
        for cls in ("operator_dm", "operator_urgent"):
            with self.subTest(cls=cls):
                c, srv, _ = self.client(auto_complete=False)

                def steps(s, tid, text):
                    s.user_message(tid, text)
                    s.notify("item/completed", {"threadId": THREAD, "turnId": tid, "item": {
                        "type": "commandExecution", "id": "cmd1", "status": "completed"}})
                    s.agent_message(tid, "working on it")
                    s.telegram_reply(tid, "424242424", text="here are the options")
                    s.notify("item/completed", {"threadId": THREAD, "turnId": tid, "item": {
                        "type": "mcpToolCall", "id": "other", "server": "fs", "tool": "read",
                        "status": "completed", "arguments": {}}})
                self._turn(srv, steps)
                res = c.deliver(self.DM, f"tg-8416-{cls}", cls, timeout=5)
                self.assertEqual(res.state, cd.DELIVERED, res.evidence)
                self.assertEqual(res.evidence.get("receipt"), "telegram_reply")

    def test_echo_without_a_reply_is_no_longer_a_receipt(self):
        c, srv, _ = self.client(reply="echo")
        res = c.deliver(self.DM, "tg-8417", "operator_dm", timeout=5)
        self.assertEqual(res.state, cd.AMBIGUOUS)
        self.assertEqual(res.evidence["reason"], "completed without a Telegram reply")

    def test_receipt_rules(self):
        """Each way a reply can fail to prove THIS message was answered."""
        cases = {
            "wrong chat": lambda s, t, x: (s.user_message(t, x), s.telegram_reply(t, "999")),
            "failed call": lambda s, t, x: (s.user_message(t, x),
                                            s.telegram_reply(t, "424242424", status="failed")),
            "call error": lambda s, t, x: (s.user_message(t, x), s.telegram_reply(
                t, "424242424", error={"message": "chat not found"})),
            "other tool": lambda s, t, x: (s.user_message(t, x), s.notify("item/completed", {
                "threadId": THREAD, "turnId": t, "item": {
                    "type": "mcpToolCall", "id": "x", "server": "telegram", "tool": "react",
                    "status": "completed", "arguments": {"chat_id": "424242424"}}})),
            "reply BEFORE this message": lambda s, t, x: (
                s.telegram_reply(t, "424242424"), s.user_message(t, x)),
        }
        for name, steps in cases.items():
            with self.subTest(name):
                c, srv, _ = self.client(auto_complete=False)
                self._turn(srv, steps)
                did = "tg-8418-" + name.split()[0]
                res = c.deliver(self.DM, did, "operator_dm", timeout=5)
                self.assertEqual(res.state, cd.AMBIGUOUS, (name, res.evidence))

    def test_receipt_only_in_turn_completed_items_is_delivered(self):
        """A server that sends the turn's items only on turn/completed, not streamed."""
        c, srv, _ = self.client(auto_complete=False)

        def start(s, m):
            s.respond(m["id"], {"turn": {"id": "turn-tc", "status": "inProgress"}})
            s.start_turn("turn-tc")
            items = [{"type": "userMessage", "id": "u1",
                      "content": [{"type": "text", "text": s._text(m)}]},
                     {"type": "mcpToolCall", "id": "r1", "server": "telegram", "tool": "reply",
                      "status": "completed", "error": None,
                      "arguments": {"chat_id": "424242424", "text": "ok"}}]
            s.notify("turn/completed", {"threadId": THREAD, "turn": {
                "id": "turn-tc", "status": "completed", "items": items, "error": None}})
            s.active = None
        srv.on["turn/start"] = start
        res = c.deliver(self.DM, "tg-8424", "operator_dm", timeout=5)
        self.assertEqual(res.state, cd.DELIVERED, res.evidence)

    def test_string_arguments_are_parsed(self):
        items = [{"type": "userMessage", "content": [{"type": "text", "text": "[delivery-id: d-0001]"}]},
                 {"type": "mcpToolCall", "server": "telegram", "tool": "reply",
                  "status": "completed", "arguments": '{"chat_id": 424242424}'}]
        self.assertTrue(cd.ack_receipt(items, "d-0001", "424242424"))
        self.assertFalse(cd.ack_receipt(items, "d-0002", "424242424"), "other id")
        self.assertFalse(cd.ack_receipt(items[:1] + [dict(items[1], arguments="{bad")],
                                        "d-0001", "424242424"))

    def test_operator_chat_id_parses_the_poller_header(self):
        self.assertEqual(cd.operator_chat_id(self.DM), "424242424")
        self.assertEqual(cd.operator_chat_id(
            "[inbound Telegram DM from the operator | chat_id=-1001 | message_id=3] x"), "-1001")
        self.assertIsNone(cd.operator_chat_id("hi"))

    def test_handled_dm_never_carries_its_id_into_the_reply(self):
        """The ticket's regression: acknowledged, with the id absent from the outbound body."""
        c, srv, _ = self.client(auto_complete=False)
        sent = []

        def steps(s, tid, text):
            s.user_message(tid, text)
            body = "Ranked options: A, B, C"
            sent.append(body)
            s.telegram_reply(tid, cd.operator_chat_id(text), text=body)
        self._turn(srv, steps)
        res = c.deliver(self.DM, "tg-8419", "operator_dm", timeout=5)
        self.assertEqual(res.state, cd.DELIVERED, res.evidence)
        self.assertNotIn("tg-8419", sent[0])
        self.assertNotIn("delivery-id", sent[0])

    def test_turn_bookkeeping_is_pruned(self):
        c, _, _ = self.client()
        for i in range(cd.MAX_TRACKED_TURNS + 20):
            c.deliver("m", f"mb-prune-{i:04d}", "mailbox", timeout=5)
        self.assertLessEqual(len(c._turn_outcome), cd.MAX_TRACKED_TURNS)
        self.assertEqual(c._agent_msgs, {})

    def test_interrupted_turn_is_ambiguous(self):
        c, srv, _ = self.client(auto_complete=False)
        srv.on["turn/start"] = lambda s, m: (
            s.respond(m["id"], {"turn": {"id": "turn-i", "status": "inProgress"}}),
            s.start_turn("turn-i"), s.complete("turn-i", status="interrupted"))
        res = c.deliver("hi", "mb-8414", "mailbox", timeout=5)
        self.assertEqual(res.state, cd.AMBIGUOUS)

    def test_server_error_on_start_is_failed(self):
        c, srv, _ = self.client()
        srv.start_error = (-32603, "internal")
        res = c.deliver("hi", "mb-8415", "mailbox", timeout=5)
        self.assertEqual(res.state, cd.FAILED)
        self.assertEqual(res.evidence["error"]["code"], -32603)


# ------------------------------------------------------------------ idempotency
class IdempotencyTests(Base):
    def test_duplicate_delivered_id_is_noop(self):
        c, srv, _ = self.client()
        first = c.deliver("hi", "tg-8420", "operator_dm", timeout=5)
        self.assertEqual(first.state, cd.DELIVERED)
        second = c.deliver("hi", "tg-8420", "operator_dm", timeout=5)
        self.assertTrue(second.duplicate)
        self.assertEqual(second.state, cd.DELIVERED)
        self.assertEqual(second.turn_id, first.turn_id)
        self.assertEqual(len(srv.requests("turn/start")), 1)

    def test_duplicate_across_clients_sharing_a_log(self):
        c1, _, _ = self.client()
        c1.deliver("hi", "tg-8421", "operator_dm", timeout=5)
        c2, srv2, _ = self.client()
        self.assertTrue(c2.deliver("hi", "tg-8421", "operator_dm", timeout=5).duplicate)
        self.assertEqual(srv2.requests("turn/start"), [])

    def test_ambiguous_is_not_replayed_unless_asked(self):
        c, srv, _ = self.client(reply="plain")
        self.assertEqual(c.deliver("hi", "tg-8422", "operator_dm", timeout=5).state, cd.AMBIGUOUS)
        again = c.deliver("hi", "tg-8422", "operator_dm", timeout=5)
        self.assertTrue(again.duplicate)
        self.assertEqual(again.state, cd.AMBIGUOUS)
        self.assertEqual(len(srv.requests("turn/start")), 1)
        srv.reply = "telegram"
        forced = c.deliver("hi", "tg-8422", "operator_dm", timeout=5, replay_ambiguous=True)
        self.assertEqual(forced.state, cd.DELIVERED)

    def test_accepted_without_terminal_reads_as_ambiguous(self):
        cd.DeliveryLog(self.log_path).append({"delivery_id": "tg-8423", "state": cd.ACCEPTED,
                                              "msg_class": "operator_dm", "turn_id": "t-old"})
        c, srv, _ = self.client()
        res = c.deliver("hi", "tg-8423", "operator_dm", timeout=5)
        self.assertEqual(res.state, cd.AMBIGUOUS)
        self.assertTrue(res.duplicate)
        self.assertEqual(srv.requests("turn/start"), [])

    def test_lock_file_sits_next_to_log(self):
        c, _, _ = self.client()
        c.deliver("hi", "mb-8424", "mailbox", timeout=5)
        self.assertTrue((self.log_path.parent / "deliveries.jsonl.lock").exists())

    def test_invalid_delivery_id_rejected(self):
        c, srv, _ = self.client()
        for bad in ("", "a b c d", "x]y-1234", "ab"):
            with self.assertRaises(ValueError):
                c.deliver("hi", bad, "operator_dm")
        self.assertEqual(srv.requests("turn/start"), [])


# ------------------------------------------------------------------ approvals
class ApprovalTests(Base):
    EXPECTED = {
        "item/commandExecution/requestApproval": {"decision": "decline"},
        "item/fileChange/requestApproval": {"decision": "decline"},
        "execCommandApproval": {"decision": {"denied": {"rejection": cd._DENY}}},
        "applyPatchApproval": {"decision": {"denied": {"rejection": cd._DENY}}},
        "item/permissions/requestApproval": {"permissions": {}, "scope": "turn"},
        "item/tool/requestUserInput": {"answers": {}},
        "mcpServer/elicitation/request": {"action": "decline"},
    }

    def test_every_approval_request_is_declined_and_logged(self):
        c, srv, _ = self.client(auto_complete=False)
        replies = {}

        def start_with_approvals(s, m):
            s.respond(m["id"], {"turn": {"id": "turn-a", "status": "inProgress"}})
            s.start_turn("turn-a")
            for method in self.EXPECTED:
                rid = s.server_request(method, {"threadId": THREAD, "turnId": "turn-a",
                                                "itemId": "it-1", "command": "rm -rf /secret"})
                replies[method] = rid
            s.complete("turn-a", text="nothing to do")
        srv.on["turn/start"] = start_with_approvals
        res = c.deliver("do it", "mb-8430", "mailbox", timeout=5)
        self.assertEqual(res.state, cd.DELIVERED)
        for method, rid in replies.items():
            got = srv.wait_reply(rid)
            self.assertIsNotNone(got, method)
            self.assertEqual(got.get("result"), self.EXPECTED[method], method)
        audit = self.records(event="server_request")
        self.assertEqual(sorted(r["method"] for r in audit), sorted(self.EXPECTED))
        self.assertTrue(all(r["answer"] == "declined" for r in audit))
        self.assertNotIn("rm -rf", self.log_path.read_text())

    def test_unknown_server_request_gets_method_not_found(self):
        c, srv, _ = self.client(auto_complete=False)
        rid = srv.server_request("item/tool/call", {"threadId": THREAD})
        got = srv.wait_reply(rid)
        self.assertEqual(got["error"]["code"], -32601)

    def test_legacy_denial_shape_matches_schema(self):
        # codex 0.147.0 ReviewDecision: the denial variant is {"denied": {"rejection": str}};
        # a bare "denied" string (as the T-4988 probe sent) is not a valid variant.
        for method in ("execCommandApproval", "applyPatchApproval"):
            dec = cd.APPROVAL_REFUSALS[method]["decision"]
            self.assertIsInstance(dec, dict)
            self.assertIsInstance(dec["denied"]["rejection"], str)


# ------------------------------------------------------------------ policy
class PolicyTests(Base):
    def test_resume_sends_explicit_policy(self):
        c, srv, _ = self.client()
        p = srv.requests("thread/resume")[0]["params"]
        self.assertEqual(p, {"threadId": THREAD, "sandbox": "read-only", "approvalPolicy": "never",
                             "model": "gpt-fake", "cwd": "/tmp/seat"})

    def test_broader_sandbox_request_refused_before_send(self):
        c, srv, _ = self.client(resume=False)
        c.initialize()
        with self.assertRaises(cd.PolicyViolation):
            c.resume(THREAD, sandbox="danger-full-access")
        with self.assertRaises(cd.PolicyViolation):
            c.resume(THREAD, approval_policy="on-request")
        with self.assertRaises(cd.PolicyViolation):
            c.resume(THREAD, model="gpt-other")
        with self.assertRaises(cd.PolicyViolation):
            c.resume(THREAD, network_access=True)
        self.assertEqual(srv.requests("thread/resume"), [])

    def test_narrower_sandbox_accepted(self):
        ww = cd.SeatPolicy("workspace-write", "never", writable_roots=["/srv/x"])
        c, srv, _ = self.client(policy=ww, resume=False)
        c.initialize()
        c.resume(THREAD, sandbox="read-only")
        self.assertEqual(srv.requests("thread/resume")[0]["params"]["sandbox"], "read-only")
        c.deliver("m", "mb-8440", "mailbox", timeout=5)
        self.assertEqual(srv.requests("turn/start")[0]["params"]["sandboxPolicy"],
                         {"type": "readOnly", "networkAccess": False})

    def test_workspace_write_turn_carries_declared_roots(self):
        ww = cd.SeatPolicy("workspace-write", "on-request", writable_roots=["/srv/x"])
        c, srv, _ = self.client(policy=ww)
        c.deliver("m", "mb-8441", "mailbox", timeout=5)
        self.assertEqual(srv.requests("turn/start")[0]["params"]["sandboxPolicy"],
                         {"type": "workspaceWrite", "networkAccess": False,
                          "writableRoots": ["/srv/x"]})

    def test_server_reporting_broader_settings_is_refused(self):
        cases = [
            {"sandbox": {"type": "dangerFullAccess"}},
            {"sandbox": {"type": "externalSandbox", "networkAccess": "enabled"}},
            {"sandbox": {"type": "readOnly", "networkAccess": True}},
            {"approvalPolicy": "on-request"},
            {"model": "gpt-other"},
        ]
        for override in cases:
            with self.subTest(override=override):
                c, srv, _ = self.client(resume=False)
                srv.resume_overrides = override
                with self.assertRaises(cd.PolicyViolation):
                    c.connect(THREAD)

    def test_deliver_requires_resume(self):
        c, _, _ = self.client(resume=False)
        with self.assertRaises(cd.DeliveryError):
            c.deliver("x", "tg-8442", "operator_dm")

    def test_seat_policy_validates(self):
        with self.assertRaises(ValueError):
            cd.SeatPolicy("yolo", "never")
        with self.assertRaises(ValueError):
            cd.SeatPolicy("read-only", "always")


# ------------------------------------------------------------------ human turns
class HumanTurnTests(Base):
    def test_human_started_turn_marks_busy_and_is_logged(self):
        c, srv, _ = self.client()
        self.assertFalse(c.busy)
        tid = srv.start_human_turn()
        self.assertTrue(_wait_for(lambda: c.busy))
        self.assertEqual(c.human_turns, [tid])
        self.assertEqual(len(self.records(event="human_turn_observed", turn_id=tid)), 1)
        res = c.deliver("mail", "mb-8450", "mailbox", timeout=5)
        self.assertEqual(res.state, cd.QUEUED)
        res = c.deliver("dm", "tg-8451", "operator_dm", timeout=5)
        self.assertEqual(res.route, "steer")
        self.assertEqual(res.turn_id, tid)
        self.assertEqual(res.state, cd.DELIVERED)

    def test_own_turns_are_not_logged_as_human(self):
        c, _, _ = self.client()
        c.deliver("m", "mb-8452", "mailbox", timeout=5)
        self.assertEqual(c.human_turns, [])
        self.assertEqual(self.records(event="human_turn_observed"), [])

    def test_own_turn_started_before_response_is_not_human(self):
        c, srv, _ = self.client()

        def started_first(s, m):
            s.start_turn("turn-early")
            s.respond(m["id"], {"turn": {"id": "turn-early", "status": "inProgress"}})
            s.complete("turn-early", text="done")
        srv.on["turn/start"] = started_first
        self.assertEqual(c.deliver("m", "mb-8453", "mailbox", timeout=5).state, cd.DELIVERED)
        self.assertEqual(c.human_turns, [])

    def test_human_turn_racing_our_start_falls_back_to_steer(self):
        c, srv, _ = self.client()

        def human_won(s, m):
            tid = s.start_human_turn()
            s.error(m["id"], -32600, "turn already in progress")
            s.on.pop("turn/start")
        srv.on["turn/start"] = human_won
        res = c.deliver("dm", "tg-8454", "operator_dm", timeout=5)
        self.assertEqual(res.route, "start->steer")
        self.assertEqual(res.state, cd.DELIVERED, res.evidence)
        self.assertEqual(len(c.human_turns), 1)


class SharedTurnTests(Base):
    """T-5308 drill 2 (live consultant): a controller prompt starts a turn, an operator DM
    is steered into it, and the one turn/completed must settle BOTH deliveries."""

    def test_two_deliveries_in_one_turn_both_settle(self):
        c, srv, _ = self.client(auto_complete=False)
        results = {}

        def run(did, cls, text):
            results[did] = c.deliver(text, did, cls, timeout=5)
        t1 = threading.Thread(target=run, args=("ctl-0001", "controller", "do a long thing"))
        t1.start()
        self.assertTrue(_wait_for(lambda: srv.active))
        tid = srv.active
        t2 = threading.Thread(target=run, args=("dm-0001", "operator_dm", "hello"))
        t2.start()
        self.assertTrue(_wait_for(lambda: srv.requests("turn/steer")))
        srv.user_message(tid, srv._text(srv.requests("turn/steer")[0]))  # the steered DM
        srv.telegram_reply(tid)
        srv.complete(tid, text="done")
        t1.join(6)
        t2.join(6)
        self.assertEqual(results["ctl-0001"].state, cd.DELIVERED)
        self.assertEqual(results["dm-0001"].state, cd.DELIVERED, results["dm-0001"].evidence)
        self.assertEqual(results["dm-0001"].route, "steer")
        self.assertEqual(results["dm-0001"].turn_id, tid)


class BusyAfterStartTests(Base):
    """T-5308 drill 5 (live consultant): a mail sent right after a DM's turn/start must
    queue, not fold into the DM's turn, even before turn/started is seen."""

    def test_mail_right_after_a_start_queues(self):
        c, srv, _ = self.client(auto_complete=False)
        srv.on["turn/start"] = lambda s, m: s.respond(          # reply, but never emit
            m["id"], {"turn": {"id": "t-dm", "status": "inProgress", "items": []}})
        done = {}
        th = threading.Thread(target=lambda: done.setdefault(
            "dm", c.deliver("hi", "dm-0001", "operator_dm", timeout=2)))
        th.start()
        self.assertTrue(_wait_for(lambda: srv.requests("turn/start")))
        self.assertTrue(_wait_for(lambda: c.busy))
        res = c.deliver("mail", "mb-0001", "mailbox", timeout=2)
        self.assertEqual(res.state, cd.QUEUED)
        self.assertEqual(len(srv.requests("turn/start")), 1)
        th.join(4)

    def test_a_turn_that_already_completed_does_not_stick_busy(self):
        c, srv, _ = self.client()                 # auto_complete: done before return
        res = c.deliver("hi", "dm-0002", "operator_dm", timeout=5)
        self.assertEqual(res.state, cd.DELIVERED)
        self.assertTrue(_wait_for(lambda: not c.busy))


class FoldedStartTests(Base):
    """codex 0.147.0, observed live (T-4988 evidence/busy_turnstart.log): a
    turn/start sent while another turn is active succeeds with a fresh turn id,
    but the input is folded into the ACTIVE turn and the returned id never
    starts or completes."""

    @staticmethod
    def user_message(s, tid, text):
        item = {"type": "userMessage", "id": "um_" + tid,
                "content": [{"type": "text", "text": text, "text_elements": []}]}
        s.notify("item/started", {"threadId": THREAD, "turnId": tid, "item": item})
        s.notify("item/completed", {"threadId": THREAD, "turnId": tid, "item": item})

    def folding_server(self, srv):
        def fold(s, m):
            text = s._text(m)
            phantom = s.new_turn_id()
            s.respond(m["id"], {"turn": {"id": phantom, "status": "inProgress", "items": []}})
            carrier = s.active
            self.user_message(s, carrier, text)
            s.telegram_reply(carrier)
            s.complete(carrier, text=s.reply_text(text))
        srv.on["turn/start"] = fold

    def test_start_racing_a_human_turn_is_delivered_via_the_carrier_turn(self):
        c, srv, _ = self.client()
        self.folding_server(srv)

        def human_then_fold(s, m):
            s.start_human_turn()              # began after our idle read
            srv.on.pop("turn/start")
            self.folding_server(srv)
            srv.on["turn/start"](s, m)
        srv.on["turn/start"] = human_then_fold
        res = c.deliver("dm", "tg-8460", "operator_dm", timeout=2)
        self.assertEqual(res.state, cd.DELIVERED, res.evidence)
        self.assertTrue(res.turn_id.startswith("human-"), res.turn_id)
        self.assertEqual(res.evidence.get("folded_into"), res.turn_id)
        self.assertEqual(self.records(delivery_id="tg-8460")[-1]["turn_id"], res.turn_id)

    def test_mailbox_folded_into_a_turn_is_delivered(self):
        c, srv, _ = self.client()

        def human_then_fold(s, m):
            s.start_human_turn()
            srv.on.pop("turn/start")
            self.folding_server(srv)
            srv.on["turn/start"](s, m)
        srv.on["turn/start"] = human_then_fold
        res = c.deliver("m", "mb-8461", "mailbox", timeout=2)
        self.assertEqual(res.state, cd.DELIVERED, res.evidence)

    def test_user_message_in_own_turn_keeps_that_turn(self):
        c, srv, _ = self.client()

        def own(s, m):
            tid = s.new_turn_id()
            s.respond(m["id"], {"turn": {"id": tid, "status": "inProgress", "items": []}})
            s.start_turn(tid)
            self.user_message(s, tid, s._text(m))
            s.telegram_reply(tid)
            s.complete(tid, text=s.reply_text(s._text(m)))
        srv.on["turn/start"] = own
        res = c.deliver("dm", "tg-8462", "operator_dm", timeout=2)
        self.assertEqual(res.state, cd.DELIVERED, res.evidence)
        self.assertNotIn("folded_into", res.evidence)


# ------------------------------------------------------------------ logging hygiene
class LoggingTests(Base):
    def test_log_holds_only_a_scrubbed_preview(self):
        c, _, _ = self.client()
        secret = "ghp_" + "A1b2C3d4" * 5
        body = "please rotate token=" + secret + " " + ("lorem ipsum " * 40)
        c.deliver(body, "tg-8460", "operator_dm", timeout=5)
        raw = self.log_path.read_text()
        self.assertNotIn(secret, raw)
        self.assertNotIn("lorem ipsum " * 10, raw)
        for rec in self.records(delivery_id="tg-8460"):
            if "preview" in rec:
                self.assertLessEqual(len(rec["preview"]), cd.PREVIEW_CHARS)

    def test_redact_patterns(self):
        for s in ("sk-abcdefghijklmnop", "Bearer abc.def.ghijkl", "password: hunter22",
                  "123456789:AAEhBOweik6ad6PsVMRxjeQKqDDu2kXjq0Y"):
            self.assertNotIn(s, cd.redact("x " + s + " y"), s)


# ------------------------------------------------------------------ real transports
class StdioTransportTests(Base):
    def test_stdio_subprocess_fake_round_trip(self):
        argv = [sys.executable, str(Path(__file__).resolve()), "--fake-stdio-server"]
        t = cd.StdioTransport(argv)
        c = cd.CodexDeliveryClient(t, READ_ONLY, self.log_path, request_timeout=10)
        self.addCleanup(c.close)
        c.connect(THREAD)
        res = c.deliver("hi over stdio", "tg-8470", "operator_dm", timeout=10)
        self.assertEqual(res.state, cd.DELIVERED, res.evidence)


class WebSocketTransportTests(Base):
    def test_unix_websocket_round_trip(self):
        path = os.path.join(self._tmp.name, "as.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        self.addCleanup(srv.close)
        seen = {}

        def serve():
            conn, _ = srv.accept()
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += conn.recv(4096)
            key = re.search(rb"Sec-WebSocket-Key: (\S+)", buf).group(1)
            accept = base64.b64encode(hashlib.sha1(
                key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                         b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            b0, b1 = conn.recv(2)
            seen["masked"] = bool(b1 & 0x80)
            n = b1 & 0x7F
            mask = conn.recv(4)
            data = b""
            while len(data) < n:
                data += conn.recv(n - len(data))
            req = json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
            seen["req"] = req
            body = json.dumps({"id": req["id"], "result": {"ok": True}}).encode()
            half = len(body) // 2
            conn.sendall(bytes([0x89, 0]))                              # ping
            conn.sendall(bytes([0x01, half]) + body[:half])             # text, not final
            conn.sendall(bytes([0x80, len(body) - half]) + body[half:]) # continuation, final
            b0, b1 = conn.recv(2)                                       # pong from client
            seen["pong_opcode"] = b0 & 0x0F
            conn.recv(4)
            conn.sendall(bytes([0x88, 0]))                              # close
            conn.close()
        th = threading.Thread(target=serve, daemon=True)
        th.start()
        t = cd.UnixWebSocketTransport(path)
        t.send({"id": 7, "method": "initialize"})
        self.assertEqual(t.recv(), {"id": 7, "result": {"ok": True}})
        self.assertIsNone(t.recv())
        t.close()
        th.join(5)
        self.assertTrue(seen["masked"])
        self.assertEqual(seen["req"]["method"], "initialize")
        self.assertEqual(seen["pong_opcode"], 0xA)

    def test_frame_length_encodings(self):
        for n in (5, 300, 70000):
            frame = cd.UnixWebSocketTransport.encode_frame(0x1, b"x" * n, None)
            if n < 126:
                self.assertEqual(frame[1], n)
            elif n < 65536:
                self.assertEqual(struct.unpack(">H", frame[2:4])[0], n)
            else:
                self.assertEqual(struct.unpack(">Q", frame[2:10])[0], n)


class CliTests(Base):
    def test_cli_deliver_and_status_over_stdio(self):
        import contextlib
        import io
        cmd = f"{sys.executable} {Path(__file__).resolve()} --fake-stdio-server"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cd._cli(["deliver", "--stdio-cmd", cmd, "--thread-id", THREAD,
                          "--sandbox", "read-only", "--approval-policy", "never",
                          "--log", str(self.log_path), "--id", "cli-0001",
                          "--class", "operator_dm", "--text", "hi", "--timeout", "10"])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["state"], cd.DELIVERED)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cd._cli(["status", "--log", str(self.log_path), "--id", "cli-0001"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["state"], cd.DELIVERED)


def _wait_for(pred, timeout=5.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _run_fake_stdio_server():
    lock = threading.Lock()

    def out(obj):
        with lock:
            sys.stdout.write(json.dumps(obj) + "\n")
            sys.stdout.flush()
    server = FakeAppServer(out)
    for line in sys.stdin:
        line = line.strip()
        if line:
            server.handle(json.loads(line))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fake-stdio-server":
        _run_fake_stdio_server()
    else:
        unittest.main()
