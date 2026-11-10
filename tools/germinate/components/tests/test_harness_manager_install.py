"""T-5091 U1 (germination GF2): harness-manager's install leg.

``plan_install`` is an adapter over the LIVE installer's pure plan
(``PersonaInstaller.plan(..., source_revision=...)``); its ops run only through the
SIMULATED ``HarnessInstallSim`` (plus the engine's built-in InstallerFileHandler for
mkdir / atomic-write). Nothing here actuates a host.

The ``test_real_*`` tests read the real repo and real instance.yaml (read-only).
Engine-tier tests run on a tmp checkout over a simulated host, reusing the GF1
component test's fixture (its S3 in-scope stub dependencies included).

Merge-gate note (mason ruling, T-5091 U1): the pinned pre/post live-digest
comparison is merge evidence, not a committed fixture; the PERMANENT guard on the
installer's default path is the source_revision equivalence below.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import persona_installer as PI
from tools.germinate.engine.backend import InstallerFileHandler
from tools.germinate.engine.components import load_repo_op_registry, read_op_kinds
from tools.germinate.engine.contract import load_contract
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.operation import parse_operation
from tools.germinate.engine.planner import InProcessJsonPlannerRunner, PlannerGuard
from tools.germinate.engine.tests import helpers as H
from tools.germinate.planner_sandbox.admission import coordinator

from tools.germinate.components.tests import test_harness_manager_component as GF1

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPONENTS = REPO_ROOT / "tools" / "germinate" / "components"
PLANNER = COMPONENTS / "harness-manager.py"
HANDLERS = COMPONENTS / "harness-manager_handlers.py"
MANIFEST_PATH = REPO_ROOT / "instance.yaml"
CID = "harness-manager"
REV = "0123456789abcdef0123456789abcdef01234567"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HM = _load(PLANNER, "_t5091_harness_manager")
HMH = _load(HANDLERS, "_t5091_harness_manager_handlers")


def _manifest():
    return PI.Manifest.load(MANIFEST_PATH)


def _personas():
    roles = _manifest().roles
    return sorted(role["persona"] for role in roles.values() if isinstance(role, dict) and role.get("persona"))


PERSONAS = _personas()


def _account(persona: str) -> str:
    return _manifest().binding(persona).account


def _request(account: str, revision: str | None = REV, **desired):
    if revision is not None:
        desired["source_revision"] = revision
    return {"scope": f"seat-{account}", "desired": desired, "observed": {}}


# --- the installer seam: equivalence + refusal (PERMANENT default-path guard) ----


def test_real_every_seat_is_planned():
    assert len(PERSONAS) == 8 and {"orchestrator", "mason"} <= set(PERSONAS)


@pytest.mark.parametrize("bypass", (False, True), ids=("default", "maintenance-bypass"))
@pytest.mark.parametrize("persona", PERSONAS)
def test_real_source_revision_equivalence(persona, bypass):
    """plan(source_revision=_git_head(repo)) == plan(): the default path is the seam's
    identity, for every seat and digest-bound variant (the reconcile-keep-going
    workflow is a pure projection of this same plan in persona_installer_apply)."""

    installer = PI.PersonaInstaller()
    manifest = _manifest()
    extra = {"maintenance_bypass": True} if bypass else {}
    default = installer.plan(manifest, persona, **extra)
    explicit = installer.plan(manifest, persona, source_revision=PI._git_head(REPO_ROOT), **extra)
    assert explicit == default
    assert explicit.digest == default.digest


def test_real_reconcile_workflow_equivalence():
    """The reconcile-keep-going dry-run variant builds from the same plan() call."""

    from tools import persona_installer_apply as A

    for persona in PERSONAS:
        _i, live, _m = A.build_plan(MANIFEST_PATH, persona, workflow="reconcile-keep-going")
        assert all(op.operation_id for op in live.operations)
        base = PI.PersonaInstaller().plan(_manifest(), persona, source_revision=PI._git_head(REPO_ROOT))
        assert {op.operation_id for op in live.operations} <= {op.operation_id for op in base.operations}


@pytest.mark.parametrize(
    "bad",
    ("", "unversioned", "abc123", REV.upper(), REV + "0", REV[:-1], " " + REV[1:], REV[:-1] + "g"),
)
def test_malformed_source_revision_refuses(bad):
    with pytest.raises(PI.ManifestError, match="source_revision must be a full 40-hex commit id"):
        PI.PersonaInstaller().plan(_manifest(), "mason", source_revision=bad)


def test_explicit_revision_is_the_plan_revision():
    plan = PI.PersonaInstaller().plan(_manifest(), "mason", source_revision=REV)
    assert plan.provenance["source_revision"] == REV
    banners = [op.desired["content"] for op in plan.operations if "content" in op.desired and PI.MANAGED_MARKER in op.desired["content"]]
    assert banners and all(f"revision={REV}" in content for content in banners)


# --- purity under PlannerGuard -----------------------------------------------


def _preimport_lazy_modules():
    """plan() lazily imports mailbox_onboard / keep_going / persona_bringup; importing
    a module is not plan I/O, so load them before the guard window."""

    for name in ("tools.mailbox_onboard", "tools.keep_going", "tools.persona_bringup"):
        __import__(name)


@pytest.mark.parametrize("persona", PERSONAS)
def test_real_plan_is_pure_under_planner_guard(persona):
    """With an explicit revision, plan() makes no guard-refused call (no subprocess,
    write, socket, thread, sqlite) and opens/lists only checked-in repo files."""

    import sysconfig

    _preimport_lazy_modules()
    manifest = _manifest()
    # "checked-in" means tracked by git, captured BEFORE the guard window (the guard
    # refuses subprocesses). Tracked files plus their parent directories (listings).
    listed = subprocess.run(
        ("git", "ls-files", "-z"), cwd=REPO_ROOT, capture_output=True, check=True, timeout=120
    ).stdout.decode("utf-8").split("\0")
    tracked: set[Path] = set()
    for rel in filter(None, listed):
        path = (REPO_ROOT / rel).resolve()
        tracked.add(path)
        tracked.update(p for p in path.parents if p.is_relative_to(REPO_ROOT))
    assert len(tracked) > 100
    exempt = {
        Path(sysconfig.get_paths()[key]).resolve()
        for key in ("stdlib", "platstdlib", "purelib", "platlib")
    }
    reads: list[str] = []
    active: list[int | None] = [None]

    def recorder(event, args):
        if active[0] == threading.get_ident() and event in ("open", "os.listdir", "os.scandir", "glob.glob"):
            reads.append(str(args[0]) if args else "")

    sys.addaudithook(recorder)
    guard = PlannerGuard()
    with guard:
        active[0] = threading.get_ident()
        try:
            PI.PersonaInstaller().plan(manifest, persona, source_revision=REV)
        finally:
            active[0] = None
    assert guard.violations == []
    untracked = sorted(
        {
            r
            for r in reads
            if Path(r).resolve() not in tracked
            and not any(Path(r).resolve().is_relative_to(root) for root in exempt)
        }
    )
    assert untracked == []
    assert reads, "the recorder saw no reads at all (hook not live?)"


def test_real_default_plan_is_refused_by_the_guard():
    """Control: the default path's _git_head subprocess IS the guard-refused call."""

    _preimport_lazy_modules()
    guard = PlannerGuard()
    with guard:
        with pytest.raises(Exception):
            PI.PersonaInstaller().plan(_manifest(), "mason")
    assert any("subprocess.Popen" in v for v in guard.violations)


