#!/usr/bin/env python3
"""Candidate response-capture adapter (T-3461).

Composes ``runtime_controller.submit_to_runtime`` (fire-and-forget prompt
delivery) with a nonce-tagged, tmux-pane-polled JSON self-report protocol so
``ContinuitySwitchCoordinator``'s ``default_ports().communicate()`` can fill
``CommunicationResult.structured_payload`` with REAL candidate self-report
data instead of T-3177's scaffold-hardcoded ``{}`` -- see T-3177 DEC-8/DEC-9
for exactly which ``verify_continuity`` assertions that scaffold forced
fail-closed against a real live candidate.

Deliberately its OWN module rather than folded into
``continuity_switch_coordinator.py`` (T-3461 DECISIONS): the response-capture
protocol (prompt shape, nonce discipline, pane-poll loop, JSON extraction) is
a distinct, independently-testable concern from the coordinator's
transactional state machine, and folding it in would push
``continuity_switch_coordinator.py`` well past the code-size-norms flag
threshold. This module never imports from ``continuity_switch_coordinator``
(one-directional dependency) -- a few tiny private helpers are duplicated
rather than crossing that boundary, the same precedent as T-3177 DEC-2 for
``persona_continuity``'s own private-boundary helpers.

Never modifies ``tools/runtime_controller.py`` or ``tools/persona_continuity.py``
(both consumed as libraries) -- preserves T-3177's external-module boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from tools.harness_adapter.types import ExecutionRequest, TmuxTarget
from tools.persona_continuity import CandidateSourceRevision
from tools import runtime_controller

# T-3461: mirrors runtime_controller.PROVIDER_COMPLETION_TIMEOUT_SECONDS's
# existing precedent for "wait on a live LLM turn to complete" rather than
# inventing a new arbitrary ceiling -- same concern class (a real candidate
# has to think and type a response).
RESPONSE_CAPTURE_TIMEOUT_SECONDS = 90.0
RESPONSE_CAPTURE_POLL_SECONDS = 1.0
RESPONSE_CAPTURE_ATTEMPTS = int(RESPONSE_CAPTURE_TIMEOUT_SECONDS / RESPONSE_CAPTURE_POLL_SECONDS)

MAX_SELF_REPORT_BYTES = 256 * 1024

_BEGIN_PREFIX = "PANTHEON-SELF-REPORT-BEGIN-"
_END_PREFIX = "PANTHEON-SELF-REPORT-END-"

# T-3461: persona_continuity._candidate_evidence_from_obj REJECTS an empty
# `source_revisions` list as malformed evidence (raises ContinuityError),
# rather than treating it as a legitimate empty self-report -- see
# verify_continuity's evidence-construction comment in
# continuity_switch_coordinator.py for the full trap. This sentinel is
# schema-valid (canonical `repo:` uri, sha256-shaped hex) and structurally
# cannot equal any real bundle's source refs.
_UNIDENTIFIED_SOURCE_REVISIONS: tuple[CandidateSourceRevision, ...] = (
    CandidateSourceRevision(uri="repo:unidentified.json", source_revision="unidentified", source_sha256="0" * 64),
)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def new_nonce() -> str:
    return f"selfreport-{os.urandom(8).hex()}"


def _fresh_id(prefix: str) -> str:
    return f"{prefix}-{os.urandom(8).hex()}"


def build_self_report_prompt(intent: str, nonce: str) -> str:
    """Wrap `intent` (the coordinator's semantic question, e.g. 'identify
    yourself: persona, role, next action') with a bounded, deterministic,
    machine-parseable response contract.

    `nonce` is fresh per call so a STALE self-report left over on the
    VISIBLE pane from a prior turn (this module's own pane-capture is
    visible-screen-only, matching runtime_controller's own readiness-check
    discipline) can never be mistaken for the current response -- see
    `extract_self_report`. This is a freshness-binding mechanism, not a
    secrecy one: the nonce is transmitted openly in the prompt.
    """

    return (
        f"{intent}\n\n"
        "Respond with a single line in EXACTLY this format (nothing else on "
        f"that line): {_BEGIN_PREFIX}{nonce} <json> {_END_PREFIX}{nonce}\n"
        "<json> is a single-line, minified JSON OBJECT (no embedded raw "
        "newlines) with these keys, every value drawn from your own loaded "
        "continuity checkpoint/bundle -- never invented:\n"
        '{"role_id": "<your loaded role-contract source ref>", '
        '"role_authority_refs": ["..."], "current_commitment_refs": ["..."], '
        '"expected_next_action": "...", "authority_limits": ["..."], '
        '"unavailable_capabilities": ["..."], '
        '"capability_evidence_complete": true|false, '
        '"checkpoint_sha256": "<your loaded checkpoint envelope\'s payload_sha256>", '
        '"descriptor_sha256": "<your loaded bundle\'s descriptor_sha256>", '
        '"source_revisions": [{"uri": "...", "source_revision": "...", '
        '"source_sha256": "..."}], '
        '"communication_correlation_id": "<the correlation_id in your loaded '
        'checkpoint\'s boundary section>", '
        '"behavioral_variance": ["..."]}\n'
        "Emit this line exactly once."
    )


def extract_self_report(pane_text: str, nonce: str) -> dict[str, Any] | None:
    """Find the marker-delimited JSON block tagged with `nonce` in
    `pane_text` and parse it.

    Never raises. Degrades every one of {no markers, wrong/absent nonce,
    oversized match, malformed JSON, non-object JSON} to None -- the same
    fail-closed shape as a fully silent candidate (T-3461 DECISIONS).
    """

    if not pane_text:
        return None
    joined = pane_text.replace("\n", " ")
    pattern = re.compile(
        re.escape(f"{_BEGIN_PREFIX}{nonce}") + r"(.*?)" + re.escape(f"{_END_PREFIX}{nonce}"),
        re.DOTALL,
    )
    match = pattern.search(joined)
    if match is None:
        return None
    raw = match.group(1).strip()
    if not raw or len(raw) > MAX_SELF_REPORT_BYTES:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def candidate_source_revisions_from_payload(raw: object) -> tuple[CandidateSourceRevision, ...]:
    """Parse a candidate's self-reported `source_revisions` list into typed
    evidence.

    Absent / not-a-list / empty -> the non-empty `_UNIDENTIFIED_SOURCE_REVISIONS`
    sentinel (never an empty tuple -- see module docstring / the sentinel's
    own comment for why an empty tuple would raise downstream instead of
    failing the assertion cleanly). Never raises: a non-dict list entry
    degrades to an all-empty-string placeholder, which persona_continuity's
    OWN schema validator then rejects cleanly downstream (the same
    raise-on-malformed-content precedent `role_id` already has) rather than
    this module attempting to duplicate that validation.
    """

    if not isinstance(raw, list) or not raw:
        return _UNIDENTIFIED_SOURCE_REVISIONS
    revisions: list[CandidateSourceRevision] = []
    for item in raw:
        if isinstance(item, dict):
            revisions.append(CandidateSourceRevision(
                uri=item.get("uri", ""),
                source_revision=item.get("source_revision", ""),
                source_sha256=item.get("source_sha256", ""),
            ))
        else:
            revisions.append(CandidateSourceRevision(uri="", source_revision="", source_sha256=""))
    return tuple(revisions)


def capture_pane_joined(target: TmuxTarget) -> str:
    """Own minimal tmux capture-pane call.

    `-J` rejoins wrapped rows so a self-report JSON line that wraps across
    the terminal's column width isn't split into multiple regex-unmatchable
    rows. Deliberately NOT a reuse of
    `runtime_controller._capture_visible_runtime_pane` (lacks -J; that
    helper's visible-screen-only, no-join shape fits ITS readiness-pattern-
    matching use case, not this module's single-line JSON contract) -- a
    fresh, minimal, purpose-fit call, the same posture as this coordinator
    bundle's other direct tmux calls (e.g.
    `continuity_switch_coordinator._default_stop_runtime`). No custom
    tmux-socket/context threading, matching that same precedent (production
    only ever uses the default socket; context-injection is a
    runtime_controller test-isolation concern).
    """

    result = subprocess.run(
        ("tmux", "capture-pane", "-p", "-J", "-t", runtime_controller.runtime_target_text(target)),
        check=False, capture_output=True, text=True, shell=False,
    )
    return result.stdout if result.returncode == 0 else ""


@dataclass(frozen=True)
class SelfReportCommunication:
    correlation_id: str
    ok: bool
    evidence_sha256: str
    structured_payload: Mapping[str, Any]


def default_communicate(
    handle: "runtime_controller.RuntimeHandle",
    prompt: str,
    *,
    repo_root: Path,
    state_root: Path | None,
    submit: Callable[..., "runtime_controller.DeliveryReceipt"] = runtime_controller.submit_to_runtime,
    capture_pane: Callable[[TmuxTarget], str] = capture_pane_joined,
    attempts: int = RESPONSE_CAPTURE_ATTEMPTS,
    poll_interval_seconds: float = RESPONSE_CAPTURE_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    nonce_factory: Callable[[], str] = new_nonce,
) -> SelfReportCommunication:
    """Submit a self-report request to `handle`'s pane and poll-capture the
    response.

    Never raises: a `submit` exception (tmux/subprocess submission failure)
    degrades to `ok=False, structured_payload={}` rather than propagating
    into `verify_continuity`. A submission that succeeds but never yields a
    parseable self-report (silence, timeout, malformed content) degrades to
    `ok=True, structured_payload={}` -- matching the shape the coordinator's
    own FakeWorld test fixture already assumes for a silent candidate.
    """

    nonce = nonce_factory()
    full_prompt = build_self_report_prompt(prompt, nonce)
    transport_ok: bool
    transport_evidence: Mapping[str, Any]
    try:
        receipt = submit(handle, ExecutionRequest(prompt=full_prompt, cwd=repo_root), state_root=state_root)
        transport_ok = True
        transport_evidence = dict(receipt.evidence)
    except Exception as exc:  # tmux/subprocess submission failure -- fail-closed, never raise into verify_continuity
        transport_ok = False
        transport_evidence = {"submission_error": str(exc)[:1000]}

    pane_text = ""
    parsed: dict[str, Any] | None = None
    if transport_ok:
        for attempt in range(1, attempts + 1):
            pane_text = capture_pane(handle.tmux)
            parsed = extract_self_report(pane_text, nonce)
            if parsed is not None:
                break
            if attempt < attempts:
                sleep(poll_interval_seconds)

    payload: dict[str, Any] = parsed if isinstance(parsed, dict) else {}
    reported_correlation = payload.get("communication_correlation_id")
    correlation_id = reported_correlation if isinstance(reported_correlation, str) and reported_correlation else _fresh_id("comm")
    evidence_sha256 = _sha256(_json_bytes({
        "nonce_sha256": _sha256(nonce.encode()),
        "transport": transport_evidence,
        "pane_sha256": _sha256(pane_text.encode()) if pane_text else None,
        "self_report_parsed": parsed is not None,
    }))
    return SelfReportCommunication(
        correlation_id=correlation_id,
        ok=transport_ok,
        evidence_sha256=evidence_sha256,
        structured_payload=payload,
    )
