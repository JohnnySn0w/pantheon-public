"""harness-manager ``remove`` leg, the full-seat decommission: shared pure data (T-5859, T-5091 U3).

UNTESTED-LIVE. Built dark: nothing here actuates. The first live fire is supervised. If any
op here ever needs root or another account it is reclassified (a host-substrate family,
ORG-080 A-4), never widened in place (Recovery D15 (T-5859)).

SEAT HALF ONLY (mason ruling (i), 2026-11-02). A ``root_half: managed`` seat refuses
``root-half-pending`` (T-5873 carries the per-seat host-substrate retire seam and the
seat-before-root ordering on remove); an ``observe-only`` seat gets no root op and the
manifest says so.

QUARANTINE, NEVER DELETE (ORG-052 §8 / ORG-080 D-E, "retire != delete"). Every artifact a
bring-up created is either moved by a same-filesystem rename into the seat's OWN quarantine
tree, disabled with its pre-state recorded, or left in place and listed as ``retained``.
No op removes data; a test walks this module, the planner section and the simulated
handler for deletion calls. Same-filesystem is enforced as DATA here (every move op carries
``rename: same-filesystem``); a future live handler must refuse EXDEV itself and never fall
back to a copy-then-delete.

This leaf module is imported by BOTH the planner (harness-manager.py) and the simulated
handler (harness-manager_handlers.py). It is pure: no I/O. Every function takes the
documents it reads as arguments.

Contents:

* the installer-artifact classification: every installer op kind (``KIND_DISPOSITION``) and
  every ``mkdir`` op id (``MKDIR_DISPOSITION``) mapped to what the decommission does with
  it. An unclassified kind or mkdir id REFUSES the plan (fail closed: a new installer
  artifact needs a classification before a decommission can plan);
* the quarantine naming (per seat, per stamp; collision-safe) and the tree digests;
* the exact-field CHECK table (postconditions as data, the U4/U5 convention);
* Recovery D15 (T-5859) (w)/(x)/(y): routing residue, cross-seat references and retained
  secrets, computed as pure data from the committed checkout's documents (paths only;
  a secret is never read).
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re

#: Recovery D15 (T-5859) / U4 (i): the only systemd manager these ops address.
SYSTEMD_MANAGER = "user"
#: Recovery D15 (T-5859) / U4 (n): who is paged when a halt or a stopped undo leaves a seat
#: half-removed and unsupervised.
ESCALATION_RECIPIENT = "recovery"
#: ORG-080 §2 YAML contract (gates): the operation gate every decommission passes.
GATE = "decommission"
#: The component-local class (Recovery D15 (T-5859)), in plan order.
KINDS = (
    "harness.decommission-record",
    "harness.decommission-unit",
    "harness.decommission-move",
    "harness.decommission-commit",
)
RECORD_SCHEMA = "pantheon-decommission-manifest/v1"
#: The run stamp (U5's form): it names the quarantine tree, so it is unique per run.
STAMP_RE = re.compile(r"\d{8}T\d{6}Z")
#: Recovery D15 (T-5859) (v): retiring a persona is org-shape; EVERY seat needs an operator anchor.
OPERATOR_ANCHOR_RE = re.compile(r"operator:[A-Za-z0-9][A-Za-z0-9._#/-]{0,79}")
#: The follow-up that carries the root half (mason ruling (i)).
ROOT_HALF_TICKET = "T-5873"
#: systemd property values (exact, never substrings: U5 SOP §4).
UP = {"UnitFileState": "enabled", "ActiveState": "active"}
DOWN = {"UnitFileState": "disabled", "ActiveState": "inactive"}
#: A unit already settled: nothing to stop (a static oneshot, a disabled timer).
SETTLED_FILE_STATES = ("disabled", "static")
IN_FLIGHT = ("active", "activating", "deactivating", "reloading")
#: persona_rollback.sh:68 (== tools/runtime_controller.py TMUX_RUNTIME_SCOPE_UNIT; U4's
#: harness_manager_supervisor.RUNTIME_SCOPE_UNIT, parity-tested).
RUNTIME_SCOPE_UNIT = "pantheon-runtime-tmux.scope"
CONTROLLER_STEM = "pantheon-runtime-controller"

# --- the installer-artifact classification -----------------------------------------

NONE, MOVE, UNIT, RETAINED, MKDIR, COVERED = "none", "move", "unit", "retained", "mkdir", "covered"

#: Installer op kind -> what the decommission does with what that op created.
#:   none      the op creates nothing that persists (checks, probes, fires, observes)
#:   move      the op's target is quarantined (a file or a dir)
#:   unit      the op ENABLED a unit: it is disabled --now (pre-state recorded) when up
#:   retained  a key inside a shared seat document: left in place, listed (D-2: a key
#:             removal is deletion-shaped)
#:   mkdir     per op id, ``MKDIR_DISPOSITION``
#:   covered   the op's artifact lies inside a quarantined dir (refuses if it does not)
KIND_DISPOSITION = {
    "brief-check": NONE, "claude-mcp-server-check": NONE, "codex-standing-instructions-override-check": NONE,
    "codex-standing-instructions-source-check": NONE, "codex-standing-instructions-target-check": NONE,
    "codex-telegram-disabled-first": NONE, "harness-check": NONE, "identity-check": NONE, "manager-check": NONE,
    "policy-check": NONE, "source-check": NONE, "timer-enablement-absence-check": NONE,
    "codex-telegram-mcp-check": NONE, "codex-telegram-source-check": NONE, "daemon-reload": NONE,
    "direct-fire": NONE, "mailbox-probe": NONE, "mailbox-writable-root-check": NONE,
    "seat-periodic-daemon-reload": NONE, "seat-periodic-timer-observe": NONE, "stale-unit-failure-reset": NONE,
    "timer-observe": NONE, "unit-fire": NONE,
    "user-unit-install": MOVE, "seat-periodic-unit-install": MOVE, "codex-standing-instructions-install": MOVE,
    "seat-runtime-residue-quarantine": MOVE,
    "timer-enable": UNIT, "seat-periodic-timer-enable": UNIT, "seat-service-enable-observe": UNIT,
    "claude-mcp-server-registration": RETAINED, "seat-claude-settings-env": RETAINED,
    "seat-codex-hook-config": RETAINED,
    "seat-claude-workspace-trust": RETAINED,
    "mkdir": MKDIR,
    "atomic-write": COVERED, "seat-service-decommission": COVERED,
}

SHARED, MOVE_DIR, INSIDE = "shared", "move", "inside"
#: Installer mkdir op id -> shared ancestor (retained) | move (quarantined whole) | inside a moved dir.
MKDIR_DISPOSITION = {
    "connective.config-dir": SHARED,
    "connective.systemd-dir": SHARED,
    "connective.unit-dir": SHARED,
    "connective.local-dir": SHARED,
    "connective.local-state-dir": SHARED,
    "connective.pantheon-state-dir": SHARED,
    "connective.installer-state-dir": SHARED,
    "connective.state-dir": MOVE_DIR,
    "connective.receipt-dir": INSIDE,
    "codex-telegram.state-dir": MOVE_DIR,
    "codex-telegram.release-root": MOVE_DIR,
    # T-6118: chronicle-export dirs live outside the seat home; none is removed by a single
    # seat's decommission (retained, not moved, so the outside-the-home check never applies).
    "chronicle-export.root": SHARED,      # shared cross-seat parent
    "chronicle-export.ingested": SHARED,  # shared cross-seat ingested archive
    "chronicle-export.seat": SHARED,      # holds not-yet-ingested transcripts; see MKDIR_RETAIN_REASON
}

#: Optional per-id reason for a retained mkdir; ids absent here keep the generic reason.
MKDIR_RETAIN_REASON = {
    "chronicle-export.seat": "holds transcripts not yet ingested; removal belongs to the receipt-driven "
                             "prune (T-6118 PR-5), not decommission (data loss, D-2)",
}
DEFAULT_MKDIR_RETAIN_REASON = "shared ancestor dir (installer mkdir); other paths live in it"


class Unclassified(ValueError):
    """An installer artifact the decommission has no disposition for (fail closed)."""


def disposition(kind: str) -> str:
    if kind not in KIND_DISPOSITION:
        raise Unclassified(f"installer op kind {kind!r} has no decommission disposition")
    return KIND_DISPOSITION[kind]


def mkdir_disposition(installer_id: str) -> str:
    if installer_id not in MKDIR_DISPOSITION:
        raise Unclassified(f"installer mkdir {installer_id!r} has no decommission disposition")
    return MKDIR_DISPOSITION[installer_id]


def under(path: str, root: str) -> bool:
    """``path`` is ``root`` or below it (both normalized absolute paths)."""

    return path == root or path.startswith(root.rstrip("/") + "/")


def unit_name_of(path: str) -> str:
    return posixpath.basename(path)


def instance_unit(file_name: str, persona: str) -> str:
    """The unit a unit FILE yields for this seat: a template ``x@.service`` -> ``x@<persona>.service``."""

    stem, dot, suffix = file_name.rpartition(".")
    if stem.endswith("@"):
        return f"{stem}{persona}.{suffix}"
    return file_name


def fragment_of(unit: str, unit_dir: str) -> str:
    """The unit file systemd loads ``unit`` from (an instance loads its template)."""

    stem, dot, suffix = unit.rpartition(".")
    if "@" in stem:
        stem = stem.split("@", 1)[0] + "@"
    return f"{unit_dir}/{stem}.{suffix}"


# --- quarantine naming and digests -------------------------------------------------


def home_of(account: str) -> str:
    return f"/home/{account}"


def quarantine_root(account: str, stamp: str) -> str:
    """Per seat (the seat's own home), per run (the stamp). A run whose root already
    exists refuses (choose a fresh stamp); nothing is ever overwritten."""

    if not STAMP_RE.fullmatch(stamp or ""):
        raise ValueError(f"stamp {stamp!r} is not YYYYMMDDTHHMMSSZ")
    return f"{home_of(account)}/.local/state/pantheon/quarantine/decommission/{stamp}"


def record_path(root: str) -> str:
    return f"{root}/manifest.json"


def destination(root: str, account: str, path: str) -> str:
    """Where ``path`` goes: ``<root>/tree/<path relative to the seat home>``."""

    home = home_of(account)
    if not under(path, home) or path == home:
        raise ValueError(f"{path} is not inside the seat's home {home}")
    return f"{root}/tree/{path[len(home) + 1:]}"


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def subtree(files, root: str) -> dict:
    """Every observed entry at or below ``root``, keyed by its path relative to ``root``
    (``""`` is ``root`` itself)."""

    out = {}
    for path, entry in (files or {}).items():
        if under(path, root):
            out[path[len(root):].lstrip("/")] = entry
    return out


def tree_digest(files, root: str) -> str | None:
    """A file's own sha256, or the digest of a directory's whole subtree; None when absent."""

    entries = subtree(files, root)
    top = entries.get("")
    if not isinstance(top, dict):
        return None
    if top.get("kind") == "file":
        return top.get("sha256")
    return digest(sorted(entries.items()))


def tree_bytes(files, root: str) -> int:
    """Recovery D15 (T-5859) (z): the byte size a quarantine keeps (plain data)."""

    return sum(int(e.get("size") or 0) for e in subtree(files, root).values()
               if isinstance(e, dict) and e.get("kind") == "file")


#: Command-line token separators (review F1(c)): whitespace, ``=``, shell joiners and quotes,
#: so ``sh -c 'cd /x&&./run'`` and ``WorkingDirectory=/x`` both yield ``/x``.
_TOKEN_SPLIT = re.compile(r"[\s=;&|()'\"]+")


def paths_in(text: str, home: str | None = None) -> list[str]:
    """Absolute paths named in a command line or a directory field (for (x) / F4). ``~`` and
    systemd's ``%h`` expand to ``home`` (the unit's own account home) when it is known."""

    out = []
    for token in _TOKEN_SPLIT.split(text or ""):
        if home:
            if token == "~" or token.startswith("~/"):
                token = home + token[1:]
            token = token.replace("%h", home)
        if token.startswith("/"):
            out.append(posixpath.normpath(token))
    return out


#: Review R2-1: every UnitFileState under which systemd starts the unit (until reboot included).
ARMED_FILE_STATES = ("enabled", "enabled-runtime", "linked", "linked-runtime", "alias")
#: Review F2 / R2-4: the ORG-106 records dir inside the persona state root; it never moves.
ORG106_DIR = "overrides"


def unit_live(spec) -> bool:
    """Review F1(b) / R2-1: a unit counts as live (armed) when its UnitFileState starts it
    (``ARMED_FILE_STATES``), it failed (a Restart= may be pending) or it is in flight.
    Exact systemd property values."""

    return isinstance(spec, dict) and (spec.get("UnitFileState") in ARMED_FILE_STATES
                                       or spec.get("ActiveState") == "failed"
                                       or spec.get("ActiveState") in IN_FLIGHT)


def timer_target(timer: str, spec) -> str:
    """The unit a ``.timer`` fires: its observed ``Unit=`` when given, else the same-name service."""

    named = spec.get("Unit") if isinstance(spec, dict) else None
    return named if isinstance(named, str) and named else timer[: -len(".timer")] + ".service"


def triggered_by_live_timers(units) -> set:
    return {timer_target(name, spec) for name, spec in (units or {}).items()
            if name.endswith(".timer") and unit_live(spec)}


def live_refs_inside(units, sources, *, account: str | None = None, exclude=()) -> list[str]:
    """Units whose ExecStart / WorkingDirectory resolves inside a tree being quarantined, among
    the LIVE ones: armed, failed, in flight, or the target of a live timer (review F1(a): a
    timer's oneshot is inactive between fires). ``account`` is the owner when a unit does not
    name one itself (the seat's own units, review F4). A path under any ``exclude`` root (the
    retained ORG-106 ``overrides/``, which never moves: review R2-4) is no hit."""

    units = units or {}
    triggered = triggered_by_live_timers(units)
    out = []
    for unit, spec in sorted(units.items()):
        if not isinstance(spec, dict) or not (unit_live(spec) or unit in triggered):
            continue
        owner = spec.get("account") or account
        home = home_of(owner) if isinstance(owner, str) and owner else None
        named = paths_in(spec.get("ExecStart") or "", home) + paths_in(spec.get("WorkingDirectory") or "", home)
        named = [p for p in named if not any(under(p, e) for e in exclude)]
        hits = sorted({s for s in sources for p in named if under(p, s)})
        if hits:
            out.append(f"{owner or '?'}:{unit} -> {', '.join(hits)}")
    return out


def peer_refs_inside(peer_units, sources, exclude=()) -> list[str]:
    """Recovery D15 (T-5859) (x): LIVE peer units whose ExecStart / WorkingDirectory resolves
    inside a tree being quarantined (see ``live_refs_inside``)."""

    return live_refs_inside(peer_units, sources, exclude=exclude)


# --- postconditions ----------------------------------------------------------------


def encode(checks) -> str:
    return json.dumps(list(checks), sort_keys=True, separators=(",", ":"))


def decode(raw: str):
    checks = json.loads(raw)
    if not isinstance(checks, list):
        raise ValueError("postconditions must be a list")
    for item in checks:
        if not (isinstance(item, dict) and item.get("check") in CHECKS and isinstance(item.get("params", {}), dict)):
            raise ValueError(f"malformed postcondition {item!r}")
    return checks


def check(check_id: str, advisory: bool = False, **params) -> dict:
    if check_id not in CHECKS:
        raise ValueError(f"unknown check {check_id!r}")
    out = {"check": check_id, "params": {k: str(v) for k, v in params.items()}}
    if advisory:
        out["advisory"] = True
    return out


def _unit(view, name):
    unit = (view.get("units") or {}).get(name)
    return unit if isinstance(unit, dict) else None


def _c_unit_fields(view, p):
    unit = _unit(view, p["unit"])
    if unit is None:
        return False, f"{p['unit']}: no observation (could not ask)"
    got = {k: unit.get(k) for k in ("UnitFileState", "ActiveState")}
    want = {k: p[k] for k in ("UnitFileState", "ActiveState")}
    return got == want, f"{p['unit']}: {got} (want {want})"


def _c_unit_gone(view, p):
    """Not loadable any more and not running: LoadState exactly not-found, ActiveState inactive,
    or no entry at all in the seat's own manager."""

    unit = _unit(view, p["unit"])
    if unit is None:
        return True, f"{p['unit']}: not loaded"
    ok = unit.get("LoadState") == "not-found" and unit.get("ActiveState") == "inactive"
    return ok, f"{p['unit']}: LoadState {unit.get('LoadState')!r} ActiveState {unit.get('ActiveState')!r}"


def _c_session_absent(view, p):
    sessions = view.get("sessions")
    if not isinstance(sessions, dict):
        return False, "no session observation (could not ask)"
    return p["session"] not in sessions, f"session {p['session']!r} {'present' if p['session'] in sessions else 'absent'}"


def _c_path_absent(view, p):
    files = view.get("files")
    if not isinstance(files, dict):
        return False, "no file observation (could not ask)"
    present = bool(subtree(files, p["path"]))
    return not present, f"{p['path']} {'present' if present else 'absent'}"


def _c_path_digest(view, p):
    files = view.get("files")
    if not isinstance(files, dict):
        return False, "no file observation (could not ask)"
    got = tree_digest(files, p["path"])
    return got == p["sha256"], f"{p['path']} digest {str(got)[:12]} (want {p['sha256'][:12]})"


def _c_record(view, p):
    record = view.get("record")
    if not isinstance(record, dict):
        return False, "no decommission manifest"
    body = {k: v for k, v in record.items() if k != "status"}
    ok = digest(body) == p["sha256"] and record.get("status") == p["status"]
    return ok, f"manifest status {record.get('status')!r} (want {p['status']!r})"


def _c_residue(view, p):
    return True, f"{p['outcome']}: {p['residue']}"


CHECKS = {
    "unit-fields": _c_unit_fields,
    "unit-gone": _c_unit_gone,
    "session-absent": _c_session_absent,
    "path-absent": _c_path_absent,
    "path-digest": _c_path_digest,
    "record": _c_record,
    "residue": _c_residue,
}


def evaluate(checks, view) -> list[dict]:
    out = []
    for item in checks:
        try:
            ok, detail = CHECKS[item["check"]](view, item.get("params") or {})
        except (KeyError, TypeError, ValueError) as exc:
            ok, detail = False, f"malformed: {exc}"
        out.append({"check": item["check"], "ok": ok is True, "advisory": bool(item.get("advisory")), "detail": detail})
    return out


def verdict(results) -> tuple[bool, str]:
    failed = [r for r in results if not r["ok"] and not r["advisory"]]
    if failed:
        return False, "; ".join(r["detail"] for r in failed)
    return True, f"{len(results)} postcondition(s) hold"


def record_body(record) -> dict:
    """The manifest without its status (the status is what the record and commit ops move)."""

    return {k: v for k, v in record.items() if k != "status"}


# --- Recovery D15 (T-5859) (w) / (x) / (y): residue and secrets, pure data ------------------

#: (w) where a persona stays ROUTABLE. (path, kind, what): ``yaml-roles`` / ``yaml-personas`` /
#: ``yaml-grants`` read structurally; ``text`` reports the lines naming the persona.
ROUTING_SOURCES = (
    ("instance.yaml", "yaml-roles", "the instance role binding (agent_role_map derives from it)"),
    ("instance.yaml", "yaml-bridge", "codex_telegram_bridge persona entry"),
    ("persona_roles.yaml", "yaml-personas", "persona_roles role pool membership"),
    ("tools/agent_role_map.py", "text", "agent_role_map fallback bindings"),
    ("tools/inbox_consumer.py", "text", "inbox_consumer delivery targets"),
    ("tools/mailbox_wake.py", "text", "mailbox_wake"),
    ("repos.yaml", "yaml-grants", "repos.yaml grants keyed by the role"),
)
#: (x) peer-side references that target the persona.
CROSS_SEAT_SOURCES = (
    ("tools/systemd/expected-units.yaml", "yaml-units", "declared peer units for the persona"),
    ("tools/context_fill_monitor.py", "text", "Recovery-side context-fill monitor"),
    ("tools/activity_monitor.py", "text", "Recovery-side activity monitor"),
    ("tools/restart_drain.py", "text", "restart-drain coverage"),
    ("tools/fleet_health.py", "text", "fleet_health sweep"),
    ("sop/standard/seat-parity/axes.yaml", "text", "seat-parity sweep axes"),
    ("sop/standard/seat-parity/carve-outs.yaml", "text", "seat-parity carve-outs"),
    ("tools/verify_startup.sh", "text", "verify_startup agent lists"),
    # review R2-6: not an @P unit, so neither (x) nor (x2) sees it; Recovery D15 (T-5859) (x3) is
    # the guard (remediation must be vetoed for the seat); it stays named here as residue
    ("tools/parity_remediate.py", "fleet",
     "Recovery's fleet-wide pantheon-parity-remediate can `enable --now` this seat's timers across the "
     "user boundary (apply_repair); it is not an @P unit, so it is guarded by (x3) (remediation "
     "vetoed: seat-maintenance armed or deep-sleep) at decommission time only. A seat-maintenance "
     "window is capped (240 minutes); once it lapses, parity remediation will attempt enable --now "
     "on the moved units and fail, a recurring false escalation, until the (w) routing-data PR or a "
     "parity carve-out for this seat lands"),
)
#: (x) the Recovery-side monitors that target a persona by instance name (U5's stems).
RECOVERY_MONITOR_STEMS = ("pantheon-context-fill-monitor", "pantheon-activity-monitor")
SOURCE_PATHS = tuple(sorted({p for p, _k, _w in ROUTING_SOURCES + CROSS_SEAT_SOURCES}))


def _word_lines(text: str, word: str) -> list[int]:
    pattern = re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(word)}(?![A-Za-z0-9_-])")
    return [i for i, line in enumerate(text.splitlines(), 1) if pattern.search(line)]


