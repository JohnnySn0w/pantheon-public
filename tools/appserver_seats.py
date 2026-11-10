"""tools/appserver_seats.py: the seat-name lookup over harness/codex/appserver_seats.json (T-6118 PR-2).

A Codex seat is known by its appserver_seats.json key ("consultant"), not by the persona that runs it
("codex"). Two callers need that mapping: tools/persona_installer.py (the fleet-usage timer instance)
and tools/chronicle_export.py (the memex export seat dir). Pure and light: stdlib only, so the
session-end path can import it without pulling persona_installer in.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

APPSERVER_SEATS = Path("harness") / "codex" / "appserver_seats.json"
#: A seat name becomes a directory name and a systemd timer instance, so it stays this shape.
SEAT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


class AppserverSeatsError(Exception):
    """The seats file is unreadable, malformed, or does not name exactly one seat for the account.

    ``kind`` is 'unreadable', 'malformed' or 'count'; ``matches`` lists the seat names found
    (kind 'count'), so a caller that treats zero matches as 'no appserver seat' can tell it
    apart from an ambiguous account."""

    def __init__(self, message: str, *, kind: str, matches: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.kind = kind
        self.matches = matches


def appserver_seat_for_account(repo_root: Path, account: str) -> str:
    """The one appserver_seats.json key whose ``account`` is ``account``.

    Raises AppserverSeatsError when the file cannot be read, is not a seats mapping, or does not
    hold exactly one valid-named seat for the account."""
    try:
        seats = json.loads((Path(repo_root) / APPSERVER_SEATS).read_text(encoding="utf-8"))["seats"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AppserverSeatsError(f"cannot read {APPSERVER_SEATS}: {exc}", kind="unreadable") from None
    if not isinstance(seats, dict):
        raise AppserverSeatsError(
            f"{APPSERVER_SEATS}: seats must be a mapping, got {type(seats).__name__}", kind="malformed")
    names = sorted(
        name for name, seat in seats.items()
        if isinstance(seat, dict) and seat.get("account") == account
    )
    if len(names) != 1 or not SEAT_NAME_RE.match(names[0]):
        raise AppserverSeatsError(
            f"{APPSERVER_SEATS}: expected exactly one seat for account {account!r}, found {names}",
            kind="count", matches=tuple(names))
    return names[0]
