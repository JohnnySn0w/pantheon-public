"""ORG-103 L3 (T-5304): tests for tools/codex_seat_delivery.py against a fake app-server.

One test class per concern; every row of the build note's recovery table has a test
(RecoveryTableTests). The fake is test_codex_delivery's FakeAppServer plus the methods
the writer adds (thread/loaded/list, thread/read, account/read) and a turn history that
survives reconnects, so a dropped connection can be reconciled from the server's record.

Run: python3 -m unittest tools.test_codex_seat_delivery
"""
from __future__ import annotations

import json
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import codex_delivery as cd  # noqa: E402
from tools import codex_seat_delivery as sd  # noqa: E402
from tools.test_codex_delivery import THREAD, FakeAppServer  # noqa: E402

CWD = "/home/codex"
POLICY = cd.SeatPolicy("danger-full-access", "never", model="gpt-fake", cwd=CWD)


class SeatServer(FakeAppServer):
    """A fake server whose state outlives any one connection."""

    def __init__(self, **kw):
        super().__init__(lambda obj: None, **kw)
        self.history: dict[str, dict] = {}
        self.loaded = [THREAD]
        self.account = {"type": "chatgpt", "email": "consultant@example.test", "planType": "pro"}
        self.hang_heartbeat = False
        self.resume_error = None
        self.turn_error = None

    def h_thread_resume(self, msg):
        if self.resume_error:
            self.error(msg["id"], *self.resume_error)
            return
        self.resume_overrides.setdefault("cwd", CWD)
        super().h_thread_resume(msg)

    def h_thread_loaded_list(self, msg):
        if not self.hang_heartbeat:
            self.respond(msg["id"], {"data": list(self.loaded), "nextCursor": None})

    def h_thread_read(self, msg):
        self.respond(msg["id"], {"thread": {"id": THREAD, "turns": list(self.history.values())}})

    def h_account_read(self, msg):
        self.respond(msg["id"], {"account": self.account, "requiresOpenaiAuth": True})

    def h_turn_start(self, msg):
        text = self._text(msg)
        if self.active and not self.start_error:
            # codex 0.147.0: a turn/start while busy folds into the active turn.
            self.history[self.active]["items"].append(_user(text))
            self.notify("item/started", {"threadId": THREAD, "turnId": self.active,
                                         "item": _user(text)})
            self.respond(msg["id"], {"turn": {"id": self.new_turn_id(), "status": "inProgress",
                                              "items": []}})
            return
        if self.start_error:
            self.error(msg["id"], *self.start_error)
            return
        tid = self.new_turn_id()
        self.history[tid] = {"id": tid, "status": "inProgress", "items": [_user(text)]}
        self.respond(msg["id"], {"turn": {"id": tid, "status": "inProgress", "items": []}})
        self.start_turn(tid)
        self.notify("item/started", {"threadId": THREAD, "turnId": tid, "item": _user(text)})
        if self.auto_complete:
            self.complete(tid, text=self.reply_text(text))

    def h_turn_steer(self, msg):
        tid = msg["params"]["expectedTurnId"]
        text = self._text(msg)
        if tid in self.history:
            self.history[tid]["items"].append(_user(text))
        self.notify("item/started", {"threadId": THREAD, "turnId": tid, "item": _user(text)})
        self.respond(msg["id"], {"turnId": tid})
        if self.auto_complete:
            self.complete(tid, text=self.reply_text(text))

    def complete(self, tid, status="completed", text=None):
        h = self.history.setdefault(tid, {"id": tid, "items": []})
        if status == "completed" and text is not None and self.reply == "telegram":
            # T-5413 receipt: answer each carried operator DM through the telegram tool.
            for i in list(h["items"]):
                if i["type"] == "userMessage" and cd.DELIVERY_TAG_RE.search(
                        i["content"][0]["text"]):
                    self._reply_item(tid, cd.operator_chat_id(i["content"][0]["text"]) or "0")
        if text is not None:
            h["items"].append({"type": "agentMessage", "id": "m" + tid, "text": text})
        h["status"] = status
        if self.turn_error and status == "failed":
            h["error"] = self.turn_error
        if text is not None:
            self.agent_message(tid, text)
        self.notify("thread/status/changed", {"threadId": THREAD, "status": {"type": "idle"}})
        self.notify("turn/completed", {"threadId": THREAD, "turn": {
            "id": tid, "status": status, "items": [], "error": h.get("error")}})
        if self.active == tid:
            self.active = None

    def _reply_item(self, tid, chat_id):
        item = {"type": "mcpToolCall", "id": f"mcp{tid}{len(self.history[tid]['items'])}",
                "server": "telegram", "tool": "reply", "status": "completed", "error": None,
                "arguments": {"chat_id": chat_id, "text": "on it"}}
        self.history[tid]["items"].append(item)
        self.notify("item/completed", {"threadId": THREAD, "turnId": tid, "item": item})

    def answer_last(self, tid):
        """Complete `tid` answering, on Telegram, every operator DM carried in it."""
        mode, self.reply = self.reply, "telegram"
        try:
            self.complete(tid, text="done")
        finally:
            self.reply = mode

    def user_texts(self):
        return [self._text(m) for m in self.received
                if m.get("method") in ("turn/start", "turn/steer")]


