#!/usr/bin/env python3
"""Run the repo's `tools/test_*.sh` suites under the normal pytest smoke loop.

T-2278. Six shell suites lived in `tools/`, were documented, and were executed
by NOTHING: the smoke loop is `python3 -m unittest tools.test_<module>` /
`pytest tools`, neither of which can collect a bash file. They fired only when a
human remembered them.

WHY THAT IS THE ARC'S OWN THESIS, not a nit. T-2245 exists because 4266 unit
tests stayed green through a 90-minute delivery outage — tests aimed one stratum
below where the failure lived. A test no runner executes is that defect taken to
its limit: **it cannot even be green.** Its firing probability is "did somebody
think to run it", which `lessons/2026-07-28-failure-presents-as-absence.md` names
as the absence of a detection strategy rather than one.

What they actually guard is not theoretical — e.g.
`test_acp_bridge_supervise_session_death.sh` kills a supervised session
underneath the supervisor and asserts the loop EXITS within 30s, because a
supervisor that outlives its dead child keeps the unit `active` and nothing ever
restarts it. That is a guard on the exact 2026-07-28 failure.

DISCOVERY IS BY GLOB, deliberately. Enumerating the six by name here would
reproduce the defect one layer up: a seventh suite would be added and silently
not run, and the list would read as coverage. `test_every_shell_suite_is_run`
below pins that the glob is non-empty, so an empty match fails loudly instead of
passing vacuously — a zero-length parametrize is otherwise indistinguishable
from "all green".

COST, measured 2026-08-02 rather than estimated: ~99s serial for all six, of
which `test_handoff_and_restart.sh` alone is ~72s. That is roughly +50% on a
~200s full run. Accepted rather than optimised away — an opt-out defaulting to
"skip the slow one" is how this becomes manual-only-by-accident again. If the
cost becomes a real problem that is a measured future decision, not a
preemptive one.

PARALLELISM: CI runs `python -m pytest tools/ -q -n auto` (T-3566), so these
suites run under xdist alongside the Python tests; that overlap is why the whole
job floor is ~the longest suite rather than the sum. (The 2026-08 note here said
"no `-n`"; that stopped being true when xdist was adopted.)

SINGLE EXECUTION PATH (T-4280): this file is the ONLY place CI runs the shell
suites. A separate `bash tools/run_shell_suites.sh` step (T-2330, which predates
this file) re-ran every suite serially after pytest, ~218s median per run on the
one pve2 runner, and was removed. `tools/test_shell_suite_coverage.py` pins that
CI collects this file exactly once and that no second path returns.

T-4230 (2026-09-29): the COST figures above are stale (handoff+restart is now
~245s idle, the seat-relaunch suite ~275s) and the total-wall cap that sat next to
them went red under host load. It is now a no-progress watchdog; see the comment
on `_IDLE_TIMEOUT_SECONDS` for the reasoning and the accepted trade-off.
"""
from __future__ import annotations

import inspect
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent


def discover_shell_suites(tools_dir: Path) -> list[Path]:
    """Every `test_*.sh` directly under `tools_dir`, sorted. By GLOB, never a list."""
    return sorted(tools_dir.glob("test_*.sh"))


SHELL_SUITES = discover_shell_suites(TOOLS)

# T-4230: NO-PROGRESS timeout, not a total wall cap. Read this before "fixing" it.
#
# The property this file asserts is "the suite is not HUNG", not "the suite is
# fast". These two suites are CPU-bound in production helpers (each restart-path
# run starts ~7 Python helpers, ~0.5s CPU apiece, mostly PyYAML parsing
# instance.yaml), so their wall time scales with host load, not with anything the
# suite controls: ~245s and ~275s idle, 600s+ at load 12-30. A total-wall cap of
# 300s therefore reported "the host was busy" as a red run (T-4230).
#
# So the cap is on SILENCE: the suite is killed (process group) only if it emits
# no output for _IDLE_TIMEOUT_SECONDS. Each big suite prints a line per case
# (~5s idle, ~50s worst measured under load 24), so 300s of silence is a real
# hang with ~6x margin. _CEILING_SECONDS bounds a runaway that keeps printing.
#
# ACCEPTED TRADE-OFF (orchestrator, T-4230 plan approval): "the suite took > 300s" is
# NO LONGER a regression signal. A suite that gets slower but keeps making
# progress passes; only "a case is silent for 300s" or "total > 1200s" fails.
# Do not restore the total cap: under load it cannot tell a hang from a busy box.
# Suites that print nothing until the end (the short ones) degrade to the old
# semantics: their silence IS their total, so 300s is still their cap.
#
# The ceiling is 1200s, not larger, so that it can fire INSIDE CI's 30-minute job
# timeout (.github/workflows/ci.yml, timeout-minutes: 30 = 1800s, and this test
# starts minutes into that job): a ceiling at or above the job timeout would never
# fire in CI, and a runaway would surface as an unattributed job timeout instead of
# a named failure here. It is still ~3.5x the slowest healthy suite measured under
# load 22. test_production_wiring_pins_the_watchdog_bounds pins both numbers.
_IDLE_TIMEOUT_SECONDS = 300
_CEILING_SECONDS = 1200


