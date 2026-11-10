"""T-5964: the watch component adopts the ORG-080 contract by observe-and-attest.

Every leg WRAPS the legacy watch pipeline (tools/watch_runner.py, the tools/systemd
templates, tools/watch_pipeline_decommission.py) without rewriting it. It attests
repo-side evidence of the hand-installed claude-research pipeline and refuses
otherwise; the engine never installs, reads or tears down that account.

Every test runs on tmp_path stand-ins: a temporary checkout holding copies of the
component files and a fixture registry, over a simulated host. Evidence is built
by ``watch_evidence.collect`` from the committed byte-copy fixtures under
``fixtures/watch/`` (see SOURCE.txt). Only the ``test_real_*`` tests read the real
repo (read-only). ``_seed`` is a TEST HELPER: it writes simulated evidence into the
simulated host; no lifecycle verb does that.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
import yaml

from tools import watch_pipeline_decommission as WD
from tools import watch_runner as WR
from tools._organ_lifecycle import CommandResult, FakeCommandRunner
from tools.germinate.cli import main
from tools.germinate.components import watch as P
from tools.germinate.components import watch_evidence as E
from tools.germinate.engine import handlers_digest as HD
from tools.germinate.engine.backend import KILL_POINTS, SimulatedKill
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.run_state import RunStore
from tools.germinate.engine.tests import helpers as H
from tools.germinate.planner_sandbox.admission import coordinator
from tools.germinate.registry.contract_digest import compute_contract_digest

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPONENTS = REPO_ROOT / "tools" / "germinate" / "components"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "watch"
FILES = ("watch.yaml", "watch.py", "watch_handlers.py", "watch_evidence.py", "watch.invariants.yaml")
CID = "watch"
KIND = P.KIND
GATE = P.GATE
RETIRE_GATE = P.RETIRE_GATE
FROM_REV, HEAD = "f" * 40, "a" * 40
OBSERVED_AT = "2026-11-02T18:00:00Z"


def _landing():
    _sha, author, subject = (FIXTURES / "landing-commit.tsv").read_text(encoding="utf-8").rstrip("\n").split("\t")
    return author, subject


REPORTS = FIXTURES / "reports"
DAY_INDEX = REPORTS / "2026-11-02" / "day-index.md"


def _trigger() -> str:
    """The forbidden byte sequence, built at runtime so the contiguous bytes never appear
    in source (the tools/test_watch_runner.py fixture-string convention)."""

    return "HERME" + "S.md"


def _evidence(reports_root=REPORTS, day_index_path=DAY_INDEX, **overrides):
    """Evidence collected from the committed captures of real repo state."""

    author, subject = _landing()
    ev = E.collect(
        templates_dir=FIXTURES / "systemd",
        topics_path=FIXTURES / "topics.md",
        reports_root=reports_root,
        day_index_path=day_index_path,
        landing_author=author,
        landing_subject=subject,
        repo_head=HEAD,
        observed_at=OBSERVED_AT,
    )
    ev.update(copy.deepcopy(overrides))
    return ev


def _decommission_report(tmp_path, *, execute=True, account=P.PIPELINE_ACCOUNT, runner=None):
    """A real report from tools.watch_pipeline_decommission.decommission (FakeCommandRunner, tmp home)."""

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    report = WD.decommission(runner or FakeCommandRunner(), home=home, repo_root=tmp_path, execute=execute, current_account=account)
    return E.decommission_report(report)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    directory = repo / "tools" / "germinate" / "components"
    directory.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        shutil.copy(COMPONENTS / name, directory / name)
    digest = compute_contract_digest(directory / "watch.yaml")
    H.make_checkout(repo, [H.row(CID, digest, steward="specialist")])
    return repo


def _seed(backend, evidence=None, decommission=None, attestations=None) -> None:
    """TEST HELPER: write simulated evidence into the simulated host."""

    backend.handler(KIND).load_state({"evidence": evidence, "decommission": decommission, "attestations": attestations or {}})
    backend.save()


def _attestations(backend):
    return backend.handler(KIND).dump_state()["attestations"]


@pytest.fixture
def setup(tmp_path):
    repo = _repo(tmp_path)
    return repo, H.engine(tmp_path, repo)


def _apply(eng, leg, gate=GATE, **kwargs):
    plan = eng.plan(leg, "system", CID, **kwargs)
    [outcome] = eng.apply(leg, "system", CID, plan_digest=plan.digest, accept_gates=frozenset({gate}), **kwargs)
    assert outcome.state == "SATISFIED"
    return plan, outcome


# --- contract, registry, registration -------------------------------------------


def test_real_contract_loads_and_registry_row_validates():
    from tools.germinate.registry.validate import errors, validate_repo

    assert errors(validate_repo(REPO_ROOT)) == []
    doc = yaml.safe_load((REPO_ROOT / "registry" / "systems.yaml").read_text(encoding="utf-8"))
    [row] = [r for r in doc["systems"] if r.get("id") == CID]
    assert row["contract"]["digest"] == compute_contract_digest(COMPONENTS / "watch.yaml")
    assert row["contract_adoption"] == "partial"  # D10
    assert {leg: cell["status"] for leg, cell in row["legs"].items()} == dict.fromkeys(
        ("install", "configure", "health", "update", "remove"), "partial"
    )
    assert row["legs"]["health"]["ticket"] == "T-5979"
    assert {row["legs"][leg]["ticket"] for leg in ("install", "configure", "update", "remove")} == {"T-5972"}
    assert row["adoption_gaps"]["adoption_evidence.emitter"]["ticket"] == "T-5990"
    assert row["adoption_gaps"]["plan_repair"]["ticket"] == "T-5972"
    body = yaml.safe_load((COMPONENTS / "watch.yaml").read_text(encoding="utf-8"))["component"]
    assert body["steward"] == row["steward"] == "specialist"
    assert body["depends_on"] == [] and body["scopes"] == ["system"]  # D4
    assert body["privilege"] == {"seat": [KIND, P.RETIRE_KIND]}
    assert set(body["legs"]) == {"install", "configure", "update", "remove", "verify"}
    assert "emitter" not in body  # D5: no Health1 emitter exists
    assert [g["id"] for g in body["gates"]] == [GATE, RETIRE_GATE]
    assert all(g["approval"] == {"class": "operator"} and g["irreversible"] is False for g in body["gates"])
    invariants = yaml.safe_load((COMPONENTS / "watch.invariants.yaml").read_text(encoding="utf-8"))
    assert invariants["status"] == "CURRENT"  # D15: specialist+Recovery sign-offs complete (T-5984)
    assert [i["id"] for i in invariants["invariants"]] == [
        "W1-templates-present", "W2-landed-run-fresh", "W3-landed-by-pipeline-account", "W4-topics-parse", "W5-latest-day-no-fail",
        "W6-reports-byte-sequence-clean",
    ]


def test_real_registry_row_fails_without_the_component_files(tmp_path):
    """Red-without-the-change: the same row against a checkout lacking the module refuses."""

    from tools.germinate.registry.validate import check_contracts, errors

    doc = yaml.safe_load((REPO_ROOT / "registry" / "systems.yaml").read_text(encoding="utf-8"))
    msgs = "\n".join(str(f) for f in errors(check_contracts(doc, tmp_path)))
    assert "watch.contract" in msgs


def test_component_kinds_are_seat_in_their_own_class(setup):
    _repo_, eng = setup
    for kind in (KIND, P.RETIRE_KIND):
        entry = eng.backend.op_registry.get(kind)
        assert (entry.capability_class, entry.privilege, entry.read_only, entry.substrate) == ("watch.adoption", "seat", False, False)
        assert eng.backend.handles(kind)


def test_repin_check_is_clean(capsys):
    assert main(["registry", "repin", CID, "--check", "--repo-root", str(REPO_ROOT)]) == 0


def test_handlers_digest_covers_the_planner_and_the_evidence_collector(tmp_path):
    """The handlers pin covers watch_evidence.py (and the shared rules in watch.py): an
    edit to either, with no repin, refuses at discover."""

    assert HD.compute(COMPONENTS / "watch_handlers.py").files == ("watch.py", "watch_evidence.py", "watch_handlers.py")
    for name in ("watch_evidence.py", "watch.py"):
        repo = _repo(tmp_path / name)
        target = repo / "tools" / "germinate" / "components" / name
        target.write_text(target.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
        with pytest.raises(Exception, match="handlers_digest"):
            H.engine(tmp_path / name, repo)


def test_install_scope_equals_the_decommission_stems():
    """Differential: install and remove cover exactly the pipeline the legacy teardown
    covers; pantheon-policy-outflow (it runs on claude) is not watch (D1)."""

    assert P.UNIT_STEMS == WD.WATCH_UNIT_STEMS
    assert P.PIPELINE_ACCOUNT == WD.PIPELINE_ACCOUNT
    assert P.UNITS == E.unit_names()
    assert not any("policy-outflow" in unit for unit in P.UNITS)
    for unit in P.UNITS:
        assert (REPO_ROOT / "tools" / "systemd" / unit).is_file()


def test_freshness_tiers_are_their_own_constants():
    """W2 tiers come from the observed landing cadence (r2: specialist + Recovery), not watch_runner's
    HN lookback window; no coupling to it remains in the planner."""

    assert (P.FRESH_DEGRADED_DAYS, P.FRESH_FAILED_DAYS) == (2, 8)
    assert "HN_WINDOW" not in (COMPONENTS / "watch.py").read_text(encoding="utf-8")


# --- install: refuse unless healthy legacy evidence is observed -----------------


def test_install_refuses_with_no_evidence(setup):
    _repo_, eng = setup
    with pytest.raises(EngineRefusal) as info:
        eng.plan("install", "system", CID)
    assert info.value.code == "plan-refused"
    assert "no watch evidence is observed" in str(info.value) and "claude-research" in str(info.value)
    with pytest.raises(EngineRefusal):
        eng.apply("install", "system", CID, plan_digest="sha256:" + "0" * 64, accept_gates=frozenset({GATE}))
    assert list(RunStore(eng.backend.run_state_root("system")).all_tuples()) == []
    assert _attestations(eng.backend) == {}


def _without_template(unit):
    ev = _evidence()
    ev["templates"][unit] = None
    return ev


def _landed(**changes):
    ev = _evidence()
    ev["landed"].update(changes)
    return ev


@pytest.mark.parametrize(
    ("evidence", "match"),
    [
        (_without_template("pantheon-watch-digest.timer"), "pantheon-watch-digest.timer is absent"),
        (_evidence(observed_at="2026-11-11T09:00:00Z"), "stale: the newest landed run 2026-11-02 is 9 days, 9:00:00 old"),
        (_landed(author="codex-fallback"), "landed by 'codex-fallback'"),
        (_evidence(landed=None), "no landed watch run"),
        (_landed(rows=[{"slug": "x", "status": "fail"}]), "has no ok row"),
        (_evidence(observed_at="2026-11-01T09:00:00Z"), "dated after the observation"),
    ],
    ids=["template-missing", "stale", "wrong-author", "no-day-index", "no-ok-row", "future-dated"],
)
def test_unhealthy_install_evidence_refuses(setup, evidence, match):
    _repo_, eng = setup
    _seed(eng.backend, evidence)
    with pytest.raises(EngineRefusal, match=match) as info:
        eng.plan("install", "system", CID)
    assert info.value.code == "plan-refused"


def test_install_differential_minimal_pairs(setup):
    """The healthy capture plans; the boundary day plans; one day past it refuses."""

    _repo_, eng = setup
    for observed_at, plans in (("2026-11-10T00:00:00Z", True), ("2026-11-10T00:00:01Z", False)):
        _seed(eng.backend, _evidence(observed_at=observed_at))
        if plans:
            assert len(eng.plan("install", "system", CID).nodes[0].operations) == 1
        else:
            with pytest.raises(EngineRefusal, match="stale"):
                eng.plan("install", "system", CID)
    for author, plans in ((P.PIPELINE_ACCOUNT, True), ("claude", False)):
        _seed(eng.backend, _landed(author=author))
        if plans:
            assert len(eng.plan("install", "system", CID).nodes[0].operations) == 1
        else:
            with pytest.raises(EngineRefusal, match="landed by"):
                eng.plan("install", "system", CID)


def test_attests_then_reruns_empty(setup):
    _repo_, eng = setup
    evidence = _evidence()
    _seed(eng.backend, evidence)
    plan = eng.plan("install", "system", CID)
    [op] = plan.nodes[0].operations
    assert (op.kind, op.target, op.privilege, op.gate, op.layer) == (KIND, P.INSTALL_TARGET, "seat", GATE, 0)
    assert op.crash_contract == "write-ahead-inverse-durable" and op.inverse == {"attestation": "absent"}
    assert op.desired["pipeline_account"] == "claude-research"
    assert {k: v for k, v in op.desired.items() if k.startswith("unit.")} == {f"unit.{u}": evidence["templates"][u] for u in P.UNITS}
    assert "never run by the engine" in op.provenance["live_install"]
    [outcome] = eng.apply("install", "system", CID, plan_digest=plan.digest, accept_gates=frozenset({GATE}))
    assert outcome.state == "SATISFIED"
    assert _attestations(eng.backend) == {P.INSTALL_TARGET: dict(op.desired)}
    store = RunStore(eng.backend.run_state_root("system"))
    with store.open(store.find(outcome.run_id)) as handle:
        record = handle.read_record()
    assert [r["op_id"] for r in record["receipts"]] == [op.operation_id]
    assert record["operations"][0]["postcondition_check"] == dict(op.desired)
    assert eng.plan("install", "system", CID).nodes[0].operations == ()  # verify, not mutate
    [result] = eng.verify("system", CID)
    assert result["ok"], result
    assert result["invariants"]["state"] == "ready"


def test_the_plan_digest_binds_the_evidence(setup):
    _repo_, eng = setup
    _seed(eng.backend, _evidence())
    first = eng.plan("install", "system", CID).digest
    changed = _evidence()
    changed["templates"]["pantheon-watch.service"] = "e" * 64
    _seed(eng.backend, changed)
    assert eng.plan("install", "system", CID).digest != first
    with pytest.raises(EngineRefusal, match="plan-digest-mismatch"):
        eng.apply("install", "system", CID, plan_digest=first, accept_gates=frozenset({GATE}))


def test_healthy_drift_re_attests_and_verify_names_drifted_keys(setup):
    _repo_, eng = setup
    _seed(eng.backend, _evidence())
    first, _ = _apply(eng, "install")
    prior = _attestations(eng.backend)
    changed = _evidence()
    changed["templates"]["pantheon-watch.service"] = "e" * 64
    _seed(eng.backend, changed, attestations=prior)
    [result] = eng.verify("system", CID)
    assert not result["ok"]
    detail = result["checks"][0]["detail"]
    assert "drift" in detail and "unit.pantheon-watch.service" in detail and "unit.pantheon-watch.timer" not in detail
    [op] = eng.plan("install", "system", CID).nodes[0].operations
    assert op.precondition["attestation_sha256"] != "absent" and "attestation_sha256" in op.inverse
    assert op.desired["unit.pantheon-watch.service"] == "e" * 64
    _apply(eng, "install")
    [result] = eng.verify("system", CID)
    assert result["ok"], result
    assert eng.plan("install", "system", CID).nodes[0].operations == ()


def test_attested_then_unhealthy_refuses_never_re_attests(setup):
    _repo_, eng = setup
    _seed(eng.backend, _evidence())
    _apply(eng, "install")
    prior = _attestations(eng.backend)
    _seed(eng.backend, _evidence(observed_at="2026-11-20T00:00:00Z"), attestations=prior)
    with pytest.raises(EngineRefusal, match="stale") as info:
        eng.plan("install", "system", CID)
    assert info.value.code == "plan-refused"
    [result] = eng.verify("system", CID)
    assert not result["ok"] and "no longer healthy" in result["checks"][0]["detail"]
    assert result["invariants"]["state"] == "unhealthy"
    assert _attestations(eng.backend) == prior  # the old attestation is left, flagged by verify


def test_apply_refuses_without_the_gate(setup):
    _repo_, eng = setup
    _seed(eng.backend, _evidence())
    plan = eng.plan("install", "system", CID)
    with pytest.raises(EngineRefusal) as info:
        eng.apply("install", "system", CID, plan_digest=plan.digest)
    assert info.value.code == "gate-unanswered"
    assert _attestations(eng.backend) == {}


def test_evidence_drift_between_plan_and_apply_halts_before_attesting(setup):
    _repo_, eng = setup
    _seed(eng.backend, _evidence())
    plan = eng.plan("install", "system", CID)
    _seed(eng.backend, _evidence(observed_at="2026-11-20T00:00:00Z"))
    with pytest.raises(EngineRefusal):
        eng.apply("install", "system", CID, plan_digest=plan.digest, accept_gates=frozenset({GATE}))
    assert _attestations(eng.backend) == {}


# --- configure / update / verify -------------------------------------------------


def test_configure_attests_topics_and_refuses_parse_warnings_or_no_install(setup):
    _repo_, eng = setup
    _seed(eng.backend, _evidence())
    with pytest.raises(EngineRefusal, match="needs an attested install first"):
        eng.plan("configure", "system", CID)
    _apply(eng, "install")
    prior = _attestations(eng.backend)
    for topics, match in (
        ({"warnings": ["topic 'x' has no cadence"]}, "parse_topics warns"),
        ({"active": []}, "no active topic"),
    ):
        bad = _evidence()
        bad["topics"].update(topics)
        _seed(eng.backend, bad, attestations=prior)
        with pytest.raises(EngineRefusal, match=match):
            eng.plan("configure", "system", CID)
    evidence = _evidence()
    _seed(eng.backend, evidence, attestations=prior)
    plan, _ = _apply(eng, "configure")
    [op] = plan.nodes[0].operations
    assert op.target == P.TOPICS_TARGET and op.desired["active_count"] == str(len(evidence["topics"]["active"]))
    assert op.desired["topics_sha256"] == evidence["topics"]["sha256"]
    assert eng.plan("configure", "system", CID).nodes[0].operations == ()
    edited = _evidence()  # healthy drift: topics.md edited and still parses
    edited["topics"]["sha256"] = "d" * 64
    _seed(eng.backend, edited, attestations=_attestations(eng.backend))
    [result] = eng.verify("system", CID)
    assert not result["ok"] and "topics_sha256" in result["checks"][0]["detail"]
    [op] = eng.plan("configure", "system", CID).nodes[0].operations
    assert op.desired["topics_sha256"] == "d" * 64


def test_update_refuses_without_revs_or_head_mismatch_or_no_install(setup):
    _repo_, eng = setup
    _seed(eng.backend, _evidence())
    with pytest.raises(EngineRefusal, match="needs an attested install first"):
        eng.plan("update", "system", CID, source_revs=(FROM_REV, HEAD))
    _apply(eng, "install")
    with pytest.raises(EngineRefusal, match="needs --from-rev and --to-rev"):
        eng.plan("update", "system", CID)
    with pytest.raises(EngineRefusal, match="is not the observed HEAD"):
        eng.plan("update", "system", CID, source_revs=(FROM_REV, "b" * 40))


def test_update_refuses_when_unit_templates_changed_naming_units(setup):
    _repo_, eng = setup
    _seed(eng.backend, _evidence())
    _apply(eng, "install")
    changed = _evidence()
    changed["templates"]["pantheon-knowledge-sweep.timer"] = "e" * 64
    _seed(eng.backend, changed, attestations=_attestations(eng.backend))
    with pytest.raises(EngineRefusal, match=r"unit\.pantheon-knowledge-sweep\.timer") as info:
        eng.plan("update", "system", CID, source_revs=(FROM_REV, HEAD))
    assert "hand re-install" in str(info.value) and "T-5972" in str(info.value)


def test_update_attests_a_code_only_update_and_verify_rechecks_health(setup):
    _repo_, eng = setup
    _seed(eng.backend, _evidence())
    _apply(eng, "install")
    plan, outcome = _apply(eng, "update", source_revs=(FROM_REV, HEAD))
    [op] = plan.nodes[0].operations
    assert op.target == P.UPDATE_TARGET and op.gate == GATE
    assert (op.desired["source_rev_from"], op.desired["source_rev_to"]) == (FROM_REV, HEAD)
    assert eng.plan("update", "system", CID, source_revs=(FROM_REV, HEAD)).nodes[0].operations == ()
    [result] = eng.verify("system", CID)  # the update run is the latest forward run
    assert result["run_id"] == outcome.run_id and result["ok"], result
    later = _evidence(repo_head="9" * 40, observed_at="2026-11-25T00:00:00Z")  # head moved on, pipeline stalled
    _seed(eng.backend, later, attestations=_attestations(eng.backend))
    [result] = eng.verify("system", CID)
    assert not result["ok"] and "no longer healthy" in result["checks"][0]["detail"]


def test_verify_invariants_w1_to_w5(setup):
    _repo_, eng = setup
    _seed(eng.backend, _evidence())
    _apply(eng, "install")
    prior = _attestations(eng.backend)

    def statuses(evidence):
        _seed(eng.backend, evidence, attestations=prior)
        [result] = eng.verify("system", CID)
        inv = result["invariants"]
        return {r["id"]: r["status"] for r in inv["results"]}, inv["state"]

    by_id, state = statuses(_evidence())
    assert set(by_id.values()) == {"pass"} and state == "ready"
    failing = _evidence()
    failing["landed"]["rows"].append({"slug": "mcp-supply-chain", "status": "fail"})
    by_id, state = statuses(failing)
    assert by_id["W5-latest-day-no-fail"] == "fail" and state == "degraded"
    assert [k for k, v in by_id.items() if v != "pass"] == ["W5-latest-day-no-fail"]
    by_id, state = statuses(_without_template("pantheon-watch.timer"))
    assert by_id["W1-templates-present"] == "fail" and state == "unhealthy"
    by_id, _ = statuses(_evidence(observed_at="2026-11-20T00:00:00Z"))
    assert by_id["W2-landed-run-fresh"] == "fail" and by_id["W1-templates-present"] == "pass"
    by_id, _ = statuses(_landed(author="claude"))
    assert by_id["W3-landed-by-pipeline-account"] == "fail"
    warned = _evidence()
    warned["topics"]["warnings"] = ["bad stanza"]
    by_id, _ = statuses(warned)
    assert by_id["W4-topics-parse"] == "fail"
    _seed(eng.backend, None, attestations=prior)  # nothing observed: could not ask, never a pass
    [result] = eng.verify("system", CID)
    assert not result["ok"] and result["invariants"]["results"][0]["status"] == "alarm"


# --- remove: retire the attestations after an observed decommission --------------


@pytest.mark.parametrize("case", ["none", "dry-run", "wrong-account", "timer-failed"])
def test_remove_refuses_without_an_executed_decommission_report(setup, tmp_path, case):
    _repo_, eng = setup
    _seed(eng.backend, _evidence())
    _apply(eng, "install")
    report = {
        "none": lambda: None,
        "dry-run": lambda: _decommission_report(tmp_path, execute=False),
        "wrong-account": lambda: _decommission_report(tmp_path, account="suborch2"),
        "timer-failed": lambda: _decommission_report(
            tmp_path,
            runner=FakeCommandRunner({("systemctl", "--user", "disable", "--now", "pantheon-watch-digest.timer"): CommandResult(1, "", "boom")}),
        ),
    }[case]()
    _seed(eng.backend, _evidence(), decommission=report, attestations=_attestations(eng.backend))
    with pytest.raises(EngineRefusal) as info:
        eng.plan("remove", "system", CID)
    assert info.value.code == "plan-refused" and "watch_pipeline_decommission.py" in str(info.value)
    assert {"none": "no decommission report", "dry-run": "dry run", "wrong-account": "warnings",
            "timer-failed": "pantheon-watch-digest.timer"}[case] in str(info.value)


def test_remove_retires_attestations_behind_gate_and_never_touches_evidence(setup, tmp_path):
    _repo_, eng = setup
    evidence = _evidence()
    _seed(eng.backend, evidence)
    _apply(eng, "install")
    _apply(eng, "configure")
    report = _decommission_report(tmp_path)
    _seed(eng.backend, evidence, decommission=report, attestations=_attestations(eng.backend))
    plan = eng.plan("remove", "system", CID)
    [op] = plan.nodes[0].operations
    assert (op.kind, op.target, op.gate) == (P.RETIRE_KIND, P.RETIRE_TARGET, RETIRE_GATE)
    assert op.desired["decommission_report_sha256"] == P.sha(report)
    with pytest.raises(EngineRefusal) as info:
        eng.apply("remove", "system", CID, plan_digest=plan.digest)
    assert info.value.code == "gate-unanswered"
    [outcome] = eng.apply("remove", "system", CID, plan_digest=plan.digest, accept_gates=frozenset({RETIRE_GATE}))
    assert outcome.state == "SATISFIED"
    state = eng.backend.handler(KIND).dump_state()
    assert state["attestations"] == {} and state["evidence"] == evidence and state["decommission"] == report
    assert eng.plan("remove", "system", CID).nodes[0].operations == ()  # nothing left to retire
    [result] = eng.verify("system", CID)  # verify reads the latest forward run: the install is gone
    assert not result["ok"] and "attestation is absent" in result["checks"][0]["detail"]


def test_remove_fixture_report_from_the_real_decommission_tool(tmp_path):
    report = _decommission_report(tmp_path)
    assert report["dry_run"] is False and report["warnings"] == []
    assert [f"disabled --now {stem}.timer" for stem in WD.WATCH_UNIT_STEMS] == report["changed"][:3]
    assert P.decommission_problems(report) == []
    repo = _repo(tmp_path)
    eng = H.engine(tmp_path, repo)
    _seed(eng.backend, _evidence(), decommission=report, attestations={P.INSTALL_TARGET: {"pipeline_account": "claude-research"}})
    [op] = eng.plan("remove", "system", CID).nodes[0].operations
    assert op.kind == P.RETIRE_KIND and "T-2887" in op.provenance["live_teardown"]


# --- crash contract: kill / reconcile / rollback ---------------------------------


@pytest.mark.parametrize("point", KILL_POINTS)
def test_kill_then_reconcile_then_rollback_install(tmp_path, point):
    repo = _repo(tmp_path)
    eng = H.engine(tmp_path, repo)
    evidence = _evidence()
    _seed(eng.backend, evidence)
    plan = eng.plan("install", "system", CID)
    [op] = plan.nodes[0].operations
    eng.backend.faults.kill = (op.operation_id, point)
    with pytest.raises(SimulatedKill):
        eng.apply("install", "system", CID, plan_digest=plan.digest, accept_gates=frozenset({GATE}))
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    restarted = H.engine(tmp_path, repo)
    outcome = restarted.reconcile_run(run_id, executor_dead=True)
    state = restarted.backend.handler(KIND).dump_state()
    assert state["evidence"] == evidence  # the legacy evidence is never touched
    if point in ("after-manifest", "after-record"):
        assert outcome.state == "SATISFIED"
        assert state["attestations"] == {P.INSTALL_TARGET: dict(op.desired)}
        return
    assert outcome.state == "HALTED"
    assert restarted.rollback(run_id).state == "ROLLED_BACK"
    state = restarted.backend.handler(KIND).dump_state()
    assert state["attestations"] == {} and state["evidence"] == evidence


@pytest.mark.parametrize("point", KILL_POINTS)
def test_kill_then_reconcile_then_rollback_remove(tmp_path, point):
    repo = _repo(tmp_path)
    eng = H.engine(tmp_path, repo)
    evidence = _evidence()
    _seed(eng.backend, evidence)
    _apply(eng, "install")
    attested = _attestations(eng.backend)
    report = _decommission_report(tmp_path)
    _seed(eng.backend, evidence, decommission=report, attestations=attested)
    plan = eng.plan("remove", "system", CID)
    [op] = plan.nodes[0].operations
    eng.backend.faults.kill = (op.operation_id, point)
    with pytest.raises(SimulatedKill):
        eng.apply("remove", "system", CID, plan_digest=plan.digest, accept_gates=frozenset({RETIRE_GATE}))
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    restarted = H.engine(tmp_path, repo)
    outcome = restarted.reconcile_run(run_id, executor_dead=True)
    if point in ("after-manifest", "after-record"):
        assert outcome.state == "SATISFIED"
        assert _attestations(restarted.backend) == {}
        return
    assert outcome.state == "HALTED"
    assert restarted.rollback(run_id).state == "ROLLED_BACK"
    state = restarted.backend.handler(KIND).dump_state()
    assert state["attestations"] == attested and state["evidence"] == evidence and state["decommission"] == report


# --- integration: the real legacy shapes ------------------------------------------


def test_attests_real_captured_watch_state(tmp_path):
    """The committed captures (byte copies of real repo files) plan an attestation, and a
    day index written by the real watch_runner.append_day_index_entry parses the same way."""

    evidence = _evidence()
    assert evidence["landed"]["date"] == "2026-11-02" and evidence["landed"]["author"] == "claude-research"
    assert len(evidence["landed"]["rows"]) == 14 and {r["status"] for r in evidence["landed"]["rows"]} == {"ok"}
    assert P.install_problems(evidence) == [] and P.topics_problems(evidence) == []
    reports = tmp_path / "reports"
    old = reports / "2026-11-02"
    old.mkdir(parents=True)
    shutil.copy(DAY_INDEX, old / E.DAY_INDEX)
    new = reports / "2026-11-09"
    new.mkdir()
    WR.append_day_index_entry(new, "mcp-supply-chain", "ok", "TL;DR: fine")
    WR.append_day_index_entry(new, "context-management", "fail", "dispatch failed")
    (reports / "2026-11-10").mkdir()  # a day dir with no index yet is skipped
    assert E.newest_day_index(reports) == new / E.DAY_INDEX
    landed = E.landed(E.newest_day_index(reports), "claude-research", "research(watch): land 2026-11-09 reports")
    assert landed["date"] == "2026-11-09"
    assert landed["rows"] == [{"slug": "mcp-supply-chain", "status": "ok"}, {"slug": "context-management", "status": "fail"}]
    assert P.landed_failures({"landed": landed}) == ["context-management"]
    repo = _repo(tmp_path)
    eng = H.engine(tmp_path, repo)
    _seed(eng.backend, evidence)
    [op] = eng.plan("install", "system", CID).nodes[0].operations
    assert op.desired["unit.pantheon-watch.timer"] == E.templates(FIXTURES / "systemd")["pantheon-watch.timer"]


# --- r2: W2 tiers, W6, strict parsing ----------------------------------------------


def _verify_state(eng, evidence):
    """Attest over fresh evidence, then observe ``evidence`` and return verify's W2 result and state."""

    _seed(eng.backend, _evidence())
    if not _attestations(eng.backend):
        _apply(eng, "install")
    _seed(eng.backend, evidence, attestations=_attestations(eng.backend))
    [result] = eng.verify("system", CID)
    by_id = {r["id"]: r for r in result["invariants"]["results"]}
    return by_id, result["invariants"]["state"]