def _user(text):
    return {"type": "userMessage", "id": "u", "content": [{"type": "text", "text": text}]}


class Conn:
    """One connection to a SeatServer; the server is rebound to the newest one."""

    def __init__(self, server: SeatServer):
        self.to_client: queue.Queue = queue.Queue()
        self.to_server: queue.Queue = queue.Queue()
        self.closed = False
        self.server = server
        server.out = self._server_out
        threading.Thread(target=self._serve, daemon=True).start()

    def _server_out(self, obj):
        if not self.closed:
            self.to_client.put(json.loads(json.dumps(obj)))

    def _serve(self):
        while True:
            msg = self.to_server.get()
            if msg is None:
                return
            self.server.handle(msg)

    def drop(self):
        self.closed = True
        self.to_client.put(None)
        self.to_server.put(None)

    def send(self, obj):
        if self.closed:
            raise ConnectionError("closed")
        self.to_server.put(json.loads(json.dumps(obj)))

    def recv(self):
        return self.to_client.get()

    def close(self):
        if not self.closed:
            self.drop()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # ignore_errors: a straggling reader-thread audit line must not fail teardown.
        self.addCleanup(shutil.rmtree, self._tmp.name, True)
        self.state = Path(self._tmp.name) / "codex-appserver"
        self.state.mkdir()
        (self.state / "thread.json").write_text(json.dumps({"thread_id": THREAD}))
        self.server = SeatServer()
        self.conns: list[Conn] = []
        self.connect_errors: list[Exception] = []
        self.pages: list[tuple[str, str, str]] = []
        self.restarts = 0
        self.clock = Clock()

    def _connect(self):
        if self.connect_errors:
            raise self.connect_errors.pop(0)
        c = Conn(self.server)
        self.conns.append(c)
        return c

    def _restart(self):
        self.restarts += 1

    def path(self, account_email=None, **kw):
        b = sd.SeatBinding("consultant", POLICY, self.state / "thread.json", account_email)
        kw.setdefault("request_timeout", 2.0)
        kw.setdefault("turn_timeout", 5.0)
        p = sd.SeatDeliveryPath(b, self.state, self._connect,
                                pager=lambda k, s, body: self.pages.append((k, s, body)),
                                restart_server=self._restart, clock=self.clock, **kw)
        # Cleanups run LIFO: close first (a waiting worker sees the disconnect and
        # records at once), then join the workers, and only then drop the tempdir.
        self.addCleanup(p.join_workers)
        self.addCleanup(p.close)
        return p

    def enqueue(self, text, cls, did):
        return sd.enqueue(self.state, text, cls, seat="consultant", delivery_id=did)

    def state_of(self, did):
        rec = cd.DeliveryLog(self.state / "deliveries.jsonl").last_state(did)
        return rec and rec["state"]

    def settle(self, p, did, want):
        self.assertTrue(wait_for(lambda: self.state_of(did) == want),
                        f"{did}: {self.state_of(did)} != {want}")
        p.join_workers()

    def page_keys(self):
        return [k for k, _, _ in self.pages]

    def starts(self):
        return self.server.requests("turn/start")


