"""T-5473 / T-5474: the installer performs SOP phase Q itself.

Intent: let persona_installer converge an established seat's bootstrap without the hand-move's
manual state surgery. Two receipt-owned seat-local ops run before the consume bootstrap fire:

* ``seat-runtime-residue-quarantine`` MOVES (never deletes) stale control-plane residue into a
  stamped quarantine dir -- only where the unit-fire is an actual bootstrap fire (consume timer
  not enabled); on a converged/live seat it is a recorded no-op;
* ``seat-claude-workspace-trust`` sets ~/.claude.json projects[<clone>].hasTrustDialogAccepted.

Simulated backend / tmp roots / stub user manager only. NOTHING here touches a real
~/.claude.json, a real state root, a user manager, a live seat, claude or codex.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import pwd
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tools import persona_installer as _pim
from tools.host_control.envelope import EXECUTOR_KIND_CLASSES, OUT_OF_ENVELOPE_KINDS
from tools.persona_installer import (
    ExecutionContext,
    InstallOperation,
    LocalHostBackend,
    OperationFailed,
    PreflightResult,
    SimulatedHostBackend,
)
from tools.test_persona_installer_periodics import PeriodicCase

QKIND = "seat-runtime-residue-quarantine"
QID = "service.runtime-residue.quarantine"
TKIND = "seat-claude-workspace-trust"
TID = "harness.claude-workspace-trust"
KEY = "hasTrustDialogAccepted"
PERSONA = "recovery"
TIMER = f"pantheon-persona-consume@{PERSONA}.timer"
STAMP = "20261026T120000Z"

RESIDUE_FILES = (
    "recovery/runtime-controller/runtime.json",
    "recovery/runtime-controller/initial-viability.json",
    "recovery/runtime-controller/transition.json",
    "recovery/staged-cutover-1.json",
    "recovery/staged-cutover-2.json",
    "recovery/active-runtime.json",
    "recovery/mid-session-cap.json",
    "recovery.json",
    "recovery.backoff.json",
    "recovery.policyskip.json",
    "recovery.softstreak.json",
    "recovery.diagstreak.json",
)
RESIDUE_DIRS_WITH_CHILD = (
    "recovery/runtime-controller/initial-viability-invocations",
    "recovery/runtime-controller/quota",
    "recovery/runtimes",
    "recovery/invocations",
    "recovery/quota",
)
KEPT_FILES = (
    "recovery/runtime-controller/controller-evidence.jsonl",
    "recovery/runtime-controller/runtime-operation.lock",
    "recovery/overrides/holds.json",
)


def current_user() -> str:
    return pwd.getpwuid(os.geteuid()).pw_name


def make_q_op(root: str) -> InstallOperation:
    return InstallOperation(
        operation_id=QID, layer=1, kind=QKIND, target=f"{root}/{PERSONA}",
        precondition={"state": "must-pass"},
        desired={"persona_state_root": root, "persona": PERSONA, "timer": TIMER},
        inverse=None, privilege="operator-required", owner=current_user(),
        provenance={"install_root": "/"},
    )


def make_t_op(target: str, project: str) -> InstallOperation:
    return InstallOperation(
        operation_id=TID, layer=1, kind=TKIND, target=target,
        precondition={"state": "must-pass"},
        desired={"project_path": project, "key": KEY},
        inverse=None, privilege="operator-required", owner=current_user(),
        provenance={"install_root": "/"},
    )


# ---------------------------------------------------------------------------
# drivers: the same contract runs against the simulated and the local backend
# ---------------------------------------------------------------------------
class SimQDriver:
    ROOT = "/home/seat/.local/state/pantheon/personas"

    def setUp(self) -> None:
        self.backend = SimulatedHostBackend()
        self.backend.timer_enabled = False
        self.root = self.ROOT
        self.op = make_q_op(self.root)

    def set_pending(self, pending: bool) -> None:
        self.backend.timer_enabled = not pending

    def set_unit_state(self, unit: str, state) -> None:
        if state is None or isinstance(state, Exception):
            self.backend.unreadable_units.add(unit)
        else:
            self.backend.unit_active_states[unit] = state

    def put_file(self, rel: str, text: str = "{}") -> None:
        self.backend.seed_file(f"{self.root}/{rel}", text, "0600")

    def put_dir(self, rel: str, child: str | None = None) -> None:
        self.backend.entries[f"{self.root}/{rel}"] = {"state": "directory", "mode": "0700"}
        if child:
            self.put_file(f"{rel}/{child}", "child")

    def present(self, rel: str) -> bool:
        return self.backend.entries.get(f"{self.root}/{rel}", {"state": "absent"})["state"] != "absent"

    def snapshot(self) -> dict[str, str]:
        out = {}
        for key, entry in self.backend.entries.items():
            if key.startswith(self.root + "/") and entry["state"] != "absent":
                out[key[len(self.root) + 1:]] = entry.get("content_b64", entry["state"])
        return out

    def ctx(self) -> ExecutionContext:
        return ExecutionContext("all-in-one")

    def rollback_roots(self):
        return ()

    def stamp(self) -> None:
        self.backend.quarantine_stamp = STAMP


class LocalQDriver:
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.root = str(self.tmp / "personas")
        Path(self.root).mkdir()
        self.pending = True
        self.backend = LocalHostBackend()
        self.unit_states: dict[str, object] = {}  # unit -> ActiveState, or an Exception to raise
        self.backend._user_manager = types.SimpleNamespace(
            unit_file_enablement=lambda unit, timeout=None: types.SimpleNamespace(enabled=not self.pending),
            read_unit=self._read_unit,
        )
        self.backend._manager_ready = lambda: PreflightResult(True, "ready", "stub")  # type: ignore[method-assign]
        self.op = make_q_op(self.root)
        patcher = mock.patch.object(_pim, "_quarantine_stamp", return_value=STAMP)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _read_unit(self, unit, purpose):
        state = self.unit_states.get(unit, "inactive")
        if isinstance(state, Exception):
            raise state
        return types.SimpleNamespace(active_state=state)

    def set_pending(self, pending: bool) -> None:
        self.pending = pending

    def set_unit_state(self, unit: str, state) -> None:
        self.unit_states[unit] = state

    def put_file(self, rel: str, text: str = "{}") -> None:
        path = Path(self.root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def put_dir(self, rel: str, child: str | None = None) -> None:
        path = Path(self.root) / rel
        path.mkdir(parents=True, exist_ok=True)
        if child:
            (path / child).write_text("child")

    def present(self, rel: str) -> bool:
        return os.path.lexists(Path(self.root) / rel)

    def snapshot(self) -> dict[str, str]:
        out = {}
        base = Path(self.root)
        for path in sorted(base.rglob("*")):
            rel = str(path.relative_to(base))
            out[rel] = base64.b64encode(path.read_bytes()).decode() if path.is_file() else "directory"
        return out

    def ctx(self) -> ExecutionContext:
        return ExecutionContext("all-in-one", allowed_roots=(str(self.tmp),))

    def rollback_roots(self):
        return (str(self.tmp),)

    def stamp(self) -> None:
        pass


class QuarantineContract:
    def seed_full(self) -> None:
        for rel in RESIDUE_FILES + KEPT_FILES:
            self.put_file(rel)
        for rel in RESIDUE_DIRS_WITH_CHILD:
            self.put_dir(rel, "child.json")
        self.put_dir("recovery/overrides")  # parent of the kept hold (idempotent for Local)

    def execute(self):
        return self.backend.execute(self.op, self.ctx())

    def rollback(self, mutation) -> None:
        self.backend.rollback_mutation(mutation, allowed_roots=self.rollback_roots())

    def test_bootstrap_moves_every_residue_class_and_keeps_evidence_lock_and_overrides(self) -> None:
        self.seed_full()
        self.stamp()
        mutation, evidence = self.execute()
        self.assertTrue(evidence.ok)
        self.assertTrue(mutation.changed)
        for rel in RESIDUE_FILES + RESIDUE_DIRS_WITH_CHILD:
            with self.subTest(rel=rel):
                self.assertFalse(self.present(rel))
        for rel in KEPT_FILES:
            with self.subTest(kept=rel):
                self.assertTrue(self.present(rel))
        q = f"recovery/.installer-quarantine-{STAMP}"
        self.assertTrue(self.present(f"{q}/runtime-controller/runtime.json"))
        self.assertTrue(self.present(f"{q}/runtime-controller/initial-viability-invocations/child.json"))
        self.assertTrue(self.present(f"{q}/persona-root/staged-cutover-1.json"))
        self.assertTrue(self.present(f"{q}/persona-root/staged-cutover-2.json"))
        self.assertTrue(self.present(f"{q}/persona-root/runtimes/child.json"))
        self.assertTrue(self.present(f"{q}/state-root/recovery.policyskip.json"))
        self.assertEqual(mutation.before["stamp"], STAMP)
        self.assertEqual(self.backend.can_rollback(mutation).classification, "owned")

    def test_rollback_restores_the_tree_exactly(self) -> None:
        self.seed_full()
        before = self.snapshot()
        mutation, _ = self.execute()
        self.assertNotEqual(self.snapshot(), before)
        self.rollback(mutation)
        self.assertEqual(self.snapshot(), before)

    def test_rollback_refuses_on_an_occupied_destination_and_moves_nothing(self) -> None:
        self.seed_full()
        mutation, _ = self.execute()
        self.put_file("recovery/active-runtime.json", "the controller re-created this")
        check = self.backend.can_rollback(mutation)
        self.assertFalse(check.ok)
        self.assertEqual(check.classification, "drifted")
        tree = self.snapshot()
        with self.assertRaises(OperationFailed) as caught:
            self.rollback(mutation)
        self.assertIn("active-runtime.json", str(caught.exception))
        self.assertEqual(self.snapshot(), tree)  # nothing was half-restored

    def test_rollback_refuses_when_a_quarantined_copy_is_gone(self) -> None:
        self.seed_full()
        self.stamp()
        mutation, _ = self.execute()
        check = _pim._quarantine_can_rollback(mutation.before, lambda p: not p.endswith("/runtime.json"))
        self.assertFalse(check.ok)
        self.assertEqual(check.classification, "drifted")

    def test_no_residue_is_an_unchanged_noop(self) -> None:
        for rel in KEPT_FILES:
            self.put_file(rel)
        before = self.snapshot()
        self.assertEqual(self.backend.preflight(self.op).classification, "absent")
        mutation, evidence = self.execute()
        self.assertFalse(mutation.changed)
        self.assertEqual(mutation.before["state"], "absent")
        self.assertTrue(evidence.ok)
        self.assertEqual(self.snapshot(), before)
        self.rollback(mutation)
        self.assertEqual(self.snapshot(), before)

    def test_dry_run_is_read_only_and_names_what_would_move(self) -> None:
        self.seed_full()
        before = self.snapshot()
        result = self.backend.preflight(self.op)
        self.assertTrue(result.ok)
        self.assertEqual(result.classification, "ready")
        for needle in ("runtime-controller/runtime.json", "persona-root/active-runtime.json",
                       "state-root/recovery.json", "persona-root/staged-cutover-1.json"):
            self.assertIn(needle, result.detail)
        for kept in ("controller-evidence", "runtime-operation.lock", "overrides"):
            self.assertNotIn(kept, result.detail)
        self.assertEqual(self.snapshot(), before)

    def test_converged_live_seat_moves_nothing_and_the_dry_run_says_so(self) -> None:
        self.seed_full()
        self.set_pending(False)  # consume timer enabled: unit-fire is NOT a bootstrap fire
        before = self.snapshot()
        result = self.backend.preflight(self.op)
        self.assertTrue(result.ok)
        self.assertEqual(result.classification, "converged")
        self.assertIn("not a bootstrap fire", result.detail)
        mutation, evidence = self.execute()
        self.assertFalse(mutation.changed)
        self.assertEqual(mutation.before["state"], "skipped")
        self.assertIn("not a bootstrap fire", mutation.before["reason"])
        self.assertTrue(evidence.ok)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backend.can_rollback(mutation).classification, "not-applied")
        self.rollback(mutation)
        self.assertEqual(self.snapshot(), before)

    CONTROLLER = "pantheon-runtime-controller@recovery.service"
    CONSUME = "pantheon-persona-consume@recovery.service"

    def test_live_controller_with_a_disabled_timer_is_refused_and_nothing_moves(self) -> None:
        # reviewer B1: an operator paused consume (timer disabled) but the controller is running
        self.seed_full()
        before = self.snapshot()
        for state in ("active", "activating", "reloading", "deactivating", "mystery"):
            with self.subTest(controller=state):
                self.set_unit_state(self.CONTROLLER, state)
                result = self.backend.preflight(self.op)
                self.assertFalse(result.ok)
                self.assertEqual(result.classification, "conflicting")
                self.assertIn(self.CONTROLLER, result.detail)
                with self.assertRaises(OperationFailed):
                    self.execute()
                self.assertEqual(self.snapshot(), before)

    def test_live_consume_service_is_refused_too(self) -> None:
        self.seed_full()
        before = self.snapshot()
        self.set_unit_state(self.CONSUME, "active")
        self.assertFalse(self.backend.preflight(self.op).ok)
        with self.assertRaises(OperationFailed):
            self.execute()
        self.assertEqual(self.snapshot(), before)

    def test_unreadable_controller_state_is_refused_and_nothing_moves(self) -> None:
        self.seed_full()
        before = self.snapshot()
        self.set_unit_state(self.CONTROLLER, OperationFailed("show failed"))
        result = self.backend.preflight(self.op)
        self.assertFalse(result.ok)
        self.assertIn("unreadable", result.detail)
        with self.assertRaises(OperationFailed):
            self.execute()
        self.assertEqual(self.snapshot(), before)

    def test_inactive_or_failed_controller_does_not_block(self) -> None:
        self.seed_full()
        for state in ("inactive", "failed"):
            with self.subTest(controller=state):
                self.set_unit_state(self.CONTROLLER, state)
                self.assertEqual(self.backend.preflight(self.op).classification, "ready")

    def test_dry_run_and_execute_use_the_same_liveness_helpers(self) -> None:
        # reviewer follow-up: ONE predicate, the same read in preflight and execute
        self.seed_full()
        with mock.patch.object(_pim, "_runtime_liveness_refusal", wraps=_pim._runtime_liveness_refusal) as gate:
            self.backend.preflight(self.op)
            self.assertEqual(gate.call_count, 1)
            self.execute()
            self.assertEqual(gate.call_count, 2)
        self.assertEqual(gate.call_args_list[0], gate.call_args_list[1])

    def test_enabled_timer_with_live_controller_is_the_converged_noop_not_a_refusal(self) -> None:
        self.seed_full()
        self.set_pending(False)
        self.set_unit_state(self.CONTROLLER, "active")
        before = self.snapshot()
        self.assertEqual(self.backend.preflight(self.op).classification, "converged")
        mutation, _ = self.execute()
        self.assertFalse(mutation.changed)
        self.assertEqual(self.snapshot(), before)

    def test_bootstrap_seat_moves_where_the_converged_seat_does_not(self) -> None:
        self.seed_full()
        self.set_pending(True)
        mutation, _ = self.execute()
        self.assertTrue(mutation.changed)

    def test_malformed_desired_is_refused(self) -> None:
        for desired in ({"persona_state_root": "relative", "persona": PERSONA, "timer": TIMER},
                        {"persona_state_root": self.root, "persona": "../x", "timer": TIMER},
                        {"persona_state_root": self.root, "persona": PERSONA, "timer": ""}):
            with self.subTest(desired=desired):
                bad = dataclasses.replace(self.op, desired=desired)
                self.assertFalse(self.backend.preflight(bad).ok)
                with self.assertRaises(OperationFailed):
                    self.backend.execute(bad, self.ctx())

    def test_verify_survives_a_fresh_runtime_json_from_the_bootstrap(self) -> None:
        self.seed_full()
        mutation, evidence = self.execute()
        self.put_file("recovery/runtime-controller/runtime.json", "fresh, written by the fire")
        verified = self.backend.verify_operation(self.op, staged_evidence=evidence)
        self.assertTrue(verified.ok, verified.detail)

    def test_malformed_receipt_cannot_escape_the_state_root(self) -> None:
        self.seed_full()
        mutation, _ = self.execute()
        for moved in ('[["persona-root","../../etc"]]', '[["nowhere","x"]]', '[["persona-root","a/b"]]', "not json"):
            with self.subTest(moved=moved):
                forged = dataclasses.replace(mutation, before={**mutation.before, "moved": moved})
                self.assertFalse(self.backend.can_rollback(forged).ok)
                with self.assertRaises(OperationFailed):
                    self.rollback(forged)


class SimulatedQuarantineTests(QuarantineContract, SimQDriver, unittest.TestCase):
    pass


class LocalQuarantineTests(QuarantineContract, LocalQDriver, unittest.TestCase):
    def test_a_failed_move_restores_what_it_already_moved(self) -> None:
        self.seed_full()
        before = self.snapshot()
        real = os.rename
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 4:
                raise OSError("simulated disk error")
            return real(src, dst)

        with mock.patch.object(_pim.os, "rename", side_effect=flaky):
            with self.assertRaises(OperationFailed):
                self.execute()
        quarantine = Path(self.root) / "recovery" / f".installer-quarantine-{STAMP}"
        leftovers = sorted(p for p in quarantine.rglob("*") if p.is_file()) if quarantine.exists() else []
        self.assertEqual(leftovers, [])
        for rel, content in before.items():
            self.assertTrue(self.present(rel), rel)

    def test_forged_receipt_cannot_rename_outside_the_allowed_roots(self) -> None:
        # reviewer B2: persona_state_root comes from the stored receipt, so it is untrusted
        outside = Path(self.temp.name + "-outside")
        outside.mkdir()
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        (outside / "recovery").mkdir()
        (outside / "recovery" / f".installer-quarantine-{STAMP}" / "persona-root").mkdir(parents=True)
        (outside / "recovery" / f".installer-quarantine-{STAMP}" / "persona-root" / "active-runtime.json").write_text("secret")
        forged = _pim.MutationRecord(
            QID, QKIND, f"{outside}/recovery", current_user(), True,
            {"state": "present", "persona_state_root": str(outside), "persona": "recovery", "stamp": STAMP,
             "moved": '[["persona-root","active-runtime.json"]]'},
            "quarantined",
        )
        with self.assertRaises(OperationFailed) as caught:
            self.backend.rollback_mutation(forged, allowed_roots=(str(self.tmp),))
        self.assertIn("outside allowed roots", str(caught.exception))
        self.assertFalse((outside / "recovery" / "active-runtime.json").exists())
        # a receipt whose target disagrees with persona_state_root/persona is refused even inside the roots
        self.seed_full()
        mutation, _ = self.execute()
        mismatched = dataclasses.replace(mutation, target=str(self.tmp / "elsewhere"))
        self.assertFalse(self.backend.can_rollback(mismatched).ok)
        before = self.snapshot()
        with self.assertRaises(OperationFailed):
            self.backend.rollback_mutation(mismatched, allowed_roots=(str(self.tmp),))
        self.assertEqual(self.snapshot(), before)

    def test_unreadable_timer_state_fails_closed(self) -> None:
        self.seed_full()

        def boom(unit, timeout=None):
            raise _pim._us.UserManagerProtocolError("bad")

        self.backend._user_manager = types.SimpleNamespace(unit_file_enablement=boom)
        self.assertFalse(self.backend.preflight(self.op).ok)
        before = self.snapshot()
        with self.assertRaises(OperationFailed):
            self.execute()
        self.assertEqual(self.snapshot(), before)


# ---------------------------------------------------------------------------
# workspace trust
# ---------------------------------------------------------------------------
class SimTDriver:
    TARGET = "/home/seat/.claude.json"
    PROJECT = "/home/seat/pantheon-config"

    def setUp(self) -> None:
        self.backend = SimulatedHostBackend()
        self.target, self.project = self.TARGET, self.PROJECT
        self.op = make_t_op(self.target, self.project)

    def seed(self, text: str, mode: str = "0640") -> None:
        self.backend.seed_file(self.target, text, mode)

    def read(self) -> str | None:
        entry = self.backend.entries.get(self.target)
        return base64.b64decode(entry["content_b64"]).decode() if entry and entry["state"] == "file" else None

    def mode(self) -> str:
        return self.backend.entries[self.target]["mode"]

    def ctx(self) -> ExecutionContext:
        return ExecutionContext("all-in-one")

    def rollback_roots(self):
        return ()


class LocalTDriver:
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.backend = LocalHostBackend()
        self.target = str(self.tmp / ".claude.json")
        self.project = str(self.tmp / "pantheon-config")
        self.op = make_t_op(self.target, self.project)

    def seed(self, text: str, mode: str = "0640") -> None:
        path = Path(self.target)
        path.write_text(text)
        path.chmod(int(mode, 8))

    def read(self) -> str | None:
        path = Path(self.target)
        return path.read_text() if path.exists() else None

    def mode(self) -> str:
        return format(stat.S_IMODE(os.stat(self.target).st_mode), "04o")

    def ctx(self) -> ExecutionContext:
        return ExecutionContext("all-in-one", allowed_roots=(str(self.tmp),))

    def rollback_roots(self):
        return (str(self.tmp),)


class TrustContract:
    def execute(self):
        return self.backend.execute(self.op, self.ctx())

    def rollback(self, mutation) -> None:
        self.backend.rollback_mutation(mutation, allowed_roots=self.rollback_roots())

    def doc(self):
        text = self.read()
        return None if text is None else json.loads(text)

    def test_absent_file_is_created_0600_owned(self) -> None:
        self.assertTrue(self.backend.preflight(self.op).ok)
        mutation, evidence = self.execute()
        self.assertTrue(evidence.ok)
        self.assertEqual(self.doc(), {"projects": {self.project: {KEY: True}}})
        self.assertEqual(self.mode(), "0600")
        self.assertTrue(mutation.changed)
        self.assertEqual((mutation.before["owned"], mutation.before["prior"]), ("true", "absent-file"))

    def test_absent_projects_preserves_every_other_key_and_the_mode(self) -> None:
        self.seed('{\n  "zeta": 1,\n  "numStartups": 7,\n  "mcpServers": {"a": {}}\n}\n', "0640")
        mutation, _ = self.execute()
        doc = self.doc()
        self.assertEqual(list(doc), ["zeta", "numStartups", "mcpServers", "projects"])
        self.assertEqual(doc["mcpServers"], {"a": {}})
        self.assertEqual(self.mode(), "0640")
        self.assertTrue(self.read().endswith("\n"))
        self.assertEqual(mutation.before["prior"], "absent-projects")

    def test_absent_entry_preserves_other_projects(self) -> None:
        self.seed(json.dumps({"projects": {"/other": {KEY: False, "x": 1}}}))
        mutation, _ = self.execute()
        self.assertEqual(self.doc()["projects"]["/other"], {KEY: False, "x": 1})
        self.assertIs(self.doc()["projects"][self.project][KEY], True)
        self.assertEqual(mutation.before["prior"], "absent-entry")

    def test_entry_with_other_keys_keeps_them_and_their_order(self) -> None:
        self.seed(json.dumps({"projects": {self.project: {"allowedTools": ["a"], "lastCost": 1.5}}}))
        mutation, _ = self.execute()
        self.assertEqual(list(self.doc()["projects"][self.project]), ["allowedTools", "lastCost", KEY])
        self.assertEqual(mutation.before["prior"], "absent-key")

    def test_already_true_is_a_byte_identical_noop_not_owned(self) -> None:
        text = json.dumps({"projects": {self.project: {KEY: True}}}, indent=4) + "\n"
        self.seed(text)
        self.assertEqual(self.backend.preflight(self.op).classification, "matching")
        mutation, evidence = self.execute()
        self.assertEqual(self.read(), text)
        self.assertFalse(mutation.changed)
        self.assertEqual(mutation.before["owned"], "false")
        self.assertTrue(evidence.ok)
        self.assertEqual(self.backend.can_rollback(mutation).classification, "not-applied")
        self.rollback(mutation)
        self.assertEqual(self.read(), text)

    def test_false_flips_to_true_and_rollback_restores_false(self) -> None:
        self.seed(json.dumps({"projects": {self.project: {KEY: False, "k": 1}}}))
        mutation, _ = self.execute()
        self.assertIs(self.doc()["projects"][self.project][KEY], True)
        self.assertEqual(mutation.before["prior"], "false")
        self.rollback(mutation)
        self.assertEqual(self.doc(), {"projects": {self.project: {KEY: False, "k": 1}}})

    def test_rollback_absent_key_removes_only_the_key(self) -> None:
        self.seed(json.dumps({"projects": {self.project: {"keep": 1}}}))
        mutation, _ = self.execute()
        self.rollback(mutation)
        self.assertEqual(self.doc(), {"projects": {self.project: {"keep": 1}}})

    def test_rollback_absent_entry_removes_the_entry_but_not_other_projects(self) -> None:
        self.seed(json.dumps({"projects": {"/other": {"x": 1}}}))
        mutation, _ = self.execute()
        self.rollback(mutation)
        self.assertEqual(self.doc(), {"projects": {"/other": {"x": 1}}})

    def test_rollback_absent_projects_removes_the_projects_object(self) -> None:
        self.seed('{"a": 1}')
        mutation, _ = self.execute()
        self.rollback(mutation)
        self.assertEqual(self.doc(), {"a": 1})

    def test_rollback_of_a_created_file_deletes_it_when_only_our_write_was_in_it(self) -> None:
        mutation, _ = self.execute()
        self.assertEqual(self.backend.can_rollback(mutation).classification, "owned")
        self.rollback(mutation)
        self.assertIsNone(self.read())

    def test_rollback_of_a_created_file_leaves_what_else_now_lives_in_it(self) -> None:
        mutation, _ = self.execute()
        doc = self.doc()
        doc["firstRun"] = True  # claude wrote its own state after us
        self.seed(json.dumps(doc))
        self.rollback(mutation)
        self.assertEqual(self.doc(), {"firstRun": True})

    def test_rollback_keeps_a_sibling_project_added_after_our_write(self) -> None:
        mutation, _ = self.execute()
        doc = self.doc()
        doc["projects"]["/later"] = {"x": 1}
        self.seed(json.dumps(doc))
        self.rollback(mutation)
        self.assertEqual(self.doc(), {"projects": {"/later": {"x": 1}}})

    def test_rollback_refuses_when_the_value_changed_after_apply(self) -> None:
        self.seed(json.dumps({"projects": {self.project: {"k": 1}}}))
        mutation, _ = self.execute()
        text = json.dumps({"projects": {self.project: {"k": 1, KEY: False}}})
        self.seed(text)
        check = self.backend.can_rollback(mutation)
        self.assertFalse(check.ok)
        self.assertEqual(check.classification, "drifted")
        with self.assertRaises(OperationFailed):
            self.rollback(mutation)
        self.assertEqual(self.read(), text)

    def test_rollback_when_already_gone_is_not_applied(self) -> None:
        self.seed("{}")
        mutation, _ = self.execute()
        self.seed(json.dumps({"projects": {}}))
        self.assertEqual(self.backend.can_rollback(mutation).classification, "not-applied")

    def test_malformed_shapes_are_refused_and_untouched(self) -> None:
        for text in ("{not json", "[]", "3", '{"projects": []}', '{"projects": {"%s": 3}}',
                     '{"projects": {"%s": {"%s": "yes"}}}', '{"a": 1, "a": 2}'):
            text = text.replace("%s", self.project, 1).replace("%s", KEY, 1)
            with self.subTest(text=text):
                self.seed(text)
                self.assertFalse(self.backend.preflight(self.op).ok)
                with self.assertRaises(OperationFailed):
                    self.execute()
                self.assertEqual(self.read(), text)

    def test_verify_operation_reflects_the_trust(self) -> None:
        self.assertFalse(self.backend.verify_operation(self.op).ok)
        self.execute()
        self.assertTrue(self.backend.verify_operation(self.op).ok)

    def test_preflight_is_read_only(self) -> None:
        self.seed('{"a": 1}')
        before = self.read()
        self.backend.preflight(self.op)
        self.assertEqual(self.read(), before)


class SimulatedTrustTests(TrustContract, SimTDriver, unittest.TestCase):
    pass


class LocalTrustTests(TrustContract, LocalTDriver, unittest.TestCase):
    def test_symlinked_file_is_refused(self) -> None:
        real = self.tmp / "elsewhere.json"
        real.write_text("{}")
        os.symlink(real, self.target)
        self.assertFalse(self.backend.preflight(self.op).ok)
        with self.assertRaises(OperationFailed):
            self.execute()
        self.assertEqual(real.read_text(), "{}")

    def test_concurrent_writer_between_read_and_rename_aborts_without_clobbering(self) -> None:
        self.seed('{"mine": 0}')
        original_with = _pim._trust_with

        def racing(raw, project):
            result = original_with(raw, project)
            Path(self.target).write_text('{"theirs": 1}')  # claude wrote between our read and rename
            return result

        with mock.patch.object(_pim, "_trust_with", side_effect=racing):
            with self.assertRaises(OperationFailed):
                self.execute()
        self.assertEqual(Path(self.target).read_text(), '{"theirs": 1}')
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [".claude.json"])

    def test_rollback_delete_is_cas_checked(self) -> None:
        mutation, _ = self.execute()
        original_load = _pim._settings_env_load
        state = {"n": 0}

        def load(path):
            state["n"] += 1
            if state["n"] == 2:  # the re-read right before the unlink
                Path(self.target).write_text('{"claude": "wrote"}')
            return original_load(path)

        with mock.patch.object(_pim, "_settings_env_load", side_effect=load):
            with self.assertRaises(OperationFailed):
                self.rollback(mutation)
        self.assertEqual(Path(self.target).read_text(), '{"claude": "wrote"}')


# ---------------------------------------------------------------------------
# plan shape, wiring and dry-run/execute fidelity
# ---------------------------------------------------------------------------
class PlanShapeTests(PeriodicCase):
    def test_kinds_are_seat_local_residue_outside_the_envelope(self) -> None:
        from tools.host_control.planner import SEAT_LOCAL_RESIDUE_LIMIT, partition_installer_plan

        for kind in (QKIND, TKIND):
            self.assertIn(kind, _pim.SEAT_LOCAL_RESIDUE_KINDS)
            self.assertIn(kind, OUT_OF_ENVELOPE_KINDS)
            self.assertNotIn(kind, EXECUTOR_KIND_CLASSES)
            self.assertIn(kind, _pim.ACTIVATION_OPERATION_KINDS)  # reviewer B3: run at activation, not stage
        broker, residue = partition_installer_plan(self.plan("recovery"))
        self.assertLessEqual({QID, TID}, {o.operation_id for o in residue})
        self.assertFalse({QID, TID} & {o.operation_id for o in broker.operations})
        self.assertLessEqual(len(residue), SEAT_LOCAL_RESIDUE_LIMIT)

    def test_trust_then_quarantine_precede_every_launch_and_the_fire(self) -> None:
        ids = self.ids(self.plan("recovery"))
        at = ids.index
        self.assertLess(at("host.user-manager"), at(TID))
        self.assertLess(at(TID), at(QID))
        for later in ("service.ho-resumer.decommission", "harness.direct-fire", "connective.unit-fire",
                      "service.runtime-controller.enable-observe"):
            self.assertLess(at(QID), at(later))

    def test_op_desired_is_stable_inputs_only(self) -> None:
        plan = self.plan("recovery")
        home = self.root / "projection" / "home" / "claude-fallback"
        q, t = self.op(plan, QID), self.op(plan, TID)
        self.assertEqual(q.desired, {
            "persona_state_root": str(home / ".local" / "state" / "pantheon" / "personas"),
            "persona": "recovery", "timer": "pantheon-persona-consume@recovery.timer",
        })
        self.assertEqual(t.target, str(home / ".claude.json"))
        self.assertEqual(t.desired, {"project_path": str(home / "pantheon-config"), "key": KEY})
        self.assertEqual(self.plan("recovery").digest, plan.digest)  # no time-varying input

    def test_unit_fire_names_the_ops_that_clear_its_blockers(self) -> None:
        fire = self.op(self.plan("recovery"), "connective.unit-fire")
        self.assertEqual(fire.desired["residue_cleared_by"], QID)
        self.assertEqual(fire.desired["trust_set_by"], TID)

    def test_codex_only_seat_gets_the_quarantine_but_no_trust_op(self) -> None:
        plan = self.plan("consultant")
        self.assertIn(QID, self.ids(plan))
        self.assertNotIn(TID, self.ids(plan))
        fire = self.op(plan, "connective.unit-fire")
        self.assertEqual(fire.desired["residue_cleared_by"], QID)
        self.assertNotIn("trust_set_by", fire.desired)

    def test_bootstrap_residue_is_one_definition_for_the_gate_and_the_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in RESIDUE_FILES:
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")
            rendered = _pim._bootstrap_residue(root, PERSONA)
            entries = _pim._bootstrap_residue_entries(root, PERSONA)
            self.assertEqual(len(rendered), len(entries))
            self.assertEqual(sorted(rendered), sorted(RESIDUE_FILES))


class FidelityTests(PeriodicCase):
    """Dry run == execute: residue present and trust missing dry-runs to pass (the fire is
    predicted clear by the planned clearing ops) and then executes green."""

    def seed(self, backend, plan, *, trusted: bool):
        q, t = self.op(plan, QID), self.op(plan, TID)
        root = q.desired["persona_state_root"]
        for rel in RESIDUE_FILES:
            backend.seed_file(f"{root}/{rel}", "{}", "0600")
        if trusted:
            backend.seed_file(t.target, json.dumps({"projects": {t.desired["project_path"]: {KEY: True}}}), "0600")
        return root

    def test_residue_present_and_trust_missing_dry_runs_pass_and_executes_green(self) -> None:
        plan = self.plan("recovery")
        backend = SimulatedHostBackend()
        backend.model_bootstrap_state = True
        root = self.seed(backend, plan, trusted=False)
        fire = self.op(plan, "connective.unit-fire")
        # the dry run predicts the planned ops clear both blockers ...
        self.assertTrue(backend.preflight(fire).ok, backend.preflight(fire).detail)
        # ... while the execute-time check, run before them, still refuses for real
        self.assertTrue(backend._sim_bootstrap_blocker(fire, preflight=False))
        report = self.installer.apply(plan, plan.digest, backend, self.actor(plan))
        self.assertEqual(report.status, "applied", report.errors)
        self.assertFalse(any(k == f"{root}/recovery.json" for k in backend.entries))
        self.assertIsNone(backend._sim_bootstrap_blocker(fire, preflight=False))
        rolled = self.installer.rollback(
            report.receipt, backend, self.actor(plan), plan=plan, manifest=self.manifest
        )
        self.assertEqual(rolled.status, "rolled-back", rolled)
        for rel in RESIDUE_FILES:
            self.assertEqual(backend.entries[f"{root}/{rel}"]["state"], "file", rel)

    def test_without_the_clearing_ops_the_dry_run_refuses_like_execute(self) -> None:
        plan = self.plan("recovery")
        backend = SimulatedHostBackend()
        backend.model_bootstrap_state = True
        self.seed(backend, plan, trusted=False)
        fire = self.op(plan, "connective.unit-fire")
        bare = dataclasses.replace(
            fire, desired={k: v for k, v in fire.desired.items() if k not in ("residue_cleared_by", "trust_set_by")}
        )
        self.assertFalse(backend.preflight(bare).ok)
        self.assertEqual(backend.preflight(bare).detail, backend._sim_bootstrap_blocker(bare, preflight=False))

    def test_staged_prefix_does_not_run_the_ops_and_activate_does(self) -> None:
        # reviewer B3: stage_disabled must not touch runtime state or ~/.claude.json (the operator's
        # teardown has not happened yet); activate_staged runs both, before the bootstrap fire.
        plan = self.plan("recovery")
        backend = SimulatedHostBackend()
        backend.model_bootstrap_state = True
        root = self.seed(backend, plan, trusted=False)
        trust = self.op(plan, TID)
        staged = self.installer.stage_disabled(plan, plan.digest, backend, self.actor(plan))
        self.assertIn(staged.status, {"applied", "noop"}, staged.errors)
        self.assertNotIn(QID, backend.events)
        self.assertNotIn(TID, backend.events)
        self.assertNotIn(QID, {m.operation_id for m in staged.receipt.mutations})
        self.assertEqual(backend.entries[f"{root}/recovery.json"]["state"], "file")
        self.assertNotIn(trust.target, backend.entries)
        activated = self.installer.activate_staged(plan, plan.digest, staged.receipt, backend, self.actor(plan))
        self.assertEqual(activated.status, "applied", activated.errors)
        order = [e for e in backend.events if e in (TID, QID, "connective.unit-fire")]
        self.assertEqual(order, [TID, QID, "connective.unit-fire"])
        self.assertNotIn(f"{root}/recovery.json", backend.entries)
        self.assertIn(QID, {m.operation_id for m in activated.receipt.mutations})

    def test_new_ops_preflight_under_every_workflow_context_and_scope(self) -> None:
        # T-5472 rebase: preflight(op, context) must work for the new kinds, and the workflow
        # scope filter must evaluate them (execution_scope "all") under every workflow.
        from tools.persona_installer import (
            EXECUTION_CONTEXT_ALL_IN_ONE,
            EXECUTION_CONTEXT_STAGE_PREFIX,
            EXECUTION_CONTEXT_STAGED_ACTIVATE,
            preflight_in_context,
            workflow_scopes,
        )

        plan = self.plan("recovery")
        for workflow, context in (
            ("all-in-one", EXECUTION_CONTEXT_ALL_IN_ONE),
            ("stage-prefix", EXECUTION_CONTEXT_STAGE_PREFIX),
            ("staged-activate", EXECUTION_CONTEXT_STAGED_ACTIVATE),
        ):
            for op_id in (QID, TID):
                operation = self.op(plan, op_id)
                with self.subTest(workflow=workflow, op=op_id):
                    self.assertIn(operation.execution_scope, workflow_scopes(workflow))
                    backend = SimulatedHostBackend()
                    self.assertTrue(preflight_in_context(backend, operation, context).ok)
                    self.assertEqual(
                        preflight_in_context(backend, operation, context),
                        backend.preflight(operation),
                    )

    def test_converged_live_seat_apply_leaves_runtime_state_in_place(self) -> None:
        plan = self.plan("recovery")
        backend = SimulatedHostBackend()
        backend.timer_enabled = True  # converged: the consume timer is already enabled
        q = self.op(plan, QID)
        root = q.desired["persona_state_root"]
        backend.seed_file(f"{root}/recovery/runtime-controller/runtime.json", "live", "0600")
        _, evidence = backend.execute(q, EXECUTION_ALL)
        self.assertTrue(evidence.ok)
        self.assertEqual(backend.entries[f"{root}/recovery/runtime-controller/runtime.json"]["state"], "file")
        self.assertEqual(evidence.evidence["residue"], "left-in-place")


EXECUTION_ALL = ExecutionContext("all-in-one")


class LocalBlockerPreflightModeTests(unittest.TestCase):
    """The Local blocker predicts clear only in the dry run, only for what the plan clears."""

    def test_preflight_mode_skips_exactly_the_cleared_checks(self) -> None:
        from tools.test_persona_installer_converge import BootstrapGateTests

        # reuse the converge fixture's real gates backend on a standalone fire op
        case = BootstrapGateTests("test_ready_host_passes_the_gates")
        case.setUp()
        self.addCleanup(case.doCleanups)
        persona = case.op_.provenance["persona"]
        (case.home / ".claude.json").unlink()
        (case.state_root / persona).mkdir(parents=True, exist_ok=True)
        (case.state_root / persona / "active-runtime.json").write_text("{}")
        backend, _ = case.gates()
        planned = dataclasses.replace(
            case.op_, desired={**case.op_.desired, "residue_cleared_by": QID, "trust_set_by": TID}
        )
        self.assertIsNone(self._blocker(backend, planned, preflight=True))
        self.assertIn("workspace trust missing", self._blocker(backend, planned, preflight=False))
        trust_only = dataclasses.replace(case.op_, desired={**case.op_.desired, "trust_set_by": TID})
        self.assertIn("control-plane residue", self._blocker(backend, trust_only, preflight=True))

    @staticmethod
    def _blocker(backend, operation, *, preflight: bool):
        backend._blocker_preflight_mode = preflight
        try:
            return backend._unit_fire_bootstrap_blocker(operation)
        finally:
            backend._blocker_preflight_mode = False


if __name__ == "__main__":
    unittest.main()
