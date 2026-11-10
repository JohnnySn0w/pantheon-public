from __future__ import annotations

import json

import pytest

from tools.germinate.cli import main
from tools.germinate.engine.backend import SimulatedHostBackend
from tools.germinate.engine.tests import helpers as H

# owner alice: test_context_flag_... invokes as account alice, and C6 requires a seat op's owner to match it
OPS = {"install": [H.dir_op("op-1", "/srv/c", owner="alice"), H.file_op("op-2", "/srv/c/a.conf", "hi\n", owner="alice")]}


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _p, digest = H.write_contract(repo, "comp-a", ops=OPS)
    H.make_checkout(repo, [H.row("comp-a", digest)])
    sim_root = tmp_path / "sim"
    SimulatedHostBackend(sim_root).simulate_postboot()
    return ["--repo-root", str(repo), "--simulated-host-root", str(sim_root)], sim_root


def _result(out: str):
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return [line["result"] for line in lines if "result" in line][-1]


def test_plan_apply_status_verify_round_trip(setup, capsys):
    common, sim_root = setup
    assert main(["plan", "install", "--system", "comp-a", *common]) == 0
    digest = _result(capsys.readouterr().out)["plan_digest"]
    assert main(["apply", "install", "--system", "comp-a", "--plan-digest", digest, *common]) == 0
    [outcome] = _result(capsys.readouterr().out)
    assert outcome["state"] == "SATISFIED"
    assert main(["status", *common]) == 0
    assert _result(capsys.readouterr().out)[0]["runs"]["system/install"]["run_id"] == outcome["run_id"]
    assert main(["verify", "--system", "comp-a", *common]) == 0
    SimulatedHostBackend(sim_root).seed_file("/srv/c/a.conf", "drift\n")
    capsys.readouterr()
    assert main(["verify", "--system", "comp-a", *common]) == 1


def test_apply_with_a_stale_digest_is_refused(setup, capsys):
    common, _ = setup
    assert main(["apply", "install", "--all", "--plan-digest", "sha256:" + "0" * 64, *common]) == 1
    assert "plan-digest-mismatch" in capsys.readouterr().err


def test_rollback_via_cli(setup, capsys):
    common, sim_root = setup
    main(["plan", "install", "--system", "comp-a", *common])
    digest = _result(capsys.readouterr().out)["plan_digest"]
    backend = SimulatedHostBackend(sim_root)
    # inject the failure through the persisted-state-free path: a conflicting unmanaged file
    backend.seed_file("/srv/c/a.conf", "someone else's file\n")
    assert main(["apply", "install", "--system", "comp-a", "--plan-digest", digest, *common]) == 1
    err = capsys.readouterr().err
    run_id = err.split("run ")[1].split(" ")[0]
    assert main(["rollback", "--run", run_id, *common]) == 0
    assert _result(capsys.readouterr().out)["state"] == "ROLLED_BACK"
    assert SimulatedHostBackend(sim_root).entry("/srv/c") is None


def test_simulated_host_root_is_required(capsys):
    with pytest.raises(SystemExit) as info:
        main(["plan", "install", "--all"])
    assert info.value.code == 2


@pytest.mark.parametrize("root", ["/var/lib/pantheon/germinate", "/run/pantheon/germinate", "/etc/sim"])
def test_production_roots_are_refused(root, capsys):
    assert main(["status", "--simulated-host-root", root]) == 1
    assert "production prefix" in capsys.readouterr().err


def test_no_postboot_refuses(tmp_path, capsys):
    assert main(["status", "--simulated-host-root", str(tmp_path / "fresh")]) == 1
    assert "postboot-unmet" in capsys.readouterr().err


def test_seat_scope_flag(setup, capsys):
    common, _ = setup
    with pytest.raises(SystemExit) as info:
        main(["plan", "install", "--seat", "orchestrator", "--system", "comp-a", *common])
    assert info.value.code == 2  # scopes are mutually exclusive
    capsys.readouterr()
    assert main(["plan", "install", "--seat", "orchestrator", *common]) == 0
    assert _result(capsys.readouterr().out)["nodes"] == ["comp-a"]
    assert main(["plan", "install", "--seat", "Not/A/Seat", *common]) == 1


def test_registry_group_is_unchanged(setup, capsys):
    common, _ = setup
    repo = common[1]
    assert main(["registry", "validate", "--repo-root", repo, "--today", "2026-10-17"]) == 0


def test_sim_init_then_end_to_end(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _p, digest = H.write_contract(repo, "comp-a", ops=OPS, desired_schema={"channel": {"enum": ["stable", "edge"]}})
    H.make_checkout(repo, [H.row("comp-a", digest)])
    sim_root = tmp_path / "e2e"
    common = ["--repo-root", str(repo), "--simulated-host-root", str(sim_root)]
    assert main(["sim", "init", "--simulated-host-root", str(sim_root)]) == 0
    assert _result(capsys.readouterr().out)["postboot"] == "simulated"
    assert main(["plan", "install", "--system", "comp-a", "--set", "channel=edge", *common]) == 0
    digest = _result(capsys.readouterr().out)["plan_digest"]
    assert main(["apply", "install", "--system", "comp-a", "--set", "channel=edge", "--plan-digest", digest, *common]) == 0
    assert _result(capsys.readouterr().out)[0]["state"] == "SATISFIED"
    assert main(["verify", "--all", *common]) == 0
    capsys.readouterr()
    assert main(["plan", "install", "--system", "comp-a", "--set", "channel=nightly", *common]) == 1
    assert "inputs-refused" in capsys.readouterr().err
    assert main(["plan", "install", "--system", "comp-a", "--from-rev", "a", *common]) == 1
    assert "go together" in capsys.readouterr().err


def test_sim_init_refuses_production_roots(capsys):
    assert main(["sim", "init", "--simulated-host-root", "/run/pantheon/sim"]) == 1
    assert "production prefix" in capsys.readouterr().err


def test_corrupt_run_record_is_a_typed_refusal(setup, capsys):
    common, sim_root = setup
    main(["plan", "install", "--system", "comp-a", *common])
    digest = _result(capsys.readouterr().out)["plan_digest"]
    main(["apply", "install", "--system", "comp-a", "--plan-digest", digest, *common])
    capsys.readouterr()
    [record] = list((sim_root / "run-state").rglob("record.json"))
    record.write_bytes(record.read_bytes().replace(b'"SATISFIED"', b'"BOGUS"'))
    assert main(["status", *common]) == 1
    assert "run-state-refused" in capsys.readouterr().err


def test_context_flag_defaults_to_operator_and_parses(setup, monkeypatch):
    from tools.germinate.engine import cli as engine_cli

    seen = []
    real = engine_cli._engine

    def spy(args):
        eng = real(args)
        seen.append((eng.context.kind, eng.context.account))
        return eng

    monkeypatch.setattr(engine_cli, "_engine", spy)
    common, _ = setup
    assert main(["plan", "install", "--system", "comp-a", *common]) == 0
    assert main(["plan", "install", "--system", "comp-a", *common, "--context", "scheduled", "--invoking-account", "alice"]) == 0
    assert seen == [("operator", None), ("scheduled", "alice")]


def test_invalid_context_value_exits_non_zero(setup, capsys):
    common, _ = setup
    with pytest.raises(SystemExit) as exc:
        main(["plan", "install", "--system", "comp-a", *common, "--context", "cron"])
    assert exc.value.code != 0
    assert "--context" in capsys.readouterr().err
