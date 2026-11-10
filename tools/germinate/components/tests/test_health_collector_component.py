"""T-5102: the health-collector component, an adapter over tools/health_collector.py and
tools/health_watcher.py (both unchanged).

Every engine-tier test runs on a tmp checkout (copies of the four component files, the three
tools/systemd unit templates and a fixture registry) over a simulated host. The roster is this
checkout's instance.yaml through health_export.fleet_personas(), exactly what the live collector
reads. host-substrate is a labelled STUB with an empty install (the out dir it will own is T-6011).

TEST HELPERS (labelled; no lifecycle verb does these):
* ``_seed_out_dir`` writes the root-provisioned /run/pantheon-health facts;
* ``_seed_running`` writes the collector as Recovery hand-installed it (units at their templates,
  timer enabled, watcher active) plus a fresh aggregate;
* ``_aggregate`` is what the REAL collector code writes (``health_collector.collect``) over the
  captured emitter replies (T-5410's PersonaController1 GetAll, the captured busctl denied /
  no_service stderr), reproducing Recovery's live 2026-11-02 reading: 10 entries, exactly
  host-approval, host-broker and persona-controller.codex unreachable.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import random
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tools import health_collector as hc
from tools.germinate.engine.backend import KILL_POINTS, SimulatedKill
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.invariant_digest import digest_of
from tools.germinate.engine.run_state import RunStore
from tools.germinate.engine.tests import helpers as H
from tools.germinate.planner_sandbox.admission import coordinator
from tools.germinate.registry.contract_digest import compute_contract_digest
from tools.germinate.registry.repin import repin
from tools.health_export import fleet_personas

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPONENTS = REPO_ROOT / "tools" / "germinate" / "components"
FILES = ("health-collector.yaml", "health-collector.py", "health-collector_handlers.py", "health-collector.invariants.yaml",
         "health-collector.subscriptions.yaml")
PC_EMITTER = {"bus": "session", "interface": "org.pantheon.PersonaController1"}
UNITS = ("pantheon-health-collector.service", "pantheon-health-collector.timer", "pantheon-health-watcher.service")
CID = "health-collector"
SCOPE = "system"
KIND = "health-collector.adopt-attest"
GATE = "health-collector-retire"
NOW = 1_800_000_000
EXPECTED_UNREACHABLE = {"host-approval", "host-broker", "persona-controller.codex"}
FIXTURE_DIR = REPO_ROOT / "tools" / "fixtures" / "health_collector"
PC_TODAY = (FIXTURE_DIR / "persona_getall_flag_off.json").read_text(encoding="utf-8")


class _LiveLikeReader:
    """Captured replies arranged as Recovery's live 2026-11-02 reading (the 3 unreachable entries)."""

    def __init__(self, unreachable_personas=("codex",)):
        self.unreachable = set(unreachable_personas)

    def getall_system(self, bus_name, path, interface):
        if bus_name == "org.pantheon.HostBroker1":
            return hc.classify_stderr("Call failed: Access denied"), None
        return hc.classify_stderr("Call failed: The name org.x was not provided by any .service files"), None

    def getall_session(self, account, persona, interface):
        return ("unreachable", None) if persona in self.unreachable else ("ok", PC_TODAY)


def _aggregate(now=NOW, unreachable_personas=("codex",)):
    return hc.collect(fleet_personas(), reader=_LiveLikeReader(unreachable_personas), now=now, mode="signal")


def _template(unit):
    import hashlib

    return hashlib.sha256((REPO_ROOT / "tools" / "systemd" / unit).read_bytes()).hexdigest()


SPEC = {
    "pantheon-health-collector.service": {"enabled": False, "active": False},
    "pantheon-health-collector.timer": {"enabled": True, "active": True},
    "pantheon-health-watcher.service": {"enabled": True, "active": True},
}


def _repo(tmp_path: Path, extra_contracts=()) -> Path:
    repo = tmp_path / "repo"
    directory = repo / "tools" / "germinate" / "components"
    directory.mkdir(parents=True)
    for name in FILES:
        shutil.copy(COMPONENTS / name, directory / name)
    (repo / "tools" / "systemd").mkdir(parents=True)
    for unit in UNITS:
        shutil.copy(REPO_ROOT / "tools" / "systemd" / unit, repo / "tools" / "systemd" / unit)
    # STUB (labelled): harness-manager's PersonaController1 emitter declaration is what fans the
    # persona entries out (T-5980); an extra_contracts entry of the same id overrides it.
    stub_hm = H.contract_body("harness-manager", emitter=PC_EMITTER)
    for cid, body in [("harness-manager", stub_hm), *extra_contracts]:
        (directory / f"{cid}.yaml").write_text(yaml.safe_dump({"component": body}), encoding="utf-8")
    digest = compute_contract_digest(directory / "health-collector.yaml")
    # STUB (labelled): host-substrate with an empty system install stands in for the real one
    _p, hs_digest = H.write_contract(repo, "host-substrate", ops={"install": []}, scopes=["system"])
    # STUB (labelled): the sidecar's registry_component must be a registry system (derive registry_ids)
    _p, ab_digest = H.write_contract(repo, "action-broker", ops={"install": []}, scopes=["system"])
    H.make_checkout(repo, [H.row(CID, digest, steward="recovery"), H.row("host-substrate", hs_digest),
                           H.row("action-broker", ab_digest)])
    return repo


