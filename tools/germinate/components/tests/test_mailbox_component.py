"""T-5092 (germination GG): the mailbox component, an adapter over tools/mailbox_onboard.py.

Every engine-tier test runs on a tmp checkout (copies of the three component
files, this repo's instance.yaml read-only, and a fixture registry) over a
simulated host. The hub substrate (group, hub-owner, hub root) is
host-substrate's: here a labelled STUB host-substrate component with an empty
install stands in for it, and ``_seed_substrate`` is the labelled TEST HELPER
that writes the substrate facts into the simulated organ (mason ruling 2,
T-5092; the follow-up is host-substrate's empty accounts / mailbox_dirs).
``_seed_running`` writes a running legacy organ (what mailbox_onboard
provision leaves) into the simulated host. No lifecycle verb does either.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from tools import mailbox_onboard as MO
from tools.germinate.engine.backend import KILL_POINTS, SimulatedKill
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.run_state import RunStore
from tools.germinate.engine.tests import helpers as H
from tools.germinate.planner_sandbox.admission import coordinator
from tools.germinate.registry.contract_digest import compute_contract_digest
from tools.germinate.registry.repin import repin

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPONENTS = REPO_ROOT / "tools" / "germinate" / "components"
FILES = ("mailbox.yaml", "mailbox.py", "mailbox_handlers.py", "mailbox.invariants.yaml")
CID = "mailbox"
SCOPE = "system"
KIND = "mailbox.adopt-attest"


def _ctx():
    ctx = MO.load_mailbox_context(REPO_ROOT / "instance.yaml", REPO_ROOT)
    return ctx, MO._all_persona_bindings(ctx.manifest)


CTX, BINDINGS = _ctx()
SOLE = sorted(b.persona for b in BINDINGS if b.persona in CTX.wake_timer_personas)
SECONDARY = sorted(b.persona for b in BINDINGS if b.persona in CTX.wake_timer_secondary_personas)
MODE = format(MO.MAILBOX_DIR_MODE, "o")
PERSONAS = sorted(b.persona for b in BINDINGS)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    directory = repo / "tools" / "germinate" / "components"
    directory.mkdir(parents=True)
    for name in FILES:
        shutil.copy(COMPONENTS / name, directory / name)
    shutil.copy(REPO_ROOT / "instance.yaml", repo / "instance.yaml")
    digest = compute_contract_digest(directory / "mailbox.yaml")
    # STUB (labelled): host-substrate with an empty system install stands in for the real one
    _p, hs_digest = H.write_contract(repo, "host-substrate", ops={"install": []}, scopes=["system"])
    H.make_checkout(repo, [H.row(CID, digest), H.row("host-substrate", hs_digest)])
    return repo


def _handler(eng):
    return eng.backend.handler(KIND)


def _state(eng):
    return _handler(eng).dump_state()


def _write(eng, mutate) -> None:
    """TEST HELPER: edit the simulated organ in place and persist it."""

    state = _state(eng)
    mutate(state)
    _handler(eng).load_state(state)
    eng.backend.save()


def _seed_substrate(state) -> None:
    """TEST HELPER (labelled, mason ruling 2): the hub substrate host-substrate would install."""

    state["substrate"] = {"group": True, "hub_owner": True, "hub_root": True}
    state["members"] = {b.account: True for b in BINDINGS}


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mailbox_getfacl_real.txt"
LIVE_ARTISAN = "/home/post-office/mailbox/artisan"


def _acl_text(persona: str) -> str:
    """T-6009: REAL `getfacl -p` output (captured from the live hub, artisan) re-pointed at ``persona``'s hub paths."""

    blocks = FIXTURE.read_text(encoding="utf-8").strip("\n").split("\n\n")
    artisan = [b for b in blocks if b.startswith((f"# file: {LIVE_ARTISAN}\n", f"# file: {LIVE_ARTISAN}/new\n", f"# file: {LIVE_ARTISAN}/tmp\n"))]
    assert len(artisan) == 3
    return "\n\n".join(artisan).replace(LIVE_ARTISAN, f"{CTX.mailbox_hub}/{persona}") + "\n"


def _edit_acl(text: str, sub: str, fn) -> str:
    """Apply fn(lines) -> lines to one block of getfacl text; ``sub`` is "", "/new" or "/tmp" (relative to the persona dir)."""

    out = []
    for block in text.strip("\n").split("\n\n"):
        head = block.split("\n", 1)[0]
        hit = head.endswith(sub) if sub else not head.endswith(("/new", "/tmp"))
        out.append("\n".join(fn(block.split("\n"))) if hit else block)
    return "\n\n".join(out) + "\n"


def _seed_running(state) -> None:
    """TEST HELPER: a running legacy organ, as mailbox_onboard provision leaves it."""

    _seed_substrate(state)
    state["maildirs"] = {p: {"present": True, "mode": MODE, "acl": _acl_text(p)} for p in PERSONAS}
    state["units"] = {p: {"file": True, "managed": True, "enabled": True} for p in SOLE}


def _engine(tmp_path, repo, seed=None):
    eng = H.engine(tmp_path, repo)
    if seed is not None:
        _write(eng, seed)
    stub = eng.plan("install", SCOPE, "host-substrate")
    [outcome] = eng.apply("install", SCOPE, "host-substrate", plan_digest=stub.digest)
    assert outcome.state == "SATISFIED"
    return eng


@pytest.fixture
def running(tmp_path):
    repo = _repo(tmp_path)
    return repo, _engine(tmp_path, repo, _seed_running)


def _ops(plan):
    [node] = plan.nodes
    return node.operations


def _apply(eng, leg, **kwargs):
    plan = eng.plan(leg, SCOPE, CID)
    [outcome] = eng.apply(leg, SCOPE, CID, plan_digest=plan.digest, **kwargs)
    return plan, outcome


