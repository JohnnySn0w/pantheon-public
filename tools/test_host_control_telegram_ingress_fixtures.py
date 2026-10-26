"""Shared fixtures for the T-2919 Telegram human-approval ingress tests.

NOT a test module: it holds zero test cases (pytest collection here is a
deliberate no-op; named ``test_*`` to sit beside its consumers, the
``tools/test_host_control_approval_dbus_driver.py`` precedent).

Contents:

- ``FakeBotApi`` — a LOCAL Telegram Bot API stand-in: stdlib
  ``ThreadingHTTPServer`` bound to 127.0.0.1 on an ephemeral port, inside
  the test process, implementing exactly ``getMe``, ``getUpdates``,
  ``sendMessage``, ``editMessageText`` and ``answerCallbackQuery`` with
  provider-shaped JSON. Shapes follow the coordinator-verified Bot API
  reference (T-2919 Q2 follow-up): Update (sequential ``update_id``, at
  most one optional payload), CallbackQuery (``id``/``from``/
  ``chat_instance`` required, optional ``message``/``inline_message_id``/
  ``data``), InaccessibleMessage (``date`` == 0), Message (``from``
  optional, ``forward_origin`` for forwards, ``edit_date``), Chat ids as
  integers, ``callback_data`` 1-64 BYTES, getUpdates confirm-by-offset,
  and ``{"ok": false, "error_code", "description"}`` failures. NO network
  beyond loopback; NO real token.
- update builders for every spoof shape;
- ``HumanStack`` — a real approval journal + service + policy authorizer
  with a human ingress registration, wired IN-PROCESS to a real
  ``IngressCore`` over the fake (presenter and decider bridges).
"""

from __future__ import annotations

import io
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from tools.host_control.approval.contracts import PlanMetadata
from tools.host_control.approval.human import HumanIngressRegistration
from tools.host_control.approval.journal import ApprovalJournal
from tools.host_control.approval.policy import (
    PolicyApproverAuthorizer,
    parse_approver_policy,
)
from tools.host_control.approval.service import ApprovalService
from tools.host_control.contracts import HostControlError, PolicyDenied
from tools.host_control.telegram_ingress.bot_api import BotApiClient
from tools.host_control.telegram_ingress.core import (
    DecisionRefused,
    DecisionUnavailable,
    IngressCore,
    IngressLog,
    PresentRefused,
)
from tools.host_control.telegram_ingress.state import IngressState
from tools.test_host_control_approval_dbus_driver import FakeApprovalMetadataSource

# ---------------------------------------------------------------------------
# canaries (gate 21): recognizable values scanned for in every sink
# ---------------------------------------------------------------------------

BOT_ID = 7000001
# not a real token shape the Bot API documents; the ingress never parses it
CANARY_TOKEN = b"7000001:CANARYtokenAAAAbbbbCCCCddddEEEEffff"
CANARY_TOKEN_TEXT = CANARY_TOKEN.decode()
NONCE_MARK = "c0ffee"  # every service-minted handle/nonce starts with it
CANARY_DIGEST = "d1ce57" + "a" * 58
EFFECT_CANARY = "EFFECTCANARY-7c1d"

HUMAN_USER = 424242
HUMAN_CHAT = 424242  # private chat: chat id == user id
OTHER_USER = 515151
OTHER_CHAT = -1001234567890  # a supergroup id (negative, > 32 bits)

INGRESS_UID = 64001
CONSUMER_UID = 64002
PLAN_HANDLE = "2" * 64
OTHER_PLAN_HANDLE = "4" * 64
OTHER_DIGEST = "5" * 64
EFFECT_DIGEST = "f" * 64
HUMAN_FAMILY = "account.create"
FIXED_START = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# the fake Bot API
# ---------------------------------------------------------------------------


@dataclass
class RecordedRequest:
    method: str
    path: str
    body: bytes


