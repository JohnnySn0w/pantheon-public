"""T-5859 (T-5091 U3, germination GF2): harness-manager ``plan_remove``, the full-seat
decommission, SEAT HALF ONLY, built DARK (UNTESTED-LIVE).

The seat's bring-up (U1's installer plan) is classified artifact by artifact and inverted:
a manifest written first, disable --now of every installer unit still up, a same-filesystem
rename of every installer artifact into the seat's own quarantine tree, a daemon-reload and
a commit. Nothing is deleted. Ops run only through the SIMULATED HarnessDecommissionSim.

The ``test_real_*`` tests read the real repo, instance.yaml and the installer's own plan,
read-only. Engine-tier tests run on a tmp checkout over a simulated host, reusing the GF1
fixture. Recovery D15 (T-5859) conditions (v)-(z) each have tests named for them.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from tools import persona_installer as PI
from tools.germinate.engine.components import load_repo_op_registry, read_op_kinds
from tools.germinate.engine.contract import load_contract
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.planner import InProcessJsonPlannerRunner, PlannerGuard
from tools.germinate.engine.tests import helpers as H
from tools.germinate.host_substrate import families as F
from tools.germinate.op_registry import RESERVED_CLASSES, component_kind_errors
from tools.germinate.planner_sandbox.admission import coordinator

from tools.germinate.components import harness_manager_remove as R
from tools.germinate.components import harness_manager_root as HR
from tools.germinate.components import harness_manager_run_state as RS
from tools.germinate.components import harness_manager_supervisor as S
from tools.germinate.components.tests import test_harness_manager_component as GF1

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPONENTS = REPO_ROOT / "tools" / "germinate" / "components"
PLANNER = COMPONENTS / "harness-manager.py"
HANDLERS = COMPONENTS / "harness-manager_handlers.py"
HELPER = COMPONENTS / "harness_manager_remove.py"
MANIFEST_PATH = REPO_ROOT / "instance.yaml"
CID = "harness-manager"
REV = "0123456789abcdef0123456789abcdef01234567"
STAMP = "20261102T050000Z"
ANCHOR = "operator:TG7001"
GATE = "decommission"
ACCOUNTS = {"mason": "suborch1", "specialist": "claude-research", "orchestrator": "claude", "recovery": "claude-fallback",
            "watchman": "codex-fallback", "archivist": "exocortex", "artisan": "suborch2", "codex": "codex"}
UIDS = {"suborch1": 1007, "suborch2": 1008, "claude": 1000, "claude-research": 1002, "codex": 1003,
        "exocortex": 1009, "claude-fallback": 1001, "codex-fallback": 1010}
DECOMMISSIONABLE = ("mason", "artisan", "specialist", "orchestrator", "archivist", "codex")
KINDS = ("harness.decommission-record", "harness.decommission-unit", "harness.decommission-move",
         "harness.decommission-commit")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HM = _load(PLANNER, "_t5859_harness_manager")
HMH = _load(HANDLERS, "_t5859_harness_manager_handlers")


# --- fixtures: derived from the INSTALLER's plan, never from plan_remove ----------------------


def _installer_plan(persona):
    manifest = PI.Manifest.load(PI.REPO_ROOT / "instance.yaml")
    return PI.PersonaInstaller(repo_root=PI.REPO_ROOT).plan(manifest, persona, source_revision=REV)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file(path, size=100):
    return {"kind": "file", "sha256": _sha(path), "size": size}


UP = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "UnitFileState": "enabled"}
DOWN = {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead", "UnitFileState": "disabled"}
STATIC = {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead", "UnitFileState": "static"}
SCOPE_UP = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "UnitFileState": "transient"}


def _session(persona):
    for op in _installer_plan(persona).operations:
        if op.kind == "unit-fire":
            return op.desired["tmux_target"].split(":", 1)[0]
    raise AssertionError(persona)


def _seat(persona):
    """A paused seat as U5 leaves it: every installer artifact on disk, the wake timers
    (RS.INSTALLER_WAKE_STEMS) disabled, every other timer (quota, restart-drain, the T-5961
    fleet-usage periodics) and the runtime controller still up."""

    account = ACCOUNTS[persona]
    home = f"/home/{account}"
    files = {f"{home}/.config/systemd/user": {"kind": "dir"}}
    units = {}
    for op in _installer_plan(persona).operations:
        if op.kind in ("user-unit-install", "seat-periodic-unit-install", "codex-standing-instructions-install", "atomic-write"):
            parent = op.target.rsplit("/", 1)[0]
            files.setdefault(parent, {"kind": "dir"})
            files[op.target] = _file(op.target)
        elif op.kind in ("mkdir", "seat-runtime-residue-quarantine"):
            files.setdefault(op.target, {"kind": "dir"})
        if op.kind in ("timer-enable", "seat-periodic-timer-enable"):
            keep_up = not op.target.startswith(tuple(f"{stem}@" for stem in RS.INSTALLER_WAKE_STEMS))
            units[op.target] = dict(UP if keep_up else DOWN)
            service = op.target[: -len(".timer")] + ".service"
            units[service] = dict(STATIC)
        elif op.kind == "seat-service-enable-observe":
            units[op.target] = dict(UP)
    personas = f"{home}/.local/state/pantheon/personas/{persona}"
    files[f"{personas}/overrides"] = {"kind": "dir"}
    files[f"{personas}/overrides/seat-state.json"] = _file("seat-state", 321)
    files[f"{personas}/overrides/holds.json"] = _file("holds", 12)
    files[f"{personas}/runtime-controller"] = {"kind": "dir"}
    files[f"{personas}/runtime-controller/runtime.json"] = _file("runtime", 222)
    units[R.RUNTIME_SCOPE_UNIT] = dict(SCOPE_UP)
    units["dbus.service"] = dict(UP, ExecStart="/usr/bin/dbus-daemon --session --address=systemd:")  # never touched
    return {
        "uid": UIDS[account], "user_bus": True,
        "target_manifest_sha256": hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest(),
        "units": units, "files": files,
        "sessions": {_session(persona): {"scope": R.RUNTIME_SCOPE_UNIT}},
        "peer_units": {"pantheon-activity-monitor@x.timer": {"account": "claude-fallback", "ActiveState": "active",
                                                             "ExecStart": "/usr/bin/python3 /home/claude-fallback/tool.py",
                                                             "WorkingDirectory": "/home/claude-fallback"},
                       **_coverage_disarmed(persona)},
        # Recovery D15 (T-5859) (x3): seat-maintenance armed until after the observation time
        "remediation": {"observed_at": NOW, "maintenance": {"expires": "2026-11-02T09:00:00Z", "by": "recovery"},
                        "seat_state": _record("paused-intake")},
    }


NOW = "2026-11-02T05:00:00Z"


def _record(state, **extra):
    """A valid ORG-106 seat-state record (tools.seat_state.validate_record)."""

    from tools import seat_state

    doc = {"schema": seat_state.SCHEMA, "state": state, "phase": "set" if state == "paused-intake" else "asleep",
           "reason": "fleet bringdown", "indefinite": True, "expires": None,
           "set_ts": "2026-11-01T00:00:00Z", "phase_ts": "2026-11-01T00:00:00Z"} | extra
    assert seat_state.validate_record(doc) == [], seat_state.validate_record(doc)
    return doc


#: Recovery D15 (T-5859) (x2): the Recovery-side monitors that target a persona, HAND-LISTED.
_MONITORS = ("pantheon-context-fill-monitor", "pantheon-activity-monitor")
ABSENT = {"LoadState": "not-found", "UnitFileState": "", "ActiveState": "inactive"}


def _declared_coverage(persona):
    """Peer units another seat declares for ``persona`` in expected-units.yaml (read here, not via R)."""

    import yaml

    doc = yaml.safe_load((REPO_ROOT / "tools" / "systemd" / "expected-units.yaml").read_text(encoding="utf-8")) or {}
    return sorted(e["unit"] for owner, entries in doc.items() if owner != persona
                  for e in entries or () if isinstance(e, dict) and f"@{persona}." in str(e.get("unit")))


def _coverage_disarmed(persona):
    """Every (x2) coverage unit observed: the undeclared monitors proven absent, the declared ones disabled."""

    out = {f"{m}@{persona}.{s}": dict(ABSENT, account="claude-fallback") for m in _MONITORS for s in ("service", "timer")}
    for unit in _declared_coverage(persona):
        out[unit] = dict(DOWN, account="claude-fallback")
    return out


def _paused(persona):
    account = ACCOUNTS[persona]
    pause_id = f"rs-{account}-20261101T000000Z"
    manifest = {"schema": RS.MANIFEST_SCHEMA, "persona": persona, "account": account, "pause_id": pause_id,
                "plan_revision": REV, "stamp": "20261101T000000Z", "anchor": ANCHOR, "keep": [], "entries": []}
    return {"run_state": "paused", "manifest": manifest,
            "seat_state": {"state": "paused-intake", "pause_id": pause_id, "persona": persona}}


def _request(persona, seat=None, run_state=None, **desired):
    desired = {"stamp": STAMP, "anchor": ANCHOR, "source_revision": REV} | desired
    desired = {k: v for k, v in desired.items() if v is not None}
    account = ACCOUNTS[persona]
    return {
        "scope": f"seat-{account}", "desired": desired,
        "observed": {
            HM.DECOMMISSION_HANDLER: {"account": account, "seat": seat if seat is not None else _seat(persona)},
            HM.RUN_STATE_HANDLER: {"account": account, "seat": run_state if run_state is not None else _paused(persona)},
            HM.HANDLER: {"account": account, "seat": {"transition": None}},
        },
    }


def _plan(persona, seat=None, run_state=None, **desired):
    return HM.plan_remove(_request(persona, seat, run_state, **desired))


def _doc(ops):
    record = ops[0]
    assert record["kind"] == "harness.decommission-record"
    return json.loads(record["desired"]["document"])


def _ids(ops):
    return [op["operation_id"] for op in ops]


def _inst(raw):
    return PI.InstallOperation(
        operation_id=raw["operation_id"], layer=raw["layer"], kind=raw["kind"], target=raw["target"],
        precondition=dict(raw["precondition"]), desired=dict(raw["desired"]), inverse=raw["inverse"],
        privilege=raw["privilege"], owner=raw["owner"], provenance=dict(raw["provenance"]), execution_scope="all",
    )


def _sim(persona, seat=None):
    sim = HMH.HarnessDecommissionSim()
    sim.load_state({"seats": {ACCOUNTS[persona]: seat if seat is not None else _seat(persona)}})
    return sim


def _state(sim, persona):
    seat = sim.dump_state()["seats"][ACCOUNTS[persona]]
    return {k: v for k, v in seat.items() if k not in ("writers", "verdicts")}


def _run(sim, ops):
    mutations = []
    for raw in ops:
        op = _inst(raw)
        result = sim.preflight(op)
        assert result.ok, (raw["operation_id"], result.detail)
        mutation, evidence = sim.execute(op)
        assert evidence.ok
        assert sim.verify_operation(op).ok, (raw["operation_id"], sim.verify_operation(op).detail)
        mutations.append(mutation)
    return mutations


def _undone(state, record_path):
    """``state`` with the manifest left in place, marked undone (the leg never deletes it)."""

    out = copy.deepcopy(state)
    entry = out["files"][record_path]
    assert entry["document"]["status"] == "undone"
    del out["files"][record_path]
    return out


# --- the snapshot: what a bring-up created and what the decommission does with it -------------

#: HAND-LISTED: every quarantine source for mason (the installer's unit files, the drop-in
#: dir, the persona-installer state dir and the persona state root). Nothing else moves.
MASON_HOME = "/home/suborch1"
MASON_UNIT_DIR = f"{MASON_HOME}/.config/systemd/user"
MASON_MOVES = sorted([
    *(f"{MASON_UNIT_DIR}/{n}" for n in (
        "pantheon-persona-consume@.service", "pantheon-persona-consume@.timer",
        "pantheon-persona-quota@.service", "pantheon-persona-quota@.timer",
        "pantheon-inbox-consumer@.service", "pantheon-inbox-consumer@.timer",
        "pantheon-keep-going@.service", "pantheon-keep-going@.timer",
        "pantheon-restart-drain.service", "pantheon-restart-drain.timer", "pantheon-restart-drain.service.d",
        "pantheon-unit-failure-alert@.service", "pantheon-mailbox-wake@.service", "pantheon-mailbox-wake@.timer",
        "pantheon-runtime-controller@.service")),
    # T-5961: the installer's seat-periodic fleet-usage unit files (claude seat: no codex export)
    *(f"{MASON_UNIT_DIR}/{n}" for _op, service, timer, codex_only in PI._FLEET_USAGE_UNIT_FILES
      if not codex_only for n in (service, timer)),
    # T-6141: the owner-side tmp-session-prune unit files (mason is a claude seat other than recovery)
    *(f"{MASON_UNIT_DIR}/{n}" for _op, service, timer in PI._TMP_SESSION_PRUNE_UNIT_FILES
      for n in (service, timer)),
    # T-6118 PR-5: the receipt-driven chronicle-prune unit files
    *(f"{MASON_UNIT_DIR}/{n}" for _op, service, timer in PI._CHRONICLE_PRUNE_UNIT_FILES
      for n in (service, timer)),
    f"{MASON_HOME}/.local/state/pantheon/persona-installer/mason",
    # review F2: the persona state root moves per child; overrides/ (ORG-106) stays
    f"{MASON_HOME}/.local/state/pantheon/personas/mason/runtime-controller",
])
#: T-5961: the fleet-usage prune timer is not a U5 wake stem, so a paused seat still runs it
MASON_STOPS = ["pantheon-chronicle-prune@mason.timer", "pantheon-fleet-usage-prune@mason.timer", "pantheon-persona-quota@mason.timer",
               "pantheon-restart-drain.timer", "pantheon-tmp-session-prune@mason.timer",
               "pantheon-runtime-controller@mason.service"]


def test_real_mason_snapshot_is_the_inverse_of_its_bring_up():
    ops = _plan("mason")
    root = R.quarantine_root("suborch1", STAMP)
    assert [op["kind"] for op in ops] == (
        ["harness.decommission-record"] + ["harness.decommission-unit"] * len(MASON_STOPS) + ["harness.decommission-move"] * len(MASON_MOVES)
        + ["harness.decommission-unit", "harness.decommission-commit"])
    assert [op["target"] for op in ops[1:1 + len(MASON_STOPS)]] == MASON_STOPS
    moves = [op["desired"] for op in ops if op["kind"] == "harness.decommission-move"]
    assert [m["source"] for m in moves] == MASON_MOVES
    for m in moves:
        assert m["destination"] == f"{root}/tree/{m['source'][len(MASON_HOME) + 1:]}"
        assert (m["rename"], m["follow_symlinks"]) == ("same-filesystem", "never")
    assert ops[0]["target"] == f"{root}/manifest.json"
    assert ops[-2]["target"] == "suborch1:daemon-reload" and ops[-1]["target"] == "suborch1:decommission"


@pytest.mark.parametrize("persona", DECOMMISSIONABLE)
def test_real_every_seat_plans_and_moves_only_its_own_artifacts(persona):
    account = ACCOUNTS[persona]
    ops = _plan(persona)
    doc = _doc(ops)
    root = R.quarantine_root(account, STAMP)
    assert doc["persona"] == persona and doc["account"] == account and doc["quarantine_root"] == root
    for op in ops:
        assert op["owner"] == account and op["privilege"] == "seat" and op["kind"] in KINDS
        assert op["desired"]["systemd_manager"] == "user" and op["desired"]["account"] == account
        assert op["desired"]["xdg_runtime_dir"] == f"/run/user/{UIDS[account]}"
        if op["kind"] == "harness.decommission-move":
            assert op["desired"]["source"].startswith(f"/home/{account}/")
            assert op["desired"]["destination"].startswith(f"{root}/tree/")
    sources = [op["desired"]["source"] for op in ops if op["kind"] == "harness.decommission-move"]
    assert not [a for a in sources for b in sources if a != b and R.under(a, b)]  # no nested moves
    assert len(set(_ids(ops))) == len(ops)


def test_real_every_installer_kind_and_mkdir_id_is_classified():
    """Fail closed: every installer kind (U1's 36) and every mkdir id of every seat is in the tables."""

    assert set(R.KIND_DISPOSITION) == set(HM._INSTALL_KIND_MAP)
    manifest = PI.Manifest.load(PI.REPO_ROOT / "instance.yaml")
    for role in manifest.roles.values():
        persona = role.get("persona") if isinstance(role, dict) else None
        if persona:
            for op in _installer_plan(persona).operations:
                R.disposition(op.kind)
                if op.kind == "mkdir":
                    R.mkdir_disposition(op.operation_id)


def test_an_unclassified_installer_kind_refuses(monkeypatch):
    monkeypatch.delitem(R.KIND_DISPOSITION, "user-unit-install")
    with pytest.raises(HM.PlanRefused, match=r"\[unclassified-artifact\].*user-unit-install"):
        _plan("mason")


def test_retained_lists_shared_documents_ancestors_and_the_mailbox_dir():
    retained = {r["item"]: r["reason"] for r in _doc(_plan("orchestrator"))["retained"]}
    assert any("claude-mcp-server-registration mailbox" in k for k in retained)
    assert any("seat-claude-settings-env" in k for k in retained)
    assert any("seat-claude-workspace-trust" in k for k in retained)
    assert "/home/post-office/mailbox/orchestrator" in retained
    assert "/home/claude/.config/systemd/user" in retained and "/home/claude/.local/state/pantheon" in retained
    assert "/home/claude/.local/state/pantheon/harness-manager/run-state-manifest.json" in retained


def test_t6118_chronicle_export_dirs_are_retained_with_accurate_reasons():
    for op_id in ("chronicle-export.root", "chronicle-export.seat", "chronicle-export.ingested"):
        assert R.mkdir_disposition(op_id) == R.SHARED
    assert "chronicle-export.seat" in R.MKDIR_RETAIN_REASON
    assert "not yet ingested" in R.MKDIR_RETAIN_REASON["chronicle-export.seat"]
    assert "chronicle-export.root" not in R.MKDIR_RETAIN_REASON  # default reason, unchanged
    retained = {r["item"]: r["reason"] for r in _doc(_plan("orchestrator"))["retained"]}
    seat = [v for k, v in retained.items() if k.startswith("/var/lib/pantheon/chronicle-export/") and "ingested" not in k
            and k != "/var/lib/pantheon/chronicle-export"]
    assert any("not yet ingested" in v for v in seat)
    assert retained.get("/var/lib/pantheon/chronicle-export") == R.DEFAULT_MKDIR_RETAIN_REASON


def test_absent_artifacts_are_surfaced_never_silently_skipped():
    seat = _seat("mason")
    gone = f"{MASON_UNIT_DIR}/pantheon-keep-going@.timer"
    del seat["files"][gone]
    ops = _plan("mason", seat)
    assert gone in _doc(ops)["absent"]
    assert gone not in [op["target"] for op in ops]


# --- ordering and escalation marks -------------------------------------------------------------


def test_order_is_record_timers_services_controller_moves_reload_commit():
    ops = _plan("mason")
    kinds = [op["kind"] for op in ops]
    assert kinds[0] == "harness.decommission-record" and kinds[-1] == "harness.decommission-commit"
    units = [op["target"] for op in ops if op["kind"] == "harness.decommission-unit" and op["desired"]["action"] == "disable-now"]
    assert units == sorted(units, key=lambda u: (u.startswith(R.CONTROLLER_STEM), not u.endswith(".timer"), u))
    controller = next(i for i, op in enumerate(ops) if op["target"].startswith(R.CONTROLLER_STEM))
    first_move = kinds.index("harness.decommission-move")
    assert controller == first_move - 1  # the controller stops LAST, right before the first move
    assert kinds[-2] == "harness.decommission-unit" and ops[-2]["desired"]["action"] == "daemon-reload"


def test_d6_escalation_starts_at_the_controller_stop():
    ops = _plan("mason")
    controller = next(i for i, op in enumerate(ops) if op["target"].startswith(R.CONTROLLER_STEM))
    assert all("escalate_on_halt" not in op["desired"] for op in ops[:controller])
    assert all(op["desired"]["escalate_on_halt"] == "true" for op in ops[controller:])
    undo = ops[controller]
    assert undo["desired"]["undo"] == "daemon-reload-then-enable" and undo["desired"]["escalation_recipient"] == "recovery"
    assert undo["desired"]["requires_restored"].split(",") == MASON_MOVES


def test_d6_a_controller_already_down_escalates_from_the_first_mutation():
    seat = _seat("mason")
    seat["units"]["pantheon-runtime-controller@mason.service"] = dict(DOWN)
    seat["sessions"] = {}
    ops = _plan("mason", seat)
    assert "escalate_on_halt" not in ops[0]["desired"]
    assert all(op["desired"]["escalate_on_halt"] == "true" for op in ops[1:])


def test_the_gate_and_the_operator_anchor_ride_the_first_op_only():
    ops = _plan("mason")
    assert ops[0]["gate"] == GATE and ops[0]["desired"]["anchor"] == ANCHOR
    assert [op for op in ops[1:] if op.get("gate")] == []


# --- refusals (typed) -------------------------------------------------------------------------


@pytest.mark.parametrize("desired, code", [
    ({"stamp": "nope"}, "inputs-refused"),
    ({"source_revision": "abc"}, "inputs-refused"),
    ({"harness": "codex"}, "inputs-refused"),
    ({"anchor": None}, "operator-anchor-required"),
])
def test_desired_refusals(desired, code):
    with pytest.raises(HM.PlanRefused, match=rf"\[{code}\]"):
        _plan("mason", **desired)


@pytest.mark.parametrize("persona", ("mason", "artisan", "specialist", "archivist"))
def test_v_every_seat_needs_a_operator_anchor_not_only_the_ho(persona):
    """Recovery D15 (T-5859) (v): a non-HO seat with a non-operator anchor refuses."""

    for anchor in ("orchestrator:T-5859", "recovery:x", "mason:TG1"):
        with pytest.raises(HM.PlanRefused, match=r"\[operator-anchor-required\].*Recovery D15 \(T-5859\) \(v\)"):
            _plan(persona, anchor=anchor)


@pytest.mark.parametrize("persona", ("recovery", "watchman"))
def test_recovery_tier_refuses(persona):
    with pytest.raises(HM.PlanRefused, match=r"\[role-protected\]"):
        _plan(persona, seat={}, run_state={})


def test_a_managed_root_half_refuses_root_half_pending_naming_the_follow_up(monkeypatch):
    real = HR.identities

    def managed(doc):
        out = real(doc)
        ident = out["engineering-orchestrator-1"]
        out["engineering-orchestrator-1"] = HR.Identity(**{**ident.__dict__, "root_half": "managed"})
        return out

    monkeypatch.setattr(HR, "identities", managed)
    with pytest.raises(HM.PlanRefused, match=r"\[root-half-pending\].*T-5873"):
        _plan("mason")


def test_observe_only_seats_emit_no_root_ops_and_say_so():
    for persona in DECOMMISSIONABLE:
        ops = _plan(persona)
        assert not [op for op in ops if op["kind"] in F.FAMILIES or op["privilege"] != "seat"]
        doc = _doc(ops)
        assert doc["root_half"] == "observe-only" and doc["root_ops"].startswith("none: observe-only")
        assert all(op["provenance"]["root_half"] == "observe-only" for op in ops)


def test_scope_and_observation_refusals():
    request = _request("mason")
    with pytest.raises(HM.PlanRefused, match=r"\[scope-refused\]"):
        HM.plan_remove(dict(request, scope="system"))
    request = _request("mason")
    del request["observed"][HM.DECOMMISSION_HANDLER]
    with pytest.raises(HM.PlanRefused, match=r"\[observation-missing\]"):
        HM.plan_remove(request)
    for field, value, code in (("target_manifest_sha256", "0" * 64, "manifest-mismatch"),
                               ("user_bus", False, "user-bus-unreachable"),
                               ("uid", None, "observation-missing"),
                               ("files", None, "observation-missing")):
        seat = _seat("mason")
        seat[field] = value
        with pytest.raises(HM.PlanRefused, match=rf"\[{code}\]"):
            _plan("mason", seat)


@pytest.mark.parametrize("mutate", [
    lambda rs: rs.update(run_state="running"),
    lambda rs: rs.update(manifest=None),
    lambda rs: rs.update(seat_state=None),
    lambda rs: rs["seat_state"].update(pause_id="other"),
    lambda rs: rs["seat_state"].update(state="halted", phase="halted"),
], ids=["running", "no-manifest", "no-seat-state", "pause-id-mismatch", "halted-pause"])
def test_d3_the_seat_must_carry_a_committed_u5_pause(mutate):
    rs = _paused("mason")
    mutate(rs)
    with pytest.raises(HM.PlanRefused, match=r"\[seat-not-paused\]"):
        _plan("mason", run_state=rs)


def test_transition_in_flight_refuses():
    request = _request("mason")
    request["observed"][HM.HANDLER] = {"seat": {"transition": {"phase": "preparing"}}}
    with pytest.raises(HM.PlanRefused, match=r"\[transition-in-flight\]"):
        HM.plan_remove(request)


def test_quarantine_exists_refuses_choose_a_fresh_stamp():
    seat = _seat("mason")
    seat["files"][R.record_path(R.quarantine_root("suborch1", STAMP))] = _file("old")
    with pytest.raises(HM.PlanRefused, match=r"\[quarantine-exists\]"):
        _plan("mason", seat)
    assert _plan("mason", seat, stamp="20261102T060000Z")


def test_a_symlinked_source_refuses():
    seat = _seat("mason")
    seat["files"][f"{MASON_HOME}/.local/state/pantheon/personas/mason"] = {"kind": "symlink"}
    with pytest.raises(HM.PlanRefused, match=r"\[source-symlink\]"):
        _plan("mason", seat)


@pytest.mark.parametrize("fields", [
    {"UnitFileState": "masked", "ActiveState": "inactive"},
    {"UnitFileState": "enabled", "ActiveState": "inactive"},
    {"UnitFileState": "disabled", "ActiveState": "activating"},
])
def test_a_unit_neither_up_nor_settled_refuses(fields):
    seat = _seat("mason")
    seat["units"]["pantheon-persona-quota@mason.timer"] = dict(UP, **fields)
    with pytest.raises(HM.PlanRefused, match=r"\[unit-state-unfaithful\]"):
        _plan("mason", seat)


def test_a_session_outside_the_runtime_scope_refuses():
    seat = _seat("mason")
    seat["sessions"] = {_session("mason"): {"scope": "app-other.scope"}}
    with pytest.raises(HM.PlanRefused, match=r"\[session-unowned\]"):
        _plan("mason", seat)
    seat = _seat("mason")
    seat["units"]["pantheon-runtime-controller@mason.service"] = dict(DOWN)  # nothing would end it
    with pytest.raises(HM.PlanRefused, match=r"\[session-unowned\]"):
        _plan("mason", seat)


def test_x_a_live_peer_reference_inside_a_quarantined_tree_refuses():
    """Recovery D15 (T-5859) (x), plan time."""

    state = f"{MASON_HOME}/.local/state/pantheon/personas/mason/runtime-controller"
    seat = _seat("mason")
    seat["peer_units"]["pantheon-restart-drain-peer.service"] = {
        "account": "claude-fallback", "ActiveState": "active",
        "ExecStart": f"/usr/bin/python3 {state}/runtime.py --x", "WorkingDirectory": "/home/claude-fallback"}
    with pytest.raises(HM.PlanRefused, match=r"\[peer-reference-live\].*claude-fallback:pantheon-restart-drain-peer.service"):
        _plan("mason", seat)
    seat["peer_units"]["pantheon-restart-drain-peer.service"].update(ActiveState="inactive", UnitFileState="static")
    assert _plan("mason", seat)  # inactive, not enabled, not failed, no live timer fires it
    seat["peer_units"]["w.service"] = {"account": "codex", "ActiveState": "active", "ExecStart": "/usr/bin/true",
                                       "WorkingDirectory": f"{MASON_HOME}/.local/state/pantheon/persona-installer/mason"}
    with pytest.raises(HM.PlanRefused, match=r"\[peer-reference-live\].*codex:w.service"):
        _plan("mason", seat)


_STATE = f"{MASON_HOME}/.local/state/pantheon/personas/mason/runtime-controller"


@pytest.mark.parametrize("units", [
    # F1(a): an active peer TIMER fires an inactive oneshot that executes inside a moved tree
    {"peer-sweep.timer": {"account": "claude-fallback", "ActiveState": "active", "UnitFileState": "enabled"},
     "peer-sweep.service": {"account": "claude-fallback", "ActiveState": "inactive", "UnitFileState": "static",
                            "ExecStart": f"/usr/bin/python3 {_STATE}/x.py"}},
    # F1(a): the timer names its unit explicitly (Unit=)
    {"t.timer": {"account": "codex", "ActiveState": "active", "Unit": "other.service"},
     "other.service": {"account": "codex", "ActiveState": "inactive", "ExecStart": f"{_STATE}/run"}},
    # F1(b): failed (a Restart= may be pending) / enabled but inactive
    {"p.service": {"account": "codex", "ActiveState": "failed", "ExecStart": f"/usr/bin/python3 {_STATE}/x.py"}},
    {"p.service": {"account": "codex", "ActiveState": "inactive", "UnitFileState": "enabled", "ExecStart": f"{_STATE}/x"}},
    # F1(c): ~ and %h expand to the unit's own home; shell-joined forms are tokenized
    {"p.service": {"account": "suborch1", "ActiveState": "active",
                   "ExecStart": "/usr/bin/python3 ~/.local/state/pantheon/personas/mason/runtime-controller/x.py"}},
    {"p.service": {"account": "suborch1", "ActiveState": "active", "WorkingDirectory": "%h/.local/state/pantheon/persona-installer/mason"}},
    {"p.service": {"account": "codex", "ActiveState": "active", "ExecStart": f"/bin/sh -c 'cd {_STATE}&&./run'"}},
    {"p.service": {"account": "codex", "ActiveState": "active", "ExecStart": f"/bin/sh -c \"(cd {_STATE};./run)|tee\""}},
], ids=["timer-oneshot", "timer-unit-field", "failed", "enabled-inactive", "tilde", "percent-h", "sh-c-quote", "sh-c-paren"])
def test_f1_live_peer_reference_gaps_refuse(units):
    seat = _seat("mason")
    seat["peer_units"].update(copy.deepcopy(units))
    with pytest.raises(HM.PlanRefused, match=r"\[peer-reference-live\]"):
        _plan("mason", seat)


def test_f1_tilde_of_another_account_is_that_accounts_home():
    seat = _seat("mason")
    seat["peer_units"]["p.service"] = {"account": "codex", "ActiveState": "active",
                                       "ExecStart": "/usr/bin/python3 ~/.local/state/pantheon/personas/mason/runtime-controller/x.py"}
    assert _plan("mason", seat)  # /home/codex/..., not the decommissioned seat's tree


def test_f4_a_live_seat_unit_outside_the_plan_needs_its_execstart_observed():
    seat = _seat("mason")
    seat["units"]["exocortex-thing.service"] = dict(UP)
    with pytest.raises(HM.PlanRefused, match=r"\[observation-missing\].*exocortex-thing.service"):
        _plan("mason", seat)
    seat["units"]["exocortex-thing.service"]["ExecStart"] = "/usr/bin/python3 /home/suborch1/elsewhere/x.py"
    assert _plan("mason", seat)


def test_f4_a_live_seat_unit_executing_from_a_moved_tree_refuses():
    seat = _seat("mason")
    seat["units"]["exocortex-thing.service"] = dict(UP, ExecStart="/usr/bin/python3 %h/.local/state/pantheon/persona-installer/mason/x.py")
    with pytest.raises(HM.PlanRefused, match=r"\[seat-unit-inside-quarantine\].*suborch1:exocortex-thing.service"):
        _plan("mason", seat)


def test_f5_a_missing_runtime_observation_refuses():
    request = _request("mason")
    del request["observed"][HM.HANDLER]
    with pytest.raises(HM.PlanRefused, match=r"\[observation-missing\].*harness-manager-seat"):
        HM.plan_remove(request)


def test_f2_org106_records_are_retained_in_place():
    ops = _plan("mason")
    doc = _doc(ops)
    overrides = f"{MASON_HOME}/.local/state/pantheon/personas/mason/overrides"
    assert {"item": overrides, "reason": "ORG-106 record keeps a decommissioned seat's intake blocked"} in doc["retained"]
    sources = [op["desired"]["source"] for op in ops if op["kind"] == "harness.decommission-move"]
    assert not [s for s in sources if R.under(overrides, s) or R.under(s, overrides)]
    sim = _sim("mason")
    _run(sim, ops)
    files = _state(sim, "mason")["files"]
    assert files[f"{overrides}/seat-state.json"] == _seat("mason")["files"][f"{overrides}/seat-state.json"]
    assert files[f"{overrides}/holds.json"] == _seat("mason")["files"][f"{overrides}/holds.json"]
    assert not [op for op in ops if op["kind"] == "harness.decommission-move" and R.under(op["target"], overrides)]


def test_r2_5_absent_overrides_is_surfaced_absent_not_retained():
    seat = _seat("mason")
    overrides = f"{MASON_HOME}/.local/state/pantheon/personas/mason/overrides"
    seat["files"] = {p: e for p, e in seat["files"].items() if not R.under(p, overrides)}
    doc = _doc(_plan("mason", seat))
    assert overrides in doc["absent"]
    assert not [r for r in doc["retained"] if r["item"] == overrides]


# --- review round 2: R2-1 .. R2-6 ------------------------------------------------------------


@pytest.mark.parametrize("spec", [
    {"LoadState": "loaded", "UnitFileState": "enabled-runtime", "ActiveState": "inactive"},
    {"LoadState": "loaded", "UnitFileState": "linked", "ActiveState": "inactive"},
    {"LoadState": "loaded", "UnitFileState": "linked-runtime", "ActiveState": "inactive"},
    {"LoadState": "loaded", "UnitFileState": "alias", "ActiveState": "inactive"},
    {"LoadState": "not-found", "UnitFileState": "", "ActiveState": "active"},
    {"LoadState": "loaded", "UnitFileState": "disabled", "ActiveState": "failed"},
], ids=["enabled-runtime", "linked", "linked-runtime", "alias", "not-found-but-active", "failed"])
def test_r2_1_every_armed_coverage_state_refuses(spec):
    seat = _seat("mason")
    seat["peer_units"]["pantheon-context-fill-monitor@mason.timer"] = dict(spec, account="claude-fallback")
    with pytest.raises(HM.PlanRefused, match=r"\[peer-coverage-armed\].*pantheon-context-fill-monitor@mason\.timer"):
        _plan("mason", seat)
    ops = _plan("mason")  # and the apply-time re-check on rm-record refuses the same state
    sim = _sim("mason", seat)
    assert "peer-coverage-armed" in sim.preflight(_inst(ops[0])).detail


def test_r2_1_masked_or_proven_absent_coverage_passes():
    for spec in ({"LoadState": "masked", "UnitFileState": "masked", "ActiveState": "inactive"},
                 {"LoadState": "not-found", "UnitFileState": "", "ActiveState": "inactive"}):
        seat = _seat("mason")
        seat["peer_units"]["pantheon-context-fill-monitor@mason.timer"] = dict(spec, account="claude-fallback")
        assert _plan("mason", seat)


def test_r2_2_coverage_re_armed_before_the_controller_stop_refuses_there():
    ops = _plan("mason")
    controller = next(op for op in ops if op["target"].startswith(R.CONTROLLER_STEM))
    assert controller["desired"]["peer_coverage"] == ops[0]["desired"]["peer_coverage"]
    sim = _sim("mason")
    _run(sim, ops[: ops.index(controller)])  # rm-record and the timer stops went through
    state = sim.dump_state()
    state["seats"]["suborch1"]["peer_units"]["pantheon-context-fill-monitor@mason.timer"] = dict(UP, account="claude-fallback")
    sim.load_state(state)
    assert sim.preflight(_inst(controller)).ok  # escalating: refused at the attempt
    with pytest.raises(HMH.EscalatingHalt, match="peer-coverage-armed"):
        sim.execute(_inst(controller))
    assert _state(sim, "mason")["units"]["pantheon-runtime-controller@mason.service"]["ActiveState"] == "active"


def test_r2_3_a_peer_live_at_the_state_root_after_the_re_plan_refuses_at_the_move():
    ops = _plan("mason")
    root = f"{MASON_HOME}/.local/state/pantheon/personas/mason"
    move = next(op for op in ops if op["kind"] == "harness.decommission-move" and R.under(op["target"], root))
    assert move["desired"]["referenced_root"] == root and move["desired"]["referenced_exclude"] == f"{root}/overrides"
    sim = _sim("mason")
    _run(sim, ops[: ops.index(move)])
    state = sim.dump_state()
    state["seats"]["suborch1"]["peer_units"]["late.service"] = {"account": "claude-fallback", "ActiveState": "active",
                                                                "ExecStart": "/usr/bin/x", "WorkingDirectory": root}
    sim.load_state(state)
    with pytest.raises(HMH.EscalatingHalt, match="live peer unit"):
        sim.execute(_inst(move))
    assert move["target"] in _state(sim, "mason")["files"]


def test_r2_4_a_unit_running_from_the_retained_overrides_is_admitted():
    overrides = f"{MASON_HOME}/.local/state/pantheon/personas/mason/overrides"
    seat = _seat("mason")
    seat["peer_units"]["reader.service"] = {"account": "claude-fallback", "ActiveState": "active",
                                            "ExecStart": "/usr/bin/true", "WorkingDirectory": overrides}
    seat["units"]["own.service"] = dict(UP, ExecStart=f"/usr/bin/cat {overrides}/seat-state.json")
    ops = _plan("mason", seat)
    sim = _sim("mason", seat)
    _run(sim, ops)  # and the apply-time re-checks admit it too


def test_r2_6_the_fleet_wide_parity_remediation_is_named_as_residue():
    cross = _doc(_plan("mason"))["cross_seat_residue"]
    [entry] = [r for r in cross if r.startswith("tools/parity_remediate.py:")]
    assert "pantheon-parity-remediate" in entry and "enable --now" in entry and "not an @P unit" in entry
    assert "guarded by (x3)" in entry


# --- Recovery D15 (T-5859) (x3): fleet remediation must be vetoed for the seat --------------------


def _remediation(maintenance=None, record=None, observed_at=NOW):
    return {"observed_at": observed_at, "maintenance": maintenance, "seat_state": record}


def test_x3_a_paused_intake_seat_without_seat_maintenance_refuses():
    seat = _seat("mason")
    seat["remediation"] = _remediation(None, _record("paused-intake"))
    with pytest.raises(HM.PlanRefused, match=r"\[remediation-not-vetoed\].*arm seat-maintenance for mason"):
        _plan("mason", seat)


@pytest.mark.parametrize("maintenance", [
    {"expires": NOW},                       # expires == now: not armed (expires > now is the rule)
    {"expires": "2026-11-02T04:59:59Z"},    # expired
    {"expires": "tomorrow"},                # malformed reads not armed
    {"by": "recovery"},                         # no expires
])
def test_x3_expired_or_malformed_maintenance_does_not_veto(maintenance):
    seat = _seat("mason")
    seat["remediation"] = _remediation(maintenance, _record("paused-intake"))
    with pytest.raises(HM.PlanRefused, match=r"\[remediation-not-vetoed\]"):
        _plan("mason", seat)


def test_x3_seat_maintenance_armed_passes():
    seat = _seat("mason")
    seat["remediation"] = _remediation({"expires": "2026-11-02T05:00:01Z"}, _record("paused-intake"))
    assert _plan("mason", seat)


def test_x3_deep_sleep_passes_and_an_expired_one_does_not():
    seat = _seat("mason")
    seat["remediation"] = _remediation(None, _record("deep-sleep"))
    assert _plan("mason", seat)
    seat["remediation"] = _remediation(None, _record("deep-sleep", indefinite=False, expires="2026-11-02T04:00:00Z"))
    with pytest.raises(HM.PlanRefused, match=r"\[remediation-not-vetoed\]"):
        _plan("mason", seat)


@pytest.mark.parametrize("observed", [None, {}, {"observed_at": NOW, "maintenance": None},
                                      {"observed_at": "now", "maintenance": {"expires": "2026-11-02T09:00:00Z"}, "seat_state": None}],
                         ids=["absent", "empty", "no-seat-state-key", "bad-clock"])
def test_x3_unobserved_refuses_could_not_ask(observed):
    seat = _seat("mason")
    seat["remediation"] = observed
    with pytest.raises(HM.PlanRefused, match=r"\[remediation-not-vetoed\].*could not ask"):
        _plan("mason", seat)


_PARITY_CASES = [
    ({"expires": "2026-11-02T09:00:00Z"}, None),
    ({"expires": "2026-11-02T01:00:00Z"}, None),
    ({"expires": "garbage"}, None),
    (None, "paused-intake"),
    (None, "deep-sleep"),
    (None, "deep-sleep-expired"),
    (None, None),
]


@pytest.mark.parametrize("maintenance, state", _PARITY_CASES, ids=[
    "maintenance-armed", "maintenance-expired", "maintenance-garbage", "paused-intake", "deep-sleep",
    "deep-sleep-expired", "nothing"])
def test_x3_mirrors_the_live_seat_state_reader(tmp_path, maintenance, state):
    """Review N3: the mirror agrees with the REAL ``seat_state.effective(...).remediation_vetoed``
    over a temporary state root and maintenance registry (only this test does I/O; the
    planner and the handler stay pure)."""

    from datetime import datetime, timezone

    from tools import harness_state, notify_policy, seat_state

    record = None
    if state == "deep-sleep-expired":
        record = _record("deep-sleep", indefinite=False, expires="2026-11-02T04:00:00Z")
    elif state is not None:
        record = _record(state)
    registry = tmp_path / "maintenance.json"
    registry.write_text(json.dumps({"schema": notify_policy.MAINTENANCE_SCHEMA,
                                    "maintenance": {"mason": maintenance} if maintenance else {}}), encoding="utf-8")
    if record is not None:
        path = harness_state.seat_state_path("mason", root=tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(record), encoding="utf-8")
    now = datetime(2026, 11, 2, 5, 0, 0, tzinfo=timezone.utc)
    assert NOW == now.strftime("%Y-%m-%dT%H:%M:%SZ")
    live = seat_state.effective("mason", now=now, state_root=tmp_path, maintenance_registry=registry,
                                oom_holds_path=tmp_path / "no-oom-holds").remediation_vetoed
    assert R.remediation_vetoed(_remediation(maintenance, record), HM.dc_record_vetoes) is bool(live)


def test_x3_un_vetoed_between_plan_and_apply_refuses_at_rm_record_and_at_the_controller_stop():
    ops = _plan("mason")
    assert ops[0]["desired"]["remediation_veto"] == "required"
    controller = next(op for op in ops if op["target"].startswith(R.CONTROLLER_STEM))
    assert controller["desired"]["remediation_veto"] == "required"
    sim = _sim("mason")
    state = sim.dump_state()
    state["seats"]["suborch1"]["remediation"]["maintenance"] = None  # seat-maintenance lapsed after plan
    sim.load_state(state)
    result = sim.preflight(_inst(ops[0]))
    assert not result.ok and "remediation-not-vetoed" in result.detail
    sim = _sim("mason")  # vetoed through rm-record and the timer stops, then lapsed before the controller stop
    _run(sim, ops[: ops.index(controller)])
    state = sim.dump_state()
    state["seats"]["suborch1"]["remediation"]["maintenance"] = {"expires": "2026-11-02T04:00:00Z"}
    sim.load_state(state)
    with pytest.raises(HMH.EscalatingHalt, match="remediation-not-vetoed"):
        sim.execute(_inst(controller))


def test_f2_an_empty_persona_state_root_is_surfaced_absent():
    seat = _seat("mason")
    root = f"{MASON_HOME}/.local/state/pantheon/personas/mason"
    seat["files"] = {p: e for p, e in seat["files"].items() if not R.under(p, root)}
    assert root in _doc(_plan("mason", seat))["absent"]


def test_x_a_live_peer_reference_refuses_the_move_at_apply():
    """Recovery D15 (T-5859) (x), apply time: the peer unit appears after plan; the move op
    (escalating, after the controller stop) halts with EscalatingHalt before it moves."""

    ops = _plan("mason")
    sim = _sim("mason")
    target = next(op for op in ops if op["kind"] == "harness.decommission-move")
    _run(sim, ops[: ops.index(target)])
    state = sim.dump_state()
    state["seats"]["suborch1"]["peer_units"]["late.service"] = {
        "account": "claude-fallback", "ActiveState": "active", "ExecStart": f"{target['desired']['source']} --run"}
    sim.load_state(state)
    with pytest.raises(HMH.EscalatingHalt, match="live peer unit"):
        sim.execute(_inst(target))
    assert target["desired"]["source"] in sim.dump_state()["seats"]["suborch1"]["files"]


# --- Recovery D15 (T-5859) (x2): Recovery-side coverage of the persona must be disarmed ------------------


def test_x2_coverage_units_are_the_monitors_plus_declared_peer_units():
    assert HM.plan_remove  # the planner reads the same expected-units.yaml
    monitors = sorted(f"{m}@specialist.{s}" for m in _MONITORS for s in ("service", "timer"))
    import yaml

    doc = yaml.safe_load((REPO_ROOT / "tools" / "systemd" / "expected-units.yaml").read_text(encoding="utf-8"))
    assert R.peer_coverage_units(doc, "specialist") == sorted(set(monitors) | set(_declared_coverage("specialist")))
    assert "pantheon-context-fill-monitor@specialist.timer" in _declared_coverage("specialist")
    assert R.peer_coverage_units(None, "mason") == sorted(f"{m}@mason.{s}" for m in _MONITORS for s in ("service", "timer"))
    assert _doc(_plan("mason"))["peer_coverage"] == R.peer_coverage_units(doc, "mason")


def test_x2_an_enabled_ctxfill_timer_refuses():
    """Recovery's required test: an enabled pantheon-context-fill-monitor@P timer -> typed refusal."""

    seat = _seat("mason")
    seat["peer_units"]["pantheon-context-fill-monitor@mason.timer"] = dict(UP, account="claude-fallback")
    with pytest.raises(HM.PlanRefused, match=r"\[peer-coverage-armed\].*claude-fallback:pantheon-context-fill-monitor@mason\.timer"):
        _plan("mason", seat)


def test_x2_an_active_but_disabled_monitor_refuses():
    seat = _seat("specialist")
    seat["peer_units"]["pantheon-context-fill-monitor@specialist.timer"] = dict(UP, UnitFileState="disabled", account="claude-fallback")
    with pytest.raises(HM.PlanRefused, match=r"\[peer-coverage-armed\].*pantheon-context-fill-monitor@specialist\.timer \(disabled/active\)"):
        _plan("specialist", seat)


def test_x2_all_coverage_disabled_and_inactive_passes():
    seat = _seat("specialist")
    for unit in R.peer_coverage_units(None, "specialist") + _declared_coverage("specialist"):
        seat["peer_units"][unit] = dict(DOWN, account="claude-fallback")
    assert _plan("specialist", seat)[0]["kind"] == "harness.decommission-record"


def test_x2_undeclared_coverage_without_proof_of_absence_refuses_could_not_ask():
    seat = _seat("mason")  # mason has no declared Recovery monitor: the fixture proves each absent
    del seat["peer_units"]["pantheon-activity-monitor@mason.service"]
    with pytest.raises(HM.PlanRefused, match=r"\[peer-coverage-could-not-ask\].*pantheon-activity-monitor@mason\.service"):
        _plan("mason", seat)
    seat = _seat("specialist")  # declared but unobserved also cannot be told disarmed
    del seat["peer_units"]["pantheon-context-fill-monitor@specialist.timer"]
    with pytest.raises(HM.PlanRefused, match=r"\[peer-coverage-could-not-ask\]"):
        _plan("specialist", seat)


def test_x2_the_manifest_write_re_checks_coverage_at_apply():
    ops = _plan("mason")
    sim = _sim("mason")
    state = sim.dump_state()
    state["seats"]["suborch1"]["peer_units"]["pantheon-context-fill-monitor@mason.timer"] = dict(UP, account="claude-fallback")
    sim.load_state(state)
    result = sim.preflight(_inst(ops[0]))
    assert not result.ok and "peer-coverage-armed" in result.detail


def test_engine_x2_coverage_re_armed_between_plan_and_apply_refuses_before_any_mutation(tmp_path):
    eng = _engine(tmp_path)
    plan = eng.plan("remove", SCOPE, CID, desired=DESIRED)
    seat = _dc_state(eng)
    peer = dict(seat["peer_units"])
    peer["pantheon-context-fill-monitor@mason.timer"] = dict(UP, account="claude-fallback")
    _set_seat(eng, peer_units=peer)
    before = _dc_state(eng)
    with pytest.raises(EngineRefusal) as info:
        eng.apply("remove", SCOPE, CID, plan_digest=plan.digest, accept_gates=frozenset({GATE}), desired=DESIRED)
    # apply re-plans from the current observation first (executor.apply -> _plan), so the
    # refusal lands there; the rm-record preflight re-check (the sim test above) closes the
    # remaining window between that re-plan and the first mutation
    assert info.value.code == "plan-refused" and "[peer-coverage-armed]" in info.value.message
    assert _dc_state(eng) == before  # nothing moved, nothing written


# --- Recovery D15 (T-5859) (w) / (y) / (z): residue, secrets and sizes, as data ----------------------


def test_w_routing_residue_lists_every_routable_place():
    doc = _doc(_plan("mason"))
    routing = doc["routing_residue"]
    assert "instance.yaml: roles.engineering-orchestrator-1 binds persona 'mason' " \
           "(the instance role binding (agent_role_map derives from it))" in routing
    assert any(r.startswith("persona_roles.yaml: personas.mason role_ids ['engineering_orchestrator']") for r in routing)
    assert any(r.startswith("tools/agent_role_map.py: agent_role_map fallback bindings names 'mason'") for r in routing)
    assert any(r.startswith("tools/inbox_consumer.py: inbox_consumer delivery targets names 'mason'") for r in routing)
    assert any(r.startswith("repos.yaml: role 'engineering-orchestrator-1' is granted") for r in routing)
    assert doc["outcome"] == "decommissioned-with-residue"
    commit = _plan("mason")[-1]
    assert commit["desired"]["outcome"] == "decommissioned-with-residue"
    residue = [c for c in R.decode(commit["desired"]["postconditions"]) if c["check"] == "residue"]
    assert residue and residue[0]["advisory"] is True and "instance.yaml" in residue[0]["params"]["residue"]


def test_x_cross_seat_residue_reports_declared_and_undeclared_peer_units():
    cross = _doc(_plan("mason"))["cross_seat_residue"]
    for stem in R.RECOVERY_MONITOR_STEMS:
        assert (f"tools/systemd/expected-units.yaml: {stem}@mason.timer is not declared; "
                "check the Recovery-side monitor by hand") in cross
    assert any(r.startswith("tools/verify_startup.sh: verify_startup agent lists names 'mason'") for r in cross)
    specialist = _doc(_plan("specialist"))["cross_seat_residue"]
    assert ("tools/systemd/expected-units.yaml: recovery declares pantheon-context-fill-monitor@specialist.timer "
            "(stays armed; cross-account, by hand)") in specialist


def test_residue_sources_absent_are_named_not_assumed_absent():
    docs = {path: None for path in R.SOURCE_PATHS}
    out = R.routing_residue(docs, persona="mason", role_key="engineering-orchestrator-1")
    assert out and all("not declared (absent at the plan revision)" in r for r in out)


def test_y_secrets_retained_by_path_only():
    doc = _doc(_plan("orchestrator"))
    secrets = {s["path"]: s for s in doc["secrets_retained"]}
    assert set(secrets) >= {"/home/claude/gh_token.env", "/home/claude/.claude/.credentials.json",
                            "/home/claude/.claude/channels/telegram/.env"}
    assert all(set(s) <= {"path", "source", "quarantined_to"} for s in doc["secrets_retained"])  # never content
    codex = {s["path"] for s in _doc(_plan("codex"))["secrets_retained"]}
    assert {"/home/codex/.codex/auth.json", "/home/codex/.config/codex-telegram/.env"} <= codex
    specialist = _doc(_plan("specialist"))
    assert any("names no path" in u for u in specialist["secrets_underivable"])  # "SSH (git@github.com ...)" flagged


def test_y_a_secret_inside_a_quarantined_tree_names_where_it_went():
    entries, _flags = R.secrets_retained(
        account="claude", persona="orchestrator", role={"harness": "claude-code"}, bridge={"credential_env": ".config/codex-telegram/.env"},
        moves=[("/home/claude/.config/codex-telegram", "/q/tree/.config/codex-telegram")])
    moved = next(e for e in entries if e["path"] == "/home/claude/.config/codex-telegram/.env")
    assert moved["quarantined_to"] == "/q/tree/.config/codex-telegram/.env"


def test_z_each_quarantined_tree_records_its_byte_size():
    seat = _seat("mason")
    tree = f"{MASON_HOME}/.local/state/pantheon/personas/mason/runtime-controller"
    seat["files"][f"{tree}/big.log"] = _file("big", 4096)
    ops = _plan("mason", seat)
    moves = {m["source"]: m for m in _doc(ops)["moves"]}
    assert moves[tree]["bytes"] == 4096 + 222
    assert moves[f"{MASON_UNIT_DIR}/pantheon-persona-consume@.timer"]["bytes"] == 100
    op = next(op for op in ops if op["target"] == tree)
    assert op["desired"]["bytes"] == str(4096 + 222)


# --- the simulated handler: apply, verify, rollback, no delete --------------------------------


def test_sim_apply_verifies_every_postcondition_and_quarantines_everything():
    ops = _plan("mason")
    sim = _sim("mason")
    _run(sim, ops)
    state = _state(sim, "mason")
    root = R.quarantine_root("suborch1", STAMP)
    for source in MASON_MOVES:
        assert not R.subtree(state["files"], source)
        assert R.subtree(state["files"], R.destination(root, "suborch1", source))
    assert state["files"][R.record_path(root)]["document"]["status"] == "committed"
    assert all(state["units"][u]["LoadState"] == "not-found" for u in MASON_STOPS)
    assert R.RUNTIME_SCOPE_UNIT not in state["units"] and state["sessions"] == {}
    assert state["units"]["dbus.service"] == _seat("mason")["units"]["dbus.service"]  # untouched
    # review F2: the ORG-106 records stay where seat_state.effective_for reads them
    seat_state = f"{MASON_HOME}/.local/state/pantheon/personas/mason/overrides/seat-state.json"
    assert state["files"][seat_state] == _seat("mason")["files"][seat_state]


def test_sim_every_inverse_round_trips_and_the_manifest_is_never_deleted():
    original = _seat("mason")
    ops = _plan("mason")
    sim = _sim("mason", copy.deepcopy(original))
    mutations = _run(sim, ops)
    for mutation in reversed(mutations):
        assert sim.can_rollback(mutation).ok
        sim.rollback_mutation(mutation)
    record = R.record_path(R.quarantine_root("suborch1", STAMP))
    assert _undone(_state(sim, "mason"), record) == original


@pytest.mark.parametrize("cut", [*range(1, 8), -2, -1, 0],
                         ids=[*map(str, range(1, 8)), "before-rm-reload", "through-rm-reload", "through-rm-commit"])
def test_sim_partial_apply_rolls_back_to_the_original(cut):
    """Applied prefixes, including the cut points at rm-reload and rm-commit (review F6)."""

    original = _seat("mason")
    ops = _plan("mason")
    assert [op["operation_id"] for op in ops[-2:]] == ["rm-reload", "rm-commit"]
    cut = cut if cut > 0 else len(ops) + cut
    sim = _sim("mason", copy.deepcopy(original))
    mutations = _run(sim, ops[:cut])
    for mutation in reversed(mutations):
        sim.rollback_mutation(mutation)
    assert _undone(_state(sim, "mason"), R.record_path(R.quarantine_root("suborch1", STAMP))) == original


def test_sim_refuses_foreign_managers_paths_outside_home_and_drift():
    ops = _plan("mason")
    sim = _sim("mason")
    bad = copy.deepcopy(ops[1])
    bad["desired"]["xdg_runtime_dir"] = "/run/user/0"
    assert not sim.preflight(_inst(bad)).ok
    move = copy.deepcopy(next(op for op in ops if op["kind"] == "harness.decommission-move"))
    move["desired"].pop("escalate_on_halt")
    move["desired"]["destination"] = "/home/claude/stolen"
    assert "outside the seat's own home" in sim.preflight(_inst(move)).detail
    move = copy.deepcopy(next(op for op in ops if op["kind"] == "harness.decommission-move"))
    move["desired"].pop("escalate_on_halt")
    move["precondition"]["source_sha256"] = "0" * 64
    assert "moved from its precondition" in sim.preflight(_inst(move)).detail


def _sim_source():
    text = HANDLERS.read_text(encoding="utf-8")
    [node] = [n for n in ast.parse(text).body if isinstance(n, ast.ClassDef) and n.name == "HarnessDecommissionSim"]
    return ast.get_source_segment(text, node)


def _function_sources():
    tree = ast.parse(PLANNER.read_text(encoding="utf-8"))
    names = {"plan_remove", "_dc_op", "_dc_id", "_dc_paused", "_dc_inventory", "_dc_docs", "_assert_dc_scoped", "_refuse",
             "_dc_carve_out_records", "dc_record_vetoes"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes} == names
    return [ast.get_source_segment(PLANNER.read_text(encoding="utf-8"), n) for n in nodes]


#: Deletion- or truncation-shaped calls, modules and strings no U3 code may carry
#: (QUARANTINE, NEVER DELETE). Review F3 added the write/truncate forms, ``del`` and
#: ``__import__``.
_DELETE_CALLS = {"unlink", "remove", "rmdir", "rmtree", "removedirs", "truncate", "rename_and_delete", "shred", "purge",
                 "write_text", "write_bytes", "__import__"}
_DELETE_MODULES = ("os", "shutil", "subprocess", "pathlib")
_DELETE_STRINGS = ("userdel", "deluser", "rm -", "rm\t", "--purge", "shred ", "rmdir", "unlink")
_WRITE_MODES = ("w", "a", "x", "+")


def _delete_hits(source: str) -> list[str]:
    """ONE scan, used by the pin and by its self-test: every deletion-shaped construct in ``source``."""

    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _DELETE_CALLS:
                hits.append(ast.unparse(node))
            if name == "open":  # open(p, 'w' | 'a' | 'x' | '+'), positional or mode=
                modes = [a for a in node.args[1:2]] + [k.value for k in node.keywords if k.arg == "mode"]
                if any(not isinstance(m, ast.Constant) or any(c in str(m.value) for c in _WRITE_MODES) for m in modes):
                    hits.append(ast.unparse(node))
        elif isinstance(node, ast.Delete):
            hits.append(ast.unparse(node))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
            hits += [m for m in mods if m.split(".")[0] in _DELETE_MODULES]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            hits += [node.value for s in _DELETE_STRINGS if s in node.value]
        elif isinstance(node, ast.Name) and node.id == "__import__":
            hits.append("__import__")
    return hits


def test_no_delete_anywhere_in_the_new_code():
    for source in (HELPER.read_text(encoding="utf-8"), _sim_source(), *_function_sources()):
        assert _delete_hits(source) == []


@pytest.mark.parametrize("planted", [
    "import shutil\nshutil.rmtree(p)\n", "p.unlink()\n", "x = 'userdel -r seat'\n", "import os\n",
    "open(p, 'w').close()\n", "open(p, mode='a')\n", "open(p, 'x')\n", "open(p, m)\n",
    "p.write_text('')\n", "p.write_bytes(b'')\n", "del units[k]\n", "del files[p], x\n",
    "__import__('os').replace(p, p)\n", "f = __import__\n",
])
def test_the_no_delete_scan_catches_a_planted_deletion(planted):
    assert _delete_hits(planted), planted
    assert _delete_hits("open(p)\nopen(p, 'r')\nd.pop(k, None)\n") == []  # reads and a cell vacate are not deletions


# --- engine tier (tmp checkout, simulated host) -------------------------------------------------

SCOPE = "seat-suborch1"


def _engine(tmp_path, seat=None, *, gate_class="operator"):
    repo = GF1._repo(tmp_path, gate_class=gate_class)
    eng = GF1._engine(tmp_path, repo, deps=False)
    eng.backend.handler("harness.decommission-move").load_state({"seats": {"suborch1": seat or _seat("mason")}})
    eng.backend.handler("harness.run-state-unit").load_state({"seats": {"suborch1": _paused("mason")}})
    eng.backend.save()
    return eng


def _dc_state(eng):
    seat = eng.backend.handler("harness.decommission-move").dump_state()["seats"]["suborch1"]
    return {k: v for k, v in seat.items() if k not in ("writers", "verdicts")}


def _set_seat(eng, **changes):
    handler = eng.backend.handler("harness.decommission-move")
    state = handler.dump_state()
    state["seats"]["suborch1"].update(changes)
    handler.load_state(state)
    eng.backend.save()


DESIRED = {"stamp": STAMP, "anchor": ANCHOR, "source_revision": REV}


def _apply(eng, accept=frozenset({GATE}), desired=DESIRED):
    plan = eng.plan("remove", SCOPE, CID, desired=desired)
    return eng.apply("remove", SCOPE, CID, plan_digest=plan.digest, accept_gates=accept, desired=desired)


def _run_id(eng):
    return coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]


