"""ORG-080 D-X kill-injection: a simulated process death at every durable
boundary of an op, for both crash contracts, then a fresh engine reconciles and
rolls back to the exact preimage.

At this tier a "kill" is a raised ``SimulatedKill`` (a BaseException no
executor handler catches), not a real SIGKILL, and both crash contracts go
through the same WAL intent -> execute -> verify -> receipt path; the matrix
proves every durable prefix is classifiable and reversible, not that the two
contracts differ. Real-process kills belong to the disposable-host tier (D-T).
"""

from __future__ import annotations

import pytest

from tools.germinate.engine.backend import KILL_POINTS, SimulatedHostBackend, SimulatedKill
from tools.germinate.engine.executor import Engine, EngineRefusal
from tools.germinate.engine.run_state import RunStore
from tools.germinate.engine.tests import helpers as H
from tools.germinate.planner_sandbox.admission import coordinator
from tools.germinate.planner_sandbox.wal import StructuralPrefix
from tools.persona_installer import MANAGED_MARKER

MANAGED = f"# {MANAGED_MARKER}\noriginal\n"

EXPECTED_PREFIX = {
    "after-intent": StructuralPrefix.INTENT_ONLY,
    "after-execute": StructuralPrefix.INTENT_ONLY,  # no probe: applied-ness is the backend's call at rollback
    "after-receipt": StructuralPrefix.ORPHAN_RECEIPT,
    "after-manifest": StructuralPrefix.MANIFEST_REFERENCED,
    "after-record": StructuralPrefix.MANIFEST_REFERENCED,
}
HALT_AT = {
    "after-intent": "op-2",
    "after-execute": "op-2",
    "after-receipt": "op-2",
    "after-manifest": "op-3",
    "after-record": "op-3",
}


def _ops(contract):
    return {
        "install": [
            H.dir_op("op-1", "/srv/k"),
            H.file_op("op-2", "/srv/k/managed.conf", "new\n", crash_contract=contract),
            H.file_op("op-3", "/srv/k/fresh.conf", "fresh\n", crash_contract=contract),
        ]
    }


def _killed_run(tmp_path, contract, point, kill_op="op-2"):
    _repo, eng = H.simple_setup(tmp_path, ops=_ops(contract))
    eng.backend.seed_file("/srv/k/managed.conf", MANAGED)
    preimage = {t: eng.backend.entry(t) for t in ("/srv/k", "/srv/k/managed.conf", "/srv/k/fresh.conf")}
    plan = eng.plan("install", "system", "comp-a")
    eng.backend.faults.kill = (kill_op, point)
    with pytest.raises(SimulatedKill):
        eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", "comp-a")["run_id"]
    # a fresh process: new backend object and engine over the same on-disk state
    restarted = Engine(SimulatedHostBackend(eng.backend.root), repo_root=eng.repo_root)
    return restarted, run_id, preimage


def _handle(eng, run_id):
    store = RunStore(eng.backend.run_state_root("system"))
    return store.open(store.find(run_id))


@pytest.mark.parametrize("contract", ["stageable", "write-ahead-inverse-durable"])
@pytest.mark.parametrize("point", KILL_POINTS)
def test_kill_then_reconcile_then_rollback_restores_preimage(tmp_path, contract, point):
    eng, run_id, preimage = _killed_run(tmp_path, contract, point)
    with _handle(eng, run_id) as handle:
        assert handle.read_record()["state"] == "ACTIVE"
        assert handle.wal().classify_prefix(op_id="op-2").structural_prefix is EXPECTED_PREFIX[point]
    with pytest.raises(EngineRefusal, match="run-active"):
        eng.rollback(run_id)
    outcome = eng.reconcile_run(run_id, executor_dead=True)
    assert outcome.state == "HALTED"
    with _handle(eng, run_id) as handle:
        record = handle.read_record()
        assert record["halted"]["op_id"] == HALT_AT[point]
        assert record["receipts"] == [e.to_json_obj() for e in handle.wal().read_manifest().receipts]
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    for target, before in preimage.items():
        assert eng.backend.entry(target) == before, target
    assert coordinator.read_claim(eng.backend.gates_root, "sim", "comp-a") is None


