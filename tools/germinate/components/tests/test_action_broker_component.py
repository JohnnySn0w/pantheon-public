"""T-5948: the action-broker component adopts the ORG-080 contract by observe-and-attest.

The install leg WRAPS the legacy T-2920 install (tools/host_broker_install_apply.py)
without rewriting it: it attests an existing healthy legacy install (receipt +
verify result) and refuses otherwise; the engine never installs the broker.

Every test runs on tmp_path stand-ins: a temporary checkout holding copies of the
component files and a fixture registry, over a simulated host. Only the
``test_real_*`` tests read the real repo (read-only). ``_seed`` is a TEST HELPER: it
writes simulated legacy evidence into the simulated host; no lifecycle verb does that.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
from pathlib import Path
from unittest import mock

import pytest
import yaml

from tools import host_broker_install_apply as A
from tools.germinate.cli import main
from tools.germinate.engine.backend import KILL_POINTS, SimulatedKill
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.run_state import RunStore
from tools.germinate.engine.tests import helpers as H
from tools.germinate.planner_sandbox.admission import coordinator
from tools.germinate.registry.contract_digest import compute_contract_digest

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPONENTS = REPO_ROOT / "tools" / "germinate" / "components"
FILES = ("action-broker.yaml", "action-broker.py", "action-broker_handlers.py")
CID = "action-broker"
KIND = "action-broker.adopt-attest"
TARGET = "action-broker:legacy-install"
GATE = "adopt-legacy-install"
PLAN_SHA = "p" * 64


def _receipt(**overrides):
    """A legacy-shaped receipt (pantheon-host-broker-install-receipt/v1), re-signed."""

    doc = {
        "schema": A.RECEIPT_SCHEMA,
        "plan_sha256": PLAN_SHA,
        "framework_rev": "rev-fixture",
        "mode": A.FIXTURE_MODE,
        "status": "complete",
        "noop": False,
        "failed_entry": None,
        "entries": [{"id": "dir:/etc/pantheon", "action": "created"}],
    }
    doc.update(overrides)
    doc["receipt_sha256"] = A.receipt_digest(doc)
    return doc


def _verify(rc=0, plan_sha256=PLAN_SHA, failed=()):
    report = [{"seq": 0, "id": "dir:/etc/pantheon", "kind": "dir", "ok": True},
              {"seq": 1, "id": "config:/etc/pantheon/host-broker.json", "kind": "config", "ok": True}]
    for row in report:
        if row["id"] in failed:
            row["ok"] = False
    return {"rc": rc, "plan_sha256": plan_sha256, "report": report}


def _repo(tmp_path: Path, extra_rows=()) -> Path:
    repo = tmp_path / "repo"
    (repo / "tools" / "germinate" / "components").mkdir(parents=True, exist_ok=True)
    for name in FILES:
        shutil.copy(COMPONENTS / name, repo / "tools" / "germinate" / "components" / name)
    digest = compute_contract_digest(repo / "tools" / "germinate" / "components" / "action-broker.yaml")
    H.make_checkout(repo, [H.row(CID, digest, steward="codex"), *extra_rows])
    return repo


def _seed(backend, receipt=None, verify=None, attestations=None) -> None:
    """TEST HELPER: write simulated legacy install evidence into the simulated host."""

    backend.handler(KIND).load_state(
        {"install": {"receipt": receipt, "verify": verify}, "attestations": attestations or {}}
    )
    backend.save()


def _attestation(backend):
    return backend.handler(KIND).dump_state()["attestations"].get(TARGET)


@pytest.fixture
def setup(tmp_path):
    repo = _repo(tmp_path)
    return repo, H.engine(tmp_path, repo)


# --- contract, registry, registration -------------------------------------------


def test_real_contract_loads_and_registry_row_validates():
    from tools.germinate.registry.validate import errors, validate_repo

    assert errors(validate_repo(REPO_ROOT)) == []
    doc = yaml.safe_load((REPO_ROOT / "registry" / "systems.yaml").read_text(encoding="utf-8"))
    [row] = [r for r in doc["systems"] if r.get("id") == CID]
    assert row["contract"]["digest"] == compute_contract_digest(COMPONENTS / "action-broker.yaml")
    assert row["contract_adoption"] == "not_adopted"  # install only, simulated handler only (D6)
    body = yaml.safe_load((COMPONENTS / "action-broker.yaml").read_text(encoding="utf-8"))["component"]
    assert body["steward"] == row["steward"] == "codex"
    assert body["depends_on"] == [] and body["scopes"] == ["system"]  # D3
    assert body["privilege"] == {"seat": [KIND]}  # D4: no broker_runtime
    assert set(body["legs"]) == {"install"}
    assert body["gates"] == [{"id": GATE, "irreversible": False, "note": body["gates"][0]["note"], "approval": {"class": "operator"}}]


def test_real_registry_row_fails_without_the_component_files(tmp_path):
    """Red-without-the-change: the same row against a checkout lacking the module refuses."""

    from tools.germinate.registry.validate import check_contracts, errors

    doc = yaml.safe_load((REPO_ROOT / "registry" / "systems.yaml").read_text(encoding="utf-8"))
    msgs = "\n".join(str(f) for f in errors(check_contracts(doc, tmp_path)))
    assert "action-broker.contract" in msgs


def test_component_kind_is_seat_in_its_own_class(setup):
    _repo_, eng = setup
    entry = eng.backend.op_registry.get(KIND)
    assert (entry.capability_class, entry.privilege, entry.read_only, entry.substrate) == ("action-broker.adoption", "seat", False, False)
    assert eng.backend.handles(KIND)


def test_repin_check_is_clean(capsys):
    assert main(["registry", "repin", CID, "--check", "--repo-root", str(REPO_ROOT)]) == 0


# --- planning: refuse unless a healthy legacy install is observed -----------------


def test_install_refuses_when_the_broker_is_not_installed(setup):
    _repo_, eng = setup
    with pytest.raises(EngineRefusal) as info:
        eng.plan("install", "system", CID)
    assert info.value.code == "plan-refused"
    assert "not installed" in str(info.value) and "T-2920" in str(info.value) and "T-2925" in str(info.value)
    with pytest.raises(EngineRefusal):
        eng.apply("install", "system", CID, plan_digest="sha256:" + "0" * 64, accept_gates=frozenset({GATE}))
    assert list(RunStore(eng.backend.run_state_root("system")).all_tuples()) == []  # nothing admitted
    assert _attestation(eng.backend) is None


def _tampered():
    doc = _receipt()
    doc["framework_rev"] = "rev-other"  # body changed, signature not
    return doc


@pytest.mark.parametrize(
    ("receipt", "verify", "match"),
    [
        (_tampered(), _verify(), "receipt_digest"),
        (_receipt(mode=A.REHEARSAL_MODE), _verify(), "rehearsal-nspawn"),
        (_receipt(mode=A.LIVE_MODE), _verify(), "cannot serve the fixture path"),
        (_receipt(status="partial"), _verify(), "status 'partial'"),
        (_receipt(schema="other/v1"), _verify(), "schema"),
        (_receipt(), _verify(rc=1, failed=("config:/etc/pantheon/host-broker.json",)), r"failed entries \['config:/etc/pantheon/host-broker.json'\]"),
        (_receipt(), _verify(rc=1), "verify rc 1"),
        (_receipt(), _verify(failed=("dir:/etc/pantheon",)), "failed entries"),
        (_receipt(), _verify(plan_sha256="q" * 64), "verify ran over plan"),
        (_receipt(), None, "no legacy verify result"),
    ],
    ids=["digest-tampered", "rehearsal-nspawn", "live-on-sim-tier", "status-partial", "schema", "verify-rc1-failed",
         "verify-rc1", "verify-rc0-but-failed-row", "plan-sha-mismatch", "no-verify"],
)
def test_unhealthy_legacy_evidence_refuses(setup, receipt, verify, match):
    _repo_, eng = setup
    _seed(eng.backend, receipt, verify)
    with pytest.raises(EngineRefusal, match=match) as info:
        eng.plan("install", "system", CID)
    assert info.value.code == "plan-refused" and "T-2920" in str(info.value)


def test_attests_then_reruns_empty(setup):
    _repo_, eng = setup
    receipt, verify = _receipt(), _verify()
    _seed(eng.backend, receipt, verify)
    plan = eng.plan("install", "system", CID)
    [op] = plan.nodes[0].operations
    assert (op.kind, op.target, op.privilege, op.gate, op.layer) == (KIND, TARGET, "seat", GATE, 0)
    assert op.crash_contract == "write-ahead-inverse-durable" and op.inverse == {"attestation": "absent"}
    assert op.desired["receipt_sha256"] == receipt["receipt_sha256"]
    assert op.desired["plan_sha256"] == PLAN_SHA and op.desired["mode"] == "fixture" and op.desired["status"] == "complete"
    assert "tools.host_broker_install_apply.verify" in op.provenance["evidence"]
    assert "T-2920" in op.provenance["live_install"]
    [outcome] = eng.apply("install", "system", CID, plan_digest=plan.digest, accept_gates=frozenset({GATE}))
    assert outcome.state == "SATISFIED"
    assert _attestation(eng.backend) == dict(op.desired)
    store = RunStore(eng.backend.run_state_root("system"))
    with store.open(store.find(outcome.run_id)) as handle:
        record = handle.read_record()
    assert [r["op_id"] for r in record["receipts"]] == [op.operation_id]
    assert record["operations"][0]["postcondition_check"] == dict(op.desired)  # the evidence is in the run record
    assert eng.plan("install", "system", CID).nodes[0].operations == ()  # verify, not mutate


def test_the_plan_digest_binds_the_evidence(setup):
    _repo_, eng = setup
    _seed(eng.backend, _receipt(), _verify())
    first = eng.plan("install", "system", CID).digest
    _seed(eng.backend, _receipt(framework_rev="rev-2"), _verify())
    second = eng.plan("install", "system", CID)
    assert second.digest != first
    with pytest.raises(EngineRefusal, match="plan-digest-mismatch"):
        eng.apply("install", "system", CID, plan_digest=first, accept_gates=frozenset({GATE}))


def _apply_attest(eng):
    plan = eng.plan("install", "system", CID)
    [op] = plan.nodes[0].operations
    [outcome] = eng.apply("install", "system", CID, plan_digest=plan.digest, accept_gates=frozenset({GATE}))
    assert outcome.state == "SATISFIED"
    return op


def test_drifted_healthy_evidence_re_attests_and_verify_reports_drift(setup):
    """Drift (a)+(b): new healthy evidence replans ONE attest op with the new digests;
    the engine's verify compares current evidence against the attested evidence."""

    _repo_, eng = setup
    _seed(eng.backend, _receipt(), _verify())
    first = _apply_attest(eng)
    [result] = eng.verify("system", CID)
    assert result["ok"], result
    prior = _attestation(eng.backend)
    new_verify = _verify()
    new_verify["report"].append({"seq": 2, "id": "launcher:/usr/local/libexec/pantheon-host", "kind": "launcher", "ok": True})
    _seed(eng.backend, _receipt(framework_rev="rev-2", plan_sha256="r" * 64), dict(new_verify, plan_sha256="r" * 64),
          attestations={TARGET: prior})
    [result] = eng.verify("system", CID)
    assert not result["ok"] and "drift" in result["checks"][0]["detail"]
    for name in ("framework_rev", "plan_sha256", "receipt_sha256", "verify_report_sha256"):
        assert name in result["checks"][0]["detail"]
    [op] = eng.plan("install", "system", CID).nodes[0].operations
    assert op.kind == KIND and op.precondition["attestation_sha256"] != "absent" and "attestation_sha256" in op.inverse
    for name in ("framework_rev", "plan_sha256", "receipt_sha256", "verify_report_sha256"):
        assert op.desired[name] != first.desired[name]
    assert op.desired["plan_sha256"] == "r" * 64 and op.desired["framework_rev"] == "rev-2"
    _apply_attest(eng)
    [result] = eng.verify("system", CID)
    assert result["ok"], result
    assert eng.plan("install", "system", CID).nodes[0].operations == ()


