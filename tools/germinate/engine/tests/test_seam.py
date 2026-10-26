"""The frozen adoption seam (T-5074 review): a component brings handlers, op kinds
and plan inputs by shipping files; no engine edit."""

from __future__ import annotations

import pytest
import yaml

from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.tests import helpers as H
from tools.germinate.op_registry import load_op_registry
from tools.germinate.registry.contract_digest import SENTINEL, compute_contract_digest

HANDLERS_SRC = '''
from tools.persona_installer import MutationRecord, OperationEvidence, PreflightResult


class RepoHandler:
    name = "repo-sim"
    kinds = frozenset({"repo.ff-update"})

    def __init__(self):
        self.head = "rev-0"

    def observe(self, component, scope):
        return {"head": self.head}

    def preflight(self, op):
        return PreflightResult(True, "ok", "simulated repo")

    def execute(self, op):
        before = {"head": self.head}
        self.head = op.desired["to"]
        return (
            MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, before, self.head),
            OperationEvidence(op.operation_id, op.layer, op.kind, True, "moved"),
        )

    def verify_operation(self, op):
        return OperationEvidence(op.operation_id, op.layer, op.kind, self.head == op.desired["to"], "head check")

    def rollback_intent(self, op):
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, {"head": self.head}, op.desired["to"])

    def can_rollback(self, m):
        if self.head == m.before["head"]:
            return PreflightResult(True, "not-applied", "already at preimage")
        return PreflightResult(self.head == m.after_fingerprint, "owned" if self.head == m.after_fingerprint else "drifted", "")

    def rollback_mutation(self, m):
        self.head = m.before["head"]

    def dump_state(self):
        return {"head": self.head}

    def load_state(self, state):
        self.head = state.get("head", "rev-0")


HANDLERS = [RepoHandler()]
OP_KINDS = {"repo.ff-update": ("repo.local", "seat")}
'''

PLANNER_SRC = '''
def _op(request):
    return {
        "operation_id": "ff-1", "layer": 1, "kind": "repo.ff-update", "target": "pantheon-config",
        "precondition": {"from": request["observed"]["repo-sim"]["head"]},
        "desired": {"to": request["source_rev_to"] or "rev-1", "channel": request["desired"].get("channel", "")},
        "inverse": None, "privilege": "seat", "owner": "orchestrator", "provenance": {},
        "crash_contract": "stageable", "side_effect_free": False,
    }

def plan_install(request):
    return [_op(request)]

def plan_update(request):
    assert request["source_rev_from"] == "rev-1", request
    return [_op(request)]

def plan_remove(request):
    return []
'''


def _component(repo, cid="comp-r", **overrides):
    defaults = dict(
        privilege={"seat": ["repo.ff-update"]},
        legs={"install": "plan_install", "update": "plan_update", "remove": "plan_remove"},
        desired_schema={"channel": {"enum": ["stable", "edge"], "default": "stable"}},
    )
    defaults.update(overrides)
    H.write_contract(repo, cid, planner_src=PLANNER_SRC, **defaults)
    return H.write_handlers(repo, cid, HANDLERS_SRC)


def _setup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    digest = _component(repo)
    H.make_checkout(repo, [H.row("comp-r", digest)])
    return H.engine(tmp_path, repo)


def test_component_handlers_and_kinds_load_without_engine_edits(tmp_path):
    eng = _setup(tmp_path)
    assert eng.backend.handles("repo.ff-update")
    assert eng.backend.op_registry.get("repo.ff-update").capability_class == "repo.local"
    assert eng.backend.loaded_op_registry_digest == load_op_registry({"repo.ff-update": ("repo.local", "seat")}).digest
    assert eng.backend.loaded_op_registry_digest != load_op_registry().digest
    plan = eng.plan("install", "system", "comp-r")
    [outcome] = eng.apply("install", "system", "comp-r", plan_digest=plan.digest)
    assert outcome.state == "SATISFIED"
    reloaded = H.engine(tmp_path, eng.repo_root)  # fresh process: handler state persisted
    assert reloaded.backend.observe("comp-r", "system", frozenset({"repo.ff-update"})) == {"repo-sim": {"head": "rev-1"}}


