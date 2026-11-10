"""T-5866: opt-in rollback-on-halt (Recovery D15 T-5858 (r2); ORG-080 §10, D-Y).

An op declaring ``rollback_on_halt: true`` that HALTS makes the engine run
``rollback(run_id)`` at once: the same code path as a manual rollback, so the same
carry-forward and escalation semantics. An op that does not opt in halts exactly as
before (the differential test pins that against the pre-T-5866 executor).
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import time as _real_time
import types
from collections import Counter
from pathlib import Path

import pytest

from tools.germinate.engine import run_state as RSTATE
from tools.germinate.engine import transcript as TRANSCRIPT
from tools.germinate.engine import executor as EXECUTOR
from tools.germinate.engine.backend import SimulatedHostBackend, SimulatedKill
from tools.germinate.engine.executor import Engine, EngineRefusal
from tools.germinate.engine.run_state import RunStore
from tools.germinate.engine.tests import helpers as H
from tools.germinate.engine.tests.test_escalating_halt import HANDLERS_SRC
from tools.germinate.planner_sandbox.admission import coordinator

REPO_ROOT = Path(__file__).resolve().parents[4]
#: The last main commit to touch executor.py before T-5866 (T-5233, #1501). The differential
#: guard runs THAT executor and this one over the same plans; if git cannot produce it the
#: test FAILS, never skips (ci.yml checks out with fetch-depth 0).
_PRE_T5866 = "1a23e432c5"


# --- fixtures ------------------------------------------------------------------------------


def _ops(opt_in: bool):
    extra = {"rollback_on_halt": True} if opt_in else {}
    return {
        "install": [
            H.dir_op("op-1", "/srv/k"),
            H.file_op("op-2", "/srv/k/a.conf", "x\n"),
            H.file_op("op-3", "/srv/k/b.conf", "y\n", **extra),
        ]
    }


TARGETS = ("/srv/k", "/srv/k/a.conf", "/srv/k/b.conf")


def _setup(tmp_path, opt_in=True):
    _repo, eng = H.simple_setup(tmp_path, ops=_ops(opt_in))
    return eng


def _claim(eng, cid="comp-a"):
    return coordinator.read_claim(eng.backend.gates_root, "sim", cid)


def _record(eng, run_id):
    store, tup = eng._find_run(run_id)
    with store.open(tup) as handle:
        events = [e for e in map(json.loads, handle.read_transcript()) if isinstance(e, dict)]
        return handle.read_record(), events


def _count_undos(eng, counter):
    real = eng.backend.rollback_mutation

    def counted(mutation):
        real(mutation)
        counter[mutation.operation_id] += 1  # counted only once the undo took

    eng.backend.rollback_mutation = counted


def _halt(eng, cid="comp-a", desired=None):
    plan = eng.plan("install", "system", cid, desired=desired)
    with pytest.raises(EngineRefusal) as info:
        eng.apply("install", "system", cid, plan_digest=plan.digest, desired=desired)
    assert info.value.code == "halted"
    return info.value


def _fresh(eng):
    """A new process: a new backend object and engine over the same on-disk state."""

    return Engine.from_checkout(eng.backend.root, repo_root=eng.repo_root)


# --- the op field ------------------------------------------------------------------------------


def test_opt_in_halt_auto_rolls_back_and_releases_the_claim(tmp_path):
    eng = _setup(tmp_path)
    eng.backend.faults.fail = {"op-3"}
    undos = Counter()
    _count_undos(eng, undos)
    refusal = _halt(eng)
    assert "HALTED at op-3" in refusal.message and refusal.message.endswith("; rollback_on_halt: ROLLED_BACK")
    run_id = re.search(r"run (\S+) HALTED", refusal.message).group(1)
    assert all(eng.backend.entry(t) is None for t in TARGETS)
    assert _claim(eng) is None
    record, events = _record(eng, run_id)
    assert record["state"] == "ROLLED_BACK" and record["halted"] is None and record["rollback_incomplete"] is None
    names = [e["event"] for e in events]
    # the halt is durable BEFORE the engine's decision, and the decision before any rollback state change
    assert names.index("run.halted") < names.index("rollback.auto") < names.index("rollback.started") < names.index("rollback.complete")
    assert undos == Counter({"op-2": 1, "op-1": 1})
    with pytest.raises(EngineRefusal, match="rollback-refused"):  # ROLLED_BACK is absorbing
        eng.rollback(run_id)
    eng.plan("install", "system", "comp-a")  # the component is free again


def test_a_non_opt_in_halt_still_waits_for_an_operator_rollback(tmp_path):
    eng = _setup(tmp_path, opt_in=False)
    eng.backend.faults.fail = {"op-3"}
    refusal = _halt(eng)
    assert "rollback_on_halt" not in refusal.message
    run_id = _claim(eng)["run_id"]
    record, events = _record(eng, run_id)
    assert record["state"] == "HALTED" and "rollback.auto" not in [e["event"] for e in events]
    assert eng.backend.entry("/srv/k/a.conf") is not None
    assert eng.rollback(run_id).state == "ROLLED_BACK"


def test_only_the_halting_op_s_own_opt_in_counts(tmp_path):
    """op-3 opts in, but the run halts at op-2: no automatic rollback."""

    eng = _setup(tmp_path)
    eng.backend.faults.fail = {"op-2"}
    refusal = _halt(eng)
    assert "rollback_on_halt" not in refusal.message
    assert _record(eng, _claim(eng)["run_id"])[0]["state"] == "HALTED"


def test_a_pre_op_failure_never_auto_rolls_back(tmp_path, monkeypatch):
    """Nothing was applied before the first op; the pre-op path is unchanged (T-5810)."""

    _repo, eng = H.simple_setup(tmp_path, ops={"install": [H.file_op("op-1", "/srv/a", "x\n", rollback_on_halt=True)]})
    plan = eng.plan("install", "system", "comp-a")
    real = RSTATE.RunHandle.update
    fired = []

    def flip_fails(self, **changes):  # the PENDING -> ACTIVE flip, after admission, before any op
        if not fired and changes.get("state") is RSTATE.State.ACTIVE:
            fired.append(True)
            raise OSError("simulated record write failure")
        return real(self, **changes)

    monkeypatch.setattr(RSTATE.RunHandle, "update", flip_fails)
    with pytest.raises(EngineRefusal) as info:
        eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    assert fired
    assert info.value.code == "halted" and "before op-1 ran" in info.value.message
    assert "rollback_on_halt" not in info.value.message
    record, events = _record(eng, _claim(eng)["run_id"])
    assert record["state"] == "HALTED" and "rollback.auto" not in [e["event"] for e in events]


def test_a_refused_auto_rollback_leaves_the_halt_in_place(tmp_path, monkeypatch):
    """A rollback refused before any state change: the run stays HALTED, the refusal is named."""

    eng = _setup(tmp_path)
    eng.backend.faults.fail = {"op-3"}

    def refuse(run_id):
        raise EngineRefusal("postboot-unmet", "simulated")

    monkeypatch.setattr(eng, "rollback", refuse)
    refusal = _halt(eng)
    assert refusal.message.endswith("; rollback_on_halt: postboot-unmet: simulated")
    assert _record(eng, _claim(eng)["run_id"])[0]["state"] == "HALTED"


def test_an_auto_rollback_that_raises_never_masks_the_halt(tmp_path, monkeypatch):
    eng = _setup(tmp_path)
    eng.backend.faults.fail = {"op-3"}

    def boom(run_id):
        raise OSError("disk gone")

    monkeypatch.setattr(eng, "rollback", boom)
    refusal = _halt(eng)
    assert refusal.code == "halted" and "HALTED at op-3" in refusal.message
    assert "rollback_on_halt failed (OSError: disk gone)" in refusal.message
    assert "the halt marker is kept: retry with python3 -m tools.germinate rollback --run " in refusal.message
    assert "reconcile_run(executor_dead=True), then retry" in refusal.message


def test_a_failed_auto_rollback_is_rollback_incomplete_and_the_claim_stays(tmp_path):
    eng = _setup(tmp_path)
    eng.backend.faults.fail = {"op-3"}
    eng.backend.faults.rollback_fail = {"op-2"}
    refusal = _halt(eng)
    assert "; rollback_on_halt: rollback-incomplete: " in refusal.message
    run_id = _claim(eng)["run_id"]
    record, _events = _record(eng, run_id)
    assert record["state"] == "ROLLBACK_INCOMPLETE"
    assert record["rollback_incomplete"] == {"failed_run_id": run_id, "outstanding_inverses": ["op-2"]}
    eng.backend.faults.rollback_fail = set()
    assert eng.rollback(run_id).state == "ROLLED_BACK" and _claim(eng) is None


# --- escalation: the EscalatingHalt marker plus ONE page ------------------------------------------

PLANNER_SRC = '''
def plan_install(request):
    op = {
        "operation_id": "flag-1", "layer": 1, "kind": "flag.set", "target": "flag",
        "precondition": {}, "desired": {"mode": request["desired"].get("mode", "ok")},
        "inverse": None, "privilege": "seat", "owner": "owner-1", "provenance": {},
        "crash_contract": "write-ahead-inverse-durable", "side_effect_free": False,
    }
    if request["desired"].get("opt") == "yes":
        op["rollback_on_halt"] = True
    return [op]
'''
CID = "comp-r"


def _flag_engine(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    H.write_contract(repo, CID, planner_src=PLANNER_SRC, privilege={"seat": ["flag.set"]}, legs={"install": "plan_install"},
                     desired_schema={"mode": {"enum": ["ok", "plain", "escalate", "both"], "default": "ok"},
                                     "opt": {"enum": ["yes", "no"], "default": "no"}})
    digest = H.write_handlers(repo, CID, HANDLERS_SRC)
    H.make_checkout(repo, [H.row(CID, digest)])
    return H.engine(tmp_path, repo)


def _pages(events):
    return [(e["op_id"], e["phase"]) for e in events if e.get("event") == "escalation.page"]


def test_an_escalating_opt_in_halt_pages_once_then_rolls_back_leaving_no_marker(tmp_path):
    eng = _flag_engine(tmp_path)
    refusal = _halt(eng, CID, {"mode": "escalate", "opt": "yes"})
    assert refusal.message.endswith("; rollback_on_halt: ROLLED_BACK")
    run_id = re.search(r"run (\S+) HALTED", refusal.message).group(1)
    record, events = _record(eng, run_id)
    assert record["state"] == "ROLLED_BACK" and record["rollback_incomplete"] is None
    assert _pages(events) == [("flag-1", "halt")]
    names = [e["event"] for e in events]
    assert names.index("escalation.page") < names.index("rollback.auto")  # marker + page first (Recovery (r1))
    [row] = [r for r in eng.status() if r["component"] == CID]
    assert "escalations" not in row["runs"]["system/install"]


def test_a_failed_auto_rollback_keeps_the_marker_with_exactly_one_page(tmp_path):
    """The ticket's case: the undo fails plainly, so ROLLBACK_INCOMPLETE carries the forward
    halt's marker (it would otherwise be dropped when the rollback cleared ``halted``), and the
    one page is the halt's own. A retry while still stuck keeps both and pages no more."""

    eng = _flag_engine(tmp_path)
    eng.backend.faults.rollback_fail = {"flag-1"}
    refusal = _halt(eng, CID, {"mode": "escalate", "opt": "yes"})
    assert "; rollback_on_halt: rollback-incomplete: " in refusal.message
    run_id = _claim(eng, CID)["run_id"]
    record, events = _record(eng, run_id)
    assert record["state"] == "ROLLBACK_INCOMPLETE"
    [marker] = record["rollback_incomplete"]["escalations"]
    assert marker == {"op_id": "flag-1", "subject": "subject-1", "recipient": "watcher", "reason": marker["reason"],
                      "rollback": f"python3 -m tools.germinate rollback --run {run_id}"}
    assert _pages(events) == [("flag-1", "halt")]
    [row] = [r for r in eng.status() if r["component"] == CID]
    assert row["runs"]["system/install"]["escalations"] == [marker]
    with pytest.raises(EngineRefusal, match="rollback-incomplete"):
        eng.rollback(run_id)
    record, events = _record(eng, run_id)
    assert record["rollback_incomplete"]["escalations"] == [marker] and _pages(events) == [("flag-1", "halt")]
    eng.backend.faults.rollback_fail = set()
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    record, events = _record(eng, run_id)
    assert record["rollback_incomplete"] is None and _pages(events) == [("flag-1", "halt")]


