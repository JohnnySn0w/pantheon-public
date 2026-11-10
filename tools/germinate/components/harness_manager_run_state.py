"""harness-manager configure ``run_state``: fleet bringdown/restore, shared pure data (T-5858, T-5091 U5).

UNTESTED-LIVE. Built dark: nothing here actuates. The operative surface stays
``sop/standard/fleet-daemon-bringdown-and-restore.md`` (byte-pinned, untouched; HO
ruling Q2: "the pause SOP stays as the operational surface (A-7), and restore converges
to the desired-state"). The first live fire is supervised. If any op here ever needs
root or another account it is reclassified (a host-substrate family, ORG-080 A-4),
never widened in place (Recovery D15 (T-5858)).

This leaf module is imported by BOTH the planner (harness-manager.py, to write each op's
postconditions into the op as data) and the simulated handler (harness-manager_handlers.py,
to evaluate them). It is pure: no I/O.

Contents:

* ``CATEGORIES``: the SOP §2 target-set table as data (one row per table row; parity-tested
  against the SOP), plus the two rows mason ruled in on top of it (F3: the consume timer,
  after the 2026-10-04 manifest; the codex receiver unit, from instance.yaml), and
  ``classify`` over a unit name;
* the exact-field CHECK table (SOP §4: "inactive" contains "active", so no check ever
  matches a state by substring; ActiveState / UnitFileState are systemd property values
  compared with ``==`` against closed enums). Missing or malformed facts never pass;
* ``INVENTORY``: every step of the SOP, by line, mapped to its op kind or to an explicit
  out-of-scope note (an exact bijection; the parity test reads it).
"""

from __future__ import annotations

import hashlib
import json
import re

#: Recovery D15 (T-5858) / U4 (i): the only systemd manager these ops address.
SYSTEMD_MANAGER = "user"
#: Recovery D15 (T-5858) / U4 (n): who is paged when a rollback could leave a seat dark.
ESCALATION_RECIPIENT = "recovery"
RUN_STATES = ("running", "paused")

#: systemd property values (``systemctl --user show -p ActiveState,UnitFileState``).
ACTIVE_STATES = ("active", "reloading", "inactive", "failed", "activating", "deactivating")
UNIT_FILE_STATES = (
    "enabled", "enabled-runtime", "linked", "linked-runtime", "alias", "masked", "masked-runtime",
    "static", "indirect", "disabled", "generated", "transient", "bad",
)
#: A pause / restore target must be exactly one of these two pre-states (the plan refuses
#: masked, static, linked and every mixed or transitional state: Recovery D15 (T-5858) (u)).
UP = {"UnitFileState": "enabled", "ActiveState": "active"}
DOWN = {"UnitFileState": "disabled", "ActiveState": "inactive"}
#: SOP 3.2: a oneshot in one of these is in flight; the drain waits, never stops it (Recovery (r)).
IN_FLIGHT = ("active", "activating", "deactivating", "reloading")

