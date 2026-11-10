"""T-5947 criterion (T-5092): harness-manager's dependency planning succeeds in sim
against the REAL registry rows and component files once its dependencies are SATISFIED.

The checkout is a tmp copy of this repo's component files, instance.yaml, checkouts/,
tools/systemd/ and registry/systems.yaml, unmodified. Every dependency is the real
component over its simulated handler: host-substrate (real install), and mailbox,
credential-broker and action-broker (observe-and-attest over a healthy legacy setup
seeded by the labelled TEST HELPERs below; T-5948 replaced the former action-broker
stub). Nothing here touches a live host.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.tests import helpers as H
from tools.germinate.engine.tests.test_preflight import HEAD, write_observation
from tools.germinate.engine.run_state import RunStore

from tools.germinate.components.tests import test_action_broker_component as AB
from tools.germinate.components.tests import test_credential_broker_component as CB
from tools.germinate.components.tests import test_harness_manager_component as GF1
from tools.germinate.components.tests import test_harness_manager_preflight as PFT
from tools.germinate.components.tests import test_harness_manager_run_state as RS
from tools.germinate.components.tests import test_mailbox_component as MB

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPONENTS = REPO_ROOT / "tools" / "germinate" / "components"
HM = "harness-manager"
DEPS = ("host-substrate", "mailbox", "credential-broker")  # T-6167 PR-3: action-broker is no install dependency
ROOT_PASSWD = {"passwd:root": {"uid": 0, "gid": 0, "home": "/root", "shell": "/bin/bash", "locked": False}}


def _registry(repo: Path) -> dict:
    doc = yaml.safe_load((REPO_ROOT / "registry" / "systems.yaml").read_text(encoding="utf-8"))
    rows = {row["id"]: row for row in doc["systems"]}
    assert rows["action-broker"]["contract"]["digest"] != "unpinned"  # the real T-5948 component, no stub
    return doc


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    directory = repo / "tools" / "germinate" / "components"
    directory.mkdir(parents=True)
    for path in COMPONENTS.iterdir():
        if path.is_file():
            shutil.copy(path, directory / path.name)
    shutil.copy(REPO_ROOT / "instance.yaml", repo / "instance.yaml")
    shutil.copytree(REPO_ROOT / "checkouts", repo / "checkouts")
    shutil.copytree(REPO_ROOT / "tools" / "systemd", repo / "tools" / "systemd")
    H.make_checkout(repo, _registry(repo)["systems"])
    return repo


def _install(eng, cid):
    plan = eng.plan("install", "system", cid)
    gates = frozenset({AB.GATE}) if cid == AB.CID else frozenset()  # action-broker's operator gate
    [outcome] = eng.apply("install", "system", cid, plan_digest=plan.digest, accept_gates=gates)
    assert outcome.state == "SATISFIED", cid
    return plan


def _build(tmp_path, mailbox_seed=MB._seed_running):
    repo = _repo(tmp_path)
    eng = H.engine(tmp_path, repo)
    eng.backend.handler("account.ensure").load_state({"subjects": dict(ROOT_PASSWD)})  # TEST HELPER: a listed passwd
    MB._write(eng, mailbox_seed)  # TEST HELPER: the (running, or degraded) mailbox organ
    CB._write(eng, CB._seed_refs)  # TEST HELPER: present credential reference metadata
    GF1._seed(eng.backend, GF1._seat())  # TEST HELPER: the simulated seat harness-manager configures
    AB._seed(eng.backend, AB._receipt(), AB._verify())  # TEST HELPER: a healthy legacy T-2920 install
    eng.backend.save()
    return eng


@pytest.fixture
def engine(tmp_path):
    return _build(tmp_path)


def test_harness_manager_plans_refuse_until_every_dependency_is_satisfied_then_succeed(engine):
    eng = engine
    for dep in DEPS:
        with pytest.raises(EngineRefusal) as info:
            GF1._configure(eng)
        assert info.value.code == "dependency-not-installed" and dep in info.value.message
        plan = _install(eng, dep)
        if dep in ("mailbox", "credential-broker"):
            [op] = plan.nodes[0].operations
            assert op.kind == f"{dep}.adopt-attest"
            assert op.provenance["basis"] == "observed-legacy"
    # the dependency gate (executor _plan, FORWARD_LEGS) is leg-independent: the
    # harness-runtime swap stands for install / configure / run_state here (the
    # install leg's full installer plan is exercised in test_harness_manager_install)
    plan = GF1._configure(eng)
    [node] = plan.nodes
    assert node.component == HM and node.operations
    assert all(eng._satisfied_install(dep, "system") for dep in DEPS)


def test_pinned_dependency_behaviour_is_unchanged(engine):
    """Differential: satisfaction is the engine's unchanged rule (a SATISFIED install run),
    whatever the dependency's install basis; a dependency never installed still refuses."""

    eng = engine
    for dep in ("host-substrate", "credential-broker"):
        _install(eng, dep)
    with pytest.raises(EngineRefusal) as info:
        GF1._configure(eng)
    assert info.value.code == "dependency-not-installed" and "mailbox" in info.value.message


