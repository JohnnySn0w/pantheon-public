"""T-5819: ``coordinator.release`` is a compare-and-delete that can never
delete a claim held by a different run (ORG-080 C1 / §4).

Interleavings are driven through the ``_after_holder_read`` injection point
and explicit second file descriptors -- no threads, no timing.
"""

from __future__ import annotations

import ast
import fcntl
import os
import re
from pathlib import Path

import pytest

from tools.germinate.planner_sandbox.admission import coordinator
from tools.germinate.planner_sandbox.admission.exceptions import ClaimHolderMismatch, PrimitiveUnavailable

INST, COMP = "inst", "comp"
TOOLS = Path(__file__).resolve().parents[3]  # .../tools


@pytest.fixture
def gates(tmp_path):
    coordinator.bootstrap_component(tmp_path, INST, COMP)
    return tmp_path


def _claim(gates, run_id):
    coordinator.claim(gates, INST, COMP, run_id=run_id, scope="system", uid=os.getuid(), verb="apply")


def _holder(gates):
    claim = coordinator.read_claim(gates, INST, COMP)
    return None if claim is None else claim["run_id"]


def _leaf(gates) -> Path:
    return gates / INST / COMP


def test_release_by_the_holder_removes_the_claim(gates):
    _claim(gates, "X")
    coordinator.release(gates, INST, COMP, expected_run_id="X")
    assert _holder(gates) is None


def test_missing_claim_is_a_noop(gates, tmp_path):
    coordinator.release(gates, INST, COMP, expected_run_id="X")  # bootstrapped, never claimed
    _claim(gates, "X")
    coordinator.release(gates, INST, COMP, expected_run_id="X")
    coordinator.release(gates, INST, COMP, expected_run_id="X")  # idempotent second release
    coordinator.release(tmp_path / "never-bootstrapped", INST, COMP, expected_run_id="X")
    assert _holder(gates) is None


def test_mismatch_is_refused_and_keeps_the_claim(gates):
    _claim(gates, "Y")
    with pytest.raises(ClaimHolderMismatch) as exc_info:
        coordinator.release(gates, INST, COMP, expected_run_id="X")
    err = exc_info.value
    assert (err.component, err.expected_run_id, err.actual_run_id) == (COMP, "X", "Y")
    assert "claim-holder-mismatch" in str(err)
    assert _holder(gates) == "Y"


def test_two_releasers_with_a_fresh_claim_between_refuse_the_second(gates):
    """#1 and #2 both release X; Y claims between them; #2 refuses and Y keeps admission."""

    _claim(gates, "X")
    coordinator.release(gates, INST, COMP, expected_run_id="X")  # releaser #1
    _claim(gates, "Y")  # a fresh run is admitted
    with pytest.raises(ClaimHolderMismatch):
        coordinator.release(gates, INST, COMP, expected_run_id="X")  # releaser #2 (stale)
    assert _holder(gates) == "Y"
    with pytest.raises(coordinator.AdmissionRefused):
        _claim(gates, "Z")  # a third run stays refused


def test_malformed_claim_fails_closed_and_is_not_deleted(gates):
    canonical = _leaf(gates) / coordinator._CLAIM_LEAF_NAME
    canonical.write_text("{broken", encoding="utf-8")
    with pytest.raises(PrimitiveUnavailable):
        coordinator.release(gates, INST, COMP, expected_run_id="X")
    assert canonical.read_text(encoding="utf-8") == "{broken"  # pre-T-5819 this was unlinked


