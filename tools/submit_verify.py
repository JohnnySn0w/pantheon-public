"""tools/submit_verify.py — harness-aware tmux submit-verify (T-1048).

Generalizes the tmux send-keys "did my injected text actually submit?" check
across harness types (claude-code, codex, and the next sibling agent) via
per-harness *profiles*. The verify ALGORITHM is shared; only the
"is my injected text still pending (not yet submitted)?" predicate is
profile-driven.

Background — the bug this removes (T-1046 root cause)
----------------------------------------------------
Both consumers historically keyed submit-verification on the Claude-Code-only
paste sentinel ``[Pasted text``:

  * pantheon-config:tools/send_keys_submit.sh  (Recovery/Orchestrator/Specialist panes)
  * codex-config:codex_telegram/poller.py:inject()  (codex's pane)

On codex's TUI that marker NEVER appears, so the absence-check always passed on
the first Enter regardless of whether the text actually submitted -> the text
hung unsubmitted while the verify lied "confirmed" (T-1046). The operator's direction
(Telegram 3031): "a lot of our sentinel scripts will need to be generalized and
then have configs for different harness types." One shared submit-verify, N
harness configs.

Design shape (operator-aligned 2026-06-10, Telegram 3063 "leans are good")
------------------------------------------------------------------------
  * Q1 (where config lives): per-consumer DATA + one shared ALGORITHM module.
        pantheon-config owns this verifier + the claude-code profile + the
        canonical codex profile; codex-config converges its poller onto the
        same algorithm (separate supervised deploy — its bwrap sandbox + repo).
  * Q2 (bash<->python seam): ONE predicate home (this file). "Bash keeps the
        orchestration, python owns the predicate" — send_keys_submit.sh keeps the
        tmux send-keys/capture-pane calls in bash (they must stay DIRECT children
        so handoff_and_restart.sh's wall-clock watchdog can reap a hung send-keys
        via pkill -P) and shells out to ``--pending-check`` for the per-harness
        "is it still pending?" decision only. Python-native callers (the codex
        poller) can instead use the full ``submit_verify()`` orchestration here.
  * Q3 (harness selection): EXPLICIT ``--harness`` flag. No pane-sniffing
        (sniff drift bites exactly when a TUI updates).

Side effects (send_text / send_enter / capture / sleep) are injected into
``submit_verify()`` so the orchestration is fully unit-testable, mirroring
tools/kick_verify.py. The module is stdlib-only by design: it sits on the
restart/kick critical path (send_keys_submit.sh -> handoff_and_restart.sh), so
it must never fail to import.

Adding a new harness = add a profile row + (only if a novel pending-strategy)
one predicate branch in ``submit_pending``. No new fork.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
# Profiles — what varies by harness.                                          #
# --------------------------------------------------------------------------- #
#
# Each profile fully describes the TUI-specific part of submit-verification:
#   pending_strategy : which predicate branch decides "still pending?"
#   settle_delay_s   : paste-mode settle wait before each Enter
#   submit_retries   : extra Enter attempts if submit stays unconfirmed
#   + strategy-specific fields (paste_sentinel/paste_sentinel_scope |
#     composer_glyph/needle_len)
#
# The data lives here (pantheon-config); codex-config converges its poller onto
# this same algorithm at its supervised deploy. Keeping the canonical codex
# profile here too means "one place to read what every harness needs" without a
# cross-repo data-sync seam on the load-bearing logic.

DEFAULT_SETTLE_DELAY_S = 0.7
DEFAULT_SUBMIT_RETRIES = 2
#: T-5412: wait before re-reading an empty post-Enter capture (one blank read can be
#: a redraw in flight; only two in a row count as empty).
EMPTY_RECAPTURE_S = 0.4
#: T-5011 PR-B: Hermes's paste-mode settle (H0: ~1s measured, 1.2s with margin).
#: send_keys_submit.sh's HERMES_ENTER_DELAY and inbox_consumer's hermes delay
#: carry the same value (tests pin both).
HERMES_SETTLE_DELAY_S = 1.2

PROFILES: dict[str, dict] = {
    # Claude Code TUI: large pastes collapse to the "[Pasted text" marker, but
    # short/uncollapsed pastes remain as literal text on the U+276F composer
    # line. The first Enter on a fresh TUI can be eaten before input readiness;
    # sentinel-only verification then false-passes because no marker exists and
    # suppresses the retry that would converge. Mirror the T-2408 codex shape:
    # pending == scoped paste sentinel OR the live composer still holds the
    # beginning of the injected text.
    "claude-code": {
        "pending_strategy": "composer_holds_text",
        "composer_glyph": "❯",  # U+276F
        "needle_len": 48,
        "paste_sentinel": "[Pasted text",
        "paste_sentinel_scope": "composer_box",  # T-4293 (was whole_pane)
        "repress_when_stuck": True,  # T-4141: should_repress() may fire here
        "settle_delay_s": DEFAULT_SETTLE_DELAY_S,
        "submit_retries": DEFAULT_SUBMIT_RETRIES,
    },
    # codex TUI: no paste sentinel. A not-yet-submitted line sits in the composer
    # (the line bearing the U+203A "›" glyph) still holding our injected text;
    # after a real submit the composer reverts to its placeholder, which lacks
    # our framing. Pending == the composer still holds the injected text.
    # (Canonical spec; codex-config's poller reconciles its deployed predicate
    #  onto this at convergence time.)
    "codex": {
        "pending_strategy": "composer_holds_text",
        "composer_glyph": "›",  # ›
        "needle_len": 48,
        # T-2408: codex DOES have a paste sentinel after all — for LARGE pastes
        # the TUI collapses the composer into "[Pasted Content N chars]" + a
        # reflowed tail, which hides exactly the needle the composer check
        # searches for (the BEGINNING of the injected text). The old "codex TUI:
        # no paste sentinel" note was true only of the short injections T-1046
        # was written against; wake prompts exceed the collapse threshold by
        # construction (T-2266 subject lists), so EVERY wake kick false-greened.
        # MEASURED 2026-08-02: two collapsed pastes sat at an idle codex prompt
        # for hours while submit-verify had reported both submitted — a 4.5h
        # comms gap ended by one manual Enter. The two checks OR together:
        # needle for short injections, sentinel for collapsed ones.
        "paste_sentinel": "[Pasted Content",
        "paste_sentinel_scope": "composer_region",
        "settle_delay_s": DEFAULT_SETTLE_DELAY_S,
        "submit_retries": DEFAULT_SUBMIT_RETRIES,
    },
    # Hermes Agent classic CLI (T-5011 PR-B; T-4924 H0 captures, Hermes v0.22.0).
    # The idle input line is "❯ <placeholder>" between two "────" rules, the same
    # composer shape as claude-code. A paste of 5+ lines or 2000+ chars collapses
    # into "[Pasted text #N: L lines → <file>]" in the input buffer
    # (hermes_cli/cli_tui_mixin.py _tui_collapse_paste), so the claude-code
    # sentinel and composer_box scope apply. H0: a send-keys text plus an
    # IMMEDIATE Enter is swallowed into the paste as a newline; text, a ~1s pause,
    # then Enter submits, so the settle is longer (1.2s, artisan ruling 7).
    # Known gap: while a turn runs the input line reads "☤ ❯ msg=interrupt ...",
    # which does not START with the glyph, so a busy-pane injection reads
    # "submitted". The wake path only injects into an idle pane
    # (hermes_pane_idle_state), and busy_input_mode is queue.
    "hermes": {
        "pending_strategy": "composer_holds_text",
        "composer_glyph": "❯",  # U+276F
        "needle_len": 48,
        "paste_sentinel": "[Pasted text",
        "paste_sentinel_scope": "composer_box",
        "settle_delay_s": HERMES_SETTLE_DELAY_S,
        "submit_retries": DEFAULT_SUBMIT_RETRIES,
    },
}


def get_profile(harness: str) -> dict:
    """Return the profile dict for ``harness`` or raise KeyError-style ValueError."""
    try:
        return PROFILES[harness]
    except KeyError:
        raise ValueError(
            f"unknown harness {harness!r}; known: {sorted(PROFILES)}"
        ) from None


# --------------------------------------------------------------------------- #
# Pending predicate — the ONLY profile-driven part (pure, unit-testable).     #
# --------------------------------------------------------------------------- #

def _normalize_ws(s: str) -> str:
    """Collapse all whitespace runs to single spaces — the TUI may reflow/pad
    injected text, so matching is done on whitespace-normalized strings."""
    return " ".join(s.split())


# The composer's bottom border: a horizontal rule or a box corner (grouped `─`,
# see the byte-locale note on _READY_FRAME_PATTERN below).
_COMPOSER_BORDER = re.compile(r"(─){20,}|╰")


def _composer_region(lines: list[str], glyph: str) -> list[str] | None:
    """The live composer: the LAST glyph-led line down to the composer's bottom
    border (or the end of the pane if there is none). None when no line begins with
    the glyph, i.e. no composer is visible."""
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].lstrip().startswith(glyph):
            end = next((j for j in range(i + 1, len(lines))
                        if _COMPOSER_BORDER.search(lines[j])), len(lines))
            return lines[i:end]
    return None


#: codex's composer glyph (U+203A). The duplicate-render rule below is codex-only.
CODEX_COMPOSER_GLYPH = "›"
#: T-4987: codex's status footer ("model[ effort] · cwd"). A stdlib copy of
#: health_export._CODEX_FOOTER_RE; a test pins the two equal.
_CODEX_FOOTER_RE = re.compile(r"^\s*[\w.\-]+(?:\s+\w+)?\s+\u00b7\s+\S")
#: T-4987: a below-footer block shorter than this (whitespace-stripped) is no evidence.
DUPLICATE_MIN_CHARS = 16


def codex_duplicate_render(lines: list[str]) -> tuple[int, int, list[str]] | None:
    """T-4987: a codex composer drawn twice, the second copy BELOW the status footer.

    Watchman, 2026-10-22: the wake notice sat unsubmitted while the composer was
    rendered once above the footer and again below it, the copy scrolled by one row
    ('› (from inbox_consumer). ...') and cut off at the pane bottom. A sane codex
    screen always ends with its footer, so glyph lines after the LAST footer, with no
    footer of their own, are this duplicate. _composer_region starts at the last glyph
    line, so it read the cut-off copy, which holds neither needle.

    Returns (k, f, fragments): k is the glyph line of the composer above the footer, f
    the footer line, fragments the whitespace-stripped text of each glyph-led block
    below the footer (glyph dropped). None when the pane is not that shape, including
    when anything other than glyph-led blocks sits below the footer. Callers decide
    what agrees: this only locates the two renders."""
    glyph = CODEX_COMPOSER_GLYPH
    glyph_idx = [j for j, ln in enumerate(lines) if ln.lstrip().startswith(glyph)]
    footer_idx = [j for j, ln in enumerate(lines) if _CODEX_FOOTER_RE.match(ln)]
    if not glyph_idx or not footer_idx or footer_idx[-1] > glyph_idx[-1]:
        return None
    f = footer_idx[-1]
    above = [j for j in glyph_idx if j < f]
    if not above:
        return None
    blocks: list[str] = []
    for ln in lines[f + 1:]:
        s = ln.strip()
        if not s:
            continue
        if s.startswith(glyph):
            blocks.append(s[len(glyph):])
        elif blocks:
            blocks[-1] += s
        else:
            return None
    return above[-1], f, ["".join(b.split()) for b in blocks]


def duplicate_agrees(fragments: list[str], compact_text: str) -> bool:
    """T-4987: every below-footer block is a real piece of ``compact_text``
    (whitespace-stripped): at least DUPLICATE_MIN_CHARS long and a substring of it. A
    cut-off copy passes because truncation only shortens it."""
    return bool(fragments) and all(
        len(fr) >= DUPLICATE_MIN_CHARS and fr in compact_text for fr in fragments)


def _composer_holds_text(pane_text: str, injected_text: str, glyph: str,
                         needle_len: int) -> bool:
    """codex strategy: True iff the composer still holds our injected text.

    codex's live input line is the LAST glyph-bearing line; after a real submit
    it reverts to codex's placeholder (no injected framing). We check the first
    ``needle_len`` chars of the injected text (whitespace-normalized, robust to
    the TUI reflowing/padding the line) against ONLY that last composer line.

    Scoping to the LAST glyph line — not every glyph-bearing line — is load-
    bearing (T-1054 reconciliation against codex's deployed predicate). Once a
    message submits it scrolls UP into history, where it can still sit on a
    glyph-prefixed line ABOVE the live placeholder. Joining all glyph lines would
    keep matching that just-submitted text and falsely report "still pending"
    (→ spurious Enter retries + a false "unconfirmed"). The live composer is
    always the bottom-most glyph line, so take the last one. Match `lstrip()`-
    anchored (the composer line BEGINS with the glyph; a glyph mid-prose in
    output is not the composer).
    """
    # T-4141: the match runs over the whole COMPOSER REGION, whitespace-STRIPPED,
    # not just the glyph line. The region runs from the last glyph line to the
    # composer's bottom border (or the end of the pane if there is none). When the
    # composer already held unsubmitted text, an appended injection lands on wrapped
    # continuation lines that carry no glyph, sometimes split mid-word. Checking the
    # glyph line alone false-passed that (artisan, 2026-09-28). History above stays out
    # (the region starts at the LAST glyph line), and so does anything below the
    # border: a tty-echoing stand-in leaves submitted text there.
    compact = "".join(_normalize_ws(injected_text).split())
    needle = "".join(_normalize_ws(injected_text)[:needle_len].split())
    # T-5380: a long single-line paste scrolls its HEAD out of the composer's visible
    # rows (about 7-8; the cursor, hence the tail, stays in view). Recovery's T-5148 and
    # T-5221 deliveries sat unsubmitted for 2h+ and 3.9h because the head needle alone
    # read "not pending" after a swallowed Enter. The TAIL needle covers that: it is
    # matched in the same composer region only, so a submitted prompt (now in history
    # above the region, or below the border) still reads not-pending.
    tail_needle = compact[-len("".join(needle.split())):] if needle else ""
    if not needle:
        # Nothing distinctive to match (empty/whitespace injection) — can't
        # assert it's still pending, so treat as not-pending (don't loop forever).
        return False
    lines = pane_text.splitlines()
    region = _composer_region(lines, glyph)
    if region is None:
        return False  # no composer line visible -> not pending (submitted/cleared)
    held = "".join("".join(region).split())
    if needle in held or tail_needle in held:
        return True
    # T-4987: the composer drawn twice, a cut-off copy below the footer (watchman
    # 2026-10-22, read "submitted" at inject time). Pending only when the two renders
    # agree: the copy is pieces of OUR text AND the composer above the footer holds a
    # needle. A copy alone, or one with other text, keeps the reading above, so text
    # in history and a placeholder composer still read not-pending (T-1054).
    if glyph == CODEX_COMPOSER_GLYPH:
        dup = codex_duplicate_render(lines)
        if dup is not None and duplicate_agrees(dup[2], compact):
            above = "".join("".join(lines[dup[0]:dup[1]]).split())
            return needle in above or tail_needle in above
    return False


def submit_pending(profile: dict, pane_text: str, injected_text: str) -> bool:
    """Return True iff the injected text appears NOT yet submitted, per profile.

    Dispatches on ``profile['pending_strategy']``. Pure function (no I/O) — the
    single home of every harness's "still pending?" logic.
    """
    strategy = profile.get("pending_strategy")
    if strategy == "paste_sentinel":
        return profile["paste_sentinel"] in pane_text
    if strategy == "composer_holds_text":
        # T-2408: a composer strategy may ALSO carry a paste sentinel, covering
        # exactly the case where the needle check goes blind — a collapsed
        # "[Pasted Content N chars]" block renders everything EXCEPT the
        # beginning of the injected text, and in the measured pane it also
        # REPLACES the '›' composer line entirely, so the needle loop fell
        # through to "no composer visible -> not pending". The two checks cover
        # each other's gap: sentinel for collapsed pastes, needle for short
        # injections (which never collapse and show no sentinel).
        #
        # SCOPED BELOW THE LAST COMPOSER LINE, not whole-pane, for the same
        # reason the needle check takes only the LAST glyph line (T-1054):
        # submitted content scrolls UP into history, and if the marker ever
        # lingers there a whole-pane scan would read "still pending" and fire
        # spurious Enters — into a composer whose placeholder suggestion an
        # Enter might RUN. Observed post-submit the marker vanishes, but one
        # observation is provenance, not a guarantee. A pending collapsed paste
        # sits AT the composer (measured: with no glyph line at all), so:
        # sentinel counts iff it appears after the last glyph line, or anywhere
        # when no glyph line exists.
        sentinel = profile.get("paste_sentinel")
        if sentinel:
            sentinel_scope = profile.get(
                "paste_sentinel_scope", "composer_region")
            if sentinel_scope == "whole_pane":
                # No in-repo profile uses this since T-4293 (claude-code moved to
                # composer_box). Kept for a profile defined out of repo.
                if sentinel in pane_text:
                    return True
            elif sentinel_scope == "composer_box":
                # T-4293 (claude-code; whole_pane before). Claude's placeholder
                # "[Pasted text #N +M lines]" is text IN the prompt input: the
                # bundle builds it into the input value and parses it back out
                # (Q9/Uc in 2.1.284), and artisan's pane stream draws it into the
                # composer with its "paste again to expand" hint. The fixtures
                # that drew it ABOVE the "❯" line were synthetic. Whole-pane read
                # any OLD marker left in scrollback as our paste still pending:
                # a false rc 2/8, the wake marker not advanced, a duplicate kick
                # and an OnFailure page. So the scope is the live composer only
                # (the region _composer_holds_text reads), whitespace-stripped
                # because the marker can wrap. With no composer visible (a modal,
                # the old boxed frame), fall back to the whole pane: fail closed,
                # as before.
                lines = pane_text.splitlines()
                region = _composer_region(lines, profile["composer_glyph"])
                haystack = "\n".join(lines if region is None else region)
                if "".join(sentinel.split()) in "".join(haystack.split()):
                    return True
            elif sentinel_scope == "composer_region":
                lines = pane_text.splitlines()
                last_glyph = -1
                for i, line in enumerate(lines):
                    if line.lstrip().startswith(profile["composer_glyph"]):
                        last_glyph = i
                # Region INCLUDES the last glyph line (T-2408 second specimen,
                # measured ~1h after the first fix landed): a pending collapsed
                # paste can render ON the composer line itself —
                #   "› [Pasted Content 1020 chars], the hub must be…"
                # A submitted marker sits above the live composer, so this
                # retains Codex's scrollback-history safety.
                region = lines[last_glyph:] if last_glyph >= 0 else lines
                if any(sentinel in line for line in region):
                    return True
            else:
                raise ValueError(
                    f"unknown paste_sentinel_scope {sentinel_scope!r}")
        return _composer_holds_text(
            pane_text, injected_text,
            profile["composer_glyph"], profile["needle_len"],
        )
    raise ValueError(f"unknown pending_strategy {strategy!r}")


def _inbox_consumer():
    """inbox_consumer owns the pane sentinels (T-0716). Import it lazily, from this
    file's own dir, so a script run and a package import resolve the same module."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import inbox_consumer  # noqa: PLC0415 (lazy by design)
    return inbox_consumer


def should_repress(profile: dict, pane_text: str, injected_text: str) -> bool:
    """T-4141: True iff ONE more Enter would submit text WE injected.

    Four conditions. OUR text is still visibly in the composer (the needle match,
    NOT the bare paste sentinel: a stale marker plus an Enter on an empty composer
    could run a ghost suggestion). No modal footer (an Enter would pick an option).
    Not generating. No background agent row either: a generating footer can omit
    "esc to interrupt" (T-4193), and the rows are this check's remaining busy
    evidence. That over-holds a turn-ended pane with background agents, where input
    does submit (T-4284), which is the safe direction. pane_idle_state is
    deliberately NOT used: with text in the composer it
    reads busy under the manage footer, which is exactly the stuck state this
    exists to clear (artisan, 2026-09-28: one Enter on the idle pane submitted the
    stuck text at once). Profiles opt in (claude-code only; codex's busy signals are
    not validated here). Fail-safe: anything unclassifiable returns False."""
    if not profile.get("repress_when_stuck"):
        return False
    if not _composer_holds_text(pane_text, injected_text,
                                profile["composer_glyph"], profile["needle_len"]):
        return False
    try:
        ic = _inbox_consumer()
        non_blank = [ln for ln in pane_text.splitlines() if ln.strip()]
        bottom = "\n".join(non_blank[-ic._WEDGE_BOTTOM_REGION_LINES:])
        if any(f in bottom for f in ic._WEDGE_DIALOG_FOOTERS):
            return False
        if ic._GENERATING_SENTINEL in bottom:
            return False
        if ic._BACKGROUND_AGENT_ROW_RE.search(bottom) or ic._WAITING_FOR_AGENTS_RE.search(bottom):
            return False
    except Exception:
        return False
    return True


# --------------------------------------------------------------------------- #
# Runtime input readiness predicate (T-2382).                                 #
# --------------------------------------------------------------------------- #
#
# A freshly-started TUI can sit alive-but-not-yet-interactive: the process is
# up and the tmux pane exists, but the harness hasn't finished drawing its
# composer, so an unattended first-fire kick lands on dead pixels and is
# silently swallowed instead of submitting. ``ready_for_submission`` answers
# "would a submit into this pane right now actually be accepted?" — distinct
# from ``submit_pending`` above, which answers "did the text I ALREADY
# injected go through?". runtime_controller.py polls this BEFORE publishing a
# fresh or replaced runtime as ready, mirroring the proven idle-confirm
# conjunction in recovery_boot_kick.sh (lines 48-56, 88-107): prompt-frame +
# idle/composer glyph + not-a-modal, generalized to accept either harness's
# composer glyph so one predicate covers claude and codex.

# The `─` quantifiers MUST stay grouped exactly like this: `─` is 3 UTF-8
# bytes, and in a byte locale a bare `─+`/`─{20,}` binds the quantifier to
# the LAST byte only and silently never matches — the documented CI-red trap
# from recovery_boot_kick.sh:48-51. Grouping is identical under UTF-8 here.
_READY_FRAME_PATTERN = re.compile(r"(╭(─)+╮|(─){20,})")
# T-4646: Claude Code 2.1.289's startup nudge "Make auto mode your default permission
# mode?" has NO confirm footer, and its option cursor ❯ passes the composer-glyph check.
# Its first option line is the positive witness. On 2026-10-05 a restart kick's Enter
# accepted it and switched specialist to auto mode. Shell copies of this marker live in the
# DIALOG_EXCLUDE of handoff_and_restart.sh, verify_startup.sh and recovery_boot_kick.sh.
AUTO_MODE_NUDGE_MARKER = "Yes, set auto mode as my default permission mode"
_READY_DIALOG_PATTERN = re.compile(r"Enter to confirm|Esc to cancel|" + re.escape(AUTO_MODE_NUDGE_MARKER))
_READY_COMPOSER_GLYPHS = ("❯", "›")  # claude U+276F, codex U+203A
# T-4646: the footer row a Claude Code composer always draws (the permission-mode
# indicator or a focus hint) or the generating line. After a full render wait, its
# ABSENCE from the bottom region means the pane is not at a composer (an option list,
# a prompt, a boot screen), and an Enter there picks whatever is highlighted.
_CLAUDE_COMPOSER_FOOTER_RE = re.compile(
    r"(?:⏸|⏵⏵?)\s[^·\n]*\bon\b|\? for shortcuts|shift\+tab to cycle|esc to interrupt"
    r"|ctrl\+t to (?:show|hide) tasks|← for agents|↓ to manage")
_ENTER_ANYWAY_BOTTOM_LINES = 8


# T-5011 PR-B: Hermes classic-CLI pane markers (H0, tui/classic2 captures). The
# canonical constants are tools/harness_adapter/hermes.py PANE_*; this stdlib copy
# serves the bare-path importers (mailbox_wake), and a test pins the two equal.
HERMES_PANE_IDLE_PROMPT = "❯ "
HERMES_PANE_BUSY_HINT = "msg=interrupt · /queue · /bg · /steer"
HERMES_PANE_BUSY_STATUS_GLYPH = "⏱"
_HERMES_BOTTOM_LINES = 8


def hermes_pane_idle_state(pane_text: str) -> bool | None:
    """Hermes classic-CLI idle predicate (T-5011 PR-B): True idle, False busy,
    None unclassifiable.

    Busy: the running-turn prompt hint, or the turn timer glyph, in the bottom
    region. Idle: an input line that starts with the idle prompt and neither busy
    marker. Anything else (an empty capture, a modal, a screen without the input
    line) is None, which a waker must treat as "do not inject".
    """
    if not pane_text or not pane_text.strip():
        return None
    bottom = [ln for ln in pane_text.splitlines() if ln.strip()][-_HERMES_BOTTOM_LINES:]
    joined = "\n".join(bottom)
    if HERMES_PANE_BUSY_HINT in joined or HERMES_PANE_BUSY_STATUS_GLYPH in joined:
        return False
    if any(ln.lstrip().startswith(HERMES_PANE_IDLE_PROMPT) for ln in bottom):
        return True
    return None


def paste_refusal(pane_text: str) -> str | None:
    """T-4646: why a render-gated caller must not even PASTE into ``pane_text``, or
    None. Positive dialog evidence only, because a fresh TUI may not have drawn its
    composer footer yet. An empty capture is no evidence."""
    if pane_text and _READY_DIALOG_PATTERN.search(pane_text):
        return "dialog"
    return None


def enter_anyway_refusal(pane_text: str, harness: str) -> str | None:
    """T-4646: why send_keys_submit must NOT press its fallback Enter, or None.

    Asked only after the render wait expired without seeing our text in a composer,
    when the old behaviour was 'press Enter anyway'. "dialog": a known dialog (incl.
    the auto-mode nudge). "no_composer_footer" (claude-code only): no composer footer
    in the bottom region, so this is not a composer. An empty capture is no evidence,
    so it keeps the old behaviour."""
    if not pane_text or not pane_text.strip():
        return None
    if _READY_DIALOG_PATTERN.search(pane_text):
        return "dialog"
    if harness == "claude-code":
        non_blank = [ln for ln in pane_text.splitlines() if ln.strip()]
        if not _CLAUDE_COMPOSER_FOOTER_RE.search("\n".join(non_blank[-_ENTER_ANYWAY_BOTTOM_LINES:])):
            return "no_composer_footer"
    return None


# T-4832: Claude Code's plan-approval dialog ("Yes, … and auto-accept edits" / "No,
# keep planning" / "Tell Claude what to change"); an Enter there approves the plan.
# Its option label is matched in the BOTTOM region only, where the focused dialog
# sits: the same words in transcript prose above must not block a submit.
# Anchored to an option row ("3. No, keep planning", cursor optional), so an agent
# printing the phrase (cat-ing a test file) does not block its own kicks (T-4832 r1).
_PLAN_DIALOG_PATTERN = re.compile(r"^\s*(?:❯\s*)?\d\.\s+No, keep planning", re.M)
_ENTER_CHECK_BOTTOM_LINES = 12  # the handoff_and_restart.sh step-3 guard's region


def enter_refusal(pane_text: str) -> str | None:
    """T-4832: why send_keys_submit must not press Enter NOW, or None.

    Asked right before every Enter, after the paste: a dialog that opened after the
    pre-paste checks would be answered by the Enter (the step-3 guard's TOCTOU).
    Looks at the bottom non-blank lines only. An empty capture is no evidence."""
    if not pane_text or not pane_text.strip():
        return None
    bottom = "\n".join([ln for ln in pane_text.splitlines() if ln.strip()][-_ENTER_CHECK_BOTTOM_LINES:])
    if _READY_DIALOG_PATTERN.search(bottom) or _PLAN_DIALOG_PATTERN.search(bottom):
        return "dialog"
    return None


def ready_for_submission(pane_text: str) -> bool:
    """True iff a submit into ``pane_text`` right now would actually land.

    Full conjunction — every condition must hold:
      1. ``pane_text`` is non-empty.
      2. A prompt-frame edge is visible (the TUI has finished drawing its box).
      3. A composer/idle glyph is visible — claude's ``❯`` OR codex's ``›``.
      4. No focused confirm modal is blocking input ("Enter to confirm" /
         "Esc to cancel" absent).
    """
    return not_ready_reason(pane_text) is None


def not_ready_reason(pane_text: str) -> str | None:
    """Which ``ready_for_submission`` condition ``pane_text`` fails, or None.

    One of "empty", "no_frame", "no_composer_glyph", "dialog_footer", checked
    in that order. A derived label, never pane text, so evidence can carry it
    (T-4362: attempt 2's probe wait could not say why the pane was never ready).
    """
    if not pane_text:
        return "empty"
    if not _READY_FRAME_PATTERN.search(pane_text):
        return "no_frame"
    if not any(glyph in pane_text for glyph in _READY_COMPOSER_GLYPHS):
        return "no_composer_glyph"
    if _READY_DIALOG_PATTERN.search(pane_text):
        return "dialog_footer"
    return None


def busy_reason(pane_text: str) -> str | None:
    """Why a submit into ``pane_text`` would land in a RUNNING turn, or None (T-4459).

    ``not_ready_reason`` can't see this: the current TUI draws its frame and ``❯``
    composer mid-turn too, so it passes a busy pane (T-3026 attempt 4: recovery's boot kick
    fired into a still-running probe turn and sat unsent). The evidence is the same
    bottom region ``should_repress`` reads, with inbox_consumer's sentinels (T-0716):
    "dialog_footer", "generating" ("esc to interrupt") or "background_agents" (a
    generating footer can omit "esc to interrupt", T-4193). That over-holds a
    turn-ended pane that still has background agents (T-4284), which is the safe
    direction. Fail-safe: sentinels that can't be loaded read as "unclassifiable",
    never idle.
    """
    try:
        ic = _inbox_consumer()
        non_blank = [ln for ln in pane_text.splitlines() if ln.strip()]
        bottom = "\n".join(non_blank[-ic._WEDGE_BOTTOM_REGION_LINES:])
        if any(f in bottom for f in ic._WEDGE_DIALOG_FOOTERS):
            return "dialog_footer"
        if ic._GENERATING_SENTINEL in bottom:
            return "generating"
        if ic._BACKGROUND_AGENT_ROW_RE.search(bottom) or ic._WAITING_FOR_AGENTS_RE.search(bottom):
            return "background_agents"
    except Exception:  # noqa: BLE001 - unclassifiable is busy, never idle
        return "unclassifiable"
    return None


# --------------------------------------------------------------------------- #
# Verify orchestration — shared across harnesses, side-effects injected.      #
# --------------------------------------------------------------------------- #

class SubmitResult:
    def __init__(self, ok: bool, attempts: int, detail: str = ""):
        self.ok = ok
        self.attempts = attempts
        self.detail = detail

    def __repr__(self) -> str:
        return (f"SubmitResult(ok={self.ok}, attempts={self.attempts}, "
                f"detail={self.detail!r})")


def submit_verify(profile: dict, pane: str, text: str, *,
                  send_text, send_enter, capture, sleep=time.sleep) -> SubmitResult:
    """Inject ``text`` into ``pane`` and verify the TUI submitted it.

    send_text(pane, text) -> bool   (literal send-keys of the text; False = failed)
    send_enter(pane)      -> bool   (a SEPARATE Enter keystroke; False = failed)
    capture(pane)         -> str    (pane capture for the pending check)

    Algorithm (identical across harnesses; T-0519 discipline):
      send text once -> for each attempt: settle, (retries only: capture and stop if
      already not-pending) send a separate Enter, capture, check
      ``submit_pending(profile, ...)``; stop on first not-pending (submitted). An
      empty post-Enter capture (two reads in a row) is unknown (T-5412): never a
      stop, ends ok=False.
      Retry the Enter up to ``submit_retries`` times, else report unconfirmed.
    Only ``submit_pending`` (the predicate) differs by harness.
    """
    settle = float(profile.get("settle_delay_s", DEFAULT_SETTLE_DELAY_S))
    retries = int(profile.get("submit_retries", DEFAULT_SUBMIT_RETRIES))

    if not send_text(pane, text):
        return SubmitResult(False, 0, "send-keys (text) failed")

    last_empty = False
    for attempt in range(retries + 1):
        if settle:
            sleep(settle)
        # T-5380 round 2: re-verify before every RETRY Enter. The previous Enter may
        # have landed late, inside the settle window; a retry then would be an extra
        # Enter into a live seat. The first Enter stays unconditional.
        # An empty or whitespace-only capture (a failed capture reads "") has no
        # composer region and would read "submitted": it casts no vote (round 3).
        if attempt > 0:
            seen = capture(pane)
            if seen.strip() and not submit_pending(profile, seen, text):
                return SubmitResult(True, attempt, "submit confirmed before retry Enter")
        if not send_enter(pane):
            return SubmitResult(False, attempt + 1, "send-keys (Enter) failed")
        seen = capture(pane)
        if not seen.strip():
            sleep(EMPTY_RECAPTURE_S)
            seen = capture(pane)
        # T-5412: an empty or whitespace-only capture right after the Enter is
        # UNKNOWN, not "submitted": it has no composer region, so the predicate
        # would read it as cleared. A real pane is never empty (a failed capture
        # reads ""). Fall through to the next iteration's pre-retry re-check.
        last_empty = not seen.strip()
        if last_empty:
            continue
        if not submit_pending(profile, seen, text):
            return SubmitResult(True, attempt + 1, "submit confirmed")
        # still pending — paste-mode/composer hasn't cleared; retry the Enter
    if last_empty:
        return SubmitResult(False, retries + 1,
                            f"unverified: empty capture after {retries} Enter retries")
    return SubmitResult(False, retries + 1,
                        f"submit unconfirmed after {retries} Enter retries")


# --------------------------------------------------------------------------- #
# Real tmux side-effects (used by the CLI; mocked out in unit tests).         #
# --------------------------------------------------------------------------- #

_TMUX_TIMEOUT = 10


def tmux_send_text(pane: str, text: str) -> bool:
    """send-keys the text literally. ``-l`` = literal (no key-name parsing).

    Wire-identical to the long-standing send_keys_submit.sh (`send-keys -t P -l TEXT`)
    so this refactor changes no observable behavior on the live claude-code restart
    path. (The codex poller historically added a ``--`` flag-terminator for
    leading-dash safety; if codex needs that when it converges onto this verifier,
    add it as a per-profile send option rather than changing the claude-code wire
    format here.)"""
    try:
        r = subprocess.run(
            ["tmux", "send-keys", "-t", pane, "-l", text],
            capture_output=True, text=True, timeout=_TMUX_TIMEOUT,
        )
        if r.returncode != 0:
            sys.stderr.write(
                f"[submit_verify] ERROR: tmux send-keys (text) failed rc={r.returncode} "
                f"for pane {pane}\n")
            return False
        return True
    except Exception as e:  # noqa: BLE001 — never crash the kick path
        sys.stderr.write(
            f"[submit_verify] ERROR: tmux send-keys (text) raised {type(e).__name__}\n")
        return False


def tmux_send_enter(pane: str) -> bool:
    try:
        r = subprocess.run(
            ["tmux", "send-keys", "-t", pane, "Enter"],
            capture_output=True, text=True, timeout=_TMUX_TIMEOUT,
        )
        if r.returncode != 0:
            sys.stderr.write(
                f"[submit_verify] ERROR: tmux send-keys (Enter) failed rc={r.returncode} "
                f"for pane {pane}\n")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(
            f"[submit_verify] ERROR: tmux send-keys (Enter) raised {type(e).__name__}\n")
        return False


def tmux_capture(pane: str) -> str:
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane],
            capture_output=True, text=True, timeout=_TMUX_TIMEOUT,
        )
        return r.stdout
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# CLI — send_keys_submit.sh (and any caller) shells out to this.              #
# --------------------------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Harness-aware tmux submit-verify (T-1048).")
    p.add_argument("--harness", required=True, choices=sorted(PROFILES),
                   help="harness profile to use (explicit; no sniffing)")
    p.add_argument(
        "--pending-check", action="store_true",
        help="PREDICATE-ONLY mode: read the captured pane text from stdin and "
             "print 'pending' (text not yet submitted) or 'submitted'. Does NO "
             "tmux I/O — the caller (e.g. send_keys_submit.sh) keeps the tmux "
             "send/capture as direct children so the restart-stack wall-clock "
             "watchdog can still reap a hung send-keys (pkill -P is parent-based).")
    p.add_argument(
        "--repress-check", action="store_true",
        help="T-4141 PREDICATE-ONLY mode: read the captured pane text from stdin and "
             "print 'repress' (one more Enter would submit OUR text: it is still in the "
             "composer and the pane is not generating, running a background agent, or "
             "showing a modal) or 'hold'. Does NO tmux I/O.")
    p.add_argument(
        "--ready-check", action="store_true",
        help="T-4459 PREDICATE-ONLY mode: read the captured pane text from stdin and "
             "print 'ready' (a fresh submit would start a turn now), 'not_ready:<reason>' "
             "(not_ready_reason) or 'busy:<reason>' (busy_reason). Does NO tmux I/O.")
    p.add_argument(
        "--paste-check", action="store_true",
        help="T-4646 PREDICATE-ONLY mode: pane text on stdin; print 'ok' or "
             "'refuse:<reason>' (paste_refusal: positive dialog evidence). No tmux I/O.")
    p.add_argument(
        "--enter-anyway-check", action="store_true",
        help="T-4646 PREDICATE-ONLY mode: pane text on stdin; print 'ok' or "
             "'refuse:<reason>' (enter_anyway_refusal). No tmux I/O.")
    p.add_argument(
        "--enter-check", action="store_true",
        help="T-4832 PREDICATE-ONLY mode: pane text on stdin; print 'ok' or "
             "'refuse:<reason>' (enter_refusal: a dialog in the bottom region). No tmux I/O.")
    p.add_argument("--text", default="",
                   help="the injected text (needed by composer-style predicates)")
    p.add_argument("pane", nargs="?",
                   help="tmux target pane (full-orchestration mode only)")
    p.add_argument("text_pos", nargs="?", metavar="text",
                   help="text to inject and submit (full-orchestration mode only)")
    return p