def test_real_plan_install_runs_under_the_json_planner_runner():
    ops = InProcessJsonPlannerRunner().run(PLANNER, "install", _request("suborch1"))
    assert ops and all(isinstance(op, dict) for op in ops)


_ENV_PROBE = r"""
import json, sys, hashlib
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from tools import persona_installer as PI
plan = PI.PersonaInstaller().plan(PI.Manifest.load(Path(sys.argv[1]) / "instance.yaml"), "mason", source_revision=sys.argv[2])
print(plan.digest)
"""


def test_real_plan_is_invariant_to_import_time_env(tmp_path):
    """mason ruling: KEEP_GOING_ADAPTIVE and HOME are read at lazy-import time; the
    plan bytes must not depend on them. Fresh interpreters, so the imports re-run."""

    base_env = {k: v for k, v in os.environ.items() if k not in ("KEEP_GOING_ADAPTIVE", "HOME")}
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    variants = {
        "unset": dict(base_env),
        "adaptive-0": dict(base_env, KEEP_GOING_ADAPTIVE="0"),
        "adaptive-1": dict(base_env, KEEP_GOING_ADAPTIVE="1"),
        "tmp-home": dict(base_env, HOME=str(fake_home)),
    }
    digests = {}
    for name, env in variants.items():
        out = subprocess.run(
            (sys.executable, "-c", _ENV_PROBE, str(REPO_ROOT), REV),
            env=env, capture_output=True, text=True, check=True, timeout=240,
        )
        digests[name] = out.stdout.strip()
    assert len(set(digests.values())) == 1, digests
    assert digests["unset"] == PI.PersonaInstaller().plan(_manifest(), "mason", source_revision=REV).digest


