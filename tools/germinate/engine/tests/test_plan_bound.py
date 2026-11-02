"""T-5809: the run plan.json has ONE bound (run_state.MAX_PLAN_BYTES), shared by the
executor's write and RunHandle.read_plan; every other artifact keeps the generic
64 KiB MAX_ARTIFACT_BYTES. An oversized plan refuses before any claim or admission.

The bulk in these plans rides in each op's ``provenance`` (copied into plan.json but
not into record.json), the shape of a real full-seat install node: T-5091 U1's
harness-manager node is ~88 KB of plan over a ~34 KB record operations block.
"""

from __future__ import annotations

import inspect

import pytest

from tools.germinate.engine import executor as EX
from tools.germinate.engine import run_state as RS
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.tests import helpers as H
from tools.germinate.planner_sandbox.admission import coordinator
from tools.germinate.planner_sandbox.wal import AuthorityError
from tools.germinate.planner_sandbox.wal import artifacts as A
from tools.germinate.planner_sandbox.wal.artifacts import MAX_ARTIFACT_BYTES, CanonicalJsonError


def _bulky_ops(count: int, pad: int, prefix: str = "op") -> list[dict]:
    """``count`` small file writes, each carrying ``pad`` bytes of provenance."""

    return [
        H.file_op(f"{prefix}-{i}", f"/srv/bulk/{prefix}-{i}.conf", f"v{i}\n", provenance={"source": "test", "note": "x" * pad})
        for i in range(count)
    ]


def _store(eng) -> RS.RunStore:
    return RS.RunStore(eng.backend.run_state_root("system"))


def _no_run_admitted(eng) -> bool:
    root = eng.backend.run_state_root("system")
    return not root.exists() or _store(eng).all_tuples() == []


# --- the plan bound: a full-seat-sized plan round-trips ----------------------


def test_full_seat_sized_plan_writes_and_read_plan_round_trips(tmp_path):
    ops = _bulky_ops(50, 2000)  # ~120 KB of plan: over the generic cap, well under the plan bound
    _repo, eng = H.simple_setup(tmp_path, ops={"install": ops})
    plan = eng.plan("install", "system", "comp-a")
    [outcome] = eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    assert outcome.state == "SATISFIED"
    with _store(eng).open(_store(eng).find(outcome.run_id)) as handle:
        size = (handle.path / "plan.json").stat().st_size
        assert MAX_ARTIFACT_BYTES < size <= RS.MAX_PLAN_BYTES
        assert 100_000 < size < 150_000
        doc = handle.read_plan()
        assert doc["plan_digest"] == plan.digest
        assert [o["operation_id"] for o in doc["node"]["operations"]] == [o["operation_id"] for o in ops]
        assert doc["node"]["operations"][7]["provenance"]["note"] == "x" * 2000
        assert handle.read_record()["state"] == "SATISFIED"  # the record stayed under its own (generic) cap
    assert coordinator.read_claim(eng.backend.gates_root, "sim", "comp-a") is None


def test_full_seat_sized_plan_rolls_back_through_read_plan(tmp_path):
    """rollback re-reads plan.json: the read side must accept what the write side stored."""

    ops = _bulky_ops(50, 2000)
    _repo, eng = H.simple_setup(tmp_path, ops={"install": ops})
    eng.backend.faults.fail.add("op-49")
    plan = eng.plan("install", "system", "comp-a")
    with pytest.raises(EngineRefusal, match="halted"):
        eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", "comp-a")["run_id"]
    eng.backend.faults.fail.clear()
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    assert eng.backend.entry("/srv/bulk/op-0.conf") is None


# --- an oversized plan refuses before claim and admission -------------------


def test_over_bound_plan_refuses_before_claim_or_admission(tmp_path):
    ops = _bulky_ops(70, 16_000)  # ~1.1 MiB of plan; each op stays well under the generic cap
    _repo, eng = H.simple_setup(tmp_path, ops={"install": ops})
    plan = eng.plan("install", "system", "comp-a")
    before = H.tree_snapshot(eng.backend.root)
    with pytest.raises(EngineRefusal, match="plan-too-large") as info:
        eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    assert info.value.code == "plan-too-large"
    assert "comp-a" in info.value.message and str(RS.MAX_PLAN_BYTES) in info.value.message
    assert coordinator.read_claim(eng.backend.gates_root, "sim", "comp-a") is None
    assert _no_run_admitted(eng)
    assert H.tree_snapshot(eng.backend.root) == before  # no claim file, no run dir, no mutation
    assert eng.backend.entry("/srv/bulk/op-0.conf") is None