def _transcript(eng, run_id):
    store, run_tuple = eng._find_run(run_id)
    with store.open(run_tuple) as handle:
        events = [json.loads(line) for line in handle.read_transcript()]
        return [e for e in events if isinstance(e, dict)], handle.read_record()


RECORD = R.record_path(R.quarantine_root("suborch1", STAMP))


def test_engine_real_contract_gate_refuses_without_a_grant(tmp_path):
    eng = _engine(tmp_path, gate_class="independent-agent")
    before = H.tree_snapshot(eng.backend.root)
    with pytest.raises(EngineRefusal) as info:
        _apply(eng, accept=frozenset())
    assert info.value.code.startswith("grant") and GATE in info.value.message
    assert H.tree_snapshot(eng.backend.root) == before


def test_engine_operator_copy_refuses_without_accepting_the_gate(tmp_path):
    eng = _engine(tmp_path)
    for accept in (None, frozenset({"seat-run-state"})):
        with pytest.raises(EngineRefusal) as info:
            _apply(eng, accept=accept)
        assert info.value.code == "gate-unanswered" and GATE in info.value.message


def test_engine_decommission_then_verify_and_satisfied_is_absorbing(tmp_path):
    eng = _engine(tmp_path)
    [outcome] = _apply(eng)
    assert outcome.state == "SATISFIED"
    state = _dc_state(eng)
    assert state["files"][RECORD]["document"]["status"] == "committed"
    # every op's postconditions were verified at apply (the run is SATISFIED); the engine's
    # drift verify reads forward legs only (executor FORWARD_LEGS), so a remove run adds no check
    [result] = eng.verify(SCOPE, CID)
    assert result["checks"] == [] and result["run_id"] is None
    with pytest.raises(EngineRefusal) as info:  # restore after SATISFIED is a forward plan (T-5874)
        eng.rollback(outcome.run_id)
    assert info.value.code == "rollback-refused"
    with pytest.raises(EngineRefusal) as info:  # a second plan at the same stamp refuses
        eng.plan("remove", SCOPE, CID, desired=DESIRED)
    assert "quarantine-exists" in info.value.message