def test_release_critical_section_excludes_a_concurrent_releaser(gates, monkeypatch):
    """Inside the section (holder read, before unlink) no other releaser can take the lock,
    so the claim it read cannot be swapped before the unlink."""

    _claim(gates, "X")
    observed = []

    def probe():
        fd = os.open(_leaf(gates), os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observed.append(True)
        finally:
            os.close(fd)

    monkeypatch.setattr(coordinator, "_after_holder_read", probe)
    coordinator.release(gates, INST, COMP, expected_run_id="X")
    assert observed and _holder(gates) is None
    fd = os.open(_leaf(gates), os.O_RDONLY | os.O_DIRECTORY)  # and the lock is dropped afterwards
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_release_lock_busy_fails_closed_without_deleting(gates, monkeypatch):
    _claim(gates, "X")
    monkeypatch.setattr(coordinator, "_RELEASE_LOCK_WAIT_S", 0.0)
    fd = os.open(_leaf(gates), os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(PrimitiveUnavailable, match="release lock busy"):
            coordinator.release(gates, INST, COMP, expected_run_id="X")
    finally:
        os.close(fd)
    assert _holder(gates) == "X"


def test_claim_takes_no_lock_so_admission_semantics_are_unchanged(gates):
    fd = os.open(_leaf(gates), os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _claim(gates, "X")  # admission proceeds while the release lock is held elsewhere
    finally:
        os.close(fd)
    assert _holder(gates) == "X"


def test_release_requires_expected_run_id(gates):
    _claim(gates, "X")
    with pytest.raises(TypeError):
        coordinator.release(gates, INST, COMP)  # the unconditional form no longer exists
    with pytest.raises(TypeError):
        coordinator.release(gates, INST, COMP, "X")  # keyword-only
    assert _holder(gates) == "X"


@pytest.mark.parametrize("payload", ["[]", '"x"', "3", "null"])
def test_non_object_json_claim_fails_closed_and_is_not_deleted(gates, payload):
    canonical = _leaf(gates) / coordinator._CLAIM_LEAF_NAME
    canonical.write_text(payload, encoding="utf-8")
    with pytest.raises(PrimitiveUnavailable, match="expected a JSON object"):
        coordinator.release(gates, INST, COMP, expected_run_id="X")
    assert canonical.read_text(encoding="utf-8") == payload


def test_symlinked_claim_fails_closed_and_is_not_deleted(gates, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text('{"run_id": "X"}', encoding="utf-8")
    canonical = _leaf(gates) / coordinator._CLAIM_LEAF_NAME
    canonical.symlink_to(target)
    with pytest.raises(PrimitiveUnavailable, match="claim unreadable"):
        coordinator.release(gates, INST, COMP, expected_run_id="X")
    assert canonical.is_symlink() and target.exists()


def test_tree_replaced_mid_section_acts_on_the_locked_directory(gates, monkeypatch):
    """L1 / C4-1: between the read and the unlink the leaf is renamed away and a fresh leaf
    holding another run's claim appears at the old path; release deletes only in the locked dir."""

    _claim(gates, "X")
    moved = gates / INST / "moved-away"

    def swap_tree():
        _leaf(gates).rename(moved)
        coordinator.bootstrap_component(gates, INST, COMP)
        _claim(gates, "Y")

    monkeypatch.setattr(coordinator, "_after_holder_read", swap_tree)
    coordinator.release(gates, INST, COMP, expected_run_id="X")
    assert not (moved / coordinator._CLAIM_LEAF_NAME).exists()  # X's claim, in the locked dir, is gone
    assert _holder(gates) == "Y"  # the replacement tree's claim is untouched


# --- caller-migration pin ---------------------------------------------------------


def _py_files():
    for path in TOOLS.rglob("*.py"):
        rel = path.relative_to(TOOLS).parts
        if ".claude" in rel or "node_modules" in rel:
            continue
        yield path


def test_every_release_caller_passes_its_run_id():
    """Every ``coordinator.release`` call passes ``expected_run_id=``; every engine
    ``._release`` call passes (component, run_id). A new unconditional caller fails here."""

    seen_coordinator, seen_engine, bad = 0, 0, []
    for path in _py_files():
        src = path.read_text(encoding="utf-8", errors="replace")
        if "coordinator.release" not in src and "._release(" not in src:  # cheap prefilter before ast.parse
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            func = node.func
            where = f"{path.relative_to(TOOLS)}:{node.lineno}"
            if func.attr == "release" and isinstance(func.value, ast.Name) and func.value.id == "coordinator":
                seen_coordinator += 1
                if not any(k.arg == "expected_run_id" for k in node.keywords):
                    if path.name != "test_release_cas.py":  # the deliberate TypeError probes above
                        bad.append(where)
            elif func.attr == "_release" and "germinate" in path.parts:
                seen_engine += 1
                if len(node.args) + len(node.keywords) != 2:
                    if path.name != "test_release_holder_race.py":  # the deliberate TypeError probe
                        bad.append(where)
    assert not bad, f"release callers without the releasing run id: {bad}"
    assert seen_coordinator >= 2 and seen_engine >= 7  # gate.acquire + Engine._release; the 7 engine sites


_REMOVERS = {"unlink", "rename", "replace", "remove", "rmdir", "_best_effort_unlink"}
_CLAIM_NAMES = {"canonical", "_CLAIM_LEAF_NAME"}
_CLAIM_PATH_USERS = {"claim", "read_claim", "release"}


def _names_claim(node: ast.AST) -> bool:
    """``canonical`` / ``_CLAIM_LEAF_NAME``, or a ``_claim_path(...)`` call."""

    if isinstance(node, ast.Name):
        return node.id in _CLAIM_NAMES
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_claim_path"


def _claim_removal_offenders(src: str) -> list[str]:
    """Remover calls on the claim outside ``release`` (as an argument --
    ``os.unlink(canonical)``, ``Path.unlink(canonical)`` -- or as the receiver --
    ``canonical.unlink()``, ``_claim_path(...).rename(x)``), and any
    ``_claim_path(...)`` use outside claim/read_claim/release."""

    offenders = []
    for fn in [n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
            if name == "_claim_path" and fn.name not in _CLAIM_PATH_USERS:
                offenders.append(f"{fn.name}:{node.lineno}:_claim_path")
            if name in _REMOVERS and fn.name != "release":
                receiver = func.value if isinstance(func, ast.Attribute) else None
                args = list(node.args) + [k.value for k in node.keywords]
                if (receiver is not None and _names_claim(receiver)) or any(_names_claim(a) for a in args):
                    offenders.append(f"{fn.name}:{node.lineno}:{name}")
    return offenders


def test_only_release_removes_the_canonical_claim():
    """flock is advisory: the compare-and-delete holds only if release() is the sole remover."""

    src = (TOOLS / "germinate/planner_sandbox/admission/coordinator.py").read_text(encoding="utf-8")
    assert _claim_removal_offenders(src) == []
    for path in _py_files():
        if path.name == "coordinator.py" or path.name.startswith("test_") or "tests" in path.parts:
            continue  # production code only: tests seed claims (e.g. a malformed one) deliberately
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"\b(_claim_path|_CLAIM_LEAF_NAME)\b|_component_gate\.claim", text):
            pytest.fail(f"{path.relative_to(TOOLS)} reaches the claim path outside the coordinator")


_HDR = "def claim(system_dir, i, c):\n"


@pytest.mark.parametrize(
    "planted, expect",
    [
        (_HDR + "    canonical = _claim_path(system_dir, i, c)\n    canonical.unlink()\n", ["unlink"]),
        (_HDR + "    os.unlink(canonical)\n", ["unlink"]),
        (_HDR + "    Path.unlink(canonical)\n", ["unlink"]),
        (_HDR + "    canonical.replace(other)\n", ["replace"]),
        (_HDR + "    _claim_path(system_dir, i, c).rename(other)\n", ["rename"]),
        (_HDR + "    os.rename(_CLAIM_LEAF_NAME, x, src_dir_fd=fd)\n", ["rename"]),
        (_HDR + "    _best_effort_unlink(canonical)\n", ["_best_effort_unlink"]),
        ("def sweep(system_dir, i, c):\n    p = _claim_path(system_dir, i, c)\n", ["_claim_path"]),
        ("def release(system_dir, i, c):\n    canonical = _claim_path(system_dir, i, c)\n    canonical.unlink()\n", []),
    ],
)
def test_remover_pin_catches_a_planted_removal(planted, expect):
    """Self-test on source strings (no real edit): the pin flags each planted shape."""

    assert [o.rsplit(":", 1)[1] for o in _claim_removal_offenders(planted)] == expect