def test_over_bound_node_refuses_every_node_of_the_plan_before_any_claim(tmp_path):
    """--all: a small first node must not be admitted when a later node is over the bound."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _p, small = H.write_contract(repo, "comp-a", ops={"install": [H.file_op("a-1", "/srv/a.conf", "a\n")]})
    _p, big = H.write_contract(repo, "comp-z", ops={"install": _bulky_ops(70, 16_000, prefix="z")})
    H.make_checkout(repo, [H.row("comp-a", small), H.row("comp-z", big)])
    eng = H.engine(tmp_path, repo)
    plan = eng.plan("install", "system", None)
    assert [n.component for n in plan.nodes] == ["comp-a", "comp-z"]
    with pytest.raises(EngineRefusal, match="plan-too-large: comp-z"):
        eng.apply("install", "system", None, plan_digest=plan.digest)
    for cid in ("comp-a", "comp-z"):
        assert coordinator.read_claim(eng.backend.gates_root, "sim", cid) is None
    assert _no_run_admitted(eng)
    assert eng.backend.entry("/srv/a.conf") is None


def test_write_plan_itself_refuses_over_the_bound(tmp_path):
    store = RS.RunStore(tmp_path / "rs")
    tup = RS.RunTuple("sim", "comp-a", "system", "install", RS.new_ulid())
    with pytest.raises(RS.RunStateError, match="MAX_PLAN_BYTES"):
        store.create(tup, operations=[{"op_id": "op-1", "crash_contract": "stageable"}], plan=b"{}" + b" " * RS.MAX_PLAN_BYTES + b"\n")
    assert not (store.root / tup.relpath()).exists()  # nothing admitted: no record, no plan, no run directory


def test_encode_plan_boundary():
    def doc(n: int) -> dict:
        return {"pad": "x" * n}

    overhead = len(A.canonical_json_bytes(doc(0)))
    assert len(RS.encode_plan(doc(RS.MAX_PLAN_BYTES - overhead))) == RS.MAX_PLAN_BYTES
    with pytest.raises(CanonicalJsonError, match="json-too-large"):
        RS.encode_plan(doc(RS.MAX_PLAN_BYTES - overhead + 1))


def _exact_plan(size: int) -> bytes:
    """Canonical plan bytes of exactly ``size`` bytes (encoded with an ample bound)."""

    overhead = len(A.canonical_json_bytes({"pad": ""}))
    data = A.canonical_json_bytes({"pad": "x" * (size - overhead)}, max_bytes=size)
    assert len(data) == size
    return data


@pytest.mark.parametrize("size,accepted", [(RS.MAX_PLAN_BYTES, True), (RS.MAX_PLAN_BYTES + 1, False)])
def test_read_plan_boundary_matches_the_write_bound(tmp_path, size, accepted):
    """read_plan accepts exactly MAX_PLAN_BYTES and refuses one byte more: lockstep with encode/write."""

    data = _exact_plan(size)
    store = RS.RunStore(tmp_path / "rs")
    tup = RS.RunTuple("sim", "comp-a", "system", "install", RS.new_ulid())
    with store.create(tup, operations=[{"op_id": "op-1", "crash_contract": "stageable"}], plan=RS.encode_plan({})) as handle:
        # plant past write_plan (which would itself refuse the +1 case) to exercise the read side alone
        handle.held.replace_regular("plan.json", data, expected_uid=store.uid, mode=RS.FILE_MODE)
        if accepted:
            assert len(handle.read_plan()["pad"]) == len(data) - len(A.canonical_json_bytes({"pad": ""}))
        else:
            with pytest.raises(AuthorityError, match="file-too-large"):
                handle.read_plan()


def test_float_node_refuses_unencodable_not_too_large_before_any_claim(tmp_path, monkeypatch):
    """plan_digest's plain json.dumps accepts a float, so the digest approves; the canonical
    encode then refuses it as plan-unencodable (not plan-too-large), still before any claim."""

    from tools.germinate.engine import dag

    original = dag.PlanNode.to_json_obj
    monkeypatch.setattr(dag.PlanNode, "to_json_obj", lambda self: {**original(self), "weight": 1.5})
    _repo, eng = H.simple_setup(tmp_path, ops={"install": [H.file_op("op-1", "/srv/f.conf", "f\n")]})
    plan = eng.plan("install", "system", "comp-a")
    before = H.tree_snapshot(eng.backend.root)
    with pytest.raises(EngineRefusal) as info:
        eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    assert info.value.code == "plan-unencodable"
    assert "float-not-allowed" in info.value.message
    assert coordinator.read_claim(eng.backend.gates_root, "sim", "comp-a") is None
    assert _no_run_admitted(eng)
    assert H.tree_snapshot(eng.backend.root) == before


# --- every non-plan artifact keeps the generic 64 KiB cap --------------------


def _over_generic() -> dict:
    return {"pad": "x" * MAX_ARTIFACT_BYTES}


def test_generic_cap_is_unchanged_for_encode_and_load():
    assert MAX_ARTIFACT_BYTES == 65536
    with pytest.raises(CanonicalJsonError, match="json-too-large"):
        A.canonical_json_bytes(_over_generic())
    data = A.canonical_json_bytes(_over_generic(), max_bytes=RS.MAX_PLAN_BYTES)
    with pytest.raises(CanonicalJsonError, match="json-too-large"):
        A.load_canonical_json(data)  # default bound: the generic cap
    assert A.load_canonical_json(data, max_bytes=RS.MAX_PLAN_BYTES) == _over_generic()


def test_run_record_keeps_the_generic_cap_on_write_and_read(tmp_path):
    store = RS.RunStore(tmp_path / "rs")
    big_ops = [{"op_id": f"op-{i}", "crash_contract": "stageable", "postcondition_check": {"note": "x" * 2000}} for i in range(40)]
    with pytest.raises(Exception, match="json-too-large"):
        store.create(RS.RunTuple("sim", "comp-a", "system", "install", RS.new_ulid()), operations=big_ops, plan=RS.encode_plan({}))
    # a canonical record planted over the generic cap still refuses on read, as before T-5809
    tup = RS.RunTuple("sim", "comp-b", "system", "install", RS.new_ulid())
    with store.create(tup, operations=[{"op_id": "op-1", "crash_contract": "stageable"}], plan=RS.encode_plan({})) as handle:
        record = handle.read_record()
        record["operations"] = big_ops
        planted = A.canonical_json_bytes(record, max_bytes=RS.MAX_PLAN_BYTES)
        assert MAX_ARTIFACT_BYTES < len(planted) < RS.MAX_PLAN_BYTES
        handle.held.replace_regular("record.json", planted, expected_uid=store.uid, mode=RS.FILE_MODE)
        with pytest.raises(RS.RunStateError, match="json-too-large"):
            handle.read_record()


def test_wal_intent_artifacts_keep_the_generic_cap(tmp_path):
    """A single op whose WAL intent payload is over 64 KiB still halts the run, plan bound or not."""

    huge = H.file_op("op-1", "/srv/huge.conf", "h\n", provenance={"source": "test", "note": "x" * MAX_ARTIFACT_BYTES})
    _repo, eng = H.simple_setup(tmp_path, ops={"install": [huge]})
    plan = eng.plan("install", "system", "comp-a")
    with pytest.raises(EngineRefusal, match="json-too-large"):
        eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    assert eng.backend.entry("/srv/huge.conf") is None


# --- pin: write and read reference the same constant -------------------------


def test_plan_write_and_read_share_one_constant():
    assert RS.MAX_PLAN_BYTES == 1 << 20 and RS.MAX_PLAN_BYTES > MAX_ARTIFACT_BYTES
    read_src = inspect.getsource(RS.RunHandle.read_plan)
    assert read_src.count("MAX_PLAN_BYTES") == 2 and "1 << 20" not in read_src
    assert "MAX_PLAN_BYTES" in inspect.getsource(RS.RunHandle.write_plan)
    assert "max_bytes=MAX_PLAN_BYTES" in inspect.getsource(RS.encode_plan)
    executor_src = inspect.getsource(EX.Engine._encode_plan_node)
    assert "encode_plan(" in executor_src and "canonical_json_bytes" not in executor_src
    apply_node_src = inspect.getsource(EX.Engine._apply_node)
    assert "plan=plan_bytes" in apply_node_src and "canonical_json_bytes" not in apply_node_src
    assert "handle.write_plan(plan)" in inspect.getsource(RS.RunStore._admit)  # T-5810: create writes the plan


def test_moving_the_one_constant_moves_both_write_and_read(tmp_path, monkeypatch):
    """Behavioural pin: shrink MAX_PLAN_BYTES alone, and the encode, write AND read sides all follow it."""

    doc = {"pad": "x" * 5000}
    data = RS.encode_plan(doc)
    store = RS.RunStore(tmp_path / "rs")
    tup = RS.RunTuple("sim", "comp-a", "system", "install", RS.new_ulid())
    with store.create(tup, operations=[{"op_id": "op-1", "crash_contract": "stageable"}], plan=data) as handle:
        assert handle.read_plan() == doc
        monkeypatch.setattr(RS, "MAX_PLAN_BYTES", 4096)
        with pytest.raises(CanonicalJsonError, match="json-too-large"):
            RS.encode_plan(doc)
        with pytest.raises(RS.RunStateError, match="MAX_PLAN_BYTES"):
            handle.write_plan(data)
        with pytest.raises(AuthorityError, match="file-too-large"):  # the read side's file bound is MAX_PLAN_BYTES too
            handle.read_plan()
