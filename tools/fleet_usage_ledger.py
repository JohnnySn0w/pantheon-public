#!/usr/bin/env python3
"""tools/fleet_usage_ledger.py — the READ side of the per-seat usage ledger (T-4536).

ARC INTENT (the operator TG 10732, "get seat usage data out in a clean way"): make Claude-seat
token burn attributable per seat / worker (subagent) / model / day, so quota decisions rest on
data rather than on the pool-wide percentage alone.

SOURCE. The fleet-usage mod (tools/mods/fleet-usage, turn.complete hook) appends one row per
turn to ``<fleet-usage-dir>/turns/<seat>/<YYYY-MM-DD>.jsonl`` (UTC day). Codex seats get rows of
the same schema from ``tools/fleet_usage_codex.py export``. Schema
``pantheon-fleet-usage-turn/v1``; the committed contract fixture
``tools/mods/fleet-usage/contract/fleet-usage-turn-row.ts`` pins the Claude row on both sides.
Rows carry token counts, ids and a model name only: never content. This reader re-checks that
on the way in, keeping only the allowlisted keys (CLAUDE_ROW_KEYS / CODEX_ROW_KEYS).

COMMANDS.
  rollup  [--dir D] [--today | --window | --since DAY [--until DAY]] [--by seat,model,...]
          token sums, turns and weighted units per group. Dimensions: seat, role, model, day,
          agent_type ("main" for the main loop), worker (agent id, "main"), harness.
  share   [--dir D] [--weights in=1,out=5,cr=0.1,cw=1.25]
          each Claude seat's estimated share of the current seven_day window, from the
          freshest valid gate reading (tools/fleet_usage_gate.load_readings).
  prune   --seat SEAT --older-than-days N [--dir D] [--commit]
          retention for the caller's OWN seat dir; dry-run unless --commit. Folds first, and
          refuses (keeps, exit 3) any old day not folded with its file's current sha (T-5962).
  fold    --seat SEAT [--commit]
          fold the caller's own complete days into turns/<seat>/aggregate-v1.json (T-5962).
  history --period day|week|month|quarter|year [--at T] [--last N] [--by ...] [--seat S]
          [--harness H] [--series]
          period aggregates merged across every seat's aggregate plus unfolded raw days:
          per-turn n/mean/stddev/min/max, totals, units, usd_est, share of the Claude pool.
  markers [--since T]   the committed change-marker log plus seat/model first-seen markers.
  Every command takes --json and --now (epoch seconds, for reproducible output).
  The period store, its merge math and its alignment are documented in tools/fleet_usage_agg.py.

WEIGHTING (the share estimate). An ESTIMATE, a token-class price-ratio proxy.
It is NOT Anthropic's quota formula, which is not published. Each token class is weighted by
its list price relative to that model's own uncached input price:
  units = w_in*input + w_out*output + w_cr*cache_read + w_cw*cache_creation
Default (any model not listed below): in 1, out 5, cache_read 0.1, cache_creation 1.25.
Per-model cache_read: claude-opus-5-5 0.05; claude-fable-5-1 and claude-mythos-5-1 0.025.
Source (verified 2026-11-02 for T-4536), the claude-api skill bundled with Claude Code 2.1.316:
shared/model-migration.md (around :2039, Claude Opus 5.5 pricing): $4 / $20 per MTok,
5-minute cache writes $5 (1.25x), cache reads $0.20 (0.05x base input), so output 5x input;
shared/prompt-caching.md (around :144, "Economics"): cache reads ~0.1x base input, 0.05x on
Opus 5.5, 0.025x on Fable 5.1 / Mythos 5.1; cache writes 1.25x for the 5-minute TTL (2x for
1-hour). The same skill's model table lists output at 5x input for every current model
(Sonnet 5.5 $2/$10, Fable 5.1 $10/$50, Opus 5/4.x $5/$25). The turn's usage does not split
cache writes by TTL, so 1.25 (the 5-minute default) is assumed. Units are per-model
input-token equivalents: no cross-model price scaling is applied (whether the Max window
weights models differently is not published). Override with --weights (applies to all models).
Codex rows are a separate OpenAI pool: they are reported, never folded into the Max share,
and carry no units.

MISSING / PARTIAL DATA never raises: a missing dir gives status "no-data"; malformed lines,
unknown schemas, a row whose seat disagrees with its dir, and badly named files are skipped
and counted in ``skipped``; duplicate (harness, seat, session, turn, agent) rows are dropped;
no valid seven_day reading gives shares with est_pct_points null and a reason.

Not built (heavier documented alternative): Claude Code's OpenTelemetry export
(CLAUDE_CODE_ENABLE_TELEMETRY plus an exporter and a collector).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from tools import fleet_usage_gate as _gate
except ImportError:  # run as a script from tools/
    import fleet_usage_gate as _gate  # type: ignore

SCHEMA = "pantheon-fleet-usage-turn/v1"
SHARE_SCHEMA = "pantheon-fleet-usage-share/v1"
ROLLUP_SCHEMA = "pantheon-fleet-usage-rollup/v1"
ENV_DIR = "PANTHEON_FLEET_USAGE_DIR"
ENV_SEAT = "PANTHEON_FLEET_USAGE_SEAT"
DEFAULT_DIR = "/var/lib/pantheon/fleet-usage"
SEVEN_DAY_SECONDS = 7 * 24 * 3600
MAX_LINE_BYTES = 4096
MIN_PRUNE_DAYS = 8  # never prune inside the current seven_day window
# T-5962: the seat's period-aggregate store and its lock live in the seat dir; every reader
# here skips them (and the aggregate's temp files), never counting them as bad files.
AGG_FILE = "aggregate-v1.json"
AGG_LOCK_FILE = ".aggregate-v1.lock"
AGG_TMP_PREFIX = ".aggregate-v1."

# T-4536 review F1: `.` and `..` are refused (a seat name is a path component under turns/).
# Review N4: anchored with \Z, not $ (Python's $ also matches before a trailing newline, JS's
# does not), so these agree with the mod's patterns.
SEAT_RE = re.compile(r"^(?!\.\.?\Z)[A-Za-z0-9_.-]{1,64}\Z")
DAY_FILE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.jsonl\Z")

TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
# The ONLY keys a Claude row carries, in the mod's order (TURN_ROW_KEYS in
# tools/mods/fleet-usage/hooks/register.ts; the test pins both against the contract).
CLAUDE_ROW_KEYS = (
    "schema",
    "ts",
    "harness",
    "seat",
    "session_id",
    "turn_id",
    "agent_id",
    "agent_type",
    "parent_agent_id",
    "model",
    "model_source",
    *TOKEN_KEYS,
    "duration_ms",
    "reason",
)
CODEX_ROW_KEYS = CLAUDE_ROW_KEYS + ("reasoning_output_tokens",)
HARNESSES = ("claude", "codex")
DIMENSIONS = ("seat", "role", "model", "day", "agent_type", "worker", "harness")


@dataclass(frozen=True)
class Weights:
    input: float
    output: float
    cache_read: float
    cache_creation: float

    def units(self, row: dict) -> float:
        return (
            self.input * row["input_tokens"]
            + self.output * row["output_tokens"]
            + self.cache_read * row["cache_read_input_tokens"]
            + self.cache_creation * row["cache_creation_input_tokens"]
        )

    def as_dict(self) -> dict:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_creation": self.cache_creation,
        }


DEFAULT_WEIGHTS = Weights(input=1.0, output=5.0, cache_read=0.1, cache_creation=1.25)
# Only the models whose ratios differ from the default (see the module docstring's source).
MODEL_WEIGHTS: dict[str, Weights] = {
    "claude-opus-5-5": Weights(1.0, 5.0, 0.05, 1.25),
    "claude-fable-5-1": Weights(1.0, 5.0, 0.025, 1.25),
    "claude-mythos-5-1": Weights(1.0, 5.0, 0.025, 1.25),
}
WEIGHTS_SOURCE = (
    "claude-api skill (Claude Code 2.1.316): shared/model-migration.md (Opus 5.5 $4/$20, "
    "cache write $5, cache read $0.20) + shared/prompt-caching.md Economics (cache ratios); "
    "verified 2026-11-02 (T-4536)"
)
WEIGHTS_NOTE = (
    "ESTIMATE: token-class price-ratio proxy relative to each model's own input price; "
    "NOT Anthropic's quota formula (unpublished); cache writes assumed 5-minute TTL (1.25x); "
    "no cross-model scaling"
)


def weights_for(model: str, override: Weights | None = None) -> Weights:
    """The per-model ratios when the model is known, else the stated default. A model id
    matches a table key exactly or as a prefix followed by a non-alphanumeric character
    (``claude-opus-5-5[1m]`` matches; ``claude-opus-5-50`` would not)."""
    if override is not None:
        return override
    for key, w in MODEL_WEIGHTS.items():
        if model == key or (model.startswith(key) and not model[len(key)].isalnum()):
            return w
    return DEFAULT_WEIGHTS


# T-5962: each model's uncached INPUT list price in USD per million tokens, for the $-equivalent
# (usd_est = units x this / 1e6). An ESTIMATE on the same footing as the weights above. Source
# (verified 2026-11-02 for T-5962, D11), the claude-api skill bundled with Claude Code 2.1.316:
# shared/models.md:79 (Opus 5.5 $4 / $20), :80 (Opus 5 at Opus 4.8's pricing, $5 / $25),
# :85 (Sonnet 5.5 $2 / $10), :77 (Fable 5 / Mythos 5 priced as Fable 5.1);
# shared/model-migration.md:1758 (Fable 5.1 $10 / $50), :1764 (Mythos 5.1 = Fable 5.1 pricing),
# :1226 (Sonnet 5 $2 / $10), :2240 (Haiku 4.5 $1 / $5). Not priced: Haiku 5.5 (two rate cards
# chosen by prompt length, :2240) and every model not listed. Codex rows are never priced
# (an OpenAI pool; no price table in this repo). Changing this table or the weights requires a
# new "pricing" marker in tools/fleet_usage_markers.json (the test pins pricing_fingerprint()).
MODEL_INPUT_USD_PER_MTOK: dict[str, float] = {
    "claude-opus-5-5": 4.0,
    "claude-opus-5": 5.0,
    "claude-opus-4-8": 5.0,
    "claude-sonnet-5-5": 2.0,
    "claude-sonnet-5": 2.0,
    "claude-fable-5-1": 10.0,
    "claude-fable-5": 10.0,
    "claude-mythos-5-1": 10.0,
    "claude-mythos-5": 10.0,
    "claude-haiku-4-5": 1.0,
}
UNPRICED_REASONS: dict[str, str] = {
    "claude-haiku-5-5": "two rate cards chosen by prompt length; not priced",
}
PRICE_SOURCE = (
    "claude-api skill (Claude Code 2.1.316): shared/models.md + shared/model-migration.md list "
    "prices; verified 2026-11-02 (T-5962)"
)


_PRICE_SUFFIX_RE = re.compile(r"(-\d{8})?(\[[^\]]*\])?\Z")


def _model_matches(model: str, key: str) -> bool:
    """Exact id, optionally followed by a -YYYYMMDD date and/or a [..] context tag. Stricter
    than weights_for's prefix rule on purpose: an unknown sibling (claude-opus-5-6 against
    claude-opus-5) must be UNPRICED, never priced as its neighbour."""
    return model.startswith(key) and _PRICE_SUFFIX_RE.match(model[len(key):]) is not None


def input_usd_per_mtok(model: str) -> tuple[float | None, str | None]:
    """(price, None) for a priced model, else (None, reason)."""
    for key in sorted(UNPRICED_REASONS, key=len, reverse=True):
        if _model_matches(model, key):
            return None, UNPRICED_REASONS[key]
    for key in sorted(MODEL_INPUT_USD_PER_MTOK, key=len, reverse=True):
        if _model_matches(model, key):
            return MODEL_INPUT_USD_PER_MTOK[key], None
    return None, "model not in the price table"


def pricing_fingerprint() -> str:
    """sha256 of every number the units and $-equivalent depend on. The newest "pricing"
    marker in tools/fleet_usage_markers.json must carry it (test-pinned)."""
    doc = {
        "default": DEFAULT_WEIGHTS.as_dict(),
        "per_model": {m: w.as_dict() for m, w in sorted(MODEL_WEIGHTS.items())},
        "input_usd_per_mtok": dict(sorted(MODEL_INPUT_USD_PER_MTOK.items())),
        "unpriced": sorted(UNPRICED_REASONS),
    }
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode("utf-8")).hexdigest()


def parse_weights(text: str) -> Weights:
    names = {"in": "input", "out": "output", "cr": "cache_read", "cw": "cache_creation"}
    vals = DEFAULT_WEIGHTS.as_dict()
    for part in text.split(","):
        key, sep, raw = part.partition("=")
        key = key.strip()
        if not sep or key not in names:
            raise ValueError(f"bad weight {part!r}; use in=,out=,cr=,cw=")
        num = float(raw)
        if not (num >= 0) or num == float("inf"):
            raise ValueError(f"bad weight value {raw!r}")
        vals[names[key]] = num
    return Weights(**vals)


# ------------------------------------------------------------------------------- reading


def _is_count(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _opt_str(v) -> bool:
    return v is None or isinstance(v, str)


def normalize_row(doc, seat_dir: str) -> dict | None:
    """The row with ONLY its allowlisted keys, or None when it is not a valid v1 row of the
    seat whose dir holds it."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return None
    harness = doc.get("harness")
    if harness not in HARNESSES:
        return None
    if doc.get("seat") != seat_dir:
        return None
    ts = doc.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or ts < 0:
        return None
    if not all(_is_count(doc.get(k)) for k in TOKEN_KEYS):
        return None
    for key in ("session_id", "turn_id", "agent_id", "agent_type", "parent_agent_id"):
        if not _opt_str(doc.get(key)):
            return None
    model = doc.get("model")
    if not isinstance(model, str) or not model:
        return None
    keys = CODEX_ROW_KEYS if harness == "codex" else CLAUDE_ROW_KEYS
    row = {k: doc.get(k) for k in keys}
    if harness == "codex" and not _is_count(row.get("reasoning_output_tokens")):
        row["reasoning_output_tokens"] = 0
    return row


