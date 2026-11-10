#!/usr/bin/env python3
"""T-4988 / ORG-103: verified delivery of input to a codex seat via the codex app-server.

Every codex inbound path (operator DMs, urgent DMs, mailbox wakes, ticket pokes,
controller prompts) is meant to call this library instead of typing into the
seat's TUI with tmux. The operational design is in
notes/2026-10-14-t4988-codex-appserver-build.md; this module implements its
delivery-state machine, priority routing, seat-policy ceiling and deny-by-default
approval answers. It is NOT wired into any live path yet.

Stdlib only. Protocol verified against codex 0.147.0 (T-4988 probe,
/home/pantheon-shared/t4988/).

Transports
  UnixWebSocketTransport(path)  a running `codex app-server --listen unix://PATH`
                                (RFC 6455 text frames over AF_UNIX)
  StdioTransport(argv)          a spawned process speaking newline-delimited
                                JSON-RPC on stdio (`codex app-server`, or a fake
                                server in tests)

Delivery states (one record per transition in the append-only JSONL log)
  queued     mailbox/poke while the seat is busy: nothing was sent
  accepted   the server returned a turn id for the turn carrying the message
  delivered  the turn that carried it reached turn/completed with status=completed, and for
             operator_dm/operator_urgent that turn holds the receipt: after the
             message's own userMessage item, a completed Telegram `reply` tool call
             to the DM's chat (T-5413; `ack_receipt`). The id is never asked to be
             echoed, so it stays out of Telegram-visible text. The carrying turn is the
             one whose userMessage item holds the delivery-id tag: a turn/start
             that lands while another turn is active is folded into that turn
             (observed live, codex 0.147.0), and the turn id it returned never
             starts or completes.
  failed     nothing reached the agent (server error, refused, transport down
             before the request was written)
  ambiguous  the request may have reached the agent but delivery could not be
             proven (disconnect, timeout, interrupt/failed turn, no receipt).
             Never blindly replayed.

CLI
  codex_delivery.py deliver (--sock PATH | --stdio-cmd CMD) --thread-id ID
      --sandbox MODE --approval-policy POLICY [--model M] [--cwd DIR]
      --log PATH --id DELIVERY_ID --class CLASS (--text T | --text-file F)
      [--interrupt] [--timeout SECS]
  codex_delivery.py status --log PATH --id DELIVERY_ID

Exit codes (deliver): 0 delivered or queued, 1 failed, 2 ambiguous,
3 refused by the seat policy or bad usage.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import queue
import re
import shlex
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ------------------------------------------------------------------ constants
MSG_CLASSES = ("operator_dm", "operator_urgent", "mailbox", "poke", "controller")
ACK_CLASSES = frozenset({"operator_dm", "operator_urgent"})

QUEUED = "queued"
ACCEPTED = "accepted"
DELIVERED = "delivered"
FAILED = "failed"
AMBIGUOUS = "ambiguous"
STATES = (QUEUED, ACCEPTED, DELIVERED, FAILED, AMBIGUOUS)

SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
# T-6112: thread/resume answers with the thread's initialTurnsPage, so its latency grows with the
# rollout (consultant 2026-11-03: 114 MB rollout, 31.5s over the unix-socket WebSocket vs 6.3s stdio).
# Resume therefore never inherits a short generic request timeout. T-6115 D-5: 300 -> 120s once
# the O(n^2) WS reader was fixed (#1618): the same 116 MB rollout then resumed in 20.4s over WS vs
# 20.7s over stdio at load ~12 (transport parity), a ~6x margin. Thread rotation (D-3) bounds it.
RESUME_TIMEOUT_S = 120.0
SANDBOX_RANK = {mode: i for i, mode in enumerate(SANDBOX_MODES)}
_SANDBOX_WIRE = {"read-only": "readOnly", "workspace-write": "workspaceWrite",
                 "danger-full-access": "dangerFullAccess"}
# externalSandbox means codex itself does not sandbox: rank it as the broadest.
_SANDBOX_FROM_WIRE = {"readOnly": "read-only", "workspaceWrite": "workspace-write",
                      "dangerFullAccess": "danger-full-access",
                      "externalSandbox": "danger-full-access"}
APPROVAL_POLICIES = ("untrusted", "on-request", "never")

JSONRPC_INVALID_REQUEST = -32600
DELIVERY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$")
PREVIEW_CHARS = 60
# Completed-turn outcomes kept for waiters (oldest dropped first). Waiters read
# without consuming (several deliveries can share a turn), so this is the bound.
MAX_TRACKED_TURNS = 64
DELIVERY_TAG_RE = re.compile(r"\[delivery-id: ([A-Za-z0-9][A-Za-z0-9._:-]{3,127})\]")
# T-5413: the operator-class receipt. The poller's header carries the DM's chat id
# (harness/codex/codex_telegram/poller.py); the reply goes out through the seat's
# telegram MCP server's `reply` tool (server, tool as the app-server reports them).
TELEGRAM_REPLY_TOOLS = frozenset({("telegram", "reply")})
OPERATOR_CHAT_RE = re.compile(r"\[inbound Telegram DM from [^|\]\n]*\| chat_id=(-?\d+)")

# Server -> client requests answered deny/decline by default (build note rule 4).
# Shapes are from the codex 0.147.0 schema dump. Note the legacy ReviewDecision
# denial is an object, not the bare string "denied".
_DENY = "denied by pantheon codex_delivery (deny-by-default seat policy)"
APPROVAL_REFUSALS: dict[str, dict] = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "execCommandApproval": {"decision": {"denied": {"rejection": _DENY}}},
    "applyPatchApproval": {"decision": {"denied": {"rejection": _DENY}}},
    "item/permissions/requestApproval": {"permissions": {}, "scope": "turn"},
    "item/tool/requestUserInput": {"answers": {}},
    "mcpServer/elicitation/request": {"action": "decline"},
}

_SECRETISH = [
    re.compile(r"(?i)\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(?:ghp|gho|ghs|ghu|github_pat|glpat|xox[abprs])_[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}"),           # telegram bot token
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(pass(?:word|wd)?|secret|token|api[_-]?key|auth)\s*[:=]\s*\S+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"),  # JWT
    re.compile(r"[A-Za-z0-9+/=_-]{32,}"),                   # long opaque blobs
]


def redact(text: str) -> str:
    out = text
    for pat in _SECRETISH:
        out = pat.sub("[redacted]", out)
    return out


def preview(text: str, n: int = PREVIEW_CHARS) -> str:
    """Short, single-line, secret-scrubbed preview. Never log more than this."""
    flat = " ".join(str(text).split())
    flat = redact(flat)
    return flat if len(flat) <= n else flat[: n - 1] + "…"


# ------------------------------------------------------------------ errors
class DeliveryError(Exception):
    pass


class PolicyViolation(DeliveryError):
    """A request (or the server's reported state) exceeds the seat policy ceiling."""


class RpcError(DeliveryError):
    def __init__(self, method: str, code: Optional[int], message: str):
        super().__init__(f"{method} failed: {code} {message}")
        self.method = method
        self.code = code
        self.message = message


class Disconnected(DeliveryError):
    """The transport closed. `sent` says whether the request had been written."""

    def __init__(self, method: str, sent: bool):
        super().__init__(f"{method}: app-server connection lost ({'after' if sent else 'before'} send)")
        self.method = method
        self.sent = sent


class RequestTimeout(DeliveryError):
    def __init__(self, method: str):
        super().__init__(f"{method}: no response from app-server")
        self.method = method


# ------------------------------------------------------------------ seat policy
@dataclass(frozen=True)
class SeatPolicy:
    """The seat's declared runtime binding. A ceiling no caller can raise."""

    sandbox: str
    approval_policy: str
    model: Optional[str] = None
    cwd: Optional[str] = None
    network_access: bool = False
    writable_roots: tuple = ()

    def __post_init__(self):
        if self.sandbox not in SANDBOX_RANK:
            raise ValueError(f"unknown sandbox mode {self.sandbox!r}")
        if self.approval_policy not in APPROVAL_POLICIES:
            raise ValueError(f"unknown approval policy {self.approval_policy!r}")
        object.__setattr__(self, "writable_roots", tuple(self.writable_roots))

    def constrain(self, *, sandbox: Optional[str] = None, approval_policy: Optional[str] = None,
                  model: Optional[str] = None, network_access: Optional[bool] = None) -> "SeatPolicy":
        """Return the effective policy for a caller request, or raise PolicyViolation.

        A caller may only narrow the sandbox. Approval policy and model are fixed
        by the binding (an exact match is the only accepted value); network
        access may be dropped, never added.
        """
        eff_sandbox = self.sandbox
        if sandbox is not None:
            if sandbox not in SANDBOX_RANK:
                raise PolicyViolation(f"unknown sandbox mode {sandbox!r}")
            if SANDBOX_RANK[sandbox] > SANDBOX_RANK[self.sandbox]:
                raise PolicyViolation(f"sandbox {sandbox!r} exceeds seat ceiling {self.sandbox!r}")
            eff_sandbox = sandbox
        if approval_policy is not None and approval_policy != self.approval_policy:
            raise PolicyViolation(
                f"approval policy {approval_policy!r} differs from seat binding {self.approval_policy!r}")
        if model is not None and model != self.model:
            raise PolicyViolation(f"model {model!r} differs from seat binding {self.model!r}")
        eff_net = self.network_access
        if network_access is not None:
            if network_access and not self.network_access:
                raise PolicyViolation("network access exceeds seat ceiling")
            eff_net = network_access
        return SeatPolicy(eff_sandbox, self.approval_policy, self.model, self.cwd, eff_net,
                          self.writable_roots if eff_sandbox == "workspace-write" else ())

    def sandbox_policy(self) -> dict:
        kind = _SANDBOX_WIRE[self.sandbox]
        if kind == "dangerFullAccess":
            return {"type": kind}
        if kind == "readOnly":
            return {"type": kind, "networkAccess": bool(self.network_access)}
        return {"type": kind, "networkAccess": bool(self.network_access),
                "writableRoots": list(self.writable_roots)}

    def resume_params(self, thread_id: str) -> dict:
        p = {"threadId": thread_id, "sandbox": self.sandbox, "approvalPolicy": self.approval_policy}
        if self.model:
            p["model"] = self.model
        if self.cwd:
            p["cwd"] = self.cwd
        return p

    def turn_params(self) -> dict:
        p = {"approvalPolicy": self.approval_policy, "sandboxPolicy": self.sandbox_policy()}
        if self.model:
            p["model"] = self.model
        if self.cwd:
            p["cwd"] = self.cwd
        return p

    def verify_reported(self, resume_result: dict) -> None:
        """Fail closed if the server reports settings broader than the ceiling.

        A resume inherits whatever the last client set (T-4988 probe), so the
        response is checked rather than trusted.
        """
        sb = resume_result.get("sandbox")
        if sb is not None:
            if isinstance(sb, dict):
                mode = _SANDBOX_FROM_WIRE.get(sb.get("type"))
                net = sb.get("networkAccess")
                net_on = net is True or (isinstance(net, str) and net not in ("restricted", "disabled"))
            else:
                mode, net_on = (sb if sb in SANDBOX_RANK else None), False
            if mode is None:
                raise PolicyViolation(f"server reported unknown sandbox {sb!r}")
            if SANDBOX_RANK[mode] > SANDBOX_RANK[self.sandbox]:
                raise PolicyViolation(f"server reports sandbox {mode!r} above ceiling {self.sandbox!r}")
            if net_on and not self.network_access and mode != "danger-full-access":
                raise PolicyViolation("server reports network access above ceiling")
        ap = resume_result.get("approvalPolicy")
        if ap is not None and ap != self.approval_policy:
            raise PolicyViolation(f"server reports approval policy {ap!r}, seat binding {self.approval_policy!r}")
        model = resume_result.get("model")
        if self.model and model is not None and model != self.model:
            raise PolicyViolation(f"server reports model {model!r}, seat binding {self.model!r}")


# ------------------------------------------------------------------ transports
class StdioTransport:
    """Spawned process speaking newline-delimited JSON-RPC on stdin/stdout."""

    def __init__(self, argv: list[str], env: Optional[dict] = None):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=0, env=env)
        self._wlock = threading.Lock()

    def send(self, obj: dict) -> None:
        data = (json.dumps(obj) + "\n").encode()
        with self._wlock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise ConnectionError(str(exc)) from exc

    def recv(self) -> Optional[dict]:
        while True:
            try:
                line = self.proc.stdout.readline()
            except (OSError, ValueError):
                return None
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except ValueError:
                continue  # non-JSON noise on stdout is ignored, not fatal

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass
        try:
            self.proc.stdout.close()
        except Exception:
            pass


