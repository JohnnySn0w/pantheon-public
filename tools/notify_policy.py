#!/usr/bin/env python3
"""Per-persona notification mute policy — the ORG-076 §7 "Operator plane" control (T-2978).

A single per-persona mute registry + `is_muted(persona)` that every persona-scoped
alert path consults BEFORE sending. Muting a persona uniformly silences ALL its
fleet alerts (credential, channel-liveness, mail-staleness, context-fill, unit-
failure, …) — one place, instead of the piecemeal per-monitor skip envs this
supersedes (`CHANNEL_LIVENESS_SKIP`, `PANTHEON_CRED_ALERT_SKIP`).

DESIGN INVARIANTS
- **Fail-open (mute ≠ blind).** `is_muted` returns False on ANY registry fault
  (missing file, unreadable, malformed, unknown persona). A registry problem must
  never SILENCE an alert — the safe direction is a spurious page, not a missed one.
  Classification/trace-logging stays at each monitor regardless; only the operator
  page is withheld when a persona is *explicitly, readably* muted.
- **Single source of truth**, read fresh each call (no in-process cache) so a
  toggle takes effect on the next monitor cycle with no restart — ORG-068
  stateless/restart-fixable.
- **Atomic writes** (tmp + os.replace) so a concurrent reader never sees a
  half-written registry.

REGISTRY LOCATION: overridable via `PANTHEON_MUTE_REGISTRY`; the module default is
`/opt/pantheon-health/notify_mute.json` (the original root-provisioned design target,
never created). The LIVE location is `/var/lib/pantheon/notify-mute/notify_mute.json`
(T-5440): Recovery-owned, `pantheon-shared`, setgid directory and a group-readable (0640)
registry. Every alert-emitting monitor and writer runs under Recovery's user manager. The
monitor units set `PANTHEON_MUTE_REGISTRY` to that path explicitly.
kino's read (T-2979, orchestrator) is a separate leg — kino is not in `pantheon-mailbox`. Because
a caller with NO env falls back to the (absent) `/opt` default, `is_muted` fail-opens there
(nobody muted): a missing env can only cause a spurious page, never a silenced one.

FORMAT (schema pantheon-notify-mute/v1):
    {"schema": "pantheon-notify-mute/v1",
     "muted": {"<persona>": {"since": "<iso8601>", "by": "<who>", "reason": "<str>"}}}
`is_muted` keys on persona presence in `muted`; the metadata is for the health view.

SEAT MAINTENANCE MODE (T-2984 — the runtime-controller cutover companion). A SECOND,
orthogonal control siblinged here so the operator plane is one module: `in_maintenance
(persona)` suppresses remediation ACTIONS (auto-restarts) — NOT just alerts — while a
seat is deliberately in a non-standard pane state (a T-2953 harness-controller cutover
install). Distinct from mute in two ways that matter:
  - Domain: mute silences ALERTS; maintenance vetoes the ACTION (the wedge detector's
    graceful-restart, channel-liveness remediation) that would otherwise fight a cutover
    — observed live on specialist during T-2970 BT5, where the wedge detector misread the
    post-swap pane and dispatched a restart against a healthy seat.
  - AUTO-EXPIRY: every maintenance entry carries an `expires`; `in_maintenance` returns
    False past it, and a missing/malformed `expires` also reads False. A forgotten toggle
    can therefore NEVER leave a seat unmonitored — the failure direction is "monitor
    resumes", never "seat silently unguarded".
Storage is a SEPARATE registry from the mute one: the mute registry's shared `/opt`
location needs a root provisioning step (still pending, T-2978) and is dormant until
then, but maintenance must work NOW — and every consumer (context-fill + channel-liveness
monitors, and the enter/exit CLI) runs under the SAME account (Recovery's `systemctl --user`
manager, verified T-2984), so a Recovery-writable state-dir file needs no cross-account
provisioning. Path: `$PANTHEON_MAINTENANCE_REGISTRY`, else `$PANTHEON_STATE_DIR/
seat_maintenance.json`, else the SHARED `/var/lib/pantheon/seat-maintenance/
seat_maintenance.json` when that dir exists, else `~/.local/state/pantheon/
seat_maintenance.json` (the CLI warns loudly on that per-account fallback).
T-4150: the shared default exists because the "same account" premise held for the
CONSUMERS but not the ARMERS. orchestrator drives cutovers from its own account, and its
per-account arm for recovery never reached Recovery's monitors.
T-4362: maintenance also PAUSES the seat's inbox consumer. inbox_consumer skips its whole
cycle (skip:seat_maintenance: no clone pull, no ticket delivery) while the seat is armed,
so a hand-move's clone stays still. That makes it a delivery pause as well as an action
veto: an armed seat gets no inbox deliveries until the entry exits or expires. The
hand-move SOP's phase 0 refuses to fire unless the seat is armed (T-4377).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = "pantheon-notify-mute/v1"
DEFAULT_REGISTRY = "/opt/pantheon-health/notify_mute.json"

MAINTENANCE_SCHEMA = "pantheon-seat-maintenance/v1"
MAINTENANCE_DEFAULT_MINUTES = 60   # a typical cutover-install window
MAINTENANCE_MAX_MINUTES = 240      # hard cap (mirrors T-2965 WEDGE_SUPPRESS_MAX_MINUTES)


# T-4668: where the registry actually lives. DEFAULT_REGISTRY (/opt) was never
# provisioned, and only 3 of the 21 units that route alerts through
# route_failure_alert set PANTHEON_MUTE_REGISTRY, so on the other 18 is_muted read
# a missing file, failed open, and the mute chokepoint silenced nothing. Falling
# back here, once, at the chokepoint means a new unit cannot drift out of it.
HEALTH_PLANE_REGISTRY = "/var/lib/pantheon/notify-mute/notify_mute.json"

_load_fault_logged = False


def _registry_path(registry: str | os.PathLike | None = None) -> Path:
    """Explicit argument, else $PANTHEON_MUTE_REGISTRY, else the /opt default if it
    exists, else HEALTH_PLANE_REGISTRY (T-4668). os.path.exists, not Path.exists:
    the latter raises on EACCES under 3.12, and an unreadable /opt is 'absent' here."""
    if registry is not None:
        return Path(registry)
    env = os.environ.get("PANTHEON_MUTE_REGISTRY")
    if env:
        return Path(env)
    if os.path.exists(DEFAULT_REGISTRY):
        return Path(DEFAULT_REGISTRY)
    return Path(HEALTH_PLANE_REGISTRY)


def _load(registry: str | os.PathLike | None = None) -> dict:
    """Return the parsed registry, or an empty-but-valid one on ANY fault (fail-open).
    The first fault in a process is logged to stderr (T-4668): a registry nobody can
    read used to fail open in silence, which is how 18 units lost muting unnoticed."""
    global _load_fault_logged
    path = _registry_path(registry)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        if not _load_fault_logged:
            _load_fault_logged = True
            print(f"[notify_policy] mute registry {path} unreadable ({exc.__class__.__name__}); "
                  f"failing open: nothing reads as muted", file=sys.stderr)
        return {"schema": SCHEMA, "muted": {}}
    if not isinstance(data, dict) or not isinstance(data.get("muted"), dict):
        return {"schema": SCHEMA, "muted": {}}
    return data


class RegistryUnreadable(OSError):
    """T-5396: a registry that EXISTS but cannot be read or parsed. Writers refuse on it;
    only readers fail open. (An OSError, so CLI paths that already catch OSError exit 1.)"""


def _load_strict(path: Path, key: str, schema: str) -> dict:
    """The registry a WRITER may build on: absent = empty (the first write creates it);
    present but unreadable, malformed or wrong-shaped = RegistryUnreadable. A writer that
    used the fail-open load would rewrite the file with only its own entry, silently
    dropping every other mute or maintenance window (T-5396)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"schema": schema, key: {}}
    except (OSError, UnicodeDecodeError) as exc:
        raise RegistryUnreadable(f"{path}: unreadable ({exc.__class__.__name__}); refusing to "
                                 f"write over it") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RegistryUnreadable(f"{path}: not valid JSON; refusing to write over it") from exc
    if not isinstance(data, dict) or not isinstance(data.get(key), dict):
        raise RegistryUnreadable(f"{path}: no '{key}' mapping; refusing to write over it")
    return data


