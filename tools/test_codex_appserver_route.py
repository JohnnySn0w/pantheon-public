"""ORG-103 L4 (T-5306): the injector cutover to a codex seat's app-server delivery path.

Covers harness/codex/codex_telegram/appserver_route.py and its three callers (the
Telegram poller, keep-going, mailbox-wake). Every test passes an explicit `home` or
patches the checks, so no real switch file, socket or tmux is touched.

Run: python3 -m unittest tools.test_codex_appserver_route
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "harness" / "codex"))

from codex_telegram import appserver_route as ar  # noqa: E402
from tools import codex_delivery as cd  # noqa: E402
from tools import codex_seat_delivery as sd  # noqa: E402

ok = lambda *_a, **_k: (True, "ok")          # noqa: E731
down = lambda *_a, **_k: (False, "down")     # noqa: E731
WRAPPED = "[inbound Telegram DM from the operator | chat_id=1 | message_id=42] hello  (Reply via ...)"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.state = self.home / ".local/state/pantheon/codex-appserver"
        self.pasted: list[str] = []
        self.alerts: list[tuple] = []
        # A test must never mail Recovery for real (a malformed-switch case once did).
        p = mock.patch.object(ar, "alert_recovery", self.alert)
        p.start()
        self.addCleanup(p.stop)

    def switch(self, **over):
        cfg = {"enabled": True, "seat": "consultant",
               "state_dir": "~/.local/state/pantheon/codex-appserver",
               "socket": str(self.home / "as.sock")}
        cfg.update(over)
        p = self.home / ar.SWITCH_REL
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cfg))

    def paste(self, text):
        self.pasted.append(text)
        return True

    def alert(self, subject, body):
        self.alerts.append((subject, body))
        return True

    def deliver(self, text=WRAPPED, cls="operator_dm", writer=ok, sock=ok, **kw):
        return ar.deliver(text, cls, self.paste, injector="test", home=self.home,
                          writer_check=writer, socket_check=sock, alert=self.alert, **kw)

    def outcomes(self):
        p = self.state / "inject-outcomes.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def spool(self):
        return sorted((self.state / "spool").glob("*.json")) if (self.state / "spool").exists() else []


# ------------------------------------------------------------------ switch
class SwitchTests(Base):
    def test_no_switch_is_exactly_the_paste_path(self):
        self.assertTrue(self.deliver())
        self.assertEqual(self.pasted, [WRAPPED])
        self.assertEqual((self.spool(), self.outcomes(), self.alerts), ([], [], []))

    def test_disabled_switch_is_the_paste_path(self):
        self.switch(enabled=False)
        self.deliver()
        self.assertEqual(self.pasted, [WRAPPED])
        self.assertEqual(self.outcomes(), [])

    def test_malformed_switch_is_the_paste_path(self):
        self.switch()
        (self.home / ar.SWITCH_REL).write_text("{not json")
        self.deliver()
        self.assertEqual(self.pasted, [WRAPPED])

    def test_switch_missing_a_key_is_the_paste_path(self):
        self.switch(socket="")
        self.deliver()
        self.assertEqual(self.pasted, [WRAPPED])

    def test_tilde_state_dir_resolves_under_the_seat_home(self):
        self.switch()
        self.assertEqual(ar.load_switch(self.home)["state_dir"], str(self.state))


# ------------------------------------------------------------------ route on
class RouteTests(Base):
    def test_route_up_enqueues_and_never_pastes(self):
        self.switch()
        self.assertTrue(self.deliver())
        self.assertEqual(self.pasted, [])
        self.assertEqual(len(self.spool()), 1)
        doc = json.loads(self.spool()[0].read_text())
        self.assertEqual((doc["msg_class"], doc["text"]), ("operator_dm", WRAPPED))
        self.assertEqual(self.outcomes()[-1]["route"], "appserver")
        self.assertEqual(self.alerts, [])

    def test_writer_down_pastes_and_alerts_every_time(self):
        self.switch()
        for _ in range(2):
            self.assertTrue(self.deliver(writer=down))
        self.assertEqual(len(self.pasted), 2)
        self.assertEqual(len(self.alerts), 2, "every fallback use alerts Recovery")
        self.assertEqual([o["route"] for o in self.outcomes()],
                         ["unavailable", "paste-fallback"] * 2)
        self.assertEqual(self.spool(), [])

    def test_socket_down_pastes_and_alerts(self):
        self.switch()
        self.deliver(sock=down)
        self.assertEqual(len(self.pasted), 1)
        self.assertEqual(len(self.alerts), 1)

    def test_failed_paste_is_reported_and_still_alerted(self):
        self.switch()
        self.assertFalse(ar.deliver(WRAPPED, "operator_dm", lambda t: False, injector="t",
                                    home=self.home, writer_check=down, socket_check=ok,
                                    alert=self.alert))
        self.assertEqual(self.outcomes()[-1]["pasted"], False)
        self.assertEqual(len(self.alerts), 1)

    def test_undeliverable_alert_is_logged(self):
        self.switch()
        ar.deliver(WRAPPED, "operator_dm", self.paste, injector="t", home=self.home,
                   writer_check=down, socket_check=ok, alert=lambda s, b: False)
        self.assertEqual(self.outcomes()[-1]["route"], "alert-failed")

    def test_writer_claims_what_the_route_spools(self):
        """The spool format is re-implemented here; the real writer must accept it."""
        self.switch()
        self.deliver(text="poke text", cls="poke")
        b = sd.SeatBinding("consultant", cd.SeatPolicy("danger-full-access", "never"),
                           self.state / "thread.json")
        path = sd.SeatDeliveryPath(b, self.state, lambda: None, pager=lambda *a: None,
                                   restart_server=lambda: None)
        path._claim_spool()
        pending = path._pending_items()
        self.assertEqual([(d["msg_class"], d["text"]) for d in pending], [("poke", "poke text")])
        self.assertEqual(self.spool(), [])


class RouteErrorTests(Base):
    """Any exception inside the route becomes an alerted paste fallback (orchestrator, T-5306)."""

    def boom(self, *_a, **_k):
        raise RuntimeError("check exploded")

    def test_writer_check_raising_falls_back(self):
        self.switch()
        self.assertTrue(self.deliver(writer=self.boom))
        self.assertEqual(len(self.pasted), 1)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("route error", self.outcomes()[0]["reason"])

    def test_socket_check_raising_falls_back(self):
        self.switch()
        self.assertTrue(self.deliver(sock=self.boom))
        self.assertEqual((len(self.pasted), len(self.alerts)), (1, 1))

    def test_switch_with_wrong_types_falls_back_or_is_off(self):
        self.switch(state_dir=7)          # not a string -> treated as no switch
        self.assertTrue(self.deliver())
        self.assertEqual(self.pasted, [WRAPPED])

    def test_load_switch_raising_falls_back(self):
        with mock.patch.object(ar, "load_switch", side_effect=RuntimeError("bad")):
            self.assertTrue(self.deliver())
        self.assertEqual(len(self.pasted), 1)
        self.assertEqual(len(self.alerts), 1)

    def test_alert_raising_never_breaks_the_paste(self):
        self.switch()
        def bad_alert(s, b):
            raise RuntimeError("mail down")
        self.assertTrue(ar.deliver(WRAPPED, "operator_dm", self.paste, injector="t",
                                   home=self.home, writer_check=down, socket_check=ok,
                                   alert=bad_alert))
        self.assertEqual(len(self.pasted), 1)

    def test_binary_switch_file_is_off(self):
        p = self.home / ar.SWITCH_REL
        p.parent.mkdir(parents=True)
        p.write_bytes(b"\xff\xfe\x00")
        self.assertIsNone(ar.load_switch(self.home))


class ClassAndIdTests(Base):
    def test_urgent_markers(self):
        for body in ("/stop now", "/urgent look", "URGENT: x", "urgent! x"):
            self.assertEqual(ar.dm_class(f"[inbound Telegram DM from the operator | x] {body}"),
                             "operator_urgent", body)
        for body in ("hello", "this is urgent", "stop it"):
            self.assertEqual(ar.dm_class(f"[inbound Telegram DM from the operator | x] {body}"),
                             "operator_dm", body)

    def test_same_dm_same_id_so_a_retry_is_deduplicated(self):
        a = ar.make_id("consultant", "operator_dm", WRAPPED)
        self.assertEqual(a, ar.make_id("consultant", "operator_dm", WRAPPED))
        self.assertNotEqual(a, ar.make_id("consultant", "operator_dm", WRAPPED + "x"))
        self.assertTrue(cd.DELIVERY_ID_RE.match(a))

    def test_pokes_get_fresh_ids(self):
        self.assertNotEqual(ar.make_id("consultant", "poke", "same"), ar.make_id("consultant", "poke", "same"))

    def test_mailbox_id_keys_on_the_newest_message(self):
        did = ar.make_id("consultant", "mailbox", "x", unique="01792746058934221182.1925544.000179.orchestrator")
        self.assertTrue(cd.DELIVERY_ID_RE.match(did))
        self.assertIn("000179.orchestrator", did)


# ------------------------------------------------------------------ availability checks
class AvailabilityTests(Base):
    def proc(self, pid, cmdline):
        d = self.home / "proc" / str(pid)
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(cmdline)

    def test_writer_alive_needs_a_live_writer_pid(self):
        self.state.mkdir(parents=True)
        self.assertFalse(ar.writer_alive(self.state, proc_root=self.home / "proc")[0])
        (self.state / "writer.lock").write_text("4242\n")
        self.assertFalse(ar.writer_alive(self.state, proc_root=self.home / "proc")[0])
        self.proc(4242, b"/usr/bin/python3\0/x/tools/codex_seat_delivery.py\0run\0--seat\0consultant\0")
        self.assertTrue(ar.writer_alive(self.state, proc_root=self.home / "proc")[0])

    def test_recycled_pid_is_not_a_writer(self):
        self.state.mkdir(parents=True)
        (self.state / "writer.lock").write_text("4243\n")
        self.proc(4243, b"/usr/bin/sleep\0100\0")
        ok_, why = ar.writer_alive(self.state, proc_root=self.home / "proc")
        self.assertFalse(ok_)
        self.assertIn("not a codex_seat_delivery writer", why)

    def test_writer_alive_matches_the_real_writer_lock_format(self):
        self.state.mkdir(parents=True)
        lock = sd.WriterLock(self.state / "writer.lock")
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        pid = (self.state / "writer.lock").read_text().split()[0]
        self.assertEqual(pid, str(os.getpid()))

    def test_socket_accepts(self):
        path = str(self.home / "s.sock")
        self.assertFalse(ar.socket_accepts(path)[0])
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        self.addCleanup(srv.close)
        threading.Thread(target=lambda: srv.accept()[0].close(), daemon=True).start()
        self.assertTrue(ar.socket_accepts(path)[0])


# ------------------------------------------------------------------ callers
class PollerAndKeepgoingTests(Base):
    def test_poller_default_injector_is_routed(self):
        from codex_telegram import poller
        import inspect
        for fn in (poller.process_update, poller.drain_pending):
            self.assertIs(inspect.signature(fn).parameters["injector"].default, poller.inject_routed)

    def test_poller_routed_inject_with_switch_off_calls_inject(self):
        from codex_telegram import poller
        with mock.patch.object(poller, "inject", return_value=True) as inj, \
                mock.patch.object(ar, "load_switch", return_value=None):
            self.assertTrue(poller.inject_routed("codex-session", WRAPPED))
        inj.assert_called_once_with("codex-session", WRAPPED)

    def test_poller_routed_inject_with_switch_on_enqueues(self):
        from codex_telegram import poller
        self.switch()
        with mock.patch.object(poller, "inject") as inj, \
                mock.patch.object(ar, "_home", return_value=self.home), \
                mock.patch.object(ar, "writer_alive", ok), mock.patch.object(ar, "socket_accepts", ok):
            self.assertTrue(poller.inject_routed("codex-session", WRAPPED))
        inj.assert_not_called()
        self.assertEqual(json.loads(self.spool()[0].read_text())["msg_class"], "operator_dm")

    def test_poller_urgent_dm_is_routed_as_urgent(self):
        from codex_telegram import poller
        self.switch()
        with mock.patch.object(poller, "inject"), \
                mock.patch.object(ar, "_home", return_value=self.home), \
                mock.patch.object(ar, "writer_alive", ok), mock.patch.object(ar, "socket_accepts", ok):
            poller.inject_routed("codex-session",
                                 "[inbound Telegram DM from the operator | chat_id=1 | message_id=9] /stop")
        self.assertEqual(json.loads(self.spool()[0].read_text())["msg_class"], "operator_urgent")

    def test_keepgoing_classes(self):
        from codex_telegram import keepgoing
        seen = []
        with mock.patch.object(ar, "deliver", lambda text, cls, paste, **k: seen.append(cls) or True):
            keepgoing.routed_injector("nudge")("p", "t")
            keepgoing.routed_injector("kick")("p", "t")
        self.assertEqual(seen, ["poke", "controller"])

    def test_keepgoing_routed_with_switch_off_uses_inject(self):
        from codex_telegram import keepgoing
        with mock.patch.object(keepgoing, "inject", return_value=True) as inj, \
                mock.patch.object(ar, "load_switch", return_value=None):
            self.assertTrue(keepgoing.routed_injector("nudge")("codex-session", "go"))
        inj.assert_called_once_with("codex-session", "go")


class MailboxWakeTests(Base):
    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(REPO / "tools"))
        from tools import mailbox_wake as mw
        import importlib
        self.mw = mw
        self.mailbox = importlib.import_module("tools.mailbox")
        self.inbox = self.home / "inbox"
        self.mailbox.provision(self.inbox)
        self.injected = []
        self.mailbox.send(str(self.inbox), payload="p", sender="orchestrator", subject="s")
        # mailbox_wake loads its own copy of the route module, out of reach of the
        # Base patch above: give that copy the recording alert too.
        real_loader = self.mw._appserver_route_module

        def loader():
            mod = real_loader()
            mod.alert_recovery = self.alert
            return mod
        p = mock.patch.object(self.mw, "_appserver_route_module", loader)
        p.start()
        self.addCleanup(p.stop)

    def inject(self, pane, prompt, harness="claude-code"):
        self.injected.append((pane, harness))
        return True

    def run_wake(self, agent, route):
        calls = []

        def route_fn(prompt, newest, home):
            calls.append((newest, home))
            return route
        with mock.patch.object(self.mw.live_harness, "resolve", lambda a, h, **k: h):
            rc = self.mw.run(agent, inbox=self.inbox, do_inject=self.inject,
                             session_key_fn=lambda p: "S1", now=1000.0,
                             pane_idle_fn=lambda p: True,
                             unknown_pane_ok_fn=lambda p, h: True,
                             maintenance_fn=lambda a: False,
                             route_fn=route_fn if route is not None else None)
        return rc, calls

    def test_enqueued_wake_skips_the_pane_and_advances_the_marker(self):
        rc, calls = self.run_wake("codex", ("enqueued", "consultant-mailbox-x"))
        self.assertEqual(rc, 0)
        self.assertEqual(self.injected, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "/home/codex")
        marker = self.mw._read_marker(self.inbox / self.mw._MARKER_NAME)
        self.assertEqual(marker["newest"], self.mw.peek(self.inbox / "new")[-1])

    def test_unavailable_route_falls_back_to_the_pane(self):
        rc, _ = self.run_wake("codex", ("unavailable", "writer down"))
        self.assertEqual(rc, 0)
        self.assertEqual(self.injected, [("codex-session", "codex")])

    def test_route_off_is_the_old_path(self):
        rc, _ = self.run_wake("codex", ("off", None))
        self.assertEqual(self.injected, [("codex-session", "codex")])

    def test_claude_seats_never_consult_the_route(self):
        rc, calls = self.run_wake("recovery", ("enqueued", "x"))
        self.assertEqual(calls, [])
        self.assertEqual(len(self.injected), 1)

    def test_broken_route_module_with_a_switch_is_unavailable_not_silent(self):
        (self.home / ar.SWITCH_REL).parent.mkdir(parents=True)
        (self.home / ar.SWITCH_REL).write_text("{}")
        with mock.patch.object(self.mw, "_appserver_route_module", side_effect=SyntaxError("x")):
            self.assertEqual(self.mw.appserver_route_try("p", "n", str(self.home))[0], "unavailable")
        with mock.patch.object(self.mw, "_appserver_route_module", side_effect=SyntaxError("x")):
            self.assertEqual(self.mw.appserver_route_try("p", "n", str(self.home / "nobody"))[0], "off")

    def test_malformed_switch_file_never_crashes_the_wake(self):
        """orchestrator: a malformed switch must not crash _run_locked; real module, real file."""
        p = self.home / ar.SWITCH_REL
        p.parent.mkdir(parents=True)
        for content in ("{not json", '{"enabled": true}', '{"enabled": true, "seat": "c", '
                        '"state_dir": "/nonexistent/x", "socket": "/nonexistent/s"}'):
            p.write_text(content)
            status, _ = self.mw.appserver_route_try("p", "n", str(self.home))
            self.assertIn(status, ("off", "unavailable"), content)
        self.assertEqual(len(self.alerts), 1, "only the well-formed, unreachable switch alerts")

    def test_try_enqueue_raising_inside_the_wake_falls_back_to_the_pane(self):
        route = self.mw._appserver_route_module()   # the patched loader: alerts recorded
        route.try_enqueue = mock.Mock(side_effect=RuntimeError("boom"))
        route.safe_note_fallback = mock.Mock()
        # T-5660: the real route runs only for the hub inbox; this tmp inbox opts in.
        with mock.patch.object(self.mw, "_appserver_route_module", return_value=route), \
                mock.patch.object(self.mw, "_is_hub_inbox", lambda box, agent: True):
            rc, _ = self.run_wake("codex", None)
        self.assertEqual(rc, 0)
        self.assertEqual(self.injected, [("codex-session", "codex")])
        route.safe_note_fallback.assert_called_once()

    def test_real_route_module_loads_and_is_off_without_a_switch(self):
        self.assertEqual(self.mw.appserver_route_try("p", "n", str(self.home)), ("off", None))


if __name__ == "__main__":
    unittest.main()
