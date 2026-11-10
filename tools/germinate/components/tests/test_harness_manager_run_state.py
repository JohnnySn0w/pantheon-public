"""T-5858 (T-5091 U5, germination GF2): harness-manager configure ``run_state``, fleet
bringdown/restore, built DARK (UNTESTED-LIVE).

``plan_run_state`` re-states sop/standard/fleet-daemon-bringdown-and-restore.md §3 (pause)
and §5 (restore) as typed ops with declarative pre/postconditions. The SOP stays untouched
(byte-pinned below) and remains the operational surface (HO ruling Q2, ORG-080 A-7). Ops
run only through the SIMULATED HarnessRunStateSim; nothing here actuates a host.

The ``test_real_*`` tests read the real repo, instance.yaml, schedules/ and the installer's
own plan, read-only. Engine-tier tests run on a tmp checkout over a simulated host, reusing
the GF1 fixture. Recovery D15 (T-5858) conditions (o)-(u) each have tests named for them.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

from tools import persona_installer as PI
from tools import seat_state
from tools.germinate.engine.components import load_repo_op_registry, read_op_kinds
from tools.germinate.engine.contract import load_contract
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.planner import InProcessJsonPlannerRunner, PlannerGuard
from tools.germinate.engine.tests import helpers as H
from tools.germinate.op_registry import RESERVED_CLASSES, component_kind_errors
from tools.germinate.planner_sandbox.admission import coordinator

from tools.germinate.components import harness_manager_run_state as RS
from tools.germinate.components.tests import test_harness_manager_component as GF1

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPONENTS = REPO_ROOT / "tools" / "germinate" / "components"
PLANNER = COMPONENTS / "harness-manager.py"
HANDLERS = COMPONENTS / "harness-manager_handlers.py"
HELPER = COMPONENTS / "harness_manager_run_state.py"
SOP = REPO_ROOT / "sop" / "standard" / "fleet-daemon-bringdown-and-restore.md"
MANIFEST_PATH = REPO_ROOT / "instance.yaml"
CID = "harness-manager"
REV = "0123456789abcdef0123456789abcdef01234567"
STAMP = "20261102T040000Z"
RESTORE_STAMP = "20261103T040000Z"
ANCHOR = "operator:TG6694"
GATE = "seat-run-state"
UID = 1007
NEXT = "Mon 2026-11-02 05:00:00 UTC"  # == the sim's next elapse on enable
ACCOUNTS = {"mason": "suborch1", "specialist": "claude-research", "orchestrator": "claude", "recovery": "claude-fallback",
            "watchman": "codex-fallback", "archivist": "exocortex", "artisan": "suborch2"}
RUN_STATE_KINDS = (
    "harness.run-state-manifest", "harness.run-state-seat-state", "harness.run-state-unit",
    "harness.run-state-drain", "harness.run-state-reset-failed", "harness.run-state-commit",
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HM = _load(PLANNER, "_t5858_harness_manager")
HMH = _load(HANDLERS, "_t5858_harness_manager_handlers")


# --- fixtures: independent of the planner -------------------------------------------


def _wake(persona, keep_going=True):
    stems = ["pantheon-inbox-consumer", "pantheon-mailbox-wake", "pantheon-persona-consume"]
    if keep_going:
        stems.append("pantheon-keep-going")
    return [f"{s}@{persona}.timer" for s in stems]


#: T-5862: the units declared for the first time by that ticket, hand-listed (instance.yaml
#: run_state_units, and specialist's two dispatch loops in schedules/specialist.yaml).
T5862_DECLARED = {
    "specialist": ["pantheon-acp-bridge-backlog@specialist.timer", "specialist-a2a.service",
              "pantheon-knowledge-sweep.timer", "pantheon-watch-digest.timer"],
    "archivist": ["pantheon-acp-bridge-backlog@archivist.timer"],
}

#: The §2 targets the data yields per persona, HAND-LISTED from the installer's seat timers,
#: instance.yaml (companion_units, codex_receiver_unit, run_state_units) and schedules/<persona>.yaml.
EXPECTED_TARGETS = {
    "mason": sorted(_wake("mason")),
    "artisan": sorted(_wake("artisan")),
    "specialist": sorted(_wake("specialist") + ["pantheon-watch.timer", "pantheon-acp-bridge@specialist.service", "codex-telegram-poller.service"]
                    + T5862_DECLARED["specialist"]),
    "orchestrator": sorted(_wake("orchestrator") + ["codex-telegram-poller.service"]),
    "archivist": sorted(_wake("archivist", keep_going=False) + ["codex-telegram-poller.service"] + T5862_DECLARED["archivist"]),
}


def _oneshot(timer):
    return timer[: -len(".timer")] + ".service"


def _paths(persona):
    root = f"/home/{ACCOUNTS[persona]}/.local/state/pantheon"
    return f"{root}/harness-manager/run-state-manifest.json", f"{root}/personas/{persona}/overrides/seat-state.json"


def _expected_pins(persona):
    targets = EXPECTED_TARGETS[persona]
    return {"units": sorted(set(targets) | {_oneshot(t) for t in targets if t.endswith(".timer")}),
            "files": sorted(_paths(persona))}


def _up(unit):
    if unit.endswith(".timer"):
        return {"LoadState": "loaded", "ActiveState": "active", "SubState": "waiting", "UnitFileState": "enabled",
                "NextElapseUSecRealtime": NEXT}
    return {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "UnitFileState": "enabled"}


def _static_oneshot():
    return {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead", "UnitFileState": "static"}


def _seat(persona, **overrides):
    units = {}
    for unit in EXPECTED_TARGETS[persona]:
        units[unit] = _up(unit)
        if unit.endswith(".timer"):
            units[_oneshot(unit)] = _static_oneshot()
    # never targets: the supervisor, the quota timer, restart-drain (F3), OS plumbing, a slice
    units[f"pantheon-runtime-controller@{persona}.service"] = _up("x.service")
    units[f"pantheon-persona-quota@{persona}.timer"] = _up("x.timer")
    units["pantheon-restart-drain.timer"] = _up("x.timer")
    units["dbus.service"] = _up("x.service")
    units["app-pantheon-persona-consume.slice"] = {"LoadState": "loaded", "ActiveState": "active", "SubState": "active",
                                                   "UnitFileState": "static"}
    seat = {
        "uid": UID,
        "user_bus": True,
        "target_manifest_sha256": hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest(),
        "units": units,
        "manifest": None,
        "seat_state": None,
        "seat_state_tombstones": [],
        "run_state": None,
        "pins": _expected_pins(persona),
    }
    seat.update(overrides)
    return seat


def _request(persona, seat=None, **desired):
    desired = {"run_state": "paused", "stamp": STAMP, "source_revision": REV, "anchor": ANCHOR} | desired
    desired = {k: v for k, v in desired.items() if v is not None}
    account = ACCOUNTS[persona]
    seat = seat if seat is not None else _seat(persona)
    return {"scope": f"seat-{account}", "desired": desired, "observed": {HM.RUN_STATE_HANDLER: {"account": account, "seat": seat}}}


def _pause(persona, seat=None, **desired):
    return HM.plan_configure(_request(persona, seat, **desired))


def _restore(persona, seat, **desired):
    return HM.plan_configure(_request(persona, seat, **({"run_state": "running", "stamp": RESTORE_STAMP} | desired)))


def _by_id(ops):
    return {op["operation_id"]: op for op in ops}


def _ids(ops):
    return [op["operation_id"] for op in ops]


def _inst(raw):
    return PI.InstallOperation(
        operation_id=raw["operation_id"], layer=raw["layer"], kind=raw["kind"], target=raw["target"],
        precondition=dict(raw["precondition"]), desired=dict(raw["desired"]), inverse=raw["inverse"],
        privilege=raw["privilege"], owner=raw["owner"], provenance=dict(raw["provenance"]), execution_scope="all",
    )


def _sim(persona, seat=None):
    sim = HMH.HarnessRunStateSim()
    sim.load_state({"seats": {ACCOUNTS[persona]: seat if seat is not None else _seat(persona)}})
    return sim


def _run(sim, ops):
    """Preflight + execute every op through the sim (the unit tier's stand-in for apply)."""

    for raw in ops:
        op = _inst(raw)
        result = sim.preflight(op)
        assert result.ok, (raw["operation_id"], result.detail)
        _mutation, evidence = sim.execute(op)
        assert evidence.ok
        assert sim.verify_operation(op).ok, (raw["operation_id"], sim.verify_operation(op).detail)


def _state(sim, persona):
    seat = sim.dump_state()["seats"][ACCOUNTS[persona]]
    return {k: v for k, v in seat.items() if k not in ("writers", "verdicts")}


def _paused(persona, seat=None, **desired):
    sim = _sim(persona, seat)
    _run(sim, _pause(persona, _state(sim, persona), **desired))
    return sim


# --- the SOP stays untouched; inventory and category parity -----------------------------

#: sha256 of the SOP's bytes at T-5858's base (it must not change here; Q2 / A-7).
_SOP_SHA256 = "b1e9aada3e710aa67b4553731d848267317b2c085cc5dace2837dad9ae31f854"


def test_real_sop_is_byte_unchanged():
    assert hashlib.sha256(SOP.read_bytes()).hexdigest() == _SOP_SHA256


_MAPPING_HEADS = {"planner:", "category:", "structural:", "anti-step:", "commit", "out-of-scope:", *RUN_STATE_KINDS}


def test_real_inventory_is_an_exact_bijection_with_the_sop_steps():
    missing, extra = RS.inventory_parity(SOP.read_text(encoding="utf-8"))
    assert (missing, extra) == ([], []), f"unmapped SOP step lines {missing}; rows that are not steps {extra}"
    lines = [line for line, _m in RS.INVENTORY]
    assert len(lines) == len(set(lines))
    for _line, mapping in RS.INVENTORY:
        assert mapping.split(" ")[0] in _MAPPING_HEADS, mapping


def test_deleting_any_single_inventory_row_fails_the_parity():
    text = SOP.read_text(encoding="utf-8")
    for row in RS.INVENTORY:
        rows = tuple(r for r in RS.INVENTORY if r != row)
        missing, _extra = RS.inventory_parity(text, rows)
        assert missing == [row[0]], row


def test_inventory_names_every_sop_kind_and_every_procedure_step():
    """Every kind that re-states an SOP step is named (the seat-state kind is Recovery (p), not an
    SOP step), and every numbered 3.x step has its row."""

    mapped = " ".join(m for _l, m in RS.INVENTORY)
    for kind in RUN_STATE_KINDS:
        if kind != "harness.run-state-seat-state":
            assert kind in mapped, kind
    text = SOP.read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(text, 1):
        if re.match(r"\*\*3\.\d — ", line):
            assert number in dict(RS.INVENTORY), number


def _table_tokens(line):
    """The unit names in a §2 row's examples column (the third column's prose is not units)."""

    tokens = re.findall(r"`([^`]+)`", line.split("|")[2])
    out = []
    for token in tokens:
        token = token.replace("@P", "@mason").replace("<seat>", "mason").replace("*", "x")
        out.append(token if "." in token else token + ".service")
    return out


def test_real_sop_table_rows_map_to_categories_exactly():
    """Each SOP §2 table row is one CATEGORIES row (same order), and every unit the row names
    classifies to it with the row's disposition."""

    lines = SOP.read_text(encoding="utf-8").splitlines()
    want = {"yes": RS.PAUSE, "judgement": RS.JUDGEMENT, "never": RS.NEVER}
    assert list(RS.SOP_TABLE_ROWS.values()) == [row[0] for row in RS.CATEGORIES]
    for number, category in RS.SOP_TABLE_ROWS.items():
        line = lines[number - 1]
        disposition = want[re.search(r"\| \*\*(yes|judgement|never)", line).group(1)]
        tokens = _table_tokens(line)
        assert tokens, number
        for token in tokens:
            assert RS.classify(token) == (category, disposition), (number, token)


def test_deleting_a_category_row_breaks_its_sop_row():
    lines = SOP.read_text(encoding="utf-8").splitlines()
    saved = RS.CATEGORIES
    try:
        for index, row in enumerate(saved):
            RS.CATEGORIES = saved[:index] + saved[index + 1:]
            [number] = [n for n, c in RS.SOP_TABLE_ROWS.items() if c == row[0]]
            assert any(RS.classify(t) != (row[0], row[1]) for t in _table_tokens(lines[number - 1])), row[0]
    finally:
        RS.CATEGORIES = saved


def test_additions_beyond_the_sop_table_are_named_with_their_basis():
    text = SOP.read_text(encoding="utf-8")
    for stem, basis in RS.ADDITIONS.items():
        assert stem not in text and basis
    assert RS.classify("pantheon-persona-consume@mason.timer") == ("wake timers", RS.PAUSE)
    assert RS.classify("codex-telegram-poller.service") == ("inbound bridges", RS.PAUSE)
    # mason ruling F3: never targets
    for unit in ("pantheon-restart-drain.timer", "pantheon-runtime-controller@mason.service", "pantheon-persona-quota@mason.timer"):
        assert RS.classify(unit) is None, unit


# --- target derivation (data only) ---------------------------------------------------------


@pytest.mark.parametrize("persona", sorted(EXPECTED_TARGETS))
def test_real_targets_and_pins_are_data_derived(persona):
    ops = _pause(persona)
    commit = _by_id(ops)["rs-commit"]
    assert json.loads(commit["provenance"]["pinned_targets"]) == _expected_pins(persona)
    paused = [op["desired"]["unit"] for op in ops if op["kind"] == "harness.run-state-unit"]
    assert sorted(paused) == EXPECTED_TARGETS[persona]


def test_real_plan_runs_under_the_json_planner_runner():
    ops = InProcessJsonPlannerRunner().run(PLANNER, "configure", _request("specialist"))
    assert _ids(ops) == _ids(_pause("specialist"))


def _preimport():
    for name in ("tools.mailbox_onboard", "tools.keep_going", "tools.persona_bringup", "tools.agent_role_map",
                 "tools.persona_apply_path", "tools.scheduler_spec", "tools.seat_state", "yaml"):
        __import__(name)


@pytest.mark.parametrize("shape", ("paused", "running"))
def test_real_plan_run_state_is_pure_under_planner_guard(shape):
    _preimport()
    request = _request("specialist") if shape == "paused" else _request(
        "specialist", _state(_paused("specialist"), "specialist"), run_state="running", stamp=RESTORE_STAMP)
    unguarded = HM.plan_configure(json.loads(json.dumps(request)))
    guard = PlannerGuard()
    with guard:
        guarded = HM.plan_configure(json.loads(json.dumps(request)))
    assert guard.violations == []
    assert guarded == unguarded


# --- pause: order, shape, pinning --------------------------------------------------------------


def test_pause_order_is_the_sop_order():
    """SOP §3 + Recovery (p)/(q): manifest FIRST, seat-state before the first unit op, every timer
    before the drain, the drain before every service, reset-failed, then the commit."""

    ops = _pause("specialist")
    ids = _ids(ops)
    timers = [i for i in ids if i.startswith("rs-timer-")]
    services = [i for i in ids if i.startswith("rs-service-")]
    assert ids == ["rs-manifest", "rs-seat-state", *timers, "rs-drain", *services, "rs-reset-failed", "rs-commit"]
    by = _by_id(ops)
    assert all(by[t]["desired"]["unit"].endswith(".timer") for t in timers)
    assert not any(by[s]["desired"]["unit"].endswith(".timer") for s in services) and services
    assert all(by[i]["desired"]["action"] == "disable-now" for i in timers + services)  # SOP 3.4
    gated = [op for op in ops if op.get("gate")]
    assert [op["operation_id"] for op in gated] == ["rs-timer-0"]  # D-10: the first unit op
    assert gated[0]["gate"] == GATE and gated[0]["desired"]["anchor"] == ANCHOR
    drained = by["rs-drain"]["desired"]["oneshots"].split(",")
    assert drained == [_oneshot(by[t]["desired"]["unit"]) for t in timers]
    assert by["rs-drain"]["desired"]["on_timeout"] == "abort" and by["rs-drain"]["desired"]["window_s"] == "120"


def test_every_op_is_pinned_to_the_seats_own_user_manager():
    for op in _pause("specialist"):
        assert op["kind"] in RUN_STATE_KINDS
        assert op["privilege"] == "seat" and op["owner"] == "claude-research"
        assert op["desired"]["systemd_manager"] == "user" and op["desired"]["account"] == "claude-research"
        assert op["desired"]["xdg_runtime_dir"] == f"/run/user/{UID}"
        assert op["precondition"]["user_bus"] == "reachable"


def test_manifest_carries_pause_id_revision_and_pre_state_and_is_atomic_fsync():
    """Recovery D15 (T-5858) (q)."""

    ops = _pause("mason")
    manifest_op = _by_id(ops)["rs-manifest"]
    doc = json.loads(manifest_op["desired"]["document"])
    assert manifest_op["desired"]["write"] == "atomic-fsync" and manifest_op["precondition"]["manifest_sha256"] == "absent"
    assert manifest_op["target"] == _paths("mason")[0]
    assert doc["pause_id"] == f"rs-suborch1-{STAMP}" and doc["plan_revision"] == REV and doc["anchor"] == ANCHOR
    assert [e["unit"] for e in doc["entries"]] == [op["desired"]["unit"] for op in ops if op["kind"] == "harness.run-state-unit"]
    assert all(e["pre"] == RS.UP for e in doc["entries"])
    assert RS.manifest_problems(doc, persona="mason", account="suborch1") == []


def test_seat_state_record_is_the_l1_writers_shape():
    """Recovery D15 (T-5858) (p): the ORG-106 record (paused, by, anchor, pause_id) passes the L1
    writer's own validation, reads as paused-intake, and is never a private flag."""

    op = _by_id(_pause("mason"))["rs-seat-state"]
    record = json.loads(op["desired"]["document"])
    assert op["target"] == _paths("mason")[1]
    assert seat_state.validate_record(record) == []
    contribution = seat_state.parse_record(record)
    assert contribution.state == seat_state.STATE_PAUSED_INTAKE and contribution.intake_blocked
    assert record["set_by"].startswith("germinate:harness-manager") and record["anchor"] == ANCHOR
    assert record["pause_id"] == f"rs-suborch1-{STAMP}" and record["indefinite"] is True and record["expires"] is None
    assert record["set_ts"] == "2026-11-02T04:00:00Z"


def test_u_seat_state_written_first_and_cleared_last():
    """Recovery D15 (T-5858) (u)/(p)/(t): pause writes seat-state before the first unit op;
    restore clears it as the LAST op, after the commit."""

    ids = _ids(_pause("mason"))
    assert ids.index("rs-seat-state") < min(i for i, x in enumerate(ids) if x.startswith("rs-timer-"))
    ids = _ids(_restore("mason", _state(_paused("mason"), "mason")))
    assert ids[-2:] == ["rs-restore-commit", "rs-seat-state-clear"]


def test_commit_checks_are_exact_fields():
    by = _by_id(_pause("mason"))
    checks = json.loads(by["rs-commit"]["desired"]["postconditions"])
    fields = [c for c in checks if c["check"] == "unit-fields"]
    assert {c["params"]["unit"] for c in fields} == set(EXPECTED_TARGETS["mason"])
    assert all({k: c["params"][k] for k in ("UnitFileState", "ActiveState")} == RS.DOWN for c in fields)
    assert {c["check"] for c in checks} >= {"no-active-wake", "no-new-failed", "document"}


# --- the §4 trap ------------------------------------------------------------------------------------


def test_section_4_trap_inactive_never_reads_as_active():
    view = {"units": {"a.timer": {"ActiveState": "inactive", "UnitFileState": "disabled"}}}
    [result] = RS.evaluate([RS.check("unit-fields", unit="a.timer", ActiveState="active")], view)
    assert result["ok"] is False
    # the WRONG reading the SOP warns about would have passed it
    naive = sum("active" in u["ActiveState"] for u in view["units"].values())
    assert naive == 1
    [wake] = RS.evaluate([RS.check("no-active-wake")], {"units": {"pantheon-keep-going@x.timer": {"ActiveState": "inactive"}}})
    assert wake["ok"] is True
    [wake] = RS.evaluate([RS.check("no-active-wake")], {"units": {"pantheon-keep-going@x.timer": {"ActiveState": "active"}}})
    assert wake["ok"] is False


@pytest.mark.parametrize("path", (HELPER, PLANNER, HANDLERS), ids=lambda p: p.name)
def test_section_4_no_substring_test_on_a_state(path):
    source = path.read_text(encoding="utf-8")
    assert not re.search(r"""["'](in)?active["']\s+in\b""", source)
    assert not re.search(r"""\bin\s+\w*\.get\(["'](ActiveState|UnitFileState)["']\)""", source)
    assert not re.search(r"""["'](ActiveState|UnitFileState)["']\]\s*\.\s*(find|startswith|endswith|count)\(""", source)


def test_checks_never_pass_on_missing_or_malformed_facts():
    for check_id, params in (("unit-fields", {"unit": "x.timer", "ActiveState": "active"}), ("no-active-wake", {}),
                             ("no-new-failed", {}), ("next-elapse", {"unit": "x.timer"})):
        [result] = RS.evaluate([{"check": check_id, "params": params}], {})
        assert result["ok"] is False, check_id
    [broken] = RS.evaluate([{"check": "unit-fields", "params": {}}], {"units": {}})
    assert broken["ok"] is False and "raised" in broken["detail"]


# --- refusals ---------------------------------------------------------------------------------------


def _with_unit(persona, unit, fields):
    seat = _seat(persona)
    seat["units"][unit] = fields
    return seat


@pytest.mark.parametrize(
    "desired, match",
    [
        ({"harness": "codex"}, "D-L"),
        ({"supervisor": "consume"}, "D-L"),
        ({"run_state": "stopped"}, "must be one of"),
        ({"stamp": "2026-11-02"}, "YYYYMMDDTHHMMSSZ"),
        ({"source_revision": "abc"}, "40-hex"),
        ({"anchor": None}, "anchor"),
        ({"anchor": "no colon"}, "anchor"),
        ({"keep": "pantheon-keep-going@mason.timer,pantheon-keep-going@mason.timer"}, "distinct"),
        ({"keep": "pantheon-persona-quota@mason.timer"}, "not in the seat's target set"),
    ],
)
def test_desired_refusals(desired, match):
    with pytest.raises(HM.PlanRefused, match=match):
        _pause("mason", **desired)


def test_scope_and_observation_refusals():
    request = _request("mason")
    request["scope"] = "system"
    with pytest.raises(HM.PlanRefused, match="seat scope"):
        HM.plan_configure(request)
    request = _request("mason")
    request["observed"] = {}
    with pytest.raises(HM.PlanRefused, match="no harness-manager-run-state seat observation"):
        HM.plan_configure(request)
    with pytest.raises(HM.PlanRefused, match="user bus"):
        _pause("mason", _seat("mason", user_bus=False))
    with pytest.raises(HM.PlanRefused, match="instance.yaml is not the plan's"):
        _pause("mason", _seat("mason", target_manifest_sha256="0" * 64))


@pytest.mark.parametrize("shape", ("paused", "running"))
@pytest.mark.parametrize("persona", ("recovery", "watchman"))
def test_o_recovery_seats_refuse(persona, shape):
    """Recovery D15 (T-5858) (o)/(u): pausing the recovery tier removes recovery."""

    account = ACCOUNTS[persona]
    request = {"scope": f"seat-{account}",
               "desired": {"run_state": shape, "stamp": STAMP, "source_revision": REV, "anchor": ANCHOR},
               "observed": {HM.RUN_STATE_HANDLER: {"account": account, "seat": _seat("mason")}}}
    with pytest.raises(HM.PlanRefused, match=r"recovery tier.*\(o\)"):
        HM.plan_configure(request)


def test_o_recovery_tier_is_read_from_instance_roles():
    manifest = PI.Manifest.load(MANIFEST_PATH)
    tier = sorted(r["persona"] for k, r in manifest.roles.items() if isinstance(r, dict) and HM._is_recovery_role(k))
    assert tier == ["watchman", "recovery"]


def test_o_ho_pause_needs_a_operator_anchor():
    with pytest.raises(HM.PlanRefused, match=r"HO seat.*operator"):
        _pause("orchestrator", anchor="operator:orchestrator-ticket-T-1")
    ops = _pause("orchestrator", anchor="operator:TG7000")
    assert _by_id(ops)["rs-timer-0"]["desired"]["anchor"] == "operator:TG7000"
    assert _pause("mason", anchor="operator:T-5858")  # other seats: any principal


def test_u_a_non_derivable_section_2_unit_refuses():
    """F1 / D-3 / Recovery (u): a live §2 unit no data source declares refuses the plan."""

    for unit in ("pantheon-acp-bridge-backlog@mason.timer", "pantheon-knowledge-sweep.timer", "claude-telegram-research.service",
                 "pantheon-keep-going@orchestrator.timer"):  # the last: another seat's instance in this manager
        seat = _with_unit("mason", unit, _up(unit))
        with pytest.raises(HM.PlanRefused, match="§2 wake path not derivable from instance.yaml; restore could not know it"):
            _pause("mason", seat)
    # disabled + inactive leftovers are not wake paths: no refusal
    seat = _with_unit("mason", "claude-telegram-research.service", dict(RS.DOWN, LoadState="loaded"))
    assert _pause("mason", seat)


@pytest.mark.parametrize("state", ("masked", "static", "linked", "enabled-runtime", "indirect"))
def test_u_masked_static_or_linked_target_refuses(state):
    unit = "pantheon-keep-going@mason.timer"
    seat = _with_unit("mason", unit, dict(_up(unit), UnitFileState=state))
    with pytest.raises(HM.PlanRefused, match="cannot be paused and restored faithfully"):
        _pause("mason", seat)


@pytest.mark.parametrize("fields", ({"UnitFileState": "enabled", "ActiveState": "inactive"},
                                    {"UnitFileState": "disabled", "ActiveState": "active"},
                                    {"UnitFileState": "enabled", "ActiveState": "failed"},
                                    {"UnitFileState": "enabled", "ActiveState": "activating"}))
def test_mixed_or_transitional_target_refuses(fields):
    seat = _with_unit("mason", "pantheon-keep-going@mason.timer", dict(fields, LoadState="loaded"))
    with pytest.raises(HM.PlanRefused, match="mixed or transitional"):
        _pause("mason", seat)


def test_pause_refuses_an_existing_seat_state_record_and_nothing_to_pause():
    with pytest.raises(HM.PlanRefused, match="already carries an ORG-106 seat-state record"):
        _pause("mason", _seat("mason", seat_state={"state": "paused-intake"}))
    seat = _seat("mason")
    for unit in EXPECTED_TARGETS["mason"]:
        seat["units"][unit] = dict(RS.DOWN, LoadState="loaded")
    with pytest.raises(HM.PlanRefused, match="no running target"):
        _pause("mason", seat)


def test_q_a_second_pause_while_a_manifest_is_open_refuses():
    """Recovery D15 (T-5858) (q): a pause halted partway (manifest open, not committed) is
    recovered by rollback or a restore, never by a second pause."""

    sim = _sim("mason")
    ops = _pause("mason")
    _run(sim, ops[:3])  # manifest, seat-state, the first timer: then it halted
    with pytest.raises(HM.PlanRefused, match=r"a pause manifest is open .* never a second pause"):
        _pause("mason", _state(sim, "mason"))
    with pytest.raises(HM.PlanRefused, match="a pause manifest is open"):
        _pause("mason", _seat("mason", manifest="{not json"))


def test_pausing_a_committed_paused_seat_is_a_validated_no_op():
    seat = _state(_paused("mason"), "mason")
    assert seat["run_state"] == "paused"
    assert _pause("mason", seat, stamp="20261102T050000Z") == []
    with pytest.raises(HM.PlanRefused, match="a pause manifest is open"):  # a different keep is not "as desired"
        _pause("mason", seat, keep="pantheon-keep-going@mason.timer")


# --- restore refusals (Recovery (q)/(u)) ------------------------------------------------------------------


def test_u_restore_refuses_a_missing_manifest_on_a_paused_seat():
    """Recovery (q)/(u): a seat that is paused (run_state, or an ORG-106 record) without a valid
    manifest refuses; restore never infers."""

    with pytest.raises(HM.PlanRefused, match="no pause manifest; restore only what a pause recorded"):
        _restore("mason", _seat("mason", run_state="paused"))
    with pytest.raises(HM.PlanRefused, match="no pause manifest"):
        _restore("mason", _seat("mason", seat_state={"state": "paused-intake", "pause_id": "x"}))
    seat = _state(_paused("mason"), "mason")
    seat["manifest"] = None
    with pytest.raises(HM.PlanRefused, match="no pause manifest"):
        _restore("mason", seat)


def test_r1_2_restore_on_a_running_seat_is_a_validated_no_op():
    """Review R1-2 (mason ruling): no manifest, no seat-state record, run_state not paused =
    idempotence, not inference."""

    assert _restore("mason", _seat("mason")) == []  # a fresh seat, never paused
    sim = _paused("mason")
    _run(sim, _restore("mason", _state(sim, "mason")))
    after = _state(sim, "mason")
    assert after["run_state"] == "running"
    assert _restore("mason", after, stamp="20261104T000000Z") == []  # after a full round trip


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(manifest="garbage"),
        lambda s: s["manifest"].update(schema="other/v1"),
        lambda s: s["manifest"].update(persona="artisan"),
        lambda s: s["manifest"]["entries"].append({"unit": "pantheon-keep-going@mason.timer", "phase": "timer", "pre": RS.UP}),
        lambda s: s["manifest"]["entries"][0].update(pre={"UnitFileState": "disabled", "ActiveState": "inactive"}),
        lambda s: s["seat_state"].update(pause_id="rs-suborch1-other"),
        lambda s: s.update(seat_state=None),
        lambda s: s["manifest"]["entries"].append({"unit": "pantheon-acp-bridge@mason.service", "phase": "service", "pre": RS.UP}),
        lambda s: s["manifest"].update(plan_revision="f" * 40),
    ],
    ids=("not-an-object", "schema", "persona", "duplicate-unit", "pre-state", "pause-id-mismatch", "no-seat-state",
         "foreign-unit", "plan-revision-mismatch"),
)
def test_u_restore_refuses_a_malformed_or_mismatched_manifest(mutate):
    seat = _state(_paused("mason"), "mason")
    mutate(seat)
    with pytest.raises(HM.PlanRefused, match="malformed or mismatched"):
        _restore("mason", seat)


