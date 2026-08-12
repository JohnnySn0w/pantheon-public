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
        "paste_sentinel_scope": "whole_pane",
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
    needle = _normalize_ws(injected_text)[:needle_len].strip()
    if not needle:
        # Nothing distinctive to match (empty/whitespace injection) — can't
        # assert it's still pending, so treat as not-pending (don't loop forever).
        return False
    for line in reversed(pane_text.splitlines()):
        if line.lstrip().startswith(glyph):
            return needle in _normalize_ws(line)
    return False  # no composer line visible -> not pending (submitted/cleared)


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
                # Claude's pending collapsed-paste marker is measured above a
                # trailing U+276F composer line. Whole-pane preserves its
                # historical predicate and fails closed if a stale marker ever
                # lingers: retry/unconfirmed, never false-pass.
                if sentinel in pane_text:
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
_READY_DIALOG_PATTERN = re.compile(r"Enter to confirm|Esc to cancel")
_READY_COMPOSER_GLYPHS = ("❯", "›")  # claude U+276F, codex U+203A


def ready_for_submission(pane_text: str) -> bool:
    """True iff a submit into ``pane_text`` right now would actually land.

    Full conjunction — every condition must hold:
      1. ``pane_text`` is non-empty.
      2. A prompt-frame edge is visible (the TUI has finished drawing its box).
      3. A composer/idle glyph is visible — claude's ``❯`` OR codex's ``›``.
      4. No focused confirm modal is blocking input ("Enter to confirm" /
         "Esc to cancel" absent).
    """
    if not pane_text:
        return False
    if not _READY_FRAME_PATTERN.search(pane_text):
        return False
    if not any(glyph in pane_text for glyph in _READY_COMPOSER_GLYPHS):
        return False
    if _READY_DIALOG_PATTERN.search(pane_text):
        return False
    return True


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
      send text once -> for each attempt: settle, send a separate Enter, capture,
      check ``submit_pending(profile, ...)``; stop on first not-pending (submitted).
      Retry the Enter up to ``submit_retries`` times, else report unconfirmed.
    Only ``submit_pending`` (the predicate) differs by harness.
    """
    settle = float(profile.get("settle_delay_s", DEFAULT_SETTLE_DELAY_S))
    retries = int(profile.get("submit_retries", DEFAULT_SUBMIT_RETRIES))

    if not send_text(pane, text):
        return SubmitResult(False, 0, "send-keys (text) failed")

    for attempt in range(retries + 1):
        if settle:
            sleep(settle)
        if not send_enter(pane):
            return SubmitResult(False, attempt + 1, "send-keys (Enter) failed")
        if not submit_pending(profile, capture(pane), text):
            return SubmitResult(True, attempt + 1, "submit confirmed")
        # still pending — paste-mode/composer hasn't cleared; retry the Enter
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
