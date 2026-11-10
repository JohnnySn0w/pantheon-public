"""T-5090 (germination GF1): the harness-manager component's unprivileged half.

verify's one invariant list (ORG-080 A-6), configure sub-op (b) (the harness-runtime
swap) over the SIMULATED handler, and the PersonaController1 emitter binding.

Every test runs on tmp_path stand-ins: a temporary checkout holding copies of the
component files and a fixture registry, over a simulated host. Only the
``test_real_*`` tests read the real repo (read-only). ``_seed`` is a TEST HELPER: it
writes simulated seat state into the simulated host; no lifecycle verb does that.

WORKAROUND (T-5090 S3, cross-scope-dependency seam gap): the engine requires each
dependency to have a SATISFIED install in the SAME scope, so the fixture registry
carries in-scope STUB dependency components (host-substrate, mailbox,
action-broker, credential-broker) with empty seat-scope installs. They stand in
for system-scope components; the cross-scope seam has since closed (T-5135 S3), and
test_harness_manager_dependencies exercises the real components (T-5947).
"""

from __future__ import annotations

import ast
import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tools.germinate.engine.backend import KILL_POINTS, SimulatedKill
from tools.germinate.engine.contract import ContractError, load_contract
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.operation import operation_from_json_obj
from tools.germinate.engine.tests import helpers as H
from tools.germinate.planner_sandbox.admission import coordinator
from tools.germinate.registry.contract_digest import compute_contract_digest
from tools.harness_adapter import integration

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPONENTS = REPO_ROOT / "tools" / "germinate" / "components"
FILES = (
    "harness-manager.yaml",
    "harness-manager.py",
    "harness-manager_handlers.py",
    "harness-manager.invariants.yaml",
    "harness_manager_verify.py",
    "harness_manager_supervisor.py",  # T-5830: a helper the handlers import (digest-pinned)
    "harness_manager_run_state.py",  # T-5858: a helper the handlers import (digest-pinned)
    "harness_manager_remove.py",  # T-5859: a helper the handlers import (digest-pinned)
    "harness_manager_preflight.py",  # T-5921: the PREFLIGHT hook's checks (digest-pinned)
)
CID = "harness-manager"
DEPS = ("host-substrate", "mailbox", "action-broker", "credential-broker")
ACCT = "suborch1"
SCOPE = f"seat-{ACCT}"
CLAUDE, CODEX = "claude/interactive-subscription", "codex/interactive-chatgpt"
KINDS = ("harness.transition-stage", "harness.receiver-handoff", "harness.runtime-replace", "harness.receiver-handoff", "harness.transition-commit")


def _verify_module():
    from tools.germinate.components import harness_manager_verify

    return harness_manager_verify


def _seat(**overrides):
    seat = {
        "persona": "mason",
        "role": "engineering-orchestrator-1",
        "runtime": {"selected": CLAUDE},
        "receiver": {"holder": "claude"},
        "transition": None,
        "holds": {},
        "harness_runtime_role": {"primary": CLAUDE, "fallbacks": [CODEX], "profiles": {"claude": "interactive-subscription", "codex": "interactive-chatgpt"}},
        "communications": {"kind": "telegram", "watchdog_units": ["claude-watchdog.timer"], "claude_receiver_topology": "systemd-user"},
        "readiness": {CLAUDE: True, CODEX: True},
    }
    seat.update(overrides)
    return seat