def _lines(lines: list[int]) -> str:
    shown = ",".join(str(n) for n in lines[:12])
    return shown + (f",... ({len(lines)} lines)" if len(lines) > 12 else "")


def _residue(sources, docs, *, persona: str, role_key: str) -> list[str]:
    out = []
    for path, kind, what in sources:
        raw = docs.get(path)
        if raw is None:
            out.append(f"{path}: not declared (absent at the plan revision); check {what} by hand")
            continue
        if kind == "text":
            lines = _word_lines(raw, persona)
            if lines:
                out.append(f"{path}: {what} names {persona!r} (lines {_lines(lines)})")
            continue
        doc = raw if isinstance(raw, (dict, list)) else None
        if kind == "yaml-roles":
            roles = (doc or {}).get("roles") or {}
            hits = sorted(k for k, r in roles.items() if isinstance(r, dict) and r.get("persona") == persona)
            out += [f"{path}: roles.{k} binds persona {persona!r} ({what})" for k in hits]
        elif kind == "yaml-bridge":
            bridge = (((doc or {}).get("persona_installer") or {}).get("codex_telegram_bridge") or {}).get("personas") or {}
            if persona in bridge:
                out.append(f"{path}: persona_installer.codex_telegram_bridge.personas.{persona} ({what})")
        elif kind == "yaml-personas":
            personas = (doc or {}).get("personas") or {}
            if persona in personas:
                roles = (personas[persona] or {}).get("role_ids") or []
                out.append(f"{path}: personas.{persona} role_ids {list(roles)} ({what})")
        elif kind == "yaml-grants":
            repos = []
            for entry in _repo_entries(doc):
                grants = entry.get("grants") if isinstance(entry.get("grants"), dict) else {}
                agents = entry.get("agents") if isinstance(entry.get("agents"), list) else []
                if role_key in grants or role_key in agents:
                    repos.append(str(entry.get("repo") or entry.get("name") or "?"))
            if repos:
                out.append(f"{path}: role {role_key!r} is granted {sorted(set(repos))} ({what})")
        elif kind == "yaml-units":
            out += _declared_units(path, doc, persona)
        elif kind == "fleet":  # a fleet-wide path that can reach EVERY seat (review R2-6): always named
            out.append(f"{path}: {what}")
    return out


