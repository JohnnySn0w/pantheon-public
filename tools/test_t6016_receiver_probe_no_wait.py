"""T-6016: read-only receiver probes are single-shot and never wait.

Consume holds the per-persona runtime-operation lock while probing the
recorded receiver. A waiting probe blocks on a state only the controller can
produce, and the controller defers on the held lock: a mutual wait. These tests
reproduce that interleaving with a real lock and a fake bridge subprocess.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import codex_telegram_bridge as bridge
from tools import runtime_controller as rc
from tools.harness_adapter import integration as hi

PERSONA = "recovery"
CONTROLLER_BOUND_S = 2.0
FAKE_BLOCK_CAP_S = 10.0  # the old-code fake gives up after this; the suite never hangs


def _binding() -> SimpleNamespace:
    return SimpleNamespace(
        persona=PERSONA,
        launch=SimpleNamespace(harness="claude", native={}),
        communications={"kind": "telegram"},
    )


class _Interleave:
    """Run `probe` under the real operation lock while a 'controller' thread
    polls that lock non-blocking. The fake bridge waits for the controller
    unless it was invoked with --no-wait."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.controller_got_lock = threading.Event()
        self.argvs: list[tuple[str, ...]] = []
        self.result: object = None
        self.error: BaseException | None = None

    def fake_subprocess(self, binding, *args, **kwargs):
        self.argvs.append(tuple(args))
        if "--no-wait" not in args:
            # Old behavior: poll until the controller can start (it never can).
            self.controller_got_lock.wait(FAKE_BLOCK_CAP_S)
        raise RuntimeError("tmux pane is not present")

    def run(self, probe) -> None:
        in_probe = threading.Event()

        def holder() -> None:
            try:
                with rc.persona_operation_lock(PERSONA, state_root=self.tmp, blocking=False):
                    in_probe.set()
                    self.result = probe()
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertions
                self.error = exc

        def controller() -> None:
            if not in_probe.wait(5):
                return
            deadline = CONTROLLER_BOUND_S
            import time

            end = time.monotonic() + deadline
            while time.monotonic() < end:
                try:
                    with rc.persona_operation_lock(PERSONA, state_root=self.tmp, blocking=False):
                        self.controller_got_lock.set()
                        return
                except rc.OperationLockBusy:
                    time.sleep(0.05)

        holder_t = threading.Thread(target=holder, daemon=True)
        controller_t = threading.Thread(target=controller, daemon=True)
        holder_t.start()
        controller_t.start()
        controller_t.join(CONTROLLER_BOUND_S + 3)
        holder_t.join(FAKE_BLOCK_CAP_S + 3)


