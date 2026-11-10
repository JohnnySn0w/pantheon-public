"""T-6167 PR-4: the live-install orchestrator (live_run.py). DARK.

Same-process integration with fakes: ordering (host-substrate leg before the seat sub-run),
the exit-code classification of the sub-run, and C5 canary outcome parsing.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from tools._organ_lifecycle import CommandResult
from tools.germinate import live_run as LR
from tools.germinate.host_substrate import root_launcher as RL

DIGEST = "sha256:" + "a" * 64
OK = RL.LaunchResult(0, '{"result": []}\n', "", "u.service")


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(RL, "ROOT_LAUNCHER_LIVE", True)
    monkeypatch.setattr(RL, "LIVE_PROFILES", MappingProxyType({"seat-run": True, "c5-canary": True}))


def _run(root_leg, seat_launch, err=None):
    import io

    buf = err if err is not None else io.StringIO()
    return LR.run_install("alice", DIGEST, root_leg=root_leg, seat_launch=seat_launch, err=buf), buf.getvalue()


# --- ordering -------------------------------------------------------------------------


def test_install_orders_host_substrate_before_seat_subrun(live):
    order = []
    code, _ = _run(lambda a: order.append(("root", a)), lambda a, d: order.append(("seat", a, d)) or OK)
    assert code == 0
    assert order == [("root", "alice"), ("seat", "alice", DIGEST)]


def test_seat_subrun_not_started_when_root_leg_refuses(live):
    calls = []

    def root_leg(account):
        raise LR.LiveRunRefused("root-leg-refused", "host-substrate leg refused")

    code, err = _run(root_leg, lambda a, d: calls.append(a) or OK)
    assert code == 1 and calls == []
    assert "refused: root-leg-refused:" in err


def test_production_root_leg_is_a_typed_refusal(live):
    calls = []
    code, err = _run(LR.root_live_backend_unavailable, lambda a, d: calls.append(a) or OK)
    assert code == 1 and calls == []
    assert "refused: root-live-backend-unavailable:" in err


def test_pins_are_checked_before_the_root_leg(monkeypatch):
    calls = []
    code, err = _run(lambda a: calls.append("root"), lambda a, d: calls.append("seat") or OK)
    assert code == 1 and calls == []
    assert "live-actuation-disabled" in err


def test_launcher_refusal_from_the_subrun_is_typed_and_exit_1(live):
    def launch(a, d):
        raise RL.LauncherRefused("framework-path-refused", "bad tree")

    code, err = _run(lambda a: None, launch)
    assert code == 1 and "refused: framework-path-refused:" in err


# --- exit mapping ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "rc,stderr,code,exit_code",
    [
        (0, "", "ok", 0),
        (1, "refused: plan-digest-mismatch: x", "seat-run-refused", 1),
        (1, "", "seat-run-refused", 1),
        (1, "refused: dependency-not-installed: harness-manager depends on host-substrate", "seat-dependency-not-installed", 10),
        (1, "noise\nrefused: postboot-unmet: no marker", "seat-postboot-unmet", 11),
        (1, "refused: seat-pin-disabled: dark", "seat-pin-disabled", 12),
        (2, "", "seat-run-usage", 2),
        (218, "", "seat-run-unit-failed", 20),
        (226, "", "seat-run-unit-failed", 20),
        (255, "", "seat-run-unit-failed", 20),
        (7, "", "seat-run-failed", 21),
    ],
)
def test_exit_code_mapping(live, rc, stderr, code, exit_code):
    assert LR.classify_child(rc, stderr) == (code, exit_code)
    got, err = _run(lambda a: None, lambda a, d: RL.LaunchResult(rc, "", stderr, "u"))
    assert got == exit_code
    if exit_code:
        assert f"refused: {code}:" in err


def test_typed_refusal_exit_codes_are_distinct():
    pairs = {LR.classify_child(1, f"refused: {c}: x") for c in LR.CHILD_REFUSALS}
    pairs |= {LR.classify_child(r, "") for r in (1, 2, 218, 7)}
    exits = [e for _, e in pairs]
    assert len(set(exits)) == len(exits) == 7


def test_only_the_last_refusal_line_classifies(live):
    # the child's output is data: an earlier benign line cannot reclassify the final refusal
    text = "refused: postboot-unmet: first\nrefused: plan-digest-mismatch: last"
    assert LR.classify_child(1, text) == ("seat-run-refused", 1)
    # and a refusal line is only consulted for exit 1
    assert LR.classify_child(7, "refused: postboot-unmet: x") == ("seat-run-failed", 21)


@pytest.mark.parametrize(
    "exc",
    [OSError("no such file"), TimeoutError("slow"), subprocess.TimeoutExpired(["x"], 1), subprocess.CalledProcessError(1, "x")],
)
def test_runner_error_maps_to_launch_failed(live, exc):
    def boom(a, d):
        raise exc

    code, err = _run(lambda a: None, boom)
    assert code == 22 and "refused: seat-launch-failed:" in err


def test_a_programming_error_is_not_swallowed(live):
    def boom(a, d):
        raise ValueError("bug")

    with pytest.raises(ValueError):
        _run(lambda a: None, boom)


# --- canaries -------------------------------------------------------------------------


def _res(rc=0, out="", err=""):
    return RL.LaunchResult(rc, out, err, "u")


def test_c5_canary_outcome_parsing_red_green():
    assert LR.parse_canary("network", _res(out="CANARY_RESULT:DENIED:EPERM\n")).verdict == "green"
    assert LR.parse_canary("network", _res(out="CANARY_RESULT:ALLOWED:connected\n")).verdict == "red"
    assert LR.parse_canary("network", _res(out="")).verdict == "red"
    assert LR.parse_canary("network", _res(out="CANARY_RESULT:DENIED:a\nCANARY_RESULT:ALLOWED:b\n")).verdict == "red"
    assert LR.parse_canary("network", _res(out="CANARY_RESULT:UNEXPECTED_EXCEPTION:X\n")).verdict == "red"
    assert LR.parse_canary("network", _res(out="CANARY_RESULT:INDETERMINATE:x")).verdict == "red"
    assert LR.parse_canary("network", _res(rc=226, err="NAMESPACE", out="CANARY_RESULT:DENIED:x")).verdict == "red"
    # a DENIED prefix is required exactly: a mention inside another outcome is not green
    assert LR.parse_canary("network", _res(out="CANARY_RESULT:ALLOWED:DENIED:\n")).verdict == "red"


def test_c5_5_data_only_return_parsing():
    ops = '[{"id": "noop", "payload": {}}, {"id": "totally-unknown-op-id", "payload": {}}]'
    assert LR.parse_canary("data-only-return", _res(out=ops)).verdict == "green"
    assert LR.parse_canary("data-only-return", _res(out='[{"id": "noop"}]')).verdict == "red"
    assert LR.parse_canary("data-only-return", _res(out="not json")).verdict == "red"
    assert LR.parse_canary("data-only-return", _res(out='{"id": "noop"}')).verdict == "red"


class _CanaryRunner:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def run(self, argv):
        self.calls.append(tuple(argv))
        text = "\0".join(argv)
        for name, res in self.outcomes.items():
            if f"'--canary={name}'" in text or (name == "data-only-return" and "plan_install" in text):
                return CommandResult(*res)
        raise AssertionError("unscripted canary")


def _builder():
    from tools.germinate.host_substrate.tests.test_root_launcher import build, ident  # reuse the fakes

    def b(profile, seat, mode, **kw):
        return build(profile, seat, mode=mode, **kw)

    return b


def test_run_c5_canaries_runs_in_order_and_prints_argv_first(live):
    import io

    out = io.StringIO()
    ops = '[{"id": "noop"}, {"id": "totally-unknown-op-id"}]'
    runner = _CanaryRunner({n: (0, "CANARY_RESULT:DENIED:x\n", "") for n in RL.CANARY_MODES if n != "data-only-return"})
    runner.outcomes["data-only-return"] = (0, ops, "")
    results = LR.run_c5_canaries("alice", build=_builder(), runner=runner, gates_dir="/srv/g", out=out)
    assert [r.name for r in results] == list(RL.CANARY_MODES)
    assert all(r.verdict == "green" for r in results)
    assert out.getvalue().count("ARGV ") == 5
    assert len(runner.calls) == 5


def test_run_c5_canaries_red_on_allowed_and_skip_without_gates_dir(live):
    runner = _CanaryRunner({n: (0, "CANARY_RESULT:ALLOWED:oops\n", "") for n in RL.CANARY_MODES})
    results = LR.run_c5_canaries("alice", build=_builder(), runner=runner)
    verdicts = {r.name: r.verdict for r in results}
    assert verdicts["authority-holding"] == "skip"
    assert verdicts["network"] == "red" and verdicts["subprocess-creation"] == "red"


def test_run_c5_canaries_launcher_refusal_is_red_not_a_crash(monkeypatch):
    results = LR.run_c5_canaries("alice", build=_builder(), runner=_CanaryRunner({}), gates_dir="/srv/g")
    assert all(r.verdict in ("red", "skip") for r in results)
    assert any("live-actuation-disabled" in r.detail for r in results)


# --- pins and static shape ------------------------------------------------------------


def test_live_run_has_no_pin_of_its_own_and_no_default_actuation():
    tree = ast.parse(Path(LR.__file__).read_text())
    assigned = {t.id for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))
                for t in ([n.target] if isinstance(n, ast.AnnAssign) else n.targets) if isinstance(t, ast.Name)}
    assert "ROOT_LAUNCHER_LIVE" not in assigned and "LIVE_PROFILES" not in assigned


def test_cli_install_dark_touches_nothing(monkeypatch, capsys):
    monkeypatch.setattr(LR, "production_launcher", lambda fw: (_ for _ in ()).throw(AssertionError("I/O")))
    monkeypatch.setattr(LR, "production_framework_root", lambda: (_ for _ in ()).throw(AssertionError("I/O")))
    assert LR.cli_install("alice", DIGEST) == 1
    assert "live-actuation-disabled" in capsys.readouterr().err


def test_module_modes_refuse_dark_before_any_read(monkeypatch, capsys):
    monkeypatch.setattr(LR, "production_launcher", lambda fw: (_ for _ in ()).throw(AssertionError("I/O")))
    monkeypatch.setattr(LR, "production_framework_root", lambda: (_ for _ in ()).throw(AssertionError("I/O")))
    assert LR.main(["c5-canaries", "--seat", "alice"]) == 1
    assert LR.main(["selfcheck", "--seat", "alice"]) == 1
    assert "live-actuation-disabled" in capsys.readouterr().err


# --- R1: the production live-group seam -----------------------------------------------


def _seam(gids, names, pw_gid=2000):
    return LR.production_live_groups(
        getpwnam=lambda a: SimpleNamespace(pw_gid=pw_gid),
        getgrouplist=lambda a, g: list(gids),
        getgrgid=lambda g: SimpleNamespace(gr_name=names[g]) if g in names else (_ for _ in ()).throw(KeyError(g)),
    )


def test_production_live_groups_splits_primary_from_supplementary():
    seam = _seam([2000, 3001, 3002], {2000: "alice", 3001: "pmail", 3002: "docker"})
    assert seam("alice") == ("alice", frozenset({"pmail", "docker"}))


def test_production_live_groups_adds_the_primary_even_if_the_list_omits_it():
    assert _seam([3001], {2000: "alice", 3001: "pmail"})("alice") == ("alice", frozenset({"pmail"}))


def test_production_live_groups_unknown_gid_fails_closed_typed():
    with pytest.raises(RL.LauncherRefused) as info:
        _seam([2000, 3999], {2000: "alice"})("alice")
    assert info.value.code == "group-drift" and "3999" in info.value.message


def test_production_live_groups_unknown_primary_gid_fails_closed_typed():
    with pytest.raises(RL.LauncherRefused) as info:
        _seam([3001], {3001: "pmail"})("alice")
    assert info.value.code == "group-drift"


def test_live_group_refusal_through_run_install_is_typed_exit_1(live):
    def launch(a, d):
        _seam([2000, 3999], {2000: "alice"})(a)

    code, err = _run(lambda a: None, launch)
    assert code == 1 and "refused: group-drift:" in err


# --- R3: malformed instance.yaml is a typed refusal -----------------------------------


def test_production_launcher_malformed_identities_is_typed(monkeypatch, tmp_path):
    from tools.germinate.components import harness_manager_root as HMR

    def boom(doc):
        raise HMR.IdentityRefused("roles.x.identity.uid: not an int")

    monkeypatch.setattr(HMR, "load_instance", lambda p: {"roles": {}})
    monkeypatch.setattr(HMR, "identities", boom)
    with pytest.raises(LR.LiveRunRefused) as info:
        LR.production_launcher(tmp_path)
    assert info.value.code == "identity-refused" and info.value.exit_code == LR.EXIT_REFUSED


def test_malformed_identities_through_run_install_maps_to_exit_1(live, monkeypatch, tmp_path):
    from tools.germinate.components import harness_manager_root as HMR

    monkeypatch.setattr(HMR, "load_instance", lambda p: {"roles": {}})
    monkeypatch.setattr(HMR, "identities", lambda d: (_ for _ in ()).throw(HMR.IdentityRefused("bad")))

    def launch(a, d):
        LR.production_launcher(tmp_path)

    code, err = _run(lambda a: None, launch)
    assert code == 1 and "refused: identity-refused:" in err


def test_main_malformed_identities_is_typed_not_a_crash(live, monkeypatch, capsys):
    def boom(fw):
        raise LR.LiveRunRefused("identity-refused", "bad")

    monkeypatch.setattr(LR, "production_launcher", boom)
    monkeypatch.setattr(LR, "production_framework_root", lambda: Path("/nonexistent"))
    assert LR.main(["selfcheck", "--seat", "alice"]) == 1
    assert "refused: identity-refused:" in capsys.readouterr().err


# --- R4: cli_install's own pin guard --------------------------------------------------


def test_cli_install_dark_refuses_before_run_install(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(LR, "run_install", lambda *a, **k: called.append(1) or 0)
    assert LR.cli_install("alice", DIGEST) == 1
    assert called == []
    assert "live-actuation-disabled" in capsys.readouterr().err


def test_cli_install_live_reaches_run_install(live, monkeypatch):
    called = []
    monkeypatch.setattr(LR, "run_install", lambda *a, **k: called.append(a) or 0)
    assert LR.cli_install("alice", DIGEST) == 0
    assert called == [("alice", DIGEST)]
