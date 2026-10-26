from __future__ import annotations

import base64
import hashlib
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


RUN_KEY = "run-a"
OP_A = "op-a"


def _wal_api() -> SimpleNamespace:
    from tools.germinate.planner_sandbox import wal

    return SimpleNamespace(
        BytesPayload=wal.BytesPayload,
        EffectObservation=wal.EffectObservation,
        EffectState=wal.EffectState,
        StructuralPrefix=wal.StructuralPrefix,
        WalStore=wal.WalStore,
    )


def _open_store(api: SimpleNamespace, run_dir: Path):
    os.chmod(run_dir, 0o700)
    fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return api.WalStore.from_fd(fd, run_key=RUN_KEY, expected_uid=os.geteuid())
    finally:
        os.close(fd)


class _SatisfiedProbe:
    def __init__(self, api: SimpleNamespace):
        self._api = api

    def observe(self, intent):
        return self._api.EffectObservation(
            applied=True,
            postcondition_satisfied=True,
            evidence={"kind": "fresh-process-test-probe"},
        )


class _AppliedProbe:
    def __init__(self, api: SimpleNamespace):
        self._api = api

    def observe(self, intent):
        return self._api.EffectObservation(
            applied=True,
            postcondition_satisfied=None,
            evidence={"kind": "fresh-process-test-probe"},
        )


def _child_script() -> str:
    return textwrap.dedent(
        """
        import base64
        import hashlib
        import os
        import signal
        import sys
        from pathlib import Path

        from tools.germinate.planner_sandbox import wal

        run_dir = Path(sys.argv[1])
        boundary = sys.argv[2]
        fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            store = wal.WalStore.from_fd(fd, run_key="run-a", expected_uid=os.geteuid())
        finally:
            os.close(fd)

        def payload(data, **extra):
            return wal.BytesPayload(
                kind="bytes",
                bytes_b64=base64.b64encode(data).decode("ascii"),
                sha256=hashlib.sha256(data).hexdigest(),
                payload=extra or None,
            )

        store.initialize_manifest(
            created_at="2026-08-29T00:00:00Z",
            updated_at="2026-08-29T00:00:00Z",
        )
        intent_ref, intent_digest = store.write_intent(
            op_id="op-a",
            effect_domain="local",
            preimage=payload(b"before"),
            inverse=payload(b"before", restore=True),
            postcondition_check={
                "kind": "sha256-equals",
                "sha256": hashlib.sha256(b"after").hexdigest(),
            },
            created_at="2026-08-29T00:00:00Z",
        )
        if boundary == "after-intent":
            os.kill(os.getpid(), signal.SIGKILL)

        (run_dir / "target.txt").write_bytes(b"after")
        if boundary == "after-mutation":
            os.kill(os.getpid(), signal.SIGKILL)

        receipt_ref, receipt_digest = store.write_receipt(
            op_id="op-a",
            intent_ref=intent_ref,
            intent_digest=intent_digest,
            postcondition_result={"satisfied": True, "evidence": {"kind": "caller-proof"}},
            receipt_payload={"kind": "poc-receipt"},
            committed_at="2026-08-29T00:00:01Z",
        )
        if boundary == "after-receipt":
            os.kill(os.getpid(), signal.SIGKILL)

        store.commit_receipt_reference(
            op_id="op-a",
            receipt_ref=receipt_ref,
            committed_at="2026-08-29T00:00:01Z",
            updated_at="2026-08-29T00:00:02Z",
        )
        """
    )


# T-4412: this was a fixed 10 s and raised TimeoutExpired on a loaded CI leg (dispatch
# run 36685685124, py3.11). The child imports the WAL and does several fsync'd writes
# before it SIGKILLs itself; interpreter startup + imports + fsyncs on a disk-bound,
# xdist-loaded runner is legitimately slow, not hung. The deadline only has to separate
# "slow" from "hung", so it is generous; a real hang still fails, just not at 10 s.
_CHILD_DEADLINE_S = 120.0


def _run_killed_child(run_dir: Path, boundary: str):
    proc = subprocess.Popen([sys.executable, "-c", _child_script(), str(run_dir), boundary])
    try:
        proc.wait(timeout=_CHILD_DEADLINE_S)
    except subprocess.TimeoutExpired:
        proc.kill()  # never leak a hung child into the rest of the run
        proc.wait()
        raise AssertionError(
            f"crash child (boundary={boundary!r}) still running after {_CHILD_DEADLINE_S}s: "
            "a hang, not load"
        ) from None
    assert proc.returncode == -signal.SIGKILL, (
        f"crash child (boundary={boundary!r}) exited {proc.returncode}, expected SIGKILL "
        f"({-signal.SIGKILL})"
    )