def test_without_the_opt_in_a_failed_manual_rollback_is_unchanged(tmp_path):
    """Non-opt-in: today's semantics exactly (the halted marker is not carried)."""

    eng = _flag_engine(tmp_path)
    _halt(eng, CID, {"mode": "escalate", "opt": "no"})
    run_id = _claim(eng, CID)["run_id"]
    eng.backend.faults.rollback_fail = {"flag-1"}
    with pytest.raises(EngineRefusal, match="rollback-incomplete"):
        eng.rollback(run_id)
    record, events = _record(eng, run_id)
    assert "escalations" not in record["rollback_incomplete"] and _pages(events) == [("flag-1", "halt")]


def test_an_undo_that_escalates_pages_once_per_phase(tmp_path):
    eng = _flag_engine(tmp_path)
    handler = eng.backend.handler("flag.set")
    handler.load_state({"flag": "off", "undo_stuck": True})
    eng.backend.save()
    _halt(eng, CID, {"mode": "escalate", "opt": "yes"})
    run_id = _claim(eng, CID)["run_id"]
    record, events = _record(eng, run_id)
    assert _pages(events) == [("flag-1", "halt"), ("flag-1", "rollback")]
    marks = record["rollback_incomplete"]["escalations"]
    assert [("rollback" in m) for m in marks] == [True, False]  # the halt's marker, then the undo's
    with pytest.raises(EngineRefusal, match="rollback-incomplete"):
        eng.rollback(run_id)
    _record_after, events = _record(eng, run_id)
    assert _pages(events) == [("flag-1", "halt"), ("flag-1", "rollback")]


