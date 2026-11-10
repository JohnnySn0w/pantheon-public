"""T-5081 (germination GC): the pantheon-config component adopts tools/repo_reconciler.py
through the GB seam with an adapter only.

Every test runs on tmp_path stand-ins: a temporary checkout holding copies of the
three component files, a fixture ``checkouts/global.yaml`` and a registry, over a
simulated host. Only ``test_real_registry_row_*`` reads the real repo (read-only).
``_seed`` is a TEST HELPER: it writes simulated remote/checkout state into the
simulated host; no lifecycle verb does that.
"""

from __future__ import annotations

import hashlib
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
FILES = ("pantheon-config.yaml", "pantheon-config.py", "pantheon-config_handlers.py", "pantheon-config.invariants.yaml")
CID = "pantheon-config"
CK = "pantheon-config-test"
A, B, C, D, E = ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40)
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


MODES_OK = {"pre-commit": "exec", "commit-msg": "exec", "pre-push": "exec"}
MODES_OFF = {"pre-commit": "noexec", "commit-msg": "noexec", "pre-push": "noexec"}


def _hooks_entry(path=".githooks", modes=None, engine=False, identity=None):
    return {"hooks_path": path, "modes": dict(MODES_OK if modes is None else modes), "engine": engine, "identity": identity}


def _seed_local(backend, hooks=None, attestations=None, manifests=None, manifest_attestations=None, fs=None) -> None:
    """TEST HELPER: write simulated hook/attestation/manifest state into the local handler.

    ``manifests`` is only ever seeded here: the engine never writes instance.yaml."""

    state = {
        "hooks": hooks or {},
        "attestations": attestations or {},
        "manifests": manifests or {},
        "manifest_attestations": manifest_attestations or {},
    }
    if fs is not None:
        state["fs"] = fs
    backend.handler("repo.hooks-activate").load_state(state)
    backend.save()


VALID_MANIFEST = "role_membership_projection:\n  source: membership.yaml\n  expected_revision: 1\n  expected_digest: abc\n"
OTHER_MANIFEST = VALID_MANIFEST + "other_key: changed\n"
INVALID_MANIFESTS = {
    "no-projection-block": "roles:\n  a: b\n",
    "not-a-mapping": "- just\n- a list\n",
    "unparseable": "role_membership_projection: {source: [unclosed\n",
}


