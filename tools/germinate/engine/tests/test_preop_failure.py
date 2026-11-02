"""T-5810: a failure after a run is admitted (record.json) but before any op runs
must never wedge the component.

- Failures inside ``RunStore.create`` (plan, manifest, record) leave no record,
  no run directory, and no claim.
- Failures after the record lands but before the first op HALT the run at its
  first op with the claim RETAINED (ORG-080 §10 halt semantics); ``rollback``
  then closes it ROLLED_BACK and releases. A zero-op node closes SATISFIED.
- If settling fails too, ``reconcile_run`` + ``rollback`` still recover it,
  because the plan and manifest always exist before the record.
- ``reconcile_run`` releases a claim still held by a SATISFIED / ROLLED_BACK run
  (ORG-080 admission matrix, terminal-state row).

The op-failure path (claim retained after an op failed) is pinned unchanged by
test_executor.py::test_op_failure_halts_and_keeps_the_claim and
::test_rollback_of_a_halted_run_restores_the_preimage.
"""

from __future__ import annotations

import pytest

from tools.germinate.engine import executor as EX
from tools.germinate.engine import run_state as RS
from tools.germinate.engine import transcript as TR
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.tests import helpers as H
from tools.germinate.planner_sandbox.admission import coordinator
from tools.germinate.planner_sandbox.wal.store import WalStore

OPS = {"install": [H.dir_op("op-1", "/srv/x"), H.file_op("op-2", "/srv/x/a.conf", "hello\n")]}


class Injected(RuntimeError):
    pass


def _apply(eng):
    plan = eng.plan("install", "system", "comp-a")
    return eng.apply("install", "system", "comp-a", plan_digest=plan.digest)


def _store(eng) -> RS.RunStore:
    return RS.RunStore(eng.backend.run_state_root("system"))


def _claim(eng):
    return coordinator.read_claim(eng.backend.gates_root, "sim", "comp-a")


def _record(eng, run_id):
    store = _store(eng)
    with store.open(store.find(run_id)) as handle:
        return handle.read_record()


def _one_shot(monkeypatch, owner, name, when=lambda *a, **k: True):
    """Make ``owner.name`` raise Injected the first time ``when`` holds, then behave normally."""

    real = getattr(owner, name)
    fired = []

    def wrapper(*args, **kwargs):
        if not fired and when(*args, **kwargs):
            fired.append(True)
            raise Injected(f"injected {name}")
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, wrapper)
    return fired


def _assert_recovers_cleanly(eng):
    """The component is admissible again: a fresh apply runs to SATISFIED."""

    [outcome] = _apply(eng)
    assert outcome.state == "SATISFIED" and _claim(eng) is None


# --- (1) failures inside RunStore.create: nothing admitted --------------------

CREATE_POINTS = [
    (RS.RunHandle, "write_plan"),
    (WalStore, "initialize_manifest"),
    (RS.RunHandle, "_write"),
]


@pytest.mark.parametrize("owner,name", CREATE_POINTS, ids=[n for _o, n in CREATE_POINTS])
def test_create_failure_admits_nothing_and_releases_the_claim(tmp_path, monkeypatch, owner, name):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    fired = _one_shot(monkeypatch, owner, name)
    with pytest.raises(Injected):
        _apply(eng)
    assert fired
    assert _claim(eng) is None
    assert _store(eng).all_tuples() == []  # no record and no record-less run directory
    assert eng.backend.entry("/srv/x") is None
    _assert_recovers_cleanly(eng)


def test_create_writes_plan_and_manifest_before_the_record(tmp_path, monkeypatch):
    """The record (the admission) is the LAST file create writes."""

    order = []
    real_plan, real_manifest, real_write = RS.RunHandle.write_plan, WalStore.initialize_manifest, RS.RunHandle._write
    monkeypatch.setattr(RS.RunHandle, "write_plan", lambda self, d: (order.append("plan"), real_plan(self, d))[1])
    monkeypatch.setattr(
        WalStore, "initialize_manifest", lambda self, **kw: (order.append("manifest"), real_manifest(self, **kw))[1]
    )
    monkeypatch.setattr(RS.RunHandle, "_write", lambda self, r: (order.append("record"), real_write(self, r))[1])
    store = RS.RunStore(tmp_path / "rs")
    tup = RS.RunTuple("sim", "comp-a", "system", "install", RS.new_ulid())
    with store.create(tup, operations=[], plan=RS.encode_plan({})) as handle:
        assert handle.read_plan() == {} and handle.wal().read_manifest().receipts == ()
    assert order == ["plan", "manifest", "record"]