def test_restore_refuses_a_unit_neither_paused_nor_at_its_pre_state():
    seat = _state(_paused("mason"), "mason")
    seat["units"]["pantheon-keep-going@mason.timer"]["UnitFileState"] = "masked"
    with pytest.raises(HM.PlanRefused, match="masked|neither its paused state"):
        _restore("mason", seat)


# --- sim: pause / restore round trips --------------------------------------------------------------


@pytest.mark.parametrize("persona", ("mason", "specialist", "orchestrator"))
def test_restore_is_the_exact_inverse_of_pause(persona):
    original = _seat(persona)
    desired = {"anchor": "operator:TG1"} if persona == "orchestrator" else {}
    sim = _paused(persona, copy.deepcopy(original), **desired)
    paused = _state(sim, persona)
    for unit in EXPECTED_TARGETS[persona]:
        assert {k: paused["units"][unit][k] for k in RS.DOWN} == RS.DOWN
    assert paused["run_state"] == "paused" and paused["manifest"] and paused["seat_state"]
    ops = _restore(persona, paused)
    ids = _ids(ops)
    services = [i for i in ids if i.startswith("rs-restore-service-")]
    timers = [i for i in ids if i.startswith("rs-restore-timer-")]
    assert ids == [*services, *timers, "rs-manifest-retire", "rs-restore-commit", "rs-seat-state-clear"]  # D-5
    _run(sim, ops)
    restored = _state(sim, persona)
    assert restored["units"] == original["units"]  # cell for cell
    assert restored["manifest"] is None and restored["seat_state"] is None and restored["run_state"] == "running"
    [tomb] = restored["seat_state_tombstones"]
    assert tomb["pause_id"] == paused["seat_state"]["pause_id"] and tomb["ended_ts"] == "2026-11-03T04:00:00Z"


