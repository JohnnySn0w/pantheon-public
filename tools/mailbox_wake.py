"""tools/mailbox_wake.py — the wake half of the ORG-026 mailbox (T-0772 deployment layer).

Fired as a systemd `--user` oneshot by `pantheon-mailbox-wake@<agent>.path` when the
agent's `~/inbox/new/` changes. Its ONLY job is to WAKE the agent — it does NOT consume
or ack. A generic, content-light kick is injected into the agent's own tmux pane
(reusing the battle-tested `send_keys_submit.sh`); the agent then drains + acks its
inbox IN-SESSION via `tools/mailbox.py`'s consume (where readdir-is-truth + at-least-once
already live).

Why wake-only — the load-bearing design choice (interacts with T-0899):
    If the waker consumed+acked and THEN kicked, a dropped kick (the silent boot-window
    kick-drop, T-0899) would lose the message: acked to cur/, but the agent never acted.
    By leaving every message in new/ until the AGENT itself consumes it, a dropped wake
    is non-fatal — the message simply waits for the next wake (or the agent's own drain).
    Wake is best-effort/retriable; consume+ack is durable/in-agent. Clean separation.

Debounce: a marker file (`inbox/.wake_marker`) records the newest message name we last
kicked for. While messages sit unconsumed and nothing newer has arrived, the .path may
re-fire (spurious mtime touches) — we skip re-kicking so we don't spam the pane. A
genuinely newer message (names[-1] advances) re-nudges.

T-1901 (the Mason first-retask hole): the debounce must also NOT hold when the kick's
TARGET is gone. A kick lands in a pane, the session is then restarted (fresh MCP enums,
context-fill cycle, seat bringup), and the fresh session never saw the nudge — with the
newest-message-id unchanged, the old marker suppressed every retry indefinitely. The
marker therefore records WHO was kicked, not just FOR WHAT: a JSON
`{"newest": <name>, "session": <tmux session_created:pane_pid>, "ts": <epoch>}`.
`decide()` re-kicks when the newest advances (unchanged rule), when the pane's session
identity differs from the marker's (the kicked session is dead — its replacement never
got the nudge), or when the kick is older than REKICK_TTL_SECONDS with mail still
unread (bounded kick-verification: covers T-0899-class swallowed kicks at ≤1 kick/TTL).
Legacy plain-text markers are read as {newest, session: None, ts: None}: with a live
session key available they wake ONCE (un-strands any pre-upgrade stuck seat), then the
marker is rewritten in the new format; with no session key readable the legacy
newest-only debounce applies unchanged — fail-safe, never noisier than before on a
box where tmux can't be queried.

T-2158 (the mid-turn swallowed kick): submit-verify's success criterion is "the text
is no longer PENDING in the pane", which is NOT "the agent will process it". Injected
mid-turn the input line clears, the verify passes, the kick is a no-op — and the
debounce then counts it delivered, correctly by its own logic. An agent that is busy
every time the waker fires can therefore stay un-drained indefinitely with every log
line green (specialist, 2026-07-28: 12 messages, 32 minutes, six debounced sweeps). A FIFTH
condition re-kicks when the pane has gone IDLE since a kick that mail outlived — once
per newest message, on the busy->idle transition, so the state CHANGE is the signal and
no anti-spam threshold has to be invented. The TTL above remains the backstop for a
swallowed idle kick, and an unreadable/foreign pane reports UNKNOWN and changes nothing.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:  # T-4096: the hub path comes from instance.yaml, never a literal.
    from tools.agent_role_map import mailbox_hub
except ImportError:  # script run: tools/ is on sys.path, the repo root is not
    from agent_role_map import mailbox_hub  # type: ignore
try:  # T-4792: the live harness comes from the controller's runtime.json.
    from tools import live_harness
except ImportError:
    import live_harness  # type: ignore

# Per-agent substrate (pane + owning account). Kept as a small explicit map rather
# than importing inbox_consumer — that module carries live-Telegram/tmux side-effect
# surface we must not pull into a oneshot on the real-token host (the full-suite leak
# lesson). Values mirror inbox_consumer._AGENT_HEALTH_CONFIG.
AGENT_PANE = {
    "orchestrator": {"pane_target": "claude-telegram-bot:0", "home": "/home/claude"},
    "specialist": {"pane_target": "claude-research-bot:0", "home": "/home/claude-research"},
    "recovery": {"pane_target": "claude-telegram-bot-fallback:0", "home": "/home/claude-fallback"},
    # T-1896 (ORG-043): sub-orchestrator seats (Mason/Artisan). Standing sessions
    # are plain claude TUIs in tmux on their own accounts - no telegram plugin.
    "mason": {"pane_target": "suborch1-main:0", "home": "/home/suborch1"},
    "artisan": {"pane_target": "suborch2-main:0", "home": "/home/suborch2"},
    # codex (T-1041 mail-comms): an OpenAI codex-CLI agent, not Claude Code. Its
    # tmux session is "codex-session" and a separate Enter submits a turn (same as
    # its telegram poller's proven inject path). T-1106: it MUST pass
    # `harness: codex` so send_keys_submit verifies against the codex profile —
    # the claude-code "[Pasted text" sentinel is always-absent on codex's TUI, so
    # the default profile FALSE-PASSES (reports submitted without confirming;
    # same class as the T-1054 poller fix). The codex profile checks codex's
    # composer-holds-text predicate instead.
    "codex": {"pane_target": "codex-session", "home": "/home/codex", "harness": "codex"},
    # archivist (T-1824 / ORG-042 second head): standing telegram daemon under the
    # `exocortex` user, same supervised-tmux shape as recovery. Claude-Code harness.
    # NOTE: her wake/inbox-consumer timers install in HER user manager during her
    # harness bringup; this entry only readies hub-side wake config.
    "archivist": {"pane_target": "claude-telegram-bot-exocortex:0", "home": "/home/exocortex"},
    # watchman (T-4884 B6): codex-primary recovery seat on codex-fallback. A codex
    # TUI, so it MUST carry harness: codex (same T-1106 reason as codex above).
    # Pane = instance.yaml harness_runtime.roles.system-orchestrator-1.tmux.session.
    "watchman": {"pane_target": "codex-fallback-main:0", "home": "/home/codex-fallback", "harness": "codex"},
}
# Default harness for agents that don't specify one (the Claude-Code personas).
_DEFAULT_HARNESS = "claude-code"

_SEND_KEYS_HELPER = Path(__file__).resolve().parent / "send_keys_submit.sh"
_MARKER_NAME = ".wake_marker"
_DEFER_STATE_NAME = ".wake_defer_state"
# T-5283: two wakers can kick one seat (this unit's run() and persona_bringup's consume
# wake path). Both take this lock, non-blocking, from decide through the marker write.
_WAKE_LOCK_NAME = ".wake.lock"
WAKE_LOCK_BUSY_REASON = "wake-lock-busy"

# T-4983: every kick opens with this fixed token, the one distinctive thing that lets
# the recovery below call a composer's contents OUR OWN notice. build_persona_kick and
# stuck_own_notice both read it from here (single source).
WAKE_NOTICE_MARKER = "[pantheon-mailbox-wake] "
# T-5047: the fixed closing sentence of every kick (marker-era and legacy alike). A
# stuck composer is only ours when it ENDS with this, so "our notice + text the user
# typed after it" is never submitted. build_persona_kick reads it from here too.
WAKE_NOTICE_TAIL = ("Messages stay in new/ until you consume them, so nothing is lost "
                    "if you finish your current turn first.")
# T-5047: collapsed-paste placeholders (Claude "[Pasted text #N +K lines]", codex
# "[Pasted Content N chars]"; shapes from submit_verify's profiles/tests). A placeholder
# carries no text to verify, so it is ours only with a recent unconfirmed own inject.
_PLACEHOLDER_RE = re.compile(
    r"\[Pasted text #\d+ \+\d+ lines\]|\[Pasted Content \d+ chars\]")
_INJECT_RECORD_NAME = ".wake_last_inject"
INJECT_RECORD_TTL_S = 600.0
# The pre-T-4983 opening, accepted only anchored at the start of the composer text, so a
# pane already stuck on an old kick recovers too.
_LEGACY_NOTICE_RE = re.compile(
    r"You have \d+ unread inter-agent messages? in your mailbox \(from")
# Consecutive UNKNOWN deferrals for one unread set before the stuck-seat alert: 12 at the
# 2-min path cadence is ~24 min.
STUCK_ALERT_THRESHOLD = 12
STUCK_ALERT_VOCAB = "wake-stuck"
_STUCK_ENTER_SETTLE_S = 1.0
# The last composer glyph must sit this close to the pane bottom (non-blank lines).
_STUCK_COMPOSER_MAX_LINES = 40
_COMPOSER_GLYPH = {"codex": "\u203a", "claude-code": "\u276f"}  # codex U+203A, claude U+276F

# Path A (2026-06-09): inboxes live in the neutral group-accessible hub under
# the hub owner's home (ORG-026 "Conditionally a shared HOME"), NOT each agent's own
# mode-750 home — a private home can't be traversed cross-account to deliver. Each
# persona's inbox is MAILBOX_HUB/<persona>. (`home` in AGENT_PANE is now only the
# pane-account hint; it no longer locates the inbox.) The hub root itself is
# instance.yaml's mailbox.hub (T-4096), so an ORG-087 move is a manifest flip.
MAILBOX_HUB = Path(mailbox_hub())


# --------------------------------------------------------------------------- peek
def peek(new_dir: Path) -> list[str]:
    """Sorted names of delivered-but-unconsumed messages (readdir is the truth).

    Reads, never moves — peeking must not ack. Skips dotfiles (the lock/marker live
    in the inbox root anyway, not in new/). Missing new/ → no messages.

    ⚠️ This is the raw readdir. To decide whether to WAKE a seat use `deliverable()`
    instead — a message can be present in new/ and still be one the drain refuses to
    hand over (T-2384)."""
    try:
        return sorted(p.name for p in new_dir.iterdir() if p.is_file() and not p.name.startswith("."))
    except FileNotFoundError:
        return []


def _mailbox_module():
    """The mailbox TRANSPORT module, or None if it cannot be had.

    Deliberately paranoid about one specific mis-import: a bare `import mailbox`
    with tools/ absent from sys.path silently binds Python's STDLIB mailbox
    (mailbox.py's own usage note calls this out). That module imports fine and has
    none of our helpers, so the discriminator is the attribute, not the import."""
    for name in ("tools.mailbox", "mailbox"):
        try:
            mod = __import__(name, fromlist=["backoff_active"])
        except ImportError:
            continue
        if hasattr(mod, "backoff_active"):
            return mod
    return None


def deliverable(new_dir: Path, now: float | None = None) -> list[str]:
    """Unread messages the drain would actually hand to the agent right now.

    peek() minus every message inside its retry-backoff window (T-2386's sidecar).
    This is the count a wake decision must use, and the reason is the whole of
    T-2384: archivist's seat took 242 consecutive "1 unread from inbox_consumer"
    wakes across three days, every one of them TRUE — one real message sat in new/
    that could not be delivered, and the waker re-kicked it once per TTL forever.
    T-2386 bounded the retry RATE, which fixed the spin but not the wake: a backed
    -off message still reads as 1 unread to a raw readdir, so the loop survives its
    own fix — and converges, since the backoff cap (1800s) equals REKICK_TTL_SECONDS.

    Waking for a message the drain will not claim cannot accomplish anything: the
    seat's own consume() applies the same backoff and returns having done nothing.
    Nothing is lost by staying quiet — when the window lapses the next poll wakes
    the seat normally.

    FAIL-SOFT, in the same direction as every other layer here: if the transport
    module or the sidecar cannot be read we count the message, i.e. we degrade to
    the pre-T-2384 behaviour (a spurious wake) rather than to silence. A broken
    backoff store must never be able to suppress mail."""
    names = peek(new_dir)
    if not names:
        return names
    mb = _mailbox_module()
    if mb is None:
        return names
    inbox = new_dir.parent
    out = []
    for name in names:
        try:
            if mb.backoff_active(inbox, name, now):
                continue
        except Exception:
            pass  # unreadable/odd sidecar → treat as deliverable (fail-soft)
        out.append(name)
    return out


# Re-kick TTL (env-overridable): while mail sits unread, re-nudge at most once per
# this window even if newest-id and session are unchanged (a kick can be swallowed
# by a busy/booting session — T-0899 class). Bounded by construction: ≤1 kick/TTL.
REKICK_TTL_SECONDS = int(os.environ.get("PANTHEON_MAILBOX_REKICK_TTL_SECONDS", "1800"))


# ------------------------------------------------------- urgent bypass (T-1641 Ph2)
# Cap the per-decide() urgent scan so a pathological backlog cannot make this an
# unbounded read — a deep backlog is mail_staleness's territory, not the waker's.
_URGENT_SCAN_CAP = 200

# Reason string for the bypass branch (T-1641), named for the same reason
# IDLE_REKICK_REASON is: run() matches on it to decide idle_kicked bookkeeping, and
# a free-form sentence would be a silent coupling between decide() and run().
URGENT_BYPASS_REASON = "urgent bypasses debounce (Phase 2, T-1641)"


def _urgent_phase2_enabled() -> bool:
    """Fail-CLOSED proxy for mailbox.urgent_phase2_enabled(): importing mailbox_wake
    must never crash if mailbox.py cannot be found, and "cannot tell" means OFF —
    the same direction the flag itself defaults to. Lazy import, mirroring
    _mailbox_module()'s own rationale for not paying that cost at module load."""
    mb = _mailbox_module()
    if mb is None or not hasattr(mb, "urgent_phase2_enabled"):
        return False
    try:
        return bool(mb.urgent_phase2_enabled())
    except Exception:
        return False


def _has_urgent(new_dir: Path, names: list[str]) -> bool:
    """Best-effort: True iff any of `names` (already filtered to deliverable) is
    severity=="urgent". Scans ALL of `names`, not just the newest — an urgent
    message can sit BEHIND a later normal one (severity does not affect delivery
    order), and a newest-only check would silently stop bypassing for exactly the
    message the bypass exists for.

    FAIL-CLOSED per file: an unreadable/malformed message is skipped, not counted.
    A flaky read must never manufacture bypass power it wasn't handed — the
    asymmetry matters because this predicate's True branch changes what decide()
    does on every subsequent sweep, unlike a read failure elsewhere in this module
    that just degrades one kick's text."""
    for name in names[:_URGENT_SCAN_CAP]:
        try:
            rec = json.loads((new_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError, AttributeError):
            continue
        if rec.get("severity", "normal") == "urgent":
            return True
    return False


# Reason string for condition 5 (T-2158). A named constant because run() has to
# recognise an idle-triggered wake to record it on the marker, and matching on a
# free-form sentence would be a silent coupling.
IDLE_REKICK_REASON = "pane went idle since kick, mail still unread (T-2158)"

# T-4120: after a failed kick, re-read the pane this many times, this far apart
# (~3s), before calling it a dead seat. One sample could land in the brief
# unclassifiable submit->generate transition. Well inside the unit's 90s budget.
RECHECK_SAMPLES = 4
RECHECK_INTERVAL_SECS = 1.0

# T-4800: on exit 8, the bottom of the pane goes to stderr (the journal, which the
# OnFailure alert forwards). The 2026-10-09 archivist exit 8 was a false one (the
# turn had landed 5s earlier), and with only the verdicts logged the screen the
# re-checks misread could not be reconstructed. 8 lines cover the bottom-6 region
# pane_idle_state reads plus the composer, and keep the verdict line inside the
# 12-line journal tail unit_failure_alert forwards (systemd adds ~4 lines after).
EXIT8_PANE_TAIL_LINES = 8


def pane_tail(pane_target: str, lines: int = EXIT8_PANE_TAIL_LINES) -> list[str]:
    """The last `lines` non-blank lines of the pane as plain text, [] on any failure.
    Diagnostic only: nothing decides on it."""
    if not pane_target or not pane_target.strip():
        return []
    try:
        out = subprocess.run(["tmux", "capture-pane", "-p", "-t", pane_target],
                             capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return []
        return [ln.rstrip() for ln in out.stdout.splitlines() if ln.strip()][-lines:]
    except Exception:
        return []


def _read_marker(marker: Path) -> dict | None:
    """Marker state: {"newest": str, "session": str|None, "ts": float|None,
    "idle_kicked": bool}.

    New markers are JSON; a legacy plain-text marker (pre-T-1901) parses as a
    newest-only record. Unreadable/empty → None.

    `idle_kicked` (T-2158) records that the kick on file was itself triggered by
    condition 5, so the idle re-kick fires ONCE per newest message rather than on
    every sweep the pane stays idle. Absent/legacy → False."""
    try:
        raw = marker.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    if not raw:
        return None
    try:
        state = json.loads(raw)
        if isinstance(state, dict) and isinstance(state.get("newest"), str):
            return {"newest": state["newest"], "session": state.get("session"),
                    "ts": state.get("ts"), "idle_kicked": bool(state.get("idle_kicked"))}
    except (json.JSONDecodeError, ValueError):
        pass
    # legacy plain-text form
    return {"newest": raw, "session": None, "ts": None, "idle_kicked": False}


def _write_marker(marker: Path, newest: str, session_key: str | None, now: float,
                  idle_kicked: bool = False) -> None:
    payload = json.dumps(
        {"newest": newest, "session": session_key, "ts": now,
         "idle_kicked": idle_kicked},
    ) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        prefix=f"{marker.name}.",
        dir=marker.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o660)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker)
        directory = os.open(marker.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def session_key(pane_target: str) -> str | None:
    """Identity of the session/pane a kick would land in: `session_created:pane_pid`.

    Changes whenever the tmux session is recreated (service restart kills + respawns
    it) or the pane's root process is replaced. Any failure (no tmux, dead target,
    timeout) → None — callers must then fall back to legacy newest-only debounce,
    NEVER treat it as 'changed' (a flaky query must not cause kick spam)."""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_target,
             "#{session_created}:#{pane_pid}"],
            capture_output=True, text=True, timeout=10,
        )
        key = r.stdout.strip()
        # A dead/unknown target can still exit 0 with EMPTY format fields (":" on
        # tmux 3.4 — caught by the contract test), so validate the shape: both
        # session_created and pane_pid are numeric.
        if r.returncode == 0 and re.fullmatch(r"\d+:\d+", key):
            return key
        return None
    except (subprocess.SubprocessError, OSError):
        return None


def pane_is_idle(pane_target: str) -> bool | None:
    """Is `pane_target` sitting at an idle prompt? True/False/None (None = UNKNOWN).

    The impure half of T-2158 condition 5, and a deliberate sibling of session_key():
    same failure contract — ANY failure (no tmux, dead target, timeout, unreadable
    capture, foreign TUI) returns None, and None must never cause a wake.

    Classification is DELEGATED to inbox_consumer.pane_idle_state rather than copied.
    That module owns the T-0716 sentinels and their precedence rule (generating wins
    when both are present; a modal footer is UNKNOWN, not idle), and a second copy of
    that logic is exactly the drift shape T-2171 just had to fix in keep_going. The
    import is LAZY and inside the try: mailbox_wake is deliberately import-light (see
    AGENT_PANE's note on not pulling that module's side-effect surface into a
    oneshot), so the cost is paid only on the path that needs it, and an ImportError
    degrades to None like any other failure rather than breaking the waker.
    """
    if not pane_target or not pane_target.strip():
        # An empty target does NOT make tmux fail — it captures the ambient pane, so
        # a missing/blank config value would silently classify the WRONG session.
        # Caught by test_pane_is_idle_returns_none_on_empty_target.
        return None
    try:
        out = subprocess.run(
            # -e: pane_idle_state needs the DIM attribute to tell a prompt
            # suggestion from typed text (T-4284).
            ["tmux", "capture-pane", "-p", "-e", "-t", pane_target],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return None
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        from inbox_consumer import pane_idle_state  # noqa: PLC0415 (lazy by design)
        return pane_idle_state(out.stdout)
    except Exception:
        # Intentionally broad: this is a best-effort signal on the wake path, and the
        # fail-safe direction is UNKNOWN. No exception from a pane probe may ever stop
        # an agent being woken by conditions 1-4.
        return None


def _plain_tail_busy(tail: list[str]) -> bool:
    """T-5002: True only if pane_idle_state reads these plain lines as GENERATING (False).
    Same lazy import and the same fail-safe direction as pane_is_idle: any error is "not
    busy", so the exit-8 alarm stands."""
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        from inbox_consumer import pane_idle_state  # noqa: PLC0415 (lazy by design)
        return pane_idle_state("\n".join(tail)) is False
    except Exception:
        return False


def unknown_pane_deliverable(pane_target: str, harness: str = _DEFAULT_HARNESS) -> bool:
    """T-4292: may a wake be injected into a pane whose idle verdict was UNKNOWN (None)?

    The kick is a paste plus Enter. On a claude pane, UNKNOWN covers a permission or
    confirm modal ("Esc to cancel · Tab to amend", "Enter to confirm · Esc to
    cancel"), a modal outside that list (the spend/rate-limit picker, which held
    three seats on 2026-09-29), and any screen the classifier does not recognise.
    An Enter there can confirm a dialog the agent was waiting on, so a claude pane
    needs POSITIVE evidence, and pane_idle_state is that evidence: None means no.
    (A generating turn whose footer omits "esc to interrupt" reads BUSY since
    T-4284, via the spinner above the composer.)

    codex is different. pane_idle_state reads claude's sentinels, so it returns None
    for EVERY codex screen, and "None means no" would stop waking codex at all. There
    the positive evidence is submit_verify.ready_for_submission (frame, "›"
    composer, no confirm dialog) plus neither codex_modal screen. codex runs with
    approval_policy = "never", so it has no permission prompts to confirm.

    Any failure returns False: deferring only retains mail, and the drain-progress
    staleness monitor escalates a queue that stops draining (T-2267).

    hermes (T-5011 PR-B): pane_idle_state reads claude's sentinels, so a hermes
    pane is UNKNOWN too. Its positive evidence is submit_verify.hermes_pane_idle_state
    (the idle input line, no busy hint or turn timer); approvals are off, so there
    is no permission prompt to confirm."""
    if harness == "hermes":
        return _hermes_pane_deliverable(pane_target)
    if harness != "codex":
        return False
    try:
        out = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane_target],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return False
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        from submit_verify import ready_for_submission  # noqa: PLC0415 (lazy by design)
        from codex_modal import (  # noqa: PLC0415
            is_codex_model_migration_modal, is_codex_update_modal)
        text = out.stdout
        return (ready_for_submission(text)
                and not is_codex_update_modal(text)
                and not is_codex_model_migration_modal(text))
    except Exception:
        return False


def _is_our_notice(text: str | None) -> bool:
    """Our notice: opens with the marker (or the legacy opening) AND ends with the fixed
    closing sentence (T-5047 anchor-at-end)."""
    return bool(text) and (text.startswith(WAKE_NOTICE_MARKER.strip())
                           or bool(_LEGACY_NOTICE_RE.match(text))
                           ) and text.rstrip().endswith(WAKE_NOTICE_TAIL)


def write_inject_record(path: Path, ts: float, session: str | None,
                        confirmed: bool) -> None:
    """T-5047: remember the last kick we injected into this pane. Never raises. Mode 0600:
    the inbox dir is group-writable, so read_inject_record trusts only a file no other
    seat could have written."""
    try:
        _atomic_json(path, {"ts": ts, "session": session, "confirmed": bool(confirmed)},
                     mode=0o600)
    except Exception:
        pass


def read_inject_record(path: Path) -> dict | None:
    """The inject record, or None. Trusted only when it is a regular file we opened
    without following a symlink, owned by this process's euid, with no group/other write
    bit (checked on the opened fd, so nothing can swap in between check and read). Any
    doubt is None."""
    import stat  # noqa: PLC0415
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
                or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            return None
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = None
            rec = json.loads(handle.read(65536))
        return rec if isinstance(rec, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _record_vouches(record: dict | None, now: float | None, session: str | None) -> bool:
    """The positive evidence for a placeholder: our last inject here was UNCONFIRMED, is
    at most INJECT_RECORD_TTL_S old, and went to this same session."""
    try:
        if (not record or record.get("confirmed") is not False or now is None
                or not session or record.get("session") != session):
            return False
        age = now - float(record["ts"])
        return 0 <= age <= INJECT_RECORD_TTL_S
    except Exception:
        return False


def composer_parts(pane_text: str, glyph: str) -> tuple[str, list[str]] | None:
    """T-4983/T-5047: the live composer split at its first blank line. Returns
    (paragraph, rest): the paragraph is the glyph line plus its wrapped continuation
    lines (glyph dropped, joined with single spaces); rest is the non-blank lines after
    that blank line, inside the composer region (codex draws its footer there, since it
    has no bottom border). None when no composer is visible or it is implausibly long."""
    lines = [ln for ln in pane_text.splitlines()]
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    from submit_verify import _composer_region  # noqa: PLC0415 (lazy by design)
    region = _composer_region(lines, glyph)
    if region is None or len([ln for ln in region if ln.strip()]) > _STUCK_COMPOSER_MAX_LINES:
        return None
    cut = next((i for i, ln in enumerate(region) if not ln.strip()), len(region))
    first = region[0].lstrip()[len(glyph):]
    para = " ".join(part.strip() for part in [first, *region[1:cut]] if part.strip())
    rest = [ln.strip() for ln in region[cut:] if ln.strip()]
    return para, rest


def duplicate_render_parts(pane_text: str, glyph: str) -> tuple[str, list[str]] | None:
    """T-4987: composer_parts of the composer ABOVE the footer when codex drew it twice
    (submit_verify.codex_duplicate_render: a cut-off copy below the footer, watchman
    2026-10-22). Only when every below-footer block is a piece of that composer's text,
    so nothing but our two renders is on screen; anything else is None."""
    if glyph != _COMPOSER_GLYPH["codex"]:
        return None
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    from submit_verify import codex_duplicate_render, duplicate_agrees  # noqa: PLC0415
    lines = pane_text.splitlines()
    dup = codex_duplicate_render(lines)
    if dup is None:
        return None
    parts = composer_parts("\n".join(lines[:dup[1] + 1]), glyph)
    if parts is None or not duplicate_agrees(dup[2], "".join(parts[0].split())):
        return None
    return parts


def composer_text(pane_text: str, glyph: str) -> str | None:
    """The composer's first paragraph (see composer_parts), or None."""
    parts = composer_parts(pane_text, glyph)
    return None if parts is None else parts[0]


def _footer_only(rest: list[str]) -> bool:
    """T-5047 anchor-at-end: after the composer paragraph there may be nothing, or one
    status-footer-shaped line (codex: "model effort · cwd"). Anything else could be text
    the user typed after our notice."""
    if not rest:
        return True
    if len(rest) != 1:
        return False
    # Reuse health_export's codex footer recognizer ("model[ effort] \u00b7 cwd"), and
    # additionally require the cwd to be path-shaped, so user text like `rm -rf x \u00b7 y`
    # or `echo a \u00b7 b` is never taken for a footer. Any failure to import is False.
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        from health_export import _CODEX_FOOTER_RE  # noqa: PLC0415
        line = rest[0]
        return (_CODEX_FOOTER_RE.match(line) is not None
                and line.split(" \u00b7 ", 1)[1].lstrip().startswith(("~", "/")))
    except Exception:
        return False


def stuck_composer_state(pane_text: str, harness: str, record: dict | None = None,
                         now: float | None = None, session: str | None = None
                         ) -> str | None:
    """T-4983/T-5047: classify the composer. "ours" = one Enter is safe; "unverified" =
    a collapsed placeholder (or placeholder plus text) we cannot vouch for, or one with
    dialog evidence: never press Enter, but count it toward the wake-stuck alert;
    None = not ours / busy / unclear (caller defers as before). Any exception is None.

    "ours" needs: a codex or claude-code harness; composer text that is our notice
    (marker or legacy opening at the start, WAKE_NOTICE_TAIL at the end) OR exactly one
    collapsed placeholder vouched for by the recent unconfirmed own-inject record; and
    no dialog evidence (codex_modal_monitor.classify_codex_pane, the codex update and
    migration modals, submit_verify's paste/enter refusals); and no busy evidence
    (submit_verify.busy_reason, "esc to interrupt" after the composer glyph)."""
    glyph = _COMPOSER_GLYPH.get(harness)
    if glyph is None or not pane_text or not pane_text.strip():
        return None
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        from submit_verify import busy_reason, enter_refusal, paste_refusal  # noqa: PLC0415
        from codex_modal import (  # noqa: PLC0415
            is_codex_model_migration_modal, is_codex_update_modal)
        from codex_modal_monitor import classify_codex_pane  # noqa: PLC0415
        parts = composer_parts(pane_text, glyph)
        if parts is None:
            return None
        text, rest = parts
        anchored = _footer_only(rest)
        notice = _is_our_notice(text) and anchored
        if not notice:
            # T-4987: the last glyph line was a cut-off duplicate below the footer; read
            # the composer above the footer instead (same anchor and evidence rules).
            dup = duplicate_render_parts(pane_text, glyph)
            if dup is not None:
                text, rest = dup
                anchored = _footer_only(rest)
                notice = _is_our_notice(text) and anchored
        placeholder = bool(text) and _PLACEHOLDER_RE.match(text) is not None
        if not (notice or placeholder):
            return None
        if (busy_reason(pane_text) is not None
                # busy_reason reads only the last 6 lines; a wrapped notice can
                # push the footer's "esc to interrupt" out of that window.
                or "esc to interrupt" in pane_text[pane_text.rfind(glyph):]):
            return None
        dialog = (classify_codex_pane(pane_text) is not None
                  or is_codex_update_modal(pane_text)
                  or is_codex_model_migration_modal(pane_text)
                  or enter_refusal(pane_text) is not None
                  or paste_refusal(pane_text) is not None)
        if notice:
            return None if dialog else "ours"
        exact = _PLACEHOLDER_RE.fullmatch(text) is not None and anchored
        if exact and not dialog and _record_vouches(record, now, session):
            return "ours"
        return "unverified"
    except Exception:
        return None


def stuck_own_notice(pane_text: str, harness: str, record: dict | None = None,
                     now: float | None = None, session: str | None = None) -> bool:
    """T-4983: is one Enter safe because the composer holds OUR OWN unsubmitted notice
    (or, T-5047, a vouched collapsed placeholder)? Anything unclear is False."""
    return stuck_composer_state(pane_text, harness, record, now, session) == "ours"


def _capture_plain(pane_target: str) -> str:
    try:
        out = subprocess.run(["tmux", "capture-pane", "-p", "-t", pane_target],
                             capture_output=True, text=True, timeout=10)
        return out.stdout if out.returncode == 0 else ""
    except Exception:
        return ""


def _send_enter(pane_target: str) -> bool:
    """One bare Enter through submit_verify's tmux helper (never a re-paste)."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    from submit_verify import tmux_send_enter  # noqa: PLC0415 (lazy by design)
    return tmux_send_enter(pane_target)


def recover_stuck_notice(agent: str, pane_target: str, harness: str, *,
                         capture_fn=_capture_plain, enter_fn=_send_enter,
                         sleep_fn=time.sleep, record: dict | None = None,
                         now: float | None = None, session: str | None = None) -> str:
    """T-4983: submit our own stuck notice with a single Enter. Returns "none" (not our
    notice, nothing sent), "recovered" (the composer no longer holds it), or "failed"
    (Enter sent, notice still there; or Enter could not be sent), or "unverified" (T-5047:
    a placeholder we cannot vouch for; no Enter sent, the caller counts it). The pane is
    read right before the Enter and again after it."""
    state = stuck_composer_state(capture_fn(pane_target), harness, record, now, session)
    if state == "unverified":
        return "unverified"
    if state != "ours":
        return "none"
    if not enter_fn(pane_target):
        print(f"[mailbox_wake] {agent}: stuck own notice, Enter send FAILED (T-4983)",
              file=sys.stderr)
        return "failed"
    sleep_fn(_STUCK_ENTER_SETTLE_S)
    after = capture_fn(pane_target)
    glyph = _COMPOSER_GLYPH.get(harness, "")
    try:
        held = [composer_text(after, glyph)] if after and glyph else []
        # T-4987: a duplicated render still on screen still holds it.
        dup = duplicate_render_parts(after, glyph) if after and glyph else None
        if dup is not None:
            held.append(dup[0])
        # Start-anchored only: after the Enter, anything still marker-led or
        # placeholder-led means the submit did not take.
        still = any(bool(h) and (
            h.startswith(WAKE_NOTICE_MARKER.strip())
            or _LEGACY_NOTICE_RE.match(h) is not None
            or _PLACEHOLDER_RE.match(h) is not None) for h in held)
    except Exception:
        still = False
    if still:
        print(f"[mailbox_wake] {agent}: sent one Enter to our stuck notice but the "
              f"composer still holds it (T-4983)", file=sys.stderr)
        return "failed"
    print(f"[mailbox_wake] {agent}: submitted our own stuck wake notice with one Enter "
          f"(T-4983)")
    return "recovered"


def _atomic_json(path: Path, payload: dict, mode: int = 0o660) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f"{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(json.dumps(payload) + "\n")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def wake_lock(box: Path):
    """T-5283: serialize the two wakers on <box>/.wake.lock. Yields one of
      "held"     we hold an exclusive flock; released on exit, whatever happens.
      "busy"     another waker holds it; the caller treats this as debounced.
      "unlocked" the lock file could not be opened or locked; the caller proceeds
                 without it (fail open: a wake is never lost to the lock).
    The file is created 0o660 like the marker, so the other seat's waker (same
    group, shared box) can open it. Python fds are non-inheritable and O_CLOEXEC is
    explicit, so the kick's child processes never hold the lock."""
    path = Path(box) / _WAKE_LOCK_NAME
    fd = None
    try:
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o660)
            try:
                os.fchmod(fd, 0o660)  # undo the umask, exactly as _write_marker does
            except OSError:
                pass
        except FileExistsError:
            fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
    except OSError as exc:
        print(f"[mailbox_wake] wake lock {path} unavailable ({exc!r}); "
              f"proceeding unlocked (T-5283 fail open)", file=sys.stderr)
    if fd is None:
        yield "unlocked"  # outside the except, so a caller's error carries no context
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = "held"
        except BlockingIOError:
            state = "busy"
        except OSError as exc:
            print(f"[mailbox_wake] wake lock {path} flock failed ({exc!r}); "
                  f"proceeding unlocked (T-5283 fail open)", file=sys.stderr)
            state = "unlocked"
        yield state
    finally:
        os.close(fd)  # closing the only fd on this description drops the flock