@pytest.mark.parametrize(
    ("observed_at", "attests", "w2", "state"),
    [
        ("2026-11-04T00:00:00Z", True, "pass", "ready"),  # exactly 2 days
        ("2026-11-04T00:00:01Z", True, "fail", "degraded"),  # just over 2 days
        ("2026-11-10T00:00:00Z", True, "fail", "degraded"),  # exactly 8 days
        ("2026-11-10T00:00:01Z", False, "fail", "unhealthy"),  # just over 8 days
    ],
    ids=["exactly-2d", "over-2d", "exactly-8d", "over-8d"],
)
def test_w2_degraded_and_failed_boundaries(setup, observed_at, attests, w2, state):
    _repo_, eng = setup
    evidence = _evidence(observed_at=observed_at)
    _seed(eng.backend, evidence)
    if attests:
        assert len(eng.plan("install", "system", CID).nodes[0].operations) == 1
    else:
        with pytest.raises(EngineRefusal, match="stale"):
            eng.plan("install", "system", CID)
    by_id, got = _verify_state(eng, evidence)
    assert (by_id["W2-landed-run-fresh"]["status"], got) == (w2, state)
    if state == "degraded":
        assert by_id["W2-landed-run-fresh"]["detail"].startswith("degraded:")


def _reports_copy(tmp_path, day="2026-11-02"):
    reports = tmp_path / "reports"
    (reports / day).mkdir(parents=True)
    shutil.copy(DAY_INDEX, reports / day / E.DAY_INDEX)
    return reports


