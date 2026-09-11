from __future__ import annotations

import base64
import hashlib
import json
import multiprocessing
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest


RUN_KEY = "run-a"
OP_A = "op-a"
OP_B = "op-b"


def _wal_api() -> SimpleNamespace:
    from tools.germinate.planner_sandbox import wal

    return SimpleNamespace(
        AuthorityError=wal.AuthorityError,
        BytesPayload=wal.BytesPayload,
        EffectObservation=wal.EffectObservation,
        EffectState=wal.EffectState,
        HeldDir=wal.HeldDir,
        ReconcileError=wal.ReconcileError,
        StructuralPrefix=wal.StructuralPrefix,
        WalStore=wal.WalStore,
        WalStoreError=wal.WalStoreError,
        canonical_json_bytes=wal.canonical_json_bytes,
        load_canonical_json=wal.load_canonical_json,
        sha256_hex=wal.sha256_hex,
    )


def _open_store(api: SimpleNamespace, run_dir: Path, run_key: str = RUN_KEY):
    os.chmod(run_dir, 0o700)
    fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return api.WalStore.from_fd(fd, run_key=run_key, expected_uid=os.geteuid())
    finally:
        os.close(fd)


def _payload(api: SimpleNamespace, data: bytes, **payload: object):
    return api.BytesPayload(
        kind="bytes",
        bytes_b64=base64.b64encode(data).decode("ascii"),
        sha256=hashlib.sha256(data).hexdigest(),
        payload=payload or None,
    )


def _satisfied_result() -> dict[str, object]:
    return {"satisfied": True, "evidence": {"kind": "caller-proof"}}


def _unsatisfied_result() -> dict[str, object]:
    return {"satisfied": False, "evidence": {"kind": "caller-proof"}}


class _SatisfiedProbe:
    def __init__(self, api: SimpleNamespace):
        self._api = api

    def observe(self, intent):
        return self._api.EffectObservation(
            applied=True,
            postcondition_satisfied=True,
            evidence={"kind": "test-probe"},
        )


class _AppliedProbe:
    def __init__(self, api: SimpleNamespace):
        self._api = api

    def observe(self, intent):
        return self._api.EffectObservation(
            applied=True,
            postcondition_satisfied=None,
            evidence={"kind": "test-probe"},
        )


def _write_intent(api: SimpleNamespace, store, op_id: str = OP_A, effect_domain: str = "local"):
    return store.write_intent(
        op_id=op_id,
        effect_domain=effect_domain,
        preimage=_payload(api, b"before"),
        inverse=_payload(api, b"before", restore=True),
        postcondition_check={
            "kind": "sha256-equals",
            "sha256": hashlib.sha256(b"after").hexdigest(),
        },
        created_at="2026-08-29T00:00:00Z",
    )


def _write_receipt(api: SimpleNamespace, store, op_id: str = OP_A):
    _intent, intent_digest = store.read_intent(op_id)
    intent_ref = f"intent-{op_id}.json"
    return store.write_receipt(
        op_id=op_id,
        intent_ref=intent_ref,
        intent_digest=intent_digest,
        postcondition_result=_satisfied_result(),
        receipt_payload={"kind": "poc-receipt"},
        committed_at="2026-08-29T00:00:01Z",
    )


def _committed_store(api: SimpleNamespace, run_dir: Path):
    store = _open_store(api, run_dir)
    store.initialize_manifest(
        created_at="2026-08-29T00:00:00Z",
        updated_at="2026-08-29T00:00:00Z",
    )
    _write_intent(api, store)
    receipt_ref, _receipt_digest = _write_receipt(api, store)
    store.commit_receipt_reference(
        op_id=OP_A,
        receipt_ref=receipt_ref,
        committed_at="2026-08-29T00:00:01Z",
        updated_at="2026-08-29T00:00:02Z",
    )
    return store


def _snapshot_tree(root: Path) -> dict[str, tuple[bytes, int]]:
    snapshot = {}
    for path in sorted(p for p in root.iterdir() if p.is_file()):
        st = path.stat(follow_symlinks=False)
        snapshot[path.name] = (path.read_bytes(), stat.S_IMODE(st.st_mode))
    return snapshot


