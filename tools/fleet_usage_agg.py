"""tools/fleet_usage_agg.py — durable period aggregates for the per-seat usage ledger (T-5962).

ARC INTENT (the operator TG 1017-1032): long-horizon usage history that outlives the 35-day raw-row
retention, so seat token burn stays attributable per seat / harness / model / main-vs-subagent
over days, weeks, months, quarters and years.

STORE. One file per seat, in the seat's OWN dir: ``<dir>/turns/<seat>/aggregate-v1.json``
(schema ``pantheon-fleet-usage-agg/v1``). Only that seat writes it, under the same own-seat
guard as prune; it is written atomically (temp file in the same dir, fsync, rename, 0644) under
an flock on ``.aggregate-v1.lock``. Fleet-wide views merge every seat's file on read.

CELLS. A cell is a flat list of integers: ``[turns, n, sum, sumsq, min, max]`` with one
(n, sum, sumsq, min, max) group per field in FIELDS (the four token counts and duration_ms; n
is per field because duration_ms can be null). min/max are null when n is 0. Cells are keyed
by (harness, model, agent_class) where agent_class is "main" (no agent_id) or "sub".

MERGE. Two cells merge by adding turns, n, sum and sumsq and taking min of mins and max of
maxes: integer arithmetic, so a merge is EXACT for any grouping and order. Derived stats:
mean = sum/n and sample variance = (n*sumsq - sum^2) / (n*(n-1)); the numerator is an exact
integer, so there is no cancellation error. In exact arithmetic this equals Chan's parallel
variance update; the tests check that with Fractions.

TIERS. Every boundary below falls on a whole UTC hour, so every tier is an exact merge of
hourly cells.
  h1  UTC hour.                         Day series (24 buckets).  Kept H1_RETAIN_DAYS once sealed.
  h6  6 hours on the quota-week grid    Week series (28 buckets). Kept H6_RETAIN_DAYS once sealed.
      (starts 05, 11, 17, 23Z).
  d1  UTC day.                          Month series. Kept forever.
  w   quota week (Sunday 17:00Z, the    Quarter and year series. Kept forever.
      T-4510 seven_day reset) split where it crosses a UTC calendar-quarter boundary, so a
      quarter and a year are each an exact union of w cells.
Periods: day = UTC day; week = Sunday 17:00Z to Sunday 17:00Z; month, quarter and year are
UTC calendar periods.

FOLD AND SEAL. ``fold`` records, for every complete day file (day before today UTC), its
sha256 and its 24 hourly cells, replacing that day's record whenever the file's sha changes
(so it is idempotent and absorbs the hourly Codex re-export and late appends). A folded day
whose raw file still exists is OPEN: its coarse cells are derived from its hourly cells on
read. ``prune`` folds first, then for each day past retention refuses unless the day is folded
with the file's current sha; it SEALS the day (adds its hourly cells into the h6/d1/w tiers and
marks it sealed), writes the aggregate, and only then deletes the file. A crash between the
write and the delete leaves a sealed day whose file still exists; the next run deletes it
without sealing it twice. A sealed day is never refolded.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import stat
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from tools import fleet_usage_ledger as L
except ImportError:  # run as a script from tools/
    import fleet_usage_ledger as L  # type: ignore

AGG_SCHEMA = "pantheon-fleet-usage-agg/v1"
HISTORY_SCHEMA = "pantheon-fleet-usage-history/v1"
MARKERS_SCHEMA = "pantheon-fleet-usage-markers/v1"
AGG_FILE = L.AGG_FILE
LOCK_FILE = L.AGG_LOCK_FILE
TMP_PREFIX = L.AGG_TMP_PREFIX
MARKERS_FILE = Path(__file__).resolve().parent / "fleet_usage_markers.json"

FIELDS = L.TOKEN_KEYS + ("duration_ms",)
STATS = ("n", "sum", "sumsq", "min", "max")
CELL_LEN = 1 + len(FIELDS) * len(STATS)
TIERS = ("h1", "h6", "d1", "w")
SEALED_TIERS = ("h6", "d1", "w")
AGENT_CLASSES = ("main", "sub")
GROUP_DIMS = ("seat", "harness", "model", "agent_class")
PERIODS = ("day", "week", "month", "quarter", "year")

HOUR = 3600
H6 = 6 * HOUR
DAY = 24 * HOUR
WEEK = 7 * DAY
# 2026-10-11T17:00:00Z, a Sunday: the seven_day resets_at of the T-4510 contract fixture
# (tools/fixtures/fleet_usage/ledger/specialist.json). Every quota week starts a whole number of
# weeks from here.
QUOTA_WEEK_ANCHOR = 1791738000
H1_RETAIN_DAYS = 40
H6_RETAIN_DAYS = 400
MARKER_KINDS = ("pricing", "optimization", "seat", "model", "coverage", "other")


class AggregateError(Exception):
    """The seat's aggregate file exists but cannot be trusted; nothing is written over it."""


