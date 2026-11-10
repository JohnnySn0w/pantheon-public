"""T-6167 PR-4: `apply install --seat ACCOUNT --live` at the engine CLI. DARK.

The CLI's before-any-I/O guarantee covers the launcher's global pin and LIVE_PROFILES: with
either off, `--live` refuses with the typed `live-actuation-disabled` and exit 1 before an
Engine, an instance.yaml read or a runner exists. Option combinations are classified (usage
error vs accepted) before the seam is reached.
"""

from __future__ import annotations

import argparse
import json
from types import MappingProxyType

import pytest

from tools.germinate.cli import main
from tools.germinate.engine import cli as engine_cli
from tools.germinate.engine.backend import SimulatedHostBackend
from tools.germinate.engine.executor import Engine
from tools.germinate.engine.tests import helpers as H
from tools.germinate.host_substrate import root_launcher as RL

DIGEST = "sha256:" + "a" * 64
LIVE = ["apply", "install", "--seat", "alice", "--plan-digest", DIGEST, "--live"]


@pytest.fixture
def no_engine(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("Engine.from_checkout reached")

    monkeypatch.setattr(Engine, "from_checkout", boom)
    monkeypatch.setattr(engine_cli, "_engine", boom)


def _parser(live_install=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    engine_cli.register(sub, live_install=live_install)
    return parser


def test_cli_live_pin_false_refuses_before_engine_built(no_engine, capsys):
    assert main(LIVE) == 1
    err = capsys.readouterr().err
    assert "refused: live-actuation-disabled:" in err


@pytest.mark.parametrize("glob,prof", [(False, True), (True, False), (False, False)])
def test_cli_each_pin_false_refuses(no_engine, monkeypatch, capsys, glob, prof):
    monkeypatch.setattr(RL, "ROOT_LAUNCHER_LIVE", glob)
    monkeypatch.setattr(RL, "LIVE_PROFILES", MappingProxyType({"seat-run": prof, "c5-canary": prof}))
    assert main(LIVE) == 1
    assert "live-actuation-disabled" in capsys.readouterr().err


def test_cli_live_and_simulated_host_root_refused(no_engine, tmp_path, capsys):
    assert main([*LIVE, "--simulated-host-root", str(tmp_path / "sim")]) == 2
    assert "mutually exclusive" in capsys.readouterr().err


@pytest.mark.parametrize("leg", ["configure", "update", "remove", "repair"])
def test_live_refused_for_other_legs(no_engine, leg, capsys):
    assert main(["apply", leg, "--seat", "alice", "--plan-digest", DIGEST, "--live"]) == 2
    assert "only for `apply install --seat" in capsys.readouterr().err


def test_live_refused_for_system_and_all_scopes(no_engine, capsys):
    assert main(["apply", "install", "--system", "comp-a", "--plan-digest", DIGEST, "--live"]) == 2
    assert main(["apply", "install", "--all", "--plan-digest", DIGEST, "--live"]) == 2


@pytest.mark.parametrize(
    "verb",
    [
        ["plan", "install", "--seat", "alice", "--live"],
        ["verify", "--seat", "alice", "--live"],
        ["rollback", "--run", "r1", "--live"],
        ["status", "--live"],
    ],
)
def test_live_is_not_a_flag_of_other_verbs(no_engine, verb):
    with pytest.raises(SystemExit) as info:
        main(verb)
    assert info.value.code == 2


@pytest.mark.parametrize(
    "extra",
    [
        ["--set", "k=v"], ["--from-rev", "a", "--to-rev", "b"], ["--accept-gates"], ["--grant-file", "/x"],
        ["--preflight-receipt", "/x"], ["--repo-root", "/x"], ["--registry", "/x"], ["--instance", "prod"],
        ["--context", "daemon"], ["--invoking-account", "alice"],
    ],
)
def test_live_unsupported_options_are_usage_errors(no_engine, extra, capsys):
    assert main([*LIVE, *extra]) == 2
    assert "cannot be combined with --live" in capsys.readouterr().err


def test_live_known_good_defaults_reach_the_seam(no_engine):
    seen = []
    parser = _parser(lambda a: seen.append(a) or 7)
    args = parser.parse_args(LIVE)
    assert args.func(args) == 7
    assert seen and seen[0].seat == "alice" and seen[0].plan_digest == DIGEST
    # benign: the defaults that equal the unsupported-option table values are accepted
    args = parser.parse_args([*LIVE, "--instance", "sim", "--context", "operator"])
    assert args.func(args) == 7


def test_live_without_a_wired_seam_is_a_typed_refusal(no_engine, capsys):
    parser = _parser(None)
    args = parser.parse_args(LIVE)
    assert args.func(args) == 1
    assert "refused: live-not-wired" in capsys.readouterr().err


def test_apply_without_live_still_requires_the_simulated_root(no_engine, capsys):
    assert main(["apply", "install", "--seat", "alice", "--plan-digest", DIGEST]) == 2
    assert "--simulated-host-root is required unless --live" in capsys.readouterr().err


def test_other_verbs_keep_the_simulated_root_required():
    for verb in (["plan", "install", "--all"], ["verify"], ["status"], ["rollback", "--run", "r"]):
        with pytest.raises(SystemExit) as info:
            main(verb)
        assert info.value.code == 2


def test_cli_refusal_maps_exit_code(monkeypatch, capsys):
    """Pins on: the production root_leg is the typed root-live-backend-unavailable, exit 1,
    and the seat sub-run is never started."""

    from tools.germinate import live_run

    monkeypatch.setattr(RL, "ROOT_LAUNCHER_LIVE", True)
    monkeypatch.setattr(RL, "LIVE_PROFILES", MappingProxyType({"seat-run": True, "c5-canary": True}))
    monkeypatch.setattr(RL, "launch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sub-run started")))
    monkeypatch.setattr(live_run, "production_launcher", lambda fw: (_ for _ in ()).throw(AssertionError("sub-run started")))
    assert main(LIVE) == 1
    assert "refused: root-live-backend-unavailable:" in capsys.readouterr().err


def test_simulated_mode_unchanged(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    ops = {"install": [H.dir_op("op-1", "/srv/c", owner="alice")]}
    _p, digest = H.write_contract(repo, "comp-a", ops=ops)
    H.make_checkout(repo, [H.row("comp-a", digest)])
    sim_root = tmp_path / "sim"
    SimulatedHostBackend(sim_root).simulate_postboot()
    common = ["--repo-root", str(repo), "--simulated-host-root", str(sim_root)]
    assert main(["plan", "install", "--system", "comp-a", *common]) == 0
    plan = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")][-1]["result"]["plan_digest"]
    assert main(["apply", "install", "--system", "comp-a", "--plan-digest", plan, *common]) == 0
    out = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")][-1]["result"]
    assert out[0]["state"] == "SATISFIED"
