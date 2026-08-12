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

T-1901 (the Metis first-retask hole): the debounce must also NOT hold when the kick's
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
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Per-agent substrate (pane + owning account). Kept as a small explicit map rather
# than importing inbox_consumer — that module carries live-Telegram/tmux side-effect
# surface we must not pull into a oneshot on the real-token host (the full-suite leak
# lesson). Values mirror inbox_consumer._AGENT_HEALTH_CONFIG.
AGENT_PANE = {
    "orchestrator": {"pane_target": "claude-telegram-bot:0", "home": "/home/claude"},
    "specialist": {"pane_target": "claude-research-bot:0", "home": "/home/claude-research"},
    "recovery": {"pane_target": "claude-telegram-bot-fallback:0", "home": "/home/claude-fallback"},
    # T-1896 (ORG-043): sub-orchestrator seats (Metis/Atlas). Standing sessions
    # are plain claude TUIs in tmux on their own accounts - no telegram plugin.
    "metis": {"pane_target": "suborch1-main:0", "home": "/home/suborch1"},
    "atlas": {"pane_target": "suborch2-main:0", "home": "/home/suborch2"},
    # codex (T-1041 mail-comms): an OpenAI codex-CLI agent, not Claude Code. Its
    # tmux session is "codex-session" and a separate Enter submits a turn (same as
    # its telegram poller's proven inject path). T-1106: it MUST pass
    # `harness: codex` so send_keys_submit verifies against the codex profile —
    # the claude-code "[Pasted text" sentinel is always-absent on codex's TUI, so
    # the default profile FALSE-PASSES (reports submitted without confirming;
    # same class as the T-1054 poller fix). The codex profile checks codex's
    # composer-holds-text predicate instead.
    "codex": {"pane_target": "codex-session", "home": "/home/codex", "harness": "codex"},
    # the-ancestor (T-1124 / Veneration, T-0765): the channel-less standing claude
    # session that consumes its mailbox in-session (pane-inject, like the agents).
    # Confirmed pane-inject model from the daemon source (a standing `claude` tmux
    # session), NOT the legacy programmatic ancestor_consume.py drain. Claude-Code
    # harness (default), so no explicit `harness` key needed.
    "the-ancestor": {"pane_target": "ancestor-daemon:0", "home": "/home/the-ancestor"},
    # mnemosyne (T-1824 / ORG-042 second head): standing telegram daemon under the
    # `exocortex` user, same supervised-tmux shape as recovery. Claude-Code harness.
    # NOTE: her wake/inbox-consumer timers install in HER user manager during her
    # harness bringup; this entry only readies hub-side wake config.
    "mnemosyne": {"pane_target": "claude-telegram-bot-exocortex:0", "home": "/home/exocortex"},
}
# Default harness for agents that don't specify one (the Claude-Code personas).
_DEFAULT_HARNESS = "claude-code"

_SEND_KEYS_HELPER = Path(__file__).resolve().parent / "send_keys_submit.sh"
_MARKER_NAME = ".wake_marker"

