"""Tests for tools.bringup_acceptance.registry (T-2556 phase 11 / T-2561)."""

from __future__ import annotations

import sys
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest import mock

from tools.bringup_acceptance.registry import (
    BUILD_ENTRY_POINT,
    OPTIONALS_PACKAGE,
    discover_optional_modules,
    run_optional_module,
    run_persona_optionals,
)
from tools.bringup_acceptance.types import (
    HardStop,
    MatrixRow,
    ModuleDeclaration,
    ModuleEvidence,
    RestoreEvidence,
    RunContext,
    TargetInventory,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeAcceptanceModule:
    """A configurable AcceptanceModule double with call-order tracking.

    ``tier``/``id`` are real instance attributes (never omitted) so a test
    that wants to fail the EXPLICIT tier/id checks in registry.py must do so
    by setting a WRONG value, not by leaving the attribute off -- proving
    those checks (not bare isinstance/hasattr) are what catches it.
    """

    def __init__(
        self,
        *,
        module_id: str = "fake-module",
        tier: str = "optional",
        applies: bool = True,
        raise_on_prove: Exception | None = None,
        raise_on_restore: Exception | None = None,
        calls: list[str] | None = None,
    ) -> None:
        self.id = module_id
        self.tier = tier
        self._applies = applies
        self._raise_on_prove = raise_on_prove
        self._raise_on_restore = raise_on_restore
        self.calls = calls if calls is not None else []

    def declare(self, target: TargetInventory) -> ModuleDeclaration:
        self.calls.append("declare")
        return ModuleDeclaration(
            module_id=self.id, tier=self.tier, applies=self._applies,
            reason=f"{self.id} applies={self._applies}",
        )

    def prove(self, ctx: RunContext) -> ModuleEvidence:
        self.calls.append("prove")
        if self._raise_on_prove is not None:
            raise self._raise_on_prove
        return ModuleEvidence(
            module_id=self.id, status="proven", trace_path=Path("/tmp/trace.jsonl"),
            evidence_paths=(), detail={},
        )

    def restore(self, ctx: RunContext) -> RestoreEvidence:
        self.calls.append("restore")
        if self._raise_on_restore is not None:
            raise self._raise_on_restore
        return RestoreEvidence(module_id=self.id, restored=True, detail={})

    def report(self, evidence: ModuleEvidence) -> MatrixRow:
        self.calls.append("report")
        return MatrixRow(
            module_id=self.id, tier=self.tier, status=evidence.status,
            summary=f"{self.id}: {evidence.status}", evidence_refs=evidence.evidence_paths,
        )


_TARGET = TargetInventory(persona="test-persona", entries=())
_CTX = RunContext(
    run_id="run-1", persona="test-persona", account="test-account", harness="test-harness",
    profile="test-profile", hub_path=Path("/tmp/hub"), state_path=Path("/tmp/state"),
    evidence_root=Path("/tmp/evidence"), baseline_ref="test-ref",
)


@contextmanager
def _fake_plugin(persona: str, **attrs: object) -> Iterator[types.ModuleType]:
    """Inject a fake module at OPTIONALS_PACKAGE.<persona> via sys.modules,
    never touching the real optionals/ package on disk. import_module()
    checks sys.modules first, so this is a faithful double of a real
    plug-in file for discover_optional_modules' own purposes."""

    module_name = f"{OPTIONALS_PACKAGE}.{persona}"
    fake = types.ModuleType(module_name)
    for key, value in attrs.items():
        setattr(fake, key, value)
    sys.modules[module_name] = fake
    try:
        yield fake
    finally:
        sys.modules.pop(module_name, None)


class DiscoverOptionalModulesTest(unittest.TestCase):
    def test_bare_persona_with_no_plugin_file_returns_empty_tuple(self) -> None:
        # No sys.modules injection -- this persona genuinely has no file on
        # disk under optionals/. Absence is the core-only case, not an error.
        self.assertEqual(discover_optional_modules("no-such-persona-t2561"), ())

    def test_invalid_persona_token_is_rejected(self) -> None:
        with self.assertRaises(HardStop):
            discover_optional_modules("../escape")

    def test_discovers_modules_from_a_fake_plugin_via_build_entry_point(self) -> None:
        module = _FakeAcceptanceModule(module_id="fake-one")
        with _fake_plugin("fakepersona", **{BUILD_ENTRY_POINT: lambda: (module,)}):
            discovered = discover_optional_modules("fakepersona")
        self.assertEqual(discovered, (module,))

    def test_missing_build_entry_point_is_a_hard_stop(self) -> None:
        with _fake_plugin("fakepersona"):
            with self.assertRaises(HardStop):
                discover_optional_modules("fakepersona")

    def test_non_callable_build_entry_point_is_a_hard_stop(self) -> None:
        with _fake_plugin("fakepersona", **{BUILD_ENTRY_POINT: "not-callable"}):
            with self.assertRaises(HardStop):
                discover_optional_modules("fakepersona")

    def test_non_conforming_instance_is_a_hard_stop(self) -> None:
        # Passes method-presence isinstance (declare/prove/restore/report all
        # exist) but id is None -- the explicit non-empty-module_id check,
        # not isinstance, must be what catches this.
        module = _FakeAcceptanceModule(module_id=None)  # type: ignore[arg-type]
        with _fake_plugin("fakepersona", **{BUILD_ENTRY_POINT: lambda: (module,)}):
            with self.assertRaises(HardStop):
                discover_optional_modules("fakepersona")

    def test_wrong_tier_declared_by_a_plugin_module_is_a_hard_stop(self) -> None:
        # Passes isinstance (tier attribute present, method-presence intact)
        # but the VALUE is "core" -- the explicit tier=="optional" check,
        # not isinstance, must be what catches this.
        module = _FakeAcceptanceModule(module_id="fake-core", tier="core")
        with _fake_plugin("fakepersona", **{BUILD_ENTRY_POINT: lambda: (module,)}):
            with self.assertRaises(HardStop):
                discover_optional_modules("fakepersona")

    def test_duplicate_module_ids_within_one_plugin_is_a_hard_stop(self) -> None:
        first = _FakeAcceptanceModule(module_id="dup")
        second = _FakeAcceptanceModule(module_id="dup")
        with _fake_plugin("fakepersona", **{BUILD_ENTRY_POINT: lambda: (first, second)}):
            with self.assertRaises(HardStop):
                discover_optional_modules("fakepersona")

    def test_transitive_import_error_inside_a_real_plugin_propagates(self) -> None:
        # A real plug-in file that itself fails to import some OTHER module
        # must never be silently absorbed as "persona has no optionals" --
        # only a ModuleNotFoundError naming the plug-in's OWN dotted path is
        # the bare-daemon case.
        target_name = f"{OPTIONALS_PACKAGE}.fakepersona"

        def _raise_transitive(name: str, *args: object, **kwargs: object) -> object:
            if name == target_name:
                raise ModuleNotFoundError(
                    f"No module named 'some_other_thing'", name="some_other_thing"
                )
            raise AssertionError(f"unexpected import_module call: {name!r}")

        with mock.patch(
            "tools.bringup_acceptance.registry.importlib.import_module",
            side_effect=_raise_transitive,
        ):
            with self.assertRaises(ModuleNotFoundError):
                discover_optional_modules("fakepersona")


class RunOptionalModuleTest(unittest.TestCase):
    def test_declared_absent_short_circuits_without_calling_prove_restore_report(self) -> None:
        calls: list[str] = []
        module = _FakeAcceptanceModule(module_id="absent-one", applies=False, calls=calls)
        row = run_optional_module(module, target=_TARGET, ctx=_CTX)
        self.assertEqual(row.status, "declared_absent")
        self.assertEqual(row.module_id, "absent-one")
        self.assertEqual(calls, ["declare"])

    def test_applies_true_runs_prove_restore_report_in_order(self) -> None:
        calls: list[str] = []
        module = _FakeAcceptanceModule(module_id="applies-one", applies=True, calls=calls)
        row = run_optional_module(module, target=_TARGET, ctx=_CTX)
        self.assertEqual(calls, ["declare", "prove", "restore", "report"])
        self.assertEqual(row.status, "proven")
        self.assertEqual(row.module_id, "applies-one")

    def test_restore_is_still_called_when_prove_raises_hardstop(self) -> None:
        calls: list[str] = []
        module = _FakeAcceptanceModule(
            module_id="prove-fails", applies=True, calls=calls,
            raise_on_prove=HardStop("prove blew up"),
        )
        with self.assertRaises(HardStop):
            run_optional_module(module, target=_TARGET, ctx=_CTX)
        self.assertEqual(calls, ["declare", "prove", "restore"])

    def test_restore_failure_after_prove_failure_propagates_restore_hardstop(self) -> None:
        calls: list[str] = []
        module = _FakeAcceptanceModule(
            module_id="both-fail", applies=True, calls=calls,
            raise_on_prove=HardStop("prove blew up"),
            raise_on_restore=HardStop("restore residue left behind"),
        )
        with self.assertRaises(HardStop) as caught:
            run_optional_module(module, target=_TARGET, ctx=_CTX)
        self.assertIn("restore residue", str(caught.exception))
        self.assertEqual(calls, ["declare", "prove", "restore"])


class RunPersonaOptionalsTest(unittest.TestCase):
    def test_bare_persona_returns_empty_tuple(self) -> None:
        self.assertEqual(
            run_persona_optionals("no-such-persona-t2561", target=_TARGET, ctx=_CTX), ()
        )

    def test_wires_discover_and_run_together(self) -> None:
        applies_calls: list[str] = []
        absent_calls: list[str] = []
        applies_module = _FakeAcceptanceModule(
            module_id="wired-applies", applies=True, calls=applies_calls
        )
        absent_module = _FakeAcceptanceModule(
            module_id="wired-absent", applies=False, calls=absent_calls
        )
        with _fake_plugin(
            "fakepersona", **{BUILD_ENTRY_POINT: lambda: (applies_module, absent_module)}
        ):
            rows = run_persona_optionals("fakepersona", target=_TARGET, ctx=_CTX)
        self.assertEqual({row.module_id for row in rows}, {"wired-applies", "wired-absent"})
        by_id = {row.module_id: row for row in rows}
        self.assertEqual(by_id["wired-applies"].status, "proven")
        self.assertEqual(by_id["wired-absent"].status, "declared_absent")
        self.assertEqual(applies_calls, ["declare", "prove", "restore", "report"])
        self.assertEqual(absent_calls, ["declare"])


if __name__ == "__main__":
    unittest.main()