# --- plan_install op data ------------------------------------------------------


@pytest.mark.parametrize("persona", ("mason", "orchestrator"))
def test_real_plan_install_op_data_snapshot(persona):
    """Op-by-op against the installer's own plan (structural, so it survives
    legitimate template / manifest changes): 1:1, same order, identity kind,
    registry privilege, mapped crash contract, pass-through payload."""

    account = _account(persona)
    ops = HM.plan_install(_request(account))
    plan = PI.PersonaInstaller().plan(_manifest(), persona, source_revision=REV)
    assert len(ops) == len(plan.operations)
    for raw, item in zip(ops, plan.operations):
        kind, privilege, crash = HM._INSTALL_KIND_MAP[item.kind]
        assert raw["operation_id"] == item.operation_id.replace(".", "_")
        assert (raw["kind"], raw["privilege"], raw["crash_contract"]) == (kind, privilege, crash)
        assert raw["kind"] == item.kind
        assert raw["side_effect_free"] is (privilege == "none")
        assert raw["layer"] == item.layer and raw["target"] == item.target and raw["owner"] == item.owner == account
        assert raw["precondition"] == dict(item.precondition) and raw["desired"] == dict(item.desired)
        assert raw["inverse"] == (None if item.inverse is None else dict(item.inverse))
        assert raw["execution_scope"] == item.execution_scope
        prov = raw["provenance"]
        # every installer provenance key passes through UNCHANGED (never overwritten)
        assert {k: prov[k] for k in item.provenance} == dict(item.provenance)
        assert prov["installer_operation_id"] == item.operation_id
        assert prov["installer_privilege"] == item.privilege
        assert prov["plan_source_revision"] == REV
    first = ops[0]["provenance"]
    assert first["installer_plan_digest"] == plan.digest
    assert first["adapter_of"] == "tools.persona_installer.PersonaInstaller.plan"
    assert first["installer_operator_actions"] == str(len(plan.operator_actions))
    assert all("installer_plan_digest" not in raw["provenance"] for raw in ops[1:])
    by_kind = {raw["kind"] for raw in ops}
    assert {"identity-check", "mkdir", "user-unit-install", "daemon-reload"} <= by_kind


@pytest.mark.parametrize("persona", PERSONAS)
def test_real_plan_install_ops_parse_against_registry_and_contract(persona):
    registry = load_repo_op_registry(REPO_ROOT)
    contract = load_contract(COMPONENTS / "harness-manager.yaml", registry)
    ops = HM.plan_install(_request(_account(persona)))
    parsed = [parse_operation(raw, contract=contract, op_registry=registry) for raw in ops]
    assert len({op.operation_id for op in parsed}) == len(parsed)
    assert all(op.privilege in ("none", "seat") for op in parsed)  # root is U2 (host-substrate)


def test_plan_install_refuses_system_scope_and_missing_revision():
    with pytest.raises(HM.PlanRefused, match="seat scope"):
        HM.plan_install({"scope": "system", "desired": {"source_revision": REV}})
    for desired in ({}, {"source_revision": "unversioned"}, {"source_revision": REV.upper()}):
        with pytest.raises(HM.PlanRefused, match="source_revision"):
            HM.plan_install({"scope": "seat-suborch1", "desired": desired})
    with pytest.raises(HM.PlanRefused, match="no persona"):
        HM.plan_install(_request("nobody-here"))