# Path A (2026-06-09): inboxes live in the neutral group-accessible hub under
# /home/the-ancestor (ORG-026 "Conditionally a shared HOME"), NOT each agent's own
# mode-750 home — a private home can't be traversed cross-account to deliver. Each
# persona's inbox is MAILBOX_HUB/<persona>. (`home` in AGENT_PANE is now only the
# pane-account hint; it no longer locates the inbox.)
MAILBOX_HUB = Path("/home/the-ancestor/mailbox")


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
    T-2384: mnemosyne's seat took 242 consecutive "1 unread from inbox_consumer"
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
            ["tmux", "capture-pane", "-p", "-t", pane_target],
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
    per-substrate for free: codex's different TUI and mnemosyne's absent pane
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
    """`sender: subject` for each queued message, oldest first (T-2266, mnemosyne).

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
        # codex runs sandboxed (workspace-write) and its sandbox BLOCKS `sg`
        # (setgroups/setgid denied). A restarted codex session holds the
        # pantheon-mailbox group natively, so run the drain bare — no sg wrapper.
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
        f"You have {n} unread inter-agent message{plural} in your mailbox (from {who}). "
        f"{queued}"
        f"Drain them: `{drain_cmd}` "
        f"to inspect+ack, or read {tools_dir}/mailbox.py and consume with a handler that acts on each payload. "
        f"Your inbox is {inbox} (the shared /home/the-ancestor hub, ORG-026 Path A). "
        f"{grp_note}"
        f"Messages stay in new/ until you consume them, so nothing is lost if you finish your current turn first."
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
def run(agent: str, *, inbox: Path | None = None, do_inject=inject,
        session_key_fn=session_key, now: float | None = None,
        pane_idle_fn=pane_is_idle) -> int:
    cfg = AGENT_PANE.get(agent)
    if cfg is None:
        print(f"[mailbox_wake] unknown agent {agent!r} (known: {list(AGENT_PANE)})", file=sys.stderr)
        return 64
    box = Path(inbox) if inbox else MAILBOX_HUB / agent
    new_dir, marker = box / "new", box / _MARKER_NAME
    now = time.time() if now is None else now
    current_session = session_key_fn(cfg["pane_target"])  # None → legacy fallback
    pane_idle = pane_idle_fn(cfg["pane_target"])           # None → unknown, never wakes

    action, names, reason = decide(new_dir, marker, current_session, now, pane_idle)
    if action == "empty":
        print(f"[mailbox_wake] {agent}: inbox/new empty — nothing to wake")
        return 0
    if action == "debounced":
        print(f"[mailbox_wake] {agent}: {len(names)} unread — debounced ({reason})")
        return 0

    # T-2681: a CONFIRMED-BUSY pane (mid-turn) cannot accept a submit — an inject
    # there fails 'submit unconfirmed' and systemd records a FALSE unit failure
    # (status=8). Waking a busy agent is also pointless: it is already active and
    # drains its own mail. So DEFER on a confirmed-busy pane instead of attempting a
    # doomed inject. The marker is NOT advanced (identical to the fail path below),
    # so the next .path fire retries once the pane goes idle — no mail lost, no wake
    # missed (a newer message / urgent bypass re-evaluates to "wake" on the idle
    # sweep, and condition 5 idle-rekick covers the debounced case). Idle/unknown
    # panes fall through and inject as before.
    if pane_idle is False:
        print(f"[mailbox_wake] {agent}: {len(names)} unread — deferred, pane busy; "
              f"will retry on next fire ({reason})")
        return 0

    prompt = build_kick(agent, names, _senders(new_dir, names),
                        _queued_subjects(new_dir, names))
    harness = cfg.get("harness", _DEFAULT_HARNESS)
    if do_inject(cfg["pane_target"], prompt, harness):
        # Only advance the marker once the kick lands, recording WHICH session got
        # it (T-1901) and when (TTL re-kick basis).
        # idle_kicked records WHY this kick happened, so condition 5 fires once per
        # newest message. Any other wake reason resets it to False — a fresh cause
        # deserves a fresh idle re-kick if that one is swallowed too.
        _write_marker(marker, names[-1], current_session, now,
                      idle_kicked=(reason == IDLE_REKICK_REASON))
        print(f"[mailbox_wake] {agent}: woke pane for {len(names)} unread message(s) ({reason})")
        return 0
    # Kick failed on a NON-busy pane (idle, or unknown/dead). Do NOT advance the
    # marker, so the next .path fire retries; messages are still in new/ (never acked
    # here), so nothing is lost. Return 8 so the unit's OnFailure fires — this is the
    # dead-seat send-liveness rung (T-2245): an idle pane that rejects the submit, or
    # a target with no live session, is a genuine anomaly worth surfacing. (The benign
    # busy-pane collision is already handled by the confirmed-busy skip above, T-2681.)
    print(f"[mailbox_wake] {agent}: wake kick failed; messages remain in new/ for retry", file=sys.stderr)
    return 8


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Wake an agent on new inter-agent mail (ORG-026).")
    ap.add_argument("--agent", required=True, choices=sorted(AGENT_PANE))
    ap.add_argument("--inbox", default=None, help="override inbox path (default: <home>/inbox)")
    args = ap.parse_args(argv)
    return run(args.agent, inbox=Path(args.inbox) if args.inbox else None)


if __name__ == "__main__":
    raise SystemExit(main())
