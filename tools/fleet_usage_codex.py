#!/usr/bin/env python3
"""tools/fleet_usage_codex.py — per-turn usage rows for Codex seats (T-4536).

ARC INTENT (the operator TG 10732): make seat token burn attributable per seat / worker / model /
day. The Claude seats get rows from the fleet-usage mod; Codex seats (consultant on account codex,
watchman on codex-fallback: harness/codex/appserver_seats.json) run no mod, so this reads
their session rollouts and emits rows in the SAME schema (pantheon-fleet-usage-turn/v1,
harness "codex"), so tools/fleet_usage_ledger.py reads both. Codex is a separate OpenAI pool:
the ledger reports it and never folds it into the Claude seven_day share.

SOURCE (D11: established in-repo, nothing invented). ``~/.codex/sessions/YYYY/MM/DD/
rollout-<ts>-<session>.jsonl`` (harness_idle.py, health_export.py). The token event shape is
the one Consultant verified against a live Codex 0.147.0 rollout (T-2957) and that
codex_context_lifecycle.py parses: a line ``{"type":"event_msg","timestamp":ISO,
"payload":{"type":"token_count","info":{"last_token_usage":U,"total_token_usage":U,...}}}``
with U = {input_tokens, cached_input_tokens, cache_write_input_tokens?, output_tokens,
reasoning_output_tokens, total_tokens}. ``task_started`` / ``turn_context`` / ``turn_aborted``
carry ``turn_id``; line 0 is ``session_meta`` whose payload ``id`` is the session; a subagent
rollout's session_meta adds ``parent_thread_id`` / ``agent_role`` (and thread_source
"subagent"). Pinned CLI: 0.147.0.

MAPPING to the v1 row:
  - usage per token_count event = the delta of total_token_usage from the previous event in
    the same rollout (immune to the duplicate token_count lines rollouts carry). A total that
    goes DOWN in any component falls back to that event's last_token_usage. Deltas are summed
    per turn_id; one row per turn (a token event before any turn_id goes to turn_id null).
  - input_tokens = input - cached (OpenAI counts cached input inside input_tokens; when cached
    exceeds input they are treated as disjoint), cache_read_input_tokens = cached,
    cache_creation_input_tokens = cache_write (0 when absent), output_tokens = output;
    reasoning_output_tokens is kept as an extra, informational field (inside output).
  - model: ``turn_context.payload.model`` when it is a string matching the safe pattern
    (model_source "turn_context"); else the seat's ``model`` in harness/codex/
    appserver_seats.json ("seat_config"); else "unknown" ("unknown"). The repo's fixtures
    strip turn_context to turn_id, so whether a live rollout carries the model there is NOT
    established: model_source makes a fallback guess impossible to mistake for an observation.
  - ts = the turn's last token event; duration_ms = last token event minus task_started
    (approximate; null when either is missing); reason "aborted" when a turn_aborted names
    the turn, else "unknown".
  - subagent rollouts: agent_id = the rollout's session id, agent_type = agent_role (safe
    pattern, else "other"), parent_agent_id = parent_thread_id. Root: all three null.
  - Only allowlisted keys are written. base_instructions, messages, reasoning text and every
    other payload field are never read into a row.

T-4889 PACING SIGNAL. The ``export`` command also performs the read-only Codex app-server
``account/rateLimits/read`` probe and atomically publishes
``<fleet-usage-dir>/turns/<seat>/quota.json``. An unavailable probe writes null windows, replacing
stale truth so the pacing gate fails open. No model turn is created.

COMMANDS.
  rows    --sessions DIR --seat SEAT [--since-days N]   rows as JSONL on stdout
  export  --sessions DIR --seat SEAT [--dir D] [--days N] [--commit]
          rewrites <dir>/turns/<seat>/<day>.jsonl for the last N UTC days (default 2) from the
          rollouts, atomically (temp file + rename). Deterministic and idempotent; the Codex
          seat's day files are written by this export alone. DRY RUN unless --commit. Never
          creates the top-level dir. With --commit, also refreshes turns/<seat>/quota.json from the
          app-server quota probe. Refuses unless --seat is the caller's own seat
          (PANTHEON_FLEET_USAGE_SEAT, then USER, then LOGNAME), and never replaces a day file
          that holds a non-codex row. It creates turns/ 3775 (setgid + sticky) and turns/<seat>
          2755 when missing, as the mod does.

ROLLOUT NOTE (named, not done): provision <fleet-usage-dir>/turns root-owned, group
pantheon-shared, mode 3775, before any seat writes, so no seat account owns the shared
turns/ dir and the sticky bit protects every seat dir from the start. It must run AS the codex account (its rollouts are
          private); scheduling it (a timer per codex account) is a rollout step, not done here.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from tools import fleet_usage_ledger as _ledger
except ImportError:  # run as a script from tools/
    import fleet_usage_ledger as _ledger  # type: ignore

try:
    from tools.harness_adapter.provider_quota import (
        CodexAppServerQuotaObserver,
        read_codex_app_server_rate_limits,
    )
except ImportError:  # run as a script from tools/
    from harness_adapter.provider_quota import (  # type: ignore
        CodexAppServerQuotaObserver,
        read_codex_app_server_rate_limits,
    )

PINNED_CODEX_CLI_VERSION = "0.147.0"
SEATS_FILE = REPO_ROOT / "harness" / "codex" / "appserver_seats.json"
# Same filename pattern as codex_context_lifecycle._SESSION_FILENAME_RE.
# Anchored with \Z (review N4: Python's $ also matches before a trailing newline).
ROLLOUT_RE = re.compile(r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(?P<session>.+)\.jsonl\Z")
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}\Z")
TYPE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}\Z")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@\[\]-]{1,128}\Z")
SUBAGENT_KEYS = ("parent_thread_id", "agent_path", "agent_nickname", "forked_from_id",
                 "agent_role", "multi_agent_version", "subagent_history_start_ordinal")
USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens")


def _id(v) -> str | None:
    return v if isinstance(v, str) and ID_RE.match(v) else None


def _count(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _ts(v) -> float | None:
    if not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _usage(u) -> dict | None:
    if not isinstance(u, dict):
        return None
    return {k: _count(u.get(k)) for k in USAGE_FIELDS}


def seat_config_model(seat: str, seats_file: Path = SEATS_FILE) -> str | None:
    try:
        doc = json.loads(seats_file.read_text(encoding="utf-8"))
        model = doc["seats"][seat]["model"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return model if isinstance(model, str) and MODEL_RE.match(model) else None


def _new_turn() -> dict:
    return {"usage": {k: 0 for k in USAGE_FIELDS}, "first": None, "last": None,
            "started": None, "model": None, "aborted": False, "seen_tokens": False}


def rows_from_rollout(path: Path, seat: str, fallback_model: str | None) -> list[dict]:
    """One v1 row per turn of one rollout file. Malformed lines are skipped; never raises
    on content (an unreadable file gives [])."""
    try:
        lines = path.read_bytes().splitlines()
    except OSError:
        return []
    m = ROLLOUT_RE.match(path.name)
    session = _id(m.group("session")) if m else None
    agent_id = agent_type = parent = None
    turns: dict = {}
    order: list = []
    current = None
    prev_total = None

    def turn(tid):
        if tid not in turns:
            turns[tid] = _new_turn()
            order.append(tid)
        return turns[tid]

    for idx, raw in enumerate(lines):
        try:
            rec = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(rec, dict):
            continue
        rtype = rec.get("type")
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            continue
        if idx == 0 and rtype == "session_meta":
            session = _id(payload.get("id")) or session
            is_sub = payload.get("thread_source") == "subagent" or any(
                k in payload for k in SUBAGENT_KEYS)
            if is_sub:
                agent_id = session
                role = payload.get("agent_role")
                agent_type = role if isinstance(role, str) and TYPE_RE.match(role) else "other"
                parent = _id(payload.get("parent_thread_id"))
            continue
        ptype = payload.get("type")
        tid = _id(payload.get("turn_id"))
        if rtype == "turn_context" and tid:
            current = tid
            model = payload.get("model")
            if isinstance(model, str) and MODEL_RE.match(model):
                turn(tid)["model"] = model
            else:
                turn(tid)
            continue
        if rtype != "event_msg":
            continue
        if ptype == "task_started" and tid:
            current = tid
            t = turn(tid)
            if t["started"] is None:
                t["started"] = _ts(rec.get("timestamp"))
            continue
        if ptype == "turn_aborted" and tid:
            turn(tid)["aborted"] = True
            continue
        if ptype != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        total = _usage(info.get("total_token_usage"))
        last = _usage(info.get("last_token_usage"))
        if total is None and last is None:
            continue
        if total is not None and prev_total is not None:
            delta = {k: total[k] - prev_total[k] for k in USAGE_FIELDS}
            if any(v < 0 for v in delta.values()):
                delta = last or {k: 0 for k in USAGE_FIELDS}
        elif total is not None:
            delta = total if last is None else last
        else:
            delta = last
        if total is not None:
            prev_total = total
        t = turn(current)
        for k in USAGE_FIELDS:
            t["usage"][k] += delta[k]
        t["seen_tokens"] = True
        ts = _ts(rec.get("timestamp"))
        if ts is not None:
            t["first"] = ts if t["first"] is None else t["first"]
            t["last"] = ts
    rows = []
    for tid in order:
        t = turns[tid]
        if not t["seen_tokens"] or t["last"] is None:
            continue
        u = t["usage"]
        if all(v == 0 for v in u.values()):
            continue
        cached, inp = u["cached_input_tokens"], u["input_tokens"]
        uncached = inp - cached if cached <= inp else inp
        if t["model"]:
            model, source = t["model"], "turn_context"
        elif fallback_model:
            model, source = fallback_model, "seat_config"
        else:
            model, source = "unknown", "unknown"
        duration = None
        if t["started"] is not None and t["last"] >= t["started"]:
            duration = int(round((t["last"] - t["started"]) * 1000))
        rows.append({
            "schema": _ledger.SCHEMA,
            "ts": int(t["last"]),
            "harness": "codex",
            "seat": seat,
            "session_id": session,
            "turn_id": tid,
            "agent_id": agent_id,
            "agent_type": agent_type,
            "parent_agent_id": parent,
            "model": model,
            "model_source": source,
            "input_tokens": uncached,
            "output_tokens": u["output_tokens"],
            "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": u["cache_write_input_tokens"],
            "duration_ms": duration,
            "reason": "aborted" if t["aborted"] else "unknown",
            "reasoning_output_tokens": u["reasoning_output_tokens"],
        })
    # Allowlist on the way out, in the reader's key order.
    return [{k: r[k] for k in _ledger.CODEX_ROW_KEYS} for r in rows]


def iter_rollouts(sessions: Path, modified_since: float | None = None):
    """Rollout files under `sessions`, sorted; regular files only (symlinks skipped)."""
    try:
        candidates = sorted(sessions.rglob("rollout-*.jsonl"))
    except OSError:
        return
    for path in candidates:
        if not ROLLOUT_RE.match(path.name) or path.is_symlink() or not path.is_file():
            continue
        if modified_since is not None:
            try:
                if path.stat().st_mtime < modified_since:
                    continue
            except OSError:
                continue
        yield path


def collect_rows(sessions: Path, seat: str, *, since_ts: float | None = None,
                 seats_file: Path = SEATS_FILE) -> list[dict]:
    fallback = seat_config_model(seat, seats_file)
    rows = []
    for path in iter_rollouts(sessions, modified_since=since_ts):
        for row in rows_from_rollout(path, seat, fallback):
            if since_ts is None or row["ts"] >= since_ts:
                rows.append(row)
    rows.sort(key=lambda r: (r["ts"], r["session_id"] or "", r["turn_id"] or ""))
    return rows


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def _holds_claude_rows(path: Path) -> bool:
    """True when an existing day file holds any harness "claude" row (or cannot be read, which
    is treated the same: refuse rather than overwrite what we could not check)."""
    try:
        lines = path.read_bytes().splitlines()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    for raw in lines:
        try:
            doc = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(doc, dict) and doc.get("harness") != "codex":
            return True
    return False


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def quota_gate_record(seat: str, snapshot, *, now: datetime) -> dict:
    """Normalize the Codex app-server snapshot into the shared gate record.

    An unavailable observation deliberately writes null windows, replacing any stale
    positive reading for this seat so the gate fails open instead of holding on old truth.
    """
    now = _aware(now).astimezone(timezone.utc)

    def window(names: set[str]):
        if getattr(snapshot, "freshness", None) != "fresh":
            return None
        for item in tuple(getattr(snapshot, "windows", ()) or ()):
            name = str(getattr(item, "name", "")).strip().lower().replace(" ", "-")
            if name not in names:
                continue
            try:
                used = float(item.used_percent)
                reset = _aware(item.resets_at).timestamp()
            except (AttributeError, TypeError, ValueError):
                return None
            if not (0.0 <= used <= 100.0) or reset <= now.timestamp():
                return None
            return {"used_percentage": used, "resets_at": reset}
        return None

    return {
        "schema": "pantheon-fleet-usage/v1",
        "harness": "codex",
        "ts": now.timestamp(),
        "seat": seat,
        "seven_day": window({"week", "weekly", "weekly-quota", "weekly_remaining", "7d", "seven-day"}),
        "five_hour": window({"5h", "five-hour", "five_hour"}),
    }


def write_quota_gate_record(directory: Path, seat: str, snapshot, *, now: datetime) -> Path:
    if not _ledger.SEAT_RE.match(seat):
        raise ValueError(f"bad seat name {seat!r}")
    if not directory.is_dir():
        raise ValueError(f"{directory} does not exist (it is provisioned, never created here)")
    # Codex service accounts intentionally are not members of pantheon-shared and
    # cannot create top-level records. The installer already provisions this
    # account-owned seat directory for the rollout export, so keep the provider
    # quota beside those rows instead of widening shared-directory permissions.
    seat_dir = directory / "turns" / seat
    if not seat_dir.is_dir():
        raise ValueError(f"{seat_dir} does not exist (it is provisioned, never created here)")
    target = seat_dir / "quota.json"
    if target.is_symlink():
        raise ValueError(f"{target} is a symlink; refusing")
    body = json.dumps(quota_gate_record(seat, snapshot, now=now), separators=(",", ":")) + "\n"
    fd, tmp = tempfile.mkstemp(dir=seat_dir, prefix=".quota.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def read_live_quota_snapshot(*, now: datetime):
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    executable = Path(os.environ.get("PANTHEON_CODEX_BIN") or "/usr/bin/codex")
    observer = CodexAppServerQuotaObserver(
        lambda: read_codex_app_server_rate_limits(executable, codex_home=codex_home),
        profile="codex/interactive-chatgpt",
        now=lambda: _aware(now).astimezone(timezone.utc),
    )
    return observer.read()


def export(sessions: Path, seat: str, directory: Path, *, days: int = 2, commit: bool = False,
           now: float | None = None, seats_file: Path = SEATS_FILE, env=None) -> dict:
    """Rewrite the last `days` UTC day files of a Codex seat from its rollouts. Dry run
    unless commit=True. Review F5: the seat must be the caller's own (resolved as the mod and
    prune resolve it), and a day file holding any non-codex row is never replaced."""
    if not _ledger.SEAT_RE.match(seat):
        raise ValueError(f"bad seat name {seat!r}")
    mine = _ledger.own_seat(env)
    if seat != mine:
        raise ValueError(f"refusing seat {seat!r}: not this caller's own seat ({mine!r})")
    if days < 1:
        raise ValueError("--days must be >= 1")
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, timezone.utc).date()
    first = today - timedelta(days=days - 1)
    since = datetime(first.year, first.month, first.day, tzinfo=timezone.utc).timestamp()
    rows = collect_rows(sessions, seat, since_ts=since, seats_file=seats_file)
    by_day: dict = {}
    for d in range(days):
        by_day[(first + timedelta(days=d)).isoformat()] = []
    for row in rows:
        day = _day(row["ts"])
        if day in by_day:
            by_day[day].append(row)
    out = {"seat": seat, "commit": commit, "days": {d: len(v) for d, v in by_day.items()},
           "written": []}
    if not commit:
        return out
    if not directory.is_dir():
        raise ValueError(f"{directory} does not exist (it is provisioned, never created here)")
    turns = directory / "turns"
    seat_dir = turns / seat
    # turns/ 3775 (setgid + sticky) as the mod creates it (review N2); seat dir 2755.
    for path, mode in ((turns, 0o3775), (seat_dir, 0o2755)):
        if path.is_symlink():
            raise ValueError(f"{path} is a symlink; refusing")
        if not path.exists():
            try:
                path.mkdir()
                os.chmod(path, mode)
            except FileExistsError:
                pass
    for day, day_rows in by_day.items():
        if not day_rows:
            continue
        target = seat_dir / f"{day}.jsonl"
        if target.is_symlink():
            raise ValueError(f"{target} is a symlink; refusing")
        if _holds_claude_rows(target):
            raise ValueError(f"{target} holds non-codex rows; refusing to replace it")
        body = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in day_rows)
        fd, tmp = tempfile.mkstemp(dir=seat_dir, prefix=f".{day}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(body)
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        out["written"].append(str(target))
    return out


def build_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False (review F8): `--comm` must never stand in for `--commit`.
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rows", allow_abbrev=False)
    r.add_argument("--sessions", required=True)
    r.add_argument("--seat", required=True)
    r.add_argument("--since-days", type=int, default=None)
    e = sub.add_parser("export", allow_abbrev=False)
    e.add_argument("--sessions", required=True)
    e.add_argument("--seat", required=True)
    e.add_argument("--dir", default=os.environ.get(_ledger.ENV_DIR) or _ledger.DEFAULT_DIR)
    e.add_argument("--days", type=int, default=2)
    e.add_argument("--commit", action="store_true", help="write (default: dry run)")
    e.add_argument("--now", type=float, default=None)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "rows":
            since = None
            if args.since_days is not None:
                since = time.time() - args.since_days * 86400
            for row in collect_rows(Path(args.sessions), args.seat, since_ts=since):
                print(json.dumps(row, separators=(",", ":")))
            return 0
        out = export(Path(args.sessions), args.seat, Path(args.dir), days=args.days,
                     commit=args.commit, now=args.now)
        if args.commit:
            observed_at = datetime.fromtimestamp(
                time.time() if args.now is None else args.now, timezone.utc
            )
            snapshot = read_live_quota_snapshot(now=observed_at)
            quota_path = write_quota_gate_record(
                Path(args.dir), args.seat, snapshot, now=observed_at
            )
            out["quota_record"] = str(quota_path)
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