def _receipt(eng, run_id, op_id):
    store = RunStore(eng.backend.run_state_root(SCOPE))
    with store.open(store.find(run_id)) as handle:
        wal = handle.wal()
        ref = wal.classify_prefix(op_id=op_id).receipt_ref
        receipt, _ = wal.read_receipt_by_ref(ref)
        return receipt.receipt_payload


# --- observe-and-attest: the brownfield install path (T-5947) -------------------


def test_running_organ_plans_one_attest_op(running):
    _repo_, eng = running
    [op] = _ops(eng.plan("install", SCOPE, CID))
    assert (op.kind, op.target, op.privilege, op.owner, op.crash_contract) == (
        KIND, "attestation:mailbox", "seat", CTX.hub_owner, "write-ahead-inverse-durable")
    assert op.desired["verify_rc"] == "0" and op.desired["hub"] == str(CTX.mailbox_hub)
    assert op.desired["personas"] == ",".join(PERSONAS)
    assert op.desired["report_sha256"] == _handler(eng).evidence()["report_sha256"]
    assert op.provenance["basis"] == "observed-legacy" and op.provenance["re_attest"] == "false"
    assert "mailbox_onboard.verify" in op.provenance["planner"]


def test_attest_applies_satisfied_and_rerun_plans_nothing(running):
    _repo_, eng = running
    plan, outcome = _apply(eng, "install")
    assert outcome.state == "SATISFIED"
    assert _state(eng)["attestation"] == dict(_ops(plan)[0].desired)
    assert _ops(eng.plan("install", SCOPE, CID)) == ()
    _again, rerun = _apply(eng, "install")
    assert rerun.state == "SATISFIED"
    assert eng._satisfied_install(CID, SCOPE)


def test_receipt_records_the_attested_evidence(running):
    _repo_, eng = running
    plan, outcome = _apply(eng, "install")
    [op] = _ops(plan)
    payload = _receipt(eng, outcome.run_id, op.operation_id)
    assert json.loads(payload["mutation"]["after_fingerprint"]) == dict(op.desired)
    assert "attested mailbox_onboard verify rc 0" in payload["evidence"]["detail"]
    [result] = eng.verify(SCOPE, CID)
    assert result["ok"] and result["checks"][0]["op_id"] == op.operation_id


def test_healthy_drift_re_attests_with_new_digests(running):
    _repo_, eng = running
    first, _outcome = _apply(eng, "install")
    # an allowed secondary waker's standalone timer comes up: still healthy, but the report moved
    _write(eng, lambda s: s["units"].update({SECONDARY[0]: {"file": True, "managed": True, "enabled": True}}))
    [result] = eng.verify(SCOPE, CID)
    assert not result["ok"] and "report_sha256" in result["checks"][0]["detail"]
    [op] = _ops(eng.plan("install", SCOPE, CID))
    assert op.kind == KIND and op.provenance["re_attest"] == "true"
    assert op.desired["verify_rc"] == "0" and op.desired["report_sha256"] != _ops(first)[0].desired["report_sha256"]
    assert op.precondition["attested_report_sha256"] == _ops(first)[0].desired["report_sha256"]
    _plan, outcome = _apply(eng, "install")
    assert outcome.state == "SATISFIED" and eng.verify(SCOPE, CID)[0]["ok"]


def test_unhealthy_evidence_refuses_and_is_never_re_attested(running):
    _repo_, eng = running
    _apply(eng, "install")
    attested = _state(eng)["attestation"]
    _write(eng, lambda s: s["maildirs"].pop(PERSONAS[0]))  # the legacy verify now fails
    [result] = eng.verify(SCOPE, CID)
    assert not result["ok"] and "verify_rc now 1" in result["checks"][0]["detail"]
    with pytest.raises(EngineRefusal, match="UNHEALTHY now .*never re-attested") as info:
        eng.plan("install", SCOPE, CID)
    assert info.value.code == "plan-refused"
    assert _state(eng)["attestation"] == attested


def test_a_0700_maildir_refuses_attest(tmp_path):
    """Review F1: verify_persona checks is_dir only; the planner requires mailbox_onboard's mode."""

    repo = _repo(tmp_path)

    def seed(state):
        _seed_running(state)
        state["maildirs"][PERSONAS[0]]["mode"] = "700"

    eng = _engine(tmp_path, repo, seed)
    assert _handler(eng).evidence()["verify_rc"] == "0"  # the legacy verify alone reads healthy
    with pytest.raises(EngineRefusal, match=f"maildir-mode-degraded: {PERSONAS[0]} 700 .*requires {MODE}.*repair") as info:
        eng.plan("install", SCOPE, CID)
    assert info.value.code == "plan-refused"


def test_a_2770_organ_attests(running):
    _repo_, eng = running
    assert all(md["mode"] == MODE == "2770" for md in _state(eng)["maildirs"].values())
    [op] = _ops(eng.plan("install", SCOPE, CID))
    assert op.kind == KIND


def test_drift_to_0700_after_attest_refuses_re_attest_and_verify_flags_it(running):
    _repo_, eng = running
    _apply(eng, "install")
    attested = _state(eng)["attestation"]
    _write(eng, lambda s: s["maildirs"][PERSONAS[0]].update(mode="700"))
    [result] = eng.verify(SCOPE, CID)
    assert not result["ok"] and "facts_sha256" in result["checks"][0]["detail"]
    with pytest.raises(EngineRefusal, match="maildir-mode-degraded"):
        eng.plan("install", SCOPE, CID)
    assert _state(eng)["attestation"] == attested