def test_update_leg_receives_source_revs(tmp_path):
    eng = _setup(tmp_path)
    eng.apply("install", "system", "comp-r", plan_digest=eng.plan("install", "system", "comp-r").digest)
    revs = ("rev-1", "rev-2")
    plan = eng.plan("update", "system", "comp-r", source_revs=revs)
    assert plan.nodes[0].operations[0].desired["to"] == "rev-2"
    with pytest.raises(EngineRefusal, match="plan-digest-mismatch"):
        eng.apply("update", "system", "comp-r", plan_digest=plan.digest, source_revs=("rev-1", "rev-3"))
    eng.apply("update", "system", "comp-r", plan_digest=plan.digest, source_revs=revs)
    with pytest.raises(EngineRefusal, match="update leg only"):
        eng.plan("install", "system", "comp-r", source_revs=revs)


def test_desired_state_comes_from_the_declared_schema(tmp_path):
    eng = _setup(tmp_path)
    assert eng.plan("install", "system", "comp-r").nodes[0].operations[0].desired["channel"] == "stable"
    edge = eng.plan("install", "system", "comp-r", desired={"channel": "edge"})
    assert edge.nodes[0].operations[0].desired["channel"] == "edge"
    with pytest.raises(EngineRefusal, match="not one of"):
        eng.plan("install", "system", "comp-r", desired={"channel": "nightly"})
    with pytest.raises(EngineRefusal, match="not declared"):
        eng.plan("install", "system", "comp-r", desired={"harness": "codex"})
    with pytest.raises(EngineRefusal, match="--system"):
        eng.plan("install", "system", desired={"channel": "edge"})


