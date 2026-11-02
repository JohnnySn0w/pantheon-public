"""T-5819: a releaser that passed its holder check can never delete a claim a
DIFFERENT run took afterwards (ORG-080 C1 / §4: one run per component).

The interleaving is driven deterministically by injecting at the gap between
reconcile_run's holder check and its release -- no threads, no timing.
"""

from __future__ import annotations

import pytest

from tools.germinate.engine import executor as EX
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.tests import helpers as H
from tools.germinate.planner_sandbox.admission import coordinator

OPS = {"install": [H.dir_op("op-1", "/srv/x"), H.file_op("op-2", "/srv/x/a.conf", "hello\n")]}


class Injected(RuntimeError):
    pass


def _claim(eng):
    return coordinator.read_claim(eng.backend.gates_root, "sim", "comp-a")


def _satisfied_run_still_holding_its_claim(eng, monkeypatch) -> str:
    """apply -> SATISFIED, but the close's release fails once: the T-5810 terminal-state row."""

    real = EX.Engine._release
    fired = []

    def fail_once(self, *args, **kwargs):
        if not fired:
            fired.append(True)
            raise Injected("injected _release")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(EX.Engine, "_release", fail_once)
    plan = eng.plan("install", "system", "comp-a")
    with pytest.raises(Injected):
        eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    monkeypatch.setattr(EX.Engine, "_release", real)
    run_id = _claim(eng)["run_id"]
    return run_id


def test_second_reconcile_refuses_and_keeps_the_fresh_claim(tmp_path, monkeypatch):
    """Two reconcile_run(X) pass the holder check; #1 releases; Y claims; #2 must not delete Y."""

    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    x = _satisfied_run_still_holding_its_claim(eng, monkeypatch)

    real_holder = EX.Engine._claim_holder
    interleaved = []

    def holder_then_interleave(self, component):
        holder = real_holder(self, component)  # #2 observes X as the holder ...
        if not interleaved:
            interleaved.append(True)
            # ... then, before #2 releases: #1 completes and a fresh run Y is admitted.
            assert eng.reconcile_run(x, executor_dead=True).state == "SATISFIED"
            coordinator.claim(eng.backend.gates_root, "sim", "comp-a", run_id="run-Y", scope="system", uid=0, verb="apply")
        return holder

    monkeypatch.setattr(EX.Engine, "_claim_holder", holder_then_interleave)
    with pytest.raises(EngineRefusal, match="claim-holder-mismatch"):
        eng.reconcile_run(x, executor_dead=True)
    assert interleaved
    assert _claim(eng)["run_id"] == "run-Y"  # Y's admission survives: a third run stays refused
    with pytest.raises(EngineRefusal, match="admission-refused"):
        eng._claim("comp-a", "run-Z", "system", "apply")


def test_engine_release_requires_the_run_id(tmp_path):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    with pytest.raises(TypeError):
        eng._release("comp-a")  # the unconditional form no longer exists


def test_zero_op_settle_surfaces_a_claim_holder_mismatch(tmp_path, monkeypatch):
    """N5: the zero-op settle path's release meets another run's claim; the typed refusal
    surfaces, not the misleading 'settling also failed ... reconcile_run' remedy."""

    from tools.germinate.engine import transcript as TR

    _repo, eng = H.simple_setup(tmp_path, ops={"install": []})
    real_emit = TR.Transcript.emit
    fired = []

    def emit_then_steal(self, event, *args, **kwargs):
        if event == "run.admitted" and not fired:
            fired.append(True)
            # another run's claim replaces ours: the C1 violation release must not paper over
            coordinator.release(eng.backend.gates_root, "sim", "comp-a", expected_run_id=_claim(eng)["run_id"])
            coordinator.claim(eng.backend.gates_root, "sim", "comp-a", run_id="run-Y", scope="system", uid=0, verb="apply")
            raise Injected("pre-op failure")
        return real_emit(self, event, *args, **kwargs)

    monkeypatch.setattr(TR.Transcript, "emit", emit_then_steal)
    plan = eng.plan("install", "system", "comp-a")
    with pytest.raises(EngineRefusal, match="claim-holder-mismatch: .*claim held by another run: investigate as tampering") as exc_info:
        eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    assert "reconcile_run" not in str(exc_info.value)
    assert _claim(eng)["run_id"] == "run-Y"