def note_defer(state_path: Path, names: list[str], now: float) -> tuple[int, bool]:
    """T-4983: count one more consecutive deferral for this unread set. Returns
    (count, alert_now); alert_now is True once per episode, when the count reaches
    STUCK_ALERT_THRESHOLD. A different unread set starts a new episode. Any state I/O
    failure returns (0, False): counting must never break the waker."""
    key = "|".join(sorted(names))
    try:
        try:
            prev = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = {}
        if not isinstance(prev, dict) or prev.get("key") != key:
            prev = {"key": key, "count": 0, "first_ts": now, "alerted": False}
        count = int(prev.get("count", 0)) + 1
        alerted = bool(prev.get("alerted"))
        alert_now = count >= STUCK_ALERT_THRESHOLD and not alerted
        _atomic_json(state_path, {"key": key, "count": count,
                                  "first_ts": prev.get("first_ts", now),
                                  "alerted": alerted or alert_now})
        return count, alert_now
    except Exception:
        return 0, False


def clear_defer_state(state_path: Path) -> None:
    try:
        state_path.unlink()
    except OSError:
        pass


def _load_route_failure_alert():
    try:
        from tools.inbox_consumer import route_failure_alert  # package / tests
    except ImportError:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        from inbox_consumer import route_failure_alert  # script run (prod)
    return route_failure_alert