# --- T-5960: the halt marker survives an interrupted auto-rollback -------------------------------


def _kill_engine_mid_rollback(tmp_path):
    """An escalating opt-in halt whose auto-rollback is killed at the first undo."""

    eng = _flag_engine(tmp_path)

    def die(mutation):
        raise SimulatedKill("killed during the auto-rollback")

    eng.backend.rollback_mutation = die
    plan = eng.plan("install", "system", CID, desired={"mode": "escalate", "opt": "yes"})
    with pytest.raises(SimulatedKill):
        eng.apply("install", "system", CID, plan_digest=plan.digest, desired={"mode": "escalate", "opt": "yes"})
    eng.backend.save()  # the flag is set on the host; a new process sees it
    fresh = _fresh(eng)
    return fresh, _claim(fresh, CID)["run_id"]


def _status_escalations(eng):
    [row] = [r for r in eng.status() if r["component"] == CID]
    return row["runs"]["system/install"].get("escalations")


def test_a_kill_mid_auto_rollback_then_reconcile_keeps_the_halt_marker(tmp_path):
    fresh, run_id = _kill_engine_mid_rollback(tmp_path)
    record, events = _record(fresh, run_id)
    assert record["state"] == "ROLLING_BACK"
    [marker] = record["rollback_incomplete"]["escalations"]  # durable from the ROLLING_BACK write
    assert marker["op_id"] == "flag-1" and _pages(events) == [("flag-1", "halt")]
    assert fresh.reconcile_run(run_id, executor_dead=True).state == "ROLLBACK_INCOMPLETE"
    record, events = _record(fresh, run_id)
    assert record["rollback_incomplete"]["escalations"] == [marker]
    assert record["rollback_incomplete"]["outstanding_inverses"] == ["flag-1"]
    assert _status_escalations(fresh) == [marker]
    assert _pages(events) == [("flag-1", "halt")]
    assert fresh.rollback(run_id).state == "ROLLED_BACK"
    record, events = _record(fresh, run_id)
    assert record["rollback_incomplete"] is None and _status_escalations(fresh) is None
    assert _pages(events) == [("flag-1", "halt")]