class UnixWebSocketTransport:
    """Minimal RFC 6455 client over AF_UNIX: text frames, client-side masking,
    fragmented messages, ping/pong and close. One JSON message per message."""

    def __init__(self, path: str, connect_timeout: float = 10.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(connect_timeout)
        self.sock.connect(path)
        self._wlock = threading.Lock()
        self._buf = bytearray()
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % key).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("socket closed during WebSocket handshake")
            buf += chunk
            if len(buf) > 65536:
                raise ConnectionError("oversized WebSocket handshake response")
        head, rest = buf.split(b"\r\n\r\n", 1)
        self._buf = bytearray(rest)
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise ConnectionError("WebSocket upgrade refused: %r" % status[:120])
        self.sock.settimeout(None)

    def _read(self, n: int) -> bytes:
        # T-6115 D-6: a bytearray consumed in place. `bytes +=` re-copied the whole
        # buffer per 64 KiB chunk, so one large frame (thread/resume's initialTurnsPage)
        # cost O(n^2): 32 MiB took 12.5s, and consultant's resume 31.5s vs 6.3s over stdio.
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, min(n - len(self._buf), 1 << 22)))
            if not chunk:
                raise EOFError
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    @staticmethod
    def encode_frame(opcode: int, payload: bytes, mask: Optional[bytes]) -> bytes:
        hdr = bytes([0x80 | opcode])
        n = len(payload)
        mbit = 0x80 if mask else 0
        if n < 126:
            hdr += bytes([mbit | n])
        elif n < 65536:
            hdr += bytes([mbit | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([mbit | 127]) + struct.pack(">Q", n)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            return hdr + mask + payload
        return hdr + payload

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        frame = self.encode_frame(opcode, payload, os.urandom(4))
        with self._wlock:
            try:
                self.sock.sendall(frame)
            except OSError as exc:
                raise ConnectionError(str(exc)) from exc

    def send(self, obj: dict) -> None:
        self._send_frame(0x1, json.dumps(obj).encode())

    def recv(self) -> Optional[dict]:
        msg = bytearray()
        try:
            while True:
                b0, b1 = self._read(2)
                fin, opcode = b0 & 0x80, b0 & 0x0F
                n = b1 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._read(8))[0]
                mask = self._read(4) if b1 & 0x80 else None
                data = self._read(n)
                if mask:
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                if opcode == 0x8:
                    return None
                if opcode == 0x9:
                    self._send_frame(0xA, data)
                    continue
                if opcode == 0xA:
                    continue
                msg += data
                if fin:
                    try:
                        return json.loads(msg)
                    except ValueError:
                        msg = bytearray()
                        continue
        except (EOFError, OSError, ConnectionError):
            return None

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ------------------------------------------------------------------ delivery log
class DeliveryLog:
    """Append-only JSONL. One record per state transition or audit event.

    `lock()` serializes writers (check-then-send) across processes and threads
    with fcntl.flock on `<log>.lock`. Single-line appends use O_APPEND and are
    written in one write() call, so audit events from the reader thread need no
    lock (and must not take it: that could deadlock against a writer waiting on
    the server)."""

    def __init__(self, path: os.PathLike | str):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        rec.update(record)
        line = (json.dumps(rec, sort_keys=True) + "\n").encode()
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

    def records(self) -> list[dict]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        out = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # a torn line never blocks the log
        return out

    def last_state(self, delivery_id: str) -> Optional[dict]:
        last = None
        for rec in self.records():
            if rec.get("delivery_id") == delivery_id and rec.get("state") in STATES:
                last = rec
        return last

    def lock(self) -> "_FlockCtx":
        return _FlockCtx(self.lock_path)


class _FlockCtx:
    def __init__(self, path: Path):
        self.path = path
        self.fd: Optional[int] = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None
        return False


# ------------------------------------------------------------------ result
@dataclass
class DeliveryResult:
    delivery_id: str
    msg_class: str
    state: str
    turn_id: Optional[str] = None
    route: Optional[str] = None
    evidence: dict = field(default_factory=dict)
    duplicate: bool = False

    def as_dict(self) -> dict:
        return {"delivery_id": self.delivery_id, "msg_class": self.msg_class, "state": self.state,
                "turn_id": self.turn_id, "route": self.route, "evidence": self.evidence,
                "duplicate": self.duplicate}


def format_input(text: str, delivery_id: str, msg_class: str) -> str:
    """The exact text the agent sees: a tagged id line (it identifies the carrying
    turn) and the message. Operator classes also get an instruction NOT to repeat the
    tag: their receipt is the Telegram reply itself (T-5413, the operator TG 2187/2191)."""
    parts = [f"[delivery-id: {delivery_id}]", text]
    if msg_class in ACK_CLASSES:
        parts.append("(The delivery-id line above is internal routing. Do not repeat it "
                     "or its id in your reply.)")
    return "\n".join(parts)


def operator_chat_id(text: str) -> Optional[str]:
    """The chat id in the poller's DM header, or None when there is no header."""
    m = OPERATOR_CHAT_RE.search(text or "")
    return m.group(1) if m else None


def _tool_args(item: dict) -> dict:
    args = item.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return {}
    return args if isinstance(args, dict) else {}


def ack_receipt(items, delivery_id: str, chat_id: Optional[str]) -> bool:
    """T-5413: did this turn answer the operator message `delivery_id`?

    `items` are the turn's ThreadItems in order. True when, AFTER the userMessage that
    carries the id's tag, a Telegram reply tool call completed without error, to
    `chat_id` (any chat when the message carried no header, which only tests send).
    Ordering keeps it per delivery: a DM folded into a turn that had already replied to
    an earlier message is not proven by that earlier reply."""
    seen = False
    for item in items or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "userMessage":
            if any(isinstance(p, dict) and delivery_id in DELIVERY_TAG_RE.findall(p.get("text") or "")
                   for p in item.get("content") or []):
                seen = True
        elif seen and kind == "mcpToolCall":
            if ((item.get("server"), item.get("tool")) in TELEGRAM_REPLY_TOOLS
                    and item.get("status") == "completed" and not item.get("error")
                    and (chat_id is None or str(_tool_args(item).get("chat_id")) == chat_id)):
                return True
    return False


# ------------------------------------------------------------------ client
class CodexDeliveryClient:
    """One connection to a seat's app-server, bound to one thread and one SeatPolicy."""

    def __init__(self, transport, policy: SeatPolicy, log: DeliveryLog | os.PathLike | str, *,
                 seat: str = "", request_timeout: float = 60.0,
                 client_name: str = "pantheon_codex_delivery"):
        if not isinstance(policy, SeatPolicy):
            raise TypeError("policy must be a SeatPolicy")
        self.t = transport
        self.policy = policy
        self.policy_effective = policy
        self.log =log if isinstance(log, DeliveryLog) else DeliveryLog(log)
        self.seat = seat
        self.request_timeout = request_timeout
        self.client_name = client_name
        self.thread_id: Optional[str] = None

        self._next_id = 1
        self._id_lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._cv = threading.Condition()
        self._closed = False
        # turn tracking (guarded by _cv)
        self._busy = False
        self._active_turn: Optional[str] = None
        self._our_turns: set[str] = set()
        self._starts_in_flight = 0
        self._unattributed: set[str] = set()
        self._turn_outcome: dict[str, dict] = {}
        self._agent_msgs: dict[str, list[str]] = {}
        self._turn_items: dict[str, list[dict]] = {}   # T-5413: completed items, in order
        self._carrier: dict[str, str] = {}   # delivery id -> turn whose userMessage carries it
        self.human_turns: list[str] = []
        self.approvals: list[dict] = []
        # Observers called on the reader thread as listener(event, params) for every
        # notification, "server_request" and finally "closed". They must not block or
        # call request() (the reply would arrive on the thread they are blocking).
        self.listeners: list = []

        self._reader = threading.Thread(target=self._read_loop, name="codex-delivery-reader",
                                        daemon=True)
        self._reader.start()

    # ---------------------------------------------------------- plumbing
    def _audit(self, event: str, **fields) -> None:
        rec = {"event": event, "seat": self.seat, "thread_id": self.thread_id}
        rec.update(fields)
        try:
            self.log.append(rec)
        except OSError:
            pass  # audit is best-effort; never crash the reader

    def _read_loop(self) -> None:
        while True:
            msg = self.t.recv()
            if msg is None:
                break
            if not isinstance(msg, dict):
                continue
            if "method" in msg and "id" in msg:
                self._answer_server_request(msg)
            elif "id" in msg:
                q = self._pending.pop(msg.get("id"), None)
                if q is not None:
                    q.put(msg)
            elif "method" in msg:
                self._on_notification(msg["method"], msg.get("params") or {})
                self._emit(msg["method"], msg.get("params") or {})
        with self._cv:
            self._closed = True
            self._cv.notify_all()
        for q in list(self._pending.values()):
            q.put(None)
        self._pending.clear()
        self._emit("closed", {})

    def _emit(self, event: str, params: dict) -> None:
        for fn in list(self.listeners):
            try:
                fn(event, params)
            except Exception as exc:  # an observer bug never kills the reader
                self._audit("listener_error", listener_event=event, error=repr(exc)[:200])

    def _answer_server_request(self, msg: dict) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method in APPROVAL_REFUSALS:
            reply = {"id": msg["id"], "result": APPROVAL_REFUSALS[method]}
            answer = "declined"
        else:
            reply = {"id": msg["id"], "error": {"code": -32601,
                                                "message": f"codex_delivery: unsupported {method}"}}
            answer = "unsupported"
        try:
            self.t.send(reply)
        except ConnectionError:
            answer += " (send failed)"
        rec = {"method": method, "answer": answer, "turn_id": params.get("turnId"),
               "item_id": params.get("itemId")}
        self.approvals.append(rec)
        self._audit("server_request", **rec)
        self._emit("server_request", rec)

    def _on_notification(self, method: str, p: dict) -> None:
        if p.get("threadId") not in (None, self.thread_id):
            return
        with self._cv:
            if method == "turn/started":
                tid = (p.get("turn") or {}).get("id")
                if tid:
                    self._busy = True
                    self._active_turn = tid
                    if tid not in self._our_turns:
                        if self._starts_in_flight:
                            self._unattributed.add(tid)
                        else:
                            self._note_human(tid)
            elif method == "turn/completed":
                turn = p.get("turn") or {}
                tid = turn.get("id")
                if tid:
                    msgs = self._agent_msgs.pop(tid, None) or []
                    seen_items = self._turn_items.pop(tid, None) or []
                    turn_items = [i for i in turn.get("items") or [] if isinstance(i, dict)]
                    for item in turn.get("items") or []:
                        if isinstance(item, dict) and item.get("type") == "agentMessage":
                            text = item.get("text") or ""
                            if text not in msgs:
                                msgs.append(text)
                    self._our_turns.discard(tid)
                    self._turn_outcome[tid] = {"status": turn.get("status"),
                                               "final": msgs[-1] if msgs else None,
                                               "agent_messages": msgs,
                                               # Both views: whether turn/completed's
                                               # items is complete is not specified.
                                               "items": turn_items,
                                               "streamed_items": seen_items,
                                               "error": turn.get("error")}
                    while len(self._turn_outcome) > MAX_TRACKED_TURNS:
                        self._turn_outcome.pop(next(iter(self._turn_outcome)))
                    if self._active_turn == tid:
                        self._active_turn = None
                        self._busy = False
            elif method == "item/completed":
                item = p.get("item") or {}
                tid = p.get("turnId")
                if tid and isinstance(item, dict) and item.get("type") in (
                        "userMessage", "mcpToolCall", "agentMessage"):
                    self._turn_items.setdefault(tid, []).append(item)
                    while len(self._turn_items) > MAX_TRACKED_TURNS:
                        self._turn_items.pop(next(iter(self._turn_items)))
                if tid and item.get("type") == "agentMessage":
                    self._agent_msgs.setdefault(tid, []).append(item.get("text") or "")
                elif tid and item.get("type") == "userMessage":
                    self._note_carrier(tid, item)
            elif method == "item/started":
                item = p.get("item") or {}
                tid = p.get("turnId")
                if tid and item.get("type") == "userMessage":
                    self._note_carrier(tid, item)
                    # Its position is what orders the receipt; a server that streams a
                    # userMessage only as item/started must still place it (T-5413).
                    self._turn_items.setdefault(tid, []).append(item)
            elif method == "thread/status/changed":
                st = (p.get("status") or {}).get("type")
                if st == "active":
                    self._busy = True
                elif st in ("idle", "notLoaded", "systemError"):
                    self._busy = False
                    self._active_turn = None
            self._cv.notify_all()

    def _note_carrier(self, tid: str, item: dict) -> None:
        # caller holds _cv
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            for did in DELIVERY_TAG_RE.findall(part.get("text") or ""):
                self._carrier[did] = tid
        while len(self._carrier) > MAX_TRACKED_TURNS:
            self._carrier.pop(next(iter(self._carrier)))

    def _note_human(self, tid: str) -> None:
        # caller holds _cv
        self.human_turns.append(tid)
        self._audit("human_turn_observed", turn_id=tid)

    def _new_id(self) -> int:
        with self._id_lock:
            rid = self._next_id
            self._next_id += 1
            return rid

    def request(self, method: str, params: Optional[dict] = None,
                timeout: Optional[float] = None) -> dict:
        rid = self._new_id()
        q: queue.Queue = queue.Queue()
        self._pending[rid] = q
        if self._closed:
            self._pending.pop(rid, None)
            raise Disconnected(method, sent=False)
        msg: dict[str, Any] = {"id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self.t.send(msg)
        except ConnectionError:
            self._pending.pop(rid, None)
            raise Disconnected(method, sent=False)
        try:
            resp = q.get(timeout=self.request_timeout if timeout is None else timeout)
        except queue.Empty:
            self._pending.pop(rid, None)
            raise RequestTimeout(method)
        if resp is None:
            raise Disconnected(method, sent=True)
        if "error" in resp:
            err = resp.get("error") or {}
            raise RpcError(method, err.get("code"), str(err.get("message", "")))
        return resp.get("result") or {}

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        msg: dict[str, Any] = {"method": method}
        if params is not None:
            msg["params"] = params
        self.t.send(msg)

    # ---------------------------------------------------------- session
    def initialize(self) -> dict:
        res = self.request("initialize", {"clientInfo": {"name": self.client_name,
                                                         "title": "Pantheon codex delivery",
                                                         "version": "0.1"}})
        self.notify("initialized")
        return res

    def resume(self, thread_id: str, *, sandbox: Optional[str] = None,
               approval_policy: Optional[str] = None, model: Optional[str] = None,
               network_access: Optional[bool] = None) -> dict:
        """Resume `thread_id` with settings no broader than the seat policy.

        Raises PolicyViolation before anything is sent if the request is broader,
        and after the response if the server reports broader settings."""
        eff = self.policy.constrain(sandbox=sandbox, approval_policy=approval_policy, model=model,
                                    network_access=network_access)
        self.thread_id = thread_id
        res = self.request("thread/resume", eff.resume_params(thread_id),
                           timeout=max(self.request_timeout, RESUME_TIMEOUT_S))
        eff.verify_reported(res)
        self.policy_effective = eff
        thread = res.get("thread") or {}
        with self._cv:
            st = (thread.get("status") or {}).get("type")
            self._busy = st == "active"
            for turn in thread.get("turns") or []:
                if isinstance(turn, dict) and turn.get("status") == "inProgress":
                    self._active_turn = turn.get("id")
                    self._busy = True
        self._audit("resumed", busy=self._busy, active_turn=self._active_turn,
                    sandbox=eff.sandbox, approval_policy=eff.approval_policy, model=eff.model)
        return res

    def connect(self, thread_id: str, **overrides) -> dict:
        self.initialize()
        return self.resume(thread_id, **overrides)

    @property
    def closed(self) -> bool:
        with self._cv:
            return self._closed

    @property
    def busy(self) -> bool:
        with self._cv:
            return self._busy

    @property
    def active_turn(self) -> Optional[str]:
        with self._cv:
            return self._active_turn

    def close(self) -> None:
        try:
            self.t.close()
        finally:
            self._reader.join(timeout=5)

    # ---------------------------------------------------------- turn ops
    def _input(self, text: str) -> list[dict]:
        return [{"type": "text", "text": text}]

    def _turn_start(self, text: str) -> str:
        with self._cv:
            self._starts_in_flight += 1
        try:
            params = {"threadId": self.thread_id, "input": self._input(text)}
            params.update(self.policy_effective.turn_params())
            res = self.request("turn/start", params)
            tid = (res.get("turn") or {}).get("id")
            if not tid:
                raise RpcError("turn/start", None, "response carried no turn id")
            with self._cv:
                self._our_turns.add(tid)
                self._unattributed.discard(tid)
                # T-5308 drill 5: the turn is running from the moment the server hands
                # back its id, before turn/started arrives. Without this a mail or poke
                # sent in the same instant reads idle and is folded into this turn
                # instead of queueing. Skip it if the turn already completed.
                if tid not in self._turn_outcome:
                    self._busy = True
                    if self._active_turn is None:
                        self._active_turn = tid
                return tid
        finally:
            with self._cv:
                self._starts_in_flight -= 1
                if not self._starts_in_flight:
                    for other in sorted(self._unattributed):
                        if other not in self._our_turns:
                            self._note_human(other)
                    self._unattributed.clear()

    def _turn_steer(self, text: str, expected_turn: str) -> str:
        res = self.request("turn/steer", {"threadId": self.thread_id,
                                          "expectedTurnId": expected_turn,
                                          "input": self._input(text)})
        return res.get("turnId") or expected_turn

    def _interrupt_active(self, wait: float) -> Optional[str]:
        """Interrupt the active turn and wait (bounded) for it to complete."""
        with self._cv:
            tid = self._active_turn
        if not tid:
            return None
        try:
            self.request("turn/interrupt", {"threadId": self.thread_id, "turnId": tid})
        except RpcError as exc:
            if exc.code != JSONRPC_INVALID_REQUEST:
                raise
        deadline = time.monotonic() + wait
        with self._cv:
            while tid not in self._turn_outcome and not self._closed:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cv.wait(left)
        return tid

    def _wait_busy_turn_id(self, wait: float) -> Optional[str]:
        deadline = time.monotonic() + wait
        with self._cv:
            while self._busy and not self._active_turn and not self._closed:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cv.wait(left)
            return self._active_turn

    def _await_turn(self, tid: str, timeout: float,
                    delivery_id: Optional[str] = None) -> tuple[Optional[dict], str]:
        """Wait for the turn that carries `delivery_id` (else `tid`) to complete.

        Returns (outcome or None on timeout, carrying turn id)."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                carrier = self._carrier.get(delivery_id, tid) if delivery_id else tid
                if carrier in self._turn_outcome:
                    break
                if self._closed:
                    return {"_disconnected": True}, carrier
                left = deadline - time.monotonic()
                if left <= 0:
                    return None, carrier
                self._cv.wait(left)
            if carrier != tid:
                self._our_turns.discard(tid)   # the returned id will never run
            # Read, never consume: several deliveries can share one turn (a DM steered
            # into a turn another delivery started; T-5308 drill 2 on consultant). The dict
            # is bounded by MAX_TRACKED_TURNS, so leaving the entry costs nothing.
            return dict(self._turn_outcome[carrier]), carrier

    # ---------------------------------------------------------- deliver
    def deliver(self, text: str, delivery_id: str, msg_class: str, *, interrupt: bool = False,
                timeout: float = 1800.0, interrupt_wait: float = 30.0,
                replay_ambiguous: bool = False, on_sent=None) -> DeliveryResult:
        """Deliver `text` to the resumed thread and return the proven outcome.

        `on_sent()`, if given, is called once the send phase is over (the message was
        sent, queued, refused or found to be a duplicate), before waiting for the
        carrying turn. A caller that serializes senders uses it to release the next."""
        if msg_class not in MSG_CLASSES:
            raise ValueError(f"unknown msg_class {msg_class!r}")
        if not DELIVERY_ID_RE.match(delivery_id or ""):
            raise ValueError("delivery_id must match %s" % DELIVERY_ID_RE.pattern)
        if self.thread_id is None:
            raise DeliveryError("resume() a thread before deliver()")
        if interrupt and msg_class != "controller":
            raise ValueError("interrupt= is only meaningful for msg_class='controller'")

        base = {"delivery_id": delivery_id, "msg_class": msg_class, "seat": self.seat,
                "thread_id": self.thread_id}
        payload = format_input(text, delivery_id, msg_class)

        try:
            sent = self._deliver_send(text, delivery_id, msg_class, base, payload, interrupt,
                                      interrupt_wait, replay_ambiguous)
        finally:
            if on_sent is not None:
                on_sent()
        if isinstance(sent, DeliveryResult):
            return sent
        route, tid = sent
        return self._deliver_await(delivery_id, msg_class, base, route, tid, timeout,
                                   operator_chat_id(text))

    def _deliver_send(self, text, delivery_id, msg_class, base, payload, interrupt,
                      interrupt_wait, replay_ambiguous):
        """Locked check-then-send. Returns a final DeliveryResult or (route, turn id)."""
        with self.log.lock():
            prior = self.log.last_state(delivery_id)
            if prior is not None:
                pst = prior.get("state")
                if pst == DELIVERED or (pst in (ACCEPTED, AMBIGUOUS) and not replay_ambiguous):
                    # accepted-without-terminal means an earlier writer died mid-flight:
                    # fate unknown, so it is ambiguous and not replayed.
                    return DeliveryResult(delivery_id, prior.get("msg_class", msg_class),
                                          pst if pst != ACCEPTED else AMBIGUOUS,
                                          prior.get("turn_id"), prior.get("route"),
                                          {"prior_record": prior, "note": "not re-sent"},
                                          duplicate=True)

            def record(state: str, **extra) -> None:
                rec = dict(base, state=state, preview=preview(text))
                rec.update(extra)
                self.log.append(rec)

            route, tid, err = self._send(payload, msg_class, interrupt, interrupt_wait)
            if tid is None:
                state = QUEUED if route == "queued" else (AMBIGUOUS if err and err.get("maybe_sent")
                                                          else FAILED)
                record(state, route=route, error=err)
                return DeliveryResult(delivery_id, msg_class, state, None, route,
                                      {"error": err} if err else {})
            record(ACCEPTED, route=route, turn_id=tid)
            return route, tid

    def _deliver_await(self, delivery_id, msg_class, base, route, tid, timeout,
                       chat_id: Optional[str] = None) -> DeliveryResult:
        # Outside the lock: an operator_urgent must be able to interrupt while
        # an earlier delivery waits for its turn to finish.
        outcome, carrier = self._await_turn(tid, timeout, delivery_id)
        evidence: dict[str, Any] = {}
        if carrier != tid:
            evidence["folded_into"] = carrier
            self._audit("turn_folded", delivery_id=delivery_id, returned_turn=tid,
                        carrier_turn=carrier)
        if outcome is None:
            state, evidence["reason"] = AMBIGUOUS, "timeout waiting for turn/completed"
        elif outcome.get("_disconnected"):
            state, evidence["reason"] = AMBIGUOUS, "disconnected after accepted"
        else:
            evidence["turn_status"] = outcome.get("status")
            final = outcome.get("final")
            evidence["final_preview"] = preview(final or "")
            if outcome.get("error"):
                evidence["turn_error"] = outcome.get("error")
            if outcome.get("status") != "completed":
                state, evidence["reason"] = AMBIGUOUS, f"turn ended {outcome.get('status')}"
            elif msg_class in ACK_CLASSES and not any(
                    ack_receipt(outcome.get(k), delivery_id, chat_id)
                    for k in ("items", "streamed_items")):
                state, evidence["reason"] = AMBIGUOUS, "completed without a Telegram reply"
            else:
                state = DELIVERED
                if msg_class in ACK_CLASSES:
                    evidence["receipt"] = "telegram_reply"
        with self.log.lock():
            self.log.append(dict(base, state=state, route=route, turn_id=carrier,
                                 evidence=evidence))
        return DeliveryResult(delivery_id, msg_class, state, carrier, route, evidence)

    def resend_tagged(self, payload: str) -> tuple[str, str]:
        """Re-send an already-accepted message's tagged payload with no log gate.

        Used only by the seat delivery path's compaction re-steer: steer into the
        active turn, else start one. The original deliver() keeps waiting on the
        carrying turn, which the tag identifies wherever it lands."""
        with self._cv:
            tid = self._active_turn if self._busy else None
        if tid:
            return "steer", self._turn_steer(payload, tid)
        return "start", self._turn_start(payload)

    def read_thread(self, include_turns: bool = True) -> dict:
        res = self.request("thread/read", {"threadId": self.thread_id,
                                           "includeTurns": include_turns})
        return res.get("thread") or {}

    def _send(self, payload: str, msg_class: str, interrupt: bool,
              interrupt_wait: float) -> tuple[str, Optional[str], Optional[dict]]:
        """Route and send. Returns (route, turn_id or None, error or None)."""
        try:
            busy = self.busy
            if msg_class in ("mailbox", "poke"):
                if busy:
                    return "queued", None, None
                return "start", self._turn_start(payload), None

            if msg_class == "operator_urgent" or (msg_class == "controller" and interrupt):
                route = "start"
                if busy:
                    self._wait_busy_turn_id(interrupt_wait)
                    if self._interrupt_active(interrupt_wait):
                        route = "interrupt+start"
                return route, self._turn_start(payload), None

            if msg_class == "controller":
                return "start", self._turn_start(payload), None

            # operator_dm
            if busy:
                expected = self._wait_busy_turn_id(interrupt_wait)
                if expected:
                    try:
                        return "steer", self._turn_steer(payload, expected), None
                    except RpcError as exc:
                        if not (exc.code == JSONRPC_INVALID_REQUEST
                                and "no active turn" in exc.message.lower()):
                            raise
                        self._audit("steer_race", turn_id=expected)
                        return "steer->start", self._turn_start(payload), None
            try:
                return "start", self._turn_start(payload), None
            except RpcError:
                # A human turn may have started between our idle read and turn/start.
                expected = self._wait_busy_turn_id(min(interrupt_wait, 5.0))
                if not expected:
                    raise
                return "start->steer", self._turn_steer(payload, expected), None
        except RpcError as exc:
            return "error", None, {"method": exc.method, "code": exc.code,
                                   "message": preview(exc.message, 200), "maybe_sent": False}
        except Disconnected as exc:
            return "error", None, {"method": exc.method, "message": "disconnected",
                                   "maybe_sent": exc.sent and exc.method in ("turn/start",
                                                                             "turn/steer")}
        except RequestTimeout as exc:
            return "error", None, {"method": exc.method, "message": "request timeout",
                                   "maybe_sent": exc.method in ("turn/start", "turn/steer")}


# ------------------------------------------------------------------ CLI
def _cli(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("deliver")
    conn = d.add_mutually_exclusive_group(required=True)
    conn.add_argument("--sock")
    conn.add_argument("--stdio-cmd")
    d.add_argument("--thread-id", required=True)
    d.add_argument("--sandbox", required=True, choices=SANDBOX_MODES)
    d.add_argument("--approval-policy", required=True, choices=APPROVAL_POLICIES)
    d.add_argument("--model")
    d.add_argument("--cwd")
    d.add_argument("--seat", default="")
    d.add_argument("--log", required=True)
    d.add_argument("--id", required=True, dest="delivery_id")
    d.add_argument("--class", required=True, dest="msg_class", choices=MSG_CLASSES)
    txt = d.add_mutually_exclusive_group(required=True)
    txt.add_argument("--text")
    txt.add_argument("--text-file")
    d.add_argument("--interrupt", action="store_true")
    d.add_argument("--timeout", type=float, default=1800.0)
    s = sub.add_parser("status")
    s.add_argument("--log", required=True)
    s.add_argument("--id", required=True, dest="delivery_id")
    a = ap.parse_args(argv)

    if a.cmd == "status":
        rec = DeliveryLog(a.log).last_state(a.delivery_id)
        print(json.dumps(rec, sort_keys=True))
        return 0 if rec else 1

    text = a.text if a.text is not None else Path(a.text_file).read_text(encoding="utf-8")
    try:
        policy = SeatPolicy(a.sandbox, a.approval_policy, a.model, a.cwd)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    transport = (UnixWebSocketTransport(a.sock) if a.sock
                 else StdioTransport(shlex.split(a.stdio_cmd)))
    client = CodexDeliveryClient(transport, policy, a.log, seat=a.seat)
    try:
        try:
            client.connect(a.thread_id)
        except PolicyViolation as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 3
        except DeliveryError as exc:
            print(json.dumps({"state": FAILED, "error": str(exc)}))
            return 1
        try:
            res = client.deliver(text, a.delivery_id, a.msg_class, interrupt=a.interrupt,
                                 timeout=a.timeout)
        except ValueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 3
        print(json.dumps(res.as_dict(), sort_keys=True))
        return {DELIVERED: 0, QUEUED: 0, FAILED: 1, AMBIGUOUS: 2}.get(res.state, 2)
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(_cli())
