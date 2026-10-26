"""T-2917 5c.1 parity proofs for the typed user-manager port.

(a) GOLDEN LEGACY PARITY. The pre-5c.1 text parsers and classifiers are copied
VERBATIM below from origin/main 9e8fa21fe (``git show
9e8fa21fe:tools/persona_installer.py``) as ``_legacy_*`` reference
implementations. A table-driven corpus of ``systemctl show`` stdout (every
literal the tools/test_persona_installer.py fakes return, plus edge cases) is
driven through both the legacy references and the new path (the
persona_installer legacy-shaped wrappers AND the typed path:
``_systemctl_snapshot_from_stdout`` + tools.user_systemd classifiers), and the
test asserts identical classifications, identical exception type and message,
and identical evidence dicts (including key order). Crash parity (a legacy
ValueError on a unicode digit) is asserted too.

(b) CROSS-TRANSPORT EQUIVALENCE. For a state matrix, the same state is built
both as ``systemctl show`` text and as native typed values (as a D-Bus layer
delivers them), and the resulting UnitSnapshots must be equal apart from
``source``, with identical classifier verdicts.

These encodings are our MODEL of systemd: the native values and the text
renderings here are what we believe systemd emits. Agreement with a REAL
systemd (both over the bus and on the command line) is a live item for
T-2924; nothing here touches a real manager.
"""

from __future__ import annotations

import dataclasses
import unittest
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping

from tools import persona_installer as pi
from tools import user_systemd as us
from tools.persona_installer import OperationFailed, _sha256_bytes
from tools.user_systemd import ReadPurpose

TEST_INVOCATION_ID = "a" * 32
VALID_ID = "0123456789abcdef0123456789abcdef"


# ===========================================================================
# Legacy reference implementations, VERBATIM from 9e8fa21fe (renamed only)
# ===========================================================================

_LEGACY_STALE_UNIT_FAILURE_RESET_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "ExecMainStatus",
    "InvocationID",
    "Job",
)


@dataclass(frozen=True)
class _LegacyTimerUnitState:
    classification: Literal["enabled", "disabled", "not-found"]
    load_state: str
    unit_file_state: str
    active_state: str
    sub_state: str


@dataclass(frozen=True)
class _LegacyStaleUnitFailureResetSnapshot:
    load_state: str
    active_state: str
    sub_state: str
    result: str
    exec_main_status: str
    invocation_id: str
    job: str
    stdout_length: int
    stdout_sha256: str


def _legacy_parse_stale_unit_failure_reset_show(stdout: str) -> _LegacyStaleUnitFailureResetSnapshot:
    values: dict[str, str] = {}
    allowed = set(_LEGACY_STALE_UNIT_FAILURE_RESET_PROPERTIES)
    for line in stdout.splitlines():
        if not line.strip():
            continue
        if line.count("=") != 1:
            raise OperationFailed("stale unit show output is malformed")
        key, value = line.split("=", 1)
        if key not in allowed:
            raise OperationFailed("stale unit show output contains unexpected keys")
        if key in values:
            raise OperationFailed("stale unit show output contains duplicate keys")
        values[key] = value
    if set(values) != allowed:
        raise OperationFailed("stale unit show output missing required keys")
    raw = stdout.encode("utf-8")
    return _LegacyStaleUnitFailureResetSnapshot(
        load_state=values["LoadState"],
        active_state=values["ActiveState"],
        sub_state=values["SubState"],
        result=values["Result"],
        exec_main_status=values["ExecMainStatus"],
        invocation_id=values["InvocationID"],
        job=values["Job"],
        stdout_length=len(raw),
        stdout_sha256=_sha256_bytes(raw),
    )


def _legacy_canonical_positive_decimal(value: str) -> bool:
    return value.isdigit() and value != "0" and str(int(value)) == value


def _legacy_classify_stale_unit_failure_reset_snapshot(
    snapshot: _LegacyStaleUnitFailureResetSnapshot,
) -> Literal["clean-noop", "reset-required"]:
    if (
        snapshot.load_state == "not-found"
        and snapshot.active_state == "inactive"
        and snapshot.sub_state == "dead"
        and snapshot.result == "success"
        and snapshot.exec_main_status == "0"
        and snapshot.invocation_id == ""
        and snapshot.job == ""
    ):
        return "clean-noop"
    if snapshot.load_state == "loaded":
        raise OperationFailed("pre-existing loaded service")
    if (
        snapshot.load_state == "not-found"
        and snapshot.active_state == "failed"
        and snapshot.sub_state == "failed"
        and snapshot.result == "exit-code"
        and _legacy_canonical_positive_decimal(snapshot.exec_main_status)
        and _legacy_valid_systemd_invocation_id(snapshot.invocation_id)
        and snapshot.job == ""
    ):
        return "reset-required"
    raise OperationFailed("unsupported stale unit state")