def test_reconcile_rebuilds_the_marker_from_the_transcript_for_a_pre_fix_record(tmp_path):
    fresh, run_id = _kill_engine_mid_rollback(tmp_path)
    store, tup = fresh._find_run(run_id)
    with store.open(tup) as handle:  # a record written before T-5960: ROLLING_BACK with no marker
        record = handle.read_record()
        [marker] = record["rollback_incomplete"]["escalations"]
        handle.update(rollback_incomplete=None)
    assert fresh.reconcile_run(run_id, executor_dead=True).state == "ROLLBACK_INCOMPLETE"
    record, events = _record(fresh, run_id)
    assert record["rollback_incomplete"]["escalations"] == [marker]
    assert _status_escalations(fresh) == [marker] and _pages(events) == [("flag-1", "halt")]
    assert fresh.rollback(run_id).state == "ROLLED_BACK"
    assert _pages(_record(fresh, run_id)[1]) == [("flag-1", "halt")]


def test_reconcile_pages_a_halt_whose_page_was_never_sent(tmp_path):
    """A stop is never silent: a marker with no page gets exactly one page at reconcile."""

    fresh, run_id = _kill_engine_mid_rollback(tmp_path)
    store, tup = fresh._find_run(run_id)
    with store.open(tup) as handle:
        tpath = Path(handle.path) / "transcript.jsonl"
    kept = [l for l in tpath.read_text().splitlines() if '"escalation.page"' not in l]
    tpath.write_text("\n".join(kept) + "\n")
    assert _pages(_record(fresh, run_id)[1]) == []
    fresh.reconcile_run(run_id, executor_dead=True)
    assert _pages(_record(fresh, run_id)[1]) == [("flag-1", "halt")]


