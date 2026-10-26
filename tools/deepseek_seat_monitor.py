#!/usr/bin/env python3
"""deepseek_seat_monitor — Recovery's dry-run check of the DeepSeek seat (T-5125, T-4924 H3b MR-2).

WHY THIS EXISTS. The third system seat (unix account deepseek-fallback) has no
bot and no send path; it reports to Recovery STRUCTURALLY, through one enum-only
record, /run/deepseek-fallback-health/watcher.json (tools/deepseek_seat_watcher.py).
Plan notes/2026-10-12-t4924-deepseek-hermes-system-seat-plan.md §4.3/§5.1: with
Recovery healthy, Recovery's deterministic monitors are the seat's pager. This is that
monitor's check, plus the two signals only Recovery's side can compute:

  * SOURCE-PIN STALENESS (§2.3, D-40). The seat runs from a read-only sparse
    checkout pinned at one commit, and has neither origin/main nor egress to it.
    Recovery compares the pin's source objects (pinned-rules/<name>.md, the selected
    SOPs, the defaults file; a directory compares as its tree) with origin/main
    in Recovery's own clone and exports deepseek_seat_source_pin_behind. No fetch:
    origin/main is as fresh as Recovery's clone.
  * EGRESS-REFRESH STALENESS (§6). A root timer refreshes the seat's egress
    address set; its last-success stamp must stay fresh.

NOT DEFINED YET (inputs, unknown until wired). The pin file is written at H5a
alongside the sparse checkout and the refresh stamp by H5a's root timer; neither
location nor format exists yet. Both are therefore injected (--pin-file,
--egress-refresh-file) and read unknown when not given. Provisional formats,
root-owned by default, read with the hardened O_NOFOLLOW reader:
    pin:     {"schema_version": 1, "commit": "<full sha>", "paths": ["<rel>", ...]}
    refresh: {"schema_version": 1, "refreshed_at": <epoch int>}

PAGES (Page; fixed single-line templates; the rules are in check())
  RECORD_STALE / RECORD_UNKNOWN   the record is stale / missing, forged, wrong-owner,
                                  or the account is unresolvable. Field pages are
                                  then suppressed (every field reads unknown).
  BALANCE_LOW, BALANCE_EXHAUSTED, BURN_RATE_ALARM, CONFIG_DRIFT, EGRESS_OPEN,
  EGRESS_UNKNOWN                  from the record's enums. balance/drift unknown is
                                  reported, not paged: both read unknown until H7.
  SOURCE_PIN_BEHIND, EGRESS_REFRESH_STALE   Recovery-side.
  SOURCE_PIN_UNKNOWN, EGRESS_REFRESH_UNKNOWN  only when that input is CONFIGURED
                                  and unreadable; an unconfigured input never pages.

INERTNESS. Dry-run is the only mode: --live/--live-page exit 64 before anything
runs. Side effects: stdout lines, and `git rev-parse` in --repo when --pin-file
is given. Nothing is sent, written or paged. Exit 0 whether or not a page would
fire: the pages ARE the output (stdout would_page lines and the JSON `pages`),
and a non-zero exit would fire the timer unit's OnFailure on every tick (the
activity_monitor_drift_check convention). 64 refuses a live flag; a real crash
still exits non-zero.

UNTRUSTED INPUT. Every value printed is an enum member, an int, or one of this
module's fixed strings. The record is read only through load_seat_health; pin
paths and the commit are validated before they reach git's argv.

NAMING. The seat has no persona name (it self-names, plan §5); everything here
is keyed on the account.
"""
from __future__ import annotations

import argparse
import enum
import json
import os
import pwd
import re
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.deepseek_seat_watcher import (  # noqa: E402
    DEFAULT_OUT_DIR,
    ENUM_FIELDS,
    HEALTH_FUTURE_SKEW_S,
    SEAT_ACCOUNT,
    _read_owned,
    _unknown,
    load_seat_health,
)
from tools.health_export import _MAX_EPOCH, _bounded_int  # noqa: E402

DEFAULT_SEAT_HEALTH_DIR = DEFAULT_OUT_DIR
DEFAULT_REPO = Path(__file__).resolve().parent.parent
DEFAULT_REF = "origin/main"
SCHEMA_VERSION = 1