def test_kill_after_the_last_record_reconciles_to_satisfied(tmp_path):
    eng, run_id, _pre = _killed_run(tmp_path, "stageable", "after-record", kill_op="op-3")
    assert eng.reconcile_run(run_id, executor_dead=True).state == "SATISFIED"
    assert coordinator.read_claim(eng.backend.gates_root, "sim", "comp-a") is None


def test_reconcile_requires_an_explicit_death_assertion(tmp_path):
    eng, run_id, _pre = _killed_run(tmp_path, "stageable", "after-intent")
    with pytest.raises(EngineRefusal, match="liveness-unknown"):
        eng.reconcile_run(run_id, executor_dead=False)


def test_a_killed_run_keeps_the_component_gated(tmp_path):
    eng, _run_id, _pre = _killed_run(tmp_path, "stageable", "after-execute")
    with pytest.raises(EngineRefusal, match="claim-held"):
        eng.plan("install", "system", "comp-a")


def _halted_run(tmp_path):
    ops = {"install": [H.dir_op("op-1", "/srv/k"), H.file_op("op-2", "/srv/k/a.conf", "x\n"), H.file_op("op-3", "/srv/k/b.conf", "y\n")]}
    _repo, eng = H.simple_setup(tmp_path, ops=ops)
    plan = eng.plan("install", "system", "comp-a")
    eng.backend.faults.fail = {"op-3"}
    with pytest.raises(EngineRefusal, match="halted"):
        eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    return eng, coordinator.read_claim(eng.backend.gates_root, "sim", "comp-a")["run_id"]


def _kill_on_call(eng, n):
    real = eng.backend.rollback_mutation
    calls = []

    def maybe_die(mutation):
        calls.append(mutation.operation_id)
        if len(calls) == n:
            raise SimulatedKill("killed during rollback")
        real(mutation)

    eng.backend.rollback_mutation = maybe_die


@pytest.mark.parametrize("kill_on,outstanding", [(1, ["op-2", "op-1"]), (2, ["op-1"])])
def test_kill_mid_rollback_reconciles_to_incomplete_then_retry_completes(tmp_path, kill_on, outstanding):
    """Reviewer probe_rb.py: a crash mid-rollback must not strand the run in ROLLING_BACK."""

    eng, run_id = _halted_run(tmp_path)
    _kill_on_call(eng, kill_on)
    with pytest.raises(SimulatedKill):
        eng.rollback(run_id)
    fresh = Engine(SimulatedHostBackend(eng.backend.root), repo_root=eng.repo_root)
    with pytest.raises(EngineRefusal, match="run-active: .*ROLLING_BACK"):
        fresh.rollback(run_id)
    assert fresh.reconcile_run(run_id, executor_dead=True).state == "ROLLBACK_INCOMPLETE"
    with _handle(fresh, run_id) as handle:
        assert handle.read_record()["rollback_incomplete"] == {"failed_run_id": run_id, "outstanding_inverses": outstanding}
    with pytest.raises(EngineRefusal, match="claim-held"):
        fresh.plan("install", "system", "comp-a")
    assert fresh.rollback(run_id).state == "ROLLED_BACK"
    assert all(fresh.backend.entry(t) is None for t in ("/srv/k", "/srv/k/a.conf", "/srv/k/b.conf"))
    assert coordinator.read_claim(fresh.backend.gates_root, "sim", "comp-a") is None


def test_reconcile_requires_postboot(tmp_path):
    """Reviewer probe_rb.py C3 case: reconcile_run is a library surface, so postboot gates it."""

    eng, run_id, _pre = _killed_run(tmp_path, "stageable", "after-intent")
    eng.backend.postboot_marker_path.unlink()
    with pytest.raises(EngineRefusal, match="postboot-unmet"):
        eng.reconcile_run(run_id, executor_dead=True)


def test_kill_reconcile_rollback_cycles_do_not_accumulate_fds(tmp_path):
    """T-5147: a whole-suite process runs hundreds of kill cases; each must leave no run fd behind."""
    import os

    def fds() -> int:
        return len(os.listdir("/proc/self/fd"))

    def cycle(i: int) -> None:
        (tmp_path / f"c{i}").mkdir()
        eng, run_id, _pre = _killed_run(tmp_path / f"c{i}", "stageable", "after-execute")
        eng.reconcile_run(run_id, executor_dead=True)
        eng.rollback(run_id)

    cycle(0)  # warm imports / lazily opened files
    before = fds()
    for i in range(1, 4):
        cycle(i)
    assert fds() <= before + 1