def test_op_id_sanitising_and_collision_refusal():
    assert HM._engine_op_id("connective.config-dir") == "connective_config-dir"
    long_id = "x." * 50
    engine_id = HM._engine_op_id(long_id)
    assert len(engine_id) == 64 and engine_id.startswith("x_x_") and engine_id[47] == "-"
    assert HM._engine_op_id(long_id + "y") != engine_id

    def item(op_id):
        return SimpleNamespace(
            operation_id=op_id, kind="mkdir", target="/t", owner="a", precondition={}, desired={},
            inverse=None, layer=1, provenance={}, privilege="privileged-helper", execution_scope="all",
        )

    fake = SimpleNamespace(digest="d", operator_actions=(), operations=(item("a.b"), item("a_b")))
    with pytest.raises(HM.PlanRefused, match="collide"):
        HM._install_ops(fake, REV)
    unmapped = SimpleNamespace(**dict(vars(item("z")), kind="not-a-kind"))
    with pytest.raises(HM.PlanRefused, match="no engine mapping"):
        HM._install_ops(SimpleNamespace(digest="d", operator_actions=(), operations=(unmapped,)), REV)


# --- plan_configure stays byte-identical (the _op keyword defaults) --------------

#: sha256 of canonical plan_configure output, captured from the BASE planner
#: (c252d71656, before the _op keyword extension) for the two request shapes.
_CONFIGURE_SNAPSHOT = {
    "telegram": (5, "be7307f39cf72caf8a089fc10a4d92bb3b2b95f17155f2f50a09b6a1d3881680"),
    "no-telegram": (3, "ffe8805e214325e86507a6fba1085a7970bcf68f9222d0460c9d408c5ed33da3"),
}


@pytest.mark.parametrize("shape", sorted(_CONFIGURE_SNAPSHOT))
def test_plan_configure_output_is_byte_identical_to_base(shape):
    seat = GF1._seat() if shape == "telegram" else GF1._seat(communications=None)
    request = {"scope": "seat-suborch1", "desired": {"harness": "codex"}, "observed": {"harness-manager-seat": {"account": "suborch1", "seat": seat}}}
    ops = HM.plan_configure(json.loads(json.dumps(request)))
    count, digest = _CONFIGURE_SNAPSHOT[shape]
    assert (len(ops), hashlib.sha256(json.dumps(ops, sort_keys=True).encode()).hexdigest()) == (count, digest)


# --- mapping coverage (property) --------------------------------------------------


def test_real_every_installer_kind_has_exactly_one_mapping_and_one_handler():
    seen = set()
    for persona in PERSONAS:
        for bypass in (False, True):
            plan = PI.PersonaInstaller().plan(_manifest(), persona, source_revision=REV, maintenance_bypass=bypass)
            seen |= {op.kind for op in plan.operations}
    mapped = set(HM._INSTALL_KIND_MAP)
    assert seen <= mapped
    registry = load_repo_op_registry(REPO_ROOT)
    for kind, (engine_kind, privilege, crash) in HM._INSTALL_KIND_MAP.items():
        entry = registry.get(engine_kind)
        assert entry is not None and engine_kind == kind
        assert entry.privilege == privilege
        assert (crash is None) == entry.read_only
    sim_kinds = HMH.HarnessInstallSim.kinds
    builtin = InstallerFileHandler.kinds
    assert not (sim_kinds & builtin)
    assert sim_kinds | builtin == mapped
    assert HMH._INSTALL_READ_ONLY_KINDS == {k for k, v in HM._INSTALL_KIND_MAP.items() if v[1] == "none"}


