"""ClaudeHarnessAdapter — concrete binding for the claude-code CLI harness.

Implements PantheonHarnessAdapter for claude-code (the primary Pantheon harness:
Orchestrator/HO, Specialist, Recovery).  All 7 generalized + 2 bridge surfaces are implemented
with real key/filename translations.

Not-abstracted surfaces (sandbox_config, profiles_config, app_server_config,
cloud_config, doctor_config) remain provider-specific descriptions. They are
not forced into Codex-shaped policy.

References:
    adr/ORG-031-harness-abstraction-and-daemon-onboarding.md §Decision (i)
    knowledge/episodic/2026-06-04-codex-harness-abstraction-synthesis/
      notes.md §2 (capability map)
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import shlex
import stat
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from tools.harness_adapter._common import (
    deliver_to_mailbox,
    finalize_events,
    parse_json_object,
)
from tools.bypass_consent import ensure_bypass_consent
from tools.harness_adapter.base import PantheonHarnessAdapter
from tools.harness_adapter.types import (
    AuthDescriptor,
    CommandSpec,
    DeliveryReceipt,
    DiagnosticObservation,
    ExecutionRequest,
    ExecutionResult,
    HarnessLaunchConfig,
    HarnessCapabilities,
    HarnessEvent,
    HarnessSurfaceDescriptors,
    HookCapability,
    InboundRequest,
    McpDescriptor,
    MemoryDescriptor,
    ProviderBillingEvidence,
    PostStartAssessment,
    PostStartPaneSample,
    ProviderCompletionAssessment,
    ProviderCompletionProbe,
    RoleDescriptor,
    SessionHandle,
    StandingInstructionsDescriptor,
    TmuxTarget,
    Usage,
)
from tools.claude_statusline_capture import SCHEMA as STATUSLINE_SCHEMA
from tools.claude_quota_probe import CAP_SENTINELS, READY_PROMPT_LINE
from tools.harness_adapter.quota import QuotaObserver
from tools.harness_adapter.provider_quota import (
    ClaudeAcceptanceQuotaOverlayObserver,
    ClaudeStatuslineQuotaObserver,
    UnavailableQuotaObserver,
    valid_acceptance_run_id,
)


# Native keys a claude role binding may carry. diagnostics._NATIVE_KEYS["claude"]
# is a pinned copy (tools/test_t4531_openrouter_profile.py asserts equality).
CLAUDE_NATIVE_KEYS = frozenset({"permission_mode", "channels", "effort", "base_url"})

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_COMPLETION_SCHEMA = "pantheon-harness-provider-completion/v1"
_MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024
# T-4452: a fresh session's transcript is read from offset 0, boot attachments
# included. Recovery's was 166 KB with a 70.6 KB instructions line (CLAUDE.md plus
# memory) at T-3026 attempt 3, over the old 256 KiB / 64 KiB caps' headroom.
# The file cap above is the real bound on what is read.
_MAX_TRANSCRIPT_DELTA_BYTES = _MAX_TRANSCRIPT_BYTES
_MAX_TRANSCRIPT_LINES = 400
_MAX_JSONL_LINE_BYTES = 1024 * 1024
_MAX_STATUSLINE_BYTES = 1024 * 1024
# T-4321: the probe's history entry is among the newest lines, so reading the
# tail is enough (fleet history.jsonl files run up to ~2 MB).
_MAX_PROMPT_HISTORY_TAIL_BYTES = 256 * 1024
_EMPTY_PASTED_CONTENTS_RE = re.compile(rb'"pastedContents":\s*\{\}')

# T-5791 r1: line-anchored provider banners only, so prose that quotes a banner
# ("the docs say 'Please run /login'", a test log's "Error 401 ...") never
# matches. T-5803: the line must open with exactly one of Claude Code's own
# message glyphs (●/⏺/⎿); bare, '>'- or '│'-prefixed lines (cat'd files,
# quoted blocks, tool-output boxes) never count. Each pattern
# is single-line (no re.S). Real banner: `● Please run /login · API Error: 401
# Invalid authentication credentials`.
_AUTH_BANNER_PREFIX = r"^\s*[●⏺⎿]\s+"
_AUTHENTICATION_LOSS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("http-401", re.compile(
        _AUTH_BANNER_PREFIX
        + r"(?:Please run /login\s*·\s*)?API Error:\s*401\b[^\n]{0,200}?"
        r"(?:Invalid authentication credentials|authentication_error)",
    )),
    ("account-disabled", re.compile(
        _AUTH_BANNER_PREFIX
        + r"(?:API Error:\s*40[0-3]\b[^\n]{0,200}?)?(?:Your |This )?(?:account|organization) "
        r"(?:has been|was|is) (?:disabled|suspended|revoked)\b[^\n]{0,80}$",
        re.I,
    )),
    ("login-required", re.compile(
        _AUTH_BANNER_PREFIX
        + r"(?:(?:Not logged in|Invalid API key|OAuth token (?:has been )?(?:revoked|expired))\s*[·-]\s*)?"
        r"Please run /login\s*(?:·[^\n]{0,200})?$",
    )),
)
#: Only the active prompt/error region is classified, never scrollback.
AUTH_LOSS_ACTIVE_REGION_LINES = 12


def classify_claude_authentication_loss(text: str) -> tuple[str, int] | None:
    """Return (pattern_id, banner_line_count) for explicit provider auth loss, else None.

    The banner must sit in the last AUTH_LOSS_ACTIVE_REGION_LINES non-empty lines.
    banner_line_count counts every banner line in the capture, so a caller can
    corroborate on a NEW failure (the count grew) instead of re-reading one
    static pane."""
    lines = [line for line in _normalize_provider_pane(text).split("\n") if line.strip()]

    def match(line: str) -> str | None:
        return next((pid for pid, pattern in _AUTHENTICATION_LOSS_PATTERNS if pattern.search(line)), None)

    active = next(
        (pid for line in reversed(lines[-AUTH_LOSS_ACTIVE_REGION_LINES:]) if (pid := match(line))),
        None,
    )
    if active is None:
        return None
    return active, sum(1 for line in lines if match(line))


def _normalize_provider_pane(text: str) -> str:
    return _ANSI_RE.sub("", text).replace("\r", "")


def _claude_cap_sentinel(text: str) -> str | None:
    normalized = _normalize_provider_pane(text)
    for pattern in CAP_SENTINELS:
        for line in normalized.splitlines():
            match = pattern.match(line.strip())
            if match:
                return match.group(1)
    return None


def _has_claude_rate_limit_modal_framing(text: str) -> bool:
    normalized = _normalize_provider_pane(text).lower()
    required = (
        "/rate-limit-options",
        "what do you want to do?",
        "stop and wait for limit to reset",
        "upgrade your plan",
    )
    return all(item in normalized for item in required)


def classify_claude_post_start_subscription_cap(
    *,
    baseline_text: str,
    sample_text: str,
) -> tuple[str | None, str | None]:
    """Return a narrow provider cap classification for changed post-start UI."""
    if _normalize_provider_pane(baseline_text) == _normalize_provider_pane(sample_text):
        return None, None
    if _claude_cap_sentinel(baseline_text):
        return None, None
    sentinel = _claude_cap_sentinel(sample_text)
    if not sentinel:
        return None, None
    if not _has_claude_rate_limit_modal_framing(sample_text):
        return None, None
    return "claude-rate-limit-options-modal", "post-start-subscription-cap"


# ------------------------------------------------------------ T-4037: mid-session
# cap detection. classify_claude_post_start_subscription_cap (above) requires a
# BASELINE diff (pre-launch vs post-launch pane sample) and only runs at
# _controller_start_runtime time -- it structurally cannot see a cap that pops
# WHILE an already-healthy runtime is mid-session (the T-4037 gap: a pane that
# never died just reads as healthy forever). classify_claude_mid_session_cap
# below is a single-sample classifier (no baseline needed) for that case.

# T-4024 (real capture, specialist 2026-09-27 cap window) — independently defined here
# rather than imported from tools/context_fill_monitor.py's SPEND_LIMIT_SENTINEL:
# that module's own INTERACTIVE_MODAL_SENTINELS is loose substring matching mixed
# with /login sentinels (explicitly not to be reused for detection, T-4037 brief);
# this is a plain string literal, safe to duplicate without coupling the two.
_SPEND_LIMIT_SENTINEL = "monthly spend limit"

# Three reset-time text shapes observed in real captures (T-4037 REBOUND Finding 2
# added the day-name shape; the docstrings below used to overclaim "two"):
#   dated:    "Resets Sep 27, 5pm (UTC)"       -- the monthly-spend-limit modal
#   bare:     "resets 2:50pm (UTC)"            -- the session/weekly/opus modal
#   day-name: "resets Sunday" / "resets Mon 12:00am" -- real in-repo captures at
#             tools/test_claude_quota_probe.py:230 and tools/test_ho_stop_failure.py:256;
#             time-of-day is OPTIONAL on this shape (see _resolve_dow_reset).
# The bare form mirrors tools/acp_bridge.py's _CAP_RESET_RE (cited, not imported --
# a different subsystem/call-site parsing a different text source; duplicating a
# 3-line regex is cheaper than an inter-module coupling for it, per P7). The
# day-name form mirrors tools/hooks/ho_stop_failure.py's _RESETS_RE / _DOW_MAP /
# _parse_resets_at weekday-advance algorithm (cited, not imported -- same
# no-cross-module-coupling reasoning; that parser resolves in the OS LOCAL
# timezone, which this module deliberately does NOT follow -- see _resolve_dow_reset).
_MID_SESSION_CAP_DATED_RESET_RE = re.compile(
    r"resets\s+(?!(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\s)"
    r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
    re.IGNORECASE,
)
_MID_SESSION_CAP_BARE_RESET_RE = re.compile(
    r"reset[s]?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
    re.IGNORECASE,
)
# Day-name form: "resets Sunday" (bare) or "resets Mon 12:00am" (day + time). The
# negative lookahead on the DATED regex above keeps it from ever treating one of
# these seven tokens as a month name (T-4037 Finding 2) -- the two token sets are
# disjoint by construction (no day-of-week abbreviation collides with a real month
# abbreviation), so the exclusion never shadows a genuine dated match.
_MID_SESSION_CAP_DOW_RESET_RE = re.compile(
    r"resets?\s+(mon|tue|wed|thu|fri|sat|sun)[a-z]*"
    r"(?:\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm))?\b",
    re.IGNORECASE,
)
_DOW_ABBR_TO_NUM = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}  # Monday=0..Sunday=6 -- mirrors tools/hooks/ho_stop_failure.py's _DOW_MAP exactly.
# A reset time this-far-or-less BEHIND `now` reads as boundary clock skew (the
# pane was captured right at/just after the reset), not a genuine stale/next-cycle
# occurrence -- mirrors acp_bridge._CAP_RESET_SKEW_GRACE_SECS exactly (same
# reasoning, independently named per the no-cross-module-coupling call above).
_MID_SESSION_CAP_RESET_SKEW_GRACE_SECONDS = 15 * 60.0
# T-5801: a dated reset this far from `now` belongs to the adjacent year.
_DATED_RESET_YEAR_ROLL_SECONDS = 183 * 86400.0

_MONTH_ABBR_TO_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Bottom-region window for mid-session CHROME checks (T-4037 REBOUND round 1
# Finding 1; TIGHTENED + FOOTER-ANCHORED in round 2 Finding 1 below). A live
# modal is always the LAST thing rendered in a `capture-pane -p` sample --
# nothing draws below it while it's open. Round 1 relied on a loose 20-line
# "somewhere near the end" window; the round-2 reviewer showed ordinary prose
# QUOTING the modal (sentinel + chrome + footer, byte-for-byte) still landed
# inside that window whenever followed by 0-11 trailing lines of commentary.
# Round 2 closes that gap two ways: (a) `_footer_is_final_line` below requires
# the confirm/cancel footer to be the pane's final modal line (allowing only
# Claude's exact UI-owned background-task status row below it) --
# structurally impossible for a quote followed by ANY further prose, and
# always true for the Claude Code UI's own always-rendered input-box/status
# tail once the modal actually closes; (b) this window shrinks from 20 to 16
# lines -- the longer real fixture's sentinel-to-footer span is exactly 13
# lines (orchestrator_cap_modal_pane.txt), so 16 keeps a tight 3-line margin for
# incidental variation (e.g. the "Skipped for Nm Ns" status line wrapping
# onto two rows) without loosening back toward the 20-line gap the reviewer
# exploited. The shorter real fixture's span is 6 lines
# (orchestrator_spend_limit_modal_pane.txt), well inside either window.
_MID_SESSION_CAP_BOTTOM_REGION_LINES = 16

# T-4037 REBOUND round 2 Finding 1: the modal's confirm/cancel footer, matched
# tolerant of leading/trailing whitespace and case (ANSI codes are already
# stripped by `_normalize_provider_pane` before this ever runs -- both real
# captures render each word of this line in its own escape-wrapped span, but
# the LITERAL text between them, including the single spaces and the "·"
# separator, is byte-identical across both fixtures once ANSI-stripped).
_FOOTER_LINE_RE = re.compile(
    r"(?i)^[ \t ]*enter[ \t ]+to[ \t ]+confirm[ \t ]*"
    r"·[ \t ]*esc[ \t ]+to[ \t ]+cancel[ \t ]*$"
)

# Claude 5.5 may render this one UI-owned task-status row below an open modal
# (observed live on Archivist, T-5744/T-5757). Keep the allowance exact and
# single-line: arbitrary prose after the footer must still fail the structural
# anchor that prevents transcript quotes from classifying.
_BACKGROUND_TASK_MODAL_TAIL_RE = re.compile(
    r"(?i)^[ \t ]*background[ \t ]+task[ \t ]+update[ \t ]+waiting[ \t ]+"
    r"while[ \t ]+this[ \t ]+panel[ \t ]+is[ \t ]+open[ \t ]*$"
)

# T-4037 REBOUND round 2 Finding 2: the live modal marks its highlighted
# option with "❯" immediately before the option text -- "❯ 1. Stop and wait
# for limit to reset" (session/weekly/opus, real capture) / "❯ Adjust monthly
# spend limit:" (spend-limit, real capture). `❯` ALONE proves nothing -- it is
# also the ordinary idle-prompt cursor glyph (see `READY_PROMPT_LINE` in
# tools/claude_quota_probe.py, which matches ANY line starting with "❯"
# followed by arbitrary text) -- so this requires the glyph immediately
# adjacent to the SPECIFIC option phrase, not bare presence anywhere in the
# region. Matched against the already-lowercased region text in the chrome
# checkers below.
_SESSION_OPTION_GLYPH_RE = re.compile(
    r"❯[ \t ]*1\.[ \t ]*stop[ \t ]+and[ \t ]+wait[ \t ]+"
    r"for[ \t ]+limit[ \t ]+to[ \t ]+reset"
)
_SPEND_OPTION_GLYPH_RE = re.compile(
    r"❯[ \t ]*adjust[ \t ]+monthly[ \t ]+spend[ \t ]+limit:"
)


def _modal_anchored_lines(text: str) -> list[str]:
    """Normalized pane lines with the one known modal-owned tail removed."""
    normalized = _normalize_provider_pane(text).rstrip()
    lines = normalized.splitlines()
    if lines and _BACKGROUND_TASK_MODAL_TAIL_RE.fullmatch(lines[-1]):
        lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()
    return lines


def _footer_is_final_line(text: str) -> bool:
    """True iff `text`'s final modal line (after ANSI-strip and removal of the
    exact optional background-task UI row) is the confirm/cancel footer
    (T-4037 REBOUND round 2 Finding 1; T-5744/T-5757).

    This is the structural anchor the round-2 reviewer's false positives all
    share: a pane sample that merely QUOTES the modal in conversation is, in
    the realistic case, followed by further conversational text, or --
    whenever the modal has genuinely closed -- by the Claude Code UI's own
    always-rendered input-box/prompt/status-line tail. Neither ever leaves
    the footer as the pane's final rendered content. A live, still-open modal
    is the one case where only the exact optional task-status row may follow.
    """
    lines = _modal_anchored_lines(text)
    if not lines:
        return False
    return _FOOTER_LINE_RE.fullmatch(lines[-1]) is not None


def _bottom_region(text: str) -> str:
    """Join the trailing `_MID_SESSION_CAP_BOTTOM_REGION_LINES` lines of
    `text` (after trailing blank lines are stripped, since a fixed-height pane
    routinely has empty rows below a modal that doesn't fill it). Callers
    must gate on `_footer_is_final_line` first (T-4037 REBOUND round 2) --
    this function only tightens the SPAN of the window; it does not itself
    require the window to end at the footer."""
    lines = _modal_anchored_lines(text)
    return "\n".join(lines[-_MID_SESSION_CAP_BOTTOM_REGION_LINES:])


def _has_claude_session_cap_modal_chrome(text: str) -> bool:
    """True iff `text`'s BOTTOM REGION (see `_bottom_region`) carries the
    session/weekly/opus rate-limit modal's actual render CHROME -- the
    "What do you want to do?" heading, the "❯"-adjacent first option and the
    "Upgrade your plan" second option, from the real capture
    (tools/testdata/orchestrator_cap_modal_pane.txt) -- not merely the framing
    phrases `_has_claude_rate_limit_modal_framing` above checks.

    T-4037 REBOUND round 1 Finding 1: that framing check (four loose
    substrings, no positional constraint) is reused unmodified by
    `classify_claude_post_start_subscription_cap`, which has its own
    baseline-diff defence and is out of scope for this fix. This is a
    DIFFERENT, stricter, mid-session-only check.

    T-4037 REBOUND round 2 Finding 2: the confirm/cancel footer requirement
    moved to the caller-level `_footer_is_final_line` gate (position-anchored,
    strictly stronger than a substring check); the bare "1." / "stop and wait
    for limit to reset" substrings moved into `_SESSION_OPTION_GLYPH_RE`,
    which additionally requires the "❯" selector glyph immediately adjacent
    to that phrase -- prose that quotes the option text without the glyph
    prefix (the round-2 reviewer's ASCII-only repro) no longer matches.
    """
    region = _bottom_region(text).lower()
    required = (
        "what do you want to do?",
        "2.",
        "upgrade your plan",
    )
    if not all(item in region for item in required):
        return False
    return _SESSION_OPTION_GLYPH_RE.search(region) is not None


def _has_claude_spend_limit_modal_chrome(text: str) -> bool:
    """True iff `text`'s BOTTOM REGION (see `_bottom_region`) carries the
    spend-limit modal's actual render CHROME (real capture,
    tools/testdata/orchestrator_spend_limit_modal_pane.txt) -- the credit-balance
    readout, the "❯"-adjacent adjust-limit option, and the wait-to-reset line.

    T-4037 REBOUND round 1 Finding 1: replaces the pre-fix
    `_has_claude_spend_limit_modal_framing`, which required only three loose
    substrings ("monthly spend limit" / "what do you want to do?" / "wait for
    limit to reset") anywhere in the pane -- the reviewer showed ordinary
    prose discussing this very bug satisfies all three.

    T-4037 REBOUND round 2 Finding 2: the confirm/cancel footer requirement
    moved to the caller-level `_footer_is_final_line` gate; the bare "adjust
    monthly spend limit:" substring moved into `_SPEND_OPTION_GLYPH_RE`,
    which additionally requires the "❯" selector glyph immediately adjacent.

    Does NOT require "/rate-limit-options" as a literal substring: the real
    committed capture doesn't include the command-invocation line (it scrolled
    out of the visible pane by capture time -- a visible-pane-only capture, per
    _capture_visible_runtime_pane's own docstring, can legitimately miss it).
    """
    region = _bottom_region(text).lower()
    required = (
        _SPEND_LIMIT_SENTINEL,
        "what do you want to do?",
        "usage credit balance: $",
        "wait for limit to reset",
    )
    if not all(item in region for item in required):
        return False
    return _SPEND_OPTION_GLYPH_RE.search(region) is not None


def _cap_sentinel_kind(sentinel: str) -> str:
    """Map a CAP_SENTINELS-captured phrase to the session/weekly/opus
    modal_kind vocabulary (factored out of `classify_claude_mid_session_cap`'s
    original inline `kind_match` regex, T-4047, so the post-start classifier
    below can reuse the SAME regex rather than duplicating it). Returns
    "unknown-limit" if the sentinel doesn't contain one of the three known
    words -- defensive only; every real `CAP_SENTINELS` match always does."""
    kind_match = re.search(r"(session|weekly|opus)", sentinel, re.IGNORECASE)
    return f"{kind_match.group(1).lower()}-limit" if kind_match else "unknown-limit"


def _parse_claude_mid_session_cap_reset_at(text: str, *, now: datetime) -> datetime | None:
    """Absolute UTC reset instant parsed from a mid-session cap pane, or None.

    Tries the dated form first ("Resets Sep 27, 5pm (UTC)" -- the spend-limit
    modal), then the day-name form ("resets Sunday" / "resets Mon 12:00am" --
    T-4037 Finding 2, real in-repo captures at
    tools/test_claude_quota_probe.py:230 and tools/test_ho_stop_failure.py:256),
    then the bare time-only form ("resets 2:50pm (UTC)" -- session/weekly/opus).
    UTC is assumed for all three, matching every real capture on file (the
    modal always states "(UTC)") -- this deliberately does NOT follow
    tools/hooks/ho_stop_failure.py's OS-local-timezone resolution for its own
    day-name form; see `_resolve_dow_reset`. A result within
    `_MID_SESSION_CAP_RESET_SKEW_GRACE_SECONDS` behind `now` reads as boundary
    skew and returns None (no confident parse) -- genuinely further behind
    rolls to the next occurrence (next day for the bare form; next week for
    the day-name form). The dated form takes the nearest year instead
    (T-5801): a reset already past stays past, so a modal outliving its
    reset reads as due; only a date more than half a year off moves a year. Never raises: any malformed group returns None, same as
    "no reset text present" -- the caller (runtime_controller.py) falls back
    to a conservative default either way.
    """
    normalized = _normalize_provider_pane(text)
    dated = _MID_SESSION_CAP_DATED_RESET_RE.search(normalized)
    if dated is not None:
        return _resolve_dated_reset(dated, now=now)
    dow = _MID_SESSION_CAP_DOW_RESET_RE.search(normalized)
    if dow is not None:
        return _resolve_dow_reset(dow, now=now)
    bare = _MID_SESSION_CAP_BARE_RESET_RE.search(normalized)
    if bare is not None:
        return _resolve_bare_reset(bare, now=now)
    return None


def _resolve_dated_reset(match: re.Match[str], *, now: datetime) -> datetime | None:
    month_name, day, hour, minute, meridiem = match.groups()
    month = _MONTH_ABBR_TO_NUM.get(month_name[:3].lower())
    if month is None:
        return None
    try:
        day_i = int(day)
        hour_i = int(hour) % 12
        minute_i = int(minute or 0)
    except ValueError:
        return None
    if not (1 <= day_i <= 31) or not (0 <= minute_i <= 59):
        return None
    if meridiem.lower() == "pm":
        hour_i += 12
    try:
        candidate = now.replace(
            year=now.year, month=month, day=day_i,
            hour=hour_i, minute=minute_i, second=0, microsecond=0,
        )
    except ValueError:
        return None  # e.g. Feb 30 -- not a real date
    # T-5801: month+day pin the date to within a year of `now`, so pick the
    # NEAREST year. A modal still shown after its reset (the case
    # cap_modal_reaper exists for) stays in the past; only a date more than
    # half a year off crosses a year boundary (Jan shown in Dec, or Dec in Jan).
    year_shift = 0
    if (candidate - now).total_seconds() > _DATED_RESET_YEAR_ROLL_SECONDS:
        year_shift = -1
    elif (now - candidate).total_seconds() > _DATED_RESET_YEAR_ROLL_SECONDS:
        year_shift = 1
    if year_shift:
        try:
            candidate = candidate.replace(year=candidate.year + year_shift)
        except ValueError:
            return None  # T-4173: Feb 29 has no adjacent-year date -> unparsed
    if candidate <= now and (now - candidate).total_seconds() <= _MID_SESSION_CAP_RESET_SKEW_GRACE_SECONDS:
        return None
    return candidate


def _resolve_dow_reset(match: re.Match[str], *, now: datetime) -> datetime | None:
    """Resolve a day-name reset match ("resets Sunday" / "resets Mon 12:00am")
    to the next UTC occurrence of that weekday (T-4037 Finding 2).

    Mirrors tools/hooks/ho_stop_failure.py's `_DOW_MAP` + `_parse_resets_at`
    weekday-advance algorithm (same-weekday-and-already-passed -> +7 days,
    otherwise `(target - current) % 7` days ahead) -- duplicated, not
    imported, per this module's existing no-cross-module-coupling call (see
    the module-level comment above `_MID_SESSION_CAP_DATED_RESET_RE`). That
    parser resolves in the OS LOCAL timezone (`datetime.now().astimezone()`);
    this one stays in UTC throughout, consistent with every OTHER form this
    module parses (`_resolve_dated_reset`, `_resolve_bare_reset`) and with the
    real captures, which always state "(UTC)" explicitly.

    When no time-of-day is present ("resets Sunday" bare), this resolves to
    23:59:59 UTC on the next occurrence of that day -- the LAST instant the
    real reset could plausibly land, given no more specific information. This
    is a deliberately conservative (longer) choice: the caller
    (runtime_controller.py's block_until_tmux_death) uses this value directly
    as `cooldown_until` for the next retry attempt, and this feature's
    existing posture everywhere else it faces comparable uncertainty (the
    4-hour MID_SESSION_CAP_CONSERVATIVE_DEFAULT_SECONDS fallback, the
    never-SIGKILL confirm-death wait) is to wait longer rather than risk
    retrying before the cap has actually lifted. No formal P/D id names this
    specific tradeoff; it is chosen for internal consistency with the rest of
    the feature -- see this module's DECISIONS entry.
    """
    dow_str, hour, minute, meridiem = match.groups()
    target_dow = _DOW_ABBR_TO_NUM.get(dow_str[:3].lower())
    if target_dow is None:
        return None
    current_dow = now.weekday()
    days_ahead = (target_dow - current_dow) % 7
    if hour is None:
        candidate = now.replace(hour=23, minute=59, second=59, microsecond=0) + timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate
    try:
        hour_i = int(hour) % 12
        minute_i = int(minute or 0)
    except ValueError:
        return None
    if not (0 <= minute_i <= 59):
        return None
    if meridiem.lower() == "pm":
        hour_i += 12
    candidate = now.replace(hour=hour_i, minute=minute_i, second=0, microsecond=0) + timedelta(days=days_ahead)
    if candidate <= now:
        if (now - candidate).total_seconds() <= _MID_SESSION_CAP_RESET_SKEW_GRACE_SECONDS:
            return None
        candidate += timedelta(days=7)
    return candidate


def _resolve_bare_reset(match: re.Match[str], *, now: datetime) -> datetime | None:
    hour, minute, meridiem = match.groups()
    try:
        hour_i = int(hour) % 12
        minute_i = int(minute or 0)
    except ValueError:
        return None
    if not (0 <= minute_i <= 59):
        return None
    if meridiem.lower() == "pm":
        hour_i += 12
    candidate = now.replace(hour=hour_i, minute=minute_i, second=0, microsecond=0)
    if candidate <= now:
        if (now - candidate).total_seconds() <= _MID_SESSION_CAP_RESET_SKEW_GRACE_SECONDS:
            return None
        candidate += timedelta(days=1)
    return candidate


@dataclass(frozen=True)
class ClaudeMidSessionCapModal:
    """A single-sample mid-session cap classification (T-4037).

    modal_kind: "session-limit" | "weekly-limit" | "opus-limit" | "monthly-spend-limit"
    sentinel_text: the matched sentinel substring (for logging/debugging; never
        the full pane text -- callers should not persist raw pane prose)
    reset_at: absolute UTC reset instant parsed from the pane, or None if no
        reset text was present/parseable
    reset_source: "parsed" if reset_at is set, else None
    render_span_sha256: sha256 hex digest of the exact tight bottom-region
        text (`_bottom_region`, post ANSI-strip normalization) that satisfied
        the footer/chrome/glyph checks (T-4037 REBOUND round 2 Finding 3).
        Deterministic for a given `text`; a caller comparing two samples'
        `render_span_sha256` is comparing ONLY the modal's own rendered span
        -- never the full pane, which routinely carries unrelated churn above
        the modal (e.g. a ticking "Skipped for Nm Ns" status line) that would
        otherwise defeat a same-render debounce check on every real capture.
        Never the sentinel/framing text alone -- two structurally different
        renders that happen to share a sentinel line would still hash
        differently, which is the intended (stricter) behavior.
    """

    modal_kind: str
    sentinel_text: str
    reset_at: datetime | None
    reset_source: str | None
    render_span_sha256: str


def classify_claude_mid_session_cap(text: str, *, now: datetime) -> ClaudeMidSessionCapModal | None:
    """Classify a SINGLE pane sample as a mid-session cap modal, or None.

    Unlike `classify_claude_post_start_subscription_cap`, this needs no
    baseline-diff -- it runs on one pane capture, taken any time after a
    runtime is already up and healthy (T-4037: the gap that classifier can't
    cover).

    T-4037 REBOUND round 1 Finding 1: neither variant is classified on
    framing phrases alone anymore. That was the false-positive gap the
    reviewer found: prose *discussing* this bug, quoting the same
    sentinel/framing phrases, would trip the pre-fix check on a flag-on /
    non-comms / fallback-viable binding that drives a real terminate call.

    T-4037 REBOUND round 2 Finding 1: round 1's fix (modal render CHROME,
    scoped to a loose trailing 20-line window) was still defeated by ordinary
    ASCII prose that quoted the modal byte-for-byte and was followed by a
    SHORT amount of further text (0-11 trailing lines still fit inside that
    window). This function now additionally requires
    `_footer_is_final_line`: the modal's own confirm/cancel footer must be the
    final modal line, with only the exact optional task-status row after it.
    Prose quoting it,
    or the Claude Code UI's own always-rendered input-box/status tail once
    the modal has closed, is not. This is a single-sample, POSITION-anchored
    structural check -- strictly stronger than the round-1 substring window,
    which is also tightened (20 -> 16 lines, see `_bottom_region`).

    T-4037 REBOUND round 2 Finding 2: `_has_claude_session_cap_modal_chrome`
    and `_has_claude_spend_limit_modal_chrome` additionally require the "❯"
    selector glyph immediately adjacent to the modal's highlighted option
    text, not merely present anywhere in the window -- "❯" alone is also the
    ordinary idle-prompt cursor (see `READY_PROMPT_LINE` in
    tools/claude_quota_probe.py).

    `_claude_cap_sentinel` and `_has_claude_rate_limit_modal_framing`
    themselves stay byte-unchanged -- both are also used by the post-start
    classifier above, which has its own baseline-diff defence and is out of
    scope for this fix.

    Residual risk (acknowledged, not eliminated; corrected from round 1's
    overclaim that this was merely "unlikely"): a pane sample whose FINAL
    static content -- with nothing rendered below it -- is a "❯"-glyph-exact
    reproduction of the full modal, chrome, footer and all, still classifies
    on THIS function alone. Closing that residual gap requires more than one
    sample: the caller (`tools/runtime_controller.py`'s
    `_observe_and_publish_mid_session_cap`) additionally requires TWO
    consecutive samples, ~60s apart, whose `render_span_sha256` match exactly,
    before it will ever publish `modal_present: true` or permit the swap
    action -- see that function's docstring. So the true remaining bar, after
    both layers, is: a pane whose final static content is, ACROSS TWO
    SEPARATE POLLS, a glyph-exact reproduction of the modal with nothing
    rendered below it either time. This function alone does not enforce that
    second layer; it is documented here so a reader of this function in
    isolation does not understate the risk.

    T-4047 DECISION: this residual is formally ACCEPTED AND BOUNDED, not
    closed with a further structural anchor. Re-verified against the current
    code, the bar an adversarial pane sample must clear is FOUR independent
    gates: (1) `_claude_cap_sentinel`'s exact "you've hit your
    (session|weekly|opus) limit · resets ..." phrase (CAP_SENTINELS); (2)
    `_footer_is_final_line` -- the modal's confirm/cancel footer must be the
    final modal line (apart from the exact optional task-status row); (3) the "❯" selector glyph
    immediately adjacent to the modal's own highlighted option text, not
    merely present in the window (`_SESSION_OPTION_GLYPH_RE`/
    `_SPEND_OPTION_GLYPH_RE`); (4) TWO consecutive samples ~60s apart whose
    `render_span_sha256` matches byte-for-byte
    (`_observe_and_publish_mid_session_cap`'s debounce). Recovery's cap-modal
    reaper (T-4024, a different persona's tool reading the same published
    state cross-account -- see `read_mid_session_cap_state`'s docstring) is a
    fifth, independent re-verification layer before anything ever acts on
    this. A further structural anchor (e.g. an ANSI/style byte a transcript
    quote structurally can't reproduce) was considered and deferred: absent
    an observed false positive, engineering past a four-gate-plus-cross-
    account-reverify bar isn't justified by P7, and would be exactly the
    novelty-driven engineering D12 warns against.
    """
    if not _footer_is_final_line(text):
        return None
    bottom = _bottom_region(text)
    sentinel = _claude_cap_sentinel(bottom)
    if sentinel is not None and _has_claude_session_cap_modal_chrome(text):
        kind = _cap_sentinel_kind(sentinel)
        reset_at = _parse_claude_mid_session_cap_reset_at(text, now=now)
        return ClaudeMidSessionCapModal(
            modal_kind=kind, sentinel_text=sentinel, reset_at=reset_at,
            reset_source="parsed" if reset_at is not None else None,
            render_span_sha256=_sha256_text(bottom),
        )
    if _has_claude_spend_limit_modal_chrome(text):
        reset_at = _parse_claude_mid_session_cap_reset_at(text, now=now)
        return ClaudeMidSessionCapModal(
            modal_kind="monthly-spend-limit", sentinel_text=_SPEND_LIMIT_SENTINEL,
            reset_at=reset_at, reset_source="parsed" if reset_at is not None else None,
            render_span_sha256=_sha256_text(bottom),
        )
    return None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str | None:
    try:
        raw, _st = _read_regular_file_no_follow(path, max_bytes=_MAX_STATUSLINE_BYTES)
        return hashlib.sha256(raw).hexdigest()
    except OSError:
        return None


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _read_statusline(path: Path) -> Mapping[str, Any] | None:
    try:
        raw, _st = _read_regular_file_no_follow(path, max_bytes=_MAX_STATUSLINE_BYTES)
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, Mapping) or payload.get("schema") != STATUSLINE_SCHEMA:
        return None
    return payload


def _read_regular_file_no_follow(path: Path, *, max_bytes: int) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            raise OSError("provider evidence file is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = st.st_size
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks), st
    finally:
        os.close(fd)


# T-4459: the probe-taken fallback reads at most this many transcripts written since the
# submit (newest first), each from its tail only.
_PROBE_TRANSCRIPT_MAX_CANDIDATES = 4
_PROBE_TRANSCRIPT_MTIME_SLACK_SECONDS = 5.0


def _read_tail_no_follow(path: Path, *, max_bytes: int) -> bytes:
    """The last ``max_bytes`` of a regular file, opened without following a symlink."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError("not a regular file")
        os.lseek(fd, max(0, st.st_size - max_bytes), os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = min(st.st_size, max_bytes)
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _probe_prompt_in_recent_transcript(account_home: Path, marker: bytes, since: datetime) -> bool:
    """Did a transcript written since ``since`` record the probe as a USER event? (T-4459)

    history.jsonl is not proof of a swallow. Claude Code 2.1.285 buffers history writes
    (a pendingEntries queue flushed under a lock that can fail with LockContended), and
    an entry's timestamp is its add time, not its write time. So in T-3026 attempt 4 the
    history lacked a probe the harness had already taken, and the controller re-typed it
    into a second turn. The transcript's user event is written at submit, so it settles
    the question. Only .jsonl files under ~/.claude/projects touched since the submit are
    read, newest first, bounded, never following a symlink. The nonce is fresh per probe,
    so an older transcript can't carry it.
    """
    projects = account_home / ".claude" / "projects"
    cutoff = since.timestamp() - _PROBE_TRANSCRIPT_MTIME_SLACK_SECONDS
    candidates: list[tuple[float, str]] = []
    try:
        with os.scandir(projects) as project_dirs:
            for project in project_dirs:
                if not project.is_dir(follow_symlinks=False):
                    continue
                try:
                    with os.scandir(project.path) as entries:
                        for entry in entries:
                            if not entry.name.endswith(".jsonl") or not entry.is_file(follow_symlinks=False):
                                continue
                            mtime = entry.stat(follow_symlinks=False).st_mtime
                            if mtime >= cutoff:
                                candidates.append((mtime, entry.path))
                except OSError:
                    continue
    except OSError:
        return False
    candidates.sort(reverse=True)
    for _mtime, path in candidates[:_PROBE_TRANSCRIPT_MAX_CANDIDATES]:
        try:
            tail = _read_tail_no_follow(Path(path), max_bytes=_MAX_PROMPT_HISTORY_TAIL_BYTES)
        except OSError:
            continue
        for raw in tail.splitlines():
            if marker not in raw or len(raw) > _MAX_JSONL_LINE_BYTES:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue  # the tail's first line may be cut mid-record
            if isinstance(obj, Mapping) and obj.get("type") == "user":
                return True
    return False


def _confined_transcript(account_home: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    raw = Path(value)
    if not raw.is_absolute():
        return None
    try:
        projects = (account_home / ".claude" / "projects").resolve()
        resolved = raw.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(projects):
        return None
    try:
        _raw, st = _read_regular_file_no_follow(resolved, max_bytes=_MAX_TRANSCRIPT_BYTES)
    except OSError:
        return None
    return resolved


def _completion_transcript(account_home: Path, value: Any) -> tuple[Path | None, str]:
    """Classify the statusline's transcript_path for the provider-completion probe.

    ("ok") the confined, bounded regular file. ("pending") a well-formed path
    confined to ~/.claude/projects that is not usable YET: absent, because a fresh
    session hasn't flushed its jsonl, or over _MAX_TRANSCRIPT_BYTES, because the
    statusline still names a prior long session. The probe keeps polling, and a
    condition that never clears still ends as provider-completion-not-proven at
    the deadline. ("invalid") anything that can never become valid: non-string or
    relative path, escape from ~/.claude/projects, symlink, non-regular file, or
    any other OS error.

    T-4151: all of these used to be terminal. On specialist 2026-09-28T13:34Z, the first
    sample after a ctxfill restart named a transcript that did not exist yet, and
    the candidate was torn down 10s after launch. Never reads the file."""
    if not isinstance(value, str) or not value:
        return None, "invalid"
    raw = Path(value)
    if not raw.is_absolute():
        return None, "invalid"
    try:
        projects = (account_home / ".claude" / "projects").resolve()
        resolved = raw.resolve()
    except OSError:
        return None, "invalid"
    if not resolved.is_relative_to(projects):
        return None, "invalid"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(resolved, flags)
    except FileNotFoundError:
        return None, "pending"
    except OSError:
        return None, "invalid"
    try:
        st = os.fstat(fd)
    except OSError:
        return None, "invalid"
    finally:
        os.close(fd)
    if not stat.S_ISREG(st.st_mode):
        return None, "invalid"
    if st.st_size > _MAX_TRANSCRIPT_BYTES:
        return None, "pending"
    return resolved, "ok"


def _transcript_boundary(account_home: Path, observation: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if observation is None:
        return None
    path = _confined_transcript(account_home, observation.get("transcript_path"))
    if path is None:
        return None
    try:
        _raw, st = _read_regular_file_no_follow(path, max_bytes=_MAX_TRANSCRIPT_BYTES)
    except OSError:
        return None
    relative = str(path.relative_to((account_home / ".claude" / "projects").resolve()))
    return {
        "path": path,
        "offset": st.st_size,
        "inode": st.st_ino,
        "device": st.st_dev,
        "relative_sha256": _sha256_text(relative),
    }


def _assistant_delta_has_nonce(path: Path, *, start: int, marker: str) -> tuple[bool, int, int, int]:
    try:
        raw_all, st = _read_regular_file_no_follow(path, max_bytes=_MAX_TRANSCRIPT_BYTES)
    except OSError:
        return False, start, 0, 1
    if start < 0 or st.st_size < start or st.st_size > _MAX_TRANSCRIPT_BYTES:
        return False, start, 0, 1
    delta_size = st.st_size - start
    if delta_size > _MAX_TRANSCRIPT_DELTA_BYTES:
        return False, st.st_size, 0, 1
    raw = raw_all[start:]
    if raw and not raw.endswith(b"\n"):
        raw = raw.rsplit(b"\n", 1)[0] if b"\n" in raw else b""
    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) > _MAX_TRANSCRIPT_LINES:
        return False, st.st_size, 0, 1
    assistant_events = 0
    malformed = 0
    for line in lines:
        if len(line) > _MAX_JSONL_LINE_BYTES:
            # T-4452: too big to parse is not malformed; skip it. A skip can
            # only miss the reply (the probe keeps waiting), never pass one.
            # No prefix peek: Claude Code writes the top-level "type" after
            # "attachment"/"message", so a prefix shows a nested "type" first.
            continue
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            malformed += 1
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role is not None and role != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        assistant_events += 1
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip() == marker:
                return True, st.st_size, assistant_events, malformed
    return False, st.st_size, assistant_events, malformed


BACKEND_OVERRIDE_FLAG = "PANTHEON_CLAUDE_BACKEND_OVERRIDE"
BACKEND_KEY_RELPATH = Path(".claude") / "backend-api-key"
_APIKEY_HELPER = Path(__file__).resolve().parents[1] / "claude_apikey_helper.py"


class ClaudeBackendOverrideError(RuntimeError):
    """T-6005: the dark base_url backend override failed a safety check.

    Deliberately not an OSError/ValueError: the controller's fail-open settings
    wiring must not swallow it. Messages never contain key material.
    """


def _claude_login_present(home: Path) -> bool:
    """Minimal detector for a Claude subscription login on this account.

    Broad on purpose: any `.claude/.credentials.json` entry (even empty or a
    link), a non-empty `oauthAccount` in `.claude.json`, or an OAuth token env.
    """
    try:
        os.lstat(home / ".claude" / ".credentials.json")
        return True
    except FileNotFoundError:
        pass
    except OSError:
        return True  # cannot prove absence: fail closed
    try:
        data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("oauthAccount"):
            return True
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        return True
    return bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))


def _check_key_file(path: Path) -> None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise ClaudeBackendOverrideError("backend key file missing") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ClaudeBackendOverrideError("backend key file is a symlink") from None
        raise ClaudeBackendOverrideError(
            f"backend key file unreadable ({type(exc).__name__})"
        ) from None
    try:
        st = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(st.st_mode):
        raise ClaudeBackendOverrideError("backend key file is not a regular file")
    if st.st_uid != os.geteuid():
        raise ClaudeBackendOverrideError("backend key file is not owned by the running uid")
    if stat.S_IMODE(st.st_mode) != 0o600:
        raise ClaudeBackendOverrideError("backend key file mode is not 0600")


def _check_backend_override(launch: HarnessLaunchConfig | None, home: Path) -> str | None:
    """Return the validated base_url, or None when no override is in play.

    Raises ClaudeBackendOverrideError on any failed check (fail closed).
    """
    native = launch.native if launch is not None and isinstance(launch.native, Mapping) else {}
    flag = os.environ.get(BACKEND_OVERRIDE_FLAG) == "1"
    if "base_url" not in native:
        if flag:
            raise ClaudeBackendOverrideError(
                f"{BACKEND_OVERRIDE_FLAG}=1 set but the profile has no base_url"
            )
        return None
    assert launch is not None
    if launch.meter != "api" or not flag:
        raise ClaudeBackendOverrideError(
            f"base_url requires meter: api and {BACKEND_OVERRIDE_FLAG}=1"
        )
    base_url = native["base_url"]
    parts = urlsplit(base_url) if isinstance(base_url, str) else None
    if (
        parts is None
        or parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or any(ch.isspace() or ord(ch) < 32 for ch in base_url)
    ):
        raise ClaudeBackendOverrideError("base_url must be an https URL without userinfo")
    if _claude_login_present(home):
        raise ClaudeBackendOverrideError(
            "account holds a Claude subscription login; refusing third-party backend"
        )
    _check_key_file(home / BACKEND_KEY_RELPATH)
    return base_url


class ClaudeHarnessAdapter(PantheonHarnessAdapter):
    """Harness adapter for claude-code (Orchestrator / Specialist / Recovery instances).

    Args:
        role_domain: ORG-027 instance role-key (e.g. "orchestrator").
        claude_home: path to the claude user home; defaults to Path.home().
                     Override in tests to avoid reading real home-dir config.
    """

    HARNESS_NAME: str = "claude"

    def __init__(
        self,
        role_domain: str,
        claude_home: Path | None = None,
        mailbox_path: Path | None = None,
        launch: HarnessLaunchConfig | None = None,
    ) -> None:
        super().__init__(role_domain, launch=launch)
        self._claude_home = claude_home if claude_home is not None else Path.home()
        self._mailbox_path = mailbox_path

    # -------------------------------------------------------------------------
    # Typed lifecycle contract
    # -------------------------------------------------------------------------

    def capabilities(self) -> HarnessCapabilities:
        hook_names = (
            "PreToolUse",
            "PermissionRequest",
            "PostToolUse",
            "PostToolUseFailure",
            "Notification",
            "UserPromptSubmit",
            "SessionStart",
            "SessionEnd",
            "Stop",
            "SubagentStart",
            "SubagentStop",
            "PreCompact",
            "PostCompact",
            "TeammateIdle",
            "TaskCompleted",
            "ConfigChange",
            "WorktreeCreate",
            "WorktreeRemove",
            "InstructionsLoaded",
        )
        hooks = {
            name: HookCapability(
                event=name,
                matchers=name
                not in {
                    "UserPromptSubmit",
                    "SessionStart",
                    "SessionEnd",
                    "Stop",
                    "SubagentStart",
                    "SubagentStop",
                    "PreCompact",
                    "PostCompact",
                },
                can_block=name
                in {
                    "PreToolUse",
                    "PermissionRequest",
                    "UserPromptSubmit",
                    "Stop",
                    "SubagentStop",
                    "TaskCompleted",
                    "ConfigChange",
                    "WorktreeCreate",
                },
                can_continue=name in {"Stop", "SubagentStop"},
                handler_types=("command", "prompt", "agent"),
            )
            for name in hook_names
        }
        return HarnessCapabilities(
            harness="claude",
            session_resume_by_id=True,
            session_resume_last=True,
            structured_progress=True,
            structured_output_schema=True,
            local_generated_memory=True,
            native_stop_continuation=True,
            inbound_mechanisms=("channel", "maildir"),
            hook_events=hooks,
        )

    def _refuse_headless_backend_override(self) -> None:
        if self.launch is not None and "base_url" in self.launch.native:
            raise ClaudeBackendOverrideError(
                "base_url backend override is not supported on headless launches"
            )

    def build_exec(self, request: ExecutionRequest) -> CommandSpec:
        self._refuse_headless_backend_override()
        argv = [
            str(self.executable),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        model = request.model or (self.launch.model if self.launch else None)
        if model:
            argv.extend(["--model", model])
        if request.output_schema:
            argv.extend(["--json-schema", str(request.output_schema)])
        return CommandSpec(
            argv=tuple(argv),
            stdin=request.prompt,
            cwd=request.cwd,
        )

    def build_resume(
        self,
        session: SessionHandle,
        prompt: str | None = None,
    ) -> CommandSpec:
        self._refuse_headless_backend_override()
        cwd = session.cwd if session.cwd is not None else Path.cwd()
        return CommandSpec(
            argv=(
                str(self.executable),
                "-p",
                "--resume",
                session.session_id,
                "--output-format",
                "stream-json",
                "--verbose",
            ),
            stdin=prompt,
            cwd=cwd,
        )

    def parse_event(self, line: str) -> HarnessEvent | None:
        raw, parse_error = parse_json_object(line)
        if parse_error:
            return HarnessEvent(
                harness="claude",
                kind="error",
                session_id=None,
                message=parse_error,
                usage=None,
                raw={"line": line.rstrip("\n"), "category": "provider"},
            )
        if raw is None:
            return None

        native_type = raw.get("type")
        session_id = raw.get("session_id")
        session_id = session_id if isinstance(session_id, str) else None
        usage_raw = raw.get("usage")
        usage = _claude_usage(usage_raw, raw.get("total_cost_usd"))
        message: str | None = None
        kind = "progress"

        if native_type == "system" and raw.get("subtype") == "init":
            kind = "session_started"
        elif native_type == "assistant":
            content = raw.get("message")
            if isinstance(content, dict):
                message = _claude_text(content.get("content"))
            kind = "progress"
        elif native_type in {"tool_progress", "tool_use"}:
            kind = "tool_started"
        elif native_type == "tool_use_summary":
            kind = "tool_completed"
            summary = raw.get("summary")
            message = summary if isinstance(summary, str) else None
        elif native_type == "rate_limit_event":
            kind = "retry_scheduled"
            message = str(raw.get("message") or "rate limit event")
        elif native_type == "result":
            result = raw.get("result")
            message = result if isinstance(result, str) else None
            is_error = bool(raw.get("is_error")) or raw.get("subtype") not in {
                None,
                "success",
            }
            kind = "turn_failed" if is_error else "turn_completed"
        return HarnessEvent(
            harness="claude",
            kind=kind,
            session_id=session_id,
            message=message,
            usage=usage,
            raw=raw,
        )

    def finalize(
        self,
        events: Iterable[HarnessEvent],
        *,
        returncode: int,
        stderr: str,
    ) -> ExecutionResult:
        captured = tuple(events)
        return finalize_events(
            "claude",
            captured,
            returncode=returncode,
            stderr=stderr,
            provider_billing=_claude_provider_billing(captured),
        )

    def deliver_inbound(self, request: InboundRequest) -> DeliveryReceipt:
        return deliver_to_mailbox(
            "claude",
            request,
            default_inbox=self._mailbox_path,
            default_sender="harness-adapter",
        )

    def spawn_error_event(self, exc: OSError) -> HarnessEvent:
        return HarnessEvent(
            harness="claude",
            kind="error",
            session_id=None,
            message=f"{type(exc).__name__}: {exc}",
            usage=None,
            raw={"category": "config", "retryable": False},
        )

    def validate_launch_config(self) -> tuple[DiagnosticObservation, ...]:
        if self.launch is None:
            return (
                DiagnosticObservation(
                    check_id="claude.runtime_profile",
                    status="warn",
                    summary="legacy adapter has no immutable runtime profile",
                    authoritative=True,
                    evidence={"profile": "", "legacy": "true"},
                ),
            )
        unknown = sorted(set(self.launch.native) - CLAUDE_NATIVE_KEYS)
        status = "fail" if unknown else "pass"
        override_obs: tuple[DiagnosticObservation, ...] = ()
        if "base_url" in self.launch.native or os.environ.get(BACKEND_OVERRIDE_FLAG) == "1":
            try:
                _check_backend_override(self.launch, self._claude_home)
                override_status, override_summary = "pass", "backend override guard passes"
            except ClaudeBackendOverrideError as exc:
                override_status, override_summary = "fail", str(exc)
            override_obs = (
                DiagnosticObservation(
                    check_id="claude.backend_override",
                    status=override_status,
                    summary=override_summary,
                    authoritative=True,
                    evidence={"harness": "claude", "profile": self.launch.profile},
                ),
            )
        return override_obs + (
            DiagnosticObservation(
                check_id="claude.runtime_profile",
                status=status,
                summary=(
                    "claude runtime profile is valid"
                    if status == "pass"
                    else "claude runtime profile contains unknown native keys"
                ),
                authoritative=True,
                evidence={
                    "harness": self.launch.harness,
                    "profile": self.launch.profile,
                    "mode": self.launch.mode,
                    "meter": self.launch.meter,
                    "quota_source": self.launch.quota_source,
                    "unknown_native_keys": ",".join(unknown),
                    "native_namespace": "claude",
                },
            ),
        )

    def describe_surfaces(self, cwd: Path) -> HarnessSurfaceDescriptors:
        standing = self.standing_prompt_filename()
        discovered = _find_upward(cwd, standing)
        return HarnessSurfaceDescriptors(
            standing=StandingInstructionsDescriptor(
                filename=standing,
                discovered=(discovered,) if discovered else (),
                load_order=("cwd-upward", "claude-home"),
                authoritative=True,
                evidence={"harness": "claude", "cwd": str(cwd)},
            ),
            auth=self.auth_descriptor(),
            memory=MemoryDescriptor(
                available=True,
                enabled=False,
                readable=None,
                writable=None,
                evidence={
                    "harness": "claude",
                    "enabled_by_adapter": "false",
                    "claim": "descriptor-only",
                },
            ),
            mcp=McpDescriptor(
                config_paths=(self.mcp_server_config_path(), cwd / ".mcp.json"),
                configured_servers=(),
                scope=("user", "project"),
                authoritative=False,
                evidence={"harness": "claude", "native_format": "json"},
            ),
            roles=RoleDescriptor(
                config_paths=(self._claude_home / ".claude" / "settings.json",),
                configured_roles=(),
                delegation_modes=("agent-tool",),
                authoritative=False,
                evidence={"harness": "claude", "schema": "claude-native"},
            ),
        )

    def auth_descriptor(self) -> AuthDescriptor:
        return AuthDescriptor(
            mechanisms=("oauth", "api-key"),
            material_paths=(
                self._claude_home / ".claude" / ".credentials.json",
                self._claude_home / ".claude" / "settings.json",
            ),
            observed_state="unverified",
            authoritative=False,
            evidence={
                "harness": "claude",
                "descriptor_only": "true",
                "live_probe": "not-run",
            },
        )

    def build_interactive_launch(self, target: TmuxTarget, *, cwd: Path) -> CommandSpec:
        base_url = _check_backend_override(self.launch, self._claude_home)
        argv = [str(self.executable)]
        channels = (
            self.launch.native.get("channels")
            if self.launch and isinstance(self.launch.native, dict)
            else None
        )
        if isinstance(channels, str) and channels:
            argv.extend(["--channels", channels])
        model = self.launch.model if self.launch else None
        if model:
            argv.extend(["--model", model])
        effort = (
            self.launch.native.get("effort")
            if self.launch and isinstance(self.launch.native, dict)
            else None
        )
        if isinstance(effort, str) and effort:
            argv.extend(["--effort", effort])
        permission_mode = (
            self.launch.native.get("permission_mode")
            if self.launch and isinstance(self.launch.native, dict)
            else None
        )
        if permission_mode is None:
            permission_mode = "bypassPermissions"
        if isinstance(permission_mode, str):
            argv.extend(["--permission-mode", permission_mode])
        env = {
                "PANTHEON_TMUX_SESSION": target.session,
                "PANTHEON_TMUX_WINDOW": target.window,
                "PANTHEON_TMUX_PANE": target.pane,
                # T-5843: seats receive injected text (inbox_consumer, mailbox_wake, Recovery
                # alerts); a typed '@/path' would load the file pre-model, bypassing
                # permission prompts and the worker guard. Probed live (2.1.316): the flag
                # turns every mention into an unread reference. T-4382 stays as the
                # per-site layer for harnesses without it.
                "CLAUDE_CODE_DISABLE_FILE_MENTIONS": "1",
        }
        if base_url is not None:
            # Not secret; the key travels only via apiKeyHelper (T-6005).
            env["ANTHROPIC_BASE_URL"] = base_url
        return CommandSpec(argv=tuple(argv), stdin=None, cwd=cwd, env=env)

    def interactive_statusline_settings(self, *, output: Path) -> dict[str, Any]:
        """T-4024: Claude settings that make the MAIN session write the status-line
        observation that prepare/assess_interactive_provider_completion (and
        quota_observer) read from ``output``.

        Only claude_quota_probe's isolated session ever had a statusLine
        (T-2382). The main session had none, so provider completion could never
        be proven and every claude start through ensure_runtime was torn down at
        90s. The capture helper prints nothing, so the TUI's status line stays
        empty. The controller writes this to a file and passes it as --settings.
        """
        helper = Path(__file__).resolve().parents[1] / "claude_statusline_capture.py"
        command = " ".join(
            shlex.quote(part)
            for part in ("/usr/bin/python3", str(helper), "--output", str(output))
        )
        settings: dict[str, Any] = {"statusLine": {"type": "command", "command": command}}
        if _check_backend_override(self.launch, self._claude_home) is not None:
            settings["apiKeyHelper"] = " ".join(
                shlex.quote(part)
                for part in (
                    "/usr/bin/python3",
                    str(_APIKEY_HELPER),
                    "--key-file",
                    str(self._claude_home / BACKEND_KEY_RELPATH),
                )
            )
        return settings

    def quota_observer(self, state_dir: Path) -> QuotaObserver:
        profile = self.launch.profile if self.launch else "legacy"
        run_id = os.environ.get("PANTHEON_ACCEPTANCE_RUN_ID")
        overlay = os.environ.get("PANTHEON_ACCEPTANCE_QUOTA_OVERLAY")
        if run_id is None and overlay is None:
            return ClaudeStatuslineQuotaObserver(
                state_dir / "quota" / "claude-statusline.json",
                profile=f"claude/{profile}",
            )
        if not _valid_acceptance_overlay_config(run_id, overlay):
            return UnavailableQuotaObserver(
                profile=f"claude/{profile}",
                reason="invalid-acceptance-overlay-config",
                evidence={
                    "provider": "claude",
                    "source": "acceptance-overlay",
                    "fixture": "true",
                    "run_id_present": str(run_id is not None).lower(),
                    "overlay_present": str(overlay is not None).lower(),
                },
            )
        assert run_id is not None
        assert overlay is not None
        canonical_path = state_dir / "quota" / "claude-statusline.json"
        return ClaudeAcceptanceQuotaOverlayObserver(
            Path(overlay),
            canonical_path=canonical_path,
            profile=f"claude/{profile}",
            run_id=run_id,
            state_dir=state_dir,
        )

    def assess_post_start_readiness(
        self,
        *,
        harness: str,
        profile: str,
        baseline: PostStartPaneSample,
        samples: Iterable[PostStartPaneSample],
        now: datetime | None = None,
    ) -> PostStartAssessment:
        sample_tuple = tuple(samples)
        changed = tuple(
            sample.capture_index
            for sample in sample_tuple
            if sample.sha256 != baseline.sha256
        )
        resolved_now = now if now is not None else datetime.now(timezone.utc)
        # T-5791: the launch itself is the fresh event at post-start (the banner
        # differs from the pre-launch baseline); two consecutive changed samples
        # holding the same active-region banner corroborate it.
        auth_candidate: str | None = None
        auth_hits = 0
        for sample in sample_tuple:
            if sample.sha256 == baseline.sha256:
                continue
            auth = classify_claude_authentication_loss(sample.text)
            if auth is not None:
                if auth[0] == auth_candidate:
                    auth_hits += 1
                else:
                    auth_candidate, auth_hits = auth[0], 1
                if auth_hits >= 2:
                    return PostStartAssessment(
                        schema="pantheon-harness-post-start-assessment/v1",
                        harness=harness, profile=profile, status="authentication-loss",
                        cause="claude-authentication-loss", pattern_id=auth[0],
                        baseline_sha256=baseline.sha256, capture_count=len(sample_tuple),
                        changed_capture_indices=changed,
                    )
            else:
                auth_candidate, auth_hits = None, 0
            pattern_id, cause = classify_claude_post_start_subscription_cap(
                baseline_text=baseline.text,
                sample_text=sample.text,
            )
            if pattern_id is not None:
                # T-4047: derive the reset instant + modal kind from the SAME
                # sample text the classifier above already matched against --
                # `_claude_cap_sentinel` re-runs its cheap regex a second
                # time rather than threading it out of
                # classify_claude_post_start_subscription_cap's return shape
                # (keeps that well-tested function's signature untouched;
                # see this ticket's DECISIONS). Never None here: a non-None
                # pattern_id structurally implies sample.text matched
                # CAP_SENTINELS (classify_claude_post_start_subscription_cap
                # requires it), so `or ""` is defensive only.
                sentinel = _claude_cap_sentinel(sample.text) or ""
                return PostStartAssessment(
                    schema="pantheon-harness-post-start-assessment/v1",
                    harness=harness,
                    profile=profile,
                    status="subscription-cap",
                    cause=cause,
                    pattern_id=pattern_id,
                    baseline_sha256=baseline.sha256,
                    capture_count=len(sample_tuple),
                    changed_capture_indices=changed,
                    cap_kind=_cap_sentinel_kind(sentinel),
                    reset_at=_parse_claude_mid_session_cap_reset_at(sample.text, now=resolved_now),
                )
        ready = any(
            sample.sha256 != baseline.sha256
            and READY_PROMPT_LINE.search(_normalize_provider_pane(sample.text))
            for sample in sample_tuple
        )
        return PostStartAssessment(
            schema="pantheon-harness-post-start-assessment/v1",
            harness=harness,
            profile=profile,
            status="ready" if ready else "unknown",
            cause=None,
            pattern_id=None,
            baseline_sha256=baseline.sha256,
            capture_count=len(sample_tuple),
            changed_capture_indices=changed,
        )

    def prepare_interactive_provider_completion(
        self,
        *,
        harness: str,
        profile: str,
        persona: str,
        account_home: Path,
        state_dir: Path,
        pane_baseline: PostStartPaneSample,
        submitted_at: datetime,
        nonce_marker: str,
        nonce_sha256: str,
    ) -> ProviderCompletionProbe | None:
        del persona
        if harness != "claude":
            return None
        statusline_path = state_dir / "quota" / "claude-statusline.json"
        baseline_sha = _sha256_file(statusline_path)
        baseline_statusline = _read_statusline(statusline_path)
        baseline_transcript = _transcript_boundary(account_home, baseline_statusline)
        prompt = (
            "Provider completion probe. Reply with exactly this marker and no extra text: "
            f"{nonce_marker}"
        )
        return ProviderCompletionProbe(
            prompt=prompt,
            nonce_marker=nonce_marker,
            nonce_sha256=nonce_sha256,
            prompt_sha256=_sha256_text(prompt),
            provider_state={
                "account_home": account_home,
                "statusline_path": statusline_path,
                "baseline_statusline_sha256": baseline_sha,
                "baseline_transcript": baseline_transcript,
                "baseline_pane_text": pane_baseline.text,
                "baseline_pane_sha256": pane_baseline.sha256,
                "submitted_at": submitted_at,
                "harness": harness,
                "profile": profile,
            },
        )

    def assess_interactive_provider_completion(
        self,
        *,
        harness: str,
        profile: str,
        probe: ProviderCompletionProbe | None,
        samples: Iterable[PostStartPaneSample],
    ) -> ProviderCompletionAssessment:
        sample_tuple = tuple(samples)
        changed = tuple(
            sample.capture_index
            for sample in sample_tuple
            if probe is not None
            and sample.sha256 != probe.provider_state.get("baseline_pane_sha256")
        )
        if probe is None or harness != "claude":
            return ProviderCompletionAssessment(
                schema=_COMPLETION_SCHEMA,
                harness=harness,
                profile=profile,
                status="not-applicable",
                cause=None,
                pattern_id=None,
                nonce_sha256=None,
                prompt_sha256=None,
                statusline_baseline_sha256=None,
                statusline_observed_sha256=None,
                capture_count=len(sample_tuple),
                changed_capture_indices=(),
            )
        baseline_text = str(probe.provider_state.get("baseline_pane_text") or "")
        # T-6235: fallback-return may launch Claude straight into the monthly
        # spend-limit panel.  In that shape the provider-completion baseline
        # ALREADY contains the modal, so the older post-start classifier's
        # deliberate baseline-diff requirement can never fire.  The
        # mid-session classifier is safe here because it is structurally
        # anchored to the final modal footer and selected-option glyph; use it
        # for both the first baseline and later samples.  This also returns the
        # parsed reset boundary so controller_main re-holds until the actual
        # provider reset rather than periodically retrying a known-blocked UI.
        observed_at = probe.provider_state.get("submitted_at")
        if not isinstance(observed_at, datetime):
            observed_at = datetime.now(timezone.utc)
        modal_samples = (baseline_text, *(sample.text for sample in sample_tuple))
        for modal_text in modal_samples:
            modal = classify_claude_mid_session_cap(modal_text, now=observed_at)
            if modal is None:
                continue
            pattern_id = (
                "claude-monthly-spend-limit-modal"
                if modal.modal_kind == "monthly-spend-limit"
                else f"claude-{modal.modal_kind}-modal"
            )
            return ProviderCompletionAssessment(
                schema=_COMPLETION_SCHEMA,
                harness=harness,
                profile=profile,
                status="subscription-cap",
                cause="provider-completion-subscription-cap",
                pattern_id=pattern_id,
                nonce_sha256=probe.nonce_sha256,
                prompt_sha256=probe.prompt_sha256,
                statusline_baseline_sha256=probe.provider_state.get("baseline_statusline_sha256"),
                statusline_observed_sha256=_sha256_file(probe.provider_state["statusline_path"]),
                capture_count=len(sample_tuple),
                changed_capture_indices=changed,
                cap_kind=modal.modal_kind,
                reset_at=modal.reset_at,
            )
        for sample in sample_tuple:
            pattern_id, cause = classify_claude_post_start_subscription_cap(
                baseline_text=baseline_text,
                sample_text=sample.text,
            )
            if pattern_id is not None:
                return ProviderCompletionAssessment(
                    schema=_COMPLETION_SCHEMA,
                    harness=harness,
                    profile=profile,
                    status="subscription-cap",
                    cause="provider-completion-subscription-cap",
                    pattern_id=pattern_id,
                    nonce_sha256=probe.nonce_sha256,
                    prompt_sha256=probe.prompt_sha256,
                    statusline_baseline_sha256=probe.provider_state.get("baseline_statusline_sha256"),
                    statusline_observed_sha256=_sha256_file(probe.provider_state["statusline_path"]),
                    capture_count=len(sample_tuple),
                    changed_capture_indices=changed,
                )
        statusline_path = probe.provider_state["statusline_path"]
        current_sha = _sha256_file(statusline_path)
        baseline_sha = probe.provider_state.get("baseline_statusline_sha256")
        statusline = _read_statusline(statusline_path)
        submitted_at = probe.provider_state["submitted_at"]
        if (
            current_sha is None
            or current_sha == baseline_sha
            or statusline is None
            or _parse_utc(statusline.get("observed_at")) is None
            or _parse_utc(statusline.get("observed_at")) < submitted_at
            or not statusline.get("session_id")
            or not statusline.get("transcript_path")
        ):
            return ProviderCompletionAssessment(
                schema=_COMPLETION_SCHEMA,
                harness=harness,
                profile=profile,
                status="unknown",
                cause="provider-completion-waiting",
                pattern_id=None,
                nonce_sha256=probe.nonce_sha256,
                prompt_sha256=probe.prompt_sha256,
                statusline_baseline_sha256=baseline_sha,
                statusline_observed_sha256=current_sha,
                capture_count=len(sample_tuple),
                changed_capture_indices=changed,
            )
        account_home = probe.provider_state["account_home"]
        transcript, transcript_state = _completion_transcript(
            account_home, statusline.get("transcript_path"))
        if transcript_state == "pending":
            return ProviderCompletionAssessment(
                schema=_COMPLETION_SCHEMA,
                harness=harness,
                profile=profile,
                status="unknown",
                cause="provider-completion-waiting",
                pattern_id=None,
                nonce_sha256=probe.nonce_sha256,
                prompt_sha256=probe.prompt_sha256,
                statusline_baseline_sha256=baseline_sha,
                statusline_observed_sha256=current_sha,
                capture_count=len(sample_tuple),
                changed_capture_indices=changed,
            )
        if transcript is None:
            cause = "provider-completion-transcript-invalid"
            start = None
            rel_hash = None
        else:
            baseline = probe.provider_state.get("baseline_transcript")
            rel_hash = _sha256_text(str(transcript.relative_to((account_home / ".claude" / "projects").resolve())))
            start = 0
            if isinstance(baseline, Mapping) and baseline.get("path") == transcript:
                try:
                    st = transcript.stat(follow_symlinks=False)
                except OSError:
                    st = None
                if (
                    st is None
                    or st.st_ino != baseline.get("inode")
                    or st.st_dev != baseline.get("device")
                    or st.st_size < int(baseline.get("offset", 0))
                ):
                    cause = "provider-completion-transcript-replaced"
                else:
                    start = int(baseline["offset"])
                    cause = None
            else:
                cause = None
        if transcript is None or cause is not None:
            return ProviderCompletionAssessment(
                schema=_COMPLETION_SCHEMA,
                harness=harness,
                profile=profile,
                status="operational-readiness",
                cause=cause,
                pattern_id=None,
                nonce_sha256=probe.nonce_sha256,
                prompt_sha256=probe.prompt_sha256,
                statusline_baseline_sha256=baseline_sha,
                statusline_observed_sha256=current_sha,
                capture_count=len(sample_tuple),
                changed_capture_indices=changed,
                transcript_relative_path_sha256=rel_hash,
                transcript_start_offset=start,
            )
        seen, end, assistant_count, malformed = _assistant_delta_has_nonce(
            transcript,
            start=start,
            marker=probe.nonce_marker,
        )
        if malformed:
            status = "operational-readiness"
            cause = "provider-completion-transcript-malformed"
        elif seen:
            status = "provider-completed"
            cause = None
        else:
            status = "unknown"
            cause = "provider-completion-waiting"
        return ProviderCompletionAssessment(
            schema=_COMPLETION_SCHEMA,
            harness=harness,
            profile=profile,
            status=status,
            cause=cause,
            pattern_id="claude-statusline-transcript-nonce" if seen else None,
            nonce_sha256=probe.nonce_sha256,
            prompt_sha256=probe.prompt_sha256,
            statusline_baseline_sha256=baseline_sha,
            statusline_observed_sha256=current_sha,
            capture_count=len(sample_tuple),
            changed_capture_indices=changed,
            transcript_relative_path_sha256=rel_hash,
            transcript_start_offset=start,
            transcript_end_offset=end,
            assistant_event_count=assistant_count,
        )

    def interactive_provider_completion_submitted(
        self,
        *,
        probe: ProviderCompletionProbe,
    ) -> bool | None:
        """Did Claude Code take the probe as a prompt at all? (T-4321)

        Claude Code appends each submitted prompt to ~/.claude/history.jsonl at
        submit time, before the turn runs (measured 2026-09-29: the entry is
        stamped ~130 ms before the transcript's user event). A probe pasted
        while the TUI is still booting can be swallowed: no history entry, no
        transcript, and the completion wait can only time out (recovery's T-3026
        handmove, 2026-09-29 19:59Z).

        True: an entry carries the nonce, or a pasted-text entry is stamped at
        or after the submit (a collapsed paste keeps its text in paste-cache,
        so its nonce isn't visible here; it still proves a submit), or (T-4459)
        a transcript written since the submit holds the nonce in a user event.
        History writes are buffered, so a taken probe can be missing there.
        False: the history is readable and holds no such entry, and no recent
        transcript has the user event either.
        None: can't tell (no history file yet, or unreadable). Callers must
        not read that as swallowed.
        """
        account_home = probe.provider_state.get("account_home")
        submitted_at = probe.provider_state.get("submitted_at")
        if not isinstance(account_home, Path) or not isinstance(submitted_at, datetime):
            return None
        history = account_home / ".claude" / "history.jsonl"
        try:
            with history.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - _MAX_PROMPT_HISTORY_TAIL_BYTES))
                tail = handle.read()
        except OSError:
            return None
        marker = probe.nonce_marker.encode("utf-8")
        submitted_ms = submitted_at.timestamp() * 1000
        for raw in tail.splitlines():
            if marker in raw:
                return True
            # Every entry carries the key; only a NON-empty one can be a paste.
            if b'"pastedContents"' not in raw or _EMPTY_PASTED_CONTENTS_RE.search(raw):
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue  # the tail's first line may be cut mid-record
            if not isinstance(entry, Mapping):
                continue
            stamp = entry.get("timestamp")
            if (
                entry.get("pastedContents")
                and isinstance(stamp, (int, float))
                and not isinstance(stamp, bool)
                and stamp >= submitted_ms
            ):
                return True
        # T-4459: an absent history entry is not a swallow while the write is buffered.
        return _probe_prompt_in_recent_transcript(account_home, marker, submitted_at)

    # -------------------------------------------------------------------------
    # Partition 1 — Generalized surfaces
    # -------------------------------------------------------------------------

    def standing_prompt_filename(self) -> str:
        """Return "CLAUDE.md" — the claude-code standing-prompt filename."""
        return "CLAUDE.md"

    def mcp_server_config_path(self) -> Path:
        """Return ~/.claude.json — user/local MCP registrations live here."""
        return self._claude_home / ".claude.json"

    def hooks_config_path(self) -> Path:
        """Return ~/.claude/settings.json — hooks live in the same settings file."""
        return self._claude_home / ".claude" / "settings.json"

    def spawn_subagent(self, role: str, config_overrides: dict[str, Any]) -> dict[str, Any]:
        """Return a claude-code Agent-tool spawn descriptor for the given role.

        Shape (skeleton stub; not wired to real subprocess):
            {"harness": "claude", "role": role, "isolation": "none",
             "config_overrides": config_overrides}

        Runtime wiring uses the Agent tool with isolation and prompt parameters;
        see the team dispatch pattern in feedback_implementer_dispatch_isolation_worktree_pattern.
        """
        return {
            "harness": "claude",
            "role": role,
            "isolation": "none",
            "config_overrides": config_overrides,
        }

    def resume_session(self, session_id: str) -> dict[str, Any]:
        """Return a claude --resume <session_id> descriptor.

        Shape: {"harness": "claude", "cli_args": ["--resume", session_id]}

        The session_id maps to a JSONL session file under
        ~/.claude/projects/<cwd-hash>/<session_id>.jsonl.
        The ho_resumer.py tool drives this surface at runtime.
        """
        return {
            "harness": "claude",
            "cli_args": ["--resume", session_id],
            "session_id": session_id,
        }

    def exec_headless(self, prompt: str, **kwargs: Any) -> list[str]:
        """Return ["claude", "-p", prompt] — the claude headless execution args.

        Args:
            prompt: the prompt string to pass to ``claude -p``.
            **kwargs: reserved for future harness-specific flags (model, etc.).

        Returns:
            CLI args list suitable for subprocess.run().
        """
        return [str(self.executable), "-p", prompt]

    def auth_status(self) -> dict[str, Any]:
        """Return auth state dict for claude-code.

        Shape:
            {"harness": "claude",
             "key_source": "ANTHROPIC_API_KEY",
             "settings_path": "<claude_home>/.claude/settings.json"}
        """
        return {
            "harness": "claude",
            "key_source": "ANTHROPIC_API_KEY",
            "settings_path": str(self.mcp_server_config_path()),
            "observed_state": self.auth_descriptor().observed_state,
            "descriptor_only": True,
        }

    # -------------------------------------------------------------------------
    # Partition 2 — Bridge surfaces
    # -------------------------------------------------------------------------

    def inject_inbound(self, message: str, **kwargs: Any) -> None:
        """Compatibility shim over durable maildir delivery.

        BRIDGE SURFACE — mechanism: server pushes via the ``claude/channel`` MCP
        endpoint into the active session.  This capability is claude-code-specific
        and does NOT generalize to other harnesses (codex does not implement it;
        live-verified T-0714).

        ``deliver_inbound`` returns the receipt new callers should inspect.
        Queueing proves durability only; a separate Claude wake/channel bridge
        advances the receipt to submitted or acknowledged.
        """
        request = InboundRequest(
            message=message,
            correlation_id=str(kwargs.pop("correlation_id", "legacy-inbound")),
            source=str(kwargs.pop("source", "legacy")),
            reply_route={str(key): str(value) for key, value in kwargs.items()},
        )
        self.deliver_inbound(request)
        return None

    def approval_defaults(self) -> dict[str, Any]:
        """Return claude-code unattended-mode defaults.

        Per the HC principle (HarnessAdapter.approval_defaults, T-4929; ORG-063 s1;
        the operator TG 11742/11753 via T-4939) every harness gets the maximal unlock,
        which for claude is ``bypassPermissions``, with the unix account as the
        boundary (T-4935). Blocking PreToolUse hooks (worker-guard) and any
        ``permissions.deny``/``ask`` rules still hold under this mode. The
        one-time bypass warning dialog is pre-accepted by tools.bypass_consent
        (user-scope settings; ``--settings`` does not count).

        Shape:
            {"permission_mode": "bypassPermissions",
             "flag": "--permission-mode bypassPermissions",
             "tool_approval": "bypass-permissions"}
        """
        return {
            "permission_mode": "bypassPermissions",
            "flag": "--permission-mode bypassPermissions",
            "tool_approval": "bypass-permissions",
        }

    # -------------------------------------------------------------------------
    # Partition 3 — NOT abstracted (documented per-harness attributes)
    # -------------------------------------------------------------------------

    @property
    def sandbox_config(self) -> dict[str, Any]:
        """Describe Claude Code's provider-specific sandbox and permission modes."""
        return {
            "harness": "claude",
            "mechanism": "native sandbox settings plus permission modes",
            "description": (
                "Claude Code has native sandbox configuration and permission modes. "
                "Pantheon's Unix-account boundary remains an additional org-tier layer."
            ),
        }

    @property
    def profiles_config(self) -> dict[str, Any]:
        """claude-code profiles are per-instance settings.json files (org-tier).

        No ``-p <profile>`` flag analog.  Each Pantheon agent (Orchestrator/Specialist/Recovery) has
        its own home-dir settings.json; profile separation is achieved via Unix-user
        separation.
        """
        return {
            "harness": "claude",
            "mechanism": "per-instance settings.json via Unix user separation",
            "description": (
                "No -p <profile> flag. Each agent has its own home-dir settings.json; "
                "profile separation is achieved via distinct Unix accounts."
            ),
        }

    @property
    def app_server_config(self) -> dict[str, Any]:
        """Describe Claude Code remote-control without equating it to Codex app-server."""
        return {
            "harness": "claude",
            "supported": True,
            "surfaces": ["remote-control"],
            "description": "Remote-control is native; its wire and policy are Claude-specific.",
        }

    @property
    def cloud_config(self) -> dict[str, Any]:
        """claude-code has no cloud-task ingest analog.

        Returns {"supported": False, "reason": "no claude-code analog"} so consumers
        can distinguish "no analog" from "configured-but-empty".
        """
        return {
            "harness": "claude",
            "supported": False,
            "reason": "no claude-code analog",
        }

    @property
    def doctor_config(self) -> dict[str, Any]:
        """Describe Claude's native diagnostics without claiming Codex equivalence."""
        return {
            "harness": "claude",
            "mechanism": "/doctor plus --safe-mode/config checks",
            "tools": ["/doctor", "--safe-mode"],
            "description": (
                "Native checks remain namespaced Claude diagnostics; Pantheon also "
                "uses JSONL/session and process checks."
            ),
        }


def _valid_acceptance_overlay_config(run_id: str | None, overlay: str | None) -> bool:
    if not valid_acceptance_run_id(run_id):
        return False
    if not isinstance(overlay, str) or not overlay.strip():
        return False
    return True


def _claude_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts) or None


def _claude_usage(value: Any, total_cost: Any) -> Usage | None:
    if not isinstance(value, dict) and not isinstance(total_cost, (int, float)):
        return None
    value = value if isinstance(value, dict) else {}
    return Usage(
        input_tokens=_optional_int(value.get("input_tokens")),
        cached_input_tokens=_optional_int(
            value.get("cache_read_input_tokens")
            or value.get("cached_input_tokens")
        ),
        output_tokens=_optional_int(value.get("output_tokens")),
        cost_usd=float(total_cost) if isinstance(total_cost, (int, float)) else None,
    )


def _claude_provider_billing(
    events: tuple[HarnessEvent, ...],
) -> ProviderBillingEvidence | None:
    api_key_source: str | None = None
    result_usage: Usage | None = None
    total_cost_usd: float | None = None
    source_events: list[str] = []

    for event in events:
        if (
            event.raw.get("type") == "system"
            and event.raw.get("subtype") == "init"
        ):
            api_key_source = _claude_init_api_key_source(event.raw)
            if api_key_source is not None:
                source_events.append("system/init")
            break

    for event in reversed(events):
        if event.raw.get("type") != "result":
            continue
        result_usage = event.usage
        total_cost_usd = _optional_float(event.raw.get("total_cost_usd"))
        source_events.append("result")
        break

    if api_key_source is None and result_usage is None and total_cost_usd is None:
        return None
    return ProviderBillingEvidence(
        provider="claude",
        api_key_source=api_key_source,
        total_cost_usd=total_cost_usd,
        input_tokens=result_usage.input_tokens if result_usage else None,
        cached_input_tokens=(
            result_usage.cached_input_tokens if result_usage else None
        ),
        output_tokens=result_usage.output_tokens if result_usage else None,
        source_events=tuple(source_events),
    )


def _claude_init_api_key_source(raw: Mapping[str, Any]) -> str | None:
    value = raw.get("apiKeySource")
    return value if isinstance(value, str) and value else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _find_upward(cwd: Path, filename: str) -> Path | None:
    current = cwd.resolve()
    while True:
        candidate = current / filename
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent


def prepare_claude_launch(binding: Any, *, cwd: Path) -> None:
    """Seed a claude runtime before its interactive launch (T-5011 PR-A).

    Moved verbatim out of ``runtime_controller._start_runtime`` (T-4924 plan
    §2.4). T-4935: seats launch --permission-mode bypassPermissions; its
    one-time warning dialog ("No, exit" preselected) is skipped only by
    USER-scope settings (not the controller's --settings file), so seed it.
    Fail-open, but say so: a False return means the seat may stall on the
    dialog (T-4953).
    """
    del cwd
    consent_dir = binding.account_home / ".claude"
    if not ensure_bypass_consent(consent_dir):
        print(
            f"[runtime_controller] bypass consent NOT ensured in {consent_dir}/settings.json; "
            "the seat may stall on the bypass-permissions warning dialog",
            file=sys.stderr,
        )