def _fail_once_on_incomplete_write(monkeypatch, times):
    real = RSTATE.RunHandle.update
    left = [times]

    def flaky(self, **changes):
        if changes.get("state") is RSTATE.State.ROLLBACK_INCOMPLETE and left[0] > 0:
            left[0] -= 1
            raise OSError("simulated record write failure")
        return real(self, **changes)

    monkeypatch.setattr(RSTATE.RunHandle, "update", flaky)


def test_an_io_fault_on_the_incomplete_write_settles_in_process_with_the_marker(tmp_path, monkeypatch):
    eng = _flag_engine(tmp_path)
    eng.backend.faults.rollback_fail = {"flag-1"}  # a plain undo failure: the ROLLBACK_INCOMPLETE write is reached
    _fail_once_on_incomplete_write(monkeypatch, 1)
    refusal = _halt(eng, CID, {"mode": "escalate", "opt": "yes"})
    run_id = _claim(eng, CID)["run_id"]
    assert "rollback_on_halt failed (OSError: simulated record write failure)" in refusal.message
    assert f"python3 -m tools.germinate rollback --run {run_id}" in refusal.message
    record, events = _record(eng, run_id)
    assert record["state"] == "ROLLBACK_INCOMPLETE"  # the handler's best-effort settle
    [marker] = record["rollback_incomplete"]["escalations"]
    assert record["rollback_incomplete"]["outstanding_inverses"] == ["flag-1"]
    assert _status_escalations(eng) == [marker] and _pages(events) == [("flag-1", "halt")]
    eng.backend.faults.rollback_fail = set()
    assert eng.rollback(run_id).state == "ROLLED_BACK"  # the operator's plain retry
    assert _pages(_record(eng, run_id)[1]) == [("flag-1", "halt")]


