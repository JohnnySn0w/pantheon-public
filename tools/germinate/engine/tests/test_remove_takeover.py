"""T-5875: a GATED remove may take over the claim of a ROLLBACK_INCOMPLETE run (ORG-080 D-Y).

D-Y: ``partial`` with ``rollback_incomplete: <run-id>`` is a structural mutation gate; "only
rollback-retry or gated decommission is admissible". Gated means the remove node carries an op
whose declared gate is independent-agent class (the D-E decommission gate), answered by a grant
in ``_check_gates``. Every other verb, and every other holder state, still refuses ``claim-held``.
The old run stays on disk (state ROLLBACK_INCOMPLETE, outstanding inverses intact), marked
``superseded_by``, so one run remains the auditable rollback owner (P4).
"""

from __future__ import annotations

import fcntl
import json
import os
from collections import Counter

import pytest

from tools.germinate.engine import executor as EXECUTOR
from tools.germinate.engine.backend import SimulatedKill
from tools.germinate.engine.executor import Engine, EngineRefusal
from tools.germinate.engine.run_state import State
from tools.germinate.engine.tests import helpers as H
from tools.germinate.planner_sandbox.admission import coordinator

GATES = [
    {"id": "decommission", "irreversible": True,
     "approval": {"class": "independent-agent", "grant_source": "action-broker-approval-registry"}},
    {"id": "op-gate", "approval": {"class": "operator"}},
]
LEGS = {leg: f"plan_{leg}" for leg in ("install", "configure", "update", "remove")}
GRANT = {"rm-1": "d" * 64}
COMP = "comp-a"


def _install_ops(opt_in: bool):
    extra = {"rollback_on_halt": True} if opt_in else {}
    return [
        H.dir_op("op-1", "/srv/k"),
        H.file_op("op-2", "/srv/k/a.conf", "x\n"),
        H.file_op("op-3", "/srv/k/b.conf", "y\n", **extra),
    ]


class _Bound:
    state = "bound"
    authority_record_id = "a" * 64
    reason_code = "bound"


class _Consumer:
    def validate_and_bind(self, *args):
        return _Bound()


def _setup(tmp_path, remove_ops, *, opt_in=True):
    repo = tmp_path / "repo"
    repo.mkdir()
    ops = {"install": _install_ops(opt_in), "configure": [], "update": [], "remove": remove_ops}
    _p, digest = H.write_contract(repo, COMP, ops=ops, gates=GATES, legs=LEGS)
    H.make_checkout(repo, [H.row(COMP, digest)])
    eng = H.engine(tmp_path, repo)
    eng.grant_consumer = _Consumer()
    return eng


def _gated(target="/srv/k", **extra):
    """A decommission op: gated, and (by default) covering outstanding inverse op-1's target (/srv/k)."""

    return H.dir_op("rm-1", target, gate="decommission", **extra)


def _claim(eng):
    return coordinator.read_claim(eng.backend.gates_root, "sim", COMP)


def _record(eng, run_id):
    store, tup = eng._find_run(run_id)
    with store.open(tup) as handle:
        events = [json.loads(line) for line in handle.read_transcript()]
        return handle.read_record(), events


def _stuck(eng):
    """Install HALTs at op-3 (opt-in), the auto-rollback cannot undo op-1: ROLLBACK_INCOMPLETE holds the claim."""

    eng.backend.faults.fail = {"op-3"}
    eng.backend.faults.rollback_fail = {"op-1"}
    plan = eng.plan("install", "system", COMP)
    with pytest.raises(EngineRefusal) as info:
        eng.apply("install", "system", COMP, plan_digest=plan.digest)
    assert info.value.code == "halted"
    eng.backend.faults.fail = set()
    run_id = _claim(eng)["run_id"]
    assert _record(eng, run_id)[0]["state"] == "ROLLBACK_INCOMPLETE"
    return run_id


def _remove(eng, *, grants=GRANT, accept=None):
    plan = eng.plan("remove", "system", COMP)
    return eng.apply("remove", "system", COMP, plan_digest=plan.digest, grants=grants, accept_gates=accept)


# --- 1: the gated remove is admitted -----------------------------------------------------------


def test_gated_remove_over_a_rollback_incomplete_holder_is_admitted(tmp_path):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)
    [outcome] = _remove(eng)
    assert outcome.state == "SATISFIED" and outcome.run_id != old
    assert _claim(eng) is None  # the remove run released the claim it took over


# --- 2: ungated is refused ---------------------------------------------------------------------


