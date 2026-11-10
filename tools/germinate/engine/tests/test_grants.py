"""S2 (T-5135): independent-agent gates consume a pre-issued grant from the ORG-052 approval service.

The grant source here is the REAL ``ApprovalService`` over a tmp sqlite journal
(in-process, no bus), with the real ``PolicyApproverAuthorizer`` (separation of
duty). Its plan-metadata seam is a live recompute from the engine's own plan.
Nothing touches a host, a socket or the live-actuation switch.
"""

from __future__ import annotations

import ast
import dataclasses
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.germinate.engine import grants as G
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.run_state import RunStore
from tools.germinate.engine.tests import helpers as H
from tools.host_control.approval.contracts import ApproverDenied, PlanMetadata
from tools.host_control.approval.journal import ApprovalJournal
from tools.host_control.approval.policy import PolicyApproverAuthorizer, parse_approver_policy
from tools.host_control.approval.service import ApprovalService
from tools.host_control.contracts import UnknownHandle

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
CONSUMER_UID = 4242
SEAT = "seat-alice"
GATES = [
    {"id": "gate-a", "irreversible": True, "approval": {"class": "independent-agent", "grant_source": "action-broker-approval-registry"}},
    {"id": "gate-b", "irreversible": True, "approval": {"class": "independent-agent", "grant_source": "action-broker-approval-registry"}},
    {"id": "op-gate", "approval": {"class": "operator"}},
]
OPS_ONE = {"install": [H.dir_op("op-a", "/srv/a", gate="gate-a")]}
OPS_TWO = {"install": [H.dir_op("op-a", "/srv/a", gate="gate-a"), H.dir_op("op-b", "/srv/b", gate="gate-b")]}
OPS_MIXED = {"install": [H.dir_op("op-o", "/srv/o", gate="op-gate"), H.dir_op("op-a", "/srv/a", gate="gate-a")]}


def _policy():
    grants = lambda: [{"family": "mkdir", "seat_classes": ["persona_seat"], "max_ttl_seconds": 600}]  # noqa: E731
    return {
        "schema": "pantheon-approval-policy/v1",
        "revision": "approver-policy-r1",
        "seat_classes": {"system": "persona_seat", SEAT: "persona_seat"},
        "subjects": {"operator": 1001, "germinate-engine": 1002, "reviewer": 1003, "reviewer-twin": 1003, "other": 1004},
        "family_categories": {"mkdir": "seat_apply_reversible"},
        "approvers": {
            "reviewer": {"decision_source": "agent", "grants": grants()},
            "reviewer-twin": {"decision_source": "agent", "grants": grants()},
            "operator": {"decision_source": "agent", "grants": grants()},
            "germinate-engine": {"decision_source": "agent", "grants": grants()},
            "other": {"decision_source": "agent", "grants": grants()},
        },
        "revokers": ["reviewer"],
    }


class LiveSource:
    """Plan-metadata seam: recomputed from the engine's CURRENT plan on every call (never cached)."""

    def __init__(self, eng, leg, scopes):
        self.eng, self.leg, self.scopes = eng, leg, scopes
        self.policy_revision = "broker-policy-r1"

    def _find(self, plan_handle):
        for scope in self.scopes:
            for req in self.eng.grant_requests(self.leg, scope, "comp-a"):
                if req.plan_handle == plan_handle:
                    return req
        raise UnknownHandle("unknown plan")

    def plan_metadata(self, plan_handle):
        return PlanMetadata(**self._find(plan_handle).metadata_fields(self.policy_revision))

    def plan_summary(self, plan_handle, expected_digest):
        req = self._find(plan_handle)
        return req.plan_digest, req.summary()


