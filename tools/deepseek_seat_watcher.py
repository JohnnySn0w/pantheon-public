#!/usr/bin/env python3
"""deepseek_seat_watcher — one deterministic watcher cycle for the DeepSeek seat (T-5125, T-4924 H3b).

WHY THIS EXISTS. The third system seat (unix account deepseek-fallback, slot
system-orchestrator-2) runs on a prepaid PRC-hosted model with no bot token and
no send path: it reports to Recovery STRUCTURALLY, never in free text (plan
notes/2026-10-12-t4924-deepseek-hermes-system-seat-plan.md §5, §5.1). This
plain-Python cycle is that structure. It runs as deepseek-fallback, observes
the seat's own balance, burn rate, rendered config and egress policy, and
writes one enum-only record, /run/deepseek-fallback-health/watcher.json (0640,
atomic), that Recovery's monitors (and Watchman's watcher, for page text only) read
with load_seat_health() below.

NAMING. The seat has no persona name yet (it self-names in its first session,
plan §5) and the earlier working name is withdrawn. Nothing here names a
persona: every path, field and key is the account, SEAT_ACCOUNT.

RECORD FIELDS (FIELDS; every value an enum member, an int or null)
  schema_version, heartbeat (epoch int), account
  balance_band        ok|low|exhausted|unknown   (quota.classify_balance over the
                                                  injected observer's QuotaSnapshot)
  balance_minor_units int|null                   (total * 100, floored: cents/fen)
  cap_state           unknown|ok|capped          (capped <=> balance_band exhausted)
  burn_rate           ok|alarm|unknown           (see burn_rate())
  config_drift        none|drifted|unknown       (see config_state())
  config_render       ok|failed|unknown
  egress_policy       enforced|open|unknown      (see evaluate_egress())
  incidents           sorted Incident values
  actions             Action values; ALWAYS empty in this MR

INERTNESS CONTRACT (T-4884 §8). The seat is receive-only and broker repairs are
a later phase, so this cycle decides no action at all: `actions` is always [].
Dry-run is the default and the only mode; --live/--live-page are refused with
exit 64 before anything runs. Side effects are exactly: stdout lines, the
--state-file (burn-rate history) and the record. The egress checks open (and
close) bare TCP/UDP sockets and send no payload but a fixed DNS root query to
the local resolver; --render-sample opens nothing and writes nothing.

NOT WIRED HERE (later phases). main() constructs no balance observer (the
thresholds are set at H7, and the key lives on the seat) and no renderer (H2's
hermes target), so balance_band/config_* read unknown from the CLI. run() takes
both injected; tests drive them with the real DeepSeekBalanceObserver over a
canned body and a fake renderer.

UNTRUSTED INPUT. Provider bodies are reduced to a band and an int by
provider_quota/quota before this module sees them; the renderer's output and the
seat's files are compared as bytes and never printed; incident lines come from
single-line fixed templates formatted with ints only. No string from any input
reaches the record, the state file or stdout.
"""
from __future__ import annotations

import argparse
import enum
import errno
import fcntl
import json
import os
import pwd
import socket
import stat
import sys
import time
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.harness_adapter.hermes_render import (  # noqa: E402
    HERMES_HOME_DIRNAME,
    IDENTITY_FILENAME,
    RUNTIME_CONFIG_FILENAME,
    SEAT_DIRNAME,
    STANDING_FILENAME,
)
from tools.harness_adapter.quota import classify_balance  # noqa: E402
from tools.health_export import _MAX_EPOCH, _bounded_int, atomic_write_json  # noqa: E402
from tools.recovery_watcher import CAP_STATES, _load_json, _read_small  # noqa: E402

SEAT_ACCOUNT = "deepseek-fallback"
DEFAULT_OUT_DIR = Path("/run/deepseek-fallback-health")
HEALTH_NAME = "watcher.json"
DEFAULT_STATE_FILE = Path.home() / ".local/state/pantheon/deepseek_seat_watcher.json"
SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1