@dataclass
class ReadResult:
    rows: list
    skipped: dict
    status: str


def is_reserved_name(name: str) -> bool:
    """The seat's aggregate, its lock and its temp files (T-5962)."""
    return name in (AGG_FILE, AGG_LOCK_FILE) or name.startswith(AGG_TMP_PREFIX)


def _day_of_file(name: str) -> date | None:
    m = DAY_FILE_RE.match(name)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def read_rows(
    directory,
    *,
    start_ts: float | None = None,
    end_ts: float | None = None,
    seats: set | None = None,
) -> ReadResult:
    """Every valid row under <directory>/turns with start_ts <= ts < end_ts. Never raises."""
    skipped = {"bad_line": 0, "bad_row": 0, "bad_file": 0, "duplicate": 0}
    rows: list = []
    turns = Path(directory) / "turns"
    try:
        seat_dirs = sorted(p for p in turns.iterdir() if p.is_dir())
    except OSError:
        return ReadResult([], skipped, "no-data")
    first_day = (
        datetime.fromtimestamp(start_ts, timezone.utc).date() if start_ts is not None else None
    )
    last_day = (
        datetime.fromtimestamp(end_ts, timezone.utc).date() if end_ts is not None else None
    )
    seen: set = set()
    for seat_path in seat_dirs:
        seat = seat_path.name
        if not SEAT_RE.match(seat) or (seats is not None and seat not in seats):
            continue
        try:
            files = sorted(seat_path.iterdir())
        except OSError:
            continue
        for path in files:
            if is_reserved_name(path.name):
                continue
            day = _day_of_file(path.name)
            if day is None:
                skipped["bad_file"] += 1
                continue
            if (first_day and day < first_day) or (last_day and day > last_day):
                continue
            try:
                with path.open("rb") as fh:
                    data = fh.read()
            except OSError:
                skipped["bad_file"] += 1
                continue
            for row in _parse_lines(data, seat, seen, skipped):
                if start_ts is not None and row["ts"] < start_ts:
                    continue
                if end_ts is not None and row["ts"] >= end_ts:
                    continue
                rows.append(row)
    return ReadResult(rows, skipped, "ok" if rows else "no-data")