def test_empty_verify_report_refuses(setup):
    _repo_, eng = setup
    verify = _verify()
    verify["report"] = []
    _seed(eng.backend, _receipt(), verify)
    with pytest.raises(EngineRefusal, match="empty") as info:
        eng.plan("install", "system", CID)
    assert info.value.code == "plan-refused"


def test_non_dict_verify_row_refuses_naming_the_row(setup):
    _repo_, eng = setup
    verify = _verify()
    verify["report"].append("garbage")
    _seed(eng.backend, _receipt(), verify)
    with pytest.raises(EngineRefusal) as info:  # no AttributeError escapes
        eng.plan("install", "system", CID)
    assert info.value.code == "plan-refused" and "garbage" in str(info.value)


def test_attested_then_unhealthy_evidence_refuses_never_re_attests(setup):
    _repo_, eng = setup
    _seed(eng.backend, _receipt(), _verify())
    _apply_attest(eng)
    prior = _attestation(eng.backend)
    _seed(eng.backend, _receipt(), _verify(rc=1), attestations={TARGET: prior})
    with pytest.raises(EngineRefusal, match="verify rc 1") as info:
        eng.plan("install", "system", CID)
    assert info.value.code == "plan-refused"
    [result] = eng.verify("system", CID)
    assert not result["ok"] and "no longer healthy" in result["checks"][0]["detail"]
    assert _attestation(eng.backend) == prior  # the old attestation is left, flagged by verify