def test_failed_create_leaves_a_foreign_file_and_its_directory_alone(tmp_path, monkeypatch):
    """Cleanup unlinks only the files create writes, by name, and never recurses."""

    store = RS.RunStore(tmp_path / "rs")
    tup = RS.RunTuple("sim", "comp-a", "system", "install", RS.new_ulid())
    real = WalStore.initialize_manifest

    def plant_then_fail(self, **kw):
        real(self, **kw)
        (store.root / tup.relpath() / "foreign.txt").write_text("keep me")
        raise Injected("after manifest")

    monkeypatch.setattr(WalStore, "initialize_manifest", plant_then_fail)
    with pytest.raises(Injected):
        store.create(tup, operations=[], plan=RS.encode_plan({}))
    run_dir = store.root / tup.relpath()
    assert sorted(p.name for p in run_dir.iterdir()) == ["foreign.txt"]


# --- (2) failures after the record lands, before the first op -----------------


def _is_admitted_emit(self, event, **_fields):
    return event == "run.admitted"


def _is_active_flip(self, *, state=None, **_fields):
    return state is RS.State.ACTIVE


def _nth_call(n):
    """True on the n-th call only (RunHandle.wal is first called inside create, then in _apply_node)."""

    calls = []

    def when(*_a, **_k):
        calls.append(None)
        return len(calls) == n

    return when


PRE_OP_POINTS = [
    ("wal", RS.RunHandle, "wal", _nth_call(2)),
    ("bind", TR.Transcript, "bind", lambda *a, **k: True),
    ("emit-admitted", TR.Transcript, "emit", _is_admitted_emit),
    ("active-flip", RS.RunHandle, "update", _is_active_flip),
]


@pytest.mark.parametrize("label,owner,name,when", PRE_OP_POINTS, ids=[p[0] for p in PRE_OP_POINTS])
def test_pre_op_failure_halts_at_the_first_op_with_the_claim_retained(tmp_path, monkeypatch, label, owner, name, when):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    fired = _one_shot(monkeypatch, owner, name, when)
    with pytest.raises(EngineRefusal, match="halted") as info:
        _apply(eng)
    assert fired, label
    assert isinstance(info.value.__cause__, Injected)
    holder = _claim(eng)
    assert holder is not None and holder["verb"] == "apply"  # retained, exactly as an op failure retains it
    record = _record(eng, holder["run_id"])
    assert record["state"] == "HALTED"
    assert record["halted"]["op_id"] == "op-1"
    assert record["halted"]["reason"].startswith("pre-op failure: Injected")
    assert record["receipts"] == []
    assert eng.backend.entry("/srv/x") is None  # no op ran
    with pytest.raises(EngineRefusal, match="claim-held"):
        eng.plan("install", "system", "comp-a")
    # the normal operator path recovers it: every op is BEFORE_INTENT, so rollback closes clean
    assert eng.rollback(holder["run_id"]).state == "ROLLED_BACK"
    assert _claim(eng) is None
    _assert_recovers_cleanly(eng)


def test_pre_op_halt_is_in_the_transcript_when_the_transcript_works(tmp_path, monkeypatch):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    _one_shot(monkeypatch, RS.RunHandle, "update", _is_active_flip)
    with pytest.raises(EngineRefusal, match="halted"):
        _apply(eng)
    run_id = _claim(eng)["run_id"]
    store = _store(eng)
    with store.open(store.find(run_id)) as handle:
        events = [__import__("json").loads(line)["event"] for line in handle.read_transcript()]
    assert events == ["run.admitted", "run.halted"]


def test_unsettleable_pre_op_failure_still_recovers_through_reconcile_and_rollback(tmp_path, monkeypatch):
    """The settle writes fail too: the run stays PENDING, claim held, plan + manifest on disk."""

    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    real = RS.RunHandle.update

    def always_fail(self, **kw):
        raise Injected("record writes are failing")

    monkeypatch.setattr(RS.RunHandle, "update", always_fail)
    with pytest.raises(EngineRefusal, match="pre-op-failure: .*settling it also failed"):
        _apply(eng)
    monkeypatch.setattr(RS.RunHandle, "update", real)
    run_id = _claim(eng)["run_id"]
    assert _record(eng, run_id)["state"] == "PENDING"
    assert eng.reconcile_run(run_id, executor_dead=True).state == "HALTED"
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    assert _claim(eng) is None
    _assert_recovers_cleanly(eng)


# --- zero-op nodes reach _apply_node (a converged planner returns []) ----------


