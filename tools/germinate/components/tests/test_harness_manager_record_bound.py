"""T-6146: every real seat's harness-manager run record fits run_state.MAX_RECORD_BYTES.

Builds the record exactly as the engine admits and completes a run (executor.py: one
{op_id, crash_contract, precondition_check, postcondition_check} entry per op, one receipt
per committed op) from the real planners over the real instance.yaml, writes it through the
real RunHandle, and reads it back. The decommission (``remove``) planner refuses recovery and
watchman (role-protected recovery tier), so they have install records only; the codex seat
(consultant) and every other seat have both.

Measured 2026-11-04 on main (bytes): decommission orchestrator 73,999 / specialist 69,396 / codex 65,024 /
archivist 63,807 / artisan 59,626 / mason 59,389; install orchestrator 66,391 / recovery 65,427 /
specialist 56,890 / archivist 53,446. orchestrator and specialist decommission were already over the 64 KiB
generic cap (65,536) before the bound.
"""

from __future__ import annotations

import pytest

from tools.germinate.components.tests import test_harness_manager_remove as T
from tools.germinate.engine import run_state as RS

#: a record must fit with at least this much headroom, so seat growth fails here, in CI,
#: while the real bound still has room (not at decommission time).
HEADROOM = 2

PERSONAS = ("orchestrator", "specialist", "mason", "artisan", "recovery", "archivist", "codex", "watchman")  # codex = consultant


def _record_ops(ops):
    return [{"op_id": o["operation_id"], "crash_contract": o["crash_contract"],
             "precondition_check": dict(o["precondition"]), "postcondition_check": dict(o["desired"])} for o in ops]


def _completed_record_size(tmp_path, ops, leg):
    store = RS.RunStore(tmp_path / "rs")
    tup = RS.RunTuple("sim", "harness-manager", "seat-x", leg, RS.new_ulid())
    with store.create(tup, operations=_record_ops(ops), plan=RS.encode_plan({})) as handle:
        receipts = [{"committed_at": "2026-11-04T07:37:32.634472Z", "op_id": o["operation_id"],
                     "receipt_ref": f"receipt-{o['operation_id']}.json"} for o in ops]
        handle.update(receipts=receipts)
        handle.read_record()
        return (handle.path / "record.json").stat().st_size


def _report(kind, persona, count, size):
    print(f"RECORD-SIZE {kind} {persona} ops={count} bytes={size}")


@pytest.mark.parametrize("persona", PERSONAS)
def test_real_install_run_record_fits_the_bound(tmp_path, persona):
    ops = T.HM.plan_install({"scope": f"seat-{T.ACCOUNTS[persona]}", "desired": {"source_revision": T.REV}})
    size = _completed_record_size(tmp_path, ops, "install")
    _report("install", persona, len(ops), size)
    assert size * HEADROOM <= RS.MAX_RECORD_BYTES


@pytest.mark.parametrize("persona", [p for p in PERSONAS if p not in ("recovery", "watchman")])
def test_real_decommission_run_record_fits_the_bound(tmp_path, persona):
    ops = T._plan(persona)
    size = _completed_record_size(tmp_path, ops, "remove")
    _report("decommission", persona, len(ops), size)
    assert size * HEADROOM <= RS.MAX_RECORD_BYTES


@pytest.mark.parametrize("persona", ["recovery", "watchman"])
def test_the_recovery_tier_has_no_decommission_record(persona):
    with pytest.raises(T.HM.PlanRefused, match=r"\[role-protected\]"):
        T._plan(persona)
