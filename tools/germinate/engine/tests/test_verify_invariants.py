"""S1 (T-5135): the engine verify leg evaluates the component's invariant list."""

from __future__ import annotations

import pytest

from tools.germinate.engine.tests import helpers as H

REF = f"org.pantheon.test/v1@{H.LIST_DIGEST}"
BOUND = dict(
    invariant_list_id="org.pantheon.test/v1",
    invariant_list_digest=H.LIST_DIGEST,
    legs={"install": "plan_install", "remove": "plan_remove", "verify": {"invariant_list_ref": REF}},
    emitter={"invariant_list_ref": REF},
)
OPS = {"install": [H.dir_op("op-1", "/srv/x")]}


def _setup(tmp_path, hook=None, *, installed=True):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS, **BOUND)
    if hook is not None:
        eng.verify_hooks["comp-a"] = hook
    if installed:
        plan = eng.plan("install", "system", "comp-a")
        eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    return eng


def _ok(list_doc, observed, scope):
    assert list_doc["invariant_list"]["id"] == "org.pantheon.test/v1"
    return {"results": [{"id": "T1", "status": "pass", "detail": "ok"}, {"id": "T2", "status": "vacuous", "detail": "n/a"}], "state": "ready"}


def test_passing_invariants_ride_on_the_verify_result(tmp_path):
    eng = _setup(tmp_path, _ok)
    [result] = eng.verify("system", "comp-a")
    assert result["ok"] and result["checks"]
    assert result["invariants"]["ok"] and result["invariants"]["state"] == "ready"
    assert result["invariants"]["list_ref"] == REF


@pytest.mark.parametrize("status", ["fail", "alarm"])
def test_failing_or_alarming_invariant_flips_ok_even_when_drift_is_clean(tmp_path, status):
    eng = _setup(tmp_path, lambda l, o, s: {"results": [{"id": "T1", "status": status, "detail": "x"}], "state": "degraded"})
    [result] = eng.verify("system", "comp-a")
    assert all(c["ok"] for c in result["checks"])
    assert not result["ok"] and not result["invariants"]["ok"]


def test_declared_list_without_a_hook_alarms_not_passes(tmp_path):
    eng = _setup(tmp_path, None)
    [result] = eng.verify("system", "comp-a")
    assert not result["ok"]
    assert result["invariants"]["results"][0]["status"] == "alarm"
    assert "no VERIFY hook" in result["invariants"]["results"][0]["detail"]


def _boom(list_doc, observed, scope):
    raise RuntimeError("boom")


@pytest.mark.parametrize(
    "hook",
    [
        _boom,
        lambda l, o, s: {"results": []},
        lambda l, o, s: {"results": [{"id": "T1", "status": "yes"}]},
        lambda l, o, s: {"results": ["not-a-mapping"]},
        lambda l, o, s: {"results": [{"id": "T1", "status": ["pass"]}]},
        lambda l, o, s: {"results": [{"id": "T1", "status": {"x": 1}}]},
        lambda l, o, s: {},
    ],
)
def test_broken_or_malformed_evaluator_is_an_alarm(tmp_path, hook):
    eng = _setup(tmp_path, hook)
    [result] = eng.verify("system", "comp-a")
    assert not result["ok"]
    assert all(r["status"] == "alarm" for r in result["invariants"]["results"])


def test_nothing_is_evaluated_without_a_satisfied_run(tmp_path):
    eng = _setup(tmp_path, _boom, installed=False)
    [result] = eng.verify("system", "comp-a")
    assert result["ok"] and "invariants" not in result


def test_component_without_a_list_is_unchanged(tmp_path):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    plan = eng.plan("install", "system", "comp-a")
    eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    [result] = eng.verify("system", "comp-a")
    assert result["ok"] and "invariants" not in result


def test_verify_refuses_a_list_whose_digest_drifted(tmp_path):
    from tools.germinate.engine.executor import EngineRefusal

    eng = _setup(tmp_path, _ok)
    path = eng.repo_root / "tools" / "germinate" / "components" / "comp-a.invariants.yaml"
    path.write_text(path.read_text().replace("T1", "T9"))
    with pytest.raises(EngineRefusal, match="recomputed"):
        eng.verify("system", "comp-a")


def test_declared_list_without_a_verify_ref_alarms_not_skips(tmp_path):
    _repo, eng = H.simple_setup(
        tmp_path,
        ops=OPS,
        invariant_list_id="org.pantheon.test/v1",
        invariant_list_digest=H.LIST_DIGEST,
        legs={"install": "plan_install", "remove": "plan_remove"},
    )
    eng.verify_hooks["comp-a"] = _ok
    plan = eng.plan("install", "system", "comp-a")
    eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    [result] = eng.verify("system", "comp-a")
    assert not result["ok"]
    [r] = result["invariants"]["results"]
    assert r["status"] == "alarm" and "no invariant_list_ref" in r["detail"]