def _handler(eng):
    return eng.backend.handler(KIND)


def _state(eng):
    return _handler(eng).dump_state()


def _write(eng, mutate) -> None:
    """TEST HELPER: edit the simulated organ in place and persist it."""

    state = _state(eng)
    mutate(state)
    _handler(eng).load_state(state)
    eng.backend.save()


def _seed_out_dir(state) -> None:
    """TEST HELPER (labelled): the root tmpfiles out dir (host-substrate's once T-6011 lands)."""

    state["out_dir"] = {"present": True, "mode": "2750", "group": "pantheon-health"}


def _seed_running(state) -> None:
    """TEST HELPER: the collector as Recovery hand-installed it, plus a fresh aggregate."""

    _seed_out_dir(state)
    state["units"] = {u: {"sha256": _template(u), **SPEC[u]} for u in UNITS}
    state["aggregate"] = _aggregate()
    state["now"] = NOW + 30


def _engine(tmp_path, repo, seed=None):
    eng = H.engine(tmp_path, repo)
    if seed is not None:
        _write(eng, seed)
    stub = eng.plan("install", SCOPE, "host-substrate")
    [outcome] = eng.apply("install", SCOPE, "host-substrate", plan_digest=stub.digest)
    assert outcome.state == "SATISFIED"
    return eng


@pytest.fixture
def running(tmp_path):
    repo = _repo(tmp_path)
    return repo, _engine(tmp_path, repo, _seed_running)


def _ops(plan):
    [node] = plan.nodes
    return node.operations


def _apply(eng, leg, **kwargs):
    plan = eng.plan(leg, SCOPE, CID)
    [outcome] = eng.apply(leg, SCOPE, CID, plan_digest=plan.digest, **kwargs)
    return plan, outcome


def _receipt(eng, run_id, op_id):
    store = RunStore(eng.backend.run_state_root(SCOPE))
    with store.open(store.find(run_id)) as handle:
        wal = handle.wal()
        ref = wal.classify_prefix(op_id=op_id).receipt_ref
        receipt, _ = wal.read_receipt_by_ref(ref)
        return receipt.receipt_payload


def _invariants(eng):
    [result] = eng.verify(SCOPE, CID)
    return result, {r["id"]: r for r in result["invariants"]["results"]}


def _list_doc():
    return yaml.safe_load((COMPONENTS / "health-collector.invariants.yaml").read_text(encoding="utf-8"))


def _handlers_module(handler):
    """The engine-loaded handlers module (loaded by path, not registered in sys.modules)."""

    from types import SimpleNamespace

    return SimpleNamespace(VERIFY=type(handler).__init__.__globals__["VERIFY"])


def _verify(observed_state, eng):
    """VERIFY over a handler observation of ``observed_state`` (state left as it was)."""

    handler = _handler(eng)
    saved = handler.dump_state()
    handler.load_state(observed_state)
    try:
        mod = _handlers_module(handler)
        out = mod.VERIFY(_list_doc(), {CID: handler.observe(CID, SCOPE)}, SCOPE)
    finally:
        handler.load_state(saved)
    return out["state"], {r["id"]: r for r in out["results"]}


# --- observe-and-attest: the brownfield install path ------------------------------


def test_running_collector_plans_one_attest_op(running):
    _repo_, eng = running
    [op] = _ops(eng.plan("install", SCOPE, CID))
    assert op.kind == KIND and op.target == "attestation:health-collector"
    assert op.desired["verify_rc"] == "0" and op.desired["aggregate_status"] == "fresh"
    assert op.provenance["re_attest"] == "false"


def test_attest_applies_satisfied_and_rerun_plans_nothing(running):
    _repo_, eng = running
    _plan, outcome = _apply(eng, "install")
    assert outcome.state == "SATISFIED"
    assert _ops(eng.plan("install", SCOPE, CID)) == ()
    result, inv = _invariants(eng)
    assert result["ok"], result
    assert inv["HC8-reachability"]["status"] == "pass"
    assert result["invariants"]["state"] == "ready"


def test_receipt_records_the_attested_evidence(running):
    _repo_, eng = running
    plan, outcome = _apply(eng, "install")
    [op] = _ops(plan)
    payload = json.dumps(_receipt(eng, outcome.run_id, op.operation_id), sort_keys=True)
    assert op.desired["units_sha256"] in payload