def test_apply_refuses_without_the_gate(setup):
    _repo_, eng = setup
    _seed(eng.backend, _receipt(), _verify())
    plan = eng.plan("install", "system", CID)
    with pytest.raises(EngineRefusal) as info:
        eng.apply("install", "system", CID, plan_digest=plan.digest)
    assert info.value.code == "gate-unanswered"
    assert _attestation(eng.backend) is None


def test_evidence_drift_between_plan_and_apply_halts_before_attesting(setup):
    """The handler re-derives the evidence at preflight (a receipt that turned unhealthy)."""

    _repo_, eng = setup
    _seed(eng.backend, _receipt(), _verify())
    plan = eng.plan("install", "system", CID)
    _seed(eng.backend, _receipt(), _verify(rc=1))
    with pytest.raises(EngineRefusal):
        eng.apply("install", "system", CID, plan_digest=plan.digest, accept_gates=frozenset({GATE}))
    assert _attestation(eng.backend) is None


@pytest.mark.parametrize("point", KILL_POINTS)
def test_kill_then_reconcile_then_rollback_removes_the_attestation_only(tmp_path, point):
    repo = _repo(tmp_path)
    eng = H.engine(tmp_path, repo)
    receipt, verify = _receipt(), _verify()
    _seed(eng.backend, receipt, verify)
    plan = eng.plan("install", "system", CID)
    [op] = plan.nodes[0].operations
    eng.backend.faults.kill = (op.operation_id, point)
    with pytest.raises(SimulatedKill):
        eng.apply("install", "system", CID, plan_digest=plan.digest, accept_gates=frozenset({GATE}))
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    restarted = H.engine(tmp_path, repo)  # a fresh process over the same simulated host
    outcome = restarted.reconcile_run(run_id, executor_dead=True)
    state = restarted.backend.handler(KIND).dump_state()
    assert state["install"] == {"receipt": receipt, "verify": verify}  # the legacy evidence is never touched
    if point in ("after-manifest", "after-record"):
        assert outcome.state == "SATISFIED"  # the receipt is committed: the attestation stands
        assert state["attestations"][TARGET] == dict(op.desired)
        return
    assert outcome.state == "HALTED"
    assert restarted.rollback(run_id).state == "ROLLED_BACK"
    state = restarted.backend.handler(KIND).dump_state()
    assert state["attestations"] == {}
    assert state["install"] == {"receipt": receipt, "verify": verify}


