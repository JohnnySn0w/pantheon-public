"""ORG-103 L3 (T-5304): tests for tools/codex_seat_delivery.py against a fake app-server.

One test class per concern; every row of the build note's recovery table has a test
(RecoveryTableTests). The fake is test_codex_delivery's FakeAppServer plus the methods
the writer adds (thread/loaded/list, thread/read, account/read) and a turn history that
survives reconnects, so a dropped connection can be reconciled from the server's record.

Run: python3 -m unittest tools.test_codex_seat_delivery
"""
from __future__ import annotations

import json
import itertools
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

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
        self.loaded_response = None
        self.account = {"type": "chatgpt", "email": "consultant@example.test", "planType": "pro"}
        self.hang_heartbeat = False
        self.resume_error = None
        self.resume_frame = None        # a recorded frame, sent verbatim (T-6130)
        self.turn_error = None

    def h_thread_resume(self, msg):
        if self.resume_error:
            self.error(msg["id"], *self.resume_error)
            return
        if self.resume_frame is not None:
            self.out(dict(self.resume_frame, id=msg["id"]))
            return
        self.resume_overrides.setdefault("cwd", CWD)
        super().h_thread_resume(msg)

    def h_thread_loaded_list(self, msg):
        if not self.hang_heartbeat:
            response = self.loaded_response
            if response is None:
                response = {"data": list(self.loaded), "nextCursor": None}
            self.respond(msg["id"], response)

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
    def test_readiness_publication_requires_exact_loaded_thread_heartbeat(self):
        receipts = []
        p = self.path(readiness_publisher=receipts.append)
        self.server.loaded_response = {"data": THREAD}
        p.tick()
        self.assertEqual(receipts, [], "connected transport is not readiness proof")
        p.close()
        p = self.path(readiness_publisher=receipts.append)
        self.server.loaded_response = {"data": [THREAD], "nextCursor": None}
        p.tick()
        self.assertEqual(receipts, [THREAD])
        self.clock.t += sd.HEARTBEAT_EVERY_S
        p.tick()
        self.assertEqual(receipts, [THREAD], "receipt must publish once")

    def test_atomic_readiness_receipt_has_exact_schema_and_owner_mode(self):
        receipt = self.state / "input-ready.json"
        sd.publish_input_ready(receipt, persona="consultant", thread_id=THREAD, launch_nonce="n-1")
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(receipt.read_text()), {
            "schema": sd.INPUT_READY_SCHEMA, "persona": "consultant",
            "thread_id": THREAD, "launch_nonce": "n-1",
        })

    def test_atomic_receipt_temp_symlink_substitution_fails_without_target_damage(self):
        receipt = self.state / "input-ready.json"
        victim = self.state / "victim"
        victim.write_text("do-not-touch")
        fixed = "ab" * 16
        temp = receipt.with_name(f".{receipt.name}.tmp.{fixed}")
        temp.symlink_to(victim)
        with mock.patch.object(sd.secrets, "token_hex", return_value=fixed):
            with self.assertRaises(FileExistsError):
                sd.publish_input_ready(
                    receipt, persona="consultant", thread_id=THREAD, launch_nonce="nonce",
                )
        self.assertEqual(victim.read_text(), "do-not-touch")
        self.assertTrue(temp.is_symlink())
        self.assertFalse(receipt.exists())

    def test_three_misses_persist_one_replacement_and_ambiguate_in_flight(self):
        self.server.auto_complete = False
        p = self.path(heartbeat_timeout=0.05)
        self.enqueue("dm", "operator_dm", "dm-0001")
        p.tick()
        self.assertTrue(wait_for(lambda: self.state_of("dm-0001") in (cd.ACCEPTED, cd.DELIVERED)), self.state_of("dm-0001"))
        self.server.hang_heartbeat = True
        for _ in range(3):
            self.clock.t += sd.HEARTBEAT_EVERY_S
            p.tick()
        self.assertEqual(self.restarts, 1)
        self.assertNotIn("server_hung", self.page_keys())
        state = json.loads((self.state / "hung-recovery.json").read_text())
        self.assertEqual((state["attempts"], state["escalated"]), (1, False))
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

    def test_unhealthy_successor_holds_without_a_second_replacement_and_pages_once(self):
        first = self.path(heartbeat_timeout=0.05)
        first.tick()
        self.server.hang_heartbeat = True
        for _ in range(3):
            self.clock.t += sd.HEARTBEAT_EVERY_S
            first.tick()
        self.assertEqual(self.restarts, 1)
        first.close()

        successor = self.path(heartbeat_timeout=0.05)
        for _ in range(6):
            self.clock.t += sd.HEARTBEAT_EVERY_S
            successor.tick()
        self.assertEqual(self.restarts, 1, "the persistent bound must block generation two")
        self.assertEqual(successor.hold, sd.HOLD_HUNG_RECOVERY)
        self.assertEqual(self.page_keys().count("server_hung"), 1)
        state = json.loads((self.state / "hung-recovery.json").read_text())
        self.assertEqual((state["attempts"], state["escalated"]), (1, True))

    def test_malformed_state_normalizes_and_later_generations_inherit_hold_without_repage(self):
        (self.state / "hung-recovery.json").write_text(json.dumps({
            "version": 1, "episode_id": None, "attempts": 0, "escalated": True,
        }))
        first = self.path()
        self.assertEqual(first.hold, sd.HOLD_HUNG_RECOVERY)
        self.assertEqual(self.restarts, 0)
        self.assertEqual(self.page_keys(), ["server_hung"])
        durable = json.loads((self.state / "hung-recovery.json").read_text())
        self.assertEqual((durable["attempts"], durable["escalated"]), (1, True))
        self.assertTrue(durable["episode_id"])
        first.close()

        successor = self.path()
        self.assertEqual(successor.hold, sd.HOLD_HUNG_RECOVERY)
        self.assertEqual(self.restarts, 0)
        self.assertEqual(self.page_keys(), ["server_hung"])

    def test_state_write_failure_holds_without_replacement_or_exception(self):
        p = self.path(heartbeat_timeout=0.05)
        p.tick()
        self.server.hang_heartbeat = True
        failure = OSError(28, "No space left on device")
        with mock.patch.object(p.log, "append", side_effect=failure), \
                mock.patch.object(sd, "_write_json_atomic", side_effect=failure):
            for _ in range(3):
                self.clock.t += sd.HEARTBEAT_EVERY_S
                p.tick()
            self.assertEqual(p.hold, sd.HOLD_HUNG_RECOVERY)
            self.assertEqual(self.restarts, 0)
            self.assertEqual(self.page_keys(), ["server_hung"])
            self.assertFalse((self.state / "hung-recovery.json").exists())
            self.assertTrue(
                (self.state.parent / ".codex-appserver-hung-recovery-escalated").exists()
            )
            for _ in range(3):
                self.clock.t += sd.HEARTBEAT_EVERY_S
                p.tick()
            self.assertEqual(self.restarts, 0)
            self.assertEqual(self.page_keys(), ["server_hung"])

    def test_unproven_witness_ownership_holds_without_replacement_or_page(self):
        p = self.path(heartbeat_timeout=0.05)
        p.tick()
        self.server.hang_heartbeat = True
        failure = OSError(28, "No space left on device")
        with mock.patch.object(p.log, "append", side_effect=failure), \
                mock.patch.object(sd, "_write_json_atomic", side_effect=failure), \
                mock.patch.object(sd, "_claim_escalation_witness", side_effect=failure):
            for _ in range(3):
                self.clock.t += sd.HEARTBEAT_EVERY_S
                p.tick()
            self.assertEqual(p.hold, sd.HOLD_HUNG_RECOVERY)
            self.assertEqual(self.restarts, 0)
            self.assertEqual(self.page_keys(), [])
            for _ in range(3):
                self.clock.t += sd.HEARTBEAT_EVERY_S
                p.tick()
            self.assertEqual(self.restarts, 0)
            self.assertEqual(self.page_keys(), [])
            self.assertFalse((self.state / "hung-recovery.json").exists())
            self.assertFalse(
                (self.state.parent / ".codex-appserver-hung-recovery-escalated").exists()
            )

    def test_lost_witness_claim_race_holds_without_replacement_or_page(self):
        (self.state / "hung-recovery.json").write_text(json.dumps({
            "version": 1, "episode_id": "episode-1", "attempts": 1,
            "escalated": False,
        }))
        p = self.path(heartbeat_timeout=0.05)
        p.tick()
        self.server.hang_heartbeat = True
        with mock.patch.object(sd, "_claim_escalation_witness", return_value=False):
            for _ in range(3):
                self.clock.t += sd.HEARTBEAT_EVERY_S
                p.tick()
        self.assertEqual(p.hold, sd.HOLD_HUNG_RECOVERY)
        self.assertEqual(self.restarts, 0)
        self.assertEqual(self.page_keys(), [])

    def _assert_partial_witness_failure_holds_silently(self, phase):
        p = self.path(heartbeat_timeout=0.05)
        p.tick()
        self.server.hang_heartbeat = True
        failure = OSError(28, f"synthetic {phase} failure")
        if phase == "write":
            witness_patch = mock.patch.object(sd.json, "dump", side_effect=failure)
        else:
            real_fsync = sd.os.fsync
            calls = 0

            def fail_at_phase(fd):
                nonlocal calls
                calls += 1
                if calls == (1 if phase == "file_fsync" else 2):
                    raise failure
                return real_fsync(fd)

            witness_patch = mock.patch.object(sd.os, "fsync", side_effect=fail_at_phase)
        with mock.patch.object(p.log, "append", side_effect=failure), \
                mock.patch.object(sd, "_write_json_atomic", side_effect=failure), \
                witness_patch:
            for _ in range(3):
                self.clock.t += sd.HEARTBEAT_EVERY_S
                p.tick()
        witness = self.state.parent / ".codex-appserver-hung-recovery-escalated"
        self.assertEqual(p.hold, sd.HOLD_HUNG_RECOVERY)
        self.assertEqual(self.restarts, 0)
        self.assertEqual(self.page_keys(), [])
        self.assertTrue(witness.exists(), "partial exclusive witness must be retained")

    def test_witness_write_failure_after_exclusive_create_holds_silently(self):
        self._assert_partial_witness_failure_holds_silently("write")

    def test_witness_file_fsync_failure_holds_silently(self):
        self._assert_partial_witness_failure_holds_silently("file_fsync")

    def test_witness_parent_fsync_failure_holds_silently(self):
        self._assert_partial_witness_failure_holds_silently("parent_fsync")

    def test_exact_healthy_heartbeat_clears_failed_state_escalation_witness(self):
        failed = self.path(heartbeat_timeout=0.05)
        failed.tick()
        self.server.hang_heartbeat = True
        failure = OSError(28, "No space left on device")
        with mock.patch.object(failed.log, "append", side_effect=failure), \
                mock.patch.object(sd, "_write_json_atomic", side_effect=failure):
            for _ in range(3):
                self.clock.t += sd.HEARTBEAT_EVERY_S
                failed.tick()
        witness = self.state.parent / ".codex-appserver-hung-recovery-escalated"
        self.assertTrue(witness.exists())
        self.assertEqual(self.page_keys(), ["server_hung"])
        failed.close()

        self.server.hang_heartbeat = False
        recovered = self.path()
        recovered.tick()
        self.assertIsNone(recovered.hold)
        self.assertFalse(witness.exists())
        durable = json.loads((self.state / "hung-recovery.json").read_text())
        self.assertEqual(
            (durable["episode_id"], durable["attempts"], durable["escalated"]),
            (None, 0, False),
        )
        self.assertEqual(self.page_keys(), ["server_hung"])

    def test_escalated_partial_witness_probes_until_exact_healthy_list_clears(self):
        witness = self.state.parent / ".codex-appserver-hung-recovery-escalated"
        (self.state / "hung-recovery.json").write_text(json.dumps({
            "version": 1, "episode_id": "episode-1", "attempts": 1,
            "escalated": True,
        }))
        witness.write_bytes(b'{"version":')
        self.enqueue("held until recovery proof", "operator_dm", "dm-0099")
        p = self.path()
        self.assertEqual(p.hold, sd.HOLD_HUNG_RECOVERY)

        malformed = (
            {"data": None},
            {"data": THREAD},
            {"data": [THREAD, 1]},
            {"data": {THREAD: True}},
        )
        for response in malformed:
            with self.subTest(response=response):
                self.server.loaded_response = response
                p.tick()
                durable = json.loads((self.state / "hung-recovery.json").read_text())
                self.assertEqual(
                    (durable["episode_id"], durable["attempts"], durable["escalated"]),
                    ("episode-1", 1, True),
                )
                self.assertTrue(witness.exists())
                self.assertEqual(p.hold, sd.HOLD_HUNG_RECOVERY)
                self.assertEqual(self.restarts, 0)
                self.assertEqual(self.page_keys(), [])
                self.assertEqual(self.starts(), [], "hold must permit heartbeat traffic only")
                self.clock.t += sd.HEARTBEAT_EVERY_S

        self.server.loaded_response = {"data": [THREAD]}
        p.tick()
        durable = json.loads((self.state / "hung-recovery.json").read_text())
        self.assertEqual(
            (durable["episode_id"], durable["attempts"], durable["escalated"]),
            (None, 0, False),
        )
        self.assertFalse(witness.exists())
        self.assertIsNone(p.hold)
        self.assertEqual(self.restarts, 0)
        self.assertEqual(self.page_keys(), [])
        self.assertEqual(self.starts(), [], "the recovery probe tick must not dispatch")
        p.tick()
        self.assertTrue(wait_for(lambda: len(self.starts()) == 1))

    def test_healthy_reset_write_failure_preserves_bound_and_holds(self):
        attempted = {
            "version": 1, "episode_id": "episode-1", "attempts": 1,
            "escalated": False,
        }
        (self.state / "hung-recovery.json").write_text(json.dumps(attempted))
        p = self.path()
        p.tick()
        self.clock.t += sd.HEARTBEAT_EVERY_S
        with mock.patch.object(
            sd, "_write_json_atomic", side_effect=OSError(13, "Permission denied")
        ):
            p.tick()
        self.assertEqual(p.hold, sd.HOLD_HUNG_RECOVERY)
        self.assertEqual(self.restarts, 0)
        self.assertEqual(self.page_keys(), ["server_hung"])
        durable = json.loads((self.state / "hung-recovery.json").read_text())
        self.assertEqual(
            (durable["episode_id"], durable["attempts"], durable["escalated"]),
            ("episode-1", 1, False),
        )

    def test_only_exact_string_list_containing_thread_clears_attempt(self):
        witness = self.state.parent / ".codex-appserver-hung-recovery-escalated"
        (self.state / "hung-recovery.json").write_text(json.dumps({
            "version": 1, "episode_id": "episode-1", "attempts": 1, "escalated": False,
        }))
        malformed = [
            {"data": THREAD},
            {"data": {THREAD: True}},
            {"data": None},
            {"data": [THREAD, 1]},
            {"data": [THREAD], "partial": True},
            {},
        ]
        for index, response in enumerate(malformed):
            with self.subTest(response=response):
                self.server.loaded_response = response
                p = self.path()
                p.tick()
                self.clock.t += sd.HEARTBEAT_EVERY_S
                p.tick()
                state = json.loads((self.state / "hung-recovery.json").read_text())
                self.assertEqual(state["attempts"], 1, f"case {index} cleared the bound")
                self.assertIsNotNone(state["episode_id"])
                p.close()
                (self.state / "hung-recovery.json").write_text(json.dumps({
                    "version": 1, "episode_id": "episode-1", "attempts": 1,
                    "escalated": False,
                }))
                sd._clear_escalation_witness(witness)

        self.server.loaded_response = {"data": [THREAD]}
        healthy = self.path()
        healthy.tick()
        self.clock.t += sd.HEARTBEAT_EVERY_S
        healthy.tick()
        state = json.loads((self.state / "hung-recovery.json").read_text())
        self.assertEqual(
            (state["episode_id"], state["attempts"], state["escalated"]),
            (None, 0, False),
        )


