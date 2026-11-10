from __future__ import annotations

import json

import pytest

from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.receipts import SCHEMA as RECEIPT_SCHEMA, ReceiptError, validate_body
from tools.germinate.engine.run_state import RunStore
from tools.germinate.engine.tests import helpers as H
from tools.germinate.op_registry import load_op_registry
from tools.germinate.planner_sandbox.admission import coordinator
from tools.germinate.planner_sandbox.wal.artifacts import load_canonical_json
from tools.persona_installer import MANAGED_MARKER

OPS = {"install": [H.dir_op("op-1", "/srv/x"), H.file_op("op-2", "/srv/x/a.conf", "hello\n"), H.file_op("op-3", "/srv/x/b.conf", "b\n")]}


def _apply(eng, **kw):
    plan = eng.plan("install", "system", "comp-a")
    return eng.apply("install", "system", "comp-a", plan_digest=plan.digest, **kw)


def _handle(eng, run_id):
    store = RunStore(eng.backend.run_state_root("system"))
    return store.open(store.find(run_id))


def test_happy_path_writes_record_wal_receipts_and_transcript(tmp_path):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    [outcome] = _apply(eng)
    assert outcome.state == "SATISFIED"
    assert eng.backend.entry("/srv/x/a.conf")["state"] == "file"
    assert coordinator.read_claim(eng.backend.gates_root, "sim", "comp-a") is None
    with _handle(eng, outcome.run_id) as handle:
        record = handle.read_record()
        manifest = handle.wal().read_manifest()
        assert [r["op_id"] for r in record["receipts"]] == ["op-1", "op-2", "op-3"]
        assert record["receipts"] == [e.to_json_obj() for e in manifest.receipts]
        receipt, _ = handle.wal().read_receipt_by_ref("receipt-op-2.json")
        body = receipt.receipt_payload
        assert body["schema"] == RECEIPT_SCHEMA
        assert body["op_registry_digest"] == load_op_registry().digest
        assert body["evidence_scope"] == "simulated" and body["mutation"]["changed"] is True
        assert body["authority"] is None  # T-5233: always present; null for an op no grant authorized
        validate_body(body)
        events = [json.loads(line)["event"] for line in handle.read_transcript()]
        assert events[0] == "run.admitted" and events[-1] == "run.satisfied"
        assert events.count("op.intent") == 3 and events.count("op.receipted") == 3
        admitted = json.loads(handle.read_transcript()[0])
        assert admitted["op_registry_digest"] == eng.backend.loaded_op_registry_digest
        assert admitted["expected_runtime_subset"]["digest"].startswith("sha256:")
        assert load_canonical_json((handle.path / "plan.json").read_bytes())["plan_digest"].startswith("sha256:")


@pytest.mark.parametrize(
    "authority",
    [
        {"gate_id": "g", "scope": "system"},  # missing authority_record_id
        {"gate_id": "g", "scope": "system", "authority_record_id": "a" * 64, "approval_handle": "b" * 64},
        {"gate_id": "", "scope": "system", "authority_record_id": "a" * 64},
        "a" * 64,
    ],
)
def test_receipt_authority_shape_is_closed(tmp_path, authority):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    [outcome] = _apply(eng)
    with _handle(eng, outcome.run_id) as handle:
        body = dict(handle.wal().read_receipt_by_ref("receipt-op-1.json")[0].receipt_payload)
    body["authority"] = authority
    with pytest.raises(ReceiptError):
        validate_body(body)
    del body["authority"]
    with pytest.raises(ReceiptError):
        validate_body(body)  # the key is always present


def test_plan_is_read_only(tmp_path):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    before = H.tree_snapshot(eng.backend.root)
    eng.plan("install", "system", "comp-a")
    eng.plan("install", "seat-orchestrator", "comp-a")
    assert H.tree_snapshot(eng.backend.root) == before  # nothing written anywhere under the sim root
    assert not eng.backend.run_state_root("system").exists()
    assert eng.backend.entry("/srv/x") is None
    assert coordinator.read_claim(eng.backend.gates_root, "sim", "comp-a") is None


def test_plan_digest_mismatch_refuses_before_mutation(tmp_path):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    with pytest.raises(EngineRefusal, match="plan-digest-mismatch"):
        eng.apply("install", "system", "comp-a", plan_digest="sha256:" + "0" * 64)
    assert eng.backend.entry("/srv/x") is None and not eng.backend.run_state_root("system").exists()


def test_held_claim_refuses_plan_and_apply_before_mutation(tmp_path):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    plan = eng.plan("install", "system", "comp-a")
    coordinator.bootstrap_component(eng.backend.gates_root, "sim", "comp-a")
    coordinator.claim(eng.backend.gates_root, "sim", "comp-a", run_id="other", scope="system", uid=0, verb="apply")
    with pytest.raises(EngineRefusal, match="claim-held"):
        eng.plan("install", "system", "comp-a")
    with pytest.raises(EngineRefusal, match="claim-held"):
        eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    assert eng.backend.entry("/srv/x") is None


