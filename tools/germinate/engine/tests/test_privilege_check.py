"""T-5941 (engine seam gap S7): the actuation-time privilege check (engine/privilege.py)."""

from __future__ import annotations

import io
import itertools
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from tools.germinate.components.tests import test_host_substrate_component as HS
from tools.germinate.engine import privilege as P
from tools.germinate.engine.context import InvocationContext
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.tests import helpers as H
from tools.persona_installer import OperationFailed

ALL = frozenset({"none", "seat", "root"})
COMPONENTS = Path(__file__).resolve().parents[2] / "components"


def _be(ceiling=ALL, bound=None):
    return SimpleNamespace(privilege_ceiling=frozenset(ceiling), bound_account=bound)


def _op(priv="seat", owner="alice", op_id="o"):
    return SimpleNamespace(operation_id=op_id, privilege=priv, owner=owner)


def _r(backend=None, ctx=None, scope="system", op=None, comp="comp-a"):
    return P.refusal(backend or _be(), ctx or InvocationContext(), scope, op or _op(), comp)


# --- C1 / C2: the privilege and the ceiling ---------------------------------------


@pytest.mark.parametrize("priv", ["superuser", "", None, "ROOT"])
def test_c1_unknown_privilege_is_refused_even_if_the_ceiling_lists_it(priv):
    assert _r(_be({"none", "seat", "root", "superuser", "", "ROOT", None}), op=_op(priv)) is not None


@pytest.mark.parametrize("priv", ["none", "seat", "root"])
def test_c2_privilege_above_the_ceiling_is_refused(priv):
    ceiling = ALL - {priv}
    assert _r(_be(ceiling), op=_op(priv)) is not None
    assert _r(_be(ALL), op=_op(priv)) is None


def test_c2_empty_ceiling_refuses_everything():
    assert all(_r(_be(set()), op=_op(p)) for p in ALL)


# --- C3: privilege vs node scope --------------------------------------------------


def test_c3_root_in_a_seat_scope_is_refused_everywhere_else_permitted():
    assert _r(scope="seat-alice", op=_op("root")) is not None
    assert _r(scope="system", op=_op("root")) is None
    for p in ("none", "seat"):
        assert _r(scope="seat-alice", op=_op(p)) is None


@pytest.mark.parametrize("scope", ["", "seat-", "seat", "all", "global", None])
def test_c3_unparseable_scope_is_refused(scope):
    assert _r(scope=scope) is not None


# --- C4: node scope vs bound account ----------------------------------------------


def test_c4_bound_backend_admits_only_its_own_seat_scope_and_none_in_system():
    b = _be(bound="alice")
    assert _r(b, scope="seat-alice", op=_op("seat", "alice")) is None
    assert _r(b, scope="seat-bob", op=_op("seat", "alice")) is not None
    assert _r(b, scope="system", op=_op("none")) is None
    assert _r(b, scope="system", op=_op("seat", "alice")) is not None
    assert _r(b, scope="system", op=_op("root")) is not None


def test_c4_unbound_backend_is_unconstrained_by_scope():
    for scope in ("system", "seat-alice", "seat-bob"):
        assert _r(_be(), scope=scope, op=_op("none")) is None


# --- C5: seat op owner vs bound account -------------------------------------------


def test_c5_seat_op_owner_must_be_the_bound_account_or_the_component_label():
    b = _be(bound="alice")
    assert _r(b, scope="seat-alice", op=_op("seat", "alice")) is None
    assert _r(b, scope="seat-alice", op=_op("seat", "bob")) is not None
    assert _r(b, scope="seat-alice", op=_op("seat", "action-broker"), comp="action-broker") is None
    assert _r(b, scope="seat-alice", op=_op("seat", "action-broker"), comp="watch") is not None  # exactly its own id
    assert _r(b, scope="seat-alice", op=_op("none", "bob")) is None  # only seat ops carry an owner constraint


# --- C6: the invocation account ---------------------------------------------------


