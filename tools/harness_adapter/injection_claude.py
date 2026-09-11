"""Claude Code native encoder for the Concordance injection contract (T-3242).

ORG-073 §4: harness bindings are separate native encoders — each parses the
native hook stdin, constructs an :class:`InjectionRequest`, and converts an
:class:`InjectionResult` into the harness-native response shape. This module
is the Claude Code binding for the ``UserPromptSubmit`` hook event, written
against MEASURED wire reality: the on-disk capture fixtures under
``knowledge/episodic/2026-08-21-org-073-v2-poc-prep/ups-capture-fixtures/``
(two live fires on the claude seat, T-3241 §4 annotation), not against
documentation.

Observed stdin fields (both fixture fires, byte-identical payloads)::

    session_id, transcript_path, cwd, prompt_id, permission_mode,
    hook_event_name, prompt

Faithful parsing, no validation: this encoder never raises on payload shape
and never pre-validates. Missing / blank / non-str required fields produce a
request that fails ``InjectionRequest.missing_required()``; the contract's
``HookInjectionProvider.invoke()`` owns dispatch on gaps (typed no-op), per
the T-3240 split of duties.

``InjectionRequest.native`` carries the ENTIRE stdin payload as a shallow
copy — mapped keys included, original key order, values untouched. The typed
fields are lossy projections (e.g. the ``cwd`` string becomes a ``Path`` or
``None``); ``native`` stays the complete source of record. Consequence a
maintainer should expect: ``request.native["prompt"]`` and
``request.prompt_text`` return the same string via different accessors, and
mutating a caller-held nested value after parsing is visible through
``native`` (shallow copy, not deep).

Model-silence (ORG-073 §3) is routed through the ONE sanctioned seam,
:func:`tools.harness_adapter.injection.model_visible_text`: only an
:class:`Injection` can produce non-empty stdout. A no-op or backend failure
renders as ``("", 0)`` — never an "explicit nothing relevant" message, never
stderr, never a non-zero exit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from tools.harness_adapter.injection import (
    InjectionRequest,
    InjectionResult,
    model_visible_text,
)
from tools.harness_adapter.types import HarnessName


def request_from_native(
    stdin_json: Mapping[str, Any],
    *,
    harness: HarnessName = "claude",
) -> InjectionRequest:
    """Parse a Claude Code UserPromptSubmit stdin payload into an InjectionRequest.

    Faithful parsing, no validation (T-3240 contract split): missing / blank /
    non-str required fields pass through verbatim (absent keys become ``""``)
    and are flagged by ``missing_required()`` at ``invoke()`` time — this
    function never raises on payload shape. A non-``Mapping`` payload (a JSON
    array or scalar off the wire) degrades to an empty-required request rather
    than raising, keeping the hook wire un-crashable.

    ``native`` receives the entire payload as an order-preserving shallow
    copy; the typed fields are projections of it, never replacements.
    """
    if not isinstance(stdin_json, Mapping):
        return InjectionRequest(prompt_text="", session_id="", harness=harness, native={})
    cwd_raw = stdin_json.get("cwd")
    hook_event_raw = stdin_json.get("hook_event_name")
    return InjectionRequest(
        prompt_text=stdin_json.get("prompt", ""),
        session_id=stdin_json.get("session_id", ""),
        harness=harness,
        hook_event=hook_event_raw if isinstance(hook_event_raw, str) else None,
        cwd=Path(cwd_raw) if isinstance(cwd_raw, str) and cwd_raw.strip() else None,
        native=dict(stdin_json),
    )


def native_response_from_result(
    result: InjectionResult,
    *,
    hook_event_name: str = "UserPromptSubmit",
) -> tuple[str, int]:
    """Render an InjectionResult into ``(stdout_text, exit_code)`` for the hook wire.

    An :class:`Injection` renders the observed Claude Code envelope —
    ``{"hookSpecificOutput": {"hookEventName": ..., "additionalContext": ...}}``
    via bare ``json.dumps`` defaults plus a trailing newline; the byte shape is
    proven against the T-3241 fixtures' accepted ``emit.stdout``. Callers must
    emit the string verbatim via ``sys.stdout.write`` (NOT ``print``, which
    would double the newline).

    Everything else — no-op, backend failure, or non-result garbage from a
    defective caller — renders ``("", 0)``: model-silent per ORG-073 §3. The
    only path to non-empty stdout is ``model_visible_text()`` returning a
    string, which only an :class:`Injection` does. Exit code is ``0`` on every
    path.
    """
    text = model_visible_text(result)
    if text is None:
        return ("", 0)
    envelope = {
        "hookSpecificOutput": {
            "hookEventName": hook_event_name,
            "additionalContext": text,
        }
    }
    return (json.dumps(envelope) + "\n", 0)
