"""T-5477: the handlers-digest oracle and discover()'s fail-closed import pin (tamper tests)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tools.germinate.engine import components
from tools.germinate.engine import handlers_digest as HD
from tools.germinate.engine.components import ComponentLoadError, discover
from tools.germinate.engine.contract import ContractError, check_handlers_pin, read_component
from tools.germinate.engine.tests import helpers as H

PKG = "tools.germinate.components"


def _dir(tmp_path: Path) -> Path:
    directory = tmp_path / "components"
    directory.mkdir(exist_ok=True)
    return directory


def _write(directory: Path, name: str, src: str) -> Path:
    path = directory / name
    path.write_text(src, encoding="utf-8")
    return path


# --- the oracle --------------------------------------------------------------


def test_digest_is_deterministic_and_binds_entry_bytes(tmp_path):
    d = _dir(tmp_path)
    entry = _write(d, "c_handlers.py", "HANDLERS = ()\n")
    first = HD.compute(entry)
    assert first == HD.compute(entry)
    assert first.digest.startswith("sha256:") and first.files == ("c_handlers.py",)
    assert first.entry_source == b"HANDLERS = ()\n"
    _write(d, "c_handlers.py", "HANDLERS = () \n")  # one byte, no normalization
    assert HD.compute(entry).digest != first.digest


def test_function_level_helper_import_is_covered_and_binds_helper_bytes(tmp_path):
    d = _dir(tmp_path)
    entry = _write(d, "c_handlers.py", f"def verify():\n    from {PKG} import c_helper as V\n    return V\n")
    _write(d, "c_helper.py", "X = 1\n")
    before = HD.compute(entry)
    assert before.files == ("c_handlers.py", "c_helper.py")
    _write(d, "c_helper.py", "X = 2\n")
    assert HD.compute(entry).digest != before.digest


def test_helpers_are_followed_transitively_in_every_import_spelling(tmp_path):
    d = _dir(tmp_path)
    entry = _write(d, "c_handlers.py", f"import {PKG}.h_one\n")
    _write(d, "h_one.py", f"from {PKG}.h_two import Y\n")
    _write(d, "h_two.py", f"from {PKG} import h_one\nY = 1\n")  # a cycle terminates
    assert HD.compute(entry).files == ("c_handlers.py", "h_one.py", "h_two.py")


def test_shared_tools_modules_are_not_in_the_closure(tmp_path):
    d = _dir(tmp_path)
    entry = _write(
        d,
        "c_handlers.py",
        "import re\nimport tools.persona_installer\nfrom tools.persona_installer import MutationRecord\n"
        "from tools.germinate.host_substrate import families as F\nfrom tools import checkout_spec\nP = re.compile('x')\n",
    )
    assert HD.compute(entry).files == ("c_handlers.py",)  # re.compile is an attribute; sibling packages are not ancestors


def test_adding_or_renaming_a_helper_changes_the_digest(tmp_path):
    d = _dir(tmp_path)
    entry = _write(d, "c_handlers.py", f"from {PKG} import h_a\n")
    _write(d, "h_a.py", "X = 1\n")
    _write(d, "h_b.py", "X = 1\n")
    a = HD.compute(entry).digest
    _write(d, "c_handlers.py", f"from {PKG} import h_b\n")
    b = HD.compute(entry).digest
    _write(d, "c_handlers.py", f"from {PKG} import h_a, h_b\n")
    assert len({a, b, HD.compute(entry).digest}) == 3


@pytest.mark.parametrize(
    "setup, needle",
    [
        ("missing", "not a regular file"),
        ("symlink-helper", "not a regular file"),
        ("subpackage", "not a single helper module"),
    ],
)
def test_unpinnable_helpers_refuse(tmp_path, setup, needle):
    d = _dir(tmp_path)
    entry = _write(d, "c_handlers.py", f"from {PKG} import h_x\n")
    if setup == "symlink-helper":
        real = _write(tmp_path, "real.py", "X = 1\n")
        (d / "h_x.py").symlink_to(real)
    elif setup == "subpackage":
        (d / "h_x").mkdir()
    with pytest.raises(HD.HandlersDigestError, match=needle):
        HD.compute(entry)


def test_symlinked_entry_refuses(tmp_path):
    d = _dir(tmp_path)
    real = _write(tmp_path, "real_handlers.py", "HANDLERS = ()\n")
    (d / "c_handlers.py").symlink_to(real)
    with pytest.raises(HD.HandlersDigestError, match="symlinks are refused"):
        HD.compute(d / "c_handlers.py")


REFUSED = {
    "import importlib\n": "'importlib'",
    "import importlib.util\n": "'importlib.util'",
    "from importlib import import_module\n": "'importlib'",
    "import runpy\n": "'runpy'",
    "import builtins\n": "'builtins'",
    "def f():\n    return __import__('os')\n": "'__import__'",
    "exec('X = 1')\n": "'exec'",
    "X = eval('1')\n": "'eval'",
    "C = compile('1', 'x', 'eval')\n": "'compile'",
    "B = __builtins__\n": "'__builtins__'",
    f"from {PKG} import *\n": "star import",
    "from . import h\n": "relative import",
    f"import {PKG}\n": "bare",
    # review finding 2: the bare package reached through its parent, and its ancestors
    "from tools.germinate import components\n": "bare 'tools.germinate.components' package",
    "from tools.germinate import components as C\n": "bare 'tools.germinate.components' package",
    "from tools import germinate\n": "bare 'tools.germinate' package",
    "import tools.germinate\n": "bare 'tools.germinate' package",
    "import tools\n": "bare 'tools' package",
    "import pkgutil\n": "'pkgutil'",
    "import zipimport\n": "'zipimport'",
    "from marshal import loads\n": "'marshal'",
}


@pytest.mark.parametrize("src", sorted(REFUSED), ids=lambda s: s.strip().splitlines()[-1][:40])
def test_forms_the_walk_cannot_follow_refuse_naming_file_and_token(tmp_path, src):
    d = _dir(tmp_path)
    entry = _write(d, "c_handlers.py", src)
    with pytest.raises(HD.HandlersDigestError) as exc:
        HD.compute(entry)
    assert str(exc.value).startswith("c_handlers.py:") and REFUSED[src] in str(exc.value)


def test_a_refused_form_in_a_helper_names_the_helper(tmp_path):
    d = _dir(tmp_path)
    entry = _write(d, "c_handlers.py", f"from {PKG} import h_bad\n")
    _write(d, "h_bad.py", "import importlib\n")
    with pytest.raises(HD.HandlersDigestError, match=r"^h_bad\.py:1: imports 'importlib'"):
        HD.compute(entry)


def test_the_real_components_pin_their_helpers():
    root = Path(__file__).resolve().parents[2] / "components"
    assert HD.compute(root / "harness-manager_handlers.py").files == ("harness-manager_handlers.py", "harness_manager_verify.py")


# --- discover(): verify before import, fail closed ----------------------------

MARKER_SRC = "import pathlib\npathlib.Path({marker!r}).write_text('ran')\nOP_KINDS = {{'git-ff': ('repo.local', 'seat')}}\nHANDLERS = ('h',)\n"


def _pinned(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = tmp_path / "IMPORTED"
    H.write_contract(repo, "comp-a")
    digest = H.write_handlers(repo, "comp-a", MARKER_SRC.format(marker=str(marker)))
    H.make_checkout(repo, [H.row("comp-a", digest)])
    return repo, marker, digest


def _discover(repo: Path):
    return discover(repo, repo / "registry" / "systems.yaml")


def _handlers(repo: Path) -> Path:
    return repo / "tools" / "germinate" / "components" / "comp-a_handlers.py"


def test_a_correct_pin_loads(tmp_path):
    repo, marker, _ = _pinned(tmp_path)
    [reg] = _discover(repo)
    assert reg.handlers == ("h",) and reg.op_kinds == {"git-ff": ("repo.local", "seat")} and marker.exists()


def test_an_edited_handler_refuses_before_any_code_runs(tmp_path):
    repo, marker, _ = _pinned(tmp_path)
    path = _handlers(repo)
    path.write_text(path.read_text() + "\nEVIL = 1\n", encoding="utf-8")
    with pytest.raises(ComponentLoadError, match=r"comp-a: .*handlers_digest .* != recomputed .*handlers not imported"):
        _discover(repo)
    assert not marker.exists()


def test_an_edited_helper_refuses_before_any_code_runs(tmp_path):
    repo, marker, _ = _pinned(tmp_path)
    directory = _handlers(repo).parent
    (directory / "comp_a_helper.py").write_text("X = 1\n", encoding="utf-8")
    src = MARKER_SRC.format(marker=str(marker)) + f"\ndef helper():\n    from {PKG} import comp_a_helper\n"
    digest = H.write_handlers(repo, "comp-a", src)
    H.make_checkout(repo, [H.row("comp-a", digest)])
    assert check_handlers_pin(read_component(directory / "comp-a.yaml"), directory / "comp-a.yaml").files[-1] == "comp_a_helper.py"
    (directory / "comp_a_helper.py").write_text("X = 2\n", encoding="utf-8")
    with pytest.raises(ComponentLoadError, match="handlers_digest .* != recomputed .* comp_a_helper.py"):
        _discover(repo)
    assert not marker.exists()


def test_a_missing_pin_refuses(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = tmp_path / "IMPORTED"
    _p, digest = H.write_contract(repo, "comp-a")
    _handlers(repo).write_text(MARKER_SRC.format(marker=str(marker)), encoding="utf-8")
    H.make_checkout(repo, [H.row("comp-a", digest)])
    with pytest.raises(ComponentLoadError, match="exists but the contract pins no handlers_digest"):
        _discover(repo)
    assert not marker.exists()


def test_re_pinning_the_contract_line_alone_refuses_at_the_registry_digest(tmp_path):
    """An out-of-band edit to the handler AND its contract line, with the registry row untouched."""

    repo, marker, _ = _pinned(tmp_path)
    contract = _handlers(repo).with_name("comp-a.yaml")
    path = _handlers(repo)
    path.write_text(path.read_text() + "\nEVIL = 1\n", encoding="utf-8")
    body = yaml.safe_load(contract.read_text())["component"]
    body["handlers_digest"] = HD.compute(path).digest  # self-consistent contract, stale registry row
    contract.write_text(yaml.safe_dump({"component": body}, sort_keys=False), encoding="utf-8")
    with pytest.raises(ComponentLoadError, match="registry digest .* != recomputed .*handlers not imported"):
        _discover(repo)
    assert not marker.exists()


def test_a_pin_without_a_handlers_file_refuses(tmp_path):
    repo, _marker, _ = _pinned(tmp_path)
    _handlers(repo).unlink()
    contract = _handlers(repo).with_name("comp-a.yaml")
    with pytest.raises(ContractError, match="handlers_digest is declared but comp-a_handlers.py does not exist"):
        check_handlers_pin(read_component(contract), contract)


def test_discover_refuses_a_pin_whose_handlers_file_is_gone(tmp_path):
    """Review finding 3: the no-file skip must not bypass the pin (matches validate and load_contract)."""

    repo, _marker, _ = _pinned(tmp_path)
    _handlers(repo).unlink()
    with pytest.raises(ComponentLoadError, match="comp-a: handlers_digest is declared but comp-a_handlers.py does not exist"):
        _discover(repo)
    assert discover(repo, repo / "registry" / "systems.yaml", import_handlers=False) == []  # the validator path stays code-free


def test_discover_refuses_a_non_file_at_the_handlers_path(tmp_path):
    repo, _marker, _ = _pinned(tmp_path)
    _handlers(repo).unlink()
    _handlers(repo).mkdir()
    with pytest.raises(ComponentLoadError, match="comp-a: handlers module unpinnable"):
        _discover(repo)


def test_a_component_without_handlers_or_pin_still_loads(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _p, digest = H.write_contract(repo, "comp-a")
    H.make_checkout(repo, [H.row("comp-a", digest)])
    assert _discover(repo) == []


def test_the_validator_path_runs_no_code_and_checks_no_pin_at_discover(tmp_path):
    repo, marker, _ = _pinned(tmp_path)
    path = _handlers(repo)
    path.write_text(path.read_text() + "\nEVIL = 1\n", encoding="utf-8")
    [reg] = discover(repo, repo / "registry" / "systems.yaml", import_handlers=False)
    assert reg.handlers == () and not marker.exists()  # the validator reports the drift instead


def test_the_entry_runs_from_the_verified_bytes_not_a_re_read(tmp_path):
    path = _write(_dir(tmp_path), "c_handlers.py", "X = 'on-disk'\n")
    module = components._import(path, "c", b"X = 'verified'\n")
    assert module.X == "verified"
