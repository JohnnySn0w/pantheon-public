#!/usr/bin/env python3
"""tools/session_chronicle.py: the session-end Chronicle duty (T-5016).

The operator TG 11829/11830: archiving a seat's session when it ends or is reset is a
harness-controller duty, for EVERY seat on EVERY harness. Before this, only the
legacy claude restart script wrote Chronicle entries, and only on a graceful,
acked restart; a plain session death, a controller swap, a consume-plane restart
or a seat relaunch archived nothing.

ONE duty, several callers, ONE record per session end:

  * tools/runtime_controller.py runs `session-end` (bounded subprocess) right
    before every pane (re)start in _start_runtime (catch-all: replace, swaps,
    consume-plane restarts, reboot/crash reconcile) and right after
    controller_main sees its supervised runtime end.
  * tools/handoff_and_restart.sh, tools/pantheon-seat-relaunch.sh and
    tools/fleet_reboot.py call it instead of writing the Chronicle themselves.

What gets archived (the "archive unit"), in chronicle.py's existing layout:

  * index line   `- <ts> | session-end/<cause> | <uuid> | <summary>`
  * <ts>-<uuid>.md            HANDOFF.md as it stood at session end (fresh or not)
  * <ts>-<uuid>.pane.log.gz   the session's pane-timed side-log (harness-neutral)
  * <ts>-<uuid>.session.json  metadata: harness, cause, freshness, native
                              transcript POINTER (path/bytes/sha256), writer
  * <ts>-<uuid>.summary.md    (T-6118 PR-4) a deterministic, marked tail summary of the native
                              transcript, only when the HANDOFF was stale; beside the .md, never in it

A HOLLOW end (no transcript, no pane log, stale HANDOFF, lifetime under 120 s) writes only the done
claim and a session.json with hollow:true: no .md, no index line, no memex export (T-6118 PR-4).

If a controller swap already archived the session's handoff (planned-runtime-swap
/ forced-provider-cap), the duty pairs its companions with THAT entry and writes
no second index line.

Exactly once: each session has a key (persona + runtime.json started_at + the
pane log's first timestamp). A writer claims `<chronicle>/<agent>/.claims/<key>.json`
by linking a fully written temp file into place (exclusive and atomic, T-5037),
carrying the session identity from its first byte; a `done` claim makes every
later caller a no-op, and a writer with no pane log defers to a fresh `claimed`
claim for the same runtime session. A `failed` or stale `claimed` claim may be
re-claimed.

Fail-open: the duty never raises to its caller and the CLI always exits 0. A
failure is recorded on the claim and routed to Recovery (inbox_consumer's
route_failure_alert, cooldown-gated). PANTHEON_SESSION_CHRONICLE_ALERT=off keeps
alerts in the log only (test sandboxes).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import chronicle  # noqa: E402
from tools import chronicle_export  # noqa: E402

SCHEMA = "pantheon-session-chronicle/v1"
REASON_PREFIX = "session-end/"
SWAP_REASONS = ("planned-runtime-swap", "forced-provider-cap")
CAUSES = (
    "start", "replace", "start-reconcile", "death", "cap-swap", "fallback-return",
    "operator-swap", "graceful-restart", "forced-restart", "ctxfill-relaunch",
    "fleet-reboot", "orphan-staged", "thread-rotation", "authentication-loss-swap", "sleep",
)
CLAIMS_DIRNAME = ".claims"
CLAIM_STALE_SECONDS = 300.0
ALERT_COOLDOWN_SECONDS = 6 * 3600.0
ALERT_ENV = "PANTHEON_SESSION_CHRONICLE_ALERT"
ALERT_VOCAB = "chronicle-archive-failed"
ALERT_KIND_FAILED = "archive-failed"      # the session end was NOT archived
ALERT_KIND_DEGRADED = "archived-degraded"  # the record was written; something around it failed
TMUX_TIMEOUT_SECONDS = 5.0
COVERAGE_GRACE_SECONDS = 15 * 60.0
# A pane-unknown marker normally clears at the next successful start archive.
# Give it twice the runtime controller's two-hour transient-backoff ceiling;
# beyond that, WARN would hide a durable archive miss indefinitely (T-5132).
PANE_UNKNOWN_STALE_SECONDS = 4 * 3600.0
_PANE_STAMP_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] ")
_STAMP_RE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
#: A persona names directories (chronicle/<agent>, personas/<persona>) and a log
#: file name, so anything outside this shape is refused before a path is built.
PERSONA_RE = re.compile(r"^[a-z0-9-]+$")


def valid_persona(persona: Any) -> bool:
    return isinstance(persona, str) and bool(PERSONA_RE.match(persona))


def _log(msg: str) -> None:
    try:
        print(f"[session-chronicle] {msg}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    """ISO-8601 (runtime.json / index) or a pane-log stamp 'YYYY-MM-DD HH:MM:SS'
    (host localtime, UTC on the fleet VPS) -> aware UTC datetime, else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------- locations
def pane_log_dir() -> Path:
    """Same resolution as arm_pane_log.sh and handoff_and_restart.sh."""
    configured = os.environ.get("PANE_LOG_DIR")
    return Path(configured) if configured else Path.home() / ".local" / "log" / "pantheon"


def persona_state_root() -> Path:
    """runtime_controller's persona state root. PANTHEON_PERSONA_STATE_DIR wins
    (the controller's own override); else <PANTHEON_STATE_DIR>/personas; else
    ~/.local/state/pantheon/personas (identical to the controller default when
    neither variable is set)."""
    configured = os.environ.get("PANTHEON_PERSONA_STATE_DIR")
    if configured:
        return Path(configured)
    state_dir = os.environ.get("PANTHEON_STATE_DIR")
    if state_dir:
        return Path(state_dir) / "personas"
    return Path.home() / ".local" / "state" / "pantheon" / "personas"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _runtime_record(persona: str, state_root: Path) -> dict[str, Any]:
    return _read_json(state_root / persona / "runtime-controller" / "runtime.json")


#: T-5019 item 5: how much newer active-runtime.json's started_ts must be than
#: runtime.json's started_at before the record counts as stale. The controller
#: writes both from the same handle.started_at, so a healthy pair is equal.
STALE_RECORD_TOLERANCE_SECONDS = 1.0
ACTIVE_ABSENT_KEY = "_active_runtime_absent"
RECORD_STARTED_KEY = "_rt_record_started_at"
RECORD_CLAIM_FIELD = "rt_record_started_at"


def _effective_runtime_record(persona: str, state_root: Path) -> dict[str, Any]:
    """runtime.json, with started_at taken from active-runtime.json when the
    record is stale (T-5019 item 5, the D-10 residual).

    controller_main skips the runtime.json write when source provenance is
    unavailable (_persist_runtime_record_on_launch), so started_at can name an
    OLD session and every pane-log-less session end would share one key. It
    writes active-runtime.json (started_ts) on every launch regardless, and
    clears it only after H2 ran. Healthy seats are untouched: the two values
    are equal there. The raw record value is kept under RECORD_STARTED_KEY, and
    ACTIVE_ABSENT_KEY says the active record was gone (so an H2 claim made
    under the active value may cover this session)."""
    rt = _runtime_record(persona, state_root)
    raw = rt.get("started_at") if isinstance(rt.get("started_at"), str) else None
    active = _read_json(state_root / persona / "active-runtime.json").get("started_ts")
    active_at = _parse_time(active)
    out = dict(rt)
    out[RECORD_STARTED_KEY] = raw
    out[ACTIVE_ABSENT_KEY] = active_at is None
    raw_at = _parse_time(raw)
    if active_at is not None and (
        raw_at is None or (active_at - raw_at).total_seconds() > STALE_RECORD_TOLERANCE_SECONDS
    ):
        out["started_at"] = active
    return out


def _pane_first_stamp(path: Path) -> str | None:
    """The first timestamp in a pane-timed log: identifies the session the log
    belongs to (each session's tap starts a fresh file)."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(50):
                line = handle.readline()
                if not line:
                    break
                match = _PANE_STAMP_RE.match(line)
                if match:
                    return match.group(1)
    except OSError:
        return None
    return None


def _nonempty(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


# ---------------------------------------------------------------- tmux
def _tmux_checked(socket: str | None, *args: str) -> tuple[subprocess.CompletedProcess[str] | None, bool]:
    """(result, timed_out). result is None when tmux could not be run or timed out."""
    argv = ["tmux", *(["-L", socket] if socket else []), *args]
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=TMUX_TIMEOUT_SECONDS), False
    except subprocess.TimeoutExpired:
        return None, True
    except (OSError, subprocess.SubprocessError):
        return None, False


def _tmux(socket: str | None, *args: str) -> subprocess.CompletedProcess[str] | None:
    return _tmux_checked(socket, *args)[0]


def _pane_query(target: str | None, socket: str | None, fmt: str) -> str | None:
    if not target:
        return None
    result = _tmux(socket, "display-message", "-p", "-t", target, fmt)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip()


def _pane_dead_state(target: str | None, socket: str | None) -> str:
    """T-5019 item 3: 'live', 'dead' or 'unknown'. A query that TIMES OUT (a hung
    tmux server) is unknown, never dead. A pane that is gone (non-zero exit), no
    target, or no tmux binary reads as dead, as before."""
    if not target:
        return "dead"
    result, timed_out = _tmux_checked(socket, "display-message", "-p", "-t", target, "#{pane_dead}")
    if timed_out:
        return "unknown"
    if result is None or result.returncode != 0:
        return "dead"
    return "live" if result.stdout.strip() == "0" else "dead"


def close_pane_pipe(target: str | None, socket: str | None) -> bool:
    """Close the pane's pipe-pane tap if one is attached. A tap survives
    `respawn-pane -k`, so without this the next session would keep writing into
    the archived (unlinked) log and arm_pane_log.sh would refuse to re-arm.
    Returns True only when a pipe was attached and is now closed."""
    if _pane_query(target, socket, "#{pane_pipe}") != "1":
        return False
    result = _tmux(socket, "pipe-pane", "-t", target)
    return result is not None and result.returncode == 0


# ---------------------------------------------------------------- claims
def _claims_dir(root: Path, agent: str) -> Path:
    return root / agent / CLAIMS_DIRNAME


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    tmp.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _create_exclusive(path: Path, payload: dict[str, Any]) -> bool:
    """Create ``path`` holding ``payload`` only if it does not exist (T-5037).

    O_CREAT|O_EXCL then write left the claim EMPTY for a moment. A racing
    writer that read it then saw no state and no time, judged it stale, moved
    it aside and claimed too, so two writers each archived the session (the
    two-record CI flake). Linking a fully written temp file into place is
    exclusive (link raises FileExistsError) and atomic, so a claim is never
    seen half-written. The temp name ends in .claim, never matched by *.json.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.claim")
    try:
        tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.link(tmp, path)
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)
    return True


def _claim_is_fresh(claim: dict[str, Any], now: datetime) -> bool:
    """A 'claimed' doc is live while its ``at`` is within CLAIM_STALE_SECONDS,
    the same rule ``_try_claim`` uses before reclaiming it."""
    at = _parse_time(claim.get("at"))
    return at is not None and (now - at).total_seconds() <= CLAIM_STALE_SECONDS


def _empty_claim_is_fresh(path: Path) -> bool:
    """An existing claim with no readable content counts as busy while it is
    young (a writer may have died between create and write before T-5037, or the
    file is being replaced). Judged by the file's wall-clock age, since ``now``
    can be an injected test time."""
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return False
    return age <= CLAIM_STALE_SECONDS


