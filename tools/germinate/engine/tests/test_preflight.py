"""T-5860 (T-5091 U6): ``germinate plan --preflight``, the read-only GO/NO-GO receipt.

Covers the GO path and every NO-GO reason, the ``sudo -n -l`` classification
table, the receipt's binding to the plan digest (and leg/scope/system/instance),
expiry, tamper evidence, the opt-in ``apply --preflight-receipt`` consumption,
the read-only proof (sim root byte-identical; claim, run-store, WAL and grant
binders raise if called), the dark pins and the no-persona-names AST check.
The real-component derivation tests live in
components/tests/test_preflight_real_plans.py.
"""

from __future__ import annotations

import ast
import datetime as dt
import io
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tools.germinate.cli import main
from tools.germinate.engine import executor as X
from tools.germinate.engine import preflight as PF
from tools.germinate.engine.backend import SimulatedHostBackend
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.run_state import RunStore
from tools.germinate.engine.tests import helpers as H
from tools.germinate.planner_sandbox.admission import coordinator
from tools.germinate.planner_sandbox.wal import CanonicalJsonError, WalStore

REPO_ROOT = Path(__file__).resolve().parents[4]
ENGINE_DIR = Path(PF.__file__).parent
HEAD = "a" * 40
OTHER = "b" * 40
NOW = dt.datetime(2026, 11, 2, 12, 0, 0, tzinfo=dt.timezone.utc)
OPS = {"install": [H.dir_op("op-1", "/srv/c"), H.file_op("op-2", "/srv/c/a.conf", "hi\n")]}

LISTING_SELF_ONLY = """Matching Defaults entries for opr on vps:
    env_reset, mail_badpass, secure_path=/usr/local/sbin\\:/usr/local/bin\\:/usr/sbin\\:/usr/bin, use_pty

User opr may run the following commands on vps:
    (opr) NOPASSWD: ALL
"""
LISTING_SIM = """Matching Defaults entries for opr on vps:
    env_reset, use_pty

Runas and Command-specific defaults for opr:
    Defaults!/usr/bin/true !requiretty

User opr may run the following commands on vps:
    (sim) NOPASSWD: ALL
    (root) NOPASSWD: /usr/local/sbin/pantheon-probe status
"""


def write_observation(
    root: Path, *, user="opr", uid=4100, listing=LISTING_SIM, exit_code=0, head=HEAD, clean=True, drop=(),
    host=None, stderr=None, probe=None,
):
    """TEST HELPER: the simulated operator / sudo listing / checkout (/ probe) observation."""

    doc = {
        "schema": PF.SIM_OBSERVATION_SCHEMA,
        "operator": {"user": user, "uid": uid},
        "sudo_listing": {"exit_code": exit_code, "stdout": listing},
        "checkout": {"head": head, "clean": clean},
    }
    if host is not None:
        doc["operator"]["host"] = host
    if stderr is not None:
        doc["sudo_listing"]["stderr"] = stderr
    if probe is not None:
        doc["probe"] = probe
    for key in drop:
        doc.pop(key)
    path = root.joinpath(*PF.SIM_OBSERVATION_REL)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _engine(tmp_path, ops=OPS, **overrides):
    _repo, eng = H.simple_setup(tmp_path, ops=ops, **overrides)
    eng.echo = io.StringIO()
    eng.clock = lambda: NOW
    write_observation(eng.backend.root)
    return eng


def _checks(receipt):
    return {c["id"]: c for c in receipt["checks"]}


def _failed(receipt):
    return {c["id"]: c["reason"] for c in receipt["checks"] if c["status"] == "fail"}


# --- GO ---------------------------------------------------------------------------------


def test_go_receipt_is_sealed_and_bound_to_the_plan(tmp_path):
    eng = _engine(tmp_path)
    plan = eng.plan("install", "system", "comp-a")
    receipt = eng.preflight("install", "system", "comp-a", plan_digest=plan.digest)
    assert receipt["verdict"] == "GO", _failed(receipt)
    assert [c["id"] for c in receipt["checks"]] == list(PF.CHECK_IDS)
    assert receipt["plan"]["plan_digest"] == plan.digest
    assert receipt["plan"]["nodes"] == [{"component": "comp-a", "contract_digest": plan.nodes[0].contract_digest}]
    assert receipt["observed_at"] == "2026-11-02T12:00:00Z" and receipt["expires_at"] == "2026-11-02T13:00:00Z"
    assert receipt["observation_tier"] == "simulated" and receipt["operator"] == {"user": "opr", "uid": 4100}
    assert PF.validate_receipt(receipt) is receipt
    cap = _checks(receipt)["capability"]["evidence"]
    assert cap["required"] == {"sim": ["comp-a/op-1", "comp-a/op-2"]} and cap["held"] == {"sim": "sudo"}
    hold = _checks(receipt)["source-hold"]["evidence"]
    assert (hold["head"], hold["mode"], hold["hold_mechanism"]) == (HEAD, "detect-at-apply", "none")


def test_self_account_holds_without_any_sudo(tmp_path):
    eng = _engine(tmp_path)
    write_observation(eng.backend.root, user="sim", listing="", exit_code=1)
    receipt = eng.preflight("install", "system", "comp-a")
    assert receipt["verdict"] == "GO", _failed(receipt)
    assert _checks(receipt)["capability"]["evidence"]["held"] == {"sim": "self"}


def test_a_plan_needing_no_capability_passes_without_any_observation(tmp_path):
    eng = _engine(tmp_path, ops={"install": []})
    write_observation(eng.backend.root, drop=("operator", "sudo_listing"))
    receipt = eng.preflight("install", "system", "comp-a")
    assert receipt["verdict"] == "GO", _failed(receipt)


# --- each NO-GO path --------------------------------------------------------------------


@pytest.mark.parametrize("kind, code", [("claim", "claim-held"), ("postboot", "postboot-unmet"), ("unknown", "unknown-component")])
def test_plan_refusals_are_no_go_receipts_with_the_rest_skipped(tmp_path, kind, code):
    eng = _engine(tmp_path)
    system = "comp-a"
    if kind == "claim":
        coordinator.bootstrap_component(eng.backend.gates_root, "sim", "comp-a")
        coordinator.claim(eng.backend.gates_root, "sim", "comp-a", run_id=X.new_ulid(), scope="system", uid=os.getuid(), verb="apply")
    elif kind == "postboot":
        eng.backend.postboot_marker_path.unlink()
    else:
        system = "nope"
    receipt = eng.preflight("install", "system", system)
    assert receipt["verdict"] == "NO-GO"
    checks = _checks(receipt)
    assert (checks["plan"]["status"], checks["plan"]["reason"]) == ("fail", code)
    assert {c["status"] for cid, c in checks.items() if cid != "plan"} == {"skip"}
    assert receipt["plan"]["plan_digest"] is None
    PF.validate_receipt(receipt)