#: Exact unit identifiers (no globs, no spaces, no path separators).
UNIT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9@_.:-]{0,127}\.(service|timer)")
#: ``<principal>:<ref>``; the HO seat needs principal ``operator`` (Recovery D15 (T-5858) (o)).
ANCHOR_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9][A-Za-z0-9._#/-]{0,79}")
#: The run stamp doubles as the record timestamps (a pure planner reads no clock).
STAMP_RE = re.compile(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z")
MANIFEST_SCHEMA = "pantheon-run-state-manifest/v1"
#: T-5868 Phase B (Recovery D15 (T-5858) (r1)): a halted pause's paused-intake record is replaced, in
#: one write, by the ORG-106 halted record (notes s10); it is KEPT until the run's rollback completes.
STATE_HALTED = "halted"
#: T-5868 Phase B: the keys a halted record adds to the paused-intake one (notes s10).
HALTED_KEYS = ("run_id", "op_id", "halted_at", "rollback_cmd")


def halted_record(record: dict, *, run_id: str, op_id: str, halted_at: str) -> dict:
    """The ORG-106 halted record that replaces a U5 pause's paused-intake record (notes s10): the same
    record (pause_id, owner and reason carried) with state and phase "halted", indefinite, no expiry."""

    from tools import seat_state  # lazy: the L1 reader owns the canonical rollback line

    return dict(record, state=STATE_HALTED, phase=STATE_HALTED, indefinite=True, expires=None, run_id=run_id,
                op_id=op_id, halted_at=halted_at, rollback_cmd=seat_state.halted_rollback_cmd(run_id))


def unhalted(value):
    """A seat-state cell with the halted keys removed and the planned paused-intake state/phase
    restored, so the run's own halt does not read as drift (can_rollback) or a changed document."""

    if isinstance(value, dict) and value.get("state") == STATE_HALTED:
        return {**{k: v for k, v in value.items() if k not in HALTED_KEYS}, "state": "paused-intake", "phase": "set"}
    return value


#: T-5868 Q3: the record owner's page address. operator is never paged directly (orchestrator's ask).
OWNER_ALIASES = {"operator": "orchestrator"}


def owner_recipient(owner) -> str | None:
    """persona -> itself; ``role:X`` -> the persona holding role X (agent_role_map); operator -> orchestrator."""

    from tools import agent_role_map

    if not isinstance(owner, str) or not owner.strip():
        return None
    name = owner.strip()
    if name.startswith("role:"):
        name = name[len("role:"):]
    name = agent_role_map.resolve_persona(name)
    return OWNER_ALIASES.get(name, name)

#: The wake-timer stems taken from the installer's own plan (its seat timers). The
#: installer also plans pantheon-persona-quota (not a wake path: SOP §2 "Only wake paths
#: cost money") and pantheon-restart-drain (EXCLUDED, mason ruling F3: a resurrection
#: path, SOP §6); both stay running and restart-drain is reported as residue.
INSTALLER_WAKE_STEMS = ("pantheon-persona-consume", "pantheon-inbox-consumer", "pantheon-keep-going", "pantheon-mailbox-wake")
CONSUME_STEM = "pantheon-persona-consume"
RESTART_DRAIN_TIMER = "pantheon-restart-drain.timer"
#: SOP §2 dispatch loops a schedules/<persona>.yaml entry may declare.
DISPATCH_NAMES = ("pantheon-watch", "pantheon-knowledge-sweep", "pantheon-watch-digest")
#: Recovery D15 (T-5858) (s): the Recovery-side monitors that can resurrect a paused seat.
RECOVERY_MONITOR_STEMS = ("pantheon-context-fill-monitor", "pantheon-activity-monitor")

PAUSE, JUDGEMENT, NEVER = "pause", "judgement", "never"

#: (category, disposition, templated stems, exact basenames, basename prefixes, unit suffixes).
#: Rows 1-6 are SOP §2's table rows, in order; the ADDITIONS are mason ruling F3.
CATEGORIES = (
    ("wake timers", PAUSE,
     ("pantheon-keep-going", "pantheon-inbox-consumer", "pantheon-mailbox-wake", "pantheon-acp-bridge-backlog", CONSUME_STEM),
     (), (), ()),
    ("inbound bridges", PAUSE, ("pantheon-acp-bridge",), ("codex-telegram-poller", "specialist-a2a"), ("claude-telegram",), ()),
    ("dispatch loops", PAUSE, (), DISPATCH_NAMES, (), ()),
    ("seat apps", JUDGEMENT, (), ("exocortex",), (), ()),
    ("OS plumbing", NEVER, (), ("dbus", "gpg-agent", "launchpadlib-cache-clean"), (), ()),
    ("slices", NEVER, (), (), (), ("slice",)),
)
#: What the rows above add beyond the SOP's literal table text, and why (parity-tested).
ADDITIONS = {
    CONSUME_STEM: "mason ruling F3: the seat's main wake path; paused in the 2026-10-04 manifest "
                  "(knowledge/episodic/2026-10-12-seat-pause-mason-artisan/)",
    "codex-telegram-poller": "instance.yaml codex_telegram_bridge codex_receiver_unit: the codex inbound bridge",
}
#: Units the SOP's PROCEDURE names (not its §2 table) that a row above classifies, with the
#: SOP line naming them (parity-tested: the name is on that line). T-5862: specialist-a2a (the
#: A2A loopback server, tools/a2a/specialist-a2a.service) is one of the long-running services SOP
#: 3.3 stops, so it is an inbound bridge here, and specialist declares it in run_state_units.
PROCEDURE_NAMED = {
    "specialist-a2a": (58, "SOP 3.3 names specialist-a2a among the long-running services stopped after the drain"),
}


def parse_run_state_units(raw, *, persona: str) -> tuple[tuple[str, str | None], ...]:
    """T-5862: a role's instance.yaml ``run_state_units`` -> ((unit, oneshot-or-None), ...).

    The §2 pause-category units this seat runs that no other data source declares (the
    installer plan, companion_units, the bridge receivers, schedules/<persona>.yaml). Absent
    or null is empty. Each entry is a mapping with exactly one key, ``unit``. A timer's oneshot
    is always derived as ``<stem>.service``, never declared: a declared override could name any
    service (a live retired bridge, another seat's unit) and turn a refusal into a drain wait
    (T-5862 review F1). Type (timer/service) comes from the suffix and the category from
    ``classify``; neither is stored, so the data cannot disagree with them.
    Raises ValueError on anything else. Pure."""

    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{persona}: run_state_units must be a list")
    out, seen = [], set()
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) != {"unit"}:
            raise ValueError(f"{persona}: run_state_units entry {entry!r} must be a mapping of exactly one key, unit")
        unit = entry["unit"]
        if not isinstance(unit, str) or not UNIT_RE.fullmatch(unit):
            raise ValueError(f"{persona}: run_state_units unit {unit!r} is not an exact unit name")
        if unit in seen:
            raise ValueError(f"{persona}: run_state_units declares {unit} twice")
        seen.add(unit)
        if not is_pause_category(unit):
            raise ValueError(f"{persona}: run_state_units {unit} is not a §2 pause-category unit (classify: {classify(unit)})")
        instance = instance_of(unit)
        if instance is not None and instance != persona:
            raise ValueError(f"{persona}: run_state_units {unit} belongs to {instance!r}, not this seat")
        oneshot = unit[: -len(".timer")] + ".service" if unit.endswith(".timer") else None
        out.append((unit, oneshot))
    return tuple(out)


def classify(unit: str):
    """(category, disposition) of a unit name, or None (an unclassified unit is not touched)."""

    base, dot, suffix = unit.rpartition(".")
    if not dot:
        base, suffix = unit, ""
    for name, disposition, templated, exact, prefixes, suffixes in CATEGORIES:
        if suffix in suffixes:
            return name, disposition
        if "@" in base:
            if base.split("@", 1)[0] in templated:
                return name, disposition
            continue
        if base in exact or any(base == p or base.startswith(p + "-") for p in prefixes):
            return name, disposition
    return None


def is_pause_category(unit: str) -> bool:
    found = classify(unit)
    return found is not None and found[1] == PAUSE


def is_wake_timer(unit: str) -> bool:
    found = classify(unit)
    return found is not None and found[0] == "wake timers" and unit.endswith(".timer")


def instance_of(unit: str) -> str | None:
    base = unit.rpartition(".")[0]
    return base.split("@", 1)[1] if "@" in base else None


def stamp_ts(stamp: str) -> str:
    """``20261101T120000Z`` -> ``2026-11-01T12:00:00Z`` (ValueError if not that exact form)."""

    match = STAMP_RE.fullmatch(stamp or "")
    if not match:
        raise ValueError(f"stamp {stamp!r} is not YYYYMMDDTHHMMSSZ")
    y, mo, d, h, mi, s = match.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}Z"


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def manifest_problems(doc, *, persona: str, account: str) -> list[str]:
    """Why ``doc`` is not a usable pause manifest (empty = valid). Never raises. Recovery D15
    (T-5858) (q): restore refuses on any problem; it never infers."""

    if not isinstance(doc, dict):
        return [f"manifest is {type(doc).__name__}, not an object"]
    problems = []
    if doc.get("schema") != MANIFEST_SCHEMA:
        problems.append(f"schema {doc.get('schema')!r} != {MANIFEST_SCHEMA!r}")
    if doc.get("persona") != persona or doc.get("account") != account:
        problems.append(f"manifest is for {doc.get('persona')!r}/{doc.get('account')!r}, not {persona!r}/{account!r}")
    for key in ("pause_id", "plan_revision", "stamp", "anchor"):
        if not isinstance(doc.get(key), str) or not doc.get(key):
            problems.append(f"{key} missing")
    keep = doc.get("keep")
    if not (isinstance(keep, list) and all(isinstance(u, str) and UNIT_RE.fullmatch(u) for u in keep)):
        problems.append("keep must be a list of unit names")
    entries = doc.get("entries")
    if not isinstance(entries, list):
        return problems + ["entries must be a list"]
    seen = set()
    for entry in entries:
        if not (isinstance(entry, dict) and set(entry) == {"unit", "phase", "pre"}):
            problems.append(f"malformed entry {entry!r}")
            continue
        unit = entry["unit"]
        if not (isinstance(unit, str) and UNIT_RE.fullmatch(unit)) or unit in seen:
            problems.append(f"bad or duplicate unit {unit!r}")
        seen.add(unit)
        if entry["phase"] not in ("timer", "service") or entry["phase"] != ("timer" if str(unit).endswith(".timer") else "service"):
            problems.append(f"{unit}: phase {entry['phase']!r} does not match the unit")
        if entry["pre"] != UP:
            problems.append(f"{unit}: recorded pre-state {entry['pre']!r} is not enabled/active")
    return problems


