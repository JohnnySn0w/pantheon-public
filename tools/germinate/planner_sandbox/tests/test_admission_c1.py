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

import contextlib
import os
import queue
import signal
import subprocess
import sys
import threading
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
    hold = sys.argv[8] if len(sys.argv) > 8 else "0"
    start_gate = len(sys.argv) > 9 and sys.argv[9] == "wait-go"

    if start_gate:
        # Start barrier: report readiness (imports done), then attempt
        # only once the test writes "go" -- lets N callers contend at once.
        print("READY", flush=True)
        sys.stdin.readline()

    try:
        with gate.acquire(system_dir, instance, component, verb, scope=scope, uid=uid) as run_id:
            if marker is not None:
                marker.write_text(run_id, encoding="utf-8")
            print(f"ADMITTED {run_id}", flush=True)
            if hold == "until-stdin":
                # Handshake hold: keep the claim until the test writes a
                # line (or closes stdin). No timing dependence.
                sys.stdin.readline()
            elif float(hold) > 0:
                time.sleep(float(hold))
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
    hold_seconds: float | str = 0.0,
    *,
    strace_log: Path | None = None,
    wait_go: bool = False,
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
        "wait-go" if wait_go else "",
    ]
    if strace_log is not None:
        args = ["strace", "-f", "-e", "trace=openat", "-o", str(strace_log), *args]
    env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT)}
    return subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


#: Upper bound on how long a caller may take to report its admission
#: outcome. A failure bound only -- never a synchronisation mechanism --
#: so it is generous enough for a heavily loaded serial CI runner.
_OUTCOME_LINE_TIMEOUT = 120.0

#: Cleanup bound for reaping a released (or killed) caller. Deliberately
#: independent of any test deadline: cleanup must still get time to run
#: after that deadline has already been blown.
_REAP_GRACE = 30.0


def _deadline_in(seconds: float) -> float:
    return time.monotonic() + seconds


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _await_outcome_line(proc: subprocess.Popen[str], deadline: float | None = None) -> str:
    """Block until ``proc`` prints its first outcome line
    (READY/ADMITTED/REFUSED/UNAVAILABLE) and return it stripped.

    This is the handshake that replaces sleep-based ordering (T-4331):
    once this returns ``ADMITTED ...``, the caller provably holds the
    claim on disk, and with ``hold_seconds="until-stdin"`` it keeps
    holding until the test releases it via stdin.

    ``deadline`` is an absolute ``time.monotonic()`` value shared across
    every wait of one test, so the bounds do not stack; ``None`` means a
    fresh ``_OUTCOME_LINE_TIMEOUT``. On timeout the caller is killed and
    reaped (``wait()``, NOT ``communicate()``, so a later
    ``_release_and_reap`` stays valid) and ``AssertionError`` is raised.
    """

    if deadline is None:
        deadline = _deadline_in(_OUTCOME_LINE_TIMEOUT)
    lines: queue.Queue[str] = queue.Queue(maxsize=1)
    stdout = proc.stdout
    assert stdout is not None
    reader = threading.Thread(target=lambda: lines.put(stdout.readline()), daemon=True)
    reader.start()
    try:
        return lines.get(timeout=_remaining(deadline)).strip()
    except queue.Empty:
        proc.kill()
        proc.wait(timeout=_REAP_GRACE)
        raise AssertionError(
            f"caller pid={proc.pid} printed no outcome line before its deadline"
        ) from None


def _release_and_reap(proc: subprocess.Popen[str]) -> None:
    """Release a holding caller via stdin and reap it. Never raises, so
    it cannot mask the assertion that led to cleanup, and a loop over
    several callers always reaches every one of them.

    Callers write almost nothing after release, so ``wait()`` without
    draining stdout/stderr cannot deadlock on a full pipe.
    """

    try:
        if proc.poll() is None and proc.stdin is not None and not proc.stdin.closed:
            try:
                proc.stdin.write("release\n")
                proc.stdin.close()
            except (OSError, ValueError):
                pass  # caller exited between poll() and write: nothing to release
        proc.wait(timeout=_REAP_GRACE)
    except Exception:
        proc.kill()
        proc.wait()
    finally:
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                with contextlib.suppress(OSError, ValueError):
                    pipe.close()


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
        inner_args, env={"PYTHONPATH": str(_REPO_ROOT)}, timeout=_OUTCOME_LINE_TIMEOUT
    )


