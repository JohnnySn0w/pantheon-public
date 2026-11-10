"""T-6167 PR-5: seat group memberships through host-substrate (orchestrator ruling Q3).

Reverses T-5829 (1)(e) FOR MEMBERSHIPS ONLY, under four binding conditions, each pinned here:
  (a) the seat's OWN account only;
  (b) only into a PRE-EXISTING group (a fragment never creates a group);
  (c) only groups the seat's own identity block names; never a root-equivalent group
      (a hard refusal, one test per group) and never a private group;
  (d) the receipt records the pre-state; a rollback removes exactly the membership it
      added and a membership that already existed survives it.

Fixtures only: every observation is injected or the simulated port, and nothing touches
a live host. The real base manifest's ``groups`` is empty, so real-fleet use waits on
Recovery's signed base groups (T-5846 item 1); this file proves the mechanism.
"""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from tools.germinate.components import harness_manager_root as HR
from tools.germinate.components.tests import test_harness_manager_root as TR
from tools.germinate.components.tests import test_host_substrate_component as HS
from tools.germinate.engine.backend import SimulatedKill
from tools.germinate.engine.tests import helpers as H
from tools.germinate.host_substrate import families as F
from tools.germinate.host_substrate.helper import HelperRefusal
from tools.germinate.planner_sandbox.admission import coordinator

CID = TR.CID
ROOT = TR.ROOT
SEATS = TR.SEATS
BASE_GROUPS = frozenset({"pmail"})  # HS.fixture_manifest() allowlists exactly this group
PMAIL = {"group:pmail": {"gid": 3001}}


def _ident_obj(account="newseat", uid=3500, groups=("pmail",)):
    return HR.Identity(f"role-{account}", account, uid, "/bin/bash", "0750", tuple(sorted(groups)), (), "managed")


def _frag_with(groups, account="newseat", uid=3500):
    frag = TR._frag(account, uid)
    frag["accounts"][account]["groups"] = list(groups)
    return frag


def _scope(frag, *, declared=BASE_GROUPS, base=BASE_GROUPS, account="newseat", seats=SEATS):
    HR.check_seat_scope(f"role-{account}", account, frag, seat_accounts=seats, subjects=TR.OBSERVED,
                        declared_groups=frozenset(declared), base_groups=frozenset(base))


def _two_group_manifest():
    doc = HS.fixture_manifest()
    doc["groups"]["pshared"] = {"gid": 3002}
    return doc


def _member_op(plan, group, account="newseat"):
    return next(op for op in plan.nodes[0].operations if op.target == f"member:{group}:{account}")


# --- the fragment carries the seat's own declared groups ---------------------------------


def test_fragment_for_emits_the_sorted_declared_groups_and_nothing_else_new():
    frag = HR.fragment_for(_ident_obj(groups=("pshared", "pmail")))
    assert frag["accounts"]["newseat"]["groups"] == ["pmail", "pshared"]
    assert "groups" not in frag  # a fragment never carries a groups SECTION
    assert "groups" not in HR.fragment_for(_ident_obj(groups=()))["accounts"]["newseat"]


def test_a_declared_base_group_membership_passes_scope_and_merges_with_groups_untouched():
    doc = TR._instance(newseat=TR._ident(groups=["pmail"]))
    merged = HR.seat_merge(TR._base(), doc, lambda: ROOT | PMAIL, plan_time=True)
    m = F.load_manifest(merged.doc)
    assert m.section("accounts")["newseat"]["groups"] == ["pmail"]
    assert m.section("groups") == TR._base()["groups"]  # no group was added


def test_fragment_digest_covers_the_groups_and_the_merge_is_order_independent():
    with_groups, without = HR.fragment_for(_ident_obj()), HR.fragment_for(_ident_obj(groups=()))
    assert HR.fragment_sha256(with_groups) != HR.fragment_sha256(without)
    fa, fb = HR.fragment_for(_ident_obj("seata", 3500)), HR.fragment_for(_ident_obj("seatb", 3501))
    base = TR._base()
    one = HR.merge(base, {"role-seata": fa, "role-seatb": fb})
    two = HR.merge(base, {"role-seatb": fb, "role-seata": fa})
    assert F.sha256_text(F.canonical(one)) == F.sha256_text(F.canonical(two))