def is_muted(persona: str, registry: str | os.PathLike | None = None) -> bool:
    """True iff `persona` is explicitly, readably muted. FAIL-OPEN: any registry
    fault (missing/unreadable/malformed) returns False — a broken registry never
    silences an alert."""
    if not persona:
        return False
    return persona in _load(registry).get("muted", {})


def list_muted(registry: str | os.PathLike | None = None) -> dict:
    """Return the {persona: metadata} mapping of currently-muted personas."""
    return dict(_load(registry).get("muted", {}))


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".notify_mute.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        # mkstemp creates 0600 (writer-only). The shared registry lives in a group-
        # readable dir (pantheon-mailbox) so EVERY alert-emitting account reads the same
        # source of truth (T-2987) — widen to 0640 so a toggle written by one account
        # stays readable by the others. Non-secret content (persona names + mute meta).
        # Best-effort: a chmod failure (e.g. a non-owner refresh) must not fail the write.
        try:
            os.chmod(tmp, 0o640)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def mute(persona: str, *, by: str = "", reason: str = "",
         registry: str | os.PathLike | None = None, now_iso: str | None = None) -> bool:
    """Mute `persona`. Returns True if newly muted, False if already muted (idempotent)."""
    if not persona:
        raise ValueError("persona must be non-empty")
    data = _load_strict(_registry_path(registry), "muted", SCHEMA)
    muted = data["muted"]
    data.setdefault("schema", SCHEMA)
    if persona in muted:
        return False
    stamp = now_iso or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    muted[persona] = {"since": stamp, "by": by, "reason": reason}
    _atomic_write(_registry_path(registry), data)
    return True