def test_c1_1_cross_uid_concurrent_claim(require_privileged_uid_switch: None) -> None:
    """Cross-uid refusal while a claim is provably held. The native
    caller claims first and holds (handshake: its ADMITTED line); only
    then does a caller under a distinct REAL uid attempt the same
    (instance, component), and it must refuse at the admission tier
    before any mutation-tier code runs. Instrument: strace both callers,
    prove the loser's log never opens the mutation marker path.

    The name is kept for traceability; this test orders the two claims
    deterministically (T-4331). Simultaneous contention is exercised by
    ``test_c1_5_simultaneous_multiprocess_claim_single_winner``."""

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
            hold_seconds="until-stdin",
            strace_log=native_log,
        )
        # Handshake, not a sleep (T-4331): block until native provably
        # holds the claim. Under load a sleep-based head start let the two
        # callers run without overlapping -- both then admitted
        # sequentially and legitimately, and the test flaked with "both".
        native_outcome = _await_outcome_line(native_proc)
        try:
            assert native_outcome.startswith("ADMITTED"), (
                f"native caller must win the uncontended first claim, got: {native_outcome!r}"
            )
            # nobody attempts (and completes) strictly while native holds.
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
        finally:
            # Release native's hold (second half of the handshake); also
            # runs on failure so no caller is left blocked on stdin.
            _release_and_reap(native_proc)

        outcomes = {
            "native": native_outcome,
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
    """Consultant v9 BLOCK-1 witness. Caller A claims admission, execs a plan
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
            hold_seconds="until-stdin",  # holds until SIGKILL; stdin is never written
        )
        # Handshake, not a sleep (T-4331): SIGKILL only once caller A
        # provably holds the claim. A fixed sleep could fire the kill
        # before a loaded caller A had claimed, letting caller B admit.
        native_outcome = _await_outcome_line(native_proc)
        assert native_outcome.startswith("ADMITTED"), (
            f"caller A must hold the claim before the kill, got: {native_outcome!r}"
        )
        assert native_proc.poll() is None, "caller A should still be alive/holding"

        os.kill(native_proc.pid, signal.SIGKILL)
        native_proc.communicate(timeout=_OUTCOME_LINE_TIMEOUT)
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


def test_c1_5_simultaneous_multiprocess_claim_single_winner() -> None:
    """N real processes released through a start barrier attempt
    admission on the same (instance, component) at once; exactly one is
    ADMITTED. Timing-independent by construction (T-4331): the winner
    holds its claim until the test has collected EVERY contender's
    outcome, so each attempt provably overlaps a live hold -- the only
    way to see two ADMITTED lines is a genuine non-atomic claim. No
    cross-uid dependency, so this runs where C1-1 typed-skips."""

    contenders = 6
    # ONE overall bound for the whole round, shared by every wait (so the
    # per-call bounds do not stack across 2*N serial waits).
    deadline = _deadline_in(_OUTCOME_LINE_TIMEOUT)
    with temp_gate_tree() as tree, temp_scratch_dir() as scratch:
        script_path = _write_caller_script(scratch)
        bootstrap_component(tree.system_dir, "c1-5-instance", "c1-5-component")

        procs = [
            _run_native(
                script_path,
                tree.system_dir,
                "c1-5-instance",
                "c1-5-component",
                "plan",
                f"seat-{9999910 + i}",
                9999910 + i,
                hold_seconds="until-stdin",
                wait_go=True,
            )
            for i in range(contenders)
        ]
        outcomes = _run_contention_round(procs, deadline)

        admitted = [o for o in outcomes if o.startswith("ADMITTED")]
        refused = [o for o in outcomes if o.startswith("REFUSED")]
        assert len(admitted) == 1, f"expected exactly one ADMITTED, got: {outcomes}"
        assert len(refused) == contenders - 1, f"expected the rest REFUSED, got: {outcomes}"


def _run_contention_round(procs: list[subprocess.Popen[str]], deadline: float) -> list[str]:
    """Drive a start-barrier round: await every READY, send "go" to all,
    collect every outcome line, THEN release and reap every contender
    (the winner holds until all outcomes are in). Every wait draws on the
    one shared ``deadline``. Cleanup releases each contender
    independently, so a failure (e.g. one contender timing out) surfaces
    as the original AssertionError and every child is still reaped."""

    try:
        for proc in procs:
            ready = _await_outcome_line(proc, deadline)
            assert ready == "READY", f"contender failed before the start barrier: {ready!r}"
        for proc in procs:
            assert proc.stdin is not None
            proc.stdin.write("go\n")
            proc.stdin.flush()
        return [_await_outcome_line(proc, deadline) for proc in procs]
    finally:
        for proc in procs:
            _release_and_reap(proc)


def test_c1_6_contention_round_timeout_surfaces_and_reaps_all() -> None:
    """Harness self-test for the C1-5 cleanup path: one contender that
    never prints must surface as the timeout AssertionError (not a
    cleanup-time error masking it), and every contender -- including
    those still blocked on stdin -- must be reaped."""

    import pytest

    with temp_gate_tree() as tree, temp_scratch_dir() as scratch:
        script_path = _write_caller_script(scratch)
        bootstrap_component(tree.system_dir, "c1-6-instance", "c1-6-component")

        def contender(i: int) -> subprocess.Popen[str]:
            return _run_native(
                script_path,
                tree.system_dir,
                "c1-6-instance",
                "c1-6-component",
                "plan",
                f"seat-{9999920 + i}",
                9999920 + i,
                hold_seconds="until-stdin",
                wait_go=True,
            )

        silent = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        procs = [contender(0), silent, contender(1)]

        # Short deadline: the silent contender can never meet it,
        # whatever the load, so the timeout path is always taken.
        with pytest.raises(AssertionError, match="printed no outcome line"):
            _run_contention_round(procs, _deadline_in(2.0))

        assert all(proc.returncode is not None for proc in procs), (
            f"every contender must be reaped: {[proc.returncode for proc in procs]}"
        )