# ---- thresholds (module constants; tests override via monkeypatch) ----------
# Provisional until H5a fixes the refresh timer's cadence (TTL-driven, §6):
# several missed refreshes, not one late one.
EGRESS_REFRESH_STALE_S = 2 * 3600
MAX_PIN_BYTES = 16 * 1024
MAX_PIN_PATHS = 64
MAX_PATH_LEN = 256
MAX_REFRESH_BYTES = 1024
GIT_TIMEOUT_S = 10

PIN_STATES = ("current", "behind", "unknown")
REFRESH_STATES = ("fresh", "stale", "unknown")
# load_seat_health's reasons, plus this module's no_account. Anything else prints "unknown".
RECORD_REASONS = frozenset({"ok", "incidents_mismatch", "missing", "symlink", "unreadable",
                            "not_regular", "oversize", "owner", "invalid", "schema", "account",
                            "future", "stale", "error", "no_account"})
RECORD_FIELDS = tuple(ENUM_FIELDS)

_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
_PATH_RE = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./-]*")

RC_OK = 0
RC_LIVE_REFUSED = 64


class Page(enum.Enum):
    RECORD_UNKNOWN = "RECORD_UNKNOWN"
    RECORD_STALE = "RECORD_STALE"
    BALANCE_LOW = "BALANCE_LOW"
    BALANCE_EXHAUSTED = "BALANCE_EXHAUSTED"
    BURN_RATE_ALARM = "BURN_RATE_ALARM"
    CONFIG_DRIFT = "CONFIG_DRIFT"
    EGRESS_OPEN = "EGRESS_OPEN"
    EGRESS_UNKNOWN = "EGRESS_UNKNOWN"
    SOURCE_PIN_BEHIND = "SOURCE_PIN_BEHIND"
    SOURCE_PIN_UNKNOWN = "SOURCE_PIN_UNKNOWN"
    EGRESS_REFRESH_STALE = "EGRESS_REFRESH_STALE"
    EGRESS_REFRESH_UNKNOWN = "EGRESS_REFRESH_UNKNOWN"


# Single-line, fixed text; no substitution at all.
_TEMPLATES = {
    Page.RECORD_UNKNOWN: "the seat's health record is missing, unreadable, forged, wrong-owner, "
                         "or the account does not resolve.",
    Page.RECORD_STALE: "the seat's health record is older than its stale window; its watcher "
                       "timer may be down.",
    Page.BALANCE_LOW: "the prepaid DeepSeek balance is below low_water.",
    Page.BALANCE_EXHAUSTED: "the prepaid DeepSeek balance is at or below hard_floor; the seat "
                            "treats itself as capped.",
    Page.BURN_RATE_ALARM: "the DeepSeek balance fell faster than the 24h burn alarm.",
    Page.CONFIG_DRIFT: "a rendered seat file differs on disk, or ~/seat holds an unexpected entry.",
    Page.EGRESS_OPEN: "a connection the seat's egress policy must reject succeeded.",
    Page.EGRESS_UNKNOWN: "the seat's egress checks failed or the allowed API was unreachable.",
    Page.SOURCE_PIN_BEHIND: "a source of the seat's pinned checkout differs from origin/main; "
                            "Recovery owns the pin bump.",
    Page.SOURCE_PIN_UNKNOWN: "the seat's pin file or its objects could not be read or resolved.",
    Page.EGRESS_REFRESH_STALE: "the seat's egress address-set refresh is stale.",
    Page.EGRESS_REFRESH_UNKNOWN: "the seat's egress refresh stamp could not be read.",
}


def render_page(page: Page) -> str:
    return f"page={page.value}: {_TEMPLATES[page]}"


# ------------------------------------------------------------- seat record
def resolve_seat_uid() -> int | None:
    try:
        return pwd.getpwnam(SEAT_ACCOUNT).pw_uid
    except KeyError:
        return None


def read_record(health_dir: Path, now: int, uid) -> dict:
    """The record via load_seat_health (never raises). An unresolvable account,
    or an explicit None, is reason no_account: never an unchecked read."""
    if not isinstance(uid, int) or isinstance(uid, bool):
        return _unknown("no_account")
    return load_seat_health(health_dir, expected_uid=uid, now=now)


# --------------------------------------------------------- pin staleness
def _load_owned_json(path: Path, cap: int, owner_uid: int):
    try:
        data, _reason, uid = _read_owned(Path(path), cap, os.fstat)
    except OSError:
        return None
    if data is None or uid != owner_uid:
        return None
    try:
        return json.loads(data.decode("ascii"))
    except Exception:  # noqa: BLE001 - incl. RecursionError
        return None


