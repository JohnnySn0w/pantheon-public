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
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Per-agent substrate (pane + owning account). Kept as a small explicit map rather
# than importing inbox_consumer — that module carries live-Telegram/tmux side-effect
# surface we must not pull into a oneshot on the real-token host (the full-suite leak
# lesson). Values mirror inbox_consumer._AGENT_HEALTH_CONFIG.
AGENT_PANE = {
    "orchestrator": {"pane_target": "claude-telegram-bot:0", "home": "/home/claude"},
    "specialist": {"pane_target": "claude-research-bot:0", "home": "/home/claude-research"},
    "recovery": {"pane_target": "claude-telegram-bot-fallback:0", "home": "/home/claude-fallback"},
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
    in the inbox root anyway, not in new/). Missing new/ → no messages."""
    try:
        return sorted(p.name for p in new_dir.iterdir() if p.is_file() and not p.name.startswith("."))
    except FileNotFoundError:
        return []


def _read_marker(marker: Path) -> str | None:
    try:
        return marker.read_text(encoding="utf-8").strip() or None
    except (FileNotFoundError, OSError):
        return None


def _write_marker(marker: Path, newest: str) -> None:
    marker.write_text(newest + "\n", encoding="utf-8")


def decide(new_dir: Path, marker: Path) -> tuple[str, list[str]]:
    """Pure decision (no injection, no marker write): return (action, names).

    action ∈ {"empty", "debounced", "wake"}:
      empty     — nothing in new/; do nothing.
      debounced — newest message == last-kicked marker; already nudged, agent hasn't
                  drained yet, don't re-spam the pane.
      wake      — a genuinely newer message is present; inject a wake kick.
    """
    names = peek(new_dir)
    if not names:
        return ("empty", names)
    if _read_marker(marker) == names[-1]:
        return ("debounced", names)
    return ("wake", names)


# --------------------------------------------------------------------------- senders
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


def build_kick(agent: str, names: list[str], senders: list[str]) -> str:
    who = ", ".join(senders) if senders else "another agent"
    n = len(names)
    plural = "s" if n != 1 else ""
    inbox = MAILBOX_HUB / agent  # Path A: the hub subdir, NOT ~/inbox (which no longer exists)
    # Absolute tools path (T-1041): a relative `sys.path.insert(0,'tools')` only
    # resolves when cwd == the repo root. codex's cwd is its home (repo is a
    # subdir), so the relative form silently imported the stdlib `mailbox` (no
    # consume()). An absolute path derived from the agent's home is correct for
    # every agent regardless of cwd.
    tools_dir = f"{AGENT_PANE[agent]['home']}/pantheon-config/tools"
    drain_cmd = (
        f"python3 -c \"import sys; sys.path.insert(0,'{tools_dir}'); import mailbox; "
        f"print(mailbox.consume('{inbox}', lambda m: print(m['sender'], m['subject'], m['payload'])))\""
    )
    if agent == "codex":
        # codex runs sandboxed (workspace-write) and its sandbox BLOCKS `sg`
        # (setgroups/setgid denied). A restarted codex session holds the
        # veneration group natively, so run the drain bare — no sg wrapper.
        grp_note = (
            "Run it bare — your session holds the veneration group natively; codex's sandbox "
            "blocks `sg` and it isn't needed. (If you hit a write/permission error on the hub, "
            "the hub must be in your config.toml [sandbox_workspace_write] writable_roots.) "
        )
    else:
        grp_note = (
            "NOTE: if a bare run hits PermissionError on the hub, wrap it `sg veneration -c '<the "
            "python command above>'` — a live session-process that predates the veneration-group "
            "grant needs it (T-1016 / a user-manager restart removes this need). "
        )
    return (
        f"You have {n} unread inter-agent message{plural} in your mailbox (from {who}). "
        f"Drain them: `{drain_cmd}` "
        f"to inspect+ack, or read {tools_dir}/mailbox.py and consume with a handler that acts on each payload. "
        f"Your inbox is {inbox} (the shared /home/the-ancestor hub, ORG-026 Path A). "
        f"{grp_note}"
        f"Messages stay in new/ until you consume them, so nothing is lost if you finish your current turn first."
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
def run(agent: str, *, inbox: Path | None = None, do_inject=inject) -> int:
    cfg = AGENT_PANE.get(agent)
    if cfg is None:
        print(f"[mailbox_wake] unknown agent {agent!r} (known: {list(AGENT_PANE)})", file=sys.stderr)
        return 64
    box = Path(inbox) if inbox else MAILBOX_HUB / agent
    new_dir, marker = box / "new", box / _MARKER_NAME

    action, names = decide(new_dir, marker)
    if action == "empty":
        print(f"[mailbox_wake] {agent}: inbox/new empty — nothing to wake")
        return 0
    if action == "debounced":
        print(f"[mailbox_wake] {agent}: {len(names)} unread, newest unchanged — debounced (no re-kick)")
        return 0

    prompt = build_kick(agent, names, _senders(new_dir, names))
    harness = cfg.get("harness", _DEFAULT_HARNESS)
    if do_inject(cfg["pane_target"], prompt, harness):
        _write_marker(marker, names[-1])  # only advance the marker once the kick lands
        print(f"[mailbox_wake] {agent}: woke pane for {len(names)} unread message(s)")
        return 0
    # Kick failed → do NOT advance the marker, so the next .path fire retries. Messages
    # are still in new/ (never acked here), so nothing is lost.
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
