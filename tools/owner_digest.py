"""Deliver an unattended tool's full digest to its OWNER's mailbox (T-6370).

The T-6369 pattern, shared: a weekly tool's proof-of-life digest goes to the seat
that owns the tool, never to the operator's chat. The operator's Telegram is reserved for
messages he must act on, and each caller decides that separately (most have none).

    from tools.owner_digest import send_owner_digest
    send_owner_digest(text, sender="artifact-gc", subject="artifact-gc digest")

Raises on a delivery failure (MailboxNotFound, PermissionError, ...): the caller
decides the exit code, so a failed proof-of-life is loud rather than silent.
Suppressed under the test-suite guards, so a suite run never touches the live hub
(the 2026-10-12 ticket_grooming leak).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_OWNER = "recovery"


def digest_owner() -> str:
    return os.environ.get("PANTHEON_DIGEST_OWNER") or DEFAULT_OWNER


def send_owner_digest(text: str, *, sender: str, subject: str,
                      owner: str | None = None) -> str | None:
    """Send `text` to the owner's mailbox; return the message id (None when
    suppressed by a test guard). `subject` gets today's UTC date appended."""
    if os.environ.get("PANTHEON_DISABLE_LIVE_TELEGRAM") or \
            os.environ.get("PANTHEON_DISABLE_LIVE_MAILBOX"):
        print("[mailbox digest suppressed: test guard]", file=sys.stderr)
        return None
    from tools import mailbox as _mailbox
    from tools.agent_role_map import mailbox_hub
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # dedupe_pending=False: each fire's digest is a distinct record, not a
    # re-delivery of a pending one (T-6369 note).
    return _mailbox.send(str(Path(mailbox_hub()) / (owner or digest_owner())), text,
                         sender=sender, subject=f"{subject} {stamp}", dedupe_pending=False)