def test_c6_no_account_imposes_no_constraint_and_an_account_must_match_everything_set():
    assert _r(_be(bound="alice"), scope="seat-alice", op=_op("seat", "alice")) is None
    ctx = InvocationContext("scheduled", "alice")
    assert _r(_be(bound="alice"), ctx, "seat-alice", _op("seat", "alice")) is None
    assert _r(_be(bound="bob"), ctx, "seat-bob", _op("seat", "bob")) is not None  # backend bound elsewhere
    assert _r(_be(), ctx, "seat-bob", _op("none")) is not None  # scope account differs
    assert _r(_be(), ctx, "seat-alice", _op("seat", "bob")) is not None  # op owned by another account
    assert _r(_be(), ctx, "system", _op("seat", "watch"), comp="watch") is None  # component label owner
    assert _r(_be(), ctx, "system", _op("seat", "alice")) is None
    assert _r(_be(), ctx, "system", _op("seat", "bob")) is not None  # owner compared in every scope


def test_c6_system_scope_unbound_backend_refuses_a_seat_op_owned_by_another_account():
    ctx = InvocationContext("scheduled", "alice")
    why = _r(_be(bound=None), ctx, "system", _op("seat", "bob"))
    assert why is not None and "does not own" in why
    assert _r(_be(bound=None), ctx, "system", _op("seat", "alice")) is None


def test_validate_scope_dependency_is_real_refusal_alone_admits_a_malformed_seat_scope():
    assert _r(_be(), scope="seat-a ", op=_op("none")) is None  # the sim backend's validate_scope refuses this earlier


# --- the positive table -----------------------------------------------------------


@pytest.mark.parametrize(
    "scope,priv,owner",
    [("system", "none", "x"), ("system", "seat", "x"), ("system", "root", "root"), ("seat-alice", "none", "x"), ("seat-alice", "seat", "alice")],
)
def test_positive_table_unbound_default_context(scope, priv, owner):
    assert _r(scope=scope, op=_op(priv, owner)) is None


@pytest.mark.parametrize("scope,priv", list(itertools.product(["system", "seat-alice"], ["none", "seat", "root"])))
def test_unbound_full_ceiling_matches_the_scope_rule_exactly(scope, priv):
    assert (_r(scope=scope, op=_op(priv)) is None) == (scope == "system" or priv != "root")


# --- the dark live seat backend ---------------------------------------------------


def _seat_backend():
    from tools.germinate.seat_backend import backend as SB

    ident = SB.SeatIdentity("persona-a", "alice", 1000, Path("/nonexistent-state"))
    return SB._SeatBackendCore(ident, port=object(), op_registry=SimpleNamespace(digest="d"))


def test_dark_seat_backend_refuses_root_and_other_accounts_without_actuation():
    be = _seat_backend()  # constructor only sets attributes; no live host access
    assert be.bound_account == "alice" and be.privilege_ceiling == frozenset({"none", "seat"})
    assert _r(be, scope="seat-alice", op=_op("seat", "alice")) is None
    assert _r(be, scope="system", op=_op("root", "root")) is not None
    assert _r(be, scope="seat-alice", op=_op("root", "alice")) is not None
    assert _r(be, scope="seat-alice", op=_op("seat", "bob")) is not None
    assert _r(be, scope="seat-bob", op=_op("seat", "bob")) is not None
    # none is inside the ceiling since T-6167 PR-2; the coarse check admits it and the backend's
    # per-kind pin (READ_ONLY_NONE_KINDS) is the real gate (test_install_handlers.py)
    assert _r(be, scope="seat-alice", op=_op("none", "alice")) is None


# --- real contracts: owner literals ------------------------------------------------


def test_real_contract_owner_literals_are_pinned():
    """Every owner literal a real contract plans. The component-label exemption (C5/C6) is
    exactly owner == component id, so a new literal must be a deliberate, reviewed addition."""

    found: dict[str, set[str]] = {}
    for src in sorted(COMPONENTS.glob("*.py")):
        if src.name.endswith("_handlers.py") or src.name.startswith("test_"):
            continue
        text = src.read_text(encoding="utf-8")
        lits = set(re.findall(r'"owner":\s*"([^"]+)"', text)) | set(re.findall(r'^OWNER\s*=\s*"([^"]+)"', text, re.M))
        lits |= set(re.findall(r'or\s+"(unowned)"', text))
        if lits:
            found[src.stem] = lits
    assert found == {
        "action-broker": {"action-broker"},  # == component id: the label exemption
        "watch": {"watch"},  # == component id: the label exemption
        "host-substrate": {"root"},  # privilege root, system scope only
        "credential-broker": {"unowned"},  # system scope only; refused (not exempt) on a bound backend
    }


