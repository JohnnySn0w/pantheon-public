"""T-5940 (S5): the engine's backend selection seam. The simulated backend stays the
default, and a run through the default path is unchanged against an explicit simulated
factory (differential)."""

from __future__ import annotations

import pytest

from tools.germinate.engine import preflight as PF
from tools.germinate.engine.backend import BackendRefused, SimRootRefused, SimulatedHostBackend
from tools.germinate.engine.executor import Engine, EngineRefusal
from tools.germinate.engine.tests import helpers as H
from tools.germinate.engine.tests.test_seam import _component


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    digest = _component(repo)
    H.make_checkout(repo, [H.row("comp-r", digest)])
    return repo


def _run(eng):
    eng.backend.simulate_postboot()
    plan = eng.plan("install", "system", "comp-r")
    outcomes = eng.apply("install", "system", "comp-r", plan_digest=plan.digest)
    observed = eng.backend.observe("comp-r", "system", frozenset({"repo.ff-update"}))
    return plan.digest, [o.state for o in outcomes], observed


def test_default_is_the_simulated_backend(tmp_path):
    eng = Engine.from_checkout(tmp_path / "sim", repo_root=_repo(tmp_path))
    assert type(eng.backend) is SimulatedHostBackend
    assert type(eng.preflight_port) is PF.SimulatedPreflightPort and eng.preflight_port.root == eng.backend.root
    assert eng.backend.privilege_ceiling == frozenset({"none", "seat", "root"})
    assert eng.backend.bound_account is None


def test_default_and_explicit_simulated_factory_are_identical(tmp_path):
    repo = _repo(tmp_path)
    seen = {}

    def factory(op_registry, handlers):
        seen["registry"], seen["handlers"] = op_registry, handlers
        return SimulatedHostBackend(tmp_path / "sim-b", op_registry=op_registry, handlers=handlers)

    default = Engine.from_checkout(tmp_path / "sim-a", repo_root=repo)
    explicit = Engine.from_checkout(None, repo_root=repo, backend_factory=factory)
    assert type(explicit.backend) is SimulatedHostBackend
    assert type(explicit.preflight_port) is PF.SimulatedPreflightPort
    assert seen["registry"].digest == default.backend.loaded_op_registry_digest == explicit.backend.loaded_op_registry_digest
    assert [h.name for h in seen["handlers"]] == ["repo-sim"]
    assert sorted(default.backend._by_kind) == sorted(explicit.backend._by_kind)
    assert explicit.backend.root.name == "sim-b"
    via_default, via_factory = _run(default), _run(explicit)
    assert via_default == via_factory
    assert via_default[1:] == (["SATISFIED"], {"repo-sim": {"head": "rev-1"}})


def test_no_root_and_no_factory_refuses(tmp_path):
    with pytest.raises(EngineRefusal, match="backend-refused"):
        Engine.from_checkout(None, repo_root=_repo(tmp_path))


def test_a_non_simulated_backend_gets_no_preflight_port(tmp_path):
    class NotSim:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def factory(op_registry, handlers):
        return NotSim(SimulatedHostBackend(tmp_path / "sim", op_registry=op_registry, handlers=handlers))

    eng = Engine.from_checkout(None, repo_root=_repo(tmp_path), backend_factory=factory)
    assert eng.preflight_port is None


def test_factory_value_error_is_a_registration_refusal(tmp_path):
    def factory(op_registry, handlers):
        raise ValueError("kind 'x' already has a handler")

    with pytest.raises(EngineRefusal, match="component-registration-refused"):
        Engine.from_checkout(None, repo_root=_repo(tmp_path), backend_factory=factory)


def test_sim_root_refusal_is_a_backend_refusal():
    assert issubclass(SimRootRefused, BackendRefused)
