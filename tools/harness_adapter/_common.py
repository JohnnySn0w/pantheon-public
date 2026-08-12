"""Shared mechanics for harness bindings; provider semantics stay in bindings."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from tools import mailbox as pantheon_mailbox
from tools.harness_adapter.types import (
    DeliveryReceipt,
    ErrorCategory,
    ExecutionError,
    ExecutionResult,
    HarnessEvent,
    HarnessName,
    InboundRequest,
    ProviderBillingEvidence,
    SessionHandle,
)


def parse_json_object(line: str) -> tuple[Mapping[str, Any] | None, str | None]:
    stripped = line.strip()
    if not stripped:
        return None, None
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON event: {exc.msg}"
    if not isinstance(value, dict):
        return None, "structured event is not a JSON object"
    return value, None


def classify_error(text: str) -> tuple[ErrorCategory, bool]:
    lower = text.lower()
    if any(token in lower for token in ("rate limit", "rate_limit", "429")):
        return "rate_limit", True
    if any(token in lower for token in ("quota", "usage limit", "weekly limit")):
        return "quota", True
    if any(token in lower for token in ("unauthorized", "authentication", "login required")):
        return "auth", False
    if "approval" in lower or "permission prompt" in lower:
        return "approval", False
    if any(token in lower for token in ("sandbox", "permission denied", "operation not permitted")):
        return "sandbox", False
    if any(token in lower for token in ("config", "configuration", "no such file")):
        return "config", False
    return "provider", False


def finalize_events(
    harness: HarnessName,
    events: Iterable[HarnessEvent],
    *,
    returncode: int,
    stderr: str,
    provider_billing: ProviderBillingEvidence | None = None,
) -> ExecutionResult:
    captured = tuple(events)
    session_id = next(
        (event.session_id for event in captured if event.session_id),
        None,
    )
    session_cwd: Path | None = None
    for event in captured:
        raw_cwd = event.raw.get("cwd")
        if isinstance(raw_cwd, str) and raw_cwd:
            session_cwd = Path(raw_cwd)
            break
    session = (
        SessionHandle(
            session_id=session_id,
            name=None,
            cwd=session_cwd,
            resumable=True,
        )
        if session_id
        else None
    )

    final_message = next(
        (
            event.message
            for event in reversed(captured)
            if event.kind in {"message_completed", "turn_completed"} and event.message
        ),
        None,
    )
    usage = next(
        (event.usage for event in reversed(captured) if event.usage is not None),
        None,
    )
    model: str | None = None
    for event in reversed(captured):
        raw_model = event.raw.get("model")
        if isinstance(raw_model, str) and raw_model:
            model = raw_model
            break
        raw_models = event.raw.get("modelUsage")
        if isinstance(raw_models, dict) and raw_models:
            model = next((str(key) for key in raw_models), None)
            if model:
                break

    failure_event = next(
        (
            event
            for event in reversed(captured)
            if event.kind in {"turn_failed", "error"}
        ),
        None,
    )
    completed = any(event.kind == "turn_completed" for event in captured)
    error: ExecutionError | None = None
    status = "completed"

    if returncode < 0:
        status = "interrupted"
        error = ExecutionError(
            category="interrupted",
            message=f"process interrupted by signal {-returncode}",
            retryable=True,
        )
    elif failure_event is not None:
        status = "failed"
        message = failure_event.message or stderr.strip() or "harness event reported failure"
        raw_category = failure_event.raw.get("category")
        if raw_category in {
            "auth",
            "quota",
            "rate_limit",
            "approval",
            "sandbox",
            "config",
            "tool",
            "provider",
            "interrupted",
            "unknown",
        }:
            category = raw_category
            retryable = bool(failure_event.raw.get("retryable", False))
        else:
            category, retryable = classify_error(message)
        error = ExecutionError(
            category=category,
            message=message,
            retryable=retryable,
            raw=failure_event.raw,
        )
    elif returncode != 0:
        status = "failed"
        message = stderr.strip() or f"harness exited with status {returncode}"
        category, retryable = classify_error(message)
        error = ExecutionError(
            category=category,
            message=message,
            retryable=retryable,
        )
    elif not completed:
        status = "failed"
        error = ExecutionError(
            category="provider",
            message="process exited without a structural turn-completed event",
            retryable=False,
        )

    return ExecutionResult(
        harness=harness,
        status=status,
        final_message=final_message,
        session=session,
        usage=usage,
        model=model,
        returncode=returncode,
        error=error,
        provider_billing=provider_billing,
    )


def deliver_to_mailbox(
    harness: HarnessName,
    request: InboundRequest,
    *,
    default_inbox: Path | None,
    default_sender: str,
) -> DeliveryReceipt:
    inbox_value = request.reply_route.get("recipient_inbox")
    inbox = Path(inbox_value) if inbox_value else default_inbox
    if inbox is None:
        return DeliveryReceipt(
            harness=harness,
            correlation_id=request.correlation_id,
            state="rejected",
            mechanism="maildir",
            retryable=False,
            evidence={"reason": "no recipient_inbox configured"},
        )

    sender = request.reply_route.get("sender", default_sender)
    subject = request.reply_route.get("subject", f"inbound:{request.correlation_id}")
    severity = request.reply_route.get("severity", "normal")
    payload = {
        "kind": "harness_inbound",
        "correlation_id": request.correlation_id,
        "source": request.source,
        "message": request.message,
        "reply_route": dict(request.reply_route),
    }
    try:
        message_id = pantheon_mailbox.send(
            str(inbox),
            payload,
            sender,
            subject=subject,
            severity=severity,
        )
    except (OSError, ValueError, pantheon_mailbox.MailboxNotFound) as exc:
        return DeliveryReceipt(
            harness=harness,
            correlation_id=request.correlation_id,
            state="failed",
            mechanism="maildir",
            retryable=isinstance(exc, OSError),
            evidence={
                "recipient_inbox": str(inbox),
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
    return DeliveryReceipt(
        harness=harness,
        correlation_id=request.correlation_id,
        state="durably_queued",
        mechanism="maildir",
        retryable=False,
        evidence={
            "recipient_inbox": str(inbox),
            "message_id": message_id,
        },
    )