def test_a_unit_down_before_the_pause_is_never_restored():
    """The 2026-10-12 mason precedent: a consume timer deliberately off stays off."""

    seat = _seat("mason")
    seat["units"]["pantheon-persona-consume@mason.timer"] = dict(RS.DOWN, LoadState="loaded", SubState="dead")
    sim = _paused("mason", copy.deepcopy(seat))
    entries = [e["unit"] for e in _state(sim, "mason")["manifest"]["entries"]]
    assert "pantheon-persona-consume@mason.timer" not in entries
    _run(sim, _restore("mason", _state(sim, "mason")))
    assert _state(sim, "mason")["units"] == seat["units"]


def test_keep_list_is_honoured_on_pause():
    keep = "pantheon-acp-bridge@specialist.service"
    ops = _pause("specialist", keep=keep)
    assert keep not in [op["desired"].get("unit") for op in ops]
    by = _by_id(ops)
    assert keep not in [e["unit"] for e in json.loads(by["rs-manifest"]["desired"]["document"])["entries"]]
    checks = json.loads(by["rs-commit"]["desired"]["postconditions"])
    assert {"check": "unit-fields", "params": {"unit": keep, **RS.UP}} in checks
    [wake] = [c for c in checks if c["check"] == "no-active-wake"]
    assert keep in wake["params"]["allow"].split(",")
    sim = _sim("specialist")
    _run(sim, ops)
    assert {k: _state(sim, "specialist")["units"][keep][k] for k in RS.UP} == RS.UP


def test_keep_list_on_restore_is_the_legitimate_partial():
    """SOP:105-106: restoring part of a seat leaves the rest paused, in the manifest, and the
    seat-state record in place; a later full restore finishes it."""

    original = _seat("specialist")
    sim = _paused("specialist", copy.deepcopy(original))
    keep = "pantheon-watch.timer"
    ops = _restore("specialist", _state(sim, "specialist"), keep=keep)
    assert "rs-seat-state-clear" not in _ids(ops) and keep not in [op["desired"].get("unit") for op in ops]
    _run(sim, ops)
    partial = _state(sim, "specialist")
    assert [e["unit"] for e in partial["manifest"]["entries"]] == [keep]
    assert partial["seat_state"] is not None and partial["run_state"] == "paused"
    assert {k: partial["units"][keep][k] for k in RS.DOWN} == RS.DOWN
    with pytest.raises(HM.PlanRefused, match="a pause manifest is open"):  # Recovery (q)
        _pause("specialist", partial, stamp="20261104T000000Z")
    _run(sim, _restore("specialist", partial, stamp="20261104T000000Z"))
    assert _state(sim, "specialist")["units"] == original["units"] and _state(sim, "specialist")["seat_state"] is None


def test_restore_re_run_after_a_partial_failure_plans_only_the_remainder():
    sim = _paused("mason")
    ops = _restore("mason", _state(sim, "mason"))
    _run(sim, ops[:2])  # two units re-enabled, then it stopped
    again = _restore("mason", _state(sim, "mason"))
    enabled = {op["desired"]["unit"] for op in ops[:2]}
    assert not enabled & {op["desired"].get("unit") for op in again}
    _run(sim, again)
    assert _state(sim, "mason")["units"] == _seat("mason")["units"]


def test_s_commit_reports_paused_with_residue_naming_the_resurrection_paths():
    """Recovery D15 (T-5858) (s) + F2: named, reported, never claimed as paused; no cross-account op."""

    ops = _pause("specialist")
    commit = _by_id(ops)["rs-commit"]
    assert commit["desired"]["outcome"] == "paused-with-residue"
    residue = commit["desired"]["residue"]
    for name in ("pantheon-context-fill-monitor@specialist.timer", "pantheon-activity-monitor@specialist.timer",
                 "pantheon-restart-drain.timer", "runtime-owned"):
        assert name in residue, name
    assert {op["owner"] for op in ops} == {"claude-research"}
    assert not [op for op in ops if "monitor" in op["target"]]
    [check] = [c for c in json.loads(commit["desired"]["postconditions"]) if c["check"] == "residue"]
    assert check["advisory"] is True
    sim = _sim("specialist")
    _run(sim, ops)
    detail = sim.verify_operation(_inst(commit)).detail
    assert "advisory: paused-with-residue" in detail


def test_t_restore_reads_observed_fields_only_and_needs_a_scheduled_consume_timer():
    """Recovery D15 (T-5858) (t): actor-external. No restore op fires or waits on a unit it
    re-enables; the commit asserts the recorded pre-state and the consume timer's next fire."""

    ops = _restore("mason", _state(_paused("mason"), "mason"))
    assert {op["kind"] for op in ops} <= set(RUN_STATE_KINDS)
    commit = _by_id(ops)["rs-restore-commit"]
    checks = json.loads(commit["desired"]["postconditions"])
    assert {"check": "next-elapse", "params": {"unit": "pantheon-persona-consume@mason.timer"}} in checks
    assert all(c["check"] in ("unit-fields", "next-elapse", "no-new-failed", "document") for c in checks)
    sim = _paused("mason")
    seat = _state(sim, "mason")
    sim_state = sim.dump_state()
    sim_state["seats"]["suborch1"]["next_elapse"] = {"pantheon-persona-consume@mason.timer": "n/a"}
    sim.load_state(sim_state)
    restore = _restore("mason", seat)
    for raw in restore[: _ids(restore).index("rs-restore-commit")]:
        sim.execute(_inst(raw))
    _mutation, _ev = sim.execute(_inst(_by_id(restore)["rs-restore-commit"]))
    evidence = sim.verify_operation(_inst(_by_id(restore)["rs-restore-commit"]))
    assert not evidence.ok and "next elapse 'n/a'" in evidence.detail


