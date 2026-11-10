"""T-5940 (S5): the dark live seat port. Pinned refusal, per-seat pins, fixed argv
construction against a FakeCommandRunner (no real systemctl), parsers, confined seat
files (tmp_path only), and the structural/lint guards."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from tools import codex_recovery
from tools._organ_lifecycle import CommandResult, FakeCommandRunner
from tools.germinate.components import harness_manager_run_state as RS
from tools.germinate.engine.backend import BackendRefused
from tools.germinate.seat_backend import ports as P

PKG = Path(P.__file__).resolve().parent
TOOLS = PKG.parents[1]
UID = 1009
PREFIX = ["/usr/bin/env", "XDG_RUNTIME_DIR=/run/user/1009", "/usr/bin/systemctl", "--user"]


class _Ops(P._LiveSeatOps):
    """Test-only: the port's operations with a fake runner, no dark gate."""

    def __init__(self, runner, state_root):
        self._runner, self._uid, self._state_root = runner, UID, Path(state_root)


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# --- the pin -------------------------------------------------------------------------


def test_every_seat_pin_is_literal_false():
    assigns = [
        n for n in ast.walk(_tree(PKG / "ports.py"))
        if isinstance(n, ast.AnnAssign | ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "LIVE_ACTUATION_ENABLED"
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))
    ]
    assert len(assigns) == 1
    call = assigns[0].value
    assert isinstance(call, ast.Call) and getattr(call.func, "id", None) == "MappingProxyType"
    (table,) = call.args
    assert isinstance(table, ast.Dict) and table.keys
    assert all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in table.keys)
    assert all(isinstance(v, ast.Constant) and v.value is False for v in table.values)
    assert dict(P.LIVE_ACTUATION_ENABLED) == {"artisan": False}


def test_the_pin_module_reads_no_override():
    tree = _tree(PKG / "ports.py")
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not ({"environ", "getenv", "getenvb", "environb"} & (names | attrs))
    assert not ({"argparse", "sys"} & imported)


def test_the_pin_table_is_read_only():
    with pytest.raises(TypeError):
        P.LIVE_ACTUATION_ENABLED["artisan"] = True  # type: ignore[index]


def test_is_live_is_per_seat():
    flipped = {"artisan": True}
    assert P.is_live("artisan", flipped)
    assert not P.is_live("mason", flipped)
    assert not P.is_live("artisan", {"artisan": 1})  # exactly True, never truthy
    assert not P.is_live("artisan", {"artisan": "true"})
    for persona in ("artisan", "mason", "orchestrator", "nobody"):
        assert not P.is_live(persona)


@pytest.mark.parametrize("persona", ["artisan", "mason", "nobody"])
def test_the_port_refuses_while_pinned(persona, tmp_path):
    runner = FakeCommandRunner()
    with pytest.raises(P.LiveActuationDisabled, match="disabled"):
        P.LiveSeatPort(persona, runner, uid=UID, state_root=tmp_path)
    assert runner.calls == []
    assert list(tmp_path.iterdir()) == []
    assert issubclass(P.LiveActuationDisabled, BackendRefused)


# --- argv ----------------------------------------------------------------------------


def test_binaries_match_the_codex_recovery_argv():
    assert P.SYSTEMCTL == codex_recovery.SYSTEMCTL
    assert P.ENV_BIN == codex_recovery.ENV_BIN


def test_unit_regex_matches_the_run_state_module():
    assert P.UNIT_RE.pattern == RS.UNIT_RE.pattern