def test_engine_failure_before_the_controller_stop_halts_plainly_and_rolls_back(tmp_path):
    original = _seat("mason")
    eng = _engine(tmp_path, copy.deepcopy(original))
    _set_seat(eng, unit_stuck={"pantheon-restart-drain.timer": "disable-now"})
    with pytest.raises(EngineRefusal) as info:
        _apply(eng)
    assert info.value.code == "halted"
    run_id = _run_id(eng)
    events, record = _transcript(eng, run_id)
    assert "escalations" not in record["halted"]
    assert not [e for e in events if e["event"] == "escalation.page"]
    _set_seat(eng, unit_stuck={})
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    assert _undone({k: v for k, v in _dc_state(eng).items() if k != "unit_stuck"}, RECORD) == original


def test_engine_a_halt_after_the_controller_stop_escalates_once_and_rollback_restores(tmp_path):
    original = _seat("mason")
    eng = _engine(tmp_path, copy.deepcopy(original))
    victim = f"{MASON_HOME}/.local/state/pantheon/persona-installer/mason"
    _set_seat(eng, move_fail=[victim])
    with pytest.raises(EngineRefusal) as info:
        _apply(eng)
    assert info.value.code == "halted" and "half-removed" in info.value.message
    run_id = _run_id(eng)
    halted = _dc_state(eng)
    assert victim in halted["files"] and not R.subtree(halted["files"], MASON_MOVES[0])  # earlier moves done
    events, record = _transcript(eng, run_id)
    command = f"python3 -m tools.germinate rollback --run {run_id}"
    [marker] = record["halted"]["escalations"]
    assert (marker["subject"], marker["recipient"], marker["rollback"]) == ("suborch1", "recovery", command)
    assert [e["op_id"] for e in events if e["event"] == "escalation.page"] == [marker["op_id"]]
    _set_seat(eng, move_fail=[])
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    assert _undone({k: v for k, v in _dc_state(eng).items() if k != "move_fail"}, RECORD) == original