def unmute(persona: str, registry: str | os.PathLike | None = None) -> bool:
    """Unmute `persona`. Returns True if it was muted (and is now removed), else False."""
    if not persona:
        raise ValueError("persona must be non-empty")
    data = _load_strict(_registry_path(registry), "muted", SCHEMA)
    muted = data["muted"]
    if persona not in muted:
        return False
    del muted[persona]
    data.setdefault("schema", SCHEMA)
    _atomic_write(_registry_path(registry), data)
    return True


# --- Seat maintenance mode (T-2984): suppress remediation ACTIONS with auto-expiry ---

# T-4150: the default registry is SHARED, so an arm reaches the monitors whichever
# account runs it. The monitors run as Recovery; a cutover is often driven from orchestrator's
# account. Under the old per-account default, orchestrator's 09-11 arm for recovery landed in
# /home/claude and suppressed nothing. The dir is 2775 claude-fallback:pantheon-shared
# (every seat account but codex is a member; T-4150 provisioned it, no root needed).
SHARED_MAINTENANCE_DIR = Path("/var/lib/pantheon/seat-maintenance")


def _maintenance_registry_path(registry: str | os.PathLike | None = None) -> Path:
    if registry is not None:
        return Path(registry)
    env = os.environ.get("PANTHEON_MAINTENANCE_REGISTRY")
    if env:
        return Path(env)
    state = os.environ.get("PANTHEON_STATE_DIR")
    if state:
        return Path(state) / "seat_maintenance.json"
    if SHARED_MAINTENANCE_DIR.is_dir():
        return SHARED_MAINTENANCE_DIR / "seat_maintenance.json"
    # Unprovisioned host: per-account, so only monitors running as THIS account see
    # the arm. The CLI warns loudly when it lands here.
    return Path.home() / ".local" / "state" / "pantheon" / "seat_maintenance.json"


def _maintenance_registry_is_shared(path: Path) -> bool:
    return path.parent == SHARED_MAINTENANCE_DIR