def test_exact_argv():
    props = "--property=Id,LoadState,ActiveState,SubState,UnitFileState,NextElapseUSecRealtime"
    assert P.show_argv(UID, ["a.timer", "b.service"]) == PREFIX + ["show", props, "--", "a.timer", "b.service"]
    assert P.disable_now_argv(UID, "pantheon-keep-going@artisan.timer") == PREFIX + [
        "disable", "--now", "--", "pantheon-keep-going@artisan.timer"]
    assert P.enable_now_argv(UID, "x.service") == PREFIX + ["enable", "--now", "--", "x.service"]
    assert P.reset_failed_argv(UID, ["x.service", "y.timer"]) == PREFIX + ["reset-failed", "--", "x.service", "y.timer"]
    assert P.list_units_argv(UID) == PREFIX + [
        "list-units", "--all", "--plain", "--no-legend", "--no-pager", "--type=service,timer"]
    assert P.list_unit_files_argv(UID) == PREFIX + [
        "list-unit-files", "--plain", "--no-legend", "--no-pager", "--type=service,timer"]


@pytest.mark.parametrize("unit", ["-x.service", "--now.service", "a*.timer", "a b.service", "foo", "../x.service",
                                  "a/b.service", "x.slice", "", None, 3])
def test_bad_unit_names_refuse(unit):
    with pytest.raises(P.SeatPortError):
        P.disable_now_argv(UID, unit)


@pytest.mark.parametrize("units", [[], "a.service", ["a.service", "a.service"]])
def test_bad_unit_lists_refuse(units):
    with pytest.raises(P.SeatPortError):
        P.reset_failed_argv(UID, units)


@pytest.mark.parametrize("uid", [0, -1, True, "1009", None])
def test_root_or_bad_uid_refuses(uid):
    with pytest.raises(P.SeatPortError):
        P.user_manager_prefix(uid)


# --- operations against a fake runner ------------------------------------------------

SHOW_OUT = (
    "Id=a.timer\nLoadState=loaded\nActiveState=active\nSubState=waiting\nUnitFileState=enabled\n"
    "NextElapseUSecRealtime=Mon 2026-11-02 05:00:00 UTC\n\n"
    "Id=a.service\nLoadState=loaded\nActiveState=inactive\nSubState=dead\nUnitFileState=static\n"
)


def test_mutations_run_exactly_one_fixed_argv(tmp_path):
    runner = FakeCommandRunner()
    ops = _Ops(runner, tmp_path)
    ops.disable_now("a.timer")
    ops.enable_now("a.timer")
    ops.reset_failed(["a.service"])
    assert runner.calls == [
        tuple(PREFIX + ["disable", "--now", "--", "a.timer"]),
        tuple(PREFIX + ["enable", "--now", "--", "a.timer"]),
        tuple(PREFIX + ["reset-failed", "--", "a.service"]),
    ]


def test_a_nonzero_exit_raises(tmp_path):
    runner = FakeCommandRunner(default=CommandResult(1, "", "Failed to connect to bus"))
    with pytest.raises(P.SeatPortError, match="exited 1"):
        _Ops(runner, tmp_path).disable_now("a.timer")