class ReadOnlyProbeLockInterleaveTests(unittest.TestCase):
    def _run(self, probe):
        with tempfile.TemporaryDirectory() as tmp:
            il = _Interleave(Path(tmp))
            with mock.patch.object(hi, "_telegram_subprocess", il.fake_subprocess):
                il.run(probe)
            return il

    def test_rehydrate_releases_lock_to_controller(self) -> None:
        class Blocked(Exception):
            pass

        def raise_block(reason, **kwargs):
            raise Blocked(reason)

        def rehydrate():
            try:
                rc._rehydrate_recorded_runtime_selection(
                    _binding(), {}, adapter_factory=None, state_root=Path("/nonexistent"),
                    backend=None, context=None, now=None, manifest_order=(), attempt_limit=1,
                )
            except Blocked as exc:
                return str(exc)

        with mock.patch.object(rc, "_recorded_scope_inactive", return_value=True), \
                mock.patch.object(rc, "_raise_runtime_viability_block", side_effect=raise_block):
            il = self._run(rehydrate)
        self.assertIsNone(il.error)
        self.assertTrue(il.controller_got_lock.is_set(), "controller never acquired the lock")
        self.assertEqual(il.result, "recorded-receiver-not-present")
        self.assertIn("--no-wait", il.argvs[0])

    def test_rehydrate_passes_wait_false_to_both_public_probes(self) -> None:
        with mock.patch.object(rc, "_recorded_scope_inactive", return_value=True), \
                mock.patch.object(rc, "prove_managed_receiver_present", return_value=True) as present, \
                mock.patch.object(rc, "prove_predecessor_receiver_absent", return_value=False) as absent, \
                mock.patch.object(rc, "_raise_runtime_viability_block", side_effect=RuntimeError("stop")):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                rc._rehydrate_recorded_runtime_selection(
                    _binding(), {}, adapter_factory=None, state_root=Path("/nonexistent"),
                    backend=None, context=None, now=None, manifest_order=(), attempt_limit=1,
                )
        present.assert_called_once()
        self.assertIs(present.call_args.kwargs.get("wait"), False)
        self.assertIs(absent.call_args.kwargs.get("wait"), False)

    def test_public_probes_default_to_waiting(self) -> None:
        argvs: list[tuple[str, ...]] = []

        def fake(binding, *args, **kwargs):
            argvs.append(tuple(args))
            raise RuntimeError("tmux pane is not present")

        codex = SimpleNamespace(persona=PERSONA, launch=SimpleNamespace(harness="codex", native={}), communications={"kind": "telegram"})
        with mock.patch.object(hi, "_telegram_subprocess", fake), \
                mock.patch.object(rc, "_manifest_bindings", return_value=[_binding()]):
            self.assertFalse(rc.prove_managed_receiver_present(_binding()))
            self.assertFalse(rc.prove_predecessor_receiver_absent(codex, manifest_path=Path("/x")))
            self.assertFalse(rc.prove_managed_receiver_present(_binding(), wait=False))
        self.assertEqual(len(argvs), 3)
        self.assertNotIn("--no-wait", argvs[0])
        self.assertNotIn("--no-wait", argvs[1])
        self.assertIn("--no-wait", argvs[2])

    def test_read_only_receiver_present_releases_lock_to_controller(self) -> None:
        il = self._run(lambda: self._swallow(lambda: rc._read_only_receiver_present(_binding(), backend=None)))
        self.assertTrue(il.controller_got_lock.is_set(), "controller never acquired the lock")
        self.assertEqual(il.result, "raised:RuntimeError")
        self.assertIn("--no-wait", il.argvs[0])

    def test_predecessor_absent_probe_releases_lock_to_controller(self) -> None:
        binding = SimpleNamespace(
            persona=PERSONA,
            launch=SimpleNamespace(harness="codex", native={}),
            communications={"kind": "telegram"},
        )
        predecessor = _binding()
        with mock.patch.object(rc, "_manifest_bindings", return_value=[predecessor]):
            il = self._run(
                lambda: rc.prove_predecessor_receiver_absent(binding, manifest_path=Path("/nonexistent"), wait=False)
            )
        self.assertIsNone(il.error)
        self.assertTrue(il.controller_got_lock.is_set(), "controller never acquired the lock")
        self.assertIs(il.result, False)
        self.assertIn("--no-wait", il.argvs[0])

    @staticmethod
    def _swallow(fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            return f"raised:{type(exc).__name__}"


class SingleShotOutcomeTests(unittest.TestCase):
    def test_single_shot_not_present_maps_to_not_ok_probe(self) -> None:
        with mock.patch.object(hi, "_telegram_subprocess", side_effect=RuntimeError("tmux pane is not present")):
            probe = rc._probe_managed_receiver(_binding(), "present", backend=None, wait=False)
        self.assertFalse(probe.ok)
        self.assertEqual(probe.state, "unknown")


class ProveTelegramReceiverStateArgvTests(unittest.TestCase):
    def _argv(self, **kwargs) -> tuple[str, ...]:
        captured: list[tuple[str, ...]] = []

        def fake(binding, *args, **kw):
            captured.append(tuple(args))
            return {"state": "present", "evidence": {}}

        with mock.patch.object(hi, "_telegram_subprocess", fake):
            hi._prove_telegram_receiver_state(_binding(), "present", backend=None, **kwargs)
        return captured[0]

    def test_no_wait_flag_only_when_wait_false(self) -> None:
        self.assertNotIn("--no-wait", self._argv())
        self.assertNotIn("--no-wait", self._argv(wait=True))
        self.assertIn("--no-wait", self._argv(wait=False))

    def test_default_argv_is_unchanged(self) -> None:
        self.assertEqual(
            self._argv(),
            ("receiver-state", "--persona", PERSONA, "--kind", "claude", "--expected", "present", "--json"),
        )

    def test_legacy_backend_method_without_wait_kwarg_still_called(self) -> None:
        calls: list[tuple] = []

        class Legacy:
            def _require_present(self, binding, *, kind):
                calls.append((kind,))
                return {}

        hi._prove_telegram_receiver_state(_binding(), "present", backend=Legacy(), wait=False)
        self.assertEqual(calls, [("claude",)])

    def test_backend_method_with_wait_kwarg_receives_it(self) -> None:
        seen: list[bool] = []

        class Modern:
            def _require_present(self, binding, *, kind, wait=True):
                seen.append(wait)
                return {}

        hi._prove_telegram_receiver_state(_binding(), "present", backend=Modern(), wait=False)
        hi._prove_telegram_receiver_state(_binding(), "present", backend=Modern())
        self.assertEqual(seen, [False, True])


class BridgeNoWaitTests(unittest.TestCase):
    def _backend(self, once) -> bridge.LocalReceiverBackend:
        backend = bridge.LocalReceiverBackend(timeout_seconds=5)
        backend._phase_binding = lambda binding, phase: SimpleNamespace(claude_receiver_topology="runtime-owned")  # type: ignore[method-assign]
        backend._claude_state_once = once  # type: ignore[method-assign]
        return backend

    def test_wait_false_calls_state_once_and_raises(self) -> None:
        calls: list[int] = []

        def once(binding, expected, *, topology_phase="managed"):
            calls.append(1)
            raise bridge.BridgeError("tmux pane is not present")

        backend = self._backend(once)
        with mock.patch.object(bridge.time, "sleep", side_effect=AssertionError("must not poll")):
            with self.assertRaisesRegex(bridge.BridgeError, "tmux pane is not present"):
                backend._require_claude_state(object(), expected="present", wait=False)
            with self.assertRaises(bridge.BridgeError):
                backend._require_present(object(), kind="claude", wait=False)
            with self.assertRaises(bridge.BridgeError):
                backend._require_absent(object(), kind="claude", wait=False)
        self.assertEqual(len(calls), 3)

    def test_default_still_polls(self) -> None:
        calls: list[int] = []

        def once(binding, expected, *, topology_phase="managed"):
            calls.append(1)
            if len(calls) < 3:
                raise bridge.BridgeError("tmux pane is not present")
            return {"ok": "yes"}

        backend = self._backend(once)
        with mock.patch.object(bridge.time, "sleep"):
            self.assertEqual(dict(backend._require_claude_state(object(), expected="present")), {"ok": "yes"})
        self.assertEqual(len(calls), 3)

    def test_cli_no_wait_reaches_backend(self) -> None:
        for argv_extra, want in (([], True), (["--no-wait"], False)):
            with self.subTest(argv_extra=argv_extra):
                seen: dict[str, object] = {}

                def fake_present(self_, binding, *, kind, topology_phase="managed", wait=True):
                    seen["wait"] = wait
                    return {"kind": kind}

                binding = SimpleNamespace(persona=PERSONA, account="a")
                with mock.patch.object(bridge, "binding_for_current_account", return_value=binding), \
                        mock.patch.object(bridge.LocalReceiverBackend, "_require_present", fake_present), \
                        mock.patch("builtins.print"):
                    rc_code = bridge.main(
                        ["receiver-state", "--persona", PERSONA, "--kind", "claude", "--expected", "present", "--json", *argv_extra]
                    )
                self.assertEqual(rc_code, 0)
                self.assertEqual(seen["wait"], want)


if __name__ == "__main__":
    unittest.main()
