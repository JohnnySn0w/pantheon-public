"""S4 (T-5135): the invariant-list digest is recomputed by the engine load and the validator."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tools.germinate.components import harness_manager_verify as V
from tools.germinate.engine import contract as C
from tools.germinate.engine import invariant_digest as D
from tools.germinate.engine.tests import helpers as H
from tools.germinate.host_substrate import invariants as I
from tools.germinate.op_registry import load_op_registry

REG = load_op_registry()
REF = f"org.pantheon.test/v1@{H.LIST_DIGEST}"
BOUND = dict(
    invariant_list_id="org.pantheon.test/v1",
    invariant_list_digest=H.LIST_DIGEST,
    legs={"install": "plan_install", "remove": "plan_remove", "verify": {"invariant_list_ref": REF}},
    emitter={"invariant_list_ref": REF},
)


def test_digest_recomputed_from_the_list_file(tmp_path):
    path, _ = H.write_contract(tmp_path, "comp-a", **BOUND)
    C.load_contract(path, REG)
    list_file = path.with_name("comp-a.invariants.yaml")
    doc = yaml.safe_load(list_file.read_text())
    doc["invariant_list"]["invariants"][0]["class"] = "schema"
    list_file.write_text(yaml.safe_dump(doc))
    with pytest.raises(C.ContractError, match="recomputed"):
        C.load_contract(path, REG)


def test_missing_list_file_refuses(tmp_path):
    path, _ = H.write_contract(tmp_path, "comp-a", **BOUND)
    path.with_name("comp-a.invariants.yaml").unlink()
    with pytest.raises(C.ContractError, match="missing"):
        C.load_contract(path, REG)


def test_list_id_must_match_the_declared_id(tmp_path):
    other = REF.replace("test", "other")
    path, _ = H.write_contract(
        tmp_path,
        "comp-b",
        invariant_list_id="org.pantheon.other/v1",
        invariant_list_digest=H.LIST_DIGEST,
        legs={"install": "plan_install", "remove": "plan_remove", "verify": {"invariant_list_ref": other}},
        emitter={"invariant_list_ref": other},
    )
    with pytest.raises(C.ContractError, match="list file's id"):
        C.load_contract(path, REG)


def test_oracle_reproduces_both_components_own_digest_functions():
    comps = Path(V.__file__).parent
    assert D.compute(comps / "harness-manager.yaml")[0] == V.list_digest()
    hs = yaml.safe_load((comps / "host-substrate.invariants.yaml").read_text())
    assert D.compute(comps / "host-substrate.yaml")[0] == I.list_digest(hs)


def test_status_fields_do_not_change_the_flat_digest():
    doc = {"schema": D.FLAT_SCHEMA, "id": "x", "invariants": [{"id": "a"}], "status": "CURRENT"}
    assert D.digest_of(doc) == D.digest_of(dict(doc, status="DRAFT"))


def test_unrecognised_list_shape_refuses():
    with pytest.raises(D.InvariantDigestError, match="unrecognised"):
        D.digest_of({"foo": 1})
