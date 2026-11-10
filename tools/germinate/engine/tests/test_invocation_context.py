"""T-5243 (engine seam gap S6): the invocation context and execution_scope enforcement."""

from __future__ import annotations

import itertools
import json
from types import SimpleNamespace

import pytest

from tools.germinate.components.tests import test_host_substrate_component as HS
from tools.germinate.engine import context as C
from tools.germinate.engine.context import ADMITTED, CONTEXT_KINDS, InvocationContext, refusal
from tools.germinate.engine.executor import Engine, EngineRefusal
from tools.germinate.engine.tests import helpers as H

SCOPES = ("all", "staged-cutover")
PRIVS = ("none", "seat", "root")

EXPECTED = (
    {("operator", s, p) for s in SCOPES for p in PRIVS}
    | {("scheduled", "all", "none"), ("scheduled", "all", "seat")}
    | {("scheduled", "staged-cutover", "none"), ("scheduled", "staged-cutover", "seat")}
    | {("daemon", "all", "none"), ("daemon", "all", "seat")}
)


def _op(scope, priv, op_id="o"):
    return SimpleNamespace(operation_id=op_id, execution_scope=scope, privilege=priv)


# --- the table -----------------------------------------------------------------


@pytest.mark.parametrize("kind,scope,priv", list(itertools.product(CONTEXT_KINDS, SCOPES, PRIVS)))
def test_cross_product_matches_the_expected_permit_set(kind, scope, priv):
    assert (refusal(InvocationContext(kind), _op(scope, priv)) is None) == ((kind, scope, priv) in EXPECTED)


def test_admitted_is_exactly_the_expected_set():
    assert set(ADMITTED) == EXPECTED


def test_unknown_kind_is_rejected_at_construction():
    with pytest.raises(ValueError):
        InvocationContext("cron")


@pytest.mark.parametrize("kind", CONTEXT_KINDS)
@pytest.mark.parametrize("scope,priv", [("everything", "none"), ("all", "superuser"), (None, None)])
def test_unknown_scope_or_privilege_is_refused_in_every_context(kind, scope, priv):
    assert refusal(InvocationContext(kind), _op(scope, priv)) is not None


def test_default_context_is_operator():
    assert InvocationContext().kind == "operator"
    assert C.OPERATOR == "operator"


# --- real plans ----------------------------------------------------------------

def _setup(tmp_path, ops, context=None, **contract):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _p, digest = H.write_contract(repo, "comp-a", ops={"install": ops}, **contract)
    H.make_checkout(repo, [H.row("comp-a", digest)])
    return repo, H.engine(tmp_path, repo, context=context)


def _eng(base, name, repo, **kw):
    (base / name).mkdir(parents=True, exist_ok=True)
    return H.engine(base / name, repo, **kw)


def _staged_ops():
    """Seat ops marked staged-cutover: legal for the operator, not admitted for the daemon."""

    return [H.dir_op("op-1", "/srv/c", execution_scope="staged-cutover"), H.file_op("op-2", "/srv/c/a.conf", "hi\n", execution_scope="staged-cutover")]


def _assert_refused_without_side_effects(eng, plan_digest, leg="install", system="comp-a", **kw):
    before = H.tree_snapshot(eng.backend.root)
    with pytest.raises(EngineRefusal) as exc:
        eng.plan(leg, "system", system, **kw)
    assert exc.value.code == "context-refused"
    receipt = eng.preflight(leg, "system", system, plan_digest=plan_digest, **kw)
    assert receipt["verdict"] == "NO-GO"
    assert "context-refused" in json.dumps(receipt)
    with pytest.raises(EngineRefusal) as exc:
        eng.apply(leg, "system", system, plan_digest=plan_digest, **kw)
    assert exc.value.code == "context-refused"
    assert H.tree_snapshot(eng.backend.root) == before  # no run dir, no claim, no tree change


def test_daemon_plus_staged_cutover_refuses_plan_preflight_and_apply(tmp_path):
    repo, op_eng = _setup(tmp_path / "o", _staged_ops())
    plan = op_eng.plan("install", "system", "comp-a")  # the operator may plan it
    _assert_refused_without_side_effects(_eng(tmp_path, "d", repo, context=InvocationContext("daemon", "alice")), plan.digest)


def test_a_mixed_plan_is_refused_whole(tmp_path):
    ops = [H.dir_op("op-1", "/srv/c"), H.file_op("op-2", "/srv/c/a.conf", "hi\n", execution_scope="staged-cutover")]
    repo, _ = _setup(tmp_path / "o", ops)
    eng = _eng(tmp_path, "d", repo, context=InvocationContext("daemon"))
    before = H.tree_snapshot(eng.backend.root)
    with pytest.raises(EngineRefusal) as exc:
        eng.plan("install", "system", "comp-a")
    assert exc.value.code == "context-refused" and "op-2" in exc.value.message
    assert H.tree_snapshot(eng.backend.root) == before


def test_permitted_ops_plan_and_apply_under_scheduled(tmp_path):
    repo, _ = _setup(tmp_path / "o", [H.dir_op("op-1", "/srv/c", owner="alice"), H.file_op("op-2", "/srv/c/a.conf", "hi\n", owner="alice")])  # C6: owner matches the invoking account
    eng = _eng(tmp_path, "s", repo, context=InvocationContext("scheduled", "alice"))
    plan = eng.plan("install", "system", "comp-a")
    [out] = eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    assert out.state == "SATISFIED"


# --- host-substrate ------------------------------------------------------------