# ---- thresholds (module constants; tests override via monkeypatch) ----------
BURN_WINDOW_S = 24 * 3600
BURN_MIN_SPAN_S = 3600             # an "ok" verdict needs at least this much history
DEFAULT_BURN_ALARM_MINOR_UNITS = 500   # plan §4.3 lean: $5 in 24h; the manifest overrides
MAX_HISTORY = 512                  # 24h at a 5-min poll is 288 samples
MAX_STATE_BYTES = 64 * 1024
MAX_HEALTH_BYTES = 4 * 1024        # a real record is ~330 bytes
MAX_RENDER_BYTES = 1024 * 1024
MAX_SEAT_ENTRIES = 256
POLL_INTERVAL_S = 5 * 60           # plan §4.3: the writer's timer cadence
# Three poll intervals: a record must survive the gap to the next write plus
# timer slack (AccuracySec) and the cycle's own probe time without flapping.
HEALTH_STALE_S = 3 * POLL_INTERVAL_S
HEALTH_FUTURE_SKEW_S = 60
MINOR_UNITS_BOUND = 2 ** 40        # |minor units| beyond this is not a balance
EGRESS_TIMEOUT_S = 3.0

BALANCE_BANDS = ("ok", "low", "exhausted", "unknown")
BURN_STATES = ("ok", "alarm", "unknown")
DRIFT_STATES = ("none", "drifted", "unknown")
RENDER_STATES = ("ok", "failed", "unknown")
EGRESS_STATES = ("enforced", "open", "unknown")
# CAP_STATES is recovery_watcher's (unknown|ok|capped): one vocabulary across seats.

# The rendered files, relative to the seat's home, as tools/harness_adapter/
# hermes_render.py installs them (install_path()): a renderer returning any other
# key set is a broken render, not drift.
EXPECTED_RENDER = frozenset({
    f"{HERMES_HOME_DIRNAME}/{RUNTIME_CONFIG_FILENAME}",
    f"{HERMES_HOME_DIRNAME}/{IDENTITY_FILENAME}",
    f"{SEAT_DIRNAME}/{STANDING_FILENAME}",
})


class Incident(enum.Enum):
    BALANCE_LOW = "BALANCE_LOW"
    BALANCE_EXHAUSTED = "BALANCE_EXHAUSTED"
    BALANCE_UNKNOWN = "BALANCE_UNKNOWN"
    BURN_RATE_ALARM = "BURN_RATE_ALARM"
    CONFIG_DRIFT = "CONFIG_DRIFT"
    CONFIG_RENDER_FAILED = "CONFIG_RENDER_FAILED"
    EGRESS_OPEN = "EGRESS_OPEN"
    EGRESS_UNKNOWN = "EGRESS_UNKNOWN"


class Action(enum.Enum):
    """Closed and, in this MR, never emitted: the seat is receive-only and its
    broker repairs (lease, kill-session, unstick) are a later phase."""
    NONE = "NONE"


# A record's `actions` may hold only these; empty until a later phase adds repairs.
EMITTABLE_ACTIONS = frozenset(a.value for a in Action if a is not Action.NONE)
INCIDENT_VALUES = frozenset(i.value for i in Incident)

FIELDS = ("schema_version", "heartbeat", "account", "balance_band", "balance_minor_units",
          "cap_state", "burn_rate", "config_drift", "config_render", "egress_policy",
          "incidents", "actions")
ENUM_FIELDS = {"balance_band": BALANCE_BANDS, "cap_state": CAP_STATES, "burn_rate": BURN_STATES,
               "config_drift": DRIFT_STATES, "config_render": RENDER_STATES,
               "egress_policy": EGRESS_STATES}


# -------------------------------------------------------------------- balance
def balance_from_snapshot(snapshot, thresholds) -> tuple[str, int | None]:
    """-> (balance_band, balance_minor_units) from a QuotaSnapshot.

    The band is re-derived with quota.classify_balance against `thresholds`
    (never read from the snapshot's string evidence), so a currency mismatch
    between the reading and the thresholds is unknown. No fresh balance reading
    -> ("unknown", None). Minor units are total * 100, floored."""
    reading = getattr(snapshot, "balance", None)
    if reading is None or getattr(snapshot, "freshness", None) != "fresh":
        return "unknown", None
    try:
        band = classify_balance(reading, thresholds)
    except Exception:  # noqa: BLE001 - a malformed reading is no evidence
        return "unknown", None
    if band not in BALANCE_BANDS or band == "unknown":
        return "unknown", None
    try:
        minor = int((reading.total * 100).to_integral_value(rounding=ROUND_FLOOR))
    except Exception:  # noqa: BLE001
        return band, None
    return band, minor if -MINOR_UNITS_BOUND <= minor <= MINOR_UNITS_BOUND else None