class Rig:
    def __init__(self, tmp_path, ops, *, scopes=("system",), contract_scopes=("system", "seat")):
        _repo, self.eng = H.simple_setup(tmp_path, ops=ops, gates=GATES, scopes=list(contract_scopes))
        self.eng.echo = io.StringIO()
        self.eng.grant_actor = "operator"
        self.eng.grant_performer = "germinate-engine"
        self.now = NOW
        self.journal = ApprovalJournal.open(tmp_path / "approval.sqlite3")
        self.service = ApprovalService(
            journal=self.journal,
            metadata_source=LiveSource(self.eng, "install", scopes),
            clock=lambda: self.now,
            authorizer=PolicyApproverAuthorizer(parse_approver_policy(_policy())),
            registered_consumer_uids=frozenset((CONSUMER_UID,)),
        )
        self.eng.grant_consumer = self.service.as_binder(CONSUMER_UID)
        self._key = 0

    def requests(self, scope="system"):
        return {r.op_id: r for r in self.eng.grant_requests("install", scope, "comp-a")}

    def issue(self, req, approver="reviewer"):
        review = self.service.review(approver, req.plan_handle, req.plan_digest, None)
        self._key += 1
        result = self.service.decide(
            approver, review.record.review_handle, req.plan_digest, "granted", "approved", "anchor", f"key-{self._key}"
        )
        return result.approval_handle

    def status(self, handle):
        return self.service.approval_status(CONSUMER_UID, handle).state

    def apply(self, grants, scope="system", **kw):
        plan = self.eng.plan("install", scope, "comp-a")
        return self.eng.apply("install", scope, "comp-a", plan_digest=plan.digest, grants=grants, **kw)

    def close(self):
        self.journal.close()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path, OPS_ONE)
    yield r
    r.close()


def _assert_untouched(rig, *targets):
    for target in targets:
        assert rig.eng.backend.entry(target) is None


def test_a_matching_grant_is_consumed_and_the_apply_proceeds(rig):
    req = rig.requests()["op-a"]
    handle = rig.issue(req)
    [outcome] = rig.apply({"op-a": handle})
    assert outcome.state == "SATISFIED"
    assert rig.eng.backend.entry("/srv/a") is not None
    assert rig.status(handle) == "bound"
    log = rig.eng.echo.getvalue()
    assert '"event": "grant.bound"' in log and '"authority_record_id"' in log
    assert handle not in log  # the grant handle never reaches the transcript


def test_no_consumer_wired_refuses_before_any_mutation(rig):
    handle = rig.issue(rig.requests()["op-a"])
    rig.eng.grant_consumer = None
    with pytest.raises(EngineRefusal) as info:
        rig.apply({"op-a": handle})
    assert info.value.code == "grant-required"
    _assert_untouched(rig, "/srv/a")
    assert rig.status(handle) == "issued"


def test_no_grant_supplied_refuses_and_accept_gates_cannot_stand_in(rig):
    for accept in (None, frozenset(), frozenset({"gate-a"})):
        with pytest.raises(EngineRefusal) as info:
            rig.apply(None, accept_gates=accept)
        assert info.value.code == "grant-required"
    _assert_untouched(rig, "/srv/a")


@pytest.mark.parametrize("bad", ["", "xyz", "A" * 64, "a" * 63, "a" * 65])
def test_malformed_handle_refuses(rig, bad):
    with pytest.raises(EngineRefusal) as info:
        rig.apply({"op-a": bad})
    assert info.value.code == "grant-refused"
    _assert_untouched(rig, "/srv/a")


def test_unknown_handle_is_denied(rig):
    with pytest.raises(EngineRefusal, match="unknown_approval"):
        rig.apply({"op-a": "b" * 64})
    _assert_untouched(rig, "/srv/a")


def test_a_grant_is_single_use_and_replay_of_the_same_operation_is_idempotent(rig):
    req = rig.requests()["op-a"]
    handle = rig.issue(req)
    rig.apply({"op-a": handle})
    consumer = rig.eng.grant_consumer
    other_op = "c" * 64
    denied = consumer.validate_and_bind(handle, req.plan_handle, req.plan_digest, other_op)
    assert (denied.state, denied.reason_code) == ("denied", "already_bound")
    replay = consumer.validate_and_bind(handle, req.plan_handle, req.plan_digest, req.operation_handle)
    assert (replay.state, replay.reason_code) == ("bound", "bound_replay")