def alert_stuck(agent: str, msg: str) -> None:
    """T-4983: page through inbox_consumer.route_failure_alert, the route the other
    liveness alerts use. For agent == recovery it mails orchestrator (recovery-self-failure), not
    recovery's own deaf inbox. Never raises."""
    try:
        _load_route_failure_alert()(agent, msg, STUCK_ALERT_VOCAB)
    except Exception as exc:  # noqa: BLE001
        print(f"[mailbox_wake] {agent}: stuck-seat alert FAILED ({exc!r})", file=sys.stderr)


def decide(new_dir: Path, marker: Path, current_session: str | None = None,
           now: float | None = None, pane_idle: bool | None = None) -> tuple[str, list[str], str]:
    """Pure decision (no injection, no marker write): return (action, names, reason).

    action ∈ {"empty", "debounced", "wake"}:
      empty     — nothing DELIVERABLE in new/ (absent, or every message is inside
                  its retry-backoff window — T-2384); do nothing.
      debounced — already nudged this session for this newest message recently
                  enough; don't re-spam the pane.
      wake      — nudge: newer message, OR the kicked session is gone (T-1901),
                  OR the last kick aged past REKICK_TTL_SECONDS unconsumed, OR the
                  pane has gone IDLE since a kick that mail outlived (T-2158), OR
                  (flag-gated, T-1641 Ph2) any unconsumed message is severity=
                  "urgent" — bypasses debounce entirely rather than waiting out
                  REKICK_TTL_SECONDS or an idle transition.

    `pane_idle` is THREE-state and the caller computes it (I/O stays out of here,
    exactly as with current_session):
        True  — pane is at an idle prompt
        False — pane is generating
        None  — UNKNOWN (unreadable pane, foreign substrate, tmux failure)

    None MUST NOT trigger a wake — the same contract session_key() documents, "a
    flaky query must not cause kick spam". That is also what makes this degrade
    per-substrate for free: codex's different TUI and archivist's absent pane
    return None and the waker behaves exactly as it did before T-2158.

    CONDITION 5, and why it exists (T-2158): submit-verify's success criterion is
    "the injected text is no longer PENDING in the pane", which is NOT "the agent
    will process it". Injected mid-turn the input line clears, the verify passes,
    and the kick is a no-op — then the debounce correctly counts it delivered. So
    an agent that is busy every time the waker fires can stay un-drained
    indefinitely with every log line green. Observed on specialist 2026-07-28: kicked
    04:09:47, six sweeps debounced, not re-kicked until the TTL backstop at
    04:41:22 — 32 minutes of a 12-message backlog while nothing looked wrong.

    Condition 5 fires ONCE per newest message, on the busy→idle TRANSITION, which
    is deliberate and dissolves a threshold rather than guessing one. The naive
    form ("idle and unread and previously kicked → wake") re-kicks every sweep the
    pane stays idle, so it needs a floor, and 300-600s was an undecided range in
    the original spec. Firing on the transition needs no floor: the state CHANGE is
    the signal, mirroring condition 3 (T-1901's "session identity changed since
    kick") in shape as well as intent. Condition 4's TTL remains the backstop for
    the case where the idle-triggered kick is ALSO swallowed, so nothing is lost by
    not repeating — and max wake latency for this failure drops from ~30 min to one
    timer period, which is what bounds T-2157's detector.

    Ordering note: condition 4 is checked BEFORE 5 on purpose. A TTL-triggered wake
    resets idle_kicked, so an agent that is still stuck after the backstop fires can
    get a fresh idle-triggered kick rather than being permanently spent.
    """
    names = deliverable(new_dir, now)
    if not names:
        return ("empty", names, "no unread")
    state = _read_marker(marker)
    if state is None or state["newest"] != names[-1]:
        return ("wake", names, "newer message")
    if current_session is not None:
        if state["session"] is None:
            # Legacy/unknown-target marker + a readable live session: we cannot know
            # the kick reached THIS session — wake once, marker rewrites in new form.
            return ("wake", names, "marker predates session tracking")
        if state["session"] != current_session:
            return ("wake", names, "session identity changed since kick (T-1901)")
    ts = state["ts"]
    if ts is not None and now is not None and (now - ts) > REKICK_TTL_SECONDS:
        return ("wake", names, f"kick older than {REKICK_TTL_SECONDS}s, mail still unread")
    # Condition 5. `ts is not None` narrows this to markers that actually record a
    # kick; `is True` is explicit so a None never satisfies it by truthiness.
    if pane_idle is True and ts is not None and not state["idle_kicked"]:
        return ("wake", names, IDLE_REKICK_REASON)
    # Condition 6 (T-1641 Ph2), flag-gated and checked LAST: every other condition
    # above is a genuine state CHANGE (newer message, session gone, TTL elapsed,
    # idle transition); this one is not — it deliberately re-wakes on an UNCHANGED
    # debounced state, once per sweep, for as long as urgent mail sits unconsumed.
    # "acting on stale state is worse than interrupting" is the whole design point
    # of severity=urgent (T-1640 design note), so it does not wait out
    # REKICK_TTL_SECONDS the way normal mail does. Flag OFF -> this branch never
    # runs and decide() is byte-for-byte its pre-T-1641 self.
    if _urgent_phase2_enabled() and _has_urgent(new_dir, names):
        return ("wake", names, URGENT_BYPASS_REASON)
    return ("debounced", names, "newest unchanged, same session, within TTL")