def _dg(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _manifest(text=VALID_MANIFEST, identity=None):
    return {"text": text, "identity": identity}


def _m_attested(text=VALID_MANIFEST, identity=None):
    return {"digest": _dg(text), "identity": identity}


def _local(backend):
    return backend.handler("repo.hooks-activate").dump_state()


def _converged(backend, **kw) -> None:
    """TEST HELPER: seed the simulated repo (``_state(**kw)``) with healthy hooks and a valid manifest for the
    checkout, so the invariant list reads a converged fixture (both keyed to the same installed identity)."""

    _seed(backend, _state(**kw))
    _seed_local(backend, hooks={CK: _hooks_entry(identity=kw.get("installed"))}, manifests={CK: _manifest(identity=kw.get("installed"))})


def _add_manifest(eng) -> None:
    """TEST HELPER: seed a valid instance.yaml for the checkout (the engine never writes it), keyed to the
    checkout's installed identity, so a converged checkout also passes PC-5; an existing manifest stays."""

    ck = _ck(eng.backend)
    if ck is None:  # a removed checkout has nothing to manifest
        return
    handler = eng.backend.handler("repo.hooks-activate")
    state = handler.dump_state()
    state["manifests"].setdefault(CK, _manifest(identity=ck["installed_sha"]))
    handler.load_state(state)
    eng.backend.save()


def _attested(origin=SIM_URL, head=A, basis="observed-legacy", identity=None, modes=None):
    return {
        "checkout_id": CK, "origin": origin, "hooks_path": ".githooks", "hook_modes": dict(MODES_OK if modes is None else modes),
        "observed_head": head, "basis": basis, "identity": identity,
    }


def _op_of(plan, kind):
    return next(op for op in plan.nodes[0].operations if op.kind == kind)


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
    _seed(eng.backend, _state(head=B, installed=B))  # engine-owned: install plans nothing
    _seed_local(eng.backend, hooks={CK: _hooks_entry(engine=True, identity=B)})
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


def test_install_absent_checkout_plans_clone_then_hooks_activate(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(present=False))
    with pytest.raises(EngineRefusal, match="never clones"):
        eng.plan("update", "system", CID, source_revs=(A, B))
    plan = eng.plan("install", "system", CID)
    # the engine applies in list order and rolls back reversed: clone first, hooks undone first
    assert [op.kind for op in plan.nodes[0].operations] == ["repo.clone", "repo.hooks-activate"]
    clone, hooks = plan.nodes[0].operations
    assert clone.desired["head"] == B and (hooks.target, hooks.privilege) == (CK, "seat")
    assert hooks.desired["hooks_path"] == ".githooks" and hooks.crash_contract == "write-ahead-inverse-durable"
    [outcome] = eng.apply("install", "system", CID, plan_digest=plan.digest)
    assert outcome.state == "SATISFIED"
    assert _ck(eng.backend)["installed_sha"] == B
    entry = _local(eng.backend)["hooks"][CK]
    assert (entry["hooks_path"], entry["modes"], entry["engine"]) == (".githooks", MODES_OK, True)
    store = RunStore(eng.backend.run_state_root("system"))
    with store.open(store.find(outcome.run_id)) as handle:
        assert [r["op_id"] for r in handle.read_record()["receipts"]] == [f"clone-{CK}", f"hooks-activate-{CK}"]
    _add_manifest(eng)
    [result] = eng.verify("system", CID)
    assert result["ok"] and len(result["checks"]) == 2
    assert eng.plan("install", "system", CID).nodes[0].operations == ()  # converged: engine-owned, healthy


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
    _converged(eng.backend)
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


ALL_KINDS = [
    "repo.ff-advance", "repo.clone", "repo.reclaim", "repo.hooks-activate", "pantheon-config.adopt-attest",
    "pantheon-config.manifest-attest", "configure:repo.hooks-activate",
]


def _scenario(eng, kind):
    if kind == "configure:repo.hooks-activate":  # engine-installed checkout whose hooks were lost; manifest already attested
        _seed(eng.backend, _state(head=B, installed=B))
        _seed_local(eng.backend, manifests={CK: _manifest(identity=B)}, manifest_attestations={CK: _m_attested(identity=B)})
        return "configure", None
    if kind == "pantheon-config.manifest-attest":  # hooks healthy and engine-owned; manifest valid but unattested
        _seed(eng.backend, _state(head=B, installed=B))
        _seed_local(eng.backend, hooks={CK: _hooks_entry(engine=True, identity=B)}, manifests={CK: _manifest(identity=B)})
        return "configure", None
    if kind == "repo.ff-advance":
        _converged(eng.backend)
        return "update", (A, B)
    if kind in ("repo.clone", "repo.hooks-activate"):
        _seed(eng.backend, _state(present=False))
        return "install", None
    if kind == "pantheon-config.adopt-attest":
        _seed(eng.backend, _state())
        _seed_local(eng.backend, hooks={CK: _hooks_entry()})
        return "install", None
    _seed(eng.backend, _state(head=B, installed=B))
    return "remove", None


def _snapshot(backend):
    return _ck(backend), _local(backend)


@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize("point", KILL_POINTS)
def test_kill_then_reconcile_then_rollback_restores_preimage(tmp_path, kind, point):
    repo = _repo(tmp_path)
    eng = H.engine(tmp_path, repo)
    leg, revs = _scenario(eng, kind)
    preimage = _snapshot(eng.backend)
    plan = eng.plan(leg, "system", CID, source_revs=revs)
    op = _op_of(plan, kind.split(":")[-1])  # hooks-activate is killed AFTER the clone is receipted
    eng.backend.faults.kill = (op.operation_id, point)
    with pytest.raises(SimulatedKill):
        eng.apply(leg, "system", CID, plan_digest=plan.digest, source_revs=revs)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    restarted = H.engine(tmp_path, repo)  # a fresh process over the same simulated host
    outcome = restarted.reconcile_run(run_id, executor_dead=True)
    last = plan.nodes[0].operations[-1] is op
    if point in ("after-manifest", "after-record") and last:
        assert outcome.state == "SATISFIED"  # the receipt is committed: the op stands
        _add_manifest(restarted)
        assert restarted.verify("system", CID)[0]["ok"]
        return
    assert outcome.state == "HALTED"
    assert restarted.rollback(run_id).state == "ROLLED_BACK"
    assert _snapshot(restarted.backend) == preimage


# --- the install leg: hooks and adoption (simulated tier) ---------------------


def test_halt_at_hooks_rolls_back_hooks_then_clone(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(present=False))
    plan = eng.plan("install", "system", CID)
    hooks = _op_of(plan, "repo.hooks-activate")
    eng.backend.faults.kill = (hooks.operation_id, "after-execute")
    with pytest.raises(SimulatedKill):
        eng.apply("install", "system", CID, plan_digest=plan.digest)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    assert eng.reconcile_run(run_id, executor_dead=True).state == "HALTED"
    assert _ck(eng.backend) is not None and _local(eng.backend)["hooks"][CK]["engine"]
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    assert _ck(eng.backend) is None and CK not in _local(eng.backend)["hooks"]


def test_foreign_hooks_path_is_refused_never_overwritten(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(head=B))
    _seed_local(eng.backend, hooks={CK: _hooks_entry(path="/elsewhere/hooks")})
    with pytest.raises(EngineRefusal, match="hooksPath set to /elsewhere/hooks; refusing to overwrite"):
        eng.plan("install", "system", CID)
    # and the handler refuses it too, should a foreign path appear between plan and apply
    _seed(eng.backend, _state(present=False))
    plan = eng.plan("install", "system", CID)
    clone_state = _state(head=B, installed=B)
    hooks_op = _op_of(plan, "repo.hooks-activate")
    _seed(eng.backend, clone_state)
    _seed_local(eng.backend, hooks={CK: _hooks_entry(path="/elsewhere/hooks", identity=B)})
    verdict = eng.backend.handler("repo.hooks-activate").preflight(hooks_op.to_install_operation())
    assert not verdict.ok and "refusing to overwrite" in verdict.detail


def _install_request(tmp_path):
    """A realistic plan_install request: the two handlers' real observations over a PRESENT checkout.

    The planner module is loaded from the tmp repo copy (its CHECKOUTS_DIR is relative to
    its own file), exactly as the sandboxed planner would see it."""

    import importlib.util

    repo = _repo(tmp_path)
    eng = H.engine(tmp_path, repo)
    _seed(eng.backend, _state(head=B))
    _seed_local(eng.backend, hooks={CK: _hooks_entry()})
    spec = importlib.util.spec_from_file_location("pc_planner_direct", repo / "tools" / "germinate" / "components" / "pantheon-config.py")
    planner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(planner)
    request = {
        "observed": {
            "pantheon-config-repo": eng.backend.handler("repo.clone").observe(CID, "system"),
            "pantheon-config-local": eng.backend.handler("repo.hooks-activate").observe(CID, "system"),
        }
    }
    assert CK in request["observed"]["pantheon-config-local"]["checkouts"]
    return planner, request


def test_install_refuses_without_the_local_observation(tmp_path):
    planner, request = _install_request(tmp_path)
    assert isinstance(planner.plan_install(request), list)  # with the key present it plans normally
    del request["observed"]["pantheon-config-local"]
    with pytest.raises(planner.PlanRefused, match="request carries no pantheon-config-local observation"):
        planner.plan_install(request)


def test_install_refuses_a_present_checkout_missing_from_the_local_observation(tmp_path):
    planner, request = _install_request(tmp_path)
    assert request["observed"]["pantheon-config-repo"]["checkouts"][CK]["exists"]  # present: not the clone path
    assert isinstance(planner.plan_install(request), list)  # with the entry present it plans normally
    del request["observed"]["pantheon-config-local"]["checkouts"][CK]
    with pytest.raises(planner.PlanRefused, match=f"no pantheon-config-local observation for checkout '{CK}'"):
        planner.plan_install(request)


@pytest.mark.parametrize(
    "hooks",
    [
        _hooks_entry(path=None),
        _hooks_entry(modes={"pre-commit": "exec", "commit-msg": "noexec", "pre-push": "exec"}),
        _hooks_entry(modes={"pre-commit": "exec", "commit-msg": "exec", "pre-push": "missing"}),
        _hooks_entry(modes=MODES_OFF),
    ],
    ids=["unset", "noexec", "missing", "all-off"],
)
def test_hand_installed_checkout_with_inactive_hooks_is_refused(setup, hooks):
    _repo_, eng = setup
    _seed(eng.backend, _state(head=B))
    _seed_local(eng.backend, hooks={CK: hooks})
    with pytest.raises(EngineRefusal, match="inactive hooks.*tools/install_hooks.sh"):
        eng.plan("install", "system", CID)


def test_hand_installed_healthy_checkout_is_adopted_by_attestation(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(head=B))
    _seed_local(eng.backend, hooks={CK: _hooks_entry()})
    plan = eng.plan("install", "system", CID)
    [op] = plan.nodes[0].operations
    assert (op.kind, op.target) == ("pantheon-config.adopt-attest", CK)
    assert op.provenance["basis"] == "observed-legacy" and "re_attest" not in op.desired
    [outcome] = eng.apply("install", "system", CID, plan_digest=plan.digest)
    assert outcome.state == "SATISFIED"
    record = _local(eng.backend)["attestations"][CK]
    assert (record["origin"], record["hooks_path"], record["hook_modes"], record["observed_head"]) == (SIM_URL, ".githooks", MODES_OK, B)
    _add_manifest(eng)
    assert eng.verify("system", CID)[0]["ok"]
    assert eng.plan("install", "system", CID).nodes[0].operations == ()  # attested, unchanged
    assert _ck(eng.backend)["installed_sha"] is None  # adoption never claims the checkout as engine-installed


def test_attested_checkout_with_drifted_evidence_is_re_attested(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(head=B))
    _seed_local(eng.backend, hooks={CK: _hooks_entry()}, attestations={CK: _attested(origin="sim://old-origin")})
    plan = eng.plan("install", "system", CID)
    [op] = plan.nodes[0].operations
    assert op.kind == "pantheon-config.adopt-attest" and op.desired["re_attest"] == "true"
    assert op.precondition == {"attestation": "present"}
    eng.apply("install", "system", CID, plan_digest=plan.digest)
    assert _local(eng.backend)["attestations"][CK]["origin"] == SIM_URL


def test_attested_checkout_now_unhealthy_is_refused(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(head=B))
    _seed_local(eng.backend, hooks={CK: _hooks_entry(modes={"pre-commit": "exec", "commit-msg": "exec", "pre-push": "noexec"})}, attestations={CK: _attested()})
    with pytest.raises(EngineRefusal, match="inactive hooks"):
        eng.plan("install", "system", CID)


def test_engine_installed_checkout_with_lost_hooks_install_plans_nothing_and_verify_flags_drift(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(present=False))
    plan = eng.plan("install", "system", CID)
    eng.apply("install", "system", CID, plan_digest=plan.digest)
    _add_manifest(eng)
    assert eng.verify("system", CID)[0]["ok"]
    _seed_local(eng.backend, hooks={})  # the hooks were lost behind the engine's back
    _add_manifest(eng)  # the reseed above dropped the manifest too; only the hooks are lost
    assert eng.plan("install", "system", CID).nodes[0].operations == ()  # drift is configure's, not install's
    [result] = eng.verify("system", CID)
    assert not result["ok"]
    assert any(not c["ok"] and "hooks not active" in c["detail"] for c in result["checks"])
    assert {r["id"] for r in result["invariants"]["results"] if r["status"] != "pass"} == {PC3}  # the list flags the same loss


def test_adopt_attest_handler_preflight_and_verify_guard_the_evidence(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(head=B))
    _seed_local(eng.backend, hooks={CK: _hooks_entry()})
    inst = _op_of(eng.plan("install", "system", CID), "pantheon-config.adopt-attest").to_install_operation()
    handler = eng.backend.handler("pantheon-config.adopt-attest")
    assert handler.preflight(inst).ok
    for label, state, hooks in (
        ("dirty", _state(head=B, dirty=True), _hooks_entry()),
        ("hooks lost", _state(head=B), _hooks_entry(path=None)),
        ("evidence changed", _state(head=B), _hooks_entry(modes={"pre-commit": "exec", "commit-msg": "exec", "pre-push": "missing"})),
        ("absent", _state(present=False), _hooks_entry()),
        ("foreign origin", _state(head=B, origin="sim://someone-else"), _hooks_entry()),
    ):
        _seed(eng.backend, state)
        _seed_local(eng.backend, hooks={CK: hooks})
        assert not handler.preflight(inst).ok, label
    _seed(eng.backend, _state(head=B))
    _seed_local(eng.backend, hooks={CK: _hooks_entry()})
    handler.execute(inst)
    assert handler.verify_operation(inst).ok
    _seed_local(eng.backend, hooks={CK: _hooks_entry(path=None)}, attestations=handler.dump_state()["attestations"])
    assert not handler.verify_operation(inst).ok  # evidence drifted since the attestation


def test_adopt_attest_rollback_restores_the_prior_attestation_only_when_owned(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(head=B))
    _seed_local(eng.backend, hooks={CK: _hooks_entry()}, attestations={CK: _attested(origin="sim://old-origin")})
    inst = _op_of(eng.plan("install", "system", CID), "pantheon-config.adopt-attest").to_install_operation()
    handler = eng.backend.handler("pantheon-config.adopt-attest")
    prior = handler.dump_state()["attestations"][CK]
    mutation = handler.rollback_intent(inst)
    assert handler.can_rollback(mutation).classification == "not-applied"
    handler.execute(inst)
    assert handler.can_rollback(mutation).classification == "owned"
    handler.rollback_mutation(mutation)
    assert handler.dump_state()["attestations"][CK] == prior
    handler.execute(inst)
    handler.state["attestations"][CK]["origin"] = "sim://tampered"
    assert handler.can_rollback(mutation).classification == "drifted"
    with pytest.raises(Exception, match="drifted"):
        handler.rollback_mutation(mutation)


def test_hooks_activate_rollback_is_owned_only_for_its_own_activation(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(present=False))
    plan = eng.plan("install", "system", CID)
    clone, hooks = plan.nodes[0].operations
    _seed(eng.backend, _state(head=B, installed=B))
    handler = eng.backend.handler("repo.hooks-activate")
    mutation = handler.rollback_intent(hooks.to_install_operation())
    assert handler.can_rollback(mutation).classification == "not-applied"
    handler.execute(hooks.to_install_operation())
    assert handler.can_rollback(mutation).classification == "owned"
    handler.state["hooks"][CK]["modes"]["pre-push"] = "noexec"
    assert handler.can_rollback(mutation).classification == "drifted"
    handler.state["hooks"][CK]["modes"]["pre-push"] = "exec"
    handler.rollback_mutation(mutation)
    assert CK not in handler.dump_state()["hooks"]
    # a hand-set, already-correct hooks path is not ours: never "owned"
    _seed_local(eng.backend, hooks={CK: _hooks_entry(modes=MODES_OFF, identity=B)})
    mutation = handler.rollback_intent(hooks.to_install_operation())
    handler.execute(hooks.to_install_operation())
    assert handler.can_rollback(mutation).classification == "drifted"


def test_a_rolled_back_clone_leaves_no_hooks_state_for_a_later_clone(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(present=False))
    plan = eng.plan("install", "system", CID)
    hooks = _op_of(plan, "repo.hooks-activate")
    eng.backend.faults.kill = (hooks.operation_id, "after-execute")
    with pytest.raises(SimulatedKill):
        eng.apply("install", "system", CID, plan_digest=plan.digest)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    eng.reconcile_run(run_id, executor_dead=True)
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    eng.backend.faults.kill = None
    again = eng.plan("install", "system", CID)
    assert [op.kind for op in again.nodes[0].operations] == ["repo.clone", "repo.hooks-activate"]
    [outcome] = eng.apply("install", "system", CID, plan_digest=again.digest)
    assert outcome.state == "SATISFIED"
    # a rollback of that second run's hooks op still unsets the hooks: owned, not drifted
    handler = eng.backend.handler("repo.hooks-activate")
    hooks_inst = _op_of(again, "repo.hooks-activate").to_install_operation()
    done = handler.dump_state()
    handler.load_state({"hooks": {}, "attestations": {}})
    mutation = handler.rollback_intent(hooks_inst)  # what the second run's write-ahead inverse recorded
    assert mutation.before["hooks_path"] == "unset" and mutation.before["engine"] == "false"
    handler.load_state(done)
    assert handler.can_rollback(mutation).classification == "owned"
    handler.rollback_mutation(mutation)
    assert not handler.observe(CID, "system")["checkouts"][CK]["hooks_active"]


def test_hooks_state_of_a_vanished_or_re_cloned_checkout_reads_as_default(setup):
    _repo_, eng = setup
    handler = eng.backend.handler("repo.hooks-activate")
    _seed(eng.backend, _state(head=B, installed=B))
    _seed_local(eng.backend, hooks={CK: _hooks_entry(engine=True, identity=B)})
    assert handler.observe(CID, "system")["checkouts"][CK]["hooks_active"]
    _seed(eng.backend, _state(present=False))  # clone rolled back / reclaimed; the entry went stale
    assert handler.observe(CID, "system")["checkouts"] == {}
    _seed(eng.backend, _state(head=C, installed=C))  # a later clone under another identity
    seen = handler.observe(CID, "system")["checkouts"][CK]
    assert (seen["hooks_path"], seen["hooks_active"], seen["engine_hooks"]) == (None, False, False)


# --- the configure leg: hooks convergence + manifest check-and-record (simulated tier) ---


def _configure_request(tmp_path, *, state=None, hooks="default", manifests="default", attestations=None, manifest_attestations="default", fs=None):
    """A realistic plan_configure request over a healthy, engine-installed, fully-attested checkout
    (the planner is loaded from the tmp repo copy, as the sandboxed planner would see it)."""

    import importlib.util

    repo = _repo(tmp_path)
    eng = H.engine(tmp_path, repo)
    _seed(eng.backend, _state(head=B, installed=B) if state is None else state)
    _seed_local(
        eng.backend,
        hooks={CK: _hooks_entry(engine=True, identity=B)} if hooks == "default" else hooks,
        attestations=attestations,
        manifests={CK: _manifest(identity=B)} if manifests == "default" else manifests,
        manifest_attestations={CK: _m_attested(identity=B)} if manifest_attestations == "default" else manifest_attestations,
        fs=fs,
    )
    spec = importlib.util.spec_from_file_location("pc_planner_direct", repo / "tools" / "germinate" / "components" / "pantheon-config.py")
    planner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(planner)
    request = {
        "observed": {
            "pantheon-config-repo": eng.backend.handler("repo.clone").observe(CID, "system"),
            "pantheon-config-local": eng.backend.handler("repo.hooks-activate").observe(CID, "system"),
        }
    }
    return planner, request


def test_configure_plans_nothing_for_a_converged_checkout(tmp_path):
    planner, request = _configure_request(tmp_path)
    assert request["observed"]["pantheon-config-local"]["checkouts"][CK]["manifest"]["valid"]
    assert planner.plan_configure(request) == []


def test_configure_refuses_without_the_local_observation(tmp_path):
    planner, request = _configure_request(tmp_path)
    del request["observed"]["pantheon-config-local"]
    with pytest.raises(planner.PlanRefused, match="request carries no pantheon-config-local observation"):
        planner.plan_configure(request)


def test_configure_refuses_without_the_repo_observation(tmp_path):
    planner, request = _configure_request(tmp_path)
    del request["observed"]["pantheon-config-repo"]
    with pytest.raises(planner.PlanRefused, match="request carries no pantheon-config-repo observation"):
        planner.plan_configure(request)


def test_configure_refuses_an_absent_checkout_it_never_clones(tmp_path):
    planner, request = _configure_request(tmp_path, state=_state(present=False), hooks={}, manifests={}, manifest_attestations={})
    with pytest.raises(planner.PlanRefused, match="never clones"):
        planner.plan_configure(request)


def test_configure_refuses_a_present_checkout_missing_from_the_local_observation(tmp_path):
    planner, request = _configure_request(tmp_path)
    assert planner.plan_configure(request) == []
    del request["observed"]["pantheon-config-local"]["checkouts"][CK]
    with pytest.raises(planner.PlanRefused, match=f"no pantheon-config-local observation for checkout '{CK}'"):
        planner.plan_configure(request)


def test_configure_refuses_a_checkout_the_engine_has_no_record_of(tmp_path):
    planner, request = _configure_request(
        tmp_path, state=_state(head=B), hooks={CK: _hooks_entry()}, manifests={CK: _manifest()}, manifest_attestations={}
    )
    assert request["observed"]["pantheon-config-local"]["checkouts"][CK]["attestation"] is None
    with pytest.raises(planner.PlanRefused, match="no engine record; run germinate install first"):
        planner.plan_configure(request)


def test_configure_accepts_a_hand_installed_checkout_once_adopted(tmp_path):
    planner, request = _configure_request(
        tmp_path, state=_state(head=B), hooks={CK: _hooks_entry()}, attestations={CK: _attested()}, manifests={CK: _manifest()}, manifest_attestations={}
    )
    [op] = planner.plan_configure(request)
    assert op["kind"] == "pantheon-config.manifest-attest"


def test_configure_refuses_a_foreign_hooks_path(tmp_path):
    planner, request = _configure_request(tmp_path, hooks={CK: _hooks_entry(path="/elsewhere/hooks", engine=True, identity=B)})
    with pytest.raises(planner.PlanRefused, match="hooksPath set to /elsewhere/hooks; refusing to overwrite"):
        planner.plan_configure(request)


@pytest.mark.parametrize("path", [".githooks", None], ids=["githooks", "unset"])
def test_configure_refuses_a_missing_hook(tmp_path, path):
    modes = {"pre-commit": "exec", "commit-msg": "missing", "pre-push": "exec"}
    planner, request = _configure_request(tmp_path, hooks={CK: _hooks_entry(path=path, modes=modes, engine=True, identity=B)})
    with pytest.raises(planner.PlanRefused, match="tools/install_hooks.sh"):
        planner.plan_configure(request)


@pytest.mark.parametrize("engine", [True, False], ids=["engine-owned", "hand-set"])
def test_configure_refuses_githooks_with_a_lost_exec_bit(tmp_path, engine):
    modes = {"pre-commit": "exec", "commit-msg": "exec", "pre-push": "noexec"}
    planner, request = _configure_request(tmp_path, hooks={CK: _hooks_entry(path=".githooks", modes=modes, engine=engine, identity=B)})
    with pytest.raises(planner.PlanRefused, match="tools/install_hooks.sh"):
        planner.plan_configure(request)


def test_configure_plans_hooks_activate_only_when_hooks_path_is_unset(tmp_path):
    planner, request = _configure_request(tmp_path, hooks={})
    [op] = planner.plan_configure(request)
    assert (op["kind"], op["target"]) == ("repo.hooks-activate", CK)
    assert op["precondition"] == {"hooks_path": "unset"} and op["desired"]["hooks_path"] == ".githooks"
    assert op["inverse"] == {"hooks_path": "unset"} and op["crash_contract"] == "write-ahead-inverse-durable"
    # already active: no hooks op
    planner, request = _configure_request(tmp_path / "again")
    assert planner.plan_configure(request) == []


def test_configure_refuses_an_absent_manifest(tmp_path):
    planner, request = _configure_request(tmp_path, manifests={}, manifest_attestations={})
    assert request["observed"]["pantheon-config-local"]["checkouts"][CK]["manifest"] is None
    with pytest.raises(planner.PlanRefused, match="no instance.yaml manifest"):
        planner.plan_configure(request)


@pytest.mark.parametrize("text", list(INVALID_MANIFESTS.values()), ids=list(INVALID_MANIFESTS))
def test_configure_refuses_an_invalid_manifest_with_its_detail(tmp_path, text):
    planner, request = _configure_request(tmp_path, manifests={CK: _manifest(text, identity=B)}, manifest_attestations={})
    seen = request["observed"]["pantheon-config-local"]["checkouts"][CK]["manifest"]
    assert seen["valid"] is False and seen["detail"]
    with pytest.raises(planner.PlanRefused, match="manifest is invalid") as caught:
        planner.plan_configure(request)
    assert seen["detail"] in str(caught.value)


def test_configure_attests_a_valid_unattested_manifest(tmp_path):
    planner, request = _configure_request(tmp_path, manifest_attestations={})
    [op] = planner.plan_configure(request)
    assert (op["kind"], op["operation_id"], op["target"]) == ("pantheon-config.manifest-attest", f"manifest-attest-{CK}", CK)
    assert op["precondition"] == {"attestation": "none", "manifest_digest": _dg(VALID_MANIFEST)}
    assert op["desired"]["digest"] == _dg(VALID_MANIFEST) and op["inverse"] == {"attestation": "none"}
    assert op["crash_contract"] == "write-ahead-inverse-durable" and op["side_effect_free"] is False


def test_configure_re_attests_a_changed_manifest_with_the_old_digest_as_prior(tmp_path):
    planner, request = _configure_request(
        tmp_path, manifests={CK: _manifest(OTHER_MANIFEST, identity=B)}, manifest_attestations={CK: _m_attested(identity=B)}
    )
    [op] = planner.plan_configure(request)
    assert op["precondition"] == {"attestation": _dg(VALID_MANIFEST), "manifest_digest": _dg(OTHER_MANIFEST)}
    assert op["desired"]["digest"] == _dg(OTHER_MANIFEST) and op["inverse"] == {"attestation": _dg(VALID_MANIFEST)}


def test_configure_orders_the_hooks_op_before_the_manifest_op(tmp_path):
    planner, request = _configure_request(tmp_path, hooks={}, manifest_attestations={})
    assert [op["kind"] for op in planner.plan_configure(request)] == ["repo.hooks-activate", "pantheon-config.manifest-attest"]


def test_configure_applies_verifies_and_converges(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(head=B, installed=B))
    _seed_local(eng.backend, manifests={CK: _manifest(identity=B)})
    plan = eng.plan("configure", "system", CID)
    assert [op.kind for op in plan.nodes[0].operations] == ["repo.hooks-activate", "pantheon-config.manifest-attest"]
    [outcome] = eng.apply("configure", "system", CID, plan_digest=plan.digest)
    assert outcome.state == "SATISFIED"
    local = _local(eng.backend)
    assert local["manifest_attestations"][CK]["digest"] == _dg(VALID_MANIFEST)
    assert local["manifests"][CK]["text"] == VALID_MANIFEST  # the engine never rewrote the manifest
    store = RunStore(eng.backend.run_state_root("system"))
    with store.open(store.find(outcome.run_id)) as handle:
        assert [r["op_id"] for r in handle.read_record()["receipts"]] == [f"hooks-activate-{CK}", f"manifest-attest-{CK}"]
    [result] = eng.verify("system", CID)
    assert result["ok"] and len(result["checks"]) == 2
    assert eng.plan("configure", "system", CID).nodes[0].operations == ()


def test_configure_halt_rolls_back_both_ops(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(head=B, installed=B))
    _seed_local(eng.backend, manifests={CK: _manifest(identity=B)})
    before = _local(eng.backend)
    plan = eng.plan("configure", "system", CID)
    eng.backend.faults.kill = (_op_of(plan, "pantheon-config.manifest-attest").operation_id, "after-execute")
    with pytest.raises(SimulatedKill):
        eng.apply("configure", "system", CID, plan_digest=plan.digest)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    assert eng.reconcile_run(run_id, executor_dead=True).state == "HALTED"
    assert CK in _local(eng.backend)["manifest_attestations"]
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    assert _local(eng.backend) == before


def _manifest_op(eng, **seed):
    _seed(eng.backend, _state(head=B, installed=B))
    _seed_local(eng.backend, hooks={CK: _hooks_entry(engine=True, identity=B)}, **seed)
    return _op_of(eng.plan("configure", "system", CID), "pantheon-config.manifest-attest").to_install_operation()


def test_manifest_attest_handler_preflight_and_verify_guard_the_manifest(setup):
    _repo_, eng = setup
    handler = eng.backend.handler("pantheon-config.manifest-attest")
    inst = _manifest_op(eng, manifests={CK: _manifest(identity=B)})
    assert handler.preflight(inst).ok
    for label, state, hooks, manifests in (
        ("dirty", _state(head=B, installed=B, dirty=True), None, {CK: _manifest(identity=B)}),
        ("absent", _state(present=False), None, {CK: _manifest(identity=B)}),
        ("manifest changed", None, None, {CK: _manifest(OTHER_MANIFEST, identity=B)}),
        ("manifest invalid", None, None, {CK: _manifest(INVALID_MANIFESTS["no-projection-block"], identity=B)}),
        ("manifest gone", None, None, {}),
        ("manifest stale identity", None, None, {CK: _manifest(identity=C)}),
    ):
        _seed(eng.backend, state or _state(head=B, installed=B))
        _seed_local(eng.backend, hooks={CK: _hooks_entry(engine=True, identity=B)}, manifests=manifests)
        assert not handler.preflight(inst).ok, label
    _seed(eng.backend, _state(head=B, installed=B))
    _seed_local(eng.backend, hooks={CK: _hooks_entry(engine=True, identity=B)}, manifests={CK: _manifest(identity=B)})
    assert not handler.verify_operation(inst).ok  # nothing recorded yet
    handler.execute(inst)
    assert handler.verify_operation(inst).ok
    state = handler.dump_state()
    state["manifests"][CK]["text"] = OTHER_MANIFEST  # the manifest moved after the attestation
    handler.load_state(state)
    assert not handler.verify_operation(inst).ok
    state["manifests"][CK]["text"] = VALID_MANIFEST
    state["manifest_attestations"][CK]["digest"] = _dg(OTHER_MANIFEST)  # attestation not as planned
    handler.load_state(state)
    assert not handler.verify_operation(inst).ok


def test_manifest_attest_rollback_restores_the_prior_attestation_only_when_owned(setup):
    _repo_, eng = setup
    handler = eng.backend.handler("pantheon-config.manifest-attest")
    inst = _manifest_op(eng, manifests={CK: _manifest(OTHER_MANIFEST, identity=B)}, manifest_attestations={CK: _m_attested(identity=B)})
    prior = handler.dump_state()["manifest_attestations"][CK]
    mutation = handler.rollback_intent(inst)
    assert handler.can_rollback(mutation).classification == "not-applied"
    handler.execute(inst)
    assert handler.can_rollback(mutation).classification == "owned"
    handler.rollback_mutation(mutation)
    assert handler.dump_state()["manifest_attestations"][CK] == prior
    handler.execute(inst)
    handler.state["manifest_attestations"][CK]["digest"] = "f" * 64  # tampered
    assert handler.can_rollback(mutation).classification == "drifted"
    with pytest.raises(Exception, match="drifted"):
        handler.rollback_mutation(mutation)
    assert handler.dump_state()["manifest_attestations"][CK]["digest"] == "f" * 64  # a refused rollback changed nothing


def test_manifest_attest_rollback_of_a_first_attestation_leaves_none(setup):
    _repo_, eng = setup
    handler = eng.backend.handler("pantheon-config.manifest-attest")
    inst = _manifest_op(eng, manifests={CK: _manifest(identity=B)})
    mutation = handler.rollback_intent(inst)
    assert mutation.before == {"attestation": "none"}
    handler.execute(inst)
    handler.rollback_mutation(mutation)
    assert handler.dump_state()["manifest_attestations"] == {}


def test_manifest_state_of_a_vanished_or_re_cloned_checkout_reads_as_absent(setup):
    _repo_, eng = setup
    handler = eng.backend.handler("pantheon-config.manifest-attest")
    _seed(eng.backend, _state(head=B, installed=B))
    _seed_local(eng.backend, manifests={CK: _manifest(identity=B)}, manifest_attestations={CK: _m_attested(identity=B)})
    seen = handler.observe(CID, "system")["checkouts"][CK]
    assert seen["manifest"]["valid"] and seen["manifest_attestation"]["digest"] == _dg(VALID_MANIFEST)
    _seed(eng.backend, _state(present=False))
    assert handler.observe(CID, "system")["checkouts"] == {}
    _seed(eng.backend, _state(head=C, installed=C))  # a later clone under another identity
    seen = handler.observe(CID, "system")["checkouts"][CK]
    assert seen["manifest"] is None and seen["manifest_attestation"] is None


def test_observe_reports_manifest_validity_and_raw_byte_digest(setup):
    _repo_, eng = setup
    handler = eng.backend.handler("pantheon-config.manifest-attest")
    _seed(eng.backend, _state(head=B, installed=B))
    for text in (VALID_MANIFEST, *INVALID_MANIFESTS.values()):
        _seed_local(eng.backend, manifests={CK: _manifest(text, identity=B)})
        seen = handler.observe(CID, "system")["checkouts"][CK]["manifest"]
        assert seen["digest"] == _dg(text) and seen["valid"] is (text == VALID_MANIFEST)
        assert bool(seen["detail"]) is (text != VALID_MANIFEST)


def test_manifest_validity_leaves_no_residue_in_the_loader_cache(setup):
    import os
    import tempfile

    from tools import agent_role_map as ARM

    _repo_, eng = setup
    handler = eng.backend.handler("pantheon-config.manifest-attest")
    _seed(eng.backend, _state(head=B, installed=B))
    _seed_local(eng.backend, manifests={CK: _manifest(identity=B)})
    before = dict(ARM._MANIFEST_CACHE)
    for text in (VALID_MANIFEST, *INVALID_MANIFESTS.values()):
        _seed_local(eng.backend, manifests={CK: _manifest(text, identity=B)})
        handler.observe(CID, "system")
    assert ARM._MANIFEST_CACHE == before
    assert not [p for p in os.listdir(tempfile.gettempdir()) if p.startswith("germinate-manifest-")]


def test_manifest_validity_uses_the_fail_closed_projection_loader_only():
    """Grep-level pin (ruling S1 = A): validity is _load_membership_projection, never the swallowing loaders."""

    src = (COMPONENTS / "pantheon-config_handlers.py").read_text(encoding="utf-8")
    assert "_load_membership_projection" in src
    for swallowing in ("_load_instance_role_map", "_load_instance_bindings", "_load_instance_mailbox", "load_instance_document"):
        assert swallowing not in src, swallowing


# --- registration and the registry ------------------------------------------


def test_component_kinds_are_seat_privilege_in_their_own_class(setup):
    _repo_, eng = setup
    from tools.germinate.components import pantheon_config_git as PG

    kinds = ("repo.clone", "repo.ff-advance", "repo.reclaim", "repo.hooks-activate", "pantheon-config.adopt-attest", "pantheon-config.manifest-attest")
    assert len(kinds) == 6
    assert set(kinds) == PG.KINDS | {"repo.hooks-activate", "pantheon-config.adopt-attest", "pantheon-config.manifest-attest"}
    assert eng.backend.handler("repo.clone").kinds == PG.KINDS
    for kind in kinds:
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
    assert (row["legs"]["install"]["status"], row["legs"]["install"]["ticket"]) == ("partial", "T-6098")
    legacy = row["legacy_coverage"]["install"]
    assert legacy["status"] == "partial" and legacy["ticket"] == "T-6098" and "simulated tier" in legacy["gap"]
    configure = row["legs"]["configure"]
    assert (configure["status"], configure["ticket"]) == ("partial", "T-6098")
    assert "SIMULATED tier only" in configure["gap"] and "test_configure_applies_verifies_and_converges" in configure["gap"]
    assert "never writes instance.yaml" in configure["gap"] and "roles stanza" in configure["gap"]
    assert "plan_configure" not in row["adoption_gaps"]
    legacy = row["legacy_coverage"]["configure"]
    assert (legacy["status"], legacy["ticket"]) == ("partial", "T-6098") and "ensure-hooks-active.sh" in legacy["gap"]
    assert row["contract"]["digest"] == compute_contract_digest(COMPONENTS / "pantheon-config.yaml")


def test_no_registry_row_cites_t5107_any_more():
    """PR3 closes the arc: the health leg and both adoption gaps moved to T-6096."""

    text = (REPO_ROOT / "registry" / "systems.yaml").read_text(encoding="utf-8")
    assert "ticket: T-5107" not in text
    doc = yaml.safe_load(text)
    [row] = [r for r in doc["systems"] if r.get("id") == CID]
    assert row["legs"]["health"]["ticket"] == "T-6096" and row["adoption_gaps"]["adoption_evidence.emitter"]["ticket"] == "T-6096"


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
        _converged(H.Engine.from_checkout(sim_root, repo_root=repo).backend, health=health)  # TEST HELPER seed
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


def test_cli_smoke_configure_plan_apply_verify_and_rollback(tmp_path, capsys):
    repo = _repo(tmp_path)

    def seeded(name):
        sim_root = tmp_path / f"cfg-{name}"
        common = ["--repo-root", str(repo), "--simulated-host-root", str(sim_root)]
        assert main(["sim", "init", "--simulated-host-root", str(sim_root)]) == 0
        backend = H.Engine.from_checkout(sim_root, repo_root=repo).backend
        _seed(backend, _state(head=B, installed=B))  # TEST HELPER seeds
        _seed_local(backend, manifests={CK: _manifest(identity=B)})
        capsys.readouterr()
        configure = ["--system", CID, *common]
        assert main(["plan", "configure", *configure]) == 0
        return sim_root, common, configure, _result(capsys.readouterr().out)["plan_digest"]

    sim_root, common, configure, digest = seeded("ok")
    assert main(["apply", "configure", "--plan-digest", digest, *configure]) == 0
    [outcome] = _result(capsys.readouterr().out)
    assert outcome["state"] == "SATISFIED"
    assert main(["verify", "--system", CID, *common]) == 0
    capsys.readouterr()
    assert main(["rollback", "--run", outcome["run_id"], *common]) == 1  # SATISFIED is absorbing
    assert "rollback-refused" in capsys.readouterr().err

    # a halted variant (killed after the manifest attestation), reconciled, then rolled back by the CLI
    sim_root, common, configure, digest = seeded("halt")
    eng = H.Engine.from_checkout(sim_root, repo_root=repo)
    plan = eng.plan("configure", "system", CID)
    assert plan.digest == digest
    eng.backend.faults.kill = (_op_of(plan, "pantheon-config.manifest-attest").operation_id, "after-execute")
    with pytest.raises(SimulatedKill):
        eng.apply("configure", "system", CID, plan_digest=plan.digest)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    assert H.Engine.from_checkout(sim_root, repo_root=repo).reconcile_run(run_id, executor_dead=True).state == "HALTED"
    assert main(["rollback", "--run", run_id, *common]) == 0
    assert _result(capsys.readouterr().out)["state"] == "ROLLED_BACK"
    assert _local(H.Engine.from_checkout(sim_root, repo_root=repo).backend)["manifest_attestations"] == {}




# --- T-5107 PR3: the health leg's verify invariant list org.pantheon.pantheon-config/v1 (PC-1..PC-6) ----

LIST_ID = "org.pantheon.pantheon-config/v1"
PC_IDS = ["PC-1-checkout-managed", "PC-2-tree-clean", "PC-3-hooks-active", "PC-4-currency", "PC-5-manifest-valid", "PC-6-attestations-current"]
PC1, PC2, PC3, PC4, PC5, PC6 = PC_IDS
REPO_H, LOCAL_H = "pantheon-config-repo", "pantheon-config-local"


def _sha(i: int) -> str:
    return format(i, "040x")


def _installed(eng):
    """A satisfied engine install of an absent checkout (converged: head B, hooks active, a valid manifest)."""

    _seed(eng.backend, _state(present=False))
    plan = eng.plan("install", "system", CID)
    [outcome] = eng.apply("install", "system", CID, plan_digest=plan.digest)
    assert outcome.state == "SATISFIED"
    installed = eng.backend.handler("repo.clone").dump_state()["checkouts"][CK]["installed_sha"]
    _mutate(eng, "repo.hooks-activate", lambda s: s["manifests"].update({CK: _manifest(identity=installed)}))  # the engine never writes instance.yaml


def _mutate(eng, kind, fn) -> None:
    """TEST HELPER: edit the simulated repo (kind repo.clone) or local (repo.hooks-activate) handler state in place."""

    handler = eng.backend.handler(kind)
    state = handler.dump_state()
    fn(state)
    handler.load_state(state)
    eng.backend.save()


def _inv(eng):
    [result] = eng.verify("system", CID)
    return result, {r["id"]: r for r in result["invariants"]["results"]}, result["invariants"]["state"]


def _not_pass(by_id):
    return sorted(i for i, r in by_id.items() if r["status"] != "pass")


def _breach(setup, kind, fn, *, only, state):
    _repo_, eng = setup
    _installed(eng)
    _mutate(eng, kind, fn)
    result, by_id, got = _inv(eng)
    assert _not_pass(by_id) == [only], by_id
    assert got == state and not result["ok"]
    return by_id[only]["detail"]


def _observed(eng):
    """The VERIFY input shape for a converged install."""

    _installed(eng)
    h = eng.backend
    return {
        REPO_H: h.handler("repo.clone").observe(CID, "system"),
        LOCAL_H: h.handler("repo.hooks-activate").observe(CID, "system"),
    }


def _ph():
    import importlib.util

    spec = importlib.util.spec_from_file_location("pc_handlers_under_test", COMPONENTS / "pantheon-config_handlers.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PH = _ph()


def _verify(observed, ids=PC_IDS):
    out = PH.VERIFY({"invariants": [{"id": i} for i in ids]}, observed, "system")
    return out["state"], {r["id"]: r for r in out["results"]}


def _raw(observed):
    return observed[REPO_H]["checkouts"][CK]


def _loc(observed):
    return observed[LOCAL_H]["checkouts"][CK]


def test_real_list_is_current_and_bound_to_the_verify_leg():
    from tools.germinate.engine import invariant_digest as ID

    text = (COMPONENTS / "pantheon-config.invariants.yaml").read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    assert doc["schema"] == "pantheon-invariant-list/v0" and doc["id"] == LIST_ID
    assert doc["status"] == "CURRENT" and doc["co_author"] == "recovery" and doc["signoff"] == "complete 2026-11-03"
    flat = " ".join(line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("#"))  # the header, unwrapped
    assert "Recovery signed PC-1..PC-6 on 2026-11-03 (D15)" in flat and "changes A-C as built" in flat  # the D15 signoff
    assert "stays status DRAFT" not in flat and "Open signoff points" not in flat and "there is no B2" in flat
    assert [i["id"] for i in doc["invariants"]] == PC_IDS
    body = yaml.safe_load((COMPONENTS / "pantheon-config.yaml").read_text(encoding="utf-8"))["component"]
    digest, list_id = ID.compute(COMPONENTS / "pantheon-config.yaml")
    assert list_id == LIST_ID == body["invariant_list_id"]
    assert body["invariant_list_digest"] == digest
    assert body["legs"]["verify"] == {"invariant_list_ref": f"{LIST_ID}@{digest}"}
    assert "emitter" not in body  # the live collector derives subscriptions from emitter blocks (T-6096, D15)
    assert PH.HANDLERS == [PH._REPO, PH._LOCAL]
    reg = yaml.safe_load((REPO_ROOT / "registry" / "systems.yaml").read_text(encoding="utf-8"))
    [row] = [r for r in reg["systems"] if r.get("id") == CID]
    assert "verify_contract" not in row["adoption_gaps"]  # a bound list closes it
    health = row["legs"]["health"]
    assert health["status"] == "partial" and health["ticket"] == "T-6096"
    assert "T-6098" in health["gap"] and "PC-4" in health["gap"]
    assert "own .githooks" in health["gap"] and "PENDING_PUSH" in health["gap"] and "24h" in health["gap"]
    assert row["adoption_gaps"]["adoption_evidence.emitter"]["ticket"] == "T-6096"


def _hook_files(githooks_dir):
    """The hooks a ``.githooks`` directory holds, by the production rule.

    Production names its hooks explicitly (install_hooks.sh, ensure-hooks-active.sh, HOOK_NAMES) and never lists
    the directory; a hook is a regular file there. A directory, a dot/dunder entry or a compiled-bytecode
    artefact (``__pycache__``, ``*.pyc``) is interpreter debris from importing a hook module, not a hook."""

    return sorted(
        p.name
        for p in Path(githooks_dir).iterdir()
        if p.is_file() and not p.name.startswith(".") and not (p.name.startswith("__") and p.name.endswith("__")) and p.suffix != ".pyc"
    )


def _fixture_githooks(tmp_path, extra=()):
    d = tmp_path / ".githooks"
    d.mkdir()
    for name in (*PH.HOOK_NAMES, *extra):
        (d / name).write_text("#!/bin/sh\n", encoding="utf-8")
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "hook.cpython-312.pyc").write_bytes(b"\0")
    (d / "sub").mkdir()  # a plain directory is not a hook: only the is_file rule excludes it
    (d / "sub" / "helper.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (d / "stray.pyc").write_bytes(b"\0")
    return d


def test_hook_files_ignores_bytecode_debris_in_githooks(tmp_path):
    assert _hook_files(_fixture_githooks(tmp_path)) == sorted(PH.HOOK_NAMES)


def test_hook_files_still_sees_an_unlisted_hook(tmp_path):
    files = _hook_files(_fixture_githooks(tmp_path, extra=("post-checkout",)))
    assert files != sorted(PH.HOOK_NAMES) and "post-checkout" in files


def test_hooks_and_currency_constants_mirror_their_sources():
    """The evaluator never imports its monitors; these pins keep its literals equal to theirs."""

    import ast

    sh = (REPO_ROOT / "tools" / "hooks" / "ensure-hooks-active.sh").read_text(encoding="utf-8")
    assert f'SUBDIR="{PH.HOOKS_PATH}"' in sh
    assert f"for h in {' '.join(PH.HOOK_NAMES)}; do" in sh
    assert _hook_files(REPO_ROOT / ".githooks") == sorted(PH.HOOK_NAMES)
    tree = ast.parse((REPO_ROOT / "tools" / "fleet_clone_currency_check.py").read_text(encoding="utf-8"))
    consts, names = {}, {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Tuple) and isinstance(node.value, ast.Tuple):
            for t, v in zip(node.targets[0].elts, node.value.elts):
                consts[t.id] = v.value
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id == "DRIFT_THRESHOLD_DEFAULT":
                [default] = [a.value for a in ast.walk(node.value) if isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value.isdigit()]
                consts["threshold"] = int(default)
            if node.targets[0].id == "_ALERT_VERDICTS":
                names = {e.id for e in node.value.elts}
    assert (PH.CURRENT, PH.DRIFTED, PH.OFF_MAIN) == (consts["CURRENT"], consts["DRIFTED"], consts["OFF_MAIN"])
    assert PH.DRIFT_THRESHOLD == consts["threshold"] == 20
    assert {"DRIFTED", "OFF_MAIN"} <= names and "PENDING_PUSH" not in names  # ahead is the monitor's non-alert PENDING_PUSH


def test_healthy_install_passes_every_invariant_and_is_ready(setup):
    _repo_, eng = setup
    _installed(eng)
    result, by_id, state = _inv(eng)
    assert len(result["invariants"]["results"]) == 6
    assert list(by_id) == PC_IDS and {r["status"] for r in by_id.values()} == {"pass"}
    assert state == "ready" and result["ok"]


# --- PC-1 ---


def test_pc1_absent_checkout_is_failed(setup):
    detail = _breach(setup, "repo.clone", lambda s: s["checkouts"].clear(), only=PC1, state="unhealthy")
    assert CK in detail and "absent" in detail


def test_pc1_foreign_origin_is_failed(setup):
    detail = _breach(setup, "repo.clone", lambda s: s["checkouts"][CK].update(origin="sim://someone-else"), only=PC1, state="unhealthy")
    assert "not the managed remote" in detail and CK in detail


def test_pc1_empty_observation_is_failed(setup):
    observed = _observed(setup[1])
    observed[REPO_H]["checkouts"] = {}
    state, by_id = _verify(observed)
    assert by_id[PC1]["status"] == "fail" and state == "unhealthy" and "no checkout observed" in by_id[PC1]["detail"]


@pytest.mark.parametrize("bad", [None, "yes", 1, 0, False])
def test_pc1_exists_and_is_managed_must_be_true(setup, bad):
    for key in ("exists", "is_managed"):
        observed = _observed(setup[1])
        _raw(observed)[key] = bad
        state, by_id = _verify(observed)
        assert by_id[PC1]["status"] == "fail" and state == "unhealthy", key


def test_pc1_malformed_checkout_entry_is_failed(setup):
    observed = _observed(setup[1])
    observed[REPO_H]["checkouts"][CK] = "garbage"
    state, by_id = _verify(observed)
    assert by_id[PC1]["status"] == "fail" and state == "unhealthy" and "malformed" in by_id[PC1]["detail"]


# --- PC-2 ---


def test_pc2_dirty_tree_is_failed(setup):
    detail = _breach(setup, "repo.clone", lambda s: s["checkouts"][CK].update(dirty=True), only=PC2, state="unhealthy")
    assert CK in detail and "dirty" in detail


@pytest.mark.parametrize("bad", [None, "no", 1, 0, "False"])
def test_pc2_non_bool_dirty_is_failed(setup, bad):
    observed = _observed(setup[1])
    _raw(observed)["dirty"] = bad
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC2] and state == "unhealthy" and "not a bool" in by_id[PC2]["detail"]


def test_pc2_missing_dirty_fact_is_failed(setup):
    observed = _observed(setup[1])
    del _raw(observed)["dirty"]
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC2] and state == "unhealthy"