def read_balance(observer, thresholds) -> tuple[str, int | None, str | None]:
    """-> (band, minor_units, currency). A missing or raising observer is unknown."""
    if observer is None:
        return "unknown", None, None
    try:
        snapshot = observer.read()
    except Exception:  # noqa: BLE001 - provider failures are unknown, never a crash
        return "unknown", None, None
    band, minor = balance_from_snapshot(snapshot, thresholds)
    currency = getattr(getattr(snapshot, "balance", None), "currency", None)
    return band, minor, (currency if minor is not None else None)


def cap_state_for(band: str) -> str:
    if band == "exhausted":
        return "capped"
    return "ok" if band in ("ok", "low") else "unknown"


def burn_alarm_from_thresholds(thresholds) -> int | None:
    """The manifest's burn_alarm_24h (a currency amount) in minor units, or None."""
    amount = getattr(thresholds, "burn_alarm_24h", None)
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
        return None
    return int((amount * 100).to_integral_value(rounding=ROUND_FLOOR))


# ------------------------------------------------------------------ burn rate
def load_state(path: Path, now: int) -> dict:
    """The burn-rate history, hardened (recovery_watcher._load_json: regular,
    non-symlink, size-capped, ascii JSON). Anything malformed -- one bad entry,
    out-of-order or future timestamps, a wrong schema -- starts fresh: a forged
    or corrupt history may only cost an `unknown`, never a false ok/alarm."""
    fresh = {"currency": None, "history": []}
    doc = _load_json(path, MAX_STATE_BYTES)
    if not isinstance(doc, dict) or doc.get("schema_version") != STATE_SCHEMA_VERSION:
        return fresh
    currency, raw = doc.get("currency"), doc.get("history")
    if not (currency is None or (isinstance(currency, str) and currency.isascii()
                                 and 0 < len(currency) <= 8)):
        return fresh
    if not isinstance(raw, list) or len(raw) > MAX_HISTORY:
        return fresh
    history: list[tuple[int, int]] = []
    for item in raw:
        if not (isinstance(item, list) and len(item) == 2):
            return fresh
        ts = _bounded_int(item[0], _MAX_EPOCH)
        m = item[1]
        if (ts is None or ts > now + HEALTH_FUTURE_SKEW_S or isinstance(m, bool)
                or not isinstance(m, int) or abs(m) > MINOR_UNITS_BOUND):
            return fresh
        if history and (ts < history[-1][0] or m > history[-1][1]):
            return fresh   # we only ever write ascending time, non-increasing balance
        history.append((ts, m))
    return {"currency": currency, "history": history}


def burn_rate(state: dict, *, now: int, minor: int | None, currency: str | None,
              alarm_minor_units: int) -> tuple[str, dict]:
    """-> (burn_rate, new state). Pure.

    alarm  : the balance fell by MORE than alarm_minor_units from its highest
             sample in the last BURN_WINDOW_S (since the last top-up) to now.
             A drop seen is evidence, whatever the history span.
    ok     : no such drop, over at least BURN_MIN_SPAN_S of history.
    unknown: no balance this cycle (history kept untouched), or too little history.
    A rise is a top-up and resets the baseline; a currency change resets it too.
    """
    if minor is None or currency is None:
        return "unknown", state
    history = list(state.get("history") or []) if state.get("currency") == currency else []
    history = [(t, m) for t, m in history if now - BURN_WINDOW_S <= t <= now]
    if history and minor > history[-1][1]:
        history = []
    history.append((now, minor))
    history = history[-MAX_HISTORY:]
    new = {"currency": currency, "history": history}
    peak = max(m for _, m in history)
    if peak - minor > alarm_minor_units:
        return "alarm", new
    if len(history) < 2 or now - history[0][0] < BURN_MIN_SPAN_S:
        return "unknown", new
    return "ok", new


# --------------------------------------------------------------- config drift
def _valid_render(rendered) -> dict[str, bytes] | None:
    if not isinstance(rendered, dict) or set(rendered) != EXPECTED_RENDER:
        return None
    out = {}
    for rel, data in rendered.items():
        p = PurePosixPath(rel)
        if (p.is_absolute() or ".." in p.parts or not isinstance(data, bytes)
                or len(data) > MAX_RENDER_BYTES):
            return None
        out[rel] = data
    return out


