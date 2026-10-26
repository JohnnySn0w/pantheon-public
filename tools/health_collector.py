#!/usr/bin/env python3
"""health_collector — one sanitized, enum-only fleet health aggregate (T-5395, ORG-068 E10).

WHY THIS EXISTS. Two uniform health emitters exist (ORG-068 SS92): the host broker's
`org.pantheon.Health1` on the system bus (tools/host_control/health_dbus.py) and each
persona's `org.pantheon.PersonaController1` on its session bus
(tools/persona_controller_health_dbus.py). Nothing aggregated them. This collector is the
ORG-080 registry component `health-collector`: it reads every emitter and publishes ONE
snapshot, so kino's Health page (T-2660) and every later consumer read one source instead of
scraping. Per entry it carries the ratified RFC-004 SS5.5 consumer tuple
(component_id, invariant_list_id, invariant_list_digest, current_status, reasons[], bus_class).

    <out>  (default /run/pantheon-health/fleet-health.json, mode 0644)
        schema "pantheon-fleet-health/1", schema_version, collected_at, collector{...}, entries[]

CONTRACTS (same shape as tools/health_export.py, T-4884)
  * This module is the POLL leg: one Properties.GetAll per emitter per run. The SUBSCRIBING
    leg (T-5409, ORG-080 SS5.5) lives in tools/health_watcher.py: a signal is only a trigger,
    and it calls run() here for a fresh full re-Get. Stateless across runs (E10.C3): nothing
    is read back from the previous output. Restart-fixable (E10.C2): just run it again.
  * `collector.mode` (T-5409, additive enum, schema_version unchanged) records what triggered
    THIS write: poll (timer) | signal (watcher, after a PropertiesChanged) | startup (watcher's
    initial Get). It never says the watcher is alive: `collected_at` is stamped only by a
    completed full collect, so a dead watcher cannot make the file look fresher.
  * Concurrent writers (timer oneshot + watcher) serialize on an flock of the OUT DIRECTORY
    (no lock file is created): a slow older collect can never overwrite a newer one. The lock
    is mutual exclusion, not remembered state. A run that cannot get it returns RC_BUSY.
  * Every bus read goes through ONE reader object (CollectorReader in production, a fake in
    tests): argv arrays only, bounded timeouts, a failed read is an honest unreachable /
    denied / no_service class and a null status, never an exception and never "ready".
  * A persona controller WITHOUT the pantheon-health/v1 leg (today, until T-5137 lands) is
    reported `health_leg: absent` with status `not_configured`. Status is never derived from
    viability. The T-5137 properties are bound here as string literals.
  * sanitize_entry() is the second, schema-level gate right before the write: enums, bounded
    idents, caps. No free-text value from any emitter (Reasons / Revisions values, timestamps,
    runtime strings) is ever copied out. ONE DELIBERATE EXCEPTION to T-5395's enum-only rule
    (ORG-106 L4a, justified via ORG-105): the seat_state.reason an operator typed, kept
    printable-only and capped at 200 chars. SeatStateFault is a string on D-Bus but only a
    boolean here.
    Reasons are exported as KEYS only (invariant ids / roster leg names).
  * Atomic write (tmp + fsync + rename) at mode 0644; the aggregate's own `collected_at` and
    `collector` block are the heartbeat (same rename). Exit 0 on partial reads; 2 only when
    the output location is unwritable.

NOT INSTALLED BY THIS STEP. See tools/systemd/pantheon-health-collector.{service,timer}.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import pwd
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.health_export import (  # noqa: E402
    SudoReader, _bounded_int, _ident, atomic_write_json, fleet_personas,
)

SCHEMA = "pantheon-fleet-health/1"
SCHEMA_VERSION = 1
DEFAULT_OUT = Path("/run/pantheon-health/fleet-health.json")
COLLECTOR_ID = "health-collector"
CONTRACT_REVISION = "pantheon-health/v1"
FILE_MODE = 0o644
RC_BUSY = 3                       # another collector holds the out-dir lock; its write is as fresh
MODES = frozenset({"poll", "signal", "startup"})

HEALTH1_INTERFACE = "org.pantheon.Health1"
PC_INTERFACE = "org.pantheon.PersonaController1"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"

# (component_id, bus name, object path, registry component)
HOST_SERVICES = (
    ("host-approval", "org.pantheon.HostApproval1", "/org/pantheon/HostApproval1/Health", "action-broker"),
    ("host-broker", "org.pantheon.HostBroker1", "/org/pantheon/HostBroker1/Health", "action-broker"),
)
PERSONA_REGISTRY_COMPONENT = "harness-manager"

STATUSES = frozenset({"ready", "degraded", "unhealthy", "not_configured", "suppressed"})
REACHABILITY = frozenset({"ok", "unreachable", "denied", "no_service"})
HEALTH_LEGS = frozenset({"present", "absent", "unreachable"})
BUS_CLASSES = frozenset({"system", "session"})
KINDS = frozenset({"persona_controller", "host_service"})
REGISTRY_COMPONENTS = frozenset({"harness-manager", "action-broker"})
VIABILITY = frozenset({"viable", "cooldown-hold", "all-held", "exhausted"})
MODAL_KINDS = frozenset({"session-limit", "weekly-limit", "opus-limit", "monthly-spend-limit"})
# ORG-106 L4a (T-5527): seat-state publication.
SEAT_STATES = frozenset({"active", "paused-intake", "deep-sleep"})
SEAT_STATE_SOURCES = frozenset({"seat-state", "maintenance", "oom-hold", "autonomy_off"})
SEAT_REASON_MAX = 200
_ISO_Z_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")  # used with fullmatch: `$` would pass a trailing newline

MAX_LIST = 16
_COMPONENT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_LEG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")      # roster leg / invariant id (owner-mismatch:x)
_INV_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_INV_DIGEST_RE = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,127}$")
_MAX_DURATION_MS = 3_600_000
_MAX_EPOCH = 2 ** 40
_BUSCTL_TIMEOUT_S = 6
_POOL = 8
_LOCK_POLL_S = 0.1

ENTRY_FIELDS: tuple[str, ...] = (
    "component_id", "kind", "registry_component", "persona", "account", "bus_class",
    "reachability", "health_leg", "contract_revision", "current_status",
    "invariant_list_id", "invariant_list_digest", "reasons", "subcomponents",
    "security_drift", "viability", "seat_state",
)
DOC_FIELDS = ("schema", "schema_version", "collected_at", "collector", "entries")


# --------------------------------------------------------------------- reader
def classify_stderr(text: str | None) -> str:
    """Map busctl's failure text to a fixed class. The text itself is never kept."""
    t = text or ""
    if "Access denied" in t or "AccessDenied" in t:
        return "denied"
    if "not provided by any" in t or "not activatable" in t or "ServiceUnknown" in t:
        return "no_service"
    return "unreachable"