# --- (a) the seat's own account only --------------------------------------------------------


def test_a_a_membership_for_another_seats_account_refuses():
    frag = _frag_with(["pmail"])
    frag["accounts"]["otherseat"] = {"uid": 3501, "home": "/home/otherseat", "shell": "/bin/bash", "groups": ["pmail"]}
    with pytest.raises(HR.IdentityRefused, match="accounts must be exactly the seat's own"):
        _scope(frag)


def test_a_a_membership_for_a_base_account_collides_and_refuses():
    with pytest.raises(HR.IdentityRefused, match="accounts.alice .*collides with base"):
        HR.merge(TR._base(), {"role-alice": _frag_with(["pmail"], account="alice", uid=3500)})


def test_a_an_op_for_one_seat_naming_a_group_declared_only_for_another_refuses_at_the_handler(tmp_path):
    base = _two_group_manifest()
    doc = TR._instance(newseat=TR._ident(groups=["pmail", "pshared"]), second=TR._ident(3501, groups=["pmail"]))
    repo = TR._seat_repo(tmp_path, doc, base)
    eng = TR._seat_engine(tmp_path, repo, PMAIL | {"group:pshared": {"gid": 3002}})
    plan = eng.plan("install", "system", CID)
    handler = eng.backend.handler("group.member")
    stolen = replace(_member_op(plan, "pshared"), target="member:pshared:second", desired={"group": "pshared", "account": "second", "state": "present"})
    result = handler.preflight(stolen)
    assert not result.ok and result.classification == "refused"
    assert "is not allowlisted into pshared" in result.detail


# --- (b) pre-existing groups only -------------------------------------------------------------


def test_b_a_group_absent_from_the_observation_refuses_at_plan():
    doc = TR._instance(newseat=TR._ident(groups=["pmail"]))
    with pytest.raises(HR.IdentityRefused, match="group:pmail is not in the observation"):
        HR.seat_merge(TR._base(), doc, lambda: ROOT, plan_time=True)
    assert {f.code for f in HR.drift_check(doc, ROOT)} == {"could-not-ask"}


def test_b_a_group_absent_from_the_base_groups_refuses_at_merge_and_at_scope():
    ghost = _frag_with(["ghostgrp"])
    with pytest.raises(HR.IdentityRefused, match="absent from the base groups"):
        HR.merge(TR._base(), {"role-newseat": ghost})
    with pytest.raises(HR.IdentityRefused, match="not in the base manifest's groups"):
        _scope(ghost, declared={"ghostgrp"}, base=BASE_GROUPS)
    doc = TR._instance(newseat=TR._ident(groups=["ghostgrp"]))
    with pytest.raises(HR.IdentityRefused, match="not allowlisted"):
        HR.seat_merge(TR._base(), doc, lambda: ROOT, plan_time=False)


def test_b_a_fragment_that_adds_a_groups_section_refuses_at_merge():
    frag = _frag_with(["pmail"]) | {"groups": {"seatgrp": {"gid": 3900}}}
    with pytest.raises(HR.IdentityRefused, match="populates 'groups'"):
        HR.merge(TR._base(), {"role-newseat": frag})


def test_b_a_group_or_account_missing_at_apply_refuses_in_preflight_and_execute(tmp_path):
    repo = TR._seat_repo(tmp_path, TR._instance(newseat=TR._ident(groups=["pmail"])))
    eng = TR._seat_engine(tmp_path, repo, PMAIL)
    plan = eng.plan("install", "system", CID)
    op = _member_op(plan, "pmail")
    handler = eng.backend.handler("group.member")
    # the account does not exist yet (its account.ensure has not run)
    result = handler.preflight(op)
    assert not result.ok and result.classification == "refused" and "account 'newseat' does not exist" in result.detail
    with pytest.raises(HelperRefusal, match="account 'newseat' does not exist"):
        handler.execute(op)
    handler.port.put("passwd:newseat", {"uid": 3500, "gid": 3500, "home": "/home/newseat", "shell": "/bin/bash", "locked": False})
    handler.port.state["subjects"].pop("group:pmail")  # the group vanished between plan and apply
    result = handler.preflight(op)
    assert not result.ok and result.classification == "refused" and "group 'pmail' does not exist" in result.detail
    with pytest.raises(HelperRefusal, match="group 'pmail' does not exist"):
        handler.execute(op)
    assert handler.port.get("member:pmail:newseat") is None  # nothing was written