# ------------------------------------------------------------------ single writer
class SingleWriterTests(Base):
    def test_second_writer_is_rejected(self):
        lock = sd.WriterLock(self.state / "writer.lock")
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        rc = sd._cli(["run", "--state-dir", str(self.state), "--sock", "/nonexistent",
                      "--seat", "consultant", "--sandbox", "danger-full-access",
                      "--approval-policy", "never"])
        self.assertEqual(rc, sd.EXIT_DUPLICATE)

    def test_seq_is_monotonic_and_seeded_from_the_log(self):
        log = sd.MonotonicLog(self.state / "deliveries.jsonl")
        log.append({"event": "a"})
        log.append({"event": "b"})
        again = sd.MonotonicLog(self.state / "deliveries.jsonl")
        again.append({"event": "c"})
        self.assertEqual([r["seq"] for r in again.records()], [1, 2, 3])

    def test_duplicate_spool_of_a_delivered_id_is_not_resent(self):
        p = self.path()
        self.enqueue("hello", "operator_dm", "dm-0001")
        p.tick()
        self.settle(p, "dm-0001", cd.DELIVERED)
        self.enqueue("hello", "operator_dm", "dm-0001")
        p.tick()
        p.tick()
        self.assertEqual(len(self.starts()), 1)


# ------------------------------------------------------------------ routing
class RoutingTests(Base):
    def test_claimed_in_priority_order(self):
        p = self.path()
        self.enqueue("mail", "mailbox", "mb-0001")
        self.enqueue("dm", "operator_dm", "dm-0001")
        self.enqueue("stop", "operator_urgent", "ur-0001")
        p.tick()
        for did in ("mb-0001", "dm-0001", "ur-0001"):
            self.settle(p, did, cd.DELIVERED)
        order = [cd.DELIVERY_TAG_RE.search(t).group(1) for t in self.server.user_texts()]
        self.assertEqual(order, ["ur-0001", "dm-0001", "mb-0001"])

    def test_mail_while_busy_queues_then_drains_one_per_idle_transition(self):
        self.server.auto_complete = False
        self.server.active = "human-1"
        self.server.history["human-1"] = {"id": "human-1", "status": "inProgress", "items": []}
        p = self.path()
        self.enqueue("mail", "mailbox", "mb-0001")
        self.enqueue("poke", "poke", "pk-0001")
        p.tick()
        self.settle(p, "mb-0001", cd.QUEUED)
        self.settle(p, "pk-0001", cd.QUEUED)
        self.assertEqual(self.starts(), [])

        self.server.complete("human-1")
        self.assertTrue(wait_for(lambda: not p.client.busy))
        p.tick()
        self.assertTrue(wait_for(lambda: len(self.starts()) == 1))
        self.assertIn("mb-0001", self.server.user_texts()[0])
        p.tick()
        self.assertEqual(len(self.starts()), 1, "one drain per idle transition")

        self.server.complete(self.server.active)
        self.settle(p, "mb-0001", cd.DELIVERED)
        p.tick()
        self.assertTrue(wait_for(lambda: len(self.starts()) == 2))
        self.assertIn("pk-0001", self.server.user_texts()[1])

    def test_drain_is_one_per_idle_transition_even_when_turns_finish_at_once(self):
        self.server.active = "human-1"
        self.server.history["human-1"] = {"id": "human-1", "status": "inProgress", "items": []}
        self.server.auto_complete = False
        p = self.path()
        self.enqueue("mail", "mailbox", "mb-0001")
        self.enqueue("poke", "poke", "pk-0001")
        p.tick()
        self.settle(p, "pk-0001", cd.QUEUED)
        self.server.auto_complete = True        # drained turns now finish instantly
        self.server.complete("human-1")
        self.assertTrue(wait_for(lambda: not p.client.busy))
        p.tick()
        self.settle(p, "mb-0001", cd.DELIVERED)
        self.assertEqual(len(self.starts()), 1, "drained more than one per idle transition")
        p.tick()
        self.settle(p, "pk-0001", cd.DELIVERED)
        self.assertEqual(len(self.starts()), 2)

    def test_dm_steers_into_busy_turn(self):
        self.server.auto_complete = False
        self.server.active = "human-1"
        self.server.history["human-1"] = {"id": "human-1", "status": "inProgress", "items": []}
        p = self.path()
        self.enqueue("dm", "operator_dm", "dm-0001")
        p.tick()
        self.assertTrue(wait_for(lambda: self.server.requests("turn/steer")))
        self.server.answer_last("human-1")
        self.settle(p, "dm-0001", cd.DELIVERED)