def test_ungated_remove_is_refused_claim_held(tmp_path):
    eng = _setup(tmp_path, [H.dir_op("rm-1", "/srv/k")])
    old = _stuck(eng)
    with pytest.raises(EngineRefusal) as info:
        eng.plan("remove", "system", COMP)
    assert info.value.code == "claim-held"
    assert _claim(eng)["run_id"] == old


def test_an_operator_only_gate_is_not_enough(tmp_path):
    eng = _setup(tmp_path, [H.dir_op("rm-1", "/srv/k", gate="op-gate")])
    old = _stuck(eng)
    with pytest.raises(EngineRefusal) as info:
        eng.plan("remove", "system", COMP)
    assert info.value.code == "claim-held"
    assert _claim(eng)["run_id"] == old


def test_the_grant_is_still_required_at_apply(tmp_path):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)
    with pytest.raises(EngineRefusal) as info:
        _remove(eng, grants=None)
    assert info.value.code == "grant-required"
    assert _claim(eng)["run_id"] == old and _record(eng, old)[0]["rollback_incomplete"].get("superseded_by") is None


# --- 3: every other verb still refuses ---------------------------------------------------------


@pytest.mark.parametrize("leg", ["install", "configure", "update"])
def test_other_legs_still_refuse_claim_held(tmp_path, leg):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)
    with pytest.raises(EngineRefusal) as info:
        eng.plan(leg, "system", COMP)
    assert info.value.code == "claim-held"
    assert _claim(eng)["run_id"] == old


# --- 4: any other holder state still refuses a gated remove ------------------------------------


def test_a_halted_holder_still_refuses_a_gated_remove(tmp_path):
    eng = _setup(tmp_path, [_gated()], opt_in=False)  # no auto-rollback: the run stays HALTED
    eng.backend.faults.fail = {"op-3"}
    plan = eng.plan("install", "system", COMP)
    with pytest.raises(EngineRefusal):
        eng.apply("install", "system", COMP, plan_digest=plan.digest)
    eng.backend.faults.fail = set()
    run_id = _claim(eng)["run_id"]
    assert _record(eng, run_id)[0]["state"] == "HALTED"
    with pytest.raises(EngineRefusal) as info:
        eng.plan("remove", "system", COMP)
    assert info.value.code == "claim-held" and _claim(eng)["run_id"] == run_id


def test_an_in_progress_holder_still_refuses_a_gated_remove(tmp_path):
    eng = _setup(tmp_path, [_gated()])
    eng.backend.faults.kill = ("op-2", "after-intent")
    plan = eng.plan("install", "system", COMP)
    with pytest.raises(SimulatedKill):
        eng.apply("install", "system", COMP, plan_digest=plan.digest)
    eng.backend.faults.kill = None
    fresh = Engine.from_checkout(eng.backend.root, repo_root=eng.repo_root)
    fresh.grant_consumer = _Consumer()
    run_id = _claim(fresh)["run_id"]
    assert _record(fresh, run_id)[0]["state"] == "ACTIVE"
    with pytest.raises(EngineRefusal) as info:
        fresh.plan("remove", "system", COMP)
    assert info.value.code == "claim-held" and _claim(fresh)["run_id"] == run_id


# --- 5: the audit links the two runs, the old record stays -------------------------------------


def test_the_takeover_links_the_runs_and_keeps_the_old_record(tmp_path):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)
    before, _ = _record(eng, old)
    [outcome] = _remove(eng)
    new = outcome.run_id
    after, old_events = _record(eng, old)
    assert after["state"] == "ROLLBACK_INCOMPLETE"  # superseded, not deleted, not re-stated
    marker = after["rollback_incomplete"]
    assert marker["superseded_by"] == new and marker["supersession_state"] == "committed"
    assert marker["outstanding_inverses"] == before["rollback_incomplete"]["outstanding_inverses"] == ["op-1"]
    assert marker["failed_run_id"] == old
    assert after["operations"] == before["operations"] and after["receipts"] == before["receipts"]
    assert [e for e in old_events if e["event"] == "rollback.superseded"][0]["superseded_by"] == new
    _rec, new_events = _record(eng, new)
    [link] = [e for e in new_events if e["event"] == "run.supersedes"]
    assert link["superseded_run_id"] == old and link["outstanding_inverses"] == ["op-1"]
    assert link["uncovered_inverses"] == []


