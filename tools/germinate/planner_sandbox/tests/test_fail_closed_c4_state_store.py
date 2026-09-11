"""C4 (fail-closed on primitive unavailability), state-store dimension,
for T-3293 (Tranche B). Tranche-specific filename per the T-3293 brief --
deliberately NOT shared with other tranches' `test_fail_closed_c4.py` to
avoid cross-tranche merge collision.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from tools.germinate.planner_sandbox.harness.fixtures import StateStore, temp_state_store
from tools.germinate.planner_sandbox.state_machine.reconciler import NextState, RefuseMalformed, reconcile
from tools.germinate.planner_sandbox.state_machine.states import State

_INSTANCE = "test-instance"
_COMPONENT = "test-component"
_SCOPE = "system"
_LEG = "install"
_SCHEMA = "pantheon-run-state/v1"
_BOOT_A = "4a5b6c7d-89ab-cdef-0123-456789abcdef"


def _record_path(store: StateStore, run_id: str) -> Path:
    return store.record_path(
        instance=_INSTANCE, component=_COMPONENT, scope=_SCOPE, leg=_LEG, run_id=run_id
    )


def test_c4_state_store_malformed() -> None:
    """Corrupt a run-record's JSON; next reconciliation refuses that
    record specifically without progressing others (ORG-080 C4)."""

    with temp_state_store() as store:
        good_path = _record_path(store, "01JAGOODRUNID000000000000")
        good_path.parent.mkdir(parents=True, exist_ok=True)
        good_payload = {
            "schema": _SCHEMA,
            "tuple": {
                "instance": _INSTANCE,
                "component": _COMPONENT,
                "scope": _SCOPE,
                "leg": _LEG,
                "run_id": "01JAGOODRUNID000000000000",
            },
            "state": State.ACTIVE.value,
            "boot_id": _BOOT_A,
        }
        good_tmp = good_path.with_suffix(".json.tmp")
        good_tmp.write_text(json.dumps(good_payload), encoding="utf-8")
        os.rename(good_tmp, good_path)

        bad_path = _record_path(store, "01JABADRUNID0000000000000")
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        # Genuinely unparseable JSON -- real corruption, not a semantic
        # field-value error.
        bad_tmp = bad_path.with_suffix(".json.tmp")
        bad_tmp.write_text('{"schema": "pantheon-run-state/v1", "state": ', encoding="utf-8")
        os.rename(bad_tmp, bad_path)

        good_result = reconcile(good_path.read_text(encoding="utf-8"), current_boot_id=_BOOT_A)
        bad_result = reconcile(bad_path.read_text(encoding="utf-8"), current_boot_id=_BOOT_A)

        # The sibling record's own content is untouched by reconciling the
        # corrupt one -- proves per-record isolation without needing a
        # multi-record sweep loop in the reconciler itself.
        good_path_text_after = good_path.read_text(encoding="utf-8")
        bad_path_text_after = bad_path.read_text(encoding="utf-8")

    assert isinstance(bad_result, RefuseMalformed)
    assert "invalid JSON" in bad_result.reason

    assert isinstance(good_result, NextState)
    assert good_result.state is State.ACTIVE

    assert json.loads(good_path_text_after)["state"] == State.ACTIVE.value
    assert bad_path_text_after == '{"schema": "pantheon-run-state/v1", "state": '