def test_engine_controller_postcondition_unmet_escalates(tmp_path):
    eng = _engine(tmp_path)
    _set_seat(eng, scope_survives=True)
    with pytest.raises(EngineRefusal) as info:
        _apply(eng)
    assert info.value.code == "halted" and "session" in info.value.message
    _events, record = _transcript(eng, _run_id(eng))
    [marker] = record["halted"]["escalations"]
    assert marker["op_id"].startswith("rm-unit-") and marker["recipient"] == "recovery"


def test_engine_a_stopped_rename_back_leaves_the_controller_down_and_escalates(tmp_path):
    eng = _engine(tmp_path)
    _set_seat(eng, move_fail=[MASON_MOVES[-1]])
    with pytest.raises(EngineRefusal):
        _apply(eng)
    run_id = _run_id(eng)
    _set_seat(eng, move_fail=[], rename_back_fail=[MASON_MOVES[0]])
    with pytest.raises(EngineRefusal) as info:
        eng.rollback(run_id)
    assert info.value.code == "rollback-incomplete"
    _events, record = _transcript(eng, run_id)
    [marker] = record["rollback_incomplete"]["escalations"]
    controller_op = HM._dc_id("unit", "pantheon-runtime-controller@mason.service")
    assert marker["op_id"] == controller_op and "half-removed" in marker["reason"]
    assert _dc_state(eng)["units"]["pantheon-runtime-controller@mason.service"]["ActiveState"] == "inactive"
    _set_seat(eng, rename_back_fail=[])
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    assert _dc_state(eng)["units"]["pantheon-runtime-controller@mason.service"] == UP