# --- postconditions (exact fields; SOP §4) -------------------------------------------


def encode(checks) -> str:
    return canonical(list(checks))


def decode(raw: str):
    checks = json.loads(raw)
    if not isinstance(checks, list):
        raise ValueError("postconditions must be a list")
    for item in checks:
        if not (isinstance(item, dict) and item.get("check") in CHECKS and isinstance(item.get("params", {}), dict)):
            raise ValueError(f"malformed postcondition {item!r}")
    return checks


def check(check_id: str, advisory: bool = False, **params: str) -> dict:
    if check_id not in CHECKS:
        raise ValueError(f"unknown check {check_id!r}")
    out = {"check": check_id, "params": {k: str(v) for k, v in params.items()}}
    if advisory:
        out["advisory"] = True
    return out


def _units(view):
    units = view.get("units")
    return units if isinstance(units, dict) else None


def _csv(value: str) -> list[str]:
    return [v for v in (value or "").split(",") if v]


def _c_unit_fields(view, p):
    """SOP §4 RIGHT, never WRONG: each named systemd property is read as a field and
    compared with ``==`` (an ``in`` / substring test would let "inactive" pass "active")."""

    units = _units(view)
    if units is None:
        return False, "no unit observation: could not ask"
    unit = units.get(p["unit"])
    if not isinstance(unit, dict):
        return False, f"{p['unit']}: not in the seat's user manager"
    for field in ("UnitFileState", "ActiveState"):
        if field in p:
            value = unit.get(field)
            if not isinstance(value, str) or value != p[field]:
                return False, f"{p['unit']} {field}={value!r}, want {p[field]!r}"
    return True, f"{p['unit']} fields OK"