def test_real_op_kinds_literal_is_unchanged():
    """ORG-080 D-AC / mason ruling: identity mapping, so the install leg adds nothing to
    OP_KINDS. (T-5830 later added sub-op (a)'s isolated harness.supervisor block; it is
    pinned in test_harness_manager_supervisor; T-5858 added configure run_state's isolated
    harness.run-state block, pinned in test_harness_manager_run_state; T-5859 added the remove
    leg's isolated harness.decommission block, pinned in test_harness_manager_remove.)"""

    kinds = read_op_kinds(CID, HANDLERS)
    others = ("harness.supervisor", "harness.run-state", "harness.decommission")
    assert {k: v for k, v in kinds.items() if v[0] not in others} == {
        "harness.transition-stage": ("harness.runtime", "seat"),
        "harness.receiver-handoff": ("harness.runtime", "seat"),
        "harness.runtime-replace": ("harness.runtime", "seat"),
        "harness.transition-commit": ("harness.runtime", "seat"),
    }
    assert not set(kinds) & set(HM._INSTALL_KIND_MAP)


# --- structural pins ------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_planner_imports_no_runtime_controller_and_no_live_backend():
    names = _imports(PLANNER)
    assert "tools.persona_installer.PersonaInstaller" in names
    banned_modules = ("tools.runtime_controller", "tools.persona_installer_apply", "subprocess", "tools.germinate.host_substrate")
    for name in names:
        assert not any(name == b or name.startswith(b + ".") for b in banned_modules), name
        assert not name.endswith("HostBackend"), name


def test_live_actuation_stays_false_and_no_new_flag():
    from tools.germinate.host_substrate import ports

    assert ports.LIVE_ACTUATION_ENABLED is False
    for path in (PLANNER, HANDLERS):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assigned = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name)
        }
        assert not any(name.startswith("LIVE_ACTUATION") for name in assigned), path


# --- simulated execution: unit tier ---------------------------------------------


def _install_op(kind="unit-fire", op_id="unit-fire_mason", owner="suborch1", desired=None):
    return PI.InstallOperation(
        operation_id=op_id, layer=1, kind=kind, target="pantheon-x.service", precondition={"state": "must-pass"},
        desired=desired or {"active": "active"}, inverse=None, privilege="unprivileged", owner=owner,
        provenance={}, execution_scope="all",
    )


def test_sim_apply_verify_rollback_round_trip_unit():
    sim = HMH.HarnessInstallSim()
    sim.add_seat("suborch1")
    op = _install_op()
    assert sim.preflight(op).ok
    intent = sim.rollback_intent(op)
    assert sim.can_rollback(intent).classification == "not-applied"
    mutation, evidence = sim.execute(op)
    assert evidence.ok and sim.verify_operation(op).ok
    assert sim.can_rollback(mutation).classification == "owned"
    assert sim.preflight(op).ok  # idempotent re-entry
    sim.rollback_mutation(mutation)
    assert sim.dump_state()["seats"]["suborch1"]["slots"] == {}
    assert not sim.verify_operation(op).ok


def test_sim_drifted_slot_refuses_rollback_and_preflight():
    sim = HMH.HarnessInstallSim()
    sim.add_seat("suborch1")
    op = _install_op()
    mutation, _ = sim.execute(op)
    state = sim.dump_state()
    state["seats"]["suborch1"]["slots"][op.operation_id] = {"kind": op.kind, "target": op.target, "desired": {"active": "someone-else"}}
    sim.load_state(state)
    check = sim.can_rollback(mutation)
    assert (check.ok, check.classification) == (False, "drifted")
    assert not sim.preflight(op).ok


def test_sim_read_only_kinds_write_nothing_and_read_facts():
    sim = HMH.HarnessInstallSim()
    sim.add_seat("suborch1", facts_default=True)
    check = _install_op(kind="identity-check", op_id="identity_check")
    mutation, evidence = sim.execute(check)
    assert evidence.ok and mutation.changed is False
    assert sim.dump_state()["seats"]["suborch1"]["slots"] == {}
    state = sim.dump_state()
    state["seats"]["suborch1"]["facts"][check.target] = False
    sim.load_state(state)
    assert not sim.preflight(check).ok and not sim.verify_operation(check).ok


def test_sim_unknown_seat_refuses():
    assert not HMH.HarnessInstallSim().preflight(_install_op()).ok


# --- simulated execution: engine tier (tmp checkout, simulated host) -------------


def _install_engine(tmp_path):
    repo = GF1._repo(tmp_path)
    eng = GF1._engine(tmp_path, repo)
    eng.backend.handler("unit-fire").add_seat(GF1.ACCT)
    eng.backend.save()
    return repo, eng