# T-4412: the crash child's runtime is dominated by fsync'd WAL writes plus interpreter
# startup and imports, which a loaded or disk-bound CI runner stretches well past any
# tight fixed budget. These pin the load-robust deadline and that its semantics survive.
def test_crash_child_deadline_is_generous_not_a_tight_fixed_budget():
    assert _CHILD_DEADLINE_S >= 60.0


def test_a_hung_child_fails_the_test_cleanly_and_is_reaped(monkeypatch, tmp_path: Path):
    spawned: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def spy_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    this = sys.modules[__name__]
    monkeypatch.setattr(subprocess, "Popen", spy_popen)
    monkeypatch.setattr(this, "_child_script", lambda: "import time; time.sleep(600)")
    monkeypatch.setattr(this, "_CHILD_DEADLINE_S", 0.5)
    started = time.monotonic()
    with pytest.raises(AssertionError, match="hang"):
        _run_killed_child(tmp_path, "after-intent")
    assert time.monotonic() - started < 5.0  # the named deadline is what bounds the wait
    assert spawned and spawned[0].poll() is not None  # killed and reaped, not leaked


def test_a_slow_child_within_the_deadline_still_passes(monkeypatch, tmp_path: Path):
    this = sys.modules[__name__]
    monkeypatch.setattr(
        this,
        "_child_script",
        lambda: "import os, signal, time; time.sleep(1.0); os.kill(os.getpid(), signal.SIGKILL)",
    )
    monkeypatch.setattr(this, "_CHILD_DEADLINE_S", 30.0)
    _run_killed_child(tmp_path, "after-intent")  # slow but SIGKILLed: crash semantics hold


def test_a_child_that_exits_without_being_killed_still_fails(monkeypatch, tmp_path: Path):
    this = sys.modules[__name__]
    monkeypatch.setattr(this, "_child_script", lambda: "import sys; sys.exit(0)")
    with pytest.raises(AssertionError, match="SIGKILL"):
        _run_killed_child(tmp_path, "after-intent")


def test_kill_after_intent_before_mutation_classifies_intent_only(tmp_path: Path):
    api = _wal_api()
    _run_killed_child(tmp_path, "after-intent")

    store = _open_store(api, tmp_path)
    classification = store.classify_prefix(op_id=OP_A, probe=None)
    assert classification.structural_prefix is api.StructuralPrefix.INTENT_ONLY
    assert classification.effect_state is api.EffectState.UNKNOWN_REFUSE
    assert classification.success_authorized is False
    assert not (tmp_path / "target.txt").exists()
    store.close()


def test_kill_after_mutation_before_receipt_classifies_applied_only_via_probe(tmp_path: Path):
    api = _wal_api()
    _run_killed_child(tmp_path, "after-mutation")

    store = _open_store(api, tmp_path)
    without_probe = store.classify_prefix(op_id=OP_A, probe=None)
    assert without_probe.structural_prefix is api.StructuralPrefix.INTENT_ONLY
    assert without_probe.effect_state is api.EffectState.UNKNOWN_REFUSE
    assert without_probe.success_authorized is False

    with_probe = store.classify_prefix(op_id=OP_A, probe=_AppliedProbe(api))
    assert with_probe.structural_prefix is api.StructuralPrefix.APPLIED_NO_RECEIPT
    assert with_probe.effect_state is api.EffectState.APPLIED
    assert with_probe.success_authorized is False
    store.close()


def test_kill_after_receipt_before_reference_classifies_orphan_not_success(tmp_path: Path):
    api = _wal_api()
    _run_killed_child(tmp_path, "after-receipt")

    store = _open_store(api, tmp_path)
    without_probe = store.classify_prefix(op_id=OP_A, probe=None)
    assert without_probe.structural_prefix is api.StructuralPrefix.ORPHAN_RECEIPT
    assert without_probe.effect_state is api.EffectState.UNKNOWN_REFUSE
    assert without_probe.success_authorized is False

    with_probe = store.classify_prefix(op_id=OP_A, probe=_SatisfiedProbe(api))
    assert with_probe.structural_prefix is api.StructuralPrefix.ORPHAN_RECEIPT
    assert with_probe.effect_state is api.EffectState.POSTCONDITION_SATISFIED
    assert with_probe.success_authorized is False
    store.close()
