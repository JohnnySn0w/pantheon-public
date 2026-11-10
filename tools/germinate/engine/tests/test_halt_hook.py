"""T-5868 Phase B: the engine's ``on_halted`` hook and multi-recipient halt pages.

After an EscalatingHalt is durable (HALTED record, ``halted.escalations`` marker, the page), the engine
calls the handler's optional ``on_halted(run_id, op, exc)``. If it raises, the halt stands and the SAME
recipients get a page of a distinct class (Recovery D15 condition 1). Pages dedupe per (run, op, phase,
recipient), so an owner who is also the primary recipient is paged once (condition 2)."""

from __future__ import annotations

import json

import pytest

from tools.germinate.engine.escalation import EscalatingHalt, rollback_command
from tools.germinate.engine.executor import HALT_NOT_RECORDED, EngineRefusal
from tools.germinate.engine.tests import helpers as H
from tools.germinate.planner_sandbox.admission import coordinator

HANDLERS_SRC = '''
from tools.germinate.engine.escalation import EscalatingHalt
from tools.persona_installer import MutationRecord, OperationEvidence, OperationFailed, PreflightResult


class FlagHandler:
    name = "flag-sim"
    kinds = frozenset({"flag.set"})

    def __init__(self):
        self.flag = "off"
        self.hook_calls = []

    def observe(self, component, scope):
        return {"flag": self.flag}

    def preflight(self, op):
        return PreflightResult(True, "ok", "simulated flag")

    def rollback_intent(self, op):
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, {"flag": self.flag}, "on")

    def execute(self, op):
        mutation = self.rollback_intent(op)
        self.flag = "on"
        mode = op.desired["mode"]
        if mode == "plain":
            raise OperationFailed("flag stuck (plain)")
        extra = {"two": ("orchestrator",), "same": ("watcher",), "dup": ("orchestrator", "watcher", "orchestrator"), "hookfail": ("orchestrator",)}.get(mode, ())
        raise EscalatingHalt("stuck, subject unsupervised", recipient="watcher", subject="subject-1", recipients=extra)

    def on_halted(self, run_id, op, exc):
        self.hook_calls.append((run_id, op.operation_id, exc.recipients))
        if op.desired["mode"] == "hookfail":
            raise RuntimeError("seat-state write refused")

    def verify_operation(self, op):
        return OperationEvidence(op.operation_id, op.layer, op.kind, True, "ok")

    def can_rollback(self, m):
        return PreflightResult(True, "owned", "")

    def rollback_mutation(self, m):
        self.flag = m.before["flag"]

    def dump_state(self):
        return {"flag": self.flag}

    def load_state(self, state):
        self.flag = state.get("flag", "off")


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

CID = "comp-hook"
MODES = ["plain", "two", "same", "dup", "hookfail", "ok"]


def _engine(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    H.write_contract(repo, CID, planner_src=PLANNER_SRC, privilege={"seat": ["flag.set"]},
                     legs={"install": "plan_install"}, desired_schema={"mode": {"enum": MODES, "default": "ok"}})
    digest = H.write_handlers(repo, CID, HANDLERS_SRC)
    H.make_checkout(repo, [H.row(CID, digest)])
    return H.engine(tmp_path, repo)


def _halt(eng, mode):
    plan = eng.plan("install", "system", CID, desired={"mode": mode})
    with pytest.raises(EngineRefusal) as info:
        eng.apply("install", "system", CID, plan_digest=plan.digest, desired={"mode": mode})
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    store, tup = eng._find_run(run_id)
    with store.open(tup) as handle:
        events = [e for e in map(json.loads, handle.read_transcript()) if isinstance(e, dict)]
        record = handle.read_record()
    return run_id, events, record


def _pages(events):
    return [e for e in events if e["event"] == "escalation.page"]


def test_the_hook_is_called_after_the_halt_and_page_with_the_run_id(tmp_path):
    eng = _engine(tmp_path)
    run_id, events, record = _halt(eng, "two")
    assert eng.backend.handler("flag.set").hook_calls == [(run_id, "flag-1", ("watcher", "orchestrator"))]
    assert record["state"] == "HALTED"
    assert [(p["recipient"], p["phase"]) for p in _pages(events)] == [("watcher", "halt"), ("orchestrator", "halt")]
    assert [m["recipient"] for m in record["halted"]["escalations"]] == ["watcher", "orchestrator"]
    assert all(m["rollback"] == rollback_command(run_id) for m in record["halted"]["escalations"])


def test_a_plain_halt_does_not_call_the_hook(tmp_path):
    eng = _engine(tmp_path)
    _run_id, events, _record = _halt(eng, "plain")
    assert eng.backend.handler("flag.set").hook_calls == [] and not _pages(events)


def test_a_failing_hook_leaves_the_halt_standing_and_pages_the_same_recipients_again(tmp_path):
    """Recovery D15 condition 1: the original page is sent AND a distinct 'halt not recorded' page."""
    eng = _engine(tmp_path)
    run_id, events, record = _halt(eng, "hookfail")
    assert record["state"] == "HALTED" and record["halted"]["op_id"] == "flag-1"
    pages = _pages(events)
    original = [p for p in pages if p["phase"] == "halt"]
    unrecorded = [p for p in pages if p["phase"] == "halt-unrecorded"]
    assert [p["recipient"] for p in original] == ["watcher", "orchestrator"]
    assert [p["recipient"] for p in unrecorded] == ["watcher", "orchestrator"]  # the SAME recipients
    assert HALT_NOT_RECORDED in unrecorded[0]["reason"] and "seat-state write refused" in unrecorded[0]["reason"]
    assert HALT_NOT_RECORDED == "halt not recorded on seat state; seat reads as authorised pause"
    assert unrecorded[0]["page_run_id"] == run_id
    # the halt is still rollback-admissible, and a rollback does not repeat or lose anything
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    assert len(_pages(_halt_events(eng, run_id))) == len(pages)


def _halt_events(eng, run_id):
    store, tup = eng._find_run(run_id)
    with store.open(tup) as handle:
        return [e for e in map(json.loads, handle.read_transcript()) if isinstance(e, dict)]


def test_owner_equal_to_the_primary_recipient_is_paged_once(tmp_path):
    """Recovery D15 condition 2."""
    eng = _engine(tmp_path)
    _run_id, events, record = _halt(eng, "same")
    assert [p["recipient"] for p in _pages(events)] == ["watcher"]
    assert [m["recipient"] for m in record["halted"]["escalations"]] == ["watcher"]


def test_duplicate_recipients_collapse_in_order():
    exc = EscalatingHalt("r", recipient="recovery", subject="s", recipients=("orchestrator", "recovery", "orchestrator"))
    assert exc.recipients == ("recovery", "orchestrator") and exc.recipient == "recovery"
    assert EscalatingHalt("r", recipient="recovery", subject="s").recipients == ("recovery",)
    with pytest.raises(ValueError):
        EscalatingHalt("r", recipient="recovery", subject="s", recipients=("",))


def test_run_state_of_reads_the_run_store_under_a_simulated_host_root(tmp_path):
    """The lookup the harness `wake --clear-halted` escape uses (T-5868 Phase B)."""
    from tools.germinate.engine.executor import run_state_of
    from tools.germinate.engine.run_state import RunStateError, new_ulid

    eng = _engine(tmp_path)
    run_id, _events, _record = _halt(eng, "two")
    assert run_state_of(eng.backend.root, run_id) == "HALTED"
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    assert run_state_of(eng.backend.root, run_id) == "ROLLED_BACK"
    assert run_state_of(eng.backend.root, new_ulid()) is None
    with pytest.raises(RunStateError):
        run_state_of(eng.backend.root, "not-a-ulid")