def _restamp(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    body = yaml.safe_load(text)["component"]
    path.write_text(text.replace(body["contract_digest"], "SHA256_DIGEST_PLACEHOLDER"), encoding="utf-8")
    digest = compute_contract_digest(path)
    path.write_text(path.read_text(encoding="utf-8").replace("SHA256_DIGEST_PLACEHOLDER", digest), encoding="utf-8")
    return digest


def _repo(
    tmp_path: Path,
    *,
    gate_class: str = "independent-agent",
    stubs: bool = True,
    dependency_scopes: tuple[str, ...] = ("seat",),
) -> Path:
    """A tmp checkout. ``gate_class='operator'`` is the LABELLED test copy whose
    authority-transfer gate is operator-class, so the crash contracts can be driven
    through apply (the real contract's independent-agent gate refuses: S2)."""

    repo = tmp_path / "repo"
    directory = repo / "tools" / "germinate" / "components"
    directory.mkdir(parents=True)
    for name in FILES:
        shutil.copy(COMPONENTS / name, directory / name)
    contract = directory / "harness-manager.yaml"
    if gate_class != "independent-agent":
        text = contract.read_text(encoding="utf-8")
        text = text.replace("class: independent-agent\n        grant_source: action-broker-approval-registry\n", f"class: {gate_class}\n")
        contract.write_text(text, encoding="utf-8")
    digest = _restamp(contract)
    rows = [H.row(CID, digest)]
    if stubs:
        for dep in DEPS:  # S3 WORKAROUND: in-scope stub dependencies (module docstring)
            _p, dep_digest = H.write_contract(repo, dep, ops={"install": []}, scopes=list(dependency_scopes))
            rows.append(H.row(dep, dep_digest))
    H.make_checkout(repo, rows)
    return repo


def _seed(backend, seat) -> None:
    """TEST HELPER: write simulated seat state into the simulated host."""

    backend.handler("harness.runtime-replace").load_state({"seats": {ACCT: seat}})
    backend.save()


def _seat_state(backend):
    return backend.handler("harness.runtime-replace").dump_state()["seats"][ACCT]


def _install_deps(eng, scope: str = SCOPE) -> None:
    for dep in DEPS:
        plan = eng.plan("install", scope, dep)
        [outcome] = eng.apply("install", scope, dep, plan_digest=plan.digest)
        assert outcome.state == "SATISFIED"


def _engine(tmp_path, repo, seat=None, *, deps=True):
    eng = H.engine(tmp_path, repo)
    _seed(eng.backend, seat if seat is not None else _seat())
    if deps:
        _install_deps(eng)
    return eng


@pytest.fixture
def real(tmp_path):
    repo = _repo(tmp_path)
    return repo, _engine(tmp_path, repo)


@pytest.fixture
def gated(tmp_path):
    repo = _repo(tmp_path, gate_class="operator")
    return repo, _engine(tmp_path, repo)


def _configure(eng, harness="codex"):
    return eng.plan("configure", SCOPE, CID, desired={"harness": harness})


# --- contract, registry, discovery ---------------------------------------------


def test_real_contract_loads_and_registry_row_validates():
    from tools.germinate.registry.validate import errors, validate_repo

    assert errors(validate_repo(REPO_ROOT)) == []
    doc = yaml.safe_load((REPO_ROOT / "registry" / "systems.yaml").read_text(encoding="utf-8"))
    [row] = [r for r in doc["systems"] if r.get("id") == CID]
    assert row["contract"]["digest"] == compute_contract_digest(COMPONENTS / "harness-manager.yaml")
    # every GF2 leg still runs only through simulated handlers, so the row stays not_adopted.
    assert row["contract_adoption"] == "not_adopted"
    body = yaml.safe_load((COMPONENTS / "harness-manager.yaml").read_text(encoding="utf-8"))["component"]
    assert body["scopes"] == ["system", "seat"]
    assert set(body["legs"]) == {"install", "configure", "remove", "verify"}  # install: T-5091 U1; remove: U3 (T-5859)


def test_planner_imports_leaf_modules_not_runtime_controller():
    tree = ast.parse((COMPONENTS / "harness-manager.py").read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert "tools.harness_state" in imports
    assert "tools.harness_adapter.integration" in imports
    assert not any(name == "tools.runtime_controller" or name.startswith("tools.runtime_controller.") for name in imports)


def test_real_registry_row_fails_without_the_component_files(tmp_path):
    from tools.germinate.registry.validate import check_contracts, errors

    doc = yaml.safe_load((REPO_ROOT / "registry" / "systems.yaml").read_text(encoding="utf-8"))
    msgs = "\n".join(str(f) for f in errors(check_contracts(doc, tmp_path)))
    assert "harness-manager.contract" in msgs


def test_list_digest_is_pinned_in_the_contract():
    """S4 stopgap: nothing in the engine or validator recomputes invariant_list_digest."""

    V = _verify_module()
    body = yaml.safe_load((COMPONENTS / "harness-manager.yaml").read_text(encoding="utf-8"))["component"]
    digest = V.list_digest(COMPONENTS / "harness-manager.invariants.yaml")
    assert body["invariant_list_id"] == V.LIST_ID
    assert body["invariant_list_digest"] == digest
    ref = f"{V.LIST_ID}@{digest}"
    assert body["legs"]["verify"]["invariant_list_ref"] == ref == body["emitter"]["invariant_list_ref"]


def test_editing_the_list_breaks_the_pin(tmp_path):
    V = _verify_module()
    copy_path = tmp_path / "list.yaml"
    doc = yaml.safe_load((COMPONENTS / "harness-manager.invariants.yaml").read_text(encoding="utf-8"))
    doc["invariant_list"]["invariants"][0]["on_fail"] = "unhealthy"
    copy_path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert V.list_digest(copy_path) != V.list_digest(COMPONENTS / "harness-manager.invariants.yaml")


@pytest.mark.parametrize("block", ["emitter", "verify"])
def test_a_drifted_invariant_ref_refuses_at_load(tmp_path, real, block):
    repo, eng = real
    contract = repo / "tools" / "germinate" / "components" / "harness-manager.yaml"
    text = contract.read_text(encoding="utf-8")
    ref = yaml.safe_load(text)["component"]["emitter"]["invariant_list_ref"]
    head, sep, tail = text.partition(f"invariant_list_ref: {ref}")
    if block == "emitter":
        tail = tail.replace(f"invariant_list_ref: {ref}", f"invariant_list_ref: {ref[:-1]}0")
    else:
        sep = f"invariant_list_ref: {ref[:-1]}0"
    contract.write_text(head + sep + tail, encoding="utf-8")
    _restamp(contract)
    with pytest.raises(ContractError, match="drifts"):
        load_contract(contract, eng.backend.op_registry)


def test_discovery_registers_the_simulated_handler_and_kinds(real):
    _repo_, eng = real
    for kind in set(KINDS):
        entry = eng.backend.op_registry.get(kind)
        assert (entry.capability_class, entry.privilege, entry.read_only, entry.substrate, entry.broker_admitted) == (
            "harness.runtime", "seat", False, False, False,
        )
        assert eng.backend.handles(kind)


# --- the one invariant list -----------------------------------------------------


def test_list_is_v1_to_v19_plus_every_hard_axis_and_monitor_invariants():
    V = _verify_module()
    doc = V.load_list()
    assert doc["invariant_list"]["status"] == "CURRENT" and doc["invariant_list"]["co_author"] == "recovery"
    entries = {e["id"]: e for e in doc["invariant_list"]["invariants"]}
    axes = yaml.safe_load((REPO_ROOT / "sop" / "standard" / "seat-parity" / "axes.yaml").read_text(encoding="utf-8"))["axes"]
    hard = {a["axis_id"]: a for a in axes if a["tier"] == "HARD"}
    monitors = {
        "ORG-080/hook-symlink-allowlist-parity",
        "ORG-080/configured-owner-observed-manager",
    }
    expected = {f"ORG-079/V{n}" for n in range(1, 20)} | {f"ORG-099/{a}" for a in hard} | monitors
    assert set(entries) == expected
    for axis_id, axis in hard.items():
        entry = entries[f"ORG-099/{axis_id}"]
        assert entry["class"] == "recorded-probe" and entry["on_fail"] == "unhealthy"
        assert entry["applies_to"] == ("all" if axis["applies_to"] == "all-seats" else {"roles": axis["applies_to"]})
    assert entries["ORG-079/V11"]["on_fail"] == "unhealthy"
    hook = entries["ORG-080/hook-symlink-allowlist-parity"]
    assert hook["class"] == "hook-allowlist-parity"
    assert hook["applies_to"] == "all" and hook["on_fail"] == "degraded"
    assert hook["machine_check"] == "ambiguous"
    assert hook["surfaces"] == ["verify", "emitter"]
    assert hook["unwired_seams"] == ["T-5135", "T-5137"]
    assert hook["source"] == "architecture/adr/ORG-080-germination-engine.md#6-health-plane-seam"
    assert hook["text"] == "Hook symlink and allowlist parity hold on both verify and emitter surfaces"
    owner = entries["ORG-080/configured-owner-observed-manager"]
    assert owner["class"] == "owner-manager-match"
    assert owner["applies_to"] == "all" and owner["on_fail"] == "unhealthy"
    assert owner["machine_check"] == "ambiguous"
    assert owner["surfaces"] == ["verify", "emitter"]
    assert owner["unwired_seams"] == ["T-5135", "T-5137"]
    assert owner["source"] == "architecture/adr/ORG-080-germination-engine.md#6-health-plane-seam"
    assert owner["text"] == "Configured owner matches the observed manager; mismatch is reported distinctly"
    assert doc["invariant_list"]["co_author_review"] == "approved"


CTX = {"adapter": "claude", "role": "engineering-orchestrator-1", "systemd_runtime": True}
PASS_FACTS = {
    "presence-mode": {"present": True, "owner": "suborch1", "mode": "0700", "expected_owner": "suborch1", "expected_mode": "0700", "symlink": False},
    "schema": {"document": {"mcpServers": {}}, "required_keys": ["mcpServers"]},
    "set-compare": {"actual": ["a.timer", "b.service"], "expected": ["b.service", "a.timer"]},
    "receipt-bound": {"evidence_sha256": "ab" * 32, "receipt_sha256": "ab" * 32},
    "live-compare": {"actual_grants": ["group:mailbox"], "declared": ["group:mailbox", "group:x"], "current_session": True},
    "trace": {"chain": ["hook", "handoff_and_restart.sh"], "resolves": True},
    "recorded-probe": {"verdict": "PASS"},
    "hook-allowlist-parity": {"hook_symlink_matches": True, "allowlist_matches": True},
    "owner-manager-match": {"configured_owner": "orchestrator", "observed_manager": "orchestrator"},
}
FAIL_FACTS = {
    "presence-mode": {"present": True, "owner": "root", "mode": "0700", "expected_owner": "suborch1", "expected_mode": "0700", "symlink": False},
    "schema": {"document": {}, "required_keys": ["mcpServers"]},
    "set-compare": {"actual": ["a.timer", "extra.service"], "expected": ["a.timer"]},
    "receipt-bound": {"evidence_sha256": "ab" * 32, "receipt_sha256": "cd" * 32},
    "live-compare": {"actual_grants": ["sudo:ALL"], "declared": ["group:mailbox"], "current_session": True},
    "trace": {"chain": ["hook"], "resolves": False},
    "recorded-probe": {"verdict": "FAIL"},
    "hook-allowlist-parity": {"hook_symlink_matches": False, "allowlist_matches": False},
    "owner-manager-match": {"configured_owner": "orchestrator", "observed_manager": "recovery"},
}


def _entry(cls, **extra):
    entry = {"id": f"X/{cls}", "class": cls, "applies_to": "all", "on_fail": "degraded", "machine_check": "defined"}
    entry.update(extra)
    return entry


@pytest.mark.parametrize("cls", sorted(PASS_FACTS))
def test_verify_passes_and_fails_per_class(cls):
    V = _verify_module()
    entry = _entry(cls)
    ok = V.evaluate_one(entry, {"context": CTX, "facts": {entry["id"]: PASS_FACTS[cls]}})
    bad = V.evaluate_one(entry, {"context": CTX, "facts": {entry["id"]: FAIL_FACTS[cls]}})
    assert (ok["status"], bad["status"]) == ("pass", "fail"), (ok, bad)


@pytest.mark.parametrize("cls", sorted(PASS_FACTS))
def test_could_not_ask_is_an_alarm_never_a_pass(cls):
    V = _verify_module()
    entry = _entry(cls)
    assert V.evaluate_one(entry, {"context": CTX, "facts": {}})["status"] == "alarm"
    assert V.evaluate_one(entry, {"context": CTX, "facts": {entry["id"]: {"unrelated": 1}}})["status"] == "alarm"
    assert V.evaluate_one(entry, {"facts": {entry["id"]: PASS_FACTS[cls]}})["status"] == "alarm"


def test_unmeasurable_probe_and_symlink_and_stale_cordon():
    V = _verify_module()
    probe = _entry("recorded-probe")
    assert V.evaluate_one(probe, {"context": CTX, "facts": {probe["id"]: {"verdict": "UNMEASURABLE"}}})["status"] == "alarm"
    root = _entry("presence-mode", no_symlink=True)
    fact = dict(PASS_FACTS["presence-mode"], symlink=True)
    assert V.evaluate_one(root, {"context": CTX, "facts": {root["id"]: fact}})["status"] == "fail"
    cordon = _entry("live-compare")
    stale = dict(PASS_FACTS["live-compare"], current_session=False)
    assert V.evaluate_one(cordon, {"context": CTX, "facts": {cordon["id"]: stale}})["status"] == "fail"


@pytest.mark.parametrize(
    "applies, context, expected",
    [
        ({"adapters": ["claude"]}, dict(CTX, adapter="codex"), "vacuous"),
        ({"adapters": ["claude"]}, CTX, "pass"),
        ({"systemd_runtime": True}, dict(CTX, systemd_runtime=False), "vacuous"),
        ({"roles": ["peer-daemon-codex"]}, CTX, "vacuous"),
        ({"roles": ["engineering-orchestrator-1"]}, CTX, "pass"),
    ],
)
def test_applicability_is_adapter_role_and_systemd_qualified(applies, context, expected):
    V = _verify_module()
    entry = _entry("recorded-probe", applies_to=applies)
    assert V.evaluate_one(entry, {"context": context, "facts": {entry["id"]: {"verdict": "PASS"}}})["status"] == expected


def test_carve_outs_pass_only_when_verified():
    V = _verify_module()
    entry = _entry("recorded-probe")
    obs = {"context": CTX, "facts": {entry["id"]: {"verdict": "FAIL"}}}
    verified = dict(obs, carve_outs={entry["id"]: {"verdict": "intentional-absence", "verified": True}})
    unverified = dict(obs, carve_outs={entry["id"]: {"verdict": "intentional-absence", "verified": False}})
    assert V.evaluate_one(entry, verified)["status"] == "pass"
    assert V.evaluate_one(entry, unverified)["status"] == "alarm"


def _all_pass_observation(doc):
    facts = {e["id"]: PASS_FACTS[e["class"]] for e in doc["invariant_list"]["invariants"]}
    return {"context": CTX, "facts": facts}


def test_the_real_list_evaluates_and_rolls_up():
    V = _verify_module()
    doc = V.load_list()
    obs = _all_pass_observation(doc)
    results = V.evaluate(doc, obs)
    assert {r["status"] for r in results} <= {"pass", "vacuous"}
    assert V.rollup(results, obs)["state"] == "ready"
    drift = copy.deepcopy(obs)
    drift["facts"]["ORG-079/V2"] = FAIL_FACTS["presence-mode"]
    assert V.rollup(V.evaluate(doc, drift), drift)["state"] == "degraded"
    cordon = copy.deepcopy(obs)
    cordon["facts"]["ORG-079/V11"] = FAIL_FACTS["live-compare"]
    rolled = V.rollup(V.evaluate(doc, cordon), cordon)
    assert (rolled["state"], rolled["security_drift"]) == ("unhealthy", True)
    hard = copy.deepcopy(obs)
    del hard["facts"]["ORG-099/mcp-python-package-pinned"]  # could not ask -> alarm -> HARD -> unhealthy
    assert V.rollup(V.evaluate(doc, hard), hard)["state"] == "unhealthy"
    muted = dict(drift, suppressed=True)
    rolled = V.rollup(V.evaluate(doc, muted), muted)
    assert rolled["state"] == "suppressed" and "ORG-079/V2" in rolled["reasons"]  # mute != blind
    assert V.rollup(V.evaluate(doc, {}), {})["state"] == "not_configured"
    assert set(V.STATES) == set(yaml.safe_load((COMPONENTS / "harness-manager.yaml").read_text())["component"]["emitter"]["reports"]["states"])


def test_monitor_failures_are_distinct_and_missing_or_malformed_never_pass():
    V = _verify_module()
    doc = V.load_list()
    ids = {
        "ORG-080/hook-symlink-allowlist-parity": "hook symlink parity failed; allowlist parity failed",
        "ORG-080/configured-owner-observed-manager": "configured owner 'orchestrator' != observed manager 'recovery'",
    }
    obs = _all_pass_observation(doc)
    entries = {entry["id"]: entry for entry in doc["invariant_list"]["invariants"]}
    for iid in ids:
        obs["facts"][iid] = FAIL_FACTS[entries[iid]["class"]]
    results = {result["id"]: result for result in V.evaluate(doc, obs)}
    assert {iid: (results[iid]["status"], results[iid]["detail"]) for iid in ids} == {
        iid: ("fail", detail) for iid, detail in ids.items()
    }
    rolled = V.rollup(list(results.values()), obs)
    assert rolled["state"] == "unhealthy"
    assert {iid: rolled["reasons"][iid] for iid in ids} == ids

    for iid in ids:
        missing = copy.deepcopy(obs)
        del missing["facts"][iid]
        missing_result = {result["id"]: result for result in V.evaluate(doc, missing)}[iid]
        assert missing_result["status"] == "alarm"
        malformed = copy.deepcopy(obs)
        malformed["facts"][iid] = {"unrelated": 1}
        malformed_result = {result["id"]: result for result in V.evaluate(doc, malformed)}[iid]
        assert malformed_result["status"] == "alarm"


def test_a_malformed_list_is_refused():
    V = _verify_module()
    doc = V.load_list()
    bad = copy.deepcopy(doc)
    bad["invariant_list"]["invariants"][0]["class"] = "vibes"
    with pytest.raises(V.InvariantListError):
        V.evaluate(bad, {})
    dup = copy.deepcopy(doc)
    dup["invariant_list"]["invariants"].append(dup["invariant_list"]["invariants"][0])
    with pytest.raises(V.InvariantListError):
        V.check_list(dup)


# --- configure sub-op (b): planning ----------------------------------------------


def test_configure_plans_the_swap_in_the_existing_code_order(real):
    _repo_, eng = real
    [node] = _configure(eng).nodes
    ops = node.operations
    assert tuple(op.kind for op in ops) == KINDS
    assert [op.crash_contract for op in ops] == ["stageable", "write-ahead-inverse-durable", "write-ahead-inverse-durable", "write-ahead-inverse-durable", "stageable"]
    assert [op.gate for op in ops] == [None, "authority-transfer", None, None, None]
    assert all(op.privilege == "seat" and op.owner == ACCT and not op.side_effect_free for op in ops)
    stage, pre, runtime, post, commit = ops
    assert runtime.precondition == {"selected": CLAUDE} and runtime.desired == {"selected": CODEX}
    binding = SimpleNamespace(communications=_seat()["communications"])
    assert pre.desired["actions"] == ",".join(integration.receiver_handoff_actions(binding, "claude-to-codex", "pre"))
    assert post.desired["actions"] == ",".join(integration.receiver_handoff_actions(binding, "claude-to-codex", "post"))
    assert (pre.precondition, post.desired["holder"]) == ({"holder": "claude"}, "codex")
    assert stage.desired["phase"] == "preparing" and commit.desired["phase"] == "awaiting-successor-read"
    assert stage.desired["transition_id"] == commit.desired["transition_id"]
    assert "runtime_controller" in runtime.provenance["adapter_of"]


def test_configure_without_a_telegram_receiver_gates_the_runtime_replace(tmp_path):
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, repo, _seat(communications=None, receiver={"holder": None}))
    [node] = _configure(eng).nodes
    assert [op.kind for op in node.operations] == ["harness.transition-stage", "harness.runtime-replace", "harness.transition-commit"]
    assert node.operations[1].gate == "authority-transfer"


def test_already_active_destination_plans_nothing(tmp_path):
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, repo, _seat(runtime={"selected": CODEX}, receiver={"holder": "codex"}))
    [node] = _configure(eng).nodes
    assert node.operations == ()