# --------------------------------------------------------------------------- senders
def _queued_subjects(new_dir: Path, names: list[str], cap: int = 8) -> list[str]:
    """`sender: subject` for each queued message, oldest first (T-2266, archivist).

    ⚠️ THE COUNT ALONE IS THE DEFECT, not a cosmetic shortfall. Delivery is FIFO at
    one message per turn, so a CORRECTION is authored after the claim it corrects
    and therefore consumed after it — every time. A faster drain narrows that
    window; it cannot close it, because the ordering is the mechanism. "You have 4
    unread messages" gives the receiving turn no way to know that message 4 retracts
    message 1, so it acts on the claim and learns about the retraction afterwards.
    A subject list is the cheapest thing that lets a turn SEE the correction is
    already queued and read out of order deliberately.

    Best-effort throughout: a malformed message contributes nothing and never
    breaks the waker. Capped so a deep queue cannot produce an unbounded prompt —
    and the caller SAYS when it truncated, because a silently clipped list would
    reintroduce exactly the invisibility this fixes.
    """
    import json
    out: list[str] = []
    for n in names[:cap]:
        try:
            rec = json.loads((new_dir / n).read_text(encoding="utf-8"))
            sender = rec.get("sender") or "?"
            subj = (rec.get("subject") or "(no subject)").strip().replace("\n", " ")
        except (OSError, json.JSONDecodeError, ValueError, AttributeError):
            continue
        out.append(f"{sender}: {subj[:110]}")
    return out


