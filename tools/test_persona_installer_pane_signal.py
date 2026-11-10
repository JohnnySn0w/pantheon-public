"""T-5612: the seat's tmux pane is a liveness signal for the residue quarantine.

A FAILED controller unit does not prove the claude pane is gone. The pane is read read-only,
against the SEAT's socket (``-S /tmp/tmux-<seat uid>/default``, never the caller's default), and
enters the shared ``_runtime_liveness_refusal`` gate as a ``tmux-session:<name>`` pseudo-entry.
Missing socket == absent; any unreadable/foreign/erroring probe == refuse (fail closed).
"""

from __future__ import annotations

import dataclasses
import os
import stat as stat_mod
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from tools import persona_installer as PI
from tools import persona_installer_activation as ACT

SESSION = "pantheon-recovery"
UID = 4242
SOCKET = f"/tmp/tmux-{UID}/default"
CONTROLLER = "pantheon-runtime-controller@recovery.service"
CONSUME = "pantheon-persona-consume@recovery.service"
KEY = f"tmux-session:{SESSION}"


def sock_stat(uid: int = UID, mode: int = stat_mod.S_IFSOCK | 0o600):
    return lambda path: SimpleNamespace(st_mode=mode, st_uid=uid)


def missing(path):
    raise FileNotFoundError(path)


def child(returncode: int, stderr: str = "", log: list | None = None):
    def run(argv):
        if log is not None:
            log.append(argv)
        return SimpleNamespace(returncode=returncode, stderr=stderr)

    return run


class GateTests(unittest.TestCase):
    UNITS = {CONTROLLER: "failed", CONSUME: "inactive"}

    def test_failed_controller_with_a_live_pane_refuses_naming_the_pane(self) -> None:
        refusal = ACT._runtime_liveness_refusal({**self.UNITS, KEY: "live"})
        self.assertIsNotNone(refusal)
        self.assertIn(SESSION, refusal)
        self.assertIn("live pane", refusal)

    def test_absent_pane_with_inactive_or_failed_units_is_admitted(self) -> None:
        self.assertIsNone(ACT._runtime_liveness_refusal({**self.UNITS, KEY: "absent"}))
        self.assertIsNone(ACT._runtime_liveness_refusal(self.UNITS))  # callers without the signal: as before

    def test_unreadable_or_unknown_pane_refuses(self) -> None:
        for state in (None, "weird"):
            with self.subTest(state=state):
                self.assertIn(SESSION, ACT._runtime_liveness_refusal({**self.UNITS, KEY: state}) or "")

    def test_absent_is_not_a_safe_unit_state(self) -> None:
        self.assertIsNotNone(ACT._runtime_liveness_refusal({CONTROLLER: "absent"}))


class ProbeShapeTests(unittest.TestCase):
    def test_argv_names_the_seat_socket_and_an_exact_session(self) -> None:
        self.assertEqual(
            ACT.pane_probe_argv(SESSION, SOCKET),
            ["tmux", "-S", SOCKET, "has-session", "-t", f"={SESSION}"],
        )
        self.assertEqual(ACT.seat_tmux_socket(UID), SOCKET)

    def test_probe_result_table(self) -> None:
        for rc, stderr, want in (
            (0, "", "live"),
            (1, "no server running on /tmp/tmux-4242/default", "absent"),
            (1, "can't find session: =pantheon-recovery", "absent"),
            (1, "error connecting to /tmp/tmux-4242/default (Permission denied)", None),
            (1, "", None),
            (2, "no server running", None),
            (127, "", None),
        ):
            with self.subTest(rc=rc, stderr=stderr):
                self.assertEqual(ACT.pane_state_from_probe(rc, stderr), want)


class ReaderTests(unittest.TestCase):
    def read(self, *, session=SESSION, uid=UID, stat=None, run=None):
        return ACT.read_seat_pane_state(session, uid, run=run or child(0), stat=stat or sock_stat())

    def test_live(self) -> None:
        self.assertEqual(self.read(), "live")

    def test_missing_socket_is_absent_and_never_spawns_tmux(self) -> None:
        log: list = []
        self.assertEqual(self.read(stat=missing, run=child(0, log=log)), "absent")
        self.assertEqual(log, [])

    def test_probe_argv_carries_the_seat_socket(self) -> None:
        log: list = []
        self.read(run=child(0, log=log))
        self.assertEqual(log, [["tmux", "-S", SOCKET, "has-session", "-t", f"={SESSION}"]])

    def test_uid_mismatch_refuses(self) -> None:
        # a socket that is not the seat's: owned by someone else
        log: list = []
        self.assertIsNone(self.read(stat=sock_stat(uid=UID + 1), run=child(1, "no server running", log)))
        self.assertEqual(log, [])
        # the path exists, but the server will not talk to this caller
        self.assertIsNone(self.read(run=child(1, "error connecting to x (Permission denied)")))

    def test_unreadable_path_non_socket_and_probe_failures_refuse(self) -> None:
        def denied(path):
            raise PermissionError(path)

        self.assertIsNone(self.read(stat=denied))
        self.assertIsNone(self.read(stat=sock_stat(mode=stat_mod.S_IFREG | 0o600)))
        for exc in (FileNotFoundError("tmux"), subprocess.TimeoutExpired("tmux", 1), OSError("boom")):
            with self.subTest(exc=type(exc).__name__):
                def run(argv, _exc=exc):
                    raise _exc

                self.assertIsNone(self.read(run=run))

    def test_undeterminable_uid_or_bad_session_refuses(self) -> None:
        for uid in (None, -1, True, "1000"):
            with self.subTest(uid=uid):
                self.assertIsNone(self.read(uid=uid))
        for session in (None, "", "-x", "a b", "a:b", "x" * 80):
            with self.subTest(session=session):
                self.assertIsNone(self.read(session=session))

    def test_pseudo_entry_for_an_unnamed_session_is_unreadable(self) -> None:
        states = ACT.pane_liveness_states("", UID, run=child(0), stat=sock_stat())
        self.assertEqual(states, {"tmux-session:<unnamed>": None})


