"""Bounded-exit-window verification for `controller_main`'s normal exit path
(T-2970 BT5 scenario (d) -- explicit scope per T-2969's F2 REBOUND-fix
DECISIONS annotation).

NOT a pytest test module -- same convention as this package's sibling
`persona_controller_health_dbus_livebus_verify.py` (filename doesn't match
pytest's `test_*.py`/`*_test.py` default collection glob). Run it directly:

    python3 -m tools.manual.runtime_controller_restart_fixable_verify --self-test
    python3 -m tools.manual.runtime_controller_restart_fixable_verify \
        --watch-pid <controller MainPID> --bound-seconds 30 \
        [--trigger-cmd "kill -9 <runtime pane pid>"]

Why this exists: T-2969's F2 finding observed that once
`ensure_glib_mainloop_running()` has started its background GLib-mainloop
thread, a NORMAL Python interpreter shutdown (`controller-main`'s own
`raise SystemExit(main())`, reached whenever `controller_main` returns
after observing a tmux-death) does not always complete cleanly in this
environment's dbus-python/GLib stack -- observed there as a silent abort
run standalone and an indefinite hang under pytest. Which shape (if
either) the REAL systemd-managed `pantheon-runtime-controller@<persona>
.service` process exhibits was explicitly left open by T-2969 ("flagged
to HO/BT2, not fixed here -- out of this script's scope") and folded into
this ticket's scenario (d). If the real production process can HANG (not
just crash-with-a-nonzero-exit-code), that threatens the ORG-068 SS87
restart-fixable invariant even though `Restart=always` tolerates a crashy
exit fine -- systemd only respawns once the OLD MainPID actually goes
away; a hung-but-still-running process is not a failure from systemd's
point of view, so `Restart=always` would never fire at all.

Measurement discipline: elapsed time is measured against the PID's own
liveness (`os.kill(pid, 0)`), NEVER against systemd's derived
`ActiveState`/`SubState` -- those are themselves driven by the same
process-exit event this script exists to verify, so using them as the
oracle would beg the question this script is asked to answer.

Exit code 0 and "ALL CHECKS PASSED" (or "DIED WITHIN BOUND") on success;
nonzero and a printed failure otherwise.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass

DEFAULT_BOUND_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.1
# Self-test uses a short bound so `--self-test` stays fast (well under a
# second for the quick-exit case, ~2s for the deliberately-slow case) --
# it is proving the MEASUREMENT MECHANICS are correct, not reproducing the
# real dbus-python/GLib bug shape (that needs the real native stack; see
# `persona_controller_health_dbus_livebus_verify.py` for that side of it).
SELF_TEST_BOUND_SECONDS = 2.0
SELF_TEST_SLOW_SLEEP_SECONDS = 5.0


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, just not signalable by us -- still alive.
        return True
    return True


@dataclass(frozen=True)
class ExitWindowResult:
    died_within_bound: bool
    elapsed_seconds: float
    bound_seconds: float


def wait_for_pid_death(
    pid: int,
    *,
    bound_seconds: float,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    sleep=time.sleep,
    monotonic=time.monotonic,
    alive_check=None,
) -> ExitWindowResult:
    """Poll until `pid` disappears or `bound_seconds` elapses. Never blocks
    longer than `bound_seconds` -- a still-alive PID at the deadline is
    reported as `died_within_bound=False`, not waited out further (the
    whole point is a BOUNDED check; an unbounded wait would defeat it).

    `alive_check` is a test seam (same injectable-dependency convention
    `runtime_controller.block_until_tmux_death` uses for its own
    `alive_check`/`sleep`). Defaults to `pid_alive(pid)` -- the right
    oracle for an EXTERNAL process (e.g. systemd's MainPID: systemd is the
    reaping parent, not us). The self-test below overrides it to
    `proc.poll() is not None` for its own dummy children instead, because
    `os.kill(child_pid, 0)` stays true for an un-reaped ZOMBIE -- a child
    of THIS process that has already exited but hasn't been `wait()`-ed on
    yet -- which would otherwise make every self-test dummy look
    permanently alive regardless of how fast it actually exited."""
    check = alive_check or (lambda: pid_alive(pid))
    start = monotonic()
    deadline = start + bound_seconds
    while monotonic() < deadline:
        if not check():
            return ExitWindowResult(True, monotonic() - start, bound_seconds)
        sleep(poll_interval)
    alive = check()
    return ExitWindowResult(not alive, monotonic() - start, bound_seconds)


def _self_test_quick_exit() -> ExitWindowResult:
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"])
    try:
        return wait_for_pid_death(
            proc.pid, bound_seconds=SELF_TEST_BOUND_SECONDS,
            alive_check=lambda: proc.poll() is None,
        )
    finally:
        proc.wait(timeout=SELF_TEST_BOUND_SECONDS + 5)


def _self_test_slow_exit() -> ExitWindowResult:
    proc = subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({SELF_TEST_SLOW_SLEEP_SECONDS})"]
    )
    try:
        return wait_for_pid_death(
            proc.pid, bound_seconds=SELF_TEST_BOUND_SECONDS,
            alive_check=lambda: proc.poll() is None,
        )
    finally:
        # Don't leave the deliberately-slow dummy running past this check.
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=SELF_TEST_SLOW_SLEEP_SECONDS + 5)


def run_self_test() -> int:
    assert SELF_TEST_SLOW_SLEEP_SECONDS > SELF_TEST_BOUND_SECONDS, (
        "self-test's slow dummy must outlive the self-test bound, or it "
        "isn't exercising the FAIL path at all"
    )
    quick = _self_test_quick_exit()
    if not quick.died_within_bound:
        print(
            f"FAIL: quick-exit dummy did not die within {quick.bound_seconds}s "
            f"(elapsed {quick.elapsed_seconds:.3f}s) -- the checker's own "
            "measurement loop is broken (a promptly-exiting process must "
            "always be detected as dead)."
        )
        return 1
    print(
        f"PASS: quick-exit dummy correctly detected dead in "
        f"{quick.elapsed_seconds:.3f}s (bound {quick.bound_seconds}s)"
    )

    slow = _self_test_slow_exit()
    if slow.died_within_bound:
        print(
            f"FAIL: slow-exit dummy (sleeps {SELF_TEST_SLOW_SLEEP_SECONDS}s) "
            f"was reported dead within the {slow.bound_seconds}s bound -- the "
            "checker isn't actually distinguishing a hung/slow process from "
            "a prompt one."
        )
        return 1
    print(
        f"PASS: slow-exit dummy correctly reported as NOT dead within "
        f"{slow.bound_seconds}s bound (checker did not falsely pass a "
        "hung/slow process)"
    )

    print("ALL CHECKS PASSED")
    return 0


def run_live_check(pid: int, *, bound_seconds: float, trigger_cmd: str | None) -> int:
    if not pid_alive(pid):
        print(f"FAIL: pid {pid} is not alive BEFORE the trigger -- nothing to watch.")
        return 1
    if trigger_cmd:
        print(f"Triggering: {trigger_cmd}")
        subprocess.run(shlex.split(trigger_cmd), check=True)
    print(f"Watching pid {pid} for death within {bound_seconds}s (measured against os.kill(pid, 0), not systemd state)...")
    result = wait_for_pid_death(pid, bound_seconds=bound_seconds)
    if not result.died_within_bound:
        print(
            f"FAIL: pid {pid} was STILL ALIVE after the {bound_seconds}s bound "
            f"(watched {result.elapsed_seconds:.3f}s). This is the SS87 "
            "restart-fixable hazard T-2969's F2 finding flagged: a hung "
            "(not crashed) controller_main process never triggers "
            "Restart=always, because systemd never observes MainPID exit."
        )
        return 1
    print(f"DIED WITHIN BOUND: pid {pid} exited after {result.elapsed_seconds:.3f}s (bound {bound_seconds}s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="Run the synthetic self-test only.")
    parser.add_argument("--watch-pid", type=int, default=None, help="PID to watch for death.")
    parser.add_argument("--bound-seconds", type=float, default=DEFAULT_BOUND_SECONDS)
    parser.add_argument(
        "--trigger-cmd",
        default=None,
        help="Optional shell command run immediately before timing starts (e.g. 'kill -9 <pid>').",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()
    if args.watch_pid is not None:
        return run_live_check(args.watch_pid, bound_seconds=args.bound_seconds, trigger_cmd=args.trigger_cmd)
    parser.error("one of --self-test or --watch-pid is required")
    return 64


if __name__ == "__main__":
    sys.exit(main())
