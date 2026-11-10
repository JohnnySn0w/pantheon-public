"""Real-state tripwire for the test suite (T-6118 PR-0).

The REAL Chronicle (``~/.local/state/pantheon/chronicle``), REAL persona state
(``~/.local/state/pantheon/personas``), and sender ledger (``sent.jsonl``) must hold only real
activity: the memex export and
the coverage metric read them. Test fixtures that leak there (2026-10-27..11-03: 477 of 513
Chronicle session records on Recovery) corrupt both. tools/conftest.py redirects every writer;
this module is the cheap independent check that the redirect held: snapshot the running
account's real roots at conftest import, re-snapshot at session finish, and fail the session
on any difference.

Own-seat exclusion. A seat that runs the suite from its live pane has a controller and a
Chronicle writer that legitimately touch ITS OWN persona's subtree while the run is going.
That subtree is resolved as ``agent_role_map.account_persona(<account name from the passwd
entry>)`` and handled as follows:
  * ``personas/<own>``: skipped entirely (the live controller writes it constantly).
  * ``chronicle/<own>``: skipped EXCEPT session records whose ``handoff_file`` is under
    ``/tmp/``. A real session's handoff lives in the account home; a fixture's lives in a
    TemporaryDirectory. Those records are the leak signature (Recovery's 477), so they stay guarded
    even in the own subtree.
Every other persona (a fixture ``recovery`` under suborch2) stays fully guarded. If the account
cannot be resolved to a persona, nothing is excluded (fail closed: a false positive is
loud, a missed leak is silent).

The memex export tree (T-6118 PR-2). ``/var/lib/pantheon/chronicle-export`` is a REAL host
path that session-end hardlinks into, and conftest points every resolver at an absent tmp path.
The tripwire snapshots the real ``chronicle_export.DEFAULT_EXPORT_ROOT`` constant (never the
env-resolved root, which conftest redirects) and fails the run on any change, exempting the
running account's OWN seat dirs (a live seat's own session end writes there while the suite runs)
and ``.ingested`` (archivist's receipts). An absent or unreadable root is an empty snapshot.

The home is always the passwd entry of the running uid, never ``$HOME`` (a test run may have
HOME pointed elsewhere) and never another account's home.
"""
from __future__ import annotations

import json
import os
import pwd
from pathlib import Path
from typing import Iterable

#: The tmp-handoff leak signature lives in this file kind (tools/session_chronicle.py).
SESSION_JSON_SUFFIX = ".session.json"
TMP_PREFIX = "/tmp/"

Snapshot = dict[str, tuple[int | None, int | None]]


def real_home() -> Path:
    """The running uid's home from the passwd database (not $HOME, not another account)."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def own_export_seats(persona: str | None = None) -> tuple[str, ...]:
    """The export seat dirs the running account legitimately writes: its persona and, for a
    codex account, the appserver seat name (consultant). Plus archivist's ``.ingested``."""
    names: list[str] = [".ingested"]
    own = persona if persona is not None else own_persona()
    if own:
        names.append(own)
    try:
        from tools.appserver_seats import appserver_seat_for_account

        repo = Path(__file__).resolve().parent.parent
        names.append(appserver_seat_for_account(repo, pwd.getpwuid(os.getuid()).pw_name))
    except Exception:  # noqa: BLE001 -- no appserver seat for this account (or unreadable): nothing more
        pass
    return tuple(dict.fromkeys(names))


def real_roots(home: Path | None = None) -> dict[str, Path]:
    base = (home or real_home()) / ".local" / "state" / "pantheon"
    return {
        "chronicle": base / "chronicle",
        "personas": base / "personas",
        "sent": base / "sent.jsonl",
    }


def own_persona() -> str | None:
    """The persona resident on the running account, or None when it cannot be resolved."""
    try:
        from tools.agent_role_map import account_persona

        return account_persona(pwd.getpwuid(os.getuid()).pw_name)
    except Exception:  # noqa: BLE001 -- the guard must never break collection
        return None


def _is_tmp_handoff_session(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    handoff = data.get("handoff_file") if isinstance(data, dict) else None
    return isinstance(handoff, str) and handoff.startswith(TMP_PREFIX)


def snapshot_root(root: Path, *, skip: Iterable[str] = (), signature_only: Iterable[str] = ()) -> Snapshot:
    """Relative path -> (mtime_ns, size) for files, (None, None) for directories.

    ``skip`` names top-level children to ignore wholesale; ``signature_only`` names top-level
    children where only tmp-handoff ``*.session.json`` files are recorded. Symlinks are
    recorded by lstat and never followed. An absent root gives an empty snapshot."""
    out: Snapshot = {}
    skip_set, sig_set = set(skip), set(signature_only)
    if not root.is_dir() or root.is_symlink():
        return out
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root)
        top = rel_dir.parts[0] if rel_dir.parts else None
        if rel_dir == Path("."):
            dirnames[:] = [d for d in dirnames if d not in skip_set]
            filenames = [f for f in filenames if f not in skip_set]
        sig = top in sig_set
        if rel_dir != Path(".") and not sig:
            out[str(rel_dir)] = (None, None)
        for name in filenames:
            path = Path(dirpath) / name
            if sig and not (name.endswith(SESSION_JSON_SUFFIX) and _is_tmp_handoff_session(path)):
                continue
            try:
                st = os.lstat(path)
            except OSError:
                continue
            out[str(path.relative_to(root))] = (st.st_mtime_ns, st.st_size)
    return out


def snapshot_file(path: Path) -> Snapshot:
    """Snapshot one exact path by lstat, without following a symlink."""
    try:
        st = os.lstat(path)
    except OSError:
        return {}
    return {path.name: (st.st_mtime_ns, st.st_size)}


def take_snapshot(home: Path | None = None, persona: str | None = None,
                  export_root: Path | None = None) -> dict[str, Snapshot]:
    """Snapshot the real roots, applying the own-seat exclusion for ``persona``.
    ``export_root`` defaults to the REAL chronicle_export.DEFAULT_EXPORT_ROOT (tests pass a tmp one)."""
    roots = real_roots(home)
    own = persona if persona is not None else own_persona()
    own_skip = (own,) if own else ()
    try:
        from tools.chronicle_export import DEFAULT_EXPORT_ROOT

        export = snapshot_root(export_root or DEFAULT_EXPORT_ROOT, skip=own_export_seats(own))
    except Exception:  # noqa: BLE001 -- absent or unreadable root: an empty snapshot
        export = {}
    return {
        "personas": snapshot_root(roots["personas"], skip=own_skip),
        "chronicle": snapshot_root(roots["chronicle"], signature_only=own_skip),
        "export": export,
        "sent": snapshot_file(roots["sent"]),
    }


def diff(before: dict[str, Snapshot], after: dict[str, Snapshot]) -> list[str]:
    """Human-readable added/changed/removed lines, empty when nothing differs."""
    lines: list[str] = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name, {}), after.get(name, {})
        for rel in sorted(set(new) - set(old)):
            lines.append(f"added    {name}/{rel}")
        for rel in sorted(set(old) & set(new)):
            if old[rel] != new[rel]:
                lines.append(f"changed  {name}/{rel}")
        for rel in sorted(set(old) - set(new)):
            lines.append(f"removed  {name}/{rel}")
    return lines