def _on_halted(sim, op, exc, run_id="01HZZZZZZZZZZZZZZZZZZZZZZZ"):
    """What the engine does after a halt is durable (T-5868 Phase B): call the handler's on_halted."""
    sim.on_halted(run_id, op, exc)


def test_r_drain_waits_never_stops_and_times_out_at_attempt_time():
    """Recovery D15 (T-5858) (r)/(r1): the drain has no effect of its own; a oneshot in flight is
    its timeout, learnt when it runs (preflight admits it). The halt KEEPS the paused-intake
    record, annotates it, and escalates."""

    sim = _sim("mason")
    ops = _pause("mason")
    by = _by_id(ops)
    _run(sim, [op for op in ops if op["operation_id"] in ("rs-manifest", "rs-seat-state") or op["operation_id"].startswith("rs-timer-")])
    state = sim.dump_state()
    state["seats"]["suborch1"]["units"]["pantheon-inbox-consumer@mason.service"]["ActiveState"] = "active"
    sim.load_state(state)
    drain = _inst(by["rs-drain"])
    assert sim.preflight(drain).ok
    record = _state(sim, "mason")["seat_state"]
    with pytest.raises(HMH.EscalatingHalt) as info:
        sim.execute(drain)
    assert "drain timeout after 120s" in info.value.reason and "ABORTED" in info.value.reason
    assert info.value.recipient == "recovery" and info.value.subject == "suborch1"
    assert _state(sim, "mason")["seat_state"] == record  # the handler itself no longer touches the record
    assert info.value.recipients == ("recovery", "orchestrator")  # T-5868 Q3: recovery plus the owner (operator -> orchestrator)
    _on_halted(sim, drain, info.value)
    after = _state(sim, "mason")
    assert after["seat_state"] == RS.halted_record(record, run_id="01HZZZZZZZZZZZZZZZZZZZZZZZ", op_id="rs-drain",
                                                   halted_at=after["seat_state"]["halted_at"])
    assert seat_state.validate_record(after["seat_state"]) == [] and after["seat_state_tombstones"] == []  # kept, never cleared
    assert after["units"]["pantheon-inbox-consumer@mason.service"]["ActiveState"] == "active"  # never stopped
    assert sim._effects(drain, sim.dump_state()["seats"]["suborch1"]) == {}


@pytest.mark.parametrize("persona", sorted(EXPECTED_TARGETS))
def test_r_no_oneshot_is_ever_a_unit_op_target(persona):
    """Recovery (r): a timer's oneshot service is drained (waited on), never stopped. specialist also
    has services (the acp bridge, the codex receiver), so the check is not vacuous there."""

    ops = _pause(persona)
    oneshots = {_oneshot(t) for t in EXPECTED_TARGETS[persona] if t.endswith(".timer")}
    targets = {op["desired"]["unit"] for op in ops if op["kind"] == "harness.run-state-unit"}
    assert oneshots and not oneshots & targets
    if persona == "specialist":
        assert {"pantheon-acp-bridge@specialist.service", "codex-telegram-poller.service"} <= targets
    assert set(_by_id(ops)["rs-drain"]["desired"]["oneshots"].split(",")) == oneshots


def test_r1_a_bare_halted_state_is_invalid_without_its_keys():
    """Recovery D15 (T-5858) (r1), as amended by T-5868 Phase B: the halt is the ORG-106 halted record,
    written by on_halted; a record that merely says state halted (no keys, wrong phase) is invalid."""

    record = json.loads(_by_id(_pause("mason"))["rs-seat-state"]["desired"]["document"])
    assert seat_state.RECORD_STATES == ("paused-intake", "deep-sleep", "halted")
    assert seat_state.validate_record(dict(record, state="halted"))  # no halted keys / phase: invalid
    assert seat_state.validate_record(RS.halted_record(record, run_id="r1", op_id="rs-drain", halted_at="2026-11-03T09:00:00Z")) == []


def test_r1_a_restore_halt_keeps_the_record_and_does_not_escalate():
    sim = _paused("mason")
    seat = _state(sim, "mason")
    ops = _restore("mason", seat)
    state = sim.dump_state()
    first = ops[0]["desired"]["unit"]
    state["seats"]["suborch1"]["unit_stuck"] = {first: "enable-now"}
    sim.load_state(state)
    with pytest.raises(HMH.OperationFailed) as info:
        sim.execute(_inst(ops[0]))
    assert not isinstance(info.value, HMH.EscalatingHalt)
    assert _state(sim, "mason")["seat_state"] == seat["seat_state"]  # still paused, still authorised


# --- sim unit tier: rollback mechanics -----------------------------------------------------------------


def test_sim_unknown_seat_drift_and_foreign_pins_refuse():
    op = _inst(_by_id(_pause("mason"))["rs-timer-0"])
    assert not HMH.HarnessRunStateSim().preflight(op).ok
    sim = _sim("mason")
    mutation, _ev = sim.execute(op)
    state = sim.dump_state()
    state["seats"]["suborch1"]["units"][op.desired["unit"]]["UnitFileState"] = "enabled"  # someone re-enabled it
    sim.load_state(state)
    assert sim.can_rollback(mutation).classification == "drifted"
    seat = _seat("mason")
    seat["pins"] = {"units": [], "files": []}
    assert "outside the data-derived pins" in _sim("mason", seat).preflight(op).detail
    seat = _seat("mason", uid=999)
    assert "user runtime dir" in _sim("mason", seat).preflight(op).detail


def test_sim_rollback_failure_escalates_only_on_the_dark_predicate():
    ops = _pause("mason")
    timer_ops = [op for op in ops if op["operation_id"].startswith("rs-timer-")]
    sim = _sim("mason")
    mutations = {}
    for raw in ops:
        if raw["operation_id"] == "rs-commit":
            break
        mutations[raw["operation_id"]], _ = sim.execute(_inst(raw))
    assert [op["desired"].get("undo_decides_dark") for op in timer_ops] == ["true"] + [None] * (len(timer_ops) - 1)
    state = sim.dump_state()
    # every timer stuck: the newer undos fail PLAIN even though the seat looks dark (review
    # R1-1: older timers are still to come); only the last-undone, rs-timer-0, decides
    state["seats"]["suborch1"]["unit_stuck"] = {op["desired"]["unit"]: "enable-now" for op in timer_ops}
    sim.load_state(state)
    for op in reversed(timer_ops[1:]):
        with pytest.raises(HMH.OperationFailed) as info:
            sim.rollback_mutation(mutations[op["operation_id"]])
        assert not isinstance(info.value, HMH.EscalatingRollbackStop)
    with pytest.raises(HMH.EscalatingRollbackStop) as info:
        sim.rollback_mutation(mutations[timer_ops[0]["operation_id"]])
    assert info.value.recipient == "recovery" and info.value.subject == "suborch1" and "dark" in info.value.reason
    # rs-timer-0 stuck but the others came back: not dark, plain
    state["seats"]["suborch1"]["unit_stuck"] = {timer_ops[0]["desired"]["unit"]: "enable-now"}
    sim.load_state(state)
    for op in reversed(timer_ops[1:]):
        sim.rollback_mutation(mutations[op["operation_id"]])
    with pytest.raises(HMH.OperationFailed) as info:
        sim.rollback_mutation(mutations[timer_ops[0]["operation_id"]])
    assert not isinstance(info.value, HMH.EscalatingRollbackStop)


# --- engine tier (tmp checkout, simulated host) -------------------------------------------------------


def _engine(tmp_path, persona, seat=None):
    account = ACCOUNTS[persona]
    scope = f"seat-{account}"
    repo = GF1._repo(tmp_path, gate_class="operator")
    eng = GF1._engine(tmp_path, repo, deps=False)
    GF1._install_deps(eng, scope)
    eng.backend.handler("harness.run-state-unit").load_state({"seats": {account: seat or _seat(persona)}})
    eng.backend.save()
    return repo, eng, scope


def _rs_state(eng, persona):
    seat = eng.backend.handler("harness.run-state-unit").dump_state()["seats"][ACCOUNTS[persona]]
    return {k: v for k, v in seat.items() if k not in ("writers", "verdicts")}


def _set_seat(eng, persona, **changes):
    handler = eng.backend.handler("harness.run-state-unit")
    state = handler.dump_state()
    state["seats"][ACCOUNTS[persona]].update(changes)
    handler.load_state(state)
    eng.backend.save()


def _desired(shape="paused", stamp=STAMP, **extra):
    return {"run_state": shape, "stamp": stamp, "source_revision": REV, "anchor": ANCHOR} | extra


def _apply(eng, scope, desired, accept=frozenset({GATE})):
    plan = eng.plan("configure", scope, CID, desired=desired)
    return eng.apply("configure", scope, CID, plan_digest=plan.digest, accept_gates=accept, desired=desired)


def _run_id(eng):
    return coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]


def _transcript(eng, run_id):
    store, run_tuple = eng._find_run(run_id)
    with store.open(run_tuple) as handle:
        events = [json.loads(line) for line in handle.read_transcript()]
        return [e for e in events if isinstance(e, dict)], handle.read_record()


def test_engine_real_contract_declares_the_gate_and_refuses_without_it(tmp_path):
    _repo, eng, scope = _engine(tmp_path, "mason")
    before = H.tree_snapshot(eng.backend.root)
    for accept in (None, frozenset({"supervisor-cutover"})):  # (the empty set accepts every operator gate)
        with pytest.raises(EngineRefusal) as info:
            _apply(eng, scope, _desired(), accept=accept)
        assert info.value.code == "gate-unanswered" and GATE in info.value.message
    assert H.tree_snapshot(eng.backend.root) == before


def test_engine_pause_then_restore_round_trip(tmp_path):
    original = _seat("specialist")
    _repo, eng, scope = _engine(tmp_path, "specialist", copy.deepcopy(original))
    [outcome] = _apply(eng, scope, _desired())
    assert outcome.state == "SATISFIED"
    paused = _rs_state(eng, "specialist")
    assert paused["run_state"] == "paused" and paused["seat_state"]["state"] == "paused-intake"
    [result] = eng.verify(scope, CID)
    assert result["checks"] and all(c["ok"] for c in result["checks"]), [c for c in result["checks"] if not c["ok"]]
    with pytest.raises(EngineRefusal) as info:  # a SATISFIED run is absorbing: restore is the forward plan
        eng.rollback(outcome.run_id)
    assert info.value.code == "rollback-refused"
    [outcome] = _apply(eng, scope, _desired("running", RESTORE_STAMP))
    assert outcome.state == "SATISFIED"
    restored = _rs_state(eng, "specialist")
    assert restored["units"] == original["units"] and restored["seat_state"] is None and restored["manifest"] is None
    [result] = eng.verify(scope, CID)
    assert all(c["ok"] for c in result["checks"]), [c for c in result["checks"] if not c["ok"]]


def _halted_run_id(refusal):
    return re.search(r"run (\S+) HALTED", refusal.message).group(1)


def _undo_order(events):
    return [e["op_id"] for e in events if e["event"] == "op.rolled_back"]


def test_t5866_only_the_drain_opts_in_to_rollback_on_halt():
    """T-5866 / Recovery D15 (T-5858) (r)/(r2): the drain timeout aborts the pause, so the drain op
    (and nothing else, mason ruling 2) declares rollback_on_halt."""

    for persona in ("mason", "specialist"):
        assert [op["operation_id"] for op in _pause(persona) if op.get("rollback_on_halt")] == ["rs-drain"]
        assert _by_id(_pause(persona))["rs-drain"]["rollback_on_halt"] is True
    sim = _paused("specialist")
    assert not [op for op in _restore("specialist", _state(sim, "specialist")) if "rollback_on_halt" in op]