# --- PC-3 ---


@pytest.mark.parametrize("path", [None, "other-hooks", "/abs/.githooks"], ids=["unset", "other-dir", "absolute-other-clone-nonexistent"])
def test_pc3_wrong_hooks_path_is_failed(setup, path):
    detail = _breach(setup, "repo.hooks-activate", lambda s: s["hooks"][CK].update(hooks_path=path), only=PC3, state="unhealthy")
    assert ".githooks" in detail and CK in detail


@pytest.mark.parametrize("mode", ["noexec", "missing"])
def test_pc3_hook_not_executable_is_failed(setup, mode):
    detail = _breach(setup, "repo.hooks-activate", lambda s: s["hooks"][CK]["modes"].update({"pre-push": mode}), only=PC3, state="unhealthy")
    assert "pre-push" in detail and CK in detail


@pytest.mark.parametrize("path", [None, "", 7, [".githooks"]])
def test_pc3_non_string_or_wrong_path_is_failed(setup, path):
    observed = _observed(setup[1])
    _loc(observed)["hooks_path"] = path
    assert _verify(observed)[1][PC3]["status"] == "fail"


@pytest.mark.parametrize(
    "modes", [None, "exec", [], {}, {"pre-commit": "exec", "commit-msg": "exec"}, {"pre-commit": "exec", "commit-msg": "exec", "pre-push": True}, {"pre-commit": "exec", "commit-msg": "exec", "pre-push": "EXEC"}]
)
def test_pc3_malformed_or_incomplete_modes_are_failed(setup, modes):
    observed = _observed(setup[1])
    _loc(observed)["hook_modes"] = modes
    assert _verify(observed)[1][PC3]["status"] == "fail"