def test_w6_byte_sequence_pass_and_fail(setup, tmp_path):
    _repo_, eng = setup
    clean = _evidence()
    assert clean["byte_scan"] == {"ok": True, "detail": None}
    by_id, state = _verify_state(eng, clean)
    assert by_id["W6-reports-byte-sequence-clean"]["status"] == "pass" and state == "ready"
    reports = _reports_copy(tmp_path)
    nested = reports / "2026-11-02" / "snapshots"
    nested.mkdir()
    (nested / "note.md").write_text("see " + _trigger() + " for the policy\n", encoding="utf-8")
    dirty = _evidence(reports_root=reports, day_index_path=reports / "2026-11-02" / E.DAY_INDEX)
    assert dirty["byte_scan"]["ok"] is False and dirty["byte_scan"]["detail"].endswith("note.md:1")
    by_id, state = _verify_state(eng, dirty)
    assert by_id["W6-reports-byte-sequence-clean"]["status"] == "fail" and state == "unhealthy"
    _seed(eng.backend, dirty)
    with pytest.raises(EngineRefusal, match="forbidden byte sequence"):
        eng.plan("install", "system", CID)
    missing = _evidence()
    del missing["byte_scan"]
    _seed(eng.backend, missing)
    with pytest.raises(EngineRefusal, match="no byte-sequence scan"):
        eng.plan("install", "system", CID)