def _parse_lines(data: bytes, seat: str, seen: set, skipped: dict) -> list:
    """The valid, de-duplicated rows of one day file's bytes; counts what it skips."""
    rows = []
    for raw in data.splitlines():
        if not raw.strip():
            continue
        if len(raw) > MAX_LINE_BYTES:
            skipped["bad_line"] += 1
            continue
        try:
            doc = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            skipped["bad_line"] += 1
            continue
        row = normalize_row(doc, seat)
        if row is None:
            skipped["bad_row"] += 1
            continue
        if row["turn_id"] is not None:
            key = (row["harness"], seat, row["session_id"], row["turn_id"], row["agent_id"])
            if key in seen:
                skipped["duplicate"] += 1
                continue
            seen.add(key)
        rows.append(row)
    return rows


def parse_day_bytes(data: bytes, seat: str) -> tuple[list, dict]:
    """(rows, skipped) of one day file's bytes, de-duplicated within the file (T-5962 fold)."""
    skipped = {"bad_line": 0, "bad_row": 0, "duplicate": 0}
    return _parse_lines(data, seat, set(), skipped), skipped


# ------------------------------------------------------------------------------- rollups


def role_of(seat: str) -> str:
    """The seat's role id via agent_role_map; "unknown" when it does not resolve."""
    try:
        try:
            from tools import agent_role_map as arm
        except ImportError:
            import agent_role_map as arm  # type: ignore
        persona = seat if seat in arm.AGENT_ROLE_MAP else (arm.account_persona(seat) or seat)
        role = arm.AGENT_ROLE_MAP.get(persona)
        return role if isinstance(role, str) and role else "unknown"
    except Exception:  # noqa: BLE001 — attribution must never break a read
        return "unknown"