def test_partial_legacy_organ_refuses_install_until_configure_converges_it(tmp_path):
    """Review F2: gaps beside legacy items refuse install; configure, then install attests."""

    repo = _repo(tmp_path)

    def seed(state):
        _seed_running(state)
        del state["maildirs"][PERSONAS[-1]]

    eng = _engine(tmp_path, repo, seed)
    with pytest.raises(EngineRefusal, match=f"partial legacy mailbox: .*maildir:{PERSONAS[-1]}.*configure first"):
        eng.plan("install", SCOPE, CID)
    [op] = _ops(eng.plan("configure", SCOPE, CID))
    assert (op.kind, op.target, op.desired["mode"]) == ("mailbox.maildir-provision", f"maildir:{PERSONAS[-1]}", MODE)
    _apply(eng, "configure")
    # T-6009: configure re-creates the maildir without an ACL (mailbox_onboard provision sets none), so the
    # organ still reads unhealthy and install keeps refusing, now on the ACL, until the ACLs are applied.
    with pytest.raises(EngineRefusal, match=f"no healthy legacy mailbox install: {PERSONAS[-1]} maildir_acl"):
        eng.plan("install", SCOPE, CID)
    _write(eng, lambda s: s["maildirs"][PERSONAS[-1]].update(acl=_acl_text(PERSONAS[-1])))  # TEST HELPER: the host-side setfacl
    [op] = _ops(eng.plan("install", SCOPE, CID))
    assert op.kind == KIND
    _plan, outcome = _apply(eng, "install")
    assert outcome.state == "SATISFIED" and _state(eng)["attestation"] is not None


def test_no_hub_substrate_refuses_naming_the_supervised_path(tmp_path):
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, repo)
    with pytest.raises(EngineRefusal, match="hub substrate is absent .*supervised path: host-substrate install, or .*mailbox_onboard.py"):
        eng.plan("install", SCOPE, CID)


def test_hard_legacy_failure_refuses(tmp_path):
    repo = _repo(tmp_path)

    def seed(state):
        _seed_running(state)
        state["members"][BINDINGS[0].account] = False  # group membership is host-substrate's

    eng = _engine(tmp_path, repo, seed)
    with pytest.raises(EngineRefusal, match="no healthy legacy mailbox install: .*group_membership"):
        eng.plan("install", SCOPE, CID)


def _report_obs(rows):
    return {
        "context": {"personas": {}},
        "units": {},
        "maildirs": {},
        "installed": [],
        "attestation": None,
        "substrate": {"group": True, "hub_owner": True, "hub_root": True},
        "verify": {"rc": 1, "report": rows},
        "evidence": {},
    }


_GOOD = {"persona": "alice", "name": "hub.new", "ok": True, "detail": ""}


@pytest.mark.parametrize(
    "row, text",
    [
        ("not-a-dict", "malformed verify report row #1"),
        ({"name": "hub.new", "ok": True, "detail": ""}, "malformed verify report row #1"),
        ({"persona": "alice", "name": "", "ok": True, "detail": ""}, "malformed verify report row #1"),
        ({"persona": "alice", "name": "hub.new", "detail": ""}, "alice hub.new: ok is not a bool (None)"),
        ({"persona": "alice", "name": "hub.new", "ok": "yes", "detail": ""}, "alice hub.new: ok is not a bool ('yes')"),
    ],
)
def test_malformed_report_row_is_a_hard_failure_not_a_crash(row, text):
    from tools.germinate.components import mailbox as component

    _maildirs, _units, hard = component._classify(_report_obs([_GOOD, row]))
    assert any(text in h for h in hard), hard
    with pytest.raises(component.PlanRefused, match="no healthy legacy mailbox install: .*" + re.escape(text)):
        component.plan_install({"observed": {component.HANDLER: dict(_report_obs([_GOOD, row]), substrate={}, maildirs={}, evidence={})}})


@pytest.mark.parametrize(
    "report, text",
    [
        ([_GOOD, "not-a-dict"], "malformed verify report row #1"),
        ([_GOOD, {"name": "hub.new", "ok": False, "detail": ""}], "malformed verify report row #1"),
        ("x", "malformed verify report: not a list"),
    ],
)
def test_attested_unhealthy_with_malformed_report_refuses_not_crashes(report, text):
    from tools.germinate.components import mailbox as component

    obs = dict(_report_obs(report), attestation={"k": "v"})
    with pytest.raises(component.PlanRefused, match="UNHEALTHY") as info:
        component.plan_install({"observed": {component.HANDLER: obs}})
    assert text in str(info.value), str(info.value)


def test_attested_unhealthy_names_a_truthy_non_bool_ok_row():
    from tools.germinate.components import mailbox as component

    row = {"persona": "alice", "name": "hub.new", "ok": "yes", "detail": ""}
    obs = dict(_report_obs([_GOOD, row]), attestation={"k": "v"})
    with pytest.raises(component.PlanRefused, match="UNHEALTHY") as info:
        component.plan_install({"observed": {component.HANDLER: obs}})
    assert "alice hub.new" in str(info.value), str(info.value)
    assert "UNHEALTHY ()" not in str(info.value), str(info.value)


_SOLE_ROW = {"persona": "alice", "name": "wake_timer.enabled", "ok": False, "detail": ""}


@pytest.mark.parametrize("where", ["personas", "units"])
@pytest.mark.parametrize("entry", ["junk", ["x"], 1])
def test_truthy_non_dict_persona_or_unit_entry_is_a_hard_failure(where, entry):
    from tools.germinate.components import mailbox as component

    obs = _report_obs([_GOOD, _SOLE_ROW])
    if where == "personas":
        obs["context"] = {"personas": {"alice": entry}}
    else:
        obs["context"] = {"personas": {"alice": {"wake": "sole"}}}
        obs["units"] = {"alice": entry}
    _m, _u, hard = component._classify(obs)
    assert any("alice" in h and "not an object" in h for h in hard), hard
    with pytest.raises(component.PlanRefused, match="alice"):
        component.plan_configure({"observed": {component.HANDLER: obs}})


