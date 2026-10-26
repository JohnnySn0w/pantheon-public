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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import chronicle  # noqa: E402

SCHEMA = "pantheon-session-chronicle/v1"
REASON_PREFIX = "session-end/"
SWAP_REASONS = ("planned-runtime-swap", "forced-provider-cap")
CAUSES = (
    "start", "replace", "start-reconcile", "death", "cap-swap", "fallback-return",
    "operator-swap", "graceful-restart", "forced-restart", "ctxfill-relaunch",
    "fleet-reboot", "orphan-staged",
)
CLAIMS_DIRNAME = ".claims"
CLAIM_STALE_SECONDS = 300.0
ALERT_COOLDOWN_SECONDS = 6 * 3600.0
ALERT_ENV = "PANTHEON_SESSION_CHRONICLE_ALERT"
ALERT_VOCAB = "chronicle-archive-failed"
TMUX_TIMEOUT_SECONDS = 5.0
COVERAGE_GRACE_SECONDS = 15 * 60.0
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
                  route_fn: Callable[[str, str, str], None] | None = None) -> str:
    """Route one Recovery alert per ALERT_COOLDOWN_SECONDS per persona. Never raises.
    Returns 'routed', 'cooldown', 'disabled' or 'failed: ...'."""
    try:
        current = now or _utc_now()
        stamp = _claims_dir(root, agent) / "alert-stamp"  # not *.json: claims are globbed by that
        last = _parse_time(_read_json(stamp).get("last_alert_at"))
        if last is not None and (current - last).total_seconds() < ALERT_COOLDOWN_SECONDS:
            return "cooldown"
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
                        started_at: datetime | None, cwd: Path | None = None) -> dict[str, Any] | None:
    if not harness:
        return None
    try:
        from tools.harness_adapter import integration as harness_integration

        resolver = getattr(harness_integration.integration_for(harness), "session_transcript", None)
        if resolver is None:
            return None
        found = resolver(account_home=account_home, started_at=started_at, cwd=cwd)
        if found is None:
            return None
        digest = hashlib.sha256()
        size = 0
        with found.path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
                size += len(chunk)
        return {"path": str(found.path), "session_id": found.session_id,
                "bytes": size, "sha256": digest.hexdigest(),
                "candidates": int(getattr(found, "candidates", 1) or 1)}
    except Exception as exc:  # noqa: BLE001 -- a pointer is optional
        _log(f"transcript pointer unavailable ({harness}): {exc!r}")
        return None


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


