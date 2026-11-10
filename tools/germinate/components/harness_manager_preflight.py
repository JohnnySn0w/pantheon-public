"""harness-manager's component PREFLIGHT checks (T-5921; T-3191 (viii) component items).

Exported from harness-manager_handlers.py as ``PREFLIGHT`` (the hook schema is in
engine/preflight.py, "COMPONENT PREFLIGHT HOOK"). Read-only: every host fact comes
through ``ctx.probe`` (lstat, a bounded no-follow read, a passwd lookup); the plan
data and instance.yaml (the planner checkout, ``persona_installer.REPO_ROOT``, as
the planner itself reads it) are the only other inputs. Nothing here can change the
host. One check per id, always all four, in this order:

* ``seat-user-bus`` -- every seat-scope plan: ``/run/user/<uid>/bus`` is a socket
  (every harness-manager seat op drives the seat's own user manager).
* ``target-manifest-identical`` -- every seat-scope plan: the seat clone's
  ``<home>/pantheon-config/instance.yaml`` has the planner's sha256 (mason ruling Q2,
  T-5830: a stale target clone means the seat's units read other config).
* ``seat-state-awake`` -- install and configure (not ``desired.run_state``, whose
  plan owns that record): no ORG-106 seat-state record (``harness_state.seat_state_path``
  under ``<home>/.local/state/pantheon``). Any record (paused-intake or deep-sleep)
  fails; a TTL-expired record still fails here (fail-closed; seat_state.effective
  is the authority for expiry, and this check does not re-derive it).
* ``credential-mode`` -- a seat with a ``codex_telegram_bridge`` persona binding: the
  bot credential (``<home>/<credential_env>``) is a regular file (never a symlink),
  owned by the seat uid, mode exactly 0600. It is stat'ed, never read. Else vacuous.

A check is ``vacuous`` (with a reason) only when it does not apply to this plan; a
probe that cannot observe is ``fail`` / ``unobservable``, never a pass.

DEFERRED (mason ruling Q5, T-5921), not built: ``receiver-topology`` (the declared
Telegram receiver's unit present and the other receiver's enable link absent) and
``webhook-inactive`` (consume the existing ``codex_telegram_bridge`` telegram-preflight
receipt). Both are vacuous on a channel-less seat; both await Recovery's review of the
shape.
"""

from __future__ import annotations

import hashlib
import posixpath
from pathlib import Path
from typing import Any

from tools import harness_state
from tools.germinate.engine.preflight import Unobservable

SEAT_PREFIX = "seat-"
CHECK_IDS = ("seat-user-bus", "target-manifest-identical", "seat-state-awake", "credential-mode")
TARGET_CLONE = "pantheon-config"
STATE_ROOT_REL = ".local/state/pantheon"


def _c(cid: str, status: str, reason: str | None, detail: str, **evidence: Any) -> dict[str, Any]:
    return {"id": cid, "status": status, "reason": reason, "detail": detail, "evidence": evidence}


def _manifest():
    """(manifest, planner instance.yaml sha256) from the planner checkout, or (None, error text)."""

    from tools.persona_installer import REPO_ROOT, Manifest

    path = REPO_ROOT / "instance.yaml"
    try:
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        return Manifest.load(path, strict_flip_keys=False), sha
    except Exception as exc:  # noqa: BLE001 -- an unreadable manifest fails the checks that need it
        return None, f"{type(exc).__name__}: {exc}"


def _persona(manifest, account: str) -> str | None:
    for role in manifest.roles.values():
        persona = role.get("persona") if isinstance(role, dict) else getattr(role, "persona", None)
        try:
            if persona and manifest.binding(persona).account == account:
                return persona
        except Exception:  # noqa: BLE001 -- a role that does not bind is simply not this seat
            continue
    return None


def _user_bus(probe, info) -> dict[str, Any]:
    cid = "seat-user-bus"
    path = f"/run/user/{info.uid}/bus"
    try:
        seen = probe.path_stat(path)
    except Unobservable as exc:
        return _c(cid, "fail", "unobservable", str(exc), path=path)
    if seen is None:
        return _c(cid, "fail", "user-bus-absent", f"{path} is absent: the seat's user manager is not reachable", path=path)
    if seen.kind != "socket":
        return _c(cid, "fail", "user-bus-not-socket", f"{path} is a {seen.kind}, not a socket", path=path, kind=seen.kind)
    return _c(cid, "pass", None, f"{path} is a socket", path=path)