@pytest.mark.parametrize("obs", ["junk", ["x"], 1])
def test_non_dict_observation_is_refused_not_a_crash(obs):
    from tools.germinate.components import mailbox as component

    for plan in (component.plan_install, component.plan_configure, component.plan_remove):
        with pytest.raises(component.PlanRefused, match="no .* observation"):
            plan({"observed": {component.HANDLER: obs}})


def test_report_not_a_list_is_a_hard_failure():
    from tools.germinate.components import mailbox as component

    for report in (None, "x", {"a": 1}):
        _m, _u, hard = component._classify(_report_obs(report))
        assert hard == ["malformed verify report: not a list"], hard


def test_foreign_wake_unit_refuses(tmp_path):
    repo = _repo(tmp_path)

    def seed(state):
        _seed_running(state)
        state["units"][SOLE[0]] = {"file": True, "managed": False, "enabled": False}  # not the tool's banner

    eng = _engine(tmp_path, repo, seed)
    with pytest.raises(EngineRefusal, match="a foreign wake unit file is in place"):
        eng.plan("install", SCOPE, CID)


def test_dependency_on_host_substrate_is_required(tmp_path):
    repo = _repo(tmp_path)
    eng = H.engine(tmp_path, repo)
    _write(eng, _seed_running)
    with pytest.raises(EngineRefusal) as info:
        eng.plan("install", SCOPE, CID)
    assert info.value.code == "dependency-not-installed"


# --- greenfield provision and configure -----------------------------------------


def test_greenfield_plans_provision_ops_then_converges_to_nothing(tmp_path):
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, repo, _seed_substrate)
    ops = _ops(eng.plan("install", SCOPE, CID))
    assert sorted(op.target for op in ops if op.kind == "mailbox.maildir-provision") == [f"maildir:{p}" for p in PERSONAS]
    assert sorted(op.target for op in ops if op.kind == "mailbox.wake-unit-install") == [f"wake-unit:{p}" for p in SOLE]
    owners = {op.target: op.owner for op in ops}
    accounts = {b.persona: b.account for b in BINDINGS}
    assert all(owners[f"maildir:{p}"] == CTX.hub_owner for p in PERSONAS)
    assert all(owners[f"wake-unit:{p}"] == accounts[p] for p in SOLE)
    _plan, outcome = _apply(eng, "install")
    assert outcome.state == "SATISFIED"
    # T-6009: truthful. mailbox_onboard provision sets no ACL, so a greenfield maildir is ACL-short: the
    # legacy verify reads rc 1, M3 is degraded naming every persona, and nothing attests (the planner
    # refuses an unhealthy organ). Converging the ACLs is not this component's op yet (follow-up).
    assert _handler(eng).evidence()["verify_rc"] == "1"
    [result] = eng.verify(SCOPE, CID)
    by_id = {r["id"]: r for r in result["invariants"]["results"]}
    assert result["invariants"]["state"] == "degraded" and not result["ok"]
    assert by_id["M3-maildir-acl"]["status"] == "fail"
    assert all(f"{CTX.mailbox_hub}/{p}: no ACL observed" in by_id["M3-maildir-acl"]["detail"] for p in PERSONAS)
    with pytest.raises(EngineRefusal, match="no healthy legacy mailbox install: .*maildir_acl") as info:
        eng.plan("install", SCOPE, CID)
    assert info.value.code == "plan-refused"


def test_configure_converges_a_missing_maildir_back_to_the_attested_evidence(running):
    _repo_, eng = running
    _apply(eng, "install")
    _write(eng, lambda s: s["maildirs"].pop(PERSONAS[-1]))
    [op] = _ops(eng.plan("configure", SCOPE, CID))
    assert (op.kind, op.target) == ("mailbox.maildir-provision", f"maildir:{PERSONAS[-1]}")
    _apply(eng, "configure")
    # T-6009: the re-provisioned maildir is truthfully ACL-short (provision sets none): rc 1, and the
    # attested mailbox is UNHEALTHY now, so install refuses rather than re-attests.
    assert _handler(eng).evidence()["verify_rc"] == "1"
    [result] = eng.verify(SCOPE, CID)
    assert not result["ok"] and result["invariants"]["state"] == "degraded"
    with pytest.raises(EngineRefusal, match=f"UNHEALTHY now .*{PERSONAS[-1]} maildir_acl"):
        eng.plan("install", SCOPE, CID)


def test_undeclared_legs_refuse(running):
    _repo_, eng = running
    for leg in ("update", "repair"):
        with pytest.raises(EngineRefusal) as info:
            eng.plan(leg, SCOPE, CID, source_revs=("a", "b") if leg == "update" else None)
        assert info.value.code == "leg-not-declared"


# --- remove, gates, crash and rollback -------------------------------------------


def test_remove_refuses_without_the_retire_gate(running):
    _repo_, eng = running
    _apply(eng, "install")
    plan = eng.plan("remove", SCOPE, CID)
    assert {op.gate for op in _ops(plan)} == {"mailbox-retire"}
    with pytest.raises(EngineRefusal) as info:
        eng.apply("remove", SCOPE, CID, plan_digest=plan.digest)
    assert info.value.code == "gate-unanswered"
    assert _state(eng)["attestation"] is not None


def test_remove_retires_units_and_attestation_never_maildirs(running):
    _repo_, eng = running
    _apply(eng, "install")
    before = _state(eng)["maildirs"]
    plan, outcome = _apply(eng, "remove", accept_gates=frozenset({"mailbox-retire"}))
    assert outcome.state == "SATISFIED"
    kinds = sorted({op.kind for op in _ops(plan)})
    assert kinds == ["mailbox.attest-retire", "mailbox.wake-unit-retire"]
    state = _state(eng)
    assert state["attestation"] is None and state["maildirs"] == before
    assert all(not state["units"][p]["enabled"] for p in SOLE)


