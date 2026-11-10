"""T-5921: harness-manager's component PREFLIGHT checks (T-3191 viii component items).

Unit tests drive ``harness_manager_preflight.preflight`` with a simulated port (the
operator.json ``probe`` section); the engine tests go through ``plan --preflight``
with the component test fixtures, whose stub dependencies are satisfied in the
simulated host (mason ruling Q1; the real-dependency preflight is covered by
test_harness_manager_dependencies, T-5947; without dependencies it stays NO-GO at
dependency-not-installed, asserted below). instance.yaml is the REAL planner checkout's (read-only), as the planner
reads it.
"""

from __future__ import annotations

import hashlib

import pytest

from tools.germinate.components import harness_manager_preflight as P
from tools.germinate.components.tests import test_harness_manager_component as HMC
from tools.germinate.engine import preflight as PF
from tools.germinate.engine.tests.test_preflight import HEAD, write_observation
from tools.persona_installer import REPO_ROOT

PLANNER_SHA = hashlib.sha256((REPO_ROOT / "instance.yaml").read_bytes()).hexdigest()

#: (account, persona, has a codex_telegram_bridge binding) from the real instance.yaml.
CHANNEL_LESS = ("suborch2", "artisan")
TELEGRAM = ("claude-fallback", "recovery")
UID = 4321


def _probe_doc(account, *, bus="socket", manifest=PLANNER_SHA, seat_state=None, cred=None, uid=UID):
    home = f"/home/{account}"
    paths = {}
    if bus is not None:
        paths[f"/run/user/{uid}/bus"] = {"kind": bus, "uid": uid, "gid": uid, "mode": "0666"}
    if cred is not None:
        paths[f"{home}/.claude/channels/telegram/.env"] = cred
    doc = {
        "accounts": {account: {"uid": uid, "home": home}},
        "paths": paths,
        "files": {} if manifest is None else {f"{home}/pantheon-config/instance.yaml": manifest},
        "json": {},
    }
    if seat_state is not None:
        persona = {"suborch2": "artisan", "claude-fallback": "recovery", HMC.ACCT: "mason"}[account]
        doc["json"][f"{home}/.local/state/pantheon/personas/{persona}/overrides/seat-state.json"] = seat_state
    return doc


def _run(tmp_path, account, probe, *, leg="install", desired=None, scope=None):
    write_observation(tmp_path, probe=probe)
    ctx = PF.PreflightContext(
        component="harness-manager", leg=leg, scope=scope or f"seat-{account}", ops=(), desired=desired or {},
        repo_root=str(REPO_ROOT), tier="simulated", probe=PF.Probe(PF.SimulatedPreflightPort(tmp_path)),
    )
    checks = PF.component_checks(P.preflight(ctx))  # always schema-valid
    assert [c["id"] for c in checks] == list(P.CHECK_IDS)
    return {c["id"]: (c["status"], c["reason"]) for c in checks}


GOOD_CRED = {"kind": "file", "uid": UID, "gid": UID, "mode": "0600"}


def test_artisan_shaped_seat_gets_real_checks_and_a_vacuous_credential(tmp_path):
    assert _run(tmp_path, CHANNEL_LESS[0], _probe_doc(CHANNEL_LESS[0])) == {
        "seat-user-bus": ("pass", None),
        "target-manifest-identical": ("pass", None),
        "seat-state-awake": ("pass", None),
        "credential-mode": ("vacuous", "no-telegram-receiver"),
    }


def test_a_telegram_seat_with_a_good_credential_passes(tmp_path):
    out = _run(tmp_path, TELEGRAM[0], _probe_doc(TELEGRAM[0], cred=GOOD_CRED))
    assert set(out.values()) == {("pass", None)}


@pytest.mark.parametrize(
    "cred, reason",
    [
        (None, "credential-absent"),
        ({**GOOD_CRED, "mode": "0640"}, "credential-mode"),
        ({**GOOD_CRED, "mode": "0400"}, "credential-mode"),
        ({**GOOD_CRED, "uid": UID + 1}, "credential-owner"),
        ({**GOOD_CRED, "kind": "symlink", "mode": "0777"}, "credential-not-regular"),
        ("unobservable", "unobservable"),
    ],
)
def test_credential_mode_failures(tmp_path, cred, reason):
    probe = _probe_doc(TELEGRAM[0], cred=cred if cred != "unobservable" else None)
    if cred == "unobservable":
        probe["paths"]["/home/claude-fallback/.claude/channels/telegram/.env"] = "unobservable"
    assert _run(tmp_path, TELEGRAM[0], probe)["credential-mode"] == ("fail", reason)


@pytest.mark.parametrize(
    "kw, reason",
    [({"bus": None}, "user-bus-absent"), ({"bus": "file"}, "user-bus-not-socket")],
)
def test_user_bus_failures(tmp_path, kw, reason):
    assert _run(tmp_path, CHANNEL_LESS[0], _probe_doc(CHANNEL_LESS[0], **kw))["seat-user-bus"] == ("fail", reason)


@pytest.mark.parametrize("manifest, reason", [(None, "target-manifest-absent"), ("e" * 64, "target-manifest-differs")])
def test_target_manifest_failures(tmp_path, manifest, reason):
    out = _run(tmp_path, CHANNEL_LESS[0], _probe_doc(CHANNEL_LESS[0], manifest=manifest))
    assert out["target-manifest-identical"] == ("fail", reason)


