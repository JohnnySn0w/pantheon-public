"""T-5092 (germination GG): the credential-broker component, ADOPT-ONLY over
tools/credential_broker.py + tools/credential_broker_cli.py (both unchanged).

Engine-tier tests run on a tmp checkout (copies of the three component files and
this repo's instance.yaml, read-only) over a simulated host. ``_seed_refs`` is a
TEST HELPER: it writes reference METADATA (presence, mode) into the simulated
host; the handler materializes a fixed placeholder file per reference (never a
secret) that ``HostFileCredentialBroker.verify`` stats. No lifecycle verb does that.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tools import credential_broker_cli as CLI
from tools.germinate.engine.backend import KILL_POINTS, SimulatedKill
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.run_state import RunStore
from tools.germinate.engine.tests import helpers as H
from tools.germinate.planner_sandbox.admission import coordinator
from tools.germinate.registry.contract_digest import compute_contract_digest
from tools.germinate.registry.repin import repin
from tools.persona_installer import Manifest

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPONENTS = REPO_ROOT / "tools" / "germinate" / "components"
FILES = ("credential-broker.yaml", "credential-broker.py", "credential-broker_handlers.py")
CID = "credential-broker"
SCOPE = "system"
KIND = "credential-broker.adopt-attest"


def _declared():
    manifest = Manifest.load(REPO_ROOT / "instance.yaml", strict_flip_keys=False)
    out = []
    for raw in manifest.roles.values():
        persona = raw.get("persona") if isinstance(raw, dict) else None
        if persona:
            binding = manifest.binding(persona)
            out.extend((ref.owner, ref.reference_id) for ref in binding.credential_refs)
    return out


DECLARED = _declared()
REF_IDS = sorted(ref for _a, ref in DECLARED)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    directory = repo / "tools" / "germinate" / "components"
    directory.mkdir(parents=True)
    for name in FILES:
        shutil.copy(COMPONENTS / name, directory / name)
    shutil.copy(REPO_ROOT / "instance.yaml", repo / "instance.yaml")
    H.make_checkout(repo, [H.row(CID, compute_contract_digest(directory / "credential-broker.yaml"), steward="codex")])
    return repo


def _handler(eng):
    return eng.backend.handler(KIND)


def _state(eng):
    return _handler(eng).dump_state()


def _write(eng, mutate) -> None:
    """TEST HELPER: edit the simulated reference metadata and persist it."""

    state = _state(eng)
    mutate(state)
    _handler(eng).load_state(state)
    eng.backend.save()


def _seed_refs(state) -> None:
    """TEST HELPER: every declared reference present at mode 0600 (metadata only)."""

    state["refs"] = {ref: {"present": True, "mode": "0600"} for ref in REF_IDS}


@pytest.fixture
def healthy(tmp_path):
    repo = _repo(tmp_path)
    eng = H.engine(tmp_path, repo)
    _write(eng, _seed_refs)
    return repo, eng


def _ops(plan):
    [node] = plan.nodes
    return node.operations


def _apply(eng, leg):
    plan = eng.plan(leg, SCOPE, CID)
    [outcome] = eng.apply(leg, SCOPE, CID, plan_digest=plan.digest)
    return plan, outcome


def test_real_manifest_declares_references():
    assert REF_IDS and all("/" in ref for ref in REF_IDS)


def test_healthy_refs_plan_one_attest_op(healthy):
    _repo_, eng = healthy
    [op] = _ops(eng.plan("install", SCOPE, CID))
    assert (op.kind, op.target, op.privilege, op.crash_contract) == (KIND, "attestation:credential-broker", "seat", "write-ahead-inverse-durable")
    assert {owner for owner, _ref in DECLARED} == {op.owner}  # the references' one declared owner (review F5)
    assert op.desired["verify_rc"] == "0" and op.desired["refs"] == ",".join(REF_IDS)
    assert op.provenance["basis"] == "observed-legacy"
    assert "HostFileCredentialBroker.verify" in op.provenance["planner"]


def test_attest_applies_satisfied_and_rerun_plans_nothing(healthy):
    _repo_, eng = healthy
    plan, outcome = _apply(eng, "install")
    assert outcome.state == "SATISFIED"
    assert _state(eng)["attestation"] == dict(_ops(plan)[0].desired)
    assert _ops(eng.plan("install", SCOPE, CID)) == ()
    assert eng._satisfied_install(CID, SCOPE)


def test_receipt_records_the_attested_evidence(healthy):
    _repo_, eng = healthy
    plan, outcome = _apply(eng, "install")
    [op] = _ops(plan)
    store = RunStore(eng.backend.run_state_root(SCOPE))
    with store.open(store.find(outcome.run_id)) as handle:
        wal = handle.wal()
        receipt, _ = wal.read_receipt_by_ref(wal.classify_prefix(op_id=op.operation_id).receipt_ref)
    payload = receipt.receipt_payload
    assert json.loads(payload["mutation"]["after_fingerprint"]) == dict(op.desired)
    assert "attested broker verify rc 0" in payload["evidence"]["detail"]


def test_no_secret_bytes_reach_plan_or_state(healthy):
    _repo_, eng = healthy
    plan, _outcome = _apply(eng, "install")
    text = json.dumps([op.to_json_obj() for op in _ops(plan)]) + json.dumps(_state(eng))
    assert "placeholder" not in text


def test_healthy_drift_re_attests_and_verify_reports_it(healthy):
    _repo_, eng = healthy
    first, _outcome = _apply(eng, "install")
    _write(eng, lambda s: s["refs"][REF_IDS[0]].update(expires_at=4102444800.0))  # still valid, report moved
    [result] = eng.verify(SCOPE, CID)
    assert not result["ok"] and "report_sha256" in result["checks"][0]["detail"]
    [op] = _ops(eng.plan("install", SCOPE, CID))
    assert op.provenance["re_attest"] == "true" and op.desired["report_sha256"] != _ops(first)[0].desired["report_sha256"]
    _apply(eng, "install")
    assert eng.verify(SCOPE, CID)[0]["ok"]


def test_unhealthy_refuses_with_the_cli_gate_message_and_never_re_attests(healthy):
    _repo_, eng = healthy
    _apply(eng, "install")
    attested = _state(eng)["attestation"]
    _write(eng, lambda s: s["refs"][REF_IDS[0]].update(mode="0644"))  # the broker refuses a loose mode
    [result] = eng.verify(SCOPE, CID)
    assert not result["ok"] and "verify_rc now 1" in result["checks"][0]["detail"]
    with pytest.raises(EngineRefusal) as info:
        eng.plan("install", SCOPE, CID)
    assert info.value.code == "plan-refused" and CLI.GATE_MESSAGE in info.value.message
    assert "never attested" in info.value.message and REF_IDS[0] in info.value.message
    assert _state(eng)["attestation"] == attested


def test_no_legacy_setup_refuses(tmp_path):
    repo = _repo(tmp_path)
    eng = H.engine(tmp_path, repo)
    with pytest.raises(EngineRefusal, match="no healthy legacy credential setup .*FileNotFoundError") as info:
        eng.plan("install", SCOPE, CID)
    assert CLI.GATE_MESSAGE in info.value.message and "T-2869" in info.value.message


def test_undeclared_legs_refuse(healthy):
    _repo_, eng = healthy
    for leg in ("configure", "repair"):
        with pytest.raises(EngineRefusal) as info:
            eng.plan(leg, SCOPE, CID)
        assert info.value.code == "leg-not-declared"


def test_remove_retires_only_the_attestation(healthy):
    _repo_, eng = healthy
    assert _ops(eng.plan("remove", SCOPE, CID)) == ()
    _apply(eng, "install")
    refs = _state(eng)["refs"]
    plan, outcome = _apply(eng, "remove")
    assert [op.kind for op in _ops(plan)] == ["credential-broker.attest-retire"] and outcome.state == "SATISFIED"
    assert _state(eng)["attestation"] is None and _state(eng)["refs"] == refs


@pytest.mark.parametrize("point", KILL_POINTS)
def test_kill_then_reconcile_then_rollback_removes_the_attestation(tmp_path, point):
    repo = _repo(tmp_path)
    eng = H.engine(tmp_path, repo)
    _write(eng, _seed_refs)
    plan = eng.plan("install", SCOPE, CID)
    [op] = _ops(plan)
    eng.backend.faults.kill = (op.operation_id, point)
    with pytest.raises(SimulatedKill):
        eng.apply("install", SCOPE, CID, plan_digest=plan.digest)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    restarted = H.engine(tmp_path, repo)
    outcome = restarted.reconcile_run(run_id, executor_dead=True)
    if point in ("after-manifest", "after-record"):
        assert outcome.state == "SATISFIED" and restarted.verify(SCOPE, CID)[0]["ok"]
        return
    assert outcome.state == "HALTED"
    assert restarted.rollback(run_id).state == "ROLLED_BACK"
    assert _state(restarted)["attestation"] is None


def test_component_kinds_are_seat_privilege_in_their_own_class(healthy):
    _repo_, eng = healthy
    for kind in (KIND, "credential-broker.attest-retire"):
        entry = eng.backend.op_registry.get(kind)
        assert (entry.capability_class, entry.privilege, entry.read_only) == ("credential-broker.attest", "seat", False)


def test_real_repo_pins_are_current():
    result = repin(REPO_ROOT, REPO_ROOT / "registry" / "systems.yaml", CID)
    assert result.drift == []
