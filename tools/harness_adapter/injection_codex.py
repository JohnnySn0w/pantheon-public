"""Codex CLI native encoder for the Concordance injection contract (T-3243).

ORG-073 §4: harness bindings are separate native encoders — each parses the
native hook stdin, constructs an :class:`InjectionRequest`, and converts an
:class:`InjectionResult` into the harness-native response shape. This module
is the Codex CLI binding for the ``UserPromptSubmit`` hook event.

**DOCUMENTED-NOT-OBSERVED.** Unlike ``injection_claude.py`` (T-3242, written
against two live-captured ``UserPromptSubmit`` fires, T-3241 §4), this module
is written against DOCUMENTATION ONLY. The Codex home directory is not
readable from this arc's seat and no Codex hooks are configured anywhere
readable from it — live capture is structurally someone else's leg (the
live-verify follow-on ticket filed to the codex seat). Every stdin key name,
key set, and response envelope shape below is taken from:

- ``adr/ORG-073-information-flow-retrieval-injection-layer.md`` §4 (harness
  integration contract) + §1 (per-harness timeout defaults)
- ``knowledge/episodic/2026-06-02-codex-harness-investigation/notes.md``
  (T-1710 abstraction synthesis, live-verified against the codex daemon for
  hook *mechanics* — event names, trust model, approval/sandbox config — but
  NOT for this specific ``UserPromptSubmit`` stdin/stdout wire shape)
- ``knowledge/episodic/2026-06-02-codex-vs-claude-code-capability-surface/notes.md``
  (independent corroboration: hooks are "near-superset CLONE of claude-code
  hooks" — same JSON-on-stdin ``session_id``/``cwd``/``hook_event_name``/
  ``permission_mode`` shape, same ``hookSpecificOutput`` stdout envelope)

Documented stdin fields for ``UserPromptSubmit`` (near-1:1 with Claude Code):
``session_id``, ``cwd``, ``hook_event_name``, ``permission_mode``, ``prompt``.
Documented stdout envelope (near-1:1 with Claude Code):
``{"hookSpecificOutput": {"hookEventName": ..., "additionalContext": ...}}``.

None of the above has been observed on a live Codex wire from this seat. The
live-verify ticket (filed to the codex seat) diffs these assumptions against
a real capture; do not read this module's tests as wire-shape proof — read
them as the documented spec, marked as such.

Faithful parsing, no validation: this encoder never raises on payload shape
and never pre-validates. Missing / blank / non-str required fields produce a
request that fails ``InjectionRequest.missing_required()``; the contract's
``HookInjectionProvider.invoke()`` owns dispatch on gaps (typed no-op), per
the T-3240 split of duties. Same faithful-never-raises discipline as T-3242.

``InjectionRequest.native`` carries the ENTIRE stdin payload as a shallow
copy — mapped keys included, original key order, values untouched. The typed
fields are lossy projections; ``native`` stays the complete source of record.

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
    harness: HarnessName = "codex",
) -> InjectionRequest:
    """Parse a Codex CLI UserPromptSubmit stdin payload into an InjectionRequest.

    DOCUMENTED-NOT-OBSERVED (see module docstring): the ``prompt``,
    ``session_id``, ``cwd``, ``hook_event_name`` key names are taken from
    documentation, not from a live capture.

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

    DOCUMENTED-NOT-OBSERVED (see module docstring): an :class:`Injection`
    renders the DOCUMENTED Codex envelope —
    ``{"hookSpecificOutput": {"hookEventName": ..., "additionalContext": ...}}``
    — via bare ``json.dumps`` defaults plus a trailing newline. This byte
    shape is inherited from the "near-1:1 with Claude Code" documentation
    claim and from Claude's own T-3241-fixture-proven shape; it is NOT itself
    proven against a live Codex capture. Callers must emit the string
    verbatim via ``sys.stdout.write`` (NOT ``print``, which would double the
    newline).

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