def test_engine_u_drain_timeout_aborts_and_auto_rolls_back(tmp_path):
    """Recovery D15 (T-5858) (r)/(r1)/(u) + T-5866: an in-flight oneshot times the drain out; the run
    halts at rs-drain (nothing stopped), writes ONE durable marker and ONE page naming the run_id
    and the rollback command, and the engine then rolls the pause back AT ONCE: every timer
    re-enabled, the seat-state record (still annotated until then) cleared only after every
    timer is back, the manifest last; no marker left, the claim released."""

    seat = _seat("mason")
    seat["units"]["pantheon-keep-going@mason.service"]["ActiveState"] = "active"
    original = copy.deepcopy(seat)
    _repo, eng, scope = _engine(tmp_path, "mason", seat)
    with pytest.raises(EngineRefusal) as info:
        _apply(eng, scope, _desired())
    assert info.value.code == "halted" and "rs-drain" in info.value.message and "drain timeout" in info.value.message
    assert info.value.message.endswith("; rollback_on_halt: ROLLED_BACK")
    run_id = _halted_run_id(info.value)
    back = _rs_state(eng, "mason")
    assert back["units"] == original["units"]  # timers back; the in-flight oneshot never stopped
    assert back["seat_state"] is None and back["manifest"] is None and back["seat_state_tombstones"] == []
    events, record = _transcript(eng, run_id)
    assert record["state"] == "ROLLED_BACK" and record["halted"] is None and record["rollback_incomplete"] is None
    command = f"python3 -m tools.germinate rollback --run {run_id}"
    page, owner_page = [e for e in events if e["event"] == "escalation.page"]  # recovery, then the owner (operator -> orchestrator)
    assert (owner_page["recipient"], owner_page["phase"]) == ("orchestrator", "halt")
    assert (page["op_id"], page["phase"], page["recipient"], page["page_run_id"], page["rollback"]) == (
        "rs-drain", "halt", "recovery", run_id, command)
    names = [e["event"] for e in events]
    assert names.index("run.halted") < names.index("escalation.page") < names.index("rollback.auto")
    order = _undo_order(events)
    timers = [f"rs-timer-{i}" for i in range(len(EXPECTED_TARGETS["mason"]))]
    assert order == list(reversed(timers)) + ["rs-seat-state", "rs-manifest"]  # (r1): record cleared after the timers
    [row] = [r for r in eng.status() if r["component"] == CID]
    assert row["claim"] is None and "escalations" not in row["runs"][f"{scope}/configure"]


def test_engine_u_drain_timeout_halt_state_before_the_auto_rollback(tmp_path, monkeypatch):
    """Recovery D15 (T-5858) (r1), at crash point A (the process dies after the halt, before the
    automatic rollback starts): the seat-state record is KEPT and annotated, not plain paused;
    the marker and page are durable; a restore refuses; the operator rollback(run_id) the page
    names finishes it."""

    from tools.germinate.engine.backend import SimulatedKill
    from tools.germinate.engine.executor import Engine

    seat = _seat("mason")
    seat["units"]["pantheon-keep-going@mason.service"]["ActiveState"] = "active"
    original = copy.deepcopy(seat)
    _repo, eng, scope = _engine(tmp_path, "mason", seat)

    def die(run_id):
        raise SimulatedKill("killed before the auto-rollback started")

    monkeypatch.setattr(eng, "rollback", die)
    with pytest.raises(SimulatedKill):
        _apply(eng, scope, _desired())
    run_id = _run_id(eng)
    halted = _rs_state(eng, "mason")
    assert all(halted["units"][t]["UnitFileState"] == "disabled" for t in EXPECTED_TARGETS["mason"])
    assert halted["units"]["pantheon-keep-going@mason.service"]["ActiveState"] == "active"  # never stopped
    assert halted["manifest"] is not None and halted["seat_state_tombstones"] == []  # (r1): kept, never cleared
    assert halted["seat_state"]["state"] == "halted" and halted["seat_state"]["pause_id"] == f"rs-suborch1-{STAMP}"
    assert halted["seat_state"]["op_id"] == "rs-drain" and halted["seat_state"]["run_id"] == run_id  # ... the ORG-106 halted record
    with pytest.raises(HM.PlanRefused, match=r"HALTED at rs-drain; recover it with rollback\(run_id\)"):
        _restore("mason", halted)
    events, record = _transcript(eng, run_id)
    command = f"python3 -m tools.germinate rollback --run {run_id}"
    marker, owner_marker = record["halted"]["escalations"]  # T-5868 Q3: one entry per recipient, recovery then the owner
    assert (owner_marker["recipient"], owner_marker["op_id"]) == ("orchestrator", "rs-drain")  # operator -> orchestrator
    assert marker == {"op_id": "rs-drain", "subject": "suborch1", "recipient": "recovery", "rollback": command,
                      "reason": marker["reason"]} and "drain timeout" in marker["reason"]
    assert [e["op_id"] for e in events if e["event"] == "escalation.page"] == ["rs-drain", "rs-drain"]
    [row] = [r for r in eng.status() if r["component"] == CID]
    assert row["runs"][f"{scope}/configure"]["escalations"] == [marker, owner_marker]
    assert Engine.rollback(eng, run_id).state == "ROLLED_BACK"
    back = _rs_state(eng, "mason")
    assert back["units"] == original["units"] and back["seat_state"] is None and back["manifest"] is None
    events, _record = _transcript(eng, run_id)
    assert len([e for e in events if e["event"] == "escalation.page"]) == 2  # one per recipient, once each


def test_engine_failed_commit_halts_and_rollback_restores(tmp_path):
    """SOP §4 verify 3 as a hard commit check: a unit that failed after the plan was made
    (not one the reset-failed op owns) halts the commit; rollback restores every cell."""

    original = _seat("mason")
    _repo, eng, scope = _engine(tmp_path, "mason", copy.deepcopy(original))
    _set_seat(eng, "mason", fails_during={"rs-timer-1": ["pantheon-other@mason.service"]})
    with pytest.raises(EngineRefusal) as info:
        _apply(eng, scope, _desired())
    assert info.value.code == "halted" and "rs-commit" in info.value.message and "no-new-failed" in info.value.message
    assert "EscalatingHalt" in info.value.message  # (r1): an apply-time postcondition halt on a pause escalates
    assert _rs_state(eng, "mason")["seat_state"]["op_id"] == "rs-commit"
    _events, record = _transcript(eng, _run_id(eng))
    assert [m["op_id"] for m in record["halted"]["escalations"]] == ["rs-commit", "rs-commit"]
    assert eng.rollback(_run_id(eng)).state == "ROLLED_BACK"
    back = _rs_state(eng, "mason")
    assert back["seat_state"] is None and back["manifest"] is None
    assert {k: v for k, v in back["units"].items() if k != "pantheon-other@mason.service"} == original["units"]


def test_engine_restore_partial_failure_rolls_back_to_paused(tmp_path):
    _repo, eng, scope = _engine(tmp_path, "specialist")
    [outcome] = _apply(eng, scope, _desired())
    paused = _rs_state(eng, "specialist")
    _set_seat(eng, "specialist", unit_stuck={"pantheon-acp-bridge@specialist.service": "enable-now"})
    with pytest.raises(EngineRefusal) as info:
        _apply(eng, scope, _desired("running", RESTORE_STAMP))
    assert info.value.code == "halted"
    assert eng.rollback(_run_id(eng)).state == "ROLLED_BACK"
    back = _rs_state(eng, "specialist")
    assert back["units"] == paused["units"] and back["manifest"] == paused["manifest"]
    assert back["seat_state"] == paused["seat_state"]  # still authorised: the seat is paused


def test_engine_t_restore_commit_failure_never_clears_seat_state(tmp_path):
    _repo, eng, scope = _engine(tmp_path, "mason")
    _apply(eng, scope, _desired())
    _set_seat(eng, "mason", next_elapse={"pantheon-persona-consume@mason.timer": "n/a"})
    with pytest.raises(EngineRefusal) as info:
        _apply(eng, scope, _desired("running", RESTORE_STAMP))
    assert info.value.code == "halted" and "rs-restore-commit" in info.value.message
    assert _rs_state(eng, "mason")["seat_state"] is not None  # (t): cleared only after the checks pass
    _assert_plain_restore_halt(eng, "mason", info.value)
    run_id = _run_id(eng)
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    assert _rs_state(eng, "mason")["seat_state"]["state"] == "paused-intake"
    _set_seat(eng, "mason", next_elapse={})
    [outcome] = _apply(eng, scope, _desired("running", "20261104T000000Z"))
    assert outcome.state == "SATISFIED" and _rs_state(eng, "mason")["seat_state"] is None


def _assert_plain_restore_halt(eng, persona, refusal):
    """Review R2 F1: a RESTORE halt is plain: no EscalatingHalt, no annotation, no marker, no page."""

    assert "EscalatingHalt" not in refusal.message and "postcondition unmet" in refusal.message
    assert (_rs_state(eng, persona)["seat_state"] or {}).get("state") != "halted"
    events, record = _transcript(eng, _run_id(eng))
    assert "escalations" not in record["halted"]
    assert not [e for e in events if e["event"] == "escalation.page"]


def test_engine_r2_f1_a_partial_restore_commit_failure_halts_plainly_and_recovers(tmp_path):
    """Review R2 F1's probe: specialist paused, then a restore keeping pantheon-watch.timer whose
    commit fails (the consume timer has no next elapse). The partial restore commits
    run_state "paused" with the same pause_id, but it is a RESTORE run (provenance.run), so
    the halt is plain; rollback(run_id) returns the seat to its paused state, and a restore
    then succeeds (no wedge)."""

    _repo, eng, scope = _engine(tmp_path, "specialist")
    _apply(eng, scope, _desired())
    paused = _rs_state(eng, "specialist")
    _set_seat(eng, "specialist", next_elapse={"pantheon-persona-consume@specialist.timer": "n/a"})
    with pytest.raises(EngineRefusal) as info:
        _apply(eng, scope, _desired("running", RESTORE_STAMP, keep="pantheon-watch.timer"))
    assert info.value.code == "halted" and "rs-restore-commit" in info.value.message
    _assert_plain_restore_halt(eng, "specialist", info.value)
    assert eng.rollback(_run_id(eng)).state == "ROLLED_BACK"
    back = _rs_state(eng, "specialist")
    assert back["seat_state"] == paused["seat_state"] and back["manifest"] == paused["manifest"]
    assert back["units"] == paused["units"]
    _set_seat(eng, "specialist", next_elapse={})
    [outcome] = _apply(eng, scope, _desired("running", "20261104T000000Z"))
    assert outcome.state == "SATISFIED"
    done = _rs_state(eng, "specialist")
    assert done["seat_state"] is None and done["manifest"] is None and done["units"] == _seat("specialist")["units"]


def test_r2_f1_every_op_carries_its_run_kind_from_the_planner():
    assert {op["provenance"]["run"] for op in _pause("specialist")} == {"pause"}
    sim = _paused("specialist")
    for keep in (None, "pantheon-watch.timer"):
        assert {op["provenance"]["run"] for op in _restore("specialist", _state(sim, "specialist"), keep=keep)} == {"restore"}


def _draining_seat():
    seat = _seat("mason")
    seat["units"]["pantheon-mailbox-wake@mason.service"]["ActiveState"] = "activating"
    return seat


def _assert_pause_kept_on_record(eng):
    """T-5866 guard (Recovery D15 (T-5858) (r1) as tightened, mason ruling 1): a half-undone pause
    keeps its annotated seat-state record and its manifest, so a restore keeps refusing."""

    kept = _rs_state(eng, "mason")
    assert kept["seat_state"]["state"] == "halted" and kept["seat_state"]["op_id"] == "rs-drain"
    assert kept["manifest"] is not None and kept["seat_state_tombstones"] == []
    with pytest.raises(HM.PlanRefused, match="HALTED at rs-drain"):
        _restore("mason", kept)