def test_inverse_removes_the_attestation(running):
    _repo_, eng = running
    plan = eng.plan("install", SCOPE, CID)
    [op] = _ops(plan)
    eng.backend.faults.fail = {op.operation_id}  # postcondition never verified: the run halts
    with pytest.raises(EngineRefusal, match="HALTED"):
        eng.apply("install", SCOPE, CID, plan_digest=plan.digest)
    eng.backend.faults.fail = set()
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    assert _state(eng)["attestation"] is None


@pytest.mark.parametrize("point", KILL_POINTS)
def test_kill_then_reconcile_then_rollback_restores_preimage(tmp_path, point):
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, repo, _seed_running)
    preimage = _state(eng)["attestation"]
    plan = eng.plan("install", SCOPE, CID)
    [op] = _ops(plan)
    eng.backend.faults.kill = (op.operation_id, point)
    with pytest.raises(SimulatedKill):
        eng.apply("install", SCOPE, CID, plan_digest=plan.digest)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    restarted = H.engine(tmp_path, repo)
    outcome = restarted.reconcile_run(run_id, executor_dead=True)
    if point in ("after-manifest", "after-record"):
        assert outcome.state == "SATISFIED" and restarted.verify(SCOPE, CID)[0]["ok"]
        return
    assert outcome.state == "HALTED"
    assert restarted.rollback(run_id).state == "ROLLED_BACK"
    assert _state(restarted)["attestation"] == preimage


# --- registration, digests, the registry ----------------------------------------


def test_component_kinds_are_seat_privilege_in_their_own_class(running):
    _repo_, eng = running
    for kind in ("mailbox.maildir-provision", "mailbox.wake-unit-install", "mailbox.wake-unit-retire", KIND, "mailbox.attest-retire"):
        entry = eng.backend.op_registry.get(kind)
        assert (entry.capability_class, entry.privilege, entry.read_only) == ("mailbox.organ", "seat", False)