def _try_claim(path: Path, now: datetime,
               identity: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """(state, prior claim doc). state: 'claimed' (this caller owns it), 'done',
    'pending' (done, pane log still to attach) or 'busy' (another writer holds a
    fresh claim). The prior doc is what a re-claim superseded, so the caller can
    see whether a killed predecessor already wrote the record (review MINOR 4).

    ``identity`` (T-5037) is written into the claim from its first byte, so a
    writer with a DIFFERENT key for the same session (no pane log, keyed on the
    runtime record alone) sees this one while it is still archiving."""
    payload = {**(identity or {}), "state": "claimed", "pid": os.getpid(), "at": _iso(now)}
    previous: dict[str, Any] = {}
    for _ in range(3):
        if not _create_exclusive(path, payload):
            existing = _read_json(path)
            if not existing and _empty_claim_is_fresh(path):
                return "busy", existing
            state = existing.get("state")
            if state == "done":
                return ("pending" if existing.get("pane_log") == "pending" else "done"), existing
            at = _parse_time(existing.get("at"))
            stale = at is None or (now - at).total_seconds() > CLAIM_STALE_SECONDS
            if state == "claimed" and not stale:
                return "busy", existing
            previous = existing
            # failed, unreadable, or a stale claim: move it aside (rename is
            # atomic, so only one re-claimer wins) and retry the exclusive create.
            try:
                os.rename(path, path.with_name(f"{path.name}.superseded-{os.getpid()}-{time.monotonic_ns()}"))
            except FileNotFoundError:
                pass
            continue
        return "claimed", previous
    return "busy", previous


def _done_claim_for_stamp(root: Path, agent: str, stamp: str) -> dict[str, Any] | None:
    """T-5019 item 2: the done claim of a session whose pane log began at ``stamp``."""
    for claim in _claims_for(root, agent):
        if claim.get("state") == "done" and claim.get("pane_first_stamp") == stamp:
            return claim
    return None


def _stale_record_claim(root: Path, agent: str, record_started: str, stamp: str) -> dict[str, Any] | None:
    """T-5019 rebound MAJOR 1: the claim an earlier call made for THIS pane log
    while active-runtime.json still named the session (keyed on its started_ts,
    with the stale runtime.json value kept as rt_record_started_at)."""
    for claim in _claims_for(root, agent):
        if claim.get(RECORD_CLAIM_FIELD) == record_started and claim.get("pane_first_stamp") == stamp:
            return claim
    return None


#: T-5019 rebound MINOR 3: H2 could not tell whether the pane was dead (hung
#: tmux) and archived nothing. Not *.json, so claim globs never read it.
PANE_UNKNOWN_MARKER = "pane-unknown-pending"


def _mark_pane_unknown(root: Path, agent: str, now: datetime) -> None:
    try:
        claims = _claims_dir(root, agent)
        claims.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(claims / PANE_UNKNOWN_MARKER, {"at": _iso(now)})
    except Exception as exc:  # noqa: BLE001 -- the marker is best-effort
        _log(f"pane-unknown marker not written: {exc!r}")


def _clear_pane_unknown(root: Path, agent: str) -> None:
    try:
        (_claims_dir(root, agent) / PANE_UNKNOWN_MARKER).unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        _log(f"pane-unknown marker not cleared: {exc!r}")


def _claims_for(root: Path, agent: str) -> list[dict[str, Any]]:
    directory = _claims_dir(root, agent)
    out: list[dict[str, Any]] = []
    try:
        entries = sorted(directory.glob("*.json"))
    except OSError:
        return out
    for path in entries:
        data = _read_json(path)
        if data:
            data["_path"] = str(path)
            out.append(data)
    return out


# ---------------------------------------------------------------- alerting
def alert_failure(persona: str, detail: str, *, root: Path, agent: str,
                  now: datetime | None = None,
                  route_fn: Callable[[str, str, str], None] | None = None,
                  kind: str = ALERT_KIND_FAILED) -> str:
    """Route one Recovery alert per ALERT_COOLDOWN_SECONDS per persona. Never raises.
    Returns 'routed', 'cooldown', 'disabled' or 'failed: ...'.

    ``kind`` (T-6118 R4): ALERT_KIND_FAILED says the session end was NOT archived; ALERT_KIND_DEGRADED
    says the record WAS written but something around it (group, transcript, pane log, memex
    export) is wrong. The degraded kind has its own cooldown stamp so it cannot mute a real failure."""
    try:
        current = now or _utc_now()
        degraded = kind == ALERT_KIND_DEGRADED
        # not *.json: claims are globbed by that
        stamp = _claims_dir(root, agent) / ("alert-stamp-degraded" if degraded else "alert-stamp")
        last = _parse_time(_read_json(stamp).get("last_alert_at"))
        if last is not None and (current - last).total_seconds() < ALERT_COOLDOWN_SECONDS:
            return "cooldown"
        if degraded:
            msg = (
                f"session-chronicle {persona}: session end archived, but {detail}. The restart was "
                f"not blocked. The record's .session.json carries the detail; see "
                f"~/.local/state/pantheon/chronicle/{agent}/ and re-run "
                f"`python3 ~/pantheon-config/tools/session_chronicle.py coverage --persona {persona}` as that seat."
            )[:1500]
        else:
            msg = (
                f"session-chronicle {persona}: a session end was NOT archived to the Chronicle "
                f"({detail}). The restart was not blocked. Check "
                f"~/.local/state/pantheon/chronicle/{agent}/.claims and re-run "
                f"`python3 ~/pantheon-config/tools/session_chronicle.py coverage --persona {persona}` as that seat."
            )[:1500]
        if route_fn is None:
            if os.environ.get(ALERT_ENV, "on") == "off":
                _log(f"alert suppressed by {ALERT_ENV}=off: {msg}")
                return "disabled"
            from tools.inbox_consumer import route_failure_alert as route_fn  # lazy, like the monitors
        route_fn(persona, msg, ALERT_VOCAB)
        try:
            stamp.parent.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(stamp, {"last_alert_at": _iso(current)})
        except Exception:  # noqa: BLE001 -- the stamp is best-effort
            pass
        return "routed"
    except Exception as exc:  # noqa: BLE001 -- alerting is best-effort
        _log(f"alert routing failed for {persona}: {exc!r}")
        return f"failed: {type(exc).__name__}"


# ---------------------------------------------------------------- the duty
@dataclass
class SessionEndRequest:
    persona: str
    cause: str
    account_home: Path = field(default_factory=Path.home)
    handoff_file: Path | None = None
    harness: str | None = None
    tmux_target: str | None = None
    tmux_socket: str | None = None
    stage: bool = False             # rename the live pane log (the session is ending now)
    defer_pane_log: bool = False    # record now, leave the live log for the next run to attach
    sources: tuple[Path, ...] = ()  # explicit staged logs (legacy restart script)
    uuid: str | None = None
    handoff_fresh: bool | None = None
    entrypoint: str = "cli"
    certain: bool = False           # the caller KNOWS a session ended now (restart scripts)
    rt_pinned: bool = False         # rt_started_at below names the ending session (captured
    rt_started_at: str | None = None  # BEFORE a restart rewrote runtime.json; review B1)
    stage_if_dead: bool = False     # H2: touch nothing while the pane runs a live process (M1)
    pane_stamps: tuple[str, ...] = ()  # first stamps of `sources`, captured before a restart (T-5019)
    cwd: Path | None = None         # launch cwd: scopes the claude transcript lookup
    native_transcript: dict | None = None  # T-6115: caller-archived pointer, recorded instead of the resolver's
    archive_group: str | None = chronicle.NATIVE_TRANSCRIPT_GROUP  # T-6118: group of the files the memex export hardlinks
    export_root: Path | None = None  # T-6118: memex export tree (kwarg > env > default)
    export_seat: str | None = None   # T-6118: override the resolved export seat dir
    session_id: str | None = None    # T-6118: the supervisor's codex thread id (picks the rollout)
    transcript_path: Path | None = None  # T-6302: archive THIS transcript, not the resolver's pick (backfill)
    backfill: bool = False           # T-6302: a dead session archived after the fact (recorded in the meta)
    state_root: Path | None = None
    chronicle_root: Path | None = None
    log_dir: Path | None = None


def _handoff_snapshot(path: Path, session_start: datetime | None) -> tuple[str, bool, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return (f"# (no HANDOFF at session end: {path})\n", False, None)
    fresh = session_start is not None and mtime >= session_start
    if not text.strip():
        text = f"# (empty HANDOFF at session end: {path})\n"
    return (text, fresh, _iso(mtime))


def _transcript_pointer(harness: str | None, account_home: Path,
                        started_at: datetime | None, cwd: Path | None = None,
                        session_id: str | None = None) -> dict[str, Any] | None:
    if not harness:
        return None
    try:
        from tools.harness_adapter import integration as harness_integration

        resolver = getattr(harness_integration.integration_for(harness), "session_transcript", None)
        if resolver is None:
            return None
        hint = {"session_id": session_id} if session_id else {}  # only when known: older resolvers lack it
        found = resolver(account_home=account_home, started_at=started_at, cwd=cwd, **hint)
        if found is None:
            return None
        return _pointer_from_path(found.path, found.session_id, int(getattr(found, "candidates", 1) or 1))
    except Exception as exc:  # noqa: BLE001 -- a pointer is optional
        _log(f"transcript pointer unavailable ({harness}): {exc!r}")
        return None


def _pointer_from_path(path: Path, session_id: str | None, candidates: int = 1) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"path": str(path), "session_id": session_id, "bytes": size, "sha256": digest.hexdigest(),
            "candidates": candidates}


# ---------------------------------------------------------------- growth since the last archive (T-6302)
def _pointer_source(pointer: dict[str, Any]) -> str | None:
    """The live file a pointer names: ``source_path`` of an archived pointer, else the bare
    pointer's own path (never the archived .gz)."""
    source = pointer.get("source_path")
    if isinstance(source, str) and source:
        return source
    path = pointer.get("path")
    return path if isinstance(path, str) and path and not path.endswith(".gz") else None


def _recorded_transcript(root: Path, agent: str, claim: dict[str, Any]) -> dict[str, Any] | None:
    """What a done claim's record captured of the transcript: {bytes, source, session_id}, or None
    when it captured none (a hollow or transcript-less record has no identity to extend).
    Claims written before T-6302 carry no extent: their session.json does."""
    size = claim.get("transcript_bytes")
    source = claim.get("transcript_source")
    session_id = claim.get("transcript_session_id")
    if not isinstance(size, int):
        ts, uuid = claim.get("stem_ts"), claim.get("stem_uuid")
        if not ts or not uuid:
            return None
        meta = _read_json(root / agent / str(ts)[:10] / f"{chronicle._fs_safe_ts(ts)}-{uuid}.session.json")
        pointer = meta.get("native_transcript") if isinstance(meta.get("native_transcript"), dict) else {}
        size = pointer.get("source_bytes", pointer.get("bytes"))
        source, session_id = _pointer_source(pointer), pointer.get("session_id")
    if not isinstance(size, int) or not (source or session_id):
        return None
    return {"bytes": size, "source": source, "session_id": session_id}


def _same_transcript(recorded: dict[str, Any], pointer: dict[str, Any]) -> bool:
    if recorded.get("source"):
        return recorded["source"] == _pointer_source(pointer)
    return bool(recorded.get("session_id")) and recorded.get("session_id") == pointer.get("session_id")


def _growth_extension(req: SessionEndRequest, *, root: Path, agent: str, rt: dict[str, Any],
                      rt_started: str | None, pane_stamp: str | None,
                      role: str) -> dict[str, Any] | None:
    """T-6302: a done claim says a session was archived, not how much of it. When the session's
    transcript now holds more than its records captured (the host died after a handoff-time
    archive, so nothing ever archived the rest), return what an EXTENSION record needs: its own
    claim key, the grown pointer and the record it extends. None when the session was never
    archived (the normal path writes it), nothing grew, or the transcript cannot be seen."""
    if role != "session" or rt_started is None or req.native_transcript is not None:
        return None
    done = [c for c in _claims_for(root, agent) if c.get("state") == "done" and c.get("rt_started_at") == rt_started]
    if not done:
        return None
    session_start = _parse_time(rt_started) or _parse_time(pane_stamp)
    if req.transcript_path is not None:
        pointer = _pointer_from_path(req.transcript_path, req.session_id)
    else:
        harness = req.harness or rt.get("harness")
        pointer = _transcript_pointer(harness, req.account_home, session_start, req.cwd, req.session_id)
    if not pointer or not pointer.get("path"):
        return None
    # Only the SAME transcript, grown, is an extension. A claim that captured no transcript (hollow),
    # or a different file (a successor's, a thread rotation's), is never extended, however large.
    matching = [(c, r) for c in done if (r := _recorded_transcript(root, agent, c)) is not None
                and _same_transcript(r, pointer)]
    if not matching or int(pointer.get("bytes") or 0) <= max(r["bytes"] for _c, r in matching):
        return None
    previous = max((c for c, _r in matching), key=lambda c: str(c.get("stem_ts") or c.get("at") or ""))
    key = _session_key(req.persona, rt_started, f"{pane_stamp or '-'}|ext|{pointer.get('bytes')}")
    return {"key": key, "pointer": pointer,
            "extends": {"stem_ts": previous.get("stem_ts"), "stem_uuid": previous.get("stem_uuid")},
            "claims": [c.get("_path") for c, _r in matching if c.get("_path")]}


def _transcript_end_time(pointer: dict[str, Any] | None, fallback: datetime) -> str:
    try:
        source = Path(str((pointer or {}).get("source_path") or (pointer or {})["path"]))
        return _iso(datetime.fromtimestamp(source.stat().st_mtime, tz=timezone.utc))
    except (OSError, KeyError):
        return _iso(fallback)


HOLLOW_MAX_LIFETIME_SECONDS = 120   # T-6118 PR-4: a session this short, with nothing to archive, is hollow
SUMMARY_TAIL_BYTES = 4 << 20        # the synthesized summary reads only the transcript tail
SUMMARY_MAX_TURNS = 5               # last N assistant texts and last N user prompts
SUMMARY_TEXT_CLIP = 2000            # chars per text
SUMMARY_MAX_BYTES = 16 * 1024       # hard cap on the whole summary file
SUMMARY_SYNTHESIZER = "session_chronicle/v1"
_SUMMARY_CONTEXT_PREFIXES = ("<environment_context>", "<user_instructions>", "<INSTRUCTIONS>",
                             "# AGENTS.md instructions")  # codex injects these as user messages

# ---------------------------------------------------------------- synthesized summary (T-6118 PR-4)
def _block_texts(content: Any, want: str) -> list[str] | None:
    """Text of the ``want`` blocks in a message ``content``. None when the content is a tool_result
    carrier (it holds no human text). A bare string is one text."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return None
        if block.get("type") == want and isinstance(block.get("text"), str):
            out.append(block["text"])
    return out


def _turn_of(obj: Any) -> tuple[str, str] | None:
    """(role, text) for one transcript line, or None. Claude jsonl (type user/assistant, text blocks only,
    no sidechains) and codex rollouts (response_item/message). Codex also logs event_msg
    agent_message/user_message duplicates of the same turns; only response_item is read, so nothing is
    counted twice."""
    if not isinstance(obj, dict):
        return None
    kind = obj.get("type")
    if kind in ("user", "assistant"):
        message = obj.get("message")
        if obj.get("isSidechain") is True or obj.get("isMeta") is True or not isinstance(message, dict):
            return None
        texts = _block_texts(message.get("content"), "text")
        role = kind
    elif kind == "response_item":
        payload = obj.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message":
            return None
        role = payload.get("role")
        if role not in ("user", "assistant"):
            return None
        texts = _block_texts(payload.get("content"), "output_text" if role == "assistant" else "input_text")
    else:
        return None
    text = "\n".join(t.strip() for t in (texts or []) if t.strip()).strip()
    if not text or (role == "user" and text.startswith(_SUMMARY_CONTEXT_PREFIXES)):
        return None
    return role, text


def _clip(text: str, limit: int = SUMMARY_TEXT_CLIP) -> str:
    return text if len(text) <= limit else text[:limit] + " ...[clipped]"


def synthesize_summary(source: Path, *, persona: str, session_start: datetime | None, ended_at: datetime,
                       handoff_mtime: str | None, sha256: str | None) -> tuple[str, int]:
    """(summary text, tail bytes read). Deterministic: no model, no network, no clock; the same bytes and
    arguments always give the same text. Reads only the last SUMMARY_TAIL_BYTES of ``source`` and drops
    the partial first line; bad lines are skipped."""
    with source.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        start = max(0, size - SUMMARY_TAIL_BYTES)
        handle.seek(start)
        data = handle.read(SUMMARY_TAIL_BYTES)
    tail_bytes = len(data)
    if start > 0:
        cut = data.find(b"\n")
        data = b"" if cut < 0 else data[cut + 1:]
    assistants: list[str] = []
    users: list[str] = []
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            turn = _turn_of(json.loads(raw))
        except (ValueError, RecursionError):
            continue
        if turn is not None:
            (assistants if turn[0] == "assistant" else users).append(turn[1])
    header = "\n".join([
        "---", "summary_source: synthesized", f"synthesizer: {SUMMARY_SYNTHESIZER}", f"persona: {persona}",
        f"session_started_at: {_iso(session_start) if session_start else 'null'}",
        f"ended_at: {_iso(ended_at)}", f"transcript_source: {source.name}",
        f"transcript_sha256: {sha256 or 'null'}", f"tail_bytes: {tail_bytes}",
        f"handoff_mtime: {handoff_mtime or 'null'}", "---", ""])
    if assistants or users:
        # assistant messages first: the hard cap then trims the less useful tail (the older prompts)
        parts = ["\n## Last assistant messages\n"] + [f"\n{_clip(t)}\n" for t in assistants[-SUMMARY_MAX_TURNS:]]
        parts += ["\n## Last user prompts\n"] + [f"\n{_clip(t)}\n" for t in users[-SUMMARY_MAX_TURNS:]]
        body = "".join(parts)
    else:
        body = "\n(no assistant/user text in transcript tail)\n"
    text = header + chronicle.scrub_credentials(body)
    raw_out = text.encode("utf-8")
    if len(raw_out) > SUMMARY_MAX_BYTES:
        marker = "\n[truncated]\n"
        room = SUMMARY_MAX_BYTES - len(marker.encode("utf-8"))
        text = raw_out[:room].decode("utf-8", "ignore") + marker  # "ignore" drops a split char: a char boundary
    return text, tail_bytes


def _summary_source(pointer: dict[str, Any] | None) -> tuple[Path, str | None] | None:
    """The raw native transcript a summary can be read from, and the sha256 the resolver recorded for it."""
    if not pointer:
        return None
    raw = pointer.get("source_path")
    sha = pointer.get("source_sha256")
    if not raw and str(pointer.get("path") or "").endswith(".jsonl"):
        raw, sha = pointer.get("path"), pointer.get("sha256")
    if not raw:
        return None
    path = Path(str(raw))
    return (path, str(sha) if sha else None) if path.is_file() else None


def _is_hollow(*, role: str, source: Path | None, req: SessionEndRequest, pointer: dict[str, Any] | None,
               fresh: bool, session_start: datetime | None, now: datetime) -> bool:
    """T-6118 PR-4: nothing to archive and nothing lived: no pane log, no transcript, a stale HANDOFF,
    a KNOWN start and a lifetime under HOLLOW_MAX_LIFETIME_SECONDS. An unknown start is never hollow."""
    if role != "session" or source is not None or fresh or session_start is None:
        return False
    if req.native_transcript is not None or (pointer and pointer.get("path")):
        return False
    return 0 <= (now - session_start).total_seconds() < HOLLOW_MAX_LIFETIME_SECONDS


def _session_key(persona: str, rt_started_at: str | None, pane_stamp: str | None) -> str:
    raw = f"{persona}|{rt_started_at or '-'}|{pane_stamp or '-'}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _swap_entry(agent: str, root: Path, session_start: datetime | None) -> dict | None:
    """A planned/forced swap archive written for THIS session (at or after its start)."""
    if session_start is None:
        return None
    best = None
    for entry in chronicle.entries(agent, root=root):
        if entry.get("reason") not in SWAP_REASONS:
            continue
        at = _parse_time(entry.get("ts"))
        if at is not None and at >= session_start and (best is None or entry["ts"] > best["ts"]):
            best = entry
    return best


def _staged_name(live: Path) -> Path:
    staged = live.with_name(live.name + ".dying")
    n = 2
    while staged.exists():
        staged = live.with_name(f"{live.name}.dying.{n}")
        n += 1
    return staged


def _candidate_sources(req: SessionEndRequest, log_dir: Path) -> tuple[list[tuple[Path, str]], list[str]]:
    """[(path, role)] where role is 'session' (the ending session's log) or
    'orphan' (a log staged earlier and never archived)."""
    notes: list[str] = []
    live = log_dir / f"pane-timed-{req.persona}.log"
    if req.sources:
        # Explicit staged sources (the legacy restart script): only these, never
        # the live log, and nothing at all once another writer consumed them.
        return [(Path(p), "session") for p in req.sources if _nonempty(Path(p))], notes
    staged = sorted(p for p in log_dir.glob(f"pane-timed-{req.persona}.log.dying*") if _nonempty(p))
    out: list[tuple[Path, str]] = []
    if req.stage and not req.defer_pane_log and _nonempty(live):
        try:
            target = _staged_name(live)
            os.rename(live, target)
            out.append((target, "session"))
            notes.append(f"staged {live.name} -> {target.name}")
        except OSError as exc:
            notes.append(f"stage failed: {exc!r}")
            out.append((live, "session"))
    elif req.defer_pane_log and _nonempty(live):
        out.append((live, "session"))  # recorded, never consumed here (attached by a later stage run)
    # Review B1: any other caller leaves the live log alone. Only a caller that
    # is ending the pane's session NOW (--stage) may take it; a non-stage caller
    # running after a restart would otherwise consume the SUCCESSOR's log.
    # Earlier-staged logs: when nothing else names the session they ARE it
    # (the legacy script staged before restarting); otherwise orphans.
    for path in staged:
        out.append((path, "session" if not out else "orphan"))
    return out, notes


ARCHIVED_HARNESSES = ("claude", "codex")  # the archivist contract: claude jsonl + codex rollouts only
SESSION_END_GZ_LEVEL = 6
EXPORT_MODE = 0o640


def _group_for_export(req: SessionEndRequest) -> tuple[str | None, str | None]:
    """(group, error): the export group when this account can chown to it, else (None, why)."""
    if req.archive_group:
        # T-6118 R2: archive_group_problem answers None for euid 0 even for a group that does not exist,
        # and _resolve_gid would then raise a non-permission error that loses the record.
        import grp

        try:
            grp.getgrnam(req.archive_group)
        except KeyError:
            return None, f"archive group {req.archive_group!r} does not exist"
    problem = chronicle.archive_group_problem(req.archive_group)
    if problem is not None:
        return None, problem
    return req.archive_group, None


def _is_permission_failure(exc: BaseException) -> bool:
    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, PermissionError):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _gz_sha_matches(path: Path, want_sha: str) -> bool:
    """True when ``path`` decompresses and the sha256 of the decompressed bytes is ``want_sha``."""
    import gzip

    try:
        digest = hashlib.sha256()
        with gzip.open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest() == want_sha
    except (OSError, EOFError, gzip.BadGzipFile):
        return False


def _reusable_transcript_gz(agent: str, root: Path, stem_ts: str, stem_uuid: str,
                            want_sha: str) -> Path | None:
    """An earlier session-end .gz for this stem whose decompressed bytes are the resolver's source."""
    day_dir = root / agent / stem_ts[:10]
    base = f"{chronicle._fs_safe_ts(stem_ts)}-{stem_uuid}.transcript.jsonl"
    candidates = [day_dir / f"{base}.gz"] + [day_dir / f"{base}.{n}.gz" for n in range(2, 12)]
    for cand in candidates:
        if cand.is_file() and _gz_sha_matches(cand, want_sha):
            return cand
    return None


def _archive_session_transcript(pointer: dict[str, Any], *, agent: str, root: Path, stem_ts: str,
                                stem_uuid: str, group: str | None,
                                notes: dict[str, str]) -> dict[str, Any]:
    """Compress the resolver's transcript beside the record and return the archived pointer.
    A group PermissionError retries ungrouped (noting ``group_error``); other errors propagate."""
    src = Path(str(pointer["path"]))
    want_sha = str(pointer.get("sha256") or "")
    gz = _reusable_transcript_gz(agent, root, stem_ts, stem_uuid, want_sha) if want_sha else None
    if gz is not None:
        data = hashlib.sha256()
        with gz.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                data.update(block)
        archived = {"path": str(gz), "bytes": gz.stat().st_size, "sha256": data.hexdigest(),
                    "source_bytes": pointer.get("bytes")}
    else:
        try:
            archived = chronicle.archive_native_transcript(
                agent, src, jsonl_uuid=stem_uuid, ts=stem_ts, root=root, group=group,
                compresslevel=SESSION_END_GZ_LEVEL)
        except chronicle.ChronicleDurabilityError as exc:
            if group is None or not _is_permission_failure(exc):
                raise
            notes["group_error"] = f"{type(exc).__name__}: {chronicle.scrub_credentials(str(exc))}"[:300]
            archived = chronicle.archive_native_transcript(
                agent, src, jsonl_uuid=stem_uuid, ts=stem_ts, root=root, group=None,
                compresslevel=SESSION_END_GZ_LEVEL)
    # T-6118 R3: the resolver hashed the source earlier; archive_native_transcript hashed the bytes it
    # actually compressed. Record THOSE, and say so when the live transcript grew in between.
    source_sha = archived.get("source_sha256") or pointer.get("sha256")
    source_bytes = archived.get("source_bytes", pointer.get("bytes"))
    out = {"path": archived["path"], "bytes": archived["bytes"], "sha256": archived["sha256"],
           "source_path": str(src), "session_id": pointer.get("session_id"),
           "source_bytes": source_bytes, "source_sha256": source_sha,
           "candidates": pointer.get("candidates", 1)}
    if want_sha and source_sha != want_sha:
        out["source_changed_during_archive"] = True
    return out


def _transcript_gz_of(pointer: dict[str, Any] | None, root: Path, agent: str) -> Path | None:
    """The archived ``.transcript.jsonl.gz`` the pointer names, when it sits under ``root/agent``
    (a bare resolver pointer names the raw jsonl and has nothing to export)."""
    if not pointer or not pointer.get("path"):
        return None
    gz = Path(str(pointer["path"]))
    try:
        if not chronicle_export.is_transcript_gz(gz.name) or not gz.is_file() or gz.is_symlink():
            return None
        gz.resolve().relative_to((root / agent).resolve())
    except (OSError, ValueError):
        return None
    return gz


def _transcript_absent_reason(pointer: dict[str, Any] | None, harness: str | None) -> str:
    """Why a record has no archived transcript .gz: the harness is not archived (hermes...), the
    resolver found nothing, or a pointer exists but its archive is not there (the archive failed)."""
    if harness and harness not in ARCHIVED_HARNESSES:
        return "harness-not-archived"
    if not pointer or not (pointer.get("path") or pointer.get("source_path")):
        return "none-resolved"
    return "archive-failed"


def _export_stage1(req: SessionEndRequest, *, agent: str, root: Path, harness: str | None,
                   stem_ts: str, stem_uuid: str, record_path: Path | None, pointer: dict[str, Any] | None,
                   alert_fn: Callable[..., str], result: dict[str, Any],
                   summary_path: Path | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], Path | None, str]:
    """Hardlink the transcript .gz, the .md and the synthesized .summary.md (if any) into the memex export tree (T-6118). A record with no
    archived transcript (HANDOFF-only) exports its .md alone, unless that .md is byte-identical to the
    seat's most recently exported one (status handoff-identical: nothing is written, nothing alerts). With a
    synthesized summary, an identical .md is left out and the rest ships (meta carries ``identical_to``).
    Fail-open: any problem is recorded and alerted, never raised.
    Returns (export meta, supersedes, gz, date)."""
    date = stem_ts[:10]
    ex_root = chronicle_export.export_root(req.export_root)
    gz = _transcript_gz_of(pointer, root, agent)
    seat, why = (req.export_seat, None) if req.export_seat else chronicle_export.resolve_seat(
        persona=req.persona, harness=harness, account_home=req.account_home)
    if seat is None:
        meta = {"status": chronicle_export.STATUS_SEAT_UNRESOLVED, "seat": None, "date": date, "files": [],
                "root": str(ex_root), "detail": why}
        result["alert_export"] = alert_fn(f"memex export seat unresolved: {why}", kind=ALERT_KIND_DEGRADED)
        return meta, [], gz, date
    if gz is None and record_path is None:
        return {"status": chronicle_export.STATUS_SKIPPED, "seat": seat, "date": date, "files": [],
                "root": str(ex_root), "detail": "no-md"}, [], None, date
    # Handoff dedupe (archivist decision 4): a .md byte-identical to the seat's newest committed export
    # is not re-exported, whether the record is transcript-less (nothing else to ship: handoff-identical)
    # or carries a synthesized summary (the rest still ships; the twin is recorded as identical_to).
    # A record with a transcript but no summary (fresh HANDOFF) always ships its .md (PR-2 contract).
    twin = None
    if record_path is not None and (gz is None or summary_path is not None):
        twin = chronicle_export.identical_to_newest(ex_root, seat, record_path, date=date)
    if twin is not None and gz is None and summary_path is None:
        return {"status": chronicle_export.STATUS_HANDOFF_IDENTICAL, "seat": seat, "date": date,
                "files": [], "root": str(ex_root), "detail": None, "identical_to": twin}, [], None, date
    outcome = chronicle_export.export_files(
        (gz, None if twin is not None else record_path, summary_path), root=ex_root, seat=seat, date=date)
    supersedes: list[dict[str, Any]] = []
    gz_in = gz is not None and any(f.get("name") == gz.name for f in outcome.files)
    if gz_in and pointer and pointer.get("source_path"):
        try:
            supersedes, sdetail = chronicle_export.find_supersedes(
                agent=agent, chron_root=root, export_root_=ex_root, new_gz=gz,
                source_path=str(pointer["source_path"]), skip_stem=f"{chronicle._fs_safe_ts(stem_ts)}-{stem_uuid}")
            if sdetail:
                outcome.detail = "; ".join(x for x in (outcome.detail, sdetail) if x)
        except Exception as exc:  # noqa: BLE001 -- fail-open: ship both
            outcome.detail = "; ".join(x for x in (outcome.detail, f"supersede scan failed: {exc!r}") if x)[:600]
    if outcome.status in chronicle_export.ALERT_STATUSES:
        result["alert_export"] = alert_fn(f"memex export {outcome.status}: {outcome.detail}", kind=ALERT_KIND_DEGRADED)
    export_meta = outcome.as_meta()
    if twin is not None:
        export_meta["identical_to"] = twin
    return export_meta, supersedes, gz, date


def _export_stage2(req: SessionEndRequest, export_meta: dict[str, Any], meta_path: Path | None, *,
                   alert_fn: Callable[..., str], result: dict[str, Any]) -> None:
    """Link the ``.session.json`` LAST: it is the commit marker (an ingester that sees it sees the
    rest). Only after a full stage 1. A failure alerts and is recorded; it never raises."""
    if export_meta.get("status") != chronicle_export.STATUS_EXPORTED or meta_path is None:
        return
    outcome = chronicle_export.link_one(meta_path, root=export_meta["root"], seat=export_meta["seat"],
                                        date=export_meta["date"])
    if outcome.status == chronicle_export.STATUS_EXPORTED:
        result["export_session_json"] = outcome.files[0]["state"]
        return
    result["export_session_json"] = "failed"
    result["alert_export"] = alert_fn(f"memex export of {meta_path.name} {outcome.status}: {outcome.detail}",
                                      kind=ALERT_KIND_DEGRADED)


def _archive_one(req: SessionEndRequest, *, root: Path, agent: str, rt: dict[str, Any],
                 source: Path | None, role: str, now: datetime,
                 alert_fn: Callable[..., str]) -> dict[str, Any]:  # alert_fn(detail, kind=ALERT_KIND_*)
    rt_started = rt.get("started_at") if isinstance(rt.get("started_at"), str) else None
    pane_stamp = _pane_first_stamp(source) if source is not None else None
    if role == "orphan":
        rt_started = None  # an orphan log predates the recorded runtime
    key = _session_key(req.persona, rt_started, pane_stamp)
    claims = _claims_dir(root, agent)
    claims.mkdir(parents=True, exist_ok=True)
    claim_path = claims / f"{key}.json"
    result: dict[str, Any] = {"key": key, "source": str(source) if source else None}

    ext = _growth_extension(req, root=root, agent=agent, rt=rt, rt_started=rt_started,
                            pane_stamp=pane_stamp, role=role)
    if ext is not None:
        key = ext["key"]
        claim_path = claims / f"{key}.json"
        result["key"] = key
        result["extends"] = ext["extends"]

    if (ext is None and source is not None and pane_stamp and role == "session" and rt_started is not None
            and rt.get(ACTIVE_ABSENT_KEY)):
        # T-5019 rebound MAJOR 1: on a stale-record seat an earlier call (H2, a
        # fleet-reboot pause) keyed THIS log's session on active-runtime.json's
        # started_ts; the controller has since cleared that file, so this call
        # only sees the stale runtime.json value. Adopt that claim: done is a
        # no-op, pending attaches the log, failed is retried under its stem.
        adopted = _stale_record_claim(root, agent, rt_started, pane_stamp)
        if adopted is not None:
            claim_path = Path(adopted["_path"])
            key = str(adopted.get("key") or claim_path.stem)
            if isinstance(adopted.get("rt_started_at"), str):
                rt_started = adopted["rt_started_at"]
            result["key"] = key

    if ext is None and req.rt_pinned and req.rt_started_at is None and role == "session":
        # T-5019 item 2: the restart script could not capture the ending
        # session's identity, so its key differs from the one the controller's
        # H1 used for the same staged log. A done claim for the log's first
        # stamp (read now, or captured by the script before H1 consumed the
        # file) means that session end is already archived.
        stamps = [pane_stamp] if pane_stamp else [s for s in req.pane_stamps if s]
        for stamp in stamps:
            matched = _done_claim_for_stamp(root, agent, stamp)
            if matched is None:
                continue
            if matched.get("pane_log") == "pending" and source is not None:
                # Rebound MINOR 2: archived, but its pane log is still owed.
                return _attach_pending(req, Path(matched["_path"]), source, agent=agent, root=root,
                                       alert_fn=alert_fn, result=result)
            result["status"] = "already-archived"
            return result

    if source is None and req.certain and not (req.stage or req.defer_pane_log or req.rt_pinned):
        # Review B1: a caller that is not ending the pane's session now and did
        # not pin the ending session's identity may be running AFTER a restart,
        # when runtime.json already names the successor. Refuse rather than
        # file a record (and a done claim) against the wrong session.
        result["status"] = "unpinned-refused"
        _log(f"{req.persona}: --certain without --stage/--defer-pane-log/--rt-started-at; not archiving")
        return result
    if source is None:
        # No pane log: archive only if no claim already covers this runtime record.
        if rt_started is None and not req.certain:
            result["status"] = "no-session"
            return result
        if rt_started is None:
            # A restart script ended a session that left no identity (no
            # runtime.json, no pane log): one record for this call.
            key = _session_key(req.persona, f"certain:{_iso(now)}", None)
            claim_path = claims / f"{key}.json"
            result["key"] = key
        active_absent = bool(rt.get(ACTIVE_ABSENT_KEY))
        existing_claims = _claims_for(root, agent) if ext is None else []  # an extension is judged on its own key
        for claim in existing_claims:
            if claim.get("state") != "done":
                continue
            if claim.get("rt_started_at") == rt_started:
                result["status"] = "already-archived"
                return result
            if active_absent and rt_started is not None and claim.get(RECORD_CLAIM_FIELD) == rt_started:
                # T-5019 item 5: H2 archived this session under active-runtime.json's
                # started_ts (the record was stale); the controller cleared that
                # file afterwards, so this call only sees the stale record value.
                result["status"] = "already-archived"
                return result
        for claim in existing_claims:
            if (claim.get("state") == "claimed" and rt_started is not None
                    and claim.get("rt_started_at") == rt_started and _claim_is_fresh(claim, now)):
                # T-5037: another writer is archiving this session right now under
                # its pane-log key (it already moved the staged log, which is why
                # this call has none). Checking only done claims let both file a
                # record, the two-record CI flake. Done claims are checked first,
                # so an archived session still reads as already-archived.
                result["status"] = "busy"
                result["busy_holder"] = {"pid": claim.get("pid"), "at": claim.get("at")}
                _log(f"{req.persona}: session {rt_started} is being archived by pid "
                     f"{claim.get('pid')} under key {claim.get('key')}; not archiving it twice")
                return result

    state, previous = _try_claim(claim_path, now, identity={
        "key": key, "persona": req.persona, "rt_started_at": rt_started, "pane_first_stamp": pane_stamp,
        "cause": "orphan-staged" if role == "orphan" else req.cause,
    })
    if state in ("done", "busy"):
        result["status"] = "already-archived" if state == "done" else "busy"
        if state == "busy":
            result["busy_holder"] = {"pid": previous.get("pid"), "at": previous.get("at")}
            _log(f"{req.persona}: session {key} is being archived by pid {previous.get('pid')} "
                 f"(claimed {previous.get('at')}); not archiving it twice")
        return result
    if state == "pending":
        return _attach_pending(req, claim_path, source, agent=agent, root=root, alert_fn=alert_fn, result=result)

    session_start = _parse_time(rt_started) or _parse_time(pane_stamp)
    cause = "orphan-staged" if role == "orphan" else req.cause
    claim_doc: dict[str, Any] = {
        "schema": SCHEMA, "state": "claimed", "key": key, "persona": req.persona,
        "rt_started_at": rt_started, "pane_first_stamp": pane_stamp, "cause": cause,
        "at": _iso(now), "pid": os.getpid(),
    }
    record_started = rt.get(RECORD_STARTED_KEY)
    if role == "session" and record_started and record_started != rt_started:
        # T-5019 item 5: only on a stale record, so healthy claim docs are unchanged.
        claim_doc[RECORD_CLAIM_FIELD] = record_started
    try:
        group, group_error = _group_for_export(req)  # T-6118 R5: inside the try, a raise fails the claim
        if group_error is not None:
            result["alert_group"] = alert_fn(f"chronicle export group unavailable: {group_error}",
                                             kind=ALERT_KIND_DEGRADED)
        handoff = req.handoff_file or (req.account_home / "HANDOFF.md")
        if req.backfill:
            # T-6302: the seat's current HANDOFF.md belongs to the live successor, not this dead session.
            text, fresh, handoff_mtime, handoff = BACKFILL_HANDOFF_TEXT, False, None, None
        else:
            text, fresh, handoff_mtime = _handoff_snapshot(Path(handoff), session_start)
        if req.handoff_fresh is not None and role == "session":
            fresh = req.handoff_fresh
        harness = req.harness or (rt.get("harness") if role == "session" else None)
        if req.native_transcript is not None and role == "session":
            pointer = dict(req.native_transcript)  # T-6115: the caller already archived the exact transcript
        elif ext is not None:
            pointer = ext["pointer"]
        elif req.transcript_path is not None and role == "session":
            pointer = _pointer_from_path(req.transcript_path, req.session_id)  # T-6302: a named dead session
        else:
            pointer = _transcript_pointer(harness, req.account_home, session_start, req.cwd, req.session_id) \
                if role == "session" else None
        # An optional pointer names the stem only when it is unambiguous (MINOR 2).
        pointer_id = (pointer or {}).get("session_id") if (pointer or {}).get("candidates", 1) == 1 else None
        uuid = (req.uuid if req.uuid and req.uuid != "unknown" else None) or pointer_id or f"rt-{key[:12]}"
        uuid = re.sub(r"[^A-Za-z0-9._-]", "_", str(uuid))[:96]

        swap = _swap_entry(agent, root, session_start) if role == "session" and ext is None else None
        reused = _predecessor_record(agent, root, previous) if ext is None else None  # an extension is a NEW stem
        fresh_stem = False   # a newly planned stem: neither swap-paired nor a reused predecessor's
        hollow = False
        if swap is not None:
            stem_ts, stem_uuid = swap["ts"], swap["uuid"]
            record_path = chronicle.snapshot_path(agent, swap, root=root)
            paired = True
        elif reused is not None:
            # MINOR 4: a predecessor was killed after writing the record but
            # before marking the claim done. Finish ITS record; no second line.
            stem_ts, stem_uuid = reused["ts"], reused["uuid"]
            record_path = chronicle.snapshot_path(agent, reused, root=root)
            paired = False
            result["reused_record"] = True
            claim_doc.update({"stem_ts": stem_ts, "stem_uuid": stem_uuid})  # T-5019 item 4
        else:
            stem_ts, stem_uuid = _iso(now), uuid
            # Persist the planned stem BEFORE writing, so a re-claim after a
            # kill can find the record instead of appending a duplicate.
            # T-5019 item 4: on claim_doc itself, so a FAILED doc (written by
            # the handler below) still carries it for the retry to reuse.
            claim_doc.update({"stem_ts": stem_ts, "stem_uuid": stem_uuid})
            _write_json_atomic(claim_path, claim_doc)
            fresh_stem = True
            paired = False
            # T-6118 PR-4 AMENDS T-5016's every-session-end-gets-a-full-record rule (the operator TG 11829):
            # every session END still gets a record, but a hollow end (no transcript, no pane log, stale
            # HANDOFF, lifetime <120s) gets a MINIMAL one: the done claim plus session.json hollow:true,
            # with no .md, no index line and no memex export. HO-approved 2026-11-04 on recovery's data
            # (27/36 of recovery's real records were hollow start-reconcile).
            hollow = _is_hollow(role=role, source=source, req=req, pointer=pointer, fresh=fresh,
                                session_start=session_start, now=now)
            if hollow:
                record_path = None
                result["hollow"] = True
            else:
                try:
                    record_path = chronicle.archive_required(
                        agent, text, jsonl_uuid=stem_uuid, reason=f"{REASON_PREFIX}{cause}",
                        ts=stem_ts, root=root, mode=EXPORT_MODE, group=group,
                    )
                except chronicle.ChronicleDurabilityError as exc:
                    if group is None or not _is_permission_failure(exc):
                        raise
                    group_error = f"{type(exc).__name__}: {chronicle.scrub_credentials(str(exc))}"[:300]
                    group = None
                    result["alert_group"] = alert_fn(f"chronicle export group unavailable: {group_error}",
                                                     kind=ALERT_KIND_DEGRADED)
                    record_path = chronicle.archive_required(
                        agent, text, jsonl_uuid=stem_uuid, reason=f"{REASON_PREFIX}{cause}",
                        ts=stem_ts, root=root, mode=EXPORT_MODE, group=None,
                    )

        transcript_error: str | None = None
        if (role == "session" and req.native_transcript is None and pointer
                and pointer.get("path") and harness in ARCHIVED_HARNESSES):
            notes: dict[str, str] = {}
            try:
                pointer = _archive_session_transcript(
                    pointer, agent=agent, root=root, stem_ts=stem_ts, stem_uuid=stem_uuid,
                    group=group, notes=notes)
                if "group_error" in notes and group_error is None:
                    group_error = notes["group_error"]
                    group = None  # chown is denied: do not retry it on the .session.json
                    result["alert_group"] = alert_fn(f"chronicle export group unavailable: {group_error}",
                                                 kind=ALERT_KIND_DEGRADED)
            except Exception as exc:  # noqa: BLE001 -- fail-open: keep the bare pointer
                transcript_error = f"{type(exc).__name__}: {chronicle.scrub_credentials(str(exc))}"[:300]
                result["alert_transcript"] = alert_fn(f"native transcript not archived: {transcript_error}",
                                                      kind=ALERT_KIND_DEGRADED)

        pane_state = "absent"
        if source is not None and not req.defer_pane_log:
            companion = chronicle.archive_companion(agent, source, jsonl_uuid=stem_uuid, ts=stem_ts, root=root)
            pane_state = "archived" if companion is not None else "pending"
            result["pane_log"] = str(companion) if companion else None
        elif source is not None:
            pane_state = "pending"

        # T-6118 PR-4: a stale HANDOFF on a newly written record with a transcript source gets a
        # deterministic synthesized tail summary beside the .md (never inside it). Asking the live
        # runtime for a summary before teardown is not implemented (T-6118 PR-4 scope); the
        # synthesized tail summary is the fallback.
        summary_path: Path | None = None
        summary_info: dict[str, Any] | None = None
        summary_error: str | None = None
        summary_src = _summary_source(pointer) if (fresh_stem and not hollow and not fresh) else None
        if summary_src is not None:
            try:
                body, _tail = synthesize_summary(
                    summary_src[0], persona=req.persona, session_start=session_start, ended_at=now,
                    handoff_mtime=handoff_mtime, sha256=summary_src[1])
                try:
                    summary_path = chronicle.write_companion_text(
                        agent, body, jsonl_uuid=stem_uuid, ts=stem_ts, suffix="summary.md",
                        root=root, mode=EXPORT_MODE, group=group)
                except chronicle.ChronicleDurabilityError as exc:
                    if group is None or not _is_permission_failure(exc):
                        raise
                    if group_error is None:
                        group_error = f"{type(exc).__name__}: {chronicle.scrub_credentials(str(exc))}"[:300]
                        result["alert_group"] = alert_fn(f"chronicle export group unavailable: {group_error}",
                                                         kind=ALERT_KIND_DEGRADED)
                    group = None
                    summary_path = chronicle.write_companion_text(
                        agent, body, jsonl_uuid=stem_uuid, ts=stem_ts, suffix="summary.md",
                        root=root, mode=EXPORT_MODE, group=None)
                raw_summary = summary_path.read_bytes()
                summary_info = {"source": "synthesized", "path": str(summary_path), "bytes": len(raw_summary),
                                "sha256": hashlib.sha256(raw_summary).hexdigest()}
            except Exception as exc:  # noqa: BLE001 -- fail-open: the record is written without a summary
                summary_path, summary_info = None, None
                summary_error = f"{type(exc).__name__}: {chronicle.scrub_credentials(str(exc))}"[:300]
                result["alert_summary"] = alert_fn(f"synthesized summary not written: {summary_error}",
                                                   kind=ALERT_KIND_DEGRADED)

        try:
            if hollow:
                export_meta, supersedes, stage1_gz = (
                    {"status": chronicle_export.STATUS_SKIPPED, "seat": None, "date": stem_ts[:10], "files": [],
                     "root": str(chronicle_export.export_root(req.export_root)), "detail": "hollow"}, [], None)
            else:
                export_meta, supersedes, stage1_gz, _date = _export_stage1(
                    req, agent=agent, root=root, harness=harness, stem_ts=stem_ts, stem_uuid=stem_uuid,
                    record_path=record_path, pointer=pointer, summary_path=summary_path,
                    alert_fn=alert_fn, result=result)
        except Exception as exc:  # noqa: BLE001 -- the export is fail-open
            export_meta = {"status": chronicle_export.STATUS_FAILED, "seat": None, "date": stem_ts[:10],
                           "files": [], "root": str(chronicle_export.export_root(req.export_root)),
                           "detail": f"{type(exc).__name__}: {chronicle.scrub_credentials(str(exc))}"[:300]}
            supersedes = []
            stage1_gz = _transcript_gz_of(pointer, root, agent)  # stage 1 raised before it could hand its result back
            result["alert_export"] = alert_fn(f"memex export failed: {export_meta['detail']}", kind=ALERT_KIND_DEGRADED)
        result["export"] = export_meta["status"]

        meta = {
            "schema": SCHEMA, "persona": req.persona, "agent": agent, "cause": cause,
            "session_key": key, "harness": harness,
            "profile": rt.get("profile") if role == "session" else None,
            "model": rt.get("model") if role == "session" else None,
            "tmux_target": req.tmux_target or rt.get("tmux_target"),
            "session_started_at": _iso(session_start) if session_start else None,
            "rt_started_at": rt_started, "pane_first_stamp": pane_stamp,
            "ended_at": _iso(now), "handoff_file": str(handoff) if handoff is not None else None, "handoff_fresh": fresh,
            "handoff_mtime": handoff_mtime, "native_transcript": pointer,
            "pane_log": pane_state, "paired_with_swap_entry": paired,
            "writer": {"entrypoint": req.entrypoint, "pid": os.getpid()},
            "export": export_meta, "supersedes": supersedes,
            "hollow": hollow, "summary": summary_info,
        }
        if ext is not None:
            meta["extends"] = ext["extends"]
        if req.backfill:
            meta["backfill"] = True
            meta["handoff_unavailable"] = True
        if summary_error is not None:
            meta["summary_error"] = summary_error
        if stage1_gz is not None:  # stage 1's answer: the field matches what was exported
            meta["transcript"] = "archived"
        else:
            meta["transcript"] = "absent"
            meta["transcript_absent_reason"] = _transcript_absent_reason(pointer, harness)
        if transcript_error is not None:
            meta["native_transcript_error"] = transcript_error
        if group_error is not None:
            meta["group_error"] = group_error
        meta_path: Path | None = None
        try:
            meta_path = chronicle.write_companion_text(agent, json.dumps(meta, indent=2, sort_keys=True) + "\n",
                                                       jsonl_uuid=stem_uuid, ts=stem_ts, suffix="session.json",
                                                       root=root, mode=EXPORT_MODE, group=group)
        except chronicle.ChronicleDurabilityError as exc:
            if group is None or not _is_permission_failure(exc):
                raise
            meta["group_error"] = group_error = \
                f"{type(exc).__name__}: {chronicle.scrub_credentials(str(exc))}"[:300]
            result["alert_group"] = alert_fn(f"chronicle export group unavailable: {group_error}",
                                                 kind=ALERT_KIND_DEGRADED)
            meta_path = chronicle.write_companion_text(agent, json.dumps(meta, indent=2, sort_keys=True) + "\n",
                                                       jsonl_uuid=stem_uuid, ts=stem_ts, suffix="session.json",
                                                       root=root, mode=EXPORT_MODE, group=None)
        try:
            _export_stage2(req, export_meta, meta_path, alert_fn=alert_fn, result=result)
        except Exception as exc:  # noqa: BLE001 -- fail-open: the record is written
            result["export_session_json"] = "failed"
            result["alert_export"] = alert_fn(f"memex export of the session record failed: {exc!r}"[:300],
                                              kind=ALERT_KIND_DEGRADED)
        claim_doc.update({"state": "done", "stem_ts": stem_ts, "stem_uuid": stem_uuid,
                          "record": str(record_path) if record_path is not None else None,
                          "pane_log": pane_state, "at": _iso(now),
                          "transcript_bytes": (pointer or {}).get("source_bytes", (pointer or {}).get("bytes")),
                          "transcript_source": _pointer_source(pointer or {}),
                          "transcript_session_id": (pointer or {}).get("session_id"),
                          "ended_at": _transcript_end_time(pointer, now)})
        if ext is not None:
            claim_doc["extends"] = ext["extends"]
        if hollow:
            claim_doc["hollow"] = True
        _write_json_atomic(claim_path, claim_doc)
        if ext is not None:
            _resolve_superseded_pending(ext["claims"], stem_ts=stem_ts, stem_uuid=stem_uuid)
        result.update({"status": "hollow" if hollow else ("archived" if not paired else "paired"),
                       "record": str(record_path) if record_path is not None else None,
                       "uuid": stem_uuid, "cause": cause, "pane_log_state": pane_state})
        if hollow and meta_path is not None:
            result["meta"] = str(meta_path)
        if pane_state == "pending" and not req.defer_pane_log:
            result["alert"] = alert_fn(f"pane log not archived for {source}", kind=ALERT_KIND_DEGRADED)
        return result
    except Exception as exc:  # noqa: BLE001 -- fail-open by contract
        detail = f"{type(exc).__name__}: {chronicle.scrub_credentials(str(exc))}"[:300]
        claim_doc.update({"state": "failed", "error": detail, "at": _iso(now)})
        try:
            _write_json_atomic(claim_path, claim_doc)
        except Exception:  # noqa: BLE001
            pass
        result.update({"status": "failed", "error": detail, "alert": alert_fn(detail)})
        return result


def _resolve_superseded_pending(paths: list[str], *, stem_ts: str, stem_uuid: str) -> None:
    """T-6302: an extension carries the session's pane log, so an older claim of the same session still
    owing one is settled by it instead of staying pending forever."""
    for raw in paths:
        path = Path(raw)
        doc = _read_json(path)
        if doc.get("state") == "done" and doc.get("pane_log") == "pending":
            doc.update({"pane_log": "superseded-by-extension",
                        "superseded_by": {"stem_ts": stem_ts, "stem_uuid": stem_uuid}})
            try:
                _write_json_atomic(path, doc)
            except Exception:  # noqa: BLE001 -- best effort; the old claim just stays pending
                pass


def _predecessor_record(agent: str, root: Path, previous: dict[str, Any]) -> dict | None:
    """The index entry a superseded claim had planned (stem_ts/stem_uuid), if it
    was actually written."""
    ts, uuid = previous.get("stem_ts"), previous.get("stem_uuid")
    if not ts or not uuid:
        return None
    for entry in chronicle.entries(agent, root=root):
        if entry.get("ts") == ts and entry.get("uuid") == uuid:
            return entry
    return None


def _attach_pending(req: SessionEndRequest, claim_path: Path, source: Path | None, *, agent: str,
                    root: Path, alert_fn: Callable[..., str], result: dict[str, Any]) -> dict[str, Any]:
    """A done claim whose pane log was deferred (fleet-reboot pause) or failed:
    attach the log now, under the claim's own stem."""
    claim = _read_json(claim_path)
    if source is None or req.defer_pane_log:
        result["status"] = "already-archived"
        return result
    companion = chronicle.archive_companion(agent, source, jsonl_uuid=str(claim.get("stem_uuid")),
                                            ts=str(claim.get("stem_ts")), root=root)
    if companion is None:
        result.update({"status": "failed", "error": "pending pane log not archived",
                       "alert": alert_fn(f"pending pane log not archived for {source}")})
        return result
    claim["pane_log"] = "archived"
    try:
        _write_json_atomic(claim_path, claim)
    except Exception:  # noqa: BLE001
        pass
    result.update({"status": "attached", "pane_log": str(companion)})
    return result


def session_end(req: SessionEndRequest, *, now: datetime | None = None,
                route_fn: Callable[[str, str, str], None] | None = None) -> dict[str, Any]:
    """Archive the ending session(s) for ``req.persona``. Never raises."""
    current = now or _utc_now()
    summary: dict[str, Any] = {"schema": SCHEMA, "persona": req.persona, "cause": req.cause,
                               "status": "ok", "results": [], "closed_pipe": False}
    if not valid_persona(req.persona):
        summary.update({"status": "failed", "error": f"invalid persona {req.persona!r}"[:120]})
        return summary
    try:
        root = req.chronicle_root or chronicle.chronicle_root()
        agent = chronicle.agent_dir_name(req.persona)
        state_root = req.state_root or persona_state_root()
        log_dir = req.log_dir or pane_log_dir()

        def alert_fn(detail: str, kind: str = ALERT_KIND_FAILED) -> str:
            return alert_failure(req.persona, detail, root=root, agent=agent, now=current,
                                 route_fn=route_fn, kind=kind)

        pane_state = _pane_dead_state(req.tmux_target, req.tmux_socket) if req.stage_if_dead else "dead"
        if pane_state == "live":
            # Review M1: the pane runs a live process, so whatever ended is not
            # in it any more (an operator swap put the destination there and H1
            # already archived the source). Touch nothing.
            summary["results"].append({"status": "pane-live-skip"})
            return summary
        if pane_state == "unknown":
            # T-5019 item 3: a hung tmux cannot say whether the pane is dead, so
            # the pane may still hold a live session's tap. Touch nothing; the
            # next start's H1 archives whatever did end.
            _log(f"{req.persona}: pane_dead query timed out; not staging")
            _mark_pane_unknown(root, agent, current)  # rebound MINOR 3: coverage shows it
            summary["results"].append({"status": "pane-unknown-skip"})
            return summary
        if req.cause == "start":
            dead = _pane_query(req.tmux_target, req.tmux_socket, "#{pane_dead}")
            req.cause = "replace" if dead == "0" else "start-reconcile"
            summary["cause"] = req.cause
        if req.stage and not req.defer_pane_log:
            summary["closed_pipe"] = close_pane_pipe(req.tmux_target, req.tmux_socket)
        rt = _effective_runtime_record(req.persona, state_root)
        if req.rt_pinned:
            # Review B1: the caller captured the ENDING session's runtime record
            # before its restart; a newer runtime.json belongs to the successor.
            if rt.get("started_at") != req.rt_started_at:
                rt = {"started_at": req.rt_started_at} if req.rt_started_at else {}
        sources, notes = _candidate_sources(req, log_dir)
        summary["notes"] = notes
        if not sources:
            summary["results"].append(_archive_one(req, root=root, agent=agent, rt=rt, source=None,
                                                   role="session", now=current, alert_fn=alert_fn))
        for path, role in sources:
            summary["results"].append(_archive_one(req, root=root, agent=agent, rt=rt, source=path,
                                                   role=role, now=current, alert_fn=alert_fn))
        if any(r.get("status") == "failed" for r in summary["results"]):
            summary["status"] = "failed"
        elif not req.defer_pane_log:
            # Rebound MINOR 3: a run that could see the pane and archived (or
            # found archived) what ended resolves an earlier unknown-pane skip.
            _clear_pane_unknown(root, agent)
    except Exception as exc:  # noqa: BLE001 -- fail-open by contract
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"[:300]
        try:
            root = req.chronicle_root or chronicle.chronicle_root()
            summary["alert"] = alert_failure(req.persona, summary["error"], root=root,
                                             agent=chronicle.agent_dir_name(req.persona),
                                             now=current, route_fn=route_fn)
        except Exception:  # noqa: BLE001
            pass
    return summary


# ---------------------------------------------------------------- backfill of one dead session (T-6302)
BACKFILL_MIN_QUIET_SECONDS = 300.0
BACKFILL_HANDOFF_TEXT = "# (HANDOFF unavailable: this session was archived after it ended; the seat's current HANDOFF is its successor's)\n"
_BACKFILL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{3,95}$")
BACKFILL_HARNESSES = ("claude", "codex")


def _find_session_transcript(harness: str, account_home: Path, session_id: str) -> tuple[Path | None, str | None]:
    """(path, problem): the one transcript file the harness wrote for ``session_id``. Never guesses:
    no match or several matches is a refusal, not a pick by recency."""
    if harness == "claude":
        hits = sorted((account_home / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))
    else:
        hits = sorted((account_home / ".codex" / "sessions").glob(f"*/*/*/rollout-*-{session_id}.jsonl"))
    if not hits:
        return None, "transcript-not-found"
    if len(hits) > 1:
        return None, "transcript-ambiguous"
    return hits[0], None


LIVE_ID_KEYS = ("session_id", "thread_id", "native_session_id")


def _live_session_ids(record: dict[str, Any], active_path: Path, pane_pid: str | None,
                      proc_root: Path) -> set[str]:
    """Every harness session id the seat's live state names: the runtime record, active-runtime.json,
    and the argv of the pane's process (a harness started with --session-id/resume carries it there)."""
    ids: set[str] = set()
    for doc in (record, _read_json(active_path)):
        ids.update(str(doc[k]) for k in LIVE_ID_KEYS if isinstance(doc.get(k), str) and doc[k])
    if pane_pid and pane_pid.isdigit():
        try:
            argv = (proc_root / pane_pid / "cmdline").read_bytes().split(b"\0")
            ids.update(a.decode("utf-8", "replace") for a in argv if a)
        except OSError:
            pass
    return ids


def backfill_dead_session(req: SessionEndRequest, *, dry_run: bool = False,
                          min_quiet_seconds: float = BACKFILL_MIN_QUIET_SECONDS,
                          now: datetime | None = None,
                          route_fn: Callable[[str, str, str], None] | None = None,
                          proc_root: Path = Path("/proc")) -> dict[str, Any]:
    """Archive ONE named dead session (``req.session_id`` = the harness transcript id,
    ``req.rt_started_at`` = its runtime start) without reading the seat's current runtime
    record for identity, touching its live pane log or its tmux pane. Never raises.

    Refuses (status 'refused', a stable ``reason``) unless every check passes: the ids are
    well formed; the transcript resolves to exactly one file; liveness is KNOWN (a runtime
    record or a pane process); the session is not the live one (same start, a start at or after
    the live start, or a transcript written since the live start); the transcript has been quiet
    for ``min_quiet_seconds``. A session already archived up to its transcript's current size is
    'already-archived'; one archived partially gets an extension record (see _growth_extension).

    Pre-boot record (T-6306): a runtime record that started before the host's boot time (/proc/stat
    btime) is dead. With no live pane nothing is live, so the live-session comparisons are skipped
    (output ``liveness: record-predates-boot``); the quiet and identity checks still apply. With a live
    pane it is inconsistent: refused ``liveness-inconsistent``. Unreadable btime: no exemption."""
    current = now or _utc_now()
    out: dict[str, Any] = {"schema": SCHEMA, "persona": req.persona, "cause": req.cause, "backfill": True,
                           "dry_run": dry_run}

    def refuse(reason: str, **detail: Any) -> dict[str, Any]:
        out.update({"status": "refused", "reason": reason, **detail})
        return out

    try:
        if not valid_persona(req.persona):
            return refuse("invalid-persona")
        harness = req.harness
        if harness not in BACKFILL_HARNESSES:
            return refuse("harness-unsupported", harness=harness)
        rt_started = req.rt_started_at
        rt_at = _parse_time(rt_started)
        if rt_started is None or rt_at is None:
            return refuse("rt-started-at-required")
        session_id = req.session_id
        if not session_id or not _BACKFILL_ID_RE.match(session_id):
            return refuse("bad-session-id")
        path, problem = _find_session_transcript(harness, req.account_home, session_id)
        if path is None:
            return refuse(problem or "transcript-not-found")
        written = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

        root = req.chronicle_root or chronicle.chronicle_root()
        agent = chronicle.agent_dir_name(req.persona)
        state_root = req.state_root or persona_state_root()
        record = _effective_runtime_record(req.persona, state_root)
        live_at = _parse_time(record.get("started_at"))
        target = req.tmux_target or record.get("tmux_target") or None
        pid_text = _pane_query(target, req.tmux_socket, "#{pane_pid}")
        pane_at = _proc_start_time(int(pid_text), proc_root) if pid_text and pid_text.isdigit() else None
        live_starts = [t for t in (live_at, pane_at) if t is not None]
        if not live_starts:
            return refuse("liveness-unknown")  # no runtime record and no pane: fail closed
        boot = _boot_time(proc_root)
        if live_at is not None and boot is not None and live_at < boot:
            # The record began before this boot, so it names a session the reboot killed.
            if pane_at is not None:
                return refuse("liveness-inconsistent",
                              why="runtime record predates boot but a pane process is live")
            if written >= boot:
                # T-6306 review: the exemption means "killed by the reboot". A transcript written
                # after this boot belongs to a session that ran after it, so it may be live.
                return refuse("live-session", why="transcript written since boot (record predates boot)",
                              transcript_mtime=_iso(written))
            out["liveness"] = "record-predates-boot"  # nothing is live: skip the live-session comparisons
        else:
            live_start = min(live_starts)
            if session_id in _live_session_ids(record, state_root / req.persona / "active-runtime.json",
                                               pid_text, proc_root):
                return refuse("live-session", why="session-id is the live session's")
            if live_at is not None and live_at == rt_at:
                return refuse("live-session", why="rt-started-at is the current runtime record's")
            if rt_at > live_start:
                return refuse("live-session", why="rt-started-at is after the live session's start")
            if written >= live_start:
                return refuse("live-session", why="transcript written since the live session started",
                              transcript_mtime=_iso(written))
        age = (current - written).total_seconds()
        if age < min_quiet_seconds:
            return refuse("still-written", transcript_mtime=_iso(written), quiet_seconds=round(age, 1))

        req = replace(req, transcript_path=path, backfill=True, rt_pinned=True, stage=False,
                      defer_pane_log=False, sources=(), certain=False, handoff_fresh=False,
                      session_id=session_id)
        rt = {"started_at": rt_started, "harness": harness}
        out["transcript"] = str(path)
        if dry_run:
            ext = _growth_extension(req, root=root, agent=agent, rt=rt, rt_started=rt_started,
                                    pane_stamp=None, role="session")
            covered = any(c.get("state") == "done" and c.get("rt_started_at") == rt_started
                          for c in _claims_for(root, agent))
            out.update({"status": "dry-run",
                        "would": "extend" if ext is not None else ("already-archived" if covered else "archive")})
            return out

        def alert_fn(detail: str, kind: str = ALERT_KIND_FAILED) -> str:
            return alert_failure(req.persona, detail, root=root, agent=agent, now=current,
                                 route_fn=route_fn, kind=kind)

        result = _archive_one(req, root=root, agent=agent, rt=rt, source=None, role="session",
                              now=current, alert_fn=alert_fn)
        out.update(result)
        out["dry_run"] = False
        return out
    except Exception as exc:  # noqa: BLE001 -- fail-open by contract
        out.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]})
        return out