def test_zero_op_node_reaches_apply_node(tmp_path):
    _repo, eng = H.simple_setup(tmp_path, ops={"install": []})
    [outcome] = _apply(eng)
    assert outcome.state == "SATISFIED" and _claim(eng) is None


def test_zero_op_pre_op_failure_closes_satisfied_and_releases(tmp_path, monkeypatch):
    _repo, eng = H.simple_setup(tmp_path, ops={"install": []})
    _one_shot(monkeypatch, TR.Transcript, "emit", _is_admitted_emit)
    with pytest.raises(EngineRefusal, match="pre-op-failure: .*zero-op run closed SATISFIED"):
        _apply(eng)
    assert _claim(eng) is None
    [tup] = _store(eng).all_tuples()
    assert _record(eng, tup.run_id)["state"] == "SATISFIED"
    _assert_recovers_cleanly(eng)


def test_zero_op_unsettleable_failure_reconciles_to_satisfied(tmp_path, monkeypatch):
    _repo, eng = H.simple_setup(tmp_path, ops={"install": []})
    real = RS.RunHandle.update
    monkeypatch.setattr(RS.RunHandle, "update", lambda self, **kw: (_ for _ in ()).throw(Injected("no writes")))
    with pytest.raises(EngineRefusal, match="pre-op-failure"):
        _apply(eng)
    monkeypatch.setattr(RS.RunHandle, "update", real)
    run_id = _claim(eng)["run_id"]
    assert eng.reconcile_run(run_id, executor_dead=True).state == "SATISFIED"
    assert _claim(eng) is None


# --- (3) reconcile_run releases a claim held by a terminal run ----------------


def test_release_failure_after_satisfied_is_recovered_by_reconcile(tmp_path, monkeypatch):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    _one_shot(monkeypatch, EX.Engine, "_release")
    with pytest.raises(Injected):
        _apply(eng)
    run_id = _claim(eng)["run_id"]
    assert _record(eng, run_id)["state"] == "SATISFIED"  # absorbing: rollback refuses it
    with pytest.raises(EngineRefusal, match="rollback-refused"):
        eng.rollback(run_id)
    assert eng.reconcile_run(run_id, executor_dead=True).state == "SATISFIED"
    assert _claim(eng) is None
    eng.plan("install", "system", "comp-a")  # admissible again


def test_release_failure_after_rolled_back_is_recovered_by_reconcile(tmp_path, monkeypatch):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    eng.backend.faults.fail.add("op-2")
    with pytest.raises(EngineRefusal, match="halted"):
        _apply(eng)
    eng.backend.faults.fail.clear()
    run_id = _claim(eng)["run_id"]
    _one_shot(monkeypatch, EX.Engine, "_release")
    with pytest.raises(Injected):
        eng.rollback(run_id)
    assert _record(eng, run_id)["state"] == "ROLLED_BACK" and _claim(eng) is not None
    assert eng.reconcile_run(run_id, executor_dead=True).state == "ROLLED_BACK"
    assert _claim(eng) is None


def test_reconcile_never_releases_a_claim_another_run_holds(tmp_path):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    [outcome] = _apply(eng)
    coordinator.claim(eng.backend.gates_root, "sim", "comp-a", run_id="other", scope="system", uid=0, verb="apply")
    with pytest.raises(EngineRefusal, match="tampering-signal"):
        eng.reconcile_run(outcome.run_id, executor_dead=True)
    assert _claim(eng)["run_id"] == "other"


# --- review round (F2, F1, F4) ---------------------------------------------------


def test_failure_after_the_record_replace_unlinks_record_first_and_admits_nothing(tmp_path, monkeypatch):
    """F2: record.json landed, then a failure (e.g. the dir fsync): cleanup goes record -> manifest -> plan."""

    store = RS.RunStore(tmp_path / "rs")
    tup = RS.RunTuple("sim", "comp-a", "system", "install", RS.new_ulid())
    real_write = RS.RunHandle._write

    def write_then_fail(self, record):
        real_write(self, record)
        raise Injected("after the record replace")

    unlinked = []
    real_unlink = RS.os.unlink

    def recording_unlink(name, *a, **kw):
        if name in RS.RunStore._CREATE_FILES:  # os is global: HeldDir's temp-file unlinks pass through too
            unlinked.append(name)
        return real_unlink(name, *a, **kw)

    monkeypatch.setattr(RS.RunHandle, "_write", write_then_fail)
    monkeypatch.setattr(RS.os, "unlink", recording_unlink)
    with pytest.raises(Injected):
        store.create(tup, operations=[], plan=RS.encode_plan({}))
    assert unlinked == ["record.json", "manifest.json", "plan.json"]
    assert not (store.root / tup.relpath()).exists()
    assert store.all_tuples() == [] and store.orphan_tuples() == []


