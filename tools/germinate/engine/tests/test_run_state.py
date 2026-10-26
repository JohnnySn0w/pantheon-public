from __future__ import annotations

import os

import pytest

from tools.germinate.engine import run_state as RS
from tools.germinate.planner_sandbox.state_machine.states import State


@pytest.mark.parametrize("value", ["comp-a", "seat-orchestrator", "a/b", "naïve", "x y", "~._-"])
def test_segment_round_trip(value):
    assert RS.decode_segment(RS.encode_segment(value)) == value


def test_encoding_is_lowercase_percent():
    assert RS.encode_segment("a/b") == "a%2fb"


@pytest.mark.parametrize(
    "encoded",
    ["a%2Fb", "%2d", "%ff", "%c3", "", "a" * 65, "a%2", "a%zz", "a b", "..", "."],
)
def test_rejecting_decoder(encoded):
    with pytest.raises(RS.RunStateError):
        RS.decode_segment(encoded)


def test_encoder_refuses_traversal_and_overlong():
    for bad in ("", ".", "..", "/" * 30):
        with pytest.raises(RS.RunStateError):
            RS.encode_segment(bad)


def test_ulid_canonical_and_monotonic():
    a, b = RS.new_ulid(1_700_000_000_000), RS.new_ulid(1_700_000_000_000)
    assert len(a) == 26 and a < b
    assert RS.validate_ulid(a) == a


@pytest.mark.parametrize("bad", ["01h", "0" * 25, "I" * 26, "u" + "0" * 25, 5])
def test_ulid_rejects_noncanonical(bad):
    with pytest.raises(RS.RunStateError):
        RS.validate_ulid(bad)


def test_ulid_from_the_future_refuses():
    future = RS.new_ulid(2_000_000_000_000)
    with pytest.raises(RS.RunStateError, match="future"):
        RS.validate_ulid(future, now_ms=1_700_000_000_000)
    RS.validate_ulid(future, now_ms=2_000_000_000_000 - 30_000)  # within the 60s skew allowance


def _tuple():
    return RS.RunTuple("sim", "comp-a", "system", "install", RS.new_ulid())


def _create(tmp_path):
    store = RS.RunStore(tmp_path / "rs")
    tup = _tuple()
    handle = store.create(tup, operations=[{"op_id": "op-1", "crash_contract": "stageable"}])
    return store, tup, handle


def test_bijective_path_and_modes(tmp_path):
    store, tup, handle = _create(tmp_path)
    with handle:
        assert handle.path == store.root / "schema-v1" / "sim" / "comp-a" / "system" / "install" / tup.run_id
        assert oct(os.stat(handle.path).st_mode & 0o777) == "0o700"
        assert oct(os.stat(handle.path / "record.json").st_mode & 0o777) == "0o600"
        assert handle.read_record()["state"] == "PENDING"
    assert store.find(tup.run_id) == tup
    assert store.all_tuples() == [tup]


def test_transitions_follow_the_closed_table(tmp_path):
    _store, _tup, handle = _create(tmp_path)
    with handle:
        handle.update(state=State.ACTIVE)
        with pytest.raises(RS.RunStateError, match="illegal transition"):
            handle.update(state=State.ROLLED_BACK)
        handle.update(state=State.HALTED, halted={"op_id": "op-1", "reason": "x", "at": RS.now_iso()})
        handle.update(state=State.ROLLING_BACK, halted=None)
        handle.update(state=State.ROLLED_BACK)
        with pytest.raises(RS.RunStateError, match="illegal transition"):
            handle.update(state=State.ACTIVE)  # terminal is absorbing


def test_record_invariants_refuse(tmp_path):
    _store, _tup, handle = _create(tmp_path)
    with handle:
        with pytest.raises(RS.RunStateError, match="halted is non-null"):
            handle.update(state=State.ACTIVE, halted={"op_id": "op-1", "reason": "x", "at": "t"})
        handle.update(state=State.ACTIVE)
        with pytest.raises(RS.RunStateError, match="halted.op_id"):
            handle.update(state=State.HALTED, halted={"op_id": "ghost", "reason": "x", "at": "t"})
        with pytest.raises(RS.RunStateError, match="not writable"):
            handle.update(tuple={})
        with pytest.raises(RS.RunStateError, match="not writable"):
            handle.update(unknown_field=1)


def test_tampered_record_refuses_reads(tmp_path):
    store, tup, handle = _create(tmp_path)
    record_path = handle.path / "record.json"
    handle.close()
    other = RS.RunTuple("sim", "comp-b", "system", "install", tup.run_id)
    data = record_path.read_bytes().replace(b'"comp-a"', b'"comp-b"')
    record_path.write_bytes(data)
    with store.open(tup) as again, pytest.raises(RS.RunStateError, match="tampering"):
        again.read_record()
    del other


def test_wrong_mode_refuses(tmp_path):
    store, tup, handle = _create(tmp_path)
    handle.close()
    os.chmod(store.root / tup.relpath() / "record.json", 0o644)
    with store.open(tup) as again, pytest.raises(RS.RunStateError, match="unreadable"):
        again.read_record()
    os.chmod(store.root / tup.relpath(), 0o755)
    with pytest.raises(RS.RunStateError):
        store.open(tup)


def test_ancestor_with_wrong_mode_refuses_create(tmp_path):
    root = tmp_path / "rs"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)
    with pytest.raises(RS.RunStateError, match="mode-0700"):
        RS.RunStore(root).create(_tuple(), operations=[])


def test_unknown_schema_refuses(tmp_path):
    with pytest.raises(RS.RunStateError):
        RS.validate_record(
            {k: None for k in RS.RECORD_KEYS} | {"schema": "pantheon-run-state/v2"}
        )
