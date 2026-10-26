#!/usr/bin/env python3
"""tools/seat_sleep.py - ORG-106 L3a (T-5526): the launch-side enforcement of DEEP-SLEEP.

One shared predicate answers "may a session be launched for this seat?" so every
launcher (runtime_controller's chokepoint, consume, the shell launchers via
`seat_state.py launch-check`) asks the same question:

  * ``seat-sleep``: an unexpired deep-sleep record (phase draining OR asleep).
    REFUSED at every launch seam.
  * ``oom-hold``: the ORG-104 hold. NOT refused here (ORG-104 rule 2 is enforced at
    consume through maintenance; T-5526 ruling, D6): it is OBSERVED and a successful
    launch under it pages as ``launch_breach`` (`note_launch_breach`).

FAIL-OPEN: any fault in the reader yields "not blocked" plus the fault text, never a
raise (ORG-106 section 6). This module also owns the system-orchestrator hold-down flag
removal (`/var/lib/pantheon/oom-hold/<persona>`, ORG-104 rule 3) used by wake, the
paused-intake downgrade and the janitor's expiry. L3b adds the writers.

Stdlib + repo modules only.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import harness_state as hs  # noqa: E402
from tools import seat_state  # noqa: E402

CAUSE_SLEEP = "seat-sleep"
CAUSE_OOM = "oom-hold"

#: `seat_state.py launch-check` exit codes. 2 is already "invalid persona".
EXIT_ALLOWED = 0
EXIT_BLOCKED_SLEEP = 3

DEFAULT_HOLD_DIR = Path("/var/lib/pantheon/oom-hold")


@dataclass(frozen=True)
class LaunchBlock:
    """Result of `launch_block`. ``cause`` is None when a launch is allowed."""

    cause: str | None = None
    oom_held: bool = False
    fault: str | None = None
    owner: str | None = None
    reason: str | None = None
    expires: str | None = None

    @property
    def sleeping(self) -> bool:
        return self.cause == CAUSE_SLEEP


def seat_root_for(personas_dir: Path | None) -> Path | None:
    """The seat-state root (parent of ``personas/``) for a runtime plane's personas dir.
    None means "use the environment default", which `seat_state.effective` resolves."""
    return Path(personas_dir).parent if personas_dir is not None else None


def launch_block(
    persona: str,
    *,
    root: Path | None = None,
    now: datetime | None = None,
    oom_holds_path: Any = None,
    reader: Callable[..., Any] | None = None,
) -> LaunchBlock:
    """May a session be launched for `persona`? Never raises (fail-open)."""
    try:
        read = reader or seat_state.effective
        holds = seat_state.DEFAULT_OOM_HOLDS if oom_holds_path is None else oom_holds_path
        eff = read(persona, now=now, state_root=root, oom_holds_path=holds)
        oom = seat_state.SRC_OOM in eff.sources
        sleeping = eff.state == seat_state.STATE_DEEP_SLEEP
        return LaunchBlock(
            cause=CAUSE_SLEEP if sleeping else None,
            oom_held=oom,
            fault=eff.fault,
            owner=eff.owner,
            reason=eff.reason,
            expires=eff.expires,
        )
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        return LaunchBlock(fault=f"seat-sleep: {type(exc).__name__}: {exc}"[:300])


# ---------------------------------------------------------------------------
# System-orchestrator hold-down flag (ORG-104 rule 3, T-5226)
# ---------------------------------------------------------------------------

def is_sysorch(persona: str) -> bool:
    """True iff `persona` holds a system-orchestrator instance role (recovery, watchman)."""
    from tools import agent_role_map
    from tools.oom import sysorch_restart

    return bool(sysorch_restart.SYSORCH_ROLE.match(agent_role_map.resolve_instance_role(persona)))


def sysorch_flag_path(persona: str, *, hold_dir: Path | None = None) -> Path:
    hs.validate_persona(persona)
    return Path(hold_dir if hold_dir is not None else DEFAULT_HOLD_DIR) / persona


def clear_sysorch_flag(
    persona: str, *, hold_dir: Path | None = None, sysorch: bool | None = None
) -> bool:
    """Remove the hold-down flag for a system orchestrator. True iff one was removed.

    A no-op (False) for any other persona, so non-sysorch callers need no branch.
    Raises OSError on a real removal failure: callers decide (wake and the janitor
    refuse to end the record while a flag they own is stuck, the dangerous side)."""
    if not (is_sysorch(persona) if sysorch is None else sysorch):
        return False
    path = sysorch_flag_path(persona, hold_dir=hold_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def write_sysorch_flag(
    persona: str, *, hold_dir: Path | None = None, sysorch: bool | None = None, note: str = ""
) -> bool:
    """Create (or refresh) the hold-down flag for a system orchestrator. True iff one was
    written. The writer twin of `clear_sysorch_flag`: both gate on the SAME `is_sysorch`, so
    what sleep writes and what wake / the janitor remove can never disagree about which
    seats carry a flag. A no-op (False) for any other persona. Raises OSError on failure:
    the sleep verb then refuses and leaves the record unwritten (fail toward a running
    seat, ORG-106 section 6)."""
    if not (is_sysorch(persona) if sysorch is None else sysorch):
        return False
    path = sysorch_flag_path(persona, hold_dir=hold_dir)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, (note or "deep-sleep hold-down (ORG-106)").encode("utf-8")[:200] + b"\n")
    finally:
        os.close(fd)
    return True


# ---------------------------------------------------------------------------
# Telegram receiver seats (the 24 h update-retention guard)
# ---------------------------------------------------------------------------

def is_telegram_receiver(persona: str, *, manifest_path: Path | None = None) -> bool:
    """True iff `persona` has an inbound Telegram receiver, from the broadest structural
    facts in the manifest, per configured runtime: the codex-telegram-bridge membership
    (`communications.kind == "telegram"`) OR a Claude launch that carries
    `--channels plugin:telegram` (archivist's receiver is claude-only and is NOT in the
    bridge list). FAULT MEANS RECEIVER: an unreadable manifest or binding refuses the
    long sleep rather than risk silent message loss."""
    try:
        from tools import persona_bringup

        manifest = Path(manifest_path) if manifest_path is not None else persona_bringup.DEFAULT_MANIFEST
        primary = persona_bringup.load_binding(manifest, persona)
        for binding in persona_bringup.configured_runtime_bindings(manifest, primary):
            comms = binding.communications
            if isinstance(comms, dict) and comms.get("kind") == "telegram":
                return True
            native = getattr(binding.launch, "native", None) or {}
            channels = native.get("channels") if isinstance(native, dict) else None
            if isinstance(channels, str) and "plugin:telegram" in channels:
                return True
        return False
    except Exception:  # noqa: BLE001 - fault means receiver (refuse)
        return True


# ---------------------------------------------------------------------------
# Repeated hold-down flag removal failure (alert)
# ---------------------------------------------------------------------------

FLAG_FAILURE_ALERT_AFTER = 3
FLAG_FAILURE_ALERT_COOLDOWN = timedelta(hours=6)
FLAG_FAILURE_VOCAB = "oom_hold_flag_stuck"
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def flag_failures_path(persona: str, *, root: Path) -> Path:
    return hs.persona_root(persona, root=root) / "overrides" / "seat-state.flag-failures.json"


def _read_flag_failures(persona: str, root: Path) -> dict[str, Any]:
    try:
        doc = json.loads(flag_failures_path(persona, root=root).read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def note_flag_failure(persona: str, root: Path, exc: BaseException, *, now: datetime | None = None) -> int:
    """Record one failed hold-down flag removal (callers hold the seat-state lock). Returns
    the consecutive-failure count. Never raises."""
    try:
        when = (now or datetime.now(timezone.utc)).strftime(_TS_FMT)
        prior = _read_flag_failures(persona, root)
        prev = prior.get("count")
        count = prev + 1 if isinstance(prev, int) else 1
        hs._atomic_json(flag_failures_path(persona, root=root), {
            "schema": "pantheon-seat-flag-failures/v1",
            "count": count,
            "first_ts": prior.get("first_ts") or when,
            "last_ts": when,
            "last_error": f"{type(exc).__name__}: {exc}"[:300],
            "last_alert_ts": prior.get("last_alert_ts"),
        })
        return count
    except Exception:  # noqa: BLE001 - accounting must never mask the real failure
        return 0


def clear_flag_failures(persona: str, root: Path) -> None:
    try:
        flag_failures_path(persona, root=root).unlink()
    except OSError:
        pass


def clear_sysorch_flag_accounted(
    persona: str, *, root: Path, hold_dir: Path | None = None, now: datetime | None = None
) -> bool:
    """`clear_sysorch_flag` plus failure accounting: a failure bumps the durable counter
    (then re-raises), a success resets it. Call under the seat-state lock."""
    try:
        removed = clear_sysorch_flag(persona, hold_dir=hold_dir)
    except OSError as exc:
        note_flag_failure(persona, root, exc, now=now)
        raise
    clear_flag_failures(persona, root)
    return removed


def maybe_alert_flag_failure(
    persona: str, root: Path, *, now: datetime | None = None,
    alert_fn: Callable[[str, str, str], None] | None = None,
) -> bool:
    """Page (cooldown-gated) once the removal has failed FLAG_FAILURE_ALERT_AFTER times in a
    row. Call AFTER releasing the seat-state lock: routing a page can block. True iff paged.
    Never raises."""
    try:
        when = now or datetime.now(timezone.utc)
        doc = _read_flag_failures(persona, root)
        if not isinstance(doc.get("count"), int) or doc["count"] < FLAG_FAILURE_ALERT_AFTER:
            return False
        last = seat_state._parse_ts(doc.get("last_alert_ts"))
        if last is not None and when - last < FLAG_FAILURE_ALERT_COOLDOWN:
            return False
        if alert_fn is None:
            from tools import inbox_consumer

            alert_fn = inbox_consumer.route_failure_alert
        alert_fn(
            persona,
            f"{FLAG_FAILURE_VOCAB}: removing /var/lib/pantheon/oom-hold/{persona} has failed "
            f"{doc['count']} times in a row (last: {doc.get('last_error', '?')}). The seat-state "
            f"record is kept so nothing is half-ended, but {persona} stays held down until the "
            "flag is removed. Check the directory's group (pantheon-health) permissions.",
            FLAG_FAILURE_VOCAB,
        )
        doc["last_alert_ts"] = when.strftime(_TS_FMT)
        hs._atomic_json(flag_failures_path(persona, root=root), doc)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# launch_breach tripwire
# ---------------------------------------------------------------------------

def breach_message(persona: str, block: LaunchBlock) -> str:
    what = "deep-sleep" if block.sleeping else "an OOM hold"
    return (
        f"launch_breach: a session was launched for {persona} while {what} is in force "
        f"(owner: {block.owner or '-'}, reason: {block.reason or '-'}). A launcher bypassed the "
        f"seat-state chokepoint. Find it: the controller evidence log has the row."
    )