def _dim_value(row: dict, dim: str, roles: dict) -> str:
    if dim == "seat":
        return row["seat"]
    if dim == "role":
        if row["seat"] not in roles:
            roles[row["seat"]] = role_of(row["seat"])
        return roles[row["seat"]]
    if dim == "model":
        return row["model"]
    if dim == "day":
        return datetime.fromtimestamp(row["ts"], timezone.utc).date().isoformat()
    if dim == "agent_type":
        return "main" if row["agent_id"] is None else (row["agent_type"] or "unknown")
    if dim == "worker":
        return "main" if row["agent_id"] is None else row["agent_id"]
    if dim == "harness":
        return row["harness"]
    raise ValueError(f"unknown dimension {dim!r}")


def _empty_tokens() -> dict:
    return {k: 0 for k in TOKEN_KEYS}


def row_units(row: dict, override: Weights | None = None) -> float | None:
    if row["harness"] != "claude":
        return None
    return weights_for(row["model"], override).units(row)


def rollup(rows: list, by=("seat",), override: Weights | None = None) -> list:
    """Groups sorted by key: {key:{dim:value}, turns, tokens{4}, units|None}. Units are
    summed over Claude rows only; a group of Codex rows only has units None."""
    for dim in by:
        if dim not in DIMENSIONS:
            raise ValueError(f"unknown dimension {dim!r}; choose from {', '.join(DIMENSIONS)}")
    roles: dict = {}
    groups: dict = {}
    for row in rows:
        key = tuple(_dim_value(row, d, roles) for d in by)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"turns": 0, "tokens": _empty_tokens(), "units": None}
        g["turns"] += 1
        for k in TOKEN_KEYS:
            g["tokens"][k] += row[k]
        u = row_units(row, override)
        if u is not None:
            g["units"] = (g["units"] or 0.0) + u
    out = []
    for key in sorted(groups):
        g = groups[key]
        units = None if g["units"] is None else round(g["units"], 3)
        out.append({"key": dict(zip(by, key)), "turns": g["turns"], "tokens": g["tokens"],
                    "units": units})
    return out