def test_dependency_not_installed_is_a_no_go(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _p, dep = H.write_contract(repo, "dep", ops={"install": []})
    _p, top = H.write_contract(repo, "top", ops=OPS, depends_on=["dep"])
    H.make_checkout(repo, [H.row("dep", dep), H.row("top", top)])
    eng = H.engine(tmp_path, repo)
    write_observation(eng.backend.root)
    assert _failed(eng.preflight("install", "system", "top")) == {"plan": "dependency-not-installed"}


def test_plan_digest_mismatch(tmp_path):
    eng = _engine(tmp_path)
    receipt = eng.preflight("install", "system", "comp-a", plan_digest="sha256:" + "0" * 64)
    assert _failed(receipt) == {"plan-digest": "plan-digest-mismatch"}


def test_plan_too_large(tmp_path, monkeypatch):
    eng = _engine(tmp_path)

    def too_large(_obj):
        raise CanonicalJsonError("json-too-large")

    monkeypatch.setattr(X, "encode_plan", too_large)
    assert _failed(eng.preflight("install", "system", "comp-a")) == {"plan-encodable": "plan-too-large"}


GATES = [
    {"id": "op-gate", "approval": {"class": "operator"}},
    {"id": "ia-gate", "irreversible": True, "approval": {"class": "independent-agent", "grant_source": "action-broker-approval-registry"}},
]


def test_operator_gate_unanswered_then_answered(tmp_path):
    eng = _engine(tmp_path, ops={"install": [H.dir_op("op-o", "/srv/o", gate="op-gate")]}, gates=GATES)
    assert _failed(eng.preflight("install", "system", "comp-a")) == {"gates": "gate-unanswered"}
    assert _failed(eng.preflight("install", "system", "comp-a", accept_gates=frozenset({"other"}))) == {"gates": "gate-unanswered"}
    assert eng.preflight("install", "system", "comp-a", accept_gates=frozenset())["verdict"] == "GO"
    assert eng.preflight("install", "system", "comp-a", accept_gates=frozenset({"op-gate"}))["verdict"] == "GO"


def test_reserved_authority_gate(tmp_path):
    eng = _engine(
        tmp_path, ops={"install": [H.dir_op("op-r", "/srv/r", gate="seat-plan-flip")]}, gates=GATES,
        reserved_authority=[{"id": "seat-plan-flip"}],
    )
    assert _failed(eng.preflight("install", "system", "comp-a", accept_gates=frozenset())) == {"gates": "reserved-authority"}


class CountingConsumer:
    def __init__(self):
        self.calls = 0

    def validate_and_bind(self, *args):
        self.calls += 1
        raise AssertionError("a preflight must never bind a grant")


def _grant_file(tmp_path, lines):
    path = tmp_path / "grants"
    path.write_text("".join(f"{op} {handle}\n" for op, handle in lines), encoding="ascii")
    os.chmod(path, 0o600)
    return path


def test_grants_presence_only_and_never_bound(tmp_path):
    eng = _engine(tmp_path, ops={"install": [H.dir_op("op-a", "/srv/a", gate="ia-gate")]}, gates=GATES)
    assert _failed(eng.preflight("install", "system", "comp-a")) == {"grants": "grant-required"}  # no consumer wired
    consumer = CountingConsumer()
    eng.grant_consumer = consumer
    assert _failed(eng.preflight("install", "system", "comp-a")) == {"grants": "grant-required"}  # no handle
    good = _grant_file(tmp_path, [("op-a", "f" * 64)])
    receipt = eng.preflight("install", "system", "comp-a", grant_file=good)
    assert receipt["verdict"] == "GO", _failed(receipt)
    grants = _checks(receipt)["grants"]["evidence"]
    assert grants == {"requests": ["op-a"], "present": ["op-a"], "binding": "not-checked (single-use)"}
    assert "f" * 64 not in json.dumps(receipt)  # the handle never reaches the receipt
    bad = _grant_file(tmp_path, [("op-a", "XYZ")])
    assert _failed(eng.preflight("install", "system", "comp-a", grant_file=bad)) == {"grants": "grant-refused"}
    assert consumer.calls == 0


def test_capability_missing_names_the_target_and_its_ops(tmp_path):
    eng = _engine(tmp_path)
    write_observation(eng.backend.root, listing=LISTING_SELF_ONLY)
    receipt = eng.preflight("install", "system", "comp-a")
    assert _failed(receipt) == {"capability": "capability-missing"}
    check = _checks(receipt)["capability"]
    assert check["evidence"]["missing"] == {"sim": "no-rule"} and "comp-a/op-1" in check["detail"]


def test_sudo_listing_unavailable(tmp_path):
    eng = _engine(tmp_path)
    write_observation(eng.backend.root, listing="", stderr="sudo: unable to resolve host\n", exit_code=1)
    assert _failed(eng.preflight("install", "system", "comp-a")) == {"capability": "sudo-listing-unavailable"}
    write_observation(eng.backend.root, listing="", exit_code=2)
    assert _failed(eng.preflight("install", "system", "comp-a")) == {"capability": "sudo-listing-unavailable"}
    write_observation(eng.backend.root, drop=("sudo_listing",))
    assert _failed(eng.preflight("install", "system", "comp-a")) == {"capability": "sudo-listing-unavailable"}


# --- T-5921: the typed sudo outcome, the real artisan capture, the host header ------------

FIXTURES = Path(__file__).parent / "fixtures" / "sudo_n_l"
#: Real captures (T-5921): artisan (no passwordless sudo) and Recovery's account WITH rules.
#: claude-fallback-listed.json lists the accounts that seat can sudo to: it stays in
#: pantheon-config and must NEVER be copied into a mirror or export payload.


def _capture(name):
    doc = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert doc["provenance"].startswith("CAPTURED")
    return doc


def test_the_real_artisan_capture_is_a_typed_no_passwordless_sudo(tmp_path):
    """artisan (suborch2), 2026-11-02: rc 1, stderr 'sudo: a password is required', stdout empty."""

    doc = _capture("suborch2-password-required.json")
    listing = PF.SudoListing(doc["exit_code"], doc["stdout"], doc["stderr"])
    assert PF.sudo_outcome(listing) == PF.SUDO_PASSWORD_REQUIRED
    operator = PF.OperatorIdentity(doc["account"], 4200, doc["host"])
    ev = PF.evaluate_capabilities({"root": ["c/op"], "other": ["c/op2"]}, operator, listing)
    assert (ev["status"], ev["reason"]) == ("fail", "capability-missing")
    assert ev["evidence"]["missing"] == {"other": "no-passwordless-sudo", "root": "no-passwordless-sudo"}
    assert ev["evidence"]["sudo_listing"] == "password-required" and "[sudo -n -l: password-required]" in ev["detail"]
    # the same seat preflighting its own ops needs no sudo at all (ruling Q4: artisan runs as itself)
    ev = PF.evaluate_capabilities({doc["account"]: ["c/op"]}, operator, listing)
    assert ev["status"] == "pass" and ev["evidence"]["held"] == {doc["account"]: "self"}
    # end to end through the simulated port, stderr separate
    eng = _engine(tmp_path)
    write_observation(eng.backend.root, user=doc["account"], host=doc["host"], listing=doc["stdout"],
                      stderr=doc["stderr"], exit_code=doc["exit_code"])
    receipt = eng.preflight("install", "system", "comp-a")
    assert _failed(receipt) == {"capability": "capability-missing"}
    assert _checks(receipt)["capability"]["evidence"]["missing"] == {"sim": "no-passwordless-sudo"}


def test_the_real_rules_capture_parses_fully_and_joins_wrapped_commands():
    """Recovery co-review F1: sudo wraps mid-command onto deeper-indented lines, often with no comma."""

    doc = _capture("claude-fallback-listed.json")
    listing = PF.SudoListing(doc["exit_code"], doc["stdout"], doc["stderr"])
    assert PF.sudo_outcome(listing) == PF.SUDO_LISTED
    parsed = PF.parse_sudo_listing(doc["stdout"], doc["account"], doc["host"])
    assert parsed.own_unparsed == () and parsed.unparsed == ()
    commands = [cmd for r in parsed.rules for _tags, cmd in r.items]
    assert "/usr/local/sbin/codex-recovery lease acquire claude-fallback" in commands
    assert "/usr/local/sbin/codex-recovery lease release claude-fallback" in commands
    assert "/usr/bin/systemctl daemon-reload" in commands
    assert "/usr/bin/journalctl -u kino --no-pager --since -24h" in commands
    assert "/bin/journalctl -k --no-pager --since -1h" in commands
    assert "/usr/local/sbin/codex-recovery lease acquire" not in commands  # never truncated into a broader one
    assert len(parsed.rules) == 15
    operator = PF.OperatorIdentity(doc["account"], 4500, doc["host"])
    ev = PF.evaluate_capabilities({"suborch2": ["c/a"], "root": ["c/b"], "nobody-x": ["c/c"]}, operator, listing)
    assert ev["evidence"]["held"] == {"suborch2": "sudo"}
    assert ev["evidence"]["missing"] == {"nobody-x": "no-rule", "root": "restricted-command"}
    assert ev["evidence"]["sudo_listing"] == "listed" and ev["reason"] == "capability-missing"


@pytest.mark.parametrize(
    "body",
    [
        "    (root) NOPASSWD: ALL, /usr/bin/x\n        y, !/usr/bin/su\n",  # wrapped mid-command, no comma
        "    (root) NOPASSWD: ALL,\n        !/usr/bin/su\n",
    ],
)
def test_a_wrapped_negation_is_never_lost(body):
    """Recovery co-review F2: a synthetic ALL plus a wrapped !/usr/bin/su is NOT held."""

    assert _listing_verdict(body, "root") == "negated"


def test_an_unparsed_own_section_line_makes_the_listing_unobservable():
    """Recovery co-review F2: fail CLOSED, even when another rule would grant."""

    ev = PF.evaluate_capabilities(
        {"root": ["c/op"]}, PF.OperatorIdentity("opr", 4100),
        PF.SudoListing(0, "User opr may run the following commands on vps:\n    (root) NOPASSWD: ALL\n    garbage\n"),
    )
    assert (ev["status"], ev["reason"]) == ("fail", "sudo-listing-unavailable")
    assert ev["evidence"]["sudo_listing"] == "unobservable" and ev["evidence"]["missing"] == {"root": "sudo-listing-unavailable"}
    assert ev["evidence"]["unparsed"] == ["garbage"] and "1 unparsed line(s)" in ev["detail"]
    # a foreign section's lines are unparsed but never make the listing unobservable
    text = "User bob may run the following commands on vps:\n    garbage\n\nUser opr may run the following commands on vps:\n    (root) NOPASSWD: ALL\n"
    ev = PF.evaluate_capabilities({"root": ["c/op"]}, PF.OperatorIdentity("opr", 4100), PF.SudoListing(0, text))
    assert ev["status"] == "pass" and ev["evidence"]["sudo_listing"] == "listed"


@pytest.mark.parametrize(
    "code, out, err, expected",
    [
        (0, "", "", "listed"),
        (1, "", "sudo: a password is required\n", "password-required"),
        (1, "sudo: a password is required\nrc=1\n", "", "password-required"),  # a merged-stream capture
        (1, "", "Sorry, user opr may not run sudo on vps.\n", "not-allowed"),
        (1, "User opr is not allowed to run sudo on vps.\n", "", "not-allowed"),
        (1, "", "sudo: unable to resolve host\n", "unobservable"),
        (2, "", "sudo: a password is required\n", "unobservable"),
        (127, "", "", "unobservable"),
    ],
)
def test_sudo_outcome_classes(code, out, err, expected):
    assert PF.sudo_outcome(PF.SudoListing(code, out, err)) == expected


def test_not_allowed_is_a_typed_miss_not_unavailable():
    ev = PF.evaluate_capabilities(
        {"root": ["c/op"]}, PF.OperatorIdentity("opr", 4100), PF.SudoListing(1, "", "Sorry, user opr may not run sudo on vps.\n")
    )
    assert (ev["reason"], ev["evidence"]["missing"]) == ("capability-missing", {"root": "no-passwordless-sudo"})


def test_a_section_for_another_host_grants_nothing():
    """T-5860 r2 nit 2: the header's host must be the observed host (short names compared)."""

    text = "User opr may run the following commands on other:\n    (root) NOPASSWD: ALL\n"
    ev = PF.evaluate_capabilities({"root": ["c/op"]}, PF.OperatorIdentity("opr", 4100, "swarm"), PF.SudoListing(0, text))
    assert ev["evidence"]["missing"] == {"root": "no-rule"}
    assert ev["evidence"]["unparsed"] == ["User opr may run the following commands on other:", "(root) NOPASSWD: ALL"]
    for listed in ("swarm", "swarm.example.net"):
        text = f"User opr may run the following commands on {listed}:\n    (root) NOPASSWD: ALL\n"
        ev = PF.evaluate_capabilities({"root": ["c/op"]}, PF.OperatorIdentity("opr", 4100, "swarm"), PF.SudoListing(0, text))
        assert ev["evidence"]["held"] == {"root": "sudo"}
    # no host known (an older simulated file): the header host is not checked
    assert PF.parse_sudo_listing(text, "opr").rules


def test_sim_operator_host_is_validated(tmp_path):
    eng = _engine(tmp_path)
    write_observation(eng.backend.root, host="bad host!")
    assert _failed(eng.preflight("install", "system", "comp-a")) == {"capability": "operator-unobservable"}


def test_unknown_privilege_is_a_typed_no_go_not_a_traceback(tmp_path, monkeypatch):
    """T-5860 r2 nit 1 (unreachable through parse today, so forced here)."""

    eng = _engine(tmp_path)

    def unknown(privilege, owner):
        raise PF.PreflightError("privilege-unknown", f"privilege {privilege!r} is not none, seat or root")

    monkeypatch.setattr(PF, "run_as_target", unknown)
    receipt = eng.preflight("install", "system", "comp-a")
    assert _failed(receipt) == {"capability": "privilege-unknown"} and receipt["operator"] is None
    PF.validate_receipt(receipt)


def test_operator_unobservable(tmp_path):
    eng = _engine(tmp_path)
    write_observation(eng.backend.root, drop=("operator",))
    assert _failed(eng.preflight("install", "system", "comp-a")) == {"capability": "operator-unobservable"}


def test_port_unwired_is_a_no_go(tmp_path):
    eng = _engine(tmp_path)
    eng.preflight_port = None
    assert _failed(eng.preflight("install", "system", "comp-a")) == {
        "capability": "observation-port-unwired", "source-hold": "observation-port-unwired"
    }


def test_missing_observation_file_is_unobservable_everywhere(tmp_path):
    eng = _engine(tmp_path)
    eng.backend.root.joinpath(*PF.SIM_OBSERVATION_REL).unlink()
    assert _failed(eng.preflight("install", "system", "comp-a")) == {
        "capability": "operator-unobservable", "source-hold": "checkout-unobservable"
    }


def test_checkout_unobservable_dirty_and_off_revision(tmp_path):
    eng = _engine(tmp_path)
    write_observation(eng.backend.root, drop=("checkout",))
    assert _failed(eng.preflight("install", "system", "comp-a")) == {"source-hold": "checkout-unobservable"}
    write_observation(eng.backend.root, head="not-hex")
    assert _failed(eng.preflight("install", "system", "comp-a")) == {"source-hold": "checkout-unobservable"}
    write_observation(eng.backend.root, clean=False)
    assert _failed(eng.preflight("install", "system", "comp-a")) == {"source-hold": "checkout-dirty"}


def test_checkout_must_be_at_the_plans_source_revision(tmp_path):
    eng = _engine(tmp_path, desired_schema={"source_revision": {}})
    receipt = eng.preflight("install", "system", "comp-a", desired={"source_revision": OTHER})
    assert _failed(receipt) == {"source-hold": "checkout-not-at-plan-revision"}
    assert _checks(receipt)["source-hold"]["evidence"]["plan_revisions"] == {"comp-a": OTHER}
    assert eng.preflight("install", "system", "comp-a", desired={"source_revision": HEAD})["verdict"] == "GO"


def test_ttl_bounds_refuse(tmp_path):
    eng = _engine(tmp_path)
    for ttl in (0, 59, 86401, True):
        with pytest.raises(EngineRefusal) as info:
            eng.preflight("install", "system", "comp-a", ttl_seconds=ttl)
        assert info.value.code == "inputs-refused"
    assert eng.preflight("install", "system", "comp-a", ttl_seconds=60)["ttl_seconds"] == 60


# --- sudo -n -l classification ----------------------------------------------------------


def _class(text, target):
    parsed = PF.parse_sudo_listing("User opr may run the following commands on vps:\n" + text)
    return [PF.rule_classification(r, target) for r in parsed.rules], list(parsed.unparsed)


@pytest.mark.parametrize(
    "line, target, expected",
    [
        ("    (codex) NOPASSWD: ALL\n", "codex", (["held"], [])),
        ("    (codex) NOPASSWD: ALL\n", "root", ([None], [])),
        ("    (ALL : ALL) ALL\n", "root", (["password-required"], [])),
        ("    (ALL : ALL) NOPASSWD: ALL\n", "anyone", (["held"], [])),
        ("    (root) NOPASSWD: /usr/bin/systemctl status x.service\n", "root", (["restricted-command"], [])),
        ("    (root) NOPASSWD: ALL, !/usr/bin/su\n", "root", (["negated"], [])),
        ("    (alice, bob) NOPASSWD: ALL\n", "bob", (["held"], [])),
        ("    (alice) NOPASSWD: SETENV: ALL\n", "alice", (["held"], [])),
        ("    (alice) NOPASSWD: /bin/a, PASSWD: ALL\n", "alice", (["password-required"], [])),
        ("    (alice) PASSWD: /bin/a, NOPASSWD: ALL\n", "alice", (["held"], [])),
        ("    (root) NOPASSWD: /bin/a,\n        /bin/b, ALL\n", "root", (["held"], [])),
        ("    (%admins) NOPASSWD: ALL\n", "root", ([], ["(%admins) NOPASSWD: ALL"])),
        ("    (#0) NOPASSWD: ALL\n", "root", ([], ["(#0) NOPASSWD: ALL"])),
        ("    (OPS_USERS) NOPASSWD: ALL\n", "root", ([], ["(OPS_USERS) NOPASSWD: ALL"])),
        ("    (!root) NOPASSWD: ALL\n", "alice", ([], ["(!root) NOPASSWD: ALL"])),
        ("    NOPASSWD: ALL\n", "root", ([], ["NOPASSWD: ALL"])),
        ("    (root) BOGUS: ALL\n", "root", ([], ["(root) BOGUS: ALL"])),
        ("    (root) CWD=/tmp ALL\n", "root", ([], ["(root) CWD=/tmp ALL"])),
        ("    ( : grp) NOPASSWD: ALL\n", "root", ([None], [])),
    ],
)
def test_sudo_rule_classification(line, target, expected):
    assert _class(line, target) == expected


def _listing_verdict(body, target, user="opr"):
    ev = PF.evaluate_capabilities(
        {target: ["c/op"]}, PF.OperatorIdentity(user, 4100),
        PF.SudoListing(0, "User opr may run the following commands on vps:\n" + body),
    )
    return ev["evidence"]["held"].get(target) or ev["evidence"]["missing"].get(target)


@pytest.mark.parametrize(
    "body, target, expected",
    [
        # review R1 F1 probes: sudoers is last-match-wins, sudo -l lists rules in order
        ("    (ALL) NOPASSWD: ALL\n    (root) PASSWD: ALL\n", "root", "password-required"),
        ("    (ALL) NOPASSWD: ALL\n    (root) NOPASSWD: !/bin/bash\n", "root", "negated"),
        ("    (root) PASSWD: ALL\n    (ALL) NOPASSWD: ALL\n", "root", "sudo"),  # the reverse order is held
        # a negation anywhere in a covering rule demotes, whatever its position
        ("    (root) NOPASSWD: !/bin/bash\n    (ALL) NOPASSWD: ALL\n", "root", "negated"),
        # a later password-tagged restricted rule may be the last match for the engine command
        ("    (ALL) NOPASSWD: ALL\n    (root) /usr/bin/foo\n", "root", "password-required"),
        ("    (ALL) NOPASSWD: ALL\n    (root) NOPASSWD: /usr/bin/foo\n", "root", "sudo"),
        # a rule that does not cover the target never overrides
        ("    (root) NOPASSWD: ALL\n    (alice) PASSWD: ALL\n", "root", "sudo"),
        ("    (alice) NOPASSWD: ALL\n    (ALL) PASSWD: ALL\n", "alice", "password-required"),
        ("    (root) NOPASSWD: /usr/bin/foo\n", "root", "restricted-command"),
        ("    (alice) NOPASSWD: ALL\n", "root", "no-rule"),
    ],
)
def test_covering_rules_are_walked_in_listing_order_last_match_wins(body, target, expected):
    assert _listing_verdict(body, target) == expected


def test_a_foreign_user_section_grants_nothing():
    """Review R1 F2: only the operator's own section is read; a foreign one goes to unparsed."""

    text = ("User bob may run the following commands on vps:\n    (root) NOPASSWD: ALL\n\n"
            "User opr may run the following commands on vps:\n    (opr) NOPASSWD: ALL\n")
    ev = PF.evaluate_capabilities({"root": ["c/op"]}, PF.OperatorIdentity("opr", 4100), PF.SudoListing(0, text))
    assert (ev["status"], ev["reason"], ev["evidence"]["missing"]) == ("fail", "capability-missing", {"root": "no-rule"})
    assert ev["evidence"]["unparsed"] == ["User bob may run the following commands on vps:", "(root) NOPASSWD: ALL"]
    only_bob = "User bob may run the following commands on vps:\n    (root) NOPASSWD: ALL\n"
    ev = PF.evaluate_capabilities({"root": ["c/op"]}, PF.OperatorIdentity("opr", 4100), PF.SudoListing(0, only_bob))
    assert ev["evidence"]["missing"] == {"root": "no-rule"}
    ev = PF.evaluate_capabilities({"root": ["c/op"]}, PF.OperatorIdentity("bob", 4101), PF.SudoListing(0, only_bob))
    assert ev["evidence"]["held"] == {"root": "sudo"}


def test_unknown_privilege_refuses():
    """Review R1 F4: an unknown privilege is a refusal, never "needs nothing"."""

    with pytest.raises(PF.PreflightError) as info:
        PF.required_capabilities([("a", "superuser", "x")])
    assert info.value.code == "privilege-unknown"
    assert PF.run_as_target("none", "x") is None


def test_defaults_sections_are_ignored_and_not_allowed_is_empty():
    parsed = PF.parse_sudo_listing(LISTING_SIM)
    assert [r.raw for r in parsed.rules] == ["(sim) NOPASSWD: ALL", "(root) NOPASSWD: /usr/local/sbin/pantheon-probe status"]
    assert parsed.unparsed == ()
    assert PF.parse_sudo_listing("User opr is not allowed to run sudo on vps.\n") == PF.ParsedListing((), ())


def test_unknown_lines_never_grant():
    ev = PF.evaluate_capabilities(
        {"root": ["x/op"]}, PF.OperatorIdentity("opr", 4100),
        PF.SudoListing(0, "User opr may run the following commands on vps:\n    (#0) NOPASSWD: ALL\n    garbage\n"),
    )
    # T-5921 (Recovery F2): own-section lines we cannot read make the whole listing unobservable
    assert (ev["status"], ev["reason"]) == ("fail", "sudo-listing-unavailable")
    assert ev["evidence"]["unparsed"] == ["(#0) NOPASSWD: ALL", "garbage"]


def test_root_is_held_by_uid_zero_and_derivation_maps_privilege():
    ev = PF.evaluate_capabilities({"root": ["x/op"]}, PF.OperatorIdentity("admin", 0), PF.SudoListing(1, ""))
    assert ev["status"] == "pass" and ev["evidence"]["held"] == {"root": "self"}
    assert PF.required_capabilities([("a", "none", "x"), ("b", "seat", "alice"), ("c", "root", "root"), ("d", "seat", "alice")]) == {
        "alice": ["b", "d"], "root": ["c"]
    }


def test_role_owners_resolve_through_the_instance_roles_or_refuse():
    opr = PF.OperatorIdentity("opr", 4100)
    listing = PF.SudoListing(0, "User opr may run the following commands on vps:\n    (acct-x) NOPASSWD: ALL\n")
    roles = {"role-x": "acct-x"}
    ev = PF.evaluate_capabilities({"role:role-x": ["c/op"]}, opr, listing, roles=roles)
    assert ev["status"] == "pass" and ev["evidence"]["resolved"] == {"role:role-x": "acct-x"} and ev["evidence"]["held"] == {"acct-x": "sudo"}
    ev = PF.evaluate_capabilities({"role:role-y": ["c/op"]}, opr, listing, roles=roles)
    assert (ev["status"], ev["reason"]) == ("fail", "owner-unresolved")
    ev = PF.evaluate_capabilities({"Not An Account": ["c/op"]}, opr, listing, roles=roles)
    assert (ev["status"], ev["reason"]) == ("fail", "owner-unresolved")


def test_role_accounts_reads_the_manifest_roles(tmp_path):
    assert PF.role_accounts(tmp_path) == {}
    (tmp_path / "instance.yaml").write_text(
        yaml.safe_dump({"roles": {"r1": {"persona": "p1", "account": "acct-1"}, "r2": {"account": "Bad Name"}, "r3": "x"}}),
        encoding="utf-8",
    )
    assert PF.role_accounts(tmp_path) == {"r1": "acct-1"}
    (tmp_path / "instance.yaml").write_text(":\n  - [unbalanced", encoding="utf-8")
    assert PF.role_accounts(tmp_path) == {}


# --- apply --preflight-receipt (opt-in) ------------------------------------------------


def _apply(eng, receipt, **kw):
    plan = eng.plan("install", "system", "comp-a")
    return eng.apply("install", "system", "comp-a", plan_digest=plan.digest, preflight_receipt=receipt, **kw)


def test_apply_with_a_go_receipt_runs(tmp_path):
    eng = _engine(tmp_path)
    receipt = eng.preflight("install", "system", "comp-a")
    [outcome] = _apply(eng, receipt)
    assert outcome.state == "SATISFIED"


def _refusal(eng, receipt, **kw):
    before = H.tree_snapshot(eng.backend.root)
    with pytest.raises(EngineRefusal) as info:
        _apply(eng, receipt, **kw)
    assert H.tree_snapshot(eng.backend.root) == before  # nothing claimed, admitted or written
    assert coordinator.read_claim(eng.backend.gates_root, "sim", "comp-a") is None
    return info.value.code


def test_apply_refuses_a_no_go_receipt_before_any_grant_or_claim(tmp_path):
    eng = _engine(tmp_path, ops={"install": [H.dir_op("op-a", "/srv/a", gate="ia-gate")]}, gates=GATES)
    consumer = CountingConsumer()
    eng.grant_consumer = consumer
    receipt = eng.preflight("install", "system", "comp-a")
    assert receipt["verdict"] == "NO-GO"
    assert _refusal(eng, receipt, grants={"op-a": "f" * 64}) == "preflight-no-go"
    assert consumer.calls == 0


def test_receipt_for_another_plan_is_refused(tmp_path):
    eng = _engine(tmp_path)
    receipt = eng.preflight("install", "system", "comp-a")
    other = dict(receipt, plan=dict(receipt["plan"], plan_digest="sha256:" + "1" * 64))
    assert _refusal(eng, PF.seal(other)) == "preflight-receipt-mismatch"
    for key, value in (("leg", "repair"), ("scope", "seat-sim"), ("system", None), ("instance", "prod")):
        assert _refusal(eng, PF.seal(dict(receipt, plan=dict(receipt["plan"], **{key: value})))) == "preflight-receipt-mismatch"


def test_receipt_digest_binds_through_a_changed_plan(tmp_path):
    """A receipt computed for plan A refuses once the plan has become B (the digest moved)."""

    eng = _engine(tmp_path)
    receipt = eng.preflight("install", "system", "comp-a")
    H.write_contract(eng.repo_root, "comp-a", ops={"install": [H.dir_op("op-9", "/srv/z")]})
    digest = H.write_contract(eng.repo_root, "comp-a", ops={"install": [H.dir_op("op-9", "/srv/z")]})[1]
    H.make_checkout(eng.repo_root, [H.row("comp-a", digest)])
    assert _refusal(eng, receipt) == "preflight-receipt-mismatch"


def test_tampered_receipt_is_malformed(tmp_path):
    eng = _engine(tmp_path)
    receipt = eng.preflight("install", "system", "comp-a", plan_digest="sha256:" + "0" * 64)
    assert receipt["verdict"] == "NO-GO"
    flipped = dict(receipt, verdict="GO")
    assert _refusal(eng, flipped) == "preflight-receipt-malformed"
    forged_checks = [dict(c, status="pass", reason=None) for c in receipt["checks"]]
    assert _refusal(eng, dict(receipt, checks=forged_checks, verdict="GO")) == "preflight-receipt-malformed"
    assert _refusal(eng, {"schema": PF.RECEIPT_SCHEMA}) == "preflight-receipt-malformed"


def test_expired_and_future_receipts(tmp_path):
    eng = _engine(tmp_path)
    receipt = eng.preflight("install", "system", "comp-a", ttl_seconds=600)
    eng.clock = lambda: NOW + dt.timedelta(seconds=601)
    assert _refusal(eng, receipt) == "preflight-receipt-expired"
    eng.clock = lambda: NOW - dt.timedelta(seconds=61)
    assert _refusal(eng, receipt) == "preflight-receipt-future"
    eng.clock = lambda: NOW + dt.timedelta(seconds=600)
    assert _apply(eng, receipt)[0].state == "SATISFIED"


def test_source_drift_after_the_preflight_refuses(tmp_path):
    eng = _engine(tmp_path)
    receipt = eng.preflight("install", "system", "comp-a")
    write_observation(eng.backend.root, head=OTHER)
    assert _refusal(eng, receipt) == "source-drift"
    write_observation(eng.backend.root, clean=False)
    assert _refusal(eng, receipt) == "source-drift"
    write_observation(eng.backend.root, drop=("checkout",))
    assert _refusal(eng, receipt) == "checkout-unobservable"


def test_apply_accept_gates_must_match_the_receipts(tmp_path):
    """Review R1 F3: the gates check proved one answer set; apply with another refuses."""

    eng = _engine(tmp_path, ops={"install": [H.dir_op("op-o", "/srv/o", gate="op-gate")]}, gates=GATES)
    receipt = eng.preflight("install", "system", "comp-a", accept_gates=frozenset({"op-gate"}))
    assert receipt["verdict"] == "GO" and _checks(receipt)["gates"]["evidence"]["accepted"] == ["op-gate"]
    assert _refusal(eng, receipt, accept_gates=frozenset()) == "preflight-receipt-mismatch"
    assert _refusal(eng, receipt, accept_gates=None) == "preflight-receipt-mismatch"  # checked before the gates
    bare = eng.preflight("install", "system", "comp-a", accept_gates=frozenset())
    assert _checks(bare)["gates"]["evidence"]["accepted"] == "all"
    assert _refusal(eng, bare, accept_gates=frozenset({"op-gate"})) == "preflight-receipt-mismatch"
    assert _apply(eng, receipt, accept_gates=frozenset({"op-gate"}))[0].state == "SATISFIED"


def test_apply_without_accept_gates_refuses_a_receipt_that_answered_them(tmp_path):
    eng = _engine(tmp_path, ops={"install": [H.dir_op("op-o", "/srv/o", gate="op-gate")]}, gates=GATES)
    receipt = eng.preflight("install", "system", "comp-a", accept_gates=frozenset())
    assert _refusal(eng, receipt, accept_gates=None) == "preflight-receipt-mismatch"
    (tmp_path / "u").mkdir()
    ungated = _engine(tmp_path / "u")
    plain = ungated.preflight("install", "system", "comp-a")
    assert _checks(plain)["gates"]["evidence"]["accepted"] is None
    assert _refusal(ungated, plain, accept_gates=frozenset()) == "preflight-receipt-mismatch"


def test_receipt_ttl_out_of_range_is_malformed(tmp_path):
    """Review R1 F5."""

    eng = _engine(tmp_path)
    receipt = eng.preflight("install", "system", "comp-a")
    observed = PF.parse_time(receipt["observed_at"])
    for ttl in (59, 86401):
        forged = PF.seal(dict(receipt, ttl_seconds=ttl, expires_at=PF.format_time(observed + dt.timedelta(seconds=ttl))))
        with pytest.raises(PF.PreflightError) as info:
            PF.validate_receipt(forged)
        assert info.value.code == "preflight-receipt-malformed" and "ttl_seconds" in info.value.message


def test_tier_and_port_refusals(tmp_path):
    eng = _engine(tmp_path)
    receipt = eng.preflight("install", "system", "comp-a")
    assert _refusal(eng, PF.seal(dict(receipt, observation_tier="live"))) == "preflight-tier-mismatch"
    eng.preflight_port = None
    assert _refusal(eng, receipt) == "preflight-port-unwired"


class RaisingPort:
    tier = "simulated"

    def __getattr__(self, name):
        raise AssertionError(f"apply without a receipt must not observe ({name})")


def test_apply_without_a_receipt_is_unchanged_and_never_observes(tmp_path):
    eng = _engine(tmp_path)
    eng.preflight_port = RaisingPort()
    [outcome] = _apply(eng, None)
    assert outcome.state == "SATISFIED"


# --- read-only proof --------------------------------------------------------------------


def _full_snapshot(root: Path):
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        out[str(p.relative_to(root))] = (st.st_mode, st.st_size, st.st_mtime_ns, p.read_bytes() if p.is_file() else None)
    return out


def _forbid(monkeypatch):
    def boom(name):
        def raiser(*_a, **_k):
            raise AssertionError(f"a preflight called {name}")
        return raiser

    for name in ("claim", "bootstrap_component", "release"):
        monkeypatch.setattr(coordinator, name, boom(f"coordinator.{name}"))
    monkeypatch.setattr(RunStore, "create", boom("RunStore.create"))
    monkeypatch.setattr(WalStore, "write_intent", boom("WalStore.write_intent"))
    monkeypatch.setattr(subprocess, "Popen", boom("subprocess.Popen"))
    monkeypatch.setattr(os, "system", boom("os.system"))


@pytest.mark.parametrize("verdict", ["GO", "NO-GO"])
def test_preflight_is_read_only(tmp_path, monkeypatch, verdict):
    eng = _engine(tmp_path, ops={"install": [H.dir_op("op-a", "/srv/a", gate="ia-gate"), H.dir_op("op-b", "/srv/b")]}, gates=GATES)
    consumer = CountingConsumer()
    eng.grant_consumer = consumer
    grants = _grant_file(tmp_path, [("op-a", "f" * 64)])
    # an existing HALTED run elsewhere in the store, so the store and WAL are non-empty
    _p, dep = H.write_contract(eng.repo_root, "comp-b", ops={"install": [H.dir_op("op-x", "/srv/x")]})
    H.make_checkout(eng.repo_root, [H.row("comp-a", H.write_contract(
        eng.repo_root, "comp-a", ops={"install": [H.dir_op("op-a", "/srv/a", gate="ia-gate"), H.dir_op("op-b", "/srv/b")]},
        gates=GATES)[1]), H.row("comp-b", dep)])
    eng.backend.faults.fail.add("op-x")
    with pytest.raises(EngineRefusal):
        eng.apply("install", "system", "comp-b", plan_digest=eng.plan("install", "system", "comp-b").digest)
    if verdict == "NO-GO":
        write_observation(eng.backend.root, listing=LISTING_SELF_ONLY)
    before = _full_snapshot(eng.backend.root)
    _forbid(monkeypatch)
    receipt = eng.preflight("install", "system", "comp-a", grant_file=grants)
    assert receipt["verdict"] == verdict, _failed(receipt)
    assert _full_snapshot(eng.backend.root) == before
    assert consumer.calls == 0


def test_a_refused_plan_preflight_is_read_only(tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    coordinator.bootstrap_component(eng.backend.gates_root, "sim", "comp-a")
    coordinator.claim(eng.backend.gates_root, "sim", "comp-a", run_id=X.new_ulid(), scope="system", uid=os.getuid(), verb="apply")
    before = _full_snapshot(eng.backend.root)
    _forbid(monkeypatch)
    assert _failed(eng.preflight("install", "system", "comp-a")) == {"plan": "claim-held"}
    assert _full_snapshot(eng.backend.root) == before


# --- CLI ---------------------------------------------------------------------------------


@pytest.fixture
def cli(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _p, digest = H.write_contract(repo, "comp-a", ops=OPS)
    H.make_checkout(repo, [H.row("comp-a", digest)])
    sim_root = tmp_path / "sim"
    SimulatedHostBackend(sim_root).simulate_postboot()
    write_observation(sim_root)
    return ["--repo-root", str(repo), "--simulated-host-root", str(sim_root)], sim_root


def _result(out):
    return [json.loads(line)["result"] for line in out.splitlines() if line.startswith('{"result"')][-1]


def test_cli_preflight_go_then_apply_with_the_saved_receipt(cli, capsys, tmp_path):
    common, sim_root = cli
    assert main(["plan", "install", "--system", "comp-a", "--preflight", *common]) == 0
    out = capsys.readouterr().out
    receipt = _result(out)["receipt"]
    assert receipt["verdict"] == "GO"
    saved = tmp_path / "receipt.jsonl"
    saved.write_text(out, encoding="utf-8")
    digest = receipt["plan"]["plan_digest"]
    assert main(["apply", "install", "--system", "comp-a", "--plan-digest", digest, "--preflight-receipt", str(saved), *common]) == 0
    assert _result(capsys.readouterr().out)[0]["state"] == "SATISFIED"


def test_cli_no_go_exits_one_and_apply_refuses_it(cli, capsys, tmp_path):
    common, sim_root = cli
    write_observation(sim_root, listing=LISTING_SELF_ONLY)
    assert main(["plan", "install", "--system", "comp-a", "--preflight", *common]) == 1
    out = capsys.readouterr().out
    receipt = _result(out)["receipt"]
    assert receipt["verdict"] == "NO-GO"
    saved = tmp_path / "receipt.jsonl"
    saved.write_text(out, encoding="utf-8")
    assert main(["apply", "install", "--system", "comp-a", "--plan-digest", receipt["plan"]["plan_digest"],
                 "--preflight-receipt", str(saved), *common]) == 1
    assert "refused: preflight-no-go" in capsys.readouterr().err


def test_cli_unreadable_receipt_and_preflight_only_flags(cli, capsys, tmp_path):
    common, _sim = cli
    junk = tmp_path / "junk"
    junk.write_text("not a receipt\n", encoding="utf-8")
    assert main(["apply", "install", "--system", "comp-a", "--plan-digest", "sha256:" + "0" * 64,
                 "--preflight-receipt", str(junk), *common]) == 1
    assert "refused: preflight-receipt-unreadable" in capsys.readouterr().err
    for flag in (["--accept-gates"], ["--receipt-ttl", "60"], ["--plan-digest", "x"], ["--grant-file", "g"]):
        assert main(["plan", "install", "--system", "comp-a", *flag, *common]) == 2
        assert "only apply with --preflight" in capsys.readouterr().err
    assert main(["plan", "install", "--system", "comp-a", "--preflight", "--receipt-ttl", "5", *common]) == 1
    assert "inputs-refused" in capsys.readouterr().err


def test_load_receipt_file_takes_the_last_result_line(tmp_path):
    path = tmp_path / "r"
    path.write_text('{"event": "x"}\n{"result": {"receipt": {"n": 1}}}\n{"result": {"receipt": {"n": 2}}}\n', encoding="utf-8")
    assert PF.load_receipt_file(path) == {"n": 2}
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(PF.PreflightError) as info:
        PF.load_receipt_file(link)
    assert info.value.code == "preflight-receipt-unreadable"


# --- dark pins and engine-generic -------------------------------------------------------


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def test_preflight_module_is_dark():
    tree = _tree(ENGINE_DIR / "preflight.py")
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        t.id for n in ast.walk(tree) if isinstance(n, ast.AnnAssign | ast.Assign)
        for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Name)
    }
    assert not [n for n in names if n.startswith("LIVE")], "no live flag"
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"subprocess", "socket", "shutil", "ctypes", "multiprocessing", "pty", "http", "urllib"}