# ---------------------------------------------------------------- coverage
def _btime(proc_root: Path) -> int:
    return next(int(line.split()[1]) for line in (proc_root / "stat").read_text().splitlines()
                if line.startswith("btime "))


def _boot_time(proc_root: Path = Path("/proc")) -> datetime | None:
    """The host's boot time from ``proc_root/stat`` btime; None when unreadable."""
    try:
        return datetime.fromtimestamp(_btime(proc_root), tz=timezone.utc)
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _proc_start_time(pid: int, proc_root: Path = Path("/proc")) -> datetime | None:
    try:
        stat_text = (proc_root / str(pid) / "stat").read_text()
        fields = stat_text[stat_text.rindex(")") + 2:].split()
        start_ticks = int(fields[19])
        return datetime.fromtimestamp(_btime(proc_root) + start_ticks / os.sysconf("SC_CLK_TCK"),
                                      tz=timezone.utc)
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def last_restart(persona: str, *, tmux_target: str | None, tmux_socket: str | None,
                 state_root: Path, proc_root: Path = Path("/proc")) -> tuple[datetime | None, str]:
    """When the seat's CURRENT session started, harness- and plane-neutral:
    the start time of the pane's process; else runtime.json started_at; else
    active-runtime.json started_ts."""
    pid_text = _pane_query(tmux_target, tmux_socket, "#{pane_pid}")
    if pid_text and pid_text.isdigit():
        started = _proc_start_time(int(pid_text), proc_root)
        if started is not None:
            return started, "pane-pid"
    rt_started = _parse_time(_runtime_record(persona, state_root).get("started_at"))
    if rt_started is not None:
        return rt_started, "runtime.json"
    active = _parse_time(_read_json(state_root / persona / "active-runtime.json").get("started_ts"))
    if active is not None:
        return active, "active-runtime.json"
    return None, "unknown"