@pytest.mark.parametrize(
    "seat, match",
    [
        (
            _seat(runtime={"selected": CODEX}, receiver={"holder": "claude"}),
            "active runtime receiver is held by 'claude', not target harness 'codex'",
        ),
        (
            _seat(runtime={"selected": CODEX}, receiver={"holder": "codex"}, readiness={CLAUDE: True}),
            "active runtime liveness was not positively proven",
        ),
    ],
)
def test_already_active_destination_refuses_drifted_or_missing_evidence(tmp_path, seat, match):
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, repo, seat)
    with pytest.raises(EngineRefusal, match=match) as info:
        _configure(eng)
    assert info.value.code == "plan-refused"


@pytest.mark.parametrize(
    "seat, match",
    [
        (_seat(transition={"phase": "compensation-failed"}), "compensation failed"),
        (_seat(holds={CODEX: {"reason": "quota"}}), "operator-held"),
        (_seat(harness_runtime_role={"primary": CLAUDE, "fallbacks": [], "profiles": {"claude": "interactive-subscription", "codex": "interactive-chatgpt"}}), "not the primary or a configured fallback"),
        (_seat(transition={"phase": "preparing", "transition_id": "t-x"}), "already preparing"),
        (_seat(receiver={"holder": "codex"}), "receiver is held by"),
        (_seat(harness_runtime_role={"primary": CLAUDE, "fallbacks": [CODEX], "profiles": {"claude": "interactive-subscription"}}), "declares no codex profile"),
    ],
)
def test_existing_swap_preconditions_are_plan_refusals(tmp_path, seat, match):
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, repo, seat)
    with pytest.raises(EngineRefusal, match=match) as info:
        _configure(eng)
    assert info.value.code == "plan-refused"


