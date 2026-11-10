"""T-5830 (T-5091 U4, germination GF2): harness-manager configure sub-op (a), the
supervisor cutover, built DARK (UNTESTED-LIVE).

``plan_supervisor`` re-states tools/persona_cutover.sh (``supervisor: consume``) and
tools/persona_rollback.sh (``supervisor: bespoke``) as typed ops with declarative
pre/postconditions. The scripts and tools/persona_cutover_resolve.py stay untouched
(byte-hash pinned below) and remain the operative path. Ops run only through the
SIMULATED HarnessSupervisorSim (plus HarnessInstallSim for the installer's activation
suffix); nothing here actuates a host.

The ``test_real_*`` tests read the real repo, the real instance.yaml and the real,
cross-account-verified bespoke unit texts (tools/test_persona_cutover_resolve.py),
read-only. Engine-tier tests run on a tmp checkout over a simulated host, reusing the
GF1 fixture (its labelled operator-gate copy drives apply; the real contract refuses
grant-required: S2).

Each Recovery D15 (T-5830) binding condition (h)-(m) has its own test, named for it.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

from tools import persona_cutover_resolve as R
from tools import persona_installer as PI
from tools.germinate.engine.backend import KILL_POINTS, SimulatedKill
from tools.germinate.engine.components import load_repo_op_registry, read_op_kinds
from tools.germinate.engine.contract import load_contract
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.operation import parse_operation
from tools.germinate.engine.planner import InProcessJsonPlannerRunner, PlannerGuard
from tools.germinate.engine.tests import helpers as H
from tools.germinate.op_registry import RESERVED_CLASSES, component_kind_errors
from tools.germinate.planner_sandbox.admission import coordinator
from tools.test_persona_cutover_resolve import SPECIALIST_UNIT_TEXT, RECOVERY_UNIT_TEXT

from tools.germinate.components import harness_manager_supervisor as S
from tools.germinate.components.tests import test_harness_manager_component as GF1

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPONENTS = REPO_ROOT / "tools" / "germinate" / "components"
PLANNER = COMPONENTS / "harness-manager.py"
HANDLERS = COMPONENTS / "harness-manager_handlers.py"
HELPER = COMPONENTS / "harness_manager_supervisor.py"
CUTOVER = REPO_ROOT / "tools" / "persona_cutover.sh"
ROLLBACK = REPO_ROOT / "tools" / "persona_rollback.sh"
RESOLVER = REPO_ROOT / "tools" / "persona_cutover_resolve.py"
MANIFEST_PATH = REPO_ROOT / "instance.yaml"
CID = "harness-manager"
REV = "0123456789abcdef0123456789abcdef01234567"
STAMP = "20261101T120000Z"
UNIT_TEXTS = {"recovery": RECOVERY_UNIT_TEXT, "specialist": SPECIALIST_UNIT_TEXT}
SUPERVISOR_KINDS = (
    "harness.supervisor-file", "harness.supervisor-unit", "harness.companion-unit",
    "harness.control-plane-teardown", "harness.consume-retire", "harness.supervisor-commit",
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HM = _load(PLANNER, "_t5830_harness_manager")
HMH = _load(HANDLERS, "_t5830_harness_manager_handlers")


# --- fixtures: a real persona's observation, seeded from the resolver ------------


def _identity(persona):
    return R.resolve_persona(persona, manifest_path=MANIFEST_PATH, unit_text=UNIT_TEXTS[persona])


def _companions(identity):
    return [tuple(t.split("=", 1)) for t in identity.companion_units.split()]


def _paths(identity):
    return [str(identity.service_unit_path), str(identity.supervise_wrapper_path), str(identity.settings_json_path)]


def _consume_paths(identity):
    home = str(identity.home)
    dropin_dir = f"{home}/.config/systemd/user/{identity.consume_service}.d"
    return dropin_dir, f"{dropin_dir}/urgent.conf", f"{home}/.config/systemd/user/timers.target.wants/{identity.consume_timer}"


def _expected_pins(identity, stamp=STAMP):
    """Independent of the planner: the targets the resolver alone yields (Recovery D15 (h))."""

    companions = _companions(identity)
    files = _paths(identity)
    return {
        "units": sorted({identity.service, identity.consume_timer, identity.consume_service, S.CODEX_RECEIVER_UNIT,
                         S.RUNTIME_SCOPE_UNIT, S.RUNTIME_CONSUME_SLICE, *(u for u, _ in companions)}),
        "sessions": sorted({identity.tmux_session, *(s for _, s in companions)}),
        "files": sorted({*files, *(f"{f}.pre-cutover-{stamp}" for f in files), *_consume_paths(identity)}),
    }


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _installer_digest(persona):
    return PI.PersonaInstaller().plan(PI.Manifest.load(MANIFEST_PATH), persona, harness_override="claude", source_revision=REV).digest


def _claude_facts(identity):
    return {
        "control_plane_session": True,
        "runtime_scope_unit": S.RUNTIME_SCOPE_UNIT,
        "tmux_server_cgroup": f"0::/user.slice/user-{identity.uid}.slice/user@{identity.uid}.service/app.slice/{S.RUNTIME_SCOPE_UNIT}",
        "runtime": {"harness": "claude", "profile": "interactive-subscription"},
        "argv": f"/x/.npm-global/bin/claude --channels {identity.channels} --model {identity.model} --permission-mode bypassPermissions",
        "pane_text": "╭" + "─" * 30 + "╮\n> ",
        "telegram_poller_in_pane": True,
        "telegram_pollers": 1,
        "consume_environment": "PANTHEON_INSTANCE_MANIFEST=/x/instance.yaml",
        "claude_child": True,
        "bun_poller": True,
    }


def _seat(persona, **overrides):
    identity = _identity(persona)
    files = {p: {"kind": "file", "sha256": _sha(p)} for p in _paths(identity)}
    units = {
        identity.service: {"loaded": True, "enabled": True, "active": True},
        identity.consume_timer: {"loaded": True, "enabled": False, "active": False},
        identity.consume_service: {"loaded": True, "enabled": False, "active": False},
    }
    sessions = {identity.tmux_session: True, identity.tmux_session + "-sibling": True}
    for unit, session in _companions(identity):
        units[unit] = {"loaded": True, "enabled": True, "active": True}
        sessions[session] = True
    seat = {
        "uid": identity.uid,
        "user_bus": True,
        "seat_state": "awake",
        "bespoke_unit_text": UNIT_TEXTS[persona],
        "target_manifest_sha256": hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest(),
        "install_precondition": {"ok": True, "detail": "plan OK"},
        "staged": {"stamp": STAMP, "plan_digest": _installer_digest(persona), "receipt_valid": True},
        "manifest_loads": True,
        "supervisor": "bespoke",
        "units": units,
        "sessions": sessions,
        "files": files,
        "facts": _claude_facts(identity),
        "pins": _expected_pins(identity),
    }
    seat.update(overrides)
    return seat


def _request(persona, seat=None, **desired):
    identity = _identity(persona)
    desired = {"supervisor": "consume", "stamp": STAMP, "source_revision": REV} | desired
    desired = {k: v for k, v in desired.items() if v is not None}
    seat = seat if seat is not None else _seat(persona)
    return {
        "scope": f"seat-{identity.account}",
        "desired": desired,
        "observed": {HM.SUPERVISOR_HANDLER: {"account": identity.account, "seat": seat}},
    }


def _cutover(persona, **kw):
    return HM.plan_configure(_request(persona, **kw))


def _post_cutover_seat(persona):
    """The seat as a successful cutover leaves it (backups present, bespoke retired)."""

    identity = _identity(persona)
    seat = _seat(persona, supervisor="consume")
    for path in _paths(identity):
        seat["files"][f"{path}.pre-cutover-{STAMP}"] = dict(seat["files"][path])
    seat["units"][identity.service] = {"loaded": True, "enabled": False, "active": False}
    seat["sessions"][identity.tmux_session] = True  # the control-plane session (same name)
    seat["units"][S.RUNTIME_SCOPE_UNIT] = {"loaded": True, "enabled": False, "active": True}
    seat["facts"]["tmux_server_cgroup"] = f"0::/user.slice/user-{identity.uid}.slice/user@{identity.uid}.service/app.slice/x.service"
    return seat


def _restore(persona, seat=None, **desired):
    desired = {"supervisor": "bespoke", "source_revision": None} | desired
    return HM.plan_configure(_request(persona, seat if seat is not None else _post_cutover_seat(persona), **desired))


def _by_id(ops):
    return {op["operation_id"]: op for op in ops}


def _sv(ops):
    return [op for op in ops if op["kind"] in SUPERVISOR_KINDS or op["operation_id"].startswith("sv-")]


# --- the scripts and the resolver stay untouched ---------------------------------

#: sha256 of the operative path's bytes (T-5830 base; re-pinned by T-5956, which added the
#: companion pre-state snapshot; the supervisor-side parity is T-6047).
_PINNED = {
    CUTOVER: "247253a43b21291cb4f516ac5f8c958d2a455357d4e9457051842b771bab8ac2",
    ROLLBACK: "e6fb8fbcb489d190bfb8489a304c7b429c505fe377dfbf571ba59f84df20a303",
    RESOLVER: "c6325b6ca4fecda15cbb54cb7cfb183e94b28f123af5da14d7725522a7670982",
}


@pytest.mark.parametrize("path", sorted(_PINNED), ids=lambda p: p.name)
def test_real_operative_path_is_byte_unchanged(path):
    assert hashlib.sha256(path.read_bytes()).hexdigest() == _PINNED[path]


# --- invariant-inventory parity ---------------------------------------------------

def _inventoried(script):
    return {line for name, line, _m in S.INVENTORY if name == script}


_MAPPING_HEADS = {
    "plan", "structural:", "precondition:", "planner:", "out-of-scope:", "daemon-reload", "mkdir",
    "atomic-write", "installer", "commit", *SUPERVISOR_KINDS,
}


@pytest.mark.parametrize("path", (CUTOVER, ROLLBACK), ids=lambda p: p.name)
def test_real_inventory_is_an_exact_bijection_with_the_script_steps(path):
    """Review F2: EXACT. Every step line (mutation, guard, verify condition) has exactly
    one row at its own line number, and every row names a step line; no window, no
    first-word matching. A guard and its action are separate lines and rows."""

    missing, extra = S.inventory_parity(path.name, path.read_text(encoding="utf-8"))
    assert (missing, extra) == ([], []), f"{path.name}: unmapped step lines {missing}; rows that are not steps {extra}"
    rows = [line for name, line, _m in S.INVENTORY if name == path.name]
    assert len(rows) == len(set(rows)), "a step line has two rows"
    for name, number, mapping in S.INVENTORY:
        if name == path.name:
            assert mapping.split(" ")[0] in _MAPPING_HEADS, mapping


@pytest.mark.parametrize("path", (CUTOVER, ROLLBACK), ids=lambda p: p.name)
def test_deleting_any_single_inventory_row_fails_the_parity(path):
    """Self-test of the parity check: removing ANY one row is caught, at that exact line
    (the reviewer's five deleted rows among them)."""

    text = path.read_text(encoding="utf-8")
    rows = [row for row in S.INVENTORY if row[0] == path.name]
    assert rows
    for row in rows:
        reduced = tuple(r for r in S.INVENTORY if r is not row)
        missing, extra = S.inventory_parity(path.name, text, reduced)
        assert (missing, extra) == ([row[1]], []), row
    reviewer = {"persona_cutover.sh": (273, 248), "persona_rollback.sh": (366, 306, 376)}[path.name]
    assert set(reviewer) <= {r[1] for r in rows}


def test_guards_and_their_actions_are_separate_rows():
    rows = {(n, l) for n, l, _m in S.INVENTORY}
    for pair in ((247, 248), (273, 275)):
        assert {("persona_cutover.sh", line) for line in pair} <= rows
    for pair in ((305, 306), (311, 312), (329, 330), (366, 369)):
        assert {("persona_rollback.sh", line) for line in pair} <= rows


def test_inventory_names_every_supervisor_kind():
    named = " ".join(m for _n, _l, m in S.INVENTORY)
    assert all(kind in named for kind in SUPERVISOR_KINDS)


def test_real_every_script_verify_element_is_a_commit_check():
    for path in (CUTOVER, ROLLBACK):
        lines = path.read_text(encoding="utf-8").splitlines()
        rows = _inventoried(path.name)
        for number, line in enumerate(lines, 1):
            if re.search(r'echo "verify: .*(OK|WARN)"', line):
                assert any(0 <= number - r <= 8 for r in rows), f"{path.name}:{number} verify element not inventoried"


# --- constants re-stated from the scripts (parity) --------------------------------


def test_real_constants_match_the_scripts():
    rollback = ROLLBACK.read_text(encoding="utf-8")
    cutover = CUTOVER.read_text(encoding="utf-8")
    assert f'RUNTIME_SCOPE_UNIT="{S.RUNTIME_SCOPE_UNIT}"' in rollback
    assert f'CODEX_RECEIVER_UNIT="{S.CODEX_RECEIVER_UNIT}"' in rollback
    assert f'RUNTIME_CONSUME_SLICE="{S.RUNTIME_CONSUME_SLICE}"' in rollback
    assert f'URGENT_ENV="{S.URGENT_ENV}"' in cutover
    assert "printf '[Service]\\nEnvironment=%s=1\\n'" in cutover
    assert S.URGENT_DROPIN_CONTENT == "[Service]\nEnvironment=%s=1\n" % S.URGENT_ENV
    assert f"grep -qE '{S.COMPOSER_READY_RE.pattern}'" in cutover
    assert f"grep -qE '{S.CHANNELS_DIALOG_RE.pattern}'" in cutover
    assert f"--permission-mode {S.PERMISSION_MODE}" in cutover
    assert f'SELECTED_PROFILE" == "{S.CODEX_PROFILE}"' in cutover
    assert ".pre-cutover-${STAMP}" in cutover and S.backup_path("/a", "T") == "/a.pre-cutover-T"
    from tools import t2611_recovery_persona_apply as T

    assert S.STAMP_RE.pattern == T.STAMP_RE.pattern


# --- op data for two real personas --------------------------------------------------

_CUTOVER_IDS = {
    "recovery": ["sv-snapshot-0", "sv-snapshot-1", "sv-snapshot-2", "sv-daemon-reload", "sv-retire", "sv-require-quiesced", "sv-commit"],
    "specialist": ["sv-snapshot-0", "sv-snapshot-1", "sv-snapshot-2", "sv-daemon-reload", "sv-retire", "sv-companion-stop-0",
              "sv-require-quiesced", "sv-companion-start-0", "sv-commit"],
}
_RESTORE_IDS = {
    "recovery": ["sv-restore-0", "sv-restore-1", "sv-restore-2", "sv-restore-daemon-reload", "sv-teardown", "sv-consume-retire",
             "sv-reinstate", "sv-commit"],
    "specialist": ["sv-restore-0", "sv-restore-1", "sv-restore-2", "sv-restore-daemon-reload", "sv-teardown", "sv-consume-retire",
              "sv-reinstate", "sv-companion-start-0", "sv-commit"],
}


@pytest.mark.parametrize("persona", sorted(UNIT_TEXTS))
def test_real_cutover_op_snapshot(persona):
    identity = _identity(persona)
    ops = _cutover(persona)
    sv = [op for op in ops if op["operation_id"].startswith("sv-")]
    assert [op["operation_id"] for op in sv] == _CUTOVER_IDS[persona]
    by = _by_id(ops)
    assert [by[f"sv-snapshot-{i}"]["target"] for i in range(3)] == _paths(identity)
    assert by["sv-retire"]["target"] == identity.service and by["sv-retire"]["gate"] == "supervisor-cutover"
    assert [op["operation_id"] for op in ops if op.get("gate")] == ["sv-retire"]
    assert by["sv-commit"]["desired"]["supervisor"] == "consume"
    # the activation suffix is EXACTLY U1's adapter over the installer's own suffix
    plan = PI.PersonaInstaller().plan(PI.Manifest.load(MANIFEST_PATH), persona, harness_override="claude", source_revision=REV)
    index = next(i for i, op in enumerate(plan.operations) if op.kind in PI.ACTIVATION_OPERATION_KINDS)
    from types import SimpleNamespace

    suffix = HM._install_ops(SimpleNamespace(digest=plan.digest, operator_actions=plan.operator_actions, operations=plan.operations[index:]), REV)
    start = [op["operation_id"] for op in ops].index("sv-require-quiesced") + 1
    assert ops[start:start + len(suffix)] == suffix
    assert len(ops) == len(sv) + len(suffix)
    # every op parses against the real registry and contract
    registry = load_repo_op_registry(REPO_ROOT)
    contract = load_contract(COMPONENTS / "harness-manager.yaml", registry)
    parsed = [parse_operation(raw, contract=contract, op_registry=registry) for raw in ops]
    assert all(op.privilege in ("none", "seat") for op in parsed)


@pytest.mark.parametrize("persona", sorted(UNIT_TEXTS))
def test_real_restore_op_snapshot(persona):
    identity = _identity(persona)
    ops = _restore(persona)
    assert [op["operation_id"] for op in ops] == _RESTORE_IDS[persona]
    by = _by_id(ops)
    assert [by[f"sv-restore-{i}"]["target"] for i in range(3)] == _paths(identity)
    assert [op["operation_id"] for op in ops if op.get("gate")] == ["sv-reinstate"]
    assert by["sv-consume-retire"]["desired"]["staged_rollback"] == "manifest-pinned"
    assert by["sv-commit"]["desired"]["supervisor"] == "bespoke"
    registry = load_repo_op_registry(REPO_ROOT)
    contract = load_contract(COMPONENTS / "harness-manager.yaml", registry)
    for raw in ops:
        parse_operation(raw, contract=contract, op_registry=registry)


@pytest.mark.parametrize("persona", sorted(UNIT_TEXTS))
def test_real_plan_identity_is_the_resolvers(persona):
    """No re-derivation: channels / model / names come from resolve_persona unchanged."""

    identity = _identity(persona)
    commit = _by_id(_cutover(persona))["sv-commit"]
    checks = S.decode(commit["desired"]["postconditions"])
    argv = next(c for c in checks if c["check"] == "claude-launch-argv")
    assert argv["params"] == {"channels": identity.channels, "model": identity.model}
    assert commit["desired"]["xdg_runtime_dir"] == str(identity.xdg_runtime_dir) == f"/run/user/{identity.uid}"


def test_real_plan_supervisor_runs_under_the_json_planner_runner():
    ops = InProcessJsonPlannerRunner().run(PLANNER, "configure", _request("specialist"))
    assert [op["operation_id"] for op in ops if op["operation_id"].startswith("sv-")] == _CUTOVER_IDS["specialist"]


# --- purity under PlannerGuard ------------------------------------------------------


def _preimport():
    for name in ("tools.mailbox_onboard", "tools.keep_going", "tools.persona_bringup", "tools.agent_role_map", "tools.persona_apply_path"):
        __import__(name)


@pytest.mark.parametrize("shape", ("consume", "bespoke"))
@pytest.mark.parametrize("persona", sorted(UNIT_TEXTS))
def test_real_plan_supervisor_is_pure_under_planner_guard(persona, shape):
    _preimport()
    request = _request(persona) if shape == "consume" else _request(persona, _post_cutover_seat(persona), supervisor="bespoke", source_revision=None)
    unguarded = HM.plan_configure(json.loads(json.dumps(request)))
    guard = PlannerGuard()
    with guard:
        guarded = HM.plan_configure(json.loads(json.dumps(request)))
    assert guard.violations == []
    assert guarded == unguarded


# --- refusals -------------------------------------------------------------------------


def _without(seat, key):
    seat = dict(seat)
    seat.pop(key)
    return seat


@pytest.mark.parametrize(
    "mutate, desired, match",
    [
        (None, {"stamp": None}, "desired.stamp"),
        (None, {"stamp": "-bad"}, "desired.stamp"),
        (None, {"source_revision": "abc"}, "source_revision"),
        (None, {"harness": "codex"}, "D-L"),
        (None, {"urgent_env": "2"}, "urgent_env"),
        (lambda s: dict(s, target_manifest_sha256="0" * 64), {}, "stale config"),
        (lambda s: dict(s, seat_state="deep-sleep"), {}, "deep-sleep"),
        (lambda s: _without(s, "seat_state"), {}, "not proven awake"),
        (lambda s: dict(s, user_bus=False), {}, "user bus"),
        (lambda s: dict(s, install_precondition={"ok": False, "detail": "harness not on PATH"}), {}, "install-done"),
        (lambda s: dict(s, staged={}), {}, "staged checkpoint"),
        (lambda s: dict(s, staged=dict(s["staged"], plan_digest="x")), {}, "staged checkpoint"),
        (lambda s: dict(s, staged=dict(s["staged"], receipt_valid=False)), {}, "staged checkpoint"),
        (lambda s: dict(s, staged=dict(s["staged"], stamp="other")), {}, "staged checkpoint"),
        (lambda s: dict(s, supervisor="consume"), {}, "not bespoke"),
        (lambda s: dict(s, bespoke_unit_text="[Unit]\n"), {}, "persona resolution failed"),
        (lambda s: _without(s, "bespoke_unit_text"), {}, "no observed bespoke unit text"),
    ],
)
def test_cutover_refusals(mutate, desired, match):
    seat = _seat("recovery")
    if mutate is not None:
        seat = mutate(seat)
    with pytest.raises(HM.PlanRefused, match=match):
        HM.plan_configure(_request("recovery", seat, **desired))


def test_cutover_refuses_an_inactive_bespoke_service_and_an_existing_backup():
    identity = _identity("recovery")
    seat = _seat("recovery")
    seat["units"][identity.service] = {"loaded": True, "enabled": True, "active": False}
    with pytest.raises(HM.PlanRefused, match="not enabled\\+active"):
        HM.plan_configure(_request("recovery", seat))
    seat = _seat("recovery")
    seat["files"][f"{_paths(identity)[0]}.pre-cutover-{STAMP}"] = {"kind": "file", "sha256": "x"}
    with pytest.raises(HM.PlanRefused, match="fresh stamp"):
        HM.plan_configure(_request("recovery", seat))


def test_scope_and_shape_refusals():
    request = _request("recovery")
    with pytest.raises(HM.PlanRefused, match="seat scope"):
        HM.plan_configure(dict(request, scope="system"))
    with pytest.raises(HM.PlanRefused, match="desired.supervisor"):
        HM.plan_configure(dict(request, desired=dict(request["desired"], supervisor="systemd")))
    with pytest.raises(HM.PlanRefused, match="no harness-manager-supervisor seat observation"):
        HM.plan_configure(dict(request, observed={}))
    # another seat's scope over recovery's observation: the persona follows the scope, so the
    # observed staged checkpoint (recovery's installer digest) no longer matches
    with pytest.raises(HM.PlanRefused, match="not enabled\\+active|staged checkpoint"):
        HM.plan_configure(dict(request, scope="seat-claude-research"))


@pytest.mark.parametrize("index", range(3))
def test_restore_refuses_a_missing_backup_never_partial(index):
    identity = _identity("recovery")
    seat = _post_cutover_seat("recovery")
    del seat["files"][f"{_paths(identity)[index]}.pre-cutover-{STAMP}"]
    with pytest.raises(HM.PlanRefused, match="backup .* is missing"):
        _restore("recovery", seat)


def test_restore_refusals():
    with pytest.raises(HM.PlanRefused, match="break_glass"):
        _restore("recovery", break_glass="2")
    with pytest.raises(HM.PlanRefused, match="deep-sleep"):
        _restore("recovery", dict(_post_cutover_seat("recovery"), seat_state="deep-sleep"))
    with pytest.raises(HM.PlanRefused, match="recorded supervisor"):
        _restore("recovery", _without(_post_cutover_seat("recovery"), "supervisor"))


def test_restore_staged_rollback_pin_mode_is_explicit_in_the_plan():
    """rollback.sh:338-345: manifest-pinned normally; break-glass only when asked or
    when the manifest does not load, and then loudly (mode + reason in the plan)."""

    pinned = _by_id(_restore("recovery"))["sv-consume-retire"]
    assert pinned["desired"]["staged_rollback"] == "manifest-pinned"
    asked = _by_id(_restore("recovery", break_glass="1"))["sv-consume-retire"]
    assert asked["desired"]["staged_rollback"] == "break-glass"
    assert "explicit" in asked["provenance"]["staged_rollback_pin_reason"]
    auto = _by_id(_restore("recovery", dict(_post_cutover_seat("recovery"), manifest_loads=False)))["sv-consume-retire"]
    assert auto["desired"]["staged_rollback"] == "break-glass"
    assert "does not load" in auto["provenance"]["staged_rollback_pin_reason"]


def test_urgent_env_is_an_explicit_desired_value():
    identity = _identity("recovery")
    plain = _by_id(_cutover("recovery"))
    assert "sv-urgent-dropin" not in plain
    assert "urgent-env" not in plain["sv-commit"]["desired"]["postconditions"]
    urgent = _by_id(_cutover("recovery", urgent_env="1"))
    dropin = urgent["sv-urgent-dropin"]
    assert dropin["kind"] == "atomic-write" and dropin["target"] == _consume_paths(identity)[1]
    assert dropin["desired"]["content"] == S.URGENT_DROPIN_CONTENT
    assert urgent["sv-urgent-dir"]["kind"] == "mkdir"
    checks = S.decode(urgent["sv-commit"]["desired"]["postconditions"])
    assert {"check": "urgent-env", "params": {"assignment": f"{S.URGENT_ENV}=1"}} in checks


def test_drop_in_removal_moves_into_consume_retire_before_reinstate():
    ops = _restore("recovery")
    ids = [op["operation_id"] for op in ops]
    assert ids.index("sv-consume-retire") < ids.index("sv-reinstate")
    assert _by_id(ops)["sv-consume-retire"]["desired"]["dropin_path"].endswith("/urgent.conf")


def test_plan_configure_path_b_is_unaffected_by_the_dispatch():
    """Path (b) still answers a harness-only request (the byte-identical snapshot is
    test_harness_manager_install::test_plan_configure_output_is_byte_identical_to_base)."""

    request = {"scope": "seat-suborch1", "desired": {"harness": "codex"}, "observed": {"harness-manager-seat": {"account": "suborch1", "seat": GF1._seat()}}}
    assert [op["kind"] for op in HM.plan_configure(request)] == list(GF1.KINDS)


# --- Recovery D15 (T-5830) binding conditions ----------------------------------------------


@pytest.mark.parametrize("persona", sorted(UNIT_TEXTS))
def test_recovery_h_targets_are_resolver_pins_recorded_in_the_plan(persona):
    identity = _identity(persona)
    for ops in (_cutover(persona), _restore(persona)):
        commit = _by_id(ops)["sv-commit"]
        assert json.loads(commit["provenance"]["pinned_targets"]) == _expected_pins(identity)
        manifest_sha = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()
        assert all(op["provenance"]["instance_yaml_sha256"] == manifest_sha for op in ops if op["operation_id"].startswith("sv-"))


def test_recovery_h_a_foreign_target_refuses_at_plan_and_at_preflight():
    pins = _expected_pins(_identity("recovery"))
    ops = _cutover("recovery")
    tampered = json.loads(json.dumps(ops))
    _by_id(tampered)["sv-retire"]["target"] = "claude-telegram-bot.service"
    with pytest.raises(HM.PlanRefused, match="outside the resolver-derived pins"):
        HM._assert_pinned(tampered, pins)
    sim, op = _sim_with("recovery"), _inst(_by_id(tampered)["sv-retire"])
    assert not sim.preflight(op).ok and "outside the resolver-derived pins" in sim.preflight(op).detail


def test_recovery_i_user_manager_only():
    for ops in (_cutover("specialist"), _restore("specialist")):
        for op in ops:
            if op["kind"] in SUPERVISOR_KINDS:
                assert op["desired"]["systemd_manager"] == "user" and op["desired"]["account"] == op["owner"]
            if op["operation_id"] == "sv-daemon-reload":
                assert op["desired"]["manager"] == "user"
    sim = _sim_with("recovery")
    retire = _by_id(_cutover("recovery"))["sv-retire"]
    for change in ({"systemd_manager": "system"}, {"xdg_runtime_dir": "/run/user/0"}, {"account": "root"}):
        bad = _inst(dict(retire, desired=dict(retire["desired"], **change)))
        assert not sim.preflight(bad).ok


def _string_constants(path):
    """Every string constant except docstrings."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
        # STEP_RE is the PARSER of the scripts' text (it must spell their commands to match
        # them); it is a regex pattern, never a command the ops could run
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "STEP_RE" for t in node.targets):
            docstrings.update(id(n) for n in ast.walk(node.value) if isinstance(n, ast.Constant))
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]


@pytest.mark.parametrize("path", (PLANNER, HANDLERS, HELPER), ids=lambda p: p.name)
def test_recovery_i_string_pin_no_system_manager_sudo_or_machine(path):
    banned = re.compile(r"--system\b|\bsudo\b|--machine\b|(^|\s)-M(\s|$)|\bsystemctl\b")
    hits = [s for s in _string_constants(path) if banned.search(s)]
    assert hits == []


def test_recovery_j_exact_tmux_targets_and_the_prefix_hazard():
    # the T-5804 hazard, modelled: a bare target prefix-matches a sibling
    assert S.tmux_resolve("claude-telegram-bot", {"claude-telegram-bot-fallback"}) == "claude-telegram-bot-fallback"
    assert S.tmux_resolve("=claude-telegram-bot", {"claude-telegram-bot-fallback"}) is None
    for ops in (_cutover("specialist"), _restore("specialist"), _cutover("recovery"), _restore("recovery")):
        targets = [t for op in ops for t in op["desired"].get("tmux_targets", "").split(",") if t]
        assert targets and all(t.startswith("=") for t in targets)
        for op in ops:
            for check in S.decode(op["desired"].get("postconditions", "[]")):
                if check["check"] == "session":
                    assert check["params"]["target"].startswith("=")
    with pytest.raises(ValueError):
        S.session_of("claude-telegram-bot")
    sim = _sim_with("recovery")
    retire = _by_id(_cutover("recovery"))["sv-retire"]
    bare = _inst(dict(retire, desired=dict(retire["desired"], tmux_targets=retire["desired"]["tmux_targets"][1:])))
    assert not sim.preflight(bare).ok


def test_recovery_k_no_symlink_follow_at_plan_and_preflight():
    identity = _identity("recovery")
    seat = _seat("recovery")
    seat["files"][_paths(identity)[2]] = {"kind": "symlink", "sha256": "x"}
    with pytest.raises(HM.PlanRefused, match="symlink"):
        HM.plan_configure(_request("recovery", seat))
    seat = _post_cutover_seat("recovery")
    seat["files"][f"{_paths(identity)[1]}.pre-cutover-{STAMP}"] = {"kind": "symlink", "sha256": "x"}
    with pytest.raises(HM.PlanRefused, match="symlink"):
        _restore("recovery", seat)
    snapshot = _by_id(_cutover("recovery"))["sv-snapshot-0"]
    assert snapshot["desired"]["write"] == "atomic-no-follow"
    sim = _sim_with("recovery")
    state = sim.dump_state()
    state["seats"][identity.account]["files"][snapshot["desired"]["backup_path"]] = {"kind": "symlink", "sha256": "y"}
    sim.load_state(state)
    assert "symlink" in sim.preflight(_inst(snapshot)).detail
    sim = _sim_with("recovery")
    copying = _inst(dict(snapshot, desired=dict(snapshot["desired"], write="copy")))
    assert not sim.preflight(copying).ok


def test_recovery_l_single_receiver():
    for ops in (_cutover("specialist"), _restore("specialist")):
        for op in ops:
            if op["kind"] == "harness.control-plane-teardown":
                assert op["desired"]["receiver_unit"] == S.CODEX_RECEIVER_UNIT
            if op["kind"] == "harness.companion-unit":
                assert op["desired"]["session_unit"] in {u for u, _ in _companions(_identity("specialist"))}
        checks = S.decode(_by_id(ops)["sv-commit"]["desired"]["postconditions"])
        assert {"check": "single-receiver", "params": {}} in checks
    view = {"facts": {"telegram_pollers": 2}}
    assert S.evaluate([S.check("single-receiver")], view)[0]["ok"] is False
    assert S.evaluate([S.check("single-receiver")], {"facts": {}})[0]["ok"] is False  # could not ask


def test_recovery_m_ordering_gates_are_explicit_preconditions():
    identity = _identity("specialist")
    ops = _cutover("specialist")
    ids = [op["operation_id"] for op in ops]
    barrier = _by_id(ops)["sv-require-quiesced"]
    assert ids.index("sv-retire") < ids.index("sv-companion-stop-0") < ids.index("sv-require-quiesced")
    assert not ops[ids.index("sv-require-quiesced") + 1]["operation_id"].startswith("sv-")  # activation follows
    assert barrier["precondition"]["sessions_absent"].split(",") == [S.exact_target(identity.tmux_session), "=acp-bridge-specialist"]
    reinstate = _by_id(_restore("specialist"))["sv-reinstate"]["precondition"]
    assert S.RUNTIME_SCOPE_UNIT in reinstate["units_inactive"] and S.CODEX_RECEIVER_UNIT in reinstate["units_inactive"]
    assert identity.consume_timer in reinstate["units_not_found"] and reinstate["files_absent"] == _consume_paths(identity)[2]
    # the sim refuses the barrier while the bespoke session lives, and the reinstate while the scope is up
    sim = _sim_with("specialist")
    assert "still present" in sim.preflight(_inst(barrier)).detail
    sim = _sim_with("specialist", _post_cutover_seat("specialist"))
    assert "gate:" in sim.preflight(_inst(_by_id(_restore("specialist"))["sv-reinstate"])).detail


# --- simulated handler: unit tier ---------------------------------------------------------


def _inst(raw):
    return PI.InstallOperation(
        operation_id=raw["operation_id"], layer=raw["layer"], kind=raw["kind"], target=raw["target"],
        precondition=dict(raw["precondition"]), desired=dict(raw["desired"]), inverse=raw["inverse"],
        privilege=raw["privilege"], owner=raw["owner"], provenance=dict(raw["provenance"]), execution_scope="all",
    )


def _sim_with(persona, seat=None):
    sim = HMH.HarnessSupervisorSim()
    sim.load_state({"seats": {_identity(persona).account: seat if seat is not None else _seat(persona)}})
    return sim


def test_sim_round_trip_and_supersession_unit():
    sim = _sim_with("specialist")
    by = _by_id(_cutover("specialist"))
    stop, start = _inst(by["sv-companion-stop-0"]), _inst(by["sv-companion-start-0"])
    assert sim.preflight(stop).ok
    m_stop, ev = sim.execute(stop)
    assert ev.ok and sim.verify_operation(stop).ok and sim.can_rollback(m_stop).classification == "owned"
    m_start, _ = sim.execute(start)
    assert sim.verify_operation(start).ok
    assert "superseded" in sim.verify_operation(stop).detail and sim.verify_operation(stop).ok
    # T-6047 intended change: start puts the unit and session cells back but the pre-state
    # record stays (first record wins), so stop alone no longer reads not-applied;
    # the engine reverses newest first, so start is undone before stop is asked
    assert sim.can_rollback(m_stop).classification == "drifted"
    assert sim.can_rollback(m_start).classification == "owned"
    sim.rollback_mutation(m_start)
    assert sim.can_rollback(m_stop).classification == "owned"
    sim.rollback_mutation(m_stop)
    assert sim.dump_state()["seats"]["claude-research"]["units"] == _seat("specialist")["units"]


def test_sim_unknown_seat_and_drift_refuse():
    sim = HMH.HarnessSupervisorSim()
    assert not sim.preflight(_inst(_by_id(_cutover("recovery"))["sv-retire"])).ok
    sim = _sim_with("recovery")
    retire = _inst(_by_id(_cutover("recovery"))["sv-retire"])
    mutation, _ = sim.execute(retire)
    state = sim.dump_state()
    state["seats"]["claude-fallback"]["units"][_identity("recovery").service]["enabled"] = True
    sim.load_state(state)
    assert sim.can_rollback(mutation).classification == "drifted"


def test_checks_never_pass_on_missing_or_malformed_facts():
    for check_id in ("control-plane-session", "scope-owns-tmux-server", "scope-clean", "runtime-harness-declared",
                     "urgent-env", "single-receiver", "process-present"):
        params = {"scope-clean": {"scope_unit": "x"}, "urgent-env": {"assignment": "A=1"}, "process-present": {"which": "claude"}}.get(check_id, {})
        [result] = S.evaluate([{"check": check_id, "params": params}], {"facts": {}})
        assert result["ok"] is False, check_id
    [broken] = S.evaluate([{"check": "file", "params": {}}], {})
    assert broken["ok"] is False and "raised" in broken["detail"]
    ok, detail = S.verdict(S.evaluate([S.check("process-present", advisory=True, which="bun")], {"facts": {}}))
    assert ok and "advisory" in detail


def test_codex_branch_checks():
    view = {"facts": {"runtime": {"harness": "codex", "profile": "interactive-chatgpt"}, "initial_viability": True}}
    assert S.evaluate([S.check("codex-profile-viability")], view)[0]["ok"]
    assert S.evaluate([S.check("claude-launch-argv", channels="c", model="m")], view)[0]["ok"]  # not applicable
    view["facts"]["initial_viability"] = False
    assert not S.evaluate([S.check("codex-profile-viability")], view)[0]["ok"]
    assert not S.evaluate([S.check("runtime-harness-declared")], {"facts": {"runtime": {"harness": "x"}}})[0]["ok"]


# --- engine tier (tmp checkout, simulated host) -------------------------------------------


def _engine(tmp_path, persona, *, gate_class="operator", seat=None):
    identity = _identity(persona)
    scope = f"seat-{identity.account}"
    repo = GF1._repo(tmp_path, gate_class=gate_class)
    eng = GF1._engine(tmp_path, repo, deps=False)
    GF1._install_deps(eng, scope)
    eng.backend.handler("unit-fire").add_seat(identity.account)
    eng.backend.handler("harness.supervisor-unit").load_state({"seats": {identity.account: seat or _seat(persona)}})
    eng.backend.save()
    return repo, eng, scope


def _sv_state(eng, persona):
    seat = eng.backend.handler("harness.supervisor-unit").dump_state()["seats"][_identity(persona).account]
    return {k: v for k, v in seat.items() if k not in ("writers", "verdicts")}


def _desired(shape="consume"):
    if shape == "consume":
        return {"supervisor": "consume", "stamp": STAMP, "source_revision": REV}
    return {"supervisor": "bespoke", "stamp": STAMP}


def _apply(eng, scope, plan, shape="consume", accept=frozenset()):
    return eng.apply("configure", scope, CID, plan_digest=plan.digest, accept_gates=accept, desired=_desired(shape))


def test_engine_real_contract_refuses_grant_required_before_any_mutation(tmp_path):
    _repo, eng, scope = _engine(tmp_path, "recovery", gate_class="independent-agent")
    plan = eng.plan("configure", scope, CID, desired=_desired())
    before = H.tree_snapshot(eng.backend.root)
    for accept in (None, frozenset(), frozenset({"supervisor-cutover"})):
        with pytest.raises(EngineRefusal) as info:
            _apply(eng, scope, plan, accept=accept)
        assert info.value.code == "grant-required"
    assert H.tree_snapshot(eng.backend.root) == before


@pytest.mark.parametrize("persona", sorted(UNIT_TEXTS))
def test_engine_cutover_applies_then_verify_rechecks_every_receipt(tmp_path, persona):
    identity = _identity(persona)
    _repo, eng, scope = _engine(tmp_path, persona)
    plan = eng.plan("configure", scope, CID, desired=_desired())
    [outcome] = _apply(eng, scope, plan)
    assert outcome.state == "SATISFIED"
    seat = _sv_state(eng, persona)
    assert seat["supervisor"] == "consume"
    assert seat["units"][identity.service] == {"loaded": True, "enabled": False, "active": False}
    assert identity.tmux_session not in seat["sessions"]
    assert seat["sessions"][identity.tmux_session + "-sibling"] is True  # Recovery D15 (j): the sibling survives
    for path in _paths(identity):
        assert seat["files"][f"{path}.pre-cutover-{STAMP}"] == seat["files"][path]
    for unit, session in _companions(identity):
        assert seat["units"][unit]["active"] is True and seat["sessions"][session] is True
    [result] = eng.verify(scope, CID)
    assert result["checks"] and all(c["ok"] for c in result["checks"]), [c for c in result["checks"] if not c["ok"]]


def test_engine_failed_postcondition_halts_and_rollback_restores_every_handler(tmp_path):
    seat = _seat("specialist")
    seat["facts"]["telegram_pollers"] = 2  # Recovery D15 (l): a second poller fails the commit
    _repo, eng, scope = _engine(tmp_path, "specialist", seat=seat)
    sv_before = _sv_state(eng, "specialist")
    install_before = eng.backend.handler("unit-fire").dump_state()
    plan = eng.plan("configure", scope, CID, desired=_desired())
    with pytest.raises(EngineRefusal) as info:
        _apply(eng, scope, plan)
    assert info.value.code == "halted" and "single-receiver" in info.value.message
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    assert _sv_state(eng, "specialist") == sv_before
    assert eng.backend.handler("unit-fire").dump_state() == install_before


def test_engine_rollback_refuses_a_drifted_cell(tmp_path):
    seat = _seat("recovery")
    seat["facts"]["control_plane_session"] = False
    identity = _identity("recovery")
    _repo, eng, scope = _engine(tmp_path, "recovery", seat=seat)
    plan = eng.plan("configure", scope, CID, desired=_desired())
    with pytest.raises(EngineRefusal):
        _apply(eng, scope, plan)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    handler = eng.backend.handler("harness.supervisor-unit")
    state = handler.dump_state()
    state["seats"][identity.account]["units"][identity.service]["enabled"] = True  # someone else re-enabled it
    handler.load_state(state)
    eng.backend.save()
    with pytest.raises(EngineRefusal) as info:
        eng.rollback(run_id)
    assert info.value.code == "rollback-incomplete"
    store, run_tuple = eng._find_run(run_id)
    with store.open(run_tuple) as handle:
        record = handle.read_record()
    assert "sv-retire" in record["rollback_incomplete"]["outstanding_inverses"]


def test_engine_restore_after_a_satisfied_cutover(tmp_path):
    """A SATISFIED run is absorbing (rollback refuses it), so restore is the bespoke plan."""

    identity = _identity("specialist")
    _repo, eng, scope = _engine(tmp_path, "specialist")
    plan = eng.plan("configure", scope, CID, desired=_desired())
    [outcome] = _apply(eng, scope, plan)
    assert outcome.state == "SATISFIED"
    with pytest.raises(EngineRefusal) as info:
        eng.rollback(outcome.run_id)
    assert info.value.code == "rollback-refused"
    handler = eng.backend.handler("harness.supervisor-unit")
    state = handler.dump_state()
    seat = state["seats"][identity.account]
    seat["units"][S.RUNTIME_SCOPE_UNIT] = {"loaded": True, "enabled": False, "active": True}
    seat["sessions"][identity.tmux_session] = True  # the control-plane session
    seat["facts"]["tmux_server_cgroup"] = "0::/user.slice/x.service"
    handler.load_state(state)
    eng.backend.save()
    restore = eng.plan("configure", scope, CID, desired=_desired("bespoke"))
    [outcome] = _apply(eng, scope, restore, "bespoke")
    assert outcome.state == "SATISFIED"
    seat = _sv_state(eng, "specialist")
    assert seat["supervisor"] == "bespoke"
    assert seat["units"][identity.service] == {"loaded": True, "enabled": True, "active": True}
    assert seat["units"][S.RUNTIME_SCOPE_UNIT]["active"] is False
    assert identity.consume_timer not in seat["units"] and identity.consume_service not in seat["units"]
    assert seat["sessions"][identity.tmux_session] is True and seat["sessions"][identity.tmux_session + "-sibling"] is True
    [result] = eng.verify(scope, CID)
    assert all(c["ok"] for c in result["checks"]), [c for c in result["checks"] if not c["ok"]]


_KILL_OPS = ("sv-snapshot-0", "sv-retire", "sv-companion-stop-0", "sv-require-quiesced", "sv-companion-start-0", "sv-commit")


@pytest.mark.parametrize("point", KILL_POINTS)
@pytest.mark.parametrize("op_id", _KILL_OPS)
def test_engine_kill_then_reconcile_then_rollback_restores_preimage(tmp_path, op_id, point):
    """ORG-080 D-X kill-injection over every supervisor op kind in the cutover."""

    repo, eng, scope = _engine(tmp_path, "specialist")
    sv_before = _sv_state(eng, "specialist")
    plan = eng.plan("configure", scope, CID, desired=_desired())
    eng.backend.faults.kill = (op_id, point)
    with pytest.raises(SimulatedKill):
        _apply(eng, scope, plan)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    restarted = H.engine(tmp_path, repo)
    outcome = restarted.reconcile_run(run_id, executor_dead=True)
    if op_id == "sv-commit" and point in ("after-manifest", "after-record"):
        assert outcome.state == "SATISFIED"
        return
    assert outcome.state == "HALTED"
    assert restarted.rollback(run_id).state == "ROLLED_BACK"
    seat = restarted.backend.handler("harness.supervisor-unit").dump_state()["seats"][_identity("specialist").account]
    assert {k: v for k, v in seat.items() if k not in ("writers", "verdicts")} == sv_before


@pytest.mark.parametrize("point", KILL_POINTS)
@pytest.mark.parametrize("op_id", ("sv-restore-0", "sv-teardown", "sv-consume-retire", "sv-reinstate"))
def test_engine_restore_kill_then_rollback_restores_preimage(tmp_path, op_id, point):
    repo, eng, scope = _engine(tmp_path, "recovery", seat=_post_cutover_seat("recovery"))
    handler = eng.backend.handler("harness.supervisor-unit")
    state = handler.dump_state()
    # make every restore op a real change: the live unit file drifted since the snapshot
    state["seats"]["claude-fallback"]["files"][_paths(_identity("recovery"))[0]] = {"kind": "file", "sha256": "edited"}
    handler.load_state(state)
    eng.backend.save()
    sv_before = _sv_state(eng, "recovery")
    plan = eng.plan("configure", scope, CID, desired=_desired("bespoke"))
    eng.backend.faults.kill = (op_id, point)
    with pytest.raises(SimulatedKill):
        _apply(eng, scope, plan, "bespoke")
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    restarted = H.engine(tmp_path, repo)
    assert restarted.reconcile_run(run_id, executor_dead=True).state == "HALTED"
    assert restarted.rollback(run_id).state == "ROLLED_BACK"
    seat = restarted.backend.handler("harness.supervisor-unit").dump_state()["seats"]["claude-fallback"]
    assert {k: v for k, v in seat.items() if k not in ("writers", "verdicts")} == sv_before


# --- review F1 + Recovery D15 (T-5830) (n): the composite sv-retire undo ------------------------


def _seed_control_plane_leftover(eng, persona, stuck=()):
    """What a real activation leaves (the supervisor sim cannot see HarnessInstallSim):
    the control-plane session under the bespoke name, an active runtime scope and an
    active codex receiver. ``stuck`` is the SIM fault for a teardown that will not take."""

    identity = _identity(persona)
    handler = eng.backend.handler("harness.supervisor-unit")
    state = handler.dump_state()
    seat = state["seats"][identity.account]
    seat["sessions"][identity.tmux_session] = True
    seat["units"][S.RUNTIME_SCOPE_UNIT] = {"loaded": True, "enabled": False, "active": True}
    seat["units"][S.CODEX_RECEIVER_UNIT] = {"loaded": True, "enabled": True, "active": True}
    seat["units"][S.RUNTIME_CONSUME_SLICE] = {"loaded": True, "enabled": False, "active": True}
    if stuck:
        seat["teardown_stuck"] = list(stuck)
    handler.load_state(state)
    eng.backend.save()


def _transcript(eng, run_id):
    store, run_tuple = eng._find_run(run_id)
    with store.open(run_tuple) as handle:
        events = [json.loads(line) for line in handle.read_transcript()]
        return [e for e in events if isinstance(e, dict)], handle.read_record()


def _pages(events):
    return [e for e in events if e["event"] == "escalation.page"]


def _activation_ids(plan):
    ids = [op.operation_id for op in plan.nodes[0].operations]
    start = ids.index("sv-require-quiesced") + 1
    end = next(i for i in range(start, len(ids)) if ids[i].startswith("sv-"))
    return ids[start:end]


def _halt_at(eng, scope, plan, op_id):
    eng.backend.faults.fail.add(op_id)
    with pytest.raises(EngineRefusal) as info:
        _apply(eng, scope, plan)
    assert info.value.code == "halted"
    eng.backend.faults.fail.discard(op_id)
    return coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]


@pytest.mark.parametrize("where", ("first-activation", "last-activation", "sv-companion-start-0", "sv-commit"))
def test_f1_failure_after_activation_with_leftover_rolls_back_to_bespoke(tmp_path, where):
    identity = _identity("specialist")
    _repo, eng, scope = _engine(tmp_path, "specialist")
    plan = eng.plan("configure", scope, CID, desired=_desired())
    activation = _activation_ids(plan)
    op_id = {"first-activation": activation[0], "last-activation": activation[-1]}.get(where, where)
    run_id = _halt_at(eng, scope, plan, op_id)
    _seed_control_plane_leftover(eng, "specialist")
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    seat = _sv_state(eng, "specialist")
    assert seat["units"][identity.service] == {"loaded": True, "enabled": True, "active": True}
    assert seat["sessions"][identity.tmux_session] is True  # bespoke, after the control plane was torn down
    assert seat["units"][S.RUNTIME_SCOPE_UNIT]["active"] is False
    assert seat["units"][S.CODEX_RECEIVER_UNIT]["active"] is False  # Recovery (l): no second poller
    assert seat["sessions"][identity.tmux_session + "-sibling"] is True  # Recovery (j): exact target
    for unit, session in _companions(identity):
        assert seat["units"][unit]["active"] is True and seat["sessions"][session] is True
    events, _record = _transcript(eng, run_id)
    assert not _pages(events) and "escalations" not in (_record.get("rollback_incomplete") or {})  # Recovery (n): success never pages
    rolled = [e["op_id"] for e in events if e["event"] == "op.rolled_back"]
    assert "sv-retire" in rolled
    if where in ("last-activation", "sv-companion-start-0", "sv-commit"):
        # newest first: activation's own inverses ran BEFORE the composite undo
        undone = [i for i in activation if i in rolled]
        assert undone and all(rolled.index(i) < rolled.index("sv-retire") for i in undone)


def _halted_with_leftover(tmp_path, persona, stuck=()):
    """A cutover HALTED at its commit (postcondition), then the control-plane leftover
    seeded, with ``stuck`` pieces that refuse their stop AT ATTEMPT TIME."""

    seat = _seat(persona)
    seat["facts"]["control_plane_session"] = False  # the commit's postcondition fails: HALTED
    repo, eng, scope = _engine(tmp_path, persona, seat=seat)
    plan = eng.plan("configure", scope, CID, desired=_desired())
    with pytest.raises(EngineRefusal) as info:
        _apply(eng, scope, plan)
    assert info.value.code == "halted"
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    _seed_control_plane_leftover(eng, persona, stuck=stuck)
    return repo, eng, scope, run_id


def _retire_check(eng, run_id):
    """can_rollback on sv-retire's real write-ahead record, BEFORE any attempt."""

    store, run_tuple = eng._find_run(run_id)
    with store.open(run_tuple) as handle:
        raw = next(o for o in handle.read_plan()["node"]["operations"] if o["operation_id"] == "sv-retire")
        from tools.germinate.engine.operation import operation_from_json_obj

        mutation = eng._mutation_for_rollback(handle.wal(), operation_from_json_obj(raw))
    return eng.backend.can_rollback(mutation)


def _clear_stuck(eng, persona):
    handler = eng.backend.handler("harness.supervisor-unit")
    state = handler.dump_state()
    state["seats"][_identity(persona).account].pop("teardown_stuck", None)
    handler.load_state(state)
    eng.backend.save()


def test_r2_1_p2_attempt_time_stop_escalates_once_and_recovers(tmp_path):
    """Review probe P2 on the REAL attempt path: can_rollback predicts "owned", the scope
    stop then fails when tried, after the session was already killed. Recovery D15 (n)."""

    identity = _identity("recovery")
    repo, eng, scope, run_id = _halted_with_leftover(tmp_path, "recovery", stuck=("scope",))
    check = _retire_check(eng, run_id)
    assert (check.ok, check.classification) == (True, "owned")  # nothing predicted the stop

    with pytest.raises(EngineRefusal) as info:
        eng.rollback(run_id)
    assert info.value.code == "rollback-incomplete"
    seat = _sv_state(eng, "recovery")
    assert seat["units"][identity.service]["active"] is False  # bespoke NOT started
    assert identity.tmux_session not in seat["sessions"]  # the session WAS killed ...
    assert seat["units"][S.RUNTIME_SCOPE_UNIT]["active"] is True  # ... and the scope stayed up: no supervisor
    events, record = _transcript(eng, run_id)
    [marker] = record["rollback_incomplete"]["escalations"]  # the durable marker
    assert (marker["op_id"], marker["subject"], marker["recipient"]) == ("sv-retire", identity.account, "recovery")
    assert marker["reason"].startswith(f"control plane not torn down: {S.RUNTIME_SCOPE_UNIT}")
    assert f"session ={identity.tmux_session}" in marker["reason"]  # what was already torn down
    [page] = _pages(events)  # exactly one page
    assert {k: page[k] for k in ("op_id", "recipient", "subject", "reason", "page_run_id", "delivery")} == {
        **marker, "page_run_id": run_id, "delivery": "deferred-dark"}
    [row] = [r for r in eng.status() if r["component"] == CID]
    assert row["runs"][f"{scope}/configure"]["escalations"] == [marker]  # visible to the recovery reader
    # the partial teardown is durable: a fresh process over the same simulated host sees it
    restarted = H.engine(tmp_path, repo)
    durable = restarted.backend.handler("harness.supervisor-unit").dump_state()["seats"][identity.account]
    assert identity.tmux_session not in durable["sessions"]

    # the documented recovery is NOT the restore plan (the run holds the claim, D-Y) ...
    with pytest.raises(EngineRefusal) as info:
        eng.plan("configure", scope, CID, desired=_desired("bespoke"))
    assert info.value.code == "claim-held"
    # ... a retry while still blocked stops again and pages NO second time
    with pytest.raises(EngineRefusal) as info:
        eng.rollback(run_id)
    assert info.value.code == "rollback-incomplete"
    events, record = _transcript(eng, run_id)
    assert len(_pages(events)) == 1
    assert record["rollback_incomplete"]["escalations"][0]["recipient"] == "recovery"
    # ... clear the blocker, re-run rollback(run_id): ROLLED_BACK, bespoke reinstated, still one page
    _clear_stuck(eng, "recovery")
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    seat = _sv_state(eng, "recovery")
    assert seat["units"][identity.service] == {"loaded": True, "enabled": True, "active": True}
    assert seat["units"][S.RUNTIME_SCOPE_UNIT]["active"] is False
    assert seat["sessions"][identity.tmux_session] is True
    events, _record = _transcript(eng, run_id)
    assert len(_pages(events)) == 1
    [row] = [r for r in eng.status() if r["component"] == CID]
    assert "escalations" not in row["runs"][f"{scope}/configure"]
    assert coordinator.read_claim(eng.backend.gates_root, "sim", CID) is None


@pytest.mark.parametrize(
    "stuck, escalates",
    [("session", False), ("receiver", True), ("scope", True)],
    ids=("session-first-nothing-torn", "receiver-after-session", "scope-after-session-and-receiver"),
)
def test_r2_1_failure_at_each_teardown_substep_on_attempt(tmp_path, stuck, escalates):
    """Review R2-1 (c): an attempt-time failure escalates exactly when something was
    already torn down; a failure before any teardown mutation is a plain incomplete."""

    identity = _identity("recovery")
    _repo, eng, scope, run_id = _halted_with_leftover(tmp_path, "recovery", stuck=(stuck,))
    assert _retire_check(eng, run_id).classification == "owned"
    with pytest.raises(EngineRefusal) as info:
        eng.rollback(run_id)
    assert info.value.code == "rollback-incomplete"
    seat = _sv_state(eng, "recovery")
    assert seat["units"][identity.service]["active"] is False  # bespoke never started over the control plane
    events, record = _transcript(eng, run_id)
    assert "sv-retire" in record["rollback_incomplete"]["outstanding_inverses"]
    if escalates:
        assert [m["op_id"] for m in record["rollback_incomplete"]["escalations"]] == ["sv-retire"]
        assert len(_pages(events)) == 1
    else:
        assert "escalations" not in record["rollback_incomplete"] and _pages(events) == []
        assert seat["sessions"][identity.tmux_session] is True  # nothing torn: the control plane still serves
        assert seat["units"][S.RUNTIME_SCOPE_UNIT]["active"] is True
    _clear_stuck(eng, "recovery")
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    events, _record = _transcript(eng, run_id)
    assert len(_pages(events)) == (1 if escalates else 0)


def test_r2_1_a_soft_slice_failure_never_blocks_or_escalates(tmp_path):
    _repo, eng, scope, run_id = _halted_with_leftover(tmp_path, "recovery", stuck=("slice",))
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    events, _record = _transcript(eng, run_id)
    assert _pages(events) == []
    assert _sv_state(eng, "recovery")["units"][S.RUNTIME_CONSUME_SLICE]["active"] is True  # recorded, not asserted


def test_r2_2_a_non_object_transcript_line_does_not_break_paging(tmp_path):
    _repo, eng, scope, run_id = _halted_with_leftover(tmp_path, "recovery", stuck=("scope",))
    store, run_tuple = eng._find_run(run_id)
    with store.open(run_tuple) as handle:
        handle.append_transcript(b"[1]\n")
        handle.append_transcript(b"\"a string\"\n")
    with pytest.raises(EngineRefusal) as info:
        eng.rollback(run_id)
    assert info.value.code == "rollback-incomplete"
    events, record = _transcript(eng, run_id)
    assert len(_pages(events)) == 1  # the new page was still emitted
    assert record["rollback_incomplete"]["escalations"]


def test_r3_1_escalation_marker_survives_a_drift_refused_retry(tmp_path):
    """Review R3-1: escalate, an outside actor then re-enables bespoke (without starting
    it), the retry is refused as drifted, and the marker must NOT disappear."""

    identity = _identity("recovery")
    _repo, eng, scope, run_id = _halted_with_leftover(tmp_path, "recovery", stuck=("scope",))
    with pytest.raises(EngineRefusal):
        eng.rollback(run_id)
    _events, record = _transcript(eng, run_id)
    [marker] = record["rollback_incomplete"]["escalations"]
    handler = eng.backend.handler("harness.supervisor-unit")
    state = handler.dump_state()
    state["seats"][identity.account]["units"][identity.service] = {"loaded": True, "enabled": True, "active": False}
    handler.load_state(state)
    eng.backend.save()
    with pytest.raises(EngineRefusal) as info:
        eng.rollback(run_id)
    assert info.value.code == "rollback-incomplete"
    events, record = _transcript(eng, run_id)
    assert [e for e in events if e["event"] == "op.rollback.failed" and e["op_id"] == "sv-retire"][-1]["reason"].endswith(
        f"{identity.service} moved since the retire"
    )  # this attempt failed as drifted, not as an escalation
    assert record["rollback_incomplete"]["escalations"] == [marker]  # carried forward
    [row] = [r for r in eng.status() if r["component"] == CID]
    assert row["runs"][f"{scope}/configure"]["escalations"] == [marker]
    assert len(_pages(events)) == 1


def test_r3_2_a_failed_save_never_masks_the_escalation(tmp_path, monkeypatch):
    """Review R3-2: an OSError from the simulated host's save during an escalating undo
    must not replace the EscalatingRollbackStop: the marker and the page still land."""

    _repo, eng, scope, run_id = _halted_with_leftover(tmp_path, "recovery", stuck=("scope",))

    def failing_save():
        raise OSError("disk full (injected)")

    monkeypatch.setattr(eng.backend, "save", failing_save)
    with pytest.raises(EngineRefusal) as info:
        eng.rollback(run_id)
    assert info.value.code == "rollback-incomplete"
    events, record = _transcript(eng, run_id)
    assert [m["op_id"] for m in record["rollback_incomplete"]["escalations"]] == ["sv-retire"]
    assert len(_pages(events)) == 1
    failed = [e for e in events if e["event"] == "op.rollback.failed" and e["op_id"] == "sv-retire"]
    assert failed[-1]["reason"].startswith("EscalatingRollbackStop: control plane not torn down")


def test_r3_2_backend_reraises_the_original_and_propagates_a_lone_save_failure(tmp_path, monkeypatch):
    from tools.germinate.engine.escalation import EscalatingRollbackStop

    _repo, eng, _scope = _engine(tmp_path, "recovery")
    backend = eng.backend
    stop = EscalatingRollbackStop("stopped", recipient="recovery", subject="claude-fallback")

    class Undo:
        kind = "harness.supervisor-unit"
        operation_id = "x"

    def boom(_mutation):
        raise stop

    monkeypatch.setattr(backend.handler("harness.supervisor-unit"), "rollback_mutation", boom)
    monkeypatch.setattr(backend, "save", lambda: (_ for _ in ()).throw(OSError("injected")))
    with pytest.raises(EscalatingRollbackStop) as info:
        backend.rollback_mutation(Undo())
    assert info.value is stop and any("injected" in note for note in info.value.__notes__)
    # a successful undo whose save fails still raises the save failure, as before
    monkeypatch.setattr(backend.handler("harness.supervisor-unit"), "rollback_mutation", lambda _m: None)
    with pytest.raises(OSError, match="injected"):
        backend.rollback_mutation(Undo())


def test_r2_1_composite_undo_unit_tier_plain_vs_escalating():
    from tools.germinate.engine.escalation import EscalatingRollbackStop

    identity = _identity("recovery")
    for stuck, kind in (("session", PI.OperationFailed), ("scope", EscalatingRollbackStop)):
        sim = _sim_with("recovery")
        mutation, _ = sim.execute(_inst(_by_id(_cutover("recovery"))["sv-retire"]))
        undo = json.loads(mutation.before["undo"])
        assert (undo["session"], undo["receiver_unit"], undo["scope_unit"], undo["recipient"]) == (
            identity.tmux_session, S.CODEX_RECEIVER_UNIT, S.RUNTIME_SCOPE_UNIT, "recovery")
        state = sim.dump_state()
        seat = state["seats"][identity.account]
        seat["sessions"][identity.tmux_session] = True
        seat["units"][S.RUNTIME_SCOPE_UNIT] = {"loaded": True, "enabled": False, "active": True}
        seat["teardown_stuck"] = [stuck]
        sim.load_state(state)
        assert sim.can_rollback(mutation).classification == "owned"
        with pytest.raises(kind, match="control plane not torn down") as info:
            sim.rollback_mutation(mutation)
        if kind is EscalatingRollbackStop:
            assert (info.value.recipient, info.value.subject) == ("recovery", identity.account)
        else:
            assert not isinstance(info.value, EscalatingRollbackStop)
        assert sim.dump_state()["seats"][identity.account]["units"][identity.service]["active"] is False


def test_escalating_rollback_stop_requires_its_fields():
    from tools.germinate.engine.escalation import EscalatingRollbackStop

    stop = EscalatingRollbackStop("r", recipient="recovery", subject="acct")
    assert isinstance(stop, PI.OperationFailed) and stop.evidence == {"recipient": "recovery", "subject": "acct"}
    for bad in ({"recipient": "", "subject": "a"}, {"recipient": "w", "subject": ""}):
        with pytest.raises(ValueError):
            EscalatingRollbackStop("r", **bad)


def test_r2_3_engine_names_no_persona_or_component_concept():
    """ORG-080 A-4: component concepts stay in the component."""

    for path in (REPO_ROOT / "tools/germinate/engine/executor.py", REPO_ROOT / "tools/germinate/engine/escalation.py"):
        text = path.read_text(encoding="utf-8").lower()
        for word in (r"\brecovery\b", r"dark_seat", r"dark-seat", r"\bbespoke\b", r"\bsupervisor\b"):
            assert not re.search(word, text), (path.name, word)


def test_f1_retire_inverse_targets_are_pinned_exact_and_user_manager():
    retire = _by_id(_cutover("specialist"))["sv-retire"]
    d = retire["desired"]
    assert (d["receiver_unit"], d["scope_unit"], d["slice_unit"]) == (S.CODEX_RECEIVER_UNIT, S.RUNTIME_SCOPE_UNIT, S.RUNTIME_CONSUME_SLICE)
    assert d["undo"] == "teardown-then-reinstate" and d["systemd_manager"] == "user"
    assert d["escalation_recipient"] == S.ESCALATION_RECIPIENT == "recovery"  # a component fact, recorded in the plan
    assert all(t.startswith("=") for t in d["tmux_targets"].split(","))
    pins = _expected_pins(_identity("specialist"))
    tampered = json.loads(json.dumps(_cutover("specialist")))
    _by_id(tampered)["sv-retire"]["desired"]["receiver_unit"] = "someone-else.service"
    with pytest.raises(HM.PlanRefused, match="outside the resolver-derived pins"):
        HM._assert_pinned(tampered, pins)


# --- review F3 / F4 -------------------------------------------------------------------------


def test_f3_symlink_and_gates_are_checked_before_re_entry():
    identity = _identity("recovery")
    snapshot = _by_id(_cutover("recovery"))["sv-snapshot-0"]
    seat = _seat("recovery")
    link = {"kind": "symlink", "sha256": "x"}
    seat["files"][snapshot["target"]] = link
    seat["files"][snapshot["desired"]["backup_path"]] = dict(link)  # already "at its result"
    assert "symlink" in _sim_with("recovery", seat).preflight(_inst(snapshot)).detail
    reinstate = _by_id(_restore("recovery"))["sv-reinstate"]
    seat = _post_cutover_seat("recovery")
    seat["units"][identity.service] = {"loaded": True, "enabled": True, "active": True}  # already reinstated
    assert seat["units"][S.RUNTIME_SCOPE_UNIT]["active"] is True
    assert "gate:" in _sim_with("recovery", seat).preflight(_inst(reinstate)).detail


def test_f4_runtime_scope_is_hard_in_teardown_and_reinstate():
    by = _by_id(_restore("recovery"))
    teardown = by["sv-teardown"]
    assert teardown["desired"]["soft"] == "slice_unit"
    checks = S.decode(teardown["desired"]["postconditions"])
    assert {"check": "unit", "params": {"unit": S.RUNTIME_SCOPE_UNIT, "active": "false"}} in checks
    assert S.RUNTIME_SCOPE_UNIT in by["sv-reinstate"]["precondition"]["units_inactive"]


# --- kind classification and dark pins ------------------------------------------------------


def test_real_supervisor_kinds_are_one_isolated_component_local_block():
    kinds = read_op_kinds(CID, HANDLERS)
    supervisor = {k: v for k, v in kinds.items() if v[0] == "harness.supervisor"}
    assert supervisor == {k: ("harness.supervisor", "seat") for k in SUPERVISOR_KINDS}
    order = list(kinds)
    first = order.index(SUPERVISOR_KINDS[0])
    assert order[first:first + len(SUPERVISOR_KINDS)] == list(SUPERVISOR_KINDS)  # contiguous
    assert "harness.supervisor" not in RESERVED_CLASSES
    registry = load_repo_op_registry(REPO_ROOT)
    for kind in SUPERVISOR_KINDS:
        assert component_kind_errors(kind, "harness.supervisor", "seat") == []
        entry = registry.get(kind)
        assert entry.privilege == "seat" and not entry.broker_admitted and not entry.substrate
    assert HMH.HarnessSupervisorSim.kinds == frozenset(SUPERVISOR_KINDS)


def test_real_contract_declares_the_gate_schema_and_privilege():
    registry = load_repo_op_registry(REPO_ROOT)
    contract = load_contract(COMPONENTS / "harness-manager.yaml", registry)
    gate = contract.gates["supervisor-cutover"]
    assert gate.approval_class == "independent-agent" and gate.irreversible is True
    assert contract.desired_schema["supervisor"].enum == ("consume", "bespoke")
    assert set(SUPERVISOR_KINDS) <= contract.privilege["seat"]
    assert "root" not in str(sorted(contract.privilege))  # no root list: if needed, reclassified (Recovery D15)


def test_dark_pins_live_actuation_false_and_no_flag():
    from tools import host_broker_install
    from tools.germinate.components import pantheon_config_git
    from tools.germinate.host_substrate import ports

    assert ports.LIVE_ACTUATION_ENABLED is False
    assert host_broker_install.LIVE_ACTUATION_ENABLED is False
    assert pantheon_config_git.LIVE_ACTUATION_ENABLED is False
    for path in (PLANNER, HANDLERS, HELPER):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {t.id for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign))
                 for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Name)}
        assert not any(name.startswith("LIVE_ACTUATION") for name in names), path