def test_engine_claim_held_refuses_a_second_plan(tmp_path):
    eng = _engine(tmp_path)
    _set_seat(eng, move_fail=[MASON_MOVES[0]])
    with pytest.raises(EngineRefusal):
        _apply(eng)
    with pytest.raises(EngineRefusal) as info:
        eng.plan("remove", SCOPE, CID, desired=dict(DESIRED, stamp="20261102T070000Z"))
    assert info.value.code == "claim-held"


# --- planner purity -----------------------------------------------------------------------------


def test_real_plan_runs_under_the_json_planner_runner():
    ops = InProcessJsonPlannerRunner().run(PLANNER, "remove", _request("mason"))
    assert _ids(ops) == _ids(_plan("mason"))


@pytest.mark.parametrize("veto", ["maintenance", "deep-sleep"])
def test_real_plan_remove_is_pure_under_planner_guard(veto):
    """Review N4: the deep-sleep case reaches the seat_state.parse_record path under the guard."""

    for name in ("tools.mailbox_onboard", "tools.keep_going", "tools.persona_bringup", "tools.agent_role_map",
                 "tools.persona_apply_path", "tools.scheduler_spec", "tools.seat_state", "yaml"):
        __import__(name)
    seat = _seat("mason")
    if veto == "deep-sleep":
        seat["remediation"] = _remediation(None, _record("deep-sleep"))
    request = _request("mason", seat)
    unguarded = HM.plan_remove(json.loads(json.dumps(request)))
    guard = PlannerGuard()
    with guard:
        guarded = HM.plan_remove(json.loads(json.dumps(request)))
    assert guard.violations == []
    assert guarded == unguarded


