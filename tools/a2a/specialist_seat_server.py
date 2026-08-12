"""Specialist seat A2A server — rung-1 loopback, real `research-request` skill.

Design: knowledge/episodic/2026-07-20-a2a-rung1-design/notes.md sections
2.1 (server unit shape), 2.2 (Agent Card, adopted verbatim from
agent_card.json), 2.3/2.4 (skill contract + lifecycle), 3 (security), and
knowledge/episodic/2026-07-20-a2a-rung1-specialist-seat/notes.md §7 (BT-5
addendum — the authoritative cross-seat invocation mechanism). Ticket:
T-1890 (BT-5 of the T-1882 rung ladder), building on T-1886/T-1887/T-1888/
T-1889 (BT-1..4).

Scope at BT-5: replace the BT-1..4 stub `research-request` handler with the
real thin A2A face over the specialist watch-researcher stack. A standalone
a2a-sdk process cannot spawn Claude subagents (and headless `claude -p` is
billing-walled — design notes §7 consult), so research runs ASYNCHRONOUSLY
on specialist's own live session: this server's intake screen stages a FIELDED
staged object to the specialist mailbox inbox and leaves the task WORKING; a
background poll loop consumes results from the provisioned specialist-a2a
mailbox inbox (the specialist session applies
sop/standard/a2a-research-request-handling.md on the other end) and
applies COMPLETED/FAILED onto the task asynchronously, out-of-band from
the HTTP request that created it. See the "Async result loop" section
below for why that requires `TaskManager.save_task_event()` rather than
the live-request `TaskUpdater`/`EventQueue` API.

SDK surface notes (a2a-sdk 1.1.1): see PROVISION.md "SDK surface notes"
section for the full writeup of why this module serves the Agent Card as a
static file instead of through the SDK's own AgentCard-object serializer,
and why `enable_v0_3_compat=True` is turned on for the JSON-RPC routes.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from google.protobuf import json_format, struct_pb2
from google.protobuf.timestamp_pb2 import Timestamp
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a.helpers.proto_helpers import new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import (
    InMemoryTaskStore,
    TaskManager,
    TaskStore,
    TaskUpdater,
)
from a2a.types import a2a_pb2 as pb
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH

# Bootstrap: make `from tools.mailbox import ...` work whether this module
# runs as a script (systemd ExecStart invokes it directly — __package__ is
# "" ) or as a package submodule (`python -m tools.a2a.specialist_seat_server`,
# or imported by the test suite — __package__ is already "tools.a2a", repo
# root already on sys.path). Same convention as tools/activity_digest.py.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools import mailbox  # noqa: E402


logger = logging.getLogger(__name__)

AGENT_CARD_PATH = Path(__file__).parent / "agent_card.json"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 41100
HOST_ENV = "SPECIALIST_A2A_HOST"
PORT_ENV = "SPECIALIST_A2A_PORT"
ALLOW_NONLOOPBACK_ENV = "SPECIALIST_A2A_ALLOW_NONLOOPBACK"
RPC_URL = "/a2a"

# Hostnames treated as loopback for the bind-safety check. Real deployments
# only ever use 127.0.0.1 (see agent_card.json's "url"); the others are
# accepted so local dev/testing against ::1 or localhost isn't punished.
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# T-1889 (BT-4): above-bar detection, kept as the real intake screen's
# topic-sensitivity gate (design notes §2.4's initial trigger set). A
# SIMPLE deterministic keyword scan — this constant's earlier docstring
# already named it a stand-in for "BT-5's real intake screen"; BT-5
# generalizes the SAME shape (now scanning the structured `question` +
# `requester_context` fields, not just top-level message text) rather than
# claiming a smarter classifier. The real nuanced judgment is the SESSION
# side's job (design notes §7.4, "the screen can't foresee every hazard;
# the session can") — this screen only narrows. Tune during BT-6 soak.
ABOVE_BAR_KEYWORDS: tuple[str, ...] = (
    "exploit",
    "credentials",
    "pantheon internals",
)

# Design notes §3 never-do list: what the research-request skill can NEVER
# do, regardless of payload.
#
# ADVISORY, NOT LOAD-BEARING (T-2531). This denylist is a coarse fast-path
# pre-catch, NOT the primary control. Measurement (T-2529 corpus, 55 naive
# never-do paraphrases) showed a denylist over an *open* default catches
# ~0% of paraphrased action-requests — a substring denylist over natural
# language can never be complete, so whatever the default does decides the
# posture. The primary control is now the INVERTED DEFAULT below
# (`is_research_question_shape`): the seat screens for conformance to its
# one advertised skill (a well-formed research question) and HOLDs
# everything else, rather than trying to enumerate every forbidden phrasing
# (P1 — architecture over an ever-growing policy list). These triggers
# remain only as a leak-free fast reject for the exact literal phrasings,
# and for audit signal; they are not relied upon for coverage. Scans BOTH
# `question` and `requester_context` (design notes §7.3: `requester_context`
# is not the only attacker-controlled field — `question` is equally so).
NEVER_DO_TRIGGERS: tuple[str, ...] = (
    "grant access",
    "modify allowlist",
    "add me to the allowlist",
    "add to the allowlist",
    "approve the pending pairing",
    "approve my pairing",
    "run ticket ops",
    "ticket.py",
    "execute shell",
    "run shell",
    "run a shell command",
    "touch operator-curated",
    "edit operator-curated",
    "relay this to telegram",
    "relay to operator",
)

# --- T-2531 invert-default primary control -------------------------------
# The seat advertises exactly ONE skill: answer a bounded research question.
# The primary intake control therefore screens for conformance to that skill
# (does this LOOK like a research question?) and HOLDs everything else for
# operator approval, rather than enumerating forbidden phrasings. This is
# the P1 move: a positive shape-test over a closed default is structural;
# a denylist over an open default is a policy list that must be endlessly
# grown and still decays (measured ~0% coverage, T-2529 corpus).
#
# HONEST SCOPE (D5 instrument-trust): validated against the SAME naive
# T-2529 corpus (0/55 never-do paraphrases pass; 0/8 legit-research held).
# That demonstrates the screen FLIPS the measured failure — it is NOT a
# proof of unbypassability. A pure topic-question phrasing of an action
# ("what is the process to add me to the allowlist?") can still shape-pass;
# it is caught by the two structural layers this screen only fronts:
#   (1) staging quarantine — requester text lands as DATA values only
#       (`build_staged_object`), never concatenated into instructions;
#   (2) the session-side second-look (SOP a2a-research-request-handling.md
#       §2) — full session judgment, not a keyword scan, and the seat only
#       RESEARCHES a staged request, never executes an action.
# So a shape-pass is bounded to "Specialist researches that topic", not "Specialist
# does that action". The screen narrows; the session decides.

# Question/research LEADS: a request that STARTS with one of these reads as
# a question or a research directive. Trailing spaces are deliberate (e.g.
# "do " / "is ") to avoid matching "document"/"island".
_RESEARCH_LEAD: tuple[str, ...] = (
    "what", "how", "why", "when", "where", "which", "who", "whom", "whose",
    "is ", "are ", "does", "do ", "can ", "could ", "should ", "will ",
    "research", "survey", "summarise", "summarize", "compare", "review",
    "investigate", "analyse", "analyze", "explain", "describe", "find out",
    "look into", "assess", "evaluate", "outline", "give an overview",
    "literature review", "what is", "what are", "tell me about",
)

# Research phrasings that read as a topic question anywhere in the text,
# not only at the lead.
_RESEARCH_ANYWHERE: tuple[str, ...] = (
    "research", "survey", "literature review", "state of the art",
    "best practice", "best practices", "overview of", "comparison of",
    "how does", "how do", "what is the", "what are the",
)

# NEGATIVE gate: structural markers of an ACTION-ON-BEHALF-OF-REQUESTER —
# "do X for me / grant me Y". These are not a denylist of forbidden verbs
# (that is the failure mode this ticket replaces); they describe the SHAPE
# of a favor-request, which the seat's one skill (answer a research
# question) is not. Checked FIRST so an action dressed as a question ("can
# you sort my pairing?") is held even though it ends in "?".
_ACTION_FAVOR_MARKERS: tuple[str, ...] = (
    "for me", "on my behalf", "my access", "give me", "me access",
    "my account", "my agent", "my endpoint", "my requests", "my pairing",
    "my pair ", "handle that", "sort that", "okay it", "action it",
    "can you sort", "can you handle",
)

# A research question longer than this is not a plausible bounded question;
# hold it for operator eyes rather than stage a wall of text.
_MAX_QUESTION_CHARS = 4000


def is_research_question_shape(question: str) -> bool:
    """True iff `question` conforms to the seat's one advertised skill:
    a bounded, well-formed research question (T-2531 primary control).

    Order matters: the action-favor negative gate runs BEFORE the positive
    shape tests, so an action-on-behalf request dressed with a "?" or a
    "could you" lead is still held. See the block comment above for the
    honest scope of this test and the structural layers behind it.
    """
    q = question.strip()
    if not q or len(q) > _MAX_QUESTION_CHARS:
        return False
    lo = q.lower()
    if any(marker in lo for marker in _ACTION_FAVOR_MARKERS):
        return False
    if lo.startswith(_RESEARCH_LEAD):
        return True
    if q.rstrip().endswith("?"):
        return True
    if any(keyword in lo for keyword in _RESEARCH_ANYWHERE):
        return True
    return False


# §2.4/§3: the INPUT_REQUIRED status message states only what approval is
# pending — no keyword, no internal field/class name, no stack. Generic and
# fixed regardless of which keyword matched, so it can never leak *which*
# rule fired.
PENDING_APPROVAL_MESSAGE = (
    "This request has been flagged as requiring operator approval before "
    "the seat proceeds. Send a follow-up message on this task to approve "
    "and resume, or call tasks/cancel to cancel it."
)

# §3: never-do-list rejection is also leak-free — generic and fixed
# regardless of which trigger matched.
NEVER_DO_REJECTION_MESSAGE = (
    "This request falls outside the research-request skill's bounded "
    "scope and cannot be actioned. Submit a bounded research question "
    "instead."
)

REPORT_UNREADABLE_MESSAGE = (
    "The research report could not be read from its committed location. "
    "The request could not be completed."
)

RESULT_GENERIC_FAILURE_MESSAGE = "The research request could not be completed."

DEFAULT_DEPTH = "standard"

# Design notes §7.1 (Specialist's mailbox-both-ways mechanism): the specialist
# session inbox this server STAGES requests into, and the dedicated
# specialist-a2a result inbox this server is the SOLE consumer of. Both are
# already provisioned on the shared substrate. Explicit-parameter
# (not env-var) overridable — same convention as tools/mailbox_wake.py's
# `run(..., inbox=...)` — so tests can point at a temp maildir without
# touching the live boxes.
DEFAULT_REQUEST_INBOX = "/home/the-ancestor/mailbox/specialist"
DEFAULT_RESULT_INBOX = "/home/the-ancestor/mailbox/specialist-a2a"

# Design notes §7.1: "minimum viable events: staged→WORKING and
# result→COMPLETED/FAILED... latency fits the card's published contract
# ('minutes to hours')". A 5s poll cadence matches the existing
# mailbox-wake poll cadence (tools/mailbox_wake.py) and is cheap against a
# minutes-to-hours task.
RESULT_POLL_INTERVAL_SECONDS = 5.0

# Sender label this server uses when staging into the specialist inbox — must
# satisfy tools/mailbox.py's sender-name charset (_SENDER_RE).
STAGE_SENDER = "specialist-a2a"


def load_agent_card_dict() -> dict:
    """Loads the finalized §2.2 Agent Card as a plain dict.

    Served verbatim at the well-known discovery path. Kept separate from
    `build_native_agent_card` below — see PROVISION.md SDK surface notes
    for why these are two different objects.
    """
    with AGENT_CARD_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def build_native_agent_card(card: dict) -> pb.AgentCard:
    """Builds the SDK-native (v1.0 proto) AgentCard used internally by
    DefaultRequestHandler for capability checks.

    This is NOT what's served at the well-known path (see
    `create_agent_card_file_route`) — the native proto schema has no
    `url`/`preferredTransport`/`protocolVersion`/`stateTransitionHistory`
    fields, so round-tripping §2.2 through it would silently drop them.
    Only the fields the native schema actually has are carried over.
    """
    caps = card.get("capabilities", {})
    skills = [
        pb.AgentSkill(
            id=skill["id"],
            name=skill["name"],
            description=skill.get("description", ""),
            tags=list(skill.get("tags", [])),
            input_modes=list(skill.get("inputModes", [])),
            output_modes=list(skill.get("outputModes", [])),
            examples=list(skill.get("examples", [])),
        )
        for skill in card.get("skills", [])
    ]
    return pb.AgentCard(
        name=card["name"],
        description=card.get("description", ""),
        version=card.get("version", ""),
        capabilities=pb.AgentCapabilities(
            streaming=bool(caps.get("streaming", False)),
            push_notifications=bool(caps.get("pushNotifications", False)),
        ),
        default_input_modes=list(card.get("defaultInputModes", [])),
        default_output_modes=list(card.get("defaultOutputModes", [])),
        skills=skills,
    )


def create_agent_card_file_route(card_path: Path = AGENT_CARD_PATH) -> Route:
    """Serves the literal Agent Card JSON file at the well-known path.

    Deliberately bypasses the SDK's `create_agent_card_routes` helper,
    which serializes from the native proto AgentCard and would drop the
    fields noted in `build_native_agent_card`'s docstring.
    """

    async def _get_card(request: Request) -> JSONResponse:
        with card_path.open(encoding="utf-8") as f:
            return JSONResponse(json.load(f))

    return Route(
        path=AGENT_CARD_WELL_KNOWN_PATH,
        endpoint=_get_card,
        methods=["GET"],
    )


def is_above_bar(text: str) -> bool:
    """Above-bar topic-sensitivity scan (design notes §2.4).

    Case-insensitive substring scan against `ABOVE_BAR_KEYWORDS`.
    """
    lowered = text.lower()
    return any(keyword in lowered for keyword in ABOVE_BAR_KEYWORDS)


def is_never_do_violation(question: str, requester_context: str) -> bool:
    """Never-do-list scan (design notes §3). See `NEVER_DO_TRIGGERS`'s
    comment for why both fields are scanned and why this runs before
    `is_above_bar`."""
    combined = f"{question}\n{requester_context}".lower()
    return any(trigger in combined for trigger in NEVER_DO_TRIGGERS)


def extract_request_fields(message: pb.Message) -> dict:
    """Extracts the §2.3 research-request payload fields from an inbound
    A2A Message.

    Looks for a `data` Part (application/json DataPart per the skill's
    `inputModes`) and reads `question`/`depth`/`scope`/`deadline`/
    `requester_context` from it; falls back to the concatenated text of
    any `text` Parts as `question` if no data part is present (keeps
    plain-text callers — e.g. the orchestrator_client smoke usage — working).
    Defaults match §2.3: `depth="standard"`, `scope={}`, `deadline=None`,
    `requester_context=""`.
    """
    data_dict: dict = {}
    text_chunks: list[str] = []
    for part in message.parts:
        if part.HasField("data"):
            parsed = json_format.MessageToDict(part.data)
            if isinstance(parsed, dict):
                data_dict.update(parsed)
        elif part.HasField("text"):
            text_chunks.append(part.text)

    question = data_dict.get("question")
    if not question:
        question = "\n".join(text_chunks).strip()

    scope = data_dict.get("scope")
    if not isinstance(scope, dict):
        scope = {}

    return {
        "question": question or "",
        "depth": data_dict.get("depth") or DEFAULT_DEPTH,
        "scope": scope,
        "deadline": data_dict.get("deadline"),
        "requester_context": data_dict.get("requester_context") or "",
    }


def build_staged_object(task_id: str, fields: dict) -> dict:
    """Builds the exact design-notes-§7.3 staged-object shape.

    Fields are assigned as dict VALUES only — never concatenated into any
    other string (subject, log line, instruction text). This is the
    structural quarantine layer 1 (§7.3): requester-controlled text can
    only ever be read as DATA by anything downstream of this function.
    """
    return {
        "task_id": task_id,
        "question": fields["question"],
        "depth": fields["depth"],
        "scope": fields["scope"],
        "deadline": fields["deadline"],
        "untrusted_requester_context": fields["requester_context"],
    }


def stage_request_for_research(
    task_id: str,
    fields: dict,
    *,
    request_inbox: str = DEFAULT_REQUEST_INBOX,
    sender: str = STAGE_SENDER,
) -> str:
    """Sends the FIELDED staged object (§7.3) to the specialist inbox.

    The subject carries only the task_id — never requester text — so the
    subject itself can never become a second, unquarantined vector for the
    same content `build_staged_object`'s dict-shape already protects.
    """
    staged = build_staged_object(task_id, fields)
    return mailbox.send(
        request_inbox,
        staged,
        sender,
        subject=f"a2a research-request {task_id}",
    )


class ResearchRequestExecutor(AgentExecutor):
    """Real `research-request` skill (T-1890, BT-5).

    A fresh request runs the intake screen (design notes §2.4/§3):
    - never-do-list violation -> WORKING -> FAILED, no mailbox send (§3:
      out-of-scope actions are rejected, not gated).
    - above-bar topic -> WORKING -> INPUT_REQUIRED, same leak-free pause
      as BT-4 (T-1889).
    - otherwise -> stages a FIELDED staged object to the specialist inbox
      (§7.1/§7.3) and RETURNS, leaving the task WORKING. The task reaches
      a terminal state ASYNCHRONOUSLY, out-of-band, when
      `poll_and_apply_results` (below) applies a result read from the
      specialist-a2a mailbox — this request's own EventQueue is long gone by
      then; see the "Async result loop" section below for the mechanism.

    A follow-up Message on a paused (INPUT_REQUIRED) task is the human's
    approval (design notes §2.4: "the human at rung 1 is orchestrator... resumes")
    — it stages the ORIGINAL request's fields (`task.history[0]`), never
    the resume message's own text: an approval reply must never itself
    become a second, unscreened injection vector, and it does NOT re-run
    the intake screen (the approval IS the gate pass — re-running the same
    deterministic scan on identical text would just re-trip the same gate
    forever).
    """

    def __init__(self, *, request_inbox: str = DEFAULT_REQUEST_INBOX) -> None:
        self._request_inbox = request_inbox

    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        if context.current_task is not None:
            await self._resume(context, event_queue)
            return
        await self._start_fresh(context, event_queue)

    async def _start_fresh(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        # The framework requires an explicit Task object enqueued before any
        # TaskStatusUpdateEvent — new_task_from_user_message also validates
        # the inbound message shape (role=user, non-empty parts), so a
        # malformed message raises here and the framework turns that into a
        # clean TASK_STATE_ERROR / protocol-level response rather than a
        # crash (AgentExecutor.execute contract).
        task = new_task_from_user_message(context.message)
        await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()

        fields = extract_request_fields(context.message)
        await self._screen_and_dispatch(updater, task.id, fields)

    async def _screen_and_dispatch(
        self, updater: TaskUpdater, task_id: str, fields: dict
    ) -> None:
        question = fields["question"]
        requester_context = fields["requester_context"]

        # T-2531 screen order:
        #   1. never-do literal fast-reject (advisory pre-catch) → FAILED
        #   2. above-bar topic-sensitivity → HOLD (operator approval)
        #   3. INVERTED DEFAULT — does this conform to the one advertised
        #      skill (a research question)? If NOT → HOLD. Only a request
        #      that reads as a bounded research question stages for work.
        # The default is HOLD, not PASS: the seat opens closed and only a
        # positively-recognized research question gets through (P1).
        if is_never_do_violation(question, requester_context):
            await updater.failed(
                message=updater.new_agent_message(
                    parts=[pb.Part(text=NEVER_DO_REJECTION_MESSAGE)]
                )
            )
            return

        if is_above_bar(f"{question}\n{requester_context}"):
            await updater.requires_input(
                message=updater.new_agent_message(
                    parts=[pb.Part(text=PENDING_APPROVAL_MESSAGE)]
                )
            )
            return

        if not is_research_question_shape(question):
            await updater.requires_input(
                message=updater.new_agent_message(
                    parts=[pb.Part(text=PENDING_APPROVAL_MESSAGE)]
                )
            )
            return

        stage_request_for_research(
            task_id, fields, request_inbox=self._request_inbox
        )
        # Task stays WORKING — the last published event was start_work()'s
        # WORKING transition; no further event is published here. The
        # asynchronous result loop resolves the task later.

    async def _resume(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        # T-1889 (BT-4): a follow-up Message on an existing (paused) task —
        # `context.current_task` was fetched + updated with this message by
        # the SDK's DefaultRequestHandler before `execute` was called again
        # (design notes §2.4's "HITL wiring" paragraph). The ORIGINAL
        # question/fields come from the task's history[0] (the message
        # that created the task) — never from this follow-up message's own
        # text/parts (see class docstring: an approval reply must never
        # itself become an unscreened injection vector).
        task = context.current_task
        original_message = task.history[0] if task.history else None
        fields = (
            extract_request_fields(original_message)
            if original_message is not None
            else {
                "question": "",
                "depth": DEFAULT_DEPTH,
                "scope": {},
                "deadline": None,
                "requester_context": "",
            }
        )

        updater = TaskUpdater(event_queue, task.id, task.context_id)
        # Resume = approval; no re-screen (class docstring). Move the task
        # observably back to WORKING first (it was paused at
        # INPUT_REQUIRED), then stage — mirrors the fresh path's
        # start_work-then-stage order.
        await updater.update_status(pb.TaskState.TASK_STATE_WORKING)
        stage_request_for_research(
            task.id, fields, request_inbox=self._request_inbox
        )

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        updater = TaskUpdater(
            event_queue, context.task_id, context.context_id
        )
        await updater.cancel()


# ---------------------------------------------------------------------------
# Async result loop (T-1890 BT-5).
#
# `TaskUpdater` only ENQUEUES events onto a live `EventQueue` tied to one
# HTTP request; once `execute()` above has returned (which happens as soon
# as a request is staged), that queue is gone. The actual task-store-write
# primitive the framework itself uses is `TaskManager.save_task_event()`
# (a2a.server.tasks, publicly exported) — it persists a
# Task/TaskStatusUpdateEvent/TaskArtifactUpdateEvent independent of any
# live queue, which is exactly what's needed here: every request in this
# rung-1 server is unauthenticated, so a fresh `ServerCallContext()` built
# in the poll loop resolves to the SAME owner bucket in the
# `InMemoryTaskStore` as the original request's context (D11: verified
# live against the installed SDK, not assumed).
#
# The TaskStatusUpdateEvent/TaskArtifactUpdateEvent protos below are built
# directly rather than routed back through a throwaway `TaskUpdater` +
# `EventQueue` pair: direct `EventQueue()` construction is a deprecated
# path in the installed a2a-sdk (1.1.1). The shapes below faithfully
# mirror what `TaskUpdater.update_status`/`add_artifact` build internally
# (state / optional message / UTC timestamp) — a small, deliberate
# duplication, not a reinvention.
# ---------------------------------------------------------------------------


def _utc_timestamp() -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(datetime.now(timezone.utc))
    return ts


def _generic_agent_message(text: str) -> pb.Message:
    return pb.Message(
        role=pb.Role.ROLE_AGENT,
        message_id=str(uuid.uuid4()),
        parts=[pb.Part(text=text)],
    )


async def _apply_status_event(
    task_store: TaskStore,
    task_id: str,
    context_id: str,
    state: pb.TaskState,
    message: pb.Message | None = None,
) -> None:
    tm = TaskManager(task_store, ServerCallContext(), task_id, context_id, None)
    status = pb.TaskStatus(state=state, timestamp=_utc_timestamp())
    if message is not None:
        status.message.CopyFrom(message)
    await tm.save_task_event(
        pb.TaskStatusUpdateEvent(
            task_id=task_id, context_id=context_id, status=status
        )
    )


async def _apply_artifact_event(
    task_store: TaskStore,
    task_id: str,
    context_id: str,
    parts: list[pb.Part],
    name: str,
) -> None:
    tm = TaskManager(task_store, ServerCallContext(), task_id, context_id, None)
    artifact = pb.Artifact(artifact_id=str(uuid.uuid4()), name=name, parts=parts)
    await tm.save_task_event(
        pb.TaskArtifactUpdateEvent(
            task_id=task_id,
            context_id=context_id,
            artifact=artifact,
            append=False,
            last_chunk=True,
        )
    )


async def _apply_one_result(task_store: TaskStore, result: dict) -> None:
    """Applies one design-notes-§7.1 result payload
    (`{task_id, status, report_path, metadata}`) to `task_store`.

    Unknown `task_id` (server restarted mid-research and lost the
    in-memory task, or a duplicate/late result) is logged and returns —
    the caller (`poll_and_apply_results`'s handler) has already accepted
    the mailbox message, satisfying the idempotency requirement (design
    notes §7.1 crash/restart honesty: "a result for an unknown task-id is
    logged + acked").
    """
    task_id = result.get("task_id")
    status = result.get("status")
    task = await task_store.get(task_id, ServerCallContext())
    if task is None:
        logger.warning(
            "a2a result for unknown task_id %r (status=%r) — acked, no "
            "state change",
            task_id,
            status,
        )
        return
    context_id = task.context_id

    if status == "completed":
        report_path = result.get("report_path")
        try:
            report_md = Path(report_path).read_text(encoding="utf-8")
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                "a2a result task %s: report unreadable (%s)", task_id, exc
            )
            await _apply_status_event(
                task_store,
                task_id,
                context_id,
                pb.TaskState.TASK_STATE_FAILED,
                message=_generic_agent_message(REPORT_UNREADABLE_MESSAGE),
            )
            return

        metadata_value = struct_pb2.Value()
        json_format.ParseDict(result.get("metadata") or {}, metadata_value)
        await _apply_artifact_event(
            task_store,
            task_id,
            context_id,
            parts=[pb.Part(text=report_md), pb.Part(data=metadata_value)],
            name="a2a-research-report",
        )
        await _apply_status_event(
            task_store, task_id, context_id, pb.TaskState.TASK_STATE_COMPLETED
        )
        return

    if status in ("rejected", "failed"):
        metadata = result.get("metadata") or {}
        reason = metadata.get("reason") or RESULT_GENERIC_FAILURE_MESSAGE
        await _apply_status_event(
            task_store,
            task_id,
            context_id,
            pb.TaskState.TASK_STATE_FAILED,
            message=_generic_agent_message(reason),
        )
        return

    logger.warning(
        "a2a result task %s: unrecognized status %r — treated as failed",
        task_id,
        status,
    )
    await _apply_status_event(
        task_store,
        task_id,
        context_id,
        pb.TaskState.TASK_STATE_FAILED,
        message=_generic_agent_message(RESULT_GENERIC_FAILURE_MESSAGE),
    )


def _validate_result_payload(payload) -> dict:
    """Normalize a §7.1 result payload to a dict, then validate it (T-1952).

    Accepts str OR dict — Postel. The two sides of the wire disagreed on
    serialization: staging sends the result as a DICT (mailbox serializes it),
    while a real session json.dumps'd it into a STRING first. The stricter
    dict-only check turned that guess into a hard failure, so a VALID completed
    result — real research, 12 sources, trail committed — was retried 3× and
    DLQ'd as "malformed" (BT-6 E2E, 2026-07-22 17:43Z, task abb4c4aa).

    Dict is canonical (see sop/standard/a2a-research-request-handling.md step 6);
    the str branch exists so a serialization guess can never dead-letter a good
    result again. Validation after normalization is IDENTICAL for both shapes —
    a str that parses to a non-dict, or to a dict missing task_id/status, still
    fails, so this widens the accepted wire shape without weakening the contract.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            # Unparseable str is genuinely malformed — raise (retry → DLQ), but
            # name the cause so it is distinguishable from a shape mismatch.
            raise ValueError(
                f"malformed a2a result payload (str, not valid JSON): {payload!r}"
            ) from exc
    if not isinstance(payload, dict) or not payload.get("task_id") or not payload.get("status"):
        raise ValueError(f"malformed a2a result payload: {payload!r}")
    return payload


async def poll_and_apply_results(
    task_store: TaskStore, *, result_inbox: str = DEFAULT_RESULT_INBOX
) -> dict:
    """One drain pass of the specialist-a2a result mailbox (§7.1), applying
    each well-formed result to `task_store`.

    The mailbox handler is deliberately side-effect-free (validates shape
    + collects) — the actual (async) task-store mutation happens after
    `mailbox.consume` returns, since `consume`'s handler contract is
    synchronous. A malformed payload (missing `task_id`/`status`) raises
    inside the handler, which leaves it in `new/` for retry (and eventual
    DLQ) per `tools/mailbox.py`'s own semantics — distinct from an
    unknown-but-well-formed `task_id`, which is accepted (see
    `_apply_one_result`) rather than retried forever.
    """
    collected: list[dict] = []

    def _handler(msg: dict) -> None:
        collected.append(_validate_result_payload(msg.get("payload")))

    drain_result = await asyncio.to_thread(mailbox.consume, result_inbox, _handler)
    if drain_result.get("reason") == "inbox_missing":
        # T-2216: a missing inbox is a standing misconfiguration (a mistyped
        # or vanished hub path), not a transient lease contest. `_run_poll_loop`
        # (the only caller) discards this return value entirely, so left
        # unlogged it would spin silently forever — the exact failure-presents-
        # as-absence shape this ticket closes, one level up.
        logger.warning(
            "a2a result-poll: inbox %s does not exist — mailbox misconfigured, "
            "not a transient condition", result_inbox,
        )
    for payload in collected:
        await _apply_one_result(task_store, payload)
    return drain_result


async def _run_poll_loop(
    task_store: TaskStore,
    *,
    result_inbox: str = DEFAULT_RESULT_INBOX,
    interval: float = RESULT_POLL_INTERVAL_SECONDS,
) -> None:
    while True:
        try:
            await poll_and_apply_results(task_store, result_inbox=result_inbox)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a poll-iteration failure must not
            # kill the loop; log and retry next interval.
            logger.exception(
                "a2a result-poll iteration failed; will retry next interval"
            )
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Loopback-only enforcement, structural (T-2536).
#
# `resolve_bind_config` (below) only guards `main()`'s own CLI entrypoint:
# it reads SPECIALIST_A2A_HOST/SPECIALIST_A2A_ALLOW_NONLOOPBACK at process-start and
# refuses to even call `uvicorn.run(...)` with a non-loopback host. That
# left a gap -- any OTHER entrypoint that constructs/serves the app built
# by `build_app()` (`uvicorn ...:build_app --factory --host 0.0.0.0`,
# gunicorn, a container CMD, a test harness) never runs `main()` and so
# never consults those env vars at all; uvicorn's own `--host` CLI flag
# binds the socket directly. A construction-time check inside `build_app()`
# itself wouldn't close this either -- it would still only ever see
# SPECIALIST_A2A_HOST, never uvicorn's own flag.
#
# The only property that's actually entrypoint-agnostic is per-CONNECTION
# enforcement: refuse any request whose ASGI peer address
# (`scope["client"]`) is non-loopback, regardless of what host string the
# app was bound to. `LoopbackOnlyMiddleware` is installed directly on the
# `Starlette` app object `build_app()` returns, so every entrypoint that
# serves that object inherits the guard -- there is no second copy to keep
# in sync and no CLI flag that can route around it.
# ---------------------------------------------------------------------------

NONLOOPBACK_REFUSED_MESSAGE = (
    "This seat only accepts loopback connections at rung 1; non-loopback "
    "access requires the cross-seat identity layer, which is not "
    "currently configured."
)


def _is_loopback_addr(host: str) -> bool:
    """True iff `host` (a numeric ASGI peer address -- ASGI servers never
    hand middleware a hostname like "localhost", only an IP) is loopback.

    Uses `ipaddress.is_loopback` (127.0.0.0/8, ::1) rather than the
    hostname-string `LOOPBACK_HOSTS` set above -- that set is for
    validating a configured BIND hostname (`resolve_bind_config`, a
    different concern); a real per-request peer address is always
    numeric. Also honors the IPv4-mapped-IPv6 form (`::ffff:127.0.0.1`),
    which `ipaddress`'s own `is_loopback` does not unwrap by itself.
    """
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    mapped = getattr(addr, "ipv4_mapped", None)
    return mapped is not None and mapped.is_loopback


def _authorize_nonloopback_request(scope: dict) -> bool:
    """Identity-hook seam (T-2536): decides whether a non-loopback request
    should be allowed anyway.

    No identity layer exists yet -- the ORG-047 cross-seat passport is
    Proposed/unbuilt -- so this unconditionally returns False today:
    refuse-by-default. This is deliberately a single stubbable predicate
    (not a bespoke credential/token/mTLS check -- explicitly out of scope)
    taking the raw ASGI `scope` rather than a parsed credential, so
    whatever transport-level identity the eventual passport reads (a
    tailnet identity header, a peer cert, ...) can be wired in here
    without this signature -- or `LoopbackOnlyMiddleware`, which only
    ever calls this one function -- needing to change again.
    """
    return False


class LoopbackOnlyMiddleware:
    """Pure ASGI middleware: refuses any HTTP request whose ASGI peer
    (`scope["client"]`) is non-loopback, unless
    `_authorize_nonloopback_request` grants it. See the module-level
    comment above this class for why per-connection enforcement (not a
    construction-time or CLI-level check) is the only entrypoint-agnostic
    shape.

    A missing/unparseable `scope["client"]` is treated as non-loopback
    (refuse-by-default) rather than allowed through -- the safe direction
    to fail in when the peer address can't be verified.
    """

    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        client = scope.get("client")
        host = client[0] if client else None
        if host is not None and _is_loopback_addr(host):
            await self._app(scope, receive, send)
            return

        if _authorize_nonloopback_request(scope):
            await self._app(scope, receive, send)
            return

        response = JSONResponse(
            {"error": NONLOOPBACK_REFUSED_MESSAGE}, status_code=403
        )
        await response(scope, receive, send)


def build_app(
    *,
    request_inbox: str = DEFAULT_REQUEST_INBOX,
    result_inbox: str = DEFAULT_RESULT_INBOX,
) -> Starlette:
    """Assembles the Starlette app: JSON-RPC routes + card discovery route
    + the background result-poll loop (started/stopped via ASGI lifespan)
    + the T-2536 loopback-only ASGI middleware (see the block comment
    above `LoopbackOnlyMiddleware` for why this, and not
    `resolve_bind_config`, is the load-bearing guard against non-loopback
    access regardless of which entrypoint serves this app).
    """
    card_dict = load_agent_card_dict()
    native_card = build_native_agent_card(card_dict)
    task_store = InMemoryTaskStore()

    handler = DefaultRequestHandler(
        agent_executor=ResearchRequestExecutor(request_inbox=request_inbox),
        task_store=task_store,
        agent_card=native_card,
    )

    # enable_v0_3_compat=True additionally exposes the slash-style method
    # names (message/send, tasks/get, tasks/cancel, message/stream) named
    # in design notes §2.1, on the same /a2a endpoint, alongside the SDK's
    # native PascalCase methods (SendMessage, GetTask, ...). Both delegate
    # to the same `handler` — see PROVISION.md SDK surface notes.
    jsonrpc_routes = create_jsonrpc_routes(
        request_handler=handler,
        rpc_url=RPC_URL,
        enable_v0_3_compat=True,
    )
    card_route = create_agent_card_file_route()

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        poll_task = asyncio.create_task(
            _run_poll_loop(task_store, result_inbox=result_inbox)
        )
        try:
            yield
        finally:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task

    return Starlette(
        routes=[*jsonrpc_routes, card_route],
        middleware=[Middleware(LoopbackOnlyMiddleware)],
        lifespan=lifespan,
    )


def resolve_bind_config() -> tuple[str, int]:
    """Resolves (host, port) from env, refusing a non-loopback host unless
    explicitly overridden.

    This is `main()`'s own early, friendly fail-fast for the common CLI
    path -- it raises before the process even opens a listening socket,
    which is a nicer failure than a 403 on the first request. It is NOT
    the only guard: since T-2536, `build_app()` installs
    `LoopbackOnlyMiddleware` directly on the app object, which is what
    actually enforces loopback-only regardless of which entrypoint serves
    that app (see the block comment above `LoopbackOnlyMiddleware`).

    Raises:
        ValueError: if the resolved host isn't loopback and
            SPECIALIST_A2A_ALLOW_NONLOOPBACK isn't set to a truthy value.
    """
    host = os.environ.get(HOST_ENV, DEFAULT_HOST)
    port = int(os.environ.get(PORT_ENV, str(DEFAULT_PORT)))
    allow_nonloopback = os.environ.get(ALLOW_NONLOOPBACK_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    if host not in LOOPBACK_HOSTS and not allow_nonloopback:
        raise ValueError(
            f"Refusing to bind non-loopback host {host!r} — "
            f"rung-1 posture is loopback-only (design notes §2.1/§3). "
            f"Set {ALLOW_NONLOOPBACK_ENV}=1 to override."
        )

    return host, port


def main() -> None:
    import uvicorn

    host, port = resolve_bind_config()
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting specialist A2A server on %s:%s", host, port)
    uvicorn.run(build_app(), host=host, port=port)


if __name__ == "__main__":
    main()