def test_healthy_drift_re_attests_and_verify_flags_it(running):
    _repo_, eng = running
    _apply(eng, "install")
    # the timer-driven oneshot is mid-run: a healthy fact change that moves the evidence
    _write(eng, lambda s: s["units"]["pantheon-health-collector.service"].update(active=True))
    result, _inv = _invariants(eng)
    assert not result["ok"] and any("drift" in c["detail"] for c in result["checks"])
    [op] = _ops(eng.plan("install", SCOPE, CID))
    assert op.kind == KIND and op.provenance["re_attest"] == "true"
    _apply(eng, "install")
    assert eng.verify(SCOPE, CID)[0]["ok"]


def test_stale_aggregate_fails_hc6_and_is_never_re_attested(running):
    _repo_, eng = running
    _apply(eng, "install")
    _write(eng, lambda s: s.update(now=NOW + 181))
    result, inv = _invariants(eng)
    assert not result["ok"] and inv["HC6-aggregate-fresh"]["status"] == "fail"
    assert result["invariants"]["state"] == "unhealthy"
    with pytest.raises(EngineRefusal, match="UNHEALTHY"):
        eng.plan("install", SCOPE, CID)


def test_hc6_threshold_is_180s_inclusive(running):
    _repo_, eng = running
    base = _state(eng)
    for now, want in ((NOW + 180, "pass"), (NOW + 181, "fail")):
        state, inv = _verify(dict(base, now=now), eng)
        assert inv["HC6-aggregate-fresh"]["status"] == want, (now, inv["HC6-aggregate-fresh"])
    assert next(e for e in _list_doc()["invariants"] if e["id"] == "HC6-aggregate-fresh")["max_age_secs"] == 180


def test_hc5_hc7_fail_on_a_missing_or_malformed_aggregate(running):
    _repo_, eng = running
    base = _state(eng)
    for agg in (None, dict(_aggregate(), schema="other")):
        state, inv = _verify(dict(base, aggregate=agg), eng)
        for iid in ("HC5-aggregate-shape", "HC6-aggregate-fresh", "HC7-collector-mode", "HC8-reachability"):
            assert inv[iid]["status"] == "fail", (agg is None, iid)
        assert state == "unhealthy"


# --- HC-8: the expected-unreachable set (Recovery, 2026-11-02) ----------------------------


def test_hc8_passes_with_exactly_the_expected_set_unreachable(running):
    _repo_, eng = running
    agg = _state(eng)["aggregate"]
    unreachable = {e["component_id"] for e in agg["entries"] if e["reachability"] != "ok"}
    assert unreachable == EXPECTED_UNREACHABLE and len(agg["entries"]) == 10
    _state_, inv = _verify(_state(eng), eng)
    assert inv["HC8-reachability"]["status"] == "pass"


def test_hc8_fails_on_an_unlisted_unreachable_component(running):
    _repo_, eng = running
    state = dict(_state(eng), aggregate=_aggregate(unreachable_personas=("codex", "recovery")))
    verdict, inv = _verify(state, eng)
    assert inv["HC8-reachability"]["status"] == "fail"
    assert "persona-controller.recovery" in inv["HC8-reachability"]["detail"]
    assert verdict == "degraded"


def test_hc8_fails_when_a_listed_component_leaves_the_subscription_set(running):
    _repo_, eng = running
    agg = _aggregate()
    agg["entries"] = [e for e in agg["entries"] if e["component_id"] != "host-approval"]
    agg["collector"]["entry_count"] = len(agg["entries"])
    verdict, inv = _verify(dict(_state(eng), aggregate=agg), eng)
    assert inv["HC5-aggregate-shape"]["status"] == "pass"
    assert inv["HC8-reachability"]["status"] == "fail"
    assert "missing from the subscription set: host-approval" in inv["HC8-reachability"]["detail"]


def test_hc8_listed_entries_carry_a_reason_and_an_existing_ticket():
    """Existence only, never "open": main must not go red when T-2920 / T-5992 resolve (r1 F6)."""

    hc8 = next(e for e in _list_doc()["invariants"] if e["id"] == "HC8-reachability")
    rows = hc8["expected_unreachable"]
    assert {r["component_id"] for r in rows} == EXPECTED_UNREACHABLE
    for row in rows:
        assert isinstance(row["reason"], str) and row["reason"].strip(), row
        assert re.fullmatch(r"T-\d{4,}", row["ticket"]), row
        hits = [p for state in ("inbox", "active", "done") for p in (REPO_ROOT / "tickets" / state).glob(f"{row['ticket']}.md")]
        assert hits, row
    assert {r["component_id"]: r["ticket"] for r in rows}["persona-controller.codex"] == "T-5992"