def seven_day_share(
    directory, now: float, override: Weights | None = None
) -> dict:
    """Each Claude seat's estimated share of the current seven_day window."""
    readings = _gate.load_readings(directory, now)
    reading = readings.get("seven_day")
    weights_doc = {
        "source": WEIGHTS_SOURCE,
        "note": WEIGHTS_NOTE,
        "default": DEFAULT_WEIGHTS.as_dict(),
        "per_model": {m: w.as_dict() for m, w in sorted(MODEL_WEIGHTS.items())},
        "override": override.as_dict() if override else None,
    }
    doc = {
        "schema": SHARE_SCHEMA,
        "generated_at": int(now),
        "estimate": True,
        "weights": weights_doc,
        "window": None,
        "status": "ok",
        "reason": None,
        "seats": [],
        "codex": [],
        "skipped": {},
    }
    if reading is None:
        start = now - SEVEN_DAY_SECONDS
        doc["reason"] = "no valid seven_day reading: shares only, est_pct_points null"
    else:
        start = reading.resets_at - SEVEN_DAY_SECONDS
        doc["window"] = {
            "start": int(start),
            "end": int(now),
            "resets_at": int(reading.resets_at),
            "used_percentage": reading.pct,
            "reading_seat": reading.seat,
            "reading_ts": int(reading.ts),
        }
    result = read_rows(directory, start_ts=start, end_ts=now + 1)
    doc["skipped"] = result.skipped
    claude = [r for r in result.rows if r["harness"] == "claude"]
    codex = [r for r in result.rows if r["harness"] == "codex"]
    by_seat = rollup(claude, ("seat",), override)
    total = sum(g["units"] or 0.0 for g in by_seat)
    for g in by_seat:
        seat = g["key"]["seat"]
        share = (g["units"] or 0.0) / total if total > 0 else None
        est = None
        if share is not None and reading is not None:
            est = round(share * reading.pct, 3)
        doc["seats"].append({
            "seat": seat,
            "role": role_of(seat),
            "turns": g["turns"],
            "tokens": g["tokens"],
            "units": g["units"],
            "share": None if share is None else round(share, 6),
            "est_pct_points": est,
        })
    for g in rollup(codex, ("seat",), override):
        doc["codex"].append({"seat": g["key"]["seat"], "turns": g["turns"],
                             "tokens": g["tokens"],
                             "note": "OpenAI pool: not part of the weekly Max %"})
    if not claude:
        doc["status"] = "no-data"
    return doc


# ------------------------------------------------------------------------------- prune


def own_seat(env=None) -> str | None:
    """The caller's seat, resolved exactly as the mod resolves it: PANTHEON_FLEET_USAGE_SEAT,
    then USER, then LOGNAME, the first that is a valid seat name."""
    env = os.environ if env is None else env
    for key in (ENV_SEAT, "USER", "LOGNAME"):
        val = env.get(key)
        if isinstance(val, str) and SEAT_RE.match(val):
            return val
    return None