def _profile_with_env_overrides(harness: str) -> dict:
    """Profile copy with optional env overrides for the tunables, so existing
    callers/tests can speed up or retune without code changes:
      INJECT_ENTER_DELAY -> settle_delay_s,  SUBMIT_RETRIES -> submit_retries.
    (Names mirror the legacy send_keys_submit.sh env contract.)"""
    profile = dict(get_profile(harness))
    delay = os.environ.get("INJECT_ENTER_DELAY")
    if delay is not None:
        try:
            profile["settle_delay_s"] = float(delay)
        except ValueError:
            pass
    retries = os.environ.get("SUBMIT_RETRIES")
    if retries is not None:
        try:
            profile["submit_retries"] = int(retries)
        except ValueError:
            pass
    return profile


def _run_pending_check(harness: str, text: str, stdin) -> int:
    """Predicate-only mode: pane text on stdin -> print 'pending'/'submitted'.

    This is the seam send_keys_submit.sh uses: bash owns the tmux orchestration
    (send-keys/capture as direct children, watchdog-reapable) and asks python the
    single profile-driven question. Always exits 0 on a clean evaluation; the
    verdict is on stdout. A non-zero exit means the predicate itself errored —
    callers must treat that as 'could not confirm' (fail-safe: keep retrying /
    report unconfirmed, NEVER false-pass to 'submitted')."""
    profile = get_profile(harness)
    pane_text = stdin.read()
    verdict = "pending" if submit_pending(profile, pane_text, text) else "submitted"
    sys.stdout.write(verdict + "\n")
    return 0