def config_state(renderer, seat_home: Path | None) -> tuple[str, str]:
    """-> (config_render, config_drift).

    No renderer wired -> (unknown, unknown). A raising renderer, or one whose
    output is not exactly {EXPECTED_RENDER path: bytes}, -> (failed, unknown).
    Otherwise each rendered file must equal the seat's on-disk bytes (a regular,
    non-symlink file: _read_small), and ~/seat must hold nothing but the
    rendered seat files; anything else is drifted. No seat home, or a ~/seat
    listing that fails other than "absent", -> drift unknown. The seat's own
    harness home (~/.hermes) carries runtime state, so only its rendered files
    are compared, not its listing."""
    if renderer is None:
        return "unknown", "unknown"
    try:
        rendered = _valid_render(renderer())
    except Exception:  # noqa: BLE001 - a broken renderer is "failed", its text dropped
        rendered = None
    if rendered is None:
        return "failed", "unknown"
    if seat_home is None:
        return "ok", "unknown"
    drifted = False
    for rel, want in rendered.items():
        got = _read_small(Path(seat_home) / rel, MAX_RENDER_BYTES)
        if got != want:
            drifted = True
    allowed = {PurePosixPath(rel).name for rel in rendered
               if PurePosixPath(rel).parts[0] == SEAT_DIRNAME}
    try:
        with os.scandir(Path(seat_home) / SEAT_DIRNAME) as it:
            for n, entry in enumerate(it):
                if n >= MAX_SEAT_ENTRIES or entry.name not in allowed:
                    drifted = True
                    break
    except FileNotFoundError:
        drifted = True      # the rendered seat file is missing too
    except OSError:
        return "ok", "unknown"
    return "ok", "drifted" if drifted else "none"


# --------------------------------------------------------------------- egress
CHECK_BLOCKED_EXTERNAL = "blocked_external"
CHECK_RESOLVER = "resolver"
CHECK_LOOPBACK = "loopback"
CHECK_ALLOWED_API = "allowed_api"
# What each behavioral check must observe under the plan §6 policy.
EGRESS_EXPECT = {CHECK_BLOCKED_EXTERNAL: "rejected", CHECK_RESOLVER: "rejected",
                 CHECK_LOOPBACK: "rejected", CHECK_ALLOWED_API: "connected"}
# errnos a REJECT target (or an owner-match refusal) surfaces as on connect/send.
_REJECT_ERRNOS = frozenset({errno.ECONNREFUSED, errno.EPERM, errno.EACCES,
                            errno.ENETUNREACH, errno.EHOSTUNREACH})
# A minimal DNS query (id 0x5eed, RD, one question: the root, type NS, class IN).
_DNS_ROOT_QUERY = bytes.fromhex("5eed01000001000000000000" "00" "0002" "0001")


def _rejected(exc: OSError) -> bool:
    return not isinstance(exc, socket.timeout) and exc.errno in _REJECT_ERRNOS