def _append_worker(run_dir: str, op_id: str, committed_at: str, updated_at: str, queue):
    try:
        api = _wal_api()
        store = _open_store(api, Path(run_dir))
        receipt_ref, _receipt_digest = _write_receipt(api, store, op_id=op_id)
        store.commit_receipt_reference(
            op_id=op_id,
            receipt_ref=receipt_ref,
            committed_at=committed_at,
            updated_at=updated_at,
        )
        store.close()
        queue.put(("ok", op_id))
    except BaseException as exc:  # pragma: no cover - surfaced to parent assertion
        queue.put(("error", type(exc).__name__, str(exc)))


def test_held_dir_survives_ancestor_replacement_and_refuses_symlink_leaf(tmp_path: Path):
    api = _wal_api()
    original_parent = tmp_path / "parent"
    original_run = original_parent / "run"
    original_run.mkdir(parents=True)
    store = _open_store(api, original_run)

    moved_parent = tmp_path / "parent-moved"
    original_parent.rename(moved_parent)
    replacement_run = original_parent / "run"
    replacement_run.mkdir(parents=True)

    store.initialize_manifest(
        created_at="2026-08-29T00:00:00Z",
        updated_at="2026-08-29T00:00:00Z",
    )
    _write_intent(api, store)

    assert (moved_parent / "run" / "intent-op-a.json").is_file()
    assert not (replacement_run / "intent-op-a.json").exists()

    os.symlink(tmp_path / "elsewhere", moved_parent / "run" / "intent-symlink.json")
    with pytest.raises(api.WalStoreError):
        _write_intent(api, store, op_id="symlink")
    store.close()


def test_atomic_manifest_write_orders_file_fsync_rename_parent_fsync(tmp_path: Path, monkeypatch):
    api = _wal_api()
    from tools.germinate.planner_sandbox.wal import authority as authority_module

    events: list[tuple[str, object]] = []
    real_fsync = authority_module.os.fsync
    real_replace = authority_module.os.replace

    def recording_fsync(fd: int):
        st = os.fstat(fd)
        if stat.S_ISREG(st.st_mode):
            events.append(("fsync-file", fd))
        elif stat.S_ISDIR(st.st_mode):
            events.append(("fsync-dir", fd))
        return real_fsync(fd)

    def recording_replace(src, dst, *args, **kwargs):
        events.append(("replace", (src, dst, kwargs)))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(authority_module.os, "fsync", recording_fsync)
    monkeypatch.setattr(authority_module.os, "replace", recording_replace)

    store = _open_store(api, tmp_path)
    store.initialize_manifest(
        created_at="2026-08-29T00:00:00Z",
        updated_at="2026-08-29T00:00:00Z",
    )
    _write_intent(api, store)
    receipt_ref, _receipt_digest = _write_receipt(api, store)
    store.commit_receipt_reference(
        op_id=OP_A,
        receipt_ref=receipt_ref,
        committed_at="2026-08-29T00:00:01Z",
        updated_at="2026-08-29T00:00:02Z",
    )
    store.close()

    replace_index = next(i for i, event in enumerate(events) if event[0] == "replace")
    assert any(event[0] == "fsync-file" for event in events[:replace_index])
    assert any(event[0] == "fsync-dir" for event in events[replace_index + 1 :])
    replace_kwargs = events[replace_index][1][2]
    assert replace_kwargs["src_dir_fd"] == replace_kwargs["dst_dir_fd"]