def _repo_entries(doc):
    """Every mapping in repos.yaml that carries ``grants`` or ``agents`` (any nesting)."""

    found = []

    def walk(node):
        if isinstance(node, dict):
            if "grants" in node or "agents" in node:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(doc)
    return found


def _declared_units(path, doc, persona):
    out, named = [], set()
    for account, entries in sorted((doc or {}).items()) if isinstance(doc, dict) else []:
        for entry in entries if isinstance(entries, list) else []:
            unit = entry.get("unit") if isinstance(entry, dict) else None
            if isinstance(unit, str) and f"@{persona}." in unit:
                named.add(unit)
                out.append(f"{path}: {account} declares {unit} (stays armed; cross-account, by hand)")
    for stem in RECOVERY_MONITOR_STEMS:
        unit = f"{stem}@{persona}.timer"
        if unit not in named:
            out.append(f"{path}: {unit} is not declared; check the Recovery-side monitor by hand")
    return out


def routing_residue(docs, *, persona: str, role_key: str) -> list[str]:
    """Recovery D15 (T-5859) (w): every place the persona is still routable, from the committed
    checkout's documents (``docs``: path -> parsed YAML / text / None when absent)."""

    return _residue(ROUTING_SOURCES, docs, persona=persona, role_key=role_key)