def test_hc8_fails_degraded_when_a_listed_component_is_reachable(running):
    """Recovery's "exactly that set": a listed entry that answers is a stale list entry (r1 F2)."""

    _repo_, eng = running
    state = dict(_state(eng), aggregate=_aggregate(unreachable_personas=()))   # codex now answers
    verdict, inv = _verify(state, eng)
    assert inv["HC8-reachability"]["status"] == "fail"
    assert "persona-controller.codex is reachable; remove it from expected_unreachable" in inv["HC8-reachability"]["detail"]
    assert verdict == "degraded"


# --- refusals ---------------------------------------------------------------------


def test_absent_out_dir_refuses_naming_the_root_step(tmp_path):
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, repo, lambda s: (_seed_running(s), s.update(out_dir={"present": False, "mode": None, "group": None})))
    with pytest.raises(EngineRefusal, match="T-6011"):
        eng.plan("install", SCOPE, CID)


def test_wrong_out_dir_mode_refuses(tmp_path):
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, repo, lambda s: (_seed_running(s), s["out_dir"].update(mode="0755")))
    with pytest.raises(EngineRefusal, match="never installed through this component"):
        eng.plan("install", SCOPE, CID)


def test_dependency_on_host_substrate_is_required(tmp_path):
    repo = _repo(tmp_path)
    eng = H.engine(tmp_path, repo)
    _write(eng, _seed_running)
    with pytest.raises(EngineRefusal) as info:
        eng.plan("install", SCOPE, CID)
    assert info.value.code == "dependency-not-installed"


def test_unhealthy_legacy_collector_refuses_naming_the_supervised_path(tmp_path):
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, repo, lambda s: (_seed_running(s), s["units"]["pantheon-health-watcher.service"].update(active=False)))
    with pytest.raises(EngineRefusal, match="hand-installed") as info:
        eng.plan("install", SCOPE, CID)
    assert "HC3-watcher-active" in str(info.value)


def test_undeclared_legs_refuse(running):
    _repo_, eng = running
    with pytest.raises(EngineRefusal) as info:
        eng.plan("repair", SCOPE, CID)
    assert info.value.code == "leg-not-declared"


# --- greenfield -------------------------------------------------------------------


def test_greenfield_plans_unit_installs_then_converges(tmp_path):
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, repo, _seed_out_dir)
    plan, outcome = _apply(eng, "install")
    assert outcome.state == "SATISFIED"
    assert sorted(op.target for op in _ops(plan)) == sorted(f"unit:{u}" for u in UNITS)
    state = _state(eng)
    assert all(state["units"][u] == {"sha256": _template(u), **SPEC[u]} for u in UNITS)
    # TEST HELPER: the watcher's startup collect writes the first aggregate
    _write(eng, lambda s: s.update(aggregate=_aggregate(), now=NOW + 5))
    assert _ops(eng.plan("install", SCOPE, CID)) == ()   # engine-owned and healthy
    assert eng.verify(SCOPE, CID)[0]["ok"]


# --- configure: the subscription set ------------------------------------------------


def test_configure_attests_the_subscription_then_reruns_empty(running):
    _repo_, eng = running
    plan, outcome = _apply(eng, "configure")
    assert outcome.state == "SATISFIED"
    [op] = _ops(plan)
    assert op.kind == "health-collector.subscription-attest"
    assert op.desired["entry_count"] == "10" and op.desired["declared"] == "harness-manager"
    assert _ops(eng.plan("configure", SCOPE, CID)) == ()
    assert eng.verify(SCOPE, CID)[0]["ok"]


def test_configure_attests_a_declared_health1_emitter_with_endpoints(tmp_path):
    """T-5980: a newly declared emitter is attested (configure configures), not refused."""

    emitter = {"bus": "system", "interface": "org.pantheon.Health1",
               "endpoints": [{"component_id": "mailbox-health", "bus_name": "org.pantheon.Mailbox1",
                              "object_path": "/org/pantheon/Mailbox1/Health"}]}
    body = H.contract_body("mailbox", emitter=emitter)
    repo = _repo(tmp_path, extra_contracts=[("mailbox", body)])
    eng = _engine(tmp_path, repo, _seed_running)
    sub = _handler(eng).context()["subscription"]
    assert "mailbox-health|mailbox|system" in sub["entries"] and sub["unsubscribable"] == []
    [op] = _ops(eng.plan("configure", SCOPE, CID))
    assert op.desired["entry_count"] == "11"
    assert op.desired["declared"] == "harness-manager,mailbox"


def test_configure_refuses_a_health1_emitter_without_endpoints(tmp_path):
    body = H.contract_body("mailbox", emitter={"bus": "system", "interface": "org.pantheon.Health1"})
    repo = _repo(tmp_path, extra_contracts=[("mailbox", body)])
    eng = _engine(tmp_path, repo, _seed_running)
    with pytest.raises(EngineRefusal, match="endpoints") as info:
        eng.plan("configure", SCOPE, CID)
    assert "mailbox" in str(info.value) and "T-5980" not in str(info.value)