def test_intent_is_immutable_canonical_digest_bound_and_contains_preimage_inverse(tmp_path: Path):
    api = _wal_api()
    store = _open_store(api, tmp_path)
    store.initialize_manifest(
        created_at="2026-08-29T00:00:00Z",
        updated_at="2026-08-29T00:00:00Z",
    )
    intent_ref, intent_digest = _write_intent(api, store)

    intent_bytes = (tmp_path / intent_ref).read_bytes()
    assert intent_digest == hashlib.sha256(intent_bytes).hexdigest()
    decoded = api.load_canonical_json(intent_bytes)
    assert intent_bytes == api.canonical_json_bytes(decoded)
    assert decoded["preimage"]["sha256"] == hashlib.sha256(b"before").hexdigest()
    assert decoded["inverse"]["sha256"] == hashlib.sha256(b"before").hexdigest()
    assert decoded["inverse"]["payload"] == {"restore": True}

    before = intent_bytes
    with pytest.raises(api.WalStoreError):
        store.write_intent(
            op_id=OP_A,
            effect_domain="local",
            preimage=_payload(api, b"different"),
            inverse=_payload(api, b"different"),
            postcondition_check={"kind": "sha256-equals", "sha256": "0" * 64},
            created_at="2026-08-29T00:00:01Z",
        )
    assert (tmp_path / intent_ref).read_bytes() == before
    store.close()


def test_receipt_commit_requires_supplied_satisfied_proof_and_emits_exact_entry_shape(tmp_path: Path):
    api = _wal_api()
    store = _open_store(api, tmp_path)
    store.initialize_manifest(
        created_at="2026-08-29T00:00:00Z",
        updated_at="2026-08-29T00:00:00Z",
    )
    intent_ref, intent_digest = _write_intent(api, store)

    with pytest.raises(api.WalStoreError):
        store.write_receipt(
            op_id=OP_A,
            intent_ref=intent_ref,
            intent_digest=intent_digest,
            postcondition_result=_unsatisfied_result(),
            receipt_payload={"kind": "poc-receipt"},
            committed_at="2026-08-29T00:00:01Z",
        )
    assert store.read_manifest().receipts == ()

    receipt_ref, _receipt_digest = store.write_receipt(
        op_id=OP_A,
        intent_ref=intent_ref,
        intent_digest=intent_digest,
        postcondition_result=_satisfied_result(),
        receipt_payload={"kind": "poc-receipt"},
        committed_at="2026-08-29T00:00:01Z",
    )
    entry = store.commit_receipt_reference(
        op_id=OP_A,
        receipt_ref=receipt_ref,
        committed_at="2026-08-29T00:00:01Z",
        updated_at="2026-08-29T00:00:02Z",
    )
    assert set(entry.__dict__) == {"op_id", "receipt_ref", "committed_at"}
    manifest = json.loads((tmp_path / "manifest.json").read_bytes())
    assert set(manifest["receipts"][0]) == {"op_id", "receipt_ref", "committed_at"}
    store.close()


def test_record_lock_two_process_append_has_no_lost_update(tmp_path: Path):
    api = _wal_api()
    store = _open_store(api, tmp_path)
    store.initialize_manifest(
        created_at="2026-08-29T00:00:00Z",
        updated_at="2026-08-29T00:00:00Z",
    )
    _write_intent(api, store, op_id=OP_A)
    _write_intent(api, store, op_id=OP_B)
    store.close()

    queue = multiprocessing.Queue()
    processes = [
        multiprocessing.Process(
            target=_append_worker,
            args=(str(tmp_path), OP_A, "2026-08-29T00:00:01Z", "2026-08-29T00:00:02Z", queue),
        ),
        multiprocessing.Process(
            target=_append_worker,
            args=(str(tmp_path), OP_B, "2026-08-29T00:00:03Z", "2026-08-29T00:00:04Z", queue),
        ),
    ]
    for process in processes:
        process.start()
    results = [queue.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)

    assert results == [("ok", OP_A), ("ok", OP_B)] or results == [("ok", OP_B), ("ok", OP_A)]
    reopened = _open_store(api, tmp_path)
    assert {entry.op_id for entry in reopened.read_manifest().receipts} == {OP_A, OP_B}
    reopened.close()


