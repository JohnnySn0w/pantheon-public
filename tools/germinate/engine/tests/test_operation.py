from __future__ import annotations

import pytest

from tools.germinate.engine import operation as O
from tools.germinate.engine.contract import load_contract
from tools.germinate.engine.tests import helpers as H
from tools.germinate.op_registry import load_op_registry

REG = load_op_registry()


@pytest.fixture
def contract(tmp_path):
    path, _ = H.write_contract(
        tmp_path,
        "comp-a",
        privilege={"seat": ["mkdir", "atomic-write"], "bootstrap_root": ["linger.enable"]},
        gates=[{"id": "restart", "approval": {"class": "operator"}}],
    )
    return load_contract(path, REG)


def _parse(contract, **changes):
    raw = H.file_op("op-1", "/srv/a", "x")
    raw.update(changes)
    return O.parse_operation(raw, contract=contract, op_registry=REG)


def test_valid_mutating_op_parses_and_maps_to_install_operation(contract):
    op = _parse(contract)
    inst = op.to_install_operation()
    assert inst.kind == "atomic-write" and inst.privilege == "unprivileged"
    assert O.operation_from_json_obj(op.to_json_obj()) == op


def test_privilege_mapping_reading():
    assert O.INSTALLER_PRIVILEGE == {"none": "unprivileged", "seat": "unprivileged", "root": "privileged-helper"}


def test_mutating_op_needs_a_crash_contract(contract):
    with pytest.raises(O.OperationError, match="D-X"):
        _parse(contract, crash_contract=None)
    with pytest.raises(O.OperationError, match="D-X"):
        _parse(contract, crash_contract="best-effort")


def test_side_effect_free_rules(contract):
    with pytest.raises(O.OperationError, match="cannot declare side_effect_free"):
        _parse(contract, side_effect_free=True, crash_contract=None)
    read_only = H.file_op("op-2", "seat", "", kind="identity-check", privilege="none", crash_contract=None, side_effect_free=True)
    assert O.parse_operation(read_only, contract=contract, op_registry=REG).side_effect_free
    with pytest.raises(O.OperationError, match="must declare side_effect_free"):
        O.parse_operation(dict(read_only, side_effect_free=False), contract=contract, op_registry=REG)
    with pytest.raises(O.OperationError, match="no crash_contract"):
        O.parse_operation(dict(read_only, crash_contract="stageable"), contract=contract, op_registry=REG)


@pytest.mark.parametrize("kind", ["harness-turn", "send-keys-prompt", "model-reply-wait"])
def test_llm_path_kinds_refuse(contract, kind):
    with pytest.raises(O.OperationError, match="llm_free"):
        _parse(contract, kind=kind)


def test_unknown_kind_and_fields_refuse(contract):
    with pytest.raises(O.OperationError, match="not in the typed-op registry"):
        _parse(contract, kind="shell")
    raw = H.file_op("op-1", "/srv/a", "x")
    raw["command"] = "rm"
    with pytest.raises(O.OperationError, match="unknown fields"):
        O.parse_operation(raw, contract=contract, op_registry=REG)


def test_privilege_must_match_registry_and_contract(contract):
    with pytest.raises(O.OperationError, match="disagrees with the registry"):
        _parse(contract, privilege="root")
    assert _parse(contract, kind="linger.enable", privilege="root").privilege == "root"
    with pytest.raises(O.OperationError, match="bootstrap_root"):
        _parse(contract, kind="account.ensure", privilege="root")
    with pytest.raises(O.OperationError, match="seat/broker_runtime"):
        _parse(contract, kind="daemon-reload")


def test_gate_must_be_declared(contract):
    assert _parse(contract, gate="restart").gate == "restart"
    with pytest.raises(O.OperationError, match="neither a declared gate"):
        _parse(contract, gate="undeclared")


def test_op_id_alphabet_is_the_wals(contract):
    with pytest.raises(O.OperationError, match="operation_id"):
        _parse(contract, operation_id="op.1/../x")


# --- T-5866: opt-in rollback_on_halt --------------------------------------------------------


def test_rollback_on_halt_defaults_off_and_is_written_only_when_true(contract):
    plain = _parse(contract)
    assert plain.rollback_on_halt is False and "rollback_on_halt" not in plain.to_json_obj()
    explicit_off = _parse(contract, rollback_on_halt=False)
    assert explicit_off == plain and explicit_off.to_json_obj() == plain.to_json_obj()  # byte-identical plan data
    on = _parse(contract, rollback_on_halt=True)
    assert on.rollback_on_halt is True and on.to_json_obj() == dict(plain.to_json_obj(), rollback_on_halt=True)
    assert O.operation_from_json_obj(on.to_json_obj()) == on  # survives plan.json (rollback reads it there)
    assert O.operation_from_json_obj(plain.to_json_obj()) == plain


@pytest.mark.parametrize("value", ["true", 1, 0, None, "yes"])
def test_rollback_on_halt_must_be_a_boolean(contract, value):
    with pytest.raises(O.OperationError, match="rollback_on_halt must be a boolean"):
        _parse(contract, rollback_on_halt=value)