class EgressProbe:
    """The real behavioral checks, run as the seat's UID (it cannot read firewall
    state, plan §6). Each returns "connected" or "rejected", and RAISES on a
    timeout, a name-resolution failure or any other error (evaluate_egress maps
    that to unknown). No payload is sent but the fixed DNS root query."""

    def __init__(self, *, timeout: float = EGRESS_TIMEOUT_S,
                 blocked_host: tuple[str, int] = ("1.1.1.1", 443),
                 resolver: tuple[str, int] = ("127.0.0.53", 53),
                 api_host: tuple[str, int] = ("api.deepseek.com", 443)):
        self.timeout = timeout
        self.blocked_host = blocked_host
        self.resolver_addr = resolver
        self.api_host = api_host

    def _tcp(self, addr) -> str:
        try:
            sock = socket.create_connection(addr, timeout=self.timeout)
        except OSError as exc:
            if _rejected(exc):
                return "rejected"
            raise
        sock.close()
        return "connected"

    def blocked_external(self) -> str:
        return self._tcp(self.blocked_host)     # an IP literal: no DNS needed

    def allowed_api(self) -> str:
        return self._tcp(self.api_host)         # resolves via the root hosts mapping

    def _udp_dns(self) -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(self.timeout)
            try:
                s.connect(self.resolver_addr)
                s.send(_DNS_ROOT_QUERY)
                s.recv(512)
            except OSError as exc:
                if _rejected(exc):
                    return "rejected"
                raise
            return "connected"
        finally:
            s.close()

    def resolver(self) -> str:
        """UDP and TCP to the host resolver; connected if EITHER answers. Both
        legs always run: a timeout (DROP) on one must not mask the other
        connecting. Raises only when neither connected and a leg errored."""
        results, errors = [], []
        for leg in (lambda: self._tcp(self.resolver_addr), self._udp_dns):
            try:
                results.append(leg())
            except OSError as exc:
                errors.append(exc)
        if "connected" in results:
            return "connected"
        if errors:
            raise errors[0]
        return "rejected"

    def loopback(self) -> str:
        """Connect to a listener this probe opens on 127.0.0.1 (no other local
        service is touched); the owner-match policy must reject the SYN."""
        lsn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            lsn.bind(("127.0.0.1", 0))
            lsn.listen(1)
            return self._tcp(lsn.getsockname())
        finally:
            lsn.close()

    def checks(self) -> dict:
        return {CHECK_BLOCKED_EXTERNAL: self.blocked_external, CHECK_RESOLVER: self.resolver,
                CHECK_LOOPBACK: self.loopback, CHECK_ALLOWED_API: self.allowed_api}


def make_egress_checks() -> dict:
    return EgressProbe().checks()


def evaluate_egress(checks) -> str:
    """enforced|open|unknown from the four checks (a {name: callable} map).

    open    : any check that must be rejected connected (outranks everything).
    unknown : a check is missing, raised (incl. timeout), returned anything but
              "connected"/"rejected", or the allowed API was rejected.
    enforced: every check observed exactly what EGRESS_EXPECT says."""
    if not isinstance(checks, dict):
        return "unknown"
    results = {}
    for name in EGRESS_EXPECT:
        fn = checks.get(name)
        try:
            r = fn() if callable(fn) else None
        except Exception:  # noqa: BLE001 - a probe failure is unknown, never a crash
            r = None
        results[name] = r if r in ("connected", "rejected") else None
    if any(results[n] == "connected" for n, want in EGRESS_EXPECT.items() if want == "rejected"):
        return "open"
    if all(results[n] == want for n, want in EGRESS_EXPECT.items()):
        return "enforced"
    return "unknown"


# ------------------------------------------------------------------ incidents
def incidents_for(rec: dict) -> list[str]:
    out = set()
    band = rec.get("balance_band")
    if band == "low":
        out.add(Incident.BALANCE_LOW)
    elif band == "exhausted":
        out.add(Incident.BALANCE_EXHAUSTED)
    elif band == "unknown":
        out.add(Incident.BALANCE_UNKNOWN)
    if rec.get("burn_rate") == "alarm":
        out.add(Incident.BURN_RATE_ALARM)
    if rec.get("config_drift") == "drifted":
        out.add(Incident.CONFIG_DRIFT)
    if rec.get("config_render") == "failed":
        out.add(Incident.CONFIG_RENDER_FAILED)
    if rec.get("egress_policy") == "open":
        out.add(Incident.EGRESS_OPEN)
    elif rec.get("egress_policy") == "unknown":
        out.add(Incident.EGRESS_UNKNOWN)
    return sorted(i.value for i in out)


# Single-line, fixed text; the only substitutions are ints (or "unknown").
_TEMPLATES = {
    Incident.BALANCE_LOW: "incident=BALANCE_LOW: the prepaid balance is below low_water "
                          "(balance_minor_units={minor}).",
    Incident.BALANCE_EXHAUSTED: "incident=BALANCE_EXHAUSTED: the prepaid balance is at or below "
                                "hard_floor or unavailable; the seat treats itself as capped "
                                "(balance_minor_units={minor}).",
    Incident.BALANCE_UNKNOWN: "incident=BALANCE_UNKNOWN: no usable balance reading this cycle "
                              "(no observer, read failure, no thresholds or currency mismatch).",
    Incident.BURN_RATE_ALARM: "incident=BURN_RATE_ALARM: the balance fell by more than "
                              "{alarm} minor units within 24h (balance_minor_units={minor}).",
    Incident.CONFIG_DRIFT: "incident=CONFIG_DRIFT: a rendered seat file differs on disk, is "
                           "missing, or ~/seat holds an unexpected entry.",
    Incident.CONFIG_RENDER_FAILED: "incident=CONFIG_RENDER_FAILED: the seat renderer raised or "
                                   "returned an unexpected file set.",
    Incident.EGRESS_OPEN: "incident=EGRESS_OPEN: a connection the egress policy must reject "
                          "succeeded.",
    Incident.EGRESS_UNKNOWN: "incident=EGRESS_UNKNOWN: an egress check failed, timed out, or "
                             "the allowed API endpoint was unreachable.",
}


