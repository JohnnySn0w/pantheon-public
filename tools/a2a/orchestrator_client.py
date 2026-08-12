"""Orchestrator seat A2A client — rung-1 round-trip against specialist's server.

Design: knowledge/episodic/2026-07-20-a2a-rung1-design/notes.md section 2.3
(research-request skill contract) and section 5 BT-2. Ticket: T-1887 (BT-2
of the T-1882 rung ladder). Server: tools/a2a/specialist_seat_server.py (T-1886),
whose finalized Agent Card is tools/a2a/agent_card.json.

Fetches the specialist Agent Card, sends a `research-request` Message (a
TextPart with the natural-language question plus a DataPart carrying the
full structured §2.3 payload), follows the resulting Task to a terminal
state, and prints the returned artifact. Scope at rung 1: prove
Message -> Task(SUBMITTED -> WORKING -> COMPLETED) two-seat mechanics on
one host.

T-1889 (BT-4) adds INPUT_REQUIRED/AUTH_REQUIRED pause/resume: by default
(`--on-pause report`) a paused task is still just reported cleanly, same as
BT-2/BT-3. `--on-pause resume` drives a paused task to COMPLETED by
sending a follow-up Message on the same task; `--on-pause cancel` drives
it to CANCELED via `tasks/cancel`. Rung 1's human is orchestrator driving this
client — no Telegram wiring (design notes §2.4's rung boundary).

Usage: see the "Running the orchestrator-seat client" section of PROVISION.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from typing import Sequence

import httpx
from google.protobuf import json_format, struct_pb2

from a2a.client import A2AClientError, A2ACardResolver, ClientFactory
from a2a.client.card_resolver import AgentCardResolutionError
from a2a.client.client import Client
from a2a.types import a2a_pb2 as pb


# Default matches agent_card.json's own "url" (127.0.0.1:41100) and
# specialist_seat_server.py's DEFAULT_HOST/DEFAULT_PORT — hardcoded rather than
# imported from the server module to keep the client decoupled from the
# server's internals (they are separate deployables/venvs).
DEFAULT_BASE_URL = "http://127.0.0.1:41100"
BASE_URL_ENV = "SPECIALIST_A2A_URL"

# Per agent_card.json "url" / design notes §2.1 ("POST /a2a").
RPC_PATH = "/a2a"

TERMINAL_STATES = {
    pb.TaskState.TASK_STATE_COMPLETED,
    pb.TaskState.TASK_STATE_FAILED,
    pb.TaskState.TASK_STATE_CANCELED,
    pb.TaskState.TASK_STATE_REJECTED,
}
PAUSE_STATES = {
    pb.TaskState.TASK_STATE_INPUT_REQUIRED,
    pb.TaskState.TASK_STATE_AUTH_REQUIRED,
}

POLL_INTERVAL_S = 0.5
POLL_TIMEOUT_S = 120.0

DEFAULT_QUESTION = (
    "What changed in the MCP spec release scheduled 2026-07-28?"
)


def resolve_base_url() -> str:
    """Resolves the specialist A2A base URL from env, defaulting to the
    loopback production value."""
    import os

    return os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL)


async def build_client(base_url: str) -> Client:
    """Fetches the Agent Card from `base_url` and builds an SDK client.

    Always resolves the card then rewrites `supported_interfaces[].url` to
    `base_url + RPC_PATH`, rather than trusting the card's own declared
    interface url. Against the live production server this is a no-op
    (the card's url IS 127.0.0.1:41100/a2a) — it empirically confirms the
    design's "straight card-based discovery reaches it" claim. Against any
    base_url whose served card declares a different url (e.g. an
    ephemeral-port in-process test server, per PROVISION.md's SDK surface
    notes on the client-side card resolver), the rewrite is what actually
    makes the client reach the right endpoint. One code path handles both,
    which is also what makes SPECIALIST_A2A_URL override safe in general.

    Raises:
        AgentCardResolutionError: if the server is unreachable or the card
            can't be fetched/parsed (raised by A2ACardResolver.get_agent_card
            with an already-clean message — no wrapping needed here).
    """
    async with httpx.AsyncClient() as hc:
        resolver = A2ACardResolver(hc, base_url)
        card = await resolver.get_agent_card()

    rpc_url = base_url.rstrip("/") + RPC_PATH
    for iface in card.supported_interfaces:
        iface.url = rpc_url

    return ClientFactory().create(card)


def build_message(
    *,
    question: str,
    depth: str,
    domains_hint: Sequence[str] | None,
    recency_days: int | None,
    requester_context: str | None,
) -> pb.Message:
    """Builds a research-request Message per design notes §2.3.

    Sends BOTH a TextPart (what RequestContext.get_user_input() reads —
    confirmed from the installed SDK that it only concatenates TextParts,
    ignoring DataParts) and a DataPart carrying the full structured
    payload. The rung-1 stub only reads the TextPart; the DataPart is
    forward-compatible with BT-5's real research-request skill at zero
    cost, exercising the intended real payload shape now.
    """
    payload: dict = {"question": question, "depth": depth}
    scope: dict = {}
    if domains_hint:
        scope["domains_hint"] = list(domains_hint)
    if recency_days is not None:
        scope["recency_days"] = recency_days
    if scope:
        payload["scope"] = scope
    if requester_context:
        payload["requester_context"] = requester_context

    data_value = struct_pb2.Value()
    json_format.ParseDict(payload, data_value)

    return pb.Message(
        role=pb.Role.ROLE_USER,
        message_id=str(uuid.uuid4()),
        parts=[
            pb.Part(text=question),
            pb.Part(data=data_value),
        ],
    )


def build_resume_message(task: pb.Task, *, text: str) -> pb.Message:
    """Builds a follow-up Message that resumes a paused task.

    Per design notes §2.4's "HITL wiring" paragraph and the D11 SDK trace
    of the installed a2a-sdk 1.1.1 (`DefaultRequestHandler.
    _setup_message_execution`): there is no dedicated "resume" RPC — a
    paused task resumes via an ordinary `message/send`/`message/stream`
    call whose Message carries the SAME `task_id`/`context_id` as the
    paused task. `role=ROLE_USER` because this is orchestrator (the rung-1 human)
    speaking to the seat, same as the original request.
    """
    return pb.Message(
        role=pb.Role.ROLE_USER,
        message_id=str(uuid.uuid4()),
        task_id=task.id,
        context_id=task.context_id,
        parts=[pb.Part(text=text)],
    )


async def resume_task(
    client: Client,
    task: pb.Task,
    *,
    text: str = "approved",
    on_event=None,
    poll_timeout_s: float = POLL_TIMEOUT_S,
) -> pb.Task:
    """Resumes a paused task by sending a follow-up Message and following
    it to a terminal state. Reuses `send_and_follow` — resume is just
    another `message/send` call, per `build_resume_message`'s docstring."""
    message = build_resume_message(task, text=text)
    request = pb.SendMessageRequest(message=message)
    return await send_and_follow(
        client, request, on_event=on_event, poll_timeout_s=poll_timeout_s
    )