def _archive_one(req: SessionEndRequest, *, root: Path, agent: str, rt: dict[str, Any],
                 source: Path | None, role: str, now: datetime,
                 alert_fn: Callable[..., str]) -> dict[str, Any]:
    rt_started = rt.get("started_at") if isinstance(rt.get("started_at"), str) else None
    pane_stamp = _pane_first_stamp(source) if source is not None else None
    if role == "orphan":
        rt_started = None  # an orphan log predates the recorded runtime
    key = _session_key(req.persona, rt_started, pane_stamp)
    claims = _claims_dir(root, agent)
    claims.mkdir(parents=True, exist_ok=True)
    claim_path = claims / f"{key}.json"
    result: dict[str, Any] = {"key": key, "source": str(source) if source else None}

    if (source is not None and pane_stamp and role == "session" and rt_started is not None
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

    if req.rt_pinned and req.rt_started_at is None and role == "session":
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
        existing_claims = _claims_for(root, agent)
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
        handoff = req.handoff_file or (req.account_home / "HANDOFF.md")
        text, fresh, handoff_mtime = _handoff_snapshot(Path(handoff), session_start)
        if req.handoff_fresh is not None and role == "session":
            fresh = req.handoff_fresh
        harness = req.harness or (rt.get("harness") if role == "session" else None)
        pointer = _transcript_pointer(harness, req.account_home, session_start, req.cwd) \
            if role == "session" else None
        # An optional pointer names the stem only when it is unambiguous (MINOR 2).
        pointer_id = (pointer or {}).get("session_id") if (pointer or {}).get("candidates", 1) == 1 else None
        uuid = (req.uuid if req.uuid and req.uuid != "unknown" else None) or pointer_id or f"rt-{key[:12]}"
        uuid = re.sub(r"[^A-Za-z0-9._-]", "_", str(uuid))[:96]

        swap = _swap_entry(agent, root, session_start) if role == "session" else None
        reused = _predecessor_record(agent, root, previous)
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
            record_path = chronicle.archive_required(
                agent, text, jsonl_uuid=stem_uuid, reason=f"{REASON_PREFIX}{cause}",
                ts=stem_ts, root=root,
            )
            paired = False

        pane_state = "absent"
        if source is not None and not req.defer_pane_log:
            companion = chronicle.archive_companion(agent, source, jsonl_uuid=stem_uuid, ts=stem_ts, root=root)
            pane_state = "archived" if companion is not None else "pending"
            result["pane_log"] = str(companion) if companion else None
        elif source is not None:
            pane_state = "pending"

        meta = {
            "schema": SCHEMA, "persona": req.persona, "agent": agent, "cause": cause,
            "session_key": key, "harness": harness,
            "profile": rt.get("profile") if role == "session" else None,
            "model": rt.get("model") if role == "session" else None,
            "tmux_target": req.tmux_target or rt.get("tmux_target"),
            "session_started_at": _iso(session_start) if session_start else None,
            "rt_started_at": rt_started, "pane_first_stamp": pane_stamp,
            "ended_at": _iso(now), "handoff_file": str(handoff), "handoff_fresh": fresh,
            "handoff_mtime": handoff_mtime, "native_transcript": pointer,
            "pane_log": pane_state, "paired_with_swap_entry": paired,
            "writer": {"entrypoint": req.entrypoint, "pid": os.getpid()},
        }
        chronicle.write_companion_text(agent, json.dumps(meta, indent=2, sort_keys=True) + "\n",
                                       jsonl_uuid=stem_uuid, ts=stem_ts, suffix="session.json", root=root)
        claim_doc.update({"state": "done", "stem_ts": stem_ts, "stem_uuid": stem_uuid,
                          "record": str(record_path), "pane_log": pane_state, "at": _iso(now)})
        _write_json_atomic(claim_path, claim_doc)
        result.update({"status": "archived" if not paired else "paired", "record": str(record_path),
                       "uuid": stem_uuid, "cause": cause, "pane_log_state": pane_state})
        if pane_state == "pending" and not req.defer_pane_log:
            result["alert"] = alert_fn(f"pane log not archived for {source}")
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

        def alert_fn(detail: str) -> str:
            return alert_failure(req.persona, detail, root=root, agent=agent, now=current, route_fn=route_fn)

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


# ---------------------------------------------------------------- coverage
def _proc_start_time(pid: int, proc_root: Path = Path("/proc")) -> datetime | None:
    try:
        stat_text = (proc_root / str(pid) / "stat").read_text()
        fields = stat_text[stat_text.rindex(")") + 2:].split()
        start_ticks = int(fields[19])
        btime = next(int(line.split()[1]) for line in (proc_root / "stat").read_text().splitlines()
                     if line.startswith("btime "))
        return datetime.fromtimestamp(btime + start_ticks / os.sysconf("SC_CLK_TCK"), tz=timezone.utc)
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
    if marker.exists():
        # T-5019 rebound MINOR 3: H2 skipped on a hung tmux; the next H1 resolves it.
        since = _read_json(marker).get("at", "?")
        out.update({"status": "WARN", "detail": f"session end pending: pane state unknown (since {since})"[:300]})
        return out
    archived = any(since_floor(e.get("ts")) for e in entries) or any(
        c.get("state") == "done" and since_floor(c.get("at")) for c in claims
    )
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
    )
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
    end.add_argument("--certain", action="store_true",
                     help="a session certainly ended now: archive even with no session identity")
    end.add_argument("--json", action="store_true", help="accepted for symmetry; output is always JSON")
    end.set_defaults(func=_cmd_session_end)
    ident = sub.add_parser("identity", help="print the persona's current session identity (before a restart)")
    ident.add_argument("--persona")
    ident.add_argument("--handoff-file")
    ident.add_argument("--source", action="append")
    ident.add_argument("--persona-state-dir")
    ident.set_defaults(func=_cmd_identity)
    cov = sub.add_parser("coverage", help="is the seat's last session end archived?")
    cov.add_argument("--persona", required=True)
    cov.add_argument("--tmux-target")
    cov.add_argument("--tmux-socket")
    cov.add_argument("--persona-state-dir")
    cov.add_argument("--bool", action="store_true")
    cov.set_defaults(func=_cmd_coverage)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) else 64
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