# --- T-5947 acceptance: harness-manager's legs plan in sim against the real registry ----------


def _seed_degraded_mailbox(state):
    MB._seed_running(state)
    state["maildirs"][MB.PERSONAS[0]]["mode"] = "700"  # the legacy verify reads healthy; the planner refuses it


@pytest.fixture(scope="module")
def attested(tmp_path_factory):
    """One engine whose four dependencies are all SATISFIED; the tests below only plan or
    preflight on it (neither mutates run state), so it is shared to keep the setup cost down."""

    eng = _build(tmp_path_factory.mktemp("t5947"))
    plans = {dep: _install(eng, dep) for dep in DEPS}
    return eng, plans


def test_install_leg_plans_once_the_dependencies_are_attested(attested):
    eng, _plans = attested
    plan = eng.plan("install", GF1.SCOPE, HM, desired={"source_revision": RS.REV})
    [node] = plan.nodes
    assert node.component == HM and node.operations


def test_run_state_leg_plans_once_the_dependencies_are_attested(attested):
    eng, _plans = attested
    eng.backend.handler("harness.run-state-unit").load_state({"seats": {GF1.ACCT: RS._seat("mason")}})  # TEST HELPER
    eng.backend.save()
    plan = eng.plan("configure", GF1.SCOPE, HM, desired=RS._desired())
    [node] = plan.nodes
    assert node.component == HM and node.operations
    assert {op.kind for op in node.operations} >= {"harness.run-state-manifest", "harness.run-state-unit"}


def test_preflight_clears_the_plan_and_component_checks_over_attested_dependencies(attested):
    """Not GO: the grants check fails by design (an unrelated, un-supplied grant). What T-5947
    pins is that the dependency gate no longer stops the plan, and the component checks run."""

    eng, _plans = attested
    write_observation(eng.backend.root, user=GF1.ACCT, head=HEAD, probe=PFT._probe_doc(GF1.ACCT))
    receipt = eng.preflight("configure", GF1.SCOPE, HM, desired={"harness": "codex"})
    checks = {c["id"]: c for c in receipt["checks"]}
    for cid in ("plan", "plan-digest", "component"):
        assert checks[cid]["status"] == "pass", checks[cid]
    assert all(c.get("reason") != "dependency-not-installed" for c in receipt["checks"])


