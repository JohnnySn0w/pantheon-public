"""T-6167 PR-4: the seat-side child (seat_child.py) and its place in the framework closure. DARK."""

from __future__ import annotations

import builtins
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import host_broker_install as HBI
from tools.germinate import live_run
from tools.germinate import seat_child as SC
from tools.persona_installer import Manifest

REPO = Path(__file__).resolve().parents[4]
DIGEST = "sha256:" + "a" * 64


def _manifest(*pairs):
    return SimpleNamespace(roles={f"r{i}": {"persona": p, "account": a} for i, (p, a) in enumerate(pairs)})


def test_persona_resolved_from_account():
    m = _manifest(("artisan", "atlasacct"), ("mason", "metisacct"))
    assert SC.persona_for_account(m, "atlasacct") == "artisan"
    assert SC.persona_for_account(m, "metisacct") == "mason"
    for bad in ("nobody", "", "atlasacct2"):
        with pytest.raises(LookupError):
            SC.persona_for_account(m, bad)
    with pytest.raises(LookupError):  # two personas on one account is ambiguous: refused
        SC.persona_for_account(_manifest(("a", "x"), ("b", "x")), "x")


def test_child_selfcheck_is_mutation_free(monkeypatch):
    opened = []
    real_open = builtins.open

    def spy(file, mode="r", *a, **k):
        opened.append((str(file), mode))
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", spy)
    out = io.StringIO()
    assert SC.main(["selfcheck", "alice"], out=out) == 0
    report = json.loads(out.getvalue())
    assert set(report) == {"account", "uid", "euid", "gid", "groups", "cwd", "env_keys", "isolated",
                           "dont_write_bytecode", "framework_root", "no_new_privs", "interpreter"}
    assert out.getvalue().count("\n") == 1
    assert all("w" not in m and "a" not in m and "+" not in m for _, m in opened), opened


def test_selfcheck_report_uses_process_state_only(monkeypatch):
    monkeypatch.setattr(SC.os, "getuid", lambda: 1101)
    monkeypatch.setattr(SC.os, "geteuid", lambda: 1101)
    monkeypatch.setattr(SC.os, "getgid", lambda: 1101)
    monkeypatch.setattr(SC.os, "getgroups", lambda: [1101, 3001])
    report = SC.selfcheck_report("alice", root=Path("/fw"))
    assert (report["uid"], report["euid"], report["gid"], report["groups"]) == (1101, 1101, 1101, [1101, 3001])
    assert report["framework_root"] == "/fw"


@pytest.mark.parametrize(
    "argv",
    [[], ["install"], ["install", "alice"], ["install", "alice", "sha256:short"], ["install", "alice", DIGEST, "x"],
     ["selfcheck"], ["selfcheck", "alice", "x"], ["selfcheck", "Alice"], ["selfcheck", "a;b"], ["frobnicate", "alice"],
     ["install", "../x", DIGEST]],
)
def test_main_usage_classification_unknown_is_exit_2(argv, capsys):
    assert SC.main(argv) == 2


def test_main_known_good_install_dispatches(monkeypatch):
    seen = []
    monkeypatch.setattr(SC, "run_install", lambda a, d: seen.append((a, d)) or 0)
    assert SC.main(["install", "alice", DIGEST]) == 0
    assert seen == [("alice", DIGEST)]


def _real_account():
    m = Manifest.load(REPO / "instance.yaml")
    for raw in m.roles.values():
        if isinstance(raw, dict) and raw.get("persona") and isinstance(raw.get("account"), str):
            return raw["account"], m
    raise AssertionError("no bound role in instance.yaml")


def test_child_refuses_when_persona_pin_false(capsys):
    """The third pin (seat_backend.ports.LIVE_ACTUATION_ENABLED, all False) refuses construction in
    the child; the child prints the typed code and the parent maps it to its own exit code."""

    account, _ = _real_account()
    code = SC.run_install(account, DIGEST, root=REPO)
    err = capsys.readouterr().err
    assert code == 1
    assert "refused: seat-pin-disabled:" in err
    assert live_run.classify_child(code, err) == ("seat-pin-disabled", 12)


def test_child_unresolvable_account_is_a_typed_refusal(capsys):
    assert SC.run_install("no-such-seat", DIGEST, root=REPO) == 1
    assert "refused: seat-persona-unresolved:" in capsys.readouterr().err


def test_child_dependency_and_postboot_refusals_reach_the_parent_mapping(capsys, monkeypatch):
    """The engine's own typed refusals pass through the child unchanged and map to distinct exits."""

    from tools.germinate.engine import cli as engine_cli
    from tools.germinate.engine.executor import Engine, EngineRefusal

    account, _ = _real_account()
    for code, exit_code in (("dependency-not-installed", 10), ("postboot-unmet", 11)):
        monkeypatch.setattr(Engine, "from_checkout", classmethod(lambda cls, *a, **k: object()))
        monkeypatch.setattr(engine_cli, "_apply", lambda e, a, code=code: (_ for _ in ()).throw(EngineRefusal(code, "x")))
        assert SC.run_install(account, DIGEST, root=REPO) == 1
        err = capsys.readouterr().err
        assert live_run.classify_child(1, err)[1] == exit_code


# --- the framework closure ------------------------------------------------------------

#: The exact set seat_child adds to the rev-pinned copy (Q1/Q-B approved): seat_child itself and
#: the 12 transitive tools/germinate modules it reaches. None is root-side code.
EXPECTED_ADDED = frozenset(
    {
        "tools/germinate/seat_child.py",
        "tools/germinate/cli.py",
        "tools/germinate/engine/cli.py",
        "tools/germinate/preflight_live.py",
        "tools/germinate/registry/adr_link.py",
        "tools/germinate/registry/cli.py",
        "tools/germinate/registry/git_resolver.py",
        "tools/germinate/registry/render.py",
        "tools/germinate/registry/repin.py",
        "tools/germinate/seat_backend/__init__.py",
        "tools/germinate/seat_backend/backend.py",
        "tools/germinate/seat_backend/install_handlers.py",
        "tools/germinate/seat_backend/ports.py",
    }
)
ROOT_SIDE = (
    "tools/germinate/live_run.py",
    "tools/germinate/host_substrate/root_launcher.py",
    "tools/germinate/components/harness_manager_root.py",
    "tools/germinate/host_substrate/families.py",
)


def _closure(entries):
    files, _external = HBI.import_closure(REPO, entries)
    return files


def test_closure_includes_seat_child_and_no_host_control_module():
    assert "tools.germinate.seat_child" in HBI.CLOSURE_ENTRY_MODULES
    others = [e for e in HBI.CLOSURE_ENTRY_MODULES if e != "tools.germinate.seat_child"]
    before, after = _closure(others), _closure(HBI.CLOSURE_ENTRY_MODULES)
    added = after - before
    assert added == EXPECTED_ADDED, sorted(added ^ EXPECTED_ADDED)
    assert before <= after
    assert all(f.startswith("tools/germinate/") for f in added)
    assert not any(f.startswith("tools/host_control") for f in added)
    # the root-side launcher stays out of the copy (live_run is loaded by name from germinate/cli.py)
    assert not any(f in after for f in ROOT_SIDE)


def test_service_closures_are_unchanged_by_seat_child():
    """The broker, approval and executor service closures start from their own host_control entries
    and never reach seat_child or any module it added."""

    svc = [e for e in HBI.CLOSURE_ENTRY_MODULES if e.startswith("tools.host_control")]
    assert svc
    reach = _closure(svc)
    assert not reach & EXPECTED_ADDED
    assert not any(f in reach for f in ROOT_SIDE)