def test_real_label_owners_equal_their_component_id():
    for comp, owner in (("action-broker", "action-broker"), ("watch", "watch")):
        assert _r(_be(bound="alice"), scope="seat-alice", op=_op("seat", owner), comp=comp) is None


# --- engine integration -------------------------------------------------------------


def _two_comp_setup(tmp_path, owner_b="bob"):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    rows = []
    for cid, owner in (("comp-a", "alice"), ("comp-b", owner_b)):
        ops = [H.dir_op(f"{cid}-d", f"/srv/{cid}", owner=owner)]
        _p, digest = H.write_contract(repo, cid, ops={"install": ops})
        rows.append(H.row(cid, digest))
    H.make_checkout(repo, rows)
    return repo


def _bound_engine(tmp_path, repo, account="alice", name="b", **kw):
    (tmp_path / name).mkdir(parents=True, exist_ok=True)
    eng = H.engine(tmp_path / name, repo, **kw)
    eng.backend.bound_account = account
    return eng


def test_mixed_plan_is_refused_whole_across_two_nodes_with_nothing_applied(tmp_path):
    repo = _two_comp_setup(tmp_path)
    eng = _bound_engine(tmp_path, repo)  # comp-a (alice) would pass, comp-b (bob) is refused
    before = H.tree_snapshot(eng.backend.root)
    with pytest.raises(EngineRefusal) as exc:
        eng.plan("install", "seat-alice", None)
    assert exc.value.code == "privilege-refused" and "comp-b" in exc.value.message
    assert H.tree_snapshot(eng.backend.root) == before  # host unchanged, no run record, no claim, no WAL


def test_plan_preflight_apply_and_grant_requests_all_refuse(tmp_path):
    repo = _two_comp_setup(tmp_path)
    (tmp_path / "ok").mkdir()
    ok = H.engine(tmp_path / "ok", repo).plan("install", "seat-alice", None)
    eng = _bound_engine(tmp_path, repo)
    before = H.tree_snapshot(eng.backend.root)
    with pytest.raises(EngineRefusal) as exc:
        eng.plan("install", "seat-alice", None)
    assert exc.value.code == "privilege-refused"
    receipt = eng.preflight("install", "seat-alice", None, plan_digest=ok.digest)
    assert receipt["verdict"] == "NO-GO" and "privilege-refused" in json.dumps(receipt)
    with pytest.raises(EngineRefusal) as exc:
        eng.apply("install", "seat-alice", None, plan_digest=ok.digest)
    assert exc.value.code == "privilege-refused"
    with pytest.raises(EngineRefusal) as exc:
        eng.grant_requests("install", "seat-alice", None)
    assert exc.value.code == "privilege-refused"
    assert H.tree_snapshot(eng.backend.root) == before


def test_execute_op_backstop_raises_before_any_wal_intent(tmp_path):
    repo, eng = H.simple_setup(tmp_path, {"install": [H.dir_op("op-1", "/srv/c")]})
    merged = eng.plan("install", "system", "comp-a")
    node = merged.nodes[0]
    eng.backend.privilege_ceiling = frozenset({"none"})  # lowered after planning: a path that skipped _plan
    handle, wal = mock.MagicMock(), mock.MagicMock()
    with mock.patch.object(eng.backend, "preflight") as pre:
        with pytest.raises(OperationFailed, match="privilege refused"):
            eng._execute_op(handle, wal, node, merged, node.operations[0], mock.MagicMock())
    wal.write_intent.assert_not_called()
    pre.assert_not_called()


def test_a_permitted_bound_plan_applies(tmp_path):
    repo = _two_comp_setup(tmp_path, owner_b="alice")
    eng = _bound_engine(tmp_path, repo)
    plan = eng.plan("install", "seat-alice", None)
    assert {o.state for o in eng.apply("install", "seat-alice", None, plan_digest=plan.digest)} == {"SATISFIED"}