def test_a_declared_emitter_the_collector_covers_is_accepted(tmp_path):
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, repo, _seed_running)
    [op] = _ops(eng.plan("configure", SCOPE, CID))
    assert op.desired["declared"] == "harness-manager"


def test_subscription_differential_against_the_collectors_own_reads(running):
    """Differential A: the set configure attests == the emitters collect() actually calls."""

    _repo_, eng = running
    entries = _handler(eng).context()["subscription"]["entries"]

    class Recording:
        calls = []

        def getall_system(self, bus_name, path, interface):
            Recording.calls.append(("system", bus_name))
            return "unreachable", None

        def getall_session(self, account, persona, interface):
            Recording.calls.append(("session", persona))
            return "unreachable", None

    hc.collect(fleet_personas(), reader=Recording(), now=0)
    by_bus = {"org.pantheon.HostApproval1": "host-approval", "org.pantheon.HostBroker1": "host-broker"}
    called = {by_bus[n] if kind == "system" else f"persona-controller.{n}" for kind, n in Recording.calls}
    assert {e.split("|")[0] for e in entries} == called
    assert {p for p, _a, _t in fleet_personas()} <= {c.removeprefix("persona-controller.") for c in called}


# --- update -------------------------------------------------------------------------


def test_update_converges_a_changed_template_and_re_attests(running):
    repo, eng = running
    _apply(eng, "install")
    watcher = repo / "tools" / "systemd" / "pantheon-health-watcher.service"
    watcher.write_text(watcher.read_text(encoding="utf-8") + "\n# T-5102 test: a template change\n", encoding="utf-8")
    import hashlib

    new_sha = hashlib.sha256(watcher.read_bytes()).hexdigest()
    eng = H.engine(repo.parent, repo)   # a fresh engine (same simulated host) reads the new template
    _result, inv = _invariants(eng)
    assert inv["HC1-unit-templates"]["status"] == "fail"
    plan, outcome = _apply(eng, "update")
    assert outcome.state == "SATISFIED"
    kinds = [op.kind for op in _ops(plan)]
    assert kinds == ["health-collector.unit-install", KIND]
    assert _ops(plan)[0].desired == {"unit": "pantheon-health-watcher.service", "sha256": new_sha, "enabled": "true", "restart": "true"}
    assert _state(eng)["units"]["pantheon-health-watcher.service"]["sha256"] == new_sha
    assert eng.verify(SCOPE, CID)[0]["ok"]
    assert _ops(eng.plan("install", SCOPE, CID)) == ()


def test_update_over_a_stale_aggregate_plans_units_only_and_install_re_attests_once_fresh(running):
    """r1 F1: unhealthy converged evidence is never attested by update; the unit fix lands."""

    repo, eng = running
    _apply(eng, "install")
    attested = _state(eng)["attestation"]
    watcher = repo / "tools" / "systemd" / "pantheon-health-watcher.service"
    watcher.write_text(watcher.read_text(encoding="utf-8") + "\n# T-5102 r1 F1: a template change\n", encoding="utf-8")
    import hashlib

    new_sha = hashlib.sha256(watcher.read_bytes()).hexdigest()
    eng = H.engine(repo.parent, repo)
    _write(eng, lambda s: s.update(now=NOW + 181))           # the aggregate is stale: HC-6 fails
    plan, outcome = _apply(eng, "update")
    assert [op.kind for op in _ops(plan)] == ["health-collector.unit-install"]
    assert outcome.state == "SATISFIED"
    state = _state(eng)
    assert state["units"]["pantheon-health-watcher.service"]["sha256"] == new_sha   # the unit fix stayed
    assert state["attestation"] == attested                                          # nothing attested
    with pytest.raises(EngineRefusal, match="UNHEALTHY"):
        eng.plan("install", SCOPE, CID)                       # still stale: never re-attested
    # TEST HELPER: the collector writes a fresh aggregate again
    _write(eng, lambda s: s.update(aggregate=_aggregate(now=NOW + 200), now=NOW + 210))
    [op] = _ops(eng.plan("install", SCOPE, CID))
    assert op.kind == KIND and op.provenance["re_attest"] == "true"
    _apply(eng, "install")
    assert eng.verify(SCOPE, CID)[0]["ok"]