def cross_seat_residue(docs, *, persona: str, role_key: str) -> list[str]:
    """Recovery D15 (T-5859) (x): peer-side references that target the persona."""

    return _residue(CROSS_SEAT_SOURCES, docs, persona=persona, role_key=role_key)


_PATH_TOKEN = re.compile(r"(~/[^\s,()]+|/home/[^\s,()]+)")
#: (y) the harness OAuth credential file, by harness convention.
HARNESS_CREDENTIALS = {
    "claude-code": (".claude/.credentials.json", "claude OAuth (harness convention)"),
    "codex": (".codex/auth.json", "codex ChatGPT OAuth (harness convention)"),
}


def secrets_retained(*, account: str, persona: str, role, bridge, moves) -> tuple[list[dict], list[str]]:
    """Recovery D15 (T-5859) (y): the secrets left on the host, by PATH ONLY (never read).
    Returns (entries, underivable). An entry inside a quarantined tree also names where it
    went. Sources: the role's ``token_ref`` / ``telegram_bot_token_ref`` (instance.yaml),
    the codex_telegram_bridge ``credential_env``, and the harness credential convention."""

    home = home_of(account)
    found, underivable = {}, []

    def add(path, source):
        path = posixpath.normpath(home + path[1:] if path.startswith("~/") else path)
        found.setdefault(path, source)

    role = role or {}
    for field in ("token_ref", "telegram_bot_token_ref"):
        text = role.get(field)
        if not isinstance(text, str):
            continue
        paths = _PATH_TOKEN.findall(text)
        for path in paths:
            add(path, f"instance.yaml roles.<{persona}>.{field}")
        if not paths:
            underivable.append(f"{field}: {text.strip()!r} names no path (deploy key or credential not derivable)")
    if not isinstance(role.get("token_ref"), str):
        underivable.append("token_ref: absent from the role (git credential not derivable)")
    env = (bridge or {}).get("credential_env")
    if isinstance(env, str):
        add(f"{home}/{env}", "instance.yaml codex_telegram_bridge credential_env (bot token)")
    harness = role.get("harness")
    if harness in HARNESS_CREDENTIALS:
        rel, source = HARNESS_CREDENTIALS[harness]
        add(f"{home}/{rel}", source)
    else:
        underivable.append(f"harness {harness!r}: no credential-file convention")
    entries = []
    for path in sorted(found):
        entry = {"path": path, "source": found[path]}
        for source, dest in moves:
            if under(path, source):
                entry["quarantined_to"] = dest + path[len(source):]
        entries.append(entry)
    return entries, underivable


