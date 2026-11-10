"""T-5860: the preflight capability derivation over REAL component plans.

host-substrate (root), harness-manager (seat, the seat's own account) and
pantheon-config (seat, a ``role:<key>`` owner resolved through the instance
roles) are planned by their real planners through the engine; the derived
run-as set must equal the plan's own (privilege, owner) pairs, and the receipt
verdict must follow the simulated operator's sudo listing.
"""

from __future__ import annotations

import io

import yaml

from tools.germinate.engine import preflight as PF
from tools.germinate.engine.tests import helpers as H
from tools.germinate.engine.tests.test_preflight import HEAD, write_observation

from tools.germinate.components.tests import test_harness_manager_component as HMC
from tools.germinate.components.tests import test_harness_manager_install as HMI
from tools.germinate.components.tests import test_harness_manager_preflight as HMP
from tools.germinate.components.tests import test_harness_manager_remove as HMR
from tools.germinate.components.tests import test_harness_manager_run_state as HMRS
from tools.germinate.components.tests import test_harness_manager_supervisor as HMS
from tools.germinate.components.tests import test_host_substrate_component as HSC
from tools.germinate.components.tests import test_pantheon_config_component as PCC


def _listing(*rules: str) -> str:
    return "User opr may run the following commands on vps:\n" + "".join(f"    {r}\n" for r in rules)


def _pairs(merged):
    out: dict[str, set[str]] = {}
    for node in merged.nodes:
        for op in node.operations:
            target = {"root": "root", "seat": op.owner}.get(op.privilege)
            if target is not None:
                out.setdefault(target, set()).add(f"{node.component}/{op.operation_id}")
    return out


def _cap(receipt):
    return next(c for c in receipt["checks"] if c["id"] == "capability")


def _quiet(eng):
    eng.echo = io.StringIO()
    return eng


def test_host_substrate_needs_root_and_follows_the_listing(tmp_path):
    eng = _quiet(H.engine(tmp_path, HSC._repo(tmp_path)))
    merged = eng.plan("install", "system", HSC.CID)
    assert merged.nodes[0].operations
    derived = PF.required_capabilities(
        (f"{n.component}/{op.operation_id}", op.privilege, op.owner) for n in merged.nodes for op in n.operations
    )
    assert {k: set(v) for k, v in derived.items()} == _pairs(merged) and set(derived) == {"root"}
    write_observation(eng.backend.root, listing=_listing("(opr) NOPASSWD: ALL"))
    receipt = eng.preflight("install", "system", HSC.CID, accept_gates=frozenset())
    assert (_cap(receipt)["reason"], _cap(receipt)["evidence"]["missing"]) == ("capability-missing", {"root": "no-rule"})
    write_observation(eng.backend.root, listing=_listing("(root) NOPASSWD: ALL"))
    receipt = eng.preflight("install", "system", HSC.CID, accept_gates=frozenset())
    assert receipt["verdict"] == "GO", [c for c in receipt["checks"] if c["status"] != "pass"]
    assert _cap(receipt)["evidence"]["held"] == {"root": "sudo"}


def test_host_substrate_sudoers_gate_is_a_grant_no_go(tmp_path):
    eng = _quiet(H.engine(tmp_path, HSC._repo(tmp_path, HSC.fixture_manifest(sudoers=True))))
    write_observation(eng.backend.root, listing=_listing("(root) NOPASSWD: ALL"))
    receipt = eng.preflight("install", "system", HSC.CID, accept_gates=frozenset())
    failed = {c["id"]: c["reason"] for c in receipt["checks"] if c["status"] == "fail"}
    assert failed == {"grants": "grant-required"}


def test_harness_manager_seat_plan_runs_as_the_seat_account(tmp_path):
    repo = HMC._repo(tmp_path)
    eng = _quiet(HMC._engine(tmp_path, repo))
    merged = HMC._configure(eng)
    assert merged.nodes[0].operations
    assert set(_pairs(merged)) == {HMC.ACCT}
    # the seat runs its own sub-run: self. T-5921: a healthy seat probe, so the component checks pass
    write_observation(eng.backend.root, user=HMC.ACCT, probe=HMP._probe_doc(HMC.ACCT))
    receipt = eng.preflight("configure", HMC.SCOPE, HMC.CID, desired={"harness": "codex"})
    assert _cap(receipt)["status"] == "pass" and _cap(receipt)["evidence"]["held"] == {HMC.ACCT: "self"}
    # the real swap's authority-transfer gate is independent-agent: no consumer is wired, so NO-GO
    assert {c["id"]: c["reason"] for c in receipt["checks"] if c["status"] == "fail"} == {"grants": "grant-required"}
    write_observation(eng.backend.root, user="opr", listing=_listing("(root) NOPASSWD: ALL"))
    receipt = eng.preflight("configure", HMC.SCOPE, HMC.CID, desired={"harness": "codex"})
    assert _cap(receipt)["evidence"]["missing"] == {HMC.ACCT: "no-rule"}