def test_brownfield_dependencies_are_visible_as_attestations_in_their_install_records(attested):
    """The attest basis is already traceable in the existing run-record fields: the op id,
    the committed receipt reference and the plan op kind. host-substrate is a real install."""

    eng, plans = attested
    for dep in DEPS:
        [record] = [rec for tup, rec in eng._runs(dep) if tup.leg == "install"]
        plan_ops = plans[dep].nodes[0].operations
        if dep == "host-substrate":
            assert plan_ops and not any(o.kind.endswith(".adopt-attest") for o in plan_ops)
            assert not any(o["op_id"].startswith("adopt-attest-") for o in record["operations"])
            continue
        [plan_op] = plan_ops
        assert plan_op.kind == f"{dep}.adopt-attest"
        assert plan_op.provenance["basis"] == "observed-legacy"
        [op] = record["operations"]
        assert op["op_id"] == f"adopt-attest-{dep}"
        [receipt] = [r for r in record["receipts"] if r["op_id"] == op["op_id"]]
        assert receipt["receipt_ref"]
        store = RunStore(eng.backend.run_state_root("system"))
        with store.open(store.find(record["tuple"]["run_id"])) as handle:  # the receipt really resolves
            assert handle.wal().read_receipt_by_ref(receipt["receipt_ref"])


def test_a_degraded_brownfield_dependency_gets_no_record_and_blocks_harness_manager(tmp_path):
    """Differential (T-5947 Ask): SATISFIED is reachable only through a healthy attest. A 0700
    maildir refuses mailbox's own install plan, so no install record exists, and every
    harness-manager leg (and its preflight, the real-dependency NO-GO variant) stays blocked."""

    eng = _build(tmp_path, _seed_degraded_mailbox)
    for dep in ("host-substrate", "credential-broker"):
        _install(eng, dep)
    with pytest.raises(EngineRefusal, match="maildir-mode-degraded") as info:
        eng.plan("install", "system", "mailbox")
    assert info.value.code == "plan-refused"
    assert not [t for t, _r in eng._runs("mailbox")] and not eng._satisfied_install("mailbox", "system")
    for leg, desired in (("configure", {"harness": "codex"}), ("install", {"source_revision": RS.REV})):
        with pytest.raises(EngineRefusal) as info:
            eng.plan(leg, GF1.SCOPE, HM, desired=desired)
        assert info.value.code == "dependency-not-installed" and "mailbox" in info.value.message
    write_observation(eng.backend.root, user=GF1.ACCT, head=HEAD, probe=PFT._probe_doc(GF1.ACCT))
    receipt = eng.preflight("configure", GF1.SCOPE, HM, desired={"harness": "codex"})
    checks = {c["id"]: c for c in receipt["checks"]}
    assert receipt["verdict"] == "NO-GO" and checks["plan"]["reason"] == "dependency-not-installed"
    assert checks["component"]["status"] == "skip"


# --- T-6167 PR-3: the action broker serves running seats, not seat setup -----------------------


def test_contract_does_not_depend_on_action_broker():
    contract = yaml.safe_load((COMPONENTS / "harness-manager.yaml").read_text(encoding="utf-8"))["component"]
    assert contract["depends_on"] == ["host-substrate", "mailbox", "credential-broker"]


def test_seat_install_plans_without_an_action_broker_satisfied_record(tmp_path):
    """Unit + Integ-same-process: the three real dependencies SATISFIED, action-broker never
    installed (no run record at all); harness-manager's seat install and configure still plan."""

    eng = _build(tmp_path)
    for dep in DEPS:
        _install(eng, dep)
    assert not list(eng._runs("action-broker")) and not eng._satisfied_install("action-broker", "system")
    plan = eng.plan("install", GF1.SCOPE, HM, desired={"source_revision": RS.REV})
    [node] = plan.nodes
    assert node.component == HM and node.operations
    assert GF1._configure(eng).nodes[0].component == HM


@pytest.mark.parametrize("missing", DEPS)
def test_seat_install_still_refuses_when_a_remaining_dependency_is_not_installed(tmp_path, missing):
    eng = _build(tmp_path)
    for dep in DEPS:
        if dep != missing and missing != "host-substrate":  # mailbox itself needs host-substrate; nothing else installs without it
            _install(eng, dep)
    with pytest.raises(EngineRefusal) as info:
        eng.plan("install", GF1.SCOPE, HM, desired={"source_revision": RS.REV})
    assert info.value.code == "dependency-not-installed" and missing in info.value.message