def test_a_handlers_edit_refuses_until_repinned(tmp_path):
    repo = _repo(tmp_path)
    handlers = repo / "tools" / "germinate" / "components" / "mailbox_handlers.py"
    handlers.write_text(handlers.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    with pytest.raises(EngineRefusal) as info:
        H.engine(tmp_path, repo)
    assert info.value.code == "component-registration-refused"
    digest = H.repin_contract(repo, CID)
    H.make_checkout(repo, [H.row(CID, digest), H.row("host-substrate", compute_contract_digest(
        repo / "tools" / "germinate" / "components" / "host-substrate.yaml"))])
    assert H.engine(tmp_path, repo).plan("install", SCOPE, "host-substrate")


def test_real_repo_pins_are_current():
    result = repin(REPO_ROOT, REPO_ROOT / "registry" / "systems.yaml", CID)
    assert result.drift == []


# --- T-5991: the verify invariant list org.pantheon.mailbox/v1 (M1-M7) -----------

LIST_ID = "org.pantheon.mailbox/v1"
M_IDS = [
    "M1-hub-substrate", "M2-maildir-present", "M3-maildir-acl", "M4-group-membership", "M5-wake-unit-sole",
    "M6-wake-unit-non-sole", "M7-attestation-current",
]


def _inv(eng):
    [result] = eng.verify(SCOPE, CID)
    return result, {r["id"]: r for r in result["invariants"]["results"]}, result["invariants"]["state"]


def _failing(by_id, *, drift=False):
    """Failing ids. Any substrate or unit change also moves the attested facts, so M7 rides along (named drift)."""

    return sorted(i for i, r in by_id.items() if r["status"] != "pass" and (drift or i != "M7-attestation-current"))


def _breach(running, mutate, *, only, state):
    """Install, apply ``mutate`` to the organ; exactly ``only`` fails, the state is ``state``, verify not ok."""

    _repo_, eng = running
    _apply(eng, "install")
    _write(eng, mutate)
    result, by_id, got = _inv(eng)
    assert _failing(by_id) == [only], by_id
    assert by_id["M7-attestation-current"]["status"] == "fail"  # the attested facts moved too
    assert got == state and not result["ok"]
    return by_id[only]["detail"]


def test_real_list_is_draft_and_bound_to_the_verify_leg():
    import yaml

    from tools.germinate.engine import invariant_digest as ID

    doc = yaml.safe_load((COMPONENTS / "mailbox.invariants.yaml").read_text(encoding="utf-8"))
    assert doc["status"] == "DRAFT" and doc["co_author"] == "recovery" and doc["signoff"] == "pending"
    assert [i["id"] for i in doc["invariants"]] == M_IDS
    body = yaml.safe_load((COMPONENTS / "mailbox.yaml").read_text(encoding="utf-8"))["component"]
    digest, list_id = ID.compute(COMPONENTS / "mailbox.yaml")
    assert list_id == LIST_ID == body["invariant_list_id"]
    assert body["invariant_list_digest"] == digest
    assert body["legs"]["verify"] == {"invariant_list_ref": f"{LIST_ID}@{digest}"}
    assert "emitter" not in body  # no Health1 emitter (ORG-081 D-11, T-5102)
    reg = yaml.safe_load((REPO_ROOT / "registry" / "systems.yaml").read_text(encoding="utf-8"))
    [row] = [r for r in reg["systems"] if r.get("id") == CID]
    assert "verify_contract" not in row["adoption_gaps"]  # the watch precedent: a bound list closes it
    assert row["legs"]["health"]["status"] == "partial" and row["legs"]["health"]["ticket"] == "T-5979"


def test_healthy_organ_passes_every_invariant_and_is_ready(running):
    _repo_, eng = running
    _apply(eng, "install")
    result, by_id, state = _inv(eng)
    assert list(by_id) == M_IDS and {r["status"] for r in by_id.values()} == {"pass"}
    assert state == "ready" and result["ok"]


def test_m1_missing_hub_owner_is_failed(running):
    detail = _breach(running, lambda s: s["substrate"].update(hub_owner=False), only="M1-hub-substrate", state="unhealthy")
    assert "hub_owner" in detail


def test_m1_missing_group_is_failed_and_membership_with_it(running):
    _repo_, eng = running
    _apply(eng, "install")
    _write(eng, lambda s: s["substrate"].update(group=False))
    _result, by_id, state = _inv(eng)
    # T-6009: with no group there are no members to hold an ACL entry, so M3 cannot evaluate either (fail closed)
    assert _failing(by_id) == ["M1-hub-substrate", "M3-maildir-acl", "M4-group-membership"] and "group" in by_id["M1-hub-substrate"]["detail"]
    assert "no mailbox group members observed" in by_id["M3-maildir-acl"]["detail"]
    assert state == "unhealthy"


def test_m1_missing_hub_root_is_failed_and_maildirs_with_it(running):
    _repo_, eng = running
    _apply(eng, "install")
    _write(eng, lambda s: s["substrate"].update(hub_root=False))
    _result, by_id, state = _inv(eng)
    assert "M1-hub-substrate" in _failing(by_id) and "hub_root" in by_id["M1-hub-substrate"]["detail"]
    assert by_id["M2-maildir-present"]["status"] == "fail" and state == "unhealthy"


def test_m2_missing_maildir_is_failed(running):
    detail = _breach(running, lambda s: s["maildirs"].pop(PERSONAS[0]), only="M2-maildir-present", state="unhealthy")
    assert PERSONAS[0] in detail


def test_m3_a_wrong_mode_alone_is_no_longer_an_m3_finding(running):
    """T-6009: 02770 and group::--- grant nothing on the live hub; M3 reads the ACLs, not the mode (the planner still gates on mode)."""

    _repo_, eng = running
    _apply(eng, "install")
    _write(eng, lambda s: s["maildirs"][PERSONAS[0]].update(mode="700"))
    _result, by_id, _state_ = _inv(eng)
    assert by_id["M3-maildir-acl"]["status"] == "pass"


def _acl_breach(running, persona, mutate_text):
    return _breach(
        running, lambda s: s["maildirs"][persona].update(acl=mutate_text(s["maildirs"][persona]["acl"])),
        only="M3-maildir-acl", state="degraded",
    )


def test_m3_missing_access_entry_is_degraded_and_names_account_path_and_kind(running):
    account = [b.account for b in BINDINGS if b.persona == PERSONAS[0]][0]
    detail = _acl_breach(running, PERSONAS[0], lambda t: _edit_acl(t, "/new", lambda ls: [x for x in ls if x != "user:suborch2:rwx"]))
    assert "suborch2" in detail and f"{CTX.mailbox_hub}/{PERSONAS[0]}/new" in detail and "access" in detail, (detail, account)


def test_m3_missing_default_entry_is_degraded_and_names_account_path_and_kind(running):
    detail = _acl_breach(running, PERSONAS[1], lambda t: _edit_acl(t, "", lambda ls: [x for x in ls if x != "default:user:codex:rwx"]))
    assert "codex" in detail and f"{CTX.mailbox_hub}/{PERSONAS[1]}:" in detail and "default" in detail


def test_m3_mask_restriction_is_degraded_and_says_masked(running):
    detail = _acl_breach(running, PERSONAS[0], lambda t: _edit_acl(t, "/tmp", lambda ls: ["mask::r-x" if x == "mask::rwx" else x for x in ls]))
    assert "masked by mask::r-x" in detail and f"{CTX.mailbox_hub}/{PERSONAS[0]}/tmp" in detail


@pytest.mark.parametrize("sub", ["/new", "/tmp"])
def test_m3_a_subdir_missing_from_the_output_is_no_acl_observed(running, sub):
    def drop(text):
        blocks = text.strip("\n").split("\n\n")
        return "\n\n".join(b for b in blocks if not b.split("\n", 1)[0].endswith(sub)) + "\n"

    detail = _acl_breach(running, PERSONAS[0], drop)
    assert f"{CTX.mailbox_hub}/{PERSONAS[0]}{sub}: no ACL observed" in detail


def test_m3_a_maildir_with_no_acl_data_fails_closed(running):
    detail = _breach(running, lambda s: s["maildirs"][PERSONAS[0]].pop("acl"), only="M3-maildir-acl", state="degraded")
    assert "no ACL observed" in detail


def test_m3_a_group_member_without_an_entry_is_degraded(running):
    outsider = "outsider-t6009"
    detail = _breach(running, lambda s: s["members"].update({outsider: True}), only="M3-maildir-acl", state="degraded")
    assert outsider in detail


def test_m3_every_present_maildir_needs_a_maildir_acl_row(running):
    MH, observed = _observed_healthy(running)
    observed["verify"]["report"] = [c for c in observed["verify"]["report"] if not (c["persona"] == PERSONAS[0] and c["name"] == "maildir_acl")]
    state, by_id = _run(MH, observed)
    assert state == "degraded" and by_id["M3-maildir-acl"]["status"] == "fail"
    assert f"{PERSONAS[0]}: could not evaluate: no maildir_acl row" in by_id["M3-maildir-acl"]["detail"]


def test_m4_missing_group_membership_is_failed(running):
    account = BINDINGS[0].account
    detail = _breach(running, lambda s: s["members"].update({account: False}), only="M4-group-membership", state="unhealthy")
    assert account in detail


def test_m5_disabled_sole_wake_unit_is_failed(running):
    detail = _breach(running, lambda s: s["units"][SOLE[0]].update(enabled=False), only="M5-wake-unit-sole", state="unhealthy")
    assert SOLE[0] in detail


def test_m5_foreign_wake_unit_is_failed_and_named(running):
    detail = _breach(running, lambda s: s["units"][SOLE[0]].update(managed=False), only="M5-wake-unit-sole", state="unhealthy")
    assert "foreign" in detail


def test_m6_non_wake_timer_enabled_is_failed_and_secondary_is_exempt(running):
    """This instance binds no persona with wake "none" (every persona is sole or secondary), so the failing
    case is a labelled SYNTHETIC observation: mailbox_onboard verify's own wake_timer.not_enabled check name."""

    from tools.germinate.components import mailbox_handlers as MH

    _repo_, eng = running
    _apply(eng, "install")
    observed = _handler(eng).observe("mailbox", SCOPE)
    listing = {"invariants": [{"id": "M6-wake-unit-non-sole"}]}
    assert MH.VERIFY(listing, {"mailbox-organ": observed}, SCOPE)["state"] == "ready"
    for secondary in SECONDARY:  # an allowed secondary waker with an enabled timer is not a breach
        assert observed["context"]["personas"][secondary]["wake"] == "secondary"
    observed["verify"]["report"].append({"persona": "ghost", "name": "wake_timer.not_enabled", "ok": False, "detail": "timer enabled"})
    out = MH.VERIFY(listing, {"mailbox-organ": observed}, SCOPE)
    assert out["state"] == "unhealthy" and out["results"][0]["status"] == "fail" and "ghost" in out["results"][0]["detail"]


def test_m7_attestation_drift_is_degraded_and_names_the_keys(running):
    _repo_, eng = running
    _apply(eng, "install")
    _write(eng, lambda s: s["attestation"].update(report_sha256="0" * 64))
    result, by_id, state = _inv(eng)
    assert _failing(by_id, drift=True) == ["M7-attestation-current"] and "report_sha256" in by_id["M7-attestation-current"]["detail"]
    assert state == "degraded" and not result["ok"]


def test_verify_without_an_observation_is_an_alarm_never_a_pass():
    from tools.germinate.components import mailbox_handlers as MH

    with pytest.raises(ValueError, match="could not ask"):
        MH.VERIFY({"invariants": [{"id": "M1-hub-substrate"}]}, {}, "system")
    out = MH.VERIFY({"invariants": [{"id": "M99-unknown"}]}, {"mailbox-organ": {"verify": {"report": []}}}, "system")
    assert out["results"][0]["status"] == "alarm" and out["state"] == "unhealthy"


def test_an_edited_list_refuses_verify_until_repinned(tmp_path):
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, repo, _seed_running)
    _apply(eng, "install")
    path = repo / "tools" / "germinate" / "components" / "mailbox.invariants.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("M3-maildir-acl", "M3-maildir-acls"), encoding="utf-8")
    with pytest.raises(EngineRefusal, match="invariant_list_digest"):
        eng.verify(SCOPE, CID)


