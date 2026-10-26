from __future__ import annotations

import pytest
import yaml

from tools.germinate.engine import contract as C
from tools.germinate.engine.tests import helpers as H
from tools.germinate.op_registry import load_op_registry

REG = load_op_registry()
REF = f"org.pantheon.test/v1@{H.LIST_DIGEST}"


def _load(tmp_path, **overrides):
    path, _digest = H.write_contract(tmp_path, "comp-a", **overrides)
    return C.load_contract(path, REG)


def test_valid_contract_loads(tmp_path):
    loaded = _load(tmp_path)
    assert loaded.id == "comp-a" and loaded.planner_path.name == "comp-a.py"
    assert loaded.privilege["seat"] == frozenset({"mkdir", "atomic-write"})


@pytest.mark.parametrize("value", ["external", "rust", None])
def test_planner_kind_closed_enum_refuses_with_adr_message(tmp_path, value):
    overrides = {"planner_kind": value}
    path, _ = H.write_contract(tmp_path, "comp-a", **overrides)
    if value is None:
        doc = yaml.safe_load(path.read_text())
        del doc["component"]["planner_kind"]
        path.write_text(yaml.safe_dump(doc))
    with pytest.raises(C.ContractError, match="only `python` is accepted"):
        C.load_contract(path, REG)


def test_written_digest_must_match_recompute(tmp_path):
    path, _ = H.write_contract(tmp_path, "comp-a")
    text = path.read_text().replace("steward: orchestrator", "steward: recovery")
    path.write_text(text)
    with pytest.raises(C.ContractError, match="D-9"):
        C.load_contract(path, REG)


def test_unknown_and_missing_fields_refuse(tmp_path):
    with pytest.raises(C.ContractError, match="not in the contract schema"):
        _load(tmp_path, surprise=1)
    path, _ = H.write_contract(tmp_path, "comp-b")
    doc = yaml.safe_load(path.read_text())
    del doc["component"]["receipts"]
    path.write_text(yaml.safe_dump(doc))
    with pytest.raises(C.ContractError, match="missing required"):
        C.load_contract(path, REG)


def test_privilege_kinds_must_be_registry_kinds(tmp_path):
    with pytest.raises(C.ContractError, match="not an op-registry kind"):
        _load(tmp_path, privilege={"seat": ["file.atomic-write"]})


def test_bootstrap_root_disjoint_from_seat_and_runtime(tmp_path):
    with pytest.raises(C.ContractError, match="overlaps"):
        _load(tmp_path, privilege={"bootstrap_root": ["mkdir"], "seat": ["mkdir"]})


def test_seat_and_runtime_may_overlap(tmp_path):
    loaded = _load(tmp_path, privilege={"seat": ["daemon-reload"], "broker_runtime": ["daemon-reload"]})
    assert loaded.broker_runtime == frozenset({"daemon-reload"})


def test_broker_runtime_refuses_substrate_and_out_of_envelope(tmp_path):
    with pytest.raises(C.ContractError, match="substrate"):
        _load(tmp_path, privilege={"broker_runtime": ["account.ensure"]})
    with pytest.raises(C.ContractError, match="outside the broker envelope"):
        _load(tmp_path, privilege={"broker_runtime": ["claude-mcp-server-registration"]})


def test_substrate_kinds_are_bootstrap_root_only(tmp_path):
    with pytest.raises(C.ContractError, match="bootstrap_root only"):
        _load(tmp_path, privilege={"seat": ["linger.enable"]})
    assert _load(tmp_path, privilege={"bootstrap_root": ["linger.enable"]}).privilege["bootstrap_root"]


def test_declared_leg_needs_planner_function(tmp_path):
    with pytest.raises(C.ContractError, match="does not define plan_remove"):
        _load(tmp_path, planner_src="def plan_install(request):\n    return []\n")
    with pytest.raises(C.ContractError, match="must name plan_install"):
        _load(tmp_path, legs={"install": "do_install"})


def test_invariant_binding_is_enforced(tmp_path):
    ok = _load(
        tmp_path,
        invariant_list_id="org.pantheon.test/v1",
        invariant_list_digest=H.LIST_DIGEST,
        legs={"install": "plan_install", "remove": "plan_remove", "verify": {"invariant_list_ref": REF}},
        emitter={"invariant_list_ref": REF},
    )
    assert "verify" in ok.legs
    with pytest.raises(C.ContractError, match="D-11"):
        _load(
            tmp_path,
            invariant_list_id="org.pantheon.test/v1",
            invariant_list_digest=H.LIST_DIGEST,
            legs={"install": "plan_install", "remove": "plan_remove", "verify": {"invariant_list_ref": REF}},
            emitter={"invariant_list_ref": "org.pantheon.other/v1@" + H.LIST_DIGEST},
        )


def test_interactions_refuse_until_parking_exists(tmp_path):
    with pytest.raises(C.ContractError, match="parking/resume is deferred"):
        _load(tmp_path, interactions=[{"id": "oauth-onboarding"}])


def test_gate_shapes(tmp_path):
    loaded = _load(
        tmp_path,
        gates=[
            {"id": "restart", "irreversible": False, "approval": {"class": "operator"}},
            {"id": "decommission", "irreversible": True, "approval": {"class": "independent-agent", "grant_source": "broker"}},
        ],
        reserved_authority=[{"id": "seat-plan-flip"}],
    )
    assert loaded.gates["decommission"].approval_class == "independent-agent"
    assert loaded.reserved_authority == frozenset({"seat-plan-flip"})
    with pytest.raises(C.ContractError, match="grant_source"):
        _load(tmp_path, gates=[{"id": "x", "approval": {"class": "independent-agent"}}])
    with pytest.raises(C.ContractError, match="approval.class"):
        _load(tmp_path, gates=[{"id": "x", "approval": {"class": "anyone"}}])


def test_static_predicates_read_ast_only(tmp_path):
    path, _ = H.write_contract(tmp_path, "comp-a", planner_src="raise SystemExit('component code must not run')\n" "def plan_install(r):\n    return []\n")
    present = C.static_predicates(C.read_component(path), C.planner_path_for(path))
    assert present["plan_install"] and not present["plan_remove"]
    assert present["receipt_schema"] and present["sandbox"] and not present["verify_contract"]
