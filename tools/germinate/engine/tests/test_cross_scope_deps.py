"""S3 (T-5135): a seat-scope step may rest on a system-scope install (ORG-080 §2 depends_on)."""

from __future__ import annotations

import pytest

from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.tests import helpers as H

SEAT = "seat-alice"
DEP_OPS = {"install": [H.dir_op("d-1", "/srv/root-thing")]}
USER_OPS = {"install": [H.dir_op("u-1", "/srv/seat-thing")]}


def _setup(tmp_path, *, dep_scopes, user_scopes=("seat",)):
    repo = tmp_path / "repo"
    repo.mkdir()
    _p, dep_digest = H.write_contract(repo, "dep-root", ops=DEP_OPS, scopes=list(dep_scopes))
    _p, user_digest = H.write_contract(repo, "user-seat", ops=USER_OPS, scopes=list(user_scopes), depends_on=["dep-root"])
    H.make_checkout(repo, [H.row("dep-root", dep_digest), H.row("user-seat", user_digest)])
    return H.engine(tmp_path, repo)


def _install(eng, scope, cid):
    plan = eng.plan("install", scope, cid)
    [outcome] = eng.apply("install", scope, cid, plan_digest=plan.digest)
    return outcome


def test_seat_install_refuses_until_the_system_dependency_is_satisfied(tmp_path):
    eng = _setup(tmp_path, dep_scopes=("system",))
    with pytest.raises(EngineRefusal) as info:
        eng.plan("install", SEAT, "user-seat")
    assert info.value.code == "dependency-not-installed" and "scope system (resolved from seat-alice)" in info.value.message


def test_seat_install_proceeds_once_the_system_dependency_is_satisfied(tmp_path):
    eng = _setup(tmp_path, dep_scopes=("system",))
    assert _install(eng, "system", "dep-root").state == "SATISFIED"
    assert _install(eng, SEAT, "user-seat").state == "SATISFIED"


def test_a_seat_scope_install_of_a_system_only_dependency_is_not_a_substitute(tmp_path):
    eng = _setup(tmp_path, dep_scopes=("system",))
    # the dependency declares only system, so it cannot even be installed at a seat scope
    with pytest.raises(EngineRefusal) as info:
        eng.plan("install", SEAT, "dep-root")
    assert info.value.code == "scope-refused"
    with pytest.raises(EngineRefusal) as info:
        eng.plan("install", SEAT, "user-seat")
    assert info.value.code == "dependency-not-installed"


def test_a_system_dependent_never_resolves_to_a_seat_only_dependency(tmp_path):
    eng = _setup(tmp_path, dep_scopes=("seat",), user_scopes=("system", "seat"))
    assert _install(eng, SEAT, "dep-root").state == "SATISFIED"
    with pytest.raises(EngineRefusal) as info:
        eng.plan("install", "system", "user-seat")
    assert info.value.code == "dependency-not-installed"


def test_a_dependency_declaring_both_scopes_keeps_the_same_scope_rule(tmp_path):
    eng = _setup(tmp_path, dep_scopes=("system", "seat"))
    assert _install(eng, "system", "dep-root").state == "SATISFIED"
    with pytest.raises(EngineRefusal) as info:
        eng.plan("install", SEAT, "user-seat")
    assert info.value.code == "dependency-not-installed" and "resolved from" not in info.value.message
    assert _install(eng, SEAT, "dep-root").state == "SATISFIED"
    assert _install(eng, SEAT, "user-seat").state == "SATISFIED"


def test_remove_leg_does_not_require_the_dependency(tmp_path):
    eng = _setup(tmp_path, dep_scopes=("system",))
    assert eng.plan("remove", SEAT, "user-seat") is not None