def test_harness_manager_run_state_ops_derive_the_seat_account():
    """The fleet bringdown planner's raw ops (U5), through the same derivation."""

    for persona in ("specialist", "mason"):
        ops = HMRS._pause(persona)
        derived = PF.required_capabilities((op["operation_id"], op["privilege"], op["owner"]) for op in ops)
        assert set(derived) == {HMRS.ACCOUNTS[persona]}, derived
        assert sorted(derived[HMRS.ACCOUNTS[persona]]) == sorted(op["operation_id"] for op in ops if op["privilege"] != "none")


def _only_own_account(ops, account):
    derived = PF.required_capabilities((op["operation_id"], op["privilege"], op["owner"]) for op in ops)
    assert set(derived) == {account}, derived
    assert sorted(derived[account]) == sorted(op["operation_id"] for op in ops if op["privilege"] != "none")
    assert {(op["privilege"], op["owner"]) for op in ops if op["privilege"] != "none"} == {("seat", account)}


def test_harness_manager_install_ops_derive_the_seat_account():
    """Review R1 F6: the install leg, every real seat."""

    for persona in HMI.PERSONAS:
        account = HMI._account(persona)
        ops = HMI.HM.plan_install(HMI._request(account))
        assert ops
        _only_own_account(ops, account)


def test_harness_manager_supervisor_ops_derive_the_seat_account():
    """Review R1 F6: configure sub-op (a), cutover and restore."""

    for persona in sorted(HMS.UNIT_TEXTS):
        account = HMS._identity(persona).account
        _only_own_account(HMS._cutover(persona), account)
        _only_own_account(HMS._restore(persona), account)


def test_harness_manager_remove_ops_derive_the_seat_account():
    """Review R1 F6: the U3 remove leg, every decommissionable seat."""

    for persona in HMR.DECOMMISSIONABLE:
        ops = HMR._plan(persona)
        assert ops
        _only_own_account(ops, HMR.ACCOUNTS[persona])


def test_pantheon_config_role_owner_resolves_through_the_instance_roles(tmp_path):
    repo = PCC._repo(tmp_path)
    eng = _quiet(H.engine(tmp_path, repo))
    PCC._seed(eng.backend, PCC._state())
    merged = eng.plan("update", "system", PCC.CID, source_revs=(PCC.A, PCC.B))
    assert set(_pairs(merged)) == {"role:engineering-orchestrator-2"}
    write_observation(eng.backend.root, listing=_listing("(acct-two) NOPASSWD: ALL"), head=HEAD)
    receipt = eng.preflight("update", "system", PCC.CID, source_revs=(PCC.A, PCC.B))
    assert _cap(receipt)["reason"] == "owner-unresolved"  # the tmp checkout has no instance roles
    (repo / "instance.yaml").write_text(
        yaml.safe_dump({"roles": {"engineering-orchestrator-2": {"persona": "p2", "account": "acct-two"}}}), encoding="utf-8"
    )
    receipt = eng.preflight("update", "system", PCC.CID, source_revs=(PCC.A, PCC.B))
    assert receipt["verdict"] == "GO", [c for c in receipt["checks"] if c["status"] != "pass"]
    assert _cap(receipt)["evidence"]["resolved"] == {"role:engineering-orchestrator-2": "acct-two"}


def test_the_real_instance_roles_resolve_every_real_checkout_owner():
    """Every ``role:`` target_account in the real checkouts/global.yaml resolves through the real roles."""

    roles = PF.role_accounts(HSC.REPO_ROOT)
    doc = yaml.safe_load((HSC.REPO_ROOT / "checkouts" / "global.yaml").read_text(encoding="utf-8"))
    owners = {c["target_account"] for c in doc["checkouts"]}
    assert owners
    _resolved, _via, unresolved = PF.resolve_targets({o: ["x"] for o in owners}, roles)
    assert unresolved == {}