def test_a_grant_for_gate_a_cannot_bind_gate_b(tmp_path):
    rig = Rig(tmp_path, OPS_TWO)
    try:
        reqs = rig.requests()
        handle_a = rig.issue(reqs["op-a"])
        handle_b = rig.issue(reqs["op-b"])
        with pytest.raises(EngineRefusal, match="op-a: grant denied \\(plan_mismatch\\)"):
            rig.apply({"op-a": handle_b, "op-b": handle_a})  # each grant offered for the OTHER gate's operation
        _assert_untouched(rig, "/srv/a", "/srv/b")
        assert rig.status(handle_a) == "issued" and rig.status(handle_b) == "issued"
        # one grant cannot answer two gated operations either (a partial bind may burn the first, never mutates)
        with pytest.raises(EngineRefusal, match="op-b: grant denied"):
            rig.apply({"op-a": handle_a, "op-b": handle_a})
        _assert_untouched(rig, "/srv/a", "/srv/b")
    finally:
        rig.close()


def test_a_grant_for_scope_x_cannot_bind_scope_y(tmp_path):
    rig = Rig(tmp_path, OPS_ONE, scopes=("system", SEAT))
    try:
        system_req = rig.requests("system")["op-a"]
        seat_req = rig.requests(SEAT)["op-a"]
        assert system_req.plan_handle != seat_req.plan_handle and system_req.operation_handle != seat_req.operation_handle
        handle = rig.issue(system_req)
        with pytest.raises(EngineRefusal, match="plan_mismatch"):
            rig.apply({"op-a": handle}, scope=SEAT)
        _assert_untouched(rig, "/srv/a")
        assert rig.status(handle) == "issued"
        [outcome] = rig.apply({"op-a": handle}, scope="system")
        assert outcome.state == "SATISFIED"
    finally:
        rig.close()


def _receipt_bodies(rig, run_id, scope="system"):
    store = RunStore(rig.eng.backend.run_state_root(scope))
    with store.open(store.find(run_id)) as handle:
        wal = handle.wal()
        return {
            e.op_id: wal.read_receipt_by_ref(e.receipt_ref)[0].receipt_payload for e in wal.read_manifest().receipts
        }


def test_receipt_body_carries_the_authority_gate_and_scope_never_the_handle(tmp_path):
    rig = Rig(tmp_path, OPS_MIXED)
    try:
        handle = rig.issue(rig.requests()["op-a"])
        [outcome] = rig.apply({"op-a": handle}, accept_gates=frozenset({"op-gate"}))
        bodies = _receipt_bodies(rig, outcome.run_id)
        bound = [json.loads(line) for line in rig.eng.echo.getvalue().splitlines() if '"grant.bound"' in line]
        assert len(bound) == 1 and bound[0]["component"] == "comp-a" and bound[0]["scope"] == "system"
        assert bodies["op-a"]["authority"] == {
            "gate_id": "gate-a",
            "scope": "system",
            "authority_record_id": bound[0]["authority_record_id"],
        }
        assert bodies["op-o"]["authority"] is None  # operator gate: no independent authority
        assert handle not in json.dumps(bodies)
    finally:
        rig.close()


def test_binding_and_summary_name_gate_and_scope_explicitly(rig):
    req = rig.requests()["op-a"]
    binding = req.binding()
    assert (binding["component"], binding["op_id"], binding["gate_id"], binding["scope"], binding["kind"]) == (
        "comp-a", "op-a", "gate-a", "system", req.kind,
    )
    assert "plan_handle" not in binding and "operator" not in json.dumps(binding)  # actor-free, handle-free
    summary = json.loads(req.summary())
    assert summary["gate_id"] == "gate-a" and summary["scope"] == "system"
    assert summary["effect_digest"] == req.effect_digest and summary["plan_digest"] == req.plan_digest
    assert len(req.summary()) < 4096
    review = rig.service.review("reviewer", req.plan_handle, req.plan_digest, None)
    assert review.record.effect_digest == req.effect_digest