def _senders(new_dir: Path, names: list[str]) -> list[str]:
    """Best-effort distinct sender list for the kick text. A malformed/unreadable
    message never breaks the waker — it just doesn't contribute a sender name."""
    import json
    seen: list[str] = []
    for n in names:
        try:
            s = json.loads((new_dir / n).read_text(encoding="utf-8")).get("sender")
        except (OSError, json.JSONDecodeError, ValueError, AttributeError):
            s = None
        if isinstance(s, str) and s not in seen:
            seen.append(s)
    return seen


def build_persona_kick(
    persona: str,
    names: list[str],
    senders: list[str],
    subjects: list[str] | None = None,
    *,
    inbox: Path,
    tools_dir: Path,
    bare_group: bool = False,
) -> str:
    """Build a wake prompt from a resolved persona runtime binding.

    Unlike the legacy wrapper below, this surface does not consult the static
    ``AGENT_PANE`` table.  Runtime-controller callers already have the durable
    manifest binding, stable pane, selected harness, and mailbox path; routing
    them back through a second persona map would make a manifest-only harness
    swap impossible.
    """
    who = ", ".join(senders) if senders else "another agent"
    n = len(names)
    plural = "s" if n != 1 else ""
    drain_cmd = (
        f"python3 -c \"import sys; sys.path.insert(0,'{tools_dir}'); import mailbox; "
        f"print(mailbox.consume('{inbox}', lambda m: print(m['sender'], m['subject'], m['payload'])))\""
    )
    if bare_group:
        # codex runs danger-full-access (HC full-unlock default, T-4929); the
        # unix account is the boundary. A restarted codex session holds the
        # pantheon-mailbox group natively, so run the drain bare — no sg wrapper
        # (older workspace-write sandboxes also blocked `sg`).
        grp_note = (
            "Run it bare — your session holds the pantheon-mailbox group natively; codex's sandbox "
            "blocks `sg` and it isn't needed. (If you hit a write/permission error on the hub, "
            "the hub must be in your config.toml [sandbox_workspace_write] writable_roots.) "
        )
    else:
        grp_note = (
            "NOTE: if a bare run hits PermissionError on the hub, wrap it `sg pantheon-mailbox -c '<the "
            "python command above>'` — a live session-process that predates the pantheon-mailbox-group "
            "grant needs it (T-1016 / a user-manager restart removes this need). "
        )
    # T-2266: name what is queued, in order, so a correction sitting behind the
    # claim it corrects is visible BEFORE the claim is acted on.
    queued = ""
    if subjects:
        lines = "\n".join(f"  {i}. {t}" for i, t in enumerate(subjects, 1))
        more = f"\n  ... and {n - len(subjects)} more" if n > len(subjects) else ""
        queued = (f"\nQUEUED, oldest first — READ THIS BEFORE ACTING ON ANY ONE OF THEM. "
                  f"Delivery is FIFO, so a correction is always consumed AFTER the claim it "
                  f"corrects; if a later subject retracts an earlier one, honour the later "
                  f"one.\n{lines}{more}\n")
    return (
        f"{WAKE_NOTICE_MARKER}"
        f"You have {n} unread inter-agent message{plural} in your mailbox (from {who}). "
        f"{queued}"
        f"Drain them: `{drain_cmd}` "
        f"to inspect+ack, or read {tools_dir}/mailbox.py and consume with a handler that acts on each payload. "
        f"Your inbox is {inbox} (the shared mailbox hub, ORG-026 Path A). "
        f"{grp_note}"
        f"{WAKE_NOTICE_TAIL}"
    )