async def cancel_task(client: Client, task: pb.Task) -> pb.Task:
    """Cancels a task via `tasks/cancel`. Works on a paused
    (INPUT_REQUIRED/AUTH_REQUIRED) task with no server-side special-casing
    — confirmed against the installed SDK: `DefaultRequestHandler.
    on_cancel_task`'s terminal-state guard only excludes
    COMPLETED/CANCELED/FAILED/REJECTED."""
    return await client.cancel_task(pb.CancelTaskRequest(id=task.id))


async def send_and_follow(
    client: Client,
    request: pb.SendMessageRequest,
    *,
    poll_timeout_s: float = POLL_TIMEOUT_S,
    poll_interval_s: float = POLL_INTERVAL_S,
    on_event=None,
) -> pb.Task:
    """Sends the message and follows the task to a terminal state.

    Mirrors tools/test_a2a_specialist_seat_server.py::TestStubRoundTrip: the
    send_message stream yields the initial Task plus intermediate
    status_update events, but the terminal COMPLETED state (with
    artifacts) is only reliably available via a follow-up get_task call
    after the stream is exhausted — confirmed against this SDK version,
    not assumed. Polls get_task with a bounded timeout in case a
    (future, non-stub) skill settles slightly after the stream closes.

    `on_event`, if given, is called with each raw StreamResponse (for
    caller-side progress printing); returning early on error still lets
    the caller see what happened.
    """
    task_id = None
    async for resp in client.send_message(request):
        if on_event is not None:
            on_event(resp)
        which = resp.WhichOneof("payload")
        if which == "task":
            task_id = resp.task.id
        elif which == "status_update":
            if task_id is None:
                task_id = resp.status_update.task_id

    if task_id is None:
        raise RuntimeError(
            "server never returned a task id in the message/send stream"
        )

    task = await client.get_task(pb.GetTaskRequest(id=task_id))
    deadline = asyncio.get_event_loop().time() + poll_timeout_s
    while (
        task.status.state not in TERMINAL_STATES
        and task.status.state not in PAUSE_STATES
        and asyncio.get_event_loop().time() < deadline
    ):
        await asyncio.sleep(poll_interval_s)
        task = await client.get_task(pb.GetTaskRequest(id=task_id))

    return task


