"""Tests for tools.bringup_acceptance.optionals.orchestrator (T-2556 phase 14 / T-2566).

Orchestrator's custom bringup surface, proven WITHOUT re-firing either script's
main(): hook_emissions.py (T-2562, core catalog) already live-fires
ho_stop_failure.py inside a sandboxed HOME. This module proves a distinct,
non-duplicative thing instead -- the HO-resumer script + its systemd units
exist and match their documented contract, and the PRODUCER/CONSUMER schema
contract between ho_stop_failure.py (writes state) and ho_resumer.py (reads
it) is source-consistent -- all via ``ast.parse`` only, never importing or
executing either target script.

The happy-path tests point ``PANTHEON_BRINGUP_REPO_ROOT`` at THIS checkout's
real files (mirrors test_bringup_acceptance_hook_emissions.py's own "use the
real repo-checked-in scripts" convention) so the extraction is proven against
actual ground truth, not a synthetic fixture that could silently drift from
what the real files say. Negative-path tests use synthetic tempdir fixtures.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.bringup_acceptance.optionals.orchestrator import (
    RESUMER_MODULE_ID,
    STOP_FAILURE_MODULE_ID,
    OrchestratorHoResumerModule,
    OrchestratorStopFailureHookModule,
    _ACCOUNT_HOME_ENV,
    _REPO_ROOT_ENV,
    _basename_of,
    _extract_dict_literal_keys,
    _extract_get_call_keys,
    _extract_numeric_constant,
    _extract_string_constant,
    _has_module_level_function,
    build_optional_modules,
)
from tools.bringup_acceptance.registry import discover_optional_modules
from tools.bringup_acceptance.types import HardStop, RunContext, TargetInventory

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ACCOUNT = "t2566-probe-account"  # deliberately not any real Pantheon account

# The real, ground-truth schema each script actually writes/reads -- verified
# by reading tools/hooks/ho_stop_failure.py and tools/ho_resumer.py at build
# time. The happy-path tests assert the AST extraction reproduces THESE
# exact sets, not merely "something non-empty" -- an extraction bug that
# silently dropped a real key must fail this test.
_REAL_PRODUCER_KEYS = {
    "error_type", "error_message", "transcript_path",
    "session_id", "resets_at", "written_at",
}
_REAL_CONSUMER_KEYS = {"session_id", "resets_at"}


def _run_context(evidence_root: Path, *, run_id: str = "20260805T000000Z") -> RunContext:
    return RunContext(
        run_id=run_id,
        persona="orchestrator",
        account=_ACCOUNT,
        harness="claude",
        profile="interactive",
        hub_path=Path("/tmp/hub"),
        state_path=Path("/tmp/state"),
        evidence_root=evidence_root,
        baseline_ref="deadbeef" * 5,
    )


@patch.dict(os.environ, {}, clear=False)
def _clear_overrides() -> None:
    os.environ.pop(_ACCOUNT_HOME_ENV, None)
    os.environ.pop(_REPO_ROOT_ENV, None)


class _OverrideMixin(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._saved = {
            _ACCOUNT_HOME_ENV: os.environ.get(_ACCOUNT_HOME_ENV),
            _REPO_ROOT_ENV: os.environ.get(_REPO_ROOT_ENV),
        }
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _set_repo_root(self, path: Path) -> None:
        os.environ[_REPO_ROOT_ENV] = str(path)

    def _set_account_home(self, path: Path) -> None:
        os.environ[_ACCOUNT_HOME_ENV] = str(path)


# ---------------------------------------------------------------------------
# Pure AST-extraction helper tests
# ---------------------------------------------------------------------------


class BasenameExtractionTest(unittest.TestCase):
    def test_interpreter_prefixed_command(self) -> None:
        self.assertEqual(
            _basename_of("python3 /home/claude/pantheon-config/tools/hooks/ho_stop_failure.py"),
            "ho_stop_failure.py",
        )

    def test_bare_script_path(self) -> None:
        self.assertEqual(
            _basename_of("/home/claude/pantheon-config/tools/hooks/ho_stop_failure.py"),
            "ho_stop_failure.py",
        )


class StringConstantExtractionTest(unittest.TestCase):
    def test_extracts_simple_string_literal(self) -> None:
        source = 'FOO = "bar"\n'
        self.assertEqual(
            _extract_string_constant(source, path=Path("<test>"), name="FOO"), "bar"
        )

    def test_extracts_implicit_string_concatenation(self) -> None:
        source = 'FOO = (\n    "bar"\n    "baz"\n)\n'
        self.assertEqual(
            _extract_string_constant(source, path=Path("<test>"), name="FOO"), "barbaz"
        )

    def test_missing_name_is_a_hard_stop(self) -> None:
        with self.assertRaises(HardStop):
            _extract_string_constant("FOO = 1\n", path=Path("<test>"), name="BAR")

    def test_non_string_value_is_a_hard_stop(self) -> None:
        with self.assertRaises(HardStop):
            _extract_string_constant("FOO = 1\n", path=Path("<test>"), name="FOO")


class NumericConstantExtractionTest(unittest.TestCase):
    def test_extracts_plain_int_literal(self) -> None:
        self.assertEqual(
            _extract_numeric_constant("FOO = 90\n", path=Path("<test>"), name="FOO"), 90
        )

    def test_extracts_mult_of_int_literals(self) -> None:
        self.assertEqual(
            _extract_numeric_constant("FOO = 24 * 3600\n", path=Path("<test>"), name="FOO"), 86400
        )

    def test_extracts_add_of_int_literals(self) -> None:
        self.assertEqual(
            _extract_numeric_constant("FOO = 1 + 2\n", path=Path("<test>"), name="FOO"), 3
        )

    def test_missing_name_is_a_hard_stop(self) -> None:
        with self.assertRaises(HardStop):
            _extract_numeric_constant("FOO = 1\n", path=Path("<test>"), name="BAR")

    def test_non_numeric_literal_expression_is_a_hard_stop(self) -> None:
        # Restricted evaluator: only numeric-literal Add/Mult -- a function
        # call must never be silently evaluated or guessed at.
        with self.assertRaises(HardStop):
            _extract_numeric_constant("FOO = some_func()\n", path=Path("<test>"), name="FOO")

    def test_subtraction_is_not_supported_by_the_restricted_evaluator(self) -> None:
        # Deliberately narrow per team-lead ratification: Add/Mult of number
        # literals ONLY, not general arithmetic.
        with self.assertRaises(HardStop):
            _extract_numeric_constant("FOO = 5 - 2\n", path=Path("<test>"), name="FOO")


class FunctionPresenceExtractionTest(unittest.TestCase):
    def test_present_function_detected(self) -> None:
        self.assertTrue(
            _has_module_level_function("def main():\n    pass\n", path=Path("<test>"), name="main")
        )

    def test_absent_function_not_detected(self) -> None:
        self.assertFalse(
            _has_module_level_function("def other():\n    pass\n", path=Path("<test>"), name="main")
        )


class DictLiteralKeyExtractionTest(unittest.TestCase):
    def test_extracts_keys_from_dict_literal_inside_function(self) -> None:
        source = (
            "def main():\n"
            "    state: dict = {\n"
            '        "a": 1,\n'
            '        "b": 2,\n'
            "    }\n"
        )
        self.assertEqual(
            _extract_dict_literal_keys(source, path=Path("<test>"), func_name="main", var_name="state"),
            ("a", "b"),
        )

    def test_no_matching_function_is_a_hard_stop(self) -> None:
        with self.assertRaises(HardStop):
            _extract_dict_literal_keys("def other():\n    pass\n", path=Path("<test>"), func_name="main", var_name="state")

    def test_no_matching_dict_literal_is_a_hard_stop(self) -> None:
        with self.assertRaises(HardStop):
            _extract_dict_literal_keys(
                "def main():\n    x = 1\n", path=Path("<test>"), func_name="main", var_name="state"
            )


class GetCallKeyExtractionTest(unittest.TestCase):
    def test_extracts_keys_from_get_calls(self) -> None:
        source = (
            "def main():\n"
            '    a = state.get("a", "")\n'
            '    b = state.get("b")\n'
        )
        self.assertEqual(
            _extract_get_call_keys(source, path=Path("<test>"), func_name="main", var_name="state"),
            ("a", "b"),
        )

    def test_no_get_calls_returns_empty_tuple(self) -> None:
        self.assertEqual(
            _extract_get_call_keys("def main():\n    pass\n", path=Path("<test>"), func_name="main", var_name="state"),
            (),
        )


# ---------------------------------------------------------------------------
# declare()
# ---------------------------------------------------------------------------


class DeclareTest(unittest.TestCase):
    def test_orchestrator_persona_applies_for_both_modules(self) -> None:
        target = TargetInventory(persona="orchestrator", entries=())
        for module in (OrchestratorHoResumerModule(), OrchestratorStopFailureHookModule()):
            declaration = module.declare(target)
            self.assertTrue(declaration.applies)
            self.assertEqual(declaration.tier, "optional")

    def test_non_orchestrator_persona_is_declared_absent_for_both_modules(self) -> None:
        target = TargetInventory(persona="recovery", entries=())
        for module in (OrchestratorHoResumerModule(), OrchestratorStopFailureHookModule()):
            declaration = module.declare(target)
            self.assertFalse(declaration.applies)


# ---------------------------------------------------------------------------
# OrchestratorHoResumerModule.prove() -- happy path against THIS checkout's real files
# ---------------------------------------------------------------------------


class OrchestratorHoResumerModuleHappyPathTest(_OverrideMixin):
    def test_prove_against_real_repo_files(self) -> None:
        self._set_repo_root(_REPO_ROOT)
        with tempfile.TemporaryDirectory() as evidence_root:
            ctx = _run_context(Path(evidence_root))
            module = OrchestratorHoResumerModule()
            evidence = module.prove(ctx)
            self.assertEqual(evidence.status, "proven")
            self.assertEqual(evidence.detail["state_file_name"], "ho_limit_hit.json")
            self.assertEqual(evidence.detail["resume_margin_seconds"], 90)
            self.assertEqual(evidence.detail["stale_mtime_seconds"], 86400)
            row = module.report(evidence)
            self.assertEqual(row.module_id, RESUMER_MODULE_ID)
            self.assertEqual(row.status, "proven")
            restore_evidence = module.restore(ctx)
            self.assertTrue(restore_evidence.restored)


class OrchestratorHoResumerModuleNegativePathTest(_OverrideMixin):
    def _stage(self, tmp: Path, *, resumer: str | None, timer: str | None, service: str | None) -> None:
        tools_dir = tmp / "tools"
        systemd_dir = tools_dir / "systemd"
        systemd_dir.mkdir(parents=True)
        if resumer is not None:
            (tools_dir / "ho_resumer.py").write_text(resumer, encoding="utf-8")
        if timer is not None:
            (systemd_dir / "pantheon-ho-resumer.timer").write_text(timer, encoding="utf-8")
        if service is not None:
            (systemd_dir / "pantheon-ho-resumer.service").write_text(service, encoding="utf-8")

    _GOOD_RESUMER = (
        "RESUMER_PROMPT = \"continue\"\n"
        "_DEFAULT_STATE_FILE_NAME = \"ho_limit_hit.json\"\n"
        "_RESUME_MARGIN_SECONDS = 90\n"
        "_STALE_MTIME_SECONDS = 24 * 3600\n"
        "def main():\n    pass\n"
    )
    _GOOD_TIMER = "[Timer]\nOnCalendar=*-*-* *:0/5:00\nUnit=pantheon-ho-resumer.service\n"
    _GOOD_SERVICE = "[Service]\nType=oneshot\nExecStart=/usr/bin/python3 %h/pantheon-config/tools/ho_resumer.py\n"

    def test_missing_resumer_file_is_a_hard_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._stage(Path(tmp), resumer=None, timer=self._GOOD_TIMER, service=self._GOOD_SERVICE)
            self._set_repo_root(Path(tmp))
            with tempfile.TemporaryDirectory() as evidence_root:
                with self.assertRaises(HardStop):
                    OrchestratorHoResumerModule().prove(_run_context(Path(evidence_root)))

    def test_missing_main_function_is_a_hard_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken = self._GOOD_RESUMER.replace("def main():\n    pass\n", "")
            self._stage(Path(tmp), resumer=broken, timer=self._GOOD_TIMER, service=self._GOOD_SERVICE)
            self._set_repo_root(Path(tmp))
            with tempfile.TemporaryDirectory() as evidence_root:
                with self.assertRaises(HardStop):
                    OrchestratorHoResumerModule().prove(_run_context(Path(evidence_root)))

    def test_wrong_state_file_name_is_a_hard_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken = self._GOOD_RESUMER.replace(
                '_DEFAULT_STATE_FILE_NAME = "ho_limit_hit.json"',
                '_DEFAULT_STATE_FILE_NAME = "something_else.json"',
            )
            self._stage(Path(tmp), resumer=broken, timer=self._GOOD_TIMER, service=self._GOOD_SERVICE)
            self._set_repo_root(Path(tmp))
            with tempfile.TemporaryDirectory() as evidence_root:
                with self.assertRaises(HardStop):
                    OrchestratorHoResumerModule().prove(_run_context(Path(evidence_root)))

    def test_timer_missing_oncalendar_is_a_hard_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken_timer = "[Timer]\nUnit=pantheon-ho-resumer.service\n"
            self._stage(Path(tmp), resumer=self._GOOD_RESUMER, timer=broken_timer, service=self._GOOD_SERVICE)
            self._set_repo_root(Path(tmp))
            with tempfile.TemporaryDirectory() as evidence_root:
                with self.assertRaises(HardStop):
                    OrchestratorHoResumerModule().prove(_run_context(Path(evidence_root)))

    def test_service_missing_oneshot_is_a_hard_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken_service = "[Service]\nExecStart=/usr/bin/python3 %h/pantheon-config/tools/ho_resumer.py\n"
            self._stage(Path(tmp), resumer=self._GOOD_RESUMER, timer=self._GOOD_TIMER, service=broken_service)
            self._set_repo_root(Path(tmp))
            with tempfile.TemporaryDirectory() as evidence_root:
                with self.assertRaises(HardStop):
                    OrchestratorHoResumerModule().prove(_run_context(Path(evidence_root)))


# ---------------------------------------------------------------------------
# OrchestratorStopFailureHookModule.prove()
# ---------------------------------------------------------------------------


def _settings_json_text(command: str) -> str:
    return json.dumps({"hooks": {"StopFailure": [{"hooks": [{"type": "command", "command": command}]}]}})


class OrchestratorStopFailureHookModuleHappyPathTest(_OverrideMixin):
    def test_prove_against_real_repo_files_extracts_exact_known_schema(self) -> None:
        self._set_repo_root(_REPO_ROOT)
        with tempfile.TemporaryDirectory() as account_home:
            settings_path = Path(account_home) / ".claude" / "settings.json"
            settings_path.parent.mkdir(parents=True)
            settings_path.write_text(
                _settings_json_text("/home/claude/pantheon-config/tools/hooks/ho_stop_failure.py"),
                encoding="utf-8",
            )
            self._set_account_home(Path(account_home))
            with tempfile.TemporaryDirectory() as evidence_root:
                ctx = _run_context(Path(evidence_root))
                module = OrchestratorStopFailureHookModule()
                evidence = module.prove(ctx)
                self.assertEqual(evidence.status, "proven")
                self.assertEqual(set(evidence.detail["producer_keys"]), _REAL_PRODUCER_KEYS)
                self.assertEqual(set(evidence.detail["consumer_keys"]), _REAL_CONSUMER_KEYS)
                row = module.report(evidence)
                self.assertEqual(row.module_id, STOP_FAILURE_MODULE_ID)
                self.assertEqual(row.status, "proven")
                restore_evidence = module.restore(ctx)
                self.assertTrue(restore_evidence.restored)


class OrchestratorStopFailureHookModuleNegativePathTest(_OverrideMixin):
    def test_hook_not_wired_to_stopfailure_is_a_hard_stop(self) -> None:
        self._set_repo_root(_REPO_ROOT)
        with tempfile.TemporaryDirectory() as account_home:
            settings_path = Path(account_home) / ".claude" / "settings.json"
            settings_path.parent.mkdir(parents=True)
            settings_path.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
            self._set_account_home(Path(account_home))
            with tempfile.TemporaryDirectory() as evidence_root:
                with self.assertRaises(HardStop):
                    OrchestratorStopFailureHookModule().prove(_run_context(Path(evidence_root)))

    def test_stopfailure_wired_to_a_different_script_is_a_hard_stop(self) -> None:
        self._set_repo_root(_REPO_ROOT)
        with tempfile.TemporaryDirectory() as account_home:
            settings_path = Path(account_home) / ".claude" / "settings.json"
            settings_path.parent.mkdir(parents=True)
            settings_path.write_text(
                _settings_json_text("/home/claude/pantheon-config/tools/hooks/some-other-hook.py"),
                encoding="utf-8",
            )
            self._set_account_home(Path(account_home))
            with tempfile.TemporaryDirectory() as evidence_root:
                with self.assertRaises(HardStop):
                    OrchestratorStopFailureHookModule().prove(_run_context(Path(evidence_root)))

    def test_consumer_reading_a_key_the_producer_does_not_write_is_a_hard_stop(self) -> None:
        # Real ho_resumer.py (consumer) stays untouched; a SYNTHETIC producer
        # with a narrowed schema (missing resets_at) must trip the mismatch.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "tools" / "hooks").mkdir(parents=True)
            (repo / "tools" / "hooks" / "ho_stop_failure.py").write_text(
                "def main():\n"
                "    state: dict = {\n"
                '        "session_id": 1,\n'
                "    }\n",
                encoding="utf-8",
            )
            # Real ho_resumer.py content, copied byte-for-byte, so the
            # CONSUMER side of this test is still real ground truth.
            real_resumer = (_REPO_ROOT / "tools" / "ho_resumer.py").read_text(encoding="utf-8")
            (repo / "tools" / "ho_resumer.py").write_text(real_resumer, encoding="utf-8")
            self._set_repo_root(repo)
            with tempfile.TemporaryDirectory() as account_home:
                settings_path = Path(account_home) / ".claude" / "settings.json"
                settings_path.parent.mkdir(parents=True)
                settings_path.write_text(
                    _settings_json_text("/home/claude/pantheon-config/tools/hooks/ho_stop_failure.py"),
                    encoding="utf-8",
                )
                self._set_account_home(Path(account_home))
                with tempfile.TemporaryDirectory() as evidence_root:
                    with self.assertRaises(HardStop) as caught:
                        OrchestratorStopFailureHookModule().prove(_run_context(Path(evidence_root)))
                    self.assertIn("resets_at", str(caught.exception))


# ---------------------------------------------------------------------------
# registry conformance
# ---------------------------------------------------------------------------


class BuildOptionalModulesTest(unittest.TestCase):
    def test_returns_both_modules_with_expected_ids_and_tier(self) -> None:
        modules = build_optional_modules()
        ids = {module.id for module in modules}
        self.assertEqual(ids, {RESUMER_MODULE_ID, STOP_FAILURE_MODULE_ID})
        for module in modules:
            self.assertEqual(module.tier, "optional")

    def test_discoverable_via_the_registry_entry_point(self) -> None:
        modules = discover_optional_modules("orchestrator")
        ids = {module.id for module in modules}
        self.assertEqual(ids, {RESUMER_MODULE_ID, STOP_FAILURE_MODULE_ID})


if __name__ == "__main__":
    unittest.main()