def _load_maintenance(registry: str | os.PathLike | None = None) -> dict:
    """Return the parsed maintenance registry, or an empty-but-valid one on ANY fault."""
    path = _maintenance_registry_path(registry)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {"schema": MAINTENANCE_SCHEMA, "maintenance": {}}
    if not isinstance(data, dict) or not isinstance(data.get("maintenance"), dict):
        return {"schema": MAINTENANCE_SCHEMA, "maintenance": {}}
    return data


def _parse_iso(s: object) -> datetime | None:
    try:
        return datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _prune_expired(mapping: dict, now_dt: datetime) -> None:
    """Drop expired/malformed entries in place — keeps the file from accreting stale rows."""
    for persona in list(mapping):
        entry = mapping[persona]
        exp = _parse_iso(entry.get("expires")) if isinstance(entry, dict) else None
        if exp is None or exp <= now_dt:
            del mapping[persona]


def in_maintenance(persona: str, registry: str | os.PathLike | None = None,
                   now_iso: str | None = None) -> bool:
    """True iff `persona` is in an ACTIVE (non-expired) maintenance window — i.e. its
    remediation actions must be suppressed. FAIL-SAFE toward monitoring: any fault, a
    missing entry, OR a missing/malformed/expired `expires` returns False, so a forgotten
    or corrupt toggle can never leave a seat un-remediated indefinitely."""
    if not persona:
        return False
    entry = _load_maintenance(registry).get("maintenance", {}).get(persona)
    if not isinstance(entry, dict):
        return False
    expires = _parse_iso(entry.get("expires"))
    if expires is None:
        return False
    now = (_parse_iso(now_iso) if now_iso else None) or datetime.now(timezone.utc)
    return expires > now


def list_maintenance(registry: str | os.PathLike | None = None) -> dict:
    """Return the {persona: metadata} mapping of maintenance entries (expired included —
    callers annotate active/expired via `expires`)."""
    return dict(_load_maintenance(registry).get("maintenance", {}))


def enter_maintenance(persona: str, *, minutes: int = MAINTENANCE_DEFAULT_MINUTES,
                      by: str = "", reason: str = "",
                      registry: str | os.PathLike | None = None,
                      now_iso: str | None = None) -> str:
    """Put `persona` into maintenance for `minutes` (bounded to [1, MAINTENANCE_MAX_MINUTES]).
    Idempotent-refresh: re-entering re-arms the window from now. Returns the `expires` iso."""
    if not persona:
        raise ValueError("persona must be non-empty")
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = MAINTENANCE_DEFAULT_MINUTES
    minutes = max(1, min(minutes, MAINTENANCE_MAX_MINUTES))
    now = (_parse_iso(now_iso) if now_iso else None) or datetime.now(timezone.utc)
    expires_dt = now + timedelta(minutes=minutes)
    data = _load_strict(_maintenance_registry_path(registry), "maintenance", MAINTENANCE_SCHEMA)
    m = data["maintenance"]
    data.setdefault("schema", MAINTENANCE_SCHEMA)
    _prune_expired(m, now)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires = expires_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    m[persona] = {"since": stamp, "expires": expires, "minutes": minutes,
                  "by": by, "reason": reason}
    _atomic_write(_maintenance_registry_path(registry), data)
    return expires


def exit_maintenance(persona: str, registry: str | os.PathLike | None = None) -> bool:
    """Clear `persona`'s maintenance window. Returns True if it was present, else False."""
    if not persona:
        raise ValueError("persona must be non-empty")
    data = _load_strict(_maintenance_registry_path(registry), "maintenance", MAINTENANCE_SCHEMA)
    m = data["maintenance"]
    if persona not in m:
        return False
    del m[persona]
    data.setdefault("schema", MAINTENANCE_SCHEMA)
    _atomic_write(_maintenance_registry_path(registry), data)
    return True