def _install_state(eng):
    return {
        "install": eng.backend.handler("unit-fire").dump_state(),
        "files": eng.backend.handler("mkdir").dump_state()["entries"],
    }


def _plan_install(eng):
    return eng.plan("install", GF1.SCOPE, CID, desired={"source_revision": REV})


def test_engine_install_plans_every_seat_op(tmp_path):
    _repo, eng = _install_engine(tmp_path)
    plan = _plan_install(eng)
    [node] = plan.nodes
    assert [op.operation_id for op in node.operations] == [raw["operation_id"] for raw in HM.plan_install(_request(GF1.ACCT))]


def test_engine_install_real_plan_fits_the_plan_bound(tmp_path):
    """T-5809 (#1444): a full-seat install node is over the 64 KiB generic artifact cap
    but under MAX_PLAN_BYTES, so the UNPATCHED engine stores and reads it."""
    from tools.germinate.engine.run_state import MAX_PLAN_BYTES, encode_plan
    from tools.germinate.planner_sandbox.wal.artifacts import MAX_ARTIFACT_BYTES

    _repo, eng = _install_engine(tmp_path)
    [node] = _plan_install(eng).nodes
    size = len(encode_plan(node.to_json_obj()))
    assert MAX_ARTIFACT_BYTES < size <= MAX_PLAN_BYTES


def test_engine_install_apply_then_verify(tmp_path):
    _repo, eng = _install_engine(tmp_path)
    plan = _plan_install(eng)
    [node] = plan.nodes
    assert len(node.operations) == len(HM.plan_install(_request(GF1.ACCT)))
    [outcome] = eng.apply("install", GF1.SCOPE, CID, plan_digest=plan.digest, desired={"source_revision": REV})
    assert outcome.state == "SATISFIED"
    slots = eng.backend.handler("unit-fire").dump_state()["seats"][GF1.ACCT]["slots"]
    mutating = [op for op in node.operations if not op.side_effect_free and op.kind not in InstallerFileHandler.kinds]
    assert set(slots) == {op.operation_id for op in mutating}
    [result] = eng.verify(GF1.SCOPE, CID)
    assert all(c["ok"] for c in result["checks"])


def test_engine_install_failure_halts_and_rollback_restores_preimage(tmp_path):
    _repo, eng = _install_engine(tmp_path)
    preimage = _install_state(eng)
    plan = _plan_install(eng)
    ops = plan.nodes[0].operations
    victim = [op for op in ops if op.kind == "daemon-reload"][-1]
    eng.backend.faults.fail.add(victim.operation_id)
    with pytest.raises(EngineRefusal) as info:
        eng.apply("install", GF1.SCOPE, CID, plan_digest=plan.digest, desired={"source_revision": REV})
    assert info.value.code == "halted"
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    restored = _install_state(eng)
    assert restored["install"] == preimage["install"]
    # the built-in InstallerFileHandler (mkdir / atomic-write) rolls back too
    assert restored["files"] == preimage["files"]


def test_engine_install_rollback_refuses_a_drifted_slot(tmp_path):
    _repo, eng = _install_engine(tmp_path)
    plan = _plan_install(eng)
    ops = plan.nodes[0].operations
    victim = [op for op in ops if op.kind == "daemon-reload"][-1]
    eng.backend.faults.fail.add(victim.operation_id)
    with pytest.raises(EngineRefusal):
        eng.apply("install", GF1.SCOPE, CID, plan_digest=plan.digest, desired={"source_revision": REV})
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    handler = eng.backend.handler("unit-fire")
    state = handler.dump_state()
    slots = state["seats"][GF1.ACCT]["slots"]
    moved_id = next(iter(slots))
    slots[moved_id] = {"kind": "drift", "target": "drift", "desired": {}}
    handler.load_state(state)
    eng.backend.save()
    with pytest.raises(EngineRefusal) as info:
        eng.rollback(run_id)
    assert info.value.code == "rollback-incomplete"
    store, run_tuple = eng._find_run(run_id)
    with store.open(run_tuple) as handle:
        record = handle.read_record()
    assert moved_id in record["rollback_incomplete"]["outstanding_inverses"]