def test_effect_digest_commits_to_every_binding_field():
    base = G.GrantRequest("comp-a", "op-a", "gate-a", "mkdir", "system", "1" * 64, "sha256:c", "operator", "engine", "2" * 64)
    seen = {base.effect_digest}
    for field, value in (
        ("component", "comp-b"),  # same op id, gate and scope in another component (collided under v1)
        ("op_id", "op-b"),
        ("gate_id", "gate-b"),
        ("scope", "seat-alice"),
        ("kind", "file"),
        ("op_spec_digest", "3" * 64),
        ("contract_digest", "sha256:d"),
    ):
        digest = dataclasses.replace(base, **{field: value}).effect_digest
        assert digest not in seen, field
        seen.add(digest)
    # actor-free (ORG-052 §4: owner/performer excluded) and plan-digest-free
    assert dataclasses.replace(base, actor="other", performer="x", plan_digest="9" * 64).effect_digest == base.effect_digest


def test_a_resumed_apply_within_expiry_replays_the_binding_without_a_second_grant(tmp_path):
    rig = Rig(tmp_path, OPS_TWO)
    try:
        reqs = rig.requests()
        handle_a = rig.issue(reqs["op-a"])
        wrong_b = rig.issue(reqs["op-a"])  # a grant for op-a's gate, offered for op-b's
        with pytest.raises(EngineRefusal, match=r"op-b: grant denied \(plan_mismatch\)"):
            rig.apply({"op-a": handle_a, "op-b": wrong_b})
        assert rig.status(handle_a) == "bound"  # op-a's bind committed before op-b refused
        _assert_untouched(rig, "/srv/a", "/srv/b")
        handle_b = rig.issue(reqs["op-b"])
        rig.now = NOW + timedelta(minutes=5)
        [outcome] = rig.apply({"op-a": handle_a, "op-b": handle_b})
        assert outcome.state == "SATISFIED"
        bodies = _receipt_bodies(rig, outcome.run_id)
        assert bodies["op-a"]["authority"]["gate_id"] == "gate-a" and bodies["op-b"]["authority"]["gate_id"] == "gate-b"
    finally:
        rig.close()


def test_a_resumed_apply_after_expiry_cannot_replay_the_bound_grant(tmp_path):
    rig = Rig(tmp_path, OPS_TWO)
    try:
        reqs = rig.requests()
        handle_a = rig.issue(reqs["op-a"])
        wrong_b = rig.issue(reqs["op-a"])
        with pytest.raises(EngineRefusal, match="op-b: grant denied"):
            rig.apply({"op-a": handle_a, "op-b": wrong_b})
        assert rig.status(handle_a) == "bound"
        rig.now = NOW + timedelta(hours=2)
        handle_b = rig.issue(reqs["op-b"])
        with pytest.raises(EngineRefusal, match=r"op-a: grant denied \(expired\)"):
            rig.apply({"op-a": handle_a, "op-b": handle_b})
        _assert_untouched(rig, "/srv/a", "/srv/b")
        assert rig.status(handle_a) == "bound"  # BOUND is terminal; expiry never rewrites it
        assert rig.status(handle_b) == "issued"  # refused before op-b's bind: not burned
    finally:
        rig.close()


def test_metadata_drift_between_issue_and_bind_is_denied(rig):
    handle = rig.issue(rig.requests()["op-a"])
    rig.service._metadata_source.policy_revision = "broker-policy-r2"
    with pytest.raises(EngineRefusal, match="policy_revision_mismatch"):
        rig.apply({"op-a": handle})
    _assert_untouched(rig, "/srv/a")


def test_self_approval_is_refused_end_to_end(rig):
    req = rig.requests()["op-a"]
    for approver in ("operator", "germinate-engine"):  # the plan owner (actor) and the performer
        with pytest.raises(ApproverDenied):
            rig.issue(req, approver)
    # a same-uid shadow of the actor's trust domain is not independent either (reviewer-twin shares reviewer's uid, not the actor's)
    rig.eng.grant_actor = "reviewer"
    req2 = rig.requests()["op-a"]
    with pytest.raises(ApproverDenied):
        rig.issue(req2, "reviewer")
    with pytest.raises(ApproverDenied):
        rig.issue(req2, "reviewer-twin")  # shares reviewer's uid: same trust domain
    with pytest.raises(EngineRefusal) as info:
        rig.apply(None)
    assert info.value.code == "grant-required"
    _assert_untouched(rig, "/srv/a")


