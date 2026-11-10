"""T-5921: the live READ-ONLY preflight reader (tools/germinate/preflight_live.py), DARK per seat.

Every test drives the reader through an injected fake runner or tmp_path files; no
test runs a real process against the host. The dark pin is asserted on the
committed source (AST), and the pin is flipped only in-memory (monkeypatch) to
exercise the observe paths.
"""

from __future__ import annotations

import ast
import json
import os
import socket
from pathlib import Path

import pytest

from tools._organ_lifecycle import CommandResult, FakeCommandRunner
from tools.germinate import preflight_live as L
from tools.germinate.cli import main
from tools.germinate.engine import preflight as PF
from tools.germinate.engine.tests import helpers as H
from tools.germinate.engine.tests.test_preflight import OPS, write_observation

LIVE_SRC = Path(L.__file__)
ENGINE_DIR = Path(PF.__file__).parent
HEAD = "d" * 40
SEAT = "suborch2"


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(L, "LIVE_READER_SEATS", {SEAT: True})


def _port(runner):
    return L.LivePreflightPort(SEAT, runner=runner)


# --- dark by construction --------------------------------------------------------------


def test_every_committed_pin_is_the_literal_false():
    tree = ast.parse(LIVE_SRC.read_text(encoding="utf-8"))
    [node] = [n for n in tree.body if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", None) == "LIVE_READER_SEATS"]
    assert isinstance(node.value, ast.Dict) and node.value.keys
    for key, value in zip(node.value.keys, node.value.values):
        assert isinstance(key, ast.Constant) and isinstance(value, ast.Constant) and value.value is False, ast.dump(value)
    assert L.LIVE_READER_SEATS == {SEAT: False}


@pytest.mark.parametrize("seat", [SEAT, "not-listed", ""])
def test_the_reader_refuses_while_dark(seat):
    with pytest.raises(L.LiveReaderDisabled):
        L.LivePreflightPort(seat, runner=FakeCommandRunner())


def test_a_truthy_non_true_pin_still_refuses(monkeypatch):
    monkeypatch.setattr(L, "LIVE_READER_SEATS", {SEAT: 1})
    with pytest.raises(L.LiveReaderDisabled):
        L.LivePreflightPort(SEAT, runner=FakeCommandRunner())


def test_nothing_in_engine_imports_the_live_reader():
    for path in ENGINE_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        assert not {m for m in mods if "preflight_live" in m}, path.name


def test_cli_live_tier_refuses_while_dark(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _p, digest = H.write_contract(repo, "comp-a", ops=OPS)
    H.make_checkout(repo, [H.row("comp-a", digest)])
    sim = tmp_path / "sim"
    common = ["--repo-root", str(repo), "--simulated-host-root", str(sim)]
    assert main(["sim", "init", "--simulated-host-root", str(sim)]) == 0
    write_observation(sim)
    capsys.readouterr()
    assert main(["plan", "install", "--seat", SEAT, "--preflight", "--observation-tier", "live", *common]) == 1
    err = capsys.readouterr().err
    assert "refused: observation-tier-refused: LiveReaderDisabled" in err
    assert main(["plan", "install", "--system", "comp-a", "--observation-tier", "live", *common]) == 2
    assert "only apply with --preflight" in capsys.readouterr().err


def test_cli_live_tier_wires_the_injected_port(tmp_path, capsys, enabled, monkeypatch):
    """With the pin flipped in memory and a fake runner, the receipt is on the live tier."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _p, digest = H.write_contract(repo, "comp-a", ops={"install": []}, scopes=["seat"])
    H.make_checkout(repo, [H.row("comp-a", digest)])
    sim = tmp_path / "sim"
    assert main(["sim", "init", "--simulated-host-root", str(sim)]) == 0
    built = []

    def factory(seat):
        built.append(seat)
        return _port(_scripted(repo))

    monkeypatch.setattr(L, "live_port_factory", factory)
    capsys.readouterr()
    code = main(["plan", "install", "--seat", SEAT, "--preflight", "--observation-tier", "live",
                 "--repo-root", str(repo), "--simulated-host-root", str(sim)])
    out = capsys.readouterr().out
    receipt = [json.loads(line)["result"] for line in out.splitlines() if line.startswith('{"result"')][-1]["receipt"]
    assert built == [SEAT] and receipt["observation_tier"] == "live-readonly"
    assert code == (0 if receipt["verdict"] == "GO" else 1)


# --- the fixed argv and parsing --------------------------------------------------------


def _home():
    import pwd

    return pwd.getpwuid(os.getuid()).pw_dir


def _scripted(repo, *, head=HEAD, status="", sudo=CommandResult(1, "", "sudo: a password is required\n"), rc=0):
    runner = FakeCommandRunner()
    runner.scripted = {
        tuple(L.SUDO_LIST_ARGV): sudo,
        L.git_argv(_home(), str(repo), "rev-parse", "HEAD"): CommandResult(rc, head + "\n", ""),
        L.git_argv(_home(), str(repo), "status", "--porcelain=v1", "--untracked-files=normal"): CommandResult(rc, status, ""),
    }
    runner.default = CommandResult(99, "", "unscripted argv")
    return runner


def test_sudo_argv_is_fixed_and_cleared(enabled):
    assert L.SUDO_LIST_ARGV == ("/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "LC_ALL=C", "/usr/bin/sudo", "-n", "-l")
    runner = _scripted("/r")
    listing = _port(runner).sudo_listing()
    assert listing == PF.SudoListing(1, "", "sudo: a password is required\n")
    assert PF.sudo_outcome(listing) == PF.SUDO_PASSWORD_REQUIRED
    assert runner.calls == [L.SUDO_LIST_ARGV]


def test_git_argv_is_read_only_and_fixed(enabled):
    argv = L.git_argv("/home/x", "/repo", "status", "--porcelain=v1")
    assert argv[:6] == ("/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "HOME=/home/x", "LC_ALL=C", "/usr/bin/git")
    assert argv[6:9] == ("--no-optional-locks", "-C", "/repo")


@pytest.mark.parametrize("status, clean", [("", True), (" M a.py\n", False), ("?? new\n", False)])
def test_checkout_clean_and_dirty(enabled, status, clean):
    port = _port(_scripted("/repo", status=status))
    assert port.checkout(Path("/repo")) == PF.CheckoutObservation(HEAD, clean)


@pytest.mark.parametrize("kw", [{"rc": 128}, {"head": "not-a-sha"}, {"head": "a" * 39}])
def test_checkout_errors_are_unobservable(enabled, kw):
    with pytest.raises(PF.Unobservable):
        _port(_scripted("/repo", **kw)).checkout(Path("/repo"))


def test_a_runner_failure_is_unobservable(enabled):
    class Exploding:
        def run(self, argv):
            raise FileNotFoundError("no sudo binary")

    with pytest.raises(PF.Unobservable):
        _port(Exploding()).sudo_listing()


def test_operator_is_observed_in_process(enabled):
    op = _port(FakeCommandRunner()).operator()
    assert op.uid == os.getuid() and op.host == socket.gethostname().split(".", 1)[0]


# --- the in-process probe ---------------------------------------------------------------


def test_path_stat_never_follows_and_reports_mode(enabled, tmp_path):
    port = _port(FakeCommandRunner())
    f = tmp_path / "cred"
    f.write_text("x", encoding="utf-8")
    os.chmod(f, 0o600)
    link = tmp_path / "link"
    link.symlink_to(f)
    assert port.path_stat(str(f)) == PF.PathStat("file", os.getuid(), os.getgid(), "0600")
    assert port.path_stat(str(link)).kind == "symlink"
    assert port.path_stat(str(tmp_path / "absent")) is None
    assert port.path_stat(str(tmp_path)).kind == "directory"
    with pytest.raises(PF.Unobservable):
        port.path_stat("relative")


def test_reads_are_bounded_regular_and_no_follow(enabled, tmp_path, monkeypatch):
    port = _port(FakeCommandRunner())
    f = tmp_path / "a.json"
    f.write_text('{"k": 1}', encoding="utf-8")
    link = tmp_path / "l.json"
    link.symlink_to(f)
    import hashlib

    assert port.file_sha256(str(f)) == hashlib.sha256(b'{"k": 1}').hexdigest()
    assert port.read_json(str(f)) == {"k": 1}
    assert port.read_json(str(tmp_path / "absent")) is None
    for call in (lambda: port.read_json(str(link)), lambda: port.file_sha256(str(tmp_path))):
        with pytest.raises(PF.Unobservable):
            call()
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(PF.Unobservable):
        port.read_json(str(tmp_path / "bad.json"))
    monkeypatch.setattr(L, "MAX_JSON_BYTES", 4)
    with pytest.raises(PF.Unobservable):
        port.read_json(str(f))


def test_account_lookup(enabled):
    import pwd

    port = _port(FakeCommandRunner())
    me = pwd.getpwuid(os.getuid())
    assert port.account(me.pw_name) == PF.AccountInfo(me.pw_uid, me.pw_dir)
    assert port.account("no-such-account-t5921") is None
    with pytest.raises(PF.Unobservable):
        port.account("Bad Name")