def test_a_stale_rollback_of_the_superseded_run_is_refused(tmp_path):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)
    _remove(eng)  # the remove run released the claim: only the marker stands between a retry and the host
    eng.backend.faults.rollback_fail = set()
    with pytest.raises(EngineRefusal) as info:
        eng.rollback(old)
    assert info.value.code == "rollback-superseded"
    assert _claim(eng) is None and _record(eng, old)[0]["state"] == "ROLLBACK_INCOMPLETE"


def test_a_superseded_run_cannot_be_taken_over_again(tmp_path):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)
    _remove(eng)
    # a claim naming the superseded run (e.g. restored by hand) is no longer takeover-eligible
    coordinator.claim(eng.backend.gates_root, "sim", COMP, run_id=old, scope="system", uid=eng.uid, verb="rollback")
    with pytest.raises(EngineRefusal) as info:
        eng.plan("remove", "system", COMP)
    assert info.value.code == "claim-held"


# --- 6: regression, the same run's rollback retry still works ----------------------------------


def test_rollback_retry_by_the_same_run_still_works(tmp_path):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)
    eng.backend.faults.rollback_fail = set()
    assert eng.rollback(old).state == "ROLLED_BACK" and _claim(eng) is None


# --- 7: unconditional ownership, uncovered inverses are recorded and escalated -----------------


def test_an_uncovered_inverse_is_admitted_recorded_and_escalated(tmp_path):
    eng = _setup(tmp_path, [_gated(target="/srv/elsewhere")])  # does not touch op-1's /srv/k
    old = _stuck(eng)
    [outcome] = _remove(eng)
    assert outcome.state == "SATISFIED"
    uncovered = [{"op_id": "op-1", "target": "/srv/k"}]
    record, _events = _record(eng, old)
    assert record["rollback_incomplete"]["uncovered_inverses"] == uncovered
    _new, events = _record(eng, outcome.run_id)
    [link] = [e for e in events if e["event"] == "run.supersedes"]
    assert link["uncovered_inverses"] == uncovered
    [page] = [e for e in events if e["event"] == "escalation.page"]
    assert page["op_id"] == "op-1" and page["subject"] == "/srv/k" and page["recipient"] == "orchestrator"
    assert page["page_run_id"] == outcome.run_id and old in page["reason"]


def test_a_fully_covered_takeover_raises_no_escalation(tmp_path):
    eng = _setup(tmp_path, [_gated()])
    _stuck(eng)
    [outcome] = _remove(eng)
    _rec, events = _record(eng, outcome.run_id)
    assert [e for e in events if e["event"] == "escalation.page"] == []
    assert [e for e in events if e["event"] == "run.supersedes"][0]["uncovered_inverses"] == []


# --- the failed-create path hands the claim back ------------------------------------------------


def test_a_failed_run_admission_restores_the_old_claim(tmp_path, monkeypatch):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)
    from tools.germinate.engine import executor as EXECUTOR

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(EXECUTOR.RunStore, "create", boom)
    with pytest.raises(OSError):
        _remove(eng)
    assert _claim(eng)["run_id"] == old
    assert _record(eng, old)[0]["rollback_incomplete"].get("superseded_by") is None


# === REBOUND 1: serialization, fence-first ordering, crash reconcile ==================================


class _Crash(BaseException):
    """A process death: a BaseException, so no ``except Exception`` settles it."""


def _fence_keys(record):
    marker = record["rollback_incomplete"] or {}
    return {k: marker.get(k) for k in ("superseded_by", "supersession_state")}


def _hook(monkeypatch, point, action):
    def seam(p, **ctx):
        if p == point:
            action(**ctx)

    monkeypatch.setattr(EXECUTOR, "_supersession_seam", seam)


def _undos(eng):
    counter = Counter()
    real = eng.backend.rollback_mutation

    def counted(mutation):
        real(mutation)
        counter[mutation.operation_id] += 1

    eng.backend.rollback_mutation = counted
    return counter


def _fresh(eng):
    fresh = Engine.from_checkout(eng.backend.root, repo_root=eng.repo_root)
    fresh.grant_consumer = _Consumer()
    return fresh


def _crash_at(eng, monkeypatch, point):
    def die(**ctx):
        raise _Crash(point)

    _hook(monkeypatch, point, die)
    with pytest.raises(_Crash):
        _remove(eng)
    monkeypatch.undo()