def test_engine_rollback_that_leaves_the_seat_dark_escalates_once(tmp_path):
    """D-8: every wake timer refuses to re-enable during the (automatic, T-5866) rollback of an
    aborted pause, so the seat cannot wake: EscalatingRollbackStop -> one page to recovery, durable
    marker, beside the forward halt's carried marker; the pause stays on record."""

    seat = _draining_seat()
    _repo, eng, scope = _engine(tmp_path, "mason", seat)
    _set_seat(eng, "mason", unit_stuck={t: "enable-now" for t in EXPECTED_TARGETS["mason"]})
    with pytest.raises(EngineRefusal) as info:
        _apply(eng, scope, _desired())
    assert info.value.code == "halted" and "; rollback_on_halt: rollback-incomplete: " in info.value.message
    run_id = _run_id(eng)  # the claim stays held (D-Y)
    events, record = _transcript(eng, run_id)
    assert record["state"] == "ROLLBACK_INCOMPLETE"
    halt_marker, halt_owner_marker, dark_marker = record["rollback_incomplete"]["escalations"]
    assert halt_owner_marker["op_id"] == "rs-drain" and halt_owner_marker["recipient"] == "orchestrator"
    assert halt_marker["op_id"] == "rs-drain" and "rollback" in halt_marker  # the forward halt's, carried (T-5866)
    assert dark_marker["op_id"] == "rs-timer-0"  # review R1-1: the LAST-undone wake timer decides
    assert dark_marker["recipient"] == "recovery" and dark_marker["subject"] == "suborch1" and "dark" in dark_marker["reason"]
    outstanding = record["rollback_incomplete"]["outstanding_inverses"]
    assert set(outstanding) == {f"rs-timer-{i}" for i in range(4)} | {"rs-seat-state", "rs-manifest"}
    _assert_pause_kept_on_record(eng)
    pages = [e["op_id"] for e in events if e["event"] == "escalation.page"]
    assert pages == ["rs-drain", "rs-drain", "rs-timer-0"]  # the forward halt (r1) to recovery + owner, then the dark undo (D-8): once each
    with pytest.raises(EngineRefusal):  # a retry while still blocked pages no second time
        eng.rollback(run_id)
    events, record = _transcript(eng, run_id)
    assert [e["op_id"] for e in events if e["event"] == "escalation.page"] == pages
    assert [m["op_id"] for m in record["rollback_incomplete"]["escalations"]] == ["rs-drain", "rs-drain", "rs-timer-0"]
    _set_seat(eng, "mason", unit_stuck={})
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    back = _rs_state(eng, "mason")
    assert back["units"] == seat["units"] and back["seat_state"] is None and back["manifest"] is None


def test_engine_r1_1_only_the_newest_timer_stuck_is_no_escalation(tmp_path):
    """Review R1-1's probe: a drain-timeout halt, then only rs-timer-3 refuses to re-enable.
    The older timers come back, so the seat is NOT dark: no rollback escalation (the forward
    (r1) halt page is the only page, its marker carried). T-5866: the seat-state record and
    the manifest are kept (outstanding) until rs-timer-3 is back too; then a retry clears them."""

    seat = _draining_seat()
    _repo, eng, scope = _engine(tmp_path, "mason", seat)
    newest = sorted(EXPECTED_TARGETS["mason"])[-1]
    _set_seat(eng, "mason", unit_stuck={newest: "enable-now"})
    with pytest.raises(EngineRefusal) as info:
        _apply(eng, scope, _desired())
    assert "; rollback_on_halt: rollback-incomplete: " in info.value.message
    run_id = _run_id(eng)
    events, record = _transcript(eng, run_id)
    assert record["rollback_incomplete"]["outstanding_inverses"] == ["rs-timer-3", "rs-seat-state", "rs-manifest"]
    assert [m["op_id"] for m in record["rollback_incomplete"]["escalations"]] == ["rs-drain", "rs-drain"]  # the halt's only (recovery + owner)
    assert [e["op_id"] for e in events if e["event"] == "escalation.page"] == ["rs-drain", "rs-drain"]
    units = _rs_state(eng, "mason")["units"]
    assert sum(units[t]["ActiveState"] == "active" for t in EXPECTED_TARGETS["mason"]) == 3
    _assert_pause_kept_on_record(eng)
    _set_seat(eng, "mason", unit_stuck={})
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    back = _rs_state(eng, "mason")
    assert back["units"] == seat["units"] and back["seat_state"] is None and back["manifest"] is None
    events, _record = _transcript(eng, run_id)
    assert [e["op_id"] for e in events if e["event"] == "escalation.page"] == ["rs-drain", "rs-drain"]


def test_engine_rollback_failure_with_a_live_wake_timer_is_plain(tmp_path):
    _repo, eng, scope = _engine(tmp_path, "mason", _draining_seat())
    first = sorted(EXPECTED_TARGETS["mason"])[0]  # rs-timer-0: undone LAST, after the others are back
    _set_seat(eng, "mason", unit_stuck={first: "enable-now"})
    with pytest.raises(EngineRefusal) as info:
        _apply(eng, scope, _desired())
    assert "; rollback_on_halt: rollback-incomplete: " in info.value.message
    _events, record = _transcript(eng, _run_id(eng))
    marks = record["rollback_incomplete"]["escalations"]
    assert [(m["op_id"], "rollback" in m) for m in marks] == [("rs-drain", True)] * 2  # no rollback-phase (dark) marker
    _assert_pause_kept_on_record(eng)


def test_sim_t5866_guard_keeps_the_pause_record_until_every_manifest_unit_is_back():
    """Handler tier: undoing a pause's seat-state set or manifest write refuses (plain, so the
    op stays outstanding) while a manifest unit is not back at enabled/active."""

    ops = _pause("mason")
    sim = _sim("mason")
    mutations = {}
    for raw in ops:
        if raw["operation_id"] == "rs-commit":
            break
        mutations[raw["operation_id"]], _ = sim.execute(_inst(raw))
    for op_id in ("rs-seat-state", "rs-manifest"):
        with pytest.raises(HMH.OperationFailed, match="manifest units not yet restored"):
            sim.rollback_mutation(mutations[op_id])
    assert _state(sim, "mason")["seat_state"] is not None and _state(sim, "mason")["manifest"] is not None
    for op_id in reversed(list(mutations)):
        sim.rollback_mutation(mutations[op_id])  # newest first, as the engine does
    after = _state(sim, "mason")
    assert after["units"] == _seat("mason")["units"]
    assert after["seat_state"] is None and after["manifest"] is None


# --- U1 / U4 outputs unchanged; dispatch ------------------------------------------------------------


#: The last commit whose harness-manager.py predates U5 (origin/main 076a8f7932, merged into
#: agent/T-5858 at 6dbc83f7d0; it carries U1, U2 and U4). Review R1-4: the guard is
#: DIFFERENTIAL, that planner and this one over the same checkout, instance.yaml and
#: requests, so it never skips when instance.yaml moves. The ci.yml test job checks out
#: with fetch-depth 0, so the pinned commit is always present; if git cannot produce it the
#: test FAILS, never skips.
_PRE_U5 = "076a8f7932"


def _pre_u5_planner(tmp_path):
    import subprocess

    shown = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{_PRE_U5}:tools/germinate/components/harness-manager.py"],
        capture_output=True, text=True, check=True,
    )
    source = tmp_path / "harness_manager_pre_u5.py"
    source.write_text(shown.stdout, encoding="utf-8")
    module = _load(source, "_t5858_pre_u5_harness_manager")
    assert not hasattr(module, "plan_run_state")  # really the pre-U5 planner
    return module


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


def test_real_u1_plan_install_and_u4_plan_supervisor_outputs_are_unchanged(tmp_path):
    """The pre-U5 planner module imports the CURRENT shared helpers (e.g.
    harness_manager_supervisor.py, tools.persona_installer), so this guards the planner
    module only; those helpers have their own tests (review R2 NIT 4)."""

    from tools.germinate.components.tests import test_harness_manager_supervisor as SV

    old = _pre_u5_planner(tmp_path)
    _grant_t5480_kind_mapping(old)
    t5480_kinds_seen = set()
    for persona in ("mason", "specialist", "orchestrator"):
        request = {"scope": f"seat-{ACCOUNTS[persona]}", "desired": {"source_revision": REV}}
        new_install = HM.plan_install(copy.deepcopy(request))
        assert new_install == old.plan_install(copy.deepcopy(request)), persona
        t5480_kinds_seen |= {op["kind"] for op in new_install} & set(_T5480_KINDS)
    assert t5480_kinds_seen == set(_T5480_KINDS)  # the granted kind is really exercised
    for persona in ("specialist", "recovery"):
        for request in (SV._request(persona),
                        SV._request(persona, SV._post_cutover_seat(persona), supervisor="bespoke", source_revision=None)):
            new_ops = HM.plan_configure(copy.deepcopy(request))
            # T-6047 intended change (T-5956 parity): strip exactly desired.companion_snapshot from the
            # companion STOP ops; everything else must stay byte-equal to the base
            for op in new_ops:
                if op["kind"] == "harness.companion-unit" and op["desired"]["action"] == "stop":
                    assert op["desired"].pop("companion_snapshot")
            assert new_ops and new_ops == old.plan_configure(copy.deepcopy(request)), persona


def test_plan_configure_dispatch_leaves_sub_ops_a_and_b_alone():
    from tools.germinate.components.tests import test_harness_manager_supervisor as SV

    request = SV._request("recovery")
    assert HM.plan_configure(copy.deepcopy(request)) == HM.plan_supervisor(copy.deepcopy(request))
    with pytest.raises(HM.PlanRefused, match="configure run_state is its own run"):
        HM.plan_configure(SV._request("recovery", run_state="paused"))


# --- kind classification, contract and dark pins ------------------------------------------------------


def test_real_run_state_kinds_are_one_isolated_component_local_block():
    kinds = read_op_kinds(CID, HANDLERS)
    block = {k: v for k, v in kinds.items() if v[0] == "harness.run-state"}
    assert block == {k: ("harness.run-state", "seat") for k in RUN_STATE_KINDS}
    order = list(kinds)
    first = order.index(RUN_STATE_KINDS[0])
    assert order[first:first + len(RUN_STATE_KINDS)] == list(RUN_STATE_KINDS)
    assert "harness.run-state" not in RESERVED_CLASSES
    registry = load_repo_op_registry(REPO_ROOT)
    for kind in RUN_STATE_KINDS:
        assert component_kind_errors(kind, "harness.run-state", "seat") == []
        entry = registry.get(kind)
        assert entry.privilege == "seat" and not entry.broker_admitted and not entry.substrate
    assert HMH.HarnessRunStateSim.kinds == frozenset(RUN_STATE_KINDS)
    assert HM._RUN_STATE_KINDS == frozenset(RUN_STATE_KINDS)


def test_real_contract_declares_the_gate_schema_and_privilege():
    contract = load_contract(COMPONENTS / "harness-manager.yaml", load_repo_op_registry(REPO_ROOT))
    gate = contract.gates[GATE]
    assert gate.approval_class == "operator" and gate.irreversible is False
    assert contract.desired_schema["run_state"].enum == ("running", "paused")
    assert {"keep", "anchor"} <= set(contract.desired_schema)
    assert set(RUN_STATE_KINDS) <= contract.privilege["seat"]


def test_dark_pins_live_actuation_false_and_no_flag():
    from tools import host_broker_install
    from tools.germinate.components import pantheon_config_git
    from tools.germinate.host_substrate import ports

    assert ports.LIVE_ACTUATION_ENABLED is False
    assert host_broker_install.LIVE_ACTUATION_ENABLED is False
    assert pantheon_config_git.LIVE_ACTUATION_ENABLED is False
    for path in (PLANNER, HANDLERS, HELPER):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {t.id for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign))
                 for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Name)}
        assert not any(name.startswith("LIVE_ACTUATION") for name in names), path
        assert "os.environ" not in path.read_text(encoding="utf-8"), path