def _change_template(repo, unit):
    import hashlib

    path = repo / "tools" / "systemd" / unit
    path.write_text(path.read_text(encoding="utf-8") + f"\n# T-5102 r2: {unit} template change\n", encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "changed, fault",
    [
        # (A) the watcher at its template bytes but STOPPED, plus a collector.service template change
        ("pantheon-health-collector.service", ("pantheon-health-watcher.service", "active", False)),
        # (B) the timer DISABLED, plus a watcher template change
        ("pantheon-health-watcher.service", ("pantheon-health-collector.timer", "enabled", False)),
    ],
    ids=["A-stopped-watcher", "B-disabled-timer"],
)
def test_update_converges_enabled_and_active_not_only_bytes(running, changed, fault):
    """r2 F1 (reviewer probe A / B): a non-byte drift is converged by update, and the attest the
    plan carries is the one apply observes, so the run is SATISFIED, never HALTED."""

    repo, eng = running
    _apply(eng, "install")
    new_sha = _change_template(repo, changed)
    eng = H.engine(repo.parent, repo)
    unit, field, value = fault
    _write(eng, lambda s: s["units"][unit].update({field: value}))
    plan, outcome = _apply(eng, "update")
    targets = [op.target for op in _ops(plan)]
    assert f"unit:{changed}" in targets and f"unit:{unit}" in targets and targets[-1] == "attestation:health-collector"
    assert outcome.state == "SATISFIED"
    state = _state(eng)
    assert state["units"][changed]["sha256"] == new_sha
    assert state["units"][unit] == {"sha256": _template(unit), **SPEC[unit]}
    assert state["attestation"] == {k: _ops(plan)[-1].desired[k] for k in ("units_sha256", "out_dir", "aggregate_status", "verify_rc")}
    assert eng.verify(SCOPE, CID)[0]["ok"]


_UNIT_STATES = [(drifted, enabled, active) for drifted in (False, True) for enabled in (False, True) for active in (False, True)]
_GRID = [(units, fresh) for units in itertools.product(_UNIT_STATES, repeat=len(UNITS)) for fresh in (True, False)]
_SAMPLE = random.Random(5102).sample(_GRID, 40)   # 1024 cases is too slow for the engine tier; seed listed


@pytest.mark.parametrize("units, fresh", _SAMPLE, ids=[f"case{i}" for i in range(len(_SAMPLE))])
def test_update_attest_property(tmp_path, units, fresh):
    """r2 F1 property, over (bytes current/drifted x enabled y/n x active y/n) per unit x aggregate
    fresh/stale: whenever plan_update includes an attest op, apply never HALTs and the attestation
    is that op's evidence; whenever it does not, apply attests nothing."""

    def mutate(state):
        for unit, (drifted, enabled, active) in zip(UNITS, units):
            state["units"][unit] = {"sha256": "f" * 64 if drifted else _template(unit), "enabled": enabled, "active": active}
        state["now"] = NOW + 30 if fresh else NOW + 181

    _check_update_attest(tmp_path, mutate, want_attest=fresh and not _is_noop(units))


def _is_noop(units):
    """True when every unit is already at spec (no unit op, so nothing to attest)."""

    return all(not drifted and (enabled, active) == (SPEC[u]["enabled"], SPEC[u]["active"]) for u, (drifted, enabled, active) in zip(UNITS, units))


def _check_update_attest(tmp_path, mutate, want_attest):
    """The r2 F1 property on one mutated state: when plan_update includes an attest op, apply never
    HALTs (a HALT raises EngineRefusal) and the attestation is that op's evidence; when it does not,
    apply attests nothing. ``want_attest`` pins the expected plan shape."""

    repo = _repo(tmp_path)
    eng = _engine(tmp_path, repo, _seed_running)
    _apply(eng, "install")
    attested = _state(eng)["attestation"]
    _write(eng, mutate)
    present = [u for u in UNITS if _state(eng)["units"][u]["sha256"]]
    plan = eng.plan("update", SCOPE, CID)
    ops = _ops(plan)
    has_attest = any(op.kind == KIND for op in ops)
    assert has_attest == want_attest
    if not ops:
        return
    [outcome] = eng.apply("update", SCOPE, CID, plan_digest=plan.digest)   # a HALT raises EngineRefusal
    assert outcome.state == "SATISFIED"
    state = _state(eng)
    assert all(state["units"][u] == {"sha256": _template(u), **SPEC[u]} for u in present)   # absent units are install's job
    if has_attest:
        assert state["attestation"] == {k: ops[-1].desired[k] for k in ("units_sha256", "out_dir", "aggregate_status", "verify_rc")}
        assert eng.verify(SCOPE, CID)[0]["ok"]
    else:
        assert state["attestation"] == attested


def _drift_watcher(state):
    state["units"]["pantheon-health-watcher.service"]["sha256"] = "f" * 64


def _absent(*units):
    def mutate(state):
        for unit in units:
            state["units"][unit] = {"sha256": None, "enabled": False, "active": False}
    return mutate


def _aggregate_is(aggregate):
    def mutate(state):
        state["aggregate"] = aggregate
    return mutate


def _both(*mutations):
    def mutate(state):
        for m in mutations:
            m(state)
    return mutate


