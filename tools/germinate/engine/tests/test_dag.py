from __future__ import annotations

import pytest

from tools.germinate.engine.dag import PlanNode, order_components, plan_digest
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.operation import operation_from_json_obj
from tools.germinate.engine.planner import PlanError
from tools.germinate.engine.tests import helpers as H


def test_topological_order_is_deterministic():
    deps = {"c": ["a", "b"], "b": ["a"], "a": [], "d": []}
    assert order_components(deps, ["d", "c", "b", "a"], leg="install") == ["a", "b", "c", "d"]
    assert order_components(deps, ["a", "b", "c", "d"], leg="install") == ["a", "b", "c", "d"]


def test_remove_runs_in_reverse():
    deps = {"b": ["a"], "a": []}
    assert order_components(deps, ["a", "b"], leg="remove") == ["b", "a"]


def test_cycle_refuses():
    with pytest.raises(PlanError, match="cycle"):
        order_components({"a": ["b"], "b": ["a"]}, ["a", "b"], leg="install")


def _node(content="x"):
    op = operation_from_json_obj(dict(H.file_op("op-1", "/t", content), gate=None, execution_scope="all"))
    return PlanNode("a", "install", "system", "sha256:" + "1" * 64, (op,))


def test_digest_binds_ops_and_registry_digest():
    base = plan_digest("install", "system", [_node()], "sha256:r1")
    assert base == plan_digest("install", "system", [_node()], "sha256:r1")
    assert base != plan_digest("install", "system", [_node("y")], "sha256:r1")
    assert base != plan_digest("install", "system", [_node()], "sha256:r2")
    assert base != plan_digest("install", "seat-x", [_node()], "sha256:r1")


def _two_component_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _p, da = H.write_contract(repo, "comp-a", ops={"install": [H.dir_op("op-a", "/a")]})
    _p, db = H.write_contract(repo, "comp-b", depends_on=["comp-a"], ops={"install": [H.dir_op("op-b", "/b")]})
    H.make_checkout(repo, [H.row("comp-a", da), H.row("comp-b", db)])
    return repo


def test_all_plans_in_dependency_order(tmp_path):
    eng = H.engine(tmp_path, _two_component_repo(tmp_path))
    assert [n.component for n in eng.plan("install", "system").nodes] == ["comp-a", "comp-b"]
    assert [n.component for n in eng.plan("remove", "system").nodes] == ["comp-b", "comp-a"]


def test_system_plans_one_component_and_needs_installed_deps(tmp_path):
    eng = H.engine(tmp_path, _two_component_repo(tmp_path))
    with pytest.raises(EngineRefusal, match="dependency-not-installed"):
        eng.plan("install", "system", "comp-b")
    a = eng.plan("install", "system", "comp-a")
    eng.apply("install", "system", "comp-a", plan_digest=a.digest)
    assert [n.component for n in eng.plan("install", "system", "comp-b").nodes] == ["comp-b"]


def test_unknown_dependency_refuses(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _p, d = H.write_contract(repo, "comp-a", depends_on=["ghost"])
    H.make_checkout(repo, [H.row("comp-a", d)])
    with pytest.raises(EngineRefusal, match="unknown-dependency"):
        H.engine(tmp_path, repo).plan("install", "system", "comp-a")