# --- differential: the success path emits nothing new ----------------------------------


def _events(buf):
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.startswith("{")]


def _shape(events):
    return [(e["event"], sorted(k for k in e if k not in {"ts", "run_id", "ulid", "transcript_id"})) for e in events]


def test_default_engine_and_explicit_full_ceiling_unbound_backend_are_indistinguishable(tmp_path):
    from tools.germinate.engine.backend import SIM_PRIVILEGE_CEILING

    ops = [H.dir_op("op-1", "/srv/c"), H.file_op("op-2", "/srv/c/a.conf", "hi\n")]
    repo = tmp_path / "repo"
    repo.mkdir()
    _p, digest = H.write_contract(repo, "comp-a", ops={"install": ops})
    H.make_checkout(repo, [H.row("comp-a", digest)])
    out = {}
    for name in ("default", "explicit"):
        (tmp_path / name).mkdir()
        buf = io.StringIO()
        if name == "explicit":
            be = H.sim(tmp_path / name)
            be.privilege_ceiling, be.bound_account = frozenset(ALL), None
            eng = H.engine(tmp_path / name, repo, backend=be, echo=buf, context=InvocationContext())
        else:
            eng = H.engine(tmp_path / name, repo, echo=buf)
        plan = eng.plan("install", "system", "comp-a")
        [res] = eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
        out[name] = (plan.digest, res.state, _shape(_events(buf)))
    assert out["default"] == out["explicit"]
    assert "privilege" not in json.dumps(out["default"][2])
    assert SIM_PRIVILEGE_CEILING == ALL


def test_host_substrate_root_ops_in_system_scope_still_plan_under_the_default_engine(tmp_path):
    repo = HS._repo(tmp_path)
    (tmp_path / "x").mkdir()
    plan = H.engine(tmp_path / "x", repo).plan("install", "system", HS.CID)
    assert plan.nodes and any(o.privilege == "root" for n in plan.nodes for o in n.operations)


# --- manual rollback re-checks privilege ------------------------------------------------


def _halted_run(tmp_path):
    ops = {"install": [H.dir_op("op-1", "/srv/k"), H.file_op("op-2", "/srv/k/a.conf", "x\n"), H.file_op("op-3", "/srv/k/b.conf", "y\n")]}
    _repo, eng = H.simple_setup(tmp_path, ops=ops)
    eng.backend.faults.fail = {"op-3"}
    plan = eng.plan("install", "system", "comp-a")
    with pytest.raises(EngineRefusal) as info:
        eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    assert info.value.code == "halted"
    from tools.germinate.planner_sandbox.admission import coordinator
    eng.backend.faults.fail = set()
    return eng, coordinator.read_claim(eng.backend.gates_root, "sim", "comp-a")["run_id"]


@pytest.mark.parametrize("mutate", ["ceiling", "bound"])
def test_manual_rollback_is_refused_under_a_lower_privilege_backend_with_nothing_reversed(tmp_path, mutate):
    eng, run_id = _halted_run(tmp_path)
    if mutate == "ceiling":
        eng.backend.privilege_ceiling = frozenset({"none"})
    else:
        eng.backend.bound_account = "alice"  # system scope on a bound backend admits only privilege none
    store, tup = eng._find_run(run_id)
    with store.open(tup) as handle:
        before_record = handle.read_record()
    before = H.tree_snapshot(eng.backend.root)
    with pytest.raises(EngineRefusal) as exc:
        eng.rollback(run_id)
    assert exc.value.code == "privilege-refused"
    assert H.tree_snapshot(eng.backend.root) == before  # nothing reversed
    assert eng.backend.entry("/srv/k/a.conf") is not None
    with store.open(tup) as handle:
        assert handle.read_record() == before_record  # run state unchanged (still HALTED)
        assert before_record["state"] == "HALTED"


def test_manual_rollback_under_the_same_backend_is_still_admitted(tmp_path):
    eng, run_id = _halted_run(tmp_path)
    assert eng.rollback(run_id).state == "ROLLED_BACK"
