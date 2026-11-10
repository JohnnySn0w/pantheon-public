"""T-4940 guard: every test module that pins an exact git-verb sequence ending in a push
must isolate the seat's state dir for plain-unittest runs (tools/_seat_state_isolation.py).
Otherwise, on a seat with a pin-verified writable mirror, the real production manifest
arms _git_utils._resolve_write_mirror and the sequence gains a 'remote' verb (T-4940:
3 test_ticket/test_ticket_grooming tests and 1 test_watch_runner test failed on Recovery).
"""
from __future__ import annotations

import ast
import contextlib
import importlib
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent
_PUSH_TERMINATED_VERB_LIST = re.compile(r"""["']push["'],?\s*\]""")
_USES_ISOLATION = re.compile(r"_seat_state_isolation\.start\(\)")

# T-6118: a module that imports runtime_controller AND calls one of its lifecycle drivers ran the
# REAL session_chronicle subprocess under plain `python3 -m unittest` (no conftest) and wrote the
# account's real Chronicle / persona state. Seven modules did (probe, 2026-11-04); they now call
# _seat_state_isolation.start(). The ten below were probed under unittest with HOME scratched
# and left both roots empty (they stub the lifecycle seams or the hooks), so they are exempt.
# A NEW matching module must wire the helper or be probed and added here with that evidence.
_IMPORTS_CONTROLLER = re.compile(
    r"(?m)^\s*(from tools import runtime_controller|import tools\.runtime_controller|import runtime_controller"
    r"|from tools\.runtime_controller import|from tools import .*\bruntime_controller\b)")
_LIFECYCLE_DRIVER = re.compile(
    r"\b_?(start_runtime|ensure_runtime|controller_main|block_until_tmux_death|_session_chronicle_end"
    r"|_statusline_launch_args|run_codex_rc_ready_bringup|prove)\(")
_PROBED_CLEAN = frozenset({
    "test_bringup_acceptance_forced_cap_return.py", "test_bringup_acceptance_harness_runtime.py",
    "test_bringup_acceptance_runner.py", "test_continuity_switch_coordinator.py",
    "test_runtime_controller_operator_restart.py", "test_runtime_controller_session_chronicle.py",
    "test_seat_sleep.py", "test_seat_sleep_drain.py", "test_t5451_flip_policy.py",
    "test_t5791_auth_loss.py",
})