# (id, mutation, plans an attest): each case but the absent-only ones drifts the watcher bytes, so
# update has a unit op to plan and the aggregate/unit fault is what decides the attest
_FIXED = [
    ("baseline-drift-attests", _drift_watcher, True),
    ("absent-unit-with-drift", _both(_drift_watcher, _absent("pantheon-health-collector.timer")), False),
    ("absent-unit-alone", _absent("pantheon-health-collector.timer"), False),
    ("all-units-absent", _absent(*UNITS), False),
    ("aggregate-missing", _both(_drift_watcher, _aggregate_is(None)), False),
    ("aggregate-malformed", _both(_drift_watcher, _aggregate_is(dict(_aggregate(), schema="other"))), False),
    ("hc8-unlisted-unreachable", _both(_drift_watcher, _aggregate_is(_aggregate(unreachable_personas=("codex", "recovery")))), False),
    ("hc8-listed-reachable", _both(_drift_watcher, _aggregate_is(_aggregate(unreachable_personas=()))), False),
]


@pytest.mark.parametrize("mutate, want_attest", [(m, w) for _i, m, w in _FIXED], ids=[i for i, _m, _w in _FIXED])
def test_update_attest_property_fixed_cases(tmp_path, mutate, want_attest):
    """Deterministic complement to the seeded sample: absent units, a missing or malformed aggregate
    and both HC-8 faults. A plan with an attest never HALTs; one without attests nothing."""

    _check_update_attest(tmp_path, mutate, want_attest)


def _planner():
    spec = importlib.util.spec_from_file_location("_t5102_planner", COMPONENTS / "health-collector.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_planner_prediction_matches_the_handlers_execution(running):
    """Differential: apply_unit_ops is the handler's _result for every planned unit op, and the
    planner's units_sha256 encoding is the handler's evidence encoding."""

    _repo_, eng = running
    planner, handler = _planner(), _handler(eng)
    _write(eng, lambda s: (s["units"]["pantheon-health-watcher.service"].update(active=False, sha256="f" * 64)))
    obs = handler.observe(CID, SCOPE)
    assert planner._units_sha256(obs["units"]) == obs["evidence"]["units_sha256"]
    for unit in UNITS:
        op = planner._unit_install(obs, unit, "update")
        after = planner.apply_unit_ops(obs["units"], [op])
        assert after[unit] == handler._result(SimpleNamespace(kind=op["kind"], desired=op["desired"]))


def _remove_template(repo, unit):
    (repo / "tools" / "systemd" / unit).unlink()


def test_update_with_a_missing_template_refuses_clearly(running):
    repo, eng = running
    _apply(eng, "install")
    _remove_template(repo, "pantheon-health-watcher.service")
    eng = H.engine(repo.parent, repo)
    with pytest.raises(EngineRefusal, match=r"template missing: pantheon-health-watcher\.service \(tools/systemd/pantheon-health-watcher\.service\)"):
        eng.plan("update", SCOPE, CID)


def test_greenfield_install_with_a_missing_template_refuses_clearly(tmp_path):
    repo = _repo(tmp_path)
    _remove_template(repo, "pantheon-health-collector.timer")
    eng = _engine(tmp_path, repo, _seed_out_dir)
    with pytest.raises(EngineRefusal, match=r"template missing: pantheon-health-collector\.timer \(tools/systemd/pantheon-health-collector\.timer\)"):
        eng.plan("install", SCOPE, CID)


def test_update_with_nothing_installed_refuses(tmp_path):
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, repo, _seed_running)
    with pytest.raises(EngineRefusal, match="run install first"):
        eng.plan("update", SCOPE, CID)


def test_update_with_templates_current_plans_nothing(running):
    _repo_, eng = running
    _apply(eng, "install")
    assert _ops(eng.plan("update", SCOPE, CID)) == ()


# --- remove, gates, crash and rollback ----------------------------------------------


def test_remove_refuses_without_the_retire_gate(running):
    _repo_, eng = running
    _apply(eng, "install")
    plan = eng.plan("remove", SCOPE, CID)
    assert {op.gate for op in _ops(plan)} == {GATE}
    with pytest.raises(EngineRefusal) as info:
        eng.apply("remove", SCOPE, CID, plan_digest=plan.digest)
    assert info.value.code == "gate-unanswered"
    assert _state(eng)["attestation"] is not None


def test_remove_retires_units_and_attestations_never_the_out_dir(running):
    _repo_, eng = running
    _apply(eng, "install")
    _apply(eng, "configure")
    before = _state(eng)["out_dir"]
    plan, outcome = _apply(eng, "remove", accept_gates=frozenset({GATE}))
    assert outcome.state == "SATISFIED"
    assert [op.kind for op in _ops(plan)] == ["health-collector.unit-retire"] * 3 + ["health-collector.attest-retire"] * 2
    state = _state(eng)
    assert state["out_dir"] == before
    assert state["attestation"] is None and state["subscription_attestation"] is None
    assert all(state["units"][u]["sha256"] is None for u in UNITS)