@pytest.mark.parametrize("flag", [False, None, "true", 1])
def test_pc3_a_lying_hooks_active_flag_is_failed(setup, flag):
    observed = _observed(setup[1])
    _loc(observed)["hooks_active"] = flag  # path and modes are healthy: the flag alone disagrees
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC3] and state == "unhealthy" and "hooks_active" in by_id[PC3]["detail"]


def test_pc3_flag_true_over_a_foreign_path_still_fails(setup):
    observed = _observed(setup[1])
    _loc(observed)["hooks_path"] = "foreign"
    _loc(observed)["hooks_path_own"] = False
    _loc(observed)["hooks_active"] = True
    assert _verify(observed)[1][PC3]["status"] == "fail"


def test_a_checkout_missing_its_local_observation_never_passes(setup):
    observed = _observed(setup[1])
    del observed[LOCAL_H]["checkouts"][CK]
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC3, PC5, PC6] and state == "degraded"  # unknown is not a breach, and never a pass
    assert all("could not evaluate" in by_id[i]["detail"] and CK in by_id[i]["detail"] for i in (PC3, PC5, PC6))
    assert by_id[PC1]["status"] == "pass"


@pytest.mark.parametrize("mutate", [lambda o: o.pop(LOCAL_H), lambda o: o.update({LOCAL_H: None}), lambda o: o[LOCAL_H].pop("checkouts"), lambda o: o[LOCAL_H].update(checkouts=[])])
def test_no_local_observation_at_all_degrades_pc3_pc5_pc6_without_raising(setup, mutate):
    observed = _observed(setup[1])
    mutate(observed)
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC3, PC5, PC6] and state == "degraded"