@dataclass
class SuiteResult:
    returncode: int | None  # None when the watchdog killed the suite
    output: str  # stdout + stderr, interleaved
    killed_reason: str | None  # None, or why the watchdog fired
    elapsed: float


def run_suite(
    script: Path,
    *,
    idle_timeout: float = _IDLE_TIMEOUT_SECONDS,
    ceiling: float = _CEILING_SECONDS,
    cwd: Path | None = None,
) -> SuiteResult:
    """Run `bash <script>` under the no-progress watchdog (see the constants above).

    The suite runs in its OWN session/process group so a kill takes the whole tree:
    `subprocess.run(timeout=)` killed only `bash`, leaving a hung tmux/sleep child
    alive and holding the pipe. Output (stdout+stderr, interleaved) is kept even on
    a kill, so the failure is attributable.

    Returns as soon as `bash` exits: a leaked background child (the handoff suite's
    bumper subshells) may hold the pipe open forever, so EOF is NOT awaited; whatever
    is already buffered is drained non-blockingly and the group is then reaped.
    """
    start = last_output = time.monotonic()
    proc = subprocess.Popen(
        ["bash", str(script)],
        cwd=str(cwd if cwd is not None else TOOLS.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    chunks: list[bytes] = []
    killed_reason: str | None = None
    eof = False
    try:
        while proc.poll() is None:
            now = time.monotonic()
            if now - start > ceiling:
                killed_reason = f"exceeded the {ceiling:g}s absolute ceiling (runaway)"
                break
            if now - last_output > idle_timeout:
                killed_reason = f"produced no output for {idle_timeout:g}s (no-progress timeout: hung)"
                break
            if eof:
                time.sleep(0.1)
                continue
            for _key, _events in sel.select(timeout=0.2):
                data = os.read(proc.stdout.fileno(), 65536)
                if data:
                    chunks.append(data)
                    last_output = time.monotonic()
                else:
                    eof = True
                    sel.unregister(proc.stdout)
    finally:
        # Reap the whole group. TERM first on a kill so the suite's own teardown can
        # rm its sandbox; anything still alive (or any straggler after a normal exit)
        # gets KILL.
        if killed_reason is not None:
            _signal_group(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        _signal_group(proc.pid, signal.SIGKILL)
        proc.wait()
        if not eof:
            # Take what is already buffered (the tail that attributes a kill, or the
            # last lines of a normal run) but never block on EOF: a leaked
            # background child may hold the pipe open until it is reaped above.
            while sel.select(timeout=0):
                data = os.read(proc.stdout.fileno(), 65536)
                if not data:
                    break
                chunks.append(data)
        sel.close()
        proc.stdout.close()
    return SuiteResult(
        returncode=None if killed_reason else proc.returncode,
        output=b"".join(chunks).decode("utf-8", errors="replace"),
        killed_reason=killed_reason,
        elapsed=time.monotonic() - start,
    )


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def test_every_shell_suite_is_run() -> None:
    """The glob must match something.

    Without this, deleting or renaming every `test_*.sh` would leave a
    zero-length parametrize that reports all-green — the vacuous-pass shape this
    file exists to close.
    """
    assert SHELL_SUITES, (
        "no tools/test_*.sh matched — either they were removed (update this file) "
        "or the glob broke, and a zero-length parametrize passes vacuously"
    )


@pytest.mark.parametrize("script", SHELL_SUITES, ids=lambda p: p.name)
def test_shell_suite_passes(script: Path, record_property) -> None:
    """Each `tools/test_*.sh` must exit 0.

    Output is attached to the failure rather than swallowed: a shell suite that
    fails silently under a runner is barely better than one that never runs.

    T-4280: the suite's REAL result is recorded as junit properties FIRST, before any
    judgement, so tools/check_shell_suite_outcomes.py (a CI step after pytest) can
    verify from the report that each suite ran and exited 0, whatever this function
    then decides. Keep the two `record_property` lines directly under `run_suite`.
    """
    res = run_suite(script)
    record_property("suite_returncode", res.returncode)
    record_property("suite_killed", res.killed_reason is not None)
    tail ="\n".join(res.output.splitlines()[-40:])
    if res.killed_reason is not None:
        pytest.fail(f"{script.name} {res.killed_reason}\n--- last 40 lines ---\n{tail}")
    if res.returncode != 0:
        pytest.fail(f"{script.name} exited {res.returncode}\n--- last 40 lines ---\n{tail}")


# --------------------------------------------------------------------------
# T-4230: the watchdog itself. Fake suites are tiny bash scripts; the timeouts
# are shrunk to a few seconds so this stays fast. Each test pins one behaviour a future
# "simplification" back to a total wall cap (or to `subprocess.run(timeout=)`)
# would silently lose.
# --------------------------------------------------------------------------
def _fake_suite(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake_suite.sh"
    script.write_text("#!/bin/bash\n" + body)
    return script


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# T-4412 flake C. The watchdog tests pass a SHRUNK idle_timeout (production is 300s) so
# they stay fast. Any test that expects the fake suite NOT to be killed, or to have
# finished its setup output before it is, is then racing the host: under CI load one
# fork/exec stall of ~0.7s between two echoes 0.3s apart tripped the old 1.0s bound
# (#619, run 36712410345). Measured on the seat under tools/confined_load.sh (8 burners on
# one CPU), the worst gap between two such echoes over 399 was 0.72s, i.e. 1.0s left
# ~1.4x headroom; 3.0s gives that observed worst ~4x and the nominal gap 10x. These are
# test-only bounds: the production ones are pinned separately, further down.
_WATCHDOG_TEST_IDLE_TIMEOUT = 3.0
_CHATTY_GAP_SECONDS = 0.3
# Enough echoes that the run's guaranteed wall (sleep never returns early) exceeds the
# idle timeout with margin, so "outlived the idle timeout" stays true on an idle host too.
_CHATTY_CASES = 12
_MIN_IDLE_TO_GAP_RATIO = 10


def test_watchdog_passes_through_exit_code_and_both_streams(tmp_path: Path) -> None:
    script = _fake_suite(tmp_path, "echo to-stdout\necho to-stderr >&2\nexit 3\n")
    res = run_suite(script, idle_timeout=5, ceiling=30)
    assert res.returncode == 3
    assert res.killed_reason is None
    assert "to-stdout" in res.output and "to-stderr" in res.output


def test_watchdog_lets_a_chatty_suite_outlive_the_idle_timeout(tmp_path: Path) -> None:
    """The whole point of T-4230: total wall > idle_timeout must NOT fail while the
    suite keeps making progress (this is what a busy host looks like)."""
    idle_timeout = _WATCHDOG_TEST_IDLE_TIMEOUT
    assert idle_timeout >= _MIN_IDLE_TO_GAP_RATIO * _CHATTY_GAP_SECONDS, (
        "each gap between echoes needs real headroom against idle_timeout, or a load "
        "stall of a fraction of a second reads as a hang (T-4412 flake C)"
    )
    script = _fake_suite(
        tmp_path,
        f"for i in $(seq 1 {_CHATTY_CASES}); do echo case-$i; sleep {_CHATTY_GAP_SECONDS}; done\n",
    )
    res = run_suite(script, idle_timeout=idle_timeout, ceiling=30)
    assert res.killed_reason is None and res.returncode == 0
    assert res.elapsed > idle_timeout, "test is vacuous unless the run outlives the idle timeout"
    assert f"case-{_CHATTY_CASES}" in res.output


def test_watchdog_kills_a_silent_hang_and_keeps_the_output_for_attribution(tmp_path: Path) -> None:
    script = _fake_suite(tmp_path, "echo got-this-far\nsleep 60\necho never\n")
    # Same direction as flake C: a start-up stall >= idle_timeout would kill the suite
    # before "got-this-far" is ever printed, and the attribution assert below would fail.
    res = run_suite(script, idle_timeout=_WATCHDOG_TEST_IDLE_TIMEOUT, ceiling=30)
    assert res.returncode is None
    assert res.killed_reason and "no output" in res.killed_reason
    assert "got-this-far" in res.output and "never" not in res.output
    assert res.elapsed < 15, "the hang must be cut at ~idle_timeout, not run to the sleep's end"


def test_watchdog_ceiling_bounds_a_runaway_chatty_suite(tmp_path: Path) -> None:
    # The fake "runaway" is itself BOUNDED (~12s of ticks). If the ceiling ever
    # breaks, the fake suite finishes on its own and this test FAILS on the asserts
    # below; with `while true` a broken ceiling would instead hang the whole run.
    script = _fake_suite(tmp_path, "for i in $(seq 1 120); do echo tick; sleep 0.1; done\n")
    res = run_suite(script, idle_timeout=30, ceiling=1.5)
    assert res.returncode is None, "the ceiling did not fire: the runaway ran to its own end"
    assert res.killed_reason and "ceiling" in res.killed_reason
    assert res.elapsed < 8, "the runaway must be cut at ~ceiling, not run to the fake's end"


def test_watchdog_kills_descendants_not_just_bash(tmp_path: Path) -> None:
    """The old subprocess.run(timeout=) killed only `bash`; a hung tmux/sleep child kept
    running and held the pipe. The whole process group must go."""
    pidfile = tmp_path / "child.pid"
    script = _fake_suite(tmp_path, f"sleep 60 &\necho $! > {pidfile}\necho started\nwait\n")
    # Same direction as flake C: the kill must land AFTER the script has written its
    # pidfile, so idle_timeout has to outlast a load stall in the script's set-up.
    res = run_suite(script, idle_timeout=_WATCHDOG_TEST_IDLE_TIMEOUT, ceiling=30)
    assert res.returncode is None
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while _pid_alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pid_alive(child), "descendant of a killed suite survived the watchdog"


def test_watchdog_does_not_wait_on_a_pipe_held_open_by_a_leaked_background_child(tmp_path: Path) -> None:
    """A suite whose bash exits 0 while a backgrounded child still holds stdout (the
    handoff suite's bumper subshells do this) must return promptly, not block on EOF."""
    script = _fake_suite(tmp_path, "sleep 60 &\necho done\nexit 0\n")
    res = run_suite(script, idle_timeout=30, ceiling=60)
    assert res.returncode == 0 and res.killed_reason is None
    assert res.elapsed < 15


# --------------------------------------------------------------------------
# T-4230: the production wiring. The watchdog tests above shrink the bounds to ~1s,
# so on their own they would all still pass if test_shell_suite_passes were changed
# to run suites with idle_timeout=1e9 (i.e. no watchdog). These pin the numbers the
# REAL run uses, and that it actually uses them.
# --------------------------------------------------------------------------
def _discard(name: str, value: object) -> None:
    """Stands in for pytest's `record_property` fixture where a test calls
    test_shell_suite_passes directly."""


def _bounds_used_by_test_shell_suite_passes(monkeypatch) -> tuple[float, float]:
    real = run_suite
    seen: dict = {}

    def recorder(script, **kw):
        seen.update(kw)
        return SuiteResult(returncode=0, output="", killed_reason=None, elapsed=0.0)

    monkeypatch.setattr(sys.modules[__name__], "run_suite", recorder)
    test_shell_suite_passes(Path("x.sh"), _discard)
    params = inspect.signature(real).parameters
    return (
        seen.get("idle_timeout", params["idle_timeout"].default),
        seen.get("ceiling", params["ceiling"].default),
    )


def test_production_wiring_pins_the_watchdog_bounds(monkeypatch) -> None:
    """The bounds test_shell_suite_passes really runs with must be the module's
    constants, inside a band chosen on purpose (change the band here consciously):
    - idle >= 120s: well above the longest silent gap a healthy suite shows (<= 9s
      idle, ~50-60s under load 24), so host load cannot read as a hang.
    - idle < ceiling, and 600s <= ceiling <= 1200s: above the slowest healthy suite
      under load (600s+), and inside CI's 30-minute job timeout so it can actually
      fire there and name the suite instead of surfacing as a job timeout."""
    idle, ceiling = _bounds_used_by_test_shell_suite_passes(monkeypatch)
    assert idle == _IDLE_TIMEOUT_SECONDS and ceiling == _CEILING_SECONDS
    assert 120 <= idle <= 600, f"idle timeout {idle}s is outside the intended band"
    assert 600 <= ceiling <= 1200, f"ceiling {ceiling}s is outside the intended band"
    assert idle < ceiling


def test_test_shell_suite_passes_fails_a_killed_or_nonzero_suite(monkeypatch) -> None:
    """A watchdog kill (returncode None) and a plain non-zero exit must both be red."""
    for result in (
        SuiteResult(returncode=None, output="x", killed_reason="produced no output", elapsed=0.0),
        SuiteResult(returncode=2, output="x", killed_reason=None, elapsed=0.0),
    ):
        monkeypatch.setattr(sys.modules[__name__], "run_suite", lambda script, _r=result, **kw: _r)
        with pytest.raises(pytest.fail.Exception):
            test_shell_suite_passes(Path("x.sh"), _discard)


# --------------------------------------------------------------------------
# T-4230: the fail-fast in the two big suites. The wide positive windows are free
# while the SUT is healthy and slow only when it is broken; the guard against a
# broken SUT taking ~30 minutes to go red (the CI job timeout, i.e. an unattributed
# red) is that each suite aborts after SUITE_MAX_FAILED_CASES=3 failed cases. These
# run the REAL suite files, copied next to a stub SUT that is always wrong.
# --------------------------------------------------------------------------
def _suite_with_stub_sut(tmp_path: Path, suite: str, sut: str, sut_body: str) -> Path:
    tools = tmp_path / "tools"
    tools.mkdir()
    shutil.copy2(TOOLS / suite, tools / suite)
    (tools / sut).write_text("#!/bin/bash\n" + sut_body)
    # The seat suite refuses to run unless submit_verify imports from its own dir.
    # submit_verify resolves symlinks, so it finds the real inbox_consumer.
    (tools / "submit_verify.py").symlink_to(TOOLS / "submit_verify.py")
    return tools / suite


def test_handoff_suite_aborts_after_three_failures_and_does_not_wait_out_bumpers(tmp_path: Path) -> None:
    """A stub SUT that exits 0 at once (never delivering a prompt) fails every case
    (the postchecks find no side effects). The suite must abort at 3 with the failures
    named, and quickly: each case's bumper polls 60s for a "prompt delivered" that
    never comes, and postcheck_* `wait`s on it, unless the bumper's lifetime follows
    the SUT (poll_until / $SBX/sut_done). Without that this takes ~60s per case and
    the ceiling below turns it into a clean failure instead of a hang."""
    suite = _suite_with_stub_sut(tmp_path, "test_handoff_and_restart.sh", "handoff_and_restart.sh", "exit 0\n")
    res = run_suite(suite, idle_timeout=90, ceiling=100)
    assert res.killed_reason is None, f"suite hung or ran away: {res.killed_reason}\n{res.output[-1500:]}"
    assert res.returncode == 1
    assert "aborting after 3 failures" in res.output
    assert res.output.count("  FAIL: ") == 3, res.output[-2500:]
    assert "Results so far: 0 passed, 3 failed" in res.output
    assert "Results: " not in res.output, "the suite ran to completion instead of aborting"
    assert res.elapsed < 60, "bumpers outlived the SUT and were waited out"


def test_seat_relaunch_suite_aborts_after_three_failed_cases(tmp_path: Path) -> None:
    suite = _suite_with_stub_sut(tmp_path, "test_pantheon_seat_relaunch.sh", "pantheon-seat-relaunch.sh", "exit 99\n")
    res = run_suite(suite, idle_timeout=90, ceiling=100)
    assert res.killed_reason is None, f"suite hung or ran away: {res.killed_reason}\n{res.output[-1500:]}"
    assert res.returncode == 1
    assert "aborting after 3 failures" in res.output
    # Cases 1-3 (usage guards) all fail; case 4 onward must never run.
    assert "missing required arg" in res.output and "bad consume-timer rejected" in res.output
    assert "empty captured argv fails" not in res.output, "ran past the third failed case"
    assert "ABORTED, not completed" in res.output


def test_the_suite_keeps_running_below_the_failure_threshold(tmp_path: Path) -> None:
    """One flaky case must stay one red case, not end the run (1 < 3): the stub SUT is
    right for every usage-guard case (exit 64) and wrong for the rest, so exactly the
    cases after the guards fail, and the suite only aborts once THREE of them have."""
    suite = _suite_with_stub_sut(
        tmp_path, "test_pantheon_seat_relaunch.sh", "pantheon-seat-relaunch.sh",
        'for a in "$@"; do [[ "$a" == --dry-run ]] && exit 99; done\nexit 64\n',
    )
    res = run_suite(suite, idle_timeout=90, ceiling=100)
    assert res.killed_reason is None and res.returncode == 1
    # "empty captured argv fails" (exit 2 expected, stub gives 64) is failed case #1
    # and "dry-run captures + prints plan" #2; the run continued past both.
    assert "empty captured argv fails" in res.output
    assert "dry-run captures + prints plan" in res.output
    assert "aborting after 3 failures" in res.output


# --------------------------------------------------------------------------
# T-4280: discovery is what keeps a suite from silently dropping out. CI used to
# run the shell suites a second time through tools/run_shell_suites.sh, whose own
# glob was an accidental second net under this one. That step is gone (this file is
# the single execution path), so the net is pinned here BEHAVIOURALLY: the tests
# below fail for a hardcoded list, a narrowed glob, or a parametrize that binds
# fewer suites than exist, none of which a regex over this file's source would
# reliably see (T-2330: a regex satisfiable by a comment is not a guard).
# --------------------------------------------------------------------------
def test_discovery_finds_every_test_sh_and_only_those(tmp_path: Path) -> None:
    for name in ("test_a.sh", "test_b.sh", "helper.sh", "test_c.py", "notatest_d.sh", "test_e.sh.bak"):
        (tmp_path / name).write_text("")
    assert [p.name for p in discover_shell_suites(tmp_path)] == ["test_a.sh", "test_b.sh"]


def test_discovery_picks_up_a_suite_added_later_without_any_list_edit(tmp_path: Path) -> None:
    (tmp_path / "test_a.sh").write_text("")
    before = [p.name for p in discover_shell_suites(tmp_path)]
    (tmp_path / "test_brand_new.sh").write_text("")
    after = [p.name for p in discover_shell_suites(tmp_path)]
    assert before == ["test_a.sh"]
    assert after == ["test_a.sh", "test_brand_new.sh"]


def test_live_discovery_matches_an_independent_enumeration_of_tools() -> None:
    independent = sorted(
        e.name for e in os.scandir(TOOLS)
        if e.is_file() and e.name.startswith("test_") and e.name.endswith(".sh")
    )
    assert independent, "no tools/test_*.sh exist at all"
    assert [p.name for p in SHELL_SUITES] == independent


def test_every_discovered_suite_is_bound_into_the_parametrized_test() -> None:
    """A parametrize over a slice or a copied list would leave a suite discovered but
    never executed; compare what the decorator actually binds."""
    marks = [m for m in test_shell_suite_passes.pytestmark if m.name == "parametrize"]
    assert len(marks) == 1
    argnames, argvalues = marks[0].args[0], list(marks[0].args[1])
    assert argnames == "script"
    assert argvalues == SHELL_SUITES


# --------------------------------------------------------------------------
# T-4280: the wrapper records the suite's REAL result as junit properties, so
# tools/check_shell_suite_outcomes.py can judge the suite itself instead of trusting
# this function's verdict. The record must happen right after run_suite returns and
# BEFORE any pass/fail decision: a weakened criterion or an early return in here can
# then not change what the report says the suite did.
# --------------------------------------------------------------------------
def _run_recording(monkeypatch, result: SuiteResult) -> tuple[list[tuple[str, object]], type | None]:
    bag: list[tuple[str, object]] = []
    monkeypatch.setattr(sys.modules[__name__], "run_suite", lambda script, **kw: result)
    raised: type | None = None
    try:
        test_shell_suite_passes(Path("x.sh"), lambda name, value: bag.append((name, value)))
    except BaseException as exc:  # noqa: BLE001 - the point is what was recorded before it
        raised = type(exc)
    return bag, raised


def test_the_real_result_is_recorded_for_a_passing_suite(monkeypatch) -> None:
    bag, raised = _run_recording(monkeypatch, SuiteResult(returncode=0, output="", killed_reason=None, elapsed=0.0))
    assert raised is None
    assert bag == [("suite_returncode", 0), ("suite_killed", False)]


def test_the_real_result_is_recorded_before_a_failure_is_raised(monkeypatch) -> None:
    for result in (
        SuiteResult(returncode=3, output="boom", killed_reason=None, elapsed=0.0),
        SuiteResult(returncode=None, output="hung", killed_reason="produced no output", elapsed=0.0),
    ):
        bag, raised = _run_recording(monkeypatch, result)
        assert raised is pytest.fail.Exception, "a bad result must still fail the test"
        assert bag == [("suite_returncode", result.returncode), ("suite_killed", result.killed_reason is not None)]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