# --- U1 / U2 / U4 / U5 unchanged: differential, never skipping ------------------------------------

#: origin/main after U5 (T-5858) merged: the base U3 builds on. It is on main (the CI test job
#: checks out with fetch-depth 0), so git can always produce it; if it cannot, the test FAILS.
_PRE_U3 = "b385f45aa7"


def _git_show(path):
    return subprocess.run(["git", "-C", str(REPO_ROOT), "show", f"{_PRE_U3}:{path}"],
                          capture_output=True, text=True, check=True).stdout


#: T-5480: the ONLY planner-module change since the pinned bases is one new installer-kind
#: mapping (seat-codex-hook-config, the harness.codex-shell-box op). The base planner imports
#: the CURRENT tools.persona_installer, which now emits that kind; grant the base exactly that
#: one entry (and assert it is all it lacks), so everything else must still match byte for byte.
_T5480_KINDS = ("seat-codex-hook-config",)


def _grant_t5480_kind_mapping(old):
    missing = sorted(set(HM._INSTALL_KIND_MAP) - set(old._INSTALL_KIND_MAP))
    assert missing == sorted(_T5480_KINDS), missing
    old._INSTALL_KIND_MAP = {**old._INSTALL_KIND_MAP, **{k: HM._INSTALL_KIND_MAP[k] for k in _T5480_KINDS}}
    assert old._INSTALL_KIND_MAP == HM._INSTALL_KIND_MAP