def coverage(persona: str, *, tmux_target: str | None = None, tmux_socket: str | None = None,
             state_root: Path | None = None, root: Path | None = None,
             now: datetime | None = None, grace_seconds: float = COVERAGE_GRACE_SECONDS,
             restart: tuple[datetime | None, str] | None = None) -> dict[str, Any]:
    """OK when the seat's last session end is archived. FAIL when it is not, or
    when an archive attempt failed since. WARN when nothing is knowable."""
    if not valid_persona(persona):
        raise ValueError(f"invalid persona {persona!r}")
    root = root or chronicle.chronicle_root()
    state_root = state_root or persona_state_root()
    current = now or datetime.now(timezone.utc)
    agent = chronicle.agent_dir_name(persona)
    if tmux_target is None:
        tmux_target = _runtime_record(persona, state_root).get("tmux_target") or None
    started, source = restart or last_restart(persona, tmux_target=tmux_target,
                                              tmux_socket=tmux_socket, state_root=state_root)
    entries = chronicle.entries(agent, root=root)
    latest = max((e["ts"] for e in entries), default=None)
    claims = _claims_for(root, agent)
    out: dict[str, Any] = {"persona": persona, "agent": agent, "last_restart": _iso(started) if started else None,
                           "last_restart_source": source, "latest_archive": latest}
    if started is None:
        out.update({"status": "WARN", "detail": "last restart not knowable"})
        return out
    floor = started - timedelta(seconds=grace_seconds)

    def since_floor(value: Any) -> bool:
        at = _parse_time(value)
        return at is not None and at >= floor

    failed = [c for c in claims if c.get("state") == "failed" and since_floor(c.get("at"))]
    if failed:
        out.update({"status": "FAIL", "detail": f"archive-failed: {failed[-1].get('error', '?')}"[:300]})
        return out
    marker = _claims_dir(root, agent) / PANE_UNKNOWN_MARKER
    stale_marker_detail: str | None = None
    if marker.exists():
        # T-5019 rebound MINOR 3: H2 skipped on a hung tmux; the next H1 resolves it.
        since = _read_json(marker).get("at")
        marker_at = _parse_time(since)
        marker_stale = marker_at is None or (current - marker_at).total_seconds() > PANE_UNKNOWN_STALE_SECONDS
        if not marker_stale:
            out.update({"status": "WARN", "detail": f"session end pending: pane state unknown (since {since})"[:300]})
            return out
        stale_marker_detail = f"stale pane-unknown marker (since {since or '?'})"
    archived = any(since_floor(e.get("ts")) for e in entries) or any(
        c.get("state") == "done" and since_floor(c.get("at")) for c in claims
    )
    if stale_marker_detail is not None:
        detail = stale_marker_detail
        if not archived:
            detail += "; no-archive-after-last-restart"
        out.update({"status": "FAIL", "detail": detail[:300]})
        return out
    if archived:
        out.update({"status": "OK", "detail": "session end archived after last restart"})
    else:
        detail = "no-archive-after-last-restart"
        rt_started = _parse_time(_runtime_record(persona, state_root).get("started_at"))
        if rt_started is not None and rt_started < floor:
            # MINOR 1: runtime.json predates the live session (its launch record
            # was not persisted), so sessions without a pane log share one key.
            detail += " (stale-runtime-record: runtime.json started_at predates the live session)"
        out.update({"status": "FAIL", "detail": detail})
    return out


