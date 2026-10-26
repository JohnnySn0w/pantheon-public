from __future__ import annotations

import pytest

from tools.germinate.engine.planner import InProcessJsonPlannerRunner, PlanError, decode_response, encode_request

RUNNER = InProcessJsonPlannerRunner()


def _planner(tmp_path, body: str):
    path = tmp_path / "p.py"
    path.write_text(body, encoding="utf-8")
    return path


def test_round_trip_returns_plain_data(tmp_path):
    path = _planner(tmp_path, "def plan_install(request):\n    return [{'operation_id': request['component']}]\n")
    assert RUNNER.run(path, "install", {"component": "c"}) == [{"operation_id": "c"}]


def test_request_is_isolated_copy(tmp_path):
    path = _planner(tmp_path, "def plan_install(request):\n    request['observed']['x'] = 1\n    return []\n")
    request = {"observed": {}}
    RUNNER.run(path, "install", request)
    assert request == {"observed": {}}


@pytest.mark.parametrize(
    "ret",
    ["[object()]", "[lambda: 1]", "[float('nan')]", "{'not': 'a list'}", "[{1, 2}]"],
)
def test_non_data_return_refuses(tmp_path, ret):
    path = _planner(tmp_path, f"def plan_install(request):\n    return {ret}\n")
    with pytest.raises(PlanError):
        RUNNER.run(path, "install", {})


def test_raising_planner_and_missing_leg_refuse(tmp_path):
    path = _planner(tmp_path, "def plan_install(request):\n    raise RuntimeError('boom')\n")
    with pytest.raises(PlanError, match="boom"):
        RUNNER.run(path, "install", {})
    with pytest.raises(PlanError, match="no plan_remove"):
        RUNNER.run(path, "remove", {})
    broken = _planner(tmp_path, "import not_a_module_anywhere\n")
    with pytest.raises(PlanError, match="ModuleNotFoundError"):
        RUNNER.run(broken, "install", {})


def test_wire_helpers():
    with pytest.raises(PlanError):
        encode_request({"x": object()})
    with pytest.raises(PlanError):
        decode_response(b"{}")
    assert decode_response(b"[]") == []
