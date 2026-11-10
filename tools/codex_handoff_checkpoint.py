#!/usr/bin/env python3
"""codex_handoff_checkpoint.py: ask a codex seat to write a fresh HANDOFF at an idle checkpoint (T-6115).

Intent: the org record (HANDOFF, then Chronicle), not the codex thread rollout, is a codex
seat's durable memory, so threads can be bounded and rotated without losing context. Nothing
asked consultant to write a HANDOFF (stale since 2026-10-17); watchman's was fresh only because
``harness.py restart`` asks first.

Run AS the seat's account, from its own clone (homes are 0750). The prompt goes to the seat's
OWN mailbox, which its app-server delivery path drains at idle, so nothing is typed into a
live pane. The ack is the HANDOFF mtime moving (``handoff_first.request_handoff``, the same
witness ``harness.py restart`` uses).

    sudo -n -u codex sg pantheon-mailbox -c \
      'python3 ~/pantheon-config/tools/codex_handoff_checkpoint.py --persona codex'

Exit: 0 written or already fresh; 3 no ack in time (soft: the seat may be busy); 2 usage.
"""
from __future__ import annotations

import argparse
import os
import pwd
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import handoff_first  # noqa: E402

MAILBOX_ROOT = Path("/home/post-office/mailbox")
# persona -> its own inbox name (consultant's persona key is 'codex').
SEAT_INBOX = {"codex": "codex", "watchman": "watchman"}
DEFAULT_MAX_AGE_HOURS = 24.0
DEFAULT_ACK_TIMEOUT_SECONDS = 20 * 60.0
CHECKPOINT_SUBJECT = "HANDOFF checkpoint (T-6115): please refresh your HANDOFF"
CHECKPOINT_PROMPT = (
    "Routine HANDOFF checkpoint (T-6115); nothing is ending. Rewrite {path} now with your role, "
    "current state, in-flight work, open items (ticket ids) and next actions, so a fresh thread "
    "could resume from it alone. Rewrite it even if nothing changed: the checkpoint waits for the "
    "file's modification time. Then carry on with your work."
)


@dataclass(frozen=True)
class Outcome:
    status: str  # fresh | written | no-ack
    reason: str
    exit_code: int


def checkpoint(*, persona: str, home: Path, send: Callable[[str, str, str], object],
               now: float | None = None, max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
               ack_timeout_seconds: float = DEFAULT_ACK_TIMEOUT_SECONDS,
               sleep: Callable[[float], None] = time.sleep,
               monotonic: Callable[[], float] = time.monotonic) -> Outcome:
    if persona not in SEAT_INBOX:
        raise ValueError(f"not a codex seat persona: {persona!r} (known: {sorted(SEAT_INBOX)})")
    handoff = home / "HANDOFF.md"
    now = time.time() if now is None else now
    try:
        age_h = (now - handoff.stat().st_mtime) / 3600.0
    except OSError:
        age_h = None
    if age_h is not None and age_h < max_age_hours:
        return Outcome("fresh", f"{handoff} is {age_h:.1f}h old (< {max_age_hours:g}h)", 0)
    inbox = str(MAILBOX_ROOT / SEAT_INBOX[persona])
    result = handoff_first.request_handoff(
        handoff, lambda text: send(inbox, text, CHECKPOINT_SUBJECT),
        timeout_seconds=ack_timeout_seconds, sleep=sleep, monotonic=monotonic,
        prompt=CHECKPOINT_PROMPT)
    if result.confirmed:
        return Outcome("written", result.reason, 0)
    return Outcome("no-ack", result.reason, 3)


def _mailbox_send(inbox: str, payload: str, subject: str) -> object:
    from tools import mailbox as _mb  # package import: never the stdlib `mailbox` module
    return _mb.send(inbox, payload, "checkpoint", subject=subject)


def account_home() -> Path:
    """The effective account's home from the passwd db: under `sudo -u`, $HOME can still be
    the caller's, and Path.home() would then check the wrong HANDOFF (review note)."""
    return Path(pwd.getpwuid(os.geteuid()).pw_dir)


class PersonaError(ValueError):
    pass


def current_account() -> str:
    return pwd.getpwuid(os.geteuid()).pw_name


def resolve_persona(explicit: str | None, account: str | None = None) -> str:
    """The codex seat persona for the running account (T-6125). The units are instanced on the
    ACCOUNT (codex, codex-fallback); instance.yaml (agent_role_map) maps it to the persona
    (codex, watchman), so no persona is hard-coded in a unit. An explicit --persona is
    accepted only when it agrees with the account: a process may not act for another seat."""
    from tools import agent_role_map

    account = account if account is not None else current_account()
    derived = agent_role_map.account_persona(account)
    if derived not in SEAT_INBOX:
        raise PersonaError(f"account {account!r} is not a codex seat account (persona {derived!r})")
    if explicit is not None and explicit != derived:
        raise PersonaError(f"--persona {explicit!r} disagrees with account {account!r} (persona {derived!r})")
    return derived


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--persona", default=None, choices=sorted(SEAT_INBOX),
                   help="optional; derived from the running account, and must agree with it")
    p.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    p.add_argument("--ack-timeout-seconds", type=float, default=DEFAULT_ACK_TIMEOUT_SECONDS)
    a = p.parse_args(argv)
    try:
        a.persona = resolve_persona(a.persona)
    except PersonaError as exc:
        p.error(str(exc))
    out = checkpoint(persona=a.persona, home=account_home(), send=_mailbox_send,
                     max_age_hours=a.max_age_hours, ack_timeout_seconds=a.ack_timeout_seconds)
    print(f"[codex_handoff_checkpoint persona={a.persona}] {out.status}: {out.reason}")
    return out.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