class PruneRefused(Exception):
    pass


def checked_seat_dir(directory, seat: str, env=None) -> Path | None:
    """The caller's OWN seat dir, or None when it does not exist yet (a fresh seat with no rows:
    nothing to fold or prune, not a failure). Raises PruneRefused for a bad or foreign seat, a
    symlinked or non-directory seat dir, or one this user does not own."""
    if not isinstance(seat, str) or not SEAT_RE.match(seat):
        raise PruneRefused(f"bad seat name {seat!r}")
    mine = own_seat(env)
    if seat != mine:
        raise PruneRefused(f"refusing seat {seat!r}: not this caller's own seat ({mine!r})")
    seat_dir = Path(directory) / "turns" / seat
    try:
        st = os.lstat(seat_dir)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PruneRefused(f"cannot stat seat dir {seat_dir}: {exc.strerror}") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise PruneRefused(f"{seat_dir} is not a real directory (symlink or other)")
    if st.st_uid != os.getuid():
        raise PruneRefused(f"{seat_dir} is not owned by this user")
    return seat_dir


def _agg():
    try:
        from tools import fleet_usage_agg as agg
    except ImportError:  # run as a script from tools/
        import fleet_usage_agg as agg  # type: ignore
    return agg


def prune(
    directory,
    seat: str,
    older_than_days: int,
    *,
    commit: bool = False,
    today: date | None = None,
    env=None,
    now=None,
) -> dict:
    """Retention for the caller's OWN seat dir. Candidates are regular files (never symlinks)
    named exactly YYYY-MM-DD.jsonl (a real date) older than `older_than_days` UTC days.
    T-5962: the seat's complete days are folded into its aggregate FIRST, and a candidate is
    deleted only when it is folded with its file's current sha (it is sealed into the long-term
    tiers before the delete); any other candidate is kept and listed in "refused".
    Deletes only with commit=True; otherwise reports what it would delete."""
    if not isinstance(seat, str) or not SEAT_RE.match(seat):
        raise PruneRefused(f"bad seat name {seat!r}")
    if not isinstance(older_than_days, int) or older_than_days < MIN_PRUNE_DAYS:
        raise PruneRefused(f"--older-than-days must be >= {MIN_PRUNE_DAYS}")
    return _agg().prune(directory, seat, older_than_days, commit=commit, today=today, env=env,
                        now=now)


# ------------------------------------------------------------------------------- CLI


def _parse_day(text: str) -> date:
    return date.fromisoformat(text)


def _window_bounds(args, directory, now: float) -> tuple[float | None, float | None, str]:
    if args.today:
        d = datetime.fromtimestamp(now, timezone.utc).date()
        start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
        return start, now + 1, f"today {d.isoformat()} UTC"
    if args.window:
        reading = _gate.load_readings(directory, now).get("seven_day")
        if reading is None:
            return now - SEVEN_DAY_SECONDS, now + 1, "last 7 days (no valid seven_day reading)"
        return reading.resets_at - SEVEN_DAY_SECONDS, now + 1, "current seven_day window"
    start = end = None
    if args.since:
        d = _parse_day(args.since)
        start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
    if args.until:
        d = _parse_day(args.until) + timedelta(days=1)
        end = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
    return start, end, "all rows" if start is None and end is None else "day range"


def _fmt_tokens(t: dict) -> str:
    return (f"in {t['input_tokens']:,} out {t['output_tokens']:,} "
            f"cr {t['cache_read_input_tokens']:,} cw {t['cache_creation_input_tokens']:,}")


def _cmd_rollup(args) -> int:
    now = float(args.now) if args.now is not None else time.time()
    by = tuple(d.strip() for d in args.by.split(",") if d.strip())
    override = parse_weights(args.weights) if args.weights else None
    start, end, label = _window_bounds(args, args.dir, now)
    result = read_rows(args.dir, start_ts=start, end_ts=end)
    groups = rollup(result.rows, by, override)
    doc = {"schema": ROLLUP_SCHEMA, "generated_at": int(now), "range": label,
           "start": None if start is None else int(start),
           "end": None if end is None else int(end), "by": list(by),
           "status": result.status, "groups": groups, "skipped": result.skipped,
           "weights_note": WEIGHTS_NOTE, "weights_source": WEIGHTS_SOURCE}
    if args.json:
        print(json.dumps(doc, indent=2, sort_keys=True))
        return 0
    print(f"fleet usage ledger rollup ({label}; by {', '.join(by)}); status {result.status}")
    for g in groups:
        key = " ".join(f"{k}={v}" for k, v in g["key"].items())
        units = "-" if g["units"] is None else f"{g['units']:,.0f}"
        print(f"  {key}: turns {g['turns']} units {units} ({_fmt_tokens(g['tokens'])})")
    print(f"  skipped {result.skipped}")
    print(f"  units: {WEIGHTS_NOTE}. Source: {WEIGHTS_SOURCE}")
    return 0