@pytest.mark.parametrize("path", (PLANNER, HANDLERS, HELPER), ids=lambda p: p.name)
def test_no_live_surface_is_imported(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    banned = ("tools.runtime_controller", "tools.persona_installer_apply", "subprocess", "tools.germinate.host_substrate",
              "tools.user_systemd", "tools.t2611_recovery_persona_apply", "os")
    assert not [n for n in names if any(n == b or n.startswith(b + ".") for b in banned)]


def test_untested_live_is_in_every_docstring():
    for path in (PLANNER, HELPER):
        assert "UNTESTED-LIVE" in ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
    assert "UNTESTED-LIVE" in HMH.HarnessSupervisorSim.__doc__
    for path in (PLANNER, HELPER):
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
        assert "first live fire is supervised" in doc and "reclassified" in doc


# --- T-6047: companion pre-state parity with T-5956 ---------------------------------

_UNIT = "pantheon-acp-bridge@specialist.service"


def _snap_name(persona="specialist", stamp=STAMP):
    return S.companion_snapshot_name(persona, stamp)


def _restore_with_record(record, persona="specialist"):
    seat = _post_cutover_seat(persona)
    if record is not None:
        seat["companion_prestate"] = {_snap_name(persona): record}
    return _restore(persona, seat)


def _start_ids(ops):
    return [op["operation_id"] for op in ops if op["operation_id"].startswith("sv-companion-start-")]


def _unit_checks(ops):
    checks = json.loads(_by_id(ops)["sv-commit"]["desired"]["postconditions"])
    return [c for c in checks if c["check"] == "unit" and c["params"].get("unit") == _UNIT]


def test_t6047_recorded_inactive_companion_is_left_stopped_and_unverified():
    ops = _restore_with_record({_UNIT: "inactive"})
    assert _start_ids(ops) == [] and _unit_checks(ops) == []
    assert [op["operation_id"] for op in ops] == [i for i in _RESTORE_IDS["specialist"] if not i.startswith("sv-companion-start-")]


def test_t6047_recorded_active_companion_is_restored_and_verified():
    ops = _restore_with_record({_UNIT: "active"})
    assert _start_ids(ops) == ["sv-companion-start-0"] and len(_unit_checks(ops)) == 1


def test_t6047_no_record_restores_all_byte_for_byte():
    """A missing record (a pre-T-5956 cutover), an unrecorded unit and an unknown state all restore."""

    base = _restore_with_record(None)
    assert [op["operation_id"] for op in base] == _RESTORE_IDS["specialist"]
    for record in ({_UNIT: "active"}, {_UNIT: "unknown"}, {"other.service": "inactive"}, {}):
        assert _restore_with_record(record) == base  # all-active behaviour: ops and checks byte-equal


def test_t6047_mixed_companions_restore_only_the_recorded_active(monkeypatch):
    pairs = [(_UNIT, "acp-bridge-specialist"), ("second.service", "second-sess")]
    monkeypatch.setattr(HM, "_companions", lambda encoded: pairs)
    ops = _restore_with_record({_UNIT: "inactive", "second.service": "active"})
    assert _start_ids(ops) == ["sv-companion-start-1"]
    checks = json.loads(_by_id(ops)["sv-commit"]["desired"]["postconditions"])
    assert [c["params"]["unit"] for c in checks if c["check"] == "unit" and c["params"].get("active") == "true"
            and c["params"]["unit"] in dict(pairs)] == ["second.service"]


def test_t6047_cutover_stop_op_carve_out_is_exactly_the_snapshot_key():
    # intended change (T-6047): ONLY the stop ops gain desired.companion_snapshot; the rest is byte-equal
    ops = _cutover("specialist")
    stop = _by_id(ops)["sv-companion-stop-0"]
    assert stop["desired"]["companion_snapshot"] == _snap_name()
    stripped = json.loads(json.dumps(stop))
    del stripped["desired"]["companion_snapshot"]
    identity = _identity("specialist")
    legacy = HM._companion_op("sv-companion-stop-0", identity.account, identity, _UNIT, "acp-bridge-specialist", "stop", stop["provenance"])
    assert stripped == json.loads(json.dumps(legacy))
    assert all("companion_snapshot" not in op["desired"] for op in ops if op["operation_id"] != "sv-companion-stop-0")


def test_t6047_sim_cell_key_and_format_match_the_script_snapshot():
    # companion_snapshot_write's file: persona-cutover-<persona>-<stamp>.companions, "unit=ActiveState" lines
    script_path = "/home/x/.local/state/pantheon/persona-cutover-specialist-%s.companions" % STAMP
    assert script_path.rsplit("/", 1)[1] == _snap_name("specialist", STAMP)
    parsed = S.parse_companion_snapshot(f"{_UNIT}=inactive\nother.service=active\nempty.service=\n")
    assert parsed == {_UNIT: "inactive", "other.service": "active", "empty.service": "unknown"}
    assert not S.companion_should_restore(parsed, _UNIT)
    assert S.companion_should_restore(parsed, "other.service") and S.companion_should_restore(parsed, "empty.service")
    assert S.companion_should_restore(parsed, "unrecorded.service") and S.companion_should_restore(None, _UNIT)
    # the engine's restore planner reads that parsed record from the cell, and nothing else
    assert _start_ids(_restore_with_record(parsed)) == []
    # the shell lib documents the same file shape (a drift guard on the script side)
    lib = (REPO_ROOT / "tools/t2590_cutover_verify_lib.sh").read_text()
    assert "persona-cutover-%s-%s.companions" in lib and "%s=%s" in lib


def _stopped_seat(state_active):
    seat = _seat("specialist")
    seat["units"][_UNIT]["active"] = state_active
    return seat


def test_t6047_engine_stop_records_pre_stop_state_first_record_wins(tmp_path):
    identity = _identity("specialist")
    # companion inactive before the cutover: recorded inactive, stop is a no-op, cutover succeeds
    seat = _stopped_seat(False)
    seat["sessions"].pop("acp-bridge-specialist", None)
    _repo, eng, scope = _engine(tmp_path, "specialist", seat=seat)
    plan = eng.plan("configure", scope, CID, desired=_desired())
    [outcome] = _apply(eng, scope, plan)
    assert outcome.state == "SATISFIED"
    cell = _sv_state(eng, "specialist")["companion_prestate"][_snap_name()]
    assert cell == {_UNIT: "inactive"}


def test_t6047_first_record_wins_on_a_rerun(tmp_path):
    identity = _identity("specialist")
    _repo, eng, scope = _engine(tmp_path, "specialist")
    plan = eng.plan("configure", scope, CID, desired=_desired())
    _apply(eng, scope, plan)
    assert _sv_state(eng, "specialist")["companion_prestate"][_snap_name()] == {_UNIT: "active"}
    # a partial-cutover re-run sees the companion already stopped: the record must not flip
    handler = eng.backend.handler("harness.supervisor-unit")
    stop = [o for o in _cutover("specialist", seat=_seat("specialist")) if o["operation_id"] == "sv-companion-stop-0"][0]
    state = handler.dump_state()
    seat = state["seats"][identity.account]
    seat["units"][_UNIT]["active"] = False
    handler.load_state(state)
    parsed = parse_operation(stop, contract=load_contract(COMPONENTS / "harness-manager.yaml", load_repo_op_registry(REPO_ROOT)),
                             op_registry=load_repo_op_registry(REPO_ROOT))
    effects = handler._effects(parsed, handler.dump_state()["seats"][identity.account])
    assert effects[f"companion_prestate/{_snap_name()}"] == {_UNIT: "active"}


def test_t6047_engine_rollback_of_a_halted_run_leaves_an_inactive_companion_stopped(tmp_path):
    seat = _stopped_seat(False)
    seat["sessions"].pop("acp-bridge-specialist", None)
    seat["facts"]["telegram_pollers"] = 2  # the commit fails: the run halts after the stop op
    _repo, eng, scope = _engine(tmp_path, "specialist", seat=seat)
    before = _sv_state(eng, "specialist")
    plan = eng.plan("configure", scope, CID, desired=_desired())
    with pytest.raises(EngineRefusal):
        _apply(eng, scope, plan)
    run_id = coordinator.read_claim(eng.backend.gates_root, "sim", CID)["run_id"]
    assert eng.rollback(run_id).state == "ROLLED_BACK"
    after = _sv_state(eng, "specialist")
    assert after["units"][_UNIT]["active"] is False and "acp-bridge-specialist" not in after["sessions"]
    assert after == before