def build_kick(agent: str, names: list[str], senders: list[str],
               subjects: list[str] | None = None) -> str:
    """Compatibility wrapper for the legacy static pane map."""

    return build_persona_kick(
        agent,
        names,
        senders,
        subjects,
        inbox=MAILBOX_HUB / agent,
        tools_dir=Path(AGENT_PANE[agent]["home"]) / "pantheon-config" / "tools",
        bare_group=agent == "codex",
    )


def _hermes_pane_deliverable(pane_target: str) -> bool:
    """T-5011 PR-B: a hermes pane may take a wake only when
    submit_verify.hermes_pane_idle_state reads it idle. Any failure is False."""
    try:
        out = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane_target],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return False
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        from submit_verify import hermes_pane_idle_state  # noqa: PLC0415 (lazy by design)
        return hermes_pane_idle_state(out.stdout) is True
    except Exception:  # noqa: BLE001 - deferring only retains mail
        return False


# --------------------------------------------------------------------------- inject
def inject(pane_target: str, prompt: str, harness: str = _DEFAULT_HARNESS) -> bool:
    """Inject + submit-verify via send_keys_submit.sh (the T-0519/T-0538 shared path).
    Runs same-account (the @.service runs as the agent) — no sudo. Returns success.

    `harness` selects the submit_verify.py profile (T-1106): claude-code panes
    use the default; codex's pane passes `codex` so submit-verify checks codex's
    composer predicate instead of the always-absent claude-code paste sentinel."""
    try:
        r = subprocess.run(
            [str(_SEND_KEYS_HELPER), "--harness", harness, pane_target, prompt],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[mailbox_wake] inject error: {exc!r}", file=sys.stderr)
        return False
    if r.returncode != 0:
        print(f"[mailbox_wake] inject failed rc={r.returncode}: {r.stderr.strip()}", file=sys.stderr)
    return r.returncode == 0


# --------------------------------------------------------------------------- main
def run(agent: str, *, inbox: Path | None = None, **kwargs) -> int:
    """Wake `agent` under the T-5283 wake lock; a busy lock is a debounce (exit 0)."""
    if agent not in AGENT_PANE:
        return _run_locked(agent, inbox=inbox, **kwargs)  # the unknown-agent 64 path
    box = Path(inbox) if inbox else MAILBOX_HUB / agent
    with wake_lock(box) as lock_state:
        if lock_state == "busy":
            print(f"[mailbox_wake] {agent}: debounced ({WAKE_LOCK_BUSY_REASON}: another "
                  f"waker holds {box / _WAKE_LOCK_NAME}, T-5283)")
            return 0
        return _run_locked(agent, inbox=box, **kwargs)


def seat_in_maintenance(agent: str) -> bool:
    """T-5277: is `agent`'s seat deliberately held (notify_policy seat-maintenance)?

    Fail-open, like inbox_consumer.check_seat_intake: in_maintenance reads any registry
    fault as False, and an import or other raise here is False too, so a broken registry can
    only cost a wake-stuck false alarm, never stop a wake. (ORG-106 L2: the wake path now
    reads seat_hold(); this stays as the maintenance-only probe.)"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import notify_policy
        return bool(notify_policy.in_maintenance(agent))
    except Exception:
        return False


def seat_hold(agent: str):
    """ORG-106 L2 (T-5525): the seat's EffectiveSeatState when anything holds it (seat-state
    record, maintenance, OOM hold, this account's legacy autonomy_off file), else None.
    The waker runs as the seat's own account (pantheon-mailbox-wake@.service, %h), so the
    autonomy_off path is this account's and the record read is direct. Fail-open: any
    fault reads as 'not held', so a broken source can only cost a wake-stuck false alarm."""
    try:
        try:
            from tools import seat_state
        except ImportError:  # script run: tools/ is on sys.path, the repo root is not
            import seat_state  # type: ignore
        e = seat_state.effective_for(
            agent, autonomy_off_path=Path.home() / ".local" / "state" / "pantheon" / "autonomy_off")
    except Exception as exc:
        print(f"[mailbox_wake] {agent}: seat-state reader fault (fail-open): "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    if e.fault:
        print(f"[mailbox_wake] {agent}: seat-state reader fault (fail-open): {e.fault}",
              file=sys.stderr)
    return e if e.intake_blocked else None


def _intake_hold(agent: str, new_dir: Path, names: list[str], maintenance_fn=None,
                 seat_state_fn=None) -> tuple[bool, str]:
    """(hold the wake?, why). ORG-106 L2.

    An injected maintenance_fn is the legacy T-5277 seam: truthy holds everything.
    Otherwise the reader decides. Maintenance, the OOM hold and deep-sleep hold even
    urgent mail (launch_blocked / remediation_vetoed, today's maintenance behaviour).
    When the ONLY sources are paused-intake (seat-state record and/or autonomy_off) the
    seat is alive and PAUSED stops new work, not the seat (ORG-106 decision 2, Q2): normal
    mail queues, mail containing a severity=urgent message still wakes it. This is a
    pause-specific rule; it does NOT consult mailbox.urgent_phase2_enabled() (that flag is
    off in the waker's environment, and governs only the debounce bypass elsewhere)."""
    if maintenance_fn is not None:
        return (True, "seat maintenance") if maintenance_fn(agent) else (False, "")
    st = (seat_state_fn or seat_hold)(agent)
    if st is None or not st.intake_blocked:
        return False, ""
    srcs = ", ".join(st.sources) or "seat hold"
    paused_only = not (st.launch_blocked or st.remediation_vetoed)
    if paused_only and _has_urgent(new_dir, names):
        return False, f"paused-intake ({srcs}) but urgent mail waits"
    return True, srcs


def _appserver_route_module():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "harness/codex/codex_telegram/appserver_route.py"
    spec = importlib.util.spec_from_file_location("_pantheon_appserver_route", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _is_hub_inbox(box: Path, agent: str) -> bool:
    try:
        return Path(box).resolve() == (MAILBOX_HUB / agent).resolve()
    except OSError:
        return False


def _route_off_non_hub(prompt: str, newest: str, home: str) -> tuple:
    return ("off", None)


def appserver_route_try(prompt: str, newest: str, home: str) -> tuple:
    """('off', None) | ('enqueued', delivery_id) | ('unavailable', reason). A failure to
    even load the route module is 'off' only when no switch file exists; with a switch
    present it is 'unavailable' (alerted), so a broken route is never silent."""
    try:
        route = _appserver_route_module()
    except Exception as exc:  # noqa: BLE001 — never let the route break a wake
        switch = Path(home) / ".config/pantheon/codex-appserver-route.json"
        return ("unavailable", f"route module failed to load: {exc}") if switch.exists() \
            else ("off", None)
    try:
        status, detail = route.try_enqueue(prompt, "mailbox", injector="mailbox-wake",
                                           unique=newest, home=home)
    except Exception as exc:  # noqa: BLE001 — a route bug falls back to the pane, alerted
        status, detail = route.UNAVAILABLE, f"route error: {exc!r}"[:300]
    if status == route.UNAVAILABLE:
        route.safe_note_fallback(detail, "mailbox", injector="mailbox-wake", pasted=None,
                                 home=home)
    return status, detail


def _run_locked(agent: str, *, inbox: Path | None = None, do_inject=inject,
                session_key_fn=session_key, now: float | None = None,
                pane_idle_fn=pane_is_idle,
                unknown_pane_ok_fn=unknown_pane_deliverable,
                pane_tail_fn=None, capture_fn=_capture_plain, enter_fn=_send_enter,
                sleep_fn=time.sleep, alert_fn=alert_stuck, maintenance_fn=None,
                route_fn=None, seat_state_fn=None) -> int:
    cfg = AGENT_PANE.get(agent)
    if cfg is None:
        print(f"[mailbox_wake] unknown agent {agent!r} (known: {list(AGENT_PANE)})", file=sys.stderr)
        return 64
    box = Path(inbox) if inbox else MAILBOX_HUB / agent
    new_dir, marker = box / "new", box / _MARKER_NAME
    defer_state = box / _DEFER_STATE_NAME
    inject_record = box / _INJECT_RECORD_NAME
    now = time.time() if now is None else now
    current_session = session_key_fn(cfg["pane_target"])  # None → legacy fallback
    pane_idle = pane_idle_fn(cfg["pane_target"])           # None → unknown, never wakes

    action, names, reason = decide(new_dir, marker, current_session, now, pane_idle)
    if action == "empty":
        print(f"[mailbox_wake] {agent}: inbox/new empty — nothing to wake")
        clear_defer_state(defer_state)
        return 0
    if action == "debounced":
        print(f"[mailbox_wake] {agent}: {len(names)} unread — debounced ({reason})")
        return 0
    # T-5277: a seat in seat-maintenance is held on purpose (its session may not even exist).
    # Its mail stays queued in new/ for the lift; no kick, and no wake-stuck streak either,
    # since "deaf to mail" is the hold working. The streak restarts from zero after the lift.
    # ORG-106 L2 (T-5525): generalised to every intake hold via the seat-state reader; a
    # paused-intake seat still wakes for urgent mail (see _intake_hold).
    held, why = _intake_hold(agent, new_dir, names, maintenance_fn, seat_state_fn)
    if held:
        print(f"[mailbox_wake] {agent}: {len(names)} unread — held ({why}); "
              f"mail stays queued, no kick (T-5277)")
        clear_defer_state(defer_state)
        return 0
    if why:
        print(f"[mailbox_wake] {agent}: {len(names)} unread — {why}; waking (ORG-106 decision 2)")

    # T-5306 (ORG-103 L4): a codex seat whose app-server route switch is on gets its wake
    # through the seat's delivery path. That path queues while the seat is busy, so none
    # of the pane-idle logic below applies. Route down -> alerted, then the pane path.
    if cfg.get("harness") == "codex":
        prompt = build_kick(agent, names, _senders(new_dir, names),
                            _queued_subjects(new_dir, names))
        # T-5660: the live route carries wakes for the seat's real hub inbox only. A
        # wake for any other inbox (a test's tmp dir) took the default route_fn, and run
        # as codex it enqueued into the LIVE spool (consultant 2026-10-27 03:33Z, 17 wakes).
        if route_fn is None and not _is_hub_inbox(box, agent):
            route_fn = _route_off_non_hub
        status, detail = (route_fn or appserver_route_try)(prompt, names[-1], cfg["home"])
        if status == "enqueued":
            _write_marker(marker, names[-1], current_session, now,
                          idle_kicked=(reason == IDLE_REKICK_REASON))
            clear_defer_state(defer_state)
            print(f"[mailbox_wake] {agent}: {len(names)} unread — wake queued on the app-server "
                  f"delivery path as {detail} ({reason})")
            return 0
        if status == "unavailable":
            print(f"[mailbox_wake] {agent}: app-server route unavailable ({detail}); "
                  f"falling back to the pane (alerted)", file=sys.stderr)

    # T-2681: a CONFIRMED-BUSY pane (mid-turn) cannot accept a submit — an inject
    # there fails 'submit unconfirmed' and systemd records a FALSE unit failure
    # (status=8). Waking a busy agent is also pointless: it is already active and
    # drains its own mail. So DEFER on a confirmed-busy pane instead of attempting a
    # doomed inject. The marker is NOT advanced (identical to the fail path below),
    # so the next .path fire retries once the pane goes idle — no mail lost, no wake
    # missed (a newer message / urgent bypass re-evaluates to "wake" on the idle
    # sweep, and condition 5 idle-rekick covers the debounced case). A pane whose
    # main turn has ENDED while background agents run is not busy here: input
    # submits there, and pane_idle_state reads it idle for delivery (T-4284).
    # T-4792: the controller's recorded runtime wins over the static map, so a
    # Claude seat failed over to codex gets the codex profile (and vice versa).
    harness = live_harness.resolve(
        agent, cfg.get("harness", _DEFAULT_HARNESS),
        state_root=live_harness.default_state_root(cfg["home"]),
    )

    def _count_defer() -> None:
        count, alert_now = note_defer(defer_state, names, now)
        if alert_now:
            alert_fn(agent, (
                f"{agent}'s wake has been deferred {count} times in a row "
                f"(~{count * 2} min) for the same {len(names)} unread message(s); the seat "
                f"may be deaf to mail. Pane {cfg['pane_target']}, harness {harness}. "
                f"Look at the pane for a stuck composer or a dialog. (T-4983)"))

    def _recover() -> str:
        def spend_enter(pane_target):
            # T-5047: the record is one-shot. The first Enter attempted spends it, whatever
            # the outcome, so a later human paste in the TTL window is never retried into.
            try:
                inject_record.unlink()
            except OSError:
                pass
            return enter_fn(pane_target)
        return recover_stuck_notice(
            agent, cfg["pane_target"], harness, capture_fn=capture_fn, enter_fn=spend_enter,
            sleep_fn=sleep_fn, record=read_inject_record(inject_record), now=now,
            session=current_session)

    if pane_idle is False:
        # T-4983: a Claude composer holding our own unsubmitted notice reads BUSY (the
        # mode footer plus a non-empty composer is "busy" by design), so the same
        # deadlock reaches Claude seats here. Only that exact shape is recovered; a
        # genuinely busy pane is deferred and, as before, not counted.
        outcome = _recover()
        if outcome in ("failed", "unverified"):  # T-5047: a stuck placeholder counts
            _count_defer()
        print(f"[mailbox_wake] {agent}: {len(names)} unread — deferred, pane busy; "
              f"will retry on next fire ({reason})")
        return 0
    # T-4292: UNKNOWN is not permission. It used to fall through and inject, so a
    # permission dialog could receive the paste plus Enter. Now it needs the
    # harness's positive evidence (see unknown_pane_deliverable). The deferral is
    # the T-2681 one: marker not advanced, mail retained, and the next fire
    # re-evaluates.
    if pane_idle is None and not unknown_pane_ok_fn(cfg["pane_target"], harness):
        # T-4983: our own notice stuck unsubmitted makes every wake land here forever.
        # One Enter on that exact shape, nothing else; then count the deferral so a
        # seat that stays stuck pages someone.
        if _recover() != "recovered":
            _count_defer()
        print(f"[mailbox_wake] {agent}: {len(names)} unread — deferred, pane state "
              f"UNKNOWN (modal, unrecognised screen, or a turn without its sentinel); "
              f"an Enter could confirm a dialog. Will retry on next fire ({reason})")
        return 0

    prompt = build_kick(agent, names, _senders(new_dir, names),
                        _queued_subjects(new_dir, names))
    injected_ok = bool(do_inject(cfg["pane_target"], prompt, harness))
    # T-5047: record the attempt either way; confirmed only when the verified submit
    # succeeded. An unconfirmed one is the positive evidence for a collapsed placeholder.
    write_inject_record(inject_record, now, current_session, injected_ok)
    if injected_ok:
        # Only advance the marker once the kick lands, recording WHICH session got
        # it (T-1901) and when (TTL re-kick basis).
        # idle_kicked records WHY this kick happened, so condition 5 fires once per
        # newest message. Any other wake reason resets it to False — a fresh cause
        # deserves a fresh idle re-kick if that one is swallowed too.
        _write_marker(marker, names[-1], current_session, now,
                      idle_kicked=(reason == IDLE_REKICK_REASON))
        clear_defer_state(defer_state)
        print(f"[mailbox_wake] {agent}: woke pane for {len(names)} unread message(s) ({reason})")
        return 0
    # Kick failed. The pane was NOT confirmed-busy at decide-time (else the T-2681
    # skip above would have deferred). Two sub-cases needing OPPOSITE dispositions:
    #
    #  (a) idle→busy RACE (T-3554): the pane was idle at the decide-time sample but the
    #      agent started a turn before submit-verify confirmed, so send_keys_submit reports
    #      'submit unconfirmed (still pending)' — the composer HOLDS the text because the
    #      pane is now generating. This is the SAME benign busy collision T-2681 defers,
    #      observed one sample too late; it recurs on a busy fleet (recovery + artisan 2026-09-07).
    #  (b) genuine anomaly: an idle pane that rejects the submit, or a dead/unreadable
    #      seat — the dead-seat send-liveness rung (T-2245), worth the OnFailure alarm.
    #
    # RE-VERIFY AT SEND TIME (the T-2681 predicate, one sample later —
    # feedback_reverify_at_send_time_for_self_healing_signals): re-read the pane.
    # Confirmed-busy NOW ⇒ case (a): soft-exit 0 (marker NOT advanced, mail still in new/,
    # next .path fire retries once idle — no mail lost, no wake missed). Idle/unknown NOW ⇒
    # case (b): exit 8 so OnFailure fires. Cannot regress a real wedge: a dead/wedged seat
    # reads idle/unknown (not generating), so it still alarms, and the sustained backstops
    # (T-2158 TTL/idle-transition rekick, T-2157 mail-staleness detector) still catch a
    # genuinely stuck drain.
    #
    # T-4120: sample more than once. In the submit->generate transition the pane is
    # briefly UNCLASSIFIABLE, and a single re-read landing there (None) exited 8 and
    # paged recovery although the kick had landed. Busy on ANY sample is case (a); only
    # idle/unknown on EVERY sample is case (b), so the dead-seat alarm is unchanged.
    for sample in range(RECHECK_SAMPLES):
        if sample:
            time.sleep(RECHECK_INTERVAL_SECS)
        pane_now = pane_idle_fn(cfg["pane_target"])
        if pane_now is False:
            print(f"[mailbox_wake] {agent}: kick failed but pane now BUSY (idle→busy race, "
                  f"T-3554; re-check {sample + 1}/{RECHECK_SAMPLES}) — deferring, mail "
                  f"retained, will retry next fire ({reason})", file=sys.stderr)
            return 0
    print(f"[mailbox_wake] {agent}: wake kick failed on "
          f"{'idle' if pane_now else 'unknown'} pane ({RECHECK_SAMPLES} re-checks, never "
          f"busy); messages remain in new/ for retry", file=sys.stderr)
    # Resolved at call time, not as a def default, so a test's patch of pane_tail holds.
    tail = (pane_tail_fn or pane_tail)(cfg["pane_target"])
    print(f"[mailbox_wake] {agent}: pane bottom at the last re-check "
          f"({len(tail)} non-blank line(s), T-4800):", file=sys.stderr)
    for ln in tail:
        print(f"[mailbox_wake]   | {ln}", file=sys.stderr)
    # T-5002: that capture is one more sample, and it may only ADD busy evidence. On
    # 2026-10-20 05:17:48Z (orchestrator) every -e re-check read UNKNOWN while the plain capture
    # taken the same second showed the spinner of the turn the kick had started, so the
    # kick had landed and the exit 8 was false. Busy here is the T-3554 race: soft-exit 0,
    # marker not advanced, mail retained. Idle, unknown or unreadable keeps the exit 8.
    if tail and _plain_tail_busy(tail):
        print(f"[mailbox_wake] {agent}: but that capture shows a GENERATING pane (T-5002): "
              f"the kick landed; deferring, mail retained, will retry next fire ({reason})",
              file=sys.stderr)
        return 0
    return 8


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Wake an agent on new inter-agent mail (ORG-026).")
    ap.add_argument("--agent", required=True, choices=sorted(AGENT_PANE))
    ap.add_argument("--inbox", default=None, help="override inbox path (default: <home>/inbox)")
    args = ap.parse_args(argv)
    return run(args.agent, inbox=Path(args.inbox) if args.inbox else None)


if __name__ == "__main__":
    raise SystemExit(main())