def test_desired_harness_is_the_closed_enum(real):
    _repo_, eng = real
    with pytest.raises(EngineRefusal) as info:
        _configure(eng, "hermes")
    assert info.value.code == "inputs-refused"
    with pytest.raises(EngineRefusal) as info:
        eng.plan("configure", SCOPE, CID, desired={"supervisor": "systemd"})  # sub-op (a): closed enum (T-5830)
    assert info.value.code == "inputs-refused"
    with pytest.raises(EngineRefusal) as info:
        eng.plan("configure", SCOPE, CID, desired={"not_declared": "x"})
    assert info.value.code == "inputs-refused"


def test_configure_refuses_until_dependencies_are_installed(tmp_path):
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, repo, deps=False)
    with pytest.raises(EngineRefusal) as info:
        _configure(eng)
    assert info.value.code == "dependency-not-installed"


# --- absent / deferred legs and the dark-landing gate -----------------------------


@pytest.mark.parametrize("leg", ["update", "repair"])  # install landed in T-5091 U1, remove in U3 (T-5859)
def test_deferred_legs_refuse_cleanly(real, leg):
    _repo_, eng = real
    with pytest.raises(EngineRefusal) as info:
        eng.plan(leg, SCOPE, CID)
    assert info.value.code == "leg-not-declared"