@pytest.mark.parametrize("bad", [None, "x", []], ids=["none", "str", "list"])
def test_missing_or_non_dict_local_observation_is_degraded_for_pc3_pc5_pc6(setup, bad):
    observed = _observed(setup[1])
    observed[LOCAL_H]["checkouts"][CK] = bad
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC3, PC5, PC6] and state == "degraded"
    assert all(by_id[i]["status"] == "fail" and f"{CK}: no local observation (could not evaluate)" in by_id[i]["detail"] for i in (PC3, PC5, PC6))


# --- PC-4 ---


def test_pc4_off_tracking_branch_is_degraded_and_detached_head_too(setup):
    detail = _breach(setup, "repo.clone", lambda s: s["checkouts"][CK].update(branch="feature"), only=PC4, state="degraded")
    assert "OFF_MAIN" in detail and "feature" in detail and "main" in detail and CK in detail
    detail = _breach(setup, "repo.clone", lambda s: s["checkouts"][CK].update(branch="HEAD"), only=PC4, state="degraded")
    assert "OFF_MAIN" in detail and "detached" in detail


def _chain(start, n):
    """n commits above ``start`` (parents map entries plus the new tip)."""

    parents, prev = {}, start
    for i in range(1, n + 1):
        parents[_sha(0x1000 + i)] = prev
        prev = _sha(0x1000 + i)
    return parents, prev


@pytest.mark.parametrize("behind, status", [(20, "pass"), (21, "fail")])
def test_pc4_behind_threshold_is_twenty_exclusive(setup, behind, status):
    _repo_, eng = setup
    _installed(eng)
    parents, tip = _chain(B, behind)
    _mutate(eng, "repo.clone", lambda s: (s["remote"]["parents"].update(parents), s["remote"]["refs"].update(main=tip)))
    result, by_id, state = _inv(eng)
    assert by_id[PC4]["status"] == status
    if status == "pass":
        assert _not_pass(by_id) == [] and state == "ready"
    else:
        assert _not_pass(by_id) == [PC4] and state == "degraded" and not result["ok"]
        assert "DRIFTED" in by_id[PC4]["detail"] and "21" in by_id[PC4]["detail"]


def test_pc4_unreachable_remote_is_degraded_could_not_evaluate(setup):
    detail = _breach(setup, "repo.clone", lambda s: s["remote"].update(reachable=False), only=PC4, state="degraded")
    assert "unreachable" in detail and "could not evaluate" in detail


def test_pc4_no_resolved_target_is_degraded_could_not_evaluate(setup):
    detail = _breach(setup, "repo.clone", lambda s: s["remote"]["refs"].clear(), only=PC4, state="degraded")
    assert "no target" in detail and "could not evaluate" in detail


def test_pc4_unresolvable_ancestry_is_degraded_could_not_evaluate(setup):
    observed = _observed(setup[1])
    _raw(observed).update(desired_target=_sha(0x77), desired_resolvable=False)
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC4] and state == "degraded" and "ancestry" in by_id[PC4]["detail"] and "could not evaluate" in by_id[PC4]["detail"]


def test_pc4_ahead_is_not_a_breach(setup):
    _repo_, eng = setup
    _installed(eng)
    _mutate(eng, "repo.clone", lambda s: (s["checkouts"][CK].update(head=D, local_parents={D: B}), s.update(commit_ts={D: CLOCK0 - 60}, clock=CLOCK0)))
    observed = {REPO_H: eng.backend.handler("repo.clone").observe(CID, "system"), LOCAL_H: eng.backend.handler("repo.hooks-activate").observe(CID, "system")}
    assert _raw(observed)["ahead"] == 1 and _raw(observed)["behind"] == 0
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [] and state == "ready"


def test_pc4_off_main_outranks_the_behind_count(setup):
    observed = _observed(setup[1])
    _raw(observed).update(branch="feature", behind=25)
    state, by_id = _verify(observed)
    assert "OFF_MAIN" in by_id[PC4]["detail"] and "DRIFTED" not in by_id[PC4]["detail"] and state == "degraded"


@pytest.mark.parametrize("drop", ["tracking", "branch"])
def test_pc4_unobserved_branch_key_is_degraded_could_not_evaluate(setup, drop):
    observed = _observed(setup[1])
    del _raw(observed)[drop]
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC4] and state == "degraded"
    assert f"{CK}: branch not observed (could not evaluate)" in by_id[PC4]["detail"]


@pytest.mark.parametrize("value", [None, "", 3, ["main"]])
def test_pc4_mistyped_branch_is_degraded(setup, value):
    observed = _observed(setup[1])
    _raw(observed)["branch"] = value
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC4] and state == "degraded"


@pytest.mark.parametrize("value", ["", 3, ["main"]])
def test_pc4_mistyped_tracking_is_degraded(setup, value):
    observed = _observed(setup[1])
    _raw(observed)["tracking"] = value
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC4] and state == "degraded"


_CURRENCY_FACTS = ("remote_reachable", "head_sha", "desired_target", "behind", "ahead", "desired_resolvable")
CLOCK0 = 1_800_000_000  # an injected sim clock (epoch seconds); no test reads real time


@pytest.mark.parametrize("fact", _CURRENCY_FACTS)
def test_pc4_missing_currency_fact_is_failed(setup, fact):
    observed = _observed(setup[1])
    del _raw(observed)[fact]
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC4] and state == "unhealthy" and fact in by_id[PC4]["detail"] and CK in by_id[PC4]["detail"]


@pytest.mark.parametrize(
    "fact, value",
    [
        ("remote_reachable", "yes"), ("remote_reachable", None), ("desired_resolvable", 1), ("desired_resolvable", None),
        ("head_sha", None), ("head_sha", ""), ("head_sha", 7), ("desired_target", 7), ("desired_target", ["x"]),
        ("behind", None), ("behind", "3"), ("behind", True), ("behind", 2.0), ("behind", -1),
        ("ahead", None), ("ahead", "1"), ("ahead", False), ("ahead", -1),
    ],
)
def test_pc4_mistyped_currency_fact_is_failed(setup, fact, value):
    observed = _observed(setup[1])
    _raw(observed)[fact] = value
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC4] and state == "unhealthy"


def test_pc4_pinned_mode_is_current_only_at_the_pin(setup):
    observed = _observed(setup[1])
    _raw(observed).update(tracking=None, branch=None, desired_target=_raw(observed)["head_sha"])
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [] and state == "ready"
    _raw(observed).update(desired_target=_sha(0x55), behind=1)  # moved off the pin: no branch rule, no tolerance
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC4] and state == "degraded"


def test_pc4_is_skipped_for_an_unmanaged_checkout_which_pc1_reports(setup):
    observed = _observed(setup[1])
    _raw(observed).update(is_managed=False, head_sha=None, behind=0, desired_resolvable=False, dirty=True)
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC1] and state == "unhealthy"


# --- PC-5 ---


@pytest.mark.parametrize("text", list(INVALID_MANIFESTS.values()), ids=list(INVALID_MANIFESTS))
def test_pc5_unattested_invalid_manifest_is_failed(setup, text):
    detail = _breach(setup, "repo.hooks-activate", lambda s: s["manifests"].update({CK: _manifest(text, identity=B)}), only=PC5, state="unhealthy")
    assert "invalid" in detail and CK in detail


def test_pc5_absent_manifest_is_failed(setup):
    detail = _breach(setup, "repo.hooks-activate", lambda s: s["manifests"].clear(), only=PC5, state="unhealthy")
    assert "absent" in detail and CK in detail


def test_pc5_missing_manifest_observation_is_failed(setup):
    observed = _observed(setup[1])
    del _loc(observed)["manifest"]
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC5] and state == "unhealthy" and "could not evaluate" in by_id[PC5]["detail"]


