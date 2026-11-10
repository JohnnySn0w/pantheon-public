"""T-5608: the T-5473 residue-quarantine predicate is ONE set of objects.

Intent (T-5608, HO ruling 2026-10-27): the per-seat executor runs the quarantine
with preflight and execute sharing the SAME predicate as the installer op. The
predicate moved verbatim from ``tools/persona_installer.py`` into the executor-
allowed ``tools/persona_installer_activation.py``; the installer re-exports it.

Pinned here:

* identity: every moved name is the same object through both modules, and the
  executor engine binds the same objects;
* behaviour: ``LocalHostBackend._residue_quarantine_preflight`` now delegates to
  ``_residue_quarantine_check``; a transcription of its pre-T-5608 body is the
  reference, and both agree (result AND which reads ran, in which order) over a
  matrix of desired shapes, timer reads, unit states and residue listings.
"""

from __future__ import annotations

import itertools
import unittest
from pathlib import Path
from unittest import mock

from tools import persona_installer as PI
from tools import persona_installer_activation as ACT
from tools.persona_installer_contracts import InstallOperation, OperationFailed, PreflightResult

MOVED = (
    "QUARANTINE_DIR_PREFIX",
    "RESIDUE_RUNTIME_CONTROLLER",
    "RESIDUE_PERSONA_ROOT",
    "RESIDUE_STATE_ROOT_SUFFIXES",
    "_QUARANTINE_CLASSES",
    "_PERSONA_NAME",
    "_quarantine_desired",
    "_quarantine_class_base",
    "_bootstrap_residue_entries",
    "_RESIDUE_SAFE_ACTIVE_STATES",
    "_runtime_liveness_refusal",
    "_quarantine_stamp",
    "_quarantine_dir_for",
    "_quarantine_pairs",
    "_quarantine_can_rollback",
    "_residue_quarantine_check",
)


def _reference_preflight(backend, operation) -> PreflightResult:
    """The pre-T-5608 body of LocalHostBackend._residue_quarantine_preflight, verbatim."""
    try:
        root, persona, timer = ACT._quarantine_desired(operation)
        if not backend._bootstrap_fire_pending(timer):
            return PreflightResult(
                True, "converged",
                "consume timer already enabled: not a bootstrap fire, runtime state left in place",
            )
        live = ACT._runtime_liveness_refusal(backend._runtime_liveness(persona))
        if live:
            return PreflightResult(False, "conflicting", live)
        entries = ACT._bootstrap_residue_entries(Path(root), persona)
    except OperationFailed as exc:
        return PreflightResult(False, "conflicting", str(exc))
    if not entries:
        return PreflightResult(True, "absent", "no control-plane residue to quarantine")
    names = ", ".join(f"{cls}/{name}" for cls, name in entries)
    return PreflightResult(
        True, "ready",
        f"would move {len(entries)} path(s) into {root}/{persona}/{ACT.QUARANTINE_DIR_PREFIX}<stamp>: {names}",
    )


def _op(desired: dict[str, str]) -> InstallOperation:
    return InstallOperation(
        operation_id=PI.RESIDUE_QUARANTINE_OP_ID,
        layer=1,
        kind=PI.RESIDUE_QUARANTINE_KIND,
        target="/home/x/.local/state/pantheon/personas/recovery",
        precondition={"state": "must-pass"},
        desired=desired,
        inverse={},
        privilege="systemd.user:x",
        owner="x",
        provenance={},
    )


GOOD = {"persona_state_root": "/home/x/.local/state/pantheon/personas", "persona": "recovery",
        "timer": "pantheon-persona-consume@recovery.timer"}
DESIREDS = (
    GOOD,
    {**GOOD, "persona_state_root": "relative/root"},
    {**GOOD, "persona": "../escape"},
    {**GOOD, "timer": ""},
    {**GOOD, "persona_state_root": "/home/x/state/"},
)
TIMERS = (True, False, "raise")
STATES = ("inactive", "failed", "active", "activating", "deactivating", "reloading", None, "weird")
ENTRIES = ((), (("runtime-controller", "runtime.json"),),
           (("persona-root", "staged-cutover-1.json"), ("state-root", "recovery.json")), "raise")


class _Backend:
    """Records the reads; answers from the matrix cell."""

    def __init__(self, enabled, states, calls):
        self.enabled, self.states, self.calls = enabled, states, calls

    def _bootstrap_fire_pending(self, timer):
        self.calls.append(("timer", timer))
        if self.enabled == "raise":
            raise OperationFailed("cannot read the consume timer enablement: boom")
        return not self.enabled

    def _runtime_liveness(self, persona):
        self.calls.append(("liveness", persona))
        return dict(self.states)

    def _quarantine_liveness(self, operation, persona):
        # T-5612: the pane signal has its own tests; this matrix pins the unit-state half
        return self._runtime_liveness(persona)


class PredicateIdentityTests(unittest.TestCase):
    def test_every_moved_name_is_one_object(self) -> None:
        for name in MOVED:
            with self.subTest(name=name):
                self.assertIs(getattr(PI, name), getattr(ACT, name))

    def test_the_moved_code_lives_in_the_activation_module(self) -> None:
        for name in MOVED:
            value = getattr(ACT, name)
            if callable(value) and hasattr(value, "__module__"):
                self.assertEqual(value.__module__, "tools.persona_installer_activation", name)

    def test_the_executor_binds_the_same_objects(self) -> None:
        # T-5608: the per-seat executor runs the installer's predicate objects
        from tools.host_control.executor import engine

        for name in ("_residue_quarantine_check", "_bootstrap_residue_entries", "_quarantine_desired",
                     "_quarantine_pairs", "_quarantine_can_rollback", "_quarantine_dir_for",
                     "_quarantine_class_base", "_quarantine_stamp", "QUARANTINE_DIR_PREFIX",
                     "_QUARANTINE_CLASSES"):
            with self.subTest(name=name):
                self.assertIs(getattr(engine, name), getattr(ACT, name))
                self.assertIs(getattr(engine, name), getattr(PI, name))


class PredicateBehaviourMatrixTests(unittest.TestCase):
    def test_delegating_preflight_equals_the_pre_move_body(self) -> None:
        cells = 0
        for desired, enabled, controller, consume, entries in itertools.product(
            DESIREDS, TIMERS, STATES, STATES[:4] + (None,), ENTRIES
        ):
            states = {"pantheon-runtime-controller@recovery.service": controller,
                      "pantheon-persona-consume@recovery.service": consume}
            operation = _op(desired)

            def listing(root, persona, _entries=entries):
                if _entries == "raise":
                    raise OperationFailed("listing failed")
                return list(_entries)

            results = []
            for run in (_reference_preflight, PI.LocalHostBackend._residue_quarantine_preflight):
                calls: list = []
                backend = _Backend(enabled, states, calls)
                with mock.patch.object(ACT, "_bootstrap_residue_entries", side_effect=listing), \
                        mock.patch.object(PI, "_bootstrap_residue_entries", side_effect=listing):
                    result = run(backend, operation)
                results.append(((result.ok, result.classification, result.detail), tuple(calls)))
            self.assertEqual(results[0], results[1], (desired, enabled, states, entries))
            cells += 1
        self.assertGreater(cells, 2000)


if __name__ == "__main__":
    unittest.main()