def test_a_persistent_io_fault_leaves_rolling_back_with_the_marker_and_reconcile_keeps_it(tmp_path, monkeypatch):
    eng = _flag_engine(tmp_path)
    eng.backend.faults.rollback_fail = {"flag-1"}
    _fail_once_on_incomplete_write(monkeypatch, 99)
    refusal = _halt(eng, CID, {"mode": "escalate", "opt": "yes"})
    assert "rollback_on_halt failed (OSError" in refusal.message
    run_id = _claim(eng, CID)["run_id"]
    record, events = _record(eng, run_id)
    assert record["state"] == "ROLLING_BACK"
    [marker] = record["rollback_incomplete"]["escalations"]
    assert _status_escalations(eng) == [marker] and _pages(events) == [("flag-1", "halt")]
    monkeypatch.undo()
    eng.backend.save()
    fresh = _fresh(eng)
    assert fresh.reconcile_run(run_id, executor_dead=True).state == "ROLLBACK_INCOMPLETE"
    record, events = _record(fresh, run_id)
    assert record["rollback_incomplete"]["escalations"] == [marker] and _pages(events) == [("flag-1", "halt")]


# --- crash and re-entry: idempotent, never a double-applied inverse -------------------------------


def test_crash_before_the_rollback_starts_then_an_operator_rollback_finishes_it(tmp_path, monkeypatch):
    eng = _setup(tmp_path)
    eng.backend.faults.fail = {"op-3"}

    def die(run_id):
        raise SimulatedKill("killed before the auto-rollback started")

    monkeypatch.setattr(eng, "rollback", die)
    plan = eng.plan("install", "system", "comp-a")
    with pytest.raises(SimulatedKill):
        eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    fresh = _fresh(eng)
    run_id = _claim(fresh)["run_id"]
    record, events = _record(fresh, run_id)
    assert record["state"] == "HALTED" and "rollback.auto" in [e["event"] for e in events]
    undos = Counter()
    _count_undos(fresh, undos)
    assert fresh.rollback(run_id).state == "ROLLED_BACK"
    assert undos == Counter({"op-2": 1, "op-1": 1})
    assert all(fresh.backend.entry(t) is None for t in TARGETS) and _claim(fresh) is None


@pytest.mark.parametrize("kill_on,outstanding", [(1, ["op-2", "op-1"]), (2, ["op-1"])])
def test_crash_mid_auto_rollback_reconciles_then_retries_without_a_double_undo(tmp_path, kill_on, outstanding):
    eng = _setup(tmp_path)
    eng.backend.faults.fail = {"op-3"}
    undos = Counter()
    real = eng.backend.rollback_mutation
    calls = []

    def maybe_die(mutation):
        calls.append(mutation.operation_id)
        if len(calls) == kill_on:
            raise SimulatedKill("killed during the auto-rollback")
        real(mutation)
        undos[mutation.operation_id] += 1

    eng.backend.rollback_mutation = maybe_die
    plan = eng.plan("install", "system", "comp-a")
    with pytest.raises(SimulatedKill):
        eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    fresh = _fresh(eng)
    run_id = _claim(fresh)["run_id"]
    with pytest.raises(EngineRefusal, match="run-active: .*ROLLING_BACK"):
        fresh.rollback(run_id)
    assert fresh.reconcile_run(run_id, executor_dead=True).state == "ROLLBACK_INCOMPLETE"
    assert _record(fresh, run_id)[0]["rollback_incomplete"]["outstanding_inverses"] == outstanding
    _count_undos(fresh, undos)
    assert fresh.rollback(run_id).state == "ROLLED_BACK"
    assert undos == Counter({"op-2": 1, "op-1": 1})  # across both processes: each inverse exactly once
    assert all(fresh.backend.entry(t) is None for t in TARGETS) and _claim(fresh) is None


