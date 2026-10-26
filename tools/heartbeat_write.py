#!/usr/bin/env python3
"""Write a per-agent liveness heartbeat file (ORG-048 D2a).

Each standing Claude agent calls this from a Stop-hook to record that it just
took a turn. ORG-048's out-of-band Codex watcher polls these files to detect
the correlated-cap signature — a whole-subscription Claude cap freezes every
standing agent's heartbeat at once, which is the positive, Claude-independent
tell the watcher needs (see architecture/adr/ORG-048-codex-out-of-band-fleet-watcher.md D2).

Stdlib-only by design: this runs inside a Stop-hook on every turn, so it must
stay cheap, dependency-free, and reliable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from tools.agent_role_map import AGENT_ROLE_MAP, account_persona

SCHEMA_VERSION = 1


class HeartbeatWriteError(RuntimeError):
    """The heartbeat could not be written — resolution or IO failure."""


def _default_heartbeat_dir() -> Path:
    return Path.home() / ".local" / "state" / "pantheon" / "heartbeat"


def resolve_persona(agent: str | None, *, user: str | None) -> str:
    """Resolve the persona to heartbeat as.

    An explicit ``agent`` (``--agent``) wins if given, and must be a known
    persona (a key of AGENT_ROLE_MAP). Otherwise the persona is derived from
    ``user`` (normally ``$USER``) via agent_role_map.account_persona — the
    single source of truth for OS account -> persona; this does not re-derive
    a second copy of that mapping. Raises HeartbeatWriteError, never guesses,
    when resolution fails: a hook that cannot identify its agent must not
    silently write a wrong-named heartbeat.
    """
    if agent is not None:
        if agent not in AGENT_ROLE_MAP:
            raise HeartbeatWriteError(
                f"unknown --agent {agent!r}; known personas: {sorted(AGENT_ROLE_MAP)}"
            )
        return agent
    if not user:
        raise HeartbeatWriteError("cannot resolve persona: $USER is unset and no --agent given")
    persona = account_persona(user)
    if persona is None:
        raise HeartbeatWriteError(
            f"cannot resolve persona for unmapped $USER={user!r}; pass --agent explicitly"
        )
    return persona


def build_heartbeat(persona: str, *, source: str, now: datetime, pid: int) -> dict[str, object]:
    """Return the heartbeat payload — the exact 6-key schema D2a defines."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    return {
        "agent": persona,
        "ts": now.isoformat().replace("+00:00", "Z"),
        "epoch": int(now.timestamp()),
        "pid": pid,
        "source": source,
        "schema_version": SCHEMA_VERSION,
    }


def _atomic_write_json(path: Path, payload: dict[str, object], *, mode: int | None = None) -> None:
    """Write *payload* to *path* atomically: temp file in the same dir, then
    ``os.replace``. Never leaves a partial target file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:  # explicit mode (e.g. 0644 for a cross-account readable heartbeat)
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def write_heartbeat(
    *,
    agent: str | None,
    source: str,
    heartbeat_dir: Path,
    user: str | None,
    now: datetime,
    pid: int,
) -> Path:
    """Resolve the persona and atomically write its heartbeat file.

    Returns the path written. Raises HeartbeatWriteError (writing nothing) if
    the persona cannot be resolved.
    """
    persona = resolve_persona(agent, user=user)
    payload = build_heartbeat(persona, source=source, now=now, pid=pid)
    path = heartbeat_dir / f"{persona}.json"
    _atomic_write_json(path, payload)
    return path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", default=None, help="explicit persona; skips $USER resolution")
    parser.add_argument(
        "--source", default="manual", help="caller tag, e.g. 'stop-hook' (default: manual)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        write_heartbeat(
            agent=args.agent,
            source=args.source,
            heartbeat_dir=_default_heartbeat_dir(),
            user=os.environ.get("USER"),
            now=datetime.now(timezone.utc),
            pid=os.getpid(),
        )
    except HeartbeatWriteError as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