# ---------------------------------------------------------------- transcript-coverage (T-6118 PR-4b)
TRANSCRIPT_COVERAGE_THRESHOLD = 0.9
TRANSCRIPT_COVERAGE_DAYS = 7
_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SESSION_JSON_RE = re.compile(r".+\.session\.json(?:\.[0-9]+)?$")


def _receipted(meta: dict[str, Any], gz_name: str, date_dir: str, persona: str, harness: str | None,
               export_root: Path, account_home: Path | None) -> bool:
    """True when archivist's ingest receipt names this transcript .gz (schema chronicle-ingest-receipt/v1):
    ``<export_root>/.ingested/<seat>/<date>/<gz>.receipt.json`` whose ``file`` is ``<seat>/<date>/<gz>``.
    A receipt keeps the .gz counted after PR-5 prunes the archive copy. Read-only."""
    export = meta.get("export") if isinstance(meta.get("export"), dict) else {}
    seat, date = export.get("seat"), export.get("date") or date_dir
    if not seat:
        try:
            seat, _ = chronicle_export.resolve_seat(persona=persona, harness=harness,
                                                    account_home=account_home or Path.home())
        except Exception:  # noqa: BLE001
            seat = None
    if not seat or not isinstance(seat, str) or "/" in seat or "/" in str(date):
        return False
    receipt = export_root / chronicle_export.INGESTED_DIRNAME / seat / str(date) / f"{gz_name}.receipt.json"
    try:
        doc = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and doc.get("file") == f"{seat}/{date}/{gz_name}"