def test_system_scope_is_refused(tmp_path):
    repo = _repo(tmp_path, dependency_scopes=("system", "seat"))
    eng = _engine(tmp_path, repo, deps=False)
    _install_deps(eng, "system")
    with pytest.raises(EngineRefusal) as info:
        eng.plan("configure", "system", CID, desired={"harness": "codex"})
    assert info.value.code == "plan-refused"
    assert "configure (b) requires a seat scope" in info.value.message


def test_real_contract_swap_refuses_grant_required_before_any_mutation(real):
    """S2: the authority-transfer gate is independent-agent; grant consumption is
    deferred, so the real swap stays dark."""

    _repo_, eng = real
    plan = _configure(eng)
    before = H.tree_snapshot(eng.backend.root)
    seat_before = _seat_state(eng.backend)
    for accept in (None, frozenset(), frozenset({"authority-transfer"})):
        with pytest.raises(EngineRefusal) as info:
            eng.apply("configure", SCOPE, CID, plan_digest=plan.digest, accept_gates=accept, desired={"harness": "codex"})
        assert info.value.code == "grant-required"
    assert H.tree_snapshot(eng.backend.root) == before
    assert _seat_state(eng.backend) == seat_before
    assert coordinator.read_claim(eng.backend.gates_root, "sim", CID) is None