def test_cleanup_that_stops_early_never_leaves_a_record_without_its_plan(tmp_path, monkeypatch):
    """F2: if unlinking stops at the first error, what remains is a record-less dir, never a planless record."""

    store = RS.RunStore(tmp_path / "rs")
    tup = RS.RunTuple("sim", "comp-a", "system", "install", RS.new_ulid())
    real_write = RS.RunHandle._write

    def write_then_fail(self, record):
        real_write(self, record)
        raise Injected("after the record replace")

    real_unlink = RS.os.unlink

    def fail_on_manifest(name, *a, **kw):
        if name == "manifest.json":
            raise PermissionError("cannot unlink")
        return real_unlink(name, *a, **kw)

    monkeypatch.setattr(RS.RunHandle, "_write", write_then_fail)
    monkeypatch.setattr(RS.os, "unlink", fail_on_manifest)
    with pytest.raises(Injected):
        store.create(tup, operations=[], plan=RS.encode_plan({}))
    left = sorted(p.name for p in (store.root / tup.relpath()).iterdir())
    assert left == ["manifest.json", "plan.json"]  # the record went first; the plan is still there
    assert store.orphan_tuples() == [tup]


def _orphan_run_dir(eng, monkeypatch, *, claim: bool) -> str:
    """What a crash between the manifest write and the record write leaves: a run dir with no record.json."""

    store = RS.RunStore(eng.backend.ensure_run_state_root("system"))
    tup = RS.RunTuple("sim", "comp-a", "system", "install", RS.new_ulid())

    def crash(self, record):
        raise Injected("crash before the record")

    with monkeypatch.context() as m:
        m.setattr(RS.RunHandle, "_write", crash)
        m.setattr(RS.RunStore, "_CREATE_FILES", ())  # a crash runs no cleanup
        m.setattr(RS, "_rmdir_quietly", lambda path: None)
        with pytest.raises(Injected):
            store.create(tup, operations=[], plan=RS.encode_plan({}))
    assert sorted(p.name for p in (store.root / tup.relpath()).iterdir()) == ["manifest.json", "plan.json"]
    if claim:  # the executor had claimed before create
        coordinator.bootstrap_component(eng.backend.gates_root, "sim", "comp-a")
        coordinator.claim(eng.backend.gates_root, "sim", "comp-a", run_id=tup.run_id, scope="system", uid=0, verb="apply")
    return tup.run_id


def test_status_reports_an_orphan_run_dir_instead_of_raising(tmp_path, monkeypatch):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    [done] = _apply(eng)
    orphan = _orphan_run_dir(eng, monkeypatch, claim=True)
    [row] = eng.status()
    assert row["orphan_runs"] == [orphan]
    assert row["runs"] == {"system/install": {"run_id": done.run_id, "state": "SATISFIED"}}
    assert row["claim"]["run_id"] == orphan
    assert [t.run_id for t, _rec in eng._runs()] == [done.run_id]


def test_reconcile_of_an_orphan_run_dir_is_a_typed_fail_closed_refusal(tmp_path, monkeypatch):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    orphan = _orphan_run_dir(eng, monkeypatch, claim=True)
    with pytest.raises(EngineRefusal, match="tampering-signal: .*no record.json"):
        eng.reconcile_run(orphan, executor_dead=True)
    assert _claim(eng)["run_id"] == orphan  # NOT auto-released
    with pytest.raises(EngineRefusal, match="claim-held"):
        eng.plan("install", "system", "comp-a")


def test_zero_op_satisfied_but_unreleased_names_reconcile_run_only(tmp_path, monkeypatch):
    """F4: the SATISFIED write succeeded, the release failed: the remedy is reconcile_run (rollback refuses SATISFIED)."""

    _repo, eng = H.simple_setup(tmp_path, ops={"install": []})
    _one_shot(monkeypatch, TR.Transcript, "emit", _is_admitted_emit)
    _one_shot(monkeypatch, EX.Engine, "_release")
    with pytest.raises(EngineRefusal, match="pre-op-failure") as info:
        _apply(eng)
    assert str(info.value).endswith("the claim is held: reconcile_run")
    run_id = _claim(eng)["run_id"]
    assert _record(eng, run_id)["state"] == "SATISFIED"
    assert eng.reconcile_run(run_id, executor_dead=True).state == "SATISFIED"
    assert _claim(eng) is None
