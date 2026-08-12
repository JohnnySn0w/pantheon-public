"""tools/bringup_acceptance/telegram_outbound.py — telegram-outbound acceptance module.

T-2556 phase 10 / T-2568. Proves the Telegram Bot API outbound send-accept
CONTRACT (sendMessage returns a message_id, no API error, chat_id matches the
intended recipient) for both unthreaded and threaded (``reply_to``) sends,
and ships four first-class, named, tested failure-mode guards for the real
"sent-receipt-but-not-delivered" causes the operator reported: wrong chat_id,
bot-lock contention, single-poller violation, and MCP-tool eviction.

SEND-ISOLATION (T-2568's own crux, ratified by team-lead 2026-08-05): every
proof in this module runs through an injectable :data:`SendFn` seam (mirrors
``producer_fires.FireHarness`` / ``restore.CommandRunner``) — the automated
suite NEVER makes a real network call. This module therefore proves the
outbound send-accept CONTRACT plus failure-mode CLASSIFICATION, not that
Telegram genuinely accepted a real message to a real chat — that would need
a dedicated non-operator test chat_id, which does not exist yet
(``inventory.py``'s own ``TelegramObservation.dedicated_test_chat`` is
None-until-confirmed; the infra to stand one up is filed as T-2582, parent
T-2556). :meth:`TelegramOutboundModule.report` names this explicitly as a
gap in its summary, the same shape ``mailbox_roundtrip`` names its own
T-2570 live-delivery gap rather than implying a composite it hasn't proven.
A constructor-time :class:`~tools.bringup_acceptance.types.
HardStop` refuses the real operator chat_id (mirrors ``inbox_consumer.
OPERATOR_CHAT_ID``) as a belt-and-suspenders guard against ever wiring this at
a real chat, mirroring ``mailbox_roundtrip``'s ``_REAL_HUB_DEFAULT`` refusal.

FAILURE-MODE GUARD SCOPE (each named row states what it actually proves —
ratified by team-lead: every guard must say whether it is a scoped
reproduction or a live-endpoint test, never silently imply more):

* ``wrong_chat_id`` — ground-truth clean: :func:`assert_chat_id_matches`
  compares the crafted response's echoed ``chat_id`` against the intended
  recipient on every real send-accept call in this run.
* ``bot_lock_contention`` — reproduces ``channel_liveness_monitor``'s own
  409-death signature (bot.pid present, PID dead) via a REAL local pid-file
  read plus an independent pure classifier (:func:`classify_bot_lock`) —
  deliberately NOT importing ``channel_liveness_monitor.probe_bridge``,
  which is sudo/cross-account-coupled and not cleanly unit-testable; this is
  the same "clean generalization, not shared-private-helper coupling"
  precedent ``recorder.py``'s own docstring claims for its T-2382 sibling.
* ``single_poller_violation`` — an in-memory two-poller RACE
  (:func:`prove_single_poller_guard`) proving the second contender gets a
  409-shaped conflict — NOT a real ``getUpdates`` hit.
* ``mcp_tool_eviction`` — CLASSIFIER-ONLY. A standalone acceptance script
  cannot observe a live session's MCP tool registry, so this proves that
  :func:`classify_send_error` correctly names a crafted 401/unauthorized-
  shaped fault as ``mcp_tool_eviction`` rather than folding it into a
  generic failure. Live detection, and any composition with
  ``hook_emissions``'s preventative ``mcp-list-guard.py`` proof, is a
  phase-15 wire-point — deliberately NOT hard-cross-referenced here (mirrors
  ``producer_fires.reconcile_wake_trigger``'s own "ship the field, wire it
  in phase 15" precedent: no orchestrator exists yet to compose modules).
"""

from __future__ import annotations

import dataclasses
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from tools.bringup_acceptance.evidence import RunPaths, write_module_evidence
from tools.bringup_acceptance.recorder import TraceRecorder
from tools.bringup_acceptance.types import (
    HardStop,
    MatrixRow,
    ModuleDeclaration,
    ModuleEvidence,
    RestoreEvidence,
    RunContext,
    TargetInventory,
    Tier,
)

MODULE_ID = "telegram-outbound"

