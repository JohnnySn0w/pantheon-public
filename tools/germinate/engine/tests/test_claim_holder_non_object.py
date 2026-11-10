"""T-5826: ``Engine._claim_holder`` maps a non-object claim to a typed fail-closed
``EngineRefusal("primitive-unavailable")`` -- a corrupt claim must never read as
unclaimed (``None``) to any of its callers (plan, rollback, reconcile, status)."""

from __future__ import annotations

import pytest

from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.tests import helpers as H
from tools.germinate.planner_sandbox.admission import coordinator

NON_OBJECTS = ["[]", '"x"', "3", "null", "true"]


def _plant(eng, payload: str) -> None:
    coordinator.bootstrap_component(eng.backend.gates_root, eng.instance, "comp-a")
    (eng.backend.gates_root / eng.instance / "comp-a" / coordinator._CLAIM_LEAF_NAME).write_text(payload, encoding="utf-8")


@pytest.mark.parametrize("payload", NON_OBJECTS)
def test_claim_holder_non_object_refuses_primitive_unavailable(tmp_path, payload):
    _repo, eng = H.simple_setup(tmp_path)
    _plant(eng, payload)
    with pytest.raises(EngineRefusal) as exc_info:
        eng._claim_holder("comp-a")
    assert exc_info.value.code == "primitive-unavailable"


def test_claim_holder_absent_claim_is_none(tmp_path):
    _repo, eng = H.simple_setup(tmp_path)
    assert eng._claim_holder("comp-a") is None


@pytest.mark.parametrize("payload", NON_OBJECTS)
def test_status_does_not_read_a_corrupt_claim_as_free(tmp_path, payload):
    _repo, eng = H.simple_setup(tmp_path)
    _plant(eng, payload)
    with pytest.raises(EngineRefusal) as exc_info:
        eng.status()
    assert exc_info.value.code == "primitive-unavailable"
