"""T-6146: the run record.json has ONE bound (run_state.MAX_RECORD_BYTES), used by
RunHandle._write AND RunHandle.read_record, like the plan's MAX_PLAN_BYTES (T-5809).
Every other run-state artifact keeps the generic 64 KiB MAX_ARTIFACT_BYTES.

The record embeds every op's precondition/postcondition (~1.9 KB/op for a decommission
seat), so an orchestrator or specialist decommission record is already past 64 KiB on main.
"""

from __future__ import annotations

import pytest

from tools.germinate.engine import run_state as RS
from tools.germinate.planner_sandbox.wal import artifacts as A
from tools.germinate.planner_sandbox.wal.artifacts import MAX_ARTIFACT_BYTES, CanonicalJsonError


def _open(tmp_path):
    store = RS.RunStore(tmp_path / "rs")
    tup = RS.RunTuple("sim", "comp-a", "system", "install", RS.new_ulid())
    return store, store.create(tup, operations=[{"op_id": "op-1", "crash_contract": "stageable"}], plan=RS.encode_plan({}))


def _padded(record: dict, size: int) -> dict:
    """``record`` with one op padded so its canonical bytes are exactly ``size``."""

    out = dict(record, operations=[{"op_id": "op-1", "crash_contract": "stageable", "pad": ""}])
    base = len(A.canonical_json_bytes(out, max_bytes=1 << 30))
    out["operations"][0]["pad"] = "x" * (size - base)
    assert len(A.canonical_json_bytes(out, max_bytes=1 << 30)) == size
    return out


def test_the_bound_is_above_the_generic_cap():
    assert RS.MAX_RECORD_BYTES > MAX_ARTIFACT_BYTES
    assert RS.MAX_RECORD_BYTES == 1 << 19


def test_a_record_at_the_bound_writes_and_reads_back(tmp_path):
    _store, handle = _open(tmp_path)
    with handle:
        record = _padded(handle.read_record(), RS.MAX_RECORD_BYTES)
        handle._write(record)
        assert (handle.path / "record.json").stat().st_size == RS.MAX_RECORD_BYTES
        assert handle.read_record()["operations"] == record["operations"]
        assert handle.update(receipts=[])["operations"] == record["operations"]  # update re-reads and re-writes it


def test_a_record_one_byte_over_refuses_on_write_and_leaves_the_old_record(tmp_path):
    _store, handle = _open(tmp_path)
    with handle:
        before = handle.read_record()
        with pytest.raises(CanonicalJsonError, match="json-too-large"):
            handle._write(_padded(before, RS.MAX_RECORD_BYTES + 1))
        assert handle.read_record() == before


def test_a_record_one_byte_over_refuses_on_read(tmp_path):
    store, handle = _open(tmp_path)
    with handle:
        data = A.canonical_json_bytes(_padded(handle.read_record(), RS.MAX_RECORD_BYTES + 1), max_bytes=1 << 30)
        # plant past _write (which would itself refuse) to exercise the read side alone
        handle.held.replace_regular("record.json", data, expected_uid=store.uid, mode=RS.FILE_MODE)
        with pytest.raises(RS.RunStateError, match="file-too-large"):
            handle.read_record()


def test_the_record_bound_does_not_widen_the_other_artifacts():
    with pytest.raises(CanonicalJsonError, match="json-too-large"):
        A.canonical_json_bytes({"pad": "x" * MAX_ARTIFACT_BYTES})  # manifest/intent/receipt writers keep the default