@pytest.mark.parametrize("adversary", ["noncanonical-manifest", "wrong-mode-intent", "wrong-digest-receipt"])
def test_malformed_noncanonical_wrong_mode_or_digest_refuses_without_manifest_change(
    tmp_path: Path,
    adversary: str,
):
    api = _wal_api()
    store = _committed_store(api, tmp_path)
    store.close()
    before_manifest = (tmp_path / "manifest.json").read_bytes()

    # Direct Path writes below model a hostile actor after production code has closed.
    if adversary == "noncanonical-manifest":
        (tmp_path / "manifest.json").write_text('{"schema":"pantheon-wal-poc/v1"}', encoding="utf-8")
    elif adversary == "wrong-mode-intent":
        os.chmod(tmp_path / "intent-op-a.json", 0o644)
    elif adversary == "wrong-digest-receipt":
        receipt = api.load_canonical_json((tmp_path / "receipt-op-a.json").read_bytes())
        receipt["intent_digest"] = "0" * 64
        (tmp_path / "receipt-op-a.json").write_bytes(api.canonical_json_bytes(receipt))
    after_adversary_manifest = (tmp_path / "manifest.json").read_bytes()

    reopened = _open_store(api, tmp_path)
    with pytest.raises((api.WalStoreError, api.ReconcileError)):
        reopened.classify_prefix(op_id=OP_A, probe=_SatisfiedProbe(api))
    if adversary == "noncanonical-manifest":
        assert after_adversary_manifest != before_manifest
    assert (tmp_path / "manifest.json").read_bytes() == after_adversary_manifest
    reopened.close()


def test_committed_reference_without_probe_is_not_current_success(tmp_path: Path):
    api = _wal_api()
    store = _committed_store(api, tmp_path)
    classification = store.classify_prefix(op_id=OP_A, probe=None)
    assert classification.structural_prefix is api.StructuralPrefix.MANIFEST_REFERENCED
    assert classification.effect_state is api.EffectState.UNKNOWN_REFUSE
    assert classification.success_authorized is False
    store.close()


def test_external_effect_without_probe_is_unknown_and_refuses(tmp_path: Path):
    api = _wal_api()
    store = _open_store(api, tmp_path)
    store.initialize_manifest(
        created_at="2026-08-29T00:00:00Z",
        updated_at="2026-08-29T00:00:00Z",
    )
    _write_intent(api, store, effect_domain="external")

    classification = store.classify_prefix(op_id=OP_A, probe=None)
    assert classification.structural_prefix is api.StructuralPrefix.INTENT_ONLY
    assert classification.effect_state is api.EffectState.UNKNOWN_REFUSE
    assert classification.success_authorized is False
    store.close()


def test_repeated_classification_is_idempotent_and_disk_read_only(tmp_path: Path):
    api = _wal_api()
    store = _committed_store(api, tmp_path)
    before = _snapshot_tree(tmp_path)

    first = store.classify_prefix(op_id=OP_A, probe=_SatisfiedProbe(api))
    second = store.classify_prefix(op_id=OP_A, probe=_SatisfiedProbe(api))

    assert first == second
    assert first.success_authorized is True
    assert first.structural_prefix is api.StructuralPrefix.MANIFEST_REFERENCED
    assert first.effect_state is api.EffectState.POSTCONDITION_SATISFIED
    assert _snapshot_tree(tmp_path) == before
    store.close()


@pytest.mark.parametrize("state", ["committed-reference", "orphan-receipt"])
def test_audit_missing_intent_with_receipt_state_refuses_not_before_intent(tmp_path: Path, state: str):
    api = _wal_api()
    store = _open_store(api, tmp_path)
    store.initialize_manifest(
        created_at="2026-08-29T00:00:00Z",
        updated_at="2026-08-29T00:00:00Z",
    )
    _write_intent(api, store)
    receipt_ref, _receipt_digest = _write_receipt(api, store)
    if state == "committed-reference":
        store.commit_receipt_reference(
            op_id=OP_A,
            receipt_ref=receipt_ref,
            committed_at="2026-08-29T00:00:01Z",
            updated_at="2026-08-29T00:00:02Z",
        )
    store.close()

    # Direct unlink models a hostile post-close adversary; production cannot expose this.
    (tmp_path / "intent-op-a.json").unlink()

    reopened = _open_store(api, tmp_path)
    with pytest.raises(api.ReconcileError):
        reopened.classify_prefix(op_id=OP_A, probe=_SatisfiedProbe(api))
    reopened.close()