@pytest.mark.parametrize("bad", ["x", [], 3, {"digest": "0" * 64}, {"digest": "0" * 64, "valid": "yes"}, {"digest": "0" * 64, "valid": False, "detail": "no block"}])
def test_pc5_malformed_or_invalid_manifest_observation_is_failed(setup, bad):
    observed = _observed(setup[1])
    _loc(observed)["manifest"] = bad
    state, by_id = _verify(observed)
    assert by_id[PC5]["status"] == "fail" and state == "unhealthy"


# --- PC-6 ---


def test_pc6_attestation_drift_is_degraded_and_names_the_keys(setup):
    detail = _breach(
        setup, "repo.hooks-activate",
        lambda s: s["attestations"].update({CK: _attested(origin="sim://old-origin", identity=B)}), only=PC6, state="degraded",
    )
    assert "origin" in detail and "hook_modes" not in detail


def test_pc6_observed_head_is_not_part_of_currency(setup):
    _repo_, eng = setup
    _installed(eng)
    _mutate(eng, "repo.hooks-activate", lambda s: s["attestations"].update({CK: _attested(head=A, identity=B)}))
    result, by_id, state = _inv(eng)
    assert _not_pass(by_id) == [] and state == "ready" and result["ok"]


def test_pc6_manifest_attestation_drift_is_degraded_and_named(setup):
    _repo_, eng = setup
    _installed(eng)
    _mutate(eng, "repo.hooks-activate", lambda s: s.update(manifest_attestations={CK: _m_attested(identity=B)}))
    assert _inv(eng)[2] == "ready"
    _mutate(eng, "repo.hooks-activate", lambda s: s["manifests"].update({CK: _manifest(OTHER_MANIFEST, identity=B)}))
    result, by_id, state = _inv(eng)
    assert _not_pass(by_id) == [PC6] and state == "degraded" and "manifest.digest" in by_id[PC6]["detail"]


def test_pc6_attested_manifest_gone_is_pc5_failed_and_pc6_degraded(setup):
    _repo_, eng = setup
    _installed(eng)
    _mutate(eng, "repo.hooks-activate", lambda s: s.update(manifest_attestations={CK: _m_attested(identity=B)}))
    _mutate(eng, "repo.hooks-activate", lambda s: s["manifests"].clear())
    _r, by_id, state = _inv(eng)
    assert _not_pass(by_id) == [PC5, PC6] and state == "unhealthy" and "manifest.digest" in by_id[PC6]["detail"]


def test_pc6_attestation_with_missing_stable_key_is_drift(setup):
    observed = _observed(setup[1])
    attested = {k: _loc(observed)["evidence"][k] for k in ("checkout_id", "origin", "hooks_path", "hook_modes")}
    _loc(observed)["attestation"] = dict(attested)
    assert _verify(observed)[0] == "ready"
    for key in attested:
        _loc(observed)["attestation"] = {k: v for k, v in attested.items() if k != key}
        state, by_id = _verify(observed)
        assert by_id[PC6]["status"] == "fail" and state == "degraded" and key in by_id[PC6]["detail"], key


@pytest.mark.parametrize("attestation", ["x", [], 3, True])
def test_pc6_observed_non_object_attestation_still_failed(setup, attestation):
    observed = _observed(setup[1])
    _loc(observed)["attestation"] = attestation
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC6] and state == "unhealthy"


@pytest.mark.parametrize("evidence", [None, "x", [], 3])
def test_pc6_evidence_non_object_with_attestation_still_failed(setup, evidence):
    observed = _observed(setup[1])
    _loc(observed)["attestation"] = {k: _loc(observed)["evidence"][k] for k in ("checkout_id", "origin", "hooks_path", "hook_modes")}
    _loc(observed)["evidence"] = evidence
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC6] and state == "unhealthy"
    assert "could not evaluate" not in by_id[PC6]["detail"]  # an observed breach, not an unknown


@pytest.mark.parametrize("recorded", ["x", [], 3, {}, {"digest": ""}, {"digest": 5}])
def test_pc6_malformed_manifest_attestation_is_failed(setup, recorded):
    observed = _observed(setup[1])
    _loc(observed)["manifest_attestation"] = recorded
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC6] and state == "unhealthy"


@pytest.mark.parametrize("key", ["attestation", "manifest_attestation"])
def test_pc6_unobserved_attestation_fact_is_degraded(setup, key):
    observed = _observed(setup[1])
    del _loc(observed)[key]
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC6] and state == "degraded" and "could not evaluate" in by_id[PC6]["detail"]


def test_pc6_manifest_attestation_matching_digest_passes(setup):
    observed = _observed(setup[1])
    _loc(observed)["manifest_attestation"] = {"digest": _loc(observed)["manifest"]["digest"]}
    assert _verify(observed)[0] == "ready"
    _loc(observed)["manifest_attestation"] = {"digest": "0" * 64}
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC6] and state == "degraded"


# --- state mapping, entry guard, evaluator plumbing ---


def test_state_mapping(setup):
    _repo_, eng = setup
    observed = _observed(eng)
    assert _verify(observed)[0] == "ready"
    _raw(observed)["behind"] = 21  # degraded only
    assert _verify(observed)[0] == "degraded"
    _loc(observed)["hook_modes"]["pre-push"] = "noexec"  # + a failed one
    _loc(observed)["hooks_active"] = False
    assert _verify(observed)[0] == "unhealthy"
    _loc(observed)["hook_modes"]["pre-push"] = "exec"
    _loc(observed)["hooks_active"] = True
    assert _verify(observed)[0] == "degraded"
    del _raw(observed)["behind"]
    assert _verify(observed)[0] == "unhealthy"  # a missing currency fact is never a pass


@pytest.mark.parametrize(
    "mutate",
    [lambda o: o.clear(), lambda o: o.pop(REPO_H), lambda o: o.update({REPO_H: "x"}), lambda o: o[REPO_H].pop("checkouts"), lambda o: o[REPO_H].update(checkouts=[])],
)
def test_verify_without_a_repo_observation_raises(setup, mutate):
    observed = _observed(setup[1])
    mutate(observed)
    with pytest.raises(ValueError, match="could not ask"):
        PH.VERIFY({"invariants": [{"id": PC1}]}, observed, "system")
    with pytest.raises(ValueError, match="could not ask"):
        PH.VERIFY({"invariants": [{"id": PC1}]}, None, "system")


def test_unknown_invariant_id_is_an_alarm(setup):
    observed = _observed(setup[1])
    out = PH.VERIFY({"invariants": [{"id": "PC-99-nope"}, "junk"]}, observed, "system")
    assert [r["status"] for r in out["results"]] == ["alarm", "alarm"] and out["state"] == "unhealthy"


def test_verify_evaluates_only_the_listed_invariants(setup):
    observed = _observed(setup[1])
    _loc(observed)["hook_modes"]["pre-push"] = "noexec"
    _loc(observed)["hooks_active"] = False
    state, by_id = _verify(observed, [PC1])
    assert list(by_id) == [PC1] and by_id[PC1]["status"] == "pass" and state == "ready"


def test_every_checkout_is_evaluated_and_named(setup):
    observed = _observed(setup[1])
    observed[REPO_H]["checkouts"]["second-ck"] = json.loads(json.dumps(_raw(observed)))
    observed[LOCAL_H]["checkouts"]["second-ck"] = json.loads(json.dumps(_loc(observed)))
    observed[LOCAL_H]["checkouts"]["second-ck"]["hook_modes"]["pre-commit"] = "noexec"
    observed[LOCAL_H]["checkouts"]["second-ck"]["hooks_active"] = False
    state, by_id = _verify(observed)
    assert by_id[PC3]["status"] == "fail" and "second-ck" in by_id[PC3]["detail"] and CK not in by_id[PC3]["detail"]


# --- the simulated observation's branch / tracking extension ------------------------------------------


def test_sim_observation_defaults_branch_to_tracking_and_supports_detached(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(head=B))
    handler = eng.backend.handler("repo.clone")
    raw = handler.observe(CID, "system")["checkouts"][CK]
    assert (raw["tracking"], raw["branch"]) == ("main", "main")
    _mutate(eng, "repo.clone", lambda s: s["checkouts"][CK].update(branch="HEAD"))
    assert handler.observe(CID, "system")["checkouts"][CK]["branch"] == "HEAD"
    _seed(eng.backend, _state(present=False))
    raw = handler.observe(CID, "system")["checkouts"][CK]
    assert raw["tracking"] == "main" and raw["branch"] is None


def test_the_planner_ignores_the_extra_observation_fields(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state())
    base = eng.plan("update", "system", CID, source_revs=(A, B)).digest
    _mutate(eng, "repo.clone", lambda s: s["checkouts"][CK].update(branch="feature"))
    assert eng.plan("update", "system", CID, source_revs=(A, B)).digest == base


def test_a_dumped_state_without_a_branch_still_loads(setup):
    _repo_, eng = setup
    state = _state(head=B)
    assert "branch" not in state["checkouts"][CK]
    _seed(eng.backend, state)
    assert eng.backend.handler("repo.clone").observe(CID, "system")["checkouts"][CK]["branch"] == "main"


# --- T-5107 D15 change A: core.hooksPath is healthy iff it resolves to THAT clone's own .githooks ----------------

TOP = "/nonexistent/sim/pantheon-config"  # the fixture checkout's target_path (virtual, never touched)
OWN_ABS = f"{TOP}/.githooks"
OTHER_ABS = "/home/other-seat/pantheon-config/.githooks"


def _pure_fs(symlinks=None, dirs=(), absent=()):
    """realpath/isdir callables for the pure helper, over the sim's own resolver."""

    symlinks, known = dict(symlinks or {}), {TOP, OWN_ABS, *dirs}
    return (lambda p: PH._sim_realpath(symlinks, p)), (lambda p: p in known and p not in set(absent))


def _own(value, **fs):
    realpath, isdir = _pure_fs(**fs)
    return PH.hooks_path_is_own(value, TOP, realpath, isdir)


def test_hooks_path_is_own_relative_githooks():
    assert _own(".githooks") == (True, "own")


def test_hooks_path_is_own_absolute_own():
    assert _own(OWN_ABS) == (True, "own")


def test_hooks_path_is_own_other_clones_githooks():
    own, reason = _own(OTHER_ABS)
    assert own is False and OTHER_ABS in reason and OWN_ABS in reason


def test_hooks_path_is_own_symlink_into_own():
    assert _own("/link", symlinks={"/link": OWN_ABS}) == (True, "own")


def test_hooks_path_is_own_symlink_elsewhere():
    own, reason = _own("/link", symlinks={"/link": "/outside"}, dirs=["/outside"])
    assert own is False and "/outside" in reason


def test_hooks_path_is_own_nonexistent():
    assert _own(OWN_ABS, absent=[OWN_ABS]) == (False, "not an existing directory")


def test_hooks_path_is_own_repo_githooks_symlinked_outside():
    fs = {"symlinks": {OWN_ABS: "/outside"}, "dirs": ["/outside"]}
    assert _own("/outside", **fs)[0] is False  # the outside dir is not made own by the link
    assert _own(".githooks", **fs)[0] is False  # nor is the relative form: hooks would run from outside the tree
    assert _own(OWN_ABS, **fs)[0] is False


@pytest.mark.parametrize("value", [None, "", 7, [".githooks"], b".githooks", True])
def test_hooks_path_is_own_non_string_or_empty(value):
    assert _own(value) == (False, "not a path")


def test_hooks_path_is_own_relative_with_dotdot_escape():
    own, reason = _own("../other/.githooks")
    assert own is False and "/nonexistent/sim/other/.githooks" in reason
    assert _own("./../pantheon-config/.githooks") == (True, "own")  # a dotdot that lands back inside is the same directory


def test_hooks_path_is_own_symlink_loop_is_never_own():
    assert _own("/a", symlinks={"/a": "/b", "/b": "/a"})[0] is False


def _local_row(eng):
    return eng.backend.handler("repo.hooks-activate").observe(CID, "system")["checkouts"][CK]


def test_local_observe_reports_hooks_path_own_tristate(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(head=B))
    seen = {}
    for label, path in (("rel", ".githooks"), ("abs", OWN_ABS), ("other", OTHER_ABS), ("unset", None)):
        _seed_local(eng.backend, hooks={CK: _hooks_entry(path=path)})
        row = _local_row(eng)
        seen[label] = (row["hooks_path_own"], row["hooks_path_detail"], row["hooks_active"])
    assert seen["rel"] == (True, "own", True) and seen["abs"] == (True, "own", True)
    assert seen["other"][0] is False and OTHER_ABS in seen["other"][1] and seen["other"][2] is False
    assert seen["unset"][0] is None and seen["unset"][2] is False
    _seed_local(eng.backend, hooks={CK: _hooks_entry(path=OWN_ABS, modes={"pre-commit": "exec", "commit-msg": "exec", "pre-push": "noexec"})})
    row = _local_row(eng)
    assert row["hooks_path_own"] is True and row["hooks_active"] is False  # own is necessary, not sufficient


def _stale_hooks_op(eng):
    """A hooks-activate op planned while the checkout was absent (the clone, then the hooks)."""

    _seed(eng.backend, _state(present=False))
    return _op_of(eng.plan("install", "system", CID), "repo.hooks-activate").to_install_operation()