def _target_manifest(probe, info, planner_sha: str | None, manifest_error: str | None) -> dict[str, Any]:
    cid = "target-manifest-identical"
    path = f"{info.home}/{TARGET_CLONE}/instance.yaml"
    if planner_sha is None:
        return _c(cid, "fail", "planner-manifest-unreadable", f"the planner's instance.yaml: {manifest_error}")
    try:
        seen = probe.file_sha256(path)
    except Unobservable as exc:
        return _c(cid, "fail", "unobservable", str(exc), path=path, planner_sha256=planner_sha)
    if seen is None:
        return _c(cid, "fail", "target-manifest-absent", f"{path} is absent", path=path, planner_sha256=planner_sha)
    if seen != planner_sha:
        return _c(
            cid, "fail", "target-manifest-differs", f"{path} is sha256 {seen}, the planner's is {planner_sha}",
            path=path, planner_sha256=planner_sha, target_sha256=seen,
        )
    return _c(cid, "pass", None, f"{path} matches the planner's instance.yaml", path=path, sha256=seen)


def _seat_state(ctx, probe, info, persona: str | None) -> dict[str, Any]:
    cid = "seat-state-awake"
    if ctx.leg not in ("install", "configure") or ctx.desired.get("run_state"):
        return _c(cid, "vacuous", "leg-owns-seat-state", f"the {ctx.leg} plan owns or ignores the seat-state record")
    if persona is None:
        return _c(cid, "fail", "persona-unresolved", "no persona in instance.yaml is bound to this seat's account")
    path = str(harness_state.seat_state_path(persona, root=Path(info.home) / STATE_ROOT_REL))
    try:
        record = probe.read_json(path)
    except Unobservable as exc:
        return _c(cid, "fail", "unobservable", str(exc), path=path)
    if record is None:
        return _c(cid, "pass", None, "no seat-state record: the seat is awake", path=path)
    if not isinstance(record, dict) or not isinstance(record.get("state"), str):
        return _c(cid, "fail", "seat-state-malformed", f"{path} is not a seat-state record", path=path)
    return _c(
        cid, "fail", "seat-not-awake", f"the seat carries a {record['state']} record (owner {record.get('owner')!r})",
        path=path, state=record["state"], owner=record.get("owner"),
    )


def _credential(manifest, probe, info, persona: str | None, manifest_error: str | None) -> dict[str, Any]:
    cid = "credential-mode"
    if manifest is None:
        return _c(cid, "fail", "planner-manifest-unreadable", f"the planner's instance.yaml: {manifest_error}")
    if persona is None:
        return _c(cid, "fail", "persona-unresolved", "no persona in instance.yaml is bound to this seat's account")
    bridge = ((manifest.installer.get("codex_telegram_bridge") or {}).get("personas") or {}).get(persona)
    if not isinstance(bridge, dict):
        return _c(cid, "vacuous", "no-telegram-receiver", f"{persona} has no codex_telegram_bridge binding")
    rel = bridge.get("credential_env")
    if not (isinstance(rel, str) and rel and not rel.startswith("/") and posixpath.normpath(rel) == rel and ".." not in rel.split("/")):
        return _c(cid, "fail", "credential-path-malformed", f"credential_env {rel!r} is not a home-relative path")
    path = f"{info.home}/{rel}"
    try:
        seen = probe.path_stat(path)
    except Unobservable as exc:
        return _c(cid, "fail", "unobservable", str(exc), path=path)
    if seen is None:
        return _c(cid, "fail", "credential-absent", f"{path} is absent", path=path)
    found = {"kind": seen.kind, "uid": seen.uid, "mode": seen.mode}
    if seen.kind != "file":
        return _c(cid, "fail", "credential-not-regular", f"{path} is a {seen.kind} (never followed)", path=path, **found)
    if seen.uid != info.uid:
        return _c(cid, "fail", "credential-owner", f"{path} is owned by uid {seen.uid}, not the seat's {info.uid}", path=path, **found)
    if seen.mode != "0600":
        return _c(cid, "fail", "credential-mode", f"{path} is mode {seen.mode}, not 0600", path=path, **found)
    return _c(cid, "pass", None, f"{path} is a seat-owned 0600 regular file", path=path, **found)


def preflight(ctx) -> list[dict[str, Any]]:
    """The four checks (module docstring), in ``CHECK_IDS`` order."""

    if not ctx.scope.startswith(SEAT_PREFIX):
        return [_c(cid, "vacuous", "not-a-seat-scope", "harness-manager checks a seat scope only") for cid in CHECK_IDS]
    account = ctx.scope[len(SEAT_PREFIX):]
    try:
        info = ctx.probe.account(account)
    except Unobservable as exc:
        return [_c(cid, "fail", "unobservable", f"account {account!r}: {exc}") for cid in CHECK_IDS]
    if info is None:
        return [_c(cid, "fail", "seat-account-absent", f"account {account!r} does not exist") for cid in CHECK_IDS]
    manifest, sha_or_error = _manifest()
    planner_sha = sha_or_error if manifest is not None else None
    manifest_error = None if manifest is not None else sha_or_error
    persona = _persona(manifest, account) if manifest is not None else None
    return [
        _user_bus(ctx.probe, info),
        _target_manifest(ctx.probe, info, planner_sha, manifest_error),
        _seat_state(ctx, ctx.probe, info, persona),
        _credential(manifest, ctx.probe, info, persona, manifest_error),
    ]