def test_audit_satisfied_probe_without_receipt_is_applied_no_receipt_not_success(tmp_path: Path):
    api = _wal_api()
    store = _open_store(api, tmp_path)
    store.initialize_manifest(
        created_at="2026-08-29T00:00:00Z",
        updated_at="2026-08-29T00:00:00Z",
    )
    _write_intent(api, store)

    classification = store.classify_prefix(op_id=OP_A, probe=_SatisfiedProbe(api))
    assert classification.structural_prefix is api.StructuralPrefix.APPLIED_NO_RECEIPT
    assert classification.effect_state is api.EffectState.POSTCONDITION_SATISFIED
    assert classification.success_authorized is False
    store.close()


def test_audit_failed_postcondition_probe_without_receipt_is_applied_no_receipt_not_success(tmp_path: Path):
    api = _wal_api()
    store = _open_store(api, tmp_path)
    store.initialize_manifest(
        created_at="2026-08-29T00:00:00Z",
        updated_at="2026-08-29T00:00:00Z",
    )
    _write_intent(api, store)

    class FailedPostconditionProbe:
        def observe(self, intent):
            return api.EffectObservation(
                applied=True,
                postcondition_satisfied=False,
                evidence={"kind": "test-probe"},
            )

    classification = store.classify_prefix(op_id=OP_A, probe=FailedPostconditionProbe())
    assert classification.structural_prefix is api.StructuralPrefix.APPLIED_NO_RECEIPT
    assert classification.effect_state is api.EffectState.POSTCONDITION_FAILED
    assert classification.success_authorized is False
    store.close()


def test_audit_wrong_mode_manifest_lock_refuses_without_chmod_or_manifest_change(tmp_path: Path):
    api = _wal_api()
    store = _open_store(api, tmp_path)
    store.initialize_manifest(
        created_at="2026-08-29T00:00:00Z",
        updated_at="2026-08-29T00:00:00Z",
    )
    _write_intent(api, store)
    receipt_ref, _receipt_digest = _write_receipt(api, store)
    store.close()

    # Direct Path write models a hostile post-close adversary.
    lock_path = tmp_path / "manifest.lock"
    lock_path.write_bytes(b"hostile-lock")
    os.chmod(lock_path, 0o644)
    before_manifest = (tmp_path / "manifest.json").read_bytes()

    reopened = _open_store(api, tmp_path)
    with pytest.raises(api.WalStoreError):
        reopened.commit_receipt_reference(
            op_id=OP_A,
            receipt_ref=receipt_ref,
            committed_at="2026-08-29T00:00:01Z",
            updated_at="2026-08-29T00:00:02Z",
        )
    assert stat.S_IMODE(lock_path.stat(follow_symlinks=False).st_mode) == 0o644
    assert lock_path.read_bytes() == b"hostile-lock"
    assert (tmp_path / "manifest.json").read_bytes() == before_manifest
    reopened.close()


def test_audit_receipt_with_unsatisfied_stored_result_refuses_even_with_satisfied_probe(tmp_path: Path):
    api = _wal_api()
    store = _open_store(api, tmp_path)
    store.initialize_manifest(
        created_at="2026-08-29T00:00:00Z",
        updated_at="2026-08-29T00:00:00Z",
    )
    _write_intent(api, store)
    _write_receipt(api, store)
    store.close()

    # Direct canonical rewrite models a hostile post-close adversary.
    receipt_path = tmp_path / "receipt-op-a.json"
    receipt = api.load_canonical_json(receipt_path.read_bytes())
    receipt["postcondition_result"] = {"satisfied": False, "evidence": {"kind": "hostile"}}
    receipt_path.write_bytes(api.canonical_json_bytes(receipt))
    os.chmod(receipt_path, 0o600)

    reopened = _open_store(api, tmp_path)
    with pytest.raises(api.WalStoreError):
        reopened.read_receipt_by_ref("receipt-op-a.json")
    with pytest.raises(api.ReconcileError):
        reopened.classify_prefix(op_id=OP_A, probe=_SatisfiedProbe(api))
    reopened.close()
