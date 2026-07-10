#!/usr/bin/env python3
"""adaptive_finish_detect — Audit-tier Stop hook for narrate-then-exit detection.

Per T-0220 / Arize 2026-05-07 benchmark. Fires on SubagentStop (worker subagent
completes). Reads the subagent transcript JSONL, checks the last assistant turn for
narrate-then-exit linguistic patterns WITHOUT a same-turn tool_use block. On match:
logs a hit record to ~/.local/state/pantheon/adaptive_finish_log.jsonl.

Ships as AUDIT TIER ONLY. Matches are logged; nothing is blocked. Re-evaluate for
block-tier after 4 weeks of data: threshold is < 5% false-positive rate over the
window AND operator confirmation that observed true-positives have impact.

Design decisions:
  - Reads hook event JSON from stdin (same pattern as ho_stop_failure.py). Claude
    Code delivers hook events on stdin, not as shell-expanded environment variables.
  - Uses SubagentStop (not Stop). Stop fires for HO's own session end; SubagentStop
    fires per worker subagent completion and provides agent_transcript_path.
  - agent_type == "orchestrator" is permanently excluded. HO legitimately closes
    turns with narrate-style prose; per T-0220 locked decision #3.
  - Missing agent_type → fail-safe skip (no hit). Unknown callers are treated as
    excluded rather than logged, to prevent spurious hits from non-Pantheon subagents.
  - Hook always exits 0. Detector bugs must not block legitimate worker turns per
    locked decision #4 (silent-skip + log to state file).

Weekly Telegram digest of hit counts + sample matches: BUILT in
tools/adaptive_finish_digest.py + pantheon-adaptive-finish-digest.timer (T-0907;
cadence locked by HO in the T-0220 brief, decision #1). It reads this log read-only;
the original pantheon-bridge integration path is dead (2026-06-13 billing change).

Usage (hook invocation — stdin is the SubagentStop event JSON):
    python3 /home/claude/pantheon-config/tools/adaptive_finish_detect.py

Environment:
    PANTHEON_STATE_DIR — override default state dir (tests use tempdir).

Exit codes:
    0 — always (hook must not block Claude Code).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
_LOG_FILE_NAME = "adaptive_finish_log.jsonl"

# Default log path (overridden in tests via log_path= kwarg or PANTHEON_STATE_DIR).
_DEFAULT_LOG_PATH = _DEFAULT_STATE_DIR / _LOG_FILE_NAME

# Subagent types permanently excluded from detection.
# HO closes turns with narrate-style prose intentionally; all other types
# (implementer, reviewer, researcher, project-orchestrator) are monitored.
_EXCLUDED_AGENT_TYPES = frozenset({"orchestrator"})

# Narrate-then-exit pattern set. Each alternative matches the opening of a
# narrate-style phrase. Compiled once; case-insensitive.
#
# Pattern notes:
#   "Let me" — broadest, highest false-positive rate (3/3 empirical FPs in
#   b36/b37 sample came from this pattern; reviewers saying "Let me do the
#   analysis" then completing inline). Now gated by the trailing-output check
#   below (T-1128) so the inline-completion FPs are filtered, not dropped.
#   "I will" — common in worker final turns; catches "I will write X" + exit.
#   Word boundary (\b) prevents partial matches inside other words.
NARRATE_RE = re.compile(
    r"\b(I will now|I will go ahead and|Next,? I will|Finally,? I will|I will|Let me)\b",
    re.IGNORECASE,
)

# A genuine narrate-then-exit ENDS with the narration — the phrase is the tail of
# the message, with nothing substantive after it. When meaningful output follows
# the phrase, the agent narrated intent and then did the work inline (e.g. a
# subagent writing "Let me compile the answers:" followed by the actual answers),
# which is a false positive that pollutes the audit-tier digest (T-1128). We
# therefore require the LAST narrate phrase in the turn to be followed by at most
# this many non-whitespace characters. Real narrate-then-exit tails are a single
# clause ("write the smoke output."); inline-completion FPs trail a paragraph+.
TRAILING_SUBSTANTIVE_MAX = 80

# A composed deliverable — a PLAN, STRUCTURED MAP, REPORT, or reviewer assessment —
# carries markdown structure: section headings (`## ...`) and/or horizontal-rule
# separators (`---`). When the message contains such structure, the agent narrated
# intent ("Let me compose the plan:", "I will relay the plan") and then composed the
# artifact INLINE in the same turn. That is the dominant T-1449 false-positive class:
# normal plan-first composition, report compilation, reviewer final-assessment. The
# trailing-char gate (TRAILING_SUBSTANTIVE_MAX) does not catch it because the artifact
# body itself contains narrate phrases ("I will write X.") near its end, so the LAST
# match lands inside the artifact with little trailing text and fires spuriously.
#
# A genuine narrate-then-exit PROMISES a deliverable and bails — it never composes
# one. So treat the presence of a composed artifact as a hard suppressor, independent
# of where the narrate phrases fall within the message. The detector is audit-tier
# (logs only) feeding an operator-facing weekly digest; biasing toward suppression when
# real structured content was delivered is the correct tradeoff (T-1449: kill the
# benign-composition noise; a missed true-positive is only lost audit data).
_ARTIFACT_RE = re.compile(r"^\s{0,3}(#{1,6}\s+\S|-{3,}\s*$)", re.MULTILINE)


# ---------------------------------------------------------------------------
# Transcript reader
# ---------------------------------------------------------------------------


def read_last_assistant_turn(
    transcript_path: str,
) -> Optional[Tuple[str, bool]]:
    """Read the last assistant turn from a JSONL transcript file.

    Returns (text, has_tool_use) where:
      text        — concatenated text-block content of the last assistant message.
      has_tool_use — True if any tool_use block appears in the same message.

    Returns None on any error (missing file, malformed JSON, no assistant turn found).
    """
    try:
        path = Path(transcript_path)
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    last_text: Optional[str] = None
    last_has_tool: bool = False

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        text_parts: list[str] = []
        has_tool = False
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                t = block.get("text")
                if isinstance(t, str):
                    text_parts.append(t)
            elif btype == "tool_use":
                has_tool = True

        if text_parts:
            last_text = "\n".join(text_parts)
            last_has_tool = has_tool
        elif has_tool:
            # tool_use-only turn (no text) — still update the tool flag.
            last_text = ""
            last_has_tool = True

    if last_text is None:
        return None
    return last_text, last_has_tool


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _trailing_substantive_len(text: str, start: int) -> int:
    """Count non-whitespace characters in text from index `start` onward."""
    return sum(1 for c in text[start:] if not c.isspace())


def _contains_composed_artifact(text: str) -> bool:
    """True if the message contains a composed structured deliverable.

    Markdown section headings (`## ...`) or horizontal-rule separators (`---`)
    indicate the agent composed a PLAN / REPORT / STRUCTURED MAP / assessment
    inline in the same turn (the T-1449 false-positive class) rather than
    promising a deliverable and exiting. Used as a hard suppressor in
    detect_narrate_no_tool. See _ARTIFACT_RE for the rationale.
    """
    return bool(_ARTIFACT_RE.search(text))


def detect_narrate_no_tool(
    text: str,
    has_tool_use: bool,
) -> Optional[re.Match]:
    """Return a narrate match iff the turn ENDS with a narrate-then-exit phrase.

    Returns None if:
      - has_tool_use is True (action was taken — not a bare narrate-then-exit), OR
      - no narrate pattern matches, OR
      - the message contains a composed structured artifact (PLAN / REPORT /
        assessment); the agent narrated then composed inline — a false positive;
        see _ARTIFACT_RE / _contains_composed_artifact, T-1449), OR
      - the last narrate phrase is followed by more than TRAILING_SUBSTANTIVE_MAX
        non-whitespace chars (the agent narrated then did the work inline — a
        false positive; see TRAILING_SUBSTANTIVE_MAX, T-1128).

    The LAST match is used (not the first) so the check reflects whether the
    message ends on narration, not whether narration appears anywhere in it.
    """
    if has_tool_use:
        return None
    matches = list(NARRATE_RE.finditer(text))
    if not matches:
        return None
    if _contains_composed_artifact(text):
        return None
    last = matches[-1]
    if _trailing_substantive_len(text, last.end()) > TRAILING_SUBSTANTIVE_MAX:
        return None
    return last


# ---------------------------------------------------------------------------
# Event loader
# ---------------------------------------------------------------------------


def load_event(stream) -> Optional[dict]:
    """Read and parse hook event JSON from a file-like stream.

    Returns the parsed dict or None on any parse error.
    """
    raw = stream.read()
    if not raw or not raw.strip():
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    return obj


# ---------------------------------------------------------------------------
# Core processor
# ---------------------------------------------------------------------------


def process_event(event: dict, log_path: Optional[str] = None) -> None:
    """Evaluate a SubagentStop event and log a hit if narrate-then-exit is detected.

    Fails silently on any error — hook must never block Claude Code.

    Args:
        event:    Parsed SubagentStop event dict from Claude Code.
        log_path: Override log file path (for tests). Default: PANTHEON_STATE_DIR / log filename.
    """
    try:
        _process_event_inner(event, log_path)
    except Exception as exc:  # noqa: BLE001 — last-resort catch; hook must not crash
        print(f"adaptive_finish_detect: unexpected error: {exc}", file=sys.stderr)


def _process_event_inner(event: dict, log_path: Optional[str]) -> None:
    # --- HO / unknown-type exclusion ---
    agent_type = event.get("agent_type")
    if not agent_type:
        # Missing agent_type: fail-safe skip. Unknown callers are not monitored.
        return
    if agent_type in _EXCLUDED_AGENT_TYPES:
        return

    # --- Locate transcript ---
    transcript_path = event.get("agent_transcript_path")
    if not transcript_path:
        # No transcript path provided — nothing to analyse.
        return

    # --- Read last assistant turn ---
    result = read_last_assistant_turn(transcript_path)
    if result is None:
        return
    text, has_tool_use = result

    if not text:
        return

    # --- Apply narrate-then-exit detection ---
    match = detect_narrate_no_tool(text, has_tool_use)
    if match is None:
        return

    # --- Build and append hit record ---
    record = {
        "ts": int(time.time()),
        "transcript_path": transcript_path,
        "agent_type": agent_type,
        "matched_phrase": match.group(0),
        "message_excerpt": text[:200],
        "false_positive_marked": None,
    }

    # Resolve log path: kwarg > PANTHEON_STATE_DIR env > default.
    if log_path is None:
        state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
        state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
        log_path = str(state_dir / _LOG_FILE_NAME)

    resolved = Path(log_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with resolved.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")

    print(
        f"adaptive_finish_detect: hit — agent_type={agent_type} "
        f"phrase={match.group(0)!r} transcript={transcript_path}",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(log_path: Optional[str] = None) -> int:
    """Entry point. Reads SubagentStop event from stdin; processes; always exits 0."""
    try:
        event = load_event(sys.stdin)
        if event is not None:
            process_event(event, log_path=log_path)
    except Exception as exc:  # noqa: BLE001
        print(f"adaptive_finish_detect: top-level error: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