def _valid_path(p) -> bool:
    if not isinstance(p, str) or not 0 < len(p) <= MAX_PATH_LEN or not _PATH_RE.fullmatch(p):
        return False
    parts = PurePosixPath(p).parts
    return not p.startswith("/") and all(x not in ("..", "") for x in parts)


def load_pin(path: Path, owner_uid: int) -> dict | None:
    """{commit, paths} from the provisional pin file, or None if anything is off."""
    doc = _load_owned_json(path, MAX_PIN_BYTES, owner_uid)
    if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION:
        return None
    commit, paths = doc.get("commit"), doc.get("paths")
    if not isinstance(commit, str) or not _SHA_RE.fullmatch(commit):
        return None
    if (not isinstance(paths, list) or not 0 < len(paths) <= MAX_PIN_PATHS
            or not all(_valid_path(p) for p in paths)):
        return None
    return {"commit": commit, "paths": list(paths)}


def default_git(repo: Path, args: list[str]) -> str | None:
    """`git -C <repo> <args>` -> stdout stripped, or None on any failure."""
    try:
        cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                            timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return cp.stdout.strip() if cp.returncode == 0 else None


def pin_behind(pin: dict | None, *, repo: Path, ref: str, git) -> str:
    """current|behind|unknown. Compares each pin path's object id at the pin
    commit with origin/main's (`rev-parse --verify --quiet <rev>:<path>`).
    behind: any differs, or a path is gone upstream. unknown: no pin, a commit
    or ref that does not resolve, a path absent AT the pin, or a git failure."""
    if pin is None or not isinstance(ref, str) or not _REF_RE.fullmatch(ref) or ".." in ref:
        return "unknown"

    def oid(rev: str) -> str | None:
        try:
            out = git(repo, ["rev-parse", "--verify", "--quiet", rev])
        except Exception:  # noqa: BLE001 - a broken git is unknown, never a crash
            return None
        return out if isinstance(out, str) and _SHA_RE.fullmatch(out) else None

    if oid(f"{pin['commit']}^{{commit}}") is None or oid(f"{ref}^{{commit}}") is None:
        return "unknown"
    behind = False
    for p in pin["paths"]:
        pinned = oid(f"{pin['commit']}:{p}")
        if pinned is None:
            return "unknown"
        if oid(f"{ref}:{p}") != pinned:
            behind = True
    return "behind" if behind else "current"


# ------------------------------------------------------ egress refresh
def egress_refresh(path: Path, now: int, owner_uid: int) -> tuple[str, int | None]:
    """(fresh|stale|unknown, age_s|None) from the provisional refresh stamp."""
    doc = _load_owned_json(path, MAX_REFRESH_BYTES, owner_uid)
    if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION:
        return "unknown", None
    at = _bounded_int(doc.get("refreshed_at"), _MAX_EPOCH)
    if at is None or at > now + HEALTH_FUTURE_SKEW_S:
        return "unknown", None
    age = max(0, now - at)
    return ("stale" if age > EGRESS_REFRESH_STALE_S else "fresh"), age