def _offenders() -> list[str]:
    bad = []
    for path in sorted(TOOLS.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        if _PUSH_TERMINATED_VERB_LIST.search(text) and not _USES_ISOLATION.search(text):
            bad.append(path.name)
    return bad


def _controller_lifecycle_offenders() -> list[str]:
    bad = []
    for path in sorted(TOOLS.glob("test_*.py")):
        if path.name == Path(__file__).name or path.name in _PROBED_CLEAN:
            continue
        text = path.read_text(encoding="utf-8")
        if (_IMPORTS_CONTROLLER.search(text) and _LIFECYCLE_DRIVER.search(text)
                and not _USES_ISOLATION.search(text)):
            bad.append(path.name)
    return bad


class SeatStateIsolationGuard(unittest.TestCase):
    def test_helper_isolates_and_restores_oom_holds_path_t5576(self) -> None:
        try:
            from tools import _seat_state_isolation as iso
            from tools import harness as tools_harness
        except ImportError:  # pragma: no cover
            self.skipTest("tools package not importable")

        missing = object()
        prior_bare = sys.modules.get("harness", missing)
        depth = len(iso._state)
        outer_root = inner_root = None
        try:
            sys.modules.pop("harness", None)
            with mock.patch.object(sys, "path", [str(TOOLS), *sys.path]):
                bare_harness = importlib.import_module("harness")
            self.assertIsNot(tools_harness, bare_harness)

            tools_sentinel, bare_sentinel = object(), object()
            with mock.patch.object(tools_harness, "_OOM_HOLDS_PATH", tools_sentinel), \
                    mock.patch.object(bare_harness, "_OOM_HOLDS_PATH", bare_sentinel):
                outer_root = iso.start()
                outer_tools_path = tools_harness._OOM_HOLDS_PATH
                outer_bare_path = bare_harness._OOM_HOLDS_PATH
                for active in (outer_tools_path, outer_bare_path):
                    self.assertIsInstance(active, Path)
                    self.assertEqual(active.parent, Path(outer_root))
                    self.assertFalse(active.exists())

                inner_root = iso.start()
                for active in (tools_harness._OOM_HOLDS_PATH, bare_harness._OOM_HOLDS_PATH):
                    self.assertIsInstance(active, Path)
                    self.assertEqual(active.parent, Path(inner_root))
                    self.assertFalse(active.exists())

                iso.stop()
                self.assertFalse(Path(inner_root).exists())
                self.assertTrue(Path(outer_root).is_dir())
                self.assertIs(tools_harness._OOM_HOLDS_PATH, outer_tools_path)
                self.assertIs(bare_harness._OOM_HOLDS_PATH, outer_bare_path)

                iso.stop()
                self.assertFalse(Path(outer_root).exists())
                self.assertIs(tools_harness._OOM_HOLDS_PATH, tools_sentinel)
                self.assertIs(bare_harness._OOM_HOLDS_PATH, bare_sentinel)
        finally:
            while len(iso._state) > depth:
                iso.stop()
            if prior_bare is missing:
                sys.modules.pop("harness", None)
            else:
                sys.modules["harness"] = prior_bare
            for root in (inner_root, outer_root):
                if root is not None:
                    self.assertFalse(Path(root).exists())

    def test_t5028_status_modules_use_seat_state_isolation_t5576(self) -> None:
        modules = (
            "test_t5028_holds_fail_closed.py",
            "test_t5028_mr2.py",
            "test_t5028_mr3.py",
        )
        hooks = (("setUpModule", "start"), ("tearDownModule", "stop"))
        for module_name in modules:
            tree = ast.parse((TOOLS / module_name).read_text(encoding="utf-8"), filename=module_name)
            functions = {
                node.name: node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            for hook_name, method_name in hooks:
                with self.subTest(module=module_name, hook=hook_name):
                    hook = functions.get(hook_name)
                    self.assertIsNotNone(hook, f"{module_name} has no module-level {hook_name}()")
                    assert hook is not None
                    direct_calls = [
                        statement.value
                        for statement in hook.body
                        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
                    ]
                    self.assertTrue(
                        any(
                            isinstance(call.func, ast.Attribute)
                            and isinstance(call.func.value, ast.Name)
                            and call.func.value.id == "_seat_state_isolation"
                            and call.func.attr == method_name
                            and not call.args
                            and not call.keywords
                            for call in direct_calls
                        ),
                        f"{module_name} {hook_name}() must directly call "
                        f"_seat_state_isolation.{method_name}()",
                    )

    def test_t5028_status_does_not_read_hostile_oom_holds_t5576(self) -> None:
        try:
            from tools import _seat_state_isolation as iso
            from tools import harness
        except ImportError:  # pragma: no cover
            self.skipTest("tools package not importable")

        depth = len(iso._state)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hostile = base / "hostile-oom-holds.json"
            hostile.write_text('{"recovery": {"reason": "hostile sentinel"}}', encoding="utf-8")
            state_root = base / "state"
            state_root.mkdir()
            reads: list[Path] = []
            real_read_text = Path.read_text

            def track_read(path: Path, *args, **kwargs):
                if path == hostile:
                    reads.append(path)
                return real_read_text(path, *args, **kwargs)

            try:
                with mock.patch.object(harness, "_OOM_HOLDS_PATH", hostile), \
                        mock.patch.object(Path, "read_text", track_read):
                    isolated_root = iso.start()
                    active_path = harness._OOM_HOLDS_PATH
                    args = harness.build_parser().parse_args(["recovery", "status"])
                    with contextlib.redirect_stdout(io.StringIO()):
                        code = harness._do_status(args, root=state_root, pane_probe=lambda bindings: None)
                    self.assertEqual(code, 0)
                    self.assertEqual(reads, [], f"status read the hostile prior OOM-holds path: {reads}")
                    self.assertIsInstance(active_path, Path)
                    self.assertEqual(active_path.parent, Path(isolated_root))
                    self.assertFalse(active_path.exists())
                    iso.stop()
                    self.assertIs(harness._OOM_HOLDS_PATH, hostile)
                    self.assertFalse(Path(isolated_root).exists())
            finally:
                while len(iso._state) > depth:
                    iso.stop()

    def test_every_controller_lifecycle_module_isolates_chronicle_state_t6118(self):
        self.assertEqual(_controller_lifecycle_offenders(), [],
                         "these modules drive runtime_controller lifecycle paths but never call "
                         "_seat_state_isolation.start() in setUpModule (plain unittest would write the "
                         "real Chronicle/persona state); wire it, or probe the module and add it to "
                         "_PROBED_CLEAN")

    def test_every_push_sequence_module_isolates_seat_state(self):
        self.assertEqual(_offenders(), [], "these modules pin a push-terminated git sequence "
                         "but never call _seat_state_isolation.start() in setUpModule")

    def test_helper_isolates_and_restores(self):
        try:
            from tools import _seat_state_isolation as iso
        except ImportError:  # pragma: no cover
            import _seat_state_isolation as iso  # type: ignore[no-redef]
        before = os.environ.get("PANTHEON_STATE_DIR")
        path = iso.start()
        try:
            self.assertEqual(os.environ["PANTHEON_STATE_DIR"], path)
            self.assertTrue(Path(path).is_dir())
            self.assertEqual(list(Path(path).iterdir()), [])
            self.assertTrue(path.startswith(tempfile.gettempdir()))
        finally:
            iso.stop()
        self.assertEqual(os.environ.get("PANTHEON_STATE_DIR"), before)
        self.assertFalse(Path(path).exists())

    def test_helper_isolates_chronicle_and_persona_roots_t6118(self):
        """T-6118: start() also drops the narrower overrides, turns the session-chronicle hooks
        off, leaves the import-time home constants alone (resolver-agreement invariant), and
        stop() restores all of it."""
        try:
            from tools import _seat_state_isolation as iso
            from tools import chronicle, runtime_invocation_receipt as receipt
        except ImportError:  # pragma: no cover
            self.skipTest("tools package not importable")
        names = ("PANTHEON_PERSONA_STATE_DIR", "PANTHEON_CHRONICLE_ROOT",
                 "PANTHEON_SESSION_CHRONICLE_HOOKS", "PANTHEON_SESSION_CHRONICLE_ALERT")
        saved = {n: os.environ.get(n) for n in names}
        real_receipt, real_chron = receipt.DEFAULT_STATE_ROOT, chronicle.CHRONICLE_ROOT
        os.environ["PANTHEON_PERSONA_STATE_DIR"] = "/seat/real/personas"
        os.environ["PANTHEON_CHRONICLE_ROOT"] = "/seat/real/chronicle"
        os.environ["PANTHEON_SESSION_CHRONICLE_HOOKS"] = "on"
        depth = len(iso._state)
        try:
            path = iso.start()
            self.assertNotIn("PANTHEON_PERSONA_STATE_DIR", os.environ)
            self.assertNotIn("PANTHEON_CHRONICLE_ROOT", os.environ)
            self.assertEqual(os.environ["PANTHEON_SESSION_CHRONICLE_HOOKS"], "off")
            self.assertEqual(os.environ["PANTHEON_SESSION_CHRONICLE_ALERT"], "off")
            self.assertEqual(receipt.DEFAULT_STATE_ROOT, real_receipt)
            self.assertEqual(chronicle.CHRONICLE_ROOT, real_chron)
            self.assertEqual(receipt._state_root(), Path(path) / "personas")
            self.assertEqual(chronicle._chronicle_root(), Path(path) / "chronicle")
            iso.stop()
            self.assertEqual(os.environ["PANTHEON_PERSONA_STATE_DIR"], "/seat/real/personas")
            self.assertEqual(os.environ["PANTHEON_CHRONICLE_ROOT"], "/seat/real/chronicle")
            self.assertEqual(os.environ["PANTHEON_SESSION_CHRONICLE_HOOKS"], "on")
            self.assertEqual(receipt.DEFAULT_STATE_ROOT, real_receipt)
            self.assertEqual(chronicle.CHRONICLE_ROOT, real_chron)
        finally:
            while len(iso._state) > depth:
                iso.stop()
            for n, v in saved.items():
                if v is None:
                    os.environ.pop(n, None)
                else:
                    os.environ[n] = v


    def test_chronicle_default_root_follows_start_not_home_t6118(self):
        """T-6118 round 2: chronicle writes/reads with root=None resolve the root at CALL time.
        The import-time CHRONICLE_ROOT snapshot used to leak them under plain unittest, where
        start() sets PANTHEON_STATE_DIR but cannot reach a frozen constant."""
        try:
            from unittest import mock
            from tools import _seat_state_isolation as iso
            from tools import chronicle
        except ImportError:  # pragma: no cover
            self.skipTest("tools package not importable")
        names = ("PANTHEON_STATE_DIR", "PANTHEON_PERSONA_STATE_DIR", "PANTHEON_CHRONICLE_ROOT")
        saved = {n: os.environ.get(n) for n in names}
        depth = len(iso._state)
        try:
            with tempfile.TemporaryDirectory() as fake_home, mock.patch.object(
                    Path, "home", return_value=Path(fake_home)):
                path = iso.start()
                src = Path(path) / "src.log"
                src.write_text("pane")
                comp = chronicle.archive_companion(
                    "t6118-agent", src, jsonl_uuid="u1", ts="2026-11-04T00:00:00Z")
                snap = chronicle.archive(
                    "t6118-agent", "handoff", jsonl_uuid="u2", reason="test", ts="2026-11-04T00:00:01Z")
                for written in (comp, snap):
                    self.assertIsNotNone(written)
                    self.assertTrue(Path(written).is_file())
                    self.assertTrue(str(written).startswith(str(Path(path) / "chronicle")), written)
                self.assertTrue(chronicle.entries("t6118-agent"))
                self.assertEqual(chronicle.chronicle_root(), Path(path) / "chronicle")
                self.assertEqual(list(Path(fake_home).rglob("*")), [])
        finally:
            while len(iso._state) > depth:
                iso.stop()
            for n, v in saved.items():
                if v is None:
                    os.environ.pop(n, None)
                else:
                    os.environ[n] = v


if __name__ == "__main__":
    unittest.main()