def test_op_failure_halts_and_keeps_the_claim(tmp_path):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    eng.backend.faults.fail.add("op-3")
    with pytest.raises(EngineRefusal, match="halted") as info:
        _apply(eng)
    holder = coordinator.read_claim(eng.backend.gates_root, "sim", "comp-a")
    assert holder is not None
    with _handle(eng, holder["run_id"]) as handle:
        record = handle.read_record()
        assert record["state"] == "HALTED" and record["halted"]["op_id"] == "op-3"
    eng.backend.faults.fail.clear()
    with pytest.raises(EngineRefusal, match="claim-held"):
        eng.plan("install", "system", "comp-a")
    assert "HALTED" in str(info.value)


def test_rollback_of_a_halted_run_restores_the_preimage(tmp_path):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    eng.backend.seed_file("/srv/x/b.conf", f"# {MANAGED_MARKER}\nold\n")
    eng.backend.faults.fail.add("op-3")
    with pytest.raises(EngineRefusal, match="injected failure"):
        _apply(eng)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", "comp-a")["run_id"]
    outcome = eng.rollback(run_id)
    assert outcome.state == "ROLLED_BACK"
    assert eng.backend.entry("/srv/x") is None and eng.backend.entry("/srv/x/a.conf") is None
    assert "old" in __import__("base64").b64decode(eng.backend.entry("/srv/x/b.conf")["content_b64"]).decode()
    assert coordinator.read_claim(eng.backend.gates_root, "sim", "comp-a") is None
    with pytest.raises(EngineRefusal, match="rollback-refused: .*absorbing"):
        eng.rollback(run_id)


def test_rollback_failure_is_a_mutation_gate_until_retry(tmp_path):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    eng.backend.faults.fail.add("op-3")
    with pytest.raises(EngineRefusal):
        _apply(eng)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", "comp-a")["run_id"]
    eng.backend.faults.fail.clear()
    eng.backend.faults.rollback_fail.add("op-2")
    with pytest.raises(EngineRefusal, match="rollback-incomplete"):
        eng.rollback(run_id)
    with _handle(eng, run_id) as handle:
        record = handle.read_record()
        assert record["state"] == "ROLLBACK_INCOMPLETE"
        assert record["rollback_incomplete"] == {"failed_run_id": run_id, "outstanding_inverses": ["op-2"]}
    with pytest.raises(EngineRefusal, match="claim-held"):
        eng.plan("install", "system", "comp-a")
    eng.backend.faults.rollback_fail.clear()
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    assert eng.backend.entry("/srv/x/a.conf") is None and eng.backend.entry("/srv/x") is None


def test_satisfied_run_is_absorbing_for_rollback(tmp_path):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    [outcome] = _apply(eng)
    with pytest.raises(EngineRefusal, match="rollback-refused"):
        eng.rollback(outcome.run_id)


def test_gates(tmp_path):
    gated = {"install": [H.dir_op("op-1", "/srv/g", gate="restart")]}
    gates = [
        {"id": "restart", "approval": {"class": "operator"}},
        {"id": "decommission", "approval": {"class": "independent-agent", "grant_source": "broker"}},
    ]
    _repo, eng = H.simple_setup(tmp_path, ops=gated, gates=gates, reserved_authority=[{"id": "seat-plan-flip"}])
    plan = eng.plan("install", "system", "comp-a")
    with pytest.raises(EngineRefusal, match="gate-unanswered"):
        eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    with pytest.raises(EngineRefusal, match="gate-unanswered"):
        eng.apply("install", "system", "comp-a", plan_digest=plan.digest, accept_gates=frozenset({"other"}))
    assert eng.backend.entry("/srv/g") is None
    [outcome] = eng.apply("install", "system", "comp-a", plan_digest=plan.digest, accept_gates=frozenset({"restart"}))
    assert outcome.state == "SATISFIED"


@pytest.mark.parametrize("gate,code", [("decommission", "grant-required"), ("seat-plan-flip", "reserved-authority")])
def test_unanswerable_gates_refuse_even_with_accept(tmp_path, gate, code):
    gated = {"install": [H.dir_op("op-1", "/srv/g", gate=gate)]}
    gates = [{"id": "decommission", "approval": {"class": "independent-agent", "grant_source": "broker"}}]
    _repo, eng = H.simple_setup(tmp_path, ops=gated, gates=gates, reserved_authority=[{"id": "seat-plan-flip"}])
    plan = eng.plan("install", "system", "comp-a")
    with pytest.raises(EngineRefusal, match=code):
        eng.apply("install", "system", "comp-a", plan_digest=plan.digest, accept_gates=frozenset())
    assert eng.backend.entry("/srv/g") is None