def test_show_reads_exact_fields(tmp_path):
    argv = tuple(P.show_argv(UID, ["a.timer", "a.service"]))
    ops = _Ops(FakeCommandRunner({argv: CommandResult(0, SHOW_OUT, "")}), tmp_path)
    got = ops.show(["a.timer", "a.service"])
    assert got["a.timer"]["ActiveState"] == "active"
    assert got["a.timer"]["NextElapseUSecRealtime"].startswith("Mon")
    assert got["a.service"] == {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead", "UnitFileState": "static"}


@pytest.mark.parametrize("stdout", [
    "",  # nothing back
    "Id=a.timer\nLoadState=loaded\n",  # missing fields
    SHOW_OUT.replace("SubState=dead", "SubState dead"),  # not key=value
    SHOW_OUT.replace("SubState=dead", "Bogus=x"),  # unknown property
    SHOW_OUT.replace("Id=a.service", "Id=b.service"),  # a different unit
    SHOW_OUT + "\nId=a.timer\nLoadState=loaded\nActiveState=x\nSubState=x\nUnitFileState=x\n",  # duplicate
])
def test_malformed_show_raises(stdout, tmp_path):
    argv = tuple(P.show_argv(UID, ["a.timer", "a.service"]))
    ops = _Ops(FakeCommandRunner({argv: CommandResult(0, stdout, "")}), tmp_path)
    with pytest.raises(P.SeatPortError):
        ops.show(["a.timer", "a.service"])


def test_observe_units_lists_then_shows(tmp_path):
    scripted = {
        tuple(P.list_units_argv(UID)): CommandResult(0, "a.timer loaded active waiting A\nsession-3.scope x\n", ""),
        tuple(P.list_unit_files_argv(UID)): CommandResult(0, "a.service static -\na.timer enabled enabled\n", ""),
        tuple(P.show_argv(UID, ["a.service", "a.timer"])): CommandResult(0, SHOW_OUT, ""),
    }
    runner = FakeCommandRunner(scripted)
    assert sorted(_Ops(runner, tmp_path).observe_units()) == ["a.service", "a.timer"]
    assert len(runner.calls) == 3


# --- the seat's own files ------------------------------------------------------------


def test_files_round_trip_inside_the_state_root(tmp_path):
    root = Path(os.path.realpath(tmp_path)) / "state"
    root.mkdir()
    ops = _Ops(FakeCommandRunner(), root)
    ops.ensure_dir(root / "harness-manager")
    target = root / "harness-manager" / "m.json"
    assert ops.read_file(target) is None
    ops.write_file_atomic(target, b"{}\n")
    assert ops.read_file(target) == b"{}\n" and (target.stat().st_mode & 0o777) == 0o600
    ops.write_file_atomic(target, b"[]\n")
    assert ops.read_file(target) == b"[]\n"
    assert sorted(p.name for p in target.parent.iterdir()) == ["m.json"]
    ops.remove_file(target)
    ops.remove_file(target)  # idempotent
    assert not target.exists()


def test_files_outside_or_through_symlinks_refuse(tmp_path):
    base = Path(os.path.realpath(tmp_path))
    root = base / "state"
    root.mkdir()
    (base / "elsewhere").mkdir()
    (root / "link").symlink_to(base / "elsewhere")
    (root / "file-link").symlink_to(base / "elsewhere" / "x")
    ops = _Ops(FakeCommandRunner(), root)
    for bad in (base / "elsewhere" / "x", f"{root}/../elsewhere/x", "relative/x", root, root / "link" / "x"):
        with pytest.raises(P.SeatPortError):
            ops.write_file_atomic(bad, b"x")
    with pytest.raises(P.SeatPortError):
        ops.write_file_atomic(root / "file-link", b"x")
    with pytest.raises(P.SeatPortError):
        ops.remove_file(root / "file-link")
    with pytest.raises(OSError):
        ops.read_file(root / "file-link")  # O_NOFOLLOW
    assert list((base / "elsewhere").iterdir()) == []


# --- structural ----------------------------------------------------------------------

BANNED_MODULES = ("subprocess", "socket", "ctypes", "dbus", "jeepney", "http", "urllib", "multiprocessing", "pty", "shutil")
BANNED_OS_CALLS = ("system", "popen", "fork", "forkpty", "kill", "killpg")


@pytest.mark.parametrize("path", sorted(PKG.glob("*.py")), ids=lambda p: p.name)
def test_seat_backend_has_no_direct_process_or_socket_reach(path):
    found = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] in BANNED_MODULES]
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in BANNED_MODULES:
            found.append(node.module)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
            if node.attr in BANNED_OS_CALLS or node.attr.startswith(("exec", "spawn")):
                found.append(f"os.{node.attr}")
    assert found == []


UNGATED = ("_LiveSeatOps", "_SeatBackendCore")
#: The ONLY permitted reference to each ungated base: as the base of its live class, in
#: the module that defines it (where the dark gate is that live class's constructor).
GATED_USE = {"ports.py": ("_LiveSeatOps", "LiveSeatPort"), "backend.py": ("_SeatBackendCore", "LiveSeatBackend")}


