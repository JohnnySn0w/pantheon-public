"""Handoff-first: ask a live session to write its HANDOFF before it is killed (T-5069).

A runtime swap that kills a live session (``harness.py restart``, the runtime
controller's fallback-return) must not leave the successor waking on a stale
HANDOFF. This module is the one place that asks the dying session for a fresh
handoff and waits, bounded, for the ack.

The ack is the HANDOFF file's mtime moving, the same structural witness
``tools/handoff_and_restart.sh`` waits on (no flag file). The prompt is
delivered through ``runtime_controller.submit_to_runtime`` (send_keys_submit's
submit-verify transport), so the same pane-dialog guards apply.

Two policies sit on top of ``request_handoff``:

* operator restart (``harness.py restart``): unconfirmed -> refuse unless the
  operator passes ``--skip-handoff`` (enforced in harness.py).
* automatic return (controller fallback-return): unconfirmed -> proceed only if
  the existing HANDOFF is newer than the live session's start, else defer
  (``auto_swap_handoff_gate``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

DEFAULT_ACK_TIMEOUT_SECONDS = 300.0
DEFAULT_POLL_INTERVAL_SECONDS = 2.0

HANDOFF_PROMPT_TEMPLATE = (
    "A runtime swap is about to end this session. Please write a fresh {path} capturing "
    "current state, in-flight work, and immediate next actions for the next session. When "
    "done, say \"handoff written\" and stop. Rewrite the file even if nothing changed since "
    "your last handoff: the swap waits for the file's modification time, not for your reply."
)


@dataclass(frozen=True)
class HandoffOutcome:
    confirmed: bool
    reason: str
    handoff_path: Path


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def handoff_newer_than(handoff_path: Path, session_started_at: datetime) -> bool:
    """True when the HANDOFF file exists and was written after the session began."""
    mtime = _mtime_ns(handoff_path)
    if mtime is None:
        return False
    return mtime / 1e9 > session_started_at.timestamp()


def request_handoff(
    handoff_path: Path,
    submit_fn: Callable[[str], Any],
    *,
    timeout_seconds: float = DEFAULT_ACK_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    prompt: str | None = None,
) -> HandoffOutcome:
    """Send the handoff prompt via ``submit_fn`` and wait for the file's mtime to move.

    ``prompt`` (a template with ``{path}``) replaces the default swap wording; a routine
    checkpoint is not a swap (T-6115).

    Never raises for a delivery failure: it returns ``confirmed=False`` with the
    reason, so callers apply their own policy (refuse / defer / proceed).
    """
    baseline = _mtime_ns(handoff_path)
    try:
        submit_fn((prompt or HANDOFF_PROMPT_TEMPLATE).format(path=handoff_path))
    except Exception as exc:  # noqa: BLE001 - any delivery failure is "not confirmed"
        return HandoffOutcome(False, f"handoff prompt not delivered: {type(exc).__name__}: {exc}", handoff_path)
    deadline = monotonic() + timeout_seconds
    while True:
        current = _mtime_ns(handoff_path)
        if current is not None and current != baseline:
            return HandoffOutcome(True, "handoff mtime moved", handoff_path)
        if monotonic() >= deadline:
            return HandoffOutcome(
                False, f"no handoff ack within {timeout_seconds:g}s (mtime unchanged)", handoff_path
            )
        sleep(poll_interval_seconds)


def binding_handoff_path(binding: Any) -> Path:
    return Path(binding.account_home) / "HANDOFF.md"


def request_handoff_for_binding(
    binding: Any,
    *,
    state_root: Path | None = None,
    context: Any = None,
    timeout_seconds: float = DEFAULT_ACK_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> HandoffOutcome:
    """``request_handoff`` wired to the persona's live pane via the controller transport."""
    from tools import runtime_controller as rc
    from tools.harness_adapter.types import ExecutionRequest

    path = binding_handoff_path(binding)

    def submit(prompt: str) -> Any:
        handle = rc.recorded_runtime_handle(binding, state_root=state_root)
        return rc.submit_to_runtime(
            handle,
            ExecutionRequest(prompt=prompt, cwd=Path(binding.account_home)),
            persona=binding.persona,
            state_root=state_root,
            context=context,
        )

    return request_handoff(
        path, submit,
        timeout_seconds=timeout_seconds, poll_interval_seconds=poll_interval_seconds,
        sleep=sleep, monotonic=monotonic,
    )


def auto_swap_handoff_gate(
    binding: Any,
    *,
    state_root: Path | None = None,
    context: Any = None,
    request_fn: Callable[..., HandoffOutcome] | None = None,
    log_fn: Callable[[str, dict[str, Any]], None] | None = None,
    **request_kwargs: Any,
) -> bool:
    """Automatic-swap policy: True when it is safe to kill the live session.

    Safe = the session confirmed a fresh handoff, OR (confirmation failed and) the
    existing HANDOFF is newer than the live session's start. Otherwise False: the
    caller defers and retries later.
    """
    from tools import runtime_controller as rc

    outcome = (request_fn or request_handoff_for_binding)(
        binding, state_root=state_root, context=context, **request_kwargs
    )
    if outcome.confirmed:
        return True
    fallback_ok = False
    try:
        started_at = rc.recorded_runtime_handle(binding, state_root=state_root).started_at
        fallback_ok = handoff_newer_than(outcome.handoff_path, started_at)
    except Exception:  # noqa: BLE001 - unknown session start => not provably fresh => defer
        fallback_ok = False
    if log_fn is not None:
        log_fn(
            "handoff_first_unconfirmed",
            {"reason": outcome.reason, "existing_handoff_newer_than_session": fallback_ok,
             "action": "proceed" if fallback_ok else "defer"},
        )
    return fallback_ok