# --- configure sub-op (b): apply over the labelled operator-gate copy -------------


def _apply(eng, plan, accept=frozenset()):
    return eng.apply("configure", SCOPE, CID, plan_digest=plan.digest, accept_gates=accept, desired={"harness": "codex"})


def test_operator_copy_needs_the_gate_answered(gated):
    _repo_, eng = gated
    plan = _configure(eng)
    with pytest.raises(EngineRefusal) as info:
        _apply(eng, plan, None)
    assert info.value.code == "gate-unanswered"


def test_swap_applies_end_to_end_and_verify_rechecks_receipts(gated):
    _repo_, eng = gated
    plan = _configure(eng)
    [outcome] = _apply(eng, plan)
    assert outcome.state == "SATISFIED"
    seat = _seat_state(eng.backend)
    assert seat["runtime"] == {"selected": CODEX}
    assert seat["receiver"]["holder"] == "codex"
    assert seat["transition"]["phase"] == "awaiting-successor-read"
    # T-5135 S1: verify also evaluates the ONE invariant list. The seat sim carries no
    # observation yet, so every invariant alarms (could not ask) and the rollup is
    # not_configured; drift is clean but the seat is not ok. Alarm is never a pass.
    [result] = eng.verify(SCOPE, CID)
    assert len(result["checks"]) == 5 and all(c["ok"] for c in result["checks"])
    assert not result["ok"] and result["invariants"]["state"] == "not_configured"
    assert {r["status"] for r in result["invariants"]["results"]} == {"alarm"}
    # With an all-pass observation (harness_manager_verify's shape) the same verify is ok.
    doc = _verify_module().load_list()
    seat["verify_observation"] = _all_pass_observation(doc)
    _seed(eng.backend, seat)
    [result] = eng.verify(SCOPE, CID)
    assert result["ok"] and result["invariants"]["state"] == "ready"
    # One failing fact flips the seat to not ok and names the invariant.
    seat["verify_observation"]["facts"]["ORG-079/V2"] = FAIL_FACTS["presence-mode"]
    _seed(eng.backend, seat)
    [result] = eng.verify(SCOPE, CID)
    assert not result["ok"]
    assert [r["id"] for r in result["invariants"]["results"] if r["status"] == "fail"] == ["ORG-079/V2"]


