#!/usr/bin/env python3
"""ORG-103 L3 (T-5304): the single automated writer of input to one codex seat.

tools/codex_delivery.py proves one delivery over one connection. This module wraps it
into the per-seat delivery path in notes/2026-10-14-t4988-codex-appserver-build.md:

  Single writer   `run` takes a non-blocking flock on <state>/writer.lock; a second
                  start exits EXIT_DUPLICATE. Callers never talk to the app-server:
                  `enqueue` drops an atomic JSON file into <state>/spool/ and the writer
                  claims it. Every record the writer appends to deliveries.jsonl carries a
                  monotonic `seq`, seeded from the log, so the log is totally ordered.
  Routing         spool items are claimed in priority order (operator_urgent, operator_dm,
                  controller, then mailbox/poke) and sent one at a time: the next send
                  starts only after the previous one is accepted/queued/refused, while the
                  turn waits run in parallel (an urgent DM can still interrupt).
                  Mail and pokes that land while the seat is busy are `queued`; the writer
                  drains the oldest one per idle transition (same id, so the log's
                  idempotency covers a double drain).
  Identity        before every delivery the recorded thread.json id must equal the
                  connected thread, and at every (re)connect the resume reply must match
                  the binding (thread id, cwd, model, sandbox, approval; account email when
                  bound). A mismatch refuses, holds everything and pages Recovery.
  Approvals       codex_delivery declines every server request; each one also pages Recovery
                  as a policy anomaly (under approval=never the server should never ask).
  Recovery        the build note's state-machine table; see RECOVERY below.
  Heartbeat       thread/loaded/list every 30s, 10s timeout; 3 misses = hung: page Recovery,
                  restart the server, recover as for a dead server. A reply without the
                  seat's thread is the thread-won't-resume row.
  Compaction      thread/compacted (or a contextCompaction item) re-steers every accepted,
                  unacknowledged operator DM once, verbatim, same id, prefixed
                  "[re-delivery after compaction]".

Message text lives in <state>/pending/<id>.json from claim until the message is delivered
(or moved to <state>/held/ when it goes ambiguous or keeps failing), not in the
append-only log: the log keeps only a redacted preview, and pending/ is what "keep the
full text of an operator DM until it is acknowledged" means here. held/ is Recovery's: an
ambiguous id is re-sent only by Recovery, after reading the thread (`replay --id`).

RECOVERY (each transition is audit-logged)
  app-server dies      reader EOF / connect refused -> in-flight accepted ids go ambiguous
                       (codex_delivery), reconnect with backoff 1,2,4..60s, resume and
                       re-validate; page Recovery after 3 consecutive failed reconnects.
  thread won't resume  thread/resume error or identity mismatch -> hold everything, page
                       Recovery. Never thread/start for an existing seat. Cleared by a restart.
  TUI detached         not this module's: the server owns the turn, so delivery keeps going;
                       the L2 supervisor recreates the pane.
  auth expired         a turn that fails unauthorized/401 -> its id ambiguous, hold
                       everything, page Recovery (the relogin is human). Cleared by a restart.
  drop mid-turn        on every reconnect, held ambiguous ids are reconciled against
                       thread/read: a completed carrying turn (with the Telegram-reply
                       receipt, for operator classes; T-5413) proves delivered; anything
                       else stays ambiguous.

Not wired: L4 (T-5306) points the injectors at `enqueue`; L6 (T-5308) installs `run`.
Stdlib only.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

if __package__ == "tools":          # imported as tools.codex_seat_delivery (tests)
    from tools import codex_delivery as cd
else:                               # run as a script, or imported with tools/ on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import codex_delivery as cd  # noqa: E402

EXIT_OK, EXIT_DUPLICATE, EXIT_USAGE = 0, 5, 3
PRIORITY = {"operator_urgent": 0, "operator_dm": 1, "controller": 2, "mailbox": 3, "poke": 3}
DRAINABLE = ("mailbox", "poke")
HEARTBEAT_EVERY_S = 30.0
HEARTBEAT_TIMEOUT_S = 10.0
HEARTBEAT_MISSES = 3
BACKOFF_MAX_S = 60.0
RECONNECT_PAGE_AFTER = 3
FAILED_RETRIES = 3
SEND_PHASE_WAIT_S = 120.0
COMPACTION_PREFIX = "[re-delivery after compaction]"
TERMINAL = (cd.DELIVERED, cd.FAILED, cd.AMBIGUOUS)

HOLD_THREAD = "thread_wont_resume"
HOLD_AUTH = "auth_expired"
HOLD_IDENTITY = "identity_mismatch"


def _write_json_atomic(path: Path, doc: dict) -> None:
    tmp = path.with_name("." + path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _dirs(state: Path) -> dict:
    out = {k: state / k for k in ("spool", "pending", "held")}
    for p in [state, *out.values()]:
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return out


# ------------------------------------------------------------------ caller side
def enqueue(state_dir, text: str, msg_class: str, *, seat: str = "seat",
            delivery_id: Optional[str] = None) -> str:
    """Hand one message to the seat's writer. Returns its delivery id. Never talks to
    the app-server, so it is safe from any process running as the seat user."""
    if msg_class not in cd.MSG_CLASSES:
        raise ValueError(f"unknown msg_class {msg_class!r}")
    ns = time.time_ns()
    did = delivery_id or f"{seat}-{ns}-{os.getpid()}"
    if not cd.DELIVERY_ID_RE.match(did):
        raise ValueError("delivery_id must match %s" % cd.DELIVERY_ID_RE.pattern)
    d = _dirs(Path(state_dir))
    _write_json_atomic(d["spool"] / f"{ns:020d}-{did}.json",
                       {"delivery_id": did, "msg_class": msg_class, "text": text,
                        "enqueued_ns": ns})
    return did


def wait_state(log_path, delivery_id: str, timeout: float, *, accept_queued: bool = False,
               poll: float = 0.25) -> Optional[dict]:
    """Block until `delivery_id` reaches a terminal state (or `queued`, if accepted)."""
    log = cd.DeliveryLog(log_path)
    want = TERMINAL + ((cd.QUEUED,) if accept_queued else ())
    deadline = time.monotonic() + timeout
    while True:
        rec = log.last_state(delivery_id)
        if rec and rec.get("state") in want:
            return rec
        if time.monotonic() >= deadline:
            return rec
        time.sleep(poll)


# ------------------------------------------------------------------ writer side
class MonotonicLog(cd.DeliveryLog):
    """deliveries.jsonl with a strictly increasing `seq` on every record this writer
    appends. Only the flock-holding writer appends, so a process-local counter seeded
    from the log is enough."""

    def __init__(self, path):
        super().__init__(path)
        self._seq_lock = threading.Lock()
        self._seq = max((r.get("seq", 0) for r in self.records()
                         if isinstance(r.get("seq"), int)), default=0)

    def append(self, record: dict) -> None:
        with self._seq_lock:
            self._seq += 1
            rec = dict(record, seq=self._seq)
            super().append(rec)


@dataclass(frozen=True)
class SeatBinding:
    seat: str
    policy: cd.SeatPolicy
    thread_json: Path
    account_email: Optional[str] = None


class WriterLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: Optional[int] = None

    def acquire(self) -> bool:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


# T-5308 drill 11 (live codex 0.147.0, expired/revoked tokens): the failed turn carries
# codexErrorInfo "other" and the status only in the message ("unexpected status 401
# Unauthorized: Missing bearer or basic authentication in header ..."), so the enum
# alone misses the real auth failure.
_AUTH_MESSAGE_RE = re.compile(r"\b(?:status\s+)?401\b|\bunauthori[sz]ed\b", re.IGNORECASE)


def _is_auth_error(err) -> bool:
    if not isinstance(err, dict):
        return False
    info = err.get("codexErrorInfo")
    if info == "unauthorized":
        return True
    if _AUTH_MESSAGE_RE.search(str(err.get("message") or "")):
        return True
    if isinstance(info, dict):
        for v in info.values():
            if isinstance(v, dict) and v.get("httpStatusCode") == 401:
                return True
    return False


def _turn_carries(turn: dict, did: str) -> bool:
    for item in turn.get("items") or []:
        if isinstance(item, dict) and item.get("type") == "userMessage":
            for part in item.get("content") or []:
                if isinstance(part, dict) and did in cd.DELIVERY_TAG_RE.findall(
                        part.get("text") or ""):
                    return True
    return False


def _turn_answers(turn: dict, did: str, text: str) -> bool:
    return cd.ack_receipt(turn.get("items"), did, cd.operator_chat_id(text))


class SeatDeliveryPath:
    """One seat's writer. Drive it with tick() (the `run` loop calls it every 0.2s)."""

    def __init__(self, binding: SeatBinding, state_dir, connect: Callable[[], object], *,
                 pager: Callable[[str, str, str], None],
                 restart_server: Callable[[], None],
                 clock: Callable[[], float] = time.monotonic,
                 request_timeout: float = 60.0, turn_timeout: float = 1800.0,
                 heartbeat_every: float = HEARTBEAT_EVERY_S,
                 heartbeat_timeout: float = HEARTBEAT_TIMEOUT_S,
                 send_phase_wait: float = SEND_PHASE_WAIT_S):
        self.b = binding
        self.state = Path(state_dir)
        self.d = _dirs(self.state)
        self.log = MonotonicLog(self.state / "deliveries.jsonl")
        self.connect_transport = connect
        self.pager = pager
        self.restart_server = restart_server
        self.clock = clock
        self.request_timeout = request_timeout
        self.turn_timeout = turn_timeout
        self.heartbeat_every = heartbeat_every
        self.heartbeat_timeout = heartbeat_timeout
        self.send_phase_wait = send_phase_wait

        self.client: Optional[cd.CodexDeliveryClient] = None
        self.hold: Optional[str] = None
        self.paged: set[str] = set()
        self._ev_lock = threading.Lock()
        self._idle_edge = False
        self._compactions: list[str] = []
        self._compaction_counts: dict[tuple, int] = {}
        self._auth_failed = False
        self._anomalies: list[dict] = []
        self._reconnect_failures = 0
        self._next_reconnect = 0.0
        self._last_hb = 0.0
        self._hb_misses = 0
        self._workers: dict[str, threading.Thread] = {}
        self._resteered: set[tuple[str, str]] = set()

    # ---------------------------------------------------------- helpers
    def _audit(self, event: str, **fields) -> None:
        rec = {"event": event, "seat": self.b.seat,
               "thread_id": self.client.thread_id if self.client else None}
        rec.update(fields)
        self.log.append(rec)

    def page(self, key: str, subject: str, body: str) -> None:
        """Page Recovery once per key (per process lifetime)."""
        if key in self.paged:
            return
        self.paged.add(key)
        self._audit("paged", key=key, subject=subject)
        try:
            self.pager(key, f"[{self.b.seat}] {subject}", body)
        except Exception as exc:
            self._audit("page_failed", key=key, error=repr(exc)[:200])

    def _set_hold(self, reason: str, detail: str) -> None:
        if self.hold is None:
            self.hold = reason
            self._audit("hold", reason=reason, detail=cd.preview(detail, 300))
        self.page(f"hold:{reason}", f"codex delivery held: {reason}",
                  f"{detail}\nEverything stays queued in {self.d['pending']}; nothing is "
                  f"dropped. Restart the writer once fixed (T-5304).")

    def _load_item(self, path: Path) -> Optional[dict]:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(doc, dict) or doc.get("msg_class") not in cd.MSG_CLASSES \
                or not cd.DELIVERY_ID_RE.match(str(doc.get("delivery_id", ""))):
            return None
        return doc

    def _pending_items(self) -> list[dict]:
        out = []
        for p in self.d["pending"].glob("*.json"):
            doc = self._load_item(p)
            if doc is not None:
                out.append(doc)
        return sorted(out, key=lambda d: d.get("enqueued_ns", 0))

    def _pending_path(self, did: str) -> Path:
        return self.d["pending"] / f"{did}.json"

    def _move_to_held(self, did: str, reason: str) -> None:
        src = self._pending_path(did)
        if src.exists():
            os.replace(src, self.d["held"] / f"{did}.json")
        self._audit("held_for_recovery", delivery_id=did, reason=reason)

    def _last(self, did: str) -> Optional[str]:
        rec = self.log.last_state(did)
        return rec.get("state") if rec else None

    # ---------------------------------------------------------- listener (reader thread)
    def _on_event(self, event: str, p: dict) -> None:
        with self._ev_lock:
            if event == "turn/completed":
                turn = p.get("turn") or {}
                if _is_auth_error(turn.get("error")):
                    self._auth_failed = True
                self._idle_edge = True
            elif event == "error":
                if _is_auth_error(p.get("error")) and not p.get("willRetry"):
                    self._auth_failed = True
            elif event == "thread/compacted":
                self._compactions.append(self._compaction_key("notice", p.get("turnId")))
            elif event == "item/completed":
                item = p.get("item") or {}
                if item.get("type") == "contextCompaction":
                    self._compactions.append(self._compaction_key("item", p.get("turnId")))
            elif event == "server_request":
                self._anomalies.append(dict(p))

    def _compaction_key(self, kind: str, turn_id) -> str:
        """codex 0.147.0 may report one compaction twice: the deprecated thread/compacted
        notice and a contextCompaction item. The Nth of either kind in a turn is the same
        compaction, so both map to "<turn>#N" and the re-steer bound holds. (caller holds
        _ev_lock)"""
        n = self._compaction_counts.get((kind, turn_id), 0) + 1
        self._compaction_counts[(kind, turn_id)] = n
        return f"{turn_id}#{n}"

    # ---------------------------------------------------------- connect / recovery
    def _recorded_thread(self) -> Optional[str]:
        try:
            doc = json.loads(self.b.thread_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        tid = doc.get("thread_id") if isinstance(doc, dict) else None
        return tid if isinstance(tid, str) else None

    def _check_identity(self, res: dict, account: Optional[dict]) -> None:
        pol = self.b.policy
        thread = res.get("thread") or {}
        if thread.get("id") not in (None, self.client.thread_id):
            raise cd.PolicyViolation(f"resume returned thread {thread.get('id')!r}")
        if pol.cwd and res.get("cwd") is not None and res.get("cwd") != pol.cwd:
            raise cd.PolicyViolation(f"server reports cwd {res.get('cwd')!r}, binding {pol.cwd!r}")
        if self.b.account_email is not None:
            email = (account or {}).get("email")
            if email != self.b.account_email:
                raise cd.PolicyViolation(
                    f"server account {email!r}, binding {self.b.account_email!r}")

    def connect(self) -> bool:
        """(Re)connect, resume the recorded thread and validate identity. Returns True
        when deliveries may flow."""
        tid = self._recorded_thread()
        if not tid:
            self._set_hold(HOLD_THREAD, f"no usable thread record at {self.b.thread_json}")
            return False
        try:
            transport = self.connect_transport()
        except (OSError, ConnectionError) as exc:
            return self._connect_failed(f"connect: {exc}")
        client = cd.CodexDeliveryClient(transport, self.b.policy, self.log, seat=self.b.seat,
                                        request_timeout=self.request_timeout)
        client.listeners.append(self._on_event)
        self.client = client
        try:
            client.initialize()
        except (cd.Disconnected, cd.RequestTimeout, cd.RpcError) as exc:
            client.close()
            return self._connect_failed(f"initialize: {exc}")
        try:
            res = client.resume(tid)
            account = None
            if self.b.account_email is not None:
                account = client.request("account/read", {}).get("account")
            self._check_identity(res, account)
        except cd.PolicyViolation as exc:
            self._set_hold(HOLD_IDENTITY, f"identity check failed on {tid}: {exc}")
            return False
        except cd.RpcError as exc:
            self._set_hold(HOLD_THREAD, f"thread/resume {tid} failed: {exc}. Not starting a "
                                        f"new thread over the seat's context; Recovery decides.")
            return False
        except (cd.Disconnected, cd.RequestTimeout) as exc:
            client.close()
            return self._connect_failed(f"resume: {exc}")
        self._reconnect_failures = 0
        self._hb_misses = 0
        self._last_hb = self.clock()
        self._audit("connected", thread_id=tid)
        self.reconcile_held()
        with self._ev_lock:
            self._idle_edge = True      # a fresh connection is an idle transition
        return True

    def _connect_failed(self, why: str) -> bool:
        self._reconnect_failures += 1
        delay = min(BACKOFF_MAX_S, 2 ** (self._reconnect_failures - 1))
        self._next_reconnect = self.clock() + delay
        self._audit("reconnect_failed", attempt=self._reconnect_failures, retry_in_s=delay,
                    error=cd.preview(why, 200))
        if self._reconnect_failures >= RECONNECT_PAGE_AFTER:
            self.page("server_down", "codex app-server unreachable",
                      f"{self._reconnect_failures} consecutive reconnects failed; last: {why}. "
                      f"Retrying with backoff up to {BACKOFF_MAX_S:.0f}s.")
        self.client = None
        return False

    def reconcile_held(self) -> None:
        """Drop-mid-turn row: prove held ambiguous ids from the thread's own history."""
        held = [d for d in (self._load_item(p) for p in self.d["held"].glob("*.json")) if d]
        held = [d for d in held if self._last(d["delivery_id"]) == cd.AMBIGUOUS]
        if not held:
            return
        try:
            thread = self.client.read_thread(include_turns=True)
        except cd.DeliveryError as exc:
            self._audit("reconcile_failed", error=cd.preview(str(exc), 200))
            return
        turns = [t for t in thread.get("turns") or [] if isinstance(t, dict)]
        for doc in held:
            did, cls = doc["delivery_id"], doc["msg_class"]
            carriers = [t for t in turns if _turn_carries(t, did)]
            proven = next((t for t in carriers if t.get("status") == "completed"
                           and (cls not in cd.ACK_CLASSES
                                or _turn_answers(t, did, doc.get("text") or ""))), None)
            if proven is None:
                self._audit("reconcile_unproven", delivery_id=did,
                            carrier_status=[t.get("status") for t in carriers])
                continue
            self.log.append({"delivery_id": did, "msg_class": cls, "seat": self.b.seat,
                             "thread_id": self.client.thread_id, "state": cd.DELIVERED,
                             "route": "reconciled", "turn_id": proven.get("id"),
                             "evidence": {"reconciled_from": "thread/read",
                                          "receipt": ("telegram_reply"
                                                      if cls in cd.ACK_CLASSES else None)}})
            (self.d["held"] / f"{did}.json").unlink(missing_ok=True)

    def startup_reconcile(self) -> None:
        """A previous writer that died mid-flight left ids at `accepted`: their fate is
        unknown, so they go ambiguous (and to held/) before anything else runs."""
        last: dict[str, dict] = {}
        for rec in self.log.records():
            if rec.get("delivery_id") and rec.get("state") in cd.STATES:
                last[rec["delivery_id"]] = rec
        for did, rec in last.items():
            if rec.get("state") == cd.ACCEPTED:
                self.log.append({"delivery_id": did, "msg_class": rec.get("msg_class"),
                                 "seat": self.b.seat, "thread_id": rec.get("thread_id"),
                                 "state": cd.AMBIGUOUS, "route": rec.get("route"),
                                 "turn_id": rec.get("turn_id"),
                                 "evidence": {"reason": "writer restarted with delivery in flight"}})
                self._move_to_held(did, "writer restarted with delivery in flight")
                self.page(f"ambiguous:{did}", "codex delivery ambiguous",
                          f"{did} was in flight when the previous writer died.")

    # ---------------------------------------------------------- tick
    def tick(self) -> None:
        self._handle_events()       # first, so a new hold applies to this tick's claims
        self._claim_spool()
        if self.hold:
            return
        if self.client is None or self.client.closed:
            if self.client is not None:
                self._audit("disconnected")
                self.client = None
                self._next_reconnect = self.clock()     # first retry at once, then back off
            if self.clock() < self._next_reconnect or not self.connect():
                return
        self._heartbeat()
        if self.hold or self.client is None:
            return
        self._resteer_after_compaction()
        self._dispatch_new()
        self._drain_one()

    def _claim_spool(self) -> None:
        for p in sorted(self.d["spool"].glob("*.json")):
            doc = self._load_item(p)
            if doc is None:
                self._audit("spool_rejected", file=p.name)
                os.replace(p, self.d["held"] / ("bad-" + p.name))
                continue
            did = doc["delivery_id"]
            dst = self._pending_path(did)
            if dst.exists() or self._last(did) == cd.DELIVERED or (
                    (self.d["held"] / f"{did}.json").exists() and not doc.get("replay_ambiguous")):
                self._audit("spool_duplicate", delivery_id=did)
                p.unlink(missing_ok=True)
                continue
            doc["claimed"] = False
            _write_json_atomic(dst, doc)
            p.unlink(missing_ok=True)
            if self.hold:
                # Nothing is sent while held; record it so a waiting caller sees queued.
                self.log.append({"delivery_id": did, "msg_class": doc["msg_class"],
                                 "seat": self.b.seat, "state": cd.QUEUED,
                                 "route": f"held:{self.hold}",
                                 "preview": cd.preview(doc["text"])})

    def _handle_events(self) -> None:
        with self._ev_lock:
            auth, self._auth_failed = self._auth_failed, False
            anomalies, self._anomalies = self._anomalies, []
        for a in anomalies:
            self.page(f"approval:{a.get('method')}:{a.get('turn_id')}",
                      "codex approval request under a never-ask binding",
                      f"Declined {a.get('method')} (turn {a.get('turn_id')}, item "
                      f"{a.get('item_id')}). The seat binding may have drifted.")
        if auth:
            self._set_hold(HOLD_AUTH, "a turn failed unauthorized: the seat's codex auth "
                                      "expired. The relogin is human (OAuth).")

    def _heartbeat(self) -> None:
        now = self.clock()
        if now - self._last_hb < self.heartbeat_every:
            return
        self._last_hb = now
        try:
            res = self.client.request("thread/loaded/list", {}, timeout=self.heartbeat_timeout)
        except (cd.RequestTimeout, cd.Disconnected):
            self._hb_misses += 1
            self._audit("heartbeat_miss", misses=self._hb_misses)
            if self._hb_misses >= HEARTBEAT_MISSES:
                self.page("server_hung", "codex app-server hung",
                          f"{self._hb_misses} heartbeats missed; restarting the server.")
                self._audit("server_restart", reason="heartbeat")
                try:
                    self.restart_server()
                except Exception as exc:
                    self._audit("server_restart_failed", error=repr(exc)[:200])
                self.client.close()
                self.client = None
                self.paged.discard("server_hung")
                self._hb_misses = 0
                self._next_reconnect = self.clock()
            return
        except cd.RpcError as exc:
            self._audit("heartbeat_error", error=cd.preview(str(exc), 200))
            return
        self._hb_misses = 0
        if self.client.thread_id not in (res.get("data") or []):
            self._set_hold(HOLD_THREAD, f"heartbeat: thread {self.client.thread_id} is no "
                                        f"longer loaded on the app-server")

    def _resteer_after_compaction(self) -> None:
        with self._ev_lock:
            keys, self._compactions = self._compactions, []
        for key in keys:
            for doc in self._pending_items():
                did = doc["delivery_id"]
                if doc["msg_class"] not in cd.ACK_CLASSES or self._last(did) != cd.ACCEPTED:
                    continue
                if (did, key) in self._resteered:
                    continue
                self._resteered.add((did, key))
                payload = cd.format_input(f"{COMPACTION_PREFIX}\n{doc['text']}", did,
                                          doc["msg_class"])
                try:
                    route, tid = self.client.resend_tagged(payload)
                    self._audit("compaction_resteer", delivery_id=did, compaction=key,
                                route=route, turn_id=tid)
                except cd.DeliveryError as exc:
                    self._audit("compaction_resteer_failed", delivery_id=did, compaction=key,
                                error=cd.preview(str(exc), 200))

    def _dispatch_new(self) -> None:
        fresh = [d for d in self._pending_items() if not d.get("claimed")]
        fresh.sort(key=lambda d: (PRIORITY[d["msg_class"]], d.get("enqueued_ns", 0)))
        for doc in fresh:
            if self.hold or self.client is None:
                return
            doc["claimed"] = True
            _write_json_atomic(self._pending_path(doc["delivery_id"]), doc)
            self._send(doc)

    def _drain_one(self) -> None:
        with self._ev_lock:
            edge = self._idle_edge
        if not edge or self.client.busy:
            return
        for doc in self._pending_items():
            did = doc["delivery_id"]
            if not doc.get("claimed") or did in self._workers:
                continue
            st = self._last(did)
            if st == cd.QUEUED or st == cd.FAILED:
                with self._ev_lock:
                    self._idle_edge = False
                self._audit("drain", delivery_id=did, prior_state=st)
                self._send(doc)
                return
        with self._ev_lock:
            self._idle_edge = False

    def _send(self, doc: dict) -> None:
        """Start one delivery and wait until its send phase is over, so sends stay in
        priority order while the turn waits overlap."""
        did = doc["delivery_id"]
        tid = self._recorded_thread()
        if tid != self.client.thread_id:
            self._set_hold(HOLD_IDENTITY, f"thread.json now names {tid!r}; connected to "
                                          f"{self.client.thread_id!r}")
            return
        sent = threading.Event()
        client = self.client

        def work():
            try:
                res = client.deliver(doc["text"], did, doc["msg_class"],
                                     timeout=self.turn_timeout, on_sent=sent.set,
                                     replay_ambiguous=bool(doc.get("replay_ambiguous")))
                self._settle(doc, res)
            except Exception as exc:
                self._audit("deliver_error", delivery_id=did, error=repr(exc)[:200])
                sent.set()
            finally:
                self._workers.pop(did, None)

        t = threading.Thread(target=work, name=f"deliver-{did}", daemon=True)
        self._workers[did] = t
        t.start()
        if not sent.wait(self.send_phase_wait):
            self._audit("send_phase_slow", delivery_id=did)

    def _settle(self, doc: dict, res: cd.DeliveryResult) -> None:
        did = doc["delivery_id"]
        if res.state == cd.DELIVERED:
            self._pending_path(did).unlink(missing_ok=True)
        elif res.state == cd.AMBIGUOUS:
            if _is_auth_error(res.evidence.get("turn_error")):
                with self._ev_lock:
                    self._auth_failed = True
            self._move_to_held(did, str(res.evidence.get("reason")))
            self.page(f"ambiguous:{did}", "codex delivery ambiguous",
                      f"{did} ({doc['msg_class']}): {res.evidence.get('reason')}. Not "
                      f"replayed; read the thread, then `replay --id {did}` if unacted on.")
        elif res.state == cd.FAILED:
            fails = sum(1 for r in self.log.records()
                        if r.get("delivery_id") == did and r.get("state") == cd.FAILED)
            if fails >= FAILED_RETRIES:
                self._move_to_held(did, f"failed {fails} times")
                self.page(f"failed:{did}", "codex delivery keeps failing",
                          f"{did}: {res.evidence.get('error')}")

    def join_workers(self, timeout: float = 5.0) -> None:
        for t in list(self._workers.values()):
            t.join(timeout)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()


def replay_held(state_dir, delivery_id: str) -> None:
    """Recovery, after reading the thread: move a held id back to pending with the
    replay flag, so the writer re-sends it under the same id."""
    d = _dirs(Path(state_dir))
    src = d["held"] / f"{delivery_id}.json"
    doc = json.loads(src.read_text(encoding="utf-8"))
    doc.pop("claimed", None)
    doc["replay_ambiguous"] = True
    _write_json_atomic(d["spool"] / f"{time.time_ns():020d}-{delivery_id}.json", doc)
    src.unlink()   # after the spool write: a crash in between leaves a copy, never none


# ------------------------------------------------------------------ CLI
def _mailbox_pager(key: str, subject: str, body: str) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import importlib
    mb = importlib.import_module("mailbox")
    if not hasattr(mb, "send"):
        raise RuntimeError("pantheon mailbox library not importable")
    mb.send("/home/post-office/mailbox/recovery", payload=body, sender="codex_seat_delivery",
            subject=subject)


def _systemctl_restart(unit: str) -> Callable[[], None]:
    import subprocess

    def restart():
        subprocess.run(["systemctl", "--user", "restart", unit], check=False, timeout=60)
    return restart


def policy_from_args(a) -> "cd.SeatPolicy":
    """The seat policy from --from-binding (appserver_seats.json) or explicit flags.
    Explicit flags may not contradict the binding: one source of truth."""
    if not a.from_binding:
        if not (a.sandbox and a.approval_policy):
            raise ValueError("--sandbox and --approval-policy are required without --from-binding")
        return cd.SeatPolicy(a.sandbox, a.approval_policy, a.model, a.cwd)
    path = Path(__file__).resolve().parent.parent / "harness" / "codex" / "appserver_seats.json"
    b = json.loads(path.read_text(encoding="utf-8"))["seats"][a.seat]
    for flag, key in (("sandbox", "sandbox"), ("approval_policy", "approval_policy"),
                      ("model", "model"), ("cwd", "cwd")):
        given = getattr(a, flag)
        if given is not None and given != b.get(key):
            raise ValueError(f"--{flag.replace('_', '-')} {given!r} contradicts the binding {b.get(key)!r}")
    return cd.SeatPolicy(b["sandbox"], b["approval_policy"], b.get("model"), b.get("cwd"),
                         bool(b.get("network_access")), tuple(b.get("writable_roots") or ()))


def _cli(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--state-dir", required=True)
    r.add_argument("--sock", required=True)
    r.add_argument("--seat", required=True)
    r.add_argument("--from-binding", action="store_true",
                   help="take sandbox/approval/model/cwd from harness/codex/appserver_seats.json "
                        "for --seat (the seat's declared binding; T-5308)")
    r.add_argument("--sandbox", choices=cd.SANDBOX_MODES)
    r.add_argument("--approval-policy", choices=cd.APPROVAL_POLICIES)
    r.add_argument("--model")
    r.add_argument("--cwd")
    r.add_argument("--account-email")
    r.add_argument("--server-unit", default="codex-daemon.service")
    e = sub.add_parser("enqueue")
    e.add_argument("--state-dir", required=True)
    e.add_argument("--seat", default="seat")
    e.add_argument("--class", required=True, dest="msg_class", choices=cd.MSG_CLASSES)
    e.add_argument("--id", dest="delivery_id")
    tx = e.add_mutually_exclusive_group(required=True)
    tx.add_argument("--text")
    tx.add_argument("--text-file")
    e.add_argument("--wait", type=float, default=0.0,
                   help="seconds to wait for delivered/failed/ambiguous/queued")
    w = sub.add_parser("status")
    w.add_argument("--state-dir", required=True)
    w.add_argument("--id", required=True, dest="delivery_id")
    rp = sub.add_parser("replay")
    rp.add_argument("--state-dir", required=True)
    rp.add_argument("--id", required=True, dest="delivery_id")
    a = ap.parse_args(argv)

    state = Path(a.state_dir)
    if a.cmd == "enqueue":
        text = a.text if a.text is not None else Path(a.text_file).read_text(encoding="utf-8")
        try:
            did = enqueue(state, text, a.msg_class, seat=a.seat, delivery_id=a.delivery_id)
        except ValueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return EXIT_USAGE
        rec = wait_state(state / "deliveries.jsonl", did, a.wait, accept_queued=True) \
            if a.wait > 0 else None
        print(json.dumps({"delivery_id": did, "state": (rec or {}).get("state", "spooled")}))
        return EXIT_OK
    if a.cmd == "status":
        rec = cd.DeliveryLog(state / "deliveries.jsonl").last_state(a.delivery_id)
        print(json.dumps(rec, sort_keys=True))
        return EXIT_OK if rec else 1
    if a.cmd == "replay":
        replay_held(state, a.delivery_id)
        return EXIT_OK

    _dirs(state)
    lock = WriterLock(state / "writer.lock")
    if not lock.acquire():
        print("refused: another writer holds this seat's lock", file=sys.stderr)
        return EXIT_DUPLICATE
    try:
        policy = policy_from_args(a)
    except (ValueError, KeyError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        lock.release()
        return EXIT_USAGE
    binding = SeatBinding(a.seat, policy, state / "thread.json", a.account_email)
    path = SeatDeliveryPath(binding, state, lambda: cd.UnixWebSocketTransport(a.sock),
                            pager=_mailbox_pager, restart_server=_systemctl_restart(a.server_unit))
    try:
        path.startup_reconcile()
        while True:
            path.tick()
            time.sleep(0.2)
    except KeyboardInterrupt:
        return EXIT_OK
    finally:
        path.close()
        lock.release()


if __name__ == "__main__":
    sys.exit(_cli())