def test_real_u1_u4_u5_planner_outputs_are_unchanged(tmp_path):
    from tools.germinate.components.tests import test_harness_manager_run_state as U5
    from tools.germinate.components.tests import test_harness_manager_supervisor as SV

    source = tmp_path / "harness_manager_pre_u3.py"
    source.write_text(_git_show("tools/germinate/components/harness-manager.py"), encoding="utf-8")
    old = _load(source, "_t5859_pre_u3_harness_manager")
    assert not hasattr(old, "plan_remove")
    _grant_t5480_kind_mapping(old)
    t5480_kinds_seen = set()
    for persona in DECOMMISSIONABLE:  # review F6: every non-recovery persona
        request = {"scope": f"seat-{ACCOUNTS[persona]}", "desired": {"source_revision": REV}}
        new_install = HM.plan_install(copy.deepcopy(request))
        assert new_install == old.plan_install(copy.deepcopy(request)), persona
        t5480_kinds_seen |= {op["kind"] for op in new_install} & set(_T5480_KINDS)
    assert t5480_kinds_seen == set(_T5480_KINDS)  # the granted kind is really exercised
    for persona in ("specialist", "recovery"):
        for request in (SV._request(persona),
                        SV._request(persona, SV._post_cutover_seat(persona), supervisor="bespoke", source_revision=None)):
            new_ops = HM.plan_configure(copy.deepcopy(request))
            # T-6047 intended change (T-5956 parity): the companion STOP ops gain desired.companion_snapshot;
            # strip exactly that key from exactly those ops, and the rest must be byte-equal to the base
            for op in new_ops:
                if op["kind"] == "harness.companion-unit" and op["desired"]["action"] == "stop":
                    assert op["desired"].pop("companion_snapshot")
            assert new_ops and new_ops == old.plan_configure(copy.deepcopy(request)), persona
    # T-5862: one of the TWO intended U5 changes since the U3 base is that the planner also resolves a
    # role's instance.yaml run_state_units. The base planner (which reads the same instance.yaml
    # and schedules/) refuses specialist on exactly those units; given exactly those units added to
    # its derived targets, it must reproduce the new plan byte for byte. mason declares none.
    base_targets = old._rs_targets

    def base_plus_declared(manifest, persona, revision, role):
        targets, oneshots, residue = base_targets(manifest, persona, revision, role)
        for unit, oneshot in U5.RS.parse_run_state_units(role.get("run_state_units"), persona=persona):
            assert unit not in targets, unit
            targets[unit] = "instance.yaml run_state_units"
            if oneshot is not None:
                oneshots[unit] = oneshot
        return targets, oneshots, residue

    for persona in ("mason", "specialist"):
        request = U5._request(persona)
        new_ops = HM.plan_configure(copy.deepcopy(request))
        declared = U5._real_targets(persona)[0]
        declared = sorted(u for u, src in declared.items() if src == "instance.yaml run_state_units")
        if declared:
            assert declared == ["specialist-a2a.service", "pantheon-acp-bridge-backlog@specialist.timer"], persona
            with pytest.raises(old.PlanRefused, match="§2 wake path not derivable") as refused:
                old.plan_configure(copy.deepcopy(request))
            assert any(u in str(refused.value) for u in declared), refused.value
        # T-5866 (Recovery D15 r2): the second intended U5 change since the U3 base is that rs-drain
        # opts into the engine's rollback_on_halt. Strip exactly that key from exactly that op.
        opted = [op["operation_id"] for op in new_ops if "rollback_on_halt" in op]
        assert opted == ["rs-drain"], (persona, opted)
        assert next(op for op in new_ops if op["operation_id"] == "rs-drain")["rollback_on_halt"] is True
        stripped = [{k: v for k, v in op.items() if k != "rollback_on_halt"} for op in new_ops]
        old._rs_targets = base_plus_declared
        try:  # both intended changes accounted for: the rest must equal the base byte for byte
            assert stripped and stripped == old.plan_configure(copy.deepcopy(request)), persona
        finally:
            old._rs_targets = base_targets


