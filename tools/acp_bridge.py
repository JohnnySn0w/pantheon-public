"""tools/acp_bridge.py — mailbox -> ACP inbound bridge engine (T-1633, ORG-031 Unit 1).

Replaces tmux send-keys with a native JSON-RPC 2.0 session over the
claude-code-acp wrapper's stdio (ndjson framing — confirmed empirically in
the T-1630 spike, see knowledge/episodic/2026-07-07-claude-code-acp-spike/).
This module is the ENGINE ONLY: importable, clean-room-testable, no install
and no enable. `tools/acp_bridge_wetrun.py` is the separate empirical gate
against the real wrapper; `tools/test_acp_bridge.py` drives this module
end-to-end against a duck-typed transport double.

Design summary (approved baseline, T-1633 dispatch):

  - AcpTransport: real stdio transport (Popen + ndjson recv-loop, mirrors the
    spike's rung2.py shape).
  - FakeAcpTransport: NOT a subclass — a duck-typed test double with the same
    method surface, scripted responses, no subprocess.
  - assert_clean_room(): the API-key-shadow money-trap hard gate. Any env var
    starting ANTHROPIC or CLAUDE_CODE — INCLUDING an empty-string value
    (membership, not truthiness) — is a CleanRoomViolation. A leaked key
    silently flips the agent from subscription to metered API billing.
  - AcpBridge: session lifecycle (initialize -> session/load-or-new, with
    ANY session/load error falling back to session/new) + the mailbox-turn
    loop (tools/mailbox.py's consume() ack contract: handler returns ->
    ack-by-move-to-cur/; handler raises -> message stays in new/ for retry).
    A failed turn is never lost and never silently acked — -32000
    (Authentication required) is a RAISE like any other transport failure,
    not a swallowed no-op.

session/load — EMPIRICAL RESULT, RESOLVED (T-1690 root cause, live-proven
2026-07-12 on real claude-code-acp 0.16.2): session/load({sessionId, cwd,
mcpServers: []}) WORKS — including on the LIVE pilot bridge, whose session
id survived a systemctl restart — with ONE edge: it fails -32603 for a
session that never completed a turn, because the Claude Agent SDK writes
no transcript JSONL until the first turn, so the wrapper's
findSessionFile() finds nothing and throws ("Session not found", wrapped
by the JSON-RPC layer as generic -32603 Internal error). The T-1689
wet-run gate hit exactly this edge by design accident: empty-inbox runs
create turn-less sessions. CONSEQUENCE: session continuity across bridge
restarts works in production (any bridge that has processed >= 1 message
has a transcript); the any-error fallback to session/new remains
load-bearing for the turn-less edge and for any future wrapper change —
do not remove it. When wet-run-gating a wrapper version bump, send one
canary prompt BEFORE testing session/load, or the empty-session edge
masquerades as a load failure (see the pilot runbook Step 1).

By contrast, session/prompt's wire shape ({sessionId, prompt: [{type: "text",
text}]}, response carrying stopReason) IS empirically spike-verified end to
end against the real wrapper (T-1630 rung2.py/rung3.py; see
knowledge/episodic/2026-07-07-claude-code-acp-spike/notes.md — "Turn
completed end-to-end: stopReason: end_turn"). handle_message() below uses
that exact shape verbatim. The asymmetry matters at pilot time: a
session/load defect is masked by the any-error fallback and needs
acp_bridge_wetrun.py to surface it, whereas any session/prompt failure fails
loud (raises) with no fallback to hide behind.
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import re
import select
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Import the shared mailbox module the same dual-identity-safe way mailbox.py
# itself imports _lease — so `python -m pytest tools/` (package identity
# tools.mailbox) and a bare run from inside tools/ both resolve to ONE module
# object.
try:
    from tools import mailbox
except ImportError:  # bare run with cwd=tools/
    import mailbox  # type: ignore

# keep_going: same dual-identity-safe import (T-1935 — the busy-gate probe
# reuses its pane-capture + tri-state idle detection).
try:
    from tools import keep_going as _keep_going
except ImportError:  # bare run with cwd=tools/
    import keep_going as _keep_going  # type: ignore

# Senders whose messages are idempotent, fire-and-forget heartbeats: the whole
# point is to un-park an idle seat, so the nudge has SUCCEEDED the moment a turn
# is underway. For these, a session/prompt timeout (turn still running past the
# large prompt_timeout) is treated as success and ACKed — never retried, never
# dead-lettered (T-1985). Mirrors mailbox._DLQ_ALERT_SENDER's sender-based
# special-casing. keep_going is the only such sender today (it routes through
# the mailbox for specialist alone — keep_going.DELIVERY["specialist"] == "mailbox"), so
# this is dormant for every other agent and inert on every non-nudge message.
_FIRE_AND_FORGET_SENDERS = frozenset({"keep_going"})


# --------------------------------------------------------------- exceptions
class CleanRoomViolation(Exception):
    """Raised by assert_clean_room() when an ANTHROPIC_*/CLAUDE_CODE_* env var
    is present (even as an empty string) — the API-key-shadow billing trap."""


class AcpError(Exception):
    """A well-formed JSON-RPC error response from the ACP wrapper."""

    def __init__(self, code, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(f"ACP error {code}: {message}")


class AcpTransportError(Exception):
    """A transport-level failure that is NOT a JSON-RPC error object: the
    wrapper process exited mid-wait, or a request timed out. Kept distinct
    from AcpError so callers can tell "the wrapper told me no" from "the
    wrapper vanished/hung" — both still propagate bare through the mailbox
    handler, so the ack-vs-retry contract does not depend on the split."""


class AcpTimeoutError(AcpTransportError):
    """A request timed out waiting for its response — the wrapper is still
    alive, the turn is simply still running server-side. A SUBCLASS of
    AcpTransportError so every existing `except AcpTransportError` and bare
    propagation path behaves identically (backward-compatible); the split
    exists only so handle_message can tell "the turn is still running" (a
    timeout) apart from "the wrapper vanished" (a bare AcpTransportError).
    That distinction is what lets a fire-and-forget nudge treat a still-running
    turn as success (T-1985) while a genuine wrapper-exit still fails."""


class AcpCapError(AcpTransportError):
    """The wrapper reported a USAGE CAP / rate limit (e.g. -32603 "You've hit
    your limit · resets 5pm (UTC)"). An ENVIRONMENTAL failure, not message
    poison: while the seat is capped, EVERY message fails this exact way. A
    subclass of AcpTransportError so existing `except AcpTransportError` /
    bare-propagation paths are unchanged.

    Carries `mailbox_transient = True` — the tools/mailbox.py _drain contract
    (T-2038): a handler failure with this truthy attribute is recorded but NOT
    counted toward DLQ, so the message stays in new/ and delivers cleanly when
    the cap resets. This closes the T-2038 loop: without it, a cap dead-lettered
    innocent keep_going nudges, and each quarantine spawned a mailbox-dlq alert
    that — being quarantine-exempt — then retried forever against the still-
    capped wrapper (observed: ~50k iterations hammering session/prompt)."""

    mailbox_transient = True


# ------------------------------------------------------------ cap detection
# The wrapper wraps a subscription usage-cap as a generic JSON-RPC -32603
# ("Internal error") whose MESSAGE carries the human cap text — so match on the
# message text, never the code (-32603 is also a genuine internal error). Kept
# as a small, extend-friendly signature set: a false negative (wrapper reword)
# merely degrades to the pre-T-2038 behavior; a false positive can't come from
# message content (the text originates from the wrapper, not the mailbox record).
_CAP_ERROR_SIGNATURES = (
    "hit your limit",
    "usage limit",
    "rate limit",
    "resets",  # "resets 5pm (UTC)" — the reset-time clause of the cap message
)


def _is_cap_error(exc: Exception) -> bool:
    """True iff `exc` is an AcpError signalling a usage-cap / rate limit."""
    if not isinstance(exc, AcpError):
        return False
    message = (exc.message or "").lower()
    return any(sig in message for sig in _CAP_ERROR_SIGNATURES)


# ------------------------------------------- cap reset-time parsing (T-2510)
# The cap message carries the reset clock time ("... resets 5:10pm (UTC)").
# The T-2038 breaker originally re-probed on a FIXED cap_cooldown_secs (300s),
# so a multi-HOUR subscription cap ("out of extra usage") was probed ~once every
# 5 min for its whole duration. Each probe calls session/prompt, and the wrapper
# APPENDS the prompt to its session context BEFORE returning the -32603 — so a
# long cap accreted dozens of identical [mailbox:...] banners that all surfaced
# in the one prompt that finally ran after reset (~20x observed on specialist,
# 2026-08-03; T-2510). Parsing the reset time lets the breaker defer until the
# cap actually clears — one probe at reset, not dozens through the window.
_CAP_RESET_RE = re.compile(r"reset[s]?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
                           re.IGNORECASE)
# A margin past the stated reset so the re-probe lands AFTER the cap clears, not
# exactly on the boundary (wrapper/clock skew, reset-processing lag).
_CAP_RESET_MARGIN_SECS = 90.0
# Bound a mis-parse / absurd reset: never defer a single window beyond this. If
# the cap is still live at re-probe, the fresh cap error re-defers correctly.
_MAX_CAP_DEFER_SECS = 6 * 3600.0
# A reset time this-far-or-less BEHIND now reads as boundary clock skew (or the
# cap having just cleared), not a genuine next-day reset — defer nothing, let the
# fixed cooldown re-probe soon. Beyond it, an already-passed time is the NEXT
# occurrence (a cap at 22:00 "resets 12am" -> tomorrow's midnight).
_CAP_RESET_SKEW_GRACE_SECS = 15 * 60.0


def _parse_cap_reset_delay(message: str, now_wall: float | None = None) -> float | None:
    """Seconds from `now_wall` until the reset time named in a cap `message`
    ("... resets 5:10pm (UTC)"), or None if no reset time is parseable.

    UTC is assumed: the observed wrapper cap text always states "(UTC)". A
    future reset today defers to it (a cap seen at 15:00 with "resets 5:10pm"
    defers ~2h10m rather than the fixed 5-min cooldown). A reset time BEHIND
    now is disambiguated by how far behind: within _CAP_RESET_SKEW_GRACE_SECS it
    reads as boundary clock skew / a just-cleared cap and returns None (fixed
    cooldown re-probes soon — deferring ~a day on a boundary misread is the worse
    failure); further behind it is the NEXT occurrence and rolls to tomorrow (a
    cap at 22:00 "resets 12am" -> tomorrow's midnight, ~2h). Returns None on any
    parse failure too (pre-T-2510 behaviour, never worse). The result is bounded
    to (0, _MAX_CAP_DEFER_SECS] with a small margin past the stated reset.
    """
    if not message:
        return None
    m = _CAP_RESET_RE.search(message)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    meridiem = m.group(3).lower()
    if not (1 <= hour <= 12) or minute > 59:
        return None
    hour %= 12                       # 12 -> 0, so 12am -> 0 and (below) 12pm -> 12
    if meridiem == "pm":
        hour += 12
    now_wall = time.time() if now_wall is None else now_wall
    now_dt = datetime.fromtimestamp(now_wall, tz=timezone.utc)
    target = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_dt:
        if (now_dt - target).total_seconds() <= _CAP_RESET_SKEW_GRACE_SECS:
            return None              # boundary skew / just-cleared -> fixed cooldown
        target += timedelta(days=1)  # genuinely the next occurrence -> tomorrow
    delay = (target - now_dt).total_seconds() + _CAP_RESET_MARGIN_SECS
    return min(delay, _MAX_CAP_DEFER_SECS)


# ------------------------------------------------------------- clean room
def assert_clean_room() -> None:
    """Raise CleanRoomViolation if any ANTHROPIC_*/CLAUDE_CODE_* env var is
    present. Membership check (`k in os.environ`), NOT truthiness — an
    ANTHROPIC_API_KEY="" must still raise; a set-but-empty key is still a
    live credential-shadow surface to the SDK."""
    bad = sorted(
        k for k in os.environ if k.startswith("ANTHROPIC") or k.startswith("CLAUDE_CODE")
    )
    if bad:
        raise CleanRoomViolation(
            f"clean-room violated — env vars present (API-key-shadow billing trap): {bad}"
        )


# --------------------------------------------------------- request dispatch
def _build_response_for_request(handlers: dict, msg: dict) -> dict:
    """Build the JSON-RPC response envelope for an INCOMING request message
    ({method, id, params}) from the peer. Never raises: an unregistered
    method becomes -32601 (method not found); a handler that raises becomes
    -32603 (internal error) with str(exc) as the message — this is also how
    fs handler IOErrors surface, no special-casing needed, since any
    exception degrades the same way.

    T-1831: this is the class fix. The prior _recv_until had no concept of
    an incoming request at all — it discarded any message whose id happened
    not to match the response it was waiting on, silently dropping fs
    requests and deadlocking the turn (the T-1830 mnemosyne incident). Every
    incoming request now gets an explicit response, in-band, always.

    Shared by AcpTransport._recv_until's real dispatch and
    FakeAcpTransport.deliver_request's test-only simulation, so both paths
    exercise identical routing logic."""
    method = msg.get("method")
    req_id = msg.get("id")
    handler = handlers.get(method)
    if handler is None:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"method not found: {method!r}"},
        }
    try:
        result = handler(msg.get("params") or {})
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32603, "message": str(exc)},
        }
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


# ------------------------------------------------------------ real transport
class AcpTransport:
    """The real stdio transport: spawns the ACP wrapper and speaks
    newline-delimited JSON-RPC 2.0. ndjson framing only — LSP
    Content-Length framing was checked in the spike and never needed."""

    def __init__(self, wrapper_cmd=None):
        self.wrapper_cmd = list(wrapper_cmd) if wrapper_cmd else ["claude-code-acp"]
        self.proc = subprocess.Popen(
            self.wrapper_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._buf = b""
        self._id_counter = itertools.count(1)
        self.handlers: dict = {}

    def register_handler(self, method: str, handler) -> None:
        """Register a callable to service incoming JSON-RPC requests from the
        wrapper for `method`. Called with the request's `params` dict
        (never None); the return value becomes the JSON-RPC `result`. Any
        exception raised becomes a JSON-RPC error response — see
        _build_response_for_request."""
        self.handlers[method] = handler

    def _send(self, obj) -> None:
        line = (json.dumps(obj) + "\n").encode("utf-8")
        self.proc.stdin.write(line)
        self.proc.stdin.flush()

    def _recv_until(self, pred, timeout: float):
        """Return the first parsed ndjson message satisfying pred(msg), or
        None on timeout/wrapper-exit. Mirrors the spike's rung2.py recv loop
        (select + os.read + split on b"\\n").

        T-1831: a message carrying BOTH "method" and "id" is an INCOMING
        REQUEST from the wrapper (e.g. fs/read_text_file), never our own
        response — our own outgoing requests never echo back with "method"
        set. It is routed inline (single-threaded, same as the wrapper's
        expectation of prompt responses) via _build_response_for_request and
        the JSON-RPC response is sent back immediately; it is NEVER treated
        as a candidate for `pred` (this also sidesteps id-space collisions:
        the wrapper's own request-id counter can legitimately coincide with
        ours, since JSON-RPC id spaces are per-direction, not shared). A
        message carrying "method" with no "id" is a notification — dropped,
        same as always (pred would have returned False on it anyway)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r, _, _ = select.select([self.proc.stdout], [], [], 0.5)
            if r:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                self._buf += chunk
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "method" in msg:
                    if msg.get("id") is not None:
                        self._send(_build_response_for_request(self.handlers, msg))
                    continue
                if pred(msg):
                    return msg
            if self.proc.poll() is not None:
                return None
        return None

    def request(self, method: str, params=None, timeout: float = 60.0):
        """Blocking JSON-RPC request. Returns the `result` value on success.
        Raises AcpError on a JSON-RPC error response, AcpTransportError on
        wrapper-exit or timeout."""
        req_id = next(self._id_counter)
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        msg = self._recv_until(lambda m: m.get("id") == req_id, timeout)
        if msg is None:
            if self.proc.poll() is not None:
                raise AcpTransportError(
                    f"wrapper exited (rc={self.proc.returncode}) while waiting for {method!r}"
                )
            raise AcpTimeoutError(f"timeout waiting for {method!r} response")
        if "error" in msg:
            err = msg["error"] or {}
            raise AcpError(err.get("code"), err.get("message", ""))
        return msg.get("result")

    def notify(self, method: str, params=None) -> None:
        """Fire-and-forget JSON-RPC notification (no id, no response wait)."""
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass


# -------------------------------------------------------------- fake transport
class FakeAcpTransport:
    """Duck-typed test double — same method surface as AcpTransport, no
    subprocess. NOT a subclass (the transport seam is structural, not via
    inheritance).

    `responses`: {method: value | Exception | [value_or_Exception, ...]}.
    A list is popped sequentially per call (for scripting e.g. "session/load
    fails once, then session/new succeeds"). `handler(method, params)` is an
    escape hatch for full programmatic control; if given it wins over
    `responses`. Every call is recorded in `self.calls` for assertions.

    `register_handler`/`handlers` mirror AcpTransport's real request-routing
    surface (T-1831) so AcpBridge.start() can register fs handlers against
    either transport identically. `deliver_request` is a TEST-ONLY addition
    (not part of AcpTransport's real surface) that simulates the wrapper
    sending an incoming request — it runs the exact same
    _build_response_for_request dispatch the real AcpTransport._recv_until
    uses, so routing/error-code/handler-exception behavior is tested once,
    against the shared function, rather than re-implemented here.
    """

    def __init__(self, responses=None, handler=None):
        self.responses = dict(responses or {})
        self._handler = handler
        self.calls: list[tuple[str, dict]] = []
        self.closed = False
        self.handlers: dict = {}
        self.sent_responses: list[dict] = []

    def register_handler(self, method: str, handler) -> None:
        self.handlers[method] = handler

    def deliver_request(self, method: str, params=None, req_id=1) -> dict:
        """TEST-ONLY: simulate an incoming JSON-RPC request from the peer.
        Returns the response envelope that would have been sent back
        (also appended to self.sent_responses)."""
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        resp = _build_response_for_request(self.handlers, msg)
        self.sent_responses.append(resp)
        return resp

    def request(self, method: str, params=None, timeout: float = 60.0):
        self.calls.append((method, params or {}))
        if self._handler is not None:
            result = self._handler(method, params or {})
        elif method in self.responses:
            entry = self.responses[method]
            if isinstance(entry, list):
                if not entry:
                    raise AcpError(-32601, f"no scripted response left for {method!r}")
                result = entry.pop(0)
            else:
                result = entry
        else:
            raise AcpError(-32601, f"no scripted response for {method!r}")
        if isinstance(result, Exception):
            raise result
        return result

    def notify(self, method: str, params=None) -> None:
        self.calls.append((method, params or {}))

    def close(self) -> None:
        self.closed = True


# ------------------------------------------------------------------- bridge
_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"


class AcpBridge:
    """Session lifecycle + the mailbox-message -> session/prompt turn loop."""

    def __init__(
        self,
        *,
        agent: str,
        cwd: str,
        inbox,
        wrapper_cmd=None,
        state_path=None,
        transport=None,
        prompt_timeout: float = 300.0,
        main_pane: str | None = None,
        prefer_main_seat: bool = False,
        consume_ttl_secs: int = mailbox._DEFAULT_CONSUME_TTL_SECS,
        cap_cooldown_secs: float = 300.0,
        batch: bool = False,
        recycle_after_turns: int | None = None,
    ):
        self.agent = agent
        self.cwd = cwd
        self.inbox = Path(inbox)
        self.wrapper_cmd = wrapper_cmd
        self.state_path = Path(state_path) if state_path else (
            _DEFAULT_STATE_DIR / f"acp-bridge-{agent}.json"
        )
        self.prompt_timeout = prompt_timeout
        # T-1935 half (a): the agent's LIVE main-session tmux pane. When set,
        # run_once() defers the whole drain while that pane is affirmatively
        # busy — the wrapper must not execute (possibly repo-mutating) ticket
        # work interleaved with the main session on the same checkout (the
        # T-1827/T-1918 rebase-conflict + reset-debris race). None = gate off
        # (tests / topologies with no main session).
        self.main_pane = main_pane
        # T-2259: when True AND main_pane reads affirmatively idle AND mail is
        # waiting, run_once() wakes the MAIN seat to drain instead of paying one
        # ACP turn per message. Opt-in per unit — the measured casualty
        # (mnemosyne, 83-minute seat monopolisation over an idle main seat)
        # enables it first; agents whose ACP turns are short keep the old path.
        self.prefer_main_seat = prefer_main_seat
        # T-1935: consume()'s per-inbox consumer lease defaults to a 900s TTL
        # with wedged-ALIVE reaping (SIGTERM→SIGKILL) — but a multi-message
        # drain at prompt_timeout=300s/turn can legitimately exceed 900s, so
        # the default would let a second bridge instance KILL a live bridge
        # mid-turn and re-drain (the un-acked message then re-executes: the
        # observed duplicate-send retry site). Size the TTL for a deep drain;
        # a DEAD holder is reaped immediately regardless of TTL, so a large
        # value only delays reaping a wedged-alive process — and every turn
        # is already bounded by the transport's own request timeout.
        self.consume_ttl_secs = consume_ttl_secs
        # T-2038 circuit breaker: when a session/prompt hits a usage cap, stop
        # hammering the capped wrapper. _capped_until is a time.monotonic()
        # deadline; while now < it, run_once() defers the whole drain (messages
        # wait in new/, untouched, uncounted). cap_cooldown_secs also bounds
        # recovery latency after the cap resets (the next drain retries then).
        self.cap_cooldown_secs = cap_cooldown_secs
        self._capped_until = 0.0
        # T-2348: how many times we took the ACP drain while BOTH the main pane
        # and the seat's liveness were unknown. Counted, not just logged, so the
        # split-brain window has a number attached rather than being reconstructed
        # from log greps after the fact.
        self._unknown_seat_drains = 0
        # T-2352: deliver a whole drain snapshot as ONE prompt (opt-in). OFF by
        # default so every existing caller and test keeps the per-message path
        # (the three acceptance tests encode that path and must pass unchanged).
        # When on, run_once() batches the happy path and DEGRADES to per-message
        # on any non-cap batch failure via _deliver_individually below — so
        # failure isolation + DLQ attribution are preserved exactly where they
        # matter (when something went wrong) at zero cost when nothing did.
        self.batch = batch
        # The fallback latch: set True after a non-cap batch failure so the NEXT
        # run_once() delivers that same snapshot PER-MESSAGE (today's proven
        # path), where a poison message fails ALONE and DLQ counts it against the
        # right id. Cleared back to False after a clean per-message pass, so
        # batching resumes as an optimisation once the poison is retired. A cap
        # failure does NOT set it — a cap is environmental, not poison, and the
        # breaker already defers the whole drain until reset.
        self._deliver_individually = False
        # T-2797: proactive session recycling. The wrapper session accumulates
        # context across every delivered message for the whole process life
        # (start() resolves ONE session; run_once() reuses it), so a heavy-mail
        # seat (specialist) eventually crosses the model's context ceiling and every
        # delivery fails ACP -32603 "Prompt is too long" until manually recycled
        # (bugs/acp-bridge-context-full-prompt-too-long-mailbox-backlog.md).
        # When recycle_after_turns is set, run_once() proactively recycles the
        # session (close + delete state file -> next drain does session/new) once
        # _delivered_since_session crosses it AND the drain is idle. Counts
        # DELIVERED MESSAGES (turns), not poll iterations — turns are the causal
        # growth driver (cost is superlinear in turns; cache-read/turn climbs
        # 8.3x by turn 100+). None = OFF (default) so behaviour is unchanged for
        # every existing caller/test until a seat opts in. Enable per-seat AFTER
        # the wet-run calibrates the threshold (acp_bridge_wetrun.py rung).
        self.recycle_after_turns = recycle_after_turns
        self._delivered_since_session = 0
        self._external_transport = transport
        self.transport = None
        self.session_id = None

    # ------------------------------------------------------------- startup
    # fs wire schema (T-1831, verified against the AUTHORITATIVE published
    # source — @agentclientprotocol/sdk 0.14.1, the exact dependency pinned
    # by claude-code-acp 0.16.2's package.json — not guessed from memory):
    #   fs/read_text_file:  {sessionId, path, line?, limit?} -> {content}
    #   fs/write_text_file: {sessionId, path, content}       -> {}
    # `path` is documented "Absolute path to the file" on both; `line` is
    # 1-based start line, `limit` is max lines to read.
    @staticmethod
    def _fs_read_text_file(params: dict) -> dict:
        """fs/read_text_file handler. Any OSError (missing file, permission
        denied, ...) propagates out of this function untouched —
        _build_response_for_request converts it into a JSON-RPC error
        response; this handler does not need its own try/except.

        BINARY FILES ARE REFUSED WITH AN EXPLANATION (T-2226). Reading a PNG used
        to surface the bare UnicodeDecodeError text — "'utf-8' codec can't decode
        byte 0x89 in position 0" — which names a symptom (0x89 is the PNG magic
        byte), not a cause, and reads like a bug in THIS handler. It is not one.
        Verified against the published schema (agentclientprotocol.com/protocol/
        schema, 2026-08-02): ACP defines exactly TWO fs/* methods, read_text_file
        and write_text_file, and NEITHER carries binary. Images are ContentBlocks
        (base64 data + mimeType) that the AGENT produces and streams over
        session/update — they are never served from the client's filesystem. This
        response type is {content: <string>}, so there is no representation in
        which a PNG could be returned here, and the clientCapabilities we
        advertise below are already exactly the two that exist.

        So refusal is the correct behaviour and the only open question was whether
        it explains itself. The agent runs on the same box as the file and can
        read it directly; the error says so, and does not pretend this handler
        could have been fixed.

        Deliberately keyed on the DECODE FAILING rather than sniffing magic bytes
        or scanning for NULs: "is this valid UTF-8" is the exact predicate this
        method's contract cares about, and a heuristic would eventually refuse a
        text file it did not recognise. Multibyte UTF-8 (accents, em-dashes, CJK)
        decodes fine and is served normally — there is a test pinning that.
        """
        path = params["path"]
        line = params.get("line")
        limit = params.get("limit")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                if line is None and limit is None:
                    content = fh.read()
                else:
                    lines = fh.readlines()
                    start = (line - 1) if line else 0
                    end = (start + limit) if limit is not None else None
                    content = "".join(lines[start:end])
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"{path} is a binary file, not UTF-8 text (decode failed at byte "
                f"{exc.start}). fs/read_text_file returns a string and ACP defines "
                "no binary or image filesystem method, so this file cannot be "
                "served over the protocol. An agent needing its contents must read "
                "it from disk directly and emit an image ContentBlock."
            ) from exc
        return {"content": content}

    @staticmethod
    def _fs_write_text_file(params: dict) -> dict:
        """fs/write_text_file handler. Empty dict result on success (per the
        wire schema's WriteTextFileResponse); OSError propagates the same
        way _fs_read_text_file's does."""
        path = params["path"]
        content = params["content"]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return {}

    def start(self) -> None:
        """Clean-room gate, spawn/attach transport, initialize, resolve session."""
        assert_clean_room()
        self.transport = (
            self._external_transport
            if self._external_transport is not None
            else AcpTransport(self.wrapper_cmd)
        )
        self.transport.register_handler("fs/read_text_file", self._fs_read_text_file)
        self.transport.register_handler("fs/write_text_file", self._fs_write_text_file)
        self.transport.request(
            "initialize",
            {
                "protocolVersion": 1,
                # T-1831: advertised-implies-implemented is now a structural
                # invariant, not a hope. fs/read_text_file and
                # fs/write_text_file are registered above and routed by
                # AcpTransport._recv_until's request dispatch (see
                # _build_response_for_request) — the deadlock class behind
                # the T-1830 hotfix (advertise a cap, _recv_until silently
                # discards the wrapper's resulting request, turn hangs
                # forever; live-hit by mnemosyne msg-1) cannot recur for fs,
                # because there is no longer any code path that advertises a
                # cap without a handler backing it. tools/acp_bridge_wetrun.py's
                # fs-caps rung is the wetrun-gate that keeps this honest for
                # future caps: it reads caps back out of the actual
                # initialize params sent here (never a hardcoded copy) and
                # fails loud if an advertised-true cap is ever unexercisable.
                "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
            },
        )
        self.session_id = self._resolve_session()
        # T-2797: a freshly established session starts the turn count over.
        self._delivered_since_session = 0

    def _read_state(self):
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_state(self, session_id: str) -> None:
        """Atomic tmp-write + os.rename, mirroring mailbox.py's durability
        pattern. json.dumps(..., sort_keys=True) so the file is diff-stable."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.parent / f".{self.state_path.name}.tmp.{os.getpid()}"
        data = json.dumps({"sessionId": session_id}, sort_keys=True) + "\n"
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp_path, self.state_path)

    def _resolve_session(self) -> str:
        """Resume the stored session, or start a new one. EVERY outcome is logged
        distinctly (T-2341).

        ⚠️ WHY THE LOGGING IS THE POINT HERE. The bridge RESUMES by default and a new
        session id is the exception — measured across two seats, specialist kept the id on
        19 of 22 restarts and mnemosyne on 3 of 4, so an id change is the ~14% case and
        it is the lossy one. Yet "the id changed" is the only signal that survived, and
        it carries TWO OPPOSITE meanings depending on why you restarted: after
        un-wedging a stuck turn a fresh session is the outcome you wanted, while after a
        routine restart it means continuity was LOST. One observable, two readings.

        Worse, a changed id had THREE indistinguishable causes:
          (a) no stored sessionId at all (first run)      -> not a failure
          (b) session/load raised, silent fallback        -> CONTINUITY LOST
          (c) session/load SUCCEEDED but returned a new id -> continuity KEPT
        The fallback for (b) was logged at DEBUG with a comment saying it was
        "diagnosable at wet-run/pilot time" — accurate about the code and false about
        the deployed system: main() calls basicConfig(level=INFO), so DEBUG is a black
        hole in production. Measured on specialist's log: 3 id-change events, 0 fallback
        lines, 0 DEBUG lines of any kind. Three resumes failed and the log said nothing.

        So each branch now says which of the three it was, at a level that actually
        emits. Behaviour is unchanged — still swallow-and-fall-back; only the record
        changes, so a lost-continuity event is self-reporting instead of something a
        reader has to reconstruct by diffing the id against the state file.
        """
        state = self._read_state()
        existing_id = state.get("sessionId") if state else None
        if existing_id:
            try:
                result = self.transport.request(
                    "session/load",
                    {"sessionId": existing_id, "cwd": self.cwd, "mcpServers": []},
                )
                new_id = (
                    result.get("sessionId", existing_id)
                    if isinstance(result, dict)
                    else existing_id
                )
                if new_id != existing_id:
                    # Cause (c): the load SUCCEEDED, so continuity is intact even
                    # though the id moved. Logged because the id change is the
                    # ambiguous observable — without this line it is indistinguishable
                    # from the lossy case below.
                    logging.warning(
                        "session/load agent=%s RESUMED but the server returned a "
                        "DIFFERENT sessionId (%s -> %s); continuity KEPT — do not "
                        "read this id change as a lost session [T-2341]",
                        self.agent, existing_id, new_id,
                    )
                else:
                    logging.info(
                        "session/load agent=%s resumed sessionId=%s [T-2341]",
                        self.agent, existing_id,
                    )
                self._write_state(new_id)
                return new_id
            except Exception as exc:
                # Cause (b): ANY error from session/load -> fall back to session/new.
                # WARNING, not debug: this is the lossy branch and the whole reason
                # the id changes. The swallow-and-fallback behaviour is deliberately
                # unchanged — a bridge that refused to start because it could not
                # resume would be strictly worse than one that starts fresh and says so.
                logging.warning(
                    "session/load FAILED for agent=%s sessionId=%s: %r — starting a "
                    "NEW session; CONTINUITY LOST (this is what a changed session id "
                    "means here) [T-2341]",
                    self.agent, existing_id, exc,
                )
        else:
            # Cause (a): nothing to resume. Not a failure, and it must not be read as
            # one — first run, or a deliberately removed state file (the documented
            # way to force a clean session).
            logging.info(
                "session/load agent=%s: no stored sessionId — starting a new session "
                "(first run or state file removed); nothing was lost [T-2341]",
                self.agent,
            )
        result = self.transport.request("session/new", {"cwd": self.cwd, "mcpServers": []})
        new_id = result["sessionId"]
        self._write_state(new_id)
        return new_id

    # --------------------------------------------------------- the turn loop
    @staticmethod
    def _compose_prompt(msg: dict) -> str:
        """Compose the prompt text from the REAL mailbox record shape
        {id, sender, subject, ts, payload} (tools/mailbox.py send()) — not
        the spike's flat from/to/subject/body shape."""
        sender = msg.get("sender", "unknown")
        subject = msg.get("subject") or "(no subject)"
        payload = msg.get("payload")
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, sort_keys=True)
        else:
            body = str(payload)
        return f"[mailbox:{sender}] {subject}: {body}"

    # Bound the hint scan so a pathological queue can never make delivery
    # expensive. Past this we still report the count, just not the breakdown.
    _HINT_SCAN_CAP = 200

    def _queue_hint(self, msg: dict) -> str:
        """T-2248: tell the receiver what is queued BEHIND this message.

        The defect this closes is not queue depth, it is that turn-serialised
        delivery makes staleness invisible to the RECEIVER — nothing in a
        delivered message says "three later messages from this sender are already
        queued behind me", so a message and its own RETRACTION are
        indistinguishable on arrival. Mnemosyne measured the cost: she re-derived
        work a peer had already withdrawn, and once corrected a figure the sender
        had retracted 16 minutes earlier.

        Deliberately NOT faster delivery (her ticket rules that out: it narrows
        the window without closing it, and the operator vetoed batching outright).
        This converts an invisible hazard into a visible one at the cost of a
        readdir.

        Never raises: a hint is an optimisation, and failing to compute one must
        never cost a delivery. Any error yields an empty hint.
        """
        try:
            new = self.inbox / "new"
            names = sorted(p.name for p in new.iterdir() if p.is_file())
            me = msg.get("id")
            names = [n for n in names if n != me]
            if not names:
                return ""
            total = len(names)
            if total > self._HINT_SCAN_CAP:
                return (f"\n\n[mailbox:queue] {total} more messages are queued behind this one "
                        f"(too many to summarise). Treat anything time-sensitive in it as "
                        f"possibly superseded; drain before acting.")
            by_sender: dict[str, int] = {}
            superseded_by = None
            oldest_ns = None
            for n in names:
                try:
                    q = json.loads((new / n).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                s = q.get("sender", "unknown")
                by_sender[s] = by_sender.get(s, 0) + 1
                ts = q.get("ts_ns")
                if isinstance(ts, int) and (oldest_ns is None or ts < oldest_ns):
                    oldest_ns = ts
                if me is not None and q.get("supersedes") == me:
                    superseded_by = q.get("id")

            parts = []
            # The load-bearing line: a later message from the SAME sender is the
            # one that can invalidate what you are reading right now.
            same = by_sender.get(msg.get("sender", ""), 0)
            if superseded_by:
                parts.append(
                    f"!! THIS MESSAGE HAS ALREADY BEEN SUPERSEDED by a later one from the same "
                    f"sender ({superseded_by}), which is still queued. Do not act on this; "
                    f"drain first.")
            elif same:
                parts.append(
                    f"{same} later message(s) from {msg.get('sender')} are ALREADY QUEUED behind "
                    f"this one — this may be superseded or retracted. Prefer draining before "
                    f"acting on anything consequential from this sender.")
            breakdown = ", ".join(f"{s} x{c}" for s, c in
                                  sorted(by_sender.items(), key=lambda kv: -kv[1]))
            age = ""
            if oldest_ns:
                secs = max(0, int(time.time() - oldest_ns / 1e9))
                age = f"; oldest queued {secs // 3600}h{(secs % 3600) // 60:02d}m"
            parts.append(f"{total} queued behind this ({breakdown}){age}.")
            return "\n\n[mailbox:queue] " + " ".join(parts)
        except Exception:  # a hint must never cost a delivery
            return ""

    def handle_message(self, msg: dict) -> None:
        """The mailbox.consume() handler. Returning normally ACKs (moves the
        message to cur/); raising leaves it in new/ for retry. Called lazily
        via run_once() after start() has run.

        Fire-and-forget nudges (sender in _FIRE_AND_FORGET_SENDERS, e.g.
        keep_going) are ACKed on a session/prompt TIMEOUT: the turn is still
        running, which is exactly the un-park the nudge exists to cause, so it
        succeeded (T-1985). Genuine failures — a JSON-RPC AcpError, or a bare
        AcpTransportError from wrapper-exit — still raise for every sender.

        This ack-on-timeout is safe against prompt-stacking ONLY because two
        upstream gates guarantee a nudge never enters a busy seat: keep_going
        fires solely when the seat is idle-at-prompt, and run_once() defers the
        whole drain while the main pane is affirmatively busy (T-1935). Do not
        weaken either gate without revisiting this: acking-and-not-retrying a
        nudge depends on the seat being idle at send time, so a second prompt
        can never stack behind a turn the first nudge started."""
        # T-2038: if a prior message this drain already hit a usage cap, the
        # breaker is open — don't re-hammer the capped wrapper with session/prompt
        # for every remaining message. Fail transient (mailbox leaves it in new/,
        # uncounted); run_once() defers the next drain until the cooldown lapses.
        if time.monotonic() < self._capped_until:
            raise AcpCapError("session capped; circuit breaker open")

        prompt_text = self._compose_prompt(msg) + self._queue_hint(msg)
        fire_and_forget = msg.get("sender") in _FIRE_AND_FORGET_SENDERS
        try:
            result = self.transport.request(
                "session/prompt",
                {"sessionId": self.session_id, "prompt": [{"type": "text", "text": prompt_text}]},
                timeout=self.prompt_timeout,
            )
        except AcpTimeoutError:
            if fire_and_forget:
                # The turn is still running past prompt_timeout — for a nudge
                # that IS success (the seat un-parked). Ack; do not retry.
                return
            # A normal task turn you legitimately want to wait for and retry.
            raise
        except AcpError as exc:
            # T-2038: a usage-cap (-32603 "You've hit your limit · resets ...")
            # is ENVIRONMENTAL, not message poison — every message fails it the
            # same way. Trip the breaker (stop hammering) and raise a transient
            # error so mailbox._drain leaves the message in new/ WITHOUT counting
            # it toward DLQ. This applies to EVERY sender, including keep_going
            # (a capped nudge is not poison — it delivers on reset) and the
            # mailbox-dlq alert sender (whose quarantine-exemption is exactly
            # what let the old path loop forever). A non-cap AcpError still
            # propagates unchanged (recorded, retried, eventually quarantined).
            if _is_cap_error(exc):
                self._trip_cap_breaker(exc)
                raise AcpCapError(str(exc)) from exc
            raise
        stop_reason = result.get("stopReason") if isinstance(result, dict) else None
        if not stop_reason:
            # Defensive: per the ACP spec the session/prompt response only
            # arrives once the turn stops, so this should not occur in
            # practice. Treat it as a transport-shaped failure -> RAISE, so
            # the message is never silently acked on an ambiguous result.
            raise AcpTransportError(
                f"session/prompt returned no terminal stopReason: {result!r}"
            )

    # T-2352: the batch envelope. One header stating delivery is FIFO and a
    # later message may retract an earlier one (T-2286 decision 2), then each
    # message numbered [i/N] oldest-first via the same _compose_prompt the
    # per-message path uses. The full contents make _queue_hint redundant (the
    # receiver already sees the whole queue), so it is deliberately omitted here.
    _BATCH_HEADER = (
        "You have {n} queued mailbox messages, delivered together as ONE turn "
        "below. They are numbered [i/{n}] in FIFO order, OLDEST FIRST. Delivery "
        "is at-least-once and FIFO: a LATER message may correct, supersede, or "
        "retract an EARLIER one, so read all {n} before acting on any, then "
        "process them in order."
    )

    def handle_batch(self, msgs: list) -> None:
        """T-2352 batch handler for mailbox.consume(batch_handler=...). Deliver
        the whole claimed snapshot as ONE session/prompt turn.

        Return normally  -> mailbox acks ALL N (a clean turn consumed the batch;
                            at-least-once means a lost turn acks nothing and all
                            N redeliver — tolerated duplicate, not loss, per the
                            T-2286 decision that ack-all-on-one-success is safe).
        Raise            -> mailbox requeues ALL N without DLQ attribution; a
                            usage cap raises AcpCapError (mailbox_transient, the
                            breaker defers), every other failure raises so
                            run_once() latches per-message delivery for the next
                            pass and the poison is isolated there.

        Fire-and-forget nudge semantics are NOT reproduced here: a batch timeout
        raises (requeue all) and the per-message fallback applies the
        ack-on-timeout rule correctly on the next pass. Keeping that one subtle
        rule on the single-message path avoids a second place it can drift."""
        if time.monotonic() < self._capped_until:
            raise AcpCapError("session capped; circuit breaker open")
        if not msgs:  # nothing claimed — a clean no-op turn, ack nothing
            return
        n = len(msgs)
        parts = [self._BATCH_HEADER.format(n=n)]
        for i, msg in enumerate(msgs, 1):
            parts.append(f"[{i}/{n}] " + self._compose_prompt(msg))
        prompt_text = "\n\n".join(parts)
        try:
            result = self.transport.request(
                "session/prompt",
                {"sessionId": self.session_id, "prompt": [{"type": "text", "text": prompt_text}]},
                timeout=self.prompt_timeout,
            )
        except AcpError as exc:
            # Same cap carve-out as handle_message: a usage cap is environmental,
            # not batch poison — trip the breaker and raise transient so the whole
            # snapshot requeues uncounted and does NOT latch per-message delivery.
            if _is_cap_error(exc):
                self._trip_cap_breaker(exc)
                raise AcpCapError(str(exc)) from exc
            raise
        # AcpTimeoutError / AcpTransportError propagate unchanged -> requeue all.
        stop_reason = result.get("stopReason") if isinstance(result, dict) else None
        if not stop_reason:
            raise AcpTransportError(
                f"session/prompt returned no terminal stopReason: {result!r}"
            )

    # ------------------------------------------------------------ entrypoints
    def _trip_cap_breaker(self, exc: Exception) -> None:
        """Open the usage-cap circuit breaker (T-2038): defer drains so we stop
        hammering a capped wrapper. Logged at WARNING so the cap is visible in
        the bridge log (the failing session/prompt is otherwise only a
        per-message error line).

        T-2510: prefer the reset time embedded in the cap message ("resets
        5:10pm (UTC)") over the fixed cap_cooldown_secs. A subscription cap can
        last hours, and the old fixed-5-min cooldown re-probed session/prompt
        ~once per cooldown for the whole window — each probe appending another
        banner to the wrapper's session context (it accepts the prompt, THEN
        caps), so a long cap surfaced as dozens of duplicated banners in the one
        prompt that ran after reset. Deferring until the actual reset makes that
        a single probe. Falls back to the fixed cooldown when no reset time is
        parseable (wrapper reword). cap_cooldown_secs<=0 keeps the breaker OFF
        entirely (retry immediately) — the explicit test/opt-out contract — so
        reset-parsing never silently re-arms a disabled breaker."""
        if self.cap_cooldown_secs <= 0:
            self._capped_until = time.monotonic()   # breaker disabled: no defer
            logging.warning(
                "acp-bridge[%s] usage cap hit (%s) — breaker disabled "
                "(cap_cooldown_secs<=0); not deferring", self.agent, exc,
            )
            return
        reset_delay = (_parse_cap_reset_delay(exc.message)
                       if isinstance(exc, AcpError) else None)
        delay = reset_delay if reset_delay is not None else self.cap_cooldown_secs
        self._capped_until = time.monotonic() + delay
        logging.warning(
            "acp-bridge[%s] usage cap hit (%s) — deferring drains for %.0fs %s",
            self.agent, exc, delay,
            "until the stated reset (T-2510; one probe at reset, not per-cooldown)"
            if reset_delay is not None else "(fixed cooldown; no reset time parsed)",
        )

    def _main_pane_idle(self) -> bool | None:
        """Tri-state idle probe of the agent's main-session pane.

        True = affirmatively idle-at-prompt; False = affirmatively busy;
        None = unknown (no pane configured, capture failed, import failed).
        T-2259 split this out of _main_session_busy because the router needs
        the full tri-state: busy defers, IDLE can now re-route, unknown must
        do neither."""
        if not self.main_pane:
            return None
        try:
            import getpass
            pane_text = _keep_going.capture_pane(self.main_pane, getpass.getuser())
            if pane_text is None:
                return None
            return _keep_going.is_idle_at_prompt(pane_text)
        except Exception:
            return None

    def _main_seat_present(self) -> bool | None:
        """Tri-state liveness of the main seat's tmux SESSION (T-2348).

        True = the session exists (seat is there, whatever its pane is doing);
        False = provably absent (no such session / no tmux server);
        None = could not find out.

        Deliberately a DIFFERENT question from `_main_pane_idle`, and that
        difference is the whole point of this probe. mnemosyne's log has the one
        real instance: a degrading tmux client walked the pane from
        writable-and-readable -> unwritable-but-readable (98 consecutive wake
        failures, ~8 min) -> unreadable, and the drain fired on the unreadable
        step. The main seat was ALIVE throughout — same session id on both sides.
        So "pane unreadable" carried no information about whether a second writer
        would be created; session existence does.

        `has-session` is the right instrument rather than a process scan: it asks
        the same subsystem that failed, needs no grant for our own account, and is
        cheap enough for a 5s poll. Absent means the seat cannot be running there.
        """
        session = (self.main_pane or "").split(":", 1)[0]
        if not session:
            return None
        try:
            r = subprocess.run(
                ["bash", "-lc",
                 f"XDG_RUNTIME_DIR=/run/user/$(id -u) tmux has-session -t {session}"],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None                      # cannot ask -> UNKNOWN, never "absent"
        if r.returncode == 0:
            return True
        err = (r.stderr or "").lower()
        if "can't find session" in err or "no server running" in err:
            return False                     # definitive: nothing to collide with
        return None                          # some other tmux failure -> blind

    def _main_session_busy(self) -> bool:
        """True iff the agent's main-session pane is AFFIRMATIVELY busy.

        Fail-OPEN on inconclusive/error (None): a broken probe must degrade to
        the pre-T-1935 behavior, not silently stop the poke pipeline forever.
        Only an affirmative busy-signal defers the drain."""
        return self._main_pane_idle() is False

    def _wake_main_seat(self) -> bool:
        """T-2259: hand this drain to the agent's MAIN seat via mailbox_wake.

        Injects the standard drain-your-inbox kick into the main pane (the
        exact path every main-seat agent already receives mail by) and leaves
        every message untouched in new/ — the main seat acks by consuming, so
        at-least-once survives and nothing is delivered twice. mailbox_wake's
        own decide() carries the debounce/marker state, so a 5s poll loop
        calling this repeatedly does not spam the pane.

        Returns True if the wake path handled it (including debounced/empty);
        False means the wake could not be delivered and the caller should just
        retry next poll — never fall through to the ACP dispatch in the same
        pass, or a swallowed kick turns into a double delivery race."""
        try:
            try:
                from tools import mailbox_wake as _mw
            except ImportError:
                import mailbox_wake as _mw  # type: ignore
            return _mw.run(self.agent, inbox=self.inbox) == 0
        except Exception as exc:
            logging.warning("acp-bridge[%s]: main-seat wake failed (%r)", self.agent, exc)
            return False

    def run_once(self) -> dict:
        """Ensure the session is started, then drain one pass of the inbox.
        Returns mailbox.consume()'s result dict ({skipped, processed, errors}).
        This is Unit 1's clean entrypoint for a supervise-script loop.

        T-1935: while the main session's pane is affirmatively busy, the whole
        drain is DEFERRED (refuse+defer, not handler-raise — a defer must not
        burn the message's dead-letter attempt budget). Messages stay in new/
        untouched; the next poll re-checks.

        T-2038: likewise while the usage-cap breaker is open — nothing can be
        delivered to a capped seat, so draining would only re-hit the cap on
        every message. Defer until the cooldown lapses, then retry."""
        if time.monotonic() < self._capped_until:
            return {"skipped": True, "reason": "session_capped",
                    "processed": [], "errors": [], "quarantined": []}
        idle = self._main_pane_idle()
        if idle is False:
            return {"skipped": True, "reason": "main_session_busy",
                    "processed": [], "errors": [], "quarantined": []}
        # T-2259: mail was pinned to the ACP seat, so ONE long turn blocked
        # delivery for 83 minutes while the agent's main seat sat idle at the
        # prompt. With --prefer-main-seat, an AFFIRMATIVELY idle main pane takes
        # the drain instead: wake it with the standard drain-your-inbox kick and
        # leave the queue in new/ for it to consume (it acks by consuming). The
        # ACP dispatch remains the path for unknown/unreadable panes, so a
        # broken probe degrades to the old behavior, never to silence. Only
        # divert when there is actually mail — an empty queue needs no wake.
        if self.prefer_main_seat and idle is True and mailbox.exists(self.inbox) \
                and any((Path(self.inbox) / "new").iterdir()):
            ok = self._wake_main_seat()
            return {"skipped": True,
                    "reason": "routed_to_main_seat" if ok else "main_seat_wake_failed",
                    "processed": [], "errors": [], "quarantined": []}
        # T-2348: UNKNOWN is now its own branch. It used to fall through to the
        # ACP drain by omission — the docstring above said "busy defers, IDLE can
        # re-route, unknown must do NEITHER", and unknown drained. The dangerous
        # case was the DEFAULT case, which is the shape being fixed here more than
        # any particular outcome: `else` was carrying a decision nobody made.
        #
        # An unreadable pane does not tell us whether the main seat is there, so
        # ask the question that does. Only a PROVABLY ABSENT seat may drain
        # (option (c)) — that is the one case where no second writer can exist.
        #
        # When liveness is ITSELF unknown we drain anyway (option (b)) rather than
        # defer, but deliberately and loudly. Two-sided reasoning, both directions
        # having a real cost: deferring on double-unknown risks silence, and the
        # wake-failure branch already returns without draining, so unbounded
        # non-delivery is a demonstrated failure mode here (~8 min, unalarmed),
        # not a hypothetical. Meanwhile T-2287 narrowed the consumer lease to a
        # claim step, so a second executor can no longer duplicate a DELIVERY —
        # the residual cost is shared mutable state (T-2335/T-2336), not lost
        # mail. Given that asymmetry, silence is the worse failure. This is
        # emphatically NOT "cannot tell means absent": absent is one branch and
        # blind is another, and they are logged apart so the split-brain window is
        # observable instead of invisible.
        if self.prefer_main_seat and idle is None and mailbox.exists(self.inbox) \
                and any((Path(self.inbox) / "new").iterdir()):
            seat = self._main_seat_present()
            if seat is True:
                logging.warning(
                    "acp-bridge[%s]: main pane UNREADABLE but its tmux session is "
                    "PRESENT — deferring the drain rather than becoming a second "
                    "writer (T-2348); messages stay in new/ for retry", self.agent)
                return {"skipped": True, "reason": "main_pane_unknown_seat_present",
                        "processed": [], "errors": [], "quarantined": []}
            if seat is None:
                logging.warning(
                    "acp-bridge[%s]: main pane unreadable AND seat liveness "
                    "UNKNOWN — draining via ACP anyway to avoid silence, but this "
                    "is the split-brain window (T-2348)", self.agent)
                self._unknown_seat_drains += 1
            else:
                logging.info(
                    "acp-bridge[%s]: main pane unreadable and its tmux session is "
                    "provably ABSENT — ACP drain is the only seat (T-2348)",
                    self.agent)
        if self.transport is None:
            self.start()
        # T-2352: batch the happy path, degrade to per-message on failure. The
        # batch path runs only when enabled AND not currently latched into the
        # per-message fallback; otherwise this is exactly the pre-T-2352 call.
        if self.batch and not self._deliver_individually:
            capped_before = self._capped_until
            result = mailbox.consume(
                self.inbox, batch_handler=self.handle_batch,
                ttl_secs=self.consume_ttl_secs,
                label=f"acp-bridge:{self.agent}",
            )
            # Latch per-message delivery only on a NON-cap batch failure. A cap
            # newly tripped the breaker (capped_until advanced), and a cap is
            # environmental not poison — per-message would not attribute anything,
            # and the breaker already defers the next drain until reset. Any other
            # error means a message in the batch may be poison, so the next pass
            # goes per-message to isolate it.
            if result.get("errors") and self._capped_until == capped_before:
                self._deliver_individually = True
                logging.info(
                    "acp-bridge[%s]: batch turn failed (non-cap) — next drain "
                    "delivers per-message to isolate the offender (T-2352)",
                    self.agent,
                )
        else:
            result = mailbox.consume(
                self.inbox, self.handle_message,
                ttl_secs=self.consume_ttl_secs,
                label=f"acp-bridge:{self.agent}",
            )
            # Resume batching once a per-message pass completes with no failures:
            # the poison has acked, quarantined, or drained, so the queue is clean
            # and the batch optimisation is safe again. A pass that still has
            # errors keeps the latch set so isolation continues until it clears.
            if self.batch and self._deliver_individually \
                    and not result.get("skipped") and not result.get("errors"):
                self._deliver_individually = False
                logging.info(
                    "acp-bridge[%s]: per-message pass clean — resuming batched "
                    "delivery (T-2352)", self.agent,
                )
        if result.get("reason") == "inbox_missing":
            # T-2216: a missing inbox is a standing misconfiguration (a
            # mistyped or vanished hub path), not a transient lease contest —
            # it must never be invisible in an unattended loop. main() already
            # surfaces this via its per-drain JSON status line, but serve()
            # (Unit 2's other sanctioned caller, see below) discards
            # run_once()'s return value entirely. Logged HERE, at the single
            # choke point both callers go through, so neither can spin
            # silently forever.
            logging.warning(
                "acp-bridge[%s]: inbox %s does not exist — mailbox "
                "misconfigured, not a transient condition", self.agent, self.inbox,
            )
        # T-2797: count delivered turns, then proactively recycle if due + idle.
        self._delivered_since_session += len(result.get("processed") or [])
        self._maybe_recycle(result)
        return result

    def _maybe_recycle(self, result: dict) -> bool:
        """T-2797: recycle the wrapper session BEFORE it accumulates enough
        context to hit ACP -32603 'Prompt is too long'. Recycle = close the
        transport + delete the persisted state file, so the NEXT run_once()'s
        start() finds no sessionId and takes the session/new (fresh empty
        context) path — reusing the exact 'stop + rm state + start' shape the
        manual recovery SOP uses, in-process.

        Fires ONLY when ALL hold, so no message is ever dropped mid-delivery and
        a transient cap is never mistaken for a context wedge:
          - recycle_after_turns is set (opt-in; OFF by default);
          - a session is actually established (transport up);
          - the usage-cap breaker is NOT open (a cap is transient per T-2038 —
            recycling would just burn the fresh session; wait for reset instead);
          - _delivered_since_session has crossed the threshold;
          - THIS drain was idle: it delivered nothing, errored on nothing, was
            not a deferral/skip, and the inbox new/ is now empty.
        Returns True iff a recycle happened. Logged at INFO (greppable) as a
        DELIBERATE continuity drop, distinct from the lossy session/load fallback
        (T-2341): the ACP session's conversational context is disposable; the
        agent's committed work + memory-dir captures are untouched.
        """
        if self.recycle_after_turns is None:
            return False
        if self.transport is None:
            return False
        if time.monotonic() < self._capped_until:
            return False
        if self._delivered_since_session < self.recycle_after_turns:
            return False
        # Idle gate: never recycle while anything is pending or just moved.
        if result.get("skipped") or result.get("processed") or result.get("errors"):
            return False
        try:
            if mailbox.exists(self.inbox) and any((Path(self.inbox) / "new").iterdir()):
                return False
        except OSError:
            return False
        old_id = self.session_id
        turns = self._delivered_since_session
        try:
            self.close()
        except Exception as exc:  # never let a teardown hiccup wedge the loop
            logging.warning(
                "acp-bridge[%s]: recycle close() raised %r — continuing to reset "
                "state anyway (T-2797)", self.agent, exc,
            )
        self.transport = None
        self.session_id = None
        try:
            self.state_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            # If the state file can't be removed, a restart would RESUME the wedged
            # session — so refuse to claim a recycle. The next start() will re-load
            # the old id; the seat is no worse off than before this pass.
            logging.warning(
                "acp-bridge[%s]: recycle could not remove state file %s: %r — "
                "NOT recycled (a restart would resume the old session) [T-2797]",
                self.agent, self.state_path, exc,
            )
            return False
        self._delivered_since_session = 0
        logging.info(
            "acp-bridge[%s]: proactively RECYCLED session %s after %d delivered "
            "turns (idle drain) — next drain starts fresh via session/new; "
            "conversational context intentionally dropped, committed work + "
            "memory intact [T-2797]", self.agent, old_id, turns,
        )
        return True

    def serve(self, poll_interval: float = 5.0, max_iterations=None) -> None:
        """Loop run_once() with a sleep between drains. Unit 2's supervise
        script calls this (or drives run_once() in its own loop — either
        composes)."""
        i = 0
        while max_iterations is None or i < max_iterations:
            self.run_once()
            i += 1
            if max_iterations is None or i < max_iterations:
                time.sleep(poll_interval)

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()


# --------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    """Production serve-CLI launcher (Unit 2). Long-lived: drives its own
    run_once()-loop forever (or bounded by --max-iterations for tests/debug),
    printing one JSON status line per drain so a supervise script's tmux
    pane / tee'd log carries visibility into consume() activity.

    This is the ONLY sanctioned production entrypoint. It does NOT replace
    tools/acp_bridge_wetrun.py, which stays the separate HUMAN empirical
    gate against the real wrapper (session/load verification, etc) and is
    never invoked by a supervised/unattended process.

    Not `serve()` verbatim: serve() is silent (no per-iteration output),
    which would leave a supervising tmux pane blank. This engine does not
    expose raw session/update wire traffic (see module docstring), so the
    per-drain {skipped, processed, errors} summary is the closest available
    operator signal without reopening Unit-1 transport internals.
    """
    parser = argparse.ArgumentParser(
        description="Production serve-loop launcher for AcpBridge (Unit 2, ORG-031)."
    )
    parser.add_argument("--agent", required=True, help="agent name (state file + logging label)")
    parser.add_argument("--cwd", required=True, help="the agent's REAL working directory")
    parser.add_argument("--inbox", required=True, help="mailbox inbox root dir (tools/mailbox.py layout)")
    parser.add_argument("--wrapper-cmd", nargs="+", default=None,
                         help="override the wrapper command (default: ['claude-code-acp'])")
    parser.add_argument("--state-path", default=None,
                         help="override the session-state file path")
    parser.add_argument("--prompt-timeout", type=float, default=300.0)
    parser.add_argument("--consume-ttl", type=int,
                        default=mailbox._DEFAULT_CONSUME_TTL_SECS,
                         help="mailbox consumer-lease TTL seconds (T-1935). A deep "
                              "multi-message drain is bounded by prompt_timeout x "
                              "batch_depth, which must stay under this or a second "
                              "bridge could reap the lease mid-drain. Raise in lockstep "
                              "if --prompt-timeout goes higher (T-1990).")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--cap-cooldown", type=float, default=300.0,
                        help="seconds to defer drains after a usage-cap error "
                             "(T-2038 circuit breaker). Also bounds recovery "
                             "latency after the cap resets.")
    parser.add_argument("--prefer-main-seat", action="store_true",
                         help="T-2259: when the main pane is affirmatively idle "
                              "and mail is queued, wake the MAIN seat to drain "
                              "(mailbox_wake kick; messages stay in new/ and the "
                              "seat acks by consuming) instead of paying one ACP "
                              "turn per message. Requires --main-pane.")
    parser.add_argument("--main-pane", default=None,
                         help="agent's live main-session tmux pane (e.g. "
                              "claude-research-bot:0); when set, drains are "
                              "deferred while it is affirmatively busy (T-1935)")
    parser.add_argument("--batch", action="store_true",
                         help="T-2352: deliver each drain snapshot as ONE prompt "
                              "(numbered [i/N] FIFO envelope) instead of one turn "
                              "per message. Failure degrades to per-message "
                              "delivery automatically, so isolation + DLQ "
                              "attribution are preserved; a poison message can "
                              "never wedge the inbox.")
    parser.add_argument("--recycle-after-turns", type=int, default=None,
                         help="T-2797: proactively recycle the wrapper session "
                              "(close + delete state file -> session/new) once this "
                              "many messages have been delivered on the current "
                              "session AND the drain is idle, to stay under the ACP "
                              "-32603 'Prompt is too long' ceiling. Counts delivered "
                              "turns, not poll iterations. Omit = OFF (default). "
                              "Calibrate the value via acp_bridge_wetrun.py before "
                              "enabling a live seat; enable specialist first (heaviest).")
    parser.add_argument("--max-iterations", type=int, default=None,
                         help="bound the loop (tests/debug only; production omits this)")
    args = parser.parse_args(argv)

    # Configure the root logger so acp_bridge's logging.warning/debug calls
    # actually emit (T-2038: they were previously black holes — no basicConfig,
    # so the session/load fallback + cap warnings went nowhere). stderr is
    # captured by the supervise script's `2>&1 | tee` into the bridge log.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    bridge = AcpBridge(
        agent=args.agent,
        cwd=args.cwd,
        inbox=args.inbox,
        wrapper_cmd=args.wrapper_cmd,
        state_path=args.state_path,
        prompt_timeout=args.prompt_timeout,
        main_pane=args.main_pane,
        prefer_main_seat=args.prefer_main_seat,
        consume_ttl_secs=args.consume_ttl,
        cap_cooldown_secs=args.cap_cooldown,
        batch=args.batch,
        recycle_after_turns=args.recycle_after_turns,
    )
    i = 0
    try:
        while args.max_iterations is None or i < args.max_iterations:
            result = bridge.run_once()
            status = {"iteration": i, "agent": args.agent, "session_id": bridge.session_id}
            status.update(result)
            print(json.dumps(status, sort_keys=True), flush=True)
            # T-2038 visibility: surface any failures at WARNING (timestamped,
            # greppable) rather than only inside the per-drain status JSON — the
            # gap that let the original loop stay invisible to a journalctl look.
            if result.get("errors") or result.get("quarantined"):
                logging.warning(
                    "acp-bridge[%s] drain %d: errors=%s quarantined=%s",
                    args.agent, i, result.get("errors"), result.get("quarantined"),
                )
            i += 1
            if args.max_iterations is None or i < args.max_iterations:
                time.sleep(args.poll_interval)
    finally:
        bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
