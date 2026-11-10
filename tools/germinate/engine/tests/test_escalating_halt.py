"""EscalatingHalt (T-5858, Recovery D15 (T-5858) (r1)): the generic forward-path twin of
EscalatingRollbackStop. A component's handler raises it when a forward halt leaves its
subject unsupervised; the engine halts exactly as before, plus a durable
``halted.escalations`` marker and ONE ``escalation.page`` naming the rollback command. The
engine names no persona or component concept (ORG-080 A-4); an ordinary OperationFailed
halt is unchanged."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from tools.germinate.engine import escalation
from tools.germinate.engine.escalation import EscalatingHalt, rollback_command
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.run_state import RunStateError, validate_record
from tools.germinate.engine.tests import helpers as H
from tools.germinate.planner_sandbox.admission import coordinator

HANDLERS_SRC = '''
from tools.germinate.engine.escalation import EscalatingHalt, EscalatingRollbackStop
from tools.persona_installer import MutationRecord, OperationEvidence, OperationFailed, PreflightResult


class FlagHandler:
    name = "flag-sim"
    kinds = frozenset({"flag.set"})

    def __init__(self):
        self.flag = "off"
        self.undo_stuck = False

    def observe(self, component, scope):
        return {"flag": self.flag}

    def preflight(self, op):
        return PreflightResult(True, "ok", "simulated flag")

    def rollback_intent(self, op):
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, {"flag": self.flag}, "on")

    def execute(self, op):
        mutation = self.rollback_intent(op)
        self.flag = "on"
        if op.desired["mode"] == "plain":
            raise OperationFailed("flag stuck (plain)")
        if op.desired["mode"] == "escalate":
            raise EscalatingHalt("flag stuck with the subject unsupervised", recipient="watcher", subject="subject-1")
        return mutation, OperationEvidence(op.operation_id, op.layer, op.kind, True, "set")

    def verify_operation(self, op):
        if op.desired["mode"] == "both":  # applied, then the apply-time check finds the subject unsupervised
            raise EscalatingHalt("applied but subject unsupervised", recipient="watcher", subject="subject-1")
        return OperationEvidence(op.operation_id, op.layer, op.kind, self.flag == "on", "flag check")

    def can_rollback(self, m):
        if self.flag == m.before["flag"]:
            return PreflightResult(True, "not-applied", "at preimage")
        return PreflightResult(True, "owned", "")

    def rollback_mutation(self, m):
        if self.undo_stuck:
            raise EscalatingRollbackStop("undo stopped: subject dark", recipient="watcher", subject="subject-1")
        self.flag = m.before["flag"]

    def dump_state(self):
        return {"flag": self.flag, "undo_stuck": self.undo_stuck}

    def load_state(self, state):
        self.flag = state.get("flag", "off")
        self.undo_stuck = state.get("undo_stuck", False)


HANDLERS = [FlagHandler()]
OP_KINDS = {"flag.set": ("repo.local", "seat")}
'''

PLANNER_SRC = '''
def plan_install(request):
    return [{
        "operation_id": "flag-1", "layer": 1, "kind": "flag.set", "target": "flag",
        "precondition": {}, "desired": {"mode": request["desired"].get("mode", "ok")},
        "inverse": None, "privilege": "seat", "owner": "owner-1", "provenance": {},
        "crash_contract": "write-ahead-inverse-durable", "side_effect_free": False,
    }]
'''

CID = "comp-h"


def _engine(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    H.write_contract(repo, CID, planner_src=PLANNER_SRC, privilege={"seat": ["flag.set"]},
                     legs={"install": "plan_install"}, desired_schema={"mode": {"enum": ["ok", "plain", "escalate", "both"], "default": "ok"}})
    digest = H.write_handlers(repo, CID, HANDLERS_SRC)
    H.make_checkout(repo, [H.row(CID, digest)])
    return H.engine(tmp_path, repo)


def _halt(eng, mode):
    plan = eng.plan("install", "system", CID, desired={"mode": mode})
    with pytest.raises(EngineRefusal) as info:
        eng.apply("install", "system", CID, plan_digest=plan.digest, desired={"mode": mode})
    assert info.value.code == "halted"
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    store, tup = eng._find_run(run_id)
    with store.open(tup) as handle:
        events = [json.loads(line) for line in handle.read_transcript()]
        record = handle.read_record()
    return run_id, info.value, [e for e in events if isinstance(e, dict)], record


def test_an_ordinary_halt_is_unchanged(tmp_path):
    eng = _engine(tmp_path)
    _run_id, refusal, events, record = _halt(eng, "plain")
    assert set(record["halted"]) == {"op_id", "reason", "at"}  # no escalations key at all
    assert record["halted"]["reason"] == "OperationFailed: flag stuck (plain)"
    assert not [e for e in events if e["event"] == "escalation.page"]
    [row] = [r for r in eng.status() if r["component"] == CID]
    assert "escalations" not in row["runs"]["system/install"]


def test_an_escalating_halt_marks_durably_then_pages_once_naming_the_rollback(tmp_path):
    eng = _engine(tmp_path)
    run_id, refusal, events, record = _halt(eng, "escalate")
    command = rollback_command(run_id)
    assert command == f"python3 -m tools.germinate rollback --run {run_id}"
    assert record["state"] == "HALTED" and record["halted"]["op_id"] == "flag-1"
    assert record["halted"]["escalations"] == [{
        "op_id": "flag-1", "subject": "subject-1", "recipient": "watcher",
        "reason": "flag stuck with the subject unsupervised", "rollback": command,
    }]
    [page] = [e for e in events if e["event"] == "escalation.page"]
    assert {k: page[k] for k in ("op_id", "recipient", "subject", "page_run_id", "rollback", "delivery")} == {
        "op_id": "flag-1", "recipient": "watcher", "subject": "subject-1", "page_run_id": run_id,
        "rollback": command, "delivery": "deferred-dark"}
    order = [e["event"] for e in events]
    assert order.index("run.halted") < order.index("escalation.page")
    [row] = [r for r in eng.status() if r["component"] == CID]
    assert row["runs"]["system/install"]["escalations"] == record["halted"]["escalations"]
    # rollback clears the marker; the page is never repeated
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    store, tup = eng._find_run(run_id)
    with store.open(tup) as handle:
        after = handle.read_record()
        pages = [e for e in map(json.loads, handle.read_transcript()) if isinstance(e, dict) and e.get("event") == "escalation.page"]
    assert after["halted"] is None and len(pages) == 1
    assert eng.backend.handler("flag.set").dump_state() == {"flag": "off", "undo_stuck": False}


def test_the_page_is_idempotent_per_run_and_op(tmp_path):
    eng = _engine(tmp_path)
    run_id, _refusal, _events, record = _halt(eng, "escalate")
    store, tup = eng._find_run(run_id)
    with store.open(tup) as handle:
        from tools.germinate.engine import transcript

        t = transcript.Transcript("01TESTCORRELATION0000000000").bind(handle)
        eng._page_escalations_once(handle, t, run_id, record["halted"]["escalations"])  # a re-entry
        pages = [e for e in map(json.loads, handle.read_transcript()) if isinstance(e, dict) and e.get("event") == "escalation.page"]
    assert len(pages) == 1


def _pages(eng, run_id):
    store, tup = eng._find_run(run_id)
    with store.open(tup) as handle:
        events = [e for e in map(json.loads, handle.read_transcript()) if isinstance(e, dict)]
        return [e for e in events if e.get("event") == "escalation.page"], handle.read_record(), handle


def test_one_op_pages_once_per_phase_halt_then_rollback(tmp_path):
    """Review R2 F2: an op that pages for its forward halt and whose undo later stops pages
    TWICE (phase halt, phase rollback), never swallowing the second; re-entry of either
    phase adds none (ORG-080 A-4 / the U4 (n) "ONE page per escalation" intent)."""

    from tools.germinate.engine import transcript

    eng = _engine(tmp_path)
    run_id, _refusal, events, record = _halt(eng, "both")  # applied, then the verify escalates
    assert [m["op_id"] for m in record["halted"]["escalations"]] == ["flag-1"]
    handler = eng.backend.handler("flag.set")
    handler.load_state({"flag": "on", "undo_stuck": True})
    eng.backend.save()
    for _attempt in range(2):  # a retry while still stuck pages no second time
        with pytest.raises(EngineRefusal) as info:
            eng.rollback(run_id)
        assert info.value.code == "rollback-incomplete"
    pages, record, _h = _pages(eng, run_id)
    assert [(p["op_id"], p["phase"]) for p in pages] == [("flag-1", "halt"), ("flag-1", "rollback")]
    assert "rollback" in pages[0] and "rollback" not in pages[1]
    assert [m["op_id"] for m in record["rollback_incomplete"]["escalations"]] == ["flag-1"]
    store, tup = eng._find_run(run_id)
    with store.open(tup) as handle:  # re-entry of BOTH phases' markers adds nothing
        t = transcript.Transcript("01TESTCORRELATION0000000000").bind(handle)
        halt_marker = {"op_id": "flag-1", "subject": "subject-1", "reason": "x", "recipient": "watcher",
                       "rollback": rollback_command(run_id)}
        eng._page_escalations_once(handle, t, run_id, [halt_marker] + record["rollback_incomplete"]["escalations"])
    pages, _record, _h = _pages(eng, run_id)
    assert len(pages) == 2
    handler.load_state({"flag": "on", "undo_stuck": False})
    eng.backend.save()
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    pages, _record, _h = _pages(eng, run_id)
    assert len(pages) == 2


def test_a_legacy_page_without_a_phase_still_dedupes():
    from tools.germinate.engine.executor import _page_phase

    assert _page_phase({"op_id": "x"}) == "rollback"  # a U4 rollback-stop page, written before phases
    assert _page_phase({"op_id": "x", "rollback": "cmd"}) == "halt"
    assert _page_phase({"op_id": "x", "phase": "halt"}) == "halt"


def test_the_halted_marker_schema_is_closed(tmp_path):
    eng = _engine(tmp_path)
    _run_id, _refusal, _events, record = _halt(eng, "escalate")
    validate_record(record)
    for bad in ([], [{"op_id": "flag-1"}], [dict(record["halted"]["escalations"][0], rollback="")], "x"):
        with pytest.raises(RunStateError):
            validate_record(dict(record, halted=dict(record["halted"], escalations=bad)))
    with pytest.raises(RunStateError):
        validate_record(dict(record, halted=dict(record["halted"], extra="x")))


def test_escalating_halt_requires_its_fields_and_is_an_operation_failure():
    from tools.persona_installer import OperationFailed

    for args in (("", "r", "s"), ("x", "", "s"), ("x", "r", "")):
        with pytest.raises(ValueError):
            EscalatingHalt(args[0], recipient=args[1], subject=args[2])
    assert issubclass(EscalatingHalt, OperationFailed)


def test_the_engine_names_no_persona_or_component_concept():
    for name in ("escalation.py", "executor.py", "run_state.py"):
        path = Path(escalation.__file__).with_name(name)
        strings = {n.value for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        words = ("recovery", "orchestrator", "mason", "watchman")  # no persona anywhere in the engine
        if name == "escalation.py":  # and the vocabulary module names no component concept either
            words += ("seat-state", "pause", "harness", "timer")
        for word in words:
            assert not [s for s in strings if word in s.lower().split()], (name, word)