def test_the_simulated_port_is_the_only_port_in_the_engine():
    classes = {
        n.name
        for path in ENGINE_DIR.glob("*.py")
        for n in ast.walk(_tree(path))
        if isinstance(n, ast.ClassDef) and n.name.endswith("Port") and n.name != "PreflightPort"
    }
    assert classes == {"SimulatedPreflightPort"}


def test_from_checkout_wires_the_simulated_port(tmp_path):
    eng = _engine(tmp_path)
    assert type(eng.preflight_port) is PF.SimulatedPreflightPort and eng.preflight_port.root == eng.backend.root


def _persona_words():
    """Every persona name and account bound in the real instance.yaml ``roles`` (read live, not a list)."""

    doc = yaml.safe_load((REPO_ROOT / "instance.yaml").read_text(encoding="utf-8"))
    words: set[str] = set()
    for role in (doc.get("roles") or {}).values():
        if isinstance(role, dict):
            words.update(v for k, v in role.items() if k in ("persona", "account") and isinstance(v, str))
    return words


def test_no_persona_or_account_names_in_the_touched_engine_modules():
    words = _persona_words()
    assert {"orchestrator", "recovery"} <= words, sorted(words)  # the walk found the fleet
    for name in ("preflight.py", "executor.py", "cli.py", "components.py"):
        strings = {n.value.lower() for n in ast.walk(_tree(ENGINE_DIR / name)) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        tokens = {t for s in strings for t in re.split(r"[^a-z0-9-]+", s) if t}
        assert not tokens & words, (name, sorted(tokens & words))


# --- T-5921: the optional component PREFLIGHT hook ----------------------------------------


def _sub(cid, status="pass", reason=None, detail="d", **evidence):
    return {"id": cid, "status": status, "reason": reason, "detail": detail, "evidence": evidence}


def _component(receipt):
    return _checks(receipt)["component"]


def test_no_hook_is_a_pass_listed_as_none(tmp_path):
    eng = _engine(tmp_path)
    receipt = eng.preflight("install", "system", "comp-a")
    assert receipt["verdict"] == "GO" and receipt["schema"] == "pantheon-germinate-preflight-receipt/v1"
    comp = _component(receipt)
    assert comp["status"] == "pass" and comp["evidence"] == {"components": {"comp-a": {"hook": "none", "checks": []}}}


def test_hook_sees_the_plan_data_and_a_read_only_probe(tmp_path):
    eng = _engine(tmp_path)
    write_observation(eng.backend.root, probe={
        "accounts": {"acct-a": {"uid": 4300, "home": "/home/acct-a"}},
        "paths": {"/run/user/4300/bus": {"kind": "socket", "uid": 4300, "gid": 4300, "mode": "0666"}},
        "files": {"/home/acct-a/f": "c" * 64},
        "json": {"/home/acct-a/j.json": {"k": 1}},
    })
    seen = {}

    def hook(ctx):
        seen.update(component=ctx.component, leg=ctx.leg, scope=ctx.scope, tier=ctx.tier,
                    ops=[o["operation_id"] for o in ctx.ops], repo=ctx.repo_root)
        seen["probe"] = (
            ctx.probe.account("acct-a"), ctx.probe.account("nobody"), ctx.probe.path_stat("/run/user/4300/bus"),
            ctx.probe.path_stat("/absent"), ctx.probe.file_sha256("/home/acct-a/f"), ctx.probe.read_json("/home/acct-a/j.json"),
        )
        assert not hasattr(ctx, "backend") and not hasattr(ctx.probe, "__dict__")
        return [_sub("one"), _sub("two", "vacuous", "not-here")]

    eng.preflight_hooks["comp-a"] = hook
    receipt = eng.preflight("install", "system", "comp-a")
    assert receipt["verdict"] == "GO", _failed(receipt)
    assert seen["component"] == "comp-a" and seen["leg"] == "install" and seen["scope"] == "system"
    assert seen["tier"] == "simulated" and seen["ops"] == ["op-1", "op-2"] and seen["repo"] == str(eng.repo_root)
    assert seen["probe"] == (
        PF.AccountInfo(4300, "/home/acct-a"), None, PF.PathStat("socket", 4300, 4300, "0666"), None, "c" * 64, {"k": 1}
    )
    comp = _component(receipt)
    assert [c["id"] for c in comp["evidence"]["components"]["comp-a"]["checks"]] == ["one", "two"]
    assert "1 component checks pass, 1 vacuous" in comp["detail"]


def test_a_failing_sub_check_is_a_no_go(tmp_path):
    eng = _engine(tmp_path)
    eng.preflight_hooks["comp-a"] = lambda ctx: [_sub("ok"), _sub("bad", "fail", "credential-mode", "mode 0644", mode="0644")]
    receipt = eng.preflight("install", "system", "comp-a")
    assert _failed(receipt) == {"component": "component-check-failed"}
    assert "comp-a/bad (credential-mode)" in _component(receipt)["detail"]
    PF.validate_receipt(receipt)


def _raises(ctx):
    raise RuntimeError("boom")


def _probes_unwired_path(ctx):
    ctx.probe.path_stat("relative/path")


@pytest.mark.parametrize(
    "hook, fragment",
    [
        (_raises, "component-hook-raised: RuntimeError: boom"),
        (_probes_unwired_path, "component-hook-raised: Unobservable"),
        (lambda ctx: [], "component-check-malformed"),
        (lambda ctx: None, "component-check-malformed"),
        (lambda ctx: [{"id": "x", "status": "pass"}], "component-check-malformed"),
        (lambda ctx: [_sub("x", "alarm", "r")], "component-check-malformed"),
        (lambda ctx: [_sub("x", "fail", None)], "component-check-malformed"),
        (lambda ctx: [_sub("x", "pass", "why")], "component-check-malformed"),
        (lambda ctx: [_sub("X Y")], "component-check-malformed"),
        (lambda ctx: [_sub("x"), _sub("x")], "component-check-malformed"),
        (lambda ctx: [_sub("x", n=float("nan"))], "component-check-malformed"),
        (lambda ctx: [{**_sub("x"), "extra": 1}], "component-check-malformed"),
    ],
)
def test_a_broken_hook_is_a_no_go_never_a_pass(tmp_path, hook, fragment):
    eng = _engine(tmp_path)
    eng.preflight_hooks["comp-a"] = hook
    receipt = eng.preflight("install", "system", "comp-a")
    assert _failed(receipt) == {"component": "component-hook-failed"}
    assert fragment in _component(receipt)["evidence"]["components"]["comp-a"]["error"]
    PF.validate_receipt(receipt)


def test_hook_with_no_port_is_a_no_go(tmp_path):
    eng = _engine(tmp_path)
    eng.preflight_port = None
    eng.preflight_hooks["comp-a"] = lambda ctx: [_sub("x")]
    assert _failed(eng.preflight("install", "system", "comp-a"))["component"] == "component-hook-failed"


def test_an_unobservable_probe_is_raised_to_the_hook(tmp_path):
    eng = _engine(tmp_path)
    write_observation(eng.backend.root, probe={"paths": {"/p": "unobservable"}})
    caught = []

    def hook(ctx):
        try:
            ctx.probe.path_stat("/p")
        except PF.Unobservable as exc:
            caught.append(str(exc))
            return [_sub("p", "fail", "unobservable", str(exc))]
        return [_sub("p")]

    eng.preflight_hooks["comp-a"] = hook
    assert _failed(eng.preflight("install", "system", "comp-a")) == {"component": "component-check-failed"}
    assert caught and "unobservable" in caught[0]
    write_observation(eng.backend.root)  # no probe section at all: every probe is unobservable
    assert _failed(eng.preflight("install", "system", "comp-a")) == {"component": "component-check-failed"}


@pytest.mark.parametrize("entry", [{"kind": "door", "uid": 1, "gid": 1, "mode": "0600"}, {"kind": "file", "uid": -1, "gid": 1, "mode": "0600"},
                                   {"kind": "file", "uid": 1, "gid": 1, "mode": "600"}])
def test_sim_probe_rejects_malformed_entries(tmp_path, entry):
    write_observation(tmp_path, probe={"paths": {"/p": entry}, "files": {"/f": "nothex"}, "accounts": {"a": {"uid": 1, "home": "rel"}}})
    port = PF.SimulatedPreflightPort(tmp_path)
    for call in (lambda: port.path_stat("/p"), lambda: port.file_sha256("/f"), lambda: port.account("a"),
                 lambda: port.account("Bad Name"), lambda: port.path_stat("/a/../b")):
        with pytest.raises(PF.Unobservable):
            call()


def test_the_hook_is_not_run_when_the_plan_is_refused(tmp_path):
    eng = _engine(tmp_path)
    calls = []
    eng.preflight_hooks["comp-a"] = lambda ctx: calls.append(1) or [_sub("x")]
    eng.backend.postboot_marker_path.unlink()
    receipt = eng.preflight("install", "system", "comp-a")
    assert _component(receipt)["status"] == "skip" and calls == []


def test_a_v0_receipt_is_refused():
    receipt = PF.seal({
        "schema": "pantheon-germinate-preflight-receipt/v0", "verdict": "GO", "plan": {k: None for k in PF.PLAN_KEYS},
        "observation_tier": "simulated", "evidence_scope": "x", "observed_at": "2026-11-02T12:00:00Z",
        "expires_at": "2026-11-02T13:00:00Z", "ttl_seconds": 3600, "operator": None,
        "checks": [PF.check(cid, PF.PASS, None, "") for cid in PF.CHECK_IDS[:7]],
    })
    with pytest.raises(PF.PreflightError) as info:
        PF.validate_receipt(receipt)
    assert info.value.code == "preflight-receipt-malformed"


HOOK_SRC = "def PREFLIGHT(ctx):\n    return [{'id': 'from-handlers', 'status': 'pass', 'reason': None, 'detail': 'registered'}]\n"


def test_a_handlers_module_registers_preflight(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    H.write_contract(repo, "comp-a", ops=OPS)
    digest = H.write_handlers(repo, "comp-a", HOOK_SRC)
    H.make_checkout(repo, [H.row("comp-a", digest)])
    eng = H.engine(tmp_path, repo)
    eng.clock = lambda: NOW
    write_observation(eng.backend.root)
    receipt = eng.preflight("install", "system", "comp-a")
    assert receipt["verdict"] == "GO", _failed(receipt)
    assert _component(receipt)["evidence"]["components"]["comp-a"]["checks"][0]["id"] == "from-handlers"


def test_a_non_callable_preflight_refuses_registration(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    H.write_contract(repo, "comp-a", ops=OPS)
    digest = H.write_handlers(repo, "comp-a", "PREFLIGHT = 3\n")
    H.make_checkout(repo, [H.row("comp-a", digest)])
    with pytest.raises(EngineRefusal) as info:
        H.engine(tmp_path, repo)
    assert info.value.code == "component-registration-refused" and "PREFLIGHT must be a callable" in info.value.message