def test_a_drifted_verify_ref_is_a_validation_refusal(tmp_path):
    from tools.germinate.registry.validate import errors, validate_repo

    repo = _repo(tmp_path)
    assert errors(validate_repo(repo)) == []
    path = repo / "tools" / "germinate" / "components" / "mailbox.yaml"
    text = path.read_text(encoding="utf-8")
    ref = [line for line in text.splitlines() if "invariant_list_ref" in line][0]
    path.write_text(text.replace(ref, ref[:-4] + "0000"), encoding="utf-8")
    assert any("drifts from" in str(f) for f in errors(validate_repo(repo)))


# --- T-5991 review: fail-open edges (an empty observation never reads healthy) ------


def _observed_healthy(running):
    from tools.germinate.components import mailbox_handlers as MH

    _repo_, eng = running
    _apply(eng, "install")
    return MH, _handler(eng).observe("mailbox", SCOPE)


def _run(MH, observed):
    listing = {"invariants": [{"id": i} for i in M_IDS]}
    out = MH.VERIFY(listing, {"mailbox-organ": observed}, SCOPE)
    return out["state"], {r["id"]: r for r in out["results"]}


def test_empty_bound_persona_set_fails_m2(running):
    MH, observed = _observed_healthy(running)
    assert _run(MH, observed)[0] == "ready"
    observed["context"]["personas"] = {}
    state, by_id = _run(MH, observed)
    assert state == "unhealthy"
    assert by_id["M2-maildir-present"]["status"] == "fail" and "no bound personas observed" in by_id["M2-maildir-present"]["detail"]
    del observed["context"]["personas"]  # a missing map is the same
    assert _run(MH, observed)[0] == "unhealthy"


@pytest.mark.parametrize("verify", [{}, {"rc": 0, "report": []}, {"rc": 0, "report": None}, {"rc": 0, "report": {}}])
def test_missing_or_empty_verify_report_fails_m4_and_m2_m6_could_not_evaluate(running, verify):
    MH, observed = _observed_healthy(running)
    observed["verify"] = verify
    state, by_id = _run(MH, observed)
    assert state == "unhealthy"
    assert by_id["M4-group-membership"]["status"] == "fail" and "no verify report observed" in by_id["M4-group-membership"]["detail"]
    for iid in ("M2-maildir-present", "M6-wake-unit-non-sole"):
        assert by_id[iid]["status"] == "fail" and "could not evaluate" in by_id[iid]["detail"], iid


