"""T-5084 (germination GE): the host-substrate component through the GB seam.

Every test runs on tmp_path stand-ins: a temporary checkout holding copies of
the component files, a FIXTURE manifest and unit/policy sources, and a registry,
over a simulated host. Only ``test_real_*`` reads the real repo (read-only).
Nothing here touches a live host: the handler is the simulated port, and the
host port refuses to construct.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import shutil
from pathlib import Path

import pytest
import yaml

from tools.germinate.engine.backend import KILL_POINTS, SimulatedKill
from tools.germinate.engine.contract import ContractError, load_contract
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.run_state import RunStore
from tools.germinate.engine.tests import helpers as H
from tools.germinate.host_substrate import families as F
from tools.germinate.host_substrate import gate32a
from tools.germinate.host_substrate import invariants as I
from tools.germinate.op_registry import load_op_registry
from tools.germinate.planner_sandbox.admission import coordinator
from tools.germinate.registry.contract_digest import compute_contract_digest

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPONENTS = REPO_ROOT / "tools" / "germinate" / "components"
FILES = ("host-substrate.yaml", "host-substrate.py", "host-substrate_handlers.py", "host-substrate.invariants.yaml")
CID = "host-substrate"
UNIT_BODY = "[Unit]\nDescription=fixture\n[Service]\nType=oneshot\nExecStart=/usr/bin/true\n"
POLICY_BODY = "<busconfig/>\n"


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def fixture_manifest(*, sudoers=False):
    doc = {
        "schema": F.MANIFEST_SCHEMA,
        "status": "CURRENT",
        "co_author": "recovery",
        "signoff": "approved",
        "groups": {"pmail": {"gid": 3001}},
        "accounts": {"alice": {"uid": 3101, "home": "/home/alice", "shell": "/bin/bash", "linger": True, "groups": ["pmail"]}},
        "installer_roots": ["/srv/pantheon"],
        "paths": {"/srv/pantheon/shared": {"owner": "alice", "group": "pmail", "mode": "0750", "acl": ["user::rwx", "group::r-x", "other::---", "group:pmail:r-x"]}},
        "mailbox_dirs": {"alice": {"path": "/srv/pantheon/mailbox-alice", "owner": "alice", "group": "pmail", "mode": "2770"}},
        "units": {"fixture.service": {"path": "/etc/systemd/system/fixture.service", "source": "fixture/fixture.service", "sha256": _sha(UNIT_BODY), "enable": True}},
        "dbus_policies": {"org.fixture": {"path": "/etc/dbus-1/system.d/org.fixture.conf", "source": "fixture/org.fixture.conf", "sha256": _sha(POLICY_BODY)}},
        "sudoers_templates": {},
        "legacy_requirements": [],
    }
    if sudoers:
        doc["sudoers_templates"] = {
            "alice-probe": {"id": "alice-probe", "path": "/etc/sudoers.d/pantheon-alice-probe", "grantee": "alice", "runas": "root",
                            "commands": ["/usr/local/sbin/pantheon-probe status"], "ticket": "T-5084", "expiry": "2026-12-31"}
        }
    return doc


def _repo(tmp_path, manifest=None):
    repo = tmp_path / "repo"
    comp = repo / "tools" / "germinate" / "components"
    comp.mkdir(parents=True)
    for name in FILES:
        shutil.copy(COMPONENTS / name, comp / name)
    (comp / "host-substrate.manifest.yaml").write_text(yaml.safe_dump(manifest or fixture_manifest(), sort_keys=False), encoding="utf-8")
    (repo / "fixture").mkdir()
    (repo / "fixture" / "fixture.service").write_text(UNIT_BODY, encoding="utf-8")
    (repo / "fixture" / "org.fixture.conf").write_text(POLICY_BODY, encoding="utf-8")
    H.make_checkout(repo, [H.row(CID, compute_contract_digest(comp / "host-substrate.yaml"))])
    return repo


def _subjects(eng):
    return eng.backend.handler("account.ensure").dump_state()["subjects"]


def _seed(eng, subjects):
    """TEST HELPER: write simulated substrate state into the simulated host."""

    eng.backend.handler("account.ensure").load_state({"subjects": subjects})
    eng.backend.save()


@pytest.fixture
def setup(tmp_path):
    repo = _repo(tmp_path)
    return repo, H.engine(tmp_path, repo)


def _install(eng, leg="install", accept=frozenset()):
    plan = eng.plan(leg, "system", CID)
    return plan, eng.apply(leg, "system", CID, plan_digest=plan.digest, accept_gates=accept)


# --- contract and discovery ---------------------------------------------------


def test_real_contract_loads_with_its_pinned_digest():
    path = COMPONENTS / "host-substrate.yaml"
    contract = load_contract(path, load_op_registry())
    assert contract.privilege["bootstrap_root"] == F.FAMILIES
    assert not contract.privilege.get("seat") and not contract.privilege.get("broker_runtime")
    body = yaml.safe_load(path.read_text())["component"]
    assert body["contract_digest"] == compute_contract_digest(path)


def test_discovery_serves_exactly_the_substrate_families_and_adds_no_kinds(setup):
    _repo_, eng = setup
    for kind in F.FAMILIES:
        entry = eng.backend.op_registry.get(kind)
        assert (entry.privilege, entry.substrate, entry.broker_admitted) == ("root", True, False)
        assert eng.backend.handles(kind)
    assert eng.backend.op_registry.digest == load_op_registry().digest  # OP_KINDS adds nothing


@pytest.mark.parametrize("where", ["seat", "broker_runtime"])
def test_a_substrate_family_outside_bootstrap_root_is_refused(tmp_path, where):
    path = tmp_path / "host-substrate.yaml"
    body = yaml.safe_load((COMPONENTS / "host-substrate.yaml").read_text())
    body["component"]["privilege"][where] = ["account.ensure"]
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    shutil.copy(COMPONENTS / "host-substrate.py", tmp_path / "host-substrate.py")
    body["component"]["contract_digest"] = compute_contract_digest(path)
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    with pytest.raises(ContractError, match="substrate|ORG-082"):
        load_contract(path, load_op_registry())


def test_invariant_list_digest_is_recomputed_not_trusted():
    doc = yaml.safe_load((COMPONENTS / "host-substrate.invariants.yaml").read_text())
    I.check_list(doc)
    body = yaml.safe_load((COMPONENTS / "host-substrate.yaml").read_text())["component"]
    assert body["invariant_list_digest"] == I.list_digest(doc)
    assert body["legs"]["verify"]["invariant_list_ref"] == f"{doc['id']}@{I.list_digest(doc)}"
    assert (doc["status"], doc["co_author"], doc["signoff"]) == ("CURRENT", "recovery", "approved")


def test_real_manifest_validates_and_pins_the_oom_unit_bytes():
    doc = yaml.safe_load((COMPONENTS / "host-substrate.manifest.yaml").read_text())
    m = F.load_manifest(doc, repo_root=REPO_ROOT)
    assert (doc["status"], doc["co_author"], doc["signoff"]) == ("CURRENT", "recovery", "approved")
    assert set(m.section("units")) == {"pantheon-oom-shield.service", "pantheon-oom-shield.timer", "user-slice-oomd-dropin",
                                         "user-account-slice-oomd-dropin"}
    assert {r["path"] for r in doc["legacy_requirements"]} == {
        "/usr/local/sbin/pantheon_oom_shield.sh", "/etc/systemd/oomd.conf.d/50-pantheon.conf", "/usr/lib/systemd/systemd-oomd",
        "/etc/default/earlyoom", "/usr/bin/earlyoom"}
    doc["units"]["pantheon-oom-shield.timer"]["sha256"] = "0" * 64
    with pytest.raises(F.ManifestError, match="pinned sha256"):
        F.load_manifest(doc, repo_root=REPO_ROOT)


def test_real_registry_row_validates_with_the_pinned_digest():
    from tools.germinate.registry.validate import errors, validate_repo

    assert errors(validate_repo(REPO_ROOT)) == []
    doc = yaml.safe_load((REPO_ROOT / "registry" / "systems.yaml").read_text(encoding="utf-8"))
    [row] = [r for r in doc["systems"] if r.get("id") == CID]
    assert row["contract_adoption"] == "partial"
    assert row["contract"]["digest"] == compute_contract_digest(COMPONENTS / "host-substrate.yaml")
    assert "adoption_evidence.emitter" in row["adoption_gaps"]
    assert "CURRENT (co-author recovery, approved)" in row["legs"]["health"]["gap"]


@pytest.mark.parametrize(
    "section, key, typo",
    [("accounts", "alice", "lingre"), ("units", "fixture.service", "enablee")],
)
def test_hostile_manifest_unknown_fields_refuse_plan_without_mutation(tmp_path, section, key, typo):
    doc = fixture_manifest()
    doc[section][key][typo] = True
    repo = _repo(tmp_path, doc)
    eng = H.engine(tmp_path, repo)
    with pytest.raises(EngineRefusal, match=rf"plan-refused:.*PlanRefused:.*{section}.*unknown fields.*{typo}"):
        eng.plan("install", "system", CID)
    assert _subjects(eng) == {}


def test_hostile_manifest_legacy_string_refuses_plan_without_mutation(tmp_path):
    doc = fixture_manifest()
    doc["legacy_requirements"] = ["/usr/local/sbin/legacy"]
    repo = _repo(tmp_path, doc)
    eng = H.engine(tmp_path, repo)
    with pytest.raises(EngineRefusal, match=r"plan-refused:.*PlanRefused:.*legacy_requirements\[0\]: must be a mapping"):
        eng.plan("install", "system", CID)
    assert _subjects(eng) == {}


@pytest.mark.parametrize(
    "section, key, field",
    [("accounts", "alice", "shell"), ("units", "fixture.service", "sha256")],
)
def test_hostile_manifest_missing_fields_refuse_plan_without_mutation(tmp_path, section, key, field):
    doc = fixture_manifest()
    doc[section][key].pop(field)
    repo = _repo(tmp_path, doc)
    eng = H.engine(tmp_path, repo)
    with pytest.raises(EngineRefusal, match=rf"plan-refused:.*PlanRefused:.*{section}.*missing fields.*{field}"):
        eng.plan("install", "system", CID)
    assert _subjects(eng) == {}


# --- planning, apply, idempotent rerun, verify --------------------------------


def test_install_converges_every_family_from_the_manifest_then_reruns_empty(setup):
    _repo_, eng = setup
    plan, [outcome] = _install(eng)
    ops = plan.nodes[0].operations
    assert {op.privilege for op in ops} == {"root"} and {op.crash_contract for op in ops} == {"write-ahead-inverse-durable"}
    assert [op.kind for op in ops][:3] == ["group.ensure", "account.ensure", "group.member"]
    assert outcome.state == "SATISFIED"
    subjects = _subjects(eng)
    assert subjects["passwd:alice"] == {"uid": 3101, "gid": 3101, "home": "/home/alice", "shell": "/bin/bash", "locked": False}
    assert subjects["member:pmail:alice"] is True and subjects["linger:alice"] is True
    assert subjects["enabled:fixture.service"] is True
    assert all(r["ok"] for r in I.evaluate(F.load_manifest(fixture_manifest()), subjects))
    # gate 35 (simulated): an identical rerun plans nothing
    assert eng.plan("install", "system", CID).nodes[0].operations == ()
    [result] = eng.verify("system", CID)
    assert result["ok"] and result["checks"]


def test_verify_evaluates_the_invariant_list_and_flags_drift_by_invariant_id(setup):
    """T-5135 S1: the engine verify leg evaluates the ONE list (ORG-080 A-6)."""

    _repo_, eng = setup
    _install(eng)
    [result] = eng.verify("system", CID)
    inv = result["invariants"]
    assert inv["ok"] and inv["state"] == "ready"
    assert [r["id"] for r in inv["results"]] == list(I.CHECKS)
    subjects = _subjects(eng)
    subjects["owner_mode:/srv/pantheon/shared"]["mode"] = "0777"
    _seed(eng, subjects)
    [result] = eng.verify("system", CID)
    inv = result["invariants"]
    assert not inv["ok"] and not result["ok"] and inv["state"] == "unhealthy"
    assert [r["id"] for r in inv["results"] if r["status"] == "fail"] == ["HS5-owned-paths"]


def test_verify_reports_drift_and_repair_converges_it(setup):
    _repo_, eng = setup
    _install(eng)
    subjects = _subjects(eng)
    subjects["owner_mode:/srv/pantheon/shared"]["mode"] = "0777"
    _seed(eng, subjects)
    [result] = eng.verify("system", CID)
    assert not result["ok"]
    bad = [r for r in I.evaluate(F.load_manifest(fixture_manifest()), subjects) if not r["ok"]]
    assert [r["id"] for r in bad] == ["HS5-owned-paths"]
    plan = eng.plan("repair", "system", CID)
    [op] = plan.nodes[0].operations
    assert op.kind == "owner_mode.set"
    eng.apply("repair", "system", CID, plan_digest=plan.digest)
    assert _subjects(eng)["owner_mode:/srv/pantheon/shared"]["mode"] == "0750"


def test_account_conflict_is_never_clobbered(setup):
    _repo_, eng = setup
    _seed(eng, {"passwd:mallory": {"uid": 3101, "gid": 3101, "home": "/home/mallory", "shell": "/bin/bash", "locked": False}})
    plan = eng.plan("install", "system", CID)
    with pytest.raises(EngineRefusal, match="HALTED|preflight"):
        eng.apply("install", "system", CID, plan_digest=plan.digest)
    assert "passwd:alice" not in _subjects(eng)


def test_update_only_stages_and_never_activates(setup):
    _repo_, eng = setup
    plan = eng.plan("update", "system", CID, source_revs=("a" * 40, "b" * 40))
    ops = plan.nodes[0].operations
    assert ops and {op.execution_scope for op in ops} == {"staged-cutover"}
    assert all(op.target.startswith("staged:") for op in ops)
    eng.apply("update", "system", CID, plan_digest=plan.digest, source_revs=("a" * 40, "b" * 40))
    assert all(k.startswith("staged:") for k in _subjects(eng))  # no live subject changed


def test_remove_retires_and_never_deletes(setup):
    _repo_, eng = setup
    _install(eng)
    plan = eng.plan("remove", "system", CID)
    with pytest.raises(EngineRefusal, match="gate-unanswered"):
        eng.apply("remove", "system", CID, plan_digest=plan.digest)
    eng.apply("remove", "system", CID, plan_digest=plan.digest, accept_gates=frozenset())
    subjects = _subjects(eng)
    assert subjects["passwd:alice"]["locked"] is True  # retired, still present (gate 34)
    assert subjects["dir:/srv/pantheon/mailbox-alice"]["mode"] == "0000"  # locked, not removed
    assert "linger:alice" not in subjects and "member:pmail:alice" not in subjects


def test_sudoers_apply_through_the_engine_refuses_until_grant_consumption(tmp_path):
    repo = _repo(tmp_path, fixture_manifest(sudoers=True))
    eng = H.engine(tmp_path, repo)
    plan = eng.plan("install", "system", CID)
    assert any(op.gate == "sudoers-self-lockout" for op in plan.nodes[0].operations)
    with pytest.raises(EngineRefusal, match="grant-required|independent-agent"):  # T-5135 S2
        eng.apply("install", "system", CID, plan_digest=plan.digest, accept_gates=frozenset())
    assert _subjects(eng) == {}


def test_a_manifest_change_after_plan_refuses_apply(setup):
    repo, eng = setup
    plan = eng.plan("install", "system", CID)
    doc = fixture_manifest()
    doc["accounts"]["alice"]["shell"] = "/usr/sbin/nologin"
    (repo / "tools/germinate/components/host-substrate.manifest.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(EngineRefusal, match="plan-digest-mismatch"):
        eng.apply("install", "system", CID, plan_digest=plan.digest)


def _first_op_and_handler(eng):
    op = eng.plan("install", "system", CID).nodes[0].operations[0]
    return op, eng.backend.handler(op.kind)


def test_handler_refuses_a_target_that_disagrees_with_manifest_resolution(setup):
    _repo_, eng = setup
    op, handler = _first_op_and_handler(eng)
    bad = replace(op, target="group:not-pmail")
    result = handler.preflight(bad)
    assert (result.ok, result.classification, result.detail) == (
        False,
        "refused",
        f"{op.operation_id}: target 'group:not-pmail' != resolved subject 'group:pmail'",
    )


def test_handler_refuses_manifest_provenance_mismatch_with_stable_reason(setup):
    _repo_, eng = setup
    op, handler = _first_op_and_handler(eng)
    bad = replace(op, provenance={"planner": "host-substrate", "manifest_sha256": "0" * 64})
    result = handler.preflight(bad)
    assert (result.ok, result.classification, result.detail) == (
        False,
        "refused",
        f"{op.operation_id}: the manifest changed since plan",
    )


def test_handler_classifies_malformed_operation_as_refused(setup):
    _repo_, eng = setup
    op, handler = _first_op_and_handler(eng)
    bad = replace(op, desired={"group": "pmail", "gid": "0"})
    result = handler.preflight(bad)
    assert not result.ok and result.classification == "refused"
    assert result.detail == "group.ensure: parameters must be exactly ['group'], got ['gid', 'group']"


def test_handler_classifies_precondition_drift_with_resolved_subject(setup):
    _repo_, eng = setup
    op, handler = _first_op_and_handler(eng)
    handler.port.put("group:pmail", {"gid": 3999})
    result = handler.preflight(op)
    assert (result.ok, result.classification, result.detail) == (
        False,
        "drifted",
        "group:pmail changed since plan",
    )


# --- kill injection (ORG-080 D-X), every family the engine path actuates -------
#
# Each case seeds the simulated host so exactly ONE op is pending (the rest
# already converged / retired), so the matrix exercises that family alone.

FORWARD_KINDS = ["group.ensure", "account.ensure", "group.member", "linger.enable", "mailbox_dir.ensure",
                 "owner_mode.set", "acl.converge", "unit.system.install", "unit.system.enable", "dbus_policy.install"]
RETIRE_KINDS = {  # remove-leg family -> the params its (retired) value resolves from
    "account.retire": {"account": "alice"},
    "mailbox_dir.retire": {"mailbox": "alice"},
    "linger.disable": {"account": "alice"},
    "group.member": {"group": "pmail", "account": "alice", "state": "absent"},
    "unit.system.enable": {"unit": "fixture.service", "state": "disabled"},
    "unit.system.install": {"unit": "fixture.service", "state": "absent", "sha256": _sha(UNIT_BODY)},
    "dbus_policy.retire": {"policy": "org.fixture"},
}


def _converged():
    return {r.subject: r.after for _cid, r in I._expect(F.load_manifest(fixture_manifest()))}


def _retired():
    m = F.load_manifest(fixture_manifest())
    state = _converged()
    for family, params in RETIRE_KINDS.items():
        r = F.resolve(family, params, m)
        if r.after is None:
            state.pop(r.subject, None)
        else:
            state[r.subject] = r.after
    return state


def _kill_case(tmp_path, leg, seed, kind, point):
    repo = _repo(tmp_path)
    eng = H.engine(tmp_path, repo)
    _seed(eng, seed)
    accept = frozenset() if leg == "remove" else None
    plan = eng.plan(leg, "system", CID)
    [op] = plan.nodes[0].operations
    assert op.kind == kind
    eng.backend.faults.kill = (op.operation_id, point)
    with pytest.raises(SimulatedKill):
        eng.apply(leg, "system", CID, plan_digest=plan.digest, accept_gates=accept)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    restarted = H.engine(tmp_path, repo)  # a fresh process over the same simulated host
    outcome = restarted.reconcile_run(run_id, executor_dead=True)
    if point in ("after-manifest", "after-record"):
        assert outcome.state == "SATISFIED"  # the receipt is committed: the op stands
        assert restarted.verify("system", CID)[0]["ok"]
        return
    assert outcome.state == "HALTED"
    assert restarted.rollback(run_id).state == "ROLLED_BACK"
    assert _subjects(restarted) == seed  # the exact preimage
    assert coordinator.read_claim(restarted.backend.gates_root, "sim", CID) is None


@pytest.mark.parametrize("point", KILL_POINTS)
@pytest.mark.parametrize("kind", FORWARD_KINDS)
def test_kill_then_reconcile_then_rollback_restores_preimage(tmp_path, kind, point):
    m = F.load_manifest(fixture_manifest())
    [(subject, _after)] = [(r.subject, r.after) for _c, r in I._expect(m) if r.family == kind]
    seed = {k: v for k, v in _converged().items() if k != subject}
    _kill_case(tmp_path, "install", seed, kind, point)


@pytest.mark.parametrize("point", KILL_POINTS)
@pytest.mark.parametrize("kind", sorted(RETIRE_KINDS))
def test_kill_mid_remove_then_rollback_restores_the_installed_state(tmp_path, kind, point):
    m = F.load_manifest(fixture_manifest())
    r = F.resolve(kind, RETIRE_KINDS[kind], m)
    seed = _retired()
    seed[r.subject] = _converged()[r.subject]  # only this one still to retire
    _kill_case(tmp_path, "remove", seed, kind, point)


def test_kill_mid_rollback_leaves_rollback_incomplete_then_retry_clears(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    eng = H.engine(tmp_path, repo)
    plan = eng.plan("install", "system", CID)
    last = plan.nodes[0].operations[-1]
    eng.backend.faults.fail = {last.operation_id}
    with pytest.raises(EngineRefusal, match="HALTED"):
        eng.apply("install", "system", CID, plan_digest=plan.digest)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    first = plan.nodes[0].operations[0]
    eng.backend.faults.rollback_fail = {first.operation_id}
    with pytest.raises(EngineRefusal, match="rollback-incomplete"):
        eng.rollback(run_id)
    eng.backend.faults.rollback_fail = set()
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    assert _subjects(eng) == {}


def test_receipts_record_root_ops_with_simulated_evidence_scope(setup):
    _repo_, eng = setup
    _plan, [outcome] = _install(eng)
    store = RunStore(eng.backend.run_state_root("system"))
    with store.open(store.find(outcome.run_id)) as handle:
        record = handle.read_record()
    assert record["receipts"] and {o["crash_contract"] for o in record["operations"]} == {"write-ahead-inverse-durable"}
    assert eng.backend.evidence_scope == "simulated"


# --- ORG-082 gate 32a, simulated tier -------------------------------------------


def test_gate_32a_digest_equality_projection_and_substrate_absence(setup):
    repo, eng = setup
    root_registry = eng.backend.op_registry  # the in-process root context's load
    # The broker daemon context: its REAL load path (ORG-082 step 3, T-2918),
    # which captures the digest and asserts the substrate-free dispatch table.
    from tools.host_control.runtime_table import load_checkout_op_registry, load_daemon_runtime

    daemon = load_daemon_runtime(host_operation_families={"persona.install"},
                                 registry_loader=lambda: load_checkout_op_registry(repo))  # the same checkout
    daemon_registry = daemon.op_registry
    assert daemon.loaded_op_registry_digest == root_registry.digest
    projection = daemon.dispatch_table
    assert projection == gate32a.daemon_projection(daemon_registry)
    union = frozenset(daemon_registry.broker_admitted_kinds)  # fixture components' broker_runtime union
    expected = gate32a.runtime_subset_digest(union)
    assert gate32a.check(root_registry=root_registry, daemon_registry=daemon_registry, projection=projection,
                         expected_union=union, expected_digest=expected) == []
    assert not projection & F.FAMILIES
    plan = eng.plan("install", "system", CID)
    # the engine's own §2a digest over the REAL union (host-substrate adds no broker_runtime)
    assert plan.expected_runtime_subset_digest == gate32a.runtime_subset_digest(())


def test_gate_32a_refuses_a_skewed_registry_and_a_leaked_family():
    root = load_op_registry()
    skewed = load_op_registry({"extra.kind": ("operator.extra", "seat")})
    projection = gate32a.daemon_projection(root)
    expected = gate32a.runtime_subset_digest(projection)
    failures = gate32a.check(root_registry=root, daemon_registry=skewed, projection=projection, expected_union=projection, expected_digest=expected)
    assert any(f.startswith("REGISTRY_DIGEST_MISMATCH") for f in failures)
    leaked = projection | {"account.ensure"}
    failures = gate32a.check(root_registry=root, daemon_registry=root, projection=leaked, expected_union=projection, expected_digest=expected)
    assert any("RUNTIME_SUBSET_MISMATCH" in f for f in failures) and any("SUBSTRATE_FAMILY_NOT_EXPOSED" in f for f in failures)


def test_gate_32a_projection_union_mismatch_is_independently_discriminated():
    registry = load_op_registry()
    projection = gate32a.daemon_projection(registry)
    union = projection | {"fixture.extra-runtime-kind"}
    failures = gate32a.check(root_registry=registry, daemon_registry=registry, projection=projection,
                             expected_union=union, expected_digest=gate32a.runtime_subset_digest(projection))
    assert failures == ["RUNTIME_SUBSET_MISMATCH: extra [], missing ['fixture.extra-runtime-kind']"]


def test_gate_32a_projection_digest_mismatch_is_independently_discriminated():
    registry = load_op_registry()
    projection = gate32a.daemon_projection(registry)
    wrong_digest = gate32a.runtime_subset_digest(projection | {"fixture.extra-runtime-kind"})
    failures = gate32a.check(root_registry=registry, daemon_registry=registry, projection=projection,
                             expected_union=projection, expected_digest=wrong_digest)
    assert failures == ["RUNTIME_SUBSET_MISMATCH: projection digest != expected-runtime-subset digest"]