def test_invalid_date_dirs_are_skipped(tmp_path):
    reports = _reports_copy(tmp_path)
    for name in ("2026-13-40", "9999-99-99", "20261109", "2026-02-30"):
        (reports / name).mkdir()
        shutil.copy(DAY_INDEX, reports / name / E.DAY_INDEX)
    assert E.newest_day_index(reports) == reports / "2026-11-02" / E.DAY_INDEX
    assert [E.day_of(n) for n in ("2026-13-40", "9999-99-99", "20261109", "2026-02-30")] == [None] * 4


def test_day_index_header_dir_mismatch_refuses(setup, tmp_path):
    _repo_, eng = setup
    reports = _reports_copy(tmp_path, day="2026-11-05")  # its header still says 2026-11-02
    index = E.newest_day_index(reports)
    evidence = _evidence(reports_root=reports, day_index_path=index)
    assert "differs from its directory '2026-11-05'" in evidence["landed"]["unreadable"]
    _seed(eng.backend, evidence)
    with pytest.raises(EngineRefusal, match="unreadable"):
        eng.plan("install", "system", CID)


def _templates(**changes):
    ev = _evidence()
    ev["templates"].update(changes)
    return ev


@pytest.mark.parametrize(
    ("evidence", "match"),
    [
        (_landed(date="20261102"), "is not a YYYY-MM-DD date"),
        (_landed(date=20261102), "is not a YYYY-MM-DD date"),
        (_landed(date="2026-11-02T00:00:00Z"), "is not a YYYY-MM-DD date"),
        (_landed(rows="ok"), "rows are not a list"),
        (_evidence(observed_at="2026-11-02 18:00:00"), "observed_at .* is not a"),
        (_evidence(observed_at="2026-11-02T18:00:00+00:00"), "observed_at .* is not a"),
        (_evidence(observed_at="20261102T180000Z"), "observed_at .* is not a"),
        (_evidence(observed_at=1793642400), "observed_at .* is not a"),
        (_templates(**{"pantheon-watch.timer": "sha256:" + "a" * 64}), "malformed digest"),
        (_templates(**{"pantheon-watch.timer": "A" * 64}), "malformed digest"),
        (_templates(**{"pantheon-watch.timer": 7}), "malformed digest"),
        (_evidence(templates=["pantheon-watch.timer"]), "not a mapping"),
    ],
    ids=["day-basic-form", "day-int", "day-prefix", "rows-not-list", "observed-space", "observed-offset",
         "observed-basic", "observed-int", "digest-prefixed", "digest-upper", "digest-int", "templates-list"],
)
def test_malformed_install_evidence_refuses(setup, evidence, match):
    _repo_, eng = setup
    _seed(eng.backend, evidence)
    with pytest.raises(EngineRefusal, match=match) as info:
        eng.plan("install", "system", CID)
    assert info.value.code == "plan-refused"