@pytest.mark.parametrize(
    "readiness, match",
    [
        ({CODEX: False}, "not input-ready"),
        ({}, "readiness proof is missing or not true"),
        ({CODEX: "yes"}, "readiness proof is missing or not true"),
    ],
    ids=("false", "missing", "malformed"),
)
def test_readiness_failure_halts_and_rollback_restores_every_slot(gated, readiness, match):
    _repo_, eng = gated
    seat = _seat(readiness=readiness)
    _seed(eng.backend, seat)
    preimage = _seat_state(eng.backend)
    plan = _configure(eng)
    with pytest.raises(EngineRefusal) as info:
        _apply(eng, plan)
    assert info.value.code == "halted" and match in info.value.message
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    assert _seat_state(eng.backend) == preimage


def test_rollback_refuses_a_slot_that_moved(gated):
    _repo_, eng = gated
    _seed(eng.backend, _seat(readiness={CODEX: False}))
    plan = _configure(eng)
    with pytest.raises(EngineRefusal):
        _apply(eng, plan)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    state = eng.backend.handler("harness.runtime-replace").dump_state()
    state["seats"][ACCT]["receiver"] = {"holder": "claude", "last_handoff": "someone-else"}
    eng.backend.handler("harness.runtime-replace").load_state(state)
    eng.backend.save()
    store, run_tuple = eng._find_run(run_id)
    with store.open(run_tuple) as handle:
        plan_doc = handle.read_plan()
        receiver_pre = next(
            operation_from_json_obj(raw)
            for raw in plan_doc["node"]["operations"]
            if raw["kind"] == "harness.receiver-handoff" and raw["desired"]["phase"] == "pre"
        )
        mutation = eng._mutation_for_rollback(handle.wal(), receiver_pre)
    check = eng.backend.can_rollback(mutation)
    assert (check.ok, check.classification, check.detail) == (
        False,
        "drifted",
        "receiver moved since this op",
    )
    with pytest.raises(EngineRefusal) as info:
        eng.rollback(run_id)
    assert info.value.code == "rollback-incomplete"
    with store.open(run_tuple) as handle:
        failed = [json.loads(line) for line in handle.read_transcript() if json.loads(line)["event"] == "op.rollback.failed"]
        record = handle.read_record()
    assert failed[-1]["op_id"] == receiver_pre.operation_id
    assert failed[-1]["reason"] == "OperationFailed: cannot roll back (drifted): receiver moved since this op"
    assert receiver_pre.operation_id in record["rollback_incomplete"]["outstanding_inverses"]


@pytest.mark.parametrize("index", range(len(KINDS)))
@pytest.mark.parametrize("point", KILL_POINTS)
def test_kill_then_reconcile_then_rollback_restores_preimage(tmp_path, index, point):
    repo = _repo(tmp_path, gate_class="operator")
    eng = _engine(tmp_path, repo)
    preimage = _seat_state(eng.backend)
    plan = _configure(eng)
    op = plan.nodes[0].operations[index]
    eng.backend.faults.kill = (op.operation_id, point)
    with pytest.raises(SimulatedKill):
        _apply(eng, plan)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    restarted = H.engine(tmp_path, repo)  # a fresh process over the same simulated host
    outcome = restarted.reconcile_run(run_id, executor_dead=True)
    if index == len(KINDS) - 1 and point in ("after-manifest", "after-record"):
        assert outcome.state == "SATISFIED"  # every receipt committed: the swap stands
        # drift is clean; the invariant half alarms with no seat observation (S1), so check checks only
        assert all(c["ok"] for c in restarted.verify(SCOPE, CID)[0]["checks"])
        return
    assert outcome.state == "HALTED"
    assert restarted.rollback(run_id).state == "ROLLED_BACK"
    assert _seat_state(restarted.backend) == preimage