class RealBackendTests(unittest.TestCase):
    """LocalHostBackend: the real reader, with subprocess.run mocked (no tmux is ever executed)."""

    def backend(self):
        backend = PI.LocalHostBackend()
        backend._user_unit_read = lambda unit, purpose: SimpleNamespace(active_state="failed" if "controller" in unit else "inactive")
        return backend

    def op(self, **desired):
        base = {"persona_state_root": "/home/x/.local/state/pantheon/personas", "persona": "recovery",
                "timer": "pantheon-persona-consume@recovery.timer", "tmux_session": SESSION}
        base.update(desired)
        return PI.InstallOperation(
            operation_id=PI.RESIDUE_QUARANTINE_OP_ID, layer=1, kind=PI.RESIDUE_QUARANTINE_KIND,
            target="/home/x/.local/state/pantheon/personas/recovery", precondition={"state": "must-pass"},
            desired=base, inverse={}, privilege="systemd.user:x", owner="x", provenance={},
        )

    def liveness(self, *, run, stat=sock_stat(), uid=UID, desired=None):
        backend = self.backend()
        with mock.patch.object(PI.subprocess, "run", run), \
                mock.patch.object(PI.LocalHostBackend, "_pane_stat", staticmethod(stat)), \
                mock.patch.object(PI.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=uid)):
            return backend._quarantine_liveness(self.op(**(desired or {})), "recovery")

    def test_reader_uses_has_session_with_the_seat_socket_and_never_makes_a_server(self) -> None:
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        states = self.liveness(run=run)
        self.assertEqual(states[KEY], "live")
        run.assert_called_once()
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["tmux", "-S", SOCKET, "has-session", "-t", f"={SESSION}"])
        for server_verb in ("new-session", "start-server", "new", "attach-session", "ls", "list-sessions"):
            self.assertNotIn(server_verb, argv)

    def test_missing_socket_never_spawns_a_process(self) -> None:
        run = mock.Mock()
        self.assertEqual(self.liveness(run=run, stat=missing)[KEY], "absent")
        run.assert_not_called()

    def test_unresolvable_owner_refuses(self) -> None:
        backend = self.backend()
        with mock.patch.object(PI.subprocess, "run", mock.Mock()) as run, \
                mock.patch.object(PI.pwd, "getpwnam", side_effect=KeyError("x")):
            states = backend._quarantine_liveness(self.op(), "recovery")
        self.assertIsNone(states[KEY])
        run.assert_not_called()

    def test_failed_controller_plus_live_pane_refuses_through_the_shared_check(self) -> None:
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        backend = self.backend()
        backend._bootstrap_fire_pending = lambda timer: True
        with mock.patch.object(PI.subprocess, "run", run), \
                mock.patch.object(PI.LocalHostBackend, "_pane_stat", staticmethod(sock_stat())), \
                mock.patch.object(PI.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=UID)), \
                mock.patch.object(PI, "_bootstrap_residue_entries", return_value=[("persona-root", "x")]):
            result = backend._residue_quarantine_preflight(self.op())
        self.assertFalse(result.ok)
        self.assertEqual(result.classification, "conflicting")
        self.assertIn(SESSION, result.detail)

    def test_absent_pane_with_failed_controller_is_admitted_as_before(self) -> None:
        backend = self.backend()
        backend._bootstrap_fire_pending = lambda timer: True
        with mock.patch.object(PI.LocalHostBackend, "_pane_stat", staticmethod(missing)), \
                mock.patch.object(PI.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=UID)), \
                mock.patch.object(PI, "_bootstrap_residue_entries", return_value=[("persona-root", "x")]):
            result = backend._residue_quarantine_preflight(self.op())
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.classification, "ready")

    def test_plan_without_a_session_refuses(self) -> None:
        run = mock.Mock()
        desired = {"tmux_session": ""}
        self.assertIsNone(self.liveness(run=run, desired=desired)["tmux-session:<unnamed>"])
        run.assert_not_called()


class SimBackendTests(unittest.TestCase):
    def test_injectable_pane_state(self) -> None:
        backend = PI.SimulatedHostBackend()
        op = RealBackendTests().op()
        self.assertEqual(backend._sim_pane_states(op), {KEY: "absent"})
        backend.live_panes.add(SESSION)
        self.assertEqual(backend._sim_pane_states(op), {KEY: "live"})
        backend.unreadable_panes.add(SESSION)
        self.assertEqual(backend._sim_pane_states(op), {KEY: None})


if __name__ == "__main__":
    unittest.main()
