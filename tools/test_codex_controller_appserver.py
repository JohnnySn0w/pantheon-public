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
import unittest
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
                 model: str = "gpt-fake"):
        self.path, self.thread_id, self.cwd, self.model = path, thread_id, cwd, model
        self.methods = []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self):
        self.sock.close()

    def _send(self, conn, doc):
        body = json.dumps(doc).encode()
        if len(body) < 126:
            header = bytes([0x81, len(body)])
        else:
            header = bytes([0x81, 126]) + struct.pack(">H", len(body))
        conn.sendall(header + body)

    def _serve(self):
        try:
            while True:
                conn, _ = self.sock.accept()
                self._serve_connection(conn)
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
                self._send(conn, {"id": doc["id"], "result": result})


def _run_fake_codex() -> int:
    """Disposable app-server/TUI process for the real wrapper boundary test."""
    pidfile = Path(os.environ["T5740_FAKE_PIDFILE"])
    with open(pidfile, "a", encoding="utf-8") as fh:
        fh.write(f"{os.getpid()}\n")
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
        switch = self._switch()
        rollout = self.home / ".codex/sessions/2026/10/29"
        rollout.mkdir(parents=True)
        (rollout / "rollout-fixture-thread-current.jsonl").write_text("{}\n")
        raw_nonce = "-mr3h4r3UYL6Oi606h7xmIxWHSSDdLn8ljT_vL8i8dE"
        with mock.patch("tools.harness_adapter.codex.secrets.token_urlsafe",
                        return_value=raw_nonce):
            spec = self.adapter.build_interactive_launch(
                self.binding.controller.tmux, cwd=self.binding.launch_cwd,
            )
        nonce = spec.argv[spec.argv.index("--launch-nonce") + 1]
        self.assertEqual(nonce, f"n_{raw_nonce}")

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
            argv, cwd=spec.cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

        def stop_wrapper():
            switch.unlink(missing_ok=True)
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(5)

        self.addCleanup(stop_wrapper)
        ready = threading.Event()
        for _ in range(200):
            if self.adapter.appserver_input_ready():
                ready.set()
                break
            if proc.poll() is not None:
                break
            ready.wait(0.05)
        if not ready.is_set():
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
        fake_pids = [int(line) for line in pidfile.read_text().splitlines()]
        peer_pids = [
            json.loads(line)["pid"]
            for line in (self.home / ".local/state/pantheon/codex-appserver/peer-audit.jsonl")
            .read_text().splitlines()
        ]
        self.assertTrue(fake_pids)
        self.assertTrue(peer_pids)
        self.assertTrue(all(not _pid_alive(pid) for pid in set(fake_pids + peer_pids)))

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
        server = ManagedWriterServer(sock, thread_id=thread_id, cwd=str(self.binding.launch_cwd))
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
        with mock.patch.object(runtime_controller, "_runtime_alive", return_value=True), \
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
                mock.patch.object(runtime_controller, "_stop_created_scope_best_effort") as stop:
            with self.assertRaisesRegex(runtime_controller.RuntimeOperationalFailure,
                                        "input readiness"):
                runtime_controller._verify_runtime_input_ready(
                    self.binding,
                    runtime_controller.RuntimeStart("test.scope", True),
                    state_root=self.root / "state", adapter=self.adapter,
                )
        stop.assert_called_once_with("test.scope")

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