@dataclass
class FakeBotApi:
    token: str = CANARY_TOKEN_TEXT
    bot_id: int = BOT_ID
    long_poll_cap_seconds: float = 0.2
    requests: list[RecordedRequest] = field(default_factory=list)
    faults: dict[str, str] = field(default_factory=dict)
    get_me_result: dict | None = None
    _updates: list[dict] = field(default_factory=list)
    _next_update_id: int = 900001
    _next_message_id: int = 501
    _next_callback: int = 1
    messages: dict[tuple[int, int], dict] = field(default_factory=dict)
    answered: list[str] = field(default_factory=list)
    # T-5518 stall knobs: a getUpdates that would RETURN updates waits on
    # ``hold_updates`` (when set to an unset Event) and counts in
    # ``held_polls``; every sendMessage first sleeps ``send_delay_seconds``
    hold_updates: threading.Event | None = None
    held_polls: int = 0
    send_delay_seconds: float = 0.0

    def __post_init__(self) -> None:
        self._cond = threading.Condition()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # silence stderr
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                status, payload = fake._dispatch(self.path, body)
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()

    # -- lifecycle ---------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    # -- provider-side helpers ----------------------------------------

    def bot_user(self) -> dict:
        return {
            "id": self.bot_id,
            "is_bot": True,
            "first_name": "Pantheon Approval",
            "username": "pantheon_approval_test_bot",
        }

    @staticmethod
    def user(user_id: int = HUMAN_USER, *, is_bot: bool = False) -> dict:
        return {"id": user_id, "is_bot": is_bot, "first_name": "Tester"}

    @staticmethod
    def chat(chat_id: int = HUMAN_CHAT, chat_type: str | None = None) -> dict:
        return {
            "id": chat_id,
            "type": chat_type or ("private" if chat_id > 0 else "supergroup"),
        }

    def push(self, payload_key: str, payload: dict) -> int:
        """Queue one Update carrying exactly one optional payload."""

        with self._cond:
            update_id = self._next_update_id
            self._next_update_id += 1
            self._updates.append({"update_id": update_id, payload_key: payload})
            self._cond.notify_all()
            return update_id

    def callback(
        self,
        *,
        message: dict | None,
        data: str | None,
        sender: dict | None = None,
        callback_id: str | None = None,
        inline_message_id: str | None = None,
    ) -> tuple[int, str]:
        if callback_id is None:
            callback_id = f"cbq-{self._next_callback}"
            self._next_callback += 1
        cq: dict = {
            "id": callback_id,
            "from": sender or self.user(),
            "chat_instance": "ci-1",
        }
        if message is not None:
            cq["message"] = message
        if inline_message_id is not None:
            cq["inline_message_id"] = inline_message_id
        if data is not None:
            cq["data"] = data
        return self.push("callback_query", cq), callback_id

    def card_message(self, chat_id: int, message_id: int) -> dict:
        """The Message object Telegram would attach to a callback for a
        stored message (a deep copy)."""

        return json.loads(json.dumps(self.messages[(chat_id, message_id)]))

    def last_card(self) -> dict:
        key = max(self.messages, key=lambda k: k[1])
        return self.card_message(*key)

    @staticmethod
    def button_data(message: dict, index: int) -> str:
        return message["reply_markup"]["inline_keyboard"][0][index]["callback_data"]

    def pending_update_ids(self) -> list[int]:
        with self._cond:
            return [u["update_id"] for u in self._updates]

    # -- dispatch -------------------------------------------------------

    def _error(self, status: int, description: str) -> tuple[int, dict]:
        return status, {"ok": False, "error_code": status, "description": description}

    def _dispatch(self, path: str, body: bytes) -> tuple[int, dict]:
        prefix = f"/bot{self.token}/"
        method = path[len(prefix):] if path.startswith(prefix) else ""
        self.requests.append(RecordedRequest(method or "?", path, body))
        if not path.startswith(prefix):
            # the documented failure shape; the code is not relied on
            return self._error(401, "Unauthorized")
        fault = self.faults.get(method)
        if fault == "500":
            return self._error(500, "Internal Server Error")
        if fault == "401":
            return self._error(401, "Unauthorized")
        if fault == "not_ok":
            return 200, {"ok": False, "error_code": 400, "description": "Bad Request"}
        if fault == "no_ok_field":
            return 200, {"unexpected": True}
        if fault == "malformed":
            return 200, b"<html>gateway says hi</html>"
        try:
            payload = json.loads(body.decode() or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._error(400, "Bad Request: invalid JSON")
        handler = {
            "getMe": self._get_me,
            "getUpdates": self._get_updates,
            "sendMessage": self._send_message,
            "editMessageText": self._edit_message_text,
            "answerCallbackQuery": self._answer_callback_query,
        }.get(method)
        if handler is None:
            return self._error(404, "Not Found: method not found")
        return handler(payload)

    def _get_me(self, payload: dict) -> tuple[int, dict]:
        return 200, {"ok": True, "result": self.get_me_result or self.bot_user()}

    def _get_updates(self, payload: dict) -> tuple[int, dict]:
        offset = payload.get("offset", 0)
        limit = payload.get("limit", 100)
        timeout = payload.get("timeout", 0)
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            return self._error(400, "Bad Request: invalid limit")
        deadline = time.monotonic() + min(float(timeout), self.long_poll_cap_seconds)
        with self._cond:
            # "an update is considered confirmed as soon as getUpdates is
            # called with an offset higher than its update_id"
            if isinstance(offset, int) and offset > 0:
                self._updates = [u for u in self._updates if u["update_id"] >= offset]
            while not self._updates and time.monotonic() < deadline:
                self._cond.wait(max(0.0, deadline - time.monotonic()))
            gate = self.hold_updates if self._updates else None
            if gate is not None:
                self.held_polls += 1
        if gate is not None:
            gate.wait(8.0)  # below the client's 10 s transport slack
        with self._cond:
            result = json.loads(json.dumps(self._updates[:limit]))
        return 200, {"ok": True, "result": result}

    def _validate_markup(self, markup: object) -> str | None:
        if markup is None:
            return None
        if not isinstance(markup, dict) or not isinstance(
            markup.get("inline_keyboard"), list
        ):
            return "Bad Request: invalid reply markup"
        for row in markup["inline_keyboard"]:
            for button in row:
                data = button.get("callback_data")
                if data is not None and not 1 <= len(data.encode()) <= 64:
                    return "Bad Request: BUTTON_DATA_INVALID"
        return None

    def _send_message(self, payload: dict) -> tuple[int, dict]:
        if self.send_delay_seconds:
            time.sleep(self.send_delay_seconds)
        chat_id = payload.get("chat_id")
        text = payload.get("text")
        if not isinstance(chat_id, int) or not isinstance(text, str):
            return self._error(400, "Bad Request: chat_id and text are required")
        # Telegram trims leading/trailing whitespace and counts length in
        # UTF-16 code units (1-4096 after entity parsing); the stored and
        # echoed text is the TRIMMED one
        text = text.strip()
        if not 1 <= len(text.encode("utf-16-le")) // 2 <= 4096:
            return self._error(400, "Bad Request: message text is empty or too long")
        problem = self._validate_markup(payload.get("reply_markup"))
        if problem:
            return self._error(400, problem)
        with self._cond:
            message_id = self._next_message_id
            self._next_message_id += 1
        message = {
            "message_id": message_id,
            "from": self.bot_user(),
            "chat": self.chat(chat_id),
            "date": int(time.time()),
            "text": text,
        }
        if payload.get("reply_markup") is not None:
            message["reply_markup"] = payload["reply_markup"]
        self.messages[(chat_id, message_id)] = message
        echo_fault = self.faults.get("sendMessage.echo")
        if echo_fault in ("wrong_chat", "no_text", "empty_text"):
            echoed = json.loads(json.dumps(message))
            if echo_fault == "wrong_chat":
                echoed["chat"] = self.chat(OTHER_CHAT)
            elif echo_fault == "no_text":
                echoed.pop("text")
            else:
                echoed["text"] = ""
            return 200, {"ok": True, "result": echoed}
        return 200, {"ok": True, "result": message}

    def _edit_message_text(self, payload: dict) -> tuple[int, dict]:
        key = (payload.get("chat_id"), payload.get("message_id"))
        if key not in self.messages:
            return self._error(400, "Bad Request: message to edit not found")
        text = payload.get("text")
        if not isinstance(text, str) or not 1 <= len(text) <= 4096:
            return self._error(400, "Bad Request: text is empty or too long")
        problem = self._validate_markup(payload.get("reply_markup"))
        if problem:
            return self._error(400, problem)
        message = self.messages[key]
        message["text"] = text
        message["edit_date"] = int(time.time())
        markup = payload.get("reply_markup")
        if markup is None or not markup.get("inline_keyboard"):
            message.pop("reply_markup", None)
        else:
            message["reply_markup"] = markup
        return 200, {"ok": True, "result": message}

    def _answer_callback_query(self, payload: dict) -> tuple[int, dict]:
        callback_id = payload.get("callback_query_id")
        if not isinstance(callback_id, str):
            return self._error(400, "Bad Request: query id required")
        self.answered.append(callback_id)
        return 200, {"ok": True, "result": True}


# ---------------------------------------------------------------------------
# policy + approval domain with a human ingress registration
# ---------------------------------------------------------------------------


def human_policy_raw(
    *,
    ingress_uid: int = INGRESS_UID,
    agent_uid: int = 64010,
    owner_uid: int = 64020,
    revokers: tuple[str, ...] = ("root-admin",),
    human_families: tuple[str, ...] = (HUMAN_FAMILY,),
) -> dict:
    return {
        "schema": "pantheon-approval-policy/v1",
        "revision": "approver-policy-human-r1",
        "seat_classes": {"recovery": "worker-seat"},
        "subjects": {
            "operator": ingress_uid,
            "specialist": agent_uid,
            "alpha": owner_uid,
            "root-admin": 0,
        },
        "family_categories": {
            HUMAN_FAMILY: "unix_accounts",
            "persona.install": "seat_apply_reversible",
        },
        "approvers": {
            "operator": {
                "decision_source": "human",
                "grants": [
                    {
                        "family": family,
                        "seat_classes": ["worker-seat"],
                        "max_ttl_seconds": 1800,
                    }
                    for family in human_families
                ],
            },
            "specialist": {
                "decision_source": "agent",
                "grants": [
                    {
                        "family": family,
                        "seat_classes": ["worker-seat"],
                        "max_ttl_seconds": 3600,
                    }
                    for family in (HUMAN_FAMILY, "persona.install")
                ],
            },
        },
        "revokers": list(revokers),
    }


def human_plan(plan_digest: str = CANARY_DIGEST, *, family: str = HUMAN_FAMILY) -> PlanMetadata:
    return PlanMetadata(
        owner="alpha",
        performer="alpha",
        target_seat_id="recovery",
        operation_families=(family,),
        plan_digest=plan_digest,
        effect_digest=EFFECT_DIGEST,
        manifest_revision="manifest-r1",
        policy_revision="policy-r1",
    )


def registration(uid: int = INGRESS_UID) -> HumanIngressRegistration:
    return HumanIngressRegistration(
        uid=uid,
        provider="telegram",
        human_subject="operator",
        provider_user_id=str(HUMAN_USER),
        provider_chat_id=str(HUMAN_CHAT),
    )


class MarkedHandles:
    """Deterministic CSPRNG stand-in for tests: every minted handle /
    nonce starts with NONCE_MARK so leak scans can find any of them."""

    def __init__(self) -> None:
        self.count = 0
        self.minted: list[str] = []

    def __call__(self) -> str:
        self.count += 1
        value = f"{NONCE_MARK}{self.count:058x}"
        self.minted.append(value)
        return value


class Clock:
    def __init__(self, start: datetime = FIXED_START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


class InProcessDecider:
    """HumanDecider bridge: calls the real service as the given uid and
    maps refusals the way the D-Bus client does (closed body)."""

    def __init__(self, service: ApprovalService, uid: int) -> None:
        self.service = service
        self.uid = uid
        self.calls: list[tuple] = []

    def human_decision(self, *args: str) -> tuple[str, str]:
        self.calls.append(args)
        try:
            result = self.service.human_decision(self.uid, *args)
        except PolicyDenied as exc:
            raise DecisionRefused(str(exc.reason_code)) from None
        except HostControlError as exc:
            raise DecisionRefused(str(exc)[:64]) from None
        return (result.state, result.attestation_handle)


class RecordingDecider:
    """HumanDecider that ACCEPTS everything it is given (mutation tests:
    isolates the ingress checks from the service's defense in depth)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def human_decision(self, *args: str) -> tuple[str, str]:
        self.calls.append(args)
        return ("granted" if args[2] == "granted" else "denied", "a" * 64)


class CorePresenter:
    """HumanCardPresenter bridge to an in-process IngressCore."""

    def __init__(self) -> None:
        self.core: IngressCore | None = None
        self.calls: list[tuple] = []

    def present(self, *args: str) -> tuple[str, str]:
        self.calls.append(args)
        assert self.core is not None
        return self.core.present(*args)


class ManualScheduler:
    """``DeliveryScheduler`` fake on the stack clock: ``call_later`` only
    records; ``run_due`` fires what is due (in order), as a loop would."""

    def __init__(self, clock: "Clock") -> None:
        self.clock = clock
        self.timers: list[tuple[datetime, int, Callable[[], None]]] = []
        self._seq = 0
        self.delays: list[float] = []

    def call_later(self, delay_seconds: float, callback: Callable[[], None]) -> None:
        self._seq += 1
        self.delays.append(delay_seconds)
        self.timers.append(
            (self.clock() + timedelta(seconds=delay_seconds), self._seq, callback)
        )

    def run_due(self) -> int:
        fired = 0
        while True:
            due = sorted(
                (t for t in self.timers if t[0] <= self.clock()),
                key=lambda t: (t[0], t[1]),
            )
            if not due:
                return fired
            self.timers.remove(due[0])
            due[0][2]()
            fired += 1


class CoreAsyncPresenter:
    """``AsyncHumanCardPresenter`` bridge to an in-process IngressCore.

    ``mode`` scripts each attempt: ``"ok"`` presents and replies;
    ``"lost"`` presents, then reports a timeout (the T-2919 r2 stall: the
    ingress serviced it after the caller gave up); ``"down"`` fails
    without reaching the ingress; ``"hold"`` keeps the attempt until
    ``release()`` (an attempt in flight)."""

    def __init__(self) -> None:
        self.core: IngressCore | None = None
        self.calls: list[tuple] = []
        self.mode = "ok"
        self.held: list[Callable[[], None]] = []

    def begin_present(self, *args, on_done) -> None:
        from tools.host_control.approval.presentation import PresentReply

        self.calls.append(args)
        assert self.core is not None
        mode = self.mode
        if mode == "down":
            on_done(PresentReply.failed())
            return

        def run() -> None:
            try:
                state, ref = self.core.present(*args)
            except PresentRefused as exc:
                on_done(PresentReply.failed(retryable=exc.kind == "unavailable"))
                return
            if mode == "lost":
                on_done(PresentReply.failed())
            else:
                on_done(PresentReply(state, ref))

        if mode == "hold":
            self.held.append(run)
        else:
            run()

    def release(self) -> None:
        held, self.held = self.held, []
        for run in held:
            run()


class HeldDecider:
    """Async ``begin_human_decision`` decider whose answers wait until
    ``release_all`` (a HumanDecision in flight); answers come from the
    wrapped synchronous decider, or ``answer_with`` when set."""

    def __init__(self, inner: object) -> None:
        self.inner = inner
        self.calls: list[tuple] = []
        self.pending: list[tuple[tuple, Callable]] = []
        self.answer_with: BaseException | None = None

    def begin_human_decision(self, *args, on_done) -> None:
        self.calls.append(args)
        self.pending.append((args, on_done))

    def release_all(self) -> int:
        pending, self.pending = self.pending, []
        for args, on_done in pending:
            if self.answer_with is not None:
                on_done(None, self.answer_with)
                continue
            try:
                on_done(self.inner.human_decision(*args), None)
            except (DecisionRefused, DecisionUnavailable) as exc:
                on_done(None, exc)
        return len(pending)


@dataclass
class HumanStack:
    root: Path
    fake: FakeBotApi
    clock: Clock
    handles: MarkedHandles
    metadata: FakeApprovalMetadataSource
    journal: ApprovalJournal
    service: ApprovalService
    presenter: object
    decider: object
    core: IngressCore
    state: IngressState
    log_stream: io.StringIO
    scheduler: ManualScheduler | None = None

    def close(self) -> None:
        self.state.close()
        self.journal.close()
        self.fake.close()

    def open_review(self, plan_handle: str = PLAN_HANDLE, digest: str = CANARY_DIGEST):
        return self.service.review("specialist", plan_handle, digest, None).record

    def request(self, review, key: str = "rh-1"):
        return self.service.request_human(
            "specialist", review.review_handle, review.plan_digest, key
        )


def build_human_stack(
    root: Path,
    *,
    decider_factory: Callable[[ApprovalService], object] | None = None,
    checks=None,
    review_ttl: timedelta = timedelta(hours=1),
    policy_raw: dict | None = None,
    journal_class: type = ApprovalJournal,
    async_delivery: bool = False,
) -> HumanStack:
    root.mkdir(parents=True, exist_ok=True)
    fake = FakeBotApi()
    clock = Clock()
    handles = MarkedHandles()
    metadata = FakeApprovalMetadataSource()
    metadata.plans[PLAN_HANDLE] = human_plan()
    metadata.plans[OTHER_PLAN_HANDLE] = human_plan(OTHER_DIGEST)
    metadata.summaries[PLAN_HANDLE] = (
        '{"effects":["create account svc-demo"],"marker":"' + EFFECT_CANARY + '"}'
    )
    journal = journal_class.open(root / "approval.sqlite3")
    presenter = CoreAsyncPresenter() if async_delivery else CorePresenter()
    scheduler = ManualScheduler(clock) if async_delivery else None
    presenter_kwargs = (
        {"card_presenter": presenter, "delivery_scheduler": scheduler}
        if async_delivery
        else {"presenter": presenter}
    )
    service = ApprovalService(
        journal=journal,
        metadata_source=metadata,
        clock=clock,
        authorizer=PolicyApproverAuthorizer(
            parse_approver_policy(policy_raw or human_policy_raw())
        ),
        handle_factory=handles,
        registered_consumer_uids=frozenset((CONSUMER_UID,)),
        human_ingress=registration(),
        review_ttl=review_ttl,
        **presenter_kwargs,
    )
    decider = (
        decider_factory(service)
        if decider_factory is not None
        else InProcessDecider(service, INGRESS_UID)
    )
    state_dir = root / "ingress-state"
    state_dir.mkdir(mode=0o700)
    state = IngressState.open(state_dir)
    log_stream = io.StringIO()
    kwargs = {}
    if checks is not None:
        kwargs["checks"] = checks
    core = IngressCore(
        state=state,
        api=BotApiClient(CANARY_TOKEN, base_url=fake.base_url),
        decider=decider,  # type: ignore[arg-type]
        bot_user_id=BOT_ID,
        allow_user_id=HUMAN_USER,
        allow_chat_id=HUMAN_CHAT,
        card_ttl_max_seconds=3600,
        log=IngressLog(log_stream),
        clock=clock,
        **kwargs,
    )
    presenter.core = core
    return HumanStack(
        root=root,
        fake=fake,
        clock=clock,
        handles=handles,
        metadata=metadata,
        journal=journal,
        service=service,
        presenter=presenter,
        decider=decider,
        core=core,
        state=state,
        log_stream=log_stream,
        scheduler=scheduler,
    )


# ---------------------------------------------------------------------------
# crash-test child (window (a): process death BETWEEN consuming the nonce
# and committing the decision). The fault hook is a TEST SUBCLASS only —
# production has no fault flag. Inputs arrive in a JSON file (never argv).
# ---------------------------------------------------------------------------


class KillAfterNonceConsumedJournal(ApprovalJournal):
    """Dies (real process exit, no cleanup, no rollback) right after the
    nonce-consuming UPDATE executed inside the open transaction."""

    def _after_human_nonce_consumed(self, connection) -> None:
        import os

        os._exit(137)


def crash_child(spec_path: str) -> int:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    metadata = FakeApprovalMetadataSource()
    metadata.plans[PLAN_HANDLE] = human_plan()
    journal = KillAfterNonceConsumedJournal.open(Path(spec["journal"]))
    now = datetime.fromisoformat(spec["now"])
    service = ApprovalService(
        journal=journal,
        metadata_source=metadata,
        clock=lambda: now,
        authorizer=PolicyApproverAuthorizer(parse_approver_policy(human_policy_raw())),
        handle_factory=MarkedHandles(),
        human_ingress=registration(),
    )
    service.human_decision(
        INGRESS_UID,
        spec["review_handle"],
        spec["digest"],
        "granted",
        spec["source_event"],
        spec["nonce"],
        spec["key"],
    )
    return 0  # unreachable when the hook fires


if __name__ == "__main__":  # pragma: no cover - subprocess entry only
    import sys

    if len(sys.argv) == 3 and sys.argv[1] == "crash-child":
        raise SystemExit(crash_child(sys.argv[2]))
    raise SystemExit(64)