def test_hooks_activate_preflight_refuses_own_absolute_stale_plan_and_never_overwrites(setup):
    _repo_, eng = setup
    op = _stale_hooks_op(eng)
    _seed(eng.backend, _state(head=B, installed=B))
    _seed_local(eng.backend, hooks={CK: _hooks_entry(path=OWN_ABS, engine=False, identity=B)})
    handler = eng.backend.handler("repo.hooks-activate")
    before = handler.dump_state()
    verdict = handler.preflight(op)
    assert not verdict.ok and f"already active as {OWN_ABS}; plan is stale" in verdict.detail
    assert handler.dump_state() == before and handler.observe(CID, "system")["checkouts"][CK]["hooks_path"] == OWN_ABS


def test_hooks_activate_preflight_unset_ok_and_foreign_refused(setup):
    _repo_, eng = setup
    op = _stale_hooks_op(eng)
    _seed(eng.backend, _state(head=B, installed=B))
    handler = eng.backend.handler("repo.hooks-activate")
    for path, ok in ((None, True), (".githooks", True), ("/elsewhere/hooks", False), (OTHER_ABS, False), ("other-hooks", False)):
        _seed_local(eng.backend, hooks={CK: _hooks_entry(path=path, identity=B)} if path is not None else {})
        verdict = handler.preflight(op)
        assert verdict.ok is ok, path
        if not ok:
            assert f"hooksPath set to {path}; refusing to overwrite" in verdict.detail


def test_hooks_activate_verify_accepts_own_absolute(setup):
    _repo_, eng = setup
    op = _stale_hooks_op(eng)
    _seed(eng.backend, _state(head=B, installed=B))
    handler = eng.backend.handler("repo.hooks-activate")
    for path, modes, ok in (
        (OWN_ABS, None, True), (".githooks", None, True), (OTHER_ABS, None, False), (None, None, False),
        (OWN_ABS, {"pre-commit": "exec", "commit-msg": "exec", "pre-push": "noexec"}, False),
    ):
        _seed_local(eng.backend, hooks={CK: _hooks_entry(path=path, modes=modes, identity=B)})
        assert handler.verify_operation(op).ok is ok, (path, modes)


def test_hooks_activate_rollback_owned_only_when_preimage_unset(setup):
    _repo_, eng = setup
    op = _stale_hooks_op(eng)
    _seed(eng.backend, _state(head=B, installed=B))
    handler = eng.backend.handler("repo.hooks-activate")
    # an absolute (hand-installed) preimage: the engine never owns what it rewrites
    _seed_local(eng.backend, hooks={CK: _hooks_entry(path=OWN_ABS, modes=MODES_OFF, identity=B)})
    mutation = handler.rollback_intent(op)
    assert mutation.before["hooks_path"] == OWN_ABS
    handler.execute(op)
    assert handler.can_rollback(mutation).classification == "drifted"
    # an unset preimage stays owned
    _seed_local(eng.backend, hooks={})
    mutation = handler.rollback_intent(op)
    handler.execute(op)
    assert handler.can_rollback(mutation).classification == "owned"


def test_install_adopt_attests_hand_install_with_absolute_own_path(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(head=B))
    _seed_local(eng.backend, hooks={CK: _hooks_entry(path=OWN_ABS)})
    plan = eng.plan("install", "system", CID)
    [op] = plan.nodes[0].operations
    assert op.kind == "pantheon-config.adopt-attest" and json.loads(op.desired["evidence"])["hooks_path"] == OWN_ABS
    [outcome] = eng.apply("install", "system", CID, plan_digest=plan.digest)
    assert outcome.state == "SATISFIED"
    assert _local(eng.backend)["attestations"][CK]["hooks_path"] == OWN_ABS  # the evidence keeps the RAW path
    _add_manifest(eng)
    assert eng.verify("system", CID)[0]["ok"]
    # a hand switch relative <-> absolute is evidence drift (PC-6), by design
    _mutate(eng, "repo.hooks-activate", lambda s: s["hooks"][CK].update(hooks_path=".githooks"))
    result, by_id, state = _inv(eng)
    assert _not_pass(by_id) == [PC6] and state == "degraded" and "hooks_path" in by_id[PC6]["detail"]


def test_install_refuses_other_clones_path_never_overwritten(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(head=B))
    _seed_local(eng.backend, hooks={CK: _hooks_entry(path=OTHER_ABS)})
    with pytest.raises(EngineRefusal, match=f"hooksPath set to {OTHER_ABS}; refusing to overwrite"):
        eng.plan("install", "system", CID)
    assert _local(eng.backend)["hooks"][CK]["hooks_path"] == OTHER_ABS


@pytest.mark.parametrize(
    "path, fs",
    [
        (OWN_ABS, {"absent": [OWN_ABS]}),
        ("/link", {"symlinks": {"/link": "/outside"}, "dirs": ["/outside"]}),
    ],
    ids=["nonexistent", "symlink-elsewhere"],
)
def test_install_refuses_nonexistent_and_symlink_elsewhere(setup, path, fs):
    _repo_, eng = setup
    _seed(eng.backend, _state(head=B))
    _seed_local(eng.backend, hooks={CK: _hooks_entry(path=path)}, fs=fs)
    with pytest.raises(EngineRefusal, match=f"hooksPath set to {path}; refusing to overwrite"):
        eng.plan("install", "system", CID)


def test_configure_plans_nothing_for_absolute_own_path_with_active_hooks(tmp_path):
    planner, request = _configure_request(tmp_path, hooks={CK: _hooks_entry(path=OWN_ABS, engine=True, identity=B)})
    assert request["observed"]["pantheon-config-local"]["checkouts"][CK]["hooks_path_own"] is True
    assert planner.plan_configure(request) == []


def test_configure_does_not_converge_absolute_to_relative(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(head=B, installed=B))
    _seed_local(eng.backend, hooks={CK: _hooks_entry(path=OWN_ABS, engine=True, identity=B)}, manifests={CK: _manifest(identity=B)})
    plan = eng.plan("configure", "system", CID)
    assert [op.kind for op in plan.nodes[0].operations] == ["pantheon-config.manifest-attest"]
    eng.apply("configure", "system", CID, plan_digest=plan.digest)
    assert _local(eng.backend)["hooks"][CK]["hooks_path"] == OWN_ABS
    assert eng.plan("configure", "system", CID).nodes[0].operations == ()


def test_configure_refuses_other_clones_path(tmp_path):
    planner, request = _configure_request(tmp_path, hooks={CK: _hooks_entry(path=OTHER_ABS, engine=True, identity=B)})
    with pytest.raises(planner.PlanRefused, match=f"hooksPath set to {OTHER_ABS}; refusing to overwrite"):
        planner.plan_configure(request)


def test_planner_fails_closed_when_hooks_path_own_fact_is_missing(tmp_path):
    planner, request = _configure_request(tmp_path)
    del request["observed"]["pantheon-config-local"]["checkouts"][CK]["hooks_path_own"]
    with pytest.raises(planner.PlanRefused, match="refusing to overwrite"):
        planner.plan_configure(request)
    planner, request = _install_request(tmp_path / "install")
    assert isinstance(planner.plan_install(request), list)
    del request["observed"]["pantheon-config-local"]["checkouts"][CK]["hooks_path_own"]
    with pytest.raises(planner.PlanRefused, match="refusing to overwrite"):
        planner.plan_install(request)


def test_pc3_passes_on_absolute_own_path(setup):
    _repo_, eng = setup
    _installed(eng)
    _mutate(eng, "repo.hooks-activate", lambda s: s["hooks"][CK].update(hooks_path=OWN_ABS))
    result, by_id, state = _inv(eng)
    assert _not_pass(by_id) == [] and state == "ready" and result["ok"]


def test_pc3_fails_on_other_clones_path(setup):
    detail = _breach(setup, "repo.hooks-activate", lambda s: s["hooks"][CK].update(hooks_path=OTHER_ABS), only=PC3, state="unhealthy")
    assert CK in detail and ".githooks" in detail and "not this clone's own" in detail and OTHER_ABS in detail


def test_pc3_symlink_into_own_passes(setup):
    _repo_, eng = setup
    _installed(eng)
    _mutate(eng, "repo.hooks-activate", lambda s: (s["hooks"][CK].update(hooks_path="/link"), s["fs"].update(symlinks={"/link": OWN_ABS})))
    result, by_id, state = _inv(eng)
    assert _not_pass(by_id) == [] and state == "ready"


def test_pc3_symlink_elsewhere_fails(setup):
    detail = _breach(
        setup, "repo.hooks-activate",
        lambda s: (s["hooks"][CK].update(hooks_path="/link"), s["fs"].update(symlinks={"/link": "/outside"}, dirs=["/outside"])),
        only=PC3, state="unhealthy",
    )
    assert "not this clone's own" in detail and "/outside" in detail


def test_pc3_nonexistent_own_path_fails(setup):
    detail = _breach(
        setup, "repo.hooks-activate",
        lambda s: (s["hooks"][CK].update(hooks_path=OWN_ABS), s["fs"].update(absent=[OWN_ABS])), only=PC3, state="unhealthy",
    )
    assert "not an existing directory" in detail and CK in detail


# --- T-5107 D15 change C: PENDING_PUSH age (PC-4) ------------------------------------------------------------------


def _ahead_raw(observed, age, *, ts=CLOCK0 - 7200):
    """An ahead-by-one tracking checkout whose oldest unpushed commit is ``age`` seconds old at observation."""

    _raw(observed).update(ahead=1, oldest_unpushed_commit_ts=ts, observed_at=ts + age)


def test_pc4_ahead_within_24h_passes(setup):
    observed = _observed(setup[1])
    _ahead_raw(observed, 3600)
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [] and state == "ready"


def test_pc4_ahead_exactly_86400_passes(setup):
    observed = _observed(setup[1])
    _ahead_raw(observed, 86400)
    assert _not_pass(_verify(observed)[1]) == []


def test_pc4_ahead_86401_is_degraded_pending_push(setup):
    observed = _observed(setup[1])
    _ahead_raw(observed, 86401)
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC4] and state == "degraded"
    assert f"{CK}: PENDING_PUSH: oldest unpushed commit 24h old (threshold 24h)" in by_id[PC4]["detail"]
    _ahead_raw(observed, 3 * 86400 + 5)
    assert "72h old" in _verify(observed)[1][PC4]["detail"]


@pytest.mark.parametrize(
    "update, drop",
    [
        ({}, "oldest_unpushed_commit_ts"), ({"oldest_unpushed_commit_ts": None}, None), ({}, "observed_at"), ({"observed_at": None}, None),
        ({"oldest_unpushed_commit_ts": "100"}, None), ({"observed_at": "200"}, None), ({"oldest_unpushed_commit_ts": True}, None),
        ({"observed_at": True}, None), ({"oldest_unpushed_commit_ts": 1.5}, None), ({"oldest_unpushed_commit_ts": -5}, None),
        ({"oldest_unpushed_commit_ts": 5000, "observed_at": 4000}, None),
    ],
    ids=["ts-absent", "ts-none", "clock-absent", "clock-none", "ts-str", "clock-str", "ts-bool", "clock-bool", "ts-float", "ts-negative", "negative-age"],
)
def test_pc4_ahead_age_unknown_is_degraded(setup, update, drop):
    observed = _observed(setup[1])
    _ahead_raw(observed, 60)
    _raw(observed).update(update)
    if drop:
        del _raw(observed)[drop]
    state, by_id = _verify(observed)
    assert _not_pass(by_id) == [PC4] and state == "degraded"
    assert f"{CK}: PENDING_PUSH age not observed (could not evaluate)" in by_id[PC4]["detail"]


def test_pc4_ahead_zero_needs_no_age_fact(setup):
    observed = _observed(setup[1])
    for key in ("oldest_unpushed_commit_ts", "observed_at"):
        _raw(observed).pop(key, None)
    assert _raw(observed)["ahead"] == 0
    assert _not_pass(_verify(observed)[1]) == []


def test_pc4_pending_push_ranks_after_off_main_and_drifted(setup):
    observed = _observed(setup[1])
    _ahead_raw(observed, 5 * 86400)
    _raw(observed).update(behind=25)
    assert "DRIFTED" in _verify(observed)[1][PC4]["detail"] and "PENDING_PUSH" not in _verify(observed)[1][PC4]["detail"]
    _raw(observed).update(branch="feature")
    detail = _verify(observed)[1][PC4]["detail"]
    assert "OFF_MAIN" in detail and "DRIFTED" not in detail and "PENDING_PUSH" not in detail