def _int_or_unknown(v) -> str:
    return str(int(v)) if isinstance(v, int) and not isinstance(v, bool) else "unknown"


def render_incident(inc: Incident, rec: dict) -> str:
    return _TEMPLATES[inc].format(minor=_int_or_unknown(rec.get("balance_minor_units")),
                                  alarm=_int_or_unknown(rec.get("burn_alarm_minor_units")))


# --------------------------------------------------------------------- cycle
def default_seat_home() -> Path | None:
    try:
        return Path(pwd.getpwnam(SEAT_ACCOUNT).pw_dir)
    except KeyError:
        return None


def build_record(*, now: int, band: str, minor: int | None, burn: str, render: str,
                 drift: str, egress: str) -> dict:
    rec = {"schema_version": SCHEMA_VERSION, "heartbeat": now, "account": SEAT_ACCOUNT,
           "balance_band": band, "balance_minor_units": minor, "cap_state": cap_state_for(band),
           "burn_rate": burn, "config_drift": drift, "config_render": render,
           "egress_policy": egress}
    rec["incidents"] = incidents_for(rec)
    rec["actions"] = []          # inert: no action is ever decided in this MR
    return {k: rec[k] for k in FIELDS}


def run(*, out_dir: Path, state_file: Path, now: int | None = None, observer=None,
        thresholds="observer", renderer=None, seat_home: Path | None = None,
        egress_checks=None, burn_alarm_minor_units: int | None = None, out=None) -> int:
    """One dry-run cycle. `thresholds` defaults to the observer's own (the
    manifest's, at H7). The burn alarm is burn_alarm_minor_units, else the
    thresholds' burn_alarm_24h, else DEFAULT_BURN_ALARM_MINOR_UNITS.
    rc 0 ok (including a skipped cycle: another holds the lock, nothing is
    written); 2 the lock, the state or the record write failed."""
    out = sys.stdout if out is None else out
    fixed_now = None if now is None else int(now)
    if thresholds == "observer":
        thresholds = getattr(observer, "thresholds", None)
    alarm = burn_alarm_minor_units
    if alarm is None:
        alarm = burn_alarm_from_thresholds(thresholds)
    if alarm is None:
        alarm = DEFAULT_BURN_ALARM_MINOR_UNITS

    def log(line: str) -> None:
        print(f"deepseek_seat_watcher: {line}", file=out)

    # The slow observations (balance HTTP, egress probes up to ~4x
    # EGRESS_TIMEOUT_S) run first and write nothing, so the heartbeat is
    # stamped at write time, not at cycle start.
    band, minor, currency = read_balance(observer, thresholds)
    render, drift = config_state(renderer, seat_home)
    egress = evaluate_egress(egress_checks)

    # One cycle at a time from load_state to the last write: atomic_write_json
    # uses a FIXED .tmp name, and an overlapping cycle (a manual run during the
    # timer's) must neither rename another's half-written file into place nor
    # write a state built from history read before the other's write. A
    # contended cycle SKIPS (rc 0, so the timer unit's OnFailure stays quiet):
    # the lock holder's record is as fresh.
    lock_fd = None
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(state_file.parent / f".{state_file.name}.lock",
                          os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if lock_fd is not None:
            os.close(lock_fd)
        if isinstance(exc, BlockingIOError):
            log("skip cycle_lock_contended (another cycle is writing)")
            return 0
        log(f"error cycle_lock_failed {exc.__class__.__name__}")
        return 2

    rc = 0
    try:
        now = int(time.time()) if fixed_now is None else fixed_now
        state = load_state(state_file, now)
        burn, new_state = burn_rate(state, now=now, minor=minor, currency=currency,
                                    alarm_minor_units=alarm)
        rec = build_record(now=now, band=band, minor=minor, burn=burn, render=render,
                           drift=drift, egress=egress)
        log(f"mode=dry-run account={SEAT_ACCOUNT} balance_band={band} "
            f"cap_state={rec['cap_state']} burn_rate={burn} config_render={render} "
            f"config_drift={drift} egress_policy={egress} "
            f"incidents={len(rec['incidents'])} actions=0")
        for value in rec["incidents"]:
            log(render_incident(Incident(value), {**rec, "burn_alarm_minor_units": alarm}))
        if new_state is not state:
            try:
                atomic_write_json(state_file.parent, state_file.name,
                                  {"schema_version": STATE_SCHEMA_VERSION, "updated_at": now,
                                   "currency": new_state["currency"],
                                   "history": [list(e) for e in new_state["history"]]})
            except OSError as exc:
                log(f"error state_file_write_failed {exc.__class__.__name__}")
                rc = 2
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(out_dir, HEALTH_NAME, rec)
        except OSError as exc:
            log(f"error record_write_failed {exc.__class__.__name__}")
            rc = 2
    finally:
        os.close(lock_fd)    # releases the flock
    print(json.dumps(rec, ensure_ascii=True, separators=(",", ":")), file=out)
    return rc


# -------------------------------------------------------------------- reader
def _unknown(reason: str) -> dict:
    rec = {f: "unknown" for f in ENUM_FIELDS}
    rec.update({"schema_version": None, "heartbeat": None, "account": None,
                "balance_minor_units": None, "incidents": None, "actions": None})
    return {**{k: rec[k] for k in FIELDS}, "record_state": "unknown", "reason": reason}


def _enum_list(v, allowed: frozenset[str]) -> list[str] | None:
    if (not isinstance(v, list) or len(v) > len(allowed)
            or not all(isinstance(x, str) and x in allowed for x in v) or len(set(v)) != len(v)):
        return None
    return sorted(v)


def _read_owned(path: Path, cap: int, fstat) -> tuple[bytes | None, str, int | None]:
    """-> (data, reason, owner uid), all from ONE O_NOFOLLOW fd (no stat/open race).
    The same checks as recovery_watcher._read_small, with the reason and the
    owner kept (and fstat injectable, for the wrong-owner tests)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, "missing", None
    except OSError as exc:
        return None, "symlink" if exc.errno == errno.ELOOP else "unreadable", None
    try:
        st = fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "not_regular", None
        if st.st_size > cap:
            return None, "oversize", None
        data = os.read(fd, cap + 1)
        if len(data) > cap:
            return None, "oversize", None
        return data, "ok", st.st_uid
    finally:
        os.close(fd)


def load_seat_health(health_dir, *, expected_uid: int | None, now: int,
                     fstat=os.fstat) -> dict:
    """Recovery's and Watchman's hardened read of <health_dir>/watcher.json. NEVER raises.

    Returns exactly FIELDS plus record_state (ok|unknown) and reason. The WHOLE
    record is unknown (every enum "unknown", every int/list None) when the file
    is missing, a symlink, not regular, oversize, not ascii JSON, not schema
    SCHEMA_VERSION, for another account, owned by a uid != expected_uid, or its
    heartbeat is > HEALTH_FUTURE_SKEW_S in the future or > HEALTH_STALE_S old.
    Otherwise each field is checked alone: an out-of-enum value is "unknown", a
    bad int is None, a bad incidents/actions list is None. Fields not in FIELDS
    are dropped. incidents is re-derived from the enum fields (reason
    "incidents_mismatch" when the record's own list disagrees). expected_uid is
    the WRITER's uid (deepseek-fallback's), not root; it is REQUIRED, and only
    an explicit None skips the owner check."""
    try:
        data, reason, uid = _read_owned(Path(health_dir) / HEALTH_NAME, MAX_HEALTH_BYTES, fstat)
        if data is None:
            return _unknown(reason)
        if expected_uid is not None and uid != expected_uid:
            return _unknown("owner")
        try:
            doc = json.loads(data.decode("ascii"))
        except Exception:  # noqa: BLE001 - incl. RecursionError
            return _unknown("invalid")
        if not isinstance(doc, dict):
            return _unknown("invalid")
        sv = doc.get("schema_version")
        if isinstance(sv, bool) or sv != SCHEMA_VERSION:
            return _unknown("schema")
        hb = _bounded_int(doc.get("heartbeat"), _MAX_EPOCH)
        if hb is None:
            return _unknown("schema")
        if doc.get("account") != SEAT_ACCOUNT:
            return _unknown("account")
        if hb > now + HEALTH_FUTURE_SKEW_S:
            return _unknown("future")
        if now - hb > HEALTH_STALE_S:
            return _unknown("stale")
        out = {"schema_version": SCHEMA_VERSION, "heartbeat": hb, "account": SEAT_ACCOUNT}
        for f, allowed in ENUM_FIELDS.items():
            v = doc.get(f)
            out[f] = v if isinstance(v, str) and v in allowed else "unknown"
        m = doc.get("balance_minor_units")
        out["balance_minor_units"] = (m if isinstance(m, int) and not isinstance(m, bool)
                                      and abs(m) <= MINOR_UNITS_BOUND else None)
        # incidents is a pure function of the enum fields: re-derive it, so a
        # record claiming egress_policy=open with incidents=[] cannot read ok.
        claimed = _enum_list(doc.get("incidents"), INCIDENT_VALUES)
        out["incidents"] = incidents_for(out)
        out["actions"] = _enum_list(doc.get("actions"), EMITTABLE_ACTIONS)
        reason = "ok" if claimed == out["incidents"] else "incidents_mismatch"
        return {**{k: out[k] for k in FIELDS}, "record_state": "ok", "reason": reason}
    except Exception:  # noqa: BLE001 - the reader's contract is "never raises"
        return _unknown("error")


# ---------------------------------------------------------------- render-sample
def render_sample(now: int | None = None) -> list[str]:
    """Synthetic worlds covering every incident class, through the real
    build_record/render_incident. Reads and writes nothing; opens no socket."""
    now = int(time.time()) if now is None else int(now)
    worlds = [
        ("healthy", dict(band="ok", minor=5000, burn="ok", render="ok", drift="none",
                         egress="enforced")),
        ("low balance, burning", dict(band="low", minor=525, burn="alarm", render="ok",
                                      drift="none", egress="enforced")),
        ("exhausted, drifted, egress open", dict(band="exhausted", minor=80, burn="ok",
                                                 render="ok", drift="drifted", egress="open")),
        ("nothing observable", dict(band="unknown", minor=None, burn="unknown",
                                    render="failed", drift="unknown", egress="unknown")),
    ]
    lines = ["render-sample: SYNTHETIC records; nothing is read, written or sent"]
    for name, kw in worlds:
        rec = build_record(now=now, **kw)
        lines.append(f"--- world: {name}")
        lines.append(json.dumps(rec, ensure_ascii=True, separators=(",", ":")))
        lines += [render_incident(Incident(v), {**rec, "burn_alarm_minor_units":
                                                DEFAULT_BURN_ALARM_MINOR_UNITS})
                  for v in rec["incidents"]]
    return lines


# ----------------------------------------------------------------------- CLI
LIVE_REFUSED = "refused: this watcher has no live mode (T-5125 is dry-run only; repairs are a later phase)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    ap.add_argument("--burn-alarm-minor-units", type=int, default=None)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="one observe-only cycle (default). Inert for ACTIONS only: it "
                           "opens the egress probe sockets and writes the state file and "
                           "the record. Use --render-sample for a no-socket, no-write run")
    mode.add_argument("--render-sample", action="store_true",
                      help="print synthetic records for every incident class; writes nothing")
    # Present only so an attempt is refused loudly (exit 64) instead of misparsed.
    mode.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--live-page", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.burn_alarm_minor_units is not None and args.burn_alarm_minor_units < 0:
        ap.error("--burn-alarm-minor-units must be >= 0")
    if args.live or args.live_page:
        print(f"deepseek_seat_watcher: {LIVE_REFUSED}", file=sys.stderr)
        return 64
    if args.render_sample:
        print("\n".join(render_sample()))
        return 0
    return run(out_dir=args.out_dir, state_file=args.state_file,
               observer=None, renderer=None, seat_home=default_seat_home(),
               egress_checks=make_egress_checks(),
               burn_alarm_minor_units=args.burn_alarm_minor_units)


if __name__ == "__main__":
    sys.exit(main())