def test_scheduled_staged_cutover_root_is_not_admitted():
    """HO ruling 2026-11-03 (strict A-3), pinned at the table and at refusal()."""

    assert ("scheduled", "staged-cutover", "root") not in ADMITTED
    assert not any(k == "scheduled" and p == "root" for k, _s, p in ADMITTED)
    assert refusal(InvocationContext("scheduled"), _op("staged-cutover", "root")) is not None


def test_host_substrate_update_leg_staged_root_is_refused_under_scheduled(tmp_path):
    """HO ruling 2026-11-03 (strict ORG-080 A-3): the update leg stages root ops under
    staged-cutover; a SCHEDULED run never actuates root, so the whole plan is refused in
    _plan with nothing written. The operator still plans it (digest unaffected)."""

    repo = HS._repo(tmp_path)
    revs = ("a" * 40, "b" * 40)
    plan = _eng(tmp_path, "op", repo).plan("update", "system", HS.CID, source_revs=revs)
    eng = _eng(tmp_path, "sch", repo, context=InvocationContext("scheduled", "alice"))
    _assert_refused_without_side_effects(eng, plan.digest, leg="update", system=HS.CID, source_revs=revs)


@pytest.mark.parametrize("kind", ["scheduled", "daemon"])
def test_host_substrate_install_leg_root_all_is_refused_without_side_effects(tmp_path, kind):
    repo = HS._repo(tmp_path)
    plan = _eng(tmp_path, "op", repo).plan("install", "system", HS.CID)
    eng = _eng(tmp_path, "x", repo, context=InvocationContext(kind))
    _assert_refused_without_side_effects(eng, plan.digest, system=HS.CID)
    with pytest.raises(EngineRefusal) as exc:
        eng.plan("install", "system", HS.CID)
    assert kind in exc.value.message


def test_host_substrate_update_leg_is_refused_under_daemon(tmp_path):
    repo = HS._repo(tmp_path)
    revs = ("a" * 40, "b" * 40)
    plan = _eng(tmp_path, "op", repo).plan("update", "system", HS.CID, source_revs=revs)
    _assert_refused_without_side_effects(_eng(tmp_path, "d", repo, context=InvocationContext("daemon")), plan.digest, leg="update", system=HS.CID, source_revs=revs)


# --- manual rollback / reconcile -------------------------------------------------


def _halted_staged_run(tmp_path):
    """An operator run of staged-cutover ops that HALTS (an unmanaged conflicting file): rollback-admissible."""

    repo, eng = _setup(tmp_path, _staged_ops())
    plan = eng.plan("install", "system", "comp-a")
    eng.backend.seed_file("/srv/c/a.conf", "someone else's file\n")
    with pytest.raises(EngineRefusal) as exc:
        eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    run_id = exc.value.message.split("run ")[1].split(" ")[0]
    return repo, eng, run_id


def _other(eng, repo, kind):
    other = Engine.from_checkout(eng.backend.root, repo_root=repo, context=InvocationContext(kind))
    other.backend.simulate_postboot()
    return other


def test_manual_rollback_refused_when_a_recorded_op_is_not_admitted(tmp_path):
    repo, eng, run_id = _halted_staged_run(tmp_path)
    with pytest.raises(EngineRefusal) as exc:
        _other(eng, repo, "daemon").rollback(run_id)
    assert exc.value.code == "context-refused"
    assert eng.rollback(run_id).state == "ROLLED_BACK"  # nothing moved: the operator can still undo it


def test_manual_rollback_is_allowed_when_every_recorded_op_is_admitted(tmp_path):
    repo, eng, run_id = _halted_staged_run(tmp_path)
    assert _other(eng, repo, "scheduled").rollback(run_id).state == "ROLLED_BACK"


def test_reconcile_run_refused_under_any_non_operator_context(tmp_path):
    repo, eng, run_id = _halted_staged_run(tmp_path)
    for kind in ("scheduled", "daemon"):
        with pytest.raises(EngineRefusal) as exc:
            _other(eng, repo, kind).reconcile_run(run_id, executor_dead=True)
        assert exc.value.code == "context-refused"


# --- operator output is unchanged --------------------------------------------------


def test_operator_transcript_has_no_context_event_and_run_admitted_keys_are_unchanged(tmp_path, capsys):
    import io

    repo, _ = _setup(tmp_path / "o", [H.dir_op("op-1", "/srv/c")])
    buf = io.StringIO()
    eng = _eng(tmp_path, "r", repo, echo=buf)
    plan = eng.plan("install", "system", "comp-a")
    eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.startswith("{")]
    assert "context.admitted" not in {e["event"] for e in events}
    [admitted] = [e for e in events if e["event"] == "run.admitted"]
    assert "invocation_context" not in admitted


def test_non_operator_transcript_records_the_context(tmp_path):
    import io

    repo, _ = _setup(tmp_path / "o", [H.dir_op("op-1", "/srv/c", owner="alice")])
    buf = io.StringIO()
    eng = _eng(tmp_path, "r", repo, echo=buf, context=InvocationContext("scheduled", "alice"))
    plan = eng.plan("install", "system", "comp-a")
    eng.apply("install", "system", "comp-a", plan_digest=plan.digest)
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.startswith("{")]
    ctx = [e for e in events if e["event"] == "context.admitted"]
    assert ctx and ctx[0]["kind"] == "scheduled" and ctx[0]["account"] == "alice"
    [admitted] = [e for e in events if e["event"] == "run.admitted"]
    assert admitted["invocation_context"] == {"kind": "scheduled", "account": "alice"}