# --- Recovery D15 (T-5859) (x3): fleet remediation must be vetoed for the seat ----------------

#: notify_policy's maintenance-registry timestamp form (``_parse_iso``: %Y-%m-%dT%H:%M:%SZ);
#: fixed-width, so string order is time order.
ISO_Z_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def remediation_vetoed(observed, record_vetoes):
    """Recovery D15 (T-5859) (x3): True / False / None (could not ask) over the OBSERVED fact
    ``{"observed_at", "maintenance", "seat_state"}``, mirroring the
    ``EffectiveSeatState.remediation_vetoed`` FIELD as data (NOT the wider
    ``tools.seat_state.remediation_vetoed()`` function, which since T-5918 also counts a U5
    run_state pause through ``repair_vetoed``; whether (x3) should take that wider veto is
    routed to Recovery, mason ruling Q1 on T-5918): vetoed iff the seat-maintenance
    registry holds an entry whose ``expires`` is after the observation time
    (``notify_policy.in_maintenance``: a missing or malformed expires reads not-armed), OR the
    seat-state record vetoes (``seat_state.parse_record(...).remediation_vetoed``: only an
    unexpired, valid deep-sleep record; paused-intake does NOT). ``record_vetoes(record,
    observed_at)`` is that pure parse, injected by the caller so this module stays stdlib-only.
    A missing observation, or one without both keys and a valid clock, is None (fail closed)."""

    if not isinstance(observed, dict) or not {"observed_at", "maintenance", "seat_state"} <= set(observed):
        return None
    now = observed["observed_at"]
    if not isinstance(now, str) or not ISO_Z_RE.fullmatch(now):
        return None
    entry = observed["maintenance"]
    expires = entry.get("expires") if isinstance(entry, dict) else None
    if isinstance(expires, str) and ISO_Z_RE.fullmatch(expires) and expires > now:
        return True
    record = observed["seat_state"]
    return record is not None and bool(record_vetoes(record, now))