def main(argv: list[str] | None = None, *, stdin=None) -> int:
    args = _build_parser().parse_args(argv)

    if args.pending_check:
        return _run_pending_check(args.harness, args.text, stdin or sys.stdin)
    if args.repress_check:
        verdict = should_repress(get_profile(args.harness), (stdin or sys.stdin).read(), args.text)
        sys.stdout.write(("repress" if verdict else "hold") + "\n")
        return 0
    if args.ready_check:
        pane_text = (stdin or sys.stdin).read()
        not_ready = not_ready_reason(pane_text)
        busy = None if not_ready is not None else busy_reason(pane_text)
        verdict = (
            f"not_ready:{not_ready}" if not_ready is not None
            else f"busy:{busy}" if busy is not None
            else "ready"
        )
        sys.stdout.write(verdict + "\n")
        return 0
    if args.paste_check or args.enter_anyway_check or args.enter_check:
        pane_text = (stdin or sys.stdin).read()
        reason = (paste_refusal(pane_text) if args.paste_check
                  else enter_refusal(pane_text) if args.enter_check
                  else enter_anyway_refusal(pane_text, args.harness))
        sys.stdout.write(("ok" if reason is None else f"refuse:{reason}") + "\n")
        return 0

    # Full-orchestration mode (python-native callers / manual use). The bash
    # restart path does NOT use this — it keeps tmux I/O in bash (see above).
    if not args.pane or args.text_pos is None:
        sys.stderr.write(
            "[submit_verify] ERROR: full-orchestration mode needs <pane> <text>\n")
        return 1
    profile = _profile_with_env_overrides(args.harness)
    result = submit_verify(
        profile, args.pane, args.text_pos,
        send_text=tmux_send_text, send_enter=tmux_send_enter, capture=tmux_capture,
    )
    if result.ok:
        return 0
    # Match the legacy send_keys_submit.sh stderr vocabulary ("failed"/"unconfirmed")
    # so existing log greps / tests stay valid.
    sys.stderr.write(
        f"[submit_verify] ERROR: {result.detail} (harness={args.harness}, pane={args.pane})\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