# ------------------------------------------------------------------------------- cells


def empty_cell() -> list:
    cell = [0]
    for _ in FIELDS:
        cell.extend((0, 0, 0, None, None))
    return cell


def cell_add_row(cell: list, row: dict) -> None:
    cell[0] += 1
    for i, field in enumerate(FIELDS):
        v = row.get(field)
        if not L._is_count(v):
            continue
        b = 1 + 5 * i
        cell[b] += 1
        cell[b + 1] += v
        cell[b + 2] += v * v
        cell[b + 3] = v if cell[b + 3] is None else min(cell[b + 3], v)
        cell[b + 4] = v if cell[b + 4] is None else max(cell[b + 4], v)


def merge_into(dst: list, src: list) -> list:
    dst[0] += src[0]
    for i in range(len(FIELDS)):
        b = 1 + 5 * i
        dst[b] += src[b]
        dst[b + 1] += src[b + 1]
        dst[b + 2] += src[b + 2]
        for j, pick in ((b + 3, min), (b + 4, max)):
            if src[j] is not None:
                dst[j] = src[j] if dst[j] is None else pick(dst[j], src[j])
    return dst


def merge_cells(*cells) -> list:
    out = empty_cell()
    for c in cells:
        merge_into(out, c)
    return out


def field_stats(cell: list, field: str) -> dict:
    b = 1 + 5 * FIELDS.index(field)
    n, s, q, mn, mx = cell[b:b + 5]
    mean = s / n if n else None
    var = (n * q - s * s) / (n * (n - 1)) if n > 1 else None
    return {"n": n, "sum": s, "mean": mean, "var": var,
            "stddev": math.sqrt(var) if var is not None else None, "min": mn, "max": mx}


def field_sum(cell: list, field: str) -> int:
    return cell[2 + 5 * FIELDS.index(field)]


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def valid_cell(c) -> bool:
    if not isinstance(c, list) or len(c) != CELL_LEN or not _is_int(c[0]) or c[0] < 0:
        return False
    for i in range(len(FIELDS)):
        n, s, q, mn, mx = c[1 + 5 * i:6 + 5 * i]
        if not all(_is_int(x) and x >= 0 for x in (n, s, q)) or n > c[0]:
            return False
        if n == 0:
            if mn is not None or mx is not None or s or q:
                return False
        elif not (_is_int(mn) and _is_int(mx) and 0 <= mn <= mx):
            return False
    return True


# ------------------------------------------------------------------------------- keys


def key_of(row: dict) -> str:
    agent_class = "main" if row.get("agent_id") is None else "sub"
    return json.dumps([row["harness"], row["model"], agent_class], separators=(",", ":"))


def parse_key(key: str) -> dict | None:
    try:
        harness, model, agent_class = json.loads(key)
    except (ValueError, TypeError):
        return None
    if harness not in L.HARNESSES or agent_class not in AGENT_CLASSES:
        return None
    if not isinstance(model, str) or not model:
        return None
    return {"harness": harness, "model": model, "agent_class": agent_class}


# ------------------------------------------------------------------------------- time