class AtomicStateWriteTests(unittest.TestCase):
    def test_atomic_json_write_fsyncs_file_and_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            with mock.patch.object(sd.os, "fsync", wraps=sd.os.fsync) as fsync:
                sd._write_json_atomic(path, {"state": "attempted"})
            self.assertEqual(json.loads(path.read_text()), {"state": "attempted"})
            self.assertGreaterEqual(fsync.call_count, 2)

    def test_escalation_witness_is_exclusive_durable_and_clearable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime/hung-recovery-escalated"
            with mock.patch.object(sd.os, "fsync", wraps=sd.os.fsync) as fsync:
                self.assertTrue(sd._claim_escalation_witness(path, "consultant"))
                self.assertFalse(sd._claim_escalation_witness(path, "consultant"))
                self.assertGreaterEqual(fsync.call_count, 2)
                self.assertEqual(json.loads(path.read_text())["seat"], "consultant")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                sd._clear_escalation_witness(path)
            self.assertFalse(path.exists())

    def test_escalation_witness_flushes_userspace_buffer_before_file_fsync(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime/hung-recovery-escalated"
            events = []
            real_fdopen = sd.os.fdopen
            real_fsync = sd.os.fsync

            class TrackedFile:
                def __init__(self, handle):
                    self.handle = handle

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return self.handle.__exit__(*args)

                def __getattr__(self, name):
                    return getattr(self.handle, name)

                def write(self, value):
                    return self.handle.write(value)

                def flush(self):
                    events.append("flush")
                    return self.handle.flush()

            def tracked_fdopen(fd, *args, **kwargs):
                return TrackedFile(real_fdopen(fd, *args, **kwargs))

            def tracked_fsync(fd):
                events.append("fsync")
                return real_fsync(fd)

            with mock.patch.object(sd.os, "fdopen", side_effect=tracked_fdopen), \
                    mock.patch.object(sd.os, "fsync", side_effect=tracked_fsync):
                self.assertTrue(sd._claim_escalation_witness(path, "consultant"))
            self.assertEqual(events[:2], ["flush", "fsync"])

    def test_witness_failures_after_exclusive_create_retain_partial_and_never_own(self):
        failure = OSError(28, "synthetic durability failure")
        for phase in ("write", "file_fsync", "parent_fsync"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "runtime/hung-recovery-escalated"
                if phase == "write":
                    patcher = mock.patch.object(sd.json, "dump", side_effect=failure)
                else:
                    real_fsync = sd.os.fsync
                    calls = 0

                    def fail_at_phase(fd):
                        nonlocal calls
                        calls += 1
                        if calls == (1 if phase == "file_fsync" else 2):
                            raise failure
                        return real_fsync(fd)

                    patcher = mock.patch.object(sd.os, "fsync", side_effect=fail_at_phase)
                with patcher:
                    with self.assertRaises(OSError):
                        sd._claim_escalation_witness(path, "consultant")
                self.assertTrue(path.exists(), "failed witness must remain conservatively")
                self.assertFalse(
                    sd._claim_escalation_witness(path, "consultant"),
                    "a later generation must observe the retained exclusive witness",
                )


class HungRecoveryStateTableTests(unittest.TestCase):
    def test_exact_union_and_bool_int_traps(self):
        episodes = (None, "episode-1", "", 0)
        attempts = (0, 1, 2, False, True)
        escalated = (False, True, 0, 1, None)
        def is_valid(episode, count, page):
            clear = episode is None and type(count) is int and count == 0 and page is False
            active = type(episode) is str and bool(episode) and type(count) is int \
                and count == 1 and type(page) is bool
            return clear or active
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hung-recovery.json"
            for episode, count, page in itertools.product(episodes, attempts, escalated):
                path.write_text(json.dumps({
                    "version": 1,
                    "episode_id": episode,
                    "attempts": count,
                    "escalated": page,
                }))
                state, malformed = sd.load_hung_recovery_state(path)
                expected = is_valid(episode, count, page)
                with self.subTest(episode=episode, attempts=count, escalated=page):
                    self.assertEqual(not malformed, expected)
                    if expected:
                        self.assertEqual(
                            (state["episode_id"], state["attempts"], state["escalated"]),
                            (episode, count, page),
                        )
                    else:
                        self.assertEqual((state["attempts"], state["escalated"]), (1, False))

    def test_missing_corrupt_and_wrong_version_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hung-recovery.json"
            state, malformed = sd.load_hung_recovery_state(path)
            self.assertFalse(malformed)
            self.assertEqual(
                (state["episode_id"], state["attempts"], state["escalated"]),
                (None, 0, False),
            )
            for raw in ("{", "[]", '{"version": 2}'):
                path.write_text(raw)
                state, malformed = sd.load_hung_recovery_state(path)
                self.assertTrue(malformed)
                self.assertEqual((state["attempts"], state["escalated"]), (1, False))


# ------------------------------------------------------------------ compaction
class CompactionTests(Base):
    def _busy_dm(self):
        self.server.auto_complete = False
        self.server.active = "human-1"
        self.server.history["human-1"] = {"id": "human-1", "status": "inProgress", "items": []}
        p = self.path()
        self.enqueue("please read X", "operator_dm", "dm-0001")
        p.tick()
        self.assertTrue(wait_for(lambda: self.state_of("dm-0001") in (cd.ACCEPTED, cd.DELIVERED)), self.state_of("dm-0001"))
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
        self.assertTrue(wait_for(lambda: self.state_of("dm-0001") in (cd.ACCEPTED, cd.DELIVERED)), self.state_of("dm-0001"))
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


_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "codex_appserver" / "resume_no_rollout.json"


def recorded_no_rollout_frame():
    """The thread/resume reply a REAL codex app-server 0.147.0 sent for a thread that
    never ran a turn (recorded, see the fixture's provenance), with the test's thread
    id swapped in for the recorded one."""
    doc = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    frame = json.loads(json.dumps(doc["frame"]).replace(doc["recorded_thread_id"], THREAD))
    return frame


class UnmaterializedThreadTests(Base):
    """T-6130: a FRESH thread has no rollout until its first turn, so thread/resume
    answers -32600 'no rollout found'. That is a wait, not a hold."""

    def record(self, **fields):
        (self.state / "thread.json").write_text(json.dumps(dict({"thread_id": THREAD}, **fields)))

    def test_recorded_frame_parses_to_the_error_the_writer_matches_on(self):
        frame = recorded_no_rollout_frame()
        self.assertEqual(frame["error"]["code"], -32600)
        self.assertEqual(frame["error"]["message"], f"no rollout found for thread id {THREAD}")
        self.server.resume_frame = frame
        self.record(materialized=False)
        p = self.path()
        p.tick()
        self.assertIsNone(p.hold)

    def test_unmaterialized_thread_backs_off_instead_of_holding(self):
        self.record(materialized=False)
        self.server.resume_frame = recorded_no_rollout_frame()
        self.enqueue("dm", "operator_dm", "dm-0001")
        p = self.path()
        p.tick()
        self.assertIsNone(p.hold)
        self.assertEqual(self.page_keys(), [])
        self.assertIsNone(p.client)
        self.assertEqual(self.server.requests("thread/start"), [])
        self.assertEqual(self.starts(), [])
        # Backoff: no reconnect before the delay, a retry after it.
        p.tick()
        self.assertEqual(len(self.server.requests("thread/resume")), 1)
        self.clock.t += 2
        p.tick()
        self.assertEqual(len(self.server.requests("thread/resume")), 2)
        # The TUI's first turn materializes the thread; the next retry resumes it
        # and the queued mail flows.
        self.server.resume_frame = None
        self.record(materialized=True)
        self.clock.t += 5
        p.tick()
        self.assertIsNotNone(p.client)
        self.assertIsNone(p.hold)
        p.tick()
        self.assertTrue(wait_for(lambda: self.state_of("dm-0001") in (cd.ACCEPTED, cd.DELIVERED)), self.state_of("dm-0001"))

    def test_persisting_past_the_bound_pages_once_and_never_holds(self):
        self.record(materialized=False)
        self.server.resume_frame = recorded_no_rollout_frame()
        p = self.path()
        p.tick()
        for _ in range(10):
            self.clock.t += sd.UNMATERIALIZED_PAGE_AFTER_S / 5
            p.tick()
        self.assertEqual(self.page_keys(), ["thread_unmaterialized"])
        self.assertIsNone(p.hold)
        self.clock.t += 600
        p.tick()
        self.assertEqual(self.page_keys(), ["thread_unmaterialized"], "paged more than once")

    def test_no_page_before_the_bound(self):
        self.record(materialized=False)
        self.server.resume_frame = recorded_no_rollout_frame()
        p = self.path()
        p.tick()
        self.clock.t += sd.UNMATERIALIZED_PAGE_AFTER_S - 10
        p.tick()
        self.assertEqual(self.page_keys(), [])

    def test_materialized_thread_that_wont_resume_keeps_the_hard_hold(self):
        self.record(materialized=True)
        self.server.resume_frame = recorded_no_rollout_frame()
        p = self.path()
        p.tick()
        self.assertEqual(p.hold, sd.HOLD_THREAD)
        self.assertEqual(self.page_keys(), ["hold:thread_wont_resume"])

    def test_record_without_materialized_key_keeps_the_hard_hold(self):
        self.server.resume_frame = recorded_no_rollout_frame()
        p = self.path()
        p.tick()
        self.assertEqual(p.hold, sd.HOLD_THREAD)

    def test_other_resume_errors_on_an_unmaterialized_record_still_hold(self):
        self.record(materialized=False)
        self.server.resume_error = (-32600, "invalid thread id")
        p = self.path()
        p.tick()
        self.assertEqual(p.hold, sd.HOLD_THREAD)
        self.server.resume_error = (-32603, f"no rollout found for thread id {THREAD}")
        p2 = self.path()
        p2.tick()
        self.assertEqual(p2.hold, sd.HOLD_THREAD)


if __name__ == "__main__":
    unittest.main()