def _legacy_unit_fire_invocation_digest(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8")) if value else ""


def _legacy_stale_unit_failure_reset_snapshot_evidence(
    prefix: str,
    snapshot: _LegacyStaleUnitFailureResetSnapshot,
) -> dict[str, str]:
    invocation_state = "present" if snapshot.invocation_id else "empty"
    return {
        f"{prefix}_load_state": snapshot.load_state,
        f"{prefix}_active_state": snapshot.active_state,
        f"{prefix}_sub_state": snapshot.sub_state,
        f"{prefix}_result": snapshot.result,
        f"{prefix}_exec_main_status": snapshot.exec_main_status,
        f"{prefix}_invocation_id_state": invocation_state,
        f"{prefix}_invocation_id_valid": str(
            _legacy_valid_systemd_invocation_id(snapshot.invocation_id)
        ).lower(),
        f"{prefix}_invocation_id_sha256": _legacy_unit_fire_invocation_digest(
            snapshot.invocation_id
        ),
        f"{prefix}_job": snapshot.job,
        f"{prefix}_stdout_length": str(snapshot.stdout_length),
        f"{prefix}_stdout_sha256": snapshot.stdout_sha256,
    }


def _legacy_parse_systemctl_timer_show(stdout: str) -> _LegacyTimerUnitState:
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        if line == "":
            continue
        if line.count("=") != 1:
            raise OperationFailed("timer show output is malformed")
        key, value = line.split("=", 1)
        if key not in {"LoadState", "UnitFileState", "ActiveState", "SubState"}:
            raise OperationFailed("timer show output contains unexpected keys")
        if key in values:
            raise OperationFailed("timer show output contains duplicate keys")
        values[key] = value
    if set(values) != {"LoadState", "UnitFileState", "ActiveState", "SubState"}:
        raise OperationFailed("timer show output missing required keys")
    load_state = values["LoadState"]
    unit_file_state = values["UnitFileState"]
    active_state = values["ActiveState"]
    sub_state = values["SubState"]
    if load_state == "loaded" and unit_file_state in {"enabled", "disabled"}:
        classification: Literal["enabled", "disabled"] = (
            "enabled" if unit_file_state == "enabled" else "disabled"
        )
        return _LegacyTimerUnitState(
            classification=classification,
            load_state=load_state,
            unit_file_state=unit_file_state,
            active_state=active_state,
            sub_state=sub_state,
        )
    if load_state == "not-found" and unit_file_state in {"", "not-found"}:
        return _LegacyTimerUnitState(
            classification="not-found",
            load_state=load_state,
            unit_file_state=unit_file_state,
            active_state=active_state,
            sub_state=sub_state,
        )
    raise OperationFailed("timer show output has unsupported load/unit-file state")


def _legacy_unit_inactive_dead(unit: str, stdout: str) -> _LegacyTimerUnitState:
    # LocalHostBackend._unit_inactive_dead's text parse + classification
    # (after its returncode check), verbatim with `result.stdout` -> `stdout`.
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        if line == "":
            continue
        if line.count("=") != 1:
            raise OperationFailed(f"{unit} show output is malformed")
        key, value = line.split("=", 1)
        if key not in {"LoadState", "UnitFileState", "ActiveState", "SubState"}:
            raise OperationFailed(f"{unit} show output contains unexpected keys")
        if key in values:
            raise OperationFailed(f"{unit} show output contains duplicate keys")
        values[key] = value
    if set(values) != {"LoadState", "UnitFileState", "ActiveState", "SubState"}:
        raise OperationFailed(f"{unit} show output missing required keys")
    classification: Literal["enabled", "disabled", "not-found"]
    if values["LoadState"] == "not-found":
        classification = "not-found"
    else:
        classification = "enabled" if values["UnitFileState"] == "enabled" else "disabled"
    return _LegacyTimerUnitState(
        classification=classification,
        load_state=values["LoadState"],
        unit_file_state=values["UnitFileState"],
        active_state=values["ActiveState"],
        sub_state=values["SubState"],
    )


def _legacy_prove_inactive_dead(state: _LegacyTimerUnitState) -> bool:
    # _prove_unit_inactive_dead's predicate (negated raise condition)
    return not (state.active_state != "inactive" or state.sub_state != "dead")


def _legacy_prove_notfound_inactive_dead(state: _LegacyTimerUnitState) -> bool:
    # _prove_unit_notfound_inactive_dead's predicate (negated raise condition)
    return not (
        state.load_state != "not-found"
        or state.unit_file_state not in {"", "not-found"}
        or state.active_state != "inactive"
        or state.sub_state != "dead"
    )


def _legacy_finite_next_elapse_basis(values: Mapping[str, str]) -> str | None:
    """Return the systemd clock basis with a finite scheduled next elapse."""

    invalid = {"", "0", "n/a", "infinity"}
    for basis, key in (
        ("realtime", "NextElapseUSecRealtime"),
        ("monotonic", "NextElapseUSecMonotonic"),
    ):
        if values.get(key, "").strip().lower() not in invalid:
            return basis
    return None


def _legacy_exec_status_within_success_policy(exec_main_status: str, success_exit_status: str) -> bool:
    try:
        code = int(exec_main_status)
    except (TypeError, ValueError):
        return False
    if code == 0:
        return True
    allowed: set[int] = set()
    for token in (success_exit_status or "").split():
        try:
            allowed.add(int(token))
        except ValueError:
            continue  # signal name (SIGxxx) -- not an exit code, skip
    return code in allowed


_LEGACY_CONVERGENCE_SERVICE_IN_FLIGHT_STATES = frozenset({"activating", "active", "deactivating"})


def _legacy_convergence_service_in_flight(values: Mapping[str, str]) -> bool:
    if values.get("ActiveState", "") in _LEGACY_CONVERGENCE_SERVICE_IN_FLIGHT_STATES:
        return True
    return bool(values.get("Job", "").strip())


def _legacy_valid_systemd_invocation_id(value: str) -> bool:
    return len(value) == 32 and value.islower() and all(item in "0123456789abcdef" for item in value)


@dataclass(frozen=True)
class _LegacyUnitFirePollSnapshot:
    active_state: str
    substate: str
    result: str
    exec_main_status: str
    success_exit_status: str
    invocation_id: str
    invocation_id_valid: bool
    job: str
    stdout_length: int
    stdout_sha256: str


def _legacy_unit_fire_show_parse(stdout: str) -> _LegacyUnitFirePollSnapshot:
    # LocalHostBackend._unit_fire_show after its returncode check, verbatim
    # (stdout already normalised with `result.stdout or ""`).
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        if line.count("=") != 1:
            raise OperationFailed("unit-fire show output is malformed")
        key, value = line.split("=", 1)
        if key in values:
            raise OperationFailed("unit-fire show output contains duplicate keys")
        if key in {"InvocationID", "ActiveState", "SubState", "Result", "ExecMainStatus", "SuccessExitStatus", "Job"}:
            values[key] = value.strip()
    for required in ("InvocationID", "ActiveState", "Result", "ExecMainStatus"):
        if required not in values:
            raise OperationFailed("unit-fire show output missing required keys")
    invocation_id = values.get("InvocationID", "")
    return _LegacyUnitFirePollSnapshot(
        active_state=values.get("ActiveState", ""),
        substate=values.get("SubState", ""),
        result=values.get("Result", ""),
        exec_main_status=values.get("ExecMainStatus", ""),
        success_exit_status=values.get("SuccessExitStatus", ""),
        invocation_id=invocation_id,
        invocation_id_valid=_legacy_valid_systemd_invocation_id(invocation_id),
        job=values.get("Job", ""),
        stdout_length=len(stdout.encode("utf-8")),
        stdout_sha256=_sha256_bytes(stdout.encode("utf-8")),
    )


def _legacy_unit_fire_snapshot_has_failed(snapshot: Any) -> bool:
    return (
        snapshot.active_state == "failed"
        or (snapshot.result not in {"", "success"})
        or (
            snapshot.exec_main_status != ""
            and not _legacy_exec_status_within_success_policy(
                snapshot.exec_main_status, snapshot.success_exit_status
            )
        )
    )


def _legacy_unit_fire_pre_enqueue_idle(snapshot: Any) -> bool:
    # _prove_unit_fire_pre_enqueue_idle's predicate (negated raise condition)
    job = snapshot.job.strip()
    no_job = job in {"", "0", "n/a", "none", "None"}
    return not (snapshot.active_state not in {"inactive", "failed"} or snapshot.invocation_id or not no_job)


def _legacy_systemd_show_parse(stdout: str) -> dict[str, str]:
    # LocalHostBackend._systemd_show after its returncode check, verbatim.
    values = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _legacy_receiver_idle(values: Mapping[str, str]) -> bool:
    # codex-telegram-disabled-first: negated "is active before cutover" test
    active_state = values.get("ActiveState", "")
    main_pid = values.get("MainPID", "")
    return not (active_state not in {"inactive", "failed"} or main_pid not in {"", "0"})


# ===========================================================================
# Corpus
# ===========================================================================


def _stale_show(
    *,
    load_state: str = "not-found",
    active_state: str = "failed",
    sub_state: str = "failed",
    result: str = "exit-code",
    exec_main_status: str = "1",
    invocation_id: str = TEST_INVOCATION_ID,
    job: str = "",
) -> str:
    # mirrors tools/test_persona_installer.py stale_unit_failed_show()
    return (
        f"LoadState={load_state}\n"
        f"ActiveState={active_state}\n"
        f"SubState={sub_state}\n"
        f"Result={result}\n"
        f"ExecMainStatus={exec_main_status}\n"
        f"InvocationID={invocation_id}\n"
        f"Job={job}\n"
    )


STALE_CLEAN = "LoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nInvocationID=\nJob=\n"

# Literals extracted from tools/test_persona_installer.py fakes (f-strings
# resolved with the values those fakes use).
FAKE_LITERALS = [
    "ActiveState=active\nSubState=waiting\nNextElapseUSecRealtime=Sat 2026-08-15 04:00:00 UTC\nNextElapseUSecMonotonic=12345\n",
    "ActiveState=inactive\nMainPID=0\n",
    "ActiveState=inactive\nResult=success\nExecMainStatus=0\n",
    "ActiveState=active\n",
    "ActiveState=inactive\n",
    "ActiveState=inactive\nSubState=dead\n",
    "ActiveState=active\nSubState=waiting\n",
    "ActiveState=active\nSubState=running\nNextElapseUSecRealtime=\nNextElapseUSecMonotonic=infinity\n",
    "ActiveState=active\nSubState=waiting\nNextElapseUSecRealtime=n/a\nNextElapseUSecMonotonic=0\n",
    "ActiveState=inactive\nSubState=x\nMainPID=0\n",
    "ActiveState=active\nSubState=running\nMainPID=2415630\n",
    "InvocationID=../../not-an-id\nActiveState=activating\nSubState=start\nResult=success\nExecMainStatus=0\nJob=1\n",
    "InvocationID=BAD-RAW-INVOCATION\nActiveState=activating\nSubState=start\nResult=success\nExecMainStatus=0\nJob=1\n",
    "InvocationID=BAD=RAW\nActiveState=activating\nSubState=start\nResult=success\nExecMainStatus=0\nJob=1\n",
    "InvocationID=BAD\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
    "InvocationID=\nActiveState=active\nSubState=running\nResult=success\nExecMainStatus=7\nJob=1\n",
    "InvocationID=\nActiveState=failed\nSubState=failed\nResult=failed\nExecMainStatus=1\nJob=\n",
    "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=99\n",
    "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
    f"InvocationID={'A' * 32}\nActiveState=activating\nSubState=start\nResult=success\nExecMainStatus=0\nJob=1\n",
    f"InvocationID={'b' * 32}\nActiveState=inactive\nResult=success\nExecMainStatus=0\n",
    f"InvocationID={'b' * 32}\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
    f"InvocationID={TEST_INVOCATION_ID}\n",
    f"InvocationID={TEST_INVOCATION_ID}\nActiveState=activating\nSubState=start\nResult=success\nExecMainStatus=0\nJob=1\n",
    f"InvocationID={TEST_INVOCATION_ID}\nActiveState=active\nSubState=running\nResult=success\nExecMainStatus=0\nJob=1\n",
    "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=5\nSuccessExitStatus=3 4 5\nJob=\n",
    "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=1\nSuccessExitStatus=3 4 5\nJob=\n",
    "LoadState loaded\nUnitFileState=disabled\nActiveState=inactive\nSubState=dead\n",
    "LoadState= not-found\n",
    "LoadState=loaded\nLoadState=loaded\nUnitFileState=disabled\nActiveState=inactive\nSubState=dead\n",
    "LoadState=loaded\nUnitFileState=disabled\nActiveState=inactive\n",
    "LoadState=loaded\nUnitFileState=disabled\nActiveState=inactive\nSubState=dead\n",
    "LoadState=loaded\nUnitFileState=enabled\n",
    "LoadState=loaded\nUnitFileState=enabled\nActiveState=active\nSubState=waiting\n",
    "LoadState=loaded\nUnitFileState=enabled\nActiveState=inactive\nSubState=dead\n",
    "LoadState=loaded\nUnitFileState=enabled\nActiveState=inactive\nSubState=dead\nUnexpected=value\n",
    "LoadState=loaded\nUnitFileState=generated\nActiveState=inactive\nSubState=dead\n",
    "LoadState=loaded\nUnitFileState=masked\nActiveState=inactive\nSubState=dead\n",
    "LoadState=loaded\nUnitFileState=static\nActiveState=active\nSubState=running\n",
    "LoadState=loaded\nUnitFileState=static\nActiveState=inactive\nSubState=dead\n",
    "LoadState=not-found\n",
    "LoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nInvocationID=\n",
    STALE_CLEAN,
    "LoadState=not-found\nUnitFileState=not-found\nActiveState=active\nSubState=running\n",
    "LoadState=not-found\nUnitFileState=not-found\nActiveState=inactive\n",
    "LoadState=not-found\nUnitFileState=not-found\nActiveState=inactive\nSubState=dead\n",
    "LoadState=not-found\nUnitFileState=\nActiveState=inactive\nSubState=dead\n",
    "LoadState=unknown\nUnitFileState=disabled\nActiveState=inactive\nSubState=dead\n",
    # stale_unit_failed_show()/stale_unit_clean_show() cases
    _stale_show(),
    _stale_show(load_state="loaded"),
    _stale_show(active_state="active", sub_state="running", result="success", exec_main_status="0"),
    _stale_show(active_state="activating", sub_state="start", result="success", exec_main_status="0"),
    _stale_show(active_state="inactive", sub_state="dead", result="success", exec_main_status="0", invocation_id="", job="42"),
    STALE_CLEAN + "Job=\n",
    STALE_CLEAN + "MainPID=0\n",
    _stale_show(invocation_id="A" * 32),
    _stale_show(result="timeout"),
    _stale_show(exec_main_status="+1"),
    _stale_show(exec_main_status="0"),
    STALE_CLEAN.replace("LoadState=not-found\n", "LoadState= not-found\n"),
    _stale_show(invocation_id=f"{TEST_INVOCATION_ID} "),
    STALE_CLEAN.replace("Job=\n", "Job= \n"),
    _stale_show(exec_main_status="1 "),
]

NONCANONICAL_DECIMALS = ["007", "+5", " 5", "1_0", "٥", "²", "", "0", "00", "5", "-1", "12345", "1 "]
JOBS = ["", "0", "5", " 1", "n/a", "none", "None", "00", "x"]
INVOCATIONS = ["", VALID_ID, TEST_INVOCATION_ID, "A" * 32, "0" * 32, "BAD", VALID_ID + " ", "g" * 32]
POLICIES = ["", "3 4 5", "3 4 5 SIGTERM", "SIGTERM 3", "03 4", "SIGTERM", " 3", "3  4", "1_0", "5 ٥"]
ELAPSES = ["", "0", "n/a", "N/A", "infinity", " Infinity ", "12345", "Sat 2026-08-15 04:00:00 UTC", " 2min 5s ", "00"]
PIDS = ["0", "", "123", "00", " 0", "x"]


def _edge_corpus() -> list[str]:
    out: list[str] = [
        "",
        "\n",
        "   \n\t\n",
        "garbage",
        "A=B=C\n",
        "=\n",
        "Unexpected=1\n",
    ]
    for value in NONCANONICAL_DECIMALS:
        out.append(_stale_show(exec_main_status=value))
        out.append(STALE_CLEAN.replace("ExecMainStatus=0", f"ExecMainStatus={value}"))
        for policy in ("", "3 4 5", "SIGTERM 3"):
            out.append(
                f"InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\n"
                f"ExecMainStatus={value}\nSuccessExitStatus={policy}\nJob=\n"
            )
            out.append(
                f"InvocationID=\nActiveState=inactive\nResult=success\n"
                f"ExecMainStatus={value}\nSuccessExitStatus={policy}\nJob=\n"
            )
    for job in JOBS:
        out.append(_stale_show(job=job))
        out.append(STALE_CLEAN.replace("Job=\n", f"Job={job}\n"))
        out.append(
            f"InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob={job}\n"
        )
        out.append(f"InvocationID=\nActiveState=inactive\nResult=success\nExecMainStatus=0\nSuccessExitStatus=\nJob={job}\n")
    for invocation in INVOCATIONS:
        out.append(_stale_show(invocation_id=invocation))
        out.append(
            f"InvocationID={invocation}\nActiveState=activating\nSubState=start\nResult=success\nExecMainStatus=0\nJob=1\n"
        )
    for policy in POLICIES:
        for status in ("0", "3", "5", "10", "1"):
            out.append(
                f"InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\n"
                f"ExecMainStatus={status}\nSuccessExitStatus={policy}\nJob=\n"
            )
            out.append(
                f"InvocationID={VALID_ID}\nActiveState=inactive\nResult=success\n"
                f"ExecMainStatus={status}\nSuccessExitStatus={policy}\n"
            )
    for realtime in ELAPSES:
        for monotonic in ("", "infinity", "12345", "n/a"):
            out.append(
                f"ActiveState=active\nSubState=waiting\n"
                f"NextElapseUSecRealtime={realtime}\nNextElapseUSecMonotonic={monotonic}\n"
            )
    out.append("ActiveState=active\nSubState=waiting\nNextElapseUSecRealtime=12345\n")
    out.append("ActiveState=active\nSubState=waiting\n")
    for pid in PIDS:
        for active in ("inactive", "failed", "active", ""):
            out.append(f"ActiveState={active}\nMainPID={pid}\n")
    out.append("ActiveState=inactive\n")
    out.append("MainPID=0\n")
    # duplicate / unexpected / missing keys across families
    out.append("InvocationID=\nInvocationID=\nActiveState=inactive\nResult=success\nExecMainStatus=0\n")
    out.append("InvocationID=\nActiveState=inactive\nResult=success\nExecMainStatus=0\nOther=1\nOther=2\n")
    out.append("InvocationID=\nActiveState=inactive\nResult=success\n")
    out.append("ActiveState=inactive\nActiveState=active\nJob=\n")
    out.append("LoadState=loaded\nUnitFileState=enabled\nActiveState=inactive\nSubState=dead\nSubState=dead\n")
    out.append("LoadState=loaded\nUnitFileState=enabled\nActiveState=inactive\n\nSubState=dead\n")
    out.append("LoadState=loaded\nUnitFileState=enabled\nActiveState=inactive\n \nSubState=dead\n")
    out.append("ActiveState=inactive\r\nResult=success\r\n")
    # 5c.1 review fixup 1: digit strings past the 20-digit canonical bound,
    # including past Python's 4300-digit int/str limit, for every numeric
    # property (ExecMainStatus, Job, MainPID, SuccessExitStatus, NextElapse*)
    for digits in ("1" * 20, "1" * 21, "9" * 30, "1" * 4300, "1" * 4301, "5" * 5000):
        out.append(_stale_show(exec_main_status=digits))
        out.append(_stale_show(job=digits))
        out.append(
            f"InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\n"
            f"ExecMainStatus={digits}\nSuccessExitStatus=3 {digits}\nJob={digits}\n"
        )
        out.append(
            f"InvocationID=\nActiveState=inactive\nResult=success\n"
            f"ExecMainStatus=5\nSuccessExitStatus={digits} 5\nJob={digits}\n"
        )
        out.append(f"ActiveState=inactive\nMainPID={digits}\n")
        out.append(
            f"ActiveState=active\nSubState=waiting\n"
            f"NextElapseUSecRealtime={digits}\nNextElapseUSecMonotonic={digits}\n"
        )
    return out


CORPUS: list[str] = list(dict.fromkeys(FAKE_LITERALS + _edge_corpus()))


# ===========================================================================
# Helpers
# ===========================================================================


def _outcome(fn: Callable[[], Any]) -> tuple:
    try:
        return ("ok", fn())
    except Exception as exc:  # noqa: BLE001 - parity of the exact exception is the point
        return ("raise", type(exc), str(exc))


def _typed(unit: str, purpose: ReadPurpose, stdout: str, subject: str) -> us.UnitSnapshot:
    """The typed read path, with the ProtocolError mapped to the historical
    OperationFailed text the call site raises."""

    try:
        return pi._systemctl_snapshot_from_stdout(unit, purpose, stdout)
    except us.UserManagerProtocolError as exc:
        raise OperationFailed(f"{subject} show output {exc.reason}") from None


def _as_operation_failed(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except us.UnitStateRefused as exc:
        raise OperationFailed(str(exc)) from None


def _astuple_outcome(outcome: tuple) -> tuple:
    if outcome[0] == "ok" and dataclasses.is_dataclass(outcome[1]):
        return ("ok", dataclasses.astuple(outcome[1]))
    return outcome


# ===========================================================================
# (a) golden legacy parity
# ===========================================================================


class GoldenLegacyParityTests(unittest.TestCase):
    def test_corpus_is_substantial(self) -> None:
        self.assertGreater(len(CORPUS), 300)

    def test_stale_reset_parity(self) -> None:
        for stdout in CORPUS:
            with self.subTest(stdout=stdout):
                legacy_parse = _outcome(lambda: _legacy_parse_stale_unit_failure_reset_show(stdout))
                wrapper_parse = _outcome(lambda: pi._parse_stale_unit_failure_reset_show(stdout))
                self.assertEqual(_astuple_outcome(wrapper_parse), _astuple_outcome(legacy_parse))
                typed_parse = _outcome(lambda: _typed("unit.service", ReadPurpose.STALE_RESET, stdout, "stale unit"))
                if legacy_parse[0] == "raise":
                    self.assertEqual(typed_parse, legacy_parse)
                    continue
                self.assertEqual(typed_parse[0], "ok", typed_parse)
                legacy_snap = legacy_parse[1]
                snap = typed_parse[1]
                # evidence (pre/post) identical, including key order
                for prefix in ("pre", "post"):
                    self.assertEqual(
                        list(pi._stale_unit_failure_reset_snapshot_evidence(prefix, snap).items()),
                        list(_legacy_stale_unit_failure_reset_snapshot_evidence(prefix, legacy_snap).items()),
                    )
                legacy_cls = _outcome(lambda: _legacy_classify_stale_unit_failure_reset_snapshot(legacy_snap))
                self.assertEqual(_outcome(lambda: pi._classify_stale_unit_failure_reset_snapshot(snap)), legacy_cls)
                self.assertEqual(
                    _outcome(lambda: _as_operation_failed(lambda: us.classify_stale_reset(snap))),
                    legacy_cls,
                )

    def test_stale_reset_crash_parity_on_unicode_superscript(self) -> None:
        stdout = _stale_show(exec_main_status="²")
        legacy = _outcome(
            lambda: _legacy_classify_stale_unit_failure_reset_snapshot(
                _legacy_parse_stale_unit_failure_reset_show(stdout)
            )
        )
        self.assertEqual(legacy[:2], ("raise", ValueError))
        snap = pi._systemctl_snapshot_from_stdout("u", ReadPurpose.STALE_RESET, stdout)
        self.assertEqual(_outcome(lambda: pi._classify_stale_unit_failure_reset_snapshot(snap)), legacy)

    def test_timer_and_inactive_dead_parity(self) -> None:
        unit = "pantheon-persona-consume@orchestrator.service"
        for stdout in CORPUS:
            with self.subTest(stdout=stdout):
                legacy = _outcome(lambda: _legacy_parse_systemctl_timer_show(stdout))
                self.assertEqual(
                    _astuple_outcome(_outcome(lambda: pi._parse_systemctl_timer_show(stdout))),
                    _astuple_outcome(legacy),
                )
                typed_timer = _outcome(
                    lambda: pi._timer_unit_state_from_snapshot(
                        _typed("t.timer", ReadPurpose.UNIT_FILE_ACTIVE, stdout, "timer")
                    )
                )
                self.assertEqual(_astuple_outcome(typed_timer), _astuple_outcome(legacy))

                legacy_dead = _outcome(lambda: _legacy_unit_inactive_dead(unit, stdout))
                typed_snap = _outcome(lambda: _typed(unit, ReadPurpose.UNIT_FILE_ACTIVE, stdout, unit))
                if legacy_dead[0] == "raise":
                    self.assertEqual(typed_snap, legacy_dead)
                    continue
                snap = typed_snap[1]
                state = pi.LocalHostBackend._unit_presence_state(snap)
                self.assertEqual(dataclasses.astuple(state), dataclasses.astuple(legacy_dead[1]))
                self.assertEqual(us.proves_inactive_dead(snap), _legacy_prove_inactive_dead(legacy_dead[1]))
                self.assertEqual(
                    us.proves_notfound_inactive_dead(snap),
                    _legacy_prove_notfound_inactive_dead(legacy_dead[1]),
                )

    def test_unit_fire_poll_parity(self) -> None:
        for stdout in CORPUS:
            with self.subTest(stdout=stdout):
                legacy = _outcome(lambda: _legacy_unit_fire_show_parse(stdout))
                typed = _outcome(lambda: _typed("s.service", ReadPurpose.UNIT_FIRE_POLL, stdout, "unit-fire"))
                if legacy[0] == "raise":
                    self.assertEqual(typed, legacy)
                    continue
                self.assertEqual(typed[0], "ok", typed)
                snap = typed[1]
                legacy_snap = legacy[1]
                view = pi._unit_fire_poll_view(snap)
                self.assertEqual(dataclasses.astuple(view), dataclasses.astuple(legacy_snap))
                # diagnostics evidence (stdout_* keys) identical
                for snapshot_arg in (view, None):
                    new_diag = pi._unit_fire_bounded_diagnostics(
                        phase="capture",
                        classification="x",
                        snapshot=snapshot_arg,
                        start_count=1,
                        cleanup_state="pending",
                    )
                    legacy_diag = pi._unit_fire_bounded_diagnostics(
                        phase="capture",
                        classification="x",
                        snapshot=pi.UnitFirePollSnapshot(*dataclasses.astuple(legacy_snap)) if snapshot_arg else None,
                        start_count=1,
                        cleanup_state="pending",
                    )
                    self.assertEqual(list(new_diag.items()), list(legacy_diag.items()))
                legacy_failed = _outcome(lambda: _legacy_unit_fire_snapshot_has_failed(legacy_snap))
                self.assertEqual(_outcome(lambda: pi._unit_fire_snapshot_has_failed(view)), legacy_failed)
                self.assertEqual(_outcome(lambda: us.unit_fire_has_failed(snap)), legacy_failed)
                self.assertEqual(
                    us.unit_fire_idle_before_enqueue(snap),
                    _legacy_unit_fire_pre_enqueue_idle(legacy_snap),
                )
                self.assertEqual(
                    us.unit_fire_idle_before_enqueue(pi._unit_fire_poll_typed(view)),
                    _legacy_unit_fire_pre_enqueue_idle(legacy_snap),
                )
                self.assertEqual(snap.invocation_id_valid, legacy_snap.invocation_id_valid)
                self.assertEqual(
                    _outcome(lambda: pi._exec_status_within_success_policy(view.exec_main_status, view.success_exit_status)),
                    _outcome(
                        lambda: _legacy_exec_status_within_success_policy(
                            legacy_snap.exec_main_status, legacy_snap.success_exit_status
                        )
                    ),
                )

    def test_lenient_reads_parity(self) -> None:
        for stdout in CORPUS:
            legacy_values = _legacy_systemd_show_parse(stdout)
            with self.subTest(stdout=stdout):
                # SERVICE_SETTLE / SERVICE_OBSERVE: in-flight, success gate, evidence
                for purpose in (ReadPurpose.SERVICE_SETTLE, ReadPurpose.SERVICE_OBSERVE):
                    snap = pi._systemctl_snapshot_from_stdout("s.service", purpose, stdout)
                    if purpose is ReadPurpose.SERVICE_SETTLE:
                        self.assertEqual(
                            us.snapshot_service_in_flight(snap),
                            _legacy_convergence_service_in_flight(legacy_values),
                        )
                    legacy_gate = (
                        legacy_values.get("ActiveState") != "inactive"
                        or legacy_values.get("Result") != "success"
                        or not _legacy_exec_status_within_success_policy(
                            legacy_values.get("ExecMainStatus", ""),
                            legacy_values.get("SuccessExitStatus", ""),
                        )
                    )
                    new_gate = (
                        snap.active_state != "inactive"
                        or snap.result != "success"
                        or not us.exit_status_within_policy(snap.exec_main_status, snap.success_exit)
                    )
                    self.assertEqual(new_gate, legacy_gate)
                    self.assertEqual(
                        [
                            snap.invocation_id or "",
                            snap.active_state or "",
                            snap.result or "",
                            us.render_exec_status(snap.exec_main_status),
                            us.render_success_exit(snap.success_exit),
                        ],
                        [
                            legacy_values.get("InvocationID", ""),
                            legacy_values.get("ActiveState", ""),
                            legacy_values.get("Result", ""),
                            legacy_values.get("ExecMainStatus", ""),
                            legacy_values.get("SuccessExitStatus", ""),
                        ],
                    )
                # TIMER_ELAPSE
                timer = pi._systemctl_snapshot_from_stdout("t.timer", ReadPurpose.TIMER_ELAPSE, stdout)
                self.assertEqual(
                    (timer.active_state != "active" or timer.sub_state != "waiting"),
                    (legacy_values.get("ActiveState") != "active" or legacy_values.get("SubState") != "waiting"),
                )
                self.assertEqual(
                    us.snapshot_finite_next_elapse_basis(timer),
                    _legacy_finite_next_elapse_basis(legacy_values),
                )
                # RECEIVER_ACTIVE
                receiver = pi._systemctl_snapshot_from_stdout("r.service", ReadPurpose.RECEIVER_ACTIVE, stdout)
                self.assertEqual(
                    us.receiver_idle(receiver.active_state, receiver.main_pid),
                    _legacy_receiver_idle(legacy_values),
                )
                self.assertEqual(
                    [receiver.active_state or "", us.render_main_pid(receiver.main_pid) or "0"],
                    [legacy_values.get("ActiveState", ""), legacy_values.get("MainPID", "") or "0"],
                )

    def test_mapping_wrappers_parity(self) -> None:
        for realtime in ELAPSES + [None]:
            for monotonic in ELAPSES + [None]:
                values = {}
                if realtime is not None:
                    values["NextElapseUSecRealtime"] = realtime
                if monotonic is not None:
                    values["NextElapseUSecMonotonic"] = monotonic
                with self.subTest(values=values):
                    self.assertEqual(pi._finite_next_elapse_basis(values), _legacy_finite_next_elapse_basis(values))
        for status in NONCANONICAL_DECIMALS + ["3", "10", "garbage", None]:
            for policy in POLICIES + [None]:
                with self.subTest(status=status, policy=policy):
                    self.assertEqual(
                        _outcome(lambda: pi._exec_status_within_success_policy(status, policy)),
                        _outcome(lambda: _legacy_exec_status_within_success_policy(status, policy)),
                    )
        for active in ("", "inactive", "activating", "active", "deactivating", "failed", None):
            for job in JOBS + [None]:
                values = {}
                if active is not None:
                    values["ActiveState"] = active
                if job is not None:
                    values["Job"] = job
                with self.subTest(values=values):
                    self.assertEqual(
                        pi._legacy_convergence_service_in_flight(values),
                        _legacy_convergence_service_in_flight(values),
                    )
        for invocation in INVOCATIONS + ["", "ABCDEF" + "a" * 26, "a" * 31, "a" * 33]:
            self.assertEqual(
                pi._valid_systemd_invocation_id(invocation),
                _legacy_valid_systemd_invocation_id(invocation),
                invocation,
            )

    def test_unit_fire_snapshot_wrapper_parity_on_constructed_views(self) -> None:
        for active in ("inactive", "failed", "active"):
            for result in ("", "success", "exit-code"):
                for status in NONCANONICAL_DECIMALS:
                    for policy in POLICIES:
                        for job in ("", "0", "5", " 1"):
                            view = pi.UnitFirePollSnapshot(
                                active_state=active,
                                substate="dead",
                                result=result,
                                exec_main_status=status,
                                success_exit_status=policy,
                                invocation_id="",
                                invocation_id_valid=False,
                                job=job,
                                stdout_length=0,
                                stdout_sha256="",
                            )
                            self.assertEqual(
                                _outcome(lambda: pi._unit_fire_snapshot_has_failed(view)),
                                _outcome(lambda: _legacy_unit_fire_snapshot_has_failed(view)),
                                view,
                            )
                            self.assertEqual(
                                us.unit_fire_idle_before_enqueue(pi._unit_fire_poll_typed(view)),
                                _legacy_unit_fire_pre_enqueue_idle(view),
                                view,
                            )


# ===========================================================================
# (b) cross-transport equivalence
# ===========================================================================

JOB_PATH = "/org/freedesktop/systemd1/job/{}"


def _native_job(job_id: int) -> tuple[int, str]:
    return (job_id, JOB_PATH.format(job_id) if job_id else "/")


def _native_invocation(hex_id: str) -> bytes:
    return bytes.fromhex(hex_id) if hex_id else b""


def _text(values: Mapping[str, str]) -> str:
    return "".join(f"{key}={value}\n" for key, value in values.items())


def _pair(purpose: ReadPurpose, text_values: Mapping[str, str], native_values: Mapping[str, object]):
    unit = "pantheon-persona-consume@orchestrator.service"
    text_snap = pi._systemctl_snapshot_from_stdout(unit, purpose, _text(text_values))
    native_snap = us.unit_snapshot_from_native(unit, purpose, native_values, transport="dbus")
    return text_snap, native_snap


class ServiceBringupObserveParityTests(unittest.TestCase):
    """T-5452: SERVICE_BRINGUP_OBSERVE text and native encodings agree, and the
    strict text read refuses what the retired private parse refused."""

    def test_text_and_native_agree(self) -> None:
        purpose = ReadPurpose.SERVICE_BRINGUP_OBSERVE
        text = {"ActiveState": "active", "SubState": "running", "MainPID": "4242", "NRestarts": "2"}
        native = {"ActiveState": "active", "SubState": "running", "MainPID": 4242, "NRestarts": 2}
        text_snap, native_snap = _pair(purpose, text, native)
        self.assertEqual(dataclasses.replace(text_snap, source=native_snap.source), native_snap)

    def test_strict_text_read_accepts_and_refuses_the_frozen_corpus(self) -> None:
        # T-5517: the retired private parse is deleted; its corpus is frozen here as
        # expected outcomes of the single strict parser (None = refused).
        purpose = ReadPurpose.SERVICE_BRINGUP_OBSERVE
        good = "ActiveState=active\nSubState=running\nMainPID=4242\nNRestarts=0\n"
        ok = {"ActiveState": "active", "SubState": "running", "MainPID": "4242", "NRestarts": "0"}
        corpus = [
            (good, ok),
            (good.replace("NRestarts=0", "NRestarts=12"), {**ok, "NRestarts": "12"}),
            ("ActiveState=active\nSubState=running\nMainPID=4242\n", None),
            (good + "NRestarts=0\n", None),
            (good + "Result=success\n", None),
            (good.replace("4242", "abc"), None),
            (good.replace("4242", "04242"), None),
            (good.replace("NRestarts=0", "NRestarts="), None),
            ("", None),
        ]
        for stdout, expected in corpus:
            with self.subTest(stdout=stdout):
                try:
                    snap = pi._systemctl_snapshot_from_stdout("c.service", purpose, stdout)
                    typed = pi._controller_values_from_snapshot(snap)
                except (us.UserManagerProtocolError, pi.OperationFailed):
                    typed = None
                self.assertEqual(typed, expected)

    def test_native_nrestarts_is_u_and_main_pid_is_u(self) -> None:
        # D-Bus type contract: both are uint32 on the Service interface. user_bus imports
        # dbus lazily, so its table is always checked; the fake imports dbus at module
        # level, so its half runs only where dbus-python is installed (not CI's tools job).
        import importlib.util

        from tools.host_control.executor import user_bus

        self.assertEqual(user_bus.PROPERTY_INTERFACES["NRestarts"], user_bus.SERVICE_INTERFACE)
        self.assertEqual(user_bus.PROPERTY_INTERFACES["NRestarts"], user_bus.PROPERTY_INTERFACES["MainPID"])
        if importlib.util.find_spec("dbus") is None:
            return
        from tools import _host_control_fake_systemd1 as fake

        self.assertEqual(fake.PROPERTY_INTERFACES["NRestarts"], fake.SERVICE_IFACE)
        self.assertEqual(fake.NOT_FOUND_PROPS["NRestarts"], 0)
        self.assertIs(type(fake.typed("NRestarts", 3)), type(fake.typed("MainPID", 3)))
        self.assertEqual(type(fake.typed("NRestarts", 3)).__name__, "UInt32")


class CrossTransportEquivalenceTests(unittest.TestCase):
    """Our MODEL of systemd's two encodings must agree (live check: T-2924)."""

    def assertEquivalent(self, text_snap: us.UnitSnapshot, native_snap: us.UnitSnapshot) -> None:
        self.assertEqual(dataclasses.replace(text_snap, source=native_snap.source), native_snap)
        self.assertEqual(text_snap.source.encoding, pi.SYSTEMCTL_SHOW_STDOUT)
        self.assertEqual(native_snap.source.encoding, us.TYPED_CANONICAL_JSON)
        # a typed digest is never labelled as stdout
        self.assertNotIn("x_stdout_sha256", us.snapshot_digest_evidence("x", native_snap.source))
        self.assertIn("x_stdout_sha256", us.snapshot_digest_evidence("x", text_snap.source))

    def _verdicts(self, snap: us.UnitSnapshot) -> dict[str, object]:
        purpose = snap.purpose
        out: dict[str, object] = {}
        if purpose is ReadPurpose.STALE_RESET:
            out["stale"] = _outcome(lambda: us.classify_stale_reset(snap))
        if purpose is ReadPurpose.UNIT_FILE_ACTIVE:
            out["timer"] = _outcome(lambda: us.classify_timer_unit_file(snap))
            out["presence"] = us.classify_unit_file_presence(snap)
            out["dead"] = us.proves_inactive_dead(snap)
            out["notfound_dead"] = us.proves_notfound_inactive_dead(snap)
        if purpose in (ReadPurpose.UNIT_FIRE_POLL, ReadPurpose.SERVICE_SETTLE, ReadPurpose.SERVICE_OBSERVE):
            out["failed"] = us.unit_fire_has_failed(snap)
            out["policy"] = us.exit_status_within_policy(snap.exec_main_status, snap.success_exit)
            out["valid_id"] = snap.invocation_id_valid
            out["in_flight"] = us.snapshot_service_in_flight(snap)
        if purpose is ReadPurpose.UNIT_FIRE_POLL:
            out["idle"] = us.unit_fire_idle_before_enqueue(snap)
        if purpose is ReadPurpose.TIMER_ELAPSE:
            out["basis"] = us.snapshot_finite_next_elapse_basis(snap)
        return out

    def check(self, purpose, text_values, native_values) -> tuple[us.UnitSnapshot, us.UnitSnapshot]:
        text_snap, native_snap = _pair(purpose, text_values, native_values)
        self.assertEquivalent(text_snap, native_snap)
        self.assertEqual(self._verdicts(text_snap), self._verdicts(native_snap))
        return text_snap, native_snap

    def test_stale_matrix(self) -> None:
        cases = {
            "clean-noop": ("not-found", "inactive", "dead", "success", 0, "", 0),
            "reset-required": ("not-found", "failed", "failed", "exit-code", 1, VALID_ID, 0),
            "loaded": ("loaded", "failed", "failed", "exit-code", 1, VALID_ID, 0),
            "unsupported-job": ("not-found", "inactive", "dead", "success", 0, "", 42),
            "unsupported-status": ("not-found", "failed", "failed", "exit-code", 0, VALID_ID, 0),
        }
        expected = {
            "clean-noop": ("ok", "clean-noop"),
            "reset-required": ("ok", "reset-required"),
            "loaded": ("raise", us.UnitStateRefused, us.STALE_LOADED_REFUSAL),
            "unsupported-job": ("raise", us.UnitStateRefused, us.STALE_UNSUPPORTED_REFUSAL),
            "unsupported-status": ("raise", us.UnitStateRefused, us.STALE_UNSUPPORTED_REFUSAL),
        }
        for label, (load, active, sub, result, status, invocation, job) in cases.items():
            with self.subTest(label=label):
                text = {
                    "LoadState": load,
                    "ActiveState": active,
                    "SubState": sub,
                    "Result": result,
                    "ExecMainStatus": str(status),
                    "InvocationID": invocation,
                    "Job": str(job) if job else "",
                }
                native = {
                    "LoadState": load,
                    "ActiveState": active,
                    "SubState": sub,
                    "Result": result,
                    "ExecMainStatus": status,
                    "InvocationID": _native_invocation(invocation),
                    "Job": _native_job(job),
                }
                text_snap, _ = self.check(ReadPurpose.STALE_RESET, text, native)
                self.assertEqual(_outcome(lambda: us.classify_stale_reset(text_snap)), expected[label])
        # 16 zero bytes is "no invocation" on the bus
        _, zero = _pair(
            ReadPurpose.STALE_RESET,
            {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead", "Result": "success",
             "ExecMainStatus": "0", "InvocationID": "", "Job": ""},
            {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead", "Result": "success",
             "ExecMainStatus": 0, "InvocationID": b"\x00" * 16, "Job": (0, "/")},
        )
        self.assertEqual(zero.invocation_id, "")
        self.assertEqual(us.classify_stale_reset(zero), "clean-noop")

    def test_timer_matrix(self) -> None:
        cases = {
            "enabled": ("loaded", "enabled", "active", "waiting"),
            "disabled": ("loaded", "disabled", "inactive", "dead"),
            "not-found": ("not-found", "", "inactive", "dead"),
            "not-found-file": ("not-found", "not-found", "inactive", "dead"),
            "unsupported": ("loaded", "static", "inactive", "dead"),
        }
        for label, (load, ufs, active, sub) in cases.items():
            with self.subTest(label=label):
                values = {"LoadState": load, "UnitFileState": ufs, "ActiveState": active, "SubState": sub}
                self.check(ReadPurpose.UNIT_FILE_ACTIVE, values, dict(values))

    def test_unit_fire_poll_matrix(self) -> None:
        cases = [
            ("exit-0", VALID_ID, "inactive", "dead", "success", 0, ([], []), "", 0),
            ("in-policy-5", VALID_ID, "inactive", "dead", "success", 5, ([3, 4, 5], []), "3 4 5", 0),
            ("out-of-policy-1", "", "inactive", "dead", "success", 1, ([3, 4, 5], []), "3 4 5", 0),
            ("signal-policy", "", "inactive", "dead", "success", 0, ([3, 4, 5], [15]), "3 4 5 SIGTERM", 0),
            ("signal-exit", "", "failed", "failed", "signal", 15, ([], []), "", 0),
            ("activating-job", VALID_ID, "activating", "start", "success", 0, ([], []), "", 7),
        ]
        for label, invocation, active, sub, result, status, native_policy, text_policy, job in cases:
            with self.subTest(label=label):
                text = {
                    "InvocationID": invocation,
                    "ActiveState": active,
                    "SubState": sub,
                    "Result": result,
                    "ExecMainStatus": str(status),
                    "SuccessExitStatus": text_policy,
                    "Job": str(job) if job else "",
                }
                native = {
                    "InvocationID": _native_invocation(invocation),
                    "ActiveState": active,
                    "SubState": sub,
                    "Result": result,
                    "ExecMainStatus": status,
                    "SuccessExitStatus": native_policy,
                    "Job": _native_job(job),
                }
                self.check(ReadPurpose.UNIT_FIRE_POLL, text, native)

    def test_unit_fire_malformed_invocation(self) -> None:
        text_snap = pi._systemctl_snapshot_from_stdout(
            "s.service",
            ReadPurpose.UNIT_FIRE_POLL,
            "InvocationID=BAD\nActiveState=activating\nResult=success\nExecMainStatus=0\n",
        )
        self.assertEqual(text_snap.invocation_id, "BAD")
        self.assertFalse(text_snap.invocation_id_valid)
        for raw in (b"\x01" * 5, b"\x01" * 17, "not-bytes"):
            with self.subTest(raw=raw), self.assertRaises(us.UserManagerProtocolError):
                us.unit_snapshot_from_native(
                    "s.service",
                    ReadPurpose.UNIT_FIRE_POLL,
                    {"InvocationID": raw, "ActiveState": "activating", "Result": "success", "ExecMainStatus": 0},
                    transport="dbus",
                )

    def test_settle_in_flight_matrix(self) -> None:
        for active in ("inactive", "activating", "active", "deactivating", "failed"):
            for job in (0, 3):
                with self.subTest(active=active, job=job):
                    text = {
                        "InvocationID": "",
                        "ActiveState": active,
                        "Result": "success",
                        "ExecMainStatus": "0",
                        "SuccessExitStatus": "",
                        "Job": str(job) if job else "",
                    }
                    native = {
                        "InvocationID": b"",
                        "ActiveState": active,
                        "Result": "success",
                        "ExecMainStatus": 0,
                        "SuccessExitStatus": ([], []),
                        "Job": _native_job(job),
                    }
                    text_snap, _ = self.check(ReadPurpose.SERVICE_SETTLE, text, native)
                    self.assertEqual(
                        us.snapshot_service_in_flight(text_snap),
                        active in {"activating", "active", "deactivating"} or job != 0,
                    )

    def test_next_elapse_sentinels(self) -> None:
        sentinels = [("0", 0, "unset"), ("infinity", 2**64 - 1, "infinity"), ("12345", 12345, "finite")]
        for rt_text, rt_native, rt_kind in sentinels:
            for mono_text, mono_native, mono_kind in sentinels:
                with self.subTest(realtime=rt_text, monotonic=mono_text):
                    text = {
                        "ActiveState": "active",
                        "SubState": "waiting",
                        "NextElapseUSecRealtime": rt_text,
                        "NextElapseUSecMonotonic": mono_text,
                    }
                    native = {
                        "ActiveState": "active",
                        "SubState": "waiting",
                        "NextElapseUSecRealtime": rt_native,
                        "NextElapseUSecMonotonic": mono_native,
                    }
                    text_snap, _ = self.check(ReadPurpose.TIMER_ELAPSE, text, native)
                    self.assertEqual(text_snap.next_elapse_realtime.kind, rt_kind)
                    self.assertEqual(text_snap.next_elapse_monotonic.kind, mono_kind)
                    self.assertEqual(
                        us.snapshot_finite_next_elapse_basis(text_snap),
                        _legacy_finite_next_elapse_basis(text),
                    )


class LegacyWrapperCompositionTests(unittest.TestCase):
    """5c.1 review fixup 2: the legacy-shaped parser feeds the classifier
    exactly as before (the string view is accepted and classified the same
    as the legacy pair), and anything else is a TypeError."""

    def test_parse_then_classify_matches_legacy_over_the_corpus(self) -> None:
        for stdout in CORPUS:
            with self.subTest(stdout=stdout[:80]):
                legacy = _outcome(
                    lambda: _legacy_classify_stale_unit_failure_reset_snapshot(
                        _legacy_parse_stale_unit_failure_reset_show(stdout)
                    )
                )
                new = _outcome(
                    lambda: pi._classify_stale_unit_failure_reset_snapshot(
                        pi._parse_stale_unit_failure_reset_show(stdout)
                    )
                )
                self.assertEqual(new, legacy)

    def test_other_types_are_refused(self) -> None:
        with self.assertRaises(TypeError):
            pi._classify_stale_unit_failure_reset_snapshot({"LoadState": "not-found"})  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