# ------------------------------------------------------------------ identity
class IdentityTests(Base):
    def test_thread_json_change_refuses_and_pages(self):
        p = self.path()
        p.tick()
        (self.state / "thread.json").write_text(json.dumps({"thread_id": "other-thread"}))
        self.enqueue("dm", "operator_dm", "dm-0001")
        p.tick()
        self.assertEqual(p.hold, sd.HOLD_IDENTITY)
        self.assertEqual(self.starts(), [])
        self.assertIn("hold:identity_mismatch", self.page_keys())
        self.assertTrue((self.state / "pending" / "dm-0001.json").exists(), "nothing dropped")

    def test_cwd_mismatch_on_resume_holds(self):
        self.server.resume_overrides["cwd"] = "/tmp/elsewhere"
        p = self.path()
        self.enqueue("dm", "operator_dm", "dm-0001")
        p.tick()
        self.assertEqual(p.hold, sd.HOLD_IDENTITY)
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.state_of("dm-0001"), None)

    def test_model_mismatch_on_resume_holds(self):
        self.server.resume_overrides["model"] = "gpt-other"
        p = self.path()
        p.tick()
        self.assertEqual(p.hold, sd.HOLD_IDENTITY)

    def test_account_mismatch_holds(self):
        p = self.path(account_email="someone-else@example.test")
        p.tick()
        self.assertEqual(p.hold, sd.HOLD_IDENTITY)

    def test_matching_account_connects(self):
        p = self.path(account_email="consultant@example.test")
        p.tick()
        self.assertIsNone(p.hold)
        self.assertIsNotNone(p.client)

    def test_held_messages_are_recorded_queued(self):
        self.server.resume_overrides["cwd"] = "/tmp/elsewhere"
        p = self.path()
        p.tick()
        self.enqueue("dm", "operator_dm", "dm-0001")
        p.tick()
        self.assertEqual(self.state_of("dm-0001"), cd.QUEUED)


# ------------------------------------------------------------------ approvals
class ApprovalTests(Base):
    def test_any_server_request_is_declined_and_pages(self):
        p = self.path()
        p.tick()
        rid = self.server.server_request("item/commandExecution/requestApproval",
                                         {"threadId": THREAD, "turnId": "t1", "itemId": "i1"})
        reply = self.server.wait_reply(rid)
        self.assertEqual(reply["result"], {"decision": "decline"})
        self.assertTrue(wait_for(lambda: p._anomalies))   # the reply precedes the emit
        p.tick()
        self.assertTrue(any(k.startswith("approval:item/commandExecution") for k in self.page_keys()))