def _ungated_refs(path: Path, text: str) -> list[str]:
    """Every reference to an ungated base in ``text`` (name use, attribute access, import,
    or the bare name as a string, e.g. ``getattr(P, "_LiveSeatOps")``), except the one
    permitted base reference per defining module (review LOW, T-5940): a direct
    instantiation would bypass the pin and the euid binding."""

    tree = ast.parse(text, filename=str(path))
    allowed: set[int] = set()
    gated = GATED_USE.get(path.name) if path.resolve().parent == PKG else None
    if gated is not None:
        base_name, live_class = gated
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == live_class:
                allowed.update(id(b) for b in node.bases if getattr(b, "id", None) == base_name)
    found = []
    for node in ast.walk(tree):
        if id(node) in allowed:
            continue
        if isinstance(node, ast.Name) and node.id in UNGATED:
            found.append(f"{path.name}:{node.lineno} names {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in UNGATED:
            found.append(f"{path.name}:{node.lineno} accesses .{node.attr}")
        elif isinstance(node, ast.alias) and (node.name.rpartition(".")[2] in UNGATED or node.asname in UNGATED):
            found.append(f"{path.name}:{getattr(node, 'lineno', '?')} imports {node.name}")
        elif isinstance(node, ast.Constant) and node.value in UNGATED:
            found.append(f"{path.name}:{node.lineno} spells {node.value!r}")
    return found


def _production_sources():
    """Non-test .py under tools/, plus the repo-root scripts."""

    for path in [*TOOLS.rglob("*.py"), *TOOLS.parent.glob("*.py")]:
        if "tests" in path.parts or path.name.startswith("test_"):
            continue
        yield path


def test_only_the_live_classes_reference_the_ungated_bases():
    """The gate lives in the two live constructors. No production code may subclass,
    instantiate, import or otherwise name the ungated bases (tests excluded)."""

    found = []
    for path in _production_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(name in text for name in UNGATED):
            found += _ungated_refs(path, text)
    assert found == []


def test_the_live_classes_still_derive_from_the_ungated_bases():
    from tools.germinate.seat_backend import backend as B

    assert issubclass(P.LiveSeatPort, P._LiveSeatOps)
    assert issubclass(B.LiveSeatBackend, B._SeatBackendCore)


def test_the_scanner_fires_on_each_reference_form(tmp_path):
    src = (
        "from tools.germinate.seat_backend.ports import _LiveSeatOps\n"
        "from tools.germinate.seat_backend import ports as P\n"
        "import tools.germinate.seat_backend.backend._SeatBackendCore as C\n"
        "ops = P._LiveSeatOps()\n"
        "core = _SeatBackendCore\n"
        "sneaky = getattr(P, '_LiveSeatOps')\n"
        "class Rogue(P._LiveSeatOps):\n    pass\n"
    )
    found = _ungated_refs(tmp_path / "rogue.py", src)
    assert len(found) == 6, found
    assert any("imports tools.germinate.seat_backend.ports" not in f and "imports" in f and "_LiveSeatOps" in f for f in found)
    assert any("imports" in f and "_SeatBackendCore" in f for f in found)
    assert sum("accesses ._LiveSeatOps" in f for f in found) == 2  # the call and the subclass base
    assert any("names _SeatBackendCore" in f for f in found)
    assert any("spells '_LiveSeatOps'" in f for f in found)


def test_the_scanner_allows_only_the_live_base_in_its_defining_module():
    ports_text = (PKG / "ports.py").read_text(encoding="utf-8")
    backend_text = (PKG / "backend.py").read_text(encoding="utf-8")
    assert _ungated_refs(PKG / "ports.py", ports_text) == []
    assert _ungated_refs(PKG / "backend.py", backend_text) == []
    # the same live-class line in the other module, or a second use in the defining one, fires
    assert _ungated_refs(PKG / "backend.py", "class LiveSeatPort(_LiveSeatOps):\n    pass\n")
    assert _ungated_refs(PKG / "ports.py", ports_text + "\nx = _LiveSeatOps()\n")
    assert _ungated_refs(PKG.parent / "ports.py", ports_text)  # a same-named module elsewhere