def test_kind_declared_twice_refuses(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    d1 = _component(repo, "comp-r")
    d2 = _component(repo, "comp-s")
    H.make_checkout(repo, [H.row("comp-r", d1), H.row("comp-s", d2)])
    with pytest.raises(EngineRefusal, match="component-registration-refused: .*declared by both"):
        H.engine(tmp_path, repo)


def test_kind_colliding_with_the_envelope_refuses(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _component(repo)
    digest = H.write_handlers(repo, "comp-r", HANDLERS_SRC.replace('OP_KINDS = {"repo.ff-update"', 'OP_KINDS = {"mkdir"'))
    H.make_checkout(repo, [H.row("comp-r", digest)])
    with pytest.raises(EngineRefusal, match="component-registration-refused"):
        H.engine(tmp_path, repo)


def test_runtime_subset_union_covers_every_registered_component(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _p, da = H.write_contract(repo, "comp-a", ops={"install": []})
    _p, db = H.write_contract(repo, "comp-b", ops={"install": []}, privilege={"seat": ["mkdir"], "broker_runtime": ["daemon-reload"]})
    H.make_checkout(repo, [H.row("comp-a", da), H.row("comp-b", db)])
    eng = H.engine(tmp_path, repo)
    only_a = eng.plan("install", "system", "comp-a").expected_runtime_subset_digest
    assert only_a == eng.plan("install", "system", "comp-b").expected_runtime_subset_digest
    wrapper = tmp_path / "wrapper.yaml"
    body = {"id": "expected-runtime-subset", "steward": "broker-daemon", "version": 1, "contract_digest": SENTINEL}
    wrapper.write_text(yaml.safe_dump({"component": dict(body, broker_runtime=["daemon-reload"])}), encoding="utf-8")
    assert only_a == compute_contract_digest(wrapper)  # the auditor's file path gives the same digest
    wrapper.write_text(yaml.safe_dump({"component": dict(body, broker_runtime=[])}), encoding="utf-8")
    assert only_a != compute_contract_digest(wrapper)


# --- component op-kind restriction (ORG-080 D-AC; reviewer kinds.py) -----------

ESCALATIONS = {
    "shadow-envelope": "OP_KINDS={'atomic-write': ('diagnostics.read','none')}",
    "shadow-substrate": "OP_KINDS={'account.ensure': ('files.seat_root','seat')}",
    "shadow-llm": "OP_KINDS={'harness-turn': ('files.seat_root','seat')}",
    "new-root-kind": "OP_KINDS={'repo.root-thing': ('substrate.root','root')}",
    "mutating-as-readonly": "OP_KINDS={'git-ff': ('diagnostics.read','none')}",
    "fake-broker-class": "OP_KINDS={'git-ff2': ('user_systemd.activate','seat')}",
    "non-literal": "OP_KINDS=dict(x=('repo.local','seat'))",
}


def _kinds_repo(tmp_path, src, cid="comp-a", **contract):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    H.write_contract(repo, cid, **contract)
    return repo, H.write_handlers(repo, cid, src + "\nHANDLERS = ()\n")


@pytest.mark.parametrize("name", sorted(ESCALATIONS))
def test_component_kind_escalations_refuse_at_load_and_in_the_validator(tmp_path, name):
    from tools.germinate.registry.validate import check_contracts, errors

    repo, digest = _kinds_repo(tmp_path, ESCALATIONS[name])
    H.make_checkout(repo, [H.row("comp-a", digest)])
    with pytest.raises(EngineRefusal, match="component-registration-refused"):
        H.engine(tmp_path, repo)
    doc = yaml.safe_load((repo / "registry" / "systems.yaml").read_text())
    assert "component op kinds refused" in "\n".join(str(f) for f in errors(check_contracts(doc, repo)))


def test_ordinary_seat_component_kind_loads(tmp_path):
    repo, digest = _kinds_repo(tmp_path, "OP_KINDS={'git-ff': ('repo.local','seat')}")
    H.make_checkout(repo, [H.row("comp-a", digest)])
    assert H.engine(tmp_path, repo).backend.op_registry.get("git-ff").privilege == "seat"


def test_merged_registry_digest_ignores_declaration_order(tmp_path):
    digests = []
    orders = (
        ("ab", [("comp-a", "{'k-z': ('x.y','seat'), 'k-a': ('x.y','seat')}"), ("comp-b", "{'k-m': ('x.y','seat')}")]),
        ("ba", [("comp-b", "{'k-m': ('x.y','seat')}"), ("comp-a", "{'k-a': ('x.y','seat'), 'k-z': ('x.y','seat')}")]),
    )
    for order, pairs in orders:
        base = tmp_path / order
        base.mkdir()
        rows = []
        for cid, kinds in pairs:
            repo, digest = _kinds_repo(base, f"OP_KINDS={kinds}", cid=cid)
            rows.append(H.row(cid, digest))
        H.make_checkout(repo, rows)
        digests.append(H.engine(base, repo).backend.op_registry.digest)
    assert digests[0] == digests[1]


def test_validator_runs_the_full_contract_load(tmp_path):
    """A contract naming a kind no registry (merged or engine) defines would block every plan."""

    from tools.germinate.registry.validate import check_contracts, errors

    repo = tmp_path / "repo"
    repo.mkdir()
    _p, digest = H.write_contract(repo, "comp-a", privilege={"seat": ["repo.nowhere"]})
    H.make_checkout(repo, [H.row("comp-a", digest)])
    doc = yaml.safe_load((repo / "registry" / "systems.yaml").read_text())
    msgs = "\n".join(str(f) for f in errors(check_contracts(doc, repo)))
    assert "would refuse this contract at plan" in msgs and "repo.nowhere" in msgs
    digest = H.write_handlers(repo, "comp-a", "OP_KINDS={'repo.nowhere': ('repo.local','seat')}\n")
    H.make_checkout(repo, [H.row("comp-a", digest)])
    doc = yaml.safe_load((repo / "registry" / "systems.yaml").read_text())
    assert errors(check_contracts(doc, repo)) == []


def test_validator_never_imports_handlers(tmp_path):
    from tools.germinate.registry.validate import check_contracts, errors

    marker = tmp_path / "IMPORTED"
    src = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\nOP_KINDS={{'git-ff': ('repo.local','seat')}}"
    repo, digest = _kinds_repo(tmp_path, src)
    H.make_checkout(repo, [H.row("comp-a", digest)])
    doc = yaml.safe_load((repo / "registry" / "systems.yaml").read_text())
    assert errors(check_contracts(doc, repo)) == [] and not marker.exists()