def describe_status_update(status_update: pb.TaskStatusUpdateEvent) -> str:
    """Renders a `status_update` stream event for progress display.

    Distinguishes an intermediate WORKING-phase progress event (per design
    notes §5 BT-3 — carries a human-readable message, e.g. "lane
    dispatched") from a terminal state transition (COMPLETED/FAILED/
    CANCELED/REJECTED — no message at rung 1), so a human watching the
    held SSE stream can tell "still working, here's what's happening" from
    "the task just ended".
    """
    state = status_update.status.state
    state_name = pb.TaskState.Name(state)
    label = "terminal" if state in TERMINAL_STATES else "progress"

    text = None
    if status_update.status.HasField("message"):
        for part in status_update.status.message.parts:
            if part.HasField("text"):
                text = part.text
                break

    if text:
        return f"[status] ({label}) state={state_name} — {text}"
    return f"[status] ({label}) state={state_name}"


def format_task_output(task: pb.Task) -> str:
    """Formats a (terminal or paused) Task for printing: the report
    TextPart(s), then the metadata DataPart(s) as pretty JSON."""
    state_name = pb.TaskState.Name(task.status.state)
    lines = [f"task id: {task.id}", f"state: {state_name}"]

    if task.status.state in PAUSE_STATES:
        lines.append(
            "\nTask paused pending input (INPUT_REQUIRED/AUTH_REQUIRED — "
            "resume with --on-pause resume or cancel with --on-pause "
            "cancel; see PROVISION.md). Status message:"
        )
        if task.status.HasField("message"):
            for part in task.status.message.parts:
                if part.HasField("text"):
                    lines.append(part.text)
        return "\n".join(lines)

    if task.status.state == pb.TaskState.TASK_STATE_FAILED:
        lines.append("\nTask FAILED. Status message:")
        if task.status.HasField("message"):
            for part in task.status.message.parts:
                if part.HasField("text"):
                    lines.append(part.text)
        return "\n".join(lines)

    if not task.artifacts:
        lines.append("\n(no artifacts returned)")
        return "\n".join(lines)

    for artifact in task.artifacts:
        lines.append(f"\n--- artifact: {artifact.name or artifact.artifact_id} ---")
        for part in artifact.parts:
            if part.HasField("text"):
                lines.append(part.text)
            elif part.HasField("data"):
                metadata = json_format.MessageToDict(part.data)
                lines.append(json.dumps(metadata, indent=2))

    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Drive a research-request Message against the specialist A2A seat "
            "and print the returned artifact (T-1887 rung-1 round-trip)."
        )
    )
    parser.add_argument(
        "--base-url",
        default=resolve_base_url(),
        help=f"Specialist A2A base URL (env {BASE_URL_ENV}; default %(default)s)",
    )
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument(
        "--poll-timeout-s",
        type=float,
        default=POLL_TIMEOUT_S,
        help=(
            "Max seconds to poll the task toward a settled state before "
            "reporting whatever state it is in (default %(default)s). Under "
            "BT-5 a passing request legitimately STAYS WORKING while async "
            "research runs — a short timeout + WORKING is a normal outcome, "
            "not a failure (T-1942). Tests use a small value for speed."
        ),
    )
    parser.add_argument(
        "--depth", choices=["quick", "standard", "deep"], default="standard"
    )
    parser.add_argument(
        "--domains-hint",
        default="modelcontextprotocol.io",
        help="Comma-separated list; empty string omits scope.domains_hint",
    )
    parser.add_argument("--recency-days", type=int, default=30)
    parser.add_argument(
        "--requester-context",
        default="T-1887 rung-1 round-trip smoke — proving two-seat A2A mechanics",
    )
    parser.add_argument(
        "--on-pause",
        choices=["report", "resume", "cancel"],
        default="report",
        help=(
            "Action when the task pauses at INPUT_REQUIRED/AUTH_REQUIRED "
            "(T-1889/BT-4): 'report' (default) prints the pending-approval "
            "status and exits, same as BT-2/BT-3; 'resume' sends a "
            "follow-up Message (--resume-text) to drive it to COMPLETED; "
            "'cancel' calls tasks/cancel to drive it to CANCELED."
        ),
    )
    parser.add_argument(
        "--resume-text",
        default="approved",
        help="TextPart content of the follow-up Message for --on-pause resume",
    )
    return parser.parse_args(argv)