# --- (c) only groups this seat's block names; never root-equivalent; never private --------------


def test_c_a_group_not_in_this_seats_block_refuses_at_scope():
    with pytest.raises(HR.IdentityRefused, match="not named in this seat's identity block"):
        _scope(_frag_with(["pmail"]), declared=set())
    with pytest.raises(HR.IdentityRefused, match="not named in this seat's identity block"):
        _scope(_frag_with(["pmail"]), declared={"pshared"}, base={"pmail", "pshared"})


def test_c_a_block_edited_after_plan_refuses_at_apply_by_the_fragment_digest(tmp_path):
    repo = TR._seat_repo(tmp_path, TR._instance(newseat=TR._ident(groups=["pmail"])))
    eng = TR._seat_engine(tmp_path, repo, PMAIL)
    plan = eng.plan("install", "system", CID)
    op = _member_op(plan, "pmail")
    handler = eng.backend.handler("group.member")
    (repo / "instance.yaml").write_text(yaml.safe_dump(TR._instance(newseat=TR._ident(groups=[]))), encoding="utf-8")
    result = handler.preflight(op)
    assert not result.ok and result.classification == "refused"
    assert "changed since plan" in result.detail


def test_c_root_equivalent_groups_are_pinned_to_the_nine_name_set():
    assert HR.ROOT_EQUIVALENT_GROUPS == frozenset(
        {"sudo", "wheel", "admin", "root", "adm", "shadow", "disk", "docker", "lxd"})
    assert {"sudo", "wheel", "adm", "root"} <= HR.ROOT_EQUIVALENT_GROUPS


@pytest.mark.parametrize("group", sorted(HR.ROOT_EQUIVALENT_GROUPS))
def test_c_each_root_equivalent_group_is_refused_for_an_existing_managed_seat(group):
    with pytest.raises(HR.IdentityRefused, match="root-equivalent"):
        HR.fragment_for(_ident_obj(groups=(group,)))
    # even with the group declared AND in the base groups, the scope check refuses it
    with pytest.raises(HR.IdentityRefused, match="root-equivalent"):
        _scope(_frag_with([group]), declared={group}, base={group})
    # an existing managed seat (account observed) reaches the same refusal through seat_merge
    base = TR._base() | {"groups": {"pmail": {"gid": 3001}, group: {"gid": 3200}}}
    doc = TR._instance(newseat=TR._ident(groups=[group]))
    with pytest.raises(HR.IdentityRefused, match="root-equivalent"):
        HR.seat_merge(base, doc, lambda: ROOT | TR._existing("newseat", 3500, groups=(group,)), plan_time=False)


@pytest.mark.parametrize("private", ("newseat", "otherseat"))
def test_c_a_private_group_is_refused_own_or_another_seats(private):
    with pytest.raises(HR.IdentityRefused, match="private group"):
        _scope(_frag_with([private]), declared={private}, base={private})


# --- (d) pre-state in the receipt; rollback removes exactly what it added -------------------------


def _rollback_engine(tmp_path):
    base = _two_group_manifest()
    doc = TR._instance(newseat=TR._ident(groups=["pmail", "pshared"]))
    repo = TR._seat_repo(tmp_path, doc, base)
    eng = TR._seat_engine(tmp_path, repo, PMAIL | {"group:pshared": {"gid": 3002}})
    plan = eng.plan("install", "system", CID)
    eng.backend.handler("group.member").port.put(  # the new seat's account.ensure has run
        "passwd:newseat", {"uid": 3500, "gid": 3500, "home": "/home/newseat", "shell": "/bin/bash", "locked": False})
    return eng, plan