def transcript_coverage(persona: str, *, days: int = TRANSCRIPT_COVERAGE_DAYS,
                        threshold: float = TRANSCRIPT_COVERAGE_THRESHOLD, root: Path | None = None,
                        export_root: Path | None = None, account_home: Path | None = None,
                        now: datetime | None = None) -> dict[str, Any]:
    """Share of the seat's non-hollow session records, ended in the last ``days``, that have a transcript:
    the archived .gz exists, or an ingest receipt names it. Hollow records leave both sides. A window with
    no non-hollow ends is OK with a null ratio (the last-restart axis covers 'stopped producing records').
    Read-only: never writes, chowns or mkdirs."""
    if not valid_persona(persona):
        raise ValueError(f"invalid persona {persona!r}")
    root = root or chronicle.chronicle_root()
    ex_root = chronicle_export.export_root(export_root)
    agent = chronicle.agent_dir_name(persona)
    current = now or datetime.now(timezone.utc)
    start = current - timedelta(days=days)
    counts = {"records": 0, "hollow": 0, "eligible": 0, "with_transcript": 0, "receipted_only": 0, "unparseable": 0}
    reasons: dict[str, int] = {}
    try:
        date_dirs = sorted(d for d in (root / agent).iterdir() if _DATE_DIR_RE.match(d.name) and d.is_dir())
    except OSError:
        date_dirs = []
    for ddir in date_dirs:
        try:
            names = sorted(n for n in os.listdir(ddir) if _SESSION_JSON_RE.match(n))
        except OSError:
            continue
        for name in names:
            meta = _read_json(ddir / name)
            ended = _parse_time(meta.get("ended_at")) if meta else None
            if ended is None:
                counts["unparseable"] += 1
                continue
            if ended < start:
                continue
            counts["records"] += 1
            if meta.get("hollow") is True:
                counts["hollow"] += 1
                continue
            counts["eligible"] += 1
            pointer = meta.get("native_transcript") if isinstance(meta.get("native_transcript"), dict) else None
            if _transcript_gz_of(pointer, root, agent) is not None:
                counts["with_transcript"] += 1
                continue
            gz_name = Path(str(pointer.get("path"))).name if pointer and pointer.get("path") else ""
            if gz_name and chronicle_export.is_transcript_gz(gz_name) and _receipted(
                    meta, gz_name, ddir.name, persona, meta.get("harness"), ex_root, account_home):
                counts["with_transcript"] += 1
                counts["receipted_only"] += 1
                continue
            reason = str(meta.get("transcript_absent_reason") or "unspecified")
            reasons[reason] = reasons.get(reason, 0) + 1
    eligible = counts["eligible"]
    ratio = round(counts["with_transcript"] / eligible, 4) if eligible else None
    status = "OK" if ratio is None or ratio >= threshold else "FAIL"
    detail = ("no non-hollow session ends in window" if ratio is None else
              f"{counts['with_transcript']}/{eligible} non-hollow session ends have a transcript "
              f"(ratio {ratio}, threshold {threshold})")
    return {"persona": persona, "agent": agent, "days": days, "threshold": threshold,
            "window_start": _iso(start), **counts, "ratio": ratio, "status": status,
            "detail": detail, "absent_reasons": reasons}