@pytest.mark.parametrize("final", ["ROLLING_BACK", "ROLLED_BACK"])
def test_old_settling_between_the_plan_check_and_the_swap_refuses_the_takeover(tmp_path, monkeypatch, final):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)

    def settle(**ctx):
        store, tup = eng._find_run(old)
        with store.open(tup) as handle:
            handle.update(state=State.ROLLING_BACK, rollback_incomplete=None)
            if final == "ROLLED_BACK":
                handle.update(state=State.ROLLED_BACK)

    _hook(monkeypatch, "before-swap", settle)
    with pytest.raises(EngineRefusal) as info:  # not a TypeError from dict(None), not a validate failure
        _remove(eng)
    assert info.value.code == "claim-held" and "nothing swapped" in info.value.message
    assert _claim(eng)["run_id"] == old
    record, _ = _record(eng, old)
    assert record["state"] == final and _fence_keys(record) == {"superseded_by": None, "supersession_state": None}


def test_the_fence_is_written_before_the_swap_and_the_commit_flips_it(tmp_path, monkeypatch):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)
    seen = {}

    def at_fence(sup, run_id):
        seen["fence"] = _fence_keys(_record(eng, old)[0])
        seen["claim"] = _claim(eng)["run_id"]  # the claim is still the stuck run's

    _hook(monkeypatch, "after-fence", at_fence)
    [outcome] = _remove(eng)
    assert seen == {"fence": {"superseded_by": outcome.run_id, "supersession_state": "pending"}, "claim": old}
    assert _fence_keys(_record(eng, old)[0]) == {"superseded_by": outcome.run_id, "supersession_state": "committed"}


def test_rollback_and_takeover_fail_closed_while_the_claim_lock_is_held(tmp_path, monkeypatch):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)
    eng.backend.faults.rollback_fail = set()
    monkeypatch.setattr(coordinator, "_RELEASE_LOCK_WAIT_S", 0.05)
    fd = os.open(eng.backend.gates_root / "sim" / COMP, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(EngineRefusal) as info:
            eng.rollback(old)
        assert info.value.code == "primitive-unavailable"
        with pytest.raises(EngineRefusal) as info:
            _remove(eng)
        assert info.value.code == "primitive-unavailable"
    finally:
        os.close(fd)
    record, _ = _record(eng, old)
    assert record["state"] == "ROLLBACK_INCOMPLETE" and _fence_keys(record)["superseded_by"] is None
    assert _claim(eng)["run_id"] == old


def test_rollback_cannot_interleave_with_a_takeover_in_flight(tmp_path, monkeypatch):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)
    eng.backend.faults.rollback_fail = set()
    monkeypatch.setattr(coordinator, "_RELEASE_LOCK_WAIT_S", 0.05)
    undos = _undos(eng)
    caught = []

    def rollback_now(**ctx):
        with pytest.raises(EngineRefusal) as info:
            eng.rollback(old)  # fence written, swap pending, lock held by the takeover
        caught.append(info.value.code)

    _hook(monkeypatch, "after-fence", rollback_now)
    [outcome] = _remove(eng)
    assert caught == ["primitive-unavailable"] and outcome.state == "SATISFIED" and not undos


def test_a_takeover_cannot_interleave_with_a_rollback_in_flight(tmp_path, monkeypatch):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)
    eng.backend.faults.rollback_fail = set()
    monkeypatch.setattr(coordinator, "_RELEASE_LOCK_WAIT_S", 0.05)
    caught = []

    def takeover_now(**ctx):  # inside rollback's locked check-and-transition
        with pytest.raises(EngineRefusal) as info:
            _remove(eng)
        caught.append(info.value.code)

    _hook(monkeypatch, "rollback-locked", takeover_now)
    assert eng.rollback(old).state == "ROLLED_BACK"
    assert caught == ["primitive-unavailable"]
    assert _fence_keys(_record(eng, old)[0]) == {"superseded_by": None, "supersession_state": None}


def test_crash_after_the_fence_is_reconciled_and_the_stuck_run_is_retryable_again(tmp_path, monkeypatch):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)
    _crash_at(eng, monkeypatch, "after-fence")
    fresh = _fresh(eng)
    record, _ = _record(fresh, old)
    assert _fence_keys(record)["supersession_state"] == "pending" and _claim(fresh)["run_id"] == old
    fresh.backend.faults.rollback_fail = set()
    assert fresh.rollback(old).state == "ROLLED_BACK"  # reconcile cleared the orphan fence first
    record, events = _record(fresh, old)
    assert _fence_keys(record) == {"superseded_by": None, "supersession_state": None}
    assert [e for e in events if e["event"] == "supersession.reconciled"][0]["action"] == "fence-cleared"
    assert _claim(fresh) is None