class CollectorReader(SudoReader):
    """The ONE bus-read surface. Returns (reach_class, stdout|None); never raises.

    System bus: run directly (the collector's own uid must be in the broker's monitor_uids).
    Session bus: the account-switch prefix from SudoReader._argv (`sudo -n -u <acct> env
    DBUS_SESSION_BUS_ADDRESS=... busctl --user ...`), argv arrays only."""

    def __init__(self, timeout: int = _BUSCTL_TIMEOUT_S, *, system_address: str | None = None,
                 session_addresses: dict[str, str] | None = None):
        super().__init__(timeout=timeout)
        # Scratch-bus seams (T-5409 tests). Production passes neither: the real system bus and
        # /run/user/<uid>/bus are used exactly as before.
        self.system_address = system_address
        self.session_addresses = dict(session_addresses or {})

    def _exec(self, argv: list[str]) -> tuple[str, str | None]:
        try:
            out = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                                 timeout=self.timeout, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError, ValueError):
            return "unreachable", None
        if out.returncode != 0:
            return classify_stderr(out.stderr), None
        return "ok", out.stdout

    @staticmethod
    def _call(bus_flag: str, name: str, path: str, interface: str, address: str | None = None) -> list[str]:
        flag = [bus_flag] if address is None else [bus_flag, address]
        return ["busctl", *flag, "--json=short", "call", name, path,
                PROPERTIES_INTERFACE, "GetAll", "s", interface]

    def getall_system(self, bus_name: str, path: str, interface: str) -> tuple[str, str | None]:
        if self.system_address:      # scratch-bus seam (tests); production leaves this None
            return self._exec(self._call("--address", bus_name, path, interface, self.system_address))
        return self._exec(self._call("--system", bus_name, path, interface))

    def getall_session(self, account: str, persona: str, interface: str) -> tuple[str, str | None]:
        if account in self.session_addresses:     # scratch-bus seam (tests)
            argv = self._call("--address", f"org.pantheon.PersonaController.{persona}",
                              f"/org/pantheon/PersonaController/{persona}", interface,
                              self.session_addresses[account])
            return self._exec(self._argv(account, argv))
        try:
            uid = pwd.getpwnam(account).pw_uid
        except KeyError:
            return "unreachable", None
        env = {"DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
               "XDG_RUNTIME_DIR": f"/run/user/{uid}"}
        argv = self._call("--user", f"org.pantheon.PersonaController.{persona}",
                          f"/org/pantheon/PersonaController/{persona}", interface)
        return self._exec(self._argv(account, argv, env))