def test_inverse_removes_the_attestation(running):
    _repo_, eng = running
    plan = eng.plan("install", SCOPE, CID)
    [op] = _ops(plan)
    eng.backend.faults.fail = {op.operation_id}  # postcondition never verified: the run halts
    with pytest.raises(EngineRefusal, match="HALTED"):
        eng.apply("install", SCOPE, CID, plan_digest=plan.digest)
    eng.backend.faults.fail = set()
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    assert _state(eng)["attestation"] is None


@pytest.mark.parametrize("point", KILL_POINTS)
def test_kill_then_reconcile_then_rollback_restores_preimage(tmp_path, point):
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, repo, _seed_running)
    preimage = _state(eng)["attestation"]
    plan = eng.plan("install", SCOPE, CID)
    [op] = _ops(plan)
    eng.backend.faults.kill = (op.operation_id, point)
    with pytest.raises(SimulatedKill):
        eng.apply("install", SCOPE, CID, plan_digest=plan.digest)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    restarted = H.engine(tmp_path, repo)
    outcome = restarted.reconcile_run(run_id, executor_dead=True)
    if point in ("after-manifest", "after-record"):
        assert outcome.state == "SATISFIED" and restarted.verify(SCOPE, CID)[0]["ok"]
        return
    assert outcome.state == "HALTED"
    assert restarted.rollback(run_id).state == "ROLLED_BACK"
    assert _state(restarted)["attestation"] == preimage


@pytest.mark.parametrize("point", KILL_POINTS)
def test_kill_mid_greenfield_unit_install_rolls_back_to_absent(tmp_path, point):
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, repo, _seed_out_dir)
    plan = eng.plan("install", SCOPE, CID)
    first = _ops(plan)[0]
    eng.backend.faults.kill = (first.operation_id, point)
    with pytest.raises(SimulatedKill):
        eng.apply("install", SCOPE, CID, plan_digest=plan.digest)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    restarted = H.engine(tmp_path, repo)
    outcome = restarted.reconcile_run(run_id, executor_dead=True)
    assert outcome.state == "HALTED"     # the first of three ops: no kill point completes the run (r1 F4)
    assert restarted.rollback(run_id).state == "ROLLED_BACK"
    state = _state(restarted)
    assert all(state["units"][u]["sha256"] is None for u in UNITS) and state["installed"] == {}


# --- registration, digests, the invariant list, the registry --------------------------


def test_component_kinds_are_seat_privilege_in_their_own_class(running):
    _repo_, eng = running
    kinds = ("unit-install", "unit-retire", "adopt-attest", "subscription-attest", "attest-retire")
    for kind in kinds:
        entry = eng.backend.op_registry.get(f"health-collector.{kind}")
        assert (entry.capability_class, entry.privilege, entry.read_only) == ("health.collector", "seat", False)


def test_a_handlers_edit_refuses_until_repinned(tmp_path):
    repo = _repo(tmp_path)
    handlers = repo / "tools" / "germinate" / "components" / "health-collector_handlers.py"
    handlers.write_text(handlers.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    with pytest.raises(EngineRefusal) as info:
        H.engine(tmp_path, repo)
    assert info.value.code == "component-registration-refused"
    digest = H.repin_contract(repo, CID)
    H.make_checkout(repo, [H.row(CID, digest, steward="recovery"), H.row("host-substrate", compute_contract_digest(
        repo / "tools" / "germinate" / "components" / "host-substrate.yaml"))])
    assert H.engine(tmp_path, repo).plan("install", SCOPE, "host-substrate")


def test_invariant_list_is_draft_hc1_to_hc8_and_pinned():
    doc = _list_doc()
    assert (doc["status"], doc["co_author"], doc["signoff"]) == ("DRAFT", "recovery", "pending")
    assert [e["id"].split("-")[0] for e in doc["invariants"]] == [f"HC{i}" for i in range(1, 9)]
    body = yaml.safe_load((COMPONENTS / "health-collector.yaml").read_text(encoding="utf-8"))["component"]
    assert body["invariant_list_digest"] == digest_of(doc)
    assert body["legs"]["verify"]["invariant_list_ref"] == f"{doc['id']}@{digest_of(doc)}"
    assert "emitter" not in body                     # D5 option (i): no self-emitter


def test_an_unknown_invariant_is_an_alarm_never_a_pass(running):
    _repo_, eng = running
    handler = _handler(eng)
    mod = _handlers_module(handler)
    doc = _list_doc()
    doc["invariants"].append({"id": "HC9-new", "check": "x"})
    out = mod.VERIFY(doc, {CID: handler.observe(CID, SCOPE)}, SCOPE)
    assert {"id": "HC9-new", "status": "alarm", "detail": "no evaluator for this invariant"} in out["results"]
    assert out["state"] == "unhealthy"


def test_real_repo_pins_are_current():
    result = repin(REPO_ROOT, REPO_ROOT / "registry" / "systems.yaml", CID)
    assert result.drift == []