# ------------------------------------------------------------------ recovery table
class RecoveryTableTests(Base):
    # row 1: app-server dies
    def test_server_dies_backoff_and_page_after_three(self):
        self.connect_errors = [ConnectionRefusedError("refused")] * 4
        p = self.path()
        delays = []
        for _ in range(4):
            p.tick()
            delays.append(p._next_reconnect - self.clock.t)
            self.clock.t = p._next_reconnect
        self.assertEqual(delays, [1, 2, 4, 8])
        self.assertEqual(self.page_keys().count("server_down"), 1)
        p.tick()
        self.assertIsNotNone(p.client)
        self.assertEqual(p._reconnect_failures, 0)

    def test_server_dies_mid_turn_goes_ambiguous_and_is_never_replayed(self):
        self.server.auto_complete = False
        p = self.path()
        self.enqueue("dm", "operator_dm", "dm-0001")
        p.tick()
        self.settle_accepted("dm-0001")
        self.conns[-1].drop()
        self.settle(p, "dm-0001", cd.AMBIGUOUS)
        self.assertIn("ambiguous:dm-0001", self.page_keys())
        self.assertTrue((self.state / "held" / "dm-0001.json").exists())
        self.server.history.clear()     # the dead server never ran it
        self.clock.t += 2
        p.tick()
        p.tick()
        self.assertIsNotNone(p.client)
        self.assertEqual(len(self.starts()), 1, "ambiguous id was re-sent")
        self.assertEqual(self.state_of("dm-0001"), cd.AMBIGUOUS)

    def settle_accepted(self, did):
        self.assertTrue(wait_for(lambda: self.state_of(did) == cd.ACCEPTED))

    # row 2: thread won't resume
    def test_thread_wont_resume_holds_and_never_starts_a_thread(self):
        self.server.resume_error = (-32600, "no rollout found")
        p = self.path()
        self.enqueue("dm", "operator_dm", "dm-0001")
        for _ in range(3):
            p.tick()
            self.clock.t += 120
        self.assertEqual(p.hold, sd.HOLD_THREAD)
        self.assertEqual(self.server.requests("thread/start"), [])
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.page_keys(), ["hold:thread_wont_resume"])
        self.assertEqual(len(self.server.requests("thread/resume")), 1, "held, not retried")

    def test_missing_thread_record_holds(self):
        (self.state / "thread.json").unlink()
        p = self.path()
        p.tick()
        self.assertEqual(p.hold, sd.HOLD_THREAD)
        self.assertEqual(self.conns, [])

    # row 3: TUI detached
    def test_tui_detach_does_not_touch_delivery(self):
        # The pane is the L2 supervisor's; the writer has no pane dependency at all:
        # delivery runs over the socket whether or not any TUI is attached.
        p = self.path()
        self.enqueue("dm", "operator_dm", "dm-0001")
        p.tick()
        self.settle(p, "dm-0001", cd.DELIVERED)
        src = Path(sd.__file__).read_text()
        self.assertNotIn("tmux", src.split('"""', 2)[2])

    # row 4: auth expired
    def test_auth_expiry_holds_everything_and_pages(self):
        self.server.auto_complete = False
        self.server.turn_error = {"message": "401", "codexErrorInfo": "unauthorized"}
        p = self.path()
        self.enqueue("dm", "operator_dm", "dm-0001")
        p.tick()
        self.settle_accepted("dm-0001")
        self.server.complete(self.server.active, status="failed")
        self.settle(p, "dm-0001", cd.AMBIGUOUS)
        self.enqueue("mail", "mailbox", "mb-0001")
        p.tick()
        self.assertEqual(p.hold, sd.HOLD_AUTH)
        p.tick()
        self.assertIn("hold:auth_expired", self.page_keys())
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.state_of("mb-0001"), cd.QUEUED)
        self.assertTrue((self.state / "pending" / "mb-0001.json").exists())

    def test_live_401_with_other_error_info_is_recognised(self):
        """The exact shape codex 0.147.0 produced in drill 11 (T-5308)."""
        live = {"additionalDetails": None, "codexErrorInfo": "other",
                "message": "unexpected status 401 Unauthorized: Missing bearer or basic "
                           "authentication in header, url: https://api.openai.com/v1/responses"}
        self.assertTrue(sd._is_auth_error(live))
        self.assertFalse(sd._is_auth_error({"codexErrorInfo": "other",
                                            "message": "stream disconnected (status 502)"}))
        self.assertFalse(sd._is_auth_error({"codexErrorInfo": "other",
                                            "message": "context window of 4012 tokens"}))

    def test_auth_expiry_with_the_live_error_shape_holds(self):
        self.server.auto_complete = False
        self.server.turn_error = {"message": "unexpected status 401 Unauthorized: Missing bearer",
                                  "codexErrorInfo": "other"}
        p = self.path()
        self.enqueue("dm", "operator_dm", "dm-0001")
        p.tick()
        self.settle_accepted("dm-0001")
        self.server.complete(self.server.active, status="failed")
        self.settle(p, "dm-0001", cd.AMBIGUOUS)
        self.enqueue("mail", "mailbox", "mb-0001")
        p.tick()
        self.assertEqual(p.hold, sd.HOLD_AUTH)
        self.assertEqual(len(self.starts()), 1, "nothing sent after the auth failure")
        self.assertEqual(self.state_of("mb-0001"), cd.QUEUED)

    def test_auth_http_401_variant_is_recognised(self):
        self.assertTrue(sd._is_auth_error(
            {"message": "x", "codexErrorInfo": {"responseStreamConnectionFailed":
                                                {"httpStatusCode": 401}}}))
        self.assertFalse(sd._is_auth_error({"message": "x", "codexErrorInfo": "serverOverloaded"}))

    # row 5: connection drop mid-turn, server alive
    def test_drop_mid_turn_reconciles_to_delivered_from_thread_history(self):
        self.server.auto_complete = False
        p = self.path()
        self.enqueue("dm", "operator_dm", "dm-0001")
        p.tick()
        self.settle_accepted("dm-0001")
        tid = self.server.active
        self.conns[-1].drop()
        self.settle(p, "dm-0001", cd.AMBIGUOUS)
        self.server.answer_last(tid)        # the server finished it while we were away
        self.clock.t += 2
        p.tick()
        p.tick()
        self.assertEqual(self.state_of("dm-0001"), cd.DELIVERED)
        rec = cd.DeliveryLog(self.state / "deliveries.jsonl").last_state("dm-0001")
        self.assertEqual(rec["route"], "reconciled")
        self.assertFalse((self.state / "held" / "dm-0001.json").exists())

    def test_drop_mid_turn_without_a_reply_stays_ambiguous(self):
        self.server.auto_complete = False
        p = self.path()
        self.enqueue("dm", "operator_dm", "dm-0001")
        p.tick()
        self.settle_accepted("dm-0001")
        tid = self.server.active
        self.conns[-1].drop()
        self.settle(p, "dm-0001", cd.AMBIGUOUS)
        self.server.reply = "plain"                  # finished, but never answered on Telegram
        self.server.complete(tid, text="done, no id")
        self.clock.t += 2
        p.tick()
        p.tick()
        self.assertEqual(self.state_of("dm-0001"), cd.AMBIGUOUS)
        self.assertTrue((self.state / "held" / "dm-0001.json").exists())

    def test_writer_restart_with_accepted_id_goes_ambiguous(self):
        log = cd.DeliveryLog(self.state / "deliveries.jsonl")
        log.append({"delivery_id": "dm-0009", "msg_class": "operator_dm", "state": cd.ACCEPTED,
                    "turn_id": "t9"})
        (self.state / "pending").mkdir()
        (self.state / "pending" / "dm-0009.json").write_text(json.dumps(
            {"delivery_id": "dm-0009", "msg_class": "operator_dm", "text": "x", "claimed": True}))
        p = self.path()
        p.startup_reconcile()
        self.assertEqual(self.state_of("dm-0009"), cd.AMBIGUOUS)
        self.assertTrue((self.state / "held" / "dm-0009.json").exists())
        self.assertIn("ambiguous:dm-0009", self.page_keys())