# --------------------------------------------------------------------- parsing
def parse_getall(text: object) -> dict[str, tuple[str, object]] | None:
    """busctl --json=short GetAll reply -> {property: (signature, data)}, or None.

    Cells that are not {"type": str, "data": ...} are dropped, never raised on."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    if not isinstance(doc, dict) or doc.get("type") != "a{sv}":
        return None
    data = doc.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        return None
    props: dict[str, tuple[str, object]] = {}
    for name, cell in data[0].items():
        if (isinstance(name, str) and isinstance(cell, dict) and isinstance(cell.get("type"), str)
                and "data" in cell):
            props[name] = (cell["type"], cell["data"])
    return props


def _typed(props: dict, name: str, sig: str):
    cell = props.get(name)
    return cell[1] if cell is not None and cell[0] == sig else None


def _enum_str(v: object, allowed: frozenset) -> str | None:
    return v if isinstance(v, str) and v in allowed else None


def _reasons(m: object) -> list[str]:
    """Keys (invariant ids / roster legs) whose value is a non-empty string. Values are dropped."""
    if not isinstance(m, dict):
        return []
    return sorted(k for k, v in m.items()
                  if isinstance(k, str) and _LEG_RE.match(k) and isinstance(v, str) and v)[:MAX_LIST]


def _subcomponents(m: object) -> dict[str, str]:
    if not isinstance(m, dict):
        return {}
    ok = {k: v for k, v in m.items()
          if isinstance(k, str) and _LEG_RE.match(k) and isinstance(v, str) and v in STATUSES}
    return {k: ok[k] for k in sorted(ok)[:MAX_LIST]}


def _split_ref(ref: object) -> tuple[str | None, str | None]:
    if isinstance(ref, str) and ref.count("@") == 1:
        i, d = ref.split("@")
        if _INV_ID_RE.match(i) and _INV_DIGEST_RE.match(d):
            return i, d
    return None, None


def _viability(props: dict) -> dict:
    runtime = _typed(props, "ActiveRuntime", "s")
    modal = _typed(props, "ModalPresent", "b")
    return {
        "viability_status": _enum_str(_typed(props, "ViabilityStatus", "s"), VIABILITY),
        "modal_present": modal if isinstance(modal, bool) else None,
        "modal_kind": _enum_str(_typed(props, "ModalKind", "s"), MODAL_KINDS),
        "active_runtime_present": bool(runtime) if isinstance(runtime, str) else None,
    }


def _seat_reason(v: object) -> str:
    """ORG-106 decision 6 / ORG-105: the ONE free-text field this aggregate carries (kino shows
    the pause reason). Printable characters only, control characters stripped, capped."""
    if not isinstance(v, str):
        return ""
    return "".join(c for c in v if c.isprintable())[:SEAT_REASON_MAX]


def _seat_state(props: dict) -> dict | None:
    """The sanitized seat-state object, or None when the publisher predates the properties
    (or published an unusable SeatState): None is "unknown", consumers treat it as active."""
    state = _enum_str(_typed(props, "SeatState", "s"), SEAT_STATES)
    if state is None:
        return None
    sources = _typed(props, "SeatStateSources", "as")
    expires = _typed(props, "SeatStateExpires", "s")
    fault = _typed(props, "SeatStateFault", "s")
    return {
        "state": state,
        "sources": (sorted({x for x in sources if x in SEAT_STATE_SOURCES})
                    if isinstance(sources, list) else []),
        "owner": _ident(_typed(props, "SeatStateOwner", "s")),
        "reason": _seat_reason(_typed(props, "SeatStateReason", "s")),
        "expires": expires if isinstance(expires, str) and _ISO_Z_RE.fullmatch(expires) else None,
        "fault": bool(fault) if isinstance(fault, str) else False,
    }


# ------------------------------------------------------------------ collection
def _entry(component_id, kind, registry, persona, account, bus_class, reach, props) -> dict:
    """Raw (already enum-mapped) entry. `props` is None unless the read succeeded."""
    ent = {
        "component_id": component_id, "kind": kind, "registry_component": registry,
        "persona": persona, "account": account, "bus_class": bus_class,
        "reachability": reach, "health_leg": "unreachable", "contract_revision": None,
        "current_status": None, "invariant_list_id": None, "invariant_list_digest": None,
        "reasons": [], "subcomponents": {}, "security_drift": None, "viability": None,
        "seat_state": None,
    }
    if props is None:
        return ent
    rev = _typed(props, "ContractRevision", "s")
    if "Status" in props:
        ent["health_leg"] = "present"
        if rev == CONTRACT_REVISION:
            ent["contract_revision"] = rev
            ent["current_status"] = _enum_str(_typed(props, "Status", "s"), STATUSES)
    else:
        ent["health_leg"] = "absent"
        if kind == "persona_controller":
            ent["current_status"] = "not_configured"   # leg absent; never inferred from viability
    ent["invariant_list_id"], ent["invariant_list_digest"] = _split_ref(_typed(props, "InvariantListRef", "s"))
    ent["reasons"] = _reasons(_typed(props, "Reasons", "a{ss}"))
    ent["subcomponents"] = _subcomponents(_typed(props, "Subcomponents", "a{ss}"))
    drift = _typed(props, "SecurityDrift", "b")
    ent["security_drift"] = drift if isinstance(drift, bool) else None
    if kind == "persona_controller":
        ent["viability"] = _viability(props)
        ent["seat_state"] = _seat_state(props)
    return ent


def _read_one(fn, *args) -> tuple[str, dict | None]:
    try:
        reach, text = fn(*args)
    except Exception:  # noqa: BLE001 - a read error is unreachable, never a crash
        return "unreachable", None
    if reach != "ok":
        return (reach if reach in REACHABILITY else "unreachable"), None
    props = parse_getall(text)
    return ("ok", props) if props is not None else ("unreachable", None)


def _host_entry(reader, spec) -> dict:
    cid, bus_name, path, registry = spec
    reach, props = _read_one(reader.getall_system, bus_name, path, HEALTH1_INTERFACE)
    return _entry(cid, "host_service", registry, None, None, "system", reach, props)


def _persona_entry(reader, persona: str, account: str) -> dict:
    reach, props = _read_one(reader.getall_session, account, persona, PC_INTERFACE)
    return _entry(f"persona-controller.{persona}", "persona_controller", PERSONA_REGISTRY_COMPONENT,
                  persona, account, "session", reach, props)


def collect(personas, *, reader, now: float, mode: str = "poll") -> dict:
    """Poll every emitter and return the sanitized aggregate document. Never raises."""
    t0 = time.monotonic()
    jobs = [lambda s=s: _host_entry(reader, s) for s in HOST_SERVICES]
    for persona, account, _pane in personas:
        if _ident(persona) is None or _ident(account) is None:
            continue
        jobs.append(lambda p=persona, a=account: _persona_entry(reader, p, a))
    with ThreadPoolExecutor(max_workers=_POOL) as pool:
        raw = list(pool.map(lambda j: j(), jobs))
    entries = [e for e in (sanitize_entry(r) for r in raw) if e is not None]
    entries.sort(key=lambda e: e["component_id"])
    return sanitize_doc({
        "collected_at": int(now),
        "collector": {"mode": mode, "entry_count": len(entries),
                      "read_ok_count": sum(1 for e in entries if e["reachability"] == "ok"),
                      "duration_ms": int((time.monotonic() - t0) * 1000)},
        "entries": entries,
    })


# ------------------------------------------------------------------ sanitizer
def _sanitize_seat_state(ss: object) -> dict | None:
    if not isinstance(ss, dict):
        return None
    state = _enum_str(ss.get("state"), SEAT_STATES)
    if state is None:
        return None
    sources, expires = ss.get("sources"), ss.get("expires")
    return {
        "state": state,
        "sources": (sorted({x for x in sources if isinstance(x, str) and x in SEAT_STATE_SOURCES})
                    if isinstance(sources, list) else []),
        "owner": _ident(ss.get("owner")),
        "reason": _seat_reason(ss.get("reason")),
        "expires": expires if isinstance(expires, str) and _ISO_Z_RE.fullmatch(expires) else None,
        "fault": ss["fault"] if isinstance(ss.get("fault"), bool) else False,
    }


def sanitize_entry(rec: object) -> dict | None:
    """Rebuild an entry with exactly ENTRY_FIELDS. None only if component_id is unusable."""
    if not isinstance(rec, dict):
        return None
    cid = _ident(rec.get("component_id"), _COMPONENT_RE)
    if cid is None:
        return None
    via = rec.get("viability")
    reasons = rec.get("reasons")
    drift = rec.get("security_drift")
    ss = rec.get("seat_state")
    inv_id = _ident(rec.get("invariant_list_id"), _INV_ID_RE)
    digest = rec.get("invariant_list_digest")
    digest = (digest if isinstance(digest, str) and len(digest) <= 128 and digest.isascii()
              and _INV_DIGEST_RE.match(digest) else None)
    if inv_id is None or digest is None:
        inv_id = digest = None
    return {
        "component_id": cid,
        "kind": _enum_str(rec.get("kind"), KINDS),
        "registry_component": _enum_str(rec.get("registry_component"), REGISTRY_COMPONENTS),
        "persona": _ident(rec.get("persona")),
        "account": _ident(rec.get("account")),
        "bus_class": _enum_str(rec.get("bus_class"), BUS_CLASSES),
        "reachability": _enum_str(rec.get("reachability"), REACHABILITY) or "unreachable",
        "health_leg": _enum_str(rec.get("health_leg"), HEALTH_LEGS) or "unreachable",
        "contract_revision": CONTRACT_REVISION if rec.get("contract_revision") == CONTRACT_REVISION else None,
        "current_status": _enum_str(rec.get("current_status"), STATUSES),
        "invariant_list_id": inv_id,
        "invariant_list_digest": digest,
        "reasons": (sorted({r for r in reasons if _ident(r, _LEG_RE)})[:MAX_LIST]
                    if isinstance(reasons, list) else []),
        "subcomponents": _subcomponents(rec.get("subcomponents")),
        "security_drift": drift if isinstance(drift, bool) else None,
        "viability": None if not isinstance(via, dict) else {
            "viability_status": _enum_str(via.get("viability_status"), VIABILITY),
            "modal_present": via["modal_present"] if isinstance(via.get("modal_present"), bool) else None,
            "modal_kind": _enum_str(via.get("modal_kind"), MODAL_KINDS),
            "active_runtime_present": (via["active_runtime_present"]
                                      if isinstance(via.get("active_runtime_present"), bool) else None),
        },
        "seat_state": _sanitize_seat_state(ss),
    }


def sanitize_doc(doc: dict) -> dict:
    col = doc.get("collector") if isinstance(doc.get("collector"), dict) else {}
    entries = [e for e in (sanitize_entry(x) for x in doc.get("entries", [])) if e is not None]
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "collected_at": _bounded_int(doc.get("collected_at"), _MAX_EPOCH) or 0,
        "collector": {
            "component_id": COLLECTOR_ID,
            "mode": _enum_str(col.get("mode"), MODES) or "poll",
            "entry_count": len(entries),
            "read_ok_count": _bounded_int(col.get("read_ok_count"), 1_000_000) or 0,
            "duration_ms": _bounded_int(col.get("duration_ms"), _MAX_DURATION_MS) or 0,
        },
        "entries": entries,
    }


# ------------------------------------------------------------------------ run
class _OutDirLock:
    """flock on the out directory fd (no lock file: the dir must hold only the aggregate)."""

    def __init__(self, out_dir: Path, wait_s: float):
        self.out_dir, self.wait_s, self.fd = out_dir, wait_s, None

    def __enter__(self):
        fd = os.open(self.out_dir, os.O_RDONLY | os.O_DIRECTORY)
        deadline = time.monotonic() + self.wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.fd = fd
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return False
                time.sleep(_LOCK_POLL_S)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)       # closing the fd releases the flock
        return False


def run(personas, *, reader, out: Path, now: float | None = None, dry_run: bool = False,
        mode: str = "poll", lock_wait_s: float = 20.0) -> int:
    if dry_run:
        doc = collect(personas, reader=reader, now=time.time() if now is None else now, mode=mode)
        print(json.dumps(doc, indent=2, ensure_ascii=True))
        return 0
    out = Path(out)
    if not out.parent.is_dir() or not os.access(out.parent, os.W_OK | os.X_OK):
        print(f"health_collector: out dir not writable: {out.parent}", file=sys.stderr)
        return 2
    try:
        lock = _OutDirLock(out.parent, lock_wait_s)
        with lock as got:
            if not got:
                return RC_BUSY
            # collect INSIDE the lock so write order == collect order.
            doc = collect(personas, reader=reader, now=time.time() if now is None else now, mode=mode)
            atomic_write_json(out.parent, out.name, doc, mode=FILE_MODE)
    except OSError as exc:
        print(f"health_collector: write failed in {out.parent}: {exc.__class__.__name__}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--dry-run", action="store_true", help="print JSON to stdout, write nothing")
    args = ap.parse_args(argv)
    rc = run(fleet_personas(), reader=CollectorReader(), out=args.out, dry_run=args.dry_run)
    return 0 if rc == RC_BUSY else rc          # a peer collector's write is as fresh as ours


if __name__ == "__main__":
    sys.exit(main())