def test_expired_grant_is_denied(rig):
    handle = rig.issue(rig.requests()["op-a"])
    rig.now = NOW + timedelta(hours=2)
    with pytest.raises(EngineRefusal, match="expired"):
        rig.apply({"op-a": handle})
    _assert_untouched(rig, "/srv/a")


def test_revoked_grant_is_denied(rig):
    handle = rig.issue(rig.requests()["op-a"])
    rig.service.revoke("reviewer", handle, "revoked_by_admin")
    with pytest.raises(EngineRefusal, match="revoked"):
        rig.apply({"op-a": handle})
    _assert_untouched(rig, "/srv/a")


def test_consumer_outage_fails_closed(rig):
    handle = rig.issue(rig.requests()["op-a"])

    class Down:
        def validate_and_bind(self, *args):
            raise ConnectionError("approval service down")

    rig.eng.grant_consumer = Down()
    with pytest.raises(EngineRefusal, match="grant-refused: op-a: .*ConnectionError") as info:
        rig.apply({"op-a": handle})
    assert "down" not in info.value.message
    _assert_untouched(rig, "/srv/a")


@pytest.mark.parametrize(
    "outcome",
    [
        None,
        object(),
        type("O", (), {"state": "bound", "authority_record_id": None, "reason_code": "bound"})(),
        type("O", (), {"state": "bound", "authority_record_id": "short", "reason_code": "bound"})(),
        type("O", (), {"state": "pending", "authority_record_id": "a" * 64, "reason_code": "x"})(),
    ],
)
def test_malformed_or_non_bound_outcome_is_a_refusal(rig, outcome):
    class Odd:
        def validate_and_bind(self, *args):
            return outcome

    rig.eng.grant_consumer = Odd()
    with pytest.raises(EngineRefusal) as info:
        rig.apply({"op-a": "d" * 64})
    assert info.value.code == "grant-refused"
    _assert_untouched(rig, "/srv/a")


def test_a_missing_grant_for_a_later_op_burns_no_earlier_grant(tmp_path):
    rig = Rig(tmp_path, OPS_TWO)
    try:
        reqs = rig.requests()
        handle_a = rig.issue(reqs["op-a"])
        with pytest.raises(EngineRefusal) as info:
            rig.apply({"op-a": handle_a})
        assert info.value.code == "grant-required" and "op-b" in info.value.message
        assert rig.status(handle_a) == "issued"
        _assert_untouched(rig, "/srv/a", "/srv/b")
        handle_b = rig.issue(reqs["op-b"])
        [outcome] = rig.apply({"op-a": handle_a, "op-b": handle_b})
        assert outcome.state == "SATISFIED"
    finally:
        rig.close()


def test_an_unanswered_operator_gate_refuses_before_a_grant_is_burned(tmp_path):
    rig = Rig(tmp_path, OPS_MIXED)
    try:
        handle = rig.issue(rig.requests()["op-a"])
        with pytest.raises(EngineRefusal, match="gate-unanswered"):
            rig.apply({"op-a": handle})
        assert rig.status(handle) == "issued"
        [outcome] = rig.apply({"op-a": handle}, accept_gates=frozenset({"op-gate"}))
        assert outcome.state == "SATISFIED"
    finally:
        rig.close()


def test_a_grant_is_not_needed_for_an_ungated_or_operator_only_plan(tmp_path):
    ops = {"install": [H.dir_op("op-o", "/srv/o", gate="op-gate")]}
    rig = Rig(tmp_path, ops)
    try:
        rig.eng.grant_consumer = None
        [outcome] = rig.apply(None, accept_gates=frozenset())
        assert outcome.state == "SATISFIED"
    finally:
        rig.close()