def _cli_mute(args) -> int:
    if args.cmd == "mute":
        by = args.by or f"cli:{os.environ.get('USER', '?')}"
        newly = mute(args.persona, by=by, reason=args.reason, registry=args.registry)
        print(f"{args.persona}: {'muted' if newly else 'already muted'}")
        return 0
    was = unmute(args.persona, registry=args.registry)
    print(f"{args.persona}: {'unmuted' if was else 'was not muted'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--registry", help=f"registry path (default env PANTHEON_MUTE_REGISTRY or {DEFAULT_REGISTRY})")
    sub = p.add_subparsers(dest="cmd", required=True)
    for verb in ("mute", "unmute"):
        sp = sub.add_parser(verb)
        sp.add_argument("persona")
        if verb == "mute":
            sp.add_argument("--reason", default="")
            sp.add_argument("--by", default=None,
                            help="who asked (default cli:$USER); e.g. kino:<user> (T-5397)")
    sp_list = sub.add_parser("list")
    sp_is = sub.add_parser("is-muted")
    sp_is.add_argument("persona")
    for verb in ("enter-maintenance", "exit-maintenance", "in-maintenance"):
        sp = sub.add_parser(verb)
        sp.add_argument("persona")
        if verb == "enter-maintenance":
            sp.add_argument("--minutes", type=int, default=MAINTENANCE_DEFAULT_MINUTES)
            sp.add_argument("--reason", default="")
    sub.add_parser("list-maintenance")
    args = p.parse_args(argv)

    if args.cmd in ("mute", "unmute"):
        try:
            return _cli_mute(args)
        except RegistryUnreadable as exc:
            print(f"[notify_policy] REFUSED {args.cmd}: {exc}. No mute changed (T-5396).",
                  file=sys.stderr)
            return 3
    if args.cmd == "list":
        muted = list_muted(args.registry)
        if not muted:
            print("(no personas muted)")
        for persona, meta in sorted(muted.items()):
            print(f"{persona}\tsince={meta.get('since', '?')}\tby={meta.get('by', '?')}\treason={meta.get('reason', '')}")
        return 0
    if args.cmd == "is-muted":
        muted = is_muted(args.persona, args.registry)
        print("muted" if muted else "not-muted")
        return 0 if muted else 1
    if args.cmd in ("enter-maintenance", "exit-maintenance"):
        # T-4150: name the file every time, and say loudly when it is per-account,
        # because an arm the acting monitors never read fails silently otherwise.
        path = _maintenance_registry_path(args.registry)
        try:
            if args.cmd == "enter-maintenance":
                expires = enter_maintenance(args.persona, minutes=args.minutes,
                                            by=f"cli:{os.environ.get('USER', '?')}",
                                            reason=args.reason, registry=args.registry)
                print(f"{args.persona}: maintenance ARMED until {expires} ({args.minutes}m)")
            else:
                was = exit_maintenance(args.persona, registry=args.registry)
                print(f"{args.persona}: {'maintenance cleared' if was else 'was not in maintenance'}")
        except OSError as exc:
            print(f"ERROR: cannot write maintenance registry {path}: {exc}. Arm from an "
                  f"account in group pantheon-shared (e.g. recovery or orchestrator).", file=sys.stderr)
            return 1
        print(f"registry: {path}", file=sys.stderr)
        if not _maintenance_registry_is_shared(path) and args.registry is None and not (
                os.environ.get("PANTHEON_MAINTENANCE_REGISTRY") or os.environ.get("PANTHEON_STATE_DIR")):
            print(f"WARNING: {SHARED_MAINTENANCE_DIR} is not provisioned on this host, so this "
                  f"arm is PER-ACCOUNT. Only monitors running as {os.environ.get('USER', 'this user')} "
                  f"will honor it.", file=sys.stderr)
        return 0
    if args.cmd == "in-maintenance":
        active = in_maintenance(args.persona, args.registry)
        print("in-maintenance" if active else "not-in-maintenance")
        return 0 if active else 1
    if args.cmd == "list-maintenance":
        entries = list_maintenance(args.registry)
        now_dt = datetime.now(timezone.utc)
        if not entries:
            print("(no seats in maintenance)")
        for persona, meta in sorted(entries.items()):
            exp = _parse_iso(meta.get("expires"))
            state = "ACTIVE" if (exp is not None and exp > now_dt) else "expired"
            print(f"{persona}\t{state}\texpires={meta.get('expires', '?')}"
                  f"\tby={meta.get('by', '?')}\treason={meta.get('reason', '')}")
        return 0
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