@pytest.mark.parametrize(
    ("topics", "match"),
    [
        ({"warnings": "oops"}, "warnings is not a list"),
        ({"active": "mcp-supply-chain"}, "active is not a list"),
        ({"parsed": None}, "parsed is not a list"),
        ({"sha256": "x"}, "digest 'x' is malformed"),
    ],
    ids=["warnings-str", "active-str", "parsed-none", "sha-malformed"],
)
def test_malformed_topics_refuse_configure(setup, topics, match):
    _repo_, eng = setup
    _seed(eng.backend, _evidence())
    _apply(eng, "install")
    bad = _evidence()
    bad["topics"].update(topics)
    _seed(eng.backend, bad, attestations=_attestations(eng.backend))
    with pytest.raises(EngineRefusal, match=match):
        eng.plan("configure", "system", CID)


@pytest.mark.parametrize(
    "report",
    [
        {"dry_run": "false", "changed": [], "warnings": []},
        {"dry_run": False, "changed": "disabled --now pantheon-watch.timer", "warnings": []},
        {"dry_run": False, "changed": [], "warnings": "none"},
        {"dry_run": False, "changed": [1, 2, 3], "warnings": []},
    ],
    ids=["dry-run-str", "changed-str", "warnings-str", "changed-ints"],
)
def test_malformed_decommission_report_refuses(setup, report):
    _repo_, eng = setup
    _seed(eng.backend, _evidence(), decommission=report, attestations={P.INSTALL_TARGET: {"pipeline_account": "claude-research"}})
    with pytest.raises(EngineRefusal, match="the decommission report is malformed"):
        eng.plan("remove", "system", CID)