# --- grant file (handles never in argv) ----------------------------------------


def _write(path: Path, text: str, mode=0o600):
    path.write_text(text, encoding="ascii")
    path.chmod(mode)
    return path


def test_grant_file_happy_path_and_comments(tmp_path):
    f = _write(tmp_path / "g", "# grants\n\nop-a " + "a" * 64 + "\nop-b " + "b" * 64 + "\n")
    assert G.read_grant_file(f, os.getuid()) == {"op-a": "a" * 64, "op-b": "b" * 64}
    f.chmod(0o400)
    assert G.read_grant_file(f, os.getuid())


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o602, 0o666])
def test_grant_file_loose_mode_refuses(tmp_path, mode):
    f = _write(tmp_path / "g", "op-a " + "a" * 64 + "\n", mode)
    with pytest.raises(G.GrantRefused, match="0600"):
        G.read_grant_file(f, os.getuid())


def test_grant_file_symlink_wrong_owner_and_oversize_refuse(tmp_path):
    real = _write(tmp_path / "real", "op-a " + "a" * 64 + "\n")
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(G.GrantRefused, match="symlink"):
        G.read_grant_file(link, os.getuid())
    with pytest.raises(G.GrantRefused, match="owned"):
        G.read_grant_file(real, os.getuid() + 1)
    big = _write(tmp_path / "big", "#" + "x" * G.MAX_GRANT_FILE_BYTES + "\n")
    with pytest.raises(G.GrantRefused, match="oversized"):
        G.read_grant_file(big, os.getuid())
    with pytest.raises(G.GrantRefused, match="cannot be opened"):
        G.read_grant_file(tmp_path / "absent", os.getuid())


@pytest.mark.parametrize("body", ["op-a\n", "op-a XYZ\n", "op-a " + "a" * 64 + " extra\n", "op-a " + "a" * 64 + "\nop-a " + "b" * 64 + "\n"])
def test_grant_file_malformed_lines_refuse(tmp_path, body):
    with pytest.raises(G.GrantRefused):
        G.read_grant_file(_write(tmp_path / "g", body), os.getuid())


# --- cli --------------------------------------------------------------------------


def test_cli_grant_file_without_a_wired_consumer_refuses(tmp_path, capsys):
    from tools.germinate.cli import main
    from tools.germinate.engine.backend import SimulatedHostBackend

    repo = tmp_path / "repo"
    repo.mkdir()
    _p, digest = H.write_contract(repo, "comp-a", ops=OPS_ONE, gates=GATES)
    H.make_checkout(repo, [H.row("comp-a", digest)])
    sim_root = tmp_path / "sim"
    SimulatedHostBackend(sim_root).simulate_postboot()
    common = ["--repo-root", str(repo), "--simulated-host-root", str(sim_root)]
    assert main(["plan", "install", "--system", "comp-a", *common]) == 0
    plan_digest = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")][-1]["result"]["plan_digest"]
    f = _write(tmp_path / "g", "op-a " + "a" * 64 + "\n")
    rc = main(["apply", "install", "--system", "comp-a", "--plan-digest", plan_digest, "--grant-file", str(f), *common])
    assert rc == 1
    assert "grant-required" in capsys.readouterr().err
    f.chmod(0o644)  # a loose grant file refuses before anything else
    rc = main(["apply", "install", "--system", "comp-a", "--plan-digest", plan_digest, "--grant-file", str(f), *common])
    assert rc == 1 and "0600" in capsys.readouterr().err
    assert SimulatedHostBackend(sim_root).entry("/srv/a") is None


# --- the boundary --------------------------------------------------------------------


def test_grants_module_reaches_no_bus_socket_or_live_switch():
    tree = ast.parse(Path(G.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert not imported & {"socket", "dbus", "subprocess", "ctypes", "http", "urllib", "tools"}
    text = Path(G.__file__).read_text(encoding="utf-8")
    assert "LIVE_ACTUATION_ENABLED" not in text
