"""C5 red-before-green canary tests (T-3295, ORG-080 tranche D).

Five canaries, one per README C5 test (planner_sandbox/README.md
"C5 -- Sandbox capability confinement"). Each runs the REAL launcher
(``launcher/launcher.py``) inside a REAL ``systemd-run --user`` transient
unit via the harness's ``run_in_sandbox()`` -- no mocks, per the arc's
whole premise.

**Design note -- reusing ``run_in_sandbox()`` unmodified.** The harness's
``run_in_sandbox(source, ...)`` always executes ``python3 -u <source-file>``
(no ``-SEI``, no extra argv) -- fixed shape, out of this tranche's file
scope to change. Each canary's ``source`` is therefore a tiny re-exec shim:
it primes stdin to immediate-EOF (an already-open pipe fd, no ``/dev``
path lookup needed -- see ``systemd_run_shape.py`` deviation 4) and then
``os.execv()``s into ``python3 -SEI <launcher.py> <argv...>``, exercising
the REAL canonical ExecStart form end to end. This works because plain
``execve`` (as opposed to ``execveat``) is deliberately left open at the
systemd ``SystemCallFilter`` layer per the ADR's own text ("~execve is NOT
in the systemd-level filter") -- the re-exec hop is faithful to the
documented design, not a workaround around it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tools.germinate.planner_sandbox.harness.fixtures import (
    temp_gate_tree,
    temp_scratch_dir,
)
from tools.germinate.planner_sandbox.harness.sandbox_runner import run_in_sandbox
from tools.germinate.planner_sandbox.launcher import systemd_run_shape

_LAUNCHER_DIR = Path(__file__).resolve().parent.parent / "launcher"
_LAUNCHER_PATH = _LAUNCHER_DIR / "launcher.py"
_SECCOMP_VENDOR_DIR = _LAUNCHER_DIR / "seccomp"

_SEAT_UID = 9999903


def _reexec_shim(extra_argv: list[str]) -> str:
    """Build the ``source`` fed to ``run_in_sandbox()``.

    Primes stdin to an already-open, immediate-EOF fd (no path lookup, so
    it doesn't depend on ``/dev`` visibility -- see systemd_run_shape.py
    deviation 4) then re-execs into the real launcher under ``-SEI``.
    """

    argv_literal = ", ".join(repr(a) for a in extra_argv)
    return (
        "import os, sys\n"
        "r, w = os.pipe()\n"
        "os.close(w)\n"
        "os.dup2(r, 0)\n"
        f"os.execv(sys.executable, [sys.executable, '-SEI', {str(_LAUNCHER_PATH)!r}, {argv_literal}])\n"
    )


def _build_properties(
    scratch_dir: Path,
    component_module_dir: Path | None = None,
    *,
    include_filesystem_root: bool = True,
) -> list[str]:
    return systemd_run_shape.build_properties(
        scratch_dir=scratch_dir,
        launcher_path=_LAUNCHER_PATH,
        seccomp_vendor_dir=_SECCOMP_VENDOR_DIR,
        component_module_dir=component_module_dir,
        include_filesystem_root=include_filesystem_root,
    )


def _require_temporary_filesystem() -> None:
    """Typed-SKIP unless TemporaryFileSystem= can start a unit on this host.

    Finding 5 (systemd_run_shape.py docstring): TemporaryFileSystem= fails
    with code=exited/status=226/NAMESPACE ("Failed to set up mount
    namespacing: Operation not supported") on this host class, independent
    of PrivateUsers=/PrivateMounts=. Canaries 3-5 need the filesystem-
    visibility-root mechanism this primitive provides; canaries 1-2 don't
    (see their _build_properties(..., include_filesystem_root=False) call)
    and are unaffected.
    """

    with temp_scratch_dir() as scratch:
        try:
            systemd_run_shape.probe_temporary_filesystem_support(scratch_dir=scratch)
        except systemd_run_shape.PrimitiveUnavailable as exc:
            pytest.skip(reason=f"{exc.name} unavailable: {exc.reason}")


@pytest.fixture
def require_temporary_filesystem() -> None:
    _require_temporary_filesystem()


def _run_canary(
    name: str, extra: str | None = None, *, include_filesystem_root: bool = True
) -> tuple[str, object]:
    """Runs one canary in a real sandbox; returns (outcome, RunResult)."""

    argv = [f"--canary={name}"]
    with temp_scratch_dir() as scratch:
        argv.append(str(scratch))
        if extra is not None:
            argv.append(extra)
        source = _reexec_shim(argv)
        result = run_in_sandbox(
            source,
            scratch_dir=scratch,
            properties=_build_properties(scratch, include_filesystem_root=include_filesystem_root),
            timeout=20.0,
        )
        marker = scratch / f"canary-{name}.result"
        if marker.exists():
            outcome = marker.read_text(encoding="utf-8")
        else:
            # Marker absent -- typically a signal-kill (DEC 9: systemd-run
            # --wait returns a flat 255 on signal-kill, not 128+signal;
            # evidence is stderr-only) or a crash before the canary could
            # write it. Fail-closed: treat as indeterminate, never as PASS.
            outcome = (
                f"INDETERMINATE:no marker file; rc={result.returncode} "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )
    return outcome, result


def _assert_denied(outcome: str, result: object) -> None:
    assert outcome.startswith("DENIED:"), (
        f"expected a DENIED outcome, got {outcome!r} " f"(RunResult: {result!r})"
    )


def test_c5_1_subprocess_creation_canary(require_systemd_user: None) -> None:
    # No filesystem-visibility-root needed -- subprocess denial is
    # SystemCallFilter=~@process, unaffected by finding 5.
    outcome, result = _run_canary("subprocess-creation", include_filesystem_root=False)
    _assert_denied(outcome, result)


def test_c5_2_network_canary(require_systemd_user: None) -> None:
    # No filesystem-visibility-root needed -- network denial is
    # RestrictAddressFamilies=, unaffected by finding 5.
    outcome, result = _run_canary("network", include_filesystem_root=False)
    _assert_denied(outcome, result)


def test_c5_3_host_mutation_canary(
    require_systemd_user: None, require_temporary_filesystem: None
) -> None:
    outcome, result = _run_canary("host-mutation")
    _assert_denied(outcome, result)


def test_c5_4_authority_holding_canary(
    require_systemd_user: None, require_temporary_filesystem: None
) -> None:
    """Sandbox visibility root MUST NOT expose Tranche A's coordination-
    primitive gate tree (PO-owned cross-tranche invariant, T-3295 brief).
    """

    with temp_gate_tree(seat_uids=[_SEAT_UID]) as tree:
        outcome, result = _run_canary("authority-holding", extra=str(tree.gates_dir))
    _assert_denied(outcome, result)


def test_c5_5_data_only_return(
    require_systemd_user: None, require_temporary_filesystem: None
) -> None:
    """Planner returns a list[Operation] (data only); a minimal in-test
    executor-stand-in validates known ids and refuses an unknown one --
    demonstrating the capability-shaped (not lexical) boundary. NOT wired
    to the real tools/host_control/contracts.py registry -- that's
    pre-existing production infra, out of this ref-impl's "consumes
    minimal fixtures" scope (README non-goals).
    """

    component_source = (
        "def plan_install(request):\n"
        "    return [\n"
        "        {'id': 'noop', 'payload': {}},\n"
        "        {'id': 'totally-unknown-op-id', 'payload': {'danger': True}},\n"
        "    ]\n"
    )

    with temp_scratch_dir() as component_dir, temp_scratch_dir() as scratch:
        module_path = component_dir / "canary_component.py"
        module_path.write_text(component_source, encoding="utf-8")

        source = _reexec_shim(["plan_install", str(module_path)])
        result = run_in_sandbox(
            source,
            scratch_dir=scratch,
            properties=_build_properties(scratch, component_module_dir=component_dir),
            timeout=20.0,
        )

    assert result.returncode == 0, f"launcher plan_install failed: {result!r}"

    import json

    ops = json.loads(result.stdout)
    assert isinstance(ops, list)
    assert all(isinstance(op, dict) and "id" in op for op in ops)

    # Minimal in-test executor-stand-in -- the planner's job ends at
    # returning data; actuation decisions are a SEPARATE piece of code.
    known_op_registry = {"noop"}
    accepted = [op for op in ops if op["id"] in known_op_registry]
    refused = [op for op in ops if op["id"] not in known_op_registry]

    assert [op["id"] for op in accepted] == ["noop"]
    assert [op["id"] for op in refused] == ["totally-unknown-op-id"]


def _require_nsjail() -> None:
    if shutil.which("nsjail") is None:
        pytest.skip(
            reason=(
                "nsjail unavailable: not found on PATH, and no apt "
                "candidate for this Ubuntu 24.04 host (see "
                "launcher/nsjail_profile.md for the equivalence table this "
                "leg would exercise if nsjail were installed)"
            )
        )


@pytest.fixture
def require_nsjail() -> None:
    _require_nsjail()


def test_nsjail_leg_typed_skip_when_unavailable(require_nsjail: None) -> None:
    # If this ever runs (nsjail became available on the CI/dev host), it's
    # a deliberate loud failure, not a silent pass -- a reminder to wire up
    # the live leg per nsjail_profile.md rather than let this test become
    # meaninglessly green forever.
    pytest.fail(
        "nsjail is now available on this host -- wire up the live "
        "equivalence-table leg per launcher/nsjail_profile.md instead of "
        "relying on this typed-SKIP placeholder."
    )