@pytest.mark.parametrize(
    "record, expected",
    [
        ({"state": "deep-sleep", "owner": "operator"}, ("fail", "seat-not-awake")),
        ({"state": "paused-intake", "owner": "orchestrator"}, ("fail", "seat-not-awake")),
        ([1, 2], ("fail", "seat-state-malformed")),
        ("unobservable", ("fail", "unobservable")),
    ],
)
def test_seat_state_failures(tmp_path, record, expected):
    probe = _probe_doc(CHANNEL_LESS[0], seat_state=record)
    assert _run(tmp_path, CHANNEL_LESS[0], probe)["seat-state-awake"] == expected


def test_seat_state_is_vacuous_where_the_plan_owns_it(tmp_path):
    probe = _probe_doc(CHANNEL_LESS[0], seat_state={"state": "paused-intake"})
    for leg, desired in (("configure", {"run_state": "running"}), ("remove", {})):
        out = _run(tmp_path, CHANNEL_LESS[0], probe, leg=leg, desired=desired)
        assert out["seat-state-awake"] == ("vacuous", "leg-owns-seat-state")
    assert _run(tmp_path, CHANNEL_LESS[0], probe, leg="configure", desired={"harness": "codex"})["seat-state-awake"][0] == "fail"


def test_system_scope_is_vacuous_and_absent_or_unobservable_accounts_fail(tmp_path):
    out = _run(tmp_path, CHANNEL_LESS[0], _probe_doc(CHANNEL_LESS[0]), scope="system")
    assert set(out.values()) == {("vacuous", "not-a-seat-scope")}
    probe = _probe_doc(CHANNEL_LESS[0])
    probe["accounts"] = {}
    assert set(_run(tmp_path, CHANNEL_LESS[0], probe).values()) == {("fail", "seat-account-absent")}
    probe["accounts"] = {CHANNEL_LESS[0]: "unobservable"}
    assert set(_run(tmp_path, CHANNEL_LESS[0], probe).values()) == {("fail", "unobservable")}
    write_observation(tmp_path)  # no probe section at all
    ctx = PF.PreflightContext("harness-manager", "install", "seat-suborch2", (), {}, "", "simulated",
                              PF.Probe(PF.SimulatedPreflightPort(tmp_path)))
    assert {c["reason"] for c in P.preflight(ctx)} == {"unobservable"}


def test_an_unbound_account_fails_the_persona_checks(tmp_path):
    out = _run(tmp_path, "nobody-t5921", _probe_doc("nobody-t5921"))
    assert out["seat-state-awake"] == ("fail", "persona-unresolved")
    assert out["credential-mode"] == ("fail", "persona-unresolved")


def test_the_deferred_checks_are_named_and_absent():
    assert "receiver-topology" in P.__doc__ and "webhook-inactive" in P.__doc__
    assert not {"receiver-topology", "webhook-inactive"} & set(P.CHECK_IDS)


# --- through the engine (sim deps satisfied; the component test fixtures) ----------------


def _observe(eng, probe):
    write_observation(eng.backend.root, user=HMC.ACCT, head=HEAD, probe=probe)


def test_engine_preflight_folds_the_harness_manager_checks(tmp_path):
    repo = HMC._repo(tmp_path)
    eng = HMC._engine(tmp_path, repo)
    assert "harness-manager" in eng.preflight_hooks
    _observe(eng, _probe_doc(HMC.ACCT))
    receipt = eng.preflight("configure", HMC.SCOPE, HMC.CID, desired={"harness": "codex"})
    comp = {c["id"]: c for c in receipt["checks"]}["component"]
    assert comp["status"] == "pass", comp
    subs = comp["evidence"]["components"]["harness-manager"]
    assert subs["hook"] == "registered" and [c["id"] for c in subs["checks"]] == list(P.CHECK_IDS)
    PF.validate_receipt(receipt)
    # a stale target clone turns it into a NO-GO
    _observe(eng, _probe_doc(HMC.ACCT, manifest="0" * 64))
    receipt = eng.preflight("configure", HMC.SCOPE, HMC.CID, desired={"harness": "codex"})
    comp = {c["id"]: c for c in receipt["checks"]}["component"]
    assert (comp["status"], comp["reason"]) == ("fail", "component-check-failed")
    assert "harness-manager/target-manifest-identical (target-manifest-differs)" in comp["detail"]


def test_without_satisfied_dependencies_the_preflight_is_no_go_at_the_plan(tmp_path):
    """Ruling Q1: stub deps absent -> NO-GO at the plan (real deps: T-5947 tests); component checks skip."""

    repo = HMC._repo(tmp_path)
    eng = HMC._engine(tmp_path, repo, deps=False)
    _observe(eng, _probe_doc(HMC.ACCT))
    receipt = eng.preflight("configure", HMC.SCOPE, HMC.CID, desired={"harness": "codex"})
    checks = {c["id"]: c for c in receipt["checks"]}
    assert receipt["verdict"] == "NO-GO" and checks["plan"]["reason"] == "dependency-not-installed"
    assert checks["component"]["status"] == "skip"