# The real operator chat — this module must NEVER be wired at it. A module-
# level literal (not an import of tools.inbox_consumer.OPERATOR_CHAT_ID) so a
# change to that module's constant does not silently widen or narrow this
# guard — same posture mailbox_roundtrip's own docstring gives for its
# _REAL_HUB_DEFAULT constant.
_REAL_OPERATOR_CHAT_ID = "456474197"

# classify_send_error()'s four named failure-mode kinds.
WRONG_CHAT_ID = "wrong_chat_id"
BOT_LOCK_CONTENTION = "bot_lock_contention"
SINGLE_POLLER_VIOLATION = "single_poller_violation"
MCP_TOOL_EVICTION = "mcp_tool_eviction"
UNKNOWN_FAILURE = "unknown"


# ---------------------------------------------------------------------------
# Injectable send seam — mirrors producer_fires.FireHarness / restore.CommandRunner.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SendRequest:
    """One sendMessage attempt's intent."""

    chat_id: str
    text: str
    reply_to_message_id: str | None = None


@dataclasses.dataclass(frozen=True)
class SendResult:
    """One sendMessage attempt's ground truth, as the injected SendFn reports it.

    ``error_kind`` is populated only when ``ok`` is False, and is the RAW
    signal the sender (fake or live) observed — an HTTP-status-shaped token,
    a network exception, whatever ground truth it has. :func:`classify_send_error`
    is the single place that turns that raw signal into one of the four named
    failure-mode kinds, so the mapping is tested once, not re-derived per test.
    """

    ok: bool
    message_id: str | None
    chat_id: str | None
    reply_to_message_id: str | None
    error_kind: str | None
    raw: Mapping[str, Any]


SendFn = Callable[[SendRequest], SendResult]


def make_live_send_fn(token: str) -> SendFn:
    """Real Bot API sendMessage wiring — mirrors ``inbox_consumer.
    _send_telegram_alert``'s exact request shape. NOT exercised by the test
    suite (SEND-ISOLATION; see module docstring) — production glue only,
    same precedent as ``producer_fires.make_live_fire_harness``.
    """

    def _send(request: SendRequest) -> SendResult:
        import json
        import urllib.error
        import urllib.request

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body: dict[str, Any] = {"chat_id": request.chat_id, "text": request.text}
        if request.reply_to_message_id is not None:
            body["reply_to_message_id"] = request.reply_to_message_id
        http_request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=10) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                raw = json.loads(exc.read().decode("utf-8"))
            except ValueError:
                raw = {}
            error_kind = f"http_{exc.code}"
            if exc.code == 401:
                error_kind = "http_401_unauthorized"
            elif exc.code == 409:
                error_kind = "http_409_conflict"
            return SendResult(
                ok=False, message_id=None, chat_id=None, reply_to_message_id=None,
                error_kind=error_kind, raw=raw,
            )
        except (urllib.error.URLError, OSError) as exc:
            return SendResult(
                ok=False, message_id=None, chat_id=None, reply_to_message_id=None,
                error_kind="network_error", raw={"error": str(exc)},
            )
        result = raw.get("result") or {}
        chat = result.get("chat") or {}
        reply_to_message = result.get("reply_to_message") or {}
        ok = bool(raw.get("ok"))
        return SendResult(
            ok=ok,
            message_id=str(result["message_id"]) if "message_id" in result else None,
            chat_id=str(chat["id"]) if "id" in chat else None,
            reply_to_message_id=(
                str(reply_to_message["message_id"]) if "message_id" in reply_to_message else None
            ),
            error_kind=None if ok else "api_error",
            raw=raw,
        )

    return _send