def test_sim_oldest_unpushed_is_min_over_ahead_commits(setup):
    _repo_, eng = setup
    _installed(eng)
    handler = eng.backend.handler("repo.clone")

    def seen():
        raw = handler.observe(CID, "system")["checkouts"][CK]
        return raw["ahead"], raw["oldest_unpushed_commit_ts"], raw["observed_at"]

    assert seen() == (0, None, None)  # nothing unpushed, no clock
    _mutate(eng, "repo.clone", lambda s: (s["checkouts"][CK].update(head=E, local_parents={D: B, E: D}), s.update(commit_ts={D: 100, E: 200}, clock=500)))
    assert seen() == (2, 100, 500)
    _mutate(eng, "repo.clone", lambda s: s.update(commit_ts={D: 300, E: 200}))
    assert seen() == (2, 200, 500)  # the minimum, whichever commit carries it
    _mutate(eng, "repo.clone", lambda s: s.update(commit_ts={E: 200}))
    assert seen() == (2, None, 500)  # any ahead commit without a timestamp: unknown
    _mutate(eng, "repo.clone", lambda s: (s.update(commit_ts={D: 100, E: 200}), s.pop("clock")))
    assert seen() == (2, 100, None)  # no clock: the observation time is unknown, never the real time
    _mutate(eng, "repo.clone", lambda s: (s["checkouts"][CK].update(head=B), s.update(clock=500)))
    assert seen() == (0, None, 500)


def test_planner_ignores_age_fields(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state())
    base = eng.plan("update", "system", CID, source_revs=(A, B)).digest
    _mutate(eng, "repo.clone", lambda s: s.update(commit_ts={A: 5}, clock=CLOCK0))
    assert eng.plan("update", "system", CID, source_revs=(A, B)).digest == base
    raw = eng.backend.handler("repo.clone").observe(CID, "system")["checkouts"][CK]
    assert "observed_at" in raw and "oldest_unpushed_commit_ts" in raw
    assert not {"observed_at", "oldest_unpushed_commit_ts"} & set(PH._OBS_FIELDS)  # the reconciler's probes select by _OBS_FIELDS
    planner_src = (COMPONENTS / "pantheon-config.py").read_text(encoding="utf-8")
    assert "oldest_unpushed_commit_ts" not in planner_src and "observed_at" not in planner_src


def test_pending_push_threshold_constant_pinned():
    assert PH.PENDING_PUSH_MAX_AGE_S == 86400
    text = (COMPONENTS / "pantheon-config.invariants.yaml").read_text(encoding="utf-8")
    assert "more than 24 hours old at observation time (PENDING_PUSH, degraded)" in text


# --- T-5107 audit: bytecode debris under a real seat's .githooks is never a hook, a missing hook, a foreign entry or a mode failure ---

DEBRIS_DIRS = [f"{OWN_ABS}/__pycache__"]
DEBRIS_MODES = {"__pycache__": "noexec", "stray.pyc": "missing", "hook.cpython-312.pyc": "noexec"}  # stray sim entries beside the three hooks


def test_debris_under_own_githooks_keeps_the_path_own_and_hooks_active(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(head=B))
    _seed_local(eng.backend, hooks={CK: _hooks_entry(path=OWN_ABS, modes={**MODES_OK, **DEBRIS_MODES})}, fs={"symlinks": {}, "dirs": DEBRIS_DIRS, "absent": []})
    row = _local_row(eng)
    assert (row["hooks_path_own"], row["hooks_path_detail"], row["hooks_active"]) == (True, "own", True)
    assert _own(".githooks", dirs=DEBRIS_DIRS) == (True, "own") and _own(OWN_ABS, dirs=DEBRIS_DIRS) == (True, "own")
    handler = eng.backend.handler("repo.hooks-activate")
    assert handler._active(CK, handler._hooks(CK)) is True


def test_debris_under_githooks_passes_pc3(setup):
    _repo_, eng = setup
    _installed(eng)
    _mutate(
        eng, "repo.hooks-activate",
        lambda s: (s["hooks"][CK].update(hooks_path=OWN_ABS, modes={**MODES_OK, **DEBRIS_MODES}), s["fs"].update(dirs=DEBRIS_DIRS)),
    )
    result, by_id, state = _inv(eng)
    assert _not_pass(by_id) == [] and state == "ready" and result["ok"]


def test_debris_does_not_change_install_or_configure_plans(tmp_path):
    fs = {"symlinks": {}, "dirs": DEBRIS_DIRS, "absent": []}
    hooks = {CK: _hooks_entry(path=OWN_ABS, modes={**MODES_OK, **DEBRIS_MODES}, engine=True, identity=B)}
    planner, request = _configure_request(tmp_path / "cfg", hooks=hooks, fs=fs)
    assert planner.plan_configure(request) == []
    planner, request = _configure_request(tmp_path / "inst", hooks={CK: dict(hooks[CK], engine=False)}, fs=fs)
    assert planner.plan_configure(request) == []
    planner, request = _install_request(tmp_path / "adopt")
    local = request["observed"]["pantheon-config-local"]["checkouts"][CK]
    assert local["hooks_path_own"] is True
    planner, request = _debris_install_request(tmp_path / "adopt2")
    [op] = planner.plan_install(request)
    assert op["kind"] == "pantheon-config.adopt-attest"


def _debris_install_request(tmp_path):
    """TEST HELPER: _install_request, but over a hand-installed seat whose own .githooks carries bytecode debris."""

    import importlib.util

    repo = _repo(tmp_path)
    eng = H.engine(tmp_path, repo)
    _seed(eng.backend, _state(head=B))
    _seed_local(eng.backend, hooks={CK: _hooks_entry(path=OWN_ABS, modes={**MODES_OK, **DEBRIS_MODES})}, fs={"symlinks": {}, "dirs": DEBRIS_DIRS, "absent": []})
    spec = importlib.util.spec_from_file_location("pc_planner_debris", repo / "tools" / "germinate" / "components" / "pantheon-config.py")
    planner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(planner)
    request = {
        "observed": {
            "pantheon-config-repo": eng.backend.handler("repo.clone").observe(CID, "system"),
            "pantheon-config-local": eng.backend.handler("repo.hooks-activate").observe(CID, "system"),
        }
    }
    return planner, request


def test_planner_hook_names_equal_the_handlers_and_stray_modes_never_refuse(tmp_path):
    planner, request = _configure_request(tmp_path, hooks={CK: _hooks_entry(modes={**MODES_OK, "stray.pyc": "missing", "__pycache__": "noexec"}, engine=True, identity=B)})
    assert planner.HOOK_NAMES == PH.HOOK_NAMES
    assert planner.plan_configure(request) == []
    # a real lost hook still refuses, with the stray entries absent from the message
    modes = {**MODES_OK, "stray.pyc": "missing", "pre-push": "missing"}
    planner, request = _configure_request(tmp_path / "lost", hooks={CK: _hooks_entry(modes=modes, engine=True, identity=B)})
    with pytest.raises(planner.PlanRefused, match=r"hooks missing: \['pre-push'\]"):
        planner.plan_configure(request)


# The planner narrows to HOOK_NAMES as defense-in-depth behind the handler's own narrowing (it matters once a live
# observer, T-6098, can hand it unnarrowed modes). Every test above builds its request through the handler, which
# already drops stray keys, so these inject the stray key into the request by hand to exercise the planner alone.

def _local_ck(request):
    return request["observed"]["pantheon-config-local"]["checkouts"][CK]


@pytest.mark.parametrize("key,mode", [("stray.pyc", "missing"), ("stray.pyc", "noexec"), ("__pycache__", "missing"), ("__pycache__", "noexec")])
def test_planner_configure_ignores_a_hand_injected_stray_mode(tmp_path, key, mode):
    planner, request = _configure_request(tmp_path)
    _local_ck(request)["hook_modes"][key] = mode
    assert planner.plan_configure(request) == []


def test_planner_configure_still_refuses_a_real_lost_hook_naming_only_it(tmp_path):
    planner, request = _configure_request(tmp_path)
    _local_ck(request)["hook_modes"].update({"stray.pyc": "missing", "pre-push": "missing"})
    with pytest.raises(planner.PlanRefused, match=r"hooks missing: \['pre-push'\];"):
        planner.plan_configure(request)
    planner, request = _configure_request(tmp_path / "noexec")
    _local_ck(request)["hook_modes"].update({"stray.pyc": "noexec", "pre-push": "noexec"})
    with pytest.raises(planner.PlanRefused, match=r"not executable under .*: \['pre-push'\];"):
        planner.plan_configure(request)


@pytest.mark.parametrize("key", ["stray.pyc", "__pycache__"])
def test_planner_install_refusal_names_the_real_hook_not_the_stray_key(tmp_path, key):
    planner, request = _install_request(tmp_path)
    local = _local_ck(request)
    local["hook_modes"].update({key: "noexec", "pre-push": "noexec"})
    local["hooks_active"] = False
    with pytest.raises(planner.PlanRefused) as exc:
        planner.plan_install(request)
    assert "not executable: ['pre-push']" in str(exc.value) and key not in str(exc.value)


@pytest.mark.parametrize("key,mode", [("stray.pyc", "missing"), ("__pycache__", "noexec")])
def test_planner_install_healthy_hand_install_with_a_stray_mode_still_adopts(tmp_path, key, mode):
    planner, request = _install_request(tmp_path)
    _local_ck(request)["hook_modes"][key] = mode
    [op] = planner.plan_install(request)
    assert op["kind"] == "pantheon-config.adopt-attest"


def test_a_changed_mode_on_a_named_hook_is_evidence_drift(setup):
    """Converse of the debris pin: a named hook's mode is attested evidence, so changing it shows as PC-6 drift."""

    _repo_, eng = setup
    _seed(eng.backend, _state(head=B, installed=B))
    _seed_local(eng.backend, hooks={CK: _hooks_entry(path=".githooks", modes=MODES_OK, engine=True, identity=B)}, attestations={CK: _attested(head=B, identity=B)})
    attested = _local_row(eng)["evidence"]["hook_modes"]
    _seed_local(eng.backend, hooks={CK: _hooks_entry(path=".githooks", modes={**MODES_OK, "pre-push": "noexec"}, engine=True, identity=B)}, attestations={CK: _attested(head=B, identity=B)})
    now = _local_row(eng)["evidence"]["hook_modes"]
    assert attested == MODES_OK and now == {**MODES_OK, "pre-push": "noexec"} and now != attested


def test_a_changed_mode_on_a_named_hook_is_rollback_drifted(setup):
    _repo_, eng = setup
    op = _stale_hooks_op(eng)
    _seed(eng.backend, _state(head=B, installed=B))
    handler = eng.backend.handler("repo.hooks-activate")
    _seed_local(eng.backend, hooks={CK: _hooks_entry(path=None, modes=MODES_OFF, identity=B)})
    mutation = handler.rollback_intent(op)
    handler.execute(op)
    _seed_local(eng.backend, hooks={CK: _hooks_entry(path=None, modes={**MODES_OK, "pre-push": "noexec"}, identity=B)})
    assert handler.can_rollback(mutation).classification == "drifted"


def test_debris_modes_never_reach_evidence_or_stale_an_attestation(setup):
    _repo_, eng = setup
    _seed(eng.backend, _state(head=B, installed=B))
    _seed_local(
        eng.backend,
        hooks={CK: _hooks_entry(path=".githooks", modes={**MODES_OK, **DEBRIS_MODES}, engine=True, identity=B)},
        attestations={CK: _attested(head=B, identity=B)},
    )
    row = _local_row(eng)
    assert row["hook_modes"] == MODES_OK and row["evidence"]["hook_modes"] == MODES_OK


def test_debris_modes_do_not_make_hooks_rollback_drifted(setup):
    _repo_, eng = setup
    op = _stale_hooks_op(eng)
    _seed(eng.backend, _state(head=B, installed=B))
    handler = eng.backend.handler("repo.hooks-activate")
    _seed_local(eng.backend, hooks={CK: _hooks_entry(path=None, modes={**MODES_OFF, **DEBRIS_MODES}, identity=B)})
    mutation = handler.rollback_intent(op)
    handler.execute(op)
    assert handler.verify_operation(op).ok and handler.can_rollback(mutation).classification == "owned"


def test_debris_appearing_after_the_activation_does_not_make_rollback_drifted(setup):
    """The recorded mutation is clean; stray entries show up in the CURRENT state afterwards (a hook module imported)."""

    _repo_, eng = setup
    op = _stale_hooks_op(eng)
    _seed(eng.backend, _state(head=B, installed=B))
    handler = eng.backend.handler("repo.hooks-activate")
    _seed_local(eng.backend, hooks={CK: _hooks_entry(path=None, modes=MODES_OFF, identity=B)})
    mutation = handler.rollback_intent(op)
    handler.execute(op)
    assert handler.can_rollback(mutation).classification == "owned"
    handler.state["hooks"][CK]["modes"].update(DEBRIS_MODES)
    assert handler.can_rollback(mutation).classification == "owned"


def test_debris_never_makes_a_missing_or_noexec_hook_look_fine(setup):
    """The converse pin: only the three named hooks decide; debris cannot mask a real lost hook."""

    _repo_, eng = setup
    _seed(eng.backend, _state(head=B))
    for bad in ("noexec", "missing"):
        modes = {**MODES_OK, **DEBRIS_MODES, "pre-push": bad}
        _seed_local(eng.backend, hooks={CK: _hooks_entry(path=OWN_ABS, modes=modes)}, fs={"symlinks": {}, "dirs": DEBRIS_DIRS, "absent": []})
        assert _local_row(eng)["hooks_active"] is False, bad