def _c_no_active_wake(view, p):
    """SOP §4 verify 1: zero active wake-path units (exact ActiveState), the keep-list aside."""

    units = _units(view)
    if units is None:
        return False, "no unit observation: could not ask"
    allowed = set(_csv(p.get("allow", "")))
    live = sorted(
        name for name, unit in units.items()
        if name not in allowed and is_pause_category(name) and isinstance(unit, dict) and unit.get("ActiveState") == "active"
    )
    return not live, "zero active wake units" if not live else f"active wake units: {live}"


def _c_no_new_failed(view, p):
    """SOP §4 verify 3: no failed unit beyond those already failed at plan time (exact set)."""

    units = _units(view)
    if units is None:
        return False, "no unit observation: could not ask"
    baseline = set(_csv(p.get("baseline", "")))
    failed = sorted(n for n, u in units.items() if isinstance(u, dict) and u.get("ActiveState") == "failed")
    new = [n for n in failed if n not in baseline]
    return not new, "no new failed unit" if not new else f"new failed units: {new}"


def _c_next_elapse(view, p):
    """Recovery D15 (T-5858) (t): the consume timer has a scheduled next fire."""

    units = _units(view)
    unit = (units or {}).get(p["unit"])
    value = unit.get("NextElapseUSecRealtime") if isinstance(unit, dict) else None
    ok = isinstance(value, str) and value not in ("", "n/a", "0")
    return ok, f"{p['unit']} next elapse {value!r}"