def _epoch(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _date(t: int) -> date:
    return datetime.fromtimestamp(t, timezone.utc).date()


def _iso(t: int) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hour_start(t) -> int:
    return int(t // HOUR) * HOUR


def h6_start(t: int) -> int:
    return QUOTA_WEEK_ANCHOR + ((t - QUOTA_WEEK_ANCHOR) // H6) * H6


def day_start(t: int) -> int:
    return (t // DAY) * DAY


def week_start(t: int) -> int:
    return QUOTA_WEEK_ANCHOR + ((t - QUOTA_WEEK_ANCHOR) // WEEK) * WEEK


def month_bounds(t: int) -> tuple[int, int]:
    d = _date(t)
    start = date(d.year, d.month, 1)
    end = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return _epoch(start), _epoch(end)


def quarter_bounds(t: int) -> tuple[int, int]:
    d = _date(t)
    m = ((d.month - 1) // 3) * 3 + 1
    start = date(d.year, m, 1)
    end = date(d.year + (m == 10), (m + 2) % 12 + 1, 1)
    return _epoch(start), _epoch(end)


def year_bounds(t: int) -> tuple[int, int]:
    d = _date(t)
    return _epoch(date(d.year, 1, 1)), _epoch(date(d.year + 1, 1, 1))


def w_span(t: int) -> tuple[int, int]:
    """The w cell holding instant t: its quota week, split at a calendar-quarter boundary."""
    ws = week_start(t)
    qs, qe = quarter_bounds(t)
    return max(ws, qs), min(ws + WEEK, qe)


TIER_START = {"h1": hour_start, "h6": h6_start, "d1": day_start, "w": lambda t: w_span(t)[0]}


def period_bounds(period: str, t: int) -> tuple[int, int]:
    if period == "day":
        s = day_start(t)
        return s, s + DAY
    if period == "week":
        s = week_start(t)
        return s, s + WEEK
    if period == "month":
        return month_bounds(t)
    if period == "quarter":
        return quarter_bounds(t)
    if period == "year":
        return year_bounds(t)
    raise ValueError(f"unknown period {period!r}; choose from {', '.join(PERIODS)}")


# series tier, total tier per period
PERIOD_TIERS = {"day": ("h1", "d1"), "week": ("h6", "w"), "month": ("d1", "d1"),
                "quarter": ("w", "w"), "year": ("w", "w")}


def series_spans(tier: str, start: int, end: int) -> list[tuple[int, int]]:
    step = {"h1": HOUR, "h6": H6, "d1": DAY}.get(tier)
    if step is not None:
        return [(s, s + step) for s in range(start, end, step)]
    spans, t = [], start
    while t < end:
        s, e = w_span(t)
        spans.append((s, e))
        t = e
    return spans


# ------------------------------------------------------------------------------- doc


def new_doc(seat: str) -> dict:
    return {"schema": AGG_SCHEMA, "seat": seat, "updated_at": None, "fields": list(FIELDS),
            "stats": list(STATS), "quota_week_anchor": QUOTA_WEEK_ANCHOR, "days": {},
            "sealed": {t: {} for t in SEALED_TIERS}}


def _valid_cellmap(m) -> bool:
    if not isinstance(m, dict):
        return False
    for key, cells in m.items():
        if parse_key(key) is None or not isinstance(cells, dict):
            return False
        for start, c in cells.items():
            if not (isinstance(start, str) and start.isdigit()) or not valid_cell(c):
                return False
    return True


def validate_doc(doc, seat: str) -> None:
    """Raise ValueError unless doc is a v1 aggregate of `seat`."""
    if not isinstance(doc, dict) or doc.get("schema") != AGG_SCHEMA:
        raise ValueError("not a pantheon-fleet-usage-agg/v1 document")
    if doc.get("seat") != seat:
        raise ValueError(f"aggregate seat {doc.get('seat')!r} is not {seat!r}")
    if doc.get("fields") != list(FIELDS) or doc.get("stats") != list(STATS):
        raise ValueError("aggregate field/stat layout differs from this reader")
    if doc.get("quota_week_anchor") != QUOTA_WEEK_ANCHOR:
        raise ValueError("aggregate quota_week_anchor differs from this reader")
    days, sealed = doc.get("days"), doc.get("sealed")
    if not isinstance(days, dict) or not isinstance(sealed, dict):
        raise ValueError("aggregate days/sealed missing")
    if set(sealed) != set(SEALED_TIERS) or not all(_valid_cellmap(sealed[t]) for t in SEALED_TIERS):
        raise ValueError("aggregate sealed tiers malformed")
    for name, rec in days.items():
        if L._day_of_file(f"{name}.jsonl") is None or not isinstance(rec, dict):
            raise ValueError(f"bad day record {name!r}")
        if not isinstance(rec.get("sha256"), str) or not _valid_cellmap(rec.get("h1")):
            raise ValueError(f"bad day record {name!r}")
        if rec.get("sealed_at") is not None and not _is_int(rec.get("sealed_at")):
            raise ValueError(f"bad day record {name!r}")


def load_doc(seat_dir: Path, seat: str) -> dict:
    """The seat's aggregate, or a new empty one when absent. AggregateError when it exists but
    is not a valid v1 aggregate of this seat (it is never overwritten then)."""
    path = seat_dir / AGG_FILE
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return new_doc(seat)
    if not stat.S_ISREG(st.st_mode):
        raise AggregateError(f"{path} is not a regular file")
    try:
        doc = json.loads(path.read_bytes())
        validate_doc(doc, seat)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise AggregateError(f"{path}: {exc}") from exc
    return doc


def write_doc(seat_dir: Path, doc: dict, now: int) -> None:
    doc["updated_at"] = now
    data = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    fd, tmp = tempfile.mkstemp(dir=seat_dir, prefix=TMP_PREFIX, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, seat_dir / AGG_FILE)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        dfd = os.open(seat_dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


@contextlib.contextmanager
def seat_lock(seat_dir: Path):
    fd = os.open(seat_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


# ------------------------------------------------------------------------------- fold


def _read_day_file(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            return fh.read()
    finally:
        if fd >= 0:
            os.close(fd)


def day_h1(data: bytes, seat: str, day: date) -> tuple[dict, int, dict]:
    """The hourly cells of one day file's bytes: ({key: {start: cell}}, rows, skipped). Rows
    whose ts falls outside `day` are counted in skipped["off_day"], never folded."""
    rows, skipped = L.parse_day_bytes(data, seat)
    skipped["off_day"] = 0
    start = _epoch(day)
    h1: dict = {}
    folded = 0
    for row in rows:
        if not (start <= row["ts"] < start + DAY):
            skipped["off_day"] += 1
            continue
        cells = h1.setdefault(key_of(row), {})
        cell = cells.setdefault(str(hour_start(row["ts"])), empty_cell())
        cell_add_row(cell, row)
        folded += 1
    return h1, folded, skipped


def _day_files(seat_dir: Path):
    """(name, day) for each regular, well-named day file, sorted."""
    out = []
    for entry in sorted(os.listdir(seat_dir)):
        day = L._day_of_file(entry)
        if day is None:
            continue
        try:
            if not stat.S_ISREG(os.lstat(seat_dir / entry).st_mode):
                continue
        except OSError:
            continue
        out.append((entry, day))
    return out


def fold_into(doc: dict, seat_dir: Path, seat: str, today: date, now: int) -> dict:
    """Fold every complete day file into doc (in memory). Returns the report; report["shas"]
    maps each day file name read to its current sha256."""
    report = {"folded": [], "refolded": [], "unchanged": 0, "sealed_changed": [],
              "unreadable": [], "changed": False, "shas": {}}
    for name, day in _day_files(seat_dir):
        if day >= today:
            continue
        data = _read_day_file(seat_dir / name)
        if data is None:
            report["unreadable"].append(name)
            continue
        sha = hashlib.sha256(data).hexdigest()
        report["shas"][name] = sha
        key = day.isoformat()
        rec = doc["days"].get(key)
        if rec is not None and rec["sha256"] == sha:
            report["unchanged"] += 1
            continue
        if rec is not None and rec.get("sealed_at") is not None:
            report["sealed_changed"].append(name)
            continue
        h1, rows, skipped = day_h1(data, seat, day)
        doc["days"][key] = {"sha256": sha, "bytes": len(data), "rows": rows,
                            "skipped": skipped, "folded_at": now, "sealed_at": None, "h1": h1}
        report["refolded" if rec is not None else "folded"].append(name)
        report["changed"] = True
    return report


def seal_day(doc: dict, day_key: str, now: int) -> None:
    rec = doc["days"][day_key]
    if rec.get("sealed_at") is not None:
        return
    sealed = doc["sealed"]
    for key, hours in rec["h1"].items():
        for hs, cell in hours.items():
            t = int(hs)
            for tier in SEALED_TIERS:
                start = str(TIER_START[tier](t))
                target = sealed[tier].setdefault(key, {}).setdefault(start, empty_cell())
                merge_into(target, cell)
    rec["sealed_at"] = now


def trim(doc: dict, today: date) -> bool:
    """Drop hourly cells of sealed days older than H1_RETAIN_DAYS and sealed h6 cells older
    than H6_RETAIN_DAYS. True when anything was dropped."""
    changed = False
    h1_cut = today - timedelta(days=H1_RETAIN_DAYS)
    for name, rec in doc["days"].items():
        if rec.get("sealed_at") is not None and rec["h1"] and date.fromisoformat(name) < h1_cut:
            rec["h1"] = {}
            changed = True
    h6_cut = _epoch(today) - H6_RETAIN_DAYS * DAY
    h6 = doc["sealed"]["h6"]
    for key in list(h6):
        for start in [s for s in h6[key] if int(s) < h6_cut]:
            del h6[key][start]
            changed = True
        if not h6[key]:
            del h6[key]
    return changed


def _now_today(today: date | None, now) -> tuple[date, int]:
    now = int(time.time() if now is None else now)
    return (today or _date(now)), now


def fold(directory, seat: str, *, commit: bool = False, today: date | None = None,
         env=None, now=None) -> dict:
    """Fold the caller's OWN seat's complete day files into its aggregate. Dry run unless
    commit; writes only when something changed (so a repeat run is a byte-identical no-op)."""
    today, now = _now_today(today, now)
    seat_dir = L.checked_seat_dir(directory, seat, env)
    out = {"seat": seat, "commit": commit, "status": "ok"}
    if seat_dir is None:
        out.update(status="no-data", dir=str(Path(directory) / "turns" / seat))
        return out
    out["dir"] = str(seat_dir)
    with (seat_lock(seat_dir) if commit else contextlib.nullcontext()):
        doc = load_doc(seat_dir, seat)
        report = fold_into(doc, seat_dir, seat, today, now)
        if commit and report["changed"]:
            write_doc(seat_dir, doc, now)
    report.pop("shas")
    out.update(report)
    return out


def prune(directory, seat: str, older_than_days: int, *, commit: bool = False,
          today: date | None = None, env=None, now=None) -> dict:
    """Retention for the caller's OWN seat dir, after folding. A day past retention is
    deleted only when it is folded with its file's current sha; it is sealed (and the
    aggregate written) before the file is deleted. Dry run unless commit."""
    today, now = _now_today(today, now)
    seat_dir = L.checked_seat_dir(directory, seat, env)
    cutoff = today - timedelta(days=older_than_days)
    verb = "deleted" if commit else "would_delete"
    out = {"seat": seat, "cutoff": cutoff.isoformat(), "commit": commit, verb: [], "kept": 0,
           "ignored": [], "refused": [], "refused_reasons": {}, "sealed": [], "status": "ok"}
    if seat_dir is None:
        out.update(status="no-data", dir=str(Path(directory) / "turns" / seat), fold=None)
        return out
    out["dir"] = str(seat_dir)

    def refuse(name: str, reason: str) -> None:
        out["refused"].append(name)
        out["refused_reasons"][name] = reason

    with (seat_lock(seat_dir) if commit else contextlib.nullcontext()):
        doc = load_doc(seat_dir, seat)
        report = fold_into(doc, seat_dir, seat, today, now)
        shas = report.pop("shas")
        out["fold"] = report
        changed = report["changed"]
        to_delete = []
        for entry in sorted(os.listdir(seat_dir)):
            if L.is_reserved_name(entry):
                continue
            day = L._day_of_file(entry)
            try:
                est = os.lstat(seat_dir / entry)
            except OSError:
                continue
            if day is None or not stat.S_ISREG(est.st_mode):
                out["ignored"].append(entry)
                continue
            if day >= cutoff:
                out["kept"] += 1
                continue
            rec = doc["days"].get(day.isoformat())
            sha = shas.get(entry)
            if rec is None or sha is None:
                refuse(entry, "not folded")
                continue
            if rec["sha256"] != sha:
                refuse(entry, "changed after seal" if rec.get("sealed_at") is not None
                       else "changed since fold")
                continue
            if (rec.get("skipped") or {}).get("off_day", 0) > 0:
                # T-5962 review LOW-1: rows dated outside this file's day are never folded, so
                # deleting the file would lose them; keep it for an operator instead
                refuse(entry, "off-day rows")
                continue
            if rec.get("sealed_at") is None:
                seal_day(doc, day.isoformat(), now)
                out["sealed"].append(entry)
                changed = True
            to_delete.append((entry, sha))
        changed = trim(doc, today) or changed
        if not commit:
            out[verb] = [e for e, _ in to_delete]
            return out
        if changed:
            write_doc(seat_dir, doc, now)
        for entry, sha in to_delete:
            data = _read_day_file(seat_dir / entry)
            if data is None or hashlib.sha256(data).hexdigest() != sha:
                refuse(entry, "changed after seal")
                continue
            os.unlink(seat_dir / entry)
            out[verb].append(entry)
    return out


# ------------------------------------------------------------------------------- read side


def _add_cell(view: dict, tier: str, key: str, start: int, cell: list) -> None:
    target = view[tier].setdefault(key, {})
    if start in target:
        merge_into(target[start], cell)
    else:
        target[start] = list(cell)


def seat_view(directory, seat: str) -> dict:
    """Everything known about one seat, as {tier: {key: {start(int): cell}}} plus status.
    Never raises. Open days and complete or current raw days not (or no longer) matching a
    folded record are read from the raw file; sealed tiers come from the aggregate."""
    seat_dir = Path(directory) / "turns" / seat
    view = {t: {} for t in TIERS}
    info = {"seat": seat, "aggregate": "absent", "raw_days": 0, "sealed_changed": [],
            "skipped": {}}
    doc = new_doc(seat)
    try:
        doc = load_doc(seat_dir, seat)
        if (seat_dir / AGG_FILE).exists():
            info["aggregate"] = "ok"
    except AggregateError as exc:
        info["aggregate"] = f"invalid: {exc}"
    days = dict(doc["days"])
    raw_files = []
    with contextlib.suppress(OSError):
        raw_files = _day_files(seat_dir)
    for name, day in raw_files:
        key = day.isoformat()
        rec = days.get(key)
        data = _read_day_file(seat_dir / name)
        if data is None:
            continue
        sha = hashlib.sha256(data).hexdigest()
        if rec is not None and rec["sha256"] == sha:
            continue
        if rec is not None and rec.get("sealed_at") is not None:
            info["sealed_changed"].append(name)
            continue
        h1, _, skipped = day_h1(data, seat, day)
        for k, v in skipped.items():
            info["skipped"][k] = info["skipped"].get(k, 0) + v
        days[key] = {"sha256": sha, "sealed_at": None, "h1": h1}
        info["raw_days"] += 1
    for tier in SEALED_TIERS:
        for key, cells in doc["sealed"][tier].items():
            for start, cell in cells.items():
                _add_cell(view, tier, key, int(start), cell)
    for rec in days.values():
        open_day = rec.get("sealed_at") is None
        for key, hours in rec["h1"].items():
            for hs, cell in hours.items():
                t = int(hs)
                _add_cell(view, "h1", key, t, cell)
                if open_day:
                    for tier in SEALED_TIERS:
                        _add_cell(view, tier, key, TIER_START[tier](t), cell)
    return {"view": view, "info": info}


def fleet_views(directory) -> dict:
    turns = Path(directory) / "turns"
    out = {}
    try:
        names = sorted(p.name for p in turns.iterdir() if p.is_dir())
    except OSError:
        return out
    for seat in names:
        if L.SEAT_RE.match(seat):
            out[seat] = seat_view(directory, seat)
    return out


def _group_key(seat: str, k: dict, by: tuple) -> tuple:
    vals = {"seat": seat, **k}
    return tuple(vals[d] for d in by)


def _cell_money(k: dict, cell: list, override=None) -> tuple:
    """(units|None, usd|None, unpriced_reason|None) of one key's cell."""
    if k["harness"] != "claude":
        return None, None, "OpenAI pool: no price table in this repo (codex)"
    w = L.weights_for(k["model"], override)
    units = (w.input * field_sum(cell, "input_tokens") + w.output * field_sum(cell, "output_tokens")
             + w.cache_read * field_sum(cell, "cache_read_input_tokens")
             + w.cache_creation * field_sum(cell, "cache_creation_input_tokens"))
    price, reason = L.input_usd_per_mtok(k["model"])
    if price is None:
        return units, None, reason
    return units, units * price / 1e6, None


def _parse_at(text: str | None, now: int) -> int:
    if text is None:
        return now
    if "T" in text:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
                   .timestamp())
    return _epoch(date.fromisoformat(text))


def history(directory, period: str, *, at: str | None = None, last: int = 1,
            by=("seat",), seats=None, harness=None, series: bool = False,
            override=None, now=None, markers_file: Path | None = None) -> dict:
    """Period aggregates per group, for the `last` periods ending with the one holding `at`."""
    now = int(time.time() if now is None else now)
    if period not in PERIODS:
        raise ValueError(f"unknown period {period!r}; choose from {', '.join(PERIODS)}")
    for d in by:
        if d not in GROUP_DIMS:
            raise ValueError(f"unknown dimension {d!r}; choose from {', '.join(GROUP_DIMS)}")
    if not isinstance(last, int) or last < 1:
        raise ValueError("--last must be >= 1")
    t = _parse_at(at, now)
    bounds = []
    for _ in range(last):
        s, e = period_bounds(period, t)
        bounds.append((s, e))
        t = s - 1
    bounds.reverse()
    views = fleet_views(directory)
    series_tier, total_tier = PERIOD_TIERS[period]
    today = _date(now)
    horizon = {"h1": _epoch(today) - H1_RETAIN_DAYS * DAY,
               "h6": _epoch(today) - H6_RETAIN_DAYS * DAY}
    first_seen: dict = {}
    for seat, sv in views.items():
        for key, cells in sv["view"]["d1"].items():
            k = parse_key(key)
            if k is None or not cells:
                continue
            g = _group_key(seat, k, tuple(by))
            first_seen[g] = min(first_seen.get(g, 1 << 62), min(cells))
    periods = []
    for start, end in bounds:
        groups: dict = {}
        pool = {"claude_units": 0.0}
        for seat, sv in views.items():
            for key, cells in sv["view"][total_tier].items():
                k = parse_key(key)
                if k is None:
                    continue
                inside = [c for s, c in cells.items() if start <= s < end]
                if not inside:
                    continue
                cell = merge_cells(*inside)
                units, usd, reason = _cell_money(k, cell, override)
                if units is not None:
                    pool["claude_units"] += units
                if seats is not None and seat not in seats:
                    continue
                if harness is not None and k["harness"] != harness:
                    continue
                g = _group_key(seat, k, tuple(by))
                grp = groups.setdefault(g, {"cell": empty_cell(), "units": None, "usd": None,
                                            "unpriced": {}, "harnesses": set(), "members": []})
                merge_into(grp["cell"], cell)
                grp["harnesses"].add(k["harness"])
                grp["members"].append((seat, key))
                if units is not None:
                    grp["units"] = (grp["units"] or 0.0) + units
                if usd is not None:
                    grp["usd"] = (grp["usd"] or 0.0) + usd
                if reason is not None:
                    grp["unpriced"][k["model"]] = reason
        out_groups = []
        for g in sorted(groups):
            grp = groups[g]
            cell = grp["cell"]
            entry = {"key": dict(zip(by, g)), "turns": cell[0],
                     "harnesses": sorted(grp["harnesses"]),
                     "per_turn": {f: field_stats(cell, f) for f in FIELDS},
                     "totals": {f: field_sum(cell, f) for f in FIELDS},
                     "units": None if grp["units"] is None else round(grp["units"], 3),
                     "usd_est": None if grp["usd"] is None else round(grp["usd"], 6),
                     "usd_unpriced": dict(sorted(grp["unpriced"].items())),
                     "share_of_claude_pool": None, "share_note": None}
            if "codex" in grp["harnesses"] and "claude" in grp["harnesses"]:
                entry["share_note"] = "mixed harnesses: share covers the Claude part only"
            if grp["units"] is not None and pool["claude_units"] > 0:
                entry["share_of_claude_pool"] = round(grp["units"] / pool["claude_units"], 6)
            elif "claude" not in grp["harnesses"]:
                entry["share_note"] = "OpenAI pool: reported separately, never in the Claude share"
            if series:
                entry["series"] = _series(views, grp["members"], series_tier, start, end,
                                          horizon, first_seen.get(g), now)
            out_groups.append(entry)
        periods.append({"period": period, "start": start, "end": end, "start_iso": _iso(start),
                        "end_iso": _iso(end), "complete": end <= now, "groups": out_groups,
                        "pool": {"claude_units": round(pool["claude_units"], 3)}})
    lo, hi = bounds[0][0], bounds[-1][1]
    return {"schema": HISTORY_SCHEMA, "generated_at": now, "period": period, "by": list(by),
            "filters": {"seats": sorted(seats) if seats else None, "harness": harness},
            "periods": periods,
            "markers": [m for m in all_markers(views, markers_file) if lo <= m["ts"] < hi],
            "seats": {s: sv["info"] for s, sv in views.items()},
            "estimate_note": L.WEIGHTS_NOTE + "; usd_est = units x the model's input list price "
                             "(ESTIMATE, " + L.PRICE_SOURCE + ")",
            "weights_source": L.WEIGHTS_SOURCE}


def _series(views, members, tier, start, end, horizon, first, now) -> dict:
    """The period's buckets for one group, plus stats over full-length bucket totals (zero
    filled from the group's first seen day to now)."""
    if tier in horizon and start < horizon[tier]:
        return {"tier": tier, "buckets": None,
                "reason": f"{tier} resolution expired (kept {H1_RETAIN_DAYS if tier == 'h1' else H6_RETAIN_DAYS} days)"}
    spans = series_spans(tier, start, end)
    full_len = {"h1": HOUR, "h6": H6, "d1": DAY, "w": WEEK}[tier]
    buckets = []
    totals_for_stats: dict = {f: [] for f in FIELDS}
    for s, e in spans:
        cell = empty_cell()
        for seat, key in members:
            c = views[seat]["view"][tier].get(key, {}).get(s)
            if c is not None:
                merge_into(cell, c)
        buckets.append({"start": s, "end": e, "partial": (e - s) != full_len, "turns": cell[0],
                        "totals": {f: field_sum(cell, f) for f in FIELDS}})
        counted = (e - s) == full_len and e <= now and first is not None and e > first
        if counted:
            for f in FIELDS:
                totals_for_stats[f].append(field_sum(cell, f))
    stats = {}
    for f, vals in totals_for_stats.items():
        c = empty_cell()
        for v in vals:
            cell_add_row(c, {f: v})
        stats[f] = field_stats(c, f)
    return {"tier": tier, "buckets": buckets, "bucket_total_stats": stats,
            "buckets_counted": len(totals_for_stats[FIELDS[0]])}


# ------------------------------------------------------------------------------- markers


def load_markers(path: Path | None = None) -> list:
    """The committed change-marker log (fleet and seat scope). Raises ValueError if malformed."""
    path = path or MARKERS_FILE
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema") != MARKERS_SCHEMA:
        raise ValueError("markers file: bad schema")
    out = []
    for m in doc.get("markers", []):
        if not isinstance(m, dict):
            raise ValueError("markers file: a marker is not an object")
        for k in ("at", "scope", "kind", "label", "ref"):
            if not isinstance(m.get(k), str) or not m[k]:
                raise ValueError(f"markers file: marker missing {k!r}")
        if m["kind"] not in MARKER_KINDS:
            raise ValueError(f"markers file: unknown kind {m['kind']!r}")
        ts = _parse_at(m["at"], 0)
        out.append({**m, "ts": ts, "derived": False})
    return out


def derived_markers(views: dict) -> list:
    """Seat first-seen and per-seat model first-seen, read off the cells: to the hour while the
    hourly cells are kept, else to the day."""
    out = []
    for seat, sv in sorted(views.items()):
        by_tier: dict = {"h1": {}, "d1": {}}
        for tier, firsts_t in by_tier.items():
            for key, cells in sv["view"][tier].items():
                k = parse_key(key)
                if k is not None and cells:
                    firsts_t[k["model"]] = min(firsts_t.get(k["model"], 1 << 62), min(cells))
        firsts = dict(by_tier["d1"])
        for model, t in by_tier["h1"].items():
            # the hourly first is exact unless older days of that model were trimmed to d1
            if day_start(t) <= firsts.get(model, t):
                firsts[model] = t
        if not firsts:
            continue
        out.append({"ts": min(firsts.values()), "scope": seat, "kind": "seat",
                    "label": f"{seat} first seen", "ref": None, "derived": True})
        for model, ts in sorted(firsts.items()):
            out.append({"ts": ts, "scope": seat, "kind": "model",
                        "label": f"{model} first seen", "ref": None, "derived": True})
    return out


def all_markers(views: dict, markers_file: Path | None = None) -> list:
    try:
        committed = load_markers(markers_file)
    except (OSError, ValueError):
        committed = []
    out = committed + derived_markers(views)
    for m in out:
        m["at_iso"] = _iso(m["ts"])
    return sorted(out, key=lambda m: (m["ts"], m["scope"], m["label"]))
