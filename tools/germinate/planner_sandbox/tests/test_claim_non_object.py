"""T-5826: a claim file that is valid JSON but not an object (``[]``, a string,
a number, ``null``, ``true``) fails closed at EVERY coordinator entry point with a
typed ``PrimitiveUnavailable`` (ORG-080 C4) -- never a raw crash, never "unclaimed".
A genuinely absent claim file is still the distinct "unclaimed" (``None``)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.germinate.planner_sandbox.admission import coordinator
from tools.germinate.planner_sandbox.admission.exceptions import PrimitiveUnavailable

INST, COMP = "inst", "comp"
NON_OBJECTS = ["[]", '"x"', "3", "null", "true"]


@pytest.fixture
def gates(tmp_path):
    coordinator.bootstrap_component(tmp_path, INST, COMP)
    return tmp_path


def _canonical(gates) -> Path:
    return gates / INST / COMP / coordinator._CLAIM_LEAF_NAME


def _plant(gates, payload: str) -> Path:
    path = _canonical(gates)
    path.write_text(payload, encoding="utf-8")
    return path


def _claim(gates):
    coordinator.claim(gates, INST, COMP, run_id="new", scope="system", uid=os.getuid(), verb="apply")


@pytest.mark.parametrize("payload", NON_OBJECTS)
def test_read_claim_non_object_is_primitive_unavailable(gates, payload):
    _plant(gates, payload)
    with pytest.raises(PrimitiveUnavailable, match="expected a JSON object"):
        coordinator.read_claim(gates, INST, COMP)


@pytest.mark.parametrize("payload", NON_OBJECTS)
def test_claim_over_non_object_claim_is_primitive_unavailable_and_keeps_it(gates, payload):
    canonical = _plant(gates, payload)
    with pytest.raises(PrimitiveUnavailable, match="expected a JSON object"):
        _claim(gates)
    assert canonical.read_text(encoding="utf-8") == payload
    assert [p.name for p in canonical.parent.iterdir()] == [coordinator._CLAIM_LEAF_NAME]  # temp file cleaned


@pytest.mark.parametrize("payload", NON_OBJECTS)
def test_release_non_object_claim_is_primitive_unavailable_and_keeps_it(gates, payload):
    canonical = _plant(gates, payload)
    with pytest.raises(PrimitiveUnavailable, match="expected a JSON object"):
        coordinator.release(gates, INST, COMP, expected_run_id="X")
    assert canonical.read_text(encoding="utf-8") == payload


def test_absent_claim_is_still_unclaimed_not_an_error(gates):
    assert coordinator.read_claim(gates, INST, COMP) is None
    coordinator.release(gates, INST, COMP, expected_run_id="X")
    _claim(gates)
    assert coordinator.read_claim(gates, INST, COMP)["run_id"] == "new"


def test_malformed_claim_unchanged(gates):
    _plant(gates, "{broken")
    with pytest.raises(PrimitiveUnavailable, match="malformed"):
        coordinator.read_claim(gates, INST, COMP)
    with pytest.raises(PrimitiveUnavailable, match="malformed"):
        _claim(gates)