# ------------------------------------------------------------------ heartbeat
class HeartbeatTests(Base):
    def test_three_misses_page_restart_and_ambiguate_in_flight(self):
        self.server.auto_complete = False
        p = self.path(heartbeat_timeout=0.05)
        self.enqueue("dm", "operator_dm", "dm-0001")
        p.tick()
        self.assertTrue(wait_for(lambda: self.state_of("dm-0001") == cd.ACCEPTED))
        self.server.hang_heartbeat = True
        for _ in range(3):
            self.clock.t += sd.HEARTBEAT_EVERY_S
            p.tick()
        self.assertEqual(self.restarts, 1)
        self.assertIn("server_hung", self.page_keys())
        self.settle(p, "dm-0001", cd.AMBIGUOUS)

    def test_two_misses_do_nothing(self):
        p = self.path(heartbeat_timeout=0.05)
        p.tick()
        self.server.hang_heartbeat = True
        for _ in range(2):
            self.clock.t += sd.HEARTBEAT_EVERY_S
            p.tick()
        self.server.hang_heartbeat = False
        self.clock.t += sd.HEARTBEAT_EVERY_S
        p.tick()
        self.assertEqual(self.restarts, 0)
        self.assertEqual(p._hb_misses, 0)

    def test_thread_not_loaded_is_the_wont_resume_row(self):
        p = self.path()
        p.tick()
        self.server.loaded = []
        self.clock.t += sd.HEARTBEAT_EVERY_S
        p.tick()
        self.assertEqual(p.hold, sd.HOLD_THREAD)


