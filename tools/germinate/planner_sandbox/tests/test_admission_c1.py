"""C1 (component-wide single-run serialization) red-before-green tests.

Real primitives only -- real filesystem claims, real subprocesses, real
``strace``, real ``sudo``-mediated uid switch. No mocks: a test that
cannot reach a real primitive typed-SKIPs via ``require_privileged_uid_switch``
(see ``conftest.py``) rather than substituting a fake.

See ``tools/germinate/planner_sandbox/admission/coordinator.py`` and
T-3292 DECISIONS for the coordination-primitive pick (filesystem-atomic
link-claim, surviving caller-process exit by construction) and the
cross-uid mechanism pick (``sudo -u nobody`` -- a genuine ``setuid()``
kuid switch, empirically distinguished from the cosmetic bare
``unshare -U`` display-only uid, which was tested and rejected).
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from tools.germinate.planner_sandbox.admission import bootstrap_component
from tools.germinate.planner_sandbox.harness.fixtures import temp_gate_tree, temp_scratch_dir

from .conftest import PRIVILEGED_SWITCH_UID, run_as_privileged_switch_uid

_REPO_ROOT = Path(__file__).resolve().parents[4]

#: Self-contained caller script: attempts admission for one
#: (instance, component) and prints a single machine-parseable line
#: (ADMITTED/REFUSED/UNAVAILABLE) so the test can assert outcomes across
#: a real process boundary without parsing exceptions cross-process.
_CALLER_SOURCE = """\
import sys
import time
from pathlib import Path

from tools.germinate.planner_sandbox.admission import gate
from tools.germinate.planner_sandbox.admission.exceptions import (
    AdmissionRefused,
    PrimitiveUnavailable,
)


def main() -> int:
    system_dir = Path(sys.argv[1])
    instance, component, verb, scope = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
    uid = int(sys.argv[6])
    marker = Path(sys.argv[7]) if len(sys.argv) > 7 and sys.argv[7] else None
    hold_seconds = float(sys.argv[8]) if len(sys.argv) > 8 else 0.0

    try:
        with gate.acquire(system_dir, instance, component, verb, scope=scope, uid=uid) as run_id:
            print(f"ADMITTED {run_id}", flush=True)
            if marker is not None:
                marker.write_text(run_id, encoding="utf-8")
            if hold_seconds > 0:
                time.sleep(hold_seconds)
    except AdmissionRefused as exc:
        print(f"REFUSED {exc.reason}", flush=True)
    except PrimitiveUnavailable as exc:
        print(f"UNAVAILABLE {exc.name}: {exc.reason}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _stage_support_dir(scratch: Path) -> Path:
    """Sticky (0o1777) test-support dir: caller script + strace logs +
    the mutation marker all need to be reachable/writable by BOTH the
    native uid and the sudo-switched uid. This is deliberately separate
    from any production-shape gate-tree path -- pure test plumbing, no
    ADR mode-scheme claim. As the directory's owner, the native test
    process can clean up the other uid's files inside it afterwards
    (verified empirically: sticky-bit unlink permits the directory
    owner, not just the file owner)."""

    scratch.chmod(0o1777)
    return scratch


def _write_caller_script(support_dir: Path) -> Path:
    script_path = support_dir / "caller.py"
    script_path.write_text(_CALLER_SOURCE, encoding="utf-8")
    script_path.chmod(0o644)
    return script_path


def _run_native(
    script_path: Path,
    system_dir: Path,
    instance: str,
    component: str,
    verb: str,
    scope: str,
    uid: int,
    marker: Path | None = None,
    hold_seconds: float = 0.0,
    *,
    strace_log: Path | None = None,
) -> subprocess.Popen[str]:
    args = [
        "python3",
        "-u",
        str(script_path),
        str(system_dir),
        instance,
        component,
        verb,
        scope,
        str(uid),
        str(marker) if marker is not None else "",
        str(hold_seconds),
    ]
    if strace_log is not None:
        args = ["strace", "-f", "-e", "trace=openat", "-o", str(strace_log), *args]
    env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT)}
    return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)


