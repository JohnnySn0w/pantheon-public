from __future__ import annotations

import base64
import hashlib
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace


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


def _run_killed_child(run_dir: Path, boundary: str):
    proc = subprocess.Popen([sys.executable, "-c", _child_script(), str(run_dir), boundary])
    proc.wait(timeout=10)
    assert proc.returncode == -signal.SIGKILL


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