def _default_process_alive(pid: int) -> bool:
    """Real ``os.kill(pid, 0)`` liveness probe — production default for
    ``TelegramOutboundModule``'s ``process_alive`` seam. Tests inject a fake."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not owned by us — still alive
    return True


def _nonce(label: str) -> str:
    return f"t2568-{label}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# wrong_chat_id guard — inline on every real send-accept call.
# ---------------------------------------------------------------------------


def assert_chat_id_matches(*, result: SendResult, intended_chat_id: str) -> None:
    """HardStop if the crafted response's echoed chat_id != the intended recipient.

    This is the module's ``wrong_chat_id`` guard: any 200/ok response is
    insufficient proof of ACCEPT on its own — the ticket's own acceptance
    criterion is literally ``chat_id == intended recipient``.
    """
    if result.chat_id != intended_chat_id:
        raise HardStop(
            f"{WRONG_CHAT_ID}: sendMessage response chat_id {result.chat_id!r} does not "
            f"match the intended recipient {intended_chat_id!r} (raw={result.raw!r})"
        )


# ---------------------------------------------------------------------------
# bot_lock_contention guard — real local pid-file read + pure classifier.
# ---------------------------------------------------------------------------


def classify_bot_lock(*, pid_text: str | None, process_alive: Callable[[int], bool]) -> str:
    """Classify a local bot.pid read: "absent" | "unreadable" | "alive" | "locked_dead".

    Mirrors ``channel_liveness_monitor``'s own 409-death signature (pidfile
    present, PID dead) — independent pure reimplementation, see module
    docstring for why this does not import ``probe_bridge``.
    """
    if pid_text is None or not pid_text.strip():
        return "absent"
    token = pid_text.strip().split()[0]
    if not token.isdigit():
        return "unreadable"
    return "alive" if process_alive(int(token)) else "locked_dead"


# ---------------------------------------------------------------------------
# single_poller_violation guard — in-memory two-poller race.
# ---------------------------------------------------------------------------


class _PollerLockRegistry:
    """Tiny in-memory stand-in for Telegram's server-side single-active-
    getUpdates-poller constraint (real signature: a second concurrent
    long-poll on the same token gets HTTP 409 Conflict, and the LOSING
    poller's process typically dies — channel_liveness_monitor's own
    "409-death signature"). NOT a real network call.
    """

    def __init__(self) -> None:
        self._holder: str | None = None

    def acquire(self, poller_id: str) -> None:
        if self._holder is not None and self._holder != poller_id:
            raise HardStop(
                f"{SINGLE_POLLER_VIOLATION}: poller {poller_id!r} conflicts with the "
                f"active poller {self._holder!r} (409)"
            )
        self._holder = poller_id

    def release(self, poller_id: str) -> None:
        if self._holder == poller_id:
            self._holder = None


def prove_single_poller_guard() -> Mapping[str, Any]:
    """Reproduce the single-poller 409 conflict as a deterministic in-memory
    race: a second concurrent poller against the same registry MUST be
    refused. HardStops (naming the guard itself, not a Telegram symptom) if
    the reproduction fails to conflict — that would mean the guard's own
    mechanics are broken, not that Telegram behaved unexpectedly.
    """
    registry = _PollerLockRegistry()
    registry.acquire("poller-a")
    try:
        registry.acquire("poller-b")
    except HardStop as exc:
        conflict = str(exc)
    else:
        raise HardStop(
            "single-poller-violation guard did not reproduce a 409 conflict for a "
            "second concurrent poller — guard mechanics broken"
        )
    finally:
        registry.release("poller-a")
    return {
        "second_poller_conflict": conflict,
        "scope": (
            "in-memory race reproducing the 409-conflict SHAPE — not a real getUpdates "
            "hit. Independence: this guard never touches the SendFn seam, so a "
            "single-poller violation cannot contaminate this module's own send-accept proof."
        ),
    }


# ---------------------------------------------------------------------------
# mcp_tool_eviction guard — classifier-only (see module docstring scope note).
# ---------------------------------------------------------------------------


def classify_send_error(result: SendResult) -> str:
    """Map a raw SendResult failure into one of the four named failure-mode kinds.

    ``error_kind`` on the SendResult is the RAW signal the sender observed;
    this is the single place raw signal becomes a named cause, so the
    mapping is tested once here rather than re-derived per failure-mode test.
    """
    if result.ok:
        raise HardStop("classify_send_error called on an ok=True SendResult")
    raw_kind = (result.error_kind or "").strip()
    if raw_kind in ("http_401_unauthorized", "unauthorized"):
        return MCP_TOOL_EVICTION
    if raw_kind in ("http_409_conflict", "conflict"):
        return SINGLE_POLLER_VIOLATION
    if raw_kind == "bot_lock_dead_pid":
        return BOT_LOCK_CONTENTION
    return UNKNOWN_FAILURE


_SYNTHETIC_EVICTION_FAULT = SendResult(
    ok=False, message_id=None, chat_id=None, reply_to_message_id=None,
    error_kind="http_401_unauthorized",
    raw={"synthetic": True, "note": "crafted 401 fixture — classifier self-check, not a live signal"},
)


def prove_mcp_tool_eviction_classifier_guard() -> Mapping[str, Any]:
    """CLASSIFIER-ONLY self-check: a crafted 401/unauthorized-shaped fault must
    classify as ``mcp_tool_eviction``. See module docstring for why live
    eviction DETECTION is out of scope for a standalone acceptance script.
    """
    classified = classify_send_error(_SYNTHETIC_EVICTION_FAULT)
    if classified != MCP_TOOL_EVICTION:
        raise HardStop(
            f"mcp-tool-eviction classifier guard: expected {MCP_TOOL_EVICTION!r} for a "
            f"synthetic 401 fixture, got {classified!r}"
        )
    return {
        "classified_as": classified,
        "scope": (
            "CLASSIFIER-ONLY guard: proves classify_send_error names a 401/unauthorized-"
            "shaped fault as mcp_tool_eviction rather than folding it into a generic "
            "failure. A standalone acceptance script cannot observe a live session's MCP "
            "tool registry, so live eviction DETECTION — and any composition with "
            "hook_emissions' preventative mcp-list-guard proof — is a phase-15 wire-point, "
            "deliberately NOT hard-cross-referenced here."
        ),
    }


# ---------------------------------------------------------------------------
# The AcceptanceModule
# ---------------------------------------------------------------------------


class TelegramOutboundModule:
    id = MODULE_ID
    tier: Tier = "core"

    def __init__(
        self,
        *,
        send: SendFn,
        chat_id: str,
        bot_pid_path: Path,
        process_alive: Callable[[int], bool] = _default_process_alive,
    ) -> None:
        if chat_id == _REAL_OPERATOR_CHAT_ID:
            raise HardStop(
                f"refusing to prove {MODULE_ID} against the REAL operator chat "
                f"{_REAL_OPERATOR_CHAT_ID} — chat_id must be a disposable/non-operator target"
            )
        self._send = send
        self._chat_id = chat_id
        self._bot_pid_path = bot_pid_path
        self._process_alive = process_alive

    def declare(self, target: TargetInventory) -> ModuleDeclaration:
        entries = [entry for entry in target.entries if entry.module_id == self.id]
        applies = any(entry.status == "applies" for entry in entries)
        if applies:
            reason = f"inventory declares {MODULE_ID} applies for persona {target.persona}"
        elif entries:
            reason = f"inventory marks {MODULE_ID} absent for persona {target.persona}"
        else:
            reason = f"no {MODULE_ID} inventory entry for persona {target.persona}"
        return ModuleDeclaration(module_id=self.id, tier=self.tier, applies=applies, reason=reason)

    def _prove_send(self, recorder: TraceRecorder, *, reply_to: str | None) -> Mapping[str, Any]:
        label = "threaded" if reply_to is not None else "unthreaded"
        nonce = _nonce(label)
        request = SendRequest(
            chat_id=self._chat_id, text=f"T-2568 send-accept probe {nonce}", reply_to_message_id=reply_to,
        )
        recorder.append(f"send-{label}", {"chat_id": request.chat_id, "reply_to": reply_to, "nonce": nonce})
        result = self._send(request)
        if not result.ok:
            raise HardStop(
                f"{label} send-accept: sendMessage did not report ok "
                f"(error_kind={result.error_kind!r} raw={result.raw!r})"
            )
        if not result.message_id:
            raise HardStop(
                f"{label} send-accept: sendMessage accepted but returned no message_id "
                f"(raw={result.raw!r})"
            )
        assert_chat_id_matches(result=result, intended_chat_id=self._chat_id)
        if reply_to is not None and result.reply_to_message_id != reply_to:
            raise HardStop(
                f"threaded send-accept: expected reply_to_message_id {reply_to!r} echoed "
                f"back, got {result.reply_to_message_id!r} (raw={result.raw!r})"
            )
        recorder.append(
            f"send-{label}-verified",
            {"message_id": result.message_id, "chat_id": result.chat_id},
        )
        return {
            "nonce": nonce,
            "message_id": result.message_id,
            "chat_id": result.chat_id,
            "reply_to_message_id": result.reply_to_message_id,
            "scope": (
                "proven via the injected SendFn seam — send-accept CONTRACT + "
                "classification, NOT a live Telegram round-trip (see module docstring "
                "SEND-ISOLATION; real-accept deferred — needs a dedicated non-operator "
                "test chat_id, see T-2582)"
            ),
        }

    def prove(self, ctx: RunContext) -> ModuleEvidence:
        recorder = TraceRecorder(ctx.evidence_root, module_id=self.id, run_id=ctx.run_id)
        paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)
        detail: dict[str, Any] = {}
        evidence_paths = []

        # Bot-lock-contention guard FIRST — real local ground truth, cheap,
        # and a locked-dead bridge makes the rest of this run's proof
        # questionable even though the SendFn seam is decoupled from it.
        pid_text = self._bot_pid_path.read_text(encoding="utf-8") if self._bot_pid_path.is_file() else None
        lock_status = classify_bot_lock(pid_text=pid_text, process_alive=self._process_alive)
        recorder.append(
            "bot-lock-contention-guard", {"pid_path": str(self._bot_pid_path), "status": lock_status},
        )
        bot_lock_detail = {
            "status": lock_status,
            "pid_path": str(self._bot_pid_path),
            "scope": (
                "reproduces channel_liveness_monitor's own 409-death signature (bot.pid "
                "present, PID dead) via a real local pid-file read + an independent pure "
                "classifier — not channel_liveness_monitor.probe_bridge"
            ),
        }
        detail["bot_lock_contention_guard"] = bot_lock_detail
        evidence_paths.append(
            write_module_evidence(paths, self.id, "bot-lock-contention-guard", bot_lock_detail)
        )
        if lock_status == "locked_dead":
            raise HardStop(
                f"{BOT_LOCK_CONTENTION}: bot.pid at {self._bot_pid_path} names a dead PID "
                "(409-death signature) — refusing to claim send-accept against a locked-dead bridge"
            )

        unthreaded = self._prove_send(recorder, reply_to=None)
        detail["unthreaded"] = unthreaded
        evidence_paths.append(write_module_evidence(paths, self.id, "unthreaded", unthreaded))

        threaded = self._prove_send(recorder, reply_to=_nonce("reply-to-target"))
        detail["threaded"] = threaded
        evidence_paths.append(write_module_evidence(paths, self.id, "threaded", threaded))

        poller_detail = prove_single_poller_guard()
        recorder.append("single-poller-violation-guard", poller_detail)
        detail["single_poller_violation_guard"] = poller_detail
        evidence_paths.append(
            write_module_evidence(paths, self.id, "single-poller-violation-guard", poller_detail)
        )

        mcp_detail = prove_mcp_tool_eviction_classifier_guard()
        recorder.append("mcp-tool-eviction-guard", mcp_detail)
        detail["mcp_tool_eviction_guard"] = mcp_detail
        evidence_paths.append(
            write_module_evidence(paths, self.id, "mcp-tool-eviction-guard", mcp_detail)
        )

        recorder.append("verdict", {"status": "proven"})
        return ModuleEvidence(
            module_id=self.id, status="proven", trace_path=recorder.path,
            evidence_paths=tuple(evidence_paths), detail=detail,
        )

    def restore(self, ctx: RunContext) -> RestoreEvidence:
        return RestoreEvidence(
            module_id=self.id, restored=True,
            detail={
                "note": (
                    "read-only local bot.pid check + sends via the injected SendFn seam — "
                    "no real-state mutation, no restore.py Baseline needed"
                ),
            },
        )

    def report(self, evidence: ModuleEvidence) -> MatrixRow:
        summary = (
            f"{MODULE_ID}: unthreaded+threaded send-accept CONTRACT proven via an injected "
            "SendFn seam — NOT a live Telegram round-trip. NAMED GAP: real-accept deferred "
            "— needs a dedicated non-operator test chat_id, see T-2582. wrong-chat-id guard "
            "is inline on every send-accept call; bot-lock-contention proven against real "
            "local ground truth; single-poller-violation proven via a deterministic "
            "in-memory race, not a real getUpdates hit; mcp-tool-eviction guard proves "
            "CLASSIFICATION only (live detection deferred to phase-15)."
        )
        return MatrixRow(
            module_id=self.id, tier=self.tier, status=evidence.status,
            summary=summary, evidence_refs=evidence.evidence_paths,
        )
