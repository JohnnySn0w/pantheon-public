"""T-5081 (germination GC): the pantheon-config component adopts tools/repo_reconciler.py
through the GB seam with an adapter only.

Every test runs on tmp_path stand-ins: a temporary checkout holding copies of the
three component files, a fixture ``checkouts/global.yaml`` and a registry, over a
simulated host. Only ``test_real_registry_row_*`` reads the real repo (read-only).
``_seed`` is a TEST HELPER: it writes simulated remote/checkout state into the
simulated host; no lifecycle verb does that.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from tools.germinate.cli import main
from tools.germinate.engine.backend import KILL_POINTS, SimulatedKill
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.run_state import RunStore
from tools.germinate.engine.tests import helpers as H
from tools.germinate.planner_sandbox.admission import coordinator
from tools.germinate.registry.contract_digest import compute_contract_digest

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPONENTS = REPO_ROOT / "tools" / "germinate" / "components"
FILES = ("pantheon-config.yaml", "pantheon-config.py", "pantheon-config_handlers.py")
CID = "pantheon-config"
CK = "pantheon-config-test"
A, B, C, D = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
SIM_URL = "sim://pantheon-config"


def _checkouts_yaml(policy: str = "ff-only") -> str:
    doc = {
        "schema_version": 1,
        "checkouts": [
            {
                "checkout_id": CK,
                "logical_repo": "pantheon-config",
                "remote_ref": "manifest",
                "target_account": "role:engineering-orchestrator-2",
                "target_path": "/nonexistent/sim/pantheon-config",
                "mode": {"tracking": "main"},
                "update_policy": policy,
                "health_contract": [],
            },
            {  # another logical repo: the component never plans it
                "checkout_id": "other-repo",
                "logical_repo": "pantheon-pwa",
                "remote_ref": "manifest",
                "target_account": "role:x",
                "target_path": "/nonexistent/sim/other",
                "mode": {"tracking": "main"},
                "update_policy": "ff-only",
                "health_contract": [],
            },
        ],
    }
    return yaml.safe_dump(doc, sort_keys=False)


def _repo(tmp_path: Path, policy: str = "ff-only") -> Path:
    repo = tmp_path / "repo"
    (repo / "tools" / "germinate" / "components").mkdir(parents=True)
    for name in FILES:
        shutil.copy(COMPONENTS / name, repo / "tools" / "germinate" / "components" / name)
    (repo / "checkouts").mkdir()
    (repo / "checkouts" / "global.yaml").write_text(_checkouts_yaml(policy), encoding="utf-8")
    digest = compute_contract_digest(repo / "tools" / "germinate" / "components" / "pantheon-config.yaml")
    H.make_checkout(repo, [H.row(CID, digest)])
    return repo


def _state(head=A, tip=B, *, present=True, dirty=False, origin=SIM_URL, installed=None, health=None, reachable=True, local_parents=None, url=SIM_URL):
    checkouts = {}
    if present:
        checkouts[CK] = {"head": head, "origin": origin, "dirty": dirty, "installed_sha": installed, "local_parents": local_parents or {}}
    return {
        "remote": {"url": url, "reachable": reachable, "refs": {} if tip is None else {"main": tip}, "parents": {A: None, B: A, C: B}},
        "checkouts": checkouts,
        "health": health or {},
    }


def _seed(backend, state) -> None:
    """TEST HELPER: write simulated pantheon-config state into the simulated host."""

    backend.handler("repo.ff-advance").load_state(state)
    backend.save()


def _ck(backend):
    return backend.handler("repo.ff-advance").dump_state()["checkouts"].get(CK)


@pytest.fixture
def setup(tmp_path):
    repo = _repo(tmp_path)
    return repo, H.engine(tmp_path, repo)


# --- planning: the reconciler's own reconcile_plan, under the planner guard ---


def test_update_plans_one_pinned_ff_advance(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state())
    [node] = eng.plan("update", "system", CID, source_revs=(A, B)).nodes
    [op] = node.operations
    assert (op.kind, op.target, op.privilege, op.crash_contract) == ("repo.ff-advance", CK, "seat", "write-ahead-inverse-durable")
    assert op.precondition["head"] == A and op.desired["head"] == B and op.inverse == {"head": A}
    assert op.provenance["planner"] == "tools.repo_reconciler.RepoReconciler.reconcile_plan"
    assert (op.provenance["reconciler_state"], op.provenance["reconciler_action"]) == ("behind", "ff-advance")
    assert op.owner == "role:engineering-orchestrator-2" and "other-repo" not in op.operation_id


REFUSING = {
    "dirty": (dict(dirty=True), "uncommitted"),
    "diverged": (dict(head=D, local_parents={D: A}), "ahead/diverged"),
    "foreign": (dict(origin="sim://someone-else"), "non-managed"),
    "missing-ref": (dict(tip=None), "absent on remote"),
    "credential": (dict(reachable=False), "unreachable"),
    "unresolved": (dict(url=""), "did not resolve"),
    "health": (dict(head=B, health={B: False}), "health probe failed"),
}


@pytest.mark.parametrize("name", sorted(REFUSING))
def test_reconciler_refusals_are_plan_refusals(setup, name):
    _repo_, eng = setup
    overrides, detail = REFUSING[name]
    _seed(eng.backend, _state(**overrides))
    for leg, revs in (("update", (A, B)), ("install", None), ("repair", None)):
        with pytest.raises(EngineRefusal, match="plan-refused: .*repo_reconciler refuses: .*" + detail.replace("/", ".")):
            eng.plan(leg, "system", CID, source_revs=revs)


def test_current_checkout_plans_nothing(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(head=B))
    assert eng.plan("update", "system", CID, source_revs=(B, B)).nodes[0].operations == ()
    assert eng.plan("install", "system", CID).nodes[0].operations == ()


@pytest.mark.parametrize(
    "revs, match",
    [((C, B), "--from-rev .* is not the observed HEAD"), ((A, C), "--to-rev .* is not the observed desired target"), (None, "needs --from-rev")],
)
def test_update_revs_are_bound_to_the_observation(setup, revs, match):
    _repo_, eng = setup
    _seed(eng.backend, _state())
    with pytest.raises(EngineRefusal, match=match):
        eng.plan("update", "system", CID, source_revs=revs)


@pytest.mark.parametrize("policy", ["never", "availability-only"])
def test_non_advancing_policies_plan_no_advance(tmp_path, policy):
    eng = H.engine(tmp_path, _repo(tmp_path, policy))
    _seed(eng.backend, _state())
    assert eng.plan("update", "system", CID, source_revs=(A, B)).nodes[0].operations == ()


def test_absent_checkout_install_clones_and_update_refuses(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(present=False))
    with pytest.raises(EngineRefusal, match="never clones"):
        eng.plan("update", "system", CID, source_revs=(A, B))
    plan = eng.plan("install", "system", CID)
    [op] = plan.nodes[0].operations
    assert (op.kind, op.desired["head"]) == ("repo.clone", B)
    [outcome] = eng.apply("install", "system", CID, plan_digest=plan.digest)
    assert outcome.state == "SATISFIED"
    assert _ck(eng.backend)["installed_sha"] == B


def test_repair_converges_to_the_observed_target(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(head=A, tip=C))
    plan = eng.plan("repair", "system", CID)
    [op] = plan.nodes[0].operations
    assert (op.precondition["head"], op.desired["head"]) == (A, C)
    eng.apply("repair", "system", CID, plan_digest=plan.digest)
    assert _ck(eng.backend)["head"] == C


# --- apply / verify / rollback ---------------------------------------------


def test_apply_writes_receipts_and_verify_reuses_the_reconciler(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state())
    plan = eng.plan("update", "system", CID, source_revs=(A, B))
    [outcome] = eng.apply("update", "system", CID, plan_digest=plan.digest, source_revs=(A, B))
    assert outcome.state == "SATISFIED" and _ck(eng.backend)["head"] == B
    store = RunStore(eng.backend.run_state_root("system"))
    with store.open(store.find(outcome.run_id)) as handle:
        assert [r["op_id"] for r in handle.read_record()["receipts"]] == [f"ff-advance-{CK}"]
    [result] = eng.verify("system", CID)
    assert result["ok"] and "repo_reconciler verify: current" in result["checks"][0]["detail"]
    state = _state(head=B, dirty=True)
    _seed(eng.backend, state)
    [result] = eng.verify("system", CID)
    assert not result["ok"] and "dirty" in result["checks"][0]["detail"]


def test_health_failure_halts_then_rollback_restores_head(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(health={B: False}))
    plan = eng.plan("update", "system", CID, source_revs=(A, B))
    with pytest.raises(EngineRefusal, match="HALTED .*health-fail"):
        eng.apply("update", "system", CID, plan_digest=plan.digest, source_revs=(A, B))
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    assert _ck(eng.backend)["head"] == A


def test_rollback_refuses_a_moved_head(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(health={B: False}))
    plan = eng.plan("update", "system", CID, source_revs=(A, B))
    with pytest.raises(EngineRefusal, match="HALTED"):
        eng.apply("update", "system", CID, plan_digest=plan.digest, source_revs=(A, B))
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    _seed(eng.backend, _state(head=C, health={B: False}))  # someone moved HEAD past our advance
    with pytest.raises(EngineRefusal, match="rollback-incomplete"):
        eng.rollback(run_id)
    assert _ck(eng.backend)["head"] == C  # never rewound what it does not own


def test_remove_reclaims_only_engine_installed_pristine_checkouts(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state())  # hand-cloned: not engine-installed
    assert eng.plan("remove", "system", CID).nodes[0].operations == ()
    _seed(eng.backend, _state(head=B, installed=B, dirty=True))
    with pytest.raises(EngineRefusal, match="dirty; refusing to reap"):
        eng.plan("remove", "system", CID)
    _seed(eng.backend, _state(head=C, installed=B))
    with pytest.raises(EngineRefusal, match="refusing to reap"):
        eng.plan("remove", "system", CID)
    _seed(eng.backend, _state(head=B, installed=B))
    plan = eng.plan("remove", "system", CID)
    [op] = plan.nodes[0].operations
    assert op.kind == "repo.reclaim"
    eng.apply("remove", "system", CID, plan_digest=plan.digest)
    assert _ck(eng.backend) is None


# --- kill injection (ORG-080 D-X) through the GB harness ----------------------


def _scenario(eng, kind):
    if kind == "repo.ff-advance":
        _seed(eng.backend, _state())
        return "update", (A, B)
    if kind == "repo.clone":
        _seed(eng.backend, _state(present=False))
        return "install", None
    _seed(eng.backend, _state(head=B, installed=B))
    return "remove", None


@pytest.mark.parametrize("kind", ["repo.ff-advance", "repo.clone", "repo.reclaim"])
@pytest.mark.parametrize("point", KILL_POINTS)
def test_kill_then_reconcile_then_rollback_restores_preimage(tmp_path, kind, point):
    repo = _repo(tmp_path)
    eng = H.engine(tmp_path, repo)
    leg, revs = _scenario(eng, kind)
    preimage = _ck(eng.backend)
    plan = eng.plan(leg, "system", CID, source_revs=revs)
    [op] = plan.nodes[0].operations
    assert op.kind == kind
    eng.backend.faults.kill = (op.operation_id, point)
    with pytest.raises(SimulatedKill):
        eng.apply(leg, "system", CID, plan_digest=plan.digest, source_revs=revs)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    restarted = H.engine(tmp_path, repo)  # a fresh process over the same simulated host
    outcome = restarted.reconcile_run(run_id, executor_dead=True)
    if point in ("after-manifest", "after-record"):
        assert outcome.state == "SATISFIED"  # the receipt is committed: the op stands
        assert restarted.verify("system", CID)[0]["ok"]
        return
    assert outcome.state == "HALTED"
    assert restarted.rollback(run_id).state == "ROLLED_BACK"
    assert _ck(restarted.backend) == preimage


# --- registration and the registry ------------------------------------------


def test_component_kinds_are_seat_privilege_in_their_own_class(setup):
    _repo_, eng = setup
    for kind in ("repo.clone", "repo.ff-advance", "repo.reclaim"):
        entry = eng.backend.op_registry.get(kind)
        assert (entry.capability_class, entry.privilege, entry.read_only, entry.substrate) == ("repo.checkout", "seat", False, False)
        assert eng.backend.handles(kind)


def test_real_registry_row_validates_with_the_pinned_digest():
    from tools.germinate.registry.validate import errors, validate_repo

    assert errors(validate_repo(REPO_ROOT)) == []
    doc = yaml.safe_load((REPO_ROOT / "registry" / "systems.yaml").read_text(encoding="utf-8"))
    [row] = [r for r in doc["systems"] if r.get("id") == CID]
    assert row["contract_adoption"] == "partial"
    assert row["contract"]["digest"] == compute_contract_digest(COMPONENTS / "pantheon-config.yaml")


def test_real_registry_row_fails_without_the_component_files(tmp_path):
    """Red-without-the-change: the same row against a checkout lacking the module refuses."""

    from tools.germinate.registry.validate import check_contracts, errors

    doc = yaml.safe_load((REPO_ROOT / "registry" / "systems.yaml").read_text(encoding="utf-8"))
    msgs = "\n".join(str(f) for f in errors(check_contracts(doc, tmp_path)))
    assert "pantheon-config.contract" in msgs


# --- end-to-end CLI ----------------------------------------------------------


def _result(out: str):
    return [json.loads(line)["result"] for line in out.splitlines() if line.startswith('{"result"')][-1]


def test_cli_smoke_update_apply_verify(tmp_path, capsys):
    repo = _repo(tmp_path)
    for name, health, final in (("ok", {}, "SATISFIED"), ("fault", {B: False}, "HALTED")):
        sim_root = tmp_path / f"e2e-{name}"
        common = ["--repo-root", str(repo), "--simulated-host-root", str(sim_root)]
        update = ["--system", CID, "--from-rev", A, "--to-rev", B, *common]
        assert main(["sim", "init", "--simulated-host-root", str(sim_root)]) == 0
        _seed(H.Engine.from_checkout(sim_root, repo_root=repo).backend, _state(health=health))  # TEST HELPER seed
        capsys.readouterr()
        assert main(["plan", "update", *update]) == 0
        digest = _result(capsys.readouterr().out)["plan_digest"]
        if final == "SATISFIED":
            assert main(["apply", "update", "--plan-digest", digest, *update]) == 0
            [outcome] = _result(capsys.readouterr().out)
            assert outcome["state"] == "SATISFIED"
            assert main(["verify", "--system", CID, *common]) == 0
            capsys.readouterr()
            assert main(["rollback", "--run", outcome["run_id"], *common]) == 1  # SATISFIED is absorbing
            assert "rollback-refused" in capsys.readouterr().err
        else:
            assert main(["apply", "update", "--plan-digest", digest, *update]) == 1
            run_id = capsys.readouterr().err.split("run ")[1].split(" ")[0]
            assert main(["rollback", "--run", run_id, *common]) == 0
            assert _result(capsys.readouterr().out)["state"] == "ROLLED_BACK"
            assert main(["verify", "--system", CID, *common]) == 0