def test_real_u2_inputs_are_byte_identical_to_the_u3_base():
    """U2's root half (the host-substrate planner, the seat-identity resolver and the
    host_substrate package) is untouched by U3, so its outputs cannot have moved."""

    # T-6167 PR-5 deliberately moved harness_manager_root.py and host-substrate.yaml (seat group memberships,
    # Q3); they leave this byte pin and are pinned by test_harness_manager_root_memberships.py instead.
    for path in ("tools/germinate/components/host-substrate.py", "tools/germinate/host_substrate/families.py",
                 "tools/germinate/host_substrate/ports.py"):  # helper.py left the pin at T-6048 (strict-A-3 scheduled refusal)
        assert (REPO_ROOT / path).read_text(encoding="utf-8") == _git_show(path), path
    # T-6048 compensates for unpinning helper.py: its change only touches the SCHEDULED
    # context. U2's root half reaches the helper solely through host-substrate_handlers,
    # which constructs it in BOOTSTRAP context only. Pin that premise directly: if a handler
    # ever builds the helper in another context, this fails and the unpin must be revisited.
    handlers = (REPO_ROOT / "tools/germinate/components/host-substrate_handlers.py").read_text(encoding="utf-8")
    sites = [ln.strip() for ln in handlers.splitlines() if "RootHelper(" in ln and not ln.lstrip().startswith(("from ", "import ", "#"))]
    assert len(sites) == 2, sites  # a new construction site must be reviewed against this premise
    assert all(site.endswith(", BOOTSTRAP)") for site in sites), sites


# --- kind classification, contract and dark pins -------------------------------------------------


def test_real_decommission_kinds_are_one_isolated_component_local_block():
    kinds = read_op_kinds(CID, HANDLERS)
    block = {k: v for k, v in kinds.items() if v[0] == "harness.decommission"}
    assert block == {k: ("harness.decommission", "seat") for k in KINDS}
    order = list(kinds)
    first = order.index(KINDS[0])
    assert order[first:first + len(KINDS)] == list(KINDS)
    assert "harness.decommission" not in RESERVED_CLASSES
    registry = load_repo_op_registry(REPO_ROOT)
    for kind in KINDS:
        assert component_kind_errors(kind, "harness.decommission", "seat") == []
        entry = registry.get(kind)
        assert entry.privilege == "seat" and not entry.broker_admitted and not entry.substrate
    assert HMH.HarnessDecommissionSim.kinds == frozenset(KINDS) == HM._DECOMMISSION_KINDS


def test_real_contract_declares_the_org080_decommission_gate_and_the_remove_leg():
    import yaml

    contract = load_contract(COMPONENTS / "harness-manager.yaml", load_repo_op_registry(REPO_ROOT))
    gate = contract.gates[GATE]
    assert gate.approval_class == "independent-agent" and gate.irreversible is True
    assert contract.declares_leg("remove")
    assert set(KINDS) <= contract.privilege["seat"]
    body = yaml.safe_load((COMPONENTS / "harness-manager.yaml").read_text(encoding="utf-8"))["component"]
    [raw] = [g for g in body["gates"] if g["id"] == GATE]
    # ORG-080 §2 YAML contract (gates): {id: decommission, irreversible: true, approval: {class: independent-agent,
    # grant_source: action-broker-approval-registry, binds: *binds}}
    assert raw == {"id": GATE, "irreversible": True, "approval": {
        "class": "independent-agent", "grant_source": "action-broker-approval-registry",
        "binds": ["actor", "performer", "target", "operation", "plan_digest", "scope", "policy_revision", "expiry"]}}
    adr = (REPO_ROOT / "architecture" / "adr" / "ORG-080-germination-engine.md").read_text(encoding="utf-8")
    assert "- {id: decommission, irreversible: true, approval: {class: independent-agent, " \
           "grant_source: action-broker-approval-registry, binds: *binds}}" in adr


def test_runtime_scope_constant_matches_u4():
    assert R.RUNTIME_SCOPE_UNIT == S.RUNTIME_SCOPE_UNIT


def test_dark_pins_live_actuation_false_and_no_flag():
    from tools import host_broker_install
    from tools.germinate.components import pantheon_config_git
    from tools.germinate.host_substrate import ports

    assert ports.LIVE_ACTUATION_ENABLED is False
    assert host_broker_install.LIVE_ACTUATION_ENABLED is False
    assert pantheon_config_git.LIVE_ACTUATION_ENABLED is False
    for path in (PLANNER, HANDLERS, HELPER):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        names = {t.id for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign))
                 for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Name)}
        assert not any(name.startswith("LIVE_ACTUATION") for name in names), path
        assert "os.environ" not in text, path


def test_helper_imports_nothing_live():
    tree = ast.parse(HELPER.read_text(encoding="utf-8"))
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert names <= {"__future__", "hashlib", "json", "posixpath", "re"}, names


def test_untested_live_is_in_every_docstring():
    for path in (PLANNER, HELPER):
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
        assert "UNTESTED-LIVE" in doc and "first live fire is supervised" in doc and "reclassified" in doc
    assert "UNTESTED-LIVE" in HMH.HarnessDecommissionSim.__doc__