def test_crash_after_the_swap_keeps_the_stuck_run_fenced_then_reconcile_hands_the_claim_back(tmp_path, monkeypatch):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)
    _crash_at(eng, monkeypatch, "after-swap")
    fresh = _fresh(eng)
    taker = _claim(fresh)["run_id"]
    assert taker != old
    undos = _undos(fresh)
    fresh.backend.faults.rollback_fail = set()
    with pytest.raises(EngineRefusal) as info:  # inside the grace window the taker may still be admitting
        fresh.rollback(old)
    assert info.value.code == "rollback-superseded" and not undos and _claim(fresh)["run_id"] == taker
    monkeypatch.setattr(EXECUTOR, "SUPERSESSION_GRACE_S", 0.0)
    assert fresh.rollback(old).state == "ROLLED_BACK"  # reconcile restored the claim, cleared the fence
    assert undos == Counter({"op-1": 1}) and _claim(fresh) is None
    _record_, events = _record(fresh, old)
    assert [e for e in events if e["event"] == "supersession.reconciled"][0]["action"] == "claim-restored"


def test_crash_after_the_create_is_rolled_forward_and_the_stuck_run_stays_fenced(tmp_path, monkeypatch):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)
    _crash_at(eng, monkeypatch, "after-create")
    fresh = _fresh(eng)
    taker = _claim(fresh)["run_id"]
    assert _record(fresh, taker)[0]["state"] == "PENDING"
    assert _fence_keys(_record(fresh, old)[0])["supersession_state"] == "pending"
    undos = _undos(fresh)
    fresh.backend.faults.rollback_fail = set()
    with pytest.raises(EngineRefusal) as info:
        fresh.rollback(old)  # reconcile rolls the commit forward first; the fence stays
    assert info.value.code == "rollback-superseded" and not undos
    record, old_events = _record(fresh, old)
    assert _fence_keys(record) == {"superseded_by": taker, "supersession_state": "committed"}
    assert [e for e in old_events if e["event"] == "rollback.superseded"]
    assert [e for e in _record(fresh, taker)[1] if e["event"] == "run.supersedes"]
    assert _claim(fresh)["run_id"] == taker  # the taker, not the stuck run, owns the component


def test_status_reconciles_a_crashed_takeover(tmp_path, monkeypatch):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)
    _crash_at(eng, monkeypatch, "after-fence")
    fresh = _fresh(eng)
    [row] = [r for r in fresh.status() if r["component"] == COMP]
    assert _fence_keys(_record(fresh, old)[0])["superseded_by"] is None
    assert row["claim"]["run_id"] == old


def test_a_late_failure_after_the_fence_clears_it(tmp_path, monkeypatch):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)

    def boom(**ctx):
        raise OSError("swap failed")

    _hook(monkeypatch, "after-fence", boom)
    with pytest.raises(OSError):
        _remove(eng)
    assert _claim(eng)["run_id"] == old
    assert _fence_keys(_record(eng, old)[0]) == {"superseded_by": None, "supersession_state": None}


# --- T-5875 REBOUND 2: the taker verifies under the lock; reconcile never clears a committed fence ---

def _executions(eng):
    seen = []
    real = eng.backend.execute

    def counted(op):
        seen.append(op.operation_id)
        return real(op)

    eng.backend.execute = counted
    return seen


def _lost_taker(eng, monkeypatch):
    """A live, slow taker: reconcile (grace 0) runs from the after-swap seam while the taker continues."""

    old = _stuck(eng)
    ran = _executions(eng)
    monkeypatch.setattr(EXECUTOR, "SUPERSESSION_GRACE_S", 0.0)
    taker = {}

    def reconcile_now(run_id, **ctx):
        taker["id"] = run_id
        eng.status()  # reconcile: no taker record yet, past grace -> the claim goes back to the stuck run

    _hook(monkeypatch, "after-swap", reconcile_now)
    with pytest.raises(EngineRefusal) as info:
        _remove(eng)
    monkeypatch.undo()
    assert info.value.code == "supersession-lost"
    return old, taker["id"], ran