def _cmd_share(args) -> int:
    now = float(args.now) if args.now is not None else time.time()
    override = parse_weights(args.weights) if args.weights else None
    doc = seven_day_share(args.dir, now, override)
    if args.json:
        print(json.dumps(doc, indent=2, sort_keys=True))
        return 0
    w = doc["window"]
    if w:
        print(f"seven_day {w['used_percentage']}% used (reading from {w['reading_seat']}); "
              f"window {w['start']}..{w['end']}, resets {w['resets_at']}")
    else:
        print(f"seven_day: {doc['reason']}")
    for s in doc["seats"]:
        est = "-" if s["est_pct_points"] is None else f"{s['est_pct_points']} pts"
        share = "-" if s["share"] is None else f"{100 * s['share']:.1f}%"
        print(f"  {s['seat']} ({s['role']}): share {share}, est {est}, turns {s['turns']}")
    for c in doc["codex"]:
        print(f"  {c['seat']} [codex, {c['note']}]: turns {c['turns']} ({_fmt_tokens(c['tokens'])})")
    print(f"  {WEIGHTS_NOTE}. Source: {WEIGHTS_SOURCE}")
    return 0


def _cmd_prune(args) -> int:
    now = int(float(args.now)) if args.now else None
    today = datetime.fromtimestamp(now, timezone.utc).date() if now is not None else None
    try:
        out = prune(args.dir, args.seat, args.older_than_days, commit=args.commit, today=today,
                    now=now)
    except (PruneRefused, _agg().AggregateError) as exc:
        print(f"prune refused: {exc}", file=sys.stderr)
        return 2
    # T-5962: a day kept because it is not (or no longer) folded exits 3, so the unit's
    # OnFailure alert fires; nothing about it was deleted.
    rc = 3 if out["refused"] else 0
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
        return rc
    verb = "deleted" if args.commit else "would delete (dry run; pass --commit to delete)"
    names = out["deleted" if args.commit else "would_delete"]
    if out["status"] == "no-data":
        print(f"prune {out['dir']}: no seat dir yet (no rows); nothing to fold or prune")
        return 0
    fold = out["fold"]
    print(f"prune {out['dir']} older than {out['cutoff']}: {verb} {len(names)}, "
          f"kept {out['kept']}, ignored {len(out['ignored'])}; folded "
          f"{len(fold['folded']) + len(fold['refolded'])}, sealed {len(out['sealed'])}")
    for n in names:
        print(f"  {n}")
    for n in out["refused"]:
        print(f"  REFUSED {n}: {out['refused_reasons'][n]}", file=sys.stderr)
    return rc