# --- the dependency it unblocks: harness-manager (T-5947) ---------------------------


HM_DEPS_STUBBED = ("host-substrate", "mailbox", "credential-broker")


def _hm_repo(tmp_path: Path) -> Path:
    """Real harness-manager + real action-broker; the other three dependencies are
    system-scope stubs (T-5092 / host-substrate's own install record are not here)."""

    from tools.germinate.components.tests import test_harness_manager_component as HM

    repo = tmp_path / "repo"
    directory = repo / "tools" / "germinate" / "components"
    directory.mkdir(parents=True)
    for name in (*HM.FILES, *FILES):
        shutil.copy(COMPONENTS / name, directory / name)
    rows = [
        H.row(HM.CID, compute_contract_digest(directory / "harness-manager.yaml")),
        H.row(CID, compute_contract_digest(directory / "action-broker.yaml"), steward="codex"),
    ]
    for dep in HM_DEPS_STUBBED:
        _p, digest = H.write_contract(repo, dep, ops={"install": []}, scopes=["system"])
        rows.append(H.row(dep, digest))
    H.make_checkout(repo, rows)
    return repo


def _hm_engine(tmp_path, repo, stubs=HM_DEPS_STUBBED):
    from tools.germinate.components.tests import test_harness_manager_component as HM

    eng = H.engine(tmp_path, repo)
    HM._seed(eng.backend, HM._seat())
    for dep in stubs:
        plan = eng.plan("install", "system", dep)
        [outcome] = eng.apply("install", "system", dep, plan_digest=plan.digest)
        assert outcome.state == "SATISFIED"
    return eng, HM