# ------------------------------------------------------------------ check
def check(*, seat_health_dir: Path | None = None, seat_uid="account", now: int | None = None,
          pin_file: Path | None = None, pin_owner_uid: int = 0, repo: Path | None = None,
          ref: str = DEFAULT_REF, git=None, egress_refresh_file: Path | None = None,
          egress_refresh_owner_uid: int = 0) -> dict:
    """One pure-ish evaluation -> the report dict (enums, ints, null, a page list)."""
    now = int(time.time()) if now is None else int(now)
    uid = resolve_seat_uid() if seat_uid == "account" else seat_uid
    rec = read_record(seat_health_dir or DEFAULT_SEAT_HEALTH_DIR, now, uid)
    record_ok = rec.get("record_state") == "ok"
    reason = rec.get("reason") if rec.get("reason") in RECORD_REASONS else "unknown"
    fields = {f: (rec.get(f) if record_ok and rec.get(f) in ENUM_FIELDS[f] else "unknown")
              for f in RECORD_FIELDS}

    pin_state = "unknown"
    if pin_file is not None:
        pin_state = pin_behind(load_pin(pin_file, pin_owner_uid), repo=repo or DEFAULT_REPO,
                               ref=ref, git=git or default_git)
    refresh_state, refresh_age = "unknown", None
    if egress_refresh_file is not None:
        refresh_state, refresh_age = egress_refresh(egress_refresh_file, now,
                                                    egress_refresh_owner_uid)

    pages: set[Page] = set()
    if not record_ok:
        pages.add(Page.RECORD_STALE if reason == "stale" else Page.RECORD_UNKNOWN)
    else:
        band = fields["balance_band"]
        if band == "low":
            pages.add(Page.BALANCE_LOW)
        elif band == "exhausted":
            pages.add(Page.BALANCE_EXHAUSTED)
        if fields["burn_rate"] == "alarm":
            pages.add(Page.BURN_RATE_ALARM)
        if fields["config_drift"] == "drifted":
            pages.add(Page.CONFIG_DRIFT)
        if fields["egress_policy"] == "open":
            pages.add(Page.EGRESS_OPEN)
        elif fields["egress_policy"] == "unknown":
            pages.add(Page.EGRESS_UNKNOWN)
    if pin_state == "behind":
        pages.add(Page.SOURCE_PIN_BEHIND)
    elif pin_file is not None and pin_state == "unknown":
        pages.add(Page.SOURCE_PIN_UNKNOWN)
    if refresh_state == "stale":
        pages.add(Page.EGRESS_REFRESH_STALE)
    elif egress_refresh_file is not None and refresh_state == "unknown":
        pages.add(Page.EGRESS_REFRESH_UNKNOWN)

    hb = rec.get("heartbeat") if record_ok else None
    m = rec.get("balance_minor_units") if record_ok else None
    return {"schema_version": SCHEMA_VERSION, "checked_at": now, "account": SEAT_ACCOUNT,
            "record_state": "ok" if record_ok else "unknown", "record_reason": reason,
            "record_age_s": (now - hb) if isinstance(hb, int) and not isinstance(hb, bool) else None,
            **fields,
            "balance_minor_units": m if isinstance(m, int) and not isinstance(m, bool) else None,
            "deepseek_seat_source_pin_behind": pin_state,
            "egress_refresh": refresh_state, "egress_refresh_age_s": refresh_age,
            "pages": [p.value for p in Page if p in pages]}


def run(*, out=None, **kw) -> int:
    """check() + stdout lines. Always RC_OK: pages are output, not a failure; nothing is sent."""
    out = sys.stdout if out is None else out
    rep = check(**kw)

    def log(line: str) -> None:
        print(f"deepseek_seat_monitor: {line}", file=out)

    log(f"mode=dry-run account={SEAT_ACCOUNT} record_state={rep['record_state']} "
        f"reason={rep['record_reason']} "
        + " ".join(f"{f}={rep[f]}" for f in RECORD_FIELDS)
        + f" deepseek_seat_source_pin_behind={rep['deepseek_seat_source_pin_behind']}"
          f" egress_refresh={rep['egress_refresh']} pages={len(rep['pages'])}")
    for value in rep["pages"]:
        log(f"would_page {render_page(Page(value))}")
    print(json.dumps(rep, ensure_ascii=True, separators=(",", ":")), file=out)
    return RC_OK


# ----------------------------------------------------------------------- CLI
LIVE_REFUSED = "refused: this monitor has no live mode (T-5125 is dry-run only; it sends nothing)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--seat-health-dir", type=Path, default=None,
                    help=f"the seat's health dir (default {DEFAULT_SEAT_HEALTH_DIR})")
    ap.add_argument("--pin-file", type=Path, default=None,
                    help="the seat's pin file (not defined until H5a; unset reads unknown)")
    ap.add_argument("--repo", type=Path, default=None,
                    help=f"Recovery's clone holding the ref (default {DEFAULT_REPO})")
    ap.add_argument("--ref", default=DEFAULT_REF)
    ap.add_argument("--egress-refresh-file", type=Path, default=None,
                    help="the egress refresh stamp (not defined until H5a; unset reads unknown)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="evaluate and print (default, only mode)")
    # Present only so an attempt is refused loudly (exit 64) instead of misparsed.
    mode.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--live-page", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.live or args.live_page:
        print(f"deepseek_seat_monitor: {LIVE_REFUSED}", file=sys.stderr)
        return RC_LIVE_REFUSED
    return run(seat_health_dir=args.seat_health_dir, pin_file=args.pin_file, repo=args.repo,
               ref=args.ref, egress_refresh_file=args.egress_refresh_file)


if __name__ == "__main__":
    sys.exit(main())