def test_live_slow_taker_loses_to_reconcile_and_halts_pre_op(tmp_path, monkeypatch):
    eng = _setup(tmp_path, [_gated()])
    old, taker, ran = _lost_taker(eng, monkeypatch)
    assert ran == []  # no actuation
    record, events = _record(eng, taker)
    assert record["state"] == "HALTED" and record["halted"]["reason"].startswith("supersession-lost")
    assert [e for e in events if e["event"] == "run.supersession-lost"]
    assert _claim(eng)["run_id"] == old
    assert _fence_keys(_record(eng, old)[0]) == {"superseded_by": None, "supersession_state": None}
    undos = _undos(eng)
    eng.backend.faults.rollback_fail = set()
    assert eng.rollback(old).state == "ROLLED_BACK"  # single owner: the stuck run is retryable again
    assert undos == Counter({"op-1": 1}) and _claim(eng) is None


def test_rollback_of_lost_taker_does_not_disturb_old_claim(tmp_path, monkeypatch):
    eng = _setup(tmp_path, [_gated()])
    old, taker, ran = _lost_taker(eng, monkeypatch)
    old_before = _record(eng, old)[0]
    undos = _undos(eng)
    try:
        eng.rollback(taker)
    except EngineRefusal:
        pass
    assert _claim(eng)["run_id"] == old
    assert _record(eng, old)[0] == old_before
    assert ran == [] and not undos


def test_reconcile_after_commit_leaves_claim_and_committed_fence_alone(tmp_path, monkeypatch):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)
    monkeypatch.setattr(EXECUTOR, "SUPERSESSION_GRACE_S", 0.0)
    seen = {}

    def at_commit(sup, run_id):
        eng._reconcile_supersession(COMP)
        with eng._component_lock(COMP):  # even a direct reconcile of the (non-pending) fence is a no-op
            eng._reconcile_one(COMP, sup["tup"])
        seen["claim"] = _claim(eng)["run_id"]
        seen["fence"] = _fence_keys(_record(eng, old)[0])
        seen["events"] = [e["event"] for e in _record(eng, old)[1]]
        seen["run"] = run_id

    _hook(monkeypatch, "after-commit", at_commit)
    [outcome] = _remove(eng)
    assert outcome.run_id == seen["run"] and outcome.state == "SATISFIED"
    assert seen["claim"] == seen["run"]
    assert seen["fence"] == {"superseded_by": seen["run"], "supersession_state": "committed"}
    assert "supersession.reconciled" not in seen["events"]


def test_reconcile_with_grace_zero_rolls_forward_when_the_taker_has_a_record(tmp_path, monkeypatch):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)
    monkeypatch.setattr(EXECUTOR, "SUPERSESSION_GRACE_S", 0.0)
    seen = {}

    def at_create(run_id, **ctx):
        eng.status()
        seen["claim"] = _claim(eng)["run_id"]
        seen["fence"] = _fence_keys(_record(eng, old)[0])

    _hook(monkeypatch, "after-create", at_create)
    [outcome] = _remove(eng)  # the taker's locked verify accepts the roll-forward and completes
    assert seen["claim"] == outcome.run_id
    assert seen["fence"] == {"superseded_by": outcome.run_id, "supersession_state": "committed"}
    assert outcome.state == "SATISFIED"


def test_taker_halts_if_claim_moves_before_first_actuation(tmp_path, monkeypatch):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)
    ran = _executions(eng)

    def steal(run_id, **ctx):
        coordinator.takeover(
            eng.backend.gates_root, "sim", COMP, expected_run_id=run_id, run_id="01OTHER", scope="system", uid=eng.uid, verb="apply",
        )

    _hook(monkeypatch, "after-commit", steal)
    with pytest.raises(EngineRefusal) as info:
        _remove(eng)
    assert info.value.code == "supersession-lost" and ran == []
    assert _claim(eng)["run_id"] == "01OTHER"


def test_reconcile_never_hands_back_or_pops_a_committed_fence_without_a_taker_record(tmp_path, monkeypatch):
    eng = _setup(tmp_path, [_gated()])
    old = _stuck(eng)
    monkeypatch.setattr(EXECUTOR, "SUPERSESSION_GRACE_S", 0.0)
    store, tup = eng._find_run(old)
    with store.open(tup) as handle:
        marker = dict(handle.read_record()["rollback_incomplete"])
        marker.update(superseded_by="01GHOST", supersession_state="committed")
        handle.update(rollback_incomplete=marker)
    coordinator.takeover(eng.backend.gates_root, "sim", COMP, expected_run_id=old, run_id="01GHOST", scope="system", uid=eng.uid, verb="apply")
    with eng._component_lock(COMP):
        eng._reconcile_one(COMP, tup)
    assert _claim(eng)["run_id"] == "01GHOST"
    assert _fence_keys(_record(eng, old)[0]) == {"superseded_by": "01GHOST", "supersession_state": "committed"}