def _attest(eng):
    _seed(eng.backend, _receipt(), _verify())
    plan = eng.plan("install", "system", CID)
    [outcome] = eng.apply("install", "system", CID, plan_digest=plan.digest, accept_gates=frozenset({GATE}))
    assert outcome.state == "SATISFIED"


def test_harness_manager_no_longer_depends_on_action_broker(tmp_path):
    """T-6167 PR-3 (ORG-052 section 8): the broker serves running seats, not seat setup, so
    harness-manager plans with action-broker never attested (it was the unblocked dependency)."""

    repo = _hm_repo(tmp_path)
    eng, HM = _hm_engine(tmp_path, repo)
    assert not eng._satisfied_install(CID, "system")
    [node] = HM._configure(eng).nodes
    assert node.component == HM.CID and node.operations
    try:  # install is past the dependency gate too (its own inputs aside)
        eng.plan("install", HM.SCOPE, HM.CID, desired={"source_revision": "a" * 40})
    except EngineRefusal as exc:
        assert exc.code != "dependency-not-installed", exc


def test_control_a_missing_stub_dependency_still_refuses(tmp_path):
    repo = _hm_repo(tmp_path)
    eng, HM = _hm_engine(tmp_path, repo, stubs=("host-substrate", "mailbox"))
    _attest(eng)
    with pytest.raises(EngineRefusal) as info:
        HM._configure(eng)
    assert info.value.code == "dependency-not-installed" and "depends on credential-broker" in str(info.value)


# --- integration: the real legacy shapes (a fixture-mode T-2920 apply + verify) -----


def test_attests_a_real_fixture_mode_legacy_receipt_and_verify(tmp_path):
    """RECOMMENDED: drives tools.host_broker_install_apply.apply/verify on a tmp fixture
    root, seeds their real outputs into the sim handler, and plans the attestation."""

    from tools import host_broker_install as LH
    from tools import test_host_broker_install as LT
    from tools import test_host_broker_install_apply as LA

    with mock.patch.object(LH, "_literal", LA._literal_allowlist_landed), \
            mock.patch.object(A, "lock_pinned_hashes", LA._pinned_plus_fixture_wheel):
        plan_doc, _staging = LA.build_plan()
        plan_raw = LH.plan_bytes(plan_doc)
        (tmp_path / "legacy").mkdir()
        root = LA.make_root(tmp_path / "legacy")
        outcome = A.apply(plan_raw=plan_raw, manifest_raw=LT.raw(LA.manifest_doc()), registry_raw=LT.raw(LA.registry_doc()),
                          tracked_raw=None, inputs_raw=json.dumps(LA.inputs_doc()).encode(), root=root, receipt=LA.RECEIPT,
                          execute=True, runner_factory=LA.runner_factory(), closure=LT.closure(),
                          now=lambda: "2026-11-02T12:00:00Z")
        assert outcome.rc == 0, outcome.receipt["failed_entry"]
        receipt_raw = (root / LA.RECEIPT.lstrip("/")).read_bytes()
        rc, report = A.verify(plan_raw=plan_raw, root=root, receipt_raw=receipt_raw, runner_factory=LA.runner_factory())
    assert rc == 0, [r for r in report if not r["ok"]]
    receipt = json.loads(receipt_raw)
    repo = _repo(tmp_path)
    eng = H.engine(tmp_path, repo)
    verify = {"rc": rc, "plan_sha256": json.loads(plan_raw)["plan_sha256"], "report": report}
    _seed(eng.backend, receipt, verify)
    [op] = eng.plan("install", "system", CID).nodes[0].operations
    assert op.desired["receipt_sha256"] == receipt["receipt_sha256"] == A.receipt_digest(receipt)
    assert op.desired["framework_rev"] == plan_doc["framework"]["rev"] and op.desired["mode"] == "fixture"
    assert op.desired["verify_report_sha256"] == hashlib.sha256(A.canonical(report).encode("ascii")).hexdigest()
    # one drifted entry in the real report refuses, naming it
    drifted = copy.deepcopy(report)
    drifted[0]["ok"] = False
    _seed(eng.backend, receipt, {"rc": 1, "plan_sha256": verify["plan_sha256"], "report": drifted})
    with pytest.raises(EngineRefusal, match=re.escape(drifted[0]["id"])):
        eng.plan("install", "system", CID)