def _c_document(view, p):
    """A record cell (``manifest`` / ``seat_state``) holds exactly the planned document."""

    value = view.get(p["cell"])
    if p["cell"] == "seat_state":
        value = unhalted(value)  # T-5868: the run's own halt (replace by on_halted) is not a changed document
    if p["sha256"] == "absent":
        return value is None, f"{p['cell']} absent={value is None}"
    ok = value is not None and digest(value) == p["sha256"]
    return ok, f"{p['cell']} {'matches' if ok else 'does not match'} sha256 {p['sha256'][:12]}"


def _c_run_state(view, p):
    value = view.get("run_state")
    return value == p["value"], f"run_state {value!r}, want {p['value']!r}"


def _c_residue(view, p):
    """Always reported (planner marks it advisory): SOP 3.6 "Surface the residue to the operator
    as their call", and Recovery D15 (T-5858) (s) 'paused-with-residue'."""

    return False, f"{p['outcome']}: {p['residue']}"


CHECKS = {
    "unit-fields": _c_unit_fields,
    "no-active-wake": _c_no_active_wake,
    "no-new-failed": _c_no_new_failed,
    "next-elapse": _c_next_elapse,
    "document": _c_document,
    "run-state": _c_run_state,
    "residue": _c_residue,
}


def evaluate(checks, view) -> list[dict]:
    """Every check over ``view``; a check that raises is a failure (never a pass)."""

    out = []
    for item in checks:
        try:
            ok, detail = CHECKS[item["check"]](view, item.get("params", {}))
        except Exception as exc:  # noqa: BLE001 -- a broken check is a failure, never a pass
            ok, detail = False, f"check raised {type(exc).__name__}: {exc}"
        out.append({"check": item["check"], "ok": bool(ok), "advisory": bool(item.get("advisory")), "detail": detail})
    return out


def verdict(results) -> tuple[bool, str]:
    failed = [r for r in results if not r["ok"] and not r["advisory"]]
    if failed:
        return False, "; ".join(f"{r['check']}: {r['detail']}" for r in failed)
    warns = [r for r in results if not r["ok"]]
    return True, "postconditions OK" + (f" (advisory: {'; '.join(r['detail'] for r in warns)})" if warns else "")


# --- the SOP inventory (parity-tested against the SOP) -----------------------------------

SOP_NAME = "fleet-daemon-bringdown-and-restore.md"
#: Which SOP lines are STEPS: every command, every numbered procedure step, every table
#: row with a disposition, every verify item, every §6 variation and every §7 duty.
STEP_RE = re.compile(
    r"^(systemctl --user |OUT=|\*\*3\.\d — |[123]\. |- \*\*|Then re-verify |Ledger the pause|\*\*Post-arc"
    r"|\*\*Mail is not a daemon|Only wake paths cost money|Verify \*\*three\*\*)"
    r"|^\| \S.*\| \*\*(yes|judgement|never)\b"
)


