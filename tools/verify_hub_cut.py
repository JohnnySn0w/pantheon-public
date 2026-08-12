#!/usr/bin/env python3
"""Post-cut gate for the T-2156 hub rename: the OLD path must not survive as a
well-formed maildir (T-2295).

WHY THIS EXISTS AND WHY IT IS A GATE, NOT A NICETY. The T-2156 cut has two
possible shapes and only one of them fails loudly:

  SHAPE A -- old path GONE. consume() returns {'skipped': True, 'reason':
    'inbox_missing'} and send() raises MailboxNotFound. Anything left pointing at
    the old hub says so, immediately. This is T-2216's fix working.

  SHAPE B -- old path SURVIVES as a well-formed maildir (a leftover dir, a compat
    symlink, a copy-then-switch -- anything leaving tmp/+new/+cur/). consume()
    returns {'skipped': False, 'processed': [], ...} with NO 'reason' key, which
    is BYTE-IDENTICAL to a healthy quiet inbox, and send() returns a normal
    message id into a directory nothing watches. Silent on both halves, forever.

Under Shape B every cheap proxy agrees with health -- unit active, send returns
an id, inbox reads "empty", directory exists -- which is exactly the lie
mailbox.exists()'s own docstring warns about. So the cut cannot be verified by
looking at the NEW hub; it has to be verified by proving the OLD one is dead.

A compat symlink is the specific trap: it is the obvious kindness, and it is what
produces Shape B. If a grace period is genuinely wanted, make the old path
NON-maildir (remove or rename one of tmp/new/cur) rather than symlinking it.

Usage:
    python3 tools/verify_hub_cut.py --old-hub /home/the-ancestor/mailbox
    python3 tools/verify_hub_cut.py --old-hub <path> --inbox orchestrator --inbox specialist

Exit 0 = the old hub is provably dead. Exit 1 = at least one Shape B survivor.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import mailbox as _mailbox  # noqa: E402  (needs sys.path above)

# The nine inboxes that existed at the hub pre-cut. EIGHT from §1 of the rename
# plan plus specialist-a2a, which §4g found appears in neither §1's "Inboxes: 8" nor
# the plan's original literal inventory -- it is specialist_seat_server.py:169's
# DEFAULT_RESULT_INBOX and it is live. Listed explicitly rather than discovered by
# listing the hub: the hub is traverse-only, so a listing returns EMPTY rather
# than erroring, which already produced one false negative while drafting the
# plan (precondition (a)).
DEFAULT_INBOXES = (
    "orchestrator", "specialist", "specialist-a2a", "recovery", "codex",
    "mnemosyne", "metis", "atlas", "the-ancestor",
)


def survivors(old_hub: Path, inboxes: tuple[str, ...] = DEFAULT_INBOXES) -> list[str]:
    """Names of inboxes still well-formed under `old_hub` (i.e. Shape B).

    Uses mailbox.exists(), which is the DEFINING surface rather than a proxy: it
    is true iff all of tmp/, new/, cur/ are dirs -- precisely the condition that
    makes a leftover silent. A bare directory, or a partially-cleaned one, is
    correctly NOT a survivor: it cannot swallow mail.
    """
    return [name for name in inboxes if _mailbox.exists(old_hub / name)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Assert the pre-rename hub no longer holds any well-formed maildir (T-2295).")
    ap.add_argument("--old-hub", required=True, type=Path,
                    help="the PRE-cut hub root, e.g. /home/the-ancestor/mailbox")
    ap.add_argument("--inbox", action="append", dest="inboxes", default=None,
                    help="inbox name to check (repeatable); defaults to all nine")
    args = ap.parse_args(argv)

    names = tuple(args.inboxes) if args.inboxes else DEFAULT_INBOXES
    found = survivors(args.old_hub, names)

    if found:
        print(f"FAIL: {len(found)} of {len(names)} old-hub paths are STILL well-formed "
              f"maildirs under {args.old_hub}:", file=sys.stderr)
        for name in found:
            print(f"  - {args.old_hub / name}", file=sys.stderr)
        print("\nThis is SHAPE B. A stale reference to any of these will send and consume "
              "SILENTLY and forever: consume() is byte-identical to a healthy quiet inbox "
              "and send() returns a normal id into a directory nothing watches. Remove them, "
              "or make them non-maildir. Do NOT leave a compat symlink.", file=sys.stderr)
        return 1

    print(f"OK: none of the {len(names)} old-hub paths under {args.old_hub} is a "
          f"well-formed maildir — a stale reference will now fail LOUDLY (Shape A).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