def _run_privileged_switch(
    script_path: Path,
    system_dir: Path,
    instance: str,
    component: str,
    verb: str,
    scope: str,
    uid: int,
    marker: Path | None = None,
    hold_seconds: float = 0.0,
    *,
    strace_log: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    inner_args = [
        "python3",
        "-u",
        str(script_path),
        str(system_dir),
        instance,
        component,
        verb,
        scope,
        str(uid),
        str(marker) if marker is not None else "",
        str(hold_seconds),
    ]
    if strace_log is not None:
        inner_args = ["strace", "-f", "-e", "trace=openat", "-o", str(strace_log), *inner_args]
    return run_as_privileged_switch_uid(
        inner_args, env={"PYTHONPATH": str(_REPO_ROOT)}, timeout=20.0
    )


def test_c1_1_cross_uid_concurrent_claim(require_privileged_uid_switch: None) -> None:
    """Two processes under distinct REAL uids attempt admission on the
    same (instance, component); exactly one succeeds, the other refuses
    at the admission tier before any mutation-tier code runs. Instrument:
    strace both callers, prove the loser's log never opens the mutation
    marker path."""

    with temp_gate_tree() as tree, temp_scratch_dir() as scratch:
        support = _stage_support_dir(scratch)
        script_path = _write_caller_script(support)
        bootstrap_component(tree.system_dir, "c1-1-instance", "c1-1-component")

        marker = support / "mutation-occurred.marker"
        native_log = support / "native.strace.log"
        nobody_log = support / "nobody.strace.log"

        native_proc = _run_native(
            script_path,
            tree.system_dir,
            "c1-1-instance",
            "c1-1-component",
            "plan",
            "system",
            1000,
            marker=marker,
            hold_seconds=1.0,
            strace_log=native_log,
        )
        time.sleep(0.3)  # let native win the race deterministically for this pairing
        nobody_result = _run_privileged_switch(
            script_path,
            tree.system_dir,
            "c1-1-instance",
            "c1-1-component",
            "plan",
            "seat-nobody",
            PRIVILEGED_SWITCH_UID,
            marker=marker,
            hold_seconds=0.0,
            strace_log=nobody_log,
        )
        native_stdout, native_stderr = native_proc.communicate(timeout=10)

        outcomes = {
            "native": native_stdout.strip(),
            "nobody": nobody_result.stdout.strip(),
        }
        admitted = {k: v for k, v in outcomes.items() if v.startswith("ADMITTED")}
        refused = {k: v for k, v in outcomes.items() if v.startswith("REFUSED")}

        assert len(admitted) == 1, f"expected exactly one ADMITTED, got: {outcomes}"
        assert len(refused) == 1, f"expected exactly one REFUSED, got: {outcomes}"
        assert marker.exists(), "winner must have written the mutation marker"

        loser = next(iter(refused))
        loser_log = native_log if loser == "native" else nobody_log
        assert loser_log.exists(), f"expected a strace log for the loser ({loser})"
        log_text = loser_log.read_text(encoding="utf-8", errors="replace")
        assert str(marker) not in log_text, (
            f"loser ({loser}) strace log contains the mutation-marker path -- "
            "a mutation-tier open() happened before/despite refusal"
        )

        # dir-owner cleanup of any nobody-owned files inside the sticky
        # support dir (empirically verified: sticky-bit unlink permits
        # the directory owner, not just the file owner).


def test_c1_2_caller_exit_persistence_survives_sigkill(require_privileged_uid_switch: None) -> None:
    """Cairn v9 BLOCK-1 witness. Caller A claims admission, execs a plan
    phase, then is SIGKILLed from outside -- no chance to clean up.
    Caller B under a distinct real uid attempts admission afterward and
    MUST still refuse, because the claim (a durable file) is the
    mechanism-of-record, not a live flock held by caller A's process."""

    with temp_gate_tree() as tree, temp_scratch_dir() as scratch:
        support = _stage_support_dir(scratch)
        script_path = _write_caller_script(support)
        bootstrap_component(tree.system_dir, "c1-2-instance", "c1-2-component")

        native_proc = _run_native(
            script_path,
            tree.system_dir,
            "c1-2-instance",
            "c1-2-component",
            "plan",
            "system",
            1000,
            hold_seconds=30.0,  # long enough that only SIGKILL ends it
        )
        time.sleep(0.5)
        assert native_proc.poll() is None, "caller A should still be alive/holding"

        os.kill(native_proc.pid, signal.SIGKILL)
        native_proc.wait(timeout=5)
        assert native_proc.returncode is not None and native_proc.returncode < 0, (
            "caller A must have died by signal, not by a graceful path"
        )

        nobody_result = _run_privileged_switch(
            script_path,
            tree.system_dir,
            "c1-2-instance",
            "c1-2-component",
            "plan",
            "seat-nobody",
            PRIVILEGED_SWITCH_UID,
        )
        assert nobody_result.stdout.strip().startswith("REFUSED"), (
            f"expected REFUSED after caller A's SIGKILL, got: {nobody_result.stdout!r} "
            f"stderr={nobody_result.stderr!r}"
        )


def test_c1_3_primitive_unreachable_fail_closed() -> None:
    """Remove the primitive underneath a live engine invocation; the
    next admission attempt refuses with a clearly-typed error, not a
    permit. No cross-uid dependency -- pure filesystem manipulation."""

    import pytest

    from tools.germinate.planner_sandbox.admission import gate
    from tools.germinate.planner_sandbox.admission.exceptions import PrimitiveUnavailable

    with temp_gate_tree() as tree:
        bootstrap_component(tree.system_dir, "c1-3-instance", "c1-3-component")

        with gate.acquire(
            tree.system_dir, "c1-3-instance", "c1-3-component", "plan", scope="system", uid=1000
        ):
            pass  # admit-then-release cleanly first, to prove the primitive worked at all

        # Corrupt the primitive: remove the leaf directory bootstrap created.
        leaf = tree.system_dir / "c1-3-instance" / "c1-3-component"
        leaf.rmdir()

        with pytest.raises(PrimitiveUnavailable) as exc_info:
            with gate.acquire(
                tree.system_dir,
                "c1-3-instance",
                "c1-3-component",
                "plan",
                scope="system",
                uid=1000,
            ):
                pass
        assert exc_info.value.name, "PrimitiveUnavailable must carry a primitive name"
        assert "c1-3-component" in exc_info.value.reason or "leaf" in exc_info.value.reason.lower()


def test_c1_4_n_scope_enumeration() -> None:
    """With system-scope + seat-<uid1> + seat-<uid2> all having gates in
    the tree, admission for (instance, component) is single-run across
    all three -- regardless of which of the three scope paths initiates
    the conflicting attempt. This is a gate-tree TOPOLOGY test (the
    README's own framing: "proves the actual invariant regardless of
    scope count"), not a 3-concurrent-real-uid test -- C1-1/C1-2 already
    carry the real cross-uid-contention burden."""

    from tools.germinate.planner_sandbox.admission import gate
    from tools.germinate.planner_sandbox.admission.exceptions import AdmissionRefused

    seat_uid_1, seat_uid_2 = 9999901, 9999902
    with temp_gate_tree(seat_uids=[seat_uid_1, seat_uid_2]) as tree:
        # Prove all three scope directories genuinely exist in the tree
        # (the topology this test is actually about).
        assert tree.system_dir.is_dir()
        assert tree.seat_dir(seat_uid_1).is_dir()
        assert tree.seat_dir(seat_uid_2).is_dir()

        bootstrap_component(tree.system_dir, "c1-4-instance", "c1-4-component")

        with gate.acquire(
            tree.system_dir,
            "c1-4-instance",
            "c1-4-component",
            "plan",
            scope=f"seat-{seat_uid_1}",
            uid=seat_uid_1,
        ):
            for scope, uid in (
                ("system", 0),
                (f"seat-{seat_uid_2}", seat_uid_2),
            ):
                try:
                    with gate.acquire(
                        tree.system_dir,
                        "c1-4-instance",
                        "c1-4-component",
                        "plan",
                        scope=scope,
                        uid=uid,
                    ):
                        raise AssertionError(
                            f"admission must refuse from scope={scope} while "
                            f"seat-{seat_uid_1} holds the claim"
                        )
                except AdmissionRefused as exc:
                    assert exc.scope == scope