def test_d_rollback_of_a_membership_this_op_added_removes_only_that_membership(tmp_path):
    eng, plan = _rollback_engine(tmp_path)
    handler = eng.backend.handler("group.member")
    op = _member_op(plan, "pmail")
    handler.port.put("member:pshared:newseat", True)  # another group of the same account
    handler.port.put("member:pmail:alice", True)  # the same group, another account
    mutation, _evidence = handler.execute(op)
    assert json.loads(mutation.before["snapshot"]) is None  # the receipt records the pre-state
    assert handler.port.get("member:pmail:newseat") is True
    handler.rollback_mutation(mutation)
    assert handler.port.get("member:pmail:newseat") is None
    assert handler.port.get("member:pshared:newseat") is True and handler.port.get("member:pmail:alice") is True


def test_d_rollback_of_a_membership_that_already_existed_leaves_it_and_never_touches_the_port(tmp_path, monkeypatch):
    eng, plan = _rollback_engine(tmp_path)
    handler = eng.backend.handler("group.member")
    op = _member_op(plan, "pshared")
    handler.port.put("member:pshared:newseat", True)  # present before this op (planned absent, then it appeared)
    mutation = handler.rollback_intent(op)
    assert json.loads(mutation.before["snapshot"]) is True
    puts = []
    monkeypatch.setattr(handler.port, "put", lambda subject, value: puts.append((subject, value)))
    handler.rollback_mutation(mutation)
    assert puts == []
    assert handler.port.get("member:pshared:newseat") is True


# --- engine tier (same process) ------------------------------------------------------------------


def test_engine_a_new_seat_with_a_group_converges_and_verifies_and_preflight_is_per_op(tmp_path):
    """Q5: a new seat's membership op preflights AFTER its account.ensure executed. If the
    engine batched every preflight ahead of every execute, passwd:newseat would not exist
    yet and _membership_guard would refuse; the run reaching SATISFIED proves it interleaves."""

    repo = TR._seat_repo(tmp_path, TR._instance(newseat=TR._ident(groups=["pmail"])))
    eng = H.engine(tmp_path, repo)
    HS._seed(eng, HS._converged() | ROOT)
    plan = eng.plan("install", "system", CID)
    targets = [op.target for op in plan.nodes[0].operations]
    assert targets.index("passwd:newseat") < targets.index("member:pmail:newseat")
    [outcome] = eng.apply("install", "system", CID, plan_digest=plan.digest)
    assert outcome.state == "SATISFIED"
    assert HS._subjects(eng)["member:pmail:newseat"] is True
    [result] = eng.verify("system", CID)
    assert result["ok"]


def test_engine_kill_mid_membership_then_rollback_restores_the_preimage(tmp_path):
    repo = TR._seat_repo(tmp_path, TR._instance(newseat=TR._ident(groups=["pmail"])))
    eng = H.engine(tmp_path, repo)
    seed = HS._converged() | ROOT
    HS._seed(eng, seed)
    plan = eng.plan("install", "system", CID)
    op = _member_op(plan, "pmail")
    eng.backend.faults.kill = (op.operation_id, "after-execute")
    with pytest.raises(SimulatedKill):
        eng.apply("install", "system", CID, plan_digest=plan.digest)
    assert HS._subjects(eng)["member:pmail:newseat"] is True
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    restarted = H.engine(tmp_path, repo)
    assert restarted.reconcile_run(run_id, executor_dead=True).state == "HALTED"
    assert restarted.rollback(run_id).state == "ROLLED_BACK"
    assert HS._subjects(restarted) == seed


# --- dark pins -----------------------------------------------------------------------------------


def test_live_actuation_stays_a_literal_false_in_ports_source():
    ports = Path(__file__).resolve().parents[2] / "host_substrate" / "ports.py"
    found = []
    for node in ast.walk(ast.parse(ports.read_text(encoding="utf-8"))):
        target = (node.targets[0] if isinstance(node, ast.Assign) else getattr(node, "target", None))
        if isinstance(target, ast.Name) and target.id == "LIVE_ACTUATION_ENABLED":
            found.append(getattr(node, "value", None))
    assert len(found) == 1 and isinstance(found[0], ast.Constant) and found[0].value is False