def _cmd_transcript_coverage(args: argparse.Namespace) -> int:
    try:
        result = transcript_coverage(
            args.persona, days=args.days, threshold=args.threshold,
            root=Path(args.root) if args.root else None,
            export_root=Path(args.export_root) if args.export_root else None,
            account_home=Path(args.account_home) if args.account_home else None,
            now=_parse_time(args.now) if args.now else None)
    except Exception as exc:  # noqa: BLE001
        result = {"persona": args.persona, "status": "WARN", "detail": f"transcript-coverage error: {exc!r}"[:300]}
    if args.bool:
        print("true" if result.get("status") == "OK" else "false")
    else:
        print(json.dumps(result, sort_keys=True))
    return 0


# ---------------------------------------------------------------- export-backfill (T-6118 PR-2)
SKIP_REASONS = (
    "no-md", "no-session-json", "stem-unparseable", "source-missing", "source-changed",
    "already-exported", "handoff-identical", "export-unprovisioned", "seat-unresolved", "not-owner",
)
_BACKFILL_GZ_LEVEL = 9
_UUID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass
class _BackfillRecord:
    entry: dict[str, Any]
    stem: str
    day_dir: Path
    md: Path
    meta_path: Path
    meta: dict[str, Any] = field(default_factory=dict)
    seat: str | None = None
    date: str = ""


def _estimate_gz_bytes(source: Path) -> int:
    """~compressed size: gzip a leading sample and scale (dry-run only)."""
    import gzip

    try:
        total = source.stat().st_size
        with source.open("rb") as handle:
            sample = handle.read(1 << 20)
        if not sample:
            return 0
        return int(len(gzip.compress(sample, compresslevel=6)) * (total / len(sample)))
    except OSError:
        return 0


def _record_selected(rec: _BackfillRecord, wanted: list[str]) -> list[str]:
    """The ``--record`` arguments that name this record (stem or any of its basenames)."""
    names = {rec.stem, f"{rec.stem}.md", f"{rec.stem}.session.json", f"{rec.stem}.transcript.jsonl.gz"}
    return [w for w in wanted if w in names
            or (w.startswith(f"{rec.stem}.") and chronicle_export.is_transcript_gz(w))]


def _export_present(ex_root: Path, seat: str, date: str, names: list[str]) -> bool:
    return all((ex_root / seat / date / n).is_file() for n in names)


def _write_meta_replace(path: Path, doc: dict[str, Any], gid: int | None) -> None:
    """Rewrite a session.json: temp + rename, 0640 and the export group before it is visible."""
    import tempfile

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
            os.fchmod(fh.fileno(), EXPORT_MODE)
            if gid is not None:
                os.fchown(fh.fileno(), -1, gid)
        os.replace(tmp, path)
        tmp = None  # type: ignore[assignment]
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def export_backfill(persona: str, since: str, *, execute: bool = False, records: tuple[str, ...] = (),
                    export_root: "str | os.PathLike | None" = None, account_home: Path | None = None,
                    chronicle_root: Path | None = None, repo_root: Path | None = None,
                    now: datetime | None = None) -> dict[str, Any]:
    """Archive and export a persona's earlier session records (the FIRST DROP).

    Dry-run by default and then strictly read-only. Records go oldest-first. Per record: skip with a
    reason, else (legacy pointer only) archive the source .gz under the record's own stem, fix the
    .md mode in place, link the .gz and .md, rewrite session.json (native_transcript, export,
    supersedes) and link it last. A record with no archivable transcript (HANDOFF-only: none
    resolved, a harness that is not archived, ...) exports its .md then its session.json
    (transcript "absent"), unless the .md is byte-identical to the seat's most recently exported
    one (skip handoff-identical, nothing written to the export tree). Never deletes anything.
    Returns {"lines": [[...]...], "summary", "exit"} for the CLI to print."""
    from tools import chronicle_export as cx

    current = now or _utc_now()
    home = account_home or Path.home()
    ex_root = cx.export_root(export_root)
    root = chronicle_root or chronicle.chronicle_root()
    agent = chronicle.agent_dir_name(persona)
    lines: list[list[str]] = []
    counts = {"records": 0, "archive": 0, "fix-mode": 0, "export": 0, "supersede": 0, "skip": 0, "fail": 0,
              "unprovisioned": 0,
              "handoff-only": 0, "handoff-identical": 0}
    unprovisioned_seen = not Path(ex_root).is_dir()
    # dry-run only: earlier records of this run that already have an archived .gz (source_path, file, path)
    planned: list[tuple[str, str, Path]] = []
    # dry-run only: the newest .md each seat would have exported so far in this run (the dedupe sees it)
    planned_md: dict[str, chronicle_export.ExportedMd] = {}
    seats: list[str] = []
    wanted = list(records)
    matched: set[str] = set()
    verb = (lambda dry, real: real if execute else dry)

    def skip(stem: str, reason: str, detail: str = "") -> None:
        counts["skip"] += 1
        lines.append(["skip", stem, reason] + ([detail] if detail else []))

    entries = [e for e in chronicle.entries(agent, root=root) if str(e.get("ts", ""))[:10] >= since]
    entries.sort(key=lambda e: str(e.get("ts")))
    for entry in entries:
        ts, uuid = str(entry.get("ts") or ""), str(entry.get("uuid") or "")
        stem = f"{chronicle._fs_safe_ts(ts)}-{uuid}"
        rec = _BackfillRecord(entry, stem, root / agent / ts[:10], root / agent / ts[:10] / f"{stem}.md",
                              root / agent / ts[:10] / f"{stem}.session.json", date=ts[:10])
        if wanted:
            hit = _record_selected(rec, wanted)
            if not hit:
                continue
            matched.update(hit)
        counts["records"] += 1
        if _parse_time(ts) is None or not uuid or not _UUID_RE.match(uuid):
            skip(stem, "stem-unparseable")
            continue
        if not rec.md.is_file():
            skip(stem, "no-md")
            continue
        if not rec.meta_path.is_file():
            skip(stem, "no-session-json")
            continue
        rec.meta = _read_json(rec.meta_path)
        pointer = rec.meta.get("native_transcript") if isinstance(rec.meta.get("native_transcript"), dict) else None
        harness = rec.meta.get("harness")
        handoff_only = not (pointer and (pointer.get("path") or pointer.get("source_path"))
                            and harness in ARCHIVED_HARNESSES)
        seat, why = chronicle_export.resolve_seat(persona=persona, harness=harness, account_home=home,
                                                  repo_root=repo_root)
        if seat is None:
            skip(stem, "seat-unresolved", str(why))
            continue
        rec.seat = seat
        if seat not in seats:
            seats.append(seat)
        archived_gz = None if handoff_only else _transcript_gz_of(pointer, root, agent)
        gz_name = archived_gz.name if archived_gz else f"{stem}.transcript.jsonl.gz"
        names = [rec.md.name, rec.meta_path.name] if handoff_only else [gz_name, rec.md.name, rec.meta_path.name]
        exp = rec.meta.get("export") if isinstance(rec.meta.get("export"), dict) else {}
        if exp.get("status") == cx.STATUS_EXPORTED and (handoff_only or archived_gz) \
                and _export_present(ex_root, seat, rec.date, names):
            skip(stem, "already-exported")
            continue
        if handoff_only and exp.get("status") == cx.STATUS_HANDOFF_IDENTICAL and isinstance(exp.get("identical_to"), str):
            # Settled by an earlier run: re-deciding against today's newest .md could export a record
            # that was a duplicate when it was skipped.
            counts["handoff-identical"] += 1
            skip(stem, "handoff-identical", exp["identical_to"])
            continue
        gid: int | None
        unprov_detail = ""
        try:
            with cx.ExportDir(ex_root, seat, None) as probe:  # read-only: creates nothing
                gid = probe.gid
        except cx.ExportError as exc:
            if execute and not isinstance(exc, cx.ExportUnprovisioned):
                counts["fail"] += 1
                lines.append(["fail", stem, str(exc)[:200]])
                continue
            if isinstance(exc, cx.ExportUnprovisioned):
                unprovisioned_seen = True
            if execute or not isinstance(exc, cx.ExportUnprovisioned):
                if execute:
                    counts["unprovisioned"] += 1
                skip(stem, "export-unprovisioned", str(exc)[:200])
                continue
            # DRY-RUN, unprovisioned: preview the whole record read-only, then say so in a note line.
            gid, unprov_detail = None, str(exc)[:200]
        if rec.md.stat().st_uid != os.geteuid() or rec.meta_path.stat().st_uid != os.geteuid():
            skip(stem, "not-owner")
            continue
        absent = {"transcript": "absent", "transcript_absent_reason": _transcript_absent_reason(pointer, harness)}
        if handoff_only:
            twin = cx.identical_to_newest(ex_root, seat, rec.md, date=rec.date, planned=planned_md.get(seat))
            if twin is not None:
                try:
                    if execute:
                        doc = dict(rec.meta)
                        doc.update(absent)
                        doc["export"] = {"status": cx.STATUS_HANDOFF_IDENTICAL, "seat": seat, "date": rec.date,
                                         "files": [], "root": str(ex_root), "detail": None, "identical_to": twin}
                        if doc != rec.meta:  # the Chronicle record only: nothing is written to the export tree
                            _write_meta_replace(rec.meta_path, doc, gid)
                except Exception as exc:  # noqa: BLE001 -- one bad record never stops the run
                    counts["fail"] += 1
                    lines.append(["fail", stem, f"{type(exc).__name__}: {chronicle.scrub_credentials(str(exc))}"[:300]])
                    continue
                counts["handoff-identical"] += 1
                skip(stem, "handoff-identical", twin)
                continue
        source: Path | None = None
        old_sha = ""
        if archived_gz is None and not handoff_only:
            source = Path(str(pointer.get("source_path") or pointer.get("path")))
            if source.is_symlink() or not source.is_file():
                skip(stem, "source-missing", str(source))
                continue
            old_sha = str(pointer.get("sha256") or pointer.get("source_sha256") or "")
            try:
                current_sha = chronicle_export.sha256_file(source)
            except OSError:
                skip(stem, "source-missing", str(source))
                continue
            if old_sha and current_sha != old_sha:
                skip(stem, "source-changed", str(source))
                continue

        # ---- the work for this record
        group_name = _group_name_of(gid) if gid is not None else None
        try:
            if handoff_only:
                md_mode = stat_mode(rec.md)
                if md_mode != EXPORT_MODE or (gid is not None and rec.md.stat().st_gid != gid):
                    counts["fix-mode"] += 1
                    lines.append([verb("would-fix-mode", "fix-mode"), rec.md.name, f"{md_mode:04o}->{EXPORT_MODE:04o}"])
                if not execute:
                    for n, path in ((rec.md.name, rec.md), (rec.meta_path.name, rec.meta_path)):
                        counts["export"] += 1
                        lines.append(["would-export", f"{seat}/{rec.date}/{n}", f"{path.stat().st_size}B"])
                    counts["handoff-only"] += 1
                    planned_md[seat] = cx.ExportedMd.of(rec.date, rec.md.name, rec.md)
                    if unprov_detail:
                        counts["unprovisioned"] += 1
                        lines.append(["note", stem, "export-unprovisioned", unprov_detail])
                    continue
                cx.ensure_group_readable(rec.md, gid)
                outcome = chronicle_export.export_files((rec.md,), root=ex_root, seat=seat, date=rec.date)
                if outcome.status != cx.STATUS_EXPORTED:
                    raise cx.ExportFailed(f"{outcome.status}: {outcome.detail}")
                doc = dict(rec.meta)
                doc.update(absent)
                doc["export"] = outcome.as_meta()
                doc.setdefault("supersedes", [])
                _write_meta_replace(rec.meta_path, doc, gid)
                last = chronicle_export.link_one(rec.meta_path, root=ex_root, seat=seat, date=rec.date)
                if last.status != cx.STATUS_EXPORTED:
                    raise cx.ExportFailed(f"session.json {last.status}: {last.detail}")
                counts["handoff-only"] += 1
                for n, path in ((rec.md.name, rec.md), (rec.meta_path.name, rec.meta_path)):
                    counts["export"] += 1
                    lines.append(["export", f"{seat}/{rec.date}/{n}", f"{path.stat().st_size}B"])
                continue
            if source is not None:
                counts["archive"] += 1
                lines.append([verb("would-archive", "archive"), stem, str(source),
                              f"-> {stem}.transcript.jsonl.gz", f"{source.stat().st_size}B"])
            md_mode = stat_mode(rec.md)
            if md_mode != EXPORT_MODE or (gid is not None and rec.md.stat().st_gid != gid):
                counts["fix-mode"] += 1
                lines.append([verb("would-fix-mode", "fix-mode"), rec.md.name, f"{md_mode:04o}->{EXPORT_MODE:04o}"])
            new_pointer = dict(pointer)
            gz_path = archived_gz
            if execute:
                if source is not None:
                    gz_path, new_pointer = _backfill_archive(source, pointer, agent=agent, root=root, ts=ts,
                                                             uuid=uuid, group=group_name, old_sha=old_sha)
                cx.ensure_group_readable(rec.md, gid)
                if gz_path is not None:
                    cx.ensure_group_readable(gz_path, gid)
            new_gz_for_scan = gz_path if gz_path is not None else source
            supersedes, sdetail = chronicle_export.find_supersedes(
                agent=agent, chron_root=root, export_root_=ex_root, new_gz=new_gz_for_scan,
                source_path=str(pointer.get("source_path") or pointer.get("path")), skip_stem=stem, now=current)
            for old in supersedes:
                counts["supersede"] += 1
                lines.append([verb("would-supersede", "supersede"), old["file"], f"{old['size']}B"])
            if not execute:
                src_key = str(pointer.get("source_path") or pointer.get("path"))
                have = {old["file"] for old in supersedes}
                for p_src, p_file, p_path in planned:
                    if p_src != src_key or p_file in have or p_path == new_gz_for_scan:
                        continue
                    verdict = chronicle_export._strict_prefix(
                        p_path, new_gz_for_scan, time.monotonic() + chronicle_export.SUPERSEDE_DEADLINE_SECONDS,
                        time.monotonic)
                    if verdict:
                        counts["supersede"] += 1
                        lines.append(["would-supersede", p_file, f"{p_path.stat().st_size}B"])
                for n, path in ((gz_name, gz_path), (rec.md.name, rec.md), (rec.meta_path.name, rec.meta_path)):
                    est = f"~{_estimate_gz_bytes(source)}B" if (path is None and source is not None) else f"{path.stat().st_size}B"
                    counts["export"] += 1
                    lines.append(["would-export", f"{seat}/{rec.date}/{n}", est])
                if gz_path is not None:
                    planned.append((src_key, f"{seat}/{rec.date}/{gz_name}", gz_path))
                planned_md[seat] = cx.ExportedMd.of(rec.date, rec.md.name, rec.md)
                if unprov_detail:
                    counts["unprovisioned"] += 1
                    lines.append(["note", stem, "export-unprovisioned", unprov_detail])
                continue
            outcome = chronicle_export.export_files((gz_path, rec.md), root=ex_root, seat=seat, date=rec.date)
            if outcome.status != cx.STATUS_EXPORTED:
                raise cx.ExportFailed(f"{outcome.status}: {outcome.detail}")
            export_meta = outcome.as_meta()
            if sdetail:
                export_meta["detail"] = sdetail
            doc = dict(rec.meta)
            doc["native_transcript"] = new_pointer
            doc["export"] = export_meta
            doc["transcript"] = "archived"
            doc.pop("transcript_absent_reason", None)
            doc["supersedes"] = supersedes
            _write_meta_replace(rec.meta_path, doc, gid)
            last = chronicle_export.link_one(rec.meta_path, root=ex_root, seat=seat, date=rec.date)
            if last.status != cx.STATUS_EXPORTED:
                raise cx.ExportFailed(f"session.json {last.status}: {last.detail}")
            for n, path in ((gz_name, gz_path), (rec.md.name, rec.md), (rec.meta_path.name, rec.meta_path)):
                counts["export"] += 1
                lines.append(["export", f"{seat}/{rec.date}/{n}", f"{path.stat().st_size}B"])
        except Exception as exc:  # noqa: BLE001 -- one bad record never stops the run
            detail = f"{type(exc).__name__}: {chronicle.scrub_credentials(str(exc))}"[:300]
            if execute:
                counts["fail"] += 1
                lines.append(["fail", stem, detail])
            elif isinstance(exc, OSError):
                skip(stem, "source-missing", detail)
            else:
                raise

    unmatched = [w for w in wanted if w not in matched]
    code = 2 if unmatched else (1 if execute and counts["fail"] else 0)
    return {"mode": "EXECUTE" if execute else "DRY-RUN", "persona": persona,
            "seat": ",".join(seats) if seats else "-", "since": since, "export_root": str(ex_root),
            "export_root_state": "unprovisioned" if unprovisioned_seen else "provisioned",
            "lines": lines, "counts": counts, "unmatched": unmatched, "exit": code}


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _group_name_of(gid: int) -> str | None:
    import grp

    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return None