# --- Recovery D15 (T-5859) (x2): Recovery-side coverage of the persona must be disarmed -------------

#: The unit suffixes a Recovery-side monitor of the persona comes in (each is checked).
COVERAGE_SUFFIXES = ("service", "timer")


def peer_coverage_units(expected_units_doc, persona: str) -> list[str]:
    """Recovery D15 (T-5859) (x2): every peer-side unit that targets the persona: the Recovery-side
    monitors (context-fill / activity, service and timer, ALWAYS listed: they run from Recovery's
    own clone, so (x)'s inside-the-tree check cannot see them, and the context-fill restart
    path targets the persona's runtime controller) plus every unit another seat declares in
    tools/systemd/expected-units.yaml naming ``@<persona>.`` (restart-drain / handoff
    coverage included). The same data source as U5's Recovery residue and (x)."""

    units = {f"{stem}@{persona}.{suffix}" for stem in RECOVERY_MONITOR_STEMS for suffix in COVERAGE_SUFFIXES}
    doc = expected_units_doc if isinstance(expected_units_doc, dict) else {}
    for owner, entries in doc.items():
        if owner == persona or not isinstance(entries, list):
            continue  # the persona's own units are this leg's, not peer coverage
        for entry in entries:
            unit = entry.get("unit") if isinstance(entry, dict) else None
            if isinstance(unit, str) and f"@{persona}." in unit:
                units.add(unit)
    return sorted(units)


def peer_coverage_state(peer_units, units) -> tuple[list[str], list[str]]:
    """(armed, could_not_ask) over the OBSERVED peer units (exact systemd property values;
    never a live read here). Armed: ``unit_live`` (UnitFileState in ARMED_FILE_STATES, failed,
    or in flight). Disarmed: observed and none of those. Absent: LoadState ``not-found`` AND
    ActiveState ``inactive`` (a unit still running after its file went is armed). No
    observation at all, or a malformed one, is could-not-ask (fail closed)."""

    armed, unknown = [], []
    for unit in units:
        spec = (peer_units or {}).get(unit)
        if not isinstance(spec, dict) or not {"LoadState", "UnitFileState", "ActiveState"} <= set(spec):
            unknown.append(unit)
        elif spec["LoadState"] == "not-found" and spec["ActiveState"] == "inactive":
            continue  # proven absent: no unit file AND nothing running (review R2-1, as _c_unit_gone)
        elif unit_live(spec):  # armed: ARMED_FILE_STATES, failed or in flight (review R2-1)
            armed.append(f"{spec.get('account', '?')}:{unit} ({spec['UnitFileState']}/{spec['ActiveState']})")
    return armed, unknown