def test_postboot_is_a_precondition_of_every_verb(tmp_path):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    eng.backend.postboot_marker_path.unlink()
    calls = [
        lambda: eng.plan("install", "system", "comp-a"),
        lambda: eng.apply("install", "system", "comp-a", plan_digest="x"),
        lambda: eng.verify("system"),
        lambda: eng.rollback("01M53XS0FFKNZ95Y6QCXVX2A1V"),
        lambda: eng.status(),
        lambda: eng.reconcile_run("01M53XS0FFKNZ95Y6QCXVX2A1V", executor_dead=True),
    ]
    for call in calls:
        with pytest.raises(EngineRefusal, match="postboot-unmet"):
            call()


def test_stale_and_malformed_postboot_marker_refuse(tmp_path):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    eng.backend.simulate_boot("00000000-0000-4000-8000-000000000002")  # a reboot
    with pytest.raises(EngineRefusal, match="postboot-unmet: .*stale"):
        eng.status()
    eng.backend.postboot_marker_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(EngineRefusal, match="primitive-unavailable"):
        eng.status()


def test_malformed_claim_fails_closed(tmp_path):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    coordinator.bootstrap_component(eng.backend.gates_root, "sim", "comp-a")
    (eng.backend.gates_root / "sim" / "comp-a" / "_component_gate.claim").write_text("{broken")
    with pytest.raises(EngineRefusal, match="primitive-unavailable"):
        eng.plan("install", "system", "comp-a")


def test_contract_refusals_surface_at_plan(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _p, digest = H.write_contract(repo, "comp-a", ops={"install": [H.file_op("op-1", "/t", "x", crash_contract=None)]})
    H.make_checkout(repo, [H.row("comp-a", digest)])
    with pytest.raises(EngineRefusal, match="plan-refused: .*D-X"):
        H.engine(tmp_path, repo).plan("install", "system", "comp-a")


def test_registry_digest_mismatch_and_unpinned_refuse(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    H.write_contract(repo, "comp-a")
    H.make_checkout(repo, [H.row("comp-a", "sha256:" + "9" * 64), H.row("comp-b", "unpinned", contract={"repo": "pantheon-config", "module": "m.yaml", "digest": "unpinned", "unpinned_reason": "none yet"})])
    eng = H.engine(tmp_path, repo)
    with pytest.raises(EngineRefusal, match="digest-mismatch"):
        eng.plan("install", "system", "comp-a")
    # every registered contract is loaded for the ORG-082 §2a union, so a broken one blocks all plans
    with pytest.raises(EngineRefusal, match="digest-mismatch"):
        eng.plan("install", "system", "comp-b")
    other = tmp_path / "other"
    other.mkdir()
    H.make_checkout(other, [H.row("comp-b", "unpinned", contract={"repo": "pantheon-config", "module": "m.yaml", "digest": "unpinned", "unpinned_reason": "none yet"})])
    with pytest.raises(EngineRefusal, match="contract-unpinned"):
        H.engine(other, other).plan("install", "system", "comp-b")


def test_verify_detects_drift(tmp_path):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    _apply(eng)
    assert all(r["ok"] for r in eng.verify("system", "comp-a"))
    eng.backend.seed_file("/srv/x/a.conf", "hand edit\n")
    [result] = eng.verify("system", "comp-a")
    assert not result["ok"] and [c["op_id"] for c in result["checks"] if not c["ok"]] == ["op-2"]


def test_repair_plan_receives_the_verify_result(tmp_path):
    planner = (
        "def plan_install(request):\n    return []\n"
        "def plan_remove(request):\n    return []\n"
        "def plan_repair(request):\n"
        "    bad = [c['op_id'] for c in request['verify_result']['checks'] if not c['ok']]\n"
        "    return [] if not bad else [{'operation_id': 'fix-' + bad[0], 'layer': 1, 'kind': 'mkdir', 'target': '/srv/fixed',"
        " 'precondition': {}, 'desired': {'mode': '0755'}, 'inverse': None, 'privilege': 'seat', 'owner': 'sim',"
        " 'provenance': {}, 'crash_contract': 'stageable', 'side_effect_free': False}]\n"
    )
    _repo, eng = H.simple_setup(tmp_path, planner_src=planner, legs={"install": "plan_install", "remove": "plan_remove", "repair": "plan_repair"})
    assert eng.plan("repair", "system", "comp-a").nodes[0].operations == ()


def test_status_joins_registry_and_run_state(tmp_path):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    [outcome] = _apply(eng)
    [row] = eng.status()
    assert row["component"] == "comp-a" and row["claim"] is None
    assert row["runs"] == {"system/install": {"run_id": outcome.run_id, "state": "SATISFIED"}}


def test_seat_scope_uses_its_own_store(tmp_path):
    _repo, eng = H.simple_setup(tmp_path, ops=OPS)
    plan = eng.plan("install", "seat-orchestrator", "comp-a")
    [outcome] = eng.apply("install", "seat-orchestrator", "comp-a", plan_digest=plan.digest)
    assert RunStore(eng.backend.run_state_root("seat-orchestrator")).find(outcome.run_id) is not None
    assert not eng.backend.run_state_root("system").exists()