def _backfill_archive(source: Path, old: dict[str, Any], *, agent: str, root: Path, ts: str, uuid: str,
                      group: str | None, old_sha: str) -> tuple[Path, dict[str, Any]]:
    """Archive ``source`` under the record's own stem (an earlier complete .gz for it is reused)."""
    reusable = _reusable_transcript_gz(agent, root, ts, uuid, old_sha) if old_sha else None
    if reusable is not None:
        digest, nbytes = hashlib.sha256(), 0
        with reusable.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
                nbytes += len(block)
        archived = {"path": str(reusable), "bytes": nbytes, "sha256": digest.hexdigest(),
                    "source_bytes": old.get("bytes"), "source_sha256": old_sha}
    else:
        archived = chronicle.archive_native_transcript(
            agent, source, jsonl_uuid=uuid, ts=ts, root=root, group=group, compresslevel=_BACKFILL_GZ_LEVEL)
    pointer = {
        "path": archived["path"], "bytes": archived["bytes"], "sha256": archived["sha256"],
        "source_path": str(source), "session_id": old.get("session_id"),
        "source_bytes": archived.get("source_bytes"), "source_sha256": archived.get("source_sha256") or old_sha,
        "candidates": old.get("candidates", 1), "backfilled": True,
        "pre_archive_sha256": old_sha or None,
    }
    if old_sha and pointer["source_sha256"] != old_sha:
        pointer["source_changed_during_archive"] = True
    return Path(archived["path"]), pointer


def _format_backfill(result: dict[str, Any]) -> list[str]:
    out = ["\t".join(["MODE", result["mode"], f"persona={result['persona']}", f"seat={result['seat']}",
                      f"since={result['since']}", f"export_root={result['export_root']}",
                      f"export_root_state={result['export_root_state']}"])]
    out.extend("\t".join(line) for line in result["lines"])
    c = result["counts"]
    out.append("\t".join(["SUMMARY", f"mode={result['mode']}"] + [f"{k}={c[k]}" for k in
               ("records", "archive", "fix-mode", "export", "supersede", "skip", "fail",
                                                                  "unprovisioned", "handoff-only", "handoff-identical")]))
    return out


def _cmd_export_backfill(args: argparse.Namespace) -> int:
    if not valid_persona(args.persona):
        print("invalid persona", file=sys.stderr)
        return 2
    if not re.match(r"^\d{4}-\d\d-\d\d$", args.since or ""):
        print("--since must be YYYY-MM-DD", file=sys.stderr)
        return 2
    result = export_backfill(
        args.persona, args.since, execute=args.execute, records=tuple(args.record or ()),
        export_root=args.export_root, account_home=Path(args.account_home) if args.account_home else None)
    if args.json:
        print(json.dumps({k: v for k, v in result.items()}, sort_keys=True))
    else:
        print("\n".join(_format_backfill(result)))
    for name in result["unmatched"]:
        print(f"--record {name}: no such record in the window", file=sys.stderr)
    return int(result["exit"])


# ---------------------------------------------------------------- CLI
def _cmd_session_end(args: argparse.Namespace) -> int:
    fresh = None if args.handoff_fresh is None else args.handoff_fresh == "1"
    persona = args.persona
    if not persona and args.handoff_file:
        persona = chronicle.agent_from_handoff_path(args.handoff_file)
    if not persona:
        for source in args.source or ():
            match = re.match(r"pane-timed-(.+?)\.log(\.dying.*)?$", Path(source).name)
            if match:
                persona = match.group(1)
                break
    if not persona:
        print(json.dumps({"schema": SCHEMA, "status": "failed", "error": "cannot resolve persona"}))
        return 0
    if not valid_persona(persona):
        print(json.dumps({"schema": SCHEMA, "status": "failed", "error": "invalid persona"}))
        return 0
    req = SessionEndRequest(
        persona=persona, cause=args.cause,
        account_home=Path(args.account_home) if args.account_home else Path.home(),
        handoff_file=Path(args.handoff_file) if args.handoff_file else None,
        harness=args.harness, tmux_target=args.tmux_target, tmux_socket=args.tmux_socket,
        stage=args.stage, defer_pane_log=args.defer_pane_log,
        sources=tuple(Path(s) for s in (args.source or ())), uuid=args.uuid,
        handoff_fresh=fresh, entrypoint=args.entrypoint, certain=args.certain,
        rt_pinned=args.rt_started_at is not None,
        rt_started_at=None if args.rt_started_at in (None, "-", "") else args.rt_started_at,
        stage_if_dead=args.stage_if_dead, cwd=Path(args.cwd) if args.cwd else None,
        pane_stamps=tuple(s for s in (args.pane_stamp or ()) if _STAMP_RE.match(s)),
        state_root=Path(args.persona_state_dir) if args.persona_state_dir else None,
        native_transcript=args.native_transcript_json,
        export_root=Path(args.export_root) if args.export_root else None,
        session_id=args.session_id or None,
    )
    if args.backfill_dead_session:
        result = backfill_dead_session(req, dry_run=args.dry_run, min_quiet_seconds=args.min_quiet_seconds)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("status") in ("archived", "paired", "already-archived", "hollow", "dry-run") else 1
    print(json.dumps(session_end(req), sort_keys=True))
    return 0


def _resolve_cli_persona(args: argparse.Namespace) -> str | None:
    persona = args.persona
    if not persona and args.handoff_file:
        persona = chronicle.agent_from_handoff_path(args.handoff_file)
    if not persona:
        for source in args.source or ():
            match = re.match(r"pane-timed-(.+?)\.log(\.dying.*)?$", Path(source).name)
            if match:
                persona = match.group(1)
                break
    return persona if valid_persona(persona) else None


def _cmd_identity(args: argparse.Namespace) -> int:
    """Review B1: a restart script captures the ending session's identity BEFORE
    the restart (which rewrites runtime.json), then passes it back as
    --rt-started-at. Prints the started_at, or '-' when none is recorded."""
    try:
        persona = _resolve_cli_persona(args)
        state_root = Path(args.persona_state_dir) if args.persona_state_dir else persona_state_root()
        started = _effective_runtime_record(persona, state_root).get("started_at") if persona else None
        print(started if isinstance(started, str) and started else "-")
    except Exception:  # noqa: BLE001 -- fail-open: '-' just means "no record"
        print("-")
    return 0


def _cmd_coverage(args: argparse.Namespace) -> int:
    try:
        result = coverage(args.persona, tmux_target=args.tmux_target, tmux_socket=args.tmux_socket,
                          state_root=Path(args.persona_state_dir) if args.persona_state_dir else None)
    except Exception as exc:  # noqa: BLE001
        result = {"persona": args.persona, "status": "WARN", "detail": f"coverage error: {exc!r}"[:300]}
    if args.bool:
        print("true" if result.get("status") == "OK" else "false")
    else:
        print(json.dumps(result, sort_keys=True))
    return 0


def _json_object(text: str) -> dict:
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise argparse.ArgumentTypeError("expected a JSON object")
    return doc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Session-end Chronicle duty (T-5016).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    end = sub.add_parser("session-end", help="archive the ending session(s) of a persona")
    end.add_argument("--persona")
    end.add_argument("--cause", required=True, choices=CAUSES)
    end.add_argument("--account-home")
    end.add_argument("--handoff-file")
    end.add_argument("--harness")
    end.add_argument("--tmux-target")
    end.add_argument("--tmux-socket")
    end.add_argument("--persona-state-dir")
    end.add_argument("--stage", action="store_true", help="the session is ending now: close its tap, stage its log")
    end.add_argument("--defer-pane-log", action="store_true", help="record now; attach the live log on the next run")
    end.add_argument("--source", action="append", help="an already staged pane log (repeatable)")
    end.add_argument("--uuid")
    end.add_argument("--handoff-fresh", choices=("0", "1"))
    end.add_argument("--entrypoint", default="cli")
    end.add_argument("--rt-started-at",
                     help="the ENDING session's runtime.json started_at, captured before its restart "
                          "('-' = none recorded); pins the session identity")
    end.add_argument("--stage-if-dead", action="store_true",
                     help="do nothing while the pane runs a live process (post-death hook)")
    end.add_argument("--pane-stamp", action="append",
                     help="first stamp of a --source log, captured before the restart (repeatable); "
                          "with --rt-started-at -, a done claim for it makes the call a no-op")
    end.add_argument("--cwd", help="the session's launch cwd (scopes the transcript lookup)")
    end.add_argument("--native-transcript-json", type=_json_object,
                     help="T-6115: JSON pointer of an already archived native transcript; recorded as "
                          "meta.native_transcript instead of the resolver's")
    end.add_argument("--session-id",
                     help="T-6118: the harness's own session/thread id (the codex supervisor's thread); picks the rollout")
    end.add_argument("--export-root",
                     help="T-6118: memex export tree (default $PANTHEON_CHRONICLE_EXPORT_ROOT, else "
                          "/var/lib/pantheon/chronicle-export)")
    end.add_argument("--certain", action="store_true",
                     help="a session certainly ended now: archive even with no session identity")
    end.add_argument("--backfill-dead-session", action="store_true",
                     help="T-6302: archive ONE dead session named by --session-id (the transcript id) and "
                          "--rt-started-at; never touches the live session; exit 1 on a refusal")
    end.add_argument("--dry-run", action="store_true", help="with --backfill-dead-session: decide, write nothing")
    end.add_argument("--min-quiet-seconds", type=float, default=BACKFILL_MIN_QUIET_SECONDS,
                     help="with --backfill-dead-session: refuse a transcript written within this many seconds")
    end.add_argument("--json", action="store_true", help="accepted for symmetry; output is always JSON")
    end.set_defaults(func=_cmd_session_end)
    ident = sub.add_parser("identity", help="print the persona's current session identity (before a restart)")
    ident.add_argument("--persona")
    ident.add_argument("--handoff-file")
    ident.add_argument("--source", action="append")
    ident.add_argument("--persona-state-dir")
    ident.set_defaults(func=_cmd_identity)
    bf = sub.add_parser("export-backfill", help="archive + export a persona's earlier records (dry-run by default)")
    bf.add_argument("--persona", required=True)
    bf.add_argument("--since", required=True, help="YYYY-MM-DD: records on or after this day")
    bf.add_argument("--execute", action="store_true", help="do the work; the default is a read-only dry-run")
    bf.add_argument("--record", action="append", help="only this record (stem or file basename); repeatable")
    bf.add_argument("--export-root")
    bf.add_argument("--account-home")
    bf.add_argument("--json", action="store_true")
    bf.set_defaults(func=_cmd_export_backfill)
    cov = sub.add_parser("coverage", help="is the seat's last session end archived?")
    cov.add_argument("--persona", required=True)
    cov.add_argument("--tmux-target")
    cov.add_argument("--tmux-socket")
    cov.add_argument("--persona-state-dir")
    cov.add_argument("--bool", action="store_true")
    cov.set_defaults(func=_cmd_coverage)
    tcov = sub.add_parser("transcript-coverage", help="share of non-hollow session ends that have a transcript")
    tcov.add_argument("--persona", required=True)
    tcov.add_argument("--days", type=int, default=TRANSCRIPT_COVERAGE_DAYS)
    tcov.add_argument("--threshold", type=float, default=TRANSCRIPT_COVERAGE_THRESHOLD)
    tcov.add_argument("--root", help="Chronicle root (default: the account's own)")
    tcov.add_argument("--export-root")
    tcov.add_argument("--account-home")
    tcov.add_argument("--now", help="ISO time to count the window back from (tests)")
    tcov.add_argument("--bool", action="store_true")
    tcov.add_argument("--json", action="store_true", help="accepted for symmetry; output is always JSON")
    tcov.set_defaults(func=_cmd_transcript_coverage)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) else 64
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