def test_m5_reads_the_report_check_as_well_as_the_unit_facts(running):
    MH, observed = _observed_healthy(running)
    for c in observed["verify"]["report"]:
        if c["persona"] == SOLE[0] and c["name"] == "wake_timer.enabled":
            c["ok"], c["detail"] = False, "disabled"
    state, by_id = _run(MH, observed)
    assert by_id["M5-wake-unit-sole"]["status"] == "fail" and SOLE[0] in by_id["M5-wake-unit-sole"]["detail"] and state == "unhealthy"


# --- T-6009: bool ok, malformed rows, coverage -----------------------------------


def _row(observed, persona, name):
    [row] = [c for c in observed["verify"]["report"] if c["persona"] == persona and c["name"] == name]
    return row


@pytest.mark.parametrize("bad", ["false", "true", "", 0, 1, None])
@pytest.mark.parametrize("name, iid", [
    ("hub.new", "M2-maildir-present"), ("maildir_acl", "M3-maildir-acl"), ("group_membership", "M4-group-membership"),
    ("wake_timer.enabled", "M5-wake-unit-sole"),
])
def test_a_non_bool_ok_never_passes(running, name, iid, bad):
    MH, observed = _observed_healthy(running)
    persona = SOLE[0]
    _row(observed, persona, name)["ok"] = bad
    state, by_id = _run(MH, observed)
    assert by_id[iid]["status"] == "fail" and f"{persona} {name}: ok is not a bool ({bad!r})" in by_id[iid]["detail"]
    assert state == ("degraded" if iid == "M3-maildir-acl" else "unhealthy")


def test_a_non_bool_ok_moves_verify_rc_to_1(running):
    _repo_, eng = running
    handler = _handler(eng)
    report = handler.legacy_verify()
    assert handler.evidence(report)["verify_rc"] == "0"
    next(c for c in report if c["name"] == "group_membership")["ok"] = "false"
    assert handler.evidence(report)["verify_rc"] == "1"


def test_a_non_bool_ok_on_a_not_enabled_row_fails_m6(running):
    MH, observed = _observed_healthy(running)
    observed["verify"]["report"].append({"persona": "ghost", "name": "wake_timer.not_enabled", "ok": "true", "detail": "x"})
    observed["context"]["personas"]["ghost"] = {"account": "ghost", "wake": "none"}
    observed["maildirs"]["ghost"] = {"present": True}
    _state_, by_id = _run(MH, observed)
    assert by_id["M6-wake-unit-non-sole"]["status"] == "fail" and "ghost wake_timer.not_enabled: ok is not a bool ('true')" in by_id["M6-wake-unit-non-sole"]["detail"]


@pytest.mark.parametrize("row", [
    None, "a string", 7, ["a", "list"], {}, {"persona": "orchestrator"}, {"name": "group_membership"},
    {"persona": 5, "name": "group_membership", "ok": True}, {"persona": "orchestrator", "name": None, "ok": True},
    {"persona": "", "name": "group_membership", "ok": True},
])
def test_a_malformed_row_is_an_explicit_m2_failure_not_a_raise(running, row):
    MH, observed = _observed_healthy(running)
    index = len(observed["verify"]["report"])
    observed["verify"]["report"].append(row)
    state, by_id = _run(MH, observed)
    assert state == "unhealthy" and by_id["M2-maildir-present"]["status"] == "fail"
    assert f"malformed verify report row #{index}:" in by_id["M2-maildir-present"]["detail"]
    assert all(r["status"] == "pass" for i, r in by_id.items() if i != "M2-maildir-present")  # excluded elsewhere


def test_a_malformed_row_also_moves_verify_rc_and_never_raises_in_evidence(running):
    _repo_, eng = running
    handler = _handler(eng)
    report = handler.legacy_verify() + ["junk"]
    assert handler.evidence(report)["verify_rc"] == "1"


def test_missing_group_membership_row_fails_m4(running):
    MH, observed = _observed_healthy(running)
    observed["verify"]["report"] = [c for c in observed["verify"]["report"] if not (c["persona"] == "artisan" and c["name"] == "group_membership")]
    state, by_id = _run(MH, observed)
    assert state == "unhealthy" and "artisan: no group_membership row" in by_id["M4-group-membership"]["detail"]
    assert by_id["M4-group-membership"]["status"] == "fail"


def test_missing_sole_wake_row_fails_m5(running):
    MH, observed = _observed_healthy(running)
    observed["verify"]["report"] = [c for c in observed["verify"]["report"] if not (c["persona"] == SOLE[0] and c["name"] == "wake_timer.enabled")]
    state, by_id = _run(MH, observed)
    assert state == "unhealthy" and f"{SOLE[0]}: no wake_timer.enabled row" in by_id["M5-wake-unit-sole"]["detail"]


def test_missing_non_wake_row_fails_m6_and_secondary_is_exempt(running):
    MH, observed = _observed_healthy(running)
    for secondary in SECONDARY:  # an allowed secondary waker carries no required wake row
        observed["verify"]["report"] = [c for c in observed["verify"]["report"] if not (c["persona"] == secondary and c["name"].startswith("wake_timer."))]
    assert _run(MH, observed)[0] == "ready"
    observed["context"]["personas"]["ghost"] = {"account": "ghost", "wake": "none"}  # labelled SYNTHETIC: this instance binds none
    observed["maildirs"]["ghost"] = {"present": True}
    observed["verify"]["report"] += [
        {"persona": "ghost", "name": n, "ok": True, "detail": ""} for n in ("hub.new", "hub.tmp", "hub.cur", "maildir_acl", "group_membership")
    ]
    state, by_id = _run(MH, observed)
    assert state == "unhealthy" and "ghost: could not evaluate: no wake_timer.not_enabled row" in by_id["M6-wake-unit-non-sole"]["detail"]
    assert by_id["M5-wake-unit-sole"]["status"] == "pass"