def _cmd_fold(args) -> int:
    now = int(float(args.now)) if args.now else None
    today = datetime.fromtimestamp(now, timezone.utc).date() if now is not None else None
    agg = _agg()
    try:
        out = agg.fold(args.dir, args.seat, commit=args.commit, today=today, now=now)
    except (PruneRefused, agg.AggregateError) as exc:
        print(f"fold refused: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    if out["status"] == "no-data":
        print(f"fold {out['dir']}: no seat dir yet (no rows)")
        return 0
    mode = "" if args.commit else " (dry run; pass --commit to write)"
    print(f"fold {out['dir']}{mode}: folded {len(out['folded'])}, refolded "
          f"{len(out['refolded'])}, unchanged {out['unchanged']}, sealed-and-changed "
          f"{len(out['sealed_changed'])}, unreadable {len(out['unreadable'])}")
    return 0


def _fmt_num(v) -> str:
    return "-" if v is None else (f"{v:,.0f}" if isinstance(v, (int, float)) else str(v))


def _cmd_history(args) -> int:
    now = int(float(args.now)) if args.now is not None else None
    agg = _agg()
    by = tuple(d.strip() for d in args.by.split(",") if d.strip())
    seats = set(args.seat) if args.seat else None
    override = parse_weights(args.weights) if args.weights else None
    doc = agg.history(args.dir, args.period, at=args.at, last=args.last, by=by, seats=seats,
                      harness=args.harness, series=args.series, override=override, now=now)
    if args.json:
        print(json.dumps(doc, indent=2, sort_keys=True))
        return 0
    for p in doc["periods"]:
        state = "" if p["complete"] else " (in progress)"
        print(f"{p['period']} {p['start_iso']} .. {p['end_iso']}{state}; Claude pool units "
              f"{_fmt_num(p['pool']['claude_units'])}")
        for g in p["groups"]:
            key = " ".join(f"{k}={v}" for k, v in g["key"].items())
            share = g["share_of_claude_pool"]
            share_s = "-" if share is None else f"{100 * share:.1f}%"
            usd = "-" if g["usd_est"] is None else f"${g['usd_est']:,.2f}"
            out_t = g["per_turn"]["output_tokens"]
            mean = "-" if out_t["mean"] is None else f"{out_t['mean']:,.0f}"
            sd = "-" if out_t["stddev"] is None else f"{out_t['stddev']:,.0f}"
            print(f"  {key}: turns {g['turns']} units {_fmt_num(g['units'])} share {share_s} "
                  f"usd_est {usd}; output/turn mean {mean} sd {sd}")
    for m in doc["markers"]:
        print(f"  marker {m['at_iso']} [{m['scope']}] {m['label']}"
              + (f" ({m['ref']})" if m.get("ref") else ""))
    print(f"  {doc['estimate_note']}")
    return 0


def _cmd_markers(args) -> int:
    agg = _agg()
    views = agg.fleet_views(args.dir)
    since = agg._parse_at(args.since, 0) if args.since else None
    markers = [m for m in agg.all_markers(views) if since is None or m["ts"] >= since]
    if args.json:
        print(json.dumps({"schema": agg.MARKERS_SCHEMA, "markers": markers}, indent=2,
                         sort_keys=True))
        return 0
    for m in markers:
        src = "derived" if m["derived"] else (m.get("ref") or "")
        print(f"{m['at_iso']} [{m['scope']}] {m['kind']}: {m['label']} ({src})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False (review F8): `--comm` must never stand in for `--commit`.
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    p.add_argument("--dir", default=os.environ.get(ENV_DIR) or DEFAULT_DIR)
    p.add_argument("--json", action="store_true")
    p.add_argument("--now", default=None, help="epoch seconds (reproducible output)")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rollup", allow_abbrev=False)
    rng = r.add_mutually_exclusive_group()
    rng.add_argument("--today", action="store_true")
    rng.add_argument("--window", action="store_true")
    rng.add_argument("--since")
    r.add_argument("--until")
    r.add_argument("--by", default="seat")
    r.add_argument("--weights")
    r.set_defaults(func=_cmd_rollup)
    s = sub.add_parser("share", allow_abbrev=False)
    s.add_argument("--weights")
    s.set_defaults(func=_cmd_share)
    pr = sub.add_parser("prune", allow_abbrev=False)
    pr.add_argument("--seat", required=True)
    pr.add_argument("--older-than-days", type=int, required=True)
    pr.add_argument("--commit", action="store_true", help="actually delete (default: dry run)")
    pr.add_argument("--dry-run", action="store_true", help="the default; accepted for clarity")
    pr.set_defaults(func=_cmd_prune)
    fo = sub.add_parser("fold", allow_abbrev=False)
    fo.add_argument("--seat", required=True)
    fo.add_argument("--commit", action="store_true", help="write the aggregate (default: dry run)")
    fo.add_argument("--dry-run", action="store_true", help="the default; accepted for clarity")
    fo.set_defaults(func=_cmd_fold)
    h = sub.add_parser("history", allow_abbrev=False)
    h.add_argument("--period", required=True, choices=("day", "week", "month", "quarter", "year"))
    h.add_argument("--at", help="DATE or ISO instant; the period holding it (default: now)")
    h.add_argument("--last", type=int, default=1, help="this many periods ending at --at")
    h.add_argument("--by", default="seat", help="seat,harness,model,agent_class")
    h.add_argument("--seat", action="append", help="only this seat (repeatable)")
    h.add_argument("--harness", choices=HARNESSES)
    h.add_argument("--series", action="store_true", help="include the period's buckets")
    h.add_argument("--weights")
    h.set_defaults(func=_cmd_history)
    m = sub.add_parser("markers", allow_abbrev=False)
    m.add_argument("--since", help="DATE or ISO instant")
    m.set_defaults(func=_cmd_markers)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "commit", False) and getattr(args, "dry_run", False):
        parser.error("--commit and --dry-run are mutually exclusive")
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # T-5962: run main() in the IMPORTED module, so fleet_usage_agg (which imports this module)
    # and this entry point share one set of classes (PruneRefused, AggregateError).
    try:
        from tools import fleet_usage_ledger as _self
    except ImportError:
        import fleet_usage_ledger as _self  # type: ignore
    sys.exit(_self.main())