def test_crash_after_the_rolled_back_record_before_release(tmp_path, monkeypatch):
    eng = _setup(tmp_path)
    eng.backend.faults.fail = {"op-3"}
    undos = Counter()
    _count_undos(eng, undos)

    def die(component, run_id):
        raise SimulatedKill("killed before the claim release")

    monkeypatch.setattr(eng, "_release", die)
    plan = eng.plan("install", "system", "comp-a")
    with pytest.raises(SimulatedKill):
        eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    fresh = _fresh(eng)
    run_id = _claim(fresh)["run_id"]
    assert _record(fresh, run_id)[0]["state"] == "ROLLED_BACK"
    _count_undos(fresh, undos)
    with pytest.raises(EngineRefusal, match="rollback-refused"):
        fresh.rollback(run_id)
    assert fresh.reconcile_run(run_id, executor_dead=True).state == "ROLLED_BACK"  # finishes the close (T-5810)
    assert _claim(fresh) is None and undos == Counter({"op-2": 1, "op-1": 1})
    with pytest.raises(EngineRefusal, match="rollback-refused"):
        fresh.rollback(run_id)
    assert undos == Counter({"op-2": 1, "op-1": 1})


def test_after_a_completed_auto_rollback_re_entry_is_a_no_op(tmp_path):
    eng = _setup(tmp_path)
    eng.backend.faults.fail = {"op-3"}
    refusal = _halt(eng)
    run_id = re.search(r"run (\S+) HALTED", refusal.message).group(1)
    fresh = _fresh(eng)
    undos = Counter()
    _count_undos(fresh, undos)
    with pytest.raises(EngineRefusal, match="rollback-refused"):
        fresh.rollback(run_id)
    with pytest.raises(EngineRefusal, match="tampering-signal"):  # no claim to finish
        fresh.reconcile_run(run_id, executor_dead=True)
    assert not undos


# --- differential: no opt-in, byte-identical to the pre-T-5866 executor ---------------------------


class _FixedTime:
    """``coordinator``'s ``time``: a fixed claimed_at, the real monotonic clock and sleep."""

    monotonic = staticmethod(_real_time.monotonic)
    sleep = staticmethod(_real_time.sleep)

    @staticmethod
    def time():
        return 1_700_000_000.0


def _pre_module(tmp_path, monkeypatch, rel, name):
    shown = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{_PRE_T5866}:{rel}"], capture_output=True, text=True, check=True,
    )
    assert "rollback_on_halt" not in shown.stdout  # really the pre-T-5866 source
    path = tmp_path / f"{name}.py"
    path.write_text(shown.stdout, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _pre_executor(tmp_path, monkeypatch):
    """The pre-T-5866 executor wired to the pre-T-5866 operation module, so the plan bytes
    (plan.json, plan digests) it writes come from the old ``Operation.to_json_obj`` too."""

    operation = _pre_module(tmp_path, monkeypatch, "tools/germinate/engine/operation.py", "_t5866_pre_operation")
    module = _pre_module(tmp_path, monkeypatch, "tools/germinate/engine/executor.py", "_t5866_pre_executor")
    for name in ("Operation", "OperationError", "operation_from_json_obj", "parse_operation"):
        monkeypatch.setattr(module, name, getattr(operation, name))
    return module


def _deterministic(monkeypatch, modules):
    stamps = iter(f"2026-11-02T00:00:{i // 1000:02d}.{i % 1000:06d}Z" for i in range(10**6))

    def now_iso():
        return next(stamps)

    def new_ulid():
        return RSTATE.new_ulid(now_ms=1_700_000_000_000)

    monkeypatch.setattr(RSTATE, "secrets", types.SimpleNamespace(randbits=lambda n: 0))
    monkeypatch.setattr(RSTATE, "_last_ulid", [0, 0])
    monkeypatch.setattr(RSTATE, "now_iso", now_iso)
    monkeypatch.setattr(TRANSCRIPT, "now_iso", now_iso)
    monkeypatch.setattr(coordinator, "time", _FixedTime)
    for module in modules:
        monkeypatch.setattr(module, "new_ulid", new_ulid)
        monkeypatch.setattr(module, "now_iso", now_iso)


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes().replace(str(root).encode(), b"<ROOT>")
        for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()
    }