# ------------------------------------------------------------------ compaction
class CompactionTests(Base):
    def _busy_dm(self):
        self.server.auto_complete = False
        self.server.active = "human-1"
        self.server.history["human-1"] = {"id": "human-1", "status": "inProgress", "items": []}
        p = self.path()
        self.enqueue("please read X", "operator_dm", "dm-0001")
        p.tick()
        self.assertTrue(wait_for(lambda: self.state_of("dm-0001") == cd.ACCEPTED))
        return p

    def resteers(self):
        return [t for t in self.server.user_texts() if t.find(sd.COMPACTION_PREFIX) >= 0]

    def test_unacked_dm_resteered_once_per_compaction(self):
        p = self._busy_dm()
        self.server.notify("thread/compacted", {"threadId": THREAD, "turnId": "human-1"})
        self.assertTrue(wait_for(lambda: p._compactions))
        p.tick()
        p.tick()
        self.assertEqual(len(self.resteers()), 1)
        self.assertIn("[delivery-id: dm-0001]", self.resteers()[0])
        self.assertIn("please read X", self.resteers()[0])
        self.server.notify("item/completed", {"threadId": THREAD, "turnId": "human-2",
                                              "item": {"type": "contextCompaction", "id": "c2"}})
        self.assertTrue(wait_for(lambda: p._compactions))
        p.tick()
        self.assertEqual(len(self.resteers()), 2)
        self.server.answer_last("human-1")
        self.settle(p, "dm-0001", cd.DELIVERED)

    def test_notice_and_item_for_one_compaction_resteer_once(self):
        p = self._busy_dm()
        for _ in range(2):      # two compactions, each reported both ways
            self.server.notify("thread/compacted", {"threadId": THREAD, "turnId": "human-1"})
            self.server.notify("item/completed", {"threadId": THREAD, "turnId": "human-1",
                                                  "item": {"type": "contextCompaction",
                                                           "id": "c"}})
            self.assertTrue(wait_for(lambda: len(p._compactions) == 2))
            p.tick()
        self.assertEqual(len(self.resteers()), 2)

    def test_acked_dm_is_never_resent(self):
        p = self._busy_dm()
        self.server.answer_last("human-1")
        self.settle(p, "dm-0001", cd.DELIVERED)
        self.server.notify("thread/compacted", {"threadId": THREAD, "turnId": "x"})
        self.assertTrue(wait_for(lambda: p._compactions))
        p.tick()
        self.assertEqual(self.resteers(), [])


