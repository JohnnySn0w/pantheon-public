"""T-5740 controller-owned launch selection and controller-safe rollback."""
from __future__ import annotations

import json
import os
import base64
import hashlib
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from unittest import mock
from pathlib import Path

from tools.harness_adapter.codex import CodexHarnessAdapter
from tools.persona_bringup import load_binding
from tools import runtime_controller


REPO = Path(__file__).resolve().parent.parent


def _read_exact(conn, count):
    out = b""
    while len(out) < count:
        chunk = conn.recv(count - len(out))
        if not chunk:
            raise EOFError
        out += chunk
    return out


class ManagedWriterServer:
    """Minimal real AF_UNIX WebSocket app-server for the writer CLI subprocess."""

    def __init__(self, path: Path, *, thread_id: str, cwd: str,
                 model: str = "gpt-fake", hold_loaded_list: bool = False):
        self.path, self.thread_id, self.cwd, self.model = path, thread_id, cwd, model
        self.methods = []
        self.loaded_list_request = threading.Event()
        self.release_loaded_list_response = threading.Event()
        if not hold_loaded_list:
            self.release_loaded_list_response.set()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(1)
        self.sock.settimeout(0.1)
        self.stopping = threading.Event()
        self.connection_lock = threading.Lock()
        self.connections = set()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()
        self.release_loaded_list_response.set()
        self.sock.close()
        with self.connection_lock:
            connections = list(self.connections)
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        self.thread.join(1)
        self.path.unlink(missing_ok=True)
        if self.thread.is_alive():
            raise RuntimeError("managed writer listener thread did not stop")

    def _send(self, conn, doc):
        body = json.dumps(doc).encode()
        if len(body) < 126:
            header = bytes([0x81, len(body)])
        else:
            header = bytes([0x81, 126]) + struct.pack(">H", len(body))
        conn.sendall(header + body)

    def _serve(self):
        try:
            while not self.stopping.is_set():
                try:
                    conn, _ = self.sock.accept()
                except socket.timeout:
                    continue
                if self.stopping.is_set():
                    conn.close()
                    return
                with self.connection_lock:
                    self.connections.add(conn)
                try:
                    self._serve_connection(conn)
                finally:
                    with self.connection_lock:
                        self.connections.discard(conn)
        except (EOFError, OSError):
            return

    def _serve_connection(self, conn):
        with conn:
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(4096)
            key = next(line.split(b":", 1)[1].strip() for line in request.splitlines()
                       if line.lower().startswith(b"sec-websocket-key:"))
            accept = base64.b64encode(hashlib.sha1(
                key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                         b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            while True:
                b0, b1 = _read_exact(conn, 2)
                length = b1 & 0x7f
                if length == 126:
                    length = struct.unpack(">H", _read_exact(conn, 2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", _read_exact(conn, 8))[0]
                mask = _read_exact(conn, 4) if b1 & 0x80 else b""
                payload = _read_exact(conn, length)
                if mask:
                    payload = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
                if b0 & 0x0f == 0x8:
                    return
                doc = json.loads(payload)
                method = doc.get("method")
                self.methods.append(method)
                if "id" not in doc:
                    continue
                if method == "thread/resume":
                    result = {"thread": {"id": self.thread_id, "status": {"type": "idle"}},
                              "cwd": self.cwd, "model": self.model}
                elif method == "account/read":
                    result = {"account": {"email": "watchman@example.test"}}
                elif method == "thread/loaded/list":
                    result = {"data": [self.thread_id], "nextCursor": None}
                else:
                    result = {}
                if method == "thread/loaded/list":
                    self.loaded_list_request.set()
                    self.release_loaded_list_response.wait()
                self._send(conn, {"id": doc["id"], "result": result})


@dataclass(frozen=True)
class _ProcessGroupIdentity:
    pid: int
    pgrp: int
    sid: int
    starttime: int


def _process_identity(pid: int) -> _ProcessGroupIdentity | None:
    try:
        tail = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return _ProcessGroupIdentity(pid, int(tail[2]), int(tail[3]), int(tail[19]))
    except (FileNotFoundError, ProcessLookupError):
        return None


def _record_fake_identity(pidfile: Path) -> None:
    identity = _process_identity(os.getpid())
    if identity is None:
        raise RuntimeError("fake process disappeared before recording its identity")
    role = "app-server" if len(sys.argv) > 1 and sys.argv[1] == "app-server" else "tui"
    record = {
        "pid": identity.pid,
        "pgrp": identity.pgrp,
        "sid": identity.sid,
        "starttime": identity.starttime,
        "role": role,
    }
    with open(pidfile, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _run_fake_codex() -> int:
    """Disposable app-server/TUI process for the real wrapper boundary test."""
    pidfile = Path(os.environ["T5740_FAKE_PIDFILE"])
    _record_fake_identity(pidfile)
    if len(sys.argv) > 1 and sys.argv[1] == "app-server":
        listen = sys.argv[sys.argv.index("--listen") + 1]
        server = ManagedWriterServer(
            Path(listen.removeprefix("unix://")),
            thread_id=os.environ["T5740_FAKE_THREAD"],
            cwd=os.environ["T5740_FAKE_CWD"],
            model=os.environ["T5740_FAKE_MODEL"],
        )
        signal.signal(signal.SIGTERM, lambda *_: raise_system_exit())
        signal.signal(signal.SIGINT, lambda *_: raise_system_exit())
        try:
            while True:
                signal.pause()
        finally:
            server.close()
    signal.signal(signal.SIGTERM, lambda *_: raise_system_exit())
    signal.signal(signal.SIGINT, lambda *_: raise_system_exit())
    while True:
        signal.pause()


def raise_system_exit() -> None:
    raise SystemExit(0)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class _ControllerOwnedFixtureCleanup:
    """Own every disposable process group and socket even if its wrapper dies."""

    def __init__(self, *, wrapper: subprocess.Popen, wrapper_identity: _ProcessGroupIdentity,
                 switch: Path, pidfile: Path, rundir: Path, fake: Path):
        self.wrapper = wrapper
        self.wrapper_identity = wrapper_identity
        self.switch = switch
        self.pidfile = pidfile
        self.rundir = rundir
        self.fake = fake
        self.closed = False

    def _fake_identities(self) -> list[_ProcessGroupIdentity]:
        records = {}
        if self.pidfile.exists():
            for line in self.pidfile.read_text(encoding="utf-8").splitlines():
                try:
                    doc = json.loads(line)
                    identity = _ProcessGroupIdentity(
                        int(doc["pid"]), int(doc["pgrp"]), int(doc["sid"]),
                        int(doc["starttime"]),
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                records[identity.pgrp] = identity
        # The exact private executable path is a fallback if interruption lands
        # while its append-only identity record is being written.
        expected = os.fsencode(self.fake)
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                argv = (entry / "cmdline").read_bytes().split(b"\0")
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
            if expected not in argv:
                continue
            identity = _process_identity(int(entry.name))
            if identity is None:
                continue
            if identity.pid != identity.pgrp or identity.pid != identity.sid:
                raise RuntimeError(f"fake pid {identity.pid} is not its recorded session leader")
            records[identity.pgrp] = identity
        return list(records.values())

    @staticmethod
    def _members(group: _ProcessGroupIdentity) -> dict[int, _ProcessGroupIdentity]:
        members = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            identity = _process_identity(int(entry.name))
            if identity is None or identity.pgrp != group.pgrp:
                continue
            if identity.sid != group.sid or identity.starttime < group.starttime:
                raise RuntimeError(
                    f"fixture process group {group.pgrp} was reused or changed identity"
                )
            if identity.pid == group.pid and identity.starttime != group.starttime:
                raise RuntimeError(f"fixture group leader {group.pid} changed identity")
            members[identity.pid] = identity
        return members

    @classmethod
    def _signal_members(cls, group: _ProcessGroupIdentity, sig: signal.Signals) -> None:
        handles = []
        try:
            for pid, identity in cls._members(group).items():
                fd = os.pidfd_open(pid, 0)
                handles.append((fd, identity))
            for fd, expected in handles:
                if _process_identity(expected.pid) != expected:
                    raise RuntimeError(f"fixture pid {expected.pid} changed identity before signal")
                signal.pidfd_send_signal(fd, sig)
        finally:
            for fd, _identity in handles:
                os.close(fd)

    @classmethod
    def _wait_empty(cls, group: _ProcessGroupIdentity, timeout: float,
                    reaper: subprocess.Popen | None = None) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if reaper is not None:
                reaper.poll()
            if not cls._members(group):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    @classmethod
    def _extinguish(cls, group: _ProcessGroupIdentity,
                    reaper: subprocess.Popen | None = None) -> None:
        if not cls._members(group):
            return
        cls._signal_members(group, signal.SIGTERM)
        if cls._wait_empty(group, 2, reaper):
            return
        cls._signal_members(group, signal.SIGKILL)
        if not cls._wait_empty(group, 2, reaper):
            raise RuntimeError(f"fixture process group {group.pgrp} survived TERM/KILL")

    def close(self) -> None:
        if self.closed:
            return
        errors: list[BaseException] = []
        try:
            self.switch.unlink(missing_ok=True)
        except OSError as exc:
            # Continue extinguishing every owned process.  A failed first close
            # remains retryable; it must never turn the rest of cleanup into a
            # no-op merely because one path operation failed.
            errors.append(exc)
        try:
            # Extinguish and reap the only process that can launch a new fake
            # session before taking the final inventory. Inventory-first has a
            # real race: start_pane() can cross it and orphan a late TUI.
            self._extinguish(self.wrapper_identity, self.wrapper)
        except (OSError, RuntimeError) as exc:
            errors.append(str(exc))
        for _attempt in range(3):
            try:
                fake_groups = [
                    group for group in self._fake_identities() if self._members(group)
                ]
            except (OSError, RuntimeError) as exc:
                errors.append(f"cannot inventory fake fixture groups: {exc}")
                break
            if not fake_groups:
                break
            for group in fake_groups:
                try:
                    self._extinguish(group)
                except (OSError, RuntimeError) as exc:
                    errors.append(str(exc))
            time.sleep(0.02)
        else:
            errors.append("fake fixture groups kept appearing after wrapper teardown")
        try:
            self.wrapper.wait(timeout=2)
        except subprocess.TimeoutExpired:
            errors.append(f"wrapper pid {self.wrapper.pid} was not reaped")
        if self.wrapper.stdout is not None:
            try:
                self.wrapper.stdout.close()
            except OSError as exc:
                errors.append(exc)
        for name in (".server.sock", "as.sock"):
            try:
                (self.rundir / name).unlink(missing_ok=True)
            except OSError as exc:
                errors.append(exc)
        if errors:
            if len(errors) == 1 and isinstance(errors[0], OSError):
                raise errors[0]
            raise RuntimeError("; ".join(str(error) for error in errors))
        self.closed = True


class ControllerAppServerLaunchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "codex-fallback"
        self.home.mkdir()
        self.binding = load_binding(REPO / "instance.yaml", "watchman")
        self.adapter = CodexHarnessAdapter(
            "system-orchestrator-1", codex_home=self.home, launch=self.binding.launch,
        )

    def _switch(self, **updates):
        doc = {
            "enabled": True,
            "seat": "watchman",
            "state_dir": "~/.local/state/pantheon/codex-appserver",
            "socket": str(self.root / "run/codex-appserver/as.sock"),
        }
        doc.update(updates)
        path = self.home / ".config/pantheon/codex-appserver-route.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(doc))
        state = self.home / ".local/state/pantheon/codex-appserver"
        state.mkdir(parents=True, exist_ok=True)
        (state / "thread.json").write_text(json.dumps({"thread_id": "thread-current"}))
        return path

    def _launch_real_wrapper_fixture(self, raw_nonce: str):
        switch = self._switch()
        rollout = self.home / ".codex/sessions/2026/10/29"
        rollout.mkdir(parents=True, exist_ok=True)
        (rollout / "rollout-fixture-thread-current.jsonl").write_text("{}\n")
        with mock.patch("tools.harness_adapter.codex.secrets.token_urlsafe",
                        return_value=raw_nonce):
            spec = self.adapter.build_interactive_launch(
                self.binding.controller.tmux, cwd=self.binding.launch_cwd,
            )
        fake = self.root / "fake-codex"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "from tools.test_codex_controller_appserver import _run_fake_codex\n"
            "raise SystemExit(_run_fake_codex())\n"
        )
        fake.chmod(0o700)
        argv = list(spec.argv)
        argv[argv.index("--codex-bin") + 1] = str(fake)
        argv[argv.index("--account") + 1] = "fixture-unbound"
        argv += ["--poll", "0.02"]
        pidfile = self.root / "fake-pids"
        env = {
            **os.environ, **spec.env,
            "T5740_FAKE_PIDFILE": str(pidfile),
            "T5740_FAKE_THREAD": "thread-current",
            "T5740_FAKE_CWD": str(self.binding.launch_cwd),
            "T5740_FAKE_MODEL": "gpt-5.6-sol",
            "PYTHONPATH": str(REPO),
        }
        proc = subprocess.Popen(
            argv, cwd=spec.cwd, env=env, start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        wrapper_identity = _process_identity(proc.pid)
        if (wrapper_identity is None or wrapper_identity.pgrp != proc.pid or
                wrapper_identity.sid != proc.pid):
            proc.kill()
            proc.wait(2)
            self.fail("fixture wrapper did not become its own process-group/session leader")
        rundir = Path(argv[argv.index("--rundir") + 1])
        fixture = _ControllerOwnedFixtureCleanup(
            wrapper=proc, wrapper_identity=wrapper_identity, switch=switch,
            pidfile=pidfile, rundir=rundir, fake=fake,
        )
        self.addCleanup(fixture.close)
        return fixture, spec, pidfile

    def _wait_for_real_wrapper_readiness(self, proc: subprocess.Popen) -> bool:
        for _ in range(200):
            if self.adapter.appserver_input_ready():
                return True
            if proc.poll() is not None:
                return False
            time.sleep(0.05)
        return False

    def _wait_for_writer_receipt(self, proc: subprocess.Popen, *, timeout: float = 120) -> None:
        """Wait on the writer's exact current-launch receipt, not scheduler luck.

        The generous deadline is only a hung-fixture guard.  Tests that use this
        handshake run the controller assertion with a zero-second readiness
        timeout, so host load cannot decide whether the assertion itself passes.
        """
        deadline = time.monotonic() + timeout
        while True:
            if self.adapter.appserver_input_ready():
                return
            returncode = proc.poll()
            if returncode is not None:
                self.fail(f"writer exited before publishing readiness (rc {returncode})")
            if time.monotonic() >= deadline:
                self.fail("writer did not publish the current-launch readiness receipt")
            time.sleep(0.02)

    def _wait_for_real_fixture_steady(self, fixture, pidfile: Path) -> dict:
        """Wait for the writer and both fake roles, not merely the receipt."""
        for _ in range(200):
            records = []
            if pidfile.exists():
                for line in pidfile.read_text(encoding="utf-8").splitlines():
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            roles = {record.get("role") for record in records}
            members = fixture._members(fixture.wrapper_identity)
            writer_pids = []
            for pid in members:
                try:
                    argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                except (FileNotFoundError, ProcessLookupError, PermissionError):
                    continue
                if any(part.endswith(b"codex_seat_delivery.py") for part in argv):
                    writer_pids.append(pid)
            socket_paths = [fixture.rundir / ".server.sock", fixture.rundir / "as.sock"]
            if (roles >= {"app-server", "tui"} and writer_pids
                    and all(path.exists() for path in socket_paths)
                    and all(_pid_alive(int(record["pid"])) for record in records)):
                return {"records": records, "writer_pids": writer_pids,
                        "socket_paths": socket_paths}
            if fixture.wrapper.poll() is not None:
                break
            time.sleep(0.05)
        return {}

    def test_absent_or_malformed_switch_is_exact_bare_tui_rollback(self):
        for malformed in (None, "{broken", json.dumps({"enabled": False})):
            path = self.home / ".config/pantheon/codex-appserver-route.json"
            if malformed is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(malformed)
            spec = self.adapter.build_interactive_launch(
                self.binding.controller.tmux, cwd=self.binding.launch_cwd,
            )
            self.assertEqual(spec.argv[0], "/usr/bin/codex")
            self.assertNotIn("appserver_supervise.py", " ".join(spec.argv))

    def test_enabled_switch_builds_one_controller_owned_runtime(self):
        switch = self._switch()
        spec = self.adapter.build_interactive_launch(
            self.binding.controller.tmux, cwd=self.binding.launch_cwd,
        )
        self.assertIn("appserver_supervise.py", " ".join(spec.argv))
        self.assertIn("--controller-owned", spec.argv)
        self.assertIn("--manage-writer", spec.argv)
        self.assertNotIn("--server-unit", spec.argv)
        self.assertNotIn("pantheon-runtime-controller@watchman.service", spec.argv)
        self.assertIn(str(switch), spec.argv)
        self.assertEqual(spec.argv[spec.argv.index("--persona") + 1], "watchman")
        self.assertEqual(spec.argv[spec.argv.index("--cwd") + 1], str(self.binding.launch_cwd))
        self.assertEqual(spec.cwd, self.binding.launch_cwd)
        nonce = spec.argv[spec.argv.index("--launch-nonce") + 1]
        receipt = Path(spec.argv[spec.argv.index("--readiness-receipt") + 1])
        self.assertGreaterEqual(len(nonce), 32)
        self.assertFalse(receipt.exists())

    def test_specialist_fallback_builds_controller_owned_runtime_from_its_own_clone(self):
        """T-5741: Specialist must not inherit Watchman account or instance-root assumptions."""
        home = self.root / "claude-research"
        home.mkdir()
        binding = load_binding(REPO / "instance.yaml", "specialist", harness_override="codex")
        binding = binding.__class__(
            **{**binding.__dict__, "account_home": home, "launch_cwd": REPO}
        )
        adapter = CodexHarnessAdapter(
            "specialist", codex_home=home, launch=binding.launch,
        )
        route = home / ".config/pantheon/codex-appserver-route.json"
        route.parent.mkdir(parents=True)
        state = home / ".local/state/pantheon/codex-appserver"
        state.mkdir(parents=True)
        (state / "thread.json").write_text(json.dumps({"thread_id": "specialist-thread"}))
        route.write_text(json.dumps({
            "enabled": True,
            "seat": "specialist",
            "state_dir": "~/.local/state/pantheon/codex-appserver",
            "socket": str(self.root / "run-specialist/codex-appserver/as.sock"),
        }))

        spec = adapter.build_interactive_launch(binding.controller.tmux, cwd=REPO)

        self.assertIn("--controller-owned", spec.argv)
        self.assertIn("--manage-writer", spec.argv)
        self.assertEqual(spec.argv[spec.argv.index("--persona") + 1], "specialist")
        self.assertEqual(spec.argv[spec.argv.index("--account") + 1], "claude-research")
        self.assertEqual(spec.argv[spec.argv.index("--cwd") + 1], str(REPO))
        self.assertNotIn("watchman", spec.argv)
        self.assertNotIn("--server-unit", spec.argv)
        self.assertEqual(spec.cwd, REPO)

        route.unlink()
        bare = adapter.build_interactive_launch(binding.controller.tmux, cwd=REPO)
        self.assertEqual(bare.argv[0], "/usr/bin/codex")
        self.assertFalse(adapter.appserver_input_readiness_enabled())

    def test_generated_launch_nonce_is_option_safe_for_every_raw_prefix(self):
        self._switch()
        for raw_nonce in ("-leading-option", "_leading-underscore", "alphanumeric"):
            with self.subTest(raw_nonce=raw_nonce), mock.patch(
                "tools.harness_adapter.codex.secrets.token_urlsafe", return_value=raw_nonce,
            ):
                spec = self.adapter.build_interactive_launch(
                    self.binding.controller.tmux, cwd=self.binding.launch_cwd,
                )
            nonce = spec.argv[spec.argv.index("--launch-nonce") + 1]
            self.assertEqual(nonce, f"n_{raw_nonce}")
            self.assertFalse(nonce.startswith("-"))

    def test_leading_dash_random_nonce_reaches_real_wrapper_writer_readiness(self):
        """The 18:56Z failure must reach propagated readiness, not just parse/build."""
        raw_nonce = "-mr3h4r3UYL6Oi606h7xmIxWHSSDdLn8ljT_vL8i8dE"
        fixture, spec, pidfile = self._launch_real_wrapper_fixture(raw_nonce)
        switch = fixture.switch
        proc = fixture.wrapper
        nonce = spec.argv[spec.argv.index("--launch-nonce") + 1]
        self.assertEqual(nonce, f"n_{raw_nonce}")
        if not self._wait_for_real_wrapper_readiness(proc):
            output, _ = proc.communicate(timeout=5)
            child_log = self.home / ".local/state/pantheon/codex-appserver/app-server.log"
            child_output = child_log.read_text() if child_log.exists() else "<absent>"
            self.fail(
                f"wrapper exited before propagated readiness rc={proc.returncode}: {output}\n"
                f"app-server log: {child_output}"
            )

        switch.unlink()
        output, _ = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, output)
        self.assertIn("started delivery writer", output)
        fake_pids = [json.loads(line)["pid"] for line in pidfile.read_text().splitlines()]
        peer_pids = [
            json.loads(line)["pid"]
            for line in (self.home / ".local/state/pantheon/codex-appserver/peer-audit.jsonl")
            .read_text().splitlines()
        ]
        self.assertTrue(fake_pids)
        self.assertTrue(peer_pids)
        self.assertTrue(all(not _pid_alive(pid) for pid in set(fake_pids + peer_pids)))

    def test_abrupt_wrapper_death_cleanup_reaps_fake_groups_and_private_sockets(self):
        """Suite interruption cannot orphan the fake app-server or its socket."""
        fixture, _spec, pidfile = self._launch_real_wrapper_fixture("interrupt-fixture")
        self.assertTrue(self._wait_for_real_wrapper_readiness(fixture.wrapper))
        steady = self._wait_for_real_fixture_steady(fixture, pidfile)
        self.assertTrue(steady, "both fake roles and the delivery writer must be live")
        identities = fixture._fake_identities()
        self.assertEqual({record["role"] for record in steady["records"]},
                         {"app-server", "tui"})
        self.assertGreaterEqual(len(identities), 2)
        socket_paths = steady["socket_paths"]

        os.kill(fixture.wrapper.pid, signal.SIGKILL)
        fixture.close()

        self.assertEqual(fixture.wrapper.returncode, -signal.SIGKILL)
        self.assertTrue(all(not _pid_alive(pid) for pid in steady["writer_pids"]))
        self.assertFalse(fixture._members(fixture.wrapper_identity))
        self.assertTrue(all(not _pid_alive(identity.pid) for identity in identities))
        self.assertTrue(all(not fixture._members(identity) for identity in identities))
        self.assertTrue(all(not path.exists() for path in socket_paths))

    def test_cleanup_unlink_failure_is_best_effort_and_retryable(self):
        fixture, _spec, pidfile = self._launch_real_wrapper_fixture("unlink-failure")
        self.assertTrue(self._wait_for_real_wrapper_readiness(fixture.wrapper))
        steady = self._wait_for_real_fixture_steady(fixture, pidfile)
        self.assertTrue(steady, "exception probe requires both fake roles and writer")
        identities = fixture._fake_identities()
        real_unlink = Path.unlink
        failed = []

        def fail_switch_once(path, *args, **kwargs):
            if path == fixture.switch and not failed:
                failed.append(True)
                raise OSError("injected switch unlink failure")
            return real_unlink(path, *args, **kwargs)

        try:
            with mock.patch.object(Path, "unlink", autospec=True,
                                   side_effect=fail_switch_once):
                with self.assertRaisesRegex(OSError, "injected switch unlink failure"):
                    fixture.close()
            self.assertFalse(fixture.closed, "a failed close must remain retryable")
            self.assertIsNotNone(fixture.wrapper.poll())
            self.assertFalse(fixture._members(fixture.wrapper_identity))
            self.assertTrue(all(not fixture._members(identity) for identity in identities))
            self.assertTrue(all(not path.exists() for path in steady["socket_paths"]))
        finally:
            # This also prevents the deliberately-red old implementation from
            # leaking its disposable fake processes when the assertion fires.
            fixture.closed = False
            fixture.close()
        self.assertTrue(fixture.closed)

    def test_managed_writer_close_joins_listener_with_active_peer(self):
        path = self.root / "active-writer.sock"
        server = ManagedWriterServer(
            path, thread_id="thread-current", cwd=str(self.binding.launch_cwd),
        )
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            peer.connect(str(path))
            for _ in range(100):
                with server.connection_lock:
                    if server.connections:
                        break
                time.sleep(0.01)
            else:
                self.fail("writer listener never accepted the active peer")
            server.close()
            self.assertFalse(server.thread.is_alive())
            self.assertFalse(path.exists())
            with server.connection_lock:
                self.assertFalse(server.connections)
        finally:
            peer.close()
            if server.thread.is_alive() or path.exists():
                server.close()

    def test_current_generation_exact_owner_only_receipt_is_required(self):
        self._switch()
        self.adapter.build_interactive_launch(
            self.binding.controller.tmux, cwd=self.binding.launch_cwd,
        )
        path, persona, thread_id, nonce = self.adapter._appserver_readiness
        valid = {"schema": "pantheon-codex-appserver-input-ready/v1", "persona": persona,
                 "thread_id": thread_id, "launch_nonce": nonce}
        cases = [
            {**valid, "launch_nonce": "stale"},
            {**valid, "thread_id": "wrong"},
            {**valid, "persona": "wrong"},
            {**valid, "extra": True},
            [valid],
        ]
        for doc in cases:
            path.write_text(json.dumps(doc))
            path.chmod(0o600)
            self.assertFalse(self.adapter.appserver_input_ready(), doc)
        path.write_text("{malformed")
        path.chmod(0o600)
        self.assertFalse(self.adapter.appserver_input_ready())
        path.write_text(json.dumps(valid))
        path.chmod(0o644)
        self.assertFalse(self.adapter.appserver_input_ready())
        path.chmod(0o600)
        self.assertTrue(self.adapter.appserver_input_ready())

    def test_new_launch_nonce_rejects_prior_generation_receipt(self):
        self._switch()
        first = self.adapter.build_interactive_launch(
            self.binding.controller.tmux, cwd=self.binding.launch_cwd,
        )
        old_nonce = first.argv[first.argv.index("--launch-nonce") + 1]
        receipt = Path(first.argv[first.argv.index("--readiness-receipt") + 1])
        receipt.write_text(json.dumps({"schema": "pantheon-codex-appserver-input-ready/v1",
                                       "persona": "watchman", "thread_id": "thread-current",
                                       "launch_nonce": old_nonce}))
        receipt.chmod(0o600)
        second = self.adapter.build_interactive_launch(
            self.binding.controller.tmux, cwd=self.binding.launch_cwd,
        )
        new_nonce = second.argv[second.argv.index("--launch-nonce") + 1]
        self.assertNotEqual(old_nonce, new_nonce)
        self.assertFalse(receipt.exists(), "prior-generation receipt must be cleared")

    def test_structural_readiness_never_captures_or_mutates_remote_tui(self):
        self._switch()
        self.adapter.build_interactive_launch(
            self.binding.controller.tmux, cwd=self.binding.launch_cwd,
        )
        path, persona, thread_id, nonce = self.adapter._appserver_readiness
        path.write_text(json.dumps({"schema": "pantheon-codex-appserver-input-ready/v1",
                                    "persona": persona, "thread_id": thread_id,
                                    "launch_nonce": nonce}))
        path.chmod(0o600)
        with mock.patch.object(runtime_controller, "_runtime_alive", return_value=True), \
                mock.patch.object(runtime_controller, "_capture_visible_runtime_pane") as capture:
            runtime_controller._verify_runtime_input_ready(
                self.binding,
                runtime_controller.RuntimeStart(runtime_controller.TMUX_RUNTIME_SCOPE_UNIT, False),
                state_root=self.root / "state", adapter=self.adapter,
            )
        capture.assert_not_called()

    def test_live_failure_reproduction_remote_tui_never_ready_writer_subprocess_passes(self):
        """The live 20s failure: TUI pixels stay unready while the writer proves ready."""
        self._switch()
        self.adapter.build_interactive_launch(
            self.binding.controller.tmux, cwd=self.binding.launch_cwd,
        )
        path, persona, thread_id, nonce = self.adapter._appserver_readiness
        sock = self.root / "writer.sock"
        server = ManagedWriterServer(
            sock, thread_id=thread_id, cwd=str(self.binding.launch_cwd), hold_loaded_list=True,
        )
        self.addCleanup(server.close)
        with mock.patch.object(runtime_controller, "_capture_visible_runtime_pane", return_value=""):
            old_pane_result = runtime_controller._wait_for_runtime_input_ready(
                self.binding.controller.tmux, harness="codex", profile=self.binding.launch.profile,
                adapter=self.adapter, timeout=0,
            )
        self.assertFalse(old_pane_result.ready, "the reproduced old pane boundary must time out")
        proc = subprocess.Popen([
            sys.executable, str(REPO / "tools/codex_seat_delivery.py"), "run",
            "--state-dir", str(path.parent), "--sock", str(sock), "--seat", persona,
            "--sandbox", "danger-full-access", "--approval-policy", "never",
            "--model", "gpt-fake", "--cwd", str(self.binding.launch_cwd),
            "--account-email", "watchman@example.test",
            "--readiness-receipt", str(path), "--launch-nonce", nonce,
        ], cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertTrue(
            server.loaded_list_request.wait(120), "writer never reached readiness handshake",
        )
        self.assertIsNone(proc.poll(), "writer must still be live at the reproduced boundary")
        self.assertFalse(self.adapter.appserver_input_ready())
        with mock.patch.object(runtime_controller, "RUNTIME_INPUT_READY_TIMEOUT_SECONDS", 0), \
                mock.patch.object(runtime_controller, "APP_SERVER_INPUT_READY_TIMEOUT_SECONDS", 0), \
                mock.patch.object(runtime_controller, "_runtime_alive", return_value=True), \
                mock.patch.object(runtime_controller, "_capture_visible_runtime_pane") as parked_capture:
            with self.assertRaisesRegex(
                runtime_controller.RuntimeOperationalFailure, "input readiness",
            ):
                runtime_controller._verify_runtime_input_ready(
                    self.binding,
                    runtime_controller.RuntimeStart(
                        runtime_controller.TMUX_RUNTIME_SCOPE_UNIT, False,
                    ),
                    state_root=self.root / "state", adapter=self.adapter,
                )
        parked_capture.assert_not_called()

        server.release_loaded_list_response.set()
        self._wait_for_writer_receipt(proc)
        with mock.patch.object(runtime_controller, "RUNTIME_INPUT_READY_TIMEOUT_SECONDS", 0), \
                mock.patch.object(runtime_controller, "APP_SERVER_INPUT_READY_TIMEOUT_SECONDS", 0), \
                mock.patch.object(runtime_controller, "_runtime_alive", return_value=True), \
                mock.patch.object(runtime_controller, "_capture_visible_runtime_pane") as capture:
            runtime_controller._verify_runtime_input_ready(
                self.binding,
                runtime_controller.RuntimeStart(runtime_controller.TMUX_RUNTIME_SCOPE_UNIT, False),
                state_root=self.root / "state", adapter=self.adapter,
            )
        proc.terminate()
        self.assertEqual(proc.wait(3), -15)
        self.assertEqual(server.methods[:5], [
            "initialize", "initialized", "thread/resume", "account/read",
            "thread/loaded/list",
        ])
        capture.assert_not_called()

    def test_wrapper_death_fails_promptly_through_existing_teardown(self):
        self._switch()
        self.adapter.build_interactive_launch(
            self.binding.controller.tmux, cwd=self.binding.launch_cwd,
        )
        with mock.patch.object(runtime_controller, "_runtime_alive", return_value=False), \
                mock.patch.object(runtime_controller,
                                  "_cleanup_failed_created_runtime") as cleanup:
            with self.assertRaisesRegex(runtime_controller.RuntimeOperationalFailure,
                                        "input readiness"):
                runtime_controller._verify_runtime_input_ready(
                    self.binding,
                    runtime_controller.RuntimeStart("test.scope", True),
                    state_root=self.root / "state", adapter=self.adapter,
                )
        cleanup.assert_called_once_with(
            self.binding, runtime_controller.RuntimeStart("test.scope", True), context=None,
        )

    def test_bare_codex_still_uses_pane_readiness(self):
        self.assertFalse(self.adapter.appserver_input_readiness_enabled())
        ready = runtime_controller.RuntimeInputReadiness(True, "pane-hash", "pane ready")
        with mock.patch.object(runtime_controller, "_wait_for_runtime_input_ready",
                               return_value=ready) as pane_wait:
            runtime_controller._verify_runtime_input_ready(
                self.binding,
                runtime_controller.RuntimeStart(runtime_controller.TMUX_RUNTIME_SCOPE_UNIT, False),
                state_root=self.root / "state", adapter=self.adapter,
            )
        pane_wait.assert_called_once()

    def test_same_adapter_appserver_then_route_off_restores_bare_pane_readiness(self):
        route = self._switch()
        self.adapter.build_interactive_launch(
            self.binding.controller.tmux, cwd=self.binding.launch_cwd,
        )
        self.assertTrue(self.adapter.appserver_input_readiness_enabled())
        route.unlink()
        bare = self.adapter.build_interactive_launch(
            self.binding.controller.tmux, cwd=self.binding.launch_cwd,
        )
        self.assertEqual(bare.argv[0], "/usr/bin/codex")
        self.assertFalse(self.adapter.appserver_input_readiness_enabled())
        ready = runtime_controller.RuntimeInputReadiness(True, "pane", "pane ready")
        with mock.patch.object(self.adapter, "appserver_input_ready") as stale_poll, \
                mock.patch.object(runtime_controller, "_wait_for_runtime_input_ready",
                                  return_value=ready) as pane_wait:
            runtime_controller._verify_runtime_input_ready(
                self.binding,
                runtime_controller.RuntimeStart(runtime_controller.TMUX_RUNTIME_SCOPE_UNIT, False),
                state_root=self.root / "state", adapter=self.adapter,
            )
        stale_poll.assert_not_called()
        pane_wait.assert_called_once()

    def test_receipt_reader_rejects_symlink_and_reads_one_descriptor(self):
        self._switch()
        self.adapter.build_interactive_launch(
            self.binding.controller.tmux, cwd=self.binding.launch_cwd,
        )
        path, persona, thread_id, nonce = self.adapter._appserver_readiness
        payload = {"schema": "pantheon-codex-appserver-input-ready/v1", "persona": persona,
                   "thread_id": thread_id, "launch_nonce": nonce}
        target = path.with_name("attacker-target")
        target.write_text(json.dumps(payload))
        target.chmod(0o600)
        path.symlink_to(target)
        self.assertFalse(self.adapter.appserver_input_ready())
        path.unlink()
        path.write_text(json.dumps(payload))
        path.chmod(0o600)
        replacement = path.with_name("replacement")
        replacement.write_text("{malformed")
        replacement.chmod(0o600)
        real_read = os.read
        substituted = []

        def substitute_after_open(fd, size):
            substituted.append(True)
            path.unlink()
            replacement.replace(path)
            return real_read(fd, size)

        with mock.patch("tools.harness_adapter.codex.os.read", side_effect=substitute_after_open):
            self.assertTrue(self.adapter.appserver_input_ready())
        self.assertEqual(substituted, [True], "reader must consume the already-fstat'd descriptor")

    def test_enabled_switch_for_another_account_is_refused(self):
        self._switch(seat="consultant")
        with self.assertRaisesRegex(ValueError, "belongs to account"):
            self.adapter.build_interactive_launch(
                self.binding.controller.tmux, cwd=self.binding.launch_cwd,
            )

    def test_controller_provenance_covers_the_owned_runtime(self):
        expected = {
            "harness/codex/appserver_seats.json",
            "harness/codex/appserver_supervise.py",
            "tools/codex_delivery.py",
            "tools/codex_seat_delivery.py",
        }
        self.assertTrue(expected.issubset(runtime_controller.RUNTIME_SOURCE_HASH_PATHS))


if __name__ == "__main__":
    unittest.main()