async def main_async(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    domains_hint = [d.strip() for d in args.domains_hint.split(",") if d.strip()]

    try:
        client = await build_client(args.base_url)
    except (AgentCardResolutionError, httpx.HTTPError) as exc:
        print(
            f"error: could not reach specialist A2A server at {args.base_url}: {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        message = build_message(
            question=args.question,
            depth=args.depth,
            domains_hint=domains_hint,
            recency_days=args.recency_days,
            requester_context=args.requester_context,
        )
        request = pb.SendMessageRequest(message=message)

        def _on_event(resp: pb.StreamResponse) -> None:
            which = resp.WhichOneof("payload")
            if which == "task":
                print(
                    f"[task] id={resp.task.id} "
                    f"state={pb.TaskState.Name(resp.task.status.state)}",
                    file=sys.stderr,
                )
            elif which == "status_update":
                print(describe_status_update(resp.status_update), file=sys.stderr)

        task = await send_and_follow(
            client, request, on_event=_on_event,
            poll_timeout_s=args.poll_timeout_s,
        )

        # T-1889 (BT-4): drive an explicit --on-pause action when the task
        # pauses. Default ('report') preserves BT-2/BT-3 behavior exactly —
        # print the pause and exit without touching the task further.
        if task.status.state in PAUSE_STATES and args.on_pause != "report":
            if args.on_pause == "resume":
                print(
                    f"[pause] task {task.id} paused; sending follow-up "
                    "Message to resume",
                    file=sys.stderr,
                )
                task = await resume_task(
                    client, task, text=args.resume_text, on_event=_on_event,
                    poll_timeout_s=args.poll_timeout_s,
                )
            elif args.on_pause == "cancel":
                print(
                    f"[pause] task {task.id} paused; canceling",
                    file=sys.stderr,
                )
                task = await cancel_task(client, task)
    except (A2AClientError, httpx.HTTPError, ConnectionError) as exc:
        print(
            f"error: request to {args.base_url} failed: {exc}",
            file=sys.stderr,
        )
        return 1
    finally:
        await client.close()

    print(format_task_output(task))

    # Exit-code contract (T-1942, BT-5 revision):
    #   0 — COMPLETED; CANCELED when cancel was the requested on-pause action;
    #       or WORKING: under BT-5 a passing request is STAGED to the research
    #       seat and legitimately stays WORKING while async research runs —
    #       staged-successfully is a SUCCESS of the submission, not a failure.
    #       Callers needing to distinguish staged-vs-complete parse the
    #       `state:` line format_task_output already prints on stdout.
    #   1 — everything else (FAILED, REJECTED, unrequested CANCELED, a pause
    #       left unresolved under --on-pause report, transport errors).
    if task.status.state == pb.TaskState.TASK_STATE_COMPLETED:
        return 0
    if (
        args.on_pause == "cancel"
        and task.status.state == pb.TaskState.TASK_STATE_CANCELED
    ):
        return 0
    if task.status.state == pb.TaskState.TASK_STATE_WORKING:
        print(
            "[staged] task is WORKING — request accepted and staged for "
            "async research; poll for the result separately (rc=0, T-1942)",
            file=sys.stderr,
        )
        return 0
    return 1


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