@pytest.mark.parametrize("path", (PLANNER, HANDLERS, HELPER), ids=lambda p: p.name)
def test_no_live_surface_is_imported(path):
    """Never the live ORG-106 writer (tools.harness) nor a host backend (Recovery D15 (T-5858) (p))."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            if node.module == "tools":
                names.update(f"tools.{a.name}" for a in node.names)
    banned = ("tools.runtime_controller", "tools.persona_installer_apply", "subprocess", "tools.germinate.host_substrate",
              "tools.user_systemd", "tools.harness", "tools.seat_state_janitor", "os")
    assert not [n for n in names if any(n == b or n.startswith(b + ".") for b in banned)]


def test_untested_live_is_in_every_docstring():
    for path in (PLANNER, HELPER):
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
        assert "UNTESTED-LIVE" in doc and "first live fire is supervised" in doc and "reclassified" in doc
    assert "UNTESTED-LIVE" in HMH.HarnessRunStateSim.__doc__


# --- T-5862: the per-seat §2 units declared in data (run_state_units + schedules) ----------


def _real_targets(persona):
    manifest = PI.Manifest.load(MANIFEST_PATH)
    _key, role = HM._role_key(manifest, persona)
    return HM._rs_targets(manifest, persona, REV, role)


def test_t5862_real_sources_name_the_declared_units():
    """(A) The planner resolves each newly declared unit from its one data source."""

    targets, oneshots, _residue = _real_targets("specialist")
    assert targets["pantheon-acp-bridge-backlog@specialist.timer"] == "instance.yaml run_state_units"
    assert targets["specialist-a2a.service"] == "instance.yaml run_state_units"
    assert targets["pantheon-knowledge-sweep.timer"] == "schedules/specialist.yaml"
    assert targets["pantheon-watch-digest.timer"] == "schedules/specialist.yaml"
    assert oneshots["pantheon-acp-bridge-backlog@specialist.timer"] == "pantheon-acp-bridge-backlog@specialist.service"
    assert oneshots["pantheon-knowledge-sweep.timer"] == "pantheon-knowledge-sweep.service"
    assert "specialist-a2a.service" not in oneshots
    targets, oneshots, _residue = _real_targets("archivist")
    assert targets["pantheon-acp-bridge-backlog@archivist.timer"] == "instance.yaml run_state_units"
    assert oneshots["pantheon-acp-bridge-backlog@archivist.timer"] == "pantheon-acp-bridge-backlog@archivist.service"


@pytest.mark.parametrize("persona", ("specialist", "archivist"))
def test_t5862_declared_units_pause_in_sop_order_and_restore_exactly(persona):
    """(A) specialist and archivist are now pausable: the declared timers go down before the
    drain, specialist-a2a after it, and restore puts every unit back cell for cell."""

    ops = _pause(persona)
    by = _by_id(ops)
    ids = _ids(ops)
    drain = ids.index("rs-drain")
    unit_ops = {by[i]["desired"]["unit"]: ids.index(i) for i in ids if by[i]["kind"] == "harness.run-state-unit"}
    for unit in T5862_DECLARED[persona]:
        assert (unit_ops[unit] < drain) == unit.endswith(".timer"), unit
        if unit.endswith(".timer"):
            assert _oneshot(unit) in by["rs-drain"]["desired"]["oneshots"].split(",")
    original = _seat(persona)
    sim = _paused(persona, copy.deepcopy(original))
    paused = _state(sim, persona)
    for unit in T5862_DECLARED[persona]:
        assert {k: paused["units"][unit][k] for k in RS.DOWN} == RS.DOWN
    _run(sim, _restore(persona, paused))
    assert _state(sim, persona)["units"] == original["units"]


def test_t5862_a_declared_unit_absent_from_the_seat_is_not_paused():
    """A declared unit the seat does not run is simply not a pause target."""

    seat = _seat("archivist")
    del seat["units"]["pantheon-acp-bridge-backlog@archivist.timer"]
    del seat["units"]["pantheon-acp-bridge-backlog@archivist.service"]
    paused = [op["desired"]["unit"] for op in _pause("archivist", seat) if op["kind"] == "harness.run-state-unit"]
    assert "pantheon-acp-bridge-backlog@archivist.timer" not in paused and paused


@pytest.mark.parametrize("persona, unit", [
    ("mason", "pantheon-acp-bridge-backlog@mason.timer"),
    ("mason", "specialist-a2a.service"),
    ("mason", "pantheon-watch-digest.timer"),
    ("artisan", "pantheon-knowledge-sweep.timer"),
    ("artisan", "pantheon-acp-bridge@artisan.service"),
    ("archivist", "pantheon-acp-bridge@archivist.service"),  # retired 2026-09-20 (T-3832): undeclared
])
def test_t5862_an_undeclared_live_section_2_unit_still_refuses(persona, unit):
    """(B) Fail closed stays: a live §2 unit no data source declares for THIS seat refuses."""

    with pytest.raises(HM.PlanRefused, match="§2 wake path not derivable from instance.yaml"):
        _pause(persona, _with_unit(persona, unit, _up(unit)))


@pytest.mark.parametrize("raw, match", [
    ("specialist-a2a.service", "must be a list"),
    ({"unit": "specialist-a2a.service"}, "must be a list"),
    (["specialist-a2a.service"], "must be a mapping"),
    ([{"oneshot": "x.service"}], "must be a mapping"),
    ([{"unit": "specialist-a2a.service", "class": "service"}], "must be a mapping"),
    ([{"unit": "specialist-a2a.service", "disposition": "keep"}], "must be a mapping"),
    ([{"unit": "specialist a2a.service"}], "not an exact unit name"),
    ([{"unit": "specialist-a2a"}], "not an exact unit name"),
    ([{"unit": 7}], "not an exact unit name"),
    ([{"unit": "specialist-a2a.service"}, {"unit": "specialist-a2a.service"}], "twice"),
    ([{"unit": "archivist-state-snapshot.timer"}], "not a §2 pause-category unit"),
    ([{"unit": "exocortex.service"}], "not a §2 pause-category unit"),
    ([{"unit": "dbus.service"}], "not a §2 pause-category unit"),
    ([{"unit": "pantheon-acp-bridge-backlog@mason.timer"}], "belongs to 'mason'"),
    # T-5862 review F1: a declared oneshot override is refused outright, whatever it names.
    ([{"unit": "specialist-a2a.service", "oneshot": "specialist-a2a.service"}], "exactly one key"),
    ([{"unit": "pantheon-knowledge-sweep.timer", "oneshot": "pantheon-knowledge-sweep.service"}], "exactly one key"),
    ([{"unit": "pantheon-knowledge-sweep.timer", "oneshot": None}], "exactly one key"),
    ([{"unit": "pantheon-acp-bridge-backlog@specialist.timer", "oneshot": "pantheon-acp-bridge@specialist.service"}],
     "exactly one key"),
    ([{"unit": "pantheon-acp-bridge-backlog@specialist.timer", "oneshot": "pantheon-runtime-controller@artisan.service"}],
     "exactly one key"),
])
def test_t5862_schema_rejects_malformed_entries(raw, match):
    """(C) The run_state_units schema is closed; anything else is refused, never guessed."""

    with pytest.raises(ValueError, match=match):
        RS.parse_run_state_units(raw, persona="specialist")


def test_t5862_schema_accepts_the_valid_shapes():
    assert RS.parse_run_state_units(None, persona="specialist") == ()
    assert RS.parse_run_state_units([], persona="specialist") == ()
    assert RS.parse_run_state_units([
        {"unit": "pantheon-acp-bridge-backlog@specialist.timer"},
        {"unit": "pantheon-knowledge-sweep.timer"},
        {"unit": "specialist-a2a.service"},
    ], persona="specialist") == (
        ("pantheon-acp-bridge-backlog@specialist.timer", "pantheon-acp-bridge-backlog@specialist.service"),
        ("pantheon-knowledge-sweep.timer", "pantheon-knowledge-sweep.service"),
        ("specialist-a2a.service", None),
    )


def test_t5862_the_planner_refuses_a_malformed_or_doubly_declared_entry():
    """(C) A bad role entry refuses the plan, and a unit has ONE data source, never two."""

    manifest = PI.Manifest.load(MANIFEST_PATH)
    _key, role = HM._role_key(manifest, "specialist")
    with pytest.raises(HM.PlanRefused, match="instance.yaml specialist: run_state_units must be a list"):
        HM._rs_targets(manifest, "specialist", REV, dict(role, run_state_units="specialist-a2a.service"))
    for unit in ("pantheon-acp-bridge@specialist.service", "pantheon-watch.timer", "pantheon-keep-going@specialist.timer"):
        doubled = dict(role, run_state_units=[*role["run_state_units"], {"unit": unit}])
        with pytest.raises(HM.PlanRefused, match=f"run_state_units {re.escape(unit)} is already declared by"):
            HM._rs_targets(manifest, "specialist", REV, doubled)


def test_t5862_every_real_role_run_state_units_entry_is_valid():
    """(D) CI validation of the real instance.yaml: every role's run_state_units parses, and
    only the two seats T-5862 declares carry the key."""

    manifest = PI.Manifest.load(MANIFEST_PATH)
    carriers = {}
    for _key, role in manifest.roles.items():
        if isinstance(role, dict) and "run_state_units" in role:
            persona = role["persona"]
            carriers[persona] = [u for u, _o in RS.parse_run_state_units(role["run_state_units"], persona=persona)]
    assert carriers == {
        "specialist": ["pantheon-acp-bridge-backlog@specialist.timer", "specialist-a2a.service"],
        "archivist": ["pantheon-acp-bridge-backlog@archivist.timer"],
    }


def test_t5862_artisan_pilot_unit_set_is_exactly_the_installer_wake_timers():
    """(D) artisan, the first live pilot seat: its whole §2 set is the four installer wake timers
    (the 2026-10-04 pause manifest, knowledge/episodic/2026-10-12-seat-pause-mason-artisan/)."""

    targets, oneshots, residue = _real_targets("artisan")
    assert sorted(targets) == sorted(_wake("artisan")) and set(targets.values()) == {"installer plan"}
    assert oneshots == {t: _oneshot(t) for t in _wake("artisan")}
    assert residue == ["seat-local pantheon-restart-drain.timer stays armed (resurrection path, excluded: mason ruling F3)"]


def test_t5862_snapshot_timers_and_the_retired_app_are_never_targets():
    """(E) archivist's snapshot timers are not wake paths (never touched); exocortex.service
    (retired with the app, T-5064) stays a judgement app that is only reported."""

    for unit in ("archivist-state-snapshot.timer", "exocortex-data-snapshot.timer", "pantheon-agent-state-snapshot.timer"):
        assert RS.classify(unit) is None, unit
    assert RS.classify("exocortex.service") == ("seat apps", RS.JUDGEMENT)
    targets, _o, _r = _real_targets("archivist")
    assert not {"exocortex.service", "pantheon-acp-bridge@archivist.service"} & set(targets)
    seat = _seat("archivist")
    for unit in ("archivist-state-snapshot.timer", "exocortex-data-snapshot.timer"):
        seat["units"][unit] = _up(unit)
    seat["units"]["exocortex.service"] = _up("exocortex.service")
    ops = _pause("archivist", seat)
    touched = {op["desired"].get("unit") for op in ops}
    assert not touched & {"archivist-state-snapshot.timer", "exocortex-data-snapshot.timer", "exocortex.service"}
    assert "seat app exocortex.service left running (SOP §2 judgement)" in _by_id(ops)["rs-commit"]["desired"]["residue"]


def test_t5862_procedure_named_units_are_on_their_sop_line():
    """(E) specialist-a2a is classified from the SOP's own procedure text (3.3), not invented."""

    lines = SOP.read_text(encoding="utf-8").splitlines()
    for stem, (line, basis) in RS.PROCEDURE_NAMED.items():
        assert stem in lines[line - 1] and basis
        assert stem not in RS.ADDITIONS
    assert RS.classify("specialist-a2a.service") == ("inbound bridges", RS.PAUSE)


# --- T-5918: a U5 pause vetoes fleet parity remediation (self-contained) -----------------------


def _t5918_vetoes(record):
    """What pantheon-parity-remediate decides for this seat-state cell: the live reader's
    parse (tools.seat_state.parse_record) folded through vetoes_repair's union. No record
    (None) means no veto."""

    if record is None:
        return False
    c = seat_state.parse_record(record)
    assert c.fault is None, c.fault
    return bool(c.remediation_vetoed or c.repair_vetoed)


def test_t5918_the_planners_record_is_a_repair_vetoing_run_state_pause():
    """The record U5 really writes carries the marker the reader keys on (pause_id), so the
    veto cannot drift away from the writer; and it changes nothing the monitors read."""

    record = json.loads(_by_id(_pause("mason"))["rs-seat-state"]["desired"]["document"])
    assert seat_state.RUN_STATE_PAUSE_KEY in record and seat_state.is_run_state_pause(record)
    c = seat_state.parse_record(record)
    assert c.repair_vetoed and not c.remediation_vetoed and not c.launch_blocked and c.intake_blocked
    assert _t5918_vetoes(record)


def _t5918_step(sim, raw):
    op = _inst(raw)
    assert sim.preflight(op).ok, raw["operation_id"]
    _mutation, evidence = sim.execute(op)
    assert evidence.ok and sim.verify_operation(op).ok, raw["operation_id"]