# ------------------------------------------------------------------ replay + CLI
class ReplayAndCliTests(Base):
    def test_recovery_replay_resends_held_id_under_same_id(self):
        self.server.auto_complete = False
        p = self.path()
        self.enqueue("dm", "operator_dm", "dm-0001")
        p.tick()
        self.assertTrue(wait_for(lambda: self.state_of("dm-0001") == cd.ACCEPTED))
        self.conns[-1].drop()
        self.settle(p, "dm-0001", cd.AMBIGUOUS)
        self.server.history.clear()     # a restarted server: no turn, nothing running
        self.server.active = None
        self.server.auto_complete = True
        self.clock.t += 2
        p.tick()
        sd.replay_held(self.state, "dm-0001")
        p.tick()
        self.settle(p, "dm-0001", cd.DELIVERED)
        self.assertEqual(len(self.starts()), 2)

    def test_enqueue_cli_writes_spool(self):
        rc = sd._cli(["enqueue", "--state-dir", str(self.state), "--seat", "consultant",
                      "--class", "mailbox", "--text", "hi"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(list((self.state / "spool").glob("*.json"))), 1)

    def test_bad_spool_file_is_set_aside(self):
        (self.state / "spool").mkdir(exist_ok=True)
        (self.state / "spool" / "0001-junk.json").write_text("{not json")
        p = self.path()
        p.tick()
        self.assertTrue((self.state / "held" / "bad-0001-junk.json").exists())


class FromBindingTests(Base):
    """T-5308: the writer takes its policy from harness/codex/appserver_seats.json."""

    def args(self, *extra):
        import argparse
        ns = argparse.Namespace(seat="consultant", from_binding=True, sandbox=None,
                                approval_policy=None, model=None, cwd=None)
        for k, v in extra:
            setattr(ns, k, v)
        return ns

    def binding(self):
        repo = Path(sd.__file__).resolve().parent.parent
        return json.loads((repo / "harness/codex/appserver_seats.json").read_text())["seats"]["consultant"]

    def test_policy_is_the_binding(self):
        b = self.binding()
        pol = sd.policy_from_args(self.args())
        self.assertEqual((pol.sandbox, pol.approval_policy, pol.model, pol.cwd),
                         (b["sandbox"], b["approval_policy"], b["model"], b["cwd"]))

    def test_a_contradicting_flag_is_refused(self):
        with self.assertRaises(ValueError):
            sd.policy_from_args(self.args(("model", "some-other-model")))

    def test_an_agreeing_flag_is_fine(self):
        sd.policy_from_args(self.args(("model", self.binding()["model"])))

    def test_flags_required_without_the_binding(self):
        with self.assertRaises(ValueError):
            sd.policy_from_args(self.args(("from_binding", False)))

    def test_unknown_seat_is_refused_by_the_cli(self):
        rc = sd._cli(["run", "--state-dir", str(self.state), "--sock", "/nonexistent",
                      "--seat", "no-such-seat", "--from-binding"])
        self.assertEqual(rc, sd.EXIT_USAGE)
        lock = sd.WriterLock(self.state / "writer.lock")
        self.assertTrue(lock.acquire(), "the refused run must release the seat lock")
        lock.release()

    def test_unit_template_runs_the_bound_seat(self):
        repo = Path(sd.__file__).resolve().parent.parent
        unit = (repo / "harness/codex/templates/codex-seat-delivery.service").read_text()
        execs = [ln for ln in unit.splitlines() if ln.startswith("ExecStart=")]
        self.assertEqual(len(execs), 1)
        self.assertIn("tools/codex_seat_delivery.py run", execs[0])
        self.assertIn("--from-binding", execs[0])
        self.assertIn("--seat consultant", execs[0])
        self.assertIn("RestartPreventExitStatus=5", unit)
        self.assertEqual(sd.EXIT_DUPLICATE, 5)


if __name__ == "__main__":
    unittest.main()