def _scenario(base: Path, engine_cls, refusal_cls, monkeypatch, modules):
    """Every non-opt-in path through the halt branch and rollback(): success, a plain halt
    and its rollback (a failed undo, then the retry), and an escalating halt whose undo
    escalates. Returns the outcomes and every byte the engine wrote."""

    _deterministic(monkeypatch, modules)
    out = []
    repo = base / "repo"
    repo.mkdir(parents=True)
    H.write_contract(repo, "comp-a", ops=_ops(False))
    flag_repo_contract = H.write_contract(repo, CID, planner_src=PLANNER_SRC, privilege={"seat": ["flag.set"]},
                                          legs={"install": "plan_install"},
                                          desired_schema={"mode": {"enum": ["ok", "plain", "escalate", "both"], "default": "ok"},
                                                          "opt": {"enum": ["yes", "no"], "default": "no"}})
    del flag_repo_contract
    flag_digest = H.write_handlers(repo, CID, HANDLERS_SRC)
    from tools.germinate.registry.contract_digest import compute_contract_digest

    a_digest = compute_contract_digest(repo / "tools" / "germinate" / "components" / "comp-a.yaml")
    H.make_checkout(repo, [H.row("comp-a", a_digest), H.row(CID, flag_digest)])
    eng = engine_cls.from_checkout(base / "sim", repo_root=repo)
    eng.backend.simulate_postboot()

    def attempt(fn):
        try:
            result = fn()
            out.append(("ok", repr(result)))
        except refusal_cls as exc:
            out.append((exc.code, exc.message))

    plan = eng.plan("install", "system", "comp-a")
    eng.backend.faults.fail = {"op-3"}
    attempt(lambda: eng.apply("install", "system", "comp-a", plan_digest=plan.digest))
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", "comp-a")["run_id"]
    eng.backend.faults.rollback_fail = {"op-1"}
    attempt(lambda: eng.rollback(run_id))
    eng.backend.faults = type(eng.backend.faults)()
    attempt(lambda: eng.rollback(run_id))
    attempt(lambda: eng.apply("install", "system", "comp-a", plan_digest=eng.plan("install", "system", "comp-a").digest))
    desired = {"mode": "escalate", "opt": "no"}
    attempt(lambda: eng.apply("install", "system", CID, plan_digest=eng.plan("install", "system", CID, desired=desired).digest,
                              desired=desired))
    flag_run = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    eng.backend.handler("flag.set").load_state({"flag": "on", "undo_stuck": True})
    eng.backend.save()
    attempt(lambda: eng.rollback(flag_run))
    attempt(lambda: eng.rollback(flag_run))
    out.append(("status", json.dumps(eng.status(), sort_keys=True)))
    return out, _tree(base / "sim")


def test_differential_non_opt_in_is_byte_identical_to_the_pre_t5866_executor(tmp_path, monkeypatch):
    pre = _pre_executor(tmp_path, monkeypatch)
    with monkeypatch.context() as m:
        new_out, new_tree = _scenario(tmp_path / "new" / "x", EXECUTOR.Engine, EXECUTOR.EngineRefusal, m, [EXECUTOR])
    with monkeypatch.context() as m:
        old_out, old_tree = _scenario(tmp_path / "old" / "x", pre.Engine, pre.EngineRefusal, m, [pre])
    norm = lambda items: [(c, s.replace(str(tmp_path / "new" / "x"), "<B>").replace(str(tmp_path / "old" / "x"), "<B>"))  # noqa: E731
                          for c, s in items]
    assert norm(new_out) == norm(old_out)
    assert [code for code, _ in new_out[:7]] == ["halted", "rollback-incomplete", "ok", "ok", "halted",
                                                  "rollback-incomplete", "rollback-incomplete"]
    assert set(new_tree) == set(old_tree)
    differing = sorted(k for k in new_tree if new_tree[k] != old_tree[k])
    assert not differing, differing
    assert any("transcript" in k for k in new_tree) and any(k.endswith("record.json") for k in new_tree)