def test_t5918_veto_holds_from_the_record_write_until_the_record_clear():
    """Recovery D15 (T-5858) (p): the vetoing record is written before the first unit op and
    cleared last, so remediation is vetoed for every op that leaves a timer disabled."""

    sim = _sim("mason")
    ops = _pause("mason", _state(sim, "mason"))
    seen = []
    for raw in ops:
        assert _t5918_vetoes(_state(sim, "mason")["seat_state"]) == (raw["operation_id"] not in ("rs-manifest", "rs-seat-state")), raw["operation_id"]
        _t5918_step(sim, raw)
        seen.append((raw["operation_id"], _t5918_vetoes(_state(sim, "mason")["seat_state"])))
    assert seen[0] == ("rs-manifest", False) and seen[1] == ("rs-seat-state", True)
    assert all(vetoed for _id, vetoed in seen[1:])  # paused: vetoed
    ops = _restore("mason", _state(sim, "mason"))
    for raw in ops:
        assert _t5918_vetoes(_state(sim, "mason")["seat_state"]), raw["operation_id"]  # vetoed until the clear
        _t5918_step(sim, raw)
    assert ops[-1]["operation_id"] == "rs-seat-state-clear"
    assert not _t5918_vetoes(_state(sim, "mason")["seat_state"])  # restored: no veto


def test_t5918_a_halted_pause_keeps_vetoing_until_rollback(tmp_path):
    """Recovery D15 (T-5858) (r1): a halted pause keeps the annotated record, so the veto holds
    while the wake timers sit disabled; rollback(run_id) clears it and the veto lifts.

    Since T-5866 a drain-timeout halt rolls itself back at once (rs-drain opts in to
    rollback_on_halt), so the halt that STAYS halted is a failed commit (no auto-rollback)."""

    original = _seat("mason")
    _repo, eng, scope = _engine(tmp_path, "mason", copy.deepcopy(original))
    _set_seat(eng, "mason", fails_during={"rs-timer-1": ["pantheon-other@mason.service"]})
    with pytest.raises(EngineRefusal):
        _apply(eng, scope, _desired())
    halted = _rs_state(eng, "mason")
    assert halted["seat_state"]["op_id"] == "rs-commit" and _t5918_vetoes(halted["seat_state"])
    assert eng.rollback(_run_id(eng)).state == "ROLLED_BACK"
    assert not _t5918_vetoes(_rs_state(eng, "mason")["seat_state"])


def test_t5918_a_drain_timeout_halt_vetoes_until_its_rollback_finishes(tmp_path, monkeypatch):
    """T-5866 x T-5918: at crash point A (the process dies after the drain-timeout halt, before
    the automatic rollback starts) the annotated record still vetoes repair; the operator
    rollback(run_id) the page names clears it. Without the crash the auto-rollback leaves no
    record, so no veto."""

    from tools.germinate.engine.backend import SimulatedKill
    from tools.germinate.engine.executor import Engine

    seat = _seat("mason")
    seat["units"]["pantheon-keep-going@mason.service"]["ActiveState"] = "active"
    _repo, eng, scope = _engine(tmp_path, "mason", copy.deepcopy(seat))

    def die(run_id):
        raise SimulatedKill("killed before the auto-rollback started")

    monkeypatch.setattr(eng, "rollback", die)
    with pytest.raises(SimulatedKill):
        _apply(eng, scope, _desired())
    halted = _rs_state(eng, "mason")
    assert halted["seat_state"]["op_id"] == "rs-drain" and _t5918_vetoes(halted["seat_state"])
    assert Engine.rollback(eng, _run_id(eng)).state == "ROLLED_BACK"
    assert not _t5918_vetoes(_rs_state(eng, "mason")["seat_state"])

    _repo2, eng2, scope2 = _engine(tmp_path / "auto", "mason", copy.deepcopy(seat))
    with pytest.raises(EngineRefusal):
        _apply(eng2, scope2, _desired())
    assert not _t5918_vetoes(_rs_state(eng2, "mason")["seat_state"])  # auto-rolled back


# --- T-5868 Phase B: the halted record written by the engine hook, and what clears it ------------

def _pages(events):
    return [e for e in events if e["event"] == "escalation.page"]


def test_t5868b_a_halted_pause_writes_a_valid_halted_record_and_pages_recovery_and_the_owner(tmp_path):
    _repo, eng, scope = _engine(tmp_path, "mason", _seat("mason"))
    _set_seat(eng, "mason", fails_during={"rs-timer-1": ["pantheon-other@mason.service"]})
    with pytest.raises(EngineRefusal):
        _apply(eng, scope, _desired())
    run_id = _run_id(eng)
    record = _rs_state(eng, "mason")["seat_state"]
    assert seat_state.validate_record(record) == []  # every notes-s10 key, exact rollback_cmd
    assert (record["state"], record["phase"], record["indefinite"], record["expires"]) == ("halted", "halted", True, None)
    assert record["run_id"] == run_id and record["op_id"] == "rs-commit"
    assert record["rollback_cmd"] == seat_state.halted_rollback_cmd(run_id) == f"python3 -m tools.germinate rollback --run {run_id}"
    assert record["pause_id"] == f"rs-suborch1-{STAMP}" and record["owner"] == "operator"
    parsed = seat_state.parse_record(record)
    assert parsed.alertable and parsed.intake_blocked and parsed.repair_vetoed and not parsed.remediation_vetoed
    events, run = _transcript(eng, run_id)
    assert [(p["recipient"], p["phase"]) for p in _pages(events)] == [("recovery", "halt"), ("orchestrator", "halt")]  # operator -> orchestrator
    assert [m["recipient"] for m in run["halted"]["escalations"]] == ["recovery", "orchestrator"]


def test_t5868b_the_halt_replaces_the_record_in_place_never_clearing_it(tmp_path):
    _repo, eng, scope = _engine(tmp_path, "mason", _seat("mason"))
    _set_seat(eng, "mason", fails_during={"rs-timer-1": ["pantheon-other@mason.service"]})
    with pytest.raises(EngineRefusal):
        _apply(eng, scope, _desired())
    halted = _rs_state(eng, "mason")
    assert halted["seat_state_tombstones"] == []  # one replace, no clear in between
    assert RS.unhalted(halted["seat_state"])["state"] == "paused-intake"
    with pytest.raises(HM.PlanRefused, match=r"HALTED at rs-commit; recover it with rollback\(run_id\)"):
        _restore("mason", halted)


def test_t5868b_a_completed_rollback_clears_the_record_last(tmp_path, monkeypatch):
    _repo, eng, scope = _engine(tmp_path, "mason", _seat("mason"))
    _set_seat(eng, "mason", fails_during={"rs-timer-1": ["pantheon-other@mason.service"]})
    with pytest.raises(EngineRefusal):
        _apply(eng, scope, _desired())
    handler = eng.backend.handler("harness.run-state-seat-state")
    seen = []
    real = handler.rollback_mutation

    def spy(mutation):
        before = _rs_state(eng, "mason")["seat_state"]
        real(mutation)
        after = _rs_state(eng, "mason")["seat_state"]
        seen.append((mutation.operation_id, before is not None and before["state"], after is None))

    monkeypatch.setattr(handler, "rollback_mutation", spy)
    assert eng.rollback(_run_id(eng)).state == "ROLLED_BACK"
    cleared = [i for i, (_op, _was, gone) in enumerate(seen) if gone and seen[i][0] == "rs-seat-state"]
    assert len(cleared) == 1 and seen[cleared[0]][1] == "halted"
    # every op undone before it (the units, the commit) left the HALTED record in place
    assert all(was == "halted" for _op, was, _gone in seen[:cleared[0] + 1]) and cleared[0] >= 1
    assert all(not gone for _op, _was, gone in seen[:cleared[0]])
    back = _rs_state(eng, "mason")
    assert back["seat_state"] is None and back["manifest"] is None


def test_t5868b_an_incomplete_rollback_keeps_the_halted_record(tmp_path):
    _repo, eng, scope = _engine(tmp_path, "mason", _draining_seat())
    first = sorted(EXPECTED_TARGETS["mason"])[0]
    _set_seat(eng, "mason", unit_stuck={first: "enable-now"})
    with pytest.raises(EngineRefusal) as info:
        _apply(eng, scope, _desired())
    assert "rollback-incomplete" in info.value.message
    kept = _rs_state(eng, "mason")["seat_state"]
    assert kept["state"] == "halted" and seat_state.validate_record(kept) == []
    assert kept["run_id"] == _run_id(eng) and _rs_state(eng, "mason")["manifest"] is not None
    # the retry, once the blocker is gone, completes and clears it
    _set_seat(eng, "mason", unit_stuck={})
    assert eng.rollback(_run_id(eng)).state == "ROLLED_BACK"
    assert _rs_state(eng, "mason")["seat_state"] is None


def test_t5868b_a_hook_failure_leaves_the_paused_record_pages_and_keeps_the_halt(tmp_path, monkeypatch):
    """Recovery D15 condition 1 end to end: the on_halted replace fails; the halt stands (rollback-admissible),
    the original page and the 'halt not recorded' page both go out to recovery and the owner."""
    from tools.germinate.engine.executor import HALT_NOT_RECORDED

    _repo, eng, scope = _engine(tmp_path, "mason", _seat("mason"))
    _set_seat(eng, "mason", fails_during={"rs-timer-1": ["pantheon-other@mason.service"]})
    handler = eng.backend.handler("harness.run-state-seat-state")

    def refuse(run_id, op, exc):
        raise OSError("disk full")

    monkeypatch.setattr(handler, "on_halted", refuse)
    with pytest.raises(EngineRefusal) as info:
        _apply(eng, scope, _desired())
    assert info.value.code == "halted"
    assert _rs_state(eng, "mason")["seat_state"]["state"] == "paused-intake"  # unchanged: reads as an authorised pause
    events, run = _transcript(eng, _run_id(eng))
    assert run["state"] == "HALTED"
    assert [(p["recipient"], p["phase"]) for p in _pages(events)] == [
        ("recovery", "halt"), ("orchestrator", "halt"), ("recovery", "halt-unrecorded"), ("orchestrator", "halt-unrecorded")]
    assert all(HALT_NOT_RECORDED in p["reason"] for p in _pages(events) if p["phase"] == "halt-unrecorded")
    monkeypatch.undo()
    assert eng.rollback(_run_id(eng)).state == "ROLLED_BACK"


def test_t5868b_on_halted_refuses_an_invalid_record_and_ignores_a_non_pause_op():
    ops = _pause("mason")
    sim = _sim("mason")
    for raw in ops:
        if raw["operation_id"] == "rs-commit":
            break
        sim.execute(_inst(raw))
    exc = HMH.EscalatingHalt("x", recipient="recovery", subject="suborch1")
    commit = _inst(_by_id(ops)["rs-commit"])
    before = _state(sim, "mason")
    sim.on_halted("r1", _inst(_by_id(ops)["rs-manifest"]), exc)  # the manifest op is not a pause step: no-op
    assert _state(sim, "mason") == before
    with pytest.raises(HMH.OperationFailed, match="invalid halted"):
        sim.on_halted("", commit, exc)  # an empty run_id cannot make a valid record
    assert _state(sim, "mason") == before
    sim.on_halted("r1", commit, exc)
    assert _state(sim, "mason")["seat_state"]["state"] == "halted"


@pytest.mark.parametrize("owner, want", [
    ("operator", "orchestrator"),                                   # Orchestrator's ask: nobody adds a direct the operator page
    ("recovery", "recovery"), ("mason", "mason"), ("orchestrator", "orchestrator"),   # a persona maps to itself
    ("role:head_engineering_orchestrator", "orchestrator"),        # role:X via agent_role_map
    ("role:operator", "orchestrator"), (None, None), ("", None), ("  ", None),
])
def test_t5868b_owner_recipient_resolution(owner, want):
    assert RS.owner_recipient(owner) == want


def test_t5868b_an_owner_that_resolves_to_recovery_is_paged_once():
    exc = HMH.EscalatingHalt("x", recipient=RS.ESCALATION_RECIPIENT, subject="s",
                             recipients=(RS.owner_recipient("recovery"),))
    assert exc.recipients == ("recovery",)


def test_t5868b_a_non_halting_pause_and_restore_are_unchanged(tmp_path):
    """Differential: with no halt the engine never calls the replace; the record stays the planned
    paused-intake document, the restore clears it with a tombstone, exactly as before Phase B."""
    _repo, eng, scope = _engine(tmp_path, "specialist")
    [outcome] = _apply(eng, scope, _desired())
    paused = _rs_state(eng, "specialist")["seat_state"]
    assert outcome.state == "SATISFIED" and paused["state"] == "paused-intake" and paused["phase"] == "set"
    assert not (set(RS.HALTED_KEYS) & set(paused))
    [outcome] = _apply(eng, scope, _desired("running", RESTORE_STAMP))
    back = _rs_state(eng, "specialist")
    assert outcome.state == "SATISFIED" and back["seat_state"] is None and len(back["seat_state_tombstones"]) == 1