def step_lines(text: str) -> list[int]:
    """The exact step lines of the SOP (pure: the caller reads the file)."""

    return [n for n, line in enumerate(text.splitlines(), 1) if STEP_RE.search(line.strip())]


#: SOP §2 table row line -> its CATEGORIES row name (parity-tested both ways).
SOP_TABLE_ROWS = {31: "wake timers", 32: "inbound bridges", 33: "dispatch loops", 34: "seat apps", 35: "OS plumbing", 36: "slices"}

#: (line, mapping): EXACTLY one row per step line (a bijection, enforced by
#: tests/test_harness_manager_run_state.py, which also proves deleting any row fails).
INVENTORY = (
    (22, "planner: targets from data only, never recalled (D-3); an undeclared §2 unit refuses (F1)"),
    (25, "planner: observed list-timers --all per seat, read as fields (one sub-run per seat, Q3)"),
    (26, "planner: observed running services per seat, read as fields"),
    (31, "category: wake timers (pause) + the consume timer (F3)"),
    (32, "category: inbound bridges (pause) + the codex receiver unit"),
    (33, "category: dispatch loops (pause; schedules/<persona>.yaml)"),
    (34, "category: seat apps (judgement: never a target, reported as residue)"),
    (35, "category: OS plumbing (never)"),
    (36, "category: slices (never)"),
    (38, "structural: no op names the mailbox hub or its owner account"),
    (45, "harness.run-state-manifest (record, atomic + fsync, FIRST op; Recovery (q))"),
    (48, "harness.run-state-manifest (in the seat's OWN state dir, never /home/claude; D-6)"),
    (52, "harness.run-state-unit (disable-now, every timer before any service)"),
    (55, "harness.run-state-drain (bounded wait; timeout aborts, never stops a oneshot; Recovery (r))"),
    (58, "harness.run-state-unit (disable-now, long-running services)"),
    (60, "structural: the only stop action is disable-now; its postcondition asserts UnitFileState=disabled"),
    (63, "harness.run-state-reset-failed (targets + their oneshots, per seat)"),
    (72, "structural: no op names a session, the runtime scope or a process; residue reported (F2)"),
    (80, "anti-step: no check reads a state by substring (the §4 trap test)"),
    (83, "commit check no-active-wake (exact ActiveState field)"),
    (84, "commit check unit-fields (UnitFileState/ActiveState read as fields)"),
    (92, "structural: verified by enumeration of fields, never by a return code"),
    (94, "commit check no-active-wake (paused seat: zero active wake units)"),
    (95, "commit check unit-fields per kept unit (enabled/active); kept seats get no sub-run"),
    (97, "commit check no-new-failed (exact set against the plan-time failed set)"),
    (102, "harness.run-state-unit (enable-now per manifest line; services before timers, D-5)"),
    (105, "harness.run-state-commit (restore re-verify, Recovery (t)); keep-list = the legitimate partial"),
    (110, "planner: desired.keep (exact unit names in the target set)"),
    (112, "structural: one seat-scope sub-run per seat as its own account (Q3)"),
    (113, "structural: as line 38 (consumers paused, delivery untouched)"),
    (114, "out-of-scope: the ungraceful emergency path stays the hand SOP (A-7, Q2)"),
    (117, "commit check residue: Recovery-side monitors + restart-drain named (Recovery (s)); no cross-account op"),
    (124, "out-of-scope: the ledger entry is the operator's record (receipts + manifest are the data)"),
    (129, "out-of-scope: moving the manifest to knowledge/episodic is the operator's"),
)


def inventory_parity(text: str, rows=INVENTORY) -> tuple[list[int], list[int]]:
    """(step lines with no row, rows that are not a step line). Exact."""

    steps = set(step_lines(text))
    mapped = [line for line, _m in rows]
    return sorted(steps - set(mapped)), sorted(set(mapped) - steps)
